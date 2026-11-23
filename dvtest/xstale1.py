# SPDX-License-Identifier: MIT
"""XSTALE1 (DV-Router b849a082, image dv-router:wip-2ec9be4, code ee4aae71; docs/xstale1-cases.md): the room's copy of an edge's own talker.

On the edges that have no same-protocol fast path (usrp, p25, nxdn) the room publishes the talker's stream back to the talker's own edge (a cross
stream whose origin is that edge). With no other listener at that edge the stream has no listener leg. Before XSTALE1 the 1.6 s silence timer closed
it 1.6 s into the over, so every later unit counted dv_edge_drops_total{reason=x_stale} and the over folded with end_missing / late in the tx
accounting; a listener of the same edge joining later heard nothing. After it the stream stays open for the whole over, a same-edge listener that
joins at 3 s hears the rest and its over folds as `joined` (lost 0), and real silence still ends the stream.

Switch DVT_XSTALE1=on asserts (the soak also reads it, dvtest.soak.xstale_on); off skips the rig cases with the reason.

The counters (hub metrics, dvtest.metrics samples; the hub re-exports each edge process every 10 s with a `proc` label, the edge name):

  dv_edge_drops_total{proc, reason="x_stale"}
  dv_edge_overs_total{proc, dir="tx", result="complete|lossy|end_missing|joined"}      one fold per over at the edge, after the 1.6 s accounting horizon
  dv_edge_over_units_total{proc, dir="tx", result="late|dropped|lost"}

Pure and stdlib only (cases/unit/test_xstale1_lib.py). The sockets are in cases/xstale1_support.py.
"""
import os
import time
from collections import namedtuple

ENV = "DVT_XSTALE1"
DROPS = "dv_edge_drops_total"
OVERS = "dv_edge_overs_total"
UNITS = "dv_edge_over_units_total"
EDGES = ("usrp", "p25", "nxdn")                 # the metrics label proc= of the edges under test

OVER_S = 8.0                                    # the long over of b) and c)
JOIN_S = 3.0                                    # the same-edge listener joins this long into the over (c)
JOIN_LATE_S = 1.5                               # a join that completes later than JOIN_S + this is a harness failure (slow link), not a product result
BURST_S = 4.0                                   # d): each of the two bursts; the cross stream silence limit is 1.6 s, so a burst under 3 s would not tell a closed stream from a short one
SILENCE_S = 2.4                                 # d): the pause without terminator, over the 1.6 s limit with a margin of 0.8 s
SILENCE_LIMIT_S = 1.6
RATE = 50                                       # native units a second heard or sent: USRP frames, P25 IMBE records, NXDN frames (4 per 80 ms packet)
HEARD_FRACTION = 0.8                            # share of the rest of the over the joiner must hear (a pre-XSTALE1 edge delivers none of it)
X_STALE_MAX = 2.0                               # x_stale of one case: 0 expected, a unit or two behind a StreamEnd tolerated; the old count was about 320 (usrp, p25) and 80 (nxdn) in one 8 s over
SETTLE_S = 60.0                                 # most a case waits for the fold (the hub exports every 10 s, the edge folds 1.6 s after the End)
QUIET_S = 12.0                                  # one more hub period after the fold: the edge records land on their own cadence

Counts = namedtuple("Counts", "x_stale complete lossy end_missing joined late dropped lost")


def on(environ=None):
    """DVT_XSTALE1=on: the image has XSTALE1 (DV-Router b849a082 or later): the cases assert."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


def counts(samples, proc):
    """Counts of one edge process from parsed hub samples (dvtest.metrics.parse); a missing series is 0, only the tx direction is read."""
    def drops(reason):
        return sum(s.value for s in samples if s.name == DROPS and s.labels.get("proc") == proc and s.labels.get("reason") == reason)

    def overs(result):
        return sum(s.value for s in samples if s.name == OVERS and s.labels.get("proc") == proc and s.labels.get("dir") == "tx" and s.labels.get("result") == result)

    def units(result):
        return sum(s.value for s in samples if s.name == UNITS and s.labels.get("proc") == proc and s.labels.get("dir") == "tx" and s.labels.get("result") == result)

    return Counts(drops("x_stale"), overs("complete"), overs("lossy"), overs("end_missing"), overs("joined"), units("late"), units("dropped"), units("lost"))


def procs_seen(samples):
    """Sorted proc labels of dv_edge_overs_total (what a hint names when the edge under test has no series)."""
    return sorted({s.labels.get("proc", "-") for s in samples if s.name == OVERS})


def delta(after, before):
    return Counts(*[a - b for a, b in zip(after, before)])


def folds(c):
    """Egress overs folded: one per over at the edge."""
    return c.complete + c.lossy + c.end_missing + c.joined


def stable_baseline(read, quiet_s=QUIET_S, budget_s=SETTLE_S, clock=time.monotonic, sleep=time.sleep):
    """Counts that did not move over `quiet_s` (a fold of an earlier case lands on the hub's 10 s cadence and must not leak into this case's delta); the last read
    when the budget ends first."""
    end = clock() + budget_s
    prev = read()
    while clock() < end:
        sleep(quiet_s)
        cur = read()
        if cur == prev:
            return cur
        prev = cur
    return prev


def wait_settled(read, before, want_folds=1, budget_s=SETTLE_S, quiet_s=QUIET_S, poll_s=5.0, clock=time.monotonic, sleep=time.sleep):
    """Reads Counts every `poll_s` until the egress over count rose by `want_folds` over `before`, then once more after `quiet_s` and returns that last Counts. Raises
    TimeoutError when the fold count never reached it within `budget_s`."""
    end = clock() + budget_s
    while True:
        now = read()
        if folds(now) - folds(before) >= want_folds:
            sleep(quiet_s)
            return read()
        if clock() >= end:
            raise TimeoutError("the egress over count did not rise by %d within %.0f s (folds before %g, now %g)" % (want_folds, budget_s, folds(before), folds(now)))
        sleep(poll_s)


def series_diff(before, after, procs, limit=60):
    """Lines "name{labels} before -> after" of every series (dvtest.metrics samples) that changed between two snapshots and belongs to one of `procs` (the proc label;
    "" stands for a series without one). The diagnosis of a case that never saw its fold: what DID move at the edge (an ingress counter, a session gauge, a drop reason)."""
    def keyed(samples):
        out = {}
        for s in samples:
            proc = s.labels.get("proc", "")
            if proc in procs:
                out[(s.name, tuple(sorted(s.labels.items())))] = s.value
        return out

    old, new = keyed(before), keyed(after)
    lines = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key, 0.0), new.get(key, 0.0)
        if a != b:
            labels = ",".join("%s=%s" % kv for kv in key[1])
            lines.append("%s{%s} %g -> %g" % (key[0], labels, a, b))
    if len(lines) > limit:
        lines = lines[:limit] + ["... %d more" % (len(lines) - limit)]
    return lines


def units_for(seconds, multiple=1, rate=RATE):
    """Native units of a burst of `seconds`, rounded down to a multiple of `multiple` (P25 LDU 9, NXDN packet 4, DMR-derived NXDN frames 12)."""
    n = int(seconds * rate)
    return n - n % multiple


def expected_rest(over_s, joined_at_s, rate=RATE):
    """Units still to come when a listener has joined `joined_at_s` into an over of `over_s`."""
    return max(0, int((over_s - joined_at_s) * rate))


def heard_window(rest, sent, slack, fraction=HEARD_FRACTION):
    """(lowest, highest) number of units a listener may have heard: at least `fraction` of `rest`, at most all that was sent plus the padding of the protocol (a P25 LDU, an NXDN packet)."""
    return int(fraction * rest), sent + slack


# ---- verdicts: each returns a list of problems (empty = pass)

def common_problems(d, x_stale_max=X_STALE_MAX, allow=()):
    """What every case asserts about the talker's own edge (DV-Router's expectations a, b): x_stale about 0; tx end_missing, late, dropped, lost and lossy 0
    (the names in `allow` are not judged: d) ends a stream by silence, so end_missing or lossy is the documented outcome there)."""
    out = []
    if d.x_stale > x_stale_max:
        out.append("dv_edge_drops_total{reason=x_stale} rose by %g (at most %g): the own copy of the talker was closed before the over ended" % (d.x_stale, x_stale_max))
    for name in ("end_missing", "late", "dropped", "lost", "lossy"):
        if name not in allow and getattr(d, name):
            out.append("tx %s rose by %g (want 0)" % (name, getattr(d, name)))
    return out


def judge_b(d, heard_by_talker):
    """b) one over of OVER_S with the talker alone on its edge: folded once, nothing missing, late or lost, no x_stale, and the talker hears nothing of itself."""
    out = common_problems(d)
    if folds(d) < 1:
        out.append("no egress over was folded at the edge")
    if d.joined:
        out.append("the over folded as joined with nobody joining (joined %g)" % d.joined)
    if heard_by_talker:
        out.append("the talker heard %d units of its own over" % heard_by_talker)
    return out


def judge_c(d, heard, rest, sent, slack):
    """c) a listener of the same edge joins JOIN_S into the over: it hears at least HEARD_FRACTION of the rest, no more than was sent, lost 0, and the over folds as joined."""
    out = common_problems(d)
    low, high = heard_window(rest, sent, slack)
    if heard < low:
        out.append("the joiner heard %d units, at least %d of the %d to come were expected" % (heard, low, rest))
    if heard > high:
        out.append("the joiner heard %d units, more than the %d sent (+%d padding)" % (heard, sent, slack))
    if folds(d) < 1:
        out.append("no egress over was folded at the edge")
    if d.joined < 1:
        out.append("the over did not fold as joined (joined %g, complete %g)" % (d.joined, d.complete))
    return out


def judge_d(d, heard1, sent1, heard2, sent2, slack):
    """d) two bursts of BURST_S with SILENCE_S of silence and no terminator between them, a listener of the same edge present from the start: it hears both (the first
    stream ended and the second is a new one), the edge folded two overs, no x_stale. Only the bursts' own counts are judged: heard1 is read before the second burst
    starts, heard2 is the total after it minus heard1. end_missing and lossy are allowed (a stream that ended by silence has no End of its own to count)."""
    out = common_problems(d, allow=("end_missing", "lossy"))
    for tag, heard, sent in (("first", heard1, sent1), ("second", heard2, sent2)):
        low, high = heard_window(sent, sent, slack)
        if heard < low:
            out.append("the listener heard %d of the %s burst's %d units (at least %d expected)" % (heard, tag, sent, low))
        if heard > high:
            out.append("the listener heard %d units of the %s burst, more than the %d sent (+%d padding)" % (heard, tag, sent, slack))
    if folds(d) < 2:
        out.append("the edge folded %g overs for two bursts: the second burst was not a new stream" % folds(d))
    return out
