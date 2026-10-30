# SPDX-License-Identifier: MIT
"""Offline round trips of the USRP builders used by cases/proto/test_usrp.py."""
import pytest

from dvtest.proto import usrp

pytestmark = pytest.mark.unit


def test_voice_and_unkey_layout():
    pcm = bytes(range(256)) + bytes(64)
    keyed = usrp.voice(7, pcm)
    assert len(keyed) == usrp.VOICE_SIZE and keyed[:4] == b"USRP"
    assert keyed[4:8] == (7).to_bytes(4, "big") and keyed[12:16] == (1).to_bytes(4, "big")
    assert keyed[16:32] == bytes(16) and keyed[32:] == pcm
    unkey = usrp.unkey(8)
    assert len(unkey) == usrp.HEADER_SIZE and usrp.parse(unkey).keyup == 0
    assert usrp.is_voice(keyed) and usrp.is_voice(unkey)


def test_type_is_little_endian_in_le_frames():
    frame = usrp.frame_le(3, usrp.TYPE_TEXT, b"hello")
    assert frame[20:24] == b"\x02\x00\x00\x00" and usrp.type_le(frame) == usrp.TYPE_TEXT
    assert frame[4:8] == (3).to_bytes(4, "big") and len(frame) == usrp.HEADER_SIZE + 5
    assert not usrp.is_voice(frame)


def test_padding_to_352():
    assert len(usrp.frame_le(0, usrp.TYPE_DTMF, b"*12", pad=True)) == usrp.VOICE_SIZE
    assert len(usrp.ping(0)) == usrp.HEADER_SIZE + 4
    padded = usrp.ping(0, pad=True)
    assert len(padded) == usrp.VOICE_SIZE and usrp.type_le(padded) == usrp.TYPE_PING


def test_tlv_round_trip():
    item = usrp.tlv(usrp.TLV_REMOTE_CMD, b"txTg=9")
    assert usrp.parse_tlv(item) == (usrp.TLV_REMOTE_CMD, b"txTg=9")
    assert usrp.parse_tlv(b"\x05\x09abc") is None
    with pytest.raises(ValueError):
        usrp.tlv(1, bytes(256))


def test_set_info_round_trip():
    frame = usrp.text_set_info(9, "N0CALL", src_id=1023007, rpt_id=1023008, tg=9, slot=2, cc=1)
    assert len(frame) == usrp.VOICE_SIZE and usrp.type_le(frame) == usrp.TYPE_TEXT
    info = usrp.parse_set_info(frame[usrp.HEADER_SIZE:])
    assert info == {"src_id": 1023007, "rpt_id": 1023008, "tg": 9, "slot": 2, "cc": 1, "callsign": "N0CALL"}
    assert frame[usrp.HEADER_SIZE + 1] == 12 + len("N0CALL") + 1
    assert usrp.parse_set_info(usrp.tlv(usrp.TLV_AMBE, bytes(20))) is None
