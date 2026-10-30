# SPDX-License-Identifier: MIT
"""DMRplus "dongle" client protocol (UDP 8880), as xlxd and URFd serve it
(docs/protocols/dmrplus.md; no published wire format exists).

Connect / keepalive, 31 B: DMR id in ASCII decimal (bytes 0-7), reflector
number in ASCII (8-11, 4001..4026 = module A..Z), 12-30 unused (zero
here). Disconnect, 32 B: id in ASCII (0-7), module digit at 11 ('0' = A).
Replies "ACK OK\\n\\0" / "NAK OK\\n\\0". Voice, 72 B: sequence (4), type
(8: 2 header, 1 voice, 3 terminator), slot 0x2222 (16-17), slot type
(18-19: 0xEEEE header, 0x77..0xCC bursts A..F), colour code 0x1111
(20-21), 0x1111 (22-23), the 33-byte DMR burst + 1 pad byte with every
16-bit pair swapped (26-59), call type 1 = group (62), destination and
source id little endian (64-67, 68-71).

The burst helpers decode ETSI TS 102 361-1 content the egress must carry:
centre sync (9.1.1), slot type (9.1.3, Golay(20,8) not checked), the
BPTC(196,96) data of a voice LC header (B.1.1; extraction only)."""
import time

from ..net import UdpClient, pace
from . import dmr

PORT = 8880
CONNECT_SIZE = 31
DISCONNECT_SIZE = 32
PACKET_SIZE = 72
ACK = b"ACK OK\n\x00"
NAK = b"NAK OK\n\x00"
HEADER, VOICE, TERMINATOR = 2, 1, 3
SLOT2 = 0x22
GROUP = 1
KEEPALIVE_PERIOD = 1.0
GAP = 0.060                                     # one burst (three frames) per 60 ms
BS_DATA, MS_DATA, BS_VOICE = dmr.BS_DATA, dmr.MS_DATA, dmr.BS_VOICE
MS_VOICE = bytes.fromhex("7F7D5DD57DFD")
SLOT_TYPE_HEADER = 0xEE
DATA_TYPE_VOICE_LC_HEADER, DATA_TYPE_TERMINATOR = 1, 2


def reflector_of(module):
    return 4001 + (ord(module) - ord("A"))


def _ascii(value, width):
    text = str(value).encode("ascii")[:width]
    return text + bytes(width - len(text))


def connect_packet(dmrid, reflector):
    """31-byte connect / keepalive."""
    return _ascii(dmrid, 8) + _ascii(reflector, 4) + bytes(19)


def disconnect_packet(dmrid, module="A"):
    out = bytearray(DISCONNECT_SIZE)
    out[0:8] = _ascii(dmrid, 8)
    out[11] = ord("0") + (ord(module) - ord("A"))
    return bytes(out)


def swap16(data):
    """data (even length) with every 16-bit pair swapped."""
    out = bytearray(data)
    out[0::2], out[1::2] = data[1::2], data[0::2]
    return bytes(out)


def packet(kind, seq, burst, src, dst, slot_type, slot=SLOT2, colour_code=1, call=GROUP, layout="pair34"):
    """One 72-byte voice packet. layout "pair34": burst + pad swapped as 17
    pairs (what xlxd and URFd write); "pair33": the first 32 bytes swapped,
    byte 32 at wire offset 58 (what both read)."""
    if len(burst) != 33:
        raise ValueError("DMR burst is 33 bytes")
    out = bytearray(PACKET_SIZE)
    out[4] = seq & 0xFF
    out[8] = kind
    out[16:18] = bytes((slot, slot))
    out[18:20] = bytes((slot_type, slot_type))
    cc = ((colour_code & 0x0F) << 4) | (colour_code & 0x0F)
    out[20:22] = bytes((cc, cc))
    out[22:24] = b"\x11\x11"
    if layout == "pair34":
        out[26:60] = swap16(bytes(burst) + b"\x00")
    else:
        out[26:58] = swap16(bytes(burst[:32]))
        out[58] = burst[32]
    out[62] = call
    out[64:68] = (dst & 0xFFFFFF).to_bytes(4, "little")
    out[68:72] = (src & 0xFFFFFF).to_bytes(4, "little")
    return bytes(out)


def parse(data):
    """dict of a 72-byte voice packet (burst read with the pair34 layout,
    "burst33" with the pair33 one), None for other sizes."""
    if len(data) != PACKET_SIZE:
        return None
    return {"seq": data[4], "type": data[8], "slot": data[16], "slot_type": data[18], "cc": data[20] & 0x0F,
            "call": data[62], "dst": int.from_bytes(data[64:68], "little") & 0xFFFFFF,
            "src": int.from_bytes(data[68:72], "little") & 0xFFFFFF,
            "burst": swap16(bytes(data[26:60]))[:33],
            "burst33": swap16(bytes(data[26:58])) + bytes(data[58:59])}


def burst_id(slot_type):
    """0..5 for bursts A..F (slot type 0x77..0xCC), else None."""
    nibble = slot_type & 0x0F
    return nibble - 7 if 7 <= nibble <= 12 else None


def stream(frames, src, dst, seq0=0, head=True, end=True, colour_code=1, slot=SLOT2, call=GROUP,
           layout="pair34", embedded_lc=True):
    """Header, voice bursts A-F (three 9-byte AMBE+2 frames each, A with
    BS voice sync, B-F with EMB and the embedded LC), terminator."""
    out = []
    seq = seq0

    def add(kind, burst, slot_type):
        nonlocal seq
        out.append(packet(kind, seq, burst, src, dst, slot_type, slot, colour_code, call, layout))
        seq += 1

    if head:
        add(HEADER, dmr.data_sync_payload(BS_DATA), SLOT_TYPE_HEADER)
    bursts, _ = dmr.voice_packets(frames, src, dst, 1, embedded_lc=embedded_lc, colour_code=colour_code)
    for index, data in enumerate(bursts):
        payload = dmr.parse(data)["payload"]
        vseq = index % 6
        if vseq == 0:
            payload = with_centre(payload, BS_VOICE)
        add(VOICE, payload, (vseq + 7) * 0x11)
    if end:
        add(TERMINATOR, dmr.data_sync_payload(BS_DATA), SLOT_TYPE_HEADER)
    return out


def frames_of(parsed, layout="burst"):
    """AMBE+2 frames of parsed voice packets (type 1), in order."""
    out = []
    for p in parsed:
        if p["type"] == VOICE:
            out.extend(dmr.extract(p[layout]))
    return out


# ---------------------------------------------------------------- burst content (ETSI TS 102 361-1)

_CENTRE = 108


def centre(burst):
    """The 48-bit centre (sync or EMB + embedded) of a 33-byte burst."""
    return ((int.from_bytes(burst, "big") >> _CENTRE) & ((1 << 48) - 1)).to_bytes(6, "big")


def with_centre(burst, sync):
    value = int.from_bytes(burst, "big") & ~(((1 << 48) - 1) << _CENTRE)
    return (value | (int.from_bytes(sync, "big") << _CENTRE)).to_bytes(33, "big")


def _bits(burst):
    value = int.from_bytes(burst, "big")
    return [(value >> (263 - i)) & 1 for i in range(264)]


def slot_type(burst):
    """(colour code, data type) of a data burst: the first 8 of the 20
    slot type bits (bits 98-107 and 156-165; Golay parity not checked)."""
    bits = _bits(burst)
    word = bits[98:108] + bits[156:166]
    value = 0
    for bit in word[:8]:
        value = (value << 1) | bit
    return value >> 4, value & 0x0F


def bptc196_info(burst):
    """The 96 information bits (12 bytes) of the BPTC(196,96) block of a
    data burst: bits 0-97 and 166-263, de-interleaved (index i * 181 mod
    196), the 13 x 15 matrix rows read without the reserved bits and the
    Hamming columns. No error correction."""
    bits = _bits(burst)
    raw = bits[0:98] + bits[166:264]
    deint = [raw[(i * 181) % 196] for i in range(196)]
    info = deint[4:12]
    for start in range(16, 136, 15):
        info += deint[start:start + 11]
    value = 0
    for bit in info:
        value = (value << 1) | bit
    return value.to_bytes(12, "big")


def lc_header(burst):
    """{"sync", "cc", "data_type", "lc"} of a header / terminator burst;
    lc = parse_full_lc of the first 9 information bytes."""
    cc, data_type = slot_type(burst)
    info = bptc196_info(burst)
    return {"sync": centre(burst), "cc": cc, "data_type": data_type, "lc": dmr.parse_full_lc(info[:9]),
            "info": info}


class DmrPlusClient:
    """One dongle on one reflector (module)."""

    def __init__(self, bind_ip, dmrid=1023007, module="A"):
        self.client = UdpClient(bind_ip)
        self.id = dmrid
        self.module = module
        self.reflector = reflector_of(module)
        self.dest = None
        self.last_keepalive = 0.0

    def keepalive(self, reflector=None):
        self.client.send(self.dest, connect_packet(self.id, reflector or self.reflector))
        self.last_keepalive = time.monotonic()

    def pump(self, seconds):
        """Drain for seconds, sending the keepalive every second."""
        deadline = time.monotonic() + seconds
        while True:
            if self.dest and time.monotonic() - self.last_keepalive >= KEEPALIVE_PERIOD:
                self.keepalive()
            self.client.drain()
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self.client.pump(min(left, 0.1))

    def link(self, node_ip, timeout=2.0, port=PORT):
        """Connect; the replies within timeout (b"" list when none)."""
        self.dest = (node_ip, port)
        self.client.inbox.clear()
        self.keepalive()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.drain()
            if ACK in self.client.inbox or NAK in self.client.inbox:
                self.client.pump(0.2)
                break
            time.sleep(0.01)
        return [d for d in self.client.inbox if d in (ACK, NAK)]

    def disconnect(self):
        self.client.send(self.dest, disconnect_packet(self.id, self.module))
        self.client.pump(0.2)

    def send(self, packets, gap=GAP, *others):
        """Send at gap spacing, keepalives on schedule, draining this
        client and the others after each packet."""
        start = time.monotonic()
        for index, data in enumerate(packets):
            pace(start, index, gap)
            if time.monotonic() - self.last_keepalive >= KEEPALIVE_PERIOD:
                self.keepalive()
            self.client.send(self.dest, data)
            self.client.drain()
            for other in others:
                other.client.drain()

    def talk(self, frames, dst=None, tail=1.0, **shape):
        self.send(stream(frames, self.id, dst or self.reflector, **shape))
        self.pump(tail)

    def voice(self):
        """Parsed 72-byte packets in the inbox."""
        return [p for p in (parse(d) for d in self.client.inbox) if p is not None]

    def close(self):
        self.client.close()
