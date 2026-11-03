# SPDX-License-Identifier: MIT
"""dvtest/asl_register.py: schedule from the config, process start, trimming and the slot arithmetic (no rig)."""
import pytest

from dvtest import asl_register as ar

pytestmark = pytest.mark.unit

TOML = """
[edges.allstar.register]
enable = true
interval = "60s"
[[edges.allstar.node]]
number = 1999
public = true
[[edges.allstar.node]]
number = 1991
public = true
[[edges.allstar.node]]
number = 777
public = false
[[edges.allstar.node]]
number = 1992
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
    assert ar.schedule(TOML) == ([1999, 1991, 1992], 60.0)
    assert ar.schedule("[edges.allstar]\nenable = true\n") == ([], 180.0)


def test_slot_spacing_is_the_interval_over_the_node_count():
    assert round(ar.slot_spacing(7, 60), 2) == 8.57
    assert ar.slot_spacing(9, 180) == 20.0          # XLX345: nine nodes, 180 s
    assert ar.slot_spacing(3, 10) == 20.0           # the interval is at least 60 s
    with pytest.raises(ValueError):
        ar.slot_spacing(0, 60)


def test_row_node_needs_exactly_one_node():
    assert ar.row_node(row(1, 1991)) == "1991"
    assert ar.row_node({"body": {"data": {"nodes": {"1": {}, "2": {}}}}}) is None
    assert ar.row_node({}) is None


def test_process_start_is_the_last_slot0_request_that_follows_too_soon():
    # the failing run of 20261103T232417Z: five quiet restarts, 1999 registers at each process start
    rows = [row(t, 1999) for t in (0.0, 2.4, 18.5, 34.6, 50.7, 66.9, 126.9)] + [row(11.0, 1991), row(84.0, 1992)]
    assert ar.process_start(rows, 1999) == 66.9
    # no restart: the refresh 60 s later is not a start
    assert ar.process_start([row(0.0, 1999), row(60.0, 1999), row(120.0, 1999)], 1999) == 0.0
    assert ar.process_start([row(5.0, 1991)], 1999) is None


def test_trim_keeps_the_last_process_in_time_order():
    rows = [row(84.0, 1992), row(0.0, 1999), row(66.9, 1999), row(75.4, 1991), row(50.7, 1999)]
    assert [r["t"] for r in ar.trim_rows(rows, 66.9)] == [66.9, 75.4, 84.0]


def test_first_times_and_slot_problems():
    nodes = [1999, 1991, 1992]
    spacing = ar.slot_spacing(3, 60)                        # 20 s
    rows = [row(100.0, 1999), row(100.0 + spacing, 1991), row(100.0 + 2 * spacing + 0.8, 1992), row(160.0, 1999)]
    first = ar.first_times(rows, nodes)
    assert first == {"1999": 100.0, "1991": 120.0, "1992": 140.8}
    assert ar.slot_problems(first, nodes, 100.0, 60.0) == []
    first["1992"] = 150.0                                   # 10 s late
    bad = ar.slot_problems(first, nodes, 100.0, 60.0)
    assert len(bad) == 1 and "node 1992 (slot 2)" in bad[0] and "+50.0 s" in bad[0]
    assert "no request" in ar.slot_problems({"1999": 100.0}, nodes, 100.0, 60.0)[0]


def test_gaps_and_bounds():
    rows = [row(0.0, 1991), row(60.0, 1991), row(120.5, 1991), row(30.0, 1993)]
    assert ar.gaps(rows, 1991) == [60.0, 60.5] and ar.gaps(rows, 1993) == []
    assert ar.gap_in(60.0, ar.REFRESH_GAP_S) and not ar.gap_in(16.1, ar.REFRESH_GAP_S)
    assert ar.gap_in(24.1, ar.RETRY_GAP_S) and not ar.gap_in(23.9, ar.RETRY_GAP_S)
    assert ar.gap_in(47.8, ar.SLOW_GAP_S) and not ar.gap_in(60.0, ar.SLOW_GAP_S)
