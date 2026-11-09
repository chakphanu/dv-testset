# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_asl_edge.py (no rig, stdlib only; unit tests: cases/unit/test_asl_edge_support.py).

The edge's own outbound rows (allstar-lab-out.toml) are dialled once, about 60 ms after the edge starts. The monitor row to the
lab node E (permanent=false) is not redialled after a refusal, as in app_rpt, so when the edge starts before app_rpt on asl-e has
run "Normal Repeater Init <node>" the dial is refused (`rejected:refused`) and E is never linked for the rest of the run. That is
a rig startup race, not a product fault (analysis of wip-f9aaebc, T3 failed 2/2): a case that needs the row guards on it.
"""
import time
from pathlib import Path

GUARD_FILE = "asl-lab/guards.txt"


class CircuitMissing(Exception):
    """The circuit could not be established, not even after the edge restart."""


def ensure_circuit(wait, restart, record, label, timeout=30.0):
    """Make sure a circuit is up before a scenario. `wait(timeout)` returns when it is up and raises AssertionError when not,
    `restart()` restarts the edge (quiet reload, the way T6 does), `record(line)` logs one line. Returns "up" (it was up within
    `timeout`) or "restarted" (it was not, the edge was restarted, then it was up); raises CircuitMissing when it is still not
    up after the restart. The path taken is always recorded."""
    try:
        wait(timeout)
    except AssertionError as first:
        record("%s guard: path=restart, the circuit was not up within %.0f s (%s); restarting the edge" % (
            label, timeout, _first_line(first)))
        restart()
        try:
            wait(timeout)
        except AssertionError as second:
            record("%s guard: path=missing, still not up %.0f s after the edge restart" % (label, timeout))
            raise CircuitMissing("%s: not established, not even %.0f s after an edge restart: %s" % (
                label, timeout, _first_line(second))) from second
        record("%s guard: path=restarted, the circuit is up after the edge restart" % label)
        return "restarted"
    record("%s guard: path=up, the circuit was up within %.0f s, no restart" % (label, timeout))
    return "up"


def _first_line(exc):
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def recorder(out_dir, tag, clock=time.strftime):
    """record(line): print "[RECORD] tag: line" and append "tag: line" to <out_dir>/asl-lab/guards.txt (best effort)."""
    def record(line):
        print("[RECORD] %s: %s" % (tag, line), flush=True)
        path = Path(out_dir) / GUARD_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s %s: %s\n" % (clock("%H:%M:%S"), tag, line))
        except OSError:
            pass
    return record
