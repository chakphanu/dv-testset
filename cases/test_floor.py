# SPDX-License-Identifier: MIT
"""Floor and admission behaviour on xlxd-ref and xlxd-sut (B-rows of the
DV-Router profile). Every case runs both reflectors in parallel.

Under DVT_SUT_PROFILE=xlxd the sut must behave as xlxd-ref; under another
profile the expectation comes from dvtest/specs/<profile>.py through the
behaviour_spec fixture (cases/conftest.py marks these cases behaviour,
except test_sid_preserved, which is interop).
"""
import socket
import threading
import time

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import ambed, dextra, dmr, m17, usrp

pytestmark = pytest.mark.rig

FRAMES = 100


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


def linked(ip, *made):
    for client in made:
        assert client.link(ip), "link to %s failed" % ip
        client.client.drain()
        client.client.inbox.clear()
    return made


def dstar_streams(packets):
    """{sid: [frames]} of D-STAR voice packets, last frames excluded."""
    out = {}
    for p in packets:
        v = dextra.parse_voice(p)
        if v and not v[2]:
            out.setdefault(v[0], []).append(v[3])
    return out


def dstar_stream_packets(sid, frames, last=True):
    out = [dextra.voice(sid, i, f, False, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA)
           for i, f in enumerate(frames)]
    if last:
        out.append(dextra.voice(sid, len(frames), bytes(9), True))
    return out


# ---------------------------------------------------------------- B1

def first_talker(ip, module):
    talker_a = clients.make("dstar", module, 0)
    talker_b = clients.make("dmr", module, 0)
    listener = clients.make("dstar", module, 1)
    try:
        linked(ip, listener, talker_a, talker_b)
        frames_a = signals.prng_frames("dstar", FRAMES, seed="b1a")
        frames_b = signals.prng_frames("dmr", FRAMES, seed="b1b")
        sid_a, sid_b = clients.fresh_stream_id("dstar"), clients.fresh_stream_id("dmr")
        bursts, seq = dmr.voice_packets(frames_b, talker_b.id, talker_b.tg, sid_b)
        voice_a = dstar_stream_packets(sid_a, frames_a)
        start = time.monotonic() + 0.1
        pace(start, 0)
        talker_a.client.send(talker_a.dest, talker_a.header(sid_a))
        pace(start + 0.005, 0)
        talker_b.client.send(talker_b.dest, dmr.packet(0, talker_b.id, talker_b.tg, talker_b.id, 0xA1, sid_b,
                                                       dmr.data_sync_payload()))
        # both streams run 2 s side by side: A every 20 ms, B every 60 ms
        for tick in range(len(voice_a)):
            pace(start + 0.02, tick)
            talker_a.client.send(talker_a.dest, voice_a[tick])
            if tick % 3 == 0 and tick // 3 < len(bursts):
                talker_b.client.send(talker_b.dest, bursts[tick // 3])
            listener.client.drain()
        talker_b.client.send(talker_b.dest, dmr.packet(seq, talker_b.id, talker_b.tg, talker_b.id, 0xA2, sid_b,
                                                       dmr.data_sync_payload()))
        listener.client.pump(1.0)
        streams = dstar_streams(listener.client.inbox)
    finally:
        clients.close(talker_a, talker_b, listener)
    return {"streams": {sid: len(f) for sid, f in streams.items()},
            "a_exact": streams.get(sid_a) == frames_a}


def test_first_talker_wins(rig):
    """B1: two clients key up on one module 5 ms apart: exactly one stream
    reaches the listener, complete and unmixed (which one is not asserted).
    Same expectation under every profile."""
    got = both(rig, first_talker, "M")
    print("first talker:", got)
    for name, r in got.items():
        counts = [n for n in r["streams"].values() if n]
        assert len(counts) == 1, "%s: %d streams reached the listener: %s" % (name, len(counts), r["streams"])
        assert counts[0] >= FRAMES * 0.8, "%s: winner heard %d of %d frames" % (name, counts[0], FRAMES)


# ---------------------------------------------------------------- B2

def late_on_busy(ip, module):
    """A D-STAR stream of 100 frames; at frame 30 a DMR talker sends a
    header and keeps talking ~1.4 s past the end of A. Frames of streams
    other than A reaching a D-STAR listener."""
    talker_a = clients.make("dstar", module, 0)
    talker_b = clients.make("dmr", module, 0)
    listener = clients.make("dstar", module, 1)
    try:
        linked(ip, listener, talker_a, talker_b)
        sid_a, sid_b = clients.fresh_stream_id("dstar"), clients.fresh_stream_id("dmr")
        voice_a = dstar_stream_packets(sid_a, signals.prng_frames("dstar", FRAMES, seed="b2a"))
        bursts, seq = dmr.voice_packets(signals.prng_frames("dmr", 165, seed="b2b"), talker_b.id,
                                        talker_b.tg, sid_b)
        talker_a.client.send(talker_a.dest, talker_a.header(sid_a))
        start = time.monotonic() + 0.1
        b_index = 0
        for tick in range(30 + len(bursts) * 3):
            pace(start, tick)
            if tick < len(voice_a):
                talker_a.client.send(talker_a.dest, voice_a[tick])
            if tick == 30:
                talker_b.client.send(talker_b.dest, dmr.packet(0, talker_b.id, talker_b.tg, talker_b.id, 0xA1,
                                                               sid_b, dmr.data_sync_payload()))
            elif tick > 30 and (tick - 30) % 3 == 0 and b_index < len(bursts):
                talker_b.client.send(talker_b.dest, bursts[b_index])
                b_index += 1
            listener.client.drain()
        talker_b.client.send(talker_b.dest, dmr.packet(seq, talker_b.id, talker_b.tg, talker_b.id, 0xA2, sid_b,
                                                       dmr.data_sync_payload()))
        listener.client.pump(1.0)
        streams = dstar_streams(listener.client.inbox)
    finally:
        clients.close(talker_a, talker_b, listener)
    return {"a": len(streams.get(sid_a, [])),
            "other": sum(len(f) for sid, f in streams.items() if sid != sid_a)}


def test_late_entry_denied(rig, behaviour_spec):
    """B2: a header that finds the module busy. DV-Router late_entry
    "deny": the late talker is never relayed, also not after the first
    stream ends (no promotion). xlxd profile: the sut relays the late
    talker iff xlxd-ref does. The deny is silent by the DV-Router spec (no
    NACK)."""
    got = both(rig, late_on_busy, "N")
    print("late entry on a busy module:", got)
    for name, r in got.items():
        assert r["a"] >= FRAMES * 0.8, "%s: first stream heard %d of %d frames" % (name, r["a"], FRAMES)
    if (behaviour_spec or {}).get("late_entry") == "deny":
        assert got["sut"]["other"] == 0, "sut relayed %d frames of the late talker" % got["sut"]["other"]
    else:
        assert (got["sut"]["other"] > 0) == (got["ref"]["other"] > 0), "late talker: sut %d, ref %d frames" % (
            got["sut"]["other"], got["ref"]["other"])


def test_late_entry_pending(rig, behaviour_spec):
    """B2 knob late_entry "pending": the late entry goes live from the
    first stream's close, transcoded, buffered frames older than 100 ms
    dropped. Placeholder until a profile configures the knob."""
    if (behaviour_spec or {}).get("late_entry") != "pending":
        pytest.skip("late_entry knob 'pending' not configured for this profile")
    pytest.skip("late_entry 'pending' case not written yet")


# ---------------------------------------------------------------- B3 USRP

USRP_MODULE, USRP_PEER_PORT, USRP_XLXD_PORT = "C", 32003, 34003


def usrp_peer():
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", USRP_PEER_PORT))
    peer.setblocking(False)
    return peer


def usrp_drain(peer, out):
    while True:
        try:
            out.append((time.monotonic(), peer.recv(2048)))
        except BlockingIOError:
            return


def test_usrp_unkey_after_silence(rig_usrp, behaviour_spec):
    """B3: a D-STAR talker on the USRP-bridged module stops without a last
    frame; the USRP peer gets the header-only unkey within 100 ms of the
    last voice (+100 ms tolerance). xlxd-ref has no USRP: under the xlxd
    profile the sut is held to PR #272's USRP_TX_TIMEOUT (100 ms)."""
    unkey_s = (behaviour_spec or {}).get("unkey_ms", 100) / 1000.0
    talker = clients.make("dstar", USRP_MODULE, 0)
    peer = usrp_peer()
    rx = []
    try:
        linked(rig_usrp.xlxd_sut, talker)
        frames = ambed.transcode(rig_usrp.ambed_ref, ambed.PCM, [ambed.AMBEPLUS],
                                 signals.frames(signals.tone(700, 1000)), port=rig_usrp.ambed_port)[1]
        sid = clients.fresh_stream_id("dstar")
        talker.client.send(talker.dest, talker.header(sid))
        start = time.monotonic() + 0.1
        for index, packet in enumerate(dstar_stream_packets(sid, frames, last=False)):
            pace(start, index)
            talker.client.send(talker.dest, packet)
            usrp_drain(peer, rx)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            usrp_drain(peer, rx)
            time.sleep(0.002)
    finally:
        peer.close()
        clients.close(talker)
    keyed = [t for t, p in rx if usrp.is_voice(p) and len(p) == usrp.VOICE_SIZE]
    unkeys = [t for t, p in rx if usrp.is_voice(p) and len(p) == usrp.HEADER_SIZE]
    assert len(keyed) >= 40, "USRP peer got %d keyed frames" % len(keyed)
    assert unkeys, "no USRP unkey after the stream went silent"
    gap = unkeys[0] - keyed[-1]
    print("USRP unkey %.0f ms after the last voice" % (gap * 1000))
    assert gap <= unkey_s + 0.1, "unkey %.0f ms after the last voice, expected <= %.0f ms" % (
        gap * 1000, (unkey_s + 0.1) * 1000)


def test_usrp_over_suppressed_when_busy(rig_usrp, behaviour_spec):
    """B3: a USRP over that starts while the module is busy. DV-Router
    suppresses the whole over (nothing of it is relayed, also after the
    busy stream ends). xlxd-ref has no USRP: under the xlxd profile the
    sut's behaviour is only recorded."""
    talker = clients.make("dstar", USRP_MODULE, 0)
    listener = clients.make("dstar", USRP_MODULE, 1)
    peer = usrp_peer()
    try:
        linked(rig_usrp.xlxd_sut, listener, talker)
        sid = clients.fresh_stream_id("dstar")
        voice = dstar_stream_packets(sid, signals.prng_frames("dstar", FRAMES, seed="b3"))
        pcm = signals.frames(signals.tone(700, 3000))
        talker.client.send(talker.dest, talker.header(sid))
        start = time.monotonic() + 0.1
        dest = (rig_usrp.xlxd_sut, USRP_XLXD_PORT)
        for tick in range(20 + len(pcm)):
            pace(start, tick)
            if tick < len(voice):
                talker.client.send(talker.dest, voice[tick])
            if tick >= 20:
                peer.sendto(usrp.voice(tick - 20, pcm[tick - 20]), dest)
            listener.client.drain()
        peer.sendto(usrp.unkey(len(pcm)), dest)
        listener.client.pump(1.0)
        streams = dstar_streams(listener.client.inbox)
    finally:
        peer.close()
        clients.close(talker, listener)
    other = sum(len(f) for s, f in streams.items() if s != sid)
    print("USRP over on a busy module: busy stream %d frames, over %d frames" % (
        len(streams.get(sid, [])), other))
    assert len(streams.get(sid, [])) >= FRAMES * 0.8, "busy stream heard %d frames" % len(streams.get(sid, []))
    if (behaviour_spec or {}).get("usrp_over_when_busy") == "suppress":
        assert other == 0, "sut relayed %d frames of a USRP over that started on a busy module" % other


# ---------------------------------------------------------------- B8 admission

def talk_dstar_sid(ip, module, sid, seed, listen_kind):
    """D-STAR talker on module with a given sid; frames heard by a listener
    of listen_kind on the same module."""
    talker = clients.make("dstar", module, 0)
    listener = clients.make(listen_kind, module, 1)
    try:
        linked(ip, listener, talker)
        frames = signals.prng_frames("dstar", 50, seed=seed)
        talker.client.send(talker.dest, talker.header(sid))
        talker.client.paced(talker.dest, dstar_stream_packets(sid, frames), 0.020, listener.client)
        return len(listener.collect(settle=1.0))
    finally:
        clients.close(talker, listener)


def test_admission_sid_zero(rig, behaviour_spec):
    """B8: a stream with stream id 0. DV-Router rejects it; xlxd profile:
    the sut relays it iff xlxd-ref does."""
    got = both(rig, talk_dstar_sid, "P", 0, "b8a", "dstar")
    print("sid 0 frames heard:", got)
    if (behaviour_spec or {}).get("admit_sid_zero") is False:
        assert got["sut"] == 0, "sut relayed %d frames of a sid 0 stream" % got["sut"]
    else:
        assert (got["sut"] > 0) == (got["ref"] > 0), "sid 0: sut %d, ref %d frames" % (got["sut"], got["ref"])


def second_floor(ip):
    """One DMR repeater holds the floor on Q, then sends a second stream
    (new sid) to R's talkgroup. Frames heard by a D-STAR listener on R."""
    talker = clients.make("dmr", "Q", 0)
    listener = clients.make("dstar", "R", 1)
    try:
        linked(ip, listener, talker)
        sid_q, sid_r = clients.fresh_stream_id("dmr"), clients.fresh_stream_id("dmr")
        tg_r = dmr.module_tg("R")
        q, seq_q = dmr.voice_packets(signals.prng_frames("dmr", 90, seed="b8q"), talker.id, talker.tg, sid_q)
        r, seq_r = dmr.voice_packets(signals.prng_frames("dmr", 60, seed="b8r"), talker.id, tg_r, sid_r)
        sync = dmr.data_sync_payload()
        talker.client.send(talker.dest, dmr.packet(0, talker.id, talker.tg, talker.id, 0xA1, sid_q, sync))
        start = time.monotonic() + 0.1
        for tick in range(len(q)):
            pace(start, tick, 0.060)
            talker.client.send(talker.dest, q[tick])
            if tick == 5:
                talker.client.send(talker.dest, dmr.packet(0, talker.id, tg_r, talker.id, 0xA1, sid_r, sync))
            elif 5 < tick <= 5 + len(r):
                talker.client.send(talker.dest, r[tick - 6])
            listener.client.drain()
        talker.client.send(talker.dest, dmr.packet(seq_q, talker.id, talker.tg, talker.id, 0xA2, sid_q, sync))
        talker.client.send(talker.dest, dmr.packet(seq_r, talker.id, tg_r, talker.id, 0xA2, sid_r, sync))
        return len(listener.collect(settle=1.0))
    finally:
        clients.close(talker, listener)


def test_admission_one_floor_per_client(rig, behaviour_spec):
    """B8: a client that already holds a floor opens a second stream on
    another module. DV-Router rejects it; xlxd profile: sut as xlxd-ref."""
    got = both(rig, second_floor)
    print("second floor of one client, frames heard on R:", got)
    if (behaviour_spec or {}).get("admit_second_floor") is False:
        assert got["sut"] == 0, "sut relayed %d frames of the client's second floor" % got["sut"]
    else:
        assert (got["sut"] > 0) == (got["ref"] > 0), "second floor: sut %d, ref %d frames" % (
            got["sut"], got["ref"])


def same_sid_two_rooms(ip):
    """Two local D-STAR talkers with the same sid on S and T, T starting
    0.3 s after S. Frames heard on S (M17 listener) and T (DMR listener)."""
    sid = clients.fresh_stream_id("dstar")
    talkers = {m: clients.make("dstar", m, i) for i, m in enumerate("ST")}
    listeners = {"S": clients.make("m17", "S", 1), "T": clients.make("dmr", "T", 1)}
    try:
        linked(ip, *listeners.values(), *talkers.values())
        packets = {m: dstar_stream_packets(sid, signals.prng_frames("dstar", 60, seed="b8" + m)) for m in "ST"}
        for m, t in talkers.items():
            t.client.send(t.dest, dextra.header(sid, t.callsign, m))
            if m == "S":
                time.sleep(0.3)
        start = time.monotonic()
        for tick in range(len(packets["S"]) + 15):
            pace(start, tick)
            if tick < len(packets["S"]):
                talkers["S"].client.send(talkers["S"].dest, packets["S"][tick])
            if 15 <= tick < 15 + len(packets["T"]):
                talkers["T"].client.send(talkers["T"].dest, packets["T"][tick - 15])
            for c in listeners.values():
                c.client.drain()
        return {m: len(c.collect(settle=1.0)) for m, c in listeners.items()}
    finally:
        clients.close(*talkers.values(), *listeners.values())


def test_admission_same_sid_two_rooms(rig, behaviour_spec):
    """B8: the same sid from two local clients in two rooms. DV-Router
    allows it (only remote-origin sids are checked across rooms); xlxd
    drops a stream whose sid is open on any module (IsStreamOpen,
    creflector.cpp 1099-1108). xlxd profile: sut as xlxd-ref."""
    got = both(rig, same_sid_two_rooms)
    print("same sid in two rooms, frames heard:", got)
    for name, r in got.items():
        assert r["S"] >= 40, "%s: first room heard %d frames" % (name, r["S"])
    if (behaviour_spec or {}).get("admit_same_sid_local") is True:
        assert got["sut"]["T"] >= 40, "sut dropped the second room's stream (%d frames)" % got["sut"]["T"]
    else:
        assert (got["sut"]["T"] > 0) == (got["ref"]["T"] > 0), "second room: sut %s, ref %s" % (
            got["sut"], got["ref"])


# ---------------------------------------------------------------- B10 interop

def sid_kept(ip, kind):
    """Does the same-kind listener see the talker's stream id?"""
    talker = clients.make(kind, "U", 0)
    listener = clients.make(kind, "U", 1)
    try:
        linked(ip, listener, talker)
        frames = signals.prng_frames(kind, 30, seed="b10")
        sid = clients.talk(talker, kind, frames, tail=1.0)
        listener.client.pump(0.5)
        if kind == "dstar":
            seen = {v[0] for v in map(dextra.parse_voice, listener.client.inbox) if v}
        elif kind == "dmr":
            seen = {p["stream"] for p in map(dmr.parse, listener.client.inbox) if p}
        else:
            seen = {p["stream_id"] for p in map(m17.parse, listener.client.inbox) if p}
        return {"kept": seen == {sid}, "seen": sorted(seen)[:3], "sent": sid}
    finally:
        clients.close(talker, listener)


@pytest.mark.parametrize("kind", clients.KINDS)
def test_sid_preserved(rig, kind):
    """B10 (interop): egress keeps the ingress stream id on same-mode relay
    wherever xlxd-ref does (D-STAR; xlxd assigns new ids on DMR and M17
    egress, so keeping them there cannot break a peer). A profile spec
    entry "sid_kept" lists the kinds the sut must keep regardless (DV-Router:
    D-STAR, DMR, M17)."""
    from dvtest import specs
    got = both(rig, sid_kept, kind)
    print("%s sid:" % kind, got)
    if got["ref"]["kept"]:
        assert got["sut"]["kept"], "%s stream id not kept by the sut although xlxd-ref keeps it: %s" % (
            kind, got["sut"])
    if kind in (specs.rule(rig, "sid_kept") or ()):
        assert got["sut"]["kept"], "%s stream id not kept, the profile requires it: %s" % (kind, got["sut"])


CROSS_SID = [("dmr", "dstar"), ("dmr", "m17"), ("dstar", "dmr"), ("dstar", "m17"), ("m17", "dstar")]


def sid_cross(ip, src, dst):
    """Does a dst listener see the talker's stream id in its own width
    (low 16 bits for D-STAR/M17, zero extended for DMR)?"""
    talker = clients.make(src, "V", 0)
    listener = clients.make(dst, "V", 1)
    try:
        linked(ip, listener, talker)
        sid = clients.talk(talker, src, signals.prng_frames(src, 30, seed="b10x"), tail=1.0)
        listener.client.pump(0.5)
        if dst == "dstar":
            seen = {v[0] for v in map(dextra.parse_voice, listener.client.inbox) if v}
        elif dst == "dmr":
            seen = {p["stream"] for p in map(dmr.parse, listener.client.inbox) if p}
        else:
            seen = {p["stream_id"] for p in map(m17.parse, listener.client.inbox) if p}
        want = sid if dst == "dmr" else sid & 0xFFFF
        return {"mapped": seen == {want}, "seen": sorted(seen)[:3], "want": want}
    finally:
        clients.close(talker, listener)


@pytest.mark.parametrize("pair", CROSS_SID, ids=["%s-%s" % p for p in CROSS_SID])
def test_sid_cross_mapping(rig, pair):
    """B10 (interop): cross-protocol egress carries the stream's canonical
    id in the target width. Required when the profile spec says "sid_cross":
    "canonical" (DV-Router); under the xlxd profile the sut must map where
    xlxd-ref does."""
    from dvtest import specs
    got = both(rig, sid_cross, *pair)
    print("%s -> %s sid:" % pair, got)
    if specs.rule(rig, "sid_cross") == "canonical":
        assert got["sut"]["mapped"], "%s -> %s: sut egress sid %s, want %s" % (
            pair + (got["sut"]["seen"], got["sut"]["want"]))
    elif got["ref"]["mapped"]:
        assert got["sut"]["mapped"], "%s -> %s: ref maps the sid, sut does not: %s" % (pair + (got["sut"],))


def test_sid_patch_second_leg_rewritten(rig):
    """B10 exception: a patch that sends one stream to two modules of the
    same peer rewrites the second leg's sid. Placeholder: the rig has no
    patch configuration yet."""
    pytest.skip("patch to two modules of one peer: no rig configuration yet")


# ---------------------------------------------------------------- B14

def test_b14_placeholder(behaviour_spec):
    """B14: reserved by the DV-Router profile, not specified yet."""
    pytest.skip("B14 not specified yet")
