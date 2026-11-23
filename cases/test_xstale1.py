# SPDX-License-Identifier: MIT
"""XSTALE1 (DV-Router b849a082, image dv-router:wip-2ec9be4, code ee4aae71): the room's copy of an edge's own talker stays open for the whole over on the edges
without a same-protocol fast path (usrp, p25, nxdn). docs/xstale1-cases.md; the arithmetic and the verdicts are dvtest/xstale1.py, the protocol kits
cases/xstale1_support.py. Switch DVT_XSTALE1=on (the cases skip with the reason otherwise). NOT RUN on a rig: written against the sources of b849a082.

Before the fix the leg-less cross stream (the talker's own copy at its own edge) was closed by the 1.6 s silence timer 1.6 s into the over: every later
unit counted dv_edge_drops_total{reason=x_stale}, the over folded with end_missing / late in the tx accounting, and a listener of the same edge that joined
later heard nothing. Per protocol (usrp, p25, nxdn; talker A and listener B are two clients of the same server port of the SUT):

  XS1-b  A talks one over of 8 s alone on its edge: the edge folds it (dv_edge_overs_total{dir=tx}), x_stale <= 2, tx end_missing, lossy, late, dropped and lost 0,
         A hears nothing of itself
  XS1-c  A talks 8 s; B links to the same edge 3 s into the over: B hears at least 80 percent of the rest and no more than was sent, the over folds as joined
         (dv_edge_overs_total{result=joined}), lost 0, late 0, end_missing 0, x_stale <= 2
  XS1-d  B linked from the start; A talks 4 s, goes silent for 2.4 s (over the 1.6 s limit) with no terminator or unkey, then talks 4 s again: B hears both bursts
         whole, the edge folds two overs (the stream ended and the second is a new one), x_stale <= 2, late, dropped and lost 0 (end_missing or lossy is the
         documented outcome of a stream that ended by silence: recorded, not asserted)

The counters are the hub's (rig_control.metrics, a new record every 10 s per edge process, label proc=<edge>): a case takes a baseline that did not move for 12 s, runs ONE
scenario, waits until the fold count of its edge rose and reads once more 12 s later. Needs DVT_RIG_CONTROL=1 (set by scripts/run.sh), DVT_SUT_PROFILE=dvrouter,
DVT_SUT_LAUNCH=native, DVT_USRP_DYNAMIC=1 and rig/config/dv-router/soak-edges.toml in DVT_SUT_CONFIG_EXTRA.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import data_support as ds
import xstale1_support as xs
from dvtest import metrics, xstale1

pytestmark = [pytest.mark.rig]

LOG = "xstale1.txt"
HOLD_S = 3.0                         # after the End the endpoints stay linked: the edge folds 1.6 s later (its accounting horizon) with a listener present
PROTOCOLS = ("usrp", "p25", "nxdn")


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not xstale1.on():
        pytest.skip("DVT_XSTALE1=on is not set (the image under test has XSTALE1, DV-Router b849a082: dv-router:wip-2ec9be4 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


@pytest.fixture(scope="module")
def kit_table(target):
    table, missing = xs.kits(target)
    return table, missing


@pytest.fixture
def edge(request, kit_table, rig_control):
    """(kit, read): the kit of the parametrized protocol and a reader of the Counts of its edge process; a skip when the kit is not available, a failure when the
    hub exports no dv_edge_overs_total for the edge (an image without S3 egress accounting, or another proc label)."""
    table, missing = kit_table
    name = request.node.callspec.params["proto"]
    if name not in table:
        pytest.skip("%s: %s" % (name, missing.get(name, "not available")))
    kit = table[name]

    def read():
        return xstale1.counts(metrics.parse(rig_control.metrics("xlxd-sut")), kit.proc)

    def snapshot():
        return metrics.parse(rig_control.metrics("xlxd-sut"))

    read.snapshot = snapshot                 # the full series of the hub, for the diagnosis of a fold that never came
    read.procs = (kit.proc, "dv-router", "")

    samples = metrics.parse(rig_control.metrics("xlxd-sut"))
    if kit.proc not in xstale1.procs_seen(samples):
        pytest.fail("no dv_edge_overs_total{proc=%s} on the hub metrics (procs with the family: %s): the image has no egress over accounting, or the edge %s is not "
                    "running (%s)" % (kit.proc, xstale1.procs_seen(samples), kit.proc, kit.hint))
    return kit, read


def open_endpoint(kit, index, case):
    ep = kit.endpoint(index)
    if ep.link():
        return ep
    ep.close()
    if kit.name == "usrp":
        pytest.skip("USRP client gets no REG:OK: the dynamic USRP hub is off (%s)" % kit.hint)
    pytest.fail("%s: %s endpoint %d gets no poll echo from the SUT: the edge is not serving (%s)" % (case, kit.name, index, kit.hint))


def pump_while(job, endpoints):
    while not job.done():
        for ep in endpoints:
            ep.pump(0.05)
    return job.result()


def hold(endpoints, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for ep in endpoints:
            ep.pump(0.05)


def counted(read, before, case, want_folds, note_fn, context=lambda: "", snap=None):
    """The Counts delta of the case once the edge folded. When it never does, the failure names what the clients saw (`context()`) and every series of the edge
    that moved since `snap` (read.snapshot() taken with the baseline), so a harness slip (nothing reached the edge) is told from a product result."""
    try:
        after = xstale1.wait_settled(read, before, want_folds=want_folds)
    except TimeoutError as exc:
        moved = xstale1.series_diff(snap, read.snapshot(), read.procs) if snap is not None else []
        note_fn("RECORD", "no fold: %s; series that moved: %s" % (context(), "; ".join(moved) or "none"))
        pytest.fail("%s: %s; %s; series that moved at the edge: %s" % (case, exc, context(), "; ".join(moved[:12]) or "none"))
    d = xstale1.delta(after, before)
    note_fn("RECORD", "delta of the edge counters: %s" % dict(d._asdict()))
    if d.lossy or d.lost or d.end_missing:
        moved = xstale1.series_diff(snap, read.snapshot(), read.procs) if snap is not None else []
        note_fn("RECORD", "series that moved at the edge: %s" % ("; ".join(moved) or "none"))
    return d


@pytest.mark.parametrize("proto", PROTOCOLS)
def test_xs1_b_own_over_without_listener(edge, proto, target):
    """b) the talker alone on its edge: one 8 s over, folded once and clean."""
    kit, read = edge
    case = "XS1-b[%s]" % proto
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731
    before = xstale1.stable_baseline(read)
    snap = read.snapshot()
    a = open_endpoint(kit, 0, case)
    try:
        sent = a.burst(kit.units(xstale1.OVER_S), "b", True)
        hold([a], HOLD_S)
        own = a.heard()
    finally:
        a.close()
    d = counted(read, before, case, 1, say, lambda: "%d units sent, the talker heard %d of itself" % (sent, own), snap)
    say("RECORD", "%d units sent, the talker heard %d of itself" % (sent, own))
    problems = xstale1.judge_b(d, own)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


@pytest.mark.parametrize("proto", PROTOCOLS)
def test_xs1_c_same_edge_listener_joins_at_three_seconds(edge, proto, target):
    """c) a second client of the same edge joins 3 s into an 8 s over: it hears the rest, the over folds as joined."""
    kit, read = edge
    case = "XS1-c[%s]" % proto
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731
    before = xstale1.stable_baseline(read)
    snap = read.snapshot()
    a = open_endpoint(kit, 0, case)
    b = None
    try:
        units = kit.units(xstale1.OVER_S)
        with ThreadPoolExecutor(max_workers=1) as pool:
            t0 = time.monotonic()
            job = pool.submit(a.burst, units, "c", True)
            time.sleep(max(0.0, t0 + xstale1.JOIN_S - time.monotonic()))
            b = open_endpoint(kit, 1, case)
            joined_at = time.monotonic() - t0
            pump_while(job, [b])
        sent = job.result()
        hold([a, b], HOLD_S)
        heard = b.heard()
        own = a.heard()
    finally:
        for ep in (b, a):
            if ep is not None:
                ep.close()
    assert joined_at <= xstale1.JOIN_S + xstale1.JOIN_LATE_S, \
        "%s: B was linked only %.1f s into the over (wanted about %.0f s): the link is too slow for this case, not a product result" % (case, joined_at, xstale1.JOIN_S)
    rest = xstale1.expected_rest(xstale1.OVER_S, joined_at)
    d = counted(read, before, case, 1, say,
                lambda: "%d units sent, B joined %.2f s into the over and heard %d of the %d to come, the talker heard %d of itself" % (sent, joined_at, heard, rest, own), snap)
    say("RECORD", "%d units sent, B joined %.2f s into the over and heard %d of the %d to come; the talker heard %d of itself" % (sent, joined_at, heard, rest, own))
    problems = xstale1.judge_c(d, heard, rest, sent, kit.slack)
    if own:
        problems.append("the talker heard %d units of its own over" % own)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


@pytest.mark.parametrize("proto", PROTOCOLS)
def test_xs1_d_real_silence_ends_the_stream(edge, proto, target):
    """d) 4 s, 2.4 s of silence without terminator, 4 s: the listener hears both bursts, two overs fold."""
    kit, read = edge
    case = "XS1-d[%s]" % proto
    say = lambda tag, text: note(target, case, tag, text)                         # noqa: E731
    before = xstale1.stable_baseline(read)
    snap = read.snapshot()
    a = open_endpoint(kit, 0, case)
    b = None
    try:
        b = open_endpoint(kit, 1, case)
        units = kit.units(xstale1.BURST_S)
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent1 = pump_while(pool.submit(a.burst, units, "d1", False), [b])
        hold([a, b], xstale1.SILENCE_S)
        heard1 = b.heard()
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent2 = pump_while(pool.submit(a.burst, units, "d2", True), [b])
        hold([a, b], HOLD_S)
        heard2 = b.heard() - heard1
    finally:
        for ep in (b, a):
            if ep is not None:
                ep.close()
    d = counted(read, before, case, 2, say,
                lambda: "burst 1 sent %d, B heard %d; burst 2 sent %d, B heard %d" % (sent1, heard1, sent2, heard2), snap)
    say("RECORD", "burst 1 sent %d, B heard %d; burst 2 sent %d, B heard %d" % (sent1, heard1, sent2, heard2))
    if d.end_missing or d.lossy:
        say("RECORD", "the stream that ended by silence folded as end_missing %g, lossy %g" % (d.end_missing, d.lossy))
    problems = xstale1.judge_d(d, heard1, sent1, heard2, sent2, kit.slack)
    assert not problems, "%s: %s" % (case, "; ".join(problems))
