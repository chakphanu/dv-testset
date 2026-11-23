# SPDX-License-Identifier: MIT
"""dvtest/rfpos1c.py (RFPOS1 step C: frame positions of an AllStar IAX2 over) and the rig snippet rfpos1c-usrp.toml."""
import struct
import tomllib
from pathlib import Path

import pytest

from dvtest import asl_lab, crossmode, radioseq1, rfpos1b, rfpos1c, xstale1
from dvtest.proto import dmr, iax2, usrp

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "rfpos1c-usrp.toml"


def full_voice(call, stamp, payload=b"\x80" * 160, subclass=4):
    return iax2.full_frame(call, 7, stamp, 3, 1, 2, subclass, payload)


def mini(call, stamp, payload=b"\x80" * 160):
    return struct.pack("!HH", call, stamp & 0xffff) + payload


def test_switch():
    assert rfpos1c.on({"DVT_RFPOS1C": "on"})
    assert rfpos1c.on({"DVT_RFPOS1C": "1"})
    assert not rfpos1c.on({"DVT_RFPOS1C": ""})
    assert not rfpos1c.on({})


def test_frames_are_distinct_decode_exactly_and_are_deterministic():
    ulaw, pcm = rfpos1c.make_frames()
    assert len(ulaw) == len(pcm) == rfpos1c.COUNT
    assert all(len(u) == 160 for u in ulaw) and all(len(p) == 320 for p in pcm)
    assert rfpos1c.distinct(ulaw) and rfpos1c.distinct(pcm)
    assert pcm[3] == asl_lab.ulaw_to_pcm(ulaw[3])
    assert (ulaw, pcm) == rfpos1c.make_frames()
    assert ulaw != rfpos1c.make_frames(seed="other")[0]
    want = rfpos1c.expected(pcm)
    assert rfpos1c.distinct(want["dstar"]) and rfpos1c.distinct(want["dmr"]), "the fake transform keeps the positions apart"
    assert want["usrp"] == pcm
    assert want["dstar"][0] == crossmode.transform_units(5, 1, [pcm[0]])[0]
    assert all(len(f) == 9 for f in want["dstar"] + want["dmr"])


def test_voice_stamps_expand_minis_and_find_the_hole():
    call = 0x1234
    base = 5000
    payloads = [full_voice(call, base)]
    for k in range(1, 12):
        if k == 5:
            continue                                   # the lost mini frame
        payloads.append(mini(call, base + 20 * k))
    payloads.append(mini(call + 1, base + 20 * 99))    # another call: ignored
    payloads.append(b"\x00")                           # junk
    stamps = rfpos1c.voice_stamps(payloads, call)
    assert stamps[0] == base and stamps[-1] == base + 220 and len(stamps) == 11
    assert rfpos1c.holes(stamps) == ([5], [])


def test_voice_stamps_wrap_at_16_bits_and_ignore_retransmits_and_non_voice():
    call = 9
    base = 65520
    payloads = [full_voice(call, base), full_voice(call, base), iax2.full_frame(call, 7, base, 0, 0, 6, 4, b""),
                mini(call, base + 20), mini(call, base + 40), mini(call, base + 60)]
    stamps = rfpos1c.voice_stamps(payloads, call)
    assert stamps == [base, base + 20, base + 40, base + 60]
    assert rfpos1c.holes(stamps) == ([], [])


def test_voice_stamps_mid_over_full_frame_and_a_lost_full_frame():
    call = 11
    base = 1000
    payloads = [full_voice(call, base)] + [mini(call, base + 20 * k) for k in (1, 2, 3)] + [full_voice(call, base + 80)] + [mini(call, base + 20 * k) for k in (5, 6)]
    assert rfpos1c.holes(rfpos1c.voice_stamps(payloads, call)) == ([], [])
    lost_full = [p for p in payloads if p is not payloads[4]]
    assert rfpos1c.holes(rfpos1c.voice_stamps(lost_full, call)) == ([4], [])


def test_holes_off_grid():
    assert rfpos1c.holes([100, 120, 145, 160]) == ([2], [145])
    assert rfpos1c.holes([]) == ([], [])


def test_usrp_voice_matched_and_judge():
    _ulaw, pcm = rfpos1c.make_frames(6)
    datagrams = [usrp.voice(10 + i, pcm[i]) for i in (0, 1, 3, 4, 5)] + [usrp.unkey(16), b"junk"]
    rows = rfpos1c.usrp_voice(datagrams)
    assert [seq for seq, _ in rows] == [10, 11, 13, 14, 15]
    matched = rfpos1c.usrp_matched(rows, pcm)
    assert matched == [0, 1, 3, 4, 5]
    times = [0.0, 0.02, 0.10, 0.12, 0.14]                          # the hole is kept in time: 80 ms between frames 1 and 3
    problems, facts = rfpos1c.judge_usrp(matched, times, 6, dropped=2)
    assert problems == [] and facts["hole_gap_ms"] == 80.0 and facts["packets"] == 5
    # the compacted output: the same frames, no hole in time
    problems, _ = rfpos1c.judge_usrp(matched, [0.0, 0.02, 0.04, 0.06, 0.08], 6, dropped=2)
    assert len(problems) == 1 and "closed up" in problems[0]
    # a lost frame heard, a frame missing, a shifted order
    assert rfpos1c.judge_usrp([0, 1, 2, 3, 4, 5], [0.02 * i for i in range(6)], 6, dropped=2)[0]
    assert rfpos1c.judge_usrp([0, 1, 3, 4], [0.0, 0.02, 0.1, 0.12], 6, dropped=2)[0]
    # control: nothing lost, no long gap; a long gap is a finding
    assert rfpos1c.judge_usrp(list(range(6)), [0.02 * i for i in range(6)], 6)[0] == []
    assert rfpos1c.judge_usrp(list(range(6)), [0.0, 0.02, 0.04, 0.2, 0.22, 0.24], 6)[0]
    # the capture and the listener disagree: a harness slip, not a verdict on the edge
    problems, _ = rfpos1c.judge_usrp([0, 1, 3], [0.0, 0.02], 6, dropped=2)
    assert any(p.startswith("harness:") for p in problems)


def test_judge_slots_good_gap_shift_and_compaction():
    count = 90
    good = {p: p for p in range(count) if p != 40}
    assert rfpos1c.judge_slots("d", good, count, dropped=40) == []
    full = {p: p for p in range(count)}
    assert rfpos1c.judge_slots("d", full, count) == []
    # the old behaviour: the survivors renumbered by arrival, the frame count one short, everything after the loss moved by one
    compact = {p: (p if p < 40 else p + 1) for p in range(count - 1)}
    problems = rfpos1c.judge_slots("D-STAR", compact, count, dropped=40)
    assert any("missing positions [89], wanted [40]" in p for p in problems)
    assert any("first position 40 holds frame 41" in p for p in problems)
    assert rfpos1c.runs(compact) == "0-39 hold 0-39, 40-88 hold 41-89"
    # a gap at the wrong place
    assert rfpos1c.judge_slots("d", {p: p for p in range(count) if p != 39}, count, dropped=40)
    # the lost frame heard, truncated output, nothing heard
    assert any("lost frame 40 was heard" in p for p in rfpos1c.judge_slots("d", full, count, dropped=40))
    assert any("ends at position 70" in p for p in rfpos1c.judge_slots("d", {p: p for p in range(71) if p != 40}, count, dropped=40))
    assert rfpos1c.judge_slots("d", {}, count) == ["d: nothing heard"]


def test_judge_slots_dmr_burst_keeps_a_filled_slot():
    count = 90
    filled = {p: p for p in range(count)}
    filled[40] = None                                            # the lost frame's slot carries a fill, the other two frames of the burst are intact
    assert rfpos1c.judge_slots("DMR", filled, count, dropped=40, filled=True) == []
    # the old output: one frame short and shifted, the last burst padded
    shifted = {p: (p if p < 40 else p + 1) for p in range(count)}
    shifted[89] = None
    problems = rfpos1c.judge_slots("DMR", shifted, count, dropped=40, filled=True)
    assert problems and "first position 40 holds frame 41" in problems[0]
    # a whole burst dropped instead of one frame
    burst = {p: p for p in range(count) if not 39 <= p <= 41}
    assert rfpos1c.judge_slots("DMR", burst, count, dropped=40, filled=True)


def test_slots_from_dstar_and_dmr_rows():
    _ulaw, pcm = rfpos1c.make_frames(5)
    want = rfpos1c.expected(pcm)
    rows = [(0, want["dstar"][0]), (1, want["dstar"][1]), (3, want["dstar"][3]), (3, want["dstar"][3]), (4, b"\x00" * 9)]
    slots, repeats = rfpos1c.dstar_slots(rows, want["dstar"])
    assert slots == {0: 0, 1: 1, 3: 3, 4: None} and repeats == 1
    slots, _ = rfpos1c.dmr_slots([want["dmr"][0], bytes(9), want["dmr"][2]], want["dmr"])
    assert slots == {0: 0, 1: None, 2: 2}


def dmr_datagrams(want_dmr, skip=()):
    """DMRD datagrams of a listener for the frames `want_dmr` (30 bursts), without the bursts in `skip`; the edge renumbers the DMRD seq contiguously."""
    packets, _ = dmr.voice_packets(want_dmr, 1023007, 4012, 0x1234)
    kept = [p for n, p in enumerate(packets) if n not in skip]
    return [bytes(p[:4]) + bytes([n & 0xFF]) + bytes(p[5:]) for n, p in enumerate(kept)]


def test_dmr_bursts_are_placed_by_letter_not_by_arrival():
    _ulaw, pcm = rfpos1c.make_frames()
    want = rfpos1c.expected(pcm)["dmr"]
    count = rfpos1c.COUNT
    whole = dmr_datagrams(want)
    bursts, seqs = rfpos1c.dmr_bursts(whole)
    assert len(bursts) == 30 and rfpos1c.seq_gaps(seqs) == []
    slots, _ = rfpos1c.dmr_burst_slots(bursts, want)
    assert rfpos1c.judge_dmr(slots, count, None) == [] and rfpos1c.runs(slots) == "0-89 hold 0-89"
    # the product as the lost frame should be handled: burst 13 carries frames 39 and 41 and a fill at 40
    trio = list(want[39:42])
    trio[1] = bytes(9)
    filled = list(want)
    filled[39:42] = trio
    slots, _ = rfpos1c.dmr_burst_slots(rfpos1c.dmr_bursts(dmr_datagrams(filled))[0], want)
    assert slots[40] is None and slots[39] == 39 and slots[41] == 41
    assert rfpos1c.judge_dmr(slots, count, 40) == []
    # wip-e9fcc38 (run 20261123T153031Z): the whole burst 13 (frames 39..41) not sent, the later bursts at their own letters, seq renumbered: placed by letter
    datagrams = dmr_datagrams(want, skip=(13,))
    bursts, seqs = rfpos1c.dmr_bursts(datagrams)
    assert len(bursts) == 29 and 13 not in bursts and rfpos1c.seq_gaps(seqs) == []
    slots, _ = rfpos1c.dmr_burst_slots(bursts, want)
    assert sorted(set(range(count)) - set(slots)) == [39, 40, 41] and all(slots[p] == p for p in slots)
    problems = rfpos1c.judge_dmr(slots, count, 40)
    assert len(problems) == 1 and "whole burst" in problems[0] and "[39, 41]" in problems[0] and "burst granularity" in problems[0]
    # the old image: nothing missing in letters but the frames closed up (every later frame one position early, the last burst padded)
    shifted = list(want[:40]) + list(want[41:]) + [bytes(9)]
    slots, _ = rfpos1c.dmr_burst_slots(rfpos1c.dmr_bursts(dmr_datagrams(shifted))[0], want)
    problems = rfpos1c.judge_dmr(slots, count, 40)
    assert len(problems) == 1 and "first position 40 holds frame 41" in problems[0]
    # the superframe letter wrap: a burst dropped at the start of a superframe (letter A) leaves its three positions absent
    slots, _ = rfpos1c.dmr_burst_slots(rfpos1c.dmr_bursts(dmr_datagrams(want, skip=(12,)))[0], want)
    assert sorted(set(range(count)) - set(slots)) == [36, 37, 38]
    assert rfpos1c.seq_gaps([5, 6, 8, 9, 10, 255, 0]) == [(6, 8), (10, 255)]


def reading(room=None, **egress):
    room = room or radioseq1.Counts(*([0] * 11))
    return rfpos1b.Reading(room, tuple((proc, xstale1.Counts(*c)) for proc, c in egress.items()))


def test_judge_counters_loss_and_control():
    # radioseq1.Counts: edge_gap edge_late edge_dropped room_gap room_late room_lost overs_complete overs_lossy overs_end_missing egress_*
    # xstale1.Counts: x_stale complete lossy end_missing joined late dropped lost
    egress = dict(dextra=(0, 0, 1, 0, 0, 0, 0, 0), dmrhb=(0, 0, 1, 0, 0, 0, 0, 0), usrp=(0, 0, 1, 0, 0, 0, 0, 0))
    procs = ["dextra", "dmrhb", "usrp"]
    # the loss case, wip-3f142cd and step C alike: ingress gap +1, room gap +1, lost +0, one lossy over, no egress lost
    loss = reading(radioseq1.Counts(1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0), **egress)
    problems, findings, facts = rfpos1c.judge_counters(loss, 1, procs)
    assert problems == [] and findings == [] and facts["egress"]["dmrhb"]["lost"] == 0 and facts["room"]["room_gap"] == 1
    # an egress edge that counts the hole as its own loss is a FINDING (dv-router: upstream loss, +0), not a problem
    own = reading(radioseq1.Counts(1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0), dextra=(0, 0, 1, 0, 0, 0, 0, 1), dmrhb=(0, 0, 1, 0, 0, 0, 0, 0))
    problems, findings, _x = rfpos1c.judge_counters(own, 1, procs)
    assert problems == [] and len(findings) == 1 and "proc=dextra,result=lost} rose by 1" in findings[0]
    # room counters that differ: the rest of the over counted lost, the gap missing, not lossy
    bad = reading(radioseq1.Counts(0, 0, 0, 1, 0, 40, 0, 0, 0, 0, 0, 0), dextra=(0, 1, 0, 0, 0, 0, 0, 0))
    problems = rfpos1c.judge_counters(bad, 1, ["dextra"])[0]
    assert any("proc=allstar,result=gap} rose by 0, wanted 1" in p for p in problems)
    assert any("room=l,result=lost} rose by 40, wanted 0" in p for p in problems)
    assert any("result=lossy} rose by 0, wanted 1" in p for p in problems)
    # control: nothing moves, an over folded
    clean = reading(radioseq1.Counts(0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0), dextra=(0, 1, 0, 0, 0, 0, 0, 0), dmrhb=(0, 1, 0, 0, 0, 0, 0, 0))
    assert rfpos1c.judge_counters(clean, 0, ["dextra", "dmrhb"])[0] == []
    lossy = reading(radioseq1.Counts(0, 0, 0, 2, 0, 0, 0, 1, 0, 0, 0, 0), dextra=(0, 0, 1, 0, 0, 0, 0, 0))
    problems = rfpos1c.judge_counters(lossy, 0, ["dextra"])[0]
    assert any("room=l,result=gap} rose by 2, wanted 0" in p for p in problems) and any("result=lossy} rose by 1, wanted 0" in p for p in problems)
    # no over closed in the room, an edge that folded nothing: harness slips; late and dropped are findings
    idle = reading(radioseq1.Counts(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0), dextra=(0, 0, 0, 0, 0, 2, 3, 0))
    problems, findings, _x = rfpos1c.judge_counters(idle, 0, ["dextra"])
    assert any(p.startswith("harness: no over closed") for p in problems) and any(p.startswith("harness: egress edge dextra") for p in problems)
    assert findings == ["egress edge dextra counted late 2, dropped 3"]


def test_egress_procs():
    assert rfpos1c.egress_procs(["dextra", "dmrhb", "usrp", "ysf"]) == (["dextra", "dmrhb", "usrp"], [])
    assert rfpos1c.egress_procs(["dextra"]) == (["dextra"], ["dmrhb"])


def test_the_usrp_snippet_is_one_static_row_for_module_l():
    cfg = tomllib.loads(SNIPPET.read_text())
    rows = cfg["edges"]["usrp"]["endpoint"]
    assert len(rows) == 1 and rows[0]["module"] == rfpos1c.ROOM
    assert rows[0]["bind"] == "<OWN_IP>:%d" % rfpos1c.USRP_SUT_PORT
    assert rows[0]["peer"] == "<TESTER_IP>:%d" % rfpos1c.USRP_PEER_PORT
    # none of the ports of the other USRP rows (usrp-rig.toml 34001-34003) and not the dynamic hub (34101)
    assert rfpos1c.USRP_SUT_PORT not in (34001, 34002, 34003, 34101)
