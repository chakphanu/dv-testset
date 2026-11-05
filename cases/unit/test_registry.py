# SPDX-License-Identifier: MIT
"""dvtest/registry.py: the pure judging and timing arithmetic of the LEAK2 stream-registry cases."""
import pytest

from dvtest import registry

pytestmark = pytest.mark.unit


def test_default_plan_is_sound():
    assert registry.check_plan() == []
    early, late = registry.entry_alive_until()
    assert (early, late) == (4.0, 5.0)              # last refresh 0..1 s before the kill, plus the 5 s ttl
    assert registry.P1_AFTER_KILL_S < early < late < registry.P2_AFTER_KILL_S


@pytest.mark.parametrize("p1,p2,count", [
    (1.5, 6.5, 0),
    (3.6, 6.5, 1),          # P1 too close to the earliest expiry
    (1.5, 5.4, 1),          # P2 too close to the latest expiry
    (3.9, 5.2, 2),
    (7.0, 6.5, 2),          # wrong order, and P1 after the expiry
])
def test_check_plan_flags_bad_plans(p1, p2, count):
    assert len(registry.check_plan(p1, p2)) == count


def test_pick_t_kill_accepts_a_time_inside_the_call_and_a_little_outside():
    assert registry.pick_t_kill(10.4, 10.0, 11.0) == 10.4
    assert registry.pick_t_kill(9.6, 10.0, 11.0) == 9.6          # 0.4 s early: inside the slack
    assert registry.pick_t_kill(11.4, 10.0, 11.0) == 11.4


@pytest.mark.parametrize("helper", [9.0, 11.6, 100.0, -5.0])
def test_pick_t_kill_refuses_disagreeing_clocks(helper):
    with pytest.raises(ValueError, match="clocks"):
        registry.pick_t_kill(helper, 10.0, 11.0)


def test_judge_silent():
    assert registry.judge_silent("L1", 0, 60) == []
    msg = registry.judge_silent("L1", 3, 60, "(x)")
    assert len(msg) == 1 and "3 of 60" in msg[0] and "(x)" in msg[0] and msg[0].startswith("L1")


def test_judge_delivered_threshold():
    assert registry.judge_delivered("c", 60, 60) == []
    assert registry.judge_delivered("c", 48, 60) == []              # exactly 80 percent
    assert len(registry.judge_delivered("c", 47, 60)) == 1
    assert registry.judge_delivered("c", 59, 60, minimum=1.0)
    assert registry.judge_delivered("c", 0, 0)                      # nothing sent: never a pass
    assert "47 of 60" in registry.judge_delivered("c", 47, 60)[0]


def test_judge_l3_pass_and_each_failure():
    off = "kill=+0.00 s"
    assert registry.judge_l3(0, 60, 60, off) == []
    assert registry.judge_l3(0, 50, 60, off) == []
    admitted = registry.judge_l3(5, 60, 60, off)
    assert len(admitted) == 1 and "P1" in admitted[0] and off in admitted[0]
    stale = registry.judge_l3(0, 0, 60, off)
    assert len(stale) == 2 and "P2" in stale[0] and "did not expire" in stale[1]
    both = registry.judge_l3(10, 0, 60, off)
    assert len(both) == 2 and "P1" in both[0] and "P2" in both[1]      # no combined hint when P1 was not silent


def test_offsets_text():
    text = registry.offsets_text(holder_start=100.0, kill=103.2, P1=104.7, P2=None)
    assert text == "holder_start=-3.20 s, kill=+0.00 s, P1=+1.50 s"
    assert registry.offsets_text(a=1.0) == "a=+1.00 s"


def test_poll_summary_and_judge():
    attempts = [(0.5, 0, 60), (3.0, 0, 60), (6.0, 59, 60)]
    first, text = registry.poll_summary(attempts)
    assert first == 6.0 and "3 attempts" in text and "+6.0 s:59/60" in text
    assert registry.judge_poll(attempts) == []
    assert registry.poll_summary([]) [0] is None
    late = registry.judge_poll([(0.5, 0, 60), (31.0, 60, 60)])
    assert len(late) == 1 and "30 s" in late[0]
    never = registry.judge_poll([(0.5, 0, 60)], relinked_ok=True)
    assert len(never) == 1 and "WORKED" in never[0]
    assert "did not work either" in registry.judge_poll([], relinked_ok=False)[0]
    assert "freshly" not in registry.judge_poll([])[0]


def test_compare_children():
    before = [(10, 100), (11, 101), (12, 102), (13, 103)]
    after = [(10, 100), (11, 101), (22, 250), (13, 103)]
    assert registry.compare_children(before, after) == []
    assert registry.compare_children(before, before)                          # nothing restarted at all
    assert registry.compare_children(before, [(10, 100), (11, 101), (13, 103)])       # not respawned
    two = registry.compare_children(before, [(10, 100), (21, 250), (22, 251), (13, 103)])
    assert two and any("2 dv-room processes" in p for p in two)
    reused_pid = registry.compare_children(before, [(10, 100), (11, 101), (12, 999), (13, 103)])
    assert reused_pid == []                                                    # same pid, new start time: restarted
    extra = registry.compare_children(before, after + [(30, 300)])
    assert extra and any("count" in p for p in extra)


def test_judge_launcher():
    assert registry.judge_launcher([(1, 5)], [(1, 5)]) == []
    assert registry.judge_launcher([(1, 5)], [(1, 6)])
    assert registry.judge_launcher([(1, 5)], [])
    assert registry.judge_launcher([], [])
