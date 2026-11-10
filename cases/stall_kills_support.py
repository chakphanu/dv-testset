# SPDX-License-Identifier: MIT
"""RFWD1 false-kill guard for the timing cases (cases/test_web_load.py, cases/test_web_soak.py; docs/web-load-cases.md, docs/web-soak-cases.md).

DV-Router RFWD1 (image wip-8891cc7): the launcher SIGKILLs an RF child whose heartbeat stalls for 2 s and counts it in
`dv_launcher_child_stall_kills_total{child}` (hub metrics, status.md launcher families). DV-Router asks that the counter stays 0
for every child across W8 / load and the soak: a non-zero value there is a false kill, because those cases drive load and no RF child
is ever stalled on purpose.

The module-scoped autouse fixture `stall_kills_start` reads the counter before the first test of the module (outside every measured
window); the last test of the module (`check_end`, called by `test_zz_rfwd1_no_stall_kills` of each file) reads it again after the
hub's 10 s publishing interval, writes out/<run>/web/stall-kills.json and fails when any child's end value or delta is above 0,
naming each child. A hub without the series (an image without RFWD1) is recorded as "absent" and the test skips. Pure logic (parse,
delta, verdict, merge-write) is stdlib only; the reads go through the control channel `metrics xlxd-sut` (dvtest/metrics.py).
"""
import json
import os
import time
from pathlib import Path

import pytest

from dvtest import metrics, web

FAMILY = "dv_launcher_child_stall_kills_total"
FLUSH_S = 12.0              # the hub publishes every 10 s: a kill just before the end shows up within this
START_WAIT_S = 30.0         # how long the start read retries while the hub does not answer
FILE_NAME = "stall-kills.json"


# ---------------------------------------------------------------- pure logic

def parse_stall_kills(samples):
    """{child: value} of the family in a list from metrics.parse(), None when the series is absent (no sample of the family)."""
    found = {}
    for sample in samples:
        if sample.name == FAMILY:
            found[sample.labels.get("child", "?")] = found.get(sample.labels.get("child", "?"), 0.0) + sample.value
    return found or None


def compute(start, end):
    """{child: {"start", "end", "delta"}} for every child of either reading. `start` / `end` are {child: value} or None. A child
    first seen at the end has start 0 (a counter starts at 0); a child that vanished keeps its start and has end None, delta 0."""
    out = {}
    for child in sorted(set(start or {}) | set(end or {})):
        before = (start or {}).get(child)
        after = (end or {}).get(child)
        base = 0.0 if before is None else before
        out[child] = {"start": before, "end": after, "delta": 0.0 if after is None else after - base}
    return out


def verdict(start, end, readable=True):
    """(kind, message): kind is "absent" (no series at the end: an image without RFWD1), "unreadable" (the end read failed),
    "false_kill" (any child's end value or delta above 0) or "ok"; the message names every child with its value."""
    if not readable:
        return "unreadable", "the hub metrics could not be read at the end of the module, so RFWD1 false kills cannot be excluded"
    if end is None:
        return "absent", "no %s series in the hub metrics: the image has no RFWD1" % FAMILY
    rows = compute(start, end)
    bad = {c: r for c, r in rows.items() if (r["end"] or 0) > 0 or r["delta"] > 0}
    if bad:
        parts = ["%s start %s end %s delta %s" % (c, fmt(r["start"]), fmt(r["end"]), fmt(r["delta"])) for c, r in sorted(bad.items())]
        return "false_kill", "RFWD1 false kill: %s (%s must stay 0 for every child while this module drives load)" % ("; ".join(parts), FAMILY)
    return "ok", "%s is 0 for all %d children" % (FAMILY, len(rows))


def fmt(value):
    if value is None:
        return "none"
    return str(int(value)) if float(value).is_integer() else "%g" % value


def record(module, start, end, kind, message, readable=True):
    """The JSON record of one module."""
    return {"series_present": end is not None, "readable_at_end": readable, "series_at_start": start is not None,
            "children": compute(start, end) if end is not None else {}, "verdict": kind, "message": message}


def merge_write(path, module, rec):
    """Merge the record of `module` into the JSON file at `path` (two modules may share a run). Returns the whole document, or None
    when the file cannot be written."""
    path = Path(path)
    doc = {"schema": 1, "family": FAMILY, "modules": {}}
    try:
        if path.is_file():
            old = json.loads(path.read_text())
            if isinstance(old, dict) and isinstance(old.get("modules"), dict):
                doc["modules"].update(old["modules"])
    except (OSError, ValueError):
        pass
    doc["modules"][module] = rec
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    except OSError:
        return None
    return doc


def out_path():
    return Path(os.environ.get("DVT_OUT", "out")) / "web" / FILE_NAME


# ---------------------------------------------------------------- reads (control channel)

def read_counter(control):
    """({child: value} or None when absent, readable). One read of the hub metrics; readable is False when the control channel or the
    hub did not answer."""
    try:
        text = control.metrics("xlxd-sut")
    except (RuntimeError, TimeoutError, ValueError, OSError):
        return None, False
    return parse_stall_kills(metrics.parse(text)), True


def read_start(control, sleep=time.sleep, clock=time.monotonic, wait=START_WAIT_S):
    """The start reading: retries while the hub does not answer. Returns {"counters": ..., "readable": bool}."""
    end = clock() + wait
    while True:
        counters, readable = read_counter(control)
        if readable or clock() >= end:
            return {"counters": counters, "readable": readable}
        sleep(2.0)


# ---------------------------------------------------------------- pytest glue (imported by the case files)

@pytest.fixture(scope="module", autouse=True)
def stall_kills_start(request):
    """Before the first test of the module: the counter, outside every measured window. Never skips or fails the module: without the
    control channel the start is recorded as unreadable and the last test says so."""
    try:
        control = request.getfixturevalue("rig_control")
    except pytest.skip.Exception:
        return {"counters": None, "readable": False, "control": None}
    except Exception:       # a broken rig fixture is reported by the tests that use it
        return {"counters": None, "readable": False, "control": None}
    got = read_start(control)
    got["control"] = control
    return got


def check_end(module, start, sleep=time.sleep, write=merge_write, finding=web.finding):
    """The body of the last test of a module: waits for one hub publication, reads, records, judges. Skips when the series is
    absent, fails on a false kill (and on an unreadable end). `start` is the dict of the autouse fixture."""
    control = start.get("control")
    if control is None:
        pytest.skip("no rig control channel (DVT_RIG_CONTROL): the RFWD1 stall-kill counter cannot be read")
    sleep(FLUSH_S)
    end, readable = read_counter(control)
    kind, message = verdict(start["counters"], end, readable)
    rec = record(module, start["counters"], end, kind, message, readable)
    write(out_path(), module, rec)
    print("[RECORD] RFWD1 %s: %s" % (module, message))
    if kind == "absent":
        pytest.skip(message)
    if kind in ("false_kill", "unreadable"):
        finding("RFWD1 %s: %s" % (module, message))
        pytest.fail(message)
    if not start["readable"]:
        # the start read failed but the end shows 0 everywhere: a counter starts at 0, so the delta is 0 too
        print("[RECORD] RFWD1 %s: the start read failed; every end value is 0, so the delta is 0 as well" % module)
