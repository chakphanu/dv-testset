# SPDX-License-Identifier: MIT
"""USRP (app_rpt chan_usrp) framing: "USRP" + 7 x BE32 (seq, memory,
keyup, talkgroup, type, mpxid, reserved) = 32 B header. Voice (type 0):
keyed = header + 320 B S16LE 8 kHz PCM (352 B), unkey = header only."""
import struct
from collections import namedtuple

HEADER_SIZE = 32
PCM_SIZE = 320
VOICE_SIZE = HEADER_SIZE + PCM_SIZE
TYPE_VOICE = 0
TYPE_DTMF = 1
TYPE_TEXT = 2

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
