# SPDX-License-Identifier: MIT
"""Dire Wolf as the independent AX.25 / APRS / KISS oracle (HWFREE1 item 3, docs/direwolf-cases.md).

Host side of rig/Dockerfile.direwolf + rig/direwolf-entry.py. Dire Wolf
(wb2osz/direwolf, GPL-2.0-or-later) runs only as processes inside one throw-away
container (--network none, loopback only); nothing of its code or text is in this
repository, only the formats it printed and the facts we read from them.

Two directions, the two vector files under vectors/direwolf/:

  rx_frames.json  TNC2 line -> gen_packets (AFSK audio) -> atest (AX.25 frame bytes
                  by Dire Wolf's encoder, read back through its demodulator) and
                  -> direwolf over stdin -> the real bytes of its KISS TCP port.
                  Our dvtest.ax25 must reproduce both byte for byte.
  kiss_tx.json    KISS bytes WE built (and deliberately broken ones) -> Dire Wolf's
                  KISS TCP port; KISSCOPY copies what it accepted to a second client,
                  kissutil prints the TNC2 it decodes, the direwolf log shows what the
                  TNC did. Plus kissutil's own TNC2 -> KISS encoder.

The pure functions (case tables, record builders, comparisons) are unit tested
without Docker (cases/unit/test_oracle_direwolf.py); run_job() needs the image.

    python3 -m dvtest.oracle_direwolf run --out-dir vectors/direwolf

A third command checks the modem loop of the DP15 tier B rig (docs/dp15-cases.md) without a rig: two Dire Wolf
instances in one throw-away container, each one's transmit audio relayed to the other's audio input, frames sent into
one KISS port and read from the other:

    python3 -m dvtest.oracle_direwolf loop [--frames 6] [--gap 5]

A fourth command makes Dire Wolf the oracle of the DIGI1 fill-in digipeater (docs/digi1-cases.md): node B of the same loop with MYCALL N0TEST-10 and
`DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$`, `DEDUPE 30`; node A sends the path, key and timeline vectors of dvtest/digi1_oracle.py and reads the repeats:

    python3 -m dvtest.oracle_direwolf digi [--out vectors/direwolf/digi_oracle.json]
"""
import argparse
import json
import os
import re
import subprocess
import sys

from . import ax25, limits

IMAGE = "dvt-direwolf:local"
RUN_TIMEOUT = 240
RX_SCHEMA = "dvt-direwolf-rx/1"
TX_SCHEMA = "dvt-direwolf-kiss-tx/1"
PRE_FLAGS = 32            # flag octets before a frame in gen_packets (read from its output length)
POST_FLAGS = 2
SAMPLE_RATE = 44100
BAUD = 1200

# ------------------------------------------------------------------ rx cases

# (name, TNC2 line, what it exercises). The lines use N0CALL / N0TEST / N0DIG / XLX999 style names only.
RX_CASES = [
    ("pos-digi", "N0CALL-7>APRS,WIDE1-1:!0000.00N/00000.00E>test", "position with one digipeater, destination APRS"),
    ("status-nodigi", "N0CALL>APDW18:>status", "no digipeater, SSID 0 both, status report"),
    ("message", "N0CALL-1>APRS::N0TEST-1 :Hello{123", "message with a message number"),
    ("digi-used-first", "N0CALL-15>N0TEST-1,N0DIG-2*,WIDE2-1:hello", "star on the first of two digipeaters, SSID 15"),
    ("digi-used-last", "N0CALL-15>N0TEST-1,N0DIG-2,WIDE2-1*:hello", "star on the last digipeater: the one before it is marked as repeated too"),
    ("digi-8", "A>B,D1,D2,D3,D4,D5,D6,D7,D8:x", "eight digipeaters (the maximum), one-character callsigns"),
    ("calls-short", "A>B:x", "shortest frame: 1-character callsigns, one information byte"),
    ("calls-long", "ABCDEF-9>GHIJKL-10:ssid", "6-character callsigns, SSID 9 and 10"),
    ("info-empty", "N0CALL>APRS:", "empty information field"),
    ("info-fend-fesc", "N0CALL>APRS:m<0xc0>x<0xdb>y<0xdb><0xdc><0xdb><0xdd><0xc0><0xc0>",
     "information bytes C0, DB, DB DC, DB DD, C0 C0: the KISS escape cases from a real demodulator"),
    ("info-ones", "N0CALL>APRS:" + "<0xff>" * 10, "ten FF bytes: bit stuffing on the air"),
    ("info-340", "N0CALL>APRS:" + "x" * 340, "340 information bytes (over 256)"),
    ("info-tab-cr", "N0CALL>APRS:a\tb<0x0d>", "tab and carriage return in the information field"),
    ("aprs-position", "N0CALL-9>APDW18,WIDE1-1,WIDE2-1:!4903.50N/07201.75W-Test 001234", "position with comment, two digipeaters"),
    ("lora-status", "N0CALL-7>APLRT1:>Test status", "LoRa APRS shaped line 1 (docs/direwolf-cases.md, airtime table)"),
    ("lora-position", "N0CALL-7>APLRT1,WIDE1-1:!0000.00N/00000.00E[Test", "LoRa APRS shaped line 2"),
    ("lora-igate", "N0CALL-7>APLRG1,WIDE1-1:=0000.00N/00000.00E-LoRa iGate test", "LoRa APRS shaped line 3"),
]
# negatives: Dire Wolf refuses or changes them; the record says what happened
RX_NEGATIVE_CASES = [
    ("neg-lowercase", "n0call>aprs:lower", "lower case callsign: no frame"),
    ("neg-9-digis", "A>B,D1,D2,D3,D4,D5,D6,D7,D8,D9:x", "nine digipeaters: the ninth is dropped silently"),
]


def rx_job():
    return {"mode": "rx", "baud": BAUD,
            "cases": [{"name": n, "tnc2": t} for n, t, _ in RX_CASES + RX_NEGATIVE_CASES]}


def rx_records(result):
    """Vector records from an rx job result: one per case. `ours_*` is dvtest.ax25,
    `dw_*` is what Dire Wolf produced. The KISS frames are assigned in order to the
    cases that decoded."""
    notes = {n: note for n, _t, note in RX_CASES + RX_NEGATIVE_CASES}
    kiss = ax25.kiss_decode(bytes.fromhex(result["kiss_rx_hex"]))
    kiss_raw = _kiss_frames_raw(bytes.fromhex(result["kiss_rx_hex"]))
    records = []
    ki = 0
    for case in result["cases"]:
        rec = {"schema": RX_SCHEMA, "case": case["name"], "note": notes.get(case["name"], ""),
               "tnc2": case["tnc2"], "dw_generated": case["generated"], "samples": case["samples"],
               "sample_rate": case["sample_rate"], "dw_ax25_hex": case["atest_frame_hex"],
               "dw_text": case["atest_text"]}
        try:
            ours = ax25.from_tnc2(case["tnc2"])
            rec["ours_ax25_hex"] = ours.hex()
        except ValueError as exc:
            ours = None
            rec["ours_ax25_hex"] = None
            rec["ours_error"] = str(exc)
        rec["ax25_match"] = ours is not None and ours.hex() == case["atest_frame_hex"]
        if case["atest_frame_hex"]:
            if ki < len(kiss):
                port, cmd, data, _raw = kiss[ki]
                rec["dw_kiss_hex"] = kiss_raw[ki].hex()
                rec["kiss_data_equals_ax25"] = (port, cmd) == (0, 0) and data.hex() == case["atest_frame_hex"]
                rec["ours_kiss_hex"] = ax25.kiss_encode(0, 0, bytes.fromhex(case["atest_frame_hex"])).hex()
                rec["kiss_match"] = rec["dw_kiss_hex"] == rec["ours_kiss_hex"]
            else:
                rec["dw_kiss_hex"] = None
                rec["kiss_match"] = False
            ki += 1
            frame = bytes.fromhex(case["atest_frame_hex"])
            bits = ax25.air_bits(frame, preamble_flags=PRE_FLAGS, postamble_flags=POST_FLAGS)
            rec["ours_air_bits"] = bits
            only = ax25.air_bits(frame)
            rec["ours_frame_air_bits"] = only
            rec["ours_frame_air_ms"] = "%.3f" % float(ax25.air_ms(only, BAUD))
            # samples = quiet time + bits * 44100 / 1200 (quiet time is 32 to 33 symbols, random)
            rec["quiet_samples"] = case["samples"] - bits * SAMPLE_RATE / BAUD
        records.append(rec)
    return records


def _kiss_frames_raw(stream):
    """Each KISS frame as the exact bytes on the wire, FEND to FEND (leading
    and trailing), in order. The oracle sends two FEND bytes between frames."""
    out = []
    cur = bytearray()
    for b in stream:
        if b == ax25.FEND:
            if cur:
                out.append(bytes([ax25.FEND]) + bytes(cur) + bytes([ax25.FEND]))
                cur = bytearray()
        else:
            cur.append(b)
    return out


# ------------------------------------------------------------------ tx cases

def _ui(info=b"hi", dest="APRS", src="N0CALL-7"):
    return ax25.from_tnc2("%s>%s:" % (src, dest) + ax25.info_to_text(info))


def tx_cases():
    """(name, bytes sent, what it exercises). Each case is sent on its own new
    client connection (the TNC's KISS decoder state is per client). Bytes are built
    by dvtest.ax25 or by hand."""
    ui = _ui(b"!0000.00N/00000.00E>hi")
    esc = _ui(b"a\xc0b\xdbc\xdb\xdc\xdb\xdd")
    good = ax25.kiss_encode(0, 0, _ui(b"after"))
    bad_escape = bytes([ax25.FEND, 0x00]) + ui[:16] + bytes([ax25.FESC, 0x41]) + ui[16:] + bytes([ax25.FEND])
    cases = [
        ("data-ui", ax25.kiss_encode(0, 0, ui), "canonical UI data frame, port 0"),
        ("data-escapes", ax25.kiss_encode(0, 0, esc), "information bytes C0, DB, DB DC, DB DD escaped"),
        ("data-port1", ax25.kiss_encode(1, 0, _ui(b"p1")), "port nibble 1 on a one-channel TNC"),
        ("param-txdelay", ax25.kiss_encode(0, ax25.CMD_TXDELAY, b"\x1e"), "TXDELAY 30 (10 ms units)"),
        ("param-p", ax25.kiss_encode(0, ax25.CMD_P, b"\x3f"), "persistence 63"),
        ("param-slottime", ax25.kiss_encode(0, ax25.CMD_SLOTTIME, b"\x0a"), "slot time 10 (10 ms units)"),
        ("param-txtail", ax25.kiss_encode(0, ax25.CMD_TXTAIL, b"\x05"), "TXtail 5 (10 ms units)"),
        ("param-fullduplex-1", ax25.kiss_encode(0, ax25.CMD_FULLDUPLEX, b"\x01"), "full duplex on"),
        ("param-fullduplex-0", ax25.kiss_encode(0, ax25.CMD_FULLDUPLEX, b"\x00"), "full duplex off"),
        ("param-sethardware", ax25.kiss_encode(0, ax25.CMD_SETHARDWARE, b"TNC:"), "SetHardware with a text argument"),
        ("param-port1-txdelay", ax25.kiss_encode(1, ax25.CMD_TXDELAY, b"\x1e"), "TXDELAY for port 1"),
        ("cmd-unknown-7", ax25.kiss_encode(0, 7, b"\x01"), "command nibble 7 (undefined)"),
        ("cmd-return", bytes([ax25.FEND, 0xFF, ax25.FEND]), "Return (0xFF)"),
        ("frame-empty", bytes([ax25.FEND, ax25.FEND]), "two FEND, no frame"),
        ("frame-type-only", bytes([ax25.FEND, 0x00, ax25.FEND]), "data frame with no data"),
        ("frame-short-3", ax25.kiss_encode(0, 0, b"\x01\x02\x03"), "data frame of 3 bytes (not an AX.25 frame)"),
        ("frame-14-addresses-only", ax25.kiss_encode(0, 0, ui[:14]), "two addresses and nothing else (14 bytes)"),
        ("frame-15-control-only", ax25.kiss_encode(0, 0, ui[:15]), "two addresses and a control byte (15 bytes)"),
        ("no-leading-fend", ax25.kiss_encode(0, 0, ui)[1:], "no opening FEND on a new connection"),
        ("double-fend", bytes([ax25.FEND]) + ax25.kiss_encode(0, 0, ui), "two FEND in front of the frame"),
        ("garbage-before", b"AB" + ax25.kiss_encode(0, 0, ui), "bytes 41 42 before the opening FEND"),
        ("bad-escape", bad_escape, "FESC followed by 0x41 (neither TFEND nor TFESC) inside the information field"),
        ("bad-escape-then-good", bad_escape + good, "the same, followed by a good frame in the same stream"),
        ("truncated-then-frame", ax25.kiss_encode(0, 0, ui)[:-1] + good, "a frame without its closing FEND directly followed by a complete frame"),
        ("fend-in-info", bytes([ax25.FEND, 0x00]) + ui[:18] + bytes([ax25.FEND]) + ui[18:] + bytes([ax25.FEND]),
         "an unescaped FEND in the middle of the information field"),
        ("info-256", ax25.kiss_encode(0, 0, _ui(b"x" * 256)), "256 information bytes"),
        ("info-1024", ax25.kiss_encode(0, 0, _ui(b"x" * 1024)), "1024 information bytes"),
        ("info-2029", ax25.kiss_encode(0, 0, _ui(b"x" * 2029)), "2029 information bytes: 2048 bytes from FEND to FEND (MAX_KISS_LEN counts both FEND and the type byte)"),
        ("info-2030", ax25.kiss_encode(0, 0, _ui(b"x" * 2030)), "2030 information bytes: 2049 bytes FEND to FEND, one more than the TNC's KISS buffer"),
    ]
    return cases + dvr_boundary_cases()


def _ui_len(total, info_fill=b"x", specials=b""):
    """A UI frame of exactly `total` bytes (16 bytes of header, control and PID, then the information
    field); `specials` are the first information bytes, the rest is `info_fill` repeated."""
    n = total - 16
    if n < len(specials):
        raise ValueError("frame too short for the special bytes")
    return _ui(specials + info_fill * (n - len(specials)))


def dvr_boundary_cases():
    """Cases added for the DV-Router KISS receive rules (docs/direwolf-cases.md, "KISS receive decoder"):
    port nibble other than 0, command nibbles 7 to 14, the exact limit of 1100 UNESCAPED bytes after the
    type byte with and without escapes inside, and a FESC directly before a FEND. Dire Wolf's answers are
    recorded; DV-Router's side is dvtest.ax25.DvrouterKiss."""
    cases = []
    for port in (2, 15):
        cases.append(("data-port%d" % port, ax25.kiss_encode(port, 0, _ui(b"p%d" % port)),
                      "port nibble %d on a one-channel TNC" % port))
    for cmd in range(8, 15):
        cases.append(("cmd-unknown-%d" % cmd, ax25.kiss_encode(0, cmd, b"\x01"), "command nibble %d (undefined)" % cmd))
    plain_1100 = _ui_len(1100)
    plain_1101 = _ui_len(1101)
    # 10 x C0 and 10 x DB among the information bytes: 20 escape pairs, so 1100 unescaped bytes are
    # 1120 on the wire after the type byte; only the unescaped count may matter
    specials = bytes([ax25.FEND, ax25.FESC]) * 10
    esc_1100 = _ui_len(1100, specials=specials)
    esc_1101 = _ui_len(1101, specials=specials)
    cases += [
        ("limit-1100-plain", ax25.kiss_encode(0, 0, plain_1100), "1100 data bytes after the type byte, no escapes"),
        ("limit-1101-plain", ax25.kiss_encode(0, 0, plain_1101), "1101 data bytes after the type byte, no escapes"),
        ("limit-1100-escapes", ax25.kiss_encode(0, 0, esc_1100), "1100 unescaped data bytes, 20 of them escaped (1120 on the wire)"),
        ("limit-1101-escapes", ax25.kiss_encode(0, 0, esc_1101), "1101 unescaped data bytes, 20 of them escaped (1121 on the wire)"),
        ("fesc-before-fend", bytes([ax25.FEND, 0x00]) + _ui(b"fesc") + bytes([ax25.FESC]) + ax25.kiss_encode(0, 0, _ui(b"after")),
         "FESC directly before the closing FEND, then a complete frame in the same stream"),
    ]
    return cases


TX_KISSUTIL_CASES = [
    ("ku-pos-digi", "N0CALL-7>APRS,WIDE1-1:!0000.00N/00000.00E>test"),
    ("ku-digi-used", "N0CALL-15>N0TEST-1,N0DIG-2*,WIDE2-1:hello"),
    ("ku-escapes", "N0CALL>APRS:m<0xc0>x<0xdb>y<0xdb><0xdc><0xdb><0xdd>z"),
    ("ku-status", "N0CALL>APDW18:>status"),
]


def tx_job():
    return {"mode": "tx",
            "cases": [{"name": n, "send_hex": b.hex(), "settle": 0.6} for n, b, _ in tx_cases()],
            "tnc2_cases": [{"name": n, "tnc2": t} for n, t in TX_KISSUTIL_CASES]}


_BLOCK_HEAD = re.compile(r"^<<< (.+?) from KISS client application(?:, channel (\d+))?, total length = (\d+)\s*$")
_HEX_LINE = re.compile(r"^\s+[0-9a-f]{3}:\s+((?:[0-9a-f]{2} ?)+)")
_UNWRAP = "Packet content after removing KISS framing and any escapes:"
_NOISE_LINES = re.compile(r"^(\[\d+L\] |Attached to KISS|Ready to accept|KISS client application \d+ on TCP port)")


def parse_debug_log(log):
    """Facts from a `direwolf -d n -d n` log slice: `received`: list of {kind,
    channel, length, hex} for every '<<< ... from KISS client application' block (kind
    is the word(s) Dire Wolf prints: 'Data frame', 'TXDELAY', 'Rejected Noise', ...),
    `unwrapped`: hex of every 'Packet content after removing KISS framing' block (the
    TNC's own unescaping, without the type byte), `messages`: the other lines, minus
    the transmit-queue echo and the client attach messages."""
    received, unwrapped, messages = [], [], []
    lines = log.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _BLOCK_HEAD.match(line)
        is_unwrap = line.strip() == _UNWRAP
        if m or is_unwrap:
            j = i + 1
            hexes = []
            while j < len(lines):
                h = _HEX_LINE.match(lines[j])
                if not h:
                    break
                hexes.append(h.group(1).replace(" ", ""))
                j += 1
            if m:
                received.append({"kind": m.group(1), "channel": int(m.group(2)) if m.group(2) else None,
                                 "length": int(m.group(3)), "hex": "".join(hexes)})
            else:
                unwrapped.append("".join(hexes))
            i = j
            continue
        if line.strip() and not _NOISE_LINES.match(line) and not _HEX_LINE.match(line):
            messages.append(line.strip())
        i += 1
    return {"received": received, "unwrapped": unwrapped, "messages": messages}


def parse_kissutil(text):
    """kissutil -v output: frames it decoded ('[port] TNC2 line') and the lines it
    printed about a frame it refused."""
    frames = [m.group(0) for m in re.finditer(r"^\[(\d+)\] .*$", text, re.M)]
    refused = [ln.strip() for ln in text.splitlines() if re.search(r"Invalid|not in allowable|ERROR", ln)]
    return {"frames": frames, "refused": refused}


# Cases whose bytes in Dire Wolf's answer are not ours: an overlong KISS message makes it
# copy and print a buffer that holds stale memory (different bytes every run). Only the
# lengths and its messages are kept.
VOLATILE_CASES = ("info-2030",)


def _scrub_volatile(rec):
    """Replace every hex field of a volatile record by its length (bytes)."""
    rec["volatile"] = True
    rec["dw_copy_len"] = len(rec.pop("dw_copy_hex")) // 2
    rec["dw_copy_equals_sent"] = False
    rec["dw_copy_decode"] = [{"port": f["port"], "cmd": f["cmd"], "data_len": len(f["data_hex"]) // 2}
                             for f in rec["dw_copy_decode"]]
    rec["dw_unwrapped_len"] = [len(h) // 2 for h in rec.pop("dw_unwrapped_hex")]
    for r in rec["dw_received"]:
        r["hex_len"] = len(r.pop("hex")) // 2
    rec["dw_client_rx_len"] = len(rec.pop("dw_client_rx_hex")) // 2
    rec["kissutil_frames"] = [f.split(":", 1)[0] for f in rec["kissutil_frames"]]
    return rec


def tx_records(result):
    """Vector records from a tx job result, joined with the case table."""
    meta = {n: (b, note) for n, b, note in tx_cases()}
    records = []
    for case in result["cases"]:
        if case["name"] not in meta:
            continue
        sent, note = meta[case["name"]]
        copy = bytes.fromhex(case["copy_hex"])
        dbg = parse_debug_log(case["direwolf_log"])
        ku = parse_kissutil(case["kissutil_text"])
        ours = [{"port": f[0], "cmd": f[1], "data_hex": f[2].hex()} for f in ax25.kiss_decode(sent)]
        rec = {"schema": TX_SCHEMA, "case": case["name"], "note": note, "sent_hex": sent.hex(),
               "ours_decode": ours,
               "dw_received": dbg["received"], "dw_unwrapped_hex": dbg["unwrapped"], "dw_messages": dbg["messages"],
               "dw_client_rx_hex": case.get("client_rx_hex", ""),
               "dw_copy_hex": case["copy_hex"], "dw_copy_equals_sent": copy == sent,
               "dw_copy_decode": [{"port": f[0], "cmd": f[1], "data_hex": f[2].hex()} for f in ax25.kiss_decode(copy)],
               "kissutil_frames": ku["frames"], "kissutil_refused": ku["refused"]}
        records.append(_scrub_volatile(rec) if case["name"] in VOLATILE_CASES else rec)
    return records


def kissutil_records(result):
    """Records of kissutil's TNC2 -> KISS direction: its bytes versus ours."""
    records = []
    for case in result["cases"]:
        if "tnc2" not in case:
            continue
        ours = ax25.kiss_encode(0, 0, ax25.from_tnc2(case["tnc2"]))
        copy = bytes.fromhex(case["copy_hex"])
        records.append({"schema": TX_SCHEMA, "case": case["name"], "direction": "kissutil-tnc2-to-kiss",
                        "tnc2": case["tnc2"], "dw_kiss_hex": case["copy_hex"], "ours_kiss_hex": ours.hex(),
                        "kiss_match": copy == ours})
    return records


# ------------------------------------------------------------------ container

def docker_argv(name, image=IMAGE, env=None):
    """docker run words: stdin job, no network, the repository's resource limits, a
    read-only root with a small tmpfs, no capabilities."""
    return (["docker", "run", "--rm", "-i", "--name", name, "--network", "none"]
            + limits.docker_run_args(env)
            + ["--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", image])


def run_job(job, image=IMAGE, timeout=RUN_TIMEOUT):
    """Run one job in a throw-away container; returns the parsed JSON result. A
    timed-out container is removed by name."""
    name = "dvt-direwolf-%d" % os.getpid()
    try:
        done = subprocess.run(docker_argv(name, image), input=json.dumps(job).encode("ascii"),
                              capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
        tail = (exc.stderr or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError("dvt-direwolf job timed out after %d s; progress: %s" % (timeout, tail))
    if done.returncode != 0:
        raise RuntimeError("dvt-direwolf exited %s: %s" % (done.returncode, done.stderr.decode("utf-8", "replace")[-500:]))
    return json.loads(done.stdout.decode("utf-8"))


def loop_verdict(result):
    """(ok, lines) for the result of the job `looptest`: every frame came out of the other instance's KISS port equal to
    what went in, the loop passed at least one frame in each direction."""
    if result.get("error"):
        return False, ["error: %s" % result["error"]]
    frames = result.get("frames") or []
    lines = ["%d %s %s %.3f s" % (f["seq"], f["direction"], "equal" if f["equal"] else "MISSING/DIFFERENT", f["latency_s"]) for f in frames]
    ok = bool(frames) and all(f["equal"] for f in frames) and {f["direction"] for f in frames} >= {"a->b", "b->a"}
    return ok, lines


def digi_main(out=None):
    """Run the DIGI1 oracle job (docs/digi1-cases.md): write the vectors file and print one line per path vector; exit 1 when a vector
    that Dire Wolf should treat like the model differs (a documented deviation is only listed)."""
    from dvtest import digi1_oracle
    steps = digi1_oracle.plan()
    version = run_job({"mode": "versions"}).get("direwolf", "")
    result = run_job(digi1_oracle.job(steps), timeout=digi1_oracle.JOB_TIMEOUT_S)
    if result.get("error"):
        print("error: %s" % result["error"])
        return 1
    doc = digi1_oracle.file_doc(result, steps, version)
    digi1_oracle.write(doc, out or digi1_oracle.PATH)
    by_case = {r["case"]: r for r in doc["records"]}
    bad = 0
    for step in steps:
        g = step["vector"]
        if g is None:
            continue
        problems, notes = digi1_oracle.oracle_problems(g, by_case[g.id])
        verdict, text = digi1_oracle.compare_dw(g, by_case[g.id])
        print("%-4s %-13s %s" % (g.id, verdict, text))
        bad += len(problems)
    print("timeline (0 10 29 31 45 s): %s" % digi1_oracle.timeline_outcomes(by_case))
    print("digi oracle %s" % ("OK" if not bad else "DISAGREES with the model on %d vector(s)" % bad))
    return 1 if bad else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="oracle_direwolf", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the rx and tx jobs and write the vector files")
    run.add_argument("--out-dir", default="vectors/direwolf")
    run.add_argument("--only", choices=("rx", "tx"), default=None,
                     help="run and write one direction only (the other file is left as it is)")
    loop = sub.add_parser("loop", help="check the modem loop of two Dire Wolf instances (tier B) in one throw-away container")
    loop.add_argument("--frames", type=int, default=6)
    loop.add_argument("--info-len", type=int, default=40)
    loop.add_argument("--gap", type=float, default=5.0, help="seconds between frames (a node is deaf for about its own air time)")
    digi = sub.add_parser("digi", help="DIGI1 oracle: the path vectors, key vectors and the duplicate timeline through a Dire Wolf digipeater "
                                       "(DIGIPEAT 0 0 ^WIDE1-1$ ^WIDE1-1$, DEDUPE 30) on the modem loop; writes vectors/direwolf/digi_oracle.json")
    digi.add_argument("--out", default=None, help="vectors file (default vectors/direwolf/digi_oracle.json)")
    args = parser.parse_args(argv)
    if args.cmd == "digi":
        return digi_main(args.out)
    if args.cmd == "loop":
        result = run_job({"mode": "looptest", "frames": args.frames, "info_len": args.info_len, "gap": args.gap})
        ok, lines = loop_verdict(result)
        print("\n".join(lines))
        print("loop %s" % ("OK" if ok else "FAILED"))
        return 0 if ok else 1
    os.makedirs(args.out_dir, exist_ok=True)
    version = run_job({"mode": "versions"})
    rx = run_job(rx_job()) if args.only != "tx" else None
    tx = run_job(tx_job()) if args.only != "rx" else None
    docs = []
    if rx is not None:
        docs.append(("rx_frames.json", {"schema": RX_SCHEMA, "tool": version["direwolf"], "image": IMAGE, "baud": BAUD,
                                         "pre_flags": PRE_FLAGS, "post_flags": POST_FLAGS, "kiss_stream_hex": rx["kiss_rx_hex"],
                                         "records": rx_records(rx)}))
    if tx is not None:
        docs.append(("kiss_tx.json", {"schema": TX_SCHEMA, "tool": version["direwolf"], "image": IMAGE,
                                       "records": tx_records(tx), "kissutil_encoder": kissutil_records(tx)}))
    for name, doc in docs:
        with open(os.path.join(args.out_dir, name), "w", encoding="ascii") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True, ensure_ascii=True)
            fh.write("\n")
    bad = []
    for name, doc in docs:
        if name == "rx_frames.json":
            bad = [r["case"] for r in doc["records"] if r.get("dw_generated") and not r["case"].startswith("neg-") and not (r["ax25_match"] and r.get("kiss_match"))]
            print("rx cases %d, mismatches %s" % (len(doc["records"]), bad))
        else:
            print("tx cases %d" % len(doc["records"]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
