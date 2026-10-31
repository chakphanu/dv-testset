# SPDX-License-Identifier: MIT
"""The P19 edge relink bound lives in the dvrouter spec (cases/test_edge_relink.py judges it)."""
import pytest

from dvtest.specs import dvrouter

pytestmark = pytest.mark.unit


def test_relink_spec_entries():
    entries = dvrouter.SPEC["relink"]
    assert sorted(entries) == ["P-RLK-01", "P-RLK-02", "P-RLK-03"]
    assert [entries[k]["edge"] for k in sorted(entries)] == ["dv-edge-xlx", "dv-edge-nxdn", "dv-edge-p25"]
    assert all(e["max_s"] == 10 for e in entries.values())
