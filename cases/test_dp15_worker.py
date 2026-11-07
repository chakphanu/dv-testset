# SPDX-License-Identifier: MIT
"""DV-Router data-channel cases of data.md 15.5 for the worker lifecycle (DP15 group 3): the data worker
`dv-data-worker` is killed with SIGKILL through the control channel (`kill-proc xlxd-sut dv-data-worker KILL`, unit
U1); the launcher respawns it after 0.5 s doubling to 5 s. Specification: docs/dp15-plan.md section 3.3; case table,
bounds, status and run command: docs/dp15-cases.md.

  DP15-3a  kill the worker during traffic: raw counters keep moving (`no_worker` while it is down), voice untouched
           (a D-STAR over arrives byte for byte with no gap, the M17 stream of the fake transcoder keeps flowing),
           web chat works (the POST is accepted and shown by the hub), the pending item is never rendered later, and a
           NEW item after the restart is rendered
  DP15-3b  restart: a NEW web text posted at the token is rendered within TOKEN_RENDER_S (M17 SMS) of the token; on D-STAR
           the text over starts within TOKEN_RENDER_S + IDLE_GAP_S; a text posted while the worker was down is never
           rendered, not even later (answers 4 and 5 of the DV-Router lead: "no history")

The token is the `data_worker_up` status event of the public SSE (data.md / status.md: `status` events carry `ev`,
`worker`, `kind`); the first successful `proc-stat` of the new pid is recorded as a second reference. Only
DV-Router answers these (DVT_SUT_PROFILE=dvrouter, DVT_SUT_LAUNCH=native) with the data worker on, the web profile and
the control channel; DVT_TRANSCODER_FAKE=1 gives the M17 voice of 3a. Every case skips with the reason when its
prerequisite is missing. Observations go to stdout and out/<run>/dp15-worker.txt ("RECORD", "FINDING").

NOT RUN ON A RIG yet (written against the sources only).
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import dstar_sd, metrics, slowdata, web, webload  # noqa: E402
from dvtest.proto import dextra  # noqa: E402

import data_support as ds  # noqa: E402
from data_support import (  # noqa: E402
    AT_ONCE_S, IDLE_GAP_S, PACER, ROOM, SseWatch, TOKEN_RENDER_S, ambe, build_over, dstar_listener, header_times,
    hub_session, m17_client, m17_voice_payloads, m17p_packets, over_by_my, pump_for, send_over_thread, sms_text, texts_of,
    write_note)

pytestmark = pytest.mark.rig

MODULE = ROOM
LOG = "dp15-worker.txt"
SETTING = "require_ident=%s" % os.environ.get("DVT_REQUIRE_IDENT", "true")
WORKER = "dv-data-worker"
TALKER_CALL, LISTEN_CALL = "N0TEST", "N0CALL"
ROUNDS = 3                      # 3b: the launcher backoff grows 0.5 s, 1 s, ...; a run of 10 s resets it
RUN_RESET_S = 11.0              # the worker must have run this long before a kill, so the backoff is 0.5 s again
CYCLE_WAIT_S = 40.0             # down and up token after a kill
DSTAR_TEXT_S = TOKEN_RENDER_S + IDLE_GAP_S         # answer 4: "2 s + idle gap" is the safe upper bound for a D-STAR text over
OVER_SUPERFRAMES = 30           # 3a: 30 x 420 ms = 12.6 s of voice
KILL_AT_S = 4.0                 # 3a: seconds after the header
VOICE_GAP_S = 0.25              # largest silence between two D-STAR voice frames at the listener (frames are 20 ms apart)
M17_GAP_S = 0.5                 # the same for the M17 stream of the transcoder (40 ms frames)
NO_WORKER_MIN_GAP_S = 1.5       # a down window at least this long must show `no_worker` raw units


def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    """Hub metrics with the data worker on (skips with the reason otherwise)."""
    yield from hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    """The web side: a reader (user, may not transmit), two posters (users that may transmit; two accounts so the
    per-account bucket of 1 per 5 s, burst 2 never blocks the item posted at the token) and an anonymous client factory."""
    return types.SimpleNamespace(op=web_operator, reader=web_user("N0DWR", may_tx=False).client,
                                 gap=web_user("N0DWA", may_tx=True), fresh=web_user("N0DWB", may_tx=True), anon=web_client)


@pytest.fixture(scope="module")
def worker(hub, rig_control):
    """The first sample of the running worker process (skips when there is none: the data worker is off)."""
    sample = worker_sample(rig_control)
    if sample is None:
        pytest.skip("no %s process in the SUT container: [data] is not enabled (data-ident.toml)" % WORKER)
    return sample


# ---------------------------------------------------------------- helpers

def sutip(rig):
    return rig.xlxd_sut


def worker_sample(rig_control):
    """webload.ProcSample of the worker, or None while no such process runs (proc-stat answers error 3)."""
    try:
        return webload.parse_procstat_out(rig_control.proc_stat("xlxd-sut", WORKER))
    except RuntimeError as exc:
        if "error 3" in str(exc):
            return None
        raise


def epoch_to_monotonic(epoch):
    return epoch - (time.time() - time.monotonic())


def kill_worker(rig_control, retry_s=10.0):
    """SIGKILL the worker (control channel); retries for `retry_s` while the launcher has not respawned it (error 3).
    Returns the KillResult."""
    end = time.monotonic() + retry_s
    while True:
        try:
            return rig_control.kill_proc("xlxd-sut", WORKER, "KILL")
        except RuntimeError as exc:
            if "error 3" not in str(exc) or time.monotonic() >= end:
                raise
            time.sleep(0.3)


def worker_events(watch, since=0.0):
    """[(arrival time, "data_worker_up" | "data_worker_down")] of the public SSE after `since`."""
    out = []
    for at, frame in watch.snapshot():
        if at < since:
            continue
        obj = web.sse_data(frame)
        if obj and obj.get("ev") in ("data_worker_up", "data_worker_down"):
            out.append((at, obj["ev"]))
    return out


def wait_cycle(watch, since, timeout=CYCLE_WAIT_S):
    """(t_down, t_up): arrival times of the first `data_worker_down` after `since` and of the first `data_worker_up` after
    that; None for the one that did not come in `timeout` seconds."""
    end = time.monotonic() + timeout
    t_down = None
    while True:
        events = worker_events(watch, since)
        downs = [at for at, ev in events if ev == "data_worker_down"]
        if downs:
            t_down = downs[0]
            ups = [at for at, ev in events if ev == "data_worker_up" and at > t_down]
            if ups:
                return t_down, ups[0]
        if time.monotonic() >= end:
            return t_down, None
        time.sleep(0.05)


def wait_new_pid(rig_control, old_pid, timeout=20.0):
    """(ProcSample, time.monotonic() of its reading) of the first process whose pid differs from `old_pid`, else (None, None)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sample = worker_sample(rig_control)
        if sample is not None and sample.pid != old_pid:
            return sample, epoch_to_monotonic(sample.t)
        time.sleep(0.3)
    return None, None


def post(poster, text, pace=True):
    """POST a room text as `poster` into module B; (time before, time after, status)."""
    if pace:
        PACER.wait(poster.callsign, "text")
    t0 = time.monotonic()
    resp = web.post_room_text(poster.client, MODULE, text, expect=None)
    return t0, time.monotonic(), resp.status


def m17_text_times(client, body):
    """Arrival times of the M17P SMS packets at `client` whose text contains `body`."""
    return [at for at, p in m17p_packets(client) if body in (sms_text(p) or "")]


def dstar_text_times(listener, body):
    """Arrival times of the headers of the text overs at `listener` whose decoded text contains `body`."""
    sids = {sid for sid, got in texts_of(slowdata.overs(listener.client.inbox)) if body in got["text"]}
    return [at for at, h in header_times(listener) if h["stream_id"] in sids]


def pump_until(seconds, listeners, until=None):
    """Drain the listeners for up to `seconds` (early out when until() holds)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for item in listeners:
            item.client.drain()
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else False


def counter(hub, family, **labels):
    return hub.total(family, **labels)


# ================================================================ 3a: kill during traffic

def test_dp15_3a_kill_the_worker_during_traffic(rig, target, hub, rig_control, web_ctx, worker):
    """data.md 15.5 (worker): "kill the worker during traffic: raw counters, voice untouched, web chat works". A D-STAR
    talker keys a 12.6 s over whose slow data carries the TX message in every superframe; 4 s in, the worker is killed
    (SIGKILL, control channel). Asserted: the D-STAR listener hears the over byte for byte with no silence longer than
    VOICE_GAP_S (data.md 4.1 "voice untouched"), the M17 stream of the fake transcoder (when there is one) has no gap
    longer than M17_GAP_S, the public SSE shows worker down then up, the pid changed, the raw counter family kept moving,
    `no_worker` rose when the down window lasted NO_WORKER_MIN_GAP_S or more, a web text posted while the worker was down
    is accepted (202) and shown in the room history, it is never rendered (no M17 SMS, no D-STAR text over, at the
    latest TEXT_OVER_LATE_S after the voice end), and a NEW web text posted at the token IS rendered on M17 at once."""
    case = "3a"
    PACER.wait(TALKER_CALL, "status")
    text = "DVT KILL %04d" % (int(time.time()) % 10000)
    gap_body, new_body = ds.nonce_body("G"), ds.nonce_body("N")
    before = hub.settle()
    raw_before = counter(hub, "dv_edge_raw_total")
    no_worker_before = counter(hub, "dv_edge_raw_total", result="no_worker")
    fields = dstar_sd.tx_message_fields(text, superframes=OVER_SUPERFRAMES)
    sid, packets = build_over(TALKER_CALL, fields, ambe(len(fields), case))
    sent = [(dextra.parse_voice_raw(p)["ambe"], dextra.parse_voice_raw(p)["slow"]) for p in packets[1:-1]]
    out = []
    with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, \
            dstar_listener(sutip(rig), module=MODULE, callsign=TALKER_CALL) as talker, \
            dstar_listener(sutip(rig), module=MODULE, callsign=LISTEN_CALL) as dlist, \
            m17_client(sutip(rig), callsign=TALKER_CALL, module=MODULE) as mlist:
        assert sse.ready(), "the public SSE gave no hello: %s" % sse.error
        t_start = time.monotonic()
        thread = send_over_thread(talker, packets, out)
        time.sleep(0.3 + KILL_AT_S)
        killed = kill_worker(rig_control)
        t_kill = killed.t_kill_monotonic
        note(target, case, "RECORD", "killed %s pid %s %.2f s after the header was sent" % (WORKER, killed.pids, t_kill - t_start))
        pump_for(0.2, dlist, mlist)
        # web chat while the worker is down (or restarting): accepted, shown by the hub, never rendered
        t_post_gap0, t_post_gap1, status = post(web_ctx.gap, gap_body)
        assert status == 202, "POST while the worker is down answered %s, want 202 (the web path does not need the worker)" % status
        pump_for(0.5, dlist, mlist)
        t_down, t_up = wait_cycle(sse, t_kill - 0.5)
        sample, t_new = wait_new_pid(rig_control, worker.pid)
        rows = web.room_messages(web_ctx.reader, MODULE)
        history = [r for r in rows if gap_body in (r.get("body") or "")]
        # the NEW item at the token (or, without a token, as soon as the new pid is there)
        t_token = t_up if t_up is not None else t_new
        if t_token is None:
            pytest.fail("the worker never came back within %.0f s of the kill (no token event, no new pid)" % CYCLE_WAIT_S)
        _a, _b, new_status = post(web_ctx.fresh, new_body, pace=False)
        t_new_post = time.monotonic()
        while thread.is_alive():
            pump_for(0.05, dlist, mlist)
        t_header, stamps = out[0]
        t_last = stamps[-1]
        pump_until(DSTAR_TEXT_S + 4.0 + 6.0, [dlist, mlist], until=lambda: bool(dstar_text_times(dlist, new_body)))
        pump_for(1.5, dlist, mlist)
        overs = slowdata.overs(dlist.client.inbox)
        d_times = [at for at, d in zip(dlist.client.inbox_times, dlist.client.inbox)
                   if (dextra.parse_voice_raw(d) or {}).get("stream_id") == sid]
        m_times = [at for at, d in zip(mlist.client.inbox_times, mlist.client.inbox) if d.startswith(b"M17 ") and len(d) == 54]
        m17_new = m17_text_times(mlist, new_body)
        m17_gap_text = m17_text_times(mlist, gap_body)
        d_new = dstar_text_times(dlist, new_body)
        d_gap = dstar_text_times(dlist, gap_body)
        m17_voice = len(m17_voice_payloads(mlist))
    gap = (t_up - t_down) if (t_up is not None and t_down is not None) else None
    note(target, case, "RECORD", "down event %s, up event %s, window %s s; new pid %s (old %s) read %s s after the token" % (
        None if t_down is None else "%.3f" % (t_down - t_kill), None if t_up is None else "%.3f" % (t_up - t_kill),
        None if gap is None else "%.3f" % gap, None if sample is None else sample.pid, worker.pid,
        None if (sample is None or t_up is None) else "%.3f" % (t_new - t_up)))
    # the worker came back as a new process
    assert sample is not None and sample.pid != worker.pid, "no new %s process after the kill (old pid %s)" % (WORKER, worker.pid)
    assert t_down is not None, "no data_worker_down status event on the public SSE"
    assert t_up is not None, "no data_worker_up status event after the kill within %.0f s" % CYCLE_WAIT_S
    # voice untouched: D-STAR byte for byte, no silence
    got = over_by_my(overs, TALKER_CALL)
    assert got is not None, "the listener heard no over of %s: %s" % (TALKER_CALL, [ds.describe(o) for o in overs])
    received = [(v["ambe"], v["slow"]) for v in got["voice"]]
    note(target, case, "RECORD", "voice: sent %d frames, listener got %d; largest silence %.3f s" % (
        len(sent), len(received), max([b - a for a, b in zip(d_times, d_times[1:])] or [0.0])))
    assert [a for a, _s in received] == [a for a, _s in sent], "AMBE frames differ (%d sent, %d received)" % (len(sent), len(received))
    different = [i for i, (r, s) in enumerate(zip(received, sent)) if r[1] != s[1]]
    assert len(received) == len(sent) and not different, "slow data differs at frames %s" % different[:6]
    steps = [b - a for a, b in zip(d_times, d_times[1:])]
    assert steps and max(steps) <= VOICE_GAP_S, "D-STAR voice paused %.3f s (bound %.2f s)" % (max(steps or [0]), VOICE_GAP_S)
    m_steps = [b - a for a, b in zip(m_times, m_times[1:])]
    if m_times:
        note(target, case, "RECORD", "M17 stream: %d packets (%d payloads of 16 bytes), largest silence %.3f s" % (
            len(m_times), m17_voice, max(m_steps or [0.0])))
        assert max(m_steps or [0.0]) <= M17_GAP_S, "M17 voice paused %.3f s (bound %.2f s)" % (max(m_steps), M17_GAP_S)
    else:
        note(target, case, "RECORD", "no M17 stream packets at the M17 listener (DVT_TRANSCODER_FAKE=1 gives the cross-mode voice); M17 voice not judged")
    # web chat works while it was down
    assert history, "the room history has no row with %r after a POST accepted with 202: %s" % (gap_body, [r.get("body") for r in rows[:3]])
    # the pending item is never rendered; the new one is
    assert new_status == 202, "the POST at the token answered %s" % new_status
    note(target, case, "RECORD", "NEW text: M17 SMS %s s after the POST, D-STAR text over %s s after the voice end; the gap text reached M17 %d / D-STAR %d times" % (
        None if not m17_new else "%.3f" % (m17_new[0] - t_new_post), None if not d_new else "%.3f" % (d_new[0] - t_last),
        len(m17_gap_text), len(d_gap)))
    assert not m17_gap_text and not d_gap, "the text posted while the worker was down was rendered (M17 %d, D-STAR %d); items published meanwhile are never rendered later (data.md 4.1)" % (len(m17_gap_text), len(d_gap))
    assert m17_new, "the NEW text %r posted at the token produced no M17 SMS" % new_body
    assert m17_new[0] - t_new_post <= AT_ONCE_S + TOKEN_RENDER_S, "NEW text reached M17 %.3f s after the POST" % (m17_new[0] - t_new_post)
    assert d_new, "the NEW text %r produced no D-STAR text over within the window" % new_body
    # counters: refreshed every 10 s
    after, moved_raw = hub.wait_for(before, lambda now, was: now["edge_raw.all"] > was["edge_raw.all"])
    change = ds.delta(after, before)
    raw_now = counter(hub, "dv_edge_raw_total")
    no_worker = counter(hub, "dv_edge_raw_total", result="no_worker") - no_worker_before
    has_ready = "dv_pkt_ready" in metrics.families(hub.text())
    ready, ready_ok = hub.wait_total("dv_pkt_ready", 1, timeout=ds.POLL_S) if has_ready else (None, False)
    note(target, case, "RECORD", "counter deltas %s; dv_edge_raw_total +%d (no_worker +%d); dv_pkt_ready %s" % (ds.moved(change), raw_now - raw_before, no_worker, ready))
    if ds.edge_counts_known():
        assert raw_now - raw_before >= 5, "dv_edge_raw_total moved by %d during a %d superframe over" % (raw_now - raw_before, OVER_SUPERFRAMES)
        if gap is not None and gap >= NO_WORKER_MIN_GAP_S:
            assert no_worker >= 1, "dv_edge_raw_total{result=no_worker} did not move in a %.2f s down window" % gap
        elif gap is not None:
            note(target, case, "RECORD", "down window %.3f s is under %.1f s: no_worker is not asserted" % (gap, NO_WORKER_MIN_GAP_S))
    if has_ready:
        assert ready_ok, "dv_pkt_ready is %s after the restart, want 1" % ready
    else:
        note(target, case, "RECORD", "no dv_pkt_ready family in the hub metrics")


# ================================================================ 3b: restart, renders resume within 2 s of the token

def test_dp15_3b_renders_resume_within_two_seconds_of_the_token(rig, target, hub, rig_control, web_ctx, worker):
    """data.md 15.5 (worker): "restart: renders resume within 2 s of the token". Per round (ROUNDS of them, each after the worker
    ran RUN_RESET_S so the backoff is 0.5 s again): SIGKILL the worker, post text G while it is down, wait for the
    `data_worker_up` event on the public SSE (T_token), post a NEW text N (other account) at once. Asserted: N reaches the
    M17 listener as an SMS within TOKEN_RENDER_S of T_token (answer 4: an idle room, the independent path), and the D-STAR
    listener gets a text over of N starting within TOKEN_RENDER_S + IDLE_GAP_S of T_token; G is never rendered, not
    even later (no history: answer 4/5). Recorded: the first proc-stat of the new pid relative to the token, the
    down window, the latency from the POST."""
    case = "3b"
    hub.settle()
    current = worker
    for round_no in range(1, ROUNDS + 1):
        tag = "%s r%d" % (case, round_no)
        gap_body, new_body = ds.nonce_body("G"), ds.nonce_body("N")
        with SseWatch(web_ctx.anon(), "/api/v1/public/events") as sse, \
                dstar_listener(sutip(rig), module=MODULE, callsign=LISTEN_CALL) as dlist, \
                m17_client(sutip(rig), callsign=TALKER_CALL, module=MODULE) as mlist:
            assert sse.ready(), "the public SSE gave no hello: %s" % sse.error
            pump_for(RUN_RESET_S, dlist, mlist)                         # the backoff is back at 0.5 s after a run of 10 s
            killed = kill_worker(rig_control)
            t_kill = killed.t_kill_monotonic
            t_gap0, t_gap1, status_gap = post(web_ctx.gap, gap_body)
            t_down, t_up = wait_cycle(sse, t_kill - 0.5)
            if t_up is None:
                pytest.fail("round %d: no data_worker_up event within %.0f s of the kill (down event: %s)" % (round_no, CYCLE_WAIT_S, t_down))
            # the NEW item at the token
            t_post0, t_post1, status_new = post(web_ctx.fresh, new_body, pace=False)
            pump_until(TOKEN_RENDER_S + 4.0, [dlist, mlist], until=lambda: bool(m17_text_times(mlist, new_body)))
            pump_until(DSTAR_TEXT_S + 3.0, [dlist, mlist], until=lambda: bool(dstar_text_times(dlist, new_body)))
            pump_for(IDLE_GAP_S + 2.0, dlist, mlist)                    # a late rendering of the gap text would show up now
            m17_new, m17_gap = m17_text_times(mlist, new_body), m17_text_times(mlist, gap_body)
            d_new, d_gap = dstar_text_times(dlist, new_body), dstar_text_times(dlist, gap_body)
        sample, t_seen = wait_new_pid(rig_control, current.pid, timeout=5.0)
        in_gap = t_down is not None and t_gap0 > t_down and t_gap1 < t_up
        note(target, tag, "RECORD", "kill at 0; down event +%s s, up event (token) +%.3f s; gap text POST at +%.3f s answered %s, %s; "
             "new pid %s read %s s after the token" % (
                 None if t_down is None else "%.3f" % (t_down - t_kill), t_up - t_kill, t_gap1 - t_kill, status_gap,
                 "inside the down window" if in_gap else "NOT inside the down window (worker was back or the down event late)",
                 None if sample is None else sample.pid, None if t_seen is None else "%.3f" % (t_seen - t_up)))
        note(target, tag, "RECORD", "NEW text POST %.3f s after the token answered %s; M17 SMS %s s after the token, %s s after the POST; "
             "D-STAR text over %s s after the token; gap text rendered M17 %d / D-STAR %d" % (
                 t_post1 - t_up, status_new, None if not m17_new else "%.3f" % (m17_new[0] - t_up),
                 None if not m17_new else "%.3f" % (m17_new[0] - t_post1), None if not d_new else "%.3f" % (d_new[0] - t_up),
                 len(m17_gap), len(d_gap)))
        assert status_gap == 202 and status_new == 202, "POST answers %s / %s, want 202" % (status_gap, status_new)
        assert m17_new, "round %d: no M17 SMS of the NEW text %r after the token" % (round_no, new_body)
        assert m17_new[0] - t_up <= TOKEN_RENDER_S, "round %d: first M17 render %.3f s after the token, bound %.1f s" % (
            round_no, m17_new[0] - t_up, TOKEN_RENDER_S)
        assert d_new, "round %d: no D-STAR text over of the NEW text %r" % (round_no, new_body)
        assert d_new[0] - t_up <= DSTAR_TEXT_S, "round %d: D-STAR text over %.3f s after the token, bound %.1f s (2 s + idle gap)" % (
            round_no, d_new[0] - t_up, DSTAR_TEXT_S)
        if d_new[0] - t_up > TOKEN_RENDER_S:
            note(target, tag, "RECORD", "the D-STAR text over started %.3f s after the token, beyond %.1f s (inside the idle gap allowance)" % (d_new[0] - t_up, TOKEN_RENDER_S))
        if in_gap:
            assert not m17_gap and not d_gap, "round %d: the text posted while the worker was down was rendered later (M17 %d, D-STAR %d)" % (
                round_no, len(m17_gap), len(d_gap))
        else:
            note(target, tag, "RECORD", "the gap text was not posted inside the down window: its rendering (M17 %d, D-STAR %d) is not judged" % (len(m17_gap), len(d_gap)))
        assert sample is not None and sample.pid != current.pid, "round %d: no new worker process" % round_no
        current = sample
        PACER.wait(web_ctx.fresh.callsign, "text")
