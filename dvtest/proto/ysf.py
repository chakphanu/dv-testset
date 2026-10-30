# SPDX-License-Identifier: MIT
"""YSF (Yaesu System Fusion) over the YSF reflector network protocol
(UDP 42000), as xlxd speaks it to a YSFGateway / hotspot.

Packets:

  YSFP   14 B  'YSFP' + callsign (10, space padded)        poll / link
  ack    14 B  'YSFPREFLECTOR '                             xlxd answer
  YSFU   14 B  'YSFU' + callsign (10)                       unlink
  YSFS    4 B  'YSFS'                                       status request
  status 42 B  'YSFS' + hash (5 digits) + name (16) + description (14)
               + client count (3 digits)
  YSFD  155 B  data packet:

    [0:4]     'YSFD'
    [4:14]    gateway callsign (xlxd matches WiresX commands on it;
              outbound: the stream's rpt1)
    [14:24]   source callsign (the talker xlxd reports)
    [24:34]   destination ('ALL' space padded)
    [34]      net frame counter << 1 | end-of-transmission bit
    [35:155]  one 120-byte YSF frame:
      [35:40]   sync D4 71 C9 63 4D
      [40:65]   FICH, 25 B
      [65:155]  payload, 90 B = five 18-byte blocks

FICH: four field bytes + CRC-16 CCITT (poly 0x1021, init 0, inverted, big
endian) = 48 bits, cut in four 12-bit words, each Golay(24,12) encoded;
the 96 bits + 4 zero tail bits go through the rate 1/2, K=5 convolutional
code (G1 = 1 + D^3 + D^4, G2 = 1 + D + D^2 + D^4) and the 200 coded bits
are interleaved: dibit i lands at bit 40 * (i % 5) + 2 * (i // 5).

  byte 0   FI(2) CS(2) CM(2) BN(2)
  byte 1   BT(2) FN(3) FT(3)
  byte 2   reserved(1) Dev(1) MR(3) VoIP(1) DT(2)
  byte 3   SQL(1) SQ(7)          SQ carries the DG-ID

FI: 0 header, 1 communications, 2 terminator, 3 test.
DT: 0 V/D mode 1, 1 data FR mode, 2 V/D mode 2 (DN), 3 voice FR mode.

Payload, V/D mode 2 (FI 1): each block = DCH 5 B + VCH 13 B.
  DCH: 10 text bytes (whitened) + CRC-16, 4 tail bits, convolution,
       interleave over 5 columns (as the FICH); 25 B, 5 B per block.
  VCH: one AMBE+2 frame without FEC (a 12, b 12, c 25 bits) in 104 bits:
       a, b and the top 3 bits of c with every bit sent three times
       (81 bits), the low 22 bits of c, one zero bit; whitened, then bit i
       goes to 4 * (i % 26) + i // 26. xlxd reads the middle copy.
Payload, header / terminator (FI 0 / 2) and data FR mode: each block =
  Data1 9 B + Data2 9 B. Data1 and Data2 each carry 20 bytes (whitened) +
  CRC-16 + 4 tail bits, convolution, interleave over 9 columns of 40
  bits; 45 B, 9 B per block. In a header Data1 is CSD1 = destination (10)
  + source (10), Data2 is CSD2 = downlink (10) + uplink (10).
Whitening is the PN9 sequence x^9 + x^5 + 1 whose first nine bits are
100100111 (bytes 93 D7 51 21 ...).

What xlxd does with a client:
  - link: a 14-byte YSFP with a valid callsign (first 8 characters) gets
    the ack and adds the client, auto-linked to module B. xlxd never
    polls the client: the client re-polls (YSFGateway every 5 s) and is
    dropped after 30 s without a poll or traffic.
  - YSFU: ignored by this xlxd (no unlink handler); a YSFReflector honours
    it. The working unlink is the WiresX disconnect request (DISC_REQ
    5D 2A 5F) in a data FR mode packet, FI 1, FN = FT = 1, gateway field
    = the polled callsign: about one second later xlxd answers with a
    WiresX reply and sets the client's module to ' '.
  - DV: xlxd only takes YSFD packets whose FICH decodes (Viterbi, Golay
    correction, CRC) and whose DT is V/D mode 2. FI 0 header: the
    payload CSD1 CRC must check (CSD2 is not checked), the source is
    taken from bytes 14..24, not from the CSD; a FICH SQ (DG-ID) of
    10..35 moves the client to module 'A' + SQ - 10 before the stream
    opens, any other SQ keeps the current module. FI 1: five VCH are
    decoded, the DCH is ignored, FN / FT / byte 34 are not checked. FI 2:
    closes the stream, payload ignored.
  - outbound: header FI 0 FN 0 FT 7, voice FI 1 FT 6 with FN = packet
    index, terminator FI 2 FN 0 FT 7 and byte 34 = 0x01; CS 2, MR busy,
    SQ 0. A stream's last voice packet is padded with all-zero 9-byte
    frames.
  - gain: xlxd moves the AMBE+2 gain index b2 by +1.0 dB on the way in
    (YSF -> other modes) and by -0.4 dB on the way out, so frames do not
    come back bit exact; compare with nxdn.without_gain().

Frames at the API are 9-byte DMR-form AMBE+2 frames (as DmrClient and
AMBEd codec 2 use); nxdn.from_dmr() / to_dmr() convert to and from the
49-bit form carried in the VCH.
"""
import time

from ..net import UdpClient
from .nxdn import from_dmr, golay24, join, split, to_dmr

PORT = 42000
CALLSIGN = "N0CALL"
POLL_SIZE = 14
STATUS_SIZE = 42
PACKET_SIZE = 155
FRAME_SIZE = 120
FICH_SIZE = 25
PAYLOAD_SIZE = 90
BLOCK_SIZE = 18
FRAMES_PER_PACKET = 5
PACKET_GAP = 0.100
SYNC = bytes((0xD4, 0x71, 0xC9, 0x63, 0x4D))
ACK = b"YSFPREFLECTOR "
ALL = "ALL"

FI_HEADER, FI_COMMUNICATIONS, FI_TERMINATOR, FI_TEST = 0, 1, 2, 3
DT_VD_MODE1, DT_DATA_FR_MODE, DT_VD_MODE2, DT_VOICE_FR_MODE = 0, 1, 2, 3
CM_GROUP, CM_INDIVIDUAL = 0, 3
MR_NOT_BUSY, MR_BUSY = 1, 2
DG_ID_BASE = 10                     # DG-ID 10 + n selects module 'A' + n

# AMBE+2 silence in DMR form (49 bits F8 01 A9 9F 8C E0 80); a valid
# codeword, so it survives the VCH round trip, unlike bytes(9)
SILENCE = bytes.fromhex("B9E881526173002A6B")

WIRESX_DISC_REQ = bytes((0x5D, 0x2A, 0x5F))
WIRESX_CONN_REQ = bytes((0x5D, 0x23, 0x5F))
WIRESX_END = 0x03


# ------------------------------------------------------------------ bits

def _bits(data, count=None):
    count = len(data) * 8 if count is None else count
    return [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(count)]


def _bytes(bits):
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index >> 3] |= 0x80 >> (index & 7)
    return bytes(out)


def _int_bits(value, count):
    return [(value >> (count - 1 - k)) & 1 for k in range(count)]


def _bits_int(bits):
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def _pn9(count):
    """Whitening bits: s(i) = s(i-5) xor s(i-9), first nine 100100111."""
    seq = [1, 0, 0, 1, 0, 0, 1, 1, 1]
    while len(seq) < count:
        seq.append(seq[-5] ^ seq[-9])
    return seq[:count]


WHITENING = _bytes(_pn9(160))       # 20 bytes


def _whiten(data):
    return bytes(b ^ w for b, w in zip(data, WHITENING))


def crc16(data):
    """CRC-16 CCITT as YSF uses it: poly 0x1021, init 0, MSB first,
    result inverted."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def _with_crc(data):
    return bytes(data) + crc16(data).to_bytes(2, "big")


def _crc_ok(data):
    return crc16(data[:-2]) == int.from_bytes(data[-2:], "big")


# ------------------------------------------------------------------ Golay

GOLAY_POLY = 0xC75                  # same generator as nxdn.golay23


def _golay_syndrome(word23):
    for shift in range(22, 10, -1):
        if word23 & (1 << shift):
            word23 ^= GOLAY_POLY << (shift - 11)
    return word23


def _golay_table():
    """syndrome -> error pattern for every pattern of weight <= 3 (the
    code is perfect: exactly 2048 of them)."""
    table = {0: 0}
    for i in range(23):
        table[_golay_syndrome(1 << i)] = 1 << i
        for j in range(i):
            table[_golay_syndrome((1 << i) | (1 << j))] = (1 << i) | (1 << j)
            for k in range(j):
                pattern = (1 << i) | (1 << j) | (1 << k)
                table[_golay_syndrome(pattern)] = pattern
    return table


_GOLAY_ERRORS = _golay_table()


def golay24_decode(word24):
    """12 data bits of a Golay(24,12) word, up to 3 errors corrected; the
    overall parity bit is ignored (as xlxd does)."""
    word23 = word24 >> 1
    word23 ^= _GOLAY_ERRORS[_golay_syndrome(word23)]
    return word23 >> 11


# ------------------------------------------------------------------ convolution

def _conv_step(state, bit):
    """state = d1 d2 d3 d4 (d1 = newest). -> (g1, g2, next state)."""
    d1, d2, d3, d4 = (state >> 3) & 1, (state >> 2) & 1, (state >> 1) & 1, state & 1
    return bit ^ d3 ^ d4, bit ^ d1 ^ d2 ^ d4, (bit << 3) | (state >> 1)


_TRELLIS = [[_conv_step(state, bit) for bit in (0, 1)] for state in range(16)]


def conv_encode(bits):
    """Rate 1/2 K=5 encoder; the caller appends the 4 zero tail bits."""
    state = 0
    out = []
    for bit in bits:
        g1, g2, state = _TRELLIS[state][bit]
        out += (g1, g2)
    return out


def viterbi(coded, nbits):
    """Hard decision Viterbi decoder from and back to the zero state;
    returns the first nbits information bits."""
    inf = 1 << 30
    metric = [0] + [inf] * 15
    history = []
    for step in range(len(coded) // 2):
        s0, s1 = coded[2 * step], coded[2 * step + 1]
        new = [inf] * 16
        prev = [0] * 16
        for state in range(16):
            base = metric[state]
            if base >= inf:
                continue
            for g1, g2, nxt in _TRELLIS[state]:
                cost = base + (g1 ^ s0) + (g2 ^ s1)
                if cost < new[nxt]:
                    new[nxt] = cost
                    prev[nxt] = state
        metric = new
        history.append(prev)
    state = 0
    out = []
    for prev in reversed(history):
        out.append(state >> 3)
        state = prev[state]
    out.reverse()
    return out[:nbits]


def _dibit_order(columns):
    """Bit position of coded dibit i: 20 rows x columns, 40 bits a column."""
    return [40 * (i % columns) + 2 * (i // columns) for i in range(20 * columns)]


_ORDER_5 = _dibit_order(5)          # FICH and V/D mode 2 DCH
_ORDER_9 = _dibit_order(9)          # Data1 / Data2 (CSD)


def _interleave(coded, order):
    out = [0] * len(coded)
    for i, pos in enumerate(order):
        out[pos], out[pos + 1] = coded[2 * i], coded[2 * i + 1]
    return out


def _deinterleave(raw, order):
    out = []
    for pos in order:
        out += (raw[pos], raw[pos + 1])
    return out


def _fec_encode(data, order):
    """data (with CRC) -> tail, convolution, interleave -> bytes."""
    coded = conv_encode(_bits(data) + [0, 0, 0, 0])
    return _bytes(_interleave(coded, order))


def _fec_decode(raw, order, nbytes):
    bits = viterbi(_deinterleave(_bits(raw), order), nbytes * 8)
    return _bytes(bits)


# ------------------------------------------------------------------ FICH

FICH_FIELDS = ("fi", "cs", "cm", "bn", "bt", "fn", "ft", "dev", "mr", "voip", "dt", "sql", "sq")


def fich_bytes(fi, cs=2, cm=CM_GROUP, bn=0, bt=0, fn=0, ft=0, dev=False, mr=MR_BUSY,
               voip=False, dt=DT_VD_MODE2, sql=False, sq=0):
    """The four FICH field bytes."""
    return bytes((((fi & 3) << 6) | ((cs & 3) << 4) | ((cm & 3) << 2) | (bn & 3),
                  ((bt & 3) << 6) | ((fn & 7) << 3) | (ft & 7),
                  (0x40 if dev else 0) | ((mr & 7) << 3) | (0x04 if voip else 0) | (dt & 3),
                  (0x80 if sql else 0) | (sq & 0x7F)))


def parse_fich_bytes(raw):
    b0, b1, b2, b3 = raw[:4]
    return {"fi": b0 >> 6, "cs": (b0 >> 4) & 3, "cm": (b0 >> 2) & 3, "bn": b0 & 3,
            "bt": b1 >> 6, "fn": (b1 >> 3) & 7, "ft": b1 & 7,
            "dev": bool(b2 & 0x40), "mr": (b2 >> 3) & 7, "voip": bool(b2 & 0x04), "dt": b2 & 3,
            "sql": bool(b3 & 0x80), "sq": b3 & 0x7F}


def fich_encode(fi, cs=2, cm=CM_GROUP, bn=0, bt=0, fn=0, ft=0, dev=False, mr=MR_BUSY,
                voip=False, dt=DT_VD_MODE2, sql=False, sq=0):
    """25-byte coded FICH. Defaults follow what xlxd sends (CS 2, group,
    MR busy, V/D mode 2)."""
    raw = _with_crc(fich_bytes(fi, cs, cm, bn, bt, fn, ft, dev, mr, voip, dt, sql, sq))
    bits = _bits(raw)
    words = []
    for k in range(4):
        words += _int_bits(golay24(_bits_int(bits[12 * k:12 * k + 12])), 24)
    coded = conv_encode(words + [0, 0, 0, 0])
    return _bytes(_interleave(coded, _ORDER_5))


def fich_decode(data):
    """dict of a 25-byte FICH (see FICH_FIELDS), or None when the CRC
    fails after Viterbi and Golay correction."""
    if len(data) != FICH_SIZE:
        return None
    bits = viterbi(_deinterleave(_bits(data), _ORDER_5), 96)
    plain = []
    for k in range(4):
        plain += _int_bits(golay24_decode(_bits_int(bits[24 * k:24 * k + 24])), 12)
    raw = _bytes(plain)
    if not _crc_ok(raw):
        return None
    return parse_fich_bytes(raw)


# ------------------------------------------------------------------ DCH / CSD

def dch_encode(text10):
    """V/D mode 2 DCH: 10 bytes -> 25 coded bytes (5 per block)."""
    if len(text10) != 10:
        raise ValueError("a V/D mode 2 DCH carries 10 bytes")
    return _fec_encode(_with_crc(_whiten(text10)), _ORDER_5)


def dch_decode(coded25):
    """10 bytes of a coded DCH, or None on CRC failure."""
    raw = _fec_decode(coded25, _ORDER_5, 12)
    return _whiten(raw[:10]) if _crc_ok(raw) else None


def csd_encode(data20):
    """Data1 / Data2 (CSD) of a header, terminator or data FR frame:
    20 bytes -> 45 coded bytes (9 per block)."""
    if len(data20) != 20:
        raise ValueError("a CSD / data FR field carries 20 bytes")
    return _fec_encode(_with_crc(_whiten(data20)), _ORDER_9)


def csd_decode(coded45):
    """20 bytes of a coded Data1 / Data2 field, or None on CRC failure."""
    raw = _fec_decode(coded45, _ORDER_9, 22)
    return _whiten(raw[:20]) if _crc_ok(raw) else None


# ------------------------------------------------------------------ VCH

def _vch_order(i):
    return 4 * (i % 26) + i // 26


def vch_encode(frame9):
    """9-byte DMR-form AMBE+2 frame -> 13-byte V/D mode 2 VCH. Like xlxd
    the DMR FEC is dropped without correction (nxdn.from_dmr)."""
    a12, b12, c25 = split(from_dmr(frame9))
    bits = []
    for value, count in ((a12, 12), (b12, 12), (c25 >> 22, 3)):
        for bit in _int_bits(value, count):
            bits += (bit, bit, bit)
    bits += _int_bits(c25 & 0x3FFFFF, 22) + [0]
    bits = [b ^ w for b, w in zip(bits, _bits(WHITENING, 104))]
    out = [0] * 104
    for i, bit in enumerate(bits):
        out[_vch_order(i)] = bit
    return _bytes(out)


def vch_decode(vch13):
    """13-byte VCH -> 9-byte DMR-form frame (middle copy of each tripled
    bit, as xlxd reads it; FEC and scrambling recomputed)."""
    raw = _bits(vch13, 104)
    bits = [raw[_vch_order(i)] ^ w for i, w in enumerate(_bits(WHITENING, 104))]
    a12 = _bits_int(bits[3 * i + 1] for i in range(12))
    b12 = _bits_int(bits[3 * i + 1] for i in range(12, 24))
    c25 = (_bits_int(bits[3 * i + 1] for i in range(24, 27)) << 22) | _bits_int(bits[81:103])
    return to_dmr(join(a12, b12, c25))


# ------------------------------------------------------------------ payloads

def _spread(chunks, size, offset, payload):
    for block in range(FRAMES_PER_PACKET):
        start = BLOCK_SIZE * block + offset
        payload[start:start + size] = chunks[size * block:size * (block + 1)]


def _gather(payload, size, offset):
    return b"".join(payload[BLOCK_SIZE * block + offset:BLOCK_SIZE * block + offset + size]
                    for block in range(FRAMES_PER_PACKET))


def vd2_payload(frames, dch10):
    """90-byte V/D mode 2 payload: five 9-byte frames and a 10-byte DCH."""
    if len(frames) != FRAMES_PER_PACKET:
        raise ValueError("a YSF V/D mode 2 frame carries 5 voice frames")
    out = bytearray(PAYLOAD_SIZE)
    _spread(dch_encode(dch10), 5, 0, out)
    for block, frame in enumerate(frames):
        start = BLOCK_SIZE * block + 5
        out[start:start + 13] = vch_encode(frame)
    return bytes(out)


def vd2_frames(payload):
    """The five 9-byte DMR-form frames of a V/D mode 2 payload."""
    return [vch_decode(payload[BLOCK_SIZE * block + 5:BLOCK_SIZE * (block + 1)])
            for block in range(FRAMES_PER_PACKET)]


def vd2_dch(payload):
    """The 10-byte DCH of a V/D mode 2 payload, or None on CRC failure."""
    return dch_decode(_gather(payload, 5, 0))


def fr_payload(data1, data2):
    """90-byte payload of a header / terminator (CSD1, CSD2) or a data FR
    mode frame (Data1, Data2), 20 bytes each."""
    out = bytearray(PAYLOAD_SIZE)
    _spread(csd_encode(data1), 9, 0, out)
    _spread(csd_encode(data2), 9, 9, out)
    return bytes(out)


def fr_fields(payload):
    """(data1, data2) of a header / data FR payload, None where the CRC
    fails."""
    return csd_decode(_gather(payload, 9, 0)), csd_decode(_gather(payload, 9, 9))


# ------------------------------------------------------------------ frames

def _cs(callsign, size=10):
    if isinstance(callsign, str):
        callsign = callsign.upper().encode("ascii")
    return bytes(callsign)[:size].ljust(size, b" ")


def module_dgid(module):
    """DG-ID that makes xlxd move a client to module (A -> 10 ... Z -> 35)."""
    return DG_ID_BASE + ord(module.upper()) - ord("A")


def dgid_module(dgid):
    """Module a DG-ID selects on xlxd, or None (no module change)."""
    return chr(ord("A") + dgid - DG_ID_BASE) if DG_ID_BASE <= dgid < DG_ID_BASE + 26 else None


def frame(fich25, payload90):
    """120-byte YSF frame: sync + FICH + payload."""
    return SYNC + bytes(fich25) + bytes(payload90)


def csd(src, dest="**********", downlink="", uplink=""):
    """(CSD1, CSD2) of a header: destination + source, downlink + uplink."""
    return _cs(dest) + _cs(src), _cs(downlink) + _cs(uplink)


def header_frame(src, dgid=0, dest="**********", downlink="", uplink="", fn=0, ft=7,
                 fi=FI_HEADER, dt=DT_VD_MODE2):
    """Header (FI 0) frame with CSD1 / CSD2; dgid goes in the FICH SQ."""
    csd1, csd2 = csd(src, dest, downlink, uplink)
    return frame(fich_encode(fi, fn=fn, ft=ft, dt=dt, sq=dgid), fr_payload(csd1, csd2))


def terminator_frame(src, dgid=0, dest="**********", downlink="", uplink="", fn=0, ft=7):
    """Terminator (FI 2) frame, same payload as the header."""
    return header_frame(src, dgid, dest, downlink, uplink, fn, ft, fi=FI_TERMINATOR)


def dch_text(fn, src, gateway="", dest="**********"):
    """10-byte DCH a radio sends in V/D mode 2 frame fn: destination,
    source, downlink, uplink, then blanks."""
    return {0: _cs(dest), 1: _cs(src), 2: _cs(gateway), 3: _cs(gateway)}.get(fn, _cs(""))


def voice_frame(fn, frames, dch10=None, dgid=0, ft=6):
    """Communications (FI 1) V/D mode 2 frame: five 9-byte frames, the
    10-byte DCH (blanks by default), FN fn (0..7)."""
    fich = fich_encode(FI_COMMUNICATIONS, fn=fn, ft=ft, sq=dgid)
    return frame(fich, vd2_payload(frames, dch10 if dch10 is not None else _cs("")))


# ------------------------------------------------------------------ packets

def poll_packet(callsign):
    return b"YSFP" + _cs(callsign)


def unlink_packet(callsign):
    """YSFReflector unlink; this xlxd ignores it (see wiresx_disc_packets)."""
    return b"YSFU" + _cs(callsign)


def is_poll_ack(data):
    return bytes(data) == ACK


def parse_poll(data):
    """dict of a 14-byte YSFP / YSFU packet, or None (the ack parses as a
    poll from 'REFLECTOR')."""
    if len(data) != POLL_SIZE or data[:4] not in (b"YSFP", b"YSFU"):
        return None
    return {"kind": "poll" if data[3:4] == b"P" else "unlink",
            "callsign": data[4:14].decode("ascii", "replace").rstrip()}


def status_request():
    return b"YSFS"


def parse_status(data):
    """dict of a 42-byte YSFS status reply, or None."""
    if len(data) != STATUS_SIZE or not data.startswith(b"YSFS"):
        return None
    text = data.decode("ascii", "replace")
    try:
        count = int(text[39:42])
    except ValueError:
        count = None
    return {"hash": text[4:9], "name": text[9:25].rstrip(), "description": text[25:39].rstrip(),
            "count": count}


def packet(gateway, src, dst, counter, frame120, eot=False):
    """155-byte YSFD packet; counter is the 7-bit net frame counter."""
    if len(frame120) != FRAME_SIZE:
        raise ValueError("a YSF frame is 120 bytes")
    tail = ((counter & 0x7F) << 1) | (1 if eot else 0)
    return b"YSFD" + _cs(gateway) + _cs(src) + _cs(dst) + bytes((tail,)) + bytes(frame120)


def parse(data):
    """dict of a 155-byte YSFD packet, or None.

    Always: gateway, src, dst, counter, eot, sync_ok, fich (dict or None),
    kind. kind is header / voice / terminator for V/D mode 2 packets (the
    only DV xlxd takes), data for data FR mode (WiresX), other for
    any other DT, None when the FICH does not decode. voice adds frames
    (five 9-byte frames) and dch (10 B or None); header / terminator add
    csd1 / csd2 (20 B or None); data adds data1 / data2."""
    if len(data) != PACKET_SIZE or not data.startswith(b"YSFD"):
        return None
    data = bytes(data)

    def text(raw):
        return raw.decode("ascii", "replace").rstrip()

    info = {"gateway": text(data[4:14]), "src": text(data[14:24]), "dst": text(data[24:34]),
            "counter": data[34] >> 1, "eot": bool(data[34] & 1), "sync_ok": data[35:40] == SYNC,
            "fich": fich_decode(data[40:65]), "kind": None}
    fich = info["fich"]
    if fich is None:
        return info
    payload = data[65:155]
    if fich["dt"] == DT_VD_MODE2:
        if fich["fi"] == FI_COMMUNICATIONS:
            info.update(kind="voice", frames=vd2_frames(payload), dch=vd2_dch(payload))
        elif fich["fi"] in (FI_HEADER, FI_TERMINATOR):
            csd1, csd2 = fr_fields(payload)
            info.update(kind="header" if fich["fi"] == FI_HEADER else "terminator",
                        csd1=csd1, csd2=csd2)
        else:
            info["kind"] = "other"
    elif fich["dt"] == DT_DATA_FR_MODE:
        data1, data2 = fr_fields(payload)
        info.update(kind="data", data1=data1, data2=data2)
    else:
        info["kind"] = "other"
    return info


def is_voice(data):
    info = parse(data)
    return info is not None and info["kind"] == "voice"


def wire_frames(frames, pad=SILENCE):
    """Frames a talk of frames puts on the wire (padded to whole packets)."""
    frames = list(frames)
    return frames + [pad] * (-len(frames) % FRAMES_PER_PACKET)


def stream_packets(frames, callsign, dgid=0, gateway=None, dst=ALL, pad=SILENCE):
    """Header, V/D mode 2 voice packets (FN 0..6 cycling, frames padded
    with pad to a multiple of five) and terminator (EOT bit set); net
    counter 0 on the header, then +1 per packet."""
    gateway = callsign if gateway is None else gateway
    frames = wire_frames(frames, pad)
    out = [packet(gateway, callsign, dst, 0, header_frame(callsign, dgid, downlink=gateway))]
    for index in range(0, len(frames), FRAMES_PER_PACKET):
        fn = (index // FRAMES_PER_PACKET) % 7
        body = voice_frame(fn, frames[index:index + FRAMES_PER_PACKET],
                           dch_text(fn, callsign, gateway), dgid)
        out.append(packet(gateway, callsign, dst, len(out), body))
    out.append(packet(gateway, callsign, dst, len(out),
                      terminator_frame(callsign, dgid, downlink=gateway), eot=True))
    return out


# ------------------------------------------------------------------ WiresX

def wiresx_command(command, seq=0):
    """20-byte Data2 block of a single-frame WiresX request: seq, command,
    end marker 0x03, 8-bit sum, blanks. xlxd searches the end marker from
    the top, so nothing after it may be 0x03."""
    body = bytes((seq & 0xFF,)) + bytes(command)
    if (sum(body) + WIRESX_END) & 0xFF == WIRESX_END:
        body += b" "
    body += bytes((WIRESX_END,))
    body += bytes(((sum(body)) & 0xFF,))
    if len(body) > 20:
        raise ValueError("WiresX command too long for one frame")
    return body.ljust(20, b" ")


def parse_wiresx_command(data20):
    """(command bytes 1..3, body up to the end marker) of a Data2 block
    whose end marker and sum check, else None."""
    for end in range(len(data20) - 2, 0, -1):
        if data20[end] == WIRESX_END:
            if sum(data20[:end + 1]) & 0xFF == data20[end + 1]:
                return bytes(data20[1:4]), bytes(data20[:end + 1])
            return None
    return None


def wiresx_packets(callsign, command, gateway=None):
    """A radio's WiresX request: data FR mode header, FN 0 (CSD), FN 1
    (Data2 = the command), terminator; FT 1. xlxd acts on the FN 1 packet
    only and finds the client by gateway field + IP."""
    gateway = callsign if gateway is None else gateway
    csd1, csd2 = csd(callsign, downlink=gateway)

    def data_frame(fi, fn, data1, data2):
        return frame(fich_encode(fi, fn=fn, ft=1, dt=DT_DATA_FR_MODE, mr=MR_NOT_BUSY),
                     fr_payload(data1, data2))

    frames = [data_frame(FI_HEADER, 0, csd1, csd2),
              data_frame(FI_COMMUNICATIONS, 0, csd1, csd2),
              data_frame(FI_COMMUNICATIONS, 1, _cs("", 20), wiresx_command(command)),
              data_frame(FI_TERMINATOR, 1, csd1, csd2)]
    return [packet(gateway, callsign, ALL, index, body, eot=index == len(frames) - 1)
            for index, body in enumerate(frames)]


def wiresx_disc_packets(callsign, gateway=None):
    """WiresX disconnect request (DISC_REQ 5D 2A 5F): xlxd sets the
    client's module to ' ' and answers with a data FR mode reply."""
    return wiresx_packets(callsign, WIRESX_DISC_REQ, gateway)


# ------------------------------------------------------------------ client

class YsfClient:
    """A YSF hotspot / gateway on an xlxd module (DG-ID selects it)."""

    def __init__(self, bind_ip, callsign=CALLSIGN, module="B"):
        self.client = UdpClient(bind_ip)
        self.callsign = callsign.upper()
        self.module = module.upper()
        self.dest = None

    @property
    def dgid(self):
        return module_dgid(self.module)

    def link(self, node_ip, timeout=2.5, port=PORT, join_gap=0.3):
        """Poll until xlxd acks, then join the module (header with the
        module's DG-ID + terminator)."""
        self.dest = (node_ip, port)
        poll = poll_packet(self.callsign)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.send(self.dest, poll)
            self.client.pump(0.3)
            if any(is_poll_ack(data) for data in self.client.inbox):
                self.join(None, join_gap)
                return True
        return False

    def join(self, module=None, gap=0.3, settle=0.5):
        """Move to a module (default: the current one): an empty stream,
        header with DG-ID 10 + module index, then terminator (well inside
        xlxd's stream timeout, like DmrClient.join)."""
        if module is not None:
            self.module = module.upper()
        packets = stream_packets([], self.callsign, self.dgid)
        self.client.send(self.dest, packets[0])
        self.client.pump(gap)
        self.client.send(self.dest, packets[-1])
        self.client.pump(settle)

    def keepalive(self):
        """Re-poll; xlxd drops a YSF client after 30 s without one."""
        self.client.send(self.dest, poll_packet(self.callsign))

    def unlink(self, wait=1.5):
        """WiresX disconnect (xlxd: module ' ' after about 1 s), then YSFU
        for a YSFReflector (xlxd ignores it)."""
        self.client.paced(self.dest, wiresx_disc_packets(self.callsign), PACKET_GAP)
        self.client.send(self.dest, unlink_packet(self.callsign))
        self.client.pump(wait)

    def talk(self, frames, stream_id=None, settle=0.3, tail=2.0, gap=PACKET_GAP, pad=SILENCE):
        """frames: 9-byte DMR-form AMBE+2 frames, five per packet every
        100 ms, the last packet padded with pad (SILENCE, a valid codeword;
        xlxd itself pads with bytes(9)). The header carries the module's
        DG-ID so the talk lands on self.module even after an unlink.
        stream_id is ignored: YSF has no stream id (xlxd derives one per
        source IP); the parameter keeps the client API symmetric."""
        packets = stream_packets(frames, self.callsign, self.dgid, pad=pad)
        self.keepalive()
        self.client.send(self.dest, packets[0])
        self.client.pump(settle)
        self.client.paced(self.dest, packets[1:], gap)
        self.client.pump(tail)

    def packets(self):
        """Parsed YSFD packets received so far, in arrival order."""
        return [p for p in (parse(d) for d in self.client.inbox) if p]

    def collect(self, settle=1.5):
        """9-byte frames of the received V/D mode 2 voice packets, in
        arrival order."""
        self.client.pump(settle)
        out = []
        for info in self.packets():
            if info["kind"] == "voice":
                out.extend(info["frames"])
        return out

    def close(self):
        self.client.close()


# ------------------------------------------------------------------ conformance helpers
# (docs/protocols/ysf.md, cases/proto/test_ysf.py)

WIRESX_DX_REQ = bytes((0x5D, 0x71, 0x5F))
WIRESX_ALL_REQ = bytes((0x5D, 0x66, 0x5F))
# reply codes a WiresX-answering reflector sends (first bytes of its data)
WIRESX_REPLIES = {"dx": bytes((0x5D, 0x51, 0x5F, 0x26)), "conn": bytes((0x5D, 0x41, 0x5F, 0x26)),
                  "all": bytes((0x5D, 0x46, 0x5F, 0x26))}
OPTIONS_SIZE = 50
INFO_SIZE = 80


def wiresx_conn(room):
    """CONN_REQ for room 1..26 (xlxd: module A..Z): the room as 5 digits
    after one separator byte; xlxd reads the last 3 digits."""
    return WIRESX_CONN_REQ + b"\x5f" + b"%05d" % room


def raw_frame(fi, dt, payload90, fn=0, ft=7, sq=0, mr=MR_BUSY):
    """120-byte frame with a valid FICH for any data type and an opaque
    payload: for DT / FI cases where the payload coding does not matter
    to a forwarding reflector."""
    if len(payload90) != PAYLOAD_SIZE:
        raise ValueError("a YSF payload is 90 bytes")
    return frame(fich_encode(fi, fn=fn, ft=ft, dt=dt, sq=sq, mr=mr), payload90)


def dt_stream_packets(callsign, dt, count, sq=0, gateway=None, seed=1):
    """Header, count communications packets and terminator of data type
    dt (V/D mode 1, Data FR, Voice FR ...) with PRNG payloads; counter
    +1 per packet, end bit on the terminator."""
    gateway = callsign if gateway is None else gateway
    rng = (seed * 2654435761) & 0xFFFFFFFF
    bodies = []
    for index in range(count + 2):
        payload = bytearray()
        for _ in range(PAYLOAD_SIZE):
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            payload.append(rng >> 16 & 0xFF)
        bodies.append(bytes(payload))
    out = [packet(gateway, callsign, ALL, 0, raw_frame(FI_HEADER, dt, bodies[0], sq=sq))]
    for index in range(count):
        out.append(packet(gateway, callsign, ALL, len(out),
                          raw_frame(FI_COMMUNICATIONS, dt, bodies[index + 1], fn=index % 7, ft=6, sq=sq)))
    out.append(packet(gateway, callsign, ALL, len(out), raw_frame(FI_TERMINATOR, dt, bodies[-1], sq=sq),
                      eot=True))
    return out


def with_status(data, counter=None, eot=None):
    """A YSFD packet with its status byte (byte 34) changed."""
    data = bytearray(data)
    if counter is not None:
        data[34] = ((counter & 0x7F) << 1) | (data[34] & 1)
    if eot is not None:
        data[34] = (data[34] & 0xFE) | (1 if eot else 0)
    return bytes(data)


def corrupt_fich(data):
    """A YSFD packet whose FICH no longer decodes (every FICH byte inverted)."""
    data = bytearray(data)
    for pos in range(40, 65):
        data[pos] ^= 0xFF
    return bytes(data)


def options_packet(callsign, options):
    """YSFO (50 B): callsign + options text (YSFGateway: a DG-ID list such as "20;21;")."""
    text = options.encode("ascii") if isinstance(options, str) else bytes(options)
    return b"YSFO" + _cs(callsign) + text[:OPTIONS_SIZE - 14].ljust(OPTIONS_SIZE - 14, b" ")


def info_packet(callsign, rx_hz=430000000, tx_hz=430000000, locator="AA00AA", name="dvtest",
                kind="dvtest", gateway_id=0):
    """YSFI (80 B): callsign, RX/TX frequency (9 digits each), locator (6),
    name (20), hotspot type (12), gateway id (7), filler (3)."""
    body = (_cs(callsign) + b"%09d" % rx_hz + b"%09d" % tx_hz + _cs(locator, 6) + _cs(name, 20)
            + _cs(kind, 12) + b"%07d" % gateway_id + b"   ")
    return b"YSFI" + body


def status_layout(data):
    """Field-form checks of a status reply: {size, id_digits, count_digits}."""
    data = bytes(data)
    return {"size": len(data), "id_digits": data[4:9].isdigit(),
            "count_digits": len(data) >= 42 and data[39:42].isdigit()}


def wiresx_reply_kind(packets):
    """Which WiresX reply ("dx", "conn", "all") the data FR packets carry,
    or None: searches the concatenated Data1/Data2 blocks."""
    blob = b"".join((info.get("data1") or b"") + (info.get("data2") or b"")
                    for info in packets if info and info.get("kind") == "data")
    for name, code in WIRESX_REPLIES.items():
        if code in blob:
            return name
    return None
