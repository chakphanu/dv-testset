# SPDX-License-Identifier: MIT
"""NXDN over the NXDNReflector network protocol (UDP 41400), as an
NXDNGateway speaks it. xlxd talks NXDN only as a peer of an NXDNReflector,
so test clients link to the reflector, not to xlxd.

Packets (all big endian):

  NXDNP  17 B  'NXDNP' + callsign (10, space padded) + TG u16   poll / link;
               the reflector answers each poll with the same 17 bytes
  NXDNU  17 B  'NXDNU' + callsign (10) + TG u16                  unlink
  NXDND  43 B  'NXDND' + src id u16 + dst id u16 + flags u8 + payload (33)

Flags: 0x01 group call, 0x02 data, 0x04 header (VCALL), 0x08 trailer
(TX_REL). The reflector only relays group packets whose dst id is its TG.

Payload (33 B, one 80 ms NXDN frame without sync word):

  [0]       LICH: RFCT(2) FCT(2) option(2) direction(1) parity(1)
  [1:5]     SACCH: structure(2) RAN(6) data(18) CRC-6(6)
  [5:19]    block 1: two 49-bit voice frames at bits 0 and 49, or FACCH1
  [19:33]   block 2: two 49-bit voice frames, or the FACCH1 again

FACCH1 (header / trailer): layer 3 message (10 B: type, options,
0x20 = group, src u16, dst u16, 3 x 0) + CRC-12 + 4 zero bits = 12 B.

Voice frames are AMBE+2 3600x2450 without FEC: 49 bits a(12) b(12) c(25),
held here as 7 bytes MSB first. xlxd and the AMBEd transcoder use the DMR
form instead: 72 bits with Golay(24,12) on a, scrambled Golay(23,12) on
b, interleaved over 9 bytes. to_dmr() / from_dmr() convert.
"""
import time

from ..net import UdpClient

PORT = 41400
NXDN_ID = 1001                      # N0CALL in rig/config/nxdn.csv
CALLSIGN = "N0CALL"
POLL_SIZE = 17
PACKET_SIZE = 43
PAYLOAD_SIZE = 33
FRAME_BYTES = 7                     # 49 bits
FRAME_BITS = 49
FRAMES_PER_PACKET = 4
PACKET_GAP = 0.080

GROUP, DATA, HEADER, TRAILER = 0x01, 0x02, 0x04, 0x08
VCALL, TX_REL = 0x01, 0x08

# LICH fields
RFCT_RDCH = 2
FCT_SACCH_NS, FCT_SACCH_SS = 0, 2
OPTION_STEAL_FACCH, OPTION_STEAL_NONE = 0, 3

# gain index b2 of AMBE+2 2450: raw frame bits 8..11 (high 4) and 36 (low)
GAIN_BITS = (8, 9, 10, 11, 36)


# ------------------------------------------------------------------ bits

def _get(data, index):
    return (data[index >> 3] >> (7 - (index & 7))) & 1


def _put(buf, index, bit):
    mask = 0x80 >> (index & 7)
    if bit:
        buf[index >> 3] |= mask
    else:
        buf[index >> 3] &= ~mask & 0xFF


def _read(data, start, count):
    value = 0
    for index in range(start, start + count):
        value = (value << 1) | _get(data, index)
    return value


def _write(buf, start, count, value):
    for index in range(count):
        _put(buf, start + index, (value >> (count - 1 - index)) & 1)


def _crc(data, nbits, width, poly, init):
    """MSB-first CRC of the first nbits bits of data."""
    top = 1 << (width - 1)
    crc = init
    for index in range(nbits):
        feedback = _get(data, index) ^ (1 if crc & top else 0)
        crc = (crc << 1) & ((1 << width) - 1)
        if feedback:
            crc ^= poly
    return crc


def crc6(data, nbits=26):
    """SACCH CRC-6: x^6 + x^5 + x^2 + x + 1, preset all ones."""
    return _crc(data, nbits, 6, 0x27, 0x3F)


def crc12(data, nbits=80):
    """FACCH1 CRC-12: x^12 + x^11 + x^3 + x^2 + x + 1, preset all ones."""
    return _crc(data, nbits, 12, 0x80F, 0xFFF)


# ------------------------------------------------------------------ FEC (DMR form)

GOLAY_POLY = 0xC75                  # x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1


def golay23(data12):
    """Systematic Golay(23,12): data in the top 12 bits, 11 parity bits."""
    rem = data12 << 11
    for shift in range(22, 10, -1):
        if rem & (1 << shift):
            rem ^= GOLAY_POLY << (shift - 11)
    return (data12 << 11) | rem


def golay24(data12):
    """Extended Golay(24,12): golay23 plus an even-parity bit at the end."""
    word = golay23(data12)
    return (word << 1) | (bin(word).count("1") & 1)


def prng_mask(a12):
    """23-bit scrambling mask of block b, seeded by the 12 data bits of a:
    x0 = 16 a, x(i) = (173 x(i-1) + 13849) mod 65536, bit i = x(i) >= 32768."""
    x = 16 * a12
    mask = 0
    for _ in range(23):
        x = (173 * x + 13849) % 65536
        mask = (mask << 1) | (x >> 15)
    return mask


# 72 bit positions of the 9-byte DMR frame, taken four columns of 18 at a
# time; a gets the first 24, b the next 23, c the last 25.
_DMR_ORDER = [4 * row + column for column in range(4) for row in range(18)]
DMR_A, DMR_B, DMR_C = _DMR_ORDER[:24], _DMR_ORDER[24:47], _DMR_ORDER[47:]


def _scatter(buf, positions, value):
    count = len(positions)
    for index, pos in enumerate(positions):
        _put(buf, pos, (value >> (count - 1 - index)) & 1)


def _gather(data, positions):
    value = 0
    for pos in positions:
        value = (value << 1) | _get(data, pos)
    return value


def split(frame7):
    """(a 12 bits, b 12 bits, c 25 bits) of a 49-bit frame."""
    return _read(frame7, 0, 12), _read(frame7, 12, 12), _read(frame7, 24, 25)


def join(a12, b12, c25):
    buf = bytearray(FRAME_BYTES)
    _write(buf, 0, 12, a12)
    _write(buf, 12, 12, b12)
    _write(buf, 24, 25, c25)
    return bytes(buf)


def to_dmr(frame7):
    """49-bit frame -> 9-byte DMR AMBE+2 frame (FEC added, b scrambled)."""
    a12, b12, c25 = split(frame7)
    out = bytearray(9)
    _scatter(out, DMR_A, golay24(a12))
    _scatter(out, DMR_B, golay23(b12) ^ prng_mask(a12))
    _scatter(out, DMR_C, c25)
    return bytes(out)


def from_dmr(frame9):
    """9-byte DMR frame -> 49-bit frame: data bits only, no error
    correction (b is unscrambled with the mask of the received a)."""
    a12 = _gather(frame9, DMR_A) >> 12
    b12 = (_gather(frame9, DMR_B) ^ prng_mask(a12)) >> 11
    return join(a12, b12, _gather(frame9, DMR_C))


def dmr_valid(frame9):
    """True when the a and b blocks of a DMR frame are exact codewords."""
    return to_dmr(from_dmr(frame9)) == bytes(frame9)


def gain_index(frame7):
    """AMBE+2 gain parameter b2 (0..31) of a 49-bit frame."""
    value = 0
    for pos in GAIN_BITS:
        value = (value << 1) | _get(frame7, pos)
    return value


def without_gain(frame7):
    """The frame with its gain bits cleared, to compare everything else."""
    buf = bytearray(frame7)
    for pos in GAIN_BITS:
        _put(buf, pos, 0)
    return bytes(buf)


# ------------------------------------------------------------------ payload

def lich(rfct, fct, option, outbound=False):
    """LICH byte; the parity bit is the XOR of the four RFCT/FCT bits."""
    value = (rfct << 6) | (fct << 4) | (option << 2) | (2 if outbound else 0)
    return value | (bin(value >> 4).count("1") & 1)


def parse_lich(byte):
    return {"rfct": byte >> 6, "fct": (byte >> 4) & 3, "option": (byte >> 2) & 3,
            "outbound": bool(byte & 2), "parity_ok": (byte & 1) == (bin(byte >> 4).count("1") & 1)}


def layer3(msg, src, dst):
    """10-byte layer 3 call message (VCALL / TX_REL), group call."""
    return bytes([msg & 0x3F, 0x00, 0x20, src >> 8, src & 0xFF, dst >> 8, dst & 0xFF, 0, 0, 0])


def sacch(structure, data18=0, ran=0):
    """4-byte SACCH: structure, RAN, 18 data bits, CRC-6."""
    buf = bytearray(4)
    buf[0] = ((structure & 3) << 6) | (ran & 0x3F)
    _write(buf, 8, 18, data18)
    _write(buf, 26, 6, crc6(buf))
    return bytes(buf)


def facch1(msg, src, dst):
    """12-byte FACCH1 block: layer 3 message + CRC-12 + 4 zero bits."""
    buf = bytearray(layer3(msg, src, dst) + bytes(2))
    _write(buf, 80, 12, crc12(buf))
    return bytes(buf)


def control_payload(msg, src, dst):
    """Header (VCALL) or trailer (TX_REL): FACCH1 in both blocks, idle
    single-structure SACCH."""
    out = bytearray(PAYLOAD_SIZE)
    out[0] = lich(RFCT_RDCH, FCT_SACCH_NS, OPTION_STEAL_FACCH)
    out[1:5] = sacch(0)
    block = facch1(msg, src, dst)
    out[5:17] = block
    out[19:31] = block
    return bytes(out)


def voice_payload(frames, src, dst, index):
    """Four 49-bit frames; the SACCH carries the VCALL message 18 bits at a
    time over four packets (structure 3, 2, 1, 0)."""
    if len(frames) != FRAMES_PER_PACKET:
        raise ValueError("an NXDN voice packet carries 4 frames")
    part = index % 4
    message = int.from_bytes(layer3(VCALL, src, dst)[:9], "big")
    data18 = (message >> (72 - 18 * (part + 1))) & 0x3FFFF
    out = bytearray(PAYLOAD_SIZE)
    out[0] = lich(RFCT_RDCH, FCT_SACCH_SS, OPTION_STEAL_NONE)
    out[1:5] = sacch(3 - part, data18)
    for slot, frame in enumerate(frames):
        base = (5 + 14 * (slot // 2)) * 8 + FRAME_BITS * (slot % 2)
        for bit in range(FRAME_BITS):
            _put(out, base + bit, _get(frame, bit))
    return bytes(out)


def voice_frames(payload):
    """The four 49-bit frames of a voice payload."""
    out = []
    for slot in range(FRAMES_PER_PACKET):
        base = (5 + 14 * (slot // 2)) * 8 + FRAME_BITS * (slot % 2)
        buf = bytearray(FRAME_BYTES)
        for bit in range(FRAME_BITS):
            _put(buf, bit, _get(payload, base + bit))
        out.append(bytes(buf))
    return out


def parse_facch1(block):
    """dict of a 12-byte FACCH1 block, crc_ok included."""
    return {"msg": block[0] & 0x3F, "group": bool(block[2] & 0x20),
            "src": (block[3] << 8) | block[4], "dst": (block[5] << 8) | block[6],
            "crc_ok": _read(block, 80, 12) == crc12(block)}


def parse_sacch(raw):
    return {"structure": raw[0] >> 6, "ran": raw[0] & 0x3F, "data": _read(raw, 8, 18),
            "crc_ok": _read(raw, 26, 6) == crc6(raw)}


# ------------------------------------------------------------------ packets

def _name(callsign):
    return callsign.upper().encode("ascii")[:10].ljust(10, b" ")


def poll_packet(callsign, tg):
    return b"NXDNP" + _name(callsign) + (tg & 0xFFFF).to_bytes(2, "big")


def unlink_packet(callsign, tg):
    return b"NXDNU" + _name(callsign) + (tg & 0xFFFF).to_bytes(2, "big")


def parse_poll(data):
    """dict of a 17-byte NXDNP/NXDNU packet, or None."""
    if len(data) != POLL_SIZE or data[:5] not in (b"NXDNP", b"NXDNU"):
        return None
    return {"kind": "poll" if data[4:5] == b"P" else "unlink",
            "callsign": data[5:15].decode("ascii", "replace").rstrip(),
            "tg": int.from_bytes(data[15:17], "big")}


def flags_for(payload, group=True):
    """Flags byte as NXDNGateway derives it from the payload: header or
    trailer when the LICH says FACCH1 in both blocks (0x81/0x83) and the
    message type is VCALL or TX_REL."""
    flags = GROUP if group else 0
    if payload[0] in (0x81, 0x83):
        flags |= HEADER if payload[5] == VCALL else 0
        flags |= TRAILER if payload[5] == TX_REL else 0
    elif payload[0] & 0xF0 == 0x90:
        flags |= DATA
    return flags


def packet(src, dst, payload, flags=None):
    if len(payload) != PAYLOAD_SIZE:
        raise ValueError("NXDN payload is 33 bytes")
    if flags is None:
        flags = flags_for(payload)
    return b"NXDND" + src.to_bytes(2, "big") + dst.to_bytes(2, "big") + bytes([flags]) + bytes(payload)


def parse(data):
    """dict of a 43-byte NXDND packet, or None. kind: header, trailer,
    voice or data (from the flags byte)."""
    if len(data) != PACKET_SIZE or not data.startswith(b"NXDND"):
        return None
    flags = data[9]
    if flags & DATA:
        kind = "data"
    elif flags & HEADER:
        kind = "header"
    elif flags & TRAILER:
        kind = "trailer"
    else:
        kind = "voice"
    return {"src": int.from_bytes(data[5:7], "big"), "dst": int.from_bytes(data[7:9], "big"),
            "flags": flags, "group": bool(flags & GROUP), "kind": kind, "payload": data[10:43]}


def is_voice(data):
    info = parse(data)
    return info is not None and info["kind"] == "voice"


def stream_packets(frames, src, dst):
    """Header, voice packets (frames padded with zeros to a multiple of
    four), trailer."""
    frames = list(frames) + [bytes(FRAME_BYTES)] * (-len(frames) % FRAMES_PER_PACKET)
    out = [packet(src, dst, control_payload(VCALL, src, dst))]
    for index in range(0, len(frames), FRAMES_PER_PACKET):
        out.append(packet(src, dst, voice_payload(frames[index:index + 4], src, dst, index // 4)))
    out.append(packet(src, dst, control_payload(TX_REL, src, dst)))
    return out


def wire_frames(frames):
    """Frames a listener receives for a talk of frames (padded to 4)."""
    return list(frames) + [bytes(FRAME_BYTES)] * (-len(frames) % FRAMES_PER_PACKET)


class NxdnClient:
    """A repeater/gateway on an NXDNReflector talkgroup."""

    def __init__(self, bind_ip, tg, nxdn_id=NXDN_ID, callsign=CALLSIGN):
        self.client = UdpClient(bind_ip)
        self.tg = tg
        self.id = nxdn_id
        self.callsign = callsign
        self.dest = None

    def link(self, node_ip, timeout=3.0, port=PORT):
        """Poll until the reflector echoes the poll back."""
        self.dest = (node_ip, port)
        poll = poll_packet(self.callsign, self.tg)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.send(self.dest, poll)
            self.client.pump(0.3)
            if poll in self.client.inbox:
                return True
        return False

    def keepalive(self):
        """NXDNGateway polls every 5 s; the reflector drops a repeater after
        120 s without poll or data."""
        self.client.send(self.dest, poll_packet(self.callsign, self.tg))

    def unlink(self):
        self.client.send(self.dest, unlink_packet(self.callsign, self.tg))
        self.client.pump(0.3)

    def talk(self, frames, settle=0.3, tail=2.0, gap=PACKET_GAP):
        """frames: 49-bit (7-byte) frames, four per packet every 80 ms."""
        packets = stream_packets(frames, self.id, self.tg)
        self.keepalive()
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        self.client.paced(self.dest, packets[1:], gap)
        self.client.pump(tail)

    def packets(self):
        """Parsed NXDND packets received so far, in arrival order."""
        return [p for p in (parse(d) for d in self.client.inbox) if p]

    def collect(self, settle=1.5):
        """49-bit frames of the received voice packets, in arrival order."""
        self.client.pump(settle)
        out = []
        for info in self.packets():
            if info["kind"] == "voice":
                out.extend(voice_frames(info["payload"]))
        return out

    def close(self):
        self.client.close()
