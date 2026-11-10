# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_map2d_regions.py (DV-Router MAP2d, the lazy basemap region worker).

The worker (`dv-svc-basemap serve`, a launcher-supervised child) looks at the public positions of the hub, picks the z10 cell
of each one the installed base does not cover, and extracts that cell's detail zooms from the configured source archive with
HEAD + ranged GETs, publishing `<state>/basemap/regions/r-10-<x>-<y>-<build>.pmtiles` and a new `regions/INDEX`. These helpers
read what that leaves behind: the INDEX, the request log of the fixture source (dvtest/basemap_fixture.py), the tiles that
dv-svc-web serves from a region, and the dv_basemap_* telemetry. No network except `source_stats` (the fixture's log listener).
"""
import fcntl
import gzip
import json
import os
import re
import stat
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


# ---------------------------------------------------------------- what the image reports

def image_reports_map2d(metrics_text=None, env=None):
    """(is MAP2d, why) for the image under test, decided from what it REPORTS, not from its name or a commit: the hub endpoint
    carries the dv_basemap_* series only when the image has the MAP2d worker (dv-svc-status ingests its telemetry kind, every 10 s).
    The explicit marker DVT_SUT_MAP2D (1 or 0) wins over the probe. A probe taken before the worker first published (about 10 s
    after its start) or without metrics gives False, the relaxed reading (`preparing` optional): never a false failure."""
    marker = (os.environ.get("DVT_SUT_MAP2D", "") if env is None else env).strip()
    if marker in ("0", "1"):
        return marker == "1", "marker DVT_SUT_MAP2D=%s" % marker
    if metrics_text is None:
        return False, "no metrics to probe and no DVT_SUT_MAP2D marker"
    found = basemap_metrics(metrics_text)
    if found:
        return True, "the hub reports %s" % ", ".join(sorted(found))
    return False, "the hub reports no dv_basemap_* series"


STATUS_KEYS = {"fetch_enabled", "preparing"}


def descriptor_relation(before, after):
    """(kind, problems) of two reads of tiles.json, each (etag, descriptor dict), the second later than the first.
    The ETag is a function of the descriptor body, and a status-only change (`fetch_enabled`, `preparing`: a worker started,
    a cell queued, the worker gone) moves the ETag but never the generation or the tile URLs:
      same            identical bodies: the ETag must be identical
      status-only     same gen, only STATUS_KEYS differ: the ETag must differ, `tiles` must be identical
      new-generation  another gen: the ETag must differ and `tiles` must follow the gen
      other-change    same gen but another key differs: always a problem"""
    (etag0, desc0), (etag1, desc1) = before, after
    problems = []
    if desc0 == desc1:
        if etag0 != etag1:
            problems.append("the ETag moved (%s -> %s) although the descriptor is identical" % (etag0, etag1))
        return "same", problems
    if etag0 == etag1:
        problems.append("the ETag %s did not move although the descriptor changed" % etag0)
    if desc0.get("gen") != desc1.get("gen"):
        want = ["/basemap/tiles/%s/{z}/{x}/{y}.mvt" % desc1.get("gen")]
        if desc1.get("tiles") != want:
            problems.append("tiles %r do not follow the new gen %s" % (desc1.get("tiles"), desc1.get("gen")))
        return "new-generation", problems
    changed = sorted(k for k in set(desc0) | set(desc1) if desc0.get(k) != desc1.get(k))
    if desc0.get("tiles") != desc1.get("tiles"):
        problems.append("the tile URLs changed without a new gen")
    if not set(changed) <= STATUS_KEYS:
        problems.append("keys %s changed under the same gen (only %s may)" % (changed, sorted(STATUS_KEYS)))
        return "other-change", problems
    return "status-only", problems


# ---------------------------------------------------------------- waiting for the telemetry

def wait_for_series(read, ready, timeout=25.0, poll=2.0, sleep=None, clock=None):
    """Polls `read()` (-> {series: value} or None/{} when the hub does not carry them) until `ready(series)` is true or `timeout`
    seconds pass. dv-svc-status ingests the worker's telemetry only every 10 s, so a counter read right after the event it counts
    still shows the old value. Returns (series, waited seconds, ready): the last read, how long it took, whether `ready` held. A
    hub that carries no dv_basemap_* series at all returns at once ({} or None is not going to change by waiting)."""
    import time
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    start = clock()
    while True:
        series = read()
        if not series:
            return series, clock() - start, False
        if ready(series):
            return series, clock() - start, True
        if clock() - start >= timeout:
            return series, clock() - start, False
        sleep(poll)


# ---------------------------------------------------------------- MAP2h: unavailability is a gauge, not a failure

UNAVAILABLE_REASONS = ("old_index", "writer_active", "no_install")      # the fixed reasons (status.md, deployment.md "Automatic detail regions")
INSTALL_LOCK = ".basemap-install.lock"                                  # <state>/, flock(2) LOCK_EX | LOCK_NB by the worker per cycle and by `install`
PRE_WORKER_INDEX_KEYS = ("fetch_enabled", "preparing", "too_large")     # the fields MAP2d added; an INDEX without them is "written before MAP2d"


def unavailable_snapshot(text):
    """{"reasons": {reason: value}, "failures": float|None, "fetches": float|None} from a Prometheus text, or {} when the image
    exports no dv_basemap_unavailable at all (an image before MAP2h). The three reason series are one-hot: exported always, at most
    one of them 1, all 0 while the worker is available."""
    from dvtest import metrics
    samples = metrics.parse(text)
    reasons = {s.labels.get("reason"): s.value for s in samples if s.name == "dv_basemap_unavailable"}
    if not reasons:
        return {}
    names = {s.name for s in samples}
    return {"reasons": reasons,
            "failures": metrics.total(samples, "dv_basemap_failures_total") if "dv_basemap_failures_total" in names else None,
            "fetches": metrics.total(samples, "dv_basemap_fetches_total") if "dv_basemap_fetches_total" in names else None}


def snapshot_problems(snap):
    """What is wrong with the SHAPE of the gauge: a missing or unknown reason, a value other than 0 or 1, two reasons at once."""
    reasons = (snap or {}).get("reasons") or {}
    out = []
    if set(reasons) != set(UNAVAILABLE_REASONS):
        out.append("reasons %s, want exactly %s" % (sorted(map(str, reasons)), sorted(UNAVAILABLE_REASONS)))
    if any(v not in (0.0, 1.0) for v in reasons.values()):
        out.append("values %s are not 0 or 1" % sorted(reasons.items(), key=str))
    if sum(1 for v in reasons.values() if v == 1.0) > 1:
        out.append("more than one reason is 1: %s" % sorted(r for r, v in reasons.items() if v == 1.0))
    return out


def reason_is(snap, reason):
    """True when the gauge is well formed and `reason` is the one series at 1; reason None means all three at 0 (available)."""
    if not snap or snapshot_problems(snap):
        return False
    active = sorted(r for r, v in snap["reasons"].items() if v == 1.0)
    return active == ([] if reason is None else [reason])


def old_index_bytes(index):
    """The regions/INDEX of an install that predates MAP2d: the given INDEX (dict, or bytes of a MAP2d INDEX) without the fields MAP2d
    added (`fetch_enabled`, `preparing`, `too_large`), version 1. The worker refuses it with `region INDEX was written before MAP2d`."""
    doc = json.loads(index) if isinstance(index, (bytes, str)) else dict(index)
    for key in PRE_WORKER_INDEX_KEYS:
        doc.pop(key, None)
    doc["version"] = 1
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("ascii")


def is_pre_worker_index(doc):
    return isinstance(doc, dict) and doc.get("version") == 1 and any(k not in doc for k in PRE_WORKER_INDEX_KEYS)


def replace_file(path, data):
    """Writes `data` to `path` through a temporary file in the same directory and a rename (the worker never reads half a file)."""
    path = os.fspath(path)
    tmp = path + ".dvt-tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class HeldLock:
    """Holds the install lock of a state directory like another writer would: flock(2) LOCK_EX | LOCK_NB on <state>/.basemap-install.lock
    (what dv-svc-basemap takes, publish.rs: BSD flock, not fcntl/lockf, which would not conflict). The file is created if the worker
    has not made it yet (mode 0666 minus umask, as the worker does), a symlink or a non-regular file is refused (the worker refuses it
    too, counting a failure). The lock lives as long as this object holds the descriptor: release() or leaving the `with` drops it,
    and so does a crash of the tester. BlockingIOError when someone else (the worker, mid cycle) holds it: try again."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / INSTALL_LOCK
        self.fd = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("%s is not a regular file" % self.path)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def acquire_retry(self, timeout=30.0, poll=0.2):
        """acquire(), retrying while the worker holds the lock for one of its short cycles."""
        import time
        end = time.monotonic() + timeout
        while True:
            try:
                return self.acquire()
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise
                time.sleep(poll)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @property
    def held(self):
        return self.fd is not None

    def __enter__(self):
        return self.acquire_retry()

    def __exit__(self, *exc):
        self.release()
