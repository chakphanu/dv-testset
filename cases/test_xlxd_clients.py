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
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import dextra, dmr, m17

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


def test_stream_timeout_frees_module(xlxd):
    """C8: header + voice without a last frame, then silence: within the
    stream timeout the module takes the next talker."""
    listener = listen(xlxd, "dstar", "H", 1)
    talker = clients.make("dstar", "H", 0)
    second = dmr.DmrClient("0.0.0.0", dmrid=clients.DMR_IDS[0], module="H")
    try:
        assert talker.link(xlxd) and second.link(xlxd), "talker links failed"
        listener.client.drain()
        listener.client.inbox.clear()
        sid = clients.fresh_stream_id("dstar")
        talker.client.send(talker.dest, dextra.header(sid, talker.callsign, "H"))
        start = time.monotonic()
        for index, frame in enumerate(signals.prng_frames("dstar", FRAMES, seed="c8")):
            pace(start, index)
            talker.client.send(talker.dest, dextra.voice(sid, index, frame, False))
            listener.client.drain()         # arrival times are drain times
        stopped = time.monotonic()
        listener.client.pump(STREAM_TIMEOUT_S + 0.4)
        voice = [(t, dextra.parse_voice(p)) for p, t in zip(listener.client.inbox, listener.client.inbox_times)]
        first = [v for _t, v in voice if v]
        ended = [t - stopped for t, v in voice if v and v[2]]
        listener.client.inbox.clear()
        listener.client.inbox_times.clear()
        clients.talk(second, "dmr", signals.prng_frames("dmr", FRAMES, seed="c8b"), tail=1.0)
        listener.client.pump(0.5)
        after = [v for v in (dextra.parse_voice(p) for p in listener.client.inbox) if v]
    finally:
        clients.close(talker, second, listener)
    assert len(first) >= FRAMES * 0.8, "first stream heard %d of %d frames" % (len(first), FRAMES)
    assert not {v[0] for v in after} & {v[0] for v in first}, "the timed-out stream continued after the timeout"
    assert len(after) >= FRAMES * 0.8, \
        "next talker on H %.1f s after the stop: %d frames heard (module still held?)" % (
            STREAM_TIMEOUT_S + 0.4, len(after))
    print("timed-out stream end marker: %s" % ("last frame %.2f s after the stop" % ended[0]
                                                if ended else "none (upstream behaviour)"))


LATE_MS = 30            # inside the close window (~170 ms after the last frame)
AFTER_MS = 600          # well after the codec stream CS


def late_entry(ip, gap_ms):
    """DMR stream on F, then a D-STAR header gap_ms after its terminator.
    Returns (native D-STAR frames, cross-mode M17 frames) of the second
    stream."""
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
        frames = signals.prng_frames("dstar", 50, seed="c11b")
        voice = [dextra.voice(sid, i, f, False, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA)
                 for i, f in enumerate(frames)] + [dextra.voice(sid, 50, bytes(9), True)]
        second.client.paced(second.dest, voice, 0.020, native.client, cross.client)
        native.client.pump(1.5)
        cross.client.pump(0.5)
        heard = [v[3] for v in map(dextra.parse_voice, native.client.inbox)
                 if v and not v[2] and v[0] not in old_dstar]
        converted = [f for p in map(m17.parse, cross.client.inbox)
                     if p and p["stream_id"] not in old_m17 for f in p["frames"]]
        return heard == frames, len(heard), len([f for f in converted if any(f)])
    finally:
        clients.close(first, second, native, cross)


def test_late_entry_is_native_only(rig):
    """C11: a header that arrives while the previous stream on the module
    is still closing (upstream CReflector stashes it and promotes it
    native-only after the close, creflector.cpp ~556-580 and ~660-745;
    the close is the codec-stream CS ~170 ms after the last frame, after
    the jitter buffer drains) is relayed to same-mode listeners but gets no
    transcoder stream, so cross-mode listeners hear nothing; a header after
    the close is transcoded. Asserted as the reference behaviour, xlxd-sut
    must match xlxd-ref. DV-Router may declare a divergence (transcoding a
    late entry) through a per-case whitelist."""
    got = {}

    def side(name, ip):
        try:
            got[name] = {"late": late_entry(ip, LATE_MS), "after": late_entry(ip, AFTER_MS)}
        except Exception as exc:                    # reported below
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n, getattr(rig, "xlxd_" + n))) for n in ("ref", "sut")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    print("late entry (native exact, native frames, voiced cross frames):", got)
    ref = got["ref"]
    assert ref["late"][0] and ref["after"][0], "ref: native relay not exact %s" % ref
    assert ref["late"][2] == 0, "ref: late entry was transcoded (%d voiced M17 frames)" % ref["late"][2]
    assert ref["after"][2] >= 40, "ref: entry after the close not transcoded (%d)" % ref["after"][2]
    for when in ("late", "after"):
        assert (got["sut"][when][0], got["sut"][when][2] > 0) == (ref[when][0], ref[when][2] > 0), \
            "%s entry: sut %s, ref %s" % (when, got["sut"][when], ref[when])
