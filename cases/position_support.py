# SPDX-License-Identifier: MIT
"""Shared pieces of the position cases of DV-Router (DP7: every admitted position and object is PUBLIC; private means
dropped before publication): cases/test_dp15_positions.py (the DP7 cases) and the rebased position assertions of
cases/test_dp15_streams.py. Case table, run commands and the list of UNVERIFIED shapes: docs/dp7-cases.md.

What the design says (DV-Router docs/design/data.md sections 5, 13.2, 13.4; web/README.md "Position map and position
privacy"; docs/api/openapi-web.yaml schemas Position, PositionBody, ObjectPositionBody), and what the helpers encode:

* A position row (public state `rooms[].positions[]`, public SSE `data` event, operator feed): {type "data", id,
  item_type "position" or "object", room, callsign, protocol, ts_ms, ident, via, body}; the admin feed adds hops and
  forwarded. Body of a position: lat_e7, lon_e7, alt_dm, course_deg, speed_dmh, fix_unix_ms, symbol (2 characters),
  source (1 D-PRS, 3 YSF, 4 DMR, 5 APRS, 6 NXDN; data.md section 5, sources 1 to 6), ambiguity (0..4), comment; an object adds name and
  state (1 live); an object KILL is {name, state 0} and nothing else (no coordinates).
* ambiguity = the number of minute digits the station omitted (APRS101 ch. 6); the coordinates are the CENTRE of that
  box. 0 for every GPS fix; nonzero only for source 1 (D-PRS APRS lines) and 5 (APRS).
* The position page prints decimal degrees with 5 places for ambiguity 0 and `4 - level` places otherwise, the box
  centre truncated toward zero (web/site/positions.js formatCoordinate, ported in `format_coordinate`).
* Controller limits per base callsign: position and object 1 per 30 s (they share the bucket), see data_support.Pacer.
"""
import re
import time
from fractions import Fraction

from dvtest import aprs_cases as ac
from dvtest import aprs_expect, web

PUBLIC_EVENTS = "/api/v1/public/events"
PUBLIC_STATE = "/api/v1/public/state"
POSITION_E7 = 200               # tolerance of a coordinate, 2e-5 degrees (the DMRG step is 1.1e-5)
CENTRE_E7 = 2                   # the box centre in e7 units: the parser rounds a float, one unit either way
OPERATOR_WAIT_S = 15.0
PUBLIC_WAIT_S = 20.0            # the hub, the web process and the SSE ring: seconds, not minutes

POSITION_SOURCE = {"dprs": 1, "ysf": 3, "dmr": 4, "aprs": 5, "nxdn": 6}
ROW_KEYS = {"type", "id", "item_type", "room", "callsign", "protocol", "ts_ms", "ident", "via", "body"}     # openapi Position
ADMIN_EXTRA_KEYS = {"hops", "forwarded", "dst_kind", "dst_call"}
BODY_KEYS = {"lat_e7", "lon_e7", "alt_dm", "course_deg", "speed_dmh", "fix_unix_ms", "symbol", "source", "ambiguity", "comment"}
OBJECT_BODY_KEYS = BODY_KEYS | {"name", "state"}
KILL_BODY_KEYS = {"name", "state"}
COORDINATE_KEYS = ("lat_e7", "lon_e7")

# the position every case sends unless it says otherwise: 49 deg 03.50 N, 72 deg 01.75 W (the specification's own example
# of APRS101 ch. 6, dvtest.aprs_cases.POS), i.e. both coordinates have digits in every place the ambiguity blanks
APRS_LAT, APRS_LON = 49.058333333, -72.029166667
# the GPS position of the YSF / DMR / D-STAR cases (unambiguous): 10.5 N 20.25 E, as test_dp15_streams.py
LAT, LON = 10.5, 20.25
LAT_E7, LON_E7 = 105000000, 202500000


def base_call(call):
    """The base callsign of a displayed callsign: upper case, text before the first '-', '/' or space."""
    return re.split(r"[-/ ]", str(call).strip().upper(), maxsplit=1)[0]


def same_base(call, want):
    return base_call(call) == base_call(want)


# ---------------------------------------------------------------- the display of a coordinate

def format_coordinate(e7, ambiguity=0):
    """Port of formatCoordinate() of web/site/positions.js: what the public positions page prints for a coordinate in
    1e-7 degrees. 5 places (rounded half up) for ambiguity 0, `4 - ambiguity` places (truncated toward zero) otherwise."""
    if not 0 <= ambiguity <= 4:
        raise ValueError("ambiguity is 0..4")
    places = 5 if ambiguity == 0 else 4 - ambiguity
    scale, whole = 10 ** (7 - places), 10 ** places
    magnitude = abs(int(e7))
    units = magnitude // scale if ambiguity else (magnitude + scale // 2) // scale
    text = ("%d.%0*d" % (units // whole, places, units % whole)) if places else str(units)
    return ("-" if e7 < 0 and units != 0 else "") + text


def box_centre_degrees(exact, ambiguity):
    """The centre of the box a station with ambiguity level `ambiguity` meant, as an exact Fraction of degrees, from the
    GENERATOR's exact value (dvtest.aprs_expect axis {"int": hundredths of a minute with the blanked digits as zeros,
    "per_degree": 6000}). The box is `10 ** level` hundredths of a minute wide (0.1, 1, 10 minutes) and a whole degree
    (6000 hundredths) at level 4; the centre is the lower edge (the blanked digits as 0) plus half of the width, away from
    zero in the hemisphere's direction. Independent of the parser (which sets the first blanked digit to 5). Limit: an
    integer axis of 0 has no sign, so a box whose lower edge is exactly 0 degrees 0 minutes is taken as positive."""
    if exact["per_degree"] != 6000:
        raise ValueError("a plain position axis is in hundredths of a minute (6000 per degree)")
    n = exact["int"]
    width = {0: 0, 1: 10, 2: 100, 3: 1000, 4: 6000}[ambiguity]
    magnitude = Fraction(abs(n)) + Fraction(width, 2)
    return (magnitude if n >= 0 else -magnitude) / 6000


def expected_display(exact, ambiguity):
    """The text the page must print for the generator's exact axis value at `ambiguity`: the box centre cut to the sent
    precision (truncated toward zero for 1..4, rounded half up to 5 places for 0). Exact arithmetic, no float."""
    centre = box_centre_degrees(exact, ambiguity)
    places = 5 if ambiguity == 0 else 4 - ambiguity
    scaled = abs(centre) * 10 ** places
    units = int(scaled) if ambiguity else int(scaled + Fraction(1, 2))
    whole = 10 ** places
    text = ("%d.%0*d" % (units // whole, places, units % whole)) if places else str(units)
    return ("-" if centre < 0 and units != 0 else "") + text


def centre_e7(exact, ambiguity):
    """The box centre in 1e-7 degrees, rounded (what the body should hold within CENTRE_E7)."""
    value = box_centre_degrees(exact, ambiguity) * 10 ** 7
    return int(value + Fraction(1, 2)) if value >= 0 else -int(-value + Fraction(1, 2))


# ---------------------------------------------------------------- APRS strings with an ambiguity level

def plain_text(lat, lon, level=0, symbol="/>"):
    """The plain position text `DDMM.mmN/DDDMM.mmE>` with `level` blanked digits in both coordinates (APRS101 ch. 6)."""
    return ac.lat_text(lat, level) + symbol[0] + ac.lon_text(lon, level) + symbol[1]


def position_info(lat, lon, level=0, symbol="/>", comment="dp7", dti="!"):
    """An APRS information field of a plain position without timestamp."""
    return dti + plain_text(lat, lon, level, symbol) + comment


def exact_axes(lat, lon, level=0, symbol="/>"):
    """The generator's exact value of the text plain_text() makes: aprs_expect.plain() on the same string (hundredths of a
    minute with blanks as zeros, the ambiguity counted from the blanks). Returns (lat axis, lon axis, ambiguity)."""
    got = aprs_expect.plain(plain_text(lat, lon, level, symbol))
    return got["lat"], got["lon"], got["ambiguity"]


def object_info(name, lat, lon, live=True, level=0, symbol="/>", comment="dp7", when=None):
    """An APRS object report `;NAME_____*HHMMSSh<position>comment`; `_` instead of `*` kills it. The 9 character name is
    padded with spaces, the timestamp is HMS zulu (seconds resolution: a kill sent later has a later stamp, which matters
    for the name highwater of the hub, data.md 13.2), valid for the controller's 1 h fix time window."""
    if not 1 <= len(name) <= 9:
        raise ValueError("an object name is 1..9 characters")
    stamp = time.strftime("%H%M%Sh", time.gmtime(time.time() if when is None else when))
    return ";" + name.ljust(9) + ("*" if live else "_") + stamp + plain_text(lat, lon, level, symbol) + comment


def tnc2(source, info, dest="APDVT1", path=None):
    """The TNC2 text `SRC>DEST[,PATH]:info` (what an M17 packet with specifier 0x02 carries)."""
    return "%s>%s%s:%s" % (source, dest, "," + path if path else "", info)


def dmr_id_for(call):
    """The DMR id of the rig's registry (rig/config/dmrid.dat) that resolves to `call`: N0CALL, N0TEST, N0DMP and STRnnn
    (id 1023100 + nnn, nnn 1..120). None for a callsign the registry does not know."""
    call = base_call(call)
    fixed = {"N0CALL": 1023007, "N0TEST": 1023008, "N0DMP": 1023230}
    if call in fixed:
        return fixed[call]
    match = re.fullmatch(r"STR(\d{3})", call)
    if match and 1 <= int(match.group(1)) <= 120:
        return 1023100 + int(match.group(1))
    return None


# ---------------------------------------------------------------- rows: selection and shape

def is_spatial(row):
    return isinstance(row, dict) and row.get("item_type") in ("position", "object")


def spatial_rows(rows, callsign=None, item_type=None, name=None):
    """The position / object rows of a list, optionally of one base callsign, one item type and one object name."""
    out = []
    for row in rows:
        if not is_spatial(row):
            continue
        if item_type is not None and row.get("item_type") != item_type:
            continue
        if callsign is not None and not same_base(row.get("callsign", ""), callsign):
            continue
        if name is not None and str((row.get("body") or {}).get("name", "")).strip() != name:
            continue
        out.append(row)
    return out


def state_rows(state, room=None):
    """The position / object rows of a public state document (`rooms[].positions[]`), each with its room."""
    out = []
    for item in (state or {}).get("rooms", []) or []:
        if room is not None and item.get("room") != room:
            continue
        for row in item.get("positions", []) or []:
            if isinstance(row, dict):
                out.append(dict(row, room=row.get("room", item.get("room"))))
    return out


def check_public_row(row):
    """The public shape of openapi-web.yaml (additionalProperties false on Position and its bodies): only the allow-listed
    keys, no radio id, account, origin, address or path. Returns nothing; raises AssertionError."""
    assert is_spatial(row), row
    extra = set(row) - ROW_KEYS
    assert not extra, "public row carries keys outside the allow-list: %s (row %s)" % (sorted(extra), sorted(row))
    body = row.get("body")
    assert isinstance(body, dict), "public body is %r" % (body,)
    kill = row["item_type"] == "object" and body.get("state") == 0
    allowed = KILL_BODY_KEYS if kill else (OBJECT_BODY_KEYS if row["item_type"] == "object" else BODY_KEYS)
    assert not set(body) - allowed, "public body carries keys outside the allow-list: %s" % sorted(set(body) - allowed)


def near(value, want, tol=POSITION_E7):
    return abs(value - want) <= tol


def check_position(row, source=None, symbol=None, comment=None, lat_e7=LAT_E7, lon_e7=LON_E7, tol=POSITION_E7, ambiguity=None):
    """Assertions on one position row (any surface): coordinates within `tol`, and, when given, source, symbol, comment
    and ambiguity (a GPS fix: 0)."""
    body = row["body"]
    assert isinstance(body, dict), "position body is %r" % (body,)
    assert near(body["lat_e7"], lat_e7, tol) and near(body["lon_e7"], lon_e7, tol), \
        "position %s,%s e7, want about %s,%s" % (body["lat_e7"], body["lon_e7"], lat_e7, lon_e7)
    if source is not None:
        assert body["source"] == source, "position source %r, want %r" % (body["source"], source)
    if symbol is not None:
        assert body["symbol"] == symbol, "symbol %r, want %r" % (body["symbol"], symbol)
    if comment is not None:
        assert body["comment"] == comment, "comment %r, want %r" % (body["comment"], comment)
    if ambiguity is not None:
        assert body.get("ambiguity") == ambiguity, "ambiguity %r, want %r" % (body.get("ambiguity"), ambiguity)


def check_displayed(row, lat_exact, lon_exact, level):
    """The ambiguity case: the body carries `level` and the box centre (within CENTRE_E7), and the page's text of both
    coordinates (format_coordinate) equals the text derived from the generator's exact value. Returns (lat text, lon text)."""
    body = row["body"]
    assert body.get("ambiguity") == level, "ambiguity %r in the body, want %d (the level the station sent)" % (body.get("ambiguity"), level)
    for key, exact in (("lat_e7", lat_exact), ("lon_e7", lon_exact)):
        want = centre_e7(exact, level)
        assert abs(body[key] - want) <= CENTRE_E7, "%s %d, want the box centre %d (level %d)" % (key, body[key], want, level)
    shown = (format_coordinate(body["lat_e7"], level), format_coordinate(body["lon_e7"], level))
    want_text = (expected_display(lat_exact, level), expected_display(lon_exact, level))
    assert shown == want_text, "the page would print %s, the sent precision is %s (level %d)" % (shown, want_text, level)
    return shown


# ---------------------------------------------------------------- reading the surfaces

def public_state(anon):
    """The decoded public state document (GET /api/v1/public/state, anonymous)."""
    return anon.get(PUBLIC_STATE, pace=True, expect=200).json()


def public_rows(anon, room=None):
    """The retained position / object rows of the anonymous public state."""
    return state_rows(public_state(anon), room)


def wait_public_row(anon, callsign, timeout=PUBLIC_WAIT_S, room=None, item_type="position", name=None, pred=None, poll=1.0):
    """(row, all rows seen at the last read) of the first public-state row of the base callsign that matches `pred`, polled
    until `timeout`; row is None when it never appeared."""
    end = time.monotonic() + timeout
    while True:
        rows = public_rows(anon, room)
        hits = [r for r in spatial_rows(rows, callsign, item_type, name) if pred is None or pred(r)]
        if hits:
            return hits[0], rows
        if time.monotonic() >= end:
            return None, rows
        time.sleep(poll)


def wait_gone(anon, callsign, timeout=PUBLIC_WAIT_S, room=None, item_type=None, name=None, poll=1.0):
    """True once no public-state row of the base callsign (and object name) is left, polled until `timeout`."""
    end = time.monotonic() + timeout
    while True:
        rows = spatial_rows(public_rows(anon, room), callsign, item_type, name)
        if not rows:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(poll)


def sse_rows(watch, callsign=None, since=0.0, item_type=None, name=None):
    """The position / object `data` events a SseWatch saw after `since`."""
    return spatial_rows([o for _t, o in watch.data_events(since)], callsign, item_type, name)


def operator_baseline(op):
    """The id of the newest event of the admin feed (0 when there is none), to read only what follows."""
    _items, last = web.operator_events(op, limit=1)
    return int(last or 0)


def wait_operator_rows(op, baseline, pred, timeout=OPERATOR_WAIT_S, poll=1.0, enough=1):
    """(matching rows, all rows after the baseline) of the operator feed once `enough` rows match pred or after timeout."""
    end = time.monotonic() + timeout
    seen = {}
    while True:
        items, _last = web.operator_events(op, after=baseline)
        for item in items:
            seen[item.get("id")] = item
        hits = [r for r in seen.values() if pred(r)]
        if len(hits) >= enough or time.monotonic() >= end:
            return hits, list(seen.values())
        time.sleep(poll)


def position_rows(rows, callsign=None):
    """Position rows only (not objects) of a list, optionally of one base callsign."""
    return spatial_rows(rows, callsign, "position")
