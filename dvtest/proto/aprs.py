# SPDX-License-Identifier: MIT
"""APRS information fields that are not positions or messages: positionless weather, telemetry and general queries
(APRS Protocol Reference 1.0.1 chapters 12, 13 and 15). Used by the DP15 igate case (cases/test_dp15_igate.py) to inject
RF-heard frames that DV-Router must pass on to APRS-IS with the information field unchanged (data.md 9.3 rule 3).

  weather_info(10, 9, 5, 56, 220, 4, 5, 77, rain_1h=0, rain_24h=0, rain_midnight=0, humidity=50, pressure=9900,
               software="w", unit="RSW")
      -> "_10090556c220s004g005t077r000p000P000h50b09900wRSW"      (the chapter 12 example)
  telemetry_info(5, (199, 0, 255, 73, 123), "01101001")
      -> "T#005,199,000,255,073,123,01101001"                      (the chapter 13 example)
  query_info("APRS") -> "?APRS?"                                   (a general query, chapter 15; never gated)

Every builder returns str (ASCII); the parsers return the same fields and raise ValueError on any other shape, so a
builder and its parser are tested as a round trip (cases/unit/test_proto_aprs.py). The AX.25 frame around the field is
built with dvtest/kiss_tnc.py `ui_frame`. A missing numeric field is None (printed as dots, as the chapter allows).
"""
import re

WEATHER_FIELDS = ("wind_dir", "wind_speed", "gust", "temp_f", "rain_1h", "rain_24h", "rain_midnight", "humidity", "pressure")
QUERY_NAMES = ("APRS", "IGATE", "WX", "PING")


def _num(value, width, label, low=0, high=None):
    """`value` as a zero padded integer of `width` characters, or dots for None."""
    if value is None:
        return "." * width
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("%s must be an integer or None: %r" % (label, value))
    if value < low or (high is not None and value > high):
        raise ValueError("%s %d is outside %d..%s" % (label, value, low, high))
    text = "%0*d" % (width, value)
    if len(text) != width:
        raise ValueError("%s %d does not fit %d characters" % (label, value, width))
    return text


def _temp(value):
    """The three character Fahrenheit field: `077`, `-05`, `-99`, or dots."""
    if value is None:
        return "..."
    if not isinstance(value, int) or isinstance(value, bool) or not -99 <= value <= 999:
        raise ValueError("temp_f must be an integer in -99..999 or None: %r" % (value,))
    return ("%03d" % value) if value >= 0 else ("-%02d" % -value)


def weather_info(month, day, hour, minute, wind_dir, wind_speed, gust, temp_f, *, rain_1h=None, rain_24h=None,
                 rain_midnight=None, humidity=None, pressure=None, software="d", unit="vt"):
    """A positionless weather report `_MMDDHHMMcDDDsSSSgGGGtTTT[rRRR][pPPP][PPPP][hHH][bBBBBB]<software><unit>`.
    Speeds in mph, temperature in degrees Fahrenheit, rain in hundredths of an inch, humidity 1..100 percent (100 is
    printed `00`), pressure in tenths of a millibar. `software` is one character and `unit` 2 to 4 characters
    (APRS101 p.78); both are printed as given. An absent optional block is left out; the present ones keep the order of
    the chapter (r, p, P, h, b), which is the order the parser reads them in."""
    stamp = _num(month, 2, "month", 1, 12) + _num(day, 2, "day", 1, 31) + _num(hour, 2, "hour", 0, 23) + _num(minute, 2, "minute", 0, 59)
    out = "_" + stamp + "c" + _num(wind_dir, 3, "wind_dir", 0, 360) + "s" + _num(wind_speed, 3, "wind_speed", 0, 999) \
        + "g" + _num(gust, 3, "gust", 0, 999) + "t" + _temp(temp_f)
    optional = (("r", _num(rain_1h, 3, "rain_1h", 0, 999), rain_1h), ("p", _num(rain_24h, 3, "rain_24h", 0, 999), rain_24h),
                ("P", _num(rain_midnight, 3, "rain_midnight", 0, 999), rain_midnight),
                ("h", "00" if humidity == 100 else _num(humidity, 2, "humidity", 1, 99), humidity),
                ("b", _num(pressure, 5, "pressure", 0, 99999), pressure))
    for letter, text, value in optional:
        if value is not None:
            out += letter + text
    if not re.fullmatch(r"[A-Za-z0-9]", software):
        raise ValueError("software is one letter or digit: %r" % (software,))
    if not re.fullmatch(r"[A-Za-z0-9]{2,4}", unit):
        raise ValueError("unit is 2 to 4 letters or digits: %r" % (unit,))
    return out + software + unit


_WX_RE = re.compile(r"^_(\d{2})(\d{2})(\d{2})(\d{2})c([\d.]{3})s([\d.]{3})g([\d.]{3})t(-\d{2}|[\d.]{3})"
                    r"(?:r(\d{3}))?(?:p(\d{3}))?(?:P(\d{3}))?(?:h(\d{2}))?(?:b(\d{5}))?([A-Za-z0-9])([A-Za-z0-9]{2,4})$")


def parse_weather(info):
    """The fields of a positionless weather report built like `weather_info`, as a dict (month, day, hour, minute and the
    WEATHER_FIELDS, None for dots or an absent block; humidity 100 for `00`, plus software and unit)."""
    m = _WX_RE.match(info)
    if not m:
        raise ValueError("not a positionless weather report: %r" % (info,))

    def three(text):
        return None if set(text) == {"."} else int(text)

    g = m.groups()
    humidity = None if g[11] is None else (100 if g[11] == "00" else int(g[11]))
    return {"month": int(g[0]), "day": int(g[1]), "hour": int(g[2]), "minute": int(g[3]),
            "wind_dir": three(g[4]), "wind_speed": three(g[5]), "gust": three(g[6]), "temp_f": three(g[7]),
            "rain_1h": None if g[8] is None else int(g[8]), "rain_24h": None if g[9] is None else int(g[9]),
            "rain_midnight": None if g[10] is None else int(g[10]), "humidity": humidity,
            "pressure": None if g[12] is None else int(g[12]), "software": g[13], "unit": g[14]}


def telemetry_info(seq, analog, bits, comment=""):
    """A telemetry report `T#sss,a1,a2,a3,a4,a5,bbbbbbbb[comment]`: `seq` is 0..999 or the string `MIC`, `analog` is five
    integers 0..255, `bits` is eight characters 0/1 (or an integer 0..255, printed most significant bit first)."""
    if seq == "MIC":
        head = "MIC"
    elif isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq <= 999:
        head = "%03d" % seq
    else:
        raise ValueError("telemetry sequence is 0..999 or MIC: %r" % (seq,))
    values = tuple(analog)
    if len(values) != 5 or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in values):
        raise ValueError("telemetry has five analog values 0..255: %r" % (analog,))
    if isinstance(bits, int) and not isinstance(bits, bool):
        if not 0 <= bits <= 255:
            raise ValueError("telemetry bits as an integer are 0..255: %r" % (bits,))
        bits = format(bits, "08b")
    if not re.fullmatch(r"[01]{8}", bits):
        raise ValueError("telemetry bits are eight characters 0 or 1: %r" % (bits,))
    if "\r" in comment or "\n" in comment:
        raise ValueError("a comment has no line breaks")
    return "T#%s,%s,%s" % (head, ",".join("%03d" % v for v in values), bits) + comment


_TLM_RE = re.compile(r"^T#(\d{3}|MIC),(\d{3}),(\d{3}),(\d{3}),(\d{3}),(\d{3}),([01]{8})(.*)$", re.S)


def parse_telemetry(info):
    """{seq (int or "MIC"), analog (tuple of 5), bits (8 characters), comment} of a telemetry report, else ValueError."""
    m = _TLM_RE.match(info)
    if not m:
        raise ValueError("not a telemetry report: %r" % (info,))
    analog = tuple(int(m.group(i)) for i in range(2, 7))
    if any(v > 255 for v in analog):
        raise ValueError("telemetry analog value above 255: %r" % (info,))
    seq = m.group(1)
    return {"seq": seq if seq == "MIC" else int(seq), "analog": analog, "bits": m.group(7), "comment": m.group(8)}


def query_info(name="APRS"):
    """A general query information field `?NAME?` (chapter 15): a station asks every station in range to answer.
    A packet whose information field starts with `?` is a query and an igate must never pass it to the Internet."""
    if name not in QUERY_NAMES:
        raise ValueError("query name %r is not one of %s" % (name, ", ".join(QUERY_NAMES)))
    return "?%s?" % name


def parse_query(info):
    """The name of a general query built like `query_info`, else ValueError."""
    m = re.fullmatch(r"\?([A-Z]+)\?", info)
    if not m or m.group(1) not in QUERY_NAMES:
        raise ValueError("not a general query: %r" % (info,))
    return m.group(1)


# ---------------------------------------------------------------- positions and live objects with an HMS timestamp (APRS101 ch. 6, 8, 11)
# Used by the DP18 cases (cases/dp18_support.py): the node beacon `!` and the node objects `;NAME     *HHMMSSh` that DV-Router
# originates. The expected lines are built here from degrees times 10^7, independently of DV-Router's encoder.

def _hundredths_of_minute(e7):
    """(hemisphere sign, whole degrees, minutes as an int of hundredths) of |e7| degrees x 10^7, rounded to the nearest hundredth of
    a minute with half rounding up and the carry into the degrees (59.995 minutes becomes the next degree)."""
    sign = -1 if e7 < 0 else 1
    total = (abs(e7) * 6 + 5000) // 10000                  # hundredths of a minute: |e7| / 1e7 * 60 * 100
    return sign, total // 6000, total % 6000


def lat_text_e7(lat_e7, ambiguity=0):
    """`ddmm.hhN` of a latitude in degrees x 10^7; ambiguity 0..4 replaces that many trailing digits (hundredths, units of minute,
    tens of minute, then the decimal point's neighbour) with spaces (APRS101 ch. 6, position ambiguity)."""
    if not -900000000 <= lat_e7 <= 900000000:
        raise ValueError("latitude outside -90..90 degrees: %d" % lat_e7)
    sign, deg, hund = _hundredths_of_minute(lat_e7)
    text = "%02d%02d.%02d" % (deg, hund // 100, hund % 100) + ("N" if sign >= 0 else "S")
    return _blank(text, ambiguity)


def lon_text_e7(lon_e7, ambiguity=0):
    """`dddmm.hhE` of a longitude in degrees x 10^7."""
    if not -1800000000 <= lon_e7 <= 1800000000:
        raise ValueError("longitude outside -180..180 degrees: %d" % lon_e7)
    sign, deg, hund = _hundredths_of_minute(lon_e7)
    text = "%03d%02d.%02d" % (deg, hund // 100, hund % 100) + ("E" if sign >= 0 else "W")
    return _blank(text, ambiguity)


def _blank(text, ambiguity):
    """Blank `ambiguity` trailing digits of the number part of `ddmm.hhX` (hemisphere letter kept, the point kept):
    1 = hundredths, 2 = + units of minutes, 3 = + tens of minutes, 4 = + ... the minute digits (APRS101 p.30)."""
    if not 0 <= ambiguity <= 4:
        raise ValueError("ambiguity is 0..4: %r" % (ambiguity,))
    chars = list(text)
    digits = [i for i, c in enumerate(chars[:-1]) if c.isdigit()]          # digit positions, left to right
    for i in digits[len(digits) - ambiguity:] if ambiguity else []:
        chars[i] = " "
    return "".join(chars)


def position_report(lat_e7, lon_e7, symbol="/&", comment="", ambiguity=0):
    """An uncompressed position without timestamp and without messaging, `!ddmm.hhN/dddmm.hhE&comment`."""
    if len(symbol) != 2:
        raise ValueError("a symbol is the table/overlay byte and the code byte: %r" % (symbol,))
    return "!" + lat_text_e7(lat_e7, ambiguity) + symbol[0] + lon_text_e7(lon_e7, ambiguity) + symbol[1] + comment


def object_report(name, hms, lat_e7, lon_e7, symbol="/r", comment="", ambiguity=0, live=True):
    """A live (`*`) or killed (`_`) object with an HMS timestamp: `;NAME     *HHMMSShddmm.hhN/dddmm.hhE` + code + comment.
    The name is 1..9 printable ASCII bytes padded with spaces to 9; `hms` is `HHMMSS` (UTC)."""
    if not 1 <= len(name) <= 9 or any(not 32 <= ord(c) < 127 for c in name):
        raise ValueError("an object name is 1..9 printable ASCII bytes: %r" % (name,))
    if not re.fullmatch(r"(?:[01]\d|2[0-3])[0-5]\d[0-5]\d", hms):
        raise ValueError("hms is HHMMSS: %r" % (hms,))
    return ";%-9s%s%sh" % (name, "*" if live else "_", hms) + position_report(lat_e7, lon_e7, symbol, comment, ambiguity)[1:]


_OBJ_RE = re.compile(r"^;(.{9})([*_])(\d{6})h(\d{2}[\d ]{2}\.[\d ]{2}[NS])(.)(\d{3}[\d ]{2}\.[\d ]{2}[EW])(.)(.*)$", re.S)


def parse_object(info):
    """{name (stripped of its padding on the right), name_wire, live, hms, lat, symbol_table, lon, symbol_code, comment} of an
    object with an HMS timestamp, else ValueError."""
    m = _OBJ_RE.match(info)
    if not m:
        raise ValueError("not an object with an HMS timestamp: %r" % (info,))
    return {"name": m.group(1).rstrip(" "), "name_wire": m.group(1), "live": m.group(2) == "*", "hms": m.group(3), "lat": m.group(4),
            "symbol_table": m.group(5), "lon": m.group(6), "symbol_code": m.group(7), "comment": m.group(8)}


_POS_RE = re.compile(r"^!(\d{2}[\d ]{2}\.[\d ]{2}[NS])(.)(\d{3}[\d ]{2}\.[\d ]{2}[EW])(.)(.*)$", re.S)


def parse_position_report(info):
    """{lat, symbol_table, lon, symbol_code, comment} of a `!` position without timestamp, else ValueError."""
    m = _POS_RE.match(info)
    if not m:
        raise ValueError("not an uncompressed `!` position: %r" % (info,))
    return {"lat": m.group(1), "symbol_table": m.group(2), "lon": m.group(3), "symbol_code": m.group(4), "comment": m.group(5)}


# ---------------------------------------------------------------- DP6c / DP19: weather with a position, compressed weather, telemetry definitions, locator status
# Hand-built from APRS101 1.0.1 (chapter 12 pp.63-66 weather with a position, compressed positions p.38, chapter 13 pp.68-70 telemetry
# definitions, chapter 16 pp.81-82 Maidenhead locator status). The vectors G1-G19 of DV-Router's WXTLM.md are these literals; the unit tests
# (cases/unit/test_proto_aprs.py) pin each builder to its literal and round-trip it through the matching parser.

import math  # noqa: E402

KNOTS_TO_MPH = 1.15078


def base91(value, width=4):
    """`value` as `width` base-91 characters (chr(33 + digit)), most significant first (APRS101 p.38)."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 91 ** width:
        raise ValueError("base91 value %r does not fit %d characters" % (value, width))
    out = []
    for _ in range(width):
        value, digit = divmod(value, 91)
        out.append(chr(33 + digit))
    return "".join(reversed(out))


def unbase91(text):
    value = 0
    for ch in text:
        if not 33 <= ord(ch) <= 123:
            raise ValueError("not a base-91 character: %r" % (ch,))
        value = value * 91 + ord(ch) - 33
    return value


def compressed_position(lat_e7, lon_e7):
    """(lat, lon) as the four base-91 characters each of a compressed position: 380926 x (90 - lat) and 190463 x (180 + lon), truncated."""
    lat = 380926 * (900000000 - lat_e7) // 10000000
    lon = 190463 * (1800000000 + lon_e7) // 10000000
    return base91(lat), base91(lon)


def compressed_course_speed(course_deg, speed_knots):
    """The two characters `cs` of a compressed position: course in 4 degree steps and speed as 1.08^(s - 33) - 1 knots."""
    if not 0 <= course_deg <= 360 or speed_knots < 0:
        raise ValueError("course 0..360 and speed >= 0: %r %r" % (course_deg, speed_knots))
    return chr(33 + course_deg // 4) + chr(33 + int(round(math.log(speed_knots + 1) / math.log(1.08))))


def decode_compressed_cs(c, s):
    """(course degrees, speed mph rounded) of a compressed `cs` pair, the wind direction and speed of a compressed weather position."""
    course = (ord(c) - 33) * 4
    knots = 1.08 ** (ord(s) - 33) - 1
    return course, int(round(knots * KNOTS_TO_MPH))


def weather_tail(gust, temp_f, *, rain_1h=None, rain_24h=None, rain_midnight=None, humidity=None, pressure=None, software="w", unit="RSW"):
    """`g005t077r000p000P000h50b09900wRSW`: the weather values after the wind of a complete weather report."""
    body = weather_info(1, 1, 0, 0, 0, 0, gust, temp_f, rain_1h=rain_1h, rain_24h=rain_24h, rain_midnight=rain_midnight, humidity=humidity,
                        pressure=pressure, software=software, unit=unit)
    return body[body.index("g"):]


def weather_position_info(lat_e7, lon_e7, wind_dir, wind_speed, gust, temp_f, *, dti="!", timestamp=None, table="/", **values):
    """Complete weather on an uncompressed position (G5, G6): `<dti>[DHMz]ddmm.hhN<table>dddmm.hhE_DDD/SSSg...`. `dti` `!` or `=` has no timestamp;
    `/` and `@` carry the `DDHHMMz` timestamp. The symbol code is `_` (the weather symbol)."""
    if (dti in "/@") != (timestamp is not None):
        raise ValueError("a `/` or `@` report has a DDHHMMz timestamp, `!` and `=` have none")
    if timestamp is not None and not re.fullmatch(r"\d{6}z", timestamp):
        raise ValueError("timestamp is DDHHMMz: %r" % (timestamp,))
    wind = "%s/%s" % (_num(wind_dir, 3, "wind_dir", 0, 360), _num(wind_speed, 3, "wind_speed", 0, 999))
    return dti + (timestamp or "") + lat_text_e7(lat_e7) + table + lon_text_e7(lon_e7) + "_" + wind + weather_tail(gust, temp_f, **values)


def weather_compressed_info(lat_e7, lon_e7, c, s, gust, temp_f, *, dti="=", table="/", t_type="[", **values):
    """Complete weather on a compressed position (G7): `<dti><table>YYYYXXXX_cs<T>g005t077...`. `c` and `s` are the two characters of the compressed
    course/speed pair, which carry the wind direction and speed; `s` = `z` is out of range (G7 variant: the marker and the weather stay, the wind
    speed is dropped)."""
    lat, lon = compressed_position(lat_e7, lon_e7)
    if len(c) != 1 or len(s) != 1 or len(t_type) != 1:
        raise ValueError("c, s and t_type are one character each")
    return dti + table + lat + lon + "_" + c + s + t_type + weather_tail(gust, temp_f, **values)


_WXPOS_RE = re.compile(r"^([!=/@])(?:(\d{6}z))?(\d{4}\.\d{2}[NS])(.)(\d{5}\.\d{2}[EW])_(\d{3})/(\d{3})(g.*)$", re.S)
_WXCMP_RE = re.compile(r"^([!=/@])(.)([!-{]{4})([!-{]{4})_(.)(.)(.)(g.*)$", re.S)


def parse_weather_position(info):
    """{dti, timestamp, lat, table, lon, wind_dir, wind_speed, tail} of a G5/G6 report, else ValueError."""
    m = _WXPOS_RE.match(info)
    if not m:
        raise ValueError("not a weather report on an uncompressed position: %r" % (info,))
    return {"dti": m.group(1), "timestamp": m.group(2), "lat": m.group(3), "table": m.group(4), "lon": m.group(5),
            "wind_dir": int(m.group(6)), "wind_speed": int(m.group(7)), "tail": m.group(8)}


def parse_weather_compressed(info):
    """{dti, table, lat, lon, c, s, t_type, tail, wind_dir, wind_mph} of a G7 report (wind_* from `cs`, None when `s` is out of range), else ValueError."""
    m = _WXCMP_RE.match(info)
    if not m:
        raise ValueError("not a weather report on a compressed position: %r" % (info,))
    c, s = m.group(5), m.group(6)
    course, mph = decode_compressed_cs(c, s)
    return {"dti": m.group(1), "table": m.group(2), "lat": unbase91(m.group(3)), "lon": unbase91(m.group(4)), "c": c, "s": s, "t_type": m.group(7),
            "tail": m.group(8), "wind_dir": course, "wind_mph": mph if mph <= 999 else None}


DEFINITION_KINDS = ("PARM", "UNIT", "EQNS", "BITS")


def message_info(addressee, text):
    """`:ADDRESSEE:text` with the addressee padded to nine characters (no message id, so it is never acked)."""
    if not 1 <= len(addressee) <= 9:
        raise ValueError("an APRS addressee is 1 to 9 characters: %r" % (addressee,))
    return ":%-9s:%s" % (addressee, text)


def definition_info(target, kind, fields, project=None):
    """A telemetry definition message to the telemetering station `target` (APRS101 pp.68-70): `:N0CALL-11:PARM.a,b,...`, `UNIT.`, `EQNS.` (15 coefficients a,b,c
    per analog channel, kept as written) or `BITS.<8 bits>,<project>`. It must be sent by the same BASE callsign as `target` (DV-Router: other senders are
    ignored); it never becomes a room item."""
    if kind not in DEFINITION_KINDS:
        raise ValueError("definition kind %r is not one of %s" % (kind, DEFINITION_KINDS))
    if kind == "BITS":
        if not re.fullmatch(r"[01]{8}", str(fields)):
            raise ValueError("BITS sense is eight characters 0 or 1: %r" % (fields,))
        if project is None or not 1 <= len(project) <= 23:
            raise ValueError("BITS carries a project name of 1..23 characters: %r" % (project,))
        return message_info(target, "BITS.%s,%s" % (fields, project))
    values = list(fields)
    limit = {"PARM": 13, "UNIT": 13, "EQNS": 15}[kind]
    if not 1 <= len(values) <= limit:
        raise ValueError("%s has 1..%d fields: %r" % (kind, limit, values))
    if kind == "EQNS" and len(values) % 3:
        raise ValueError("EQNS has a, b, c per channel (a multiple of three fields): %r" % (values,))
    if any(("," in str(v)) or ("|" in str(v)) or ("~" in str(v)) for v in values):
        raise ValueError("a field has no comma, bar or tilde: %r" % (values,))
    return message_info(target, "%s.%s" % (kind, ",".join(str(v) for v in values)))


def parse_definition(info):
    """(target, kind, fields list or (bits, project)) of a definition message built like `definition_info`, else ValueError."""
    m = re.fullmatch(r":(.{9}):(PARM|UNIT|EQNS|BITS)\.(.*)", info, re.S)
    if not m:
        raise ValueError("not a telemetry definition message: %r" % (info,))
    if m.group(2) == "BITS":
        bits, _comma, project = m.group(3).partition(",")
        return m.group(1).rstrip(" "), "BITS", (bits, project)
    return m.group(1).rstrip(" "), m.group(2), m.group(3).split(",")


# APRS101 pp.68-70 example, with synthetic names: five analog channels A1..A5 then B1..B8 (B6..B8 unnamed)
G10_PARM = ["Battery", "Btemp", "ATemp", "Pres", "Alt", "Camra", "Chut", "Sun", "10m", "ATV"]
G10_UNIT = ["v/100", "deg.F", "deg.F", "Mbar", "Kft", "Click", "OPEN", "on", "on", "hi"]
G10_EQNS = ["0", "5.2", "0", "0", ".53", "-32", "3", "4.39", "49", "-32", "3", "18", "1", "2", "3"]
G10_BITS = "10110000"
G10_PROJECT = "N0CALL Balloon"
G9_REPORT = (5, (199, 0, 255, 73, 123), "01101001")
# the calibrated values a * x^2 + b * x + c of G9 under G10, computed by hand (WXTLM.md)
G9_CALIBRATED = ("1034.8", "-32.00", "196243.45", "-170291", "15378")


def telemetry_definitions(target):
    """The four G10 messages for `target` in the order PARM, UNIT, EQNS, BITS."""
    return [definition_info(target, "PARM", G10_PARM), definition_info(target, "UNIT", G10_UNIT), definition_info(target, "EQNS", G10_EQNS),
            definition_info(target, "BITS", G10_BITS, G10_PROJECT)]


_GRID_FIELD = "ABCDEFGHIJKLMNOPQR"
_GRID_SUB = "ABCDEFGHIJKLMNOPQRSTUVWX"


def locator_status(locator, symbol="/-", text=""):
    """A chapter 16 locator status `>IO91SX/- text`: a four or six character Maidenhead locator (field letters A-R, square digits, optional subsquare letters
    A-X, any case), the symbol table and code, and optional text that starts with a space. DV-Router treats it as a POSITION (DP19)."""
    if not re.fullmatch(r"[A-Ra-r]{2}\d\d(?:[A-Xa-x]{2})?", locator):
        raise ValueError("not a four or six character Maidenhead locator: %r" % (locator,))
    if len(symbol) != 2 or not 33 <= ord(symbol[1]) <= 126 or not (symbol[0] in "/\\" or symbol[0].isalnum()):
        raise ValueError("a symbol is the table byte (/, \\, A-Z, 0-9) and the code byte: %r" % (symbol,))
    return ">" + locator + symbol + ((" " + text) if text else "")


def is_locator_status(info):
    """True when `info` is a locator status by the chapter 16 rules above (the classifier DV-Router calls spatial)."""
    m = re.fullmatch(r">([A-Ra-r]{2}\d\d(?:[A-Xa-x]{2})?)([/\\A-Za-z0-9])([!-~])(?: .*)?", info, re.S)
    return bool(m)


# valid forms and look-alikes of DP19 (data.md 13.4): every look-alike stays an ordinary status
LOCATOR_VALID = {"four": ">IO91/- locator4", "six": ">IO91SX/- locator6", "six_lower": ">io91sx\\- lower6", "four_no_text": ">JN58/-"}
LOCATOR_LOOKALIKES = {
    "leading_space": "> IO91SX/- space",          # a leading space
    "no_symbol": ">KN07",                          # no symbol pair (">IO91SX" would read as IO91 plus the symbol pair S X)
    "no_symbol_text": ">IO91 text",                # a space where the symbol table byte belongs
    "bad_field": ">ZZ91SX/- field",                # field letters beyond R
    "bad_square": ">IOA1SX/- square",              # a letter where a digit belongs
    "bad_subsquare": ">IO91YZ/- sub",              # subsquare letters beyond X
    "five_chars": ">IO91S/- five",                 # five characters
    "eight_chars": ">IO91SX12/- eight",            # eight characters (a longer locator is not chapter 16)
    "no_space": ">IO91SX/-nospace",                # the text does not start with a space
}
