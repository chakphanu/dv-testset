# SPDX-License-Identifier: MIT
"""XLX interlink between reflectors (UDP 10002, docs/protocols/xlx.md).

Link layer (layout as xlxd, URFd's BM link and BrandMeister use it; no
published spec):
  L connect / A ack  39 B: tag, callsign(8), version major/minor/revision,
                     module letters, NUL terminated, zero padded
  N nack / U disconnect  10 B: tag, callsign(8), 0
  keepalive           9 B: callsign(8), 0
Streams are DExtra DSVT packets: the 56 B header of dvtest.proto.dextra
and voice frames whose size is the protocol revision of the receiver:
  revision 0/1  27 B  [15:24] AMBE+ (D-Star), [24:27] slow data
  revision 2    45 B  + [27] DMR packet id (voice burst A-F = 0-5),
                        [28] DMR sub id (1-3), [29:38] AMBE+2, [38:45] DMR sync
  revision 3    53 B  + [45:53] Codec2 3200
The revision follows the version the peer announced (>= 2.6.0 -> 3,
>= 2.2.0 -> 2, >= 1.4.0 -> 1, else 0).

XlxPeer plays one reflector on UDP 10002 towards several reflectors at
once: it answers their L with A, keeps the links alive and records every
packet per source address.
"""
import socket
import threading
import time

from . import dextra
from .dextra import HEADER_SIZE, SLOW_DATA

PORT = 10002
LINK_SIZE = 39
SHORT_SIZE = 10
KEEPALIVE_SIZE = 9
FRAME_SIZE = {0: 27, 1: 27, 2: 45, 3: 53}
REVISION_OF_SIZE = {27: 1, 45: 2, 53: 3}
# last-frame D-Star slot and slow data that xlxd writes (DExtra end pattern)
END_AMBE = bytes.fromhex("55c87a000000000000")
END_SLOW = bytes.fromhex("251ac6")


def _cs8(callsign):
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return (callsign + b" " * 8)[:8]


def revision_of(version):
    """Protocol revision a reflector uses towards a peer of this version."""
    version = tuple(version)
    if version >= (2, 6, 0):
        return 3
    if version >= (2, 2, 0):
        return 2
    if version >= (1, 4, 0):
        return 1
    return 0


def _link(tag, callsign, modules, version):
    body = tag + _cs8(callsign) + bytes(version) + modules.encode()
    if len(body) >= LINK_SIZE:
        raise ValueError("too many modules")
    return body + bytes(LINK_SIZE - len(body))


def connect(callsign, modules, version=(2, 6, 1)):
    return _link(b"L", callsign, modules, version)


def ack(callsign, modules, version=(2, 6, 1)):
    return _link(b"A", callsign, modules, version)


def nack(callsign):
    return b"N" + _cs8(callsign) + b"\x00"


def disconnect(callsign):
    return b"U" + _cs8(callsign) + b"\x00"


def keepalive(callsign):
    return _cs8(callsign) + b"\x00"


def parse_link(data):
    """{tag, callsign, version, modules} for a 39 B L/A, else None."""
    if len(data) != LINK_SIZE or data[:1] not in (b"L", b"A") or data[38] != 0:
        return None
    modules = data[12:38].split(b"\x00", 1)[0]
    return {"tag": data[:1].decode(), "callsign": data[1:9].decode("latin-1").rstrip(),
            "version": tuple(data[9:12]), "modules": modules.decode("latin-1")}


def parse_short(data):
    """{tag, callsign} for a 10 B N/U, else None."""
    if len(data) != SHORT_SIZE or data[:1] not in (b"N", b"U") or data[9] != 0:
        return None
    return {"tag": data[:1].decode(), "callsign": data[1:9].decode("latin-1").rstrip()}


def parse_keepalive(data):
    """Callsign of a 9 B keepalive, else None."""
    if len(data) != KEEPALIVE_SIZE or data[8] != 0 or data.startswith(b"DSVT"):
        return None
    return data[:8].decode("latin-1").rstrip()


def header(stream_id, callsign, module, reflector="XLX996"):
    """56 B DSVT header; RPT2 = sending reflector + module."""
    return dextra.header(stream_id, callsign, module, reflector)


def voice(stream_id, seq, ambe, last=False, slow=SLOW_DATA, revision=3,
          ambe2=bytes(9), dmr_pid=None, dmr_subid=None, sync=bytes(7), codec2=bytes(8)):
    """Voice frame of a revision; seq is the stream's frame index (written
    mod 21, 0x40 on the last frame), the DMR ids default to burst seq // 3
    mod 6 and sub id seq % 3 + 1."""
    out = bytearray(b"DSVT\x20\x00\x00\x00\x20\x00\x01\x02")
    out += stream_id.to_bytes(2, "little")
    out.append((seq % 21) | (0x40 if last else 0))
    out += ambe + slow
    if revision >= 2:
        out.append((seq // 3) % 6 if dmr_pid is None else dmr_pid)
        out.append(seq % 3 + 1 if dmr_subid is None else dmr_subid)
        out += ambe2 + sync
    if revision >= 3:
        out += codec2
    if len(out) != FRAME_SIZE[revision]:
        raise ValueError("slot sizes: AMBE 9, slow 3, AMBE+2 9, sync 7, Codec2 8")
    return bytes(out)


def parse_voice(data):
    """Fields of a 27/45/53 B voice frame, else None."""
    if len(data) not in REVISION_OF_SIZE or not data.startswith(b"DSVT") or data[4] != 0x20 or data[8] != 0x20:
        return None
    out = {"stream_id": int.from_bytes(data[12:14], "little"), "seq": data[14] & 0x1F,
           "last": bool(data[14] & 0x40), "ambe": data[15:24], "slow": data[24:27],
           "revision": REVISION_OF_SIZE[len(data)], "size": len(data)}
    if len(data) >= 45:
        out.update(dmr_pid=data[27], dmr_subid=data[28], ambe2=data[29:38], sync=data[38:45])
    if len(data) == 53:
        out["codec2"] = data[45:53]
    return out


def parse_header(data):
    """dextra.parse_header plus the raw flags byte."""
    parsed = dextra.parse_header(data)
    if parsed:
        parsed["flags"] = data[15:18]
    return parsed


def stream_of(packets):
    """{stream id: {"headers": n, "frames": [parsed voice]}} of DSVT packets."""
    out = {}
    for data in packets:
        if len(data) == HEADER_SIZE:
            h = dextra.parse_header(data)
            if h:
                out.setdefault(h["stream_id"], {"headers": 0, "frames": [], "header": h})["headers"] += 1
            continue
        v = parse_voice(data)
        if v:
            out.setdefault(v["stream_id"], {"headers": 0, "frames": [], "header": None})["frames"].append(v)
    return out


class XlxPeer:
    """One reflector identity on UDP 10002 towards several reflectors.

    link(ip) sends L and answers that reflector's own L with A until
    unlink(ip) or quiet(ip); keepalives go to every linked address once a
    second. Everything received is kept per source address as
    (monotonic time, bytes)."""

    def __init__(self, callsign="XLX996", modules="DE", version=(2, 6, 1), bind_ip="0.0.0.0", port=PORT,
                 peer_port=PORT):
        self.peer_port = peer_port
        self.callsign = callsign
        self.modules = modules
        self.version = tuple(version)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(0.05)
        self.lock = threading.Lock()
        self.inbox = {}             # ip -> [(t, bytes)]
        self.answer = set()         # ips whose L gets an A
        self.alive = set()          # ips that get keepalives
        self.answered = {}          # ip -> time of our last A
        self.stopping = threading.Event()
        self.threads = [threading.Thread(target=self._rx, daemon=True),
                        threading.Thread(target=self._keepalive, daemon=True)]
        for thread in self.threads:
            thread.start()

    # ---------------------------------------------------------------- io

    def send(self, ip, data):
        self.sock.sendto(data, (ip, self.peer_port))

    def _rx(self):
        while not self.stopping.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            now = time.monotonic()
            with self.lock:
                self.inbox.setdefault(addr[0], []).append((now, data))
                answer = addr[0] in self.answer
            link = parse_link(data)
            if answer and link and link["tag"] == "L":
                self.send(addr[0], ack(self.callsign, self.modules, self.version))
                with self.lock:
                    self.answered[addr[0]] = now

    def _keepalive(self):
        while not self.stopping.wait(1.0):
            with self.lock:
                targets = list(self.alive)
            for ip in targets:
                try:
                    self.send(ip, keepalive(self.callsign))
                except OSError:
                    pass

    def packets(self, ip, since=0.0, pred=None):
        with self.lock:
            got = [(t, d) for t, d in self.inbox.get(ip, []) if t >= since]
        return [(t, d) for t, d in got if pred is None or pred(d)]

    def wait(self, ip, pred, seconds, since=0.0):
        """First (t, bytes) from ip matching pred after since, or None."""
        deadline = time.monotonic() + seconds
        while True:
            got = self.packets(ip, since, pred)
            if got:
                return got[0]
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    # ---------------------------------------------------------------- link

    def link(self, ip, seconds=8.0, version=None, modules=None, callsign=None):
        """Full link (our L acked, their L answered). Returns the parsed A,
        or None. version/modules/callsign change this identity."""
        if version is not None:
            self.version = tuple(version)
        if modules is not None:
            self.modules = modules
        if callsign is not None:
            self.callsign = callsign
        start = time.monotonic()
        with self.lock:
            self.answer.add(ip)
            self.answered.pop(ip, None)
        self.send(ip, connect(self.callsign, self.modules, self.version))
        got = self.wait(ip, lambda d: (parse_link(d) or {}).get("tag") == "A", 2.0, start)
        if got is None:
            return None
        deadline = start + seconds
        while time.monotonic() < deadline:
            with self.lock:
                if self.answered.get(ip, 0) >= start:
                    self.alive.add(ip)
                    return parse_link(got[1])
            time.sleep(0.05)
        return None

    def quiet(self, ip):
        """Stop keepalives and answers to ip; the link is not closed."""
        with self.lock:
            self.alive.discard(ip)
            self.answer.discard(ip)

    def unlink(self, ip, callsign=None):
        self.quiet(ip)
        self.send(ip, disconnect(callsign or self.callsign))

    def ask(self, ip, packet, pred, seconds=1.5):
        start = time.monotonic()
        self.send(ip, packet)
        got = self.wait(ip, pred, seconds, start)
        return got[1] if got else None

    # ---------------------------------------------------------------- streams

    def talk(self, ip, module, frames, stream_id, revision=3, ambe2=None, codec2=None,
             header_repeat=1, last=True, with_header=True, callsign="N0CALL", gap=0.020):
        """One stream of D-Star frames (plus AMBE+2 / Codec2 slot lists)."""
        if with_header:
            for _ in range(header_repeat):
                self.send(ip, header(stream_id, callsign, module, self.callsign))
        start = time.monotonic()
        count = len(frames)
        for index, frame in enumerate(frames):
            delay = start + index * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.send(ip, voice(stream_id, index, frame, revision=revision,
                                ambe2=ambe2[index] if ambe2 else bytes(9),
                                codec2=codec2[index] if codec2 else bytes(8),
                                slow=dextra.SYNC_DATA if index % 21 == 0 else SLOW_DATA))
        if last:
            self.send(ip, voice(stream_id, count, END_AMBE, last=True, slow=END_SLOW, revision=revision))

    def streams(self, ip, since=0.0):
        return stream_of([d for _t, d in self.packets(ip, since) if d.startswith(b"DSVT")])

    def close(self):
        if self.stopping.is_set():
            return
        with self.lock:
            targets = list(self.alive)
        for ip in targets:
            try:
                self.unlink(ip)
            except OSError:
                pass
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.sock.close()
