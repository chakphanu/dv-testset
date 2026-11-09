# SPDX-License-Identifier: MIT
"""cases/worker_gap_support.py: the gap POST of DP15-3a must be inside the observed down window."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker_gap_support as wg  # noqa: E402

pytestmark = pytest.mark.unit


def test_post_inside_the_window_is_valid_and_margins_are_measured():
    got = wg.judge(t_kill=10.0, t_down=10.002, t_post0=10.05, t_post1=10.30, t_up=10.506)
    assert got["valid"] and not got["hard"]
    assert got["down_to_post"] == pytest.approx(0.048) and got["post_to_up"] == pytest.approx(0.206)
    assert got["kill_to_post"] == pytest.approx(0.30) and got["kill_to_up"] == pytest.approx(0.506)


def test_the_run_of_20261109T213056Z_does_not_count():
    # the POST response 27 ms after the up event
    got = wg.judge(10.0, 10.002, 10.20, 10.533, 10.506)
    assert not got["valid"] and not got["hard"] and "before the POST response" in got["reason"]
    assert got["post_to_up"] == pytest.approx(-0.027)


def test_post_that_starts_before_the_down_event_does_not_count():
    got = wg.judge(10.0, 10.2, 10.1, 10.3, 10.7)
    assert not got["valid"] and not got["hard"] and "before the down event" in got["reason"]


def test_response_at_the_same_instant_as_the_up_event_does_not_count():
    assert not wg.judge(10.0, 10.002, 10.1, 10.506, 10.506)["valid"]


@pytest.mark.parametrize("args,needle", [
    ((10.0, None, None, None, None), "down"),
    ((10.0, 10.002, None, None, 10.5), "not posted"),
    ((10.0, 10.002, 10.05, 10.2, None), "up event"),
])
def test_missing_events_are_hard_failures_not_races(args, needle):
    got = wg.judge(*args)
    assert got["hard"] and not got["valid"] and needle in got["reason"]


def _result(valid=False, hard=False, reason="r"):
    return {"valid": valid, "hard": hard, "reason": reason, "down_to_post": None, "post_to_up": None, "kill_to_post": None, "kill_to_up": None}


def test_run_attempts_stops_at_the_first_valid_one():
    seq = [_result(reason="late"), _result(valid=True), _result(valid=True)]
    lines = []
    attempts = wg.run_attempts(lambda n: seq[n - 1], lines.append)
    assert len(attempts) == 2 and len(lines) == 2
    assert "does not count" in lines[0] and "valid" in lines[1]
    assert wg.verdict(attempts) == (attempts[1], None)


def test_run_attempts_gives_up_after_three_and_skips_with_the_reasons():
    lines = []
    attempts = wg.run_attempts(lambda n: _result(reason="late %d" % n), lines.append)
    assert len(attempts) == 3 and len(lines) == 3
    valid, skip = wg.verdict(attempts)
    assert valid is None and "3 attempt(s)" in skip and "late 1" in skip and "late 3" in skip
    assert skip.count("down->POST") == 3 and skip.count("POST->up") == 3


def test_the_skip_reason_lists_the_margins_of_every_attempt():
    first = wg.judge(10.0, 10.002, 10.20, 10.533, 10.506)
    second = wg.judge(20.0, 20.002, 20.60, 20.70, 21.0)
    second["valid"], second["reason"] = False, "forced"
    first.update(n=1)
    second.update(n=2)
    valid, skip = wg.verdict([first, second])
    assert valid is None
    assert "attempt 1:" in skip and "POST->up -0.027 s" in skip and "attempt 2:" in skip and "down->POST 0.598 s" in skip


def test_run_attempts_does_not_retry_a_hard_failure_and_does_not_skip_it():
    attempts = wg.run_attempts(lambda n: _result(hard=True, reason="no data_worker_down event"), lambda line: None)
    assert len(attempts) == 1
    assert wg.verdict(attempts) == (None, None)       # the case's own assertion reports it


def test_describe_prints_every_margin():
    line = wg.describe(2, wg.judge(10.0, 10.002, 10.05, 10.30, 10.506))
    assert line.startswith("gap attempt 2: valid") and "down->POST 0.048 s" in line and "POST->up 0.206 s" in line
