# SPDX-License-Identifier: MIT
"""dvtest/crossaudio.py: the pure helpers of the real-transcoder cross-mode YSF case."""
import pytest

from dvtest import crossaudio, echo
from dvtest.audio import signals
from dvtest.proto import dextra

pytestmark = pytest.mark.unit


def test_expected_ysf_is_what_the_vch_coder_returns_and_is_stable():
    frames = signals.prng_frames("dmr", 6, seed="x")
    once = crossaudio.expected_ysf(frames)
    assert len(once) == 6 and once != frames
    assert crossaudio.expected_ysf(once) == once          # a second pass changes nothing


def test_heard_frames_picks_the_first_over_after_a_time():
    frames = signals.prng_frames("dstar", 5, seed="y")
    evs = []
    for base, sid in ((10.0, 1), (20.0, 2)):
        pk = [dextra.header(sid, "N0CALL", "H")] + [dextra.voice(sid, i, f, False) for i, f in enumerate(frames)]
        evs += echo.events("dstar", pk, [base + i * 0.02 for i in range(len(pk))])
    assert crossaudio.heard_frames(evs) == frames
    assert crossaudio.heard_frames(evs, 15.0) == frames
    assert crossaudio.heard_frames(evs, 25.0) == []


def test_tone_report_finds_a_tone_and_not_silence():
    tone = b"".join(signals.frames(signals.tone(700, 1000)))
    rep = crossaudio.tone_report(tone, 700)
    assert abs(rep["dominant"] - 700) <= 20 and rep["share"] > 0.8
    flat = b"".join(signals.frames(signals.silence(1000)))
    assert crossaudio.tone_report(flat, 700)["share"] == 0.0


def test_repetition():
    assert crossaudio.repetition([]) == (None, 0)
    assert crossaudio.repetition([b"a", b"b", b"a"]) == (b"a", 2)
