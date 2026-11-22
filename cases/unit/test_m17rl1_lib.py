# SPDX-License-Identifier: MIT
"""dvtest/m17rl1.py: the pure judgements of the M17RL1 cases."""
import pytest

from dvtest import m17rl1 as m

pytestmark = pytest.mark.unit


def test_strict_switch():
    assert m.strict_on({"DVT_M17RL1": "on"}) and m.strict_on({"DVT_M17RL1": "ON"})
    assert not m.strict_on({}) and not m.strict_on({"DVT_M17RL1": "off"})


def test_spacing():
    assert m.spacing_problems([0.0, 3.0, 6.1, 12.0]) == []
    assert m.spacing_problems([0.0, 2.9]) == []                     # inside the slack
    bad = m.spacing_problems([0.0, 0.3, 3.3])
    assert len(bad) == 1 and "0.30 s apart" in bad[0]
    assert m.spacing_problems([5.0]) == [] and m.spacing_problems([]) == []
    assert m.spacing_problems([3.0, 0.0, 6.0]) == []                # unsorted input


def test_max_conn_in():
    assert m.max_conn_in(14.0) == 5 and m.max_conn_in(3.0) == 2 and m.max_conn_in(2.9) == 1


def test_ack_rule():
    assert m.ack_problems(5, [1, 2, 3, 4, 5]) == []
    assert "only 3 ACKN" in m.ack_problems(5, [1, 2, 3])[0]


def test_deferral_ok_and_failures():
    # last CONN at 10.0, peer CONN at 11.0, ACKN at 11.02, deferred CONN at 13.02
    assert m.deferral_problems(10.0, 11.0, [13.02], 11.02) == []
    assert any("not suppressed" in p for p in m.deferral_problems(10.0, 11.0, [11.05], 11.02))          # the old immediate reply
    assert any("dropped" in p for p in m.deferral_problems(10.0, 11.0, [], 11.02))                      # nothing deferred
    assert any("did not move the retry" in p for p in m.deferral_problems(10.0, 11.0, [16.0], 11.02))   # left at the old backoff deadline
    assert any("no ACKN" in p for p in m.deferral_problems(10.0, 11.0, [13.0], None))
    assert any("no ACKN" in p for p in m.deferral_problems(10.0, 11.0, [13.0], 13.5))


def test_immediate():
    assert m.immediate_problems(20.0, [20.05]) == []
    assert "no local CONN" in m.immediate_problems(20.0, [10.0])[0]
    assert "at once" in m.immediate_problems(20.0, [23.0])[0]


def test_relink_time():
    assert m.relink_time(10.0, [10.1, 13.0], [10.02]) == pytest.approx(0.1)
    assert m.relink_time(10.0, [13.0], [10.02]) == pytest.approx(3.0)
    assert m.relink_time(10.0, [], [10.02]) is None and m.relink_time(10.0, [11.0], []) is None
    assert m.relink_time(10.0, [9.0], [10.02]) is None             # a CONN before the peer CONN does not count
