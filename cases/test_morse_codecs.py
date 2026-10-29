# SPDX-License-Identifier: MIT
"""Morse ID text survives lossy codecs: the decoded text must match what
was keyed exactly (20 wpm, 800 Hz).

unit: upstream Codec2 3200 and 1600 round trip (c2enc/c2dec on PATH).
wire: PCM -> AMBE+ / AMBE2+ / Codec2 -> PCM through a transcoder
(DVT_AMBED_ADDR or TRANSCODER_IMAGE); codecs come from its CAPS reply.
rig: dvtest.tools.live_id sends the ID to xlxd-sut, a D-STAR listener on
the same module decodes it.
"""
import random
import shutil
import subprocess
import threading

import pytest

from dvtest.audio import morse, signals
from dvtest.proto import ambed, dextra
from dvtest.tools import live_id

WPM = 20
RIG_MODULE = "E"
WIRE_CODECS = {"ambeplus": ambed.AMBEPLUS, "ambe2plus": ambed.AMBE2PLUS, "codec2_3200": ambed.CODEC2_3200}


def id_text():
    return "VVV DE N0TEST E %04d" % random.SystemRandom().randrange(10000)


def assert_decodes(text, pcm, label):
    result = morse.check(text, morse.decode(pcm))
    print("%s: sent %r got %r" % (label, text, result["got"]))
    assert result["exact"], result


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["3200", "1600"])
def test_codec2_round_trip(mode):
    if not (shutil.which("c2enc") and shutil.which("c2dec")):
        pytest.skip("c2enc/c2dec not on PATH (built in the rig tester from scripts/pre-fetch-codec2.sh)")
    text = id_text()
    bits = subprocess.run(["c2enc", mode, "-", "-"], input=morse.encode(text, wpm=WPM),
                          capture_output=True, check=True).stdout
    pcm = subprocess.run(["c2dec", mode, "-", "-"], input=bits, capture_output=True, check=True).stdout
    assert_decodes(text, pcm, "codec2 %s" % mode)


@pytest.fixture(scope="module")
def wire(ambed_addr):
    """(addr, port, caps, text, {codec: frames}) with the ID encoded once
    into every served codec of WIRE_CODECS."""
    client = ambed.AmbedClient(*ambed_addr)
    try:
        caps = ambed.parse_caps(client.rpc(ambed.CAPS_REQUEST) or b"")
    finally:
        client.close()
    assert caps is not None, "no CAPS reply"
    if not caps.mask & (1 << ambed.PCM):
        pytest.skip("transcoder does not announce PCM (mask 0x%02x)" % caps.mask)
    outs = [c for c in WIRE_CODECS.values() if caps.mask & (1 << c)]
    text = id_text()
    frames = signals.frames(morse.encode(text, wpm=WPM))
    encoded = {}
    for i in range(0, len(outs), caps.max_outputs):
        encoded.update(ambed.transcode(ambed_addr[0], ambed.PCM, outs[i:i + caps.max_outputs], frames,
                                       port=ambed_addr[1], transaction=0x4D4F0000 + i))
    return ambed_addr, caps, text, encoded


@pytest.mark.wire
@pytest.mark.parametrize("name", list(WIRE_CODECS))
def test_morse_through_transcoder(wire, name):
    (addr, port), caps, text, encoded = wire
    codec = WIRE_CODECS[name]
    if codec not in encoded:
        pytest.skip("transcoder does not announce %s (mask 0x%02x)" % (name, caps.mask))
    pcm = ambed.transcode(addr, codec, [ambed.PCM], encoded[codec], port=port,
                          transaction=0x4D4F0100 + codec)[ambed.PCM]
    assert_decodes(text, b"".join(pcm), "PCM -> %s -> PCM" % name)


@pytest.mark.rig
def test_live_id_through_reflector(rig):
    """live_id (as run against a real reflector) on xlxd-sut module E, a
    second D-STAR client listens and decodes the text."""
    listener = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=RIG_MODULE)
    stop = threading.Event()

    def pump():
        while not stop.is_set():
            listener.client.pump(0.1)

    try:
        assert listener.link(rig.xlxd_sut), "listener link to xlxd-sut failed"
        listener.client.inbox.clear()
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            sent = live_id.run(live_id.parse_args([
                "--reflector-ip", rig.xlxd_sut, "--reflector-name", rig.reflector, "--module", RIG_MODULE,
                "--callsign", "N0TEST", "--transcoder", "%s:%d" % (rig.ambed_ref, rig.ambed_port)]))
        finally:
            stop.set()
            thread.join()
        frames = listener.collect()
    finally:
        if listener.dest:
            listener.unlink()
        listener.close()
    assert len(frames) == sent["frames"], "listener got %d of %d frames" % (len(frames), sent["frames"])
    pcm = ambed.transcode(rig.ambed_ref, ambed.AMBEPLUS, [ambed.PCM], frames, port=rig.ambed_port)[ambed.PCM]
    assert_decodes(sent["text"], b"".join(pcm), "listener on %s module %s" % (rig.reflector, RIG_MODULE))
