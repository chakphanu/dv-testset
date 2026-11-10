# SPDX-License-Identifier: MIT
"""Pure helpers of the OBSPKT1 cases (cases/test_obspkt1_monitor.py; docs/obspkt1-cases.md): the admin packet monitor of DV-Router
(`GET /api/v1/admin/packets`, `GET /api/v1/admin/packets/stream`; data.md 13.5, admin.md "Packet monitor", openapi-admin.yaml PktMonRow).
No network, no pytest.

  snapshot_problems(body)      the documented shape of a snapshot (rows, ports, producers; row keys, stages, bounds) and what it breaks
  producer_kind(id)            "edge" (ax25-... / aprsis-...) or "worker": only the worker rows carry the information field
  row_src / rows_from          the source callsign of a row (from line or header) and the rows of one source
  split_tnc2(line)             (src, dst, path list, info) of a TNC2 line
  outcomes / has_outcome       the (stage, result) pairs of a row
  parse_stream(frames)         SSE frames -> hello producers, rows (id, boot, row), gaps, byes, resync
  find_leaks(texts, markers)   which marker payloads appear in which text (logs, public state, telemetry, other admin routes)
  frames for the cases         RF frames with unique marker payloads (status, NOGATE, query, positions, Mic-E)
"""
import json
import re
import secrets
import types

from dvtest import aprs_cases, kiss_tnc

STAGES = ("modem", "raw", "parse", "rf", "route", "item", "render", "txcore", "sched", "write", "client")
ROW_KEYS = ("seq", "t_ms", "port", "dir", "out")
ROW_OPTIONAL = ("ref", "of", "line", "header", "redacted", "more")
MAX_ROWS = 1000
MAX_OUTCOMES = 8
LINE_MAX = 512
MIC_E = "(mic-e)"
REDACTED = "position_policy"
STEP_UP_WINDOW_S = 300.0                 # admin.md: step-up needs re-authentication within the last 5 minutes (a login opens the window)
STEP_UP_MARGIN_S = 15.0


# ---------------------------------------------------------------- shapes

def producer_kind(producer_id):
    """`edge` for the AX.25 and APRS-IS edges (ids like `ax25-0000000000000007`, `aprsis-...`), else `worker`."""
    return "edge" if str(producer_id).startswith(("ax25-", "aprsis-", "ax25", "aprsis")) else "worker"


def row_problems(row, where="row"):
    """Problems of one PktMonRow against openapi-admin.yaml (required keys, enums, bounds)."""
    problems = []
    if not isinstance(row, dict):
        return ["%s is not an object" % where]
    for key in ROW_KEYS:
        if key not in row:
            problems.append("%s lacks %r" % (where, key))
    if problems:
        return problems
    unknown = [k for k in row if k not in ROW_KEYS + ROW_OPTIONAL + ("id", "boot")]
    if unknown:
        problems.append("%s has unknown keys %s" % (where, unknown))
    if row["dir"] not in ("rx", "tx"):
        problems.append("%s dir %r" % (where, row["dir"]))
    if not isinstance(row["seq"], int) or not isinstance(row["t_ms"], int) or not isinstance(row["port"], str) or len(row["port"]) > 64:
        problems.append("%s seq/t_ms/port types" % where)
    if not isinstance(row["out"], list) or len(row["out"]) > MAX_OUTCOMES:
        problems.append("%s out is not a list of at most %d" % (where, MAX_OUTCOMES))
    else:
        for n, o in enumerate(row["out"]):
            if not isinstance(o, dict) or o.get("stage") not in STAGES or not isinstance(o.get("result"), str) or not o.get("result"):
                problems.append("%s outcome %d is %r" % (where, n, o))
    for key in ("line", "header"):
        if key in row and (not isinstance(row[key], str) or len(row[key]) > LINE_MAX * 2):
            problems.append("%s %s is not a bounded string" % (where, key))
    if "redacted" in row and row["redacted"] != REDACTED:
        problems.append("%s redacted %r" % (where, row["redacted"]))
    if "line" in row and "header" in row:
        problems.append("%s has both line and header" % where)
    return problems


def snapshot_problems(body, limit=None):
    """Problems of a GET /api/v1/admin/packets body: {rows, ports, producers}; rows newest first, at most MAX_ROWS (or `limit`), each row a
    PktMonRow plus `id` and `boot`; ports {id, port, first_seq, last_seq}; producers {id, boot (16 integers 0-255)}."""
    if not isinstance(body, dict):
        return ["the body is not an object"]
    problems = []
    for key in ("rows", "ports", "producers"):
        if not isinstance(body.get(key), list):
            problems.append("%r is not a list" % key)
    if problems:
        return problems
    rows = body["rows"]
    if len(rows) > (limit or MAX_ROWS):
        problems.append("%d rows exceed the limit %d" % (len(rows), limit or MAX_ROWS))
    for n, row in enumerate(rows[:50]):
        problems += row_problems(row, "rows[%d]" % n)
        if isinstance(row, dict) and (not isinstance(row.get("id"), str) or not _boot_ok(row.get("boot"))):
            problems.append("rows[%d] lacks id / boot" % n)
    for n, p in enumerate(body["ports"]):
        if not isinstance(p, dict) or not {"id", "port", "first_seq", "last_seq"} <= set(p):
            problems.append("ports[%d] is %r" % (n, p))
    for n, p in enumerate(body["producers"]):
        if not isinstance(p, dict) or not isinstance(p.get("id"), str) or not _boot_ok(p.get("boot")):
            problems.append("producers[%d] is %r" % (n, p))
    return problems


def _boot_ok(boot):
    return isinstance(boot, list) and len(boot) == 16 and all(isinstance(b, int) and 0 <= b <= 255 for b in boot)


def newest_first(rows):
    """True when the (t_ms, seq) order of rows of one producer never increases down the list."""
    last = {}
    for row in rows:
        key = (row.get("id"), row.get("port"))
        if key in last and row["seq"] > last[key]:
            return False
        last[key] = row["seq"]
    return True


# ---------------------------------------------------------------- reading rows

def split_tnc2(line):
    """(source, destination, [path], info) of `SRC>DST[,PATH...]:info`, or None."""
    head, sep, info = line.partition(":")
    src, gt, rest = head.partition(">")
    if not sep or not gt or not src:
        return None
    parts = rest.split(",")
    return src, parts[0], parts[1:], info


def row_src(row):
    text = row.get("line") if row.get("line") is not None else row.get("header")
    parsed = split_tnc2(text) if text is not None and ":" in text else None
    if parsed:
        return parsed[0]
    if text and ">" in text:
        return text.partition(">")[0]
    return None


def rows_from(rows, src, **match):
    """The rows whose source is `src`; extra keyword filters compare row keys (dir="rx", port="rf-a")."""
    return [r for r in rows if row_src(r) == src and all(r.get(k) == v for k, v in match.items())]


def worker_rows(rows):
    return [r for r in rows if producer_kind(r.get("id")) == "worker"]


def edge_rows(rows):
    return [r for r in rows if producer_kind(r.get("id")) == "edge"]


def outcomes(row):
    return [(o.get("stage"), o.get("result")) for o in row.get("out") or []]


def has_outcome(row, stage=None, result=None):
    return any((stage is None or s == stage) and (result is None or r == result) for s, r in outcomes(row))


def info_of(row):
    parsed = split_tnc2(row["line"]) if row.get("line") else None
    return None if parsed is None else parsed[3]


def mentions(rows, needle):
    """The rows whose JSON text contains `needle` (a marker payload, a coordinate string)."""
    return [r for r in rows if needle in json.dumps(r, sort_keys=True)]


# ---------------------------------------------------------------- the stream

def parse_stream(frames):
    """An SSE frame list (dvtest.web.WebClient.sse: {"event","data","retry"} dicts) -> SimpleNamespace(hello (producers), rows [(id, boot, row)],
    gaps [id], byes [data], resync, other [event names]). Data is JSON; a frame whose data is not JSON is listed in `bad`."""
    out = types.SimpleNamespace(hello=None, rows=[], gaps=[], byes=[], resync=False, other=[], bad=[])
    for frame in frames:
        event = frame.get("event", "message")
        try:
            data = json.loads(frame.get("data", "")) if frame.get("data") else None
        except ValueError:
            out.bad.append(frame)
            continue
        if event == "hello":
            out.hello = (data or {}).get("producers") if isinstance(data, dict) else None
        elif event == "rows" and isinstance(data, dict):
            for row in data.get("rows") or []:
                out.rows.append((data.get("id"), data.get("boot"), row))
        elif event == "gap" and isinstance(data, dict):
            out.gaps.append(data.get("id"))
        elif event == "bye":
            out.byes.append(data)
        elif event == "resync":
            out.resync = True
        else:
            out.other.append(event)
    return out


# ---------------------------------------------------------------- leaks

def new_marker(tag, size=6):
    """A unique payload marker, ASCII letters and digits only (`OBSPKT1X<tag><hex>`)."""
    return "OBSPKT1X%s%s" % (tag, secrets.token_hex(size // 2).upper())


def find_leaks(texts, markers):
    """{text name: [markers found in it]} for the texts (name -> str) that contain a marker. Empty = no leak."""
    out = {}
    for name, text in texts.items():
        hit = [m for m in markers if m in (text or "")]
        if hit:
            out[name] = hit
    return out


def stale_wait_s(login_age_s):
    """Seconds still to wait before a session logged in `login_age_s` ago has no step-up window any more (the login opens one)."""
    return max(0.0, STEP_UP_WINDOW_S + STEP_UP_MARGIN_S - login_age_s)


# ---------------------------------------------------------------- the frames the cases inject at rf-a

HIDDEN_BASE = "N0HID"
SRC_STATUS, SRC_NOGATE, SRC_QUERY = "N0OBA-1", "N0OBB-1", "N0OBC-1"
SRC_HIDDEN_POS, SRC_HIDDEN_MICE = "N0HID-7", "N0HID-9"
SRC_VISIBLE_POS, SRC_VISIBLE_MICE = "N0OBD-1", "N0OBE-1"
ACCOUNT = "N0OBH"                        # the web account whose hide_position the case switches on later
SRC_ACCOUNT = "N0OBH-5"
TOCALL = "APDVT1"
POS_HIDDEN = ("1137.77N", "02242.33E")   # unique coordinates: they must appear in no row of a hidden station
POS_VISIBLE = ("1030.42N", "02015.17E")
POS_ACCOUNT = ("0912.34N", "01945.67E")
POS_CLIENT = ("0915.55N", "01922.22E")


def position_info(coords, marker, table="/", code=">"):
    return "=%s%s%s%s%s" % (coords[0], table, coords[1], code, marker)


def mice_frame(src, lat, lon, marker):
    """A Mic-E UI frame: the destination encodes the latitude (aprs_cases.mice_destination), the information field the longitude, speed and
    course, then the marker text. Returns (frame bytes, destination string)."""
    dest = aprs_cases.mice_destination(lat, 0)
    info = aprs_cases.mice_info(lon, 12, 90, rest=marker.encode("ascii"))
    return kiss_tnc.ui_frame(src, dest, info, digis=("WIDE1-1*",)), dest


def rf_frame(src, info, digis=("WIDE1-1*",), tocall=TOCALL):
    return kiss_tnc.ui_frame(src, tocall, info, digis=digis)
