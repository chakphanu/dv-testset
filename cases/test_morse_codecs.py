# SPDX-License-Identifier: MIT
"""Morse ID text survives lossy codecs: the decoded text must match what
was keyed exactly (20 wpm, 800 Hz).

unit: upstream Codec2 3200 and 1600 round trip (c2enc/c2dec on PATH).
wire: PCM -> AMBE+ / AMBE2+ / Codec2 / IMBE -> PCM through a transcoder
(DVT_AMBED_ADDR or TRANSCODER_IMAGE); codecs come from its CAPS reply.
Tandems IMBE <-> AMBE+ / AMBE2+ / Codec2 and AMBE+ / AMBE2+ <-> Codec2
run at tandem.WPM (12 wpm) with a fixed set of texts.
rig: dvtest.tools.live_id sends the ID to xlxd-sut, a D-STAR listener on
the same module decodes it.
allstar: D-STAR talker -> IAX2 caller, and IAX2 caller -> M17 listener
(c2dec), both through node 1901 on module A.
"""
import random
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dvtest.audio import morse, signals, tandem
from dvtest import clients
from dvtest.proto import ambed, dextra, iax2
from dvtest.tools import live_id

WPM = 20
RIG_MODULE = "E"
WIRE_CODECS = {"ambeplus": ambed.AMBEPLUS, "ambe2plus": ambed.AMBE2PLUS, "codec2_3200": ambed.CODEC2_3200,
               "imbe": ambed.IMBE}
TANDEM = [(src, dst) for other in ("ambeplus", "ambe2plus", "codec2_3200")
          for src, dst in (("imbe", other), (other, "imbe"))]
TANDEM += [(src, dst) for other in ("ambeplus", "ambe2plus")
           for src, dst in ((other, "codec2_3200"), ("codec2_3200", other))]
# fixed tandem texts: short, NXDN-style call (a live AMBE2+ -> Codec2 finding), longer QSO line
TANDEM_TEXTS = ["PARIS 4821", "N0TEST NXDN 5836", "CQ CQ DE N0CALL K"]


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


@pytest.fixture(scope="module")
def tandem_wire(wire):
    """encoded(text) -> {codec: frames}: the text at tandem.WPM (12 wpm)
    encoded once into every served codec (two MBE hops close 20 wpm gaps)."""
    (addr, port), caps, _text, served = wire
    cache = {}

    def encoded(text):
        if text not in cache:
            outs, out = list(served), {}
            for i in range(0, len(outs), caps.max_outputs):
                out.update(ambed.transcode(addr, ambed.PCM, outs[i:i + caps.max_outputs], tandem.encode(text),
                                           port=port, transaction=0x4D4F0400 + 0x10000 * len(cache) + i))
            cache[text] = out
        return cache[text]
    return encoded


@pytest.mark.wire
@pytest.mark.parametrize("text", TANDEM_TEXTS)
@pytest.mark.parametrize("src,dst", TANDEM, ids=["%s-%s" % t for t in TANDEM])
def test_morse_tandem(wire, tandem_wire, src, dst, text):
    """PCM -> src (encoded by the transcoder) -> dst -> PCM at tandem.WPM:
    one codec-to-codec hop inside the transcoder."""
    (addr, port), caps, _text, served = wire
    a, b = WIRE_CODECS[src], WIRE_CODECS[dst]
    missing = [n for n in (src, dst) if WIRE_CODECS[n] not in served]
    if missing:
        pytest.skip("transcoder does not announce %s (mask 0x%02x)" % (" ".join(missing), caps.mask))
    tx = 0x4D4F0000 + 0x10000 * TANDEM_TEXTS.index(text) + 8 * a + b
    hop = ambed.transcode(addr, a, [b], tandem_wire(text)[a], port=port, transaction=tx + 0x200)[b]
    pcm = ambed.transcode(addr, b, [ambed.PCM], hop, port=port, transaction=tx + 0x300)[ambed.PCM]
    result = morse.check(text, morse.decode(b"".join(pcm), wpm=tandem.WPM))
    print("PCM -> %s -> %s -> PCM at %d wpm: sent %r got %r" % (src, dst, tandem.WPM, text, result["got"]))
    assert result["exact"], result


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


# ------------------------------------------------------------- AllStar (E5)

ALLSTAR_NODE, ALLSTAR_MODULE = "1901", "A"


def dial(target, timeout=5):
    caller = iax2.Client(target.allstar, target.iax_secret, username="dvt")
    try:
        caller.call(ALLSTAR_NODE, timeout=timeout)
    except Exception:
        caller.socket.close()
        raise
    caller.pump(0.3)
    return caller


@pytest.fixture(scope="module")
def node(allstar):
    """The node answers an IAX2 call (it may still be starting)."""
    deadline = time.monotonic() + 90
    while True:
        try:
            dial(allstar).close()
            return allstar
        except (TimeoutError, ConnectionError, OSError):
            if time.monotonic() > deadline:
                pytest.fail("AllStar node %s does not answer IAX2 calls" % allstar.allstar)
            time.sleep(3)


@pytest.mark.allstar
def test_morse_dstar_to_iax(node):
    """D-STAR talker on module A -> USRP -> node -> IAX2 caller decodes."""
    text = id_text()
    frames = ambed.transcode(node.ambed_ref, ambed.PCM, [ambed.AMBEPLUS], signals.frames(morse.encode(text)),
                             port=node.ambed_port)[ambed.AMBEPLUS]
    talker = clients.make("dstar", ALLSTAR_MODULE, 0)
    caller = None
    try:
        assert talker.link(node.xlxd_sut), "D-STAR talker link failed"
        caller = dial(node)
        caller.media.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(clients.talk, talker, "dstar", frames)
            while not job.done():
                caller.pump(0.05)
            job.result()
        caller.pump(1.0)
        media = list(caller.media)
    finally:
        if caller:
            caller.close()
        clients.close(talker)
    assert_decodes(text, b"".join(media), "D-STAR -> IAX2 node %s" % ALLSTAR_NODE)


@pytest.mark.allstar
def test_morse_iax_to_m17(node):
    """IAX2 caller keys node 1901 -> USRP -> module A -> M17 listener;
    its Codec2 3200 frames decode with c2dec."""
    if not shutil.which("c2dec"):
        pytest.skip("c2dec not installed (tester image builds it from .cache/codec2)")
    text = id_text()
    # keyed silence first lets the channel open, the encoder tail keeps the end
    source = [bytes(320)] * 20 + signals.frames(morse.encode(text))
    listener = clients.make("m17", ALLSTAR_MODULE, 1)
    caller = None
    try:
        assert listener.link(node.xlxd_sut), "M17 listener link failed"
        caller = dial(node)
        caller.key()
        start = time.monotonic()
        for index, frame in enumerate(source):
            while time.monotonic() < start + index * 0.02:
                caller.pump(0.001)
                listener.client.drain()
            caller.voice(frame)
        caller.pump(0.05)
        caller.unkey()
        caller.pump(1.0)
        codec2 = listener.collect(settle=0.5)
    finally:
        if caller:
            caller.close()
        clients.close(listener)
    pcm = subprocess.run(["c2dec", "3200", "-", "-"], input=b"".join(codec2), capture_output=True,
                         check=True).stdout
    assert_decodes(text, pcm, "IAX2 node %s -> M17 module %s" % (ALLSTAR_NODE, ALLSTAR_MODULE))
