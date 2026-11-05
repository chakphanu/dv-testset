# SPDX-License-Identifier: MIT
"""IPSC (Motorola MOTOTRBO IP Site Connect) as seen on the wire: encoders and
decoders for the link-maintenance packets and the group / private voice
packet, the HMAC-SHA1 packet authentication, and a simulated repeater (peer)
that registers with a master and sends calls. Stdlib only, no socket in the
packet layer.

The layouts come from public reverse-engineering notes (see docs/ipsc-phase0.md
for the table with the source file and function of every field); nothing is
copied from them. IPSC is not an open standard: a field this module cannot
back with a source is named "unknown" and passed through, never guessed.

Packet map (big endian; every packet carries the 10 byte HMAC-SHA1 digest at
the end when the system is authenticated):

  0x90 MASTER_REG_REQ    id(4) mode(1) flags(4) version(4)             14 B
  0x91 MASTER_REG_REPLY  id(4) mode(1) flags(4) peers(2) version(4)    16 B
  0x92 PEER_LIST_REQ     id(4)                                          5 B
  0x93 PEER_LIST_REPLY   id(4) length(2) { id(4) ip(4) port(2) mode(1) }*
  0x94 / 0x95 PEER_REG_REQ / REPLY    id(4) version(4)                  9 B
  0x96 MASTER_ALIVE_REQ  as 0x90                                       14 B
  0x97 MASTER_ALIVE_REPLY id(4) mode(1) flags(4) version(4)            14 B
  0x98 / 0x99 PEER_ALIVE_REQ / REPLY  id(4) mode(1) flags(4)           10 B
  0x9A / 0x9B DE_REG_REQ / REPLY      id(4)                             5 B
  0x80 / 0x81 GROUP_VOICE / PVT_VOICE 30 B header + RTP payload (burst type at 30)

The voice header is: peer id (1..4), call stream id (5), source subscriber
(6..8), destination (9..11), call type (12), call control (13..16), call info
(17: 0x20 = slot 2, 0x40 = call end), RTP version byte (18), RTP payload type
and marker (19), RTP sequence (20..21), RTP timestamp (22..25), RTP SSRC
(26..29), burst type (30).
"""
import hashlib
import hmac
import socket
import struct
import time

from . import dmr, nxdn

# ------------------------------------------------------------------ opcodes
CALL_CONFIRMATION = 0x05
TXT_MESSAGE_ACK = 0x54
CALL_MON_STATUS = 0x61
CALL_MON_RPT = 0x62
CALL_MON_NACK = 0x63
XCMP_XNL = 0x70
GROUP_VOICE = 0x80
PVT_VOICE = 0x81
GROUP_DATA = 0x83
PVT_DATA = 0x84
RPT_WAKE_UP = 0x85
CALL_INTERRUPT_REQ = 0x86
MASTER_REG_REQ = 0x90
MASTER_REG_REPLY = 0x91
PEER_LIST_REQ = 0x92
PEER_LIST_REPLY = 0x93
PEER_REG_REQ = 0x94
PEER_REG_REPLY = 0x95
MASTER_ALIVE_REQ = 0x96
MASTER_ALIVE_REPLY = 0x97
PEER_ALIVE_REQ = 0x98
PEER_ALIVE_REPLY = 0x99
DE_REG_REQ = 0x9A
DE_REG_REPLY = 0x9B
SYSTEM_MAP_REQ = 0x9C
SYSTEM_MAP_REPLY = 0x9D
UNKNOWN_9E = 0x9E
WIRELINE = 0xB2
REMOTE_PROG_REQ = 0xE0
REMOTE_PROG_REPLY = 0xE1
OPCODE_F0 = 0xF0

OPCODES = {
    CALL_CONFIRMATION: "CALL_CONFIRMATION", TXT_MESSAGE_ACK: "TXT_MESSAGE_ACK",
    CALL_MON_STATUS: "CALL_MON_STATUS", CALL_MON_RPT: "CALL_MON_RPT", CALL_MON_NACK: "CALL_MON_NACK",
    XCMP_XNL: "XCMP_XNL", GROUP_VOICE: "GROUP_VOICE", PVT_VOICE: "PVT_VOICE",
    GROUP_DATA: "GROUP_DATA", PVT_DATA: "PVT_DATA", RPT_WAKE_UP: "RPT_WAKE_UP",
    CALL_INTERRUPT_REQ: "CALL_INTERRUPT_REQ", MASTER_REG_REQ: "MASTER_REG_REQ",
    MASTER_REG_REPLY: "MASTER_REG_REPLY", PEER_LIST_REQ: "PEER_LIST_REQ",
    PEER_LIST_REPLY: "PEER_LIST_REPLY", PEER_REG_REQ: "PEER_REG_REQ", PEER_REG_REPLY: "PEER_REG_REPLY",
    MASTER_ALIVE_REQ: "MASTER_ALIVE_REQ", MASTER_ALIVE_REPLY: "MASTER_ALIVE_REPLY",
    PEER_ALIVE_REQ: "PEER_ALIVE_REQ", PEER_ALIVE_REPLY: "PEER_ALIVE_REPLY",
    DE_REG_REQ: "DE_REG_REQ", DE_REG_REPLY: "DE_REG_REPLY", SYSTEM_MAP_REQ: "SYSTEM_MAP_REQ",
    SYSTEM_MAP_REPLY: "SYSTEM_MAP_REPLY", UNKNOWN_9E: "UNKNOWN_9E", WIRELINE: "WIRELINE",
    REMOTE_PROG_REQ: "REMOTE_PROG_REQ", REMOTE_PROG_REPLY: "REMOTE_PROG_REPLY", OPCODE_F0: "OPCODE_F0",
}

AUTH_LEN = 10
KEY_LEN = 20
# burst types (byte 30 of a voice packet)
VOICE_HEAD, VOICE_TERM, SLOT1_VOICE, SLOT2_VOICE, SYNC_UNDETECT = 0x01, 0x02, 0x0A, 0x8A, 0x13
# call info (byte 17)
TS2_MASK, END_MASK = 0x20, 0x40
# RTP payload type byte (byte 19): marker bit set on the first header, 0x5E ends the call
RTP_PT_HEAD, RTP_PT_VOICE, RTP_PT_TERM = 0xDD, 0x5D, 0x5E
RTP_STEP = 480                     # 8 kHz clock, one 60 ms burst
DEFAULT_CALL_CTRL = 0x000043E2     # Motorola default, opaque
DEFAULT_CALL_TYPE = 0x02
HEADER_LEN = 31                    # bytes 0..30
HEAD_TERM_LEN = 54
BURST_LEN = {"A": 52, "B": 57, "C": 57, "D": 57, "E": 66, "F": 57}
LETTERS = "ABCDEF"


class IpscError(ValueError):
    """A packet that does not have the documented layout."""


class AuthError(IpscError):
    """The HMAC digest of the packet is missing or wrong."""


# ------------------------------------------------------------------ auth
def auth_key(text):
    """The 20 byte key of an authenticated system from the hex text of the
    codeplug (up to 40 hex characters, shorter keys are left padded with
    zeros)."""
    text = str(text).strip()
    if len(text) > 2 * KEY_LEN:
        raise ValueError("key is at most 40 hex characters")
    return bytes.fromhex(text.rjust(2 * KEY_LEN, "0"))


def digest(key, body):
    """First 10 bytes of HMAC-SHA1(key, body)."""
    return hmac.new(key, body, hashlib.sha1).digest()[:AUTH_LEN]


def sign(key, body):
    """body + digest, or body unchanged when key is None / empty."""
    return body + digest(key, body) if key else body


def strip_auth(key, data):
    """The packet without its digest, or None when the digest is wrong."""
    if len(data) <= AUTH_LEN:
        return None
    body, tag = data[:-AUTH_LEN], data[-AUTH_LEN:]
    return body if hmac.compare_digest(tag, digest(key, body)) else None


# ------------------------------------------------------------------ mode, flags, version
OP_DISABLED, OP_ENABLED, OP_KNOCKDOWN, OP_LOCKED = 0, 1, 2, 3
RADIO_NONE, RADIO_ANALOG, RADIO_DIGITAL, RADIO_MIXED = 0, 1, 2, 3
SLOT_DISABLED, SLOT_LOCAL, SLOT_IPSC, SLOT_RESERVED = 0, 1, 2, 3
MODE_DEFAULT = 0x6A                # enabled, digital, both slots linked to the IPSC


def mode_byte(op=OP_ENABLED, radio=RADIO_DIGITAL, ts1=SLOT_IPSC, ts2=SLOT_IPSC):
    """The MODE byte: four 2-bit fields (operational, radio, slot 1, slot 2)."""
    return ((op & 3) << 6) | ((radio & 3) << 4) | ((ts1 & 3) << 2) | (ts2 & 3)


def parse_mode(value):
    return {"op": value >> 6, "radio": (value >> 4) & 3, "ts1": (value >> 2) & 3, "ts2": value & 3}


FLAG_NAMES = (
    # (byte index, mask, name); bytes 0 and 1 are only partly understood
    (0, 0x10, "slot2_wireline"), (0, 0x08, "slot1_wireline"), (0, 0x04, "wireline_service"),
    (1, 0x80, "mnis"), (1, 0x40, "ip_site_freq"), (1, 0x10, "slot2_phone"), (1, 0x08, "slot1_phone"),
    (1, 0x04, "virtual_peer"), (1, 0x02, "cps_avail"),
    (2, 0x80, "csbk"), (2, 0x40, "rpt_mon"), (2, 0x20, "con_app"),
    (3, 0x80, "xnl_con"), (3, 0x40, "xnl_master"), (3, 0x20, "xnl_slave"), (3, 0x10, "auth"),
    (3, 0x08, "data"), (3, 0x04, "voice"), (3, 0x01, "master"),
)


def flags_word(byte0=0, byte1=0, **named):
    """The 4 byte FLAGS field. named: any name of FLAG_NAMES set to True
    (csbk, rpt_mon, con_app, xnl_con, xnl_master, xnl_slave, auth, data,
    voice, master, ...). voice defaults to True."""
    out = bytearray((byte0, byte1, 0, 0))
    named.setdefault("voice", True)
    known = {name for _, _, name in FLAG_NAMES}
    for name in named:
        if name not in known:
            raise ValueError("unknown flag %r" % name)
    for index, mask, name in FLAG_NAMES:
        if named.get(name):
            out[index] |= mask
    return bytes(out)


def parse_flags(word):
    """{name: bool} for the named bits plus "raw" (the 4 bytes)."""
    if len(word) != 4:
        raise IpscError("flags are 4 bytes")
    result = {name: bool(word[index] & mask) for index, mask, name in FLAG_NAMES}
    result["raw"] = bytes(word)
    return result


def version_field(main_type=1, main_version=2, compat_type=1, compat_version=1):
    """The 4 byte protocol field: two (6 bit type, 10 bit version) pairs, the
    main protocol first and the backward compatible one second. The default
    is the 04 02 04 01 of DMRlink and ipsc2hbp (IPSC v2 main, v1 compat)."""
    def pair(kind, version):
        return (((kind & 0x3F) << 10) | (version & 0x3FF)).to_bytes(2, "big")
    return pair(main_type, main_version) + pair(compat_type, compat_version)


def parse_version(field):
    if len(field) != 4:
        raise IpscError("version is 4 bytes")
    main, compat = int.from_bytes(field[:2], "big"), int.from_bytes(field[2:], "big")
    return {"main_type": main >> 10, "main_version": main & 0x3FF,
            "compat_type": compat >> 10, "compat_version": compat & 0x3FF, "raw": bytes(field)}


IPSC_VER = version_field()


def _id(radio_id):
    return int(radio_id).to_bytes(4, "big")


# ------------------------------------------------------------------ link packets (unsigned bodies)
def master_reg_req(peer_id, mode=MODE_DEFAULT, flags=None, version=IPSC_VER):
    """Peer -> master registration request (14 bytes)."""
    flags = flags_word() if flags is None else flags
    return bytes((MASTER_REG_REQ,)) + _id(peer_id) + bytes((mode,)) + flags + version


def master_reg_reply(master_id, num_peers, mode=MODE_DEFAULT, flags=None, version=IPSC_VER):
    """Master -> peer registration reply (16 bytes)."""
    flags = flags_word(master=True) if flags is None else flags
    return (bytes((MASTER_REG_REPLY,)) + _id(master_id) + bytes((mode,)) + flags
            + struct.pack(">H", num_peers) + version)


def peer_list_req(peer_id):
    return bytes((PEER_LIST_REQ,)) + _id(peer_id)


def peer_list_reply(master_id, peers):
    """peers: [(radio_id, ip text, port, mode byte)], 11 bytes each."""
    entries = b"".join(_id(rid) + socket.inet_aton(ip) + struct.pack(">H", port) + bytes((mode,))
                       for rid, ip, port, mode in peers)
    return bytes((PEER_LIST_REPLY,)) + _id(master_id) + struct.pack(">H", len(entries)) + entries


def master_alive_req(peer_id, mode=MODE_DEFAULT, flags=None, version=IPSC_VER):
    """Peer -> master keep-alive: the registration request with opcode 0x96."""
    return bytes((MASTER_ALIVE_REQ,)) + master_reg_req(peer_id, mode, flags, version)[1:]


def master_alive_reply(master_id, mode=MODE_DEFAULT, flags=None, version=IPSC_VER):
    flags = flags_word(master=True) if flags is None else flags
    return bytes((MASTER_ALIVE_REPLY,)) + _id(master_id) + bytes((mode,)) + flags + version


def peer_reg_req(peer_id, version=IPSC_VER):
    return bytes((PEER_REG_REQ,)) + _id(peer_id) + version


def peer_reg_reply(peer_id, version=IPSC_VER):
    return bytes((PEER_REG_REPLY,)) + _id(peer_id) + version


def peer_alive_req(peer_id, mode=MODE_DEFAULT, flags=None):
    flags = flags_word() if flags is None else flags
    return bytes((PEER_ALIVE_REQ,)) + _id(peer_id) + bytes((mode,)) + flags


def peer_alive_reply(peer_id, mode=MODE_DEFAULT, flags=None):
    flags = flags_word() if flags is None else flags
    return bytes((PEER_ALIVE_REPLY,)) + _id(peer_id) + bytes((mode,)) + flags


def de_reg_req(peer_id):
    return bytes((DE_REG_REQ,)) + _id(peer_id)


def de_reg_reply(master_id):
    return bytes((DE_REG_REPLY,)) + _id(master_id)


# ------------------------------------------------------------------ RS(12,9), AMBE packing
def _gf_tables():
    exp, log, value = [0] * 512, [0] * 256, 1
    for index in range(255):
        exp[index], log[value] = value, index
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for index in range(255, 512):
        exp[index] = exp[index - 255]
    return exp, log


_EXP, _LOG = _gf_tables()
# g(x) = (x + a)(x + a^2)(x + a^3) over GF(256) with x^8 + x^4 + x^3 + x^2 + 1
_RS_GEN = (0x0E, 0x38, 0x40)
# parity masks of the LC of a voice header and of a terminator (TS 102 361-1 B.3.5)
RS_MASK = {VOICE_HEAD: 0x96, VOICE_TERM: 0x99}


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def rs129(lc, burst_type=VOICE_HEAD):
    """The 3 parity bytes of the Reed-Solomon (12,9) code over a 9 byte LC,
    masked for a voice header (0x96) or a terminator (0x99)."""
    if len(lc) != 9:
        raise ValueError("the LC is 9 bytes")
    reg = [0, 0, 0]
    for byte in lc:
        feedback = byte ^ reg[0]
        reg = [reg[1] ^ _gf_mul(feedback, _RS_GEN[0]), reg[2] ^ _gf_mul(feedback, _RS_GEN[1]),
               _gf_mul(feedback, _RS_GEN[2])]
    mask = RS_MASK[burst_type]
    return bytes(value ^ mask for value in reg)


def pack_ambe(frames):
    """Three 49-bit voice frames (7 bytes each, 49 bits MSB first as in
    nxdn.py) -> the 19 byte AMBE field: frame a at bit 0, frame b at bit 50,
    frame c at bit 100, bits 49 and 99 (the bad-frame flags of b and c) and
    the last 3 bits zero."""
    if len(frames) != 3 or any(len(f) != nxdn.FRAME_BYTES for f in frames):
        raise ValueError("three 7 byte frames")
    value = 0
    for index, frame in enumerate(frames):
        value |= (int.from_bytes(frame, "big") >> 7) << (152 - 49 - 50 * index)
    return value.to_bytes(19, "big")


def unpack_ambe(field):
    """(three 7 byte frames, (bad_b, bad_c)) of a 19 byte AMBE field."""
    if len(field) != 19:
        raise ValueError("the AMBE field is 19 bytes")
    value = int.from_bytes(field, "big")
    frames = [(((value >> (152 - 49 - 50 * index)) & ((1 << 49) - 1)) << 7).to_bytes(7, "big")
              for index in range(3)]
    return frames, ((value >> (152 - 50)) & 1, (value >> (152 - 100)) & 1)


def ambe_from_dmr(frames9):
    """Three 9 byte DMR AMBE+2 frames (as in a DMRD payload) -> the 19 byte
    field (the 72 bit FEC form is reduced to the 49 data bits, no error
    correction; frames that are not exact codewords do not round trip)."""
    return pack_ambe([nxdn.from_dmr(f) for f in frames9])


def ambe_to_dmr(field):
    """The 19 byte field -> three 9 byte DMR AMBE+2 frames (FEC added)."""
    frames, _ = unpack_ambe(field)
    return [nxdn.to_dmr(f) for f in frames]


def valid_dmr_frames(frames9):
    """Frames of signals.prng_frames("dmr", n) made exact codewords, so that
    they survive 72 -> 49 -> 72 bits."""
    return [nxdn.to_dmr(nxdn.from_dmr(f)) for f in frames9]


# ------------------------------------------------------------------ voice packets
def emb_header(letter, colour_code=1):
    """The EMB header byte of a burst B..F as it follows the AMBE data in the
    IPSC burst: colour code, PI = 0 and the LCSS of the burst (B 1, C and D 3,
    E 2, F 0), the lowest bit cleared."""
    lcss = {"B": 1, "C": 3, "D": 3, "E": 2, "F": 0}[letter]
    return (dmr.emb(colour_code, 0, lcss) >> 8) & 0xFE


def head_term_payload(kind, ts, lc, rssi_raw=0):
    """Bytes 30..53 of a voice header (kind VOICE_HEAD) or terminator
    (VOICE_TERM): burst type, slot flag, length in words (10), RSSI-present
    flag, slot type byte, data size in bits (96), the 9 byte LC, its RS(12,9)
    parity, 00, 0x11 / 0x12 and the 16 bit RSSI (8.8 fixed point, 0 = none)."""
    if kind not in (VOICE_HEAD, VOICE_TERM):
        raise ValueError("kind is VOICE_HEAD or VOICE_TERM")
    return (bytes((kind, 0x80 if ts == 2 else 0x00)) + struct.pack(">H", 10) + b"\x80\x0a"
            + struct.pack(">H", 96) + bytes(lc) + rs129(lc, kind)
            + bytes((0x00, 0x11 if kind == VOICE_HEAD else 0x12)) + struct.pack(">H", rssi_raw))


def slot_voice_payload(letter, ts, ambe, lc=None, colour_code=1, fragments=None):
    """Bytes 30.. of a voice burst A..F: burst type (0x0A / 0x8A), length to
    follow, control byte, the 19 byte AMBE field and the burst specific tail:
    A none; B, C, D the 4 byte embedded LC fragment and the EMB header; E the
    fragment, the LC (FLCO, FID, options, destination, source) and the EMB
    header; F four zero bytes and 0x10. lc defaults to a zero LC; fragments
    are the four 32 bit values of dmr.encode_embedded_lc(lc)."""
    if len(ambe) != 19:
        raise ValueError("the AMBE field is 19 bytes")
    kind = SLOT2_VOICE if ts == 2 else SLOT1_VOICE
    lc = bytes(9) if lc is None else bytes(lc)
    if fragments is None:
        fragments = dmr.encode_embedded_lc(lc)
    if letter == "A":
        return bytes((kind, 0x14, 0x40)) + ambe
    if letter in "BCD":
        return (bytes((kind, 0x19, 0x06)) + ambe + fragments["BCD".index(letter)].to_bytes(4, "big")
                + bytes((emb_header(letter, colour_code),)))
    if letter == "E":
        return (bytes((kind, 0x22, 0x16)) + ambe + fragments[3].to_bytes(4, "big") + lc
                + bytes((emb_header("E", colour_code),)))
    if letter == "F":
        return bytes((kind, 0x19, 0x06)) + ambe + b"\x00\x00\x00\x00" + bytes((emb_header("F", colour_code),))
    raise ValueError("burst letter is A..F")


def voice_packet(peer_id, call_seq, src, dst, payload, ts=2, end=False, rtp_seq=0, rtp_ts=0,
                 rtp_pt=RTP_PT_VOICE, private=False, call_type=DEFAULT_CALL_TYPE,
                 call_ctrl=DEFAULT_CALL_CTRL):
    """A GROUP_VOICE (PVT_VOICE when private) packet: the 30 byte header and
    payload (which starts with the burst type byte)."""
    call_info = (TS2_MASK if ts == 2 else 0) | (END_MASK if end else 0)
    return (bytes((PVT_VOICE if private else GROUP_VOICE,)) + _id(peer_id) + bytes((call_seq & 0xFF,))
            + int(src).to_bytes(3, "big") + int(dst).to_bytes(3, "big") + bytes((call_type,))
            + int(call_ctrl).to_bytes(4, "big") + bytes((call_info, 0x80, rtp_pt))
            + struct.pack(">HI", rtp_seq & 0xFFFF, rtp_ts & 0xFFFFFFFF) + bytes(4) + payload)


def call_packets(peer_id, call_seq, src, dst, ambe_fields, ts=2, headers=3, private=False,
                 rtp_seq=0, rtp_ts=0, colour_code=1, lc_fid=0, lc_options=0, rssi_raw=0,
                 call_ctrl=DEFAULT_CALL_CTRL):
    """The packets of one transmission, in order and unpaced (send one every
    60 ms): `headers` voice headers (the first with the RTP marker, as a
    Motorola repeater sends them), one burst per 19 byte AMBE field (A..F,
    A first), and the terminator. The RTP sequence advances by one per packet
    and the timestamp by 480 per packet; the call stream id is call_seq."""
    flco = dmr.FLCO_PRIVATE if private else dmr.FLCO_GROUP
    lc = dmr.full_lc(flco, src, dst, lc_fid, lc_options)
    fragments = dmr.encode_embedded_lc(lc)
    packets = []
    state = {"seq": rtp_seq, "ts": rtp_ts}

    def emit(payload, pt, end=False):
        packets.append(voice_packet(peer_id, call_seq, src, dst, payload, ts, end, state["seq"],
                                    state["ts"], pt, private, call_ctrl=call_ctrl))
        state["seq"] += 1
        state["ts"] += RTP_STEP

    for index in range(headers):
        emit(head_term_payload(VOICE_HEAD, ts, lc, rssi_raw), RTP_PT_HEAD if index == 0 else RTP_PT_VOICE)
    for index, ambe in enumerate(ambe_fields):
        letter = LETTERS[index % 6]
        emit(slot_voice_payload(letter, ts, ambe, lc, colour_code, fragments), RTP_PT_VOICE)
    emit(head_term_payload(VOICE_TERM, ts, lc, rssi_raw), RTP_PT_TERM, end=True)
    return packets


# ------------------------------------------------------------------ decoder
def _need(data, size, exact=True):
    if len(data) < size or (exact and len(data) != size):
        raise IpscError("%s: %d bytes, expected %s%d" % (
            OPCODES.get(data[0], "0x%02X" % data[0]), len(data), "" if exact else "at least ", size))


def _rtp(data):
    return {"version_byte": data[18], "pt_byte": data[19], "marker": data[19] >> 7, "pt": data[19] & 0x7F,
            "seq": int.from_bytes(data[20:22], "big"), "ts": int.from_bytes(data[22:26], "big"),
            "ssrc": int.from_bytes(data[26:30], "big")}


def _decode_voice(data):
    _need(data, HEADER_LEN, exact=False)
    info = data[17]
    out = {"name": OPCODES[data[0]], "peer_id": int.from_bytes(data[1:5], "big"), "call_seq": data[5],
           "src": int.from_bytes(data[6:9], "big"), "dst": int.from_bytes(data[9:12], "big"),
           "call_type": data[12], "call_ctrl": int.from_bytes(data[13:17], "big"), "call_info": info,
           "ts": 2 if info & TS2_MASK else 1, "end": bool(info & END_MASK), "rtp": _rtp(data),
           "burst_type": data[30], "length": len(data)}
    burst = data[30]
    if burst in (VOICE_HEAD, VOICE_TERM):
        _need(data, 50, exact=False)
        words = int.from_bytes(data[32:34], "big")
        if 34 + 2 * words != len(data):
            raise IpscError("voice header length to follow %d words does not match %d bytes" % (words, len(data)))
        out.update(kind="HEAD" if burst == VOICE_HEAD else "TERM",
                   slot2_bit=bool(data[31] & 0x80), words=words,
                   rssi_present=bool(data[34] & 0x80), slot_type_byte=data[35],
                   data_bits=int.from_bytes(data[36:38], "big"), lc=bytes(data[38:47]),
                   rs=bytes(data[47:50]), tag=bytes(data[50:52]),
                   rssi_raw=int.from_bytes(data[52:54], "big") if len(data) >= 54 else None)
        out["rs_ok"] = out["rs"] == rs129(out["lc"], burst)
    elif (burst & 0x7F) == SYNC_UNDETECT:
        out.update(kind="SYNC_UNDETECT", slot2_bit=bool(burst & 0x80))
    elif burst in (SLOT1_VOICE, SLOT2_VOICE):
        _need(data, 52, exact=False)
        length, ctrl = data[31], data[32]
        if 32 + length != len(data):
            raise IpscError("voice burst length byte %d does not match %d bytes" % (length, len(data)))
        out.update(ts_burst=2 if burst & 0x80 else 1, ctrl=ctrl, ambe=bytes(data[33:52]),
                   sync=bool(ctrl & 0x40), lc_present=bool(ctrl & 0x10), lc_failed=bool(ctrl & 0x80),
                   bad_a=bool(ctrl & 0x01))
        if length == 0x14:
            out["kind"] = "A"
        elif length == 0x22:
            _need(data, 66)
            out.update(kind="E", fragment=bytes(data[52:56]), lc=bytes(data[56:65]), emb=data[65])
        elif length == 0x19:
            _need(data, 57)
            out.update(kind="F" if data[56] & 0xFE == 0x10 and not any(data[52:56]) else "BCD",
                       fragment=bytes(data[52:56]), emb=data[56])
        else:
            raise IpscError("unknown voice burst length byte 0x%02X" % length)
    else:
        out["kind"] = "UNKNOWN_BURST"
        out["payload"] = bytes(data[31:])
    return out


def decode(data, key=None):
    """Decode one datagram into a dict ("name", "op" and the fields of the
    packet). key: the 20 byte auth key of an authenticated system (the digest
    is checked and removed; AuthError when it is wrong), None for an
    unauthenticated one. Raises IpscError when the length or a field is not
    what the layout allows; opcodes this module does not know come back with
    name UNKNOWN_0x.. and the raw body."""
    if key:
        body = strip_auth(key, data)
        if body is None:
            raise AuthError("digest mismatch or packet too short")
        data = body
    if not data:
        raise IpscError("empty packet")
    op = data[0]
    if op in (GROUP_VOICE, PVT_VOICE):
        out = _decode_voice(data)
        out["op"] = op
        return out
    out = {"op": op, "name": OPCODES.get(op, "UNKNOWN_0x%02X" % op), "length": len(data)}
    if op in (MASTER_REG_REQ, MASTER_ALIVE_REQ, MASTER_ALIVE_REPLY):
        _need(data, 14)
        out.update(peer_id=int.from_bytes(data[1:5], "big"), mode=data[5], mode_fields=parse_mode(data[5]),
                   flags=parse_flags(data[6:10]), version=parse_version(data[10:14]))
    elif op == MASTER_REG_REPLY:
        _need(data, 16)
        out.update(peer_id=int.from_bytes(data[1:5], "big"), mode=data[5], mode_fields=parse_mode(data[5]),
                   flags=parse_flags(data[6:10]), num_peers=int.from_bytes(data[10:12], "big"),
                   version=parse_version(data[12:16]))
    elif op in (PEER_LIST_REQ, DE_REG_REQ, DE_REG_REPLY):
        _need(data, 5)
        out["peer_id"] = int.from_bytes(data[1:5], "big")
    elif op == PEER_LIST_REPLY:
        _need(data, 7, exact=False)
        size = int.from_bytes(data[5:7], "big")
        if size % 11 or len(data) != 7 + size:
            raise IpscError("PEER_LIST_REPLY: list length %d in a %d byte packet" % (size, len(data)))
        peers = [{"radio_id": int.from_bytes(data[o:o + 4], "big"), "ip": socket.inet_ntoa(data[o + 4:o + 8]),
                  "port": int.from_bytes(data[o + 8:o + 10], "big"), "mode": data[o + 10]}
                 for o in range(7, 7 + size, 11)]
        out.update(peer_id=int.from_bytes(data[1:5], "big"), peers=peers)
    elif op in (PEER_REG_REQ, PEER_REG_REPLY):
        _need(data, 9)
        out.update(peer_id=int.from_bytes(data[1:5], "big"), version=parse_version(data[5:9]))
    elif op in (PEER_ALIVE_REQ, PEER_ALIVE_REPLY):
        _need(data, 10)
        out.update(peer_id=int.from_bytes(data[1:5], "big"), mode=data[5], mode_fields=parse_mode(data[5]),
                   flags=parse_flags(data[6:10]))
    else:
        if len(data) >= 5:
            out["peer_id"] = int.from_bytes(data[1:5], "big")
        out["body"] = bytes(data)
    return out


def describe(packet):
    """One line of a decoded packet for a trace: name, peer id, the fields
    that identify it."""
    parts = [packet["name"]]
    if "peer_id" in packet:
        parts.append("id=%d" % packet["peer_id"])
    if packet["op"] in (GROUP_VOICE, PVT_VOICE):
        parts.append("ts=%d call=%d src=%d dst=%d %s rtp=%d/%d%s" % (
            packet["ts"], packet["call_seq"], packet["src"], packet["dst"], packet.get("kind", "?"),
            packet["rtp"]["seq"], packet["rtp"]["ts"], " end" if packet["end"] else ""))
    elif "peers" in packet:
        parts.append("peers=%d" % len(packet["peers"]))
    elif "num_peers" in packet:
        parts.append("peers=%d" % packet["num_peers"])
    if "mode" in packet:
        parts.append("mode=0x%02X" % packet["mode"])
    return " ".join(parts)


# ------------------------------------------------------------------ simulated repeater
class PeerSession:
    """The protocol state of one simulated MOTOTRBO repeater (a peer), without
    a socket: register(), keepalive() and handle() return the datagrams to
    send as (address, bytes) pairs, already signed. The behaviour follows the
    peer of DMRlink and ipsc2hbp: register with the master, ask for the peer
    list, register with every listed peer (PEER_REG_REQ) and keep it alive
    (PEER_ALIVE_REQ), answer the requests of the others."""

    def __init__(self, radio_id, master, key=None, mode=MODE_DEFAULT, flags=None, version=IPSC_VER,
                 ask_peer_list=True):
        self.radio_id = radio_id
        self.master = tuple(master)
        self.key = key
        self.mode = mode
        self.flags = flags_word(auth=bool(key)) if flags is None else flags
        self.version = version
        self.ask_peer_list = ask_peer_list
        self.state = "IDLE"            # IDLE, REGISTERING, REGISTERED, ACTIVE
        self.master_info = None
        self.mesh = {}                 # radio id -> {"addr", "mode", "connected", "outstanding"}
        self.missed = 0
        self.received = []             # decoded voice packets from anyone
        self.unhandled = []            # decoded packets with no handler
        self.errors = []               # (reason, raw) of undecodable datagrams
        self.trace = []                # (direction, addr, description)
        self.call_seq = 0

    def _out(self, addr, body):
        self.trace.append(("tx", addr, describe(decode(body))))
        return (addr, sign(self.key, body))

    def register(self):
        """MASTER_REG_REQ to the master."""
        self.state = "REGISTERING"
        self.missed = 0
        return [self._out(self.master, master_reg_req(self.radio_id, self.mode, self.flags, self.version))]

    def keepalive(self):
        """One tick of the maintenance loop: MASTER_ALIVE_REQ to the master
        and, once the peer list is known, PEER_REG_REQ / PEER_ALIVE_REQ to
        every listed peer."""
        out = []
        if self.state in ("REGISTERED", "ACTIVE"):
            out.append(self._out(self.master, master_alive_req(self.radio_id, self.mode, self.flags, self.version)))
            self.missed += 1
        if self.state == "ACTIVE":
            for rid, peer in self.mesh.items():
                if not peer["connected"]:
                    out.append(self._out(peer["addr"], peer_reg_req(self.radio_id, self.version)))
                else:
                    out.append(self._out(peer["addr"], peer_alive_req(self.radio_id, self.mode, self.flags)))
                    peer["outstanding"] += 1
        return out

    def de_register(self):
        self.state = "IDLE"
        return [self._out(self.master, de_reg_req(self.radio_id))]

    def next_call(self):
        """A fresh call stream id (increments by one per call, wraps at 255)."""
        self.call_seq = (self.call_seq + 1) & 0xFF
        return self.call_seq

    def handle(self, data, addr):
        """Process one received datagram; returns the replies to send."""
        try:
            packet = decode(data, self.key)
        except IpscError as error:
            self.errors.append((str(error), bytes(data)))
            return []
        self.trace.append(("rx", addr, describe(packet)))
        op = packet["op"]
        if op == MASTER_REG_REPLY:
            self.master_info = packet
            self.state = "REGISTERED"
            self.missed = 0
            if self.ask_peer_list:
                return [self._out(self.master, peer_list_req(self.radio_id))]
        elif op == PEER_LIST_REPLY:
            self.missed = 0
            self.state = "ACTIVE"
            seen = set()
            for entry in packet["peers"]:
                if entry["radio_id"] == self.radio_id:
                    continue
                seen.add(entry["radio_id"])
                known = self.mesh.get(entry["radio_id"])
                if known:
                    known["addr"] = (entry["ip"], entry["port"])
                else:
                    self.mesh[entry["radio_id"]] = {"addr": (entry["ip"], entry["port"]), "mode": entry["mode"],
                                                    "connected": False, "outstanding": 0}
            for rid in [r for r in self.mesh if r not in seen]:
                del self.mesh[rid]
        elif op == MASTER_ALIVE_REPLY:
            self.missed = 0
        elif op == PEER_REG_REQ:
            return [self._out(addr, peer_reg_reply(self.radio_id, self.version))]
        elif op == PEER_REG_REPLY:
            if packet["peer_id"] in self.mesh:
                self.mesh[packet["peer_id"]].update(connected=True, outstanding=0)
        elif op == PEER_ALIVE_REQ:
            if packet["peer_id"] in self.mesh:
                self.mesh[packet["peer_id"]]["outstanding"] = 0
            return [self._out(addr, peer_alive_reply(self.radio_id, self.mode, self.flags))]
        elif op == PEER_ALIVE_REPLY:
            if packet["peer_id"] in self.mesh:
                self.mesh[packet["peer_id"]]["outstanding"] = 0
        elif op in (GROUP_VOICE, PVT_VOICE):
            self.received.append(packet)
        else:
            self.unhandled.append(packet)
        return []

    def call(self, src, dst, ambe_fields, ts=2, **options):
        """The datagrams of one transmission to the master (and to every
        connected peer), unpaced; the caller sends one every 60 ms."""
        packets = call_packets(self.radio_id, self.next_call(), src, dst, ambe_fields, ts=ts, **options)
        targets = [self.master] + [p["addr"] for p in self.mesh.values() if p["connected"]]
        return [[(addr, sign(self.key, body)) for addr in targets] for body in packets]


class IpscPeer:
    """A PeerSession on a UDP socket: connect() runs the registration, step()
    services the socket and the keep-alive timer, send_call() paces a
    transmission at 60 ms. Used by the rig cases; the unit tests use
    PeerSession directly."""

    def __init__(self, bind_ip, master, radio_id, key=None, port=0, interval=5.0, **session):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(0.02)
        self.session = PeerSession(radio_id, master, key, **session)
        self.interval = interval
        self._next_keepalive = 0.0

    @property
    def local(self):
        return self.sock.getsockname()

    def _send(self, pairs):
        for addr, body in pairs:
            self.sock.sendto(body, addr)

    def step(self, seconds=0.1):
        """Receive for `seconds`, answer, and send a keep-alive when due."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
                self._send(self.session.handle(data, addr))
            except socket.timeout:
                pass
            now = time.monotonic()
            if now >= self._next_keepalive and self.session.state != "IDLE":
                self._next_keepalive = now + self.interval
                self._send(self.session.keepalive())
            if now >= deadline:
                return

    def connect(self, timeout=5.0):
        """Register and wait for the peer list; True when ACTIVE."""
        self._send(self.session.register())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.step(0.1)
            if self.session.state == "ACTIVE":
                return True
        return False

    def send_call(self, src, dst, ambe_fields, ts=2, gap=0.060, **options):
        start = time.monotonic()
        for index, pairs in enumerate(self.session.call(src, dst, ambe_fields, ts=ts, **options)):
            delay = start + index * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._send(pairs)

    def close(self):
        self._send(self.session.de_register())
        self.sock.close()
