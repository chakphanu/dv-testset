# SPDX-License-Identifier: MIT
"""DV-Router view of the D-STAR slow data corpus (HWFREE1 item 4, stages 1b and 1c): the 66 byte unit (little
endian bitmap, dstar.rs:122-141), the unit model of dstar.rs, the length nibble policies, the expectation fields
of vectors/dstar_sd/corpus.json (current behaviour and the planned DP5f rules) and its unit level vectors."""
import json
import os

import pytest

from dvtest import dstar_sd as sd
from dvtest import oracle_dstar_sd as orc

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _doc():
    with open(os.path.join(ROOT, "vectors", "dstar_sd", "corpus.json")) as fh:
        return json.load(fh)


def _by_name():
    return {c["case"]: c for c in _doc()["cases"]}


def _frames(rec):
    return [(int(f.split()[0]), bytes.fromhex(f.split()[1])) for f in rec["frames"]]


# ------------------------------------------------------------------ the 66 byte unit

def test_unit66_layout_and_bit_order():
    full = {f: bytes([f, f, f]) for f in range(21)}
    raw = sd.unit66(full)
    assert len(raw) == sd.UNIT66_BYTES == 66
    assert raw[:3] == bytes.fromhex("ffff1f")                   # little endian, 21 low bits set, the 3 spare bits zero
    assert raw[3:6] == b"\x00\x00\x00" and raw[3 + 60:6 + 60] == bytes([20, 20, 20])
    # bit i = frame i: frame 0 is mask 0x01 of byte 0, frame 7 mask 0x80 of byte 0, frame 8 mask 0x01 of byte 1,
    # frame 16 mask 0x01 of byte 2, frame 20 mask 0x10 of byte 2 (dstar.rs:136: b0 | b1 << 8 | b2 << 16)
    for f, (byte, bit) in {0: (0, 0), 7: (0, 7), 8: (1, 0), 16: (2, 0), 20: (2, 4)}.items():
        unit = sd.unit66({f: b"\x01\x02\x03"})
        assert unit[:3] == bytes(1 << bit if i == byte else 0 for i in range(3)), f
        assert unit[3 + 3 * f:6 + 3 * f] == b"\x01\x02\x03"
    assert sd.parse_unit66(raw) == full
    with pytest.raises(ValueError):
        sd.parse_unit66(raw[:-1])
    for spare in ("ffff3f", "ffff5f", "ffff9f", "fffff8"):      # the top 3 bits of byte 2 (dstar.rs:132); fffff8 = the retired MSB first layout
        with pytest.raises(ValueError):
            sd.parse_unit66(bytes.fromhex(spare) + raw[3:])


def test_unit66_of_a_rendered_superframe_keeps_the_wire_bytes():
    frames = sd.render(sd.halves(sd.text_blocks("N0CALL: CQ DE N0CALL")))
    units = sd.split_units(frames)
    assert len(units) == 1
    raw = sd.unit66(units[0])
    assert raw[3:6] == sd.SYNC                                   # frame 0 as received
    assert raw[6:9] == frames[1][1] and sd.xor3(raw[6:9])[:1] == b"\x40"


def test_split_units_follows_the_frame_counter_not_the_sync():
    frames = sd.render([], start=1, frames=42)
    assert [sorted(u) for u in sd.split_units(frames)] == [list(range(21))] * 2
    no_sync = [f for i, f in enumerate(frames) if i != 21]       # the second sync never arrives
    units = sd.split_units(no_sync)
    assert [len(u) for u in units] == [21, 20] and 0 not in units[1]
    assert sd.unit66(units[1])[:3] == bytes.fromhex("feff1f")      # frame 0 (bit 0) absent


# ------------------------------------------------------------------ nibble policies

def _gga_frames(mini, index=3):
    blocks = sd.simple_data_blocks(orc.GGA.encode())
    blocks[index] = bytes([mini]) + blocks[index][1:]
    return sd.render(sd.halves(blocks))


def test_nibble_policies_on_a_full_block():
    for mini, strict, ignore, dvr in ((0x35, 1, 1, 1), (0x36, 0, 1, 0), (0x3F, 0, 1, 0), (0x30, 0, 1, 1)):
        frames = _gga_frames(mini)
        got = []
        for policy in ("strict", "ignore", "dvrouter"):
            ev = sd.Decoder("reset", max_nmea=10 ** 6, nibble=policy).feed_all(frames)
            got.append(len([e for e in ev if e["kind"] == "nmea" and e["checksum_ok"]]))
        assert got == [strict, ignore, dvr], hex(mini)
    with pytest.raises(ValueError):
        sd.Decoder(nibble="loose")


def test_default_decoder_is_unchanged_by_the_nibble_option():
    frames = sd.render(sd.halves(sd.simple_data_blocks(orc.GGA.encode())))
    assert sd.Decoder().nibble == "strict"
    assert [e["kind"] for e in sd.Decoder().feed_all(frames)] == ["nmea"]


# ------------------------------------------------------------------ the unit model

def test_model_keeps_the_text_across_units_today_and_resets_it_under_dp5f():
    tb = sd.text_blocks(orc.TEXT)
    for first in (8, 9):
        frames = sd.render(sd.superframes_to_halves(sd.place(tb, first)))
        # dstar.rs:161-167: state.text and the mask persist across units: the split message completes
        assert [e["text"] for e in sd.DvrUnitDecoder().feed_frames(frames)] == [orc.TEXT], first
        # DP5f (planned): cleared at every unit, like MMDVMHost
        assert sd.DvrUnitDecoder(dp5f=True).feed_frames(frames) == [], first
    for dp5f in (False, True):
        frames = sd.render(sd.halves(tb * 3), start=2)           # offset pairing is not resynced
        assert sd.DvrUnitDecoder(dp5f=dp5f).feed_frames(frames) == []
        frames = sd.render(sd.superframes_to_halves(sd.place(tb, 6)))
        assert [e["text"] for e in sd.DvrUnitDecoder(dp5f=dp5f).feed_frames(frames)] == [orc.TEXT]


def test_model_hands_out_one_text_per_unit():
    tb = sd.text_blocks(orc.TEXT)
    twice = sd.render(sd.superframes_to_halves([tb + tb + [None] * 2]))
    assert [(e["kind"], e["seq"]) for e in sd.DvrUnitDecoder().feed_frames(twice)] == [("text", 8)]   # dstar.rs:173-175


def test_model_hole_skips_one_block_and_resets_only_the_gps_line():
    tb = sd.text_blocks(orc.TEXT)
    twice = sd.render(sd.superframes_to_halves([tb + tb + [None] * 2]))
    holed = [f for f in twice if f[0] != 4]
    ev = sd.DvrUnitDecoder().feed_frames(holed)
    assert [(e["kind"], e["seq"]) for e in ev] == [("text", 12)]            # the second copy fills the hole
    once = sd.render(sd.superframes_to_halves([tb + [None] * 6]))
    assert sd.DvrUnitDecoder().feed_frames([f for f in once if f[0] != 6]) == []
    gps = sd.render(sd.halves(sd.simple_data_blocks(orc.GGA.encode() + orc.RMC.encode())))
    ev = sd.DvrUnitDecoder().feed_frames([f for i, f in enumerate(gps) if i != 6])
    assert [e["sentence"][:6] for e in ev] == ["$GPRMC"]


def test_model_passes_header_squelch_and_fast_data_through():
    frames = sd.render(sd.halves([sd.squelch_block(19)] + sd.header_blocks(orc.RADIO_HEADER)))
    assert sd.DvrUnitDecoder().feed_frames(frames) == []
    assert sd.Decoder("reset").feed_all(frames) != []


def _units(raw):
    frames = sd.render(sd.halves(sd.simple_data_blocks(raw)))
    return frames


def _outcomes(raw, dp5f=False):
    return [r["outcome"] for r in sd.DvrUnitDecoder(dp5f=dp5f).outcomes(_units(raw))]


def _kinds(raw, dp5f=False):
    return [e["kind"] for e in sd.DvrUnitDecoder(dp5f=dp5f).feed_frames(_units(raw))]


def test_model_limits_dprs_300_including_cr_and_nmea_82_without_the_terminator():
    # D-PRS: 300 bytes from '$$CRC' to the CR (dstar.rs:57-61 and the slot of the CR, 80)
    assert _kinds(orc._dprs_of_length(299)) == _kinds(orc._dprs_of_length(300)) == ["dprs"]
    assert _kinds(orc._dprs_of_length(301)) == [] and "reject:Size" in _outcomes(orc._dprs_of_length(301))
    # NMEA: 82 stored bytes, the terminator not counted (dstar.rs:20, 62-66): with CR LF 84 bytes are accepted, 85 are not
    for total in (80, 81, 82, 83, 84):
        assert _kinds(orc._nmea_of_length(total).encode()) == ["nmea"], total
        assert set(_outcomes(orc._nmea_of_length(total).encode())) == {"parsed"}, total
    assert _kinds(orc._nmea_of_length(85).encode()) == [] and "reject:Size" in _outcomes(orc._nmea_of_length(85).encode())
    # the DP5f rule (NMEA 0183: 82 including CR LF = 80 content bytes) accepts 80, 81, 82 and rejects 83, 84, 85
    for total in (80, 81, 82):
        assert _kinds(orc._nmea_of_length(total).encode(), True) == ["nmea"], total
    for total in (83, 84, 85):
        assert _kinds(orc._nmea_of_length(total).encode(), True) == [], total
        assert "reject:Size" in _outcomes(orc._nmea_of_length(total).encode(), True), total


def test_model_cr_or_lf_ends_a_line_and_an_empty_line_gives_nothing():
    body = orc.GGA.rstrip("\r\n").encode()
    for term in (b"\r\n", b"\n", b"\r", b"\n\r", b"\r\n\r\n"):
        assert _kinds(body + term) == ["nmea"], term                 # dstar.rs:186-187: each terminator calls finish
        assert set(_outcomes(body + term)) == {"parsed"}, term       # the empty line between CR and LF is len 0: None (96-99)
    assert _kinds(body) == []                                        # no terminator: no line yet
    # D-PRS needs the CR (dstar.rs:77-79): an LF is Invalid
    dprs = sd.dprs_line(orc.DPRS_PAYLOAD, cr=False)
    assert "reject:Invalid" in _outcomes(dprs + b"\n") and _kinds(dprs + b"\n") == []
    assert _kinds(sd.dprs_line(orc.DPRS_PAYLOAD)) == ["dprs"]


def test_model_bad_checksum_is_a_crc_reject_and_resets_the_line():
    good = orc.GGA.rstrip("\r\n")
    bad_sum = good[:good.index("*") + 1] + "00"
    no_star = good[:good.index("*")]
    lower = good[:good.index("*") + 1] + good[good.index("*") + 1:].lower()
    short = good[:-1]
    for name, line in (("bad", bad_sum), ("no star", no_star), ("lower case digits", lower), ("one digit", short)):
        if line == good:
            continue
        outs = _outcomes(line.encode() + b"\r\n")
        assert "reject:Crc" in outs, name
        assert _kinds(line.encode() + b"\r\n") == [], name
    # other sentences are ignored without a check (dstar_nmea.rs:107-113): a GLL with a bad checksum is no reject
    gll = orc.GLL.rstrip("\r\n")
    assert set(_outcomes((gll[:gll.index("*") + 1] + "00").encode() + b"\r\n")) == {"parsed"}
    # after the reject the line is empty again: the next good sentence is accepted
    assert _kinds(bad_sum.encode() + b"\r\n" + orc.RMC.encode()) == ["nmea"]
    # a D-PRS line with a wrong CRC is a Crc too (dstar_dprs.rs:31-33), also one computed without the CR
    assert "reject:Crc" in _outcomes(sd.dprs_line(orc.DPRS_PAYLOAD, crc_covers_cr=False))


def test_model_reports_a_line_error_only_when_the_unit_has_no_text_item():
    tb = sd.text_blocks(orc.TEXT)
    bad = sd.simple_data_blocks((orc.GGA[:orc.GGA.index("*") + 1] + "00\r\n").encode())
    assert len(bad) == 15                                            # the CR arrives in slot 4 of the second unit
    alone = sd.DvrUnitDecoder().outcomes(sd.render(sd.superframes_to_halves(sd.place(bad))))
    assert [r["outcome"] for r in alone] == ["parsed", "reject:Crc"]
    with_text = sd.DvrUnitDecoder().outcomes(sd.render(sd.superframes_to_halves(sd.place(bad + tb))))
    # dstar.rs:208-212: the text item of the same unit hides the line error
    assert [r["outcome"] for r in with_text] == ["parsed", "parsed"]
    assert [e["kind"] for r in with_text for e in r["events"]] == ["text"]


def test_model_ignores_other_header_bytes_without_resetting_the_gps_line():
    gga = sd.simple_data_blocks(orc.GGA.encode())
    for opaque in (sd.filler_block(), sd.block(0x3F, b"ZZZZZ"), sd.block(0x36, b"ZZZZZ"), sd.block(0x50 | 5, b"ZZZZZ"),
                   sd.squelch_block(19), sd.block(0x44, b"ZZZZZ")):
        blocks = gga[:3] + [opaque] + gga[3:]
        frames = sd.render(sd.halves(blocks))
        ev = sd.DvrUnitDecoder().feed_frames(frames)
        assert [e["kind"] for e in ev] == ["nmea"], opaque.hex()      # dstar.rs:205 (0x36..0x3F too)


def test_model_overlong_line_is_discarded_to_the_next_delimiter():
    long_line = b"$GPGGA," + b"1," * 60 + b"\r\n" + orc.RMC.encode()
    ev = sd.DvrUnitDecoder().feed_frames(sd.render(sd.halves(sd.simple_data_blocks(long_line))))
    assert [e["sentence"][:6] for e in ev] == ["$GPRMC"]            # dstar.rs:72-75, 200: discard through the delimiter
    assert "reject:Size" in [r["outcome"] for r in sd.DvrUnitDecoder().outcomes(sd.render(sd.halves(sd.simple_data_blocks(long_line))))]


# ------------------------------------------------------------------ the committed corpus

def test_corpus_schema_documents_the_unit_and_the_named_differences():
    doc = _doc()
    assert doc["schema"] == "dvt-dstar-sd-corpus/3"
    assert "little endian" in doc["unit66"] and "LSB first" in doc["unit66"] and "ffff1f" in doc["unit66"]
    assert "MSB first" not in doc["unit66"] and "ASSUMPTION" not in doc["unit66"]
    assert set(doc["named_differences"]) == {"DIFF-NIBBLE", "DIFF-GPS-LIMIT", "DIFF-PAIR-HOLE", "DIFF-ONE-TEXT-PER-UNIT",
                                             "DIFF-NMEA-DELIM", "DIFF-NMEA-TYPES", "DIFF-CRC-REJECT", "DIFF-TERMINATOR"}
    assert doc["dvrouter_source"]["file"].endswith("pkt/src/dstar.rs") and "dstar.rs:136" in doc["dvrouter_source"]["rules"]["unit-bitmap"]
    assert set(doc) >= {"cases", "unit_vectors", "dvrouter_source"}


def test_every_case_has_the_dvrouter_fields_and_the_unit_reproduces_the_frames():
    for rec in _doc()["cases"]:
        name = rec["case"]
        for key in ("unit66_hex", "expected_dvrouter", "lenient_alternative", "ours_dvrouter", "dvrouter_match"):
            assert key in rec, (name, key)
        assert rec["dvrouter_match"], name
        exp = rec["expected_dvrouter"]
        assert exp["basis"] == "dvrouter-source" and len(exp["outcome"]) == len(rec["unit66_hex"]), name
        assert all(o == "parsed" or o.startswith("reject:") for o in exp["outcome"]), name
        if "expected_dvrouter_after_dp5f" in rec:
            after = rec["expected_dvrouter_after_dp5f"]
            assert after["basis"] == "lead-statement-unverified" and "DP5f" in after["rule"], name
            assert (after["text"], after["gps"], after["outcome"]) != (exp["text"], exp["gps"], exp["outcome"]), name
            assert len(after["outcome"]) == len(rec["unit66_hex"]), name
        frames = _frames(rec)
        units = sd.split_units(frames)
        assert rec["unit66_hex"] == [sd.unit66(u).hex() for u in units], name
        assert all(len(h) == 2 * sd.UNIT66_BYTES for h in rec["unit66_hex"])
        assert all(bytes.fromhex(h)[2] & 0xE0 == 0 for h in rec["unit66_hex"]), name         # no spare bit, little endian
        assert all(bytes.fromhex(h)[:3] == bytes.fromhex("ffff1f") or name.startswith(("text-lost", "text-sync", "pair-hole"))
                   for h in rec["unit66_hex"]), name                                          # every other unit is complete
        back = {}
        for n, h in enumerate(rec["unit66_hex"]):
            for seq, wire in sd.parse_unit66(bytes.fromhex(h)).items():
                back[(n, seq)] = wire
        assert sorted(back) == sorted((n, s) for n, u in enumerate(units) for s in u), name
        assert rec["lenient_alternative"] == rec["dgw"], name


def test_expected_text_after_dp5f_is_mmdvmhost_s_and_the_current_text_accumulates_like_dstargateway():
    for rec in _doc()["cases"]:
        name = rec["case"]
        if name in ("pair-hole-text-twice", "text-dvrouter-twice"):
            continue                                                # a named difference, see the next test
        cur = rec["expected_dvrouter"]["text"]
        after = rec.get("expected_dvrouter_after_dp5f", rec["expected_dvrouter"])["text"]
        assert after == [e for e in rec["mmdvm"] if e[0] == "text"], name            # the DP5f decision: MMDVMHost
        if "expected_dvrouter_after_dp5f" in rec and name != "text-lost-frame":
            assert cur == [e for e in rec["dgw"] if e[0] == "text"], name             # today: DStarGateway's accumulation
    by = _by_name()
    # one text item per unit (dstar.rs:173-175): the oracles give two, DV-Router one
    twice = by["text-dvrouter-twice"]
    assert len(twice["expected_dvrouter"]["text"]) == 1 and len([e for e in twice["mmdvm"] if e[0] == "text"]) == 2
    assert twice["named_difference"] == "DIFF-ONE-TEXT-PER-UNIT"


def test_dp5f_decision_split_and_stale_cases_differ_between_today_and_dp5f():
    by = _by_name()
    for case in ("text-slot8-split", "text-slot9-split", "text-missing-block-late", "text-stale-mix"):
        assert [e for e in by[case]["expected_dvrouter"]["text"]], case                      # today: the text comes out
        assert by[case]["expected_dvrouter_after_dp5f"]["text"] == [], case                  # DP5f: it must not
        assert by[case]["expected_dvrouter"]["text"] == [e for e in by[case]["lenient_alternative"] if e[0] == "text"], case
        assert not by[case]["lenient_differs"], case
    two = by["text-two-messages-split"]
    assert len(two["expected_dvrouter"]["text"]) == 2 and len(two["expected_dvrouter_after_dp5f"]["text"]) == 1
    assert len(two["lenient_alternative"]) == 2
    # a lost frame: the text comes out in the second unit at frame 4 today, at frame 8 under DP5f
    lost = by["text-lost-frame"]
    assert [e[1] for e in lost["expected_dvrouter"]["text"]] == [4] and [e[1] for e in lost["expected_dvrouter_after_dp5f"]["text"]] == [8]


def test_every_difference_between_expectation_and_lenient_result_is_named():
    doc = _doc()
    for rec in doc["cases"]:
        if rec["lenient_differs"]:
            assert rec.get("named_difference") in doc["named_differences"], rec["case"]
        if "named_difference" in rec:
            assert rec["lenient_differs"], rec["case"]
    assert sum(1 for r in doc["cases"] if r["lenient_differs"]) == 22


def test_gps_boundary_vectors_today_and_after_dp5f():
    by = _by_name()
    gps = lambda case: by[case]["expected_dvrouter"]["gps"]
    out = lambda case, key="expected_dvrouter": by[case].get(key, by[case]["expected_dvrouter"])["outcome"]
    dgw = lambda case: [e for e in by[case]["dgw"] if e[0] != "text"]
    # today (dstar.rs:20, 62-66, 186-187): 82 stored bytes, terminator not counted: 84 with CR LF accepted, 85 rejected
    for case in ("dprs-len-299", "dprs-len-300", "nmea-len-80", "nmea-len-81", "nmea-len-82", "nmea-len-83", "nmea-len-84"):
        assert len(gps(case)) == 1 and set(out(case)) == {"parsed"}, case
        assert [e[0] for e in gps(case)] == [e[0] for e in dgw(case)] or case.startswith("nmea"), case
    for case in ("dprs-len-301", "nmea-len-85"):
        assert gps(case) == [] and len(dgw(case)) == 1 and by[case]["named_difference"] == "DIFF-GPS-LIMIT", case
        assert out(case)[-1] == "reject:Size" and set(out(case)[:-1]) == {"parsed"}, case
    # after DP5f only nmea-len-83 and -84 change: 80 content bytes, 82 with CR LF
    after = [c["case"] for c in _doc()["cases"] if c["case"].startswith("nmea-len-") and "expected_dvrouter_after_dp5f" in c]
    assert after == ["nmea-len-83", "nmea-len-84"]
    for case in after:
        a = by[case]["expected_dvrouter_after_dp5f"]
        assert a["gps"] == [] and a["outcome"][-1] == "reject:Size" and a["basis"] == "lead-statement-unverified", case
    # line lengths including CR LF: 80, 81, 82, 83, 84, 85
    for total in (80, 81, 82, 83, 84, 85):
        assert len(orc._nmea_of_length(total)) == total and orc._nmea_of_length(total).endswith("\r\n")
    for total in (299, 300, 301):
        line = orc._dprs_of_length(total)
        assert len(line) == total and line.endswith(b"\r") and line.startswith(b"$$CRC")
    # the gps events of the accepted lengths: the same sentence DStarGateway reports, the frame may differ (CR vs LF)
    for case in ("nmea-len-80", "nmea-len-82", "nmea-len-83", "nmea-len-84"):
        assert gps(case) == dgw(case), case


def test_crc_reject_and_terminator_vectors():
    by = _by_name()
    for case in ("nmea-bad-checksum", "nmea-reject-bad-checksum", "nmea-reject-no-star", "dprs-bad-crc", "dprs-crc-without-cr"):
        rec = by[case]
        assert rec["expected_dvrouter"]["gps"] == [] and rec["expected_dvrouter"]["outcome"][-1] == "reject:Crc", case
        assert rec["named_difference"] == "DIFF-CRC-REJECT" and not [e for e in rec["dgw"] if e[0] != "text"], case   # DStarGateway silent
        assert "expected_dvrouter_after_dp5f" not in rec, case
    assert by["dprs-term-lf"]["expected_dvrouter"]["outcome"][-1] == "reject:Invalid" and by["dprs-term-lf"]["named_difference"] == "DIFF-TERMINATOR"
    lf, cr = by["nmea-term-lf-only"], by["nmea-term-cr-only"]
    assert len(lf["expected_dvrouter"]["gps"]) == 1 and lf["expected_dvrouter"]["gps"] == [e for e in lf["dgw"] if e[0] != "text"]
    assert len(cr["expected_dvrouter"]["gps"]) == 1 and not [e for e in cr["dgw"] if e[0] != "text"] and cr["named_difference"] == "DIFF-TERMINATOR"
    assert "unmodelled" in by["nmea-gga-rmc-free"]["expected_dvrouter"]
    six = by["nmea-six-types"]
    assert [e[0] for e in six["expected_dvrouter"]["gps"]] == ["nmea:GPGGA", "nmea:GPRMC"] and len(six["dgw"]) == 6
    assert six["named_difference"] == "DIFF-NMEA-TYPES"


def test_nibble_vectors_name_the_difference_and_both_results_are_recorded():
    by = _by_name()
    for case in ("nmea-nibble-0x36", "nmea-nibble-0x3f", "nmea-nibble-low", "nmea-nibble-short-block"):
        rec = by[case]
        assert rec["named_difference"] == "DIFF-NIBBLE" and rec["expected_dvrouter"]["basis"] == "dvrouter-source"
        assert [e for e in rec["dgw"] if e[0].startswith("nmea:")], case                 # DStarGateway accepts a sentence
    # the dropped / short blocks leave a sentence with a hole: its checksum fails, a Crc reject (dstar.rs:181-185, 205)
    for case in ("nmea-nibble-0x36", "nmea-nibble-0x3f", "nmea-nibble-low"):
        assert by[case]["expected_dvrouter"]["gps"] == [] and by[case]["expected_dvrouter"]["outcome"][-1] == "reject:Crc", case
    assert len(by["nmea-nibble-short-block"]["expected_dvrouter"]["gps"]) == 1
    assert set(by["nmea-nibble-short-block"]["expected_dvrouter"]["outcome"]) == {"parsed"}
    # n = 0 means 5: DV-Router and DStarGateway accept the same sentence (the CR/LF frame number differs, DIFF-NMEA-DELIM)
    zero = by["nmea-nibble-0x30"]
    assert [e[2] for e in zero["expected_dvrouter"]["gps"]] == [e[2] for e in zero["dgw"] if e[0].startswith("nmea:")]
    assert set(zero["expected_dvrouter"]["outcome"]) == {"parsed"}
    # the short-block sentence DStarGateway reports is garbled (filler bytes inside) yet passes the XOR checksum
    garbled = bytes.fromhex(by["nmea-nibble-short-block"]["dgw"][0][2])
    assert b"f" in garbled.split(b":", 1)[1] and garbled != bytes.fromhex(by["nmea-nibble-short-block"]["expected_dvrouter"]["gps"][0][2])


def test_pair_hole_vectors_have_holes_in_the_bitmap():
    by = _by_name()
    for case in ("pair-hole-text-once", "pair-hole-text-twice", "pair-hole-nmea-then-ok"):
        rec = by[case]
        assert rec["expected_dvrouter"]["basis"] == "dvrouter-source"
        assert any(sd.parse_unit66(bytes.fromhex(h)).keys() != set(range(21)) and len(sd.parse_unit66(bytes.fromhex(h))) == 20
                   for h in rec["unit66_hex"]), case
    assert by["pair-hole-text-once"]["expected_dvrouter"]["text"] == []
    assert [e[1] for e in by["pair-hole-text-twice"]["expected_dvrouter"]["text"]] == [12]
    assert by["pair-hole-text-twice"]["mmdvm"] and not [e for e in by["pair-hole-text-twice"]["mmdvm"] if e[0] == "text"]
    assert [e[0] for e in by["pair-hole-nmea-then-ok"]["expected_dvrouter"]["gps"]] == ["nmea:GPRMC"]


def test_unit_vectors_are_stated_by_hand_and_the_model_agrees():
    doc = _doc()
    vectors = doc["unit_vectors"]
    assert vectors == json.loads(json.dumps(orc.unit_vectors()))
    names = [v["name"] for v in vectors]
    assert len(names) == len(set(names))
    for want in ("unit-65-bytes", "unit-67-bytes", "unit-spare-bit-5", "unit-spare-bit-6", "unit-spare-bit-7",
                 "unit-frame0-wrong-last-byte", "unit-old-msb-first-bitmap", "seq-reset-after-invalid"):
        assert want in names
    seen = set()
    for v in vectors:
        exp = v["expected_dvrouter"]
        outs, text = orc.unit_vector_model_outcomes(v)
        assert (outs, text) == (exp["outcome"], exp["text"]), v["name"]
        assert len(v["unit_hex"]) == len(exp["outcome"]) and v["rule"].startswith(("dstar.rs:", "dstar.rs")), v["name"]
        seen |= {o for o in exp["outcome"] if o.startswith("reject:")}
    assert seen == {"reject:TooShort", "reject:Size", "reject:Invalid"}
    by = {v["name"]: v for v in vectors}
    assert len(bytes.fromhex(by["unit-65-bytes"]["unit_hex"][0])) == 65 and len(bytes.fromhex(by["unit-67-bytes"]["unit_hex"][0])) == 67
    assert by["unit-old-msb-first-bitmap"]["unit_hex"][0].startswith("fffff8")
    # the reset after a reject: without the bad unit in between the text completes, with it it does not
    assert by["seq-no-reset-without-reject"]["expected_dvrouter"]["text"] and by["seq-no-reset-without-reject"]["expected_dvrouter_after_dp5f"]["text"] == []
    assert all(by[n]["expected_dvrouter"]["text"] == [] for n in ("seq-reset-after-invalid", "seq-reset-after-too-short", "seq-reset-after-wrong-sync"))


def test_corpus_is_current_for_the_new_cases():
    names = [n for n, _, _ in orc.corpus()]
    assert len(names) == len(set(names)) == len(_doc()["cases"]) == 63
    for rec, (name, _note, frames) in zip(_doc()["cases"], orc.corpus()):
        assert rec["case"] == name
        assert rec["unit66_hex"] == [sd.unit66(u).hex() for u in sd.split_units(frames)]
        exp = orc.expected_dvrouter(name, [dict(kind=e[0], seq=e[1], hex=e[2]) for e in rec["mmdvm"]],
                                    [dict(kind=e[0], seq=e[1], hex=e[2]) for e in rec["dgw"]],
                                    orc.ours_dvrouter_view(frames), frames)
        assert rec["expected_dvrouter"] == json.loads(json.dumps(exp)), name
        after = orc.expected_dvrouter_after_dp5f(name, frames)
        assert rec.get("expected_dvrouter_after_dp5f") == json.loads(json.dumps(after)), name
