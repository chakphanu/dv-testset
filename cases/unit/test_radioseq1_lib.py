# SPDX-License-Identifier: MIT
"""dvtest.radioseq1 (DVT_RADIOSEQ1, docs/radioseq1-cases.md): the DMRD flags decoder, the superframe-order checker, the cyclic counters of YSF FN and NXDN SACCH,
the hub counters of a lossless radio over. Offline and deterministic: the datagrams are built with the encoders of dvtest.proto."""
import pytest

from dvtest import metrics, radioseq1 as rs
from dvtest.proto import dmr, nxdn, ysf

pytestmark = pytest.mark.unit

STREAM = 0x01020304
ZERO = bytes(33)


def burst_packet(seq, ftype, vseq, stream=STREAM, slot=2):
    return dmr.packet(seq, 1023007, 4017, 1, dmr.bitfield(slot=slot, frame_type=ftype, vseq=vseq), stream, ZERO)


def dmr_over(letters_seq, stream=STREAM):
    """Header, one voice burst per entry of letters_seq (vseq numbers, sync flag on 0), terminator."""
    out = [burst_packet(0, dmr.DATA, dmr.HEADER, stream)]
    for i, vseq in enumerate(letters_seq):
        out.append(burst_packet(i + 1, dmr.VOICE_SYNC if vseq == 0 else dmr.VOICE, vseq, stream))
    out.append(burst_packet(len(letters_seq) + 1, dmr.DATA, dmr.TERMINATOR, stream))
    return out


def good(n=50):
    return [i % 6 for i in range(n)]


# ---------------------------------------------------------------- DMRD flags

def test_switch():
    assert rs.on({"DVT_RADIOSEQ1": "on"}) and rs.on({"DVT_RADIOSEQ1": " ON "})
    assert not rs.on({}) and not rs.on({"DVT_RADIOSEQ1": "off"}) and not rs.on({"DVT_RADIOSEQ1": ""})


def test_flags_decoding_of_each_burst_type():
    pkts = [burst_packet(1, dmr.VOICE_SYNC, 0), burst_packet(2, dmr.VOICE, 3, slot=1), burst_packet(3, dmr.DATA, dmr.TERMINATOR)]
    rows = rs.dmrd_bursts(pkts + [b"MSTPONG\x00\x00\x00\x00", b"DMRD short"])
    assert [(r.seq, r.frame_type, r.vseq, r.slot) for r in rows] == [(1, 1, 0, 2), (2, 0, 3, 1), (3, 2, dmr.TERMINATOR, 2)]
    assert all(r.stream == STREAM for r in rows)
    assert [r.seq for r in rs.voice_bursts(rows)] == [1, 2]


def test_letters_and_histogram_of_a_good_over():
    rows = rs.dmrd_bursts(dmr_over(good(12)))
    assert rs.letters(rows) == "ABCDEFABCDEF"
    assert rs.histogram(rows) == {c: 2 for c in "ABCDEF"}


def test_a_sync_flag_that_disagrees_shows_in_lower_case():
    rows = rs.dmrd_bursts([burst_packet(1, dmr.VOICE, 0), burst_packet(2, dmr.VOICE_SYNC, 3), burst_packet(3, dmr.VOICE, 7)])
    assert rs.letters(rows) == "ad?"


# ---------------------------------------------------------------- the superframe checker

def test_a_lossless_150_word_over_passes():
    verdict = rs.superframe_verdict(rs.dmrd_bursts(dmr_over(good(50))))
    assert verdict.problems == []
    assert verdict.facts["voice_bursts"] == 50 and verdict.facts["superframes"] == 8 and verdict.facts["tail"] == 2
    assert verdict.facts["repeated"] == verdict.facts["skipped"] == verdict.facts["sync_misplaced"] == 0
    assert verdict.facts["data_bursts"] == 2 and verdict.facts["streams"] == 1


def test_the_bc9af2b1_evidence_a_and_d_only_fails_with_the_histogram():
    # += 3 per burst: A (sync) and D (voice), the room over_gap 2 per burst
    verdict = rs.superframe_verdict(rs.dmrd_bursts(dmr_over([0, 3] * 25)))
    assert verdict.facts["histogram"] == {"A": 25, "D": 25}
    assert verdict.facts["skipped"] == 49 and verdict.facts["repeated"] == 0
    assert verdict.problems and "not the letter of their position" in verdict.problems[0]
    assert "wanted B" in verdict.problems[0]


def test_a_repeated_letter_is_found():
    seq = [0, 1, 2, 2, 3, 4, 5]
    verdict = rs.superframe_verdict(rs.dmrd_bursts(dmr_over(seq)))
    assert verdict.facts["repeated"] == 1
    assert verdict.problems


def test_a_skipped_letter_is_found():
    seq = [0, 1, 2, 4, 5, 0, 1]
    verdict = rs.superframe_verdict(rs.dmrd_bursts(dmr_over(seq)))
    assert verdict.facts["skipped"] == 1 and verdict.facts["repeated"] == 0
    assert verdict.problems


def test_an_over_that_starts_after_a_is_found():
    verdict = rs.superframe_verdict(rs.dmrd_bursts(dmr_over([2, 3, 4, 5, 0, 1])))
    assert any("first voice burst is" in p for p in verdict.problems)


def test_sync_must_be_on_a_only():
    pkts = [burst_packet(1, dmr.VOICE_SYNC, 0), burst_packet(2, dmr.VOICE_SYNC, 1), burst_packet(3, dmr.VOICE, 2)]
    verdict = rs.superframe_verdict(rs.dmrd_bursts(pkts))
    assert verdict.facts["sync_misplaced"] == 1
    assert any("sync flag" in p for p in verdict.problems)
    # A without its sync flag is the same mistake
    verdict = rs.superframe_verdict(rs.dmrd_bursts([burst_packet(1, dmr.VOICE, 0), burst_packet(2, dmr.VOICE, 1)]))
    assert verdict.facts["sync_misplaced"] == 1


def test_two_streams_and_an_empty_over_are_problems():
    pkts = dmr_over(good(6), stream=1) + dmr_over(good(6), stream=2)
    verdict = rs.superframe_verdict(rs.dmrd_bursts(pkts))
    assert verdict.facts["streams"] == 2 and any("2 streams" in p for p in verdict.problems)
    assert rs.superframe_verdict([]).problems == ["no voice burst heard"]
    # the join of a client (header and terminator only) has no voice burst
    assert rs.superframe_verdict(rs.dmrd_bursts(dmr_over([]))).problems == ["no voice burst heard"]


def test_a_long_run_of_errors_is_named_briefly():
    verdict = rs.superframe_verdict(rs.dmrd_bursts(dmr_over([0, 3] * 25)))
    assert len(verdict.problems[0]) < 400 and verdict.problems[0].count("#") <= rs.MAX_LISTED


# ---------------------------------------------------------------- cyclic counters

def test_cyclic_progression_accepts_modulus_7_and_8_and_names_the_one_found():
    assert rs.cyclic_progression([0, 1, 2, 3, 4, 5, 6, 0, 1], rs.YSF_FN_MODULI).facts["modulus"] == 7
    assert rs.cyclic_progression([6, 7, 0, 1], rs.YSF_FN_MODULI).facts["modulus"] == 8
    assert rs.cyclic_progression([0, 1, 2], rs.YSF_FN_MODULI).problems == []


def test_cyclic_progression_finds_repeats_and_skips():
    assert rs.cyclic_progression([0, 1, 1, 2], rs.YSF_FN_MODULI).problems
    assert rs.cyclic_progression([0, 1, 3, 4], rs.YSF_FN_MODULI).problems
    bad = rs.cyclic_progression([0, 5, 2, 7, 4, 1], rs.YSF_FN_MODULI)    # a +5 step per frame, the 20 ms unit numbering of bc9af2b1
    assert bad.problems and bad.facts["modulus"] is None
    assert rs.cyclic_progression([3], rs.YSF_FN_MODULI).problems          # not enough values


def test_cyclic_progression_descending_for_the_nxdn_sacch():
    assert rs.cyclic_progression([3, 2, 1, 0, 3, 2, 1, 0], (4,), rs.NXDN_SACCH_STEP).problems == []
    assert rs.cyclic_progression([3, 3, 3, 3], (4,), rs.NXDN_SACCH_STEP).problems        # a step of 4 per packet: the structure never moves
    assert rs.cyclic_progression([0, 1, 2, 3], (4,), rs.NXDN_SACCH_STEP).problems


def test_ysf_fn_values_come_from_the_fich_of_voice_packets_only():
    packets = ysf.stream_packets([ysf.SILENCE] * 40, "N0CALL", dgid=0)
    assert packets[0] and ysf.parse(packets[0])["kind"] == "header"
    values = rs.ysf_fn_values(packets + [b"YSFP" + bytes(10)])
    assert values == [i % 7 for i in range(8)]
    assert rs.cyclic_progression(values, rs.YSF_FN_MODULI).facts["modulus"] == 7


def test_ysf_fn_values_of_a_skipping_numbering():
    fn = [0, 5, 2, 7]
    body = [ysf.packet("N0CALL", "N0CALL", ysf.ALL, i, ysf.voice_frame(f, [ysf.SILENCE] * 5, ysf.dch_text(f, "N0CALL", "N0CALL"))) for i, f in enumerate(fn)]
    assert rs.ysf_fn_values(body) == fn
    assert rs.cyclic_progression(rs.ysf_fn_values(body), rs.YSF_FN_MODULI).problems


def nxdn_voice_packets(indexes):
    frames = [bytes(7)] * 4
    return [nxdn.packet(1001, 2005, nxdn.voice_payload(frames, 1001, 2005, index)) for index in indexes]


def test_nxdn_sacch_structures_follow_the_packet_index():
    good_ = rs.nxdn_sacch_structures(nxdn_voice_packets(range(8)))
    assert good_ == [3, 2, 1, 0, 3, 2, 1, 0]
    assert rs.cyclic_progression(good_, (4,), rs.NXDN_SACCH_STEP).problems == []
    stuck = rs.nxdn_sacch_structures(nxdn_voice_packets([0, 4, 8, 12]))              # += 4 per packet: the same part every time
    assert stuck == [3, 3, 3, 3] and rs.cyclic_progression(stuck, (4,), rs.NXDN_SACCH_STEP).problems


def test_nxdn_header_and_trailer_are_not_voice():
    head = nxdn.packet(1001, 2005, nxdn.control_payload(nxdn.VCALL, 1001, 2005))
    tail = nxdn.packet(1001, 2005, nxdn.control_payload(nxdn.TX_REL, 1001, 2005))
    assert rs.nxdn_sacch_structures([head] + nxdn_voice_packets(range(2)) + [tail]) == [3, 2]


# ---------------------------------------------------------------- hub counters

def sample(name, value, **labels):
    return metrics.Sample(name, labels, value)


def hub(edge_gap=0, room_gap=0, complete=0, lossy=0, end_missing=0, tx_dropped=0, room="q"):
    return [
        sample("dv_edge_over_units_total", edge_gap, dir="rx", proc="radio", result="gap"),
        sample("dv_edge_over_units_total", 99, dir="rx", proc="dextra", result="gap"),          # another edge: not the radio's
        sample("dv_edge_over_units_total", 0, dir="rx", proc="radio", result="late"),
        sample("dv_edge_over_units_total", tx_dropped, dir="tx", proc="dmrhb", result="dropped"),
        sample("dv_room_over_units_total", room_gap, proc=room, result="gap", room=room),
        sample("dv_room_over_units_total", 77, proc="p", result="gap", room="p"),               # another room
        sample("dv_room_overs_total", complete, room=room, result="complete"),
        sample("dv_room_overs_total", lossy, room=room, result="lossy"),
        sample("dv_room_overs_total", end_missing, room=room, result="end_missing"),
    ]


def test_counts_take_the_radio_edge_and_one_room_only():
    c = rs.counts(hub(edge_gap=100, room_gap=100, complete=3, lossy=2, tx_dropped=36), "q")
    assert (c.edge_gap, c.room_gap, c.overs_complete, c.overs_lossy, c.egress_dropped) == (100, 100, 3, 2, 36)
    assert rs.counts(hub(), "p").room_gap == 77


def test_the_evidence_of_the_rif5j_run_is_judged_a_failure_and_a_clean_over_passes():
    before = rs.counts(hub(), "q")
    bad = rs.delta(rs.counts(hub(edge_gap=100, room_gap=100, complete=0, lossy=1), "q"), before)
    verdict = rs.judge_counts(bad)
    assert len(verdict.problems) == 3 and verdict.facts["edge_gap"] == 100
    clean = rs.delta(rs.counts(hub(complete=1), "q"), before)
    assert rs.judge_counts(clean).problems == []


def test_an_over_that_never_closed_and_end_missing():
    before = rs.counts(hub(), "q")
    none = rs.delta(rs.counts(hub(), "q"), before)
    assert rs.judge_counts(none).problems == ["no over closed in the room (dv_room_overs_total did not move)"]
    assert rs.judge_counts(none, over_expected=False).problems == []
    missing = rs.delta(rs.counts(hub(end_missing=1), "q"), before)
    assert rs.judge_counts(missing).problems == []
