# SPDX-License-Identifier: MIT
"""Client life on xlxd-ref and xlxd-sut of the docker rig.

C7: a talker that changes module (unlink, relink elsewhere; DExtra and
DMR) is heard on the new module only.
C8: a talker that stops without a last frame: the reflector ends the
stream by its timeout (reference 1.6 s) and the module is free for the
next talker. Upstream xlxd sends listeners no last frame for a timed-out
stream; that is accepted and only reported.
"""
import time

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import dextra, dmr

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
