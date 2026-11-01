# SPDX-License-Identifier: MIT
"""dvtest/metrics.py: Prometheus text reader for the hub metrics."""
import math

import pytest

from dvtest import metrics

pytestmark = pytest.mark.unit

TEXT = """\
# HELP dv_room_data_total Data items seen by the room controller.
# TYPE dv_room_data_total counter
dv_room_data_total{room="m",result="accepted",proc="m"} 4
dv_room_data_total{proc="m",result="unidentified",room="m"} 2
dv_room_data_total{room="n",proc="n",result="unidentified"} 1 1761900000000
dv_pkt_items_total{kind="status",result="unidentified",proc="pkt"} 3
dv_pkt_items_total{kind="text",result="unidentified",proc="pkt"} 1
dv_pkt_items_total{kind="text",result="accepted",proc="pkt"} 5
# TYPE dv_up gauge
dv_up 1
dv_odd{note="a \\"quoted\\" } brace, comma",path="c:\\\\x\\ny"} 7
"""


def test_total_is_a_sum_over_a_label_subset():
    assert metrics.total(TEXT, "dv_room_data_total") == 7
    assert metrics.total(TEXT, "dv_room_data_total", result="unidentified") == 3
    assert metrics.total(TEXT, "dv_room_data_total", result="unidentified", room="m") == 2
    assert metrics.total(TEXT, "dv_room_data_total", room="n") == 1
    assert metrics.total(TEXT, "dv_pkt_items_total", kind="text", result="accepted") == 5
    assert metrics.total(TEXT, "dv_room_data_total", result="denied") == 0
    assert metrics.total(TEXT, "dv_nothing") == 0
    assert metrics.total(TEXT, "dv_up") == 1


def test_label_order_does_not_matter():
    a = 'm{x="1",y="2"} 3\n'
    b = 'm{y="2",x="1"} 3\n'
    assert metrics.total(a, "m", x="1", y="2") == metrics.total(b, "m", x="1", y="2") == 3
    assert metrics.total(a, "m", y="2") == 3 and metrics.total(a, "m", y="3") == 0


def test_parse_values_and_escapes():
    samples = {(s.name, tuple(sorted(s.labels.items()))): s.value for s in metrics.parse(TEXT)}
    odd = [s for s in metrics.parse(TEXT) if s.name == "dv_odd"][0]
    assert odd.labels["note"] == 'a "quoted" } brace, comma'
    assert odd.labels["path"] == "c:\\x\ny"
    assert odd.value == 7
    assert len(samples) == 8
    special = metrics.parse("a NaN\nb +Inf\nc -Inf\nd 1e3\ne 0x10\nf\n")
    assert [s.name for s in special] == ["a", "b", "c", "d"]
    assert math.isnan(special[0].value) and special[1].value == math.inf and special[2].value == -math.inf
    assert special[3].value == 1000


def test_malformed_lines_are_skipped_not_fatal():
    text = 'ok 1\n{x="1"} 2\nbad{x=1} 3\nbad2{x="1" 3\nnovalue{x="1"}\nok2{} 4\n# comment\n\n'
    assert [(s.name, s.value) for s in metrics.parse(text)] == [("ok", 1), ("ok2", 4)]
    assert metrics.parse("") == []


def test_nan_does_not_poison_a_sum():
    assert metrics.total("m{a=\"1\"} NaN\nm{a=\"2\"} 2\n", "m") == 2


def test_families_from_help_type_and_samples():
    fams = metrics.families(TEXT)
    assert {"dv_room_data_total", "dv_pkt_items_total", "dv_up", "dv_odd"} <= fams
    # a family that only has HELP / TYPE lines is still a family
    assert metrics.families("# HELP dv_x something\n# TYPE dv_y counter\n") == {"dv_x", "dv_y"}
    assert metrics.families("") == set()
