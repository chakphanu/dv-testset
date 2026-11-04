# SPDX-License-Identifier: MIT
"""Cross-mode audio into YSF through the REAL transcoder (T14 uses a fake AMBEd and cannot tell): a DMR, D-STAR or M17 talker
keys a 3 s 700 Hz tone, encoded by transcoder-ref into the talker's own codec, on a module a YSF listener is linked to.

  frames   the YSF listener's voice frames of the over
  bytes    the YSF egress changes only the gain index b2 (-0.4 dB, xlxd parity; dv-router ruling 2026-11-04): DMR -> YSF is the same
           codec, so every frame equals the sent one but for b2 = M04[b2]; D-STAR and M17 are compared the same way with
           transcoder-ref's output for the same input (a difference is recorded, the transcoders need not agree bit for bit)
  audio    the listener's frames decoded by transcoder-ref: the dominant frequency is the tone (+- 80 Hz) and the tone holds
           most of the power
  level    the decoded level is at least half of the expected frames' (M17: within 3 dB of the same over through the reference
           reflector xlxd-ref, same real transcoder; a steady tone repeats one frame: that is no garbling)

Skips when the transcoder of the rig is the fake (DVT_TRANSCODER_FAKE=1) or transcoder-ref is not given. Results of a run:
out/<run>/crossmode-real-ysf.txt.
"""
import math
import os
import time
from pathlib import Path

import pytest

from dvtest import clients, crossaudio, echo
from dvtest.audio import signals
from dvtest.proto import ambed, nxdn
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


def hear(ip, src, sent):
    """A `src` talker keys `sent` on MODULE of the reflector at ip; the frames of the YSF listener's first over."""
    talker, listener = clients.make(src, MODULE, 0), clients.make("ysf", MODULE, 1)
    drain = None
    try:
        for c in (talker, listener):
            assert c.link(ip), "%s client did not link to %s" % (c.__class__.__name__, ip)
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
        time.sleep(1.0)
    return crossaudio.heard_frames(evs, t0 - 1)


@pytest.mark.parametrize("src", ["dmr", "dstar", "m17"])
def test_cross_mode_into_ysf_is_not_garbled(real_transcoder, tone_pcm, src):
    rig = real_transcoder
    sent = transcode(rig, ambed.PCM, CODEC[src], tone_pcm)[:FRAMES]
    ambe2 = sent if src == "dmr" else transcode(rig, CODEC[src], ambed.AMBE2PLUS, sent)
    # the YSF egress steps the gain index b2 by -0.4 dB (xlxd parity, dv-router ruling 2026-11-04): the expected frames carry it
    expected = crossaudio.expected_ysf([nxdn.to_dmr(nxdn.set_gain_index(nxdn.from_dmr(f), crossaudio.M04[nxdn.gain_index(nxdn.from_dmr(f))]))
                                        for f in ambe2])
    got = hear(rig.xlxd_sut, src, sent)
    top, count = crossaudio.repetition(got)
    first = lambda fr: [f.hex() for f in fr[:3]]  # noqa: E731
    equal = sum(1 for a, b in zip(got, expected) if a == b)
    note(rig, "%s -> ysf: sent %d frames, expected %d, heard %d; %d equal to the expected (with the egress b2 step); most repeated heard "
         "frame %s x%d (expected: %s x%d); first sent %s, first expected %s, first heard %s" % (
             src, len(sent), len(expected), len(got), equal, top.hex() if top else "-", count,
             crossaudio.repetition(expected)[0].hex(), crossaudio.repetition(expected)[1], first(sent), first(expected), first(got)))
    assert len(got) >= len(expected) - 5, "%s -> ysf: the YSF listener heard %d of %d frames" % (src, len(got), len(expected))
    pcm = b"".join(transcode(rig, ambed.AMBE2PLUS, ambed.PCM, got))
    ref = b"".join(transcode(rig, ambed.AMBE2PLUS, ambed.PCM, expected))
    heard, want = crossaudio.tone_report(pcm, FREQ), crossaudio.tone_report(ref, FREQ)
    note(rig, "%s -> ysf: decoded audio dominant %s Hz share %.2f rms %.0f; the expected frames decode to %s Hz share %.2f rms %.0f" % (
        src, heard["dominant"], heard["share"], heard["rms"], want["dominant"], want["share"], want["rms"]))
    chk = crossaudio.gain_check(ambe2, got, ("m04",))
    note(rig, "%s -> ysf: gain check of %d frames against the %s output (all bits but b2 equal, b2 = M04[b2]): %d bad %s" % (
        src, chk["checked"], "sent" if src == "dmr" else "transcoder-ref", len(chk["bad"]), chk["bad"][:3]))
    problems = []
    if heard["dominant"] is None or abs(heard["dominant"] - FREQ) > 80:
        problems.append("the decoded audio peaks at %s Hz, the tone is %d Hz" % (heard["dominant"], FREQ))
    if heard["share"] < 0.5 * want["share"]:
        problems.append("tone share %.2f against %.2f of the expected frames" % (heard["share"], want["share"]))
    if chk["checked"] != len(ambe2) or chk["bad"]:
        if src == "dmr":
            problems.append("%d of %d frames differ from the sent ones by more than the egress b2 step (index, b2 sent, heard, wanted, "
                            "other bits equal): %s" % (len(chk["bad"]), chk["checked"], chk["bad"][:3]))
        else:
            note(rig, "%s -> ysf: FINDING %d of %d frames differ from transcoder-ref's output by more than the egress step (the SUT's "
                 "transcoder need not agree bit for bit): %s" % (src, len(chk["bad"]), chk["checked"], chk["bad"][:3]))
    if src == "m17":
        # the level is judged against the reference reflector (xlxd-ref, the same real transcoder): is DV-Router quieter than xlxd?
        ref_got = hear(rig.xlxd_ref, src, sent)
        ref_pcm = b"".join(transcode(rig, ambed.AMBE2PLUS, ambed.PCM, ref_got)) if ref_got else b""
        ref_rep = crossaudio.tone_report(ref_pcm, FREQ) if ref_pcm else {"dominant": None, "share": 0.0, "rms": 0.0}
        note(rig, "m17 -> ysf: LEVEL sut rms %.0f, reference reflector rms %.0f (%d frames), transcoder-ref direct %.0f" % (
            heard["rms"], ref_rep["rms"], len(ref_got), want["rms"]))
        if not ref_got:
            pytest.skip("the reference reflector delivered no M17 -> YSF audio: no level to compare with")
        if heard["rms"] < 0.7 * ref_rep["rms"]:
            problems.append("level %.0f rms against %.0f of the reference reflector (%.1f dB down)" % (
                heard["rms"], ref_rep["rms"], 20 * math.log10(max(ref_rep["rms"], 1) / max(heard["rms"], 1))))
    elif heard["rms"] < 0.5 * want["rms"]:
        problems.append("level %.0f rms against %.0f of the expected frames" % (heard["rms"], want["rms"]))
    assert not problems, "%s -> ysf: %s" % (src, "; ".join(problems))
