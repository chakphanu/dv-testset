# SPDX-License-Identifier: MIT
"""Pure helpers of the DP12c / DP18 cases (cases/test_dp18_origin.py, cases/test_dp18_rf.py; docs/dp18-cases.md): the node beacon and the
node objects DV-Router originates to APRS-IS (data.md 9.5), the RF copies of the objects, and the per-port TX mode `simulate`
(data.md 10.x, "Built (DP12c)"). Expectations are derived from the RENDERED config of the run and built with dvtest/proto/aprs.py,
never from DV-Router's encoder. Unit tests: cases/unit/test_dp18_support.py. No network, no pytest."""
import time
import types

from dvtest.proto import aprs

STAGGER_S = 35.0                      # first due = interval + 35 s per earlier configured report (data.md 9.5)
DEFAULT_INTERVAL_S = 1200             # the documented default; the rig snippets use the 600 s floor
OBJECT_COMMENT_MAX = 43               # frequency + room + comment, data.md 9.5
HMS_TOLERANCE_S = 1.0                 # the object timestamp equals the send time within 1 s (HMS has whole seconds)
TRANSIT_ALLOWANCE_S = 0.5             # read at the aprsc after the line was written: link and server delay
ARRIVAL_WINDOW_S = (STAGGER_S - 1.0, STAGGER_S + 5.0)    # the object after the beacon at the aprsc (worker tick, link)


def base_call(call):
    """`N0TEST-5` -> `N0TEST` (the base callsign the position policy matches)."""
    return call.split("-", 1)[0].upper()


# ---------------------------------------------------------------- the rendered config

def _report(kind, row, defaults, index):
    return types.SimpleNamespace(
        kind=kind, index=index, name=row.get("name", ""), room=row.get("room"), frequency=row.get("frequency", ""),
        comment=row.get("comment", ""), lat_e7=int(row.get("lat_e7", 0)), lon_e7=int(row.get("lon_e7", 0)),
        symbol=row.get("symbol", defaults["symbol"]), ambiguity=int(row.get("ambiguity", 0)),
        interval_s=int(row.get("interval_secs", DEFAULT_INTERVAL_S)), rf_ports=list(row.get("rf_ports") or []))


def origin_shape(doc):
    """What the DP18 cases need from a rendered config dict ({} when unreadable): the APRS-IS edge (gateway call, tocall), the enabled
    reports in schedule order (beacon first, then the objects in configured order) with their first-due offset, and the position
    policy. None when the APRS-IS edge is off or no report is configured."""
    edges = doc.get("edges") or {}
    aprsis = edges.get("aprsis") if isinstance(edges.get("aprsis"), dict) else {}
    if not aprsis.get("enable"):
        return None
    reports = []
    beacon = aprsis.get("beacon")
    if isinstance(beacon, dict) and beacon.get("enable"):
        reports.append(_report("beacon", beacon, {"symbol": "/&"}, len(reports)))
    for row in aprsis.get("objects") or []:
        reports.append(_report("object", row, {"symbol": "/r"}, len(reports)))
    if not reports:
        return None
    for report in reports:
        report.first_due_s = report.interval_s + STAGGER_S * report.index
    data = doc.get("data") or {}
    optout = {base_call(c) for c in data.get("position_optout") or []}
    gateway = aprsis.get("callsign", "")
    withheld = data.get("positions", True) is False or base_call(gateway) in optout
    return types.SimpleNamespace(gateway=gateway, tocall=aprsis.get("tocall", "APZDVR"), reports=reports, withheld=withheld,
                                 beacon=next((r for r in reports if r.kind == "beacon"), None),
                                 objects=[r for r in reports if r.kind == "object"])


def object_comment(report):
    """The object comment as DV-Router renders it: frequency first, then `room=B`, then the comment, omitting empty parts, one
    space between them (data.md 9.5)."""
    parts = [report.frequency, "room=%s" % report.room if report.room else "", report.comment]
    return " ".join(p for p in parts if p)


def expected_info(report, hms=None):
    """The information field the report must have; `hms` (HHMMSS) is required for an object."""
    if report.kind == "beacon":
        return aprs.position_report(report.lat_e7, report.lon_e7, report.symbol, report.comment, report.ambiguity)
    if hms is None:
        raise ValueError("an object needs its timestamp")
    return aprs.object_report(report.name, hms, report.lat_e7, report.lon_e7, report.symbol, object_comment(report), report.ambiguity)


def info_without_stamp(info):
    """`info` with the six HMS digits of an object replaced by `HHMMSS` (a beacon is returned unchanged), for a byte compare of
    everything but the send time."""
    if info.startswith(";") and len(info) > 17 and info[11] in "*_":
        return info[:12] + "HHMMSS" + info[18:]
    return info


# ---------------------------------------------------------------- lines read at the private aprsc

def classify(parsed, shape):
    """The report ('beacon' or 'object:<name>') a parsed APRS-IS line (dvtest/aprsis_client.parse_line) is from the node, else None."""
    if parsed["src"] != shape.gateway:
        return None
    info = parsed["info"]
    if info.startswith("!"):
        return "beacon"
    if info.startswith(";"):
        try:
            return "object:" + aprs.parse_object(info)["name"]
        except ValueError:
            return None
    return None


def check_origin_line(parsed, report, shape, hms_text=None):
    """Problems of one line at the aprsc against its report ([] when it is the node's line): source, destination, the path before the
    server's q construct exactly `TCPIP*` (no client q construct), a q construct that starts with `qAC` (a verified login that sent
    its own packet), and the information field byte for byte (an object's HMS is checked separately)."""
    problems = []
    if parsed["src"] != shape.gateway:
        problems.append("source %r, expected the gateway call %r" % (parsed["src"], shape.gateway))
    if parsed["dest"] != shape.tocall:
        problems.append("destination %r, expected %r" % (parsed["dest"], shape.tocall))
    if parsed["before_q"] != ["TCPIP*"]:
        problems.append("path before the q construct %r, expected exactly ['TCPIP*']" % (parsed["before_q"],))
    q_items = [i for i in parsed["path"] if len(i) == 3 and i.startswith("q") and i[1:].isalpha()]
    if len(q_items) != 1:
        problems.append("%d q constructs in the path %r" % (len(q_items), parsed["path"]))
    if not parsed["q"] or parsed["q"][0] != "qAC":
        problems.append("q construct %r, expected it to start with 'qAC'" % (parsed["q"],))
    info = parsed["info"]
    if report.kind == "object":
        try:
            hms_text = aprs.parse_object(info)["hms"]
        except ValueError:
            problems.append("information field %r is not an object with an HMS timestamp" % (info,))
            return problems
    want = expected_info(report, hms_text)
    if info != want:
        problems.append("information field %r, expected %r" % (info, want))
    return problems


def hms_to_seconds(hms):
    return int(hms[0:2]) * 3600 + int(hms[2:4]) * 60 + int(hms[4:6])


def hms_offset_s(hms, wall_epoch):
    """Signed seconds from the stamp to the wall clock (UTC): wall - stamp, normalised across midnight to (-43200, 43200]."""
    tm = time.gmtime(wall_epoch)
    wall = tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec + (wall_epoch - int(wall_epoch))
    diff = (wall - hms_to_seconds(hms)) % 86400
    return diff - 86400 if diff > 43200 else diff


def hms_ok(hms, wall_epoch):
    """True when the stamp equals the time the line was read within HMS_TOLERANCE_S (the stamp has whole seconds and is taken when
    the report is generated, so the read time is later by 0..1 s plus the transit allowance)."""
    off = hms_offset_s(hms, wall_epoch)
    return -TRANSIT_ALLOWANCE_S <= off <= HMS_TOLERANCE_S + TRANSIT_ALLOWANCE_S


def wall_of(monotonic_at, now_wall=None, now_mono=None):
    """The wall clock (epoch seconds) of a time.monotonic() stamp."""
    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    return now_wall - (now_mono - monotonic_at)


def quiet_window_s(interval_s, margin_s=15.0):
    """How long nothing may arrive after a worker kill: one full interval minus a margin (the restarted worker waits a full interval)."""
    return max(0.0, interval_s - margin_s)


# ---------------------------------------------------------------- counters

def origin_counts(samples):
    """{(kind, result): total} of dv_pkt_origin_total (data.md 13.1: kind beacon|object, result generated|withheld|no_edge|no_rf_edge)."""
    counts = {}
    for s in samples:
        if s.name == "dv_pkt_origin_total":
            key = (s.labels.get("kind", ""), s.labels.get("result", ""))
            counts[key] = counts.get(key, 0) + int(s.value)
    return counts


def rf_tx_counts(samples, port):
    """{(kind, result): total} of dv_edge_rf_tx_total{port=port} (kind = scheduler class, result sent|simulated|dropped_*...)."""
    counts = {}
    for s in samples:
        if s.name == "dv_edge_rf_tx_total" and s.labels.get("port") == port:
            key = (s.labels.get("kind", ""), s.labels.get("result", ""))
            counts[key] = counts.get(key, 0) + int(s.value)
    return counts


def result_total(counts, result):
    """Sum over every kind of one result."""
    return sum(n for (_kind, r), n in counts.items() if r == result)


def port_family_total(samples, family, port):
    """Sum of one counter family for one port label (dv_edge_modem_total{result=tx}, dv_edge_rf_ptt_total, ...), and whether any
    series of the family has that port."""
    hits = [s for s in samples if s.name == family and s.labels.get("port") == port]
    return sum(int(s.value) for s in hits), bool(hits)


DROP_RESULTS = ("dropped_budget", "dropped_ttl", "dropped_queue", "dropped_quota", "dropped_exceeds_quota", "dropped_too_long", "dropped_gap")


def drops(counts):
    """{result: n} for the drop results with a nonzero count."""
    return {r: n for (_k, r), n in counts.items() if r in DROP_RESULTS and n}


# ---------------------------------------------------------------- RF frames at a fake TNC

def object_frames(tnc_frames, ax25_mod):
    """[(arrival time, object name, source call, info)] of the object frames among the KISS data frames of a fake TNC
    (FakeKissTnc.data_frames(): (time, channel, frame bytes)); other frames are ignored."""
    out = []
    for at, _channel, frame in tnc_frames:
        try:
            parsed = ax25_mod.parse_frame(frame)
            info = parsed["info"].decode("latin-1")
            obj = aprs.parse_object(info)
        except (ValueError, KeyError):
            continue
        out.append((at, obj["name"], parsed["src"]["text"], info))
    return out


def spread_gaps(times):
    """The gaps in seconds between consecutive times (sorted)."""
    times = sorted(times)
    return [round(b - a, 3) for a, b in zip(times, times[1:])]


# ---------------------------------------------------------------- the TX mode and the admin config API

def port_index(doc, name):
    """Index of the [[edges.ax25.port]] row called `name` in the rendered config, else None (a JSON Pointer needs the index)."""
    ports = (((doc.get("edges") or {}).get("ax25") or {}).get("port")) or []
    return next((i for i, p in enumerate(ports) if p.get("name") == name), None)


def tx_mode(doc, name):
    """The configured `tx` of a port: False, True or "simulate" (None when the port is not there)."""
    i = port_index(doc, name)
    return None if i is None else doc["edges"]["ax25"]["port"][i].get("tx", False)


def tx_change_request(index, value, base_etag, comment):
    """The body of POST /api/v1/admin/config/changes that replaces `tx` of AX.25 port number `index` (admin.md, openapi ChangesRequest:
    base_etag, ops [{op, path, value}], mode, comment, accept_pending_restart). The port's tx is restart class: the launcher
    restarts the AX.25 edge, so accept_pending_restart stays false unless a response asks for it."""
    if value not in (True, False, "simulate"):
        raise ValueError("tx is true, false or \"simulate\": %r" % (value,))
    return {"base_etag": base_etag, "mode": "apply", "comment": comment,
            "ops": [{"op": "replace", "path": "/edges/ax25/port/%d/tx" % index, "value": value}]}


def etag_of(config_response):
    """The file ETag of a GET /api/v1/admin/config reply: the body's meta.etag, else the ETag header. `config_response` has .json()
    and optionally .headers (a dict); None when neither is there."""
    try:
        body = config_response.json()
        etag = ((body or {}).get("meta") or {}).get("etag")
        if etag:
            return etag
    except Exception:                                               # noqa: BLE001  an odd body is "no etag", not a crash
        pass
    headers = getattr(config_response, "headers", None) or {}
    for key, value in headers.items():
        if key.lower() == "etag":
            return value
    return None


def history_seqs(body):
    """The sequence numbers in a GET /api/v1/admin/config/history body, newest last, tolerant of the unspecified envelope (a list, or
    an object with `items`/`history`/`entries`; each entry an object with `seq` or a bare number)."""
    items = body
    if isinstance(body, dict):
        for key in ("items", "history", "entries"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
    seqs = []
    for item in items if isinstance(items, list) else []:
        seq = item.get("seq") if isinstance(item, dict) else item
        if isinstance(seq, int) and not isinstance(seq, bool):
            seqs.append(seq)
    return sorted(seqs)


def child_state(status_body, name):
    """{pid, restarts, state, since_ms} of the launcher child `name` in GET /api/v1/admin/status `launcher.children` (a single `name[<instance>]` entry when there is no exact
    one), else None. `since_ms` is the Unix milliseconds of the CURRENT child start (launcher shell.rs:223), 0 only if it never started."""
    launcher = status_body.get("launcher") if isinstance(status_body, dict) else None
    children = [c for c in (launcher or {}).get("children") or [] if isinstance(c, dict)]
    exact = [c for c in children if c.get("name") == name]
    named = [c for c in children if str(c.get("name", "")).startswith(name + "[")]          # dv-edge-ax25[<name>]: a named instance of the same edge
    for child in exact or (named if len(named) == 1 else []):
        return {k: child.get(k) for k in ("pid", "restarts", "state", "since_ms")}
    return None


def restarted(before, after):
    """True when the child was restarted between two child_state results: the restarts counter grew or the pid changed."""
    if not before or not after:
        return False
    return (after.get("restarts") or 0) > (before.get("restarts") or 0) or (after.get("pid") != before.get("pid") and after.get("pid") is not None)


def schema_has_dp18(schema_text):
    """True when the served configuration schema (GET /api/v1/admin/schema) knows the DP12c/DP18 keys: the `simulate` tx value and
    the beacon and objects tables. A capability probe, independent of the image tag."""
    return "simulate" in schema_text and '"beacon"' in schema_text and '"objects"' in schema_text
