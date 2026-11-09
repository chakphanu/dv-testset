# SPDX-License-Identifier: MIT
"""RF-side frames of the virtual MMDVM modem (dvtest/mmdvm_modem.py): what a
radio's transmission looks like to MMDVMHost, and the decoders that read back
what MMDVMHost writes to the modem. D-STAR, DMR and YSF live here; P25 and NXDN
are in mmdvm_rf_p25.py and mmdvm_rf_nxdn.py.

A key-up is a list of (frame_type, payload, delay_ms) tuples: frame_type is the
MMDVM serial type byte, payload the bytes after it (the part MMDVMHost queues,
MMDVMHost Modem.cpp:700-760 and the mode controllers' writeModem), delay_ms the
air time to wait after the frame before the next one.

Only the facts of the formats are used (ETSI TS 102 361-1 for DMR, the JARL
D-STAR specification, the Yaesu System Fusion frame as dvtest/proto/ysf.py
already models it); nothing is copied from MMDVMHost. Where this module had to
agree with MMDVMHost to the bit, the pinned source is cited as
"MMDVMHost File.cpp:LINE" (commit deb5efe6) and the encoders were cross-checked
once against the compiled upstream decoders (docs/mmdvm-stack-cases.md).

What is real: D-STAR header with CRC-16/X-25, scrambled-free AMBE frames with
valid Golay words, the 21 frame superframe with the sync pattern; DMR voice LC
header and terminator (BPTC (196,96), RS (12,9) with the type masks, Golay
(20,8) slot type, data sync), voice bursts A..F with valid AMBE+2 words and the
embedded LC; YSF header, V/D mode 2 voice frames and terminator with FICH, CSD
and VCH coding. What is simplified: no RSSI bytes, no slow data text (null
filler), no GPS/talker alias.
"""
from .proto import dmr as dmrp
from .proto import nxdn as nxdnp
from .proto import ysf as ysfp

# MMDVM serial frame types (MMDVMHost Modem.cpp:46-90)
DSTAR_HEADER, DSTAR_DATA, DSTAR_LOST, DSTAR_EOT = 0x10, 0x11, 0x12, 0x13
DMR_DATA1, DMR_LOST1, DMR_DATA2, DMR_LOST2 = 0x18, 0x19, 0x1A, 0x1B
YSF_DATA, YSF_LOST = 0x20, 0x21


def _bits(data, count=None):
    out = []
    for byte in data:
        out.extend((byte >> (7 - i)) & 1 for i in range(8))
    return out[:count] if count is not None else out


def _from_bits(bits):
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def _bytes_from_bits(bits):
    padded = list(bits) + [0] * (-len(bits) % 8)
    return bytes(_from_bits(padded[i:i + 8]) for i in range(0, len(padded), 8))


def _pad(text, size):
    if isinstance(text, str):
        text = text.encode("ascii")
    return bytes(text)[:size].ljust(size, b" ")


# ------------------------------------------------------------------ D-STAR

DSTAR_HEADER_LEN = 41                       # MMDVMHost DStarDefines.h:23
DSTAR_FRAME_LEN = 12                        # 9 AMBE + 3 slow data, DStarDefines.h:24
DSTAR_SYNC = bytes((0x55, 0x2D, 0x16))      # DStarDefines.h:67
DSTAR_NULL_SLOW = bytes((0x16, 0x29, 0xF5))  # already scrambled filler, DStarDefines.h:41
DSTAR_REPEATER_FLAG = 0x40                  # flag 1 bit "repeater", DStarDefines.h:57
# 72-bit AMBE word of D-STAR: 12 rows x 6 columns, block A in columns 0-1,
# B in 2-3, C in 4-5 (position = 6 * row + column); A and B are extended Golay
# (24,12) words, B is scrambled by a PRNG seeded with the data of A, C is
# uncoded (MMDVMHost AMBEFEC.cpp:454-459 and regenerateDStar 585 / 808)
_DSTAR_A = [6 * row for row in range(12)] + [6 * row + 1 for row in range(12)]
_DSTAR_B = [6 * row + 2 for row in range(12)] + [6 * row + 3 for row in range(12)]
_DSTAR_C = [6 * row + 4 for row in range(12)] + [6 * row + 5 for row in range(12)]
# a transmitted frame of a mode with no real silence: the AMBE null word
DSTAR_NULL_AMBE = bytes((0x9E, 0x8D, 0x32, 0x88, 0x26, 0x1A, 0x3F, 0x61, 0xE8))


def crc16_x25(data):
    """CRC-16/X-25 (reflected 0x1021, preset and final xor 0xFFFF) as an int."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def dstar_header(my, my2="    ", rpt1="N0CALL B", rpt2="N0CALL G", ur="CQCQCQ",
                 flags=(DSTAR_REPEATER_FLAG, 0, 0)):
    """41-byte RF header: flags 3, RPT2 8, RPT1 8, YOUR 8, MY 8, MY2 4, CRC 2
    (low byte first). RPT1 must be the hotspot's callsign + module and RPT2
    its gateway callsign + G, or MMDVMHost drops the call
    (MMDVMHost DStarControl.cpp:165-195, 267)."""
    body = bytes(flags) + _pad(rpt2, 8) + _pad(rpt1, 8) + _pad(ur, 8) + _pad(my, 8) + _pad(my2, 4)
    return body + crc16_x25(body).to_bytes(2, "little")


def parse_dstar_header(header):
    body = bytes(header)
    return {"flags": tuple(body[0:3]), "rpt2": body[3:11].decode("ascii", "replace"),
            "rpt1": body[11:19].decode("ascii", "replace"), "ur": body[19:27].decode("ascii", "replace"),
            "my": body[27:35].decode("ascii", "replace"), "my2": body[35:39].decode("ascii", "replace"),
            "crc_ok": len(body) == DSTAR_HEADER_LEN and crc16_x25(body[:39]).to_bytes(2, "little") == body[39:41]}


def _gather(data, positions):
    value = 0
    for pos in positions:
        value = (value << 1) | ((data[pos >> 3] >> (7 - (pos & 7))) & 1)
    return value


def _scatter(buf, positions, value):
    count = len(positions)
    for index, pos in enumerate(positions):
        bit = (value >> (count - 1 - index)) & 1
        mask = 0x80 >> (pos & 7)
        if bit:
            buf[pos >> 3] |= mask
        else:
            buf[pos >> 3] &= ~mask & 0xFF


def _dstar_mask(a12):
    # the 24-bit scrambling mask of block B: the AMBE+2 PRNG sequence
    # x0 = 16 a, x(i) = (173 x(i-1) + 13849) mod 65536, bit i = x(i) >= 32768,
    # for i = 1..24 (nxdn.prng_mask gives the first 23)
    x = 16 * a12
    mask = 0
    for _ in range(24):
        x = (173 * x + 13849) % 65536
        mask = (mask << 1) | (x >> 15)
    return mask


def dstar_ambe(frame9):
    """The nearest valid D-STAR AMBE word: the data bits of frame9 with fresh
    Golay parity, so MMDVMHost's FEC regeneration leaves it unchanged."""
    frame9 = bytes(frame9)
    a12 = _gather(frame9, _DSTAR_A) >> 12
    b24 = _gather(frame9, _DSTAR_B) ^ _dstar_mask(a12)
    b12 = b24 >> 12
    out = bytearray(9)
    _scatter(out, _DSTAR_A, nxdnp.golay24(a12))
    _scatter(out, _DSTAR_B, nxdnp.golay24(b12) ^ _dstar_mask(a12))
    _scatter(out, _DSTAR_C, _gather(frame9, _DSTAR_C))
    return bytes(out)


# DStarGateway (CRepeaterHandler::processRepeater, Common/RepeaterHandler.cpp:752-757, and CDTMF::decode, DStarBase/DTMF.cpp, masks in
# DStarBase/DTMF.h:49-68) blanks a voice frame that looks like a DTMF tone to the null AMBE word 9e8d3288261a3f61e8, on by default:
# eight fixed bits of the nine bytes select it and every value of the four symbol bits is one of the 16 keys, so a random word
# is blanked with probability 1/256 (44 % of a 150 frame over). A byte-exact check must not use such words.
DSTAR_DTMF_MASK = bytes((0x82, 0x08, 0x20, 0x82, 0x00, 0x00, 0x82, 0x00, 0x00))
DSTAR_DTMF_SIG = bytes((0x82, 0x08, 0x20, 0x82, 0x00, 0x00, 0x00, 0x00, 0x00))


def dstar_dtmf_like(frame9):
    """True when DStarGateway takes the (valid, as the gateway sees it: see dstar_ambe) word for a DTMF key and blanks it."""
    frame9 = bytes(frame9)
    return len(frame9) == 9 and all((b & m) == s for b, m, s in zip(frame9, DSTAR_DTMF_MASK, DSTAR_DTMF_SIG))


def dstar_keyup(frames, my="N0CALL", my2="    ", rpt1="N0CALL B", rpt2="N0CALL G", ur="CQCQCQ", slow=None):
    """RF key-up: header, one 12-byte frame per 20 ms (frame 0 of every
    21 carries the sync pattern, the others the slow data `slow(n)` or the null
    filler), end of transmission. `frames` are 9-byte AMBE frames; they are
    returned in valid form by dstar_ambe, so keyup_frames(...) tells what the
    network side must see."""
    out = [(DSTAR_HEADER, dstar_header(my, my2, rpt1, rpt2, ur), 60.0)]
    for index, frame in enumerate(frames):
        phase = index % 21
        if phase == 0:
            tail = DSTAR_SYNC
        else:
            tail = bytes(slow(index)) if slow else DSTAR_NULL_SLOW
        out.append((DSTAR_DATA, dstar_ambe(frame) + tail, 20.0))
    out.append((DSTAR_EOT, b"", 20.0))
    return out


def decode_dstar(frames):
    """Downlink: the header and the voice MMDVMHost wrote (type 0x10 / 0x11 /
    0x13, MMDVMHost Modem.cpp:1420-1455)."""
    out = {"headers": [], "voice": [], "slow": [], "eot": 0}
    for ftype, payload in frames:
        if ftype == DSTAR_HEADER and len(payload) >= DSTAR_HEADER_LEN:
            out["headers"].append(parse_dstar_header(payload[:DSTAR_HEADER_LEN]))
        elif ftype == DSTAR_DATA and len(payload) >= DSTAR_FRAME_LEN:
            out["voice"].append(bytes(payload[:9]))
            out["slow"].append(bytes(payload[9:12]))
        elif ftype == DSTAR_EOT:
            out["eot"] += 1
    return out


# ------------------------------------------------------------------ DMR

DMR_BURST = 33                               # MMDVMHost DMRDefines.h:22
DMR_SYNC_DATA, DMR_SYNC_AUDIO = 0x40, 0x20   # first payload byte of a burst, DMRDefines.h:98-99
DT_VOICE_LC_HEADER, DT_TERMINATOR_WITH_LC = 1, 2
MS_VOICE_SYNC = bytes.fromhex("7F7D5DD57DFD")
MS_DATA_SYNC = dmrp.MS_DATA
BS_DATA_SYNC = dmrp.BS_DATA
BS_VOICE_SYNC = dmrp.BS_VOICE
VOICE_LC_MASK = (0x96, 0x96, 0x96)           # DMRDefines.h:70-71
TERMINATOR_LC_MASK = (0x99, 0x99, 0x99)
# slot type (CC 4 bits + data type 4 bits) with 12 parity bits, one row per
# information bit from the MSB: the Golay (20,8,6) generator, ETSI TS 102 361-1 B.3.1
SLOT_TYPE_PARITY = (0x3DA, 0xD99, 0x6CD, 0x367, 0xDC6, 0xA97, 0x93E, 0x8EB)
# Hamming (15,11,3) row checks and Hamming (13,9,3) column checks of the BPTC (B.2.1);
# each entry lists the data positions xor-ed into one parity bit
_HAM_15_11 = ((0, 1, 2, 3, 5, 7, 8), (1, 2, 3, 4, 6, 8, 9), (2, 3, 4, 5, 7, 9, 10), (0, 1, 2, 4, 6, 7, 10))
_HAM_13_9 = ((0, 1, 3, 5, 6), (0, 1, 2, 4, 6, 7), (0, 1, 2, 3, 5, 7, 8), (0, 2, 4, 5, 8))
# BPTC matrix positions of the 96 information bits (position 0 is R(0), rows of 15 follow)
_BPTC_DATA = list(range(4, 12)) + [pos for start in (16, 31, 46, 61, 76, 91, 106, 121)
                                   for pos in range(start, start + 11)]
_GF_POLY = 0x11D
_RS_GEN = (1, 14, 56, 64)                    # x^3 + 14 x^2 + 56 x + 64, roots alpha^1..alpha^3 (DMRDefines / RS129.cpp:28)


def _gf_mul(a, b):
    out = 0
    while b:
        if b & 1:
            out ^= a
        a <<= 1
        if a & 0x100:
            a ^= _GF_POLY
        b >>= 1
    return out


def rs129_parity(lc9):
    """The three RS (12,9) parity bytes of a 9-byte LC, highest power first."""
    reg = [0, 0, 0]
    for byte in lc9:
        feedback = byte ^ reg[0]
        reg = [reg[1] ^ _gf_mul(feedback, _RS_GEN[1]), reg[2] ^ _gf_mul(feedback, _RS_GEN[2]),
               _gf_mul(feedback, _RS_GEN[3])]
    return bytes(reg)


def bptc196_encode(info12):
    """196 transmitted bits of 12 information bytes: matrix of 13 rows x 15
    columns behind R(0), Hamming row and column parity, interleave i -> 181 i mod 196."""
    mat = [0] * 196
    for pos, bit in zip(_BPTC_DATA, _bits(info12, 96)):
        mat[pos] = bit
    for row in range(9):
        base = 1 + 15 * row
        for k, taps in enumerate(_HAM_15_11):
            mat[base + 11 + k] = sum(mat[base + t] for t in taps) & 1
    for col in range(15):
        column = [mat[1 + col + 15 * r] for r in range(9)]
        for k, taps in enumerate(_HAM_13_9):
            mat[1 + col + 15 * (9 + k)] = sum(column[t] for t in taps) & 1
    raw = [0] * 196
    for index in range(196):
        raw[(index * 181) % 196] = mat[index]
    return raw


def bptc196_decode(raw):
    """Information bytes of 196 received bits (no error correction)."""
    mat = [raw[(index * 181) % 196] for index in range(196)]
    return _bytes_from_bits([mat[pos] for pos in _BPTC_DATA])


def slot_type_bits(colour_code, data_type):
    data = ((colour_code & 0xF) << 4) | (data_type & 0xF)
    parity = 0
    for index, row in enumerate(SLOT_TYPE_PARITY):
        if (data >> (7 - index)) & 1:
            parity ^= row
    word = (data << 12) | parity
    return [(word >> (19 - i)) & 1 for i in range(20)]


def lc_info(lc9, data_type):
    """The 12 bytes the BPTC carries: LC, then RS parity xor the type mask."""
    mask = VOICE_LC_MASK if data_type == DT_VOICE_LC_HEADER else TERMINATOR_LC_MASK
    parity = rs129_parity(lc9)
    return bytes(lc9) + bytes(p ^ m for p, m in zip(parity, mask))


def data_burst(lc9, data_type, colour_code=1, sync=MS_DATA_SYNC):
    """33-byte voice LC header / terminator burst: 98 BPTC bits, 10 slot type
    bits, 48 sync bits, 10 slot type bits, 98 BPTC bits (burst layout of
    MMDVMHost BPTC19696.cpp:76-110, ETSI TS 102 361-1 figure 9.2)."""
    raw = bptc196_encode(lc_info(lc9, data_type))
    slot = slot_type_bits(colour_code, data_type)
    bits = raw[:98] + slot[:10] + _bits(sync, 48) + slot[10:] + raw[98:]
    return _bytes_from_bits(bits)


def parse_data_burst(burst):
    """{"cc", "dt", "lc", "rs_ok"} of a data-sync burst; lc is the 9 LC bytes
    for data types 1 and 2, else None."""
    bits = _bits(burst, 264)
    slot = _from_bits(bits[98:108] + bits[156:166])
    data, parity = slot >> 12, slot & 0xFFF
    expect = 0
    for index, row in enumerate(SLOT_TYPE_PARITY):
        if (data >> (7 - index)) & 1:
            expect ^= row
    out = {"cc": data >> 4, "dt": data & 0xF, "lc": None, "rs_ok": None, "slot_ok": expect == parity}
    if out["dt"] in (DT_VOICE_LC_HEADER, DT_TERMINATOR_WITH_LC):
        info = bptc196_decode(bits[:98] + bits[166:])
        mask = VOICE_LC_MASK if out["dt"] == DT_VOICE_LC_HEADER else TERMINATOR_LC_MASK
        lc = info[:9]
        out["lc"] = bytes(lc)
        out["rs_ok"] = bytes(b ^ m for b, m in zip(info[9:12], mask)) == rs129_parity(lc)
    return out


def _burst_center(burst):
    return _from_bits(_bits(burst, 264)[108:156])


def _set_center(burst, pattern48):
    bits = _bits(burst, 264)
    bits[108:156] = [(pattern48 >> (47 - i)) & 1 for i in range(48)]
    return _bytes_from_bits(bits)


def dmr_ambe(frame9):
    """The nearest valid DMR AMBE+2 word (Golay A, scrambled Golay B)."""
    return nxdnp.to_dmr(nxdnp.from_dmr(bytes(frame9)))


def dmr_keyup(frames, src, dst, group=True, slot=2, colour_code=1, embedded_lc=True, headers=1):
    """RF key-up on a slot: voice LC header(s) (data sync), voice bursts A..F
    (three AMBE+2 words each, burst A carries the voice sync, B..F the EMB and
    the embedded LC), terminator with LC. First payload byte: 0x40 | data type
    for sync data, 0x20 for burst A, 1..5 for B..F (MMDVMHost Modem.cpp:785,
    DMRSlot.cpp:644-680)."""
    ftype = DMR_DATA2 if slot == 2 else DMR_DATA1
    lc = dmrp.full_lc(dmrp.FLCO_GROUP if group else dmrp.FLCO_PRIVATE, src, dst)
    fragments = dmrp.encode_embedded_lc(lc) if embedded_lc else [0, 0, 0, 0]
    out = []
    for _ in range(headers):
        out.append((ftype, bytes((DMR_SYNC_DATA | DT_VOICE_LC_HEADER,)) +
                    data_burst(lc, DT_VOICE_LC_HEADER, colour_code), 60.0))
    pool = [dmr_ambe(f) for f in frames]
    while len(pool) % 3:
        pool.append(bytes(nxdnp.SILENCE_DMR))
    for index in range(0, len(pool), 3):
        vseq = (index // 3) % 6
        burst = dmrp.voice_payload(*pool[index:index + 3])
        if vseq == 0:
            burst = _set_center(burst, int.from_bytes(MS_VOICE_SYNC, "big"))
            first = DMR_SYNC_AUDIO
        else:
            burst = dmrp.with_embedded(burst, vseq, fragments, colour_code)
            first = vseq
        out.append((ftype, bytes((first,)) + burst, 60.0))
    out.append((ftype, bytes((DMR_SYNC_DATA | DT_TERMINATOR_WITH_LC,)) +
                data_burst(lc, DT_TERMINATOR_WITH_LC, colour_code), 60.0))
    return out


_SYNC_PATTERNS = {"data": (BS_DATA_SYNC, MS_DATA_SYNC), "voice": (BS_VOICE_SYNC, MS_VOICE_SYNC)}


def classify_burst(burst):
    """"data", "voice_sync" or "voice" (an EMB burst B..F) from the centre."""
    centre = _burst_center(burst)
    for kind, patterns in _SYNC_PATTERNS.items():
        for pat in patterns:
            if bin(centre ^ int.from_bytes(pat, "big")).count("1") <= 6:
                return "data" if kind == "data" else "voice_sync"
    return "voice"


def decode_dmr(frames):
    """Downlink: per slot the LC headers and terminators, the voice bursts and
    the 9-byte voice words MMDVMHost wrote (types 0x18 / 0x1A, payload = one
    byte + 33 burst bytes, MMDVMHost Modem.cpp:1473-1525)."""
    out = {1: _dmr_empty(), 2: _dmr_empty()}
    for ftype, payload in frames:
        if ftype not in (DMR_DATA1, DMR_DATA2) or len(payload) < 1 + DMR_BURST:
            continue
        slot = out[2 if ftype == DMR_DATA2 else 1]
        burst = bytes(payload[1:1 + DMR_BURST])
        kind = classify_burst(burst)
        slot["bursts"] += 1
        if kind == "data":
            info = parse_data_burst(burst)
            if info["dt"] == DT_VOICE_LC_HEADER and info["lc"]:
                slot["headers"].append(dict(dmrp.parse_full_lc(info["lc"]), cc=info["cc"], rs_ok=info["rs_ok"]))
            elif info["dt"] == DT_TERMINATOR_WITH_LC and info["lc"]:
                slot["terminators"].append(dict(dmrp.parse_full_lc(info["lc"]), cc=info["cc"], rs_ok=info["rs_ok"]))
            else:
                slot["other"] += 1
        else:
            slot["voice_bursts"] += 1
            slot["voice"].extend(dmrp.extract(burst))
    return out


def _dmr_empty():
    return {"headers": [], "terminators": [], "voice_bursts": 0, "voice": [], "other": 0, "bursts": 0}


# ------------------------------------------------------------------ YSF

YSF_FRAME = 120                              # MMDVMHost YSFDefines.h; payload = 0x00 + frame (YSFControl.cpp:150-165)


def ysf_ambe(frame9):
    """The 9-byte word a V/D mode 2 voice channel carries unchanged (49 data
    bits; dvtest/proto/ysf.py vch_encode / vch_decode)."""
    return ysfp.vch_decode(ysfp.vch_encode(bytes(frame9)))


def ysf_keyup(frames, src="N0CALL", dest="ALL", dgid=0, downlink="", uplink=""):
    """RF key-up of a V/D mode 2 call: header, a voice frame per five AMBE
    frames (100 ms), terminator. The frames are the 9-byte DMR form as in
    dvtest/proto/ysf.py."""
    out = [(YSF_DATA, b"\x00" + ysfp.header_frame(src, dgid, dest, downlink, uplink), 100.0)]
    pool = [ysf_ambe(f) for f in frames]
    while len(pool) % ysfp.FRAMES_PER_PACKET:
        pool.append(ysfp.SILENCE)
    for number, index in enumerate(range(0, len(pool), ysfp.FRAMES_PER_PACKET)):
        fn = number % 7
        dch = ysfp.dch_text(fn, src, downlink, dest)
        out.append((YSF_DATA, b"\x00" + ysfp.voice_frame(fn, pool[index:index + ysfp.FRAMES_PER_PACKET], dch, dgid, 6), 100.0))
    out.append((YSF_DATA, b"\x00" + ysfp.terminator_frame(src, dgid, dest, downlink, uplink), 100.0))
    return out


def decode_ysf(frames):
    """Downlink: header / terminator callsigns (CSD) and the voice words of
    the YSF frames MMDVMHost wrote (type 0x20)."""
    out = {"headers": [], "terminators": [], "voice": [], "frames": 0, "fich": []}
    for ftype, payload in frames:
        if ftype != YSF_DATA or len(payload) < 1 + YSF_FRAME:
            continue
        frame = bytes(payload[1:1 + YSF_FRAME])
        fich = ysfp.fich_decode(frame[5:5 + ysfp.FICH_SIZE])
        out["frames"] += 1
        out["fich"].append(fich)
        if fich is None:
            continue
        body = frame[5 + ysfp.FICH_SIZE:]
        if fich["fi"] in (ysfp.FI_HEADER, ysfp.FI_TERMINATOR):
            csd1, csd2 = ysfp.fr_fields(body)
            item = {"dest": csd1[:10].decode("ascii", "replace") if csd1 else None,
                    "src": csd1[10:20].decode("ascii", "replace") if csd1 else None,
                    "dgid": fich["sq"]}
            out["headers" if fich["fi"] == ysfp.FI_HEADER else "terminators"].append(item)
        elif fich["fi"] == ysfp.FI_COMMUNICATIONS and fich["dt"] == ysfp.DT_VD_MODE2:
            out["voice"].extend(ysfp.vd2_frames(body))
    return out


# ------------------------------------------------------------------ per mode helpers

FRAME_WIDTH = {"dstar": 9, "dmr": 9, "ysf": 9, "p25": 11, "nxdn": 7}   # bytes of one voice word per mode
PAD_TO = {"dstar": 1, "dmr": 3, "ysf": ysfp.FRAMES_PER_PACKET, "p25": 9, "nxdn": 4}  # whole bursts / frames / LDUs / RF frames
PAD_WORD = {"dstar": DSTAR_NULL_AMBE, "dmr": bytes(nxdnp.SILENCE_DMR), "ysf": bytes(ysfp.SILENCE)}


def canonical_frames(mode, frames):
    """The voice words as they cross the air interface unchanged (valid FEC
    where the host regenerates it); padding is not added."""
    if mode == "dstar":
        return [dstar_ambe(f) for f in frames]
    if mode == "dmr":
        return [dmr_ambe(f) for f in frames]
    if mode == "ysf":
        return [ysf_ambe(f) for f in frames]
    if mode == "nxdn":                       # 49 bits: the last byte keeps its top bit only
        return [bytes(f[:6]) + bytes((f[6] & 0x80,)) for f in frames]
    return [bytes(f) for f in frames]


def padded_frames(mode, frames):
    """canonical_frames plus the padding the transmission adds (whole DMR
    bursts of three, YSF frames of five): the count a listener hears through a
    reflector that relays every frame. P25 and NXDN pad with their own silence
    word (mmdvm_rf_p25 / mmdvm_rf_nxdn); here they are returned unpadded."""
    out = canonical_frames(mode, frames)
    width = PAD_TO[mode]
    if mode in PAD_WORD:
        while len(out) % width:
            out.append(PAD_WORD[mode])
    return out
