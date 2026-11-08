# SPDX-License-Identifier: MIT
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_web_info_names as t  # noqa: E402

pytestmark = pytest.mark.unit


def test_info_room_entry_reads_the_array():
    state = {"info": {"rooms": [{"room": "A", "name": "x"}, {"room": "B", "name": "Example room"}]}}
    assert t.info_room_entry(state, "B") == {"room": "B", "name": "Example room"}
    assert t.info_room_entry(state, "C") is None
    assert t.info_room_entry({"info": {"rooms": {"B": "Example room"}}}, "B") is None      # a map is not the documented shape
    assert t.info_room_entry({}, "B") is None
