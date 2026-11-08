# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp7_positions_page.py (DP7 / MAP2 DOM case): the table text a state row must produce, the page-table reader, the
map-status poll and the end-of-case checks, against fakes of the Playwright objects. Needs the playwright package for the import of the case
module only (the tester image has it; skipped elsewhere). The selectors of cases/web_pages.py are checked against the page source strings."""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position2_support as p2  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_positions2 as t2  # noqa: E402
import test_dp7_positions_page as tp  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import web  # noqa: E402

pytestmark = pytest.mark.unit

NOW = 1_800_000_000_000


def row(item_type="position", call="N0PGA", rid=1, lat=130000000, lon=1000000000, amb=0, name=None, state=1):
    body = {"lat_e7": lat, "lon_e7": lon, "ambiguity": amb, "comment": ""}
    if item_type == "object":
        body.update(name=name or "DVT7MAP", state=state)
    return {"type": "data", "id": rid, "item_type": item_type, "room": tp.DOM_ROOM, "callsign": call, "protocol": "m17",
            "ts_ms": NOW - 500, "ident": "stream", "via": "m17", "body": body}


def test_expected_table_texts():
    rows = [row(rid=1, call="N0PGA", lat=130583333, lon=1000291667),
            row("object", rid=2, call="N0PGD", amb=2, lat=130500000, lon=1000250000)]
    want = tp.expected_table(rows)
    assert want["N0PGA"] == ("13.05833", "100.02917", "As received")
    assert want["DVT7MAP (N0PGD)"] == ("13.05", "100.02", "Reduced by the sender (2 digits omitted)")


class FakeRow:
    def __init__(self, cells):
        self.cells = cells


class FakeLocator:
    def __init__(self, items=None, count=0, text=""):
        self.items, self._count, self.text = items or [], count, text

    def all(self):
        return self.items

    def count(self):
        return self._count

    def inner_text(self):
        return self.text

    def all_inner_texts(self):
        return self.items


def test_table_texts_reads_five_cells(monkeypatch):
    rows = [FakeRow([" N0PGA ", "13.05833", "100.02917", "As received", "12:00:00"]), FakeRow(["x", "y"])]
    monkeypatch.setattr(pages, "positions_rows", lambda page: FakeLocator(rows))
    monkeypatch.setattr(pages, "positions_cells", lambda r: FakeLocator(r.cells))
    assert tp.table_texts(SimpleNamespace(page=None)) == {"N0PGA": ("13.05833", "100.02917", "As received")}


def test_map_state_ready_note_and_timeout(monkeypatch):
    waits = []
    page = SimpleNamespace(wait_for_timeout=lambda ms: waits.append(ms))
    bp = SimpleNamespace(page=page)
    monkeypatch.setattr(pages, "positions_map_note", lambda p: FakeLocator(count=0))
    assert tp.map_state(bp, 1.0) == ("ready", "")
    monkeypatch.setattr(pages, "positions_map_note", lambda p: FakeLocator(count=1, text=" Basemap not installed. "))
    assert tp.map_state(bp, 1.0) == ("note", "Basemap not installed.")
    monkeypatch.setattr(pages, "positions_map_note", lambda p: FakeLocator(count=1, text=pages.POSITIONS_NOTE_LOADING))
    assert tp.map_state(bp, 0.0) == ("loading", pages.POSITIONS_NOTE_LOADING)


def test_finish_checks_flags_foreign_origins_and_page_errors():
    audit = p2.OriginAudit("https://dvt-web")
    audit.urls = ["https://dvt-web/", "https://dvt-web/basemap/tiles.json"]
    bp = SimpleNamespace(log=SimpleNamespace(errors=[]), problems=lambda expected=(): [], engine="chromium")
    tp.finish_checks(bp, audit, "ok")
    audit.urls.append("https://tiles.example.invalid/0/0/0.pbf")
    with pytest.raises(AssertionError, match="foreign origin"):
        tp.finish_checks(bp, audit, "foreign")
    audit.urls.pop()
    bp.log.errors = ["TypeError: boom"]
    with pytest.raises(AssertionError, match="page error"):
        tp.finish_checks(bp, audit, "error")


def test_selectors_match_the_page_source_strings():
    assert pages.POSITIONS_TESTIDS == {"page": "positions-page", "room": "positions-room", "refresh": "positions-refresh",
                                       "map": "positions-map", "map_note": "positions-map-note", "attribution": "positions-attribution"}
    assert pages.positions_url() == "/#/positions"
    assert pages.POSITIONS_NOTE_MISSING.endswith(".") and pages.POSITIONS_ATTRIBUTION_HREF.startswith("https://www.openstreetmap.org/")
    assert pages.BASEMAP_TILES_JSON == "/basemap/tiles.json" and pages.BASEMAP_MANIFEST == "/basemap/MANIFEST"


class FakeAnon:
    """A web client over a table {(path, if-none-match or None): (status, headers, body bytes)}."""

    def __init__(self, table):
        self.table = table

    def get(self, path, pace=True, headers=None):
        key = (path, (headers or {}).get("If-None-Match"))
        status, hdr, body = self.table.get(key, self.table.get((path, None), (404, {"cache-control": "no-store"}, b"not found\n")))
        return web.Response(status, hdr, body)


def not_installed_table():
    return {(pages.BASEMAP_TILES_JSON, None): (200, {"content-type": "application/json", "etag": '"not-installed"', "cache-control": "no-cache"},
                                               b'{"installed":false}'),
            (pages.BASEMAP_TILES_JSON, '"not-installed"'): (304, {"etag": '"not-installed"', "cache-control": "no-cache"}, b"")}


def test_not_installed_surface_accepts_the_map2_answers():
    tp.assert_not_installed_surface(FakeAnon(not_installed_table()), "ok", extra_404=["/basemap/basemap-20261106-z8.pmtiles"])


def test_not_installed_surface_rejects_the_map1_manifest_and_a_wrong_descriptor():
    table = not_installed_table()
    table[(pages.BASEMAP_MANIFEST, None)] = (200, {}, b"{}")
    with pytest.raises(AssertionError, match="MANIFEST.*want 404"):
        tp.assert_not_installed_surface(FakeAnon(table), "manifest")
    table = not_installed_table()
    table[(pages.BASEMAP_TILES_JSON, None)] = (404, {"cache-control": "no-store"}, b"not found\n")
    with pytest.raises(AssertionError, match="want 200"):
        tp.assert_not_installed_surface(FakeAnon(table), "404")
    table = not_installed_table()
    table[(pages.BASEMAP_TILES_JSON, None)] = (200, {"content-type": "application/json", "etag": 'W/"x"', "cache-control": "no-cache"}, b'{"installed":false}')
    with pytest.raises(AssertionError, match="etag"):
        tp.assert_not_installed_surface(FakeAnon(table), "weak etag")
    table = not_installed_table()
    table[("/basemap/", None)] = (404, {"cache-control": "max-age=60"}, b"")
    with pytest.raises(AssertionError, match="no-store"):
        tp.assert_not_installed_surface(FakeAnon(table), "cache")


def test_missing_page_asserts_tiles_json_is_the_only_basemap_request(monkeypatch):
    waits = []
    monkeypatch.setattr(tp, "open_positions", lambda bp: None)
    monkeypatch.setattr(tp, "assert_list_fallback", lambda bp, anon, label: {})
    monkeypatch.setattr(tp, "expect", lambda target: SimpleNamespace(to_have_text=lambda *a, **k: None, to_have_class=lambda *a, **k: None,
                                                                    to_be_hidden=lambda *a, **k: None, to_have_count=lambda *a, **k: None))
    for name in ("positions_map_note", "positions_map", "positions_attribution"):
        monkeypatch.setattr(pages, name, lambda page: None)
    audit = p2.OriginAudit("https://dvt-web")
    audit.urls = ["https://dvt-web/", "https://dvt-web/basemap/tiles.json"]
    bp = SimpleNamespace(page=None, log=SimpleNamespace(errors=[], statuses=lambda path: [200]), problems=lambda expected=(): [], engine="chromium")
    tp.assert_missing_page(bp, None, audit, "ok")
    audit.urls.append("https://dvt-web/basemap/MANIFEST")
    with pytest.raises(AssertionError, match="under /basemap/"):
        tp.assert_missing_page(bp, None, audit, "manifest")
    audit.urls.pop()
    bp.log.statuses = lambda path: [404]
    with pytest.raises(AssertionError, match="saw"):
        tp.assert_missing_page(bp, None, audit, "descriptor 404")
    assert waits == []


def test_the_seed_callsigns_are_distinct_from_the_other_position_cases():
    mine = list(tp.STATIONS) + [tp.OBJECT_REPORTER]
    others = {t2.CORR_OK, t2.CORR_NOCREDIT, t2.CORR_OFF, t2.CORR_TXOFF, t2.CORR_POSOFF, t2.CORR_HIDE, t2.HIDE_RF_CALL, t2.HIDE_RF_CONTROL,
              t2.SSE_MARK, t2.SSE_CONTROL, t2.SSE_HIDE, t2.RETENTION_CALL, *t2.ITEM_CALL.values(), "N0PEL", "N0PEP", "N0PEO",
              p1.OPT_CALL, p1.OPT_DMR, p1.HIDE_CALL, p1.OBJECT_REPORTER, p1.AMB_DPRS_CALL}
    for table in (p1.A_CALL, p1.CONTROL_CALL, p1.OFF_CALL, p1.AMB_M17_CALL, p1.AMB_RF_CALL):
        others |= set(table.values())
    assert len(set(mine)) == len(mine) and not set(mine) & others
    assert tp.OBJECT_NAME not in set(t2.ITEM_NAME.values()) and tp.OBJECT_NAME != p1.OBJECT_NAME
    assert tp.DOM_ROOM != p1.MODULE and re.fullmatch(r"[A-Z]", tp.DOM_ROOM)


def installed_table(desc, manifest):
    """The answers of an installed base as dv-svc-web gives them (crates/svc/web/src/basemap.rs), keyed like FakeAnon wants."""
    gen = desc["gen"]
    etag = '"' + "ab" * 32 + '"'
    faces = {"NotoSansRegular": [{"url": "glyphs/fonts/NotoSans-Regular.ttf", "unicode-range": ["U+20-7E"]}]}
    mvt = {"content-type": "application/vnd.mapbox-vector-tile", "cache-control": "public, max-age=31536000, immutable"}
    return {
        (pages.BASEMAP_TILES_JSON, None): (200, {"content-type": "application/json", "etag": etag, "cache-control": "no-cache"}, json.dumps(desc).encode()),
        (pages.BASEMAP_TILES_JSON, etag): (304, {"etag": etag}, b""),
        (tp.FONT_FACES, None): (200, {"content-type": "application/json"}, json.dumps(faces).encode()),
        (tp.GLYPH_RANGE, None): (200, {}, b"x" * 100),
        (tp.GLYPH_RANGE, "range"): (206, {"content-range": "bytes 0-9/100", "accept-ranges": "bytes"}, b"x" * 10),
        ("/basemap/tiles/%s/0/0/0.mvt" % gen, None): (200, dict(mvt, **{"content-encoding": "gzip"}), b"\x1f\x8bzzz"),
        ("/basemap/tiles/%s/%d/1/459.mvt" % (gen, desc["maxzoom"]), None): (204, mvt, b""),
    }


class FakeBaseAnon(FakeAnon):
    def get(self, path, pace=True, headers=None):
        if path == tp.GLYPH_RANGE and (headers or {}).get("Range"):
            status, hdr, body = self.table[(path, "range")]
            return web.Response(status, hdr, body)
        return super().get(path, pace, headers)


def test_base_surface_accepts_the_map2_answers_and_rejects_deviations():
    from test_position_map2_support import good_descriptor
    desc = good_descriptor()
    desc["files"].append({"path": "glyphs/fonts/NotoSans-Regular.ttf", "bytes": 1, "sha256": "3" * 64})
    manifest = {"overview_maxzoom": 5, "detail_maxzoom": 12,
                "tiers": [{"role": "overview", "archive": "o.pmtiles", "bbox": [-180, -85, 180, 85]},
                          {"role": "detail", "archive": "d.pmtiles", "bbox": desc["coverage"]["boxes"][0]}]}
    table = installed_table(desc, manifest)
    assert tp.assert_base_surface(FakeBaseAnon(table), manifest, desc) == "/basemap/tiles/%s/0/0/0.mvt" % desc["gen"]
    gen = desc["gen"]
    broken = dict(table)
    broken[("/basemap/tiles/%s/%d/1/459.mvt" % (gen, desc["maxzoom"]), None)] = (200, {}, b"x")
    with pytest.raises(AssertionError, match="204"):
        tp.assert_base_surface(FakeBaseAnon(broken), manifest, desc)
    broken = dict(table)
    broken[(pages.BASEMAP_MANIFEST, None)] = (200, {}, b"{}")
    with pytest.raises(AssertionError, match="not served"):
        tp.assert_base_surface(FakeBaseAnon(broken), manifest, desc)
    broken = dict(table)
    broken[("/basemap/tiles/%s/0/0/0.mvt" % gen, None)] = (204, {}, b"")
    with pytest.raises(AssertionError, match="want 200"):
        tp.assert_base_surface(FakeBaseAnon(broken), manifest, desc)
    wrong = dict(desc, maxzoom=11)
    with pytest.raises(AssertionError, match="zooms"):
        tp.assert_base_surface(FakeBaseAnon(installed_table(wrong, manifest)), manifest, wrong)
