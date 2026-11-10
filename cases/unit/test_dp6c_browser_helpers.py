# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp6c_browser.py (DP6c in browsers): the details id of positions-station.js, the Thai catalog strings, the table column indexes against the
page source strings. Needs the playwright package for the import of the case module only (the tester image has it; skipped elsewhere)."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp6c_browser as tb  # noqa: E402

from dvtest import dp6c  # noqa: E402

pytestmark = pytest.mark.unit


def test_the_details_id_is_encode_uri_component_of_room_and_guard_identity():
    assert tb.station_details_id("B:N0WBA-13") == "position-details-B%3AN0WBA-13"
    assert tb.station_details_id("B:N0WBA-13/P") == "position-details-B%3AN0WBA-13%2FP"
    assert tb.station_details_id("B:A B") == "position-details-B%3AA%20B"


def test_the_thai_strings_are_the_catalog_escapes_and_the_columns_follow_positions_js():
    assert tb.TH["Show details"][0] == chr(0x0e41) and all(v and v.isprintable() and not v.isascii() for v in tb.TH.values())
    assert (tb.COL_CALLSIGN, tb.COL_LAT, tb.COL_LON, tb.COL_WEATHER, tb.COL_DETAILS) == (0, 1, 2, 5, 6)
    assert tb.WX_CALL.split("-")[0] != tb.TLM_CALL.split("-")[0] and tb.SOLO_CALL.startswith("N0WB")
    assert dp6c.weather_summary(dp6c.G5_WEATHER) == "25.0 C 6 km/h"


def test_the_page_source_files_of_this_module_are_ascii():
    for name in ("test_dp6c_browser.py", "test_dp6c_rf.py", "test_dp6c_aprsis.py", "test_dp19_locator.py", "test_dp6c_config.py", "dp6c_support.py"):
        text = (Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8")
        assert text.isascii(), name
