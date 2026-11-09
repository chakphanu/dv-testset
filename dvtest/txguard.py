# SPDX-License-Identifier: MIT
"""Wait for the RF transmit guard of DV-Router (RFSAFE1 + RIF4b, radio.md 10.4 rule 4): after a start, a reload and a recovery
the edge holds TX on a radio port or an AX.25 port for about 13 s (G = detection 2.5 s + the 10 s amateur PTT cap + 0.5 s margin).
A case that expects a transmission waits for the fence to end first; there is no fixed sleep, and an image from before RFSAFE1
(no fence field) is not waited for.

Where the fence is reported (two places, spec sections cited because the rig could not confirm the paths yet):

  radio port  public GET /api/v1/public/state -> procs[] (only radio-edge processes with radio information; web.md "Public state
              adds optional procs", about lines 1448-1460) -> row["radio"]["ports"][] with name == port (radio.md 12.3, status.md
              about line 321): `tx_state` (rx-only, idle, fenced, keyed, sending, hang, ident, lockout; `fenced` replaces `idle` while
              `fence` is set), `fence` (startup_guard, session_loss, foreign_holder, recovery_probe or null), `guard_ms` (int or null
              while the end is not known). Wait until tx_state != "fenced". The public SSE type radio_port is live only and not used.
  AX.25 port  admin GET /api/v1/admin/clients (operator and up, step-up window; admin.md about line 416) -> items[] with kind
              "modem" -> the `modem` object of the port (status.md "Built (DP11)", data.md about line 2004): `tx_fence`
              (startup_guard, session_loss, foreign_holder, recovery_probe) and `tx_guard_ms`, present only while the port is fenced and
              omitted when null. Wait until tx_fence is absent. An absent field cannot tell "not fenced" from "image without RFSAFE1";
              both return at once and the record says so.

The AX.25 walk is deliberately tolerant (the exact nesting of the modem row was not seen): it looks at items[].modem first and then
at any object whose `name` is the port. Every first call records the keys met on the way down, so a run shows the real path.

`wait_unfenced(read, ...)` is pure: `read()` returns an Observation, the clock and sleep are injectable (cases/unit/test_txguard.py).
`radio_reader` and `ax25_reader` build `read` from an HTTP client (dvtest/web.py WebClient / dvtest/admin.py AdminClient).
"""
import time
from collections import namedtuple

STARTUP_GUARD_S = 13.0              # radio.md 10.4 rule 3: G = D 2.5 s + max(B) 10 s + 0.5 s
MARGIN_S = 2.0                      # slack on top of the guard (poll interval, publish delay of the status record)
MAX_WAIT_S = 20.0                   # never wait longer than this for one fence
POLL_S = 0.5
PUBLIC_STATE_PATH = "/api/v1/public/state"
ADMIN_CLIENTS_SUFFIX = "/clients"   # AdminClient.path() puts the mount and /api/v1/admin in front

# found: the port row exists in the reply; present: the fence field exists in it; fenced: the port is fenced now
Observation = namedtuple("Observation", "found present fenced fence guard_ms keys")
Result = namedtuple("Result", "state waited_ms fence guard_ms keys polls")
# state: unfenced (was fenced, ended) | clear (never fenced when first read) | absent (no field: image before RFSAFE1 or an AX.25
# port that is not fenced) | not_found (no port row within the bound) | timeout (still fenced at the bound) | no_reader


def _first(items, pred):
    for item in items or []:
        if isinstance(item, dict) and pred(item):
            return item
    return None


def radio_observation(state, port):
    """The Observation of radio port `port` in a public state body (see the module docstring for the path)."""
    keys = []
    if not isinstance(state, dict):
        return Observation(False, False, False, None, None, ["<not an object>"])
    keys.append("state:" + ",".join(sorted(state)[:12]))
    procs = state.get("procs")
    if not isinstance(procs, list):
        return Observation(False, False, False, None, None, keys + ["procs: absent"])
    keys.append("procs[%d]" % len(procs))
    for proc in procs:
        radio = proc.get("radio") if isinstance(proc, dict) else None
        if not isinstance(radio, dict):
            continue
        keys.append("procs[].radio:" + ",".join(sorted(radio)[:12]))
        row = _first(radio.get("ports"), lambda p: p.get("name") == port)
        if row is None:
            continue
        keys.append("ports[%s]:" % port + ",".join(sorted(row)))
        present = "tx_state" in row
        return Observation(True, present, row.get("tx_state") == "fenced", row.get("fence"), row.get("guard_ms"), keys)
    return Observation(False, False, False, None, None, keys + ["port %s: not in any procs[].radio.ports[]" % port])


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def ax25_observation(body, port):
    """The Observation of AX.25 port `port` in an admin clients body: items[].modem first, then any object named `port`."""
    keys = []
    if not isinstance(body, dict):
        return Observation(False, False, False, None, None, ["<not an object>"])
    keys.append("body:" + ",".join(sorted(body)[:12]))
    items = body.get("items")
    keys.append("items[%d]" % len(items) if isinstance(items, list) else "items: absent")
    row = None
    for item in items if isinstance(items, list) else []:
        modem = item.get("modem") if isinstance(item, dict) else None
        if isinstance(modem, dict):
            keys.append("items[].modem:" + ",".join(sorted(modem)[:16]))
            if modem.get("name") == port:
                row = modem
                break
            inner = _first(modem.get("ports"), lambda p: p.get("name") == port)
            if inner is not None:
                keys.append("items[].modem.ports[]:" + ",".join(sorted(inner)))
                row = inner
                break
    if row is None:
        row = _first(list(_walk(body)), lambda d: d.get("name") == port and ("adapter" in d or "connected" in d or "tx_fence" in d))
        if row is not None:
            keys.append("object named %s:" % port + ",".join(sorted(row)))
    if row is None:
        return Observation(False, False, False, None, None, keys + ["port %s: no modem row" % port])
    present = "tx_fence" in row
    return Observation(True, present, present and row.get("tx_fence") is not None, row.get("tx_fence"), row.get("tx_guard_ms"), keys)


def wait_unfenced(read, *, kind, clock=time.monotonic, sleep=time.sleep, margin_s=MARGIN_S, max_wait_s=MAX_WAIT_S, poll_s=POLL_S):
    """Poll `read()` until the port is not fenced. kind is "radio" (the field tx_state exists whenever the image has RFSAFE1: an
    absent field returns at once) or "ax25" (tx_fence is omitted when not fenced: an absent field returns at once). The bound is
    the startup guard plus margin, raised to the reported guard_ms plus margin, never above max_wait_s. Returns a Result."""
    start = clock()
    deadline = start + min(max_wait_s, STARTUP_GUARD_S + margin_s)
    last_fence = last_guard = None
    keys = []
    polls = 0
    was_fenced = False
    while True:
        obs = read()
        polls += 1
        if not keys:
            keys = list(obs.keys)
        now = clock()
        waited = int(round((now - start) * 1000))
        if obs.found:
            if obs.fence is not None:
                last_fence = obs.fence
            if obs.guard_ms is not None:
                last_guard = obs.guard_ms
                deadline = max(deadline, min(start + max_wait_s, now + obs.guard_ms / 1000.0 + margin_s))
            if kind == "radio" and not obs.present:
                return Result("absent", waited, last_fence, last_guard, keys, polls)
            if not obs.fenced:
                if kind == "ax25" and not obs.present and not was_fenced:
                    return Result("absent", waited, last_fence, last_guard, keys, polls)
                return Result("unfenced" if was_fenced else "clear", waited, last_fence, last_guard, keys, polls)
            was_fenced = True
        if now >= deadline:
            return Result("timeout" if obs.found else "not_found", waited, last_fence, last_guard, keys, polls)
        sleep(poll_s)


def radio_reader(client, port, path=PUBLIC_STATE_PATH):
    """read() for a radio port from a WebClient (anonymous is enough: the public state carries the observer fields)."""
    def read():
        return radio_observation(client.get(path, expect=200).json(), port)
    return read


def ax25_reader(admin, port):
    """read() for an AX.25 port from an AdminClient with at least the operator role; opens the step-up window when the route asks
    for it (403) and retries once."""
    def fetch():
        return admin.get(admin.path(ADMIN_CLIENTS_SUFFIX), expect=None)

    def read():
        resp = fetch()
        if resp.status == 403:
            admin.step_up()
            resp = fetch()
        if resp.status != 200:
            return Observation(False, False, False, None, None, ["HTTP %d from the admin clients route" % resp.status])
        return ax25_observation(resp.json(), port)
    return read


def describe(result, kind, port):
    """The text of the [RECORD] line for one wait."""
    note = {"unfenced": "the fence ended", "clear": "not fenced when first read", "timeout": "STILL FENCED at the bound",
            "not_found": "the port row never appeared", "no_reader": "no status session available",
            "absent": ("the field tx_state is absent (image before RFSAFE1)" if kind == "radio" else
                       "tx_fence is absent (not fenced, or an image before RFSAFE1: indistinguishable)")}[result.state]
    return "tx guard wait on %s port %s: %s; waited %d ms in %d reads; last fence %r, last guard_ms %r; keys met: %s" % (
        kind, port, note, result.waited_ms, result.polls, result.fence, result.guard_ms, " | ".join(result.keys))
