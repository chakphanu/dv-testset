# SPDX-License-Identifier: MIT
"""D-STAR over DExtra (UDP 30001): link, DSVT header/voice packets and a
client that talks and collects 9-byte AMBE frames."""
from ..net import UdpClient

PORT = 30001
SLOW_DATA = b"\x00\x00\x00"
SYNC_DATA = b"\x55\x2d\x16"
HEADER_SIZE = 56
VOICE_SIZE = 27


def _u16(value):
    return value.to_bytes(2, "little")


def _cs8(callsign):
    if isinstance(callsign, str):
        callsign = callsign.encode()
    return (callsign + b" " * 8)[:8]


def link_packet(callsign, module, own_module=None):
    """callsign(8) + own module + reflector module + 0x00. xlxd ACKs with
    callsign(8) + module pair + b"ACK\\x00"."""
    own = (own_module or module).encode()
    return _cs8(callsign) + own + module.encode() + b"\x00"


def unlink_packet(callsign, module):
    return _cs8(callsign) + module.encode() + b" " + b"\x00"


def keepalive_packet(callsign):
    """DExtra client keepalive: own callsign (8) + 0x00."""
    return _cs8(callsign) + b"\x00"


def is_reflector_keepalive(data):
    """xlxd sends a 9-byte "XRFxxx \\x20\\x00" keepalive to DExtra peers."""
    return len(data) == 9 and data[8:9] == b"\x00" and data[:3] == b"XRF"


def header(stream_id, callsign, module, reflector="XLX999"):
    """56 B DSVT header. rpt2 = reflector + module, rpt1 = callsign(7) +
    module, ur = CQCQCQ, my = callsign; suffix and CRC are left as spaces."""
    cs = _cs8(callsign)
    mod = module.encode()
    body = bytearray(b" " * 41)
    body[0] = body[1] = body[2] = 0
    body[3:11] = ((reflector + " " * 7)[:7]).encode() + mod
    body[11:19] = cs[:7] + mod
    body[19:27] = b"CQCQCQ  "
    body[27:35] = cs
    packet = bytearray(b"DSVT\x10\x00\x00\x00\x20\x00\x01\x02")
    packet += _u16(stream_id) + bytes([0x80]) + body
    return bytes(packet)


def voice(stream_id, seq, frame, last, slow=SLOW_DATA):
    """27 B DSVT voice: seq mod 21, 0x40 marks the last frame."""
    packet = bytearray(b"DSVT\x20\x00\x00\x00\x20\x00\x01\x02")
    packet += _u16(stream_id)
    packet.append((seq % 21) | (0x40 if last else 0))
    packet += frame + slow
    return bytes(packet)


def parse_voice(data):
    """(stream_id, seq, last, frame9) for a DSVT voice packet, else None."""
    if len(data) != VOICE_SIZE or not data.startswith(b"DSVT") or data[4] != 0x20:
        return None
    return (int.from_bytes(data[12:14], "little"), data[14] & 0x1F,
            bool(data[14] & 0x40), data[15:24])


def parse_header(data):
    """dict of header fields for a DSVT header packet, else None."""
    if len(data) != HEADER_SIZE or not data.startswith(b"DSVT") or data[4] != 0x10:
        return None
    body = data[15:]
    return {"stream_id": int.from_bytes(data[12:14], "little"), "rpt2": body[3:11],
            "rpt1": body[11:19], "ur": body[19:27], "my": body[27:35]}


class DstarClient:
    def _keepalive(self, data, _addr):
        # Echoing the reflector keepalive does NOT refresh the client (xlxd
        # logs "keepalive timeout" ~40 s after linking). Answer with the
        # client's own 9-byte keepalive instead.
        if is_reflector_keepalive(data):
            return keepalive_packet(self.callsign)
        return None

    def __init__(self, bind_ip, callsign="N0CALL", module="B"):
        self.client = UdpClient(bind_ip, responder=self._keepalive)
        self.callsign = _cs8(callsign)
        self.module = module
        self.dest = None

    def link(self, node_ip, timeout=2.5, port=PORT):
        """callsign(8) + module + module + 0x00 is ACKed; a single module
        byte or bytes([11]) as the trailing byte is not."""
        self.dest = (node_ip, port)
        self.client.send(self.dest, link_packet(self.callsign, self.module))
        self.client.pump(timeout)
        return any(b"ACK" in packet for packet in self.client.inbox)

    def keepalive(self):
        self.client.send(self.dest, keepalive_packet(self.callsign))

    def unlink(self):
        self.client.send(self.dest, unlink_packet(self.callsign, self.module))
        self.client.pump(0.4)

    def header(self, stream_id, reflector="XLX999"):
        return header(stream_id, self.callsign, self.module, reflector)

    def voice(self, stream_id, seq, frame, last):
        return voice(stream_id, seq, frame, last)

    def talk(self, frames, stream_id, reflector="XLX999", settle=0.3, tail=2.0):
        """Stream 9-byte AMBE frames; the receiver drains."""
        packets = [self.header(stream_id, reflector)]
        for index, frame in enumerate(frames):
            # frame 0 of each 21-frame superframe carries the sync pattern
            packets.append(voice(stream_id, index, frame, False,
                                 SYNC_DATA if index % 21 == 0 else SLOW_DATA))
        packets.append(voice(stream_id, len(frames), b"\x00" * 9, True))
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        # send times of the voice packets (header excluded), for latency
        self.last_stamps = self.client.paced(self.dest, packets[1:], 0.020)
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
