# SPDX-License-Identifier: MIT
"""Round trips of the M17 conformance builders (dvtest/proto/m17.py)."""
import pytest

from dvtest.proto import m17

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("message,crc", [(b"", 0xFFFF), (b"A", 0x206E), (b"123456789", 0x772B),
                                         (bytes(range(256)), 0x1C31)])
def test_crc16_spec_vectors(message, crc):
    """Part I 2.6, CRC test vectors."""
    assert m17.crc16(message) == crc


def test_stream_type_bits():
    assert m17.stream_type() == m17.TYPE_VOICE == 0x0005
    assert m17.stream_type(3) == m17.TYPE_VOICE_DATA == 0x0007
    assert m17.stream_type(1) == m17.TYPE_DATA == 0x0003
    assert m17.stream_type(can=1) == 0x0085
    assert m17.stream_type(can=15) == 0x0785
    assert m17.stream_type(signed=True) == 0x0805
    assert m17.stream_type(encryption=1) == 0x000D


def test_stream_packet_round_trip():
    meta = b"N0TEST meta 1"
    data = m17.stream_packet(0x1234, 7, bytes(range(16)), src="N0CALL", dst="M17-DVT M",
                             type_=m17.TYPE_VOICE_DATA, meta=meta, last=True)
    assert len(data) == m17.PACKET_SIZE
    p = m17.parse_stream(data)
    assert (p["stream_id"], p["dst"], p["src"], p["type"], p["fn"], p["last"]) == \
        (0x1234, "M17-DVT M", "N0CALL", 0x0007, 7, True)
    assert p["meta"] == meta + b"\0" and p["payload"] == bytes(range(16)) and p["crc_ok"]
    assert m17.parse(data)["eot"] and m17.parse(data)["seq"] == 7


def test_stream_packet_crc_forms():
    assert m17.parse_stream(m17.stream_packet(1, 0, bytes(16), crc=False))["crc"] == 0
    bad = m17.parse_stream(m17.stream_packet(1, 0, bytes(16), crc=0xDEAD))
    assert bad["crc"] == 0xDEAD and not bad["crc_ok"]
    assert m17.parse_stream(m17.stream_packet(1, 0, bytes(16)))["dst"] == "@ALL"
    assert m17.parse_stream(m17.stream_packet(1, 0, bytes(16))[:53]) is None


def test_legacy_packet_is_a_valid_stream_packet():
    """packet() (zero CRC, broadcast) parses with parse_stream."""
    p = m17.parse_stream(m17.packet(9, 3, "N0TEST", bytes(16)))
    assert p["dst"] == "@ALL" and p["src"] == "N0TEST" and p["type"] == m17.TYPE_VOICE and not p["crc_ok"]


def test_control_packets():
    assert m17.parse_control(m17.conn_packet("N0CALL", "M")) == {
        "tag": "CONN", "size": 11, "callsign": "N0CALL", "module": "M"}
    assert m17.parse_control(m17.lstn_packet("N0TEST", "N"))["tag"] == "LSTN"
    assert m17.parse_control(m17.ping_packet("XLX999"))["callsign"] == "XLX999"
    assert m17.parse_control(m17.pong_packet("N0CALL"))["size"] == 10
    assert m17.parse_control(m17.DISC_ACK) == {"tag": "DISC", "size": 4, "callsign": None, "module": None}
    peer = m17.peer_conn_packet("M17-DVT", "MN")
    assert len(peer) == 37 and peer[10:13] == b"MN\0" and m17.parse_control(peer)["callsign"] == "M17-DVT"
    assert m17.parse_control(b"XXXX") is None


def test_callsign_suffix_round_trip():
    """A 9-character CONN callsign (suffix at position 9) survives base 40."""
    assert m17.decode_callsign(m17.encode_callsign("N0CALL  H")) == "N0CALL  H"
    assert m17.address("@ALL") == m17.BROADCAST


def test_m17p_packet():
    data = m17.m17p_packet(0x05, b"hello\0", src="N0CALL", can=3)
    assert data[:4] == b"M17P" and len(data) == 4 + 30 + 1 + 6 + 2
    lsd, crc = data[4:32], int.from_bytes(data[32:34], "big")
    assert crc == m17.crc16(lsd) and lsd[:6] == m17.BROADCAST
    assert int.from_bytes(lsd[12:14], "big") == 0x0180          # CAN 3, packet mode
    body = data[34:-2]
    assert body == b"\x05hello\0" and int.from_bytes(data[-2:], "big") == m17.crc16(body)
