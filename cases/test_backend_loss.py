# SPDX-License-Identifier: MIT
"""Transcoder loss on xlxd-sut: with transcoder-sut stopped, xlxd must
not fabricate converted voice, native passthrough stays byte-equal, and
the cross path recovers after the transcoder restarts.

"Fabricated" means a frame with content: like upstream xlxd, the reflector
still relays the stream to other modes with all-zero codec frames (the
source bytes are bypassed, see cpacketstream.cpp), which carry no voice.

The transcoder is stopped/started through the run.sh control channel
(rig_control), which only acts on this project's rig services.
"""
import socket
import time

import pytest

from dvtest import clients
from dvtest.net import pace
from dvtest.audio import signals
from dvtest.proto import usrp

pytestmark = pytest.mark.rig

MODULE = "D"            # no USRP endpoint in any profile
USRP_MODULE = "C"       # tester is the USRP peer (rig/config/xlxd.usrp.in)
USRP_PEER_PORT, USRP_XLXD_PORT = 32003, 34003
FRAMES = 60


def cross_frames(ip, src, dst, frames):
    listener = clients.make(dst, MODULE, 1)
    talker = clients.make(src, MODULE, 0)
    try:
        assert listener.link(ip) and talker.link(ip), "link to %s failed" % ip
        listener.client.drain()
        listener.client.inbox.clear()
        clients.talk(talker, src, frames)
        return listener.collect()
    finally:
        clients.close(talker, listener)


@pytest.fixture(scope="module")
def transcoder_down(rig, rig_control):
    # cross path works before the fault, or the case proves nothing
    before = cross_frames(rig.xlxd_sut, "dstar", "dmr", signals.prng_frames("dstar", FRAMES, seed="loss"))
    assert len(before) >= FRAMES * 0.8, "dstar->dmr on %s before the fault: %d frames" % (MODULE, len(before))
    rig_control("stop", "transcoder-sut")
    state = {"down": True}
    yield state
    if state["down"]:
        rig_control("start", "transcoder-sut")


def test_native_passthrough_while_down(rig, transcoder_down):
    frames = signals.prng_frames("dstar", FRAMES, seed="loss-native")
    talker = clients.make("dstar", MODULE, 0)
    listener = clients.make("dstar", MODULE, 1)
    observer = clients.make("dmr", MODULE, 1)
    try:
        for client in (listener, observer, talker):
            assert client.link(rig.xlxd_sut), "link failed"
        for client in (listener, observer):
            client.client.drain()
            client.client.inbox.clear()
        clients.talk(talker, "dstar", frames)
        received = listener.collect(settle=0.5)
        converted = observer.collect(settle=0.5)
    finally:
        clients.close(talker, listener, observer)
    assert received == frames, "native D-STAR changed while the transcoder is down (%d frames)" % len(received)
    voiced = [f for f in converted if any(f)]
    assert not voiced, "%d of %d DMR frames carry content without a transcoder" % (len(voiced), len(converted))


def test_usrp_pcm_not_fabricated(rig_usrp, transcoder_down):
    listener = clients.make("dstar", USRP_MODULE, 1)
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", USRP_PEER_PORT))
    try:
        assert listener.link(rig_usrp.xlxd_sut)
        listener.client.drain()
        listener.client.inbox.clear()
        dest = (rig_usrp.xlxd_sut, USRP_XLXD_PORT)
        start = time.monotonic()
        for seq, frame in enumerate(signals.frames(signals.tone(700, 1000))):
            pace(start, seq)
            peer.sendto(usrp.voice(seq, frame), dest)
            listener.client.drain()
        peer.sendto(usrp.unkey(seq + 1), dest)
        received = listener.collect(settle=1.0)
    finally:
        peer.close()
        clients.close(listener)
    voiced = [f for f in received if any(f)]
    assert not voiced, "%d of %d D-STAR frames carry content from USRP PCM without a transcoder" % (
        len(voiced), len(received))


def test_cross_path_recovers(rig, rig_control, transcoder_down):
    rig_control("start", "transcoder-sut")
    transcoder_down["down"] = False
    frames = signals.prng_frames("dstar", FRAMES, seed="loss-recover")
    deadline = time.monotonic() + 90
    got = []
    while time.monotonic() < deadline:
        got = cross_frames(rig.xlxd_sut, "dstar", "dmr", frames)
        if len(got) >= FRAMES * 0.8:
            return
        time.sleep(5)
    pytest.fail("dstar->dmr did not recover within 90 s of the restart (%d frames)" % len(got))
