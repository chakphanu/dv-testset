# SPDX-License-Identifier: MIT
"""Deterministic test inputs: S16LE 8 kHz PCM generators and seeded
pseudo-random codec frames. Nothing here reads files.

PRNG frames come from SHA-256 in counter mode, so the bytes for a given
(seed, codec, n) are identical on every Python version and platform and
can be pinned as hashes in baselines/. PCM uses math.sin and rounds to
int16; it is deterministic on one platform and only used where a tolerance
applies.
"""
import hashlib
import math
import struct

RATE = 8000
FRAME_SAMPLES = 160
FRAME_BYTES = 2 * FRAME_SAMPLES
# payload bytes per 20 ms frame, by AMBEd codec number and by name
FRAME_WIDTH = {1: 9, 2: 9, 3: 8, 4: 11, 5: 320,
               "dstar": 9, "dmr": 9, "codec2_3200": 8, "imbe": 11, "pcm": 320}
DTMF = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
        "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
        "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
        "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633)}


def _clip(value):
    return max(-32768, min(32767, int(round(value))))


def _pack(samples):
    return struct.pack("<%dh" % len(samples), *samples)


def _count(ms):
    return int(RATE * ms / 1000)


def silence(ms):
    return bytes(2 * _count(ms))


def tone(freq, ms, amp=8000, phase=0.0):
    w = 2 * math.pi * freq / RATE
    return _pack([_clip(amp * math.sin(w * i + phase)) for i in range(_count(ms))])


def tones(freqs, ms, amp=8000):
    """Sum of sines, each at amp / len(freqs)."""
    part = amp / len(freqs)
    ws = [2 * math.pi * f / RATE for f in freqs]
    return _pack([_clip(sum(part * math.sin(w * i) for w in ws)) for i in range(_count(ms))])


def chirp(f0, f1, ms, amp=8000):
    """Linear sweep f0 -> f1 Hz."""
    n = _count(ms)
    k = (f1 - f0) / (n / RATE)
    return _pack([_clip(amp * math.sin(2 * math.pi * (f0 * (i / RATE) + 0.5 * k * (i / RATE) ** 2)))
                  for i in range(n)])


def dtmf(digits, on_ms=100, off_ms=100, amp=8000):
    out = bytearray()
    for digit in digits:
        out += tones(DTMF[digit.upper()], on_ms, amp) + silence(off_ms)
    return bytes(out)


def bursts(freq, count, on_ms=200, off_ms=200, amp=8000):
    """Tone bursts separated by silence: a clear energy envelope."""
    out = bytearray()
    for _ in range(count):
        out += tone(freq, on_ms, amp) + silence(off_ms)
    return bytes(out)


def frames(pcm, size=FRAME_BYTES):
    """Split into whole frames (the tail shorter than size is dropped)."""
    return [pcm[i:i + size] for i in range(0, len(pcm) - size + 1, size)]


def prng_bytes(seed, n):
    out = bytearray()
    counter = 0
    seed = str(seed).encode()
    while len(out) < n:
        out += hashlib.sha256(seed + b"/" + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def prng_frames(codec, n, seed=0):
    """n pseudo-random frames of the codec's width (codec number or name)."""
    width = FRAME_WIDTH[codec]
    raw = prng_bytes("%s/%s" % (seed, codec), width * n)
    return [raw[i:i + width] for i in range(0, width * n, width)]


def digest(frames_list):
    """sha256 hex of the concatenated frames (baseline form)."""
    return hashlib.sha256(b"".join(frames_list)).hexdigest()
