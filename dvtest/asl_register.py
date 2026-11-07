# SPDX-License-Identifier: MIT
"""Pure helpers of the registration cases R1-R7 of cases/test_asl_edge.py (the edge's HTTP registrar client against the lab's
stand-in): the node schedule read from the rendered config, the start of the current edge process read from the requests, trimming
the recording to that process, and the spread-slot arithmetic.

Why (dv-router ruling 2026-11-04, debug-asl-r1-cadence): the registration schedule is per edge PROCESS. Each new dv-edge-allstar
process (also after a quiet SIGUSR1 restart) starts a fresh initial spread: with N public nodes and interval I the slot spacing is
S = max(I, 60) / N and node k (config order, 0-based) first registers at start + k * S; after that a successful refresh is
max(60, min(I, refresh)) seconds, a failure backs off 30 s with 80..120 percent jitter. A recording that spans an edge restart
(for example test_restart_then_over_five_times running before R1 in one session) mixes several schedules, so the cases judge only
the rows after the last start. No I/O here; unit-tested in cases/unit/test_asl_register.py.
"""
import re
import tomllib

MIN_INTERVAL_S = 60.0             # the edge's floor of the initial spread and of a refresh (images before wip-1590f26 accepted "60s")
LAB_MIN_INTERVAL_S = 120.0        # wip-1590f26 refuses a [edges.allstar.register] interval below 120 s; the lab config uses "120s"
STANDIN_REFRESH_S = 180.0         # the "refresh" the stand-in registrar answers (rig/asl-lab-registrar.py)
REFRESH_GAP_S = (50.0, 90.0)      # a successful refresh at interval 60 s (the old lab value); the lab now uses refresh_gap(interval)
RETRY_GAP_S = (24.0, 36.0)        # a failed attempt: 30 s +/- 20 percent (http 500, no success text, oversize reply)
SLOW_GAP_S = (44.0, 56.0)         # the 20 s core timeout, then a 24..36 s backoff
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
