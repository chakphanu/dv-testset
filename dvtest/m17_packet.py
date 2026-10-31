# SPDX-License-Identifier: MIT
"""Pure helpers of the M17 stage 2 packet mode cases (cases/proto/
test_m17_packet.py, docs/protocols/m17.md rows P-M17-74..85): which datagram
every endpoint of a reflector under test has to receive for one M17P
datagram, compared byte for byte with what the tester sent.

Endpoints ("legs"): the tester's clients a, b, l (listen-only) on one module
and c, d on another, plus the fake reflector peers dvt and dvu. A client leg
gets the M17P body (n bytes); a peer leg gets it with the module letter of the
originating module appended (n+1 bytes, [M17-2] "Stream and packet mode data
passed between reflectors"); a packet that came from a peer goes to the
clients on its trailer's module only (one hop: never to the other peer); the
sender never gets an echo.

outcome() reduces a run to one word, "relayed" (every leg got exactly the
expected datagrams), "dropped" (nobody got anything of it) or "partial: ..."
with what differs, so a case asserts a word and a failure says what happened.
"""
import random

from .audio import signals
from .proto import m17

LEGS = ("a", "b", "l", "c", "d", "dvt", "dvu")
CLIENT_LEGS = ("a", "b", "l", "c", "d")
PEER_LEGS = ("dvt", "dvu")
BODY_MIN, BODY_MAX = 38, 859            # M17P datagram sizes, payload 4..825 B
RAW = 0x00                              # type specifier: raw data, no format rules


def new_nonce():
    """4 hex characters that make the markers of one run unique."""
    return "%04x" % random.SystemRandom().randrange(1 << 16)


def marker(nonce, index):
    """7 ASCII bytes, put in the LSF META field (and the data) of a probe
    packet so the datagram can be found on every leg whatever else arrives."""
    return ("%s%03d" % (nonce, index)).encode()


def with_dst(packet, dst):
    """packet (an M17P datagram) with its destination replaced (dst as
    m17.address() takes it) and the LSF CRC recomputed over bytes 4..31; the
    payload CRC and everything else stay as they are. What a reflector must
    send for a destination that looks like a reflector designation."""
    out = bytearray(packet)
    out[4:10] = m17.address(dst)
    out[32:34] = m17.crc16(bytes(out[4:32])).to_bytes(2, "big")
    return bytes(out)


def which_dst(body, datagram, candidates):
    """The candidate destination (as with_dst() takes it, "@ALL" is
    broadcast) for which datagram is exactly body rewritten to it, else
    "differs"."""
    for candidate in candidates:
        if datagram == with_dst(body, candidate):
            return candidate
    return "differs"


def sized(size, protocol=RAW, seed="m17p", **fields):
    """An M17P datagram of exactly size bytes (valid CRCs, PRNG data of
    seed): size 37 is a specifier only, 38 one data byte, 859 the largest
    payload. fields go to m17.m17p_packet."""
    data_len = size - m17.m17p_size(0)
    if data_len < 0:
        raise ValueError("an M17P datagram has at least %d bytes" % m17.m17p_size(0))
    packet = m17.m17p_packet(protocol, signals.prng_bytes(seed, data_len), **fields)
    assert len(packet) == size
    return packet


def topology(clients, peers):
    """{"clients": {leg: module letter}, "peers": {leg: modules shared}}."""
    return {"clients": dict(clients), "peers": dict(peers)}


def expect_legs(topo, origin, egress, letter=None):
    """{leg: [datagram, ...]} for every leg of topo when origin sends a
    packet whose client-side form is egress. origin: a client leg (its
    module is the client's) or a peer leg (letter: the trailer it sends)."""
    clients, peers = topo["clients"], topo["peers"]
    module = clients[origin] if origin in clients else letter
    want = {leg: [] for leg in list(clients) + list(peers)}
    for leg, mod in clients.items():
        if leg != origin and mod == module:
            want[leg] = [egress]
    if origin in clients:
        for leg, mods in peers.items():
            if module in mods:
                want[leg] = [m17.with_trailer(egress, module)]
    return want


def _sizes(datagrams):
    return "[%s]" % ",".join(str(len(d)) for d in datagrams)


def _differs(want, got):
    for index, (a, b) in enumerate(zip(want, got)):
        if a != b:
            return index
    return min(len(want), len(got))


def problems(seen, want):
    """What differs between the datagrams seen and wanted per leg."""
    out = []
    for leg in want:
        if seen.get(leg, []) == want[leg]:
            continue
        got = seen.get(leg, [])
        text = "%s got %s B, want %s B" % (leg, _sizes(got), _sizes(want[leg]))
        if len(got) == len(want[leg]) == 1 and len(got[0]) == len(want[leg][0]):
            text += " (first difference at byte %d)" % _differs(want[leg][0], got[0])
        out.append(text)
    return "; ".join(out)


def outcome(got, want, mark, legs=None):
    """"relayed" when every leg of want (restricted to legs) got exactly its
    datagrams among those of got that contain mark, "dropped" when no such
    leg got a datagram with mark at all, else "partial: <differences>".
    got: {leg: [datagram, ...]}; datagrams without mark (other probes, late
    traffic) are not looked at."""
    want = {leg: value for leg, value in want.items() if legs is None or leg in legs}
    seen = {leg: [d for d in got.get(leg, []) if mark in d] for leg in want}
    if seen == want:
        return "relayed"
    if not any(seen.values()):
        return "dropped"
    return "partial: " + problems(seen, want)
