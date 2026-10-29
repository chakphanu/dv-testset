# SPDX-License-Identifier: MIT
"""NXDN through an NXDNReflector peered with xlxd (rig profile nxdn).

xlxd speaks NXDN only as a peer of an NXDNReflector: nxdn-ref is peered
with xlxd-ref and nxdn-sut with xlxd-sut, both on talkgroup DVT_NXDN_TG
and xlxd module DVT_NXDN_MODULE. NXDN clients link to the reflector the
way NXDNGateway does; every other client links to xlxd on that module.

Passthrough NXDN <-> DMR: both carry AMBE+2, NXDN as the 49 data bits,
DMR with FEC (dvtest.proto.nxdn.to_dmr/from_dmr), so xlxd does not
transcode. It does change one field: a fixed gain step in both
directions (xlxd defines "-3" for NXDN->DMR and DMR->NXDN and applies
-3/10 = -0.3 dB) rewrites the 5-bit gain index b2 of every frame to the
gain-table entry nearest to its old gain minus 0.3 dB, which is one index
lower (b2 = 0 stays 0), and re-encodes the FEC. So the bytes differ in
almost every frame, the other 44 data bits are untouched. The cases
assert exactly that: all bits outside b2 equal, b2 one step down, DMR
FEC valid, ref and sut identical. 60 frames keep whole NXDN packets (xlxd
neither gain-steps nor pads a partial last packet the same way).

Cross cells (NXDN <-> D-STAR, M17) are differential like the matrix:
tone bursts encoded by transcoder-ref, xlxd-sut must deliver what
xlxd-ref delivers. Morse ID text keyed through NXDN must decode exactly
after D-STAR (decoded by transcoder-ref) and M17 (c2dec), and back, on
each side whose transcoder passes a probe of the same tandem; while no
side passes, the Morse cell is xfail (informational).

With --profile nxdn,allstar and DVT_NXDN_MODULE on a USRP-bridged module
(A or B), NXDN <-> AllStar is checked through IAX2 on xlxd-sut.

Results: $DVT_OUT/nxdn.json.
"""
import collections
import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dvtest import clients
from dvtest.audio import morse, score, signals, tandem
from dvtest.proto import ambed, iax2, nxdn

pytestmark = [pytest.mark.rig, pytest.mark.nxdn]

FRAMES = 60                                 # multiple of 3 (DMR) and 4 (NXDN)
NXDN_IDS = (1001, 1002)                     # N0CALL, N0TEST in rig/config/nxdn.csv
CROSS = [("nxdn", "dstar"), ("nxdn", "m17"), ("dstar", "nxdn"), ("m17", "nxdn")]
ALLSTAR_NODES = {"A": "1901", "B": "1902"}  # USRP bridge of xlxd-sut (profile allstar)
WPM = 20
MORSE_TRANSACTION = 0x4E580000             # 'NX'; tandem adds 0x100/0x200/0x300
RESULTS = {}


def sha(frames):
    return hashlib.sha256(b"".join(frames)).hexdigest()


class Side:
    def __init__(self, name, xlxd, reflector, tg, module):
        self.name, self.xlxd, self.reflector, self.tg, self.module = name, xlxd, reflector, tg, module

    def make(self, kind, index):
        """Linked client of kind; NXDN on the reflector, others on xlxd."""
        if kind == "nxdn":
            client = nxdn.NxdnClient("0.0.0.0", self.tg, nxdn_id=NXDN_IDS[index],
                                     callsign=clients.CALLSIGNS[index])
            ok = client.link(self.reflector)
        else:
            client = clients.make(kind, self.module, index)
            ok = client.link(self.xlxd)
        if not ok:
            client.close()
            raise AssertionError("%s %s link failed" % (self.name, kind))
        return client


def talk(client, kind, frames):
    if kind == "nxdn":
        client.talk(frames)
    else:
        clients.talk(client, kind, frames)


def run_cell(side, src, dst, frames):
    """Talker src and listener dst on the NXDN module of one side. Returns
    the listener's frames, its parsed NXDN packets (dst nxdn) and what came
    back to an NXDN talker."""
    listener = side.make(dst, 1)
    talker = None
    try:
        talker = side.make(src, 0)
        listener.client.drain()
        listener.client.inbox.clear()
        talker.client.drain()
        talker.client.inbox.clear()
        talk(talker, src, frames)
        got = listener.collect()
        packets = listener.packets() if dst == "nxdn" else []
        echo = talker.packets() if src == "nxdn" else []
        return {"frames": got, "packets": packets, "echo": echo}
    finally:
        clients.close(*[c for c in (talker, listener) if c])


def both(nxdn_rig, src, dst, frames):
    """run_cell on ref and sut concurrently: {name: result or exception}."""
    got = {}

    def one(side):
        try:
            got[side.name] = run_cell(side, src, dst, frames)
        except Exception as exc:                    # reported by the case
            got[side.name] = exc

    threads = [threading.Thread(target=one, args=(s,)) for s in nxdn_rig["sides"]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for name, value in got.items():
        if isinstance(value, Exception):
            raise AssertionError("%s: %r" % (name, value))
    return got


def record(key, value):
    RESULTS[key] = value
    out = Path(os.environ.get("DVT_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "nxdn.json").write_text(json.dumps(RESULTS, indent=1, sort_keys=True) + "\n")


def _probe(side):
    """True when a short DMR talk on the module reaches an NXDN listener
    (xlxd has peered with the reflector)."""
    try:
        result = run_cell(side, "dmr", "nxdn", [nxdn.to_dmr(bytes(7))] * 12)
    except AssertionError:
        return False
    return any(p["kind"] == "voice" for p in result["packets"])


@pytest.fixture(scope="module")
def nxdn_rig(rig):
    """Both NXDNReflectors answer a poll and carry xlxd traffic."""
    if os.environ.get("DVT_NXDN") != "on":
        pytest.skip("no NXDNReflector: run.sh --profile nxdn (DVT_NXDN=on)")
    try:
        tg = int(os.environ["DVT_NXDN_TG"])
        module = os.environ["DVT_NXDN_MODULE"]
        addrs = {"ref": os.environ["DVT_NXDN_REF"], "sut": os.environ["DVT_NXDN_SUT"]}
    except (KeyError, ValueError) as exc:
        pytest.skip("profile nxdn variables missing: %r" % exc)
    sides = [Side("ref", rig.xlxd_ref, addrs["ref"], tg, module),
             Side("sut", rig.xlxd_sut, addrs["sut"], tg, module)]
    for side in sides:
        deadline = time.monotonic() + 60
        while not _probe(side):
            if time.monotonic() > deadline:
                pytest.fail("%s: xlxd %s module %s does not reach NXDNReflector %s TG %d"
                            % (side.name, side.xlxd, module, side.reflector, tg))
            time.sleep(2)
    return {"rig": rig, "sides": sides, "tg": tg, "module": module}


@pytest.fixture(scope="module")
def frames49():
    """Deterministic 49-bit frames (data bits of seeded PRNG DMR frames)."""
    return [nxdn.from_dmr(f) for f in signals.prng_frames("dmr", FRAMES, seed="nxdn")]


def gain_report(sent, got):
    """Compare 49-bit frames: other bits, b2 steps, one-step-down rule."""
    steps = collections.Counter(nxdn.gain_index(g) - nxdn.gain_index(s) for s, g in zip(sent, got))
    return {
        "sent": len(sent), "received": len(got),
        "other_bits_differ": sum(nxdn.without_gain(s) != nxdn.without_gain(g) for s, g in zip(sent, got)),
        "frames_changed": sum(s != g for s, g in zip(sent, got)),
        "b2_steps": {str(k): v for k, v in sorted(steps.items())},
        "one_step_down": all(nxdn.gain_index(g) == max(nxdn.gain_index(s) - 1, 0) for s, g in zip(sent, got)),
    }


def test_nxdn_to_dmr_passthrough(nxdn_rig, frames49):
    got = both(nxdn_rig, "nxdn", "dmr", frames49)
    rows = {}
    for name, result in got.items():
        dmr9 = result["frames"]
        row = gain_report(frames49, [nxdn.from_dmr(f) for f in dmr9])
        row["dmr_fec_valid"] = sum(nxdn.dmr_valid(f) for f in dmr9)
        row["sha256"] = sha(dmr9)
        row["echo_to_talker"] = len(result["echo"])
        rows[name] = row
    record("nxdn-dmr", rows)
    for name, row in rows.items():
        assert row["received"] == FRAMES, "%s: DMR listener got %d of %d" % (name, row["received"], FRAMES)
        assert row["dmr_fec_valid"] == FRAMES, "%s: %d frames with broken FEC" % (name, FRAMES - row["dmr_fec_valid"])
        assert row["other_bits_differ"] == 0, "%s: %d frames differ outside the gain bits" % (
            name, row["other_bits_differ"])
        assert row["one_step_down"], "%s: gain steps %s" % (name, row["b2_steps"])
        assert row["echo_to_talker"] == 0, "%s: %d NXDN packets came back to the talker" % (
            name, row["echo_to_talker"])
    assert rows["sut"]["sha256"] == rows["ref"]["sha256"], "sut differs from ref"


def test_dmr_to_nxdn_passthrough_and_lifecycle(nxdn_rig, frames49):
    got = both(nxdn_rig, "dmr", "nxdn", [nxdn.to_dmr(f) for f in frames49])
    rows = {}
    for name, result in got.items():
        row = gain_report(frames49, result["frames"])
        packets = result["packets"]
        row["sha256"] = sha(result["frames"])
        row["kinds"] = [p["kind"] for p in packets]
        row["src_ids"] = sorted({p["src"] for p in packets})
        row["dst_ids"] = sorted({p["dst"] for p in packets})
        heads = [nxdn.parse_facch1(p["payload"][5:17]) for p in packets if p["kind"] != "voice"]
        row["facch1"] = [(h["msg"], h["crc_ok"], h["group"]) for h in heads]
        sacch = [nxdn.parse_sacch(p["payload"][1:5]) for p in packets if p["kind"] == "voice"]
        row["sacch_structures"] = [s["structure"] for s in sacch]
        row["sacch_crc_ok"] = all(s["crc_ok"] for s in sacch)
        row["lich"] = sorted({p["payload"][0] for p in packets})
        rows[name] = row
    record("dmr-nxdn", rows)
    voice = FRAMES // nxdn.FRAMES_PER_PACKET
    for name, row in rows.items():
        assert row["received"] == FRAMES, "%s: NXDN listener got %d of %d" % (name, row["received"], FRAMES)
        assert row["other_bits_differ"] == 0, "%s: %d frames differ outside the gain bits" % (
            name, row["other_bits_differ"])
        assert row["one_step_down"], "%s: gain steps %s" % (name, row["b2_steps"])
        assert row["kinds"] == ["header"] + ["voice"] * voice + ["trailer"], "%s: %s" % (name, row["kinds"])
        assert row["dst_ids"] == [nxdn_rig["tg"]] and len(row["src_ids"]) == 1, row
        assert row["facch1"] == [(nxdn.VCALL, True, True), (nxdn.TX_REL, True, True)], row["facch1"]
        assert row["sacch_crc_ok"] and row["sacch_structures"] == [3, 2, 1, 0] * (voice // 4) + \
            [3, 2, 1, 0][:voice % 4], row["sacch_structures"]
    assert rows["sut"]["sha256"] == rows["ref"]["sha256"], "sut differs from ref"


@pytest.fixture(scope="module")
def encoded(nxdn_rig):
    """Tone bursts encoded once by transcoder-ref: {kind: frames}; NXDN
    gets the 49 data bits of the AMBE+2 frames."""
    rig = nxdn_rig["rig"]
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    out = ambed.transcode(rig.ambed_ref, ambed.PCM, [clients.CODEC[k] for k in clients.KINDS], pcm,
                          port=rig.ambed_port)
    frames = {k: out[clients.CODEC[k]] for k in clients.KINDS}
    frames["nxdn"] = [nxdn.from_dmr(f) for f in frames["dmr"]]
    return frames


@pytest.mark.parametrize("cell", CROSS, ids=["%s-%s" % c for c in CROSS])
def test_cross_sut_equals_ref(nxdn_rig, encoded, cell):
    src, dst = cell
    got = both(nxdn_rig, src, dst, encoded[src])
    rows = {name: {"received": len(r["frames"]), "sha256": sha(r["frames"]), "echo_to_talker": len(r["echo"])}
            for name, r in got.items()}
    record("%s-%s" % cell, rows)
    assert rows["ref"]["received"] >= FRAMES * 0.8, "ref got %d frames" % rows["ref"]["received"]
    assert (rows["sut"]["received"], rows["sut"]["sha256"]) == (rows["ref"]["received"], rows["ref"]["sha256"]), \
        "sut differs from ref: %s" % rows
    assert rows["sut"]["echo_to_talker"] == 0 and rows["ref"]["echo_to_talker"] == 0, rows


# ------------------------------------------------------------------ Morse

def codec_of(kind):
    """AMBEd codec of a kind's frames on xlxd (NXDN rides as AMBE+2)."""
    return ambed.AMBE2PLUS if kind == "nxdn" else clients.CODEC[kind]


def as_codec(kind, frames):
    """Received frames in the codec form AMBEd takes (NXDN 49-bit -> DMR)."""
    return [nxdn.to_dmr(f) for f in frames] if kind == "nxdn" else frames


@pytest.fixture(scope="module")
def morse_id(nxdn_rig):
    """(text, {kind: frames}) of one Morse ID, encoded by transcoder-ref."""
    rig = nxdn_rig["rig"]
    text = "N0TEST NXDN %04d" % random.SystemRandom().randrange(10000)
    pcm = signals.frames(morse.encode(text, wpm=WPM))
    pcm += [bytes(320)] * (-len(pcm) % 12)
    out = ambed.transcode(rig.ambed_ref, ambed.PCM, [clients.CODEC[k] for k in clients.KINDS], pcm,
                          port=rig.ambed_port, transaction=MORSE_TRANSACTION)
    frames = {k: out[clients.CODEC[k]] for k in clients.KINDS}
    frames["nxdn"] = [nxdn.from_dmr(f) for f in frames["dmr"]]
    return text, frames


@pytest.mark.parametrize("cell", CROSS, ids=["%s-%s" % c for c in CROSS])
def test_morse_round_trip(nxdn_rig, morse_id, cell):
    """Morse keyed by the talker decodes exactly at the listener, on every
    side whose transcoder passes the tandem probe of this conversion.
    While no side passes, the result is informational (xfail naming the
    probe); it becomes a real assert once a transcoder is fixed."""
    src, dst = cell
    label = "morse %s-%s" % cell
    text, frames = morse_id
    rig = nxdn_rig["rig"]
    decode = tandem.decoder(rig, transaction=MORSE_TRANSACTION)
    probed = tandem.probe(rig, codec_of(src), codec_of(dst), decode, transaction=MORSE_TRANSACTION)
    got = both(nxdn_rig, src, dst, frames[src])
    verdict, rec = tandem.judge(text, {n: decode(codec_of(dst), as_codec(dst, r["frames"]))
                                       for n, r in got.items()}, probed)
    for name in tandem.SIDES:
        rec[name]["frames"] = len(got[name]["frames"])
    record(label, {"sent": text, **rec})
    if verdict.startswith("xfail"):
        pytest.xfail("%s %s" % (label, verdict))
    assert verdict == "pass", "%s %s" % (label, verdict)


# ------------------------------------------------------------------ AllStar

@pytest.fixture(scope="module")
def node(nxdn_rig, allstar):
    """The AllStar node of the NXDN module answers IAX2 (profile
    nxdn,allstar with DVT_NXDN_MODULE on a bridged module)."""
    module = nxdn_rig["module"]
    if module not in ALLSTAR_NODES:
        pytest.skip("NXDN module %s has no AllStar node (set DVT_NXDN_MODULE=A or B)" % module)
    deadline = time.monotonic() + 90
    while True:
        try:
            dial(allstar, module).close()
            return allstar
        except (TimeoutError, ConnectionError, OSError):
            if time.monotonic() > deadline:
                pytest.fail("AllStar node %s does not answer IAX2 calls" % allstar.allstar)
            time.sleep(3)


def dial(target, module, timeout=5):
    caller = iax2.Client(target.allstar, target.iax_secret, username="dvt")
    try:
        caller.call(ALLSTAR_NODES[module], timeout=timeout)
    except Exception:
        caller.socket.close()
        raise
    caller.pump(0.3)
    return caller


def tone_peak(pcm, freq, other=1200):
    spec = score.spectrum(pcm)
    return score.dominant(spec), score.band_power(spec, freq) / max(score.band_power(spec, other), 1.0)


def test_nxdn_to_allstar(nxdn_rig, node):
    side = nxdn_rig["sides"][1]
    pcm = signals.frames(signals.tone(700, 2000))
    frames = [nxdn.from_dmr(f) for f in ambed.transcode(node.ambed_ref, ambed.PCM, [ambed.AMBE2PLUS], pcm,
                                                        port=node.ambed_port)[ambed.AMBE2PLUS]]
    talker = side.make("nxdn", 0)
    caller = None
    try:
        caller = dial(node, side.module)
        caller.media.clear()
        caller.controls.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(talker.talk, frames)
            while not job.done():
                caller.pump(0.05)
            job.result()
        caller.pump(1.0)
        media, controls = list(caller.media), list(caller.controls)
    finally:
        if caller:
            caller.close()
        clients.close(talker)
    voiced = b"".join(f for f in media if score.rms(f) > 200)
    peak, ratio = tone_peak(voiced, 700)
    record("nxdn-allstar", {"media": len(media), "sent": len(frames), "peak_hz": peak, "ratio": round(ratio, 1)})
    assert len(media) >= len(frames), "IAX2 caller got %d of %d frames" % (len(media), len(frames))
    assert abs(peak - 700) < 80 and ratio > 3, "peak %s Hz, 700/1200 ratio %.1f" % (peak, ratio)
    assert 12 in controls and 13 in controls[controls.index(12):], "no key then unkey: %s" % controls


def test_allstar_to_nxdn(nxdn_rig, node):
    side = nxdn_rig["sides"][1]
    listener = side.make("nxdn", 1)
    caller = None
    sent = [bytes(320)] * 20 + signals.frames(signals.tone(700, 2000)) + [bytes(320)] * 10
    try:
        caller = dial(node, side.module)
        listener.client.inbox.clear()
        caller.key()
        start = time.monotonic()
        for index, frame in enumerate(sent):
            caller.pump(max(0.0, start + index * 0.02 - time.monotonic()))
            listener.client.drain()
            caller.voice(frame)
        caller.pump(0.05)
        caller.unkey()
        caller.pump(1.0)
        got = listener.collect(settle=1.0)
        kinds = [p["kind"] for p in listener.packets()]
    finally:
        if caller:
            caller.close()
        clients.close(listener)
    pcm = b"".join(ambed.transcode(node.ambed_ref, ambed.AMBE2PLUS, [ambed.PCM], [nxdn.to_dmr(f) for f in got],
                                   port=node.ambed_port)[ambed.PCM]) if got else b""
    voiced = b"".join(pcm[i:i + 320] for i in range(0, len(pcm), 320) if score.rms(pcm[i:i + 320]) > 200)
    peak, ratio = tone_peak(voiced, 700) if voiced else (0, 0.0)
    record("allstar-nxdn", {"frames": len(got), "kinds": collections.Counter(kinds), "peak_hz": peak,
                            "ratio": round(ratio, 1)})
    assert len(got) >= 100, "NXDN listener got %d frames for 2 s of tone" % len(got)
    assert kinds[:1] == ["header"] and kinds[-1:] == ["trailer"], collections.Counter(kinds)
    assert abs(peak - 700) < 80 and ratio > 3, "peak %s Hz, 700/1200 ratio %.1f" % (peak, ratio)
