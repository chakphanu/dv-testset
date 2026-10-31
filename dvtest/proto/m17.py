# SPDX-License-Identifier: MIT
"""M17 client in xlxd reflector-client mode (UDP 17000). Two 8-byte
Codec2 3200 frames per 54-byte voice packet.

Conformance builders (docs/protocols/m17.md): crc16(), stream_packet()
with every LSD field settable and a real or forced CRC, parse_stream(),
the control packets of M17 Part II (CONN, LSTN, PING, PONG, DISC request
and acknowledgement, the 37-byte reflector CONN) with parse_control(),
the packet-mode packet m17p_packet() with parse_m17p(), and the
reflector-to-reflector forms (peer CONN / ACKN / PING / PONG / DISC, the
module trailer, classify(), parse_peer()) used by dvtest.m17_peer."""
import time

from ..net import UdpClient

PORT = 17000
ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/."
FRAMETYPE_VOICE = 0x05
PACKET_SIZE = 54
BROADCAST = b"\xff" * 6
# stream TYPE (Part I, Stream Mode TYPE field): bit 0 stream, data type
# bits 2-1 (01 data, 10 voice 3200, 11 voice + data 1600), encryption
# type bits 4-3, CAN bit 0 in bit 7 and bits 3-1 in byte 0 bits 2-0,
# signed stream byte 0 bit 3
TYPE_VOICE = 0x0005
TYPE_VOICE_DATA = 0x0007
TYPE_DATA = 0x0003


def encode_callsign(text):
    """Base-40 callsign, 6 bytes big endian."""
    cs = (text or "").upper()[:9]
    cs = cs + " " * (9 - len(cs))
    value = 0
    for ch in reversed(cs):
        index = ALPHABET.find(ch)
        value = value * 40 + (index if index >= 0 else 0)
    return value.to_bytes(6, "big")


def decode_callsign(data):
    value = int.from_bytes(data, "big")
    out = []
    while value:
        out.append(ALPHABET[value % 40])
        value //= 40
    return "".join(out).rstrip()


def packet(stream_id, seq, src_callsign, payload16, eot=False):
    out = bytearray(b"M17 ")
    out += stream_id.to_bytes(2, "big")
    out += b"\xff" * 6
    out += encode_callsign(src_callsign)
    out.append(0)
    out.append(FRAMETYPE_VOICE)
    out += bytes(14)
    value = (seq & 0x7FFF) | (0x8000 if eot else 0)
    out += value.to_bytes(2, "big")
    out += payload16
    out += bytes(2)
    return bytes(out)


def parse(data):
    """dict of voice packet fields, or None."""
    if len(data) != PACKET_SIZE or not data.startswith(b"M17 "):
        return None
    seq = int.from_bytes(data[34:36], "big")
    return {"stream_id": int.from_bytes(data[4:6], "big"), "dst": data[6:12],
            "src": decode_callsign(data[12:18]), "frametype": data[19],
            "seq": seq & 0x7FFF, "eot": bool(seq & 0x8000),
            "frames": [data[36:44], data[44:52]]}


def conn_packet(callsign, module):
    return b"CONN" + encode_callsign(callsign) + module.encode()


def disc_packet(callsign):
    return b"DISC" + encode_callsign(callsign)


def pong_packet(callsign):
    return b"PONG" + encode_callsign(callsign)


def voice_packets(frames8, stream_id, callsign):
    """Seq 0 repeats the first frame, then pairs, then an EOT packet that
    repeats the last payload."""
    packets = [packet(stream_id, 0, callsign, (frames8[0] * 2) if frames8 else bytes(16))]
    for index in range(0, len(frames8), 2):
        pair = frames8[index:index + 2]
        payload = pair[0] + (pair[1] if len(pair) > 1 else pair[0])
        packets.append(packet(stream_id, 1 + index // 2, callsign, payload))
    packets.append(packet(stream_id, len(packets), callsign, packets[-1][36:52], eot=True))
    return packets


class M17Client:
    def _keepalive(self, data, _addr):
        # xlxd PINGs every ~10 s and drops a client that does not answer
        # PONG + callsign.
        if data.startswith(b"PING"):
            return pong_packet(self.callsign)
        return None

    def __init__(self, bind_ip, callsign="N0CALL", module="B"):
        self.client = UdpClient(bind_ip, responder=self._keepalive)
        self.callsign = callsign
        self.module = module
        self.dest = None

    def link(self, node_ip, timeout=5.0, port=PORT):
        self.dest = (node_ip, port)
        self.client.send(self.dest, conn_packet(self.callsign, self.module))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.drain()
            for data in self.client.inbox:
                if data.startswith(b"ACKN"):
                    return True
                if data.startswith(b"NACK"):
                    return False
            time.sleep(0.05)
        return False

    def unlink(self):
        self.client.send(self.dest, disc_packet(self.callsign))
        self.client.pump(0.4)

    def talk(self, frames8, stream_id, settle=0.3, tail=2.0, gap=0.040):
        """frames8: sequence of 8-byte Codec2 3200 frames (two per packet)."""
        packets = voice_packets(frames8, stream_id, self.callsign)
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        # send times of the voice packets (packet 0 excluded), for latency
        self.last_stamps = self.client.paced(self.dest, packets[1:], gap)
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Codec2 8-byte frames from forwarded voice packets. Every packet
        carries two frames, including seq 0 and the EOT packet. Pump first:
        frames sit in the kernel buffer until the socket is drained."""
        self.client.pump(settle)
        out = []
        for data in self.client.inbox:
            if len(data) == PACKET_SIZE and data.startswith(b"M17 "):
                out.append(data[36:44])
                out.append(data[44:52])
        return out

    def close(self):
        self.client.close()


# ---------------------------------------------------------------- conformance builders

def crc16(data):
    """M17 CRC (Part I 2.6): polynomial 0x5935, initial 0xFFFF, MSB first,
    no reflection, no final xor."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x5935) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def stream_type(data_type=2, can=0, encryption=0, signed=False):
    """TYPE value of a stream: data_type 1 data, 2 voice 3200, 3 voice +
    data 1600; can 0..15; encryption 0 none, 1 scrambler, 2 AES."""
    low = 1 | (data_type << 1) | (encryption << 3) | ((can & 1) << 7)
    high = ((can >> 1) & 7) | (8 if signed else 0)
    return high << 8 | low


def address(value):
    """6-byte address: bytes as given, "@ALL" / "ALL" / None broadcast,
    else the base-40 callsign (up to 9 characters, spaces kept)."""
    if isinstance(value, bytes):
        return value
    if value in (None, "@ALL", "ALL"):
        return BROADCAST
    return encode_callsign(value)


def stream_packet(stream_id, fn, payload16, src="N0CALL", dst=None, type_=TYPE_VOICE, meta=bytes(14),
                  last=False, crc=True):
    """54-byte stream packet (Part II, Stream Mode Packets). crc True:
    CRC16 over bytes 0-51; False: 0x0000; an int: that value."""
    out = bytearray(b"M17 ")
    out += stream_id.to_bytes(2, "big")
    out += address(dst) + address(src)
    out += type_.to_bytes(2, "big") + bytes(meta).ljust(14, b"\0")[:14]
    out += ((fn & 0x7FFF) | (0x8000 if last else 0)).to_bytes(2, "big")
    out += bytes(payload16).ljust(16, b"\0")[:16]
    value = crc16(out) if crc is True else (0 if crc is False else crc)
    out += value.to_bytes(2, "big")
    return bytes(out)


def parse_stream(data):
    """dict of every field of a 54-byte stream packet (crc_ok: the CRC
    over bytes 0-51 matches), or None."""
    if len(data) != PACKET_SIZE or not data.startswith(b"M17 "):
        return None
    fn = int.from_bytes(data[34:36], "big")
    crc = int.from_bytes(data[52:54], "big")
    return {"stream_id": int.from_bytes(data[4:6], "big"), "dst_raw": data[6:12],
            "dst": "@ALL" if data[6:12] == BROADCAST else decode_callsign(data[6:12]),
            "src": decode_callsign(data[12:18]), "type": int.from_bytes(data[18:20], "big"),
            "meta": data[20:34], "fn": fn & 0x7FFF, "last": bool(fn & 0x8000),
            "payload": data[36:52], "crc": crc, "crc_ok": crc == crc16(data[:52])}


def lstn_packet(callsign, module):
    return b"LSTN" + encode_callsign(callsign) + module.encode()


def ping_packet(callsign):
    return b"PING" + encode_callsign(callsign)


DISC_ACK = b"DISC"


def peer_conn_packet(designation, modules):
    """37-byte reflector-to-reflector CONN: designation + module list
    padded with NUL bytes."""
    return b"CONN" + encode_callsign(designation) + modules.encode().ljust(27, b"\0")[:27]


def parse_control(data):
    """{"tag", "size", "callsign", "module"} of a control packet (CONN,
    LSTN, ACKN, NACK, PING, PONG, DISC), or None."""
    tag = data[:4].decode("ascii", "replace")
    if tag not in ("CONN", "LSTN", "ACKN", "NACK", "PING", "PONG", "DISC"):
        return None
    return {"tag": tag, "size": len(data),
            "callsign": decode_callsign(data[4:10]) if len(data) >= 10 else None,
            "module": chr(data[10]) if len(data) == 11 else None}


def _crc_pair(crc, lsf, payload):
    """(LSF CRC, payload CRC) for the crc argument of m17p_packet: True =
    both valid, False = both 0x0000, (lsf, payload) = each None (valid) or
    an int."""
    if crc is True:
        return lsf, payload
    if crc is False:
        return 0, 0
    pair = tuple(crc)
    return (lsf if pair[0] is None else pair[0]), (payload if pair[1] is None else pair[1])


def m17p_packet(protocol, data, src="N0CALL", dst=None, can=0, type_=None, crc=True, meta=bytes(14)):
    """Packet-mode IP packet (Part II, Packet Mode IP Packet): "M17P",
    LSF (LSD + CRC16), then the type specifier, the user data and the
    payload CRC16 over both. protocol: the specifier, an int (one byte) or
    bytes (1..4, e.g. b"\xc2\x80"). type_: the 16-bit TYPE field, default
    the legacy layout from can (stream bit clear); see M17P_TYPES. crc:
    True both valid, False both 0x0000, or (lsf, payload) where None is
    valid and an int forces that value."""
    if type_ is None:
        type_ = ((can >> 1) & 7) << 8 | (can & 1) << 7          # packet mode: bit 0 clear
    lsd = address(dst) + address(src) + type_.to_bytes(2, "big") + bytes(meta).ljust(14, b"\0")[:14]
    body = (bytes([protocol]) if isinstance(protocol, int) else bytes(protocol)) + bytes(data)
    lsf_crc, payload_crc = _crc_pair(crc, crc16(lsd), crc16(body))
    return b"M17P" + lsd + lsf_crc.to_bytes(2, "big") + body + payload_crc.to_bytes(2, "big")


M17P_HEADER = 34            # "M17P" + LSD (28) + LSF CRC (2)


def m17p_size(data_len, specifier_len=1):
    """Size of an M17P datagram: 34 + len(specifier + data) + 2 (Part II
    allows a payload of 4..825 bytes, so 38..859 bytes)."""
    return M17P_HEADER + specifier_len + data_len + 2


def parse_m17p(data, trailer=None):
    """Fields of an M17P datagram, or None (not M17P or shorter than 38 B).
    trailer: True = the last byte is a module letter (reflector to reflector,
    n+1 bytes), False = none, None = decide: a trailer when the payload CRC
    is valid only without the last byte. Keys: size (as given), trailer
    (the letter or None), dst_raw, dst, src, type, meta, lsf_crc_ok, payload
    (specifier + data), payload_crc, payload_crc_ok."""
    if len(data) < 38 or not data.startswith(b"M17P"):
        return None
    letter = None

    def fields(body):
        crc = int.from_bytes(body[-2:], "big")
        return body[34:-2], crc, crc == crc16(body[34:-2])

    if trailer is None:
        trailer = not fields(data)[2] and fields(data[:-1])[2] and len(data) >= 39
    body = data
    if trailer:
        letter, body = chr(data[-1]) if 0x20 <= data[-1] < 0x7F else data[-1], data[:-1]
    payload, crc, crc_ok = fields(body)
    lsf = int.from_bytes(body[32:34], "big")
    return {"size": len(data), "trailer": letter, "dst_raw": body[4:10],
            "dst": "@ALL" if body[4:10] == BROADCAST else decode_callsign(body[4:10]),
            "src": decode_callsign(body[10:16]), "type": int.from_bytes(body[16:18], "big"),
            "meta": body[18:32], "lsf_crc_ok": lsf == crc16(body[4:32]),
            "payload": payload, "payload_crc": crc, "payload_crc_ok": crc_ok}


# M17P TYPE values by class (docs/protocols/m17.md stage 2; the legacy / v3
# split is the DV-Router layout rule, Part I only fixes the P/S bit and CAN)
M17P_TYPES = {
    "legacy": (0x0000, 0x0080, 0x0280),             # top nibble 0, stream bit 0, bits 3..4 0
    "v3": (0xF000, 0xF005, 0xF030),                 # nibble 0xF, bits 11..9 0
    "legacy_encrypted": (0x0008, 0x0010),           # bits 3..4 non-zero
    "v3_encrypted": (0xF200, 0xF800),               # bits 11..9 non-zero
    "stream_bit": (0x0001, 0xF001),                 # invalid in M17P
    "other": (0x1000,),                             # top nibble neither 0 nor 0xF
}
M17P_TYPE_LEGACY, M17P_TYPE_V3 = 0x0000, 0xF000
M17P_TYPE_LEGACY_ENCRYPTED, M17P_TYPE_V3_ENCRYPTED = 0x0008, 0xF200
M17P_TYPE_STREAM_BIT, M17P_TYPE_OTHER = 0x0001, 0x1000


# ---------------------------------------------------------------- reflector to reflector (stage 2)

PEER_CONN_SIZE = 37
PEER_CONTROL_SIZE = 10
STREAM_TRAILER_SIZE = PACKET_SIZE + 1       # 55


def peer_ackn_packet(designation, modules):
    """37-byte reflector ACKN: the answering reflector's designation and the
    linked modules, NUL padded."""
    return b"ACKN" + encode_callsign(designation) + modules.encode().ljust(27, b"\0")[:27]


def peer_ping_packet(designation):
    return b"PING" + encode_callsign(designation)


def peer_pong_packet(designation):
    return b"PONG" + encode_callsign(designation)


def peer_disc_packet(designation):
    return b"DISC" + encode_callsign(designation)


def with_trailer(packet_bytes, letter):
    """The datagram sent between reflectors: the body plus the module letter
    of the originating module (55 B stream, n+1 B M17P). letter: "M" or an
    int byte value."""
    return bytes(packet_bytes) + (bytes([letter]) if isinstance(letter, int) else letter.encode())


def strip_trailer(packet_bytes):
    """(body, letter): the datagram without its last byte and that byte as
    a one-character string (a non-printable byte stays an int); (data, None)
    for an empty datagram."""
    if not packet_bytes:
        return packet_bytes, None
    last = packet_bytes[-1]
    return bytes(packet_bytes[:-1]), (chr(last) if 0x20 <= last < 0x7F else last)


def classify(data):
    """Short name of a datagram between a tester and a reflector: CONN37,
    CONN11 (client), ACKN37, ACKN4, NACK4, NACK10, PING10, PONG10, DISC10,
    DISC4, LSTN11, STREAM54, STREAM55, M17P (size not part of the name), or
    other (also a known tag with another size)."""
    size = len(data)
    tag = bytes(data[:4])
    if tag == b"M17 " and size in (PACKET_SIZE, STREAM_TRAILER_SIZE):
        return "STREAM%d" % size
    if tag == b"M17P" and size >= 38:
        return "M17P"
    known = {b"CONN": (37, 11), b"ACKN": (37, 4), b"NACK": (4, 10), b"PING": (10,), b"PONG": (10,),
             b"DISC": (10, 4), b"LSTN": (11,)}
    if size in known.get(tag, ()):
        return "%s%d" % (tag.decode(), size)
    return "other"


def kind_matches(kind, want):
    """kind (classify()) against want: None matches all, a string matches
    by prefix ("STREAM" both sizes, "PING" PING10), a tuple/list/set any of
    its entries, a callable is called with the kind."""
    if want is None:
        return True
    if callable(want):
        return bool(want(kind))
    if isinstance(want, str):
        return kind.startswith(want)
    return any(kind.startswith(item) for item in want)


def parse_peer(data):
    """Fields of a 37 B reflector CONN / ACKN or a 10 B PING / PONG / DISC /
    NACK, or None. Keys: tag, size, designation, modules (the NUL
    terminated list of a 37 B packet, '' for 10 B), nul_ok (a NUL ends the
    list)."""
    tag = bytes(data[:4]).decode("ascii", "replace")
    if len(data) == PEER_CONN_SIZE and tag in ("CONN", "ACKN"):
        raw = bytes(data[10:37])
        end = raw.find(b"\0")
        return {"tag": tag, "size": 37, "designation": decode_callsign(data[4:10]),
                "modules": raw[:end if end >= 0 else None].decode("ascii", "replace"), "nul_ok": end >= 0}
    if len(data) == PEER_CONTROL_SIZE and tag in ("PING", "PONG", "DISC", "NACK"):
        return {"tag": tag, "size": 10, "designation": decode_callsign(data[4:10]), "modules": "", "nul_ok": True}
    return None
