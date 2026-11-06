#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Driver inside dvt-direwolf:local (docs/direwolf-cases.md). Ours; the Dire Wolf programs
(GPL-2.0-or-later) are only started as processes: gen_packets, atest, direwolf, kissutil.

Reads ONE JSON job on stdin, writes ONE JSON result on stdout. No network (the
container runs with --network none; everything is on loopback), no files outside
/tmp (the container is read-only with a tmpfs there).

Jobs (field "mode"):
  versions
      {} -> {"direwolf": first line of `direwolf -h`, "tools": [...]}
  rx    {"cases": [{"name", "tnc2"}], "baud": 1200}
      per case: gen_packets writes the AFSK audio of the TNC2 line (one file per
      line, no trailing newline: gen_packets keeps the newline of a line in the
      information field), the sample count is read from the WAV header, `atest -h`
      decodes the audio and its hex dump gives the AX.25 frame bytes. Then all the
      audio is fed to ONE direwolf over stdin (ADEVICE stdin null) and the raw bytes
      of its KISS TCP port are captured. Result per case: name, tnc2, generated
      (bool), samples, atest_frame_hex, atest_text, and for the whole run
      kiss_rx_hex (the byte stream of the KISS client) and direwolf_log.
  tx    {"cases": [{"name", "send_hex", "settle"}], "tnc2_cases": [{"name", "tnc2"}]}
      direwolf with KISSCOPY, KISS client A sends the bytes of each case, client B
      records the copy it gets, kissutil -v (client C) prints what it decodes and
      the direwolf log shows what the TNC did. tnc2_cases: kissutil -f turns a TNC2
      line into a KISS frame and sends it; B records the copy. Result per case:
      name, client_rx_hex (what the sending client got back from the TNC), copy_hex,
      kissutil_text, direwolf_log (the text printed while the case ran, Latin-1, one string).
"""
import json
import os
import pty
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tty

ANSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")
RATE = 44100
PORT = 8001
SILENCE_S = 0.4


class Tail:
    """Collects everything a stream yields into one byte buffer (thread)."""

    def __init__(self, stream):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.t = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self.t.start()

    def _run(self, stream):
        while True:
            try:
                chunk = os.read(stream.fileno(), 65536)
            except OSError:                    # EIO on a pty master once the program has exited
                return
            if not chunk:
                return
            with self.lock:
                self.buf += chunk

    def mark(self):
        with self.lock:
            return len(self.buf)

    def since(self, pos):
        with self.lock:
            return bytes(self.buf[pos:])


class SockTail(Tail):
    def __init__(self, sock):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.sock = sock
        self.t = threading.Thread(target=self._run_sock, daemon=True)
        self.t.start()

    def _run_sock(self):
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            with self.lock:
                self.buf += chunk


def pty_pair():
    """(master file object, slave fd): the program under test writes to the slave, so its stdio
    is line buffered like on a terminal (Dire Wolf logs in blocks when stdout is a pipe), raw so
    no CR is added."""
    master, slave = pty.openpty()
    tty.setraw(slave)
    return os.fdopen(master, "rb", buffering=0), slave


def clean(raw):
    return ANSI.sub(b"", raw).decode("latin-1")


def wav_samples(path):
    """Samples of a mono 16 bit PCM WAV and the raw sample bytes (header parsed by hand)."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos = 12
    rate = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            _fmt, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", body[:16])
            if ch != 1 or bits != 16:
                raise ValueError("expected mono 16 bit")
        elif cid == b"data":
            return rate, body
        pos += 8 + size + (size & 1)
    raise ValueError("no data chunk")


def hexdump_frame(text):
    """Frame bytes from the hex dump lines of `atest -h` ('  000:  82 a0 ...')."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s+[0-9a-f]{3}:\s+((?:[0-9a-f]{2} ?)+)", line)
        if m:
            out.append(m.group(1).replace(" ", ""))
    return "".join(out)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, timeout=60, **kw)


def start_direwolf(extra_conf="", extra_args=()):
    conf = ("ADEVICE stdin null\nACHANNELS 1\nCHANNEL 0\nMYCALL N0CALL\nMODEM 1200\n"
            "AGWPORT 0\nKISSPORT %d\n%s" % (PORT, extra_conf))
    with open("/tmp/dw.conf", "w") as fh:
        fh.write(conf)
    master, slave = pty_pair()
    p = subprocess.Popen(["direwolf", "-c", "/tmp/dw.conf", "-r", str(RATE), "-t", "0"] + list(extra_args) + ["-"],
                         stdin=subprocess.PIPE, stdout=slave, stderr=slave)
    os.close(slave)
    log = Tail(master)
    for _ in range(100):
        if b"Ready to accept KISS TCP client" in log.since(0):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("direwolf did not open the KISS port: " + clean(log.since(0))[-500:])
    return p, log


def connect(wait=6.0):
    """TCP client of the KISS port; retries while Dire Wolf has no free client slot
    (it frees one a moment after a client closes)."""
    end = time.time() + wait
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
            s.settimeout(None)
            return s
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.3)


def stop(p):
    try:
        p.stdin.close()
    except OSError:
        pass
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


# ------------------------------------------------------------------ modes

def mode_versions(_job):
    head = run(["direwolf", "-h"]).stdout
    return {"direwolf": clean(head).splitlines()[0] if head else "",
            "tools": ["direwolf", "gen_packets", "atest", "kissutil", "decode_aprs"]}


def mode_rx(job):
    cases = job["cases"]
    baud = int(job.get("baud", 1200))
    results = []
    pcm = bytearray()
    silence = b"\0\0" * int(RATE * SILENCE_S)
    expected = 0
    for i, case in enumerate(cases):
        txt = "/tmp/c%d.txt" % i
        wav = "/tmp/c%d.wav" % i
        with open(txt, "wb") as fh:
            fh.write(case["tnc2"].encode("latin-1"))
        cmd = ["gen_packets", "-r", str(RATE), "-o", wav]
        if baud != 1200:
            cmd += ["-B", str(baud)]
        gen = run(cmd + [txt])
        rate, samples = wav_samples(wav)
        n = len(samples) // 2
        res = {"name": case["name"], "tnc2": case["tnc2"], "generated": n > 0, "samples": n,
               "sample_rate": rate, "atest_frame_hex": "", "atest_text": ""}
        if n:
            at = run(["atest", "-h"] + (["-B", str(baud)] if baud != 1200 else []) + [wav])
            text = clean(at.stdout)
            res["atest_frame_hex"] = hexdump_frame(text)
            m = re.search(r"^\[0\] (.*)$", text, re.M)
            res["atest_text"] = m.group(1) if m else ""
            if res["atest_frame_hex"]:
                expected += 1
            pcm += samples + silence
        results.append(res)
    p, log = start_direwolf()
    sock = connect()
    tail = SockTail(sock)
    time.sleep(0.3)
    p.stdin.write(bytes(pcm))
    p.stdin.flush()
    deadline = time.time() + 20 + len(pcm) / (2 * RATE) * 0.3
    while time.time() < deadline:
        if tail.since(0).count(b"\xc0") >= 2 * expected and expected:
            break
        time.sleep(0.2)
    time.sleep(0.5)
    kiss = tail.since(0)
    stop(p)
    time.sleep(0.2)
    return {"cases": results, "kiss_rx_hex": kiss.hex(), "direwolf_log": clean(log.since(0)),
            "expected_frames": expected}


def settle(sess, minimum, quiet=0.6, limit=5.0):
    """Sleep at least `minimum`, then until the Dire Wolf log has been quiet for `quiet`
    seconds (at most `limit` in all)."""
    t0 = time.time()
    time.sleep(minimum)
    last = sess.log.mark()
    stamp = time.time()
    while time.time() - t0 < limit:
        time.sleep(0.1)
        now = sess.log.mark()
        if now != last:
            last, stamp = now, time.time()
        elif time.time() - stamp >= quiet:
            return


class TxSession:
    """direwolf with KISSCOPY, client B (raw copy) and kissutil (client C), all on loopback."""

    def __init__(self):
        os.makedirs("/tmp/ktx", exist_ok=True)
        os.makedirs("/tmp/krx", exist_ok=True)
        # -d n twice: the log shows the bytes the TNC took from the client (and the
        # noise it threw away), and the frame after it removed the KISS framing
        self.p, self.log = start_direwolf("KISSCOPY\n", ["-d", "n", "-d", "n"])
        self.b = connect()
        self.btail = SockTail(self.b)
        kmaster, kslave = pty_pair()
        self.ku = subprocess.Popen(["kissutil", "-h", "127.0.0.1", "-p", str(PORT), "-v", "-f", "/tmp/ktx", "-o", "/tmp/krx"],
                                   stdin=subprocess.DEVNULL, stdout=kslave, stderr=kslave)
        os.close(kslave)
        self.kutail = Tail(kmaster)
        time.sleep(1.0)

    def alive(self):
        return self.p.poll() is None

    def close(self):
        self.ku.terminate()
        stop(self.p)


def progress(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def wait_count(sess, needle, before, limit):
    """Wait until `needle` has been printed more than `before` times; True if it was."""
    end = time.time() + limit
    while time.time() < end:
        if sess.log.since(0).count(needle) > before:
            return True
        time.sleep(0.1)
    return False


def one_case(sess, case):
    """Send one case on a NEW client connection (the KISS decoder state is per client)
    and collect what the TNC, the copy client and kissutil show. Dire Wolf serves three
    clients at a time (B, kissutil and this one): the next connection is made only after
    it has said that the previous client has gone away and that it is ready again."""
    progress("case %s" % case["name"])
    attached = sess.log.since(0).count(b"Attached to KISS TCP client")
    a = connect()
    try:
        if not wait_count(sess, b"Attached to KISS TCP client", attached, 8):
            raise OSError("Dire Wolf did not attach the client; log tail: %r" % clean(sess.log.since(max(0, sess.log.mark() - 400))))
        atail = SockTail(a)
        a.settimeout(10)
        b0, k0, l0 = sess.btail.mark(), sess.kutail.mark(), sess.log.mark()
        a.sendall(bytes.fromhex(case["send_hex"]))
        settle(sess, float(case.get("settle", 0.6)))
        res = {"name": case["name"], "send_hex": case["send_hex"], "client_rx_hex": atail.since(0).hex(),
               "copy_hex": sess.btail.since(b0).hex(), "kissutil_text": clean(sess.kutail.since(k0)),
               "direwolf_log": clean(sess.log.since(l0))}
    finally:
        gone = sess.log.since(0).count(b"has gone away")
        ready = sess.log.since(0).count(b"Ready to accept KISS TCP client")
        try:
            a.shutdown(socket.SHUT_RDWR)      # close() alone does not end a connection a thread is still reading
        except OSError:
            pass
        a.close()
    wait_count(sess, b"has gone away", gone, 5)
    wait_count(sess, b"Ready to accept KISS TCP client", ready, 5)
    return res


def mode_tx(job):
    os_cases = job.get("cases", [])
    tnc2_cases = job.get("tnc2_cases", [])
    sess = TxSession()
    results = []
    for case in os_cases:
        if not sess.alive():                      # a case killed Dire Wolf: say so, start a new one
            results[-1]["direwolf_exit_after"] = sess.p.returncode
            sess.close()
            sess = TxSession()
        for attempt in (1, 2):
            try:
                res = one_case(sess, case)
                break
            except OSError as exc:                # no client slot: start a new Dire Wolf and try once more
                progress("case %s attempt %d failed: %r" % (case["name"], attempt, exc))
                res = {"name": case["name"], "send_hex": case["send_hex"], "error": "connect failed: %s" % exc,
                       "client_rx_hex": "", "copy_hex": "", "kissutil_text": "", "direwolf_log": ""}
                sess.close()
                sess = TxSession()
        res["attempts"] = attempt
        results.append(res)
    if not sess.alive():
        sess.close()
        sess = TxSession()
    for case in tnc2_cases:
        b0, k0, l0 = sess.btail.mark(), sess.kutail.mark(), sess.log.mark()
        tmp = "/tmp/ktx/.%s.tmp" % case["name"]
        with open(tmp, "wb") as fh:
            fh.write(case["tnc2"].encode("latin-1") + b"\n")
        os.rename(tmp, "/tmp/ktx/%s" % case["name"])
        settle(sess, float(case.get("settle", 3.0)), quiet=1.0, limit=8.0)
        results.append({"name": case["name"], "tnc2": case["tnc2"], "copy_hex": sess.btail.since(b0).hex(),
                        "kissutil_text": clean(sess.kutail.since(k0)), "direwolf_log": clean(sess.log.since(l0))})
    sess.close()
    return {"cases": results}


MODES = {"versions": mode_versions, "rx": mode_rx, "tx": mode_tx}


def main():
    job = json.loads(sys.stdin.read() or "{}")
    mode = job.get("mode")
    if mode not in MODES:
        print(json.dumps({"error": "unknown mode %r" % (mode,)}))
        return 2
    out = MODES[mode](job)
    out["mode"] = mode
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
