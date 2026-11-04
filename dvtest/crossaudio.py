# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_crossmode_real_ysf.py: what a YSF listener should hear of an over that came in
through another protocol, and whether the decoded audio still carries the tone.

  expected_ysf(frames)       AMBE+2 (DMR form) frames as a YSF listener gets them: through the VCH coder once
  heard_frames(events, t)    the voice frames of the first over after time t in a list of echo.Ev
  tone_report(pcm, freq)     dominant frequency, the share of spectrum power within 60 Hz of freq and the rms of a PCM byte string
  repetition(frames)         (most common frame, how often) - a garbled stream repeats one frame
"""
from collections import Counter

from . import echo
from .audio import score
from .proto import ysf


def expected_ysf(frames):
    return [ysf.vch_decode(ysf.vch_encode(f)) for f in frames]


def heard_frames(evs, after=0.0):
    """Frames of the first over (events closer than 0.4 s) that starts after `after`."""
    overs = [o for o in echo.split_overs(evs) if o.start > after and o.n_voice > 0]
    return overs[0].frames if overs else []


def tone_report(pcm, freq):
    spec = score.spectrum(pcm)
    total = sum(spec.values())
    band = sum(v for f, v in spec.items() if abs(f - freq) <= 60)
    return {"dominant": score.dominant(spec), "share": band / total if total else 0.0, "rms": score.rms(pcm)}


def repetition(frames):
    if not frames:
        return (None, 0)
    frame, n = Counter(frames).most_common(1)[0]
    return (frame, n)
