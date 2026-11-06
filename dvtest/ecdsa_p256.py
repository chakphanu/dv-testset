# SPDX-License-Identifier: MIT
"""Pure-Python ECDSA over NIST P-256 (secp256r1), stdlib only, for the M17
signed-stream tests (docs/m17v3-cases.md).

Written from the public curve definition (FIPS 186-4 D.1.2.3), SEC 1 point
encoding and RFC 6979 (deterministic k with HMAC-SHA-256). Test use only:
the scalar multiplication is NOT constant time, so never use it for a real
key.

Interop convention of the micro-ecc library that M17 implementations use
(read in uECC.c, bits2int() and uECC_sign(); protocol facts only, no code
copied):
  * the message is a digest of any length; its bytes are read as ONE
    big-endian integer, at most 32 bytes (longer input is cut to the first
    32 bytes). A 16-byte M17 stream digest is therefore LEFT-padded with
    zeros to 32 bytes, not hashed again and not right-padded;
  * the signature is r (32 bytes, big endian) || s (32 bytes, big endian),
    64 bytes, no DER, no low-s rule;
  * the public key is x (32) || y (32), 64 bytes, without the 0x04 prefix;
  * the private key is the 32-byte big-endian scalar.

The signer here is deterministic (RFC 6979, SHA-256) so the generated
vectors are reproducible; micro-ecc signs with a random k, which makes no
difference to a verifier.
"""
import hashlib
import hmac

# domain parameters of P-256
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
G = (GX, GY)

INF = None


class EcdsaError(ValueError):
    pass


# ---------------------------------------------------------------- group arithmetic
# Jacobian coordinates (X, Y, Z) with x = X/Z^2, y = Y/Z^3; the point at
# infinity has Z == 0.

def _jdouble(pt):
    x1, y1, z1 = pt
    if z1 == 0 or y1 == 0:
        return (1, 1, 0)
    ysq = y1 * y1 % P
    s = 4 * x1 * ysq % P
    m = 3 * (x1 - z1 * z1) * (x1 + z1 * z1) % P          # a = -3
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * ysq * ysq) % P
    z3 = 2 * y1 * z1 % P
    return (x3, y3, z3)


def _jadd(p1, p2):
    x1, y1, z1 = p1
    x2, y2, z2 = p2
    if z1 == 0:
        return p2
    if z2 == 0:
        return p1
    z1sq = z1 * z1 % P
    z2sq = z2 * z2 % P
    u1 = x1 * z2sq % P
    u2 = x2 * z1sq % P
    s1 = y1 * z2sq * z2 % P
    s2 = y2 * z1sq * z1 % P
    if u1 == u2:
        if s1 != s2:
            return (1, 1, 0)
        return _jdouble(p1)
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hsq = h * h % P
    hcu = hsq * h % P
    v = u1 * hsq % P
    x3 = (r * r - hcu - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hcu) % P
    z3 = h * z1 * z2 % P
    return (x3, y3, z3)


def _to_affine(pt):
    x, y, z = pt
    if z == 0:
        return INF
    zinv = pow(z, -1, P)
    zinv2 = zinv * zinv % P
    return (x * zinv2 % P, y * zinv2 * zinv % P)


def _mul(k, point):
    """k * point (affine in, affine out; INF is None)."""
    if point is INF or k % N == 0:
        return INF
    k %= N
    base = (point[0], point[1], 1)
    acc = (1, 1, 0)
    for bit in bin(k)[2:]:
        acc = _jdouble(acc)
        if bit == "1":
            acc = _jadd(acc, base)
    return _to_affine(acc)


def _mul2(u1, u2, point):
    """u1*G + u2*point, Shamir's trick."""
    g = (GX, GY, 1)
    q = (point[0], point[1], 1)
    both = _jadd(g, q)
    acc = (1, 1, 0)
    for i in range(max(u1.bit_length(), u2.bit_length()) - 1, -1, -1):
        acc = _jdouble(acc)
        b1 = (u1 >> i) & 1
        b2 = (u2 >> i) & 1
        if b1 and b2:
            acc = _jadd(acc, both)
        elif b1:
            acc = _jadd(acc, g)
        elif b2:
            acc = _jadd(acc, q)
    return _to_affine(acc)


def on_curve(point):
    if point is INF:
        return False
    x, y = point
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


# ---------------------------------------------------------------- keys

def _scalar(secret):
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != 32:
        raise EcdsaError("secret must be 32 bytes")
    d = int.from_bytes(secret, "big")
    if not 1 <= d < N:
        raise EcdsaError("secret out of range 1..n-1")
    return d


def public_key(secret):
    """x||y (64 bytes, micro-ecc form) of a 32-byte secret."""
    x, y = _mul(_scalar(secret), G)
    return x.to_bytes(32, "big") + y.to_bytes(32, "big")


def sec1_uncompressed(pub64):
    """04||x||y (65 bytes) from the micro-ecc x||y form."""
    if len(pub64) != 64:
        raise EcdsaError("public key must be 64 bytes (x||y)")
    return b"\x04" + bytes(pub64)


def from_sec1_uncompressed(pub65):
    if len(pub65) != 65 or pub65[0] != 4:
        raise EcdsaError("not an uncompressed SEC1 point")
    return bytes(pub65[1:])


def _point(pub64):
    if not isinstance(pub64, (bytes, bytearray)) or len(pub64) != 64:
        raise EcdsaError("public key must be 64 bytes (x||y)")
    pt = (int.from_bytes(pub64[:32], "big"), int.from_bytes(pub64[32:], "big"))
    if not on_curve(pt):
        raise EcdsaError("public key is not on the curve")
    return pt


# ---------------------------------------------------------------- digest handling

def digest_to_int(digest):
    """micro-ecc reading of the message: first min(len, 32) bytes as one
    big-endian integer (a 16-byte digest is left-padded with zeros)."""
    return int.from_bytes(bytes(digest)[:32], "big")


def _rfc6979_k(d, digest):
    """RFC 6979 section 3.2 with HMAC-SHA-256 for a 256-bit group. h1 is the
    digest as given (bits2octets of a short h1 is its integer, left-padded)."""
    x = d.to_bytes(32, "big")
    h1 = (digest_to_int(digest) % N).to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            yield cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


# ---------------------------------------------------------------- sign / verify

def sign_digest(secret, digest):
    """r||s (64 bytes) over the digest bytes (micro-ecc convention, see the
    module doc); deterministic."""
    d = _scalar(secret)
    e = digest_to_int(digest)
    for k in _rfc6979_k(d, digest):
        pt = _mul(k, G)
        r = pt[0] % N
        if r == 0:
            continue
        s = pow(k, -1, N) * (e + r * d) % N
        if s == 0:
            continue
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_digest(pub64, digest, sig64):
    """True when sig64 (r||s) is a valid signature of the digest bytes under
    the x||y public key; False for any malformed input."""
    try:
        q = _point(pub64)
    except EcdsaError:
        return False
    if not isinstance(sig64, (bytes, bytearray)) or len(sig64) != 64:
        return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = digest_to_int(digest)
    w = pow(s, -1, N)
    pt = _mul2(e * w % N, r * w % N, q)
    if pt is INF:
        return False
    return pt[0] % N == r
