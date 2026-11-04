# SPDX-License-Identifier: MIT
"""Levels of cross-mode audio through the REAL transcoder, per transcoder image tag (docs/transcoder-levels.md).

Run it once per tag: `TRANSCODER_IMAGE=<tag> DVT_TRANSCODER_LEVELS=1 scripts/run.sh -m rig -- cases/test_transcoder_levels.py` (the rig's
transcoder-ref and transcoder-sut both run the tag). Skips unless DVT_TRANSCODER_LEVELS=1, with the fake transcoder, or without transcoder-ref.

For each of nine pairs (M17 -> DMR, D-STAR, YSF; DMR -> D-STAR, M17, YSF; D-STAR -> DMR, M17, YSF) a talker keys, on one module, the 700 Hz tone
(amp 8000) and three real speech clips (the Thai clips thai_fixed, porjai02, porjai03 of the vocoder session, 8 kHz, first 6 s, git-ignored in
`.cache/speech-levels/`; CMU ARCTIC from scripts/pre-fetch-speech.sh stands in when they are absent) scaled to an active level of -26, -20 and -14 dBFS, encoded by transcoder-ref into the talker's codec; listeners of the other protocols hear it
through the SUT and transcoder-ref decodes their frames. One JSON row per measurement goes to out/<run>/transcoder-levels.jsonl
(`python3 -m dvtest.tools.levels_table` makes the table). The cases assert delivery (frames) and content (tone: the dominant frequency; speech:
the log-energy envelope follows the source); the levels are data.

`test_load_16_streams` runs 16 cross-mode streams at once for about 20 s (the transcoder CPU is sampled on the host, see the doc).
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, crossaudio, echo
from dvtest.audio import score, signals
from dvtest.proto import ambed
from echo_support import Drainer

pytestmark = pytest.mark.rig

ROOT = Path(__file__).resolve().parents[1]
MODULE = "H"
CODEC = {"dmr": ambed.AMBE2PLUS, "dstar": ambed.AMBEPLUS, "m17": ambed.CODEC2_3200, "ysf": ambed.AMBE2PLUS}
TARGETS = {"m17": ("dmr", "dstar", "ysf"), "dmr": ("dstar", "m17", "ysf"), "dstar": ("dmr", "m17", "ysf")}
CLIPS = ("thai_fixed", "porjai02", "porjai03")
# the same three Thai clips the vocoder session measured (git-ignored, copied to .cache/speech-levels/<name>.raw, 8 kHz S16LE, first 6 s);
# without them the CMU ARCTIC clips of scripts/pre-fetch-speech.sh stand in (and the rows say "arctic")
ARCTIC_FALLBACK = {"thai_fixed": ("bdl", "arctic_a0001"), "porjai02": ("slt", "arctic_a0002"), "porjai03": ("bdl", "arctic_a0003")}
LEVELS = (-26, -20, -14)
FREQ = 700
LOAD_MODULES = "ABCDFGHIJKLOPQRS"            # 16 modules, not E / M / N (echo, M17 peers)
PUSH_GAP = 0.005                             # seconds between the frames pushed into transcoder-ref (faster than real time)
SETTLE = 1.5                                 # after the last packet of an over, before the listeners are read


@pytest.fixture(scope="module")
def levels(rig):
    if os.environ.get("DVT_TRANSCODER_LEVELS") != "1":
        pytest.skip("transcoder level measurement: set DVT_TRANSCODER_LEVELS=1 (one run per transcoder image tag)")
    if os.environ.get("DVT_TRANSCODER_FAKE") == "1":
        pytest.skip("the rig's transcoder is the fake: this needs the real one")
    if not rig.is_given("ambed_ref"):
        pytest.skip("no transcoder-ref (DVT_AMBED_REF)")
    return rig


def tag():
    return os.environ.get("DVT_TRANSCODER_TAG") or "unknown"


def emit(rig, row):
    print("[ROW] " + json.dumps(row, sort_keys=True))
    try:
        with open(Path(rig.out) / "transcoder-levels.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


def clip_pcm(name):
    """(label, pcm) of a clip: the Thai clip under .cache/speech-levels, else the ARCTIC stand-in."""
    path = ROOT / ".cache" / "speech-levels" / (name + ".raw")
    if path.is_file():
        data = path.read_bytes()
        return name, data[:len(data) // 2 * 2]
    speaker, uid = ARCTIC_FALLBACK[name]
    for base in (ROOT / ".cache" / "speech-levels" / "8k", ROOT / ".cache" / "speech" / "8k"):
        path = base / speaker / (uid + ".raw")
        if path.is_file():
            data = path.read_bytes()
            return "arctic_" + uid[-4:], data[:len(data) // 2 * 2]
    pytest.skip("no speech clip %s: copy it to .cache/speech-levels/%s.raw or run scripts/pre-fetch-speech.sh" % (name, name))


def source_signals():
    """[(name, pcm)]: the tone and the clips at the three levels."""
    out = [("tone", b"".join(signals.frames(signals.tone(FREQ, 3000))))]
    for clip in CLIPS:
        label, raw = clip_pcm(clip)
        for level in LEVELS:
            out.append(("%s@%d" % (label, level), crossaudio.scale_active(raw, level)))
    return out


def encode(rig, codec, pcm):
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [codec], signals.frames(pcm), port=rig.ambed_port, gap=PUSH_GAP)[codec]


def decode(rig, codec, frames):
    return b"".join(ambed.transcode(rig.ambed_ref, codec, [ambed.PCM], frames, port=rig.ambed_port, gap=PUSH_GAP)[ambed.PCM])


def content(name, src_pcm, out_pcm):
    """{"ok", "dominant", "share", "env_corr"} of the decoded audio against the source signal."""
    spec_report = crossaudio.tone_report(out_pcm, FREQ)
    if name == "tone":
        return {"ok": bool(spec_report["dominant"] and abs(spec_report["dominant"] - FREQ) <= 80 and spec_report["share"] >= 0.5),
                "dominant": spec_report["dominant"], "share": round(spec_report["share"], 2), "env_corr": None}
    corr, _lag = score.envelope_correlation(src_pcm, out_pcm)
    return {"ok": corr >= 0.5 and crossaudio.active_dbfs(out_pcm) > -70, "dominant": None, "share": None, "env_corr": round(corr, 2)}


@pytest.mark.parametrize("src", ["m17", "dmr", "dstar"])
def test_levels_by_source(levels, src):
    rig = levels
    sigs = source_signals()
    talker = clients.make(src, MODULE, 0)
    heard = {dst: clients.make(dst, MODULE, 1) for dst in TARGETS[src]}
    drains, problems = [], []
    try:
        for c in [talker] + list(heard.values()):
            assert c.link(rig.xlxd_sut), "%s client did not link" % c.__class__.__name__
        for c in heard.values():
            c.client.drain()
            c.client.inbox.clear()
            c.client.inbox_times.clear()
            drains.append(Drainer(c.client))
            drains[-1].start()
        for name, pcm in sigs:
            sent = encode(rig, CODEC[src], pcm)
            t0 = time.monotonic()
            clients.talk(talker, src, sent, tail=0.0)
            time.sleep(SETTLE)
            for dst, listener in heard.items():
                evs = echo.events(dst, list(listener.client.inbox), list(listener.client.inbox_times))
                got = crossaudio.heard_frames(evs, t0 - 0.5)
                out_pcm = decode(rig, CODEC[dst], got) if got else b""
                if out_pcm:
                    summary, cont = crossaudio.level_summary(out_pcm), content(name, pcm, out_pcm)
                else:
                    summary, cont = {"rms": 0.0, "rms_dbfs": -120.0, "active_dbfs": -120.0}, {"ok": False, "dominant": None, "share": None, "env_corr": None}
                row = crossaudio.record_row(tag(), "%s>%s" % (src, dst), name, crossaudio.active_dbfs(pcm), len(sent), len(got), summary, cont)
                emit(rig, row)
                if len(got) < 0.9 * len(sent):
                    problems.append("%s>%s %s: %d of %d frames" % (src, dst, name, len(got), len(sent)))
                elif not cont["ok"]:
                    problems.append("%s>%s %s: content check failed %s" % (src, dst, name, cont))
            time.sleep(1.0)
    finally:
        for d in drains:
            d.stop()
        clients.close(talker, *heard.values())
    assert not problems, "; ".join(problems)


PAIRS = [(s, d) for s in TARGETS for d in TARGETS[s]]


def test_load_16_streams(levels):
    """16 cross-mode streams at once (the nine pairs in turn), each a speech clip repeated for about 20 s at -20 dBFS: frames received
    per stream, and the window (epoch seconds) in out/<run>/transcoder-levels-load.json, to read the transcoder CPU of the same window."""
    rig = levels
    _label, raw = clip_pcm(CLIPS[0])
    one = crossaudio.scale_active(raw, -20)
    pcm = (one + bytes(2 * 8000 // 2)) * 6
    result = {}
    streams = []
    for i in range(16):
        src, dst = PAIRS[i % len(PAIRS)]
        streams.append((LOAD_MODULES[i], src, dst))

    def run(module, src, dst, ready, go):
        talker, listener = clients.make(src, module, 0), clients.make(dst, module, 1)
        drain = None
        try:
            assert talker.link(rig.xlxd_sut) and listener.link(rig.xlxd_sut), "link failed on module %s" % module
            frames = encode(rig, CODEC[src], pcm)
            listener.client.drain()
            listener.client.inbox.clear()
            listener.client.inbox_times.clear()
            drain = Drainer(listener.client)
            drain.start()
            ready.append(module)
            go.wait(60)
            t0 = time.monotonic()
            clients.talk(talker, src, frames, tail=0.0)
            time.sleep(SETTLE)
            evs = echo.events(dst, list(listener.client.inbox), list(listener.client.inbox_times))
            got = crossaudio.heard_frames(evs, t0 - 0.5)
            result[module] = {"pair": "%s>%s" % (src, dst), "sent": len(frames), "got": len(got)}
        except Exception as exc:                       # reported below
            result[module] = {"pair": "%s>%s" % (src, dst), "error": repr(exc)}
        finally:
            if drain is not None:
                drain.stop()
            clients.close(talker, listener)

    ready, go = [], threading.Event()
    threads = [threading.Thread(target=run, args=(m, s, d, ready, go)) for m, s, d in streams]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 120
    while len(ready) + sum(1 for r in result.values() if "error" in r) < len(streams) and time.monotonic() < deadline:
        time.sleep(0.2)
    start = time.time()
    go.set()
    for t in threads:
        t.join(180)
    end = time.time()
    summary = {"tag": tag(), "start": start, "end": end, "streams": result}
    with open(Path(rig.out) / "transcoder-levels-load.json", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(summary, sort_keys=True) + "\n")
    print("[LOAD] %s window %.0f..%.0f (%.1f s): %s" % (tag(), start, end, end - start, json.dumps(result, sort_keys=True)))
    bad = {m: r for m, r in result.items() if "error" in r or r["got"] < 0.9 * r["sent"]}
    assert not bad, "streams that lost frames or failed: %s" % bad
