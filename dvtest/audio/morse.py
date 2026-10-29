# SPDX-License-Identifier: MIT
"""Morse code (keyed CW tone) as a content check that survives lossy voice
codecs: transcoded audio never matches byte for byte, but on/off keying of
one tone does, and decoding it back to text proves the stream arrived
complete (no lost frames, no truncated tail, no crosstalk).

encode(): S16LE PCM, PARIS timing (dot = 1200 / wpm ms), optional
Farnsworth spacing, 5 ms raised-cosine edges, ITU alphabet plus prosigns
written as <SK>, <AR>, <BT>, <KN>, <SN>.

decode(): tone frequency by Goertzel scan (400-2000 Hz) unless given,
tone envelope per 5 ms block, adaptive two-level threshold with
hysteresis, dot length and codec smear (marks longer, gaps shorter by the
same amount) estimated from the data. Unknown symbols come back as '?'.
"""
import difflib
import math
import struct

RATE = 8000
EDGE_MS = 5.0
BLOCK_MS = 5.0
# an inner gap below DROPOUT_GAP x the element gap may be a dropout in a
# mark; closing it costs DROPOUT_COST (log-timing units, see _fit)
DROPOUT_GAP = 0.7
DROPOUT_COST = 0.7
# key-down level as a fraction from the silence to the tone dB mean
LEVEL = 0.5

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "(": "-.--.", ")": "-.--.-", ":": "---...",
    "'": ".----.", '"': ".-..-.", "@": ".--.-.",
}
# prosigns as encoder input; <AR> <BT> <KN> decode as + = (
PROSIGNS = {"<AR>": ".-.-.", "<BT>": "-...-", "<KN>": "-.--.", "<SK>": "...-.-", "<SN>": "...-."}
DECODE = {code: char for char, code in CODE.items()}
for _name, _code in PROSIGNS.items():
    DECODE.setdefault(_code, _name)


# ------------------------------------------------------------------ encode

def _tokens(text):
    """Words of the text as lists of element strings ('.-')."""
    words, word, i = [], [], 0
    text = text.upper()
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            if word:
                words.append(word)
                word = []
            i += 1
            continue
        if ch == "<":
            end = text.find(">", i)
            name = text[i:end + 1] if end > 0 else ""
            if name in PROSIGNS:
                word.append(PROSIGNS[name])
                i = end + 1
                continue
        if ch not in CODE:
            raise ValueError("no Morse code for %r" % ch)
        word.append(CODE[ch])
        i += 1
    if word:
        words.append(word)
    return words


def timing(text, wpm=20, farnsworth=None):
    """[(key_down, ms)] for text: dot 1 unit, dash 3, element gap 1, letter
    gap 3, word gap 7 (unit = 1200 / wpm ms). farnsworth=<wpm> keeps the
    characters at wpm and stretches only the letter and word gaps to that
    slower overall speed (ARRL formula)."""
    unit = 1200.0 / wpm
    letter_gap, word_gap = 3 * unit, 7 * unit
    if farnsworth and farnsworth < wpm:
        delay = (60.0 * wpm - 37.2 * farnsworth) / (farnsworth * wpm) * 1000
        letter_gap, word_gap = 3 * delay / 19, 7 * delay / 19
    out = []
    for w, word in enumerate(_tokens(text)):
        if w:
            out.append((False, word_gap))
        for c, code in enumerate(word):
            if c:
                out.append((False, letter_gap))
            for e, element in enumerate(code):
                if e:
                    out.append((False, unit))
                out.append((True, unit if element == "." else 3 * unit))
    return out


def render(segments, freq=800, amp=8000, rate=RATE, lead_ms=200, tail_ms=300):
    """PCM for [(key_down, ms)]: continuous-phase sine, raised-cosine edges
    inside each mark, silence lead and tail (room for codec delay), padded
    to whole 20 ms frames."""
    edge = max(1, int(rate * EDGE_MS / 1000))
    w = 2 * math.pi * freq / rate
    t = lead_ms
    out = [0] * int(round(rate * lead_ms / 1000))
    for down, ms in segments:
        start, t = len(out), t + ms
        n = max(0, int(round(rate * t / 1000)) - start)
        if not down:
            out.extend([0] * n)
            continue
        ramp = min(edge, n // 2)
        for i in range(n):
            gain = 1.0
            k = min(i, n - 1 - i)
            if k < ramp:
                gain = 0.5 - 0.5 * math.cos(math.pi * (k + 0.5) / ramp)
            out.append(max(-32768, min(32767, int(round(amp * gain * math.sin(w * (start + i)))))))
    out.extend([0] * int(round(rate * tail_ms / 1000)))
    frame = rate // 50
    out.extend([0] * (-len(out) % frame))
    return struct.pack("<%dh" % len(out), *out)


def encode(text, wpm=20, freq=800, amp=8000, rate=RATE, farnsworth=None, lead_ms=200, tail_ms=300):
    """S16LE PCM of text keyed as a freq Hz tone; raises ValueError for a
    character without a code."""
    return render(timing(text, wpm, farnsworth), freq, amp, rate, lead_ms, tail_ms)


# ------------------------------------------------------------------ decode

def _goertzel(x, lo, hi, coef):
    """Power of one frequency over x[lo:hi] with the window mean removed,
    normalised (amp^2 / 4 for a sine); also returns the window variance."""
    n = hi - lo
    mean = sum(x[lo:hi]) / n
    a = b = 0.0
    var = 0.0
    for i in range(lo, hi):
        v = x[i] - mean
        var += v * v
        a, b = v + coef * a - b, a
    return (a * a + b * b - coef * a * b) / (n * n), var / n


def _find_freq(x, rate, lo=400, hi=2000):
    """Strongest tone between lo and hi Hz over the loudest 20 ms windows."""
    size = rate // 50
    starts = range(0, len(x) - size + 1, size)
    loud = sorted(starts, key=lambda s: -_goertzel(x, s, s + size, 0.0)[1])[:24]

    def total(f):
        coef = 2 * math.cos(2 * math.pi * f / rate)
        return sum(_goertzel(x, s, s + size, coef)[0] for s in loud)

    best = max(range(lo, hi + 1, 20), key=total)
    return max(range(best - 20, best + 21, 2), key=total)


def _otsu(values, step=0.5):
    """Two-class split of values (Otsu): (midpoint of the class means, low
    mean, high mean); the midpoint is stable when the classes leave a wide
    empty band (digital silence)."""
    lo, hi = min(values), max(values)
    bins = max(1, int((hi - lo) / step) + 1)
    hist = [0] * bins
    for v in values:
        hist[min(bins - 1, int((v - lo) / step))] += 1
    total, s_all = len(values), sum(values)
    best, w0, s0 = (-1.0, lo, lo, hi), 0, 0.0
    for i in range(bins - 1):
        w0 += hist[i]
        s0 += hist[i] * (lo + (i + 0.5) * step)
        w1 = total - w0
        if not w0 or not w1:
            continue
        m0, m1 = s0 / w0, (s_all - s0) / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best[0]:
            best = (between, 0.5 * (m0 + m1), m0, m1)
    return best[1:]


def _split(lengths):
    """Two clusters of positive lengths in the log domain: (low, high)
    lists, or (lengths, []) when they do not separate by 1.8x."""
    if len(lengths) < 2:
        return list(lengths), []
    s = sorted(lengths)
    logs = [math.log(v) for v in s]
    best, cut = -1.0, 0
    for i in range(1, len(s)):
        m0, m1 = sum(logs[:i]) / i, sum(logs[i:]) / (len(s) - i)
        between = i * (len(s) - i) * (m1 - m0) ** 2
        if between > best:
            best, cut = between, i
    low, high = s[:cut], s[cut:]
    if _median(high) / _median(low) < 1.8:
        return s, []
    return low, high


def _median(values):
    s = sorted(values)
    return s[len(s) // 2] if len(s) % 2 else 0.5 * (s[len(s) // 2 - 1] + s[len(s) // 2])


def _envelope(x, rate, freq):
    """(dB tone power, tone share of power) per 5 ms block, each over a
    10 ms window centred on the block."""
    block = int(rate * BLOCK_MS / 1000)
    coef = 2 * math.cos(2 * math.pi * freq / rate)
    db, share = [], []
    for start in range(0, len(x) - block + 1, block):
        lo, hi = max(0, start - block // 2), min(len(x), start + block + block // 2)
        power, var = _goertzel(x, lo, hi, coef)
        db.append(10 * math.log10(power + 1e-3))
        share.append(2 * power / var if var else 0.0)
    return db, share


def _keying(db, lo_mean, hi_mean, threshold):
    """Key-down runs [(start_ms, end_ms)] from the dB envelope: local
    threshold (follows slow level changes), hysteresis, edges interpolated
    between block centres."""
    span = int(600 / BLOCK_MS)
    depth = hi_mean - lo_mean
    hyst = min(3.0, 0.1 * depth)
    floor = lo_mean + 0.35 * depth
    local = [max(db[max(0, i - span):i + span + 1]) for i in range(len(db))]
    state, edges = False, []
    for i, value in enumerate(db):
        th = threshold if local[i] >= threshold + 6 else max(floor, 0.5 * (local[i] + lo_mean))
        flip = value > th + hyst if not state else value < th - hyst
        if flip:
            prev = db[i - 1] if i else lo_mean
            frac = (th - prev) / (value - prev) if value != prev else 0.5
            edges.append((i - 1 + min(1.0, max(0.0, frac)) + 0.5) * BLOCK_MS)
            state = not state
    if state:
        edges.append((len(db) + 0.5) * BLOCK_MS)
    return [(edges[k], edges[k + 1]) for k in range(0, len(edges) - 1, 2)]


def _clean(marks, min_mark, min_gap):
    """Merge marks separated by a gap below min_gap, then drop marks
    shorter than min_mark."""
    merged = []
    for start, end in marks:
        if merged and start - merged[-1][1] < min_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [m for m in merged if m[1] - m[0] >= min_mark]


def _unit(marks, gaps):
    """(unit ms, smear ms): marks are k*unit + smear, gaps k*unit - smear.
    Least squares over the median dot, dash and element gap."""
    lengths = [e - s for s, e in marks]
    dots, dashes = _split(lengths)
    if dashes:
        d1, d3 = _median(dots), _median(dashes)
        unit, smear = (d3 - d1) / 2, d1 - (d3 - d1) / 2
        inner = [g for g in gaps if g < 2 * unit - smear]
        if len(inner) >= 2:
            g1 = _median(inner)
            unit = (d3 + g1) / 4
            smear = (d1 + d3 - g1) / 3 - unit
        if abs(smear) <= 0.8 * unit:
            return unit, smear
        return (sum(dots) + sum(dashes)) / (len(dots) + 3 * len(dashes)), 0.0
    m = _median(lengths)
    short = [g for g in gaps if g < 1.5 * min(gaps)] if gaps else []
    if short and m > 2.2 * _median(short):
        return m / 3, 0.0
    return m, 0.0


def decode(pcm, rate=RATE, freq=None, wpm=None):
    """Best-effort text of a keyed tone in S16LE PCM; '' when there is no
    keyed tone. wpm (optional) fixes the dot length instead of estimating
    it from the data."""
    x = list(struct.unpack("<%dh" % (len(pcm) // 2), pcm[:len(pcm) // 2 * 2]))
    if len(x) < rate // 10 or max(abs(v) for v in x) < 64:
        return ""
    if freq is None:
        freq = _find_freq(x, rate)
    db, share = _envelope(x, rate, freq)
    threshold, lo_mean, hi_mean = _otsu(db)
    if hi_mean - lo_mean < 10:
        return ""
    on = [s for d, s in zip(db, share) if d > threshold]
    if not on or _median(on) < 0.25:
        return ""
    level = lo_mean + LEVEL * (hi_mean - lo_mean)
    marks = _clean(_keying(db, lo_mean, hi_mean, level), 12.0, 12.0)
    if not marks:
        return ""
    gaps = [marks[k + 1][0] - marks[k][1] for k in range(len(marks) - 1)]
    unit, smear = (1200.0 / wpm, 0.0) if wpm else _unit(marks, gaps)
    # very short gaps are flicker inside a mark, tiny marks are noise
    intra = [g for g in gaps if g < 2 * unit - smear]
    element_gap = _median(intra) if len(intra) >= 3 else unit - smear
    marks = _clean(marks, 0.35 * unit, 0.35 * element_gap)
    if not marks:
        return ""
    gaps = [marks[k + 1][0] - marks[k][1] for k in range(len(marks) - 1)]
    letter_cut = 2 * unit - smear
    word_cut = _word_cut([g for g in gaps if g >= letter_cut], unit, smear)
    words, letter, text = [], [], []
    for k, (start, end) in enumerate(marks):
        letter.append((end - start, gaps[k - 1] if k and letter else None))
        gap = gaps[k] if k < len(gaps) else None
        if gap is None or gap >= letter_cut:
            text.append(_letter(letter, unit, smear, element_gap))
            letter = []
            if gap is None or gap >= word_cut:
                words.append("".join(text))
                text = []
    return " ".join(words)


def _word_cut(long_gaps, unit, smear):
    """Gap length that separates words from letters (Farnsworth spacing
    makes both longer, so split the observed gaps when they cluster)."""
    low, high = _split(long_gaps)
    if high:
        return max(math.sqrt(_median(low) * _median(high)), 4 * unit - smear)
    return 5 * unit - smear


def _fit(elements, unit, smear, element_gap):
    """(code, timing cost) of [(mark ms, gap before ms or None)]: log
    distance of every mark to a dot or dash and every inner gap to the
    element gap."""
    code, cost = "", 0.0
    for mark, gap in elements:
        dot, dash = abs(math.log(mark / (unit + smear))), abs(math.log(mark / (3 * unit + smear)))
        code += "." if dot <= dash else "-"
        cost += min(dot, dash)
        if gap is not None:
            cost += abs(math.log(max(gap, 1.0) / element_gap))
    return code, cost


def _letter(elements, unit, smear, element_gap):
    """Character of one letter. A short inner gap may be a dropout inside a
    mark: closing it costs DROPOUT_COST, and it is closed when that makes the
    code valid or fits the timing better. Unknown codes give '?'."""
    code, cost = _fit(elements, unit, smear, element_gap)
    while True:
        inner = [(g, i) for i, (_m, g) in enumerate(elements)
                 if g is not None and g < DROPOUT_GAP * element_gap]
        if not inner:
            break
        gap, i = min(inner)
        joined = (elements[:i - 1] + [(elements[i - 1][0] + gap + elements[i][0], elements[i - 1][1])]
                  + elements[i + 1:])
        code2, cost2 = _fit(joined, unit, smear, element_gap)
        cost2 += DROPOUT_COST
        valid, valid2 = code in DECODE, code2 in DECODE
        if (valid2 and not valid) or (valid2 == valid and cost2 < cost):
            elements, code, cost = joined, code2, cost2
        else:
            break
    return DECODE.get(code, "?")


# ------------------------------------------------------------------- check

def _normalise(text):
    return " ".join(text.upper().split())


def check(expected, got):
    """Compare decoded text with what was sent (case and repeated spaces
    ignored): {"exact", "distance" (Levenshtein), "cer", "expected", "got",
    "diff": [(op, pos, expected_part, got_part)]}."""
    a, b = _normalise(expected), _normalise(got)
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    diff = [(op, i1, a[i1:i2], b[j1:j2])
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if op != "equal"]
    return {"exact": a == b, "distance": row[-1], "cer": row[-1] / len(a) if a else float(bool(b)),
            "expected": a, "got": b, "diff": diff}
