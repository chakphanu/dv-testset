# SPDX-License-Identifier: MIT
"""m17-fme as the independent verifier of M17 signed streams (P26j).

m17-fme (lwvmobile, GPL-3.0) is a test reference only: it runs as a separate
process in rig/Dockerfile.m17fme and nothing of it is copied here. Facts used,
read in its source at the pin M17_FME_REF:
  * input: option -^ <file>, one line per packet "NNN,<HEX>" (NNN = length, 3
    digits, 054 for a stream packet; src/io/io.c write_ip_frame_to_file and
    read_ip_frame_from_file); public key: option -k <file>, 128 hex characters
    (x||y, src/main.c case 'k');
  * output (stderr, src/decoder/m17_ipf_decoder.c and src/encryption/ecdsa.c):
      "M17 IP Stream: %04X; FN: %04X;" per stream packet, " EOT;" after the
      last, " Signature Valid;" or " Signature Invalid;" once FN 0x7FFF is
      seen, " No Public Key Loaded;", " Unknown LSF TYPE;" (a v3 TYPE: it
      rejects the new layout), " IP CRC ERR";
  * the verdict is printed only for a stream whose last frame has FN 0x7FFF,
    and it keys on the frame numbers, not on the TYPE signed bit.
Because it rejects a v3 TYPE, v3 packets are re-labelled with the legacy
TYPE (dvtest.proto.m17v3.retype_packets) before they are written; the
signature covers payloads only, so the verdict is the same.

The pure parts are unit-tested (cases/unit/test_oracle_m17.py); run_command()
and docker_argv() are the thin Docker side, not exercised before the image is
built (docs/m17v3-cases.md, "Docker steps").
"""
import argparse
import json
import re
import subprocess
import sys

from . import limits
from .proto import m17v3

SCHEMA = "dvt-m17fme-oracle/1"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
STREAM_RE = re.compile(r"M17 IP Stream: ([0-9A-Fa-f]{4}); FN: ([0-9A-Fa-f]{4});( EOT;)?")
SIG_RE = re.compile(r" Signature (Valid|Invalid);")
NO_KEY_RE = re.compile(r" No Public Key Loaded;")
UNKNOWN_TYPE_RE = re.compile(r" Unknown LSF TYPE;")
CRC_ERR_RE = re.compile(r" IP CRC ERR")


# ------------------------------------------------------------------ input files

def ipf_lines(packets):
    """Text of an IP-frame file for the packets (bytes), one line each."""
    out = []
    for pkt in packets:
        out.append("%03d,%s\n" % (len(pkt), bytes(pkt).hex().upper()))
    return "".join(out)


def pubkey_text(public_xy):
    """Public key file content: x||y as 128 hex characters."""
    if len(public_xy) != 64:
        raise ValueError("public key must be 64 bytes (x||y)")
    return bytes(public_xy).hex().upper()


def legacy_view(packets, legacy_type, meta=None):
    """The packets as a 2.0.x decoder accepts them (TYPE replaced, CRC fixed)."""
    return m17v3.retype_packets(packets, legacy_type, meta)


# ------------------------------------------------------------------ output parsing

def parse_fme_output(text):
    """List of events from m17-fme's output, in order. Each is a dict with
    "kind": "stream" (sid, fn, eot), "signature" (valid), "no_key",
    "unknown_type", "crc_error". ANSI colour codes are removed first."""
    events = []
    for raw in ANSI_RE.sub("", text).splitlines():
        # a line can hold several markers (m17-fme prints without newlines between them)
        marks = []
        for m in STREAM_RE.finditer(raw):
            marks.append((m.start(), {"kind": "stream", "sid": int(m.group(1), 16), "fn": int(m.group(2), 16),
                                      "eot": bool(m.group(3))}))
        for m in SIG_RE.finditer(raw):
            marks.append((m.start(), {"kind": "signature", "valid": m.group(1) == "Valid"}))
        for m in NO_KEY_RE.finditer(raw):
            marks.append((m.start(), {"kind": "no_key"}))
        for m in UNKNOWN_TYPE_RE.finditer(raw):
            marks.append((m.start(), {"kind": "unknown_type"}))
        for m in CRC_ERR_RE.finditer(raw):
            marks.append((m.start(), {"kind": "crc_error"}))
        events.extend(ev for _, ev in sorted(marks, key=lambda x: x[0]))
    return events


def summarize(events):
    """{"packets", "signatures": [bool...], "no_key", "unknown_type", "crc_errors", "eot_seen", "last_fn"}."""
    streams = [e for e in events if e["kind"] == "stream"]
    return {
        "packets": len(streams),
        "signatures": [e["valid"] for e in events if e["kind"] == "signature"],
        "no_key": any(e["kind"] == "no_key" for e in events),
        "unknown_type": sum(1 for e in events if e["kind"] == "unknown_type"),
        "crc_errors": sum(1 for e in events if e["kind"] == "crc_error"),
        "eot_seen": any(e["eot"] for e in streams),
        "last_fn": streams[-1]["fn"] if streams else None,
    }


def fme_verdict(summary):
    """What m17-fme says about the stream: "valid", "invalid_signature",
    "no_verdict" (no signature line: tail missing, short or not ended with
    FN 0x7FFF), "no_key", "rejected_type" (every packet had a TYPE it does not
    know) or "no_input"."""
    if summary["packets"] == 0:
        return "no_input"
    if summary["no_key"]:
        return "no_key"
    if summary["signatures"]:
        return "valid" if summary["signatures"][-1] else "invalid_signature"
    if summary["unknown_type"] and summary["unknown_type"] >= summary["packets"]:
        return "rejected_type"
    return "no_verdict"


# what m17-fme can be expected to print for each spec verdict of dvtest.proto.m17v3.verify_stream
EXPECTED_FME = {
    "valid": "valid",
    "invalid_signature": "invalid_signature",
    "incomplete": "no_verdict",
    "unsigned": "no_verdict",
    # malformed covers several things: some end with FN 0x7FFF (m17-fme then prints a verdict from the
    # digest it built), some do not (no verdict); compare() does not judge them
    "malformed": ("no_verdict", "invalid_signature", "valid"),
}


def _fme_set(spec_verdict):
    want = EXPECTED_FME[spec_verdict]
    return set(want) if isinstance(want, tuple) else {want}


def compare(entry, summary):
    """{"id", "spec_verdict", "fme", "agrees", "note"} for one signed_streams.json
    entry and the summary of m17-fme's run on it. agrees is None where the two
    are not meant to match (a malformed stream is judged by the spec rule; the
    unsigned case, because m17-fme keys on FN only)."""
    spec = entry["expect"]["verdict"]
    got = fme_verdict(summary)
    wanted = _fme_set(spec)
    for other in entry["expect"].get("also_acceptable", []):
        wanted |= _fme_set(other)
    note = entry.get("note")
    agrees = got in wanted
    if spec == "unsigned":
        agrees, note = None, "m17-fme keys on FN only, so a verdict may appear for a stream whose signed bit is clear"
    elif spec == "malformed":
        agrees, note = None, "malformed streams are judged by the spec rule, not by m17-fme"
    return {"id": entry["id"], "spec_verdict": spec, "fme": got, "agrees": agrees, "note": note}


# ------------------------------------------------------------------ thin Docker side

def docker_argv(image, workdir, ipf_name, key_name, env=None):
    """argv of one verification run: no network, read-only root, the two files
    mounted read-only, the standard limits of dvtest.limits."""
    return (["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:size=16m"]
            + limits.docker_run_args(env)
            + ["-v", "%s:/data:ro" % workdir, image, "verify", "/data/" + ipf_name, "/data/" + key_name])


def run_command(argv, *, timeout=60):
    """Run one command without a shell: {"argv", "returncode", "timed_out", "stdout"}."""
    try:
        done = subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"argv": list(argv), "returncode": None, "timed_out": True,
                "stdout": (exc.stdout or b"").decode("utf-8", "replace")}
    return {"argv": list(argv), "returncode": done.returncode, "timed_out": False,
            "stdout": done.stdout.decode("utf-8", "replace")}


def verify_entry(entry, public_xy, image, workdir, run=run_command):
    """Write the entry's packets (legacy-labelled) and the key into workdir, run
    the container once, return compare()'s dict plus the summary. Entries with
    a recipe instead of packets are skipped (None)."""
    if entry.get("packets") is None:
        return None
    packets = legacy_view([bytes.fromhex(p) for p in entry["packets"]], int(entry["legacy_type"], 16),
                          bytes(14))
    with open("%s/stream.ipf" % workdir, "w", encoding="ascii") as handle:
        handle.write(ipf_lines(packets))
    with open("%s/pub.txt" % workdir, "w", encoding="ascii") as handle:
        handle.write(pubkey_text(public_xy))
    result = run(docker_argv(image, workdir, "stream.ipf", "pub.txt"))
    summary = summarize(parse_fme_output(result["stdout"]))
    out = compare(entry, summary)
    out["summary"] = summary
    out["returncode"] = result["returncode"]
    return out


def main(argv=None):
    """python3 -m dvtest.oracle_m17 --image dvt-m17fme:local [--vectors DIR] [--out FILE]
    Runs every signed-stream vector with packets through the container and writes
    the comparison as JSON (the oracle_verdict values of the vector files are
    filled from it by hand, not by this tool)."""
    import os
    import tempfile
    parser = argparse.ArgumentParser(description=main.__doc__.split("\n")[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--vectors", default="vectors/m17v3")
    parser.add_argument("--out", default="-")
    parser.add_argument("--verdicts-out", help="write {entry id: m17-fme verdict} for "
                        "vectors/m17v3/oracle_verdicts.json (then run scripts/gen-m17v3-vectors.py)")
    args = parser.parse_args(argv)
    with open(os.path.join(args.vectors, "signed_streams.json"), encoding="ascii") as handle:
        entries = json.load(handle)["entries"]
    with open(os.path.join(args.vectors, "keys.json"), encoding="ascii") as handle:
        keys = {e["id"][4:]: bytes.fromhex(e["public_xy"]) for e in json.load(handle)["entries"]}
    results = []
    for entry in entries:
        with tempfile.TemporaryDirectory(prefix="dvt-m17fme-") as work:
            res = verify_entry(entry, keys[entry["verify_key"]], args.image, work)
        if res is not None:
            results.append(res)
    doc = {"schema": SCHEMA, "image": args.image, "results": results,
           "disagreements": [r["id"] for r in results if r["agrees"] is False]}
    if args.verdicts_out:
        with open(args.verdicts_out, "w", encoding="ascii") as handle:
            json.dump({r["id"]: r["fme"] for r in results}, handle, indent=1, sort_keys=True)
            handle.write("\n")
    text = json.dumps(doc, indent=1) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
    else:
        with open(args.out, "w", encoding="ascii") as handle:
            handle.write(text)
    return 1 if doc["disagreements"] else 0


if __name__ == "__main__":
    sys.exit(main())
