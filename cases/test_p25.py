# SPDX-License-Identifier: MIT
"""P25 through xlxd: a P25 client on a P25Reflector (p25-ref / p25-sut,
rig profile p25) whose talkgroup DVT_P25_TG is peered with module
DVT_P25_MODULE of xlxd-ref / xlxd-sut. xlxd talks P25 only as a peer of
the P25Reflector; P25 voice is IMBE (AMBEd codec 4), so every cell goes
through the transcoder and is skipped when it does not serve IMBE.

Cross cells are differential like the matrix: the same input (tone
bursts encoded by transcoder-ref at run time) goes through the ref and the
sut chain in the same run and the sut must deliver what the ref delivers.
P25 listeners also check the record lifecycle (LDU1 0x62..0x6A, LDU2
0x6B..0x73, terminator 0x80; this link has no header record) and the
talker must never hear its own call back.

Morse (dvtest.audio.tandem, its WPM): ID text P25 -> M17 (Codec2 decoded by c2dec, else by
transcoder-ref) and M17 -> P25 (IMBE decoded to PCM by transcoder-ref);
exact decode is required for each side whose transcoder passes a morse
probe of the same codec tandem, informational (xfail) while none does.
AllStar: P25 <-> IAX2 on the USRP-bridged module (profile allstar,p25).
Results: $DVT_OUT/p25.json.
"""
import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import score, signals, tandem
from dvtest.proto import ambed, dextra, dmr, iax2, m17, p25
from p25_common import encode, imbe_served, p25_client

pytestmark = [pytest.mark.rig, pytest.mark.p25]

FRAMES = 63                                  # 7 whole LDUs
KINDS = clients.KINDS                        # xlxd-side clients
ALLSTAR_NODES = {"A": "1901", "B": "1902"}   # rig/config/xlxd.usrp.in
RESULTS = {}


def sha(frames):
    return hashlib.sha256(b"".join(frames)).hexdigest()


def save(key, value):
    RESULTS[key] = value
    out = Path(os.environ.get("DVT_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "p25.json").write_text(json.dumps(RESULTS, indent=1, sort_keys=True) + "\n")


@pytest.fixture(scope="module")
def p25rig(rig):
    """Rig with profile p25 and transcoders that serve IMBE; waits until
    both xlxd instances have their P25 peer up."""
    if os.environ.get("DVT_P25") != "on":
        pytest.skip("no P25 reflectors: run.sh --profile p25 (DVT_P25=on)")
    for name in ("ambed_ref", "ambed_sut"):
        ok, how = imbe_served(getattr(rig, name), rig.ambed_port)
        if not ok:
            pytest.skip("%s does not serve IMBE (%s): P25 needs IMBE transcoding" % (name, how))
    tg = int(os.environ.get("DVT_P25_TG", "31672"))
    module = os.environ.get("DVT_P25_MODULE", "G")
    setup = {"tg": tg, "module": module, "sides": {"ref": (rig.xlxd_ref, rig.p25_ref),
                                                   "sut": (rig.xlxd_sut, rig.p25_sut)}}
    probe = encode(rig, ambed.AMBEPLUS, signals.frames(signals.tone(700, 400)))
    for side, (xlxd_ip, p25_ip) in setup["sides"].items():
        deadline = time.monotonic() + 60
        while True:
            got = run_to_p25(xlxd_ip, p25_ip, "dstar", module, tg, probe, tail=1.0)
            if got["frames"]:
                break
            if time.monotonic() > deadline:
                pytest.fail("%s: no D-STAR -> P25 audio after 60 s (xlxd %s not peered with %s TG %d?)"
                            % (side, xlxd_ip, p25_ip, tg))
            time.sleep(2)
    return rig, setup


def decode(rig, codec, frames):
    return b"".join(ambed.transcode(rig.ambed_ref, codec, [ambed.PCM], frames, port=rig.ambed_port,
                                    transaction=0x50320100 + codec)[ambed.PCM])


def stream_bounds(kind, inbox):
    """(opened, closed) of the stream a digital listener received: D-STAR
    header and last frame, DMR voice LC header and terminator, M17 EOT
    (packet 0 is not forwarded, voice opens the stream)."""
    if kind == "dstar":
        voices = [v for v in (dextra.parse_voice(d) for d in inbox) if v]
        return (any(dextra.parse_header(d) for d in inbox), any(v[2] for v in voices))
    if kind == "dmr":
        flags = [f["bitfield"] for f in (dmr.parse(d) for d in inbox) if f]
        sync = [f & 0x0F for f in flags if f & 0x30 == 0x20]
        return (1 in sync, 2 in sync)
    packets = [m for m in (m17.parse(d) for d in inbox) if m]
    return (bool(packets), any(m["eot"] for m in packets))


def run_from_p25(xlxd_ip, p25_ip, kind, module, tg, frames):
    """P25 talker on the P25Reflector, listener of kind on the xlxd module."""
    listener = clients.make(kind, module, 1)
    talker = p25_client(tg, 0)
    try:
        assert listener.link(xlxd_ip), "%s listener link to %s failed" % (kind, xlxd_ip)
        assert talker.link(p25_ip), "P25 talker link to %s failed" % p25_ip
        listener.client.drain()
        listener.client.inbox.clear()
        talker.client.inbox.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(talker.talk, frames)
            while not job.done():
                listener.client.pump(0.05)
            job.result()
        got = listener.collect()
        echo = [d for d in talker.client.inbox if p25.parse(d) is not None]
        return {"frames": got, "echo": len(echo), "bounds": stream_bounds(kind, listener.client.inbox)}
    finally:
        try:
            talker.unlink()
        finally:
            talker.close()
            clients.close(listener)


def run_to_p25(xlxd_ip, p25_ip, kind, module, tg, frames, tail=2.0):
    """Talker of kind on the xlxd module, P25 listener on the P25Reflector."""
    listener = p25_client(tg, 1)
    talker = clients.make(kind, module, 0)
    try:
        assert listener.link(p25_ip), "P25 listener link to %s failed" % p25_ip
        assert talker.link(xlxd_ip), "%s talker link to %s failed" % (kind, xlxd_ip)
        # the DMR join is an empty stream that reaches P25 as a lone
        # terminator: drain the socket before clearing
        listener.pump(0.5)
        listener.client.inbox.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(clients.talk, talker, kind, frames, "XLX999", tail)
            while not job.done():
                listener.pump(0.05)
            job.result()
        listener.pump(1.0)
        records = listener.records()
        return {"frames": [r["imbe"] for r in records if not r["terminator"]],
                "types": [r["type"] for r in records],
                "src": sorted({r["src"] for r in records if "src" in r}),
                "dst": sorted({r["dst"] for r in records if "dst" in r})}
    finally:
        try:
            listener.unlink()
        finally:
            listener.close()
            clients.close(talker)


def both(fn, setup, *args):
    """fn(xlxd_ip, p25_ip, *args) on ref and sut concurrently."""
    got = {}

    def side(name, ips):
        try:
            got[name] = fn(*ips, *args)
        except Exception as exc:            # reported by the test
            got[name] = exc

    threads = [threading.Thread(target=side, args=(n, ips)) for n, ips in setup["sides"].items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name, value in got.items():
        if isinstance(value, Exception):
            raise AssertionError("%s: %r" % (name, value))
    return got


@pytest.fixture(scope="module")
def tones(p25rig):
    rig, _ = p25rig
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))
    pcm = (pcm + [bytes(320)] * FRAMES)[:FRAMES]
    codecs = [ambed.IMBE] + [clients.CODEC[k] for k in KINDS]
    return {c: encode(rig, c, pcm) for c in codecs}


@pytest.mark.parametrize("kind", KINDS)
def test_p25_to_digital(p25rig, tones, kind):
    rig, setup = p25rig
    got = both(run_from_p25, setup, kind, setup["module"], setup["tg"], tones[ambed.IMBE])
    row = {n: {"received": len(v["frames"]), "sha256": sha(v["frames"]), "echo": v["echo"],
               "opened": v["bounds"][0], "closed": v["bounds"][1]} for n, v in got.items()}
    save("p25-%s" % kind, row)
    assert row["ref"]["received"] >= FRAMES * 0.8, "ref %s listener got %d frames" % (
        kind, row["ref"]["received"])
    for name in ("ref", "sut"):
        assert row[name]["echo"] == 0, "%s: %d records echoed to the P25 talker" % (name, row[name]["echo"])
        assert row[name]["opened"] and row[name]["closed"], "%s %s listener: stream opened %s closed %s" % (
            name, kind, row[name]["opened"], row[name]["closed"])
    assert (row["sut"]["received"], row["sut"]["sha256"]) == \
        (row["ref"]["received"], row["ref"]["sha256"]), "sut differs from ref: %s" % row


@pytest.mark.parametrize("kind", KINDS)
def test_digital_to_p25(p25rig, tones, kind):
    rig, setup = p25rig
    got = both(run_to_p25, setup, kind, setup["module"], setup["tg"], tones[clients.CODEC[kind]])
    row = {}
    for name, value in got.items():
        problems = p25.lifecycle(value["types"], whole_ldu=False)
        row[name] = {"received": len(value["frames"]), "sha256": sha(value["frames"]),
                     "records": len(value["types"]), "lifecycle": problems,
                     "whole_ldu": not p25.lifecycle(value["types"]),
                     "src": value["src"], "dst": value["dst"]}
    save("%s-p25" % kind, row)
    assert row["ref"]["received"] >= FRAMES * 0.8, "ref P25 listener got %d frames" % row["ref"]["received"]
    for name in ("ref", "sut"):
        assert not row[name]["lifecycle"], "%s lifecycle: %s" % (name, row[name]["lifecycle"])
        assert row[name]["dst"] == [setup["tg"]], "%s: destination TG %s" % (name, row[name]["dst"])
    if rig.sut_profile == specs.DEFAULT:
        keys = ("received", "sha256", "records", "whole_ldu", "src", "dst")
        assert {k: row["sut"][k] for k in keys} == {k: row["ref"][k] for k in keys}, \
            "sut differs from ref: %s" % row
        return
    # a sut that pads to whole LDUs (P-P25-24) differs from xlxd by design:
    # src/dst against the reference, the frame count and LDU shape by spec
    assert (row["sut"]["src"], row["sut"]["dst"]) == (row["ref"]["src"], row["ref"]["dst"]), row
    entry = specs.rule(rig, "test_digital_to_p25")
    if entry is None or kind not in entry.get("received", {}):
        pytest.skip("spec pending: no test_digital_to_p25 received[%s] entry in dvtest/specs/%s.py"
                    % (kind, rig.sut_profile))
    assert row["sut"]["received"] == entry["received"][kind], "sut received %d, spec %d" % (
        row["sut"]["received"], entry["received"][kind])
    assert row["sut"]["whole_ldu"] == entry["whole_ldu"], "sut whole_ldu %s, spec %s" % (
        row["sut"]["whole_ldu"], entry["whole_ldu"])


# ---------------------------------------------------------------- morse

def id_text():
    return "VVV DE N0TEST P %04d" % random.SystemRandom().randrange(10000)


def morse_cell(rig, setup, run, kind, codec_in, codec_out, label):
    """Send a Morse ID as codec_in through run (ref and sut), decode the
    codec_out frames; gated by dvtest.audio.tandem (exact where the side's
    transcoder passes the probe, informational while none does)."""
    dec = tandem.decoder(rig, transaction=0x50320000)
    probed = tandem.probe(rig, codec_in, codec_out, dec, transaction=0x50320000)
    text = id_text()
    frames = encode(rig, codec_in, tandem.encode(text))
    got = both(run, setup, kind, setup["module"], setup["tg"], frames)
    verdict, rec = tandem.judge(text, {n: dec(codec_out, v["frames"]) for n, v in got.items()}, probed)
    save(label, {"sent": text, **rec})
    print("%s: sent %r %s" % (label, text, rec))
    if verdict.startswith("xfail"):
        pytest.xfail("%s %s" % (label, verdict))
    assert verdict == "pass", "%s %s" % (label, verdict)


def test_morse_p25_to_m17(p25rig):
    rig, setup = p25rig
    morse_cell(rig, setup, run_from_p25, "m17", ambed.IMBE, ambed.CODEC2_3200, "morse-p25-m17")


def test_morse_m17_to_p25(p25rig):
    rig, setup = p25rig
    morse_cell(rig, setup, run_to_p25, "m17", ambed.CODEC2_3200, ambed.IMBE, "morse-m17-p25")


# ---------------------------------------------------------------- allstar

@pytest.fixture(scope="module")
def p25_allstar(p25rig, allstar):
    rig, setup = p25rig
    if setup["module"] not in ALLSTAR_NODES:
        pytest.skip("P25 module %s is not USRP-bridged (AllStar nodes on %s)"
                    % (setup["module"], sorted(ALLSTAR_NODES)))
    return allstar, setup


def dial(target, module):
    caller = iax2.Client(target.allstar, target.iax_secret, username="dvt")
    try:
        caller.call(ALLSTAR_NODES[module], timeout=10)
    except Exception:
        caller.socket.close()
        raise
    caller.pump(0.3)
    return caller


def tone_ok(pcm, freq=700, other=1200):
    spec = score.spectrum(pcm)
    peak = score.dominant(spec)
    ratio = score.band_power(spec, freq) / max(score.band_power(spec, other), 1.0)
    return peak, ratio, abs(peak - freq) < 80 and ratio > 3


@pytest.mark.allstar
def test_p25_to_allstar(p25_allstar):
    target, setup = p25_allstar
    imbe = encode(target, ambed.IMBE, signals.frames(signals.tone(700, 2000)))
    talker = p25_client(setup["tg"], 0)
    caller = None
    try:
        assert talker.link(target.p25_sut), "P25 talker link failed"
        caller = dial(target, setup["module"])
        caller.media.clear()
        caller.controls.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(talker.talk, imbe)
            while not job.done():
                caller.pump(0.05)
            job.result()
        caller.pump(1.0)
        media, controls = list(caller.media), list(caller.controls)
    finally:
        if caller:
            caller.close()
        talker.unlink()
        talker.close()
    voiced = b"".join(f for f in media if score.rms(f) > 200)
    peak, ratio, ok = tone_ok(voiced)
    save("p25-allstar", {"media": len(media), "voiced": len(voiced) // 320, "peak": peak,
                         "ratio": round(ratio, 1), "controls": controls})
    assert len(media) >= len(imbe) * 0.8, "IAX2 caller got %d of %d frames" % (len(media), len(imbe))
    assert ok, "returned audio peak %s Hz, 700/1200 ratio %.1f" % (peak, ratio)


@pytest.mark.allstar
def test_allstar_to_p25(p25_allstar):
    target, setup = p25_allstar
    listener = p25_client(setup["tg"], 1)
    caller = None
    try:
        assert listener.link(target.p25_sut), "P25 listener link failed"
        listener.client.inbox.clear()
        caller = dial(target, setup["module"])
        frames = [bytes(320)] * 20 + signals.frames(signals.tone(700, 2000)) + [bytes(320)] * 10
        caller.key()
        start = time.monotonic()
        for index, frame in enumerate(frames):
            while time.monotonic() < start + index * 0.02:
                caller.pump(0.001)
                listener.client.drain()
            caller.voice(frame)
        caller.pump(0.05)
        caller.unkey()
        caller.pump(0.5)
        listener.pump(2.0)
        records = listener.records()
    finally:
        if caller:
            caller.close()
        listener.unlink()
        listener.close()
    imbe = [r["imbe"] for r in records if not r["terminator"]]
    types = [r["type"] for r in records]
    pcm = decode(target, ambed.IMBE, imbe) if imbe else b""
    voiced = b"".join(f for f in signals.frames(pcm) if score.rms(f) > 200)
    peak, ratio, ok = tone_ok(voiced)
    problems = p25.lifecycle(types, whole_ldu=False)
    save("allstar-p25", {"frames": len(imbe), "lifecycle": problems, "peak": peak, "ratio": round(ratio, 1)})
    assert len(imbe) >= 100 * 0.8, "P25 listener got %d IMBE frames" % len(imbe)
    assert not problems, problems
    assert ok, "decoded audio peak %s Hz, 700/1200 ratio %.1f" % (peak, ratio)
