# SPDX-License-Identifier: MIT
"""Client life on xlxd-ref and xlxd-sut of the docker rig.

C7: a talker that changes module (unlink, relink elsewhere; DExtra and
DMR) is heard on the new module only.
C8: a talker that stops without a last frame: the reflector ends the
stream by its timeout (reference 1.6 s) and the module is free for the
next talker. Upstream xlxd sends listeners no last frame for a timed-out
stream; that is accepted and only reported.
"""
import threading
import time

import pytest

from dvtest import clients
from dvtest.audio import score, signals
from dvtest.net import pace
from dvtest.proto import ambed, dextra, dmr, m17

pytestmark = pytest.mark.rig

FRAMES = 30
STREAM_TIMEOUT_S = 1.6      # xlxd STREAM_TIMEOUT


@pytest.fixture(params=["xlxd_ref", "xlxd_sut"])
def xlxd(request, rig):
    return getattr(rig, request.param)


def heard(*listeners):
    out = [len(c.collect(settle=0.5)) for c in listeners]
    for c in listeners:
        c.client.inbox.clear()
    return out


def listen(ip, kind, module, index):
    client = clients.make(kind, module, index)
    assert client.link(ip), "%s listener link on %s failed" % (kind, module)
    client.client.drain()
    client.client.inbox.clear()
    return client


@pytest.mark.parametrize("kind", ["dstar", "dmr"])
def test_module_change_follows_talker(xlxd, kind):
    """C7: talk on F, move to G, talk again: each stream is heard on the
    talker's current module only."""
    frames = signals.prng_frames(kind, FRAMES, seed="c7")
    on_f = listen(xlxd, "m17", "F", 1)
    on_g = listen(xlxd, "dstar" if kind == "dmr" else "dmr", "G", 1)
    talker = clients.make(kind, "F", 0)
    try:
        assert talker.link(xlxd), "%s talker link failed" % kind
        clients.talk(talker, kind, frames, tail=1.0)
        first = heard(on_f, on_g)
        if kind == "dstar":
            talker.unlink()
            talker.module = "G"
            assert talker.link(xlxd), "D-STAR relink on G failed"
        else:
            talker.unlink()
            talker.join("G")
        clients.talk(talker, kind, frames, tail=1.0)
        second = heard(on_f, on_g)
    finally:
        clients.close(talker, on_f, on_g)
    least = FRAMES * 0.8
    assert first[0] >= least and first[1] == 0, "talk on F heard F/G = %s" % first
    assert second[0] == 0 and second[1] >= least, "talk on G heard F/G = %s" % second


def timeout_side(ip):
    """D-STAR header + voice on H without a last frame, then silence;
    listeners of every kind on H. Returns ({kind: end marker seen within
    the timeout window}, first stream frames heard, next talker frames)."""
    listeners = {k: listen(ip, k, "H", 1) for k in clients.KINDS}
    talker = clients.make("dstar", "H", 0)
    second = dmr.DmrClient("0.0.0.0", dmrid=clients.DMR_IDS[0], module="H")
    try:
        assert talker.link(ip) and second.link(ip), "talker links failed"
        for c in listeners.values():
            c.client.drain()
            c.client.inbox.clear()
        sid = clients.fresh_stream_id("dstar")
        talker.client.send(talker.dest, dextra.header(sid, talker.callsign, "H"))
        start = time.monotonic()
        for index, frame in enumerate(signals.prng_frames("dstar", FRAMES, seed="c8")):
            pace(start, index)
            talker.client.send(talker.dest, dextra.voice(sid, index, frame, False))
            for c in listeners.values():
                c.client.drain()
        deadline = time.monotonic() + STREAM_TIMEOUT_S + 0.4
        while time.monotonic() < deadline:
            for c in listeners.values():
                c.client.drain()
            time.sleep(0.01)
        inbox = {k: list(c.client.inbox) for k, c in listeners.items()}
        first = [v for v in map(dextra.parse_voice, inbox["dstar"]) if v]
        ended = {"dstar": any(v[2] for v in first),
                 "dmr": any(p.startswith(b"DMRD") and len(p) == dmr.PACKET_SIZE and p[15] & 0x3F == 0x22
                            for p in inbox["dmr"]),
                 "m17": any(m["eot"] for m in map(m17.parse, inbox["m17"]) if m)}
        for c in listeners.values():
            c.client.inbox.clear()
        clients.talk(second, "dmr", signals.prng_frames("dmr", FRAMES, seed="c8b"), tail=1.0)
        listeners["dstar"].client.pump(0.5)
        after = [v for v in map(dextra.parse_voice, listeners["dstar"].client.inbox) if v]
    finally:
        clients.close(talker, second, *listeners.values())
    assert not {v[0] for v in after} & {v[0] for v in first}, "the timed-out stream continued after the timeout"
    return ended, len(first), len(after)


def test_stream_timeout_frees_module(rig, behaviour_spec):
    """C8 / B3: a D-STAR talker stops without a last frame. Within the
    1.6 s stream timeout (+0.4 s) the module takes the next talker on both
    reflectors. End markers (D-STAR last frame, DMR terminator, M17 EOT) on
    each egress: under the xlxd profile the sut shows what xlxd-ref shows;
    under a spec with terminator "all_egress" (dvrouter) or the legacy
    synthesized_terminator divergence every egress gets one."""
    got = {}

    def side(name):
        try:
            got[name] = timeout_side(getattr(rig, "xlxd_" + name))
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n,)) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    print("stream timeout (end markers, first frames, next talker frames):", got)
    for name, (_ended, first, after) in got.items():
        assert first >= FRAMES * 0.8, "%s: first stream heard %d of %d frames" % (name, first, FRAMES)
        assert after >= FRAMES * 0.8, "%s: next talker %.1f s after the stop: %d frames (module held?)" % (
            name, STREAM_TIMEOUT_S + 0.4, after)
    all_egress = (behaviour_spec or {}).get("terminator") == "all_egress" or \
        "synthesized_terminator" in rig.divergences
    if all_egress:
        assert all(got["sut"][0].values()), "sut: end marker missing on %s" % [
            k for k, v in got["sut"][0].items() if not v]
    else:
        assert got["sut"][0] == got["ref"][0], "end markers: sut %s, ref %s" % (got["sut"][0], got["ref"][0])


LATE_MS = 30            # inside the close window (~170 ms after the last frame)
AFTER_MS = 600          # well after the codec stream CS


def late_entry(ip, gap_ms, frames):
    """DMR stream on F, then a D-STAR stream of frames whose header comes
    gap_ms after the DMR terminator. Returns (native D-STAR exact, native
    frame count, voiced cross-mode M17 Codec2 frames) of the second stream."""
    first = clients.make("dmr", "F", 0)
    second = clients.make("dstar", "F", 0)
    native = clients.make("dstar", "F", 1)
    cross = clients.make("m17", "F", 1)
    try:
        for client in (first, second, native, cross):
            assert client.link(ip), "link on %s failed" % ip
        dsid = clients.fresh_stream_id("dmr")
        packets, seq = dmr.voice_packets(signals.prng_frames("dmr", FRAMES, seed="c11"), first.id,
                                         first.tg, dsid)
        first.client.send(first.dest, dmr.packet(0, first.id, first.tg, first.id, 0xA1, dsid,
                                                 dmr.data_sync_payload()))
        first.client.pump(0.5)
        first.client.paced(first.dest, packets, 0.060, native.client, cross.client)
        sid = clients.fresh_stream_id("dstar")
        start = time.monotonic() + 0.060
        pace(start, 0)
        first.client.send(first.dest, dmr.packet(seq, first.id, first.tg, first.id, 0xA2, dsid,
                                                 dmr.data_sync_payload()))
        pace(start + gap_ms / 1000.0, 0)
        second.client.send(second.dest, dextra.header(sid, second.callsign, "F"))
        # stream ids of the first stream (its tail still arrives after this)
        for client in (native, cross):
            client.client.drain()
        old_dstar = {v[0] for v in map(dextra.parse_voice, native.client.inbox) if v}
        old_m17 = {p["stream_id"] for p in map(m17.parse, cross.client.inbox) if p}
        voice = [dextra.voice(sid, i, f, False, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA)
                 for i, f in enumerate(frames)] + [dextra.voice(sid, len(frames), bytes(9), True)]
        second.client.paced(second.dest, voice, 0.020, native.client, cross.client)
        native.client.pump(1.5)
        cross.client.pump(0.5)
        heard = [v[3] for v in map(dextra.parse_voice, native.client.inbox)
                 if v and not v[2] and v[0] not in old_dstar]
        converted = [f for p in map(m17.parse, cross.client.inbox)
                     if p and p["stream_id"] not in old_m17 for f in p["frames"]]
        return heard == frames, len(heard), [f for f in converted if any(f)]
    finally:
        clients.close(first, second, native, cross)


def test_late_entry_is_native_only(rig, behaviour_spec):
    """C11: a header that arrives while the previous stream on the module
    is still closing (upstream CReflector stashes it and promotes it
    native-only after the close, creflector.cpp ~556-580 and ~660-745;
    the close is the codec-stream CS ~170 ms after the last frame, after
    the jitter buffer drains) is relayed to same-mode listeners but gets no
    transcoder stream, so cross-mode listeners hear nothing; a header after
    the close is transcoded. xlxd-ref always shows this behaviour.

    Declared divergences: late_entry_transcoded (dvtest/divergences.py) -
    the sut transcodes the late entrant: its cross-mode listener must hear
    it (frames and the 700 Hz tone) while same-mode behaviour stays as on
    ref. Undeclared, the sut must match ref. Profile spec after_terminator
    "granted" (dvrouter: the DMR terminator releases the floor at once, so
    a header 30 ms later is a normal grant) expects the same as
    late_entry_transcoded."""
    tone = ambed.transcode(rig.ambed_ref, ambed.PCM, [ambed.AMBEPLUS],
                           signals.frames(signals.tone(700, 1000)), port=rig.ambed_port)[ambed.AMBEPLUS]
    got = {}

    def side(name, ip):
        try:
            got[name] = {"late": late_entry(ip, LATE_MS, tone), "after": late_entry(ip, AFTER_MS, tone)}
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n, getattr(rig, "xlxd_" + n))) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    summary = {n: {w: (r[0], r[1], len(r[2])) for w, r in got[n].items()} for n in got}
    print("late entry (native exact, native frames, voiced cross frames):", summary)
    ref, sut = got["ref"], got["sut"]
    least = len(tone) * 0.8
    assert ref["late"][0] and ref["after"][0], "ref: native relay not exact %s" % summary["ref"]
    assert not ref["late"][2], "ref: late entry was transcoded (%d voiced M17 frames)" % len(ref["late"][2])
    assert len(ref["after"][2]) >= least, "ref: entry after the close not transcoded %s" % summary["ref"]
    assert (sut["late"][0], sut["after"][0]) == (True, True), "sut: native relay %s" % summary["sut"]
    assert len(sut["after"][2]) >= least, "sut: entry after the close not transcoded %s" % summary["sut"]
    granted = (behaviour_spec or {}).get("after_terminator") == "granted"
    if granted or "late_entry_transcoded" in rig.divergences:
        assert len(sut["late"][2]) >= least, "sut declares late_entry_transcoded: %d cross frames" % (
            len(sut["late"][2]))
        pcm = b"".join(ambed.transcode(rig.ambed_ref, ambed.CODEC2_3200, [ambed.PCM], sut["late"][2],
                                       port=rig.ambed_port)[ambed.PCM])
        spec = score.spectrum(pcm)
        assert abs(score.dominant(spec) - 700) < 80, "sut late entrant decodes to %s Hz" % score.dominant(spec)
    else:
        assert not sut["late"][2], "sut transcoded the late entry (%d voiced M17 frames) like no ref " \
            "does; declare late_entry_transcoded if intended" % len(sut["late"][2])


LEAVE_AT = 50           # frame index of the leave packet in a 150-frame stream
FRAME_S = 0.020


def leave_mid_stream(ip, kind, module):
    """A D-STAR talker streams 150 frames on module; a listener of kind
    (dstar: DExtra unlink, dmr: TG 4000 unlink) leaves after LEAVE_AT
    frames. Returns (frames before the leave, frames after it, tail in s
    from the leave packet to the last frame received)."""
    listener = listen(ip, kind, module, 1)
    talker = clients.make("dstar", module, 0)
    try:
        assert talker.link(ip), "talker link failed"
        listener.client.inbox_times.clear()
        sid = clients.fresh_stream_id("dstar")
        frames = signals.prng_frames("dstar", 150, seed="c7b")
        talker.client.send(talker.dest, talker.header(sid))
        start = time.monotonic() + 0.3
        left = None
        for index, frame in enumerate(frames):
            pace(start, index)
            talker.client.send(talker.dest, dextra.voice(sid, index, frame, index == len(frames) - 1))
            if index == LEAVE_AT:
                listener.client.drain()
                if kind == "dstar":
                    listener.client.send(listener.dest, dextra.unlink_packet(listener.callsign, module))
                else:
                    # TG 4000 unlink; a 13 B RPTCL close is not honoured by
                    # xlxd (it reads the repeater id from bytes 4-7, "L" + 3 id
                    # bytes), so the listener would simply stay linked
                    listener._empty_stream(4000, 0.0)
                left = time.monotonic()
            listener.client.drain()
        listener.client.pump(1.5)
        if kind == "dstar":
            stamps = [t for p, t in zip(listener.client.inbox, listener.client.inbox_times)
                      if dextra.parse_voice(p)]
        else:
            # one DMRD burst carries three frames
            stamps = [t for p, t in zip(listener.client.inbox, listener.client.inbox_times)
                      if dmr.is_voice(p) for _ in range(3)]
    finally:
        listener.dest = None                    # it left
        clients.close(talker, listener)
    after = [t - left for t in stamps if t > left]
    return len(stamps) - len(after), len(after), (max(after) if after else 0.0)


@pytest.mark.parametrize("kind", ["dstar", "dmr"])
def test_leave_mid_stream_tail(rig, kind, behaviour_spec):
    """C7b: a listener that leaves while a stream is running. xlxd
    refreshes its per-module client list once a second (client cache,
    cdextraprotocol.h:43, cdmrmmdvmprotocol.h:52), so the reference keeps
    sending for up to 1.0 s after the leave; the sut must show the same
    unless it declares immediate_revoke, then nothing may arrive later
    than one frame time after the leave packet."""
    got = {}

    def side(name):
        try:
            got[name] = leave_mid_stream(getattr(rig, "xlxd_" + name), kind, "J")
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n,)) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    print("%s leave mid-stream (before, after, tail s):" % kind, got)
    for name, (before, _after, _tail) in got.items():
        assert before >= LEAVE_AT * 0.6, "%s: %d frames before the leave" % (name, before)
    ref, sut = got["ref"], got["sut"]
    assert ref[1] > 0 and ref[2] <= 1.0 + FRAME_S * 3, "ref tail after the leave: %d frames, %.2f s" % ref[1:]
    if (behaviour_spec or {}).get("revoke") == "immediate" or "immediate_revoke" in rig.divergences:
        assert sut[2] <= FRAME_S * 2, "sut declares immediate_revoke: frames %.2f s after the leave" % sut[2]
    else:
        assert sut[1] > 0 and sut[2] <= 1.0 + FRAME_S * 3, \
            "sut tail %d frames %.2f s, ref %d frames %.2f s" % (sut[1], sut[2], ref[1], ref[2])


TOT_S = 180.0


@pytest.mark.slow
def test_tot_180s(rig, behaviour_spec):
    """B4 TOT: a continuous transmission on the sut is closed at 180 s +- 1
    s and the listener gets the protocol terminator (last frame). Runs
    under a profile spec with tot_s or the legacy tot_180s divergence;
    xlxd-ref has no time-out timer."""
    if not ((behaviour_spec or {}).get("tot_s") or "tot_180s" in rig.divergences):
        pytest.skip("sut has no TOT under this profile (xlxd-ref has no TOT)")
    listener = listen(rig.xlxd_sut, "dstar", "K", 1)
    talker = clients.make("dstar", "K", 0)
    try:
        assert talker.link(rig.xlxd_sut)
        sid = clients.fresh_stream_id("dstar")
        talker.client.send(talker.dest, talker.header(sid))
        start = time.monotonic()
        frames = signals.prng_frames("dstar", 21, seed="tot")
        index = 0
        while time.monotonic() - start < TOT_S + 4.0:
            pace(start, index)
            talker.client.send(talker.dest, dextra.voice(sid, index, frames[index % 21], False))
            if index % 50 == 0:
                talker.keepalive()
                listener.keepalive()
            listener.client.drain()
            index += 1
        listener.client.pump(0.5)
        voice = [(t - start, dextra.parse_voice(p))
                 for p, t in zip(listener.client.inbox, listener.client.inbox_times)]
        times = [t for t, v in voice if v]
        last = [t for t, v in voice if v and v[2]]
    finally:
        clients.close(talker, listener)
    assert times, "listener heard nothing"
    assert abs(times[-1] - TOT_S) <= 1.0, "last frame %.1f s after the start, expected %.0f +- 1 s" % (
        times[-1], TOT_S)
    assert last and abs(last[0] - TOT_S) <= 1.0, "no terminator (last frame) at the TOT"
