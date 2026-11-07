# SPDX-License-Identifier: MIT
"""Web unit, TWIN2b: the Map room view fades a finished over, in real browsers (Playwright Chromium, Firefox and Playwright WebKit,
the fixtures of cases/browser_fixtures.py). Needs `scripts/run.sh --profile web,browser` and a USRP dynamic hub
(DVT_USRP_DYNAMIC=1): the protocol talker is a USRP dynamic client (PCM straight into the room, no transcoder), the simplest voice
source that puts a talker callsign into rooms[].onair of the public state. Every case skips with the reason otherwise.

The product owner's spec (docs/web-browser-cases.md, "TWIN2b map fade"), one over and its end on #/map, room N:

  t ~ 0, during the over     the room tile has the on-air look and the talker (caption, tile text)
  after the end, 5 s         the room fades from rooms[].last.end_ms (the public state), the caption says "<call> finished"
  ~ 2.5 s after the end      a fade step: a lower-opacity class and/or a computed opacity strictly between full and rest
  >= 6.5 s after the end     at rest (the page samples up to 1.25 s late)
  a new over in the fade     the full on-air look at once (within one sampling step)
  prefers-reduced-motion     a static dimmed state, computed transition-duration 0s

  test_map_room_fades_after_an_over                       the whole timeline of one 3 s over, sampled every 250 ms
  test_map_new_over_during_the_fade_restores_the_look     over, end, a second over at fade step >= 1: restored at once; then its own fade
  test_map_fade_under_reduced_motion                      emulate_media(reduced_motion='reduce'): static dimmed state, no transition

Evidence: out/<run>/web/map-fade-<engine>-<case>.json (every sample: t since end_ms, state, class list, computed fill, art opacity,
transition-duration, caption, the judges' metrics) and [RECORD] lines on stdout. The judges are pure functions
(cases/map_fade_support.py, unit tests cases/unit/test_map_fade_support.py); selectors are in cases/web_pages.py ("#/map").

UNVERIFIED and NOT RUN: every selector, class name, caption text and the timing model were read from the page SOURCES of
dv-router:wip-1bf347c (web/site/map.js, map-layout.js, map-building.js, shared/dv.css), never from a running page; the list is in
docs/web-browser-cases.md ("TWIN2b map fade"). Assertions use computed style first and the class names second.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import map_fade_support as msf  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import browser as brlib, web  # noqa: E402
from webrtc_support import Talk, tone_frames, usrp_client  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

ROOM = "N"                      # an own module: no other browser case talks in it (H J K L Q W R X Y Z M S T U and B C are taken)
CALL = "N0FAD"                  # the protocol talker
OVER_S = 3.0                    # the over of the timeline case
OVER2_S = 3.0                   # the second over of the restart case
LOAD_S = 20.0
QUIET_S = 25.0                  # the building must have no on-air and no fading room before an over starts
CAP_S = 40.0                    # hard cap of one sampling loop
AUTH_SESSION = "/api/v1/auth/session"
SSE_PATHS = ("/api/v1/public/events", "/api/v1/operator/stream")


# ---------------------------------------------------------------- page helpers

def open_map(bp):
    """Opens #/map and waits for the building, the room tile and the live indicator. Returns True when the stream is live."""
    bp.goto(pages.map_url())
    expect(pages.heading(bp.page)).to_have_text(pages.MAP_TITLE, timeout=int(LOAD_S * 1000))
    expect(pages.map_page(bp.page)).to_be_visible(timeout=int(LOAD_S * 1000))
    expect(pages.map_building(bp.page)).to_be_visible(timeout=int(LOAD_S * 1000))
    expect(pages.map_room(bp.page, ROOM)).to_be_attached(timeout=int(LOAD_S * 1000))
    try:
        expect(pages.connection_state(bp.page)).to_have_attribute("data-state", "connected", timeout=int(LOAD_S * 1000))
        return True
    except AssertionError:
        web.finding("%s: the live-update indicator never said connected on #/map (data-state %s): the fade cases go on with the polling page" % (
            brlib.label(bp.engine), pages.dashboard_state(bp.page)))
        return False


def wait_quiet(bp, sel):
    """Samples until no room is on air and nothing fades (a leftover over of an earlier case would pollute the series)."""
    end = time.monotonic() + QUIET_S
    last = None
    while time.monotonic() < end:
        last = msf.take(bp.page, sel)
        if last.get("found") and not last.get("onair_rooms") and not last.get("fading_n"):
            return True
        time.sleep(msf.SAMPLE_S)
    pytest.fail("the Map is not quiet after %.0f s (on-air rooms %s, fading elements %s): another over is still running or fading" % (
        QUIET_S, last and last.get("onair_rooms"), last and last.get("fading_n")))


def drive(bp, sel, anon, ctx, series, hook=None):
    """Samples every SAMPLE_S into `series` until TAIL_S after the end of the over of ctx['talk'] (rooms[].last.end_ms of the public
    state; read once a second after the talker thread is done). `hook(sample)` runs after each sample. Returns (end_ms, skew_ms, entry)."""
    t_begin = time.monotonic()
    tick = t_begin
    end_ms, skew, entry, next_poll = None, 0.0, {}, 0.0
    while True:
        sample = msf.take(bp.page, sel)
        series.append(sample)
        if hook:
            hook(sample)
        now = time.monotonic()
        talk = ctx.get("talk")
        if end_ms is None and talk is not None and not talk.is_alive() and now >= next_poll:
            end_ms, skew, entry = msf.read_end(anon, ROOM, ctx["since_ms"])
            next_poll = now + 1.0
        if end_ms is not None and (sample["wall_ms"] - skew - end_ms) / 1000.0 >= msf.TAIL_S:
            return end_ms, skew, entry
        if now - t_begin > CAP_S:
            return end_ms, skew, entry
        tick += msf.SAMPLE_S
        delay = tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            tick = time.monotonic()


def evidence_path(bp, name):
    return Path(os.environ.get("DVT_OUT", "out")) / "web" / ("map-fade-%s-%s.json" % (bp.engine, name))


def write_evidence(bp, name, doc, series):
    """The whole series and the judges' metrics as JSON, plus a compact table line per sample on stdout."""
    path = evidence_path(bp, name)
    doc = dict(doc, engine=bp.engine, label=brlib.label(bp.engine), room=ROOM, call=CALL, series=series)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print("[RECORD] map-fade: cannot write %s: %s" % (path, exc))
    for s in series:
        if "t" not in s:
            continue
        print("[RECORD] map-fade-series %s %s t=%.2f state=%s step=%s art_opacity=%s fill=%s caption=%r" % (
            bp.engine, name, s["t"], s.get("state"), msf.step_of(s), s.get("art_min"), s.get("fill"), (s.get("caption") or "")[:60]))
    return path


def finish_checks(bp, label):
    """No page error; console / request problems are findings (this tests someone else's product)."""
    assert not bp.log.errors, "%s: page error: %s" % (label, bp.log.errors[:1])
    for problem in bp.problems({(AUTH_SESSION, 401)} | {(path, 429) for path in SSE_PATHS}):
        web.finding("%s: %s: %s" % (brlib.label(bp.engine), label, problem))


def public_talker(anon):
    """rooms[ROOM].onair of the public state (the talker the page is told about), or None."""
    state = anon.get("/api/v1/public/state", pace=False, expect=200).json()
    return (msf.room_entry(state, ROOM) or {}).get("onair")


def talker_failures(onair):
    if not onair:
        return ["the public state has no rooms[%s].onair while the over runs" % ROOM]
    seen = str(onair.get("callsign_base") or onair.get("callsign") or "").upper()
    if not seen.startswith(CALL):
        return ["rooms[%s].onair names %r, the talker is %s" % (ROOM, onair.get("callsign_base") or onair.get("callsign"), CALL)]
    return []


def compact(metrics):
    keep = ("over_samples", "fade_first_t", "fade_first_step", "fade_last_t", "fade_last_step", "steps_seen", "mid", "rest_first_t", "rest_samples",
            "restored_u", "art_levels", "lit_rest_distance")
    return json.dumps({k: metrics[k] for k in keep if k in metrics}, sort_keys=True, default=str)


# ================================================================ the timeline of one over

def test_map_room_fades_after_an_over(browser_page, target, web_client):
    """One 3 s over from a USRP client in room N, then 8.5 s of sampling after rooms[].last.end_ms. Judged by
    map_fade_support.judge_fade: on-air look and talker during the over, a fade that starts within the documented lag and says
    '<call> finished', a fade step (class and/or computed opacity or fill strictly between) at ~2.5 s, never going back, and rest at
    >= 6.5 s with 'No one is on air.'. The public state must name the talker while it talks."""
    bp = browser_page
    anon = web_client()
    sel = pages.map_sampler_selectors(ROOM)
    peer = usrp_client(target, CALL, ROOM)
    series, failures, onair_seen = [], [], []
    try:
        live = open_map(bp)
        wait_quiet(bp, sel)
        since_ms = time.time() * 1000.0 - 500.0
        talk = Talk(peer, tone_frames(OVER_S))
        ctx = {"talk": talk, "since_ms": since_ms}
        talk.start()
        started = time.monotonic()

        def hook(sample):
            if not onair_seen and time.monotonic() - started >= 1.0 and talk.is_alive():
                onair_seen.append(public_talker(anon) or {})

        end_ms, skew, entry = drive(bp, sel, anon, ctx, series, hook)
        talk.join(timeout=5)
    finally:
        try:
            peer.unlink()
        except Exception:                          # noqa: BLE001 - leaving the room is best effort
            pass
        peer.close()
    if end_ms is None:
        write_evidence(bp, "timeline", {"live": live, "error": "no rooms[].last.end_ms after the over", "last_entry": entry}, series)
        pytest.fail("rooms[%s].last.end_ms never appeared in the public state after the over (entry %s)" % (ROOM, json.dumps(entry, sort_keys=True)[:300]))
    msf.annotate(series, end_ms, skew)
    failures += talker_failures(onair_seen[0] if onair_seen else None)
    found, metrics = msf.judge_fade(series, ROOM, CALL)
    failures += found
    metrics["transitions_seen"] = sorted({d for s in series for d in (s.get("art_transition") or [])} | {s.get("room_transition") for s in series if s.get("room_transition")})
    path = write_evidence(bp, "timeline", {"live": live, "end_ms": end_ms, "skew_ms": round(skew, 1), "metrics": metrics, "failures": failures,
                                          "public_onair_during_the_over": onair_seen[:1], "last_entry": entry}, series)
    print("[RECORD] map-fade %s timeline: skew %.1f ms, %d samples, failures %d, %s -> %s" % (
        brlib.label(bp.engine), skew, len(series), len(failures), compact(metrics), path))
    finish_checks(bp, "map fade timeline")
    assert not failures, "%s: the Map room fade differs from the TWIN2b spec: %s (evidence: %s)" % (brlib.label(bp.engine), "; ".join(failures), path)


# ================================================================ a new over during the fade

def test_map_new_over_during_the_fade_restores_the_look(browser_page, target, web_client):
    """A 1.5 s over, its end, and as soon as the page shows fade step >= 1 a second 3 s over from the same client: the full on-air look
    returns within one sampling step (no dimmed intermediate: class, computed art opacity, talker text opacity and fill all at the
    on-air values from the first restored sample on), and the second over's end fades again as in the timeline case."""
    bp = browser_page
    anon = web_client()
    sel = pages.map_sampler_selectors(ROOM)
    peer = usrp_client(target, CALL, ROOM)
    series = []
    second = {"talk": None, "wall_ms": None}
    ctx = {"talk": None, "since_ms": None}
    try:
        live = open_map(bp)
        wait_quiet(bp, sel)
        first = Talk(peer, tone_frames(1.5))
        first.start()

        def hook(sample):
            if second["talk"] is None and not first.is_alive() and msf.is_fading(sample) and (msf.step_of(sample) or 0) >= 1:
                talk = Talk(peer, tone_frames(OVER2_S))
                second["wall_ms"] = time.time() * 1000.0
                second["talk"] = talk
                ctx["since_ms"] = second["wall_ms"] - 200.0
                ctx["talk"] = talk
                talk.start()

        end_ms, skew, entry = drive(bp, sel, anon, ctx, series, hook)
        first.join(timeout=5)
        if second["talk"] is not None:
            second["talk"].join(timeout=5)
    finally:
        try:
            peer.unlink()
        except Exception:                          # noqa: BLE001
            pass
        peer.close()
    if second["talk"] is None:
        write_evidence(bp, "restart", {"live": live, "error": "no fade step >= 1 was ever sampled"}, series)
        pytest.fail("the page never showed a fade step >= 1 after the first over: the second over could not be started inside the fade")
    if end_ms is None:
        write_evidence(bp, "restart", {"live": live, "error": "no end_ms of the second over"}, series)
        pytest.fail("rooms[%s].last.end_ms of the second over never appeared (entry %s)" % (ROOM, json.dumps(entry, sort_keys=True)[:300]))
    for s in series:
        s["u"] = round((s["wall_ms"] - second["wall_ms"]) / 1000.0, 3)
    msf.annotate(series, end_ms, skew)
    lit, rest = msf.reference_fills(series)
    failures, restore = msf.judge_restore(series, ROOM, CALL, lit, rest, OVER2_S)
    sub = [s for s in series if "restored_u" in restore and s["u"] >= restore["restored_u"]]
    fade_failures, fade_metrics = msf.judge_fade(sub, ROOM, CALL) if sub else (["no samples after the restore"], {})
    failures += ["second over: " + f for f in fade_failures]
    path = write_evidence(bp, "restart", {"live": live, "end_ms": end_ms, "skew_ms": round(skew, 1), "second_over_started_wall_ms": second["wall_ms"],
                                         "restore": restore, "second_fade": fade_metrics, "failures": failures, "lit": lit, "rest": rest}, series)
    print("[RECORD] map-fade %s restart: %s | second fade %s | failures %d -> %s" % (
        brlib.label(bp.engine), compact(restore), compact(fade_metrics), len(failures), path))
    finish_checks(bp, "map fade restart")
    assert not failures, "%s: a new over during the fade does not restore the on-air look at once: %s (evidence: %s)" % (
        brlib.label(bp.engine), "; ".join(failures), path)


# ================================================================ reduced motion

def test_map_fade_under_reduced_motion(browser_page, target, web_client):
    """prefers-reduced-motion: reduce (page.emulate_media before the first navigation): after a 2 s over the fade shows the static
    dimmed state (computed opacity of the fading art 0.45 at every step, the room fill a constant colour between the on-air and the
    rest colour) with NO CSS transition (computed transition-duration 0s on the tile and on the fading elements), the caption says
    '<call> finished', and the room is at rest from 6.5 s."""
    bp = browser_page
    anon = web_client()
    sel = pages.map_sampler_selectors(ROOM)
    peer = usrp_client(target, CALL, ROOM)
    series = []
    try:
        bp.page.emulate_media(reduced_motion="reduce")
        live = open_map(bp)
        wait_quiet(bp, sel)
        talk = Talk(peer, tone_frames(2.0))
        ctx = {"talk": talk, "since_ms": time.time() * 1000.0 - 500.0}
        talk.start()
        end_ms, skew, entry = drive(bp, sel, anon, ctx, series)
        talk.join(timeout=5)
    finally:
        try:
            peer.unlink()
        except Exception:                          # noqa: BLE001
            pass
        peer.close()
    if end_ms is None:
        write_evidence(bp, "reduced", {"live": live, "error": "no rooms[].last.end_ms after the over", "last_entry": entry}, series)
        pytest.fail("rooms[%s].last.end_ms never appeared in the public state after the over (entry %s)" % (ROOM, json.dumps(entry, sort_keys=True)[:300]))
    msf.annotate(series, end_ms, skew)
    failures, metrics = msf.judge_reduced(series, ROOM, CALL)
    path = write_evidence(bp, "reduced", {"live": live, "end_ms": end_ms, "skew_ms": round(skew, 1), "metrics": metrics, "failures": failures}, series)
    print("[RECORD] map-fade %s reduced motion: %d samples, failures %d, %s -> %s" % (brlib.label(bp.engine), len(series), len(failures), compact(metrics), path))
    finish_checks(bp, "map fade reduced motion")
    assert not failures, "%s: the reduced-motion fade differs from the TWIN2b spec: %s (evidence: %s)" % (brlib.label(bp.engine), "; ".join(failures), path)
