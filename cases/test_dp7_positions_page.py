# SPDX-License-Identifier: MIT
"""Web unit, DP7 / MAP1: the public `#/positions` page in real browsers (Playwright Chromium, Firefox and Playwright WebKit, the
fixtures of cases/browser_fixtures.py). Needs `scripts/run.sh --profile web,browser` with the data worker on
(data-ident.toml); every case skips with the reason otherwise. Page knowledge (route, test ids, texts) is in cases/web_pages.py
("#/positions"); helpers: cases/position2_support.py; case table, run commands, UNVERIFIED shapes: docs/dp7-cases.md ("DP7 second
set"). NOT RUN on a rig yet: written against web/site/positions.js, positions-map.js, public.js and deployment.md (MAP1).

  test_positions_page_without_a_basemap   `<state>/basemap/` ABSENT: the coordinate table is the list fallback (one row per public
                                          position / object of the room, coordinates printed at the sent precision), the status
                                          line says `Basemap not installed.`, the map container is hidden and there is no
                                          attribution, GET /basemap/MANIFEST is 404, and the page asks ONLY its own origin
  test_positions_page_with_a_basemap      a basemap provided by the lead (env DVT_BASEMAP_FILE; skips without it) is installed into
                                          the run's state dir; the map is shown (canvas, no note, attribution link), the table is
                                          still there with the same rows, one marker circle per station / object is drawn (counted
                                          from the pixels: markers are WebGL, not DOM), and the set of origins the browser
                                          requested is exactly the page's own (foreign requests are aborted and counted)

The basemap cannot be built here: scripts/basemap-build.py needs the internet (go-pmtiles from GitHub, ranges of the Protomaps
build, glyph files). DVT_BASEMAP_FILE is what the lead supplies: the OUTPUT DIRECTORY of that script (MANIFEST, the dated
.pmtiles archive, glyphs/), a .tar / .tar.gz of it, or a bare `.pmtiles` archive (a MANIFEST is then made). It is a path under the
repository as the tester container sees it (relative to the repository root, e.g. `.local/basemap-20261106-z8.pmtiles`; the
container mounts the repository at /dvt). The cases copy it to out/<run>/sut-state/basemap/ (staging directory, one rename, as
deployment.md says) and remove only that directory afterwards.

Markers: positions-map.js draws the stations (circle colour #146cad) and objects (#b36b00) into a MapLibre GeoJSON layer; the page
exposes no marker element and no map handle. The case counts the connected blobs of those two colours in a screenshot of the map
container (UNVERIFIED method; antialiasing, label overlap and a WebGL-less engine are the failure modes). The seeds are spread over
the basemap's bounding box so that no two circles touch. An engine without WebGL2 shows `The map needs WebGL2...`; the case then checks the
list and skips the pixel part with that reason.
"""
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
MAP_S = 60.0                                     # tiles from the archive: seconds, not minutes
ARCHIVE_PATH = re.compile(r"^/basemap/basemap-[0-9]{8}-z(?:0?[0-9]|1[0-5])\.pmtiles$")


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def basemap():
    """The basemap the lead provided (DVT_BASEMAP_FILE) or None: (path, kind, bbox of the map's coverage)."""
    value = os.environ.get("DVT_BASEMAP_FILE", "").strip()
    if not value:
        return types.SimpleNamespace(path=None, kind=None, bbox=list(p2.DEFAULT_BBOX), why="DVT_BASEMAP_FILE is not set")
    path = Path(value)
    path = path if path.is_absolute() else ROOT / path
    kind = p2.basemap_kind(path)
    if kind is None:
        return types.SimpleNamespace(path=None, kind=None, bbox=list(p2.DEFAULT_BBOX),
                                     why="DVT_BASEMAP_FILE=%s is not visible in the tester as a basemap directory (with MANIFEST), a tar of one or a "
                                         ".pmtiles archive (the container mounts the repository at /dvt: use a path under the repository)" % value)
    return types.SimpleNamespace(path=path, kind=kind, bbox=p2.basemap_bbox(path), why="")


def send_position(rig, call, info):
    """One APRS payload as an M17 packet-mode report from a client linked to the DOM room (module C)."""
    base = ps.base_call(call)
    packet = m17.m17p_packet(0x02, ps.tnc2(call, info).encode("ascii"), src=call)
    with ds.m17_client(rig.xlxd_sut, callsign=base, module=DOM_ROOM) as sender:
        sender.client.send(sender.dest, packet)
        ds.pump_for(1.0, sender)


@pytest.fixture(scope="module")
def seeds(rig, web_client, basemap):
    """Three stations and one object in the DOM room, spread over the basemap's bounding box (default: Thailand), public in the
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
def installed_basemap(request, target, basemap, web_client):
    """Installs the provided basemap into out/<run>/sut-state/basemap/ (never over an existing one) and removes it at the end.
    Waits until the public host serves its MANIFEST; when it does not within 30 s the web service is restarted through the
    control channel (when there is one): UNVERIFIED whether dv-svc-web notices a basemap/ that appears while it runs."""
    if basemap.path is None:
        pytest.skip("no basemap to install: %s (docs/dp7-cases.md: build one with scripts/basemap-build.py on a machine with network "
                    "access and name it in DVT_BASEMAP_FILE)" % basemap.why)
    state = Path(target.out) / "sut-state"
    if not state.is_dir():
        pytest.skip("the SUT state directory %s is not visible in the tester (the web profile mounts out/<run>/sut-state)" % state)
    anon = web_client()
    if anon.get(pages.BASEMAP_MANIFEST, pace=False).status == 200:
        pytest.skip("a basemap is already served by this run: the cases do not replace an installed one")
    try:
        manifest = p2.install_basemap(state, basemap.path)
    except (ValueError, OSError) as exc:
        pytest.fail("cannot install the provided basemap %s: %s" % (basemap.path, exc))
    try:
        def served(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if anon.get(pages.BASEMAP_MANIFEST, pace=False).status == 200:
                    return True
                time.sleep(1.0)
            return False
        if not served(30.0):
            try:
                control = request.getfixturevalue("rig_control")
            except pytest.skip.Exception:
                pytest.fail("GET %s is still not 200 30 s after the basemap was installed and there is no control channel to restart "
                            "dv-svc-web (DVT_RIG_CONTROL=1): deployment.md installs it 'while web is stopped'" % pages.BASEMAP_MANIFEST)
            web.finding("dv-svc-web did not serve a basemap/ installed while it runs within 30 s: restarted it through the control channel")
            control.kill_proc("xlxd-sut", "dv-svc-web", "KILL")
            time.sleep(3.0)
            if not served(150.0):
                pytest.fail("GET %s is not 200 after the basemap was installed and dv-svc-web restarted" % pages.BASEMAP_MANIFEST)
        yield manifest
    finally:
        p2.remove_basemap(state)


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


# ================================================================ the list fallback: no basemap installed

def test_positions_page_without_a_basemap(browser_page, web_client, seeds):
    """web/README.md "Position map and position privacy", deployment.md (MAP1): without installed data the coordinate list stays and the
    page shows a short `Basemap not installed.` note. With `<state>/basemap/` ABSENT: GET /basemap/MANIFEST is 404, the map
    container is hidden (class `hidden`), no attribution link is shown, the table lists every public position and object of the room
    at the sent precision, and the browser asks no origin but the page's own."""
    bp = browser_page
    anon = web_client()
    if anon.get(pages.BASEMAP_MANIFEST, pace=False).status == 200:
        pytest.skip("a basemap is installed in this run: this case needs <state>/basemap/ absent")
    audit = p2.OriginAudit(bp.base).install(bp.context)
    open_positions(bp)
    expect(pages.positions_map_note(bp.page)).to_have_text(pages.POSITIONS_NOTE_MISSING, timeout=int(LOAD_S * 1000))
    expect(pages.positions_map(bp.page)).to_have_class(re.compile(r"\bhidden\b"))
    expect(pages.positions_map(bp.page)).to_be_hidden()
    expect(pages.positions_attribution(bp.page)).to_have_count(0)
    assert_list_fallback(bp, anon, "no basemap")
    assert bp.log.statuses(pages.BASEMAP_MANIFEST)[:1] == [404], "GET %s answered %s" % (pages.BASEMAP_MANIFEST, bp.log.statuses(pages.BASEMAP_MANIFEST))
    assert not audit.paths("/basemap/basemap-"), "the page asked for a basemap archive without a manifest: %s" % audit.paths("/basemap/")
    finish_checks(bp, audit, "no basemap", expected_requests=[(pages.BASEMAP_MANIFEST, 404)])


# ================================================================ the map: a basemap installed

def test_positions_page_with_a_basemap(browser_page, web_client, seeds, basemap, installed_basemap):
    """With the basemap installed: the status line is gone, the map container is visible with a canvas, the attribution link shows
    `(c) OpenStreetMap contributors` with the copyright URL, the table still lists the same rows (the list fallback does not
    disappear), one station circle per station and one object circle per object is drawn, MANIFEST and the archive are served by the page's
    own host (archive by Range requests: 206 expected), and the browser requested no origin but the page's own (the audit context
    aborts a foreign request, so a stray tile server shows up as a failure, not as a network call)."""
    bp = browser_page
    anon = web_client()
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
        pytest.fail("the map shows %r although a basemap is installed (UNVERIFIED install: manifest %s)" % (text, json.dumps(installed_basemap)[:120]))
    expect(pages.positions_map(bp.page)).to_be_visible()
    expect(pages.positions_canvas(bp.page).first).to_be_visible()
    box = pages.positions_canvas(bp.page).first.bounding_box()
    assert box and box["width"] > 100 and box["height"] > 100, "the map canvas is %s" % box
    link = pages.positions_attribution(bp.page)
    expect(link).to_be_visible()
    expect(link).to_have_text(pages.POSITIONS_ATTRIBUTION_TEXT)
    expect(link).to_have_attribute("href", pages.POSITIONS_ATTRIBUTION_HREF)
    want = assert_list_fallback(bp, anon, "basemap installed")
    # the files came from the page's own host
    assert audit.paths("/basemap/MANIFEST"), "the page never asked for %s" % pages.BASEMAP_MANIFEST
    archives = [p for p in audit.paths("/basemap/") if ARCHIVE_PATH.match(p)]
    assert archives, "the map never requested the archive (requests under /basemap/: %s)" % sorted(set(audit.paths("/basemap/")))[:6]
    statuses = {path: bp.log.statuses(path) for path in set(archives)}
    print("[RECORD] %s basemap statuses seen by the page log: manifest %s, archive %s; requests under /basemap/: %d" % (
        brlib.label(bp.engine), bp.log.statuses(pages.BASEMAP_MANIFEST), statuses, len(audit.paths("/basemap/"))))
    assert set(bp.log.statuses(pages.BASEMAP_MANIFEST)) <= {200}, bp.log.statuses(pages.BASEMAP_MANIFEST)
    for path, codes in statuses.items():
        assert set(codes) <= {200, 206, 304}, "%s answered %s" % (path, codes)
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
