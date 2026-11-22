# SPDX-License-Identifier: MIT
"""What a DV-Router image with AX25ACK1 and TXOFF2 lets a black-box case assert (DV-Router 345f1ebe, merged in bc9af2b1; docs/design/config.md "tx_ack" and
"RF TX gates", radio.md 10.1, openapi-admin `POST /api/v1/admin/radio/tx`; image dv-router:wip-f1f3777).

Switch: DVT_AX25ACK1=on (the same switch makes dvtest/sut_config.py render the line, docs/rig.md "AX.25 tx_ack").

AX25ACK1: `[edges.ax25] tx_ack` must equal "I am the control operator of <CALLSIGN> and accept responsibility for its transmissions" (callsign upper case, SSID
included). Absent or empty: every port is RX only (`effective_tx` is Off), `config check` WARNS "no control-operator acknowledgement: RX only", the start line reads
`RF TX off (no control-operator acknowledgement: RX only)`. A nonempty different sentence is an ERROR ("RF TX acknowledgement must equal: <sentence>") even with no
transmitting port. The admin status carries `ax25: {callsign, tx_enable, acknowledged}` in the AX.25 process row.

TXOFF2: the effective transmit predicate is one function for every RF producer (master `tx_enable` and `tx_ack` and the port's `tx`); a digipeater, a beacon, KISS client TX,
an RF data path and an APRS-IS RF copy no longer need TX intent to LOAD (a port TX off keeps them dormant, with a warning). `POST /api/v1/admin/radio/tx`
{kind: "radio"|"ax25", port?: 0..15, on: bool, ack?: str}: Off needs operator or admin, no step-up; On needs admin, step-up and the exact `ack`; no client ETag; it edits the
current file under the config writer lock. Off alone REBASES on the hash-verified accepted running source when the current file or the candidate is invalid (the replaced file
is kept in history); the reply carries `rebased` and `history_seq`. Pure and stdlib only (cases/unit/test_ax25ack1_lib.py).
"""
import re

from dvtest import sut_config

strict_on = sut_config.ax25_ack_on
ack_sentence = sut_config.tx_ack_sentence
NO_ACK_WARNING = "no control-operator acknowledgement: RX only"
WRONG_ACK_ERROR = "RF TX acknowledgement must equal"
START_LINE = re.compile(r"port (?P<port>\S+) RF TX (?P<state>enabled|simulate|off(?: \((?P<why>[^)]*)\))?)(?:;| callsign)")
OFF_REASONS = ("master off", "port off", "no control-operator acknowledgement: RX only", "edge disabled")


def radio_tx_body(on, port=None, ack=None, kind="ax25"):
    """The body of POST /api/v1/admin/radio/tx (openapi RadioTxRequest: kind, port 0..15 (absent: the master gate), on, ack)."""
    if kind not in ("radio", "ax25"):
        raise ValueError("kind is radio or ax25")
    body = {"kind": kind, "on": bool(on)}
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 15:
            raise ValueError("port is 0..15")
        body["port"] = port
    if ack is not None:
        body["ack"] = ack
    return body


def ax25_row(status):
    """The AX.25 process row ({proc/kind ax25, ports, ax25}) of a parsed GET /api/v1/admin/status `procs`, else None."""
    for row in (status.get("procs") if isinstance(status, dict) else None) or []:
        if isinstance(row, dict) and (row.get("kind") == "ax25" or row.get("proc") == "ax25"):
            return row
    return None


def ax25_info(status):
    """{callsign, tx_enable, acknowledged} of the AX.25 row, else None (an image without AX25ACK1 has no `ax25` object)."""
    row = ax25_row(status)
    info = row.get("ax25") if row else None
    return info if isinstance(info, dict) else None


def port_tx(status, port):
    """The `tx` label (off, on or simulate) of AX.25 port `port` in the status row, else None."""
    row = ax25_row(status)
    for item in (row or {}).get("ports") or []:
        if isinstance(item, dict) and item.get("name") == port:
            return item.get("tx")
    return None


def config_ack_problems(doc):
    """Problems ([] = fine) of a rendered or fetched config dict as to `[edges.ax25] tx_ack`: with a callsign it must be exact or absent; with a transmitting master it must be exact."""
    ax = ((doc.get("edges") or {}).get("ax25")) if isinstance(doc, dict) else None
    if not isinstance(ax, dict):
        return ["no [edges.ax25] table"]
    call, ack = str(ax.get("callsign", "")), ax.get("tx_ack", "")
    want = ack_sentence(call) if call else None
    if ack and ack != want:
        return ["tx_ack is %r, want %r" % (ack, want)]
    if ax.get("tx_enable") is True and ack != want:
        return ["tx_enable is true but tx_ack is absent: transmit is dormant (RX only)"]
    return []


def diagnostics(reply_json):
    """[(level, text)] of the errors and warnings of a config reply or a 422 body (any of `errors`, `warnings`, `diagnostics` lists of {path?, msg|message}); tolerant of shape."""
    out = []
    if not isinstance(reply_json, dict):
        return out
    for level in ("errors", "warnings", "diagnostics"):
        for item in reply_json.get(level) or []:
            if isinstance(item, dict):
                out.append((level, "%s: %s" % (item.get("path") or item.get("key") or "", item.get("msg") or item.get("message") or "")))
            elif isinstance(item, str):
                out.append((level, item))
    return out


def start_lines(log_text):
    """{port: (state, why)} of the LAST `dv-edge-ax25: port <name> RF TX <state>` start line per port in a SUT log text (state enabled, simulate or off; why is the reason after off)."""
    out = {}
    for m in START_LINE.finditer(log_text):
        out[m.group("port")] = ("off" if m.group("state").startswith("off") else m.group("state"), m.group("why"))
    return out


def start_line_problems(log_text, port, want_state, want_why=None):
    got = start_lines(log_text).get(port)
    if got is None:
        return ["no `RF TX` start line for port %s in the log" % port]
    if got[0] != want_state or (want_why is not None and got[1] != want_why):
        return ["port %s start line says %s (%s), want %s (%s)" % (port, got[0], got[1], want_state, want_why)]
    return []


def rebase_problems(status_code, body):
    """Problems of the reply to an Off request on an INVALID file: 200 with rebased true and an integer history_seq >= 1 (the replaced file kept in history)."""
    if status_code != 200:
        return ["HTTP %s, want 200 (an Off request rebases on the accepted running source when the file is invalid)" % status_code]
    problems = []
    if not isinstance(body, dict) or body.get("rebased") is not True:
        problems.append("`rebased` is %r, want true" % (body.get("rebased") if isinstance(body, dict) else None))
    seq = body.get("history_seq") if isinstance(body, dict) else None
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        problems.append("`history_seq` is %r, want an integer >= 1" % (seq,))
    return problems
