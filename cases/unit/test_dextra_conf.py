# SPDX-License-Identifier: MIT
"""Round trips for the DExtra conformance builders in dvtest/proto/dextra.py
(link forms, reply parser, full DSVT header with P_FCS, raw voice, last
frame, slow data)."""
import pytest

from dvtest.proto import dextra


def test_link_forms():
    assert dextra.link_packet("N0TEST", "P") == b"N0TEST  PP\x00"
    assert dextra.link_packet("N0TEST", "P", tail=11) == b"N0TEST  PP\x0b"
    assert dextra.raw_link(b"n0test  ", "P", "5") == b"n0test  P5\x00"
    assert dextra.raw_link("N0TEST  ", "P", "P", 11) == dextra.link_packet("N0TEST", "P", tail=11)
    with pytest.raises(ValueError):
        dextra.raw_link("N0TEST", "P", "P")
    assert dextra.xrf_link_packet("XRF997", "B", "P") == b"XRF997  BP\x00"
    assert dextra.NAT_PUNCH == b"\x00"


def test_parse_link_reply():
    link = dextra.link_packet("N0TEST", "P")
    ack = dextra.parse_link_reply(link[:10] + b"ACK\x00")
    assert ack == {"kind": "ack", "callsign": b"N0TEST  ", "own": b"P", "module": b"P"}
    assert dextra.parse_link_reply(link[:10] + b"NAK\x00")["kind"] == "nak"
    assert dextra.parse_link_reply(b"XRF999  PB\x00") == {"kind": "rev2", "callsign": b"XRF999  ",
                                                         "own": b"P", "module": b"B"}
    assert dextra.parse_link_reply(b"DISCONNECTED") == {"kind": "disconnected"}
    assert dextra.parse_link_reply(b" " * 10 + b"\x00") == {"kind": "notice_blank"}
    assert dextra.parse_link_reply(b"XRF999  P \x00")["kind"] == "unlink"
    assert dextra.parse_link_reply(b"XRF999  \x00") == {"kind": "keepalive", "callsign": b"XRF999  "}
    assert dextra.parse_link_reply(b"\x00") is None
    assert dextra.parse_link_reply(dextra.header(1, "N0TEST", "P")) is None


def test_header_crc_x25():
    # CRC-16/X-25 check value over "123456789" is 0x906E (low byte first)
    crc = 0xFFFF
    body = b"123456789"
    for byte in body:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    assert crc ^ 0xFFFF == 0x906E
    radio = dextra.radio_header("N0CALL", module="P")
    assert len(radio) == 41 and dextra.crc_ok(radio)
    assert dextra.header_crc(radio[:39]) == radio[39:41]
    assert not dextra.crc_ok(radio[:39] + b"  ")
    assert not dextra.crc_ok(radio[:39] + b"\xff\xff")


def test_header_packet_round_trip():
    packet = dextra.header_packet(0xBEEF, my="N0CALL", suffix="AMBE", flags=b"\x40\x01\x02",
                                  module="P", ids=b"\x00\x02\x01", byte8=0x21, byte14=0x81)
    assert len(packet) == dextra.HEADER_SIZE
    full = dextra.parse_header_full(packet)
    assert full == {"stream_id": 0xBEEF, "byte8": 0x21, "ids": b"\x00\x02\x01", "byte14": 0x81,
                    "flags": b"\x40\x01\x02", "rpt2": b"XLX999 P", "rpt1": b"N0CALL P",
                    "ur": b"CQCQCQ  ", "my": b"N0CALL  ", "suffix": b"AMBE",
                    "crc": packet[54:56], "crc_ok": True}
    spaces = dextra.header_packet(1, my="N0CALL", module="P", crc=b"  ")
    assert spaces[54:56] == b"  " and not dextra.parse_header_full(spaces)["crc_ok"]
    # the legacy builder is the same layout with spaces as CRC
    legacy = dextra.header(0x1234, "N0TEST", "B")
    assert dextra.header_packet(0x1234, my="N0TEST", module="B", crc=b"  ") == legacy
    assert dextra.parse_header(legacy)["rpt2"] == dextra.parse_header_full(legacy)["rpt2"]


def test_voice_raw_and_last_frame():
    frame = bytes(range(9))
    packet = dextra.voice_packet(0x1234, 0x95, frame, b"\x01\x02\x03")
    assert len(packet) == dextra.VOICE_SIZE
    raw = dextra.parse_voice_raw(packet)
    assert raw["counter"] == 0x95 and raw["ambe"] == frame and raw["slow"] == b"\x01\x02\x03"
    assert dextra.voice_packet(0x1234, 3, frame) == dextra.voice(0x1234, 3, frame, False)
    last = dextra.last_frame(0x1234, 22)
    parsed = dextra.parse_voice_raw(last)
    assert parsed["last"] and parsed["counter"] == 0x41
    assert parsed["ambe"] + parsed["slow"] == dextra.END_PATTERN
    assert dextra.END_PATTERN[:6] == b"\x55\x55\x55\x55\xc8\x7a" and len(dextra.END_PATTERN) == 12
    assert dextra.parse_voice(last) == (0x1234, 1, True, dextra.END_PATTERN[:9])
    assert dextra.parse_voice_raw(dextra.header(1, "N0TEST", "B")) is None


def test_slow_data():
    assert dextra.scramble(b"\x66\x66\x66") == dextra.NULL_SLOW
    assert dextra.scramble(dextra.scramble(b"abc")) == b"abc"
    text = dextra.slow_text("N0CALL TEST")
    assert len(text) == 20 and all(len(g) == 3 for g in text)
    plain = b"".join(dextra.scramble(g) for g in text)
    assert plain[:6] == b"\x40N0CAL" and plain[6:12] == b"\x41L TES" and plain[18] == 0x43
    assert plain[24:] == b"\x66" * 36
    radio = dextra.radio_header("N0CALL", module="P")
    resend = b"".join(dextra.scramble(g) for g in dextra.slow_header_resend(radio))
    assert resend[0] == 0x55 and resend[1:6] == radio[:5]
    assert resend[48] == 0x51 and resend[49] == radio[40]
    rebuilt = b"".join(resend[i + 1:i + 1 + (resend[i] & 0x0F)] for i in range(0, 54, 6))
    assert rebuilt == radio


def test_client_keepalive_modes():
    for mode, expect in (("own", b"N0TEST  \x00"), ("echo", b"XRF999  \x00"), ("none", None)):
        client = dextra.DstarClient("127.0.0.1", callsign="N0TEST", module="P", keepalive=mode)
        try:
            responder = client.client.responder
            got = responder(b"XRF999  \x00", None) if responder else None
            assert got == expect, mode
        finally:
            client.close()
