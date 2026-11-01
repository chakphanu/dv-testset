# SPDX-License-Identifier: MIT
"""How DV-Router's codec worker (dv-codec-ambed) and the room controller
behave when the transcoder misbehaves, using the fake extended AMBEd's modes
(DVT_TRANSCODER_FAKE=1 scripts/run.sh, docs/ambed-fake.md). Expectations come
from DV-Router docs/design/transcoding.md ("Controller transcoding", "dv-codec-ambed:
Lifecycle, Opening a session, Media, Writing a test double" and its failure
table). A point the document states is asserted through Fake.expect; a point
it leaves open, or where the sut differs, is a FINDING (printed, collected in
crossmode_findings.json) and does not fail the case.

Talker: a D-STAR (DExtra) client; listeners: a DMR client (codec 2, needs the
transcoder) and a DCS client (codec 1, the same codec: passthrough, never
waits for a worker). Every case ends with the fake back at its defaults and
the worker ready (Fake.recover) because a failure mode may leave the worker
probing CAPS.
"""
import threading
import time

import pytest

from dvtest import clients, crossmode
from dvtest.proto import ambed_ext as ext

from crossmode_fake import FRAMES, first_diff, prng, scenario, units_sent

pytestmark = [pytest.mark.rig, pytest.mark.crossmode]

TALKER, CROSS, SAME = "dstar", "dmr", "dcs"


def run(rig, frames=None, dsts=(CROSS, SAME), seed="fail"):
    frames = frames if frames is not None else prng(TALKER, seed=seed)
    return frames, scenario(rig, TALKER, list(dsts), frames)


def expected_cross(frames, dst=CROSS):
    return crossmode.expected_listener(TALKER, dst, frames).frames


def times(entries, kind, direction):
    return [e["t"] for e in entries if e["kind"] == kind and e["dir"] == direction]


def same_codec_untouched(fake, frames, res, fid):
    fake.expect(first_diff(res.frames[SAME], frames) is None, fid + "-passthrough",
                "same-codec output never waits for the worker (transcoding.md Principles)",
                first_diff(res.frames[SAME], frames))


def no_cross_audio(fake, res, fid):
    got = res.frames[CROSS]
    fake.expect(len(got) == 0, fid + "-no-cross-audio",
                "the target streams of a failed open are not produced",
                "%d DMR frames" % len(got))


def next_stream_works(rig, fake, fid):
    """After the failure mode the next stream opens a new session and is exact."""
    fake.recover()
    frames, res = run(rig, seed="after/" + fid)
    diff = first_diff(res.frames[CROSS], expected_cross(frames))
    fake.expect(diff is None, fid + "-next-stream",
                "recovery creates a new session at the next grant (transcoding.md Capacity)", diff)


# ---- OPEN answers ---------------------------------------------------------------

def test_open_busy(rig, fake):
    """BUSY is final: no cross audio, no second OPEN, no CS (none after BUSY),
    no CAPS re-probe (the worker stays ready); passthrough unaffected."""
    fake.set(open="busy")
    frames, res = run(rig, seed="busy")
    no_cross_audio(fake, res, "busy")
    same_codec_untouched(fake, frames, res, "busy")
    c = fake.counters()
    fake.expect(c.get("rx_open", 0) == 1, "busy-one-open", "one OPEN, never retried after BUSY", c.get("rx_open", 0))
    fake.expect(c.get("rx_cs", 0) == 0, "busy-no-cs", "no CS after BUSY", c.get("rx_cs", 0))
    fake.expect(c.get("rx_caps_request", 0) == 0, "busy-no-reprobe", "no CAPS re-probe after BUSY",
                c.get("rx_caps_request", 0))
    fake.expect(c.get("rx_data_request", 0) == 0, "busy-no-data", "no data after BUSY", c.get("rx_data_request", 0))
    next_stream_works(rig, fake, "busy")


def test_open_silent(rig, fake):
    """No answer: refused after 1050 ms, one immediate CAPS re-probe, no CS."""
    fake.set(open="silent")
    frames, res = run(rig, seed="silent")
    no_cross_audio(fake, res, "silent")
    same_codec_untouched(fake, frames, res, "silent")
    entries = fake.log()
    c = fake.counters()
    fake.expect(c.get("rx_open", 0) == 1, "silent-one-open", "one OPEN (the worker has no retransmit timer)",
                c.get("rx_open", 0))
    fake.expect(c.get("rx_cs", 0) == 0, "silent-no-cs", "no CS (no stream is known)", c.get("rx_cs", 0))
    opens, caps = times(entries, "open", "rx"), times(entries, "caps_request", "rx")
    fake.expect(len(caps) == 1, "silent-one-reprobe", "one CAPS re-probe", "%d CAPS requests" % len(caps))
    if opens and caps:
        dt = caps[0] - opens[0]
        print("silent open: CAPS re-probe %.3f s after the OPEN" % dt)
        fake.expect(1.0 <= dt <= 1.4, "silent-1050ms", "CAPS re-probe 1050 ms after the OPEN", "%.3f s" % dt)
    next_stream_works(rig, fake, "silent")


def test_open_late_beyond_window(rig, fake):
    """STD after 1050 ms: the open already failed (as silent), the late STD's
    stream is closed with CS and no data socket is bound."""
    fake.set(open="late:1300")
    frames, res = run(rig, seed="late")
    no_cross_audio(fake, res, "late")
    same_codec_untouched(fake, frames, res, "late")
    time.sleep(0.5)
    entries = fake.log()
    stds = [ext.parse_std(bytes.fromhex(e["hex"])) for e in entries if e["kind"] == "std"]
    cs = [ext.parse_cs(bytes.fromhex(e["hex"])) for e in entries if e["kind"] == "cs" and e["dir"] == "rx"]
    fake.expect(len(stds) == 1 and cs == [stds[0][0]], "late-cs-for-late-std",
                "CS for the late STD's stream", "STD %s, CS %s" % ([s[0] for s in stds], cs))
    c = fake.counters()
    fake.expect(c.get("rx_data_request", 0) == 0, "late-no-data", "no data on the late stream", c.get("rx_data_request", 0))
    fake.expect(c.get("rx_caps_request", 0) == 1, "late-one-reprobe", "one CAPS re-probe (as a silent open)",
                c.get("rx_caps_request", 0))
    next_stream_works(rig, fake, "late")


def test_open_late_within_window(rig, fake):
    """STD after 500 ms (< 1050 ms): a normal open, SessionReady after the
    delay, the buffered units are flushed in order; the output is complete."""
    fake.set(open="late:500")
    frames, res = run(rig, seed="slow")
    diff = first_diff(res.frames[CROSS], expected_cross(frames))
    fake.expect(diff is None, "slow-std-exact", "all units arrive, in order, after a slow STD", diff)
    sessions, _ = fake.sessions()
    fake.expect(len(sessions) == 1 and len(sessions[0].cs) == 1, "slow-std-one-session", "one session, one CS",
                "%d sessions" % len(sessions))
    if sessions:
        probs = crossmode.check_link(sessions[0], 1, units_sent(TALKER, frames), (2,))
        fake.expect(not probs, "slow-std-link", "requests are the units in order", "; ".join(probs))


def test_open_std_twice(rig, fake):
    """The same STD twice: the second is ignored, no CS, the session works."""
    fake.set(open="std_twice")
    frames, res = run(rig, seed="twice")
    diff = first_diff(res.frames[CROSS], expected_cross(frames))
    fake.expect(diff is None, "std-twice-exact", "session keeps working", diff)
    sessions, _ = fake.sessions()
    cs_total = fake.counters().get("rx_cs", 0)
    fake.expect(len(sessions) == 1 and cs_total == 1, "std-twice-one-cs", "no CS for the repeated STD (one CS at the end)",
                "%d sessions, %d CS" % (len(sessions), cs_total))


def test_open_std_then_other(rig, fake):
    """A second STD (same transaction, another stream id): CS for that other
    stream only, the session keeps its first stream."""
    fake.set(open="std_then_other")
    frames, res = run(rig, seed="other")
    diff = first_diff(res.frames[CROSS], expected_cross(frames))
    fake.expect(diff is None, "std-other-exact", "the first stream keeps working", diff)
    entries = fake.log()
    stds = [ext.parse_std(bytes.fromhex(e["hex"])) for e in entries if e["kind"] == "std"]
    cs = [ext.parse_cs(bytes.fromhex(e["hex"])) for e in entries if e["kind"] == "cs" and e["dir"] == "rx"]
    if len(stds) == 2:
        first, other = stds[0][0], stds[1][0]
        fake.expect(cs[:1] == [other], "std-other-cs-other-first", "CS for the other stream first",
                    "STD ids %s, CS %s" % ([first, other], cs))
        fake.expect(cs.count(other) == 1 and cs.count(first) == 1 and len(cs) == 2, "std-other-cs-once",
                    "CS for the other stream once, for the first stream once at the end", "CS %s" % cs)
    else:
        fake.expect(False, "std-other-two-stds", "two STDs", "%d" % len(stds))


# ---- data replies ---------------------------------------------------------------

@pytest.mark.parametrize("mode", ["silent", "wrong_source", "wrong_codec", "trailing_bytes"])
def test_data_reply_never_accepted(rig, fake, mode):
    """Every reply missing or dropped by the worker (no reply, other source
    port, wrong codec byte, trailing bytes): 400 ms after the first request
    the session ends (SessionEnded timeout, CS) and CAPS is re-probed; no
    cross audio; passthrough unaffected."""
    fake.set(data=mode)
    frames, res = run(rig, seed="data-" + mode)
    fid = "data-" + mode
    no_cross_audio(fake, res, fid)
    same_codec_untouched(fake, frames, res, fid)
    entries = fake.log()
    reqs, cs = times(entries, "data_request", "rx"), times(entries, "cs", "rx")
    fake.expect(len(cs) == 1, fid + "-cs", "CS when the session times out", "%d CS" % len(cs))
    if reqs and cs:
        dt = cs[0] - reqs[0]
        print("%s: CS %.3f s after the first request, %d requests" % (fid, dt, len(reqs)))
        fake.expect(0.39 <= dt <= 0.6, fid + "-400ms", "session ends 400 ms after the first unanswered request",
                    "%.3f s" % dt)
        sent_after = [t for t in reqs if t > cs[0] + 0.05]
        fake.expect(not sent_after, fid + "-no-requests-after-end", "no data requests after the session ended",
                    "%d requests after CS" % len(sent_after))
        fake.expect(len(reqs) < len(units_sent(TALKER, frames)), fid + "-stopped-early",
                    "the stream stops before the last unit", "%d of %d requests" % (len(reqs), len(units_sent(TALKER, frames))))
    fake.expect(fake.counters().get("rx_caps_request", 0) >= 1, fid + "-reprobe", "CAPS re-probe after a timeout",
                fake.counters().get("rx_caps_request", 0))
    next_stream_works(rig, fake, fid)


def test_data_delay_short(rig, fake):
    """Replies 100 ms late (< 400 ms): outputs arrive late, the session
    continues, the output is complete and in order."""
    fake.set(data="delay:100")
    frames, res = run(rig, seed="delay100")
    diff = first_diff(res.frames[CROSS], expected_cross(frames))
    fake.expect(diff is None, "delay100-exact", "late outputs still arrive complete and in order", diff)
    fake.expect(fake.counters().get("rx_caps_request", 0) == 0, "delay100-no-reprobe", "no CAPS re-probe",
                fake.counters().get("rx_caps_request", 0))


def test_data_delay_long(rig, fake):
    """Replies 600 ms late (>= 400 ms): as silent data, the session times out."""
    fake.set(data="delay:600")
    frames, res = run(rig, seed="delay600")
    no_cross_audio(fake, res, "delay600")
    same_codec_untouched(fake, frames, res, "delay600")
    entries = fake.log()
    reqs, cs = times(entries, "data_request", "rx"), times(entries, "cs", "rx")
    fake.expect(len(cs) == 1, "delay600-cs", "CS when the session times out", "%d CS" % len(cs))
    if reqs and cs:
        fake.expect(0.39 <= cs[0] - reqs[0] <= 0.6, "delay600-400ms", "session ends 400 ms after the first request",
                    "%.3f s" % (cs[0] - reqs[0]))


def test_data_reply_cut_after_complete_entry(rig, fake):
    """A reply cut after a complete entry is accepted, the missing outputs
    produce no output frame: the first output (DMR) is complete, the second
    (M17) never gets a unit; the session itself carries on."""
    fake.set(data="truncated_after_complete_entry:1")
    frames = prng(TALKER, seed="cut")
    res = scenario(rig, TALKER, [CROSS, "m17"], frames)
    diff = first_diff(res.frames[CROSS], expected_cross(frames))
    fake.expect(diff is None, "cut-first-output-exact", "the entries present are accepted", diff)
    want_m17 = set(crossmode.transform_units(1, 3, crossmode.source_units(TALKER, frames).voice))
    heard = [f for f in res.frames["m17"] if f in want_m17]
    fake.expect(not heard, "cut-missing-output-absent", "no unit for the output the reply left out",
                "%d of its units arrived" % len(heard))
    c = fake.counters()
    fake.expect(c.get("rx_cs", 0) == 1, "cut-one-cs", "one CS at the end (the session was not ended early)", c.get("rx_cs", 0))
    fake.expect(c.get("rx_caps_request", 0) == 0, "cut-no-reprobe", "no CAPS re-probe", c.get("rx_caps_request", 0))
    print("m17 listener frames with the second output missing: %d" % len(res.frames["m17"]))


# ---- capacity -------------------------------------------------------------------

def test_max_sessions_one_second_stream_busy(rig, fake):
    """The fake allows one live stream: a second concurrent stream's OPEN gets
    BUSY (accepted = 0), so it has no cross audio; the first stream is exact."""
    fake.set(max_sessions=1)
    first_frames = prng("dstar", 200, seed="cap-a")
    second_frames = prng("dmr", FRAMES, seed="cap-b")
    out = {}

    def first():
        out["a"] = scenario(rig, "dstar", ["dmr"], first_frames, module="D")

    def second_talk(talker, kind):
        # start only once the first stream holds the fake's one session
        fake.ctl.wait_for(lambda s: s["streams_live"] >= 1, 15.0, 0.1)
        return clients.talk(talker, kind, second_frames)

    def second():
        out["b"] = scenario(rig, "dmr", ["dstar"], second_frames, module="E", talk=second_talk)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    diff = first_diff(out["a"].frames["dmr"], expected_cross(first_frames))
    fake.expect(diff is None, "cap-first-exact", "the stream that got the session is exact", diff)
    fake.expect(len(out["b"].frames["dstar"]) == 0, "cap-second-no-audio", "BUSY: the second stream is not transcoded",
                "%d D-STAR frames" % len(out["b"].frames["dstar"]))
    c = fake.counters()
    fake.expect(c.get("busy_capacity", 0) == 1 and c.get("rx_open", 0) == 2, "cap-busy-once",
                "two OPENs, the second answered BUSY", "rx_open %s busy_capacity %s" % (c.get("rx_open"), c.get("busy_capacity")))


# ---- CAPS change, worker kill ---------------------------------------------------

def wait_new_caps_request(fake, after, seconds=20.0):
    """Wait for a CAPS request beyond `after` (a restarted worker asks at its
    first PONG)."""
    fake.ctl.wait_for(lambda s: s["counters"].get("rx_caps_request", 0) > after, seconds, 0.2)
    return fake.counters().get("rx_caps_request", 0)


def kill_worker(rig_control):
    try:
        return rig_control.kill_proc("xlxd-sut", "dv-codec-ambed", "KILL")
    except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
        pytest.fail("cannot kill dv-codec-ambed through the control channel: %s" % exc)


def test_worker_killed_mid_stream(rig, fake, rig_control):
    """KILL of dv-codec-ambed in the middle of a stream (transcoding.md,
    Capacity: token loss ends the target streams at once; passthrough is
    untouched; the launcher restarts the worker; the next grant opens a new
    session)."""
    fake.fault("worker-kill")
    frames = prng(TALKER, 150, seed="kill")
    marks = {}

    def talk(talker, kind):
        timer = threading.Timer(1.2, lambda: marks.update(kill=kill_worker(rig_control), t=time.time()))
        timer.start()
        try:
            return clients.talk(talker, kind, frames)
        finally:
            timer.cancel()
            timer.join()

    res = scenario(rig, TALKER, [CROSS, SAME], frames, talk=talk)
    want = expected_cross(frames)
    got = list(res.frames[CROSS])
    pad = 0
    while got and got[-1] == bytes(9) and pad < 2 and got[-1] != want[len(got) - 1]:
        got.pop()                                  # DMR pads a partial last burst with raw zero frames
        pad += 1
    same_codec_untouched(fake, frames, res, "kill")
    fake.expect(got == want[:len(got)], "kill-prefix", "what was delivered before the kill is exact", first_diff(got, want))
    fake.expect(len(got) < len(want), "kill-ends-stream", "the target stream ends at once", "%d of %d frames" % (len(got), len(want)))
    print("kill mid-stream: %d of %d DMR frames delivered, killed at %s" % (len(got), len(want), marks.get("t")))
    wait_new_caps_request(fake, 0, 25.0)
    time.sleep(2.0)
    next_stream_works(rig, fake, "kill")


def test_zz_caps_changed_worker_exits(rig, fake, rig_control):
    """A valid CAPS reply that differs from the first accepted one makes the
    worker exit (code 3); the launcher restarts it. Provoked by an unanswered
    OPEN (which triggers a CAPS re-probe) while the fake advertises another
    max outputs. Observed through the fake (a fresh CAPS request from the
    restarted process); the exit code is in the sut log. Last case: it ends
    with a KILL of the worker so the next one starts from the normal CAPS."""
    fake.set(open="silent", caps="changed")
    try:
        frames, res = run(rig, seed="caps")
        no_cross_audio(fake, res, "caps")
        fake.ctl.wait_for(lambda s: s["counters"].get("rx_caps_request", 0) >= 2, 20.0, 0.2)
        entries = fake.log()
        caps_t = times(entries, "caps_request", "rx")
        pings = times(entries, "ping", "rx")
        print("CAPS requests at %s; PINGs at %s" % ([round(t, 2) for t in caps_t], [round(t, 2) for t in pings]))
        fake.expect(len(caps_t) >= 2, "caps-changed-restart",
                    "worker exits (code 3) and the restarted process probes CAPS again",
                    "%d CAPS request(s)" % len(caps_t))
    finally:
        fake.ctl.reset()
        before = fake.counters().get("rx_caps_request", 0)
        kill_worker(rig_control)
        wait_new_caps_request(fake, before, 25.0)
        time.sleep(2.0)
        fake.dirty = False
