# SPDX-License-Identifier: MIT
"""Helpers shared by the M17 case files (test_m17.py: stage 1, clients and
two reflectors; test_m17_peer*.py: stage 2, configured reflector peers).
Moved out of test_m17.py unchanged."""
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import m17

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
# Destination of the relay probes: broadcast (the spec's form, accepted by
# xlxd and mrefd). URFd routes by the destination's module letter and drops
# destinations that are not a callsign (broadcast, M17-xxx): with
# DVT_REF_KIND=urfd the probes address "XLX999 <module>" (module at
# position 8) so the other rows can be compared at all; P-M17-24/36 still
# probe every form.
STREAM_DST = {"urfd": False}


def default_dst(module):
    return "XLX999 " + module if STREAM_DST["urfd"] else None


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


def not_keepalive(data):
    """Any datagram except the reflector's periodic PING (a reply check
    must not count the keepalive that happens to arrive meanwhile)."""
    return not control("PING")(data)


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
    fields.setdefault("dst", default_dst(c.module))
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


# ---------------------------------------------------------------- reflector peers (stage 2)

def peer_streams(link, since=None):
    """Stream datagrams (54 B and 55 B) a PeerLink received since a time, as
    parse_stream dicts with "trailer" (the module letter of a 55 B datagram,
    else None), "size", "t" and "raw"."""
    out = []
    for rx in link.datagrams(since=since, kind="STREAM"):
        body, letter = m17.strip_trailer(rx.data) if rx.size == m17.STREAM_TRAILER_SIZE else (rx.data, None)
        parsed = m17.parse_stream(body)
        parsed.update(trailer=letter, size=rx.size, t=rx.t, raw=rx.data)
        out.append(parsed)
    return out


def peer_m17p(link, since=None):
    """M17P datagrams a PeerLink received since a time, as parse_m17p dicts
    (trailer detected) with "t" and "raw"."""
    out = []
    for rx in link.datagrams(since=since, kind="M17P"):
        parsed = m17.parse_m17p(rx.data)
        if parsed:
            parsed.update(t=rx.t, raw=rx.data)
            out.append(parsed)
    return out


def send_from_peer(link, packets, letter=None, *listeners, gap=GAP, wait=TAIL):
    """The fake peer sends packets to its reflector at gap spacing from its
    bound port, each with the module trailer letter (None: bare, as
    given), draining the listener clients (M17Client) meanwhile; then
    settles for wait seconds."""
    start = time.monotonic()
    for index, data in enumerate(packets):
        delay = start + index * gap - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        link.send(m17.with_trailer(data, letter) if letter is not None else data)
        for listener in listeners:
            listener.client.drain()
    settle(*listeners, seconds=wait)
