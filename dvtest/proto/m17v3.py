# SPDX-License-Identifier: MIT
"""M17 version 3 (draft 3.0.0) TYPE and META encoders, the legacy (2.0.x) <->
v3 mapping, and the signed-stream generator with its negative variants.

Written from the specification text only (docs/m17v3-cases.md has the file
and line of every statement); no code of any M17 implementation is used.
Existing v2 behaviour of dvtest.proto.m17 is untouched: the 54-byte packets
are built with its stream_packet().

Layout facts used here
  v3 TYPE (16 bits, byte 0 then byte 1):
      payload<<12 | encryption<<9 | signed<<8 | meta<<4 | can
  legacy TYPE (2.0.x): bit 0 stream, bits 2-1 data type, bits 4-3 encryption
      type, bits 6-5 encryption subtype, bits 10-7 CAN, bit 11 signed,
      bits 15-12 reserved (zero).  A zero top nibble marks the legacy layout.
  META is 14 bytes.
  Digest of a signed stream: 16 zero bytes; for every data frame
      digest ^= payload; digest = rotate-left-one-byte(digest).
  Signature frames: FN 0x7FFC .. 0x7FFE and 0xFFFF (0x7FFF with the EOT bit).
"""
import hashlib
import math

from .. import ecdsa_p256
from . import m17

# ---------------------------------------------------------------- TYPE fields

PAYLOAD_VERSION_RESERVED = 0x0
PAYLOAD_DATA = 0x1
PAYLOAD_VOICE3200 = 0x2
PAYLOAD_PACKET = 0xF

ENC_NONE = 0
ENC_SCRAMBLER8, ENC_SCRAMBLER16, ENC_SCRAMBLER24 = 1, 2, 3
ENC_AES128, ENC_AES192, ENC_AES256 = 4, 5, 6
ENC_RESERVED = 7

META_NONE = 0x0
META_GNSS = 0x1
META_ECD = 0x2
META_TEXT = 0x3
META_AES_IV = 0xF

SIG_FN_FIRST = 0x7FFC               # first signature frame
SIG_FRAMES = 4
MAX_DATA_FRAMES = 0x8000 - SIG_FRAMES   # 0x7FFC data frames (FN 0 .. 0x7FFB)
EOT = 0x8000

LEGACY_SIGNED_BIT = 0x0800
V3_SIGNED_BIT = 0x0100


def v3_type(payload, encryption=0, signed=False, meta=0, can=0):
    """16-bit v3 TYPE. Field ranges are checked, reserved values are allowed
    (they are needed for negative vectors)."""
    for name, value, limit in (("payload", payload, 16), ("encryption", encryption, 8),
                               ("meta", meta, 16), ("can", can, 16)):
        if not 0 <= value < limit:
            raise ValueError("%s out of range" % name)
    return (payload << 12) | (encryption << 9) | ((1 if signed else 0) << 8) | (meta << 4) | can


def parse_v3_type(value):
    """dict of the five subfields; is_v3 is False for a zero top nibble."""
    return {"payload": (value >> 12) & 0xF, "encryption": (value >> 9) & 0x7,
            "signed": bool((value >> 8) & 1), "meta": (value >> 4) & 0xF, "can": value & 0xF,
            "is_v3": (value >> 12) != 0}


def is_v3_type(value):
    return (value & 0xF000) != 0


def v3_type_problems(value):
    """Reserved or illegal combinations of a v3 TYPE (strings; empty = fine)."""
    f = parse_v3_type(value)
    out = []
    if not f["is_v3"]:
        out.append("payload 0 is the legacy layout")
    elif f["payload"] not in (PAYLOAD_DATA, PAYLOAD_VOICE3200, PAYLOAD_PACKET):
        out.append("payload %#x reserved" % f["payload"])
    if f["encryption"] == ENC_RESERVED:
        out.append("encryption 7 reserved")
    if f["meta"] not in (META_NONE, META_GNSS, META_ECD, META_TEXT, META_AES_IV):
        out.append("meta %#x reserved" % f["meta"])
    if f["payload"] == PAYLOAD_PACKET:
        if f["encryption"]:
            out.append("packet mode with encryption")
        if f["signed"]:
            out.append("packet mode with signed bit")
        if f["meta"] == META_AES_IV:
            out.append("packet mode with AES IV meta")
    if f["meta"] == META_AES_IV and not ENC_AES128 <= f["encryption"] <= ENC_AES256:
        out.append("AES IV meta without AES encryption")
    return out


# ---------------------------------------------------------------- legacy <-> v3

class Unmappable(ValueError):
    """The TYPE has no equivalent in the other layout."""


def legacy_fields(value):
    return {"stream": value & 1, "data_type": (value >> 1) & 3, "enc_type": (value >> 3) & 3,
            "enc_subtype": (value >> 5) & 3, "can": (value >> 7) & 0xF,
            "signed": bool(value & LEGACY_SIGNED_BIT), "reserved": (value >> 12) & 0xF}


def legacy_type(stream=1, data_type=2, enc_type=0, enc_subtype=0, can=0, signed=False):
    return (stream & 1) | (data_type << 1) | (enc_type << 3) | (enc_subtype << 5) | ((can & 0xF) << 7) \
        | (LEGACY_SIGNED_BIT if signed else 0)


def legacy_to_v3(value, meta=bytes(14)):
    """(v3 TYPE, v3 META) for a legacy TYPE and its 14-byte META. Raises
    Unmappable for reserved values, voice+data 1600 bps (removed in v3) and
    every combination v3 cannot express. A legacy v2 text META is converted
    to the v3 control byte scheme; GNSS passes unchanged; legacy ECD maps to
    the ECD META type (which must not appear on an Internet link)."""
    f = legacy_fields(value)
    meta = bytes(meta).ljust(14, b"\0")[:14]
    if f["reserved"]:
        raise Unmappable("legacy reserved bits 15-12 are not zero")
    if f["stream"] == 0:
        if f["data_type"] or f["enc_type"] or f["enc_subtype"] or f["signed"]:
            raise Unmappable("packet mode with data type, encryption or signed set")
        return v3_type(PAYLOAD_PACKET, 0, False, META_NONE if not any(meta) else META_TEXT, f["can"]), \
            (v2_text_to_v3(meta) if any(meta) else meta)
    if f["data_type"] == 2:
        payload = PAYLOAD_VOICE3200
    elif f["data_type"] == 1:
        payload = PAYLOAD_DATA
    elif f["data_type"] == 3:
        raise Unmappable("voice+data 1600 bps was removed in v3")
    else:
        raise Unmappable("legacy data type 0 reserved")
    if f["enc_type"] == 0:
        enc, mnib, out_meta = ENC_NONE, META_NONE, bytes(14)
        if f["enc_subtype"] == 0:                      # text
            if meta[0] != 0:
                mnib, out_meta = META_TEXT, v2_text_to_v3(meta)
        elif f["enc_subtype"] == 1:
            mnib, out_meta = META_GNSS, meta
        elif f["enc_subtype"] == 2:
            mnib, out_meta = META_ECD, meta
        else:
            raise Unmappable("legacy null-encryption subtype 3 reserved")
    elif f["enc_type"] == 1:
        if f["enc_subtype"] == 3:
            raise Unmappable("legacy scrambler subtype 3 reserved")
        enc, mnib, out_meta = ENC_SCRAMBLER8 + f["enc_subtype"], META_NONE, bytes(14)
    elif f["enc_type"] == 2:
        if f["enc_subtype"] == 3:
            raise Unmappable("legacy AES subtype 3 reserved")
        enc, mnib, out_meta = ENC_AES128 + f["enc_subtype"], META_AES_IV, meta
    else:
        raise Unmappable("legacy encryption type 3 reserved")
    return v3_type(payload, enc, f["signed"], mnib, f["can"]), out_meta


def v3_to_legacy(value, meta=bytes(14), policy="omit"):
    """(legacy TYPE, legacy META) for a v3 TYPE and META. policy "omit" (what
    a legacy receiver is given): text, GNSS and ECD META are dropped (the
    legacy TYPE says null encryption + text and META is all zero); an AES IV
    is kept because the receiver needs it. policy "convert": GNSS passes,
    text converts to the legacy block scheme when it fits four blocks,
    otherwise it is dropped; ECD is still dropped (never sent on to a legacy
    Internet receiver). Raises Unmappable for reserved values."""
    if policy not in ("omit", "convert"):
        raise ValueError("policy")
    f = parse_v3_type(value)
    meta = bytes(meta).ljust(14, b"\0")[:14]
    if not f["is_v3"]:
        raise Unmappable("not a v3 TYPE")
    if f["encryption"] == ENC_RESERVED:
        raise Unmappable("encryption 7 reserved")
    if f["payload"] == PAYLOAD_PACKET:
        if f["encryption"] or f["signed"]:
            raise Unmappable("packet mode with encryption or signed")
        return legacy_type(0, 0, 0, 0, f["can"]), (v3_text_to_v2(meta) if policy == "convert"
                                                    and f["meta"] == META_TEXT else bytes(14))
    if f["payload"] == PAYLOAD_VOICE3200:
        dt = 2
    elif f["payload"] == PAYLOAD_DATA:
        dt = 1
    else:
        raise Unmappable("payload %#x has no legacy equivalent" % f["payload"])
    enc = f["encryption"]
    if enc == ENC_NONE:
        out_meta, subtype = bytes(14), 0
        if policy == "convert" and f["meta"] == META_GNSS:
            subtype, out_meta = 1, meta
        elif policy == "convert" and f["meta"] == META_TEXT:
            try:
                out_meta = v3_text_to_v2(meta)
            except ValueError:
                out_meta = bytes(14)
        return legacy_type(1, dt, 0, subtype, f["can"], f["signed"]), out_meta
    if ENC_SCRAMBLER8 <= enc <= ENC_SCRAMBLER24:
        return legacy_type(1, dt, 1, enc - ENC_SCRAMBLER8, f["can"], f["signed"]), bytes(14)
    return legacy_type(1, dt, 2, enc - ENC_AES128, f["can"], f["signed"]), \
        (meta if f["meta"] == META_AES_IV else bytes(14))


# ---------------------------------------------------------------- text META

TEXT_BLOCK = 13
V3_TEXT_MAX = 195          # 15 blocks of 13 bytes
V2_TEXT_MAX = 52           # 4 blocks of 13 bytes


def v3_text_blocks(text, index_base=1):
    """List of 14-byte META values for a v3 text message (str or bytes, UTF-8).
    Control byte: high nibble block count 1..15, low nibble block index.
    index_base 1 (chosen reading: the first block of a 15 block message is
    0xF1 and the last 0xFF) or 0 (the literal "0..15" range of the draft's
    prose). Padding is NUL. Empty text gives no block."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if len(data) > V3_TEXT_MAX:
        raise ValueError("v3 text longer than 195 bytes")
    if not data:
        return []
    count = math.ceil(len(data) / TEXT_BLOCK)
    out = []
    for i in range(count):
        chunk = data[i * TEXT_BLOCK:(i + 1) * TEXT_BLOCK].ljust(TEXT_BLOCK, b"\0")
        index = i + index_base
        if not 0 <= index <= 15:
            raise ValueError("block index out of the nibble")
        out.append(bytes([(count << 4) | index]) + chunk)
    return out


def decode_v3_text(blocks, index_base=1):
    """Text (bytes) from the blocks of one message in index order; raises
    ValueError for a block count that does not match, a wrong index or a
    NUL inside the text. Trailing NULs are stripped."""
    blocks = [bytes(b) for b in blocks]
    if not blocks:
        raise ValueError("no blocks")
    count = blocks[0][0] >> 4
    if count == 0 or count != len(blocks):
        raise ValueError("block count does not match the control byte")
    data = b""
    for i, block in enumerate(blocks):
        if len(block) != 14 or block[0] >> 4 != count or (block[0] & 0xF) != i + index_base:
            raise ValueError("bad control byte in block %d" % i)
        data += block[1:]
    text = data.rstrip(b"\0")
    if b"\0" in text:
        raise ValueError("NUL inside the text")
    return text


def v2_text_blocks(text):
    """Legacy (2.0.x) text META values: control high nibble is a bit map of
    the length (0001, 0011, 0111, 1111), low nibble one-hot block position,
    space padding, at most four blocks (52 bytes)."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if len(data) > V2_TEXT_MAX:
        raise ValueError("v2 text longer than 52 bytes")
    if not data:
        return []
    count = math.ceil(len(data) / TEXT_BLOCK)
    out = []
    for i in range(count):
        chunk = data[i * TEXT_BLOCK:(i + 1) * TEXT_BLOCK].ljust(TEXT_BLOCK, b" ")
        out.append(bytes([((1 << count) - 1) << 4 | (1 << i)]) + chunk)
    return out


def decode_v2_text(blocks):
    """Text (bytes, trailing spaces stripped) from legacy blocks: control bytes
    are OR-ed, complete when high and low nibble agree."""
    seen, need, parts = 0, 0, {}
    for block in blocks:
        block = bytes(block)
        need |= block[0] >> 4
        seen |= block[0] & 0xF
        parts[block[0] & 0xF] = block[1:]
    if not blocks or seen != need:
        raise ValueError("incomplete or inconsistent legacy text")
    return b"".join(parts[k] for k in sorted(parts)).rstrip(b" ")


def v2_text_to_v3(meta):
    """One legacy text META block as the v3 block of the same position (the
    count/index are re-derived; space padding becomes NUL padding)."""
    meta = bytes(meta)
    bitmap, pos = meta[0] >> 4, meta[0] & 0xF
    if meta[0] == 0 or pos == 0 or pos & (pos - 1) or bitmap not in (1, 3, 7, 15):
        raise ValueError("not a legacy text block")
    count = bitmap.bit_length()
    index = pos.bit_length()                       # 1-based
    body = meta[1:14].rstrip(b" ") if index == count else meta[1:14]      # padding only in the last block
    return bytes([(count << 4) | index]) + body.ljust(13, b"\0")


def v3_text_to_v2(meta):
    """One v3 text block as the legacy block (needs count <= 4)."""
    meta = bytes(meta)
    count, index = meta[0] >> 4, meta[0] & 0xF
    if not 1 <= count <= 4 or not 1 <= index <= count:
        raise ValueError("v3 text block does not fit the legacy scheme")
    body = meta[1:14].rstrip(b"\0") if index == count else meta[1:14]
    return bytes([((1 << count) - 1) << 4 | (1 << (index - 1))]) + body.ljust(13, b" ")


# ---------------------------------------------------------------- GNSS META

ALT_MAX_RAW = 0xFFFF
ALT_MAX_M = ALT_MAX_RAW * 0.5 - 500.0        # 32267.5 (the draft text says 31767.5)
SPEED_MAX_RAW = 0xFFF                          # 2047.5 km/h
V_LATLON, V_ALT, V_VELOCITY, V_RADIUS = 0x8, 0x4, 0x2, 0x1


def radius_code(radius_m):
    """3-bit radius field: the smallest n with radius <= 2**n metres, 7 for
    anything above 128 m (2.0.10 text; the 3.0.0 draft omits the rule)."""
    for n in range(8):
        if radius_m <= 2 ** n:
            return n
    return 7


def lat_to_raw(degrees):
    """Signed 24-bit latitude: the fraction of 90 degrees times 8388607,
    cut toward zero (the reading of the reference library; the text only
    gives the scale)."""
    return max(-8388607, min(8388607, int(degrees / 90.0 * 8388607)))


def lon_to_raw(degrees):
    return max(-8388607, min(8388607, int(degrees / 180.0 * 8388607)))


def alt_to_raw(metres):
    return max(0, min(ALT_MAX_RAW, int(math.floor((metres + 500.0) * 2 + 0.5))))


def speed_to_raw(kmh):
    return max(0, min(SPEED_MAX_RAW, int(math.floor(kmh * 2 + 0.5))))


def gnss_meta_raw(source=0, station=1, validity=0xF, lat_raw=0, lon_raw=0, alt_raw=0, bearing=0,
                  speed_raw=0, radius_n=0):
    """14-byte GNSS META from raw field values. Fields whose validity bit is
    clear are zeroed (the text says the transmitter zeroes them)."""
    if not validity & V_LATLON:
        lat_raw = lon_raw = 0
    if not validity & V_ALT:
        alt_raw = 0
    if not validity & V_VELOCITY:
        bearing = speed_raw = 0
    if not validity & V_RADIUS:
        radius_n = 0
    if not (0 <= source < 16 and 0 <= station < 16 and 0 <= validity < 16 and 0 <= radius_n < 8
            and 0 <= bearing < 512 and 0 <= alt_raw <= ALT_MAX_RAW and 0 <= speed_raw <= SPEED_MAX_RAW
            and -8388607 <= lat_raw <= 8388607 and -8388607 <= lon_raw <= 8388607):
        raise ValueError("GNSS field out of range")
    out = bytearray(14)
    out[0] = (source << 4) | station
    out[1] = (validity << 4) | (radius_n << 1) | (bearing >> 8)
    out[2] = bearing & 0xFF
    out[3:6] = (lat_raw & 0xFFFFFF).to_bytes(3, "big")
    out[6:9] = (lon_raw & 0xFFFFFF).to_bytes(3, "big")
    out[9:11] = alt_raw.to_bytes(2, "big")
    out[11] = speed_raw >> 4
    out[12] = (speed_raw & 0xF) << 4
    return bytes(out)


def gnss_meta(source=0, station=1, lat=None, lon=None, altitude=None, bearing=None, speed=None,
              radius=None):
    """GNSS META from physical values (degrees, metres, degrees, km/h,
    metres); an omitted value is invalid (validity bit clear)."""
    validity = 0
    if lat is not None and lon is not None:
        validity |= V_LATLON
    if altitude is not None:
        validity |= V_ALT
    if bearing is not None and speed is not None:
        validity |= V_VELOCITY
    if radius is not None:
        validity |= V_RADIUS
    return gnss_meta_raw(
        source, station, validity,
        lat_to_raw(lat) if lat is not None else 0, lon_to_raw(lon) if lon is not None else 0,
        alt_to_raw(altitude) if altitude is not None else 0, bearing or 0,
        speed_to_raw(speed) if speed is not None else 0, radius_code(radius) if radius is not None else 0)


def _s24(data):
    value = int.from_bytes(data, "big")
    return value - (1 << 24) if value & 0x800000 else value


def decode_gnss(meta):
    """dict of raw and physical values; the last 4 bits of byte 12 and byte 13
    must be zero (reserved) or ValueError is raised."""
    meta = bytes(meta)
    if len(meta) != 14:
        raise ValueError("META must be 14 bytes")
    if meta[12] & 0x0F or meta[13]:
        raise ValueError("reserved GNSS bytes are not zero")
    lat_raw, lon_raw = _s24(meta[3:6]), _s24(meta[6:9])
    alt_raw = int.from_bytes(meta[9:11], "big")
    speed_raw = (meta[11] << 4) | (meta[12] >> 4)
    radius_n = (meta[1] >> 1) & 7
    return {"source": meta[0] >> 4, "station": meta[0] & 0xF, "validity": meta[1] >> 4,
            "radius_n": radius_n, "radius_m": 2 ** radius_n, "bearing": ((meta[1] & 1) << 8) | meta[2],
            "lat_raw": lat_raw, "lon_raw": lon_raw, "alt_raw": alt_raw, "speed_raw": speed_raw,
            "lat": lat_raw / 8388607.0 * 90.0, "lon": lon_raw / 8388607.0 * 180.0,
            "altitude": alt_raw * 0.5 - 500.0, "speed": speed_raw * 0.5}


# ---------------------------------------------------------------- ECD and AES IV

def ecd_meta(callsign1, callsign2=None):
    """Extended Callsign Data: one or two 6-byte base-40 callsigns, NUL fill."""
    out = m17.encode_callsign(callsign1)
    if callsign2 is not None:
        out += m17.encode_callsign(callsign2)
    return out.ljust(14, b"\0")


def decode_ecd(meta):
    """(callsign 1, callsign 2 or None); anything after the second field must
    be NUL."""
    meta = bytes(meta)
    if len(meta) != 14 or any(meta[12:]):
        raise ValueError("bad ECD META")
    second = meta[6:12]
    return m17.decode_callsign(meta[:6]), (m17.decode_callsign(second) if any(second) else None)


EPOCH_2020 = 1577836800            # 2020-01-01T00:00:00Z as Unix time


def aes_iv_meta(timestamp_2020, random10):
    """112-bit nonce META: 32-bit timestamp (seconds since 2020-01-01Z, big
    endian) then 80 random bits."""
    if len(random10) != 10 or not 0 <= timestamp_2020 < 1 << 32:
        raise ValueError("bad nonce parts")
    return timestamp_2020.to_bytes(4, "big") + bytes(random10)


def aes_counter(meta, fn):
    """128-bit CTR counter block, chosen reading (2.0.10 table, also the
    layout used by the reference decoders): nonce (META, 14 bytes) then FN
    (16 bits, EOT bit not part of it) in the least significant bytes."""
    return bytes(meta) + (fn & 0x7FFF).to_bytes(2, "big")


def aes_counter_draft_prose(meta, fn):
    """The other reading, the stale prose of the 3.0.0 draft ("FN sets the
    most significant 16 bits, the 32-bit timestamp the least significant"):
    FN, the 80 random bits, then the timestamp. Only for negative vectors."""
    meta = bytes(meta)
    return (fn & 0x7FFF).to_bytes(2, "big") + meta[4:14] + meta[:4]


# ---------------------------------------------------------------- digest and signature

def digest_step(digest, payload):
    """digest ^= payload, then rotate left by one byte."""
    mixed = bytes(a ^ b for a, b in zip(digest, payload))
    return mixed[1:] + mixed[:1]


def stream_digest(payloads):
    digest = bytes(16)
    for payload in payloads:
        if len(payload) != 16:
            raise ValueError("payload must be 16 bytes")
        digest = digest_step(digest, payload)
    return digest


def payload_stream(seed, count):
    """Deterministic 16-byte payloads: SHA-256("<seed>/<index>")[:16]."""
    return [hashlib.sha256(("%s/%d" % (seed, i)).encode("ascii")).digest()[:16] for i in range(count)]


def sign_digest(secret, digest):
    return ecdsa_p256.sign_digest(secret, digest)


def signed_frames(payloads, signature):
    """Frame list [(fn, last, payload)] of a signed stream: data frames with
    FN 0.., no EOT, then the four signature chunks at 0x7FFC..0x7FFE and
    0x7FFF with the EOT bit."""
    if len(signature) != 64:
        raise ValueError("signature must be 64 bytes")
    frames = [(i & 0x7FFF, False, bytes(p)) for i, p in enumerate(payloads)]
    for i in range(SIG_FRAMES):
        frames.append((SIG_FN_FIRST + i, i == SIG_FRAMES - 1, signature[i * 16:(i + 1) * 16]))
    return frames


def frames_to_packets(frames, stream_id, src="N0CALL", dst=None, type_=0x2101, meta=bytes(14), lsd=None):
    """54-byte packets from a frame list. lsd: optional function(index) ->
    (type, meta) for streams whose TYPE/META change along the stream."""
    out = []
    for i, (fn, last, payload) in enumerate(frames):
        t, m = lsd(i) if lsd else (type_, meta)
        out.append(m17.stream_packet(stream_id, fn, payload, src=src, dst=dst, type_=t, meta=m, last=last))
    return out


def signed_stream(secret, payloads, stream_id=0x4D17, src="N0CALL", dst=None, type_=0x2101,
                  meta=bytes(14), lsd=None):
    """dict: packets, frames, digest, signature, public_key for a valid signed
    stream (the signature is over the 16-byte digest, micro-ecc convention)."""
    digest = stream_digest(payloads)
    sig = sign_digest(secret, digest)
    frames = signed_frames(payloads, sig)
    return {"packets": frames_to_packets(frames, stream_id, src, dst, type_, meta, lsd), "frames": frames,
            "digest": digest, "signature": sig, "public_key": ecdsa_p256.public_key(secret)}


def retype_packets(packets, new_type, new_meta=None):
    """Copies of 54-byte packets with TYPE (and optionally META) replaced and
    the CRC recomputed, e.g. a v3 stream re-labelled with the legacy TYPE so
    that a 2.0.x decoder accepts it (the signature covers payloads only)."""
    out = []
    for pkt in packets:
        if len(pkt) != m17.PACKET_SIZE:               # a damaged packet stays damaged
            out.append(bytes(pkt))
            continue
        body = bytearray(pkt[:52])
        body[18:20] = new_type.to_bytes(2, "big")
        if new_meta is not None:
            body[20:34] = bytes(new_meta).ljust(14, b"\0")[:14]
        out.append(bytes(body) + m17.crc16(bytes(body)).to_bytes(2, "big"))
    return out


# ---------------------------------------------------------------- reference verifier

def verify_stream(packets, public_key):
    """Reference receiver for a list of 54-byte packets in arrival order.
    Returns {"verdict", "reason"}; verdict is one of
      "valid"              4 signature frames at FN 0x7FFC.., EOT only on the last,
                           signature verifies over the digest of the data frames
      "invalid_signature"  complete tail, the signature does not verify
      "incomplete"         signed bit set, tail missing or short
      "malformed"          FN/EOT rules broken (EOT elsewhere, FN collision with
                           the signature range, duplicate or foreign tail FN)
      "unsigned"           the signed bit is clear
    The digest covers the payloads of frames with FN < 0x7FFC in arrival order."""
    parsed = []
    for pkt in packets:
        p = m17.parse_stream(pkt)
        if p is None or not p["crc_ok"]:
            return {"verdict": "malformed", "reason": "bad packet or CRC"}
        parsed.append(p)
    if not parsed:
        return {"verdict": "malformed", "reason": "empty"}
    t = parsed[0]["type"]
    signed = bool(t & (V3_SIGNED_BIT if is_v3_type(t) else LEGACY_SIGNED_BIT))
    if not signed:
        return {"verdict": "unsigned", "reason": "signed bit clear"}
    last_idx = [i for i, p in enumerate(parsed) if p["last"]]
    first_tail = next((i for i, p in enumerate(parsed) if p["fn"] >= SIG_FN_FIRST), None)
    if first_tail is None:
        return {"verdict": "incomplete", "reason": "no signature frames"}
    data, tail = parsed[:first_tail], parsed[first_tail:]
    if any(p["fn"] < SIG_FN_FIRST for p in tail):
        return {"verdict": "malformed", "reason": "data frame after a signature frame"}
    if last_idx and last_idx != [len(parsed) - 1]:
        return {"verdict": "malformed", "reason": "EOT bit not only on the final packet"}
    fns = [p["fn"] for p in tail]
    if len(tail) < SIG_FRAMES:
        if last_idx and parsed[-1]["fn"] != 0x7FFF:
            return {"verdict": "malformed", "reason": "EOT on a frame that is not 0x7FFF"}
        return {"verdict": "incomplete", "reason": "%d of 4 signature frames" % len(tail)}
    if fns != [0x7FFC, 0x7FFD, 0x7FFE, 0x7FFF]:
        return {"verdict": "malformed", "reason": "signature frame numbers %s" % [hex(f) for f in fns]}
    if not parsed[-1]["last"]:
        return {"verdict": "malformed", "reason": "EOT missing on the last signature frame"}
    digest = stream_digest([p["payload"] for p in data])
    sig = b"".join(p["payload"] for p in tail)
    if ecdsa_p256.verify_digest(public_key, digest, sig):
        return {"verdict": "valid", "reason": "signature verifies"}
    return {"verdict": "invalid_signature", "reason": "signature does not verify"}


# ---------------------------------------------------------------- negative frame lists
# Each takes a frame list [(fn, last, payload)] from signed_frames() and returns a
# modified copy; frames_to_packets() turns it into packets.

def neg_lost_frame(frames, index):
    """Data frame `index` never arrives (its FN is simply missing)."""
    return frames[:index] + frames[index + 1:]


def neg_flip_payload_bit(frames, index, bit=0):
    fn, last, payload = frames[index]
    mutated = bytearray(payload)
    mutated[bit // 8] ^= 0x80 >> (bit % 8)
    return frames[:index] + [(fn, last, bytes(mutated))] + frames[index + 1:]


def neg_swap_frames(frames, i, j):
    """Arrival order changed: the frames at positions i and j trade places and
    keep their own FN."""
    out = list(frames)
    out[i], out[j] = out[j], out[i]
    return out


def neg_duplicate_frame(frames, index):
    """Data frame `index` arrives twice in a row (same FN, same payload)."""
    return frames[:index + 1] + [frames[index]] + frames[index + 1:]


def neg_early_tail(frames, position=3):
    """The first signature chunk (7FFC) arrives after `position` data frames and
    the remaining data frames follow it."""
    data = len(frames) - SIG_FRAMES
    return frames[:position] + [frames[data]] + frames[position:data] + frames[data + 1:]


def neg_truncated_tail(frames, keep):
    """Only the first `keep` (0..3) signature frames arrive."""
    return frames[:len(frames) - SIG_FRAMES + keep]


def neg_signed_no_tail(frames):
    """Data frames only; the last one carries the EOT bit; the TYPE keeps the
    signed bit."""
    data = frames[:len(frames) - SIG_FRAMES]
    fn, _, payload = data[-1]
    return data[:-1] + [(fn, True, payload)]


def neg_eot(frames, where):
    """EOT on the wrong frame. where: "last_data" (EOT on the last data frame,
    signature frames follow), "first_sig", "third_sig" (0x7FFE, none on 0x7FFF),
    "none" (no EOT anywhere), "both" (EOT on 0x7FFE and 0x7FFF)."""
    out = [(fn, False, p) for fn, _, p in frames]
    n = len(out)
    if where == "last_data":
        out[n - SIG_FRAMES - 1] = (out[n - SIG_FRAMES - 1][0], True, out[n - SIG_FRAMES - 1][2])
        out[-1] = (out[-1][0], True, out[-1][2])
    elif where == "first_sig":
        out[n - SIG_FRAMES] = (out[n - SIG_FRAMES][0], True, out[n - SIG_FRAMES][2])
    elif where == "third_sig":
        out[n - 2] = (out[n - 2][0], True, out[n - 2][2])
    elif where == "none":
        pass
    elif where == "both":
        out[n - 2] = (out[n - 2][0], True, out[n - 2][2])
        out[-1] = (out[-1][0], True, out[-1][2])
    else:
        raise ValueError(where)
    return out


def digest_variants(payloads):
    """Messages a wrong implementation might sign instead of the 16-byte
    digest; all give a valid ECDSA signature that does not verify."""
    d = stream_digest(payloads)
    return {
        "right_padded_32": d + bytes(16),                    # zeros appended, not prepended
        "truncated_15": d[:15],
        "sha256_of_digest": hashlib.sha256(d).digest(),
        "sha256_of_payloads": hashlib.sha256(b"".join(payloads)).digest(),
        "no_rotate": _digest_no_rotate(payloads),
    }


def _digest_no_rotate(payloads):
    digest = bytes(16)
    for p in payloads:
        digest = bytes(a ^ b for a, b in zip(digest, p))
    return digest


# ---------------------------------------------------------------- strict collector (reference)

COLLECTOR_VERDICTS = ("complete_valid", "complete_invalid_signature", "incomplete", "sequence_error",
                      "size_error", "bad_public_key", "bad_signature_encoding")
TAIL_FNS = (0x7FFC, 0x7FFD, 0x7FFE, 0xFFFF)          # as sent: the EOT bit is part of the last one


def collect_stream(packets, public_xy):
    """Reference of a strict collector (the rules DV-Router's signature collector
    states), for the packets of a SIGNED stream in arrival order. The 16-bit FN
    field is compared as sent (EOT bit included):
      * a packet that is not 54 bytes                      -> "size_error"
      * FN below 0x7FFC must be exactly 0, 1, 2, ... in order and only before
        the tail started; FN 0x7FFC or above must be exactly the next of
        7FFC, 7FFD, 7FFE, FFFF; anything else (gap, duplicate, early tail,
        reordered, extra packet after the tail)          -> "sequence_error"
      * the tail never enters the digest
      * fewer than four tail chunks at the end             -> "incomplete"
      * public key not 64 bytes or not on the curve        -> "bad_public_key"
      * r or s zero or not below n                         -> "bad_signature_encoding"
      * verification false / true                -> "complete_invalid_signature" / "complete_valid"
    """
    expected = 0
    tail = []
    digest = bytes(16)
    for pkt in packets:
        if len(pkt) != m17.PACKET_SIZE:
            return "size_error"
        fn = int.from_bytes(pkt[34:36], "big")
        payload = pkt[36:52]
        if fn >= SIG_FN_FIRST:
            if len(tail) >= SIG_FRAMES or fn != TAIL_FNS[len(tail)]:
                return "sequence_error"
            tail.append(payload)
        else:
            if tail or fn != expected:
                return "sequence_error"
            digest = digest_step(digest, payload)
            expected += 1
    if len(tail) < SIG_FRAMES:
        return "incomplete"
    try:
        ecdsa_p256._point(public_xy)
    except ecdsa_p256.EcdsaError:
        return "bad_public_key"
    sig = b"".join(tail)
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if not (1 <= r < ecdsa_p256.N and 1 <= s < ecdsa_p256.N):
        return "bad_signature_encoding"
    return "complete_valid" if ecdsa_p256.verify_digest(public_xy, digest, sig) else "complete_invalid_signature"
