# SPDX-License-Identifier: MIT
"""cases/position2_support.py (second set of DP7 cases): the IS-to-RF release budget, the third-party frame split, item
reports, SSE cursors, the page row rule, the basemap staging, the PNG blob counter and the origin audit. Offline."""
import json
import struct
import sys
import tarfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position2_support as p2  # noqa: E402
import position_support as ps  # noqa: E402

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ IgateBudget

class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_budget_six_per_minute_then_wait_for_the_oldest():
    clock = Clock()
    budget = p2.IgateBudget(clock=clock, sleep=clock.sleep, margin=0)
    for _ in range(6):
        assert budget.wait(1) == 0
    # the seventh waits until the oldest of the six left the 60 s window
    assert budget.wait_seconds(1) == pytest.approx(60.0)
    assert budget.wait(1) == pytest.approx(60.0)


def test_budget_ten_per_five_minutes():
    clock = Clock()
    budget = p2.IgateBudget(clock=clock, sleep=clock.sleep, margin=0)
    for _ in range(10):
        budget.book(1)
        clock.now += 20.0          # 3 per minute: the 60 s window never fills
    # releases at t=1000, 1020, ... 1180; now = 1200: the oldest leaves the 300 s window at 1300
    assert budget.wait_seconds(1) == pytest.approx(100.0)
    assert budget.wait_seconds(2) == pytest.approx(120.0)


def test_budget_two_frames_fit_after_idle_and_margin():
    clock = Clock()
    budget = p2.IgateBudget(clock=clock, sleep=clock.sleep, margin=2.0)
    assert budget.wait_seconds(2) == 0
    budget.book(2)
    clock.now += 400
    assert budget.wait_seconds(6) == 0
    with pytest.raises(ValueError):
        budget.wait_seconds(7)


def test_budget_margin_only_when_waiting():
    clock = Clock()
    budget = p2.IgateBudget(clock=clock, sleep=clock.sleep, margin=2.0)
    budget.book(6)
    assert budget.wait_seconds(1) == pytest.approx(62.0)


# ------------------------------------------------------------------ third_party

def test_third_party_message_and_position():
    got = p2.third_party(b"}N0PSD-1>APDVT1,TCPIP,N0TEST-1*::N0PRX-2  :hello{1")
    assert got["inner_src"] == "N0PSD-1" and got["inner_dst"] == "APDVT1"
    assert got["inner_path"] == ["TCPIP", "N0TEST-1*"]
    assert got["inner_info"] == ":N0PRX-2  :hello{1"
    pos = p2.third_party("}N0PSD-1>APDVT1,TCPIP,N0TEST-1*:!4903.50N/07201.75W>dp7")
    assert pos["inner_info"].startswith("!4903.50N") and p2.is_position_info(pos["inner_info"])
    assert not p2.is_position_info(got["inner_info"])


@pytest.mark.parametrize("info", [b"!4903.50N/07201.75W>", b"}nonsense", b"}>A:x", "", b":N0PRX-2  :x"])
def test_third_party_refuses_others(info):
    assert p2.third_party(info) is None


# ------------------------------------------------------------------ item_info

def test_item_info_live_and_kill():
    plain = ps.plain_text(49.058333333, -72.029166667, 0, "/>")
    assert p2.item_info("DVT7ITM", plain) == ")DVT7ITM!4903.50N/07201.75W>"
    assert p2.item_info("DVT7ITM", plain, live=False, comment="x") == ")DVT7ITM_4903.50N/07201.75W>x"


@pytest.mark.parametrize("name", ["AB", "ABCDEFGHIJ", "AB!C", "AB_C", "AB\x7f"])
def test_item_info_name_rules(name):
    with pytest.raises(ValueError):
        p2.item_info(name, "4903.50N/07201.75W>")


# ------------------------------------------------------------------ cursors

def test_cursor_of_and_replay_path():
    assert p2.cursor_of({"id": "a1b2-17", "event": "data"}) == "a1b2-17"
    for bad in ({"id": "17"}, {"id": "a-b"}, {"id": 5}, {}, None, {"id": "a_b-1"}):
        assert p2.cursor_of(bad) is None
    frames = [(1.0, {"id": "k-1"}), (2.0, {"event": "hello"}), (3.0, {"id": "k-9"}), (4.0, {"id": "bad id"})]
    assert p2.newest_cursor(frames) == "k-9"
    assert p2.newest_cursor([]) is None
    assert p2.replay_path("k-9") == "/api/v1/public/events?last_event_id=k-9"
    assert p2.replay_path("k-9", "data") == "/api/v1/public/events?last_event_id=k-9&topics=data"
    assert p2.event_names(frames) == [None, "hello", None, None]


# ------------------------------------------------------------------ page rows

NOW = 1_800_000_000_000


def row(item_type="position", room="C", call="N0PGA", ts=NOW - 1000, rid=5, body=None, **kw):
    base = {"type": "data", "id": rid, "item_type": item_type, "room": room, "callsign": call, "protocol": "m17",
            "ts_ms": ts, "ident": "stream", "via": "m17",
            "body": body if body is not None else {"lat_e7": 105000000, "lon_e7": 202500000, "ambiguity": 0, "comment": ""}}
    base.update(kw)
    return base


def test_page_row_ok_accepts_and_rejects():
    assert p2.page_row_ok(row(), "C", NOW)
    assert not p2.page_row_ok(row(), "B", NOW)                                  # wrong room
    assert not p2.page_row_ok(row(ts=NOW - p2.KEEP_MS), "C", NOW)               # exactly 30 minutes old is out
    assert p2.page_row_ok(row(ts=NOW - p2.KEEP_MS + 1), "C", NOW)
    assert not p2.page_row_ok(row(ts=NOW + p2.SKEW_MS + 1), "C", NOW)
    assert p2.page_row_ok(row(ts=NOW + p2.SKEW_MS), "C", NOW)
    assert not p2.page_row_ok(row(ident="other"), "C", NOW)
    assert not p2.page_row_ok(row(via="web2"), "C", NOW)
    assert not p2.page_row_ok(row(call="BAD!CALL"), "C", NOW)
    assert not p2.page_row_ok(row(rid=0), "C", NOW)
    assert not p2.page_row_ok(row(body={"lat_e7": 905000000, "lon_e7": 0, "ambiguity": 0}), "C", NOW)
    assert not p2.page_row_ok(row(body={"lat_e7": 0, "lon_e7": 0, "ambiguity": 5}), "C", NOW)
    assert not p2.page_row_ok(row(body={"lat_e7": 0, "lon_e7": 0}), "C", NOW)
    obj = row("object", body={"name": "DVT7OBJ", "state": 1, "lat_e7": 1, "lon_e7": 2, "ambiguity": 0})
    assert p2.page_row_ok(obj, "C", NOW)
    assert not p2.page_row_ok(row("object", body={"name": "", "state": 1, "lat_e7": 1, "lon_e7": 2, "ambiguity": 0}), "C", NOW)
    assert not p2.page_row_ok(row("object", body={"name": "X", "state": 2, "lat_e7": 1, "lon_e7": 2, "ambiguity": 0}), "C", NOW)
    kill = row("object", body={"name": "DVT7OBJ", "state": 0})
    assert p2.page_row_ok(kill, "C", NOW)


def test_page_rows_order_kills_and_label():
    state = {"rooms": [
        {"room": "C", "positions": [
            row(call="N0PGA", ts=NOW - 5000, rid=1),
            row(call="N0PGB", ts=NOW - 1000, rid=2),
            row("object", call="N0PGO", ts=NOW - 3000, rid=3, body={"name": "DVT7OBJ", "state": 1, "lat_e7": 1, "lon_e7": 2, "ambiguity": 0}),
            row("object", call="N0PGO", ts=NOW - 2000, rid=4, body={"name": "GONE", "state": 0}),
            row(call="BAD CALL!", rid=9)]},
        {"room": "B", "positions": [row(room="B", call="N0PXX", rid=7)]}]}
    rows = p2.page_rows(state, "C", NOW)
    assert [r["id"] for r in rows] == [2, 3, 1]
    assert [p2.row_label(r) for r in rows] == ["N0PGB", "DVT7OBJ (N0PGO)", "N0PGA"]
    assert p2.page_rows({}, "C", NOW) == []


# ------------------------------------------------------------------ pmtiles and the basemap staging

def make_archive(path, bbox=(97.3, 5.6, 105.7, 20.5), max_zoom=10):
    path.write_bytes(p2.make_pmtiles_header(*bbox, max_zoom=max_zoom) + b"\x00" * 64)
    return path


def test_pmtiles_header_roundtrip_and_errors():
    head = p2.pmtiles_header(p2.make_pmtiles_header(97.3, 5.6, 105.7, 20.5, 0, 10))
    assert head["max_zoom"] == 10 and head["min_zoom"] == 0
    assert head["bbox"] == pytest.approx([97.3, 5.6, 105.7, 20.5])
    for bad in (b"", b"PMTiles\x02" + b"\0" * 120, b"X" * 127):
        with pytest.raises(ValueError):
            p2.pmtiles_header(bad)


def test_manifest_ok_matches_the_page_rules():
    good = {"version": 1, "archive": "basemap-20261106-z10.pmtiles", "files": [{"path": "basemap-20261106-z10.pmtiles"}]}
    assert p2.manifest_ok(good)
    assert not p2.manifest_ok(dict(good, version=2))
    assert not p2.manifest_ok(dict(good, archive="basemap-2026-z10.pmtiles"))
    assert not p2.manifest_ok(dict(good, archive="basemap-20261106-z16.pmtiles"))
    assert not p2.manifest_ok(dict(good, files=[{"path": "other"}]))
    assert not p2.manifest_ok(dict(good, files="x"))
    assert not p2.manifest_ok(None)


def test_stage_bare_archive_makes_the_manifest(tmp_path):
    archive = make_archive(tmp_path / "thailand.pmtiles")
    manifest = p2.stage_basemap(archive, tmp_path / "stage")
    assert manifest["archive"] == "basemap-20260101-z10.pmtiles" and manifest["maxzoom"] == 10
    assert (tmp_path / "stage" / manifest["archive"]).is_file()
    assert manifest["bbox"] == pytest.approx([97.3, 5.6, 105.7, 20.5])
    with pytest.raises(ValueError):
        p2.stage_basemap(archive, tmp_path / "stage")            # exists


def make_dir_basemap(root, with_glyph=True):
    root.mkdir()
    name = "basemap-20261106-z8.pmtiles"
    arch = make_archive(root / name, max_zoom=8)
    files = [{"path": name, "sha256": p2.sha256_file(arch)}]
    if with_glyph:
        (root / "glyphs" / "NotoSansRegular").mkdir(parents=True)
        g = root / "glyphs" / "NotoSansRegular" / "0-255.pbf"
        g.write_bytes(b"glyph")
        files.append({"path": "glyphs/NotoSansRegular/0-255.pbf", "sha256": p2.sha256_file(g)})
    (root / "MANIFEST").write_text(json.dumps({"version": 1, "archive": name, "bbox": [97.3, 5.6, 105.7, 20.5], "maxzoom": 8, "files": files}))
    return root


def test_stage_directory_and_hash_mismatch(tmp_path):
    src = make_dir_basemap(tmp_path / "out")
    assert p2.basemap_kind(src) == "dir"
    manifest = p2.stage_basemap(src, tmp_path / "stage")
    assert len(manifest["files"]) == 2 and (tmp_path / "stage" / "glyphs" / "NotoSansRegular" / "0-255.pbf").is_file()
    (src / "glyphs" / "NotoSansRegular" / "0-255.pbf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="sha256"):
        p2.stage_basemap(src, tmp_path / "stage2")
    assert p2.basemap_kind(tmp_path / "nothing") is None
    (tmp_path / "empty").mkdir()
    assert p2.basemap_kind(tmp_path / "empty") is None


def test_stage_tar_strips_one_leading_directory(tmp_path):
    src = make_dir_basemap(tmp_path / "bm")
    tar_path = tmp_path / "bm.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(src, arcname="bm")
    assert p2.basemap_kind(tar_path) == "tar"
    manifest = p2.stage_basemap(tar_path, tmp_path / "stage")
    assert manifest["archive"] == "basemap-20261106-z8.pmtiles"
    assert (tmp_path / "stage" / "MANIFEST").is_file()


def test_stage_tar_refuses_parent_names(tmp_path):
    tar_path = tmp_path / "evil.tar"
    payload = tmp_path / "payload"
    payload.write_text("x")
    with tarfile.open(tar_path, "w") as tar:
        tar.add(payload, arcname="../escape")
    with pytest.raises(ValueError, match="parent-relative"):
        p2.stage_basemap(tar_path, tmp_path / "stage")


def test_install_and_remove_only_what_was_installed(tmp_path):
    state = tmp_path / "sut-state"
    state.mkdir()
    src = make_dir_basemap(tmp_path / "out", with_glyph=False)
    manifest = p2.install_basemap(state, src)
    assert manifest["maxzoom"] == 8
    assert (state / "basemap" / "MANIFEST").is_file() and (state / "basemap" / p2.INSTALLED_MARK).is_file()
    assert not (state / "basemap.dvt-staging").exists()
    with pytest.raises(ValueError, match="exists already"):
        p2.install_basemap(state, src)
    assert p2.remove_basemap(state) is True
    assert not (state / "basemap").exists()
    # a basemap the operator put there is never removed
    (state / "basemap").mkdir()
    (state / "basemap" / "MANIFEST").write_text("{}")
    assert p2.remove_basemap(state) is False and (state / "basemap" / "MANIFEST").is_file()


def test_install_cleans_staging_after_a_bad_source(tmp_path):
    state = tmp_path / "sut-state"
    state.mkdir()
    bad = tmp_path / "bad.pmtiles"
    bad.write_bytes(b"not an archive")
    with pytest.raises(ValueError):
        p2.install_basemap(state, bad)
    assert list(state.iterdir()) == []


def test_basemap_bbox_of_each_kind_and_the_default(tmp_path):
    src = make_dir_basemap(tmp_path / "bm", with_glyph=False)
    assert p2.basemap_bbox(src) == pytest.approx([97.3, 5.6, 105.7, 20.5])
    tar_path = tmp_path / "bm.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(src, arcname="bm")
    assert p2.basemap_bbox(tar_path) == pytest.approx([97.3, 5.6, 105.7, 20.5])
    arch = tmp_path / "x.pmtiles"
    arch.write_bytes(p2.make_pmtiles_header(10.0, 20.0, 11.0, 21.0))
    assert p2.basemap_bbox(arch) == pytest.approx([10.0, 20.0, 11.0, 21.0])
    assert p2.basemap_bbox(tmp_path / "missing") == p2.DEFAULT_BBOX
    broken = tmp_path / "broken.pmtiles"
    broken.write_bytes(b"junk")
    assert p2.basemap_bbox(broken) == p2.DEFAULT_BBOX


def test_spread_points_inside_the_bbox_and_apart():
    bbox = [97.3, 5.6, 105.7, 20.5]
    pts = p2.spread_points(bbox, 4)
    assert len(pts) == 4 and len(set(pts)) == 4
    assert all(p2.inside_bbox(lat, lon, bbox) for lat, lon in pts)
    assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) > 2 for i, a in enumerate(pts) for b in pts[i + 1:])
    assert not p2.inside_bbox(10.5, 20.25, bbox)


# ------------------------------------------------------------------ PNG and blobs

def png_bytes(width, height, pixel, ctype=2):
    """A PNG (8 bit, filter type 0 or 1 on alternate rows) from pixel(x, y) -> (r, g, b)."""
    bpp = 3 if ctype == 2 else 4
    raw = bytearray()
    prev = bytearray(width * bpp)
    for y in range(height):
        line = bytearray()
        for x in range(width):
            r, g, b = pixel(x, y)
            line += bytes((r, g, b)) + (b"\xff" if ctype == 6 else b"")
        if y % 2:                                   # filter 1 (Sub) on odd rows, 2 (Up) on every fourth
            filtered = bytearray(line)
            for i in range(len(line) - 1, bpp - 1, -1):
                filtered[i] = (line[i] - line[i - bpp]) & 255
            raw += b"\x01" + filtered
        else:
            raw += b"\x00" + line
        prev = line
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


@pytest.mark.parametrize("ctype", [2, 6])
def test_decode_png_and_count_blobs(ctype):
    def pixel(x, y):
        for cx, cy in ((10, 10), (40, 12), (25, 30)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= 49:
                return p2.STATION_RGB
        if 60 <= x < 68 and 20 <= y < 28:
            return p2.OBJECT_RGB
        return (240, 240, 235)
    w, h, rows = p2.decode_png(png_bytes(80, 40, pixel, ctype))
    assert (w, h) == (80, 40) and rows[10][10] == p2.STATION_RGB
    found = p2.blobs(rows, p2.STATION_RGB)
    assert len(found) == 3
    assert all(120 <= area <= 170 for area, _x, _y in found)
    assert len(p2.blobs(rows, p2.OBJECT_RGB)) == 1
    assert p2.blobs(rows, (0, 0, 0)) == []


def test_blobs_merge_touching_circles_and_drop_specks():
    def pixel(x, y):
        if (x - 10) ** 2 + (y - 10) ** 2 <= 49 or (x - 20) ** 2 + (y - 10) ** 2 <= 49:
            return p2.STATION_RGB
        if (x, y) == (50, 5):
            return p2.STATION_RGB
        return (255, 255, 255)
    _w, _h, rows = p2.decode_png(png_bytes(60, 30, pixel))
    found = p2.blobs(rows, p2.STATION_RGB)
    assert len(found) == 1 and found[0][0] > 200            # the two circles touch; the single pixel is below min_area


def test_decode_png_refuses_others():
    with pytest.raises(ValueError):
        p2.decode_png(b"GIF89a")
    interlaced = bytearray(png_bytes(4, 4, lambda x, y: (1, 2, 3)))
    interlaced[8 + 8 + 12] = 1                                 # IHDR interlace byte (crc becomes wrong, not checked)
    with pytest.raises(ValueError):
        p2.decode_png(bytes(interlaced))


# ------------------------------------------------------------------ origins

@pytest.mark.parametrize("url,want", [
    ("https://dvt-web/api/v1/public/state", "https://dvt-web"),
    ("https://dvt-web:443/x", "https://dvt-web"),
    ("https://dvt-web:8443/x", "https://dvt-web:8443"),
    ("http://192.0.2.5:80/", "http://192.0.2.5"),
    ("wss://dvt-web/live", "wss://dvt-web"),
    ("data:image/png;base64,AAAA", None),
    ("blob:https://dvt-web/1234", None),
    ("about:blank", None),
])
def test_origin_of(url, want):
    assert p2.origin_of(url) == want


class FakeContext:
    def __init__(self):
        self.listeners, self.routes = {}, []

    def on(self, name, fn):
        self.listeners[name] = fn

    def route(self, pred, handler):
        self.routes.append((pred, handler))


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeRoute:
    def __init__(self, url):
        self.request = FakeRequest(url)
        self.aborted = False

    def abort(self):
        self.aborted = True


def test_origin_audit_records_and_aborts_foreign():
    ctx = FakeContext()
    audit = p2.OriginAudit("https://dvt-web").install(ctx)
    for url in ("https://dvt-web/", "https://dvt-web/basemap/MANIFEST", "https://tiles.example.invalid/0/0/0.pbf", "blob:https://dvt-web/1",
                "https://dvt-web/basemap/basemap-20261106-z8.pmtiles"):
        ctx.listeners["request"](FakeRequest(url))
    assert audit.origins() == ["https://dvt-web", "https://tiles.example.invalid"]
    assert audit.foreign_origins() == ["https://tiles.example.invalid"]
    pred, handler = ctx.routes[0]
    assert pred("https://tiles.example.invalid/x") and not pred("https://dvt-web/x") and not pred("blob:https://dvt-web/1")
    route = FakeRoute("https://tiles.example.invalid/x")
    handler(route)
    assert route.aborted and audit.blocked == ["https://tiles.example.invalid/x"]
    assert audit.paths("/basemap/") == ["/basemap/MANIFEST", "/basemap/basemap-20261106-z8.pmtiles"]


# ------------------------------------------------------------------ DP6c: the page (positions.js at 4ac79f26) requires hops and forwarded

def test_page_row_ok_dp6c_fields():
    assert p2.page_row_ok(row(), "C", NOW)                                      # an image before DP6c: neither key
    assert p2.page_row_ok(row(hops=0, forwarded=False), "C", NOW)
    assert p2.page_row_ok(row(hops=255, forwarded=True), "C", NOW)
    for kw in ({"hops": 256, "forwarded": False}, {"hops": -1, "forwarded": False}, {"hops": True, "forwarded": False},
               {"hops": "0", "forwarded": False}, {"hops": 0, "forwarded": 1}, {"hops": 0}, {"forwarded": False}):
        assert not p2.page_row_ok(row(**kw), "C", NOW), kw


def test_page_rows_keep_the_ssid_in_the_label_and_the_stations_apart():
    state = {"rooms": [{"room": "C", "positions": [row(call="N0PGA-9", rid=7, hops=0, forwarded=False), row(call="N0PGA", rid=6, hops=0, forwarded=False),
                                                   row(call="N0PGA-13", rid=5, hops=1, forwarded=True),
                                                   row(item_type="weather", call="N0PGA-13", rid=8, hops=0, forwarded=False, body={})]}]}
    rows = p2.page_rows(state, "C", NOW)
    assert [p2.row_label(r) for r in rows] == ["N0PGA-9", "N0PGA", "N0PGA-13"]       # types 5 and 6 are not mirrored: the DOM case sends none
    obj = row(item_type="object", call="N0PGA-9", body={"name": "OBJ1", "state": 1, "lat_e7": 1, "lon_e7": 2, "ambiguity": 0}, hops=0, forwarded=False)
    assert p2.row_label(obj) == "OBJ1 (N0PGA-9)"
