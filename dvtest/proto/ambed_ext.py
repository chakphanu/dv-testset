# SPDX-License-Identifier: MIT
"""Extended AMBEd datagrams of DV-Router and the fake transcoder transform.

Written from the DV-Router design text (transcoding.md, "AMBEd datagrams",
"Codec ids and unit widths", "Media", "Test transform"), independent of the
legacy module dvtest/proto/ambed.py (which keeps the old echo fake).

Control datagrams (worker <-> transcoder, port 10100 at both ends). Offsets
are zero based. Stream id and data port are u16 little endian; the CAPS mask
and the transaction are u32 big endian. A datagram is recognised by its tag
AND its exact length; everything else is not a datagram of this protocol.

  PING       17  0-8 AMBEDPING, 9-16 callsign (8, space padded)
  PONG        9  0-8 AMBEDPONG
  CAPS req    9  0-8 AMBEDCAPS
  CAPS reply 15  0-8 AMBEDCAPS, 9 version, 10 max outputs, 11-14 mask BE32
  OPEN       24  0-6 AMBEDOS, 7-14 callsign, 15 codec in, 16-19 four output
                 slots (unused 0), 20-23 transaction BE32
  STD        18  0-7 AMBEDSTD, 8-9 stream id, 10-11 data port, 12 codec in,
                 13 first output codec, 14-17 transaction BE32
  BUSY       13  0-8 AMBEDBUSY, 9-12 transaction BE32
  CS          9  0-6 AMBEDCS, 7-8 stream id

Data (per-stream port, same number at both ends):
  request  codec_in u8, pid u8, frame (unit width of codec_in)
  reply    out1 u8, pid u8, frame1, out2 u8, frame2, ...   (OPEN order)

Fake transform: out[i] = in[i mod len(in)] ^ K(src, dst) ^ (i & 0xFF) for
0 <= i < width(dst). K(1,2)=0x02, K(2,1)=0x03, else
((src*7 + dst*13 + 1) & 0xFF) | 0x04.
"""
import struct

PORT = 10100

AMBE = 1
AMBE2 = 2
CODEC2 = 3
IMBE = 4
PCM = 5
CODEC_IDS = (AMBE, AMBE2, CODEC2, IMBE, PCM)

# unit bytes of one 20 ms frame, by codec id
WIDTH = {AMBE: 9, AMBE2: 9, CODEC2: 8, IMBE: 11, PCM: 320}
MASK_ALL = 0x3E          # bits 1..5
MAX_OUTPUTS = 4
CAPS_VERSION = 1

TAG_PING = b"AMBEDPING"
TAG_PONG = b"AMBEDPONG"
TAG_CAPS = b"AMBEDCAPS"
TAG_OPEN = b"AMBEDOS"
TAG_STD = b"AMBEDSTD"
TAG_BUSY = b"AMBEDBUSY"
TAG_CS = b"AMBEDCS"

LEN_PING = 17
LEN_PONG = 9
LEN_CAPS_REQUEST = 9
LEN_CAPS_REPLY = 15
LEN_OPEN = 24
LEN_STD = 18
LEN_BUSY = 13
LEN_CS = 9

# K(src, dst) as printed in the design text; rows src, columns dst
K_TABLE = {
    1: {1: 0x15, 2: 0x02, 3: 0x2F, 4: 0x3C, 5: 0x4D},
    2: {1: 0x03, 2: 0x2D, 3: 0x36, 4: 0x47, 5: 0x54},
    3: {1: 0x27, 2: 0x34, 3: 0x3D, 4: 0x4E, 5: 0x57},
    4: {1: 0x2E, 2: 0x37, 3: 0x44, 4: 0x55, 5: 0x5E},
    5: {1: 0x35, 2: 0x3E, 3: 0x4F, 4: 0x5C, 5: 0x65},
}


def _check_codec(codec):
    if codec not in WIDTH:
        raise ValueError("unknown codec id %r" % (codec,))


def mask_of(codecs):
    """CAPS mask with bit n set for every codec id n."""
    mask = 0
    for codec in codecs:
        _check_codec(codec)
        mask |= 1 << codec
    return mask


def codecs_of(mask):
    """Codec ids in the low byte of a CAPS mask (bits 1..5), ascending."""
    return [c for c in CODEC_IDS if mask & (1 << c)]


def callsign8(text):
    """8 bytes: cut or space padded; bytes that are not printable ASCII
    become a space. No case conversion (the worker upper-cases itself)."""
    if isinstance(text, str):
        text = text.encode("latin-1", "replace")
    text = bytes(b if 0x20 <= b <= 0x7E else 0x20 for b in text)
    return (text + b" " * 8)[:8]


# ---- PING / PONG ------------------------------------------------------------

def ping(callsign):
    return TAG_PING + callsign8(callsign)


def parse_ping(data):
    """The 8 callsign bytes of a PING, or None."""
    if len(data) == LEN_PING and data.startswith(TAG_PING):
        return bytes(data[9:17])
    return None


def pong():
    return TAG_PONG


def is_pong(data):
    return bytes(data) == TAG_PONG


# ---- CAPS -------------------------------------------------------------------

def caps_request():
    return TAG_CAPS


def is_caps_request(data):
    return bytes(data) == TAG_CAPS


def caps_reply(max_outputs=MAX_OUTPUTS, mask=MASK_ALL, version=CAPS_VERSION):
    return TAG_CAPS + bytes([version, max_outputs]) + struct.pack(">I", mask)


def parse_caps_reply(data):
    """(version, max_outputs, mask) of a 15 B CAPS reply, or None. The
    values are not validated, see caps_reply_valid."""
    if len(data) == LEN_CAPS_REPLY and data.startswith(TAG_CAPS):
        return data[9], data[10], struct.unpack(">I", data[11:15])[0]
    return None


def caps_reply_valid(version, max_outputs, mask):
    """The worker's acceptance rule: version 1, max outputs 1..4, and the low
    byte of the mask has at least one of bits 1..5 (bits 0 and 6..31 are
    ignored)."""
    return version == 1 and 1 <= max_outputs <= 4 and (mask & 0x3E) != 0


# ---- OPEN / STD / BUSY / CS -------------------------------------------------

def open_request(callsign, codec_in, outs, transaction):
    """24 B OPEN. outs is the ascending output list (1..4 ids); unused slots
    are zero. Builders do not enforce ordering so tests can send bad opens."""
    outs = list(outs)
    if not 0 <= len(outs) <= 4:
        raise ValueError("an OPEN has four output slots")
    outs += [0] * (4 - len(outs))
    return (TAG_OPEN + callsign8(callsign) + bytes([codec_in, *outs])
            + struct.pack(">I", transaction))


def parse_open(data):
    """(callsign, codec_in, outs, transaction) of a 24 B OPEN, or None.
    outs lists the non-zero slots in slot order."""
    if len(data) != LEN_OPEN or not data.startswith(TAG_OPEN):
        return None
    outs = tuple(b for b in data[16:20] if b != 0)
    return bytes(data[7:15]), data[15], outs, struct.unpack(">I", data[20:24])[0]


def std(stream_id, port, codec_in, first_out, transaction):
    return (TAG_STD + struct.pack("<HH", stream_id, port)
            + bytes([codec_in, first_out]) + struct.pack(">I", transaction))


def parse_std(data):
    """(stream_id, port, codec_in, first_out, transaction) of an 18 B STD."""
    if len(data) != LEN_STD or not data.startswith(TAG_STD):
        return None
    stream_id, port = struct.unpack("<HH", data[8:12])
    return stream_id, port, data[12], data[13], struct.unpack(">I", data[14:18])[0]


def busy(transaction):
    return TAG_BUSY + struct.pack(">I", transaction)


def parse_busy(data):
    """The transaction of a 13 B BUSY, or None."""
    if len(data) != LEN_BUSY or not data.startswith(TAG_BUSY):
        return None
    return struct.unpack(">I", data[9:13])[0]


def cs(stream_id):
    return TAG_CS + struct.pack("<H", stream_id)


def parse_cs(data):
    """The stream id of a 9 B CS, or None."""
    if len(data) != LEN_CS or not data.startswith(TAG_CS):
        return None
    return struct.unpack("<H", data[7:9])[0]


def kind_of(data):
    """Name of the datagram on the control port, or 'unknown'. Tag plus exact
    length; the 9 B echo of the CAPS request is a 'caps_request'."""
    data = bytes(data)
    if parse_ping(data) is not None:
        return "ping"
    if data == TAG_PONG:
        return "pong"
    if data == TAG_CAPS:
        return "caps_request"
    if parse_caps_reply(data) is not None:
        return "caps_reply"
    if parse_open(data) is not None:
        return "open"
    if parse_std(data) is not None:
        return "std"
    if parse_busy(data) is not None:
        return "busy"
    if parse_cs(data) is not None:
        return "cs"
    return "unknown"


# ---- data -------------------------------------------------------------------

def data_request(codec_in, pid, frame):
    """codec_in, pid, frame; the frame must have the exact unit width."""
    _check_codec(codec_in)
    if len(frame) != WIDTH[codec_in]:
        raise ValueError("codec %d frame must be %d bytes" % (codec_in, WIDTH[codec_in]))
    return bytes([codec_in, pid & 0xFF]) + bytes(frame)


def parse_data_request(data):
    """(codec_in, pid, frame) when the codec id is known and the length is
    exactly 2 + unit width, else None."""
    if len(data) < 2 or data[0] not in WIDTH or len(data) != 2 + WIDTH[data[0]]:
        return None
    return data[0], data[1], bytes(data[2:])


def data_reply(pid, entries):
    """Reply datagram from [(codec, frame), ...] in OPEN order. The pid sits
    once, after the first codec byte."""
    out = bytearray()
    for index, (codec, frame) in enumerate(entries):
        _check_codec(codec)
        if len(frame) != WIDTH[codec]:
            raise ValueError("codec %d frame must be %d bytes" % (codec, WIDTH[codec]))
        out.append(codec)
        if index == 0:
            out.append(pid & 0xFF)
        out += frame
    return bytes(out)


def reply_size(outs):
    """Bytes of a complete reply for the OPEN-order output list."""
    return 1 + sum(1 + WIDTH[c] for c in outs)


def parse_data_reply(data, outs, allow_cut=False):
    """(pid, [(codec, frame), ...]) of a reply to a stream opened with outs.
    Raises ValueError when the first byte is not outs[0], a later codec byte
    is out of OPEN order, a frame has the wrong width, there are more entries
    than outputs or bytes follow the last frame. With allow_cut a reply may
    end after any complete entry (the missing outputs are absent)."""
    outs = list(outs)
    if len(data) < 2 or not outs or data[0] != outs[0]:
        raise ValueError("first byte is not the first output codec")
    pid = data[1]
    entries = []
    offset = 2
    for index, codec in enumerate(outs):
        if index > 0:
            if offset >= len(data):
                break
            if data[offset] != codec:
                raise ValueError("entry %d is codec %d, expected %d" % (index, data[offset], codec))
            offset += 1
        end = offset + WIDTH[codec]
        if end > len(data):
            raise ValueError("entry %d is cut inside its frame" % index)
        entries.append((codec, bytes(data[offset:end])))
        offset = end
    if offset != len(data):
        raise ValueError("%d bytes follow the last frame" % (len(data) - offset))
    if len(entries) != len(outs) and not allow_cut:
        raise ValueError("reply has %d of %d entries" % (len(entries), len(outs)))
    return pid, entries


# ---- transform --------------------------------------------------------------

def key(src, dst):
    """K(src, dst) of the fake transform."""
    _check_codec(src)
    _check_codec(dst)
    if (src, dst) == (AMBE, AMBE2):
        return 0x02
    if (src, dst) == (AMBE2, AMBE):
        return 0x03
    return ((src * 7 + dst * 13 + 1) & 0xFF) | 0x04


def transform(src, dst, frame):
    """out[i] = frame[i mod len(frame)] ^ K ^ (i & 0xFF), i < width(dst).
    The received length controls cycling (it must be non-zero); it is
    normally width(src) but any non-zero length is accepted here."""
    k = key(src, dst)
    w_in = len(frame)
    if w_in == 0:
        raise ValueError("empty frame")
    return bytes(frame[i % w_in] ^ k ^ (i & 0xFF) for i in range(WIDTH[dst]))


def expected_output(src, dst, frame):
    """The frame the fake returns for one input frame of codec src."""
    return transform(src, dst, frame)


def expected_reply(src, outs, pid, frame):
    """The complete data reply for a request (src, pid, frame) on a stream
    opened with the OPEN-order output list outs."""
    return data_reply(pid, [(dst, expected_output(src, dst, frame)) for dst in outs])
