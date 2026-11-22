# SPDX-License-Identifier: MIT
"""dvtest/rts1.py: image marker, result map, conservation. Pure, no rig."""
import pytest

from dvtest import metrics, rts1

pytestmark = pytest.mark.unit

NEW = """# HELP dv_room_rts_total RTS processing, by result
# TYPE dv_room_rts_total counter
dv_room_rts_total{proc="a",room="a",result="grant"} 3
dv_room_rts_total{proc="a",room="a",result="deny"} 2
dv_room_rts_total{proc="a",room="a",result="busy_delayed"} 0
dv_room_rts_total{proc="a",room="a",result="duplicate"} 0
dv_room_rts_total{proc="a",room="a",result="clock_domain"} 0
dv_room_rts_total{proc="a",room="a",result="no_winner"} 1
dv_room_rts_total{proc="b",room="b",result="grant"} 9
"""


def test_strict_switch():
    assert rts1.strict_on({"DVT_RTS1": "on"})
    assert not rts1.strict_on({"DVT_RTS1": ""})


def test_image_marker():
    assert rts1.image_has_rts1(NEW) is True
    assert rts1.image_has_rts1(NEW.replace(rts1.NEW_HELP, rts1.OLD_HELP)) is False
    assert rts1.image_has_rts1("# nothing\n") is None


def test_room_results_by_letter():
    got = rts1.room_results(metrics.parse(NEW), "a")
    assert got["grant"] == 3 and got["no_winner"] == 1 and "link_down" not in got
    assert rts1.room_results(metrics.parse(NEW), "b") == {"grant": 9}


def test_conservation():
    assert rts1.conservation_problems({"deny": 2, "no_winner": 1}) == []
    assert rts1.conservation_problems({"deny": 0, "no_winner": 1})
    assert rts1.conservation_problems({"grant": -1})


def test_fixed_series_missing():
    assert rts1.fixed_series_missing(rts1.room_results(metrics.parse(NEW), "a")) == []
    assert rts1.fixed_series_missing({"grant": 1}) == ["deny", "busy_delayed", "duplicate", "clock_domain"]
