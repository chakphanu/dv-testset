# SPDX-License-Identifier: MIT
"""Helpers of the DP7 / MAP2 `#/positions` DOM case (cases/test_dp7_positions_page.py): the public basemap surface of dv-svc-web
(`/basemap/tiles.json`, generation-qualified tiles, listed glyph files) and the installed base that `dv-svc-basemap install` writes
(`<state>/basemap/base/MANIFEST` v2, dated tier archives, `glyphs/`, `<state>/basemap/regions/INDEX`).

Sources (DV-Router 8ffab80, image 220e45b): docs/design/deployment.md "Optional position basemap (MAP2b)" and "MAP2c";
docs/design/web.md (positions section); crates/svc/web/src/basemap.rs; crates/svc/basemap/src/core/manifest.rs;
web/site/positions-map.js (`positionDescriptor`, `positionFontFaces`).

Pure functions only (stdlib): the browser and rig parts stay in the case module.
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

GEN_RE = re.compile(r"^[0-9a-f]{16}$")
CELL_RE = re.compile(r"^10/(?:0|[1-9][0-9]{0,3})/(?:0|[1-9][0-9]{0,3})$")
TILE_PATH_RE = re.compile(r"^/basemap/tiles/([0-9a-f]{16})/([0-9]{1,2})/([0-9]{1,5})/([0-9]{1,5})\.mvt$")
GLYPH_PREFIX = "/basemap/glyphs/"
TILES_JSON = "/basemap/tiles.json"
NOT_INSTALLED = {"installed": False}
NOT_INSTALLED_ETAG = '"not-installed"'              # crates/svc/web/src/basemap.rs: the answer without an install
TILE_CACHE = "public, max-age=31536000, immutable"  # deployment.md MAP2c: tile responses
MVT_TYPE = "application/vnd.mapbox-vector-tile"
ATTRIBUTION = "(c) OpenStreetMap contributors"
COMPACT_KEYS = {"installed", "gen", "tiles", "minzoom", "maxzoom", "overview_maxzoom", "coverage", "builds", "fetch_enabled",
                "attribution"}
# images before MAP2f (DV-Router 9d6776f) also carried the per-file glyph inventory under `files`; the shape is chosen by the
# PRESENCE of that key, never by an image name
DESCRIPTOR_KEYS = COMPACT_KEYS | {"files"}
MAX_DESCRIPTOR_BYTES = 32 * 1024                    # MAP2f: basemap_core.rs MAX_DESCRIPTOR_BYTES, deployment.md "The descriptor includes"
FONT_URL_RE = re.compile(r"^glyphs/fonts/[A-Za-z0-9_-]+\.(?:ttf|otf)$")
DATE_RE = re.compile(r"^[0-9]{8}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- the descriptor (GET /basemap/tiles.json)

def _box_ok(box):
    return (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)
            and -180 <= box[0] <= 180 and -180 <= box[2] <= 180 and -90 <= box[1] and box[3] <= 90 and box[0] != box[2] and box[1] < box[3])


def descriptor_problems(desc, raw_len=None):
    """What is wrong with an INSTALLED descriptor, as a list of strings (empty = it has every field web.md names and the shape the
    page's positionDescriptor() accepts). The key set is exact: web.md says it carries no callsign or position timestamp.
    Two shapes: the compact one (MAP2f, no `files`; `raw_len`, the body size in bytes when known, is bounded to 32 KiB) and the older
    one that adds `files`, a list of {path, ...} entries (no size bound: the glyph inventory makes it larger, 35767 bytes seen on caebc57); a descriptor with the key `files` is held to the older shape."""
    if not isinstance(desc, dict):
        return ["the descriptor is not a JSON object"]
    out = []
    legacy = "files" in desc
    want = DESCRIPTOR_KEYS if legacy else COMPACT_KEYS
    if set(desc) != want:
        out.append("keys %s, want %s" % (sorted(desc), sorted(want)))
    if not legacy and raw_len is not None and raw_len > MAX_DESCRIPTOR_BYTES:
        out.append("the descriptor is %d bytes, want at most %d (MAP2f)" % (raw_len, MAX_DESCRIPTOR_BYTES))
    if desc.get("installed") is not True:
        out.append("installed is %r" % (desc.get("installed"),))
    gen = desc.get("gen")
    if not isinstance(gen, str) or not GEN_RE.match(gen):
        out.append("gen %r is not 16 lower-case hex digits" % (gen,))
        gen = "?"
    if desc.get("minzoom") != 0:
        out.append("minzoom is %r, want 0" % (desc.get("minzoom"),))
    maxzoom, overview = desc.get("maxzoom"), desc.get("overview_maxzoom")
    if not (isinstance(maxzoom, int) and not isinstance(maxzoom, bool) and 0 < maxzoom <= 15):
        out.append("maxzoom %r is not an integer in 1..15" % (maxzoom,))
    if not (isinstance(overview, int) and not isinstance(overview, bool) and 0 <= overview < (maxzoom if isinstance(maxzoom, int) else 0)):
        out.append("overview_maxzoom %r is not an integer below maxzoom %r" % (overview, maxzoom))
    if desc.get("tiles") != ["/basemap/tiles/%s/{z}/{x}/{y}.mvt" % gen]:
        out.append("tiles %r, want the one generation-qualified URL of gen %s" % (desc.get("tiles"), gen))
    cov = desc.get("coverage")
    if not isinstance(cov, dict) or set(cov) != {"boxes", "cells"}:
        out.append("coverage %r lacks exactly boxes and cells" % (cov,))
    else:
        if not isinstance(cov["boxes"], list) or len(cov["boxes"]) > 32 or not all(_box_ok(b) for b in cov["boxes"]) or not cov["boxes"]:
            out.append("coverage.boxes %r is not 1..32 valid [west, south, east, north] boxes" % (cov["boxes"],))
        if not isinstance(cov["cells"], list) or len(cov["cells"]) > 512 or not all(isinstance(c, str) and CELL_RE.match(c) for c in cov["cells"]):
            out.append("coverage.cells %r is not up to 512 `10/x/y` cells" % (cov["cells"],))
    builds = desc.get("builds")
    if not isinstance(builds, list) or not builds or not all(isinstance(b, str) and DATE_RE.match(b) for b in builds):
        out.append("builds %r is not a list of YYYYMMDD dates" % (builds,))
    if desc.get("fetch_enabled") is not False:
        out.append("fetch_enabled is %r, want false (automatic fetching is MAP2d, planned)" % (desc.get("fetch_enabled"),))
    if desc.get("attribution") != ATTRIBUTION:
        out.append("attribution %r, want %r" % (desc.get("attribution"), ATTRIBUTION))
    if not legacy:
        return out
    files = desc.get("files")
    if not isinstance(files, list) or len(files) > 512 or not all(isinstance(f, dict) and isinstance(f.get("path"), str) for f in files):
        out.append("files is not a list of {path, ...} entries")
    else:
        paths = {f["path"] for f in files}
        for need in ("glyphs/FONT-FACES.json", "glyphs/OFL.txt"):
            if need not in paths:
                out.append("files does not list %s" % need)
        if not any(p.startswith("glyphs/NotoSansRegular/") and p.endswith(".pbf") for p in paths):
            out.append("files lists no glyphs/NotoSansRegular/*.pbf range")
        if any(p.startswith("/") or ".." in p.split("/") or not p.startswith("glyphs/") for p in paths):
            out.append("files lists a path outside glyphs/: %s" % sorted(p for p in paths if not p.startswith("glyphs/"))[:3])
    return out


def font_face_problems(faces, files):
    """positionFontFaces() of the page: exactly the one font stack NotoSansRegular, 1..256 faces whose url is a listed
    glyphs/fonts/*.ttf|otf file with a non-empty unicode-range list. `files` is the list of {path, ...} entries to hold the urls
    against: the descriptor's `files` of an older image, or the installed MANIFEST's `files` (the server's glyph allowlist) when the
    descriptor is compact (MAP2f)."""
    if not isinstance(faces, dict) or set(faces) != {"NotoSansRegular"} or not isinstance(faces["NotoSansRegular"], list):
        return ["FONT-FACES.json is not exactly {NotoSansRegular: [...]}"]
    entries = faces["NotoSansRegular"]
    if not 0 < len(entries) <= 256:
        return ["FONT-FACES.json lists %d faces, want 1..256" % len(entries)]
    listed = {f.get("path") for f in files if isinstance(f, dict)}
    out = []
    for face in entries:
        url = face.get("url") if isinstance(face, dict) else None
        ranges = face.get("unicode-range") if isinstance(face, dict) else None
        if not isinstance(url, str) or not FONT_URL_RE.match(url) or url not in listed:
            out.append("face url %r is not a listed glyphs/fonts file" % (url,))
        if not isinstance(ranges, list) or not ranges or len(ranges) > 4096:
            out.append("face %r has no unicode-range list" % (url,))
    return out[:5]


def tile_requests(paths):
    """[(gen, z, x, y, path)] of the `/basemap/tiles/<gen>/<z>/<x>/<y>.mvt` paths in `paths`."""
    out = []
    for path in paths:
        m = TILE_PATH_RE.match(path)
        if m:
            out.append((m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), path))
    return out


def lonlat_tile(lon, lat, zoom):
    """The slippy-map tile (x, y) containing (lon, lat) at `zoom`."""
    import math
    n = 1 << zoom
    lat = max(-85.0511287798066, min(85.0511287798066, lat))
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def other_gen(gen):
    """A well-formed generation that is not `gen`."""
    return "0" * 16 if gen != "0" * 16 else "f" * 16


# ---------------------------------------------------------------- the installed base (what `dv-svc-basemap install` writes)

SCHEMA_MAJOR = 4
MAX_TIERS = 32


def manifest_problems(manifest):
    """Structural checks of a MANIFEST v2 (crates/svc/basemap/src/core/manifest.rs `validate`, the parts that can be seen without the
    tier naming rules): version 2, schema major 4, overview < detail <= 15, 2..32 tiers with exactly one `world` overview, the detail
    tiers on the shared zoom range, the same build for all tiers, files below glyphs/ with a licence and a .pbf range."""
    if not isinstance(manifest, dict):
        return ["MANIFEST is not a JSON object"]
    out = []
    if manifest.get("version") != 2:
        out.append("version is %r, want 2 (v1 is the refused MAP1 shape)" % (manifest.get("version"),))
    if not re.match(r"^%d\.[0-9]+" % SCHEMA_MAJOR, str(manifest.get("schema", ""))):
        out.append("schema %r is not a major %d version" % (manifest.get("schema"), SCHEMA_MAJOR))
    overview, detail = manifest.get("overview_maxzoom"), manifest.get("detail_maxzoom")
    if not (isinstance(overview, int) and isinstance(detail, int) and 0 <= overview < detail <= 15):
        out.append("overview_maxzoom %r / detail_maxzoom %r" % (overview, detail))
        return out
    tiers = manifest.get("tiers")
    if not isinstance(tiers, list) or not 2 <= len(tiers) <= MAX_TIERS:
        return out + ["tiers %r is not 2..%d entries" % (tiers, MAX_TIERS)]
    worlds, builds = 0, set()
    for tier in tiers:
        if not isinstance(tier, dict) or tier.get("role") not in ("overview", "detail"):
            out.append("tier %r has no role" % (tier,))
            continue
        builds.add(tier.get("build"))
        if not isinstance(tier.get("archive"), str) or not tier["archive"].endswith(".pmtiles") or "/" in tier["archive"]:
            out.append("tier archive %r" % (tier.get("archive"),))
        if tier["role"] == "overview":
            worlds += 1
            if tier.get("name") != "world" or tier.get("minzoom") != 0 or tier.get("maxzoom") != overview:
                out.append("overview tier %r" % (tier,))
        elif tier.get("minzoom") != overview + 1 or tier.get("maxzoom") != detail:
            out.append("detail tier %r is not z%d-%d" % (tier.get("name"), overview + 1, detail))
    if worlds != 1:
        out.append("%d overview tiers, want 1" % worlds)
    if len(builds) != 1:
        out.append("tiers use %d different builds" % len(builds))
    files = manifest.get("files")
    if not isinstance(files, list):
        return out + ["files is not a list"]
    paths = [f.get("path") for f in files if isinstance(f, dict)]
    if len(paths) != len(files) or len(set(paths)) != len(paths) or not all(isinstance(p, str) and p.startswith("glyphs/") for p in paths):
        out.append("files are not unique paths below glyphs/")
    elif "glyphs/OFL.txt" not in paths or not any(p.endswith(".pbf") for p in paths):
        out.append("files lack glyphs/OFL.txt or a .pbf range")
    return out


def detail_bbox(manifest, default):
    """[west, south, east, north] of the first detail tier of a MANIFEST v2 (the seeds go there), else `default`."""
    try:
        for tier in manifest["tiers"]:
            if tier["role"] == "detail":
                box = [float(v) for v in tier["bbox"]]
                if len(box) == 4:
                    return box
    except (KeyError, TypeError, ValueError):
        pass
    return list(default)


def map2_root(path):
    """The directory that holds base/ (and regions/) for `path`: `path` itself, or `path/basemap` when the --state directory of the
    install was named. None when neither holds base/MANIFEST."""
    p = Path(path)
    for root in (p, p / "basemap"):
        if (root / "base" / "MANIFEST").is_file():
            return root
    return None


def flavor(path):
    """'map2' (a directory the product's `dv-svc-basemap install` wrote: base/MANIFEST), 'v1' (the MAP1 shapes of
    position2_support.basemap_kind: a directory with MANIFEST, a tar of one, a bare .pmtiles archive) or None."""
    import position2_support as p2
    p = Path(path)
    if p.is_dir() and map2_root(p) is not None:
        return "map2"
    return "v1" if p2.basemap_kind(p) is not None else None


def read_manifest2(source):
    """The MANIFEST dict of a product-made tree `source` (it holds base/MANIFEST), or None when there is none / it is not JSON."""
    root = map2_root(source) or Path(source)
    try:
        return json.loads((root / "base" / "MANIFEST").read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def tree_problems(root, manifest):
    """Every tier archive and file of `manifest` exists below `<root>/base/` as a regular file with the recorded size and sha256."""
    out = []
    base = Path(root) / "base"
    entries = [(t.get("archive"), t.get("bytes"), t.get("sha256")) for t in manifest.get("tiers", [])]
    entries += [(f.get("path"), f.get("bytes"), f.get("sha256")) for f in manifest.get("files", [])]
    for rel, size, digest in entries:
        path = base / str(rel)
        if ".." in str(rel).split("/") or str(rel).startswith("/") or path.is_symlink() or not path.is_file():
            out.append("%s is not a regular file of the base" % rel)
        elif path.stat().st_size != size:
            out.append("%s has %d bytes, MANIFEST says %s" % (rel, path.stat().st_size, size))
        elif sha256_file(path) != digest:
            out.append("sha256 of %s differs from the MANIFEST" % rel)
    return out[:5]


INSTALLED_MARK = ".dvt-installed"


def install_tree(state_dir, source):
    """Installs a tree made by `dv-svc-basemap install --state <dir>` (it holds base/ and regions/) as <state_dir>/basemap/ by one
    rename from a staging directory next to it (never over an existing basemap/), with a marker INSIDE (remove_tree deletes only
    what this made). The product's own tool made the bytes; this copies and re-checks them. Returns the MANIFEST dict.
    ValueError for anything that is not a complete verified v2 tree."""
    source = map2_root(source) or Path(source)
    manifest = read_manifest2(source)
    if manifest is None:
        raise ValueError("%s has no base/MANIFEST (name the `basemap/` directory that `dv-svc-basemap install` wrote)" % source)
    problems = manifest_problems(manifest) + tree_problems(source, manifest)
    if problems:
        raise ValueError("; ".join(problems[:3]))
    if not (source / "regions" / "INDEX").is_file():
        raise ValueError("%s has no regions/INDEX (install writes one)" % source)
    state = Path(state_dir)
    final, staging = state / "basemap", state / "basemap.dvt-staging"
    if final.exists() or staging.exists():
        raise ValueError("%s or %s exists already: not overwritten" % (final, staging))
    try:
        shutil.copytree(source, staging, symlinks=False)
        (staging / INSTALLED_MARK).write_text("installed by dv-testset\n", encoding="ascii")
        os.rename(staging, final)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def remove_tree(state_dir):
    """Removes <state_dir>/basemap/ only when install_tree() or the MAP1 installer made it (marker inside). True when removed."""
    final = Path(state_dir) / "basemap"
    if final.is_dir() and not final.is_symlink() and (final / INSTALLED_MARK).is_file():
        shutil.rmtree(final)
        return True
    return False
