# SPDX-License-Identifier: MIT
"""What a DV-Router image with MEDLOAD1 lets a black-box case assert (DV-Router fb6a2a51 .. 28fc1838, merged in bc9af2b1; docs media-format.md 186-340, status.md 704-830 and
the metrics table, work-units.md MEDLOAD1; image dv-router:wip-f1f3777).

Switch: DVT_MEDLOAD1=on asserts (strict); off (the default) skips the cases with a reason.

Per-over media loss accounting (read from the sources, the arithmetic is the edge's, not guessed):

  * the ingress edge tallies each over once at its end: dv_edge_over_units_total{dir="rx",result=late|dropped|gap}. `gap` is a missing SOURCE position inside the over, counted for the
    D-STAR family (DExtra, DPlus, DCS, G3, XLX, radio: the native counter 0..20, modulus 21) and for USRP (the u32 wire sequence); M17, YSF, DMR, P25, NXDN and WebRTC count no gap;
  * the room tallies each over once at its close: dv_room_over_units_total{room,result=late|lost|gap} (room = the module letter in lower case) and
    dv_room_overs_total{room,result=complete|lossy|end_missing}: `gap` is the edge's gap, `late` the edge and room late, `lost` the ROOM-hop loss only (the summary fields of an event
    or a recorder row add the gap to lost); an over with a source gap only is `lossy` with gap > 0 and lost = 0;
  * egress: dv_edge_over_units_total{dir="tx",result=late|dropped|lost}: source holes are not egress loss;
  * the counters are cumulative per process (the hub re-exports them with a `proc` label, about every 10 s): the cases take a baseline, run ONE over, and wait until the room over count rose;
  * late and dropped come from a CTS stall or host starvation, or from a frame the edge ignores (a repeated D-STAR counter or a short backward step is counted as one late unit, core.rs 3664-3680,
    rig run 20261122); dropped stays 0 on an unloaded run, late equals the number of ignored frames (`ignored`).

The D-STAR gap rule of the edge (crates/lib/edge/src/core.rs 3555-3600, read from the source: UNVERIFIED on a rig): for each arriving voice frame with counter `pos` (0..20; 21..31 are
dropped as invalid before counting) and the previous forwarded counter `prev`: delta = (pos + 21 - prev) mod 21; delta 0 (a duplicate) and a BACKWARD step (pos < prev and prev - pos <= 10) are
ignored; otherwise gap += delta - 1 and prev = pos. `model_gap` is that rule, `plan_over` builds the arrival order of a planned over (drops, a frame delayed behind another, duplicates).
Pure and stdlib only (cases/unit/test_medload1_lib.py).
"""
import os
import time
from collections import namedtuple

EDGE_UNITS = "dv_edge_over_units_total"
ROOM_UNITS = "dv_room_over_units_total"
ROOM_OVERS = "dv_room_overs_total"
MODULUS = 21
BACKWARD_MAX = 10
SETTLE_S = 60.0                      # three telemetry periods of the hub
QUIET_S = 12.0                       # one more period after the room count moved: the edge records land on their own cadence

Counts = namedtuple("Counts", "edge_rx_gap edge_rx_late edge_rx_dropped edge_tx_lost edge_tx_late edge_tx_dropped room_gap room_late room_lost overs_complete overs_lossy overs_end_missing")


def strict_on(environ=None):
    """DVT_MEDLOAD1=on: the image has MEDLOAD1 (the per-over units series exist)."""
    return (environ if environ is not None else os.environ).get("DVT_MEDLOAD1", "").lower() in ("on", "1", "true", "yes")


def counts(samples, room):
    """Counts of one room letter (lower case) from parsed hub samples (dvtest.metrics.parse); every edge process is summed (an idle edge is 0)."""
    def edge(direction, result):
        return sum(s.value for s in samples if s.name == EDGE_UNITS and s.labels.get("dir") == direction and s.labels.get("result") == result)

    def room_units(result):
        return sum(s.value for s in samples if s.name == ROOM_UNITS and s.labels.get("room") == room and s.labels.get("result") == result)

    def overs(result):
        return sum(s.value for s in samples if s.name == ROOM_OVERS and s.labels.get("room") == room and s.labels.get("result") == result)

    return Counts(edge("rx", "gap"), edge("rx", "late"), edge("rx", "dropped"), edge("tx", "lost"), edge("tx", "late"), edge("tx", "dropped"),
                  room_units("gap"), room_units("late"), room_units("lost"), overs("complete"), overs("lossy"), overs("end_missing"))


def families_present(samples):
    """True when the hub exports the MEDLOAD1 series at all (an image before MEDLOAD1 has none)."""
    return any(s.name in (EDGE_UNITS, ROOM_UNITS, ROOM_OVERS) for s in samples)


def delta(after, before):
    return Counts(*[a - b for a, b in zip(after, before)])


def overs_total(c):
    return c.overs_complete + c.overs_lossy + c.overs_end_missing


def wait_settled(read, before, budget_s=SETTLE_S, quiet_s=QUIET_S, clock=time.monotonic, sleep=time.sleep, poll_s=5.0, want_overs=1):
    """Reads Counts every `poll_s` until the room over count rose by `want_overs` over `before`, then reads once more after `quiet_s` (the edge records land on their own
    cadence) and returns that last Counts. Raises TimeoutError when the room count never moved within `budget_s`."""
    end = clock() + budget_s
    while True:
        now = read()
        if overs_total(now) - overs_total(before) >= want_overs:
            sleep(quiet_s)
            return read()
        if clock() >= end:
            raise TimeoutError("the room over count did not rise by %d within %.0f s (before %s, now %s)" % (want_overs, budget_s, overs_total(before), overs_total(now)))
        sleep(poll_s)


# ================================================================ the D-STAR arrival model

def plan_over(frames, drop=(), delay=None, duplicate=()):
    """Arrival order of an over of voice frames 0..frames-1 followed by the last frame (index `frames`): frames in `drop` never arrive; `delay` is {index: after_index}: that frame arrives
    right after `after_index` instead of in its place; frames in `duplicate` arrive twice in a row. Returns [frame index] in arrival order."""
    delay = dict(delay or {})
    held = {}
    for idx, after in delay.items():
        held.setdefault(after, []).append(idx)
    order = []
    for idx in range(frames + 1):
        if idx in drop or idx in delay:
            pass
        else:
            order.append(idx)
            if idx in duplicate:
                order.append(idx)
        for late in held.get(idx, ()):
            if late not in drop:
                order.append(late)
    return order


def counter_of(idx):
    """The DExtra voice counter of frame `idx` (dvtest.proto.dextra.voice: seq mod 21)."""
    return idx % MODULUS


def model_gap(order):
    """The edge's D-STAR gap count for the arrival order `order` (frame indexes), by the rule in the module docstring."""
    prev, gap = None, 0
    for idx in order:
        pos = counter_of(idx)
        if prev is None:
            prev = pos
            continue
        delta_pos = (pos + MODULUS - prev) % MODULUS
        if delta_pos == 0:
            continue
        if pos < prev and prev - pos <= BACKWARD_MAX:
            continue
        gap += delta_pos - 1
        prev = pos
    return gap


def delivered(order):
    """The frame indexes that the edge forwards (not duplicates, not backward steps): the listener hears these."""
    prev, out = None, []
    for idx in order:
        pos = counter_of(idx)
        if prev is None:
            prev = pos
            out.append(idx)
            continue
        delta_pos = (pos + MODULUS - prev) % MODULUS
        if delta_pos == 0 or (pos < prev and prev - pos <= BACKWARD_MAX):
            continue
        prev = pos
        out.append(idx)
    return out


def ignored(order):
    """How many arriving frames of `order` the edge ignores (a repeated counter or a short backward step). DV-Router counts EACH of them as a `late` unit
    (crates/lib/edge/src/core.rs 3664-3680: RxIgnored and ingress_late), so dv_edge_over_units_total{dir=rx,result=late} and the room's late rise by exactly this."""
    return len(order) - len(delivered(order))


def usrp_gap(sequences):
    """The USRP gap for arriving wire sequences (u32, modulus 2**32): the same rule on the wire sequence (a duplicate or a backward step is ignored)."""
    mod, prev, gap = 1 << 32, None, 0
    for seq in sequences:
        if prev is None:
            prev = seq
            continue
        step = (seq - prev) % mod
        if step == 0 or step > mod // 2:
            continue
        gap += step - 1
        prev = seq
    return gap


# ================================================================ S7: the loaded acceptance, rig analogue

ROUND_PASS, ROUND_STARVED, ROUND_FAIL = "pass", "HOST_STARVED", "FAIL"


def classify_round(change, sent, heard_all):
    """Classify one round of `sent` clean overs under load from the Counts delta of the round. S7 (work-units.md): "zero lost, gap, uncounted or unknown, four rounds pass with bounded repeats
    that are counted late, one non-pass labelled HOST_STARVED after complete delivery". Terms here: lost = room and egress lost; gap = edge and room gap; uncounted = sent overs minus the
    room's over count; unknown = end_missing overs. `pass`: all of them 0 and no late or dropped unit; `HOST_STARVED`: the first four 0 and every over delivered in full but late or dropped
    units were counted (the host could not keep the pace); anything else FAIL. Returns (label, reasons)."""
    reasons = []
    lost = change.room_lost + change.edge_tx_lost
    gap = change.edge_rx_gap + change.room_gap
    uncounted = sent - overs_total(change)
    unknown = change.overs_end_missing
    late = change.edge_rx_late + change.room_late + change.edge_tx_late
    dropped = change.edge_rx_dropped + change.edge_tx_dropped
    for name, value in (("lost", lost), ("gap", gap), ("uncounted", uncounted), ("unknown (end_missing)", unknown)):
        if value:
            reasons.append("%s = %g" % (name, value))
    if reasons:
        return ROUND_FAIL, reasons
    if not late and not dropped:
        return ROUND_PASS, []
    if heard_all:
        return ROUND_STARVED, ["late %g, dropped %g with every over delivered in full" % (late, dropped)]
    return ROUND_FAIL, ["late %g, dropped %g and an over was NOT delivered in full" % (late, dropped)]


def judge_rounds(labels):
    """(ok, problems) for the labels of the rounds: no FAIL, at least all but one PASS, at most one HOST_STARVED."""
    problems = []
    fails = [i for i, label in enumerate(labels) if label == ROUND_FAIL]
    starved = [i for i, label in enumerate(labels) if label == ROUND_STARVED]
    if fails:
        problems.append("round(s) %s failed" % [i + 1 for i in fails])
    if len(starved) > 1:
        problems.append("%d rounds were HOST_STARVED (S7 allows one non-pass round)" % len(starved))
    if len(labels) - len(fails) - len(starved) < len(labels) - 1:
        problems.append("fewer than %d of %d rounds passed" % (len(labels) - 1, len(labels)))
    return not problems, problems
