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


# ------------------------------------------------- conformance builders
# (docs/protocols/dplus.md). Offsets are into the UDP payload.

JARL_END = b"\x55\x55\x55\x55\xc8\x7a"          # radio last frame, JARL STD 6.0a 4.1.2 g-h
NULL_AMBE = bytes.fromhex("9e8d3288261a3f61e8")  # D-STAR silence frame
SYNC = dextra.SYNC_DATA
LOGIN_TAG = b"\x1c\xc0\x04\x00"
REPLY_TAG = b"\x08\xc0\x04\x00"
BAND = b"\x00\x01\x02"


def crc_ccitt(data):
    """D-STAR header P_FCS: CRC-CCITT x^16 + x^12 + x^5 + 1, bit-reversed
    (0x8408), preset 0xFFFF, result inverted (the X.25 form); sent low
    byte first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _field(text, size):
    if isinstance(text, str):
        text = text.encode()
    return (text + b" " * size)[:size]


def radio_header(rpt2, rpt1, my, ur="CQCQCQ", suffix="    ", flags=b"\x00\x00\x00", crc=True):
    """41 B radio header: flags(3), RPT2, RPT1, UR, MY (8 each), suffix(4),
    P_FCS. crc=True computes it, an int is written as is (low byte first)."""
    body = bytes(flags) + _field(rpt2, 8) + _field(rpt1, 8) + _field(ur, 8) + _field(my, 8) \
        + _field(suffix, 4)
    value = crc_ccitt(body) if crc is True else crc
    return body + value.to_bytes(2, "little")


def header_packet(stream_id, radio41, band=BAND):
    """58 B header: 3A 80, DSVT 10 00 00 00 20, band(3), stream id LE, 80, radio header."""
    return b"\x3a\x80DSVT\x10\x00\x00\x00\x20" + bytes(band) + stream_id.to_bytes(2, "little") \
        + b"\x80" + radio41


def end_frame(stream_id, seq, ambe=NULL_AMBE, tail=JARL_END, byte0=0x20, byte8=0x00, band=BAND):
    """32 B end of stream in the client forms: AMBE(9) + tail(6) after the
    seq byte (seq mod 21 with 0x40). ircDDBGateway: NULL_AMBE + JARL_END;
    DroidStar: zero AMBE + JARL_END. last_voice() is xlxd's own form."""
    out = bytearray([byte0, 0x80]) + b"DSVT\x20\x00" + bytes([byte8]) + b"\x00\x20" + bytes(band)
    out += stream_id.to_bytes(2, "little")
    out.append((seq % 21) | 0x40)
    out += bytes(ambe) + bytes(tail)
    return bytes(out)


def login_irc(callsign, client_id=b"DV019999"):
    """ircDDBGateway / DStarGateway: callsign NUL padded in a 16 B field."""
    return login_packet(callsign)[:20] + bytes(client_id)


def login_qnet(callsign):
    """QnetGateway: 8 characters, trailing spaces as NUL, then DV019999."""
    cs = _field(callsign, 8).rstrip(b" ")
    return LOGIN_TAG + (cs + bytes(16))[:16] + b"DV019999"


def login_droid(callsign, digits=123456):
    """DroidStar: 6 characters of the callsign, NULs to byte 20, then
    "HS" + 6 digits."""
    return LOGIN_TAG + (_field(callsign, 6) + bytes(10)) + b"HS%06d" % digits


def parse(data):
    """dict(kind=...) for any DPlus packet: connect, disconnect, keepalive,
    login, reply (text), header (fields, crc_ok), voice, end; anything
    else is kind "unknown" with its size."""
    if data == CONNECT:
        return {"kind": "connect"}
    if data == DISCONNECT:
        return {"kind": "disconnect"}
    if data == KEEPALIVE:
        return {"kind": "keepalive"}
    if len(data) == 28 and data[:4] == LOGIN_TAG:
        return {"kind": "login", "callsign": data[4:20].rstrip(b"\x00 "), "client": data[20:28]}
    if len(data) == 8 and data[:4] == REPLY_TAG:
        return {"kind": "reply", "text": data[4:8].decode("latin-1")}
    if len(data) == HEADER_SIZE and data[:2] == b"\x3a\x80" and data[2:6] == b"DSVT" and data[6] == 0x10:
        radio = data[17:58]
        crc = int.from_bytes(radio[39:41], "little")
        return {"kind": "header", "stream_id": int.from_bytes(data[14:16], "little"),
                "band": data[11:14], "flags": radio[0:3], "rpt2": radio[3:11], "rpt1": radio[11:19],
                "ur": radio[19:27], "my": radio[27:35], "suffix": radio[35:39], "crc": crc,
                "crc_ok": crc == crc_ccitt(radio[:39])}
    voice_ = parse_voice(data)
    if voice_:
        out = {"kind": "end" if voice_[2] else "voice", "stream_id": voice_[0], "seq": voice_[1],
               "frame": voice_[3], "slow": data[26:29]}
        if voice_[2]:
            out.update(byte0=data[0], byte8=data[8], tail=data[17:32])
        return out
    return {"kind": "unknown", "size": len(data)}


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
