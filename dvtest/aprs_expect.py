# SPDX-License-Identifier: MIT
"""The generator's EXACT expected values for every APRS case and the compare policy
(HWFREE1 item 3, DP9; docs/direwolf-cases.md, "Compare policy").

`dvtest.aprs_cases` builds the information strings; this module says what each string MEANS, from
the numbers and the APRS 1.0.1 formats the strings were built from, never from Dire Wolf's output.
`expected(name)` returns a dict per case (the 17 existing frames and the 76 generated strings),
written into the records of vectors/direwolf/aprs_decode.json as `expect`.

Compare policy (stated by the DV-Router lead, restated here and in the vector file header):

* EXACT, as encoded: source / destination / path, data type indicator, symbol table and code,
  message addressee / text / id, ack and reject ids, status text, object and item name and
  live / killed flag, course and speed as encoded integers, altitude feet as encoded, PHG / RNG /
  DFS fields, weather fields.
* Positions: latitude and longitude are compared with the GENERATOR's exact value (an integer in
  the encoded resolution, see `axis` below, so no float is involved) and with `decode_aprs` within
  half of the precision it prints (0.0001 minute, so 0.00005 minute).
* Timestamps and ambiguity are compared only with the generator: decode_aprs consumes the
  timestamp and does not print the ambiguity level.
* `dao-base91-bare`: the generator value (with the DAO applied) is the authority; decode_aprs
  leaves a DAO that is the whole comment unapplied, so its printed position is the one WITHOUT the
  DAO (recorded in `decode_aprs`, never compared as an error).

Shape of an `axis` (latitude or longitude): {"int": n, "per_degree": d}: the signed value is exactly
n / d degrees. Plain positions are hundredths of a minute (d = 6000), human DAO tenths of that
(60000), base-91 DAO 91 times finer (546000), compressed positions 380926 per degree of latitude
and 190463 per degree of longitude, Mic-E hundredths of a minute.
"""
import math
import re
from fractions import Fraction

from . import aprs_cases as ac

SCHEMA = "dvt-aprs-expect/1"

POLICY = (
    "EXACT as encoded: source/dest/path, data type, symbol table and code, message addressee/text/id, "
    "ack/rej ids, status text, object/item name and live/killed flag, course and speed as encoded "
    "integers, altitude feet as encoded, PHG/RNG/DFS fields, weather fields. Positions: lat/lon equal "
    "the generator's exact integer (expect.position) and within half of decode_aprs's printed "
    "precision (0.00005 minute) of decode_aprs. Timestamps and ambiguity: generator only. "
    "dao-base91-bare: generator value is the authority, decode_aprs does not apply the DAO."
)

HALF_PRINTED_MINUTE = Fraction(1, 20000)           # half of 0.0001 minute
HALF_PRINTED_DEGREE = HALF_PRINTED_MINUTE / 60

# data type text printed by decode_aprs per generator kind (a map, not a fact about the output)
DW_TYPE = {"message": "APRS Message", "ack": "ACKnowledged message", "reject": "REJected message",
           "bulletin": "Bulletin", "announcement": "Bulletin", "position": "Position",
           "mic-e": "MIC-E", "weather-position": "Weather Report", "weather-positionless": "Positionless Weather Report",
           "status": "Status Report", "object": "Object", "item": "Item"}
PHG_DIRECTIVITY = {0: "omni", 1: "NE", 2: "E", 3: "SE", 4: "S", 5: "SW", 6: "W", 7: "NW", 8: "N"}
KNOT_KMH = Fraction(1852, 1000)
KNOT_MPH = Fraction(115078, 100000)


# ------------------------------------------------------------------ small builders

def axis(n, per_degree):
    return {"int": int(n), "per_degree": int(per_degree)}


def axis_degrees(a):
    """Exact signed degrees of an axis as a Fraction."""
    return Fraction(a["int"], a["per_degree"])


def _plain_axis(text, hemi_neg, deg_digits):
    """'4903.50N' or '07201.75W' (blanks count as 0) -> (integer hundredths of a minute, blanks)."""
    body, hemi = text[:-1], text[-1]
    blanks = body.count(" ")
    digits = body.replace(" ", "0")
    deg = int(digits[:deg_digits])
    minutes = int(digits[deg_digits:deg_digits + 2])
    hundredths = int(digits[deg_digits + 3:deg_digits + 5])
    n = deg * 6000 + minutes * 100 + hundredths
    return (-n if hemi == hemi_neg else n), blanks


def plain(pos, table="/", code=">"):
    """A plain 'DDMM.mmN/DDDMM.mmE' position text (the table character is the one between the
    halves; `pos` may contain it, `table` overrides) -> position dict, ambiguity from the blanks."""
    lat_t = pos[:8]
    lon_t = pos[9:18]
    lat, blanks = _plain_axis(lat_t, "S", 2)
    lon, blanks_lon = _plain_axis(lon_t, "W", 3)
    assert blanks == blanks_lon
    return {"lat": axis(lat, 6000), "lon": axis(lon, 6000), "ambiguity": blanks, "symbol_table": table, "symbol_code": code}


def plain_text(position):
    """The 'DDMM.mmN' + table + 'DDDMM.mmE' + code text of a plain position dict (the inverse of plain(),
    blanks restored from the ambiguity level): used by the unit tests to compare with the real string."""
    def one(a, deg_digits, pos_h, neg_h):
        n = abs(a["int"])
        text = "%0*d%02d.%02d%s" % (deg_digits, n // 6000, (n // 100) % 60, n % 100, pos_h if a["int"] >= 0 else neg_h)
        chars = list(text[:-1])
        digit_at = [i for i, ch in enumerate(chars) if ch.isdigit()]
        for i in list(reversed(digit_at))[:position["ambiguity"]]:
            chars[i] = " "
        return "".join(chars) + text[-1]
    return one(position["lat"], 2, "N", "S") + position["symbol_table"] + one(position["lon"], 3, "E", "W") + position["symbol_code"]


def _b91(chars):
    value = 0
    for ch in chars:
        value = value * 91 + (ch - 33)
    return value


def compressed_position(body, table=None):
    """Exact position of a 13 byte compressed body (table, YYYY, XXXX, code, cs, T): from the base-91
    values, no float. Resolution 1/380926 degree of latitude and 1/190463 degree of longitude."""
    y, x = _b91(body[1:5]), _b91(body[5:9])
    return {"lat": axis(90 * 380926 - y, 380926), "lon": axis(x - 180 * 190463, 190463), "ambiguity": 0,
            "symbol_table": chr(body[0]), "symbol_code": chr(body[9])}


def compressed_cs(body):
    """What the cs pair of a compressed body encodes, as integers: course code (4 degrees each),
    speed code (1.08 ** code - 1 knots), altitude code (1.002 ** code feet) or range code
    (2 * 1.08 ** code miles); the T byte's GGA bit says altitude."""
    c, s, t = body[10] - 33, body[11] - 33, body[12] - 33
    if body[10] == 0x20:
        return {}
    if body[10] == ord("{"):
        return {"range_code": s}
    if (t >> 3) & 3 == 2:                       # GGA source: cs is the altitude
        return {"alt_code": c * 91 + s}
    return {"course_deg": c * 4, "speed_code": s}


def speed_knots_from_code(code):
    return 1.08 ** code - 1.0


def timestamp(kind, **kw):
    out = {"kind": kind}
    out.update(kw)
    return out


def dhm(day, hour, minute, zone):
    return timestamp("dhm", day=day, hour=hour, minute=minute, zone=zone,
                     text="%02d%02d%02d%s" % (day, hour, minute, zone))


def hms(hour, minute, second):
    return timestamp("hms", hour=hour, minute=minute, second=second, zone="h", text="%02d%02d%02dh" % (hour, minute, second))


def mdhm(month, day, hour, minute):
    return timestamp("mdhm", month=month, day=day, hour=hour, minute=minute, text="%02d%02d%02d%02d" % (month, day, hour, minute))


def phg(p, h, g, d):
    """PHGphgd digits -> encoded digits and what they mean (power p*p watts, height 10 * 2^h feet,
    gain g dBi, directivity d * 45 degrees, 0 omni)."""
    return {"p": p, "h": h, "g": g, "d": d, "power_w": p * p, "height_ft": 10 * (1 << h), "gain_dbi": g,
            "directivity": PHG_DIRECTIVITY[d]}


# ------------------------------------------------------------------ the table

POS = plain("4903.50N/07201.75W")
SEA = plain("3012.34S/14000.10W")
ZERO = plain("0000.00N/00000.00E")
SYM_CAR = ("/", ">")


def _pos(base, table=None, code=None):
    out = dict(base)
    if table is not None:
        out["symbol_table"] = table
    if code is not None:
        out["symbol_code"] = code
    return out


def _centi(value):
    """Mic-E: a signed degree value -> integer hundredths of a minute the way the encoder rounds."""
    a = abs(value)
    deg = int(a)
    minutes = int(round((a - deg) * 60 * 100))
    n = deg * 6000 + minutes
    return -n if value < 0 else n


def _mice_expect(lat, lon, speed, course, msg, custom, table="/", code=">", dti="`", **extra):
    code_name = ("C%d" % msg) if custom else ("M%d" % msg if msg < 7 else "Emergency")
    out = {"kind": "mic-e", "data_type": dti,
           "position": {"lat": axis(_centi(lat), 6000), "lon": axis(_centi(lon), 6000), "ambiguity": 0,
                        "symbol_table": table, "symbol_code": code},
           "speed_kn": speed, "course_deg": course, "message_code": code_name, "message_index": msg, "custom": custom}
    out.update(extra)
    return out


def _generated():
    e = {}
    # messages (chapter 14)
    e["msg-number"] = {"kind": "message", "addressee": "N0TEST-1", "text": "Hello", "message_id": "123"}
    e["msg-nonumber"] = {"kind": "message", "addressee": "N0TEST", "text": "Plain text, no message id", "message_id": None}
    e["msg-max"] = {"kind": "message", "addressee": "N0TEST-15", "text": "x" * 67, "message_id": "1"}
    e["msg-brace-in-text"] = {"kind": "message", "addressee": "N0TEST-1", "text": "text {with} braces inside", "message_id": "77",
                              "decode_aprs": {"differs": ["message_id", "text"], "message_id": "with} brace", "text": "text "}}
    e["msg-ack"] = {"kind": "ack", "addressee": "N0TEST-1", "ack_id": "123"}
    e["msg-ack-alnum"] = {"kind": "ack", "addressee": "N0TEST", "ack_id": "A5"}
    e["msg-rej"] = {"kind": "reject", "addressee": "N0TEST-1", "ack_id": "123"}
    e["msg-rej-alnum"] = {"kind": "reject", "addressee": "N0TEST", "ack_id": "Z9"}
    e["bln-number"] = {"kind": "bulletin", "addressee": "BLN3", "bulletin_id": "3", "bulletin_group": None, "text": "Road closed until Friday"}
    e["bln-group"] = {"kind": "bulletin", "addressee": "BLN1WX", "bulletin_id": "1", "bulletin_group": "WX", "text": "Weather group bulletin one"}
    e["bln-announce"] = {"kind": "announcement", "addressee": "BLNA", "bulletin_id": "A", "bulletin_group": None, "text": "General announcement"}
    e["bln-announce-group"] = {"kind": "announcement", "addressee": "BLNZGRP", "bulletin_id": "Z", "bulletin_group": "GRP",
                               "text": "Group announcement Z",
                               "decode_aprs": {"differs": ["bulletin_id"], "bulletin_id": "ZGRP"}}
    # plain positions (chapter 8)
    e["pos-plain"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "-"),
                      "comment": "Test 001234"}
    e["pos-messaging"] = {"kind": "position", "data_type": "=", "messaging": True, "timestamp": None, "position": _pos(SEA, "/", ">"),
                          "comment": "Pacific car"}
    e["pos-nocomment"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(ZERO, "/", ">"),
                          "comment": ""}
    e["pos-overlay"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "1", "&"),
                        "comment": ""}
    e["pos-south-east"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None,
                           "position": _pos(plain("3012.34S/14000.10E"), "/", "-"), "comment": ""}
    e["pos-altitude"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", ">"),
                         "altitude_ft": 1234, "comment": "/A=001234"}
    # ambiguity (chapter 6): blanked digits count as zero in the exact value
    for n, text in ((1, "4903.5 N/07201.7 W"), (2, "4903.  N/07201.  W"), (3, "490 .  N/0720 .  W"), (4, "49  .  N/072  .  W")):
        e["amb-%d" % n] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None,
                           "position": _pos(plain(text), "/", "-"), "comment": "ambiguity %d" % n}
    # timestamps (chapter 6)
    e["ts-dhm-z"] = {"kind": "position", "data_type": "/", "messaging": False, "timestamp": dhm(9, 23, 45, "z"), "position": _pos(POS, *SYM_CAR), "comment": ""}
    e["ts-dhm-local"] = {"kind": "position", "data_type": "@", "messaging": True, "timestamp": dhm(9, 23, 45, "/"), "position": _pos(POS, *SYM_CAR), "comment": ""}
    e["ts-hms"] = {"kind": "position", "data_type": "/", "messaging": False, "timestamp": hms(23, 45, 17), "position": _pos(POS, *SYM_CAR), "comment": ""}
    e["ts-mdhm"] = {"kind": "weather-positionless", "data_type": "_", "timestamp": mdhm(10, 9, 5, 56),
                    "weather": {"wind_dir_deg": 220, "wind_speed_mph": 4, "gust_mph": 5, "temp_f": 77, "rain_1h_hundredths_in": 1,
                                "rain_24h_hundredths_in": 2, "rain_midnight_hundredths_in": 3, "humidity_pct": 50,
                                "barometer_tenths_mbar": 9900, "luminosity_wm2": None, "software_tag": None}}
    # compressed (chapter 9)
    pos_c = ac.POS_C
    cs_c = compressed_cs(pos_c)
    e["comp-cs"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": compressed_position(pos_c),
                    "compressed": cs_c, "comment": ""}
    nocs = ac.compressed(49.5, -72.75, no_cs=True)
    e["comp-nocs"] = {"kind": "position", "data_type": "=", "messaging": True, "timestamp": None, "position": compressed_position(nocs),
                      "compressed": compressed_cs(nocs), "comment": ""}
    alt = ac.compressed(49.5, -72.75, alt_ft=1000)
    alt_cs = compressed_cs(alt)
    e["comp-alt"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": compressed_position(alt),
                     "compressed": alt_cs, "altitude_ft_requested": 1000, "comment": ""}
    rng = ac.compressed(49.5, -72.75, "\\", "r", range_mi=20)
    e["comp-range"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": compressed_position(rng),
                       "compressed": compressed_cs(rng), "range_mi_requested": 20, "comment": ""}
    e["comp-ts"] = {"kind": "position", "data_type": "@", "messaging": True, "timestamp": dhm(9, 23, 45, "z"),
                    "position": compressed_position(pos_c), "compressed": cs_c, "comment": ""}
    cust = ac.compressed(-30.2056, -140.0017, "/", "k", course=180, speed_kn=45)
    e["comp-custom"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": compressed_position(cust),
                        "compressed": compressed_cs(cust), "speed_kn_requested": 45, "comment": ""}
    # Mic-E (chapter 10)
    for name, (lat, lon, spd, crs, msg, kw), _note in ac.MICE_CASES:
        e[name] = _mice_expect(lat, lon, spd, crs, msg, kw.get("custom", False), dti=kw.get("dti", "`"))
    e["mice-status"] = _mice_expect(12.0583, 45.2, 0, 0, 5, False, comment="Status text", type_code="]")
    e["mice-altitude"] = _mice_expect(-12.0583, -45.2, 0, 0, 6, False, comment="alt 250 m", type_code="]", altitude_m=250)
    # extensions (chapter 7)
    e["phg-5132"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "#"),
                     "phg": phg(5, 1, 3, 2), "comment": ""}
    e["phg-comment"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "#"),
                        "phg": phg(2, 3, 6, 0), "comment": "/digi comment"}
    e["phg-zero"] = {"kind": "position", "data_type": "=", "messaging": True, "timestamp": None, "position": _pos(ZERO, "/", "#"),
                     "phg": phg(0, 0, 0, 0), "comment": ""}
    e["rng-0050"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "#"),
                     "range_mi": 50, "comment": " range"}
    e["dfs-2360"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "\\"),
                     "dfs": {"s": 2, "h": 3, "g": 6, "d": 0, "height_ft": 80, "gain_dbi": 6, "directivity": "omni"}, "comment": ""}
    for name, course, speed in (("cse-spd", 88, 36), ("cse-spd-zero", 0, 0), ("cse-spd-high", 270, 120)):
        e[name] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, *SYM_CAR),
                   "course_deg": course, "speed_kn": speed, "comment": ""}
    # weather (chapter 12)
    wx_full = {"wind_dir_deg": 220, "wind_speed_mph": 4, "gust_mph": 5, "temp_f": 77, "rain_1h_hundredths_in": 0,
               "rain_24h_hundredths_in": 0, "rain_midnight_hundredths_in": 0, "humidity_pct": 50,
               "barometer_tenths_mbar": 9900, "luminosity_wm2": None, "software_tag": "wRSW"}
    e["wx-positionless"] = {"kind": "weather-positionless", "data_type": "_", "timestamp": mdhm(10, 9, 5, 56), "weather": dict(wx_full)}
    e["wx-positionless-missing"] = {"kind": "weather-positionless", "data_type": "_", "timestamp": mdhm(10, 9, 5, 56),
                                    "weather": {"wind_dir_deg": None, "wind_speed_mph": None, "gust_mph": None, "temp_f": 77,
                                                "rain_1h_hundredths_in": None, "rain_24h_hundredths_in": None,
                                                "rain_midnight_hundredths_in": None, "humidity_pct": None,
                                                "barometer_tenths_mbar": None, "luminosity_wm2": None, "software_tag": None}}
    wx_pos_note = {"differs": ["wind_speed_mph"], "reason": "decode_aprs reads the speed of the _ccc/sss extension as knots (4 -> 4.6 mph)"}
    e["wx-pos"] = {"kind": "weather-position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "_"),
                   "weather": dict(wx_full), "decode_aprs": wx_pos_note}
    wx_neg = dict(wx_full, wind_dir_deg=90, wind_speed_mph=10, gust_mph=15, temp_f=-5, humidity_pct=85, barometer_tenths_mbar=10132,
                  software_tag=None)
    e["wx-pos-negative"] = {"kind": "weather-position", "data_type": "=", "messaging": True, "timestamp": None, "position": _pos(SEA, "/", "_"),
                            "weather": wx_neg, "decode_aprs": wx_pos_note}
    wx_ts = dict(wx_full, luminosity_wm2=123, software_tag=None)
    e["wx-pos-ts"] = {"kind": "weather-position", "data_type": "@", "messaging": True, "timestamp": dhm(9, 23, 45, "z"),
                      "position": _pos(POS, "/", "_"), "weather": wx_ts, "decode_aprs": wx_pos_note}
    # DAO
    e["dao-human"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None,
                      "position": {"lat": axis(294350 * 10 + 8, 60000), "lon": axis(-(432175 * 10 + 3), 60000), "ambiguity": 0,
                                   "symbol_table": "/", "symbol_code": "-"},
                      "dao": {"datum": "W", "lat_digit": 8, "lon_digit": 3, "encoding": "human"}, "comment": "Test"}
    dao_b91 = {"lat": axis(294350 * 91 + 69, 546000), "lon": axis(-(432175 * 91 + 68), 546000), "ambiguity": 0,
               "symbol_table": "/", "symbol_code": "-"}
    e["dao-base91"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": dao_b91,
                       "dao": {"datum": "w", "lat_char": "f", "lon_char": "e", "encoding": "base91"}, "comment": "Test"}
    e["dao-base91-bare"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": dao_b91,
                            "dao": {"datum": "w", "lat_char": "f", "lon_char": "e", "encoding": "base91"}, "comment": "",
                            "decode_aprs": {"applies_dao": False, "differs": ["position", "comment"], "position": _pos(POS, "/", "-"),
                                            "comment": "!wfe!"}}
    # status (chapter 16)
    e["status-plain"] = {"kind": "status", "data_type": ">", "timestamp": None, "text": "Net control tonight"}
    e["status-ts"] = {"kind": "status", "data_type": ">", "timestamp": dhm(9, 23, 45, "z"), "text": "Net at twenty hundred"}
    e["status-grid6"] = {"kind": "status", "data_type": ">", "timestamp": None, "grid": "JJ00AA", "symbol_table": "/", "symbol_code": "-",
                         "position": {"lat": axis(125, 6000), "lon": axis(250, 6000), "ambiguity": 0, "symbol_table": "/", "symbol_code": "-"},
                         "text": "Grid status"}
    e["status-grid4"] = {"kind": "status", "data_type": ">", "timestamp": None, "grid": "JJ00", "symbol_table": "/", "symbol_code": "-",
                         "position": {"lat": axis(3000, 6000), "lon": axis(6000, 6000), "ambiguity": 0, "symbol_table": "/", "symbol_code": "-"},
                         "text": "Grid 4"}
    # objects and items (chapter 11)
    e["obj-live"] = {"kind": "object", "data_type": ";", "name": "LEADER", "live": True, "timestamp": dhm(9, 23, 45, "z"),
                     "position": _pos(POS, *SYM_CAR), "course_deg": 88, "speed_kn": 36, "comment": ""}
    e["obj-kill"] = dict(e["obj-live"], live=False)
    e["obj-compressed"] = {"kind": "object", "data_type": ";", "name": "COMPOBJ", "live": True, "timestamp": dhm(9, 23, 45, "z"),
                           "position": compressed_position(pos_c), "compressed": cs_c, "comment": ""}
    e["obj-hms"] = {"kind": "object", "data_type": ";", "name": "TESTOBJ", "live": True, "timestamp": hms(23, 45, 17),
                    "position": _pos(POS, "/", "["), "comment": ""}
    e["obj-phg"] = {"kind": "object", "data_type": ";", "name": "REPEATER", "live": True, "timestamp": dhm(9, 23, 45, "z"),
                    "position": _pos(POS, "/", "#"), "phg": phg(5, 1, 3, 2), "comment": ""}
    e["item-live"] = {"kind": "item", "data_type": ")", "name": "AID #2", "live": True, "position": _pos(POS, "/", "A"), "comment": ""}
    e["item-kill"] = dict(e["item-live"], live=False)
    e["item-compressed"] = {"kind": "item", "data_type": ")", "name": "AID #3", "live": True, "position": compressed_position(pos_c),
                            "compressed": cs_c, "comment": ""}
    # third party (chapter 17): the outer frame is the case header, the inner packet is `inner`
    e["tp-position"] = {"kind": "third-party", "data_type": "}", "inner": {
        "source": "N0TEST-1", "dest": "APZ000", "path": ["TCPIP", "N0CALL*"],
        "kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "-"), "comment": "Third party"}}
    e["tp-message"] = {"kind": "third-party", "data_type": "}", "inner": {
        "source": "N0TEST-1", "dest": "APZ000", "path": ["TCPIP*"],
        "kind": "message", "addressee": "N0DEST-2", "text": "Hello via gateway", "message_id": "12"}}
    e["tp-status"] = {"kind": "third-party", "data_type": "}", "inner": {
        "source": "N0TEST", "dest": "APZ000", "path": ["TCPIP*"],
        "kind": "status", "data_type": ">", "timestamp": None, "text": "status of someone else"}}
    # invalid input
    e["neg-unknown-dti"] = {"kind": "unknown-dti", "data_type": "~"}
    e["neg-short-pos"] = {"kind": "invalid", "data_type": "!", "reason": "longitude cut off", "partial": {"lat": axis(294350, 6000), "lon": None}}
    e["neg-bad-lat"] = {"kind": "invalid", "data_type": "!", "reason": "latitude hemisphere letter is X", "partial": {"lat": None, "lon": axis(-432175, 6000)}}
    e["neg-short-msg"] = {"kind": "invalid", "data_type": ":", "reason": "shorter than the 9 character addressee field"}
    e["neg-empty"] = {"kind": "empty"}
    return e


def _existing():
    """The 17 frames of vectors/direwolf/rx_frames.json: positions, status and message decode, the rest
    starts with a character that is no APRS data type indicator."""
    e = {}
    e["ex-pos-digi"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(ZERO, "/", ">"), "comment": "test"}
    e["ex-status-nodigi"] = {"kind": "status", "data_type": ">", "timestamp": None, "text": "status"}
    e["ex-message"] = {"kind": "message", "addressee": "N0TEST-1", "text": "Hello", "message_id": "123"}
    for name, ch in (("ex-digi-used-first", "h"), ("ex-digi-used-last", "h"), ("ex-digi-8", "x"), ("ex-calls-short", "x"),
                     ("ex-calls-long", "s"), ("ex-info-340", "x"), ("ex-info-tab-cr", "a")):
        e[name] = {"kind": "unknown-dti", "data_type": ch}
    e["ex-info-empty"] = {"kind": "empty"}
    e["ex-info-fend-fesc"] = {"kind": "unknown-dti", "data_type": "m", "note": "decode_aprs reads 'm' as Morse code data"}
    e["ex-info-ones"] = {"kind": "unknown-dti", "data_type": "0xff"}
    e["ex-aprs-position"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(POS, "/", "-"),
                             "comment": "Test 001234"}
    e["ex-lora-status"] = {"kind": "status", "data_type": ">", "timestamp": None, "text": "Test status"}
    e["ex-lora-position"] = {"kind": "position", "data_type": "!", "messaging": False, "timestamp": None, "position": _pos(ZERO, "/", "["),
                             "comment": "Test"}
    e["ex-lora-igate"] = {"kind": "position", "data_type": "=", "messaging": True, "timestamp": None, "position": _pos(ZERO, "/", "-"),
                          "comment": "LoRa iGate test"}
    return e


_CACHE = {}


def table():
    if not _CACHE:
        _CACHE.update(_existing())
        _CACHE.update(_generated())
    return _CACHE


def split_header(header):
    """`SRC>DEST[,VIA...]` -> {source, dest, path}; a '*' stays on the digipeater it is written on."""
    src, _gt, rest = header.partition(">")
    parts = rest.split(",")
    return {"source": src, "dest": parts[0], "path": parts[1:]}


def expected(name, header):
    """The expectation dict of a case: header fields (exact) plus the table entry. KeyError when the
    case has no entry (the unit tests check that every case has one)."""
    out = split_header(header)
    out.update(table()[name])
    return out


# ------------------------------------------------------------------ comparing with decode_aprs

_PRINTED = re.compile(r"^([NSEW]) (\d+) ([\d.]+)$")


def printed_degrees(text):
    """'N 49 03.5000' -> exact signed degrees (Fraction)."""
    m = _PRINTED.match(text)
    if not m:
        raise ValueError("not a printed coordinate: %r" % (text,))
    value = Fraction(int(m.group(2))) + Fraction(m.group(3)) / 60
    return -value if m.group(1) in "SW" else value


def position_within_half_precision(position, lat_text, lon_text):
    """True when both printed coordinates are within half of the printed precision (0.00005 minute)
    of the generator's exact value."""
    return (abs(axis_degrees(position["lat"]) - printed_degrees(lat_text)) <= HALF_PRINTED_DEGREE
            and abs(axis_degrees(position["lon"]) - printed_degrees(lon_text)) <= HALF_PRINTED_DEGREE)


def dw_position(exp):
    """The position decode_aprs is expected to print: the generator's, except where it is documented
    not to apply a DAO."""
    dec = exp.get("decode_aprs", {})
    return dec.get("position", exp.get("position"))


def compare(exp, record):
    """Mismatches between an expectation and one aprs_decode.json record, as a list of strings (empty when
    they agree under the policy). Only what decode_aprs prints is compared; the documented differences
    (`expect.decode_aprs`) are compared against the value recorded there."""
    fields = record["fields"]
    bad = []
    dec = exp.get("decode_aprs", {})
    kind = exp["kind"]
    if kind in ("unknown-dti", "empty", "invalid"):
        if kind == "unknown-dti" and exp["data_type"] != "m" and "Unknown APRS Data Type" not in fields.get("data_type", ""):
            bad.append("data type: expected an unknown-indicator error, got %r" % fields.get("data_type"))
        return bad
    inner = exp.get("inner")
    if inner is not None:
        return compare(dict(inner), record)
    want_type = DW_TYPE.get(kind)
    got_type = fields.get("data_type", "")
    if kind == "object" and not exp["live"]:
        want_type = "Killed Object"
    if kind == "item" and not exp["live"]:
        want_type = "Killed Item"
    if kind == "position" and exp.get("timestamp"):
        want_type = "Position with time"
    if kind == "bulletin" and exp["bulletin_group"]:
        want_type = "Group Bulletin"
    if want_type and got_type != want_type:
        bad.append("data type %r != %r" % (got_type, want_type))

    def check(label, want, got):
        if want != got:
            bad.append("%s: expected %r, decode_aprs %r" % (label, want, got))

    src = exp.get("source")
    if kind == "message":
        if "message_id" not in dec.get("differs", []):
            check("message id", exp["message_id"], fields.get("message_number"))
        check("addressee", exp["addressee"], fields.get("message_to"))
        if "text" not in dec.get("differs", []):
            check("text", exp["text"], fields.get("comment", ""))
        if src:
            check("sender", src, fields.get("message_from"))
    elif kind in ("ack", "reject"):
        check("ack id", exp["ack_id"], fields.get("message_number"))
        check("addressee", exp["addressee"], fields.get("of_station"))
        if src:
            check("sender", src, fields.get("by_station"))
    elif kind in ("bulletin", "announcement"):
        check("bulletin id", dec.get("bulletin_id", exp["bulletin_id"]), fields.get("bulletin_id"))
        if exp["bulletin_group"] and kind == "bulletin":
            check("bulletin group", exp["bulletin_group"], fields.get("bulletin_group"))
        check("text", exp["text"], fields.get("comment", ""))
    elif kind == "status":
        if "grid" in exp:
            check("grid", exp["grid"], fields.get("position", {}).get("grid"))
        check("text", exp["text"], fields.get("comment", ""))
    if kind in ("object", "item"):
        check("name", exp["name"], fields.get("name"))
    if "position" in exp:
        pos = dw_position(exp)
        p = fields.get("position") or {}
        if not p.get("lat_text") or not p.get("lon_text"):
            bad.append("no printed position")
        elif not position_within_half_precision(pos, p["lat_text"], p["lon_text"]):
            bad.append("position not within half the printed precision")
    want_course = exp.get("course_deg", (exp.get("compressed") or {}).get("course_deg"))
    printed = (fields.get("position") or {}).get("course_deg")
    if printed is not None:
        check("course", want_course, printed)
    elif want_course not in (None, 0):
        bad.append("course %r expected but decode_aprs printed none" % (want_course,))
    speed = exp.get("speed_kn")
    if speed is None and "speed_code" in (exp.get("compressed") or {}):
        speed = speed_knots_from_code(exp["compressed"]["speed_code"])
    if speed:
        p = fields.get("position") or {}
        if abs(p.get("speed_kmh", -99) - float(speed) * 1.852) > 1.0:
            bad.append("speed km/h %r not within 1 of %.1f" % (p.get("speed_kmh"), float(speed) * 1.852))
    if "altitude_ft" in exp:
        check("altitude ft", exp["altitude_ft"], (fields.get("position") or {}).get("alt_ft"))
    if "altitude_m" in exp:
        check("altitude m", exp["altitude_m"], (fields.get("position") or {}).get("alt_m"))
    if "compressed" in exp and "alt_code" in exp["compressed"]:
        ft = 1.002 ** exp["compressed"]["alt_code"]
        got = (fields.get("position") or {}).get("alt_ft")
        if got is None or abs(got - ft) >= 1.0:
            bad.append("compressed altitude %r not within 1 ft of %.2f" % (got, ft))
    if "phg" in exp:
        got = fields.get("phg") or {}
        want = exp["phg"]
        if want["power_w"] == 0:
            if got:
                bad.append("PHG with power 0 should print nothing, got %r" % (got,))
        else:
            for k in ("power_w", "height_ft", "gain_dbi", "directivity"):
                check("phg " + k, want[k], got.get(k))
    if "range_mi" in exp:
        check("range", float(exp["range_mi"]), fields.get("range_miles"))
    if "compressed" in exp and "range_code" in exp["compressed"]:
        want = 2.0 * 1.08 ** exp["compressed"]["range_code"]
        got = fields.get("range_miles")
        if got is None or abs(got - want) > 0.05:
            bad.append("compressed range %r not within 0.05 of %.2f" % (got, want))
    if kind == "mic-e":
        code = exp["message_code"]
        word = ac.MICE_STD[7] if code == "Emergency" else ac.MICE_STD[int(code[1:])] if code[0] == "M" else "Custom-%s" % code[1:]
        check("mic-e status", word, fields.get("mic_e_status"))
        if "comment" in exp:
            check("comment", exp["comment"], fields.get("comment"))
    if "weather" in exp:
        bad.extend(_compare_weather(exp, fields.get("weather") or {}))
    return bad


def _compare_weather(exp, got):
    bad = []
    w = exp["weather"]

    def check(label, want, have):
        if want != have:
            bad.append("weather %s: expected %r, decode_aprs %r" % (label, want, have))

    if w["wind_dir_deg"] is not None:
        check("wind direction", w["wind_dir_deg"], got.get("wind_dir_deg"))
    if w["wind_speed_mph"] is not None:
        if exp["kind"] == "weather-position":
            want = round(w["wind_speed_mph"] * 1.15078, 1)        # decode_aprs reads the extension as knots
            check("wind speed (extension form, read as knots)", want, got.get("wind_mph"))
        else:
            check("wind speed", float(w["wind_speed_mph"]), got.get("wind_mph"))
    for key, got_key in (("gust_mph", "gust_mph"), ("temp_f", "temp_f"), ("humidity_pct", "humidity_pct"),
                         ("luminosity_wm2", "luminosity_wm2")):
        if w[key] is not None:
            check(key, w[key], got.get(got_key))
    for key, got_key in (("rain_1h_hundredths_in", "rain_1h_in"), ("rain_24h_hundredths_in", "rain_24h_in"),
                         ("rain_midnight_hundredths_in", "rain_midnight_in")):
        if w[key] is not None:
            check(key, w[key], round(got.get(got_key, -1) * 100))
    if w["barometer_tenths_mbar"] is not None:
        want = w["barometer_tenths_mbar"] / 10.0 * 0.02953
        have = got.get("barometer_inhg")
        if have is None or abs(have - want) > 0.011:
            bad.append("weather barometer %r not within 0.011 inHg of %.4f" % (have, want))
    if w["software_tag"] is not None:
        check("software", w["software_tag"], got.get("software"))
    return bad
