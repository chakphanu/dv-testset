# SPDX-License-Identifier: MIT
"""P25 phase 1 RF frames as an MMDVM modem delivers them to MMDVMHost, and the
reverse decoder for what MMDVMHost writes back to the modem.

Purpose: a virtual modem (dvtest/mmdvm_modem.py) makes the real MMDVMHost believe
a P25 radio keyed up on RF. This module only builds and parses the frame
PAYLOADS; the serial framing [0xE0][len][type] is the modem's job.

What is real (written from TIA-102.BAAA / BABA / AABF and the frame layout that
MMDVMHost P25Control.cpp / P25Data.cpp / P25Audio.cpp consume; pinned MMDVMHost
deb5efe, no GPL code copied, algorithms re-expressed):
  * 48 bit frame sync, 64 bit NID (NAC, DUID, BCH(63,16) parity, parity bit)
  * status symbols (2 bits at every 72nd bit position 70/71)
  * HDU: RS(36,20,17) over GF(64) + Golay(18,6) per hexword
  * LDU1 link control: RS(24,12,13) + Hamming(10,6,3); LDU2 crypto sync:
    RS(24,16,9) + Hamming(10,6,3)
  * IMBE voice: Golay(23,12) x4, Hamming(15,11) x3, PN scrambling of c1..c6,
    144 bit interleave -- so 11 byte IMBE frames survive MMDVMHost's re-FEC
  * low speed data (CCS (16,8) parity), TDU terminator
Simplified: link control carries only the group/private voice call formats
(LCF 0x00 / 0x03); no encryption (MI is carried, never applied); no RSSI trailer;
no talker alias / soft id / TSBK / PDU frames; the status symbols are a fixed
"inbound busy" (0,1); NID parity bit follows what the host emits per DUID.
The decoder (decode_downlink) does hard decisions only: it strips parity
without correcting, because the host generates clean frames.

MMDVM payload layout (Modem.cpp:701-727 readSerial, Modem.cpp:1577-1586
writeP25Data): after the type byte (0x30 HDR / 0x31 LDU) comes ONE flag byte
(modem to host: 0x01 = sync seen; host to modem: 0x00) and then the raw frame:
HDU 99 bytes, LDU 216, TDU 18. The TDU travels as type 0x31 (host tag EOT/DATA);
only its NID is looked at. An optional 2 byte RSSI trailer (len 220, P25Control.cpp:241)
is not generated. Host side rules this module relies on: a frame is ignored while
listening unless the flag byte is 0x01 (P25Control.cpp:170,210); the call starts at the
first LDU1 whose RS(24,12,13) link control decodes (P25Control.cpp:280) and, unless
the host has UID override, only if the source id is in its lookup table
(P25Control.cpp:304-305); an HDU is only logged (P25Control.cpp:266); the host
forwards each LDU one frame late, so the TDU is what flushes the last LDU to the
network (P25Control.cpp:560).
"""

FRAME_HDR = 0x30      # MMDVM_P25_HDR
FRAME_LDU = 0x31      # MMDVM_P25_LDU

# --- constants from the pinned MMDVMHost (P25Defines.h) ----------------------
HDR_BYTES = 99        # P25Defines.h:23
LDU_BYTES = 216       # P25Defines.h:26
TERM_BYTES = 18       # P25Defines.h:29
TERMLC_BYTES = 54     # P25Defines.h:32
TSDU_BYTES = 45       # P25Defines.h:35
SYNC = bytes((0x55, 0x75, 0xF5, 0xFF, 0x77, 0xFF))      # P25Defines.h:44
NULL_IMBE = bytes((0x04, 0x0C, 0xFD, 0x7B, 0xFB, 0x7D, 0xF2, 0x7B, 0x3D, 0x9E, 0x45))  # P25Defines.h:79
DUID_HDU, DUID_TDU, DUID_LDU1, DUID_TSDU, DUID_LDU2, DUID_PDU, DUID_TDULC = (
    0x00, 0x03, 0x05, 0x07, 0x0A, 0x0C, 0x0F)            # P25Defines.h:71-77
LCF_GROUP, LCF_PRIVATE = 0x00, 0x03                      # P25Defines.h:60,62
ALGO_UNENCRYPTED = 0x80                                  # P25Defines.h:58
SS_DIBIT = (False, True)   # status symbol dibit written by the host, P25Control.cpp:373 addBusyBits(false, true) "inbound busy"

# air time in ms: bits / 9600 (TIA-102.BAAA 9600 bit/s)
LDU_AIR_MS = 180.0
HDU_AIR_MS = 99 * 8 / 9.6
TDU_AIR_MS = 18 * 8 / 9.6

# bit ranges inside the 1728 bit LDU, status symbol positions excluded when packing.
# P25Audio.cpp:111-168 (voice) and P25Data.cpp:126-148 (link control / crypto)
VOICE_RANGES = ((114, 262), (262, 410), (452, 600), (640, 788), (830, 978),
                (1020, 1168), (1208, 1356), (1398, 1546), (1578, 1726))
LC_RANGES = ((410, 452), (600, 640), (788, 830), (978, 1020), (1168, 1208), (1356, 1398))
LSD_RANGE = (1546, 1578)          # P25LowSpeedData.cpp:73
NID_RANGE = (48, 114)             # P25NID.cpp:143
HDR_RANGE = (114, 780)            # P25Data.cpp:79


# --- bit helpers --------------------------------------------------------------
def bytes_to_bits(data):
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def bits_to_bytes(bits):
    bits = list(bits) + [0] * (-len(bits) % 8)
    return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def int_to_bits(value, width):
    return [(value >> (width - 1 - i)) & 1 for i in range(width)]


def bits_to_int(bits):
    value = 0
    for b in bits:
        value = (value << 1) | b
    return value


def _is_status(pos):
    return pos % 72 in (70, 71)      # P25Defines.h:67-69 SS0 start 70, SS1 71, increment 72


def put_bits(frame, start, stop, bits):
    """Write bits into frame[start:stop], skipping status symbol positions
    (the role of CP25Utils::encode, P25Utils.cpp:73)."""
    slots = [p for p in range(start, stop) if not _is_status(p)]
    if len(slots) != len(bits):
        raise ValueError("range %d..%d holds %d bits, got %d" % (start, stop, len(slots), len(bits)))
    for pos, bit in zip(slots, bits):
        frame[pos] = bit


def get_bits(frame, start, stop):
    """Inverse of put_bits (CP25Utils::decode, P25Utils.cpp:33)."""
    return [frame[p] for p in range(start, stop) if not _is_status(p)]


def _set_status(frame, dibit=SS_DIBIT):
    for pos in range(70, len(frame), 72):
        frame[pos], frame[pos + 1] = int(dibit[0]), int(dibit[1])


# --- Golay(23,12), Hamming(15,11) / (10,6), IMBE FEC -----------------------------
GOLAY_POLY = 0xC75      # x^11+x^10+x^6+x^5+x^4+x^2+1, TIA-102.BAAA Golay(23,12)


def golay23127(data12):
    """12 data bits -> systematic 23 bit codeword (data in the high 12 bits)."""
    reg = (data12 & 0xFFF) << 11
    for bit in range(22, 10, -1):
        if reg & (1 << bit):
            reg ^= GOLAY_POLY << (bit - 11)
    return ((data12 & 0xFFF) << 11) | reg


def golay24128(data12):
    """Golay(23,12) plus an even overall parity bit in the LSB."""
    cw = golay23127(data12)
    return (cw << 1) | (bin(cw).count("1") & 1)


def golay18_6(hexword):
    """Shortened Golay(18,6,8) of a 6 bit value: the low 18 bits of the
    (24,12) codeword with the 6 high data bits zero (P25Data.cpp:624)."""
    return golay24128(hexword & 0x3F) & 0x3FFFF


_GOLAY_SYNDROMES = {}


def golay23127_decode(codeword):
    """Bounded distance decode (up to 3 errors): -> (data12, corrected_bits)."""
    if not _GOLAY_SYNDROMES:
        _GOLAY_SYNDROMES[0] = 0
        for weight in (1, 2, 3):
            for combo in _combos(23, weight):
                err = sum(1 << p for p in combo)
                _GOLAY_SYNDROMES.setdefault(golay23127(err >> 11) ^ err, err)
    syndrome = golay23127(codeword >> 11) ^ codeword
    err = _GOLAY_SYNDROMES.get(syndrome)
    if err is None:
        return codeword >> 11, 0
    return (codeword ^ err) >> 11, bin(err).count("1")


def _combos(n, k, start=0):
    if k == 0:
        yield ()
        return
    for i in range(start, n - k + 1):
        for rest in _combos(n, k - 1, i + 1):
            yield (i,) + rest


# parity equations as index sets over the data bits d0..d(k-1) (bit 0 transmitted first)
_HAM15_EQ = ((0, 1, 2, 3, 4, 5, 6), (0, 1, 2, 3, 7, 8, 9),
             (0, 1, 4, 5, 7, 8, 10), (0, 2, 4, 6, 7, 9, 10))     # Hamming.cpp:72-75
_HAM10_EQ = ((0, 1, 2, 5), (0, 1, 3, 5), (0, 2, 3, 4), (1, 2, 3, 4))   # Hamming.cpp:224-227


def hamming15113(d11):
    """11 data bits -> 15 bit codeword (list)."""
    return list(d11) + [sum(d11[i] for i in eq) & 1 for eq in _HAM15_EQ]


def hamming15113_decode(cw15):
    """Single error correction: -> (data11 list, corrected_bits)."""
    cw = list(cw15)
    synd = [(sum(cw[i] for i in eq) + cw[11 + n]) & 1 for n, eq in enumerate(_HAM15_EQ)]
    if not any(synd):
        return cw[:11], 0
    for pos in range(15):
        probe = [0] * 15
        probe[pos] = 1
        if [(sum(probe[i] for i in eq) + probe[11 + n]) & 1 for n, eq in enumerate(_HAM15_EQ)] == synd:
            cw[pos] ^= 1
            return cw[:11], 1
    return cw[:11], 0


def hamming1063(d6):
    """6 data bits -> 10 bit codeword (list)."""
    return list(d6) + [sum(d6[i] for i in eq) & 1 for eq in _HAM10_EQ]


def _interleave_table():
    # 6 rows x 24 columns (P25Audio.cpp:28-33): row r holds 12k + (a, b) for k = 0..11
    pairs = ((0, 7), (1, 6), (2, 9), (3, 8), (4, 11), (5, 10))
    return [12 * k + off for a, b in pairs for k in range(12) for off in (a, b)]


IMBE_INTERLEAVE = _interleave_table()
_IMBE_WORDS = ((0, 12), (12, 24), (24, 36), (36, 48), (48, 59), (59, 70), (70, 81), (81, 88))


def _pn_sequence(c0_data):
    """PN scrambling bits for c1..c6 (TIA-102.BABA): p(0) = 16 * u0,
    p(n) = (173 p(n-1) + 13849) mod 65536, bit = p >= 32768 (P25Audio.cpp:182)."""
    state = 16 * c0_data
    seq = []
    for _ in range(114):
        state = (173 * state + 13849) % 65536
        seq.append(1 if state >= 32768 else 0)
    return seq


def imbe_encode(imbe):
    """11 byte IMBE frame (88 bits u0..u7) -> 18 byte (144 bit) air codeword:
    FEC, whitening and interleave, status symbols not yet inserted."""
    if len(imbe) != 11:
        raise ValueError("an IMBE frame is 11 bytes")
    bits = bytes_to_bits(imbe)
    words = [bits_to_int(bits[a:b]) for a, b in _IMBE_WORDS]
    coded = []
    for w in words[:4]:
        coded += int_to_bits(golay23127(w), 23)
    for n in (4, 5, 6):
        coded += hamming15113(int_to_bits(words[n], 11))
    coded += int_to_bits(words[7], 7)
    pn = _pn_sequence(words[0])
    for i in range(114):
        coded[23 + i] ^= pn[i]
    out = [0] * 144
    for i, bit in enumerate(coded):
        out[IMBE_INTERLEAVE[i]] = bit
    return bits_to_bytes(out)


def imbe_decode(codeword, correct=True):
    """18 byte air codeword -> (11 byte IMBE frame, corrected_bit_count). With
    correct=False the parity is only stripped (hard decision)."""
    if len(codeword) != 18:
        raise ValueError("an IMBE codeword is 18 bytes")
    air = bytes_to_bits(codeword)
    coded = [air[IMBE_INTERLEAVE[i]] for i in range(144)]
    errs = 0
    c0 = bits_to_int(coded[:23])
    if correct:
        c0_data, n = golay23127_decode(c0)
        errs += n
    else:
        c0_data = c0 >> 11
    pn = _pn_sequence(c0_data)
    for i in range(114):
        coded[23 + i] ^= pn[i]
    out = []
    for w in range(4):
        cw = bits_to_int(coded[23 * w:23 * w + 23])
        if correct:
            data, n = golay23127_decode(cw)
            errs += n
        else:
            data = cw >> 11
        out += int_to_bits(data, 12)
    for w in range(3):
        cw = coded[92 + 15 * w:107 + 15 * w]
        data, n = hamming15113_decode(cw) if correct else (cw[:11], 0)
        errs += n
        out += list(data)
    out += coded[137:144]
    return bits_to_bytes(out), errs


def imbe_regenerate(codeword):
    """FEC fixed point: decode with correction and encode again (what MMDVMHost
    P25Audio::process does to each voice word) -> (codeword, corrected_bits)."""
    imbe, errs = imbe_decode(codeword)
    return imbe_encode(imbe), errs


# --- Reed-Solomon over GF(64) (primitive polynomial x^6+x+1) ------------------
_GF_EXP = []
_GF_LOG = [0] * 64


def _gf_init():
    value = 1
    for i in range(63):
        _GF_EXP.append(value)
        _GF_LOG[value] = i
        value <<= 1
        if value & 0x40:
            value ^= 0x43
    _GF_EXP.extend(_GF_EXP)


_gf_init()


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def rs_encode(message, nroots):
    """Systematic RS over GF(64), generator with roots alpha^1..alpha^nroots,
    hexwords in transmission order (highest degree first); parity appended."""
    gen = [1]
    for i in range(1, nroots + 1):
        nxt = [0] * (len(gen) + 1)
        for j, coef in enumerate(gen):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _GF_EXP[i])
        gen = nxt
    rem = [0] * nroots
    for sym in message:
        fb = sym ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(nroots):
            rem[j] ^= _gf_mul(fb, gen[j + 1])
    return list(message) + rem


def _hexwords(data):
    bits = bytes_to_bits(data)
    return [bits_to_int(bits[i:i + 6]) for i in range(0, len(bits), 6)]


def _hex_to_bits(words):
    return [b for w in words for b in int_to_bits(w, 6)]


# --- BCH(63,16,23) NID --------------------------------------------------------
NID_GEN = int("6331141367235453", 8)    # g(x), degree 47, TIA-102.BAAA NID BCH
_NID_PARITY_ONE = (DUID_LDU1, DUID_LDU2)    # P25NID.cpp:46,53 sets the last bit for LDUs only


def _nid_parity(data16):
    reg = data16 << 47
    for bit in range(62, 46, -1):
        if reg & (1 << bit):
            reg ^= NID_GEN << (bit - 47)
    return reg


def nid_bytes(nac, duid):
    """8 byte NID: NAC(12) DUID(4) BCH parity(47) + 1 bit."""
    if not 0 <= nac <= 0xFFF:
        raise ValueError("NAC is 12 bits")
    data16 = (nac << 4) | duid
    bits = int_to_bits(data16, 16) + int_to_bits(_nid_parity(data16), 47) + \
        [1 if duid in _NID_PARITY_ONE else 0]
    return bits_to_bytes(bits)


def nid_parse(nid):
    """8 byte NID -> (nac, duid) when it is a valid NID, else None."""
    data16 = bits_to_int(bytes_to_bits(nid)[:16])
    nac, duid = data16 >> 4, data16 & 0xF
    if nid_bytes(nac, duid) != bytes(nid):
        return None
    return nac, duid


# --- frame building -----------------------------------------------------------
def _new_frame(nbytes, nac, duid):
    frame = [0] * (nbytes * 8)
    frame[:48] = bytes_to_bits(SYNC)
    put_bits(frame, NID_RANGE[0], NID_RANGE[1], bytes_to_bits(nid_bytes(nac, duid)))
    return frame


def _put_lc_words(frame, hexwords):
    """24 hexwords -> six groups of four, each Hamming(10,6,3), into the LC ranges."""
    for g, (start, stop) in enumerate(LC_RANGES):
        bits = []
        for w in hexwords[4 * g:4 * g + 4]:
            bits += hamming1063(int_to_bits(w, 6))
        put_bits(frame, start, stop, bits)


def _get_lc_words(frame):
    words = []
    for start, stop in LC_RANGES:
        bits = get_bits(frame, start, stop)
        words += [bits_to_int(bits[10 * i:10 * i + 6]) for i in range(4)]
    return words


def _check_voice(voice):
    if len(voice) != 9 or any(len(v) != 11 for v in voice):
        raise ValueError("an LDU carries exactly 9 IMBE frames of 11 bytes")


def _put_voice(frame, voice, lsd):
    _check_voice(voice)
    for (start, stop), imbe in zip(VOICE_RANGES, voice):
        put_bits(frame, start, stop, bytes_to_bits(imbe_encode(imbe)))
    lsd1, lsd2 = lsd
    put_bits(frame, LSD_RANGE[0], LSD_RANGE[1],
             bytes_to_bits(bytes((lsd1, lsd_parity(lsd1), lsd2, lsd_parity(lsd2)))))


def lsd_parity(byte):
    """Low speed data check byte: byte * x^8 mod x^8+x^5+x^4+x^3+1, i.e. the
    (16,8) cyclic code of TIA-102.BAAA (table at P25LowSpeedData.cpp:27-44)."""
    reg = (byte & 0xFF) << 8
    for bit in range(15, 7, -1):
        if reg & (1 << bit):
            reg ^= 0x139 << (bit - 8)
    return reg & 0xFF


def link_control(src, dst, group=True, mfid=0, service_options=0):
    """9 byte LDU1 link control word (LCF 0x00 group / 0x03 unit to unit)."""
    if not 0 <= src <= 0xFFFFFF:
        raise ValueError("source id is 24 bits")
    if group:
        if not 0 <= dst <= 0xFFFF:
            raise ValueError("a group voice call carries a 16 bit talkgroup")
        return bytes((LCF_GROUP, mfid, service_options, 0, dst >> 8, dst & 0xFF,
                      src >> 16, (src >> 8) & 0xFF, src & 0xFF))
    if not 0 <= dst <= 0xFFFFFF:
        raise ValueError("destination id is 24 bits")
    return bytes((LCF_PRIVATE, mfid, service_options, dst >> 16, (dst >> 8) & 0xFF, dst & 0xFF,
                  src >> 16, (src >> 8) & 0xFF, src & 0xFF))


def build_hdu(nac, dst, mfid=0, algid=ALGO_UNENCRYPTED, key_id=0, mi=bytes(9)):
    """99 byte header data unit (P25Data.cpp:77-107 layout: MI 9, MFID, ALGID,
    KID 2, TGID 2 = 15 bytes -> RS(36,20,17) -> Golay(18,6) -> 648 bits)."""
    raw = bytes(mi) + bytes((mfid, algid, key_id >> 8, key_id & 0xFF, (dst >> 8) & 0xFF, dst & 0xFF))
    words = rs_encode(_hexwords(raw), 16)
    bits = [b for w in words for b in int_to_bits(golay18_6(w), 18)]
    frame = _new_frame(HDR_BYTES, nac, DUID_HDU)
    put_bits(frame, HDR_RANGE[0], HDR_RANGE[1], bits)
    _set_status(frame)
    return bits_to_bytes(frame)


def build_ldu1(nac, src, dst, voice, group=True, mfid=0, service_options=0, lsd=(0, 0)):
    """216 byte LDU1: link control + 9 IMBE + low speed data."""
    words = rs_encode(_hexwords(link_control(src, dst, group, mfid, service_options)), 12)
    frame = _new_frame(LDU_BYTES, nac, DUID_LDU1)
    _put_lc_words(frame, words)
    _put_voice(frame, voice, lsd)
    _set_status(frame)
    return bits_to_bytes(frame)


def build_ldu2(nac, voice, algid=ALGO_UNENCRYPTED, key_id=0, mi=bytes(9), lsd=(0, 0)):
    """216 byte LDU2: message indicator, algorithm id, key id + 9 IMBE + LSD."""
    raw = bytes(mi) + bytes((algid, key_id >> 8, key_id & 0xFF))
    words = rs_encode(_hexwords(raw), 8)
    frame = _new_frame(LDU_BYTES, nac, DUID_LDU2)
    _put_lc_words(frame, words)
    _put_voice(frame, voice, lsd)
    _set_status(frame)
    return bits_to_bytes(frame)


def build_tdu(nac):
    """18 byte simple terminator (sync + NID, rest zero), P25Control.cpp:562-571 (RF) and 1288-1311 (net)."""
    frame = _new_frame(TERM_BYTES, nac, DUID_TDU)
    _set_status(frame)
    return bits_to_bytes(frame)


def build_keyup(src, dst, imbe_frames, nac=0x293, group=True, mfid=0, service_options=0,
                algid=0x80, key_id=0, mi=bytes(9), with_hdu=True):
    """A whole transmission as modem frames in air order.

    Returns [(frame_type, payload, delay_ms)]. payload is the MMDVM payload
    (flag byte 0x01 = sync seen, then the raw frame). delay_ms is the wait after
    sending that frame: frames are delivered when they end on air, so it is the
    air time of the NEXT frame (180 ms per LDU, 0 after the last frame).
    HDU first (the host only logs it; LDU1 alone also starts the call), then
    LDU1/LDU2 alternating with 9 IMBE each, the last LDU padded with the null
    IMBE frame, then the TDU. The mi/algid/key_id fields go into HDU and LDU2.
    """
    frames = [bytes(f) for f in imbe_frames]
    if not frames:
        raise ValueError("need at least one IMBE frame")
    if any(len(f) != 11 for f in frames):
        raise ValueError("IMBE frames are 11 bytes")
    frames += [NULL_IMBE] * (-len(frames) % 9)
    out = [(FRAME_HDR, build_hdu(nac, dst if group else 0, mfid, algid, key_id, mi), HDU_AIR_MS)] \
        if with_hdu else []
    for n in range(len(frames) // 9):
        voice = frames[9 * n:9 * n + 9]
        if n % 2 == 0:
            frame = build_ldu1(nac, src, dst, voice, group, mfid, service_options)
        else:
            frame = build_ldu2(nac, voice, algid, key_id, mi)
        out.append((FRAME_LDU, frame, LDU_AIR_MS))
    out.append((FRAME_LDU, build_tdu(nac), TDU_AIR_MS))
    # delivered at the end of each frame: the wait after frame k is the air time of frame k+1
    return [(ftype, b"\x01" + frame, out[k + 1][2] if k + 1 < len(out) else 0.0)
            for k, (ftype, frame, _) in enumerate(out)]


# --- decoding what MMDVMHost writes to the modem --------------------------------
def decode_downlink(frames):
    """frames: [(frame_type, payload)] (extra tuple items such as delay_ms are
    ignored) as CModem::writeP25Data queues them
    (payload = flag byte 0x00 + raw frame; a bare frame is accepted too).
    Returns the call as dict: src, dst, group, nac, voice (11 byte frames in
    order, padding included), ldu_count, has_header, terminated, plus mfid,
    service_options, algid, key_id, mi, lsd (list of (lsd1, lsd2) per LDU) and
    duids (the DUID of every frame, None for an invalid NID)."""
    res = {"src": None, "dst": None, "group": None, "nac": None, "voice": [],
           "ldu_count": 0, "has_header": False, "terminated": False,
           "mfid": None, "service_options": None, "algid": None, "key_id": None,
           "mi": None, "lsd": [], "duids": []}
    for item in frames:
        payload = bytes(item[1])       # (type, payload); extra tuple items ignored
        if len(payload) in (HDR_BYTES, LDU_BYTES, TERM_BYTES, TERMLC_BYTES, TSDU_BYTES):
            frame = payload
        else:
            frame = payload[1:]
        if len(frame) < 14:
            res["duids"].append(None)
            continue
        bits = bytes_to_bits(frame)
        parsed = nid_parse(bits_to_bytes(get_bits(bits, NID_RANGE[0], NID_RANGE[1])))
        if parsed is None:
            res["duids"].append(None)
            continue
        nac, duid = parsed
        res["duids"].append(duid)
        if res["nac"] is None:
            res["nac"] = nac
        if duid == DUID_HDU and len(frame) >= HDR_BYTES:
            res["has_header"] = True
            raw = get_bits(bits, HDR_RANGE[0], HDR_RANGE[1])
            words = [bits_to_int(raw[18 * i:18 * i + 6]) for i in range(36)][:20]
            data = bits_to_bytes(_hex_to_bits(words))
            res["mi"] = data[:9]
            res["algid"], res["key_id"] = data[10], (data[11] << 8) | data[12]
            if res["mfid"] is None:
                res["mfid"] = data[9]
        elif duid in (DUID_LDU1, DUID_LDU2) and len(frame) >= LDU_BYTES:
            res["ldu_count"] += 1
            words = _get_lc_words(bits)
            if duid == DUID_LDU1:
                lc = bits_to_bytes(_hex_to_bits(words[:12]))
                res["mfid"], res["service_options"] = lc[1], lc[2]
                res["group"] = lc[0] == LCF_GROUP
                res["src"] = (lc[6] << 16) | (lc[7] << 8) | lc[8]
                res["dst"] = ((lc[4] << 8) | lc[5]) if res["group"] else (lc[3] << 16) | (lc[4] << 8) | lc[5]
            else:
                cs = bits_to_bytes(_hex_to_bits(words[:16]))
                res["mi"], res["algid"], res["key_id"] = cs[:9], cs[9], (cs[10] << 8) | cs[11]
            for start, stop in VOICE_RANGES:
                res["voice"].append(imbe_decode(bits_to_bytes(get_bits(bits, start, stop)))[0])
            lsd = bits_to_bytes(get_bits(bits, LSD_RANGE[0], LSD_RANGE[1]))
            res["lsd"].append((lsd[0], lsd[2]))
        elif duid in (DUID_TDU, DUID_TDULC):
            res["terminated"] = True
    return res
