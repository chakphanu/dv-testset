# SPDX-License-Identifier: MIT
import hashlib

import pytest

from dvtest.audio import score, signals


def test_prng_frames_pinned():
    frames = signals.prng_frames("dstar", 4, seed=1)
    assert [len(f) for f in frames] == [9] * 4
    assert signals.prng_frames("dstar", 4, seed=1) == frames
    assert signals.prng_frames("dstar", 4, seed=2) != frames
    # pinned: these bytes go into baselines/ as hashes, they must never move
    assert signals.prng_bytes(0, 8) == hashlib.sha256(b"0/" + bytes(8)).digest()[:8]
    assert signals.digest(signals.prng_frames("codec2_3200", 100, seed=0)) == \
        hashlib.sha256(signals.prng_bytes("0/codec2_3200", 800)).hexdigest()


@pytest.mark.parametrize("codec,width", [(1, 9), (2, 9), (3, 8), (4, 11), (5, 320)])
def test_prng_frame_widths(codec, width):
    assert {len(f) for f in signals.prng_frames(codec, 10)} == {width}


def test_pcm_generators():
    assert signals.silence(20) == bytes(320)
    t = signals.tone(1000, 100)
    assert len(t) == 1600 and t == signals.tone(1000, 100)
    assert 7900 <= score.peak(t) <= 8000
    assert len(signals.chirp(300, 3000, 40)) == 640
    assert len(signals.dtmf("1#", 100, 100)) == 2 * 3200
    assert len(signals.frames(signals.tone(500, 50))) == 2      # 400 samples -> 2 whole frames
    assert len(signals.bursts(700, 2)) == 2 * 6400


def test_goertzel_and_tone_ratio():
    t = signals.tone(1000, 200, amp=10000)
    assert score.tone_ratio(t, 1000) > 0.95
    assert score.tone_ratio(t, 1500) < 0.05
    d = signals.dtmf("5", 200, 0)
    assert score.goertzel(d, 770) > 10 * score.goertzel(d, 941)
    assert score.goertzel(d, 1336) > 10 * score.goertzel(d, 1209)


def test_envelope_correlation():
    ref = signals.bursts(700, 4)
    delayed = signals.silence(20) + ref[:-320]
    corr, lag = score.envelope_correlation(ref, delayed)
    assert corr > 0.99 and lag == 1
    assert score.envelope_correlation(ref, signals.tone(700, 1600))[0] < 0.5


def test_lsd_identity_and_difference():
    ref = signals.chirp(200, 3000, 120)
    same, lag = score.lsd(ref, ref)
    assert same < 1e-6 and lag == 0
    other, _ = score.lsd(ref, signals.tone(1000, 120))
    assert other > 5


def test_lsd_stdlib_path(monkeypatch):
    """The stdlib DFT gives the numpy result (when numpy is present)."""
    ref = signals.chirp(200, 3000, 60)
    cand = signals.tone(1000, 60)
    first = score.lsd(ref, cand)
    monkeypatch.setattr(score, "_np", None)
    stdlib = score.lsd(ref, cand)
    assert stdlib[1] == first[1] and abs(stdlib[0] - first[0]) < 1e-6
