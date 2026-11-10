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

from dvtest import rfsafe1b

STARTUP_GUARD_S = 13.0              # radio.md 10.4 rule 3: G = D 2.5 s + max(B) 10 s + 0.5 s
MARGIN_S = 2.0                      # slack on top of the guard (poll interval, publish delay of the status record)
MAX_WAIT_S = 20.0                   # never wait longer than this for one fence
POLL_S = 0.5
PUBLIC_STATE_PATH = "/api/v1/public/state"
ADMIN_CLIENTS_SUFFIX = "/clients"   # AdminClient.path() puts the mount and /api/v1/admin in front

# found: the port row exists in the reply; present: the fence field exists in it; fenced: the port is fenced now
Observation = namedtuple("Observation", "found present fenced fence guard_ms keys via scope", defaults=(None, None))
# via: which path matched ("kind=modem items[].modem", "kind=modem items[].modem.ports[]", "fallback walker", "no modem rows");
# scope: "no_modem_object" when kind "modem" rows exist but none carries the `modem` object (a viewer or observer credential)
Result = namedtuple("Result", "state waited_ms fence guard_ms keys polls via", defaults=(None,))
# state: unfenced (was fenced, ended) | clear (never fenced when first read) | absent (no field: radio image before RFSAFE1, or an AX.25
# `modem` object without tx_fence) | no_modem (no kind "modem" row at all: the edge is not a modem session) | scope (modem rows without
# the `modem` object: a credential problem, never "not fenced") | not_found (no port row within the bound) | timeout (still
# fenced at the bound) | no_reader


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
    """The Observation of AX.25 port `port` in an admin clients body. Rows are matched by kind == "modem" and the port name of
    their `modem` object (admin.md about lines 265 and 416, status.md "Built (DP11)"); the loose walk (any object named like the
    port) is a fallback only, and `via` says which one matched. Rows of kind "modem" WITHOUT a `modem` object (viewer and observer
    credentials omit it) set `scope` instead of looking like an unfenced port. No kind "modem" row at all: via "no modem rows"."""
    keys = []
    if not isinstance(body, dict):
        return Observation(False, False, False, None, None, ["<not an object>"])
    keys.append("body:" + ",".join(sorted(body)[:12]))
    items = body.get("items")
    items = items if isinstance(items, list) else []
    keys.append("items[%d]" % len(items))
    rows = [item for item in items if isinstance(item, dict) and item.get("kind") == "modem"]
    keys.append("items[kind=modem][%d]" % len(rows))
    row = via = None
    with_object = 0
    for item in rows:
        modem = item.get("modem")
        if not isinstance(modem, dict):
            continue
        with_object += 1
        keys.append("items[kind=modem].modem:" + ",".join(sorted(modem)[:16]))
        if modem.get("name") == port:
            row, via = modem, "kind=modem items[].modem"
            break
        inner = _first(modem.get("ports"), lambda p: p.get("name") == port)
        if inner is not None:
            keys.append("items[kind=modem].modem.ports[]:" + ",".join(sorted(inner)))
            row, via = inner, "kind=modem items[].modem.ports[]"
            break
    if row is None and rows and not with_object:
        return Observation(False, False, False, None, None, keys + ["kind=modem rows carry no modem object"], "kind=modem without modem object",
                           "no_modem_object")
    if row is None and not rows:
        # not a modem session at all; the loose walk below still gets one chance for an unexpected nesting
        row = _first(list(_walk(body)), lambda d: d.get("name") == port and ("adapter" in d or "tx_fence" in d))
        via = "fallback walker" if row is not None else "no modem rows"
        if row is not None:
            keys.append("object named %s:" % port + ",".join(sorted(row)))
    if row is None:
        return Observation(False, False, False, None, None, keys + ["port %s: no modem row" % port], via or "kind=modem rows, port not among them")
    present = "tx_fence" in row
    return Observation(True, present, present and row.get("tx_fence") is not None, row.get("tx_fence"), row.get("tx_guard_ms"), keys, via)


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
        if obs.scope:
            return Result("scope", waited, last_fence, last_guard, keys, polls, obs.via)
        if not obs.found and kind == "ax25" and obs.via == "no modem rows":
            return Result("no_modem", waited, last_fence, last_guard, keys, polls, obs.via)
        if obs.found:
            if obs.fence is not None:
                last_fence = obs.fence
            if obs.guard_ms is not None:
                last_guard = obs.guard_ms
                deadline = max(deadline, min(start + max_wait_s, now + obs.guard_ms / 1000.0 + margin_s))
            if kind == "radio" and not obs.present:
                return Result("absent", waited, last_fence, last_guard, keys, polls, obs.via)
            if not obs.fenced:
                if kind == "ax25" and not obs.present and not was_fenced:
                    return Result("absent", waited, last_fence, last_guard, keys, polls, obs.via)
                return Result("unfenced" if was_fenced else "clear", waited, last_fence, last_guard, keys, polls, obs.via)
            was_fenced = True
        if now >= deadline:
            return Result("timeout" if obs.found else "not_found", waited, last_fence, last_guard, keys, polls, obs.via)
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
            "no_modem": "no kind \"modem\" row in the admin clients body: the edge is not a modem session, nothing to wait for",
            "scope": "CREDENTIAL/SCOPE PROBLEM: kind \"modem\" rows carry no `modem` object (viewer and observer rows omit it); "
                     "the guard cannot be read with this account (operator or admin needed)",
            "absent": ("the field tx_state is absent (image before RFSAFE1)" if kind == "radio" else
                       "the modem object is present without tx_fence: not fenced (an image before RFSAFE1 looks the same)")}[result.state]
    via = "" if not result.via else "; matched by %s" % result.via
    return "tx guard wait on %s port %s: %s%s; waited %d ms in %d reads; last fence %r, last guard_ms %r; keys met: %s" % (
        kind, port, note, via, result.waited_ms, result.polls, result.fence, result.guard_ms, " | ".join(result.keys))


# ---------------------------------------------------------------- the fenced shape right after a start (RFSAFE1 rule 3 and 4)

def uptime_reader(admin):
    """read() -> node uptime in seconds (float) or None, from the admin route GET /api/v1/admin/status `live.node_uptime_s`
    (admin.md about line 307; `uptime_s` of the hub when the node value is absent). Never raises: None when unknown."""
    def read():
        try:
            resp = admin.get(admin.path("/status"), expect=None)
            if resp.status != 200:
                return None
            live = (resp.json() or {}).get("live") or {}
            for key in ("node_uptime_s", "uptime_s"):
                if isinstance(live.get(key), (int, float)) and not isinstance(live.get(key), bool):
                    return float(live[key])
        except Exception:                                       # noqa: BLE001  a status read must never fail the case
            return None
        return None
    return read


StartupShape = namedtuple("StartupShape", "outcome detail lateness_s obs")
# outcome: fenced (the expected shape was seen inside the window) | late (the window had ended or could not be shown to be open: the
# case skips and says how late) | violation (inside the window and the shape is wrong: the case fails)


def guard_ok(value):
    """A reported guard_ms / tx_guard_ms: an int >= 0 (a bool is not one)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def startup_fenced_shape(read, uptime=None, *, kind, window_s=STARTUP_GUARD_S, early_margin_s=1.0, clock=time.monotonic,
                         sleep=time.sleep, gap_s=1.0):
    """One read (AX.25) or two reads `gap_s` apart (radio) right after a start; the window is the startup guard, 13 s after the
    start of the node. Expected shape: AX.25: tx_fence == "startup_guard"; radio: tx_state "fenced", fence "startup_guard". The guard_ms
    (tx_guard_ms) is OPTIONAL: it is absent until the end of the fence is known (dv-router, rf_guard.rs); its absence is recorded and
    never fails. When present it must be an int >= 0, and for radio ports it must decrease across the two reads when both have it. `uptime()` is the node uptime in
    seconds (None when unknown): at or above window_s - early_margin_s the window is treated as over (late), and an unfenced first
    read with unknown uptime is also late, since it cannot be told from a violation. Lateness is never a failure."""
    up = uptime() if uptime is not None else None
    if up is not None and up >= window_s - early_margin_s:
        return StartupShape("late", "node uptime %.1f s at the first read: the %.0f s window was over (or within %.1f s of its end)" % (
            up, window_s, early_margin_s), max(0.0, up - window_s), None)
    first = read()
    if first.scope:
        return StartupShape("violation", "CREDENTIAL/SCOPE PROBLEM: kind \"modem\" rows carry no `modem` object (operator or admin needed); keys: %s" % (
            " | ".join(first.keys)), None, first)
    if not first.found:
        return StartupShape("late", "no row for the port yet (%s): the edge had not published its session inside the window; keys: %s" % (
            "node uptime %.1f s" % up if up is not None else "node uptime unknown", " | ".join(first.keys)), None, first)
    if not first.fenced:
        if up is None:
            return StartupShape("late", "first read already unfenced, node uptime unknown: too late to tell from a violation", None, first)
        return StartupShape("violation", "node uptime %.1f s (inside the window) but the port is not fenced: expected a startup_guard fence (keys: %s)" % (
            up, " | ".join(first.keys)), None, first)
    problems = []
    if first.fence != "startup_guard":
        problems.append("fence %r, expected 'startup_guard'" % (first.fence,))
    if first.guard_ms is not None and not guard_ok(first.guard_ms):
        problems.append("guard_ms %r is not an int >= 0" % (first.guard_ms,))
    last = first
    if kind == "radio" and not problems:
        sleep(gap_s)
        second = read()
        last = second
        if not second.fenced:
            return StartupShape("late", "the fence ended between two reads %.1f s apart (first guard_ms %r)" % (gap_s, first.guard_ms), None, second)
        if second.guard_ms is not None and not guard_ok(second.guard_ms):
            problems.append("guard_ms %r is not an int >= 0" % (second.guard_ms,))
        elif first.guard_ms is not None and second.guard_ms is not None and not second.guard_ms < first.guard_ms:
            problems.append("guard_ms did not decrease over two reads: %r then %r" % (first.guard_ms, second.guard_ms))
    if problems:
        return StartupShape("violation", "; ".join(problems) + " (keys: %s)" % " | ".join(first.keys), None, last)
    return StartupShape("fenced", "fence %r, guard_ms %s%s, node uptime %s" % (
        first.fence, "absent" if first.guard_ms is None else first.guard_ms,
        "" if last is first else " then %s" % ("absent" if last.guard_ms is None else last.guard_ms), "%.1f s" % up if up is not None else "unknown"),
        None, last)


# ---------------------------------------------------------------- re-arming the fence: restart of the AX.25 child (RFSAFE1)

AX25_CHILD = "dv-edge-ax25"             # the launcher child key of the AX.25 edge (admin.md 278-280, router children.rs)


def ax25_child_names(status_body):
    """([AX25_CHILD] when the launcher lists exactly that key, else []), and every child name seen (for a skip message), from GET
    /api/v1/admin/status `launcher.children[].name` (openapi-admin.yaml: name, pid, restarts, since_ms, state). The key is matched
    exactly, never by substring."""
    launcher = status_body.get("launcher") if isinstance(status_body, dict) else None
    children = launcher.get("children") if isinstance(launcher, dict) else None
    names = [c.get("name") for c in children or [] if isinstance(c, dict) and isinstance(c.get("name"), str)]
    return [n for n in names if n == AX25_CHILD][:1], names


Rearm = namedtuple("Rearm", "outcome cause detail seen_after_ms fence_ms guard_first guard_second decreased row_gone polls keys absent_ms samples", defaults=(None,))
# samples: [rfsafe1b.Sample] of every read since the restart call returned (t in seconds), for the RFSAFE1b bounds (dvtest/rfsafe1b.py)
# outcome: ok | violation (no fence appeared, a wrong cause, a bad or non-decreasing guard, or a credential problem) | still_fenced (past max_fence_s)


def rearm_fence(read, restart, *, clock=time.monotonic, sleep=time.sleep, poll_s=0.2, appear_timeout_s=30.0, max_fence_s=MAX_WAIT_S,
                second_gap_s=1.0, expect_cause="startup_guard"):
    """`restart()` re-arms the fence by restarting the AX.25 child; it returns after the old process has stopped (dv-router), so t0 is
    taken when it RETURNS and the 13 s window of the new process is counted from there. A new process starts fenced with cause
    `startup_guard` (rf_guard.rs Reason::Startup; `recovery_probe` is in-process fabric recovery only), asserted as `expect_cause`
    (None: any non-null cause). Polls `read()` every poll_s until the port is fenced, ignoring a stale unfenced row, up to
    appear_timeout_s. tx_guard_ms is OPTIONAL: absent until the end of the fence is known, never a failure; `absent_ms` is how long it
    was absent from the first fenced read (the whole fence when it never appeared). When present it must be an int >= 0 and a second
    read at least second_gap_s after the first present one must be smaller. Then polls until unfenced, at most max_fence_s after the
    fence was first seen. `fence_ms` runs from t0 to the end of the fence, `seen_after_ms` from t0 to the first fenced read,
    `row_gone` says whether the port row disappeared after the restart (the new session)."""
    restart()
    t0 = clock()
    polls = 0
    row_gone = False
    keys = []
    first = None
    samples = []
    while True:
        obs = read()
        polls += 1
        if obs.found:
            samples.append(rfsafe1b.Sample(clock() - t0, bool(obs.fenced), obs.fence if obs.fenced else None, obs.guard_ms if obs.fenced else None))
        if not keys:
            keys = list(obs.keys)
        if obs.scope:
            return Rearm("violation", None, "CREDENTIAL/SCOPE PROBLEM: kind \"modem\" rows carry no `modem` object (operator or admin needed)",
                         None, None, None, None, None, row_gone, polls, keys, None)
        if not obs.found:
            row_gone = True
        elif obs.fenced:
            first = obs
            break
        if clock() - t0 >= appear_timeout_s:
            return Rearm("violation", None, "no fence by any cause within %.0f s of the restart (the port row %s in between); keys: %s" % (
                appear_timeout_s, "disappeared and came back" if row_gone and obs.found else ("disappeared" if row_gone else "never disappeared"),
                " | ".join(obs.keys)), None, None, None, None, None, row_gone, polls, keys, None)
        sleep(poll_s)
    seen_at = clock()
    cause = first.fence
    guard_first = first.guard_ms
    guard_t = seen_at if guard_first is not None else None
    absent_until = seen_at if guard_first is not None else None
    guard_second = None
    decreased = None
    bad_guard = guard_first if guard_first is not None and not guard_ok(guard_first) else None
    last = first
    while last.fenced:
        if clock() - seen_at >= max_fence_s:
            return Rearm("still_fenced", cause, "still fenced %.0f s after the fence was first seen (cause %r, last guard_ms %r)" % (
                max_fence_s, last.fence or cause, last.guard_ms), int((seen_at - t0) * 1000), None, guard_first, guard_second, decreased,
                row_gone, polls, keys, int(((absent_until if absent_until is not None else clock()) - seen_at) * 1000))
        sleep(poll_s)
        last = read()
        polls += 1
        if last.found:
            samples.append(rfsafe1b.Sample(clock() - t0, bool(last.fenced), last.fence if last.fenced else None, last.guard_ms if last.fenced else None))
        if not last.fenced:
            break
        if last.guard_ms is not None:
            if not guard_ok(last.guard_ms):
                bad_guard = last.guard_ms
            elif guard_first is None:
                guard_first, guard_t, absent_until = last.guard_ms, clock(), clock()
            elif guard_second is None and clock() - guard_t >= second_gap_s:
                guard_second = last.guard_ms
                decreased = guard_second < guard_first
    end = clock()
    absent_ms = int(((absent_until if absent_until is not None else end) - seen_at) * 1000)
    problems = []
    if expect_cause is not None and cause != expect_cause:
        problems.append("fence cause %r, expected %r (a child restart is a new process: Reason::Startup)" % (cause, expect_cause))
    if bad_guard is not None:
        problems.append("tx_guard_ms %r is not an int >= 0" % (bad_guard,))
    if decreased is False:
        problems.append("tx_guard_ms did not decrease: %r then %r" % (guard_first, guard_second))
    outcome = "violation" if problems else "ok"
    values = "%r%s" % (guard_first, "" if guard_second is None else " then %r" % (guard_second,))
    guard_note = ("tx_guard_ms absent for the whole fence" if guard_first is None else
                  "tx_guard_ms present from the first fenced read: %s" % values if absent_ms == 0 else
                  "tx_guard_ms absent for the first %d ms, then %s" % (absent_ms, values))
    detail = "; ".join(problems) if problems else "fence %r seen %d ms after the restart returned, ended %d ms after it; %s%s" % (
        cause, int((seen_at - t0) * 1000), int((end - t0) * 1000), guard_note,
        "" if decreased is not None or guard_first is None else " (the fence ended before a second guard read)")
    return Rearm(outcome, cause, detail, int((seen_at - t0) * 1000), int((end - t0) * 1000), guard_first, guard_second, decreased,
                 row_gone, polls, keys, absent_ms, samples)
