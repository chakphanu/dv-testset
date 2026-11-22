# SPDX-License-Identifier: MIT
"""RADIOSEQ1 judges (docs/radioseq1-cases.md): DV-Router's radio hub numbers received DMR, YSF, NXDN and P25 voice by FRAME ORDINAL (one step per DMR
burst, YSF frame, NXDN frame, P25 LDU) instead of in 20 ms units (bc9af2b1: += 3, 5, 4, 9). Pure functions over the datagrams a protocol client
heard and over parsed hub metrics; the switch DVT_RADIOSEQ1=on decides whether the cases assert or only record (dvtest/target.py).

  on(environ)                              the switch
  dmrd_bursts(datagrams)                   the DMRD datagrams as Burst rows (flags byte 15: frame type bits 5-4, voice sequence bits 3-0)
  letters(bursts) / histogram(bursts)      what a DMR listener saw: "ABCDEF..." (a letter in lower case = its sync flag disagrees), counts per letter
  superframe_verdict(bursts)               voice bursts A..F in order per superframe, sync on A only, none repeated or skipped, one stream
  cyclic_progression(values, moduli, step) a counter that must advance by `step` modulo one of `moduli` (YSF FN, NXDN SACCH structure)
  ysf_fn_values(datagrams)                 FN of the FICH of every YSF voice packet
  nxdn_sacch_structures(datagrams)         the two-bit SACCH structure of every NXDN voice packet
  counts(samples, room) / delta(a, b)      the gap and over counters of the radio edge and of one room, from dvtest.metrics samples
  judge_counts(change)                     gap 0 at the edge and in the room, the over not counted lossy
"""
import os
from collections import Counter, namedtuple

from . import medload1, metrics
from .proto import dmr, nxdn, ysf

ENV = "DVT_RADIOSEQ1"
LETTERS = "ABCDEF"
MAX_LISTED = 6                                   # positions named in one problem line
YSF_FN_MODULI = (7, 8)                           # FN 0..6 (FT 6, dvtest/proto/ysf.py stream_packets) or the 3-bit field cycling 0..7
NXDN_SACCH_STEP = -1                             # NXDN [1-B] 4.1.6: the structure of the SACCH superframe is 3, 2, 1, 0 (dvtest/proto/nxdn.py voice_payload)

Burst = namedtuple("Burst", "seq stream frame_type vseq slot")
Verdict = namedtuple("Verdict", "problems facts")


def on(environ=None):
    """DVT_RADIOSEQ1=on: the image has RADIOSEQ1 (DV-Router 917b9e3b, wip-37b7960 or later): the numbering assertions run."""
    return str((environ if environ is not None else os.environ).get(ENV, "")).strip().lower() in ("on", "1", "true", "yes")


# ---------------------------------------------------------------- DMR letters

def dmrd_bursts(datagrams):
    """Burst rows of the DMRD datagrams among `datagrams` (anything else, MSTPONG and the like, is left out), in arrival order. frame_type: 0 voice,
    1 voice sync, 2 data sync (header, terminator), 3 reserved; vseq: the voice sequence 0..5 = A..F or, for frame type 2, the data type."""
    out = []
    for data in datagrams:
        info = dmr.parse(bytes(data))
        if info is None:
            continue
        bits = dmr.parse_bitfield(info["bitfield"])
        out.append(Burst(info["seq"], info["stream"], bits["frame_type"], bits["vseq"], bits["slot"]))
    return out


def voice_bursts(bursts):
    return [b for b in bursts if b.frame_type in (dmr.VOICE, dmr.VOICE_SYNC)]


def letter(burst):
    """The letter of a voice burst. Lower case: the sync flag disagrees with the letter (sync is carried by A only). "?" for a vseq above 5."""
    if burst.vseq >= len(LETTERS):
        return "?"
    char = LETTERS[burst.vseq]
    return char if (burst.frame_type == dmr.VOICE_SYNC) == (burst.vseq == 0) else char.lower()


def letters(bursts):
    return "".join(letter(b) for b in voice_bursts(bursts))


def histogram(bursts):
    """{letter: count} of the voice bursts, letters in A..F order first (the evidence of bc9af2b1 was {"A": 50, "D": 50} for a lossless 150 word over)."""
    seen = Counter(letter(b) for b in voice_bursts(bursts))
    return {k: seen[k] for k in sorted(seen, key=lambda c: (c.upper() not in LETTERS, LETTERS.find(c.upper()), c.islower()))}


def superframe_verdict(bursts):
    """The voice bursts of ONE lossless over, in arrival order: the first is A, each next one is the following letter (F is followed by A), the sync flag is
    set on A and on no other letter, and they all belong to one stream. A trailing partial superframe is fine (a pad), a repeated or skipped letter is not."""
    voice = voice_bursts(bursts)
    facts = {"voice_bursts": len(voice), "superframes": len(voice) // 6, "tail": len(voice) % 6, "data_bursts": len(bursts) - len(voice),
             "streams": len({b.stream for b in voice}), "histogram": histogram(bursts), "letters_head": letters(bursts)[:24]}
    problems = []
    if not voice:
        return Verdict(["no voice burst heard"], facts)
    if facts["streams"] != 1:
        problems.append("%d streams among the voice bursts, one over wanted" % facts["streams"])
    wrong = [i for i, b in enumerate(voice) if b.vseq != i % 6]
    repeated = sum(1 for a, b in zip(voice, voice[1:]) if a.vseq == b.vseq)
    skipped = sum(1 for a, b in zip(voice, voice[1:]) if a.vseq != b.vseq and (b.vseq - a.vseq) % 6 != 1)
    facts.update(out_of_order=len(wrong), repeated=repeated, skipped=skipped)
    if voice[0].vseq != 0:
        problems.append("the first voice burst is %s, wanted A" % letter(voice[0]))
    if wrong:
        shown = ", ".join("#%d %s (wanted %s)" % (i, letter(voice[i]), LETTERS[i % 6]) for i in wrong[:MAX_LISTED])
        problems.append("%d of %d voice bursts are not the letter of their position: %s%s; repeated %d, skipped %d" % (
            len(wrong), len(voice), shown, " ..." if len(wrong) > MAX_LISTED else "", repeated, skipped))
    bad_sync = [i for i, b in enumerate(voice) if (b.frame_type == dmr.VOICE_SYNC) != (b.vseq == 0)]
    facts["sync_misplaced"] = len(bad_sync)
    if bad_sync:
        problems.append("%d bursts with the sync flag on a letter other than A or without it on A (first at #%d)" % (len(bad_sync), bad_sync[0]))
    return Verdict(problems, facts)


# ---------------------------------------------------------------- counters that must advance by one

def cyclic_progression(values, moduli, step=1):
    """values advance by `step` modulo m for one m of `moduli` (the first that fits every value and every step) at every position: no repeat, no skip.
    Returns Verdict(problems, facts); facts: modulus (None when none fits), steps (histogram of the observed steps, modulo the largest modulus), head."""
    values = list(values)
    facts = {"n": len(values), "head": values[:16], "modulus": None}
    if len(values) < 2:
        return Verdict(["only %d value(s) heard, 2 or more wanted" % len(values)], facts)
    top = max(moduli)
    facts["steps"] = dict(sorted(Counter((b - a) % top for a, b in zip(values, values[1:])).items()))
    for modulus in sorted(moduli):
        if all(0 <= v < modulus for v in values) and all((b - a) % modulus == step % modulus for a, b in zip(values, values[1:])):
            facts["modulus"] = modulus
            return Verdict([], facts)
    return Verdict(["the values do not advance by %+d modulo %s: steps modulo %d %s, head %s" % (
        step, "/".join(str(m) for m in sorted(moduli)), top, facts["steps"], facts["head"])], facts)


def ysf_fn_values(datagrams):
    """FN of the FICH of every YSF V/D mode 2 voice packet, in arrival order."""
    out = []
    for data in datagrams:
        info = ysf.parse(bytes(data))
        if info is not None and info["kind"] == "voice":
            out.append(info["fich"]["fn"])
    return out


def nxdn_sacch_structures(datagrams):
    """The SACCH structure field (bits 7-6 of the first SACCH byte, payload byte 1) of every NXDN voice packet, in arrival order."""
    out = []
    for data in datagrams:
        info = nxdn.parse(bytes(data))
        if info is not None and info["kind"] == "voice":
            out.append(nxdn.parse_sacch(info["payload"][1:5])["structure"])
    return out


# ---------------------------------------------------------------- hub counters

Counts = namedtuple("Counts", "edge_gap edge_late edge_dropped room_gap room_late room_lost overs_complete overs_lossy overs_end_missing "
                              "egress_lost egress_late egress_dropped")


def counts(samples, room, proc="radio"):
    """Counters of the radio edge (`proc`, ingress: dv_edge_over_units_total{dir=rx,proc=radio}) and of one room letter (lower case), from parsed hub samples
    (dvtest.metrics.parse). egress_*: dv_edge_over_units_total{dir=tx} summed over every edge (the listeners' edges: an egress drop is a word a listener
    did not get, the dmrhb EgressDropped 36 of the NXDN over of run 20261122T160037Z)."""
    def edge(direction, result, only=None):
        return sum(s.value for s in samples if s.name == medload1.EDGE_UNITS and s.labels.get("dir") == direction and s.labels.get("result") == result
                   and (only is None or s.labels.get("proc") == only))

    def room_units(result):
        return sum(s.value for s in samples if s.name == medload1.ROOM_UNITS and s.labels.get("room") == room and s.labels.get("result") == result)

    def overs(result):
        return sum(s.value for s in samples if s.name == medload1.ROOM_OVERS and s.labels.get("room") == room and s.labels.get("result") == result)

    return Counts(edge("rx", "gap", proc), edge("rx", "late", proc), edge("rx", "dropped", proc),
                  room_units("gap"), room_units("late"), room_units("lost"), overs("complete"), overs("lossy"), overs("end_missing"),
                  edge("tx", "lost"), edge("tx", "late"), edge("tx", "dropped"))


def delta(after, before):
    return Counts(*[a - b for a, b in zip(after, before)])


def judge_counts(change, over_expected=True):
    """A lossless radio over: no gap at the radio edge, none in the room, the over not counted lossy. `over_expected`: at least one over closed in the room
    (complete or end_missing; end_missing is a FINDING of the caller, not a problem here). Returns Verdict(problems, facts)."""
    facts = dict(change._asdict())
    problems = []
    if change.edge_gap:
        problems.append("dv_edge_over_units_total{dir=rx,proc=radio,result=gap} rose by %g, a lossless over has none" % change.edge_gap)
    if change.room_gap:
        problems.append("dv_room_over_units_total{result=gap} rose by %g in the room, a lossless over has none" % change.room_gap)
    if change.overs_lossy:
        problems.append("dv_room_overs_total{result=lossy} rose by %g: the lossless over was counted lossy" % change.overs_lossy)
    if over_expected and medload1.overs_total(change) < 1:
        problems.append("no over closed in the room (dv_room_overs_total did not move)")
    return Verdict(problems, facts)
