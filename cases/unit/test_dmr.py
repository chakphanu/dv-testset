# SPDX-License-Identifier: MIT
"""MMDVM Homebrew control packet builders and parsers (dvtest/proto/dmr.py)."""
import hashlib

import pytest

from dvtest.proto import dmr

ID = 1023007
ID_BYTES = ID.to_bytes(4, "big")


def test_login_packets():
    assert dmr.rptl(ID) == b"RPTL" + ID_BYTES
    assert dmr.rptk(ID) == b"RPTK" + ID_BYTES + bytes(32)
    digest = dmr.auth_hash(b"\x01\x02\x03\x04", "passw0rd")
    assert digest == hashlib.sha256(b"\x01\x02\x03\x04passw0rd").digest()
    assert len(dmr.rptk(ID, digest)) == 40
    with pytest.raises(ValueError):
        dmr.rptk(ID, bytes(31))


def test_rptc_layout():
    data = dmr.rptc(ID, callsign="N0CALL", rx_freq=438800000, tx_freq=431200000, power=5, colour=1,
                    lat=1.5, lon=-2.25, height=12, location="Nowhere", slots="2",
                    url="http://example.org/", software="dvtest", package="dvtest")
    assert len(data) == dmr.CONFIG_SIZE == 8 + sum(w for _, w in dmr.RPTC_FIELDS)
    fields, offset = {}, 8
    for name, width in dmr.RPTC_FIELDS:
        fields[name] = data[offset:offset + width].decode()
        offset += width
    assert data[:8] == b"RPTC" + ID_BYTES
    assert fields["callsign"] == "N0CALL  " and fields["rx_freq"] == "438800000"
    assert fields["tx_freq"] == "431200000" and fields["power"] == "05" and fields["colour"] == "01"
    assert fields["lat"] == "1.500000" and fields["lon"] == "-2.250000" and fields["height"] == "012"
    assert fields["location"].rstrip() == "Nowhere" and fields["slots"] == "2"
    assert fields["url"].rstrip() == "http://example.org/" and fields["package"].rstrip() == "dvtest"
    assert len(dmr.rptc(ID, description="x" * 50)) == dmr.CONFIG_SIZE


def test_options_ping_close():
    assert dmr.rpto(ID, "TS2=4001;") == b"RPTO" + ID_BYTES + b"TS2=4001;"
    assert dmr.rptping(ID) == b"RPTPING" + ID_BYTES and len(dmr.rptping(ID)) == 11
    assert dmr.rptcl(ID) == b"RPTCL" + ID_BYTES
    assert dmr.rptcl(ID, 13) == b"RPTCL" + ID_BYTES + bytes(4)
    with pytest.raises(ValueError):
        dmr.rptcl(ID, 10)


def test_parse_control():
    assert dmr.parse_control(b"RPTACK\x01\x02\x03\x04") == {"tag": "RPTACK", "tail": b"\x01\x02\x03\x04"}
    assert dmr.parse_control(b"MSTPONG" + ID_BYTES) == {"tag": "MSTPONG", "tail": ID_BYTES}
    assert dmr.parse_control(b"MSTNAK") == {"tag": "MSTNAK", "tail": b""}
    assert dmr.parse_control(b"MSTCL" + ID_BYTES)["tag"] == "MSTCL"
    assert dmr.parse_control(b"DMRD" + bytes(51)) is None


def test_bitfield_round_trip():
    assert dmr.bitfield(2, False, dmr.DATA, dmr.HEADER) == 0xA1
    assert dmr.bitfield(2, False, dmr.DATA, dmr.TERMINATOR) == 0xA2
    assert dmr.bitfield(2, False, dmr.VOICE_SYNC, 0) == 0x90
    assert dmr.bitfield(1, True, dmr.VOICE, 5) == 0x45
    for value in range(256):
        f = dmr.parse_bitfield(value)
        assert dmr.bitfield(f["slot"], f["private"], f["frame_type"], f["vseq"]) == value


def test_sync_payloads():
    default = dmr.data_sync_payload()
    assert default == dmr.data_sync_payload(dmr.BS_DATA)
    assert default[13] == 0x0D and default[14:19] == bytes(dmr.DATA_SYNC) and default[19] == 0xD0
    assert dmr.data_sync_payload(dmr.MS_DATA)[13:20] == bytes.fromhex("0D5D7F77FD7570")
    assert dmr.data_sync_payload(dmr.BS_VOICE)[13:20] == bytes.fromhex("0755FD7DF75F70")


def test_gateway_link_burst():
    burst = dmr.gateway_link(ID, 4001, 1023008, 0x01020304)
    assert len(burst) == 5 and all(len(p) == dmr.PACKET_SIZE for p in burst)
    fields = [dmr.parse(p) for p in burst]
    assert [f["seq"] for f in fields] == [0, 1, 2, 3, 4]
    assert [f["bitfield"] for f in fields] == [0xE1] * 3 + [0xE2] * 2
    assert {(f["src"], f["dst"], f["repeater"], f["stream"]) for f in fields} == {(ID, 4001, 1023008, 0x01020304)}
    assert all(f["payload"] == dmr.data_sync_payload() for f in fields)
    assert [dmr.parse(p)["bitfield"] for p in dmr.gateway_link(ID, 4000, ID, 1, 1, 1)] == [0xE1, 0xE2]
