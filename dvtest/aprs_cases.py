# SPDX-License-Identifier: MIT
"""APRS information strings for the decoder oracle (HWFREE1 item 3, docs/direwolf-cases.md).

The strings are written from the APRS 1.0.1 specification formats (chapters 5 to
12): one group per type DV-Router's "APRS core" parses (messages, acks, rejects,
bulletins, announcements, plain / compressed / Mic-E positions, timestamps,
PHG / RNG / DFS / course-speed, weather, DAO, ambiguity, status, objects, items,
third party). At least two strings per group, with the edge cases of each.

Every callsign is N0CALL / N0TEST style, every coordinate is invented (open sea,
the equator, or a number out of the specification's own examples). Nothing here
comes from Dire Wolf; Dire Wolf's decoder only judges these strings
(dvtest.oracle_aprs). The small encoders (compressed position, Mic-E) exist so
the strings can be built from numbers; the unit tests check them against the
examples printed in the specification and, through the committed vectors,
against the decoder.

All values are bytes (an information field is bytes, not text).
"""
import math

# ------------------------------------------------------------------ plain positions


def lat_text(lat, amb=0):
    """DDMM.mmN (8 characters); `amb` 1..4 replaces the last digits by spaces."""
    hemi = "N" if lat >= 0 else "S"
    a = abs(lat)
    deg = int(a)
    minutes = round((a - deg) * 60 * 100) / 100.0
    if minutes >= 60:
        deg, minutes = deg + 1, 0.0
    text = "%02d%05.2f%s" % (deg, minutes, hemi)
    return _blank(text, amb)


def lon_text(lon, amb=0):
    """DDDMM.mmE (9 characters)."""
    hemi = "E" if lon >= 0 else "W"
    a = abs(lon)
    deg = int(a)
    minutes = round((a - deg) * 60 * 100) / 100.0
    if minutes >= 60:
        deg, minutes = deg + 1, 0.0
    text = "%03d%05.2f%s" % (deg, minutes, hemi)
    return _blank(text, amb)


def _blank(text, amb):
    """Ambiguity: blank `amb` digits counted from the right (hundredths of a minute,
    tenths, minutes units, minutes tens)."""
    if not 0 <= amb <= 4:
        raise ValueError("ambiguity is 0..4")
    chars = list(text)
    digits = [i for i, ch in enumerate(chars) if ch.isdigit()]
    for i in list(reversed(digits))[:amb]:
        chars[i] = " "
    return "".join(chars)


# ------------------------------------------------------------------ compressed positions

def base91(value, width):
    """`width` base-91 characters (33..123), most significant first."""
    out = []
    for _ in range(width):
        out.append(33 + value % 91)
        value //= 91
    return bytes(reversed(out))


def compressed(lat, lon, table="/", code=">", *, course=None, speed_kn=None, alt_ft=None, range_mi=None,
               no_cs=False, t_byte=None):
    """Compressed position body: symbol table, YYYY, XXXX, symbol code, cs, T (13 bytes).
    cs is course/speed, or altitude (T byte says GGA), or radio range, or a blank pair."""
    y = int(380926 * (90.0 - lat))       # truncated, as the specification says
    x = int(190463 * (180.0 + lon))
    cs = b"  "
    t = 0x00                                      # other source, no fix flag, compressed origin
    if course is not None and speed_kn is not None:
        c = int(round(course / 4.0)) % 90
        s = int(round(math.log(speed_kn + 1.0) / math.log(1.08)))
        cs = bytes([33 + c, 33 + s])
        t = 0x20 | 0x18 | 0x00                    # current fix, RMC source
    elif alt_ft is not None:
        n = int(round(math.log(alt_ft) / math.log(1.002)))
        cs = bytes([33 + n // 91, 33 + n % 91])
        t = 0x20 | 0x10                           # current fix, GGA source: cs is altitude
    elif range_mi is not None:
        s = int(round(math.log(range_mi / 2.0) / math.log(1.08)))
        cs = bytes([ord("{"), 33 + s])
        t = 0x20
    elif no_cs:
        cs = b" s"
        t = 0x00
    if t_byte is not None:
        t = t_byte
    return table.encode("ascii") + base91(y, 4) + base91(x, 4) + code.encode("ascii") + cs + bytes([33 + t])


# ------------------------------------------------------------------ Mic-E

MICE_STD = ["Off Duty", "En Route", "In Service", "Returning", "Committed", "Special", "Priority", "Emergency"]
# message index i: bits A B C = 1 1 1 (M0) ... 0 0 0 (Emergency) = 7 - i


def mice_destination(lat, msg_index, *, custom=False, west=False, lon_offset=False):
    """The 6 destination characters: latitude digits + message bits (1..3), N/S, +100 degree, W/E."""
    a = abs(lat)
    deg = int(a)
    minutes = int(round((a - deg) * 60 * 100))
    digits = [deg // 10, deg % 10, minutes // 1000, (minutes // 100) % 10, (minutes // 10) % 10, minutes % 10]
    bits = [(7 - msg_index) >> 2 & 1, (7 - msg_index) >> 1 & 1, (7 - msg_index) & 1]
    flags = bits + [1 if lat >= 0 else 0, 1 if lon_offset else 0, 1 if west else 0]
    out = []
    for i, (d, f) in enumerate(zip(digits, flags)):
        if not f:
            out.append(ord("0") + d)
        elif i < 3 and custom:
            out.append(ord("A") + d)
        else:
            out.append(ord("P") + d)
    return bytes(out).decode("ascii")


def mice_info(lon, speed_kn, course, table="/", code=">", *, dti="`", rest=b""):
    """Mic-E information field: DTI, three longitude bytes, three speed / course bytes,
    symbol code, symbol table, then `rest` (status text, altitude...)."""
    a = abs(lon)
    deg = int(a)
    minutes = int(round((a - deg) * 60 * 100))
    mm, hh = minutes // 100, minutes % 100
    if deg <= 9:
        d = deg + 118
    elif deg <= 99:
        d = deg + 28
    elif deg <= 109:
        d = deg + 8
    else:
        d = deg - 72
    m = mm + 60 if mm < 10 else mm
    sp = int(speed_kn) // 10
    dc = (int(speed_kn) % 10) * 10 + int(course) // 100
    se = int(course) % 100
    body = bytes([d, m + 28, hh + 28, sp + 28, dc + 28, se + 28])
    return dti.encode("ascii") + body + code.encode("ascii") + table.encode("ascii") + rest


def mice_lon_offset(lon):
    """True when the destination must carry the +100 degree flag."""
    deg = int(abs(lon))
    return deg <= 9 or deg >= 100


def mice_altitude(meters):
    """The 3 base-91 characters + '}' of an altitude in the comment (meters above -10000)."""
    return base91(int(meters) + 10000, 3) + b"}"


# ------------------------------------------------------------------ the case table

SRC = "N0CALL-9"
DEST = "APZ000"          # experimental tocall: no product lookup noise
VIA = "WIDE1-1"

POS = "4903.50N/07201.75W"          # the specification's own example position
POS_C = b"/5L!!<*e7>7P["            # the specification's compressed example (course / speed)
SEA = "3012.34S/14000.10W"          # open Pacific


def _b(text):
    return text.encode("latin-1")


def _mice(lat, lon, speed, course, msg, *, custom=False, table="/", code=">", dti="`", rest=b""):
    return (mice_destination(lat, msg, custom=custom, west=lon < 0, lon_offset=mice_lon_offset(lon)),
            mice_info(lon, speed, course, table, code, dti=dti, rest=rest))


# name -> (lat, lon, speed knots, course, message index, keyword arguments), note. The unit tests
# compare these numbers with what the decoder printed.
MICE_CASES = [
    ("mice-offduty", (33.4273, -112.129, 0, 0, 0, {}), "message M0 Off Duty, stopped, lon 100..109 offset (spec example position)"),
    ("mice-inservice-moving", (33.4273, -112.129, 52, 251, 2, {}), "M2 In Service, 52 knots, course 251"),
    ("mice-emergency", (-5.1687, 175.501, 0, 0, 7, {}), "Emergency, south latitude, east longitude over 110"),
    ("mice-custom-c1", (51.2057, 3.0835, 10, 90, 1, {"custom": True}), "custom message C1, longitude under 10 degrees"),
    ("mice-old", (10.0, 105.9998, 5, 359, 3, {"dti": "'"}), "old-format data type indicator, M3 Returning"),
    ("mice-fast", (0.0017, -179.9998, 123, 359, 4, {}), "123 knots, course 359, lon 179, M4 Committed"),
]


def cases():
    """[(name, group, dest, info bytes, note)]. dest is DEST except for Mic-E."""
    c = []

    def add(name, group, info, note, dest=DEST):
        c.append((name, group, dest, info if isinstance(info, bytes) else _b(info), note))

    # messages, acks, rejects, bulletins, announcements (chapter 14)
    add("msg-number", "message", ":N0TEST-1 :Hello{123", "message with a message number")
    add("msg-nonumber", "message", ":N0TEST   :Plain text, no message id", "no number: no ack expected")
    add("msg-max", "message", ":N0TEST-15:" + "x" * 67 + "{1", "67 text characters (the specification's limit), SSID 15 addressee, number 1")
    add("msg-brace-in-text", "message", ":N0TEST-1 :text {with} braces inside{77",
        "a brace inside the text (not allowed by the specification): which brace starts the number is up to the decoder")
    add("msg-ack", "ack", ":N0TEST-1 :ack123", "ack of message 123")
    add("msg-ack-alnum", "ack", ":N0TEST   :ackA5", "ack with an alphanumeric number")
    add("msg-rej", "reject", ":N0TEST-1 :rej123", "reject of message 123")
    add("msg-rej-alnum", "reject", ":N0TEST   :rejZ9", "reject with an alphanumeric number")
    add("bln-number", "bulletin", ":BLN3     :Road closed until Friday", "bulletin number 3, no group")
    add("bln-group", "bulletin", ":BLN1WX   :Weather group bulletin one", "bulletin 1 of group WX")
    add("bln-announce", "announcement", ":BLNA     :General announcement", "announcement A")
    add("bln-announce-group", "announcement", ":BLNZGRP  :Group announcement Z", "announcement Z of group GRP")

    # plain positions (chapter 8)
    add("pos-plain", "position", "!" + POS + "-Test 001234", "no timestamp, no messaging, comment")
    add("pos-messaging", "position", "=" + SEA + ">Pacific car", "messaging capable, southern western hemisphere")
    add("pos-nocomment", "position", "!0000.00N/00000.00E>", "no comment at all, position 0 0")
    add("pos-overlay", "position", "!4903.50N107201.75W&", "alternate table overlay character 1")
    add("pos-south-east", "position", "!3012.34S/14000.10E-", "south and east")
    add("pos-altitude", "position", "!" + POS + ">/A=001234", "altitude in the comment")

    # ambiguity (chapter 6)
    add("amb-1", "ambiguity", "!4903.5 N/07201.7 W-ambiguity 1", "one digit blanked")
    add("amb-2", "ambiguity", "!4903.  N/07201.  W-ambiguity 2", "two digits blanked")
    add("amb-3", "ambiguity", "!490 .  N/0720 .  W-ambiguity 3", "three digits blanked (the minutes units)")
    add("amb-4", "ambiguity", "!49  .  N/072  .  W-ambiguity 4", "four digits blanked")

    # timestamps (chapter 6)
    add("ts-dhm-z", "timestamp", "/092345z" + POS + ">", "day, hour, minute, zulu; no messaging")
    add("ts-dhm-local", "timestamp", "@092345/" + POS + ">", "day, hour, minute, local; messaging")
    add("ts-hms", "timestamp", "/234517h" + POS + ">", "hour, minute, second, zulu")
    add("ts-mdhm", "timestamp", "_10090556c220s004g005t077r001p002P003h50b09900", "month, day, hour, minute in a positionless weather report")

    # compressed positions (chapter 9)
    add("comp-cs", "compressed", b"!" + POS_C, "the specification's example: course and speed")
    add("comp-nocs", "compressed", b"=" + compressed(49.5, -72.75, no_cs=True), "compressed position with the cs pair blank")
    add("comp-alt", "compressed", b"!" + compressed(49.5, -72.75, alt_ft=1000), "cs = altitude, T byte says GGA")
    add("comp-range", "compressed", b"!" + compressed(49.5, -72.75, "\\", "r", range_mi=20), "cs = pre-calculated radio range, alternate table")
    add("comp-ts", "compressed", b"@092345z" + POS_C, "timestamp and messaging")
    add("comp-custom", "compressed", b"!" + compressed(-30.2056, -140.0017, "/", "k", course=180, speed_kn=45), "southern western position, own course and speed")

    # Mic-E (chapter 10)
    for name, (lat, lon, spd, crs, msg, kw), note in MICE_CASES:
        dest, info = _mice(lat, lon, spd, crs, msg, **kw)
        add(name, "mic-e", info, note, dest=dest)
    dest, info = _mice(12.0583, 45.2, 0, 0, 5, rest=b"]Status text")
    add("mice-status", "mic-e", info, "M5 Special, status text after the position (a ']' type character)", dest=dest)
    dest, info = _mice(-12.0583, -45.2, 0, 0, 6, rest=b"]" + mice_altitude(250) + b"alt 250 m")
    add("mice-altitude", "mic-e", info, "M6 Priority, altitude in the comment as 3 base-91 characters and '}'", dest=dest)

    # PHG / RNG / DFS / course-speed (chapter 7)
    add("phg-5132", "extension", "!" + POS + "#PHG5132", "PHG power 5, height 1, gain 3, directivity 2")
    add("phg-comment", "extension", "!" + POS + "#PHG2360/digi comment", "PHG followed by a comment")
    add("phg-zero", "extension", "=0000.00N/00000.00E#PHG0000", "PHG with power 0")
    add("rng-0050", "extension", "!" + POS + "#RNG0050 range", "pre-calculated radio range 50 miles")
    add("dfs-2360", "extension", "!" + POS + "\\DFS2360", "direction finding strength 2, height 3, gain 6, omni")
    add("cse-spd", "extension", "!" + POS + ">088/036", "course 88, speed 36")
    add("cse-spd-zero", "extension", "!" + POS + ">000/000", "course 0, speed 0")
    add("cse-spd-high", "extension", "!" + POS + ">270/120", "course 270, speed 120")

    # weather (chapter 12)
    add("wx-positionless", "weather", "_10090556c220s004g005t077r000p000P000h50b09900wRSW", "positionless: the specification's example")
    add("wx-positionless-missing", "weather", "_10090556c...s...g...t077r...p...P...h..b.....", "positionless, missing fields as dots")
    add("wx-pos", "weather", "!" + POS + "_220/004g005t077r000p000P000h50b09900wRSW", "position + weather: symbol _ and wind direction / speed as data extension")
    add("wx-pos-negative", "weather", "=" + SEA + "_090/010g015t-05r000p000P000h85b10132", "negative temperature, humidity 85")
    add("wx-pos-ts", "weather", "@092345z" + POS + "_220/004g005t077r000p000P000h50b09900L123", "timestamp, luminosity")

    # DAO (APRS 1.2 addendum, widely used)
    add("dao-human", "dao", "!" + POS + "-Test !W83!", "human readable DAO, datum W (WGS84)")
    add("dao-base91", "dao", "!" + POS + "-Test !wfe!", "base-91 DAO, datum w (WGS84)")
    add("dao-base91-bare", "dao", "!" + POS + "-!wfe!", "a base-91 DAO as the whole comment, nothing in front of it")

    # status (chapter 16)
    add("status-plain", "status", ">Net control tonight", "plain status")
    add("status-ts", "status", ">092345zNet at twenty hundred", "status with a timestamp")
    add("status-grid6", "status", ">JJ00AA/- Grid status", "6 character Maidenhead locator, symbol, space, text")
    add("status-grid4", "status", ">JJ00/- Grid 4", "4 character Maidenhead locator")

    # objects (chapter 11)
    add("obj-live", "object", ";LEADER   *092345z" + POS + ">088/036", "live object, course and speed")
    add("obj-kill", "object", ";LEADER   _092345z" + POS + ">088/036", "killed object")
    add("obj-compressed", "object", b";COMPOBJ  *092345z" + POS_C, "live object, compressed position")
    add("obj-hms", "object", ";TESTOBJ  *234517h" + POS + "[", "object with an HMS timestamp")
    add("obj-phg", "object", ";REPEATER *092345z" + POS + "#PHG5132", "object with PHG")

    # items (chapter 11)
    add("item-live", "item", ")AID #2!" + POS + "A", "live item, name of 5 characters")
    add("item-kill", "item", ")AID #2_" + POS + "A", "killed item")
    add("item-compressed", "item", b")AID #3!" + POS_C, "item with a compressed position")

    # third party (chapter 17), one level
    add("tp-position", "third-party", "}N0TEST-1>APZ000,TCPIP,N0CALL*:!" + POS + "-Third party", "third-party position")
    add("tp-message", "third-party", "}N0TEST-1>APZ000,TCPIP*::N0DEST-2 :Hello via gateway{12", "third-party message")
    add("tp-status", "third-party", "}N0TEST>APZ000,TCPIP*:>status of someone else", "third-party status")

    # things that are not valid: what a decoder says about them
    add("neg-unknown-dti", "negative", "~unknown data type", "no such data type indicator")
    add("neg-short-pos", "negative", "!4903.50N/0720", "position cut off")
    add("neg-bad-lat", "negative", "!4903.50X/07201.75W-", "latitude without N or S")
    add("neg-short-msg", "negative", ":N0TEST", "message shorter than an addressee")
    add("neg-empty", "negative", b"", "empty information field")
    return c
