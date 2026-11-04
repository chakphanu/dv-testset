# SPDX-License-Identifier: MIT
"""Cross-mode audio into YSF through the REAL transcoder (T14 uses a fake AMBEd and cannot tell): a DMR, D-STAR or M17 talker
keys a 3 s 700 Hz tone, encoded by transcoder-ref into the talker's own codec, on a module a YSF listener is linked to.

  frames   the YSF listener's voice frames of the over
  bytes    DMR -> YSF is the same codec (AMBE+2): the frames must equal the sent ones through the VCH coder once. D-STAR and
           M17 go through the SUT's transcoder: the frames are compared with transcoder-ref's output for the same input
           (recorded, the transcoders are not required to agree bit for bit)
  audio    the listener's frames decoded by transcoder-ref: the dominant frequency is the tone (+- 80 Hz) and the tone holds
           most of the power
  level    the decoded level is at least half of the expected frames' (a steady tone repeats one frame: that is no garbling)

Skips when the transcoder of the rig is the fake (DVT_TRANSCODER_FAKE=1) or transcoder-ref is not given. Results of a run:
out/<run>/crossmode-real-ysf.txt.
"""
import os
import time
from pathlib import Path

import pytest

from dvtest import clients, crossaudio, echo
from dvtest.audio import signals
from dvtest.proto import ambed
from echo_support import Drainer

pytestmark = pytest.mark.rig

MODULE = "H"
FREQ = 700
FRAMES = 150
CODEC = {"dmr": ambed.AMBE2PLUS, "dstar": ambed.AMBEPLUS, "m17": ambed.CODEC2_3200}


def note(target, text):
    line = "[RECORD] " + text
    print(line)
    try:
        with open(Path(target.out) / "crossmode-real-ysf.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


@pytest.fixture(scope="module")
def real_transcoder(rig):
    if os.environ.get("DVT_TRANSCODER_FAKE") == "1":
        pytest.skip("the rig's transcoder is the fake (DVT_TRANSCODER_FAKE=1): this case needs the real one")
    if not rig.is_given("ambed_ref"):
        pytest.skip("no transcoder-ref (DVT_AMBED_REF)")
    return rig


def transcode(rig, codec_in, codec_out, frames):
    return ambed.transcode(rig.ambed_ref, codec_in, [codec_out], frames, port=rig.ambed_port)[codec_out]


@pytest.fixture(scope="module")
def tone_pcm():
    return signals.frames(signals.tone(FREQ, FRAMES * 20))


@pytest.mark.parametrize("src", ["dmr", "dstar", "m17"])
def test_cross_mode_into_ysf_is_not_garbled(real_transcoder, tone_pcm, src):
    rig = real_transcoder
    sent = transcode(rig, ambed.PCM, CODEC[src], tone_pcm)[:FRAMES]
    expected = crossaudio.expected_ysf(sent if src == "dmr" else transcode(rig, CODEC[src], ambed.AMBE2PLUS, sent))
    talker, listener = clients.make(src, MODULE, 0), clients.make("ysf", MODULE, 1)
    drain = None
    try:
        for c in (talker, listener):
            assert c.link(rig.xlxd_sut), "%s client did not link" % c.__class__.__name__
        listener.client.drain()
        listener.client.inbox.clear()
        listener.client.inbox_times.clear()
        drain = Drainer(listener.client)
        drain.start()
        t0 = time.monotonic()
        clients.talk(talker, src, sent, tail=0.0)
        time.sleep(2.0)
        evs = echo.events("ysf", list(listener.client.inbox), list(listener.client.inbox_times))
    finally:
        if drain is not None:
            drain.stop()
        clients.close(talker, listener)
    got = crossaudio.heard_frames(evs, t0 - 1)
    top, count = crossaudio.repetition(got)
    first = lambda fr: [f.hex() for f in fr[:3]]  # noqa: E731
    equal = sum(1 for a, b in zip(got, expected) if a == b)
    same_sent = sum(1 for a, b in zip(got, sent) if a == b)
    note(rig, "%s -> ysf: sent %d frames, expected %d, heard %d; %d equal to the expected and %d equal to the SENT frames at the same position; "
         "most repeated heard frame %s x%d (expected: %s x%d); first sent %s, first expected %s, first heard %s" % (
             src, len(sent), len(expected), len(got), equal, same_sent, top.hex() if top else "-", count,
             crossaudio.repetition(expected)[0].hex(), crossaudio.repetition(expected)[1], first(sent), first(expected), first(got)))
    assert len(got) >= len(expected) - 5, "%s -> ysf: the YSF listener heard %d of %d frames" % (src, len(got), len(expected))
    pcm = b"".join(transcode(rig, ambed.AMBE2PLUS, ambed.PCM, got))
    ref = b"".join(transcode(rig, ambed.AMBE2PLUS, ambed.PCM, expected))
    heard, want = crossaudio.tone_report(pcm, FREQ), crossaudio.tone_report(ref, FREQ)
    note(rig, "%s -> ysf: decoded audio dominant %s Hz share %.2f rms %.0f; the expected frames decode to %s Hz share %.2f rms %.0f" % (
        src, heard["dominant"], heard["share"], heard["rms"], want["dominant"], want["share"], want["rms"]))
    assert heard["dominant"] is not None and abs(heard["dominant"] - FREQ) <= 80, \
        "%s -> ysf: the decoded audio peaks at %s Hz, the tone is %d Hz" % (src, heard["dominant"], FREQ)
    assert heard["share"] >= 0.5 * want["share"], "%s -> ysf: tone share %.2f against %.2f of the expected frames" % (
        src, heard["share"], want["share"])
    assert heard["rms"] >= 0.5 * want["rms"], "%s -> ysf: level %.0f rms against %.0f of the expected frames (%.1f dB down)" % (
        src, heard["rms"], want["rms"], 20 * __import__("math").log10(max(want["rms"], 1) / max(heard["rms"], 1)))
    if src == "dmr":
        assert got[:len(expected)] == expected, "dmr -> ysf: same codec, but the heard frames differ from the sent ones (%d of %d equal)" % (
            equal, len(expected))
