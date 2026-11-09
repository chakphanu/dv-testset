# SPDX-License-Identifier: MIT
"""Web unit, DP7 / MAP2: the public `#/positions` page in real browsers (Playwright Chromium, Firefox and Playwright WebKit, the
fixtures of cases/browser_fixtures.py). Needs `scripts/run.sh --profile web,browser` with the data worker on
(data-ident.toml); every case skips with the reason otherwise. Page knowledge (route, test ids, texts) is in cases/web_pages.py
("#/positions"); helpers: cases/position2_support.py (table, origins, MAP1 installer) and cases/position_map2_support.py (the MAP2
descriptor, the installed base); case table, run commands, UNVERIFIED shapes: docs/dp7-cases.md ("DP7 second set"). NOT RUN on a rig
yet: written against DV-Router 8ffab80 (image 220e45b): web/site/positions.js, positions-map.js, crates/svc/web/src/basemap.rs,
docs/design/deployment.md "Optional position basemap (MAP2b)" and web.md (positions, MAP2c).

  test_positions_page_without_a_basemap   `<state>/basemap/` ABSENT: GET /basemap/tiles.json answers 200 `{"installed":false}` (strong
                                          ETag "not-installed", no-cache, 304 on a repeat), MANIFEST, tiles, glyphs and archives are 404,
                                          the page asks for tiles.json and NOTHING else under /basemap/, the status line says `Basemap not
                                          installed.`, the map container is hidden, no attribution, the coordinate table is the list
                                          fallback (one row per public position / object of the room at the sent precision), and the
                                          page asks ONLY its own origin
  test_positions_page_refuses_a_v1_basemap  the MAP1 basemap (DVT_BASEMAP_FILE = the old tarball / directory / bare archive) copied into
                                          `<state>/basemap/` is REFUSED (deployment.md: "MANIFEST v1 (MAP1) is refused under COMPAT1";
                                          web.md: a v1 install reports `installed: false`): the same answers and page as without a
                                          basemap, and the v1 MANIFEST / archive are not served. "Not installed" is the expected result.
  test_positions_page_with_a_basemap      DVT_BASEMAP_FILE names a tree written by the product's own tool (`dv-svc-basemap install
                                          --state <dir> --profile hotspot ...`, see below): it is copied to the run's state dir; the
                                          descriptor has every field web.md names, tiles / glyphs / font faces are served as specified (200,
                                          204 for an empty tile, 404 for an unknown generation, 206 for a glyph range, MANIFEST / INDEX /
                                          archives are not served), the map is shown (canvas, no note, attribution link), the table is
                                          still there with the same rows, the page requested tiles.json, tiles of the CURRENT generation,
                                          FONT-FACES.json and nothing else, one marker circle per station / object is drawn (counted from
                                          the pixels: markers are WebGL, not DOM), and the origins requested are exactly the page's own

The basemap is NOT installed by running `dv-svc-basemap` from this case (no allow-listed control action runs a program in the SUT
container, and `install` needs raw.githubusercontent.com for its 431 pinned glyph files: it has no mirror option for them; only the
index and the tile archive have `--source-index` / `--source-base`). The operator runs it once on a host with network access and the
case copies the result, like the MAP1 build script before:

    docker run --rm -v "$PWD/.local/basemap-map2:/var/lib/dv" --entrypoint dv-svc-basemap <dv-router image> \\
        install --state /var/lib/dv --profile hotspot --detail dvt=99.9,13.0,101.1,14.1
    DVT_BASEMAP_FILE=.local/basemap-map2 scripts/run.sh ...

DVT_BASEMAP_FILE is a path under the repository as the tester container sees it (the container mounts the repository at /dvt). The
case reads `base/MANIFEST` (v2), checks every tier archive and glyph file against its recorded size and sha256, stages a copy as
`out/<run>/sut-state/basemap.dvt-staging/`, renames it to `basemap/` in one step (never over an existing one), leaves a marker file
inside and at the end removes only a `basemap/` that carries the marker. dv-svc-web reads the published base without a restart.

Markers: positions-map.js draws the stations (circle colour #146cad) and objects (#b36b00) into a MapLibre GeoJSON layer; the page
exposes no marker element and no map handle. The case counts the connected blobs of those two colours in a screenshot of the map
container (UNVERIFIED method; antialiasing, label overlap and a WebGL-less engine are the failure modes). The seeds are spread over
the first detail box of the MANIFEST so that no two circles touch. An engine without WebGL2 shows `The map needs WebGL2...`; the case
then checks the list and skips the pixel part with that reason.
"""
import contextlib
import json
import os
import re
import sys
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

import data_support as ds  # noqa: E402
import position2_support as p2  # noqa: E402
import position_map2_support as m2  # noqa: E402
import position_support as ps  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import browser as brlib, web  # noqa: E402
from dvtest.proto import m17  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

ROOT = Path(__file__).resolve().parents[1]
DOM_ROOM = "C"                                   # an own room: the seeds are all it holds (module B is the room of the other position cases)
STATIONS = ["N0PGA", "N0PGB", "N0PGC"]           # three stations ...
OBJECT_REPORTER, OBJECT_NAME = "N0PGD", "DVT7MAP"   # ... and one object
AUTH_SESSION = "/api/v1/auth/session"
SSE_PATHS = ("/api/v1/public/events", "/api/v1/operator/stream")
LOAD_S = 20.0
MAP_S = 60.0                                     # tiles from the archives: seconds, not minutes
INSTALL_S = 30.0                                 # dv-svc-web reads the published base on the next request
BASEMAP_PREFIX = "/basemap/"
NO_STORE = "no-store"
GLYPH_RANGE = "/basemap/glyphs/NotoSansRegular/0-255.pbf"
FONT_FACES = "/basemap/glyphs/FONT-FACES.json"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def basemap():
    """What DVT_BASEMAP_FILE names: (path, flavor 'map2' | 'v1' | None, bbox of the seeds, why not)."""
    value = os.environ.get("DVT_BASEMAP_FILE", "").strip()
    default = list(p2.DEFAULT_BBOX)
    if not value:
        return types.SimpleNamespace(path=None, flavor=None, bbox=default, why="DVT_BASEMAP_FILE is not set")
    path = Path(value)
    path = path if path.is_absolute() else ROOT / path
    kind = m2.flavor(path)
    if kind is None:
        return types.SimpleNamespace(path=None, flavor=None, bbox=default,
                                     why="DVT_BASEMAP_FILE=%s is neither a tree written by `dv-svc-basemap install` (base/MANIFEST) nor a MAP1 "
                                         "basemap (directory with MANIFEST, tar, .pmtiles); the container mounts the repository at /dvt: use a path "
                                         "under the repository" % value)
    if kind == "map2":
        return types.SimpleNamespace(path=path, flavor=kind, bbox=m2.detail_bbox(m2.read_manifest2(path), default), why="")
    return types.SimpleNamespace(path=path, flavor=kind, bbox=p2.basemap_bbox(path), why="")


def send_position(rig, call, info):
    """One APRS payload as an M17 packet-mode report from a client linked to the DOM room (module C)."""
    base = ps.base_call(call)
    packet = m17.m17p_packet(0x02, ps.tnc2(call, info).encode("ascii"), src=call)
    with ds.m17_client(rig.xlxd_sut, callsign=base, module=DOM_ROOM) as sender:
        sender.client.send(sender.dest, packet)
        ds.pump_for(1.0, sender)


@pytest.fixture(scope="module")
def seeds(rig, web_client, basemap):
    """Three stations and one object in the DOM room, spread over the basemap's detail box (default: Thailand), public in the
    anonymous state before any page is opened. Returns [{call, kind, lat, lon}]."""
    points = p2.spread_points(basemap.bbox, 4)
    out = []
    for call, (lat, lon) in zip(STATIONS, points):
        send_position(rig, call + "-7", ps.position_info(lat, lon, 0, "/>", "dp7 map"))
        out.append({"call": call, "kind": "position", "lat": lat, "lon": lon})
    lat, lon = points[3]
    send_position(rig, OBJECT_REPORTER + "-7", ps.object_info(OBJECT_NAME, lat, lon, True, 0, "/>", "dp7 map"))
    out.append({"call": OBJECT_REPORTER, "kind": "object", "lat": lat, "lon": lon})
    anon = web_client()
    for seed in out:
        row, _rows = ps.wait_public_row(anon, seed["call"], 30.0, room=DOM_ROOM, item_type=seed["kind"],
                                        name=OBJECT_NAME if seed["kind"] == "object" else None)
        assert row is not None, "the seed %s never became public in room %s (is the data worker on, does module %s exist?)" % (seed, DOM_ROOM, DOM_ROOM)
    return out


@pytest.fixture(scope="module")
def state_dir(target):
    """out/<run>/sut-state, the SUT state directory as the tester sees it (the web profile mounts it)."""
    state = Path(target.out) / "sut-state"
    if not state.is_dir():
        pytest.skip("the SUT state directory %s is not visible in the tester (the web profile mounts out/<run>/sut-state)" % state)
    return state


def descriptor_answer(anon):
    """GET /basemap/tiles.json: (Response, parsed JSON or None)."""
    resp = anon.get(pages.BASEMAP_TILES_JSON, pace=False)
    try:
        return resp, resp.json()
    except web.WebError:
        return resp, None


def is_installed(anon):
    resp, body = descriptor_answer(anon)
    return resp.status == 200 and isinstance(body, dict) and body.get("installed") is True


@pytest.fixture(scope="module")
def installed_basemap(basemap, state_dir, web_client):
    """Installs the tree of `dv-svc-basemap install` named by DVT_BASEMAP_FILE into out/<run>/sut-state/basemap/ (never over an
    existing one), waits until /basemap/tiles.json says `installed: true` (the web service reads the published base on a request: no
    restart; deployment.md "The web service may keep running"), removes it at the end. Yields (manifest, descriptor)."""
    if basemap.flavor != "map2":
        pytest.skip("no MAP2 basemap to install: %s (docs/dp7-cases.md: run `dv-svc-basemap install --profile hotspot` on a host with "
                    "network access and name its --state directory in DVT_BASEMAP_FILE)" % (basemap.why or "DVT_BASEMAP_FILE names a MAP1 basemap"))
    anon = web_client()
    if is_installed(anon):
        pytest.skip("a basemap is already served by this run: the cases do not replace an installed one")
    try:
        manifest = m2.install_tree(state_dir, basemap.path)
    except (ValueError, OSError) as exc:
        pytest.fail("cannot install the provided basemap %s: %s" % (basemap.path, exc))
    try:
        end = time.monotonic() + INSTALL_S
        while not is_installed(anon):
            if time.monotonic() >= end:
                resp, body = descriptor_answer(anon)
                pytest.fail("GET %s still answers %d %s %.0f s after the verified base was installed under %s/basemap (is that the state "
                            "directory dv-svc-web reads?)" % (pages.BASEMAP_TILES_JSON, resp.status, json.dumps(body)[:100], INSTALL_S, state_dir))
            time.sleep(1.0)
        yield manifest, descriptor_answer(anon)[1]
    finally:
        m2.remove_tree(state_dir)


# ---------------------------------------------------------------- page helpers

def state_rows(anon):
    """The rows the page should list for the DOM room: valid retained rows of the anonymous public state, kills dropped."""
    return p2.page_rows(ps.public_state(anon), DOM_ROOM, int(time.time() * 1000))


def expected_table(rows):
    """{label: (latitude text, longitude text, precision text)} of the rows, texts as the page prints them."""
    out = {}
    for row in rows:
        body = row["body"]
        amb = body.get("ambiguity", 0)
        out[p2.row_label(row)] = (ps.format_coordinate(body["lat_e7"], amb), ps.format_coordinate(body["lon_e7"], amb),
                                  "As received" if not amb else "Reduced by the sender (%d digits omitted)" % amb)
    return out


def open_positions(bp):
    """Opens #/positions, waits for the room list of the state and selects the DOM room."""
    resp = bp.goto(pages.positions_url())
    expect(pages.heading(bp.page)).to_have_text(pages.POSITIONS_TITLE, timeout=int(LOAD_S * 1000))
    expect(pages.positions_page(bp.page)).to_be_visible()
    expect(pages.positions_room_option(bp.page, DOM_ROOM)).to_be_attached(timeout=int(LOAD_S * 1000))
    pages.positions_room(bp.page).select_option(DOM_ROOM)
    return resp


def table_texts(bp):
    """{label: (lat, lon, precision)} of the rows on the page."""
    out = {}
    for row in pages.positions_rows(bp.page).all():
        cells = [c.strip() for c in pages.positions_cells(row).all_inner_texts()]
        if len(cells) >= 4:
            out[cells[0]] = (cells[1], cells[2], cells[3])
    return out


def assert_list_fallback(bp, anon, label):
    """The coordinate table has exactly the rows of the public state of the room, with the printed precision. The state is read
    again after the table settled; a row that moved in between (a new report) is retried once."""
    for attempt in range(2):
        want = expected_table(state_rows(anon))
        try:
            expect(pages.positions_rows(bp.page)).to_have_count(len(want), timeout=15000)
            break
        except AssertionError:
            if attempt:
                raise AssertionError("%s: the table has %d row(s), the public state of room %s lists %d: %s" % (
                    label, pages.positions_rows(bp.page).count(), DOM_ROOM, len(want), sorted(want)))
    got = table_texts(bp)
    assert got == want, "%s: the table %s differs from the public state %s" % (label, got, want)
    assert len(want) >= len(STATIONS) + 1, "%s: fewer rows than the seeds (%d)" % (label, len(want))
    expect(pages.positions_empty_note(bp.page)).to_have_count(0)
    return want


def finish_checks(bp, audit, label, expected_requests=()):
    """No page error, only the own origin asked, nothing foreign was aborted; console / CSP problems are findings."""
    assert not bp.log.errors, "%s: page error: %s" % (label, bp.log.errors[:1])
    assert audit.foreign_origins() == [], "%s: the browser requested a foreign origin: %s" % (label, audit.foreign_origins())
    assert audit.blocked == [], "%s: requests to foreign origins were aborted: %s" % (label, audit.blocked[:3])
    assert audit.origins() == [audit.allowed], "%s: origins requested %s, want only %s" % (label, audit.origins(), audit.allowed)
    problems = bp.problems(set(expected_requests) | {(AUTH_SESSION, 401)} | {(path, 429) for path in SSE_PATHS})
    for problem in problems:
        web.finding("%s: %s: %s" % (brlib.label(bp.engine), label, problem))


def map_state(bp, seconds):
    """Waits until the map is 'ready' (no status line) or the status line says why not: ('ready', '') or ('note', text);
    ('loading', text) after `seconds`."""
    end = time.monotonic() + seconds
    while True:
        note = pages.positions_map_note(bp.page)
        if note.count() == 0:
            return "ready", ""
        text = note.inner_text().strip()
        if text != pages.POSITIONS_NOTE_LOADING:
            return "note", text
        if time.monotonic() >= end:
            return "loading", text
        bp.page.wait_for_timeout(500)


# ---------------------------------------------------------------- the public basemap surface (HTTP, no browser)

def get(anon, path, headers=None):
    return anon.get(path, pace=False, headers=headers)


def assert_not_installed_surface(anon, label, extra_404=()):
    """What dv-svc-web answers on the public host without a usable install (crates/svc/web/src/basemap.rs; web.md "Missing,
    incomplete or MANIFEST v1 installs report installed: false"): tiles.json 200 `{"installed":false}` with the strong ETag
    "not-installed" and `no-cache` (304 on a repeat); every other /basemap/ path is 404 `no-store` (no listing, no SPA fallback;
    MANIFEST and raw archives are not HTTP resources). `extra_404` are more paths that must be 404 (the files of a v1 install)."""
    resp, body = descriptor_answer(anon)
    assert resp.status == 200, "%s: GET %s answered %d, want 200 {\"installed\": false}" % (label, pages.BASEMAP_TILES_JSON, resp.status)
    assert body == m2.NOT_INSTALLED, "%s: tiles.json body %r, want %r" % (label, body, m2.NOT_INSTALLED)
    assert resp.headers.get("content-type", "").startswith("application/json"), "%s: tiles.json content-type %r" % (label, resp.headers.get("content-type"))
    assert resp.headers.get("etag") == m2.NOT_INSTALLED_ETAG, "%s: tiles.json etag %r, want %s" % (label, resp.headers.get("etag"), m2.NOT_INSTALLED_ETAG)
    assert resp.headers.get("cache-control") == "no-cache", "%s: tiles.json cache-control %r" % (label, resp.headers.get("cache-control"))
    again = get(anon, pages.BASEMAP_TILES_JSON, {"If-None-Match": m2.NOT_INSTALLED_ETAG})
    assert again.status == 304 and not again.body, "%s: a repeat with If-None-Match answered %d (%d bytes), want 304" % (label, again.status, len(again.body))
    for path in (pages.BASEMAP_MANIFEST, "/basemap/", "/basemap/regions/INDEX", FONT_FACES, GLYPH_RANGE,
                 "/basemap/tiles/%s/0/0/0.mvt" % ("0" * 16), "/basemap/tiles/%s/0/0/0.mvt" % ("f" * 16), *extra_404):
        other = get(anon, path)
        assert other.status == 404, "%s: GET %s answered %d, want 404 (nothing installed)" % (label, path, other.status)
        assert other.headers.get("cache-control") == NO_STORE, "%s: GET %s cache-control %r, want no-store" % (label, path, other.headers.get("cache-control"))


def assert_missing_page(bp, anon, audit, label):
    """The page of a node without a usable basemap: the `Basemap not installed.` note, the map container hidden, no attribution,
    the list fallback, tiles.json the ONLY /basemap/ request (positions-map.js `mount`: a descriptor with installed false ends
    discovery before MapLibre, a style or a tile is loaded), only the own origin."""
    open_positions(bp)
    expect(pages.positions_map_note(bp.page)).to_have_text(pages.POSITIONS_NOTE_MISSING, timeout=int(LOAD_S * 1000))
    expect(pages.positions_map(bp.page)).to_have_class(re.compile(r"\bhidden\b"))
    expect(pages.positions_map(bp.page)).to_be_hidden()
    expect(pages.positions_attribution(bp.page)).to_have_count(0)
    assert_list_fallback(bp, anon, label)
    asked = sorted(set(audit.paths(BASEMAP_PREFIX)))
    assert asked == [pages.BASEMAP_TILES_JSON], "%s: the page asked %s under /basemap/, want only %s" % (label, asked, pages.BASEMAP_TILES_JSON)
    seen = bp.log.statuses(pages.BASEMAP_TILES_JSON)
    assert seen and set(seen) <= {200, 304}, "%s: the page saw %s for %s, want 200 (304 on a revalidation)" % (label, seen, pages.BASEMAP_TILES_JSON)
    finish_checks(bp, audit, label)


# ================================================================ the list fallback: no basemap installed

def test_positions_page_without_a_basemap(browser_page, web_client, seeds):
    """web.md (positions, MAP2c) and deployment.md (MAP2b/MAP2c): without an install `/basemap/tiles.json` answers 200
    `{"installed":false}` and the page keeps the coordinate list with a short `Basemap not installed.` note. With `<state>/basemap/`
    ABSENT: the HTTP answers of assert_not_installed_surface, the map container is hidden (class `hidden`), no attribution link is
    shown, the table lists every public position and object of the room at the sent precision, tiles.json is the only /basemap/ request
    and the browser asks no origin but the page's own."""
    bp = browser_page
    anon = web_client()
    if is_installed(anon):
        pytest.skip("a basemap is installed in this run: this case needs <state>/basemap/ absent")
    assert_not_installed_surface(anon, "no basemap")
    audit = p2.OriginAudit(bp.base).install(bp.context)
    assert_missing_page(bp, anon, audit, "no basemap")


# ================================================================ a MAP1 basemap: refused

@contextlib.contextmanager
def v1_installed(state, source):
    """The MAP1 basemap copied to <state>/basemap/ the way MAP1 was installed (position2_support.install_basemap), removed after."""
    try:
        manifest = p2.install_basemap(state, source)
    except (ValueError, OSError) as exc:
        pytest.fail("cannot copy the provided MAP1 basemap %s: %s" % (source, exc))
    try:
        yield manifest
    finally:
        p2.remove_basemap(state)


def test_positions_page_refuses_a_v1_basemap(browser_page, web_client, seeds, basemap, state_dir):
    """deployment.md: "MANIFEST v1 (MAP1) is refused under COMPAT1; rerun `install` to replace an old installation"; web.md: "Missing,
    incomplete or MANIFEST v1 installs report `installed: false` and retain the list fallback". The old MAP1 basemap (DVT_BASEMAP_FILE:
    a directory with MANIFEST, a tar of one or a bare archive) is copied into `<state>/basemap/`: that is REFUSED, so the answers and the
    page are those of no basemap, and neither the v1 MANIFEST nor its archive nor its glyphs are served. Recorded as the expected
    result ("not installed"), not as a failure."""
    if basemap.flavor != "v1":
        pytest.skip("no MAP1 basemap to refuse: %s" % (basemap.why or "DVT_BASEMAP_FILE names a MAP2 tree"))
    bp = browser_page
    anon = web_client()
    if is_installed(anon):
        pytest.skip("a basemap is installed in this run: this case needs <state>/basemap/ absent")
    with v1_installed(state_dir, basemap.path) as manifest:
        served = [pages.BASEMAP_MANIFEST, "/basemap/" + str(manifest.get("archive", "basemap.pmtiles"))]
        assert_not_installed_surface(anon, "v1 basemap", extra_404=served)
        print("[RECORD] %s MAP1 basemap (archive %s) copied into the state dir: refused as specified (installed false, nothing served)" % (
            brlib.label(bp.engine), manifest.get("archive")))
        audit = p2.OriginAudit(bp.base).install(bp.context)
        assert_missing_page(bp, anon, audit, "v1 basemap")


# ================================================================ the map: a MAP2 basemap installed

def assert_base_surface(anon, manifest, desc):
    """The HTTP surface of an installed base (web.md MAP2c, deployment.md MAP2c), from the tester. Returns the tile path used."""
    resp = get(anon, pages.BASEMAP_TILES_JSON)
    problems = m2.descriptor_problems(desc, len(resp.body))
    assert not problems, "tiles.json descriptor: %s" % "; ".join(problems[:4])
    gen = desc["gen"]
    etag = resp.headers.get("etag", "")
    assert etag.startswith('"') and etag.endswith('"') and etag != m2.NOT_INSTALLED_ETAG, "tiles.json etag %r is not a strong ETag of the descriptor" % etag
    assert resp.headers.get("cache-control") == "no-cache", "tiles.json cache-control %r" % resp.headers.get("cache-control")
    again = get(anon, pages.BASEMAP_TILES_JSON, {"If-None-Match": etag})
    assert again.status == 304, "tiles.json with If-None-Match answered %d, want 304" % again.status
    assert desc["overview_maxzoom"] == manifest["overview_maxzoom"] and desc["maxzoom"] == manifest["detail_maxzoom"], \
        "tiles.json zooms %s/%s differ from the MANIFEST %s/%s" % (desc["overview_maxzoom"], desc["maxzoom"], manifest["overview_maxzoom"], manifest["detail_maxzoom"])
    detail = [t["bbox"] for t in manifest["tiers"] if t["role"] == "detail"]
    assert desc["coverage"]["boxes"] == detail, "coverage.boxes %s differ from the MANIFEST detail boxes %s" % (desc["coverage"]["boxes"], detail)
    # font faces and glyphs: listed files under /basemap/glyphs/. An older descriptor lists them under `files`; since MAP2f the
    # descriptor is compact and the server's allowlist is the installed MANIFEST (deployment.md, web.md), which the face urls are held to.
    faces = get(anon, FONT_FACES)
    assert faces.status == 200, "GET %s answered %d" % (FONT_FACES, faces.status)
    listed = desc["files"] if "files" in desc else manifest["files"]
    fprob = m2.font_face_problems(faces.json(), listed)
    assert not fprob, "FONT-FACES.json: %s" % "; ".join(fprob)
    if "files" not in desc:
        # the replacement route of the removed inventory: font binaries and the licence are served from the MANIFEST allowlist, a
        # glyph path the MANIFEST does not list is not
        urls = [f["url"] for f in faces.json()["NotoSansRegular"]]
        for url in dict.fromkeys([urls[0], urls[-1], "glyphs/OFL.txt"]):
            served = get(anon, BASEMAP_PREFIX + url)
            assert served.status == 200 and served.body, "GET %s%s answered %d (%d bytes), want 200 (listed in the MANIFEST)" % (
                BASEMAP_PREFIX, url, served.status, len(served.body))
        unlisted = get(anon, BASEMAP_PREFIX + "glyphs/NotoSansRegular/dvt-unlisted.pbf")
        assert unlisted.status == 404, "GET an unlisted glyph path answered %d, want 404 (the server keeps the MANIFEST allowlist)" % unlisted.status
    ranged = get(anon, GLYPH_RANGE, {"Range": "bytes=0-9"})
    assert ranged.status == 206 and len(ranged.body) == 10 and ranged.headers.get("content-range", "").startswith("bytes 0-9/"), \
        "GET %s with Range bytes=0-9 answered %d, %d bytes, content-range %r" % (GLYPH_RANGE, ranged.status, len(ranged.body), ranged.headers.get("content-range"))
    assert ranged.headers.get("accept-ranges") == "bytes", "glyph accept-ranges %r" % ranged.headers.get("accept-ranges")
    # tiles: a present one, an empty one, an unknown generation
    world = "/basemap/tiles/%s/0/0/0.mvt" % gen
    tile = get(anon, world)
    assert tile.status == 200, "GET %s answered %d, want 200 (the overview holds the z0 tile)" % (world, tile.status)
    assert tile.headers.get("content-type") == m2.MVT_TYPE and tile.headers.get("content-encoding") == "gzip", \
        "tile headers: content-type %r, content-encoding %r" % (tile.headers.get("content-type"), tile.headers.get("content-encoding"))
    assert tile.headers.get("cache-control") == m2.TILE_CACHE, "tile cache-control %r, want %r" % (tile.headers.get("cache-control"), m2.TILE_CACHE)
    assert tile.body[:2] == b"\x1f\x8b", "the z0 tile is not stored gzip"
    zoom = desc["maxzoom"]
    x, y = m2.lonlat_tile(-179.9, 80.0, zoom)       # far outside every detail box
    empty = "/basemap/tiles/%s/%d/%d/%d.mvt" % (gen, zoom, x, y)
    nothing = get(anon, empty)
    assert nothing.status == 204 and not nothing.body, "GET %s answered %d (%d bytes), want 204 with no body for an absent tile" % (empty, nothing.status, len(nothing.body))
    assert nothing.headers.get("cache-control") == m2.TILE_CACHE, "204 cache-control %r" % nothing.headers.get("cache-control")
    for path in ("/basemap/tiles/%s/0/0/0.mvt" % m2.other_gen(gen), "/basemap/tiles/%s/0/0/0.mvt" % "zz", "/basemap/tiles/%s/0/1/0.mvt" % gen):
        gone = get(anon, path)
        assert gone.status == 404 and gone.headers.get("cache-control") == NO_STORE, \
            "GET %s answered %d, cache-control %r, want 404 no-store (unknown generation or malformed path)" % (path, gone.status, gone.headers.get("cache-control"))
    # raw archives, MANIFEST and INDEX are not HTTP resources
    for path in (pages.BASEMAP_MANIFEST, "/basemap/base/MANIFEST", "/basemap/regions/INDEX",
                 *("/basemap/" + t["archive"] for t in manifest["tiers"]), *("/basemap/base/" + t["archive"] for t in manifest["tiers"])):
        other = get(anon, path)
        assert other.status == 404, "GET %s answered %d, want 404 (raw archives, MANIFEST and INDEX are not served)" % (path, other.status)
    return world


def test_positions_page_with_a_basemap(browser_page, web_client, seeds, basemap, installed_basemap):
    """With a MAP2 base installed: the descriptor and the tile, glyph and font surface are as specified (assert_base_surface), the
    status line is gone, the map container is visible with a canvas, the attribution link shows `(c) OpenStreetMap contributors`
    with the copyright URL, the table still lists the same rows, the page requested tiles.json, tiles of the descriptor's generation
    (200, 204 for an empty one) and FONT-FACES.json from its own host (and no MANIFEST, archive or INDEX), the browser requested no
    origin but the page's own (the audit context aborts a foreign request, so a stray tile server shows up as a failure), and one
    station circle per station and one object circle per object is drawn."""
    manifest, desc = installed_basemap
    bp = browser_page
    anon = web_client()
    assert_base_surface(anon, manifest, desc)
    audit = p2.OriginAudit(bp.base).install(bp.context)
    open_positions(bp)
    status, text = map_state(bp, MAP_S)
    if status != "ready":
        wanted = {pages.POSITIONS_NOTE_WEBGL, pages.POSITIONS_NOTE_UNAVAILABLE}
        web.finding("%s: the map did not become ready with a basemap installed: %s %r" % (brlib.label(bp.engine), status, text))
        assert_list_fallback(bp, anon, "map not ready")
        finish_checks(bp, audit, "map not ready")
        if text == pages.POSITIONS_NOTE_WEBGL:
            pytest.skip("%s has no WebGL2 here (%r): the list fallback was checked, the map and the markers cannot be" % (brlib.label(bp.engine), text))
        assert text in wanted, "with a basemap installed the status line says %r after %.0f s (%s)" % (text, MAP_S, status)
        pytest.fail("the map shows %r although a basemap is installed (gen %s, builds %s)" % (text, desc.get("gen"), json.dumps(desc.get("builds"))[:60]))
    expect(pages.positions_map(bp.page)).to_be_visible()
    expect(pages.positions_canvas(bp.page).first).to_be_visible()
    box = pages.positions_canvas(bp.page).first.bounding_box()
    assert box and box["width"] > 100 and box["height"] > 100, "the map canvas is %s" % box
    link = pages.positions_attribution(bp.page)
    expect(link).to_be_visible()
    expect(link).to_have_text(pages.POSITIONS_ATTRIBUTION_TEXT)
    expect(link).to_have_attribute("href", pages.POSITIONS_ATTRIBUTION_HREF)
    want = assert_list_fallback(bp, anon, "basemap installed")
    # what the page asked, all from the page's own host
    asked = audit.paths(BASEMAP_PREFIX)
    assert pages.BASEMAP_TILES_JSON in asked, "the page never asked for %s (requests under /basemap/: %s)" % (pages.BASEMAP_TILES_JSON, sorted(set(asked))[:6])
    tiles = m2.tile_requests(asked)
    assert tiles, "the map never requested a tile (requests under /basemap/: %s)" % sorted(set(asked))[:6]
    other_gens = sorted({t[0] for t in tiles} - {desc["gen"]})
    assert not other_gens, "tiles of generation(s) %s were requested, the descriptor names %s" % (other_gens, desc["gen"])
    assert FONT_FACES in asked, "the page never asked for %s (requests under /basemap/: %s)" % (FONT_FACES, sorted(set(asked))[:6])
    raw = [p for p in set(asked) if p.endswith((".pmtiles", "/MANIFEST", "/INDEX"))]
    assert not raw, "the page asked for raw archive / MANIFEST / INDEX files, which MAP2 does not serve: %s" % raw
    tile_statuses = {path: bp.log.statuses(path) for path in sorted({t[4] for t in tiles})}
    seen = {code for codes in tile_statuses.values() for code in codes}
    print("[RECORD] %s basemap statuses seen by the page log: descriptor %s, font faces %s, %d distinct tile URL(s) with %s; requests under /basemap/: %d" % (
        brlib.label(bp.engine), bp.log.statuses(pages.BASEMAP_TILES_JSON), bp.log.statuses(FONT_FACES), len(tile_statuses), sorted(seen), len(asked)))
    assert set(bp.log.statuses(pages.BASEMAP_TILES_JSON)) <= {200, 304}, bp.log.statuses(pages.BASEMAP_TILES_JSON)
    assert seen <= {200, 204, 304}, "tile answers seen by the page: %s" % {p: c for p, c in tile_statuses.items() if set(c) - {200, 204, 304}}
    finish_checks(bp, audit, "basemap installed")
    # the markers: one circle per row, counted in the pixels once the picture is steady
    want_rows = state_rows(anon)
    stations = sum(1 for r in want_rows if r["item_type"] == "position")
    objects = sum(1 for r in want_rows if r["item_type"] == "object")
    outside = [p2.row_label(r) for r in want_rows
               if not p2.inside_bbox(r["body"]["lat_e7"] / 1e7, r["body"]["lon_e7"] / 1e7, basemap.bbox, margin=0.02)]
    counts = None
    for _ in range(6):
        bp.page.wait_for_timeout(1500)
        _w, _h, pixels = p2.decode_png(pages.positions_map(bp.page).screenshot())
        now = (len(p2.blobs(pixels, p2.STATION_RGB)), len(p2.blobs(pixels, p2.OBJECT_RGB)))
        if now == counts:
            break
        counts = now
    shot = Path(os.environ.get("DVT_OUT", "out")) / "web" / "browser" / ("positions-map-%s.png" % bp.engine)
    try:
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(pages.positions_map(bp.page).screenshot())
    except OSError:
        pass
    print("[RECORD] %s markers counted in the pixels: stations %d objects %d; rows: stations %d objects %d; rows outside the coverage: %s; table rows %d" % (
        brlib.label(bp.engine), counts[0], counts[1], stations, objects, outside, len(want)))
    if outside:
        web.finding("%s: rows outside the basemap coverage %s (their markers are off the fitted view): the pixel count is recorded only" % (
            brlib.label(bp.engine), outside))
        return
    assert counts == (stations, objects), ("%s: marker circles counted in the map picture: %d station(s) and %d object(s), the public state of room %s has %d and %d "
                                           "(UNVERIFIED pixel method; picture: %s)" % (brlib.label(bp.engine), counts[0], counts[1], DOM_ROOM, stations, objects, shot))
