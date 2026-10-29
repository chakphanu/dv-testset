# SPDX-License-Identifier: MIT
"""AllStar (app_rpt) <-> USRP <-> xlxd-sut, end to end through IAX2.

Nodes 1901/1902 are bridged to modules A/B (rig/config/xlxd.usrp.in).
IAX2 audio in reaches M17, D-STAR and DMR listeners together; digital
talk reaches the IAX2 caller with key/unkey; two rooms carry their own
tone; nothing is echoed to the originating USRP peer; killing the node
while keyed releases the module. Tones only, no shipped audio.
"""
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dvtest import clients
from dvtest.net import pace
from dvtest.audio import score, signals
from dvtest.proto import ambed, iax2, m17, usrp

pytestmark = pytest.mark.allstar

NODES = {"A": "1901", "B": "1902"}
TONES = {"A": 700, "B": 1200}
USRP_SOURCE = "N0TEST"                      # source callsign of the USRP rows
RADIO_KEY, RADIO_UNKEY = 12, 13


def dial(target, module, timeout=5):
    caller = iax2.Client(target.allstar, target.iax_secret, username="dvt")
    try:
        caller.call(NODES[module], timeout=timeout)
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
            dial(allstar, "A").close()
            return allstar
        except (TimeoutError, ConnectionError, OSError):
            if time.monotonic() > deadline:
                pytest.fail("AllStar node %s does not answer IAX2 calls" % allstar.allstar)
            time.sleep(3)


def send_keyed(callers, frames_by_caller, drain=()):
    """Key every caller, send its frames at 20 ms, unkey."""
    for caller in callers:
        caller.key()
    start = time.monotonic()
    count = max(len(f) for f in frames_by_caller)
    for index in range(count):
        due = start + index * 0.02
        while time.monotonic() < due:
            for caller in callers:
                caller.pump(0.001)
            for client in drain:
                client.client.drain()
        for caller, frames in zip(callers, frames_by_caller):
            caller.voice(frames[index] if index < len(frames) else bytes(320))
    for caller in callers:
        caller.pump(0.05)
        caller.unkey()


def keyed_source(freq):
    # keyed silence lets the channel open and keeps the tail
    return [bytes(320)] * 20 + signals.frames(signals.chirp(300, 3000, 1000) + signals.tone(freq, 1000)) \
        + [bytes(320)] * 10


def tone_check(pcm, freq, other):
    spec = score.spectrum(pcm)
    own, rival = score.band_power(spec, freq), score.band_power(spec, other)
    return score.dominant(spec), own / max(rival, 1.0)


def loud(frames, level=200):
    return sum(1 for f in frames if score.rms(f) > level)


def test_iax_to_all_digital_listeners(node):
    listeners = {k: clients.make(k, "A", 1) for k in clients.KINDS}
    caller = None
    try:
        for kind, client in listeners.items():
            assert client.link(node.xlxd_sut), kind + " link failed"
        caller = dial(node, "A")
        caller.media.clear()
        frames = keyed_source(700)
        send_keyed([caller], [frames], drain=listeners.values())
        caller.pump(1.0)
        rows = {}
        for kind, client in listeners.items():
            got = client.collect(settle=0.3)
            rows[kind] = (len(got), len(set(got)))
        echoed = loud(caller.media)
    finally:
        if caller:
            caller.close()
        clients.close(*listeners.values())
    for kind, (count, distinct) in rows.items():
        assert count >= len(frames), "%s got %d of %d frames" % (kind, count, len(frames))
        assert distinct > 10, "%s: only %d distinct frames" % (kind, distinct)
    assert echoed == 0, "%d loud frames came back to the talking IAX2 caller" % echoed


@pytest.mark.parametrize("kind", clients.KINDS)
def test_digital_to_iax(node, kind):
    pcm = signals.frames(signals.tone(700, 2000))
    frames = ambed.transcode(node.ambed_ref, ambed.PCM, [clients.CODEC[kind]], pcm,
                             port=node.ambed_port)[clients.CODEC[kind]]
    talker = clients.make(kind, "A", 0)
    caller = None
    try:
        assert talker.link(node.xlxd_sut)
        caller = dial(node, "A")
        caller.media.clear()
        caller.controls.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(clients.talk, talker, kind, frames)
            while not job.done():
                caller.pump(0.05)
            job.result()
        caller.pump(1.0)
        media, controls = list(caller.media), list(caller.controls)
    finally:
        if caller:
            caller.close()
        clients.close(talker)
    assert len(media) >= len(frames), "IAX2 caller got %d of %d frames" % (len(media), len(frames))
    voiced = b"".join(f for f in media if score.rms(f) > 200)
    peak, ratio = tone_check(voiced, 700, 1200)
    assert abs(peak - 700) < 80 and ratio > 3, "returned audio peak %s Hz, 700/1200 ratio %.1f" % (peak, ratio)
    assert RADIO_KEY in controls and RADIO_UNKEY in controls[controls.index(RADIO_KEY):], \
        "no key then unkey toward the IAX2 caller: %s" % controls


def test_rooms_no_crosstalk(node, tmp_path):
    c2dec = shutil.which("c2dec")
    if not c2dec:
        pytest.skip("c2dec not installed (tester image builds it from .cache/codec2)")
    listeners = {m: clients.make("m17", m, 1) for m in NODES}
    callers = []
    try:
        for client in listeners.values():
            assert client.link(node.xlxd_sut)
        callers = [dial(node, m) for m in NODES]
        frames = {m: [bytes(320)] * 20 + signals.frames(signals.tone(TONES[m], 2400)) + [bytes(320)] * 10
                  for m in NODES}
        send_keyed(callers, [frames[m] for m in NODES], drain=listeners.values())
        for caller in callers:
            caller.pump(1.0)
        got = {m: (c.collect(settle=0.3), list(c.client.inbox)) for m, c in listeners.items()}
    finally:
        for caller in callers:
            caller.close()
        clients.close(*listeners.values())
    for module, (codec2, packets) in got.items():
        assert len(codec2) >= len(frames[module]), "module %s: %d frames" % (module, len(codec2))
        sources = {m17.parse(p)["src"] for p in packets if m17.parse(p)}
        assert sources == {USRP_SOURCE}, "module %s sources %s" % (module, sources)
        encoded = tmp_path / ("room-%s.c2" % module)
        decoded = tmp_path / ("room-%s.raw" % module)
        encoded.write_bytes(b"".join(codec2))
        subprocess.run([c2dec, "3200", str(encoded), str(decoded)], check=True, capture_output=True)
        pcm = decoded.read_bytes()
        other = TONES["B" if module == "A" else "A"]
        peak, ratio = tone_check(pcm[len(pcm) // 4:len(pcm) * 3 // 4], TONES[module], other)
        assert abs(peak - TONES[module]) < 80 and ratio > 3, \
            "module %s: peak %s Hz, own/other %.1f" % (module, peak, ratio)


@pytest.mark.rig
def test_usrp_no_echo(rig_usrp):
    """Tester as the USRP peer of module C: its own stream reaches D-STAR
    but nothing comes back to it; a D-STAR stream does come back."""
    listener = clients.make("dstar", "C", 1)
    talker = clients.make("dstar", "C", 0)
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", 32003))
    peer.settimeout(0.01)
    dest = (rig_usrp.xlxd_sut, 34003)

    def drain():
        out = []
        while True:
            try:
                out.append(peer.recv(2048))
            except socket.timeout:
                return out
    try:
        assert listener.link(rig_usrp.xlxd_sut) and talker.link(rig_usrp.xlxd_sut)
        listener.client.drain()
        listener.client.inbox.clear()
        echo = []
        start = time.monotonic()
        for seq, frame in enumerate(signals.frames(signals.tone(700, 1000))):
            pace(start, seq)
            peer.sendto(usrp.voice(seq, frame), dest)
            echo += drain()
        peer.sendto(usrp.unkey(seq + 1), dest)
        heard = listener.collect(settle=1.0)
        echo += drain()
        frames = ambed.transcode(rig_usrp.ambed_ref, ambed.PCM, [ambed.AMBEPLUS],
                                 signals.frames(signals.tone(700, 1000)), port=rig_usrp.ambed_port)[1]
        clients.talk(talker, "dstar", frames, tail=1.0)
        back = drain()
    finally:
        peer.close()
        clients.close(listener, talker)
    assert len(heard) >= 45, "D-STAR listener heard %d of 50 USRP frames" % len(heard)
    assert not echo, "%d packets echoed to the originating USRP peer" % len(echo)
    keyed = [p for p in back if usrp.is_voice(p) and len(p) == usrp.VOICE_SIZE]
    assert len(keyed) >= 40 and usrp.parse(back[-1]).keyup == 0, \
        "D-STAR -> USRP: %d keyed frames, last %r" % (len(keyed), back[-1:][:1])


def test_node_crash_releases_module(node, rig_control):
    """SIGKILL the AllStar container while keyed: the M17 listener gets an
    EOT and another source can take the module."""
    listener = clients.make("m17", "A", 1)
    talker = clients.make("dstar", "A", 0)
    caller = None
    killed = False
    try:
        assert listener.link(node.xlxd_sut) and talker.link(node.xlxd_sut)
        caller = dial(node, "A")
        caller.key()
        start = time.monotonic()
        for index, frame in enumerate(signals.frames(signals.tone(700, 1000))):
            caller.pump(max(0, start + index * 0.02 - time.monotonic()))
            caller.voice(frame)
            listener.client.drain()
        voices = [m17.parse(p) for p in listener.client.inbox if m17.parse(p)]
        assert voices and not any(v["eot"] for v in voices), "no open M17 stream before the fault"
        listener.client.inbox.clear()
        listener.client.inbox_times.clear()
        killed_at = time.monotonic()
        rig_control("kill", "allstar")
        killed = True
        eot_at = None
        while eot_at is None and time.monotonic() < killed_at + 5:
            listener.client.drain()
            for data, stamp in zip(listener.client.inbox, listener.client.inbox_times):
                parsed = m17.parse(data)
                if parsed and parsed["eot"]:
                    eot_at = stamp
                    break
            time.sleep(0.005)
        assert eot_at is not None, "no M17 EOT within 5 s of the node dying"
        # the reflector closes the stream (codec stream CS) up to ~170 ms
        # after the last frame while it drains its jitter buffer; a header
        # inside that window is relayed native-only by design (C11), so the
        # next talker starts after the close: 300 ms + 100 ms USRP rx timeout
        time.sleep(max(0.0, eot_at + 0.4 - time.monotonic()))
        listener.client.inbox.clear()
        frames = signals.prng_frames("dstar", 30, seed="crash")
        clients.talk(talker, "dstar", frames, tail=1.0)
        taken = listener.collect(settle=0.5)
    finally:
        if caller:
            caller.socket.close()
        clients.close(listener, talker)
        if killed:
            rig_control("start", "allstar")
    assert len(taken) >= 20, "module A still held after the node died (%d frames)" % len(taken)
    print("EOT %.0f ms after the kill request" % ((eot_at - killed_at) * 1000))
