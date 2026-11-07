#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Driver inside dvt-direwolf:local (docs/direwolf-cases.md). Ours; the Dire Wolf programs
(GPL-2.0-or-later) are only started as processes: gen_packets, atest, direwolf, kissutil.

Two ways to run it:
  * `entry.py` with ONE JSON job on stdin and ONE JSON result on stdout (the modes below);
  * `entry.py loop` as a long-running service (compose services dw-a / dw-b, profile dw): one Dire Wolf
    whose transmit audio is relayed to a peer's receive audio over UDP (tier B of the DP15 RF cases, see
    "Modem loop" below).

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
  aprs  {"cases": [{"name", "frame_hex"}]}
      decode_aprs (the APRS decoder of Dire Wolf, standalone build) is run once per case on
      a file holding ONE line of space separated hex bytes (an AX.25 frame without FCS;
      the hex input path of decode_aprs keeps the information bytes exact). Result per
      case: name, rc, output (its stdout, ANSI colour codes removed, Latin-1 text).
  looptest  {"frames": 6, "info_len": 40}
      the modem loop without hardware, all on loopback of this one container: two Dire Wolf
      instances A (KISS 8001, audio in udp 7355) and B (KISS 8002, audio in udp 7356), each one's
      transmit audio relayed to the other's audio input (the same code as the service). Frames
      alternate A -> B and B -> A (order "ab"; "ba", or "a>b" / "b>a" for one direction only);
      result per frame: direction, sent_hex, received_hex, equal, latency_s, plus the two Dire
      Wolf logs, the relay byte counts and the kernel's UDP drop counters. A node does not hear
      what arrives while its own transmission would still be on the air (half duplex): the next
      frame of the other direction needs a "gap" of about the air time of the last one (4 s for
      600 information bytes). "tap" adds what `atest` decodes from each node's transmit audio.

Modem loop (docs/dp15-cases.md, tier B). Dire Wolf 1.8.1 can READ audio from a sound card, `stdin` or
`udp:<port>` but can only WRITE audio to an ALSA PCM. The ALSA `file` plugin (inside libasound, name
`file:<path>`, slave `null`) writes every transmitted sample to a path; with a FIFO there, a relay of
ours reads it and sends it, as raw 16 bit mono UDP datagrams, to the peer's `udp:` input. Nothing else
is needed in the image, no sound card, no privileges. Environment of `entry.py loop`:
  DW_MYCALL (N0TEST-8), DW_KISS_PORT (8001), DW_AUDIO_PORT (7355, udp audio input),
  DW_PEER ("host:port" of the peer's audio input, required), DW_KISSCOPY ("1" copies a frame
  one KISS client sends to the other KISS clients, as the heard-on-two-ports case needs).
"""
import json
import os
import pty
import re
import select
import signal
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


def mode_aprs(job):
    results = []
    for i, case in enumerate(job["cases"]):
        path = "/tmp/a%d.txt" % i
        with open(path, "w") as fh:
            fh.write(" ".join(case["frame_hex"][k:k + 2] for k in range(0, len(case["frame_hex"]), 2)) + "\n")
        done = subprocess.run(["decode_aprs", path], capture_output=True, timeout=30)
        results.append({"name": case["name"], "rc": done.returncode, "output": clean(done.stdout + done.stderr)})
    return {"cases": results}


# ------------------------------------------------------------------ modem loop (tier B)

UDP_CHUNK = 1024            # bytes per audio datagram (even; Dire Wolf reads at most 2000 from one datagram)
RELAY_SPEED = 4.0           # datagrams leave at this multiple of real time (a UDP receive buffer is a few hundred KB)
TAIL_S = 0.3                # silence after the last transmitted sample: the receiving modem sees the carrier go away
IDLE_S = 0.08               # no sample for this long = the transmission has ended
LOOP_DIR = "/tmp/dw"


def split_chunks(buf, size=UDP_CHUNK):
    """(chunks, rest): `buf` cut into pieces of `size` bytes (a multiple of 2, so a datagram never splits a 16 bit
    sample); `rest` is what does not fill a piece, with an odd byte kept so the next call stays sample aligned."""
    if size < 2 or size % 2:
        raise ValueError("chunk size must be even")
    cut = len(buf) - len(buf) % size
    return [buf[i:i + size] for i in range(0, cut, size)], buf[cut:]


def loop_conf(mycall, kiss_port, audio_in, fifo, kisscopy=False):
    """Dire Wolf configuration text of one loop node: audio in from the UDP port, audio out through the ALSA file
    plugin into `fifo`, one channel, 1200 baud AFSK, KISS TCP on `kiss_port` only (no AGW port; `KISSPORT 0`
    removes the built-in default 8001, which would otherwise stay next to the configured one)."""
    lines = ["ADEVICE udp:%d file:%s" % (audio_in, fifo), "ACHANNELS 1", "CHANNEL 0", "MYCALL %s" % mycall,
             "MODEM 1200", "AGWPORT 0", "KISSPORT 0", "KISSPORT %d" % kiss_port]
    if kisscopy:
        lines.append("KISSCOPY")
    return "\n".join(lines) + "\n"


class Relay(threading.Thread):
    """Reads raw samples from a FIFO (opened read/write, so there is no end of file when Dire Wolf closes or has not
    yet opened its side) and hands them to `send(datagram)` in sample aligned pieces, paced at `speed` times real time.
    After the transmission (no sample for IDLE_S) it sends TAIL_S of silence once."""

    def __init__(self, fifo, send, rate=RATE, speed=RELAY_SPEED, tail_s=TAIL_S, idle_s=IDLE_S, chunk=UDP_CHUNK, tap=None):
        super().__init__(daemon=True)
        self.tap = tap                          # optional file object: a copy of every sample relayed (debug, atest)
        self.fifo, self.send, self.rate, self.speed = fifo, send, rate, speed
        self.tail_s, self.idle_s, self.chunk = tail_s, idle_s, chunk
        self.bytes_in = 0
        self.datagrams = 0
        self.stop_flag = threading.Event()
        self.fd = os.open(fifo, os.O_RDWR)

    def _emit(self, chunks):
        step = self.chunk / (2.0 * self.rate * self.speed)
        for piece in chunks:
            self.send(piece)
            self.datagrams += 1
            time.sleep(step)

    def run(self):
        rest = b""
        active = False
        last = 0.0
        while not self.stop_flag.is_set():
            ready, _w, _x = select.select([self.fd], [], [], 0.05)
            if ready:
                data = os.read(self.fd, 65536)
                self.bytes_in += len(data)
                if self.tap is not None:
                    self.tap.write(data)
                    self.tap.flush()
                chunks, rest = split_chunks(rest + data, self.chunk)
                self._emit(chunks)
                active, last = True, time.monotonic()
            elif active and time.monotonic() - last >= self.idle_s:
                if rest:                        # the last partial piece, padded with silence
                    chunks, _r = split_chunks(rest + b"\0" * (self.chunk - len(rest)), self.chunk)
                    self._emit(chunks)
                    rest = b""
                silence = b"\0\0" * int(self.rate * self.tail_s)
                self._emit(split_chunks(silence, self.chunk)[0])
                active = False

    def stop(self):
        self.stop_flag.set()
        self.join(2.0)
        try:
            os.close(self.fd)
        except OSError:
            pass


class LoopNode:
    """One Dire Wolf of the modem loop: the process (log through a pty), the FIFO, the relay to the peer."""

    def __init__(self, name, mycall, kiss_port, audio_in, peer, kisscopy=False, workdir=LOOP_DIR, echo_log=False, speed=RELAY_SPEED, tap=False):
        self.name, self.kiss_port, self.audio_in, self.peer = name, kiss_port, audio_in, peer
        self.workdir = workdir
        self.fifo = os.path.join(workdir, "tx-%s.raw" % name)
        self.conf = os.path.join(workdir, "dw-%s.conf" % name)
        self.echo_log = echo_log
        os.makedirs(workdir, exist_ok=True)
        if os.path.exists(self.fifo):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo, 0o600)
        with open(self.conf, "w") as fh:
            fh.write(loop_conf(mycall, kiss_port, audio_in, self.fifo, kisscopy))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.peer_host, self.peer_port, self.target = peer[0], int(peer[1]), None
        self.tap_path = os.path.join(workdir, "tap-%s.raw" % name) if tap else None
        self.relay = Relay(self.fifo, self._send, speed=speed, tap=open(self.tap_path, "wb") if tap else None)
        self.proc = None
        self.log = None
        self._forward = None

    def _send(self, datagram):
        try:
            if self.target is None:              # resolved once, when the peer exists (an IP literal always does)
                self.target = (socket.gethostbyname(self.peer_host), self.peer_port)
            self.sock.sendto(datagram, self.target)
        except OSError:
            pass                                 # the peer is not up yet: its audio would be lost anyway

    def start(self):
        self.relay.start()
        master, slave = pty_pair()
        self.proc = subprocess.Popen(["direwolf", "-c", self.conf, "-r", str(RATE), "-t", "0"],
                                     stdin=subprocess.DEVNULL, stdout=slave, stderr=slave)
        os.close(slave)
        self.log = Tail(master)
        if self.echo_log:
            self._forward = threading.Thread(target=self._pump_log, args=(master,), daemon=True)
            self._forward.start()
        return self

    def _pump_log(self, master):
        pos = 0
        while self.proc is not None and self.proc.poll() is None:
            data = self.log.since(pos)
            pos += len(data)
            if data:
                sys.stdout.write("[%s] %s" % (self.name, clean(data)))
                sys.stdout.flush()
            time.sleep(0.2)

    def ready(self, wait=10.0):
        end = time.time() + wait
        while time.time() < end:
            if b"Ready to accept KISS TCP client" in self.log.since(0):
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def stop(self):
        self.relay.stop()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.sock.close()


def kiss_connect(port, wait=8.0):
    end = time.time() + wait
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.settimeout(None)
            return s
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.3)


def kiss_frames(raw):
    """Frames (bytes without the type byte) of data commands in a KISS byte stream (FEND 0x00 ... FEND)."""
    out, cur, inside = [], bytearray(), False
    esc = False
    for b in raw:
        if b == 0xC0:
            if inside and len(cur) > 1 and cur[0] == 0x00:
                out.append(bytes(cur[1:]))
            cur, inside, esc = bytearray(), True, False
        elif inside:
            if esc:
                cur.append({0xDC: 0xC0, 0xDD: 0xDB}.get(b, b))
                esc = False
            elif b == 0xDB:
                esc = True
            else:
                cur.append(b)
    return out


def test_frame(src, seq, info_len):
    """AX.25 UI frame bytes src>APDVT1 with an APRS message to XLX999B; used by the loop test only."""
    def addr(call, ssid, last):
        return bytes(ord(c) << 1 for c in call.ljust(6)) + bytes([0x60 | (ssid << 1) | (1 if last else 0)])
    text = ("loop%d " % seq + "x" * info_len)[:info_len]
    return addr("APDVT1", 0, False) + addr(src, 7, True) + b"\x03\xf0" + (":%-9s:%s{%d" % ("XLX999B", text, seq)).encode("ascii")


def udp_drops():
    """{local port: datagrams the kernel dropped for lack of receive buffer} of the UDP sockets of this container."""
    out = {}
    try:
        with open("/proc/net/udp") as fh:
            rows = fh.read().splitlines()[1:]
    except OSError:
        return out
    for row in rows:
        cols = row.split()
        if len(cols) >= 13:
            out[int(cols[1].split(":")[1], 16)] = [int(cols[12]), cols[4]]
    return out


def atest_taps(nodes):
    """job "tap": what `atest` decodes from the audio each node transmitted (a WAV header is put in front of the raw samples)."""
    out = {}
    for node in nodes:
        wav = node.tap_path[:-4] + ".wav"
        with open(node.tap_path, "rb") as fh:
            raw = fh.read()
        with open(wav, "wb") as fh:
            fh.write(b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
                     + b"data" + struct.pack("<I", len(raw)) + raw)
        done = run(["atest", "-B", "1200", wav])
        out[node.name] = clean(done.stdout)[-1500:]
    return out


def mode_looptest(job):
    count = int(job.get("frames", 6))
    info_len = int(job.get("info_len", 40))
    speed = float(job.get("speed", RELAY_SPEED))
    order = job.get("order", "ab")
    tap = bool(job.get("tap"))
    a = LoopNode("a", "N0TEST-8", 8001, 7355, ("127.0.0.1", 7356), workdir=LOOP_DIR, speed=speed, tap=tap)
    b = LoopNode("b", "N0TEST-9", 8002, 7356, ("127.0.0.1", 7355), workdir=LOOP_DIR, speed=speed, tap=tap)
    results = []
    try:
        a.start()
        b.start()
        if not (a.ready() and b.ready()):
            return {"error": "a Dire Wolf did not open its KISS port", "log_a": clean(a.log.since(0)), "log_b": clean(b.log.since(0))}
        ca, cb = kiss_connect(8001), kiss_connect(8002)
        ta, tb = SockTail(ca), SockTail(cb)
        time.sleep(0.5)
        for seq in range(count):
            if order in ("ab", "ba"):
                to_b = (seq % 2 == 0) == (order == "ab")     # A -> B, then B -> A, ... (order "ba": the other way round)
            else:
                to_b = order == "a>b"                           # always the same direction ("a>b" or "b>a")
            dst_tail, src_sock = (tb, ca) if to_b else (ta, cb)
            frame = test_frame("N0TEST", seq, info_len)
            mark = dst_tail.mark()
            t0 = time.time()
            src_sock.sendall(b"\xc0\x00" + frame.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc") + b"\xc0")
            got = []
            end = t0 + 15.0
            while time.time() < end and not got:
                got = kiss_frames(dst_tail.since(mark))
                time.sleep(0.05)
            latency = time.time() - t0
            results.append({"seq": seq, "direction": "a->b" if to_b else "b->a", "sent_hex": frame.hex(),
                            "received_hex": got[0].hex() if got else "", "frames_received": len(got),
                            "equal": bool(got) and got[0] == frame, "latency_s": round(latency, 3)})
            time.sleep(float(job.get("gap", 0.3)))
        for s in (ca, cb):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        return {"frames": results, "relay_a": {"bytes": a.relay.bytes_in, "datagrams": a.relay.datagrams},
                "relay_b": {"bytes": b.relay.bytes_in, "datagrams": b.relay.datagrams}, "udp_drops": udp_drops(),
                "log_a": clean(a.log.since(0)), "log_b": clean(b.log.since(0)),
                "atest": atest_taps([n for n in (a, b) if n.tap_path])}
    finally:
        a.stop()
        b.stop()


def run_loop_service():
    """`entry.py loop`: one node of the loop, until SIGTERM. A Dire Wolf that exits is started again."""
    env = os.environ
    peer = env.get("DW_PEER", "")
    if ":" not in peer:
        sys.stderr.write("entry.py loop: DW_PEER=host:port (the peer's audio input) is required\n")
        return 2
    host, port = peer.rsplit(":", 1)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_a: stop.set())
    signal.signal(signal.SIGINT, lambda *_a: stop.set())
    name = env.get("DW_NAME", "n")
    while not stop.is_set():
        node = LoopNode(name, env.get("DW_MYCALL", "N0TEST-8"), int(env.get("DW_KISS_PORT", "8001")),
                        int(env.get("DW_AUDIO_PORT", "7355")), (host, int(port)),
                        kisscopy=env.get("DW_KISSCOPY", "0") == "1", echo_log=True)
        node.start()
        print("loop node %s: kiss tcp %s, audio in udp %s, audio out to %s" % (name, node.kiss_port, node.audio_in, peer), flush=True)
        while not stop.is_set() and node.proc.poll() is None:
            stop.wait(0.5)
        node.stop()
        if not stop.is_set():
            print("loop node %s: direwolf exited with %s, starting it again" % (name, node.proc.returncode), flush=True)
            stop.wait(1.0)
    return 0


MODES = {"versions": mode_versions, "rx": mode_rx, "tx": mode_tx, "aprs": mode_aprs, "looptest": mode_looptest}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        return run_loop_service()
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
