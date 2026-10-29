# SPDX-License-Identifier: MIT
"""Speech quality through a transcoder, differential: the transcoder under
test (DVT_AMBED_ADDR / TRANSCODER_IMAGE; in the rig transcoder-sut) against
the reference (DVT_AMBED_REF; in the rig transcoder-ref, e.g. with
TRANSCODER_SUT_IMAGE and TRANSCODER_REF_IMAGE) in the same run.

Input: 20 CMU ARCTIC utterances per voice (bdl, slt) at 8 kHz from
scripts/pre-fetch-speech.sh, one stream per voice (300 ms between
utterances). Per codec served by both: PCM -> codec -> PCM, codec delay
from the envelope, then per utterance STOI (8 kHz, 14 bands), LSD and MCD.
Pass: mean STOI(sut) >= STOI(ref) - 0.02 and mean LSD(sut) <= LSD(ref) +
0.5 dB; MCD is reported only. $DVT_OUT/<run>/speech_quality.json carries
numbers and sha256 only, never audio.
"""
import hashlib
import json
import os
import random
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from dvtest.audio import signals, speech
from dvtest.proto import ambed

pytestmark = [pytest.mark.wire, pytest.mark.speech]

CORPUS = Path(__file__).resolve().parents[1] / ".cache" / "speech" / "8k"
CODECS = {"ambeplus": ambed.AMBEPLUS, "ambe2plus": ambed.AMBE2PLUS, "codec2_3200": ambed.CODEC2_3200,
          "imbe": ambed.IMBE}
LEAD_MS, GAP_MS = 200, 300
STOI_MARGIN, LSD_MARGIN = 0.02, 0.5
_rng = random.SystemRandom()


@pytest.fixture(scope="module")
def corpus():
    """{voice: (frames, [(id, start sample, samples)], sha256 of the stream)}."""
    loaded = speech.load(CORPUS)
    if not loaded:
        pytest.skip("no speech corpus in %s: run scripts/pre-fetch-speech.sh" % CORPUS.name)
    out = {}
    for voice, utterances in loaded.items():
        pcm, spans = bytearray(signals.silence(LEAD_MS)), []
        for uid, data in utterances:
            spans.append((uid, len(pcm) // 2, len(data) // 2))
            pcm += data + signals.silence(GAP_MS)
        pcm += bytes(-len(pcm) % signals.FRAME_BYTES)
        out[voice] = (signals.frames(bytes(pcm)), spans, hashlib.sha256(pcm).hexdigest())
    return out


def caps_of(addr):
    client = ambed.AmbedClient(*addr)
    try:
        caps = ambed.parse_caps(client.rpc(ambed.CAPS_REQUEST) or b"")
    finally:
        client.close()
    assert caps is not None, "no CAPS reply from %s" % addr[0]
    return caps


def transcode(addr, codec_in, outs, frames):
    return ambed.transcode(addr[0], codec_in, outs, frames, port=addr[1], transaction=_rng.randrange(1, 1 << 32))


def round_trip(addr, max_outputs, codecs, corpus):
    """{(codec, voice): output PCM bytes}: PCM -> codecs per voice, then
    codec -> PCM; voices and codecs run as concurrent streams."""
    chunks = [codecs[i:i + max_outputs] for i in range(0, len(codecs), max_outputs)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        encoded = {}
        jobs = {(voice, tuple(chunk)): pool.submit(transcode, addr, ambed.PCM, chunk, frames)
                for voice, (frames, _s, _h) in corpus.items() for chunk in chunks}
        for (voice, _chunk), job in jobs.items():
            for codec, payloads in job.result().items():
                encoded[codec, voice] = payloads
        jobs = {key: pool.submit(transcode, addr, key[0], [ambed.PCM], payloads)
                for key, payloads in encoded.items()}
        return {key: b"".join(job.result()[ambed.PCM]) for key, job in jobs.items()}


def measure(addr, codecs, corpus):
    """{codec: {"stoi", "lsd", "mcd", "voices": {...}}} for one transcoder."""
    caps = caps_of(addr)
    outputs = round_trip(addr, caps.max_outputs, codecs, corpus)
    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
        jobs = {key: pool.submit(speech.score_stream, b"".join(corpus[key[1]][0]), pcm, corpus[key[1]][1])
                for key, pcm in outputs.items()}
        scored = {key: job.result() for key, job in jobs.items()}
    out = {}
    for codec in codecs:
        voices, rows = {}, []
        for voice in corpus:
            lag, utts = scored[codec, voice]
            rows += utts
            voices[voice] = {"delay_ms": lag / 8.0, "output_sha256": hashlib.sha256(outputs[codec, voice]).hexdigest(),
                             "utterances": [[u, round(s, 4), round(l, 3), round(m, 3)] for u, s, l, m in utts]}
        out[codec] = {"stoi": sum(r[1] for r in rows) / len(rows), "lsd": sum(r[2] for r in rows) / len(rows),
                      "mcd": sum(r[3] for r in rows) / len(rows), "voices": voices}
    return out


@pytest.fixture(scope="module")
def quality(ambed_addr, ambed_ref_addr, corpus, out_root):
    """Both transcoders, one after the other (their data ports overlap)."""
    sut_caps, ref_caps = caps_of(ambed_addr), caps_of(ambed_ref_addr)
    if not (sut_caps.mask & ref_caps.mask & (1 << ambed.PCM)):
        pytest.skip("PCM not announced by both (masks 0x%02x / 0x%02x)" % (sut_caps.mask, ref_caps.mask))
    codecs = [c for c in CODECS.values() if sut_caps.mask & ref_caps.mask & (1 << c)]
    result = {"sut": measure(ambed_addr, codecs, corpus), "ref": measure(ambed_ref_addr, codecs, corpus),
              "masks": {"sut": sut_caps.mask, "ref": ref_caps.mask}}
    report = {"inputs": {v: {"stream_sha256": h, "utterances": [s[0] for s in spans]}
                         for v, (_f, spans, h) in corpus.items()},
              "gate": {"stoi_margin": STOI_MARGIN, "lsd_margin_db": LSD_MARGIN},
              "masks": result["masks"],
              "codecs": {name: {role: result[role][c] for role in ("sut", "ref")}
                         for name, c in CODECS.items() if c in codecs}}
    (out_root / "speech_quality.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    return result


@pytest.mark.parametrize("name", list(CODECS))
def test_speech_quality_vs_reference(quality, name):
    codec = CODECS[name]
    if codec not in quality["sut"]:
        pytest.skip("%s not announced by both transcoders (masks sut 0x%02x ref 0x%02x)"
                    % (name, quality["masks"]["sut"], quality["masks"]["ref"]))
    sut, ref = quality["sut"][codec], quality["ref"][codec]
    print("%s: STOI sut %.4f ref %.4f | LSD sut %.2f ref %.2f dB | MCD sut %.2f ref %.2f dB"
          % (name, sut["stoi"], ref["stoi"], sut["lsd"], ref["lsd"], sut["mcd"], ref["mcd"]))
    assert sut["stoi"] >= ref["stoi"] - STOI_MARGIN, "STOI %.4f < ref %.4f - %.2f" % (sut["stoi"], ref["stoi"],
                                                                                       STOI_MARGIN)
    assert sut["lsd"] <= ref["lsd"] + LSD_MARGIN, "LSD %.2f > ref %.2f + %.1f dB" % (sut["lsd"], ref["lsd"],
                                                                                    LSD_MARGIN)
