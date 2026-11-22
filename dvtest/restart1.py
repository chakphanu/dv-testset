# SPDX-License-Identifier: MIT
"""What a DV-Router image with RESTART1 / RESTART1b lets a black-box case assert about a NODE restart (DV-Router f34eb5ba and 348f984f, merged in
bc9af2b1; docs/design/deployment.md "Launcher node restart"; image dv-router:wip-f1f3777).

Switch: DVT_RESTART1=on asserts; off (the default) skips the cases with a reason.

RESTART1 (read from the sources and deployment.md, not guessed): `POST /api/v1/admin/system/restart-node` (admin, step-up, body {"confirm": <reflector callsign>},
202) makes the launcher send the reply, stop and join every child, REAP every child pid (RESTART1b: a separate reaped event per outstanding pid; survivors
are killed after 10 s, collection error or timeout refuses the re-exec and exits 1), close its embedded fabric session and await the close, check that the
Unix listener's lock descriptor is closed, seal every descriptor from 3 up close-on-exec, and only then re-execute. Exec keeps the launcher PID (and the
parenthood). In the bare image the launcher is PID 1; in the rig compose (`init: true`) docker-init (tini) is PID 1 and the launcher runs below it, so
the branch "orphans adopted by PID 1 are collected by the launcher's reaper" is not exercised there and nothing here asserts on PID 1. At native start the new image collects inherited zombies and the reaper collects unknown children of every
launcher thread. No bind retry, fixed delay or forced lock-file removal takes part, so the new incarnation must come up on the first try: no child of the
old incarnation may survive, every child is new, the node is ready again within the shutdown budget (up to 23 s after the signal) plus the start.

A black box sees: the 202; the admin session lost (dv-svc-admin is a child) so a fresh login; /ready false and then true; launcher.inc / started_ms of
GET /api/v1/admin/status; the children[] pids; the process list of the container (`proc-stat xlxd-sut list`: live processes as `proc=` lines, processes in state Z
as `zombie=` lines, `dvtest.control.RigControl.proc_snapshot`). There is no counter or log line per reaped pid: a COMPLETED restart (started_ms advanced, same launcher pid)
implies that every child was reaped, and the sweep for state Z afterwards must be empty.

Pure and stdlib only (cases/unit/test_restart1_lib.py).
"""
import os
import time

READY_BUDGET_S = 120.0          # shutdown up to 23 s after the signal, then the start, the readiness gates and a margin
POLL_S = 1.0
LAUNCHER_COMM = "dv-router"


def strict_on(environ=None):
    """DVT_RESTART1=on: the image has RESTART1 / RESTART1b (wip-f1f3777 and later)."""
    return (environ if environ is not None else os.environ).get("DVT_RESTART1", "").lower() in ("on", "1", "true", "yes")


def launcher_view(status):
    """{inc, started_ms, node_ready, children: {name: pid}} of a parsed GET /api/v1/admin/status (None when it has no launcher block)."""
    launcher = status.get("launcher") if isinstance(status, dict) else None
    if not isinstance(launcher, dict):
        return None
    children = {}
    for child in launcher.get("children") or []:
        if isinstance(child, dict) and child.get("name"):
            children[str(child["name"])] = child.get("pid")
    return {"inc": launcher.get("inc"), "started_ms": launcher.get("started_ms"), "node_ready": launcher.get("node_ready"), "children": children}


def reflector_callsign(status):
    """The callsign the restart must confirm: `reflector` of the admin status (a string), else None."""
    value = status.get("reflector") if isinstance(status, dict) else None
    return value if isinstance(value, str) and value else None


def launcher_pid(procs):
    """The pid of the launcher in a `proc-stat xlxd-sut list` parse ([(pid, ppid, comm)]), else None."""
    pids = [p for p, _pp, c in procs if c == LAUNCHER_COMM]
    return pids[0] if pids else None


def children_of_launcher(procs):
    """[(pid, comm)] of the dv-* processes whose parent is the launcher."""
    root = launcher_pid(procs)
    return [(p, c) for p, pp, c in procs if root is not None and pp == root and c.startswith("dv-")]


def unparented(procs):
    """[(pid, ppid, comm)] of dv-* processes that are not children of the launcher and not children of another dv-* process: orphans an exec would carry."""
    root = launcher_pid(procs)
    known = {p for p, _pp, c in procs if c.startswith("dv-")}
    return [(p, pp, c) for p, pp, c in procs if c.startswith("dv-") and c != LAUNCHER_COMM and pp != root and pp not in known]


def zombie_problems(zombies):
    """Problems of the state-Z sweep after a restart: any process in state Z in the container is an uncollected child (RESTART1b: a stopped child must be collected
    before the re-exec). `zombies` is [(pid, ppid, comm)] of dvtest.control.parse_zombie_list."""
    return ["zombie %d (%s, parent %d): an uncollected process" % (pid, comm, ppid) for pid, ppid, comm in zombies]


def comm_counts(procs):
    counts = {}
    for _pid, comm in children_of_launcher(procs):
        counts[comm] = counts.get(comm, 0) + 1
    return counts


def restart_problems(before, after, procs_before, procs_after, zombies_after=()):
    """Problems ([] = fine) of one node restart. `before` / `after` are launcher_view() results taken before the request and after the node is ready again,
    `procs_*` the parsed process lists."""
    problems = []
    if before is None or after is None:
        return ["the admin status has no launcher block (before %s, after %s)" % (before is not None, after is not None)]
    if after["node_ready"] is not True:
        problems.append("launcher.node_ready is %r after the restart" % (after["node_ready"],))
    newer = (isinstance(after["started_ms"], int) and isinstance(before["started_ms"], int) and after["started_ms"] > before["started_ms"])
    if not newer:
        problems.append("launcher.started_ms did not advance (%r -> %r): no new incarnation was seen" % (before["started_ms"], after["started_ms"]))
    missing = sorted(set(before["children"]) - set(after["children"]))
    if missing:
        problems.append("children missing after the restart: %s" % missing)
    kept = sorted(n for n, pid in after["children"].items() if pid is not None and before["children"].get(n) == pid)
    if kept:
        problems.append("children that kept their pid across the restart (the old process survived the exec): %s" % kept)
    down = sorted(n for n, pid in after["children"].items() if pid is None)
    if down:
        problems.append("children without a pid after the restart: %s" % down)
    old_pids = {pid for pid in before["children"].values() if pid is not None}
    alive = sorted((p, c) for p, _pp, c in procs_after if p in old_pids)
    if alive:
        problems.append("processes of the old incarnation are still alive: %s" % alive)
    root_before, root_after = launcher_pid(procs_before), launcher_pid(procs_after)
    if root_before is None or root_after is None:
        problems.append("the launcher %s is not in the process list (before %s, after %s)" % (LAUNCHER_COMM, root_before, root_after))
    elif root_before != root_after:
        problems.append("the launcher pid changed %d -> %d: an exec keeps the pid" % (root_before, root_after))
    if comm_counts(procs_before) != comm_counts(procs_after):
        problems.append("the child set changed: %s -> %s" % (comm_counts(procs_before), comm_counts(procs_after)))
    problems.extend(zombie_problems(zombies_after))
    orphans = unparented(procs_after)
    if orphans:
        problems.append("dv-* processes that are not children of the launcher: %s" % orphans)
    return problems


def wait_ready(probe, budget_s=READY_BUDGET_S, poll_s=POLL_S, sleep=time.sleep, clock=time.monotonic):
    """Poll probe() -> True (ready) / False / raise (not reachable) until ready or the budget. Returns {"ready": bool, "seconds": float, "down_seen": bool,
    "polls": int}; down_seen is True when a poll answered not ready or failed."""
    start = clock()
    down_seen, polls = False, 0
    while True:
        polls += 1
        try:
            ok = bool(probe())
        except Exception:                                # noqa: BLE001  the node is stopping: any failure is "not ready"
            ok = False
        if ok:
            return {"ready": True, "seconds": clock() - start, "down_seen": down_seen, "polls": polls}
        down_seen = True
        if clock() - start >= budget_s:
            return {"ready": False, "seconds": clock() - start, "down_seen": down_seen, "polls": polls}
        sleep(poll_s)
