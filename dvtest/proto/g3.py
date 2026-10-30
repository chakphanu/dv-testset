# SPDX-License-Identifier: MIT
"""Icom G3 terminal / access point mode as reflectors speak it
(docs/protocols/g3.md): presence on UDP 12346, route query on UDP 12345,
D-STAR voice in the G2 DSVT framing on UDP 40000 (dextra builders).

| Packet | Size | Layout |
|--------|------|--------|
| presence | 32 B | 8 B header (byte 2 flags, byte 3 status) + callsign (8) + owner (8) + terminal callsign (8) |
| presence reply | 36 B | the 32 bytes, byte 2 0x80, byte 3 status (0 = ok), + gateway IPv4 (4) |
| route query | 16 B | 8 B header (byte 2 bit 0x10 = repeater call) + destination callsign (8) |
| route reply | 36 B | header (byte 2 + 0x80, byte 3 status: 0 ok, 1 reject) + UR (8) + RPT1 (8) + RPT2 (8) + IPv4 (4) |
| ping | 4 B | `PING`, reflector -> terminal IP:40000 |
| HPCH | 12 B | `HPCH` + area repeater callsign (8), JARL 5.2 c pinhole keeper |

A terminal is one IP: the reflector sends to IP:40000 only. The tester
plays one terminal itself (Terminal) and a second one through the relay
service `g3-term2` of the rig profile `g3` (RelayTerminal; run the relay
with `python -m dvtest.proto.g3 relay`).
"""
import json
import socket
import sys
import time

from . import dextra
from ..net import UdpClient

PRESENCE_PORT = 12346
ROUTE_PORT = 12345
DV_PORT = 40000
RELAY_PORT = 40100
PRESENCE_SIZE = 32
ROUTE_SIZE = 16
REPLY_SIZE = 36
PING = b"PING"
HEADER = b"\x00\x00\x00\x00\x00\x00\x00\x00"
REPEATER_CALL = 0x10


def _cs8(callsign):
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return (callsign + b" " * 8)[:8]


def presence_packet(terminal, callsign=None, owner=None, header=HEADER):
    """32 B presence: header + callsign + owner + terminal callsign (each 8)."""
    callsign = callsign or terminal[:7]
    owner = owner or callsign
    return bytes(header)[:8].ljust(8, b"\x00") + _cs8(callsign) + _cs8(owner) + _cs8(terminal)


def parse_presence_reply(data):
    """{"flags", "status", "body" (bytes 8-31), "address"} of a 36 B reply."""
    if len(data) != REPLY_SIZE:
        return None
    return {"flags": data[2], "status": data[3], "header": data[:8], "body": data[8:32],
            "address": socket.inet_ntoa(data[32:36])}


def route_packet(destination, repeater=True, header=HEADER):
    """16 B route query for a destination callsign (8: 7 + module)."""
    head = bytearray(bytes(header)[:8].ljust(8, b"\x00"))
    head[2] = (head[2] | REPEATER_CALL) if repeater else (head[2] & ~REPEATER_CALL & 0xFF)
    return bytes(head) + _cs8(destination)


def parse_route_reply(data):
    """{"flags", "status", "ur", "rpt1", "rpt2", "address"} of a 36 B reply."""
    if len(data) != REPLY_SIZE:
        return None
    return {"flags": data[2], "status": data[3], "header": data[:8], "ur": data[8:16],
            "rpt1": data[16:24], "rpt2": data[24:32], "address": socket.inet_ntoa(data[32:36])}


def hpch_packet(repeater):
    """JARL 5.2 c hole-punch keeper: `HPCH` + area repeater callsign (8)."""
    return b"HPCH" + _cs8(repeater)


def stream(stream_id, frames, my, rpt2, rpt1=None, ur="CQCQCQ", end=True, **fields):
    """Header + voice (sync at counter 0) + last frame, as DSVT packets."""
    out = [dextra.header_packet(stream_id, my=my, rpt1=rpt1 if rpt1 is not None else _cs8(my)[:7] + b"G",
                                rpt2=rpt2, ur=ur, **fields)]
    for index, frame in enumerate(frames):
        seq = index % 21
        out.append(dextra.voice_packet(stream_id, seq, frame, dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA))
    if end:
        out.append(dextra.last_frame(stream_id, len(frames)))
    return out


def kind_of(data):
    """"header" | "voice" | "last" | "ping" | "other" for a packet on 40000."""
    if data == PING:
        return "ping"
    if dextra.parse_header_full(data):
        return "header"
    voice = dextra.parse_voice_raw(data)
    if voice:
        return "last" if voice["last"] else "voice"
    return "other"


# ---------------------------------------------------------------- terminals

class Terminal:
    """The tester as a G3 terminal: UDP 40000 bound on its own address
    (the reflector sends voice and PING there only; one socket serves
    xlxd-ref and xlxd-sut, told apart by source IP), queries from an
    ephemeral port."""

    def __init__(self, terminal="N0TEST T", bind_ip="0.0.0.0", dv_port=DV_PORT):
        self.terminal = terminal
        self.bind = (bind_ip, dv_port)
        self.sock = None
        self.inbox = []                 # (time, packet, source ip)
        self.query = UdpClient(bind_ip)
        self.open()

    def open(self):
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self.sock.bind(self.bind)
            self.sock.setblocking(False)

    def shut(self):
        """Close UDP 40000: the reflector's next packet draws ICMP port
        unreachable."""
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def drain(self):
        while self.sock is not None:
            try:
                data, src = self.sock.recvfrom(2048)
            except (BlockingIOError, OSError):
                break
            self.inbox.append((time.monotonic(), data, src[0]))

    def ask(self, dest, packet, timeout=2.0):
        """Send a presence or route query; the first reply or None."""
        self.query.drain()
        self.query.inbox.clear()
        self.query.inbox_times.clear()
        self.query.send(dest, packet)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.query.drain()
            if self.query.inbox:
                return self.query.inbox[0]
            time.sleep(0.01)
        return None

    def presence(self, ip, **kw):
        return self.ask((ip, PRESENCE_PORT), presence_packet(self.terminal, **kw))

    def route(self, ip, destination, repeater=True):
        return self.ask((ip, ROUTE_PORT), route_packet(destination, repeater))

    def send_dv(self, ip, packet, port=DV_PORT):
        self.sock.sendto(packet, (ip, port))

    def send_dv_other_port(self, ip, packet):
        """DV from the query socket (not 40000), as early firmware did."""
        self.query.send((ip, DV_PORT), packet)

    def paced(self, ip, packets, gap=0.020, *others, other_port=False):
        """Send at gap spacing from 40000 (or the query socket), draining
        this terminal and the other clients' sockets meanwhile."""
        start = time.monotonic()
        for index, packet in enumerate(packets):
            delay = start + index * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if other_port:
                self.send_dv_other_port(ip, packet)
            else:
                self.send_dv(ip, packet)
            self.drain()
            for other in others:
                other.drain()

    def received(self, ip=None):
        """[(time, packet)] that arrived on 40000 (from ip if given)."""
        self.drain()
        return [(t, p) for t, p, src in self.inbox if ip is None or src == ip]

    def clear(self):
        self.drain()
        self.inbox.clear()

    def close(self):
        self.shut()
        self.query.close()


class RelayTerminal:
    """A second terminal at the address of the rig's `g3-term2` relay: the
    tester sends it JSON commands, it sends them on from its own address
    and forwards what it receives (DV on 40000, replies to its queries)."""

    def __init__(self, relay_ip, terminal="N0CALL T"):
        self.relay = (relay_ip, RELAY_PORT)
        self.terminal = terminal
        self.client = UdpClient("0.0.0.0")
        self.dv_inbox = []

    def _command(self, **msg):
        self.client.send(self.relay, json.dumps(msg).encode())

    def _drain(self):
        self.client.drain()
        replies = []
        for data, stamp in zip(self.client.inbox, self.client.inbox_times):
            msg = json.loads(data)
            if msg.get("on") == "dv":
                self.dv_inbox.append((stamp, bytes.fromhex(msg["hex"]), msg["src"][0]))
            else:
                replies.append(bytes.fromhex(msg["hex"]))
        self.client.inbox.clear()
        self.client.inbox_times.clear()
        return replies

    def alive(self, timeout=2.0):
        self._command(op="ping")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.drain()
            if any(json.loads(d).get("op") == "pong" for d in self.client.inbox):
                self.client.inbox.clear()
                return True
            time.sleep(0.02)
        return False

    def ask(self, dest, packet, timeout=2.0):
        self._drain()
        self._command(op="query", dst=list(dest), hex=packet.hex())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            replies = self._drain()
            if replies:
                return replies[0]
            time.sleep(0.01)
        return None

    def presence(self, ip, **kw):
        return self.ask((ip, PRESENCE_PORT), presence_packet(self.terminal, **kw))

    def route(self, ip, destination, repeater=True):
        return self.ask((ip, ROUTE_PORT), route_packet(destination, repeater))

    def send_dv(self, ip, packet, port=DV_PORT):
        self._command(op="dv", dst=[ip, port], hex=packet.hex())

    def drain(self):
        self._drain()

    def received(self, ip=None):
        self._drain()
        return [(t, p) for t, p, src in self.dv_inbox if ip is None or src == ip]

    def clear(self):
        self._drain()
        self.dv_inbox.clear()

    def close(self):
        self.client.close()


def relay_main(port=RELAY_PORT):
    """The `g3-term2` service: one terminal at this host's address. JSON
    over UDP from the controller (the last sender of a command):
    {"op": "dv", "dst": [ip, port], "hex"} sends from UDP 40000;
    {"op": "query", "dst", "hex"} sends from the query socket;
    {"op": "ping"} answers {"op": "pong"}. Everything received on 40000
    or the query socket goes back as {"on": "dv" | "query", "src", "hex"}."""
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control.bind(("0.0.0.0", port))
    dv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dv.bind(("0.0.0.0", DV_PORT))
    query = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    query.bind(("0.0.0.0", 0))
    for sock in (control, dv, query):
        sock.setblocking(False)
    controller = None
    print("g3 relay: control %d, dv %d" % (port, DV_PORT), flush=True)
    import select
    while True:
        ready, _, _ = select.select([control, dv, query], [], [], 1.0)
        for sock in ready:
            try:
                data, src = sock.recvfrom(4096)
            except OSError:
                continue
            if sock is control:
                controller = src
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if msg.get("op") == "ping":
                    control.sendto(b'{"op": "pong"}', src)
                elif msg.get("op") in ("dv", "query"):
                    out = dv if msg["op"] == "dv" else query
                    out.sendto(bytes.fromhex(msg["hex"]), tuple(msg["dst"]))
            elif controller:
                on = "dv" if sock is dv else "query"
                control.sendto(json.dumps({"on": on, "src": list(src), "hex": data.hex()}).encode(), controller)


if __name__ == "__main__":
    if sys.argv[1:2] == ["relay"]:
        relay_main()
    else:
        sys.exit("usage: python -m dvtest.proto.g3 relay")
