# SPDX-License-Identifier: MIT
"""dvtest.mmdvm_rf_nxdn: NXDN RF frames for the virtual MMDVM modem. The
constants are those of the pinned MMDVMHost (deb5efe, NXDN*.cpp/h); the
coding itself was checked byte for byte against the upstream classes in a
scratch harness (see the module docstring for what is real)."""
import pytest

from dvtest import mmdvm_rf_nxdn as rf
from dvtest.audio import signals
from dvtest.proto import nxdn

pytestmark = pytest.mark.unit

SRC, DST, RAN = 1234, 4567, 5


def talk(count=12):
    """49-bit frames derived from pseudo-random 72-bit DMR frames."""
    return [nxdn.from_dmr(f) for f in signals.prng_frames("dmr", count)]


def air(payload):
    """The descrambled 48-byte frame of a modem payload."""
    return rf.scramble(rf.from_payload(payload))


def test_constants_match_mmdvmhost():
    assert rf.FRAME_DATA == 0x40            # Modem.cpp:85 MMDVM_NXDN_DATA
    assert rf.FRAME_LOST == 0x41            # Modem.cpp:86 MMDVM_NXDN_LOST
    assert rf.FRAME_BITS == 384             # NXDNDefines.h:24
    assert rf.FRAME_BYTES == 48             # NXDNDefines.h:25
    assert rf.FSW_BYTES == bytes([0xCD, 0xF5, 0x90])    # NXDNDefines.h:32
    assert rf.FSW_MASK == bytes([0xFF, 0xFF, 0xF0])     # NXDNDefines.h:33
    assert rf.FSW_BITS == 20                # NXDNDefines.h:28
    assert rf.LICH_AT == 20                 # NXDNLICH.cpp:55,77 offset = FSW length
    assert rf.SACCH_AT == 36 and rf.SACCH_BITS == 60    # NXDNDefines.h:36-38
    assert rf.BLOCK_AT == (96, 240)         # NXDNControl.cpp:200 + FACCH1 length (NXDNDefines.h:39)
    assert rf.FACCH1_BITS == 144            # NXDNDefines.h:41
    assert rf.PAYLOAD_BYTES == 49           # Modem.cpp:1604-1625: tag byte dropped, 48 + 1
    assert rf.FRAME_MS == 80.0              # NXDNControl.cpp:281 frames / 12.5 = seconds


def test_scrambler_matches_upstream_table_head():
    # NXDNControl.cpp:31-32 SCRAMBLER[0..13]; the full 48-byte table was
    # compared in the scratch harness
    head = bytes([0x00, 0x00, 0x00, 0x82, 0xA0, 0x88, 0x8A, 0x00, 0xA2, 0xA8, 0x82, 0x8A, 0x82, 0x02])
    assert rf.SCRAMBLER[:14] == head
    assert len(rf.SCRAMBLER) == 48
    # only the first bit of each symbol after the FSW is ever flipped
    bits = rf.to_bits(rf.SCRAMBLER)
    assert not any(bits[:20]) and not any(bits[21::2])
    frame = bytes(range(48))
    assert rf.scramble(rf.scramble(frame)) == frame


def test_sequence_and_timing():
    frames = rf.build_keyup(SRC, DST, talk(12), ran=RAN)
    assert len(frames) == 1 + 3 + 1         # header, 12 voice frames / 4, trailer
    for ftype, payload, delay in frames:
        assert ftype == rf.FRAME_DATA
        assert len(payload) == 49 and payload[0] == 0
        assert delay == 80.0
    # FSW survives the scrambler (SCRAMBLER[0..2] = 0): bytes 1..3 of the payload
    for _, payload, _ in frames:
        assert payload[1:3] == b"\xCD\xF5" and payload[3] & 0xF0 == 0x90


def test_lich_bytes():
    frames = rf.build_keyup(SRC, DST, talk(8), ran=RAN)

    def lich(payload):
        bits = rf.to_bits(air(payload))
        return rf.to_bytes([bits[20 + 2 * i] for i in range(8)])[0], [bits[21 + 2 * i] for i in range(8)]

    header, _, trailer = frames[0][1], frames[1][1], frames[-1][1]
    # RDCH / SACCH NS / steal FACCH, inbound (NXDNControl.cpp:160-167: outbound is dropped)
    assert lich(header) == (0x81, [1] * 8)
    assert lich(trailer)[0] == 0x81
    # RDCH / SACCH SS / no steal: 0xAC, parity 0 (NXDNLICH.cpp:155-165)
    for _, payload, _ in frames[1:-1]:
        assert lich(payload)[0] == 0xAC
        assert nxdn.parse_lich(0xAC)["parity_ok"]


def test_header_carries_vcall_with_crc():
    info = rf.decode_frame(rf.from_payload(rf.build_keyup(SRC, DST, [], ran=RAN)[0][1]))
    assert info["fsw_ok"] and info["lich"]["parity_ok"]
    assert info["sacch"]["ran"] == RAN and info["sacch"]["structure"] == rf.SR_SINGLE
    assert info["sacch"]["data"] == nxdn.SACCH_IDLE and info["sacch"]["crc_ok"]
    assert info["facch1"][0] == info["facch1"][1]
    block = info["facch1"][0]
    assert block["crc_ok"] and block["msg"] == nxdn.VCALL
    assert (block["src"], block["dst"], block["group"]) == (SRC, DST, True)


def test_roundtrip():
    ambe = talk(16)
    frames = rf.build_keyup(SRC, DST, ambe, ran=RAN)
    out = rf.decode_downlink([(t, p) for t, p, _ in frames])
    assert out["voice"] == ambe
    assert (out["src"], out["dst"], out["group"], out["ran"]) == (SRC, DST, True, RAN)
    assert out["frame_count"] == 6 and out["has_header"] and out["terminated"]


def test_roundtrip_pads_to_four_frames():
    ambe = talk(6)
    out = rf.decode_downlink([(t, p) for t, p, _ in rf.build_keyup(SRC, DST, ambe)])
    assert out["voice"] == ambe + [bytes(7)] * 2


def test_individual_call():
    frames = rf.build_keyup(SRC, DST, talk(4), group=False)
    out = rf.decode_downlink([(t, p) for t, p, _ in frames])
    assert out["group"] is False and (out["src"], out["dst"]) == (SRC, DST)
    # host: group unless layer 3 octet 2 bit 7 is set (NXDNLayer3.cpp:87-90)
    info = rf.decode_frame(rf.from_payload(frames[0][1]))
    assert info["facch1"][0]["group"] is False


def test_voice_only_stream_gives_ids_from_sacch():
    # late entry: no header, the VCALL message comes from four SACCH parts
    # (NXDNControl.cpp:356-371)
    frames = rf.build_keyup(SRC, DST, talk(16), ran=RAN, header_frames=0, trailer_frames=0)
    out = rf.decode_downlink([(t, p) for t, p, _ in frames])
    assert not out["has_header"] and not out["terminated"]
    assert (out["src"], out["dst"], out["group"], out["ran"]) == (SRC, DST, True, RAN)
    structures = [rf.decode_frame(rf.from_payload(p))["sacch"]["structure"]
                  for _, p, _ in frames]
    assert structures == [3, 2, 1, 0]       # NXDN_SR_1_4 .. NXDN_SR_4_4


def test_rssi_trailer_and_lost():
    frames = rf.build_keyup(SRC, DST, talk(4), rssi=0x0123)
    assert all(len(p) == 51 and p[49:] == b"\x01\x23" for _, p, _ in frames)  # NXDNControl.cpp:129-132
    assert rf.decode_downlink([(t, p) for t, p, _ in frames])["voice"] == talk(4)
    assert rf.lost_frame() == (0x41, b"", 0.0)
    assert rf.decode_downlink([rf.lost_frame()[:2]])["frame_count"] == 0
    with pytest.raises(ValueError):
        rf.from_payload(b"\x00" * 48)


def test_determinism():
    ambe = talk(8)
    assert rf.build_keyup(SRC, DST, ambe) == rf.build_keyup(SRC, DST, ambe)
    assert rf.build_keyup(SRC, DST, ambe) != rf.build_keyup(SRC + 1, DST, ambe)
    assert rf.build_keyup(SRC, DST, ambe, ran=1) != rf.build_keyup(SRC, DST, ambe, ran=2)


def test_crc_checks_catch_corruption():
    block = nxdn.facch1(nxdn.VCALL, SRC, DST)
    assert nxdn.parse_facch1(block)["crc_ok"]
    bad = bytearray(block)
    bad[4] ^= 0x01
    assert not nxdn.parse_facch1(bytes(bad))["crc_ok"]
    sacch = nxdn.sacch(2, 0x1234, RAN)
    assert nxdn.parse_sacch(sacch)["crc_ok"]
    assert not nxdn.parse_sacch(bytes([sacch[0] ^ 1]) + sacch[1:])["crc_ok"]
    # a frame whose coded SACCH is garbage is reported, not decoded
    frame = bytearray(rf.scramble(rf.from_payload(rf.build_keyup(SRC, DST, [])[0][1])))
    for byte in range(5, 11):
        frame[byte] ^= 0xFF
    assert rf.decode_frame(rf.scramble(bytes(frame)))["sacch"] is None


def test_viterbi_corrects_a_channel_error():
    block = nxdn.facch1(nxdn.TX_REL, SRC, DST)
    bits = rf.facch1_encode(block)
    assert len(bits) == 144
    bits[50] ^= 1
    data, errors = rf.facch1_decode(bits)
    assert data == block and errors >= 1
    sacch = nxdn.sacch(3, 0x2AAAA, RAN)
    coded = rf.sacch_encode(sacch)
    assert len(coded) == 60
    coded[7] ^= 1
    assert rf.sacch_decode(coded)[0] == sacch


def test_coding_primitives_invert():
    bits = [1, 0, 1, 1, 0, 0, 1, 0] * 4
    assert len(rf.conv_encode(bits)) == 64
    assert rf.viterbi(rf.conv_encode(bits + [0] * 4)) == (bits + [0] * 4, 0)
    for groups, per in ((5, 12), (9, 16)):
        data = [i & 1 ^ (i >> 3) & 1 for i in range(groups * per)]
        assert rf.deinterleave(rf.interleave(data, groups, per), groups, per) == data
    assert rf.interleave(list(range(60)), 5, 12)[:3] == [0, 12, 24]
    assert rf.puncture(list(range(72)), rf.SACCH_PUNCTURE) == [i for i in range(72) if i % 6 != 5]
    assert len(rf.FACCH1_PUNCTURE) == 48 and len(rf.SACCH_PUNCTURE) == 12
