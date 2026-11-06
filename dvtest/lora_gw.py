# SPDX-License-Identifier: MIT
"""dvt-lora-gw: a scripted, hardware-free source of raw TNC2 lines and u16 length-framed AX.25 frames
for the LoRa APRS gateway path (HWFREE1 item 3, DP11d; docs/lora-gw-cases.md).

Pure Python, stdlib only, no hardware, no container. A LoRa APRS iGate puts TNC2 text lines on a
TCP port (or a serial line). On the air each line is preceded by the three bytes 3C FF 01
(`<`, 0xFF, 0x01); the prefix is a per-port hex configuration value of DV-Router's raw-tcp /
raw-serial adapter (0 to 16 bytes, exact match, removed before delivery). This module generates,
byte for byte, the streams such an adapter has to cope with, in two PROFILES (empty prefix `none`,
prefix `3cff01`) and two framings:

  * text (`caps.framing = "tnc2"`): LF ends a line, CR only directly before LF, a NUL or a CR
    anywhere else invalidates the line, at most 510 bytes after the prefix, a line error
    resynchronises at the next LF;
  * binary (`caps.framing = "raw"`): a 2-byte big-endian length (prefix included), then that many
    bytes; an invalid length terminates the connection.

EXPECTATIONS (VERIFIED by reading, not by running): every expectation was derived from the
DV-Router source tree DV-Router-wip, commit 1bb304f0, files unmodified, cited per scenario as
`file:line` relative to its `crates/` directory: edge/ax25/src/raw.rs (the framing decoder),
edge/ax25/src/adapter/tcp.rs (how decoder results become modem counters), edge/ax25/src/raw_tests.rs
(its own unit tests, reproduced in cases/unit/test_lora_gw.py), data/worker/src/core_rf.rs and
lib/pkt/src/aprs/tnc2.rs (what the worker does with a delivered TNC2 line), lib/pkt/src/ax25.rs
(the binary payload check). The firmware side (what a real iGate sends) is still a plan statement
(basis `plan-firmware`). The model classes below are OUR restatement of those lines, not a copy;
the scenario tables state their expectations by hand and the unit tests check the two against each
other and against the cases of raw_tests.rs.

Two observation points per line (the edge only frames; the worker parses TNC2):

  edge    `dv_edge_modem_total{result="bad_frame"}` counts every framing error (tcp.rs:396-398),
          `result="rx"` every line the framer hands on (tcp.rs:428-431, text lines are NOT parsed
          at the edge: tcp.rs:420, 424);
  worker  a delivered TNC2 line that `aprs::tnc2::parse` refuses is counted as
          `dv_pkt_raw_total{kind="aprs-tnc2",result="rejected"}` and
          `dv_pkt_reject_total{reason="invalid"}` (core_rf.rs:135-141, core.rs:467-476).

Nothing is ever answered on the wire; a rejection is only a counter.

    python3 -m dvtest.lora_gw write [--out-dir vectors/lora_gw]
    python3 -m dvtest.lora_gw list
    python3 -m dvtest.lora_gw serve --scenario split-mid-line --profile 3cff01 --port 8001
"""
import argparse
import json
import os
import socket
import sys
import time

from . import airtime, ax25

PREFIX = b"\x3c\xff\x01"
CRLF = b"\r\n"
PROFILES = {"none": b"", "3cff01": PREFIX}

# limits of raw.rs (lines 4-6)
MAX_PREFIX = 16
MAX_LINE = 510              # text: bytes after the prefix; the line may hold MAX_LINE + len(prefix) bytes (raw.rs:78)
MAX_FRAME = 1100            # binary: payload after the prefix (raw.rs:87)

# the three synthetic lines of the plan (research note 2b.4), the same as dvtest.airtime.LORA_APRS_LINES
STATUS, POSITION, IGATE = [ln.encode("ascii") for ln in airtime.LORA_APRS_LINES]
HEAD = b"N0CALL-7>APLRT1:"

SOURCE_TREE = "DV-Router-wip 1bb304f0 (files unmodified), paths relative to crates/"

BASIS = {
    "source-read": "read in the DV-Router source (the file:line list of the scenario); not run against a binary",
    "plan-firmware": "the plan says the iGate firmware strips the prefix and ends lines with CRLF (firmware not re-read: UNVERIFIED)",
    "invariant": "a stream parser must give the same result however the bytes are split into reads (raw.rs pushes one byte at a time)",
}

# citations (all in SOURCE_TREE)
C = {
    "lf": "edge/ax25/src/raw.rs:60-66",              # LF finishes the line and resets length, error, cr
    "skip": "edge/ax25/src/raw.rs:67-69",            # after an error the rest of the line is ignored
    "crnul": "edge/ax25/src/raw.rs:70-73",           # a NUL, or any byte after a CR, invalidates the line
    "cr": "edge/ax25/src/raw.rs:74-77",              # a CR is remembered, not stored
    "long": "edge/ax25/src/raw.rs:78-81",            # length >= 510 + prefix: TooLong
    "finish": "edge/ax25/src/raw.rs:115-128",        # error, prefix strip (119-123), empty payload (124-126)
    "bytewise": "edge/ax25/src/raw.rs:55-114",       # push() is one byte at a time
    "limits": "edge/ax25/src/raw.rs:4-6",
    "newcfg": "edge/ax25/src/raw.rs:35-40",          # prefix <= 16, no NUL/CR/LF in a text prefix
    "hdr": "edge/ax25/src/raw.rs:82-98",             # binary header
    "hdrlen": "edge/ax25/src/raw.rs:87-94",          # length <= prefix: Invalid, > 1100 + prefix: TooLong, both terminal
    "binend": "edge/ax25/src/raw.rs:99-113",         # frame complete
    "terminal": "edge/ax25/src/raw.rs:56-58",        # terminal: further bytes ignored
    "tcp_raw": "edge/ax25/src/adapter/tcp.rs:391-404",   # Ok -> received, Err -> frames_bad, terminal -> close
    "tcp_close": "edge/ax25/src/adapter/tcp.rs:291-295", # bytes() == false: close the connection
    "tcp_eof": "edge/ax25/src/adapter/tcp.rs:304-308",   # connection closed: no flush of a partial line
    "tcp_recv": "edge/ax25/src/adapter/tcp.rs:416-443",  # text is not parsed (420, 424); binary must parse as AX.25 (424-427)
    "worker_tnc2": "data/worker/src/core_rf.rs:128-142", # TNC2 lines are parsed by aprs::tnc2::parse, addresses not narrowed to AX.25
    "tnc2_parse": "lib/pkt/src/aprs/tnc2.rs:73-108",
    "tnc2_call": "lib/pkt/src/aprs/tnc2.rs:37-55",
    "tnc2_path": "lib/pkt/src/aprs/tnc2.rs:151-155",
    "reject": "data/worker/src/core.rs:467-476",
    "key": "lib/pkt/src/aprs/dedupe.rs:75-86",
    "ax25_parse": "lib/pkt/src/ax25.rs:94-146",
    "ax25_addr": "lib/pkt/src/ax25.rs:223-243",
    "test_text": "edge/ax25/src/raw_tests.rs:10-24",
    "test_bin": "edge/ax25/src/raw_tests.rs:27-41",
    "test_resync": "edge/ax25/src/raw_tests.rs:44-56",
    "test_bounds": "edge/ax25/src/raw_tests.rs:59-79",
}


# ------------------------------------------------------------------ models of the edge framer and of the worker

class TextFramer:
    """Streaming model of DV-Router's text framing (raw.rs:55-81, 115-128), prefix removed.
    feed() returns events in stream order: ("ok", payload, raw) or ("bad", kind, raw); `raw` is the line as
    received without its LF and without the one CR directly before it. kind is "Invalid" or "TooLong"."""

    def __init__(self, prefix=b""):
        if len(prefix) > MAX_PREFIX or any(b in (0, 10, 13) for b in prefix):
            raise ValueError("a text prefix is at most 16 bytes without NUL, CR or LF")
        self.prefix = bytes(prefix)
        self._reset()

    def _reset(self):
        self._raw = bytearray()
        self._stored = bytearray()
        self._error = None
        self._cr = False

    def feed(self, data):
        events = []
        for b in data:
            if b == 10:
                raw = bytes(self._raw[:-1] if self._raw[-1:] == b"\r" else self._raw)
                events.append(self._finish(raw))
                self._reset()
                continue
            self._raw.append(b)
            if self._error:
                continue
            if self._cr or b == 0:
                self._error = "Invalid"
                continue
            if b == 13:
                self._cr = True
                continue
            if len(self._stored) >= MAX_LINE + len(self.prefix):
                self._error = "TooLong"
                continue
            self._stored.append(b)
        return events

    def _finish(self, raw):
        if self._error:
            return ("bad", self._error, raw)
        if not self._stored.startswith(self.prefix):
            return ("bad", "Invalid", raw)
        payload = bytes(self._stored[len(self.prefix):])
        if not payload:
            return ("bad", "Invalid", raw)
        return ("ok", payload, raw)

    def pending(self):
        """Bytes of an unfinished line (never delivered, never counted)."""
        return bytes(self._raw)


class BinaryFramer:
    """Streaming model of the u16 big-endian length framing (raw.rs:82-113): the length counts the prefix
    and the payload and excludes the 2 header bytes; length <= len(prefix) is Invalid and length >
    1100 + len(prefix) is TooLong, both TERMINAL (the connection is closed, tcp.rs:401-403, 291-295).
    A prefix mismatch is Invalid but not terminal (the length keeps the framing aligned).
    feed() returns ("ok", payload, raw), ("bad", kind, raw, terminal); `raw` is the bytes after the header
    (the 2 header bytes for a terminal event)."""

    def __init__(self, prefix=b""):
        if len(prefix) > MAX_PREFIX:
            raise ValueError("a prefix is at most 16 bytes")
        self.prefix = bytes(prefix)
        self.terminal = False
        self._header = bytearray()
        self._expected = None
        self._body = bytearray()

    def feed(self, data):
        events = []
        for b in data:
            if self.terminal:
                break
            if self._expected is None:
                self._header.append(b)
                if len(self._header) == 2:
                    length = int.from_bytes(self._header, "big")
                    if length <= len(self.prefix) or length > MAX_FRAME + len(self.prefix):
                        self.terminal = True
                        kind = "TooLong" if length > MAX_FRAME + len(self.prefix) else "Invalid"
                        events.append(("bad", kind, bytes(self._header), True))
                        break
                    self._expected = length
                continue
            self._body.append(b)
            if len(self._body) == self._expected:
                raw = bytes(self._body)
                if raw.startswith(self.prefix):
                    events.append(("ok", raw[len(self.prefix):], raw))
                else:
                    events.append(("bad", "Invalid", raw, False))
                self._header = bytearray()
                self._expected = None
                self._body = bytearray()
        return events


def _valid_call(call):
    """lib/pkt/src/aprs/tnc2.rs:37-55: 1 to 9 characters, [A-Z0-9]+ with one optional -[A-Z0-9]+ suffix."""
    if not call or len(call) > 9:
        return False
    parts = call.split("-")
    if len(parts) > 2:
        return False

    def word(w):
        return bool(w) and all(c.isascii() and (c.isdigit() or "A" <= c <= "Z") for c in w)
    return word(parts[0]) and (len(parts) == 1 or word(parts[1]))


def _valid_path(address):
    return _valid_call(address) or (len(address) == 3 and address[0] == "q" and address[1:].isalpha() and address[1:].isascii())


def worker_tnc2_ok(payload):
    """True when the worker's TNC2 parser (tnc2.rs:73-108) accepts the delivered line: at most 510 bytes, no
    NUL / CR / LF, a ':' with a UTF-8 header before it that has a '>', valid source and destination (1 to 9
    characters, uppercase letters and digits, one optional -SUFFIX of letters and digits: SSID 16 and seven
    character calls are valid), at most 32 path entries each a valid call or a q construct, with an optional
    '*' on each. The information field (empty or any bytes) is not looked at. NOT the AX.25 rules."""
    if len(payload) > MAX_LINE or any(b in (0, 10, 13) for b in payload):
        return False
    colon = payload.find(b":")
    if colon < 0:
        return False
    try:
        header = payload[:colon].decode("utf-8")
    except UnicodeDecodeError:
        return False
    source, gt, rest = header.partition(">")
    if not gt:
        return False
    fields = rest.split(",")
    if not _valid_call(source) or not _valid_call(fields[0]):
        return False
    path = fields[1:]
    if len(path) > 32:
        return False
    for field in path:
        if not _valid_path(field[:-1] if field.endswith("*") else field):
            return False
    return True


def edge_ax25_ok(frame):
    """True when the edge accepts a binary payload as an AX.25 UI frame (tcp.rs:424-427 calls
    lib/pkt/src/ax25.rs:94-146): 16 to 1100 bytes, at most 8 digipeaters, every address six characters
    shifted left one bit, uppercase letters, digits and trailing spaces only, at least one character,
    control 0x03 and PID 0xF0."""
    if not 16 <= len(frame) <= MAX_FRAME:
        return False
    addresses = 0
    offset = 0
    while True:
        if addresses == 10:
            return False
        field = frame[offset:offset + 7]
        if len(field) < 7:
            return False
        chars = []
        for raw in field[:6]:
            if raw & 1:
                return False
            chars.append(raw >> 1)
        count, padding = 0, False
        for ch in chars:
            if ch == 0x20:
                padding = True
            elif padding or not (0x30 <= ch <= 0x39 or 0x41 <= ch <= 0x5A):
                return False
            else:
                count += 1
        if count == 0:
            return False
        addresses += 1
        offset += 7
        if field[6] & 1:
            break
    if addresses < 2:
        return False
    trailer = frame[offset:]
    return len(trailer) >= 2 and trailer[0] == 0x03 and trailer[1] == 0xF0


def classify_stream(chunks, prefix=b"", eof=False, framing="text"):
    """The reference outcome of a list of reads: one tuple per COMPLETE frame in stream order,
    ("accept", raw, delivered) when the edge frames it and the next stage accepts it, ("reject", raw, None)
    otherwise. An unfinished tail is neither delivered nor counted (also not at eof: tcp.rs:304-308 has no
    flush). Text lines that the framer delivers are judged by worker_tnc2_ok, binary payloads by edge_ax25_ok.
    Use framer_events() for the edge / worker split."""
    out = []
    for ev in framer_events(chunks, prefix, framing)["events"]:
        out.append((ev["verdict"], ev["raw"], ev.get("payload") if ev["verdict"] == "accept" else None))
    return out


def framer_events(chunks, prefix=b"", framing="text"):
    """Detailed outcome: {"events": [{verdict, stage, raw, payload, kind}], "bad_frames", "worker_invalid",
    "closed"}. stage is "edge" (a framing error or, for binary, a payload that is no AX.25 frame) or
    "worker" (a delivered TNC2 line that tnc2::parse refuses) or "ok"."""
    framer = TextFramer(prefix) if framing == "text" else BinaryFramer(prefix)
    events = []
    bad = 0
    invalid = []
    for chunk in chunks:
        for ev in framer.feed(chunk):
            if ev[0] == "bad":
                bad += 1
                events.append({"verdict": "reject", "stage": "edge", "raw": ev[2], "kind": ev[1]})
            elif framing == "text":
                if worker_tnc2_ok(ev[1]):
                    events.append({"verdict": "accept", "stage": "ok", "raw": ev[2], "payload": ev[1]})
                else:
                    invalid.append(ev[1])
                    events.append({"verdict": "reject", "stage": "worker", "raw": ev[2], "payload": ev[1], "kind": "invalid"})
            else:
                if edge_ax25_ok(ev[1]):
                    events.append({"verdict": "accept", "stage": "ok", "raw": ev[2], "payload": ev[1]})
                else:
                    bad += 1
                    events.append({"verdict": "reject", "stage": "edge", "raw": ev[2], "kind": "NotAx25"})
    return {"events": events, "bad_frames": bad, "worker_invalid": invalid,
            "closed": bool(getattr(framer, "terminal", False))}


def classify_line(raw, prefix=b""):
    """Verdict for ONE complete text line without its terminator: ("accept", delivered bytes) or
    ("reject", None). An empty line is a rejected frame, not an ignored one (raw.rs:124-126)."""
    result = classify_stream([raw + b"\n"], prefix)
    verdict, _raw, delivered = result[0]
    return (verdict, delivered)


# ------------------------------------------------------------------ scenarios

class Variant:
    """One scenario for one profile: the reads, and what the adapter has to do with them.

    accept     frames delivered end to end, in order, prefix removed (the payload bytes);
    rejects    (stage, raw, payload) per rejected frame in stream order: stage "edge" counts one bad frame,
               stage "worker" is delivered by the edge and refused by the TNC2 parser (payload = what it saw);
    closed     the adapter closes the connection (binary terminal length);
    eof        the connection closes after the last read."""

    def __init__(self, chunks, accept=(), rejects=(), eof=False, closed=False, framing="text"):
        self.chunks = [bytes(c) for c in chunks]
        self.accept = [bytes(x) for x in accept]
        self.rejects = [(stage, bytes(raw), None if payload is None else bytes(payload)) for stage, raw, payload in rejects]
        self.eof = eof
        self.closed = closed
        self.framing = framing

    @property
    def reject(self):
        return [raw for _s, raw, _p in self.rejects]

    @property
    def bad_frames(self):
        return sum(1 for s, _r, _p in self.rejects if s == "edge")

    @property
    def worker_invalid(self):
        return [p for s, _r, p in self.rejects if s == "worker"]

    unspecified = ()

    def stream(self):
        return b"".join(self.chunks)


def E(raw):
    """A frame the edge counts as a bad frame (framing error / not AX.25)."""
    return ("edge", raw, None)


def W(raw, payload=None):
    """A line the edge delivers and the worker's TNC2 parser refuses (payload defaults to the raw line)."""
    return ("worker", raw, raw if payload is None else payload)


class Scenario:
    def __init__(self, name, group, note, basis, cites, build, framing="text"):
        self.name, self.group, self.note, self.basis, self.cites, self.build = name, group, note, tuple(basis), tuple(cites), build
        self.framing = framing

    def variant(self, profile):
        return self.build(PROFILES[profile])


def _line_of_size(n, head=HEAD):
    """A valid TNC2 line (no prefix) of exactly n bytes."""
    if n < len(head):
        raise ValueError("too short")
    return head + b"x" * (n - len(head))


_line_of_raw_size = _line_of_size            # earlier name


def hdr(n):
    return int(n).to_bytes(2, "big")


def _ui_of_size(n):
    """An AX.25 UI frame of exactly n bytes (16 header bytes + information)."""
    return ax25.from_tnc2("N0CALL-7>APLRT1:" + "x" * (n - 16))


def _mk():
    """The scenario table. Each builder takes the profile's prefix (b"" or 3C FF 01) and returns a Variant
    whose expectations are written out by hand from the cited lines; classify_stream / framer_events are
    checked against them by the unit tests."""
    S = []

    def add(name, group, note, basis, cites, build, framing="text"):
        S.append(Scenario(name, group, note, basis, cites, build, framing))

    def ok(pfx, line):
        return pfx + line + CRLF

    TXT = ["lf", "finish", "bytewise", "tcp_raw", "worker_tnc2", "tnc2_parse"]
    # -- basic
    add("line-crlf", "basic", "one line, the profile's own prefix, CRLF", ["source-read", "plan-firmware"], TXT,
        lambda p: Variant([ok(p, STATUS)], accept=[STATUS]))
    add("three-lines-one-read", "basic", "three lines in one read", ["source-read", "plan-firmware"], TXT,
        lambda p: Variant([ok(p, STATUS) + ok(p, POSITION) + ok(p, IGATE)], accept=[STATUS, POSITION, IGATE]))
    add("three-lines-three-reads", "basic", "the same, one line per read", ["source-read", "plan-firmware"], TXT,
        lambda p: Variant([ok(p, STATUS), ok(p, POSITION), ok(p, IGATE)], accept=[STATUS, POSITION, IGATE]))
    # -- the other profile's form
    add("unprefixed-line", "profile", "a line WITHOUT the prefix: what the firmware sends to the host", ["source-read", "plan-firmware"],
        ["finish", "tcp_raw"],
        lambda p: Variant([ok(b"", STATUS)], accept=[] if p else [STATUS], rejects=[E(STATUS)] if p else []))
    add("prefixed-line", "profile", "a line WITH 3C FF 01: the raw radio payload", ["source-read", "plan-firmware"],
        ["finish", "tcp_raw", "worker_tnc2", "tnc2_parse"],
        lambda p: Variant([ok(PREFIX, STATUS)], accept=[STATUS] if p else [], rejects=[] if p else [W(PREFIX + STATUS)]))
    add("mixed-prefix", "profile", "prefixed, plain, prefixed in one stream", ["source-read"],
        ["finish", "tcp_raw", "worker_tnc2", "tnc2_parse"],
        lambda p: Variant([ok(PREFIX, STATUS) + ok(b"", POSITION) + ok(PREFIX, IGATE)],
                          accept=[STATUS, IGATE] if p else [POSITION],
                          rejects=[E(POSITION)] if p else [W(PREFIX + STATUS), W(PREFIX + IGATE)]))

    # -- line endings
    add("lf-only", "ending", "LF is a complete terminator (no CR)", ["source-read"], ["lf", "cr", "finish"],
        lambda p: Variant([p + STATUS + b"\n"], accept=[STATUS]))
    add("cr-only", "ending", "CR with no LF, then the connection closes: nothing is delivered and nothing is counted", ["source-read"],
        ["lf", "cr", "tcp_eof"],
        lambda p: Variant([p + STATUS + b"\r"], eof=True))
    add("lf-only-two-lines", "ending", "two lines, LF only: two lines", ["source-read"], ["lf", "finish"],
        lambda p: Variant([p + STATUS + b"\n" + p + POSITION + b"\n"], accept=[STATUS, POSITION]))
    add("no-terminator-open", "ending", "a complete-looking line with no terminator, connection stays open: nothing is delivered", ["source-read", "invariant"],
        ["lf"],
        lambda p: Variant([p + STATUS], eof=False))
    add("no-terminator-eof", "ending", "the same, then the connection closes: the partial line is discarded, not flushed and not counted", ["source-read"],
        ["lf", "tcp_eof"],
        lambda p: Variant([p + STATUS], eof=True))
    add("double-crlf", "ending", "a line then an extra CRLF: the empty line is a rejected frame (one bad frame)", ["source-read"],
        ["lf", "finish"],
        lambda p: Variant([ok(p, STATUS) + CRLF], accept=[STATUS], rejects=[E(b"")]))
    add("cr-before-text", "ending", "CR followed by anything but LF invalidates the line (one bad frame), the next line is fine", ["source-read"],
        ["cr", "crnul", "skip", "lf"],
        lambda p: Variant([p + STATUS + b"\rX" + CRLF + ok(p, POSITION)], accept=[POSITION], rejects=[E(p + STATUS + b"\rX")]))
    add("cr-cr-lf", "ending", "CR CR LF: the second CR invalidates the line", ["source-read"], ["cr", "crnul", "lf"],
        lambda p: Variant([p + STATUS + b"\r\r\n" + ok(p, POSITION)], accept=[POSITION], rejects=[E(p + STATUS + b"\r")]))

    # -- empty lines
    add("empty-line", "empty", "a single CRLF: an empty frame, one bad frame, nothing delivered", ["source-read"], ["lf", "finish"],
        lambda p: Variant([CRLF], rejects=[E(b"")]))
    add("empty-lines-between", "empty", "lines with several empty lines between: each empty line is one bad frame", ["source-read"], ["lf", "finish"],
        lambda p: Variant([ok(p, STATUS) + CRLF + CRLF + CRLF + ok(p, POSITION)], accept=[STATUS, POSITION], rejects=[E(b"")] * 3))
    add("space-line", "empty", "a line of one space: framed, then refused by the TNC2 parser", ["source-read"], ["finish", "worker_tnc2", "tnc2_parse"],
        lambda p: Variant([p + b" " + CRLF], rejects=[W(p + b" ", b" ")]))

    # -- prefix variants (absolute bytes)
    def bad(prefix_bytes, line=STATUS):
        def build(p):
            raw = prefix_bytes + line
            return Variant([raw + CRLF], rejects=[E(raw) if p else W(raw)])
        return build
    PRE = ["finish", "tcp_raw", "worker_tnc2", "tnc2_parse"]
    add("prefix-3cff02", "prefix", "3C FF 02: last byte differs", ["source-read"], PRE, bad(b"\x3c\xff\x02"))
    add("prefix-3dff01", "prefix", "3D FF 01: first byte differs", ["source-read"], PRE, bad(b"\x3d\xff\x01"))
    add("prefix-3cfe01", "prefix", "3C FE 01: middle byte differs", ["source-read"], PRE, bad(b"\x3c\xfe\x01"))
    add("prefix-swapped", "prefix", "FF 3C 01: right bytes, wrong order", ["source-read"], PRE, bad(b"\xff\x3c\x01"))
    add("prefix-two-bytes", "prefix", "3C FF only: the third byte is missing", ["source-read"], PRE, bad(b"\x3c\xff"))
    add("prefix-ascii-hex", "prefix", "the text '3cff01' in front of the line", ["source-read"], PRE, bad(b"3cff01"))
    add("prefix-twice", "prefix", "3C FF 01 3C FF 01 and the line: one prefix is removed, the second is part of the line and the TNC2 parser refuses it",
        ["source-read"], PRE,
        lambda p: Variant([PREFIX + PREFIX + STATUS + CRLF],
                          rejects=[W(PREFIX + PREFIX + STATUS, PREFIX + STATUS if p else PREFIX + PREFIX + STATUS)]))
    add("prefix-only", "prefix", "3C FF 01 and nothing else: an empty payload with the prefix configured, else a line the TNC2 parser refuses", ["source-read"],
        ["finish", "tcp_raw", "worker_tnc2"],
        lambda p: Variant([PREFIX + CRLF], rejects=[E(PREFIX) if p else W(PREFIX)]))
    add("prefix-twice-then-good", "prefix", "a doubled prefix line, then a good line: the good one still arrives", ["source-read"], PRE,
        lambda p: Variant([PREFIX + PREFIX + STATUS + CRLF + ok(p, POSITION)], accept=[POSITION],
                          rejects=[W(PREFIX + PREFIX + STATUS, PREFIX + STATUS if p else PREFIX + PREFIX + STATUS)]))

    # -- not TNC2 / TNC2 edge cases: the edge delivers them, the worker's parser decides
    G = ["finish", "worker_tnc2", "tnc2_parse", "tnc2_call", "tnc2_path"]

    def junk(name, body, note, cites=G):
        add(name, "garbage", note, ["source-read"], cites,
            lambda p: Variant([p + body + CRLF], rejects=[W(p + body, body)]))

    def valid(name, body, note):
        add(name, "garbage", note, ["source-read"], G,
            lambda p: Variant([ok(p, body)], accept=[body]))
    junk("junk-no-gt", b"N0CALL-7APLRT1:test", "no '>' after the source")
    junk("junk-no-colon", b"N0CALL-7>APLRT1,WIDE1-1 test", "no ':' before the information")
    junk("junk-empty-source", b">APLRT1:test", "empty source")
    junk("junk-empty-dest", b"N0CALL-7>:test", "empty destination")
    junk("junk-empty-digi", b"N0CALL-7>APLRT1,,WIDE1-1:test", "an empty digipeater")
    junk("junk-text", b"hello world", "plain text")
    junk("junk-binary", bytes(range(0x80, 0x90)) + b">" + bytes(range(0x90, 0xA0)) + b":" + bytes(range(0xA0, 0xB0)),
         "binary bytes (the header is not UTF-8)")
    add("junk-nul", "garbage", "NUL byte in the information: the framer invalidates the line (one bad frame)", ["source-read"], ["crnul", "skip", "lf"],
        lambda p: Variant([p + b"N0CALL-7>APLRT1:te\x00st" + CRLF], rejects=[E(p + b"N0CALL-7>APLRT1:te\x00st")]))
    junk("tnc2-lowercase", b"n0call-7>aplrt1:test", "lower case callsigns: refused by the TNC2 parser")
    valid("tnc2-ssid-16", b"N0CALL-16>APLRT1:test", "SSID 16: valid for the TNC2 parser (any alphanumeric suffix; AX.25 would refuse it)")
    valid("tnc2-call-7", b"N0CALLX>APLRT1:test", "seven character callsign: valid for the TNC2 parser (AX.25 would refuse it)")
    valid("tnc2-digis-9", b"N0CALL-7>APLRT1,D1,D2,D3,D4,D5,D6,D7,D8,D9:test", "nine digipeaters: valid for the TNC2 parser (up to 32; AX.25 would refuse it)")
    valid("tnc2-info-empty", b"N0CALL-7>APLRT1:", "valid header, empty information")
    valid("tnc2-info-high", b"N0CALL-7>APLRT1:>caf\xc3\xa9", "information with UTF-8 bytes")
    valid("tnc2-digis-8", b"N0CALL-7>APLRT1,D1,D2,D3,D4,D5,D6,D7,D8:test", "eight digipeaters")
    valid("tnc2-digi-star", b"N0CALL-7>APLRT1,D1*,WIDE2-1:test", "a digipeater with the has-been-repeated star")

    # -- size: N counts the bytes AFTER the prefix; the line may hold N + len(prefix) bytes on the wire
    def size_case(n):
        def build(p):
            line = _line_of_size(n)
            raw = p + line
            if n <= MAX_LINE:
                return Variant([raw + CRLF], accept=[line])
            return Variant([raw + CRLF], rejects=[E(raw)])
        return build
    SZ = ["long", "finish", "newcfg", "limits"]
    for n, why in [(80, "an ordinary line"), (255, "255 bytes after the prefix (the LoRa payload size is no limit on this path)"),
                   (256, "256 bytes after the prefix"), (510, "510 bytes after the prefix: the limit (on the wire 510 + the prefix length)"),
                   (511, "511 bytes after the prefix: TooLong, one bad frame"), (1024, "1024 bytes"), (4096, "4096 bytes")]:
        add("size-%d" % n, "size", why, ["source-read"], SZ, size_case(n))

    def resync(n):
        def build(p):
            line = _line_of_size(n)
            return Variant([p + line + CRLF + ok(p, POSITION)], accept=[POSITION], rejects=[E(p + line)])
        return build
    add("overlong-then-good", "size", "an overlong line (1024 bytes), then a good one: the good one still arrives", ["source-read"], SZ + ["skip"], resync(1024))
    add("overlong-4096-then-good", "size", "4096 bytes, then a good line", ["source-read"], SZ + ["skip"], resync(4096))

    def overlong_split(p):
        line = _line_of_size(2048)
        raw = p + line
        return Variant([raw[:700], raw[700:1500], raw[1500:] + CRLF + ok(p, IGATE)], accept=[IGATE], rejects=[E(raw)])
    add("overlong-split-then-good", "size", "an overlong line (2048 bytes) over three reads, then a good one", ["source-read", "invariant"], SZ + ["skip"], overlong_split)

    def overlong_open(p):
        raw = p + _line_of_size(2048)
        return Variant([raw[:1024], raw[1024:]])
    add("overlong-no-terminator", "size", "2048 bytes and no terminator, connection open: nothing is delivered and nothing is counted yet (TooLong is reported at the next LF); the buffer is fixed, nothing grows",
        ["source-read"], ["long", "skip", "lf", "limits"], overlong_open)

    # -- split reads
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
    add("split-mid-line", "split", "a line over two reads, cut in the middle", ["source-read", "invariant"], ["bytewise"], split_at(20))
    add("split-before-crlf", "split", "the line in one read, CRLF in the next", ["source-read", "invariant"], ["bytewise"],
        lambda p: Variant([p + STATUS, CRLF], accept=[STATUS]))
    add("split-between-cr-lf", "split", "cut between CR and LF", ["source-read", "invariant"], ["bytewise", "cr"],
        lambda p: Variant([p + STATUS + b"\r", b"\n"], accept=[STATUS]))
    add("split-after-prefix", "split", "the prefix in one read, the line in the next (the empty profile cuts at 0)", ["source-read", "invariant"], ["bytewise"],
        lambda p: Variant(([p] if p else []) + [STATUS + CRLF], accept=[STATUS]))
    add("split-in-prefix", "split", "cut after the first byte of the prefix (the empty profile cuts at 1 of the line)", ["source-read", "invariant"], ["bytewise"],
        lambda p: Variant([(p + STATUS + CRLF)[:1], (p + STATUS + CRLF)[1:]], accept=[STATUS]))
    add("split-in-prefix-2", "split", "cut after the second byte", ["source-read", "invariant"], ["bytewise"],
        lambda p: Variant([(p + STATUS + CRLF)[:2], (p + STATUS + CRLF)[2:]], accept=[STATUS]))
    add("split-bytewise", "split", "every byte its own read", ["source-read", "invariant"], ["bytewise"],
        lambda p: Variant([bytes([b]) for b in ok(p, POSITION)], accept=[POSITION]))
    add("split-two-lines-straddle", "split", "two lines, the second cut in the middle", ["source-read", "invariant"], ["bytewise"],
        lambda p: Variant([ok(p, STATUS) + (p + POSITION)[:10], (p + POSITION)[10:] + CRLF], accept=[STATUS, POSITION]))
    add("split-bad-prefix", "split", "a bad prefix over two reads: still refused", ["source-read", "invariant"], PRE,
        lambda p: Variant([b"\x3c\xff", b"\x02" + STATUS + CRLF],
                          rejects=[E(b"\x3c\xff\x02" + STATUS) if p else W(b"\x3c\xff\x02" + STATUS)]))

    # -- resync
    add("mid-line-connect", "resync", "the connection starts in the middle of a line (the tail of one), then good lines", ["source-read"], PRE,
        lambda p: Variant([POSITION[20:] + CRLF + ok(p, IGATE)], accept=[IGATE], rejects=[E(POSITION[20:]) if p else W(POSITION[20:])]))
    add("garbage-between-lines", "resync", "good, junk, good: the junk does not break the next line", ["source-read"], G,
        lambda p: Variant([ok(p, STATUS) + p + b"hello world" + CRLF + ok(p, POSITION)], accept=[STATUS, POSITION],
                          rejects=[W(p + b"hello world", b"hello world")]))

    # ------------------------------------------------------------ binary (caps.framing = "raw")
    F = ax25.from_tnc2(STATUS.decode("ascii"))
    F2 = ax25.from_tnc2(POSITION.decode("ascii"))
    BIN = ["hdr", "hdrlen", "binend", "tcp_raw", "tcp_recv", "ax25_parse", "ax25_addr"]

    def unit(p, frame):
        """header + prefix + frame"""
        return hdr(len(p) + len(frame)) + p + frame

    def addb(name, note, cites, build, basis=("source-read",)):
        add(name, "binary", note, list(basis), cites, build, framing="raw")
    addb("bin-valid", "one frame: header, prefix, AX.25 UI frame", BIN + ["test_bin"],
         lambda p: Variant([unit(p, F)], accept=[F], framing="raw"))
    addb("bin-two-frames-one-read", "two frames in one read", BIN,
         lambda p: Variant([unit(p, F) + unit(p, F2)], accept=[F, F2], framing="raw"))
    addb("bin-two-frames-two-reads", "one frame per read", BIN,
         lambda p: Variant([unit(p, F), unit(p, F2)], accept=[F, F2], framing="raw"))
    addb("bin-split-header", "the 2-byte length header split over two reads", BIN + ["bytewise"],
         lambda p: Variant([unit(p, F)[:1], unit(p, F)[1:]], accept=[F], framing="raw"))
    addb("bin-split-in-payload", "the frame cut in the middle of the payload (and inside the prefix for the 3cff01 profile)", BIN + ["bytewise"],
         lambda p: Variant([unit(p, F)[:2 + len(p) // 2 + 1], unit(p, F)[2 + len(p) // 2 + 1:]], accept=[F], framing="raw"))
    addb("bin-bytewise", "every byte its own read", BIN + ["bytewise"],
         lambda p: Variant([bytes([b]) for b in unit(p, F)], accept=[F], framing="raw"))
    addb("bin-zero-length", "length 0000: invalid and TERMINAL (the adapter closes the connection); the frame behind it is never read", BIN + ["terminal", "tcp_close", "test_bounds"],
         lambda p: Variant([hdr(0) + unit(p, F)], rejects=[E(hdr(0))], closed=True, framing="raw"))
    addb("bin-length-equals-prefix", "length equal to the prefix length (0 for the empty profile): no payload, terminal", BIN + ["terminal", "tcp_close"],
         lambda p: Variant([hdr(len(p)) + p + unit(p, F)], rejects=[E(hdr(len(p)))], closed=True, framing="raw"))
    addb("bin-one-byte-payload", "length = prefix + 1: framed, but one byte is no AX.25 frame: one bad frame, the connection stays and the next frame arrives", BIN,
         lambda p: Variant([hdr(len(p) + 1) + p + b"A" + unit(p, F)], accept=[F], rejects=[E(p + b"A")], framing="raw"))
    addb("bin-max", "the largest frame: 1100 bytes after the prefix (length 1100 + prefix), a valid UI frame", BIN + ["test_bounds"],
         lambda p: Variant([unit(p, _ui_of_size(1100))], accept=[_ui_of_size(1100)], framing="raw"))
    addb("bin-over-max", "1101 bytes after the prefix: TooLong and TERMINAL (the connection is closed)", BIN + ["terminal", "tcp_close", "test_bounds"],
         lambda p: Variant([hdr(len(p) + 1101) + p + _ui_of_size(1101)], rejects=[E(hdr(len(p) + 1101))], closed=True, framing="raw"))
    addb("bin-header-ffff", "length FFFF: TooLong and terminal", BIN + ["terminal", "tcp_close", "test_bin"],
         lambda p: Variant([b"\xff\xff" + unit(p, F)], rejects=[E(b"\xff\xff")], closed=True, framing="raw"))
    addb("bin-over-max-then-good", "a good frame first, then a too long length: the first arrives, the connection is closed, a later frame is never read", BIN + ["terminal", "tcp_close"],
         lambda p: Variant([unit(p, F) + hdr(len(p) + 1101) + unit(p, F2)], accept=[F], rejects=[E(hdr(len(p) + 1101))], closed=True, framing="raw"))
    def wrong_body(p):
        return b"\x3c\xff\x02" + F if p else b"\x00\x00\x00" + F[3:]
    addb("bin-wrong-prefix-then-good", "a frame whose prefix is wrong (for the empty profile: a frame with a corrupt address): one bad frame, framing stays aligned, the next frame arrives",
         BIN + ["test_bounds"],
         lambda p: Variant([hdr(len(wrong_body(p))) + wrong_body(p) + unit(p, F2)], accept=[F2], rejects=[E(wrong_body(p))], framing="raw"))
    addb("bin-not-ax25", "a payload of 11 bytes: shorter than 16, no AX.25 frame: one bad frame, then a good frame", BIN,
         lambda p: Variant([hdr(len(p) + 11) + p + b"hello world" + unit(p, F)], accept=[F], rejects=[E(p + b"hello world")], framing="raw"))
    addb("bin-15-bytes", "15 bytes (two addresses and a control byte, no PID): bad frame", BIN,
         lambda p: Variant([hdr(len(p) + 15) + p + F[:15] + unit(p, F)], accept=[F], rejects=[E(p + F[:15])], framing="raw"))
    addb("bin-16-bytes", "16 bytes (two addresses, control and PID, empty information): the smallest frame", BIN,
         lambda p: Variant([unit(p, F[:16])], accept=[F[:16]], framing="raw"))
    addb("bin-not-ui", "control byte 0x13 instead of 0x03: bad frame", BIN,
         lambda p: Variant([unit(p, F[:14] + b"\x13" + F[15:]) + unit(p, F2)], accept=[F2], rejects=[E(p + F[:14] + b"\x13" + F[15:])], framing="raw"))
    addb("bin-bad-pid", "PID 0xCF instead of 0xF0: bad frame", BIN,
         lambda p: Variant([unit(p, F[:15] + b"\xcf" + F[16:]) + unit(p, F2)], accept=[F2], rejects=[E(p + F[:15] + b"\xcf" + F[16:])], framing="raw"))
    addb("bin-lowercase-address", "a lower case letter in the destination address: bad frame", BIN,
         lambda p: Variant([unit(p, bytes([ord("n") << 1]) + F[1:]) + unit(p, F2)], accept=[F2],
                           rejects=[E(p + bytes([ord("n") << 1]) + F[1:])], framing="raw"))

    def nine_digis():
        names = ["APLRT1", "N0CALL-7"] + ["D%d" % i for i in range(1, 10)]
        out = bytearray()
        for i, name in enumerate(names):
            call, ssid = ax25.split_call(name)
            out += ax25.encode_address(call, ssid, cr=False, last=i == len(names) - 1)
        return bytes(out) + b"\x03\xf0" + b"test"
    addb("bin-nine-digipeaters", "nine digipeaters: more than the AX.25 maximum of 8, bad frame", BIN,
         lambda p: Variant([unit(p, nine_digis()) + unit(p, F2)], accept=[F2], rejects=[E(p + nine_digis())], framing="raw"))
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
        rec = {"name": s.name, "group": s.group, "framing": s.framing, "note": s.note, "basis": list(s.basis),
               "sources": [C[k] for k in s.cites], "status": "VERIFIED", "variants": {}}
        for profile in PROFILES:
            v = s.variant(profile)
            rec["variants"][profile] = {"chunks_hex": _hex_list(v.chunks), "eof": v.eof, "accept_hex": _hex_list(v.accept),
                                        "reject_hex": _hex_list(v.reject), "unspecified_hex": [],
                                        "edge_bad_frames": v.bad_frames, "worker_invalid_hex": _hex_list(v.worker_invalid),
                                        "connection_closed": v.closed}
        scenarios.append(rec)
    return {"schema": "dvt-lora-gw-streams/2", "prefix_hex": PREFIX.hex(), "terminator_hex": CRLF.hex(),
            "source_tree": SOURCE_TREE,
            "profiles": {name: pfx.hex() for name, pfx in PROFILES.items()},
            "limits": {"max_prefix": MAX_PREFIX, "max_line_after_prefix": MAX_LINE, "max_binary_after_prefix": MAX_FRAME,
                       "lora_payload_max": 255, "lora_payload_max_note": "informational only: the receive path has no 255 byte limit"},
            "basis": BASIS,
            "verdicts": {"accept": "delivered end to end, in this order, prefix removed (the binary AX.25 frame for framing raw)",
                         "reject": "NOT delivered, the frame as received (text: the line without its terminator; binary: the bytes after the header, "
                                   "the two header bytes for a terminal length)",
                         "unspecified": "always empty since schema 2 (every case is decided by the source lines in `sources`)",
                         "edge_bad_frames": "increment of the edge modem counter dv_edge_modem_total{result=\"bad_frame\"} for the whole stream "
                                            "(framing errors; for binary also a payload that is no AX.25 frame)",
                         "worker_invalid_hex": "text lines the edge delivers (counted rx) that the worker's TNC2 parser refuses "
                                               "(dv_pkt_reject_total{reason=\"invalid\"}); the line as the worker sees it (prefix removed)",
                         "connection_closed": "the adapter closes the connection after this stream (binary terminal length)"},
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
