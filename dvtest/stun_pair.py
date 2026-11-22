# SPDX-License-Identifier: MIT
"""A minimal signed STUN Binding client for the ICE-lite WebRTC edge (RFC 5389 / 8445, stdlib only): what a tester socket needs to become a VALIDATED source of a peer
(EDG4b, EDG4c: docs/edg4-cases.md). The edge answers a request whose USERNAME is `<edge ufrag>:<client ufrag>` and whose MESSAGE-INTEGRITY is the HMAC-SHA1 keyed with the edge's
`ice-pwd`; a success response with the same transaction id proves the source was validated. No secret is logged: callers pass the password in and never print it.

  request(tid, username, password, priority=..., use_candidate=False, controlling=True)   the signed request bytes
  parse(data) -> Message(cls, method, tid, attrs)        attrs: {type: value bytes}
  is_success(data, tid)                                    a Binding success with that transaction id
  integrity_ok(data, password) / fingerprint_ok(data)      checks of a message (the unit tests verify what request() builds)
  dtls_fatal_alert()                                       the plaintext epoch-0 fatal alert record (15 bytes)
"""
import hashlib
import hmac
import os
import struct
import zlib
from collections import namedtuple

MAGIC = 0x2112A442
BINDING_REQUEST, BINDING_SUCCESS = 0x0001, 0x0101
USERNAME, MESSAGE_INTEGRITY, PRIORITY, USE_CANDIDATE, FINGERPRINT, ICE_CONTROLLING = 0x0006, 0x0008, 0x0024, 0x0025, 0x8028, 0x802A
XOR_MAPPED_ADDRESS = 0x0020
FINGERPRINT_XOR = 0x5354554E
HEADER = 20

Message = namedtuple("Message", "type tid attrs")


def new_tid():
    return os.urandom(12)


def _attr(kind, value):
    pad = (-len(value)) % 4
    return struct.pack(">HH", kind, len(value)) + value + b"\0" * pad


def _header(kind, length, tid):
    return struct.pack(">HHI", kind, length, MAGIC) + tid


def request(tid, username, password, priority=1845494271, use_candidate=False, controlling=True, tie_breaker=None):
    """The signed Binding request: USERNAME, PRIORITY, [ICE-CONTROLLING], [USE-CANDIDATE], MESSAGE-INTEGRITY, FINGERPRINT. `password` is the remote (edge) ice-pwd."""
    if len(tid) != 12:
        raise ValueError("a transaction id has 12 bytes")
    body = _attr(USERNAME, username.encode("ascii")) + _attr(PRIORITY, struct.pack(">I", priority))
    if controlling:
        body += _attr(ICE_CONTROLLING, tie_breaker if tie_breaker is not None else os.urandom(8))
    if use_candidate:
        body += _attr(USE_CANDIDATE, b"")
    # the integrity covers the message with the length field counting the integrity attribute (24 bytes) but not the fingerprint
    head = _header(BINDING_REQUEST, len(body) + 24, tid)
    mac = hmac.new(password.encode("ascii"), head + body, hashlib.sha1).digest()
    body += _attr(MESSAGE_INTEGRITY, mac)
    head = _header(BINDING_REQUEST, len(body) + 8, tid)
    crc = (zlib.crc32(head + body) & 0xFFFFFFFF) ^ FINGERPRINT_XOR
    body += _attr(FINGERPRINT, struct.pack(">I", crc))
    return _header(BINDING_REQUEST, len(body), tid) + body


def parse(data):
    """Message(type, tid, attrs) of a STUN message, or None when it is not one (short, bad cookie, bad length)."""
    if len(data) < HEADER or (data[0] & 0xC0):
        return None
    kind, length, cookie = struct.unpack(">HHI", data[:8])
    if cookie != MAGIC or length != len(data) - HEADER or length % 4:
        return None
    attrs, pos = {}, HEADER
    while pos + 4 <= len(data):
        t, n = struct.unpack(">HH", data[pos:pos + 4])
        if pos + 4 + n > len(data):
            return None
        attrs.setdefault(t, data[pos + 4:pos + 4 + n])
        pos += 4 + n + ((-n) % 4)
    return Message(kind, data[8:HEADER], attrs)


def is_success(data, tid):
    m = parse(data)
    return m is not None and m.type == BINDING_SUCCESS and m.tid == tid


def _offset_of(data, kind):
    pos = HEADER
    while pos + 4 <= len(data):
        t, n = struct.unpack(">HH", data[pos:pos + 4])
        if t == kind:
            return pos
        pos += 4 + n + ((-n) % 4)
    return None


def integrity_ok(data, password):
    """True when the MESSAGE-INTEGRITY of `data` verifies under `password` (the length field is rewritten to end at the integrity attribute, RFC 5389 15.4)."""
    pos = _offset_of(data, MESSAGE_INTEGRITY)
    if pos is None:
        return False
    covered = bytearray(data[:pos])
    covered[2:4] = struct.pack(">H", pos - HEADER + 24)
    mac = hmac.new(password.encode("ascii"), bytes(covered), hashlib.sha1).digest()
    return hmac.compare_digest(mac, data[pos + 4:pos + 24])


def fingerprint_ok(data):
    pos = _offset_of(data, FINGERPRINT)
    if pos is None or pos + 8 != len(data):
        return False
    return ((zlib.crc32(data[:pos]) & 0xFFFFFFFF) ^ FINGERPRINT_XOR) == struct.unpack(">I", data[pos + 4:pos + 8])[0]


def dtls_fatal_alert():
    """A plaintext epoch-0 DTLS 1.2 fatal alert (handshake_failure) record: the datagram a hostile alternate source sends to kill a handshake in progress (EDG4c)."""
    return bytes([21, 0xFE, 0xFD] + [0] * 9 + [2, 2, 40])


def junk_rtp(size=60):
    """A datagram that looks like RTP (version 2) and authenticates as nothing."""
    return bytes([0x80, 0x00]) + os.urandom(size - 2)


def success_response(tid, password, address=("192.0.2.1", 40000)):
    """A Binding success as a server would send it (for the unit tests of is_success / parse): XOR-MAPPED-ADDRESS, integrity, fingerprint."""
    host = bytes(int(x) for x in address[0].split("."))
    xaddr = struct.pack(">BBH", 0, 1, address[1] ^ (MAGIC >> 16)) + bytes(a ^ b for a, b in zip(host, struct.pack(">I", MAGIC)))
    body = _attr(XOR_MAPPED_ADDRESS, xaddr)
    head = _header(BINDING_SUCCESS, len(body) + 24, tid)
    body += _attr(MESSAGE_INTEGRITY, hmac.new(password.encode("ascii"), head + body, hashlib.sha1).digest())
    head = _header(BINDING_SUCCESS, len(body) + 8, tid)
    body += _attr(FINGERPRINT, struct.pack(">I", (zlib.crc32(head + body) & 0xFFFFFFFF) ^ FINGERPRINT_XOR))
    return _header(BINDING_SUCCESS, len(body), tid) + body
