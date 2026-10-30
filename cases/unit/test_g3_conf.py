# SPDX-License-Identifier: MIT
"""Round trips for the Icom G3 builders in dvtest/proto/g3.py."""
import socket

import pytest

from dvtest.proto import dextra, g3

pytestmark = pytest.mark.unit


def test_presence_round_trip():
    packet = g3.presence_packet("N0TEST T")
    assert len(packet) == g3.PRESENCE_SIZE
    assert packet[8:16] == b"N0TEST  " and packet[16:24] == b"N0TEST  " and packet[24:32] == b"N0TEST T"
    reply = bytearray(packet)
    reply[2], reply[3] = 0x80, 0x00
    reply += socket.inet_aton("172.30.12.10")
    parsed = g3.parse_presence_reply(bytes(reply))
    assert parsed["flags"] == 0x80 and parsed["status"] == 0 and parsed["address"] == "172.30.12.10"
    assert parsed["body"] == packet[8:32]
    assert g3.parse_presence_reply(bytes(reply[:35])) is None


def test_route_round_trip():
    packet = g3.route_packet("XLX999 J")
    assert len(packet) == g3.ROUTE_SIZE and packet[2] & 0x10 and packet[8:] == b"XLX999 J"
    assert not g3.route_packet("XLX999 J", repeater=False)[2] & 0x10
    reply = (bytes([0, 0, 0x90, 0x01]) + bytes(4) + b"XLX999 J" + b"XLX999 G" + b"XLX999 J"
             + socket.inet_aton("0.0.0.0"))
    parsed = g3.parse_route_reply(reply)
    assert parsed["status"] == 1 and parsed["ur"] == b"XLX999 J" and parsed["rpt1"] == b"XLX999 G"
    assert parsed["rpt2"] == b"XLX999 J" and parsed["address"] == "0.0.0.0"


def test_stream_and_kinds():
    frames = [bytes([i]) * 9 for i in range(23)]
    packets = g3.stream(0x1234, frames, "N0TEST", rpt2=b"XLX999 J")
    assert len(packets) == 25
    head = dextra.parse_header_full(packets[0])
    assert head["rpt2"] == b"XLX999 J" and head["rpt1"] == b"N0TEST G" and head["my"] == b"N0TEST  "
    assert head["crc_ok"]
    assert [g3.kind_of(p) for p in packets[:2]] == ["header", "voice"] and g3.kind_of(packets[-1]) == "last"
    assert dextra.parse_voice_raw(packets[1])["slow"] == dextra.SYNC_DATA
    assert dextra.parse_voice_raw(packets[22])["counter"] == 0
    assert len(g3.stream(1, frames[:3], "N0TEST", rpt2=b"XLX999 J", end=False)) == 4
    assert g3.kind_of(g3.PING) == "ping" and g3.kind_of(b"x") == "other"
    assert g3.hpch_packet("XLX999 J") == b"HPCHXLX999 J"


def test_terminal_shut_and_open():
    term = g3.Terminal("N0TEST T", bind_ip="127.0.0.1", dv_port=0)
    try:
        port = term.sock.getsockname()[1]
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"PING", ("127.0.0.1", port))
        term.drain()
        assert [p for _t, p in term.received("127.0.0.1")] == [b"PING"]
        term.shut()
        assert term.sock is None and term.received() == [(term.inbox[0][0], b"PING")]
        sender.close()
    finally:
        term.close()
