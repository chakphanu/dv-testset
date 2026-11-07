# SPDX-License-Identifier: MIT
"""Pure helpers of the registration cases R1-R7 of cases/test_asl_edge.py (the edge's HTTP registrar client against the lab's
stand-in): the node schedule read from the rendered config, the start of the current edge process read from the requests, trimming
the recording to that process, and the spread-slot arithmetic.

Why (dv-router ruling 2026-11-04, debug-asl-r1-cadence): the registration schedule is per edge PROCESS. Each new dv-edge-allstar
process (also after a quiet SIGUSR1 restart) starts a fresh initial spread: with N public nodes and interval I the slot spacing is
S = max(I, 60) / N and node k (config order, 0-based) first registers at start + k * S; after that a successful refresh is
max(60, min(I, refresh)) seconds. A recording that spans an edge restart (for example test_restart_then_over_five_times running
before R1 in one session) mixes several schedules, so the cases judge only the rows after the last start. No I/O here;
unit-tested in cases/unit/test_asl_register.py.

Retry rules since dv-router wip-1590f26 (ASL1j; authority docs/design/allstar.md of the DV-Router wip tree: rate_limited lines 59-72,
ordinary failures 72-75 and 620-625, success 616-618;
the code in crates/edge/allstar/src/register.rs fn retry_delay_ns agrees):
 (a) ordinary failures (http, parse, timeout, transport): the n-th consecutive failure waits
     120 s * 2^(n-1), capped at 900 s, times an entropy factor of 80..120 percent, clamped to 120..900 s: n = 1 -> 120..144 s,
     n = 2 -> 192..288, n = 3 -> 384..576, n >= 4 -> 720..900. Success resets the count. The 30 s +/- 20 percent of earlier
     builds is gone.
 (b) HTTP 429, and 503 WITH a Retry-After header: result rate_limited. A valid Retry-After (seconds or an HTTP date) is used,
     bounded to 120..86400 s; absent or invalid: interval * 2^(n-1) up to max(3600, interval), no downward jitter. A 503 WITHOUT
     Retry-After is an ordinary http failure (register_worker.rs: rate_limited = 429 or (503 and Retry-After present)).
 (c) success (and the mismatch state): the next attempt is max(120, min(interval, refresh)) seconds later.
The delay is scheduled when the failed attempt COMPLETES, so the gap between two requests of a node is the delay plus the attempt
duration (a few ms for a prompt answer, the 15 s client timeout for a reply that never comes). The failure log line
(`event=allstar_registration_failed node= result= http_status= error= next_retry_delay_ms=`) is limited to the first failure and
then one line per node and result class every 300 s (design doc lines 726-737), see dvtest/tools/asl_reg_log.py.
"""
import re
import tomllib

MIN_INTERVAL_S = 60.0             # the edge's floor of the initial spread and of a refresh (images before wip-1590f26 accepted "60s")
LAB_MIN_INTERVAL_S = 120.0        # wip-1590f26 refuses a [edges.allstar.register] interval below 120 s; the lab config uses "120s"
STANDIN_REFRESH_S = 180.0         # the "refresh" the stand-in registrar answers (rig/asl-lab-registrar.py)
REFRESH_GAP_S = (50.0, 90.0)      # a successful refresh at interval 60 s (the old lab value); the lab now uses refresh_gap(interval)
# History: RETRY_GAP_S = (24, 36) and SLOW_GAP_S = (44, 56) were the 30 s +/- 20 percent back-off of builds before wip-1590f26;
# failure_delay_range(n) / retry_gap_range(n) replace them.
SUCCESS_MIN_S = 120.0             # wip-1590f26: a successful (or mismatch) registration repeats after max(120, min(interval, refresh))
FAIL_BASE_S = 120.0               # rule (a): first retry base
FAIL_CAP_S = 900.0                # rule (a): cap of base and of the final clamp
FAIL_ENTROPY = (0.8, 1.2)         # rule (a): injected entropy picks 80..120 percent
RETRY_MIN_S = 120.0               # rule (a) final clamp floor; rule (b) Retry-After floor
RETRY_AFTER_MAX_S = 86400.0       # rule (b) Retry-After ceiling
RATE_LIMIT_CAP_S = 3600.0         # rule (b) fallback ceiling is max(this, interval)
CLIENT_TIMEOUT_S = 15.0           # the registration client's timeout (register_worker.rs TIMEOUT); a reply later than that is a timeout
ATTEMPT_PROMPT_S = (0.0, 1.5)     # duration of an attempt that gets an answer at once (loopback-class rig network)
ATTEMPT_TIMEOUT_S = (CLIENT_TIMEOUT_S - 0.1, CLIENT_TIMEOUT_S + 1.5)    # 15.27 s seen on wip-1590f26
LOG_LIMIT_S = 300.0               # design doc 726-737: one failure line per node and result class every 300 s
LOG_TOLERANCE_S = 1.0             # next request vs failure line time + logged delay
SLOT_TOLERANCE_S = 1.0
RESTART_GAP_S = 50.0              # two requests of the slot-0 node closer than this are two processes, not a refresh


def parse_interval(text):
    """Seconds of an interval like "60s", "3m", "1h" or a bare number; ValueError otherwise."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(text))
    if not m:
        raise ValueError("not an interval: %r" % (text,))
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def refresh_gap(interval_s, refresh_s=STANDIN_REFRESH_S):
    """(lo, hi) seconds expected between two requests of a node whose registration succeeded (ok, mismatch): the refresh is
    max(60, min(interval, refresh)) seconds, so 120 s for the lab's interval "120s" and the stand-in's refresh 180; the window keeps
    the old shape of REFRESH_GAP_S (-10 s, +30 s around the nominal value)."""
    nominal = max(MIN_INTERVAL_S, min(interval_s, refresh_s))
    return nominal - 10.0, nominal + 30.0


def schedule(toml_text):
    """(public node numbers in config order, interval seconds) of the rendered dv-router.toml: [[edges.allstar.node]] rows with
    public = true and [edges.allstar.register] interval (default 180 s). Node order is the slot order."""
    doc = tomllib.loads(toml_text)
    allstar = (doc.get("edges") or {}).get("allstar") or {}
    nodes = [int(n["number"]) for n in allstar.get("node", []) if n.get("public") and "number" in n]
    interval = parse_interval((allstar.get("register") or {}).get("interval", "180s"))
    return nodes, interval


def slot_spacing(count, interval_s):
    """S = max(interval, 60) / count seconds."""
    if count < 1:
        raise ValueError("no public node")
    return max(interval_s, MIN_INTERVAL_S) / count


def row_node(row):
    """The node number (str) of a recorded registration request (one node per request body), None when it has none."""
    nodes = (((row.get("body") or {}).get("data") or {}).get("nodes")) or {}
    return next(iter(nodes), None) if len(nodes) == 1 else None


def process_start(rows, slot0_node, restart_gap_s=RESTART_GAP_S):
    """Epoch of the last edge process start seen in `rows`: the slot-0 node registers at the start of every process, and a refresh
    is at least about 60 s later, so the last request of that node that follows its predecessor by less than `restart_gap_s` (or the
    first request) is the start. None when the slot-0 node has no request yet."""
    times = sorted(r["t"] for r in rows if row_node(r) == str(slot0_node))
    if not times:
        return None
    start = times[0]
    for prev, cur in zip(times, times[1:]):
        if cur - prev < restart_gap_s:
            start = cur
    return start


def trim_rows(rows, start, margin_s=0.5):
    """The rows of the process that began at `start` (requests from start - margin on), in time order."""
    return sorted((r for r in rows if r["t"] >= start - margin_s), key=lambda r: r["t"])


def first_times(rows, nodes):
    """{node: time of its first request in rows} for the nodes that have one."""
    out = {}
    for r in sorted(rows, key=lambda r: r["t"]):
        node = row_node(r)
        if node is not None and node not in out:
            out[node] = r["t"]
    return {str(n): out[str(n)] for n in nodes if str(n) in out}


def slot_problems(first, nodes, start, interval_s, tolerance_s=SLOT_TOLERANCE_S):
    """Problems (strings) of the first requests against the spread schedule: node k at start + k * S +/- tolerance. A node that has
    no request yet is a problem too."""
    spacing = slot_spacing(len(nodes), interval_s)
    problems = []
    for k, node in enumerate(nodes):
        at = first.get(str(node))
        if at is None:
            problems.append("node %s (slot %d) has no request" % (node, k))
            continue
        want = k * spacing
        got = at - start
        if abs(got - want) > tolerance_s:
            problems.append("node %s (slot %d) first request at +%.1f s, expected +%.1f s (S = %.2f s) +/- %.1f s"
                            % (node, k, got, want, spacing, tolerance_s))
    return problems


def gaps(rows, node):
    """Seconds between consecutive requests of one node."""
    times = sorted(r["t"] for r in rows if row_node(r) == str(node))
    return [round(b - a, 1) for a, b in zip(times, times[1:])]


def gap_in(gap, bounds):
    """True when lo <= gap <= hi."""
    return bounds[0] <= gap <= bounds[1]


# ---------------------------------------------------------------- retry rules of wip-1590f26 (ASL1j), see the module docstring

def failure_delay_range(n):
    """(lo, hi) seconds of the delay scheduled after the n-th consecutive ordinary failure (rule (a)): base = min(120 * 2^(n-1), 900),
    times 80..120 percent, clamped to 120..900. n = 1 -> (120, 144), 2 -> (192, 288), 3 -> (384, 576), 4 and up -> (720, 900).
    (The lead's note said 768 for n = 4: 768 is 960 * 0.8, but the code caps the base at 900 before the entropy, giving 720.)"""
    if n < 1:
        raise ValueError("failure number starts at 1: %r" % (n,))
    base = min(FAIL_BASE_S * 2 ** min(n - 1, 5), FAIL_CAP_S)
    lo, hi = base * FAIL_ENTROPY[0], base * FAIL_ENTROPY[1]
    return max(lo, RETRY_MIN_S), min(hi, FAIL_CAP_S)


def attempt_bounds(timeout=False):
    """(lo, hi) seconds an attempt lasts: the client timeout for a reply that never comes, otherwise a prompt answer."""
    return ATTEMPT_TIMEOUT_S if timeout else ATTEMPT_PROMPT_S


def retry_gap_range(n, timeout=False):
    """(lo, hi) seconds between the request of the n-th failed attempt and the next request: the delay of rule (a) plus the attempt
    duration (the delay starts when the attempt completes)."""
    lo, hi = failure_delay_range(n)
    a_lo, a_hi = attempt_bounds(timeout)
    return lo + a_lo, hi + a_hi


def rate_limited_delay(n, interval_s, retry_after_s=None):
    """Exact seconds of the delay after the n-th consecutive rate_limited failure (rule (b), no jitter). A Retry-After (seconds, already
    parsed) is clamped to 120..86400; without a usable one the interval doubles per consecutive failure up to max(3600, interval)."""
    if n < 1:
        raise ValueError("failure number starts at 1: %r" % (n,))
    if retry_after_s is not None:
        return min(max(float(retry_after_s), RETRY_MIN_S), RETRY_AFTER_MAX_S)
    return min(interval_s * 2 ** min(n - 1, 5), max(RATE_LIMIT_CAP_S, interval_s))


def success_delay_s(interval_s, refresh_s=STANDIN_REFRESH_S):
    """Rule (c): max(120, min(interval, refresh)) seconds; also the logged delay of a mismatch (a successful registration with the
    Mismatch state)."""
    return max(SUCCESS_MIN_S, min(interval_s, refresh_s))


def gap_with_attempt(delay_s, timeout=False, slack_s=LOG_TOLERANCE_S):
    """(lo, hi) seconds between two requests when the delay is known exactly (rate_limited, mismatch)."""
    a_lo, a_hi = attempt_bounds(timeout)
    return delay_s + a_lo - slack_s, delay_s + a_hi + slack_s


def retry_wait_s(retries, timeout=False):
    """Worst-case seconds from a node's first request to its (retries + 1)-th request under rule (a): the sum of the n-th delay upper
    bounds plus the attempt durations. 2 retries -> 435 s (465 s with the client timeout)."""
    a_hi = attempt_bounds(timeout)[1]
    return sum(failure_delay_range(n)[1] + a_hi for n in range(1, retries + 1))


def limiter_admits(times, window_s=LOG_LIMIT_S):
    """Which failed attempts (completion times in order, one node and one result class) get a log line: the first one, then one every
    `window_s` seconds (the last ADMITTED one counts). Returns a list of booleans."""
    out, last = [], None
    for t in times:
        if last is None or t - last >= window_s:
            out.append(True)
            last = t
        else:
            out.append(False)
    return out


# node -> (registrar variant, result class of the log line, attempts needed, attempts that fail with the client timeout)
# 2997 / 2998 are the rate_limited nodes (registrar variants "429ra" = HTTP 429 + Retry-After: 300 and "503bad" = HTTP 503 with an
# invalid Retry-After; a 503 without the header would be an ordinary http failure, variant "503", not used by a lab node).
RETRY_AFTER_S = 300                # what the registrar variant 429ra sends
REG_RESULTS = {"2992": "mismatch", "2993": "http", "2994": "parse", "2995": "timeout", "2996": "parse",
               "2997": "rate_limited", "2998": "rate_limited"}
REG_NEEDED = {"2991": 2, "2992": 2, "2993": 3, "2994": 3, "2995": 3, "2996": 3, "2997": 2, "2998": 3}   # requests per node the fixture waits for


def window_budget_s(nodes, interval_s, need=None):
    """Seconds after the edge process start the registration cases may need: for each public node in config order (slot k at
    k * S) the time of its last wanted request. 'need' maps node -> number of requests (REG_NEEDED). A margin of 30 s is added."""
    need = REG_NEEDED if need is None else need
    spacing = slot_spacing(len(nodes), interval_s)
    worst = 0.0
    for k, node in enumerate(nodes):
        count = need.get(str(node), 1)
        if count <= 1:
            continue
        node = str(node)
        if REG_RESULTS.get(node) == "rate_limited":
            span = rate_limited_span_s(count - 1, interval_s, RETRY_AFTER_S if node == "2997" else None)
        elif REG_RESULTS.get(node) == "mismatch" or node == "2991":
            span = (count - 1) * (success_delay_s(interval_s) + ATTEMPT_PROMPT_S[1])
        else:
            span = retry_wait_s(count - 1, timeout=REG_RESULTS.get(node) == "timeout")
        worst = max(worst, k * spacing + span)
    return worst + 30.0


def rate_limited_span_s(retries, interval_s, retry_after_s=None):
    """Seconds from the first request to the (retries + 1)-th for a rate_limited node (delays are exact, attempts prompt)."""
    return sum(rate_limited_delay(n, interval_s, retry_after_s) + ATTEMPT_PROMPT_S[1] for n in range(1, retries + 1))
