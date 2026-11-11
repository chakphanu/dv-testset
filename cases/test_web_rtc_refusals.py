# SPDX-License-Identifier: MIT
"""WEBSTALL1 (dv-router merge 17b05933): WebRTC capacity refusals name their limit and are counted. Written offline for the image with the fix;
NOT RUN. docs/web-refusals-cases.md has the run commands, the runtime and the GAPs. All cases need DVT_WEBSTALL1FIX=on (a skip with the reason
otherwise: the current image has neither family).

  a  test_both_families_expose_every_reason      dv_edge_webrtc_offer_refused_total{reason} (10 reasons) and dv_web_rtc_refused_total{reason}
                                                 (the same 10 plus web_records, web_sessions, login_circuits, web_guest_records, web_guests,
                                                 web_guest_ip, turn_issues): every reason present, zeros included; the old
                                                 dv_edge_webrtc_join_refused_total{reason="core"} is still there. Profile web only.
  b  test_ninth_circuit_counts_login_circuits    one login (Chromium) keeps eight offer-only circuits, the ninth is 503 `unavailable` with
                                                 Retry-After 5 (the detail is recorded, not judged) and dv_web_rtc_refused_total{login_circuits}
                                                 rises by exactly one; no edge reason moves (the web service refuses it before the edge).
                                                 Profile web,browser, DVT_USRP_DYNAMIC=1.
The burst of joins in the edge's join window (3c) is cases/test_web_load.py::test_join_burst_counts_join_window; the load summary's before and
after counters (3d) are `refusals` in out/<run>/web/load.json of the load cases.

The families are published by the edge and by dv-svc-web about every 10 s and cached by the hub: every comparison waits for two equal reads
(dvtest/refusals.py settled). The two families are judged one by one and never added: an edge refusal relayed as a 503 is counted once in each.
Differences from the documents go to web.finding(); the contract (status, code, Retry-After) is asserted, the detail text is only recorded.
"""
import time

import pytest

import websid_support as ws
from browser_fixtures import BrowserPage, _failed
from dvtest import metrics, refusals, web

pytestmark = [pytest.mark.rig, pytest.mark.web]

EXPOSE_WAIT_S = 90.0            # the first records of the edge and of dv-svc-web, after the start of the run
SHOW_WAIT_S = 60.0              # a counter that is shown after the refusal: published every 10 s plus the hub cache (K22 saw up to 35 s)
ROOM = "V"                      # the Listen room of the WEBSID1 cases (a USRP dynamic hub serves it: DVT_USRP_DYNAMIC=1); the offers only need a served module
OFFER_GAP_S = 0.7               # the per-address join gate is 1/s with a burst of 5: nine offers fit when spaced like this (cases/test_web_session_rotation.py)


def need_fix():
    if not refusals.fix_on():
        pytest.skip(refusals.skip_reason())


def note(text):
    print("[RECORD] WEBSTALL1: %s" % text)


# ------------------------------------------------------------------ a: both families, every reason

def test_both_families_expose_every_reason(rig_control, web_base_url):
    need_fix()
    snap, ok = refusals.poll(lambda: refusals.read(rig_control), lambda s: not refusals.shape_problems(s), EXPOSE_WAIT_S)
    note("edge %s: %s; web %s: %s" % (refusals.EDGE_FAMILY, snap.get("edge"), refusals.WEB_FAMILY, snap.get("web")))
    assert ok, "; ".join(refusals.shape_problems(snap)) or "the families were not read"
    for fam in ("edge", "web"):
        negative = {r: v for r, v in snap[fam].items() if v < 0}
        assert not negative, "%s has negative counters: %s" % (fam, negative)
    samples = metrics.parse(rig_control.metrics("xlxd-sut"))
    core = [s for s in samples if s.name == refusals.CORE_FAMILY]
    assert core, "%s (EDG2, unchanged) is not exposed" % refusals.CORE_FAMILY
    assert {s.labels.get("reason") for s in core} == {"core"}, "%s has reasons %s, want only core" % (
        refusals.CORE_FAMILY, sorted({str(s.labels.get("reason")) for s in core}))


# ------------------------------------------------------------------ b: the ninth circuit of one login

@pytest.fixture
def circuit_page(engine_browsers, web_base_url, request, br_user):
    """Chromium signed in as the browser user (API login inside the browser; the session is bound to its User-Agent); signed out afterwards, which
    revokes the circuits. One engine: the cap and the counter do not depend on it (the engine matrix is WEBSID1's job)."""
    bp = BrowserPage("chromium", engine_browsers("chromium"), web_base_url, request.node.name.replace("[", "-").rstrip("]"))
    status = bp.api_login(br_user.callsign, br_user.password)
    assert status == 200, "browser API login of the user: HTTP %s" % status
    yield bp
    try:
        bp.api_logout()
    except Exception:                                               # noqa: BLE001 - the case is over
        pass
    bp.finish(_failed(request))


def test_ninth_circuit_counts_login_circuits(circuit_page, rig_control):
    need_fix()
    bp = circuit_page
    before, settled = refusals.settled(lambda: refusals.read(rig_control))
    problems = refusals.shape_problems(before)
    assert not problems, "before the circuits: %s" % "; ".join(problems)
    bp.goto("/shared/img/favicon.svg")
    csrf = bp.page.evaluate(ws.JS_SESSION)["csrf"]
    answers = []
    for _ in range(refusals.LOGIN_CIRCUIT_CAP + 1):
        answers.append(bp.page.evaluate(ws.JS_OFFER_ONLY, {"room": ROOM, "csrf": csrf}))
        time.sleep(OFFER_GAP_S)
    statuses = [a["status"] for a in answers]
    accepted, ninth = answers[:refusals.LOGIN_CIRCUIT_CAP], answers[refusals.LOGIN_CIRCUIT_CAP]
    assert all(a["status"] == 200 for a in accepted), "the first %d offers of one login were not all accepted: %s" % (refusals.LOGIN_CIRCUIT_CAP, statuses)
    shape = refusals.refusal_shape_problems("the ninth circuit", ninth["status"], ninth.get("code"), ninth.get("retry_after"))
    note("ninth circuit: status %s, code %s, Retry-After %s, detail %r (%s)" % (
        ninth["status"], ninth.get("code"), ninth.get("retry_after"), ninth.get("detail"), refusals.detail_note(ninth.get("detail"))))
    assert not shape, "; ".join(shape)
    if ninth.get("detail") != refusals.DETAILS["login_circuits"]:
        web.finding("WEBSTALL1: the ninth circuit of a login says %r, DV-Router 17b05933 documents %r (web.md, the per-limit detail table)" % (
            ninth.get("detail"), refusals.DETAILS["login_circuits"]))

    # the counter, once the web service has published it and the hub has cached it
    def shown(snap):
        d = refusals.delta(before, snap)
        return bool((d.get("web") or {}).get("login_circuits"))
    snap, seen = refusals.poll(lambda: refusals.read(rig_control), shown, SHOW_WAIT_S)
    if seen:
        snap, _ok = refusals.settled(lambda: refusals.read(rig_control))              # nothing else may follow it
    rec = refusals.record(before, snap)
    note("counters after the ninth circuit: %s" % refusals.line(rec))
    assert seen, "%s{reason=login_circuits} did not rise within %d s after the ninth circuit was refused (delta %s)" % (
        refusals.WEB_FAMILY, SHOW_WAIT_S, rec["delta"].get("web"))
    web_delta = rec["delta"]["web"]
    assert web_delta["login_circuits"] == 1, "%s{reason=login_circuits} rose by %g for one refused circuit" % (refusals.WEB_FAMILY, web_delta["login_circuits"])
    other_web = {r: v for r, v in refusals.moved(web_delta).items() if r != "login_circuits"}
    assert not other_web, "%s moved other reasons too: %s" % (refusals.WEB_FAMILY, other_web)
    edge_moved = refusals.moved((rec["delta"] or {}).get("edge"))
    assert not edge_moved, "%s moved %s: the web service refuses the ninth circuit by itself, the edge never saw it" % (refusals.EDGE_FAMILY, edge_moved)
