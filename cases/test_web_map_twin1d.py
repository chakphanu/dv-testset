# SPDX-License-Identifier: MIT
"""Web unit, TWIN1d: the Map in Thai and the polite status region, in real browsers (Playwright Chromium, Firefox and Playwright
WebKit, the fixtures of cases/browser_fixtures.py). Needs `scripts/run.sh --profile web,browser` and a USRP dynamic hub
(DVT_USRP_DYNAMIC=1); the hub-down scenario also needs the rig control channel (DVT_RIG_CONTROL=1, set by scripts/run.sh), as the
K22 cases do. Every case skips with the reason otherwise.

  test_map_thai_screenshot_matrix[<scenario>-<engine>]   decision section 3: scenario x EN/TH x light/dark x 360/1000/1440 px
  test_map_status_announces_over_start_and_end[<engine>] the polite `map-status` region announces once per over start and end

THE MATRIX. Four scenarios, one test each (12 combinations inside a test: 2 languages x 2 colour schemes x 3 widths; 48 per
engine). The page is visited once per language (the language switch reloads; the colour scheme and the viewport are changed in place
with emulate_media / set_viewport_size). For every combination: a full-page screenshot to
out/<run>/web/map-matrix/<engine>/<scenario>-<lang>-<scheme>-<width>.png (local evidence only, never committed) and the assertions
of cases/map_matrix_support.py (no horizontal overflow, no clipped caption, TH closed-desk sign text), all collected and asserted
once per scenario with the list of every failing combination.

The scenarios are implemented by traffic seeding, because the product's definition (web/README.md of the web assets) is NOT in the
image dv-router:wip-efd9eca and is therefore UNVERIFIED:
  map-busy       five rooms on air at once (USRP dynamic clients, tone) for the whole run
  map-quiet      no room on air, nothing fading
  map26          the rig's 26 rooms (the template serves A-Z) at rest plus one talker; fewer rooms is a finding, not a failure
  map-hub-down   the status hub (dv-svc-status) killed again and again through the control channel until the page shows the
                 `map-hub-down` callout; the page is reloaded once if the live indicator does not notice by itself
A closed desk is not something the rig produces: the case rewrites the answer of GET /api/v1/public/state in the browser (page
route) so that one enabled edge is missing from live.edges, which is exactly how the page decides a desk is closed. That is a UI
test input, not a statement about the server; the evidence says which protocol was closed.

THE STATUS REGION (TWIN1d, the product owner's spec for the next image): a MutationObserver injected before the page scripts records
every change of the region's textContent; one 6 s over from a USRP client is driven. Exactly one change at the start, none during
steady voice, exactly one at the end, none during the fade, none on first render, at most one per 2 s; role=status,
aria-live=polite, not display:none / visibility:hidden / aria-hidden, class sr-only. The case skips with a reason when the image
has no map-status (the matrix then records that as a finding and runs on).

UNVERIFIED and NOT RUN: see docs/web-browser-cases.md, "TWIN1d Thai screenshot matrix and map-status".
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import map_matrix_support as mms  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import browser as brlib, web  # noqa: E402
from dvtest.net import pace  # noqa: E402
from dvtest.proto import usrp  # noqa: E402
from webrtc_support import Talk, tone_frames, usrp_client  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

LOAD_S = 20.0
QUIET_S = 25.0                  # the building must have no on-air and no fading room before a scenario starts
VIEW_HEIGHT = 900
OVER_CAP_S = 100.0              # a seeded matrix over never lasts longer (well under any talk time limit)
SEED_WAIT_S = 20.0              # the seeded rooms must show data-state=onair within this long
HUB_NOTICE_S = 25.0             # the live page should notice the dead hub within this long ...
HUB_RELOAD_S = 40.0             # ... else it is reloaded once and given this long
HUB_BACK_S = 150.0              # the public state must answer 200 again within this long after the kills stop
AUTH_SESSION = "/api/v1/auth/session"
SSE_PATHS = ("/api/v1/public/events", "/api/v1/operator/stream")
BUSY_ROOMS = (("F", "N0MBF"), ("G", "N0MBG"), ("I", "N0MBI"), ("O", "N0MBO"), ("P", "N0MBP"))
MAP26_ROOMS = (("V", "N0M26"),)
STATUS_ROOM, STATUS_CALL = "I", "N0MST"
STATUS_OVER_S = 6.0
STATUS_FADE_TAIL_S = 7.5        # the fade is 5 s of the server clock; the page samples up to 1.25 s late
STATUS_IDLE_S = 3.0             # the quiet time on a loaded Map in which the region must not change (first render)
DROP_HEADERS = ("content-length", "content-encoding", "transfer-encoding", "etag", "last-modified", "cache-control")


def out_web():
    return Path(os.environ.get("DVT_OUT", "out")) / "web"


# ---------------------------------------------------------------- page helpers

def open_map(bp):
    """Opens #/map and waits for the heading, the page wrapper and the building."""
    bp.goto(pages.map_url())
    expect(pages.map_building(bp.page)).to_be_visible(timeout=int(LOAD_S * 1000))
    expect(pages.map_caption(bp.page)).to_be_visible(timeout=int(LOAD_S * 1000))


def quiet_counts(page):
    return page.evaluate("""(s) => ({ onair: Array.from(document.querySelectorAll(s.room)).filter((r) => r.getAttribute('data-state') === 'onair').length,
                                      fading: document.querySelectorAll(s.fading).length })""",
                         {"room": pages.map_matrix_selectors()["room_any"], "fading": pages.MAP_FADING_CSS})


def wait_quiet(page):
    """Until no room is on air and nothing fades (a leftover over of an earlier case would pollute the scenario)."""
    end = time.monotonic() + QUIET_S
    last = None
    while time.monotonic() < end:
        last = quiet_counts(page)
        if not last["onair"] and not last["fading"]:
            return
        time.sleep(0.5)
    pytest.fail("the Map is not quiet after %.0f s (%s): another over is still running or fading" % (QUIET_S, last))


def wait_onair(page, want):
    end = time.monotonic() + SEED_WAIT_S
    last = None
    while time.monotonic() < end:
        last = quiet_counts(page)
        if last["onair"] >= want:
            return last["onair"]
        time.sleep(0.4)
    pytest.fail("seeding failed: %s rooms show data-state=onair after %.0f s, wanted %d" % (last and last["onair"], SEED_WAIT_S, want))


def settle(page):
    """After a viewport change the page re-lays the building out behind a resize observer: wait until the SVG viewBox and its
    rendered width stop changing (two equal reads 200 ms apart, at most 3 s). Returns the last 'viewBox|width' text."""
    sel = pages.map_matrix_selectors()["building"]
    page.wait_for_timeout(250)
    last, stable, end = None, 0, time.monotonic() + 3.0
    while time.monotonic() < end:
        now = page.evaluate("(s) => { const e = document.querySelector(s); return e ? e.getAttribute('viewBox') + '|' + Math.round(e.getBoundingClientRect().width) : null; }", sel)
        stable = stable + 1 if (now is not None and now == last) else 0
        last = now
        if stable >= 2:
            break
        page.wait_for_timeout(200)
    return last


def set_language(bp, lang):
    """The language switch of the page (testid language-en / language-th; it stores dv-lang and reloads), or, on an image
    without the hook, the storage key and a reload. Returns 'switch' or 'storage'. Waits for the building in the new language."""
    page = bp.page
    button = pages.language_toggle(page, lang)
    via = "switch"
    if button.count():
        if button.get_attribute("aria-pressed") != "true":
            with page.expect_navigation(timeout=20000):
                button.click()
    else:
        via = "storage"
        page.evaluate("(a) => { try { localStorage.setItem(a[0], a[1]); } catch (e) { /* denied */ } }", [pages.LANG_STORAGE_KEY, lang])
        page.reload()
    expect(pages.map_building(page)).to_be_visible(timeout=int(LOAD_S * 1000))
    page.wait_for_function("(l) => document.documentElement.lang === l", arg=lang, timeout=int(LOAD_S * 1000))
    return via


# ---------------------------------------------------------------- seeded traffic

class OverLoop(threading.Thread):
    """One USRP client keying a tone into its module until stop() (or OVER_CAP_S), then the unkey: a room that stays on air for a
    whole matrix scenario. The caller unlinks and closes the peer."""

    def __init__(self, peer):
        super().__init__(daemon=True)
        self.peer = peer
        self.frames = tone_frames(2.0)
        self.halt = threading.Event()
        self.error = None

    def stop(self):
        self.halt.set()

    def run(self):
        try:
            start, index = time.monotonic(), 0
            while not self.halt.is_set() and time.monotonic() - start < OVER_CAP_S:
                pace(start, index, 0.020)
                self.peer.send(usrp.voice, self.frames[index % len(self.frames)])
                index += 1
            self.peer.send(usrp.unkey)
        except Exception as exc:                    # noqa: BLE001 - reported by the case
            self.error = exc


class HubKiller(threading.Thread):
    """SIGKILL of dv-svc-status again and again (the launcher respawns it within 0.5 to 5 s; a respawn is killed before it can
    publish), as kill_hub_for() of cases/test_web_k22.py does."""

    def __init__(self, rig_control):
        super().__init__(daemon=True)
        self.rig = rig_control
        self.halt = threading.Event()
        self.kills = 0

    def stop(self):
        self.halt.set()

    def run(self):
        while not self.halt.is_set():
            try:
                self.rig.kill_proc("xlxd-sut", "dv-svc-status", "KILL")
                self.kills += 1
            except (RuntimeError, TimeoutError, ValueError):
                pass                                # error 3: not running right now (between respawns)
            self.halt.wait(0.3)


def wait_state_back(anon):
    """True once the public state answers 200 again."""
    end = time.monotonic() + HUB_BACK_S
    while time.monotonic() < end:
        try:
            if anon.get("/api/v1/public/state", pace=False).status == 200:
                return True
        except web.WebError:
            pass
        time.sleep(2.0)
    return False


class ClosedDesk:
    """A page route on GET /api/v1/public/state that removes one enabled edge from live.edges (mms.close_one_edge), so the Map draws
    that desk closed. `protocol` is the edge it closed (None until an answer was rewritten), `rewrites` how many answers."""

    def __init__(self):
        self.protocol = None
        self.rewrites = 0
        self.passed = 0

    def handler(self, route):
        try:
            resp = route.fetch()
            body = resp.body()
        except Exception:                           # noqa: BLE001 - a dead hub or a closing page: let the request through
            try:
                route.continue_()
            except Exception:                       # noqa: BLE001
                pass
            return
        if resp.status == 200:
            try:
                state, pick = mms.close_one_edge(json.loads(body.decode("utf-8")), self.protocol)
            except ValueError:
                state, pick = None, None
            if pick is not None:
                self.protocol = pick
                self.rewrites += 1
                headers = {k: v for k, v in resp.headers.items() if k.lower() not in DROP_HEADERS}
                headers["cache-control"] = "no-store"
                route.fulfill(status=200, headers=headers, body=json.dumps(state).encode("utf-8"))
                return
        self.passed += 1
        route.fulfill(response=resp, body=body)

    def install(self, context):
        context.route(re.compile(r".*/api/v1/public/state(\?.*)?$"), self.handler)


# ---------------------------------------------------------------- the matrix

def scenario_check(scenario, m):
    """Failures of the scenario's own precondition (the seeding worked), judged on every combination's measurement."""
    if scenario == "map-busy" and m.get("onair_rooms", 0) < 3:
        return ["map-busy: only %s room(s) on air, wanted at least 3" % m.get("onair_rooms")]
    if scenario == "map-quiet" and m.get("onair_rooms", 0):
        return ["map-quiet: %s room(s) on air" % m.get("onair_rooms")]
    if scenario == "map-hub-down" and not m.get("hub_down_callout"):
        return ["map-hub-down: the hub-down callout is not on the page"]
    return []


def measure(page):
    return page.evaluate(mms.MEASURE_JS, pages.map_matrix_selectors())


def run_combo(bp, scenario, lang, scheme, width, closed, shots, record):
    """One combination: viewport and scheme in place, settle, measure, judge, screenshot. Returns the failures."""
    page = bp.page
    page.set_viewport_size({"width": width, "height": VIEW_HEIGHT})
    layout = settle(page)
    page.wait_for_timeout(150)
    m = measure(page)
    failures, notes = mms.judge_measure(m, lang, scheme, closed_required=closed.protocol is not None and scenario != "map-hub-down")
    failures += scenario_check(scenario, m)
    name = mms.shot_name(scenario, lang, scheme, width)
    shot = None
    try:
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / name), full_page=True)
        shot = name
    except Exception as exc:                        # noqa: BLE001 - evidence only; a failed screenshot is a note
        notes.append("screenshot failed: %s" % str(exc).splitlines()[0][:120])
    record.append({"combo": mms.combo_id(scenario, lang, scheme, width), "layout": layout, "mode": m.get("mode"), "rooms": m.get("rooms"),
                   "desks": m.get("desks"), "closed_desks": m.get("closed_desks"), "closed_texts": m.get("closed_texts"),
                   "onair_rooms": m.get("onair_rooms"), "status_present": m.get("status_present"), "items": len(m.get("items") or []),
                   "svg_texts": len(m.get("svg_texts") or []), "doc_scroll": m.get("doc_scroll"), "doc_client": m.get("doc_client"),
                   "screenshot": shot, "failures": failures, "notes": notes})
    return ["%s: %s" % (mms.combo_id(scenario, lang, scheme, width), f) for f in failures]


def hub_down_enter(bp, killer):
    """Starts the kills and waits for the callout (the live indicator, else one reload). Returns how it was noticed."""
    page = bp.page
    killer.start()
    try:
        pages.map_hub_down(page).wait_for(state="visible", timeout=int(HUB_NOTICE_S * 1000))
        return "live"
    except Exception:                               # noqa: BLE001 - not noticed by the live page: reload once
        pass
    page.reload()
    try:
        pages.map_hub_down(page).wait_for(state="visible", timeout=int(HUB_RELOAD_S * 1000))
        return "reload"
    except Exception:                               # noqa: BLE001
        return None


def start_seed(scenario, bp, target, loops, peers):
    """Fills `loops` (OverLoop threads, started) and `peers` (USRP clients) for the scenario, so the caller can always clean up;
    the on-air rooms are confirmed on the page."""
    rooms = BUSY_ROOMS if scenario == "map-busy" else MAP26_ROOMS if scenario == "map26" else ()
    for room, call in rooms:
        peers.append(usrp_client(target, call, room))
    for peer in peers:
        loop = OverLoop(peer)
        loops.append(loop)
        loop.start()
    if rooms:
        wait_onair(bp.page, 3 if scenario == "map-busy" else 1)


def stop_seed(loops, peers):
    for loop in loops:
        loop.stop()
    for loop in loops:
        loop.join(timeout=5)
    for peer in peers:
        try:
            peer.unlink()
        except Exception:                           # noqa: BLE001 - leaving the room is best effort
            pass
        peer.close()


@pytest.fixture(params=mms.SCENARIOS)
def scenario(request):
    return request.param


def test_map_thai_screenshot_matrix(scenario, browser_page, target, web_client, request):
    """scenario x EN/TH x light/dark x 360/1000/1440 px on #/map, 12 combinations per scenario and engine (48 per engine over the
    four scenarios). Per combination a full-page screenshot and the checks of map_matrix_support.judge_measure; every failing
    combination is listed in one assertion at the end. The status region missing on the page is a finding (older images)."""
    bp = browser_page
    anon = web_client()
    rig = request.getfixturevalue("rig_control") if scenario == "map-hub-down" else None
    shots = out_web() / "map-matrix" / bp.engine
    closed = ClosedDesk()
    closed.install(bp.context)
    record, failures, notes = [], [], []
    loops, peers, killer = [], [], None
    hub_how = {}
    bp.page.emulate_media(color_scheme="light")
    bp.page.set_viewport_size({"width": mms.WIDTHS[0], "height": VIEW_HEIGHT})
    try:
        open_map(bp)
        if scenario != "map-hub-down":
            wait_quiet(bp.page)
        start_seed(scenario, bp, target, loops, peers)
        for lang in mms.LANGS:
            via = set_language(bp, lang)
            notes.append("language %s set by the %s" % (lang, via))
            if scenario == "map26":
                n_rooms = measure(bp.page).get("rooms")
                if n_rooms != 26:
                    web.finding("TWIN1d map26: the Map draws %s rooms, not 26: the scenario ran with what the rig serves (UNVERIFIED meaning)" % n_rooms)
            if scenario == "map-hub-down":
                killer = HubKiller(rig)
                hub_how[lang] = hub_down_enter(bp, killer)
                if hub_how[lang] is None:
                    failures.append("map-hub-down/%s: the page never showed the map-hub-down callout in %.0f s of hub kills (%d kills) and one reload: "
                                    "the hub outage is not visible on the Map" % (lang, HUB_NOTICE_S + HUB_RELOAD_S, killer.kills))
            if scenario != "map-hub-down" or hub_how.get(lang):
                for scheme in mms.SCHEMES:
                    bp.page.emulate_media(color_scheme=scheme)
                    for width in mms.WIDTHS:
                        failures += run_combo(bp, scenario, lang, scheme, width, closed, shots, record)
            if killer is not None:
                killer.stop()
                killer.join(timeout=10)
                notes.append("hub-down %s: noticed %s, %d kills" % (lang, hub_how.get(lang), killer.kills))
                if not wait_state_back(anon):
                    failures.append("map-hub-down/%s: the public state did not answer 200 within %.0f s after the kills stopped" % (lang, HUB_BACK_S))
                    break
                killer = None
    finally:
        if killer is not None:
            killer.stop()
            killer.join(timeout=10)
            wait_state_back(anon)
        stop_seed(loops, peers)
    if scenario != "map-hub-down" and closed.protocol is None:
        notes.append("no edge could be closed in the public state (no enabled edge in live.edges): the closed sign was not checked")
    if record and not any(r.get("status_present") for r in record):
        web.finding("%s: TWIN1d: no data-testid=map-status on #/map in any combination (%s): the image has no polite status region" % (
            brlib.label(bp.engine), scenario))
    for loop in loops:
        if loop.error is not None:
            failures.append("%s: a seeding client failed: %s" % (scenario, loop.error))
    path = out_web() / ("map-matrix-%s-%s.json" % (bp.engine, scenario))
    doc = {"engine": bp.engine, "label": brlib.label(bp.engine), "scenario": scenario, "closed_protocol": closed.protocol,
           "closed_rewrites": closed.rewrites, "route_passed": closed.passed, "hub_notice": hub_how, "notes": notes,
           "combos": record, "failures": failures}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print("[RECORD] map-matrix: cannot write %s: %s" % (path, exc))
    print("[RECORD] map-matrix %s %s: %d of %d combinations judged, %d failures, closed desk %s -> %s" % (
        brlib.label(bp.engine), scenario, len(record), len(mms.all_combos(scenario)), len(failures), closed.protocol, path))
    for problem in bp.problems({(AUTH_SESSION, 401)} | {(p, 429) for p in SSE_PATHS}):
        web.finding("%s: map matrix %s: %s" % (brlib.label(bp.engine), scenario, problem))
    if scenario != "map-hub-down":
        assert not bp.log.errors, "%s: page error: %s" % (brlib.label(bp.engine), bp.log.errors[:1])
    elif bp.log.errors:
        web.finding("%s: map-hub-down: page error while the hub was down: %s" % (brlib.label(bp.engine), bp.log.errors[:1]))
    assert not failures, "%s: %s: %d failure(s) in %d combination(s) (evidence %s, screenshots %s):\n  %s" % (
        brlib.label(bp.engine), scenario, len(failures), len({f.split(":")[0] for f in failures}), path, shots, "\n  ".join(failures))


# ---------------------------------------------------------------- the status region

def test_map_status_announces_over_start_and_end(browser_page, target):
    """One 6 s over from a USRP client in room I, watched by a MutationObserver injected before the page scripts: the region of
    data-testid=map-status changes exactly once at the start and exactly once at the end (judge_status_log), not on first
    render, not during steady voice, not during the fade, never twice within 2 s; and it is role=status, aria-live=polite, in the
    accessibility tree (not display:none / visibility:hidden / aria-hidden) and sr-only, before, during and after the over."""
    bp = browser_page
    page = bp.page
    sel = pages.map_matrix_selectors()["status"]
    bp.context.add_init_script(script="(%s)(%s);" % (mms.STATUS_INIT_JS, json.dumps(sel)))
    open_map(bp)
    page.wait_for_timeout(1500)
    if not pages.map_status(page).count():
        pytest.skip("the image has no data-testid=%s on #/map (before TWIN1d); the screenshot matrix records it as a finding" % pages.MAP_STATUS_TESTID)
    failures, notes = [], []
    facts = {"before": page.evaluate(mms.STATUS_FACTS_JS, sel)}
    failures += ["before the over: " + f for f in mms.judge_status_facts(facts["before"])]
    wait_quiet(page)
    page.wait_for_timeout(int(STATUS_IDLE_S * 1000))
    clock_off = page.evaluate("() => Date.now()") - time.time() * 1000.0           # browser clock minus tester clock (same host on the rig)
    peer = usrp_client(target, STATUS_CALL, STATUS_ROOM)
    try:
        talk = Talk(peer, tone_frames(STATUS_OVER_S))
        t_start = time.time() * 1000.0
        talk.start()
        time.sleep(STATUS_OVER_S / 2.0)
        facts["during"] = page.evaluate(mms.STATUS_FACTS_JS, sel)
        talk.join(timeout=STATUS_OVER_S + 5.0)
        t_unkey = t_start + STATUS_OVER_S * 1000.0
        time.sleep(max(0.0, (t_unkey + STATUS_FADE_TAIL_S * 1000.0 - time.time() * 1000.0) / 1000.0))
        facts["after"] = page.evaluate(mms.STATUS_FACTS_JS, sel)
        t_end = time.time() * 1000.0
        log = page.evaluate("() => window.__dvtStatus || null")
    finally:
        try:
            peer.unlink()
        except Exception:                           # noqa: BLE001
            pass
        peer.close()
    if talk.error is not None:
        failures.append("the USRP talker failed: %s" % talk.error)
    if log is None:
        failures.append("the recorder was not installed in the page (no window.__dvtStatus)")
        log = {}
    for phase in ("during", "after"):
        failures += ["%s the over: %s" % (phase, f) for f in mms.judge_status_facts(facts[phase])]
    got, got_notes, metrics = mms.judge_status_log(log, t_start + clock_off, t_unkey + clock_off, t_end + clock_off)
    failures += got
    notes += got_notes
    path = out_web() / ("map-status-%s.json" % bp.engine)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"engine": bp.engine, "label": brlib.label(bp.engine), "room": STATUS_ROOM, "call": STATUS_CALL, "facts": facts,
                                    "metrics": metrics, "notes": notes, "failures": failures, "clock_offset_ms": round(clock_off, 1)},
                                   indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print("[RECORD] map-status: cannot write %s: %s" % (path, exc))
    print("[RECORD] map-status %s: changes %s, notes %d, failures %d -> %s" % (
        brlib.label(bp.engine), json.dumps(metrics.get("changes")), len(notes), len(failures), path))
    for note in notes:
        web.finding("%s: map-status: %s" % (brlib.label(bp.engine), note))
    assert not bp.log.errors, "%s: page error: %s" % (brlib.label(bp.engine), bp.log.errors[:1])
    assert not failures, "%s: the map-status region differs from the TWIN1d spec: %s (evidence: %s)" % (brlib.label(bp.engine), "; ".join(failures), path)
