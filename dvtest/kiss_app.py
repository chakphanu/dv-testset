# SPDX-License-Identifier: MIT
"""A headless KISS-over-TCP APP client for the DP11e cases (docs/dp11e-plan.md): the other side of a KISS server in DV-Router.

dvtest/kiss_tnc.py plays a TNC that the DV-Router AX.25 edge connects TO. DP11e turns the edge into the server: an APRS app
(APRSdroid, PinPoint APRS, Xastir, YAAC, Dire Wolf's kissutil) connects to a `[[edges.ax25.kiss_server]]` listener and uses the node as its TNC.
`AppKissClient` is such an app, pure stdlib, built on the same framing code as everything else (dvtest/ax25.py `kiss_encode`, `KissDecoder`):

* `connect()` once (no retry loop: a refusal must be observable); `source_ip` binds the local side, for an allow_from case; `rcvbuf` sets a small
  SO_RCVBUF before the connect;
* `closed_at` / `wait_closed()` / `bytes_received`: a refused or kicked client sees EOF, and "zero bytes" is a counter, not a guess;
* `send_frame` (KISS data frame), `send_command` (TXDELAY, P, SlotTime, TXtail, FullDuplex, SetHardware, Return: the server must ignore them),
  `send_text` (the ASCII init lines APRSdroid writes to a TNC at connect), `send_raw`, `send_split` (a frame in pieces);
* `pause_reading()` / `resume_reading()`: a client that stops reading, for the slow-client drop and the write stall of the server;
* `frames()` / `wait_frames()`: the data frames the server delivered, with the KISS channel nibble and the arrival time;
* `lone_fends()` / `fend_intervals()` / `max_silence()` / `would_time_out()`: the idle keepalive of the server (a lone FEND byte written to an
  idle client, DP11e `keepalive_secs`) with its timestamps, the longest stretch without any byte, and whether an app with a read timeout (APRSdroid:
  `tcp.sotimeout`, 120 s) would have dropped the link.

App models, taken from the apps' own code and documents (cited in docs/dp11e-plan.md): `aprsdroid_frames` builds what APRSdroid transmits,
`aprsdroid_init_bytes` its optional TNC init string, `AprsdroidReader` how its KISS reader accepts frames (a data frame of channel 0 only),
`pinpoint_tx` a PinPoint APRS transmit on a chosen KISS TX port. Nothing here talks to a non-loopback address unless the caller names one.
The loopback unit test is cases/unit/test_kiss_app.py.
"""
import socket
import threading
import time
import urllib.parse

from dvtest import ax25, kiss_tnc


class AppKissClient:
    def __init__(self, host, port, name="app", source_ip=None, timeout=5.0, rcvbuf=None):
        self.host, self.port, self.name = host, int(port), name
        self.source_ip, self.timeout, self.rcvbuf = source_ip, timeout, rcvbuf
        self.received = []                  # [(time.monotonic, channel, command, data)] in arrival order
        self.bytes_received = 0
        self.byte_times = []                # [(time.monotonic, nbytes)] one entry per recv
        self.fend_times = []                # [time.monotonic] of every lone FEND (a chunk of FEND bytes only, outside any frame)
        self.connected_at = None
        self.closed_at = None
        self.error = None                   # why connect() failed, else None
        self.lock = threading.Condition()
        self._sock = None
        self._thread = None
        self._decoder = ax25.KissDecoder()
        self._stop = threading.Event()
        self._reading = threading.Event()
        self._reading.set()
        self._parked = threading.Event()    # set by the reader loop while it is paused: pause_reading() waits for it

    # ------------------------------------------------------------ connection

    def connect(self):
        """True when the TCP connection was made (a server may still close it at once: see `closed_at`). One attempt."""
        try:
            if self.rcvbuf:
                # a small receive buffer must be set BEFORE the connect (it fixes the window): the slow-client case needs the kernel buffers to fill
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(self.rcvbuf))
                sock.settimeout(self.timeout)
                if self.source_ip:
                    sock.bind((self.source_ip, 0))
                sock.connect((self.host, self.port))
            else:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout,
                                                source_address=(self.source_ip, 0) if self.source_ip else None)
        except OSError as exc:
            self.error = str(exc)
            return False
        sock.settimeout(0.1)
        self._sock = sock
        self.connected_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name="kiss-app-%s" % self.name, daemon=True)
        self._thread.start()
        return True

    def _read_loop(self):
        while not self._stop.is_set():
            if not self._reading.is_set():
                self._parked.set()
                time.sleep(0.02)
                continue
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            now = time.monotonic()
            idle = not self._decoder.pending()
            frames = self._decoder.feed(chunk)
            with self.lock:
                self.byte_times.append((now, len(chunk)))
                if idle and chunk.count(ax25.FEND) == len(chunk):
                    self.fend_times.extend([now] * len(chunk))                 # a keepalive: FEND bytes alone, between frames
                self.bytes_received += len(chunk)
                self.received.extend((now, ch, cmd, data) for ch, cmd, data, _first in frames)
                self.lock.notify_all()
        with self.lock:
            if self.closed_at is None and not self._stop.is_set():
                self.closed_at = time.monotonic()
            self.lock.notify_all()

    @property
    def is_open(self):
        return self._sock is not None and self.closed_at is None and not self._stop.is_set()

    def wait_closed(self, timeout=5.0):
        """True once the server closed the connection (EOF or reset), within `timeout` seconds."""
        end = time.monotonic() + timeout
        with self.lock:
            while self.closed_at is None:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.lock.wait(min(left, 0.2))
        return True

    def pause_reading(self, wait=2.0):
        """Stop calling recv: the kernel buffers fill and the server's writes back up (a slow client). Returns once the reader loop has
        parked (True), so no recv is in flight any more and nothing can be consumed after the call; False when it did not park in `wait` s."""
        self._reading.clear()
        self._parked.clear()
        return self._parked.wait(wait)

    def resume_reading(self):
        self._reading.set()

    def close(self):
        self._stop.set()
        if self._sock is not None:
            for call in (lambda: self._sock.shutdown(socket.SHUT_RDWR), self._sock.close):
                try:
                    call()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(1.0)

    def __enter__(self):
        if not self.connect():
            raise OSError("cannot connect to %s:%s: %s" % (self.host, self.port, self.error))
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------ writing

    def send_raw(self, data):
        """Write bytes as they are; False when the write failed (the server closed the connection)."""
        try:
            self._sock.sendall(data)
        except (OSError, AttributeError):
            return False
        return True

    def send_frame(self, frame, channel=0):
        """One KISS data frame carrying an AX.25 frame (no FCS)."""
        return self.send_raw(ax25.kiss_encode(channel, 0, frame))

    def send_command(self, command, value=b"\x00", channel=0):
        """A KISS command other than data: 1 TXDELAY, 2 P, 3 SlotTime, 4 TXtail, 5 FullDuplex, 6 SetHardware, 15 Return (with channel 15 the byte 0xFF)."""
        if command == 0:
            raise ValueError("command 0 is a data frame: use send_frame")
        return self.send_raw(ax25.kiss_encode(channel, command, value))

    def send_text(self, text):
        """ASCII lines as a TNC init string: every line + CRLF (APRSdroid writes `kiss.init` this way, with a delay after each line)."""
        return self.send_raw(b"".join(line.encode("ascii") + b"\r\n" for line in text.split("\n")))

    def send_split(self, data, size=3, gap=0.02):
        """`data` in pieces of `size` bytes with `gap` seconds between them: a frame split across TCP segments."""
        for i in range(0, len(data), size):
            if not self.send_raw(data[i:i + size]):
                return False
            time.sleep(gap)
        return True

    # ------------------------------------------------------------ reading

    def frames(self, since=0.0):
        """[(arrival time, channel, frame bytes)] of the data frames (command 0) received at or after `since` (time.monotonic)."""
        with self.lock:
            return [(t, ch, data) for t, ch, cmd, data in self.received if cmd == 0 and t >= since]

    def lone_fends(self, since=0.0):
        """Times (time.monotonic) of the lone FEND bytes the server wrote while the client was idle. A chunk made only of FEND bytes, arriving
        between frames, counts one per byte; the opening FEND of a frame arrives with the frame and is not counted. TCP may merge two writes
        that are less than a read apart, which a keepalive of seconds never is."""
        with self.lock:
            return [t for t in self.fend_times if t >= since]

    def fend_intervals(self, since=0.0):
        """Seconds between consecutive lone FENDs (the keepalive period as seen by the client)."""
        times = self.lone_fends(since)
        return [round(b - a, 3) for a, b in zip(times, times[1:])]

    def max_silence(self, start, end):
        """The longest stretch in [start, end] (time.monotonic) with no byte received at all, from `start` to the first byte, between bytes
        and from the last byte to `end`."""
        with self.lock:
            marks = sorted(t for t, _n in self.byte_times if start <= t <= end)
        edges = [start] + marks + [end]
        return max(b - a for a, b in zip(edges, edges[1:]))

    def would_time_out(self, timeout_s, start, end):
        """True when an app with a read timeout of `timeout_s` seconds (APRSdroid: tcp.sotimeout, default 120) would have dropped the link
        somewhere in [start, end]: the longest silence is at least the timeout. A timeout of 0 means disabled."""
        return bool(timeout_s) and self.max_silence(start, end) >= timeout_s

    def other_commands(self):
        """[(channel, command, data)] the server sent that are not data frames (a KISS server has no reason to)."""
        with self.lock:
            return [(ch, cmd, data) for _t, ch, cmd, data in self.received if cmd != 0]

    def wait_frames(self, count=1, timeout=10.0, since=0.0):
        """The data frames since `since` once there are `count`, else what is there after `timeout` seconds."""
        end = time.monotonic() + timeout
        with self.lock:
            while len([1 for t, _ch, cmd, _d in self.received if cmd == 0 and t >= since]) < count:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.lock.wait(min(left, 0.2))
        return self.frames(since)


# ---------------------------------------------------------------- app models

APRSDROID_TOCALL = "APDR16"                 # "APDR" + the app version (AprsService.scala:71); 16 is the version the DP11e design names
APRSDROID_PATH = ("WIDE1-1",)               # the default of the preference digi_path (AprsService.scala:229, res/xml/preferences.xml)
APRSDROID_SO_TIMEOUT_S = 120                # the default of tcp.sotimeout: a silent server makes the app drop and reconnect after 30 s (TcpUploader.scala:19-20,131,173)


def aprsdroid_frames(source, position_info, message_info=None, tocall=APRSDROID_TOCALL, path=APRSDROID_PATH):
    """The AX.25 frames an APRSdroid-like app transmits: its position report, then optionally a message, both
    `SOURCE>APDR16,WIDE1-1:<info>` as UI frames with PID 0xF0. Returns [frame bytes]."""
    infos = [position_info] + ([message_info] if message_info is not None else [])
    return [kiss_tnc.ui_frame(source, tocall, info, digis=path) for info in infos]


def aprsdroid_init_bytes(init_string):
    """What APRSdroid writes to the socket before any frame when the preference kiss.init is set: the URL-decoded string split on
    newlines, every line followed by CRLF (KissProto.scala:22-32). Empty (the default): nothing."""
    text = urllib.parse.unquote(init_string)
    if not text:
        return b""
    return b"".join(line.encode("latin-1") + b"\r\n" for line in text.split("\n"))


class AprsdroidReader:
    """How APRSdroid's KISS reader takes the server's bytes (KissProto.scala:39-81): FEND ends a frame when the buffer is not empty; the
    byte 0x00 at the START of a frame is the (ignored) data command, any other byte is frame content. So only a data frame of KISS
    channel 0 is read: a frame of another channel (type byte 0x10, 0x20, ...) keeps that byte as the first address byte and the AX.25
    parse fails. `feed(bytes)` returns the frames (bytes of the AX.25 frame) it would hand to its parser successfully."""

    def __init__(self):
        self.buf = bytearray()
        self.esc = False
        self.dropped = 0

    def feed(self, data):
        out = []
        for ch in data:
            if self.esc:
                self.esc = False
                if ch == ax25.TFEND:
                    self.buf.append(ax25.FEND)
                elif ch == ax25.TFESC:
                    self.buf.append(ax25.FESC)
                continue
            if ch == ax25.FEND:
                if self.buf:
                    try:
                        ax25.parse_frame(bytes(self.buf))
                        out.append(bytes(self.buf))
                    except (ValueError, KeyError, IndexError):
                        self.dropped += 1
                    self.buf = bytearray()
            elif ch == ax25.FESC:
                self.esc = True
            elif ch == 0 and not self.buf:
                continue
            else:
                self.buf.append(ch)
        return out


def pinpoint_tx(frame, kiss_tx_port=0):
    """What PinPoint APRS writes for a transmit: a KISS data frame on its `KISS TX port` (0..15, default 0). PinPoint listens to ALL
    KISS ports 0..15 while receiving (Quick Start Guide v2.2, TNC tab)."""
    return ax25.kiss_encode(kiss_tx_port, 0, frame)
