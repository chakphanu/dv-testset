# SPDX-License-Identifier: MIT
"""dvtest/basemap_fixture.py: the glyph catalog, the PMTiles v3 archive, builds.json and the loopback server that
`dv-svc-basemap install` (DV-Router MAP2g) reads. Everything is decoded here from the specifications, not with the encoder under
test, and the HTTP behaviour (HEAD size and strong ETag, exact 206 Content-Range, If-Match 412, 416) is checked against the live
server. DVT_BASEMAP_BIN (a MAP2g dv-svc-basemap) adds one real install + verify; without it that case skips."""
import gzip
import http.client
import json
import os
import re
import struct
from pathlib import Path

import pytest

from dvtest import basemap_fixture as fx

pytestmark = pytest.mark.unit

FONT_URL_RE = re.compile(r"^glyphs/fonts/[A-Za-z0-9_-]+\.(ttf|otf)$")
RANGE_RE = re.compile(r"^U\+[0-9A-F]{1,6}(-[0-9A-F]{1,6})?$")


# ---------------------------------------------------------------- tiny decoders (independent of the encoder)

def read_varint(data, pos):
    value, shift = 0, 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def pb_fields(data):
    """[(field number, wire type, value)] of one protobuf message; wire 0 -> int, wire 2 -> bytes."""
    out, pos = [], 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 2:
            size, pos = read_varint(data, pos)
            value, pos = data[pos:pos + size], pos + size
        else:
            raise AssertionError("unexpected wire type %d" % wire)
        out.append((field, wire, value))
    return out


def parse_header(archive):
    assert archive[:8] == b"PMTiles\x03"
    ranges = [struct.unpack_from("<QQ", archive, 8 + 16 * i) for i in range(4)]
    counts = struct.unpack_from("<QQQ", archive, 72)
    flags = archive[96:102]
    min_lon, min_lat, max_lon, max_lat = struct.unpack_from("<iiii", archive, 102)
    center_zoom, center_lon, center_lat = struct.unpack_from("<Bii", archive, 118)
    assert len(archive) >= 127
    return {"root": ranges[0], "metadata": ranges[1], "leaves": ranges[2], "tiles": ranges[3],
            "addressed": counts[0], "entries": counts[1], "contents": counts[2],
            "clustered": flags[0], "internal": flags[1], "tile_comp": flags[2], "tile_type": flags[3],
            "min_zoom": flags[4], "max_zoom": flags[5], "bounds": (min_lon, min_lat, max_lon, max_lat),
            "center": (center_zoom, center_lon, center_lat)}


def parse_directory(raw):
    count, pos = read_varint(raw, 0)
    ids, pos_ids = [], pos
    last = 0
    for _ in range(count):
        delta, pos_ids = read_varint(raw, pos_ids)
        last += delta
        ids.append(last)
    pos = pos_ids
    runs, lengths, offsets = [], [], []
    for _ in range(count):
        value, pos = read_varint(raw, pos)
        runs.append(value)
    for _ in range(count):
        value, pos = read_varint(raw, pos)
        lengths.append(value)
    expect = None
    for index in range(count):
        value, pos = read_varint(raw, pos)
        offset = expect if value == 0 else value - 1
        assert offset is not None, "first offset cannot be implicit"
        offsets.append(offset)
        expect = offset + lengths[index]
    assert pos == len(raw), "trailing directory bytes"
    return list(zip(ids, offsets, lengths, runs))


@pytest.fixture(scope="module")
def archive():
    return fx.build_pmtiles()


@pytest.fixture(scope="module")
def entries(archive):
    head = parse_header(archive)
    start, size = head["root"]
    return parse_directory(gzip.decompress(archive[start:start + size]))


# ---------------------------------------------------------------- determinism

def test_every_byte_is_deterministic():
    assert fx.build_pmtiles() == fx.build_pmtiles()
    assert fx.catalog_bytes() == fx.catalog_bytes()
    assert fx.glyph_assets() == fx.glyph_assets()
    assert fx.mvt_tile(3, 1, 2) == fx.mvt_tile(3, 1, 2) and fx.mvt_tile(3, 1, 2) != fx.mvt_tile(3, 1, 3)


def test_golden_digests_pin_the_generated_bytes():
    """A change of the archive or catalog bytes must be deliberate: update these when the fixture design changes."""
    archive = fx.build_pmtiles()
    assert (len(archive), fx.sha256_hex(archive)) == (4268, "44be3893547c7a74a6cbcc73719ed4c5f29e6a17743e145207e30a73b4131c80")
    catalog = fx.catalog_bytes()
    assert (len(catalog), fx.sha256_hex(catalog)) == (78981, "abc0953c1a4e3175c5b1e58bed7a07f762c08ae6d4abdb7df253627335d439bc")


def test_gzip_stored_is_valid_gzip_for_any_size():
    for size in (0, 1, 65534, 65535, 65536, 140000):
        data = bytes((i * 7) & 255 for i in range(size))
        assert gzip.decompress(fx.gzip_stored(data)) == data


# ---------------------------------------------------------------- glyph catalog

def test_catalog_has_all_ranges_notices_face_map_and_font():
    entries_ = fx.catalog_entries()
    paths = [e["path"] for e in entries_]
    assert len(paths) == len(set(paths)) == 260
    for start in range(0, 65536, 256):
        assert "glyphs/NotoSansRegular/%d-%d.pbf" % (start, start + 255) in paths
    for need in ("glyphs/OFL.txt", "glyphs/fonts/OFL.txt", "glyphs/FONT-FACES.json", "glyphs/fonts/Fixture.ttf"):
        assert need in paths
    assert len(paths) <= 508                                   # MAX_CATALOG_ENTRIES of the installer
    raw = fx.catalog_bytes()
    assert raw.isascii() and len(raw) <= 512 * 1024
    assert json.loads(raw) == entries_


def test_catalog_pins_match_the_served_assets_and_have_one_valid_source_each():
    assets = dict(fx.glyph_assets())
    for entry in fx.catalog_entries():
        data = assets[entry["path"]]
        assert entry["bytes"] == len(data) >= 1
        assert entry["sha256"] == fx.sha256_hex(data) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert set(entry) == {"path", "bytes", "sha256", "sources"}
        (source,) = entry["sources"]
        assert source == {"url": "https://example.org/" + entry["path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
        assert len(entry["path"]) <= 160 and all(p and not p.startswith(".") and p not in (".", "..") for p in entry["path"].split("/"))


def test_each_pbf_is_the_glyphs_proto_envelope_of_its_own_range():
    for path, data in fx.glyph_assets():
        m = re.fullmatch(r"glyphs/NotoSansRegular/(\d+)-(\d+)\.pbf", path)
        if not m:
            continue
        start, end = int(m.group(1)), int(m.group(2))
        assert start % 256 == 0 and end == start + 255 and end <= 65535
        (top,) = pb_fields(data)                                # exactly one font stack, field 1
        assert top[0] == 1 and top[1] == 2
        stack = pb_fields(top[2])
        assert [(f, v) for f, w, v in stack] == [(1, b"NotoSansRegular"), (2, ("%d-%d" % (start, end)).encode())]
    assert len(dict(fx.glyph_assets())["glyphs/NotoSansRegular/0-255.pbf"]) == 26   # the size the DV-Router contract quotes


def test_face_map_obeys_the_pages_rules_and_names_a_catalog_font():
    paths = {e["path"] for e in fx.catalog_entries()}
    doc = json.loads(dict(fx.glyph_assets())["glyphs/FONT-FACES.json"])
    assert list(doc) == ["NotoSansRegular"]
    faces = doc["NotoSansRegular"]
    assert 1 <= len(faces) <= 250
    for face in faces:
        assert set(face) == {"url", "unicode-range"}
        assert FONT_URL_RE.match(face["url"]) and face["url"] in paths
        assert face["unicode-range"] and all(RANGE_RE.match(r) for r in face["unicode-range"])
    assert doc == {"NotoSansRegular": [{"url": "glyphs/fonts/Fixture.ttf", "unicode-range": ["U+0-10FFFF"]}]}   # the contract's example


def test_font_is_an_sfnt_with_a_cmap_table_inside_the_file():
    font = dict(fx.glyph_assets())["glyphs/fonts/Fixture.ttf"]
    assert font[:4] == b"\x00\x01\x00\x00"
    (tables,) = struct.unpack_from(">H", font, 4)
    assert tables == 1
    tag, checksum, offset, length = struct.unpack_from(">4sIII", font, 12)
    assert tag == b"cmap" and 12 + 16 * tables <= offset and offset + length <= len(font)
    assert checksum == sum(struct.unpack(">%dI" % (length // 4), font[offset:offset + length])) & 0xFFFFFFFF


# ---------------------------------------------------------------- tile ids

def test_tile_ids_follow_the_spec_vectors_and_invert():
    # the first ids of the PMTiles v3 numbering: z0 = 0, z1 = 1..4 (Hilbert order), z2 starts at 5
    assert fx.tile_id(0, 0, 0) == 0
    assert [fx.tile_id(1, x, y) for x, y in ((0, 0), (0, 1), (1, 1), (1, 0))] == [1, 2, 3, 4]
    assert fx.tile_id(2, 0, 0) == 5
    for z in range(0, 15):
        n = 1 << z
        for x, y in ((0, 0), (n - 1, 0), (0, n - 1), (n - 1, n - 1), (n // 3, n // 5)):
            assert fx.tile_coords(fx.tile_id(z, x, y)) == (z, x, y)


# ---------------------------------------------------------------- PMTiles archive

def test_header_is_a_valid_v3_header_with_disjoint_sections(archive):
    head = parse_header(archive)
    assert (head["clustered"], head["internal"], head["tile_comp"], head["tile_type"]) == (1, 2, 2, 1)   # clustered, gzip, gzip, MVT
    assert (head["min_zoom"], head["max_zoom"]) == (0, fx.MAX_ZOOM)
    assert head["root"][0] == 127 and head["root"][0] + head["root"][1] <= 16384
    sections = [head[n] for n in ("root", "metadata", "leaves", "tiles")]
    assert head["leaves"][1] == 0
    live = sorted(s for s in sections if s[1])
    for a, b in zip(live, live[1:]):
        assert a[0] + a[1] <= b[0]
    assert max(s[0] + s[1] for s in sections) == len(archive)
    west, south, east, north = head["bounds"]
    assert -1800000000 <= west < east <= 1800000000 and -900000000 <= south < north <= 900000000
    assert head["addressed"] == head["entries"] == head["contents"] == len(fx.tile_list())


def test_metadata_is_gzip_json_with_vector_layers(archive):
    head = parse_header(archive)
    start, size = head["metadata"]
    meta = json.loads(gzip.decompress(archive[start:start + size]))
    assert isinstance(meta["vector_layers"], list) and meta["vector_layers"]
    assert meta["vector_layers"][0]["id"] == "earth"


def test_directory_is_sorted_contiguous_and_every_tile_is_a_gzip_mvt(archive, entries):
    head = parse_header(archive)
    ids = [e[0] for e in entries]
    assert ids == sorted(set(ids)) and all(e[3] == 1 for e in entries)
    tiles_start, tiles_size = head["tiles"]
    cursor = 0
    for tid, offset, length, _run in entries:
        assert offset == cursor and length > 0
        cursor += length
        blob = archive[tiles_start + offset:tiles_start + offset + length]
        assert blob[:2] == b"\x1f\x8b"
        z, x, y = fx.tile_coords(tid)
        (layer_field,) = pb_fields(gzip.decompress(blob))                 # one layer, Tile.layers = field 3
        assert layer_field[0] == 3
        layer = pb_fields(layer_field[2])
        assert (1, 2, b"earth") in layer
        assert (15, 0, 2) in layer and (5, 0, 4096) in layer                  # version 2, extent 4096
        values = [v for f, w, v in layer if f == 4]
        assert pb_fields(values[1])[0][2] == ("%d/%d/%d" % (z, x, y)).encode()
        (feature,) = [v for f, w, v in layer if f == 2]
        kinds = {f: v for f, w, v in pb_fields(feature)}
        assert kinds[3] == 3                                                  # POLYGON
    assert cursor == tiles_size


def test_pyramid_covers_the_fixture_point_at_every_zoom_and_leaves_gaps(entries):
    have = {fx.tile_coords(e[0]) for e in entries}
    for z in range(fx.MAX_ZOOM + 1):
        assert (z,) + fx.lonlat_tile(fx.FIXTURE_LON, fx.FIXTURE_LAT, z) in have
    assert {(1, x, y) for x in (0, 1) for y in (0, 1)} <= have
    assert (0, 0, 0) in have
    for lon, lat in fx.REGION_POINTS.values():
        for z in range(fx.MAX_ZOOM + 1):
            assert (z,) + fx.lonlat_tile(lon, lat, z) in have
    assert (5, 0, 0) not in have and (12,) + fx.lonlat_tile(-179.9, 80.0, 12) not in have     # absent tiles answer 204


@pytest.mark.parametrize("profile,overview,detail", [("hotspot", 5, 12), ("router", 6, 14)])
def test_every_selected_tier_of_a_profile_finds_tiles(entries, profile, overview, detail):
    """The installer fails with `selected tier has no tiles` when a tier box holds none: the overview needs one tile at z0..overview
    (world box), the detail box needs one at z(overview+1)..detail (the default dvt box and the Thailand box)."""
    have = {fx.tile_coords(e[0]) for e in entries}
    assert any(z <= overview for z, _, _ in have)

    def inside(box, z, x, y):
        west, south, east, north = box
        x0, y1 = fx.lonlat_tile(west, south, z)
        x1, y0 = fx.lonlat_tile(east, north, z)
        return x0 <= x <= x1 and y0 <= y <= y1

    for box in ([float(v) for v in fx.DEFAULT_DETAIL[1].split(",")], [97.3, 5.6, 105.7, 20.5]):
        found = [(z, x, y) for z, x, y in have if overview < z <= detail and inside(box, z, x, y)]
        assert {z for z, _, _ in found} == set(range(overview + 1, detail + 1)), (profile, box)


def test_cells_are_z10_tiles_that_hold_their_point_and_lie_outside_the_base_detail_box():
    base = [float(v) for v in fx.DEFAULT_DETAIL[1].split(",")]
    for lon, lat in [(fx.FIXTURE_LON, fx.FIXTURE_LAT)] + list(fx.REGION_POINTS.values()):
        cell = fx.cell_of(lat, lon)
        z, x, y = (int(v) for v in cell.split("/"))
        assert z == 10 and 0 <= x < 1024 and 0 <= y < 1024
        west, south, east, north = fx.cell_bbox(cell)
        assert west <= lon < east and south <= lat < north
        assert abs((east - west) - 360.0 / 1024) < 1e-9
        inside_base = not (east < base[0] or west > base[2] or north < base[1] or south > base[3])
        assert inside_base == ((lon, lat) == (fx.FIXTURE_LON, fx.FIXTURE_LAT)), cell
    assert fx.cell_of(18.79, 98.98) == "10/793/457" and fx.cell_of(7.88, 98.39) == "10/791/489"


@pytest.mark.parametrize("profile,first,last", [("hotspot", 6, 12), ("router", 7, 14)])
def test_a_lazy_cell_extract_finds_tiles_at_every_installed_detail_zoom(entries, profile, first, last):
    """MAP2d plans the cell's box at overview_maxzoom+1 .. detail_maxzoom and fails a cell with `no source tiles` when none exist:
    every region point must have a tile in its cell box at each of those zooms (inclusive tile coordinates, as the planner)."""
    have = {fx.tile_coords(e[0]) for e in entries}
    for lon, lat in fx.REGION_POINTS.values():
        west, south, east, north = fx.cell_bbox(fx.cell_of(lat, lon))
        for z in range(first, last + 1):
            x0, y1 = fx.lonlat_tile(west, south, z)
            x1, y0 = fx.lonlat_tile(east, north, z)
            assert any(x0 <= x <= x1 and y0 <= y <= y1 for zz, x, y in have if zz == z), (profile, lon, lat, z)


def test_builds_json_has_the_fields_the_installer_reads(archive):
    (build,) = json.loads(fx.builds_json(len(archive)))
    assert set(build) == {"key", "size", "version"}
    assert re.fullmatch(r"\d{8}\.pmtiles", build["key"]) and build["key"] == fx.ARCHIVE_KEY
    assert build["size"] == len(archive) >= 127
    assert re.fullmatch(r"4\.\d+\.\d+", build["version"])


def test_etag_is_strong_and_bounded(archive):
    tag = fx.strong_etag(archive)
    assert tag.startswith('"') and tag.endswith('"') and 3 <= len(tag) <= 256 and not tag.startswith("W/")
    assert all(0x21 <= ord(c) <= 0x7E for c in tag)


# ---------------------------------------------------------------- Range parsing

def test_parse_range_cases():
    assert fx.parse_range("bytes=0-9", 100) == ("ok", 0, 9)
    assert fx.parse_range("bytes=90-", 100) == ("ok", 90, 99)
    assert fx.parse_range("bytes=90-500", 100) == ("ok", 90, 99)           # last is clamped
    assert fx.parse_range("bytes=-10", 100) == ("ok", 90, 99)
    assert fx.parse_range("bytes=-500", 100) == ("ok", 0, 99)
    assert fx.parse_range("bytes=100-120", 100) == ("unsatisfiable",)
    assert fx.parse_range("bytes=-0", 100) == ("unsatisfiable",)
    for ignored in (None, "items=0-1", "bytes=0-1,5-6", "bytes=9-3", "bytes=a-b", "bytes=5"):
        assert fx.parse_range(ignored, 100) == ("ignore",), ignored
    assert fx.if_match_ok(None, '"a"') and fx.if_match_ok("*", '"a"') and fx.if_match_ok('"b", "a"', '"a"')
    assert not fx.if_match_ok('"b"', '"a"') and not fx.if_match_ok('W/"a"', '"a"')


# ---------------------------------------------------------------- the live server

@pytest.fixture(scope="module")
def server():
    with fx.BasemapFixture() as fixture:
        yield fixture


def call(server, method, path, headers=None):
    host, port = server.url[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_server_binds_literal_loopback_and_urls_are_what_the_installer_accepts(server):
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+", server.url)
    assert server.index_url == server.url + "/builds.json"
    assert server.source_base == server.url + "/archives" and server.glyph_base == server.url + "/mirror"


def test_index_lists_the_served_archive_with_its_real_size(server):
    status, headers, body = call(server, "GET", "/builds.json")
    assert status == 200 and int(headers["content-length"]) == len(body)
    (build,) = json.loads(body)
    status, headers, body = call(server, "HEAD", "/archives/" + build["key"])
    assert status == 200 and body == b"" and int(headers["content-length"]) == build["size"] == len(server.archive)


def test_head_gives_size_strong_etag_and_range_support(server):
    status, headers, body = call(server, "HEAD", "/archives/" + fx.ARCHIVE_KEY)
    assert status == 200 and body == b""
    assert headers["etag"] == fx.strong_etag(server.archive) and not headers["etag"].startswith("W/")
    assert headers["accept-ranges"] == "bytes" and int(headers["content-length"]) == len(server.archive)


def test_range_get_answers_exact_206_with_content_range_and_length(server):
    etag = fx.strong_etag(server.archive)
    size = len(server.archive)
    for first, last in ((0, 126), (127, 300), (size - 5, size - 1), (10, 10)):
        status, headers, body = call(server, "GET", "/archives/" + fx.ARCHIVE_KEY,
                                     {"Range": "bytes=%d-%d" % (first, last), "If-Match": etag})
        assert status == 206
        assert headers["content-range"] == "bytes %d-%d/%d" % (first, last, size)
        assert int(headers["content-length"]) == last - first + 1 == len(body)
        assert body == server.archive[first:last + 1]
        assert headers["etag"] == etag


def test_if_match_mismatch_is_412_for_get_and_head_and_star_matches(server):
    for method in ("GET", "HEAD"):
        status, _, body = call(server, method, "/archives/" + fx.ARCHIVE_KEY, {"Range": "bytes=0-9", "If-Match": '"stale"'})
        assert status == 412 and body == b""
    status, _, body = call(server, "GET", "/archives/" + fx.ARCHIVE_KEY, {"Range": "bytes=0-9", "If-Match": "*"})
    assert status == 206 and body == server.archive[:10]


def test_unsatisfiable_range_is_416_with_the_size(server):
    size = len(server.archive)
    status, headers, body = call(server, "GET", "/archives/" + fx.ARCHIVE_KEY, {"Range": "bytes=%d-%d" % (size, size + 5)})
    assert status == 416 and headers["content-range"] == "bytes */%d" % size and body == b""


def test_get_without_range_is_the_whole_archive(server):
    status, headers, body = call(server, "GET", "/archives/" + fx.ARCHIVE_KEY)
    assert status == 200 and body == server.archive and int(headers["content-length"]) == len(body)


def test_mirror_serves_every_catalog_file_with_matching_size_and_sha256(server):
    for entry in fx.catalog_entries():
        status, headers, body = call(server, "GET", "/mirror/" + entry["path"])
        assert status == 200, entry["path"]
        assert len(body) == entry["bytes"] == int(headers["content-length"])
        assert fx.sha256_hex(body) == entry["sha256"] == entry["sources"][0]["sha256"]


def test_unknown_paths_are_404_and_nothing_redirects(server):
    for path in ("/", "/mirror/glyphs/NotoSansRegular/1-256.pbf", "/mirror/../builds.json", "/archives/20990101.pmtiles",
                 "/builds.json.bak", "/mirror/"):
        status, headers, _ = call(server, "GET", path)
        assert status == 404 and "location" not in headers, path


def test_request_log_records_method_range_and_if_match(server):
    before = len(server.requests)
    call(server, "GET", "/archives/" + fx.ARCHIVE_KEY, {"Range": "bytes=0-3", "If-Match": fx.strong_etag(server.archive)})
    row = server.requests[before]
    assert (row["method"], row["path"], row["range"], row["status"]) == ("GET", "/archives/" + fx.ARCHIVE_KEY, "bytes=0-3", 206)
    assert row["if_match"] == fx.strong_etag(server.archive)


def test_summarize_counts_a_lazy_job_and_flags_bad_ranges():
    rows = [
        {"method": "GET", "path": "/builds.json", "range": None, "if_match": None, "status": 200},
        {"method": "HEAD", "path": "/archives/" + fx.ARCHIVE_KEY, "range": None, "if_match": None, "status": 200},
        {"method": "GET", "path": "/archives/" + fx.ARCHIVE_KEY, "range": "bytes=0-126", "if_match": '"t"', "status": 206},
        {"method": "GET", "path": "/archives/" + fx.ARCHIVE_KEY, "range": "bytes=0-9", "if_match": None, "status": 206},
        {"method": "GET", "path": "/archives/" + fx.ARCHIVE_KEY, "range": None, "if_match": None, "status": 200},
        {"method": "GET", "path": "/mirror/glyphs/OFL.txt", "range": None, "if_match": None, "status": 200},
        {"method": "GET", "path": "/x", "range": None, "if_match": None, "status": 404},
    ]
    got = fx.summarize(rows)
    assert (got["total"], got["builds"], got["head"], got["range_get"], got["full_get"], got["mirror"], got["other"]) == (7, 1, 1, 2, 1, 1, 1)
    assert got["bad_ranges"] == 1 and got["statuses"] == {"200": 4, "206": 2, "404": 1}
    assert fx.summarize([])["total"] == 0 and fx.summarize([])["head"] == 0


def test_stats_listener_serves_only_the_request_log_and_the_access_log_gets_a_line_per_request():
    import io
    log = io.StringIO()
    with fx.BasemapFixture(stats_bind=("127.0.0.1", 0), access_log=log) as fixture:
        stats_port = fixture._stats.server_address[1]
        status, _, _ = call(fixture, "GET", "/builds.json")
        assert status == 200
        conn = http.client.HTTPConnection("127.0.0.1", stats_port, timeout=10)
        conn.request("GET", "/requests")
        resp = conn.getresponse()
        doc = json.loads(resp.read())
        assert resp.status == 200 and doc["summary"]["builds"] == 1 and doc["requests"][0]["path"] == "/builds.json"
        conn.close()
        for path, want in (("/health", 200), ("/builds.json", 404), ("/mirror/glyphs/OFL.txt", 404)):
            conn = http.client.HTTPConnection("127.0.0.1", stats_port, timeout=10)
            conn.request("GET", path)
            assert conn.getresponse().status == want, path
            conn.close()
        assert len(fixture.requests) == 1                      # the stats listener does not log itself
    assert log.getvalue().splitlines() == ["1 GET /builds.json range=- if-match=- -> 200"]


def test_install_args_carry_the_four_map2g_overrides(server, tmp_path):
    args = server.install_args(tmp_path / "state", tmp_path / "CATALOG.json")
    assert args[0] == "install" and args[args.index("--profile") + 1] == "hotspot"
    assert args[args.index("--detail") + 1] == "dvt=99.9,13.0,101.1,14.1"
    assert args[args.index("--source-index") + 1] == server.url + "/builds.json"
    assert args[args.index("--source-base") + 1] == server.url + "/archives"
    assert args[args.index("--glyph-base") + 1] == server.url + "/mirror"
    assert args[args.index("--glyph-catalog") + 1].endswith("CATALOG.json") and args[args.index("--build") + 1] == fx.BUILD_DATE


def test_install_refuses_an_existing_basemap_and_a_missing_binary(tmp_path):
    (tmp_path / "basemap").mkdir()
    with pytest.raises(fx.InstallError, match="not overwritten"):
        fx.install_tree("/bin/true", tmp_path)
    with pytest.raises(fx.InstallError, match="not an executable"):
        fx.install_tree(tmp_path / "missing", tmp_path / "other")


# ---------------------------------------------------------------- the real tool (only with a MAP2g binary)

def test_real_install_and_verify_when_a_binary_is_given(tmp_path):
    binary = os.environ.get("DVT_BASEMAP_BIN", "").strip()
    if not binary:
        pytest.skip("DVT_BASEMAP_BIN (a dv-svc-basemap with --glyph-base, DV-Router MAP2g or later) is not set")
    report = fx.install_tree(binary, tmp_path / "state")
    assert [t["name"] for t in report["tiers"]] == ["world", "dvt"]
    assert all(t["tiles"] >= 1 for t in report["tiers"])
    base = Path(tmp_path / "state" / "basemap" / "base")
    manifest = json.loads((base / "MANIFEST").read_text())
    assert manifest["version"] == 2 and manifest["glyph_catalog"] == {"kind": "operator", "sha256": fx.sha256_hex(fx.catalog_bytes())}
    assert (base / "GLYPH-CATALOG.json").read_bytes() == fx.catalog_bytes()
    assert len(manifest["files"]) == 260
    plan = json.loads((tmp_path / "state" / fx.PLAN_NAME).read_text())
    assert plan["dry_run"] is True and [t_["tiles"] for t_ in plan["tiers"]] == [t_["tiles"] for t_ in report["tiers"]]
    assert plan["glyph_source"].endswith("/mirror")
