# SPDX-License-Identifier: MIT
"""dvtest.proto.m17v3 and the committed vectors/m17v3 files (P26j, offline).

The expected values in the tables below are worked out by hand from the
specification arithmetic; the comment next to each says how."""
import json
import os

import pytest

from dvtest import ecdsa_p256 as ec
from dvtest import m17v3_vectors as vec
from dvtest.proto import m17, m17v3 as v3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VDIR = os.path.join(ROOT, "vectors", "m17v3")

KEY_A = vec.secret("a")
KEY_B = vec.secret("b")
PUB_A = ec.public_key(KEY_A)


# ---------------------------------------------------------------- TYPE

def test_type_table_matches_hand_values():
    for vid, payload, enc, signed, meta, can, value in vec.TYPE_ROWS:
        # TYPE = payload*0x1000 + enc*0x200 + signed*0x100 + meta*0x10 + can
        assert value == payload * 0x1000 + enc * 0x200 + signed * 0x100 + meta * 0x10 + can, vid
        assert v3.v3_type(payload, enc, bool(signed), meta, can) == value, vid
        f = v3.parse_v3_type(value)
        assert (f["payload"], f["encryption"], int(f["signed"]), f["meta"], f["can"]) == \
            (payload, enc, signed, meta, can), vid
        assert f["is_v3"] and v3.v3_type_problems(value) == [], vid


def test_type_bytes_follow_the_bit_table():
    # byte 0 = Payload(7-4) | Encryption(3-1) | Signed(0); byte 1 = Meta(7-4) | CAN(3-0)
    value = v3.v3_type(2, 4, True, 0xF, 0xA)
    assert value.to_bytes(2, "big") == bytes([(2 << 4) | (4 << 1) | 1, (0xF << 4) | 0xA])
    assert value == 0x29FA


def test_type_range_checks():
    for bad in ((16,), (2, 8), (2, 0, False, 16), (2, 0, False, 0, 16), (-1,)):
        with pytest.raises(ValueError):
            v3.v3_type(*bad)


def test_bad_type_rows_have_problems():
    for vid, value, _ in vec.TYPE_BAD_ROWS:
        assert v3.v3_type_problems(value), vid


def test_signed_bit_positions():
    assert v3.V3_SIGNED_BIT == 0x0100 and v3.LEGACY_SIGNED_BIT == 0x0800
    assert m17.stream_type(2, 1, 0, signed=True) == 0x0885      # existing v2 builder untouched
    assert m17.stream_type(2, 1, 0) == 0x0085


# ---------------------------------------------------------------- legacy mapping

def test_legacy_table_matches_hand_values():
    for vid, legacy, expect, _ in vec.LEGACY_ROWS:
        if expect is None:
            with pytest.raises(v3.Unmappable):
                v3.legacy_to_v3(legacy)
            continue
        meta = bytes([0x11]) + b"N0CALL" + bytes(7) if vid == "null-enc-gnss" else bytes(14)
        assert v3.legacy_to_v3(legacy, meta)[0] == expect, vid


def test_legacy_fields_of_known_values():
    assert v3.legacy_type(1, 2, 0, 0, 1, True) == 0x0885          # 1 + 4 + (1<<7) + 0x800
    assert v3.legacy_type(1, 2, 2, 2, 15) == 0x0785 | (2 << 3) | (2 << 5)
    f = v3.legacy_fields(0x08D5)
    assert (f["stream"], f["data_type"], f["enc_type"], f["enc_subtype"], f["can"], f["signed"]) == \
        (1, 2, 2, 2, 1, True)


def test_legacy_round_trips_through_convert_policy():
    for legacy in (0x0005, 0x0885, 0x0785, 0x0003, 0x0000, 0x0080, 0x000D, 0x002D, 0x004D, 0x0015, 0x0035, 0x0055,
                   0x08D5):
        meta = bytes(range(14)) if (legacy >> 3) & 3 == 2 else bytes(14)
        v3t, v3m = v3.legacy_to_v3(legacy, meta)
        back, back_meta = v3.v3_to_legacy(v3t, v3m, "convert")
        assert (back, back_meta) == (legacy, meta), hex(legacy)
    # GNSS keeps its subtype and bytes under "convert", ECD is dropped
    g = v3.gnss_meta(0, 1, 45.0, 90.0)
    t, m = v3.legacy_to_v3(0x0025, g)
    assert t == 0x2010 and m == g
    assert v3.v3_to_legacy(t, m, "convert") == (0x0025, g)
    t, m = v3.legacy_to_v3(0x0045, v3.ecd_meta("N0CALL"))
    assert t == 0x2020
    assert v3.v3_to_legacy(t, m, "convert") == (0x0005, bytes(14))


def test_v3_to_legacy_omit_drops_meta_but_keeps_iv():
    g = v3.gnss_meta(0, 1, 45.0, 90.0)
    assert v3.v3_to_legacy(0x2010, g, "omit") == (0x0005, bytes(14))
    assert v3.v3_to_legacy(0x2030, v3.v3_text_blocks("hi")[0], "omit") == (0x0005, bytes(14))
    iv = v3.aes_iv_meta(1, bytes(10))
    assert v3.v3_to_legacy(0x28F0, iv, "omit") == (0x0015, iv)
    assert v3.v3_to_legacy(0x2101, bytes(14)) == (0x0885, bytes(14))
    for bad in (0x3000, 0x2E00, 0x0005):
        with pytest.raises(v3.Unmappable):
            v3.v3_to_legacy(bad)


# ---------------------------------------------------------------- text

def test_v3_text_hand_values():
    # "The quick brown fox jumps over" is 30 bytes: blocks "The quick bro", "wn fox jumps ", "over"
    blocks = v3.v3_text_blocks("The quick brown fox jumps over")
    assert [b[0] for b in blocks] == [0x31, 0x32, 0x33]           # count 3, index 1..3
    assert blocks[0][1:] == b"The quick bro" and blocks[1][1:] == b"wn fox jumps "
    assert blocks[2][1:] == b"over" + bytes(9)                    # NUL padding
    assert v3.decode_v3_text(blocks) == b"The quick brown fox jumps over"
    base0 = v3.v3_text_blocks("The quick brown fox jumps over", index_base=0)
    assert [b[0] for b in base0] == [0x30, 0x31, 0x32]
    assert v3.decode_v3_text(base0, index_base=0) == b"The quick brown fox jumps over"
    with pytest.raises(ValueError):
        v3.decode_v3_text(base0)                                  # wrong base is detected


def test_v3_text_limits():
    one = v3.v3_text_blocks("0123456789ABC")                       # exactly 13 bytes
    assert len(one) == 1 and one[0] == b"\x11" + b"0123456789ABC"
    assert len(v3.v3_text_blocks("0123456789ABCD")) == 2
    full = v3.v3_text_blocks(b"x" * 195)                           # 15 blocks, first 0xF1, last 0xFF
    assert [b[0] for b in full] == [0xF0 | i for i in range(1, 16)]
    assert v3.decode_v3_text(full) == b"x" * 195
    with pytest.raises(ValueError):
        v3.v3_text_blocks(b"x" * 196)
    assert v3.v3_text_blocks("") == []
    assert v3.v3_text_blocks("N0CALL")[0][0] == 0x11               # packet mode control byte
    utf = "café €"
    assert v3.decode_v3_text(v3.v3_text_blocks(utf)).decode("utf-8") == utf


def test_v3_text_decoder_rejects_inconsistent_blocks():
    blocks = v3.v3_text_blocks("The quick brown fox jumps over")
    with pytest.raises(ValueError):
        v3.decode_v3_text(blocks[:2])
    with pytest.raises(ValueError):
        v3.decode_v3_text([blocks[1], blocks[0], blocks[2]])
    with pytest.raises(ValueError):
        v3.decode_v3_text([])


def test_v2_text_hand_values():
    # bit map of the length in the high nibble, one-hot position in the low nibble, space padding
    t30 = v3.v2_text_blocks("The quick brown fox jumps over")
    assert [b[0] for b in t30] == [0x71, 0x72, 0x74]
    assert t30[2][1:] == b"over" + b" " * 9
    assert [b[0] for b in v3.v2_text_blocks("x" * 52)] == [0xF1, 0xF2, 0xF4, 0xF8]
    assert [b[0] for b in v3.v2_text_blocks("x" * 26)] == [0x31, 0x32]
    assert v3.v2_text_blocks("N0CALL")[0][0] == 0x11
    assert v3.decode_v2_text(t30) == b"The quick brown fox jumps over"
    assert v3.decode_v2_text(reversed(t30)) == b"The quick brown fox jumps over"
    with pytest.raises(ValueError):
        v3.v2_text_blocks("x" * 53)
    with pytest.raises(ValueError):
        v3.decode_v2_text(t30[:2])


def test_text_conversion_between_schemes():
    v2 = v3.v2_text_blocks("The quick brown fox jumps over")
    v3b = v3.v3_text_blocks("The quick brown fox jumps over")
    assert [v3.v2_text_to_v3(b) for b in v2] == v3b
    assert [v3.v3_text_to_v2(b) for b in v3b] == v2
    with pytest.raises(ValueError):
        v3.v3_text_to_v2(v3.v3_text_blocks("x" * 60)[0])           # 5 blocks do not fit
    with pytest.raises(ValueError):
        v3.v2_text_to_v3(bytes(14))


# ---------------------------------------------------------------- GNSS

def test_gnss_hand_values():
    # source 0, station 1; validity 0xF; radius 5 m -> n=3; bearing 270 = 0x10E (bit 8 set, low 0x0E);
    # lat 45 -> trunc(0.5*8388607) = 0x3FFFFF; lon 90 -> 0x3FFFFF; altitude (100.5+500)*2 = 1201 = 0x04B1;
    # speed 87.5*2 = 175 = 0x0AF -> byte 11 = 0x0A, byte 12 = 0xF0
    meta = v3.gnss_meta(0, 1, 45.0, 90.0, 100.5, 270, 87.5, 5)
    assert meta.hex() == "01f70e3fffff3fffff04b10af000"
    # byte 1 = 0xF0 | 3<<1 | 1 = 0xF7
    dec = v3.decode_gnss(meta)
    assert (dec["validity"], dec["radius_n"], dec["bearing"], dec["alt_raw"], dec["speed_raw"]) == (15, 3, 270, 1201, 175)
    assert dec["altitude"] == 100.5 and dec["speed"] == 87.5 and dec["lat_raw"] == dec["lon_raw"] == 0x3FFFFF


def test_gnss_extremes_and_signs():
    # source/station 15 -> 0xFF; radius 1000 m -> n=7; bearing 359 = 0x167; lat +90 = 0x7FFFFF;
    # lon -180 = -8388607 = 0x800001 (two's complement, -2^23 is never used); altitude 65535; speed 4095
    meta = v3.gnss_meta(15, 15, 90.0, -180.0, v3.ALT_MAX_M, 359, 2047.5, 1000)
    assert meta.hex() == "ffff677fffff800001fffffff000"
    dec = v3.decode_gnss(meta)
    assert dec["lon_raw"] == -8388607 and dec["altitude"] == 32267.5 and dec["speed"] == 2047.5
    # lat 45, lon -22.5 -> -22.5/180 * 8388607 = -1048575.875 cut toward zero = -0xFFFFF = 0xF00001
    pos = v3.gnss_meta(0, 0, 45.0, -22.5)
    assert pos[:3].hex() == "008000" and pos[3:9].hex() == "3ffffff00001" and not any(pos[9:])


def test_gnss_invalid_fields_are_zero_and_limits():
    only_alt = v3.gnss_meta(15, 15, altitude=1234.5)
    assert only_alt[1] >> 4 == v3.V_ALT and not any(only_alt[2:9]) and only_alt[9:11] == (3469).to_bytes(2, "big")
    none = v3.gnss_meta(0, 1)
    assert none == bytes([0x01]) + bytes(13)
    assert v3.ALT_MAX_M == 32267.5 and v3.alt_to_raw(1e9) == 65535 and v3.alt_to_raw(-1e9) == 0
    assert [v3.radius_code(r) for r in (0.1, 1, 1.01, 2, 3, 128, 129, 1e6)] == [0, 0, 1, 1, 2, 7, 7, 7]
    with pytest.raises(ValueError):
        v3.gnss_meta_raw(bearing=512)
    with pytest.raises(ValueError):
        v3.decode_gnss(bytes(13) + b"\x01")                      # reserved byte not zero


# ---------------------------------------------------------------- ECD and AES IV

def test_ecd_layout():
    one = v3.ecd_meta("N0CALL")
    assert one == m17.encode_callsign("N0CALL") + bytes(8) and len(one) == 14
    two = v3.ecd_meta("N0CALL", "XLX999 A")
    assert two[:6] == m17.encode_callsign("N0CALL") and two[6:12] == m17.encode_callsign("XLX999 A")
    assert two[12:] == b"\0\0"
    assert v3.decode_ecd(one) == ("N0CALL", None) and v3.decode_ecd(two) == ("N0CALL", "XLX999 A")
    with pytest.raises(ValueError):
        v3.decode_ecd(one[:13] + b"\x01")


def test_aes_iv_and_counter_readings():
    # 187000000 = 0x0B2564C0
    iv = v3.aes_iv_meta(187000000, bytes(range(0xA0, 0xAA)))
    assert iv.hex() == "0b2564c0a0a1a2a3a4a5a6a7a8a9"
    assert v3.aes_counter(iv, 5).hex() == "0b2564c0a0a1a2a3a4a5a6a7a8a90005"
    assert v3.aes_counter(iv, 0x8005).hex() == "0b2564c0a0a1a2a3a4a5a6a7a8a90005"     # EOT bit is not part of the counter
    assert v3.aes_counter_draft_prose(iv, 5).hex() == "0005a0a1a2a3a4a5a6a7a8a90b2564c0"
    assert v3.EPOCH_2020 == 1577836800
    with pytest.raises(ValueError):
        v3.aes_iv_meta(1, bytes(9))


# ---------------------------------------------------------------- digest and stream

def test_digest_hand_values():
    p0 = bytes(range(16))
    p1 = b"\xff" * 16
    # after p0: xor with zeros = p0, rotate left one byte -> 01 02 .. 0f 00
    assert v3.digest_step(bytes(16), p0).hex() == "0102030405060708090a0b0c0d0e0f00"
    # after p1: xor ff = fe fd .. f1 f0 ff, rotate -> fd fc .. f0 ff fe
    assert v3.stream_digest([p0, p1]).hex() == "fdfcfbfaf9f8f7f6f5f4f3f2f1f0fffe"
    assert v3.stream_digest([]) == bytes(16)
    with pytest.raises(ValueError):
        v3.stream_digest([b"short"])


def test_frame_schedule_and_constants():
    assert v3.MAX_DATA_FRAMES == 0x7FFC == 0x8000 - 4
    payloads = v3.payload_stream("s", 6)
    s = v3.signed_stream(KEY_A, payloads)
    parsed = [m17.parse_stream(p) for p in s["packets"]]
    assert [p["fn"] for p in parsed] == [0, 1, 2, 3, 4, 5, 0x7FFC, 0x7FFD, 0x7FFE, 0x7FFF]
    assert [p["last"] for p in parsed] == [False] * 9 + [True]
    assert all(p["crc_ok"] and p["type"] == 0x2101 for p in parsed)
    assert b"".join(p["payload"] for p in parsed[6:]) == s["signature"]
    assert [p["payload"] for p in parsed[:6]] == payloads
    raw_fn = int.from_bytes(s["packets"][-1][34:36], "big")
    assert raw_fn == 0xFFFF


def test_valid_stream_verifies_with_both_type_layouts():
    s = v3.signed_stream(KEY_A, v3.payload_stream("s", 6))
    assert v3.verify_stream(s["packets"], PUB_A)["verdict"] == "valid"
    assert ec.verify_digest(PUB_A, s["digest"], s["signature"])
    legacy = v3.retype_packets(s["packets"], 0x0885)
    assert v3.verify_stream(legacy, PUB_A)["verdict"] == "valid"
    assert all(m17.parse_stream(p)["crc_ok"] and m17.parse_stream(p)["type"] == 0x0885 for p in legacy)
    assert v3.verify_stream(s["packets"], ec.public_key(KEY_B))["verdict"] == "invalid_signature"


def test_negative_builders_give_the_stated_verdicts():
    payloads = v3.payload_stream("s", 6)
    s = v3.signed_stream(KEY_A, payloads)
    f = s["frames"]

    def verdict(frames, type_=0x2101, key=PUB_A):
        return v3.verify_stream(v3.frames_to_packets(frames, 0x1234, "N0CALL", None, type_), key)["verdict"]

    assert verdict(v3.neg_lost_frame(f, 2)) == "invalid_signature"
    assert verdict(v3.neg_flip_payload_bit(f, 0, 127)) == "invalid_signature"
    assert verdict(v3.neg_swap_frames(f, 1, 2)) == "invalid_signature"
    assert verdict(v3.neg_truncated_tail(f, 3)) == "incomplete"
    assert verdict(v3.neg_truncated_tail(f, 0)) == "incomplete"
    assert verdict(v3.neg_signed_no_tail(f)) == "incomplete"
    for where in ("last_data", "first_sig", "third_sig", "none", "both"):
        assert verdict(v3.neg_eot(f, where)) == "malformed", where
    assert verdict(f, type_=0x2001) == "unsigned"
    for name, msg in v3.digest_variants(payloads).items():
        sig = v3.sign_digest(KEY_A, msg)
        assert verdict(v3.signed_frames(payloads, sig)) == "invalid_signature", name
    # the wrong-key signature is a perfectly formed one
    assert verdict(v3.signed_frames(payloads, v3.sign_digest(KEY_B, s["digest"]))) == "invalid_signature"
    # short packet and bad CRC
    pk = v3.frames_to_packets(f, 1)
    assert v3.verify_stream(pk[:-1] + [pk[-1][:53]], PUB_A)["verdict"] == "malformed"
    assert v3.verify_stream(pk[:-1] + [pk[-1][:52] + b"\0\0"], PUB_A)["verdict"] == "malformed"


def test_reorder_is_the_only_ambiguous_negative():
    """Restoring FN order would make the swapped stream valid, which is why the
    vector lists "valid" as also acceptable."""
    payloads = v3.payload_stream("s", 6)
    s = v3.signed_stream(KEY_A, payloads)
    swapped = v3.neg_swap_frames(s["frames"], 2, 3)
    ordered = sorted(swapped, key=lambda x: x[0])
    pk = v3.frames_to_packets(ordered, 1)
    assert v3.verify_stream(pk, PUB_A)["verdict"] == "valid"


def test_boundary_frame_counts():
    key = vec.secret("a")
    # derived: 0x8000 FN values minus the 4 signature frames
    for count, verdict in ((v3.MAX_DATA_FRAMES, "valid"), (v3.MAX_DATA_FRAMES + 1, "malformed")):
        payloads = v3.payload_stream(vec.SEED + "-boundary", count)
        sig = v3.sign_digest(key, v3.stream_digest(payloads))
        frames = v3.signed_frames(payloads, sig)
        packets = v3.frames_to_packets(frames, 0x4D03)
        assert v3.verify_stream(packets, PUB_A)["verdict"] == verdict, count
    assert [f[0] for f in v3.signed_frames(v3.payload_stream("b", v3.MAX_DATA_FRAMES), bytes(64))][-5:] == \
        [0x7FFB, 0x7FFC, 0x7FFD, 0x7FFE, 0x7FFF]


# ---------------------------------------------------------------- committed vector files

def _load(name):
    with open(os.path.join(VDIR, name), encoding="ascii") as handle:
        return json.load(handle)


def test_committed_files_equal_a_fresh_build():
    fresh = vec.build_all()
    assert sorted(fresh) == sorted(f for f in os.listdir(VDIR)
                                   if f.endswith(".json") and f != vec.OVERLAY_NAME)
    for name, text in fresh.items():
        with open(os.path.join(VDIR, name), encoding="ascii") as handle:
            assert handle.read() == text, name + " is stale: python3 scripts/gen-m17v3-vectors.py"
    assert vec.build_all() == fresh                                # deterministic


def test_manifest_hashes_and_entry_fields():
    import hashlib
    manifest = _load("manifest.json")
    for item in manifest["files"]:
        with open(os.path.join(VDIR, item["path"]), "rb") as handle:
            assert hashlib.sha256(handle.read()).hexdigest() == item["sha256"]
        doc = _load(item["path"])
        assert len(doc["entries"]) == item["entries"]
        ids = [e["id"] for e in doc["entries"]]
        assert len(ids) == len(set(ids)), item["path"]
        for e in doc["entries"]:
            assert e["spec"] and e["oracle"] and "oracle_verdict" in e, e["id"]
            if not vec.load_overlay():
                assert e["oracle_verdict"] is None, e["id"]
            assert e["oracle"] in manifest["oracles"] or "+" in e["oracle"], e["id"]
            assert e["spec"].startswith(("draft ", "v2.0.10 ", "derived", "published by RFC 6979", "Part II")), e["id"]
    assert all(manifest["sources_pinned_in_env_example"].values())


def test_key_vectors():
    for entry in _load("keys.json")["entries"]:
        sec = bytes.fromhex(entry["secret"])
        pub = ec.public_key(sec)
        assert entry["public_xy"] == pub.hex() and len(pub) == 64
        assert entry["public_sec1_uncompressed"] == ("04" + pub.hex()) and len(entry["public_sec1_uncompressed"]) == 130


def test_ecdsa_vectors():
    for entry in _load("ecdsa.json")["entries"]:
        ok = ec.verify_digest(bytes.fromhex(entry["public_xy"]), bytes.fromhex(entry["digest"]),
                              bytes.fromhex(entry["signature"]))
        assert ok == entry["verify"], entry["id"]


def test_signed_stream_vectors_give_their_stated_verdicts():
    keys = {"a": PUB_A, "b": ec.public_key(KEY_B)}
    doc = _load("signed_streams.json")
    seen = set()
    for entry in doc["entries"]:
        expect = entry["expect"]["verdict"]
        seen.add(entry["id"])
        if entry["packets"] is None:
            continue                                               # large recipes: test_boundary_frame_counts
        packets = [bytes.fromhex(p) for p in entry["packets"]]
        got = v3.verify_stream(packets, keys[entry["verify_key"]])["verdict"]
        assert got == expect, entry["id"]
        if expect == "valid":
            legacy = v3.retype_packets(packets, int(entry["legacy_type"], 16), bytes(14))
            assert v3.verify_stream(legacy, keys[entry["verify_key"]])["verdict"] == "valid", entry["id"]
    for needed in ("valid", "tampered-payload", "wrong-key", "missing-tail-frame", "reordered-frames", "truncated-mid-stream",
                   "digest-right-padded-32", "digest-truncated-15", "signed-bit-no-tail", "eot-last-data",
                   "boundary-max-frames", "boundary-one-too-many"):
        assert needed in seen, needed


def test_boundary_recipes_regenerate():
    for entry in _load("signed_streams.json")["entries"]:
        if entry["packets"] is not None:
            continue
        recipe = entry["recipe"]
        n = recipe["data_frames"]
        seed = vec.SEED + "-boundary"
        payloads = v3.payload_stream(seed, n)
        sid = int(entry["stream_id"], 16)

        def pkt(i):
            return m17.stream_packet(sid, i & 0x7FFF, payloads[i], src="N0CALL", dst=None, type_=0x2101,
                                     meta=bytes(14)).hex()

        assert recipe["head_packets"] == [pkt(0), pkt(1)]
        assert recipe["last_data_packet"] == pkt(n - 1)
        sig = v3.sign_digest(KEY_A, v3.stream_digest(payloads))
        assert entry["signature"] == sig.hex()
        assert b"".join(m17.parse_stream(bytes.fromhex(p))["payload"] for p in recipe["tail_packets"]) == sig


def test_type_meta_vectors_match_encoders():
    for entry in _load("type_meta.json")["entries"]:
        kind = entry["kind"]
        if kind == "v3_type":
            f = entry["fields"]
            assert v3.v3_type(f["payload"], f["encryption"], f["signed"], f["meta"], f["can"]) == int(entry["type"], 16)
        elif kind == "v3_type_invalid":
            assert v3.v3_type_problems(int(entry["type"], 16))
        elif kind == "map_legacy_to_v3":
            legacy = int(entry["legacy_type"], 16)
            if entry["unmappable"]:
                with pytest.raises(v3.Unmappable):
                    v3.legacy_to_v3(legacy)
            else:
                meta = v3.gnss_meta(0, 1, 45.0, 90.0) if entry["v3_type"] == "2010" else bytes(14)
                assert v3.legacy_to_v3(legacy, meta)[0] == int(entry["v3_type"], 16)
        elif kind == "map_v3_to_legacy":
            try:
                got = v3.v3_to_legacy(int(entry["v3_type"], 16), bytes.fromhex(entry["v3_meta"]), entry["policy"])
            except v3.Unmappable:
                assert entry["unmappable"]
            else:
                assert not entry["unmappable"]
                assert (got[0], got[1].hex()) == (int(entry["legacy_type"], 16), entry["legacy_meta"])
        elif kind == "meta_text_v3":
            blocks = [bytes.fromhex(b) for b in entry["blocks"]]
            text = bytes.fromhex(entry["text_utf8"])
            assert v3.decode_v3_text(blocks, entry["index_base"]) == text
        elif kind == "meta_text_v2":
            assert v3.decode_v2_text([bytes.fromhex(b) for b in entry["blocks"]]) == bytes.fromhex(entry["text_utf8"])
        elif kind == "meta_gnss":
            dec = v3.decode_gnss(bytes.fromhex(entry["meta"]))
            assert {k: dec[k] for k in entry["raw"]} == entry["raw"]
        elif kind == "meta_ecd":
            c1, c2 = entry["callsigns"]
            assert bytes.fromhex(entry["meta"]) == v3.ecd_meta(c1, c2)
            assert m17.parse_stream(bytes.fromhex(entry["packet"]))["crc_ok"]
        elif kind == "meta_aes_iv":
            iv = bytes.fromhex(entry["meta"])
            assert iv == v3.aes_iv_meta(entry["timestamp_2020"], bytes.fromhex(entry["random10"]))
            assert entry["counter_chosen"] == v3.aes_counter(iv, entry["fn"]).hex()
        else:
            raise AssertionError(kind)


def test_overlay_fills_oracle_verdict_and_rejects_unknown_ids():
    base = vec.build_all(overlay={})
    filled = vec.build_all(overlay={"valid": "Signature Valid"})
    assert base != filled
    entry = next(e for e in json.loads(filled["signed_streams.json"])["entries"] if e["id"] == "valid")
    assert entry["oracle_verdict"] == "Signature Valid"
    with pytest.raises(ValueError):
        vec.build_all(overlay={"no-such-id": "x"})
