# SPDX-License-Identifier: MIT
"""XSTALE1c (DV-Router 5072bf57, image dv-router:wip-3f142cd): the egress accounting of an over whose listener leaves, and of a stream refused at its start.
docs/xstale1c-cases.md; the arithmetic and the verdicts are dvtest/xstale1c.py (on top of dvtest/xstale1.py), the protocol kits cases/xstale1_support.py and
cases/xstale1c_support.py. Switch DVT_XSTALE1C=on (the cases skip with the reason otherwise). Rig results: docs/xstale1c-cases.md "Results".

What the image promises (DV-Router's statement):

  * a listener that leaves mid-over settles its egress over at the leave: no lost frames are counted after it (before the fix the rest of the over counted
    as lost, for example sent 150 lost 350); real losses before the leave stay counted;
  * the same listener leaving and rejoining folds as joined, without double counting;
  * a stream refused at its start (no radio id) adds no dropped frames, only the no_radio_id drop event, once.

Cases (A = talker, B = the listener that leaves, both clients of the same server port of the edge under test unless noted; counters of that edge, as in
cases/test_xstale1.py):

  XS1c-a[usrp|p25|nxdn]   B listens from the start, A talks 8 s, B leaves 3 s in: one fold, lost / late / dropped / lossy / end_missing 0, not joined
  XS1c-b[p25,leave|stay]  as a), the talker a D-STAR client (DExtra, a cross-protocol over, transcoded to the P25 listener): lost 0 after the leave; end_missing of
                          the fold is a FINDING (not asserted); stay is the control, B listening to the End (complete, lost 0)
  XS1c-c[usrp|p25|nxdn]   B leaves 3 s into the over and is back (same socket, same address) 4 s in: one fold, joined, lost / late / dropped 0, B hears the rest
  XS1c-d[p25]             a D-STAR client whose callsign has no radio id talks 4 s to B: exactly one no_radio_id event, dropped 0, no egress over folded, B hears nothing

Needs DVT_XSTALE1C=on, DVT_RIG_CONTROL=1 (set by scripts/run.sh), DVT_SUT_PROFILE=dvrouter, DVT_SUT_LAUNCH=native, DVT_USRP_DYNAMIC=1 and rig/config/dv-router/soak-edges.toml
in DVT_SUT_CONFIG_EXTRA, like cases/test_xstale1.py (whose `edge` fixture and helpers are used here). Numbers of every case: out/<run>/xstale1c.json and xstale1c.txt.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import test_xstale1 as x1
from test_xstale1 import edge, kit_table, counted, hold, open_endpoint, pump_while        # noqa: F401  (fixtures and helpers of the XSTALE1 cases)
import xstale1c_support as xc
from dvtest import metrics, xstale1, xstale1c

pytestmark = [pytest.mark.rig]

LOG = "xstale1c.txt"
UNKNOWN_CALL = "N0UNK"                      # in no dmrid.dat (cases/proto/test_p25_network.py UNKNOWN_CALL): the egress edge finds no radio id
TALK_CALL = "N0CALL"                        # in rig/config/dmrid.dat
DSTAR_UNITS = 400                           # 8 s of D-STAR frames
REFUSED_S = 4.0


def note(target, case, tag, text):
    x1.ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not xstale1c.on():
        pytest.skip("DVT_XSTALE1C=on is not set (the image under test has XSTALE1c, DV-Router 5072bf57: dv-router:wip-3f142cd or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


def record(target, case, data):
    xc.write_json(target, case, data)


def wait_until(t0, seconds):
    """Sleep until `seconds` after t0 (monotonic); returns the seconds since t0 it is now."""
    time.sleep(max(0.0, t0 + seconds - time.monotonic()))
    return time.monotonic() - t0


class _AnyProc:
    def __contains__(self, _item):
        return True


# ---------------------------------------------------------------- a) the listener leaves mid-over

@pytest.mark.parametrize("proto", x1.PROTOCOLS)
def test_xs1c_a_listener_leaves_mid_over(edge, proto, target):
    """a) B listens from the start, A talks 8 s, B leaves 3 s in: the over settles at the leave, nothing is counted lost."""
    kit, read = edge
    case = "XS1c-a[%s]" % proto
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731
    before = xstale1.stable_baseline(read)
    snap = read.snapshot()
    a = open_endpoint(kit, 0, case)
    b = None
    try:
        b = open_endpoint(kit, 1, case)
        units = kit.units(xstale1.OVER_S)
        with ThreadPoolExecutor(max_workers=1) as pool:
            t0 = time.monotonic()
            job = pool.submit(a.burst, units, "xa", True)
            left_at = wait_until(t0, xstale1c.LEAVE_S)
            heard_before = b.heard()
            b.leave()
            sent = job.result()
        hold([a], x1.HOLD_S)
        own = a.heard()
    finally:
        for ep in (b, a):
            if ep is not None:
                ep.close()
    sent_before = int(left_at * xstale1.RATE)
    assert left_at <= xstale1c.LEAVE_S + xstale1c.LEAVE_LATE_S, \
        "%s: the leave came %.1f s into the over (wanted about %.0f s): a slow pacer, not a product result" % (case, left_at, xstale1c.LEAVE_S)
    assert xstale1c.hear_before(sent_before, heard_before), \
        "%s: B heard %d of the about %d units sent before it left: the listener was not served, not a product result" % (case, heard_before, sent_before)
    d = counted(read, before, case, 1, say,
                lambda: "%d units sent, B left %.2f s into the over after hearing %d, the talker heard %d of itself" % (sent, left_at, heard_before, own), snap)
    say("RECORD", "%d units sent, B left %.2f s into the over (about %d units) after hearing %d; the talker heard %d of itself"
        % (sent, left_at, sent_before, heard_before, own))
    record(target, case, {"proto": proto, "units_sent": sent, "left_at_s": round(left_at, 2), "heard_before_leave": heard_before, "talker_heard": own,
                          "delta": dict(d._asdict())})
    problems = xstale1c.judge_leave(d)
    if own:
        problems.append("the talker heard %d units of its own over" % own)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


# ---------------------------------------------------------------- b) real losses before the leave stay counted (D-STAR talker, P25 listener)

@pytest.mark.parametrize("stays", [False, True], ids=["leave", "stay"])
@pytest.mark.parametrize("proto", ["p25"])
def test_xs1c_b_cross_protocol_listener_leaves(edge, proto, stays, target):
    """b) a D-STAR talker (transcoded to the P25 listener) talks 8 s and B leaves 3 s in: nothing is counted lost after the leave. `stay` is the control: the
    same over with B listening to the End (complete, lost 0), which tells what the leave changes. Real losses before the leave cannot be made from outside on this
    path (docs/xstale1c-cases.md: three D-STAR counters left out with B staying count lost 0)."""
    kit, read = edge
    case = "XS1c-b[%s,%s]" % (proto, "stay" if stays else "leave")
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731
    before = xstale1.stable_baseline(read)
    snap = read.snapshot()
    talker = xc.DstarTalker(target.xlxd_sut, TALK_CALL, kit.module)
    b = None
    try:
        b = open_endpoint(kit, 1, case)
        if not talker.link():
            pytest.fail("%s: the D-STAR talker gets no link ACK on module %s: the DExtra edge is not serving" % (case, kit.module))
        with ThreadPoolExecutor(max_workers=1) as pool:
            t0 = time.monotonic()
            job = pool.submit(talker.over, DSTAR_UNITS, "b", (), True)
            left_at = wait_until(t0, xstale1c.LEAVE_S)
            heard_before = b.heard()
            if stays:
                sent, _ = pump_while(job, [b])
            else:
                b.leave()
                sent, _ = job.result()
        talker.pump(x1.HOLD_S)
        heard_total = b.heard() if stays else heard_before
    finally:
        for ep in (b, talker):
            if ep is not None:
                ep.close()
    sent_before = int(left_at * xstale1.RATE)
    assert left_at <= xstale1c.LEAVE_S + xstale1c.LEAVE_LATE_S, "%s: the leave came %.1f s into the over: a slow pacer, not a product result" % (case, left_at)
    assert xstale1c.hear_before(sent_before // 2, heard_before), \
        "%s: B heard %d of the about %d units sent before it left: the transcoded over did not reach the listener, not a product result" % (case, heard_before, sent_before)
    d = counted(read, before, case, 1, say,
                lambda: "%d D-STAR frames sent, B %s %.2f s into the over after hearing %d" % (sent, "was checked" if stays else "left", left_at, heard_before), snap)
    say("RECORD", "%d D-STAR frames sent, B %s %.2f s into the over after hearing %d (%d in all)" % (sent, "was checked" if stays else "left", left_at, heard_before, heard_total))
    record(target, case, {"proto": proto, "stays": stays, "frames_sent": sent, "left_at_s": round(left_at, 2), "heard_before_leave": heard_before,
                          "heard_total": heard_total, "delta": dict(d._asdict())})
    if d.end_missing and not stays:
        # The promise is about lost: the over is settled at the leave. The fold RESULT of a transcoded over whose only listener left is end_missing (the same over
        # with the listener staying folds complete, the same-edge leave of case a) folds complete): recorded as a finding, not asserted.
        say("FINDING", "the over folded as end_missing %g after the leave (complete %g); with the listener staying the same over folds complete" % (d.end_missing, d.complete))
    problems = xstale1c.judge_leave(d, allow=() if stays else ("end_missing",))
    assert not problems, "%s: %s" % (case, "; ".join(problems))


# ---------------------------------------------------------------- c) the same listener leaves and rejoins

@pytest.mark.parametrize("proto", x1.PROTOCOLS)
def test_xs1c_c_same_listener_leaves_and_rejoins(edge, proto, target):
    """c) B leaves 3 s into the over and is back 4 s in: the over folds once, as joined, nothing counted twice."""
    kit, read = edge
    case = "XS1c-c[%s]" % proto
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731
    before = xstale1.stable_baseline(read)
    snap = read.snapshot()
    a = open_endpoint(kit, 0, case)
    b = None
    try:
        b = open_endpoint(kit, 1, case)
        units = kit.units(xstale1.OVER_S)
        with ThreadPoolExecutor(max_workers=1) as pool:
            t0 = time.monotonic()
            job = pool.submit(a.burst, units, "xc", True)
            left_at = wait_until(t0, xstale1c.LEAVE_S)
            heard_before = b.heard()
            b.leave()
            wait_until(t0, xstale1c.REJOIN_S)
            linked = b.rejoin()
            back_at = time.monotonic() - t0
            pump_while(job, [b])
        sent = job.result()
        hold([a, b], x1.HOLD_S)
        heard = b.heard()
        own = a.heard()
    finally:
        for ep in (b, a):
            if ep is not None:
                ep.close()
    assert linked, "%s: B gets no answer when it links again (the edge served it 1 s before)" % case
    sent_before = int(left_at * xstale1.RATE)
    assert left_at <= xstale1c.LEAVE_S + xstale1c.LEAVE_LATE_S, "%s: the leave came %.1f s into the over: a slow pacer, not a product result" % (case, left_at)
    assert xstale1c.hear_before(sent_before, heard_before), \
        "%s: B heard %d of the about %d units sent before it left: not a product result" % (case, heard_before, sent_before)
    assert back_at <= xstale1c.REJOIN_S + xstale1.JOIN_LATE_S, \
        "%s: B was back only %.1f s into the over: the link is too slow for this case, not a product result" % (case, back_at)
    rest = xstale1.expected_rest(xstale1.OVER_S, back_at)
    d = counted(read, before, case, 1, say,
                lambda: "%d units sent, B left %.2f s in (heard %d), was back %.2f s in and heard %d of the %d to come" % (sent, left_at, heard_before, back_at, heard, rest), snap)
    say("RECORD", "%d units sent, B left %.2f s in after hearing %d, was back %.2f s in and heard %d of the %d to come; the talker heard %d of itself"
        % (sent, left_at, heard_before, back_at, heard, rest, own))
    record(target, case, {"proto": proto, "units_sent": sent, "left_at_s": round(left_at, 2), "back_at_s": round(back_at, 2), "heard_before_leave": heard_before,
                          "heard_after_rejoin": heard, "to_come": rest, "talker_heard": own, "delta": dict(d._asdict())})
    problems = xstale1c.judge_rejoin(d, heard, rest, sent, kit.slack)
    if own:
        problems.append("the talker heard %d units of its own over" % own)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


# ---------------------------------------------------------------- d) a stream refused at its start for want of a radio id

@pytest.mark.parametrize("proto", ["p25"])
def test_xs1c_d_refused_start_adds_no_dropped_frames(edge, proto, target, rig_control):
    """d) a D-STAR talker whose callsign has no radio id talks 4 s to a P25 listener: one no_radio_id event, dropped 0, no egress over folded."""
    kit, read = edge
    case = "XS1c-d[%s]" % proto
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731

    def refusals():
        return xstale1c.no_radio_id(metrics.parse(rig_control.metrics("xlxd-sut")))

    def reading():
        return read(), tuple(sorted(refusals().items()))

    before_counts, before_refused = xstale1.stable_baseline(reading)
    before_refused = dict(before_refused)
    snap = read.snapshot()
    talker = xc.DstarTalker(target.xlxd_sut, UNKNOWN_CALL, kit.module)
    b = None
    try:
        b = open_endpoint(kit, 1, case)
        if not talker.link():
            pytest.fail("%s: the D-STAR talker gets no link ACK on module %s: the DExtra edge is not serving" % (case, kit.module))
        units = int(REFUSED_S * xstale1.RATE)
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent, _ = pump_while(pool.submit(talker.over, units, "d", (), True), [b])
        hold([b], x1.HOLD_S)
        heard = b.heard()
    finally:
        for ep in (b, talker):
            if ep is not None:
                ep.close()
    # the event lands on the hub's 10 s cadence; one more period after it shows that there is no second one
    end = time.monotonic() + xstale1.SETTLE_S
    while not xstale1c.total(xstale1c.moved_procs(refusals(), before_refused)) and time.monotonic() < end:
        time.sleep(5.0)
    time.sleep(xstale1.QUIET_S)
    rises = xstale1c.moved_procs(refusals(), before_refused)
    d = xstale1.delta(read(), before_counts)
    moved = xstale1.series_diff(snap, read.snapshot(), _AnyProc())
    say("RECORD", "%d D-STAR frames sent by %s, B heard %d; no_radio_id rose by %s; delta of the edge counters %s"
        % (sent, UNKNOWN_CALL, heard, rises or "nothing", dict(d._asdict())))
    say("RECORD", "series that moved: %s" % ("; ".join(moved[:20]) or "none"))
    record(target, case, {"callsign": UNKNOWN_CALL, "frames_sent": sent, "listener_heard": heard, "no_radio_id_rise": rises, "delta": dict(d._asdict()),
                          "moved": moved[:40]})
    if not xstale1c.total(rises):
        pytest.fail("%s: no no_radio_id event at all (series that moved: %s): the talk did not reach an egress edge that wants a radio id, not a product result"
                    % (case, "; ".join(moved[:12]) or "none"))
    problems = xstale1c.judge_refusal(d, rises, heard)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
