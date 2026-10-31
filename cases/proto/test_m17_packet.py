# SPDX-License-Identifier: MIT
"""M17 stage 2, packet mode (docs/protocols/m17.md rows P-M17-74..85): M17P
datagrams between clients and the configured reflector peers M17-DVT and
M17-DVU, which the tester plays (dvtest/m17_peer.py).

Every judged side (cases/proto/conftest.py m17_peers: "sut" = the system under
test with M17 peering, "ref" = mrefd as a calibration reference) is probed in
parallel with the same datagrams. A sweep sends every variant of a family once,
each carrying its own marker in the LSF META field, waits once, and the cases
read the outcome per variant from the module fixtures:

  relay      well formed traffic: TYPE classes, specifiers, destinations, sizes,
             datagrams from peers
  malformed  traffic that is bracketed with the control channel mark (bad CRC,
             stream-bit TYPE, 37 and 860 B, a listen-only sender)
  floor      an SMS while a voice stream runs (the stream is not disturbed)
  peer stream  a peer's stream reaches clients as exactly 54 B

The expected datagrams are built by the tester (dvtest/m17_packet.py), never
copied from what arrived: a client gets the M17P body, a peer gets it plus the
module letter of the originating module, a datagram from a peer goes to the
clients of its trailer's module only.

Judged as in m17.md: hb = asserted on both sides; dev = the profile's
SPEC["m17"]["P-M17-NN"][key] decides on the sut (skipped while there is none),
mrefd is held to the value named in the case or only recorded where it differs
by design (RECORD). Every case prints what each side did.
"""
import contextlib
import threading
import time

import pytest

from dvtest import m17_packet as mp, m17_relay as relay, specs
from dvtest.proto import m17

from m17_common import GAP, LISTENER, MOD, OTHER, TALKER, clear, connect, done, mark as control_mark, new, ready, \
    send_from_peer, settle, stream

pytestmark = [pytest.mark.proto, pytest.mark.rig]

RECORD = object()               # print the value of this side, assert nothing
ITEM_GAP = 0.05                 # between two probe datagrams of a sweep
POS_WAIT = 1.0                  # tail of a sweep whose datagrams are all expected
NEG_WAIT = 3.0                  # tail of a sweep with datagrams that must not arrive
SMS = 0x05


def hexid(value):
    return "0x%04X" % value


# ---------------------------------------------------------------- judging

def keep(nn, got, want, ref=None):
    """hb: every side's value equals want; the ref side (mrefd) equals ref
    when given (RECORD: only printed)."""
    print(nn, got)
    for side, value in got.items():
        expected = ref if side == "ref" and ref is not None else want
        if expected is RECORD:
            continue
        assert value == expected, "%s %s: got %r, want %r" % (nn, side, value, expected)


def decided(rig, nn, key, got, ref):
    """dev: the sut's value is SPEC["m17"][nn][key] (skip "spec pending"
    while there is none); the ref side (mrefd) equals ref (RECORD: printed)."""
    print(nn, key, got)
    entry = (specs.rule(rig, "m17") or {}).get(nn) or {}
    pending = False
    for side, value in got.items():
        if side == "sut":
            if key not in entry:
                pending = True
                continue
            assert value == entry[key], "%s %s: sut %r, spec %r" % (nn, key, value, entry[key])
        elif ref is not RECORD:
            assert value == ref, "%s %s: %s %r, want %r" % (nn, key, side, value, ref)
    if pending:
        pytest.skip("spec pending: no m17 %s [%r] entry in dvtest/specs/%s.py" % (nn, key, rig.sut_profile))


# ---------------------------------------------------------------- endpoints

class Crew:
    """The clients of one side: a, b and l (listen-only) on MOD, c and d on
    OTHER, all linked."""

    def __init__(self, ip, made):
        self.ip, self.clients = ip, made
        self.all = list(made.values())

    def topology(self, peers):
        return mp.topology({name: c.module for name, c in self.clients.items()},
                           {"dvt": peers.dvt.modules, "dvu": peers.dvu.modules})


@contextlib.contextmanager
def crew(ip):
    made = {"a": new(TALKER, MOD), "b": new(LISTENER, MOD), "l": new(LISTENER, MOD),
            "c": new(LISTENER, OTHER), "d": new(TALKER, OTHER)}
    try:
        reply = connect(ip, made["l"], m17.lstn_packet(LISTENER, MOD))
        assert reply == "ACKN 4", "%s: LSTN -> %s" % (ip, reply)
        ready(ip, made["a"], made["b"], made["c"], made["d"])
        clear(made["l"])
        yield Crew(ip, made)
    finally:
        done(*made.values())


def per_side(rig, peers, tag, func, *args):
    """{side: func(PeerSet, *args)} for every judged side in parallel, inside a
    dvcheck mark window named tag (None: no window)."""
    got, errors = {}, {}

    def run(side, peer_set):
        try:
            got[side] = func(peer_set, *args)
        except Exception as exc:                        # reported below
            errors[side] = exc

    if tag:
        control_mark(rig, "begin", tag)
    try:
        threads = [threading.Thread(target=run, args=item) for item in peers.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        if tag:
            control_mark(rig, "end", tag)
    for side in peers:
        assert side not in errors, "%s: %r" % (side, errors[side])
    return got


def m17p_of(datagrams):
    return [d for d in datagrams if d.startswith(b"M17P")]


def collect(cw, peers, since):
    """{leg: [M17P datagram, ...]} for the five client legs and the two
    peers."""
    for c in cw.all:
        c.client.drain()
    legs = {name: m17p_of(c.client.inbox) for name, c in cw.clients.items()}
    for name in mp.PEER_LEGS:
        legs[name] = m17p_of(rx.data for rx in getattr(peers, name).datagrams(since=since))
    return legs


def send_from(cw, peers, origin, data):
    if origin in cw.clients:
        c = cw.clients[origin]
        c.client.send(c.dest, data)
    else:
        getattr(peers, origin).send(data)


# ---------------------------------------------------------------- plans

class Variant:
    def __init__(self, name, origin, body, egress, letter, tag):
        self.name, self.origin, self.body, self.egress, self.letter, self.tag = name, origin, body, egress, letter, tag
        # the datagram that goes on the wire: a peer appends its module letter
        self.sent = m17.with_trailer(body, letter) if letter else body


class Plan:
    """The variants of one sweep, each with its own marker."""

    def __init__(self):
        self.nonce, self.items, self.by_name = mp.new_nonce(), [], {}

    def add(self, name, origin, build, letter=None, egress=None):
        """build(marker) -> the M17P body; egress(body) -> what a client
        must get when it is not the body itself; letter: the trailer a peer
        origin sends."""
        tag = mp.marker(self.nonce, len(self.items))
        body = build(tag)
        item = Variant(name, origin, body, egress(body) if egress else body, letter, tag)
        self.items.append(item)
        self.by_name[name] = item
        return item


def sms(text=b"hello ", **fields):
    return lambda tag: m17.m17p_packet(SMS, text + tag + b"\0", src=TALKER, meta=tag, **fields)


def with_type(type_, text=b"hello "):
    return sms(text, type_=type_)


def sized_packet(size, protocol=mp.RAW, **fields):
    return lambda tag: mp.sized(size, protocol, seed="m17p-%d" % size, src=TALKER, meta=tag, **fields)


TYPE_GROUPS = ("legacy", "v3", "other")
ENCRYPTED_TYPES = m17.M17P_TYPES["legacy_encrypted"] + m17.M17P_TYPES["v3_encrypted"]
SPECIFIERS = {"raw": 0x00, "ax25": 0x01, "aprs": 0x02, "ipv4": 0x04, "winlink": 0x06, "two_bytes": b"\xc2\x80"}
DST_FORMS = {"designation": "M17-DVT M", "designation_no_module": "M17-DVT", "reflector": "XLX999  M",
             "urf": "URF999  M", "callsign": "N0TEST", "broadcast": None}
CLIENT_SIZES = {"c38": (38, mp.RAW), "c38_sms": (38, SMS), "c300": (300, mp.RAW), "c859": (859, mp.RAW)}
PEER_SIZES = (38, 300, 859)


def relay_plan():
    plan = Plan()
    for group in TYPE_GROUPS:
        for type_ in m17.M17P_TYPES[group]:
            plan.add("type_%04X" % type_, "a", with_type(type_))
    for type_ in ENCRYPTED_TYPES:
        plan.add("enc_client_%04X" % type_, "a", with_type(type_, b"enc "))
        plan.add("enc_peer_%04X" % type_, "dvt", with_type(type_, b"enc "), letter=MOD)
    plan.add("type_0000_second_module", "d", with_type(0x0000, b"hello n "))
    for name, protocol in SPECIFIERS.items():
        plan.add("spec_" + name, "a", lambda tag, p=protocol: m17.m17p_packet(p, b"spec " + tag, src=TALKER, meta=tag))
    for name, text in DST_FORMS.items():
        plan.add("dst_" + name, "a", lambda tag, t=text: m17.m17p_packet(
            SMS, b"dst " + tag + b"\0", src=TALKER, dst=t, meta=tag))
    plan.add("dst_peer", "dvt", lambda tag: m17.m17p_packet(
        SMS, b"dst " + tag + b"\0", src=TALKER, dst=DST_FORMS["designation"], meta=tag), letter=MOD)
    plan.add("peer_sms", "dvt", sms(b"from dvt "), letter=MOD)
    plan.add("peer_second_module", "dvu", sms(b"from dvu "), letter=OTHER)
    for name, (size, protocol) in CLIENT_SIZES.items():
        if protocol == SMS:
            plan.add(name, "a", lambda tag: m17.m17p_packet(SMS, b"\0", src=TALKER, meta=tag))
        else:
            plan.add(name, "a", sized_packet(size, protocol))
    for size in PEER_SIZES:
        plan.add("p%d" % size, "dvt", sized_packet(size), letter=MOD)
    return plan


def malformed_plan():
    plan = Plan()
    plan.add("listen_only", "l", sms(b"lstn "))
    for type_ in m17.M17P_TYPES["stream_bit"]:
        plan.add("stream_bit_%04X" % type_, "a", with_type(type_, b"stream bit "))
    plan.add("size_37", "a", sized_packet(37))
    plan.add("size_860", "a", sized_packet(860))
    rewrite = dict(dst=DST_FORMS["designation"])


    def to_broadcast(body):
        return mp.with_dst(body, None)

    plan.add("crc_lsf", "a", sms(b"crc ", crc=(0xDEAD, None)))
    plan.add("crc_payload", "a", sms(b"crc ", crc=(None, 0xBEEF)))
    plan.add("crc_zero", "a", sms(b"crc ", crc=False))
    plan.add("crc_lsf_rewrite", "a", sms(b"crc ", crc=(0xDEAD, None), **rewrite), egress=to_broadcast)
    plan.add("crc_both_rewrite", "a", sms(b"crc ", crc=(0xDEAD, 0xBEEF), **rewrite), egress=to_broadcast)
    return plan


def run_plan(peers, plan, wait):
    with crew(peers.address) as cw:
        peers.clear()
        clear(*cw.all)
        since = time.monotonic()
        for item in plan.items:
            send_from(cw, peers, item.origin, item.sent)
            settle(*cw.all, seconds=ITEM_GAP)
        settle(*cw.all, seconds=wait)
        return {"legs": collect(cw, peers, since), "topo": cw.topology(peers)}


def outcomes(plan, got, name, legs=None):
    """{side: outcome word} of one variant (dvtest.m17_packet.outcome)."""
    item = plan.by_name[name]
    return {side: mp.outcome(g["legs"], mp.expect_legs(g["topo"], item.origin, item.egress, item.letter),
                             item.tag, legs)
            for side, g in got.items()}


def dst_outcomes(plan, got, name):
    """{side: the destination the client copy and the peer copy of a probe
    carry}: "@ALL" when the reflector rewrote it, the sent text when it left
    it alone, else "dropped" / "differs" / what is missing (bytes compared
    with the sent packet rewritten by the tester)."""
    item = plan.by_name[name]
    form = name[len("dst_"):]
    text = DST_FORMS["designation"] if form == "peer" else (DST_FORMS[form] or "@ALL")
    candidates = ("@ALL", text)
    from_peer = item.origin in mp.PEER_LEGS
    out = {}
    for side, g in got.items():
        clients = [d for d in g["legs"]["b"] if item.tag in d]
        copies = [] if from_peer else [d for d in g["legs"]["dvt"] if item.tag in d]
        if len(clients) != 1 or (not from_peer and len(copies) != 1):
            out[side] = "dropped" if not clients and not copies else \
                "copies: clients %d, peer %d" % (len(clients), len(copies))
            continue
        client = mp.which_dst(item.body, clients[0], candidates)
        if from_peer:
            out[side] = client
            continue
        peer = "no trailer" if copies[0][-1:] != MOD.encode() else mp.which_dst(item.body, copies[0][:-1], candidates)
        out[side] = client if client == peer else "client %s / peer %s" % (client, peer)
    return out


@pytest.fixture(scope="module")
def relay_run(rig, m17_peers):
    plan = relay_plan()
    return plan, per_side(rig, m17_peers, "m17-m17p-relay", run_plan, plan, POS_WAIT)


@pytest.fixture(scope="module")
def malformed_run(rig, m17_peers):
    plan = malformed_plan()
    return plan, per_side(rig, m17_peers, "m17-m17p-malformed", run_plan, plan, NEG_WAIT)


# ---------------------------------------------------------------- P-M17-74

@pytest.mark.parametrize("type_", m17.M17P_TYPES["legacy"], ids=hexid)
def test_m17p_legacy_type(relay_run, type_):
    """P-M17-74 (hb): an unencrypted SMS with a legacy TYPE from a client on M
    reaches the other clients of M byte for byte (n B) and the M17-DVT peer as
    n+1 B with the trailer 'M'; nobody else gets it and the sender no echo."""
    keep("P-M17-74 TYPE %s" % hexid(type_), outcomes(*relay_run, "type_%04X" % type_), "relayed")


def test_m17p_legacy_type_second_module(relay_run):
    """P-M17-74 (hb): the same from a client on N: clients of N and M17-DVU
    (trailer 'N'), not M17-DVT and not the clients of M."""
    keep("P-M17-74 second module", outcomes(*relay_run, "type_0000_second_module"), "relayed")


# ---------------------------------------------------------------- P-M17-75

@pytest.mark.parametrize("type_", (0xF000, 0xF030), ids=hexid)
def test_m17p_v3_type(rig, relay_run, type_):
    """P-M17-75 (dev): an unencrypted SMS with a v3 TYPE is relayed exactly as
    a legacy one, the TYPE bytes unchanged (no conversion between layouts)."""
    decided(rig, "P-M17-75", "v3_type", outcomes(*relay_run, "type_%04X" % type_), "relayed")


def test_m17p_v3_type_can5(rig, relay_run):
    """P-M17-75 (dev): TYPE 0xF005 (v3 layout, CAN 5) is relayed unchanged.
    mrefd reads bit 0 of the low byte as the stream bit and drops it."""
    decided(rig, "P-M17-75", "v3_type_can5", outcomes(*relay_run, "type_F005"), RECORD)


def test_m17p_other_layout(rig, relay_run):
    """P-M17-75 (dev): TYPE 0x1000 (neither the legacy nor the v3 layout) is
    relayed natively to clients and peers, never SMS-ingested (Q11)."""
    decided(rig, "P-M17-75", "other_layout", outcomes(*relay_run, "type_1000"), "relayed")


# ---------------------------------------------------------------- P-M17-76

@pytest.mark.parametrize("name", list(SPECIFIERS))
def test_m17p_specifier(relay_run, name):
    """P-M17-76 (hb): a non-SMS type specifier (RAW, AX.25, APRS, IPv4, Winlink
    and a two byte specifier) is relayed like SMS: the relay does not look
    into the payload."""
    keep("P-M17-76 %s" % name, outcomes(*relay_run, "spec_" + name), "relayed")


# ---------------------------------------------------------------- P-M17-77

def test_m17p_from_peer(relay_run):
    """P-M17-77 (hb): an M17P (n+1 B, trailer 'M') from M17-DVT reaches the
    clients of M as the n B body, the last byte being the payload CRC and not
    the module letter, and is not sent to M17-DVU or back to M17-DVT."""
    keep("P-M17-77 from dvt", outcomes(*relay_run, "peer_sms"), "relayed")


def test_m17p_from_peer_second_module(relay_run):
    """P-M17-77 (hb): from M17-DVU with the trailer 'N' it reaches the clients
    of N only (not M, not M17-DVT)."""
    keep("P-M17-77 from dvu", outcomes(*relay_run, "peer_second_module"), "relayed")


# ---------------------------------------------------------------- P-M17-78

def test_m17p_listen_only_sender(malformed_run):
    """P-M17-78 (hb): an M17P from a listen-only client is relayed to nobody
    (no client, no peer)."""
    keep("P-M17-78", outcomes(*malformed_run, "listen_only"), "dropped")


# ---------------------------------------------------------------- P-M17-79

# mrefd rewrites only a destination that starts with "M17-"
MREFD_DST = {"designation": "@ALL", "designation_no_module": "@ALL", "reflector": "XLX999  M", "urf": "URF999  M",
             "callsign": "N0TEST", "broadcast": "@ALL"}


@pytest.mark.parametrize("form", list(DST_FORMS))
def test_m17p_dst_rewrite(rig, relay_run, form):
    """P-M17-79 (dev): M17P destination rule: a reflector designation
    (and, decided for the sut, XLXnnn  C and URFnnn  C) becomes broadcast
    with only the LSF CRC recomputed; the specifier, data and payload CRC
    stay; a callsign or broadcast stays; the client and the peer copy alike."""
    decided(rig, "P-M17-79", form, dst_outcomes(*relay_run, "dst_" + form), MREFD_DST[form])


def test_m17p_dst_rewrite_from_peer(rig, relay_run):
    """P-M17-79 (dev): the same rule for a datagram from a peer: M17-DVT sends
    the destination 'M17-DVT M' and the clients get broadcast with a valid LSF
    CRC."""
    decided(rig, "P-M17-79", "from_peer_designation", dst_outcomes(*relay_run, "dst_peer"), "@ALL")


# ---------------------------------------------------------------- P-M17-80

CRC_REF = {"crc_both_rewrite": RECORD}      # mrefd recomputes the payload CRC too


@pytest.mark.parametrize("name", ["crc_lsf", "crc_payload", "crc_zero", "crc_lsf_rewrite", "crc_both_rewrite"])
def test_m17p_bad_crc_relayed(rig, malformed_run, name):
    """P-M17-80 (dev): an M17P with a wrong LSF CRC, a wrong payload CRC or
    both zero is relayed (no CRC check on the native relay), the bytes
    unchanged; only a designation destination rewrite repairs the LSF CRC
    (never the payload CRC) (Q12)."""
    decided(rig, "P-M17-80", name, outcomes(*malformed_run, name), CRC_REF.get(name, "relayed"))


# ---------------------------------------------------------------- P-M17-81

@pytest.mark.parametrize("type_", ENCRYPTED_TYPES, ids=hexid)
def test_m17p_encrypted_type(rig, relay_run, type_):
    """P-M17-81 (dev): an M17P with an encrypted TYPE (scrambler, AES, legacy
    and v3 layout) is dropped at ingress, from a client and from a peer: it
    reaches no client and no peer (Q13 revised: anything encrypted ends at the
    edge). The unencrypted control M17P of the same sweep (from a client and
    from a peer) must be relayed on every side, so a dead path cannot pass.
    mrefd relays encrypted M17P (printed, not asserted)."""
    plan, runs = relay_run
    control = {"client": outcomes(plan, runs, "type_0000"), "peer": outcomes(plan, runs, "peer_sms")}
    for kind, got in control.items():
        assert got == {side: "relayed" for side in runs}, "control %s M17P: %s" % (kind, got)
    client = outcomes(plan, runs, "enc_client_%04X" % type_)
    peer = outcomes(plan, runs, "enc_peer_%04X" % type_)
    got = {side: client[side] if client[side] == peer[side] else "client %s / peer %s" % (client[side], peer[side])
           for side in runs}
    decided(rig, "P-M17-81", "encrypted_type", got, RECORD)


# ---------------------------------------------------------------- P-M17-82

@pytest.mark.parametrize("type_", m17.M17P_TYPES["stream_bit"], ids=hexid)
def test_m17p_stream_bit_type(malformed_run, type_):
    """P-M17-82 (record): an M17P whose TYPE has the stream bit set is
    invalid; what each side does with it is printed (mrefd drops it). Not
    asserted: only that the reflector keeps relaying afterwards (the other
    variants of the sweep)."""
    got = outcomes(*malformed_run, "stream_bit_%04X" % type_)
    print("P-M17-82 TYPE %s:" % hexid(type_), got)
    plan, runs = malformed_run
    assert outcomes(plan, runs, "crc_zero") == {side: "relayed" for side in runs}, "reflector stopped relaying"


# ---------------------------------------------------------------- P-M17-83

def stream_problems(want, got, letter=None):
    """"ok" or what differs: the stream datagrams a leg got against the
    packets sent (only a missing FN 0 is tolerated, stage 1 judges it)."""
    if got and relay.fn_of(got[0][:54])[0] == 1:
        want = want[1:]
    found = relay.egress_problems(want, got, letter) + relay.fn_problems(got)
    return "; ".join(found) or "ok"


def m17p_problems(got, want):
    return "ok" if got == want else "got %s B, want %s B" % ([len(d) for d in got], [len(d) for d in want])


def streams_of(datagrams):
    return [d for d in datagrams if d[:4] == b"M17 "]


def floor_probe(peers):
    with crew(peers.address) as cw:
        a, b, lo = cw.clients["a"], cw.clients["b"], cw.clients["l"]
        packets = stream(a, src=LISTENER)
        sms_b = m17.m17p_packet(SMS, b"floor b\0", src=LISTENER)
        sms_d = m17.m17p_packet(SMS, b"floor dvt\0", src=LISTENER)
        peers.clear()
        clear(*cw.all)
        talker = threading.Thread(target=a.client.paced, args=(a.dest, packets, GAP, b.client, lo.client))
        start = time.monotonic()
        talker.start()
        time.sleep(0.4)
        b.client.send(b.dest, sms_b)
        time.sleep(max(0.0, start + 0.8 - time.monotonic()))
        peers.dvt.send(m17.with_trailer(sms_d, MOD))
        talker.join()
        settle(*cw.all, seconds=1.0)
        for c in cw.all:
            c.client.drain()
        found = {}
        for name in ("l", "b"):
            found["stream_" + name] = stream_problems(packets, streams_of(cw.clients[name].client.inbox))
        found["stream_dvt"] = stream_problems(
            packets, streams_of(rx.data for rx in peers.dvt.datagrams()), MOD)
        found["no_echo"] = "ok" if not streams_of(a.client.inbox) else "the talker got its own stream"
        found["sms_l"] = m17p_problems(m17p_of(lo.client.inbox), [sms_b, sms_d])
        found["sms_b"] = m17p_problems(m17p_of(b.client.inbox), [sms_d])
        found["sms_a"] = m17p_problems(m17p_of(a.client.inbox), [sms_b, sms_d])
        found["sms_dvt"] = m17p_problems(m17p_of(rx.data for rx in peers.dvt.datagrams()),
                                         [m17.with_trailer(sms_b, MOD)])
        times = {d: t for d, t in zip(lo.client.inbox, lo.client.inbox_times)}
        first = min((t for d, t in times.items() if d[:4] == b"M17 "), default=None)
        last = max((t for d, t in times.items() if d[:4] == b"M17 "), default=None)
        inside = first is not None and all(first < times.get(s, 0) < last for s in (sms_b, sms_d))
        found["interleaved"] = "ok" if inside else "the SMS did not arrive between the first and last stream packet"
        return found


@pytest.fixture(scope="module")
def floor_run(rig, m17_peers):
    return per_side(rig, m17_peers, None, floor_probe)


def test_m17p_does_not_take_the_floor(floor_run):
    """P-M17-83 (hb): while a client streams on M, an SMS from another client
    and one from M17-DVT are relayed to the others and the voice stream goes on
    with consecutive FN, the same stream id and bytes (the SMS never takes the
    floor)."""
    print("P-M17-83:", floor_run)
    for side, found in floor_run.items():
        bad = {key: value for key, value in found.items() if value != "ok"}
        assert not bad, "%s: %s" % (side, bad)


# ---------------------------------------------------------------- P-M17-84

@pytest.mark.parametrize("name,size", [("c38", 38), ("c38_sms", 38), ("c300", 300), ("c859", 859)])
def test_m17p_size_to_clients(relay_run, name, size):
    """P-M17-84 (hb): an M17P of 38 B (the smallest), 300 B and 859 B (the
    largest) from a client reaches the other clients of its module byte for
    byte: no trailer, no truncation."""
    keep("P-M17-84 %d B" % size, outcomes(*relay_run, name, mp.CLIENT_LEGS), "relayed")


def test_m17p_size_37_dropped(rig, malformed_run):
    """P-M17-84 (dev): a 37 B datagram starting with M17P (a specifier and no
    data) is not an M17P and is ignored."""
    decided(rig, "P-M17-84", "client_37", outcomes(*malformed_run, "size_37"), "dropped")


def test_m17p_size_860_from_client_dropped(rig, malformed_run):
    """P-M17-84 (dev): a client M17P of 860 B is outside the 38..859 B a client
    may send and is dropped, not relayed to anybody (Q14). mrefd accepts up to
    860 B from a client (recorded)."""
    decided(rig, "P-M17-84", "client_860", outcomes(*malformed_run, "size_860"), RECORD)


# ---------------------------------------------------------------- P-M17-85

@pytest.mark.parametrize("name,size", [("c38", 38), ("c300", 300), ("c859", 859)])
def test_m17p_size_to_peer(relay_run, name, size):
    """P-M17-85 (hb): between reflectors an M17P is n+1 B: a client's 38 B, 300 B
    and 859 B go to M17-DVT as 39 B, 301 B and 860 B ending in the module
    letter 'M' (the body unchanged), and not to M17-DVU (module not shared)."""
    keep("P-M17-85 %d B to dvt" % size, outcomes(*relay_run, name, mp.PEER_LEGS), "relayed")


@pytest.mark.parametrize("size", PEER_SIZES)
def test_m17p_size_from_peer(relay_run, size):
    """P-M17-85 (hb): from M17-DVT 39 B, 301 B and 860 B (body + trailer) arrive at
    the clients of M as 38 B, 300 B and 859 B (Q14: a peer may send up to
    860 B with the trailer)."""
    keep("P-M17-85 %d B from dvt" % size, outcomes(*relay_run, "p%d" % size), "relayed")


def peer_stream_probe(peers):
    with crew(peers.address) as cw:
        a, c, d = cw.clients["a"], cw.clients["c"], cw.clients["d"]
        out = {}
        for key, letter in (("trailer", MOD), ("bare", None)):
            if letter is None and peers.side != "sut":
                continue                     # mrefd cuts the last byte of a bare datagram
            packets = stream(a, src=LISTENER)
            peers.clear()
            clear(*cw.all)
            send_from_peer(peers.dvt, packets, letter, *cw.all)
            for c_ in cw.all:
                c_.client.drain()
            found = {}
            for name in ("a", "b", "l"):
                found["clients_" + name] = stream_problems(packets, streams_of(cw.clients[name].client.inbox))
            found["other_module"] = "ok" if not streams_of(c.client.inbox + d.client.inbox) else "leaked to N"
            found["no_peer_copy"] = "ok" if not [rx for rx in peers.dvt.datagrams() + peers.dvu.datagrams()
                                                 if rx.data[:4] == b"M17 "] else "sent back to a peer"
            out[key] = found
            time.sleep(0.3)
        return out


@pytest.fixture(scope="module")
def peer_stream_run(rig, m17_peers):
    return per_side(rig, m17_peers, "m17-m17p-peer-stream", peer_stream_probe)


def test_m17_stream_from_peer_to_clients_is_54_bytes(peer_stream_run):
    """P-M17-85 (hb): a stream sent by a peer as 55 B (body + trailer) reaches
    every client of its module as exactly 54 B, byte for byte, with consecutive
    FN, and nowhere else (not the clients of N, not back to a peer)."""
    print("P-M17-85 stream 55 B:", peer_stream_run)
    for side, runs in peer_stream_run.items():
        bad = {key: value for key, value in runs["trailer"].items() if value != "ok"}
        assert not bad, "%s: %s" % (side, bad)


def test_m17_stream_bare_from_peer_to_clients_is_54_bytes(peer_stream_run):
    """P-M17-85 (hb): a bare 54 B stream (no trailer) from the peer that shares
    one module is accepted and reaches the clients as 54 B too (Q9). sut only:
    mrefd takes the last byte of any datagram from a reflector for the
    trailer."""
    got = {side: runs["bare"] for side, runs in peer_stream_run.items() if "bare" in runs}
    print("P-M17-85 stream 54 B:", got)
    if not got:
        pytest.skip("only the sut accepts a bare 54 B stream from a reflector (mrefd cuts the last byte)")
    for side, found in got.items():
        bad = {key: value for key, value in found.items() if value != "ok"}
        assert not bad, "%s: %s" % (side, bad)

