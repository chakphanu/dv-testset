# SPDX-License-Identifier: MIT
"""DP6c (weather and telemetry on the position map) and DP19 (locator status privacy): the pure parts of the rig cases.

Source of truth: DV-Router docs/design/data.md (5.3 bodies, 8.2 paths, 13.2 hub, 13.4 privacy, 13.5 monitor) and web.md "Position map" at 4ac79f26; the golden
frames are the literals of dvtest/proto/aprs.py (G1-G19 of WXTLM.md). No network, no I/O: cases/dp6c_support.py and the case files build on this.

* Public map rows are keyed by the GUARD identity (N0WXA-13, SSID kept) and carry `hops` and `forwarded`; privacy applies to the BASE callsign.
* Types 5 (weather) and 6 (telemetry) appear in the positions view (public state `rooms[].positions[]`, public SSE `data`) and nowhere else: not in the
  message history, not in the browser event views (web/shared/event-display.js).
* The Weather column shows METRIC first with the native unit in parentheses (`C=(F-32)*5/9`, `km/h=mph*1.609344`, `mm=in100*0.254`, `hPa=hpa10/10`),
  mirrored in `metric_weather` / `weather_summary` / `weather_fields` from web/site/positions-station.js (JavaScript Math.round, half up).
"""
import math
import types
from decimal import Decimal

from dvtest.proto import aprs

# one base callsign per case family: the controller admits 1 position, 1 weather and 1 telemetry per 30 s per BASE callsign (separate buckets)
RF_BASE = "N0WXA"                    # RF main case: -13 position + weather, -11 position + telemetry, -9 position only
RF_WX, RF_TLM, RF_POS, RF_SOLO = RF_BASE + "-13", RF_BASE + "-11", RF_BASE + "-9", "N0WXC-5"
RF_DEFINER = RF_BASE + "-1"          # PARM/UNIT/EQNS/BITS come from the same BASE as the telemetering station (authority)
# a definition from ANOTHER base for the same telemetering station must change nothing (authority = the sender's base equals the addressee's base,
# case-insensitive; confirmed by dv-router, definitions.rs 231-240): distinct names, units, coefficients and sense, so that any effect shows
FOREIGN_BASE = "N0WXB"
FOREIGN_DEFINER = FOREIGN_BASE + "-1"
FOREIGN_PARM = ["Fx1", "Fx2", "Fx3", "Fx4", "Fx5", "Fb1", "Fb2", "Fb3", "Fb4", "Fb5"]
FOREIGN_UNIT = ["fu1", "fu2", "fu3", "fu4", "fu5", "fl1", "fl2", "fl3", "fl4", "fl5"]
FOREIGN_EQNS = ["0", "1", "0"] * 5
FOREIGN_BITS = "11111111"
FOREIGN_PROJECT = "Foreign Project"
Z_BASE = "N0WXZ"                     # the compressed weather with speed byte z
IS_SOLO = "N0WXI-3"                  # APRS-IS weather and telemetry without a position
IS_TLM = "N0WXI-4"
HIDE_BASE = "N0WXH"                  # the account whose hide_position case removes every SSID
OPT_BASE = "N0OPT"                   # rig/config/dv-router/dp7-position-optout.toml
LOC_BASE = "N0LOC"                   # DP19
ROOM = "B"
GAP_S = 31.0                         # >= 30 s between two positions (or two weather, two telemetry) of one base callsign
TOCALL = "APDVT1"

WX_ITEM, TLM_ITEM = "weather", "telemetry"
ANNEX_TYPES = (WX_ITEM, TLM_ITEM)

# the native weather bodies of G1/G5/G7 (APRS101 pp.63-66): wind 220 deg 4 mph gust 5 mph 77 F rain 0/0/0 humidity 50 % 990.0 hPa software w unit RSW
G5_WEATHER = {"form": "position", "wind_dir_deg": 220, "wind_mph": 4, "gust_mph": 5, "temp_f": 77, "rain_1h_in100": 0, "rain_24h_in100": 0,
              "rain_midnight_in100": 0, "humidity_pct": 50, "pressure_hpa10": 9900, "software": "w", "unit": "RSW"}
G1_WEATHER = dict(G5_WEATHER, form="positionless")
# G7 compressed: the wind direction and speed come from `cs` = 7P (88 deg, 42 mph); with s = z the speed is out of range and dropped
G7_WEATHER = dict(G5_WEATHER, wind_dir_deg=88, wind_mph=42)
G7Z_WEATHER = {k: v for k, v in dict(G5_WEATHER, wind_dir_deg=88).items() if k != "wind_mph"}
WIND_ABSENT_NOTE = "a compressed speed byte of z is out of range: the wind speed is dropped, the marker and the rest of the weather stay"


def js_round(value):
    """JavaScript Math.round: half toward positive infinity."""
    return math.floor(value + 0.5)


def metric_weather(body):
    """web/site/positions-station.js metricWeather: the metric values of a native weather body (floats as JavaScript would compute them)."""
    out = {}
    if "temp_f" in body:
        out["temp_c"] = js_round((body["temp_f"] - 32) * 50 / 9) / 10
    if "wind_mph" in body:
        out["wind_kmh"] = js_round(body["wind_mph"] * 1.609344)
    if "gust_mph" in body:
        out["gust_kmh"] = js_round(body["gust_mph"] * 1.609344)
    for key, name in (("rain_1h_in100", "rain_1h_mm"), ("rain_24h_in100", "rain_24h_mm"), ("rain_midnight_in100", "rain_midnight_mm")):
        if key in body:
            out[name] = js_round(body[key] * 254 / 100) / 10
    if "pressure_hpa10" in body:
        out["pressure_hpa"] = body["pressure_hpa10"] / 10
    if "snow_24h_in100" in body:
        out["snow_24h_cm"] = js_round(body["snow_24h_in100"] * 254 / 1000) / 10
    return out


def weather_summary(body):
    """The Weather column / map label text: `25.0 C 6 km/h` (temperature then wind, either may be absent)."""
    metric = metric_weather(body or {})
    parts = []
    if "temp_c" in metric:
        parts.append("%.1f C" % metric["temp_c"])
    if "wind_kmh" in metric:
        parts.append("%d km/h" % metric["wind_kmh"])
    return " ".join(parts)


def weather_fields(body):
    """[(label, value text)] of the station details in English, metric first with the native unit in parentheses (positions-station.js weatherFields)."""
    metric = metric_weather(body)
    fields = []
    if "temp_f" in body:
        fields.append(("Temperature", "%.1f C (%d F)" % (metric["temp_c"], body["temp_f"])))
    if "wind_mph" in body:
        fields.append(("Wind", "%d km/h (%d mph)" % (metric["wind_kmh"], body["wind_mph"])))
    if "wind_dir_deg" in body:
        fields.append(("Wind direction", "%d deg" % body["wind_dir_deg"]))
    if "gust_mph" in body:
        fields.append(("Gust", "%d km/h (%d mph)" % (metric["gust_kmh"], body["gust_mph"])))
    for key, name, label in (("rain_1h_in100", "rain_1h_mm", "Rain, last hour"), ("rain_24h_in100", "rain_24h_mm", "Rain, last 24 hours"),
                             ("rain_midnight_in100", "rain_midnight_mm", "Rain since midnight")):
        if key in body:
            fields.append((label, "%.1f mm (%.2f in)" % (metric[name], body[key] / 100)))
    if "humidity_pct" in body:
        fields.append(("Humidity", "%d%%" % body["humidity_pct"]))
    if "pressure_hpa10" in body:
        fields.append(("Pressure", "%.1f hPa" % metric["pressure_hpa"]))
    if body.get("software"):
        fields.append(("Software", body["software"]))
    if body.get("unit"):
        fields.append(("Weather station unit", body["unit"]))
    return fields


def weather_problems(got, want):
    """Differences between a public weather body and the expected native body (`form` included); a key outside the WeatherBody schema is one too."""
    problems = []
    for key, value in want.items():
        if got.get(key) != value:
            problems.append("%s = %r, expected %r" % (key, got.get(key), value))
    extra = sorted(set(got) - set(want))
    if extra:
        problems.append("keys outside the expectation: %s" % ", ".join("%s=%r" % (k, got[k]) for k in extra))
    return problems


# telemetry G9 under the G10 definitions (WXTLM.md): five calibrated analog values, eight digital bits with their BITS sense and the PARM/UNIT names
G9_SEQ = aprs.G9_REPORT[0]
G9_RAW = [str(v) for v in aprs.G9_REPORT[1]]
G9_ANALOG_NAMES = aprs.G10_PARM[:5]
G9_ANALOG_UNITS = aprs.G10_UNIT[:5]
G9_DIGITAL_BITS = [int(b) for b in aprs.G9_REPORT[2]]
G9_DIGITAL_ACTIVE = [bit == int(sense) for bit, sense in zip(G9_DIGITAL_BITS, aprs.G10_BITS)]
G9_DIGITAL_NAMES = aprs.G10_PARM[5:] + [None] * 3                 # B6..B8 are unnamed
G9_DIGITAL_LABELS = aprs.G10_UNIT[5:] + [None] * 3


def telemetry_problems(body, defined=True):
    """Differences between a public telemetry body (form numbered, sequence 5) and G9 under the G10 definitions. `defined=False`: the definitions were not
    heard, so the body carries raw values only (no names, units or calibrated values)."""
    problems = []
    if body.get("form") != "numbered":
        problems.append("form %r, expected numbered" % (body.get("form"),))
    if body.get("seq") != G9_SEQ:
        problems.append("seq %r, expected %d" % (body.get("seq"), G9_SEQ))
    analog = {c.get("ch"): c for c in body.get("analog") or []}
    for ch in range(1, 6):
        got = analog.get(ch)
        if got is None:
            problems.append("no analog channel %d" % ch)
            continue
        if got.get("raw") != G9_RAW[ch - 1]:
            problems.append("A%d raw %r, expected %r" % (ch, got.get("raw"), G9_RAW[ch - 1]))
        if defined:
            try:
                same = Decimal(got.get("value")) == Decimal(aprs.G9_CALIBRATED[ch - 1])
            except Exception:                                      # noqa: BLE001  a missing or malformed decimal string
                same = False
            if not same:
                problems.append("A%d value %r, expected %s (calibrated)" % (ch, got.get("value"), aprs.G9_CALIBRATED[ch - 1]))
            if got.get("name") != G9_ANALOG_NAMES[ch - 1]:
                problems.append("A%d name %r, expected %r" % (ch, got.get("name"), G9_ANALOG_NAMES[ch - 1]))
            if got.get("unit") != G9_ANALOG_UNITS[ch - 1]:
                problems.append("A%d unit %r, expected %r" % (ch, got.get("unit"), G9_ANALOG_UNITS[ch - 1]))
        elif got.get("value") is not None or got.get("name") is not None:
            problems.append("A%d carries a calibrated value or name without definitions: %r" % (ch, got))
    digital = {c.get("ch"): c for c in body.get("digital") or []}
    for ch in range(1, 9):
        got = digital.get(ch)
        if got is None:
            problems.append("no digital channel %d" % ch)
            continue
        if got.get("bit") != G9_DIGITAL_BITS[ch - 1]:
            problems.append("B%d bit %r, expected %d" % (ch, got.get("bit"), G9_DIGITAL_BITS[ch - 1]))
        if defined and got.get("active") is not G9_DIGITAL_ACTIVE[ch - 1]:
            problems.append("B%d active %r, expected %r (bit == BITS sense)" % (ch, got.get("active"), G9_DIGITAL_ACTIVE[ch - 1]))
        if defined and got.get("name") != G9_DIGITAL_NAMES[ch - 1]:
            problems.append("B%d name %r, expected %r" % (ch, got.get("name"), G9_DIGITAL_NAMES[ch - 1]))
        if defined and got.get("label") != G9_DIGITAL_LABELS[ch - 1]:
            problems.append("B%d label %r, expected %r" % (ch, got.get("label"), G9_DIGITAL_LABELS[ch - 1]))
    if defined and body.get("project") != aprs.G10_PROJECT:
        problems.append("project %r, expected %r" % (body.get("project"), aprs.G10_PROJECT))
    return problems


# ---------------------------------------------------------------- public rows

ROW_KEYS = {"type", "id", "item_type", "room", "callsign", "protocol", "ts_ms", "ident", "via", "hops", "forwarded", "body"}      # openapi Position (DP6c D2)
COORDINATE_KEYS = ("lat_e7", "lon_e7")


def identity_key(value):
    """The page's station key of a callsign: upper case, cut at the first space, SSID and suffix kept (positions-station.js stationKey; the same rule as
    cases/position_support.py identity_matches)."""
    return str(value).strip().upper().split(" ")[0]


def annex_rows(rows, callsign=None, kind=None):
    """The weather / telemetry rows of a list, optionally of one EXACT guard identity (upper case, SSID kept) and one kind."""
    return [r for r in rows if isinstance(r, dict) and r.get("item_type") in ANNEX_TYPES and (kind is None or r.get("item_type") == kind)
            and (callsign is None or identity_key(r.get("callsign", "")) == identity_key(callsign))]


def station_rows(rows, callsign=None, kind=None):
    """Position, object, weather and telemetry rows of a list (the public map rows), optionally of an exact guard identity and one kind."""
    return [r for r in rows if isinstance(r, dict) and r.get("item_type") in ("position", "object") + ANNEX_TYPES and (kind is None or r.get("item_type") == kind)
            and (callsign is None or identity_key(r.get("callsign", "")) == identity_key(callsign))]


def base_rows(rows, base):
    """Every public map row of the BASE callsign (any SSID): what a hide_position or an opt-out must remove."""
    base = base.upper()
    return [r for r in station_rows(rows) if identity_key(r.get("callsign", "")).split("-")[0].split("/")[0] == base]


def row_problems(row):
    """Problems of one public weather/telemetry/position row against the DP6c public shape: only the allow-listed keys, the guard identity as callsign, `hops` an int
    0..255, `forwarded` a bool, no coordinates in a weather or telemetry body."""
    problems = []
    extra = sorted(set(row) - ROW_KEYS)
    if extra:
        problems.append("keys outside the public row: %s" % extra)
    missing = sorted(ROW_KEYS - set(row))
    if missing:
        problems.append("keys missing from the public row: %s" % missing)
    hops = row.get("hops")
    if not isinstance(hops, int) or isinstance(hops, bool) or not 0 <= hops <= 255:
        problems.append("hops %r is not an int 0..255" % (hops,))
    if not isinstance(row.get("forwarded"), bool):
        problems.append("forwarded %r is not a bool" % (row.get("forwarded"),))
    if row.get("item_type") in ANNEX_TYPES and any(k in (row.get("body") or {}) for k in COORDINATE_KEYS):
        problems.append("a %s body carries coordinates" % row.get("item_type"))
    return problems


# ---------------------------------------------------------------- configuration cases (cfgload)

CONFIG_CASES = []


def _case(cid, what, mutate, verdict="reject", hint=None):
    CONFIG_CASES.append(types.SimpleNamespace(id=cid, what=what, mutate=mutate, verdict=verdict, hint=hint))


def _sub(old, new):
    def f(text):
        if old not in text:
            raise ValueError("anchor not in the snippet: %r" % old)
        return text.replace(old, new, 1)
    return f


_ROOM_PATH = '''
[[data.path]]
name = "dp6c-room-wx"
from = "room:B"
to = "room:C"
types = ["weather"]
per_min = 10
'''
_ROOM_TO_IS = '''
[[data.path]]
name = "dp6c-room-to-is"
from = "room:B"
to = "edge:aprsis"
types = ["weather"]
per_min = 10
'''

_case("accept-weather-telemetry", "an APRS-IS -> room path with the types weather and telemetry loads (the path F1 broke)", lambda t: t, verdict="accept")
_case("unknown-type", "an unknown type next to the valid ones", _sub('types = ["weather", "telemetry"]', 'types = ["weather", "telemetry", "bogus"]'), hint="type")
_case("unknown-type-only", "only an unknown type", _sub('types = ["weather", "telemetry"]', 'types = ["bogus"]'), hint="type")
_case("weather-from-room", "a room source refuses weather (display only, never forwarded from a room)", lambda t: t + _ROOM_PATH)
_case("weather-room-to-aprsis", "room -> edge:aprsis allows `position` only", lambda t: t + _ROOM_TO_IS)


def config_problems(case, verdict, exit_code):
    """Problems ([] = as expected) of a `dv-router config check` verdict (dvtest.tools.config_check.judge_check): accept = exit status 0; reject = a non-zero
    exit status and a positioned `FILE:LINE:COL: message` error that names the data path."""
    if verdict.get("unsupported"):
        return ["the image has no `config check` (usage text printed)"]
    if case.verdict == "accept":
        return [] if exit_code == 0 and verdict.get("valid") else ["expected to load, got exit status %r: %s" % (exit_code, "; ".join(verdict.get("errors") or ["no error line"]))]
    if exit_code == 0 or verdict.get("valid"):
        return ["SILENT SUCCESS: %s loaded (exit status 0); expected an error naming data.path" % case.what]
    parsed = verdict.get("parsed") or []
    if not parsed:
        return ["no `FILE:LINE:COL: message` error line (exit status %r only)" % (exit_code,)]
    first = parsed[0]
    problems = []
    if "path" not in first["key"] and "path" not in first["message"]:
        problems.append("the error key %r / message %r does not name the data path" % (first["key"], first["message"]))
    if case.hint and case.hint not in first["message"] and case.hint not in first["key"]:
        problems.append("the message does not mention %r: %s" % (case.hint, first["message"]))
    if not first["message"].strip():
        problems.append("the error message is empty")
    return problems
