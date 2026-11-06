# SPDX-License-Identifier: MIT
"""dvt-lora-gw: a scripted, hardware-free source of raw TNC2 lines for the LoRa APRS gateway path
(HWFREE1 item 3, DP11d; docs/lora-gw-cases.md).

Pure Python, stdlib only, no hardware, no container. A LoRa APRS iGate puts TNC2 text lines on a
TCP port (or a serial line). On the air each line is preceded by the three bytes 3C FF 01
(`<`, 0xFF, 0x01); the plan says the common firmware STRIPS them before the host link and ends
every line with CRLF, while a device that forwards the raw radio payload would keep them. This
module generates, byte for byte, the streams a gateway under test (DV-Router's raw-tcp /
raw-serial adapter with framing `tnc2`) has to cope with:

  * lines with and without the 3C FF 01 prefix, CRLF endings (and LF-only / CR-only),
  * bad and partial prefixes, prefix only, a doubled prefix,
  * empty lines, garbage lines that are not TNC2, lines at and over the size limits,
  * a line split over two or more reads, a mid-line connect, a line with no terminator,

each in two PROFILES: the gateway is configured with an empty prefix (`none`) or with the
prefix `3cff01`. For every scenario and profile the vector says which lines MUST be delivered
(`accept`, in order, after the prefix is removed), which MUST NOT (`reject`, as received) and
which are not decided by anything we know (`unspecified`: a test must not assert on them).

EVERY expectation here is UNVERIFIED: they are ours, derived from the plan's account of the
firmware (it strips the prefix and sends CRLF), from the DV-Router configuration document as the
plan quotes it ("Raw tnc2 uses CR/LF delimited text frames", "Receive requires an exact prefix
and removes it") and from the TNC2 line format; neither the firmware sources nor the DV-Router
document were read in this stage. `classify_stream` is a restatement of those expectations, not
an implementation of any gateway; the scenario tables state their expectations independently and
the unit tests check the two against each other.

    python3 -m dvtest.lora_gw write [--out-dir vectors/lora_gw]
    python3 -m dvtest.lora_gw list
    python3 -m dvtest.lora_gw serve --scenario split-mid-line --profile 3cff01 --port 8001
"""
import argparse
import json
import os
import re
import socket
import sys
import time

from . import airtime

PREFIX = b"\x3c\xff\x01"
CRLF = b"\r\n"
PROFILES = {"none": b"", "3cff01": PREFIX}

# size classes of one raw line (as received between terminators, prefix included), in bytes
LORA_PAYLOAD_MAX = 255          # an SX127x FIFO / LoRa payload holds 255 bytes: prefix + line (UNVERIFIED, general knowledge)
REJECT_OVER = 510               # the plan: "overlong line > 510 B" (UNVERIFIED)

# the three synthetic lines of the plan (research note 2b.4), the same as dvtest.airtime.LORA_APRS_LINES
STATUS, POSITION, IGATE = [ln.encode("ascii") for ln in airtime.LORA_APRS_LINES]
HEAD = b"N0CALL-7>APLRT1:"

BASIS = {
    "plan-firmware": "the plan says the iGate firmware strips the prefix and ends lines with CRLF (not re-read)",
    "plan-config": "the DV-Router configuration document as the plan quotes it: CR/LF delimited, exact prefix required and removed (not re-read)",
    "convention": "the TNC2 monitor line format SRC>DEST[,VIA...]:info (ours)",
    "invariant": "a stream parser must give the same result however the bytes are split into reads (ours)",
    "size": "LoRa payload limit 255 bytes (general knowledge) and the plan's 'overlong line > 510 B'",
    "open": "nothing known: left unspecified on purpose",
}


# ------------------------------------------------------------------ the reference classification

_CALL = rb"[A-Z0-9]{1,6}(?:-(?:[0-9]|1[0-5]))?"
_STRICT = re.compile(rb"^" + _CALL + rb">" + _CALL + rb"(?:,(?:" + _CALL + rb")\*?){0,8}$")
_LENIENT_PART = re.compile(rb"^[A-Za-z0-9]{1,9}(?:-[0-9]{1,2})?\*?$")


def classify_line(raw, prefix=b""):
    """Verdict for ONE complete raw line (no terminator): ("accept", delivered bytes),
    ("reject", None), ("unspecified", delivered-if-any) or ("ignore", None) for an empty line.
    The rules are the ones of the module docstring; nothing here is verified."""
    if raw == b"":
        return ("ignore", None)
    if len(raw) > REJECT_OVER:
        return ("reject", None)
    if prefix:
        if not raw.startswith(prefix):
            return ("reject", None)
        body = raw[len(prefix):]
    else:
        body = raw
    if b"\r" in raw or b"\n" in raw:
        return ("unspecified", body)
    if body == b"":
        return ("reject", None)
    header, sep, info = body.partition(b":")
    if not sep:
        return ("reject", None)
    src, gt, rest = header.partition(b">")
    if not gt:
        return ("reject", None)
    if _STRICT.match(header):
        lenient = False
    else:
        parts = [src] + rest.split(b",")
        if not all(_LENIENT_PART.match(p) for p in parts):
            return ("reject", None)
        lenient = True                         # lower case, 7+ characters, SSID over 15, nine digipeaters
    if lenient or info == b"" or any(b < 0x20 or b >= 0x80 for b in info) or len(raw) > LORA_PAYLOAD_MAX:
        return ("unspecified", body)
    return ("accept", body)


def classify_stream(chunks, prefix=b"", eof=False):
    """The model of a conforming gateway for a list of reads: lines end at CRLF only, an unfinished
    tail is not delivered while the connection is open (`unspecified` when it holds a bare CR / LF
    or when the connection closes), every complete line is judged by classify_line. Returns a list
    of (verdict, raw line, delivered bytes or None) in stream order."""
    data = b"".join(chunks)
    out = []
    pos = 0
    while True:
        end = data.find(CRLF, pos)
        if end < 0:
            break
        raw = data[pos:end]
        verdict, body = classify_line(raw, prefix)
        if verdict != "ignore":
            out.append((verdict, raw, body))
        pos = end + 2
    tail = data[pos:]
    if tail:
        if b"\r" in tail.rstrip(b"\r") or b"\n" in tail or eof:
            out.append(("unspecified", tail, None))
        elif len(tail) > REJECT_OVER:
            out.append(("reject", tail, None))
    return out


# ------------------------------------------------------------------ scenarios

class Variant:
    """One scenario for one profile: the reads, and what a gateway has to do with them."""

    def __init__(self, chunks, accept=(), reject=(), unspecified=(), eof=False):
        self.chunks = [bytes(c) for c in chunks]
        self.accept = [bytes(x) for x in accept]
        self.reject = [bytes(x) for x in reject]
        self.unspecified = [bytes(x) for x in unspecified]
        self.eof = eof

    def stream(self):
        return b"".join(self.chunks)


class Scenario:
    def __init__(self, name, group, note, basis, build):
        self.name, self.group, self.note, self.basis, self.build = name, group, note, tuple(basis), build

    def variant(self, profile):
        return self.build(PROFILES[profile])


def _line_of_raw_size(n, head=HEAD):
    """A valid TNC2 line (no prefix) of exactly n bytes."""
    if n < len(head):
        raise ValueError("too short")
    return head + b"x" * (n - len(head))


def _mk():
    """The scenario table. Each builder takes the profile's prefix (b"" or 3C FF 01) and returns a
    Variant whose expectations are written out by hand."""
    S = []

    def add(name, group, note, basis, build):
        S.append(Scenario(name, group, note, basis, build))

    def ok(pfx, line):
        return pfx + line + CRLF

    # -- basic: the profile's own form
    add("line-crlf", "basic", "one line, the profile's own prefix, CRLF", ["plan-firmware", "plan-config"],
        lambda p: Variant([ok(p, STATUS)], accept=[STATUS]))
    add("three-lines-one-read", "basic", "three lines in one read", ["plan-firmware", "plan-config"],
        lambda p: Variant([ok(p, STATUS) + ok(p, POSITION) + ok(p, IGATE)], accept=[STATUS, POSITION, IGATE]))
    add("three-lines-three-reads", "basic", "the same, one line per read", ["plan-firmware", "plan-config"],
        lambda p: Variant([ok(p, STATUS), ok(p, POSITION), ok(p, IGATE)], accept=[STATUS, POSITION, IGATE]))
    # -- the other profile's form (absolute streams)
    add("unprefixed-line", "profile", "a line WITHOUT the prefix: what the firmware sends to the host", ["plan-firmware", "plan-config"],
        lambda p: Variant([ok(b"", STATUS)], accept=[] if p else [STATUS], reject=[STATUS] if p else []))
    add("prefixed-line", "profile", "a line WITH 3C FF 01: the raw radio payload", ["plan-firmware", "plan-config", "convention"],
        lambda p: Variant([ok(PREFIX, STATUS)], accept=[STATUS] if p else [], reject=[] if p else [PREFIX + STATUS]))
    add("mixed-prefix", "profile", "prefixed, plain, prefixed in one stream", ["plan-config", "convention"],
        lambda p: Variant([ok(PREFIX, STATUS) + ok(b"", POSITION) + ok(PREFIX, IGATE)],
                          accept=[STATUS, IGATE] if p else [POSITION],
                          reject=[POSITION] if p else [PREFIX + STATUS, PREFIX + IGATE]))

    # -- line endings
    add("lf-only", "ending", "LF is the only terminator (no CR)", ["open"],
        lambda p: Variant([p + STATUS + b"\n"], unspecified=[p + STATUS + b"\n"]))
    add("cr-only", "ending", "CR is the only terminator (no LF); the connection closes, so a CR waiting for its LF is not the answer", ["open"],
        lambda p: Variant([p + STATUS + b"\r"], unspecified=[p + STATUS + b"\r"], eof=True))
    add("lf-only-two-lines", "ending", "two lines, LF only: either two lines or one line holding a bare LF", ["open"],
        lambda p: Variant([p + STATUS + b"\n" + p + POSITION + b"\n"], unspecified=[p + STATUS + b"\n" + p + POSITION + b"\n"]))
    add("no-terminator-open", "ending", "a complete-looking line with no terminator, connection stays open: nothing is delivered", ["invariant"],
        lambda p: Variant([p + STATUS], eof=False))
    add("no-terminator-eof", "ending", "the same, then the connection closes: a gateway may or may not flush it", ["open"],
        lambda p: Variant([p + STATUS], unspecified=[p + STATUS], eof=True))
    add("double-crlf", "ending", "a line then an extra CRLF: the empty line is ignored", ["plan-config", "convention"],
        lambda p: Variant([ok(p, STATUS) + CRLF], accept=[STATUS]))

    # -- empty lines
    add("empty-line", "empty", "a single CRLF: no line, no error", ["convention"],
        lambda p: Variant([CRLF]))
    add("empty-lines-between", "empty", "lines with several empty lines between", ["convention"],
        lambda p: Variant([ok(p, STATUS) + CRLF + CRLF + CRLF + ok(p, POSITION)], accept=[STATUS, POSITION]))
    add("space-line", "empty", "a line of one space", ["convention"],
        lambda p: Variant([p + b" " + CRLF], reject=[p + b" "]))

    # -- prefix variants (absolute bytes; wrong for the empty-prefix profile too, since the line is no TNC2)
    def bad(prefix_bytes, line=STATUS):
        return lambda p: Variant([prefix_bytes + line + CRLF], reject=[prefix_bytes + line])
    add("prefix-3cff02", "prefix", "3C FF 02: last byte differs", ["plan-config"], bad(b"\x3c\xff\x02"))
    add("prefix-3dff01", "prefix", "3D FF 01: first byte differs", ["plan-config"], bad(b"\x3d\xff\x01"))
    add("prefix-3cfe01", "prefix", "3C FE 01: middle byte differs", ["plan-config"], bad(b"\x3c\xfe\x01"))
    add("prefix-swapped", "prefix", "FF 3C 01: right bytes, wrong order", ["plan-config"], bad(b"\xff\x3c\x01"))
    add("prefix-two-bytes", "prefix", "3C FF only: the third byte is missing", ["plan-config"], bad(b"\x3c\xff"))
    add("prefix-ascii-hex", "prefix", "the text '3cff01' in front of the line", ["plan-config"], bad(b"3cff01"))
    add("prefix-twice", "prefix", "3C FF 01 3C FF 01 and the line: the second prefix is part of the line", ["plan-config"],
        lambda p: Variant([PREFIX + PREFIX + STATUS + CRLF], reject=[PREFIX + PREFIX + STATUS]))
    add("prefix-only", "prefix", "3C FF 01 and nothing else", ["plan-config", "convention"],
        lambda p: Variant([PREFIX + CRLF], reject=[PREFIX]))
    add("prefix-twice-then-good", "prefix", "a doubled prefix line, then a good line: the good one still arrives", ["plan-config", "invariant"],
        lambda p: Variant([PREFIX + PREFIX + STATUS + CRLF + ok(p, POSITION)], accept=[POSITION], reject=[PREFIX + PREFIX + STATUS]))

    # -- not TNC2
    def junk(name, body, note, expect="reject"):
        def build(p):
            raw = p + body
            if expect == "reject":
                return Variant([raw + CRLF], reject=[raw])
            return Variant([raw + CRLF], unspecified=[raw])
        add(name, "garbage", note, ["convention"] if expect == "reject" else ["open"], build)
    junk("junk-no-gt", b"N0CALL-7APLRT1:test", "no '>' after the source")
    junk("junk-no-colon", b"N0CALL-7>APLRT1,WIDE1-1 test", "no ':' before the information")
    junk("junk-empty-source", b">APLRT1:test", "empty source")
    junk("junk-empty-dest", b"N0CALL-7>:test", "empty destination")
    junk("junk-empty-digi", b"N0CALL-7>APLRT1,,WIDE1-1:test", "an empty digipeater")
    junk("junk-text", b"hello world", "plain text")
    junk("junk-binary", bytes(range(0x80, 0x90)) + b">" + bytes(range(0x90, 0xA0)) + b":" + bytes(range(0xA0, 0xB0)), "binary bytes")
    junk("junk-nul", b"N0CALL-7>APLRT1:te\x00st", "NUL byte in the information", expect="unspecified")
    junk("tnc2-lowercase", b"n0call-7>aplrt1:test", "lower case callsigns (Dire Wolf refuses them)", expect="unspecified")
    junk("tnc2-ssid-16", b"N0CALL-16>APLRT1:test", "SSID 16", expect="unspecified")
    junk("tnc2-call-7", b"N0CALLX>APLRT1:test", "seven character callsign", expect="unspecified")
    junk("tnc2-digis-9", b"N0CALL-7>APLRT1,D1,D2,D3,D4,D5,D6,D7,D8,D9:test", "nine digipeaters (AX.25 allows eight)", expect="unspecified")
    junk("tnc2-info-empty", b"N0CALL-7>APLRT1:", "valid header, empty information", expect="unspecified")
    junk("tnc2-info-high", b"N0CALL-7>APLRT1:>caf\xc3\xa9", "information with UTF-8 bytes", expect="unspecified")
    add("tnc2-digis-8", "garbage", "eight digipeaters: the AX.25 maximum, valid", ["convention"],
        lambda p: Variant([ok(p, b"N0CALL-7>APLRT1,D1,D2,D3,D4,D5,D6,D7,D8:test")], accept=[b"N0CALL-7>APLRT1,D1,D2,D3,D4,D5,D6,D7,D8:test"]))
    add("tnc2-digi-star", "garbage", "a digipeater with the has-been-repeated star", ["convention"],
        lambda p: Variant([ok(p, b"N0CALL-7>APLRT1,D1*,WIDE2-1:test")], accept=[b"N0CALL-7>APLRT1,D1*,WIDE2-1:test"]))

    # -- size
    for n, tag, why in [(80, "80", "an ordinary line"), (255, "255", "255 raw bytes: the LoRa payload limit with the prefix"),
                        (256, "256", "256 raw bytes: one over the LoRa payload"), (510, "510", "510 raw bytes: the plan's limit"),
                        (511, "511", "511 raw bytes: over the plan's limit"), (1024, "1024", "1024 raw bytes"),
                        (4096, "4096", "4096 raw bytes")]:
        def build(p, n=n):
            line = _line_of_raw_size(n - len(p))
            raw = p + line
            if n <= LORA_PAYLOAD_MAX:
                return Variant([raw + CRLF], accept=[line])
            if n <= REJECT_OVER:
                return Variant([raw + CRLF], unspecified=[raw])
            return Variant([raw + CRLF], reject=[raw])
        add("size-%s" % tag, "size", why, ["size"], build)

    def resync(n):
        def build(p):
            line = _line_of_raw_size(n - len(p))
            return Variant([p + line + CRLF + ok(p, POSITION)], accept=[POSITION], reject=[p + line])
        return build
    add("overlong-then-good", "size", "an overlong line (1024 raw bytes), then a good one: the good one must still arrive", ["size", "invariant"], resync(1024))
    add("overlong-4096-then-good", "size", "4096 raw bytes, then a good line", ["size", "invariant"], resync(4096))

    def overlong_split(p):
        line = _line_of_raw_size(2048 - len(p))
        raw = p + line
        return Variant([raw[:700], raw[700:1500], raw[1500:] + CRLF + ok(p, IGATE)], accept=[IGATE], reject=[raw])
    add("overlong-split-then-good", "size", "an overlong line (2048 raw bytes) over three reads, then a good one", ["size", "invariant"], overlong_split)

    def overlong_open(p):
        raw = p + _line_of_raw_size(2048 - len(p))
        return Variant([raw[:1024], raw[1024:]], reject=[raw])
    add("overlong-no-terminator", "size", "2048 bytes and no terminator, connection open: nothing is delivered and nothing may grow without bound", ["size", "invariant"], overlong_open)

    # -- split reads (each one equals its unsplit twin)
    def split_at(*cuts):
        def build(p):
            stream = ok(p, STATUS)
            pieces, last = [], 0
            for c in cuts:
                pieces.append(stream[last:c])
                last = c
            pieces.append(stream[last:])
            return Variant(pieces, accept=[STATUS])
        return build
    add("split-mid-line", "split", "a line over two reads, cut in the middle", ["invariant"], split_at(20))
    add("split-before-crlf", "split", "the line in one read, CRLF in the next", ["invariant"],
        lambda p: Variant([p + STATUS, CRLF], accept=[STATUS]))
    add("split-between-cr-lf", "split", "cut between CR and LF", ["invariant"],
        lambda p: Variant([p + STATUS + b"\r", b"\n"], accept=[STATUS]))
    add("split-after-prefix", "split", "the prefix in one read, the line in the next (the empty profile cuts at 0)", ["invariant"],
        lambda p: Variant(([p] if p else []) + [STATUS + CRLF], accept=[STATUS]))
    add("split-in-prefix", "split", "cut after the first byte of the prefix (the empty profile cuts at 1 of the line)", ["invariant"],
        lambda p: Variant([(p + STATUS + CRLF)[:1], (p + STATUS + CRLF)[1:]], accept=[STATUS]))
    add("split-in-prefix-2", "split", "cut after the second byte", ["invariant"],
        lambda p: Variant([(p + STATUS + CRLF)[:2], (p + STATUS + CRLF)[2:]], accept=[STATUS]))
    add("split-bytewise", "split", "every byte its own read", ["invariant"],
        lambda p: Variant([bytes([b]) for b in ok(p, POSITION)], accept=[POSITION]))
    add("split-two-lines-straddle", "split", "two lines, the second cut in the middle", ["invariant"],
        lambda p: Variant([ok(p, STATUS) + (p + POSITION)[:10], (p + POSITION)[10:] + CRLF], accept=[STATUS, POSITION]))
    add("split-bad-prefix", "split", "a bad prefix over two reads: still rejected", ["invariant", "plan-config"],
        lambda p: Variant([b"\x3c\xff", b"\x02" + STATUS + CRLF], reject=[b"\x3c\xff\x02" + STATUS]))

    # -- resync
    add("mid-line-connect", "resync", "the connection starts in the middle of a line (the tail of one), then good lines", ["invariant", "convention"],
        lambda p: Variant([POSITION[20:] + CRLF + ok(p, IGATE)], accept=[IGATE], reject=[POSITION[20:]]))
    add("garbage-between-lines", "resync", "good, junk, good: the junk does not break the next line", ["invariant", "convention"],
        lambda p: Variant([ok(p, STATUS) + p + b"hello world" + CRLF + ok(p, POSITION)], accept=[STATUS, POSITION], reject=[p + b"hello world"]))
    return S


SCENARIOS = _mk()
SCENARIO_BY_NAME = {s.name: s for s in SCENARIOS}
GROUPS = sorted({s.group for s in SCENARIOS})

# split scenarios and the unsplit stream they must equal
SPLIT_TWINS = {
    "split-mid-line": "line-crlf", "split-before-crlf": "line-crlf", "split-between-cr-lf": "line-crlf",
    "split-after-prefix": "line-crlf", "split-in-prefix": "line-crlf", "split-in-prefix-2": "line-crlf",
}


# ------------------------------------------------------------------ vectors

def _hex_list(items):
    return [bytes(x).hex() for x in items]


def line_rows():
    """The three base lines: TNC2 text, host-link bytes (CRLF, prefix stripped) and the LoRa payload
    (prefix + line), with the payload length and the time on air at SF12 / SF9 / SF7, BW 125 kHz."""
    rows = []
    for name, line in (("status", STATUS), ("position", POSITION), ("igate", IGATE)):
        pl = len(PREFIX) + len(line)
        row = {"id": name, "tnc2": line.decode("ascii"), "tnc2_hex": line.hex(), "host_hex": (line + CRLF).hex(),
               "air_hex": (PREFIX + line).hex(), "payload_bytes": pl}
        for label, sf in airtime.LORA_APRS_SETTINGS:
            row["air_ms_%s" % label.lower()] = airtime.exact_ms(airtime.time_on_air(pl, sf, 125000))
        rows.append(row)
    return rows


def lines_document():
    return {"schema": "dvt-lora-gw-lines/1", "prefix_hex": PREFIX.hex(), "terminator_hex": CRLF.hex(),
            "note": "synthetic placeholders (N0CALL-7); tocalls APLRT1 / APLRG1 are only plausible values (UNVERIFIED)",
            "rows": line_rows()}


def streams_document():
    scenarios = []
    for s in SCENARIOS:
        rec = {"name": s.name, "group": s.group, "note": s.note, "basis": list(s.basis), "status": "UNVERIFIED",
               "variants": {}}
        for profile in PROFILES:
            v = s.variant(profile)
            rec["variants"][profile] = {"chunks_hex": _hex_list(v.chunks), "eof": v.eof, "accept_hex": _hex_list(v.accept),
                                        "reject_hex": _hex_list(v.reject), "unspecified_hex": _hex_list(v.unspecified)}
        scenarios.append(rec)
    return {"schema": "dvt-lora-gw-streams/1", "prefix_hex": PREFIX.hex(), "terminator_hex": CRLF.hex(),
            "profiles": {name: pfx.hex() for name, pfx in PROFILES.items()},
            "limits": {"lora_payload_max": LORA_PAYLOAD_MAX, "reject_over": REJECT_OVER},
            "basis": BASIS,
            "verdicts": {"accept": "must be delivered, in this order, with the prefix removed",
                         "reject": "must NOT be delivered (the raw line as received)",
                         "unspecified": "no expectation: a test must not assert on it"},
            "scenarios": scenarios}


def write_vectors(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, doc in (("lines.json", lines_document()), ("streams.json", streams_document())):
        with open(os.path.join(out_dir, name), "w", encoding="ascii") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.write("\n")


# ------------------------------------------------------------------ the scripted source

def serve_once(chunks, host="127.0.0.1", port=0, delay=0.05, ready=None, eof=False, accept_timeout=30.0, hold=0.5):
    """Listen on host:port (127.0.0.1 by default), accept ONE client, send the reads one by one with
    `delay` seconds between them (TCP_NODELAY, so each one is its own segment), then wait `hold`
    seconds (or close at once when `eof`). `ready(port)` is called once the socket listens. Returns
    the number of bytes sent."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        srv.settimeout(accept_timeout)
        if ready:
            ready(srv.getsockname()[1])
        conn, _addr = srv.accept()
    finally:
        srv.close()
    sent = 0
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for i, chunk in enumerate(chunks):
            if i:
                time.sleep(delay)
            conn.sendall(chunk)
            sent += len(chunk)
        if not eof:
            time.sleep(hold)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
    return sent


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lora_gw", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="regenerate vectors/lora_gw/*.json")
    w.add_argument("--out-dir", default="vectors/lora_gw")
    sub.add_parser("list", help="list the scenarios")
    s = sub.add_parser("serve", help="listen on TCP and send one scenario to the first client")
    s.add_argument("--scenario", required=True)
    s.add_argument("--profile", choices=sorted(PROFILES), default="none")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8001)
    s.add_argument("--delay", type=float, default=0.05, help="seconds between reads")
    args = parser.parse_args(argv)
    if args.cmd == "write":
        write_vectors(args.out_dir)
        print("wrote %d scenarios x %d profiles to %s" % (len(SCENARIOS), len(PROFILES), args.out_dir))
        return 0
    if args.cmd == "list":
        for sc in SCENARIOS:
            print("%-26s %-8s %s" % (sc.name, sc.group, sc.note))
        return 0
    if args.scenario not in SCENARIO_BY_NAME:
        print("unknown scenario %r (see: list)" % args.scenario, file=sys.stderr)
        return 2
    v = SCENARIO_BY_NAME[args.scenario].variant(args.profile)
    sent = serve_once(v.chunks, host=args.host, port=args.port, delay=args.delay, eof=v.eof,
                      ready=lambda port: print("listening on %s:%d" % (args.host, port), flush=True))
    print("sent %d bytes in %d reads" % (sent, len(v.chunks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
