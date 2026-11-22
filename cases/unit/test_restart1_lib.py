# SPDX-License-Identifier: MIT
"""dvtest/restart1.py: the pure judgements of the RESTART1 cases."""
import pytest

from dvtest import restart1 as r

pytestmark = pytest.mark.unit


def status(started, children, ready=True, inc=1):
    return {"reflector": "XLX999", "launcher": {"inc": inc, "started_ms": started, "node_ready": ready,
                                                 "children": [{"name": n, "pid": p} for n, p in children.items()]}}


PROCS_A = [(10, 1, "dv-router"), (11, 10, "dv-edge-dcs"), (12, 10, "dv-room"), (13, 10, "dv-room")]
PROCS_B = [(10, 1, "dv-router"), (21, 10, "dv-edge-dcs"), (22, 10, "dv-room"), (23, 10, "dv-room")]


def test_views():
    view = r.launcher_view(status(5, {"dv-edge-dcs": 11}))
    assert view["children"] == {"dv-edge-dcs": 11} and view["node_ready"] is True and view["started_ms"] == 5
    assert r.launcher_view({}) is None and r.launcher_view({"launcher": None}) is None
    assert r.reflector_callsign(status(1, {})) == "XLX999" and r.reflector_callsign({"reflector": None}) is None


def test_process_helpers():
    assert r.launcher_pid(PROCS_A) == 10
    assert r.children_of_launcher(PROCS_A) == [(11, "dv-edge-dcs"), (12, "dv-room"), (13, "dv-room")]
    assert r.comm_counts(PROCS_A) == {"dv-edge-dcs": 1, "dv-room": 2}
    assert r.unparented(PROCS_A) == []
    assert r.unparented(PROCS_A + [(99, 1, "dv-edge-dcs")]) == [(99, 1, "dv-edge-dcs")]


def test_a_clean_restart_has_no_problems():
    before = r.launcher_view(status(1000, {"dv-edge-dcs": 11, "dv-room[A]": 12, "dv-room[B]": 13}))
    after = r.launcher_view(status(2000, {"dv-edge-dcs": 21, "dv-room[A]": 22, "dv-room[B]": 23}, inc=2))
    assert r.restart_problems(before, after, PROCS_A, PROCS_B) == []


def test_each_failure_is_named():
    before = r.launcher_view(status(1000, {"dv-edge-dcs": 11, "dv-room[A]": 12}))
    same_pid = r.launcher_view(status(2000, {"dv-edge-dcs": 11, "dv-room[A]": 22}))
    out = r.restart_problems(before, same_pid, PROCS_A, PROCS_B)
    assert any("kept their pid" in p and "dv-edge-dcs" in p for p in out)
    old = r.launcher_view(status(1000, {"dv-edge-dcs": 11}))
    assert any("still alive" in p for p in r.restart_problems(old, r.launcher_view(status(2000, {"dv-edge-dcs": 21})), PROCS_A, PROCS_B + [(11, 1, "dv-edge-dcs")]))
    not_ready = r.launcher_view(status(2000, {"dv-edge-dcs": 21}, ready=False))
    assert any("node_ready" in p for p in r.restart_problems(old, not_ready, PROCS_A, PROCS_B))
    stale = r.launcher_view(status(1000, {"dv-edge-dcs": 21}))
    assert any("started_ms did not advance" in p for p in r.restart_problems(old, stale, PROCS_A, PROCS_B))
    gone = r.launcher_view(status(2000, {}))
    assert any("missing" in p for p in r.restart_problems(old, gone, PROCS_A, PROCS_B))
    moved = [(77, 1, "dv-router")] + PROCS_B[1:]
    assert any("launcher pid changed" in p for p in r.restart_problems(old, r.launcher_view(status(2000, {"dv-edge-dcs": 21})), PROCS_A, moved))
    assert any("not children of the launcher" in p for p in r.restart_problems(old, r.launcher_view(status(2000, {"dv-edge-dcs": 21})), PROCS_A, PROCS_B + [(50, 1, "dv-edge-xlx")]))
    assert any("no launcher block" in p for p in r.restart_problems(None, None, [], []))


def test_wait_ready_counts_the_down_window_and_the_budget():
    t = [0.0]
    answers = iter([True, False, False, True])        # first poll ready, the rest unused
    assert r.wait_ready(lambda: next(answers), clock=lambda: t[0], sleep=lambda s: None) == {"ready": True, "seconds": 0.0, "down_seen": False, "polls": 1}
    seq = iter([False, "boom", True])

    def probe():
        value = next(seq)
        if value == "boom":
            raise OSError("refused")
        return value

    got = r.wait_ready(probe, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert got["ready"] and got["down_seen"] and got["polls"] == 3
    t[0] = 0.0
    never = r.wait_ready(lambda: False, budget_s=5.0, poll_s=1.0, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert never["ready"] is False and never["seconds"] >= 5.0


def test_zombies_after_a_restart_are_a_problem():
    before = r.launcher_view(status(1000, {"dv-edge-dcs": 11}))
    after = r.launcher_view(status(2000, {"dv-edge-dcs": 21}))
    procs_b = [(10, 1, "dv-router"), (11, 10, "dv-edge-dcs")]
    procs_a = [(10, 1, "dv-router"), (21, 10, "dv-edge-dcs")]
    assert r.restart_problems(before, after, procs_b, procs_a, []) == []
    out = r.restart_problems(before, after, procs_b, procs_a, [(30, 10, "dv-room")])
    assert len(out) == 1 and "zombie 30 (dv-room, parent 10)" in out[0]
    assert r.zombie_problems([]) == []
