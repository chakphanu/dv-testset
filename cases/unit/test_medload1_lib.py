# SPDX-License-Identifier: MIT
"""dvtest/medload1.py: the arrival model, the counts, the settle wait. Pure, no rig."""
import pytest

from dvtest import medload1 as m, metrics

pytestmark = pytest.mark.unit


def test_strict_switch():
    assert m.strict_on({"DVT_MEDLOAD1": "on"}) and not m.strict_on({})


def test_clean_over_has_no_gap():
    assert m.model_gap(m.plan_over(60)) == 0


def test_dropped_runs_count_each_missing_position():
    # 10..14 (five) and 30 (one)
    assert m.model_gap(m.plan_over(60, drop=set(range(10, 15)) | {30})) == 6


def test_a_late_frame_is_ignored_and_the_hole_counts():
    order = m.plan_over(60, drop=set(range(10, 15)) | {30}, delay={25: 28})
    assert order.index(25) == order.index(28) + 1
    assert m.model_gap(order) == 7
    assert 25 not in m.delivered(order)
    assert 28 in m.delivered(order)


def test_a_swap_counts_one_gap():
    assert m.model_gap(m.plan_over(60, delay={24: 25})) == 1


def test_counter_wrap_and_a_duplicate():
    # frame 21 (counter 0) never arrives: counters 20 then 1
    assert m.model_gap(m.plan_over(60, drop={21})) == 1
    assert m.model_gap(m.plan_over(60, duplicate={40})) == 0
    assert m.model_gap(m.plan_over(60, drop={21}, duplicate={40})) == 1
    assert m.delivered(m.plan_over(60, duplicate={40})).count(40) == 1


def test_a_backward_step_across_the_wrap_reads_as_forward():
    # frame 20 delayed behind frame 22: counter 20 after counter 1 is NOT within 10 backward: forward by 20, gap 19
    order = m.plan_over(60, delay={20: 22})
    assert m.model_gap(order) >= 1


def test_usrp_gap():
    assert m.usrp_gap([0, 1, 2, 3, 7]) == 3
    assert m.usrp_gap([0xFFFFFFFE, 1]) == 2
    assert m.usrp_gap([5, 4, 6]) == 0
    assert m.usrp_gap([5, 5, 6]) == 0


def sample(name, value, **labels):
    return metrics.Sample(name, labels, value)


def test_counts_sum_edges_and_pick_the_room():
    samples = [
        sample(m.EDGE_UNITS, 3, dir="rx", result="gap", proc="dextra"),
        sample(m.EDGE_UNITS, 2, dir="rx", result="gap", proc="usrp"),
        sample(m.EDGE_UNITS, 1, dir="tx", result="lost", proc="dextra"),
        sample(m.ROOM_UNITS, 5, room="p", result="gap", proc="p"),
        sample(m.ROOM_UNITS, 9, room="q", result="gap", proc="q"),
        sample(m.ROOM_OVERS, 2, room="p", result="lossy", proc="p"),
        sample(m.ROOM_OVERS, 1, room="p", result="complete", proc="p"),
    ]
    c = m.counts(samples, "p")
    assert c.edge_rx_gap == 5 and c.edge_tx_lost == 1 and c.room_gap == 5 and c.overs_lossy == 2
    assert m.overs_total(c) == 3
    assert m.families_present(samples) and not m.families_present([])


def test_wait_settled_returns_the_read_after_the_quiet_period():
    clock = [0.0]
    seq = [m.counts([], "p")] * 2
    reads = {"n": 0}

    def read():
        reads["n"] += 1
        if reads["n"] <= 2:
            return m.counts([], "p")
        return m.counts([sample(m.ROOM_OVERS, 1, room="p", result="complete")], "p")

    before = m.counts([], "p")
    got = m.wait_settled(read, before, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    assert m.overs_total(got) == 1 and seq


def test_wait_settled_times_out():
    clock = [0.0]
    before = m.counts([], "p")
    with pytest.raises(TimeoutError):
        m.wait_settled(lambda: before, before, budget_s=20.0, clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))


def _delta(**kw):
    base = dict.fromkeys(m.Counts._fields, 0.0)
    base.update(kw)
    return m.Counts(**base)


def test_classify_round():
    assert m.classify_round(_delta(overs_complete=5), 5, True)[0] == m.ROUND_PASS
    assert m.classify_round(_delta(overs_complete=5, room_late=3), 5, True)[0] == m.ROUND_STARVED
    assert m.classify_round(_delta(overs_complete=5, room_late=3), 5, False)[0] == m.ROUND_FAIL
    assert m.classify_round(_delta(overs_lossy=5, edge_rx_gap=2, room_gap=2), 5, True)[0] == m.ROUND_FAIL
    assert m.classify_round(_delta(overs_complete=4), 5, True)[0] == m.ROUND_FAIL        # one uncounted over
    assert m.classify_round(_delta(overs_complete=4, overs_end_missing=1), 5, True)[0] == m.ROUND_FAIL
    assert m.classify_round(_delta(overs_complete=5, room_lost=1), 5, True)[0] == m.ROUND_FAIL


def test_judge_rounds():
    ok, _ = m.judge_rounds(["pass"] * 5)
    assert ok
    assert m.judge_rounds(["pass"] * 4 + ["HOST_STARVED"])[0]
    assert not m.judge_rounds(["pass"] * 3 + ["HOST_STARVED"] * 2)[0]
    assert not m.judge_rounds(["pass"] * 4 + ["FAIL"])[0]


def test_ignored_counts_late_and_duplicate_frames():
    assert m.ignored(m.plan_over(60)) == 0
    assert m.ignored(m.plan_over(60, drop=set(range(10, 15)) | {30})) == 0
    assert m.ignored(m.plan_over(60, drop=set(range(10, 15)) | {30}, delay={25: 28})) == 1
    assert m.ignored(m.plan_over(60, drop={21}, duplicate={40})) == 1
