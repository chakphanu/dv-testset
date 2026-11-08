# SPDX-License-Identifier: MIT
"""dvtest.mmdvm_rf_p25: P25 RF frames for a virtual MMDVM modem. Offline.

Constants are from the pinned MMDVMHost deb5efe (P25Defines.h, P25NID.cpp,
P25Control.cpp). The pinned NID bytes and the golden hash were produced by this
module and are equal, byte for byte, to what the upstream CP25NID / CP25Data /
CP25Audio encoders emit (cross-checked in a scratch harness, see the module
docstring); the tests here only prove they do not drift."""
import hashlib
import random

import pytest

from dvtest import mmdvm_rf_p25 as p
from dvtest.audio.signals import prng_frames

pytestmark = pytest.mark.unit

NAC = 0x293


def _bits(frame):
    return p.bytes_to_bits(frame)


def test_frame_lengths_and_sync():
    imbe = prng_frames("imbe", 20, seed=1)
    frames = p.build_keyup(1234567, 91, imbe, nac=NAC)
    types = [t for t, _, _ in frames]
    # HDU, LDU1, LDU2, LDU1 (20 IMBE = 3 LDUs, last padded), TDU
    assert types == [p.FRAME_HDR, p.FRAME_LDU, p.FRAME_LDU, p.FRAME_LDU, p.FRAME_LDU]
    assert p.FRAME_HDR == 0x30 and p.FRAME_LDU == 0x31
    # one flag byte (0x01 = sync) + raw frame; P25Defines.h:23,26,29
    assert [len(x) for _, x, _ in frames] == [1 + 99, 1 + 216, 1 + 216, 1 + 216, 1 + 18]
    for _, payload, _ in frames:
        assert payload[0] == 0x01
        assert payload[1:7] == bytes((0x55, 0x75, 0xF5, 0xFF, 0x77, 0xFF))   # P25Defines.h:44


def test_nid_bytes_pinned_and_parseable():
    # upstream CP25NID::encode output for NAC 0x293 (P25NID.cpp:35-90), parity bit per DUID
    assert p.nid_bytes(NAC, p.DUID_HDU).hex() == "29300f3b5df801c2"
    assert p.nid_bytes(NAC, p.DUID_LDU1).hex() == "293555ef2c653437"
    assert p.nid_bytes(NAC, p.DUID_LDU2).hex() == "293aba93bec26a2b"
    assert p.nid_bytes(NAC, p.DUID_TDU).hex() == "29333977728ced6e"
    for nac in (0, 1, 0x293, 0xFFF):
        for duid in (p.DUID_HDU, p.DUID_TDU, p.DUID_LDU1, p.DUID_LDU2, p.DUID_TDULC):
            assert p.nid_parse(p.nid_bytes(nac, duid)) == (nac, duid)
    damaged = bytearray(p.nid_bytes(NAC, p.DUID_LDU1))
    damaged[5] ^= 0x10
    assert p.nid_parse(bytes(damaged)) is None


def test_nid_in_frame_with_status_symbols():
    imbe = prng_frames("imbe", 9, seed=2)
    frame = p.build_ldu1(NAC, 1, 2, imbe)
    bits = _bits(frame)
    nid = p.bits_to_bytes(p.get_bits(bits, 48, 114))      # P25NID.cpp:143 range, SS skipped
    assert nid == p.nid_bytes(NAC, p.DUID_LDU1)
    # status symbols at bit 70,71 + 72k: 24 pairs over 1728 bits, "inbound busy" = 0,1
    for k in range(24):
        assert bits[70 + 72 * k] == 0 and bits[71 + 72 * k] == 1
    assert len(frame) == 216 and len(p.build_hdu(NAC, 91)) == 99 and len(p.build_tdu(NAC)) == 18


def test_golden_hash_and_determinism():
    args = (1234567, 91, prng_frames("imbe", 20, seed=1))
    a = p.build_keyup(*args)
    b = p.build_keyup(*args)
    assert a == b
    assert hashlib.sha256(b"".join(x for _, x, _ in a)).hexdigest() == \
        "eaf36f29f497478a5440ce654589cf6b1e19f1afe3a585c43bfb2b9fb2e278e5"
    # different input -> different bytes
    assert p.build_keyup(1234568, 91, args[2]) != a


def test_delays_are_air_time_of_next_frame():
    frames = p.build_keyup(5, 6, prng_frames("imbe", 18, seed=3))
    delays = [d for _, _, d in frames]
    # HDU, LDU1, LDU2, TDU: wait before LDU1 / LDU2 = 180 ms each, before TDU = 15 ms, end = 0
    assert delays == [180.0, 180.0, 15.0, 0.0]
    assert p.build_keyup(5, 6, prng_frames("imbe", 1, seed=3), with_hdu=False)[0][0] == p.FRAME_LDU


def test_roundtrip_keyup_decode():
    imbe = prng_frames("imbe", 30, seed=4)
    frames = p.build_keyup(7654321, 31100, imbe, nac=0x4A5)
    # host side view: the host writes a flag byte 0x00 where the modem sent 0x01
    res = p.decode_downlink([(t, b"\x00" + x[1:]) for t, x, _ in frames])
    assert res["voice"][:30] == imbe
    assert res["voice"][30:] == [p.NULL_IMBE] * 6          # padding of the last LDU
    assert (res["src"], res["dst"], res["group"], res["nac"]) == (7654321, 31100, True, 0x4A5)
    assert res["ldu_count"] == 4 and res["has_header"] and res["terminated"]
    assert res["algid"] == 0x80 and res["key_id"] == 0 and res["mi"] == bytes(9)
    assert res["duids"] == [p.DUID_HDU, p.DUID_LDU1, p.DUID_LDU2, p.DUID_LDU1, p.DUID_LDU2, p.DUID_TDU]
    # the modem flavour (flag 0x01) and a bare frame decode the same
    assert p.decode_downlink([(t, x) for t, x, _ in frames])["voice"] == res["voice"]
    assert p.decode_downlink([(t, x[1:]) for t, x, _ in frames])["src"] == 7654321


def test_roundtrip_private_call_and_crypto_fields():
    imbe = prng_frames("imbe", 18, seed=5)
    mi = bytes(range(1, 10))
    frames = p.build_keyup(0xFFFFFF, 0xABCDEF, imbe, nac=0xFFF, group=False, mfid=0x90,
                           service_options=0x80, algid=0x84, key_id=0x1234, mi=mi)
    res = p.decode_downlink([(t, x) for t, x, _ in frames])
    assert res["voice"] == imbe
    assert (res["src"], res["dst"], res["group"], res["nac"]) == (0xFFFFFF, 0xABCDEF, False, 0xFFF)
    assert res["mfid"] == 0x90 and res["service_options"] == 0x80
    assert (res["algid"], res["key_id"], res["mi"]) == (0x84, 0x1234, mi)


def test_no_hdu_and_missing_terminator_flags():
    imbe = prng_frames("imbe", 9, seed=6)
    frames = p.build_keyup(1, 2, imbe, with_hdu=False)
    res = p.decode_downlink(frames[:-1])
    assert res["has_header"] is False and res["terminated"] is False and res["ldu_count"] == 1
    assert res["voice"] == imbe
    empty = p.decode_downlink([])
    assert empty["src"] is None and empty["voice"] == [] and empty["ldu_count"] == 0


def test_argument_validation():
    imbe = prng_frames("imbe", 9, seed=7)
    for bad in (lambda: p.build_keyup(1, 2, []),
                lambda: p.build_keyup(1, 2, [b"short"]),
                lambda: p.build_keyup(1, 0x10000, imbe),                 # group id is 16 bits
                lambda: p.build_keyup(0x1000000, 2, imbe),
                lambda: p.build_keyup(1, 2, imbe, nac=0x1000),
                lambda: p.imbe_encode(b"\x00" * 10)):
        with pytest.raises(ValueError):
            bad()


def test_golay_hamming_reference_values():
    # first rows of the upstream Golay24128.cpp encoding tables (data 1, 2, 3)
    assert p.golay24128(1) == 0x18EB and p.golay24128(2) == 0x293E and p.golay24128(3) == 0x31D5
    assert p.golay23127(1) == 0xC75
    # minimum distance 7 for (23,12) and 8 for the extended code, on a seeded sample
    rng = random.Random(1)
    for _ in range(300):
        a, b = rng.randrange(1, 4096), rng.randrange(1, 4096)
        if a != b:
            assert bin(p.golay23127(a) ^ p.golay23127(b)).count("1") >= 7
            assert bin(p.golay24128(a) ^ p.golay24128(b)).count("1") >= 8
    assert p.hamming15113([0] * 11) == [0] * 15
    assert p.hamming15113([1] + [0] * 10)[11:] == [1, 1, 1, 1]     # d0 is in all 4 checks (Hamming.cpp:72-75)
    assert p.hamming1063([1] + [0] * 5)[6:] == [1, 1, 1, 0]        # Hamming.cpp:224-227
    # every single bit error in a Hamming(15,11) word is corrected
    word = p.hamming15113([1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0])
    for pos in range(15):
        bad = list(word)
        bad[pos] ^= 1
        assert p.hamming15113_decode(bad) == (word[:11], 1)


def test_imbe_fec_fixed_point_and_correction():
    samples = [bytes(11), b"\xff" * 11, p.NULL_IMBE] + prng_frames("imbe", 200, seed=9)
    for imbe in samples:
        cw = p.imbe_encode(imbe)
        assert len(cw) == 18
        assert p.imbe_decode(cw) == (imbe, 0)
        assert p.imbe_decode(cw, correct=False) == (imbe, 0)
        assert p.imbe_regenerate(cw) == (cw, 0)                  # FEC fixed point
        assert p.imbe_encode(p.imbe_decode(cw)[0]) == cw
    # three bit errors inside one Golay word (c1 = coded bits 23..45; air bits are
    # interleaved, so place them through the interleave table) are removed
    imbe = samples[5]
    bits = p.bytes_to_bits(p.imbe_encode(imbe))
    for coded_pos in (25, 30, 40):
        bits[p.IMBE_INTERLEAVE[coded_pos]] ^= 1
    got, errs = p.imbe_decode(p.bits_to_bytes(bits))
    assert got == imbe and errs == 3
    # one error in each of c4, c5, c6 (Hamming)
    bits = p.bytes_to_bits(p.imbe_encode(imbe))
    for coded_pos in (95, 110, 125):
        bits[p.IMBE_INTERLEAVE[coded_pos]] ^= 1
    assert p.imbe_decode(p.bits_to_bytes(bits)) == (imbe, 3)


def test_interleave_table_is_a_permutation():
    assert sorted(p.IMBE_INTERLEAVE) == list(range(144))
    # first row of upstream P25Audio.cpp:28 and the last element of the last row
    assert p.IMBE_INTERLEAVE[:6] == [0, 7, 12, 19, 24, 31] and p.IMBE_INTERLEAVE[-1] == 142


def test_rs_codewords_have_zero_syndrome():
    # RS(24,12,13): a valid codeword evaluates to zero at alpha^1..alpha^12
    msg = [(i * 7 + 3) % 64 for i in range(12)]
    cw = p.rs_encode(msg, 12)
    assert cw[:12] == msg and len(cw) == 24
    for root in range(1, 13):
        acc = 0
        for sym in cw:
            acc = p._gf_mul(acc, p._GF_EXP[root]) ^ sym
        assert acc == 0


def test_lsd_parity_matches_upstream_table_rows():
    # P25LowSpeedData.cpp:27 first row: parity of 0..15
    row = [0x00, 0x39, 0x72, 0x4B, 0xE4, 0xDD, 0x96, 0xAF, 0xF1, 0xC8, 0x83, 0xBA, 0x15, 0x2C, 0x67, 0x5E]
    assert [p.lsd_parity(i) for i in range(16)] == row
    assert p.lsd_parity(0xFF) == 0x63          # last entry of the upstream table (0x63)
