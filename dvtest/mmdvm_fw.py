# SPDX-License-Identifier: MIT
"""A firmware-like TX path for the RIF5 fake modem (switch DVT_RIF5FW=on; docs/rif5-cases.md "The firmware-like fake").

The default fake (dvtest/mmdvm_serial.Rif5Modem on dvtest/mmdvm_modem.VirtualModem) keeps a fill model: capacity 10 / 10 / 8 frames, space freed
continuously at the air rate, a D-STAR header of 80 ms, no TX-delay preamble, no underrun. DV-Router's design-276 (RIF5j, section 1.2 and 1.9) lists
what MMDVM and MMDVM_HS firmware do instead; this module models those facts, from source and not from measurement (their RIF13 checks H3 and H4 confirm
them on boards). Pure: every method takes the time as an argument (monotonic seconds), so the unit tests run in virtual time.

Facts used (design-276 1.2, M/ = refs/MMDVM 1aa1034, HS/ = refs/MMDVM_HS bf66faf; file:line as that note gives them):

  FIFO in bytes     MMDVM: D-STAR 370 B (M/RingBuffer.h:47, M/DStarTX.cpp:194), DMR simplex 370 B (M/DMRDMOTX.cpp:54), YSF 4000 B (M/Globals.h:110-114,
                    M/YSFTX.cpp:50). HS: 1000 B (HS/SerialRB.h:34,38), 875 B (HS/DMRDMOTX.cpp:34), 1500 B (HS/YSFTX.cpp:30).
  stored per frame  D-STAR header 42 B (1 + 41), data 13 B (1 + 12), EOT 1 B (M/DStarTX.cpp:286-334); DMR 33 B (M/DMRDMOTX.cpp:110-123); YSF 120 B
                    (M/YSFTX.cpp:123-134). NAK 5 when the frame does not fit (M/DStarTX.cpp:291-294, M/DMRDMOTX.cpp:115-117, M/YSFTX.cpp:128-130).
  space unit        floor(free bytes / 13) D-STAR (M/DStarTX.cpp:447-450), / 35 DMR (M/DMRDMOTX.cpp:154-157), / 120 YSF (M/YSFTX.cpp:185-188): idle 28 /
                    10 / 33 on MMDVM, 76 / 25 / 12 on HS. In simplex the DMR slot 1 byte is a constant (10 on MMDVM, M/SerialPort.cpp:219-221).
  airtime           D-STAR header 85 B = 141.7 ms, data 20 ms, EOT 30 ms (M/DStarTX.cpp:217-263); DMR burst 60 ms; YSF frame 100 ms.
  when space frees  a whole frame leaves the FIFO when its play-out STARTS (M/DStarTX.cpp:212-262, M/DMRDMOTX.cpp:72-87, M/YSFTX.cpp:73-85).
  TX-delay preamble only when TX is off and a frame is at the head; the FIFO is not consumed meanwhile. MMDVM: D-STAR 500 ms + tx_delay (max 1 s;
                    the source comment says 250 ms, design-276 U-P), DMR and YSF 500 ms + tx_delay (max 1 s); HS D-STAR 250 ms + tx_delay. D-STAR DATA
                    after TX went off is aired with no preamble.
  underrun          when nothing is left to play, TX goes off (M/IO.cpp:336-340). A frame of the same over that arrives later is an underrun.

A model, not a firmware: no RSSI, no sample buffer, no DMR fill bursts, the underrun window below is our own rule.
"""
import time
from collections import deque, namedtuple

from . import mmdvm_modem as mm
from . import mmdvm_serial as ms

PROFILES = {
    # name: FIFO bytes per key, DMR slot 1 byte, D-STAR preamble base (s), DMR / YSF preamble base (s)
    "mmdvm": {"cap": {"dstar": 370, "dmr2": 370, "ysf": 4000}, "dmr1": 10, "pre_dstar": 0.5, "pre_other": 0.5},
    "hs": {"cap": {"dstar": 1000, "dmr2": 875, "ysf": 1500}, "dmr1": 25, "pre_dstar": 0.25, "pre_other": 0.5},
}
UNIT = {"dstar": 13, "dmr2": 35, "ysf": 120}                  # bytes per space unit
PREAMBLE_MAX_S = 1.0
UNDERRUN_WINDOW_S = 1.0               # a frame of the same mode this soon after TX went dry, without an end frame before, is an underrun (our rule)

Frame = namedtuple("Frame", "key ftype stored air end")       # end: the frame ends an over (D-STAR EOT, DMR / YSF terminator)


def key_of(ftype):
    """The FIFO key of a host frame type, or None."""
    if ftype in (ms.DSTAR_HEADER, ms.DSTAR_DATA, ms.DSTAR_EOT):
        return "dstar"
    return {ms.DMR_DATA2: "dmr2", ms.YSF_DATA: "ysf"}.get(ftype)


def stored_bytes(ftype):
    return {ms.DSTAR_HEADER: 42, ms.DSTAR_DATA: 13, ms.DSTAR_EOT: 1, ms.DMR_DATA2: 33, ms.YSF_DATA: 120}[ftype]


def airtime_s(ftype):
    return {ms.DSTAR_HEADER: 0.1417, ms.DSTAR_DATA: 0.020, ms.DSTAR_EOT: 0.030, ms.DMR_DATA2: 0.060, ms.YSF_DATA: 0.100}[ftype]


def is_end_frame(ftype, payload):
    """True for the frame that ends an over: a D-STAR EOT, a DMR terminator burst (data burst whose slot type is 2) or a YSF terminator (FICH FI 2).
    A DMR header is also a data burst; a TX that went dry right after a header is not told apart from a terminator (no case does that)."""
    if ftype == ms.DSTAR_EOT:
        return True
    try:
        if ftype == ms.DMR_DATA2:
            from . import mmdvm_rf as rf
            return rf.classify_burst(bytes(payload[1:])) == "data"
        if ftype == ms.YSF_DATA:
            from .proto import ysf as ysfp
            fich = ysfp.fich_decode(bytes(payload[6:31]))
            return bool(fich) and fich["fi"] == ysfp.FI_TERMINATOR
    except (ValueError, KeyError, IndexError, TypeError):
        return False
    return False


class FwModel:
    """The TX FIFO and play-out of one modem. `tx_delay_ms` is a number or a callable (the SET_CONFIG value is read when a preamble starts).
    `emit(t, event, **detail)` receives every model event: write (accepted, with the FIFO bytes), nak, preamble_start, preamble_end, pop, tx_off,
    underrun. Times are absolute (the caller's clock); events of the past are emitted when the model is advanced to a later time."""

    def __init__(self, profile="mmdvm", tx_delay_ms=100, emit=None):
        if profile not in PROFILES:
            raise ValueError("unknown firmware profile %r, one of %s" % (profile, sorted(PROFILES)))
        self.profile = profile
        self.p = PROFILES[profile]
        self.tx_delay_ms = tx_delay_ms
        self.emit = emit or (lambda t, event, **detail: None)
        self.fifo = {key: deque() for key in self.p["cap"]}
        self.used = {key: 0 for key in self.p["cap"]}
        self.tx = False
        self.pre_end = None
        self.cur_end = None
        self.dry_at = None
        self.dry_key = None
        self.last_end_frame = True        # nothing played yet: the next frame starts an over
        self.playing = None
        self.counts = {"underrun": 0, "nak": 0, "pop": 0}

    # --- numbers
    def delay_ms(self):
        value = self.tx_delay_ms() if callable(self.tx_delay_ms) else self.tx_delay_ms
        return max(0, min(500, value))

    def preamble_s(self, key, ftype):
        """The TX-delay preamble before `ftype` when TX is off. D-STAR data: none."""
        if key == "dstar":
            if ftype != ms.DSTAR_HEADER:
                return 0.0
            return min(PREAMBLE_MAX_S, self.p["pre_dstar"] + self.delay_ms() / 1000.0)
        return min(PREAMBLE_MAX_S, self.p["pre_other"] + self.delay_ms() / 1000.0)

    def idle_space(self, key):
        return self.p["cap"][key] // UNIT[key]

    def fifo_bytes(self):
        return dict(self.used)

    # --- time
    def _start_next(self, t):
        frame = self.fifo[self._head_key()].popleft()
        self.used[frame.key] -= frame.stored
        self.cur_end = t + frame.air
        self.playing = frame
        self.counts["pop"] += 1
        self.emit(t, "pop", type=ms.name_of(frame.ftype), air_ms=round(frame.air * 1000, 1), fifo_bytes=self.fifo_bytes())

    def _head_key(self):
        keys = [k for k, q in self.fifo.items() if q]
        return keys[0]

    def advance(self, now):
        """Process every play-out transition up to `now`."""
        while True:
            if self.pre_end is not None:
                if self.pre_end > now:
                    return
                t, self.pre_end = self.pre_end, None
                self.emit(t, "preamble_end")
                self._start_next(t)
            elif self.cur_end is not None:
                if self.cur_end > now:
                    return
                t, self.cur_end = self.cur_end, None
                self.last_end_frame = self.playing.end
                if any(self.fifo.values()):
                    self._start_next(t)
                else:
                    self.tx = False
                    self.dry_at, self.dry_key = t, self.playing.key
                    self.emit(t, "tx_off", end_frame=bool(self.playing.end))
            else:
                return

    def tx_on(self, now):
        self.advance(now)
        return self.tx

    def space(self, key, now):
        self.advance(now)
        if key == "dmr1":
            return self.p["dmr1"]
        return (self.p["cap"][key] - self.used[key]) // UNIT[key]

    # --- the host's frames
    def write(self, ftype, payload, now):
        """A native frame from the host at `now`. True when stored; False is the firmware's NAK 5 (the frame does not fit)."""
        key = key_of(ftype)
        self.advance(now)
        stored = stored_bytes(ftype)
        if self.used[key] + stored > self.p["cap"][key]:
            self.counts["nak"] += 1
            self.emit(now, "nak", reason=ms.NAK_FULL, type=ms.name_of(ftype), fifo_bytes=self.fifo_bytes())
            return False
        if (not self.tx and self.dry_at is not None and self.dry_key == key and not self.last_end_frame
                and now - self.dry_at <= UNDERRUN_WINDOW_S):
            self.counts["underrun"] += 1
            self.emit(now, "underrun", dry_s=round(now - self.dry_at, 4), type=ms.name_of(ftype))
        self.fifo[key].append(Frame(key, ftype, stored, airtime_s(ftype), is_end_frame(ftype, payload)))
        self.used[key] += stored
        self.emit(now, "write", type=ms.name_of(ftype), fifo_bytes=self.fifo_bytes())
        if not self.tx:
            self.tx = True
            pre = self.preamble_s(key, ftype)
            if pre > 0:
                self.pre_end = now + pre
                self.emit(now, "preamble_start", duration_ms=round(pre * 1000, 1))
            else:
                self._start_next(now)
        return True


class FwRif5Modem(ms.Rif5Modem):
    """Rif5Modem whose TX path is the FwModel: space in FIFO bytes / unit, a frame leaves the FIFO when its play-out starts, a preamble before the first
    frame of an over, NAK 5 on overflow, PTT (the TX flag) off when the FIFO runs dry, and `fw` events in `events` for the timeline. Everything else
    (dialogue, NAKs for mode and enables, RF key-up, silence) is the parent's."""

    def __init__(self, transport, profile="mmdvm", tx_delay_ms=None, **kw):
        super().__init__(transport, **kw)
        self.profile = profile
        self._delay_override = tx_delay_ms
        self.fw = FwModel(profile, self._tx_delay_ms, self._fw_event)

    def _tx_delay_ms(self):
        if self._delay_override is not None:
            return self._delay_override
        raw = self.config_raw
        return raw[3] * 10 if raw is not None and len(raw) > 3 else 100          # SET_CONFIG frame byte 6 = tx delay / 10 (radio.md 7.1)

    def _fw_event(self, t, event, **detail):
        self.events.append(("fw", t, event, detail))

    def fifo_bytes(self, now=None):
        self.fw.advance(time.monotonic() if now is None else now)
        return self.fw.fifo_bytes()

    def fw_events(self, since, until=None, names=None):
        """[(t, event, detail)] of the model between the times (a past event is stamped with the time it happened)."""
        return [(e[1], e[2], e[3]) for e in self.events_between(since, until) if e[0] == "fw" and (names is None or e[2] in names)]

    def _native(self, ftype, now, payload=b""):
        own = ms.MODE_DSTAR if ftype in ms.TX_TYPES["dstar"] else ms.MODE_DMR if ftype == ms.DMR_DATA2 else ms.MODE_YSF
        if not self.enabled_mask & (1 << (own - 1)) or self.mode not in (ms.MODE_IDLE, own):
            self._nak(ftype, ms.NAK_NOT_ACCEPTED)
            return
        with self.lock:
            accepted = self.fw.write(ftype, payload, now)
        if not accepted:
            self._nak(ftype, ms.NAK_FULL)
            return
        self.mode = own

    def space(self, key, now=None):
        with self.lock:
            return self.fw.space(key, time.monotonic() if now is None else now)

    def status_payload(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            tx = self.fw.tx_on(now) or now < self.cw_until
            # a mode the SET_CONFIG did not enable reports no space (review-277: firmware 0); the DMR slot 1 byte stays the fixed value
            enabled = {"dstar": 1, "dmr1": 0, "dmr2": 2, "ysf": 4}
            spaces = [self.fw.space(k, now) if not enabled[k] or self.enabled_mask & enabled[k] else 0 for k in ("dstar", "dmr1", "dmr2", "ysf")]
        flags = (mm.FLAG_TX if tx else 0) | (mm.FLAG_CD if self.cd else 0) | (mm.FLAG_LOCKOUT if self.lockout else 0)
        return bytes((self.mode, flags, 0) + tuple(spaces)).ljust(ms.STATUS_V2_MIN, b"\x00")
