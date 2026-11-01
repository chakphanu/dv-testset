# SPDX-License-Identifier: MIT
"""dvtest/slowdata.py: the D-STAR text over layout of data.md 7.6."""
import pytest

from dvtest import slowdata
from dvtest.proto import dextra

pytestmark = pytest.mark.unit

# data.md 7.6 fixture: source N0CALL, body "CQ DE N0CALL" -> "N0CALL: CQ DE N0CALL"
FIXTURE_TEXT = "N0CALL: CQ DE N0CALL"
FIXTURE_HALVES = ["30 01 A3", "33 0E DF", "31 03 A9", "50 0C C2", "32 6F D7", "35 6F DD", "33 7F D0", "31 03 DF"]


def halves():
    return [bytes.fromhex(h) for h in FIXTURE_HALVES]


def test_encode_matches_the_documented_fixture():
    groups = slowdata.encode_text_over(FIXTURE_TEXT)
    assert len(groups) == 21
    assert groups[0] == bytes.fromhex("552D16")
    assert groups[1:9] == halves() and groups[9:17] == halves()
    assert groups[17:] == [bytes.fromhex("1629F5")] * 4


def test_decode_round_trip_and_padding():
    got = slowdata.decode_text_over(slowdata.encode_text_over(FIXTURE_TEXT))
    assert got == {"text": FIXTURE_TEXT, "repeat_ok": True, "filler_ok": True}
    short = slowdata.decode_text_over(slowdata.encode_text_over("N0CALL: hi"))
    assert short["text"] == "N0CALL: hi" + " " * 10
    assert slowdata.display_text("x" * 30) == "x" * 20


def test_first_eight_fields_agree_with_the_slow_text_builder():
    # dvtest.proto.dextra.slow_text puts the same four blocks at frames 1..8
    assert dextra.slow_text(FIXTURE_TEXT)[:8] == halves()


def test_decode_reports_a_broken_repeat_and_filler():
    groups = slowdata.encode_text_over(FIXTURE_TEXT)
    groups[10] = bytes(3)
    groups[18] = bytes(3)
    got = slowdata.decode_text_over(groups)
    assert got["text"] == FIXTURE_TEXT and got["repeat_ok"] is False and got["filler_ok"] is False


def test_decode_refuses_what_is_not_a_text_over():
    good = slowdata.encode_text_over(FIXTURE_TEXT)
    assert slowdata.decode_text_over(good[:20]) is None
    assert slowdata.decode_text_over([bytes(3)] * 21) is None
    bad_sync = [bytes(3)] + good[1:]
    assert slowdata.decode_text_over(bad_sync) is None
    bad_header = list(good)
    bad_header[3] = bytes(3)                      # block 1 header no longer 0x41
    assert slowdata.decode_text_over(bad_header) is None
    assert slowdata.decode_text_over([b"\x55\x2d"] + good[1:]) is None


def over_datagrams(sid, text, last=True, frames=21):
    packets = [dextra.header_packet(sid, my="XLX999", suffix="M17 ")]
    for i, slow in enumerate(slowdata.encode_text_over(text)[:frames]):
        packets.append(dextra.voice_packet(sid, i, dextra.NULL_AMBE, slow))
    if last:
        packets.append(dextra.last_frame(sid, frames))
    return packets


def test_overs_groups_by_stream_and_decodes():
    a = over_datagrams(0x1234, FIXTURE_TEXT)
    b = over_datagrams(0x4321, "N0TEST: second")
    mixed = [a[0], b[0]] + [p for pair in zip(a[1:], b[1:]) for p in pair] + [b"junk", b"DSVT\x20"]
    got = slowdata.overs(mixed)
    assert [o["stream_id"] for o in got] == [0x1234, 0x4321]
    assert got[0]["header"]["my"] == b"XLX999  " and got[0]["header"]["suffix"] == b"M17 "
    assert got[0]["frames"] == 22 and got[0]["last"] is not None
    assert slowdata.over_text(got[0])["text"] == FIXTURE_TEXT
    assert slowdata.over_text(got[1])["text"].rstrip() == "N0TEST: second"


def test_over_text_needs_a_whole_over():
    no_last = slowdata.overs(over_datagrams(7, FIXTURE_TEXT, last=False))[0]
    assert slowdata.over_text(no_last) is None
    short = slowdata.overs(over_datagrams(8, FIXTURE_TEXT, frames=15))[0]
    assert slowdata.over_text(short) is None
    assert slowdata.overs([]) == []
