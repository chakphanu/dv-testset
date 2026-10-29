# SPDX-License-Identifier: MIT
"""Simple PCM scoring: peak/RMS, Goertzel tone power, frame log-energy
envelope correlation and LSD 0-4 kHz (cross-correlation alignment +/-1600
samples, Hann 256 hop 80, common floor). stdlib DFT by default; numpy is
used for the spectra when it happens to be installed."""
import math
import struct

try:
    import numpy as _np
except ImportError:
    _np = None

RATE = 8000


def samples(pcm):
    """S16LE bytes -> list of ints."""
    return list(struct.unpack("<%dh" % (len(pcm) // 2), pcm[:len(pcm) // 2 * 2]))


def peak(pcm):
    s = samples(pcm)
    return max((abs(x) for x in s), default=0)


def rms(pcm):
    s = samples(pcm)
    return math.sqrt(sum(x * x for x in s) / len(s)) if s else 0.0


def goertzel(pcm, freq, rate=RATE):
    """Normalised power of one frequency (amp^2 / 4 for a pure sine)."""
    s = samples(pcm)
    if not s:
        return 0.0
    k = 2 * math.cos(2 * math.pi * freq / rate)
    a = b = 0.0
    for x in s:
        a, b = x + k * a - b, a
    power = a * a + b * b - k * a * b
    return power / (len(s) * len(s))


def tone_ratio(pcm, freq, rate=RATE):
    """Share of the signal power at freq (0..1, ~1 for a clean tone)."""
    total = rms(pcm) ** 2
    return min(1.0, 2 * goertzel(pcm, freq, rate) / total) if total else 0.0


def frame_energy(pcm, size=320):
    return [math.log10(1 + rms(pcm[i:i + size]) ** 2) for i in range(0, len(pcm) - size + 1, size)]


def correlation(a, b):
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return cov / den if den else 0.0


def envelope_correlation(ref, cand, max_lag_frames=4):
    """Best log-energy envelope correlation over small frame lags (codec
    delay), returns (corr, lag_frames)."""
    ea, eb = frame_energy(ref), frame_energy(cand)
    best = (-1.0, 0)
    for lag in range(-max_lag_frames, max_lag_frames + 1):
        a, b = (ea, eb[lag:]) if lag >= 0 else (ea[-lag:], eb)
        best = max(best, (correlation(a, b), lag))
    return best


def align(ref, x, span=1600, step=4):
    """Lag (samples) of x against ref by cross-correlation; inputs are
    float lists."""
    n = min(len(ref), len(x))
    best, best_v = 0, -1e30
    for lag in range(-span, span + 1, step):
        if lag >= 0:
            u, v = ref[:n - lag], x[lag:n]
        else:
            u, v = ref[-lag:n], x[:n + lag]
        m = len(u)
        if m < 512:
            continue
        val = sum(p * q for p, q in zip(u, v)) / m
        if val > best_v:
            best_v, best = val, lag
    return best


def frame_powers(x, n=256, hop=80):
    """Power spectra (bins 0..n/2) of Hann windowed frames."""
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]
    starts = range(0, len(x) - n + 1, hop)
    if _np is not None:
        arr = _np.asarray(x, dtype=float)
        w = _np.asarray(win)
        return [list(_np.abs(_np.fft.rfft(arr[s:s + n] * w)) ** 2) for s in starts]
    cos = [[math.cos(2 * math.pi * k * b / n) for k in range(n)] for b in range(n // 2 + 1)]
    sin = [[math.sin(2 * math.pi * k * b / n) for k in range(n)] for b in range(n // 2 + 1)]
    out = []
    for s in starts:
        f = [x[s + i] * win[i] for i in range(n)]
        row = []
        for b in range(n // 2 + 1):
            re = sum(p * q for p, q in zip(f, cos[b]))
            im = sum(p * q for p, q in zip(f, sin[b]))
            row.append(re * re + im * im)
        out.append(row)
    return out


def lsd(ref_pcm, cand_pcm):
    """(LSD dB over 0..4 kHz, lag samples) of cand against ref (bytes)."""
    ref = [s / 32768.0 for s in samples(ref_pcm)]
    cand = [s / 32768.0 for s in samples(cand_pcm)]
    lag = align(ref, cand)
    if lag >= 0:
        a, b = ref[:len(ref) - lag], cand[lag:]
    else:
        a, b = ref[-lag:], cand[:len(cand) + lag]
    m = min(len(a), len(b))
    pa, pb = frame_powers(a[:m]), frame_powers(b[:m])
    k = min(len(pa), len(pb))
    if k == 0:
        return float("nan"), lag
    hi = int(4000 / 8000 * 256) + 1
    floor = max(max(max(p) for p in pa) * 1e-8, 1e-20)
    total = 0.0
    for i in range(k):
        d = 0.0
        for f in range(1, hi):
            d += (10 * math.log10(max(pa[i][f], floor) / max(pb[i][f], floor))) ** 2
        total += math.sqrt(d / (hi - 1))
    return total / k, lag
