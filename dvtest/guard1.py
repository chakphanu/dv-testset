# SPDX-License-Identifier: MIT
"""What a DV-Router image with GUARDSYNC1 / GUARDMERGE1 lets a black-box case assert (DV-Router c1b239d1 and da900207, merged in bc9af2b1; docs
floor-control.md "Guard penalties" and fabric.md "link state"; image dv-router:wip-f1f3777).

Switch: DVT_GUARD1=on asserts (strict); off (the default) skips the cases with a reason.

The guard is the short-stream abuse penalty of a callsign, node-wide: each room controller keeps the short-stream history of its own room, puts the record
`guard/<callsign>` (44 B GuardRec: strikes, link_blocked, block_until_ms, last_strike_ms) on a strike, and every other controller and edge caches it. Strike 1, 2, 3 block
the callsign's overs for 3 s, 10 s, 300 s (DENY 3, silent). GUARDSYNC1: a process that restarts, or that lost its own router link, asks the other controllers for their
`guard-snapshot/<zid>` (a bundle of at most 1024 records) at startup, after the link loss, after lost input and every 30 s; the applied-by bound is 7 s on the pending fast
path and 31 s otherwise. GUARDMERGE1: merging two records of the same callsign keeps the highest strike count, the latest block and strike times and the OR of
`link_blocked`; a record is active until max(last_strike + 300 s, block_until): merging never lowers a penalty in force.

What a rig can do: SIGKILL one dv-room by module letter (the launcher respawns it with a new room_epoch and an EMPTY guard cache), and probe with a D-STAR talker. The
timeline helper below judges the probes of a blocked callsign against the 31 s bound. Pure and stdlib only (cases/unit/test_guard1_lib.py).
"""
import os
from collections import namedtuple

APPLIED_BY_S = 31.0                 # fabric.md (GUARDSYNC1): a reconciled record is applied within 7 s (pending fast path) or 31 s
SLACK_S = 4.0                       # the respawn, the probe cadence and scheduling
BLOCK_S = (3.0, 10.0, 300.0)        # strike 1, 2, 3
PROBE_PERIOD_S = 0.5

Probe = namedtuple("Probe", "t heard")          # t: seconds on any monotonic clock; heard: the blocked callsign's probe reached the listener


def strict_on(environ=None):
    """DVT_GUARD1=on: the image has GUARDSYNC1 / GUARDMERGE1."""
    return (environ if environ is not None else os.environ).get("DVT_GUARD1", "").lower() in ("on", "1", "true", "yes")


def deadline_after(t_up, bound=APPLIED_BY_S, slack=SLACK_S):
    """The instant from which a blocked callsign must be refused again: the room answered at t_up, the record is applied within `bound` (+ slack)."""
    return t_up + bound + slack


Verdict = namedtuple("Verdict", "problems early_leaks late_leaks first_refused")


def judge_probes(probes, t_up, bound=APPLIED_BY_S, slack=SLACK_S):
    """Judge the probes of a callsign whose strike-3 block (300 s) is in force on the whole node.
    `t_up` is when the restarted room first carried an unblocked control callsign's over. Probes before t_up are ignored (the room could not carry any over).
    A probe heard from t_up to the deadline is an EARLY leak (a FINDING: the record is not yet reconciled, a window the bound allows); a probe heard at or after
    the deadline is a LATE leak (a failure: the record was never applied or was lowered). Problems also: no probe after the deadline (the case proved nothing)."""
    limit = deadline_after(t_up, bound, slack)
    after = [p for p in probes if p.t >= t_up]
    early = [p for p in after if p.heard and p.t < limit]
    late = [p for p in after if p.heard and p.t >= limit]
    problems = []
    if not [p for p in after if p.t >= limit]:
        problems.append("no probe of the blocked callsign at or after %.1f s past the room's return (the observation ended too early)" % (limit - t_up))
    for p in late:
        problems.append("the blocked callsign was heard %.1f s after the room returned (bound %.0f s + %.0f s slack): the guard record was not reconciled" % (p.t - t_up, bound, slack))
    refused = [p for p in after if not p.heard]
    return Verdict(problems, early, late, refused[0].t - t_up if refused else None)


def describe(probes, t_up):
    after = [p for p in probes if p.t >= t_up]
    heard = [round(p.t - t_up, 1) for p in after if p.heard]
    return "%d probes after the room returned, %d heard at %s s" % (len(after), len(heard), heard[:6])
