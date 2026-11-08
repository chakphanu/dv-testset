# SPDX-License-Identifier: MIT
"""Pure helpers of the D-PRS position cases (cases/test_dp7_dprs.py, docs/dp7-cases.md "D-PRS cases"): decoders of the APRS
compressed position and of the Mic-E position written from the APRS 1.0.1 text (chapters 9 and 10), the printable Mic-E
speed / course encoding the specification's own example uses, D-PRS line builders and parsers, and the scan of the M17 stream
packets a listener received for GNSS META. No sockets, no rig.

Vectors come from the APRS Protocol Reference 1.0.1 (29 August 2000), NOT from any server's code:

* chapter 9, "Lat/Long Encoding" and the example list: the compressed field `/5L!!<*e7>7P[` is 49 30.00 N, 72 45.00 W,
  course 88, speed 36.2 knots, symbol table `/`, symbol code `>`, T byte `[` (0x3A + 33: current fix, RMC, software).
  The decode is exact: YYYY = 15427503 gives 49.5 N; XXXX = 20427156 gives -72.75000394 (the text rounds it to -72.75).
  `{?` as cs is a pre-calculated radio range (about 20 miles), `S]` with T = `S` (GGA) is an altitude of 10004 feet.
* chapter 10, "Mic-E Destination Address Field Encoding" example: latitude 33 25.64 N, standard message bits 1/0/0
  (M3 Returning), North, West. The spec's worked table prints the destination as `S32U6T`, whose fifth byte `6` means a
  longitude offset of +0; the information-field example below needs +100 degrees (112 = 100 + 12), which is byte `V`.
  The two examples of the spec disagree on this one byte. Both destinations are valid frames: the main Mic-E case sends the
  consistent `S32UVT` (33 25.64 N, 112 07.74 W) and a second case sends `S32U6T` taken literally (33 25.64 N, 12 07.74 W).
* chapter 10, "Example of Decoding the Information Field Data": the nine bytes `` `(_fn"Oj/ `` (data type `` ` ``, d+28 = `(`,
  m+28 = `_`, h+28 = `f`, SP+28 = `n`, DC+28 = `"`, SE+28 = `O`, symbol code `j`, table `/`) with offset +100 decode to
  longitude 112 07.74 W, speed 20 knots, course 251 (the printable one of the two encodings the spec allows).
* chapter 10, "Mic-E Position Ambiguity": destination `T4SQZZ` has two ambiguous latitude digits, longitude `(_f` is then
  112 degrees 7 minutes.
"""
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dvtest import aprs_cases as ac  # noqa: E402
from dvtest.proto import m17, m17v3  # noqa: E402

# ---------------------------------------------------------------- the vectors (APRS 1.0.1)

SPEC_COMPRESSED = b"/5L!!<*e7>7P["                  # chapter 9 (the same bytes as dvtest.aprs_cases.POS_C)
SPEC_COMPRESSED_RANGE = b"/5L!!<*e7>{?!"            # chapter 9 example list: radio range
SPEC_COMPRESSED_ALTITUDE = b"/5L!!<*e7OS]S"         # chapter 9 example list: GGA altitude
SPEC_COMPRESSED_LAT = Fraction(99, 2)                # 49 30.00 N
SPEC_COMPRESSED_LON_TEXT = Fraction(-291, 4)         # 72 45.00 W as the text rounds it; the exact decode differs by 4e-6

SPEC_MICE_DEST_SPEC = "S32U6T"                       # as printed in the chapter 10 table (offset +0, see the module docstring)
SPEC_MICE_DEST = "S32UVT"                            # consistent with the information example (offset +100): DERIVED
SPEC_MICE_INFO = b"`(_fn\"Oj/"                        # chapter 10 information field example, 9 bytes
SPEC_MICE_LAT = Fraction(33) + Fraction(2564, 6000)               # 33 25.64 N
SPEC_MICE_LON = -(Fraction(112) + Fraction(774, 6000))           # 112 07.74 W
SPEC_MICE_LON_OFFSET0 = -(Fraction(12) + Fraction(774, 6000))      # the same information field with destination S32U6T: 12 07.74 W
SPEC_MICE_AMBIGUITY_DEST = "T4SQZZ"                  # chapter 10, Mic-E Position Ambiguity
SPEC_MICE_AMBIGUITY_INFO = b"`(_f"                   # the longitude bytes the spec pairs with it

POSITION_ERROR_E7 = 200                               # the tolerance of position_support (2e-5 degrees)


def e7(value):
    """A Fraction or float of degrees in 1e-7 degrees, rounded half away from zero."""
    scaled = Fraction(value) * 10 ** 7
    return int(scaled + Fraction(1, 2)) if scaled >= 0 else -int(-scaled + Fraction(1, 2))


# ---------------------------------------------------------------- compressed position (chapter 9)

def _base91(chars):
    value = 0
    for ch in chars:
        if not 33 <= ch <= 123:
            raise ValueError("not a base-91 character: %r" % chr(ch))
        value = value * 91 + (ch - 33)
    return value


def compressed_decode(body):
    """Decodes the 13 byte compressed field `/YYYYXXXX$csT` (the part after the `!` or `=` data type). Returns
    {lat, lon (exact Fractions of degrees), table, code, cs (None, ("course_speed", course, knots), ("range", miles) or
    ("altitude", feet)), t (the T byte value minus 33 as an int, or None when c is a space)}. Formulas of chapter 9:
    lat = 90 - YYYY / 380926, lon = -180 + XXXX / 190463, course = c * 4, speed = 1.08 ** s - 1, range = 2 * 1.08 ** s,
    altitude = 1.002 ** (c * 91 + s) feet; cs is an altitude only when bits 4..3 of T are 10 (GGA)."""
    body = bytes(body)
    if len(body) != 13:
        raise ValueError("the compressed field is 13 bytes")
    lat = 90 - Fraction(_base91(body[1:5]), 380926)
    lon = -180 + Fraction(_base91(body[5:9]), 190463)
    c, s, t = body[10] - 33, body[11] - 33, body[12] - 33
    cs, tval = None, None
    if body[10] != 0x20:
        tval = t
        if (t >> 3) & 3 == 2:
            cs = ("altitude", 1.002 ** (c * 91 + s))
        elif body[10] == ord("{"):
            cs = ("range", 2 * 1.08 ** s)
        elif 0 <= c <= 89:
            cs = ("course_speed", c * 4, 1.08 ** s - 1)
    return {"lat": lat, "lon": lon, "table": chr(body[0]), "code": chr(body[9]), "cs": cs, "t": tval}


def compressed_info(body, dti="!"):
    """The APRS information field of a compressed position without timestamp: the data type indicator and the 13 bytes."""
    return dti.encode("ascii") + bytes(body)


# ---------------------------------------------------------------- Mic-E (chapter 10)

def mice_destination_digits(dest):
    """Decodes the six destination characters. Returns {digits (list of 6 ints, a space as 0), spaces (count of space digits),
    msg_bits (3 ints), custom (list of 3 bools), north, lon_offset, west}. Table of chapter 10: 0-9 digit, flag 0; A-J digit
    (custom bit, bytes 1-3 only); K space (custom); L space, flag 0; P-Y digit, flag 1; Z space, flag 1."""
    dest = str(dest)
    if len(dest) < 6:
        raise ValueError("a Mic-E destination has at least 6 characters")
    digits, flags, custom, spaces = [], [], [], 0
    for index, ch in enumerate(dest[:6]):
        if "0" <= ch <= "9":
            digit, flag, cus, space = ord(ch) - 48, 0, False, False
        elif "A" <= ch <= "J" and index < 3:
            digit, flag, cus, space = ord(ch) - 65, 1, True, False
        elif ch == "K" and index < 3:
            digit, flag, cus, space = 0, 1, True, True
        elif ch == "L":
            digit, flag, cus, space = 0, 0, False, True
        elif "P" <= ch <= "Y":
            digit, flag, cus, space = ord(ch) - 80, 1, False, False
        elif ch == "Z":
            digit, flag, cus, space = 0, 1, False, True
        else:
            raise ValueError("character %r at destination byte %d is not a Mic-E code" % (ch, index + 1))
        digits.append(digit)
        flags.append(flag)
        custom.append(cus)
        spaces += 1 if space else 0
    return {"digits": digits, "spaces": spaces, "msg_bits": flags[:3], "custom": custom[:3], "north": bool(flags[3]),
            "lon_offset": bool(flags[4]), "west": bool(flags[5])}


def mice_decode(dest, info):
    """Decodes a Mic-E position from the destination address and the information field (bytes, data type indicator first).
    Returns {lat, lon (exact Fractions, a blanked digit counts 0), ambiguity (number of space digits), speed, course,
    message_bits, custom, code, table}. Rules of chapter 10: longitude degrees = d+28 - 28, +100 when the destination says
    so, minus 80 for 180..189, minus 190 for 190..199; minutes minus 60 when 60 or more; hundredths = h+28 - 28; speed =
    10 * (SP+28 - 28) + (DC+28 - 28) div 10, minus 800 when 800 or more; course = 100 * ((DC+28 - 28) mod 10) + (SE+28 - 28),
    minus 400 when 400 or more (this reads both encodings of the spec)."""
    info = bytes(info)
    if len(info) < 9:
        raise ValueError("a Mic-E information field is at least 9 bytes")
    if info[0] not in (0x60, 0x27, 0x1C, 0x1D):
        raise ValueError("data type indicator %r is not a Mic-E one" % chr(info[0]))
    d = mice_destination_digits(dest)
    g = d["digits"]
    lat = Fraction(g[0] * 10 + g[1]) + Fraction(g[2] * 1000 + g[3] * 100 + g[4] * 10 + g[5], 6000)
    lat = lat if d["north"] else -lat
    deg = info[1] - 28 + (100 if d["lon_offset"] else 0)
    if 180 <= deg <= 189:
        deg -= 80
    elif 190 <= deg <= 199:
        deg -= 190
    minutes = info[2] - 28
    minutes -= 60 if minutes >= 60 else 0
    hundredths = info[3] - 28
    lon = Fraction(deg) + Fraction(minutes * 100 + hundredths, 6000)
    lon = -lon if d["west"] else lon
    speed = (info[4] - 28) * 10 + (info[5] - 28) // 10
    course = ((info[5] - 28) % 10) * 100 + (info[6] - 28)
    speed -= 800 if speed >= 800 else 0
    course -= 400 if course >= 400 else 0
    return {"lat": lat, "lon": lon, "ambiguity": d["spaces"], "speed": speed, "course": course,
            "message_bits": d["msg_bits"], "custom": d["custom"], "code": chr(info[7]), "table": chr(info[8])}


def mice_info_printable(lon, speed_kn, course, table="/", code=">", dti="`", rest=b""):
    """The Mic-E information field in the printable one of the two speed / course encodings (the one the specification's
    example uses: SP+28 = `n` for 20 knots, DC+28 = `"` for course 200..299). dvtest.aprs_cases.mice_info writes the other
    encoding (control characters 0x1c to 0x1f for small values), which is equally valid but not printable."""
    base = bytearray(ac.mice_info(lon, speed_kn, course, table, code, dti=dti, rest=rest))
    tens, units = int(speed_kn) // 10, int(speed_kn) % 10
    base[4] = tens + (108 if tens < 20 else 28)
    base[5] = units * 10 + int(course) // 100 + 4 + 28
    return bytes(base)


def mice_pair(lat, lon, speed_kn, course, msg_index, table="/", code=">"):
    """(destination, information field) of a Mic-E position, built with dvtest.aprs_cases (the destination) and the printable
    encoding (the information field)."""
    dest = ac.mice_destination(lat, msg_index, west=lon < 0, lon_offset=ac.mice_lon_offset(lon))
    return dest, mice_info_printable(lon, speed_kn, course, table, code)


# ---------------------------------------------------------------- D-PRS lines

def dprs_payload(call, info, dest="API510", path="DSTAR*"):
    """The D-PRS line payload `CALL>DEST,PATH:info` as text for dvtest.dstar_sd (latin-1 bytes in `info` are kept)."""
    if isinstance(info, (bytes, bytearray)):
        info = bytes(info).decode("latin-1")
    return "%s>%s,%s:%s" % (call, dest, path, info)


def parse_dprs_line(line):
    """(source, destination, path, information field bytes) of a D-PRS line as dvtest.dstar_sd.Decoder reports it (the bytes after
    `$$CRCxxxx,`, a trailing CR dropped), or None when it is not `SRC>DEST[,PATH]:info`."""
    line = bytes(line).rstrip(b"\r\n")
    head, sep, info = line.partition(b":")
    if not sep or b">" not in head:
        return None
    source, _gt, rest = head.partition(b">")
    dest, _comma, path = rest.partition(b",")
    return source.decode("latin-1"), dest.decode("latin-1"), path.decode("latin-1"), info


def dprs_events(overs, decoder_factory):
    """The D-PRS events a decoder reads from the overs (slowdata.overs()) of a listener: [(over index, event)]. The frame list of
    an over is (counter & 0x1F, wire slow data field)."""
    out = []
    for index, over in enumerate(overs):
        if not over["voice"]:
            continue
        decoder = decoder_factory()
        for event in decoder.feed_all([(v["counter"] & 0x1F, v["slow"]) for v in over["voice"]]):
            if event["kind"] == "dprs":
                out.append((index, event))
    return out


# ---------------------------------------------------------------- M17 GNSS META on a listener

GNSS_LEGACY_TYPE = m17v3.legacy_type(1, 2, 0, 1)                      # voice, no encryption, null subtype 1 = GNSS: 0x0025
GNSS_V3_TYPE = m17v3.v3_type(m17v3.PAYLOAD_VOICE3200, 0, False, m17v3.META_GNSS)    # 0x2010


def is_gnss_type(type_):
    """True when a stream TYPE announces GNSS META (M17 specification, the TYPE field of the LSF: legacy encryption subtype 1 with no encryption,
    "GNSS position data"; v3 META type 1; production uses the legacy form, v3 only where a P26j gate enables it): the v3 TYPE (top nibble not 0) with META type 1, or the legacy TYPE (top nibble
    0) of a voice stream with no encryption and subtype 1."""
    if type_ >> 12:
        return m17v3.parse_v3_type(type_)["meta"] == m17v3.META_GNSS
    f = m17v3.legacy_fields(type_)
    return bool(f["stream"]) and f["enc_type"] == 0 and f["enc_subtype"] == 1


def decode_gnss_or_none(meta):
    try:
        return m17v3.decode_gnss(meta)
    except ValueError:
        return None


def gnss_scan(packets, near=None, within=1.0):
    """Looks for GNSS META in parsed M17 stream packets (dicts of dvtest.proto.m17.parse_stream, optionally with "at").
    typed: packets whose TYPE announces GNSS and whose META is not all zero. stray: packets with any other TYPE whose META
    nonzero decodes as a GNSS position with the lat/lon valid bit within `within` degrees of `near` = (lat, lon) (a position
    sent with the wrong TYPE; only when `near` is given). Returns {total, typed, stray, positions} where positions are the
    decoded (lat, lon) of the typed packets."""
    typed, stray, positions = [], [], []
    for pkt in packets:
        meta = bytes(pkt["meta"])
        if not any(meta):
            continue
        decoded = decode_gnss_or_none(meta)
        if is_gnss_type(pkt["type"]):
            typed.append(pkt)
            if decoded and decoded["validity"] & m17v3.V_LATLON:
                positions.append((decoded["lat"], decoded["lon"]))
        elif near is not None and decoded and decoded["validity"] & m17v3.V_LATLON \
                and abs(decoded["lat"] - near[0]) <= within and abs(decoded["lon"] - near[1]) <= within:
            stray.append(pkt)
    return {"total": len(packets), "typed": typed, "stray": stray, "positions": positions}


def lsf_view(packets):
    """The LSF META view of parsed M17 stream packets, per stream id. In an M17 stream over IP every 54 byte packet carries a copy of the LSF
    (DST, SRC, TYPE, META; the LICH chunks of the air interface are not on this plane), so the LSF META a listener sees is read from
    the copies in the stream's own frames. Returns {stream_id: {"packets", "lsfs" (distinct (TYPE, META) in order of first appearance),
    "typed" (the packets whose TYPE announces GNSS META and whose META is not all zero: {"fn", "at", "type", "decoded" or None})}}.
    Text META (TYPE 0x0005) and zero META are in "lsfs" only."""
    streams = {}
    for pkt in packets:
        view = streams.setdefault(pkt["stream_id"], {"packets": 0, "lsfs": [], "typed": [], "first_at": []})
        view["packets"] += 1
        key = (pkt["type"], bytes(pkt["meta"]))
        if key not in view["lsfs"]:
            view["lsfs"].append(key)
            view["first_at"].append(pkt.get("at"))          # arrival time (monotonic, the clock of the over's t0) of the first copy
        if is_gnss_type(pkt["type"]) and any(pkt["meta"]):
            view["typed"].append({"fn": pkt["fn"], "at": pkt.get("at"), "type": pkt["type"], "decoded": decode_gnss_or_none(pkt["meta"])})
    return streams


def gnss_in_lsf(packets, lat, lon, tol_e7=POSITION_ERROR_E7):
    """Looks in the LSF META of the stream(s) in `packets` for a GNSS position equal to (lat, lon) degrees within `tol_e7` (1e-7 degrees).
    Returns {"streams" (lsf_view), "typed" (all GNSS-typed LSF copies), "decoded" (those whose META decodes with the lat/lon valid bit),
    "matches" (decoded within tol_e7 of lat, lon), "off" (decoded but too far: [(lat, lon)])}."""
    streams = lsf_view(packets)
    typed = [t for view in streams.values() for t in view["typed"]]
    decoded = [t for t in typed if t["decoded"] and t["decoded"]["validity"] & m17v3.V_LATLON]
    matches, off = [], []
    for t in decoded:
        d = t["decoded"]
        if abs(d["lat"] - lat) * 1e7 <= tol_e7 and abs(d["lon"] - lon) * 1e7 <= tol_e7:
            matches.append(t)
        else:
            off.append((d["lat"], d["lon"]))
    return {"streams": streams, "typed": typed, "decoded": decoded, "matches": matches, "off": off}


def m17p_packets(inbox, times=None, start=0):
    """Parsed M17 packet-mode datagrams (parse_m17p dicts, plus "at") of a client inbox from index `start`."""
    out = []
    for index in range(start, len(inbox)):
        data = inbox[index]
        parsed = m17.parse_m17p(data, trailer=False) if bytes(data).startswith(b"M17P") else None
        if parsed:
            out.append(dict(parsed, at=times[index]) if times is not None else parsed)
    return out


def stream_packets(inbox, times=None, start=0):
    """Parsed M17 stream packets (parse_stream dicts, plus "at" when `times` is given) of a client inbox from index `start`."""
    out = []
    for index in range(start, len(inbox)):
        parsed = m17.parse_stream(inbox[index])
        if parsed:
            if times is not None:
                parsed = dict(parsed, at=times[index])
            out.append(parsed)
    return out
