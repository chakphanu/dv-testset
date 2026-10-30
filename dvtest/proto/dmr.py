# SPDX-License-Identifier: MIT
"""DMR over the MMDVM/Homebrew repeater protocol (UDP 62030) to xlxd.
The DMR id must exist in the reflector's dmrid.dat; TG 4001..4026 map to
modules A..Z, TG 4000 unlinks."""
import hashlib
import random

from ..net import UdpClient

PORT = 62030
DMR_ID = 1023007
PACKET_SIZE = 55
DATA_SYNC = (0xFF, 0x57, 0xD7, 0x5D, 0xF5)
CONFIG_SIZE = 302
# 48-bit sync patterns (BS = base station sourced, MS = mobile sourced)
BS_DATA = bytes.fromhex("DFF57D75DF5D")
MS_DATA = bytes.fromhex("D5D7F77FD757")
BS_VOICE = bytes.fromhex("755FD7DF75F7")
# flags byte 15: frame types (bits 5-4) and data types (bits 3-0)
VOICE, VOICE_SYNC, DATA = 0, 1, 2
HEADER, TERMINATOR, CSBK, DATA_HEADER, RATE12, RATE34 = 1, 2, 3, 6, 7, 8
# RPTC fields after "RPTC" + id, (name, width); widths sum to 294
RPTC_FIELDS = (("callsign", 8), ("rx_freq", 9), ("tx_freq", 9), ("power", 2), ("colour", 2),
               ("lat", 8), ("lon", 9), ("height", 3), ("location", 20), ("description", 19),
               ("slots", 1), ("url", 124), ("software", 40), ("package", 40))
MASTER_TAGS = (b"MSTPONG", b"MSTNAK", b"MSTCL", b"RPTACK")
_rng = random.SystemRandom()


def data_sync_payload(sync=BS_DATA):
    """33-byte burst with the 48-bit sync pattern in its centre (default
    BS data sync); the rest is zero."""
    pattern = int.from_bytes(sync, "big")
    payload = bytearray(33)
    payload[13] = (pattern >> 44) & 0x0F
    payload[14:19] = ((pattern >> 4) & ((1 << 40) - 1)).to_bytes(5, "big")
    payload[19] = (pattern & 0x0F) << 4
    return bytes(payload)


def bitfield(slot=2, private=False, frame_type=VOICE, vseq=0):
    """Flags byte 15: vseq is the voice sequence (0..5 = A..F) or, for
    frame_type DATA, the data type."""
    return (0x80 if slot == 2 else 0) | (0x40 if private else 0) | (frame_type << 4) | (vseq & 0x0F)


def parse_bitfield(value):
    return {"slot": 2 if value & 0x80 else 1, "private": bool(value & 0x40),
            "frame_type": (value >> 4) & 0x03, "vseq": value & 0x0F}


def _id(repeater):
    return repeater.to_bytes(4, "big")


def rptl(repeater):
    """Login request (8 B)."""
    return b"RPTL" + _id(repeater)


def auth_hash(salt, password):
    """SHA-256(salt + password), the RPTK digest a strict master checks."""
    return hashlib.sha256(bytes(salt) + password.encode()).digest()


def rptk(repeater, digest=bytes(32)):
    """Login key (40 B); the tester's default is a zero digest."""
    if len(digest) != 32:
        raise ValueError("RPTK digest is 32 bytes")
    return b"RPTK" + _id(repeater) + bytes(digest)


def _fit(value, width, number=False):
    text = ("%0*d" % (width, value)) if number else str(value).ljust(width)
    return text[:width]


def rptc(repeater, callsign="N0CALL", rx_freq=438800000, tx_freq=438800000, power=1, colour=1,
         lat=0.0, lon=0.0, height=0, location="", description="dvtest", slots="2", url="",
         software="dvtest", package="dvtest"):
    """Configuration (302 B): fixed-width ASCII fields of RPTC_FIELDS."""
    values = {"callsign": _fit(callsign, 8), "rx_freq": _fit(rx_freq, 9, True),
              "tx_freq": _fit(tx_freq, 9, True), "power": _fit(power, 2, True),
              "colour": _fit(colour, 2, True), "lat": _fit("%08f" % lat, 8),
              "lon": _fit("%09f" % lon, 9), "height": _fit(height, 3, True),
              "location": _fit(location, 20), "description": _fit(description, 19),
              "slots": _fit(slots, 1), "url": _fit(url, 124), "software": _fit(software, 40),
              "package": _fit(package, 40)}
    out = b"RPTC" + _id(repeater) + "".join(values[name] for name, _ in RPTC_FIELDS).encode("ascii")
    assert len(out) == CONFIG_SIZE
    return out


def rpto(repeater, options=""):
    """Options (8 B + the option text)."""
    return b"RPTO" + _id(repeater) + options.encode("ascii")


def rptping(repeater):
    """Keepalive (11 B)."""
    return b"RPTPING" + _id(repeater)


def rptcl(repeater, size=9):
    """Repeater close: RPTCL + id (9 B, the MMDVMHost form) or RPTCL +
    id + 4 zero bytes (13 B, the size xlxd parses)."""
    if size not in (9, 13):
        raise ValueError("RPTCL is 9 or 13 bytes")
    return b"RPTCL" + _id(repeater) + bytes(size - 9)


def parse_control(data):
    """{"tag", "tail"} of a master reply (MSTPONG, MSTNAK, MSTCL, RPTACK),
    tail = the bytes after the tag (salt or id); None for anything else."""
    for tag in MASTER_TAGS:
        if data.startswith(tag):
            return {"tag": tag.decode(), "tail": bytes(data[len(tag):])}
    return None


def voice_payload(frame0, frame1, frame2):
    """Three 9-byte AMBE frames around the 48-bit sync gap."""
    ambe = frame0 + frame1 + frame2
    payload = bytearray(33)
    payload[0:13] = ambe[0:13]
    payload[13] = ambe[13] & 0xF0
    payload[19] = ambe[13] & 0x0F
    payload[20:33] = ambe[14:27]
    return bytes(payload)


def extract(payload):
    """Inverse of voice_payload: the three 9-byte frames."""
    ambe = bytearray(27)
    ambe[0:14] = payload[0:14]
    ambe[13] = (payload[13] & 0xF0) | (payload[19] & 0x0F)
    ambe[14:27] = payload[20:33]
    return [bytes(ambe[0:9]), bytes(ambe[9:18]), bytes(ambe[18:27])]


# ---------------------------------------------------------------- embedded LC
# Embedded signalling in voice bursts B..F, from ETSI TS 102 361-1 V2.7.1:
# EMB PDU 9.1.2 table 9.3 (CC 4, PI 1, LCSS 2, 9 parity bits of the
# QR (16,7,6) code of B.3.2), LCSS values 9.3.3 table 9.20, Full LC PDU
# 9.1.6, embedded LC in a superframe 7.1.3 (B..E carry one LC, F the null
# message of D.1 on the inbound channel), BPTC and interleave B.2.1
# (figure B.3), Hamming (16,11,4) B.3.4 table B.16, 5-bit checksum B.3.11,
# bit positions in the burst annex E tables E.6..E.9 and E.11 (EMB bits
# 15..8, the 32-bit fragment, EMB bits 7..0 in the 48-bit centre where
# burst A has its sync). Group / unit-to-unit LC: TS 102 361-2 7.1.1/1
# and 7.1.1/2.
FLCO_GROUP, FLCO_PRIVATE = 0, 3
LCSS_SINGLE, LCSS_FIRST, LCSS_LAST, LCSS_CONTINUE = 0, 1, 2, 3
# LCSS of bursts B, C, D, E (one LC per superframe), then F (null message)
EMBEDDED_LCSS = (LCSS_FIRST, LCSS_CONTINUE, LCSS_CONTINUE, LCSS_LAST, LCSS_SINGLE)
# Parity columns of the generator matrices, one row per information bit
# (first row = first transmitted bit), leftmost parity bit as the MSB:
# table B.12 (QR (16,7,6), 9 parity bits) and table B.16 (Hamming
# (16,11,4), 5 parity bits).
QR_16_7_PARITY = (0x04F, 0x11E, 0x1B7, 0x1E2, 0x1C9, 0x0E5, 0x073)
HAMMING_16_11_PARITY = (0x13, 0x1A, 0x1F, 0x1C, 0x0E, 0x15, 0x0B, 0x16, 0x19, 0x0D, 0x07)
_CENTRE = 108                       # bit offset (from the LSB) of the 48-bit centre


def _systematic(data, table):
    """data bits followed by their parity bits (table rows = data bits,
    MSB first)."""
    parity = 0
    for index, row in enumerate(table):
        if (data >> (len(table) - 1 - index)) & 1:
            parity ^= row
    return (data << (16 - len(table))) | parity


def qr16_7(data):
    """16-bit QR (16,7,6) codeword of 7 data bits."""
    return _systematic(data & 0x7F, QR_16_7_PARITY)


def hamming16_11(data):
    """16-bit Hamming (16,11,4) codeword of 11 data bits."""
    return _systematic(data & 0x7FF, HAMMING_16_11_PARITY)


def emb(colour_code=1, pi=0, lcss=LCSS_SINGLE):
    """16-bit EMB PDU: CC, PI, LCSS and the QR (16,7,6) parity."""
    return qr16_7(((colour_code & 0x0F) << 3) | ((pi & 1) << 2) | (lcss & 3))


def parse_emb(word):
    """{"cc", "pi", "lcss", "valid"} of a 16-bit EMB (valid = the QR
    codeword is exact)."""
    data = word >> 9
    return {"cc": data >> 3, "pi": (data >> 2) & 1, "lcss": data & 3, "valid": qr16_7(data) == word}


def full_lc(flco, src, dst, fid=0, options=0, pf=0):
    """9-byte Full LC: PF, R = 0, FLCO, FID, service options, destination
    (group or target) address, source address."""
    return bytes(((pf & 1) << 7 | (flco & 0x3F), fid & 0xFF, options & 0xFF)) + \
        dst.to_bytes(3, "big") + src.to_bytes(3, "big")


def full_lc_group(src, dst, options=0):
    """Group voice channel user LC (FLCO 0, FID 0)."""
    return full_lc(FLCO_GROUP, src, dst, 0, options)


def parse_full_lc(lc):
    return {"pf": lc[0] >> 7, "flco": lc[0] & 0x3F, "fid": lc[1], "options": lc[2],
            "dst": int.from_bytes(lc[3:6], "big"), "src": int.from_bytes(lc[6:9], "big")}


def lc_checksum(lc):
    """5-bit embedded LC checksum: the sum of the nine LC octets mod 31."""
    return sum(lc[:9]) % 31


def _lc_rows(lc, checksum):
    """The seven 11-bit data rows of the embedded BPTC: LC(71..61),
    LC(60..50), then five rows of 10 LC bits + one checksum bit (CS(4)
    first)."""
    bits = int.from_bytes(lc[:9], "big")
    rows = [(bits >> 61) & 0x7FF, (bits >> 50) & 0x7FF]
    for index in range(5):
        rows.append((((bits >> (40 - 10 * index)) & 0x3FF) << 1) | ((checksum >> (4 - index)) & 1))
    return rows


def encode_embedded_lc(lc):
    """Four 32-bit fragments (bursts B, C, D, E) of a 9-byte LC: 72 bits +
    5-bit checksum in a 7 x 11 BPTC with Hamming (16,11,4) rows and an
    even-parity bottom row, read column by column, top to bottom."""
    if len(lc) != 9:
        raise ValueError("embedded LC is 9 bytes")
    matrix = [hamming16_11(row) for row in _lc_rows(lc, lc_checksum(lc))]
    column_parity = 0
    for row in matrix:
        column_parity ^= row
    matrix.append(column_parity)
    stream = 0
    for column in range(16):
        for row in matrix:
            stream = (stream << 1) | ((row >> (15 - column)) & 1)
    return [(stream >> (96 - 32 * index)) & 0xFFFFFFFF for index in range(4)]


def decode_embedded_lc(fragments):
    """(lc bytes, fec_ok, checksum_ok) from the four 32-bit fragments of
    bursts B..E; fec_ok = every Hamming row and column parity is exact (no
    correction is attempted)."""
    stream = 0
    for fragment in fragments:
        stream = (stream << 32) | (fragment & 0xFFFFFFFF)
    matrix = [0] * 8
    for column in range(16):
        for row in range(8):
            bit = (stream >> (127 - 8 * column - row)) & 1
            matrix[row] |= bit << (15 - column)
    parity = 0
    for row in matrix:
        parity ^= row
    data = [row >> 5 for row in matrix[:7]]
    fec_ok = parity == 0 and all(hamming16_11(d) == row for d, row in zip(data, matrix[:7]))
    bits = (data[0] << 61) | (data[1] << 50)
    checksum = 0
    for index in range(5):
        bits |= (data[2 + index] >> 1) << (40 - 10 * index)
        checksum = (checksum << 1) | (data[2 + index] & 1)
    lc = bits.to_bytes(9, "big")
    return lc, fec_ok, lc_checksum(lc) == checksum


def embed(payload, emb_word, fragment):
    """The 33-byte burst with its 48-bit centre replaced by EMB bits 15..8,
    the 32-bit embedded fragment and EMB bits 7..0."""
    centre = ((emb_word >> 8) << 40) | ((fragment & 0xFFFFFFFF) << 8) | (emb_word & 0xFF)
    value = int.from_bytes(payload, "big") & ~(((1 << 48) - 1) << _CENTRE)
    return (value | (centre << _CENTRE)).to_bytes(33, "big")


def embedded(payload):
    """(16-bit EMB, 32-bit fragment) of a voice burst B..F."""
    centre = (int.from_bytes(payload, "big") >> _CENTRE) & ((1 << 48) - 1)
    return ((centre >> 40) << 8) | (centre & 0xFF), (centre >> 8) & 0xFFFFFFFF


def with_embedded(payload, vseq, fragments, colour_code=1):
    """A voice burst of a superframe with embedded signalling: B..E (vseq
    1..4) carry the LC fragments with LCSS 1, 3, 3, 2; F carries the null
    embedded message with LCSS 0; A (voice sync) is returned unchanged."""
    if vseq == 0:
        return payload
    fragment = fragments[vseq - 1] if vseq <= 4 else 0
    return embed(payload, emb(colour_code, 0, EMBEDDED_LCSS[vseq - 1]), fragment)


def superframe_lc(payloads):
    """Decode the embedded LC of the payloads of bursts B, C, D, E: {"emb":
    [parse_emb of each], "lc": parse_full_lc or None, "raw", "fec_ok",
    "checksum_ok"}."""
    parts = [embedded(p) for p in payloads]
    raw, fec_ok, checksum_ok = decode_embedded_lc([f for _, f in parts])
    return {"emb": [parse_emb(e) for e, _ in parts], "raw": raw, "fec_ok": fec_ok,
            "checksum_ok": checksum_ok, "lc": parse_full_lc(raw) if checksum_ok else None}


def module_tg(module):
    return 4001 + (ord(module) - ord("A"))


def packet(seq, src, dst, repeater, bitfield, stream, payload):
    out = bytearray(PACKET_SIZE)
    out[0:4] = b"DMRD"
    out[4] = seq & 0xFF
    out[5:8] = src.to_bytes(3, "big")
    out[8:11] = dst.to_bytes(3, "big")
    out[11:15] = repeater.to_bytes(4, "big")
    out[15] = bitfield
    out[16:20] = int(stream).to_bytes(4, "little")
    out[20:53] = payload
    return bytes(out)


def parse(data):
    """dict of DMRD fields, or None."""
    if len(data) != PACKET_SIZE or not data.startswith(b"DMRD"):
        return None
    return {"seq": data[4], "src": int.from_bytes(data[5:8], "big"),
            "dst": int.from_bytes(data[8:11], "big"),
            "repeater": int.from_bytes(data[11:15], "big"), "bitfield": data[15],
            "stream": int.from_bytes(data[16:20], "little"), "payload": data[20:53]}


def is_voice(data):
    """Flags type bits 0 and 1 are voice bursts (type 1 = voice sync);
    type 2 is the data-sync header/terminator and carries no voice."""
    return len(data) == PACKET_SIZE and data.startswith(b"DMRD") and (data[15] & 0x30) != 0x20


def voice_packets(frames, src, dst, stream_id, embedded_lc=False, colour_code=1):
    """Voice bursts for frames (padded to a multiple of 3), seq from 1.
    embedded_lc: False leaves the centre of bursts B..F zero; True embeds
    the group LC of src -> dst in every superframe (B..E, F null), a
    9-byte LC embeds that LC instead."""
    fragments = None
    if embedded_lc:
        fragments = encode_embedded_lc(full_lc_group(src, dst) if embedded_lc is True else embedded_lc)
    packets = []
    seq = 1
    index = 0
    while index < len(frames):
        trio = []
        for _ in range(3):
            trio.append(frames[index] if index < len(frames) else b"\x00" * 9)
            index += 1
        # bursts A..F (vseq 0..5) from the first packet; A carries voice sync
        vseq = (seq - 1) % 6
        kind = 1 if vseq == 0 else 0
        bitfield = 0x80 | (kind << 4) | vseq
        payload = voice_payload(*trio)
        if fragments:
            payload = with_embedded(payload, vseq, fragments, colour_code)
        packets.append(packet(seq, src, dst, src, bitfield, stream_id, payload))
        seq = (seq + 1) & 0xFF
    return packets, seq


def gateway_link(src, dst, repeater, stream_id, headers=3, terminators=2):
    """The link / unlink burst of DMRGateway's XLX mode: private-call
    voice LC headers, then terminators with LC, to dst (4001..4026 link a
    module, 4000 unlinks), slot 2, one stream id, sequence 0, 1, ...,
    BS data sync; meant to be sent back to back. The LC bits stay zero
    (the XLX masters read the ids from the DMRD fields, not the LC)."""
    packets = []
    for index in range(headers + terminators):
        dtype = HEADER if index < headers else TERMINATOR
        packets.append(packet(index, src, dst, repeater, bitfield(2, True, DATA, dtype), stream_id,
                              data_sync_payload()))
    return packets


class DmrClient:
    def __init__(self, bind_ip, dmrid=DMR_ID, module="B"):
        self.client = UdpClient(bind_ip)
        self.id = dmrid
        self.tg = module_tg(module)
        self.dest = None

    def link(self, node_ip, timeout=1.0, port=PORT, join_gap=0.3):
        self.dest = (node_ip, port)
        self.client.send(self.dest, rptl(self.id))
        self.client.pump(timeout)
        if not any(p.startswith(b"RPTACK") for p in self.client.inbox):
            return False
        self.client.send(self.dest, rptk(self.id))
        self.client.pump(timeout)
        self.client.send(self.dest, rptping(self.id))
        self.client.pump(0.2)
        self.join(None, join_gap)
        return True

    def join(self, module=None, gap=0.3):
        """Join a module (default: the current one): a header, then a
        terminator, both to the module's talkgroup. The terminator follows
        well inside xlxd's 1.6 s stream timeout, so the join is a complete
        (empty) stream."""
        if module is not None:
            self.tg = module_tg(module)
        self._empty_stream(self.tg, gap)
        self.client.pump(1.0)

    def _empty_stream(self, tg, gap):
        # xlxd acts on link/unlink only in the header of a new stream id
        sid = _rng.randrange(1, 1 << 32)
        self.client.send(self.dest, packet(0, self.id, tg, self.id, 0xA1, sid, data_sync_payload()))
        self.client.pump(gap)
        self.client.send(self.dest, packet(1, self.id, tg, self.id, 0xA2, sid, data_sync_payload()))

    def close_link(self, size=13):
        """Repeater close: RPTCL + id (9 B, the MMDVMHost form) or RPTCL +
        id + 4 zero bytes (13 B, the size xlxd accepts)."""
        self.client.send(self.dest, rptcl(self.id, size))

    def keepalive(self):
        """Repeater keepalive; xlxd drops a silent DMR client after ~100 s."""
        self.client.send(self.dest, rptping(self.id))

    def unlink(self):
        """TG 4000 unlinks: a header and a terminator on a new stream id."""
        self._empty_stream(4000, 0.3)
        self.client.pump(0.4)

    def talk(self, frames, stream_id=0x22222222, settle=1.2, tail=2.0, embedded_lc=False, colour_code=1):
        """Header, voice bursts, terminator; embedded_lc as voice_packets."""
        self.client.send(self.dest, packet(0, self.id, self.tg, self.id, 0xA1, stream_id,
                                           data_sync_payload()))
        self.client.pump(settle)
        packets, seq = voice_packets(frames, self.id, self.tg, stream_id, embedded_lc, colour_code)
        # send times of the voice bursts (header/terminator excluded)
        self.last_stamps = self.client.paced(self.dest, packets, 0.060)
        self.client.send(self.dest, packet(seq, self.id, self.tg, self.id, 0xA2, stream_id,
                                           data_sync_payload()))
        self.client.pump(tail)

    def collect(self, settle=1.5):
        """Voice frames from received DMRD voice bursts, in arrival order."""
        self.client.pump(settle)
        out = []
        for data in self.client.inbox:
            if is_voice(data):
                out.extend(extract(data[20:53]))
        return out

    def close(self):
        self.client.close()
