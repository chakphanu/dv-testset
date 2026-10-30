# SPDX-License-Identifier: MIT
"""USRP (app_rpt chan_usrp) framing: "USRP" + 7 x BE32 (seq, memory,
keyup, talkgroup, type, mpxid, reserved) = 32 B header. Voice (type 0):
keyed = header + 320 B S16LE 8 kHz PCM (352 B), unkey = header only.

Non-voice types (DVSwitch extension, docs/protocols/usrp.md): every
implementation that sends a non-zero type writes it in host order, i.e.
little endian at bytes 20-23, while seq and keyup stay big endian; the
*_le builders and type_le() follow that. TLV = 1-byte tag, 1-byte length,
value; SET_INFO (tag 8) travels in a TEXT frame."""
import struct
from collections import namedtuple

HEADER_SIZE = 32
PCM_SIZE = 320
VOICE_SIZE = HEADER_SIZE + PCM_SIZE
TYPE_VOICE = 0
TYPE_DTMF = 1
TYPE_TEXT = 2
TYPE_PING = 3
TYPE_TLV = 4
TYPE_VOICE_ADPCM = 5
TYPE_VOICE_ULAW = 6

# TLV tags (DVSwitch)
TLV_BEGIN_TX = 0
TLV_AMBE = 1
TLV_END_TX = 2
TLV_TG_TUNE = 3
TLV_REMOTE_CMD = 5
TLV_SET_INFO = 8

Frame = namedtuple("Frame", "seq memory keyup talkgroup type mpxid reserved payload")


def header(seq, keyup, talkgroup=0, kind=TYPE_VOICE, memory=0, mpxid=0, reserved=0):
    return b"USRP" + struct.pack(">IIIIIII", seq & 0xFFFFFFFF, memory, keyup,
                                 talkgroup, kind, mpxid, reserved)


def voice(seq, pcm):
    if len(pcm) != PCM_SIZE:
        raise ValueError("USRP voice carries 320 bytes of PCM")
    return header(seq, 1) + pcm


def unkey(seq):
    return header(seq, 0)


def frame(seq, kind, payload, keyup=0):
    """Any frame type, e.g. a TLV/text frame a voice-only peer must drop."""
    return header(seq, keyup, kind=kind) + payload


def parse(data):
    if len(data) < HEADER_SIZE or not data.startswith(b"USRP"):
        return None
    return Frame(*struct.unpack(">IIIIIII", data[4:HEADER_SIZE]), data[HEADER_SIZE:])


def is_voice(data):
    """Strict voice check: all fields but seq zero, keyup 0/1, size 352
    when keyed and 32 when not."""
    f = parse(data)
    if f is None or len(data) not in (HEADER_SIZE, VOICE_SIZE):
        return False
    if (f.memory, f.talkgroup, f.type, f.mpxid, f.reserved) != (0, 0, 0, 0, 0) or f.keyup > 1:
        return False
    return (f.keyup == 1) == (len(data) == VOICE_SIZE)


def header_le(seq, keyup, kind):
    """Header with the type in host (little endian) order, as chan_usrp,
    Analog_Bridge, pyUC, MMDVM_CM and URFd write a non-zero type."""
    return b"USRP" + struct.pack(">IIII", seq & 0xFFFFFFFF, 0, keyup, 0) + struct.pack("<I", kind) + bytes(8)


def frame_le(seq, kind, payload, keyup=0, pad=False):
    """Non-voice frame with a little-endian type; pad=True fills the
    payload to 320 bytes (352-byte datagram) as MMDVM_CM / URFd do."""
    if pad:
        payload = payload + bytes(max(0, PCM_SIZE - len(payload)))
    return header_le(seq, keyup, kind) + payload


def type_le(data):
    """The type field read in host (little endian) order."""
    return int.from_bytes(data[20:24], "little") if len(data) >= HEADER_SIZE else None


def tlv(tag, value):
    if len(value) > 255:
        raise ValueError("TLV value longer than 255 bytes")
    return bytes([tag, len(value)]) + value


def parse_tlv(data):
    """(tag, value) of one TLV at the start of data, or None."""
    if len(data) < 2 or len(data) < 2 + data[1]:
        return None
    return data[0], data[2:2 + data[1]]


def set_info(callsign, src_id=0, rpt_id=0, tg=0, slot=0, cc=0):
    """SET_INFO TLV: source id (3 B BE), repeater id (4 B BE), TG (3 B BE),
    slot, colour code, callsign as a NUL-terminated string."""
    value = (src_id.to_bytes(3, "big") + rpt_id.to_bytes(4, "big") + tg.to_bytes(3, "big")
             + bytes([slot, cc]) + callsign.encode() + b"\0")
    return tlv(TLV_SET_INFO, value)


def parse_set_info(data):
    """dict of a SET_INFO TLV (the payload of a TEXT frame), or None."""
    item = parse_tlv(data)
    if item is None or item[0] != TLV_SET_INFO or len(item[1]) < 12:
        return None
    value = item[1]
    return {"src_id": int.from_bytes(value[0:3], "big"), "rpt_id": int.from_bytes(value[3:7], "big"),
            "tg": int.from_bytes(value[7:10], "big"), "slot": value[10], "cc": value[11],
            "callsign": value[12:].split(b"\0", 1)[0].decode(errors="replace")}


def text_set_info(seq, callsign, pad=True, **ids):
    """TEXT frame carrying SET_INFO, padded to 352 B by default (MMDVM_CM, URFd)."""
    return frame_le(seq, TYPE_TEXT, set_info(callsign, **ids), pad=pad)


def ping(seq, pad=False):
    """PING (type 3, payload "PING"): pyUC sends it unpadded as a NAT keepalive."""
    return frame_le(seq, TYPE_PING, b"PING", pad=pad)
