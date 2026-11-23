# SPDX-License-Identifier: MIT
"""ASLDROP1 (DV-Router image wip-42d5473; docs/asldrop1-cases.md): the AllStar edge counts a REFUSED voice packet once, in
dv_edge_over_units_total{dir=rx,result=late}. A packet is refused when it is before the over's first timestamp, behind playout (more than 60 ms late) or a
repeat of a pending position. Nothing else moves for it: not rx lost / gap / dropped, not the room's gap and lost, not the egress edges' tx lost and dropped.

The tester is the IAX2 caller (as in RFPOS1C, dvtest.proto.iax2.Client): a mini frame can be held back and sent later (`voice(drop=True)` for its own slot
then `voice_at(timestamp, ...)`), or stamped before the first packet of the over. The edge's tap capture shows the order in which the packets arrived
(`order_problems`: the harness check that the late frame really went out late).

  a  frame 40 sent 110 ms late (after frame 45, before 46): not heard, rx late +1
  b  frame 40 sent 30 ms late (after frame 41, before 42), inside the window: heard at its own position, rx late +0 (control)
  c  one mini stamped 20 ms before the over's first timestamp, sent right after the first frames: not heard, rx late +1

Pure and stdlib only (cases/unit/test_asldrop1_lib.py); the sockets and the rig are in cases/test_asldrop1.py.
"""
import os

from dvtest import medload1, rfpos1c

ENV = "DVT_ASLDROP1"
COUNT = rfpos1c.COUNT
HELD = rfpos1c.DROP                 # the frame that is delayed in a and b
LATE_MS = 110                       # a: after frame HELD + 5 (100 ms), before + 6 (120 ms): more than 60 ms behind playout
INSIDE_MS = 30                      # b: after frame HELD + 1 (20 ms), before + 2 (40 ms): inside the 60 ms window
BEFORE_AFTER = 2                    # c: the early stamped mini is sent after the frame with this index
RX = ("gap", "late", "dropped", "lost")


def on(environ=None):
    """DVT_ASLDROP1=on: the image has ASLDROP1 (wip-42d5473 or later); unset, every case skips naming the switch."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


def rx_counts(samples, proc=rfpos1c.INGRESS_PROC):
    """{gap, late, dropped, lost}: dv_edge_over_units_total{dir=rx,proc=<ingress edge>,result=...} of parsed hub samples (a missing series is 0)."""
    out = {r: 0.0 for r in RX}
    for s in samples:
        if s.name == medload1.EDGE_UNITS and s.labels.get("dir") == "rx" and s.labels.get("proc") == proc and s.labels.get("result") in out:
            out[s.labels["result"]] += s.value
    return out


def rx_delta(after, before):
    return {r: after[r] - before[r] for r in RX}


def sent_order(voice_log):
    """The call timestamps of the voice frames the tester actually sent, in send order (a dropped slot is not sent; a held frame appears where it was sent)."""
    return [stamp for stamp, kind in voice_log if kind != "drop"]


def order_problems(stamps, voice_log):
    """Harness check: the edge's capture holds the voice frames of the call in the order the tester sent them (so a delayed frame arrived after its successors
    and a frame stamped before the first arrived at all). A list of sentences, empty when it holds."""
    want = sent_order(voice_log)
    if stamps == want:
        return []
    if sorted(stamps) == sorted(want):
        first = next(i for i, (a, b) in enumerate(zip(stamps, want)) if a != b)
        return ["the capture holds the call's voice frames in another order than they were sent (first difference at packet %d: %d, sent %d)" % (first, stamps[first], want[first])]
    return ["the capture holds %d voice frames of the call, the tester sent %d (missing %s, extra %s)" % (
        len(stamps), len(want), sorted(set(want) - set(stamps))[:4], sorted(set(stamps) - set(want))[:4])]


def judge_heard(d_slots, usrp_matched, absent=None, count=COUNT):
    """What the D-STAR and USRP listeners must have heard. `absent` None: every position holds its own frame (D-STAR positions 0..count-1, USRP in order).
    `absent` k: the frame of position k is NOT heard by either listener and every other frame is heard once, in order (the contract names no position for the
    refused packet: the shape of the output around k is recorded by the case, not judged). A list of sentences."""
    out = []
    if absent is None:
        out += rfpos1c.judge_slots("D-STAR", d_slots, count, None)
        if usrp_matched != list(range(count)):
            out.append("USRP: frames heard %s in arrival order, wanted every position once, in order" % rfpos1c.runs(dict(enumerate(usrp_matched))))
        return out
    want = [i for i in range(count) if i != absent]
    heard = [d_slots[p] for p in sorted(d_slots) if p < count + 4]
    if absent in heard:
        out.append("D-STAR: the frame of position %d was heard (at position %s), it must be refused" % (absent, [p for p, i in d_slots.items() if i == absent]))
    elif heard != want:
        out.append("D-STAR: frames heard in position order %s, wanted every frame but %d once, in order" % (rfpos1c.runs(dict(enumerate(heard))), absent))
    if absent in usrp_matched:
        out.append("USRP: the frame of position %d was heard, it must be refused" % absent)
    elif usrp_matched != want:
        out.append("USRP: frames heard in arrival order %s, wanted every frame but %d once, in order" % (rfpos1c.runs(dict(enumerate(usrp_matched))), absent))
    return out


def judge_early(d_slots, usrp_matched, count=COUNT):
    """Case c: the frame stamped before the first one is not heard (the marker frame is in no slot: every received frame is a position's own) and the over
    is complete: like a control."""
    return judge_heard(d_slots, usrp_matched, None, count)


def judge_counters(change, rx, late):
    """Judge one over: `change` is a rfpos1b.Reading delta (room Counts, egress Counts per proc), `rx` the rx_delta of the ingress edge, `late` the refused
    packets expected (1 or 0). Returns (problems, findings, facts).

    Asserted: the ingress edge's rx late rose by exactly `late`; rx gap, lost and dropped by 0; the room's gap and lost by 0; every egress edge's tx lost and
    dropped by 0 and each folded the over (else a harness slip). Recorded: the room's late, a lossy over, the egress late."""
    problems, findings = [], []
    room = change.room
    facts = {"rx": dict(rx), "room": dict(room._asdict()), "egress": {proc: dict(c._asdict()) for proc, c in change.egress}}
    if medload1.overs_total(room) < 1:
        problems.append("harness: no over closed in room %s (dv_room_overs_total did not move)" % rfpos1c.ROOM_LABEL)
    if rx["late"] != late:
        problems.append("dv_edge_over_units_total{dir=rx,proc=%s,result=late} rose by %g, wanted %d" % (rfpos1c.INGRESS_PROC, rx["late"], late))
    for result in ("gap", "lost", "dropped"):
        if rx[result]:
            problems.append("dv_edge_over_units_total{dir=rx,proc=%s,result=%s} rose by %g, wanted 0 (a refused packet is counted once, as late)" % (rfpos1c.INGRESS_PROC, result, rx[result]))
    if room.room_gap:
        problems.append("dv_room_over_units_total{room=%s,result=gap} rose by %g, wanted 0" % (rfpos1c.ROOM_LABEL, room.room_gap))
    if room.room_lost:
        problems.append("dv_room_over_units_total{room=%s,result=lost} rose by %g, wanted 0" % (rfpos1c.ROOM_LABEL, room.room_lost))
    if room.room_late:
        findings.append("dv_room_over_units_total{room=%s,result=late} rose by %g (not named by the contract, recorded)" % (rfpos1c.ROOM_LABEL, room.room_late))
    if room.overs_lossy:
        findings.append("the over was counted lossy (%g): recorded, the contract does not name it" % room.overs_lossy)
    for proc, c in change.egress:
        if c.complete + c.lossy + c.end_missing + c.joined < 1:
            problems.append("harness: egress edge %s folded no over: its listener did not take the stream" % proc)
        for result in ("lost", "dropped"):
            if getattr(c, result):
                problems.append("dv_edge_over_units_total{dir=tx,proc=%s,result=%s} rose by %g, wanted 0" % (proc, result, getattr(c, result)))
        if c.late:
            findings.append("egress edge %s counted late %g" % (proc, c.late))
    return problems, findings, facts
