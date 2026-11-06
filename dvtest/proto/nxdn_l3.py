# SPDX-License-Identifier: MIT
"""NXDN layer 3 data call (DCALL) generator and parser, and the Kenwood
GPS payloads carried in it (DP14b, offline part).

Nothing here talks to a network; the 43-byte NXDND packets it returns are
sent through dvtest.proto.nxdn.NxdnClient (or any UDP socket). Every layout
below is marked with where it comes from:

  SPEC     read in the NXDN Forum TS 1-A / 1-B v1.3 (container only);
  GW       derived from the field reads of the NXDNGateway GPSHandler
           (g4klx NXDNClients b4b041d, NXDNGateway/GPSHandler.cpp, the
           Kenwood branch at about lines 170-252);
  DSD      derived from the field reads of dsd-fme (nxdn_gps_report in
           src/dsd_gps.c about lines 724-795, caller in src/nxdn_element.c
           about lines 2937-2965);
  UNKNOWN  nothing published; zero here.

The GPL projects above are test references only: no code or text of them
is copied; only byte and bit positions (facts) are used.

The vendor GPS payload is NOT in any public spec. GW and DSD agree on the
latitude, the longitude degrees/minutes, the hemispheres and the 38-byte
size, and DISAGREE on the longitude fraction, the course and the speed. Both
readings are implemented as a Layout so a rig run can show which decoder
sees what; neither is "the truth" until a real Kenwood capture exists.

Container (SPEC, TS 1-A 6.4.1/3 / 6.4.1/4 / 6.5.19-21 / 9.3):

  UDCH layer 3 message = 22 octets (0..21).
  DCALL header (type 0x09): octet 1 CC option, 2 call type (bits 7-5) and
    data call option (bits 4-0), 3-4 source id, 5-6 destination id,
    7 cipher type (bits 7-6) and key id, 8-10 packet information, 11-18
    optional IV (cipher dependent; not generated).
  DCALL user data (type 0x0B): octet 1 packet frame number (high nibble)
    and block number (low nibble), octets 2-21 user data (20 octets).
  Packet information: octet 0 bit 7 delivery, bit 5 selective retry, bits
    3-0 block count - 1; octet 1 bits 7-3 pad octet count, bit 2 start
    fragment, bit 1 circular fragment, bit 0 and octet 2 the 9-bit TX
    fragment count.
  The message CRC-32 covers the whole user data of the packet and sits in
  the last four octets after it, followed by the pad octets (pad after CRC
  is DERIVED from dsd-fme and TS 1-B 5.2, see docs/nxdn-gps-cases.md).

On the reflector wire (dvtest.proto.nxdn): 'NXDND' + src + dst + flags +
33-byte frame = LICH (0x90 family), byte with the RAN, then the 22 octets,
then 9 octets that are not used (zero).
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from . import nxdn

# layer 3 message types (SPEC 6.4.1)
MSG_TX_REL, MSG_DCALL_HEADER, MSG_DCALL_DATA = 0x08, 0x09, 0x0B

UDCH_OCTETS = 22
BLOCK_BYTES = 20                    # user data octets per DCALL data block
MAX_BLOCKS = 16
CRC_BYTES = 4
MAX_USER_BYTES = MAX_BLOCKS * BLOCK_BYTES - CRC_BYTES   # 316 (TS 1-B 5.2)
FRAME_OFFSET = 2                    # layer 3 octet 0 in the 33-byte frame

# LICH of a data frame: RDCH, UDCH, steal option 0 -> 0x90 (UNVERIFIED which
# of 0x90/0x92/0x9C/0x9E a given radio or repeater sends; all pass GW's test)
LICH_DATA = nxdn.lich(nxdn.RFCT_RDCH, nxdn.FCT_UDCH, nxdn.OPTION_STEAL_FACCH)
LICH_DATA_FAMILY = (0x90, 0x92, 0x9C, 0x9E)

# ------------------------------------------------------------------ CRC-32

CRC32_POLY = 0x04C11DB7
CRC32_INIT = 0xFFFFFFFF


def message_crc32(data):
    """Message CRC-32 (SPEC 9.3): MSB first, register preset to all ones,
    polynomial 0x04C11DB7, no reflection, no final xor. The result is the
    register after the last bit (this is the CRC-32/MPEG-2 variant)."""
    crc = CRC32_INIT
    for byte in data:
        for shift in range(7, -1, -1):
            top = (crc >> 31) & 1
            crc = (crc << 1) & 0xFFFFFFFF
            if top ^ ((byte >> shift) & 1):
                crc ^= CRC32_POLY
    return crc


# ------------------------------------------------------------------ GPS fix

@dataclass(frozen=True)
class GpsFix:
    """One position report. latitude/longitude in decimal degrees (south and
    west negative); course in degrees; speed in the unit of the wire field
    (knots or km/h: UNVERIFIED, the decoders do not agree on the label);
    elevation in whole units (DSD only); the time has no seconds."""
    latitude: float
    longitude: float
    course: float = 0.0
    speed: float = 0.0
    elevation: int = 0
    year: int = 2026
    month: int = 11
    day: int = 6
    hour: int = 12
    minute: int = 34


def to_degmin(value):
    """(negative, ddmm, fraction): degrees*100 + whole minutes, and the
    fraction of a minute in 1e-4 minute (0..9999), rounded half up on that
    resolution; a rounding carry runs into the minute and the degree."""
    if value != value or abs(value) > 180:
        raise ValueError("coordinate out of range: %r" % (value,))
    units = int((Decimal(repr(abs(float(value)))) * 600000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    degrees, rest = divmod(units, 600000)
    minute, fraction = divmod(rest, 10000)
    return value < 0, degrees * 100 + minute, fraction


def from_degmin(negative, ddmm, fraction):
    """Decimal degrees of a (negative, ddmm, fraction) triple."""
    degrees = ddmm // 100 + ((ddmm % 100) + fraction / 10000.0) / 60.0
    return -degrees if negative else degrees


def _tenths(value, maximum, what):
    """Round half up to 1/10; ValueError outside 0..maximum (in tenths)."""
    tenths = int((Decimal(repr(float(value))) * 10).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if tenths < 0 or tenths > maximum:
        raise ValueError("%s out of range: %r" % (what, value))
    return tenths


def course_tenths(course):
    """Course in 1/10 degree, 0..3599; 359.95 up to 360.0 round to 0 (wrap)."""
    return _tenths(course, 3600, "course") % 3600


# ------------------------------------------------------------------ Kenwood layouts

@dataclass(frozen=True)
class Layout:
    """Which decoder's reading places the three disputed fields.
    lon_fraction: 'gw' (bytes 13/14 next to the hemisphere bit), 'dsd'
    (bytes 32/33) or 'both' (written at both places, read as gw).
    course, speed: 'gw' or 'dsd'. Everything else is the same in both."""
    lon_fraction: str = "gw"
    course: str = "gw"
    speed: str = "gw"

    def __post_init__(self):
        if self.lon_fraction not in ("gw", "dsd", "both") or self.course not in ("gw", "dsd") \
                or self.speed not in ("gw", "dsd"):
            raise ValueError("bad layout %r" % (self,))


GATEWAY = Layout("gw", "gw", "gw")
DSDFME = Layout("dsd", "dsd", "dsd")
LON_BOTH = Layout("both", "gw", "gw")           # same longitude for both decoders
LAYOUTS = {"gateway": GATEWAY, "dsdfme": DSDFME, "lon-both": LON_BOTH}

FULL_BYTES, SHORT_BYTES = 38, 17
TYPE_FULL, TYPE_SHORT = 0x00, 0x01               # GW: first user data byte
FULL_MARKER = (0xFC, 0xFF)                       # DSD: bytes 36, 37 (read as 0xFFFC after reversal)

# byte positions (index into the user data, without CRC and pad)
POS_TIME = (3, 4, 5)                # DSD: hour/minute bit fields, see _put_time
POS_LAT = (9, 10, 11, 12)           # GW and DSD
POS_LON = (13, 14, 15, 16)          # GW and DSD (fraction: GW only)
POS_DATE = (17, 18)                 # DSD
POS_COURSE = (23, 24)
POS_SPEED = (25, 26)
POS_ELEV = (27, 28)                 # DSD only; GW ignores it
POS_LON_FRACTION_DSD = (32, 33)


def _position(buf, fix, with_lon_fraction):
    lat_neg, lat_dm, lat_frac = to_degmin(fix.latitude)
    lon_neg, lon_dm, lon_frac = to_degmin(fix.longitude)
    if lat_dm > 9000 or lon_dm > 18000:
        raise ValueError("coordinate out of range")
    # fraction: 15 bits, hemisphere bit 0 of the lower byte (0 = N / E)
    buf[9] = ((lat_frac & 0x7F) << 1) | int(lat_neg)
    buf[10] = lat_frac >> 7
    buf[11] = lat_dm & 0xFF
    buf[12] = lat_dm >> 8
    buf[13] = int(lon_neg)
    buf[15] = lon_dm & 0xFF
    buf[16] = lon_dm >> 8
    if with_lon_fraction:
        buf[13] |= (lon_frac & 0x7F) << 1
        buf[14] = lon_frac >> 7
    return lon_frac


def _put_time(buf, fix):
    if not (0 <= fix.hour <= 23 and 0 <= fix.minute <= 59):
        raise ValueError("time out of range")
    if not (0 <= fix.year - 2000 <= 127 and 1 <= fix.month <= 12 and 1 <= fix.day <= 31):
        raise ValueError("date out of range")
    # DSD: hour 5 bits then minute 6 bits, straddling bytes 5/4/3
    packed = (fix.hour << 6) | fix.minute                    # 11 bits
    buf[5] = (buf[5] & 0xFE) | (packed >> 10)
    buf[4] = (packed >> 2) & 0xFF
    buf[3] = (buf[3] & 0x3F) | ((packed & 0x03) << 6)
    # DSD: year-2000 (7 bits), month (4 bits), day-1 (5 bits), bytes 18/17
    date = ((fix.year - 2000) << 9) | (fix.month << 5) | (fix.day - 1)   # 16 bits
    buf[18] = date >> 8
    buf[17] = date & 0xFF


def kenwood_full(fix, layout=GATEWAY):
    """38 user data bytes of a Kenwood FULL position report (no CRC)."""
    buf = bytearray(FULL_BYTES)
    buf[0] = TYPE_FULL
    lon_frac = _position(buf, fix, layout.lon_fraction in ("gw", "both"))
    if layout.lon_fraction in ("dsd", "both"):
        buf[33] = lon_frac >> 7
        buf[32] = (lon_frac & 0x7F) << 1
    _put_time(buf, fix)
    tenths = course_tenths(fix.course)
    if layout.course == "gw":
        buf[23] = tenths >> 4
        buf[24] = tenths & 0x0F
    else:
        buf[23] = tenths & 0xFF
        buf[24] = tenths >> 8
    if layout.speed == "gw":
        # 10-bit whole part, 4-bit tenths; 1023 / 15 mean "no value" (GW)
        whole, tenth = (1023, 15) if fix.speed is None else divmod(_tenths(fix.speed, 10229, "speed"), 10)
        buf[25] = ((whole >> 6) << 4) | tenth
        buf[26] = whole & 0x3F
    else:
        # 14 bits of tenths (DSD); 0x3FFF is kept free as "no value"
        value = 0x3FFF if fix.speed is None else _tenths(fix.speed, 0x3FFE, "speed")
        buf[25] = value & 0xFF
        buf[26] = value >> 8
    elevation = fix.elevation & 0xFFFF
    buf[27] = elevation & 0xFF
    buf[28] = elevation >> 8
    buf[36], buf[37] = FULL_MARKER
    return bytes(buf)


def kenwood_short(fix):
    """17 user data bytes of a Kenwood SHORT report: position only (GW)."""
    buf = bytearray(SHORT_BYTES)
    buf[0] = TYPE_SHORT
    _position(buf, fix, True)
    return bytes(buf)


def kenwood_no_fix(kind="full"):
    """A report GW drops silently: the 'no fix' sentinels in the position."""
    buf = bytearray(FULL_BYTES if kind == "full" else SHORT_BYTES)
    buf[0] = TYPE_FULL if kind == "full" else TYPE_SHORT
    buf[9], buf[10] = 0xFE, 0xFF        # fraction 0x7FFF
    buf[11], buf[12] = 0xFF, 0xFF       # degrees/minutes 0xFFFF
    buf[13], buf[14] = 0xFE, 0xFF
    buf[15], buf[16] = 0xFF, 0xFF
    return bytes(buf)


def _read_position(data):
    lat_frac = (data[9] >> 1) | (data[10] << 7)
    lat_dm = (data[12] << 8) | data[11]
    lon_frac = (data[13] >> 1) | (data[14] << 7)
    lon_dm = (data[16] << 8) | data[15]
    return bool(data[9] & 1), lat_dm, lat_frac, bool(data[13] & 1), lon_dm, lon_frac


def parse_kenwood(data, layout=GATEWAY):
    """GpsFix of a Kenwood FULL or SHORT user data (CRC and pad already cut
    off), read the way `layout` places the disputed fields. Returns None for
    the 'no fix' sentinels, ValueError for a wrong type or a short message.
    SHORT gives course 0.0 and speed 0.0 with `partial` semantics: use
    parse_kenwood_fields() to see which fields are present."""
    return parse_kenwood_fields(data, layout)["fix"]


def parse_kenwood_fields(data, layout=GATEWAY):
    """dict: kind ('full' / 'short'), fix (GpsFix or None), raw (the integer
    field values, for the oracle), present (set of field names)."""
    if not data:
        raise ValueError("empty message")
    kind = {TYPE_FULL: "full", TYPE_SHORT: "short"}.get(data[0])
    if kind is None:
        raise ValueError("unknown Kenwood type 0x%02X" % data[0])
    if len(data) < (FULL_BYTES if kind == "full" else SHORT_BYTES):
        raise ValueError("short Kenwood %s message: %d bytes" % (kind, len(data)))
    lat_neg, lat_dm, lat_frac, lon_neg, lon_dm, lon_frac = _read_position(data)
    raw = {"lat_hem": int(lat_neg), "lat_degmin": lat_dm, "lat_fraction": lat_frac,
           "lon_hem": int(lon_neg), "lon_degmin": lon_dm, "lon_fraction": lon_frac}
    if kind == "full" and layout.lon_fraction == "dsd":
        lon_frac = (data[32] >> 1) | (data[33] << 7)
        raw["lon_fraction"] = lon_frac
    present = {"latitude", "longitude"}
    if 0x7FFF in (lat_frac, raw["lon_fraction"]) or 0xFFFF in (lat_dm, lon_dm):
        return {"kind": kind, "fix": None, "raw": raw, "present": set()}
    fields = {}
    if kind == "full":
        present |= {"course", "speed", "elevation", "time", "date"}
        if layout.course == "gw":
            course = (data[23] << 4) | (data[24] & 0x0F)
        else:
            course = ((data[24] & 0x0F) << 8) | data[23]
        if layout.speed == "gw":
            whole = ((data[25] & 0xF0) << 2) | (data[26] & 0x3F)
            tenth = data[25] & 0x0F
            speed_raw = None if (whole == 0x3FF or tenth == 0x0F) else whole * 10 + tenth
        else:
            speed_raw = ((data[26] & 0x3F) << 8) | data[25]
            speed_raw = None if speed_raw == 0x3FFF else speed_raw
        speed = None if speed_raw is None else speed_raw / 10.0
        elevation = (data[28] << 8) | data[27]
        if elevation & 0x8000:
            elevation -= 0x10000
        packed = ((data[5] & 1) << 10) | (data[4] << 2) | (data[3] >> 6)
        date = (data[18] << 8) | data[17]
        raw.update({"course_tenths": course, "speed_tenths": speed_raw})
        fields = {"course": course / 10.0, "speed": speed if speed is not None else 0.0,
                  "elevation": elevation, "hour": packed >> 6, "minute": packed & 0x3F,
                  "year": 2000 + (date >> 9), "month": (date >> 5) & 0x0F, "day": (date & 0x1F) + 1}
        if speed is None:
            present.discard("speed")
    fix = GpsFix(from_degmin(lat_neg, lat_dm, lat_frac), from_degmin(lon_neg, lon_dm, lon_frac), **fields)
    return {"kind": kind, "fix": fix, "raw": raw, "present": present}


# ------------------------------------------------------------------ Icom NMEA

def nmea_checksum(body):
    """XOR of the characters between '$' and '*' (public NMEA 0183 rule)."""
    value = 0
    for char in body.encode("ascii"):
        value ^= char
    return "%02X" % value


def nmea_rmc(fix, seconds=0, talker="GP"):
    """$GPRMC sentence for a fix, with checksum, no line end."""
    lat_neg, lat_dm, lat_frac = to_degmin(fix.latitude)
    lon_neg, lon_dm, lon_frac = to_degmin(fix.longitude)
    body = "%sRMC,%02d%02d%02d,A,%04d.%04d,%s,%05d.%04d,%s,%.1f,%.1f,%02d%02d%02d,,,A" % (
        talker, fix.hour, fix.minute, seconds, lat_dm, lat_frac, "S" if lat_neg else "N",
        lon_dm, lon_frac, "W" if lon_neg else "E", fix.speed, course_tenths(fix.course) / 10.0,
        fix.day, fix.month, fix.year % 100)
    return "$" + body + "*" + nmea_checksum(body)


def icom_user_data(fix):
    """Icom IDAS form: opcode 0x06 then the NMEA text (GW reads it, the
    sentence checksum is checked by both decoders)."""
    return bytes([0x06]) + nmea_rmc(fix).encode("ascii") + b"\r\n"


# ------------------------------------------------------------------ layer 3 octets

def packet_information(blocks, pad, *, confirmed=False, retry=False, start=True, circular=False,
                       fragment_count=0):
    """The 3 octets of the packet information (SPEC 6.5.19)."""
    if not 1 <= blocks <= MAX_BLOCKS or not 0 <= pad <= BLOCK_BYTES or not 0 <= fragment_count <= 0x1FF:
        raise ValueError("packet information out of range")
    return bytes([(0x80 if confirmed else 0) | (0x20 if retry else 0) | (blocks - 1),
                  (pad << 3) | (0x04 if start else 0) | (0x02 if circular else 0) | (fragment_count >> 8),
                  fragment_count & 0xFF])


def parse_packet_information(octets):
    return {"confirmed": bool(octets[0] & 0x80), "retry": bool(octets[0] & 0x20),
            "blocks": (octets[0] & 0x0F) + 1, "pad": octets[1] >> 3, "start": bool(octets[1] & 0x04),
            "circular": bool(octets[1] & 0x02), "fragment_count": ((octets[1] & 1) << 8) | octets[2]}


def dcall_header(src, dst, info, *, call_type=nxdn.CALL_GROUP, duplex=False, mode=0,
                 cipher=0, key=0, cc_option=0):
    """22-octet DCALL header. `info` is the 3 packet information octets.
    The data call option is duplex (bit 4) and the transmission mode in
    bits 2-0 (000 = 4800 bps, 010 = 9600 bps)."""
    option = ((call_type & 7) << 5) | (0x10 if duplex else 0) | (mode & 7)
    buf = bytearray(UDCH_OCTETS)
    buf[0] = MSG_DCALL_HEADER
    buf[1] = cc_option
    buf[2] = option
    buf[3:7] = src.to_bytes(2, "big") + dst.to_bytes(2, "big")
    buf[7] = ((cipher & 3) << 6) | (key & 0x3F)
    buf[8:11] = info
    return bytes(buf)


def dcall_block(frame_number, block_number, chunk):
    """22-octet DCALL user data format; chunk is exactly 20 octets."""
    if len(chunk) != BLOCK_BYTES or not (0 <= frame_number <= 15 and 0 <= block_number <= 15):
        raise ValueError("bad DCALL data block")
    return bytes([MSG_DCALL_DATA, (frame_number << 4) | block_number]) + bytes(chunk)


def tx_release(src, dst, call_type=nxdn.CALL_GROUP):
    """22-octet TX_REL (7 octets used)."""
    return (nxdn.layer3(nxdn.TX_REL, src, dst, call_type=call_type)[:7]).ljust(UDCH_OCTETS, b"\x00")


def udch_frame(octets, ran=0, lich=LICH_DATA):
    """33-byte reflector frame: LICH, RAN byte, the 22 octets, 9 zeros."""
    if len(octets) != UDCH_OCTETS:
        raise ValueError("a UDCH carries 22 octets")
    return bytes([lich, ran & 0x3F]) + bytes(octets) + bytes(nxdn.PAYLOAD_SIZE - 2 - UDCH_OCTETS)


def data_flags(frame, group=True):
    """NXDND flags byte as the reflector client derives it for a data frame:
    data bit, and header / trailer bit from the layer 3 message type when
    the LICH is one of the four data header/trailer values."""
    flags = nxdn.GROUP if group else 0
    if frame[0] & 0xF0 == 0x90:
        flags |= nxdn.DATA
        if frame[0] in LICH_DATA_FAMILY:
            kind = frame[FRAME_OFFSET] & 0x3F
            flags |= nxdn.HEADER if kind == MSG_DCALL_HEADER else 0
            flags |= nxdn.TRAILER if kind == MSG_TX_REL else 0
    return flags


# ------------------------------------------------------------------ packetiser

def with_crc(user):
    """User data + message CRC-32 (big endian)."""
    if len(user) > MAX_USER_BYTES:
        raise ValueError("user data over %d bytes" % MAX_USER_BYTES)
    return bytes(user) + message_crc32(user).to_bytes(CRC_BYTES, "big")


def split_blocks(message):
    """(list of 20-octet chunks, pad octets) for a message with its CRC."""
    blocks = max(1, -(-len(message) // BLOCK_BYTES))
    pad = blocks * BLOCK_BYTES - len(message)
    padded = bytes(message) + bytes(pad)
    return [padded[i * BLOCK_BYTES:(i + 1) * BLOCK_BYTES] for i in range(blocks)], pad


def data_call_octets(src, dst, user, *, crc=True, group=True, retry_blocks=None, header_opts=None,
                     corrupt_crc=False):
    """List of the 22-octet layer 3 messages of one data call: header, data
    blocks, TX_REL. retry_blocks (a list of block numbers, e.g. [3, 1])
    makes it a selective retry packet: S/R flag 1, block count of the
    retried blocks, packet frame numbers counting down over them, block
    numbers kept (SPEC 6.5.20, TS 1-B 5.4); the DV-Router side is expected
    to refuse those."""
    message = with_crc(user) if crc else bytes(user)
    if corrupt_crc:
        message = message[:-1] + bytes([message[-1] ^ 0x01])
    chunks, pad = split_blocks(message)
    total = len(chunks)
    call_type = nxdn.CALL_GROUP if group else nxdn.CALL_INDIVIDUAL
    opts = dict(header_opts or {})
    if retry_blocks is None:
        numbers = [total - 1 - i for i in range(total)]
        frames = numbers
        info = packet_information(total, pad, retry=False, **opts)
    else:
        numbers = list(retry_blocks)
        if any(not 0 <= n < total for n in numbers) or not numbers:
            raise ValueError("retry block outside the message")
        frames = [len(numbers) - 1 - i for i in range(len(numbers))]
        info = packet_information(len(numbers), pad, retry=True, **opts)
    out = [dcall_header(src, dst, info, call_type=call_type)]
    for frame_no, block_no in zip(frames, numbers):
        out.append(dcall_block(frame_no, block_no, chunks[total - 1 - block_no]))
    out.append(tx_release(src, dst, call_type))
    return out


def data_call_packets(src, dst, user, *, ran=0, lich=LICH_DATA, group=True, **kwargs):
    """The NXDND packets (43 bytes each) of one data call, in order."""
    return [nxdn.packet(src, dst, frame, data_flags(frame, group))
            for frame in (udch_frame(octets, ran, lich)
                          for octets in data_call_octets(src, dst, user, group=group, **kwargs))]


# ------------------------------------------------------------------ parser

@dataclass
class DataCall:
    src: int = 0
    dst: int = 0
    info: dict = field(default_factory=dict)
    blocks: list = field(default_factory=list)      # [(frame number, block number, chunk)]
    released: bool = False
    message: bytes = b""                            # user data + CRC, pad cut
    user: bytes = b""
    crc_ok: bool = False
    problems: list = field(default_factory=list)


def parse_call(packets):
    """Decode a list of NXDND packets (or 33-byte frames) back into a
    DataCall. `problems` lists everything a strict receiver would reject:
    a missing header or TX_REL, unequal frame/block numbers, a number that
    does not count down, a block count that differs from the blocks seen,
    the retry flag, a CRC mismatch."""
    call = DataCall()
    for item in packets:
        frame = item[10:] if len(item) == nxdn.PACKET_SIZE else item
        if len(frame) != nxdn.PAYLOAD_SIZE:
            call.problems.append("bad frame length")
            continue
        octets = frame[FRAME_OFFSET:FRAME_OFFSET + UDCH_OCTETS]
        kind = octets[0] & 0x3F
        if kind == MSG_DCALL_HEADER:
            if call.info:
                call.problems.append("second header")
            call.src = (octets[3] << 8) | octets[4]
            call.dst = (octets[5] << 8) | octets[6]
            call.info = parse_packet_information(octets[8:11])
            call.info["call_type"] = octets[2] >> 5
            call.info["cipher"] = octets[7] >> 6
        elif kind == MSG_DCALL_DATA:
            call.blocks.append((octets[1] >> 4, octets[1] & 0x0F, bytes(octets[2:22])))
        elif kind == MSG_TX_REL:
            call.released = True
        else:
            call.problems.append("unexpected message type 0x%02X" % kind)
    if not call.info:
        call.problems.append("no header")
        return call
    if not call.released:
        call.problems.append("no TX_REL")
    info = call.info
    if info["retry"]:
        call.problems.append("selective retry")
    if info["circular"] or not info["start"]:
        call.problems.append("fragmented")
    if info["cipher"]:
        call.problems.append("encrypted")
    if len(call.blocks) != info["blocks"]:
        call.problems.append("block count %d, %d blocks seen" % (info["blocks"], len(call.blocks)))
    frames = [b[0] for b in call.blocks]
    if frames != list(range(len(frames) - 1, -1, -1)):
        call.problems.append("frame numbers do not count down")
    if any(b[0] != b[1] for b in call.blocks) and not info["retry"]:
        call.problems.append("frame number differs from block number")
    data = b"".join(b[2] for b in call.blocks)
    cut = len(data) - info["pad"]
    if cut < CRC_BYTES:
        call.problems.append("message shorter than the CRC")
        return call
    call.message = data[:cut]
    call.user = call.message[:-CRC_BYTES]
    call.crc_ok = message_crc32(call.user) == int.from_bytes(call.message[-CRC_BYTES:], "big")
    if not call.crc_ok:
        call.problems.append("CRC mismatch")
    return call


# ------------------------------------------------------------------ known positions

# N0CALL area (Bangkok, decimal degrees) and edge cases. Expected degmin
# values are plain arithmetic: minutes = fraction of a degree * 60.
BASE_LAT, BASE_LON = 13.7563, 100.5018
BASE_EXPECTED = {"lat": (False, 1345, 3780), "lon": (False, 10030, 1080)}
COURSES = (0, 90, 180, 359)
SPEEDS = (0, 55, 120)               # wire unit, see GpsFix

KNOWN_POSITIONS = tuple(
    [("base-c%d-s%d" % (c, s), GpsFix(BASE_LAT, BASE_LON, float(c), float(s)))
     for c in COURSES for s in SPEEDS]
    + [("south-east", GpsFix(-33.8688, 151.2093, 45.0, 10.0)),
       ("north-west", GpsFix(40.7128, -74.0060, 270.0, 30.5)),
       ("origin", GpsFix(0.0, 0.0, 0.0, 0.0)),
       ("pole-dateline", GpsFix(89.9999, -179.9999, 359.9, 0.1)),
       ("lon-tiny-west", GpsFix(51.5, -0.0001, 12.3, 1.0))])
