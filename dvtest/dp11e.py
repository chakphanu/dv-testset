# SPDX-License-Identifier: MIT
"""Pure helpers of the DP11e cases (cases/test_dp11e_kiss.py, cases/test_dp11e_config.py; docs/dp11e-cases.md): the KISS TCP server of the
AX.25 edge of DV-Router (`[[edges.ax25.kiss_server]]`, data.md 10.10, config.md). No network, no pytest.

  kiss_shape(doc)             the listeners and ports of a RENDERED config (ports, channels, tx, allow lists, keepalive)
  REFUSALS / refusal_frames   one client frame per documented refusal label, built with dvtest/ax25.py, in the order of data.md 10.10
  frame_counts / client_counts / clients_gauge   dv_edge_kiss_frames_total / dv_edge_kiss_clients_total / dv_edge_kiss_clients readers
  keepalive_problems          the lone-FEND keepalive of an idle client against the configured period
  CONFIG_CASES                mutations of the dp11e-kiss.toml snippet that `dv-router config check` must refuse (cfgload style)
"""
import ipaddress
import re
import types

from dvtest import ax25, kiss_tnc

STATION = "N0TEST-1"                    # [edges.ax25] callsign of the snippets
SRC = "N0TEST-7"                        # an allowed client source: an SSID of the station base
SRC2 = "N0TEST-8"
TOCALL = "APDR16"                       # what APRSdroid puts in the destination
PATH = ("WIDE1-1",)
POSITION = "=1030.00N/02015.00E>dp11e"


# ---------------------------------------------------------------- the rendered config

def _split_listen(text):
    host, _sep, port = text.rpartition(":")
    return host.strip("[]"), int(port)


def kiss_shape(doc):
    """{station, ports: {name: tx mode}, listeners: {name: SimpleNamespace}} of a rendered config dict, or None when the AX.25 edge has no
    kiss_server rows. A listener has host, port, loopback, allow_from, tx, tx_from, allow_src, max_clients, tx_per_min, tx_burst,
    max_hops, keepalive_secs (60 when omitted), channels {channel: port name}."""
    ax = ((doc.get("edges") or {}).get("ax25")) or {}
    rows = ax.get("kiss_server") or []
    if not ax.get("enable") or not rows:
        return None
    listeners = {}
    for row in rows:
        host, port = _split_listen(row["listen"])
        listeners[row["name"]] = types.SimpleNamespace(
            name=row["name"], host=host, port=port, loopback=ipaddress.ip_address(host).is_loopback,
            allow_from=list(row.get("allow_from") or []), tx=bool(row.get("tx", False)), tx_from=list(row.get("tx_from") or []),
            allow_src=list(row.get("allow_src") or []), max_clients=int(row.get("max_clients", 3)), tx_per_min=int(row.get("tx_per_min", 2)),
            tx_burst=int(row.get("tx_burst", 1)), max_hops=int(row.get("max_hops", 2)), keepalive_secs=int(row.get("keepalive_secs", 60)),
            channels={int(m.get("channel", 0)): m["name"] for m in row.get("ports") or []})
    return types.SimpleNamespace(station=ax.get("callsign", ""), ports={p["name"]: p.get("tx", False) for p in ax.get("port") or []},
                                 listeners=listeners)


def peer_in(cidrs, address):
    """True when `address` is inside one of the CIDR strings."""
    ip = ipaddress.ip_address(address)
    return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)


# ---------------------------------------------------------------- client frames, one per refusal label

def good_frame(info=POSITION, src=SRC, digis=PATH, tocall=TOCALL):
    """A UI/PID F0 frame as an APRS app sends it: `SRC>APDR16,WIDE1-1:<info>`."""
    return kiss_tnc.ui_frame(src, tocall, info, digis=digis)


def _raw_frame(info, src=SRC, tocall=TOCALL, digis=PATH, control=ax25.CONTROL_UI, pid=ax25.PID_NO_L3):
    path = tocall + "".join("," + d for d in digis)
    return ax25.from_tnc2("%s>%s:%s" % (src, path, info), control=control, pid=pid)


# Refusal: id, label (the dv_edge_kiss_frames_total{dir=in} result), listener, KISS channel, what to send, and a `kind`:
# "frame" (a KISS data frame carrying `payload`), "command" (a non-data KISS command byte `payload` = (command, value)).
Refusal = types.SimpleNamespace


def info_for(rid, length=None):
    """A position-like information field that carries the id of the refusal (so a frame seen at the modem can be told apart); `length`
    pads it to exactly that many bytes."""
    text = "=1030.00N/02015.00E>dp11e-%s" % rid
    if length is None:
        return text
    if len(text) > length:
        raise ValueError("id too long for %d bytes" % length)
    return text + "x" * (length - len(text))


def refusal_frames():
    """The table of client frames that must each be refused with its label, plus the control that must be queued. The listener names are
    those of rig/config/dv-router/dp11e-kiss.toml. Order inside a listener follows data.md 10.10: the label of the FIRST failing step."""
    r = []

    def add(rid, label, listener, channel, kind, payload, why):
        r.append(Refusal(id=rid, label=label, listener=listener, channel=channel, kind=kind, payload=payload, why=why))

    add("queued", "queued", "apps", 0, "frame", good_frame(info_for("queued")), "the control: every gate passes")
    add("tx_off_listener", "tx_off", "rxonly", 0, "frame", good_frame(info_for("tx_off_listener")), "step 1: the listener has tx = false")
    add("tx_addr", "tx_addr", "txdeny", 0, "frame", good_frame(info_for("tx_addr")), "step 1: the peer is not in tx_from")
    add("tx_off_port", "tx_off", "apps", 1, "frame", good_frame(info_for("tx_off_port")), "step 2: channel 1 maps rf-b, a receive-only port")
    add("channel_2", "channel", "apps", 2, "frame", good_frame(info_for("channel_2")), "step 2: channel 2 is not mapped")
    add("channel_3", "channel", "apps", 3, "frame", good_frame(info_for("channel_3")), "step 2: channel 3 (a PinPoint KISS TX port) is not mapped")
    add("bad_frame", "bad_frame", "apps", 0, "frame", b"\x01\x02\x03", "step 3: not an AX.25 frame")
    add("not_ui_control", "not_ui", "apps", 0, "frame", _raw_frame(info_for("not_ui_control"), control=0x2F), "step 3: control 0x2F (SABM), not UI 0x03")
    add("not_ui_pid", "not_ui", "apps", 0, "frame", _raw_frame(info_for("not_ui_pid"), pid=0xCC), "step 3: PID 0xCC, not 0xF0")
    add("src_unlisted", "src", "apps", 0, "frame", good_frame(info_for("src_unlisted"), src="N0TEST-9"), "step 4: an SSID of the station base that is not in allow_src")
    add("src_other_base", "src", "apps", 0, "frame", good_frame(info_for("src_other_base"), src="N0CALL-7"), "step 4: another base callsign")
    add("path_h_bit", "path", "apps", 0, "frame", good_frame(info_for("path_h_bit"), digis=("WIDE1-1*",)), "step 5: a used H bit")
    add("path_tcpip", "path", "apps", 0, "frame", good_frame(info_for("path_tcpip"), digis=("TCPIP",)), "step 5: TCPIP marker")
    add("path_qar", "path", "apps", 0, "frame", good_frame(info_for("path_qar"), digis=("QAR", "N0TEST-1")), "step 5: qA* marker (an AX.25 address is upper case: QAR)")
    add("path_hops", "path", "apps", 0, "frame", good_frame(info_for("path_hops"), digis=("WIDE2-2", "WIDE3-1")), "step 5: New-n hops 3 > max_hops 2")
    add("too_long_257", "too_long", "apps", 0, "frame", good_frame(info_for("too_long_257", 257)), "step 6: information field of 257 bytes")
    add("too_long_empty", "too_long", "apps", 0, "frame", good_frame(info=""), "step 6: empty information field")
    add("third_party", "third_party", "apps", 0, "frame", good_frame("}N0TEST-9>APRS,TCPIP*:>dp11e-third_party"), "step 6: a leading } (third party)")
    add("command_txdelay", "command", "apps", 0, "command", (1, b"\x1e"), "a non-data command (TXDELAY) is ignored and counted")
    add("command_return", "command", "apps", 15, "command", (15, b""), "the Return command 0xFF is ignored and counted")
    return r


# ---------------------------------------------------------------- counters

def frame_counts(samples, port):
    """{(dir, result): total} of dv_edge_kiss_frames_total{port=port}."""
    out = {}
    for s in samples:
        if s.name == "dv_edge_kiss_frames_total" and s.labels.get("port") == port:
            key = (s.labels.get("dir", ""), s.labels.get("result", ""))
            out[key] = out.get(key, 0) + int(s.value)
    return out


def client_counts(samples, port):
    """{result: total} of dv_edge_kiss_clients_total{port=port} (accepted, refused_addr, full, closed, stalled, kicked)."""
    out = {}
    for s in samples:
        if s.name == "dv_edge_kiss_clients_total" and s.labels.get("port") == port:
            out[s.labels.get("result", "")] = out.get(s.labels.get("result", ""), 0) + int(s.value)
    return out


def clients_gauge(samples, port):
    """The gauge dv_edge_kiss_clients{port=port} (None when it has no series)."""
    vals = [s.value for s in samples if s.name == "dv_edge_kiss_clients" and s.labels.get("port") == port]
    return None if not vals else int(vals[-1])


def delta(after, before):
    """{key: after - before} for the keys that moved."""
    return {k: after.get(k, 0) - before.get(k, 0) for k in set(after) | set(before) if after.get(k, 0) != before.get(k, 0)}


def expected_in_deltas(refusals):
    """{listener: {result: n}} the counters must show after every refusal in the list was sent once."""
    out = {}
    for r in refusals:
        out.setdefault(r.listener, {}).setdefault(r.label, 0)
        out[r.listener][r.label] += 1
    return out


# ---------------------------------------------------------------- the keepalive

def keepalive_problems(times, period_s, start, end, tolerance=0.3, min_count=2):
    """Problems ([] = fine) of the lone FEND times (time.monotonic) an idle client saw in [start, end] with `keepalive_secs` = period_s:
    at least min_count of them, each interval within +-tolerance of the period (the first one is counted from `start` only when it is the
    first FEND after the connect, so it may be shorter: it is checked against the period as an upper bound only)."""
    got = [t for t in times if start <= t <= end]
    problems = []
    if len(got) < min_count:
        problems.append("%d lone FEND(s) in %.0f s with keepalive_secs %d, expected at least %d" % (len(got), end - start, period_s, min_count))
    gaps = [b - a for a, b in zip(got, got[1:])]
    lo, hi = period_s * (1 - tolerance), period_s * (1 + tolerance)
    bad = [round(g, 2) for g in gaps if not lo <= g <= hi]
    if bad:
        problems.append("keepalive intervals %s outside %.1f..%.1f s (period %d s)" % (bad, lo, hi, period_s))
    if got and got[0] - start > period_s * (1 + tolerance):
        problems.append("the first lone FEND came %.1f s after the connect, later than %.1f s" % (got[0] - start, period_s * (1 + tolerance)))
    return problems


# ---------------------------------------------------------------- configuration refusals (cfgload style)

CONFIG_CASES = []


def _case(cid, what, key, mutate, hint=None, verdict="reject"):
    CONFIG_CASES.append(types.SimpleNamespace(id=cid, what=what, key=key, mutate=mutate, hint=hint, verdict=verdict))


def _sub(old, new, count=1):
    def f(text):
        if old not in text:
            raise ValueError("anchor not in the snippet: %r" % old)
        return text.replace(old, new, count)
    return f


_case("accept-baseline", "the snippet as shipped loads", None, lambda t: t, verdict="accept")
_case("listen-required", "a listener without `listen` (no default port)", "listen", _sub('listen = "<OWN_IP>:8201"\n', ""))
_case("nonloopback-needs-allow-from", "a non-loopback listen with an empty allow_from", "allow_from",
      _sub('allow_from = ["<TESTER_IP>/32"]\nports = [{ name = "rf-a", channel = 0 }, { name = "rf-b", channel = 1 }]',
           'allow_from = []\nports = [{ name = "rf-a", channel = 0 }, { name = "rf-b", channel = 1 }]'))
_case("duplicate-listen", "two listeners on one address", "listen", _sub('listen = "<OWN_IP>:8202"', 'listen = "<OWN_IP>:8201"'))
_case("fifth-listener", "five listeners (the limit is four)", "kiss_server", lambda t: t + '''
[[edges.ax25.kiss_server]]
name = "fifth"
listen = "<OWN_IP>:8209"
allow_from = ["<TESTER_IP>/32"]
ports = [{ name = "rf-a", channel = 0 }]
''')
_case("keepalive-301", "keepalive_secs above 300", "keepalive_secs", _sub("keepalive_secs = 10", "keepalive_secs = 301"))
_case("channel-0-unmapped", "no mapping uses channel 0 (the default port)", "ports",
      _sub('{ name = "rf-a", channel = 0 }, { name = "rf-b", channel = 1 }', '{ name = "rf-a", channel = 2 }, { name = "rf-b", channel = 1 }'), hint="channel 0")
_case("duplicate-channel", "two ports on one channel", "ports", _sub('{ name = "rf-b", channel = 1 }', '{ name = "rf-b", channel = 0 }'))
_case("unknown-port", "a mapping names a port that does not exist", "ports", _sub('{ name = "rf-b", channel = 1 }', '{ name = "nope", channel = 1 }'))
_case("tx-without-allow-src", "tx = true with an empty allow_src", "allow_src", _sub('allow_src = ["N0TEST-7", "N0TEST-8"]', "allow_src = []"))
_case("allow-src-other-base", "an allow_src whose base is not the station base", "allow_src", _sub('allow_src = ["N0TEST-7", "N0TEST-8"]', 'allow_src = ["N0CALL-7"]'))
_case("tx-without-tx-port", "tx = true but the only mapped port is receive only", "tx",
      _sub('ports = [{ name = "rf-a", channel = 0 }, { name = "rf-b", channel = 1 }]\ntx = true', 'ports = [{ name = "rf-b", channel = 0 }]\ntx = true'))
_case("bad-name", "an upper case listener name", "name", _sub('name = "apps"', 'name = "Apps"'))
_case("max-clients-9", "max_clients above 8", "max_clients", _sub("max_clients = 3", "max_clients = 9"))
_case("max-hops-4", "max_hops above 3", "max_hops", _sub("max_hops = 2", "max_hops = 4"))
_case("tx-burst-31", "tx_burst above 30", "tx_burst", _sub("tx_burst = 5", "tx_burst = 31"))
_case("accept-loopback", "a loopback listen needs no allow_from", None, lambda t: t + '''
[[edges.ax25.kiss_server]]
name = "local"
listen = "127.0.0.1:8210"
ports = [{ name = "rf-a", channel = 0 }]
''', verdict="accept")


def config_problems(case, verdict, exit_code):
    """Problems ([] = as expected) of a `dv-router config check` verdict (dvtest.tools.config_check.judge_check) for a CONFIG_CASES entry:
    reject = a non-zero exit status, a positioned error whose key names the offending field (and whose message holds the hint when the
    document states it); accept = exit status 0."""
    if verdict.get("unsupported"):
        return ["the image has no `config check` (usage text printed)"]
    if case.verdict == "accept":
        return [] if exit_code == 0 and verdict.get("valid") else ["expected to load, got exit status %r: %s" % (exit_code, "; ".join(verdict.get("errors") or ["no error line"]))]
    if exit_code == 0 or verdict.get("valid"):
        return ["SILENT SUCCESS: %s loaded (exit status 0); expected an error naming %s" % (case.what, case.key)]
    parsed = verdict.get("parsed") or []
    if not parsed:
        return ["no `FILE:LINE:COL: message` error line (exit status %r only)" % (exit_code,)]
    first = parsed[0]
    problems = []
    if "kiss_server" not in first["key"] and "kiss_server" not in first["message"]:
        problems.append("the error key %r does not name kiss_server" % (first["key"],))
    if case.key and case.key != "kiss_server" and case.key not in first["key"] and case.key not in first["message"]:
        problems.append("the error names %r / %r, expected the field %r" % (first["key"], first["message"], case.key))
    if case.hint and case.hint not in first["message"]:
        problems.append("the message does not mention %r: %s" % (case.hint, first["message"]))
    if not first["message"].strip():
        problems.append("the error message is empty")
    return problems
