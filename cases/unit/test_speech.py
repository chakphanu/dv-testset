# SPDX-License-Identifier: MIT
"""Resampler, STOI, LSD, MCD and delay of dvtest.audio.speech on
synthetic signals (no corpus)."""
import math
import random

import pytest

from dvtest.audio import score, speech

pytestmark = pytest.mark.unit


def sine(freq, n, rate, amp=0.5):
    return [amp * math.sin(2 * math.pi * freq * i / rate) for i in range(n)]


def speechlike(seconds=3.0, rate=8000, seed=0):
    """Harmonic 'vowels' (pitch 110-220 Hz, 2.5 kHz roll-off) gated at a
    syllable rate of ~4 Hz with pauses: enough frames for STOI."""
    rng = random.Random(seed)
    out = []
    while len(out) < seconds * rate:
        f0, n = rng.uniform(110, 220), int(rng.uniform(0.12, 0.25) * rate)
        harmonics = [(k, rng.uniform(0.2, 1.0) / k) for k in range(1, int(3400 / f0))]
        for i in range(n):
            gate = math.sin(math.pi * i / n) ** 2
            out.append(0.1 * gate * sum(a * math.sin(2 * math.pi * k * f0 * i / rate) for k, a in harmonics))
        out += [0.0] * int(rng.uniform(0.03, 0.12) * rate)
    return out[:int(seconds * rate)]


def test_fft_matches_dft():
    x = [math.sin(i * 0.3) + 0.2 * (i % 5) for i in range(16)]
    dft = [sum(v * complex(math.cos(-2 * math.pi * k * i / 16), math.sin(-2 * math.pi * k * i / 16))
               for i, v in enumerate(x)) for k in range(16)]
    assert max(abs(a - b) for a, b in zip(speech.fft(x), dft)) < 1e-9


def test_downsample_keeps_passband_rejects_alias():
    n = 16000
    tone = speech.downsample2(sine(1000, n, 16000))
    assert len(tone) == n // 2
    pcm = speech.to_pcm(tone[200:-200])
    assert abs(score.rms(pcm) / 32768 - 0.5 / math.sqrt(2)) < 0.005
    assert score.tone_ratio(pcm, 1000) > 0.99
    alias = speech.downsample2(sine(5000, n, 16000))
    rms = math.sqrt(sum(v * v for v in alias[200:-200]) / len(alias[200:-200]))
    assert 20 * math.log10(rms / (0.5 / math.sqrt(2))) < -40, "5 kHz leaks into 8 kHz output"


def test_stoi_identity_and_monotonic_in_noise():
    x = speechlike()
    rng = random.Random(1)
    power = sum(v * v for v in x) / len(x)
    assert speech.stoi(x, x) == pytest.approx(1.0, abs=1e-9)
    values = []
    for snr in (20, 5, -5):
        sd = math.sqrt(power / 10 ** (snr / 10))
        values.append(speech.stoi(x, [v + rng.gauss(0, sd) for v in x]))
    assert values[0] > values[1] > values[2], values
    assert speech.stoi(x, [0.3 * v for v in x]) == pytest.approx(1.0, abs=1e-9), "STOI is gain invariant"
    assert speech.stoi(x[:800], x[:800]) == 1e-5, "too short: under 30 frames"
    with pytest.raises(ValueError):
        speech.stoi(x, x, rate=16000)


def test_lsd_matches_score_lsd():
    x = speechlike(1.0)
    rng = random.Random(2)
    y = [v + rng.gauss(0, 0.01) for v in x]
    ours = speech.lsd(speech.to_floats(speech.to_pcm(x)), speech.to_floats(speech.to_pcm(y)))
    theirs, lag = score.lsd(speech.to_pcm(x), speech.to_pcm(y))
    assert lag == 0 and ours == pytest.approx(theirs, rel=1e-6)
    assert speech.lsd(x, x) < 1e-9


def test_mcd_zero_for_identity_grows_with_distortion():
    x = speechlike(2.0)
    rng = random.Random(3)
    assert speech.mcd(x, x) < 1e-9
    assert speech.mcd(x, [0.5 * v for v in x]) < 0.01, "a level change only moves c0 (excluded)"
    # crude 2 kHz band limit: decimate by 4, then hold each sample 4 times
    lowpassed = [v for v in speech.downsample2(speech.downsample2(x)) for _ in range(4)]
    noisy = [v + rng.gauss(0, 0.01) for v in x]
    assert speech.mcd(x, noisy) > 1.0
    assert speech.mcd(x, lowpassed[:len(x)]) > speech.mcd(x, [v + rng.gauss(0, 0.001) for v in x])


@pytest.mark.parametrize("lag", [0, 37, 160, 480])
def test_delay(lag):
    x = speechlike(4.0)
    assert abs(speech.delay(x, [0.0] * lag + x) - lag) <= 8


def test_score_stream_finds_utterances():
    a, b = speechlike(1.5, seed=4), speechlike(1.5, seed=5)
    gap = [0.0] * 2400
    src = gap + a + gap + b + gap
    spans = [("a", 2400, len(a)), ("b", 4800 + len(a), len(b))]
    out = [0.0] * 200 + src
    lag, rows = speech.score_stream(speech.to_pcm(src), speech.to_pcm(out), spans)
    assert abs(lag - 200) <= 8
    assert [r[0] for r in rows] == ["a", "b"] and all(r[1] > 0.95 for r in rows), rows
