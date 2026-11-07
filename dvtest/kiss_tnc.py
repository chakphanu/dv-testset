# SPDX-License-Identifier: MIT
"""A fake KISS TNC for the DP15 RF cases of tier A (cases/test_dp15_rf.py, docs/dp15-cases.md).

The DV-Router AX.25 edge reaches a modem as a TCP CLIENT (`[[edges.ax25.port]] kind = "kiss-tcp"`, `host`,
`port`): it connects to the TNC, writes the KISS parameter frames of every configured channel (TXDELAY, P,
SlotTime, TXtail, FullDuplex, in that order; crates/edge/ax25/src/adapter/tcp.rs `kiss_parameters`), then reads
KISS data frames (AX.25 UI frames without FCS) and writes data frames when it transmits. `FakeKissTnc` is the other
end of that connection, played by the tester:

* it LISTENS on a TCP port (the rig's tester address, a fixed port named in rig/config/dv-router/dp15-rf.toml),
  accepts the edge, and decodes everything the edge writes with `ax25.KissDecoder` (nothing is interpreted: the
  frames are kept as they came, with the arrival time);
* `inject(frame)` writes one KISS data frame to the connected edge: what a modem hears on the air;
* `received` / `data_frames()` are the frames the edge wrote; the parameter frames are in `startup_params()`,
  one list per connection (a reconnect of the edge gives a new list, like a real reconnect gives new parameters).

Frame builders for the cases: `ui_frame` (AX.25 UI frame bytes from callsigns and an information field),
`aprs_message_info` (APRS message information field `:ADDRESSEE:text{id`, addressee padded to 9 characters) and
`decode_message` (the reverse, for a frame the edge transmitted). Pure stdlib; the loopback unit test is
cases/unit/test_kiss_tnc.py. Nothing here talks to a non-loopback address unless the caller binds one.
"""
import socket
import threading
import time
from dataclasses import dataclass, field

from dvtest import ax25

PARAM_COMMANDS = tuple(cmd for cmd, _value in ax25.DVR_PARAMS)      # TXDELAY, P, SlotTime, TXtail, FullDuplex


@dataclass
class KissIn:
    """One KISS frame the edge wrote: arrival time (time.monotonic), connection number (1, 2, ...), channel (port
    nibble), command nibble, the unescaped data bytes."""
    t: float
    conn: int
    channel: int
    command: int
    data: bytes


@dataclass
class _Conn:
    number: int
    sock: socket.socket
    decoder: ax25.KissDecoder = field(default_factory=ax25.KissDecoder)
    alive: bool = True


class FakeKissTnc:
    """A listening KISS TNC. `host` defaults to loopback (unit tests); the rig binds 0.0.0.0. `port` 0 picks a free port
    (see `.port` after `start()`). Use as a context manager."""

    def __init__(self, host="127.0.0.1", port=0, name="tnc"):
        self.host, self.port, self.name = host, port, name
        self.received = []                  # [KissIn] in arrival order, over all connections
        self.connections = 0                # accepted connections so far
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self._listener = None
        self._conns = []                    # [_Conn], the newest last
        self._threads = []
        self._stop = threading.Event()

    # ------------------------------------------------------------ lifecycle

    def start(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(4)
        listener.settimeout(0.2)
        self.port = listener.getsockname()[1]
        self._listener = listener
        thread = threading.Thread(target=self._accept_loop, name="kiss-tnc-accept-%s" % self.name, daemon=True)
        thread.start()
        self._threads.append(thread)
        return self

    def stop(self):
        self._stop.set()
        with self.lock:
            conns, self._conns = list(self._conns), []
        for conn in conns:
            self._close(conn)
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(1.0)
        with self.changed:
            self.changed.notify_all()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    # ------------------------------------------------------------ the two socket loops

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            sock.settimeout(0.2)
            with self.changed:
                self.connections += 1
                conn = _Conn(self.connections, sock)
                self._conns.append(conn)
                self.changed.notify_all()
            thread = threading.Thread(target=self._read_loop, args=(conn,), name="kiss-tnc-read-%s" % self.name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _read_loop(self, conn):
        while not self._stop.is_set() and conn.alive:
            try:
                chunk = conn.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            now = time.monotonic()
            frames = conn.decoder.feed(chunk)
            if frames:
                with self.changed:
                    for channel, command, data, _first in frames:
                        self.received.append(KissIn(now, conn.number, channel, command, data))
                    self.changed.notify_all()
        with self.changed:
            conn.alive = False
            self.changed.notify_all()
        self._close(conn)

    def _close(self, conn):
        conn.alive = False
        try:
            conn.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------ what the edge wrote

    def _snapshot(self):
        with self.lock:
            return list(self.received)

    def is_connected(self):
        with self.lock:
            return any(c.alive for c in self._conns)

    def current_connection(self):
        """The number of the newest live connection, else 0."""
        with self.lock:
            live = [c.number for c in self._conns if c.alive]
        return live[-1] if live else 0

    def wait(self, predicate, timeout):
        """True once predicate(self) holds (checked on every change), False after `timeout` seconds."""
        end = time.monotonic() + timeout
        with self.changed:
            while True:
                if predicate(self):
                    return True
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.changed.wait(min(left, 0.5))

    def wait_connected(self, timeout=30.0):
        return self.wait(lambda tnc: any(c.alive for c in tnc._conns), timeout)

    def startup_params(self, conn=None):
        """[(channel, command, value)] of the parameter frames (commands 1..5) of connection `conn` (default: the
        newest one that wrote any), in the order they arrived; the value is the first data byte."""
        frames = [f for f in self._snapshot() if 1 <= f.command <= 5 and len(f.data) == 1]
        numbers = sorted({f.conn for f in frames})
        if not numbers:
            return []
        conn = numbers[-1] if conn is None else conn
        return [(f.channel, f.command, f.data[0]) for f in frames if f.conn == conn]

    def wait_params(self, count=len(PARAM_COMMANDS), timeout=15.0, conn=None):
        """The parameter frames of the connection once `count` of them arrived, else what is there after `timeout`."""
        self.wait(lambda tnc: len(tnc.startup_params(conn)) >= count, timeout)
        return self.startup_params(conn)

    def data_frames(self, since=0.0, conn=None):
        """[(arrival time, channel, frame bytes)] of the KISS DATA frames (command 0) the edge wrote: the frames it
        transmitted. `since` is a time.monotonic() value."""
        return [(f.t, f.channel, f.data) for f in self._snapshot()
                if f.command == 0 and f.t >= since and (conn is None or f.conn == conn)]

    def other_frames(self):
        """KISS frames that are neither data nor one of the five parameter commands (SetHardware, Return, ...). The edge
        never writes them (data.md 10.4): a non-empty list is a finding."""
        return [f for f in self._snapshot() if f.command > 5]

    def wait_data(self, count=1, timeout=10.0, since=0.0):
        """The data frames since `since` once there are `count`, else what is there after `timeout`."""
        self.wait(lambda tnc: len(tnc.data_frames(since)) >= count, timeout)
        return self.data_frames(since)

    def decoded(self, since=0.0):
        """[(arrival time, parsed frame dict, tnc2 text)] of the transmitted data frames that parse as UI frames
        (dvtest.ax25.parse_frame, to_tnc2); a frame that does not parse is skipped (see data_frames)."""
        out = []
        for at, _channel, frame in self.data_frames(since):
            try:
                out.append((at, ax25.parse_frame(frame), ax25.to_tnc2(frame)))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------ what the modem "hears"

    def inject(self, frame, channel=0):
        """Write one KISS data frame (AX.25 frame bytes, no FCS) to the newest live connection; False when no edge is
        connected or the write failed."""
        with self.lock:
            live = [c for c in self._conns if c.alive]
        if not live:
            return False
        conn = live[-1]
        try:
            conn.sock.sendall(ax25.kiss_encode(channel, 0, frame))
        except OSError:
            self._close(conn)
            return False
        return True

    def inject_raw(self, data):
        """Write raw bytes (a malformed KISS stream) to the newest live connection."""
        with self.lock:
            live = [c for c in self._conns if c.alive]
        if not live:
            return False
        try:
            live[-1].sock.sendall(data)
        except OSError:
            return False
        return True

    def drop_connection(self):
        """Close the newest live connection from our side; the edge reconnects (backoff of its `reconnect`) and writes
        its parameter frames again."""
        with self.lock:
            live = [c for c in self._conns if c.alive]
        if live:
            self._close(live[-1])


# ---------------------------------------------------------------- frame builders and decoders

def aprs_message_info(addressee, text, msg_id=None):
    """APRS message information field `:ADDRESSEE:text{id` as bytes: the addressee is padded with spaces to 9
    characters, `msg_id` (1 to 5 characters) is optional. A message without an id is never acked."""
    if not 1 <= len(addressee) <= 9:
        raise ValueError("an APRS addressee is 1 to 9 characters: %r" % (addressee,))
    body = ":%-9s:%s" % (addressee, text)
    if msg_id is not None:
        if not 1 <= len(str(msg_id)) <= 5:
            raise ValueError("a message id is 1 to 5 characters: %r" % (msg_id,))
        body += "{%s" % msg_id
    return body.encode("latin-1")


def ui_frame(source, destination, info, digis=()):
    """AX.25 UI frame bytes (no FCS) `SOURCE>DESTINATION[,DIGI...]:info`; `info` is bytes or str (Latin-1).
    Callsigns are upper case with an optional -SSID; a digipeater with `*` has been repeated."""
    if isinstance(info, bytes):
        info = ax25.info_to_text(info)
    path = destination + "".join("," + d for d in digis)
    return ax25.from_tnc2("%s>%s:%s" % (source, path, info))


def decode_message(frame):
    """For a frame the edge transmitted: {src, dst, digis, addressee, text, msg_id} when it is a UI frame whose
    information field is an APRS message (`:ADDRESSEE:text[{id]`), else None. The addressee is stripped."""
    try:
        parsed = ax25.parse_frame(frame)
    except ValueError:
        return None
    info = parsed["info"]
    if len(info) < 11 or info[0:1] != b":" or info[10:11] != b":":
        return None
    text = info[11:].decode("latin-1")
    msg_id = None
    if "{" in text:
        text, _sep, msg_id = text.rpartition("{")
    return {"src": parsed["src"]["text"], "dst": parsed["dest"]["text"], "digis": [d["text"] for d in parsed["digis"]],
            "addressee": info[1:10].decode("latin-1").rstrip(), "text": text, "msg_id": msg_id,
            "info": info}
