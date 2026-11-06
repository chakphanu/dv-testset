# SPDX-License-Identifier: MIT
"""Builds the M17 v3 test vector files of vectors/m17v3/ (docs/m17v3-cases.md).

build_all() returns {file name: text} and is deterministic: the same call gives
the same bytes (ECDSA is RFC 6979, every payload comes from a seeded hash, no
clock, no random source). scripts/gen-m17v3-vectors.py writes the files; a unit
test regenerates them in memory and compares with the committed copies.

Every entry has
  id, kind, spec (a citation of the file and line that was read, or
  "derived: <how>"), oracle (the independent implementation that can check it),
  oracle_verdict (null until that oracle has been run).
The TYPE tables below are written as literals on purpose: each value is worked
out by hand in the comment next to it, and cases/unit/test_m17v3.py asserts
that the encoders in dvtest.proto.m17v3 agree with them.
"""
import hashlib
import json
import os
import re

from . import ecdsa_p256 as ec
from .proto import m17, m17v3 as v3

SCHEMA = "dvt-m17v3-vectors/1"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---- citations (files and lines read at the pinned revisions, docs/m17v3-cases.md)
D = "draft "           # M17_spec.tex, branch dev = 3.0.0-draft, pin M17_SPEC_REF
V2 = "v2.0.10 "         # M17_spec.tex, branch main, pin M17_SPEC_V2_REF
SPEC_TYPE = D + "l.1114-1141 layout, l.1160-1250 subfields"
DERIVE_TYPE = "byte0 = payload<<4|enc<<1|signed, byte1 = meta<<4|can"
SPEC_SIG = D + "l.2051-2079"
SPEC_FN = D + "l.716 (FN counts to 0x7FFF, MSB is EOT)"
SPEC_GNSS = D + "l.2081-2170; radius " + V2 + "l.1397; altitude max " + V2 + "l.1410"
SPEC_ECD = D + "l.2172-2201"
SPEC_TEXT = D + "l.2203-2217"
SPEC_AES = D + "l.2015-2048; " + V2 + "l.1537-1549"
SPEC_V2_TYPE = V2 + "l.1157-1320"
SPEC_V2_TEXT = V2 + "l.1306-1320"

ORACLE_SIG = "m17-fme"
ORACLE_GNSS = "libm17+mrefd"
ORACLE_NONE = "spec-only"

SRC = "N0CALL"
KEY_LABELS = {"a": b"dvtest m17v3 signing key A", "b": b"dvtest m17v3 signing key B"}
SEED = "dvtest-m17v3-a"
N_DATA = 6                    # data frames of the small streams (one superframe)
TYPE_VOICE_SIGNED_CAN1 = 0x2101
LEGACY_VOICE_SIGNED_CAN1 = 0x0885


def hx(data):
    return bytes(data).hex()


def secret(label):
    return hashlib.sha256(KEY_LABELS[label]).digest()


def load_pins(path=None):
    """KEY=value lines of .env.example for the M17 sources."""
    path = path or os.path.join(ROOT, ".env.example")
    wanted = ("M17_SPEC_REPO", "M17_SPEC_REF", "M17_SPEC_V2_REF", "M17_FME_REPO", "M17_FME_REF",
              "MICRO_ECC_REPO", "MICRO_ECC_REF", "M17_IMPL_REPO", "M17_IMPL_REF", "LIBM17_REPO", "LIBM17_REF")
    found = {}
    with open(path, encoding="ascii") as handle:
        for line in handle:
            match = re.match(r"^([A-Z0-9_]+)=(\S+)\s*$", line)
            if match and match.group(1) in wanted:
                found[match.group(1)] = match.group(2)
    return {k: found.get(k) for k in wanted}


# ---------------------------------------------------------------- keys and ecdsa

def keys_file():
    keys = []
    for label in ("a", "b"):
        sec = secret(label)
        pub = ec.public_key(sec)
        keys.append({
            "id": "key-" + label,
            "kind": "key",
            "secret": hx(sec),
            "public_xy": hx(pub),
            "public_sec1_uncompressed": hx(ec.sec1_uncompressed(pub)),
            "derivation": "secret = SHA-256(%r); public = secret * G on P-256" % KEY_LABELS[label].decode("ascii"),
            "spec": D + "l.2073-2076 (256-bit private key, 512-bit public key); layout derived: x||y as micro-ecc",
            "oracle": ORACLE_SIG,
            "oracle_verdict": None,
        })
    return {"schema": SCHEMA, "kind": "keys", "entries": keys}


def ecdsa_file():
    entries = []
    # published by RFC 6979 appendix A.2.5 (P-256, SHA-256)
    rfc_x = bytes.fromhex("C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721")
    for msg, r, s in (
            (b"sample", "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716",
             "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8"),
            (b"test", "F1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367",
             "019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083")):
        entries.append({
            "id": "rfc6979-a25-sha256-" + msg.decode("ascii"), "kind": "ecdsa",
            "secret": hx(rfc_x), "public_xy": hx(ec.public_key(rfc_x)),
            "digest": hashlib.sha256(msg).hexdigest(), "signature": (r + s).lower(), "verify": True,
            "spec": "published by RFC 6979, appendix A.2.5 (ECDSA P-256, SHA-256, message %r)" % msg.decode("ascii"),
            "oracle": "ecdsa-verifier", "oracle_verdict": None})
    sec = secret("a")
    pub = ec.public_key(sec)
    for i, digest in enumerate((bytes(range(16)), hashlib.sha256(b"m17v3 digest sample").digest()[:16])):
        sig = ec.sign_digest(sec, digest)
        entries.append({
            "id": "digest16-%d-valid" % i, "kind": "ecdsa", "secret": hx(sec), "public_xy": hx(pub),
            "digest": hx(digest), "signature": hx(sig), "verify": True,
            "spec": "derived: signature over the 16-byte digest as one big-endian integer (left-padded), "
                    "r||s; micro-ecc bits2int() read",
            "oracle": ORACLE_SIG, "oracle_verdict": None})
        bad = bytearray(sig)
        bad[40] ^= 0x01
        entries.append({
            "id": "digest16-%d-flipped-signature-bit" % i, "kind": "ecdsa", "secret": hx(sec), "public_xy": hx(pub),
            "digest": hx(digest), "signature": hx(bad), "verify": False,
            "spec": "derived: one bit of s flipped", "oracle": ORACLE_SIG, "oracle_verdict": None})
        entries.append({
            "id": "digest16-%d-right-padded" % i, "kind": "ecdsa", "secret": hx(sec), "public_xy": hx(pub),
            "digest": hx(digest + bytes(16)), "signature": hx(sig), "verify": False,
            "spec": "derived: the same signature checked against the digest right-padded to 32 bytes is another number",
            "oracle": ORACLE_SIG, "oracle_verdict": None})
    return {"schema": SCHEMA, "kind": "ecdsa", "entries": entries}


# ---------------------------------------------------------------- signed streams

# expect.collector: what a strict collector (dvtest.proto.m17v3.collect_stream) reports for a stream.
# id -> (collector verdict, other acceptable collector verdicts). None = not a signed stream, the
# collector is not used. The EOT bit is compared as sent (assumption, see docs/m17v3-cases.md).
_SEQ = ("sequence_error", [])
COLLECTOR = {
    "valid": ("complete_valid", []),
    "valid-meta-changes": ("complete_valid", []),
    "tampered-payload": ("complete_invalid_signature", []),
    "wrong-key": ("complete_invalid_signature", []),
    "lost-frame": _SEQ,
    "reordered-frames": _SEQ,
    "duplicate-data-frame": _SEQ,
    "early-tail-chunk": _SEQ,
    "missing-tail-frame": ("incomplete", []),
    "missing-tail-two": ("incomplete", []),
    "missing-tail-all": ("incomplete", []),
    "signed-bit-no-tail": ("sequence_error", ["incomplete"]),   # EOT on the last data frame
    "truncated-mid-stream": ("incomplete", []),
    "short-packet": ("size_error", []),
    "short-data-packet": ("size_error", []),
    "long-packet": ("size_error", []),
    "eot-last-data": _SEQ, "eot-first-sig": _SEQ, "eot-third-sig": _SEQ, "eot-none": _SEQ, "eot-both": _SEQ,
    "tail-duplicate-fn": _SEQ,
    "tail-fn-shifted": _SEQ,
    "unsigned-with-tail": (None, []),
    "unsigned-plain": (None, []),
    "digest-no-rotate": ("complete_invalid_signature", []),
    "digest-right-padded-32": ("complete_invalid_signature", []),
    "digest-sha256-of-digest": ("complete_invalid_signature", []),
    "digest-sha256-of-payloads": ("complete_invalid_signature", []),
    "digest-truncated-15": ("complete_invalid_signature", []),
    "bad-key-off-curve": ("bad_public_key", []),
    "bad-key-63-bytes": ("bad_public_key", []),
    "bad-key-zero": ("bad_public_key", []),
    "bad-signature-r-zero": ("bad_signature_encoding", []),
    "bad-signature-s-equals-n": ("bad_signature_encoding", []),
    "bad-signature-s-max": ("bad_signature_encoding", []),
    "bad-signature-r-equals-n": ("bad_signature_encoding", []),
    "boundary-max-frames": ("complete_valid", []),
    "boundary-one-too-many": _SEQ,
}


def _entry(vid, title, packets, key, verify_key, digest, signature, verdict, reason, spec, also=None,
           stream_id=0x4D01, type_=TYPE_VOICE_SIGNED_CAN1, extra=None, pub=None):
    collector, collector_also = COLLECTOR[vid]
    signing_bit = bool(type_ & v3.V3_SIGNED_BIT)
    if pub is None:
        pub = ec.public_key(secret(verify_key))
    entry = {
        "id": vid, "kind": "signed_stream", "title": title,
        "stream_id": "%04x" % stream_id, "type": "%04x" % type_, "signing_key": key, "verify_key": verify_key,
        "signing_bit": signing_bit, "tail_expected": signing_bit,
        "verify_public_xy": hx(pub),
        "digest": hx(digest) if digest is not None else None,
        "signature": hx(signature) if signature is not None else None,
        "packets": [hx(p) for p in packets] if packets is not None else None,
        "expect": {"verdict": verdict, "reason": reason, "also_acceptable": also or [],
                   "collector": collector, "collector_also_acceptable": collector_also},
        "legacy_type": "%04x" % LEGACY_VOICE_SIGNED_CAN1, "legacy_meta": None,
        "spec": spec, "oracle": ORACLE_SIG, "oracle_verdict": None,
    }
    if extra:
        entry.update(extra)
    return entry


def signed_streams_file():
    sec_a, sec_b = secret("a"), secret("b")
    payloads = v3.payload_stream(SEED, N_DATA)
    base = v3.signed_stream(sec_a, payloads, stream_id=0x4D01)
    digest, sig = base["digest"], base["signature"]
    frames = base["frames"]
    entries = []

    def packets_of(frm, sid, type_=TYPE_VOICE_SIGNED_CAN1, meta=bytes(14), lsd=None):
        return v3.frames_to_packets(frm, sid, SRC, None, type_, meta, lsd)

    def add(vid, title, frm, verdict, reason, spec, key_sign="a", verify="a", dig=digest, sg=sig, also=None,
            sid=0x4D01, type_=TYPE_VOICE_SIGNED_CAN1, packets=None, extra=None, pub=None):
        pk = packets if packets is not None else packets_of(frm, sid, type_)
        entries.append(_entry(vid, title, pk, key_sign, verify, dig, sg, verdict, reason, spec, also, sid, type_,
                              extra, pub))

    add("valid", "valid signed stream: 6 data frames, FN 0..5, tail FN 7FFC 7FFD 7FFE FFFF", frames, "valid",
        "digest of the data payloads is signed with key a", SPEC_SIG)

    # meta changes along the stream: text in the first superframe, GNSS in the next (8 data frames)
    pl8 = v3.payload_stream(SEED + "-meta", 8)
    s8 = v3.signed_stream(sec_a, pl8, stream_id=0x4D02)
    text_block = v3.v3_text_blocks("N0CALL TEST")[0]
    gnss = v3.gnss_meta(0, 1, 45.0, 90.0, 100.5, 270, 87.5, 5)

    def lsd8(i):
        if i < 6:
            return v3.v3_type(2, 0, True, v3.META_TEXT, 1), text_block
        return v3.v3_type(2, 0, True, v3.META_GNSS, 1), gnss

    add("valid-meta-changes", "valid signed stream whose META changes at the superframe boundary (text, then GNSS); "
        "the signature covers payloads only", s8["frames"], "valid",
        "TYPE meta nibble and META differ between superframes; the signature does not cover them",
        SPEC_SIG + "; META every superframe " + D + "l.1143-1147",
        dig=s8["digest"], sg=s8["signature"], sid=0x4D02, type_=v3.v3_type(2, 0, True, v3.META_TEXT, 1),
        packets=packets_of(s8["frames"], 0x4D02, lsd=lsd8),
        extra={"note": "TYPE and META change at packet 6; legacy_meta null = zero META in the legacy re-label"})

    # --- signature damaged
    add("tampered-payload", "one payload bit flipped in data frame 3 (CRC recomputed)",
        v3.neg_flip_payload_bit(frames, 3, 17), "invalid_signature", "digest differs from the signed one",
        SPEC_SIG + " (l.2078)")
    add("wrong-key", "signed with key b, checked against key a", None, "invalid_signature",
        "signature made with another private key", SPEC_SIG, key_sign="b", verify="a",
        sg=v3.sign_digest(sec_b, digest),
        packets=packets_of(v3.signed_frames(payloads, v3.sign_digest(sec_b, digest)), 0x4D01))
    add("lost-frame", "data frame 4 never arrives", v3.neg_lost_frame(frames, 4), "invalid_signature",
        "digest over 5 frames differs", SPEC_SIG + " l.2078")
    add("reordered-frames", "data frames 2 and 3 arrive in swapped order (each keeps its FN)",
        v3.neg_swap_frames(frames, 2, 3), "invalid_signature",
        "digest is order dependent (xor then rotate); arrival-order receivers fail", SPEC_SIG + "; derived: xor-then-rotate is order dependent", also=["valid"],
        extra={"note": "a receiver that restores FN order would verify; the spec says nothing about reordering"})
    # --- tail damaged
    add("missing-tail-frame", "last signature frame (FFFF, EOT) never arrives", v3.neg_truncated_tail(frames, 3),
        "incomplete", "3 of 4 signature frames, no EOT", SPEC_SIG + " l.2065, l.2073")
    add("missing-tail-two", "only the first two signature frames arrive", v3.neg_truncated_tail(frames, 2),
        "incomplete", "2 of 4 signature frames, no EOT", SPEC_SIG)
    add("missing-tail-all", "signed bit set, data frames only, no EOT", v3.neg_truncated_tail(frames, 0),
        "incomplete", "no signature frames", SPEC_SIG)
    add("signed-bit-no-tail", "signed bit set, data stream ends with EOT on the last data frame, no signature frames",
        v3.neg_signed_no_tail(frames), "incomplete", "TYPE says signed, the stream carries no signature",
        D + "l.1216-1221 (signed bit) with " + SPEC_SIG)
    pk = packets_of(frames, 0x4D01)
    add("truncated-mid-stream", "stream cut after 4 data packets (sender or path died)", None, "incomplete",
        "no signature frames, no EOT", SPEC_SIG, packets=pk[:4])
    add("short-packet", "the final signature packet is 53 bytes (one byte lost)", None, "malformed",
        "packet is not 54 bytes", "Part II stream packet is 54 bytes (existing dvtest/proto/m17.py PACKET_SIZE)",
        packets=pk[:-1] + [pk[-1][:53]])
    # --- sequence and size faults of a strict collector
    add("duplicate-data-frame", "data frame 2 arrives twice in a row", v3.neg_duplicate_frame(frames, 2),
        "invalid_signature", "the duplicate enters an arrival-order digest",
        SPEC_SIG + "; derived: FN must count 0,1,2,... (l.716)")
    add("early-tail-chunk", "the first signature chunk (7FFC) arrives after data frame 2, data frames follow",
        v3.neg_early_tail(frames, 3), "malformed", "a data frame after a signature frame",
        SPEC_SIG + " l.2065, l.2073")
    add("short-data-packet", "data packet 3 is 53 bytes (one byte lost)", None, "malformed",
        "packet is not 54 bytes", "Part II stream packet is 54 bytes (existing dvtest/proto/m17.py PACKET_SIZE)",
        packets=pk[:3] + [pk[3][:53]] + pk[4:])
    add("long-packet", "data packet 3 is 55 bytes (one byte too many)", None, "malformed",
        "packet is not 54 bytes", "Part II stream packet is 54 bytes (existing dvtest/proto/m17.py PACKET_SIZE)",
        packets=pk[:3] + [pk[3] + b"\x00"] + pk[4:])
    # --- EOT placement
    for where, title in (("last_data", "EOT set on the last data frame and again on FFFF"),
                         ("first_sig", "EOT set on the first signature frame (7FFC)"),
                         ("third_sig", "EOT set on 7FFE, none on FFFF"),
                         ("none", "no EOT on any frame"),
                         ("both", "EOT set on 7FFE and on FFFF")):
        add("eot-" + where.replace("_", "-"), title, v3.neg_eot(frames, where), "malformed",
            "EOT must be set only on the last signature frame", D + "l.2069, l.2073")
    # --- FN schedule
    dup = list(frames)
    dup[-2] = (0x7FFD, False, dup[-2][2])
    add("tail-duplicate-fn", "signature frame numbers 7FFC 7FFD 7FFD FFFF", dup, "malformed",
        "tail FN not the succession 7FFC 7FFD 7FFE FFFF", D + "l.2065")
    shifted = frames[:N_DATA] + [(0x7FFB + i, i == 3, frames[N_DATA + i][2]) for i in range(4)]
    add("tail-fn-shifted", "signature frames numbered 7FFB 7FFC 7FFD 7FFE (last has EOT)", shifted, "malformed",
        "tail starts at 7FFB, which is inside the data range", D + "l.2065")
    unsigned = packets_of(frames, 0x4D01, type_=v3.v3_type(2, 0, False, 0, 1))
    add("unsigned-with-tail", "signed bit CLEAR in TYPE but four tail frames present", None, "unsigned",
        "the TYPE bit decides whether a stream is signed", D + "l.2075-2076 (tail read only if the TYPE bit is set)",
        packets=unsigned, type_=v3.v3_type(2, 0, False, 0, 1),
        extra={"legacy_type": "0085", "note": "m17-fme keys on FN only and would print a signature line; compare only the spec verdict"})
    # --- digest variants (signature valid for another message)
    for name, msg in sorted(v3.digest_variants(payloads).items()):
        bad_sig = v3.sign_digest(sec_a, msg)
        add("digest-" + name.replace("_", "-"), "signature made over %s instead of the 16-byte digest" % name,
            v3.signed_frames(payloads, bad_sig), "invalid_signature",
            "the verified message is the 16-byte digest read as an integer; this signature is over a different message",
            SPEC_SIG + "; derived: micro-ecc convention (module doc of dvtest/ecdsa_p256.py)", sg=bad_sig)

    # --- unsigned stream without a tail: nothing to verify
    plain = [(i, i == N_DATA - 1, p) for i, p in enumerate(payloads)]
    add("unsigned-plain", "plain voice stream, signed bit clear, no tail, EOT on the last data frame", plain,
        "unsigned", "the TYPE bit is clear: no signature expected", D + "l.1216-1221",
        type_=v3.v3_type(2, 0, False, 0, 1), extra={"legacy_type": "0085"})
    # --- malformed public key (the stream and signature are the genuine ones)
    pub_a = ec.public_key(sec_a)
    off_curve = pub_a[:63] + bytes([pub_a[63] ^ 0x01])
    assert not ec.on_curve((int.from_bytes(off_curve[:32], "big"), int.from_bytes(off_curve[32:], "big")))
    for vid, title, bad_pub in (("bad-key-off-curve", "public key x||y is not a point of the curve (last bit of y flipped)",
                                 off_curve),
                                ("bad-key-63-bytes", "public key is 63 bytes", pub_a[:63]),
                                ("bad-key-zero", "public key is 64 zero bytes", bytes(64))):
        add(vid, title, frames, "invalid_signature", "the key cannot verify anything",
            "derived: x||y must be 64 bytes and on P-256 (micro-ecc uECC_valid_public_key)", verify=None, pub=bad_pub)
    # --- signature with r or s out of range (a complete tail)
    n_bytes = ec.N.to_bytes(32, "big")
    for vid, title, bad_sig in (("bad-signature-r-zero", "r = 0", bytes(32) + sig[32:]),
                                ("bad-signature-s-equals-n", "s = n", sig[:32] + n_bytes),
                                ("bad-signature-s-max", "s = 2^256 - 1 (above n)", sig[:32] + b"\xff" * 32),
                                ("bad-signature-r-equals-n", "r = n", n_bytes + sig[32:])):
        add(vid, title, v3.signed_frames(payloads, bad_sig), "invalid_signature",
            "r and s must be in 1..n-1", "derived: ECDSA range rule", sg=bad_sig)

    # --- frame count at the boundary (large: recipe instead of packets)
    for vid, count, verdict, reason in (
            ("boundary-max-frames", v3.MAX_DATA_FRAMES, "valid",
             "0x7FFC data frames (FN 0..0x7FFB), the longest signed stream: 0x8000 - 4"),
            ("boundary-one-too-many", v3.MAX_DATA_FRAMES + 1, "malformed",
             "data frame FN 0x7FFC collides with the first signature frame")):
        entries.append(_boundary_entry(vid, count, verdict, reason, sec_a))
    return {"schema": SCHEMA, "kind": "signed_streams",
            "packet_layout": "54 bytes: 'M17 ' | stream id (2) | DST (6) | SRC (6) | TYPE (2) | META (14) | "
                             "FN (2, MSB = EOT) | payload (16) | CRC16 (2, poly 0x5935 init 0xFFFF over bytes 0..51)",
            "verdicts": {"valid": "signature verifies", "invalid_signature": "complete tail, signature does not verify",
                         "incomplete": "signed bit set but the tail is missing or short",
                         "malformed": "FN or EOT rules broken, or a packet is not 54 bytes / has a bad CRC",
                         "unsigned": "signed bit clear"},
            "entries": entries}


def _boundary_entry(vid, count, verdict, reason, sec):
    stream_id = 0x4D03
    seed = SEED + "-boundary"
    payloads = v3.payload_stream(seed, count)
    digest = v3.stream_digest(payloads)
    sig = v3.sign_digest(sec, digest)

    def data_packet(i):
        return m17.stream_packet(stream_id, i & 0x7FFF, payloads[i], src=SRC, dst=None,
                                 type_=TYPE_VOICE_SIGNED_CAN1, meta=bytes(14), last=False)

    tail = v3.frames_to_packets(v3.signed_frames([], sig), stream_id, SRC, None, TYPE_VOICE_SIGNED_CAN1, bytes(14))
    entry = _entry(vid, "frame count at the 0x7FFB/0x7FFC boundary (%d data frames)" % count, None, "a", "a", digest,
                   sig, verdict, reason, SPEC_FN + "; " + SPEC_SIG + "; derived: 0x8000 frames minus 4 = 0x7FFC",
                   stream_id=stream_id)
    entry["recipe"] = {
        "data_frames": count,
        "data_packet_i": "stream_packet(stream_id, FN=i, payload_i, SRC N0CALL, DST broadcast, TYPE as above, META zero, no EOT)",
        "payload_i": "first 16 bytes of SHA-256 of the ASCII string '%s/<i>' (i in decimal)" % seed,
        "head_packets": [hx(data_packet(0)), hx(data_packet(1))],
        "last_data_packet": hx(data_packet(count - 1)),
        "tail_packets": [hx(p) for p in tail],
        "packets_are": "data packets 0..data_frames-1 in order, then tail_packets",
    }
    return entry


# ---------------------------------------------------------------- TYPE and mapping rows
# (id, payload, encryption, signed, meta, can, TYPE) - TYPE worked out by hand:
#   TYPE = payload*0x1000 + encryption*0x200 + signed*0x100 + meta*0x10 + can
TYPE_ROWS = (
    ("voice-clear", 2, 0, 0, 0, 0, 0x2000),                      # 2*0x1000
    ("voice-signed-can1", 2, 0, 1, 0, 1, 0x2101),                # 0x2000 + 0x100 + 1
    ("voice-meta-gnss", 2, 0, 0, 1, 0, 0x2010),                  # 0x2000 + 1*0x10
    ("voice-meta-ecd", 2, 0, 0, 2, 0, 0x2020),                   # 0x2000 + 2*0x10
    ("voice-meta-text-can10", 2, 0, 0, 3, 10, 0x203A),           # 0x2000 + 0x30 + 0xA
    ("data-clear", 1, 0, 0, 0, 0, 0x1000),                       # 1*0x1000
    ("packet-text", 0xF, 0, 0, 3, 0, 0xF030),                    # 0xF000 + 0x30
    ("packet-can5", 0xF, 0, 0, 0, 5, 0xF005),                    # 0xF000 + 5
    ("voice-scrambler8", 2, 1, 0, 0, 0, 0x2200),                 # 0x2000 + 1*0x200
    ("voice-scrambler24-signed", 2, 3, 1, 0, 0, 0x2700),         # 0x2000 + 3*0x200 + 0x100
    ("voice-aes128-iv", 2, 4, 0, 0xF, 0, 0x28F0),                # 0x2000 + 4*0x200 + 0xF0
    ("voice-aes192-iv-can3", 2, 5, 0, 0xF, 3, 0x2AF3),           # 0x2000 + 5*0x200 + 0xF0 + 3
    ("voice-aes256-signed-iv-can15", 2, 6, 1, 0xF, 15, 0x2DFF),  # 0x2000 + 6*0x200 + 0x100 + 0xF0 + 0xF
)
# reserved or illegal values: (id, TYPE, why)
TYPE_BAD_ROWS = (
    ("payload-0-is-legacy", 0x0005, "top nibble 0 is the legacy layout, not a v3 TYPE"),
    ("payload-3-reserved", 0x3000, "payload 0x3..0xE reserved"),
    ("encryption-7-reserved", 0x2E00, "7*0x200 = 0xE00, encryption 7 reserved"),
    ("meta-4-reserved", 0x2040, "meta 0x4..0xE reserved"),
    ("packet-with-encryption", 0xF200, "packet mode must have encryption 0"),
    ("packet-with-signed", 0xF100, "packet mode must have the signed bit clear"),
    ("aes-iv-without-aes", 0x20F0, "inference: meta 0xF is the AES IV, encryption is none"),
)
# legacy -> v3: (id, legacy TYPE, v3 TYPE or None, why). Legacy TYPE worked out as
#   stream | data_type<<1 | enc_type<<3 | enc_subtype<<5 | can<<7 | signed*0x800
LEGACY_ROWS = (
    ("voice-clear", 0x0005, 0x2000, "1 + 2<<1 = 5"),
    ("voice-signed-can1", 0x0885, 0x2101, "5 + 1<<7 = 0x85, + 0x800"),
    ("voice-can15", 0x0785, 0x200F, "5 + 15<<7 = 0x785"),
    ("data", 0x0003, 0x1000, "1 + 1<<1 = 3"),
    ("voice-data-1600", 0x0007, None, "data type 3 (1600 bps) was removed in v3"),
    ("packet", 0x0000, 0xF000, "stream bit 0"),
    ("packet-can1", 0x0080, 0xF001, "can 1 << 7"),
    ("scrambler8", 0x000D, 0x2200, "5 + 1<<3 = 0xD"),
    ("scrambler16", 0x002D, 0x2400, "0xD + 1<<5"),
    ("scrambler24", 0x004D, 0x2600, "0xD + 2<<5"),
    ("scrambler-subtype3", 0x006D, None, "scrambler subtype 3 reserved"),
    ("aes128", 0x0015, 0x28F0, "5 + 2<<3 = 0x15; META carries the IV"),
    ("aes192", 0x0035, 0x2AF0, "0x15 + 1<<5"),
    ("aes256", 0x0055, 0x2CF0, "0x15 + 2<<5"),
    ("aes256-signed-can1", 0x08D5, 0x2DF1, "0x55 + 0x80 + 0x800"),
    ("null-enc-gnss", 0x0025, 0x2010, "5 + 1<<5 = 0x25, META is GNSS"),
    ("null-enc-ecd", 0x0045, 0x2020, "5 + 2<<5 = 0x45, META is ECD"),
    ("null-enc-subtype3", 0x0065, None, "null-encryption subtype 3 reserved"),
    ("encryption-type3", 0x001D, None, "encryption type 3 reserved"),
    ("reserved-bits", 0x1005, None, "bits 15-12 must be zero in the legacy layout"),
)


def _gnss_floats(dec):
    """The physical values of a decoded GNSS META as the spec scales them; a
    field whose validity bit is clear is null."""
    v = dec["validity"]
    return {
        "lat_deg": dec["lat_raw"] / 8388607.0 * 90.0 if v & v3.V_LATLON else None,
        "lon_deg": dec["lon_raw"] / 8388607.0 * 180.0 if v & v3.V_LATLON else None,
        "altitude_m": dec["alt_raw"] * 0.5 - 500.0 if v & v3.V_ALT else None,
        "bearing_deg": float(dec["bearing"]) if v & v3.V_VELOCITY else None,
        "speed_kmh": dec["speed_raw"] * 0.5 if v & v3.V_VELOCITY else None,
        "radius_m": float(2 ** dec["radius_n"]) if v & v3.V_RADIUS else None,
    }


def type_meta_file():
    entries = []
    for vid, payload, enc, signed, meta, can, value in TYPE_ROWS:
        entries.append({
            "id": "type-" + vid, "kind": "v3_type",
            "fields": {"payload": payload, "encryption": enc, "signed": bool(signed), "meta": meta, "can": can},
            "type": "%04x" % value, "bytes": "%02x%02x" % (value >> 8, value & 0xFF),
            "spec": SPEC_TYPE + "; derived: " + DERIVE_TYPE,
            "oracle": ORACLE_NONE,
            "oracle_verdict": None})
    for vid, value, why in TYPE_BAD_ROWS:
        entries.append({
            "id": "type-bad-" + vid, "kind": "v3_type_invalid", "type": "%04x" % value, "why": why,
            "problems_expected": True,
            "spec": SPEC_TYPE, "oracle": ORACLE_NONE, "oracle_verdict": None})
    for vid, legacy, v3val, why in LEGACY_ROWS:
        entries.append({
            "id": "map-legacy-" + vid, "kind": "map_legacy_to_v3", "legacy_type": "%04x" % legacy,
            "v3_type": ("%04x" % v3val) if v3val is not None else None,
            "unmappable": v3val is None, "derivation": why,
            "spec": SPEC_V2_TYPE + "; " + SPEC_TYPE, "oracle": ORACLE_NONE, "oracle_verdict": None})
    # v3 -> legacy for the legacy receiver (META omitted)
    text2 = v3.v3_text_blocks("DV-Router M17 v3 text test")
    for vid, v3val, meta, policy in (
            ("voice-clear", 0x2000, bytes(14), "omit"),
            ("voice-signed-can1", 0x2101, bytes(14), "omit"),
            ("voice-gnss-omit", 0x2010, v3.gnss_meta(0, 1, 45.0, 90.0), "omit"),
            ("voice-gnss-convert", 0x2010, v3.gnss_meta(0, 1, 45.0, 90.0), "convert"),
            ("voice-text-omit", 0x2030, text2[0], "omit"),
            ("voice-text-convert", 0x2030, text2[0], "convert"),
            ("voice-ecd-omit", 0x2020, v3.ecd_meta("N0CALL"), "omit"),
            ("voice-ecd-convert", 0x2020, v3.ecd_meta("N0CALL"), "convert"),
            ("voice-aes128-iv", 0x28F0, v3.aes_iv_meta(187000000, bytes(range(0xA0, 0xAA))), "omit"),
            ("scrambler16-signed", 0x2500, bytes(14), "omit"),
            ("data", 0x1000, bytes(14), "omit"),
            ("packet", 0xF000, bytes(14), "omit"),
            ("payload-3-reserved", 0x3000, bytes(14), "omit")):
        try:
            ltype, lmeta = v3.v3_to_legacy(v3val, meta, policy)
            out = {"legacy_type": "%04x" % ltype, "legacy_meta": hx(lmeta), "unmappable": False}
        except v3.Unmappable:
            out = {"legacy_type": None, "legacy_meta": None, "unmappable": True}
        entries.append(dict({
            "id": "map-v3-%s" % vid, "kind": "map_v3_to_legacy", "v3_type": "%04x" % v3val, "v3_meta": hx(meta),
            "policy": policy,
            "spec": SPEC_TYPE + "; " + SPEC_V2_TYPE + "; derived: legacy receiver gets META omitted",
            "oracle": ORACLE_NONE, "oracle_verdict": None}, **out))
    # ---- text
    sample30 = "The quick brown fox jumps over"
    for vid, text, base, chosen in (
            ("13-bytes", "0123456789ABC", 1, True),
            ("short", "N0CALL", 1, True),
            ("3-blocks", sample30, 1, True),
            ("15-blocks-195-bytes", ("0123456789" * 20)[:195], 1, True),
            ("utf8", "café €", 1, True),
            ("3-blocks-index-base-0", sample30, 0, False)):
        blocks = v3.v3_text_blocks(text, base)
        entries.append({
            "id": "text-v3-" + vid, "kind": "meta_text_v3", "text_utf8": hx(text.encode("utf-8")),
            "blocks": [hx(b) for b in blocks], "controls": ["%02x" % b[0] for b in blocks],
            "index_base": base, "chosen_reading": chosen,
            "spec": SPEC_TEXT + "; derived: blocks = ceil(len/13), control = count<<4 | index, NUL padding",
            "oracle": ORACLE_NONE, "oracle_verdict": None})
    entries.append({
        "id": "text-v3-packet-control-0x11", "kind": "meta_text_v3", "text_utf8": hx(b"N0CALL"),
        "blocks": [hx(v3.v3_text_blocks("N0CALL")[0])], "controls": ["11"], "index_base": 1, "chosen_reading": True,
        "spec": D + "l.1245 and l.2215 (packet mode: one block, control byte 0x11)", "oracle": ORACLE_NONE,
        "oracle_verdict": None})
    for vid, text in (("1-block", "N0CALL"), ("2-blocks", "DV-Router M17 v3 text test"), ("3-blocks", sample30),
                      ("4-blocks-52-bytes", ("0123456789" * 6)[:52])):
        blocks = v3.v2_text_blocks(text)
        entries.append({
            "id": "text-v2-" + vid, "kind": "meta_text_v2", "text_utf8": hx(text.encode("utf-8")),
            "blocks": [hx(b) for b in blocks], "controls": ["%02x" % b[0] for b in blocks],
            "spec": SPEC_V2_TEXT + "; derived: control high nibble bit map, low nibble one-hot, space padding",
            "oracle": ORACLE_SIG, "oracle_verdict": None})
    # ---- GNSS
    gnss_rows = (
        # id, source, station, lat, lon, alt, bearing, speed, radius (physical), note
        ("all-valid", 0, 1, 45.0, 90.0, 100.5, 270, 87.5, 5.0),
        ("south-west", 1, 2, -22.5, -45.0, 0.0, 90, 12.0, 1.0),
        ("position-only", 0, 0, 45.0, -22.5, None, None, None, None),
        ("altitude-only", 15, 15, None, None, 1234.5, None, None, None),
        ("extremes", 15, 15, 90.0, -180.0, v3.ALT_MAX_M, 359, 2047.5, 1000.0),
        ("altitude-minimum", 0, 1, 0.0, 0.0, -500.0, 0, 0.0, 128.0),
        ("nothing-valid", 0, 1, None, None, None, None, None, None))
    for vid, src, st, lat, lon, alt, brg, spd, rad in gnss_rows:
        meta = v3.gnss_meta(src, st, lat, lon, alt, brg, spd, rad)
        dec = v3.decode_gnss(meta)
        entries.append({
            "id": "gnss-" + vid, "kind": "meta_gnss", "meta": hx(meta),
            "inputs": {"source": src, "station": st, "lat_deg": lat, "lon_deg": lon, "altitude_m": alt,
                       "bearing_deg": brg, "speed_kmh": spd, "radius_m": rad},
            "raw": {k: dec[k] for k in ("validity", "radius_n", "bearing", "lat_raw", "lon_raw", "alt_raw",
                                         "speed_raw")},
            "decoded": {k: dec[k] for k in ("source", "station", "validity", "radius_n", "bearing", "lat_raw",
                                            "lon_raw", "alt_raw", "speed_raw")},
            "decoded_float": _gnss_floats(dec),
            "expectation": "the integers of 'decoded' are the expectation of a decoder; 'decoded_float' is the "
                           "same values in units (null = validity bit clear, field ignored); no encoder rounding "
                           "is assumed",
            "libm17_args": {"data_source": src, "station_type": st, "lat": lat, "lon": lon, "validity": dec["validity"],
                            "altitude": alt, "bearing": brg, "speed": spd, "radius": rad},
            "spec": SPEC_GNSS + "; derived: lat/lon raw = trunc(deg/90*8388607) and trunc(deg/180*8388607) "
                    "(cut toward zero, the reading of libm17), altitude raw = (m+500)*2, speed raw = km/h*2, "
                    "radius = smallest n with r <= 2^n (7 above 128 m)",
            "oracle": ORACLE_GNSS, "oracle_verdict": None})
    # ---- ECD and AES IV, with a full packet each (the gate must refuse ECD on an Internet link)
    ecd_rows = (("one-callsign", "N0CALL", None), ("two-callsigns", "N0CALL", "XLX999 A"))
    for vid, c1, c2 in ecd_rows:
        meta = v3.ecd_meta(c1, c2)
        t = v3.v3_type(2, 0, False, v3.META_ECD, 1)
        pkt = m17.stream_packet(0x4D04, 0, v3.payload_stream(SEED, 1)[0], src=SRC, dst=None, type_=t, meta=meta)
        entries.append({
            "id": "ecd-" + vid, "kind": "meta_ecd", "meta": hx(meta), "callsigns": [c1, c2], "type": "%04x" % t,
            "packet": hx(pkt),
            "gate_expectation": "refuse on an Internet link (ECD is for RF repeater/hotspot use only)",
            "spec": SPEC_ECD + "; derived: 6-byte base-40 callsigns (existing encode_callsign), NUL fill to 14 bytes",
            "oracle": "libm17", "oracle_verdict": None})
    ts, rnd = 187000000, bytes(range(0xA0, 0xAA))
    iv = v3.aes_iv_meta(ts, rnd)
    t = v3.v3_type(2, v3.ENC_AES128, False, v3.META_AES_IV, 1)
    pkt = m17.stream_packet(0x4D05, 0, v3.payload_stream(SEED, 1)[0], src=SRC, dst=None, type_=t, meta=iv)
    for fn in (0, 5):
        entries.append({
            "id": "aes-iv-counter-fn%d" % fn, "kind": "meta_aes_iv", "meta": hx(iv),
            "timestamp_2020": ts, "random10": hx(rnd), "fn": fn,
            "counter_chosen": hx(v3.aes_counter(iv, fn)),
            "counter_draft_prose": hx(v3.aes_counter_draft_prose(iv, fn)),
            "type": "%04x" % t, "packet": hx(pkt), "gate_expectation": None,
            "spec": SPEC_AES + "; derived: META = timestamp(4, big endian) || random(10); counter = META || FN(2)",
            "oracle": "libm17+m17-fme",
            "oracle_verdict": None})
    return {"schema": SCHEMA, "kind": "type_meta", "entries": entries}


# ---------------------------------------------------------------- manifest and driver

def dump(doc):
    """JSON text: the header keys on top, then one entry per line (a diff then
    shows which vector changed)."""
    head = {k: v for k, v in doc.items() if k != "entries"}
    if "entries" not in doc:
        return json.dumps(head, indent=1, ensure_ascii=True) + "\n"
    top = json.dumps(head, indent=1, ensure_ascii=True)
    assert top.endswith("\n}") or top.endswith("}")
    lines = [json.dumps(e, ensure_ascii=True, separators=(",", ":")) for e in doc["entries"]]
    return top[:-1].rstrip() + ',\n "entries": [\n  ' + ",\n  ".join(lines) + "\n ]\n}\n"


OVERLAY_NAME = "oracle_verdicts.json"


def load_overlay(directory=None):
    """{entry id: oracle verdict} from the optional hand/tool written file
    vectors/m17v3/oracle_verdicts.json; {} when it does not exist. It is the
    only input that is not derived, and it is how oracle_verdict stops being
    null once an oracle has been run."""
    path = os.path.join(directory or os.path.join(ROOT, "vectors", "m17v3"), OVERLAY_NAME)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="ascii") as handle:
        return json.load(handle)


def build_all(overlay=None):
    """{file name: text}, manifest.json last. overlay: {entry id: oracle
    verdict}, default load_overlay()."""
    overlay = load_overlay() if overlay is None else overlay
    docs = {
        "keys.json": keys_file(),
        "ecdsa.json": ecdsa_file(),
        "signed_streams.json": signed_streams_file(),
        "type_meta.json": type_meta_file(),
    }
    ids = [e["id"] for doc in docs.values() for e in doc["entries"]]
    if len(ids) != len(set(ids)):
        raise ValueError("entry ids are not unique across the files")
    unknown = sorted(set(overlay) - set(ids))
    if unknown:
        raise ValueError("oracle verdicts for unknown ids: %s" % unknown)
    for doc in docs.values():
        for entry in doc["entries"]:
            if entry["id"] in overlay:
                entry["oracle_verdict"] = overlay[entry["id"]]
    files = {name: dump(doc) for name, doc in docs.items()}
    manifest = {
        "schema": SCHEMA,
        "generator": "python3 scripts/gen-m17v3-vectors.py",
        "licence": "MIT; derived from the specification text and arithmetic, no code of any M17 implementation",
        "sources_pinned_in_env_example": load_pins(),
        "spec_refs": {
            "draft": "M17_spec.tex at M17_SPEC_REF (branch dev, 3.0.0-draft, 3124 lines); l.N = line N",
            "v2.0.10": "M17_spec.tex at M17_SPEC_V2_REF (branch main, 2.0.10, 3229 lines); l.N = line N",
            "derived": "worked out from spec arithmetic; the text after 'derived:' says how"},
        "oracles": {
            "m17-fme": "m17-fme -^ <ip-frame file> -k <public key file>; prints ' Signature Valid;' or "
                       "' Signature Invalid;' (src/encryption/ecdsa.c); v3 packets are re-labelled with legacy_type "
                       "first (dvtest.proto.m17v3.retype_packets) because it rejects a v3 TYPE",
            "libm17": "payload/lsf.c set_LSF_meta_position / get_LSF_meta_position / set_LSF_meta_ecd / "
                      "set_LSF_meta_nonce; compare lat/lon within 1 LSB (float32)",
            "libm17+mrefd": "libm17 as above and mrefd position.cpp (GNSS decode)",
            "libm17+m17-fme": "libm17 set_LSF_meta_nonce and m17-fme aes_ctr_str_payload_crypt (IV bytes 14-15 = FN)",
            "ecdsa-verifier": "any P-256 ECDSA verifier over the 32-byte digest",
            "spec-only": "no independent v3 decoder exists; the specification text is the only oracle"},
        "policies": {
            "map_v3_to_legacy omit": "a legacy receiver gets META dropped (zero) except an AES IV",
            "map_v3_to_legacy convert": "GNSS passes, text converts if it fits 4 blocks, ECD is still dropped"},
        "entry_fields": {"id": "unique", "kind": "entry type", "spec": "file and line read, or 'derived: ...'",
                         "oracle": "independent implementation that can check the entry",
                         "oracle_verdict": "null until that oracle was run"},
        "files": [],
    }
    for name, text in sorted(files.items()):
        doc = json.loads(text)
        manifest["files"].append({"path": name, "kind": doc["kind"], "entries": len(doc["entries"]),
                                  "sha256": hashlib.sha256(text.encode("ascii")).hexdigest()})
    files["manifest.json"] = dump(manifest)
    return files
