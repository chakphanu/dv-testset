# SPDX-License-Identifier: MIT
"""Icom G3 terminal / access point conformance (docs/protocols/g3.md): one
case per obligation P-G3-NN. The tester is the terminal (UDP 40000 on its
own address); xlxd-ref and xlxd-sut are probed one after the other with
the same terminal socket, told apart by source IP. DExtra clients play
the rest of the module.

Judged as in g3.md: hb / diff / dev / spec (Bn); dev items take the
profile's "g3" spec entry, and every case skips under a profile whose
spec does not say {"server": True} (DV-Router has no G3 edge yet).

Order matters: test_g3_voice_without_presence and test_g3_presence_address
run first (they need a reflector that has not seen this terminal or a
route query yet). xlxd drops every G3 client of an IP on any ICMP
unreachable for that IP, and earlier cases leave closed ports behind, so
each case waits 1.5 s and sends its presence again before it talks.
"""
import contextlib
import ipaddress
import os
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import UdpClient
from dvtest.proto import dextra, g3

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "J", "K"
REFL = "XLX999"
TERM, TERM_CALL = "N0TEST T", "N0TEST"       # the tester's terminal callsign (8) and MY
LISTENER, TALKER = "STR020", "STR021"       # DExtra clients
BLACK = "N0G3X"                              # DVT_XLXD_BLACKLIST=N0G3X (no other suite uses it)
FRAMES = 63                                  # three superframes: xlxd's loop guard strikes short overs
GAP = 0.020
QUIET = 1.5                                  # after the previous case (ICMP from its closed ports)
CACHE = 1.2


@pytest.fixture(scope="module")
def server(rig):
    """Skip unless the profile's sut serves G3 (xlxd always does)."""
    if rig.sut_profile != specs.DEFAULT and not ((specs.rule(rig, "g3") or {}).get("server")):
        pytest.skip("the %s profile has no G3 server yet (SPEC[\"g3\"][\"server\"] not True)" % rig.sut_profile)
    return rig


@pytest.fixture
def term(server):
    time.sleep(QUIET)
    t = g3.Terminal(TERM)
    try:
        yield t
    finally:
        t.close()


def sides(rig):
    return (("ref", rig.xlxd_ref), ("sut", rig.xlxd_sut))


def each(rig, func, *args, **kw):
    """{"ref": func("ref", ip, *args, **kw), "sut": ...}, one after the
    other (one terminal socket serves both)."""
    return {name: func(name, ip, *args, **kw) for name, ip in sides(rig)}


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def judge_dev(rig, nn, got, *keys):
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "g3") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no g3 %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


@contextlib.contextmanager
def marked(rig, tag):
    """Control channel "mark begin|end <tag>" around deliberately
    malformed tester traffic (dvcheck reports it as a warning)."""
    _mark(rig, "begin", tag)
    try:
        yield
    finally:
        _mark(rig, "end", tag)


def _mark(rig, phase, tag):
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    directory.mkdir(parents=True, exist_ok=True)
    name = "g3-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def frames(n=FRAMES, seed="g3"):
    return signals.prng_frames("dstar", n, seed=seed)


def rpt2(module=MOD):
    return (REFL + " ")[:7] + module


def g3_stream(module=MOD, voice=None, my=TERM_CALL, end=True, **fields):
    voice = voice or frames()
    sid = clients.fresh_stream_id("dstar")
    fields.setdefault("rpt2", rpt2(module))
    return voice, g3.stream(sid, voice, my, end=end, **fields)


def g3_talk(term, ip, module=MOD, listeners=(), other_port=False, **fields):
    """Presence, then one over from the terminal on module; listeners
    (DExtra clients) are drained meanwhile."""
    term.presence(ip)
    voice, packets = g3_stream(module, **fields)
    term.send_dv(ip, packets[0]) if not other_port else term.send_dv_other_port(ip, packets[0])
    time.sleep(0.3)
    term.paced(ip, packets[1:], GAP, *[c.client for c in listeners], other_port=other_port)
    settle(term, *listeners)
    return voice


def settle(term, *listeners, seconds=0.8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        term.drain()
        for c in listeners:
            c.client.drain()
        time.sleep(0.01)


def dstar(callsign, module=MOD):
    return dextra.DstarClient("0.0.0.0", callsign=callsign, module=module)


def linked(ip, *made):
    for c in made:
        assert c.link(ip), "%s: DExtra %s not linked" % (ip, c.callsign)
    time.sleep(CACHE)
    for c in made:
        c.client.drain()
        c.client.inbox.clear()
    return made


def unlink(*made):
    for c in made:
        try:
            if c.dest:
                c.unlink()
        finally:
            c.close()


def dextra_frames(c):
    return c.collect(settle=0.3)


def dextra_talk(talker, term, ip, voice=None):
    """A DExtra over on the talker's module; the terminal drains meanwhile."""
    voice = voice or frames(seed="dextra")
    term.clear()
    talker.talk(voice, clients.fresh_stream_id("dstar"), reflector=REFL, tail=0.3)
    settle(term, seconds=1.0)
    return voice


def g3_heard(term, ip):
    """Parsed DSVT the terminal got from ip: [(kind, packet)]."""
    return [(g3.kind_of(p), p) for _t, p in term.received(ip)]


def voice_of(heard):
    return [dextra.parse_voice_raw(p)["ambe"] for k, p in heard if k == "voice"]


# ---------------------------------------------------------------- first (fresh reflector)

def test_g3_voice_without_presence(server, term):
    """P-G3-11 (diff): voice from an IP that sent no presence is not
    relayed. Runs first: the tester has sent no presence yet."""
    def probe(name, ip):
        listener = dstar(LISTENER)
        try:
            linked(ip, listener)
            voice, packets = g3_stream()
            term.send_dv(ip, packets[0])
            time.sleep(0.3)
            term.paced(ip, packets[1:], GAP, listener.client)
            return {"heard": len(dextra_frames(listener))}
        finally:
            unlink(listener)
    with marked(server, "g3-no-presence"):
        got = each(server, probe)
    print("P-G3-11 voice without presence:", got)
    for name, r in got.items():
        assert r["heard"] == 0, "%s: %s" % (name, r)


def test_g3_presence_address(server, term):
    """P-G3-03 (dev): the IPv4 in the presence reply, before any route
    query and after one (xlxd takes it from the last route query)."""
    def probe(name, ip):
        first = g3.parse_presence_reply(term.presence(ip) or b"")
        term.route(ip, rpt2())
        second = g3.parse_presence_reply(term.presence(ip) or b"")
        return {"before_route_is_reflector": bool(first) and first["address"] == ip,
                "before_route": first and first["address"],
                "after_route_is_reflector": bool(second) and second["address"] == ip}
    got = each(server, probe)
    print("P-G3-03 presence address:", got)
    for name, r in got.items():
        assert r["after_route_is_reflector"], "%s: %s" % (name, r)
    judge_dev(server, "P-G3-03", got, "before_route_is_reflector")


# ---------------------------------------------------------------- 1 presence

def test_g3_presence_reply(server, term):
    """P-G3-01 (diff): a 32 B presence -> 36 B reply to its source:
    bytes 0-31 echoed with byte 2 0x80 and byte 3 0x00, then IPv4."""
    packet = g3.presence_packet(TERM)

    def probe(name, ip):
        raw = term.ask((ip, g3.PRESENCE_PORT), packet)
        reply = g3.parse_presence_reply(raw or b"")
        return {"size": len(raw) if raw else 0, "flags": reply and reply["flags"],
                "status": reply and reply["status"],
                "echo": bool(reply) and reply["body"] == packet[8:32] and raw[:2] + raw[4:8] == packet[:2] + packet[4:8]}
    got = each(server, probe)
    print("P-G3-01 presence:", got)
    for name, r in got.items():
        assert r == {"size": 36, "flags": 0x80, "status": 0, "echo": True}, "%s: %s" % (name, r)


@pytest.mark.parametrize("size", [31, 33])
def test_g3_presence_wrong_size(server, term, size):
    """P-G3-02 (diff): a presence of another size gets no reply."""
    packet = (g3.presence_packet(TERM) + b"\x00")[:size]
    with marked(server, "g3-presence-size"):
        got = each(server, lambda name, ip: {"reply": term.ask((ip, g3.PRESENCE_PORT), packet, 1.0) is not None})
    print("P-G3-02 presence %d B:" % size, got)
    for name, r in got.items():
        assert not r["reply"], "%s: %s" % (name, r)


def test_g3_presence_replaces(server, term):
    """P-G3-04 (diff): one terminal per IP; a presence with a new terminal
    callsign replaces it (the new one has no module yet), a repeat of the
    same callsign changes nothing."""
    def probe(name, ip):
        talker = dstar(TALKER)
        try:
            linked(ip, talker)
            g3_talk(term, ip)                                   # module J
            dextra_talk(talker, term, ip)
            heard_before = len(voice_of(g3_heard(term, ip)))
            term.presence(ip)                                   # repeat: unchanged
            dextra_talk(talker, term, ip)
            heard_repeat = len(voice_of(g3_heard(term, ip)))
            # another terminal callsign (the 8th character alone does not count)
            term.ask((ip, g3.PRESENCE_PORT), g3.presence_packet("STR024 T"))
            dextra_talk(talker, term, ip)
            heard_replaced = len(voice_of(g3_heard(term, ip)))
            return {"heard_before": heard_before > 0, "heard_after_repeat": heard_repeat > 0,
                    "heard_after_replace": heard_replaced > 0}
        finally:
            unlink(talker)
    got = each(server, probe)
    print("P-G3-04 presence replace:", got)
    for name, r in got.items():
        assert r == {"heard_before": True, "heard_after_repeat": True, "heard_after_replace": False}, "%s: %s" % (
            name, r)


def test_g3_blacklisted(server, term):
    """P-G3-05 (dev): a presence from a blacklisted terminal (status
    recorded) and its header (muted). Needs DVT_XLXD_BLACKLIST=N0G3X."""
    if BLACK not in os.environ.get("DVT_XLXD_BLACKLIST", "").split():
        pytest.skip("set DVT_XLXD_BLACKLIST=%s (written to both reflectors' xlxd.blacklist)" % BLACK)

    def probe(name, ip):
        listener = dstar(LISTENER)
        try:
            linked(ip, listener)
            reply = g3.parse_presence_reply(term.ask((ip, g3.PRESENCE_PORT), g3.presence_packet(BLACK + "  T")) or b"")
            voice, packets = g3_stream(my=BLACK)
            term.send_dv(ip, packets[0])
            time.sleep(0.3)
            term.paced(ip, packets[1:], GAP, listener.client)
            return {"presence_status": reply["status"] if reply else None,
                    "header_relayed": len(dextra_frames(listener)) > 0}
        finally:
            unlink(listener)
    with marked(server, "g3-blacklisted"):
        got = each(server, probe)
    print("P-G3-05 blacklisted:", got)
    for name, r in got.items():
        assert not r["header_relayed"], "%s: blacklisted MY relayed" % name
    judge_dev(server, "P-G3-05", got, "presence_status")


# ---------------------------------------------------------------- 2 route query

def test_g3_route_accepted(server, term):
    """P-G3-06 (diff), P-G3-10 (dev): a repeater-call query for XLX999 J:
    status 0, UR / RPT1 (+G) / RPT2 fields, the reflector's IPv4; the
    callsign used on the wire is recorded."""
    def probe(name, ip):
        reply = g3.parse_route_reply(term.route(ip, rpt2()) or b"")
        return {"flags": reply and reply["flags"] & 0x80, "status": reply and reply["status"],
                "ur": reply and reply["ur"], "rpt1": reply and reply["rpt1"], "rpt2": reply and reply["rpt2"],
                "address_is_reflector": bool(reply) and reply["address"] == ip}
    got = each(server, probe)
    print("P-G3-06/10 route accepted:", got)
    for name, r in got.items():
        assert r["status"] == 0 and r["flags"] and r["address_is_reflector"], "%s: %s" % (name, r)
        assert r["ur"][7:] == MOD.encode() and r["rpt1"][7:] == b"G" and r["rpt2"][7:] == MOD.encode(), name
    judge_dev(server, "P-G3-10", got, "ur", "rpt1", "rpt2")


@pytest.mark.parametrize("what", ["routed", "foreign"])
def test_g3_route_rejected(server, term, what):
    """P-G3-07 (diff): a routed call or a foreign callsign: status 1, IPv4 0."""
    def probe(name, ip):
        raw = term.route(ip, rpt2(), repeater=False) if what == "routed" else term.route(ip, "XLX998 J")
        reply = g3.parse_route_reply(raw or b"")
        return {"status": reply and reply["status"], "address": reply and reply["address"]}
    got = each(server, probe)
    print("P-G3-07 route %s:" % what, got)
    for name, r in got.items():
        assert r == {"status": 1, "address": "0.0.0.0"}, "%s: %s" % (name, r)


@pytest.mark.parametrize("size", [15, 17])
def test_g3_route_wrong_size(server, term, size):
    """P-G3-08 (diff): a route query of another size gets no reply."""
    packet = (g3.route_packet(rpt2()) + b" ")[:size]
    with marked(server, "g3-route-size"):
        got = each(server, lambda name, ip: {"reply": term.ask((ip, g3.ROUTE_PORT), packet, 1.0) is not None})
    print("P-G3-08 route %d B:" % size, got)
    for name, r in got.items():
        assert not r["reply"], "%s: %s" % (name, r)


def test_g3_route_blank_callsign(server, term):
    """P-G3-09 (diff): an all-space destination is the reflector itself;
    the answer (status, RPT2) is compared."""
    def probe(name, ip):
        reply = g3.parse_route_reply(term.route(ip, " " * 8) or b"")
        return {"status": reply and reply["status"], "ur": reply and reply["ur"], "rpt2": reply and reply["rpt2"]}
    got = each(server, probe)
    print("P-G3-09 blank destination:", got)
    for name, r in got.items():
        assert r["ur"] and r["ur"][:6] == REFL.encode()[:6], "%s: %s" % (name, r)
    same(got, "status", "ur", "rpt2")


# ---------------------------------------------------------------- 3 voice

def talk_probe(name, ip, term, **fields):
    listener = dstar(LISTENER)
    try:
        linked(ip, listener)
        term.clear()
        voice = g3_talk(term, ip, listeners=(listener,), **fields)
        got = dextra_frames(listener)
        return {"heard": len(got), "exact": got == voice, "echo": len(voice_of(g3_heard(term, ip)))}
    finally:
        unlink(listener)


def test_g3_talk_reaches_dextra(server, term):
    """P-G3-12 (diff), P-G3-19 (B9), P-G3-20 (diff): after presence a
    header with RPT2 = XLX999 J opens a stream on J; a DExtra listener gets
    the AMBE byte exact; the terminal gets no echo."""
    got = each(server, talk_probe, term)
    print("P-G3-12/19/20 talk:", got)
    for name, r in got.items():
        assert r["exact"], "%s: %s" % (name, r)
    # B9: no echo; under the xlxd profile the echo count is compared
    if server.sut_profile == specs.DEFAULT:
        same(got, "echo")
    else:
        assert got["sut"]["echo"] == 0, "sut echoed %d frames to the talking terminal" % got["sut"]["echo"]


def test_g3_foreign_rpt2_dropped(server, term):
    """P-G3-13 (diff): a header whose RPT2 is not the reflector is dropped."""
    with marked(server, "g3-foreign-rpt2"):
        got = each(server, talk_probe, term, rpt2=b"XLX998 J")
    print("P-G3-13 foreign RPT2:", got)
    for name, r in got.items():
        assert r["heard"] == 0, "%s: %s" % (name, r)


def test_g3_other_source_port(server, term):
    """P-G3-18 (diff): voice from the terminal's IP but another source
    port is accepted."""
    with marked(server, "g3-other-port"):
        got = each(server, talk_probe, term, other_port=True)
    print("P-G3-18 other source port:", got)
    for name, r in got.items():
        assert r["exact"], "%s: %s" % (name, r)


@pytest.mark.parametrize("field", ["my", "rpt1"])
def test_g3_invalid_callsign(server, term, field):
    """P-G3-21 (dev): a header with an invalid MY or RPT1: heard recorded."""
    bad = {"my": {"my": "N0#EST"}, "rpt1": {"rpt1": b"N0#EST G"}}[field]
    with marked(server, "g3-bad-callsign"):
        got = each(server, talk_probe, term, **bad)
    key = "heard_" + field
    got = {k: {key: v["heard"]} for k, v in got.items()}
    print("P-G3-21 invalid %s:" % field, got)
    judge_dev(server, "P-G3-21", got, key)


def module_probe(name, ip, term):
    """Fresh terminal callsign (no module), then talk on J, then on K."""
    on_j, on_k = dstar(TALKER), dstar(LISTENER, OTHER)
    try:
        linked(ip, on_j, on_k)
        term.ask((ip, g3.PRESENCE_PORT), g3.presence_packet("STR025 T"))
        dextra_talk(on_j, term, ip)
        fresh = len(voice_of(g3_heard(term, ip)))
        term.terminal = "STR025 T"
        g3_talk(term, ip, MOD)
        dextra_talk(on_j, term, ip)
        after_j = len(voice_of(g3_heard(term, ip)))
        g3_talk(term, ip, OTHER)
        dextra_talk(on_j, term, ip)
        j_after_k = len(voice_of(g3_heard(term, ip)))
        dextra_talk(on_k, term, ip)
        k_after_k = len(voice_of(g3_heard(term, ip)))
        return {"fresh_hears": fresh > 0, "hears_j": after_j > 0, "hears_j_after_k": j_after_k > 0,
                "hears_k": k_after_k > 0}
    finally:
        term.terminal = TERM
        unlink(on_j, on_k)


def test_g3_module_follows_talk(server, term):
    """P-G3-14 (diff): a terminal that never talked hears nothing; it hears
    the module of its last header and moves with it."""
    got = each(server, module_probe, term)
    print("P-G3-14 module follows talk:", got)
    for name, r in got.items():
        assert r == {"fresh_hears": False, "hears_j": True, "hears_j_after_k": False, "hears_k": True}, \
            "%s: %s" % (name, r)


def listen_probe(name, ip, term):
    talker = dstar(TALKER)
    try:
        linked(ip, talker)
        g3_talk(term, ip)                                       # the terminal is on J now
        voice = dextra_talk(talker, term, ip)
        heard = g3_heard(term, ip)
        kinds = [k for k, _p in heard]
        headers = [dextra.parse_header_full(p) for k, p in heard if k == "header"]
        seqs = [dextra.parse_voice_raw(p)["counter"] for k, p in heard if k in ("voice", "last")]
        last = [dextra.parse_voice_raw(p) for k, p in heard if k == "last"]
        return {"exact": voice_of(heard) == voice, "headers": len(headers),
                "header_sizes_ok": all(len(p) == 56 for k, p in heard if k == "header"),
                "rpt2": headers[0]["rpt2"] if headers else None,
                "seq_ok": [s & 0x3F for s in seqs[:-1]] == [i % 21 for i in range(len(seqs) - 1)],
                "last_flag": bool(seqs) and bool(seqs[-1] & 0x40), "lasts": kinds.count("last"),
                "last_payload": (last[0]["ambe"] + last[0]["slow"]).hex() if last else None}
    finally:
        unlink(talker)


def test_g3_listen(server, term):
    """P-G3-15 (diff), P-G3-16 (hb), P-G3-17 (dev), P-G3-20 (diff): a DExtra
    over on J reaches the terminal at IP:40000: AMBE exact, headers 56 B
    with RPT2 = XLX999 J, sequence 0..20, one last frame. P-G3-17: under
    xlxd header copies and last payload sut == ref; else the spec's
    "min_headers" and "last_payload" ("pass" = the payload the talker sent)."""
    got = each(server, listen_probe, term)
    print("P-G3-15/16/17 listen:", got)
    for name, r in got.items():
        assert r["exact"] and r["header_sizes_ok"] and r["seq_ok"] and r["last_flag"] and r["lasts"] == 1, \
            "%s: %s" % (name, r)
        assert r["rpt2"] == rpt2().encode(), "%s: RPT2 %r" % (name, r["rpt2"])
    if server.sut_profile == specs.DEFAULT:
        same(got, "headers", "last_payload")
        return
    entry = (specs.rule(server, "g3") or {}).get("P-G3-17")
    if not entry:
        pytest.skip("spec pending: no g3 P-G3-17 entry in dvtest/specs/%s.py" % server.sut_profile)
    assert got["sut"]["headers"] >= entry["min_headers"], "sut: %d header copies" % got["sut"]["headers"]
    if entry["last_payload"] == "pass":
        # DstarClient.talk ends with a last frame of zero AMBE and its default slow data
        sent = (bytes(9) + dextra.SLOW_DATA).hex()
        assert got["sut"]["last_payload"] == sent, "sut last payload %s, sent %s" % (got["sut"]["last_payload"], sent)
    else:
        assert got["sut"]["last_payload"] == entry["last_payload"], "sut last payload %s" % got["sut"]["last_payload"]


def lost_last_probe(name, ip, term):
    listener, third = dstar(LISTENER), dstar(TALKER)
    try:
        linked(ip, listener, third)
        term.clear()
        g3_talk(term, ip, listeners=(listener,), end=False)
        time.sleep(2.0)
        lasts = sum(1 for p in listener.client.inbox if (dextra.parse_voice_raw(p) or {}).get("last"))
        listener.client.inbox.clear()
        third.talk(frames(seed="third"), clients.fresh_stream_id("dstar"), reflector=REFL, tail=0.3)
        return {"synthesized_last": lasts, "free": len(dextra_frames(listener)) > 0}
    finally:
        unlink(listener, third)


def test_g3_lost_last(server, term):
    """P-G3-22 (B3): a G3 over without a last frame ends after the silence
    timeout; the module is free; a synthesized last frame is recorded."""
    with marked(server, "g3-no-last"):
        got = each(server, lost_last_probe, term)
    print("P-G3-22 lost last frame:", got)
    for name, r in got.items():
        assert r["free"], "%s: %s" % (name, r)
    judge_dev(server, "P-G3-22", got, "synthesized_last")


# ---------------------------------------------------------------- 4 liveness

def icmp_probe(name, ip, term, how):
    talker = dstar(TALKER)
    dead = None
    try:
        linked(ip, talker)
        g3_talk(term, ip)                                       # on J
        if how == "own_port":
            term.shut()                                         # 40000 closed
            dextra_talk(talker, term, ip)                       # its packets draw ICMP
            term.open()
        else:
            dead = dstar("STR022", OTHER)
            linked(ip, dead)
            port = dead.client.local[1]
            dead.client.close()                                 # a closed DExtra port on the same IP
            on_k = dstar("STR023", OTHER)
            linked(ip, on_k)
            on_k.talk(frames(seed="k"), clients.fresh_stream_id("dstar"), reflector=REFL, tail=0.5)
            unlink(on_k)
            # unlink the dead client from its old port, so it stops drawing ICMP
            sweep = UdpClient("0.0.0.0", port=port)
            sweep.send((ip, dextra.PORT), dextra.unlink_packet("STR022", OTHER))
            sweep.close()
            dead = None
        time.sleep(1.0)
        dextra_talk(talker, term, ip)
        return {"removed": len(voice_of(g3_heard(term, ip))) == 0}
    finally:
        unlink(talker)
        if dead:
            dead.close()


@pytest.mark.parametrize("how", ["own_port", "other_port"])
def test_g3_icmp_removal(server, term, how):
    """P-G3-23 (dev): ICMP port unreachable for the terminal's 40000
    (own_port) or for another port of the same IP (other_port, a closed
    DExtra client): is the terminal removed?"""
    with marked(server, "g3-icmp"):
        got = each(server, icmp_probe, term, how)
    print("P-G3-23 ICMP %s:" % how, got)
    if how == "own_port":
        for name, r in got.items():
            assert r["removed"], "%s: terminal kept after ICMP for its own port" % name
    judge_dev(server, "P-G3-23", {k: {"removed_" + how: v["removed"]} for k, v in got.items()}, "removed_" + how)


def test_g3_ping(server, term):
    """P-G3-24 (dev): while idle the reflector sends `PING` to the
    terminal's IP:40000; count and spacing over 25 s recorded."""
    for name, ip in sides(server):
        term.presence(ip)
    term.clear()
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        term.drain()
        time.sleep(0.05)
    got = {}
    for name, ip in sides(server):
        stamps = [t for t, p in term.received(ip) if p == g3.PING]
        gaps = [round(b - a) for a, b in zip(stamps, stamps[1:])]
        got[name] = {"pings": len(stamps), "gaps": gaps, "period_10s": bool(gaps) and all(9 <= g <= 11 for g in gaps)}
    print("P-G3-24 ping:", got)
    judge_dev(server, "P-G3-24", got, "period_10s")


def test_g3_hpch(server, term):
    """P-G3-26 (dev): a JARL `HPCH` packet from the terminal's 40000: any
    reply recorded; the terminal still talks afterwards."""
    def probe(name, ip):
        term.presence(ip)
        term.clear()
        term.send_dv(ip, g3.hpch_packet(rpt2()))
        settle(term, seconds=1.0)
        replies = [p for _t, p in term.received(ip) if p != g3.PING]
        return {"reply": len(replies)}
    with marked(server, "g3-hpch"):
        got = each(server, probe)
    print("P-G3-26 HPCH:", got)
    judge_dev(server, "P-G3-26", got, "reply")


# ---------------------------------------------------------------- 5 two terminals

def test_g3_two_terminals(server, term):
    """P-G3-27 (diff): two terminals (the tester and g3-term2) on J both
    hear a DExtra talker; a terminal's over reaches the other terminal and
    not itself. Needs run.sh --profile g3."""
    if os.environ.get("DVT_G3") != "on":
        pytest.skip("needs the second terminal: scripts/run.sh --profile g3")
    relay_ip = str(ipaddress.ip_network(server.subnet, strict=False).network_address + 31)
    term2 = g3.RelayTerminal(relay_ip, "N0CALL T")
    try:
        assert term2.alive(), "g3-term2 relay at %s does not answer" % relay_ip

        def probe(name, ip):
            talker = dstar(TALKER)
            try:
                linked(ip, talker)
                term2.presence(ip)
                _voice, packets = g3_stream(my="N0CALL")
                for packet in packets:
                    term2.send_dv(ip, packet)
                    time.sleep(GAP)
                g3_talk(term, ip)
                settle(term, seconds=1.0)
                term2_echo = len(voice_of([(g3.kind_of(p), p) for _t, p in term2.received(ip)]))
                term2.clear()
                term.clear()
                voice = dextra_talk(talker, term, ip)
                time.sleep(0.5)
                return {"term1": voice_of(g3_heard(term, ip)) == voice,
                        "term2": voice_of([(g3.kind_of(p), p) for _t, p in term2.received(ip)]) == voice,
                        "term2_heard_term1": term2_echo > 0}
            finally:
                unlink(talker)
        got = each(server, probe)
        print("P-G3-27 two terminals:", got)
        for name, r in got.items():
            assert r == {"term1": True, "term2": True, "term2_heard_term1": True}, "%s: %s" % (name, r)
    finally:
        term2.close()
