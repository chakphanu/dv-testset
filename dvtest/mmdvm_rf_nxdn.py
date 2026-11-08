# SPDX-License-Identifier: MIT
"""NXDN air-interface frames for a virtual MMDVM modem.

A real radio keys up on RF, the modem firmware hands MMDVMHost one frame
per 80 ms and the host turns the call into an NXDN network stream. This
module builds those frames (build_keyup) and reads back what the host
sends to the modem on the downlink (decode_downlink).

Modem payload (the bytes after [0xE0][len][type]), per frame:

  [0]       0x00, the firmware's own tag byte; the host stores
            [TAG_DATA][payload] and runs writeModem on data + 2 = frame
            (MMDVMHost Modem.cpp:743-755, NXDNControl.cpp:151)
  [1:49]    384-bit frame, 48 bytes (NXDNDefines.h:24-25)
  [49:51]   optional raw RSSI, big endian (NXDNControl.cpp:129-132)

Frame, 384 bits = 80 ms at 4800 bit/s (the host counts 12.5 frames per
second, NXDNControl.cpp:281), after scrambling:

  bits   0..19   FSW 0xCDF59 (NXDNDefines.h:32-33)
  bits  20..35   LICH, 8 bits each followed by a 1 (NXDNLICH.cpp:77-83)
  bits  36..95   SACCH, 36 bits conv coded 1/2, punctured to 60, interleaved
  bits  96..239  block 1: FACCH1 (144 bits) or two voice frames (2 x 72)
  bits 240..383  block 2: same

FACCH1: layer 3 message 80 bits + CRC-12 + 4 zero bits = 96 bits, rate 1/2
convolution (K=5), 48 bits punctured out, 9 x 16 interleave. SACCH: the
same with 26 bits + CRC-6 + 4 zero bits and a 5 x 12 interleave. Voice:
the 72-bit AMBE+2 form with Golay(24,12) on a and scrambled Golay(23,12)
on b, which is exactly dvtest.proto.nxdn.to_dmr (NXDNAudio.cpp:478-580
uses the same tables as the DMR form).

What is real: every field above, the LICH/SACCH/FACCH1 coding, the
scrambler, the call sequence (header FACCH1 frame, voice frames with the
VCALL message spread over four SACCH parts, TX_REL trailer frame). What is
simplified: timing is a plain 80 ms per frame, there is no preamble or
radio sync, no late entry, no data (UDCH) calls, no encryption. The
frames are clean; decode_downlink is a hard-decision Viterbi that is only
good for clean frames.

The scrambler is a PN9 sequence (x^9 + x^4 + 1) flipping the first bit of
each symbol after the FSW; its phase (seed 0x0E4) was found by matching
the 48-byte table of NXDNControl.cpp:31-35, not taken from the standard.
"""
from .proto import nxdn

FRAME_DATA = 0x40                   # MMDVM_NXDN_DATA (Modem.cpp:85)
FRAME_LOST = 0x41                   # MMDVM_NXDN_LOST (Modem.cpp:86)

FRAME_BITS = 384                    # NXDN_FRAME_LENGTH_BITS
FRAME_BYTES = 48                    # NXDN_FRAME_LENGTH_BYTES
PAYLOAD_BYTES = FRAME_BYTES + 1     # leading tag byte + frame
FRAME_MS = 80.0                     # one frame of 384 bits at 4800 bit/s

FSW_BYTES = bytes([0xCD, 0xF5, 0x90])   # NXDN_FSW_BYTES
FSW_MASK = bytes([0xFF, 0xFF, 0xF0])    # NXDN_FSW_BYTES_MASK (20 bits)
FSW_BITS = 20
LICH_AT = 20
SACCH_AT = 36
BLOCK_AT = (96, 240)                # NXDN_FSW+LICH+SACCH, + FACCH1 length
FACCH1_BITS = 144
SACCH_BITS = 60
VOICE_BITS = 72

# NXDNSACCH.cpp:38 / NXDNFACCH1.cpp:41-45: coded bit positions not sent
SACCH_PUNCTURE = tuple(range(5, 72, 6))
FACCH1_PUNCTURE = tuple(range(1, 190, 4))

# SACCH structure field for superframe part 0..3 (NXDN_SR_1_4 = 3 ... 4_4 = 0)
SR_SINGLE = 0


# ------------------------------------------------------------------ bits

def to_bits(data, count=None):
    """List of bits, MSB first, of bytes (first count bits)."""
    count = len(data) * 8 if count is None else count
    return [(data[i >> 3] >> (7 - (i & 7))) & 1 for i in range(count)]


def to_bytes(bits):
    """Bytes of a list of bits, MSB first, zero padded at the end."""
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index >> 3] |= 0x80 >> (index & 7)
    return bytes(out)


def _pn9(count, seed=0x0E4):
    state, out = seed, []
    for _ in range(count):
        out.append(state & 1)
        state = (state >> 1) | ((((state >> 4) ^ state) & 1) << 8)
    return out


def _scrambler():
    bits = [0] * FRAME_BITS
    for symbol, bit in enumerate(_pn9((FRAME_BITS - FSW_BITS) // 2)):
        bits[FSW_BITS + 2 * symbol] = bit
    return to_bytes(bits)


SCRAMBLER = _scrambler()            # NXDNControl.cpp:31 SCRAMBLER[]


def scramble(frame):
    """XOR the 48-byte frame with the scrambler (its own inverse)."""
    if len(frame) != FRAME_BYTES:
        raise ValueError("an NXDN frame is %d bytes" % FRAME_BYTES)
    return bytes(a ^ b for a, b in zip(frame, SCRAMBLER))


# ------------------------------------------------------------------ coding

def conv_encode(bits):
    """Rate 1/2, K=5 convolutional code: g1 = d + d3 + d4, g2 = d + d1 +
    d2 + d4 (d1 = previous input), as 2 coded bits per input bit
    (NXDNConvolution.cpp:128-150; TS 1-A 5.1)."""
    d1 = d2 = d3 = d4 = 0
    out = []
    for d in bits:
        out.append(d ^ d3 ^ d4)
        out.append(d ^ d1 ^ d2 ^ d4)
        d4, d3, d2, d1 = d3, d2, d1, d
    return out


def puncture(coded, positions):
    skip = set(positions)
    return [b for i, b in enumerate(coded) if i not in skip]


def depuncture(bits, positions, total):
    """Coded stream of total bits with None where a bit was punctured."""
    skip, it, out = set(positions), iter(bits), []
    for i in range(total):
        out.append(None if i in skip else next(it))
    return out


def interleave(bits, groups, per):
    """Bit i goes to position i // per + groups * (i % per)."""
    out = [0] * len(bits)
    for i, bit in enumerate(bits):
        out[i // per + groups * (i % per)] = bit
    return out


def deinterleave(bits, groups, per):
    return [bits[i // per + groups * (i % per)] for i in range(len(bits))]


def _branches():
    """For state s (d1 = bit 0 ... d4 = bit 3) and input d: (next, g1, g2)."""
    table = {}
    for s in range(16):
        for d in (0, 1):
            g1 = d ^ ((s >> 2) & 1) ^ ((s >> 3) & 1)
            g2 = d ^ (s & 1) ^ ((s >> 1) & 1) ^ ((s >> 3) & 1)
            table[(s, d)] = (((s << 1) | d) & 15, g1, g2)
    return table


_BRANCH = _branches()


def viterbi(coded):
    """Hard-decision Viterbi of a rate 1/2 stream (None = erased bit) that
    ends in state 0 (the 4 tail zeros). Returns (info bits including the
    tail, number of disagreeing coded bits)."""
    steps = len(coded) // 2
    inf = 1 << 30
    cost = [0] + [inf] * 15
    back = []
    for t in range(steps):
        r1, r2 = coded[2 * t], coded[2 * t + 1]
        new = [inf] * 16
        choice = [None] * 16
        for s in range(16):
            if cost[s] >= inf:
                continue
            for d in (0, 1):
                nxt, g1, g2 = _BRANCH[(s, d)]
                metric = cost[s] + (r1 is not None and r1 != g1) + (r2 is not None and r2 != g2)
                if metric < new[nxt]:
                    new[nxt] = metric
                    choice[nxt] = (s, d)
        back.append(choice)
        cost = new
    state, bits = 0, []
    for t in range(steps - 1, -1, -1):
        state, d = back[t][state]
        bits.append(d)
    bits.reverse()
    return bits, cost[0]


# ------------------------------------------------------------------ channels

def sacch_encode(sacch4):
    """60 channel bits of a 4-byte SACCH (nxdn.sacch: 26 bits + CRC-6)."""
    coded = conv_encode(to_bits(sacch4, 32) + [0] * 4)
    return interleave(puncture(coded, SACCH_PUNCTURE), 5, 12)


def sacch_decode(bits60):
    """(4-byte SACCH, coded bit errors) from 60 channel bits."""
    coded = depuncture(deinterleave(bits60, 5, 12), SACCH_PUNCTURE, 72)
    info, errors = viterbi(coded)
    return to_bytes(info[:32]), errors


def facch1_encode(block12):
    """144 channel bits of a 12-byte FACCH1 (nxdn.facch1: 80 bits + CRC-12
    + 4 zero bits)."""
    coded = conv_encode(to_bits(block12, 96))
    return interleave(puncture(coded, FACCH1_PUNCTURE), 9, 16)


def facch1_decode(bits144):
    """(12-byte FACCH1 block, coded bit errors) from 144 channel bits."""
    coded = depuncture(deinterleave(bits144, 9, 16), FACCH1_PUNCTURE, 192)
    info, errors = viterbi(coded)
    return to_bytes(info[:96]), errors


def voice_bits(frame_a, frame_b):
    """144 channel bits of two 49-bit frames (two 72-bit AMBE+2 forms)."""
    return to_bits(nxdn.to_dmr(frame_a) + nxdn.to_dmr(frame_b))


def voice_from_bits(bits144):
    """The two 49-bit frames of 144 channel bits (no error correction)."""
    raw = to_bytes(bits144)
    return [nxdn.from_dmr(raw[:9]), nxdn.from_dmr(raw[9:])]


# ------------------------------------------------------------------ frames

def assemble_frame(lich_byte, sacch4, halves):
    """Scrambled 48-byte frame. halves: two items for block 1 and 2, each
    a 12-byte FACCH1 block or a pair of 49-bit voice frames."""
    bits = [0] * FRAME_BITS
    bits[:FSW_BITS] = to_bits(FSW_BYTES, FSW_BITS)
    for i, bit in enumerate(to_bits(bytes([lich_byte]))):
        bits[LICH_AT + 2 * i] = bit
        bits[LICH_AT + 2 * i + 1] = 1
    bits[SACCH_AT:SACCH_AT + SACCH_BITS] = sacch_encode(sacch4)
    for start, half in zip(BLOCK_AT, halves):
        if isinstance(half, (bytes, bytearray)):
            bits[start:start + FACCH1_BITS] = facch1_encode(bytes(half))
        else:
            bits[start:start + FACCH1_BITS] = voice_bits(*half)
    return scramble(to_bytes(bits))


def to_payload(frame, rssi=None):
    """Modem payload: tag byte 0x00, the frame, optional 2-byte raw RSSI."""
    out = b"\x00" + bytes(frame)
    return out + rssi.to_bytes(2, "big") if rssi is not None else out


def from_payload(payload):
    """The 48-byte frame of a modem payload (49 bytes, or 51 with RSSI)."""
    if len(payload) not in (PAYLOAD_BYTES, PAYLOAD_BYTES + 2):
        raise ValueError("NXDN modem payload is 49 or 51 bytes, got %d" % len(payload))
    return bytes(payload[1:1 + FRAME_BYTES])


def control_frame(msg, src, dst, ran=1, group=True):
    """Header (VCALL) or trailer (TX_REL) frame: SACCH idle (single), the
    same FACCH1 in both blocks, LICH RDCH / SACCH NS / steal FACCH,
    inbound (NXDNControl.cpp:197-263)."""
    call = {} if group else {"call_type": nxdn.CALL_INDIVIDUAL}
    lich = nxdn.lich(nxdn.RFCT_RDCH, nxdn.FCT_SACCH_NS, nxdn.OPTION_STEAL_FACCH)
    block = nxdn.facch1(msg, src, dst, **call)
    return assemble_frame(lich, nxdn.sacch(SR_SINGLE, nxdn.SACCH_IDLE, ran), [block, block])


def voice_frame(frames4, src, dst, index, ran=1, group=True):
    """Voice frame: four 49-bit frames, LICH RDCH / SACCH SS / no steal,
    SACCH part index % 4 of the VCALL message (structure 3, 2, 1, 0)."""
    call = {} if group else {"call_type": nxdn.CALL_INDIVIDUAL}
    part = index % 4
    lich = nxdn.lich(nxdn.RFCT_RDCH, nxdn.FCT_SACCH_SS, nxdn.OPTION_STEAL_NONE)
    sacch = nxdn.sacch(3 - part, nxdn.vcall_sacch(src, dst, part, **call), ran)
    return assemble_frame(lich, sacch, [tuple(frames4[:2]), tuple(frames4[2:])])


def lost_frame():
    """MMDVM_NXDN_LOST: the modem lost the signal; empty payload."""
    return (FRAME_LOST, b"", 0.0)


def build_keyup(src, dst, ambe_frames, ran=1, group=True, header_frames=1,
                trailer_frames=1, rssi=None):
    """A radio keying up: [(frame_type, payload, delay_ms)] in air order.
    header_frames FACCH1 VCALL frames, voice frames (ambe_frames, 7-byte
    49-bit frames, four per 80 ms frame, zero padded), trailer_frames
    FACCH1 TX_REL frames. delay_ms (80) is the air time to wait after
    sending that frame. MMDVMHost needs only the first header frame to
    open the call and the first trailer to close it; a second header
    makes it log "received RF header" again."""
    out = []

    def add(frame):
        out.append((FRAME_DATA, to_payload(frame, rssi), FRAME_MS))

    for _ in range(header_frames):
        add(control_frame(nxdn.VCALL, src, dst, ran, group))
    frames = list(ambe_frames)
    frames += [bytes(nxdn.FRAME_BYTES)] * (-len(frames) % 4)
    for index in range(0, len(frames), 4):
        add(voice_frame(frames[index:index + 4], src, dst, index // 4, ran, group))
    for _ in range(trailer_frames):
        add(control_frame(nxdn.TX_REL, src, dst, ran, group))
    return out


# ------------------------------------------------------------------ decoding

def decode_frame(frame):
    """Dict of one 48-byte frame: fsw_ok, lich (nxdn.parse_lich), sacch
    (nxdn.parse_sacch or None), facch1 [two items, nxdn.parse_facch1 dict
    or None when CRC fails], voice (list of 49-bit frames)."""
    bits = to_bits(scramble(frame))
    fsw_ok = all((frame[i] & FSW_MASK[i]) == FSW_BYTES[i] for i in range(3))
    lich_byte = to_bytes([bits[LICH_AT + 2 * i] for i in range(8)])[0]
    lich = nxdn.parse_lich(lich_byte)
    raw, _ = sacch_decode(bits[SACCH_AT:SACCH_AT + SACCH_BITS])
    sacch = nxdn.parse_sacch(raw)
    sacch = sacch if sacch["crc_ok"] else None
    facch, voice = [], []
    option = lich["option"]
    # which halves are FACCH1: option 0 both, 1 first, 2 second, 3 none
    is_facch = [option in (0, 1), option in (0, 2)]
    for start, steal in zip(BLOCK_AT, is_facch):
        block = bits[start:start + FACCH1_BITS]
        if steal:
            data, _ = facch1_decode(block)
            info = nxdn.parse_facch1(data)
            facch.append(info if info["crc_ok"] else None)
        else:
            facch.append(None)
            if lich["fct"] != nxdn.FCT_UDCH:
                voice.extend(voice_from_bits(block))
    return {"fsw_ok": fsw_ok, "lich": lich, "sacch": sacch, "facch1": facch, "voice": voice}


def decode_downlink(frames):
    """What the host wrote to the modem: frames = [(type, payload)] with
    payload as after the type byte. Returns src, dst, group, ran (None when
    not seen), voice (49-bit frames in order), frame_count, has_header (a
    VCALL FACCH1 was seen), terminated (a TX_REL FACCH1 was seen). The call
    ids come from the FACCH1 VCALL, else from four SACCH superframe parts."""
    result = {"src": None, "dst": None, "group": None, "ran": None, "voice": [],
              "frame_count": 0, "has_header": False, "terminated": False}
    parts = {}
    for ftype, payload in frames:
        if ftype != FRAME_DATA:
            continue
        info = decode_frame(from_payload(payload))
        result["frame_count"] += 1
        result["voice"].extend(info["voice"])
        sacch = info["sacch"]
        if sacch is not None:
            if result["ran"] is None:
                result["ran"] = sacch["ran"]
            if info["lich"]["fct"] == nxdn.FCT_SACCH_SS:
                part = 3 - sacch["structure"]
                if part == 0:
                    parts = {}
                parts[part] = sacch["data"]
        for block in info["facch1"]:
            if block is None:
                continue
            if block["msg"] == nxdn.VCALL and not result["has_header"]:
                result["has_header"] = True
                result["src"], result["dst"] = block["src"], block["dst"]
                result["group"] = block["group"]
            elif block["msg"] == nxdn.TX_REL:
                result["terminated"] = True
        if result["src"] is None and len(parts) == 4:
            message = 0
            for part in range(4):
                message = (message << 18) | parts[part]
            layer = nxdn.parse_layer3(message.to_bytes(9, "big") + b"\x00")
            if layer["msg"] == nxdn.VCALL:
                result["src"], result["dst"] = layer["src"], layer["dst"]
                result["group"] = layer["call_type"] < nxdn.CALL_INDIVIDUAL
    return result
