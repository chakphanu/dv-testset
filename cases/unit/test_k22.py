# SPDX-License-Identifier: MIT
"""dvtest/k22.py: the pure judgements of the K22 / ICE1a cases."""
import pytest

from dvtest import k22

pytestmark = pytest.mark.unit


def test_judge_loss_not_lost():
    assert k22.judge_loss({"ice:disconnected": 6000}, None, 5)["lost"] is False


def test_judge_loss_grace_expiry_during_the_break():
    j = k22.judge_loss({"ice:disconnected": 6200}, 18.3, 20)
    assert j == {"lost": True, "during_break": True, "side": "page_grace_expiry", "gap_s": 12.1}


def test_judge_loss_engine_failed_after_the_break():
    j = k22.judge_loss({"ice:disconnected": 6200, "ice:failed": 14000}, 14.1, 12)
    assert j["side"] == "engine_failed" and j["during_break"] is False


def test_judge_loss_unknown_gap_and_no_disconnected():
    assert k22.judge_loss({"ice:disconnected": 6000}, 30.0, 40)["side"] == "unknown"
    j = k22.judge_loss({}, 17.0, 20)
    assert j["side"] == "unknown" and j["gap_s"] is None


def test_runs_compresses_equal_neighbours():
    r = k22.runs([(0, 204), (1, 204), (2, 401), (3, 204)])
    assert [(x["status"], x["n"]) for x in r] == [(204, 2), (401, 1), (204, 1)]
    assert r[0]["first"] == 0 and r[0]["last"] == 1


def test_alive_summary_documented_order():
    series = [(100.0, 204), (101.0, 204), (130.0, 401), (131.0, 401), (140.0, 403)]
    s = k22.alive_summary(series, 100.0)
    assert s["monotonic"] and s["first"] == {204: 0.0, 401: 30.0, 403: 40.0} and s["n"] == 5
    assert s["only"] == [204, 401, 403]


def test_alive_summary_flags_a_status_that_goes_back():
    s = k22.alive_summary([(0.0, 204), (1.0, 401), (2.0, 204)], 0.0)
    assert not s["monotonic"]


def test_alive_summary_counts_none_as_an_unknown_status():
    s = k22.alive_summary([(0.0, 204), (1.0, None), (2.0, 403)], 0.0)
    assert s["monotonic"] and None in s["first"]


def test_renewed_past():
    series = [(0.0, 204), (50.0, 204)]
    assert k22.renewed_past(series, 0.0, 30.0, 10.0)
    assert not k22.renewed_past(series, 0.0, 30.0, 25.0)
    assert not k22.renewed_past([(50.0, 401)], 0.0, 30.0, 10.0)


def test_event_times_skips_hello_and_filters():
    frames = [(0.1, {"event": "hello"}), (5.0, {"event": "status"}), (6.0, {"event": "room"}), (7.0, {})]
    assert k22.event_times(frames) == [5.0, 6.0]
    assert k22.event_times(frames, names={"status"}) == [5.0]


def test_per_minute_buckets():
    assert k22.per_minute([1, 59, 61, 125], 130) == [2, 1, 1]
    assert k22.per_minute([], 600) == [0] * 10
    assert k22.per_minute([], 0) == [0]
    assert k22.per_minute([599.9], 600) == [0] * 9 + [1]


def test_idle_stream_verdict_quiet_and_noisy():
    quiet = k22.idle_stream_verdict([3.0, 4.0], 600)
    assert quiet["ok"] and quiet["after_warmup"] == 0 and quiet["worst_minute"] == 0
    noisy = k22.idle_stream_verdict([5.0 * i for i in range(1, 120)], 600)     # one event per 5 s
    assert not noisy["ok"] and noisy["worst_minute"] >= 11
    assert noisy["per_minute"][0] == 11


def test_idle_stream_verdict_warmup_is_not_judged():
    v = k22.idle_stream_verdict([float(i) for i in range(1, 30)], 600)         # 29 events in the first minute only
    assert v["ok"] and v["after_warmup"] == 0


def series(*pairs):
    """[(t, status)] from (t, status) pairs, epoch 1000 + t."""
    return [(1000.0 + t, st) for t, st in pairs]


LEASE_204 = [(t, 204) for t in range(0, 30)]


def test_lease_end_204_401_403_is_ok_and_reports_the_window():
    v = k22.judge_lease_end(series(*LEASE_204, (30, 401), (31, 401), (32, 401), (33, 403), (34, 403)), 1000.0, 60.0)
    assert v["ok"], v["why"]
    assert v["ended_s"] == 30.0 and v["t403_s"] == 33.0 and v["window401"] and v["window401_s"] == 3.0
    assert "yes, 3.0 s" in k22.window_note(v)


def test_lease_end_204_straight_to_403_is_ok_the_401_window_may_be_missed():
    # the K22 failure of 20261103T194120Z: the 5 s sweep fired between two 1 s polls
    v = k22.judge_lease_end(series(*LEASE_204, (30, 403), (31, 403)), 1000.0, 60.0)
    assert v["ok"], v["why"]
    assert not v["window401"] and v["window401_s"] == 0.0 and v["ended_s"] == 30.0
    assert "observed: no" in k22.window_note(v)


def test_lease_end_403_then_401_fails():
    v = k22.judge_lease_end(series(*LEASE_204, (30, 403), (31, 401), (32, 403)), 1000.0)
    assert not v["ok"] and "401" in v["why"] and "nothing goes back" in v["why"]


def test_lease_end_401_then_204_fails():
    v = k22.judge_lease_end(series(*LEASE_204, (30, 401), (31, 204), (32, 403)), 1000.0)
    assert not v["ok"] and "204" in v["why"]


def test_lease_end_403_then_204_fails():
    assert not k22.judge_lease_end(series((0, 204), (1, 403), (2, 204)), 1000.0)["ok"]


def test_lease_end_never_ending_in_403_fails():
    stuck_401 = k22.judge_lease_end(series(*LEASE_204, (30, 401), (31, 401)), 1000.0)
    assert not stuck_401["ok"] and "never 403" in stuck_401["why"] and stuck_401["window401"]
    renewed = k22.judge_lease_end(series(*LEASE_204), 1000.0)
    assert not renewed["ok"] and "never ended" in renewed["why"] and renewed["ended_s"] is None


@pytest.mark.parametrize("bad", [200, 500, 429, 404])
def test_lease_end_any_other_status_fails(bad):
    v = k22.judge_lease_end(series(*LEASE_204, (30, bad), (31, 403)), 1000.0)
    assert not v["ok"] and str(bad) in v["why"]


def test_lease_end_bound_applies_to_the_first_non_204_answer():
    s = series(*LEASE_204, (30, 403))
    assert k22.judge_lease_end(s, 1000.0, 30.0)["ok"]
    late = k22.judge_lease_end(s, 1000.0, 29.0)
    assert not late["ok"] and "bound" in late["why"]


def test_lease_end_ignores_answers_before_the_start_and_counts_blips():
    s = series((-5, 403), (-4, 401)) + series(*LEASE_204, (30, None), (31, 403))
    v = k22.judge_lease_end(s, 1000.0, 60.0)
    assert v["ok"], v["why"]
    assert v["gaps"] == 1 and v["n"] == len(LEASE_204) + 2
