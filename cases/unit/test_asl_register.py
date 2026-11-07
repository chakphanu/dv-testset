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
    assert round(ar.slot_spacing(7, 120), 2) == 17.14        # the lab since wip-1590f26: seven nodes, "120s"
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
    assert ar.gap_in(24.1, ar.RETRY_GAP_S) and not ar.gap_in(23.9, ar.RETRY_GAP_S)
    assert ar.gap_in(47.8, ar.SLOW_GAP_S) and not ar.gap_in(60.0, ar.SLOW_GAP_S)
