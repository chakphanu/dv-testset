# SPDX-License-Identifier: MIT
"""DV-Router view of the D-STAR slow data corpus (HWFREE1 item 4, stage 1b): the 66 byte unit, the
unit model, the length nibble policies, the expectation fields of vectors/dstar_sd/corpus.json."""
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
    assert raw[:3] == bytes.fromhex("fffff8")                   # 21 bits set, the 3 spare bits zero
    assert raw[3:6] == b"\x00\x00\x00" and raw[3 + 60:6 + 60] == bytes([20, 20, 20])
    # frame 0 is bit 7 of byte 0, frame 7 bit 0 of byte 0, frame 8 bit 7 of byte 1, frame 20 bit 3 of byte 2
    for f, (byte, bit) in {0: (0, 7), 7: (0, 0), 8: (1, 7), 16: (2, 7), 20: (2, 3)}.items():
        unit = sd.unit66({f: b"\x01\x02\x03"})
        assert unit[:3] == bytes(1 << bit if i == byte else 0 for i in range(3)), f
        assert unit[3 + 3 * f:6 + 3 * f] == b"\x01\x02\x03"
    assert sd.parse_unit66(raw) == full
    with pytest.raises(ValueError):
        sd.parse_unit66(raw[:-1])
    with pytest.raises(ValueError):
        sd.parse_unit66(bytes.fromhex("fffffc") + raw[3:])       # a spare bit set


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
    assert sd.unit66(units[1])[:3] == bytes.fromhex("7ffff8")


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

def test_model_resets_the_text_at_every_unit_and_does_not_pair_over_a_sync():
    tb = sd.text_blocks(orc.TEXT)
    for first in (8, 9):
        frames = sd.render(sd.superframes_to_halves(sd.place(tb, first)))
        assert sd.DvrUnitDecoder().feed_frames(frames) == []
    frames = sd.render(sd.halves(tb * 3), start=2)               # offset pairing is not resynced
    assert sd.DvrUnitDecoder().feed_frames(frames) == []
    frames = sd.render(sd.superframes_to_halves(sd.place(tb, 6)))
    assert [e["text"] for e in sd.DvrUnitDecoder().feed_frames(frames)] == [orc.TEXT]


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


def test_model_limits_dprs_300_including_cr_and_nmea_82_before_the_lf():
    frames = lambda raw: sd.render(sd.halves(sd.simple_data_blocks(raw)))
    ok = lambda raw: [e["kind"] for e in sd.DvrUnitDecoder().feed_frames(frames(raw))]
    assert ok(orc._dprs_of_length(299)) == ok(orc._dprs_of_length(300)) == ["dprs"] and ok(orc._dprs_of_length(301)) == []
    for total in (80, 81, 82, 83):
        assert ok(orc._nmea_of_length(total).encode()) == ["nmea"], total
    for total in (84, 85):
        assert ok(orc._nmea_of_length(total).encode()) == [], total


# ------------------------------------------------------------------ the committed corpus

def test_corpus_schema_documents_the_unit_and_the_named_differences():
    doc = _doc()
    assert doc["schema"] == "dvt-dstar-sd-corpus/2"
    assert "MSB first over frames 0..20" in doc["unit66"] and "ASSUMPTION" in doc["unit66"]
    assert set(doc["named_differences"]) == {"DIFF-NIBBLE", "DIFF-GPS-LIMIT", "DIFF-PAIR-HOLE"}


def test_every_case_has_the_dvrouter_fields_and_the_unit_reproduces_the_frames():
    for rec in _doc()["cases"]:
        name = rec["case"]
        for key in ("unit66_hex", "expected_dvrouter", "lenient_alternative", "ours_dvrouter", "dvrouter_match"):
            assert key in rec, (name, key)
        assert rec["dvrouter_match"], name
        assert rec["expected_dvrouter"]["basis"] in ("oracle", "lead-statement-unverified")
        frames = _frames(rec)
        units = sd.split_units(frames)
        assert rec["unit66_hex"] == [sd.unit66(u).hex() for u in units], name
        assert all(len(h) == 2 * sd.UNIT66_BYTES for h in rec["unit66_hex"])
        back = {}
        for n, h in enumerate(rec["unit66_hex"]):
            for seq, wire in sd.parse_unit66(bytes.fromhex(h)).items():
                back[(n, seq)] = wire
        assert sorted(back) == sorted((n, s) for n, u in enumerate(units) for s in u), name
        assert rec["lenient_alternative"] == rec["dgw"], name


def test_expected_text_is_mmdvmhost_s_for_every_oracle_case():
    for rec in _doc()["cases"]:
        exp = rec["expected_dvrouter"]
        if exp["basis"] != "oracle":
            continue
        assert exp["text"] == [e for e in rec["mmdvm"] if e[0] == "text"], rec["case"]


def test_dp5f_decision_split_and_stale_cases_emit_no_text():
    by = _by_name()
    for case in ("text-slot8-split", "text-slot9-split", "text-missing-block-late", "text-stale-mix"):
        assert by[case]["expected_dvrouter"]["text"] == [], case
        assert [e for e in by[case]["lenient_alternative"] if e[0] == "text"], case     # DStarGateway decodes
        assert by[case]["lenient_differs"], case
    assert len(by["text-two-messages-split"]["expected_dvrouter"]["text"]) == 1
    assert len(by["text-two-messages-split"]["lenient_alternative"]) == 2


def test_every_difference_between_expectation_and_lenient_result_is_named_or_the_sync_decision():
    doc = _doc()
    sync_cases = {"text-slot8-split", "text-slot9-split", "text-missing-block-late", "text-two-messages-split",
                  "text-stale-mix"}
    for rec in doc["cases"]:
        if rec["lenient_differs"] and rec["case"] not in sync_cases:
            assert rec.get("named_difference") in doc["named_differences"], rec["case"]
        if "named_difference" in rec:
            assert rec["lenient_differs"], rec["case"]


def test_gps_boundary_vectors():
    by = _by_name()
    gps = lambda case: by[case]["expected_dvrouter"]["gps"]
    dgw = lambda case: [e for e in by[case]["dgw"] if e[0] != "text"]
    for case in ("dprs-len-299", "dprs-len-300", "nmea-len-80", "nmea-len-81", "nmea-len-82", "nmea-len-83"):
        assert len(gps(case)) == 1 and gps(case) == dgw(case), case
    for case in ("dprs-len-301", "nmea-len-84", "nmea-len-85"):
        assert gps(case) == [] and len(dgw(case)) == 1 and by[case]["named_difference"] == "DIFF-GPS-LIMIT", case
    assert "limit_reading" in by["nmea-len-83"]["expected_dvrouter"] and "limit_reading" in by["nmea-len-84"]["expected_dvrouter"]
    # line lengths including CR LF: 80, 81, 82, 83, 84, 85
    for total in (80, 81, 82, 83, 84, 85):
        assert len(orc._nmea_of_length(total)) == total and orc._nmea_of_length(total).endswith("\r\n")
    for total in (299, 300, 301):
        line = orc._dprs_of_length(total)
        assert len(line) == total and line.endswith(b"\r") and line.startswith(b"$$CRC")


def test_nibble_vectors_name_the_difference_and_both_results_are_recorded():
    by = _by_name()
    for case in ("nmea-nibble-0x36", "nmea-nibble-0x3f", "nmea-nibble-low", "nmea-nibble-short-block"):
        rec = by[case]
        assert rec["named_difference"] == "DIFF-NIBBLE" and rec["expected_dvrouter"]["basis"] == "lead-statement-unverified"
        assert [e for e in rec["dgw"] if e[0].startswith("nmea:")], case                 # DStarGateway accepts a sentence
    assert by["nmea-nibble-0x36"]["expected_dvrouter"]["gps"] == [] == by["nmea-nibble-0x3f"]["expected_dvrouter"]["gps"]
    assert by["nmea-nibble-low"]["expected_dvrouter"]["gps"] == []
    assert len(by["nmea-nibble-short-block"]["expected_dvrouter"]["gps"]) == 1
    # n = 0 means 5: DV-Router and DStarGateway agree
    zero = by["nmea-nibble-0x30"]
    assert zero["expected_dvrouter"]["gps"] == [e for e in zero["dgw"] if e[0].startswith("nmea:")] and "named_difference" not in zero
    # the short-block sentence DStarGateway reports is garbled (filler bytes inside) yet passes the XOR checksum
    garbled = bytes.fromhex(by["nmea-nibble-short-block"]["dgw"][0][2])
    assert b"f" in garbled.split(b":", 1)[1] and garbled != bytes.fromhex(by["nmea-nibble-short-block"]["expected_dvrouter"]["gps"][0][2])


def test_pair_hole_vectors_are_marked_unverified_and_have_holes_in_the_bitmap():
    by = _by_name()
    for case in ("pair-hole-text-once", "pair-hole-text-twice", "pair-hole-nmea-then-ok"):
        rec = by[case]
        assert rec["expected_dvrouter"]["basis"] == "lead-statement-unverified"
        assert any(sd.parse_unit66(bytes.fromhex(h)).keys() != set(range(21)) and len(sd.parse_unit66(bytes.fromhex(h))) == 20
                   for h in rec["unit66_hex"]), case
    assert by["pair-hole-text-once"]["expected_dvrouter"]["text"] == []
    assert [e[1] for e in by["pair-hole-text-twice"]["expected_dvrouter"]["text"]] == [12]
    assert by["pair-hole-text-twice"]["mmdvm"] and not [e for e in by["pair-hole-text-twice"]["mmdvm"] if e[0] == "text"]
    assert [e[0] for e in by["pair-hole-nmea-then-ok"]["expected_dvrouter"]["gps"]] == ["nmea:GPRMC"]


def test_corpus_is_current_for_the_new_cases():
    names = [n for n, _, _ in orc.corpus()]
    assert len(names) == len(set(names)) == len(_doc()["cases"]) == 58
    for rec, (name, _note, frames) in zip(_doc()["cases"], orc.corpus()):
        assert rec["case"] == name
        assert rec["unit66_hex"] == [sd.unit66(u).hex() for u in sd.split_units(frames)]
        exp = orc.expected_dvrouter(name, [dict(kind=e[0], seq=e[1], hex=e[2]) for e in rec["mmdvm"]],
                                    [dict(kind=e[0], seq=e[1], hex=e[2]) for e in rec["dgw"]],
                                    orc.ours_dvrouter_view(frames))
        assert rec["expected_dvrouter"] == json.loads(json.dumps(exp)), name
