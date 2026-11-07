# SPDX-License-Identifier: MIT
"""cases/dprs_support.py and the constants of cases/test_dp7_dprs.py: the compressed and Mic-E decoders against the examples PRINTED in the
APRS Protocol Reference 1.0.1 (chapters 9 and 10), the printable Mic-E encoder, the D-PRS line builders (through dvtest.dstar_sd), the GNSS
META scan and the distinct case callsigns. Offline."""
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dprs_support as dp  # noqa: E402
import position_support as ps  # noqa: E402
import test_dp15_positions as tp  # noqa: E402
import test_dp7_dprs as td  # noqa: E402

from dvtest import aprs_cases as ac, dstar_sd  # noqa: E402
from dvtest.proto import m17, m17v3  # noqa: E402

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ chapter 9: compressed position

def test_compressed_spec_example_is_the_one_of_dvtest():
    assert dp.SPEC_COMPRESSED == ac.POS_C == b"/5L!!<*e7>7P["


def test_compressed_base91_values_of_the_spec_example():
    # the spec: 190463 x (180 - 72.75) = 20427156 = 27, 9, 68, 22 in base 91 = `<*e7`
    assert [c - 33 for c in b"<*e7"] == [27, 9, 68, 22]
    assert 27 * 91 ** 3 + 9 * 91 ** 2 + 68 * 91 + 22 == 20427156
    assert [c - 33 for c in b"5L!!"] == [20, 43, 0, 0]


def test_compressed_decode_spec_example():
    got = dp.compressed_decode(dp.SPEC_COMPRESSED)
    assert got["lat"] == Fraction(99, 2)                                       # 49 30.00 N, exact
    assert got["lon"] == Fraction(-180) + Fraction(20427156, 190463)           # -72.75000394
    assert abs(float(got["lon"]) + 72.75) < 5e-6
    assert (got["table"], got["code"]) == ("/", ">")
    kind, course, speed = got["cs"]
    assert kind == "course_speed" and course == 88                              # 22 x 4
    assert speed == pytest.approx(36.2, abs=0.05)                               # 1.08 ** 47 - 1
    assert got["t"] == 58                                                       # the spec: 0 0 1 11 010 = 58, '[' = 58 + 33


def test_compressed_decode_other_cs_of_the_spec_examples():
    assert dp.compressed_decode(dp.SPEC_COMPRESSED_RANGE)["cs"][0] == "range"
    assert dp.compressed_decode(dp.SPEC_COMPRESSED_RANGE)["cs"][1] == pytest.approx(20.0, abs=0.2)     # "about 20 miles"
    kind, feet = dp.compressed_decode(dp.SPEC_COMPRESSED_ALTITUDE)["cs"]
    assert kind == "altitude" and int(feet) == 10004                                                    # "= 10004 feet"


def test_compressed_decode_blank_cs_and_errors():
    body = b"/5L!!<*e7> sT"
    assert dp.compressed_decode(body)["cs"] is None
    with pytest.raises(ValueError):
        dp.compressed_decode(b"/5L!!<*e7>")
    with pytest.raises(ValueError):
        dp.compressed_decode(b"/5L!\x7f<*e7>7P[")


def test_compressed_decode_roundtrips_dvtest_encoder():
    # dvtest.aprs_cases.compressed truncates; the decode is within one encoded unit
    body = ac.compressed(-30.2056, -140.0017, "/", "k", course=180, speed_kn=45)
    got = dp.compressed_decode(body)
    assert abs(float(got["lat"]) + 30.2056) < 1 / 380926 + 1e-9
    assert abs(float(got["lon"]) + 140.0017) < 1 / 190463 + 1e-9


def test_e7_rounds_half_away_from_zero():
    assert dp.e7(Fraction(99, 2)) == 495000000
    assert dp.e7(Fraction(-1, 2 * 10 ** 7)) == -1
    assert dp.e7(dp.SPEC_COMPRESSED_LAT) == 495000000
    assert abs(dp.e7(dp.compressed_decode(dp.SPEC_COMPRESSED)["lon"]) + 727500000) <= 50      # inside the position tolerance


# ------------------------------------------------------------------ chapter 10: Mic-E

def test_mice_spec_destination_table_example():
    # the spec's own worked table: S 3 2 U 6 T = lat digits 3 3 2 5 6 4, bits 1/0/0 (M3), North, offset +0, West
    d = dp.mice_destination_digits(dp.SPEC_MICE_DEST_SPEC)
    assert d["digits"] == [3, 3, 2, 5, 6, 4]
    assert d["msg_bits"] == [1, 0, 0] and d["custom"] == [False, False, False]
    assert d["north"] is True and d["lon_offset"] is False and d["west"] is True and d["spaces"] == 0


def test_mice_consistent_destination_has_the_offset_the_info_example_needs():
    d = dp.mice_destination_digits(dp.SPEC_MICE_DEST)
    assert d["digits"] == [3, 3, 2, 5, 6, 4] and d["lon_offset"] is True and d["west"] is True and d["north"] is True
    # the two spellings differ in one byte only (the spec's table and its information example disagree on the +100 offset)
    assert [i for i in range(6) if dp.SPEC_MICE_DEST[i] != dp.SPEC_MICE_DEST_SPEC[i]] == [4]


def test_mice_spec_information_example_decodes_to_the_spec_values():
    got = dp.mice_decode(dp.SPEC_MICE_DEST, dp.SPEC_MICE_INFO)
    assert got["lat"] == dp.SPEC_MICE_LAT == Fraction(33) + Fraction(2564, 6000)       # 33 25.64 N
    assert got["lon"] == dp.SPEC_MICE_LON == -(Fraction(112) + Fraction(774, 6000))     # 112 07.74 W
    assert got["speed"] == 20 and got["course"] == 251                                   # the spec's walk-through
    assert (got["table"], got["code"]) == ("/", "j")                                     # "the jeep symbol from the Primary Symbol Table"
    assert got["message_bits"] == [1, 0, 0] and got["ambiguity"] == 0
    assert dp.e7(got["lat"]) == 334273333 and dp.e7(got["lon"]) == -1121290000


def test_mice_spec_bytes_one_by_one():
    info = dp.SPEC_MICE_INFO
    assert len(info) == 9 and info[0] == 0x60
    assert info[1] - 28 == 12 and info[2] - 28 == 67 and info[3] - 28 == 74         # d, m (67 - 60 = 7), h
    assert info[4] - 28 == 82 and info[5] - 28 == 6 and info[6] - 28 == 51          # SP, DC, SE of the walk-through


def test_mice_spec_table_destination_alone_gives_12_degrees():
    # taking the spec's printed table destination literally (offset +0), the same information bytes are 12 07.74 W: the spec's inconsistency
    got = dp.mice_decode(dp.SPEC_MICE_DEST_SPEC, dp.SPEC_MICE_INFO)
    assert got["lon"] == -(Fraction(12) + Fraction(774, 6000))


def test_mice_spec_ambiguity_example():
    # chapter 10, "Mic-E Position Ambiguity": T4SQZZ, two ambiguous digits, longitude `(_f` is then 112 degrees 7 minutes
    d = dp.mice_destination_digits(dp.SPEC_MICE_AMBIGUITY_DEST)
    assert d["spaces"] == 2 and d["digits"][:4] == [4, 4, 3, 1] and d["lon_offset"] is True and d["west"] is True
    got = dp.mice_decode(dp.SPEC_MICE_AMBIGUITY_DEST, dp.SPEC_MICE_AMBIGUITY_INFO + b'  \x1c\x1c>/')
    assert got["ambiguity"] == 2 and int(abs(got["lon"])) == 112


def test_mice_spec_message_examples():
    # chapter 10: S32 = Standard 1/0/0 (M3); F2D = Custom 1/0/Custom 1 (C2); 234 = 0/0/0 (Emergency)
    assert dp.mice_destination_digits("S32U6T")["msg_bits"] == [1, 0, 0]
    d = dp.mice_destination_digits("F2D" + "U6T")
    assert d["msg_bits"] == [1, 0, 1] and d["custom"] == [True, False, True]
    assert dp.mice_destination_digits("234" + "U6T")["msg_bits"] == [0, 0, 0]


def test_mice_destination_letters_of_the_table():
    d = dp.mice_destination_digits("0LK" + "PZQ"[0:1] + "Z" + "Z")          # '0' digit, 'L' space flag 0, 'K' space custom
    assert d["spaces"] == 4 and d["custom"] == [False, False, True]
    with pytest.raises(ValueError):
        dp.mice_destination_digits("ABC")
    with pytest.raises(ValueError):
        dp.mice_destination_digits("000AAA")                                  # A-K are not used in bytes 4-6


def test_mice_longitude_degree_rules():
    def lon(deg_byte, offset):
        dest = "S32UVT" if offset else "S32U6T"
        return dp.mice_decode(dest, bytes([0x60, deg_byte, 28 + 5, 28 + 0, 28, 28, 28, ord(">"), ord("/")]))["lon"]
    assert lon(118, True) == -Fraction(5, 60)            # 0 degrees: 'v' = 90 + 100 = 190, minus 190
    assert lon(108, True) == -(100 + Fraction(5, 60))    # 100 degrees: 'l' = 80 + 100 = 180, minus 80
    assert lon(38, True) == -(110 + Fraction(5, 60))     # 110 degrees: '&' = 10 + 100
    assert lon(38, False) == -(10 + Fraction(5, 60))     # 10 degrees: '&' = 10 + 0


def test_mice_decode_rejects_short_and_foreign_data_types():
    with pytest.raises(ValueError):
        dp.mice_decode("S32UVT", b"`(_f")
    with pytest.raises(ValueError):
        dp.mice_decode("S32UVT", b"!" + dp.SPEC_MICE_INFO[1:])


def test_mice_printable_encoder_reproduces_the_spec_information_field():
    assert dp.mice_info_printable(-112.129, 20, 251, "/", "j") == dp.SPEC_MICE_INFO


def test_mice_pair_reproduces_the_consistent_spec_example():
    dest, info = dp.mice_pair(33.4273, -112.129, 20, 251, 3, "/", "j")
    assert dest == dp.SPEC_MICE_DEST and info == dp.SPEC_MICE_INFO


@pytest.mark.parametrize("lat,lon,speed,course", [
    (33.4273, -112.129, 20, 251), (-5.1687, 175.501, 0, 0), (51.2057, 3.0835, 10, 90), (10.0, 105.9998, 5, 359),
    (0.0017, -179.9998, 123, 359), (45.0, -73.5, 199, 100), (45.0, -73.5, 200, 99)])
def test_mice_printable_pairs_roundtrip(lat, lon, speed, course):
    dest, info = dp.mice_pair(lat, lon, speed, course, 2)
    assert all(0x20 <= b <= 0x7F for b in info[4:6])                      # SP+28 and DC+28 are printable in this encoding
    assert 0x20 <= info[6] <= 0x7F or course % 100 < 4                   # SE+28 is a control character for course units 0..3 in any encoding
    got = dp.mice_decode(dest, info)
    assert float(got["lat"]) == pytest.approx(lat, abs=1.0 / 6000)
    assert float(got["lon"]) == pytest.approx(lon, abs=1.0 / 6000)
    assert (got["speed"], got["course"]) == (speed, course)
    assert got["message_bits"] == [1, 0, 1]                              # message index 2 = M2 In Service = 1 0 1


def test_mice_decode_reads_the_non_printable_encoding_of_dvtest_too():
    for name, (lat, lon, speed, course, msg, kw), _note in ac.MICE_CASES:
        dest, info = ac._mice(lat, lon, speed, course, msg, **kw)
        got = dp.mice_decode(dest, info)
        assert (got["speed"], got["course"]) == (speed, course), name
        assert float(got["lat"]) == pytest.approx(lat, abs=1.0 / 6000), name


# ------------------------------------------------------------------ D-PRS lines

def test_vector_payloads_are_valid_simple_data():
    # the characters the slow data block layer refuses (0x76 'v' and friends) must not appear in the cases' lines
    compressed = dp.dprs_payload(td.COMPRESSED_CALL, dp.compressed_info(dp.SPEC_COMPRESSED))
    mice = dp.dprs_payload(td.MICE_CALL, dp.SPEC_MICE_INFO, dest=dp.SPEC_MICE_DEST)
    for payload in (compressed, mice):
        blocks = dstar_sd.simple_data_blocks(dstar_sd.dprs_line(payload))
        assert blocks
        fields = dstar_sd.text_gps_fields("DVT DPRS 0001", payload, superframes=3)
        assert len(fields) % 21 == 0


def test_dprs_payload_and_parse_roundtrip():
    payload = dp.dprs_payload("N0CALL-7", dp.SPEC_MICE_INFO, dest=dp.SPEC_MICE_DEST)
    assert payload == "N0CALL-7>S32UVT,DSTAR*:`(_fn\"Oj/"
    line = dstar_sd.dprs_line(payload)
    assert line.startswith(b"$$CRC") and line.endswith(b"\r")
    src, dest, path, info = dp.parse_dprs_line(line[10:])        # the bytes after "$$CRCxxxx,"
    assert (src, dest, path, info) == ("N0CALL-7", "S32UVT", "DSTAR*", dp.SPEC_MICE_INFO)
    assert dp.parse_dprs_line(b"no colon here") is None
    assert dp.parse_dprs_line(b"N0CALL:info") is None


def test_dprs_events_decode_a_whole_over():
    payload = dp.dprs_payload("N0TEST", ps.position_info(ps.APRS_LAT, ps.APRS_LON, 2, "/>", "x"))
    fields = dstar_sd.text_gps_fields("DVT DPRS 0001", payload, superframes=6)
    voice = [{"counter": i % 21, "slow": wire} for i, (_seq, wire) in enumerate(fields)]
    events = dp.dprs_events([{"voice": voice}, {"voice": []}], dstar_sd.Decoder)
    assert len(events) == 1 and events[0][0] == 0 and events[0][1]["crc_ok"]
    src, dest, path, info = dp.parse_dprs_line(events[0][1]["line"])
    assert src == "N0TEST" and dest == "API510" and td.check_line_level(info, 2) == ""
    assert td.check_line_level(info, 1) != "" and td.check_line_level(info, 3) != ""      # the level is checked, not only the shape


def test_check_line_level_counts_blanks_per_level():
    for level in range(5):
        info = ps.position_info(ps.APRS_LAT, ps.APRS_LON, level, "/>", "c").encode()
        assert td.check_line_level(info, level) == ""
        assert info.decode().count(" ") == 2 * level
        assert td.check_line_level(b"x" + info[1:], level) != ""
    refined = ps.position_info(ps.APRS_LAT, ps.APRS_LON, 0, "/>", "c").encode()
    assert td.check_line_level(refined, 2) != ""


# ------------------------------------------------------------------ M17 GNSS META

def packet(type_, meta, fn=0):
    return m17.parse_stream(m17.stream_packet(0x1234, fn, bytes(16), src="N0TEST", type_=type_, meta=meta))


def test_gnss_types():
    assert dp.GNSS_LEGACY_TYPE == 0x0025 and dp.GNSS_V3_TYPE == 0x2010
    assert dp.is_gnss_type(0x0025) and dp.is_gnss_type(0x2010) and dp.is_gnss_type(0x0025 | (3 << 7))
    assert not dp.is_gnss_type(0x0005)                  # legacy voice with null-encryption text
    assert not dp.is_gnss_type(0x0045)                  # legacy ECD
    assert not dp.is_gnss_type(0x0024)                  # not a stream
    assert not dp.is_gnss_type(0x2030)                  # v3 text META
    assert not dp.is_gnss_type(0x2000)                  # v3 without META


def test_gnss_scan_finds_a_typed_position_and_ignores_text_and_zero_meta():
    meta = m17v3.gnss_meta(lat=10.5, lon=20.25)
    packets = [packet(0x0005, bytes(14)), packet(0x0005, m17.meta_text_blocks("hello")[0]),
               packet(dp.GNSS_LEGACY_TYPE, meta), packet(dp.GNSS_V3_TYPE, meta), packet(dp.GNSS_LEGACY_TYPE, bytes(14))]
    scan = dp.gnss_scan(packets, near=(10.5, 20.25))
    assert scan["total"] == 5 and len(scan["typed"]) == 2 and scan["stray"] == []
    assert len(scan["positions"]) == 2
    assert scan["positions"][0] == pytest.approx((10.5, 20.25), abs=1e-4)


def test_gnss_scan_finds_a_position_sent_with_another_type():
    meta = m17v3.gnss_meta(lat=49.0, lon=-72.0)
    scan = dp.gnss_scan([packet(0x0005, meta)], near=(49.06, -72.03))
    assert scan["typed"] == [] and len(scan["stray"]) == 1
    assert dp.gnss_scan([packet(0x0005, meta)], near=(10.5, 20.25))["stray"] == []       # far away: not this position
    assert dp.gnss_scan([packet(0x0005, meta)])["stray"] == []                           # no `near`: no stray check


def test_gnss_scan_text_meta_is_not_a_position():
    texts = [packet(0x0005, m) for m in m17.meta_text_blocks("DVT DPRS 0001 and some more text")]
    scan = dp.gnss_scan(texts, near=(49.06, -72.03))
    assert scan["typed"] == [] and scan["stray"] == []


def test_stream_packets_skips_foreign_datagrams_and_keeps_times():
    good = m17.stream_packet(0x1, 0, bytes(16), src="N0TEST")
    inbox = [b"PONG", good, b"M17P" + bytes(40), good]
    got = dp.stream_packets(inbox, [1.0, 2.0, 3.0, 4.0], start=1)
    assert [p["at"] for p in got] == [2.0, 4.0]
    assert [p["fn"] for p in dp.stream_packets(inbox)] == [0, 0]


# ------------------------------------------------------------------ the cases' constants

def test_case_callsigns_are_distinct_from_every_other_position_case():
    mine = (list(td.A_CALL.values()) + [td.COMPRESSED_CALL, td.MICE_CALL] + list(td.GNSS_CONTROL_CALL.values())
            + list(td.GNSS_AMBIGUOUS_CALL.values()) + list(td.RIDE_CALL.values()) + list(td.UPLINK_CALL.values()))
    assert len(mine) == len(set(mine))
    others = []
    for table in (tp.A_CALL, tp.CONTROL_CALL, tp.OFF_CALL, tp.AMB_M17_CALL, tp.AMB_RF_CALL):
        others += list(table.values())
    others += [tp.AMB_DPRS_CALL, tp.UPLINK_CONTROL_CALL, tp.TRANSIT_CONTROL_CALL, tp.OBJECT_REPORTER, tp.HIDE_CALL, tp.OPT_CALL, tp.OPT_DMR]
    assert not set(mine) & set(others)
    assert td.M17_LISTENER not in mine and td.RIDE_LISTENER not in mine
    assert td.M17_LISTENER != td.RIDE_LISTENER
    for call in mine + [td.M17_LISTENER, td.RIDE_LISTENER]:
        assert len(call) <= 8 and call.isalnum() and call == call.upper()
    assert {ps.base_call(c) for c in mine} == set(mine)           # no SSID: the base is the call


def test_vectors_do_not_contain_a_forbidden_simple_data_character():
    for blob in (dp.SPEC_COMPRESSED, dp.SPEC_MICE_INFO, dp.SPEC_MICE_DEST.encode()):
        assert not set(blob) & dstar_sd.FORBIDDEN_SIMPLE
