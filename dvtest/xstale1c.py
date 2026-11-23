# SPDX-License-Identifier: MIT
"""XSTALE1c (DV-Router 5072bf57, image dv-router:wip-3f142cd; docs/xstale1c-cases.md): the egress accounting of an over whose listener leaves.

What the image promises, as DV-Router stated it:

  * a listener that leaves mid-over settles its egress over at the moment it leaves: after the leave no lost frames are counted (before the fix the rest of
    the over counted as lost, for example sent 150 lost 350); real losses before the leave stay counted;
  * the same listener leaving and rejoining folds as joined, without double counting;
  * a stream refused at its start for want of a radio id adds no dropped frames, only the dv_edge_drops_total{reason="no_radio_id"} event, once, and folds no
    egress over.

Counters as in dvtest/xstale1.py (the tx fold family of one edge process) plus dv_edge_drops_total{reason="no_radio_id"}. Switch DVT_XSTALE1C=on.

Pure and stdlib only (cases/unit/test_xstale1c_lib.py). The sockets are in cases/xstale1c_support.py.
"""
import os

from dvtest import xstale1

ENV = "DVT_XSTALE1C"
ENV_D = "DVT_XSTALE1D"
NO_RADIO_ID = "no_radio_id"

LEAVE_S = 3.0                    # the listener leaves this long into the over (position 150 at 50 units a second)
REJOIN_S = 4.0                   # the same listener is back this long into the over (a gap of about 1 s: positions 150..200)
LEAVE_LATE_S = 1.0               # a leave that happens later than LEAVE_S + this is a harness problem (a slow pacer), not a product result
HEARD_BEFORE_FRACTION = 0.6      # share of the units sent before the leave the listener must have heard (a harness check: the leave must be MID-over, with the listener hearing)


def on(environ=None):
    """DVT_XSTALE1C=on: the image has XSTALE1c (DV-Router 5072bf57 or later): the cases assert."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


def on_d(environ=None):
    """DVT_XSTALE1D=on: the image has XSTALE1d (DV-Router 4f454c71, dv-router:wip-e9fcc38): any last listener leaving on an edge whose talker is on another edge folds
    complete, never end_missing (the transcoded leave XS1c-b[leave] and the same-mode cross-edge leave XS1c-e)."""
    return str((environ if environ is not None else os.environ).get(ENV_D, "")).strip().lower() in ("on", "1", "true", "yes")


def no_radio_id(samples):
    """{proc: value} of dv_edge_drops_total{reason="no_radio_id"} (a missing series is absent)."""
    out = {}
    for s in samples:
        if s.name == xstale1.DROPS and s.labels.get("reason") == NO_RADIO_ID:
            proc = s.labels.get("proc", "")
            out[proc] = out.get(proc, 0.0) + s.value
    return out


def total(by_proc):
    return sum(by_proc.values())


def moved_procs(after, before):
    """{proc: rise} of the procs whose no_radio_id count rose between two readings of `no_radio_id`."""
    return {p: v - before.get(p, 0.0) for p, v in after.items() if v - before.get(p, 0.0)}


# ---- verdicts: each returns a list of problems (empty = pass)

def judge_leave(d, allow=()):
    """a) the listener leaves mid-over, the talker goes on to the End: one fold, the over settled at the leave: lost, late, dropped, lossy, end_missing 0, not joined
    (`allow`: names of counters not judged, as in xstale1.common_problems)."""
    out = xstale1.common_problems(d, allow=allow)
    if xstale1.folds(d) != 1:
        out.append("the edge folded %g overs for one over (want 1)" % xstale1.folds(d))
    if d.joined:
        out.append("the over folded as joined with the listener only leaving (joined %g)" % d.joined)
    return out


def judge_leave_complete(d):
    """e) XSTALE1d: the leave of the last listener of an over whose talker is on another edge folds `complete` (not end_missing), as the leave of case a): judge_leave
    plus complete exactly 1."""
    out = judge_leave(d)
    if d.complete != 1:
        out.append("XSTALE1d: the over did not fold complete once after the leave (complete %g, end_missing %g)" % (d.complete, d.end_missing))
    return out


def judge_rejoin(d, heard, rest, sent, slack):
    """c) the same listener leaves and is back about a second later: the over folds once, as joined, lost / late / dropped 0 (nothing counted twice), and the
    listener hears at least HEARD_FRACTION of the units still to come after its link completed."""
    out = xstale1.common_problems(d)
    if xstale1.folds(d) != 1:
        out.append("the edge folded %g overs for one over (want 1)" % xstale1.folds(d))
    if d.joined != 1:
        out.append("the over did not fold as joined once (joined %g, complete %g)" % (d.joined, d.complete))
    low, high = xstale1.heard_window(rest, sent, slack)
    if heard < low:
        out.append("the rejoined listener heard %d units, at least %d of the %d to come were expected" % (heard, low, rest))
    if heard > high:
        out.append("the rejoined listener heard %d units, more than the %d sent (+%d padding)" % (heard, sent, slack))
    return out


def judge_refusal(d, rises, heard):
    """d) a stream refused at its start (no radio id): the no_radio_id event once, no egress over folded at the edge, nothing dropped, late or lost, and the
    listener hears nothing. `rises` is {proc: rise} of the no_radio_id counter over the case."""
    out = []
    if total(rises) != 1:
        out.append("dv_edge_drops_total{reason=no_radio_id} rose by %g (%s), exactly one event expected" % (total(rises), rises or "no proc"))
    if d.dropped:
        out.append("tx dropped rose by %g (want 0: a refused stream adds no dropped frames)" % d.dropped)
    for name in ("late", "lost", "lossy", "end_missing", "joined", "complete"):
        if getattr(d, name):
            out.append("tx %s rose by %g (want 0: the refused stream opened no egress over)" % (name, getattr(d, name)))
    if heard:
        out.append("the listener heard %d units of a refused stream" % heard)
    return out


def hear_before(sent_before_leave, heard):
    """True when the listener heard enough before the leave for the leave to count as mid-over (a harness check)."""
    return heard >= int(HEARD_BEFORE_FRACTION * sent_before_leave)
