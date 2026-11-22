# SPDX-License-Identifier: MIT
"""dvtest.radioseq1 (DVT_RADIOSEQ1, docs/radioseq1-cases.md): the DMRD flags decoder, the superframe-order checker, the cyclic counters of YSF FN and NXDN SACCH,
the hub counters of a lossless radio over. Offline and deterministic: the datagrams are built with the encoders of dvtest.proto."""
import tomllib
from pathlib import Path

import pytest

from dvtest import metrics, radioseq1 as rs
from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_serial as ms
from dvtest import rif5, sut_config
from dvtest.audio import signals
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


# ---------------------------------------------------------------- YSF FN: FN 0..6 (FT 6), FN 7 is a FINDING

def test_ysf_fn_check_accepts_the_cycle_0_to_6_and_names_modulus_7():
    values = [i % 7 for i in range(30)]
    got = rs.ysf_fn_check(values, [6] * 30)
    assert got.problems == [] and got.findings == []
    assert got.facts["modulus"] == 7 and got.facts["fn7"] == 0 and got.facts["ft"] == [6]


def test_ysf_fn_7_on_a_voice_packet_is_a_finding_not_a_problem_and_modulus_8_is_recorded():
    values = [i % 8 for i in range(24)]
    got = rs.ysf_fn_check(values, [7] * 24)
    assert got.problems == []
    assert got.facts["modulus"] == 8 and got.facts["fn7"] == 3
    assert len(got.findings) == 2
    assert "FN 7 on 3 of 24 voice packets" in got.findings[0] and "FT of the voice packets [7]" in got.findings[1]


def test_ysf_fn_check_problems_are_repeats_skips_and_short_runs():
    assert rs.ysf_fn_check([0, 1, 1, 2]).problems
    assert rs.ysf_fn_check([0, 1, 3, 4]).problems
    assert rs.ysf_fn_check([0, 2, 4, 6, 1]).problems                      # a +2 step per packet
    assert rs.ysf_fn_check([0, 5, 2, 7, 4, 1]).facts["modulus"] is None   # the 20 ms unit numbering of bc9af2b1
    assert rs.ysf_fn_check([3]).problems
    # FN 7 with a wrong step is a problem (and the FN 7 count is still a fact)
    got = rs.ysf_fn_check([0, 1, 2, 7, 4])
    assert got.problems and got.facts["fn7"] == 1 and got.facts["modulus"] is None


def test_ysf_ft_values_come_from_voice_packets_only():
    packets = ysf.stream_packets([ysf.SILENCE] * 40, "N0CALL", dgid=0)
    assert rs.ysf_ft_values(packets) == [6] * 8                           # the header and the terminator (FT 7) are not voice
    assert rs.ysf_fn_check(rs.ysf_fn_values(packets), rs.ysf_ft_values(packets)).facts["ft"] == [6]


# ---------------------------------------------------------------- RADIOSEQ1-C: two radio ports

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"


def two_port_doc(first="rif5-mmdvm-udp.toml", second=rs.SNIPPET_B):
    extras = [(SNIPPETS / first).read_text(encoding="utf-8")] + ([(SNIPPETS / second).read_text(encoding="utf-8")] if second else [])
    text = sut_config.render('[node]\nbind_ip = "<OWN_IP>"\n', "172.30.0", own=11, tester=30, extras=extras)
    return tomllib.loads(text)


def test_the_two_ports_snippet_adds_one_row_after_either_first_snippet():
    for first in ("rif5-mmdvm-udp.toml", "rif5n-modes.toml"):
        doc = two_port_doc(first)
        names = [p["name"] for p in doc["edges"]["radio"]["port"]]
        assert names == ["rif5-modem", "rif5-modem-b"]
        a, b = rs.port_pair(doc)
        assert (a.name, b.name) == (rif5.PORT_NAME, rs.PORT_B)
        assert rs.pair_problems(a, b) == []
        # the key names of config.md, distinct per port, the same DMR room and talk group, one edge-level acknowledgement
        assert (a.peer, a.bind) == (("172.30.0.30", 3340), ("172.30.0.11", 3341)) and (b.peer, b.bind) == (("172.30.0.30", 3342), ("172.30.0.11", 3343))
        assert a.modes["dmr"] == b.modes["dmr"] == "Q" and b.modes == {"dmr": "Q"}
        assert a.physical != b.physical and b.tx is True and b.dmr_tg == a.dmr_tg == 4017 and b.color_code == a.color_code
        row = doc["edges"]["radio"]["port"][1]
        assert row["band"] == "70cm" and 430_000_000 <= row["rx_freq"] <= 440_000_000 and row["tx_freq"] == row["rx_freq"]
        assert "tx_ack" not in row and doc["edges"]["radio"]["tx_ack"].startswith("I am the control operator of N0CALL ")


def test_without_the_snippet_there_is_no_pair_and_a_first_b_row_is_not_port_a():
    doc = two_port_doc(second=None)
    a, b = rs.port_pair(doc)
    assert a.name == rif5.PORT_NAME and b is None
    assert rs.pair_problems(a, b)
    swapped = two_port_doc()
    swapped["edges"]["radio"]["port"].reverse()
    a, b = rs.port_pair(swapped)
    assert (a.name, b.name) == (rif5.PORT_NAME, rs.PORT_B)
    only_b = two_port_doc()
    del only_b["edges"]["radio"]["port"][0]
    a, b = rs.port_pair(only_b)
    assert a is None and b.name == rs.PORT_B


def test_pair_problems_single_fields():
    a, b = rs.port_pair(two_port_doc())
    copy = lambda **kw: type(b)(**{**vars(b), **kw})
    assert any("same name" in p for p in rs.pair_problems(a, copy(name=a.name)))
    assert any("same udp_bind" in p for p in rs.pair_problems(a, copy(bind=a.bind)))
    assert any("same physical" in p and "one physical is one transmitter" in p for p in rs.pair_problems(a, copy(physical=a.physical)))
    assert any("room differs" in p for p in rs.pair_problems(a, copy(modes={"dmr": "R"})))
    assert any("no dmr mode" in p for p in rs.pair_problems(a, copy(modes={"ysf": "R"})))
    assert any("tx = False" in p for p in rs.pair_problems(a, copy(tx=False)))
    assert any("without a physical" in p for p in rs.pair_problems(a, copy(physical="")))
    assert rs.pair_problems(a, copy()) == [] and rs.pair_problems(a, None)


def tx_log(n_words, src=1023007, dst=4017, cc=1, headers=8, terminators=3, drop_burst=None, double_burst=None, no_sync=False, t0=10.0):
    """Host frames of a DMR TX of n_words words, as a board of case C would log them."""
    words = rf.canonical_frames("dmr", [signals.prng_bytes("c/%d" % i, 9) for i in range(n_words)])
    up = rf.dmr_keyup(words, src, dst, colour_code=cc)
    head, voice, term = up[0], up[1:-1], up[-1]
    if drop_burst is not None:
        voice = voice[:drop_burst] + voice[drop_burst + 1:]
    if double_burst is not None:
        voice = voice[:double_burst + 1] + voice[double_burst:]
    items = [(ms.SET_MODE, bytes((2,)))]
    items += [(ms.DMR_DATA2, b"\x00" + head[1][1:])] * headers
    items += [(ms.DMR_DATA2, b"\x00" + v[1][1:]) for v in voice]
    items += [(ms.DMR_DATA2, b"\x00" + term[1][1:])] * terminators
    return [ms.HostFrame(t0 + 0.06 * i, t0 + 0.06 * i, ftype, bytes(payload)) for i, (ftype, payload) in enumerate(items)], rf.padded_frames("dmr", words)


def test_a_relayed_over_with_every_burst_in_order_passes():
    for n in (18, 36, 144):
        log, sent = tx_log(n)
        got = rs.relay_verdict(log, sent, 4017, 1)
        assert got.problems == [] and got.findings == [], (n, got)
        assert got.facts["letters_n"] == n // 3 and got.facts["letters_head"].startswith("ABCDEFABC"[:min(9, n // 3)])
        assert got.facts["src"] == 1023007 and got.facts["voice_written"] == n


def test_dmr_tx_letters_count_from_the_sync_burst():
    log, _ = tx_log(54)
    assert rs.dmr_tx_letters(log) == "ABCDEF" * 3
    assert rs.dmr_tx_letters([]) == ""
    log, _ = tx_log(18)
    # without the burst A first the letters have no origin
    no_a = [f for i, f in enumerate(log) if i != 9]                                  # header x8 at 1..8, burst A at 9
    assert rs.dmr_tx_letters(no_a) == "?????"


def test_a_missing_repeated_or_out_of_order_burst_is_a_problem():
    log, sent = tx_log(36, drop_burst=3)
    got = rs.relay_verdict(log, sent, 4017, 1)
    assert got.problems and any("voice burst letters differ" in p for p in got.problems)
    assert any("written" in p or "voice" in p for p in got.problems)
    log, sent = tx_log(36, double_burst=2)
    assert rs.relay_verdict(log, sent, 4017, 1).problems
    # a short run: every word wanted
    log, sent = tx_log(36)
    short = [f for i, f in enumerate(log) if not 15 <= i < 21]
    got = rs.relay_verdict(short, sent, 4017, 1)
    assert got.problems and any("voice burst letters differ" in p for p in got.problems)


def test_the_relayed_over_must_name_the_row_room_and_one_source():
    log, sent = tx_log(18)
    assert any("dst" in p for p in rs.relay_verdict(log, sent, 4020, 1).problems)
    assert any("colour code" in p for p in rs.relay_verdict(log, sent, 4017, 2).problems)
    log, sent = tx_log(18, headers=1)
    assert any("8 expected" in p for p in rs.relay_verdict(log, sent, 4017, 1).problems)
    # no header at all: the source is 0 and the verdict still comes back
    log, sent = tx_log(18, headers=0)
    got = rs.relay_verdict(log, sent, 4017, 1)
    assert got.facts["src"] == 0 and got.problems


# ---------------------------------------------------------------- fixed-mode board (a single-mode row: SET_MODE once at Up, radio.md 7.2)

def mode_frame(payload, mono):
    return ms.HostFrame(mono, mono, ms.SET_MODE, bytes((payload,)))


def fixed_log(n_words=18):
    """The log of a fixed-mode board over one over: NO SET_MODE inside the window (the claim was given at Up, before it)."""
    log, sent = tx_log(n_words)
    return [f for f in log if f.ftype != ms.SET_MODE], sent


def test_fixed_mode_is_a_row_with_exactly_one_mode():
    import types
    assert rs.fixed_mode(types.SimpleNamespace(modes={"dmr": "Q"}))
    assert not rs.fixed_mode(types.SimpleNamespace(modes={"dmr": "Q", "dstar": "P"}))
    assert not rs.fixed_mode(types.SimpleNamespace(modes={}))
    assert not rs.fixed_mode(types.SimpleNamespace())
    for first in ("rif5-mmdvm-udp.toml", "rif5n-modes.toml"):                          # the rendered rows: A is multi-mode (rif5n-modes) or single (the plain udp snippet), B is fixed
        a, b = rs.port_pair(two_port_doc(first))
        assert rs.fixed_mode(b)
    assert not rs.fixed_mode(rs.port_pair(two_port_doc("rif5n-modes.toml"))[0])


def test_a_fixed_mode_board_claimed_at_up_passes_without_a_claim_in_the_over():
    log, sent = fixed_log()
    got = rs.relay_verdict(log, sent, 4017, 1, fixed=True, earlier=[mode_frame(2, 1.0)])
    assert got.problems == [] and got.findings == [], got
    # the same log judged as a multi-mode board is the old failure
    old = rs.relay_verdict(log, sent, 4017, 1)
    assert any("no SET_MODE 02" in p for p in old.problems)


def test_a_fixed_mode_board_may_park_in_idle_and_re_enter():
    log, sent = fixed_log()
    earlier = [mode_frame(2, 1.0), mode_frame(0, 5.0), mode_frame(2, 6.0)]
    assert rs.relay_verdict(log, sent, 4017, 1, fixed=True, earlier=earlier).problems == []
    # the re-entry may also fall inside the window, before the first frame
    log2, sent2 = tx_log(18)
    assert rs.relay_verdict(log2, sent2, 4017, 1, fixed=True, earlier=[mode_frame(2, 1.0), mode_frame(0, 5.0)]).problems == []


def test_a_fixed_mode_board_left_in_idle_or_another_mode_is_a_problem():
    log, sent = fixed_log()
    got = rs.relay_verdict(log, sent, 4017, 1, fixed=True, earlier=[mode_frame(2, 1.0), mode_frame(0, 5.0)])
    assert any("last SET_MODE" in p and "want" in p for p in got.problems), got
    got = rs.relay_verdict(log, sent, 4017, 1, fixed=True, earlier=[mode_frame(2, 1.0), mode_frame(1, 5.0)])
    assert any("last SET_MODE" in p for p in got.problems)


def test_a_fixed_mode_board_with_no_set_mode_at_all_is_a_finding_not_a_problem():
    log, sent = fixed_log()
    got = rs.relay_verdict(log, sent, 4017, 1, fixed=True, earlier=[])
    assert got.problems == [] and any("never got a SET_MODE" in f for f in got.findings), got


def test_an_idle_inside_the_over_of_a_fixed_mode_board_is_a_problem_but_one_after_it_is_not():
    log, sent = fixed_log()
    mid = log[len(log) // 2].mono
    inside = sorted(log + [mode_frame(0, mid)], key=lambda f: f.mono)
    got = rs.relay_verdict(inside, sent, 4017, 1, fixed=True, earlier=[mode_frame(2, 1.0)])
    assert any("never interrupts a stream" in p for p in got.problems), got
    after = log + [mode_frame(0, log[-1].mono + 1.0)]
    assert rs.relay_verdict(after, sent, 4017, 1, fixed=True, earlier=[mode_frame(2, 1.0)]).problems == []


def test_a_multi_mode_board_keeps_the_claim_per_over_rule():
    log, sent = tx_log(18)
    assert rs.relay_verdict(log, sent, 4017, 1).problems == []
    no_claim = [f for f in log if f.ftype != ms.SET_MODE]
    assert any("no SET_MODE 02" in p for p in rs.relay_verdict(no_claim, sent, 4017, 1).problems)
