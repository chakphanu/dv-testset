# SPDX-License-Identifier: MIT
"""dvtest/guard1.py: the probe timeline judge. Pure, no rig."""
import pytest

from dvtest import guard1
from dvtest.guard1 import Probe

pytestmark = pytest.mark.unit


def test_strict_switch():
    assert guard1.strict_on({"DVT_GUARD1": "on"})
    assert not guard1.strict_on({})


def test_deadline():
    assert guard1.deadline_after(100.0) == 100.0 + guard1.APPLIED_BY_S + guard1.SLACK_S


def test_all_refused_is_clean():
    probes = [Probe(100.0 + i * 0.5, False) for i in range(100)]
    v = guard1.judge_probes(probes, 100.0)
    assert v.problems == [] and v.early_leaks == [] and v.late_leaks == []
    assert v.first_refused == 0.0


def test_early_leak_is_a_finding_not_a_failure():
    probes = [Probe(100.0, True), Probe(101.0, True)] + [Probe(102.0 + i * 0.5, False) for i in range(100)]
    v = guard1.judge_probes(probes, 100.0)
    assert v.problems == [] and len(v.early_leaks) == 2 and v.late_leaks == []


def test_late_leak_fails():
    probes = [Probe(100.0 + i * 0.5, False) for i in range(100)] + [Probe(160.0, True)]
    v = guard1.judge_probes(probes, 100.0)
    assert len(v.late_leaks) == 1 and v.problems


def test_probes_before_the_room_returned_are_ignored():
    probes = [Probe(90.0, True)] + [Probe(100.0 + i * 0.5, False) for i in range(100)]
    assert guard1.judge_probes(probes, 100.0).problems == []


def test_too_short_an_observation_is_a_problem():
    probes = [Probe(100.0 + i, False) for i in range(5)]
    assert guard1.judge_probes(probes, 100.0).problems


def test_describe():
    assert "heard at [1.0]" in guard1.describe([Probe(101.0, True), Probe(102.0, False)], 100.0)
