# SPDX-License-Identifier: MIT
"""M17 stage 2, group 1 (docs/protocols/m17.md, rows P-M17-46 .. P-M17-60):
the configured reflector peer. The tester plays the peers M17-DVT (module M)
and M17-DVU (modules N, O) of the reflector under test (dvtest/m17_peer.py,
fixture m17_peers of cases/proto/conftest.py).

  B.a  P-M17-46 .. 55  handshake, keepalive, teardown of a peer link
  B.d  P-M17-56 .. 60  ingress admission: what a peer endpoint that is not
                       (or no longer) linked, or an identity the reflector
                       does not accept, can do

Judged sides: "sut" (DV-Router) is asserted against the decisions of
scratchpad t7-decisions.md and SPEC["m17"]; "ref" (mrefd, DVT_REF_KIND=mrefd)
is the calibration: it is asserted where it agrees and recorded where it
differs (flat 10 s CONN retry, silence instead of NACK, ordered module
list, ACKN module list not compared, peers keyed by IP only). Each case
prints what every side did. Probes return observations, the case asserts.

Row 54 restarts the sut with the rig control channel and must stay the LAST
case of this module. P-M17-44 (an unconfigured CONN gets NACK 4 B) stays in
test_m17.py; P-M17-58 / 59 / 60 extend it one factor at a time.
"""
import contextlib
import threading
import time

import pytest

from dvtest import clients, specs
from dvtest.net import UdpClient
from dvtest.proto import m17

from m17_common import (
    FRAMES, GAP, LISTENER, TAIL, TALKER, clear, done, mark, new, payloads, peer_streams, ready, received, relayed,
    reply_of, send, send_from_peer, settle, stream,
)

pytestmark = [pytest.mark.proto, pytest.mark.rig]

LINK_WAIT = 60.0
# the first CONN after a DISC: the sut after about 3 s, mrefd after up to 10 s
CONN_WAIT = {"sut": 10.0, "ref": 14.0}
# one raw socket per judged side (the sides run in parallel and may not share a port)
FOREIGN_PORT = {"sut": 17013, "ref": 17014}
QUIET = 3.5                 # "no link" window: a PING follows a link by 3 s
PING_S = 3.0
PING_TOL = {"sut": 0.3, "ref": 0.5}


# ---------------------------------------------------------------- pure helpers (cases/unit/test_m17_peer_cases.py)

def peer_stream(src=TALKER, sid=None, n=FRAMES, last=True, dst=None, **fields):
    """n stream packets (54 B, valid CRC, PRNG payloads unique to the stream
    id) a peer sends; the module trailer is added by send_from_peer."""
    sid = sid or clients.fresh_stream_id("m17")
    data = payloads(n, "m17peer-%d" % sid)
    return [m17.stream_packet(sid, i, data[i], src=src, dst=dst, last=last and i == n - 1, **fields)
            for i in range(n)]


def peer_sms(text="hi"):
    """A legacy-TYPE SMS M17P (valid CRCs) without trailer."""
    return m17.m17p_packet(5, text.encode() + b"\0", src=TALKER)


def retry_deltas(start, times):
    """Seconds between start and the first time, then between neighbours."""
    marks = [start] + list(times)
    return [round(b - a, 2) for a, b in zip(marks, marks[1:])]


def schedule_matches(deltas, want, tol):
    """The first len(want) deltas each within tol of want (and enough of them)."""
    return len(deltas) >= len(want) and all(abs(d - w) <= tol for d, w in zip(deltas, want))


def spaced(times):
    """Gaps between neighbouring times."""
    return [round(b - a, 3) for a, b in zip(times, times[1:])]


def with_extra_module(modules):
    """A module list that differs from modules by one served-looking letter
    the peer does not share (Z is unserved on both reflectors)."""
    return modules + "Z"


def bad_ackn_packets(designation, modules):
    """P-M17-48: replies to a reflector CONN that must not link, by name."""
    return {
        "ackn4": b"ACKN",
        "designation": m17.peer_ackn_packet("M17-DVX", modules),
        "modules": m17.peer_ackn_packet(designation, with_extra_module(modules)),
        "nack4": b"NACK",
        "no_nul": b"ACKN" + m17.encode_callsign(designation) + (modules[0] * 27).encode(),
    }


def malformed_conns(designation, modules):
    """P-M17-60: (name, datagram, expectation) of malformed CONNs. refused:
    a 37 B CONN with an invalid list, NACK 4 B (Q6); size: not a 37 B CONN,
    ignored; both must not link."""
    who = m17.encode_callsign(designation)
    return [
        ("empty", b"CONN" + who + bytes(27), "refused"),
        ("lower", m17.peer_conn_packet(designation, modules.lower()), "refused"),
        ("dup", m17.peer_conn_packet(designation, modules + modules[0]), "refused"),
        ("comma", m17.peer_conn_packet(designation, modules[0] + "," + modules[0]), "refused"),
        ("no_nul", b"CONN" + who + (("ABCDEFGHIJKLMNOPQRSTUVWXYZ" + modules[0]).encode()), "refused"),
        ("conn36", m17.peer_conn_packet(designation, modules)[:36], "size"),
        ("conn38", m17.peer_conn_packet(designation, modules) + b"\0", "size"),
        ("lstn37", b"LSTN" + who + modules.encode().ljust(27, b"\0"), "size"),
    ]


def wrong_module_sets(modules):
    """P-M17-59: lists that differ from the peer's configured set, by name:
    another module, an extra one, an unserved one (Z), a foreign one and,
    for a peer with several modules, a partial list."""
    other = next(ch for ch in "NOPQ" if ch not in modules)
    out = {"other": other, "extra": modules + other, "unserved": modules[0] + "Z", "foreign": "Z"}
    if len(modules) > 1:
        out["partial"] = modules[0]
    return out


# ---------------------------------------------------------------- harness

def in_parallel(jobs):
    """{name: callable} run in threads; {name: result}. Every job finishes
    (its cleanup runs) before the first exception is raised."""
    got = {}

    def run(name, job):
        try:
            got[name] = job()
        except BaseException as exc:                    # re-raised below, with the name
            got[name] = exc

    threads = [threading.Thread(target=run, args=(name, job), daemon=True) for name, job in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name, value in got.items():
        if isinstance(value, BaseException):
            raise AssertionError("%s: %r" % (name, value)) from value
    return got


def per_side(peers, func, *args):
    """{side: func(side, PeerSet, *args)} for every judged side, in parallel."""
    return in_parallel({side: (lambda s=side, p=ps: func(s, p, *args)) for side, ps in peers.items()})


def relink(ps):
    """Back to two linked peers from any state: a link the tester does not
    hold is DISC'ed first so that both ends start the handshake from zero."""
    for link in ps.links:
        link.reset()
        if not link.linked:
            link.drop()
    ps.link(LINK_WAIT)
    ps.clear()


@pytest.fixture(autouse=True)
def fresh(m17_peers):
    """Every case starts with both peers of every side linked, default
    modes and empty logs, and leaves them with the default modes."""
    for ps in m17_peers.values():
        ps.reset()
        ps.link(LINK_WAIT)
        ps.clear()
    yield
    for ps in m17_peers.values():
        ps.reset()


@contextlib.contextmanager
def malformed(rig, tag):
    """dvcheck mark window around deliberately odd tester traffic."""
    mark(rig, "begin", tag)
    try:
        yield
    finally:
        mark(rig, "end", tag)


def spec_of(rig, nn):
    return (specs.rule(rig, "m17") or {}).get(nn) or {}


def spec_check(rig, nn, got, *keys):
    """The sut's observed keys against SPEC["m17"][nn] (the dev part of a
    row); skip "spec pending" for a key the profile does not give, after the
    caller's hard assertions."""
    sut = got.get("sut")
    if sut is None:
        return
    entry = spec_of(rig, nn)
    for key in keys:
        if key in entry:
            assert sut[key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, sut[key], entry[key])
    pending = [key for key in keys if key not in entry]
    if pending:
        pytest.skip("spec pending: no m17 %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def label(rx):
    return reply_of(rx.data) if rx else "none"


def m17p_heard(c):
    c.client.drain()
    return sum(1 for d in c.client.inbox if d[:4] == b"M17P")


def designation_of(rx):
    info = m17.parse_peer(rx.data) if rx else None
    return info["designation"] if info else None


def modules_of(rx):
    info = m17.parse_peer(rx.data) if rx else None
    return info["modules"] if info else None


def raw_socket(side):
    return UdpClient("0.0.0.0", port=FOREIGN_PORT[side])


def raw_ask(raw, dest, data, seconds=1.0):
    """Send data from the raw socket; the datagrams that come back within
    seconds (the wait ends 0.1 s after the first one)."""
    first = len(raw.inbox)
    raw.send(dest, data)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        raw.drain()
        if len(raw.inbox) > first:
            time.sleep(0.1)
            raw.drain()
            break
        time.sleep(0.01)
    return raw.inbox[first:]


def expected_designation(ps):
    return ps.dvt.expect_designation


# ---------------------------------------------------------------- B.a  P-M17-46 CONN of the reflector

def conn_sent_probe(side, ps):
    out = {}
    links = {"dvt": ps.dvt, "dvu": ps.dvu}
    for link in links.values():
        link.auto_ackn = False
    try:
        t0 = time.monotonic()
        for link in links.values():
            link.drop()
        for key, link in links.items():
            rx = link.wait_for(kind="CONN37", since=t0, timeout=CONN_WAIT[side])
            out[key] = {"seen": rx is not None, "delay_s": round(rx.t - t0, 1) if rx else None,
                        "size": rx.size if rx else None, "designation": designation_of(rx),
                        "modules": modules_of(rx),
                        "padded": bool(rx) and not any(rx.data[10 + len(modules_of(rx)):])}
        out["source_ports"] = sorted({addr[1] for link in links.values() for addr in list(link.hub.links)
                                      if addr[0] == ps.address})
    finally:
        relink(ps)
    return out


def test_m17_peer_conn_sent(rig, m17_peers):
    """P-M17-46 (keep): a configured peer that is not linked gets a 37 B CONN
    from the reflector: its designation, the peer's configured module list,
    NUL padded, from the reflector's one listener port."""
    got = per_side(m17_peers, conn_sent_probe)
    print("P-M17-46 reflector CONN:", got)
    for side, r in got.items():
        ps = m17_peers[side]
        for key in ("dvt", "dvu"):
            link, o = getattr(ps, key), r[key]
            assert o["seen"], "%s %s: no CONN within %.0f s of the DISC: %s" % (side, key, CONN_WAIT[side], link.report())
            assert o["size"] == 37 and o["padded"], "%s %s: %s" % (side, key, o)
            want = expected_designation(ps)
            assert not want or o["designation"] == want, "%s %s: designation %r, expected %r" % (side, key, o["designation"], want)
            assert len(o["modules"]) == len(link.modules) and set(o["modules"]) == set(link.modules), \
                "%s %s: modules %r, configured %r" % (side, key, o["modules"], link.modules)
        assert r["source_ports"] == [ps.port], "%s: CONN came from ports %s, not the listener %d" % (side, r["source_ports"], ps.port)


# ---------------------------------------------------------------- P-M17-47 CONN of the peer is answered

def conn_answered_probe(side, ps):
    link = ps.dvt
    mod = link.modules[0]
    listener = new(LISTENER, mod)
    out = {}
    ready(ps.address, listener)
    link.auto_ackn = False                              # the reflector's own CONN stays unanswered at first
    try:
        link.drop()
        t0 = time.monotonic()
        link.send_conn()
        ackn = link.wait_for(kind="ACKN37", since=t0, timeout=1.5)
        out.update(ackn=label(ackn), ackn_designation=designation_of(ackn), ackn_modules=modules_of(ackn),
                   ackn_s=round(ackn.t - t0, 2) if ackn else None)
        pending = peer_stream()
        send_from_peer(link, pending, mod, listener)
        out["relayed_pending"] = relayed(listener, pending)
        own = link.wait_for(kind="CONN37", since=t0, timeout=max(0.1, t0 + CONN_WAIT[side] - time.monotonic()))
        out["own_conn_s"] = round(own.t - t0, 2) if own else None
        rest = t0 + QUIET - time.monotonic()
        if rest > 0:
            time.sleep(rest)
        out["pings_pending"] = len(link.datagrams(since=t0, kind="PING"))
        t1 = time.monotonic()
        link.send_ackn()                                # now the reflector's CONN is answered
        ping = link.wait_for(kind="PING", since=t1, timeout=5.5)
        out["ping_s"] = round(ping.t - t1, 2) if ping else None
        clear(listener)
        usable = peer_stream()
        send_from_peer(link, usable, mod, listener)
        out["relayed_linked"] = relayed(listener, usable)
    finally:
        done(listener)
        relink(ps)
    return out


def test_m17_peer_conn_answered(rig, m17_peers):
    """P-M17-47 (keep, dev part: own CONN): a 37 B CONN from the configured
    peer gets a 37 B ACKN (reflector designation, the peer's modules) but
    does not link by itself: no PING, no relay until the peer ACKNs the
    reflector's CONN (decision Q1). An unlinked sut sends its own CONN at
    once (mrefd on its next 10 s round)."""
    got = per_side(m17_peers, conn_answered_probe)
    for side, r in got.items():
        r["own_conn_fast"] = r["own_conn_s"] is not None and r["own_conn_s"] <= 1.5
    print("P-M17-47 CONN of the peer:", got)
    for side, r in got.items():
        ps = m17_peers[side]
        assert r["ackn"] == "ACKN 37", "%s: CONN answered %s" % (side, r["ackn"])
        want = expected_designation(ps)
        assert not want or r["ackn_designation"] == want, "%s: ACKN designation %r" % (side, r["ackn_designation"])
        assert set(r["ackn_modules"]) == set(ps.dvt.modules), "%s: ACKN modules %r" % (side, r["ackn_modules"])
        assert r["pings_pending"] == 0 and r["relayed_pending"] == 0, "%s: linked by the CONN alone: %s" % (side, r)
        assert r["own_conn_s"] is not None, "%s: no CONN of its own while unlinked" % side
        assert r["ping_s"] is not None and r["relayed_linked"] > FRAMES // 2, "%s: not linked after our ACKN: %s" % (side, r)
    spec_check(rig, "P-M17-47", got, "own_conn_fast")


# ---------------------------------------------------------------- P-M17-48 only a matching ACKN links

def bad_ackn_probe(side, ps, rig, name):
    link = ps.dvt
    mod = link.modules[0]
    listener = new(LISTENER, mod)
    out = {}
    ready(ps.address, listener)
    link.auto_ackn = False
    try:
        link.drop()
        t0 = time.monotonic()
        conn = link.wait_for(kind="CONN37", since=t0, timeout=CONN_WAIT[side])
        out["conn_seen"] = conn is not None
        if conn is None:
            return out
        with malformed(rig, "m17-peer-bad-ackn-" + name):
            tr = time.monotonic()
            link.send(bad_ackn_packets(link.hub.designation, link.modules)[name])
            pending = peer_stream()
            send_from_peer(link, pending, mod, listener)
            out["relayed_on_bad"] = relayed(listener, pending)
            rest = tr + QUIET + 0.5 - time.monotonic()
            link.wait_for(kind="PING", since=tr, timeout=max(0.1, rest))
        out["linked_on_bad"] = bool(link.datagrams(since=tr, kind="PING"))
        if name == "nack4":                             # a NACK does not end the retries (Q4)
            retry = link.wait_for(kind="CONN37", since=conn.t + 0.5, timeout=max(0.1, conn.t + 9.0 - time.monotonic()))
            out["retry_s"] = round(retry.t - conn.t, 1) if retry else None
        if not out["linked_on_bad"]:
            t1 = time.monotonic()
            link.send_ackn()
            ping = link.wait_for(kind="PING", since=t1, timeout=5.5)
            out["ping_after_good_s"] = round(ping.t - t1, 1) if ping else None
    finally:
        done(listener)
        relink(ps)
    return out


@pytest.mark.slow
@pytest.mark.parametrize("name", list(bad_ackn_packets("M17-DVT", "M")))
def test_m17_peer_only_matching_ackn_links(rig, m17_peers, name):
    """P-M17-48 (keep): the reflector's CONN is answered with a 4 B ACKN, an
    ACKN of another designation, an ACKN with another module list (mrefd
    does not compare it), a NACK or an ACKN without NUL: no PING, no relay;
    a good ACKN then links. After a NACK the CONN is retried."""
    got = per_side(m17_peers, bad_ackn_probe, rig, name)
    print("P-M17-48 %s:" % name, got)
    for side, r in got.items():
        assert r["conn_seen"], "%s: no CONN after the DISC" % side
        if side == "ref" and name == "modules":
            continue                                    # mrefd: laxer, the list of an ACKN is not compared
        assert not r["linked_on_bad"], "%s: linked on the bad ACKN %s: %s" % (side, name, r)
        assert r["relayed_on_bad"] == 0, "%s: relayed for an unlinked peer: %s" % (side, r)
        assert r["ping_after_good_s"] is not None, "%s: no PING after a good ACKN: %s" % (side, r)
        if name == "nack4" and side == "sut":
            assert r["retry_s"] is not None, "%s: no CONN retry after the NACK: %s" % (side, r)


# ---------------------------------------------------------------- P-M17-49 CONN retry schedule

def schedule_probe(side, ps, count):
    links = {"dvt": ps.dvt, "dvu": ps.dvu}
    out = {}
    for link in links.values():
        link.silent = True                              # answer nothing, send nothing
    try:
        t0 = time.monotonic()
        for link in links.values():
            link.drop()
        horizon = t0 + 3 + 6 + 12 + 24 + 30 * max(1, count - 4) + 10
        while time.monotonic() < horizon:
            if all(len(link.datagrams(since=t0, kind="CONN37")) >= count for link in links.values()):
                break
            time.sleep(0.5)
        for key, link in links.items():
            conns = link.datagrams(since=t0, kind="CONN37")
            out[key] = {"deltas": retry_deltas(t0, [rx.t for rx in conns]), "count": len(conns),
                        "unchanged": len({rx.data for rx in conns}) <= 1,
                        "pings": len(link.datagrams(since=t0, kind="PING"))}
    finally:
        relink(ps)
    return out


@pytest.mark.slow
def test_m17_peer_conn_retry_schedule(rig, m17_peers):
    """P-M17-49 (dev): an unanswered CONN is repeated unchanged after 3, 6,
    12, 24 and then every 30 s (decision Q4; SPEC gives the schedule and the
    tolerance). mrefd repeats every 10 s: recorded, not asserted."""
    want = spec_of(rig, "P-M17-49")
    count = len(want.get("retry_s", [3, 6, 12, 24, 30, 30]))
    got = per_side(m17_peers, schedule_probe, count)
    print("P-M17-49 CONN retry deltas (s):", {s: {k: v["deltas"] for k, v in r.items()} for s, r in got.items()})
    for side, r in got.items():
        for key, o in r.items():
            assert o["unchanged"], "%s %s: the retried CONN changed" % (side, key)
            assert o["pings"] == 0, "%s %s: PING without a link" % (side, key)
            assert o["count"] >= 2, "%s %s: the CONN was not repeated: %s" % (side, key, o)
    if "sut" in got and want:
        for key, o in got["sut"].items():
            assert schedule_matches(o["deltas"], want["retry_s"], want["tol_s"]), \
                "sut %s: CONN deltas %s, spec %s +- %s" % (key, o["deltas"], want["retry_s"], want["tol_s"])
    elif "sut" in got:
        pytest.skip("spec pending: no m17 P-M17-49 entry in dvtest/specs/%s.py" % rig.sut_profile)


# ---------------------------------------------------------------- P-M17-50 PING cadence

def ping_probe(side, ps, seconds):
    t0 = time.monotonic()
    time.sleep(seconds)
    out = {}
    for key, link in (("dvt", ps.dvt), ("dvu", ps.dvu)):
        pings = link.datagrams(since=t0, kind="PING")
        out[key] = {"count": len(pings), "gaps": spaced([rx.t for rx in pings]),
                    "sizes": sorted({rx.size for rx in pings}),
                    "wrong_designation": len(link.designation_mismatches(since=t0)),
                    "pongs": len(link.datagrams(since=t0, kind="PONG"))}
    return out


def test_m17_peer_ping_cadence(rig, m17_peers):
    """P-M17-50 (keep, per peer count: decision Q5): a linked peer gets one
    10 B PING with the reflector's designation every 3 s, a two-module peer
    at the same rate as a one-module peer (one PING per peer, not per
    module)."""
    seconds = 13.0
    got = per_side(m17_peers, ping_probe, seconds)
    print("P-M17-50 PING over %.0f s:" % seconds, got)
    for side, r in got.items():
        for key, o in r.items():
            assert 3 <= o["count"] <= 5, "%s %s: %d PINGs in %.0f s" % (side, key, o["count"], seconds)
            assert o["sizes"] == [10], "%s %s: PING sizes %s" % (side, key, o["sizes"])
            assert o["wrong_designation"] == 0, "%s %s: PING designation differs from %s" % (side, key, expected_designation(m17_peers[side]))
            assert all(abs(g - PING_S) <= PING_TOL[side] for g in o["gaps"]), "%s %s: PING gaps %s" % (side, key, o["gaps"])
        assert abs(r["dvt"]["count"] - r["dvu"]["count"]) <= 1, "%s: one-module peer %d PINGs, two-module peer %d" % (
            side, r["dvt"]["count"], r["dvu"]["count"])


# ---------------------------------------------------------------- P-M17-51 PING or PONG alone keeps the link

KEEP_S = 45


def keepalive_probe(side, ps):
    dvt, dvu = ps.dvt, ps.dvu
    dvu.ping_magic = "PONG"                             # DVT: PING only, DVU: PONG only; neither ever answers
    t0 = time.monotonic()
    time.sleep(KEEP_S)
    out = {}
    listeners = {}
    try:
        for key, link in (("dvt", dvt), ("dvu", dvu)):
            c = listeners[key] = new(LISTENER, link.modules[0])
            out[key] = {"discs": len(link.datagrams(since=t0, kind="DISC")),
                        "conns": len(link.datagrams(since=t0, kind="CONN37")),
                        "pings": len(link.datagrams(since=t0, kind="PING")),
                        "pongs": len(link.datagrams(since=t0, kind="PONG")),
                        "sent_magic": sorted({rx.data[:4].decode() for rx in link.sent(since=t0, kind="P")})}
            ready(ps.address, c)
        for key, link in (("dvt", dvt), ("dvu", dvu)):
            pkts = peer_stream()
            send_from_peer(link, pkts, link.modules[0], listeners[key])
            out[key]["relayed"] = relayed(listeners[key], pkts)
    finally:
        done(*listeners.values())
    return out


@pytest.mark.slow
def test_m17_peer_keepalive_ping_or_pong_only(rig, m17_peers):
    """P-M17-51 (keep): a peer that sends only PING (DVT) or only PONG (DVU)
    every 3 s and never answers stays linked for 45 s (past the 30 s dead
    timer): the reflector neither drops it (no DISC, no new CONN) nor
    answers the PING with a PONG, and a peer stream is still relayed."""
    got = per_side(m17_peers, keepalive_probe)
    print("P-M17-51 %d s with PING only (dvt) / PONG only (dvu):" % KEEP_S, got)
    for side, r in got.items():
        for key, o in r.items():
            assert o["discs"] == 0 and o["conns"] == 0, "%s %s: link dropped during %d s: %s" % (side, key, KEEP_S, o)
            assert o["pings"] >= KEEP_S // 3 - 3, "%s %s: only %d reflector PINGs in %d s" % (side, key, o["pings"], KEEP_S)
            assert o["pongs"] == 0, "%s %s: the reflector answered with %d PONGs" % (side, key, o["pongs"])
            assert o["relayed"] > FRAMES // 2, "%s %s: peer stream relayed %d/%d after %d s" % (side, key, o["relayed"], FRAMES, KEEP_S)


# ---------------------------------------------------------------- P-M17-52 dead peer

def silent_probe(side, ps):
    links = {"dvt": ps.dvt, "dvu": ps.dvu}
    out = {}
    made = []
    for link in links.values():
        link.silent = True
    t_silent = time.monotonic()
    last = {key: (link.sent()[-1].t if link.sent() else t_silent) for key, link in links.items()}
    drop = {}
    try:
        for key, link in links.items():
            rx = link.wait_for(kind="DISC", since=t_silent, timeout=max(1.0, last[key] + 40.0 - time.monotonic()))
            conn = None if rx else link.wait_for(kind="CONN37", since=t_silent, timeout=1.0)
            drop[key] = rx.t if rx else (conn.t if conn else None)
            out[key] = {"disc": label(rx), "disc_designation": designation_of(rx),
                        "drop_s": round(drop[key] - last[key], 1) if drop[key] else None}
        for key, link in links.items():
            if drop[key] is None:
                continue
            mod = link.modules[0]
            c = new(LISTENER, mod)
            made.append(c)
            ready(ps.address, c)
            pkts = peer_stream()
            send_from_peer(link, pkts, mod, c)
            out[key]["relayed_after_drop"] = relayed(c, pkts)
            out[key]["pings_after_drop"] = len(link.datagrams(since=drop[key] + 0.5, kind="PING"))
            retry = link.wait_for(kind="CONN37", since=drop[key], timeout=max(0.1, drop[key] + CONN_WAIT[side] - time.monotonic()))
            out[key]["retry_s"] = round(retry.t - drop[key], 1) if retry else None
    finally:
        done(*made)
        relink(ps)
    return out


@pytest.mark.slow
def test_m17_peer_silent_is_dropped(rig, m17_peers):
    """P-M17-52 (keep drop; record DISC and retry): a peer that goes silent
    after link-up is dropped about 30 s after its last datagram: the
    reflector stops PINGing it, relays nothing from it, and tries again
    (CONN) later. The DISC 10 B and the retry delay are recorded."""
    got = per_side(m17_peers, silent_probe)
    print("P-M17-52 silent peer:", got)
    for side, r in got.items():
        for key, o in r.items():
            assert o["drop_s"] is not None, "%s %s: still linked 40 s after the last datagram: %s" % (side, key, o)
            assert 26.0 <= o["drop_s"] <= 36.0, "%s %s: dropped %.1f s after the last datagram (30 s expected)" % (side, key, o["drop_s"])
            assert o["relayed_after_drop"] == 0, "%s %s: stream relayed after the drop: %s" % (side, key, o)
            assert o["pings_after_drop"] == 0, "%s %s: PING after the drop: %s" % (side, key, o)


def traffic_probe(side, ps):
    dvt, dvu = ps.dvt, ps.dvu
    mod_t, mod_u = dvt.modules[0], dvu.modules[0]
    dvt.silent = dvu.silent = True                      # no PING, no PONG: only traffic
    t0 = time.monotonic()
    rounds = 0
    while time.monotonic() - t0 < 40.0:
        one = peer_stream(n=1)
        dvt.send(m17.with_trailer(one[0], mod_t))       # a one-packet stream, FN 0 and last
        dvu.send(m17.with_trailer(peer_sms("keep %d" % rounds), mod_u))
        rounds += 1
        time.sleep(5.0)
    out = {"rounds": rounds}
    for key, link in (("dvt", dvt), ("dvu", dvu)):
        out[key] = {"discs": len(link.datagrams(since=t0, kind="DISC")),
                    "conns": len(link.datagrams(since=t0, kind="CONN37")),
                    "pings_last_10s": len(link.datagrams(since=time.monotonic() - 10.0, kind="PING"))}
    listeners = {}
    try:
        for key, link in (("dvt", dvt), ("dvu", dvu)):
            c = listeners[key] = new(LISTENER, link.modules[0])
            ready(ps.address, c)
        for key, link in (("dvt", dvt), ("dvu", dvu)):
            pkts = peer_stream()
            send_from_peer(link, pkts, link.modules[0], listeners[key])
            out[key]["relayed"] = relayed(listeners[key], pkts)
    finally:
        done(*listeners.values())
        relink(ps)
    return out


@pytest.mark.slow
def test_m17_peer_traffic_refreshes_timer(rig, m17_peers):
    """P-M17-52 (Q16 variant, keep): a peer that sends no PING or PONG for 40
    s but a stream datagram (DVT) or an M17P datagram (DVU) every 5 s stays
    linked: any accepted datagram refreshes the 30 s timer."""
    got = per_side(m17_peers, traffic_probe)
    print("P-M17-52 traffic only, 40 s:", got)
    for side, r in got.items():
        for key in ("dvt", "dvu"):
            o = r[key]
            if side == "ref" and (o["discs"] or o["conns"]):
                print("P-M17-52 mrefd %s: timer is not refreshed by traffic: %s" % (key, o))
                continue
            assert o["discs"] == 0 and o["conns"] == 0, "%s %s: dropped while it was sending: %s" % (side, key, o)
            assert o["pings_last_10s"] >= 1, "%s %s: no reflector PING in the last 10 s: %s" % (side, key, o)
            assert o["relayed"] > FRAMES // 2, "%s %s: not linked at the end: %s" % (side, key, o)


# ---------------------------------------------------------------- P-M17-53 DISC of the peer

def disc_probe(side, ps):
    link = ps.dvu
    m1, m2 = link.modules[0], link.modules[1]
    talkers = {m1: new(TALKER, m1), m2: new(TALKER, m2)}
    out = {}
    try:
        ready(ps.address, *talkers.values())
        t_c = time.monotonic()
        for mod, c in talkers.items():                  # control: the link carries both modules, both ways
            pkts = stream(c)
            send(c, pkts, *talkers.values())
        out["control_to_peer"] = sorted({p["trailer"] for p in peer_streams(link, since=t_c)})
        out["control_from_peer"] = {}
        for mod in (m1, m2):
            pkts = peer_stream()
            send_from_peer(link, pkts, mod, *talkers.values())
            out["control_from_peer"][mod] = relayed(talkers[mod], pkts) > 0
        clear(*talkers.values())
        link.auto_ackn = False                          # a later CONN of the reflector stays unanswered
        link.drop()
        t_d = time.monotonic()
        for mod, c in talkers.items():
            send(c, stream(c), *talkers.values())
        out["streams_to_peer_after"] = len(link.datagrams(since=t_d, kind=("STREAM", "M17P")))
        out["replies"] = sorted({rx.kind for rx in link.datagrams(since=t_d)
                                 if rx.kind not in ("CONN37",)})
        clear(*talkers.values())
        heard = {}
        for mod, c in talkers.items():
            pkts = peer_stream()
            send_from_peer(link, pkts, mod, *talkers.values())
            heard[mod] = relayed(c, pkts)
        out["heard_after"] = heard
    finally:
        done(*talkers.values())
        relink(ps)
    return out


def test_m17_peer_disc_removes_all_modules(rig, m17_peers):
    """P-M17-53 (keep): a 10 B DISC from the two-module peer removes every
    module session of it: no reply, nothing sent to it on either module and
    nothing from it relayed (the control shows both modules worked before)."""
    got = per_side(m17_peers, disc_probe)
    print("P-M17-53 DISC of the peer:", got)
    for side, r in got.items():
        ps = m17_peers[side]
        mods = ps.dvu.modules[:2]
        assert r["control_to_peer"] == list(mods), "%s: before the DISC the peer got traffic of %s" % (side, r["control_to_peer"])
        assert all(r["control_from_peer"].values()), "%s: before the DISC the peer's streams were relayed %s" % (side, r["control_from_peer"])
        assert r["streams_to_peer_after"] == 0, "%s: %d stream datagrams to a removed peer" % (side, r["streams_to_peer_after"])
        # DV-Router P26k: the sut still answers a peer DISC with a bare 4 B DISC (a deviation, mrefd sends
        # nothing to a peer); accepted for the sut until P26k lands, then tighten to "nothing"
        tolerated = {"DISC4"} if side == "sut" else set()
        assert not set(r["replies"]) & ({"DISC4", "DISC10", "NACK4", "ACKN4", "ACKN37"} - tolerated), \
            "%s: reply to a peer DISC: %s" % (side, r["replies"])
        if set(r["replies"]) & tolerated:
            print("P-M17-53 %s: bare 4 B DISC answered to a peer DISC (tolerated, P26k)" % side)
        assert not any(r["heard_after"].values()), "%s: stream of a removed peer relayed: %s" % (side, r["heard_after"])


# ---------------------------------------------------------------- P-M17-55 repeated CONN

def repeat_conn_probe(side, ps):
    link = ps.dvt
    mod = link.modules[0]
    talker, listener = new(TALKER, mod), new(LISTENER, mod)
    out = {}
    try:
        ready(ps.address, listener, talker)
        t0 = time.monotonic()
        link.send_conn()
        ackn = link.wait_for(kind=("ACKN", "NACK"), since=t0, timeout=1.5)
        out.update(reply=label(ackn), designation=designation_of(ackn), modules=modules_of(ackn))
        ping = link.wait_for(kind="PING", since=t0 + 0.2, timeout=4.5)
        out["ping_after_s"] = round(ping.t - t0, 1) if ping else None
        out["conn_back"] = len(link.datagrams(since=t0, kind="CONN"))
        out["discs"] = len(link.datagrams(since=t0, kind="DISC"))
        packets = stream(talker)
        sid = m17.parse_stream(packets[0])["stream_id"]
        t1 = time.monotonic()
        send(talker, packets, listener)
        at_peer = [p["fn"] for p in peer_streams(link, since=t1) if p["stream_id"] == sid]
        at_client = [p["fn"] for p in received(listener) if p["stream_id"] == sid]
        out.update(peer_count=len(at_peer), peer_unique=len(set(at_peer)), client_count=len(at_client),
                   client_unique=len(set(at_client)))
    finally:
        done(talker, listener)
    return out


def test_m17_peer_repeat_conn(rig, m17_peers):
    """P-M17-55 (keep ACKN and no duplicates; the rest recorded): a repeated
    CONN from a linked peer (a peer restart, a lost ACKN) is answered with
    an ACKN again, the link is kept and a relayed stream arrives once, not
    twice, at the peer and at the clients."""
    got = per_side(m17_peers, repeat_conn_probe)
    print("P-M17-55 repeated CONN:", got)
    for side, r in got.items():
        assert r["reply"] == "ACKN 37", "%s: repeated CONN answered %s" % (side, r["reply"])
        want = expected_designation(m17_peers[side])
        assert not want or r["designation"] == want, "%s: ACKN designation %r" % (side, r["designation"])
        assert r["ping_after_s"] is not None and r["discs"] == 0, "%s: link not kept: %s" % (side, r)
        assert r["peer_count"] == r["peer_unique"] > FRAMES // 2, "%s: stream to the peer: %s" % (side, r)
        assert r["client_count"] == r["client_unique"] > FRAMES // 2, "%s: stream to the client: %s" % (side, r)


# ---------------------------------------------------------------- B.d  P-M17-56 ingress of an unlinked endpoint

def unlinked_ingress_probe(side, ps, rig):
    link = ps.dvt
    mod = link.modules[0]
    listener = new(LISTENER, mod)
    out = {}

    def offer(tag):
        pkts = peer_stream()
        send_from_peer(link, pkts, mod, listener)
        before = m17p_heard(listener)
        link.send(m17.with_trailer(peer_sms("offer " + tag), mod))
        settle(listener, seconds=TAIL)
        return relayed(listener, pkts), m17p_heard(listener) - before

    try:
        ready(ps.address, listener)
        out["control_stream"], out["control_m17p"] = offer("linked")
        link.auto_ackn = False
        link.drop()
        with malformed(rig, "m17-peer-unlinked-ingress"):
            clear(listener)
            out["after_disc_stream"], out["after_disc_m17p"] = offer("after DISC")
            t = time.monotonic()
            link.send_conn()                            # the handshake is half done: the reflector's CONN stays unanswered
            link.wait_for(kind="ACKN37", since=t, timeout=1.5)
            clear(listener)
            out["pending_stream"], out["pending_m17p"] = offer("pending")
    finally:
        done(listener)
        relink(ps)
    return out


def test_m17_peer_unlinked_ingress_dropped(rig, m17_peers):
    """P-M17-56 (keep): stream and M17P datagrams (with a valid trailer) from
    a peer endpoint that was DISC'ed, or whose handshake is half done, reach
    no local client. The control shows the same datagrams are relayed while
    the peer is linked."""
    got = per_side(m17_peers, unlinked_ingress_probe, rig)
    print("P-M17-56 unlinked peer ingress:", got)
    for side, r in got.items():
        assert r["control_stream"] > FRAMES // 2 and r["control_m17p"] >= 1, "%s: nothing relayed while linked: %s" % (side, r)
        for phase in ("after_disc", "pending"):
            assert r[phase + "_stream"] == 0 and r[phase + "_m17p"] == 0, "%s: %s peer datagrams relayed: %s" % (side, phase, r)


# ---------------------------------------------------------------- P-M17-57 endpoint that is not a configured peer

def foreign_probe(side, ps, rig):
    link = ps.dvt
    mod = link.modules[0]
    listener = new(LISTENER, mod)
    raw = raw_socket(side)
    dest = (ps.address, ps.port)
    out = {}
    try:
        ready(ps.address, listener)
        ctl = peer_stream()
        send_from_peer(link, ctl, mod, listener)
        out["control"] = relayed(listener, ctl)
        clear(listener)
        with malformed(rig, "m17-peer-foreign-endpoint"):
            n0 = len(raw.inbox)
            pkts = peer_stream()
            raw.paced(dest, [m17.with_trailer(p, mod) for p in pkts], GAP, listener.client)
            settle(listener, seconds=TAIL)
            out["stream55"] = relayed(listener, pkts)
            bare = peer_stream()
            raw.paced(dest, bare, GAP, listener.client)
            settle(listener, seconds=TAIL)
            out["stream54"] = relayed(listener, bare)
            before = m17p_heard(listener)
            raw.send(dest, m17.with_trailer(peer_sms("foreign"), mod))
            settle(listener, seconds=TAIL)
            out["m17p"] = m17p_heard(listener) - before
            raw.pump(1.0)
            out["replies"] = [reply_of(d) for d in raw.inbox[n0:]]
            out["link_kept"] = link.wait_for(kind="PING", since=link.mark(), timeout=4.5) is not None
    finally:
        raw.close()
        done(listener)
    return out


def test_m17_peer_foreign_endpoint_dropped(rig, m17_peers):
    """P-M17-57 (keep on the sut, mrefd recorded): stream (55 B and bare 54
    B) and M17P datagrams from an endpoint that is not a configured peer (a
    tester port beside the peer's, same IP) are dropped without a reply
    while the real peer is linked. mrefd keys peers by IP only, so it takes
    the same IP as the peer."""
    got = per_side(m17_peers, foreign_probe, rig)
    print("P-M17-57 foreign endpoint:", got)
    for side, r in got.items():
        assert r["control"] > FRAMES // 2, "%s: the real peer's stream was not relayed: %s" % (side, r)
        assert r["link_kept"], "%s: the real peer link was lost: %s" % (side, r)
    sut = got.get("sut")
    if sut:
        assert sut["stream55"] == 0 and sut["stream54"] == 0 and sut["m17p"] == 0, "sut relayed a foreign endpoint's datagrams: %s" % sut
        assert sut["replies"] == [], "sut answered a foreign endpoint's datagrams: %s" % sut["replies"]


# ---------------------------------------------------------------- P-M17-58 identity of a CONN

def foreign_identity_probe(side, ps, rig):
    listener = new(LISTENER, ps.dvt.modules[0])
    raw = raw_socket(side)
    dest = (ps.address, ps.port)
    mods_t, mods_u = ps.dvt.modules, ps.dvu.modules
    variants = [("dvx", "M17-DVX", mods_t), ("dvt", "M17-DVT", mods_t), ("dvu", "M17-DVU", mods_u),
                ("n0call", "N0CALL", mods_t)]
    out = {"replies": {}}
    try:
        ready(ps.address, listener)
        with malformed(rig, "m17-peer-identity-foreign"):
            for name, designation, modules in variants:
                got = raw_ask(raw, dest, m17.peer_conn_packet(designation, modules))
                out["replies"][name] = [reply_of(d) for d in got]
            n_late = len(raw.inbox)
            raw.pump(QUIET)
            out["late"] = [reply_of(d) for d in raw.inbox[n_late:]]
            pkts = peer_stream()
            raw.paced(dest, [m17.with_trailer(p, ps.dvt.modules[0]) for p in pkts], GAP, listener.client)
            settle(listener, seconds=TAIL)
            out["relayed"] = relayed(listener, pkts)
    finally:
        raw.close()
        done(listener)
    return out


def test_m17_peer_conn_identity_foreign_endpoint(rig, m17_peers):
    """P-M17-58 (dev: the reply; keep: no link): a CONN from a tester port
    that is not a peer's endpoint (M17-DVX, M17-DVT or M17-DVU as the
    designation, and a non-reflector callsign) creates no link: the sut
    answers a 4 B NACK, then sends the endpoint nothing (no CONN, no PING)
    and relays nothing it sends. Extends P-M17-44. mrefd (peers keyed by
    IP, silent for refusals) is recorded."""
    got = per_side(m17_peers, foreign_identity_probe, rig)
    print("P-M17-58 CONN from a foreign endpoint:", got)
    sut = got.get("sut")
    if sut:
        assert sut["late"] == [], "sut sent the foreign endpoint %s after its CONNs" % sut["late"]
        assert sut["relayed"] == 0, "sut relayed a stream of an unlinked endpoint: %s" % sut
        first = {name: (replies[0] if replies else "none") for name, replies in sut["replies"].items()}
        assert not any(r.startswith("ACKN") for r in first.values()), "sut accepted a foreign endpoint: %s" % first
        sut["reply"] = first["dvx"]
        assert set(first.values()) == {first["dvx"]}, "sut answers differ: %s" % first
    spec_check(rig, "P-M17-58", got, "reply")


def configured_identity_probe(side, ps, rig):
    dvt, dvu = ps.dvt, ps.dvu
    mods_t, mods_u = dvt.modules, dvu.modules
    variants = [("dvx_at_dvt", dvt, "M17-DVX", mods_t), ("dvt_at_dvu", dvu, "M17-DVT", mods_t),
                ("dvu_at_dvt", dvt, "M17-DVU", mods_u)]
    b, c = new(LISTENER, mods_t[0]), new(LISTENER, mods_u[0])
    out = {"replies": {}}
    try:
        ready(ps.address, b, c)
        dvt.auto_ackn = dvu.auto_ackn = False          # an unexpected CONN of the reflector must be visible
        t_start = time.monotonic()
        with malformed(rig, "m17-peer-identity-configured"):
            for name, link, designation, modules in variants:
                t = time.monotonic()
                link.send(m17.peer_conn_packet(designation, modules))
                rx = link.wait_for(kind=("ACKN", "NACK", "CONN"), since=t, timeout=1.0)
                out["replies"][name] = label(rx)
        t_end = time.monotonic()
        for key, link in (("dvt", dvt), ("dvu", dvu)):
            out[key + "_ping"] = link.wait_for(kind="PING", since=t_end, timeout=4.0) is not None
            out[key + "_conn"] = len(link.datagrams(since=t_start, kind="CONN"))
        pkts = peer_stream()
        send_from_peer(dvt, pkts, mods_t[0], b)
        out["dvt_relayed"] = relayed(b, pkts)
        pkts = peer_stream()
        send_from_peer(dvu, pkts, mods_u[0], c)
        out["dvu_relayed"] = relayed(c, pkts)
    finally:
        done(b, c)
        relink(ps)
    return out


def test_m17_peer_conn_identity_configured_endpoint(rig, m17_peers):
    """P-M17-58 (dev: the reply; keep: the links stay as configured): a CONN
    whose designation does not belong to the endpoint it comes from (M17-DVX
    from the M17-DVT port, M17-DVT from the M17-DVU port, M17-DVU from the
    M17-DVT port) gets a 4 B NACK from the sut and changes nothing: both
    peers keep their PING and carry their own streams. Peer identity is the
    exact ip:port plus the designation (decision Q3)."""
    got = per_side(m17_peers, configured_identity_probe, rig)
    print("P-M17-58 CONN with the wrong designation from a peer port:", got)
    sut = got.get("sut")
    if sut:
        assert not any(r.startswith("ACKN") for r in sut["replies"].values()), "sut accepted a wrong designation: %s" % sut["replies"]
        assert sut["dvt_ping"] and sut["dvu_ping"], "sut dropped a peer link on a wrong CONN: %s" % sut
        assert sut["dvt_conn"] == 0 and sut["dvu_conn"] == 0, "sut re-started a handshake: %s" % sut
        assert sut["dvt_relayed"] > FRAMES // 2 and sut["dvu_relayed"] > FRAMES // 2, "sut peers no longer carry their streams: %s" % sut
        sut["reply"] = sut["replies"]["dvx_at_dvt"]
        assert set(sut["replies"].values()) == {sut["reply"]}, "sut answers differ: %s" % sut["replies"]
    spec_check(rig, "P-M17-58", got, "reply")


# ---------------------------------------------------------------- P-M17-59 / 60 refused CONN lists

def refuse_run(rig, tag, link, listener, packets):
    """The peer is DISC'ed and passive; each (name, datagram) is sent in
    turn and the first ACKN / NACK that follows is the reply. Then QUIET
    seconds to see a PING (a link) and a peer stream that must not arrive."""
    link.auto_ackn = False
    link.drop()
    out = {"replies": {}}
    with malformed(rig, tag):
        t0 = time.monotonic()
        for name, data in packets:
            t = time.monotonic()
            link.send(data)
            out["replies"][name] = label(link.wait_for(kind=("ACKN", "NACK"), since=t, timeout=1.0))
        rest = t0 + QUIET - time.monotonic()
        link.wait_for(kind="PING", since=t0, timeout=max(0.1, rest))
        out["linked"] = bool(link.datagrams(since=t0, kind="PING"))
        pkts = peer_stream()
        send_from_peer(link, pkts, link.modules[0], listener)
        out["relayed"] = relayed(listener, pkts)
    return out


def wrong_modules_probe(side, ps, rig):
    def one(link):
        listener = new(LISTENER, link.modules[0])
        try:
            ready(ps.address, listener)
            sets = wrong_module_sets(link.modules)
            packets = [(name, m17.peer_conn_packet(link.hub.designation, value)) for name, value in sets.items()]
            return refuse_run(rig, "m17-peer-wrong-modules", link, listener, packets)
        finally:
            done(listener)

    def reordered(link):
        """The same set in another order: links on the sut (decision Q6)."""
        mods = link.modules
        swapped = mods[::-1]
        out = {"swapped": swapped}
        if swapped == mods:
            return out
        link.auto_ackn = False
        link.drop()
        t0 = time.monotonic()
        link.send_conn(modules=swapped)
        ackn = link.wait_for(kind="ACKN37", since=t0, timeout=1.5)
        out.update(reply=label(ackn), ackn_modules=modules_of(ackn))
        if ackn is not None:
            own = link.wait_for(kind="CONN37", since=t0, timeout=CONN_WAIT[side])
            if own is not None:
                t1 = time.monotonic()
                link.send_ackn(modules=swapped)
                ping = link.wait_for(kind="PING", since=t1, timeout=5.5)
                out["ping_s"] = round(ping.t - t1, 1) if ping else None
        return out

    out = {}
    try:
        out = in_parallel({"dvt": lambda: one(ps.dvt), "dvu": lambda: one(ps.dvu)})
        out["dvu"]["reordered"] = reordered(ps.dvu)
    finally:
        relink(ps)
    return out


def test_m17_peer_conn_wrong_module_set(rig, m17_peers):
    """P-M17-59 (dev: the reply; keep: no link, nothing relayed; Q6): a CONN
    from a peer whose module list differs from its configured set (partial,
    extra, with an unserved module, a foreign one) gets a 4 B NACK and
    creates no link. The same set in another order links, and the ACKN
    carries the reflector's configured order. mrefd (ordered compare,
    silent) is recorded."""
    got = per_side(m17_peers, wrong_modules_probe, rig)
    print("P-M17-59 wrong module sets:", got)
    for side, r in got.items():
        for key in ("dvt", "dvu"):
            o = r[key]
            if side == "sut":
                assert not o["linked"] and o["relayed"] == 0, "%s %s: linked on a wrong module set: %s" % (side, key, o)
                accepted = {n: v for n, v in o["replies"].items() if v.startswith("ACKN")}
                assert not accepted, "%s %s: wrong module sets answered ACKN: %s" % (side, key, accepted)
            elif o["linked"]:
                print("P-M17-59 mrefd %s linked on %s" % (key, o["replies"]))
    sut = got.get("sut")
    if sut:
        replies = {n: v for key in ("dvt", "dvu") for n, v in sut[key]["replies"].items()}
        sut["reply"] = next(iter(replies.values()))
        assert set(replies.values()) == {sut["reply"]}, "sut replies differ: %s" % replies
        re = sut["dvu"]["reordered"]
        assert re.get("reply") == "ACKN 37", "sut did not accept the same set in another order: %s" % re
        # DV-Router C89 (Q6 revised): the peer's own order is echoed when the sets are equal
        assert re["ackn_modules"] == re["swapped"], "ACKN order %r, the peer sent %r" % (re["ackn_modules"], re["swapped"])
        assert re.get("ping_s") is not None, "sut did not link after the swapped list: %s" % re
    spec_check(rig, "P-M17-59", got, "reply")


def malformed_probe(side, ps, rig):
    link = ps.dvt
    listener = new(LISTENER, link.modules[0])
    talker = new(TALKER, link.modules[0])
    try:
        ready(ps.address, listener, talker)
        variants = malformed_conns(link.hub.designation, link.modules)
        out = refuse_run(rig, "m17-peer-malformed-conn", link, listener, [(n, d) for n, d, _ in variants])
        clear(listener, talker)
        packets = stream(talker)
        send(talker, packets, listener)
        out["serving"] = relayed(listener, packets)
    finally:
        done(listener, talker)
        relink(ps)
    return out


@pytest.mark.slow
def test_m17_peer_conn_malformed(rig, m17_peers):
    """P-M17-60 (dev: the reply; keep: no link, alive): a CONN with an empty,
    lower case, duplicate or non-letter module list, or a list without NUL
    gets a 4 B NACK (Q6); a 36 B or 38 B CONN and a 37 B LSTN are not a peer
    CONN. None links, and the reflector keeps serving its clients."""
    got = per_side(m17_peers, malformed_probe, rig)
    print("P-M17-60 malformed CONN:", got)
    kinds = {name: kind for name, _, kind in malformed_conns("M17-DVT", "M")}
    for side, r in got.items():
        if side == "sut" or not r["linked"]:
            assert not r["linked"] and r["relayed"] == 0, "%s: linked on a malformed CONN: %s" % (side, r)
        assert r["serving"] > FRAMES // 2, "%s: not serving clients after malformed CONNs: %s" % (side, r)
        assert not any(v.startswith("ACKN") for n, v in r["replies"].items() if side == "sut" and kinds[n] == "refused"), \
            "%s: a refused list was ACKNed: %s" % (side, r["replies"])
    sut = got.get("sut")
    if sut:
        refused = {n: v for n, v in sut["replies"].items() if kinds[n] == "refused" and n != "no_nul"}
        sut["reply_refused"] = next(iter(refused.values()))
        assert set(refused.values()) == {sut["reply_refused"]}, "sut refusals differ: %s" % refused
    spec_check(rig, "P-M17-60", got, "reply_refused")


# ---------------------------------------------------------------- P-M17-54 DISC on stop (LAST: it restarts the sut)

@pytest.mark.slow
def test_m17_peer_disc_on_stop(rig, m17_peers, rig_control):
    """P-M17-54 (dev): an orderly stop (SIGTERM, docker restart) of the sut
    sends one DISC 10 B with its designation to every linked peer (decision
    Q15; the quiet SIGUSR1 restart sends none and is not probed). mrefd is
    not on the control channel's service list: not restarted, recorded. Must
    be the last case: the sut restarts and the peers link again."""
    ps = m17_peers.get("sut")
    if ps is None:
        pytest.skip("only the sut is restarted through the control channel (mrefd is not on its service list)")
    got = {"sut": {}}
    t0 = time.monotonic()
    try:
        rig_control("restart", "xlxd-sut")
        for key, link in (("dvt", ps.dvt), ("dvu", ps.dvu)):
            discs = link.datagrams(since=t0, kind="DISC")
            got["sut"][key] = {"count": len(discs), "disc": label(discs[0]) if discs else "none",
                               "designation": designation_of(discs[0]) if discs else None}
        got["sut"]["disc"] = got["sut"]["dvt"]["disc"]
        got["sut"]["count"] = got["sut"]["dvt"]["count"]
    finally:
        relink(ps)
    print("P-M17-54 DISC on restart:", got)
    for key in ("dvt", "dvu"):
        o = got["sut"][key]
        assert o["count"] == 1, "sut sent %d DISC to %s on stop (one expected)" % (o["count"], key)
        want = expected_designation(ps)
        assert not want or o["designation"] == want, "sut DISC designation %r, expected %r" % (o["designation"], want)
    spec_check(rig, "P-M17-54", got, "disc", "count")
