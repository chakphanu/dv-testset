# SPDX-License-Identifier: MIT
"""D-STAR over DPlus (REF, UDP 20001): connect/login, the DSVT header and
voice packets of DExtra behind a 2-byte length/type prefix, and a client
that talks and collects 9-byte AMBE frames.

| Packet | Size | Layout |
|--------|------|--------|
| connect / echo | 5 B | 05 00 18 00 01; xlxd echoes it |
| login | 28 B | 1C C0 04 00 + callsign (16, zero padded) + "DV019999" |
| login OK / BUSY | 8 B | 08 C0 04 00 + "OKRW" / "BUSY", after 0..2 s |
| keepalive | 3 B | 03 60 00, both ways (xlxd sends one every second) |
| disconnect / echo | 5 B | 05 00 18 00 00 |
| header | 58 B | 3A 80 + 56 B DExtra DSVT header |
| voice | 29 B | 1D 80 + 27 B DExtra DSVT voice |
| last voice | 32 B | 20 80 + DSVT 20 00 81 00 20 00 01 02, stream id, seq or 0x40, end pattern (15) |

A DPlus client has no module after login: xlxd forwards a module to it
only once it has transmitted on that module (the first header makes it
the module's master, which sets its module). link() therefore joins by
sending an empty stream (header + last frame) to the module.
"""
import random

from . import dextra
from ..net import UdpClient

PORT = 20001
CONNECT = b"\x05\x00\x18\x00\x01"
DISCONNECT = b"\x05\x00\x18\x00\x00"
KEEPALIVE = b"\x03\x60\x00"
LOGIN_OK = b"\x08\xc0\x04\x00OKRW"
LOGIN_BUSY = b"\x08\xc0\x04\x00BUSY"
HEADER_SIZE = 58
VOICE_SIZE = 29
LAST_SIZE = 32
END_PATTERN = b"\x55\xc8\x7a" + b"\x55" * 9 + b"\x25\x1a\xc6"

_rng = random.SystemRandom()


def login_packet(callsign):
    """28 B: tag + callsign in a 16-byte zero-padded field + client id."""
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return b"\x1c\xc0\x04\x00" + (callsign.rstrip(b" ") + bytes(16))[:16] + b"DV019999"


def header(stream_id, callsign, module, reflector="XLX999"):
    return b"\x3a\x80" + dextra.header(stream_id, callsign, module, reflector)


def voice(stream_id, seq, frame, slow=dextra.SLOW_DATA):
    return b"\x1d\x80" + dextra.voice(stream_id, seq, frame, False, slow)


def last_voice(stream_id, seq):
    """32 B end of stream: seq mod 21 with 0x40, then the D-STAR end
    pattern instead of AMBE + slow data."""
    out = bytearray(b"\x20\x80DSVT\x20\x00\x81\x00\x20\x00\x01\x02")
    out += stream_id.to_bytes(2, "little")
    out.append((seq % 21) | 0x40)
    out += END_PATTERN
    return bytes(out)


def parse_header(data):
    """DExtra header fields of a 58 B DPlus header, else None."""
    if len(data) != HEADER_SIZE or data[:2] != b"\x3a\x80":
        return None
    return dextra.parse_header(data[2:])


def parse_voice(data):
    """(stream_id, seq, last, frame9) of a 29 B voice or 32 B last packet
    (frame9 of the last packet is the start of the end pattern), else None."""
    if len(data) == VOICE_SIZE and data[:2] == b"\x1d\x80":
        return dextra.parse_voice(data[2:])
    if len(data) == LAST_SIZE and data[:2] == b"\x20\x80" and data[2:6] == b"DSVT" \
            and data[6] == 0x20 and data[16] & 0x40:
        return (int.from_bytes(data[14:16], "little"), data[16] & 0x1F, True, data[17:26])
    return None


class DplusClient:
    def _keepalive(self, data, _addr):
        # xlxd sends 03 60 00 every second and drops a client it has not
        # heard from for 30 s; answering each one keeps the link up.
        if data == KEEPALIVE:
            return KEEPALIVE
        return None

    def __init__(self, bind_ip, callsign="N0CALL", module="B"):
        self.client = UdpClient(bind_ip, responder=self._keepalive)
        self.callsign = dextra._cs8(callsign)
        self.module = module
        self.dest = None

    def link(self, node_ip, timeout=3.0, port=PORT, join=True):
        """Connect, login (the OK comes after a random 0..2 s delay), then
        join the module with an empty stream unless join is False."""
        self.dest = (node_ip, port)
        self.client.send(self.dest, CONNECT)
        self.client.pump(0.3)
        if CONNECT not in self.client.inbox:
            return False
        self.client.send(self.dest, login_packet(self.callsign))
        if self._wait(timeout, (LOGIN_OK, LOGIN_BUSY)) != LOGIN_OK:
            return False
        if join:
            self.join()
        return True

    def _wait(self, timeout, wanted):
        steps = int(timeout / 0.05) + 1
        for _ in range(steps):
            self.client.drain()
            for packet in self.client.inbox:
                if packet in wanted:
                    return packet
            self.client.pump(0.05)
        return None

    def join(self, settle=1.0):
        """Header + last frame on the module: the client becomes its master
        for an empty stream and stays on the module afterwards."""
        sid = _rng.randrange(1, 1 << 16)
        self.client.send(self.dest, self.header(sid))
        self.client.pump(0.3)
        self.client.send(self.dest, last_voice(sid, 0))
        self.client.pump(settle)

    def keepalive(self):
        self.client.send(self.dest, KEEPALIVE)

    def unlink(self):
        """Disconnect; xlxd echoes it if the client was linked."""
        self.client.send(self.dest, DISCONNECT)
        self.client.pump(0.4)

    def header(self, stream_id, reflector="XLX999"):
        return header(stream_id, self.callsign, self.module, reflector)

    def talk(self, frames, stream_id, reflector="XLX999", settle=0.3, tail=2.0):
        """Stream 9-byte AMBE frames; the receiver drains."""
        packets = [self.header(stream_id, reflector)]
        for index, frame in enumerate(frames):
            # frame 0 of each 21-frame superframe carries the sync pattern
            packets.append(voice(stream_id, index, frame,
                                 dextra.SYNC_DATA if index % 21 == 0 else dextra.SLOW_DATA))
        packets.append(last_voice(stream_id, len(frames)))
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        self.client.paced(self.dest, packets[1:], 0.020)
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Voice frames received on this socket, in arrival order (the
        last-frame packet is excluded)."""
        self.client.pump(settle)
        out = []
        for packet in self.client.inbox:
            parsed = parse_voice(packet)
            if parsed and not parsed[2]:
                out.append(parsed[3])
        return out

    def close(self):
        self.client.close()
