# SPDX-License-Identifier: MIT
"""M17 reflector conformance (docs/protocols/m17.md, testlist K): one case
per obligation P-M17-NN, xlxd-ref and xlxd-sut probed in parallel, M17
clients only.

Judged as in m17.md: hb = a spec obligation, asserted on both sides; dev
= disputed between reflectors (or xlxd deviates from the spec / the
clients): sut == ref under the xlxd profile, else the profile's "m17"
spec entry {"P-M17-NN": {observed key: value}} (skip "spec pending"
without one); diff = sut == ref. Every case prints what both sides did.
"""
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import m17

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "M", "N"
TALKER, LISTENER = "N0CALL", "N0TEST"
# packets per test stream (1.2 s): 60 codec frames, so that xlxd-ref's loop
# detection (3 consecutive streams under 50 frames from one callsign on one
# module within 2 s -> TX and link block) never fires during the suite
FRAMES = 30
GAP = 0.040                                     # one packet per 40 ms
CACHE = 1.2                                     # xlxd refreshes its module client list once per second
TAIL = 0.8
DESIGNATION = "M17-DVT"                         # a reflector designation as M17Gateway hosts files write it


def both(rig, func, *args):
    """{"ref": func(ref ip, *args), "sut": ...} run in parallel."""
    got = {}

    def side(name):
        try:
            got[name] = func(getattr(rig, "xlxd_" + name), *args)
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n,)) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    return got


def judge_dev(rig, nn, got, *keys):
    """dev items: sut == ref on keys under the xlxd profile, else the
    profile's m17 spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "m17") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no m17 %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def mark(rig, phase, tag):
    """Control channel "mark begin|end <tag>": brackets deliberately
    malformed tester traffic for dvcheck (no-op without the channel)."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "m17-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def marked(rig, tag, func, *args):
    """both() inside a dvcheck mark window."""
    mark(rig, "begin", tag)
    try:
        return both(rig, func, *args)
    finally:
        mark(rig, "end", tag)


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


# ---------------------------------------------------------------- clients

def new(callsign=TALKER, module=MOD, keepalive=True):
    """An M17 client; keepalive False: it does not answer PING."""
    c = m17.M17Client("0.0.0.0", callsign=callsign, module=module)
    if not keepalive:
        c.client.responder = None
    return c


def clear(*made):
    for c in made:
        c.client.drain()
        c.client.inbox.clear()
        c.client.inbox_times.clear()


def settle(*made, seconds=TAIL):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for c in made:
            c.client.drain()
        time.sleep(0.01)


def control(tag=None):
    return lambda data: (m17.parse_control(data) or {}).get("tag") in ((tag,) if tag else
                                                                     ("ACKN", "NACK", "DISC", "CONN"))


def anything(data):
    return True


def wait_for(c, pred, seconds=1.0):
    deadline = time.monotonic() + seconds
    while True:
        c.client.drain()
        for data in c.client.inbox:
            if pred(data):
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def ask(c, packet, pred=anything, seconds=1.0):
    """Send packet with an empty inbox; the first reply matching pred."""
    clear(c)
    c.client.send(c.dest, packet)
    return wait_for(c, pred, seconds)


def reply_of(data):
    """"ACKN 4", "NACK 4", ... or "none"."""
    parsed = m17.parse_control(data) if data else None
    if not data:
        return "none"
    return "%s %d" % (parsed["tag"], parsed["size"]) if parsed else "other %d" % len(data)


def connect(ip, c, packet=None, seconds=1.0):
    """CONN (or packet) from c; the reply as reply_of()."""
    c.dest = (ip, m17.PORT)
    return reply_of(ask(c, packet or m17.conn_packet(c.callsign, c.module), control(), seconds))


def ready(ip, *made):
    for c in made:
        got = connect(ip, c)
        assert got == "ACKN 4", "%s: CONN %s %s -> %s" % (ip, c.callsign, c.module, got)
    time.sleep(CACHE)
    clear(*made)
    return made


def done(*made):
    for c in made:
        try:
            if c.dest:
                c.client.send(c.dest, m17.disc_packet(c.callsign))
        finally:
            c.client.close()


# ---------------------------------------------------------------- streams

def payloads(n=FRAMES, seed="m17conf"):
    raw = signals.prng_bytes(seed, 16 * n)
    return [raw[i:i + 16] for i in range(0, 16 * n, 16)]


def stream(c, sid=None, n=FRAMES, fn0=0, last=True, seed=None, **fields):
    """n stream packets FN fn0.. (last bit on the last one); fields go to
    m17.stream_packet (src defaults to the client's callsign). Payloads
    are PRNG bytes unique to the stream id unless seed is given."""
    sid = sid or clients.fresh_stream_id("m17")
    fields.setdefault("src", c.callsign)
    data = payloads(n, seed or "m17conf-%d" % sid)
    return [m17.stream_packet(sid, fn0 + i, data[i], last=last and i == n - 1, **fields) for i in range(n)]


def send(c, packets, *listeners, wait=TAIL):
    c.client.paced(c.dest, packets, GAP, *[m.client for m in listeners])
    settle(c, *listeners, seconds=wait)


def received(c):
    return [p for p in map(m17.parse_stream, c.client.inbox) if p]


def relayed(listener, packets):
    """How many of the sent packets' payloads the listener got."""
    got = {p["payload"] for p in received(listener)}
    return sum(1 for d in packets if m17.parse_stream(d)["payload"] in got)


def voice_after_fn0(listener, packets):
    """Every payload of FN >= 1 arrived, in order."""
    want = [m17.parse_stream(d)["payload"] for d in packets[1:]]
    got = [p["payload"] for p in received(listener) if p["payload"] in set(want)]
    return got == want


def heard(listener):
    return len(received(listener))


def talk_probe(ip, packets_of, listen_module=MOD):
    """A talker sends packets_of(talker) on MOD; returns (talker,
    listener, packets) with the listener's inbox filled."""
    talker, listener = new(TALKER), new(LISTENER, listen_module)
    ready(ip, listener, talker)
    packets = packets_of(talker)
    send(talker, packets, listener)
    return talker, listener, packets


# ---------------------------------------------------------------- 1 connect

def conn_probe(ip):
    c = new()
    try:
        return {"reply": connect(ip, c)}
    finally:
        done(c)


def test_m17_conn_ack(rig):
    """P-M17-01 (hb): CONN for a configured module -> ACKN, exactly 4 B
    (DroidStar and mvoice accept nothing else)."""
    got = both(rig, conn_probe)
    print("P-M17-01 CONN:", got)
    for name, r in got.items():
        assert r["reply"] == "ACKN 4", "%s: CONN answered %s" % (name, r["reply"])


def suffix_probe(ip):
    talker, c = new(TALKER), new(LISTENER)
    try:
        reply = connect(ip, c, m17.conn_packet(LISTENER + "  H", MOD))
        ready(ip, talker)
        clear(c)
        packets = stream(talker)
        send(talker, packets, c)
        return {"reply": reply, "relayed": relayed(c, packets) > 0}
    finally:
        done(talker, c)


def test_m17_conn_suffix(rig):
    """P-M17-02 (diff): a CONN callsign with a 9th character (M17Gateway's
    suffix, DroidStar's 'D') links and receives traffic."""
    got = both(rig, suffix_probe)
    print("P-M17-02 CONN 'N0TEST  H':", got)
    same(got, "reply", "relayed")


def unconfigured_probe(ip, module):
    c = new(module=module)
    try:
        return {"reply": connect(ip, c)}
    finally:
        done(c)


def test_m17_conn_unconfigured_module(rig):
    """P-M17-03 (dev): CONN for a module the reflector does not run ->
    NACK 4 B. xlxd-ref runs all 26 modules, so only a profile spec entry
    {"module": <letter not configured>, "reply": "NACK 4"} can judge it."""
    entry = (specs.rule(rig, "m17") or {}).get("P-M17-03") if rig.sut_profile != specs.DEFAULT else None
    if not entry:
        pytest.skip("xlxd runs all 26 modules; a profile spec P-M17-03 {module, reply} judges the sut")
    c = new(module=entry["module"])
    try:
        reply = connect(rig.xlxd_sut, c)
    finally:
        done(c)
    print("P-M17-03 CONN %s on sut: %s" % (entry["module"], reply))
    assert reply == entry["reply"], "sut answered %s, spec %s" % (reply, entry["reply"])


BAD_CONN = {
    "module_digit": m17.conn_packet(TALKER, "1"),
    "module_lower": m17.conn_packet(TALKER, "m"),
    "callsign_zero": b"CONN" + bytes(6) + b"M",
}


def bad_conn_probe(ip, name):
    c = new()
    try:
        reply = connect(ip, c, BAD_CONN[name])
        clear(c)
        pings = wait_for(c, control("PING"), 6.0) is not None     # linked clients get PING (3-5 s)
        return {"reply": reply, "linked": pings}
    finally:
        c.client.close()


@pytest.mark.parametrize("name", list(BAD_CONN))
def test_m17_conn_bad_module(rig, name):
    """P-M17-04 (dev): a CONN with a module byte outside A-Z or an
    undecodable callsign links nothing; the reply (silence or NACK) is
    the dev part."""
    got = marked(rig, "m17-bad-conn-" + name, bad_conn_probe, name)
    print("P-M17-04 %s:" % name, got)
    for side, r in got.items():
        assert not r["linked"] and r["reply"] != "ACKN 4", "%s: %s linked (%s)" % (side, name, r)
    judge_dev(rig, "P-M17-04", {n: {"reply_" + name: r["reply"]} for n, r in got.items()}, "reply_" + name)


WRONG_CONN = {
    "conn10": m17.conn_packet(TALKER, MOD)[:10],
    "conn12": m17.conn_packet(TALKER, MOD) + b"\0",
    "lstn12": m17.lstn_packet(TALKER, MOD) + b"\0",
}


def wrong_conn_probe(ip, name):
    c = new()
    try:
        return {"reply": connect(ip, c, WRONG_CONN[name])}
    finally:
        c.client.close()


@pytest.mark.parametrize("name", list(WRONG_CONN))
def test_m17_conn_wrong_size(rig, name):
    """P-M17-05 (diff): CONN/LSTN of another length gets no reply."""
    got = marked(rig, "m17-wrong-conn-" + name, wrong_conn_probe, name)
    print("P-M17-05 %s:" % name, got)
    same(got, "reply")


def listen_probe(ip):
    talker, other, lst = new(TALKER), new("N0TEST"), new(LISTENER)
    try:
        reply = connect(ip, lst, m17.lstn_packet(LISTENER, MOD))
        ready(ip, talker, other)
        clear(lst)
        packets = stream(talker)
        send(talker, packets, lst, other)
        hears = relayed(lst, packets)
        clear(lst, other)
        own = stream(lst, seed="lstn")
        send(lst, own, other)
        return {"reply": reply, "hears": hears > 0, "hears_count": hears, "relayed": relayed(other, own) > 0}
    finally:
        done(talker, other, lst)


@pytest.fixture(scope="module")
def listen_only(rig):
    got = marked(rig, "m17-lstn", listen_probe)
    print("LSTN scenario:", got)
    return got


def test_m17_listen_only(rig, listen_only):
    """P-M17-06 (dev): LSTN links a listen-only client (ACKN), it hears
    the module; whether LSTN is supported at all is the dev part."""
    judge_dev(rig, "P-M17-06", listen_only, "reply", "hears")


def test_m17_listen_only_cannot_talk(rig, listen_only):
    """P-M17-30 (dev): a listen-only client's stream is not relayed."""
    judge_dev(rig, "P-M17-30", listen_only, "relayed")


def reconn_probe(ip):
    c, talker, other = new(LISTENER), new(TALKER), new(TALKER, OTHER)
    try:
        ready(ip, c, talker, other)
        same_module = connect(ip, c)
        move = connect(ip, c, m17.conn_packet(LISTENER, OTHER))
        time.sleep(CACHE)
        clear(c)
        on_mod = stream(talker, seed="reconn-m")
        send(talker, on_mod, c)
        on_other = stream(other, seed="reconn-n")
        send(other, on_other, c)
        return {"same_module": same_module, "other_module": move,
                "hears_old": relayed(c, on_mod) > 0, "hears_new": relayed(c, on_other) > 0}
    finally:
        done(c, talker, other)


def test_m17_reconn(rig):
    """P-M17-07 (dev): a repeated CONN from a linked address: same module
    -> ACKN (M17Gateway resends CONN until ACKN; NACK would stop it), other
    module -> moved (heard there, not on the old module)."""
    got = both(rig, reconn_probe)
    print("P-M17-07 re-CONN:", got)
    judge_dev(rig, "P-M17-07", got, "same_module", "other_module", "hears_old", "hears_new")


def two_ports_probe(ip):
    talker, a, b = new(TALKER), new(LISTENER), new(LISTENER)
    try:
        ready(ip, a, b, talker)
        packets = stream(talker)
        send(talker, packets, a, b)
        counts = [heard(a), heard(b)]
        return {"each": counts[0] == counts[1] and counts[0] > 0 and
                len({p["fn"] for p in received(a)}) == counts[0], "counts": counts}
    finally:
        done(talker, a, b)


def test_m17_two_ports(rig):
    """P-M17-08 (diff): the same callsign from two ports is two clients,
    each gets the stream once."""
    got = both(rig, two_ports_probe)
    print("P-M17-08 two ports:", got)
    same(got, "each")


# ---------------------------------------------------------------- 2 keepalive

def ping_probe(ip):
    c = new()
    try:
        ready(ip, c)
        settle(c, seconds=16)
        stamps = [t for d, t in zip(c.client.inbox, c.client.inbox_times) if control("PING")(d)]
        pings = [m17.parse_control(d) for d in c.client.inbox if control("PING")(d)]
        gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
        median = gaps[len(gaps) // 2] if gaps else None
        return {"interval_s": None if median is None else round(median), "size": pings[0]["size"] if pings else None,
                "callsign": pings[0]["callsign"] if pings else None, "count": len(pings)}
    finally:
        done(c)


def test_m17_ping_interval(rig):
    """P-M17-09 (dev): PING 10 B with the reflector callsign to every
    client; spec interval 3 s (xlxd 5 s is the dev part)."""
    got = both(rig, ping_probe)
    print("P-M17-09 PING:", got)
    for side, r in got.items():
        assert r["size"] == 10, "%s: PING %s" % (side, r)
    judge_dev(rig, "P-M17-09", got, "interval_s")


KEEPALIVE_S = 45


def keepalive_scenario(ip):
    """keeper answers PING; silent sends nothing; ponger sends PONG every
    8 s without answering PING (DroidStar); pinger sends PING every 8 s;
    streamer talks on OTHER for the whole window without any keepalive.
    Then a stream on each module shows who is still linked."""
    keeper, talker = new(LISTENER), new(TALKER)
    silent, ponger, pinger = (new("N0TEST", keepalive=False) for _ in range(3))
    streamer, talker2 = new("N0TEST", OTHER, keepalive=False), new(TALKER, OTHER)
    made = (keeper, silent, ponger, pinger, streamer, talker, talker2)
    try:
        ready(ip, *made)
        start = time.monotonic()
        sid, fn, last_ka = clients.fresh_stream_id("m17"), 0, None
        disc = {}
        while time.monotonic() - start < KEEPALIVE_S:
            now = time.monotonic()
            if last_ka is None or now - last_ka >= 8:
                ponger.client.send(ponger.dest, m17.pong_packet(ponger.callsign))
                pinger.client.send(pinger.dest, m17.ping_packet(pinger.callsign))
                last_ka = now
            streamer.client.send(streamer.dest, m17.stream_packet(sid, fn, signals.prng_bytes(fn, 16),
                                                                  src=streamer.callsign))
            fn += 1
            talker.client.drain()
            talker2.client.drain()
            for name, c in (("silent", silent), ("ponger", ponger), ("pinger", pinger), ("streamer", streamer),
                            ("keeper", keeper)):
                c.client.drain()
                if name not in disc:
                    hit = next((d for d in c.client.inbox if control("DISC")(d)), None)
                    if hit:
                        disc[name] = (round(time.monotonic() - start), len(hit))
            time.sleep(max(0.0, start + fn * GAP - time.monotonic()))
        streamer.client.send(streamer.dest, m17.stream_packet(sid, fn, signals.prng_bytes(fn, 16),
                                                              src=streamer.callsign, last=True))
        pings_after = sum(1 for d in silent.client.inbox if control("PING")(d))
        time.sleep(CACHE)
        clear(*made)
        packets = stream(talker, seed="keepalive-m")
        send(talker, packets, keeper, silent, ponger, pinger)
        other = stream(talker2, seed="keepalive-n")
        send(talker2, other, streamer)
        out = {"%s_heard" % n: relayed(c, packets) > 0
               for n, c in (("keeper", keeper), ("silent", silent), ("ponger", ponger), ("pinger", pinger))}
        out["streamer_heard"] = relayed(streamer, other) > 0
        out["silent_disc"] = "DISC %d" % disc["silent"][1] if "silent" in disc else "none"
        out["silent_disc_s"] = disc.get("silent", (None,))[0]
        out["disc_others"] = sorted(n for n in disc if n != "silent")
        out["silent_pings_total"] = pings_after
        return out
    finally:
        done(*made)


@pytest.fixture(scope="module")
def keepalive(rig):
    got = both(rig, keepalive_scenario)
    print("keepalive scenario (%d s):" % KEEPALIVE_S, got)
    return got


@pytest.mark.slow
def test_m17_unsolicited_pong_keeps_link(rig, keepalive):
    """P-M17-10 (diff): PONG every 8 s without answering PING (DroidStar)
    keeps the link."""
    same(keepalive, "ponger_heard")
    assert keepalive["ref"]["ponger_heard"], keepalive["ref"]


@pytest.mark.slow
def test_m17_client_ping_keepalive(rig, keepalive):
    """P-M17-11 (diff): a client's PING every 8 s keeps the link
    (deprecated, tolerated)."""
    same(keepalive, "pinger_heard")


@pytest.mark.slow
def test_m17_silent_client_dropped(rig, keepalive):
    """P-M17-12 (dev): a silent client is dropped after about 30 s and
    sent DISC (10 B on xlxd; the form and timing are the dev part)."""
    print("P-M17-12:", {n: (r["silent_disc"], r["silent_disc_s"]) for n, r in keepalive.items()})
    judge_dev(rig, "P-M17-12", keepalive, "silent_disc")


@pytest.mark.slow
def test_m17_dropped_client_no_traffic(rig, keepalive):
    """P-M17-13 (diff): the dropped client gets no further traffic; the
    other clients keep theirs (no DISC to them)."""
    same(keepalive, "silent_heard", "keeper_heard", "disc_others")
    assert keepalive["ref"]["keeper_heard"] and not keepalive["ref"]["disc_others"], keepalive["ref"]


@pytest.mark.slow
def test_m17_stream_resets_timeout(rig, keepalive):
    """P-M17-14 (diff): a client that talks for the whole window without
    any keepalive stays linked."""
    same(keepalive, "streamer_heard")


# ---------------------------------------------------------------- 3 disconnect

def disc_probe(ip):
    c, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, c, talker)
        first = reply_of(ask(c, m17.disc_packet(c.callsign), control("DISC")))
        again = reply_of(ask(c, m17.disc_packet(c.callsign), control("DISC")))
        time.sleep(CACHE)
        clear(c)
        packets = stream(talker)
        send(talker, packets, c)
        pinged = wait_for(c, control("PING"), 6.0) is not None
        return {"ack": first, "repeat": again, "heard": heard(c), "pinged": pinged}
    finally:
        c.dest = None
        done(c, talker)


@pytest.fixture(scope="module")
def disc(rig):
    got = marked(rig, "m17-disc", disc_probe)
    print("DISC scenario:", got)
    return got


def test_m17_disc_ack(rig, disc):
    """P-M17-15 (hb): DISC 10 B -> DISC 4 B; the client gets no further
    traffic or PING."""
    for side, r in disc.items():
        assert r["ack"] == "DISC 4" and r["heard"] == 0 and not r["pinged"], "%s: %s" % (side, r)


def test_m17_disc_repeat(rig, disc):
    """P-M17-16 (dev): a repeated DISC from the now unlinked address
    (the ack was lost): acked again or silence."""
    judge_dev(rig, "P-M17-16", disc, "repeat")


WRONG_DISC = {"disc4": m17.DISC_ACK, "disc11": m17.disc_packet(LISTENER) + b"\0"}


def wrong_disc_probe(ip, name):
    c, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, c, talker)
        reply = reply_of(ask(c, WRONG_DISC[name], control(), 0.5))
        clear(c)
        packets = stream(talker)
        send(talker, packets, c)
        return {"reply": reply, "kept": relayed(c, packets) > 0}
    finally:
        done(c, talker)


@pytest.mark.parametrize("name", list(WRONG_DISC))
def test_m17_disc_wrong_size(rig, name):
    """P-M17-17 (diff): DISC of another length gets no reply, the link stays."""
    got = marked(rig, "m17-wrong-disc-" + name, wrong_disc_probe, name)
    print("P-M17-17 %s:" % name, got)
    same(got, "reply", "kept")


# ---------------------------------------------------------------- 4 stream ingress

@pytest.fixture(scope="module")
def basic(rig):
    """One talker stream on MOD (broadcast destination, valid CRC, META
    text); the listener's inbox is the egress sample for section 5."""
    meta = b"N0CALL dvt 01"

    def probe(ip):
        talker, listener, packets = talk_probe(ip, lambda t: stream(t, meta=meta))
        try:
            got = received(listener)
            fns = [p["fn"] for p in got]
            return {"after_fn0": voice_after_fn0(listener, packets), "count": len(got),
                    "sizes": sorted({len(d) for d in listener.client.inbox if d.startswith(b"M17 ")}),
                    "max_size": max((len(d) for d in listener.client.inbox), default=0),
                    "consecutive": fns == list(range(fns[0], fns[0] + len(fns))) if fns else False,
                    "last_flag": [p["last"] for p in got].count(True) == 1 and got[-1]["last"] if got else False,
                    "sid_kept": bool(got) and all(p["stream_id"] == m17.parse_stream(packets[0])["stream_id"]
                                                  for p in got),
                    "crc_ok": bool(got) and all(p["crc_ok"] for p in got),
                    "crc_values": sorted({p["crc"] for p in got})[:3],
                    "dst": sorted({p["dst"] for p in got}), "src": sorted({p["src"] for p in got}),
                    "type": sorted({p["type"] for p in got}),
                    "meta_kept": bool(got) and all(p["meta"] == meta + b"\0" for p in got),
                    "talker_echo": heard(talker)}
        finally:
            done(talker, listener)
    got = both(rig, probe)
    print("basic stream:", got)
    return got


def test_m17_stream_relayed(rig, basic):
    """P-M17-19 (hb): a 54 B stream from a linked client reaches the
    module's other client: every voice payload after FN 0 in order (FN 0
    is B17)."""
    for side, r in basic.items():
        assert r["after_fn0"], "%s: %s" % (side, r)


def fn0_less_probe(ip):
    talker, listener, packets = talk_probe(ip, lambda t: stream(t, fn0=1))
    try:
        return {"relayed": relayed(listener, packets)}
    finally:
        done(talker, listener)


def test_m17_stream_without_fn0(rig):
    """P-M17-21 (dev): on an idle module a stream whose FN 0 was lost
    (FN starts at 1) is still relayed. Busy-module late entry stays with
    B2; the interaction with B2/B16 is noted in m17.md."""
    got = marked(rig, "m17-no-fn0", fn0_less_probe)
    print("P-M17-21 FN from 1:", got)
    judge_dev(rig, "P-M17-21", {n: {"relayed": r["relayed"] > 0} for n, r in got.items()}, "relayed")


def last_frame_probe(ip):
    t1, t2, listener = new(TALKER), new("N0TEST"), new(LISTENER)
    try:
        ready(ip, listener, t1, t2)
        first = stream(t1, seed="lf-1")
        send(t1, first, listener, wait=0.1)
        second = stream(t2, seed="lf-2")
        send(t2, second, listener)
        return {"second": relayed(listener, second) >= len(second) - 1}
    finally:
        done(t1, t2, listener)


def test_m17_last_frame_closes(rig):
    """P-M17-22 (diff): the last-frame bit ends the stream at once: a
    second talker 100 ms later is relayed (all but its FN 0)."""
    got = both(rig, last_frame_probe)
    print("P-M17-22 bit 15:", got)
    same(got, "second")


DESTINATIONS = {
    "broadcast": None,
    "designation": DESIGNATION + " " + MOD,
    "designation_other_module": DESIGNATION + " " + OTHER,
    "reflector": "XLX999  " + MOD,
    "callsign": "N0TEST",
}


@pytest.fixture(scope="module")
def destinations(rig):
    def probe(ip):
        out = {}
        talker, listener, other = new(TALKER), new(LISTENER), new(LISTENER, OTHER)
        try:
            ready(ip, listener, other, talker)
            for name, dst in DESTINATIONS.items():
                clear(listener, other)
                packets = stream(talker, dst=dst, seed="dst-" + name)
                send(talker, packets, listener, other)
                got = received(listener)
                out[name] = {"relayed": relayed(listener, packets) > 0, "other_module": relayed(other, packets) > 0,
                             "egress_dst": got[0]["dst"] if got else None}
            return out
        finally:
            done(talker, listener, other)
    got = both(rig, probe)
    print("destination forms:", got)
    return got


@pytest.mark.parametrize("name", list(DESTINATIONS))
def test_m17_destination_forms(rig, destinations, name):
    """P-M17-24 (dev): every destination form is relayed on the linked
    module (never on the module its letter names)."""
    got = {n: r[name] for n, r in destinations.items()}
    judge_dev(rig, "P-M17-24", got, "relayed", "other_module")


def test_m17_egress_destination(rig, destinations):
    """P-M17-36 (dev): the egress destination per ingress form (spec
    non-legacy: unchanged, a reflector designation -> broadcast)."""
    got = {n: {"egress_" + k: v["egress_dst"] for k, v in r.items()} for n, r in destinations.items()}
    print("P-M17-36 egress destination:", got)
    judge_dev(rig, "P-M17-36", got, *["egress_" + k for k in DESTINATIONS])


def crc_probe(ip):
    talker, listener, packets = talk_probe(ip, lambda t: stream(t, crc=0xDEAD))
    try:
        return {"relayed": relayed(listener, packets) > 0}
    finally:
        done(talker, listener)


def test_m17_bad_crc_relayed(rig):
    """P-M17-25 (diff): a wrong CRC does not stop the stream (M17Gateway
    and DroidStar never send a correct one)."""
    got = marked(rig, "m17-bad-crc", crc_probe)
    print("P-M17-25 CRC 0xDEAD:", got)
    same(got, "relayed")


TYPES = {
    "voice3200": m17.TYPE_VOICE,
    "voice_data1600": m17.TYPE_VOICE_DATA,
    "can1": m17.stream_type(can=1),
    "can5": m17.stream_type(can=5),
    "signed": m17.stream_type(signed=True),
    "scrambler": m17.stream_type(encryption=1),
    "data": m17.TYPE_DATA,
}


@pytest.fixture(scope="module")
def types(rig):
    def probe(ip):
        out = {}
        talker, listener = new(TALKER), new(LISTENER)
        try:
            ready(ip, listener, talker)
            for name, type_ in TYPES.items():
                clear(listener)
                packets = stream(talker, type_=type_, seed="type-" + name)
                send(talker, packets, listener)
                got = received(listener)
                out[name] = {"relayed": relayed(listener, packets) > 0,
                             "egress_type": got[0]["type"] if got else None}
            return out
        finally:
            done(talker, listener)
    got = marked(rig, "m17-types", probe)
    print("TYPE variants:", got)
    return got


@pytest.mark.parametrize("name", list(TYPES))
def test_m17_type_variants(rig, types, name):
    """P-M17-26 (dev): which stream TYPEs are relayed (recommended: every
    stream TYPE same-mode; encryption per module policy)."""
    got = {n: {"relayed_" + name: r[name]["relayed"]} for n, r in types.items()}
    judge_dev(rig, "P-M17-26", got, "relayed_" + name)


def source_probe(ip):
    talker, listener, packets = talk_probe(ip, lambda t: stream(t, src="N0TEST"))
    try:
        got = received(listener)
        return {"relayed": relayed(listener, packets) > 0, "src": got[0]["src"] if got else None}
    finally:
        done(talker, listener)


def test_m17_source_not_login(rig):
    """P-M17-27 (diff): a source callsign other than the linked one is
    relayed as sent (M17Gateway relays its RF users)."""
    got = both(rig, source_probe)
    print("P-M17-27 src N0TEST from N0CALL:", got)
    same(got, "relayed", "src")


def unlinked_probe(ip):
    stranger, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener)
        stranger.dest = (ip, m17.PORT)
        packets = stream(stranger)
        send(stranger, packets, listener)
        return {"heard": heard(listener)}
    finally:
        stranger.dest = None
        done(stranger, listener)


def test_m17_unlinked_stream_dropped(rig):
    """P-M17-29 (diff): stream packets from an address that never linked
    are not relayed."""
    got = marked(rig, "m17-unlinked", unlinked_probe)
    print("P-M17-29 unlinked talker:", got)
    same(got, "heard")
    assert got["ref"]["heard"] == 0, got


SIZES = (53, 55, 56)


def size_probe(ip, size):
    def packets_of(t):
        return [p[:size] if size < 54 else p + b"\x01" * (size - 54) for p in stream(t)]
    talker, listener, packets = talk_probe(ip, packets_of)
    try:
        return {"heard": heard(listener)}
    finally:
        done(talker, listener)


@pytest.mark.parametrize("size", SIZES)
def test_m17_stream_wrong_size(rig, size):
    """P-M17-31 (diff): stream packets of 53, 55 (the reflector-to-
    reflector form) or 56 B from a client are not relayed."""
    got = marked(rig, "m17-size-%d" % size, size_probe, size)
    print("P-M17-31 %d B:" % size, got)
    same(got, "heard")


# ---------------------------------------------------------------- 5 stream egress

def test_m17_egress_fn(rig, basic):
    """P-M17-34 (diff): egress packets are 54 B, FN counts up by one and
    only the last packet has bit 15."""
    same(basic, "sizes", "consecutive", "last_flag")
    assert basic["ref"]["sizes"] == [54] and basic["ref"]["consecutive"] and basic["ref"]["last_flag"], basic["ref"]


def test_m17_egress_max_size(rig, basic):
    """P-M17-45 (hb, client-driven): no datagram to a client exceeds 100 B
    (M17Gateway copies into a 100 B buffer)."""
    for side, r in basic.items():
        assert r["max_size"] <= 100, "%s: %d B datagram" % (side, r["max_size"])


def test_m17_egress_crc(rig, basic):
    """P-M17-37 (dev): the egress CRC is valid (spec: recalculated when a
    field changed); xlxd sends 0x0000."""
    print("P-M17-37 egress CRC:", {n: (r["crc_ok"], r["crc_values"]) for n, r in basic.items()})
    judge_dev(rig, "P-M17-37", basic, "crc_ok")


def test_m17_egress_type_meta(rig, basic, types):
    """P-M17-38 (dev): egress TYPE and META kept (META text, 1600 mode)."""
    got = {n: {"meta_kept": basic[n]["meta_kept"],
               "type_1600_kept": types[n]["voice_data1600"]["egress_type"] == m17.TYPE_VOICE_DATA}
           for n in basic}
    print("P-M17-38:", got, {n: r["type"] for n, r in basic.items()})
    judge_dev(rig, "P-M17-38", got, "meta_kept", "type_1600_kept")


def test_m17_egress_source(rig, basic):
    """P-M17-39 (diff): the egress source is the talker's callsign."""
    same(basic, "src")
    assert basic["ref"]["src"] == [TALKER], basic["ref"]


def late_link_probe(ip):
    talker, joiner = new(TALKER), new(LISTENER)
    try:
        ready(ip, talker)
        packets = stream(talker, n=75, seed="late")
        joiner.dest = (ip, m17.PORT)
        start = time.monotonic()
        acked = first = None
        for i, data in enumerate(packets):
            while time.monotonic() < start + i * GAP:
                joiner.client.drain()
                time.sleep(0.002)
            talker.client.send(talker.dest, data)
            if i == 25:
                joiner.client.send(joiner.dest, m17.conn_packet(joiner.callsign, MOD))
            joiner.client.drain()
            for d, t in zip(joiner.client.inbox, joiner.client.inbox_times):
                if acked is None and control("ACKN")(d):
                    acked = t
                if first is None and acked is not None and m17.parse_stream(d):
                    first = t
        settle(joiner, talker)
        delay = None if acked is None or first is None else round((first - acked) * 1000)
        return {"delay_ms": delay, "within_200ms": delay is not None and delay <= 200}
    finally:
        done(talker, joiner)


def test_m17_late_link_receives(rig):
    """P-M17-40 (dev): a client that links mid-stream receives from the
    next packet (xlxd refreshes its recipient list once per second)."""
    got = both(rig, late_link_probe)
    print("P-M17-40 late link:", got)
    judge_dev(rig, "P-M17-40", got, "within_200ms")


# ---------------------------------------------------------------- 6 packet mode, peers, garbage

def m17p_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        sms = m17.m17p_packet(0x05, b"N0CALL dvt\0", src=TALKER)
        talker.client.send(talker.dest, sms)
        settle(listener, talker)
        got = [d for d in listener.client.inbox if d.startswith(b"M17P")]
        return {"forwarded": bool(got), "unchanged": bool(got) and got[0] == sms}
    finally:
        done(talker, listener)


def test_m17_packet_mode(rig):
    """P-M17-41 (dev): M17P from a linked client is forwarded unchanged
    (non-legacy reflectors); xlxd drops it."""
    got = marked(rig, "m17-m17p", m17p_probe)
    print("P-M17-41 M17P:", got)
    judge_dev(rig, "P-M17-41", got, "forwarded", "unchanged")


def parrot_probe(ip):
    talker = new(TALKER)
    try:
        ready(ip, talker)
        packets = stream(talker, dst="PARROT")
        send(talker, packets, wait=3.0)
        return {"echo": heard(talker) > 0}
    finally:
        done(talker)


def test_m17_parrot(rig):
    """P-M17-42 (dev, record): a stream to PARROT is echoed to the sender
    (mrefd; M17Gateway's fork probes with it)."""
    got = marked(rig, "m17-parrot", parrot_probe)
    print("P-M17-42 PARROT:", got)
    judge_dev(rig, "P-M17-42", got, "echo")


GARBAGE = {
    "unknown": b"XXXX" + m17.encode_callsign(TALKER),
    "short3": b"M17",
    "byte1": b"\x00",
    "oversize": b"M17 " + bytes(296),
}


def garbage_probe(ip, name):
    c, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, c, talker)
        reply = reply_of(ask(c, GARBAGE[name], seconds=0.5))
        clear(c)
        packets = stream(talker)
        send(talker, packets, c)
        return {"reply": reply, "kept": relayed(c, packets) > 0}
    finally:
        done(c, talker)


@pytest.mark.parametrize("name", list(GARBAGE))
def test_m17_garbage_ignored(rig, name):
    """P-M17-43 (diff): unknown, short and oversize packets get no reply;
    the reflector keeps serving."""
    got = marked(rig, "m17-garbage-" + name, garbage_probe, name)
    print("P-M17-43 %s:" % name, got)
    same(got, "reply", "kept")


def peer_probe(ip):
    c = new()
    try:
        return {"reply": connect(ip, c, m17.peer_conn_packet(DESIGNATION, MOD + OTHER))}
    finally:
        c.client.close()


def test_m17_peer_conn_unconfigured(rig):
    """P-M17-44 (dev, record): a 37 B reflector CONN from an unconfigured
    peer links nothing (NACK or silence)."""
    got = marked(rig, "m17-peer-conn", peer_probe)
    print("P-M17-44 peer CONN:", got)
    for side, r in got.items():
        assert not r["reply"].startswith("ACKN"), "%s: %s" % (side, r)
    judge_dev(rig, "P-M17-44", got, "reply")


# ---------------------------------------------------------------- restart (last: it restarts both reflectors)

def wait_conn(ip, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        c = new()
        try:
            if connect(ip, c) == "ACKN 4":
                return True
        finally:
            done(c)
        time.sleep(1)
    return False


@pytest.mark.slow
def test_m17_disc_on_shutdown(rig, rig_control):
    """P-M17-18 (dev): on shutdown the reflector sends DISC 10 B to its
    clients (xlxd does; mrefd and URFd do not)."""
    linked = {}
    for side in ("ref", "sut"):
        c = new()
        ready(getattr(rig, "xlxd_" + side), c)
        linked[side] = c
    got = {}
    try:
        for side, c in linked.items():
            rig_control("restart", "xlxd-" + side)
            hit = wait_for(c, control("DISC"), 2.0)
            got[side] = {"disc": reply_of(hit) if hit else "none"}
    finally:
        for c in linked.values():
            c.dest = None
            done(c)
    up = {side: wait_conn(getattr(rig, "xlxd_" + side)) for side in linked}
    time.sleep(5)                           # let the reflectors reach their transcoders again
    print("P-M17-18 DISC on restart:", got, "back up:", up)
    assert all(up.values()), up
    judge_dev(rig, "P-M17-18", got, "disc")
