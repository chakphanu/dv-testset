# SPDX-License-Identifier: MIT
"""D-STAR over DCS (UDP 30051): link, keepalive, 100-byte voice packets
that each carry the D-STAR header, and a client that talks and collects
9-byte AMBE frames.

| Packet | Size | Layout |
|--------|------|--------|
| link | 519 B | callsign(7) + own module + own module + reflector module + 0x00 + text, zero padded |
| link ACK / NAK | 14 B | callsign(7) + ' ' + own module + reflector module + `ACK\\0` / `NAK\\0`, after 0..2 s |
| unlink | 11 B | callsign(7) + own module + own module + ' ' + 0x00; xlxd answers with a NAK |
| keepalive (client) | 17 B | callsign(7) + own module + 0x00 + reflector(7) + reflector module |
| keepalive (xlxd) | 8 + 22 B | reflector callsign (8, `DCS999`); reflector(7) + module + ' ' + callsign(7) + module + module + 0A 00 20 20 |
| voice | 100 B | `0001`, header without CRC (39: flags 3, RPT2, RPT1, UR, MY, suffix 4), stream id u16 LE, seq (mod 21, 0x40 = last), AMBE(9), slow data(3), packet counter u24 LE, 0x01, 38 B text area |

xlxd takes the module from the link, not from RPT2, and opens the stream
on the first voice packet. The text area of xlxd's own packets carries
0x21 at byte 63 and a 20 character text at 64..83.
"""
from . import dextra
from ..net import UdpClient

PORT = 30051
LINK_SIZE = 519
ACK_SIZE = 14
PACKET_SIZE = 100
TEXT = b"<table><tr><td>dvtest</td></tr></table>"


def _cs7(callsign):
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return (callsign.rstrip(b" ") + b" " * 7)[:7]


def link_packet(callsign, module, own_module=None):
    own = (own_module or module).encode()
    out = _cs7(callsign) + own + own + module.encode() + b"\x00" + TEXT
    return out + bytes(LINK_SIZE - len(out))


def unlink_packet(callsign, own_module):
    own = own_module.encode()
    return _cs7(callsign) + own + own + b" \x00"


def keepalive_packet(callsign, own_module, reflector="XLX999", module="B"):
    return _cs7(callsign) + own_module.encode() + b"\x00" + _cs7(reflector) + module.encode()


def parse_ack(data):
    """"ACK" or "NAK" of a 14 B link reply, else None."""
    if len(data) != ACK_SIZE or data[13:14] != b"\x00" or data[10:13] not in (b"ACK", b"NAK"):
        return None
    return data[10:13].decode()


def is_reflector_keepalive(data):
    """The 22 B per-client keepalive xlxd sends every 5 s."""
    return len(data) == 22 and data[18:22] == b"\x0a\x00\x20\x20"


def voice(stream_id, seq, frame, last, counter, callsign, module, reflector="XLX999",
          slow=dextra.SLOW_DATA):
    """100 B voice packet. RPT2 = reflector + module, RPT1 = callsign(7) +
    module, UR = CQCQCQ, MY = callsign, as dextra.header builds them."""
    body = dextra.header(stream_id, callsign, module, reflector)[15:15 + 39]
    out = bytearray(b"0001" + body)
    out += stream_id.to_bytes(2, "little")
    out.append((seq % 21) | (0x40 if last else 0))
    out += frame + slow
    out += (counter & 0xFFFFFF).to_bytes(3, "little")
    out.append(0x01)
    out += bytes(PACKET_SIZE - len(out))
    return bytes(out)


def parse(data):
    """dict of voice packet fields, or None."""
    if len(data) < PACKET_SIZE or not data.startswith(b"0001"):
        return None
    head = data[4:43]
    return {"rpt2": head[3:11], "rpt1": head[11:19], "ur": head[19:27], "my": head[27:35],
            "stream_id": int.from_bytes(data[43:45], "little"), "seq": data[45] & 0x1F,
            "last": bool(data[45] & 0x40), "frame": data[46:55], "slow": data[55:58],
            "counter": int.from_bytes(data[58:61], "little")}


class DcsClient:
    def _keepalive(self, data, _addr):
        # xlxd drops a DCS client it has not heard from for 60 s; answer
        # its per-client keepalive with ours.
        if is_reflector_keepalive(data):
            return keepalive_packet(self.callsign, self.module, module=self.module)
        return None

    def __init__(self, bind_ip, callsign="N0CALL", module="B"):
        self.client = UdpClient(bind_ip, responder=self._keepalive)
        self.callsign = _cs7(callsign)
        self.module = module
        self.dest = None

    def link(self, node_ip, timeout=3.0, port=PORT):
        """Link to the module; the ACK comes after a random 0..2 s delay."""
        self.dest = (node_ip, port)
        self.client.send(self.dest, link_packet(self.callsign, self.module))
        steps = int(timeout / 0.05) + 1
        for _ in range(steps):
            self.client.drain()
            for packet in self.client.inbox:
                reply = parse_ack(packet)
                if reply:
                    return reply == "ACK"
            self.client.pump(0.05)
        return False

    def keepalive(self):
        self.client.send(self.dest, keepalive_packet(self.callsign, self.module, module=self.module))

    def unlink(self):
        self.client.send(self.dest, unlink_packet(self.callsign, self.module))
        self.client.pump(0.4)

    def packets(self, frames, stream_id, reflector="XLX999"):
        out = []
        for index, frame in enumerate(frames):
            out.append(voice(stream_id, index, frame, False, index, self.callsign, self.module,
                             reflector, dextra.SYNC_DATA if index % 21 == 0 else dextra.SLOW_DATA))
        out.append(voice(stream_id, len(frames), b"\x00" * 9, True, len(frames), self.callsign,
                         self.module, reflector))
        return out

    def talk(self, frames, stream_id, reflector="XLX999", settle=0.3, tail=2.0):
        """Stream 9-byte AMBE frames: the first packet opens the stream,
        the rest follow every 20 ms; the receiver drains."""
        packets = self.packets(frames, stream_id, reflector)
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        self.client.paced(self.dest, packets[1:], 0.020)
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Voice frames received on this socket, in arrival order (the
        last packet is excluded)."""
        self.client.pump(settle)
        out = []
        for packet in self.client.inbox:
            parsed = parse(packet)
            if parsed and not parsed["last"]:
                out.append(parsed["frame"])
        return out

    def close(self):
        self.client.close()
