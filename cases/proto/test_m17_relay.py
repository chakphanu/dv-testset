# SPDX-License-Identifier: MIT
"""M17 stage 2, stream relay between local clients and configured reflector
peers (docs/protocols/m17.md, rows P-M17-61..73): who receives what between
a local client, a listen-only client and the peers M17-DVT / M17-DVU, no echo,
no peer to peer transit, floor handling (61-67), and the one-hop policy and
module trailer (68-73): the 55 B stream with the originating module letter,
the destination rewrite, the bare 54 B body of a one-module peer, a trailer
outside the peer's set.

Judged per side (cases/proto/conftest.py m17_peers): "sut" against the profile
(dvtest/specs, SPEC["m17"]) and "ref" (mrefd) as calibration. A check both
agree on is asserted on both sides; where mrefd differs (module-less
timeout closing packets, the destination rewrite beyond 'M17-', the bare
M17P last byte, any trailer on a one-module peer) the sut is asserted through
the profile entry and the mrefd value is printed. Every egress datagram is
compared byte for byte with what the tester sent (dvtest.m17_relay): size 54
or 55, trailer byte, bytes 0..53, CRC over bytes 0..51, stream id and FN.
Malformed traffic (bare 54 B from a peer, bad trailers, a listen-only
client's stream) is bracketed with the control channel marks.
"""
import os
import threading
import time
import traceback

import pytest

from dvtest import clients, m17_peer, specs
from dvtest import m17_relay as relay
from dvtest.proto import m17

from m17_common import (
    FRAMES, GAP, LISTENER, MOD, OTHER, TAIL, TALKER, clear, connect, done, mark, new, payloads, ready, send,
    send_from_peer, settle,
)

pytestmark = [pytest.mark.proto, pytest.mark.rig]

PEERS = ("dvt", "dvu")
SILENT = 3.0                    # "nothing arrives" window (research D)
TIMEOUT_WAIT = 3.4              # stream timer 1.6 s plus the closing packet and margin


# ---------------------------------------------------------------- plan, sides, rooms

class Plan:
    """The module sets of the rig's two peers (DVT_M17_PEER_*_MODULES) and the
    modules the cases put a client on."""

    def __init__(self, env):
        cfg = m17_peer.peer_env(env)
        self.modules = {key: cfg[key]["modules"] for key in PEERS}
        self.mods = sorted(set("".join(self.modules.values())) | {MOD, OTHER})
        # letters the reflectors under test run: the clients' modules, the
        # peers' modules and DVT_M17_MODULES (the mrefd rig default MNOP)
        self.served = sorted(set(self.mods) | set(env.get("DVT_M17_MODULES") or "MNOP"))

    def serving(self, module):
        return [key for key in PEERS if module in self.modules[key]]

    def shared(self):
        return sorted(set(self.modules["dvt"]) & set(self.modules["dvu"]))


@pytest.fixture(scope="module")
def plan(m17_peers):
    plan = Plan(os.environ)
    if MOD not in plan.modules["dvt"]:
        pytest.skip("M17-DVT does not share module %s (DVT_M17_PEER_DVT_MODULES=%s)" % (MOD, plan.modules["dvt"]))
    return plan


def across(peers, func, *args):
    """{side: func(PeerSet, *args)} with every judged side run in parallel."""
    got, errors = {}, {}

    def run(side):
        try:
            got[side] = func(peers[side], *args)
        except BaseException:                       # reported below with the side name
            errors[side] = traceback.format_exc()

    threads = [threading.Thread(target=run, args=(side,)) for side in peers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, "\n".join("%s: %s" % item for item in errors.items())
    return got


def marked_across(rig, tag, peers, func, *args):
    """across() inside a dvcheck mark window (malformed tester traffic)."""
    mark(rig, "begin", tag)
    try:
        return across(peers, func, *args)
    finally:
        mark(rig, "end", tag)


def judge_spec(rig, nn, got, *keys):
    """The sut value of each key against the profile entry SPEC["m17"][nn]
    (the other sides are printed by the case); skip "spec pending" while the
    entry lacks a key. No-op when the sut side is not judged."""
    if "sut" not in got:
        return
    entry = (specs.rule(rig, "m17") or {}).get(nn) or {}
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    pending = [key for key in keys if key not in entry]
    if pending:
        pytest.skip("spec pending: no m17 %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def each_side(got, check):
    """check(side, result) -> list of problem strings; asserts none on any
    side."""
    problems = ["%s: %s" % (side, p) for side, result in got.items() for p in check(side, result)]
    assert not problems, "; ".join(problems[:6])


class Room:
    """The local clients of one reflector: a and b (talker, listener) and l
    (listen-only) on MOD, one client on every other module of the plan
    (c_N, c_O), all linked."""

    def __init__(self, ip, plan):
        self.ip = ip
        self.named = {"a": new(TALKER, MOD), "b": new(LISTENER, MOD), "l": new(LISTENER, MOD)}
        for module in plan.mods:
            if module != MOD:
                self.named["c_" + module] = new(TALKER, module)
        try:
            assert connect(ip, self.named["l"], m17.lstn_packet(LISTENER, MOD)) == "ACKN 4", \
                "%s: LSTN not acknowledged" % ip
            ready(ip, *[c for name, c in self.named.items() if name != "l"])
            clear(self.named["l"])
        except BaseException:
            self.close()
            raise

    @property
    def a(self):
        return self.named["a"]

    @property
    def all(self):
        return list(self.named.values())

    def on(self, module):
        return {name: c for name, c in self.named.items() if c.module == module}

    def close(self):
        done(*self.all)


def pstream(n=FRAMES, sid=None, src=TALKER, dst=None, last=True, **fields):
    """n stream packets FN 0.. (bit 15 on the last one when last), valid CRC,
    payloads unique to the stream id."""
    sid = sid or clients.fresh_stream_id("m17")
    data = payloads(n, "m17relay-%d" % sid)
    return [m17.stream_packet(sid, i, data[i], src=src, dst=dst, last=last and i == n - 1, **fields)
            for i in range(n)]


def sid_of(packets):
    return relay.stream_id(packets[0])


def talk(room, talker, packets, wait=TAIL):
    send(talker, packets, *[c for c in room.all if c is not talker], wait=wait)


def from_peer(room, link, packets, letter, wait=TAIL):
    send_from_peer(link, packets, letter, *room.all, wait=wait)


def play(room, events, tail=TAIL):
    """Run [(seconds from now, callable)] in order while every client keeps
    draining (a stream from a client and one from a peer at once)."""
    start = time.monotonic()
    for when, action in sorted(events, key=lambda event: event[0]):
        while start + when > time.monotonic():
            for c in room.all:
                c.client.drain()
            time.sleep(0.005)
        action()
    settle(*room.all, seconds=tail)


def client_events(c, packets, start=0.0):
    return [(start + i * GAP, lambda c=c, p=p: c.client.send(c.dest, p)) for i, p in enumerate(packets)]


def peer_events(link, packets, letter, start=0.0):
    return [(start + i * GAP, lambda p=p: link.send(m17.with_trailer(p, letter) if letter is not None else p))
            for i, p in enumerate(packets)]


def who_got(room, ps, sid):
    """{receiver: [datagram, ...]} of the stream datagrams (54 / 55 B) with a
    stream id: clients by name, peers "dvt" / "dvu"."""
    out = {name: relay.group_by_stream(c.client.inbox).get(sid, []) for name, c in room.named.items()}
    for key in PEERS:
        out[key] = relay.group_by_stream(rx.data for rx in getattr(ps, key).datagrams(kind="STREAM")).get(sid, [])
    return out


def delivered(room, ps, packets):
    return sorted(name for name, data in who_got(room, ps, sid_of(packets)).items() if data)


def delivery(sent, got, expect, trailer=True):
    """{receiver: [problem]} for the receivers with a problem: expect maps a
    receiver to None (client: 54 B) or a module letter (peer: 55 B with that
    trailer); a receiver not in expect must have got nothing."""
    out = {}
    for name, data in got.items():
        if name in expect:
            problems = relay.egress_problems(sent, data, expect[name], trailer)
        else:
            problems = ["%d unexpected datagrams" % len(data)] if data else []
        if problems:
            out[name] = problems
    return out


def flat(problems):
    return ["%s: %s" % (name, "; ".join(items)) for name, items in sorted(problems.items())]


def client_expect(room, plan, talker, module):
    """Receivers of a stream a local client sent on a module: the other
    clients on it (54 B) and the peers serving it (55 B)."""
    expect = {name: None for name, c in room.on(module).items() if name != talker}
    expect.update({key: module for key in plan.serving(module)})
    return expect


def peer_expect(room, module):
    """Receivers of a stream a peer sent with a module trailer: every client
    on it, no peer."""
    return {name: None for name in room.on(module)}


def m17p_in(c):
    return [d for d in c.client.inbox if d[:4] == b"M17P"]


def peer_m17p(link):
    return [rx.data for rx in link.datagrams(kind="M17P")]


# ---------------------------------------------------------------- B.b who receives what (61, 62, 65, 68)

def fanout_probe(ps, plan):
    """A talks on MOD, the other module clients on theirs, the listen-only
    client last; collect everywhere."""
    room = Room(ps.address, plan)
    try:
        ps.clear()
        sent = {}
        for name in ["a"] + sorted(n for n in room.named if n.startswith("c_")) + ["l"]:
            c = room.named[name]
            sent[name] = pstream(src=c.callsign)
            talk(room, c, sent[name], wait=0.3)
        settle(*room.all, seconds=SILENT)
        got = {name: who_got(room, ps, sid_of(packets)) for name, packets in sent.items()}
        out = {"r61": [], "r62": [], "r65": [], "r68": []}
        for name, packets in sent.items():
            c = room.named[name]
            if name == "l":
                out["r65"] += flat(delivery(packets, got[name], {}))
                continue
            expect = client_expect(room, plan, name, c.module)
            problems = delivery(packets, got[name], expect, trailer=False)
            out["r61" if name == "a" else "r62"] += ["%s: %s" % (name, p) for p in flat(problems)]
            for key in plan.serving(c.module):
                out["r68"] += ["%s -> %s: %s" % (name, key, p)
                               for p in relay.egress_problems(packets, got[name][key], c.module)
                               + relay.fn_problems(got[name][key])]
        out["heard"] = {name: {r: len(d) for r, d in got[name].items() if d} for name in sent}
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def fanout(rig, m17_peers, plan):
    # the listen-only client's stream is deliberately not relayed: marked
    got = marked_across(rig, "m17-relay-fanout", m17_peers, fanout_probe, plan)
    print("relay fan-out (datagrams heard per receiver):", {s: r["heard"] for s, r in got.items()})
    return got


def test_m17_relay_client_stream_to_all(rig, fanout):
    """P-M17-61 (keep): a stream from a local client on M reaches the other
    clients on M, the listen-only one included, with the body unchanged
    (stream id, FN, payload, SRC; 54 B), and every linked peer serving M; the
    talker, clients on other modules and peers not serving M get nothing."""
    each_side(fanout, lambda side, r: r["r61"])


def test_m17_relay_routes_by_module(rig, fanout):
    """P-M17-62 (keep): a stream on a module other than M reaches the peers
    that serve it and no one else: not the M clients, not a peer without
    that module (a peer without that module gets nothing)."""
    each_side(fanout, lambda side, r: r["r62"])


def test_m17_relay_listen_only_not_relayed(rig, fanout):
    """P-M17-65 (keep): a listen-only client's stream is relayed to no peer
    and no client."""
    each_side(fanout, lambda side, r: r["r65"])


def test_m17_relay_peer_egress_trailer(rig, fanout):
    """P-M17-68 (keep): every stream datagram to a peer is 55 B: the 54 B
    body (valid CRC over bytes 0..51, unchanged) plus the upper case letter
    of the originating module as byte 54, FN consecutive with bit 15 on the
    last only, stream id kept."""
    each_side(fanout, lambda side, r: r["r68"])


# ---------------------------------------------------------------- B.b peer to clients, one hop (63, 64, 72)

def peer_in_probe(ps, plan):
    """Every peer sends a stream per module it shares, then an M17P per peer."""
    room = Room(ps.address, plan)
    try:
        ps.clear()
        sent = {}
        for key in PEERS:
            for module in plan.modules[key]:
                packets = sent[(key, module)] = pstream()
                from_peer(room, getattr(ps, key), packets, module, wait=0.3)
        texts = {}
        for key in PEERS:
            module = plan.modules[key][0]
            texts[key] = m17.m17p_packet(5, b"relay %s\0" % key.encode())
            getattr(ps, key).send(m17.with_trailer(texts[key], module))
            settle(*room.all, seconds=0.3)
        settle(*room.all, seconds=SILENT)
        out = {"r63": [], "r72": [], "r64": [], "r64_m17p": [], "heard": {}}
        for (key, module), packets in sent.items():
            problems = delivery(packets, who_got(room, ps, sid_of(packets)), peer_expect(room, module), trailer=False)
            client_problems = {n: p for n, p in problems.items() if n not in PEERS}
            peer_problems = {n: p for n, p in problems.items() if n in PEERS}
            out["r63" if key == "dvt" else "r72"] += ["%s %s: %s" % (key, module, p) for p in flat(client_problems)]
            out["r64"] += ["%s %s stream: %s" % (key, module, p) for p in flat(peer_problems)]
        for key in PEERS:
            module = plan.modules[key][0]
            for name, c in room.named.items():
                have = [d for d in m17p_in(c) if texts[key][34:] in d]
                out["heard"]["%s m17p -> %s" % (key, name)] = len(have)
                if c.module == module and have != [texts[key]]:
                    out["r64_m17p"].append("%s M17P: %s got %d datagrams, want the 1 unchanged body" % (key, name, len(have)))
            for link_key in PEERS:
                seen = [d for d in peer_m17p(getattr(ps, link_key)) if texts[key][34:] in d]
                if seen:
                    out["r64"].append("%s M17P came back to %s (%d datagrams)" % (key, link_key, len(seen)))
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def peer_in(rig, m17_peers, plan):
    got = across(m17_peers, peer_in_probe, plan)
    print("peer ingress (M17P per receiver):", {s: r["heard"] for s, r in got.items()},
          "modules shared by both peers: %s" % (plan.shared() or "none (the no-transit check is then vacuous)"))
    return got


def test_m17_relay_peer_stream_to_clients(rig, peer_in):
    """P-M17-63 (keep): a stream from DVT (module M, 55 B) reaches every
    local client on M, the listen-only one included, as exactly 54 B (trailer
    stripped) with FN, stream id, payload and SRC unchanged; clients on other
    modules get nothing."""
    each_side(peer_in, lambda side, r: r["r63"])


def test_m17_relay_peer_one_hop(rig, peer_in):
    """P-M17-64 (keep): a datagram from a peer is never forwarded to a peer:
    not to the other peer (also when it serves the module), not back to its
    sender, for streams and for M17P (the M17P still reaches the clients of
    its module, so the silence is not a dead path)."""
    each_side(peer_in, lambda side, r: r["r64"])
    sut_only = {side: r for side, r in peer_in.items() if side == "sut"}
    each_side(sut_only, lambda side, r: r["r64_m17p"])
    print("P-M17-64 M17P to clients:", {side: r["r64_m17p"] or "ok" for side, r in peer_in.items()})


def test_m17_relay_trailer_routes_multi_module_peer(rig, peer_in, plan):
    """P-M17-72 (keep): the trailer routes the stream of a multi-module peer:
    DVU with the letter of each of its modules reaches the clients of that
    module only."""
    if len(plan.modules["dvu"]) < 2:
        pytest.skip("M17-DVU shares one module (DVT_M17_PEER_DVU_MODULES=%s)" % plan.modules["dvu"])
    each_side(peer_in, lambda side, r: r["r72"])


# ---------------------------------------------------------------- B.b floor across origins (66)

def floor_probe(ps, plan):
    """Peer first, client 0.4 s later; then client first, peer 0.4 s later.
    The late stream (30 packets, ends before the first one) must not be
    mixed in, nor relayed after it: B1 / B2."""
    room = Room(ps.address, plan)
    out = {"peer_first": [], "client_first": []}
    try:
        ps.clear()
        first, late = pstream(n=50), pstream(n=FRAMES, src=LISTENER)
        play(room, peer_events(ps.dvt, first, MOD) + client_events(room.a, late, 0.4), tail=SILENT)
        got = who_got(room, ps, sid_of(first))
        out["peer_first"] += ["first stream: " + p for p in flat(delivery(first, got, peer_expect(room, MOD), False))]
        out["peer_first"] += ["late stream: " + p for p in flat(delivery(late, who_got(room, ps, sid_of(late)), {}))]
        ps.clear()
        clear(*room.all)
        first, late = pstream(n=50), pstream(n=FRAMES, src=LISTENER)
        play(room, client_events(room.a, first) + peer_events(ps.dvt, late, MOD, 0.4), tail=SILENT)
        got = who_got(room, ps, sid_of(first))
        out["client_first"] += ["first stream: " + p for p in flat(delivery(first, got, client_expect(room, plan, "a", MOD)))]
        out["client_first"] += ["late stream: " + p for p in flat(delivery(late, who_got(room, ps, sid_of(late)), {}))]
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def floor(rig, m17_peers, plan):
    got = across(m17_peers, floor_probe, plan)
    print("floor across origins:", got)
    return got


def test_m17_relay_floor_peer_first(rig, floor):
    """P-M17-66 (keep): while a peer stream runs on M, a local client's
    stream 400 ms later (other stream id) is not mixed in and not relayed
    to peers or clients; the peer stream reaches every client complete."""
    each_side(floor, lambda side, r: r["peer_first"])


def test_m17_relay_floor_client_first(rig, floor):
    """P-M17-66 (keep): while a local client's stream runs on M, a peer
    stream 400 ms later is not relayed to clients or peers; the client's
    stream reaches the other clients and the peer complete."""
    each_side(floor, lambda side, r: r["client_first"])


# ---------------------------------------------------------------- B.b last frame and timeout (67)

def relayed_after_close(sent, got):
    """A stream sent 100 ms after the previous one's last frame reached a
    receiver: every packet but possibly its FN 0 (as P-M17-22)."""
    bodies = [d[:relay.BODY] for d in got]
    return bool(bodies) and len(bodies) >= len(sent) - 1 and bodies == sent[len(sent) - len(bodies):]


def last_frame_probe(ps, plan):
    room = Room(ps.address, plan)
    try:
        ps.clear()
        s1, s2, s3 = pstream(), pstream(), pstream(src=LISTENER)
        talk(room, room.a, s1, wait=0.1)                        # 100 ms after the last frame:
        from_peer(room, ps.dvt, s2, MOD, wait=0.1)              #   a peer stream, then
        talk(room, room.named["b"], s3, wait=SILENT)            #   another client's stream
        g1, g2, g3 = (who_got(room, ps, sid_of(s)) for s in (s1, s2, s3))
        out = {"peer_got_last": [], "peer_stream_after": [], "client_stream_after": []}
        # s1: every peer serving MOD gets it whole, bit 15 on the final datagram only
        for key in plan.serving(MOD):
            out["peer_got_last"] += ["%s: %s" % (key, p) for p in
                                     relay.egress_problems(s1, g1[key], MOD) + relay.fn_problems(g1[key])]
        out["peer_got_last"] += ["%s got s1" % k for k in PEERS if k not in plan.serving(MOD) and g1[k]]
        # s2 (peer, 100 ms after): the clients on MOD, no peer
        out["peer_stream_after"] += ["%s got %d" % (n, len(g2[n])) for n in room.on(MOD) if not relayed_after_close(s2, g2[n])]
        out["peer_stream_after"] += ["%s got the peer stream" % k for k in PEERS if g2[k]]
        # s3 (client b): the other clients on MOD and every peer serving MOD
        for name in room.on(MOD):
            if name != "b" and not relayed_after_close(s3, g3[name]):
                out["client_stream_after"].append("%s got %d" % (name, len(g3[name])))
        for key in PEERS:
            if key in plan.serving(MOD):
                if not relayed_after_close(s3, g3[key]):
                    out["client_stream_after"].append("%s got %d" % (key, len(g3[key])))
            elif g3[key]:
                out["client_stream_after"].append("%s got a stream of a module it lacks" % key)
        return out
    finally:
        room.close()


def test_m17_relay_last_frame_frees_module(rig, m17_peers, plan):
    """P-M17-67 (keep, bit 15): the last-frame bit reaches the peer as the
    stream's final datagram (and only that one), and frees the module: a
    peer stream 100 ms later is relayed to the clients, and a client stream
    100 ms after that to the other clients and the peer."""
    got = across(m17_peers, last_frame_probe, plan)
    print("P-M17-67 bit 15:", got)
    each_side(got, lambda side, r: ["peer final packet: " + p for p in r["peer_got_last"]]
              + ["peer stream after close: " + p for p in r["peer_stream_after"]]
              + ["client stream after close: " + p for p in r["client_stream_after"]])


def _same(counts):
    """The one count when all are equal, else the list (for the profile entry)."""
    return counts[0] if counts and len(set(counts)) == 1 else counts


def timeout_probe(ps, plan):
    """A client stream and then a peer stream that stop without bit 15."""
    room = Room(ps.address, plan)
    try:
        ps.clear()
        out = {}
        cs = pstream(last=False)
        talk(room, room.a, cs, wait=0.1)
        settle(*room.all, seconds=TIMEOUT_WAIT)
        g = who_got(room, ps, sid_of(cs))
        n = len(cs)
        serving = plan.serving(MOD)
        out["client_stream_data_to_peer"] = ["%s: %s" % (k, p) for k in serving
                                             for p in relay.egress_problems(cs, g[k][:n], MOD, True)]
        extra = {name: data[n:] for name, data in g.items() if name != "a"}
        out["timeout_closing_to_peers"] = _same([len(extra[k]) for k in serving])
        out["timeout_closing_to_clients"] = _same([len(extra[k]) for k in ("b", "l")])
        out["closing_problems"] = (["%s: %s" % (k, p) for k in serving for e in extra[k][:1]
                                    for p in relay.closing_problems(cs[-1], e, MOD)]
                                   + ["%s: %s" % (k, p) for k in ("b", "l") for e in extra[k][:1]
                                      for p in relay.closing_problems(cs[-1], e)])
        out["closing_after_s"] = {}
        for k in serving:
            last_data = [rx for rx in getattr(ps, k).datagrams(kind="STREAM") if relay.stream_id(rx.data) == sid_of(cs)]
            if len(last_data) > n:
                out["closing_after_s"][k] = round(last_data[-1].t - last_data[n - 1].t, 2)
        out["a_got"] = len(g["a"])
        out["non_serving_peers_got"] = sorted(k for k in PEERS if k not in serving and g[k])
        # a peer stream that stops
        ps.clear()
        clear(*room.all)
        pp = pstream(last=False)
        from_peer(room, ps.dvt, pp, MOD, wait=0.1)
        settle(*room.all, seconds=TIMEOUT_WAIT)
        g = who_got(room, ps, sid_of(pp))
        out["peer_stream_data_to_clients"] = [p for name, c in room.on(MOD).items()
                                              for p in relay.egress_problems(pp, g[name][:n], None, True)]
        extra = {name: data[n:] for name, data in g.items()}
        clients_on_m = list(room.on(MOD))
        out["peer_timeout_closing_to_clients"] = _same([len(extra[k]) for k in clients_on_m])
        out["peer_timeout_closing_to_peers"] = _same([len(extra[k]) for k in PEERS])
        out["peer_closing_problems"] = ["%s: %s" % (k, p) for k in clients_on_m for e in extra[k][:1]
                                        for p in relay.closing_problems(pp[-1], e)]
        out["others_got"] = sorted(k for k in g if k not in clients_on_m and g[k])
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def timeouts(rig, m17_peers, plan):
    got = across(m17_peers, timeout_probe, plan)
    print("stream timeout closing packets:", got)
    return got


def test_m17_relay_timeout_client_stream(rig, timeouts):
    """P-M17-67 (dev, Q7): a local client's stream that stops without bit 15:
    the stream's packets reach the peer unchanged and the peer is NOT given
    a different stream; whether the peer (and the clients) get a closing
    packet is the profile's: DV-Router sends one synthesised last packet,
    55 B with the module byte, FN = last + 1 with bit 15, valid CRC, about
    1.6 s after the last packet; mrefd sends none."""
    for side, r in timeouts.items():
        assert not r["client_stream_data_to_peer"], "%s: %s" % (side, r["client_stream_data_to_peer"])
        assert not r["non_serving_peers_got"], "%s: peers without %s got the stream: %s" % (
            side, MOD, r["non_serving_peers_got"])
        assert not r["a_got"], "%s: echo to the talker" % side
        assert not r["closing_problems"], "%s: closing packet: %s" % (side, r["closing_problems"])
        for key, seconds in r["closing_after_s"].items():
            assert 1.0 <= seconds <= 2.6, "%s: %s closing %s s after the last packet" % (side, key, seconds)
    judge_spec(rig, "P-M17-67", timeouts, "timeout_closing_to_peers", "timeout_closing_to_clients")


def test_m17_relay_timeout_peer_stream(rig, timeouts):
    """P-M17-67 (dev, Q7): a peer's stream that stops without bit 15: the
    clients get the stream and, on DV-Router, one synthesised last packet
    (54 B, FN = last + 1 with bit 15, valid CRC); NO peer gets anything (one
    hop), the sender included. mrefd sends no closing packet."""
    for side, r in timeouts.items():
        assert not r["peer_stream_data_to_clients"], "%s: %s" % (side, r["peer_stream_data_to_clients"])
        assert not r["peer_closing_problems"], "%s: closing packet: %s" % (side, r["peer_closing_problems"])
        assert not r["others_got"], "%s: unexpected receivers %s" % (side, r["others_got"])
        assert r["peer_timeout_closing_to_peers"] == 0, "%s: a peer got a closing packet" % side
    judge_spec(rig, "P-M17-67", timeouts, "peer_timeout_closing_to_clients", "peer_timeout_closing_to_peers")


# ---------------------------------------------------------------- B.c destination rewrite (69)

# name: (destination text, rewritten to broadcast by both reflectors, or None
# when they differ: the profile entry decides for the sut, mrefd is printed)
FORMS = {
    "m17_module": ("M17-DVT M", "rewritten"),
    "m17_bare": ("M17-DVT", "rewritten"),
    "broadcast": (None, "unchanged"),
    "callsign": ("N0TEST", "unchanged"),
    "xlx_module": ("XLX999  M", None),
    "xlx_other": ("XLX123  M", None),
    "urf_module": ("URF999  M", None),
}


def state_of(sent, got, letter):
    if not relay.egress_problems(sent, got, letter):
        return "unchanged"
    if not relay.egress_problems([relay.with_dst(p, None) for p in sent], got, letter):
        return "rewritten"
    return "other: " + "; ".join(relay.egress_problems(sent, got, letter)[:2])


def rewrite_probe(ps, plan):
    room = Room(ps.address, plan)
    out = {}
    try:
        ps.clear()
        for form, (dst, _) in FORMS.items():
            clear(*room.all)
            ps.clear()
            from_client = pstream(dst=dst)
            talk(room, room.a, from_client, wait=0.3)
            from_peer_pk = pstream(dst=dst)
            from_peer(room, ps.dvt, from_peer_pk, MOD, wait=0.3)
            g1, g2 = who_got(room, ps, sid_of(from_client)), who_got(room, ps, sid_of(from_peer_pk))
            paths = {"client_to_client": state_of(from_client, g1["b"], None),
                     "client_to_peer": state_of(from_client, g1["dvt"], MOD),
                     "peer_to_client": state_of(from_peer_pk, g2["b"], None)}
            states = set(paths.values())
            out[form] = {"state": states.pop() if len(states) == 1 else "mixed", "paths": paths}
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def rewrite(rig, m17_peers, plan):
    got = across(m17_peers, rewrite_probe, plan)
    print("destination forms on the peer path:", {s: {f: v["paths"] for f, v in r.items()} for s, r in got.items()})
    return got


@pytest.mark.parametrize("form", [f for f, (_, agreed) in FORMS.items() if agreed])
def test_m17_relay_destination_rewrite(rig, rewrite, form):
    """P-M17-69 (keep): a legacy reflector destination (`M17-DVT M`, also
    without the module letter) becomes broadcast with the CRC recomputed, on
    the client path and the peer path both ways; broadcast and a callsign
    are untouched."""
    want = FORMS[form][1]
    each_side(rewrite, lambda side, r: [] if r[form]["state"] == want else
              ["%s: %s, want %s (%s)" % (form, r[form]["state"], want, r[form]["paths"])])


@pytest.mark.parametrize("form", [f for f, (_, agreed) in FORMS.items() if not agreed])
def test_m17_relay_destination_rewrite_xlx_urf(rig, rewrite, form):
    """P-M17-69 (dev, Q8): `XLXnnn  C` and `URFnnn  C` are rewritten to
    broadcast with a recomputed CRC by DV-Router; mrefd rewrites only the
    `M17-` prefix (its value is printed)."""
    got = {side: {"state_" + form: r[form]["state"]} for side, r in rewrite.items()}
    print("P-M17-69 %s:" % form, got)
    for side, r in rewrite.items():
        assert r[form]["state"] in ("rewritten", "unchanged"), "%s: %s" % (side, r[form])
    judge_spec(rig, "P-M17-69", got, "state_" + form)


# ---------------------------------------------------------------- B.c bare 54 B body (70, 71)

def bare_one_probe(ps, plan):
    """DVT (one module) without trailer: a 54 B stream, then M17P."""
    room = Room(ps.address, plan)
    try:
        ps.clear()
        out = {}
        stream54 = pstream()
        from_peer(room, ps.dvt, stream54, None, wait=SILENT)
        out["stream"] = flat(delivery(stream54, who_got(room, ps, sid_of(stream54)), peer_expect(room, MOD), trailer=False))
        clear(*room.all)
        packets = {"nonletter": relay.m17p_with_low_byte(False, data=b"bare-a\0"),
                   "letter": relay.m17p_with_low_byte(ord(MOD), data=b"bare-b\0"),
                   "control": relay.m17p_with_low_byte(False, data=b"bare-c\0")}
        for name in ("nonletter", "letter"):
            ps.dvt.send(packets[name])
            settle(*room.all, seconds=0.3)
        ps.dvt.send(m17.with_trailer(packets["control"], MOD))
        settle(*room.all, seconds=SILENT)
        for name, tag in (("nonletter", b"bare-a"), ("letter", b"bare-b"), ("control", b"bare-c")):
            seen = {n: [d for d in m17p_in(c) if tag in d] for n, c in room.named.items()}
            out[name] = sum(1 for n, c in room.named.items() if seen[n] and c.module == MOD)
            out[name + "_sizes"] = sorted({len(d) for items in seen.values() for d in items})
            out[name + "_elsewhere"] = sorted(n for n, items in seen.items() if items and room.named[n].module != MOD)
        out["m_clients"] = len(room.on(MOD))
        out["control_exact"] = all(d == packets["control"] for c in room.named.values() for d in m17p_in(c)
                                   if b"bare-c" in d)
        out["m17p_to_peers"] = len(peer_m17p(ps.dvu)) + len(peer_m17p(ps.dvt))
        out["bare_m17p_delivered"] = out["nonletter"]
        out["packet_len"] = len(packets["nonletter"])
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def bare_one(rig, m17_peers, plan):
    got = marked_across(rig, "m17-relay-bare-one", m17_peers, bare_one_probe, plan)
    print("bare datagrams from the one-module peer:", got)
    return got


def test_m17_relay_bare_stream_one_module_peer(rig, bare_one):
    """P-M17-70 (keep): a peer with exactly one configured module (DVT, M)
    may send the 54 B stream without trailer: it reaches every M client as 54
    B, unchanged, and no other receiver."""
    each_side(bare_one, lambda side, r: r["stream"])


def test_m17_relay_bare_m17p_one_module_peer(rig, bare_one):
    """P-M17-70 (dev, Q9): a bare M17P from the one-module peer loses its
    last byte as the trailer: with a payload CRC whose low byte is not a
    module letter DV-Router drops it (mrefd strips the byte and relays the
    rest); the same packet with a real trailer is delivered unchanged to
    every M client, and a bare one whose last byte is the letter M is
    recorded."""
    print("P-M17-70 bare M17P (low byte not a letter):",
          {side: {k: v for k, v in r.items() if k in ("nonletter", "nonletter_sizes", "letter", "letter_sizes",
                                                      "control", "control_sizes", "packet_len")}
           for side, r in bare_one.items()})
    for side, r in bare_one.items():
        assert r["control"] == r["m_clients"] and r["control_exact"], \
            "%s: M17P with trailer reached %d of %d M clients (unchanged: %s)" % (
                side, r["control"], r["m_clients"], r["control_exact"])
        assert not r["m17p_to_peers"], "%s: an M17P went to a peer" % side
    judge_spec(rig, "P-M17-70", bare_one, "bare_m17p_delivered")


def bare_two_probe(ps, plan):
    """DVU (two modules) without trailer: CRC 0x0000 and CRC low byte = its
    first module letter; then with the trailer as a control."""
    room = Room(ps.address, plan)
    try:
        ps.clear()
        module = plan.modules["dvu"][-1]
        zero, letter, control = pstream(crc=False), pstream(crc=ord(module)), pstream()
        from_peer(room, ps.dvu, zero, None, wait=0.1)
        from_peer(room, ps.dvu, letter, None, wait=0.1)
        from_peer(room, ps.dvu, control, module, wait=SILENT)
        return {"zero_crc_delivered": delivered(room, ps, zero), "letter_crc_delivered": delivered(room, ps, letter),
                "control_delivered": delivered(room, ps, control),
                "control_problems": flat(delivery(control, who_got(room, ps, sid_of(control)), peer_expect(room, module), False))}
    finally:
        room.close()


@pytest.fixture(scope="module")
def bare_two(rig, m17_peers, plan):
    if len(plan.modules["dvu"]) < 2:
        pytest.skip("M17-DVU shares one module (DVT_M17_PEER_DVU_MODULES=%s)" % plan.modules["dvu"])
    got = marked_across(rig, "m17-relay-bare-two", m17_peers, bare_two_probe, plan)
    print("bare 54 B streams from the two-module peer:", got)
    return got


def test_m17_relay_bare_stream_two_module_peer_dropped(rig, bare_two):
    """P-M17-71 (keep): a bare 54 B stream (CRC 0x0000) from a two-module
    peer is dropped: no client on any module gets it, and no peer; the same
    stream with a trailer, sent after it, is delivered (the peer works)."""
    each_side(bare_two, lambda side, r: (["bare stream delivered to %s" % r["zero_crc_delivered"]]
                                         if r["zero_crc_delivered"] else []) + r["control_problems"])


def test_m17_relay_bare_stream_two_module_peer_letter_crc(rig, bare_two):
    """P-M17-71 (record): the same bare stream whose CRC low byte is a module
    letter of the peer: mrefd reads the module from that byte and delivers
    it, DV-Router needs the trailer; printed, nothing asserted."""
    print("P-M17-71 bare 54 B with the CRC low byte as a module letter delivered to:",
          {side: r["letter_crc_delivered"] for side, r in bare_two.items()})


# ---------------------------------------------------------------- B.c trailer outside the set (73)

def trailer_variants(plan):
    """[(peer, name, trailer)]: for each peer a letter of the rig's served
    set (DVT_M17_MODULES and the clients' modules) that the peer does not
    have (left out when there is none), a letter nobody has, 0x00 and a
    lower case letter, then the control with its own module."""
    out = []
    for key in PEERS:
        own = plan.modules[key]
        foreign = next((m for m in plan.served if m not in own), None)
        if foreign:
            out.append((key, "foreign", foreign))
        out += [(key, "unserved", "Z"), (key, "nul", 0), (key, "lower", own[0].lower()), (key, "control", own[0])]
    return out


def trailer_probe(ps, plan):
    room = Room(ps.address, plan)
    try:
        ps.clear()
        sent = {}
        for key, name, trailer in trailer_variants(plan):
            sent[(key, name)] = pstream()
            from_peer(room, getattr(ps, key), sent[(key, name)], trailer, wait=0.1)
        settle(*room.all, seconds=SILENT)
        out = {"delivered_dvt": [], "delivered_dvu": [], "control_problems": []}
        for (key, name), packets in sent.items():
            heard = delivered(room, ps, packets)
            if name == "control":
                module = plan.modules[key][0]
                out["control_problems"] += ["%s control: %s" % (key, p) for p in flat(
                    delivery(packets, who_got(room, ps, sid_of(packets)), peer_expect(room, module), False))]
            elif heard:
                out["delivered_" + key].append(name)
        return out
    finally:
        room.close()


@pytest.fixture(scope="module")
def trailers(rig, m17_peers, plan):
    got = marked_across(rig, "m17-relay-trailer", m17_peers, trailer_probe, plan)
    print("variants delivered despite a trailer outside the peer's set:", got)
    return got


def test_m17_relay_trailer_outside_set_multi_module_peer(rig, trailers):
    """P-M17-73 (keep, Q10): a stream whose trailer is outside the peer's
    set (a module of the node it does not have, a module nobody has, 0x00, a
    lower case letter) is dropped silently from a multi-module peer; with its
    own letter the stream is delivered."""
    each_side(trailers, lambda side, r: (["DVU variants delivered: %s" % r["delivered_dvu"]] if r["delivered_dvu"] else [])
              + [p for p in r["control_problems"] if p.startswith("dvu")])


def test_m17_relay_trailer_outside_set_one_module_peer(rig, trailers):
    """P-M17-73 (dev, Q10): the same four trailers from the one-module peer
    (DVT, M): DV-Router drops them all (a one-module peer needs its letter
    too); mrefd ignores the byte of a one-module peer and delivers on M."""
    print("P-M17-73 one-module peer, delivered variants:", {side: r["delivered_dvt"] for side, r in trailers.items()})
    for side, r in trailers.items():
        assert not [p for p in r["control_problems"] if p.startswith("dvt")], "%s: %s" % (side, r["control_problems"])
    judge_spec(rig, "P-M17-73", trailers, "delivered_dvt")


# ---------------------------------------------------------------- encrypted streams (86)

ENCRYPTED = {"scrambler": m17.stream_type(encryption=1), "aes": m17.stream_type(encryption=2)}


def encrypted_probe(ps, plan):
    """Per path (client on M, peer DVT on M, client on N, peer DVU on N) an
    unencrypted control stream and one stream per encrypted TYPE."""
    room = Room(ps.address, plan)
    out = {"control_problems": [], "relayed": [], "heard": {}}
    try:
        ps.clear()
        other = next(m for m in plan.modules["dvu"] if m != MOD)
        paths = [("client_" + MOD, "a", MOD, None), ("peer_dvt", None, MOD, "dvt"),
                 ("client_" + other, "c_" + other, other, None), ("peer_dvu", None, other, "dvu")]
        sent = {}
        for path, talker, module, peer in paths:
            for name, type_ in [("control", m17.TYPE_VOICE)] + list(ENCRYPTED.items()):
                packets = sent[(path, name)] = pstream(type_=type_)
                if peer:
                    from_peer(room, getattr(ps, peer), packets, module, wait=0.2)
                else:
                    talk(room, room.named[talker], packets, wait=0.2)
        settle(*room.all, seconds=SILENT)
        for (path, name), packets in sent.items():
            path_talker = next(t for p, t, _, _ in paths if p == path)
            module = next(m for p, _, m, _ in paths if p == path)
            got = who_got(room, ps, sid_of(packets))
            if name == "control":
                expect = (client_expect(room, plan, path_talker, module) if path_talker
                          else peer_expect(room, module))
                out["control_problems"] += ["%s: %s" % (path, p) for p in flat(delivery(packets, got, expect))]
            else:
                heard = sorted(n for n, d in got.items() if d)
                out["heard"]["%s %s" % (path, name)] = heard
                if heard:
                    out["relayed"].append("%s %s -> %s" % (path, name, ",".join(heard)))
        return out
    finally:
        room.close()


def test_m17_relay_encrypted_stream_blocked(rig, m17_peers, plan):
    """P-M17-86 (dev, owner decision): with the default configuration a
    stream with an encrypted TYPE (scrambler, AES) is dropped at ingress and
    relayed to nobody: from a local client to the other clients and to the
    peers, from a peer (valid trailer) to the clients and to the other peer.
    An unencrypted control stream per path proves the path works. mrefd
    blocks them too (EncryptionAllowed is not configured in the rig)."""
    if len(plan.modules["dvu"]) < 1 or not [m for m in plan.modules["dvu"] if m != MOD]:
        pytest.skip("M17-DVU has no module besides %s" % MOD)
    got = marked_across(rig, "m17-relay-encrypted", m17_peers, encrypted_probe, plan)
    print("P-M17-86 encrypted streams heard:", {s: r["heard"] for s, r in got.items()})
    each_side(got, lambda side, r: r["control_problems"])
    judge_spec(rig, "P-M17-86", {s: {"encrypted_relayed": r["relayed"]} for s, r in got.items()},
               "encrypted_relayed")
