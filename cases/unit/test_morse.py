# SPDX-License-Identifier: MIT
"""Morse encode/decode round trips on clean and degraded PCM."""
import random
import struct

import pytest

from dvtest.audio import morse

pytestmark = pytest.mark.unit

TEXT = "VVV DE N0TEST E 4821"
AMP = 8000


def mapped(pcm, fn):
    s = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
    return struct.pack("<%dh" % len(s), *[max(-32768, min(32767, int(round(fn(i, v))))) for i, v in enumerate(s)])


def decoded(pcm, text=TEXT, **kw):
    result = morse.check(text, morse.decode(pcm, **kw))
    assert result["exact"], result
    return result


@pytest.mark.parametrize("wpm", [15, 20, 25])
@pytest.mark.parametrize("freq", [600, 800, 1000])
def test_round_trip_clean(wpm, freq):
    decoded(morse.encode(TEXT, wpm=wpm, freq=freq))


def test_alphabet_and_prosigns():
    text = "ABCDEFGHIJ KLMNOPQRST UVWXYZ 0123456789 / ? . , = <SK> <SN>"
    decoded(morse.encode(text), text)
    assert morse.decode(morse.encode("<AR> <BT> <KN>")) == "+ = ("


def test_farnsworth_and_given_freq_wpm():
    pcm = morse.encode(TEXT, wpm=20, farnsworth=10)
    decoded(pcm)
    decoded(pcm, freq=800, wpm=20)


def test_white_noise_snr_10db():
    rng = random.Random(1)
    sd = (AMP ** 2 / 2 / 10) ** 0.5
    decoded(mapped(morse.encode(TEXT), lambda i, v: v + rng.gauss(0, sd)))


@pytest.mark.parametrize("seed", range(5))
def test_timing_jitter_20pct(seed):
    rng = random.Random(seed)
    segments = [(down, ms * rng.uniform(0.8, 1.2)) for down, ms in morse.timing(TEXT)]
    decoded(morse.render(segments))


def test_8bit_quantisation():
    decoded(mapped(morse.encode(TEXT), lambda i, v: (int(v) >> 8) << 8))


def test_dc_offset():
    decoded(mapped(morse.encode(TEXT), lambda i, v: v + 6000))


@pytest.mark.parametrize("gain", [0.2, 0.5, 1.0])
def test_gain(gain):
    decoded(mapped(morse.encode(TEXT), lambda i, v: v * gain))


@pytest.mark.parametrize("where", [0.3, 0.5, 0.7])
def test_dropout_40ms_inside_dash(where):
    """40 ms of zeros inside the first dash (the T of TEST) at 20 wpm."""
    lead_ms, dash_ms = 200, 3 * 60
    start = int((lead_ms + where * (dash_ms - 40)) * 8)
    pcm = mapped(morse.encode("TEST 73", lead_ms=lead_ms), lambda i, v: 0 if start <= i < start + 320 else v)
    decoded(pcm, "TEST 73")


def test_empty_and_silence():
    assert morse.decode(b"") == ""
    assert morse.decode(bytes(16000)) == ""
    assert morse.decode(mapped(bytes(16000), lambda i, v: 3000)) == ""


def test_frames_and_unknown_char():
    assert len(morse.encode("E")) % 320 == 0
    with pytest.raises(ValueError):
        morse.encode("#")


def test_check_reports_diff():
    result = morse.check("VVV DE N0TEST", "vvv  de n0tst")
    assert not result["exact"] and result["distance"] == 1
    assert result["diff"] == [("delete", 10, "E", "")]
    assert morse.check("E", " e ")["exact"]
