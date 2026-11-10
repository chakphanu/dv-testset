# SPDX-License-Identifier: MIT
"""Helpers of the second set of DP7 position cases (cases/test_dp15_positions2.py, cases/test_dp7_positions_page.py,
the ETag cases of cases/test_web_etag_diag.py). Offline pieces only; the unit tests are cases/unit/test_position2_support.py.

  IgateBudget       the flat IS-to-RF release limit of the worker (6 per 60 s and 10 per 300 s per RF port,
                    crates/data/worker/src/core_rf_state.rs igate_rate): how long a case waits before it may expect
                    `n` more frames on the air
  third_party       the decoded third-party frame DV-Router transmits for a gated IS packet:
                    `GWCALL>APZDVR[,path]:}SRC>DST,TCPIP,GWCALL*:info` (data.md 9.3, igate.rs third_party_info)
  item_info         the APRS ITEM report `)NAME!<position>` / `)NAME_<position>` (APRS101 ch. 11; object.rs: name 3..9
                    characters, `!` live, `_` killed, no timestamp)
  cursor_of / ...   the SSE ids `<boot>-<ordinal>` for a replay request (openapi LastEventId)
  page_rows / ...   what the #/positions page shows for a public state document (web/site/positions.js positionRow
                    validity, port of the checks) and the text of one table row
  basemap_*         the optional self-hosted basemap (MAP1): PMTiles header bounds, the MANIFEST the page checks,
                    staging a provided file into <state>/basemap/ (the build tool itself needs the internet)
  decode_png / blobs   a stdlib PNG decoder and a connected-component counter of one colour, to count the marker
                    circles of the map canvas (markers are drawn by WebGL, they are NOT DOM elements)
  OriginAudit       the set of origins a Playwright context requested (foreign origins are aborted)

Everything marked UNVERIFIED in docs/dp7-cases.md ("DP7 second set") was written from the DV-Router sources, not from a
running image.
"""
import hashlib
import json
import os
import re
import shutil
import struct
import tarfile
import time
import zlib
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

# ---------------------------------------------------------------- IS-to-RF release budget

class IgateBudget:
    """Flat per-port limit of the worker for everything it releases from APRS-IS to RF (gated messages and courtesy
    positions): at most 6 in 60 s and 10 in 300 s (igate_rate). A case that expects `n` frames asks `wait(n)` first so that
    the worker cannot refuse a frame with the `rate` result. `clock` and `sleep` are injectable."""

    PER_MIN, PER_5MIN = 6, 10

    def __init__(self, clock=time.monotonic, sleep=time.sleep, margin=2.0):
        self.clock, self.sleep, self.margin = clock, sleep, margin
        self.releases = []

    def wait_seconds(self, n=1, now=None):
        """Seconds to wait before `n` more releases fit both windows (0 when they fit now)."""
        now = self.clock() if now is None else now
        if n > self.PER_MIN:
            raise ValueError("more than %d releases never fit in one minute" % self.PER_MIN)
        need = 0.0
        for window, limit in ((60.0, self.PER_MIN), (300.0, self.PER_5MIN)):
            live = sorted(t for t in self.releases if now - t < window)
            over = len(live) + n - limit
            if over > 0:
                # the oldest `over` releases must leave the window
                need = max(need, live[over - 1] + window - now)
        return max(0.0, need + (self.margin if need > 0 else 0.0))

    def wait(self, n=1):
        """Sleeps until `n` releases fit, then books them (the offer). Returns the seconds slept."""
        need = self.wait_seconds(n)
        if need > 0:
            self.sleep(need)
        self.book(n)
        return need

    def book(self, n=1, at=None):
        stamp = self.clock() if at is None else at
        self.releases.extend([stamp] * n)


IGATE = IgateBudget()


# ---------------------------------------------------------------- the transmitted third-party frame

def third_party(info):
    """Splits the information field (bytes or str) of a frame the AX.25 edge transmitted for an IS packet:
    {inner_src, inner_dst, inner_path (list), inner_info, inner_line} for `}SRC>DST[,PATH]:info`, None when it is not a
    third-party packet. Latin-1 text, as the TNC2 helpers of dvtest.ax25."""
    text = info.decode("latin-1") if isinstance(info, (bytes, bytearray)) else str(info)
    if not text.startswith("}"):
        return None
    head, sep, rest = text[1:].partition(":")
    src, gt, route = head.partition(">")
    if not sep or not gt or not src:
        return None
    parts = route.split(",")
    return {"inner_src": src, "inner_dst": parts[0], "inner_path": parts[1:], "inner_info": rest, "inner_line": text[1:]}


def is_position_info(info):
    """True for an APRS information field that starts a plain or compressed position report (data type `!`, `=`, `/`, `@`)."""
    return bool(info) and info[0] in "!=/@"


# ---------------------------------------------------------------- APRS items

def item_info(name, lat_text_pos, live=True, comment=""):
    """`)NAME!<position>comment` (live) or `)NAME_<position>comment` (killed). `lat_text_pos` is the plain position text
    `DDMM.mmN/DDDMM.mmE>` (cases/position_support.plain_text). The name is 3..9 printable characters without `!` and `_`
    (object.rs: the marker is the first `!` or `_` within the first ten bytes)."""
    if not 3 <= len(name) <= 9 or any(c in "!_" for c in name) or not all(32 <= ord(c) < 127 for c in name):
        raise ValueError("an item name is 3..9 printable characters without '!' and '_'")
    return ")" + name + ("!" if live else "_") + lat_text_pos + comment


# ---------------------------------------------------------------- SSE cursors

CURSOR = re.compile(r"^[A-Za-z0-9]+-[0-9]+$")


def cursor_of(frame):
    """The `id` (`<boot>-<ordinal>`) of an SSE frame dict, or None when it has none or a malformed one."""
    value = (frame or {}).get("id")
    return value if isinstance(value, str) and CURSOR.match(value) else None


def newest_cursor(frames):
    """The id of the newest frame (a list of (time, frame)) that has a well-formed id, else None."""
    for _at, frame in reversed(list(frames)):
        found = cursor_of(frame)
        if found:
            return found
    return None


def replay_path(cursor, topics=None):
    """The public events URL that resumes after `cursor` (the Last-Event-ID may also be a query parameter, openapi)."""
    path = "/api/v1/public/events?last_event_id=%s" % cursor
    return path + ("&topics=%s" % topics if topics else "")


def event_names(frames):
    return [f.get("event") for _t, f in frames]


# ---------------------------------------------------------------- what the #/positions page shows

KEEP_MS = 30 * 60 * 1000
SKEW_MS = 60 * 1000
ROOM_RE = re.compile(r"^[A-Z]$|^r[1-9][0-9]{1,9}$")
CALL_RE = re.compile(r"^[A-Za-z0-9 /.-]{1,10}$")
PROTO_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
IDENTS = ("none", "session", "stream", "account")
VIAS = ("m17", "web", "aprsis", "rf", "forwarded")
NAME_RE = re.compile(r"^[\x20-\x7e]{1,9}$")


def page_row_ok(row, room, now_ms):
    """Port of positionRow() of web/site/positions.js (read only): does the page accept this public row for `room`? Position and object rows
    only: the DP6c weather and telemetry rows (types 5 and 6) are merged into station rows by the page and are not mirrored (the DOM case sends none).
    UNVERIFIED mirror: the page code is the authority; a row the page drops and this accepts shows as a count mismatch."""
    if not isinstance(row, dict) or row.get("type") != "data" or row.get("room") != room:
        return False
    if not ROOM_RE.match(room or "") or row.get("item_type") not in ("position", "object"):
        return False
    rid, ts = row.get("id"), row.get("ts_ms")
    if not isinstance(rid, int) or isinstance(rid, bool) or rid < 1 or not isinstance(ts, int) or ts < 0:
        return False
    if ts > now_ms + SKEW_MS or ts <= now_ms - KEEP_MS:
        return False
    if not isinstance(row.get("callsign"), str) or not CALL_RE.match(row["callsign"]):
        return False
    if not isinstance(row.get("protocol"), str) or not PROTO_RE.match(row["protocol"]):
        return False
    if "hops" in row or "forwarded" in row:
        # DP6c (4ac79f26, positions.js): hops an integer 0..255, forwarded a boolean, both required, and the station key (the callsign cut at
        # the first space) not empty; a row without either key is judged by the pre-DP6c rule (the probe is the row itself, not an env switch)
        hops = row.get("hops")
        if not isinstance(hops, int) or isinstance(hops, bool) or not 0 <= hops <= 255 or not isinstance(row.get("forwarded"), bool):
            return False
        if not row["callsign"].split(" ")[0]:
            return False
    body = row.get("body")
    if not isinstance(body, dict) or row.get("ident") not in IDENTS or row.get("via") not in VIAS:
        return False
    if row["item_type"] == "object":
        if body.get("state") not in (0, 1) or not isinstance(body.get("name"), str) or not NAME_RE.match(body["name"]):
            return False
    kill = row["item_type"] == "object" and body.get("state") == 0
    if not kill:
        lat, lon, amb = body.get("lat_e7"), body.get("lon_e7"), body.get("ambiguity")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lat, lon, amb)):
            return False
        if abs(lat) > 900000000 or abs(lon) > 1800000000 or not 0 <= amb <= 4:
            return False
    return True


def page_rows(state, room, now_ms):
    """The rows of `room` the page lists (positionRows: valid rows, kills dropped), newest first (ts_ms, id)."""
    rows = []
    for item in (state or {}).get("rooms", []) or []:
        if item.get("room") != room:
            continue
        for row in item.get("positions", []) or []:
            if isinstance(row, dict):
                row = dict(row, room=row.get("room", room))
                if page_row_ok(row, room, now_ms) and not (row["item_type"] == "object" and row["body"].get("state") == 0):
                    rows.append(row)
    rows.sort(key=lambda r: (r["ts_ms"], r["id"]), reverse=True)
    return rows


def row_label(row):
    """The first cell of a table row: `NAME (CALL)` for an object, the callsign otherwise (positions.js)."""
    name = row["body"].get("name")
    return "%s (%s)" % (name, row["callsign"]) if name else row["callsign"]


# ---------------------------------------------------------------- the optional basemap (MAP1)

MANIFEST_ARCHIVE = re.compile(r"^basemap-[0-9]{8}-z(?:0?[0-9]|1[0-5])\.pmtiles$")
PMTILES_HEADER = 127


def pmtiles_header(data):
    """{min_zoom, max_zoom, bbox [west, south, east, north] in degrees} of a PMTiles v3 header (127 bytes: magic `PMTiles`,
    version 3, the e7 bounds at byte 102). Raises ValueError for anything else."""
    if len(data) < PMTILES_HEADER or data[:7] != b"PMTiles" or data[7] != 3:
        raise ValueError("not a PMTiles v3 archive")
    min_zoom, max_zoom = data[100], data[101]
    west, south, east, north = struct.unpack_from("<iiii", data, 102)
    return {"min_zoom": min_zoom, "max_zoom": max_zoom,
            "bbox": [west / 1e7, south / 1e7, east / 1e7, north / 1e7]}


def make_pmtiles_header(west, south, east, north, min_zoom=0, max_zoom=10):
    """A syntactically valid 127 byte PMTiles v3 header (for the unit tests; NOT a usable archive)."""
    head = bytearray(PMTILES_HEADER)
    head[:8] = b"PMTiles\x03"
    head[100], head[101] = min_zoom, max_zoom
    struct.pack_into("<iiii", head, 102, round(west * 1e7), round(south * 1e7), round(east * 1e7), round(north * 1e7))
    return bytes(head)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_ok(manifest):
    """The checks of positions-map.js mount(): version 1, a dated archive name, files listing the archive."""
    return (isinstance(manifest, dict) and manifest.get("version") == 1 and isinstance(manifest.get("archive"), str)
            and bool(MANIFEST_ARCHIVE.match(manifest["archive"])) and isinstance(manifest.get("files"), list)
            and any(isinstance(f, dict) and f.get("path") == manifest["archive"] for f in manifest["files"]))


def make_manifest(archive_path, build_date="20260101"):
    """A MANIFEST for a bare .pmtiles file (when the lead supplies only the archive): the archive is renamed
    `basemap-<date>-z<maxzoom>.pmtiles` (the name the page insists on) and listed with its sha256; no glyph files (the map
    labels then need the glyph requests to answer 404 on the own origin, which they do: UNVERIFIED that the map still renders).
    Returns (manifest dict, archive file name)."""
    with open(archive_path, "rb") as fh:
        header = pmtiles_header(fh.read(PMTILES_HEADER))
    name = "basemap-%s-z%d.pmtiles" % (build_date, header["max_zoom"])
    manifest = {"version": 1, "archive": name, "bbox": header["bbox"], "maxzoom": header["max_zoom"], "build_date": build_date,
                "files": [{"path": name, "sha256": sha256_file(archive_path)}]}
    return manifest, name


def basemap_kind(path):
    """'dir' (a complete output directory of scripts/basemap-build.py: MANIFEST, archive, glyphs), 'tar' (a .tar / .tar.gz /
    .tgz of that directory), 'pmtiles' (a bare archive) or None."""
    p = Path(path)
    if p.is_dir():
        return "dir" if (p / "MANIFEST").is_file() else None
    if not p.is_file():
        return None
    if p.suffix == ".pmtiles":
        return "pmtiles"
    if p.name.endswith((".tar", ".tar.gz", ".tgz")):
        return "tar"
    return None


def _safe_member(name):
    parts = Path(name).parts
    return bool(parts) and not Path(name).is_absolute() and ".." not in parts


def stage_basemap(source, staging):
    """Copies the basemap the lead provided into the NEW directory `staging` (it must not exist) and returns the manifest
    dict. Directory: copied as is. Tar: extracted member by member (regular files and directories only, no absolute or `..`
    names, a single leading directory component is stripped when MANIFEST is below it). Bare .pmtiles: renamed and a
    MANIFEST is made. Afterwards every file the MANIFEST lists must be there with its sha256 (ValueError otherwise)."""
    kind = basemap_kind(source)
    staging = Path(staging)
    if kind is None:
        raise ValueError("%s is neither a basemap directory (with MANIFEST), a tar of one nor a .pmtiles archive" % source)
    if staging.exists():
        raise ValueError("staging directory exists: %s" % staging)
    if kind == "dir":
        shutil.copytree(source, staging, symlinks=False)
    elif kind == "tar":
        staging.mkdir(parents=True)
        with tarfile.open(source) as archive:
            members = [m for m in archive.getmembers() if m.isfile() or m.isdir()]
            if any(not _safe_member(m.name) for m in members):
                raise ValueError("the tar has an absolute or parent-relative member name")
            strip = 0
            names = [m.name for m in members if m.isfile()]
            if "MANIFEST" not in names and names and all(n.split("/", 1)[0] == names[0].split("/", 1)[0] for n in names) \
                    and any(n == names[0].split("/", 1)[0] + "/MANIFEST" for n in names):
                strip = 1
            for m in members:
                parts = Path(m.name).parts[strip:]
                if not parts:
                    continue
                target = staging.joinpath(*parts)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        manifest, name = make_manifest(source)
        staging.mkdir(parents=True)
        shutil.copyfile(source, staging / name)
        (staging / "MANIFEST").write_text(json.dumps(manifest, indent=2) + "\n", encoding="ascii")
    manifest = json.loads((staging / "MANIFEST").read_text(encoding="ascii"))
    if not manifest_ok(manifest):
        raise ValueError("the MANIFEST is not one the page accepts (version 1, basemap-YYYYMMDD-zN.pmtiles listed in files)")
    for entry in manifest["files"]:
        path = staging / entry["path"]
        if not _safe_member(entry["path"]) or not path.is_file() or path.is_symlink():
            raise ValueError("MANIFEST lists %s, which is not a regular file of the basemap" % entry["path"])
        if entry.get("sha256") and sha256_file(path) != entry["sha256"]:
            raise ValueError("sha256 of %s differs from the MANIFEST" % entry["path"])
    return manifest


DEFAULT_BBOX = [97.3, 5.6, 105.7, 20.5]      # the default of scripts/basemap-build.py (Thailand)


def basemap_bbox(source):
    """[west, south, east, north] of a provided basemap without copying it: the MANIFEST of a directory, the MANIFEST member of a
    tar, the PMTiles header of a bare archive. DEFAULT_BBOX when it cannot be read (the staging step then reports the real fault)."""
    try:
        kind = basemap_kind(source)
        if kind == "dir":
            box = json.loads((Path(source) / "MANIFEST").read_text(encoding="ascii")).get("bbox")
        elif kind == "pmtiles":
            with open(source, "rb") as fh:
                box = pmtiles_header(fh.read(PMTILES_HEADER))["bbox"]
        elif kind == "tar":
            box = None
            with tarfile.open(source) as archive:
                for member in archive.getmembers():
                    parts = Path(member.name).parts
                    if member.isfile() and parts and parts[-1] == "MANIFEST" and len(parts) <= 2 and _safe_member(member.name):
                        box = json.loads(archive.extractfile(member).read().decode("ascii")).get("bbox")
                        break
        else:
            box = None
        if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
            return [float(v) for v in box]
    except (OSError, ValueError, tarfile.TarError, UnicodeDecodeError):
        pass
    return list(DEFAULT_BBOX)


INSTALLED_MARK = ".dvt-installed"


def install_basemap(state_dir, source):
    """Installs the provided basemap as <state_dir>/basemap/ the way deployment.md says (a staging directory on the same
    filesystem, then one rename; never into an existing basemap/). Leaves a marker file INSIDE the installed directory so
    that remove_basemap() deletes only what this function made (the marker is not in the MANIFEST: it is never served).
    Returns the manifest."""
    state = Path(state_dir)
    final = state / "basemap"
    if final.exists():
        raise ValueError("%s exists already: not overwritten" % final)
    staging = state / "basemap.dvt-staging"
    if staging.exists():
        raise ValueError("%s exists already: not overwritten" % staging)
    try:
        manifest = stage_basemap(source, staging)
        (staging / INSTALLED_MARK).write_text("installed by dv-testset\n", encoding="ascii")
        os.rename(staging, final)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def remove_basemap(state_dir):
    """Removes <state_dir>/basemap/ only when install_basemap() made it (the marker file is inside). True when removed."""
    final = Path(state_dir) / "basemap"
    if final.is_dir() and not final.is_symlink() and (final / INSTALLED_MARK).is_file():
        shutil.rmtree(final)
        return True
    return False


def inside_bbox(lat, lon, bbox, margin=0.1):
    """True when (lat, lon) lies inside [west, south, east, north] shrunk by `margin` of each span."""
    west, south, east, north = bbox
    dx, dy = (east - west) * margin, (north - south) * margin
    return west + dx <= lon <= east - dx and south + dy <= lat <= north - dy


def spread_points(bbox, count=4):
    """`count` (lat, lon) points inside the bbox, far apart (a 2 x 2 grid for four): the markers must not overlap at the zoom
    the map picks. Rounded to 0.1 degree so that the APRS text carries them exactly."""
    west, south, east, north = bbox
    fx = [0.3, 0.7, 0.3, 0.7, 0.5, 0.5, 0.15, 0.85]
    fy = [0.3, 0.3, 0.7, 0.7, 0.5, 0.15, 0.5, 0.85]
    return [(round(south + (north - south) * fy[i], 1), round(west + (east - west) * fx[i], 1)) for i in range(min(count, len(fx)))]


# ---------------------------------------------------------------- PNG pixels

def decode_png(data):
    """(width, height, rows) of an 8-bit, non-interlaced PNG (grayscale, RGB, RGBA or grey+alpha), rows being lists of
    (r, g, b) tuples. Stdlib only (zlib). ValueError for anything else."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, header, palette = 8, [], None, None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            palette = [tuple(chunk[i:i + 3]) for i in range(0, len(chunk), 3)]
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("no IHDR")
    width, height, depth, ctype, _comp, _filt, interlace = header
    if depth != 8 or interlace != 0 or ctype not in (0, 2, 3, 4, 6):
        raise ValueError("only 8-bit non-interlaced PNGs: depth %d type %d interlace %d" % (depth, ctype, interlace))
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    if len(raw) != (stride + 1) * height:
        raise ValueError("PNG data has the wrong size")
    rows, prev = [], bytearray(stride)
    for y in range(height):
        ft = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + b) & 255
            elif ft == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 255
            elif ft != 0:
                raise ValueError("bad PNG filter %d" % ft)
        prev = line
        if ctype == 2:
            rows.append([(line[i], line[i + 1], line[i + 2]) for i in range(0, stride, 3)])
        elif ctype == 6:
            rows.append([(line[i], line[i + 1], line[i + 2]) for i in range(0, stride, 4)])
        elif ctype == 0:
            rows.append([(v, v, v) for v in line])
        elif ctype == 4:
            rows.append([(line[i], line[i], line[i]) for i in range(0, stride, 2)])
        else:
            if palette is None:
                raise ValueError("palette PNG without PLTE")
            rows.append([palette[v] for v in line])
    return width, height, rows


def blobs(rows, color, tol=12, min_area=20):
    """Connected components (4-neighbourhood) of the pixels within `tol` of `color` (per channel) whose area is at least
    `min_area`: a list of (area, centre x, centre y). A marker circle (radius 7 px, a white outline) is one blob of about 100
    pixels of its fill colour; two markers closer than their diameter merge into one."""
    height = len(rows)
    width = len(rows[0]) if height else 0
    mask = [[all(abs(p[k] - color[k]) <= tol for k in range(3)) for p in row] for row in rows]
    seen = [[False] * width for _ in range(height)]
    found = []
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue
            queue, area, sx, sy = deque([(x, y)]), 0, 0, 0
            seen[y][x] = True
            while queue:
                cx, cy = queue.popleft()
                area += 1
                sx += cx
                sy += cy
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < width and 0 <= ny < height and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            if area >= min_area:
                found.append((area, sx / area, sy / area))
    return found


STATION_RGB = (0x14, 0x6c, 0xad)      # circle-color of a station marker (positions-map.js)
OBJECT_RGB = (0xb3, 0x6b, 0x00)       # circle-color of an object marker


# ---------------------------------------------------------------- origins a page requests

def origin_of(url):
    """`scheme://host[:port]` of an http(s) or ws(s) URL, else None (data:, blob:, about: and friends never leave the page)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
        return None
    default = {"http": 80, "https": 443, "ws": 80, "wss": 443}[parts.scheme]
    port = parts.port or default
    return "%s://%s%s" % (parts.scheme, parts.hostname, "" if port == default else ":%d" % port)


class OriginAudit:
    """Records every request of a Playwright context (`context.on('request')`, which includes the requests of dedicated
    workers in Chromium; UNVERIFIED for Firefox and WebKit) and aborts the ones to a foreign origin so that nothing leaves the
    page's own origin even when the page misbehaves. `allowed` is the page origin. `urls` is every URL seen, `blocked` the
    foreign ones."""

    def __init__(self, allowed):
        self.allowed = origin_of(allowed) or allowed
        self.urls = []
        self.blocked = []

    def install(self, context):
        context.on("request", lambda request: self.urls.append(request.url))

        def foreign(url):
            origin = origin_of(url)
            return origin is not None and origin != self.allowed

        def handler(route):
            self.blocked.append(route.request.url)
            route.abort()

        context.route(foreign, handler)
        return self

    def origins(self):
        return sorted({o for o in (origin_of(u) for u in self.urls) if o})

    def foreign_origins(self):
        return [o for o in self.origins() if o != self.allowed]

    def paths(self, prefix):
        return [urlsplit(u).path for u in self.urls if urlsplit(u).path.startswith(prefix) and origin_of(u) == self.allowed]
