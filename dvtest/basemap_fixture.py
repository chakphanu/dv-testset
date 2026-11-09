# SPDX-License-Identifier: MIT
"""Loopback fixture for `dv-svc-basemap install` (DV-Router MAP2g): a real installed basemap tree with no internet.

The product tool fetches three kinds of things over HTTP, each from its own operator override (MAP2g):

  --source-index URL/builds.json     a JSON list of dated builds: [{"key": "YYYYMMDD.pmtiles", "size": N, "version": "4.x.y"}]
  --source-base  URL/archives        the PMTiles v3 archive named by the chosen key: HEAD (Content-Length, strong ETag), then
                                     ranged GETs with If-Match, each answered 206 with an exact Content-Range
  --glyph-base   URL/mirror          every glyph / licence / font file of the catalog, GET <base>/<catalog path>, status 200
  --glyph-catalog FILE               a JSON array (the shape of crates/svc/basemap/assets/glyphs.json): path, bytes, sha256 and
                                     exactly one pinned source per file

This module builds all of them deterministically (the same bytes on every run and host), serves them from a threaded
http.server on 127.0.0.1, and drives the install + verify of a given dv-svc-basemap binary into a state directory. The result
is the tree cases/test_dp7_positions_page.py reads through DVT_BASEMAP_FILE (docs/dp7-cases.md "The basemap tree").

What it is NOT: a production font, a visual oracle or a map. The tiles are gzip MVT with one square `earth` polygon each, the
glyph ranges are complete but EMPTY BMP blocks, the one font file is a 32-byte sfnt envelope with an empty cmap. It satisfies the
installer's structural checks (PMTiles v3 header and directories, gzip MVT, protobuf envelope, sfnt table directory, face map
rules of positions-map.js) and lets dv-svc-web serve tiles (200 present, 204 absent), glyph ranges and the face map.

The encoders are written from the specifications (PMTiles v3, Mapbox vector tile and glyph protobuf, OpenType sfnt, Wikipedia
xy2d); they do not import or copy any DV-Router code. Python standard library only.

    python3 -m dvtest.basemap_fixture install --bin PATH/dv-svc-basemap --out DIR     (scripts/basemap-install-fixture.sh)
    python3 -m dvtest.basemap_fixture serve [--port N] [--catalog-out FILE] [--stats-bind HOST:PORT] [--access-log]
    python3 -m dvtest.basemap_fixture info
"""
import argparse
import hashlib
import http.server
import json
import math
import os
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- constants

BUILD_DATE = "20261107"                     # the dated build the index lists and `--build` selects
ARCHIVE_KEY = BUILD_DATE + ".pmtiles"
SCHEMA_VERSION = "4.15.2"                   # source schema; the installer accepts major 4 only
FIXTURE_LON, FIXTURE_LAT = 100.5, 13.75     # the base point: inside DEFAULT_DETAIL, so the installed detail tier holds its tiles
# Further points whose chain of tiles (zoom 0..MAX_ZOOM) is in the archive, outside DEFAULT_DETAIL: a lazy region worker
# (MAP2d) asked for the z10 cell of one of them finds tiles to extract. name -> (lon, lat). Used by cases/test_map2d_regions.py.
REGION_POINTS = {"cnx": (98.98, 18.79), "hkt": (98.39, 7.88)}
CELL_ZOOM = 10                              # the lazy worker's cell grid ("10/<x>/<y>")
MAX_ZOOM = 14                               # covers the router profile (detail z7-14); hotspot needs 12
DEFAULT_PROFILE = "hotspot"
DEFAULT_DETAIL = ("dvt", "99.9,13.0,101.1,14.1")   # the box docs/dp7-cases.md has always used; holds FIXTURE_LON/LAT

NOTICE = b"Fixture font licence.\n"
FONT_PATH = "glyphs/fonts/Fixture.ttf"
FACES_PATH = "glyphs/FONT-FACES.json"
FACES_JSON = (b'{"NotoSansRegular":[{"url":"' + FONT_PATH.encode("ascii") +
              b'","unicode-range":["U+0-10FFFF"]}]}')
SOURCE_URL_BASE = "https://example.org/"    # valid (HTTPS) URLs for the catalog; never fetched when --glyph-base is given
MIRROR_PREFIX = "/mirror/"
ARCHIVE_PREFIX = "/archives/"
INDEX_PATH = "/builds.json"
PLAN_NAME = "install-plan.json"             # the --dry-run report, saved in the state directory beside basemap/


# ---------------------------------------------------------------- small encoders

def varint(value):
    """Protobuf / PMTiles unsigned LEB128."""
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def zigzag(value):
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


def gzip_stored(data):
    """A gzip member made of stored (uncompressed) deflate blocks: valid for any decoder and identical on every host (a real
    deflate encoder's output depends on the zlib build). Header mtime 0, OS 255."""
    out = bytearray(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff")
    chunks = [data[i:i + 65535] for i in range(0, len(data), 65535)] or [b""]
    for index, chunk in enumerate(chunks):
        final = 1 if index == len(chunks) - 1 else 0
        out += bytes([final]) + struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF) + chunk
    out += struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return bytes(out)


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- glyph assets and catalog

def _pbf_range_name(start):
    return "%d-%d" % (start, start + 255)


def glyph_pbf(start):
    """The Mapbox glyphs.proto envelope of one BMP block with NO glyphs: Glyphs{ stacks(1): Fontstack{ name(1), range(2) } }.
    26 bytes for 0-255; every length stays below one varint byte."""
    name = b"NotoSansRegular"
    rng = _pbf_range_name(start).encode("ascii")
    stack = b"\x0a" + bytes([len(name)]) + name + b"\x12" + bytes([len(rng)]) + rng
    return b"\x0a" + bytes([len(stack)]) + stack


def sfnt_font():
    """A 32-byte TrueType envelope: offset table (version 1.0, one table, searchRange 16, entrySelector 0, rangeShift 0), one
    directory record `cmap` (checksum 0 = the sum of an all-zero table, offset 28, length 4), then the 4-byte empty cmap
    (version 0, zero subtables)."""
    out = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    out += b"cmap" + struct.pack(">III", 0, 28, 4)
    out += struct.pack(">HH", 0, 0)
    return out


def glyph_assets():
    """[(path, bytes)] in catalog order: 256 BMP ranges, the two notices, the face map, the font. 260 files."""
    assets = [("glyphs/NotoSansRegular/%s.pbf" % _pbf_range_name(s), glyph_pbf(s)) for s in range(0, 65536, 256)]
    assets.append(("glyphs/OFL.txt", NOTICE))
    assets.append(("glyphs/fonts/OFL.txt", NOTICE))
    assets.append((FACES_PATH, FACES_JSON))
    assets.append((FONT_PATH, sfnt_font()))
    return assets


def catalog_entries(assets=None, url_base=SOURCE_URL_BASE):
    """The --glyph-catalog document as a list of dicts: one pinned passthrough source per file (sources [] is only for the
    built-in compiled face map, which a fixture cannot use)."""
    out = []
    for path, data in (assets if assets is not None else glyph_assets()):
        digest = sha256_hex(data)
        out.append({"path": path, "bytes": len(data), "sha256": digest,
                    "sources": [{"url": url_base + path, "bytes": len(data), "sha256": digest}]})
    return out


def catalog_bytes(assets=None, url_base=SOURCE_URL_BASE):
    """Compact ASCII JSON of catalog_entries (the installer wants bounded ASCII, at most 512 KiB and 508 entries)."""
    return json.dumps(catalog_entries(assets, url_base), separators=(",", ":"), ensure_ascii=True).encode("ascii")


# ---------------------------------------------------------------- PMTiles v3 archive

def tile_id(z, x, y):
    """PMTiles tile id: the cumulative count of all tiles of lower zooms plus the Hilbert distance (Wikipedia xy2d)."""
    width = 1 << z
    if not (0 <= x < width and 0 <= y < width):
        raise ValueError("tile %d/%d/%d is outside its zoom" % (z, x, y))
    distance = 0
    scale = width // 2
    while scale:
        rx = 1 if x & scale else 0
        ry = 1 if y & scale else 0
        distance += scale * scale * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = width - 1 - x, width - 1 - y
            x, y = y, x
        scale //= 2
    return ((1 << (2 * z)) - 1) // 3 + distance


def tile_coords(tid):
    """Inverse of tile_id: (z, x, y)."""
    z, offset = 0, 0
    while tid - offset >= (1 << z) * (1 << z):
        offset += (1 << z) * (1 << z)
        z += 1
    distance = tid - offset
    width = 1 << z
    x = y = 0
    scale = 1
    while scale < width:
        rx = 1 & (distance // 2)
        ry = 1 & (distance ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = scale - 1 - x, scale - 1 - y
            x, y = y, x
        x += scale * rx
        y += scale * ry
        distance //= 4
        scale *= 2
    return z, x, y


def lonlat_tile(lon, lat, z):
    """The slippy-map tile (x, y) containing (lon, lat) at zoom z (Web Mercator)."""
    n = 1 << z
    lat = max(-85.0511287798066, min(85.0511287798066, lat))
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def cell_of(lat, lon, zoom=CELL_ZOOM):
    """The lazy-worker cell name `<zoom>/<x>/<y>` of a coordinate (the slippy tile of the z10 grid that contains it)."""
    x, y = lonlat_tile(lon, lat, zoom)
    return "%d/%d/%d" % (zoom, x, y)


def cell_bbox(cell):
    """[west, south, east, north] of a cell name: the whole tile, edges included (how the worker plans its extract)."""
    z, x, y = (int(v) for v in cell.split("/"))
    n = 1 << z

    def lat(row):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    return [x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)]


def tile_list():
    """The (z, x, y) of every tile in the archive, sorted by tile id: one tile per zoom 0..MAX_ZOOM containing the base point
    (FIXTURE_LON, FIXTURE_LAT) and each of REGION_POINTS, plus the other three tiles of zoom 1 (so the world overview is not a
    single square)."""
    points = [(FIXTURE_LON, FIXTURE_LAT)] + list(REGION_POINTS.values())
    tiles = {(z,) + lonlat_tile(lon, lat, z) for lon, lat in points for z in range(MAX_ZOOM + 1)}
    tiles |= {(1, x, y) for x in (0, 1) for y in (0, 1)}
    return sorted(tiles, key=lambda t: tile_id(*t))


def _pb_len(field, payload):
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def mvt_tile(z, x, y):
    """One gzip MVT tile: layer `earth` (version 2, extent 4096) with a single POLYGON feature covering the whole tile, tags
    kind=land and tile=<z/x/y> (so every tile has different bytes and a test can tell them apart)."""
    geometry = (varint(9) + varint(zigzag(0)) + varint(zigzag(0)) +                     # MoveTo(1) 0,0
                varint(26) + varint(zigzag(4096)) + varint(zigzag(0)) +                 # LineTo(3) +4096,0
                varint(zigzag(0)) + varint(zigzag(4096)) + varint(zigzag(-4096)) + varint(zigzag(0)) +
                varint(15))                                                             # ClosePath
    feature = (varint(1 << 3) + varint(1) +                                             # id = 1
               _pb_len(2, bytes([0, 0, 1, 1])) +                                        # tags: key 0 -> value 0, key 1 -> value 1
               varint(3 << 3) + varint(3) +                                             # type = POLYGON
               _pb_len(4, geometry))
    layer = (_pb_len(1, b"earth") + _pb_len(2, feature) +
             _pb_len(3, b"kind") + _pb_len(3, b"tile") +
             _pb_len(4, _pb_len(1, b"land")) + _pb_len(4, _pb_len(1, ("%d/%d/%d" % (z, x, y)).encode("ascii"))) +
             varint(5 << 3) + varint(4096) + varint(15 << 3) + varint(2))
    return gzip_stored(_pb_len(3, layer))


def archive_metadata():
    """TileJSON metadata the installer rewrites per tier: it requires a `vector_layers` array."""
    layers = [{"id": name, "fields": {"kind": "String", "tile": "String"}, "minzoom": 0, "maxzoom": MAX_ZOOM}
              for name in ("earth",)]
    doc = {"name": "dv-testset basemap fixture", "version": SCHEMA_VERSION, "type": "baselayer",
           "description": "Synthetic tiles for tests; not a map.", "attribution": "dv-testset fixture",
           "minzoom": 0, "maxzoom": MAX_ZOOM, "vector_layers": layers}
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("ascii")


def _directory(entries):
    """PMTiles directory (uncompressed): count, then columns tile-id deltas, run lengths, lengths, offsets (0 = contiguous with
    the previous entry, else offset + 1). `entries` = [(tile_id, offset, length)] in id order, run length 1 each."""
    out = bytearray(varint(len(entries)))
    previous = 0
    for tid, _, _ in entries:
        out += varint(tid - previous)
        previous = tid
    for _ in entries:
        out += varint(1)
    for _, _, length in entries:
        out += varint(length)
    expected = None
    for _, offset, length in entries:
        out += varint(0 if offset == expected else offset + 1)
        expected = offset + length
    return bytes(out)


def build_pmtiles():
    """The whole source archive (PMTiles v3, clustered, internal and tile compression gzip, tile type MVT, zooms 0..MAX_ZOOM)."""
    tiles = [(tile_id(*t), mvt_tile(*t)) for t in tile_list()]
    entries, offset = [], 0
    for tid, data in tiles:
        entries.append((tid, offset, len(data)))
        offset += len(data)
    root = gzip_stored(_directory(entries))
    metadata = gzip_stored(archive_metadata())
    payload = b"".join(data for _, data in tiles)
    root_range = (127, len(root))
    meta_range = (127 + len(root), len(metadata))
    leaf_range = (meta_range[0] + meta_range[1], 0)
    tile_range = (leaf_range[0], len(payload))
    if root_range[0] + root_range[1] > 16384:
        raise ValueError("root directory exceeds the first 16 KiB")
    header = b"PMTiles\x03"
    for start, length in (root_range, meta_range, leaf_range, tile_range):
        header += struct.pack("<QQ", start, length)
    header += struct.pack("<QQQ", len(tiles), len(tiles), len(tiles))      # addressed, entries, contents
    header += bytes([1, 2, 2, 1, 0, MAX_ZOOM])                              # clustered, gzip, gzip, MVT, min/max zoom
    header += struct.pack("<iiii", -1800000000, -850511287, 1800000000, 850511287)
    header += struct.pack("<Bii", 6, int(FIXTURE_LON * 1e7), int(FIXTURE_LAT * 1e7))
    if len(header) != 127:
        raise AssertionError("PMTiles header is %d bytes" % len(header))
    return header + root + metadata + payload


def builds_json(size, key=ARCHIVE_KEY, version=SCHEMA_VERSION):
    return json.dumps([{"key": key, "size": size, "version": version}], separators=(",", ":")).encode("ascii")


def strong_etag(archive):
    """A strong validator: a quoted opaque tag derived from the bytes, no W/ prefix."""
    return '"%s"' % sha256_hex(archive)[:32]


# ---------------------------------------------------------------- the HTTP server

def parse_range(value, size):
    """RFC 9110 single byte range -> ("ok", first, last) | ("unsatisfiable",) | ("ignore",).
    Syntax the server does not understand (another unit, several ranges, first > last) is ignored (full 200), as the RFC says;
    a well-formed range that starts at or beyond the end is unsatisfiable (416)."""
    if value is None or not value.startswith("bytes="):
        return ("ignore",)
    spec = value[len("bytes="):].strip()
    if "," in spec or "-" not in spec:
        return ("ignore",)
    first_s, last_s = spec.split("-", 1)
    try:
        if first_s == "":                       # suffix: the last N bytes
            count = int(last_s)
            if count <= 0:
                return ("unsatisfiable",)
            return ("ok", max(0, size - count), size - 1)
        first = int(first_s)
        last = int(last_s) if last_s != "" else size - 1
    except ValueError:
        return ("ignore",)
    if first < 0 or last < first:
        return ("ignore",)
    if first >= size:
        return ("unsatisfiable",)
    return ("ok", first, min(last, size - 1))


def if_match_ok(header, etag):
    """If-Match precondition with strong comparison: absent or `*` or a list that contains the current tag."""
    if header is None:
        return True
    tags = [t.strip() for t in header.split(",")]
    return "*" in tags or etag in tags


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dvt-basemap-fixture"

    def log_message(self, format, *args):       # noqa: A002 - signature of the base class
        return

    def _send(self, status, headers, body=b"", head=False, length=None):
        """`length` overrides Content-Length (HEAD states the size of the object it does not send)."""
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.send_header("Connection", "close")
        self.end_headers()
        if not head and body:
            self.wfile.write(body)
        self.close_connection = True
        self.server.fixture._record(self.command, self.path, self.headers.get("Range"), self.headers.get("If-Match"), status)

    def _handle(self, head):
        fixture = self.server.fixture
        route = self.path.split("?", 1)[0]
        if route == INDEX_PATH:
            body = fixture.index
            return self._send(200, [("Content-Type", "application/json")], body, head)
        if route == ARCHIVE_PREFIX + ARCHIVE_KEY:
            return self._archive(head)
        if route.startswith(MIRROR_PREFIX):
            data = fixture.assets.get(route[len(MIRROR_PREFIX):])
            if data is not None:
                return self._send(200, [("Content-Type", "application/octet-stream")], data, head)
        return self._send(404, [], b"", head)

    def _archive(self, head):
        fixture = self.server.fixture
        archive, etag, size = fixture.archive, fixture.etag, len(fixture.archive)
        base = [("ETag", etag), ("Accept-Ranges", "bytes"), ("Content-Type", "application/octet-stream")]
        if not if_match_ok(self.headers.get("If-Match"), etag):
            return self._send(412, [("ETag", etag)], b"", head)
        if head:
            # HEAD: the size of the whole object and the strong tag; no body
            return self._send(200, base, b"", True, length=size)
        parsed = parse_range(self.headers.get("Range"), size)
        if parsed[0] == "ignore":
            return self._send(200, base, archive, False)
        if parsed[0] == "unsatisfiable":
            return self._send(416, [("Content-Range", "bytes */%d" % size)], b"", False)
        _, first, last = parsed
        return self._send(206, base + [("Content-Range", "bytes %d-%d/%d" % (first, last, size))], archive[first:last + 1], False)

    def do_GET(self):
        self._handle(False)

    def do_HEAD(self):
        self._handle(True)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def summarize(rows):
    """Counts of what a source client did, from request rows (the `requests` of BasemapFixture): one lazy-region job is one
    builds.json GET plus one archive HEAD, then ranged GETs; a client that never asked for a job leaves all of them at zero.
    `bad_ranges` counts archive GETs that are not a 206 answer to a Range request carrying If-Match."""
    archive = ARCHIVE_PREFIX + ARCHIVE_KEY
    out = {"total": len(rows), "builds": 0, "head": 0, "range_get": 0, "full_get": 0, "mirror": 0, "other": 0,
           "bad_ranges": 0, "statuses": {}}
    for row in rows:
        path, method = row["path"], row["method"]
        out["statuses"][str(row["status"])] = out["statuses"].get(str(row["status"]), 0) + 1
        if path == INDEX_PATH and method == "GET":
            out["builds"] += 1
        elif path == archive and method == "HEAD":
            out["head"] += 1
        elif path == archive and method == "GET" and row["range"]:
            out["range_get"] += 1
            if row["status"] != 206 or not row["if_match"]:
                out["bad_ranges"] += 1
        elif path == archive and method == "GET":
            out["full_get"] += 1
        elif path.startswith(MIRROR_PREFIX):
            out["mirror"] += 1
        else:
            out["other"] += 1
    return out


class _StatsHandler(http.server.BaseHTTPRequestHandler):
    """GET /requests -> {"requests": [...], "summary": {...}}; GET /health -> ok. Read only; nothing else is served."""
    protocol_version = "HTTP/1.1"
    server_version = "dvt-basemap-fixture-stats"

    def log_message(self, format, *args):       # noqa: A002
        return

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route == "/requests":
            rows, summary = self.server.fixture.snapshot()
            body, status = json.dumps({"requests": rows, "summary": summary}, separators=(",", ":")).encode("ascii"), 200
        elif route == "/health":
            body, status = b"ok\n", 200
        else:
            body, status = b"", 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if route == "/requests" else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class BasemapFixture:
    """The fixture bytes plus a started-on-demand loopback server. `with BasemapFixture() as fx:` then fx.url (no trailing
    slash) and fx.index_url / fx.source_base / fx.glyph_base for the four overrides."""

    def __init__(self, host="127.0.0.1", port=0, stats_bind=None, access_log=None):
        """`stats_bind` = (host, port) opens a second listener that serves only the request log (GET /requests): for a tester
        that cannot reach the loopback-only source listener. `access_log` is a text stream that gets one line per request."""
        self.assets = dict(glyph_assets())
        self.archive = build_pmtiles()
        self.etag = strong_etag(self.archive)
        self.index = builds_json(len(self.archive))
        self.requests = []
        self._lock = threading.Lock()
        self._host, self._port = host, port
        self._stats_bind = stats_bind
        self.access_log = access_log
        self._server = None
        self._thread = None
        self._stats = None
        self._stats_thread = None

    def _record(self, method, path, range_header, if_match, status):
        row = {"method": method, "path": path.split("?", 1)[0], "range": range_header, "if_match": if_match, "status": status}
        with self._lock:
            row["n"] = len(self.requests) + 1
            self.requests.append(row)
        if self.access_log is not None:
            self.access_log.write("%d %s %s range=%s if-match=%s -> %d\n" % (
                row["n"], method, row["path"], range_header or "-", if_match or "-", status))
            self.access_log.flush()

    def snapshot(self):
        """(request rows so far, summary) under the lock: the document the stats listener serves."""
        with self._lock:
            rows = list(self.requests)
        return rows, summarize(rows)

    def start(self):
        self._server = _Server((self._host, self._port), _Handler)
        self._server.fixture = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        if self._stats_bind is not None:
            self._stats = _Server(tuple(self._stats_bind), _StatsHandler)
            self._stats.fixture = self
            self._stats_thread = threading.Thread(target=self._stats.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            self._stats_thread.start()
        return self

    def stop(self):
        if self._stats is not None:
            self._stats.shutdown()
            self._stats.server_close()
            self._stats_thread.join(5.0)
            self._stats = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(5.0)
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    @property
    def index_url(self):
        return self.url + INDEX_PATH

    @property
    def source_base(self):
        return self.url + ARCHIVE_PREFIX.rstrip("/")

    @property
    def glyph_base(self):
        return self.url + MIRROR_PREFIX.rstrip("/")

    def install_args(self, state, catalog_path, profile=DEFAULT_PROFILE, details=(DEFAULT_DETAIL,), build=BUILD_DATE):
        """argv words after the binary for `install`: the four overrides of MAP2g (docs: map2g-for-dvt)."""
        args = ["install", "--state", str(state), "--build", build, "--profile", profile]
        for name, box in details:
            args += ["--detail", "%s=%s" % (name, box)]
        args += ["--source-index", self.index_url, "--source-base", self.source_base,
                 "--glyph-base", self.glyph_base, "--glyph-catalog", str(catalog_path)]
        return args


# ---------------------------------------------------------------- driving the product tool

class InstallError(RuntimeError):
    pass


def _run(argv, timeout):
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise InstallError("%s did not finish in %.0f s" % (argv[1], timeout)) from exc
    out = done.stdout.decode("utf-8", "replace")
    err = done.stderr.decode("utf-8", "replace")
    if done.returncode != 0:
        raise InstallError("%s exited %d: %s" % (argv[1], done.returncode, (err.strip() or out.strip())[-400:]))
    return out, err


def install_tree(binary, out, profile=DEFAULT_PROFILE, details=(DEFAULT_DETAIL,), build=BUILD_DATE, timeout=600.0):
    """Runs `<binary> install --dry-run` (the plan is saved as <out>/install-plan.json), then `install` against the fixture server
    into the state directory `out`, then `verify`. Returns the install
    report (the JSON the tool prints). `out` must not already hold a `basemap/` (nothing is overwritten)."""
    out = Path(out)
    if (out / "basemap").exists():
        raise InstallError("%s already holds a basemap/: not overwritten" % out)
    binary = str(binary)
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        raise InstallError("%s is not an executable file" % binary)
    out.mkdir(parents=True, exist_ok=True)
    with BasemapFixture() as fixture, tempfile.TemporaryDirectory(prefix="dvt-basemap-catalog-") as tmp:
        catalog = Path(tmp) / "CATALOG.json"
        catalog.write_bytes(catalog_bytes())
        argv = [binary] + fixture.install_args(out, catalog, profile, details, build)
        # --dry-run plans every tier against the fixture and writes nothing; keep its report next to the tree
        plan_out, _ = _run(argv + ["--dry-run"], timeout)
        try:
            plan = json.loads(plan_out)
        except ValueError as exc:
            raise InstallError("dry run printed no JSON plan: %r" % plan_out[:200]) from exc
        (out / PLAN_NAME).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="ascii")
        stdout, _ = _run(argv, timeout)
        _run([binary, "verify", "--state", str(out)], timeout)
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise InstallError("install printed no JSON report: %r" % stdout[:200]) from exc


# ---------------------------------------------------------------- command line

def _path_for_env(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _cmd_install(args):
    details = [tuple(d.split("=", 1)) for d in args.detail] or [DEFAULT_DETAIL]
    try:
        report = install_tree(args.bin, args.out, args.profile, details, args.build, args.timeout)
    except InstallError as exc:
        print("basemap_fixture: %s" % exc, file=sys.stderr)
        return 1
    tiers = ", ".join("%s z%d-%d %d tiles" % (t["name"], t["minzoom"], t["maxzoom"], t["tiles"]) for t in report.get("tiers", []))
    print("basemap_fixture: installed and verified build %s (%s); glyph catalog %s" % (
        report.get("build"), tiers, json.dumps(report.get("glyph_catalog"))), file=sys.stderr)
    shown = _path_for_env(args.out)
    if os.path.isabs(shown):
        print("basemap_fixture: note: %s is outside the repository; the tester container only sees paths under it" % shown, file=sys.stderr)
    print("DVT_BASEMAP_FILE=%s" % shown)
    return 0


def _hostport(text):
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("%r is not HOST:PORT" % text)
    return host, int(port)


def _cmd_serve(args):
    log = sys.stdout if args.access_log else None
    with BasemapFixture(host=args.host, port=args.port, stats_bind=args.stats_bind, access_log=log) as fixture:
        if args.catalog_out:
            Path(args.catalog_out).write_bytes(catalog_bytes())
        print("source-index %s\nsource-base  %s\nglyph-base   %s" % (fixture.index_url, fixture.source_base, fixture.glyph_base))
        if args.catalog_out:
            print("glyph-catalog %s" % args.catalog_out)
        if args.stats_bind:
            print("stats        http://%s:%d/requests" % args.stats_bind)
        sys.stdout.flush()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return 0


def _cmd_info(_args):
    archive = build_pmtiles()
    print("archive   %d bytes sha256 %s etag %s" % (len(archive), sha256_hex(archive), strong_etag(archive)))
    print("tiles     %d (zoom 0..%d), build %s, schema %s" % (len(tile_list()), MAX_ZOOM, BUILD_DATE, SCHEMA_VERSION))
    entries = catalog_entries()
    print("catalog   %d entries, %d bytes, sha256 %s" % (len(entries), len(catalog_bytes()), sha256_hex(catalog_bytes())))
    print("glyph set %d bytes" % sum(e["bytes"] for e in entries))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m dvtest.basemap_fixture", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    inst = sub.add_parser("install", help="run dv-svc-basemap install + verify against the loopback fixture")
    inst.add_argument("--bin", required=True, help="the dv-svc-basemap binary (needs --glyph-base: DV-Router MAP2g or later)")
    inst.add_argument("--out", required=True, help="the --state directory to create (must not hold a basemap/ yet)")
    inst.add_argument("--profile", default=DEFAULT_PROFILE, choices=("hotspot", "router"))
    inst.add_argument("--detail", action="append", default=[], metavar="NAME=W,S,E,N",
                      help="a detail tier (repeatable); default %s=%s" % DEFAULT_DETAIL)
    inst.add_argument("--build", default=BUILD_DATE)
    inst.add_argument("--timeout", type=float, default=600.0)
    inst.set_defaults(func=_cmd_install)
    srv = sub.add_parser("serve", help="serve the fixture until interrupted (prints the three URLs)")
    srv.add_argument("--host", default="127.0.0.1", help="source listener address; dv-svc-basemap accepts http only for literal 127.0.0.1 / [::1]")
    srv.add_argument("--port", type=int, default=0)
    srv.add_argument("--stats-bind", type=_hostport, metavar="HOST:PORT",
                     help="second listener that serves only the request log (GET /requests), for a tester outside this network namespace")
    srv.add_argument("--access-log", action="store_true", help="print one line per request on stdout")
    srv.add_argument("--catalog-out", help="also write the glyph catalog to this file")
    srv.set_defaults(func=_cmd_serve)
    info = sub.add_parser("info", help="print sizes and digests of the generated bytes")
    info.set_defaults(func=_cmd_info)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
