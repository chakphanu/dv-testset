# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_crossmode_real_ysf.py: what a YSF listener should hear of an over that came in
through another protocol, and whether the decoded audio still carries the tone.

  expected_ysf(frames)       AMBE+2 (DMR form) frames as a YSF listener gets them: through the VCH coder once
  heard_frames(events, t)    the voice frames of the first over after time t in a list of echo.Ev
  tone_report(pcm, freq)     dominant frequency, the share of spectrum power within 60 Hz of freq and the rms of a PCM byte string
  repetition(frames)         (most common frame, how often) - a garbled stream repeats one frame
  gain_check(sent, heard, steps)
                             YSF egress / ingress change only the AMBE+2 gain index b2 (xlxd parity): every bit but b2
                             equal, b2 mapped by the named steps ("p10": YSF ingress +1.0 dB, "m04": YSF egress -0.4 dB)

The b2 maps are those of DV-Router (crates/lib/radio/src/ambe2.rs, xlxd B2_MAP_PLUS_1_0_DB and the nearest-entry
fallback of AdjustAmbeGain for -0.4 dB); the ruling and the lab frames are in the report of 2026-11-04 (docs/echo-cases.md).
"""
from collections import Counter

from . import echo
from .audio import score
from .proto import nxdn, ysf


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


P10 = (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
       21, 22, 23, 24, 24, 25, 25, 26, 26, 27, 28, 29, 30, 31, 31, 31)
M04 = (0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
       14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 27, 28, 29, 30)
MAPS = {"p10": P10, "m04": M04}


def mapped_gain(b2, steps):
    for step in steps:
        b2 = MAPS[step][b2]
    return b2


def gain_check(sent, heard, steps):
    """Compare 9-byte DMR-form frames pairwise (the shorter list decides): `bad` lists (index, sent b2, heard b2, wanted b2,
    other bits equal) for every pair where an other bit differs or b2 is not the mapped value; `checked` is the number of pairs."""
    bad, n = [], min(len(sent), len(heard))
    for i in range(n):
        a, h = nxdn.from_dmr(sent[i]), nxdn.from_dmr(heard[i])
        want = mapped_gain(nxdn.gain_index(a), steps)
        same = nxdn.without_gain(a) == nxdn.without_gain(h)
        if not same or nxdn.gain_index(h) != want:
            bad.append((i, nxdn.gain_index(a), nxdn.gain_index(h), want, same))
    return {"checked": n, "bad": bad}
