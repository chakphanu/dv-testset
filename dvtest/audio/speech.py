# SPDX-License-Identifier: MIT
"""Speech-quality measures for transcoded 8 kHz speech, stdlib only.

- downsample2(): 16 -> 8 kHz, Blackman-windowed sinc low-pass (127 taps,
  cutoff 3.7 kHz), then every second sample.
- stoi(): Short-Time Objective Intelligibility (Taal et al. 2011). At
  rate=10000 it is the published algorithm (15 one-third octave bands from
  150 Hz, pystoi-compatible); at rate=8000 the same algorithm on 14 bands
  (150 Hz .. 3.4 kHz, the narrowband voice channel) without resampling.
- lsd(): log-spectral distance, same definition as score.lsd but on inputs
  already aligned (FFT instead of the stdlib DFT).
- mcd(): mel-cepstral distortion (24 mel bands 0-4 kHz, c1..c12 of the
  log filterbank energies) over the reference's speech frames.
- delay(): codec delay of a degraded signal against its reference from
  log-energy envelopes (waveform correlation fails on parametric codecs).

The corpus is CMU ARCTIC (fetched by scripts/pre-fetch-speech.sh, never
shipped): `python3 -m dvtest.audio.speech prepare TARBALL SPEAKER OUTDIR`
writes the IDS utterances as 8 kHz S16LE .raw files plus SHA256SUMS.
"""
import cmath
import hashlib
import math
import struct
import sys
import tarfile
import wave
from pathlib import Path

EPS = sys.float_info.epsilon
IDS = ["arctic_a%04d" % i for i in range(1, 21)]
SPEAKERS = ("bdl", "slt")


# ------------------------------------------------------------------ basics

def to_floats(pcm):
    """S16LE bytes -> floats in -1..1."""
    return [v / 32768.0 for v in struct.unpack("<%dh" % (len(pcm) // 2), pcm[:len(pcm) // 2 * 2])]


def to_pcm(x):
    return struct.pack("<%dh" % len(x), *[max(-32768, min(32767, int(round(v * 32768)))) for v in x])


def hann(n):
    """matlab hanning(n): no zero end points."""
    return [0.5 - 0.5 * math.cos(2 * math.pi * k / (n + 1)) for k in range(1, n + 1)]


_PLANS = {}


def _plan(n):
    if n not in _PLANS:
        bits = n.bit_length() - 1
        rev = [int(format(i, "0%db" % bits)[::-1], 2) for i in range(n)]
        _PLANS[n] = (rev, [cmath.exp(-2j * math.pi * k / n) for k in range(n // 2)])
    return _PLANS[n]


def fft(values):
    """Radix-2 FFT of a sequence whose length is a power of two."""
    n = len(values)
    rev, tw = _plan(n)
    a = [complex(values[r]) for r in rev]
    size = 2
    while size <= n:
        half, step = size // 2, n // size
        for start in range(0, n, size):
            for k in range(half):
                u, v = a[start + k], a[start + k + half] * tw[k * step]
                a[start + k], a[start + k + half] = u + v, u - v
        size *= 2
    return a


def power_pair(fx, fy, n):
    """|X|^2 and |Y|^2 (bins 0..n/2) of two real frames with one complex
    FFT of x + iy (frames shorter than n are zero padded)."""
    z = [complex(a, b) for a, b in zip(fx, fy)] + [0j] * (n - len(fx))
    s = fft(z)
    px, py = [], []
    for k in range(n // 2 + 1):
        a, b = s[k], s[-k % n].conjugate()
        px.append(abs(a + b) ** 2 / 4)
        py.append(abs(a - b) ** 2 / 4)
    return px, py


# ---------------------------------------------------------------- resample

def _lowpass(taps=127, cutoff=3700.0, rate=16000.0):
    m = taps // 2
    fc = cutoff / rate
    h = []
    for k in range(-m, m + 1):
        sinc = 2 * fc if k == 0 else math.sin(2 * math.pi * fc * k) / (math.pi * k)
        w = 0.42 + 0.5 * math.cos(math.pi * k / m) + 0.08 * math.cos(2 * math.pi * k / m)
        h.append(sinc * w)
    total = sum(h)
    return [v / total for v in h]


def downsample2(x):
    """16 kHz samples -> 8 kHz samples (zero-phase FIR, edges zero padded)."""
    h = _lowpass()
    m = len(h) // 2
    pad = [0.0] * m
    s = pad + list(x) + pad
    return [h[m] * s[i + m] + sum(h[m + k] * (s[i + m - k] + s[i + m + k]) for k in range(1, m + 1))
            for i in range(0, len(x), 2)]


# -------------------------------------------------------------------- STOI

STOI_FRAME, STOI_NFFT, STOI_N, STOI_BETA, STOI_RANGE = 256, 512, 30, -15.0, 40
STOI_BANDS = {10000: 15, 8000: 14}


def _third_octave(rate, nfft, bands, low=150.0):
    """[(first bin, end bin)] per band, bins snapped as in pystoi."""
    f = [rate * i / nfft for i in range(nfft // 2 + 1)]
    out = []
    for k in range(bands):
        lo = low * 2 ** ((2 * k - 1) / 6)
        hi = low * 2 ** ((2 * k + 1) / 6)
        out.append((min(range(len(f)), key=lambda i: (f[i] - lo) ** 2),
                    min(range(len(f)), key=lambda i: (f[i] - hi) ** 2)))
    return out


def _remove_silent(x, y, frame, hop, dyn_range):
    w = hann(frame)
    starts = range(0, len(x) - frame, hop)
    xf = [[w[i] * x[s + i] for i in range(frame)] for s in starts]
    yf = [[w[i] * y[s + i] for i in range(frame)] for s in starts]
    energy = [20 * math.log10(math.sqrt(sum(v * v for v in f)) + EPS) for f in xf]
    top = max(energy, default=0.0)
    keep = [i for i, e in enumerate(energy) if top - dyn_range - e < 0]

    def ola(frames):
        out = [0.0] * ((len(keep) - 1) * hop + frame if keep else 0)
        for n, i in enumerate(keep):
            for j, v in enumerate(frames[i]):
                out[n * hop + j] += v
        return out
    return ola(xf), ola(yf)


def stoi(clean, degraded, rate=8000):
    """STOI of degraded against clean (equal-length float sequences,
    already time aligned); 1e-5 when under 30 speech frames remain."""
    if rate not in STOI_BANDS:
        raise ValueError("stoi supports rate 8000 or 10000")
    n = min(len(clean), len(degraded))
    x, y = _remove_silent(clean[:n], degraded[:n], STOI_FRAME, STOI_FRAME // 2, STOI_RANGE)
    w = hann(STOI_FRAME)
    bands = _third_octave(rate, STOI_NFFT, STOI_BANDS[rate])
    xt, yt = [[] for _ in bands], [[] for _ in bands]
    for s in range(0, len(x) - STOI_FRAME, STOI_FRAME // 2):
        px, py = power_pair([w[i] * x[s + i] for i in range(STOI_FRAME)],
                            [w[i] * y[s + i] for i in range(STOI_FRAME)], STOI_NFFT)
        for j, (lo, hi) in enumerate(bands):
            xt[j].append(math.sqrt(sum(px[lo:hi])))
            yt[j].append(math.sqrt(sum(py[lo:hi])))
    frames = len(xt[0])
    if frames < STOI_N:
        return 1e-5
    clip = 1 + 10 ** (-STOI_BETA / 20)
    total, count = 0.0, 0
    for m in range(STOI_N, frames + 1):
        for j in range(len(bands)):
            xs, ys = xt[j][m - STOI_N:m], yt[j][m - STOI_N:m]
            gain = math.sqrt(sum(v * v for v in xs)) / (math.sqrt(sum(v * v for v in ys)) + EPS)
            yp = [min(gain * b, clip * a) for a, b in zip(xs, ys)]
            mx, my = sum(xs) / STOI_N, sum(yp) / STOI_N
            xc, yc = [v - mx for v in xs], [v - my for v in yp]
            nx = math.sqrt(sum(v * v for v in xc)) + EPS
            ny = math.sqrt(sum(v * v for v in yc)) + EPS
            total += sum(a * b for a, b in zip(xc, yc)) / (nx * ny)
        count += 1
    return total / (count * len(bands))


# --------------------------------------------------------------- LSD / MCD

def _frames_pair(x, y, size, hop):
    w = [0.5 - 0.5 * math.cos(2 * math.pi * i / size) for i in range(size)]
    n = min(len(x), len(y))
    for s in range(0, n - size + 1, hop):
        yield power_pair([w[i] * x[s + i] for i in range(size)], [w[i] * y[s + i] for i in range(size)], size)


def lsd(ref, deg, rate=8000):
    """Mean log-spectral distance (dB) 0 .. rate/2 of aligned float
    sequences: Hann 256, hop 80, floor 80 dB under the reference peak."""
    pairs = list(_frames_pair(ref, deg, 256, 80))
    if not pairs:
        return float("nan")
    hi = int(4000 / rate * 256) + 1
    floor = max(max(max(p) for p, _q in pairs) * 1e-8, 1e-20)
    total = 0.0
    for pa, pb in pairs:
        d = sum((10 * math.log10(max(pa[f], floor) / max(pb[f], floor))) ** 2 for f in range(1, hi))
        total += math.sqrt(d / (hi - 1))
    return total / len(pairs)


def _mel_bank(rate=8000, size=256, bands=24):
    def mel(f):
        return 2595 * math.log10(1 + f / 700.0)
    edges = [700 * (10 ** ((mel(rate / 2) * i / (bands + 1)) / 2595) - 1) for i in range(bands + 2)]
    bins = [e * size / rate for e in edges]
    bank = []
    for b in range(bands):
        lo, mid, hi = bins[b], bins[b + 1], bins[b + 2]
        bank.append([max(0.0, min((k - lo) / (mid - lo), (hi - k) / (hi - mid))) for k in range(size // 2 + 1)])
    return bank


def _cepstrum(power, bank, order):
    # log amplitude (half the log power), the scale the MCD constant assumes
    logs = [0.5 * math.log(max(sum(w * p for w, p in zip(row, power)), 1e-12)) for row in bank]
    m = len(logs)
    return [sum(v * math.cos(math.pi * q * (i + 0.5) / m) for i, v in enumerate(logs)) * math.sqrt(2.0 / m)
            for q in range(1, order + 1)]


def mcd(ref, deg, rate=8000, order=12, dyn_range=40.0):
    """Mean mel-cepstral distortion (dB) of aligned float sequences over
    frames whose reference energy is within dyn_range dB of its peak."""
    bank = _mel_bank(rate)
    rows = []
    for pa, pb in _frames_pair(ref, deg, 256, 80):
        rows.append((10 * math.log10(sum(pa) + 1e-20), _cepstrum(pa, bank, order), _cepstrum(pb, bank, order)))
    if not rows:
        return float("nan")
    top = max(r[0] for r in rows)
    k = 10 / math.log(10) * math.sqrt(2)
    used = [k * math.sqrt(sum((a - b) ** 2 for a, b in zip(ca, cb))) for e, ca, cb in rows if e > top - dyn_range]
    return sum(used) / len(used)


# ------------------------------------------------------------------- delay

def _envelope(x, hop, win):
    return [math.log(1e-9 + sum(v * v for v in x[s:s + win])) for s in range(0, len(x) - win + 1, hop)]


def _best_lag(a, b, lags):
    best, best_c = 0, -2.0
    for lag in lags:
        u, v = (a, b[lag:]) if lag >= 0 else (a[-lag:], b)
        n = min(len(u), len(v))
        if n < 10:
            continue
        mu, mv = sum(u[:n]) / n, sum(v[:n]) / n
        cov = sum((p - mu) * (q - mv) for p, q in zip(u[:n], v[:n]))
        den = math.sqrt(sum((p - mu) ** 2 for p in u[:n]) * sum((q - mv) ** 2 for q in v[:n]))
        c = cov / den if den else -1.0
        if c > best_c:
            best, best_c = lag, c
    return best


def delay(ref, deg, rate=8000, max_ms=300, span_s=8.0):
    """Samples by which deg lags ref (1 ms resolution), from log-energy
    envelopes of the first span_s seconds: 4 ms steps, then 1 ms."""
    n = int(span_s * rate)
    ms = rate // 1000
    coarse = _best_lag(_envelope(ref[:n], 4 * ms, 8 * ms), _envelope(deg[:n], 4 * ms, 8 * ms),
                       range(-max_ms // 4, max_ms // 4 + 1))
    fine = _best_lag(_envelope(ref[:n], ms, 8 * ms), _envelope(deg[:n], ms, 8 * ms),
                     range(4 * coarse - 4, 4 * coarse + 5))
    return fine * ms


def score_stream(source, output, spans, rate=8000):
    """(delay samples, [(id, stoi, lsd, mcd)]) of a decoded stream (S16LE)
    against its source; spans [(id, start sample, samples)] of the
    utterances in the source. One codec delay for the whole stream."""
    x, y = to_floats(source), to_floats(output)
    lag = delay(x, y, rate)
    rows = []
    for uid, start, count in spans:
        ref = x[start:start + count]
        deg = y[start + lag:start + lag + count] if start + lag >= 0 else []
        deg += [0.0] * (count - len(deg))
        rows.append((uid, stoi(ref, deg, rate), lsd(ref, deg, rate), mcd(ref, deg, rate)))
    return lag, rows


# ------------------------------------------------------------------ corpus

def read_wav16(data):
    """Mono 16-bit wav bytes -> (rate, floats)."""
    import io
    with wave.open(io.BytesIO(data)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit wav")
        return w.getframerate(), to_floats(w.readframes(w.getnframes()))


def prepare(tarball, speaker, outdir):
    """Extract IDS of one speaker from a CMU ARCTIC tarball as 8 kHz S16LE
    raw files and write SHA256SUMS; returns the number of files."""
    out = Path(outdir) / speaker
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    with tarfile.open(tarball, "r:bz2") as tar:
        members = {Path(m.name).stem: m for m in tar.getmembers()
                   if m.isfile() and m.name.endswith(".wav") and "/wav/" in m.name}
        for uid in IDS:
            if uid not in members:
                raise KeyError("%s not in %s" % (uid, tarball))
            rate, x = read_wav16(tar.extractfile(members[uid]).read())
            if rate != 16000:
                raise ValueError("%s is %d Hz, expected 16000" % (uid, rate))
            pcm = to_pcm(downsample2(x))
            (out / (uid + ".raw")).write_bytes(pcm)
            sums.append("%s  %s.raw" % (hashlib.sha256(pcm).hexdigest(), uid))
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return len(sums)


def load(root):
    """{speaker: [(id, pcm8k)]} for every speaker prepared under root;
    empty when the corpus is missing or incomplete."""
    out = {}
    for speaker in SPEAKERS:
        files = [Path(root) / speaker / (uid + ".raw") for uid in IDS]
        if not all(f.is_file() for f in files):
            return {}
        out[speaker] = [(f.stem, f.read_bytes()) for f in files]
    return out


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "prepare":
        print("speech: %s %d utterances" % (sys.argv[3], prepare(*sys.argv[2:])))
    else:
        sys.exit("usage: python3 -m dvtest.audio.speech prepare TARBALL SPEAKER OUTDIR")
