# SPDX-License-Identifier: MIT
"""Fake extended AMBEd transcoder with a deterministic bit-exact transform.

It plays the transcoder side towards the DV-Router worker dv-codec-ambed (or
any other client of the extended AMBEd protocol): PING -> PONG, CAPS -> the
same 15 B reply for its whole life, OPEN -> STD or BUSY, one data socket per
stream, CS frees a stream. Data replies carry the test transform of
dvtest.proto.ambed_ext, so a case can predict every payload byte.

Run time behaviour is switched through set_modes() or through a small JSON
control socket (one datagram in, one datagram out), see docs/ambed-fake.md.

    python -m dvtest.fake_ambed --bind IP [--port 10100] [--control-port 10190]

Thread based: one thread for the AMBEd control port, one for the JSON control
port, one scheduler thread for delayed sends and one thread per open stream.
All of them block on sockets with a timeout or on a condition variable.
"""
import argparse
import heapq
import json
import os
import signal
import socket
import struct
import threading
import time

from dvtest.proto import ambed_ext as ext

CONTROL_PORT = 10190
DATA_PORTS = (10200, 10299)   # default range of the stream data ports (lowest free first)
OTHER_STREAM = 900            # stream id of the extra STD of std_then_other
RECV_TIMEOUT = 0.05           # data threads: also the stop latency
JSON_LIMIT = 60000            # bytes of one control reply

OPEN_MODES = ("std", "busy", "silent", "late", "std_twice", "std_then_other")
DATA_MODES = ("echo", "silent", "wrong_source", "delay", "wrong_codec",
              "trailing_bytes", "truncated_after_complete_entry")
CAPS_MODES = ("normal", "delay", "drop", "changed")
PONG_MODES = ("normal", "silent")
LEN_CHECKS = ("strict", "lenient")


def default_modes(lenient_open=False):
    return {
        "open": "std", "open_delay_ms": 1200,
        "data": "echo", "data_delay_ms": 100, "trailing_n": 1, "keep_entries": 1,
        "caps": "normal", "caps_delay_ms": 1200, "caps_drop_n": None,
        "pong": "normal",
        "max_sessions": None,
        "len_check": "strict",
        "lenient_open": bool(lenient_open),
    }


class _Scheduler(threading.Thread):
    """Runs callables at a due time; sleeps on a condition, never spins."""

    def __init__(self):
        super().__init__(name="fake-ambed-sched", daemon=True)
        self.cond = threading.Condition()
        self.heap = []
        self.seq = 0
        self.stopping = False

    def after(self, delay, func):
        with self.cond:
            self.seq += 1
            heapq.heappush(self.heap, (time.monotonic() + delay, self.seq, func))
            self.cond.notify()

    def clear(self):
        with self.cond:
            self.heap.clear()

    def stop(self):
        with self.cond:
            self.stopping = True
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while not self.stopping:
                    if not self.heap:
                        self.cond.wait()
                        continue
                    wait = self.heap[0][0] - time.monotonic()
                    if wait <= 0:
                        break
                    self.cond.wait(wait)
                if self.stopping:
                    return
                _due, _seq, func = heapq.heappop(self.heap)
            try:
                func()
            except OSError:
                pass


class _Stream:
    def __init__(self, stream_id, sock, other, codec_in, outs, transaction):
        self.id = stream_id
        self.sock = sock
        self.other = other
        self.port = sock.getsockname()[1]
        self.codec_in = codec_in
        self.outs = outs
        self.transaction = transaction
        self.stop = threading.Event()
        self.requests = 0
        self.replies = 0
        self.thread = None


class FakeAmbedServer:
    """The fake transcoder. Use as a context manager or call start()/stop().

    bind_ip          address of the control socket and of every data socket
    port             AMBEd control port (0 = ephemeral, see .port afterwards)
    control_port     JSON control port on bind_ip (None = no control socket,
                     0 = ephemeral, see .control_port afterwards)
    max_outputs/caps_mask   the CAPS reply for the life of the server
    data_ports       (low, high): every stream gets the lowest free port of this
                     inclusive range (default 10200-10299; a full range or a
                     port that cannot be bound answers BUSY); None: ephemeral
    data_port_base   N: port N + stream id instead of the range (the reference
                     ambed's 10100 + id form; wins over data_ports)
    lenient_open     True: accept any OPEN whose codec ids are 1..5 and that has
                     an output (for pair-transform tests); False (default): drop
                     without a reply every OPEN the worker would never send
    on_event         callable(text) for session open / close lines
    """

    def __init__(self, bind_ip, port=ext.PORT, control_port=CONTROL_PORT,
                 max_outputs=ext.MAX_OUTPUTS, caps_mask=ext.MASK_ALL,
                 data_port_base=None, on_event=None, max_log=200000,
                 data_ports=DATA_PORTS, lenient_open=False):
        if not ext.caps_reply_valid(ext.CAPS_VERSION, max_outputs, caps_mask):
            raise ValueError("CAPS values would be refused by the worker")
        self.bind_ip = bind_ip
        self.max_outputs = max_outputs
        self.caps_mask = caps_mask
        self.data_port_base = data_port_base
        self.data_ports = None if data_ports is None else (int(data_ports[0]), int(data_ports[1]))
        if self.data_ports is not None and not 1 <= self.data_ports[0] <= self.data_ports[1] <= 65535:
            raise ValueError("data_ports must be (low, high) with 1 <= low <= high <= 65535")
        self.lenient_open_default = bool(lenient_open)
        self.on_event = on_event
        self.max_log = max_log
        self.lock = threading.RLock()
        self.t0 = time.monotonic()
        self.modes = default_modes(self.lenient_open_default)
        self.counters = {}
        self.log = []
        self.seen_tx = set()         # transactions of every OPEN since start / reset
        self.seq = 0
        self.streams = {}            # stream id -> _Stream
        self.keys = {}               # (transaction, codec in, outs) -> stream id
        self.next_id = 1
        self.caps_drop_left = None
        self.stopping = threading.Event()
        self.sched = _Scheduler()
        self.threads = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(RECV_TIMEOUT)
        self.port = self.sock.getsockname()[1]
        self.csock = None
        self.control_port = None
        if control_port is not None:
            self.csock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.csock.bind((bind_ip, control_port))
            self.csock.settimeout(RECV_TIMEOUT)
            self.control_port = self.csock.getsockname()[1]
        self.started = False

    # ---- lifecycle ----------------------------------------------------------

    def start(self):
        if self.started:
            return self
        self.started = True
        self.sched.start()
        self._spawn(self._control_loop, "fake-ambed-ctl")
        if self.csock is not None:
            self._spawn(self._json_loop, "fake-ambed-json")
        return self

    def _spawn(self, target, name, *args):
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self.threads = [t for t in self.threads if t.is_alive()]
        self.threads.append(thread)
        thread.start()
        return thread

    def stop(self):
        self.stopping.set()
        self.sched.stop()
        with self.lock:
            streams = list(self.streams.values())
            self.streams.clear()
            self.keys.clear()
        for stream in streams:
            stream.stop.set()
        for thread in [self.sched] + self.threads + [s.thread for s in streams if s.thread]:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
        self.sock.close()
        if self.csock is not None:
            self.csock.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()

    # ---- log, counters ------------------------------------------------------

    def now(self):
        return time.monotonic() - self.t0

    def _count(self, name, n=1):
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def _note(self, direction, kind, data, port, peer=None, note=None, **extra):
        with self.lock:
            self.seq += 1
            entry = {"seq": self.seq, "t": round(self.now(), 6), "dir": direction,
                     "kind": kind, "port": port, "len": len(data), "data": bytes(data)}
            if peer is not None:
                entry["peer"] = "%s:%d" % peer
            if note:
                entry["note"] = note
            entry.update(extra)
            self.log.append(entry)
            if len(self.log) > self.max_log:
                cut = len(self.log) - self.max_log
                del self.log[:cut]
                self.counters["log_dropped"] = self.counters.get("log_dropped", 0) + cut

    def log_entries(self, since=None, since_seq=None, kinds=None):
        """Copies of the log entries newer than time `since` (seconds since
        the server started) or sequence `since_seq`; kinds filters by name.
        Each entry: seq, t, dir (rx/tx), kind, port (local), peer, len, data
        (bytes), optional note. An rx 'open' entry also has tx_repeat (its
        transaction was seen in an earlier OPEN) and, when dropped, note
        'invalid' with the reason."""
        with self.lock:
            out = [dict(e) for e in self.log
                   if (since is None or e["t"] > since)
                   and (since_seq is None or e["seq"] > since_seq)
                   and (kinds is None or e["kind"] in kinds)]
        return out

    def count_of(self, kind, direction="rx"):
        """Number of logged datagrams of a kind (in the retained log)."""
        with self.lock:
            return sum(1 for e in self.log if e["kind"] == kind and e["dir"] == direction)

    def stats(self):
        with self.lock:
            streams = [{"id": s.id, "port": s.port, "codec_in": s.codec_in,
                        "outs": list(s.outs), "transaction": s.transaction,
                        "requests": s.requests, "replies": s.replies}
                       for s in self.streams.values()]
            return {"now": round(self.now(), 6), "counters": dict(self.counters),
                    "streams_live": len(streams), "streams": streams,
                    "modes": dict(self.modes), "log_size": len(self.log), "last_seq": self.seq,
                    "caps": {"version": ext.CAPS_VERSION, "max_outputs": self.max_outputs,
                             "mask": self.caps_mask},
                    "port": self.port, "control_port": self.control_port}

    # ---- modes --------------------------------------------------------------

    def set_modes(self, **kw):
        """Change modes; unspecified keys stay. ValueError (nothing applied)
        on an unknown key or value. 'open', 'data' and 'caps' take
        'name' or 'name:number' (the number is the mode's parameter)."""
        new = dict(self.modes)
        for name, value in kw.items():
            if name in ("open", "data", "caps", "pong"):
                choices = {"open": OPEN_MODES, "data": DATA_MODES,
                           "caps": CAPS_MODES, "pong": PONG_MODES}[name]
                text = str(value)
                number = None
                if ":" in text:
                    text, num = text.split(":", 1)
                    try:
                        number = int(num)
                    except ValueError:
                        raise ValueError("bad %s parameter %r" % (name, value)) from None
                if text not in choices:
                    raise ValueError("unknown %s mode %r (one of %s)" % (name, text, ", ".join(choices)))
                new[name] = text
                if number is not None:
                    param = {("open", "late"): "open_delay_ms", ("data", "delay"): "data_delay_ms",
                             ("data", "trailing_bytes"): "trailing_n",
                             ("data", "truncated_after_complete_entry"): "keep_entries",
                             ("caps", "delay"): "caps_delay_ms", ("caps", "drop"): "caps_drop_n"
                             }.get((name, text))
                    if param is None:
                        raise ValueError("mode %s:%s takes no number" % (name, text))
                    new[param] = number
            elif name in ("open_delay_ms", "data_delay_ms", "caps_delay_ms", "trailing_n", "keep_entries"):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("%s must be an integer >= 0" % name)
                new[name] = value
            elif name == "caps_drop_n":
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                    raise ValueError("caps_drop_n must be an integer >= 1 or null (all)")
                new[name] = value
            elif name == "max_sessions":
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise ValueError("max_sessions must be an integer >= 0 or null")
                new[name] = value
            elif name == "lenient_open":
                if not isinstance(value, bool):
                    raise ValueError("lenient_open must be true or false")
                new[name] = value
            elif name == "len_check":
                if value not in LEN_CHECKS:
                    raise ValueError("len_check is one of %s" % ", ".join(LEN_CHECKS))
                new[name] = value
            else:
                raise ValueError("unknown setting %r" % name)
        with self.lock:
            self.modes = new
            if "caps" in kw or "caps_drop_n" in kw:
                self.caps_drop_left = new["caps_drop_n"]

    def reset(self):
        """Default modes, empty counters and log, every stream closed, no
        delayed send pending. The clock keeps running."""
        self.sched.clear()
        with self.lock:
            streams = list(self.streams.values())
            self.streams.clear()
            self.keys.clear()
            self.modes = default_modes(self.lenient_open_default)
            self.caps_drop_left = None
            self.counters = {}
            self.log = []
            self.seen_tx = set()
        for stream in streams:
            stream.stop.set()

    # ---- control port -------------------------------------------------------

    def _send(self, sock, data, peer, kind, note=None):
        try:
            sock.sendto(data, peer)
        except OSError:
            return
        self._note("tx", kind, data, sock.getsockname()[1], peer, note)
        self._count("tx_" + kind)

    def _control_loop(self):
        while not self.stopping.is_set():
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self._on_control(data, peer)

    def _on_control(self, data, peer):
        kind = ext.kind_of(data)
        if kind not in ("ping", "caps_request", "open", "cs"):
            # a transcoder receives only these four; no reply, no error datagram
            self._note("rx", "unknown", data, self.port, peer)
            self._count("rx_unknown")
            return
        if kind == "open":
            self._on_open(data, peer)
            return
        self._note("rx", kind, data, self.port, peer)
        self._count("rx_" + kind)
        if kind == "ping":
            if self.modes["pong"] == "silent":
                self._count("pong_silenced")
            else:
                self._send(self.sock, ext.pong(), peer, "pong")
        elif kind == "caps_request":
            self._on_caps(peer)
        else:
            self._on_cs(ext.parse_cs(data))

    def _on_caps(self, peer):
        with self.lock:
            mode = self.modes["caps"]
            delay = self.modes["caps_delay_ms"] / 1000.0
            if mode == "drop":
                if self.caps_drop_left is not None:
                    self.caps_drop_left -= 1
                    if self.caps_drop_left <= 0:
                        self.modes["caps"] = "normal"
                        self.caps_drop_left = None
                self.counters["caps_dropped"] = self.counters.get("caps_dropped", 0) + 1
                return
            max_outputs = self.max_outputs
            if mode == "changed":
                max_outputs = 3 if self.max_outputs == 4 else 4
        reply = ext.caps_reply(max_outputs, self.caps_mask)
        if mode == "delay":
            self.sched.after(delay, lambda: self._send(self.sock, reply, peer, "caps_reply", "delayed"))
        else:
            self._send(self.sock, reply, peer, "caps_reply")

    def _on_cs(self, stream_id):
        text = None
        with self.lock:
            stream = self.streams.pop(stream_id, None)
            if stream is not None:
                self.keys.pop((stream.transaction, stream.codec_in, stream.outs), None)
                stream.stop.set()
                self.counters["cs_known"] = self.counters.get("cs_known", 0) + 1
                text = "session close id=%d port=%d" % (stream.id, stream.port)
            else:
                self.counters["cs_unknown"] = self.counters.get("cs_unknown", 0) + 1
        if text:
            self._event(text)

    def _event(self, text):
        if self.on_event is not None:
            self.on_event(text)

    # ---- OPEN ---------------------------------------------------------------

    def _bind_data(self, want):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.bind_ip, want))
        except OSError:
            sock.close()
            return None
        sock.settimeout(RECV_TIMEOUT)
        return sock

    def _new_stream(self, stream_id, sock, codec_in, outs, transaction):
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        other.bind((self.bind_ip, 0))
        stream = _Stream(stream_id, sock, other, codec_in, outs, transaction)
        self.streams[stream_id] = stream
        self.keys[(transaction, codec_in, outs)] = stream_id
        stream.thread = self._spawn(self._data_loop, "fake-ambed-data-%d" % stream_id, stream)
        return stream

    def _next_free_id(self):
        for _attempt in range(65535):
            stream_id = self.next_id
            self.next_id = self.next_id % 65535 + 1
            if stream_id not in self.streams:
                return stream_id
        return None

    def _alloc_stream(self, codec_in, outs, transaction):
        """New stream with its data socket, or None (the caller answers BUSY).
        Caller holds the lock."""
        base = self.data_port_base
        if base is None and self.data_ports is not None:
            stream_id = self._next_free_id()
            if stream_id is None:
                return None
            used = {s.port for s in self.streams.values()}
            for want in range(self.data_ports[0], self.data_ports[1] + 1):
                if want in used:
                    continue
                sock = self._bind_data(want)
                if sock is not None:
                    return self._new_stream(stream_id, sock, codec_in, outs, transaction)
            return None
        for _attempt in range(64):
            stream_id = self._next_free_id()
            if stream_id is None:
                return None
            want = base + stream_id if base is not None and base + stream_id <= 65535 else 0
            sock = self._bind_data(want)
            if sock is not None:
                return self._new_stream(stream_id, sock, codec_in, outs, transaction)
        return None

    def _open_problem(self, data):
        """Why a worker would never send this OPEN, or None. lenient_open keeps
        only the checks the transform needs (codec ids 1..5, one output)."""
        slots = tuple(data[16:20])
        codec_in = data[15]
        transaction = struct.unpack(">I", data[20:24])[0]
        outs = tuple(b for b in slots if b != 0)
        if codec_in not in ext.WIDTH or any(c not in ext.WIDTH for c in outs):
            return "codec_range"
        if slots[0] == 0:
            return "no_outputs"
        if self.modes["lenient_open"]:
            return None
        if slots != outs + (0,) * (4 - len(outs)):
            return "gap"
        if any(a >= b for a, b in zip(outs, outs[1:])):
            return "order"
        if codec_in in outs:
            return "input_in_outputs"
        if len(outs) > self.max_outputs:
            return "too_many_outputs"
        if any(not self.caps_mask & (1 << c) for c in (codec_in,) + outs):
            return "not_in_mask"
        if transaction == 0:
            return "transaction_zero"
        return None

    def _on_open(self, data, peer):
        _callsign, codec_in, outs, transaction = ext.parse_open(data)
        with self.lock:
            repeat = transaction in self.seen_tx
            self.seen_tx.add(transaction)
            problem = self._open_problem(data)
        if problem is not None:
            # no error datagram exists: drop, count, log
            self._note("rx", "open", data, self.port, peer, "invalid", tx_repeat=repeat, reason=problem)
            self._count("rx_open")
            self._count("open_invalid")
            self._count("open_invalid_" + problem)
            return
        self._note("rx", "open", data, self.port, peer, "tx_repeat" if repeat else None, tx_repeat=repeat)
        self._count("rx_open")
        key = (transaction, codec_in, outs)
        text = None
        send = []                    # STD datagrams to send, in order
        with self.lock:
            mode = self.modes["open"]
            delay = self.modes["open_delay_ms"] / 1000.0
            limit = self.modes["max_sessions"]
            stream = self.streams.get(self.keys.get(key))
            if mode == "busy":
                self.counters["busy_mode"] = self.counters.get("busy_mode", 0) + 1
                answer = ext.busy(transaction)
                stream = None
            elif mode == "silent":
                self.counters["open_silent"] = self.counters.get("open_silent", 0) + 1
                return
            else:
                answer = None
                if stream is None and limit is not None and len(self.streams) >= limit:
                    self.counters["busy_capacity"] = self.counters.get("busy_capacity", 0) + 1
                    answer = ext.busy(transaction)
                elif stream is None:
                    stream = self._alloc_stream(codec_in, outs, transaction)
                    if stream is None:
                        self.counters["alloc_failed"] = self.counters.get("alloc_failed", 0) + 1
                        answer = ext.busy(transaction)
                    else:
                        self.counters["open_new"] = self.counters.get("open_new", 0) + 1
                        text = "session open id=%d port=%d in=%d outs=%s tx=%08x" % (
                            stream.id, stream.port, codec_in, ",".join(map(str, outs)), transaction)
                else:
                    self.counters["open_repeat"] = self.counters.get("open_repeat", 0) + 1
            if answer is None:
                one = ext.std(stream.id, stream.port, codec_in, outs[0], transaction)
                if mode == "std_twice":
                    send = [one, one]
                elif mode == "std_then_other":
                    other_id = OTHER_STREAM
                    while other_id in self.streams:
                        other_id += 1
                    send = [one, ext.std(other_id, stream.port, codec_in, outs[0], transaction)]
                else:
                    send = [one]
        if text:
            self._event(text)
        if answer is not None:
            self._send(self.sock, answer, peer, "busy")
        elif mode == "late":
            self.sched.after(delay, lambda: self._send(self.sock, send[0], peer, "std", "late"))
        else:
            for datagram in send:
                self._send(self.sock, datagram, peer, "std")

    # ---- data ---------------------------------------------------------------

    def _data_loop(self, stream):
        try:
            while not stream.stop.is_set() and not self.stopping.is_set():
                try:
                    data, peer = stream.sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                self._on_data(stream, data, peer)
        finally:
            stream.sock.close()
            stream.other.close()

    def _on_data(self, stream, data, peer):
        with self.lock:
            modes = dict(self.modes)
            stream.requests += 1
        self._count("rx_data")
        lenient = modes["len_check"] == "lenient"
        ok = len(data) >= 3 and data[0] == stream.codec_in
        if ok and not lenient:
            ok = len(data) == 2 + ext.WIDTH[stream.codec_in]
        if not ok:
            self._note("rx", "data_request", data, stream.port, peer, "dropped")
            self._count("data_dropped")
            return
        self._note("rx", "data_request", data, stream.port, peer)
        pid = data[1]
        frame = bytes(data[2:])
        reply = ext.data_reply(pid, [(c, ext.transform(stream.codec_in, c, frame))
                                     for c in stream.outs])
        mode = modes["data"]
        sock = stream.sock
        note = None if mode == "echo" else mode
        if mode == "silent":
            self._count("data_silenced")
            return
        if mode == "wrong_source":
            sock = stream.other
        elif mode == "wrong_codec":
            wrong = stream.outs[0] % 5 + 1
            reply = bytes([wrong]) + reply[1:]
        elif mode == "trailing_bytes":
            reply = reply + bytes([0xA5]) * max(1, modes["trailing_n"])
        elif mode == "truncated_after_complete_entry":
            keep = max(1, modes["keep_entries"])
            reply = reply[:ext.reply_size(stream.outs[:keep])]
        if mode == "delay":
            self.sched.after(modes["data_delay_ms"] / 1000.0,
                             lambda: self._send_data(stream, sock, reply, peer, "delay"))
        else:
            self._send_data(stream, sock, reply, peer, note)

    def _send_data(self, stream, sock, reply, peer, note):
        try:
            sock.sendto(reply, peer)
        except OSError:
            return
        with self.lock:
            stream.replies += 1
        self._note("tx", "data_reply", reply, sock.getsockname()[1], peer, note)
        self._count("tx_data_reply")

    # ---- JSON control socket ------------------------------------------------

    def _json_loop(self):
        while not self.stopping.is_set():
            try:
                data, peer = self.csock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            reply = json.dumps(self.handle_json(data), separators=(",", ":")).encode()
            try:
                self.csock.sendto(reply, peer)
            except OSError:
                pass

    def handle_json(self, data):
        """One control request (bytes) to one reply object."""
        self._count("ctl_ops")
        try:
            req = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "error": "bad request: not JSON"}
        if not isinstance(req, dict) or not isinstance(req.get("op"), str):
            return {"ok": False, "error": "bad request: an object with a string op is needed"}
        op = req["op"]
        if op == "set":
            settings = {k: v for k, v in req.items() if k != "op"}
            try:
                self.set_modes(**settings)
            except ValueError as err:
                return {"ok": False, "error": str(err)}
            return {"ok": True, "modes": dict(self.modes)}
        if op == "reset":
            self.reset()
            return {"ok": True, "now": round(self.now(), 6), "modes": dict(self.modes)}
        if op == "stats":
            reply = self.stats()
            reply["ok"] = True
            return reply
        if op == "log":
            return self._json_log(req)
        return {"ok": False, "error": "unknown op: %s" % op[:40]}

    def _json_log(self, req):
        since = req.get("since")
        since_seq = req.get("since_seq")
        kinds = req.get("kinds")
        limit = req.get("limit")
        for name, value in (("since", since), ("since_seq", since_seq), ("limit", limit)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return {"ok": False, "error": "%s must be a number" % name}
        if kinds is not None and not (isinstance(kinds, list) and all(isinstance(k, str) for k in kinds)):
            return {"ok": False, "error": "kinds must be a list of names"}
        entries = self.log_entries(since, since_seq, kinds)
        out = []
        size = 200
        more = False
        for entry in entries:
            item = dict(entry)
            item["hex"] = item.pop("data").hex()
            size += len(json.dumps(item)) + 1
            if size > JSON_LIMIT or (limit is not None and len(out) >= limit):
                more = True
                break
            out.append(item)
        return {"ok": True, "now": round(self.now(), 6), "entries": out, "more": more,
                "last_seq": out[-1]["seq"] if out else (since_seq or 0)}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m dvtest.fake_ambed",
        description="Fake extended AMBEd transcoder (deterministic test transform).")
    parser.add_argument("--bind", default=os.environ.get("TRANSCODER_IP"),
                        help="local IP (default: env TRANSCODER_IP)")
    parser.add_argument("--port", type=int, default=ext.PORT, help="AMBEd control port")
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT,
                        help="JSON control port on the same IP (0 = ephemeral, -1 = off)")
    parser.add_argument("--max-sessions", type=int, default=None,
                        help="answer BUSY above this many live streams")
    parser.add_argument("--max-outputs", type=int, default=ext.MAX_OUTPUTS)
    parser.add_argument("--caps-mask", type=lambda t: int(t, 0), default=ext.MASK_ALL)
    parser.add_argument("--data-ports", default="%d-%d" % DATA_PORTS,
                        help="LOW-HIGH range of the stream data ports, lowest free first "
                             "(default %(default)s; 0 = ephemeral)")
    parser.add_argument("--data-port-base", type=int, default=None,
                        help="data port = base + stream id instead of the range "
                             "(the reference ambed uses 10100)")
    parser.add_argument("--lenient-open", action="store_true",
                        help="accept OPENs a worker never sends (pair-transform tests only)")
    args = parser.parse_args(argv)
    if not args.bind:
        parser.error("--bind or env TRANSCODER_IP is required")
    if args.data_ports == "0":
        ports = None
    else:
        try:
            low, high = (int(x) for x in args.data_ports.split("-", 1))
        except ValueError:
            parser.error("--data-ports is LOW-HIGH or 0")
        ports = (low, high)
    try:
        server = FakeAmbedServer(
            args.bind, args.port, None if args.control_port < 0 else args.control_port,
            args.max_outputs, args.caps_mask, args.data_port_base or None,
            on_event=lambda text: print(text, flush=True),
            data_ports=ports, lenient_open=args.lenient_open)
    except ValueError as err:
        parser.error(str(err))
    if args.max_sessions is not None:
        server.set_modes(max_sessions=args.max_sessions)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: done.set())
    server.start()
    print("fake ambed listening on %s:%d control %s" % (
        args.bind, server.port, server.control_port), flush=True)
    done.wait()
    server.stop()
    print("fake ambed stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
