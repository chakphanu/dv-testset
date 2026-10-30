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


def link_packet(callsign, module, own_module=None, tail=0):
    """callsign(8) + own module + reflector module + tail (0x00; 11 is the
    "revision 1" form DroidStar sends). xlxd ACKs with callsign(8) +
    module pair + b"ACK\\x00"."""
    own = (own_module or module).encode()
    return _cs8(callsign) + own + module.encode() + bytes([tail])


def raw_link(callsign8, own, reflector, tail=0):
    """11 B link from raw parts (bytes or str), no padding or checks: for
    invalid callsigns and module bytes."""
    def raw(value):
        return value.encode() if isinstance(value, str) else bytes(value)
    out = raw(callsign8) + raw(own) + raw(reflector) + bytes([tail])
    if len(out) != 11:
        raise ValueError("raw link is %d bytes, not 11" % len(out))
    return out


def xrf_link_packet(name, local, remote):
    """Link from a reflector peer: its XRF name (8), its own module, the
    module it links to, 0x00 ("revision 2" in the XLX family)."""
    return _cs8(name) + local.encode() + remote.encode() + b"\x00"


NAT_PUNCH = b"\x00"            # DStarGateway's 1-byte hole punch before a link
DISCONNECTED = b"DISCONNECTED"  # XLX-family answer to a revision 1 unlink


def parse_link_reply(data):
    """Classify a reflector answer on the link socket: dict with "kind"
    ack / nak (14 B: link bytes 0-9 + tag + 0x00), rev2 (11 B: name +
    two module letters + 0x00), disconnected (12 B), notice_blank (11 B,
    ten spaces + 0x00), unlink (11 B callsign + module + space + 0x00),
    keepalive (9 B), else None."""
    size = len(data)
    if size == 14 and data[13:14] == b"\x00" and data[10:13] in (b"ACK", b"NAK"):
        return {"kind": data[10:13].decode().lower(), "callsign": data[:8], "own": data[8:9],
                "module": data[9:10]}
    if size == 12 and data == DISCONNECTED:
        return {"kind": "disconnected"}
    if size == 11 and data[10:11] == b"\x00":
        if data[:10] == b" " * 10:
            return {"kind": "notice_blank"}
        if data[9:10] == b" ":
            return {"kind": "unlink", "callsign": data[:8], "module": data[8:9]}
        return {"kind": "rev2", "callsign": data[:8], "own": data[8:9], "module": data[9:10]}
    if size == 9 and data[8:9] == b"\x00":
        return {"kind": "keepalive", "callsign": data[:8]}
    return None


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


# ---------------------------------------------------------------- full builders

END_PATTERN = b"\x55\x55\x55\x55\xc8\x7a" + bytes(6)   # JARL 4.1.2 h end pattern, AMBE + slow data
NULL_AMBE = b"\x9e\x8d\x32\x88\x26\x1a\x3f\x61\xe8"     # JARL 6.6 AMBE silence
SCRAMBLE = b"\x70\x4f\x93"                               # JARL Ap1 per-frame slow data XOR
NULL_SLOW = b"\x16\x29\xf5"                              # filler 0x66 66 66, scrambled
TRUNK_IDS = b"\x00\x01\x02"                              # bytes 9-11 as xlxd and URFd send them


def _field(value, size=8):
    if isinstance(value, str):
        value = value.encode()
    return (bytes(value) + b" " * size)[:size]


def header_crc(radio39):
    """P_FCS of the first 39 radio header bytes: CRC-CCITT x^16+x^12+x^5+1
    as the gateways compute it (reflected, init 0xFFFF, final inversion,
    i.e. CRC-16/X-25), low byte first."""
    crc = 0xFFFF
    for byte in bytes(radio39[:39]):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (crc ^ 0xFFFF).to_bytes(2, "little")


def radio_header(my="N0CALL", rpt1=None, rpt2=None, ur="CQCQCQ", suffix="    ",
                 flags=b"\x00\x00\x00", crc=None, module="B", reflector="XLX999"):
    """41 B radio header (JARL 4.1.1): flags 3, RPT2 8, RPT1 8, UR 8, MY 8,
    suffix 4, P_FCS 2. rpt2 defaults to reflector + module, rpt1 to MY(7)
    + module; crc None computes it, else the 2 given bytes are used."""
    rpt2 = rpt2 if rpt2 is not None else _field(reflector, 7) + module.encode()
    rpt1 = rpt1 if rpt1 is not None else _field(my, 7) + module.encode()
    body = (bytes(flags) + _field(rpt2) + _field(rpt1) + _field(ur) + _field(my)
            + _field(suffix, 4))
    if len(body) != 39:
        raise ValueError("flags must be 3 bytes")
    return body + (header_crc(body) if crc is None else bytes(crc)[:2])


def header_packet(stream_id, radio=None, ids=TRUNK_IDS, byte8=0x20, byte14=0x80, **fields):
    """56 B DSVT header with every byte settable: radio is a 41 B radio
    header (else radio_header(**fields))."""
    radio = radio if radio is not None else radio_header(**fields)
    return (b"DSVT\x10\x00\x00\x00" + bytes([byte8]) + bytes(ids) + _u16(stream_id)
            + bytes([byte14]) + bytes(radio))


def voice_packet(stream_id, counter, ambe, slow=SLOW_DATA, ids=TRUNK_IDS, byte8=0x20):
    """27 B DSVT voice with the raw counter byte (no mod 21, no last bit)."""
    return (b"DSVT\x20\x00\x00\x00" + bytes([byte8]) + bytes(ids) + _u16(stream_id)
            + bytes([counter & 0xFF]) + bytes(ambe) + bytes(slow))


def last_frame(stream_id, counter, payload=END_PATTERN):
    """Last voice packet: counter mod 21 + 0x40, 12 B payload (AMBE + slow
    data; default the JARL end pattern)."""
    payload = bytes(payload)
    return voice_packet(stream_id, (counter % 21) | 0x40, payload[:9], payload[9:12])


def parse_header_full(data):
    """Every field of a 56 B DSVT header (prefix included), else None."""
    if len(data) != HEADER_SIZE or not data.startswith(b"DSVT") or data[4] != 0x10:
        return None
    radio = data[15:]
    return {"stream_id": int.from_bytes(data[12:14], "little"), "byte8": data[8],
            "ids": data[9:12], "byte14": data[14], "flags": radio[0:3], "rpt2": radio[3:11],
            "rpt1": radio[11:19], "ur": radio[19:27], "my": radio[27:35],
            "suffix": radio[35:39], "crc": radio[39:41], "crc_ok": crc_ok(data)}


def crc_ok(data):
    """Does the P_FCS match? data is a 56 B DSVT header or a 41 B radio header."""
    radio = data[15:] if len(data) == HEADER_SIZE else data
    return len(radio) == 41 and header_crc(radio[:39]) == bytes(radio[39:41])


def parse_voice_raw(data):
    """27 B DSVT voice: dict stream_id, counter (raw byte), last, ambe,
    slow, byte8, ids; else None."""
    if len(data) != VOICE_SIZE or not data.startswith(b"DSVT") or data[4] != 0x20:
        return None
    return {"stream_id": int.from_bytes(data[12:14], "little"), "counter": data[14],
            "last": bool(data[14] & 0x40), "ambe": data[15:24], "slow": data[24:27],
            "byte8": data[8], "ids": data[9:12]}


def scramble(data3):
    """XOR the per-frame slow data scrambler (also unscrambles)."""
    return bytes(a ^ b for a, b in zip(bytes(data3), SCRAMBLE))


def _slow_frames(blocks):
    """6-byte slow data blocks -> 20 scrambled 3-byte groups (frames 1..20
    of a superframe), filler 0x66 after the blocks."""
    raw = b"".join(blocks)
    raw = (raw + b"\x66" * 60)[:60]
    return [scramble(raw[i:i + 3]) for i in range(0, 60, 3)]


def slow_text(text):
    """20-character message (JARL 6.2, mini-headers 0x40-0x43) as 20
    scrambled 3-byte groups for frames 1..20."""
    text = _field(text, 20)
    return _slow_frames([bytes([0x40 | i]) + text[5 * i:5 * i + 5] for i in range(4)])


def slow_header_resend(radio41):
    """Radio header resend in slow data (JARL 6.4, mini-header 0x50 + count)
    as 20 scrambled 3-byte groups for frames 1..20."""
    radio = bytes(radio41)
    blocks = []
    for i in range(0, len(radio), 5):
        part = radio[i:i + 5]
        blocks.append(bytes([0x50 | len(part)]) + (part + b"\x66" * 5)[:5])
    return _slow_frames(blocks)


class DstarClient:
    def _keepalive(self, data, _addr):
        # Echoing the reflector keepalive does NOT refresh the client (xlxd
        # logs "keepalive timeout" ~40 s after linking). Answer with the
        # client's own 9-byte keepalive instead.
        if is_reflector_keepalive(data):
            return keepalive_packet(self.callsign)
        return None

    def _echo(self, data, _addr):
        return data if is_reflector_keepalive(data) else None

    def __init__(self, bind_ip, callsign="N0CALL", module="B", keepalive="own"):
        """keepalive: answer the reflector keepalive with the own callsign
        ("own", what the gateways send), echo it back ("echo") or stay
        silent ("none")."""
        responder = {"own": self._keepalive, "echo": self._echo, "none": None}[keepalive]
        self.client = UdpClient(bind_ip, responder=responder)
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

    def link_to(self, module, port=PORT):
        """Send a link to another module from the same socket and callsign,
        without an unlink first (the reply lands in the inbox)."""
        self.client.send((self.dest[0], port), link_packet(self.callsign, module))

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
