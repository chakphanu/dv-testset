# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_map2d_regions.py (DV-Router MAP2d, the lazy basemap region worker).

The worker (`dv-svc-basemap serve`, a launcher-supervised child) looks at the public positions of the hub, picks the z10 cell
of each one the installed base does not cover, and extracts that cell's detail zooms from the configured source archive with
HEAD + ranged GETs, publishing `<state>/basemap/regions/r-10-<x>-<y>-<build>.pmtiles` and a new `regions/INDEX`. These helpers
read what that leaves behind: the INDEX, the request log of the fixture source (dvtest/basemap_fixture.py), the tiles that
dv-svc-web serves from a region, and the dv_basemap_* telemetry. No network except `source_stats` (the fixture's log listener).
"""
import gzip
import json
import re
import urllib.request
from pathlib import Path

from dvtest import basemap_fixture as fx

# regions/INDEX of the MAP2d format (version 1 with the three worker fields); an INDEX without them is "written before MAP2d"
INDEX_KEYS = {"version", "generation", "regions", "day", "day_regions", "day_bytes", "backoff", "fetch_enabled", "preparing", "too_large"}
REGION_KEYS = {"cell", "archive", "build", "schema", "bytes", "sha256", "fetched_ms", "last_shown_ms", "state"}
ARCHIVE_RE = re.compile(r"^r-10-(\d+)-(\d+)-(\d{8})\.pmtiles$")
METRIC_NAMES = ("dv_basemap_regions", "dv_basemap_bytes", "dv_basemap_fetches_total", "dv_basemap_cap_hit_total",
                "dv_basemap_too_large_total", "dv_basemap_failures_total")


# ---------------------------------------------------------------- coverage

def detail_boxes(manifest):
    """[[west, south, east, north]] of the detail tiers of a MANIFEST v2."""
    return [list(t["bbox"]) for t in (manifest or {}).get("tiers", []) if t.get("role") == "detail"]


def cell_covered(cell, boxes):
    """True when the z10 cell lies wholly inside one of the detail boxes: what the worker's `covered_by_base` asks (tile-aligned at
    the tier's maxzoom; a cell that is only partly inside is NOT covered and may trigger a fetch). Dateline boxes are not handled
    (west > east never matches), the rig's boxes are in Thailand."""
    west, south, east, north = fx.cell_bbox(cell)
    return any(b[0] <= b[2] and b[0] <= west and east <= b[2] and b[1] <= south and north <= b[3] for b in boxes)


# ---------------------------------------------------------------- the INDEX

def read_index(basemap_dir):
    """The decoded <basemap_dir>/regions/INDEX (basemap_dir = <state>/basemap), or None when there is none or it is not a JSON
    object."""
    try:
        doc = json.loads((Path(basemap_dir) / "regions" / "INDEX").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def is_map2d_index(doc):
    """True for an INDEX the MAP2d worker accepts: version 1 with `fetch_enabled`, `preparing` and `too_large` (their absence is
    the pre-MAP2d shape that `verify` and the worker refuse with "re-run dv-svc-basemap install")."""
    return isinstance(doc, dict) and doc.get("version") == 1 and {"fetch_enabled", "preparing", "too_large"} <= set(doc)


def index_problems(doc):
    """Structural findings of a MAP2d INDEX (strings), [] when it is well formed."""
    if not isinstance(doc, dict):
        return ["INDEX is not a JSON object"]
    out = []
    missing, extra = INDEX_KEYS - set(doc), set(doc) - INDEX_KEYS
    if missing:
        out.append("INDEX lacks %s" % sorted(missing))
    if extra:
        out.append("INDEX has unknown keys %s" % sorted(extra))
    for key in ("generation", "day", "day_regions", "day_bytes"):
        if not isinstance(doc.get(key), int) or isinstance(doc.get(key), bool) or doc.get(key) < 0:
            out.append("%s is %r, want a non-negative integer" % (key, doc.get(key)))
    if not isinstance(doc.get("fetch_enabled"), bool):
        out.append("fetch_enabled is %r, want a boolean" % (doc.get("fetch_enabled"),))
    for key in ("regions", "backoff", "preparing", "too_large"):
        if not isinstance(doc.get(key), list) or len(doc.get(key, [])) > 512:
            out.append("%s is not a list of at most 512 entries" % key)
    for region in doc.get("regions", []) if isinstance(doc.get("regions"), list) else []:
        if not isinstance(region, dict) or set(region) != REGION_KEYS:
            out.append("region entry %r" % (region,))
            continue
        m = ARCHIVE_RE.match(str(region["archive"]))
        if not m or "10/%s/%s" % (m.group(1), m.group(2)) != region["cell"] or m.group(3) != region["build"]:
            out.append("region archive %r does not name cell %r build %r" % (region["archive"], region["cell"], region["build"]))
        if region["state"] != "ready":
            out.append("region %s is %r, want ready" % (region["cell"], region["state"]))
    return out[:8]


def region_cells(doc):
    """The cell names of the published regions, sorted."""
    return sorted(r["cell"] for r in (doc or {}).get("regions", []) if isinstance(r, dict) and "cell" in r)


# ---------------------------------------------------------------- the source's request log

def source_stats(url, timeout=10.0):
    """GET <url>/requests of the fixture's stats listener -> (rows, summary). `url` is the DVT_MAPSRC_STATS base."""
    with urllib.request.urlopen(url.rstrip("/") + "/requests", timeout=timeout) as resp:
        doc = json.loads(resp.read())
    return doc["requests"], doc["summary"]


def jobs(summary):
    """How many lazy-region jobs the worker started: a job begins with its builds.json GET and pins the archive with one HEAD."""
    return min(summary["builds"], summary["head"])


def job_problems(summary, expected_jobs):
    """What is wrong with the worker's traffic after `expected_jobs` jobs: wrong job count, a whole-archive GET (the worker only
    range-reads), a range answered otherwise than 206 or sent without If-Match, a glyph request (the worker has no glyph use)."""
    out = []
    if summary["builds"] != expected_jobs or summary["head"] != expected_jobs:
        out.append("%d builds.json GET and %d archive HEAD, want %d of each (one job each)" % (summary["builds"], summary["head"], expected_jobs))
    if expected_jobs and summary["range_get"] < 2:
        out.append("%d ranged GET, want at least 2 (header and directory, then the payload)" % summary["range_get"])
    if summary["full_get"]:
        out.append("%d GET of the whole archive without Range" % summary["full_get"])
    if summary["bad_ranges"]:
        out.append("%d ranged GET not answered 206 or sent without If-Match" % summary["bad_ranges"])
    if summary["mirror"] or summary["other"]:
        out.append("%d glyph and %d other request(s) from the worker" % (summary["mirror"], summary["other"]))
    return out


# ---------------------------------------------------------------- tiles served from a region

def tile_for(lat, lon, zoom):
    """(zoom, x, y) of the tile containing a coordinate."""
    x, y = fx.lonlat_tile(lon, lat, zoom)
    return zoom, x, y


def tile_path(gen, z, x, y):
    return "/basemap/tiles/%s/%d/%d/%d.mvt" % (gen, z, x, y)


def _fields(data):
    out, pos = [], 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos:pos + size], pos + size
        else:
            raise ValueError("unexpected protobuf wire type %d" % wire)
        out.append((field, value))
    return out


def _varint(data, pos):
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def tile_marker(body):
    """The `z/x/y` string the fixture writes into every tile (layer `earth`, value 1), read from a gzip MVT body; None when the
    body is not such a tile. Lets a case tell that the tile it got is the tile of the coordinates it asked for."""
    try:
        layers = [v for f, v in _fields(gzip.decompress(body)) if f == 3]
        layer = _fields(layers[0])
        if (1, b"earth") not in layer:
            return None
        values = [_fields(v)[0][1] for f, v in layer if f == 4]
        return values[1].decode("ascii")
    except (OSError, ValueError, IndexError, EOFError):
        return None


# ---------------------------------------------------------------- telemetry

def basemap_metrics(text):
    """{series name: value} of the dv_basemap_* series of a Prometheus text (unlabelled series; absent ones are left out, so an
    exporter that does not carry them is told from one that reports zero)."""
    from dvtest import metrics
    samples = metrics.parse(text)
    present = {s.name for s in samples}
    return {name: metrics.total(samples, name) for name in METRIC_NAMES if name in present}
