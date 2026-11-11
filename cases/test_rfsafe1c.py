# SPDX-License-Identifier: MIT
"""DV-Router RFSAFE1c cases (DV-Router commit 63fecd9f: a mid-run fence announced to the status hub, named probe failures, the launcher start in the hub's
process rows). Case table, run command, limits and GAPs: docs/rfsafe1c-cases.md. Pure helpers: dvtest/rfsafe1c.py (unit tests cases/unit/test_rfsafe1c_lib.py).

  RF1c-4a  steady state: for every token-bearing launcher child (edge, codec worker, data worker, room controller) the hub's procs[] row has the child's pid
           and the child's since_ms (GET /api/v1/admin/status: `procs` and `launcher.children` in one reply)
  RF1c-4b  after a restart of dv-edge-ax25 (POST /api/v1/admin/children/dv-edge-ax25/restart) the row may show the new `up` with the PREVIOUS child's pid and
           since_ms (recorded); once its pid equals the launcher child's pid its since_ms equals the child's (waited up to 15 s: the 10 s launcher query + slack)
  RF1c-2   during the same restart (the fence of the new process begins and ends) no `session` item with ev `update` appears in the admin event history or on the
           public change feed (the `update` event is a view-only hint of the edge -> hub path; the repaired `snapshot` item is allowed)
  RF1c-3   the SUT log lines `RF TX fenced on <physical>: probe failed part=<part>; recovery is automatic`: shape, fixed part set, configured physical only, no
           other text (the logs are collected after the run: when they are not there yet the case SKIPS and the host command `python3 -m dvtest.rfsafe1c out/<run>`
           gives the verdict)
  RF1c-1   GAP: a fence that begins MID-RUN (the observer's session cut, a foreign holder) shows in the view within 2 s and TX is refused from the first moment.
           No control action reaches the fabric session or an `rf/<physical>/ptt` token, so the case skips naming the gap (dvtest.rfsafe1c.view_latency /
           judge_latency are ready).

Gate: DVT_RFSAFE1C=on (passed to the tester by rig/compose.yml); every case skips with the reason otherwise. Shape: the AX.25 edge with the fake TNCs of
cases/test_dp15_rf.py (dp18-rf.toml or dp15-rf.toml with DVT_DP15_TX_ENABLE=true), the web profile for the admin API and the public feed.
Observations: stdout and out/<run>/rfsafe1c.txt ("RECORD", "FINDING"). NOT RUN ON A RIG: there is no RFSAFE1c image yet.
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import logscan, rfsafe1c, txguard  # noqa: E402

import data_support as ds  # noqa: E402
import dp18_support as dc  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
# the fake TNCs rf-a / rf-b (their `hub` argument is the fixture of THIS module)
from test_dp15_rf import rf  # noqa: E402,F401

pytestmark = pytest.mark.rig

LOG = "rfsafe1c.txt"
SETTING = "rfsafe1c tx=%s" % os.environ.get("DVT_DP15_TX_ENABLE", "true")
PORT = "rf-a"
SETTLE_S = 4.0                        # after the fence ended: the hub's refresh (1 s while fenced, 10 s after) and the feed fan-out
EVENTS_LIMIT = 1000
GAP_MIDRUN = ("GAP: a fence that begins mid-run needs a cause the black-box side cannot produce: cutting the observer's fabric session or holding a foreign "
              "`rf/<physical>/ptt` token. The control channel of scripts/run.sh has no action for either (kill-proc does not reach dv-edge-ax25 or the fabric "
              "router, there is no fabric client in the tester), and no new action is invented here. dvtest.rfsafe1c.view_latency / judge_latency are ready "
              "for a primitive that dv-router adds (docs/rfsafe1c-cases.md, GAPs)")


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def strict():
    if not rfsafe1c.strict_on():
        pytest.skip("DVT_RFSAFE1C is not on: the RFSAFE1c statements are asserted only on an image that has them (docs/rfsafe1c-cases.md)")
    return True


def status_body(admin):
    resp = admin.get(admin.path("/status"), expect=None)
    return resp.json() if resp.status == 200 else None


@pytest.fixture(scope="module")
def edge(strict, target, hub, admin_session):
    """The AX.25 edge with an effective-TX port rf-a and the launcher child dv-edge-ax25; skips with the reason otherwise."""
    doc = p1.read_config(target)
    if dc.tx_mode(doc, PORT) is not True or not (doc.get("edges") or {}).get("ax25", {}).get("tx_enable"):
        pytest.skip("the rendered config has no AX.25 port %s with effective TX (dp18-rf.toml, or dp15-rf.toml with DVT_DP15_TX_ENABLE=true)" % PORT)
    operator = admin_session("operator")
    body = status_body(operator)
    names, seen = txguard.ax25_child_names(body or {})
    if names != [txguard.AX25_CHILD]:
        pytest.skip("the launcher child %r is not listed by /api/v1/admin/status; children seen: %s" % (txguard.AX25_CHILD, seen))
    return types.SimpleNamespace(doc=doc, operator=operator, admin_session=admin_session, physicals=rfsafe1c.configured_physicals(doc))


# ================================================================ RF1c-4a: the steady state

def test_rf1c_4a_token_rows_carry_the_launcher_start(rig, target, edge):
    """status.md "Process up and down": a token-bearing row keeps the launcher's child pid, restarts and start (`since_ms`), fresh telemetry does not replace
    it. Steady state: once the row's pid is the child's, its since_ms is the child's (waited up to 15 s for the first launcher query)."""
    case = "RF1c-4a"
    res = rfsafe1c.wait_origin(lambda: status_body(edge.operator))
    note(target, case, "RECORD", rfsafe1c.describe_origin(res))
    assert not res.problems, "procs[] against launcher.children: %s" % (res.problems,)


# ================================================================ one restart, three observations

def run_restart(target, edge, admin, anon, sleep=time.sleep, settle_s=SETTLE_S, wait_kw=None):
    """Restart dv-edge-ax25 through the audited admin API (as DP15-6g) while the public change feed is read, then collect: the procs[] row catching up to the new
    child (RF1c-4b), the event history above the baseline and the public frames (RF1c-2), the SUT log (RF1c-3). The fence of the new process is waited out.
    `edge.operator` reads, `admin` (admin role, step-up) restarts, `anon` is an anonymous web client for the public stream;
    `wait_kw` (clock, sleep) is for the unit tests."""
    operator = edge.operator
    before = status_body(operator)
    child = dc.child_state(before or {}, txguard.AX25_CHILD) or {}
    previous = {("edge", "ax25"): child.get("pid")} if child.get("pid") is not None else None
    ev0 = operator.get(operator.path("/events?limit=1"), expect=None)
    baseline = (ev0.json() or {}).get("last_id") if ev0.status == 200 else None
    out = types.SimpleNamespace(child_before=child, baseline=baseline, public=None, public_error=None, origin=None, events=None, events_status=None,
                                fence=None, log=None, restart_status=None)
    with ds.SseWatch(anon, "/api/v1/public/events") as sse:
        ready = sse.ready()
        note(target, "RF1c", "RECORD", "public change feed %s; admin event history baseline id %r; launcher child before %s" % (
            "open" if ready else "NOT open (%s)" % sse.error, baseline, child))
        admin.step_up()
        resp = admin.post(admin.path("/children/%s/restart" % txguard.AX25_CHILD), {}, expect=None)
        out.restart_status = resp.status
        assert resp.status == 200, "POST children/%s/restart answered HTTP %d, want 200" % (txguard.AX25_CHILD, resp.status)
        out.origin = rfsafe1c.wait_origin(lambda: status_body(operator), only=[txguard.AX25_CHILD], previous=previous, **(wait_kw or {}))
        out.fence = txguard.wait_unfenced(txguard.ax25_reader(operator, PORT), kind="ax25")
        sleep(settle_s)
        events = operator.get(operator.path("/events?limit=%d" % EVENTS_LIMIT), expect=None)
        out.events_status = events.status
        out.events = rfsafe1c.event_hints((events.json() or {}).get("items") if events.status == 200 else [], baseline or 0)
        out.public = rfsafe1c.frame_hints([frame for _at, frame in sse.snapshot()])
        out.public_error = None if ready else sse.error
    note(target, "RF1c", "RECORD", "tx guard wait after the restart: " + txguard.describe(out.fence, "ax25", PORT))
    out_dir = getattr(target, "out", None)
    if out_dir:
        state, _found, _looked = logscan.request(out_dir, "RF1c-probe-failed", [rfsafe1c.PROBE_NEEDLE])
        if state == "scanned":
            texts = []
            for path in logscan.log_files(out_dir):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    texts.append((os.path.basename(path), fh.read()))
            out.log = [(name, rfsafe1c.judge_log(text, edge.physicals)) for name, text in texts]
    return out


@pytest.fixture(scope="module")
def restarted(request, target, edge, rf, admin_session):
    """One restart, three observations (RF1c-4b, RF1c-2, RF1c-3); the fake TNCs (`rf`) must be listening before the edge restarts."""
    return run_restart(target, edge, admin_session("admin"), request.getfixturevalue("web_client")())


def test_rf1c_4b_a_restarted_edge_row_catches_up_to_the_launcher_child(rig, target, restarted):
    """After the restart the new token can appear before the launcher's state put lands: for up to the 10 s query the row can show the new `up` with the previous
    child's pid and since_ms (recorded, tolerated). Once the row's pid equals the launcher child's pid, since_ms is equal (restarts is recorded)."""
    case = "RF1c-4b"
    res = restarted.origin
    note(target, case, "RECORD", rfsafe1c.describe_origin(res))
    for t, key, detail, prev in res.stale:
        note(target, case, "RECORD", "stale row at %.1f s after the restart (%s/%s%s): %s" % (t, key[0], key[1], ", still the PREVIOUS child" if prev else "", detail))
    assert not res.problems, "the hub row of the restarted AX.25 edge: %s" % (res.problems,)


def test_rf1c_2_no_session_update_hint_in_the_feeds(rig, target, restarted):
    """data.md 10.x / status.md: the modem `update` event is a hint on the edge -> hub path; it adds no `session` item to the change feed or the event ring (the
    repaired snapshot's `snapshot` item does). Read: the admin event history above the baseline and the frames of the public stream during the restart."""
    case = "RF1c-2"
    ev, pub = restarted.events, restarted.public
    note(target, case, "RECORD", "admin event history (HTTP %s): %d item(s) above id %r, types/evs %s; public change feed: %d event frame(s), types/evs %s%s" % (
        restarted.events_status, ev.seen, restarted.baseline, ev.evs, pub.seen, pub.evs, "" if restarted.public_error is None else
        " (the stream was NOT open: %s)" % restarted.public_error))
    for tag, hints in (("admin event history", ev), ("public change feed", pub)):
        for other in hints.others:
            note(target, case, "RECORD", "%s: an item with ev update of another type (not the hint): %s" % (tag, other))
    if restarted.events_status != 200:
        pytest.fail("GET /api/v1/admin/events answered HTTP %s" % restarted.events_status)
    if ev.seen == 0:
        note(target, case, "FINDING", "no event above id %r in the admin history after an edge restart: the restart should at least have produced status/session items, "
             "so the absence of a hint proves little" % restarted.baseline)
    if restarted.public_error is not None:
        note(target, case, "FINDING", "the public change feed could not be read (%s): only the admin event history is judged" % restarted.public_error)
    assert not ev.hints, "session/update hint item(s) in the admin event history: %s" % (ev.hints,)
    assert not pub.hints, "session/update hint item(s) on the public change feed: %s" % (pub.hints,)


def test_rf1c_3_the_probe_failed_line_has_its_shape(rig, target, restarted):
    """radio.md 10.4: `RF TX fenced on <physical>: probe failed part=<own_token|link_check|holder_watch|holder_query|task>; recovery is automatic`, once at the
    start of a run of failed probes, the configured physical only, no other text. After a healthy restart there should be NO such line (a FINDING when there is
    one: the new process declared its token before probing). The logs are collected after the run."""
    case = "RF1c-3"
    out_dir = getattr(target, "out", None)
    if restarted.log is None:
        pytest.skip("the SUT logs are collected after pytest ends: run `python3 -m dvtest.rfsafe1c %s` on the host for the verdict (exit 1 on a line that breaks the "
                    "shape; FINDING lines for a probe failed line after a healthy restart); the needle file RF1c-probe-failed.json is in %s/logscan" % (out_dir, out_dir))
    problems = []
    for name, judged in restarted.log:
        for finding in judged.findings:
            note(target, case, "FINDING", "%s: %s" % (name, finding))
        problems += ["%s: %s" % (name, p) for p in judged.problems]
    note(target, case, "RECORD", "SUT log judged: %d file(s), %d probe failed line(s)" % (len(restarted.log), sum(len(j.lines) for _n, j in restarted.log)))
    assert not problems, "probe failed log lines: %s" % (problems,)


# ================================================================ RF1c-1: the GAP

def test_rf1c_1_a_midrun_fence_shows_within_2_s_and_tx_is_refused(rig, target, edge):
    """Statement (1) of the DV-Router team: a fence that begins mid-run shows in the status view within 2 s of the cause, and TX is refused from the first
    moment regardless of the view. Not producible from the black-box side: see GAP_MIDRUN. Recorded so that the run says so."""
    note(target, "RF1c-1", "RECORD", GAP_MIDRUN)
    pytest.skip(GAP_MIDRUN)
