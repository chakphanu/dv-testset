# SPDX-License-Identifier: MIT
"""Reflector-to-reflector builders and parsers of dvtest/proto/m17.py
(stage 2: configured peers, module trailer, M17P)."""
import pytest

from dvtest.proto import m17

pytestmark = pytest.mark.unit

GOLDEN_SMS = bytes.fromhex(
    "4d313750" "ffffffffffff" "00004b13d106" "0000" + "00" * 14 + "970b" "05" "6869" "00" "99fb")


def test_designations_on_the_wire():
    assert m17.encode_callsign("M17-DVT").hex() == "001399d83aed"
    assert m17.encode_callsign("M17-DVU").hex() == "00148dfc3aed"
    assert m17.encode_callsign("N0CALL").hex() == "00004b13d106"


def test_peer_conn_and_ackn_37_bytes():
    conn = m17.peer_conn_packet("M17-DVT", "M")
    assert conn == bytes.fromhex("434f4e4e" "001399d83aed" "4d" + "00" * 26)
    ackn = m17.peer_ackn_packet("M17-DVU", "MN")
    assert ackn == bytes.fromhex("41434b4e" "00148dfc3aed" "4d4e" + "00" * 25)
    assert len(conn) == len(ackn) == 37
    assert m17.parse_peer(ackn) == {"tag": "ACKN", "size": 37, "designation": "M17-DVU", "modules": "MN",
                                    "nul_ok": True}
    assert m17.parse_peer(conn)["modules"] == "M"
    assert m17.classify(conn) == "CONN37" and m17.classify(ackn) == "ACKN37"


def test_peer_list_truncated_to_27_without_nul():
    full = m17.peer_conn_packet("M17-DVT", "A" * 40)
    assert len(full) == 37 and m17.parse_peer(full)["nul_ok"] is False


@pytest.mark.parametrize("build,tag", [(m17.peer_ping_packet, "PING"), (m17.peer_pong_packet, "PONG"),
                                       (m17.peer_disc_packet, "DISC")])
def test_peer_control_10_bytes(build, tag):
    data = build("M17-DVT")
    assert len(data) == 10 and data[:4] == tag.encode()
    assert m17.parse_peer(data) == {"tag": tag, "size": 10, "designation": "M17-DVT", "modules": "",
                                    "nul_ok": True}
    assert m17.classify(data) == tag + "10"


def test_classify_sizes():
    assert m17.classify(b"ACKN") == "ACKN4" and m17.classify(b"NACK") == "NACK4"
    assert m17.classify(b"DISC") == "DISC4" and m17.classify(m17.conn_packet("N0CALL", "M")) == "CONN11"
    assert m17.classify(b"CONN" + bytes(30)) == "other"
    stream = m17.stream_packet(1, 0, bytes(16))
    assert m17.classify(stream) == "STREAM54"
    assert m17.classify(m17.with_trailer(stream, "M")) == "STREAM55"
    assert m17.classify(stream + b"xy") == "other"
    assert m17.classify(m17.m17p_packet(5, b"hi\0")) == "M17P"
    assert m17.classify(b"") == "other"


def test_kind_filters():
    assert m17.kind_matches("STREAM55", "STREAM") and not m17.kind_matches("STREAM55", "STREAM54")
    assert m17.kind_matches("PING10", ("CONN", "PING")) and m17.kind_matches("x", None)
    assert m17.kind_matches("PONG10", lambda kind: kind.endswith("10"))


def test_trailer_round_trip():
    stream = m17.stream_packet(7, 3, bytes(range(16)), src="N0CALL", dst="M17-DVT M")
    wire = m17.with_trailer(stream, "N")
    assert len(wire) == 55 and wire[:54] == stream and wire[54] == ord("N")
    body, letter = m17.strip_trailer(wire)
    assert body == stream and letter == "N"
    # the CRC covers bytes 0..51 only, the trailer does not change it
    assert m17.parse_stream(body)["crc_ok"]
    assert m17.with_trailer(b"abc", 0) == b"abc\x00" and m17.strip_trailer(b"abc\x00") == (b"abc", 0)
    assert m17.strip_trailer(b"") == (b"", None)


def test_m17p_golden_sms():
    data = m17.m17p_packet(5, b"hi\0")
    assert data == GOLDEN_SMS and len(data) == 40
    parsed = m17.parse_m17p(data)
    assert (parsed["size"], parsed["trailer"], parsed["dst"], parsed["src"], parsed["type"]) == \
        (40, None, "@ALL", "N0CALL", 0)
    assert parsed["lsf_crc_ok"] and parsed["payload_crc_ok"] and parsed["payload"] == b"\x05hi\0"


def test_m17p_size_formula():
    assert m17.m17p_size(1) == 38 == len(m17.m17p_packet(0x05, b"x"))
    assert m17.m17p_size(822) == 859 == len(m17.m17p_packet(0x05, bytes(822)))
    assert m17.m17p_size(3, specifier_len=2) == 34 + 5 + 2
    assert len(m17.m17p_packet(b"\xc2\x80", b"abc")) == m17.m17p_size(3, specifier_len=2)


def test_m17p_type_and_crc_overrides():
    for kind, values in m17.M17P_TYPES.items():
        for type_ in values:
            parsed = m17.parse_m17p(m17.m17p_packet(5, b"hi\0", type_=type_))
            assert parsed["type"] == type_ and parsed["lsf_crc_ok"], kind
    assert m17.m17p_packet(5, b"hi\0", type_=0) == GOLDEN_SMS
    assert m17.m17p_packet(5, b"hi\0", can=1)[16:18] == b"\x00\x80"
    bad_lsf = m17.parse_m17p(m17.m17p_packet(5, b"hi\0", crc=(0x1234, None)))
    assert not bad_lsf["lsf_crc_ok"] and bad_lsf["payload_crc_ok"]
    bad_payload = m17.parse_m17p(m17.m17p_packet(5, b"hi\0", crc=(None, 0xBEEF)))
    assert bad_payload["lsf_crc_ok"] and not bad_payload["payload_crc_ok"] and bad_payload["payload_crc"] == 0xBEEF
    zero = m17.parse_m17p(m17.m17p_packet(5, b"hi\0", crc=False))
    assert not zero["lsf_crc_ok"] and zero["payload_crc"] == 0
    assert m17.M17P_TYPES["legacy"][0] == m17.M17P_TYPE_LEGACY and m17.M17P_TYPES["other"] == (m17.M17P_TYPE_OTHER,)


def test_m17p_trailer_detected():
    data = m17.m17p_packet(5, b"hello\0", dst="M17-DVT M")
    wire = m17.with_trailer(data, "M")
    assert len(wire) == len(data) + 1
    with_letter = m17.parse_m17p(wire)
    assert with_letter["trailer"] == "M" and with_letter["payload_crc_ok"] and with_letter["size"] == len(wire)
    assert m17.parse_m17p(data)["trailer"] is None
    assert m17.parse_m17p(wire, trailer=False)["payload_crc_ok"] is False
    assert m17.parse_m17p(b"M17P" + bytes(10)) is None and m17.parse_m17p(b"M17 " + bytes(40)) is None
