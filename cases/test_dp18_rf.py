# SPDX-License-Identifier: MIT
"""DV-Router DP12c / DP18 cases on the AX.25 edge: per-port TX mode `simulate`, a reload of that mode through the admin configuration API,
and the RF copies of the node objects (data.md 10.x "Built (DP12c)", 9.5 "Built (DP18)", review 232). The tester plays the KISS TNCs
(dvtest/kiss_tnc.py, as tier A of cases/test_dp15_rf.py). Case table, run commands, limits: docs/dp18-cases.md. Pure helpers:
cases/dp18_support.py.

  DP18-s   (dp18-rf.toml: rf-a tx = true and rf-b tx = "simulate" on ONE physical) a room text is transmitted on rf-a only: ZERO bytes at
           the simulate port's TNC after its connect, dv_edge_rf_tx_total{port=rf-b,result=simulated} grows, rf-a counts sent, and the
           modem tx and PTT counters of rf-b stay 0; a second text 31 s later is transmitted on rf-a again with the same timing (the
           hypothetical ledger of rf-b never holds the real port). LIMIT: the `rf/<physical>/ptt` token is a fabric key; the control
           channel does not expose it, so "no token" is inferred from the counters and the silence, never asserted directly.
  DP18-r1  reload rf-b simulate -> true through POST /api/v1/admin/config/changes (base_etag): the AX.25 edge restarts (restarts counter or
           pid), texts queued in the simulated scheduler are never transmitted after the change, a control text is (the port is live)
  DP18-r2  reload rf-a true -> simulate, then a restore from the configuration history: no frame queued before the change is transmitted
           after it, and the restore brings the transmitting port back
  DP18-o   (dp18-rf.toml + dp18-rf-objects.toml) eleven reports with RF copies on afsk1200-shared: the releases are spread (never in one
           tick), what the beacon class share cannot carry is counted (dropped_*), the TNC saw exactly the frames counted as sent, and the
           objects that are dropped differ between cycles (recorded, a FINDING when equal)

Every case skips with the reason on an image without DP12c / DP18 (the configuration schema does not know `simulate`). NOT RUN ON A RIG.
Not covered, no radio ports in the rig: a radio port (CM108 / serial PTT, audio) with `tx = "simulate"` never keys and never sends audio, and a
real radio port on the same physical is never blocked. Observations: stdout and out/<run>/dp18-rf.txt ("RECORD", "FINDING").
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import aprsis_client as ai, ax25, kiss_tnc, logscan, metrics, rfsafe1b, rfsafe1c, txguard  # noqa: E402

import data_support as ds  # noqa: E402
import dp18_support as dc  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_rf as tr  # noqa: E402
# the fake TNCs rf-a / rf-b (their `hub` argument is the fixture of THIS module) and the web side of tier A, reused by name
from test_dp15_rf import rf, web_ctx  # noqa: E402,F401
# the private aprsc (and the `shape` it reads) of the first DP7 set, as cases/test_dp18_origin.py; `hub` is the fixture of THIS module
from test_dp15_positions import aprsc, shape  # noqa: E402,F401

pytestmark = pytest.mark.rig

LOG = "dp18-rf.txt"
SETTING = "dp18 tx=%s" % os.environ.get("DVT_DP15_TX_ENABLE", "true")
COUNTER_WAIT_S = 40.0
SILENCE_S = 20.0                      # how long "nothing arrives at the simulate TNC" is watched after the real port transmitted
SPACING_S = 31.0                      # tx_per_min 2, burst 1: the next frame needs 30 s after the last
WATCH_AFTER_CHANGE_S = 45.0           # past the 13 s TX guard of the restarted edge plus a drain of a queued frame
RESTART_WAIT_S = 60.0
CYCLES = int(os.environ.get("DVT_DP18_CYCLES", "2"))
STAGGER_TOLERANCE_S = 5.0


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def sim(request, target, hub, admin_session):
    """The simulate shape (rf-a tx true, rf-b tx simulate), the DP12c capability probe and the fake TNCs; skips with the reason otherwise."""
    doc = p1.read_config(target)
    modes = {name: dc.tx_mode(doc, name) for name in ("rf-a", "rf-b")}
    if modes != {"rf-a": True, "rf-b": "simulate"}:
        pytest.skip("the rendered config does not have rf-a tx = true and rf-b tx = \"simulate\" (found %s): use rig/config/dv-router/dp18-rf.toml "
                    "instead of dp15-rf.toml (docs/dp18-cases.md)" % (modes,))
    admin = admin_session("operator")
    schema = admin.get(admin.path("/schema"), expect=None)
    if schema.status != 200 or not dc.schema_has_dp18(schema.text()):
        pytest.skip("the image does not know DP12c / DP18: the configuration schema has no `simulate`, `beacon` and `objects` (HTTP %d)" % schema.status)
    tnc = request.getfixturevalue("rf")
    web = request.getfixturevalue("web_ctx")
    note(target, "sim", "RECORD", "shape: rf-a %s, rf-b %s; TNC connections a=%d b=%d" % (modes["rf-a"], modes["rf-b"], tnc.a.connections, tnc.b.connections))
    return types.SimpleNamespace(rf=tnc, web=web, admin=admin, doc=doc, admin_session=admin_session, request=request)


def family_port_total(hub, family, port, **labels):
    samples = [s for s in metrics.parse(hub.text()) if s.name == family and s.labels.get("port") == port and all(s.labels.get(k) == v for k, v in labels.items())]
    return sum(int(s.value) for s in samples), bool(samples)


def wait_unfenced(sim, target, case, port):
    """The TX guard of the edge (13 s after a start / restart) must be over before a case expects a transmission on `port`; one line is recorded."""
    result = txguard.wait_unfenced(txguard.ax25_reader(sim.admin, port), kind="ax25")
    note(target, case, "FINDING" if result.state in ("timeout", "not_found", "scope") else "RECORD", txguard.describe(result, "ax25", port))
    return result


def fence_view_bounds(sim, target, case, port):
    """RFSAFE1b (dvtest/rfsafe1b.py): right after the launcher child dv-edge-ax25 restarted, poll the fence view of `port` until it clears and judge it. Strict
    (DVT_RFSAFE1B=on): only startup_guard may show, the view clears within 16 s of the child start and not before 13.0 s, tx_guard_ms is <= 13000 and
    non-increasing, read time + tx_guard_ms predicts the end within 1.5 s; any other cause or a "probe failed" log line is a FINDING. Lenient (default): the view
    may show a fence for up to about 24.5 s (wait up to 30 s); the cause and the timing are recorded, nothing is asserted. The origin is the child start: `since_ms` of the
    launcher's `launcher.children[]` entry of the admin status (Unix ms of the CURRENT child start, read at once; the hub's procs[] row of an edge has none by design). A value that
    cannot be a recent start falls back to the time the pid change was seen, and the 13.0 s lower-bound check is then skipped."""
    strict = rfsafe1b.strict_on()
    st = children_state(sim)
    since = (st or {}).get("since_ms")
    origin, age = rfsafe1b.child_start_origin(since, time.time(), time.monotonic())
    known = origin is not None
    if not known:
        origin = time.monotonic()
    trace = rfsafe1b.collect(txguard.ax25_reader(sim.admin, port), origin, max_wait_s=rfsafe1b.MAX_WAIT_S if strict else 30.0)
    judged = rfsafe1b.judge(trace.samples, strict=strict, check_early=known)
    note(target, case, "RECORD", "RFSAFE1b %s view of %s after the restart (child start %s s before the first read, from since_ms %s%s): %s; trace %s" % (
        "strict" if strict else "lenient (DVT_RFSAFE1B off)", port, "%.1f" % age if known else "unknown", since, "" if known else ", unusable: the observed restart time is the origin",
        judged.summary, trace.state))
    for finding in judged.findings:
        note(target, case, "FINDING", "RFSAFE1b: " + finding)
    out_dir = getattr(target, "out", None)
    if out_dir:
        state, found, _looked = logscan.request(out_dir, case + "-probe-failed", [rfsafe1b.PROBE_FAILED_NEEDLE])
        if state == "scanned":
            note(target, case, "RECORD", "SUT log scanned: probe failed lines %s" % {os.path.basename(k): v[:3] for k, v in found.items()})
        else:
            note(target, case, "RECORD", "SUT log scan left for the host: `%s` (a FOUND for %s-probe-failed is a finding after a healthy restart on an RFSAFE1b image, "
                 "not a failure)" % (logscan.command_for(out_dir), case))
    if trace.state == "scope":
        pytest.fail("the fence view of %s carries no `modem` object (operator or admin needed)" % port)
    assert not judged.problems, "RFSAFE1b bounds (DVT_RFSAFE1B=on): %s; %s" % (judged.problems, judged.summary)
    return judged


def procs_origin(sim, target, case, before):
    """RFSAFE1c (DVT_RFSAFE1C=on, dvtest/rfsafe1c.py, statement 4): after the child restart the hub's procs[] row of dv-edge-ax25 has the launcher child's since_ms once
    its pid is the child's pid (a stale row with the previous pid is recorded and waited out, up to 15 s). `before`: children_state before the restart."""
    if not rfsafe1c.strict_on():
        return None
    pid = (before or {}).get("pid")

    def read():
        resp = sim.admin.get(sim.admin.path("/status"), expect=None)
        return resp.json() if resp.status == 200 else None
    res = rfsafe1c.wait_origin(read, only=[txguard.AX25_CHILD], previous={("edge", "ax25"): pid} if pid is not None else None)
    note(target, case, "RECORD", "RFSAFE1c " + rfsafe1c.describe_origin(res))
    assert not res.problems, "RFSAFE1c procs[] row of the restarted AX.25 edge (DVT_RFSAFE1C=on): %s" % (res.problems,)
    return res


def post_text(sim, poster_index, tag):
    """Post a room text of module B as web user `poster_index`; (body, t0)."""
    body = ds.nonce_body(tag)
    t0, _t1, status = tr.post(sim.web.posters[poster_index % len(sim.web.posters)], body)
    assert status == 202, "POST of the room text %r answered HTTP %d, want 202" % (body, status)
    return body, t0


def frames_with(tnc, body, since=0.0):
    out = []
    for at, _ch, frame in tnc.data_frames(since):
        msg = kiss_tnc.decode_message(frame)
        if msg and body in msg["text"]:
            out.append((at, msg))
    return out


# ================================================================ DP18-s: a simulated port never transmits

def test_dp18_s_a_simulated_port_never_reaches_its_tnc(rig, target, hub, sim):
    """data.md 10.x (DP12c): a simulated port passes the same gates and the scheduler but its release is consumed inside the edge: counted
    dv_edge_rf_tx_total{result=simulated}, never written to the adapter, no PTT. The real port rf-a on the same physical keeps its timing."""
    case = "DP18-s"
    sim.rf.tx_guard(case)
    bytes_b_before = len(sim.rf.b.received)
    base_a, base_b = dc.rf_tx_counts(metrics.parse(hub.text()), "rf-a"), dc.rf_tx_counts(metrics.parse(hub.text()), "rf-b")
    modem_b0, modem_b_known = family_port_total(hub, "dv_edge_modem_total", "rf-b", result="tx")
    ptt_b0, ptt_b_known = family_port_total(hub, "dv_edge_rf_ptt_total", "rf-b")
    latencies = []
    for round_no, tag in enumerate(("SIM1", "SIM2")):
        if round_no:
            time.sleep(SPACING_S)
        body, t0 = post_text(sim, round_no, tag)
        got = []
        end = time.monotonic() + tr.TX_WAIT_S
        while not got and time.monotonic() < end:
            got = frames_with(sim.rf.a, body, t0)
            time.sleep(0.2)
        assert len(got) == 1, "the real port rf-a transmitted %d frames for %r within %.0f s, want exactly 1" % (len(got), body, tr.TX_WAIT_S)
        latencies.append(round(got[0][0] - t0, 2))
        time.sleep(SILENCE_S)
        assert len(frames_with(sim.rf.a, body, t0)) == 1, "the real port transmitted %r more than once" % body
        silent = [f for f in sim.rf.b.received if f.t >= t0]
        assert not silent, "the simulate port's TNC received %d KISS frame(s) after the text was posted: %s" % (
            len(silent), [(f.channel, f.command, len(f.data)) for f in silent])
        assert frames_with(sim.rf.b, body, t0) == [], "the simulate port's TNC received the frame %r" % body
    note(target, case, "RECORD", "rf-a latencies post -> frame: %s s (two texts %.0f s apart); bytes received by rf-b's TNC since the connect: %d KISS frames in all, "
         "%d new" % (latencies, SPACING_S, len(sim.rf.b.received), len(sim.rf.b.received) - bytes_b_before))
    assert all(lat <= tr.TX_WAIT_S for lat in latencies), "rf-a took %s s, the bound is %.0f s" % (latencies, tr.TX_WAIT_S)
    end = time.monotonic() + COUNTER_WAIT_S
    while True:
        now_b = dc.rf_tx_counts(metrics.parse(hub.text()), "rf-b")
        now_a = dc.rf_tx_counts(metrics.parse(hub.text()), "rf-a")
        sim_delta = dc.result_total(now_b, "simulated") - dc.result_total(base_b, "simulated")
        sent_delta = dc.result_total(now_a, "sent") - dc.result_total(base_a, "sent")
        if (sim_delta >= 2 and sent_delta >= 2) or time.monotonic() >= end:
            break
        time.sleep(2.0)
    note(target, case, "RECORD", "dv_edge_rf_tx_total deltas: rf-b %s, rf-a %s" % (
        {k: v - base_b.get(k, 0) for k, v in now_b.items() if v - base_b.get(k, 0)}, {k: v - base_a.get(k, 0) for k, v in now_a.items() if v - base_a.get(k, 0)}))
    assert sim_delta >= 2, "dv_edge_rf_tx_total{port=rf-b,result=simulated} grew by %d for two texts, want at least 2" % sim_delta
    assert sent_delta >= 2, "dv_edge_rf_tx_total{port=rf-a,result=sent} grew by %d for two texts, want at least 2" % sent_delta
    assert dc.result_total(now_b, "sent") == dc.result_total(base_b, "sent"), "the simulate port counted a real send: %s" % now_b
    modem_b1, _ = family_port_total(hub, "dv_edge_modem_total", "rf-b", result="tx")
    ptt_b1, _ = family_port_total(hub, "dv_edge_rf_ptt_total", "rf-b")
    note(target, case, "RECORD", "rf-b modem tx %d -> %d (series seen: %s), rf PTT %d -> %d (series seen: %s); the rf/<physical>/ptt token is NOT observable "
         "black-box (limit)" % (modem_b0, modem_b1, modem_b_known, ptt_b0, ptt_b1, ptt_b_known))
    assert modem_b1 == modem_b0, "dv_edge_modem_total{port=rf-b,result=tx} moved: a simulated release reached the modem"
    assert ptt_b1 == ptt_b0, "dv_edge_rf_ptt_total{port=rf-b} moved: a simulated port counted a PTT"


# ================================================================ DP18-r1 / r2: reload of the mode

def config_state(sim):
    """(etag, writable) of GET /api/v1/admin/config. The configuration is read and written ONLY through the admin API (GET /config, POST
    /config/changes, the history restore); the rendered out/<run>/xlxd-config/xlxd-sut/dv-router.toml is only READ at the start, for the
    shape, and never edited (the echo cases E3 / E7 edit it directly and cannot share a run with these)."""
    admin = sim.admin_adm
    resp = admin.get(admin.path("/config"), expect=None)
    assert resp.status == 200, "GET config answered HTTP %d" % resp.status
    meta = (resp.json() or {}).get("meta") or {}
    return dc.etag_of(resp), meta.get("writable", True)


def apply_tx(sim, port, value, comment):
    """Replace `tx` of an AX.25 port through POST /api/v1/admin/config/changes (mode apply, base_etag of the current file); the response."""
    admin = sim.admin_adm
    etag, writable = config_state(sim)
    if not writable:
        pytest.skip("the configuration file is not writable (the single-file :ro mount of the rig): run with DVT_SUT_CONFIG_RW=1 "
                    "(scripts/run.sh, native launch: the config is a copy in a directory mounted read-write at /etc/dv-router-rw)")
    assert etag, "GET config returned no etag"
    index = dc.port_index(sim.doc, port)
    admin.step_up()
    resp = admin.post(admin.path("/config/changes"), dc.tx_change_request(index, value, etag, comment), expect=None)
    if resp.status == 409:
        pytest.skip("the configuration change answered 409 (read_only): the SUT cannot replace its config file; run with DVT_SUT_CONFIG_RW=1 "
                    "(scripts/run.sh, native launch). Reply: %s" % resp.text()[:200])
    assert resp.status == 200, "POST config/changes (tx of %s -> %r) answered HTTP %d: %s" % (port, value, resp.status, resp.text()[:300])
    return resp


def children_state(sim):
    admin = sim.admin
    resp = admin.get(admin.path("/status"), expect=None)
    return dc.child_state(resp.json() if resp.status == 200 else {}, txguard.AX25_CHILD)


def wait_edge_restart(sim, before, case, target):
    """Poll the launcher child dv-edge-ax25 until its restarts counter grew or its pid changed; the state."""
    end = time.monotonic() + RESTART_WAIT_S
    after = children_state(sim)
    while not dc.restarted(before, after) and time.monotonic() < end:
        time.sleep(1.0)
        after = children_state(sim)
    note(target, case, "RECORD", "launcher child %s before %s, after %s" % (txguard.AX25_CHILD, before, after))
    return after


@pytest.fixture(scope="module")
def reload_ctx(sim):
    """The admin session of the reload cases (admin role, step-up opened per call)."""
    sim.admin_adm = sim.admin_session("admin")
    return sim


def queue_texts(sim, tags):
    return [post_text(sim, i, tag) for i, tag in enumerate(tags)]


def test_dp18_r1_simulate_to_true_drops_the_queued_simulated_frames(rig, target, hub, reload_ctx):
    """DP12c: the port `tx` is restart class. rf-b simulate -> true restarts the AX.25 edge; the hypothetical queue of the simulated port is
    dropped with the process, so no text posted before the change is transmitted on rf-b after it. A control text then proves rf-b is live."""
    sim, case = reload_ctx, "DP18-r1"
    sim.rf.tx_guard(case)
    queued = queue_texts(sim, ["RQ1A", "RQ1B", "RQ1C"])
    before = children_state(sim)
    b_conn = sim.rf.b.connections
    t_change = time.monotonic()
    apply_tx(sim, "rf-b", True, "dvt DP18-r1 simulate -> true")
    after = wait_edge_restart(sim, before, case, target)
    assert dc.restarted(before, after), "the AX.25 edge was not restarted by the tx change: %s -> %s" % (before, after)
    fence_view_bounds(sim, target, case, "rf-b")                         # first: the view clears 13-16 s after the child start, the TNC wait must not eat it
    procs_origin(sim, target, case, before)                              # RFSAFE1c: the hub row carries the launcher's child start (after the 15 s of the view bounds it has caught up)
    assert sim.rf.b.wait(lambda t: t.connections > b_conn and t.is_connected(), 30.0), "the edge did not reconnect to rf-b after the restart"
    wait_unfenced(sim, target, case, "rf-b")
    time.sleep(WATCH_AFTER_CHANGE_S)
    late = [(body, f[0] - t_change) for body, _t0 in queued for f in frames_with(sim.rf.b, body, t_change)]
    note(target, case, "RECORD", "frames of the queued texts at rf-b after the change: %s" % (late,))
    assert not late, "texts queued BEFORE the change were transmitted on rf-b after it: %s" % (late,)
    control, t0 = post_text(sim, 0, "RC1")
    got = []
    end = time.monotonic() + tr.TX_WAIT_S + txguard.MAX_WAIT_S
    while not got and time.monotonic() < end:
        got = frames_with(sim.rf.b, control, t0)
        time.sleep(0.2)
    assert got, "the control text %r never reached rf-b after simulate -> true: the port is not live, so the silence above proves nothing" % control
    apply_tx(sim, "rf-b", "simulate", "dvt DP18-r1 back to simulate")                    # revert: the next cases expect the snippet's shape
    wait_edge_restart(sim, after, case + " revert", target)
    wait_unfenced(sim, target, case, "rf-a")


def test_dp18_r2_true_to_simulate_then_restore_from_history(rig, target, hub, reload_ctx):
    """DP12c: rf-a true -> simulate restarts the edge and nothing queued for the real port crosses the change; the configuration history restore
    (POST /config/history/{seq}/restore, base_etag) brings the original file, and the port transmits again."""
    sim, case = reload_ctx, "DP18-r2"
    sim.rf.tx_guard(case)
    first, t0 = post_text(sim, 0, "RQ2A")
    got = []
    end = time.monotonic() + tr.TX_WAIT_S
    while not got and time.monotonic() < end:
        got = frames_with(sim.rf.a, first, t0)
        time.sleep(0.2)
    assert got, "the first text never left rf-a: the queue case needs one frame on air"
    queued = [post_text(sim, i + 1, tag) for i, tag in enumerate(["RQ2B", "RQ2C"])]          # tx_per_min 2, burst 1: these wait in the queue
    before = children_state(sim)
    t_change = time.monotonic()
    apply_tx(sim, "rf-a", "simulate", "dvt DP18-r2 true -> simulate")
    after = wait_edge_restart(sim, before, case, target)
    assert dc.restarted(before, after), "the AX.25 edge was not restarted by the tx change: %s -> %s" % (before, after)
    procs_origin(sim, target, case, before)                              # RFSAFE1c (DVT_RFSAFE1C=on): the hub row carries the launcher's child start
    time.sleep(WATCH_AFTER_CHANGE_S)
    late = [(body, f[0] - t_change) for body, _t0 in queued for f in frames_with(sim.rf.a, body, t_change)]
    note(target, case, "RECORD", "frames of the queued texts at rf-a after the change: %s" % (late,))
    assert not late, "texts queued BEFORE the change were transmitted on rf-a after it: %s" % (late,)
    # restore: the newest history entry is the file saved before the apply (admin.md: "last 50 files before each apply")
    admin = sim.admin_adm
    admin.step_up()
    hist = admin.get(admin.path("/config/history"), expect=None)
    seqs = dc.history_seqs(hist.json() if hist.status == 200 else {})
    if not seqs:
        note(target, case, "FINDING", "GET config/history gave no sequence numbers (HTTP %d); falling back to the reverse change: %s" % (hist.status, hist.text()[:200]))
        apply_tx(sim, "rf-a", True, "dvt DP18-r2 revert (no history)")
    else:
        etag, _w = config_state(sim)
        resp = admin.post(admin.path("/config/history/%d/restore" % seqs[-1]), {"base_etag": etag, "mode": "apply", "comment": "dvt DP18-r2 restore"}, expect=None)
        note(target, case, "RECORD", "restore of history entry %d answered HTTP %d" % (seqs[-1], resp.status))
        assert resp.status == 200, "restore of history entry %d answered HTTP %d: %s" % (seqs[-1], resp.status, resp.text()[:300])
    wait_edge_restart(sim, after, case + " restore", target)
    wait_unfenced(sim, target, case, "rf-a")
    control, t0 = post_text(sim, 0, "RC2")
    got = []
    end = time.monotonic() + tr.TX_WAIT_S + txguard.MAX_WAIT_S
    while not got and time.monotonic() < end:
        got = frames_with(sim.rf.a, control, t0)
        time.sleep(0.2)
    assert got, "after the restore the real port rf-a did not transmit the control text %r: the original file is not back" % control


# ================================================================ DP18-r3: a reload that breaks an RF copy dependency is refused

RELOAD_REFUSALS = (
    ("tx_off", "port tx -> false", "/edges/ax25/port/%d/tx", False, "RF copy requires an enabled TX-capable AX.25 port"),
    ("callsign", "AX.25 station call differs from the APRS-IS login", "/edges/ax25/callsign", "N0TEST-6",
     "RF copy requires the APRS-IS gateway and AX.25 station callsigns to match"),
)


def aprsis_child_state(sim):
    admin = sim.admin
    resp = admin.get(admin.path("/status"), expect=None)
    return dc.child_state(resp.json() if resp.status == 200 else {}, "dv-edge-aprsis")


@pytest.mark.parametrize("cid,what,path,value,message", RELOAD_REFUSALS, ids=[r[0] for r in RELOAD_REFUSALS])
def test_dp18_r3_a_reload_that_breaks_an_rf_copy_rule_is_refused(cid, what, path, value, message, rig, target, hub, reload_ctx):
    """DP18b (DV-Router d3c6e531, config.md "What the children get"): the launcher validates the COMPLETE model on reload and refuses one that breaks a
    cross-section rule, publishing nothing. With objects that have `rf_ports` (run R or F, dp18-rf.toml), a change through POST /api/v1/admin/config/changes
    that disables the TX of rf-a or gives the AX.25 edge another station call is refused with the launcher's diagnostic; the configuration etag, the
    APRS-IS child (same pid, restarts unchanged, up) and the AX.25 child (not restarted) stay. Needs DVT_SUT_CONFIG_RW=1 (else the skip names it)."""
    sim, case = reload_ctx, "DP18-r3-" + cid
    shp = dc.origin_shape(sim.doc)
    if shp is None or not any(r.rf_ports for r in shp.reports):
        pytest.skip("no object or beacon with rf_ports in this run: add dp18-rf.toml with dp18-rf-floor.toml (or dp18-rf-objects.toml) (docs/dp18-cases.md)")
    admin = sim.admin_adm
    etag, writable = config_state(sim)
    if not writable:
        pytest.skip("the configuration file is not writable (the single-file :ro mount of the rig): run with DVT_SUT_CONFIG_RW=1")
    if "%d" in path:
        path = path % dc.port_index(sim.doc, shp.reports[0].rf_ports[0])
    aprsis0, ax0 = aprsis_child_state(sim), children_state(sim)
    admin.step_up()
    resp = admin.post(admin.path("/config/changes"), dc.replace_request(path, value, etag, "dvt DP18-r3 " + cid), expect=None)
    if resp.status == 409 and "read_only" in resp.text():
        pytest.skip("the configuration change answered 409 (read_only): run with DVT_SUT_CONFIG_RW=1. Reply: %s" % resp.text()[:200])
    time.sleep(5.0)
    etag_after, _w = config_state(sim)
    aprsis1, ax1 = aprsis_child_state(sim), children_state(sim)
    note(target, case, "RECORD", "%s: HTTP %d, body %s; etag %s -> %s; dv-edge-aprsis %s -> %s; %s %s -> %s" % (
        what, resp.status, resp.text()[:400], etag, etag_after, aprsis0, aprsis1, txguard.AX25_CHILD, ax0, ax1))
    assert resp.status >= 400 or "invalid" in resp.text().lower(), "the change was accepted (HTTP %d): %s" % (resp.status, resp.text()[:300])
    assert message in resp.text(), "the refusal does not carry the launcher's diagnostic %r: HTTP %d %s" % (message, resp.status, resp.text()[:400])
    assert etag_after == etag, "a refused change moved the configuration etag: %s -> %s" % (etag, etag_after)
    assert aprsis1 and aprsis0 and aprsis1.get("pid") == aprsis0.get("pid") and (aprsis1.get("restarts") or 0) == (aprsis0.get("restarts") or 0), (
        "the APRS-IS child changed after a refused reload: %s -> %s" % (aprsis0, aprsis1))
    assert not dc.restarted(ax0, ax1), "the AX.25 child restarted after a refused reload: %s -> %s" % (ax0, ax1)


# ================================================================ DP18-o: several RF objects on afsk1200-shared

def strict_or_skip(what):
    if not dc.strict_on():
        pytest.skip("%s needs the strict switch DVT_DP18STRICT=on (it fails on the images where dv-edge-aprsis exits on rf_ports: docs/dp18-cases.md, \"Strict\")" % what)


@pytest.fixture(scope="module")
def objects_run(request, target, hub, admin_session):
    """ONE observation for every DP18-o case: the run waits DVT_DP18_CYCLES cycles with the tester logged in at the private aprsc (when the run
    has one), then reads the frames at the TNC of the first RF port and the counter deltas. Skips with the reason without the shape."""
    shp = dc.origin_shape(p1.read_config(target))
    if shp is None or len(shp.reports) < 3 or not all(r.rf_ports for r in shp.reports):
        pytest.skip("no run with a beacon and several objects with RF copies: add dp15-aprsis.toml, dp18-rf.toml and dp18-rf-objects.toml "
                    "(or dp18-rf-floor.toml) (docs/dp18-cases.md)")
    admin = admin_session("operator")
    schema = admin.get(admin.path("/schema"), expect=None)
    if schema.status != 200 or not dc.schema_has_dp18(schema.text()):
        pytest.skip("the image does not know DP12c / DP18 (no `simulate`, `beacon`, `objects` in the configuration schema)")
    tnc = request.getfixturevalue("rf")
    port = shp.reports[0].rf_ports[0]
    interval = min(r.interval_s for r in shp.reports)
    last_due = max(r.first_due_s for r in shp.reports)
    end_uptime = interval * (CYCLES - 1) + last_due + 60.0
    uptime = txguard.uptime_reader(admin)()
    wait = max(60.0, end_uptime - uptime) if uptime is not None else end_uptime
    samples = metrics.parse(hub.text())
    base = dc.rf_tx_counts(samples, port)
    base_origin = dc.origin_counts(samples)
    base_queue = dc.queue_len(samples, port)
    base_worker = dc.worker_rf_counts(samples)
    base_fenced = dc.edge_drops(samples, "rf_fenced")
    aprsc_host, aprsc_why = None, ""
    try:
        aprsc_host = request.getfixturevalue("aprsc")
    except pytest.skip.Exception as exc:
        aprsc_why = str(exc)
    t_start = time.monotonic()
    note(target, "DP18-o", "RECORD", "%d reports, interval %d s, %d cycle(s): waiting %.0f s (node uptime %s s); aprsc %s; strict %s" % (
        len(shp.reports), interval, CYCLES, wait, uptime, aprsc_host or ("none: " + aprsc_why), dc.strict_on()))
    lines = None
    if aprsc_host:
        with p1.aprsc_login(aprsc_host) as session:
            time.sleep(wait + 30.0)                            # the last counters are published every 10 s
            seen = session.snapshot()
        lines = []
        for at, line in seen:
            parsed = ai.parse_line(line)
            key = dc.classify(parsed, shp) if parsed else None
            if key:
                lines.append((dc.wall_of(at), key, parsed))
    else:
        time.sleep(wait + 30.0)
    frames = tnc.a.data_frames(t_start)
    objs = dc.object_frames(frames, ax25)
    events = dc.rf_events(frames, ax25)
    beacons = [e.at for e in events if e.key == "beacon"]
    times = sorted([at for at, *_ in objs] + beacons)
    samples = metrics.parse(hub.text())
    now = dc.rf_tx_counts(samples, port)
    delta = {k: v - base.get(k, 0) for k, v in now.items() if v - base.get(k, 0)}
    origin = {k: v - base_origin.get(k, 0) for k, v in dc.origin_counts(samples).items() if v - base_origin.get(k, 0)}
    queue_now = dc.queue_len(samples, port)
    return types.SimpleNamespace(shape=shp, port=port, interval=interval, wait=wait, uptime=uptime, frames=frames, objs=objs, beacons=beacons, events=events,
                                 times=times, gaps=dc.spread_gaps(times), delta=delta, origin=origin, base_queue=base_queue, queue_now=queue_now,
                                 aprsc_lines=lines, aprsc_why=aprsc_why, base=base, base_origin=base_origin, base_worker=base_worker, base_fenced=base_fenced)


def test_dp18_o_object_copies_are_spread_and_drops_are_counted(rig, target, hub, objects_run):
    """data.md 9.5 / review 232 finding 1: with the 35 s stagger the reports never share a tick, so the RF copies are released spread over the
    interval; what the beacon class share (1/8 of 5 % per 600 s on afsk1200-shared) cannot carry is counted dropped_*, and the TNC sees
    exactly the frames counted sent. The dropped objects differ between cycles (recorded)."""
    case = "DP18-o"
    run = objects_run
    shp, port, interval, delta, times, gaps = run.shape, run.port, run.interval, run.delta, run.times, run.gaps
    dropped = dc.drops(delta)
    sent = dc.result_total(delta, "sent") + dc.result_total(delta, "sent_over_budget")
    note(target, case, "RECORD", "frames at the TNC: %d (%d objects, %d beacons); gaps between releases %s s; dv_edge_rf_tx_total deltas %s; drops %s; "
         "dv_pkt_origin_total deltas %s; dv_edge_rf_queue %s -> %s" % (len(times), len(run.objs), len(run.beacons), gaps, delta, dropped, run.origin,
                                                                      run.base_queue, run.queue_now))
    assert times, "no beacon or object frame reached the TNC of %s in %.0f s" % (port, run.wait)
    assert min(gaps, default=dc.STAGGER_S) >= dc.STAGGER_S - STAGGER_TOLERANCE_S, (
        "two releases %.1f s apart: the reports are not spread by the 35 s stagger (smallest gap allowed %.0f s)" % (min(gaps), dc.STAGGER_S - STAGGER_TOLERANCE_S))
    assert len(times) == sent, "the TNC saw %d frames, the counters say sent %d" % (len(times), sent)
    assert dropped, "nothing was dropped although %d reports share a class budget of about four frames: counters %s" % (len(shp.reports), delta)
    first_beacon = min(run.beacons) if run.beacons else min(times)
    per_cycle = {}
    for at, name, _src, _info in run.objs:
        per_cycle.setdefault(int((at - first_beacon) // interval), set()).add(name)
    note(target, case, "RECORD", "objects transmitted per cycle: %s" % ({c: sorted(v) for c, v in sorted(per_cycle.items())},))
    if len(per_cycle) >= 2:
        sets = [frozenset(v) for _c, v in sorted(per_cycle.items())]
        if len(set(sets)) == 1:
            note(target, case, "FINDING", "the same objects were transmitted in every cycle (%s): the dropped ones are always the same" % sorted(sets[0]))
        else:
            note(target, case, "RECORD", "the transmitted objects differ between cycles: the drops do not always hit the same object")
    else:
        note(target, case, "RECORD", "only %d cycle observed: DVT_DP18_CYCLES=2 or more compares which objects are dropped" % len(per_cycle))


def test_dp18_o_strict_every_report_finds_both_edges(rig, target, objects_run):
    """DVT_DP18STRICT=on. `dv_pkt_origin_total{result=no_edge}` and `{result=no_rf_edge}` stay 0 (data.md 13.1: no_edge = the worker finds no APRS-IS
    edge, no_rf_edge = RF copies selected and no AX.25 edge). KNOWN PRODUCT DEFECT on the images before DP18b (DV-Router d3c6e531): dv-edge-aprsis exits with a configuration
    error as soon as `rf_ports` is set (crates/edge/aprsis/src/shell.rs validates the file with Config::default() plus only edges.aprsis, so the
    AX.25 port the copy names does not exist there); the worker then counts no_edge for every report and nothing reaches the aprsc."""
    strict_or_skip("DP18-o no_edge")
    run = objects_run
    moved = {"%s/%s" % k: v for k, v in run.origin.items() if k[1] in ("no_edge", "no_rf_edge")}
    note(target, "DP18-o", "RECORD", "strict: dv_pkt_origin_total no_edge / no_rf_edge deltas %s (generated %s)" % (
        moved, {"%s" % k[0]: v for k, v in run.origin.items() if k[1] == "generated"}))
    assert not moved, ("dv_pkt_origin_total moved for %s: the worker found no edge for some reports. With rf_ports set this is the known dv-edge-aprsis "
                       "defect (it exits on a configuration error); see docs/dp18-cases.md, \"Strict\"" % moved)


def test_dp18_o_strict_nothing_is_sent_over_budget(rig, target, objects_run):
    """DVT_DP18STRICT=on. `dv_edge_rf_tx_total{result=sent_over_budget}` is the fixed label of the `ident` class only (data.md 10.8 item 8): node
    beacons and objects must never use it, so it stays 0 on the port (a beacon that is over its class share is dropped, not sent over budget)."""
    strict_or_skip("DP18-o sent_over_budget")
    run = objects_run
    over = dc.result_total(run.delta, "sent_over_budget")
    note(target, "DP18-o", "RECORD", "strict: sent_over_budget delta %d on %s (all deltas %s)" % (over, run.port, run.delta))
    assert over == 0, "%d frames were sent over the budget on %s: %s" % (over, run.port, {k: v for k, v in run.delta.items() if k[1] == "sent_over_budget"})


def test_dp18_o_strict_every_generated_copy_is_accounted_for(rig, target, hub, objects_run):
    """DVT_DP18STRICT=on. Conservation on the RF port: the RF copies the worker generated (dv_pkt_origin_total{result=generated}, one per report and
    named RF port) equal sent + simulated + sent_over_budget + dropped_* of dv_edge_rf_tx_total{port} plus no_rf_edge (since d3c6e531 one count per withheld RF copy) plus the frames still queued (the change of the
    gauge dv_edge_rf_queue{port}); `delayed` is not terminal. The counters are published every 10 s, so the sums are re-read for up to 45 s. Terms
    that cannot be read are recorded (a missing gauge: only terminal <= generated is checked; a report that names another port: skipped).
    DVT_TXCNT1B=on (DV-Router 051a4516, data.md 10.8: generated = sent + dropped_* + canceled before release + still queued) adds the cancellation
    term dv_edge_drops_total{reason=rf_fenced} as an upper bound on the unaccounted copies (dc.conservation_problems); without it the equation is
    exactly the one above and rf_fenced is only recorded (older images leave a fenced copy queued, so it would count twice)."""
    strict_or_skip("DP18-o conservation")
    txcnt1b = dc.txcnt1b_on()
    run = objects_run
    end = time.monotonic() + 45.0
    while True:
        samples = metrics.parse(hub.text())
        now = dc.rf_tx_counts(samples, run.port)
        delta = {k: v - run.base.get(k, 0) for k, v in now.items() if v - run.base.get(k, 0)}
        origin = {k: v - run.base_origin.get(k, 0) for k, v in dc.origin_counts(samples).items() if v - run.base_origin.get(k, 0)}
        queue = dc.queue_len(samples, run.port)
        queue_delta = None if queue is None or run.base_queue is None else queue - run.base_queue
        copies, why = dc.copies_expected(origin, run.shape, run.port)
        if copies is None:
            pytest.skip("conservation not computable: %s" % why)
        fenced = dc.edge_drops(samples, "rf_fenced") - run.base_fenced
        problems = dc.conservation_problems(copies, delta, queue_delta, dc.no_rf_edge_total(origin), fenced if txcnt1b else None)
        if not problems or time.monotonic() > end:
            break
        time.sleep(5.0)
    worker = {k: v - run.base_worker.get(k, 0) for k, v in dc.worker_rf_counts(samples).items() if v - run.base_worker.get(k, 0)}
    note(target, "DP18-o", "RECORD", "strict conservation%s: " % (" (TXCNT1b equation)" if txcnt1b else "")
         + dc.conservation_terms(origin, copies, delta, run.base_queue, queue, worker, fenced, txcnt1b))
    assert not problems, "; ".join(problems)


def test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(rig, target, hub, objects_run):
    """DVT_TXCNT1=on (DV-Router unit TXCNT1, commit 7710756, data.md 10.8 "Built (TXCNT1)"). `dv_edge_rf_tx_total{result=delayed}` counts the FIRST
    hold of one queued copy, once whatever the number of dispatch polls or hold-reason changes; it is not terminal and not part of the conservation.
    Over the delta summed over the classes of the port: UPPER delayed <= RF copies expected (+ the copies already queued at the start, the gauge
    baseline), LOWER delayed >= dropped_ttl (- the baseline queue): a copy that expired waited its whole TTL, so it was held (see
    dc.txcnt1_problems for why still-queued, dropped_queue and sent copies give no lower bound). On wip-125de6f it counted every scheduler re-check
    (run R of DP18-o: +8932 in 27 minutes with 7 frames waiting): this case FAILS there, which is why it is switch-gated. rf_fenced is recorded
    (dv_edge_drops_total, not a result of this counter). Same observation as the DP18-o cases, no extra run."""
    if not dc.txcnt1_on():
        pytest.skip("DP18-o txcnt1 needs the switch DVT_TXCNT1=on (it fails on the images that count every scheduler re-check: docs/dp18-cases.md)")
    run = objects_run
    samples = metrics.parse(hub.text())
    now = dc.rf_tx_counts(samples, run.port)
    delta = {k: v - run.base.get(k, 0) for k, v in now.items() if v - run.base.get(k, 0)}
    origin = {k: v - run.base_origin.get(k, 0) for k, v in dc.origin_counts(samples).items() if v - run.base_origin.get(k, 0)}
    queue = dc.queue_len(samples, run.port)
    queue_delta = None if queue is None or run.base_queue is None else queue - run.base_queue
    copies, why = dc.copies_expected(origin, run.shape, run.port)
    if copies is None:
        pytest.skip("delayed bound not computable: %s" % why)
    delayed = dc.result_total(delta, "delayed")
    ttl = dc.result_total(delta, "dropped_ttl")
    fenced = dc.edge_drops(samples, "rf_fenced") - run.base_fenced
    note(target, "DP18-o", "RECORD", "txcnt1: delayed delta %d; copies expected %d; dropped_ttl %d; dropped_queue %d; sent %d; sent_over_budget %d; simulated %d; "
         "queue gauge base %s now %s delta %s; frames at the TNC %d; rf_fenced edge drops %d (recorded, no bound); bounds: %s <= delayed <= copies + queue base" % (
             delayed, copies, ttl, dc.result_total(delta, "dropped_queue"), dc.result_total(delta, "sent"), dc.result_total(delta, "sent_over_budget"),
             dc.result_total(delta, "simulated"), run.base_queue, queue, queue_delta, len(run.times), fenced,
             "dropped_ttl - queue base" if run.base_queue is not None else "none (queue base unreadable)"))
    problems = dc.txcnt1_problems(delayed, copies, ttl, run.base_queue)
    assert not problems, "; ".join(problems)


def test_dp18_o_txcnt1b_result_labels_are_the_documented_set(rig, target, hub, objects_run):
    """DVT_TXCNT1B=on (DV-Router 051a4516, data.md 10.8 item 8 and 13.1). Every `result` label of dv_edge_rf_tx_total on the hub (any port, any kind)
    belongs to the documented set sent, simulated, sent_over_budget, delayed, dropped_ttl, dropped_queue, dropped_exceeds_quota, dropped_too_long;
    dropped_budget, dropped_quota and dropped_gap were removed (budget, quota and gap holds wait as `delayed`, an expired hold is dropped_ttl). Same
    observation as the other DP18-o cases (the fixture makes sure the RF port has run), no extra run."""
    if not dc.txcnt1b_on():
        pytest.skip("DP18-o txcnt1b labels need the switch DVT_TXCNT1B=on (an older image still exports the removed labels: docs/dp18-cases.md)")
    seen = dc.rf_tx_result_labels(metrics.parse(hub.text()))
    note(target, "DP18-o", "RECORD", "txcnt1b: dv_edge_rf_tx_total result labels seen %s; documented %s" % (seen, list(dc.TXCNT1B_RESULTS)))
    if not seen:
        pytest.skip("the hub has no dv_edge_rf_tx_total series, so the label set cannot be checked")
    problems = dc.result_label_problems(seen)
    assert not problems, "; ".join(problems)


def test_dp18_o_f_the_same_report_is_not_sent_twice_within_the_interval(rig, target, objects_run):
    """The interval floor (600 s) at the RF side (data.md 9.5: at most once per interval, no catch-up burst). The HMS stamp of an object is the time
    the report was generated, so two frames of ONE object at the TNC must have stamps at least interval - 2 s apart however long the queue held the
    first (a retransmission would show gap 0). That is always asserted for the objects seen at least twice. Under DVT_DP18STRICT=on the release
    times at the TNC must be at least interval - 15 s apart too, the beacon included (a queue delay can shorten that gap, so it is a strict check
    and recorded otherwise). Needs two cycles (DVT_DP18_CYCLES, default 2): with the eleven reports of dp18-rf-objects.toml most are dropped and
    few repeat, so the clean run is dp18-rf-floor.toml (beacon and two objects, about four frames of share: nothing dropped)."""
    run = objects_run
    problems, rows = dc.floor_report(run.events, run.interval, arrival=dc.strict_on())
    note(target, "DP18-o-f", "RECORD", "interval %d s; reports seen at least twice at the TNC: %s" % (run.interval, rows))
    if not rows:
        pytest.skip("no report was transmitted twice at the TNC of %s in %d cycle(s): use dp18-rf-floor.toml (beacon and two objects, nothing dropped) and "
                    "DVT_DP18_CYCLES=2 or more" % (run.port, CYCLES))
    if dc.strict_on():
        few = sorted({"beacon" if r.kind == "beacon" else "object:" + r.name for r in run.shape.reports} - {r["key"] for r in rows})
        note(target, "DP18-o-f", "RECORD", "strict: reports NOT seen twice (dropped or not yet due): %s" % (few,))
    assert not problems, "; ".join(problems)


def test_dp18_o_strict_the_aprsc_sees_several_objects(rig, target, objects_run):
    """DVT_DP18STRICT=on. The APRS-IS leg of the same run (run R shares it with the RF copies): the private aprsc delivers the node's own lines
    for more than one object, every configured report at least once, each as in DP18-a (source = gateway call, `TCPIP*` before the server's qAC,
    the information field byte for byte, the object stamp within 1 s of the read time). The RF copies must not stop the APRS-IS line. On the
    images before the fix of dv-edge-aprsis (exits on rf_ports) NOTHING arrives: this case fails there, which is the point of the switch."""
    strict_or_skip("DP18-o several objects at the aprsc")
    run = objects_run
    if run.aprsc_lines is None:
        pytest.skip("no private aprsc in this run (%s): run scripts/run.sh --profile web,aprs" % run.aprsc_why)
    problems, counts = dc.aprsc_problems(run.aprsc_lines, run.shape)
    note(target, "DP18-o", "RECORD", "strict: lines from the node at the aprsc per report %s" % (counts,))
    assert not problems, "; ".join(problems)
