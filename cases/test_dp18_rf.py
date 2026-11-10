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

from dvtest import ax25, kiss_tnc, logscan, metrics, rfsafe1b, txguard  # noqa: E402

import data_support as ds  # noqa: E402
import dp18_support as dc  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_rf as tr  # noqa: E402
# the fake TNCs rf-a / rf-b (their `hub` argument is the fixture of THIS module) and the web side of tier A, reused by name
from test_dp15_rf import rf, web_ctx  # noqa: E402,F401

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
    may show a fence for up to about 24.5 s (wait up to 30 s); the cause and the timing are recorded, nothing is asserted. The origin is the child start taken
    from the launcher's since_ms (read at once; an unusable value falls back to now and the early bound is not checked)."""
    strict = rfsafe1b.strict_on()
    st = children_state(sim)
    since = (st or {}).get("since_ms")
    known = isinstance(since, (int, float)) and not isinstance(since, bool) and 0 <= since <= 30000
    origin = time.monotonic() - (since / 1000.0 if known else 0.0)
    trace = rfsafe1b.collect(txguard.ax25_reader(sim.admin, port), origin, max_wait_s=rfsafe1b.MAX_WAIT_S if strict else 30.0)
    judged = rfsafe1b.judge(trace.samples, strict=strict, check_early=known)
    note(target, case, "RECORD", "RFSAFE1b %s view of %s after the restart (child start from since_ms %s): %s; trace %s" % (
        "strict" if strict else "lenient (DVT_RFSAFE1B off)", port, since if known else "unknown", judged.summary, trace.state))
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


# ================================================================ DP18-o: several RF objects on afsk1200-shared

def test_dp18_o_object_copies_are_spread_and_drops_are_counted(rig, target, hub, request, admin_session):
    """data.md 9.5 / review 232 finding 1: with the 35 s stagger the reports never share a tick, so the RF copies are released spread over the
    interval; what the beacon class share (1/8 of 5 % per 600 s on afsk1200-shared) cannot carry is counted dropped_*, and the TNC sees
    exactly the frames counted sent. The dropped objects differ between cycles (recorded)."""
    case = "DP18-o"
    shp = dc.origin_shape(p1.read_config(target))
    if shp is None or len(shp.reports) < 3 or not all(r.rf_ports for r in shp.reports):
        pytest.skip("no run with a beacon and several objects with RF copies: add dp15-aprsis.toml, dp18-rf.toml and dp18-rf-objects.toml (docs/dp18-cases.md)")
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
    base = dc.rf_tx_counts(metrics.parse(hub.text()), port)
    base_origin = dc.origin_counts(metrics.parse(hub.text()))
    t_start = time.monotonic()
    note(target, case, "RECORD", "%d reports, interval %d s, %d cycle(s): waiting %.0f s (node uptime %s s)" % (len(shp.reports), interval, CYCLES, wait, uptime))
    time.sleep(wait)
    time.sleep(30.0)                                           # the last counters are published every 10 s
    frames = tnc.a.data_frames(t_start)
    objs = dc.object_frames(frames, ax25)
    beacons = [at for at, _ch, f in frames if (ax25.parse_frame(f)["info"][:1] == b"!")]
    times = sorted([at for at, *_ in objs] + beacons)
    gaps = dc.spread_gaps(times)
    now = dc.rf_tx_counts(metrics.parse(hub.text()), port)
    delta = {k: v - base.get(k, 0) for k, v in now.items() if v - base.get(k, 0)}
    dropped = dc.drops(delta)
    sent = dc.result_total(delta, "sent") + dc.result_total(delta, "sent_over_budget")
    origin = {k: v - base_origin.get(k, 0) for k, v in dc.origin_counts(metrics.parse(hub.text())).items() if v - base_origin.get(k, 0)}
    note(target, case, "RECORD", "frames at the TNC: %d (%d objects, %d beacons); gaps between releases %s s; dv_edge_rf_tx_total deltas %s; drops %s; "
         "dv_pkt_origin_total deltas %s" % (len(times), len(objs), len(beacons), gaps, delta, dropped, origin))
    assert times, "no beacon or object frame reached the TNC of %s in %.0f s" % (port, wait)
    assert min(gaps, default=dc.STAGGER_S) >= dc.STAGGER_S - STAGGER_TOLERANCE_S, (
        "two releases %.1f s apart: the reports are not spread by the 35 s stagger (smallest gap allowed %.0f s)" % (min(gaps), dc.STAGGER_S - STAGGER_TOLERANCE_S))
    assert len(times) == sent, "the TNC saw %d frames, the counters say sent %d" % (len(times), sent)
    assert dropped, "nothing was dropped although %d reports share a class budget of about four frames: counters %s" % (len(shp.reports), delta)
    first_beacon = min(beacons) if beacons else min(times)
    per_cycle = {}
    for at, name, _src, _info in objs:
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
