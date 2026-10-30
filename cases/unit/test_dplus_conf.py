# SPDX-License-Identifier: MIT
"""Round trips for the DPlus conformance builders (dvtest/proto/dplus.py,
docs/protocols/dplus.md)."""
from dvtest.proto import dextra, dplus


def test_crc_ccitt_x25():
    # CRC-16/X-25 check value; a header followed by its CRC (low byte
    # first) leaves the X.25 residue
    assert dplus.crc_ccitt(b"123456789") == 0x906E
    radio = dplus.radio_header("REF999 P", "N0CALL P", "N0CALL")
    reg = 0xFFFF
    for byte in radio:
        reg ^= byte
        for _ in range(8):
            reg = (reg >> 1) ^ 0x8408 if reg & 1 else reg >> 1
    assert reg == 0xF0B8


def test_radio_header_and_packet():
    radio = dplus.radio_header("REF999 P", "N0CALL B", "N0TEST", suffix="AMBE", flags=b"\x40\x00\x00")
    assert len(radio) == 41 and radio[3:11] == b"REF999 P" and radio[35:39] == b"AMBE"
    data = dplus.header_packet(0xA1B2, radio)
    assert len(data) == dplus.HEADER_SIZE and data[:2] == b"\x3a\x80"
    info = dplus.parse(data)
    assert info["kind"] == "header" and info["crc_ok"] and info["stream_id"] == 0xA1B2
    assert (info["rpt2"], info["rpt1"], info["my"], info["flags"]) == (
        b"REF999 P", b"N0CALL B", b"N0TEST  ", b"\x40\x00\x00")
    bad = dplus.parse(dplus.header_packet(1, dplus.radio_header("REF999 P", "N0CALL P", "N0CALL", crc=0)))
    assert bad["crc"] == 0 and not bad["crc_ok"]
    # the DExtra builder fills the CRC with spaces: same fields, CRC not valid
    legacy = dplus.parse(dplus.header(7, "N0CALL", "P"))
    assert legacy["rpt2"] == b"XLX999 P" and not legacy["crc_ok"]
    assert dextra.parse_header(data[2:])["stream_id"] == 0xA1B2


def test_end_forms():
    irc = dplus.end_frame(0x1234, 25)
    assert len(irc) == dplus.LAST_SIZE and irc[16] == (25 % 21) | 0x40
    assert irc[17:26] == dplus.NULL_AMBE and irc[26:32] == dplus.JARL_END
    droid = dplus.end_frame(0x1234, 3, ambe=bytes(9))
    xlxd = dplus.last_voice(0x1234, 3)
    for data in (irc, droid, xlxd):
        info = dplus.parse(data)
        assert info["kind"] == "end" and info["stream_id"] == 0x1234 and info["byte0"] == 0x20
    assert dplus.parse(xlxd)["byte8"] == 0x81 and dplus.parse(droid)["byte8"] == 0
    assert dplus.parse(xlxd)["tail"][9:15] != dplus.JARL_END
    # the URFd form (byte 0 = 1D on a 32 B packet) is not a DPlus end frame here
    assert dplus.parse(dplus.end_frame(1, 0, byte0=0x1D))["kind"] == "unknown"


def test_login_forms():
    irc = dplus.login_irc("N0CALL")
    assert irc == dplus.login_packet("N0CALL") and len(irc) == 28
    qnet = dplus.login_qnet("N0CALL")
    assert qnet[4:12] == b"N0CALL\x00\x00" and qnet[20:] == b"DV019999"
    droid = dplus.login_droid("N0CALL", 42)
    assert len(droid) == 28 and droid[4:10] == b"N0CALL" and droid[10:20] == bytes(10)
    assert droid[20:] == b"HS000042"
    for data in (irc, qnet, droid):
        info = dplus.parse(data)
        assert info["kind"] == "login" and info["callsign"] == b"N0CALL"
    assert dplus.parse(droid)["client"] == b"HS000042"


def test_parse_control_and_voice():
    assert dplus.parse(dplus.CONNECT) == {"kind": "connect"}
    assert dplus.parse(dplus.DISCONNECT) == {"kind": "disconnect"}
    assert dplus.parse(dplus.KEEPALIVE) == {"kind": "keepalive"}
    assert dplus.parse(dplus.LOGIN_OK) == {"kind": "reply", "text": "OKRW"}
    assert dplus.parse(b"\x08\xc0\x04\x00OKRO") == {"kind": "reply", "text": "OKRO"}
    voice = dplus.parse(dplus.voice(5, 0, bytes(range(9)), dplus.SYNC))
    assert voice == {"kind": "voice", "stream_id": 5, "seq": 0, "frame": bytes(range(9)),
                     "slow": dplus.SYNC}
    assert dplus.parse(b"XXXX") == {"kind": "unknown", "size": 4}
