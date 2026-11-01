# SPDX-License-Identifier: MIT
"""Extended AMBEd datagrams and the fake transform (dvtest.proto.ambed_ext).

The fixed vectors below are copied by hand from the DV-Router design text
(AMBEd datagrams, Writing a test double) and not produced by the module under
test. The golden file is the shared vector set of the DV-Router repository.
"""
import json
import pathlib

import pytest

from dvtest.proto import ambed_ext as ext

GOLDEN = pathlib.Path(__file__).parent / "data" / "fake_transform_vectors.json"

# ---- layouts written from the spec text ------------------------------------

SPEC_PING = bytes.fromhex("414d4245445049 4e47 584c58393939 2020".replace(" ", ""))
SPEC_PONG = bytes.fromhex("414d42454450 4f4e47".replace(" ", ""))
SPEC_CAPS_REPLY = bytes.fromhex("414d424544434150530104000000 3e".replace(" ", ""))
SPEC_OPEN = bytes.fromhex("414d4245444f53 584c5839393920 20 01 02 03 00 00 a1b2c3d4".replace(" ", ""))
SPEC_STD = bytes.fromhex("414d4245445354 44 1000 8427 01 02 a1b2c3d4".replace(" ", ""))
SPEC_BUSY = bytes.fromhex("414d424544425553 59 00007001".replace(" ", ""))
SPEC_CS = bytes.fromhex("414d4245444353 1001".replace(" ", ""))


def test_fixed_layouts_build():
    assert ext.ping("XLX999") == SPEC_PING and len(SPEC_PING) == 17
    assert ext.pong() == SPEC_PONG and len(SPEC_PONG) == 9
    assert ext.caps_request() == b"AMBEDCAPS" and len(b"AMBEDCAPS") == 9
    assert ext.caps_reply(4, 0x3E) == SPEC_CAPS_REPLY and len(SPEC_CAPS_REPLY) == 15
    assert ext.open_request("XLX999", 1, [2, 3], 0xA1B2C3D4) == SPEC_OPEN and len(SPEC_OPEN) == 24
    assert ext.std(16, 10116, 1, 2, 0xA1B2C3D4) == SPEC_STD and len(SPEC_STD) == 18
    assert ext.busy(0x7001) == SPEC_BUSY and len(SPEC_BUSY) == 13
    assert ext.cs(0x0110) == SPEC_CS and len(SPEC_CS) == 9


def test_fixed_layouts_parse():
    assert ext.parse_ping(SPEC_PING) == b"XLX999  "
    assert ext.is_pong(SPEC_PONG)
    assert ext.parse_caps_reply(SPEC_CAPS_REPLY) == (1, 4, 0x3E)
    assert ext.parse_open(SPEC_OPEN) == (b"XLX999  ", 1, (2, 3), 0xA1B2C3D4)
    assert ext.parse_std(SPEC_STD) == (16, 10116, 1, 2, 0xA1B2C3D4)
    assert ext.parse_busy(SPEC_BUSY) == 0x7001
    assert ext.parse_cs(SPEC_CS) == 0x0110
    names = [ext.kind_of(d) for d in (SPEC_PING, SPEC_PONG, b"AMBEDCAPS", SPEC_CAPS_REPLY,
                                      SPEC_OPEN, SPEC_STD, SPEC_BUSY, SPEC_CS)]
    assert names == ["ping", "pong", "caps_request", "caps_reply", "open", "std", "busy", "cs"]


def test_offsets_and_endianness():
    assert SPEC_STD[8:10] == b"\x10\x00" and SPEC_STD[10:12] == b"\x84\x27"   # LE u16
    assert SPEC_OPEN[20:24] == b"\xa1\xb2\xc3\xd4"                              # BE u32
    assert ext.caps_reply(2, 0x0000_0006)[11:15] == b"\x00\x00\x00\x06"
    assert ext.cs(0x1234)[7:9] == b"\x34\x12"
    assert ext.busy(1)[9:13] == b"\x00\x00\x00\x01"


@pytest.mark.parametrize("data", [
    SPEC_PING[:-1], SPEC_PING + b"x", b"AMBEDPONGx", SPEC_OPEN[:-1], SPEC_OPEN + b"\x00",
    SPEC_STD[:14], SPEC_STD + b"\x00", SPEC_BUSY[:9], SPEC_CS + b"\x00", b"", b"AMBEDOS",
    b"AMBEXXXXX" + b"\x00" * 15,
])
def test_exact_length_and_tag(data):
    assert ext.kind_of(data) == "unknown"
    assert ext.parse_ping(data) is None
    assert ext.parse_open(data) is None and ext.parse_std(data) is None
    assert ext.parse_busy(data) is None and ext.parse_cs(data) is None


def test_legacy_forms_are_not_extended_datagrams():
    assert ext.parse_std(SPEC_STD[:14]) is None        # legacy 14 B STD
    assert ext.parse_busy(b"AMBEDBUSY") is None        # legacy 9 B BUSY
    assert ext.parse_open(SPEC_OPEN[:19]) is None      # legacy 19 B OPEN


def test_callsign_padding():
    assert ext.callsign8("XLX999") == b"XLX999  "
    assert ext.callsign8("ABCDEFGHIJ") == b"ABCDEFGH"
    assert ext.callsign8(b"AB\x00\xffCD") == b"AB  CD  "
    assert len(ext.ping("")) == 17


def test_open_slots_and_bounds():
    assert ext.open_request("N0CALL", 5, [1, 2, 3, 4], 7)[16:20] == b"\x01\x02\x03\x04"
    assert ext.open_request("N0CALL", 5, [1], 7)[16:20] == b"\x01\x00\x00\x00"
    with pytest.raises(ValueError):
        ext.open_request("N0CALL", 5, [1, 2, 3, 4, 5], 7)


def test_caps_validation_rule():
    assert ext.caps_reply_valid(1, 4, 0x3E)
    assert ext.caps_reply_valid(1, 1, 0x02)               # one codec is accepted
    assert ext.caps_reply_valid(1, 2, 0xFFFFFF01 | 0x04)  # bits 0 and 6..31 ignored
    assert not ext.caps_reply_valid(2, 4, 0x3E)
    assert not ext.caps_reply_valid(1, 0, 0x3E)
    assert not ext.caps_reply_valid(1, 5, 0x3E)
    assert not ext.caps_reply_valid(1, 4, 0x41)           # no bit of 1..5
    assert ext.mask_of([1, 2, 3, 4, 5]) == 0x3E and ext.codecs_of(0x3E) == [1, 2, 3, 4, 5]
    assert ext.codecs_of(0x0A) == [1, 3]


def test_widths_and_request_sizes():
    assert ext.WIDTH == {1: 9, 2: 9, 3: 8, 4: 11, 5: 320}
    assert [2 + ext.WIDTH[c] for c in range(1, 6)] == [11, 11, 10, 13, 322]
    with pytest.raises(ValueError):
        ext.data_request(1, 0, bytes(8))
    with pytest.raises(ValueError):
        ext.data_request(6, 0, bytes(9))
    assert ext.data_request(1, 0x1FF, bytes(9))[:2] == b"\x01\xff"
    assert ext.parse_data_request(b"\x01\x05" + bytes(9)) == (1, 5, bytes(9))
    for bad in (b"", b"\x01", b"\x01\x05" + bytes(8), b"\x00\x05" + bytes(9), b"\x06\x05" + bytes(9)):
        assert ext.parse_data_request(bad) is None


# ---- transform ---------------------------------------------------------------

def _golden():
    return json.loads(GOLDEN.read_text())


def test_golden_file_shape():
    cases = _golden()
    assert len(cases) == 27
    pairs = {(c["src"], c["dst"]) for c in cases}
    assert pairs == {(s, d) for s in range(1, 6) for d in range(1, 6)}
    for c in cases:
        assert set(c) == {"src", "dst", "input_hex", "expected_hex"}
        assert c["input_hex"] == c["input_hex"].lower() and " " not in c["input_hex"]


@pytest.mark.parametrize("case", _golden(), ids=lambda c: "%d-%d-%dB" % (
    c["src"], c["dst"], len(c["input_hex"]) // 2))
def test_golden_vectors(case):
    frame = bytes.fromhex(case["input_hex"])
    want = bytes.fromhex(case["expected_hex"])
    assert len(want) == ext.WIDTH[case["dst"]]
    assert ext.expected_output(case["src"], case["dst"], frame) == want
    assert ext.transform(case["src"], case["dst"], frame) == want


# the K table as printed in the design text, rows src, columns dst
SPEC_TABLE = """
AMBE   15 02 2F 3C 4D
AMBE+2 03 2D 36 47 54
Codec2 27 34 3D 4E 57
IMBE   2E 37 44 55 5E
PCM    35 3E 4F 5C 65
"""


def test_key_table_matches_the_text_and_formula():
    rows = [line.split()[1:] for line in SPEC_TABLE.strip().splitlines()]
    for s in range(1, 6):
        for d in range(1, 6):
            printed = int(rows[s - 1][d - 1], 16)
            assert ext.key(s, d) == printed == ext.K_TABLE[s][d]
            if (s, d) not in ((1, 2), (2, 1)):
                assert printed == (((s * 7 + d * 13 + 1) & 0xFF) | 0x04)


def test_keys_nonzero_unique_pairs():
    keys = {(s, d): ext.key(s, d) for s in range(1, 6) for d in range(1, 6)}
    assert all(k != 0 for k in keys.values())
    for s in range(1, 6):
        for d in range(1, 6):
            if s != d:
                assert keys[(s, d)] ^ keys[(d, s)] != 0
    with pytest.raises(ValueError):
        ext.key(0, 1)
    with pytest.raises(ValueError):
        ext.key(1, 6)


@pytest.mark.parametrize("src", range(1, 6))
@pytest.mark.parametrize("dst", range(1, 6))
def test_all_directed_pairs(src, dst):
    frame = bytes((i * 37 + src) & 0xFF for i in range(ext.WIDTH[src]))
    out = ext.expected_output(src, dst, frame)
    assert len(out) == ext.WIDTH[dst]
    assert out == ext.expected_output(src, dst, frame)               # deterministic
    k = ext.key(src, dst)
    for i, b in enumerate(out):
        assert b == frame[i % len(frame)] ^ k ^ (i & 0xFF)


def test_cycling_uses_received_length():
    out = ext.transform(1, 3, b"\x10\x20\x30")
    assert out == bytes([0x10 ^ 0x2F ^ 0, 0x20 ^ 0x2F ^ 1, 0x30 ^ 0x2F ^ 2,
                         0x10 ^ 0x2F ^ 3, 0x20 ^ 0x2F ^ 4, 0x30 ^ 0x2F ^ 5,
                         0x10 ^ 0x2F ^ 6, 0x20 ^ 0x2F ^ 7])
    with pytest.raises(ValueError):
        ext.transform(1, 2, b"")


def test_index_xor_wraps_at_256_for_pcm():
    frame = bytes(range(9))
    out = ext.transform(1, 5, frame)
    k = 0x4D
    assert out[255] == frame[255 % 9] ^ k ^ 0xFF
    assert out[256] == frame[256 % 9] ^ k ^ 0x00
    assert out[319] == frame[319 % 9] ^ k ^ 0x3F


def test_round_trip_ambe_ambe2_exposes_one_hop_count():
    frame = bytes((i * 11 + 5) & 0xFF for i in range(9))
    there = ext.expected_output(1, 2, frame)
    back = ext.expected_output(2, 1, there)
    assert back == bytes(b ^ 0x01 for b in frame)        # K 0x02 then 0x03
    assert ext.key(1, 2) ^ ext.key(2, 1) == 0x01
    assert ext.expected_output(1, 2, back) != there      # a second round differs again


def test_other_round_trips_are_not_identity():
    frame = bytes(range(9))
    assert ext.expected_output(2, 4, frame) != frame
    assert ext.expected_output(4, 2, ext.expected_output(2, 4, frame)) != frame


# ---- replies -------------------------------------------------------------------

def test_expected_reply_layouts():
    frame = bytes(range(1, 10))
    reply = ext.expected_reply(1, [2, 3], 0x41, frame)
    assert len(reply) == 20 and reply[0] == 2 and reply[1] == 0x41 and reply[11] == 3
    assert reply[2:11] == ext.expected_output(1, 2, frame)
    assert reply[12:] == ext.expected_output(1, 3, frame)
    big = ext.expected_reply(1, [2, 3, 4, 5], 0, frame)
    assert len(big) == 353 == ext.reply_size([2, 3, 4, 5])
    assert ext.expected_reply(3, [5], 9, bytes(8)) == b"\x05\x09" + ext.expected_output(3, 5, bytes(8))


def test_parse_data_reply_round_trip_and_rejections():
    frame = bytes(range(9))
    reply = ext.expected_reply(1, [2, 3, 4], 7, frame)
    pid, entries = ext.parse_data_reply(reply, [2, 3, 4])
    assert pid == 7 and [c for c, _f in entries] == [2, 3, 4]
    assert entries[2][1] == ext.expected_output(1, 4, frame)
    with pytest.raises(ValueError):
        ext.parse_data_reply(b"\x03" + reply[1:], [2, 3, 4])                 # first codec byte
    with pytest.raises(ValueError):
        ext.parse_data_reply(reply[:11] + b"\x04" + reply[12:], [2, 3, 4])   # later codec byte
    with pytest.raises(ValueError):
        ext.parse_data_reply(reply + b"\x00", [2, 3, 4])                     # trailing byte
    with pytest.raises(ValueError):
        ext.parse_data_reply(reply[:15], [2, 3, 4], allow_cut=True)          # cut inside a frame
    with pytest.raises(ValueError):
        ext.parse_data_reply(reply[:11], [2, 3, 4])                          # cut, not allowed
    cut = ext.parse_data_reply(reply[:11], [2, 3, 4], allow_cut=True)
    assert len(cut[1]) == 1
    with pytest.raises(ValueError):
        ext.parse_data_reply(reply, [2, 3])                                  # more entries than outputs
