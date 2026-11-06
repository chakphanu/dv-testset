# SPDX-License-Identifier: MIT
"""Prometheus text format (exposition format 0.0.4) reader for the hub
metrics of the system under test (control channel action "metrics").

  samples = parse(text)                       list of Sample(name, labels, value)
  total(samples, "dv_room_data_total", result="accepted")
                                              sum of the samples with that name whose
                                              labels contain every given label
  families(text)                              names seen in HELP / TYPE / sample lines

A label subset is matched by value, never by position: the order of the labels
in a sample line is not part of the format. Comment lines other than HELP and
TYPE are ignored; a sample line may carry a timestamp (ignored). The value
words NaN, +Inf and -Inf are parsed; a sample whose value cannot be parsed is
skipped (a hub must not break a case by one odd line).
"""
import math
import re
from collections import namedtuple

Sample = namedtuple("Sample", "name labels value")

_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}


def _unescape(text):
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(_ESCAPES.get(text[i + 1], "\\" + text[i + 1]))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _value(word):
    low = word.lower()
    if low in ("nan",):
        return math.nan
    if low in ("+inf", "inf"):
        return math.inf
    if low == "-inf":
        return -math.inf
    return float(word)


def _labels(rest):
    """(labels, text after the closing brace) of a string that starts with
    "{", or None when it is not well formed."""
    labels, pos = {}, 1
    while True:
        while rest[pos:pos + 1] in (" ", ","):
            pos += 1
        if rest[pos:pos + 1] == "}":
            return labels, rest[pos + 1:]
        m = _LABEL.match(rest, pos)
        if not m:
            return None
        labels[m.group(1)] = _unescape(m.group(2))
        pos = m.end()


def parse_line(line):
    """Sample of one line, or None (blank, comment, malformed)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = _NAME.match(line)
    if not m:
        return None
    name, rest = m.group(0), line[m.end():]
    labels = {}
    if rest.startswith("{"):
        got = _labels(rest)
        if got is None:
            return None
        labels, rest = got
    words = rest.split()
    if not words or len(words) > 2:
        return None
    try:
        return Sample(name, labels, _value(words[0]))
    except ValueError:
        return None


def parse(text):
    """All samples of a metrics text, in order."""
    out = []
    for line in text.splitlines():
        sample = parse_line(line)
        if sample is not None:
            out.append(sample)
    return out


def families(text):
    """Set of metric names that appear in HELP / TYPE lines or as a sample."""
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            parts = line.split(None, 3)
            if len(parts) >= 3:
                names.add(parts[2])
        else:
            sample = parse_line(line)
            if sample is not None:
                names.add(sample.name)
    return names


def total(samples, name, **labels):
    """Sum of the values of the samples called `name` whose labels contain
    every key=value of `labels` (0.0 when none match). `samples` is a list from
    parse() or the metrics text itself."""
    if isinstance(samples, str):
        samples = parse(samples)
    return sum(s.value for s in samples
               if s.name == name and all(s.labels.get(k) == v for k, v in labels.items())
               and not math.isnan(s.value))


# The per-edge data item counter of the hub. `dv_edge_data_total{result=...}` exists up to the generation before compat1; COMPAT1 removed
# it and no successor name is known to the rig yet (not in docs, the soak families, or any recorded run). Set EDGE_DATA_SUCCESSOR to the
# new family name when DV-Router names it; until then a case on compat1 reads no edge counter (edge_data_family() is None).
EDGE_DATA_OLD = "dv_edge_data_total"
EDGE_DATA_SUCCESSOR = None


def edge_data_family(gen=None):
    """The name of the per-edge data item counter on image generation `gen` (else env DVT_SUT_GEN, else the default), or None when the
    generation has none that the rig knows (compat1 until EDGE_DATA_SUCCESSOR is set). ConfigError for an unknown generation."""
    from dvtest import sut_config
    if sut_config.gen_at_least(gen, sut_config.COMPAT1):
        return EDGE_DATA_SUCCESSOR
    return EDGE_DATA_OLD


def edge_total(samples, gen=None, **labels):
    """total() of the per-edge data counter of generation `gen`; 0.0 when the generation has no such counter."""
    name = edge_data_family(gen)
    return 0.0 if name is None else total(samples, name, **labels)
