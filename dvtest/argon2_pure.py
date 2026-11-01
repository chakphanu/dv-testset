# SPDX-License-Identifier: MIT
"""Argon2id (RFC 9106, version 0x13) in pure Python, stdlib only.

Why it exists: DV-Router's admin users file holds `name:role:$argon2id$...`
lines (docs/design/admin.md, "Auth"), and the stdlib has no Argon2. The rig
writes that file for a random per-run password of each admin test account
(dvtest/tools/admin_users.py). This is a test helper, not a password library:
it is slow (about 1 ms per 1 KiB block), so the rig uses small memory costs and
the unit test checks the RFC 9106 test vector for Argon2id.

  argon2id(password, salt, t, m_kib, p, taglen=32, secret=b"", ad=b"") -> bytes
  phc(password, salt, ...)  -> "$argon2id$v=19$m=..,t=..,p=..$<salt>$<tag>"
"""
import base64
import hashlib
import struct

MASK = 0xFFFFFFFFFFFFFFFF
BLOCK_WORDS = 128
SYNC_POINTS = 4
VERSION = 0x13
TYPE_ID = 2


def _le32(n):
    return struct.pack("<I", n)


def _blake2b(data, size):
    return hashlib.blake2b(data, digest_size=size).digest()


def _h_prime(tau, data):
    """Variable-length hash H' of RFC 9106, section 3.3."""
    if tau <= 64:
        return _blake2b(_le32(tau) + data, tau)
    r = (tau + 31) // 32 - 2
    v = _blake2b(_le32(tau) + data, 64)
    out = bytearray(v[:32])
    for _ in range(1, r):
        v = _blake2b(v, 64)
        out += v[:32]
    out += _blake2b(v, tau - 32 * r)
    return bytes(out)


def _gb(v, a, b, c, d):
    va, vb, vc, vd = v[a], v[b], v[c], v[d]
    va = (va + vb + 2 * (va & 0xFFFFFFFF) * (vb & 0xFFFFFFFF)) & MASK
    vd ^= va
    vd = ((vd >> 32) | (vd << 32)) & MASK
    vc = (vc + vd + 2 * (vc & 0xFFFFFFFF) * (vd & 0xFFFFFFFF)) & MASK
    vb ^= vc
    vb = ((vb >> 24) | (vb << 40)) & MASK
    va = (va + vb + 2 * (va & 0xFFFFFFFF) * (vb & 0xFFFFFFFF)) & MASK
    vd ^= va
    vd = ((vd >> 16) | (vd << 48)) & MASK
    vc = (vc + vd + 2 * (vc & 0xFFFFFFFF) * (vd & 0xFFFFFFFF)) & MASK
    vb ^= vc
    vb = ((vb >> 63) | (vb << 1)) & MASK
    v[a], v[b], v[c], v[d] = va, vb, vc, vd


def _permute(v):
    _gb(v, 0, 4, 8, 12)
    _gb(v, 1, 5, 9, 13)
    _gb(v, 2, 6, 10, 14)
    _gb(v, 3, 7, 11, 15)
    _gb(v, 0, 5, 10, 15)
    _gb(v, 1, 6, 11, 12)
    _gb(v, 2, 7, 8, 13)
    _gb(v, 3, 4, 9, 14)


def _compress(x, y):
    """G(X, Y) of RFC 9106, section 3.5: blocks are lists of 128 u64 words."""
    r = [a ^ b for a, b in zip(x, y)]
    z = list(r)
    for i in range(8):                       # rows
        row = z[16 * i:16 * i + 16]
        _permute(row)
        z[16 * i:16 * i + 16] = row
    for i in range(8):                       # columns
        idx = []
        for k in range(8):
            idx += [16 * k + 2 * i, 16 * k + 2 * i + 1]
        col = [z[j] for j in idx]
        _permute(col)
        for j, val in zip(idx, col):
            z[j] = val
    return [a ^ b for a, b in zip(z, r)]


def _to_block(raw):
    return list(struct.unpack("<128Q", raw))


def _from_block(block):
    return struct.pack("<128Q", *block)


def argon2id(password, salt, t, m_kib, p, taglen=32, secret=b"", ad=b""):
    """The Argon2id tag (bytes). Raises ValueError for parameters RFC 9106 forbids."""
    if p < 1 or t < 1 or taglen < 4 or len(salt) < 8 or m_kib < 8 * p:
        raise ValueError("argon2id: parameters out of range")
    h0 = _blake2b(b"".join((
        _le32(p), _le32(taglen), _le32(m_kib), _le32(t), _le32(VERSION), _le32(TYPE_ID),
        _le32(len(password)), password, _le32(len(salt)), salt,
        _le32(len(secret)), secret, _le32(len(ad)), ad)), 64)
    m_prime = 4 * p * (m_kib // (4 * p))
    q = m_prime // p                         # blocks per lane
    seg = q // SYNC_POINTS
    mem = [[None] * q for _ in range(p)]
    for lane in range(p):
        mem[lane][0] = _to_block(_h_prime(1024, h0 + _le32(0) + _le32(lane)))
        mem[lane][1] = _to_block(_h_prime(1024, h0 + _le32(1) + _le32(lane)))
    zero = [0] * BLOCK_WORDS
    for ps in range(t):
        for sl in range(SYNC_POINTS):
            independent = ps == 0 and sl < 2          # Argon2id: data-independent first half of pass 0
            for lane in range(p):
                addr = None
                inp = None
                if independent:
                    inp = [0] * BLOCK_WORDS
                    inp[0], inp[1], inp[2], inp[3], inp[4], inp[5] = ps, lane, sl, m_prime, t, TYPE_ID
                start = 0
                if ps == 0 and sl == 0:
                    start = 2
                    if independent:
                        inp[6] += 1
                        addr = _compress(zero, _compress(zero, inp))
                for i in range(start, seg):
                    col = sl * seg + i
                    prev = mem[lane][(col - 1) % q]
                    if independent:
                        if i % BLOCK_WORDS == 0:
                            inp[6] += 1
                            addr = _compress(zero, _compress(zero, inp))
                        rnd = addr[i % BLOCK_WORDS]
                    else:
                        rnd = prev[0]
                    j1, j2 = rnd & 0xFFFFFFFF, rnd >> 32
                    ref_lane = lane if (ps == 0 and sl == 0) else j2 % p
                    same = ref_lane == lane
                    if ps == 0:
                        if sl == 0:
                            area = i - 1
                        elif same:
                            area = sl * seg + i - 1
                        else:
                            area = sl * seg - (1 if i == 0 else 0)
                    else:
                        if same:
                            area = q - seg + i - 1
                        else:
                            area = q - seg - (1 if i == 0 else 0)
                    rel = (j1 * j1) >> 32
                    rel = area - 1 - ((area * rel) >> 32)
                    first = 0 if (ps == 0 or sl == 3) else (sl + 1) * seg
                    ref = mem[ref_lane][(first + rel) % q]
                    new = _compress(prev, ref)
                    if ps != 0:
                        new = [a ^ b for a, b in zip(new, mem[lane][col])]
                    mem[lane][col] = new
    final = mem[0][q - 1]
    for lane in range(1, p):
        final = [a ^ b for a, b in zip(final, mem[lane][q - 1])]
    return _h_prime(taglen, _from_block(final))


def _b64(data):
    return base64.b64encode(data).decode("ascii").rstrip("=")


def phc(password, salt, t=1, m_kib=64, p=1, taglen=32):
    """PHC string of an Argon2id hash (what the admin users file stores)."""
    if isinstance(password, str):
        password = password.encode("utf-8")
    tag = argon2id(password, salt, t, m_kib, p, taglen)
    return "$argon2id$v=19$m=%d,t=%d,p=%d$%s$%s" % (m_kib, t, p, _b64(salt), _b64(tag))
