# SPDX-License-Identifier: MIT
"""dvtest.ecdsa_p256: published RFC 6979 vectors, round trips, negatives and
the micro-ecc digest convention (16-byte digest left-padded to 32 bytes)."""
import hashlib
import time

import pytest

from dvtest import ecdsa_p256 as E

# --- published by RFC 6979, appendix A.2.5 (ECDSA, 256 bits, curve P-256) ---
RFC_X = bytes.fromhex("C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721")
RFC_UX = "60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6"
RFC_UY = "7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299"
# (message, k, r, s) with SHA-256
RFC_SHA256 = (
    (b"sample",
     "A6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60",
     "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716",
     "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8"),
    (b"test",
     "D16B6AE827F17175E040871A1C7EC3500192C4C92677336EC2537ACAEE0008E0",
     "F1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367",
     "019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083"),
)

SECRET = hashlib.sha256(b"dvtest ecdsa unit key 1").digest()
SECRET2 = hashlib.sha256(b"dvtest ecdsa unit key 2").digest()
DIGEST16 = bytes(range(0x10, 0x20))


def flip(data, bit):
    out = bytearray(data)
    out[bit // 8] ^= 0x80 >> (bit % 8)
    return bytes(out)


def test_curve_constants():
    assert E.on_curve(E.G)
    assert E._mul(E.N, E.G) is None                      # n*G is the point at infinity
    assert E._mul(E.N - 1, E.G) == (E.GX, E.P - E.GY)    # -G
    assert not E.on_curve((E.GX, E.GY + 1))


def test_rfc6979_public_key():
    pub = E.public_key(RFC_X)
    assert pub.hex().upper() == RFC_UX + RFC_UY


@pytest.mark.parametrize("msg,k,r,s", RFC_SHA256)
def test_rfc6979_sha256_vectors(msg, k, r, s):
    digest = hashlib.sha256(msg).digest()
    # the nonce of the RFC and the signature
    first = next(E._rfc6979_k(int.from_bytes(RFC_X, "big"), digest))
    assert first == int(k, 16)
    sig = E.sign_digest(RFC_X, digest)
    assert sig.hex().upper() == r + s
    assert E.verify_digest(E.public_key(RFC_X), digest, sig)


def test_round_trip_16_byte_digest():
    pub = E.public_key(SECRET)
    sig = E.sign_digest(SECRET, DIGEST16)
    assert len(sig) == 64 and len(pub) == 64
    assert E.verify_digest(pub, DIGEST16, sig)
    assert E.sign_digest(SECRET, DIGEST16) == sig         # deterministic


def test_round_trip_many_digest_lengths():
    pub = E.public_key(SECRET)
    for n in (1, 15, 16, 20, 31, 32, 33, 64):
        digest = hashlib.sha512(bytes([n])).digest()[:n]
        assert E.verify_digest(pub, digest, E.sign_digest(SECRET, digest)), n


def test_micro_ecc_left_padding_convention():
    pub = E.public_key(SECRET)
    sig = E.sign_digest(SECRET, DIGEST16)
    assert E.verify_digest(pub, bytes(16) + DIGEST16, sig)         # left-padded to 32: same number
    assert not E.verify_digest(pub, DIGEST16 + bytes(16), sig)     # right-padded: another number
    assert not E.verify_digest(pub, DIGEST16 + b"\0", sig)
    assert not E.verify_digest(pub, DIGEST16[:15], sig)
    # input longer than 32 bytes: only the first 32 count
    long = hashlib.sha256(b"x").digest() + b"tail"
    assert E.verify_digest(pub, long[:32], E.sign_digest(SECRET, long))


def test_wrong_key_and_flipped_bits():
    pub = E.public_key(SECRET)
    sig = E.sign_digest(SECRET, DIGEST16)
    assert not E.verify_digest(E.public_key(SECRET2), DIGEST16, sig)
    for bit in (0, 1, 7, 100, 255, 256, 300, 511):
        assert not E.verify_digest(pub, DIGEST16, flip(sig, bit)), bit
    for bit in (0, 5, 8, 63, 127):
        assert not E.verify_digest(pub, flip(DIGEST16, bit), sig), bit
    assert not E.verify_digest(flip(pub, 3), DIGEST16, sig)       # off the curve


def test_malformed_signature_and_key():
    pub = E.public_key(SECRET)
    sig = E.sign_digest(SECRET, DIGEST16)
    zero = bytes(32)
    n = E.N.to_bytes(32, "big")
    assert not E.verify_digest(pub, DIGEST16, zero + sig[32:])    # r = 0
    assert not E.verify_digest(pub, DIGEST16, sig[:32] + zero)    # s = 0
    assert not E.verify_digest(pub, DIGEST16, n + sig[32:])       # r = n
    assert not E.verify_digest(pub, DIGEST16, sig[:32] + n)       # s = n
    assert not E.verify_digest(pub, DIGEST16, sig[:63])
    assert not E.verify_digest(pub, DIGEST16, sig + b"\0")
    assert not E.verify_digest(pub[:63], DIGEST16, sig)
    assert not E.verify_digest(bytes(64), DIGEST16, sig)
    with pytest.raises(E.EcdsaError):
        E.public_key(bytes(32))
    with pytest.raises(E.EcdsaError):
        E.public_key(E.N.to_bytes(32, "big"))
    with pytest.raises(E.EcdsaError):
        E.public_key(b"short")


def test_sec1_forms():
    pub = E.public_key(SECRET)
    sec1 = E.sec1_uncompressed(pub)
    assert sec1[0] == 4 and sec1[1:] == pub and len(sec1) == 65
    assert E.from_sec1_uncompressed(sec1) == pub
    with pytest.raises(E.EcdsaError):
        E.from_sec1_uncompressed(b"\x02" + pub)


def test_speed():
    start = time.monotonic()
    pub = E.public_key(SECRET)
    sig = E.sign_digest(SECRET, DIGEST16)
    assert E.verify_digest(pub, DIGEST16, sig)
    assert time.monotonic() - start < 1.0


def test_cross_check_with_openssl_binding():
    """Independent check of the arithmetic through the `cryptography` package
    (OpenSSL) when it is installed: a 32-byte left-padded digest is an
    ordinary prehashed message there."""
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        Prehashed, decode_dss_signature, encode_dss_signature)

    padded = bytes(16) + DIGEST16
    pub = E.public_key(SECRET)
    pn = ec.EllipticCurvePublicNumbers(int.from_bytes(pub[:32], "big"), int.from_bytes(pub[32:], "big"),
                                       ec.SECP256R1())
    sig = E.sign_digest(SECRET, DIGEST16)
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    pn.public_key().verify(encode_dss_signature(r, s), padded, ec.ECDSA(Prehashed(hashes.SHA256())))

    key = ec.derive_private_key(int.from_bytes(SECRET, "big"), ec.SECP256R1())
    r2, s2 = decode_dss_signature(key.sign(padded, ec.ECDSA(Prehashed(hashes.SHA256()))))
    assert E.verify_digest(pub, DIGEST16, r2.to_bytes(32, "big") + s2.to_bytes(32, "big"))
