# SPDX-License-Identifier: MIT
"""Round trips for the DCS conformance builders in dvtest/proto/dcs.py
(client forms of docs/protocols/dcs.md)."""
import pytest

from dvtest.proto import dcs, dextra

pytestmark = pytest.mark.unit


def test_gateway_and_droidstar_links():
    link = dcs.gateway_link("N0CALL", "B", "J")
    assert len(link) == dcs.LINK_SIZE
    assert link[:19] == b"N0CALL  BJ\x00DCS999  "
    assert link[19:19 + len(dcs.HTML)] == dcs.HTML and not any(link[19 + len(dcs.HTML):])
    ds = dcs.droidstar_link("N0TEST", "J")
    assert len(ds) == dcs.LINK_SIZE and ds[:11] == b"N0TEST  JJ\x0b" and not any(ds[11:])
    raw = dcs.raw_link(b"N0#EST  JJ", body=b"x", size=11)
    assert raw == b"N0#EST  JJ\x00"


def test_unlinks_and_keepalives():
    assert dcs.unlink19("N0CALL", "B") == b"N0CALL  B \x00DCS999  "
    assert len(dcs.unlink19("N0CALL", "B")) == 19
    assert dcs.unlink11("N0TEST", "J") == b"N0TEST  J \x00"
    assert dcs.keepalive17("N0CALL", "B", module="J") == b"N0CALL B\x00DCS999 J"
    ds = dcs.keepalive17("N0TEST", "J", form="droidstar")
    assert ds == b"N0TEST J\x00DCS999\x00J" and len(ds) == 17


def test_parse_reply_kinds():
    assert dcs.parse_reply(b"N0CALL BBJACK\x00")["kind"] == "ack"
    nak = dcs.parse_reply(b"N0CALL BB NAK\x00")
    assert nak["kind"] == "nak" and nak["module"] == b" " and nak["own_module"] == b"B"
    real = dcs.parse_reply(b"DCS999 J N0CALL BB\x0a\x00\x20\x20")
    assert real["kind"] == "poll" and real["reflector"] == b"DCS999 " and real["module"] == b"J"
    assert real["callsign"] == b"N0CALL " and real["own_module"] == b"B" and real["tail"] == b"\x0a\x00\x20\x20"
    assert dcs.parse_reply(b"DCS999  \x00")["kind"] == "ref9"
    assert dcs.parse_reply(bytes(19))["kind"] == "notice"
    assert dcs.parse_reply(b"EEEE" + bytes(20))["kind"] == "other"


def test_voice_packet_round_trip():
    frame = bytes(range(9))
    data = dcs.voice_packet(0x1234, 0x45, frame, b"\x01\x02\x03", callsign="N0TEST", module="J",
                            suffix=b"AMBE", flags=b"\x08\x00\x00", counter=0x010203)
    assert len(data) == dcs.PACKET_SIZE and data[:4] == b"0001"
    raw = dcs.parse_raw(data)
    assert raw["rpt2"] == b"DCS999 J" and raw["rpt1"] == b"N0TEST J" and raw["ur"] == b"CQCQCQ  "
    assert raw["my"] == b"N0TEST  " and raw["suffix"] == b"AMBE" and raw["flags"] == b"\x08\x00\x00"
    assert raw["sid"] == b"\x34\x12" and raw["seq"] == 5 and raw["last"] and raw["frame"] == frame
    assert raw["slow"] == b"\x01\x02\x03" and raw["counter"] == 0x010203
    assert raw["tail"] == b"\x01" + bytes(38)
    # the tester's compact parser reads the same packet
    assert dcs.parse(data)["stream_id"] == 0x1234 and dcs.parse(data)["counter"] == 0x010203
    long = dcs.voice_packet(1, 0, tail=b"\x01" + bytes(45))
    assert len(long) == 107 and dcs.parse_reply(long)["kind"] == "voice"
    big_endian = dcs.voice_packet(0x1234, 0, sid_bytes=b"\x12\x34")
    assert dcs.parse_raw(big_endian)["sid"] == b"\x12\x34"


def test_stream_packets():
    frames = [bytes([i]) * 9 for i in range(23)]
    out = dcs.stream_packets(0x0102, frames, module="J", end=dcs.END_JARL)
    assert len(out) == 24
    seqs = [dcs.parse_raw(p)["seq_byte"] for p in out]
    assert seqs[:22] == list(range(21)) + [0] and seqs[23] == (23 % 21) | 0x40
    assert dcs.parse_raw(out[0])["slow"] == dextra.SYNC_DATA
    assert dcs.parse_raw(out[21])["slow"] == dextra.SYNC_DATA
    assert dcs.parse_raw(out[1])["slow"] == dextra.SLOW_DATA
    assert (dcs.parse_raw(out[-1])["frame"], dcs.parse_raw(out[-1])["slow"]) == dcs.END_JARL
    late = dcs.stream_packets(0x0102, frames[:3], start=7, last=False)
    assert [dcs.parse_raw(p)["seq"] for p in late] == [7, 8, 9]
