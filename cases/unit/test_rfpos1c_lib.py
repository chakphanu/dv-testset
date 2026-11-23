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
    assert len(problems) == 1 and "whole native frame" in problems[0] and "[39, 41]" in problems[0] and "native frame granularity" in problems[0]
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
    assert findings == ["egress edge dextra counted late 2"]
    # dropped is asserted everywhere (BUNDLEFILL1: wip-e9fcc38 dmrhb dropped +2 for the two survivors of a burst); tx lost of the bundled listeners too
    dropped = reading(radioseq1.Counts(1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 2), dmrhb=(0, 0, 1, 0, 0, 0, 2, 0), ysf=(0, 1, 0, 0, 0, 0, 0, 1))
    problems = rfpos1c.judge_counters(dropped, 1, ["dmrhb", "ysf"], strict_lost=("ysf",))[0]
    assert len(problems) == 2 and any("proc=dmrhb,result=dropped} rose by 2" in p for p in problems) and any("proc=ysf,result=lost} rose by 1" in p for p in problems)


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


# ---------------------------------------------------------------- BUNDLEFILL1: the bundled egresses

def test_bundle_switch():
    assert rfpos1c.bundlefill_on({"DVT_BUNDLEFILL1": "on"})
    assert not rfpos1c.bundlefill_on({"DVT_BUNDLEFILL1": ""}) and not rfpos1c.bundlefill_on({})


def test_cyclic_positions():
    assert rfpos1c.cyclic_positions([0, 1, 2, 3, 4, 5, 6, 0, 1], 7) == [0, 1, 2, 3, 4, 5, 6, 7, 8]
    assert rfpos1c.cyclic_positions([0, 1, 2, 4, 5, 6, 0], 7) == [0, 1, 2, 4, 5, 6, 7]                   # a native frame not sent is a hole
    assert rfpos1c.cyclic_positions([3, 2, 1, 0, 3, 2], 4, descending=True) == [0, 1, 2, 3, 4, 5]       # NXDN SACCH structure
    assert rfpos1c.cyclic_positions([3, 2, 0, 3], 4, descending=True) == [0, 1, 3, 4]
    assert rfpos1c.cyclic_positions([2, 3], 7) == [0, 1] and rfpos1c.cyclic_positions([], 7) == []


def p25_records(frames, skip=()):
    from dvtest.proto import p25
    records = p25.voice_records(frames, 1023007, 31672)[:-1]
    return [r for n, r in enumerate(records) if n not in skip]


def test_p25_units_by_record_type():
    frames = [bytes([i]) * 11 for i in range(36)]
    units, positions = rfpos1c.native_units("p25", p25_records(frames))
    assert positions == list(range(36)) and [units[p] for p in range(36)] == frames
    units, positions = rfpos1c.native_units("p25", p25_records(frames, skip=(20,)))
    assert sorted(set(range(36)) - set(units)) == [20] and units[21] == frames[21] and len(positions) == 35
    # the position of a unit comes from its type, not from arrival order
    ref = rfpos1c.reference({p: frames[p] for p in range(36)}, 36)
    slots = rfpos1c.native_slots(units, ref)
    assert slots[19] == 19 and slots[21] == 21 and 20 not in slots


def test_ysf_and_nxdn_units():
    from dvtest.proto import nxdn, ysf
    frames = [bytes([i + 1]) * 9 for i in range(40)]
    packets = ysf.stream_packets(frames, "N0CALL", 0)
    voice = [p for p in packets if ysf.is_voice(p)]
    assert len(voice) == 8
    units, positions = rfpos1c.native_units("ysf", voice)
    assert positions == list(range(8)) and len(units) == 40
    assert [units[p] for p in range(40)] == [ysf.vch_decode(ysf.vch_encode(f)) for f in frames]
    units, positions = rfpos1c.native_units("ysf", voice[:3] + voice[4:])                  # the fourth native frame (units 15..19) not sent
    assert positions == [0, 1, 2, 4, 5, 6, 7] and sorted(set(range(40)) - set(units)) == [15, 16, 17, 18, 19]
    nframes = [bytes([i + 1]) * 6 + b"\x80" for i in range(24)]
    nvoice = [p for p in nxdn.stream_packets(nframes, 1001, 31672) if nxdn.is_voice(p)]
    assert len(nvoice) == 6
    units, positions = rfpos1c.native_units("nxdn", nvoice)
    assert positions == list(range(6)) and [units[p] for p in range(24)] == nframes
    units, positions = rfpos1c.native_units("nxdn", nvoice[:2] + nvoice[3:])
    assert positions == [0, 1, 3, 4, 5] and sorted(set(range(24)) - set(units)) == [8, 9, 10, 11]


def test_judge_native_whole_frame_and_filled_unit():
    count = 90
    filled = {p: p for p in range(count)}
    filled[40] = None
    for per in (3, 4, 5, 9):
        assert rfpos1c.judge_native("X", filled, count, 40, per) == []
    gone = {p: p for p in range(count) if not 40 - 40 % 5 <= p < 40 - 40 % 5 + 5}
    problems = rfpos1c.judge_native("YSF", gone, count, 40, 5)
    assert len(problems) == 1 and "whole native frame" in problems[0] and "[40, 41, 42, 43, 44]" in problems[0] and "[41, 42, 43, 44]" in problems[0]
    shifted = {p: (p if p < 40 else p + 1) for p in range(count)}
    assert "first position 40 holds frame 41" in rfpos1c.judge_native("NXDN", shifted, count, 40, 4)[0]


def test_reference_and_timing():
    assert rfpos1c.reference({0: b"a", 1: b"b"}, 2) == [b"a", b"b"]
    assert rfpos1c.reference({0: b"a", 1: b"a"}, 2) is None and rfpos1c.reference({0: b"a"}, 2) is None
    t = [0.0, 0.06, 0.18, 0.24]                          # a native frame (60 ms) skipped between the second and third, the rest on time
    assert rfpos1c.timing_dev_ms(t, [0, 1, 3, 4], 60) == 0.0
    assert rfpos1c.timing_dev_ms(t, [0, 1, 2, 3], 60) == 60.0       # compacted in time
    assert rfpos1c.timing_dev_ms([0.0], [0], 60) is None
    assert rfpos1c.judge_timing("DMR", 2.0) == [] and rfpos1c.judge_timing("DMR", 60.0)
    assert rfpos1c.judge_timing("YSF", 90.0, control_ms=60.0) == [] and rfpos1c.judge_timing("YSF", 140.0, control_ms=60.0)
    assert rfpos1c.judge_timing("YSF", None)


def test_the_bundlefill_snippet_serves_module_l():
    cfg = tomllib.loads((ROOT / "rig" / "config" / "dv-router" / "bundlefill1-edges.toml").read_text())
    assert cfg["edges"]["p25"]["server_module"] == "L" and cfg["edges"]["p25"]["server_tg"] == rfpos1c.P25_TG
    assert cfg["edges"]["nxdn"]["tg_modules"] == "%d:L" % rfpos1c.P25_TG


def test_a_copy_in_the_lost_slot_is_named():
    # wip-e9fcc38 YSF (run 20261123T155348Z): the native frame is sent, every unit but 40 at its own position, slot 40 repeats the frame of position 35
    slots = {p: p for p in range(90)}
    slots[40] = 35
    problems = rfpos1c.judge_native("YSF", slots, 90, 40, 5)
    assert len(problems) == 1 and "slot (position 40) holds the frame of position 35" in problems[0] and "not a fill" in problems[0]


def test_an_absent_unit_without_fill_is_named():
    # wip-e9fcc38 P25 (run 20261123T160335Z): one record per unit, the record of position 40 is not sent, the rest at their own positions
    slots = {p: p for p in range(90) if p != 40}
    problems = rfpos1c.judge_native("P25", slots, 90, 40, 9)
    assert len(problems) == 1 and "slot (position 40) was not sent at all (no fill" in problems[0]


# ---------------------------------------------------------------- the lateness contract of the bundled egresses

def test_lateness_numbers_and_verdicts():
    # DMR: 30 bursts of 60 ms; the burst that carries the fill (13) leaves 40 ms late, the rest on time
    ctrl = {n: n * 60.0 for n in range(30)}
    loss = dict(ctrl)
    loss[13] += 40.0
    numbers = rfpos1c.lateness(ctrl, loss, "dmr")
    assert numbers["fill_ms"] == 40.0 and numbers["next_ms"] == 0.0 and numbers["after_max_ms"] == 0.0 and numbers["native_frames"] == 30
    assert rfpos1c.judge_lateness("DMR", numbers) == []
    # more than 80 ms on the fill frame, no recovery, a lasting drift: each is a defect
    late = dict(ctrl)
    late[13] += 81.0
    assert "more than 80 ms" in rfpos1c.judge_lateness("DMR", rfpos1c.lateness(ctrl, late, "dmr"))[0]
    slow = dict(ctrl)
    slow.update({n: ctrl[n] + 30.0 for n in range(14, 30)})
    problems = rfpos1c.judge_lateness("DMR", rfpos1c.lateness(ctrl, slow, "dmr"))
    assert len(problems) == 2 and "no recovery" in problems[0] and "lasting drift" in problems[1]
    drift = dict(ctrl)
    drift[25] += 25.0
    assert rfpos1c.judge_lateness("DMR", rfpos1c.lateness(ctrl, drift, "dmr")) == ["DMR: a later native frame is 25.0 ms over the control (more than 20 ms): a lasting drift"]
    # exactly the limits pass
    edge = dict(ctrl)
    edge[13] += 80.0
    edge[14] += 20.0
    edge[29] += 20.0
    assert rfpos1c.judge_lateness("DMR", rfpos1c.lateness(ctrl, edge, "dmr")) == []
    # a protocol whose fill frame is on time passes with a negative lateness
    assert rfpos1c.judge_lateness("YSF", {"fill_ms": -0.9, "next_ms": -0.9, "after_max_ms": -0.5}) == []
    assert "not sent" in rfpos1c.judge_lateness("DMR", {"fill_ms": None, "next_ms": None, "after_max_ms": None})[0]
    assert "no native frame after" in rfpos1c.judge_lateness("DMR", {"fill_ms": 10.0, "next_ms": None, "after_max_ms": None})[0]


def test_lateness_of_p25_is_per_ldu_by_the_latest_record():
    ctrl = {p: p * 20.0 for p in range(90)}
    loss = dict(ctrl)
    for p in range(40, 45):
        loss[p] += 59.0                                       # records 40..44 held until the late window gave up on 40
    numbers = rfpos1c.lateness(ctrl, loss, "p25")
    assert numbers == {"fill_ms": 59.0, "next_ms": 0.0, "after_max_ms": 0.0, "native_frames": 10}
    assert rfpos1c.judge_lateness("P25", numbers) == []
    loss[47] += 21.0                                          # a record of the next LDU late
    assert "no recovery" in rfpos1c.judge_lateness("P25", rfpos1c.lateness(ctrl, loss, "p25"))[0]
    # the padded last NXDN frame is the 23rd (index 22): inside the over, counted
    nx = {n: n * 80.0 for n in range(23)}
    slow = dict(nx)
    slow[22] += 500.0
    assert rfpos1c.lateness(nx, slow, "nxdn")["after_max_ms"] == 500.0 and rfpos1c.lateness(nx, slow, "nxdn")["native_frames"] == 23


def test_rejudge_run_from_captures(tmp_path):
    from dvtest import pcap
    from dvtest.proto import dmr
    edge, tester = "172.30.0.11", "172.30.0.30"
    frames = [bytes([i + 1]) * 9 for i in range(90)]
    packets, _ = dmr.voice_packets(frames, 1023007, 4012, 0x1234)

    def capture(case, late):
        rows = [(100.0 + n * 0.060 + late.get(n, 0.0) / 1000.0, pcap.ethernet_udp(edge, rfpos1c.DMR_SPORT, tester, 40000, bytes(p))) for n, p in enumerate(packets)]
        rows.append((99.0, pcap.ethernet_udp(edge, rfpos1c.DMR_SPORT, tester, 40000, b"RPTACK")))                      # not voice
        rows.append((100.5, pcap.ethernet_udp("172.30.0.99", rfpos1c.DMR_SPORT, tester, 40000, bytes(packets[3]))))    # another source
        path = tmp_path / "asl-lab" / ("rfpos1c-%s" % case) / "edge.pcap"
        path.parent.mkdir(parents=True)
        pcap.write(path, sorted(rows, key=lambda r: r[0]), "pcap", "<")

    capture("a", {})
    capture("b", {13: 41.0})
    capture("c", {13: 41.0, **{n: 45.0 for n in range(14, 30)}})                                                       # a lasting drift
    capture("d", {})
    result = rfpos1c.rejudge_run(tmp_path, edge)
    assert sorted(result) == ["b", "c", "d"] and sorted(result["b"]) == ["dmr"]
    assert result["b"]["dmr"]["fill_ms"] == pytest.approx(41.0, abs=0.01) and result["b"]["dmr"]["problems"] == []
    assert result["d"]["dmr"] == {"fill_ms": 0.0, "next_ms": 0.0, "after_max_ms": 0.0, "native_frames": 30, "problems": []}
    assert len(result["c"]["dmr"]["problems"]) == 2 and rfpos1c.main([str(tmp_path), edge]) == 1
