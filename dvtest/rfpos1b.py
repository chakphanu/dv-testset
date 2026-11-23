# SPDX-License-Identifier: MIT
"""RFPOS1 step B (DV-Router 47dac7b4, in 5333e4fc, image dv-router:wip-c3b24d2; docs/rfpos1b-cases.md): the shared edge core no longer lets an END-HINT unit
widen the egress width of a stream (crates/lib/edge/src/core.rs, `x_unit_p`: width = the largest seen per stream and profile, an end-hint Unit keeps it).

The defect: a YSF voice frame with the end bit is one Unit of 5 units plus the end unit (6 units, media-format.md "End unit"). The 6 widened the stream's
width from 5 to 6, so the end bundle's units were placed at `media_seq * 6 + i`, away from the 20 ms positions of the frames before it: the last frame moved,
the egress edge saw a hole before the end and counted a FALSE end-of-over gap (dv_edge_over_units_total{dir=tx,result=lost}, the over folded `lossy`) on
every egress edge of the over, same-mode and cross-mode. After the fix a YSF over ends without a gap or a lost unit. No change for D-STAR link streams.

Switch DVT_RFPOS1B=on asserts (a YSF over, listeners on the egress edges); unset, the case plays the same over and only RECORDS the counters, with a
FINDING line for every difference, so an older image still runs.

What a case reads (the hub metrics, dvtest.metrics samples; the hub re-exports each edge process about every 10 s with a `proc` label, the edge name):

  ingress, YSF talker   dv_edge_over_units_total{dir=rx,proc=ysf,result=gap}     YSF counts no source gap: 0
  room                  dv_room_over_units_total{room=y,result=gap|lost}, dv_room_overs_total{room=y,result=complete|lossy|end_missing}
  every egress edge     dv_edge_over_units_total{dir=tx,proc=<edge>,result=lost|late|dropped}, dv_edge_overs_total{dir=tx,proc=<edge>,result=...}
                        (proc ysf for the YSF listener, dmrhb for DMR, dextra for D-STAR)

Judged: the room closed the over, edge rx gap 0, room gap 0 and lost 0, the over not lossy; on each egress edge lost 0, no lossy or end_missing egress over.
late and dropped are recorded (a CTS stall or a starved host, not what the fix changes). Pure and stdlib only (cases/unit/test_rfpos1b_lib.py); the sockets are
in cases/test_rfpos1b.py.
"""
import os
import time
from collections import namedtuple

from . import medload1, radioseq1, xstale1

ENV = "DVT_RFPOS1B"
ROOM = "y"                                    # the module letter of the YSF cases (cases/proto/test_ysf.py MOD = "Y"), lower case in the room labels
INGRESS_PROC = "ysf"
EGRESS_PROC = {"ysf": "ysf", "dmr": "dmrhb", "dstar": "dextra"}     # the metrics proc= label of the edge that serves a listener of that kind
SETTLE_S = 60.0                               # most a case waits for the closes (the hub exports every 10 s, the edges fold 1.6 s after the End)
QUIET_S = 12.0                                # one more hub period after the closes: the edge records land on their own cadence

Reading = namedtuple("Reading", "room egress")          # room: radioseq1.Counts; egress: ((proc, xstale1.Counts), ...) in the order of `procs`


def on(environ=None):
    """DVT_RFPOS1B=on: the image has RFPOS1 step B (DV-Router 47dac7b4 or later): the cases assert."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


def reading(samples, procs, room=ROOM, ingress=INGRESS_PROC):
    """The Reading of parsed hub samples: the room's and the ingress edge's counters (radioseq1.counts) and the tx counters of each egress edge in `procs`."""
    return Reading(radioseq1.counts(samples, room, ingress), tuple((proc, xstale1.counts(samples, proc)) for proc in procs))


def delta(after, before):
    return Reading(radioseq1.delta(after.room, before.room),
                   tuple((proc, xstale1.delta(a, b)) for (proc, a), (_, b) in zip(after.egress, before.egress)))


def egress_folds(reading_, proc):
    """Egress overs folded at one edge (complete + lossy + end_missing + joined)."""
    return xstale1.folds(dict(reading_.egress)[proc])


def procs_missing(samples, procs):
    """The procs of `procs` that have no dv_edge_overs_total series at all (an image without egress accounting, or another proc label), and the procs seen."""
    seen = xstale1.procs_seen(samples)
    return [p for p in procs if p not in seen], seen


def stable_baseline(read, quiet_s=QUIET_S, budget_s=SETTLE_S, clock=time.monotonic, sleep=time.sleep):
    """A Reading that did not move over `quiet_s` (the fold of an earlier over lands on the hub's 10 s cadence and must not leak into this over's delta); the last
    read when the budget ends first."""
    end = clock() + budget_s
    prev = read()
    while clock() < end:
        sleep(quiet_s)
        cur = read()
        if cur == prev:
            return cur
        prev = cur
    return prev


def wait_settled(read, before, fold_procs=(), budget_s=SETTLE_S, quiet_s=QUIET_S, poll_s=5.0, clock=time.monotonic, sleep=time.sleep):
    """Reads every `poll_s` until the room's over count rose by 1 over `before` and every edge of `fold_procs` folded one more egress over, then reads once more after
    `quiet_s` and returns that last Reading. Raises TimeoutError, naming what did not move, when the budget ends first."""
    end = clock() + budget_s
    while True:
        now = read()
        room_done = medload1.overs_total(now.room) - medload1.overs_total(before.room) >= 1
        pending = [p for p in fold_procs if egress_folds(now, p) - egress_folds(before, p) < 1]
        if room_done and not pending:
            sleep(quiet_s)
            return read()
        if clock() >= end:
            raise TimeoutError("no close within %.0f s: room over count %s (before %s)%s" % (
                budget_s, medload1.overs_total(now.room), medload1.overs_total(before.room),
                "; no egress fold at %s" % ", ".join(pending) if pending else ""))
        sleep(poll_s)


Verdict = namedtuple("Verdict", "problems findings facts")


def judge(change, fold_procs=()):
    """The verdict of one YSF over from a Reading delta: problems (asserted under DVT_RFPOS1B=on), findings (late / dropped, recorded), facts.

    The room: the over closed, no gap at the YSF ingress edge or in the room, not counted lossy (radioseq1.judge_counts), room lost 0, not end_missing.
    Each egress edge: lost 0 (the false end-of-over gap of the defect), no lossy and no end_missing egress over, and a fold at each edge of `fold_procs`
    (an edge with a cross stream; a same-mode YSF listener may be served by the fast path and fold none)."""
    base = radioseq1.judge_counts(change.room)
    problems = list(base.problems)
    findings = []
    facts = {"room": dict(change.room._asdict()), "egress": {proc: dict(c._asdict()) for proc, c in change.egress}}
    if change.room.room_lost:
        problems.append("dv_room_over_units_total{room=%s,result=lost} rose by %g, a clean over has none" % (ROOM, change.room.room_lost))
    if change.room.overs_end_missing:
        problems.append("dv_room_overs_total{result=end_missing} rose by %g: the over's end was not counted" % change.room.overs_end_missing)
    for proc, c in change.egress:
        if c.lost:
            problems.append("dv_edge_over_units_total{dir=tx,proc=%s,result=lost} rose by %g: a false end-of-over gap on the egress edge" % (proc, c.lost))
        if c.lossy:
            problems.append("dv_edge_overs_total{dir=tx,proc=%s,result=lossy} rose by %g: the egress over was folded lossy" % (proc, c.lossy))
        if c.end_missing:
            problems.append("dv_edge_overs_total{dir=tx,proc=%s,result=end_missing} rose by %g: the egress edge did not see the end" % (proc, c.end_missing))
        if c.late or c.dropped:
            findings.append("egress edge %s counted late %g, dropped %g (a CTS stall or a starved host; not what RFPOS1 step B changes)" % (proc, c.late, c.dropped))
        if proc in fold_procs and xstale1.folds(c) < 1:
            problems.append("egress edge %s folded no over: the listener did not take the stream (a harness slip, not a result)" % proc)
    return Verdict(problems, findings, facts)
