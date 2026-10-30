# SPDX-License-Identifier: MIT
"""DExtra / D-STAR reflector conformance (docs/protocols/dextra.md,
testlist K2): one case per obligation P-DXT-NN, xlxd-ref and xlxd-sut
probed in parallel, DExtra clients only (no cross-mode).

Judged as in dextra.md: hb = a spec MUST/SHOULD, asserted on both sides;
dev = disputed between implementations (or xlxd deviates from spec or
clients): sut == ref under the xlxd profile, else the profile's "dextra"
spec entry {"P-DXT-NN": {observed key: value}} (skip "spec pending"
without one); diff = sut == ref. Rows covered by other cases (P-DXT-13,
23, 25, 27, 38, 40, 41) have no case here. Every case prints what both
sides did. Deliberately malformed or out-of-protocol ingress is
bracketed with control-channel marks so dvcheck does not count it.
"""
import contextlib
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import dextra

pytestmark = [pytest.mark.proto, pytest.mark.rig]

MOD, OTHER = "P", "Q"                           # not used by other cases
TALKER, LISTENER, EXTRA, GW = "N0CALL", "N0TEST", "N0OTHER", "N0GW"
PEER = "XRF997"                                 # a reflector peer linking in
FRAMES = 50
GAP = 0.020                                     # one voice packet per 20 ms
CACHE = 1.2                                     # xlxd refreshes its module client list once per second
HEAD_GAP = 0.3                                  # header -> first voice, as the other rig cases
TAIL = 0.8
LINK_WAIT = 3.0                                 # xlxd delays a first ACK by up to 2 s
REPLY_KINDS = ("ack", "nak", "rev2")


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


def mark(rig, phase, tag):
    """Control channel "mark begin|end <tag>": brackets deliberately
    malformed ingress so dvcheck can tell it from a fault (no-op without
    the control channel). Same request form as test_usrp.mark."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "dextra-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


@contextlib.contextmanager
def marked(rig, ip, tag):
    """mark begin/end around the block; one tag per side (ref and sut run
    in parallel threads)."""
    tag = "dextra-%s-%s" % (tag, ip)
    mark(rig, "begin", tag)
    try:
        yield
    finally:
        mark(rig, "end", tag)


def judge_dev(rig, nn, got, *keys):
    """dev items: sut == ref on keys under the xlxd profile, else the
    profile's dextra spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "dextra") or {}).get(nn) or {}
    pending = [key for key in keys if key not in entry]
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    if pending:
        pytest.skip("spec pending: no dextra %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def frames(n=FRAMES, seed="dextra"):
    return signals.prng_frames("dstar", n, seed=seed)


def sid():
    return clients.fresh_stream_id("dstar")


# ---------------------------------------------------------------- clients

def new(callsign=TALKER, module=MOD, keepalive="own"):
    return dextra.DstarClient("0.0.0.0", callsign=callsign, module=module, keepalive=keepalive)


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


def is_keepalive(data):
    return len(data) == 9 and data[8:9] == b"\x00"


def replies(c):
    """Inbox packets that are neither DSVT nor a reflector keepalive."""
    return [d for d in c.client.inbox if not d.startswith(b"DSVT") and not is_keepalive(d)]


def kind_of(data):
    if data is None:
        return None
    parsed = dextra.parse_link_reply(data)
    return parsed["kind"] if parsed else "other %d B" % len(data)


def link(ip, c, packet=None, seconds=LINK_WAIT):
    """Send a link (default: the client's own) and wait for ACK, NAK or a
    revision 2 reply: {"kind", "raw", "latency"}; kind None on silence."""
    c.dest = (ip, dextra.PORT)
    clear(c)
    start = time.monotonic()
    c.client.send(c.dest, packet or dextra.link_packet(c.callsign, c.module))
    while True:
        c.client.drain()
        for data, at in zip(c.client.inbox, c.client.inbox_times):
            kind = kind_of(data)
            if kind in REPLY_KINDS:
                c.linked_at = at
                return {"kind": kind, "raw": data, "latency": round(at - start, 3)}
        if time.monotonic() - start >= seconds:
            return {"kind": None, "raw": b"", "latency": None}
        time.sleep(0.005)


def ready(ip, *made):
    """Link each client in turn (xlxd keeps one pending ACK per IP), wait
    for the module client list, empty the inboxes."""
    for c in made:
        got = link(ip, c)
        assert got["kind"] == "ack", "%s: %s link to %s: %r" % (ip, c.callsign.decode().strip(), c.module, got)
    time.sleep(CACHE)
    clear(*made)
    return made


def done(*made):
    """Unlink every client that has a destination, then close."""
    for c in made:
        try:
            if c.dest:
                c.client.send(c.dest, dextra.unlink_packet(c.callsign, c.module))
        finally:
            c.dest = None
            c.client.close()


def ask(c, packet, seconds=1.0):
    """Send packet with an empty inbox; the first reply (not DSVT, not a
    keepalive) within seconds, else None."""
    clear(c)
    c.client.send(c.dest, packet)
    settle(c, seconds=seconds)
    got = replies(c)
    return got[0] if got else None


# ---------------------------------------------------------------- streams

def stream(stream_id, voice, counter0=0, head=True, end=True, slow=None, last_payload=None,
           my=TALKER, module=MOD, **header):
    """Header (radio_header fields, valid CRC unless crc= is given), voice
    with counters counter0.. mod 21 (sync data at counter 0 unless slow is
    given per frame), last frame (JARL end pattern unless last_payload)."""
    out = []
    if head:
        out.append(dextra.header_packet(stream_id, my=my, module=module, **header))
    for index, frame in enumerate(voice):
        n = counter0 + index
        data = slow[index] if slow else (dextra.SYNC_DATA if n % 21 == 0 else dextra.SLOW_DATA)
        out.append(dextra.voice_packet(stream_id, n % 21, frame, data))
    if end:
        out.append(dextra.last_frame(stream_id, counter0 + len(voice),
                                     last_payload or dextra.END_PATTERN))
    return out


def send(c, packets, *listeners, wait=TAIL):
    """A header (if first) then HEAD_GAP, the rest paced at 20 ms; every
    client keeps draining."""
    every = (c,) + listeners
    if packets and dextra.parse_header_full(packets[0]):
        c.client.send(c.dest, packets[0])
        settle(*every, seconds=HEAD_GAP)
        packets = packets[1:]
    c.client.paced(c.dest, packets, GAP, *[m.client for m in listeners])
    settle(*every, seconds=wait)


def talk(c, *listeners, voice=None, wait=TAIL, **kw):
    """A complete stream; returns (voice, stream id)."""
    voice = voice or frames()
    stream_id = sid()
    send(c, stream(stream_id, voice, my=c.callsign.decode().strip(), module=c.module, **kw),
         *listeners, wait=wait)
    return voice, stream_id


def voices(c):
    return [v for v in map(dextra.parse_voice_raw, c.client.inbox) if v]


def heard(c):
    """AMBE of the non-last voice packets, in arrival order."""
    return [v["ambe"] for v in voices(c) if not v["last"]]


def headers(c):
    return [h for h in map(dextra.parse_header_full, c.client.inbox) if h]


def dsvt_count(c):
    return sum(d.startswith(b"DSVT") for d in c.client.inbox)


# ---------------------------------------------------------------- 1 link, keepalive, unlink

def link_ack_probe(ip):
    c = new(LISTENER)
    try:
        got = link(ip, c)
        packet = dextra.link_packet(c.callsign, c.module)
        return {"kind": got["kind"], "len": len(got["raw"]), "latency": got["latency"],
                "form_ok": got["raw"][:10] == packet[:10] and got["raw"][10:] == b"ACK\x00"}
    finally:
        done(c)


def test_dextra_link_ack(rig):
    """P-DXT-01 (diff): a link to a served module gets the 14 B ACK: link
    bytes 0-9 echoed, then ACK 0x00."""
    got = both(rig, link_ack_probe)
    print("P-DXT-01 link ACK:", got)
    same(got, "kind", "len", "form_ok")


def latency_probe(ip, rounds=5):
    c = new(EXTRA)
    out = []
    try:
        for _ in range(rounds):
            got = link(ip, c)
            out.append(got["latency"])
            c.client.send(c.dest, dextra.unlink_packet(c.callsign, c.module))
            time.sleep(0.3)
        return {"latency": out, "acked": all(x is not None for x in out),
                "delayed": any(x is None or x > 0.25 for x in out)}
    finally:
        c.dest = None
        done(c)


def test_dextra_ack_latency(rig):
    """P-DXT-02 (dev): latency of five first ACKs (link, unlink, link ...).
    "delayed" = any ACK later than 250 ms (xlxd delays by a random 0-2 s)."""
    got = both(rig, latency_probe)
    print("P-DXT-02 ACK latency s:", got)
    judge_dev(rig, "P-DXT-02", got, "acked", "delayed")


def unserved_probe(ip, letter="Z"):
    c = new(EXTRA, letter)
    try:
        return {"z": link(ip, c)["kind"]}
    finally:
        done(c)


def unserved_letter():
    """DVT_UNSERVED_MODULE: a letter the sut is started without (the rig
    then leaves it out of the sut's rooms); empty when unset."""
    return os.environ.get("DVT_UNSERVED_MODULE", "").strip().upper()[:1]


def test_dextra_link_unserved_module_nak(rig):
    """P-DXT-03 (diff): a link to a module letter that is not served gets
    NAK and no link. Without DVT_UNSERVED_MODULE it is probed with Z
    (xlxd serves A.. contiguously): when both serve Z there is no
    unserved letter to probe. With it, the sut must NAK that letter (the
    profile's "reply" when its spec names one); xlxd-ref is recorded."""
    letter = unserved_letter()
    if letter:
        got = both(rig, unserved_probe, letter)
        print("P-DXT-03 link to unserved %s:" % letter, got)
        if got["sut"]["z"] == "ack" and rig.sut_profile == specs.DEFAULT:
            pytest.skip("DVT_UNSERVED_MODULE=%s has no effect on this sut (it serves %s)" % (letter, letter))
        want = ((specs.rule(rig, "dextra") or {}).get("P-DXT-03") or {}).get("reply", "nak")
        assert got["sut"]["z"] == want, "sut: link to unserved %s got %s, want %s" % (letter, got["sut"]["z"], want)
        return
    got = both(rig, unserved_probe)
    print("P-DXT-03 link to Z:", got)
    if got["ref"]["z"] == got["sut"]["z"] == "ack":
        pytest.skip("both reflectors serve A-Z (NB_OF_MODULES 26): no unserved module letter on this rig")
    same(got, "z")


INVALID = {"callsign": dextra.raw_link(b"n0call  ", MOD, MOD),
           "module": dextra.raw_link(b"N0OTHER ", MOD, "5")}


def invalid_probe(ip, rig, name):
    bad, talker = new(EXTRA), new(TALKER)
    try:
        ready(ip, talker)
        with marked(rig, ip, "p04-" + name):
            got = link(ip, bad, INVALID[name], seconds=2.5)
        replied = replies(bad)
        settle(bad, seconds=CACHE)
        clear(bad)
        talk(talker, bad)
        return {"reply": got["kind"] or kind_of(replied[0] if replied else None),
                "linked": dsvt_count(bad) > 0}
    finally:
        bad.dest = None
        done(bad, talker)


@pytest.mark.parametrize("name", list(INVALID))
def test_dextra_link_invalid(rig, name):
    """P-DXT-04 (dev): a link with a lower-case callsign or a digit as the
    reflector module: reply recorded, never linked (hears no stream)."""
    got = both(rig, invalid_probe, rig, name)
    print("P-DXT-04 %s:" % name, got)
    judge_dev(rig, "P-DXT-04", got, "reply", "linked")


def rev1_probe(ip):
    c, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, talker)
        got = link(ip, c, dextra.link_packet(c.callsign, c.module, tail=11))
        time.sleep(CACHE)
        clear(c, talker)
        voice, _ = talk(talker, c)
        exact = heard(c) == voice
        unlink = ask(c, dextra.unlink_packet(c.callsign, c.module))
        c.dest = None
        return {"reply": got["kind"], "exact": exact, "unlink_reply": kind_of(unlink)}
    finally:
        done(c, talker)


def test_dextra_link_rev1(rig):
    """P-DXT-05 (diff): a link whose last byte is 11 (DroidStar) is ACKed
    and served; the reply to its unlink is recorded (XLX family:
    DISCONNECTED)."""
    got = both(rig, rev1_probe)
    print("P-DXT-05 revision 1:", got)
    same(got, "reply", "exact", "unlink_reply")


def relink_probe(ip):
    c, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, c, talker)
        again = link(ip, c)
        time.sleep(CACHE)
        clear(c, talker)
        voice, _ = talk(talker, c)
        return {"second_reply": again["kind"], "second_latency": again["latency"],
                "once": heard(c) == voice, "frames": len(heard(c))}
    finally:
        done(c, talker)


def test_dextra_relink_single_client(rig):
    """P-DXT-06 (dev): the same link again while linked: reply recorded,
    the client still gets the module's traffic exactly once."""
    got = both(rig, relink_probe)
    print("P-DXT-06 repeated link:", got)
    judge_dev(rig, "P-DXT-06", got, "second_reply", "once")


KEEPALIVE_WATCH = 26


def keepalive_probe(ip):
    c = new(LISTENER)
    try:
        ready(ip, c)
        settle(c, seconds=KEEPALIVE_WATCH)
        got = [(d, t) for d, t in zip(c.client.inbox, c.client.inbox_times) if is_keepalive(d)]
        times = [t for _, t in got]
        gaps = [round(b - a, 1) for a, b in zip(times, times[1:])]
        return {"count": len(got), "forms": sorted({d.hex() for d, _ in got}),
                "first_s": round(times[0] - c.linked_at, 1) if times else None, "gaps": gaps,
                "period": round(min(gaps)) if gaps else None}
    finally:
        done(c)


def test_dextra_reflector_keepalive(rig):
    """P-DXT-07 (diff + dev): the reflector's keepalive over 26 s: the 9 B
    form (diff) and the period (dev; xlxd 10 s)."""
    got = both(rig, keepalive_probe)
    print("P-DXT-07 reflector keepalive:", got)
    same(got, "forms")
    judge_dev(rig, "P-DXT-07", got, "period")


def unlink_probe(ip):
    c, talker = new(LISTENER), new(TALKER)
    try:
        ready(ip, c, talker)
        reply = ask(c, dextra.unlink_packet(c.callsign, c.module))
        c.dest = None
        time.sleep(CACHE)
        clear(c, talker)
        talk(talker, c)
        return {"reply": kind_of(reply), "heard": dsvt_count(c) > 0}
    finally:
        done(c, talker)


def test_dextra_unlink_no_reply(rig):
    """P-DXT-12 (diff): an unlink gets no reply and removes the client."""
    got = both(rig, unlink_probe)
    print("P-DXT-12 unlink:", got)
    same(got, "reply", "heard")


def unlink_talking_probe(ip, rig):
    talker, listener, nxt = new(TALKER), new(LISTENER), new(GW)
    try:
        ready(ip, listener, nxt, talker)
        stream_id = sid()
        with marked(rig, ip, "p14-no-last"):                    # a stream with no last frame
            send(talker, stream(stream_id, frames(40, "p14"), end=False), listener, nxt, wait=0.0)
            talker.client.send(talker.dest, dextra.unlink_packet(talker.callsign, talker.module))
            unlinked = time.monotonic()
            settle(talker, listener, nxt, seconds=2.4)          # past xlxd's 1.6 s stream timeout
        ends = [t for d, t in zip(listener.client.inbox, listener.client.inbox_times)
                if (dextra.parse_voice_raw(d) or {}).get("last")]
        clear(talker, listener, nxt)
        talk(nxt, talker, listener, voice=frames(30, "p14b"))
        return {"end_marker": bool(ends), "end_s": round(ends[0] - unlinked, 1) if ends else None,
                "still_linked": len(heard(talker)) > 0, "next_heard": len(heard(listener)) > 0}
    finally:
        done(talker, listener, nxt)


def test_dextra_unlink_while_talking(rig):
    """P-DXT-14 (dev): the talker unlinks mid-stream and stops: end marker
    to the listener (and when), whether the talker is still linked, and
    whether the module carries the next stream 2.4 s later."""
    got = both(rig, unlink_talking_probe, rig)
    print("P-DXT-14 unlink while talking:", got)
    judge_dev(rig, "P-DXT-14", got, "end_marker", "still_linked", "next_heard")


OPEN_NEXT_S = 3.0       # B starts its stream 3 s after A's unlink (xlxd stream timeout 1.6 s)
OPEN_WATCH_S = 25.0     # A's inbox watched for 25 s after its unlink (xlxd keepalive every 10 s)
REVOKED = {"keepalives_after_unlink": 0, "new_stream_heard": False}    # B5 immediate revoke


def unlink_open_probe(ip, rig):
    """A (no keepalives of its own: it left) talks on OTHER without a last
    frame and unlinks mid-stream; B starts a fresh stream 3 s later; L
    confirms the module carried it. What reaches A after its unlink."""
    a, b = new(TALKER, OTHER, keepalive="none"), new(GW, OTHER)
    listener = new(LISTENER, OTHER)
    try:
        ready(ip, listener, b, a)
        with marked(rig, ip, "p54-open-unlink"):            # a stream with no last frame
            send(a, stream(sid(), frames(40, "p54"), end=False, module=OTHER), b, listener, wait=0.0)
            a.client.send(a.dest, dextra.unlink_packet(a.callsign, a.module))
            unlinked = time.monotonic()
        settle(a, b, listener, seconds=max(0.0, unlinked + OPEN_NEXT_S - time.monotonic()))
        clear(listener)
        voice_b, sid_b = talk(b, a, listener, voice=frames(30, "p54b"))
        settle(a, b, listener, seconds=max(0.0, unlinked + OPEN_WATCH_S - time.monotonic()))
        after = [(d, t) for d, t in zip(a.client.inbox, a.client.inbox_times) if t > unlinked]
        kas = [round(t - unlinked, 1) for d, t in after if is_keepalive(d)]
        own_b = [d for d, _ in after if d.startswith(b"DSVT") and
                 (dextra.parse_header_full(d) or dextra.parse_voice_raw(d) or {}).get("stream_id") == sid_b]
        return {"keepalives_after_unlink": len(kas), "keepalive_s": kas,
                "new_stream_heard": bool(own_b),
                "header_heard": any(dextra.parse_header_full(d) for d in own_b),
                "voice_heard": sum(1 for d in own_b if dextra.parse_voice_raw(d)),
                "next_relayed": heard(listener) == voice_b}
    finally:
        done(a, b, listener)                                   # A's unlink again (xlxd kept it)


def test_dextra_unlink_with_open_stream(rig):
    """P-DXT-54 (dev): a client talks without a last frame and unlinks
    while its own stream is open, then goes quiet; another client talks on
    the module 3 s later. Recorded for A over 25 s after its unlink:
    reflector keepalives and B's stream (header, voice). xlxd profile:
    sut == ref (keepalives compared as kept or not); other profiles: the
    "dextra" P-DXT-54 entry, else B5 immediate revoke when the profile
    states it (test_leave_mid_stream_tail revoke immediate), else pending."""
    got = both(rig, unlink_open_probe, rig)
    print("P-DXT-54 unlink with open stream:", got)
    for name, r in got.items():
        assert r["next_relayed"], "%s: the module did not carry B's stream after A's unlink: %s" % (name, r)
    if rig.sut_profile == specs.DEFAULT:
        view = {n: {"kept_alive": r["keepalives_after_unlink"] > 0, "new_stream_heard": r["new_stream_heard"]}
                for n, r in got.items()}
        same(view, "kept_alive", "new_stream_heard")
        return
    entry = (specs.rule(rig, "dextra") or {}).get("P-DXT-54")
    if entry is None and (specs.rule(rig, "test_leave_mid_stream_tail") or {}).get("revoke") == "immediate":
        entry = REVOKED
    if entry is None:
        pytest.skip("spec pending: no dextra P-DXT-54 entry in dvtest/specs/%s.py" % rig.sut_profile)
    for key, want in entry.items():
        assert got["sut"][key] == want, "P-DXT-54 %s: sut %r, spec %r" % (key, got["sut"][key], want)


# ---------------------------------------------------------------- 1 slow: keepalive timeout

SILENCE_S = 85          # one long over; xlxd drops a silent client 60-70 s after its link


def silence_scenario(ip):
    """keeper answers keepalives with its callsign, echoer echoes the
    reflector's, silent never answers; talker sends no keepalive but
    talks one 85 s stream; then keeper talks a short stream."""
    keeper, silent = new(LISTENER), new(EXTRA, keepalive="none")
    echoer, talker = new(GW, keepalive="echo"), new(TALKER, keepalive="none")
    made = (keeper, silent, echoer, talker)
    try:
        ready(ip, *made)
        voice = frames(int(SILENCE_S / GAP), "p11")
        send(talker, stream(sid(), voice), keeper, silent, echoer, wait=1.5)
        out = {}
        for name, c in (("silent", silent), ("echoer", echoer), ("keeper", keeper)):
            notices = [(kind_of(d), t) for d, t in zip(c.client.inbox, c.client.inbox_times)
                       if not d.startswith(b"DSVT") and not is_keepalive(d)]
            times = [t for d, t in zip(c.client.inbox, c.client.inbox_times)
                     if (dextra.parse_voice_raw(d) or {}).get("last") is False]
            out[name] = {"notice": notices[0][0] if notices else None,
                         "notice_s": round(notices[0][1] - c.linked_at, 1) if notices else None,
                         "frames": len(times),
                         "last_voice_s": round(times[-1] - c.linked_at, 1) if times else None}
        clear(*made)
        talk(keeper, silent, echoer, talker, voice=frames(30, "p11b"))
        out["after"] = {name: len(heard(c)) for name, c in
                        (("silent", silent), ("echoer", echoer), ("talker", talker))}
        out["sent"] = len(voice)
        return out
    finally:
        done(*made)


@pytest.fixture(scope="module")
def silence(rig):
    got = both(rig, silence_scenario)
    print("silent client scenario:", got)
    return got


def dropped(r, name):
    return r[name]["frames"] < r["sent"] - 50 and r["after"][name] == 0


def whole(r, name):
    """Heard (nearly) the whole long stream: at most 1 % lost in transit."""
    return r[name]["frames"] >= r["sent"] * 0.99


@pytest.mark.slow
def test_dextra_keepalive_echo_not_refresh(rig, silence):
    """P-DXT-08 (dev): a client that only echoes the reflector keepalive
    vs one that answers with its own callsign: which one keeps its link."""
    got = {n: {"echo_kept": not dropped(r, "echoer"), "own_kept": whole(r, "keeper")}
           for n, r in silence.items()}
    print("P-DXT-08 keepalive answers:", got)
    judge_dev(rig, "P-DXT-08", got, "echo_kept", "own_kept")


@pytest.mark.slow
def test_dextra_silent_client_dropped(rig, silence):
    """P-DXT-09 (dev): a client that never answers is dropped; the notice
    it gets and when (s after its link) are recorded."""
    got = {n: {"dropped": dropped(r, "silent"), "notice": r["silent"]["notice"],
               "notice_s": r["silent"]["notice_s"], "last_voice_s": r["silent"]["last_voice_s"]}
           for n, r in silence.items()}
    print("P-DXT-09 silent client:", got)
    judge_dev(rig, "P-DXT-09", got, "dropped", "notice")


@pytest.mark.slow
def test_dextra_dropped_client_no_voice(rig, silence):
    """P-DXT-10 (diff): the dropped client gets no voice after the drop
    (neither the rest of the running stream nor the next one)."""
    got = {n: {"stopped": r["silent"]["frames"] < r["sent"] - 50, "next": r["after"]["silent"]}
           for n, r in silence.items()}
    print("P-DXT-10 dropped client:", got)
    same(got, "stopped", "next")


@pytest.mark.slow
def test_dextra_talker_not_dropped(rig, silence):
    """P-DXT-11 (diff): a talker that sends no keepalive during an 85 s
    stream is still linked after it (hears the next stream)."""
    got = {n: {"talker_kept": r["after"]["talker"] > 0} for n, r in silence.items()}
    print("P-DXT-11 talker:", got)
    same(got, "talker_kept")


@pytest.mark.slow
def test_dextra_keepalive_isolated(rig, silence):
    """P-DXT-49 (diff): the keeper (own keepalive, same IP) keeps its link
    and the whole stream while the silent client is dropped."""
    got = {n: {"keeper_whole": whole(r, "keeper"), "keeper_notice": r["keeper"]["notice"]}
           for n, r in silence.items()}
    print("P-DXT-49 keeper:", got)
    same(got, "keeper_whole", "keeper_notice")


def poll_probe(ip, rig):
    poller, talker = new(EXTRA), new(TALKER)
    try:
        ready(ip, talker)
        poller.dest = (ip, dextra.PORT)
        with marked(rig, ip, "p52-poll"):
            for _ in range(3):
                poller.client.send(poller.dest, dextra.keepalive_packet(poller.callsign))
                settle(poller, seconds=1.0)
        reply = poller.client.inbox[0] if poller.client.inbox else None
        clear(poller)
        talk(talker, poller)
        return {"reply": kind_of(reply), "heard": dsvt_count(poller) > 0}
    finally:
        poller.dest = None
        done(poller, talker)


def test_dextra_poll_without_link(rig):
    """P-DXT-52 (dev): 9 B keepalives from an address that never linked:
    reply recorded, no traffic of the module reaches it."""
    got = both(rig, poll_probe, rig)
    print("P-DXT-52 poll without link:", got)
    judge_dev(rig, "P-DXT-52", got, "reply", "heard")


# ---------------------------------------------------------------- 2 malformed

def _sample_header():
    return dextra.header_packet(0x5A5A, my=TALKER, module=MOD)


def _sample_voice():
    return dextra.voice_packet(0x5A5A, 1, bytes(9))


GARBAGE = {
    "nat1": dextra.NAT_PUNCH,                   # sent before the link, as DStarGateway does
    "short3": b"DSV",
    "link10": dextra.link_packet(TALKER, MOD)[:10],
    "oversize": b"DSVT" + bytes(1396),
    "dsvt55": _sample_header()[:55],
    "dsvt57": _sample_header() + b"\x00",
    "voice26": _sample_voice()[:26],
    "voice28": _sample_voice() + b"\x00",
}


def garbage_probe(ip, rig, name):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        if name == "nat1":
            talker.dest = (ip, dextra.PORT)
            with marked(rig, ip, "p15-" + name):
                reply = ask(talker, GARBAGE[name], seconds=0.5)
            ready(ip, listener, talker)
        else:
            ready(ip, listener, talker)
            with marked(rig, ip, "p15-" + name):
                reply = ask(talker, GARBAGE[name], seconds=0.5)
        clear(talker, listener)
        voice, _ = talk(talker, listener)
        return {"reply": kind_of(reply), "served": heard(listener) == voice}
    finally:
        done(talker, listener)


@pytest.mark.parametrize("name", list(GARBAGE))
def test_dextra_garbage_ignored(rig, name):
    """P-DXT-15 (diff): unknown sizes and wrong-size DSVT get no reply and
    the reflector keeps serving (the next stream arrives whole)."""
    got = both(rig, garbage_probe, rig, name)
    print("P-DXT-15 %s:" % name, got)
    same(got, "reply", "served")


def stranger_probe(ip, rig):
    stranger, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener)
        stranger.dest = (ip, dextra.PORT)
        with marked(rig, ip, "p16-not-linked"):
            send(stranger, stream(sid(), frames()), listener)
        return {"heard": dsvt_count(listener) > 0}
    finally:
        stranger.dest = None
        done(stranger, listener)


def test_dextra_dsvt_not_linked(rig):
    """P-DXT-16 (diff): a stream from an address that never linked does
    not reach the module's listener."""
    got = both(rig, stranger_probe, rig)
    print("P-DXT-16 stranger stream:", got)
    same(got, "heard")


def variant_probe(ip, stream_kw=None, other_listener=False, raw_header=None, odd=None):
    """talker (MOD) sends one stream built with stream_kw; listener on MOD
    (and one on OTHER): what they got. odd = (rig, tag): the stream is
    malformed on purpose, bracketed with marks."""
    talker, listener, other = new(TALKER), new(LISTENER), new(GW, OTHER)
    try:
        ready(ip, listener, other, talker)
        voice = frames(seed="variant")
        packets = stream(sid(), voice, **(stream_kw or {}))
        if raw_header is not None:
            packets[0] = raw_header(packets[0])
        with marked(odd[0], ip, odd[1]) if odd else contextlib.nullcontext():
            send(talker, packets, listener, other)
        return {"voice": voice, "sent": packets, "relayed": len(heard(listener)) > 0,
                "exact": heard(listener) == voice, "other_relayed": len(heard(other)) > 0,
                "headers": headers(listener), "voices": voices(listener)}
    finally:
        done(talker, listener, other)


def _patch(offset, value):
    def patch(packet):
        data = bytearray(packet)
        data[offset:offset + len(value)] = value
        return bytes(data)
    return patch


PREFIX = {"byte8": _patch(8, b"\x21"), "byte14": _patch(14, b"\x00"), "ids": _patch(9, b"\x00\x02\x01")}


@pytest.mark.parametrize("name", list(PREFIX))
def test_dextra_header_prefix(rig, name):
    """P-DXT-17 (dev): header with byte 8 = 0x21, byte 14 = 0x00, or trunk
    ids 00 02 01 (IG default), voice unchanged: relayed or not."""
    got = both(rig, variant_probe, None, False, PREFIX[name], (rig, "p17-" + name))
    # one spec entry for all params: key per param (relayed_<name>)
    key = "relayed_" + name
    got = {n: dict(r, **{key: r["relayed"]}) for n, r in got.items()}
    print("P-DXT-17 %s:" % name, {n: {key: r[key]} for n, r in got.items()})
    judge_dev(rig, "P-DXT-17", got, key)


# ---------------------------------------------------------------- standard stream (P-DXT-18, 20, 29, 30, 32, 35, 37, 43, 45-47)

def std_probe(ip):
    """talker N0CALL on MOD sends one JARL-layout stream (valid P_FCS, empty
    suffix, flags 0); l1 and l2 (two callsigns, one IP) on MOD, other on
    OTHER."""
    talker, l1, l2, other = new(TALKER), new(LISTENER), new(EXTRA), new(GW, OTHER)
    try:
        ready(ip, l1, l2, other, talker)
        voice = frames(FRAMES, "std")
        stream_id = sid()
        packets = stream(stream_id, voice)
        send(talker, packets, l1, l2, other)
        return {"sid": stream_id, "voice": voice, "sent_header": packets[0],
                "l1": list(l1.client.inbox), "l2": list(l2.client.inbox),
                "other": list(other.client.inbox), "talker": list(talker.client.inbox)}
    finally:
        done(talker, l1, l2, other)


@pytest.fixture(scope="module")
def std(rig):
    return both(rig, std_probe)


def _hdrs(inbox):
    return [h for h in map(dextra.parse_header_full, inbox) if h]


def _voices(inbox):
    return [v for v in map(dextra.parse_voice_raw, inbox) if v]


def _ambe(inbox):
    return [v["ambe"] for v in _voices(inbox) if not v["last"]]


def test_dextra_header_opens_stream(rig, std):
    """P-DXT-18 (hb): a 56 B JARL-layout header (valid P_FCS) opens a stream:
    the listener gets a header and every voice frame."""
    got = {n: {"headers": len(_hdrs(r["l1"])), "exact": _ambe(r["l1"]) == r["voice"]} for n, r in std.items()}
    print("P-DXT-18 header opens stream:", got)
    for name, r in got.items():
        assert r["headers"] > 0 and r["exact"], "%s: %s" % (name, r)


def test_dextra_egress_header_crc(rig, std):
    """P-DXT-20 (hb): every egress header carries a valid P_FCS for the
    bytes it sends (JARL 4.1.1 k)."""
    got = {n: {"crc_ok": [h["crc_ok"] for h in _hdrs(r["l1"])], "crc": sorted({h["crc"].hex() for h in _hdrs(r["l1"])})}
           for n, r in std.items()}
    print("P-DXT-20 egress P_FCS:", got)
    for name, r in got.items():
        assert r["crc_ok"] and all(r["crc_ok"]), "%s: egress header CRC %s" % (name, r)


def test_dextra_egress_counter(rig, std):
    """P-DXT-29 (hb): egress header byte 0x80; voice counters 0..20 in
    order; the last frame carries 0x40 with the next counter."""
    got = {}
    for n, r in std.items():
        vs = _voices(r["l1"])
        body = [v["counter"] for v in vs if not v["last"]]
        lasts = [v["counter"] for v in vs if v["last"]]
        got[n] = {"byte14": sorted({h["byte14"] for h in _hdrs(r["l1"])}),
                  "counters_ok": body == [i % 21 for i in range(len(r["voice"]))],
                  "last": ["0x%02x" % x for x in lasts]}
    print("P-DXT-29 egress counter:", got)
    for name, r in got.items():
        assert r["byte14"] == [0x80] and r["counters_ok"], "%s: %s" % (name, r)
        assert r["last"] == ["0x%02x" % (0x40 | FRAMES % 21)], "%s: last frame counter %s" % (name, r["last"])


def test_dextra_egress_sync(rig, std):
    """P-DXT-30 (hb): egress frames with counter 0 carry 55 2D 16 as slow
    data, no other frame does."""
    got = {}
    for n, r in std.items():
        body = [v for v in _voices(r["l1"]) if not v["last"]]
        got[n] = {"sync_at_0": all(v["slow"] == dextra.SYNC_DATA for v in body if v["counter"] == 0),
                  "sync_elsewhere": sum(v["slow"] == dextra.SYNC_DATA for v in body if v["counter"] != 0),
                  "frames0": sum(v["counter"] == 0 for v in body)}
    print("P-DXT-30 egress sync:", got)
    for name, r in got.items():
        assert r["frames0"] and r["sync_at_0"] and not r["sync_elsewhere"], "%s: %s" % (name, r)


def module_free_probe(ip):
    a, b, listener = new(TALKER), new(GW), new(LISTENER)
    try:
        ready(ip, listener, a, b)
        _, sid_a = talk(a, listener, b, wait=0.6)
        last_a = [v for v in voices(listener) if v["last"] and v["stream_id"] == sid_a]
        clear(a, b, listener)
        voice_b, _ = talk(b, listener, a, voice=frames(30, "p32b"))
        return {"last_seen": bool(last_a), "next_exact": heard(listener) == voice_b}
    finally:
        done(a, b, listener)


def test_dextra_last_frame_ends_stream(rig):
    """P-DXT-32 (hb): the last frame (0x40) reaches the listener and ends
    the stream: another talker 0.6 s later is relayed whole."""
    got = both(rig, module_free_probe)
    print("P-DXT-32 last frame:", got)
    for name, r in got.items():
        assert r["last_seen"] and r["next_exact"], "%s: %s" % (name, r)


def test_dextra_module_fanout(rig, std):
    """P-DXT-35 (diff): both listeners of the module get the stream exactly
    once; the listener on another module gets nothing."""
    got = {n: {"l1_once": _ambe(r["l1"]) == r["voice"], "l2_once": _ambe(r["l2"]) == r["voice"],
               "other": sum(d.startswith(b"DSVT") for d in r["other"])} for n, r in std.items()}
    print("P-DXT-35 fan-out:", got)
    same(got, "l1_once", "l2_once", "other")


def test_dextra_egress_rpt_fields(rig, std):
    """P-DXT-37 (diff + dev): egress RPT2 (diff); RPT1, UR, MY, suffix and
    flags (dev). Ingress: RPT2 XLX999 P, RPT1 N0CALL P, UR CQCQCQ, MY
    N0CALL, empty suffix."""
    got = {}
    for n, r in std.items():
        h = (_hdrs(r["l1"]) or [{}])[0]
        got[n] = {k: (h.get(k) or b"").decode("latin-1") for k in ("rpt2", "rpt1", "ur", "my", "suffix")}
    print("P-DXT-37 egress RPT fields:", got)
    same(got, "rpt2")
    judge_dev(rig, "P-DXT-37", got, "rpt1", "ur", "my", "suffix")


def test_dextra_no_self_echo(rig, std):
    """P-DXT-43 (diff): the talker gets none of its own stream back."""
    got = {n: {"echo": sum(d.startswith(b"DSVT") for d in r["talker"])} for n, r in std.items()}
    print("P-DXT-43 self echo:", got)
    same(got, "echo")


def test_dextra_egress_header_copies(rig, std):
    """P-DXT-45 (dev): header copies a listener gets before the first
    voice, and later in the stream."""
    got = {}
    for n, r in std.items():
        first = next((i for i, d in enumerate(r["l1"]) if dextra.parse_voice_raw(d)), len(r["l1"]))
        got[n] = {"copies": len(_hdrs(r["l1"][:first])), "later": len(_hdrs(r["l1"][first:]))}
    print("P-DXT-45 header copies:", got)
    judge_dev(rig, "P-DXT-45", got, "copies", "later")


def test_dextra_egress_stream_id(rig, std):
    """P-DXT-46 (hb + B10): one stream id on every egress packet of the
    transmission (hb); equal to the ingress id (B10, diff)."""
    got = {}
    for n, r in std.items():
        ids = {h["stream_id"] for h in _hdrs(r["l1"])} | {v["stream_id"] for v in _voices(r["l1"])}
        got[n] = {"ids": len(ids), "equal": ids == {r["sid"]}}
    print("P-DXT-46 egress stream id:", got)
    for name, r in got.items():
        assert r["ids"] == 1, "%s: %d stream ids in one transmission" % (name, r["ids"])
    same(got, "equal")


def test_dextra_two_callsigns_one_ip(rig, std):
    """P-DXT-47 (diff): two callsigns from one IP (two ports) are two
    clients; each gets the module's traffic once."""
    got = {n: {"first_once": _ambe(r["l1"]) == r["voice"], "second_once": _ambe(r["l2"]) == r["voice"]}
           for n, r in std.items()}
    print("P-DXT-47 two callsigns, one IP:", got)
    same(got, "first_once", "second_once")


# ---------------------------------------------------------------- 3 header variants

CRC = {"spaces": b"  ", "ffff": b"\xff\xff", "wrong": b"\x12\x34", "valid": None}


@pytest.mark.parametrize("name", list(CRC))
def test_dextra_header_crc_ingress(rig, name):
    """P-DXT-19 (diff, B12): the ingress P_FCS is not checked: spaces,
    FF FF (IG when not computed), a wrong value and a valid one."""
    got = both(rig, variant_probe, {"crc": CRC[name]}, False, None,
               None if name == "valid" else (rig, "p19-" + name))
    print("P-DXT-19 %s:" % name, {n: {"exact": r["exact"]} for n, r in got.items()})
    same(got, "exact")


INVALID_FIELDS = {"my": {"my": "n0call"}, "rpt1": {"rpt1": b"n0call P"}, "rpt2": {"rpt2": b"xlx999 P"}}


@pytest.mark.parametrize("name", list(INVALID_FIELDS))
def test_dextra_header_invalid_callsign(rig, name):
    """P-DXT-21 (dev): a header with lower-case MY, RPT1 or RPT2."""
    got = both(rig, variant_probe, INVALID_FIELDS[name], False, None, (rig, "p21-" + name))
    # one spec entry for all params: key per param (relayed_<name>)
    key = "relayed_" + name
    got = {n: dict(r, **{key: r["relayed"]}) for n, r in got.items()}
    print("P-DXT-21 %s:" % name, {n: {key: r[key]} for n, r in got.items()})
    judge_dev(rig, "P-DXT-21", got, key)


def test_dextra_header_flags_passed(rig):
    """P-DXT-22 (dev): flag bytes 40 01 02 in: what egress carries."""
    got = both(rig, variant_probe, {"flags": b"\x40\x01\x02"}, False, None, (rig, "p22-flags"))
    got = {n: {"relayed": r["relayed"], "flags": (r["headers"] or [{"flags": b""}])[0]["flags"].hex()}
           for n, r in got.items()}
    print("P-DXT-22 flags:", got)
    judge_dev(rig, "P-DXT-22", got, "flags")


def repeat_probe(ip):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice = frames(60, "p24")
        stream_id = sid()
        packets = stream(stream_id, voice)
        packets.insert(31, packets[0])                     # the same header after 30 frames
        send(talker, packets, listener)
        vs = voices(listener)
        first = next((i for i, d in enumerate(listener.client.inbox) if dextra.parse_voice_raw(d)), 0)
        return {"exact": heard(listener) == voice,
                "counters_ok": [v["counter"] for v in vs if not v["last"]] == [i % 21 for i in range(60)],
                "ids": len({v["stream_id"] for v in vs}),
                "mid_headers": len([h for h in map(dextra.parse_header_full, listener.client.inbox[first:]) if h])}
    finally:
        done(talker, listener)


def test_dextra_header_repeat_discarded(rig):
    """P-DXT-24 (hb, JARL 5.1 c): the same header again after 30 frames:
    no second stream, no restart (frames and counters continuous, one
    id). Header copies mid-stream on egress are recorded."""
    got = both(rig, repeat_probe)
    print("P-DXT-24 repeated header:", got)
    for name, r in got.items():
        assert r["exact"] and r["counters_ok"] and r["ids"] == 1, "%s: %s" % (name, r)


def outbound_echo_probe(ip):
    a, b, listener = new(TALKER), new(GW, OTHER), new(LISTENER, OTHER)
    try:
        ready(ip, listener, a, b)
        _, stream_id = talk(a, listener, b)
        clear(a, b, listener)
        voice = frames(30, "p26")
        send(b, stream(stream_id, voice, my=GW, module=OTHER), listener, a)
        return {"relayed": len(heard(listener)) > 0}
    finally:
        done(a, b, listener)


def test_dextra_outbound_id_echo(rig):
    """P-DXT-26 (dev): a stream on module Q from another address reuses the
    id that module P just sent out (stream ended): relayed or dropped."""
    got = both(rig, outbound_echo_probe)
    print("P-DXT-26 reused outbound id:", got)
    judge_dev(rig, "P-DXT-26", got, "relayed")


# ---------------------------------------------------------------- 4 voice

def _slow(name, voice_len):
    radio = dextra.radio_header(TALKER, module=MOD)
    groups = {"text": dextra.slow_text("N0CALL DEXTRA TEST"), "header": dextra.slow_header_resend(radio),
              "filler": [dextra.NULL_SLOW] * 20}[name]
    return [dextra.SYNC_DATA if i % 21 == 0 else groups[i % 21 - 1] for i in range(voice_len)]


def _pre_last_slow(r):
    body = [v for v in r["voices"] if not v["last"]]
    return body[-1]["slow"].hex() if body else None


@pytest.mark.parametrize("name", ["text", "header", "filler"])
def test_dextra_slow_data_passthrough(rig, name):
    """P-DXT-28 (diff): scrambled slow data (text message, header resend,
    filler) over two superframes reaches the listener unchanged; the frame
    before the last frame is compared apart (pre_last_slow, xlxd writes
    55 55 55 there: P-DXT-33)."""
    slow = _slow(name, FRAMES)
    got = both(rig, variant_probe, {"slow": slow})
    got = {n: {"exact": r["exact"],
               "slow_exact": [v["slow"] for v in r["voices"] if not v["last"]][:-1] == slow[:-1],
               "pre_last_slow": _pre_last_slow(r), "sent_pre_last": slow[-1].hex()}
           for n, r in got.items()}
    print("P-DXT-28 %s:" % name, got)
    same(got, "exact", "slow_exact", "pre_last_slow")


def counter_probe(ip, rig, name):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice = frames(42, "p31")
        stream_id = sid()
        packets = [dextra.header_packet(stream_id, my=TALKER, module=MOD)]
        sent = [i % 32 if name == "over20" else (i % 21) | 0x80 for i in range(len(voice))]
        for i, (f, counter) in enumerate(zip(voice, sent)):
            packets.append(dextra.voice_packet(stream_id, counter, f,
                                               dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA))
        packets.append(dextra.last_frame(stream_id, 42))
        with marked(rig, ip, "p31-" + name):
            send(talker, packets, listener)
        body = [v["counter"] for v in voices(listener) if not v["last"]]
        return {"relayed": len(body), "egress_mod21": body == [i % 21 for i in range(len(body))],
                "raw_mod21": body == [c % 21 for c in sent[:len(body)]], "exact": heard(listener) == voice}
    finally:
        done(talker, listener)


@pytest.mark.parametrize("name", ["over20", "bit80"])
def test_dextra_counter_out_of_range(rig, name):
    """P-DXT-31 (dev): voice counters 0..31 (over20) or with bit 0x80 set:
    relayed frames and whether egress counts 0..20 (raw_mod21: egress is
    the received byte mod 21, recorded)."""
    got = both(rig, counter_probe, rig, name)
    print("P-DXT-31 %s:" % name, got)
    judge_dev(rig, "P-DXT-31", got, "exact", "egress_mod21")


LAST_PAYLOAD = frames(1, "p33")[0] + dextra.NULL_SLOW


def test_dextra_last_frame_payload(rig):
    """P-DXT-33 (dev): the last frame carries real AMBE + filler (as
    DroidStar): egress last-frame payload passed or replaced, and the slow
    data of the frame before it (pre_last_kept)."""
    got = both(rig, variant_probe, {"last_payload": LAST_PAYLOAD})
    got = {n: {"payload": next((v["ambe"] + v["slow"] for v in r["voices"] if v["last"]), b"").hex(),
               "pre_last_slow": _pre_last_slow(r)} for n, r in got.items()}
    for r in got.values():
        r["passed"] = r["payload"] == LAST_PAYLOAD.hex()
        r["jarl_end"] = r["payload"].startswith(dextra.END_PATTERN[:6].hex())
        r["pre_last_kept"] = r["pre_last_slow"] == dextra.SLOW_DATA.hex()
    print("P-DXT-33 last frame payload:", got)
    judge_dev(rig, "P-DXT-33", got, "passed", "jarl_end", "pre_last_kept")


def after_last_probe(ip, rig):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        voice = frames(30, "p34")
        stream_id = sid()
        extra = [dextra.voice_packet(stream_id, (31 + i) % 21, f) for i, f in enumerate(frames(20, "p34x"))]
        with marked(rig, ip, "p34-after-last"):
            send(talker, stream(stream_id, voice) + extra, listener)
        vs = voices(listener)
        at = next((i for i, v in enumerate(vs) if v["last"]), len(vs))
        return {"before_exact": [v["ambe"] for v in vs[:at]] == voice, "after_last": len(vs) - at - 1 if at < len(vs) else None}
    finally:
        done(talker, listener)


def test_dextra_voice_after_last(rig):
    """P-DXT-34 (diff): 20 voice frames with the same id after the last
    frame are not relayed."""
    got = both(rig, after_last_probe, rig)
    print("P-DXT-34 voice after last:", got)
    same(got, "before_exact", "after_last")


def test_dextra_first_counter_not_zero(rig):
    """P-DXT-53 (diff): a stream whose first voice counter is 1 (DroidStar)
    is relayed from its first frame."""
    got = both(rig, variant_probe, {"counter0": 1})
    got = {n: {"exact": r["exact"], "first_counter": next((v["counter"] for v in r["voices"]), None)}
           for n, r in got.items()}
    print("P-DXT-53 first counter 1:", got)
    same(got, "exact", "first_counter")


# ---------------------------------------------------------------- 5 routing

def test_dextra_rpt2_other_module(rig):
    """P-DXT-36 (dev): talker linked to P sends RPT2 = XLX999 Q: which
    module's listener gets it."""
    got = both(rig, variant_probe, {"rpt2": b"XLX999 " + OTHER.encode()})
    got = {n: {"linked_heard": r["relayed"], "rpt2_heard": r["other_relayed"]} for n, r in got.items()}
    print("P-DXT-36 RPT2 other module:", got)
    judge_dev(rig, "P-DXT-36", got, "linked_heard", "rpt2_heard")


def second_module_probe(ip):
    c, ta, tb = new(LISTENER), new(TALKER), new(GW, OTHER)
    try:
        ready(ip, c, ta, tb)
        clear(c)
        c.link_to(OTHER)
        settle(c, seconds=LINK_WAIT)
        reply = next((kind_of(d) for d in c.client.inbox if kind_of(d) in REPLY_KINDS), None)
        time.sleep(CACHE)
        clear(c, ta, tb)
        talk(ta, c, tb)
        first = len(heard(c))
        clear(c, ta, tb)
        talk(tb, c, ta)
        second = len(heard(c))
        c.client.send(c.dest, dextra.unlink_packet(c.callsign, c.module))
        time.sleep(CACHE)
        clear(c, ta, tb)
        talk(ta, c, tb)
        after_p = len(heard(c))
        clear(c, ta, tb)
        talk(tb, c, ta)
        after_q = len(heard(c))
        c.client.send(c.dest, dextra.unlink_packet(c.callsign, OTHER))
        return {"second_reply": reply, "hears_first": first > 0, "hears_second": second > 0,
                "after_unlink": [after_p > 0, after_q > 0]}
    finally:
        done(c, ta, tb)


def test_dextra_second_module_link(rig):
    """P-DXT-39 (dev): a link to Q from the address and callsign already
    linked to P (no unlink): reply, which modules it hears, and what one
    unlink leaves."""
    got = both(rig, second_module_probe)
    print("P-DXT-39 second module:", got)
    judge_dev(rig, "P-DXT-39", got, "second_reply", "hears_first", "hears_second", "after_unlink")


# ---------------------------------------------------------------- 6 stream lifetime

def headless_probe(ip, rig):
    talker, listener = new(TALKER), new(LISTENER)
    try:
        ready(ip, listener, talker)
        with marked(rig, ip, "p42-no-header"):
            send(talker, stream(sid(), frames(seed="p42"), head=False), listener)
        return {"relayed": len(heard(listener)) > 0}
    finally:
        done(talker, listener)


def test_dextra_voice_without_header(rig):
    """P-DXT-42 (dev): voice whose stream id had no header."""
    got = both(rig, headless_probe, rig)
    print("P-DXT-42 no header:", got)
    judge_dev(rig, "P-DXT-42", got, "relayed")


def late_listener_probe(ip):
    talker, late = new(TALKER), new(LISTENER)
    try:
        ready(ip, talker)
        late.dest = (ip, dextra.PORT)
        stream_id = sid()
        packets = stream(stream_id, frames(300, "p44"))
        talker.client.send(talker.dest, packets[0])
        time.sleep(HEAD_GAP)
        start = time.monotonic()
        for tick, packet in enumerate(packets[1:]):
            pace(start, tick, GAP)
            talker.client.send(talker.dest, packet)
            if tick == 25:
                late.client.send(late.dest, dextra.link_packet(late.callsign, late.module))
            late.client.drain()
        settle(late, talker, seconds=TAIL)
        own = [d for d in late.client.inbox if d.startswith(b"DSVT") and
               ((dextra.parse_header_full(d) or dextra.parse_voice_raw(d) or {}).get("stream_id") == stream_id)]
        first = own[0] if own else None
        return {"acked": any(kind_of(d) == "ack" for d in late.client.inbox),
                "header_first": bool(first) and dextra.parse_header_full(first) is not None,
                "got_voice": any(dextra.parse_voice_raw(d) for d in own)}
    finally:
        done(talker, late)


def test_dextra_late_listener_header(rig):
    """P-DXT-44 (dev): a client that links 0.5 s into a 6 s stream: does
    its first packet of that stream carry the header."""
    got = both(rig, late_listener_probe)
    print("P-DXT-44 late listener:", got)
    for name, r in got.items():
        assert r["acked"], "%s: late link not ACKed" % name
    judge_dev(rig, "P-DXT-44", got, "header_first", "got_voice")


# ---------------------------------------------------------------- 7 several clients

def same_callsign_probe(ip):
    talker, a, b = new(TALKER), new(LISTENER), new(LISTENER)
    try:
        ready(ip, a, b, talker)
        voice, _ = talk(talker, a, b)
        return {"first_once": heard(a) == voice, "second_once": heard(b) == voice}
    finally:
        done(talker, a, b)


def test_dextra_same_callsign_two_ports(rig):
    """P-DXT-48 (dev): one callsign linked from two ports: each gets the
    module's traffic once (or one of them is replaced)."""
    got = both(rig, same_callsign_probe)
    print("P-DXT-48 same callsign, two ports:", got)
    judge_dev(rig, "P-DXT-48", got, "first_once", "second_once")


# ---------------------------------------------------------------- 8 reflector peers

def peer_link_probe(ip):
    c = new(PEER, MOD)
    try:
        got = link(ip, c, dextra.xrf_link_packet(PEER, "B", MOD))
        unlink = ask(c, dextra.unlink_packet(PEER, "B")) if got["kind"] else None
        return {"reply": got["kind"], "raw": got["raw"].decode("latin-1"), "unlink_reply": kind_of(unlink)}
    finally:
        c.dest = None
        done(c)


def test_dextra_xrf_peer_link(rig):
    """P-DXT-50 (dev): a link from XRF997 (its module B to P): reply form
    and the reply to its unlink."""
    got = both(rig, peer_link_probe)
    print("P-DXT-50 XRF peer link:", got)
    judge_dev(rig, "P-DXT-50", got, "reply", "unlink_reply")


def peer_routing_probe(ip):
    peer, listener, other = new(PEER, MOD), new(LISTENER), new(GW, OTHER)
    try:
        ready(ip, listener, other)
        got = link(ip, peer, dextra.xrf_link_packet(PEER, "B", MOD))
        assert got["kind"], "%s: no reply to the XRF link" % ip
        time.sleep(CACHE)
        clear(peer, listener, other)
        voice = frames(seed="p51")
        send(peer, stream(sid(), voice, my=TALKER, rpt2=b"XLX999 " + OTHER.encode(), rpt1=b"XRF997 B"),
             listener, other)
        out = {"linked_heard": heard(listener) == voice, "rpt2_heard": len(heard(other)) > 0}
        peer.client.send(peer.dest, dextra.unlink_packet(PEER, "B"))
        peer.dest = None
        return out
    finally:
        done(peer, listener, other)


def test_dextra_xrf_peer_routing(rig):
    """P-DXT-51 (dev): XRF997 linked B -> P sends a header whose RPT2 names
    module Q: which module's listener gets it."""
    got = both(rig, peer_routing_probe)
    print("P-DXT-51 XRF peer routing:", got)
    judge_dev(rig, "P-DXT-51", got, "linked_heard", "rpt2_heard")
