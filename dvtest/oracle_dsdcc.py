# SPDX-License-Identifier: MIT
"""DSDcc as a third D-STAR slow data decoder, one that is not of the G4KLX family (HWFREE1 item 4,
stage 2, docs/dstar-slowdata-cases.md).

DSDcc (f4exb/dsdcc, GPL-3.0-or-later) is built unmodified into the image dvt-dsdcc:local
(rig/Dockerfile.dsdcc); a small driver of ours (rig/dsdcc/oracle-dsdcc.cpp) links its shared library through
the public headers, reads S16LE 48 kS/s discriminator samples from stdin and prints every change of the
20 character info text, the locator and the set of non-empty header strings. Nothing of DSDcc's code or
text is in this repository.

Two uses:
  * the two published recordings of its samples/ folder (run_recording): a summary of what the decoder
    shows, WITHOUT callsigns, names or locators (they are real personal data and are never printed or stored);
  * our own corpus (vectors/dstar_sd/corpus.json frames): modulate() turns the 21-frame superframes into
    a headerless 4800 baud two-level stream, run_case() feeds it to the driver, parse_states() reads the
    info text and locator changes. Cases with a missing frame cannot be modulated (an RF frame is never
    missing, it is garbled): they are skipped.

    python3 -m dvtest.oracle_dsdcc run --out vectors/dstar_sd/dsdcc.json
    python3 -m dvtest.oracle_dsdcc recording .cache/dsdcc/samples/dstar_f1zil_1.dis
"""
import argparse
import json
import os
import struct
import subprocess
import sys

from . import dstar_sd as sd
from . import limits

IMAGE = "dvt-dsdcc:local"
SCHEMA = "dvt-dstar-sd-dsdcc/1"
RUN_TIMEOUT = 300
SPS = 10                                # samples per symbol: 48 kS/s, 4800 baud
FRAME_SYMBOLS = 96                      # 72 voice bits + 24 slow data bits
AMPLITUDE = 6000
LEAD_IN_SYMBOLS = 400                   # alternating bits before the first frame
TAIL_SYMBOLS = 30                       # after the last frame (less than the 72 symbols that mean "lost")


def _bits_lsb_first(data):
    for byte in bytes(data):
        for k in range(8):
            yield (byte >> k) & 1


def frame_bits(wire_data, voice=sd.NULL_AMBE):
    """The 96 bits of one voice frame on the air: 72 voice bits and the 3 slow data bytes (the wire
    bytes of the corpus: frame 0 the sync pattern, the others scrambled), each byte LSB first."""
    return list(_bits_lsb_first(voice)) + list(_bits_lsb_first(wire_data))


def stream_bits(frames, lead_in=LEAD_IN_SYMBOLS, tail=TAIL_SYMBOLS):
    """Bits of an over: alternating lead-in, the frames in order, one more sync frame (so that the last
    superframe is not cut by the "lost" rule), a short alternating tail."""
    bits = [(i + 1) & 1 for i in range(lead_in)]
    for _seq, wire in frames:
        bits += frame_bits(wire)
    bits += frame_bits(sd.SYNC)
    bits += [(i + 1) & 1 for i in range(tail)]
    return bits


def modulate(frames, amplitude=AMPLITUDE, sps=SPS, smooth=3, **kw):
    """S16LE bytes of the two-level stream: bit 1 = +amplitude, bit 0 = -amplitude, `sps` samples per
    bit, a moving average over `smooth` samples to take the corners off (an ideal rectangle is not what a
    discriminator delivers)."""
    bits = stream_bits(frames, **kw)
    raw = [amplitude if b else -amplitude for b in bits for _ in range(sps)]
    if smooth > 1:
        out = []
        for i in range(len(raw)):
            window = raw[max(0, i - smooth + 1):i + 1]
            out.append(sum(window) // len(window))
        raw = out
    return struct.pack("<%dh" % len(raw), *raw)


def docker_argv(args, image=IMAGE, env=None):
    return (["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=16m"]
            + limits.docker_run_args(env) + [image] + list(args))


def run_dsdcc(samples, image=IMAGE):
    """Feed S16LE bytes to the driver (DSDcc, D-STAR only) and return its stdout text."""
    proc = subprocess.run(docker_argv([], image), input=samples, capture_output=True, timeout=RUN_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError("dsdcc driver exited %d: %s" % (proc.returncode, proc.stderr[-300:].decode("latin-1")))
    return proc.stdout.decode("latin-1")


def parse_states(text):
    """[{"sample", "text", "locator", "header_flags"}] from the driver output ('R state <sample> <text hex>
    <locator hex or -> <flags>'); the text is the 20 characters DSDcc shows (it maps bytes outside 32..127
    to '.' itself), flags: 1 MY, 2 UR, 4 RPT1, 8 RPT2 non-empty, 16 every non-empty header string printable,
    32 the locator has the shape AA00AA. Other lines are ignored."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 6 and parts[0] == "R" and parts[1] == "state":
            out.append({"sample": int(parts[2]), "text": bytes.fromhex(parts[3]).decode("latin-1"),
                        "locator": "" if parts[4] == "-" else bytes.fromhex(parts[4]).decode("latin-1"),
                        "header_flags": int(parts[5])})
    return out


def frame_of(sample, lead_in=LEAD_IN_SYMBOLS):
    """Index of the voice frame (0 = the first frame of the over) a sample position falls in."""
    return max(0, (sample - lead_in * SPS) // (FRAME_SYMBOLS * SPS))


def states(parsed, lead_in=LEAD_IN_SYMBOLS):
    """The driver's changes as {"frame", "text", "locator", "header_flags"}, in order."""
    return [{"frame": frame_of(p["sample"], lead_in), "text": p["text"], "locator": p["locator"],
             "header_flags": p["header_flags"]} for p in parsed]


def modulable(frames):
    """True when every superframe of the case has all 21 frames (a hole cannot exist on the air)."""
    return all(sorted(u) == list(range(sd.FRAMES)) for u in sd.split_units(frames))


def run_case(frames, image=IMAGE):
    """States of one corpus case as seen by DSDcc."""
    return states(parse_states(run_dsdcc(modulate(frames), image=image)))


# ------------------------------------------------------------------ the published recordings

def summarize_recording(parsed):
    """Counts of what a recording shows, WITHOUT any content: number of state changes, how many have a
    header (all four strings non-empty) and which header flag sets occur, the printable text states and
    the longest, whether a locator of the right shape appears. No callsign, name or locator leaves here."""
    printable = lambda t: all(32 <= ord(c) < 127 for c in t)
    texts = []
    for p in parsed:
        if p["text"].strip() and p["text"].strip(" ._") and (not texts or texts[-1] != p["text"]):
            texts.append(p["text"])
    locs = {p["locator"] for p in parsed if p["locator"]}
    return {
        "state_changes": len(parsed),
        "header_complete": any(p["header_flags"] & 15 == 15 for p in parsed),
        "header_complete_printable": any(p["header_flags"] & 31 == 31 for p in parsed),
        "header_garbled": any(p["header_flags"] & 15 and not p["header_flags"] & 16 for p in parsed),
        "text_states": len(texts), "text_states_printable": sum(1 for t in texts if printable(t)),
        "text_longest_chars": max([len(t.strip()) for t in texts if printable(t)] or [0]),
        "locator_states": len(locs), "locator_valid_shape": any(p["header_flags"] & 32 for p in parsed),
    }


def run_recording(path, image=IMAGE):
    with open(path, "rb") as fh:
        data = fh.read()
    return summarize_recording(parse_states(run_dsdcc(data, image=image)))


# ------------------------------------------------------------------ comparison with the corpus

def text_view(rec, st):
    """How one case looks to DSDcc against the two oracle results of the corpus record `rec`: the 20
    character states it showed, and whether each text the DV-Router expectation (MMDVMHost) and the
    lenient result (DStarGateway) names was shown at some time."""
    shown = [s["text"] for s in st]
    want = [bytes.fromhex(e[2]).decode("latin-1") for e in rec["expected_dvrouter"]["text"]]
    lenient = [bytes.fromhex(e[2]).decode("latin-1") for e in rec["lenient_alternative"] if e[0] == "text"]
    return {
        "expected_texts_shown": [t in shown for t in want],
        "lenient_texts_shown": [t in shown for t in lenient],
        "final_text": st[-1]["text"] if st else None,
    }


def build(corpus_path, image=IMAGE):
    with open(corpus_path) as fh:
        doc = json.load(fh)
    out = []
    for rec in doc["cases"]:
        frames = [(int(f.split()[0]), bytes.fromhex(f.split()[1])) for f in rec["frames"]]
        if not modulable(frames):
            out.append({"case": rec["case"], "skipped": "a frame is missing: no RF equivalent"})
            continue
        st = run_case(frames, image)
        row = {"case": rec["case"], "states": st}
        row.update(text_view(rec, st))
        row["locator_valid_shape_shown"] = any(s["header_flags"] & 32 for s in st)
        row["header_decoded"] = any(s["header_flags"] & 15 == 15 for s in st)
        out.append(row)
    return out


def pins(path=None):
    """The DSDCC_* settings of .env.example (never hard-coded here)."""
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")
    out = {}
    with open(path) as fh:
        for line in fh:
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("DSDCC_"):
                out[key] = value
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["run", "recording"])
    ap.add_argument("path", nargs="?", default=None)
    ap.add_argument("--corpus", default="vectors/dstar_sd/corpus.json")
    ap.add_argument("--out", default="vectors/dstar_sd/dsdcc.json")
    args = ap.parse_args(argv)
    if args.action == "recording":
        print(json.dumps(run_recording(args.path), indent=1))
        return 0
    rows = build(args.corpus)
    doc = {"schema": SCHEMA, "image": IMAGE, "pins": pins(), "cases": rows}
    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    print("cases %d, skipped %d" % (len(rows), sum(1 for r in rows if "skipped" in r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
