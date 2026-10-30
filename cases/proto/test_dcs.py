# SPDX-License-Identifier: MIT
"""DCS reflector conformance (docs/protocols/dcs.md): one case per
obligation P-DCS-NN, xlxd-ref and xlxd-sut probed in parallel with DCS
clients in the forms real gateways send (ircDDBGateway, DroidStar,
QnetGateway).

Judged as in dcs.md: hb = a spec requirement, asserted on both sides;
dev = disputed (or xlxd deviates from spec or clients): sut == ref under
the xlxd profile, else the profile's "dcs" spec entry {"P-DCS-NN":
{observed key: value}} (skip "spec pending" without one); diff = sut ==
ref. Every case prints what both sides did.
"""
import contextlib
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import UdpClient
from dvtest.proto import dcs, dextra

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "J", "K"
REFL = dcs.REFLECTOR                        # xlxd's DCS name for XLX999
TALKER, LISTENER, THIRD = "N0CALL", "N0TEST", "STR001"
FRAMES = 63                                 # three superframes: xlxd's loop guard
                                            # strikes a callsign for overs under 50 frames
GAP = 0.020
ACK_WAIT = 3.0                              # xlxd delays an ACK by up to 2 s
CACHE = 1.2                                 # xlxd refreshes its module client list once per second
TAIL = 0.8
TIMEOUT_WAIT = 70.0                         # past the 60 s client timeout


def both(rig, func, *args, **kw):
    """{"ref": func(ref ip, *args, **kw), "sut": ...} run in parallel."""
    got = {}

    def side(name):
        try:
            got[name] = func(getattr(rig, "xlxd_" + name), *args, **kw)
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
    profile's dcs spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "dcs") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no dcs %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


@contextlib.contextmanager
def marked(rig, tag):
    """Control channel "mark begin|end <tag>" around deliberately
    malformed tester traffic, so dvcheck reports it as a warning (no-op
    without the control channel)."""
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
    name = "dcs-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def frames(n=FRAMES, seed="dcs"):
    return signals.prng_frames("dstar", n, seed=seed)


# ---------------------------------------------------------------- clients

class Gw:
    """One DCS socket. answer=True replies to the reflector's 22 B poll
    with the 17 B keepalive, as ircDDBGateway does (for whichever own
    module the poll names, so one socket can hold several links)."""

    def __init__(self, callsign=TALKER, own=MOD, module=MOD, answer=True):
        self.callsign, self.own, self.module = callsign, own, module
        self.client = UdpClient("0.0.0.0", responder=self._poll if answer else None)
        self.dest = None

    def _poll(self, data, _addr):
        reply = dcs.parse_reply(data)
        if reply["kind"] == "poll":
            own = reply["own_module"].decode("ascii", "replace")
            return dcs.keepalive17(self.callsign, own, REFL, reply["module"].decode("ascii", "replace"))
        return None

    @property
    def port(self):
        return self.client.local[1]

    def send(self, packet):
        self.client.send(self.dest, packet)


def wait_for(gw, pred, seconds=1.0):
    """(packet, source address) of the first inbox packet matching pred."""
    deadline = time.monotonic() + seconds
    while True:
        gw.client.drain()
        for data in gw.client.inbox:
            if pred(data):
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def kind_is(*kinds):
    return lambda data: dcs.parse_reply(data)["kind"] in kinds


def ask(gw, packet, pred, seconds=1.0):
    clear(gw)
    gw.send(packet)
    return wait_for(gw, pred, seconds)


def link(ip, gw, packet=None, seconds=ACK_WAIT):
    """Send a link (gateway form by default); the 14 B reply or None."""
    gw.dest = (ip, dcs.PORT)
    packet = packet or dcs.gateway_link(gw.callsign, gw.own, gw.module)
    reply = ask(gw, packet, kind_is("ack", "nak"), seconds)
    return dcs.parse_reply(reply) if reply else None


def ready(ip, *gws):
    for gw in gws:
        reply = link(ip, gw)
        assert reply and reply["kind"] == "ack", "%s: %s not ACKed (%s)" % (ip, gw.callsign, reply)
    time.sleep(CACHE)
    clear(*gws)
    return gws


def clear(*gws):
    for gw in gws:
        gw.client.drain()
        gw.client.inbox.clear()
        gw.client.inbox_times.clear()


def settle(*gws, seconds=TAIL):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for gw in gws:
            gw.client.drain()
        time.sleep(0.01)


def done(*gws):
    """Unlink every gateway that has a destination (19 B form), then close."""
    for gw in gws:
        try:
            if gw.dest:
                gw.send(dcs.unlink19(gw.callsign, gw.own))
        finally:
            gw.client.close()


def send(gw, packets, *listeners, wait=TAIL):
    gw.client.paced(gw.dest, packets, GAP, *[m.client for m in listeners])
    settle(gw, *listeners, seconds=wait)


def talk(gw, *listeners, voice=None, wait=TAIL, sid=None, **fields):
    """A complete over of voice (default FRAMES frames) on gw's module."""
    voice = voice or frames()
    sid = sid or clients.fresh_stream_id("dstar")
    fields.setdefault("callsign", gw.callsign)
    fields.setdefault("module", gw.module)
    send(gw, dcs.stream_packets(sid, voice, **fields), *listeners, wait=wait)
    return voice, sid


def heard(gw):
    """Parsed voice packets of the inbox, in arrival order."""
    out = []
    for data in gw.client.inbox:
        reply = dcs.parse_reply(data)
        if reply["kind"] == "voice":
            out.append(reply)
    return out


def voice_frames(gw):
    return [p["frame"] for p in heard(gw) if not p["last"]]


def replies(gw, *kinds):
    return [dcs.parse_reply(d) for d in gw.client.inbox if dcs.parse_reply(d)["kind"] in kinds]


# ---------------------------------------------------------------- 1 link

def link_probe(ip):
    gw = Gw(LISTENER)
    try:
        start = time.monotonic()
        reply = link(ip, gw)
        latency = time.monotonic() - start
        return {"reply": reply["kind"] if reply else "none", "callsign": reply and reply["callsign"],
                "byte7": reply and reply["byte7"], "own": reply and reply["own_module"],
                "module": reply and reply["module"], "latency": round(latency, 2)}
    finally:
        done(gw)


def test_dcs_link_ack(rig):
    """P-DCS-01, -07 (diff): a 519 B gateway-form link from N0TEST (no
    registration) is answered with the 14 B ACK: callsign, space, own
    module, reflector module."""
    got = both(rig, link_probe)
    print("P-DCS-01 link:", got)
    for name, r in got.items():
        assert r["reply"] == "ack", "%s: link not ACKed: %s" % (name, r)
        assert (r["callsign"], r["byte7"], r["own"], r["module"]) == (
            LISTENER.encode()[:7].ljust(7), b" ", MOD.encode(), MOD.encode()), "%s: ACK fields %s" % (name, r)


def test_dcs_ack_latency(rig):
    """P-DCS-02 (dev): ACK latency. Both sides answer within 3 s; a
    profile spec {"max_s": x} bounds the sut (xlxd: random 0-2 s)."""
    got = both(rig, link_probe)
    print("P-DCS-02 ACK latency s:", {k: v["latency"] for k, v in got.items()})
    for name, r in got.items():
        assert r["reply"] == "ack" and r["latency"] < ACK_WAIT, "%s: %s" % (name, r)
    if rig.sut_profile != specs.DEFAULT:
        entry = (specs.rule(rig, "dcs") or {}).get("P-DCS-02")
        if not entry:
            pytest.skip("spec pending: no dcs P-DCS-02 entry in dvtest/specs/%s.py" % rig.sut_profile)
        assert got["sut"]["latency"] <= entry["max_s"], "sut ACK after %.2f s" % got["sut"]["latency"]


def source_probe(ip):
    """Source port of the ACK and of the polls."""
    gw = Gw(LISTENER)
    gw.dest = (ip, dcs.PORT)
    try:
        sources = set()
        gw.send(dcs.gateway_link(gw.callsign, gw.own, gw.module))
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                data, addr = gw.client.sock.recvfrom(2048)
            except OSError:
                continue
            kind = dcs.parse_reply(data)["kind"]
            if kind in ("ack", "poll"):
                sources.add((kind, addr[1]))
        return {"sources": sorted(sources)}
    finally:
        done(gw)


def test_dcs_source_port(rig):
    """P-DCS-44 (diff): ACK and polls come from port 30051 (ircDDBGateway
    and QnetGateway accept nothing else)."""
    got = both(rig, source_probe)
    print("P-DCS-44 sources:", got)
    for name, r in got.items():
        assert r["sources"] and all(port == dcs.PORT for _kind, port in r["sources"]), "%s: %s" % (name, r)
    same(got, "sources")


def minimal_probe(ip):
    gw = Gw(LISTENER)
    try:
        reply = link(ip, gw, dcs.droidstar_link(gw.callsign, gw.module))
        return {"reply": reply["kind"] if reply else "none"}
    finally:
        done(gw)


def test_dcs_link_minimal_body(rig):
    """P-DCS-03 (diff): DroidStar's link (byte 10 = 0x0B, no reflector
    name, no text) is ACKed."""
    got = both(rig, minimal_probe)
    print("P-DCS-03 DroidStar link:", got)
    assert got["ref"]["reply"] == "ack", "ref: %s" % got["ref"]
    same(got, "reply")


def module_probe(ip, module):
    gw = Gw(LISTENER, module=module)
    try:
        reply = link(ip, gw)
        return {"reply": reply["kind"] if reply else "none"}
    finally:
        done(gw)


def unserved_letter():
    """DVT_UNSERVED_MODULE: a letter the sut is started without (the rig
    then leaves it out of the sut's rooms); empty when unset."""
    return os.environ.get("DVT_UNSERVED_MODULE", "").strip().upper()[:1]


def test_dcs_link_unserved_module_nak(rig):
    """P-DCS-04 (diff): a link to a module the reflector does not serve
    gets NAK. Without DVT_UNSERVED_MODULE both rig reflectors serve A-Z, so
    module Z is probed and the answer recorded (the case skips). With it,
    the sut must NAK that letter (the profile's "reply" when its spec
    names one); xlxd-ref is recorded."""
    letter = unserved_letter()
    if letter:
        got = both(rig, module_probe, letter)
        print("P-DCS-04 module %s (unserved on the sut):" % letter, got)
        if got["sut"]["reply"] == "ack" and rig.sut_profile == specs.DEFAULT:
            pytest.skip("DVT_UNSERVED_MODULE=%s has no effect on this sut (it serves %s)" % (letter, letter))
        want = ((specs.rule(rig, "dcs") or {}).get("P-DCS-04") or {}).get("reply", "nak")
        assert got["sut"]["reply"] == want, "sut: link to unserved %s got %s, want %s" % (
            letter, got["sut"]["reply"], want)
        return
    got = both(rig, module_probe, "Z")
    print("P-DCS-04 module Z:", got)
    same(got, "reply")
    if got["ref"]["reply"] == "ack":
        pytest.skip("every module A-Z is served on this rig: no unserved module to probe")


@pytest.mark.parametrize("what", ["callsign", "module"])
def test_dcs_link_invalid(rig, what):
    """P-DCS-05 (dev): a link with an invalid callsign or a non-letter
    module never links; the reply (NAK or silence) is the dev part. One
    try per case: xlxd ignores the IP for minutes after three."""
    def probe(ip):
        gw = Gw(LISTENER)
        try:
            head = b"N0#EST  JJ" if what == "callsign" else b"N0TEST  J1"
            reply = link(ip, gw, dcs.raw_link(head))
            return {"reply": reply["kind"] if reply else "none"}
        finally:
            gw.dest = None
            done(gw)
    with marked(rig, "dcs-link-invalid"):
        got = both(rig, probe)
    print("P-DCS-05 invalid %s:" % what, got)
    for name, r in got.items():
        assert r["reply"] != "ack", "%s: invalid link ACKed" % name
    judge_dev(rig, "P-DCS-05", got, "reply")


def relink_probe(ip):
    gw, talker = Gw(LISTENER), Gw(TALKER)
    try:
        gw.dest = (ip, dcs.PORT)
        packet = dcs.gateway_link(gw.callsign, gw.own, gw.module)
        clear(gw)
        gw.send(packet)
        time.sleep(0.05)
        gw.send(packet)                         # ircDDBGateway resends after 1 s; a copy at once here
        settle(gw, seconds=ACK_WAIT)
        acks = len(replies(gw, "ack"))
        ready(ip, talker)
        clear(gw)
        voice, _sid = talk(talker, gw)
        got = voice_frames(gw)
        return {"acks": acks, "copies": round(len(got) / len(voice), 2)}
    finally:
        done(gw, talker)


def test_dcs_relink_single_client(rig):
    """P-DCS-06 (dev): a second link copy: ACKs counted; one client
    (traffic once) is required on both sides."""
    with marked(rig, "dcs-relink"):
        got = both(rig, relink_probe)
    print("P-DCS-06 repeated link:", got)
    for name, r in got.items():
        assert r["acks"] >= 1 and r["copies"] == 1.0, "%s: %s" % (name, r)
    judge_dev(rig, "P-DCS-06", got, "acks")


def wrong_size_probe(ip):
    gw = Gw(LISTENER)
    try:
        reply = link(ip, gw, b"N0TEST  JJ\x00", seconds=ACK_WAIT)
        return {"reply": reply["kind"] if reply else "none"}
    finally:
        gw.dest = None
        done(gw)


def test_dcs_link_wrong_size(rig):
    """P-DCS-08 (diff): an 11 B link form (DExtra-style, module byte not a
    space) does not link."""
    with marked(rig, "dcs-link-11b"):
        got = both(rig, wrong_size_probe)
    print("P-DCS-08 11 B link:", got)
    assert got["ref"]["reply"] != "ack"
    same(got, "reply")


# ---------------------------------------------------------------- 2 unlink

@pytest.mark.parametrize("size", [11, 19])
def test_dcs_unlink_nak(rig, size):
    """P-DCS-09 (diff), P-DCS-10 (B5): an 11 B or 19 B unlink removes the
    client and is answered with a 14 B NAK whose byte 9 is a space; no
    voice reaches the client afterwards."""
    def probe(ip):
        gw, talker = Gw(LISTENER), Gw(TALKER)
        try:
            ready(ip, gw, talker)
            packet = dcs.unlink11(gw.callsign, gw.own) if size == 11 else dcs.unlink19(gw.callsign, gw.own)
            reply = ask(gw, packet, kind_is("ack", "nak"), 2.0)
            reply = dcs.parse_reply(reply) if reply else None
            time.sleep(CACHE)
            clear(gw)
            talk(talker, gw)
            return {"reply": reply["kind"] if reply else "none", "byte9": reply and reply["module"],
                    "heard_after": len(voice_frames(gw))}
        finally:
            gw.dest = None
            done(gw, talker)
    got = both(rig, probe)
    print("P-DCS-09 unlink %d B:" % size, got)
    for name, r in got.items():
        assert r["heard_after"] == 0, "%s: %d frames after the unlink" % (name, r["heard_after"])
    assert got["ref"]["reply"] == "nak", "ref: %s" % got["ref"]
    same(got, "reply", "byte9")


def unlink_talking_probe(ip):
    talker, listener = Gw("STR010"), Gw(LISTENER)
    third = Gw(THIRD)
    try:
        ready(ip, talker, listener, third)
        voice = frames()
        sid = clients.fresh_stream_id("dstar")
        packets = dcs.stream_packets(sid, voice, last=False, callsign="STR010", module=MOD)
        send(talker, packets[:20], listener, wait=0.0)
        talker.send(dcs.unlink19(talker.callsign, talker.own))
        reply = wait_for(talker, kind_is("nak"), 1.0)
        settle(talker, listener, seconds=2.5)            # past the stream timeout either way
        clear(talker, listener)
        talk(third, talker, listener)
        return {"nak": reply is not None, "talker_hears_after": len(voice_frames(talker)) > 0,
                "listener_hears_next": len(voice_frames(listener)) > 0}
    finally:
        talker.dest = None
        done(talker, listener, third)


def test_dcs_unlink_while_talking(rig):
    """P-DCS-11 (dev): the talker unlinks mid-stream; is it removed
    (hears nothing of the next stream) and is the module free?"""
    with marked(rig, "dcs-unlink-talking"):
        got = both(rig, unlink_talking_probe)
    print("P-DCS-11 unlink while talking:", got)
    for name, r in got.items():
        assert r["listener_hears_next"], "%s: module not free after the talker left" % name
    judge_dev(rig, "P-DCS-11", got, "nak", "talker_hears_after")


def unlink_unknown_probe(ip):
    gw = Gw(LISTENER)
    gw.dest = (ip, dcs.PORT)
    try:
        reply = ask(gw, dcs.unlink19(gw.callsign, gw.own), kind_is("ack", "nak"), 2.0)
        return {"reply": dcs.parse_reply(reply)["kind"] if reply else "none"}
    finally:
        gw.dest = None
        done(gw)


def test_dcs_unlink_not_linked(rig):
    """P-DCS-12 (dev): an unlink from an address that is not linked; the
    reply (silence or NAK) is recorded."""
    with marked(rig, "dcs-unlink-unknown"):
        got = both(rig, unlink_unknown_probe)
    print("P-DCS-12 unlink, not linked:", got)
    judge_dev(rig, "P-DCS-12", got, "reply")


# ---------------------------------------------------------------- 3 keepalive

def poll_probe(ip):
    gw = Gw(LISTENER)
    try:
        ready(ip, gw)
        start = time.monotonic()
        settle(gw, seconds=16.0)
        polls, stamps, ref9 = [], [], 0
        for data, stamp in zip(gw.client.inbox, gw.client.inbox_times):
            reply = dcs.parse_reply(data)
            if reply["kind"] == "poll":
                polls.append(reply)
                stamps.append(stamp)
            elif reply["kind"] == "ref9":
                ref9 += 1
        gaps = [round(b - a, 1) for a, b in zip(stamps, stamps[1:])]
        first = polls[0] if polls else {}
        return {"polls": len(polls), "max_gap": max(gaps) if gaps else None,
                "first_after": round(stamps[0] - start, 1) if stamps else None,
                "reflector": first.get("reflector"), "module": first.get("module"),
                "byte8": first.get("byte8"), "callsign": first.get("callsign"),
                "byte16": first.get("byte16"), "own_module": first.get("own_module"),
                "tail": first.get("tail"), "ref9_per_poll": round(ref9 / len(polls), 1) if polls else None}
    finally:
        done(gw)


def test_dcs_server_poll(rig):
    """P-DCS-13 (diff + dev), P-DCS-14 (diff), P-DCS-15 (dev): the 22 B
    poll comes at least every 5 s with DCSnnn + linked module and the
    client's callsign + module; the extra 9 B packet is recorded."""
    got = both(rig, poll_probe)
    print("P-DCS-13/14/15 poll:", got)
    for name, r in got.items():
        assert r["polls"] >= 2 and r["max_gap"] is not None and r["max_gap"] <= 5.5, "%s: %s" % (name, r)
        assert r["reflector"].startswith(b"DCS") and r["module"] == MOD.encode(), "%s: %s" % (name, r)
        assert r["callsign"] == LISTENER.encode()[:7].ljust(7) and r["own_module"] == MOD.encode(), name
    same(got, "reflector", "module", "byte8", "callsign", "byte16", "own_module", "tail")
    judge_dev(rig, "P-DCS-15", got, "ref9_per_poll")


KEEPALIVE_FORMS = {
    # name: (callsign, packet builder or None for silence)
    "gateway17": ("STR002", lambda cs: dcs.keepalive17(cs, MOD, REFL, MOD)),
    "droidstar17": ("STR003", lambda cs: dcs.keepalive17(cs, MOD, REFL, form="droidstar")),
    "size9": ("STR004", lambda cs: (cs.encode() + b" " * 8)[:8] + b"\x00"),
    "size19": ("STR005", lambda cs: (cs.encode() + b" " * 7)[:7] + b" " + MOD.encode() + b"\x00"
               + (REFL.encode() + b" " * 7)[:7] + b" \x00"),
    "size22": ("STR006", lambda cs: (cs.encode() + b" " * 7)[:7] + MOD.encode() + b" "
               + (REFL.encode() + b" " * 7)[:7] + MOD.encode() + MOD.encode() + b"\x0a\x00\x20\x20"),
    "silent": ("STR007", None),
}


def keepalive_probe(ip):
    """Every form at once, one client each, none answering polls; a talker
    keeps talking on OTHER for the whole time (stream owner); after the
    client timeout a fresh stream on MOD shows who is still linked."""
    gws = {name: Gw(cs, answer=False) for name, (cs, _b) in KEEPALIVE_FORMS.items()}
    talker = Gw(TALKER)
    long_talker, long_listener = Gw(THIRD, OTHER, OTHER, answer=False), Gw("STR008", OTHER, OTHER)
    try:
        ready(ip, talker, long_listener, long_talker, *gws.values())
        voice = frames(int(TIMEOUT_WAIT / GAP))
        sid = clients.fresh_stream_id("dstar")
        packets = dcs.stream_packets(sid, voice, callsign=THIRD, module=OTHER)
        stop = threading.Event()

        def keepalives():
            while not stop.wait(10.0):
                for name, (cs, build) in KEEPALIVE_FORMS.items():
                    if build:
                        gws[name].send(build(cs))
        ticker = threading.Thread(target=keepalives, daemon=True)
        ticker.start()
        send(long_talker, packets, long_listener, talker, *gws.values(), wait=0.5)
        stop.set()
        ticker.join()
        long_heard = len(voice_frames(long_listener))
        clear(*gws.values())
        talk(talker, *gws.values())
        out = {name: len(voice_frames(gw)) > 0 for name, gw in gws.items()}
        notices = replies(gws["silent"], "notice", "nak")
        out["silent_notice"] = notices[0]["kind"] + ":%d" % len(notices[0].get("raw", b"x" * 14)) if notices else "none"
        out["long_talker_whole"] = long_heard >= len(voice) - 2
        return out
    finally:
        done(talker, long_talker, long_listener, *gws.values())


@pytest.mark.slow
def test_dcs_keepalive_forms(rig):
    """P-DCS-16 (diff), P-DCS-17 (dev), P-DCS-18 (dev), P-DCS-19 (diff):
    after the client timeout, a client sending the 17 B keepalive (gateway
    or DroidStar form) still hears its module, a silent one does not (its
    notice is recorded), the other sizes are recorded, and a client that
    talks the whole time is never dropped."""
    with marked(rig, "dcs-keepalive-forms"):
        got = both(rig, keepalive_probe)
    print("P-DCS-16..19 keepalive forms:", got)
    for name, r in got.items():
        assert r["gateway17"] and r["droidstar17"], "%s: 17 B keepalive did not keep the link: %s" % (name, r)
        assert not r["silent"], "%s: silent client still linked after %.0f s" % (name, TIMEOUT_WAIT)
        assert r["long_talker_whole"], "%s: the talking client was dropped" % name
    judge_dev(rig, "P-DCS-17", got, "size9", "size19", "size22")
    judge_dev(rig, "P-DCS-18", got, "silent_notice")


def keepalive_unlinked_probe(ip):
    stray, talker = Gw("STR009", answer=False), Gw(TALKER)
    stray.dest = (ip, dcs.PORT)
    try:
        ready(ip, talker)
        stray.send(dcs.keepalive17(stray.callsign, MOD, REFL, MOD))
        settle(stray, seconds=CACHE + 5.5)
        polls = len(replies(stray, "poll"))
        talk(talker, stray)
        return {"polls": polls, "heard": len(voice_frames(stray))}
    finally:
        stray.dest = None
        done(stray, talker)


def test_dcs_keepalive_without_link(rig):
    """P-DCS-20 (diff): a keepalive from an address that is not linked
    creates no link: no poll, no voice."""
    got = both(rig, keepalive_unlinked_probe)
    print("P-DCS-20 keepalive without link:", got)
    for name, r in got.items():
        assert r == {"polls": 0, "heard": 0}, "%s: %s" % (name, r)


# ---------------------------------------------------------------- 4 malformed

GARBAGE = {
    "short1": b"\x00",
    "short3": b"abc",
    "eeee": b"EEEE" + bytes(31),
    "status35": b"DCS999 J" + bytes(27),
    "zero15": bytes(15),
    "oversize": b"0001" + bytes(1396),
}


@pytest.mark.parametrize("name", list(GARBAGE))
def test_dcs_garbage_ignored(rig, name):
    """P-DCS-21 (diff): unknown sizes, EEEE / 35 B status and 15 B zero
    packets get no reply; the reflector still ACKs a link afterwards."""
    def probe(ip):
        gw = Gw(LISTENER)
        gw.dest = (ip, dcs.PORT)
        try:
            reply = ask(gw, GARBAGE[name], lambda d: True, 1.0)
            after = link(ip, gw)
            return {"reply": dcs.parse_reply(reply)["kind"] if reply else "none",
                    "link_after": after["kind"] if after else "none"}
        finally:
            done(gw)
    with marked(rig, "dcs-garbage"):
        got = both(rig, probe)
    print("P-DCS-21 %s:" % name, got)
    for side, r in got.items():
        assert r["reply"] == "none" and r["link_after"] == "ack", "%s: %s" % (side, r)


@pytest.mark.parametrize("size", [99, 101])
def test_dcs_voice_size(rig, size):
    """P-DCS-22 (dev): voice packets of 99 or 101 B from a linked talker:
    frames heard recorded (clients send exactly 100 B)."""
    def probe(ip):
        talker, listener = Gw(TALKER), Gw(LISTENER)
        try:
            ready(ip, talker, listener)
            sid = clients.fresh_stream_id("dstar")
            packets = dcs.stream_packets(sid, frames(), callsign=TALKER, module=MOD)
            packets = [p[:99] if size == 99 else p + b"\x00" for p in packets]
            send(talker, packets, listener)
            return {"heard": len(voice_frames(listener))}
        finally:
            done(talker, listener)
    with marked(rig, "dcs-voice-size"):
        got = both(rig, probe)
    print("P-DCS-22 voice %d B:" % size, got)
    if size == 99:
        for side, r in got.items():
            assert r["heard"] == 0, "%s: short voice relayed" % side
    judge_dev(rig, "P-DCS-22", got, "heard")


def not_linked_probe(ip):
    stray, listener = Gw("STR009"), Gw(LISTENER)
    stray.dest = (ip, dcs.PORT)
    try:
        ready(ip, listener)
        talk(stray, listener)
        return {"heard": len(voice_frames(listener))}
    finally:
        stray.dest = None
        done(stray, listener)


def test_dcs_voice_not_linked(rig):
    """P-DCS-23 (diff): voice from an address that is not linked is not
    relayed."""
    with marked(rig, "dcs-voice-unlinked"):
        got = both(rig, not_linked_probe)
    print("P-DCS-23 voice, not linked:", got)
    for side, r in got.items():
        assert r["heard"] == 0, "%s: %s" % (side, r)


# ---------------------------------------------------------------- 5 voice

def stream_probe(ip, start=0, **fields):
    """A talker and a listener on MOD; one over (start sequence, fields);
    returns what the listener and the talker got."""
    talker, listener = Gw(TALKER), Gw(LISTENER)
    try:
        ready(ip, talker, listener)
        voice, sid = talk(talker, listener, start=start, **fields)
        return {"sent": voice, "sid": sid, "rx": heard(listener), "echo": len(heard(talker))}
    finally:
        done(talker, listener)


def test_dcs_stream_opens_mid_superframe(rig):
    """P-DCS-24 (diff): a stream whose first packet has sequence 7 (no
    separate header in DCS) is relayed from its first frame."""
    with marked(rig, "dcs-mid-superframe"):
        got = both(rig, stream_probe, 7)
    rows = {k: {"heard": len([p for p in v["rx"] if not p["last"]]), "sent": len(v["sent"])} for k, v in got.items()}
    print("P-DCS-24 mid-superframe start:", rows)
    assert rows["ref"]["heard"] > 0
    same(rows, "heard")


def test_dcs_egress_fields(rig):
    """P-DCS-25 (diff + dev), P-DCS-33 (dev): egress packets are 100 B with
    RPT2 = DCSnnn + module; RPT1, UR, MY passed; MY suffix (sent empty)
    and flags (sent 08 00 00) recorded."""
    got = both(rig, stream_probe, 0, flags=b"\x08\x00\x00")
    rows = {}
    for name, r in got.items():
        first = r["rx"][0] if r["rx"] else {}
        rows[name] = {"sizes": sorted({p["size"] for p in r["rx"]}), "rpt2": first.get("rpt2"),
                      "rpt1": first.get("rpt1"), "ur": first.get("ur"), "my": first.get("my"),
                      "suffix": first.get("suffix"), "flags": first.get("flags")}
    print("P-DCS-25/33 egress fields:", rows)
    for name, r in rows.items():
        assert r["sizes"] == [100] and r["rpt2"] == b"DCS999 " + MOD.encode(), "%s: %s" % (name, r)
    same(rows, "rpt1", "ur", "my")
    judge_dev(rig, "P-DCS-25", rows, "suffix")
    judge_dev(rig, "P-DCS-33", rows, "flags")


def test_dcs_egress_sequence(rig):
    """P-DCS-26 (hb), P-DCS-27 (hb): egress sequence starts at 0, counts
    0..20 in order, the last packet carries 0x40; sequence 0 packets carry
    the sync 55 2D 16 and no other packet does."""
    got = both(rig, stream_probe)
    for name, r in got.items():
        seqs = [p["seq_byte"] for p in r["rx"]]
        print("P-DCS-26 %s sequence:" % name, seqs)
        assert seqs and seqs[0] == 0, "%s: egress starts at %s" % (name, seqs[:1])
        body = [s for s in seqs if not s & 0x40]
        assert body == [i % 21 for i in range(len(body))], "%s: sequence %s" % (name, body)
        assert seqs[-1] & 0x40 and sum(1 for s in seqs if s & 0x40) == 1, "%s: last flags %s" % (name, seqs[-3:])
        wrong = [p["seq"] for p in r["rx"] if not p["last"] and (p["seq"] == 0) != (p["slow"] == dextra.SYNC_DATA)]
        assert not wrong, "%s: sync misplaced at sequence %s" % (name, wrong)


def test_dcs_passthrough_exact(rig):
    """P-DCS-28 (diff): AMBE relayed byte exact on both sides; the frames
    whose slow data changed on the way are recorded, sut == ref."""
    got = both(rig, stream_probe)
    rows = {}
    for name, r in got.items():
        body = [p for p in r["rx"] if not p["last"]]
        assert [p["frame"] for p in body] == r["sent"], "%s: AMBE changed (%d of %d)" % (
            name, len(body), len(r["sent"]))
        want = [dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA for i in range(len(body))]
        rows[name] = {"slow_changed": [(i, p["slow"].hex()) for i, p in enumerate(body) if p["slow"] != want[i]]}
    print("P-DCS-28 slow data changed (index, bytes):", rows)
    same(rows, "slow_changed")


def test_dcs_last_packet(rig):
    """P-DCS-29 (hb): the last packet (0x40) reaches the listener once and
    ends the stream; the talker gets nothing back (P-DCS-36, B9); the
    stream id is the ingress one on every packet (P-DCS-37)."""
    got = both(rig, stream_probe)
    for name, r in got.items():
        lasts = [p for p in r["rx"] if p["last"]]
        sids = {p["sid"] for p in r["rx"]}
        print("P-DCS-29/36/37 %s: %d last, echo %d, sids %s" % (name, len(lasts), r["echo"], sids))
        assert len(lasts) == 1 and r["rx"][-1]["last"], "%s: %d last packets" % (name, len(lasts))
        assert r["echo"] == 0, "%s: %d packets echoed to the talker" % (name, r["echo"])
        assert sids == {r["sid"].to_bytes(2, "little")}, "%s: egress ids %s" % (name, sids)


def test_dcs_last_packet_payload(rig):
    """P-DCS-30 (dev): last packet sent with the JARL end pattern (as
    ircDDBGateway); the egress payload is recorded."""
    got = both(rig, stream_probe, 0, end=dcs.END_JARL)
    rows = {}
    for name, r in got.items():
        last = [p for p in r["rx"] if p["last"]]
        rows[name] = {"ambe": last[0]["frame"].hex() if last else None, "slow": last[0]["slow"].hex() if last else None}
    print("P-DCS-30 last payload:", rows)
    judge_dev(rig, "P-DCS-30", rows, "ambe", "slow")


def end_form_probe(ip, end):
    first, second, listener = Gw("STR012"), Gw("STR013"), Gw(LISTENER)
    try:
        ready(ip, first, second, listener)
        voice, _sid = talk(first, listener, end=end, wait=0.5)
        lasts = len([p for p in heard(listener) if p["last"]])
        clear(listener)
        talk(second, listener)
        return {"last_relayed": lasts, "next_heard": len(voice_frames(listener)) == FRAMES}
    finally:
        done(first, second, listener)


@pytest.mark.parametrize("form", ["ircddb", "droidstar", "xlxd"])
def test_dcs_end_forms(rig, form):
    """P-DCS-29 (hb): each client's last-packet form (ircDDBGateway JARL
    pattern, DroidStar plain AMBE frame, xlxd's own) closes the stream at
    once: one last packet reaches the listener, a second talker 0.5 s
    later is heard in full."""
    end = {"ircddb": dcs.END_JARL, "droidstar": dcs.END_DROIDSTAR, "xlxd": dcs.END_XLXD}[form]
    got = both(rig, end_form_probe, end)
    print("P-DCS-29 end form %s:" % form, got)
    for name, r in got.items():
        assert r == {"last_relayed": 1, "next_heard": True}, "%s: %s" % (name, r)


def test_dcs_counter_and_text(rig):
    """P-DCS-31 (dev): the talker sends a counter from 0 and a text area
    (0x01, 0x00, 0x21 + 20 characters); the egress counter start and the
    area are recorded."""
    tail = b"\x01\x00\x21" + b"DVTEST DCS TEXT AREA" + bytes(16)
    got = both(rig, stream_probe, 0, tail=tail)
    rows = {}
    for name, r in got.items():
        body = [p for p in r["rx"] if not p["last"]]
        counters = [p["counter"] for p in body]
        rows[name] = {"renumbered": counters == list(range(len(counters))),
                      "tail_passed": bool(body) and body[0]["tail"] == tail,
                      "byte61": body[0]["tail"][:1].hex() if body else None,
                      "counters": counters[:5]}
    print("P-DCS-31 counter/text:", rows)
    # the counter values themselves are timing dependent on xlxd (copied
    # from the latest received packet): only whether it is renumbered counts
    judge_dev(rig, "P-DCS-31", rows, "renumbered", "tail_passed", "byte61")


@pytest.mark.parametrize("field", ["my", "rpt1", "rpt2"])
def test_dcs_invalid_callsign(rig, field):
    """P-DCS-32 (dev): a header field with characters outside A-Z, 0-9,
    space: frames heard recorded."""
    bad = {field: b"N0#CALL" + (MOD.encode() if field != "my" else b" ")}
    with marked(rig, "dcs-bad-callsign"):
        got = both(rig, stream_probe, 0, **bad)
    key = "heard_" + field
    rows = {k: {key: len([p for p in v["rx"] if not p["last"]])} for k, v in got.items()}
    print("P-DCS-32 invalid %s:" % field, rows)
    judge_dev(rig, "P-DCS-32", rows, key)


def test_dcs_stream_id_zero(rig):
    """P-DCS-46 (dev): an over with stream id 0 (DroidStar can draw it)."""
    with marked(rig, "dcs-sid0"):
        got = both(rig, stream_probe, 0, sid=None, sid_bytes=b"\x00\x00")
    rows = {k: {"heard": len([p for p in v["rx"] if not p["last"]])} for k, v in got.items()}
    print("P-DCS-46 stream id 0:", rows)
    judge_dev(rig, "P-DCS-46", rows, "heard")


# ---------------------------------------------------------------- 6 routing

def fanout_probe(ip):
    talker, a, b = Gw(TALKER), Gw(LISTENER), Gw(THIRD)
    other = Gw("STR002", OTHER, OTHER)
    try:
        ready(ip, talker, a, b, other)
        voice, _sid = talk(talker, a, b, other)
        return {"a": len(voice_frames(a)) / len(voice), "b": len(voice_frames(b)) / len(voice),
                "other": len(voice_frames(other))}
    finally:
        done(talker, a, b, other)


def test_dcs_module_fanout(rig):
    """P-DCS-34 (diff): every other client of the module gets the stream
    once; a client on another module gets nothing."""
    got = both(rig, fanout_probe)
    print("P-DCS-34 fan-out:", got)
    for name, r in got.items():
        assert r == {"a": 1.0, "b": 1.0, "other": 0}, "%s: %s" % (name, r)


def rpt2_probe(ip):
    talker, on_mod, on_other = Gw(TALKER), Gw(LISTENER), Gw(THIRD, OTHER, OTHER)
    try:
        ready(ip, talker, on_mod, on_other)
        talk(talker, on_mod, on_other, rpt2=REFL.encode() + b" " + OTHER.encode())
        return {"linked_module": len(voice_frames(on_mod)) > 0, "rpt2_module": len(voice_frames(on_other)) > 0}
    finally:
        done(talker, on_mod, on_other)


def test_dcs_rpt2_other_module(rig):
    """P-DCS-35 (dev): a talker linked to J sends RPT2 = DCS999 K: which
    module hears it."""
    got = both(rig, rpt2_probe)
    print("P-DCS-35 RPT2 other module:", got)
    judge_dev(rig, "P-DCS-35", got, "linked_module", "rpt2_module")


def timeout_probe(ip):
    talker, listener, third = Gw(TALKER), Gw(LISTENER), Gw(THIRD)
    try:
        ready(ip, talker, listener, third)
        sid = clients.fresh_stream_id("dstar")
        send(talker, dcs.stream_packets(sid, frames(), last=False, callsign=TALKER, module=MOD), listener, wait=2.5)
        lasts = [p for p in heard(listener) if p["last"]]
        clear(listener)
        talk(third, listener)
        return {"synthesized_last": len(lasts), "free": len(voice_frames(listener)) > 0}
    finally:
        done(talker, listener, third)


def test_dcs_stream_timeout(rig):
    """P-DCS-39 (B3): a stream without a last packet ends after the
    silence timeout and the module is free; a synthesized last packet is
    recorded (B3 asks for one)."""
    with marked(rig, "dcs-no-last"):
        got = both(rig, timeout_probe)
    print("P-DCS-39 lost last packet:", got)
    for name, r in got.items():
        assert r["free"], "%s: module not free 2.5 s after the stream stopped" % name
    entry = specs.rule(rig, "dcs") if rig.sut_profile != specs.DEFAULT else None
    if rig.sut_profile == specs.DEFAULT:
        same(got, "synthesized_last")
    elif entry and "P-DCS-39" in entry:
        assert got["sut"]["synthesized_last"] == entry["P-DCS-39"]["synthesized_last"]
    else:
        pytest.skip("spec pending: no dcs P-DCS-39 entry in dvtest/specs/%s.py" % rig.sut_profile)


def after_last_probe(ip):
    talker, listener = Gw("STR011"), Gw(LISTENER)
    try:
        ready(ip, talker, listener)
        voice, sid = talk(talker, listener, wait=0.2)
        clear(listener)
        extra = dcs.stream_packets(sid, frames(10, seed="after"), start=len(voice) + 1, last=False,
                                   callsign="STR011", module=MOD)
        send(talker, extra, listener)
        return {"heard_after_last": len(voice_frames(listener))}
    finally:
        done(talker, listener)


def test_dcs_voice_after_last(rig):
    """P-DCS-40 (dev): packets with the id of a stream that just ended:
    frames heard recorded."""
    with marked(rig, "dcs-after-last"):
        got = both(rig, after_last_probe)
    print("P-DCS-40 voice after last:", got)
    judge_dev(rig, "P-DCS-40", got, "heard_after_last")


def late_probe(ip):
    talker, late = Gw(TALKER), Gw(LISTENER)
    try:
        ready(ip, talker)
        sid = clients.fresh_stream_id("dstar")
        packets = dcs.stream_packets(sid, frames(300), callsign=TALKER, module=MOD)   # 6 s: ACK jitter + cache
        late.dest = (ip, dcs.PORT)
        result = {}

        def joiner():
            time.sleep(0.6)
            reply = link(ip, late)
            result["ack"] = bool(reply and reply["kind"] == "ack")
        thread = threading.Thread(target=joiner)
        thread.start()
        send(talker, packets, wait=0.5)
        thread.join()
        settle(late, seconds=0.5)
        return {"ack": result.get("ack"), "heard": len(voice_frames(late)) > 0}
    finally:
        done(talker, late)


def test_dcs_late_listener(rig):
    """P-DCS-41 (diff): a client that links while a stream runs receives
    the rest of it."""
    got = both(rig, late_probe)
    print("P-DCS-41 late listener:", got)
    for name, r in got.items():
        assert r["ack"] and r["heard"], "%s: %s" % (name, r)


# ---------------------------------------------------------------- 7 addressing

def two_ports_probe(ip):
    talker, a, b = Gw(TALKER), Gw(LISTENER), Gw(THIRD)
    try:
        ready(ip, talker, a, b)
        voice, _sid = talk(talker, a, b)
        return {"ports_differ": a.port != b.port, "a": len(voice_frames(a)) == len(voice),
                "b": len(voice_frames(b)) == len(voice)}
    finally:
        done(talker, a, b)


def test_dcs_two_ports_one_ip(rig):
    """P-DCS-42 (diff): two links from one IP on different ports are two
    clients, each hears the module."""
    got = both(rig, two_ports_probe)
    print("P-DCS-42 two ports:", got)
    for name, r in got.items():
        assert r == {"ports_differ": True, "a": True, "b": True}, "%s: %s" % (name, r)


def two_modules_probe(ip):
    """QnetGateway form: one socket links own module A to reflector J and
    own module B to reflector K."""
    gw = Gw(LISTENER, "A", MOD)
    talk_j, talk_k = Gw(TALKER), Gw(THIRD, OTHER, OTHER)
    try:
        assert link(ip, gw)["kind"] == "ack"
        reply = link(ip, gw, dcs.gateway_link(gw.callsign, "B", OTHER))
        ready(ip, talk_j, talk_k)
        clear(gw)
        talk(talk_j, gw)
        hears_j = len(voice_frames(gw)) > 0
        clear(gw)
        talk(talk_k, gw)
        hears_k = len(voice_frames(gw)) > 0
        gw.send(dcs.unlink19(gw.callsign, "B"))
        time.sleep(CACHE)
        clear(gw)
        talk(talk_j, gw)
        j_after = len(voice_frames(gw)) > 0
        clear(gw)
        talk(talk_k, gw)
        k_after = len(voice_frames(gw)) > 0
        return {"second_ack": bool(reply and reply["kind"] == "ack"), "hears_j": hears_j, "hears_k": hears_k,
                "j_after_unlink_b": j_after, "k_after_unlink_b": k_after}
    finally:
        gw.send(dcs.unlink19(gw.callsign, "A"))
        done(gw, talk_j, talk_k)


def test_dcs_two_modules_one_port(rig):
    """P-DCS-43 (dev): two modules linked from one ip:port; traffic per
    module and the effect of unlinking one are recorded (recommended:
    J and K heard, after unlinking B only J)."""
    with marked(rig, "dcs-two-modules"):
        got = both(rig, two_modules_probe)
    print("P-DCS-43 two modules, one port:", got)
    judge_dev(rig, "P-DCS-43", got, "second_ack", "hears_j", "hears_k", "j_after_unlink_b", "k_after_unlink_b")
