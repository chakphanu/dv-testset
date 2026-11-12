# SPDX-License-Identifier: MIT
"""MMDVM modem serial protocol as DV-Router's `mmdvm-serial` radio adapter speaks it (DV-Router unit RIF5,
complete in software at c017e491), and a fake modem for it. Stdlib only. Cases: cases/test_rif5_mmdvm_serial.py,
document: docs/rif5-cases.md.

DV-Router's radio edge (`dv-edge-radio`) talks to a modem over a byte port: serial, pty or UDP (one frame per
datagram, only from the configured peer address). In the rig the tester plays the modem on the UDP byte port.
Facts used (DV-Router docs/design/radio.md 7.1 "Protocol facts and pure adapter", config.md "[edges.radio]"):

  envelope   short `E0 LEN TYPE payload`, LEN = total 3..255; long `E0 00 L2 TYPE payload`, total = 255 + L2
             (256..510). No checksum. LEN 1 or 2 and a zero L2 are invalid (drop the E0, rescan).
  types      GET_VERSION 00, GET_STATUS 01, SET_CONFIG 02, SET_MODE 03, SET_FREQ 04, SEND_CWID 0A, ACK 70, NAK 7F;
             D-STAR HEADER 10, DATA 11, LOST 12, EOT 13; DMR slot 2 DATA 1A, LOST 1B; YSF DATA 20, LOST 21.
  open       settle 2 s, GET_VERSION (up to 6 attempts 1.8 s apart), SET_FREQ (ACK, 1 s, one retry), SET_CONFIG
             (v1 26 B, v2 40 B; same), GET_STATUS, then Up. A NAK or a missed deadline is Down.
  polling    GET_STATUS every 250 ms; a valid status refreshes the inactivity timer, 2 s without one after Up gives
             Down (reason `inactive`). A short status never refreshes it.
  native     D-STAR header 41 B, data 12 B (9 AMBE + 3 slow data); DMR 1 control byte + 33 B burst; YSF 1 flag +
             120 B. The host writes a DMR / YSF frame as `E0 25 1A 00 <burst>` / `E0 7C 20 00 <120 B>`.

What is here:

  encode_frame / FrameReader      the envelope, both length forms
  parse_set_freq / parse_set_config_v2, expected_set_freq / expected_set_config_v2
                                  independent readings of the two setup frames from the document
  Rif5Modem                       the fake modem: a VirtualModem (dvtest/mmdvm_modem.py: UDP / pty transport, RF key-up
                                  frames of dvtest/mmdvm_rf.py, per-mode recording) with the protocol version 2 answers
                                  DV-Router needs (a 17 byte status), a log of EVERY frame the host wrote with its
                                  time, and the faults the cases need (silence, a short status)
  PinnedUdp                       the UDP transport that accepts datagrams from the configured peer only, as the edge does

The modem is not a model of a radio: no RSSI, no lockout unless a case sets the flag, the buffer space is the fill
model of VirtualModem. Judgements over the log are in dvtest/rif5.py.
"""
import select
import threading
import time
from collections import deque, namedtuple

from . import mmdvm_modem as mm

# --- types (radio.md 7.1) ------------------------------------------------------------------------------------------
GET_VERSION, GET_STATUS, SET_CONFIG, SET_MODE, SET_FREQ = 0x00, 0x01, 0x02, 0x03, 0x04
SEND_CWID = 0x0A
ACK, NAK = 0x70, 0x7F
DSTAR_HEADER, DSTAR_DATA, DSTAR_LOST, DSTAR_EOT = 0x10, 0x11, 0x12, 0x13
DMR_DATA2, DMR_LOST2 = 0x1A, 0x1B
YSF_DATA, YSF_LOST = 0x20, 0x21
# DMR slot 1 / duplex controls the adapter never sends (simplex only): ALOHA 17, DATA1 18, LOST1 19, SHORTLC 1C, START 1D, ABORT 1E
DMR_OTHER = (0x17, 0x18, 0x19, 0x1C, 0x1D, 0x1E)

NAMES = {GET_VERSION: "GET_VERSION", GET_STATUS: "GET_STATUS", SET_CONFIG: "SET_CONFIG", SET_MODE: "SET_MODE",
         SET_FREQ: "SET_FREQ", SEND_CWID: "SEND_CWID", ACK: "ACK", NAK: "NAK", DSTAR_HEADER: "DSTAR_HEADER",
         DSTAR_DATA: "DSTAR_DATA", DSTAR_LOST: "DSTAR_LOST", DSTAR_EOT: "DSTAR_EOT", DMR_DATA2: "DMR_DATA2",
         DMR_LOST2: "DMR_LOST2", YSF_DATA: "YSF_DATA", YSF_LOST: "YSF_LOST", 0x17: "DMR_ALOHA", 0x18: "DMR_DATA1",
         0x19: "DMR_LOST1", 0x1C: "DMR_SHORTLC", 0x1D: "DMR_START", 0x1E: "DMR_ABORT"}

# mode byte of SET_MODE and of the status (radio.md 7.1): idle 0, D-STAR 1, DMR 2, YSF 3
MODE_IDLE, MODE_DSTAR, MODE_DMR, MODE_YSF = 0, 1, 2, 3
MODE_ID = {"dstar": MODE_DSTAR, "dmr": MODE_DMR, "ysf": MODE_YSF}
MODE_NAME = {MODE_IDLE: "idle", MODE_DSTAR: "dstar", MODE_DMR: "dmr", MODE_YSF: "ysf"}
# the native voice types the host writes, by mode name
TX_TYPES = {"dstar": (DSTAR_HEADER, DSTAR_DATA, DSTAR_EOT), "dmr": (DMR_DATA2,), "ysf": (YSF_DATA,)}
# capability byte 1 of the version reply (radio.md 7.1): D-STAR 01, DMR 02, YSF 04
CAP_DSTAR, CAP_DMR, CAP_YSF = 0x01, 0x02, 0x04
# NAK reasons (radio.md 7.1 U9)
NAK_UNKNOWN, NAK_NOT_ACCEPTED, NAK_BAD_VALUE, NAK_FULL = 1, 2, 4, 5

STATUS_V2_MIN = 17            # payload bytes of a valid version 2 status (frame total 20)
FRAME_MAX_SHORT = 255
READER_LIMIT = 512
EVENT_LIMIT = 20000            # events kept for the timeline (about 30 minutes of polls)

HostFrame = namedtuple("HostFrame", "t mono ftype payload")     # t wall clock, mono monotonic, payload without the envelope
Frame = namedtuple("Frame", "ftype payload raw")


def name_of(ftype):
    return NAMES.get(ftype, "0x%02X" % ftype)


# --- the envelope --------------------------------------------------------------------------------------------------

def encode_frame(ftype, payload=b""):
    """One frame, short form up to a total of 255 bytes (payload 252), long form after that (total 257..510).
    A payload of 253 bytes is the first long one: a total of 256 cannot be told from a short one."""
    payload = bytes(payload)
    total = 3 + len(payload)
    if total <= FRAME_MAX_SHORT:
        return bytes((0xE0, total, ftype)) + payload
    total = 4 + len(payload)
    if total > 510:
        raise ValueError("frame of %d bytes does not fit the long form" % total)
    return bytes((0xE0, 0, total - FRAME_MAX_SHORT, ftype)) + payload


class FrameReader:
    """Byte stream -> Frame. Bytes before an E0 are dropped; a LEN of 1 or 2 and a zero L2 drop the leading E0 and
    rescan; a partial frame is kept (at most READER_LIMIT bytes: a longer unfinished buffer drops its first byte)."""

    def __init__(self):
        self.buf = bytearray()
        self.resyncs = 0

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            while self.buf and self.buf[0] != 0xE0:
                del self.buf[0]
                self.resyncs += 1
            if len(self.buf) < 2:
                return out
            length = self.buf[1]
            if length in (1, 2):
                del self.buf[0]
                self.resyncs += 1
                continue
            if length == 0:
                if len(self.buf) < 3:
                    return out
                if self.buf[2] == 0:
                    del self.buf[0]
                    self.resyncs += 1
                    continue
                total, head = FRAME_MAX_SHORT + self.buf[2], 4
            else:
                total, head = length, 3
            if len(self.buf) < total:
                if len(self.buf) > READER_LIMIT:
                    del self.buf[0]
                    continue
                return out
            raw = bytes(self.buf[:total])
            out.append(Frame(raw[head - 1], raw[head:], raw))
            del self.buf[:total]


# --- setup frames, read from the document -------------------------------------------------------------------------

def pct(percent):
    """radio.md 7.1: levels use integer (percent * 255 + 50) / 100."""
    return (percent * 255 + 50) // 100


def dc(offset):
    """radio.md 7.1: DC uses offset + 128."""
    return offset + 128


def parse_set_freq(payload):
    """SET_FREQ payload (17 B frame, 14 B payload: 00, RX Hz u32 LE, TX Hz u32 LE, RF level, POCSAG Hz u32 LE) -> dict, or None."""
    payload = bytes(payload)
    if len(payload) != 14:
        return None
    return {"zero": payload[0], "rx_hz": int.from_bytes(payload[1:5], "little"), "tx_hz": int.from_bytes(payload[5:9], "little"),
            "rf_level": payload[9], "pocsag_hz": int.from_bytes(payload[10:14], "little")}


def expected_set_freq(rx_hz, tx_hz, rf_level_percent=100):
    """The SET_FREQ frame of radio.md 7.1: `E0 11 04 00`, adjusted RX and TX Hz as u32 LE, RF level, POCSAG Hz equal to the adjusted TX."""
    return (bytes((0xE0, 0x11, 0x04, 0x00)) + rx_hz.to_bytes(4, "little") + tx_hz.to_bytes(4, "little")
            + bytes((pct(rf_level_percent),)) + tx_hz.to_bytes(4, "little"))


# frame offsets of a version 2 SET_CONFIG (radio.md 7.1: "frame 8/9 are TX/RX DC, 10/11 RX/CW levels, 12-16 mode levels, 17
# reserved, 18/19 POCSAG/FM levels, 23-26 hangs/reserved, 28 trunking (zero), 29 colour code, 30 DMR delay, 31 oscillator offset 80")
V2_OFFSETS = {"flags": 3, "enables": 4, "tx_delay": 6, "tx_dc": 8, "rx_dc": 9, "rx_level": 10, "cw_level": 11, "dstar_level": 12,
              "dmr_level": 13, "ysf_level": 14, "p25_level": 15, "nxdn_level": 16, "pocsag_level": 18, "fm_level": 19,
              "trunking": 28, "color_code": 29, "dmr_delay": 30, "oscillator": 31}
V2_ZERO = (5, 7, 17, 20, 21, 22, 23, 24, 25, 26, 27, 28, 32, 33, 34, 35, 36, 37, 38, 39)   # bytes the document leaves at zero
V2_FRAME_LEN = 40
V1_FRAME_LEN = 26


def parse_set_config_v2(payload):
    """The documented fields of a version 2 SET_CONFIG (40 B frame, 37 B payload) as {name: value}, or None for another length."""
    payload = bytes(payload)
    if len(payload) + 3 != V2_FRAME_LEN:
        return None
    frame = bytes((0xE0, V2_FRAME_LEN, SET_CONFIG)) + payload
    out = {name: frame[at] for name, at in V2_OFFSETS.items()}
    out["simplex"] = bool(frame[3] & 0x80)
    out["modes"] = [n for bit, n in ((1, "dstar"), (2, "dmr"), (4, "ysf"), (8, "p25"), (0x10, "nxdn")) if frame[4] & bit]
    out["tx_delay_ms"] = frame[6] * 10
    return out


def expected_set_config_v2(modes=("dstar", "dmr", "ysf"), tx_delay_ms=100, rx_level=50, tx_level=50, cw_id_level=50,
                           dstar_tx_level=None, dmr_tx_level=None, ysf_tx_level=None, tx_dc=0, rx_dc=0, color_code=1,
                           dmr_delay=0, rx_invert=False, tx_invert=False, ptt_invert=False, ysf_lo_dev=False,
                           use_cos_as_lockout=False):
    """The documented bytes of the v2 SET_CONFIG a row produces: the dict {offset: value} of the offsets radio.md 7.1 names plus
    the zero offsets. Offsets 15, 16, 18 and 19 (P25, NXDN, POCSAG, FM levels) are not given a value by the document and are left out."""
    bits = {"dstar": 1, "dmr": 2, "ysf": 4}
    flags = (0x80 | int(rx_invert) | (int(tx_invert) << 1) | (int(ptt_invert) << 2) | (int(ysf_lo_dev) << 3)
             | (int(use_cos_as_lockout) << 5))
    want = {V2_OFFSETS["flags"]: flags, V2_OFFSETS["enables"]: sum(bits[m] for m in modes),
            V2_OFFSETS["tx_delay"]: tx_delay_ms // 10, V2_OFFSETS["tx_dc"]: dc(tx_dc), V2_OFFSETS["rx_dc"]: dc(rx_dc),
            V2_OFFSETS["rx_level"]: pct(rx_level), V2_OFFSETS["cw_level"]: pct(cw_id_level),
            V2_OFFSETS["dstar_level"]: pct(tx_level if dstar_tx_level is None else dstar_tx_level),
            V2_OFFSETS["dmr_level"]: pct(tx_level if dmr_tx_level is None else dmr_tx_level),
            V2_OFFSETS["ysf_level"]: pct(tx_level if ysf_tx_level is None else ysf_tx_level),
            V2_OFFSETS["color_code"]: color_code, V2_OFFSETS["dmr_delay"]: dmr_delay, V2_OFFSETS["oscillator"]: 0x80}
    for at in V2_ZERO:
        want[at] = 0
    return want


def set_config_differences(frame_bytes, want):
    """[(offset, got, wanted)] for the offsets of `want` that the frame (a whole frame, E0 first) does not carry."""
    frame = bytes(frame_bytes)
    return [(at, frame[at] if at < len(frame) else None, value) for at, value in sorted(want.items())
            if at >= len(frame) or frame[at] != value]


# --- transport -----------------------------------------------------------------------------------------------------

class PinnedUdp(mm.UdpTransport):
    """A UDP byte port as the edge sees its peer: datagrams from any address other than `peer` are dropped (counted in .foreign), and
    the peer is never learned from a datagram. `bind` is the tester side address, `peer` the SUT's udp_bind address."""

    def __init__(self, bind, peer):
        super().__init__(bind=bind, peer=peer)
        self.foreign = 0

    def read(self, timeout):
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return b""
        data, addr = self.sock.recvfrom(2048)
        if addr != self.peer:
            self.foreign += 1
            return b""
        return data


# --- the fake modem ------------------------------------------------------------------------------------------------

class Rif5Modem(mm.VirtualModem):
    """The modem DV-Router's adapter needs. A VirtualModem with:

      * protocol version 2 (capabilities D-STAR, DMR, YSF by default) and a 17 byte status payload (a shorter one is a Short
        status to the adapter: radio.md 7.1 "Status offsets differ by version");
      * `frames`: every frame the host wrote, in order, with wall and monotonic time (control frames included; VirtualModem
        records only the voice ones);
      * `mute`: nothing is answered (the frames are still logged), the model of a modem that went silent;
      * `status_mode`: "valid" (default), "short" (a 10 byte payload: bytes flow, no valid status) or "none";
      * the firmware's answers: ACK for SET_FREQ / SET_CONFIG / SET_MODE (NAK 4 for a mode that is not enabled), NAK 2 for a native
        frame of a mode that is disabled or while another mode holds the modem (a frame in idle selects its mode), NAK 5 when the
        mode's buffer has no room, no answer to SEND_CWID (NAK 5 outside idle), NAK 1 for a type it does not know.
    """

    def __init__(self, transport, caps1=CAP_DSTAR | CAP_DMR | CAP_YSF, text="dvtest RIF5 modem 1.0 (no radio)", log=None):
        super().__init__(transport, caps1=caps1, text=text, log=log)
        self.frames = []
        self.mute = False
        self.status_mode = "valid"
        self.enabled_mask = caps1 & 0x07
        self.config_raw = None
        self.freq_raw = None
        self.cw_until = 0.0
        self.naks = []
        self.lockout = False
        # bounded event log for the flow-control timeline (rif5.timeline_lines): ("frame", mono, ftype, length, first payload byte or None)
        # for every host frame but GET_STATUS, ("status", mono, mode, flags, D-STAR space, DMR1 space, DMR2 space, YSF space, fifo bytes or None) for
        # every valid reply, ("fw", mono, event, detail dict) for every event of the firmware-like model (dvtest/mmdvm_fw.py, DVT_RIF5FW)
        self.events = deque(maxlen=EVENT_LIMIT)

    # --- the host's frames ---
    def _handle(self, ftype, payload):
        now = time.monotonic()
        host = HostFrame(time.time(), now, ftype, bytes(payload))
        with self.lock:
            self.frames.append(host)
            if ftype != GET_STATUS:
                self.events.append(("frame", now, ftype, len(host.payload), host.payload[0] if host.payload else None))
        if ftype in mm.PLAYOUT_MS or ftype in (DSTAR_LOST, DMR_LOST2, YSF_LOST):
            kind = mm.MODE_OF_TYPE.get(ftype)
            with self.lock:
                self.rx.append(mm.Rx(host.t, now, ftype, host.payload, kind))
        if self.mute:
            return
        if ftype == GET_VERSION:
            self.counts["version"] += 1
            self.send(GET_VERSION, self.version_payload())
        elif ftype == GET_STATUS:
            self.counts["status"] += 1
            if self.status_mode == "valid":
                body = self.status_payload(now)
                with self.lock:
                    self.events.append(("status", now, body[0], body[1], body[3], body[4], body[5], body[6], self.fifo_bytes(now)))
                self.send(GET_STATUS, body)
            elif self.status_mode == "short":
                self.send(GET_STATUS, self.status_payload(now)[:10])
        elif ftype == SET_FREQ:
            self.counts["freq"] += 1
            self.freq_raw = host.payload
            self.send(ACK, bytes((SET_FREQ,)))
        elif ftype == SET_CONFIG:
            self.counts["config"] += 1
            self.config_raw = host.payload
            if len(host.payload) >= 2:
                self.enabled_mask = host.payload[1] & 0x07
            self.mode = MODE_IDLE
            self.send(ACK, bytes((SET_CONFIG,)))
            self.configured.set()
        elif ftype == SET_MODE:
            self.counts["mode"] += 1
            want = host.payload[0] if host.payload else 255
            if want == MODE_IDLE or (want in MODE_NAME and self.enabled_mask & (1 << (want - 1))):
                self.mode = want
                self.send(ACK, bytes((SET_MODE,)))
            else:
                self._nak(SET_MODE, NAK_BAD_VALUE)
        elif ftype == SEND_CWID:
            if self.mode != MODE_IDLE:
                self._nak(SEND_CWID, NAK_FULL)
            else:
                self.cw_until = now + 0.05 * cw_bits(host.payload)
        elif ftype in TX_TYPES["dstar"] + TX_TYPES["dmr"] + TX_TYPES["ysf"]:
            self._native(ftype, now, host.payload)
        elif ftype in DMR_OTHER:
            self.send(ACK, bytes((ftype,)))
        else:
            self._nak(ftype, NAK_UNKNOWN)

    def _nak(self, ftype, reason):
        self.naks.append((time.monotonic(), ftype, reason))
        self.send(NAK, bytes((ftype, reason)))

    def _native(self, ftype, now, payload=b""):
        own = MODE_DSTAR if ftype in TX_TYPES["dstar"] else MODE_DMR if ftype == DMR_DATA2 else MODE_YSF
        if not self.enabled_mask & (1 << (own - 1)) or self.mode not in (MODE_IDLE, own):
            self._nak(ftype, NAK_NOT_ACCEPTED)
            return
        key = {MODE_DSTAR: "dstar", MODE_DMR: "dmr2", MODE_YSF: "ysf"}[own]
        need = 4 if ftype == DSTAR_HEADER else 0 if ftype == DSTAR_EOT else 1
        if self.space(key, now) < need:
            self._nak(ftype, NAK_FULL)
            return
        self.mode = own
        self._enqueue(ftype, now)

    # --- the modem's frames ---
    def status_payload(self, now=None):
        now = time.monotonic() if now is None else now
        tx = any(until > now for until in self._busy_until.values()) or now < self.cw_until
        flags = (mm.FLAG_TX if tx else 0) | (mm.FLAG_CD if self.cd else 0) | (mm.FLAG_LOCKOUT if self.lockout else 0)
        body = bytes((self.mode, flags, 0, self.space("dstar", now), self.space("dmr1", now), self.space("dmr2", now),
                      self.space("ysf", now)))
        return body.ljust(STATUS_V2_MIN, b"\x00")

    def keyup(self, mode, frames=None, seconds=3.0, wait=False, **kw):
        """An RF over now. In idle the firmware selects the mode of the signal it decodes, so the status shows it."""
        if self.mode == MODE_IDLE:
            self.mode = MODE_ID[mode]
        return super().keyup(mode, frames, seconds, wait, **kw)

    def play_air(self, mode, air, voice=(), count=0):
        """Play a prepared RF over (a list of (type, payload, delay_ms) as dvtest.mmdvm_rf builds them, possibly damaged by the caller)
        in real time like keyup(); returns the KeyUp."""
        if self.mode == MODE_IDLE:
            self.mode = MODE_ID[mode]
        ku = mm.KeyUp(mode, air, list(voice), count)
        self.keyups.append(ku)
        threading.Thread(target=self._play, args=(ku,), name="mmdvm-keyup", daemon=True).start()
        return ku

    # --- the log ---
    def mark(self):
        """A monotonic time to cut the log with (host_frames(since=...))."""
        return time.monotonic()

    def fifo_bytes(self, now=None):
        """The bytes in the TX FIFO per mode key (the firmware-like model only; the fill model has none)."""
        return None

    def events_between(self, since, until=None):
        """The timeline events (see `events`) with since <= time < until."""
        with self.lock:
            items = list(self.events)
        return [e for e in items if e[1] >= since and (until is None or e[1] < until)]

    def host_frames(self, since=None, until=None, types=None):
        with self.lock:
            items = list(self.frames)
        return [f for f in items if (since is None or f.mono >= since) and (until is None or f.mono < until)
                and (types is None or f.ftype in types)]

    def wait_frame(self, predicate, since=None, timeout=30.0):
        """The first host frame after `since` that satisfies predicate(HostFrame), or None after timeout seconds."""
        end = time.monotonic() + timeout
        while True:
            for f in self.host_frames(since):
                if predicate(f):
                    return f
            if time.monotonic() >= end:
                return None
            time.sleep(0.02)

    def wait_idle_mode(self, since, timeout=30.0):
        """The host's SET_MODE idle (E0 04 03 00) after `since`: the end of the mode hang, the point where another mode may be claimed."""
        return self.wait_frame(lambda f: f.ftype == SET_MODE and f.payload == bytes((MODE_IDLE,)), since, timeout)

    def wait_configured_after(self, since, timeout=60.0):
        """The host's SET_CONFIG after `since` (a fresh dialogue reached its configuration step), or None."""
        return self.wait_frame(lambda f: f.ftype == SET_CONFIG, since, timeout)


# --- CW ID length (radio.md 5.7: 50 ms per encoded bit, 8 lead bits, each symbol, 5 tail bits) ---------------------------

_CW = {" ": 4, "E": 4, "I": 6, "T": 6, "A": 8, "N": 8, "S": 8, "D": 10, "H": 10, "M": 10, "R": 10, "U": 10, "B": 12, "F": 12,
       "G": 12, "K": 12, "L": 12, "V": 12, "W": 12, "5": 12, "C": 14, "O": 14, "P": 14, "X": 14, "Z": 14, "4": 14, "6": 14,
       "J": 16, "Q": 16, "Y": 16, "3": 16, "7": 16, "2": 18, "8": 18, "1": 20, "9": 20, "0": 22}


def cw_bits(callsign):
    """Bits of a firmware CW ID (8 lead + symbols + 5 tail; N0CALL is 89), or 8 + 5 for an empty / unknown text."""
    text = callsign.decode("ascii", "replace") if isinstance(callsign, (bytes, bytearray)) else str(callsign)
    return 8 + sum(_CW.get(ch, 0) for ch in text.upper()) + 5
