# SPDX-License-Identifier: MIT
"""D-STAR slow data, written from the specification (HWFREE1 item 4, docs/dstar-slowdata-cases.md).

Sources, none of them an implementation we copy from:
  [STD6]  JARL "Digitalization Technology Standard for Amateur Radio" Ver 6.0a (the English
          edition of the D-STAR standard): 4.1 frame layout, 6.1 to 6.8 data frame, 7 fast data,
          Ap1 scrambler, Ap2 CRC polynomial.
  [SLOW]  J. Naylor (G4KLX), "The Format of D-Star Slow Data" v0.2 (2009): block layouts,
          filler, interleave patterns.

Frame model. A voice frame carries 9 AMBE bytes and a 3 byte data field. Frames come in
superframes of 21 (420 ms): frame 0 carries the sync pattern 55 2D 16 in its data field (no
slow data); frames 1..20 carry 3 slow data bytes each, XORed with the scrambler 70 4F 93.
Two data fields make a 6 byte BLOCK: 1 mini-header byte (type in the high nibble, length or
block number in the low nibble) and 5 payload bytes. A superframe holds 10 blocks. A block
never spans a sync frame in a conforming stream; `render(..., start=even frame)` makes
the one that does, as a negative case.

The scrambler is not copied as a constant: `lfsr_bytes()` derives it from [STD6] Ap1.1,
S(x) = x^7 + x^4 + 1, initialised to all ones, bits sent LSB first; the first 9 bytes are
70 4F 93 40 64 74 6D 30 2B (cases/unit/test_dstar_sd.py).

Layers (all pure, no sockets):
  block builders   text_blocks, simple_data_blocks, dprs_line + simple_data_blocks,
                   nmea_sentence, header_blocks, squelch_block, fast_data_mini_header, filler_block
  layout engine    place (offset, spill over superframes), interleave (G4KLX patterns),
                   superframes_to_halves, render (continuous stream of data fields, any start frame)
  fast data        fast_data_superframe (frame bytes per [STD6] 7.3; order read from the figure)
  decoders         Decoder (policy "accumulate" or "reset" at a sync frame; both behaviours exist
                   in the two oracles of docs/dstar-slowdata-cases.md)
  packet loss      packet_loss_rewrite ([STD6] 6.6 table)
  DV-Router unit   split_units, unit66 (3 byte presence bitmap + 21 x 3 raw bytes), DvrUnitDecoder
                   (model of the DV-Router lead's statements: fixed frame pairs per unit)
  carriers         over_datagrams (DSVT header + voice frames + last frame, DExtra/DPlus bytes)
"""
from .proto import dextra

# ------------------------------------------------------------------ constants

NIBBLE_POLICIES = ("strict", "ignore", "dvrouter")
UNIT66_BYTES = 3 + 3 * 21               # presence bitmap + 21 x 3 raw slow data bytes
DVR_MAX_DPRS = 300                      # D-PRS: '$$CRC' to the CR, INCLUDING the CR (DV-Router lead)
DVR_MAX_NMEA = 83                       # NMEA: 82 bytes before the LF delimiter, i.e. 83 with the LF
                                        # (reading A of docs/dstar-slowdata-cases.md, an inference)

SYNC = b"\x55\x2d\x16"                  # data field of frame 0 (never scrambled)
FRAMES = 21                             # frames per superframe
SLOTS = 10                              # blocks per superframe (20 data frames / 2)
FILL = 0x66                             # "No data" mini-header and the filler byte ('f')
NULL_AMBE = bytes.fromhex("9e8d3288261a3f61e8")      # [STD6] 6.6 silence frame
END_PATTERN_SLOW = dextra.END_PATTERN[9:12]

T_DATA = 0x30                           # 3x simple data / D-PRS, x = valid characters 1..5
T_TEXT = 0x40                           # 4x message, x = block number 0..3
T_HEADER = 0x50                         # 5x radio header resend, x = valid bytes 1..5
T_FAST_A = 0x80                         # 8x fast data, length 1..15
T_FAST_B = 0x90                         # 9x fast data, length 16 + x, x = 0..12
T_SQUELCH = 0xC0                        # Cx code squelch, x = 2
TYPE_MASK = 0xF0

TEXT_LEN = 20
HEADER_LEN = 41
FAST_SYNC_BLOCK_BYTES = 28              # [STD6] 7.3: the block that holds the sync frame
FAST_BLOCK_BYTES = 20                   # blocks 2..10
FAST_MITIGATION = 0x02                  # [STD6] 7.3: always 0x02

# [STD6] 6.2: characters that simple data must not use
FORBIDDEN_SIMPLE = frozenset([0x00, 0x11, 0x13, 0x76, 0x84, 0xE7, 0xFE])


def lfsr_bytes(count):
    """First `count` bytes of the scrambler of [STD6] Ap1.1: x^7 + x^4 + 1, all ones at the start,
    each output bit is x[n-7] xor x[n-4], packed LSB first."""
    state = [1] * 7
    bits = []
    for _ in range(8 * count):
        new = state[0] ^ state[3]
        bits.append(new)
        state = state[1:] + [new]
    return bytes(sum(bits[8 * i + k] << k for k in range(8)) for i in range(count))


SCRAMBLER = lfsr_bytes(9)
SCRAMBLE3 = SCRAMBLER[:3]               # the per frame XOR of the data field: 70 4F 93


def xor3(data3):
    """Scramble or unscramble a 3 byte data field."""
    return bytes(a ^ b for a, b in zip(bytes(data3), SCRAMBLE3))


# ------------------------------------------------------------------ checksums

def crc16_x25(data):
    """CRC-16 with the reflected polynomial 0x8408 ([STD6] Ap2.1), initial value 0xFFFF, result
    complemented: the radio header FCS and the D-PRS "$$CRC" value. Catalogue check value of
    "123456789" is 0x906E."""
    crc = 0xFFFF
    for byte in bytes(data):
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def nmea_checksum(body):
    """NMEA 0183 checksum: XOR of the characters between '$' and '*'."""
    value = 0
    for ch in body.encode("ascii"):
        value ^= ch
    return value


def nmea_sentence(body, crlf=True):
    """'$' + body + '*' + two upper-case hex digits, plus CR LF ([SLOW]: the examples end 0D 0A)."""
    text = "$%s*%02X" % (body, nmea_checksum(body))
    return text + ("\r\n" if crlf else "")


def dprs_line(payload, cr=True, crc_covers_cr=True):
    """D-PRS ([STD6] 6.8.1): '$$CRC' + 4 hex digits + ',' + the APRS string + CR. `payload` is
    "CALL-SSID>UNPROTO,PATH:body". Whether the CRC covers the CR is decided by the oracle run
    (docs/dstar-slowdata-cases.md): it does."""
    data = payload.encode("latin-1") + (b"\r" if cr else b"")
    covered = data if crc_covers_cr else data[:-1] if cr else data
    return b"$$CRC%04X," % crc16_x25(covered) + data


# ------------------------------------------------------------------ block builders

def block(mini, payload=b""):
    """6 byte block: mini-header, up to 5 payload bytes, the rest 'f' (0x66)."""
    payload = bytes(payload)
    if len(payload) > 5:
        raise ValueError("a block holds 5 payload bytes")
    return bytes([mini]) + payload + bytes([FILL]) * (5 - len(payload))


def filler_block():
    """Six 'f': the 'F' of the interleave tables of [SLOW], mini-header 0x66 = No Data."""
    return bytes([FILL]) * 6


def text_bytes(text):
    """The 20 byte display form: str (ASCII) or bytes, cut or padded with spaces."""
    raw = text.encode("ascii") if isinstance(text, str) else bytes(text)
    return (raw + b" " * TEXT_LEN)[:TEXT_LEN]


def text_blocks(text):
    """The message function ([STD6] 6.3): four blocks 0x40..0x43 of 5 characters."""
    raw = text_bytes(text)
    return [block(T_TEXT | i, raw[5 * i:5 * i + 5]) for i in range(4)]


def simple_data_blocks(data, check=True):
    """Simple data / D-PRS / NMEA text ([STD6] 6.1.3, 6.2): blocks 0x3x with 1..5 valid
    characters, the last block padded with 'f' (not counted in x). check=True refuses the
    characters 6.2 forbids."""
    data = bytes(data)
    if check:
        bad = sorted(set(data) & FORBIDDEN_SIMPLE)
        if bad:
            raise ValueError("characters forbidden in simple data: %s" % ", ".join("0x%02X" % b for b in bad))
    return [block(T_DATA | len(data[i:i + 5]), data[i:i + 5]) for i in range(0, len(data), 5)]


def header_blocks(radio41):
    """The radio header resend ([STD6] 6.4, figure p.70): 41 bytes as 8 blocks 0x55 and one block
    0x51 (the last byte), 9 blocks. The 10th block of the superframe is the filler."""
    radio = bytes(radio41)
    if len(radio) != HEADER_LEN:
        raise ValueError("radio header is 41 bytes")
    return [block(T_HEADER | len(radio[i:i + 5]), radio[i:i + 5]) for i in range(0, HEADER_LEN, 5)]


def squelch_block(code):
    """Code squelch ([STD6] 6.5, [SLOW]): 0xC2, the two-digit code as BCD twice, then 'f'.
    code is an int 0..99 or a two digit string; "19" becomes 0x19."""
    text = "%02d" % code if isinstance(code, int) else str(code)
    if len(text) != 2 or not text.isdigit():
        raise ValueError("code squelch is two decimal digits")
    bcd = int(text, 16)
    return block(T_SQUELCH | 2, bytes([bcd, bcd]))


def fast_data_mini_header(length):
    """Mini-header of a fast data block ([STD6] 6.1.3): 0x80 | length for 1..15, 0x90 | (length - 16)
    for 16..28."""
    if 1 <= length <= 15:
        return T_FAST_A | length
    if 16 <= length <= 28:
        return T_FAST_B | (length - 16)
    raise ValueError("fast data length is 1..28")


# ------------------------------------------------------------------ layout engine

def halves(blocks):
    """Blocks to data fields (3 bytes, not scrambled): two per block."""
    out = []
    for b in blocks:
        b = bytes(b)
        if len(b) != 6:
            raise ValueError("a block is 6 bytes")
        out += [b[:3], b[3:]]
    return out


def superframes_to_halves(superframes):
    """superframes: a list of lists of exactly SLOTS blocks (None = filler block) to the data
    fields of those superframes, in order."""
    flat = []
    for sf in superframes:
        if len(sf) != SLOTS:
            raise ValueError("a superframe holds %d blocks, got %d" % (SLOTS, len(sf)))
        flat += [filler_block() if b is None else b for b in sf]
    return halves(flat)


def place(blocks, first_slot=0, superframes=None):
    """Blocks laid one after the other from slot `first_slot` of superframe 0, spilling over into
    the next superframes (the message continues after the sync frame); the free slots hold the
    filler (None). `superframes` (default: as many as needed) pads with filler superframes."""
    if not 0 <= first_slot < SLOTS:
        raise ValueError("first_slot is 0..9")
    slots = [None] * first_slot + list(blocks)
    count = max(-(-len(slots) // SLOTS), superframes or 0)
    slots += [None] * (count * SLOTS - len(slots))
    return [slots[i * SLOTS:(i + 1) * SLOTS] for i in range(count)]


# Interleave patterns, one letter per block slot of a superframe, from the tables of [SLOW]
# ("Data Interleaving"; D = the sync frame, not a block) and the figures of [STD6] 6.3 to 6.5.
#   H header resend   T text   G / A GPS or D-PRS simple data   C code squelch   F filler
LAYOUTS = {
    "header": "HHHHHHHHHF",             # None row of the table: 9 header blocks and a filler
    "text": "TTTTFFFFFF",               # Text: T T T T F F F F F F D
    "text-gps": "TGTGTGTGGG",           # Text + GPS: T G T G T G T G G G D
    "text-aprs": "TATATATAAA",          # Text + APRS: T A T A T A T A A A D
    "squelch-text-gps": "CGTGTGTGTG",   # Squelch + Text + GPS: C G T G T G T G T G D
    "gps": "GGGGGGGGGG",                # G G G G G G G G G G D
    "gps-end": "GGGGGGGFFF",            # G G G G G G G F F F D
}


def interleave(pattern, sources, superframes=None):
    """Superframes (lists of 10 blocks or None) built by walking `pattern` (10 letters) once per
    superframe. sources maps a letter to ("cycle", blocks) (repeats forever, the position
    carries on across superframes: text, header, squelch) or ("once", blocks) (used up, then
    the slot is the filler: GPS, D-PRS). The result stops when every "once" source is used up
    and at least `superframes` superframes exist; with no "once" source `superframes` is needed."""
    if len(pattern) != SLOTS:
        raise ValueError("a pattern is %d letters" % SLOTS)
    pos = {letter: 0 for letter in sources}
    out = []
    while True:
        sf = []
        for letter in pattern:
            if letter == "F" or letter not in sources:
                sf.append(None)
                continue
            kind, blocks = sources[letter]
            if kind == "cycle":
                sf.append(blocks[pos[letter] % len(blocks)])
                pos[letter] += 1
            elif kind == "once":
                sf.append(blocks[pos[letter]] if pos[letter] < len(blocks) else None)
                pos[letter] += 1
            else:
                raise ValueError("source kind is cycle or once")
        out.append(sf)
        done = all(pos[l] >= len(sources[l][1]) for l in sources if sources[l][0] == "once" and l in pattern)
        if done and len(out) >= (superframes or 1):
            return out
        if len(out) > 1000:
            raise ValueError("interleave does not finish: no 'once' source in the pattern")


def render(data_fields, start=1, frames=None):
    """The frames of an over as a list of (seq, wire data field). Frame f has seq f % 21; seq 0 is
    the sync pattern (no slow data). The data fields of `data_fields` (3 bytes each, plain) fill
    frames from frame `start` on, skipping every sync frame; before the start and after the last
    data field the frame carries the filler 66 66 66. Everything but the sync is scrambled.

    start=1 is the conforming layout (first block in the first data frame after the sync). An
    even start puts the pair boundary between the data frames (1,2), (3,4), ... of the receiver
    in the middle of the sender's blocks: a block that straddles a sync."""
    fields = [bytes(h) for h in data_fields]
    out = []
    i = 0
    f = 0
    while True:
        if frames is None:
            if f >= FRAMES and i >= len(fields) and f % FRAMES == 0:
                break
        elif f >= frames:
            break
        seq = f % FRAMES
        if seq == 0:
            out.append((0, SYNC))
        else:
            if f >= start and i < len(fields):
                plain = fields[i]
                i += 1
            else:
                plain = bytes([FILL]) * 3
            out.append((seq, xor3(plain)))
        f += 1
    if i < len(fields):
        raise ValueError("frames=%d is too few for %d data fields" % (frames, len(fields)))
    return out


# ------------------------------------------------------------------ fast data

def fast_data_superframe(payload, guard=0x20):
    """One superframe of fast data ([STD6] 7.1 to 7.4): 28 + 9 x 20 = 208 payload bytes in 21 frames
    as a list of (seq, voice field 9 bytes, data field 3 bytes), wire form (scrambled).

    Layout read from the figures of 7.3: block 1 is frames 0 (sync), 1, 2: data field of frame 1
    = mini-header 0x9C (28 bytes) + 2 data, data field of frame 2 = guard + 2 data, then the voice
    fields of frames 1, 2 and 0 in that order (see the figure: "2nd Voice, 3rd Voice, 1st Voice"),
    each as 4 data, 0x02 mitigation, 4 data. Blocks 2..10 are frame pairs (3,4) ... (19,20) with
    mini-header 0x94 (20 bytes) and the voice fields of those two frames. The data field of the
    sync frame stays the sync pattern. Scrambling: the voice field with SCRAMBLER[0:9], the data
    field with SCRAMBLER[0:3], the sync field not. NOTHING decodes this except our own parser
    and the block types MMDVMHost reports (UNVERIFIED in the sense of docs/dstar-slowdata-cases.md)."""
    payload = bytes(payload)
    total = FAST_SYNC_BLOCK_BYTES + 9 * FAST_BLOCK_BYTES
    if len(payload) != total:
        raise ValueError("a fast data superframe holds %d bytes" % total)
    pos = [0]

    def take(n):
        chunk = payload[pos[0]:pos[0] + n]
        pos[0] += n
        return chunk

    def voice_field():
        a = take(4)
        b = take(4)
        return a + bytes([FAST_MITIGATION]) + b

    plain = {}                          # seq -> (voice9, data3)
    mini1 = bytes([fast_data_mini_header(FAST_SYNC_BLOCK_BYTES)])
    plain[1] = [None, mini1 + take(2)]
    plain[2] = [None, bytes([guard]) + take(2)]
    plain[1][0] = voice_field()
    plain[2][0] = voice_field()
    plain[0] = [voice_field(), SYNC]
    mini = bytes([fast_data_mini_header(FAST_BLOCK_BYTES)])
    for first in range(3, 21, 2):
        plain[first] = [None, mini + take(2)]
        plain[first + 1] = [None, bytes([guard]) + take(2)]
        plain[first][0] = voice_field()
        plain[first + 1][0] = voice_field()
    out = []
    for seq in range(FRAMES):
        voice, data = plain[seq]
        wire_voice = bytes(a ^ b for a, b in zip(voice, SCRAMBLER))
        out.append((seq, wire_voice, data if seq == 0 else xor3(data)))
    return out


def fast_data_payload(frames):
    """Inverse of fast_data_superframe: the 208 payload bytes, or None when a mini-header,
    a mitigation byte or the sync field is wrong."""
    by_seq = {seq: (bytes(a ^ b for a, b in zip(v, SCRAMBLER)), d if seq == 0 else xor3(d))
              for seq, v, d in frames}
    if sorted(by_seq) != list(range(FRAMES)) or by_seq[0][1] != SYNC:
        return None

    def voice_bytes(v):
        return None if v[4] != FAST_MITIGATION else v[:4] + v[5:]

    out = bytearray()
    for seq in range(1, 21, 2):
        mini, d1, d2 = by_seq[seq][1][0], by_seq[seq][1][1:3], by_seq[seq + 1][1][1:3]
        want = fast_data_mini_header(FAST_SYNC_BLOCK_BYTES if seq == 1 else FAST_BLOCK_BYTES)
        if mini != want:
            return None
        out += d1 + d2
        order = [1, 2, 0] if seq == 1 else [seq, seq + 1]
        for s in order:
            vb = voice_bytes(by_seq[s][0])
            if vb is None:
                return None
            out += vb
    return bytes(out)


# ------------------------------------------------------------------ packet loss

def packet_loss_rewrite(first3, first_lost=False, last_lost=True):
    """The data fields of a frame pair after a lost Ethernet packet, rows of the table in
    [STD6] 6.6 (the voice frames are replaced by NULL_AMBE). first3 is the plain first data field;
    returns (first, last) plain data fields or None when the table has no row for the case:
      first frame normal, no data, last lost            66 66 66 | 66 66 66
      first frame lost (mini-header gone)               31 80 66 | 66 66 66
      0x31 xx 66, last lost                             31 xx 66 | 66 66 66
      0x32 xx xx, last lost                             32 xx xx | 66 66 66
      0x33 / 0x34 / 0x35 xx xx, last lost               3n xx xx | 80 66 66 / 80 00 66 / 80 00 00
    """
    first3 = bytes(first3)
    if first_lost:
        return b"\x31\x80\x66", b"\x66\x66\x66"
    if not last_lost:
        return None
    if first3 == b"\x66\x66\x66":
        return first3, b"\x66\x66\x66"
    if first3[0] == 0x31:
        return first3, b"\x66\x66\x66"
    if first3[0] == 0x32:
        return first3, b"\x66\x66\x66"
    tails = {0x33: b"\x80\x66\x66", 0x34: b"\x80\x00\x66", 0x35: b"\x80\x00\x00"}
    if first3[0] in tails:
        return first3, tails[first3[0]]
    return None


# ------------------------------------------------------------------ decoders

class Decoder:
    """Slow data decoder over wire data fields.

    sync="accumulate" keeps the text segments and the header bytes over a sync frame
    (DStarGateway's CTextCollector, DSDcc); sync="reset" clears them (MMDVMHost's
    CDStarSlowData::start()). Both restart the pair phase at a sync frame; GPS / NMEA text is
    never cleared by a sync (a D-PRS line is longer than one superframe).

    feed(seq, wire3) returns the events produced by that frame, each a dict with "kind":
      text      text (20 chars, bit 7 stripped), seq
      header    bytes (41), crc_ok
      dprs      line (bytes after "$$CRCxxxx,", with the CR), crc_ok, crc (as sent)
      nmea      sentence (str), checksum_ok
      squelch   code (hex digits as str) or None when the two bytes differ
      fast      length (1..28) or None when the type is above 0x9C
    `blocks` lists (seq, type byte) of every completed block, in order.

    nibble decides how the low nibble of a 0x3x block (simple data, D-PRS, NMEA) is used:
      "strict"    1..5 valid bytes, 0 and 6..15 drop the block (the default, the standard)
      "ignore"    every 0x3x block adds 5 bytes, the nibble is not read (DStarGateway's collectors)
      "dvrouter"  0x30..0x35 valid, 0 means 5, 0x36..0x3F drop the block (statement of the
                  DV-Router lead, UNVERIFIED by any oracle)"""

    def __init__(self, sync="accumulate", max_dprs=300, max_nmea=82, nibble="strict"):
        if sync not in ("accumulate", "reset"):
            raise ValueError("sync is accumulate or reset")
        if nibble not in NIBBLE_POLICIES:
            raise ValueError("nibble is one of %s" % ", ".join(NIBBLE_POLICIES))
        self.sync = sync
        self.nibble = nibble
        self.max_dprs = max_dprs
        self.max_nmea = max_nmea
        self.blocks = []
        self._text = {}
        self._header = bytearray()
        self._buf = bytearray()
        self._half = None

    def _start(self):
        self._half = None
        if self.sync == "reset":
            self._text = {}
            self._header = bytearray()

    def feed(self, seq, wire3):
        if seq == 0:
            self._start()
            return []
        data = xor3(wire3)
        if self._half is None:
            self._half = data
            return []
        blk = self._half + data
        self._half = None
        return self._block(seq, blk)

    def feed_all(self, frames):
        events = []
        for seq, wire in frames:
            events += self.feed(seq, wire)
        return events

    def _block(self, seq, blk):
        mini = blk[0]
        self.blocks.append((seq, mini))
        kind = mini & TYPE_MASK
        events = []
        if kind == T_TEXT and (mini & 0x0F) < 4:
            self._text[mini & 0x0F] = bytes(c & 0x7F for c in blk[1:])
            if len(self._text) == 4:
                text = b"".join(self._text[i] for i in range(4)).decode("ascii")
                self._text = {}
                events.append({"kind": "text", "text": text, "seq": seq})
        elif kind == T_HEADER and 1 <= (mini & 0x0F) <= 5:
            if len(self._header) >= HEADER_LEN:
                self._header = bytearray()
            self._header += blk[1:1 + (mini & 0x0F)]
            if len(self._header) >= HEADER_LEN:
                raw = bytes(self._header[:HEADER_LEN])
                events.append({"kind": "header", "bytes": raw, "seq": seq,
                               "crc_ok": crc16_x25(raw[:39]).to_bytes(2, "little") == raw[39:41]})
        elif kind == T_DATA and self._data_len(mini & 0x0F):
            self._buf += blk[1:1 + self._data_len(mini & 0x0F)]
            events += self._sentences(seq)
        elif kind == T_SQUELCH and (mini & 0x0F) == 2:
            events.append({"kind": "squelch", "seq": seq,
                           "code": "%02x" % blk[1] if blk[1] == blk[2] else None})
        elif mini & 0xE0 == T_FAST_A:
            if mini > 0x9C or mini == 0x80:
                length = None
            else:
                length = mini & 0x0F if mini < 0x90 else 16 + (mini & 0x0F)
            events.append({"kind": "fast", "seq": seq, "length": length})
        return events

    def _data_len(self, nib):
        """Valid payload bytes of a 0x3x block under the nibble policy; 0 = block dropped."""
        if self.nibble == "ignore":
            return 5
        if self.nibble == "dvrouter":
            return 5 if nib == 0 else nib if nib <= 5 else 0
        return nib if 1 <= nib <= 5 else 0

    def _sentences(self, seq):
        events = []
        while True:
            buf = bytes(self._buf)
            dprs = buf.find(b"$$CRC")
            nmea = buf.find(b"$GP")
            starts = [(p, k) for p, k in ((dprs, "dprs"), (nmea, "nmea")) if p >= 0]
            if not starts:
                # keep a possible start ('$' or '$$') split over two blocks
                self._buf = bytearray(buf[-5:]) if b"$" in buf[-5:] else bytearray()
                return events
            pos, kind = min(starts)
            end_char = b"\r" if kind == "dprs" else b"\n"
            end = buf.find(end_char, pos)
            limit = self.max_dprs if kind == "dprs" else self.max_nmea
            if end < 0:
                if len(buf) - pos > limit:
                    self._buf = bytearray()
                else:
                    self._buf = bytearray(buf[pos:])
                return events
            sentence = buf[pos:end + 1]
            self._buf = bytearray(buf[end + 1:])
            if len(sentence) > limit:
                continue
            if kind == "dprs":
                events.append(self._dprs(seq, sentence))
            else:
                events.append(self._nmea(seq, sentence))

    @staticmethod
    def _dprs(seq, sentence):
        line = sentence[10:] if len(sentence) > 10 and sentence[9:10] == b"," else b""
        sent = sentence[5:9].decode("latin-1")
        try:
            ok = int(sent, 16) == crc16_x25(line)
        except ValueError:
            ok = False
        return {"kind": "dprs", "seq": seq, "line": line, "crc": sent, "crc_ok": ok}

    @staticmethod
    def _nmea(seq, sentence):
        text = sentence.decode("latin-1").rstrip("\r\n")
        ok = False
        if "*" in text and text.startswith("$"):
            body, _, tail = text[1:].rpartition("*")
            try:
                ok = int(tail[:2], 16) == nmea_checksum(body)
            except ValueError:
                ok = False
        return {"kind": "nmea", "seq": seq, "sentence": text, "checksum_ok": ok}


# ------------------------------------------------------------------ DV-Router unit model

def split_units(frames):
    """Frames of an over ([(seq, wire3)]) to one dict {seq: wire3} per superframe unit. A new unit
    starts whenever the sequence number does not increase (the sync frame, or a lost sync frame:
    the counter still wraps)."""
    units = []
    prev = None
    for seq, wire in frames:
        if prev is None or seq <= prev:
            units.append({})
        units[-1][seq] = bytes(wire)
        prev = seq
    return units


def unit66(unit):
    """The 66 byte unit DV-Router's decoder is fed once per superframe (statement of the DV-Router
    lead): a 3 byte presence bitmap, then 21 x 3 raw slow data bytes as received (frame 0 included,
    still the sync pattern, others still scrambled); an absent frame is 3 zero bytes.
    ASSUMPTION of this generator (the lead builds the unit from the 'seq hex6' lines himself): the
    bitmap is a 24 bit big-endian number, MSB first over frames 0..20: frame f is bit (23 - f), so
    frame 0 is bit 7 of byte 0, frame 7 bit 0 of byte 0, frame 8 bit 7 of byte 1, frame 16 bit 7 of
    byte 2, frame 20 bit 3 of byte 2; the 3 spare bits (2..0 of byte 2) are zero."""
    mask = 0
    body = b""
    for f in range(FRAMES):
        if f in unit:
            if len(unit[f]) != 3:
                raise ValueError("a slow data field is 3 bytes")
            mask |= 1 << (23 - f)
            body += bytes(unit[f])
        else:
            body += b"\x00\x00\x00"
    return mask.to_bytes(3, "big") + body


def parse_unit66(raw):
    """Inverse of unit66: {seq: wire3} of the present frames. Raises on a wrong length or on spare
    bits that are set."""
    raw = bytes(raw)
    if len(raw) != UNIT66_BYTES:
        raise ValueError("a unit is %d bytes" % UNIT66_BYTES)
    mask = int.from_bytes(raw[:3], "big")
    if mask & 0x7:
        raise ValueError("spare bits of the bitmap are set")
    return {f: raw[3 + 3 * f:6 + 3 * f] for f in range(FRAMES) if mask & (1 << (23 - f))}


class DvrUnitDecoder:
    """Model of the DV-Router lead's statements about its decoder (UNVERIFIED by any oracle except
    where the corpus says the MMDVMHost / DStarGateway result agrees):
      * one 66 byte unit (a superframe) per feed_unit() call;
      * the text segments are cleared at the start of every unit (the DP5f decision: reset at every
        sync like MMDVMHost, DStarSlowData.cpp:173-181);
      * blocks are the fixed frame pairs (1,2) (3,4) ... (19,20) of the unit: a block that
        straddles a sync cannot form, an offset pairing is not resynced;
      * a pair with a missing frame skips that block and resets the GPS line buffer only (text
        segments and the other state are kept);
      * length nibble of 0x3x blocks: 0x30..0x35 (0 means 5), 0x36..0x3F dropped;
      * D-PRS limit 300 bytes including the CR, NMEA limit 82 bytes before the delimiter;
      * header resend, fast data, squelch and filler are passed through opaque: no events.
    Events are those of Decoder; header, squelch and fast events are suppressed."""

    OPAQUE = ("header", "squelch", "fast")

    def __init__(self, max_dprs=DVR_MAX_DPRS, max_nmea=DVR_MAX_NMEA):
        self._dec = Decoder("reset", max_dprs=max_dprs, max_nmea=max_nmea, nibble="dvrouter")

    def feed_unit(self, unit):
        """unit: {seq: wire3} (split_units) or the 66 raw bytes."""
        if isinstance(unit, (bytes, bytearray)):
            unit = parse_unit66(unit)
        dec = self._dec
        dec._start()
        events = []
        for first in range(1, FRAMES, 2):
            a, b = unit.get(first), unit.get(first + 1)
            if a is None or b is None:
                dec._buf = bytearray()
                continue
            events += [e for e in dec._block(first + 1, xor3(a) + xor3(b)) if e["kind"] not in self.OPAQUE]
        return events

    def feed_frames(self, frames):
        events = []
        for unit in split_units(frames):
            events += self.feed_unit(unit)
        return events


# ------------------------------------------------------------------ carriers

def over_datagrams(stream_id, frames, radio=None, ambe=NULL_AMBE, last=True):
    """DSVT datagrams of an over (DExtra and the DPlus body): the 56 B header, one 27 B voice packet
    per (seq, wire data field) of `frames` with a null AMBE field, and the 27 B last frame
    (end pattern; counter = next seq + 0x40)."""
    out = [dextra.header_packet(stream_id, radio=radio)]
    for seq, wire in frames:
        out.append(dextra.voice_packet(stream_id, seq, ambe, slow=wire))
    if last:
        out.append(dextra.last_frame(stream_id, len(frames)))
    return out
