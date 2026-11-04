# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_crossmode_real_ysf.py: what a YSF listener should hear of an over that came in
through another protocol, and whether the decoded audio still carries the tone.

  expected_ysf(frames)       AMBE+2 (DMR form) frames as a YSF listener gets them: through the VCH coder once
  heard_frames(events, t)    the voice frames of the first over after time t in a list of echo.Ev
  tone_report(pcm, freq)     dominant frequency, the share of spectrum power within 60 Hz of freq and the rms of a PCM byte string
  repetition(frames)         (most common frame, how often) - a garbled stream repeats one frame
  active_dbfs(pcm) / scale_active(pcm, dbfs) / level_summary(pcm)
                             levels in dBFS (re 32768): rms of the whole signal and of its active 20 ms frames
  record_row / format_table  one measurement per (pair, tag, signal) as a dict, and the markdown table of a list of them
  gain_check(sent, heard, steps)
                             YSF egress / ingress change only the AMBE+2 gain index b2 (xlxd parity): every bit but b2
                             equal, b2 mapped by the named steps ("p10": YSF ingress +1.0 dB, "m04": YSF egress -0.4 dB)

The b2 maps are those of DV-Router (crates/lib/radio/src/ambe2.rs, xlxd B2_MAP_PLUS_1_0_DB and the nearest-entry
fallback of AdjustAmbeGain for -0.4 dB); the ruling and the lab frames are in the report of 2026-11-04 (docs/echo-cases.md).
"""
import math
import struct
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


# ---------------------------------------------------------------- levels (cases/test_transcoder_levels.py)

FULL_SCALE = 32768.0
ACTIVE_FLOOR_DBFS = -50.0          # a 20 ms frame quieter than this is a pause


def _samples(pcm):
    return struct.unpack("<%dh" % (len(pcm) // 2), pcm[:len(pcm) // 2 * 2])


def _dbfs(rms):
    return 20 * math.log10(rms / FULL_SCALE) if rms > 0 else -120.0


def active_dbfs(pcm, size=160):
    """rms in dBFS of the frames of `size` samples that are not pauses (rms above ACTIVE_FLOOR_DBFS); -120 when none."""
    s = _samples(pcm)
    frames = [s[i:i + size] for i in range(0, len(s) - size + 1, size)]
    power = [sum(x * x for x in f) / size for f in frames]
    active = [p for p in power if p > 0 and _dbfs(math.sqrt(p)) > ACTIVE_FLOOR_DBFS]
    return _dbfs(math.sqrt(sum(active) / len(active))) if active else -120.0


def scale_active(pcm, target_dbfs):
    """`pcm` scaled so that its active level is target_dbfs (samples clipped to 16 bit)."""
    s = _samples(pcm)
    gain = 10 ** ((target_dbfs - active_dbfs(pcm)) / 20)
    return struct.pack("<%dh" % len(s), *[max(-32768, min(32767, int(round(x * gain)))) for x in s])


def level_summary(pcm):
    s = _samples(pcm)
    rms = math.sqrt(sum(x * x for x in s) / len(s)) if s else 0.0
    return {"rms": rms, "rms_dbfs": _dbfs(rms), "active_dbfs": active_dbfs(pcm)}


def record_row(tag, pair, signal, in_active, sent, got, out, content, extra=None):
    """One measurement: levels of the decoded audio `out` (a level_summary), content = {"ok", "dominant", "share", "env_corr"}."""
    row = {"tag": tag, "pair": pair, "signal": signal, "in_active_dbfs": round(in_active, 1), "sent": sent, "got": got,
           "drops": max(0, sent - got), "rms": round(out["rms"]), "out_active_dbfs": round(out["active_dbfs"], 1),
           "delta_db": round(out["active_dbfs"] - in_active, 1)}
    row.update(content)
    row.update(extra or {})
    return row


def format_table(rows, tags=None):
    """Markdown table, one line per (pair, signal) with the rms / active dBFS / delta of each tag side by side (rows from record_row)."""
    tags = tags or sorted({r["tag"] for r in rows})
    keys = []
    for r in rows:
        k = (r["pair"], r["signal"])
        if k not in keys:
            keys.append(k)
    head = "| pair | signal | in act dBFS | " + " | ".join("%s: rms / act dBFS (delta) / got/sent / content" % t for t in tags) + " |"
    lines = [head, "|" + "---|" * (3 + len(tags))]
    for pair, signal in keys:
        cells, in_act = [], ""
        for t in tags:
            r = next((x for x in rows if x["tag"] == t and x["pair"] == pair and x["signal"] == signal), None)
            if r is None:
                cells.append("-")
                continue
            in_act = r["in_active_dbfs"]
            content = "ok" if r.get("ok") else "BAD"
            if r.get("dominant") is not None and signal == "tone":
                content += " %s Hz" % r["dominant"]
            if r.get("env_corr") is not None and signal != "tone":
                content += " env %.2f" % r["env_corr"]
            cells.append("%d / %.1f (%+.1f) / %d/%d / %s" % (r["rms"], r["out_active_dbfs"], r["delta_db"], r["got"], r["sent"], content))
        lines.append("| %s | %s | %s | %s |" % (pair, signal, in_act, " | ".join(cells)))
    return "\n".join(lines)


def summarize(rows, tags=None):
    """Markdown table, one line per pair with, for each tag: the mean level change of the speech rows (min..max), the tone row (active dBFS delta,
    rms), the frames lost over all rows of the pair and the number of rows whose content check failed."""
    tags = tags or sorted({r["tag"] for r in rows})
    pairs = []
    for r in rows:
        if r["pair"] not in pairs:
            pairs.append(r["pair"])
    head = "| pair | " + " | ".join("%s: speech delta dB mean (min..max) / tone delta dB, rms / lost frames / bad" % t for t in tags) + " |"
    lines = [head, "|" + "---|" * (1 + len(tags))]
    for pair in pairs:
        cells = []
        for t in tags:
            mine = [r for r in rows if r["tag"] == t and r["pair"] == pair]
            speech = [r["delta_db"] for r in mine if r["signal"] != "tone"]
            tone = next((r for r in mine if r["signal"] == "tone"), None)
            if not mine:
                cells.append("-")
                continue
            cells.append("%+.1f (%+.1f..%+.1f) / %s / %d / %d" % (
                sum(speech) / len(speech) if speech else 0.0, min(speech or [0.0]), max(speech or [0.0]),
                "%+.1f, %d" % (tone["delta_db"], tone["rms"]) if tone else "-",
                sum(r["drops"] for r in mine), sum(1 for r in mine if not r.get("ok"))))
        lines.append("| %s | %s |" % (pair, " | ".join(cells)))
    return "\n".join(lines)


def cpu_window(lines, start, end):
    """(samples, mean cpu %, max cpu %, max memory MiB) of the docker stats lines "<epoch> <cpu>% <mem> / <limit>" inside [start, end]."""
    cpu, mem = [], []
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[1].endswith("%"):
            continue
        try:
            t, c = float(parts[0]), float(parts[1].rstrip("%"))
        except ValueError:
            continue
        if start <= t <= end:
            cpu.append(c)
            m = parts[2]
            try:
                mem.append(float(m[:-3]) / (1024.0 if m.endswith("KiB") else 1.0) if m[-3:] in ("MiB", "KiB") else float(m[:-3]) * 1024.0)
            except ValueError:
                pass
    return (len(cpu), sum(cpu) / len(cpu) if cpu else 0.0, max(cpu or [0.0]), max(mem or [0.0]))


def load_summary(load, cpu_lines):
    """One line for a load run: streams complete / total, frames received / sent, lost, transcoder CPU in the window."""
    streams = load["streams"]
    ok = [s for s in streams.values() if "error" not in s and s["got"] >= s["sent"] * 0.9]
    sent = sum(s.get("sent", 0) for s in streams.values())
    got = sum(s.get("got", 0) for s in streams.values())
    lost = sum(max(0, s.get("sent", 0) - s.get("got", 0)) for s in streams.values())
    n, mean, peak, mem = cpu_window(cpu_lines, load["start"], load["end"])
    return {"tag": load["tag"], "streams_ok": len(ok), "streams": len(streams), "frames_got": got, "frames_sent": sent, "lost": lost,
            "window_s": round(load["end"] - load["start"], 1), "cpu_samples": n, "cpu_mean": round(mean, 1), "cpu_max": round(peak, 1),
            "mem_max_mib": round(mem, 1)}
