# SPDX-License-Identifier: MIT
"""dvtest/asl_register.py: schedule from the config, process start, trimming and the slot arithmetic (no rig)."""
import pytest

from dvtest import asl_register as ar

pytestmark = pytest.mark.unit

TOML = """
[edges.allstar.register]
enable = true
interval = "120s"
[[edges.allstar.node]]
number = 2999
public = true
[[edges.allstar.node]]
number = 2991
public = true
[[edges.allstar.node]]
number = 777
public = false
[[edges.allstar.node]]
number = 2992
public = true
"""


def row(t, node):
    return {"t": t, "body": {"data": {"nodes": {str(node): {"node": node}}}}}


def test_parse_interval():
    assert ar.parse_interval("60s") == 60 and ar.parse_interval("3m") == 180 and ar.parse_interval(" 1h ") == 3600
    assert ar.parse_interval("90") == 90
    with pytest.raises(ValueError):
        ar.parse_interval("soon")


def test_schedule_reads_public_nodes_in_config_order_and_the_interval():
    assert ar.schedule(TOML) == ([2999, 2991, 2992], 120.0)
    assert ar.schedule("[edges.allstar]\nenable = true\n") == ([], 180.0)


def test_refresh_gap_follows_the_interval():
    assert ar.refresh_gap(60) == ar.REFRESH_GAP_S == (50.0, 90.0)       # the old lab value
    assert ar.refresh_gap(120) == (110.0, 150.0)                         # the lab since wip-1590f26 (interval "120s", refresh 180)
    assert ar.refresh_gap(600) == (170.0, 210.0)                         # min(interval, refresh) = refresh 180
    assert ar.refresh_gap(10) == (50.0, 90.0)                            # the floor is 60 s
    assert ar.gap_in(120.4, ar.refresh_gap(120)) and not ar.gap_in(60.0, ar.refresh_gap(120))


def test_slot_spacing_is_the_interval_over_the_node_count():
    assert round(ar.slot_spacing(7, 60), 2) == 8.57
    assert round(ar.slot_spacing(7, 120), 2) == 17.14        # the lab at first with wip-1590f26: seven nodes, "120s"
    assert round(ar.slot_spacing(9, 120), 2) == 13.33        # now: the edge, 2991..2998
    assert ar.slot_spacing(9, 180) == 20.0          # XLX345: nine nodes, 180 s
    assert ar.slot_spacing(3, 10) == 20.0           # the interval is at least 60 s
    with pytest.raises(ValueError):
        ar.slot_spacing(0, 60)


def test_row_node_needs_exactly_one_node():
    assert ar.row_node(row(1, 2991)) == "2991"
    assert ar.row_node({"body": {"data": {"nodes": {"1": {}, "2": {}}}}}) is None
    assert ar.row_node({}) is None


def test_process_start_is_the_last_slot0_request_that_follows_too_soon():
    # the failing run of 20261103T232417Z: five quiet restarts, the slot-0 node (then numbered 1999) registers at each process start
    rows = [row(t, 2999) for t in (0.0, 2.4, 18.5, 34.6, 50.7, 66.9, 126.9)] + [row(11.0, 2991), row(84.0, 2992)]
    assert ar.process_start(rows, 2999) == 66.9
    # no restart: the refresh 60 s later is not a start
    assert ar.process_start([row(0.0, 2999), row(60.0, 2999), row(120.0, 2999)], 2999) == 0.0
    assert ar.process_start([row(5.0, 2991)], 2999) is None


def test_trim_keeps_the_last_process_in_time_order():
    rows = [row(84.0, 2992), row(0.0, 2999), row(66.9, 2999), row(75.4, 2991), row(50.7, 2999)]
    assert [r["t"] for r in ar.trim_rows(rows, 66.9)] == [66.9, 75.4, 84.0]


def test_first_times_and_slot_problems():
    nodes = [2999, 2991, 2992]
    spacing = ar.slot_spacing(3, 60)                        # 20 s
    rows = [row(100.0, 2999), row(100.0 + spacing, 2991), row(100.0 + 2 * spacing + 0.8, 2992), row(160.0, 2999)]
    first = ar.first_times(rows, nodes)
    assert first == {"2999": 100.0, "2991": 120.0, "2992": 140.8}
    assert ar.slot_problems(first, nodes, 100.0, 60.0) == []
    first["2992"] = 150.0                                   # 10 s late
    bad = ar.slot_problems(first, nodes, 100.0, 60.0)
    assert len(bad) == 1 and "node 2992 (slot 2)" in bad[0] and "+50.0 s" in bad[0]
    assert "no request" in ar.slot_problems({"2999": 100.0}, nodes, 100.0, 60.0)[0]


def test_gaps_and_bounds():
    rows = [row(0.0, 2991), row(60.0, 2991), row(120.5, 2991), row(30.0, 2993)]
    assert ar.gaps(rows, 2991) == [60.0, 60.5] and ar.gaps(rows, 2993) == []
    assert ar.gap_in(60.0, ar.REFRESH_GAP_S) and not ar.gap_in(16.1, ar.REFRESH_GAP_S)
    assert ar.gap_in(124.0, ar.retry_gap_range(1)) and not ar.gap_in(24.1, ar.retry_gap_range(1))     # the 30 s back-off is gone


# ---------------------------------------------------------------- retry rules of wip-1590f26 (ASL1j)

def test_failure_delay_range_is_120_times_2_to_n_minus_1_with_80_120_percent_and_the_clamps():
    assert ar.failure_delay_range(1) == (120.0, 144.0)       # 96..144 clamped up to the 120 s floor (design doc lines 624-625)
    assert ar.failure_delay_range(2) == (192.0, 288.0)       # design doc line 625
    assert ar.failure_delay_range(3) == (384.0, 576.0)
    assert ar.failure_delay_range(4) == (720.0, 900.0)       # base capped at 900 before the entropy, upper bound clamped at 900
    assert ar.failure_delay_range(5) == ar.failure_delay_range(40) == (720.0, 900.0)
    for n in range(1, 12):
        lo, hi = ar.failure_delay_range(n)
        assert 120.0 <= lo <= hi <= 900.0
    with pytest.raises(ValueError):
        ar.failure_delay_range(0)


def test_retry_gap_range_widens_by_the_attempt_duration():
    assert ar.retry_gap_range(1) == (120.0, 145.5)
    assert ar.retry_gap_range(2) == (192.0, 289.5)
    lo, hi = ar.retry_gap_range(1, timeout=True)
    assert (lo, hi) == (120.0 + 14.9, 144.0 + 16.5)
    # the values measured on wip-1590f26 (run 20261107T034616Z): 120.0 s (500), 123.6 (notext), 138.5 (client timeout), 134.3 (oversize)
    assert ar.gap_in(120.0, ar.retry_gap_range(1)) and ar.gap_in(123.6, ar.retry_gap_range(1)) and ar.gap_in(134.3, ar.retry_gap_range(1))
    assert ar.gap_in(138.5, ar.retry_gap_range(1, timeout=True)) and not ar.gap_in(138.5 + 30, ar.retry_gap_range(1, timeout=True))
    assert not ar.gap_in(119.0, ar.retry_gap_range(1)) and not ar.gap_in(30.0, ar.retry_gap_range(1))     # the old 24..36 s window


def test_rate_limited_delay_uses_retry_after_or_doubles_the_interval():
    assert ar.rate_limited_delay(1, 120, 300) == 300.0 and ar.rate_limited_delay(3, 120, 300) == 300.0     # header: no doubling
    assert ar.rate_limited_delay(1, 120, 10) == 120.0                          # bounded below by 120
    assert ar.rate_limited_delay(1, 120, 200000) == 86400.0                    # and above by 86400
    assert [ar.rate_limited_delay(n, 180) for n in range(1, 8)] == [180, 360, 720, 1440, 2880, 3600, 3600]     # design doc lines 66-69
    assert [ar.rate_limited_delay(n, 120) for n in range(1, 8)] == [120, 240, 480, 960, 1920, 3600, 3600]     # the lab's interval
    assert ar.rate_limited_delay(1, 7200) == 7200 and ar.rate_limited_delay(2, 7200) == 7200   # a slow interval is never accelerated
    with pytest.raises(ValueError):
        ar.rate_limited_delay(0, 120)


def test_success_delay_and_exact_gap():
    assert ar.success_delay_s(120) == 120.0 and ar.success_delay_s(600) == 180.0 and ar.success_delay_s(60) == 120.0
    assert ar.gap_with_attempt(300.0) == (299.0, 302.5)


def test_retry_wait_and_window_budget():
    assert ar.retry_wait_s(1) == 145.5 and ar.retry_wait_s(2) == 435.0
    assert ar.retry_wait_s(2, timeout=True) == 465.0
    nodes = [2999] + list(range(2991, 2999))
    budget = ar.window_budget_s(nodes, 120.0)
    # node 2995 (slot 5 at S = 13.33 s) is the slowest: 66.7 + 465 + 30
    assert round(budget, 1) == 561.7 and budget / 60 < 12
    assert ar.window_budget_s([2999, 2991], 120.0) == 60.0 + 121.5 + 30.0          # slot 1, one refresh, margin


def test_limiter_admits_the_first_failure_and_then_one_per_300_s():
    assert ar.limiter_admits([0.0, 120.0, 360.0, 480.0, 720.0]) == [True, False, True, False, True]
    assert ar.limiter_admits([0.0, 299.9, 300.0]) == [True, False, True]
    assert ar.limiter_admits([]) == []
    # the lab: attempt 1 at 0, attempt 2 at 120..144 s (suppressed), attempt 3 at 312..432 s (a line again)
    for second in (120.0, 144.0):
        third = second + ar.failure_delay_range(2)[0]
        assert ar.limiter_admits([0.0, second, third]) == [True, False, True]


def test_lab_expectation_tables_cover_the_failing_nodes():
    assert set(ar.REG_RESULTS) == {"2992", "2993", "2994", "2995", "2996", "2997", "2998"}
    assert set(ar.REG_RESULTS.values()) == {"mismatch", "http", "parse", "timeout", "rate_limited"}
    assert ar.REG_NEEDED["2995"] == 3 and ar.REG_NEEDED["2997"] == 2 and ar.REG_NEEDED["2991"] == 2
