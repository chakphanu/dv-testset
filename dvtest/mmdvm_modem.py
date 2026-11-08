# SPDX-License-Identifier: MIT
"""A virtual MMDVM modem: the serial protocol of an MMDVM hotspot board, spoken
to the real MMDVMHost without radio hardware (docs/mmdvm-stack-cases.md).

MMDVMHost reaches its modem in one of two ways, both supported here:

  udp  [Modem] Protocol=udp, ModemAddress / ModemPort = this modem, LocalAddress /
       LocalPort = the host. The same frames as on a serial line, one frame per
       datagram, the modem must answer FROM the ModemAddress:ModemPort the host
       was told (MMDVMHost UDPController.cpp:55-60 drops other senders). This is
       the path of the rig (the modem lives in the tester, MMDVMHost in the
       service mmdvm-stack).
  pty  [Modem] Protocol=uart, UARTPort = the slave side of a pseudo-terminal
       made here (os.openpty). Only for a MMDVMHost that runs in the same
       machine or container as the modem (`python3 -m dvtest.mmdvm_modem --pty`).

Frame (MMDVMHost Modem.cpp:46-120, 2576-2675): 0xE0, total length (one byte,
the frame start and length byte included), type, payload. The modem answers
GET_VERSION (protocol version 2, capabilities, CPU type, 16 byte UDID, a text
that names dvtest), GET_STATUS (mode, flags, buffer space of every mode),
SET_CONFIG, SET_FREQ and SET_MODE with ACK; it takes the voice frames the host
writes (what the reflector side delivers down to "RF") into a recording with
timestamps, per mode; and `keyup(mode, ...)` plays a transmission of a radio in
real time (mmdvm_rf.py, mmdvm_rf_p25.py, mmdvm_rf_nxdn.py build the frames).

Not a model of the radio: no RSSI, no lockout, no CW id, the buffer space is a
plausible fill model (capacity and play-out time per mode) and not the firmware's
exact numbers. M17 is absent: the pinned MMDVMHost has no M17 mode.
"""
import argparse
import os
import select
import socket
import sys
import threading
import time
from collections import namedtuple

from . import mmdvm_rf as rf
from .audio import signals

# --- serial protocol constants (MMDVMHost Modem.cpp:46-120, pinned deb5efe6) -----
FRAME_START = 0xE0
GET_VERSION, GET_STATUS, SET_CONFIG, SET_MODE, SET_FREQ = 0x00, 0x01, 0x02, 0x03, 0x04
SEND_CWID = 0x0A
DMR_ALOHA, DMR_SHORTLC, DMR_START, DMR_ABORT = 0x17, 0x1C, 0x1D, 0x1E
ACK, NAK = 0x70, 0x7F
SERIAL_DATA, TRANSPARENT, QSO_INFO = 0x80, 0x90, 0x91
# capability bits of the version frame (Modem.cpp:122-129)
CAP1_DSTAR, CAP1_DMR, CAP1_YSF, CAP1_P25, CAP1_NXDN = 0x01, 0x02, 0x04, 0x08, 0x10
CAP1_ALL = CAP1_DSTAR | CAP1_DMR | CAP1_YSF | CAP1_P25 | CAP1_NXDN
# modem modes (MMDVMHost Defines.h:31-40)
MODE_IDLE, MODE_DSTAR, MODE_DMR, MODE_YSF, MODE_P25, MODE_NXDN = 0, 1, 2, 3, 4, 5
# status flags byte (Modem.cpp:830-850, protocol version 2)
FLAG_TX, FLAG_LOCKOUT, FLAG_CD = 0x01, 0x10, 0x40
# NAK reasons the firmware uses
NAK_UNKNOWN_COMMAND, NAK_BAD_DATA = 1, 4

VERSION_TEXT = "dvtest virtual MMDVM 1.0 (no radio)"
UDID = bytes.fromhex("D17E57000000000000000000000000AA")[:16]

# frame type -> mode name of the data frames (MMDVMHost Modem.cpp:46-93)
MODE_OF_TYPE = {0x10: "dstar", 0x11: "dstar", 0x12: "dstar", 0x13: "dstar",
                0x18: "dmr", 0x19: "dmr", 0x1A: "dmr", 0x1B: "dmr",
                0x17: "dmr-ctl", 0x1C: "dmr-ctl", 0x1D: "dmr-ctl", 0x1E: "dmr-ctl",
                0x20: "ysf", 0x21: "ysf", 0x30: "p25", 0x31: "p25", 0x32: "p25",
                0x40: "nxdn", 0x41: "nxdn", QSO_INFO: "info", SERIAL_DATA: "serial", TRANSPARENT: "transparent"}
# status byte order of version 2 (Modem.cpp:880-920): D-STAR, DMR slot 1, DMR slot 2, YSF, P25, NXDN
# play-out time of one host frame per mode in ms, and the buffer capacity in frames
PLAYOUT_MS = {0x10: 80.0, 0x11: 20.0, 0x13: 20.0, 0x18: 60.0, 0x1A: 60.0, 0x20: 100.0, 0x30: 180.0, 0x31: 180.0, 0x40: 80.0}
CAPACITY = {"dstar": 10, "dmr1": 10, "dmr2": 10, "ysf": 8, "p25": 4, "nxdn": 8}

Rx = namedtuple("Rx", "t mono ftype payload mode")


def frame(ftype, payload=b""):
    """One serial frame: 0xE0, total length, type, payload."""
    total = 3 + len(payload)
    if total > 255:
        raise ValueError("frame of %d bytes does not fit one length byte" % total)
    return bytes((FRAME_START, total, ftype)) + bytes(payload)


class FrameParser:
    """Serial stream -> (type, payload) frames. Bytes before a 0xE0 are dropped;
    a length below 3 resynchronises on the next 0xE0."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            while self.buf and self.buf[0] != FRAME_START:
                del self.buf[0]
            if len(self.buf) < 2:
                return out
            total = self.buf[1]
            if total < 3:
                del self.buf[0]
                continue
            if len(self.buf) < total:
                return out
            out.append((self.buf[2], bytes(self.buf[3:total])))
            del self.buf[:total]


# --- transports ----------------------------------------------------------------

class UdpTransport:
    """Datagram per frame. The peer (the host's LocalAddress:LocalPort) is the
    source of the last datagram, or `peer` when given."""

    def __init__(self, bind=("0.0.0.0", 3334), peer=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(bind)
        self.peer = peer
        self.description = "udp %s:%d" % self.sock.getsockname()

    def read(self, timeout):
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return b""
        data, addr = self.sock.recvfrom(2048)
        self.peer = addr
        return data

    def write(self, data):
        if self.peer is not None:
            self.sock.sendto(data, self.peer)

    def close(self):
        self.sock.close()


class PtyTransport:
    """Master side of a pseudo-terminal; the slave path is what MMDVMHost's
    UARTPort names. The line is raw (no echo, no translation)."""

    def __init__(self):
        import tty
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.slave = slave            # kept open so the pty survives a host that closes and reopens it
        self.path = os.ttyname(slave)
        self.description = "pty %s" % self.path

    def read(self, timeout):
        ready, _, _ = select.select([self.master], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self.master, 4096)
        except OSError:
            return b""

    def write(self, data):
        try:
            os.write(self.master, data)
        except OSError:
            pass

    def close(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


# --- key-up --------------------------------------------------------------------

class KeyUp:
    """One transmission being played out. `voice` is what a listener must hear
    (canonical frames, padding included where the mode adds it); `sent` the
    (monotonic time, type) of every frame written."""

    def __init__(self, mode, frames, voice, voice_count):
        self.mode, self.frames, self.voice, self.voice_count = mode, frames, voice, voice_count
        self.sent = []
        self.t_start = self.t_end = None
        self.done = threading.Event()
        self.error = None

    def wait(self, timeout=None):
        return self.done.wait(timeout)

    @property
    def duration_s(self):
        return None if self.t_end is None else self.t_end - self.t_start


def default_frames(mode, count, seed="mmdvm"):
    """count deterministic voice words for the mode (PRNG, dvtest.audio.signals)."""
    width = rf.FRAME_WIDTH[mode]
    raw = signals.prng_bytes("%s/%s" % (seed, mode), width * count)
    return [raw[i:i + width] for i in range(0, width * count, width)]


class VirtualModem:
    """The modem. `transport` is a UdpTransport or PtyTransport (or a test
    double with read(timeout) / write(data) / close())."""

    def __init__(self, transport, caps1=CAP1_ALL, text=VERSION_TEXT, log=None):
        self.transport = transport
        self.caps1 = caps1
        self.text = text
        self.log = log or (lambda *a: None)
        self.lock = threading.Lock()
        self.rx = []
        self.mode = MODE_IDLE
        self.config = {}
        self.configured = threading.Event()
        self.host_seen = threading.Event()
        self.counts = {"version": 0, "status": 0, "config": 0, "freq": 0, "mode": 0, "bad": 0}
        self.cd = False
        self._busy_until = {"dstar": 0.0, "dmr1": 0.0, "dmr2": 0.0, "ysf": 0.0, "p25": 0.0, "nxdn": 0.0}
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.keyups = []

    # --- life ---
    def start(self):
        self._thread = threading.Thread(target=self._run, name="mmdvm-modem", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self.transport.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- wire ---
    def send(self, ftype, payload=b""):
        with self._send_lock:
            self.transport.write(frame(ftype, payload))

    def _run(self):
        parser = FrameParser()
        while not self._stop.is_set():
            data = self.transport.read(0.1)
            if not data:
                continue
            self.host_seen.set()
            for ftype, payload in parser.feed(data):
                try:
                    self._handle(ftype, payload)
                except Exception as exc:  # noqa: BLE001 - a bad frame must not stop the modem
                    self.counts["bad"] += 1
                    self.log("handler error", ftype, exc)

    def _handle(self, ftype, payload):
        now = time.monotonic()
        mode = MODE_OF_TYPE.get(ftype)
        if ftype in (GET_VERSION, GET_STATUS, SET_CONFIG, SET_MODE, SET_FREQ, SEND_CWID):
            if ftype == GET_VERSION:
                self.counts["version"] += 1
                self.send(GET_VERSION, self.version_payload())
            elif ftype == GET_STATUS:
                self.counts["status"] += 1
                self.send(GET_STATUS, self.status_payload(now))
            elif ftype == SET_CONFIG:
                self.counts["config"] += 1
                self.config = self.parse_config(payload)
                self.send(ACK, bytes((SET_CONFIG,)))
                self.configured.set()
            elif ftype == SET_FREQ:
                self.counts["freq"] += 1
                self.send(ACK, bytes((SET_FREQ,)))
            elif ftype == SET_MODE:
                self.counts["mode"] += 1
                self.mode = payload[0] if payload else MODE_IDLE
                self.send(ACK, bytes((SET_MODE,)))
            else:
                self.send(ACK, bytes((ftype,)))
            return
        with self.lock:
            self.rx.append(Rx(time.time(), now, ftype, bytes(payload), mode))
        if ftype in PLAYOUT_MS:
            self._enqueue(ftype, now)
        if mode is None:
            self.counts["bad"] += 1
            self.send(NAK, bytes((ftype, NAK_UNKNOWN_COMMAND)))

    # --- replies ---
    def version_payload(self):
        return bytes((2, self.caps1, 0, 2)) + UDID + self.text.encode("ascii")

    def parse_config(self, payload):
        """Fields of a version 2 SET_CONFIG (MMDVMHost Modem.cpp:2313-2420):
        flags, enabled modes, TX delay, colour code. Version 1 (26 bytes) keeps only the raw flags."""
        cfg = {"length": len(payload) + 3}
        if len(payload) >= 1:
            cfg["duplex"] = not payload[0] & 0x80
            cfg["flags"] = payload[0]
        if len(payload) >= 3:
            modes = payload[1]
            cfg["modes"] = [name for bit, name in ((0x01, "dstar"), (0x02, "dmr"), (0x04, "ysf"), (0x08, "p25"),
                                                   (0x10, "nxdn")) if modes & bit]
        if len(payload) >= 28:
            cfg["colour_code"] = payload[26]
            cfg["tx_delay_ms"] = payload[3] * 10
        return cfg

    def _slot_of(self, ftype):
        return {0x10: "dstar", 0x11: "dstar", 0x13: "dstar", 0x18: "dmr1", 0x1A: "dmr2", 0x20: "ysf",
                0x30: "p25", 0x31: "p25", 0x40: "nxdn"}.get(ftype)

    def _enqueue(self, ftype, now):
        key = self._slot_of(ftype)
        if key:
            self._busy_until[key] = max(self._busy_until[key], now) + PLAYOUT_MS[ftype] / 1000.0

    def space(self, key, now=None):
        """Free frames of one mode's buffer: capacity minus what the host wrote
        that has not played out yet (each frame takes its air time)."""
        now = time.monotonic() if now is None else now
        per = {"dstar": 0.020, "dmr1": 0.060, "dmr2": 0.060, "ysf": 0.100, "p25": 0.180, "nxdn": 0.080}[key]
        backlog = max(0.0, self._busy_until[key] - now)
        return max(0, CAPACITY[key] - int(round(backlog / per)))

    def status_payload(self, now=None):
        now = time.monotonic() if now is None else now
        tx = any(self._busy_until[k] > now for k in self._busy_until)
        flags = (FLAG_TX if tx else 0) | (FLAG_CD if self.cd else 0)
        s = self.space
        return bytes((self.mode, flags, 0, s("dstar", now), s("dmr1", now), s("dmr2", now), s("ysf", now),
                      s("p25", now), s("nxdn", now), 0, 0, 0))

    # --- handshake ---
    def wait_configured(self, timeout=60.0):
        """True once the host sent SET_CONFIG (it read the version first)."""
        return self.configured.wait(timeout)

    # --- recording ---
    def clear(self):
        with self.lock:
            self.rx = []

    def recording(self, mode=None, since=None):
        with self.lock:
            items = list(self.rx)
        return [r for r in items if (mode is None or r.mode == mode) and (since is None or r.mono >= since)]

    def pairs(self, mode, since=None):
        """(type, payload) of the recorded frames of a mode, the input of the decoders."""
        return [(r.ftype, r.payload) for r in self.recording(mode, since)]

    def wait_frames(self, mode, count, timeout=15.0, since=None, types=None):
        """Wait until `count` frames (of the given types) of a mode were written by the host."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            rows = [r for r in self.recording(mode, since) if types is None or r.ftype in types]
            if len(rows) >= count:
                return rows
            time.sleep(0.05)
        return [r for r in self.recording(mode, since) if types is None or r.ftype in types]

    def downlink(self, mode, since=None):
        """The decoded recording of a mode (what the reflector delivered)."""
        pairs = self.pairs(mode, since)
        if mode == "dstar":
            return rf.decode_dstar(pairs)
        if mode == "dmr":
            return rf.decode_dmr(pairs)
        if mode == "ysf":
            return rf.decode_ysf(pairs)
        if mode == "p25":
            from . import mmdvm_rf_p25
            return mmdvm_rf_p25.decode_downlink(pairs)
        if mode == "nxdn":
            from . import mmdvm_rf_nxdn
            return mmdvm_rf_nxdn.decode_downlink(pairs)
        raise ValueError(mode)

    # --- key-up ---
    def build_keyup(self, mode, frames=None, seconds=3.0, **kw):
        """(air frames, voice words a listener must hear) of a transmission.

        dstar: my, my2, rpt1, rpt2, ur       dmr: src, dst, group, slot, colour_code
        ysf:   src, dest, dgid               p25: src, dst, nac, group
        nxdn:  src, dst, ran, group
        frames defaults to seconds * 50 deterministic voice words (20 ms each)."""
        if mode not in rf.FRAME_WIDTH:
            raise ValueError("no key-up for mode %r (the pinned MMDVMHost has no M17)" % mode)
        if frames is None:
            frames = default_frames(mode, int(round(seconds * 50)), kw.pop("seed", "mmdvm"))
        else:
            kw.pop("seed", None)
        if mode == "dstar":
            air = rf.dstar_keyup(frames, **kw)
            voice = rf.padded_frames("dstar", frames)
        elif mode == "dmr":
            air = rf.dmr_keyup(frames, **kw)
            voice = rf.padded_frames("dmr", frames)
        elif mode == "ysf":
            air = rf.ysf_keyup(frames, **kw)
            voice = rf.padded_frames("ysf", frames)
        elif mode == "p25":
            from . import mmdvm_rf_p25
            air = mmdvm_rf_p25.build_keyup(kw.pop("src"), kw.pop("dst"), frames, **kw)
            voice = list(frames) + [mmdvm_rf_p25.NULL_IMBE] * (-len(frames) % 9)
        elif mode == "nxdn":
            from . import mmdvm_rf_nxdn
            air = mmdvm_rf_nxdn.build_keyup(kw.pop("src"), kw.pop("dst"), frames, **kw)
            voice = rf.canonical_frames("nxdn", frames) + [bytes(7)] * (-len(frames) % 4)
        else:
            raise ValueError("no key-up for mode %r (the pinned MMDVMHost has no M17)" % mode)
        return air, voice, len(frames)

    def keyup(self, mode, frames=None, seconds=3.0, wait=False, **kw):
        """Play a transmission of a radio now: frames at their air time (a
        thread), carrier detect on meanwhile. Returns the KeyUp."""
        air, voice, count = self.build_keyup(mode, frames, seconds, **kw)
        ku = KeyUp(mode, air, voice, count)
        self.keyups.append(ku)
        thread = threading.Thread(target=self._play, args=(ku,), name="mmdvm-keyup", daemon=True)
        thread.start()
        if wait:
            ku.wait(seconds + 30)
        return ku

    def _play(self, ku):
        ku.t_start = time.monotonic()
        due = ku.t_start
        self.cd = True
        try:
            for ftype, payload, delay_ms in ku.frames:
                pause = due - time.monotonic()
                if pause > 0:
                    time.sleep(pause)
                self.send(ftype, payload)
                ku.sent.append((time.monotonic(), ftype))
                due += delay_ms / 1000.0
        except Exception as exc:  # noqa: BLE001
            ku.error = exc
        finally:
            ku.t_end = time.monotonic()
            self.cd = False
            ku.done.set()


def main(argv=None):
    parser = argparse.ArgumentParser(description="virtual MMDVM modem for MMDVMHost (udp or pty)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--udp", metavar="IP:PORT", help="listen here; MMDVMHost [Modem] ModemAddress / ModemPort")
    group.add_argument("--pty", action="store_true", help="make a pseudo-terminal, print its path (UARTPort)")
    parser.add_argument("--keyup", choices=("dstar", "dmr", "ysf", "p25", "nxdn"), help="key up once after the handshake")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--callsign", default="N0CALL")
    parser.add_argument("--id", type=int, default=1023007, help="DMR / P25 / NXDN source id of the key-up")
    parser.add_argument("--dst", type=int, default=6, help="talk group of the key-up (DMR / P25 / NXDN)")
    args = parser.parse_args(argv)
    if args.udp:
        host, port = args.udp.rsplit(":", 1)
        transport = UdpTransport((host, int(port)))
    else:
        transport = PtyTransport()
    print("modem on", transport.description, flush=True)
    with VirtualModem(transport, log=lambda *a: print(*a, file=sys.stderr)) as modem:
        try:
            if args.keyup:
                print("waiting for the host ...", flush=True)
                modem.wait_configured(300)
                time.sleep(2)
                kw = {"dstar": {"my": args.callsign}, "dmr": {"src": args.id, "dst": args.dst},
                      "ysf": {"src": args.callsign}, "p25": {"src": args.id, "dst": args.dst},
                      "nxdn": {"src": args.id, "dst": args.dst}}[args.keyup]
                ku = modem.keyup(args.keyup, seconds=args.seconds, **kw)
                ku.wait(args.seconds + 30)
                time.sleep(5)
                print("key-up done, %d voice words; host wrote %d frames" % (ku.voice_count, len(modem.recording())))
            else:
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
