# SPDX-License-Identifier: MIT
"""dvtest.tools.late_report and dvtest.tools.thread_sample (pure parts), webload_gen late_bins constants."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dvtest.tools import late_report as lr  # noqa: E402
from dvtest.tools import thread_sample as ts  # noqa: E402

pytestmark = pytest.mark.unit


def test_clusters_join_neighbours_and_keep_the_big_ones():
    bins = {1000: [30, 12.0], 1010: [10, 9.0], 2002: [25, 11.0], 3001: [40, 10.5], 3500: [2, 6.0]}
    got = lr.clusters(bins, 20)
    assert got == [(1000, 1010, 40, 12.0), (2002, 2002, 25, 11.0), (3001, 3001, 40, 10.5)]
    assert lr.intervals(got) == [1002, 999]


def test_busy_around_counts_ticks_in_the_window(tmp_path):
    path = tmp_path / "threads.txt"
    path.write_text("\n".join([
        "1000 11 tokio-rt 5 5 S", "1100 11 tokio-rt 6 6 S", "1200 11 tokio-rt 9 9 R",
        "1000 12 net-0 1 1 S", "1100 12 net-0 1 1 S", "1200 12 net-0 1 1 S"]) + "\n")
    busy = lr.busy_around(lr.thread_busy(str(path)), 1100, 1100, margin_ms=100)
    assert busy[11] == ("tokio-rt", 8) and busy[12] == ("net-0", 0)


def test_read_threads_of_this_process():
    rows = ts.read_threads(os.getpid())
    assert rows and all(len(r) == 5 for r in rows)
    assert any(r[0] == os.getpid() for r in rows)


def test_late_constants_are_sane():
    from dvtest.tools import webload_gen as gen
    assert 0 < gen.LATE_MS < 20 and gen.LATE_BIN_MS > 0 and gen.LATE_BINS_MAX >= 1000
