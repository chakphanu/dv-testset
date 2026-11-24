# SPDX-License-Identifier: MIT
"""dvtest/k9c.py (the pure part of cases/test_k9c.py) and rig/config/dv-router/k9c-usrp.toml."""
import tomllib
from pathlib import Path

import pytest

from dvtest import k9c, sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "k9c-usrp.toml"

BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n\n[edges.usrp]\nenable = "auto"\n'


def rendered(gen="k9"):
    return sut_config.render(BASE, "172.30.0", extras=[SNIPPET.read_text(encoding="utf-8")], gen=gen)


def test_snippet_rows_match_the_constants_and_render_for_k9():
    rows = {r["name"]: r for r in tomllib.loads(rendered())["edges"]["usrp"]["endpoint"]}
    assert set(rows) == set(k9c.ROWS)
    for name, (bind, peer, direction, module) in k9c.ROWS.items():
        row = rows[name]
        assert row["bind"] == "172.30.0.11:%d" % bind and row["peer"] == "172.30.0.30:%d" % peer
        assert row["direction"] == direction and row["module"] == module
    # one USRP row per module (the image refuses a second one) and unique callsigns: the k9 renderer adds no name of its own
    assert len({r["module"] for r in rows.values()}) == len(rows)
    assert all("usrp-" not in r["name"] for r in tomllib.loads(rendered())["edges"]["usrp"]["endpoint"])


def test_snippet_keeps_clear_of_the_other_usrp_snippets():
    used = {p for p in (34001, 34002, 34003, 34004, 34101, 32001, 32002, 32003, 32004)}
    for bind, peer, _, module in k9c.ROWS.values():
        assert bind not in used and peer not in used and module not in "ABCEL"


def test_set_direction_replaces_the_row_line_in_place_and_keeps_the_rest():
    text = rendered()
    out = k9c.set_direction(text, "k9c-from", "both")
    assert k9c.row_direction(out, "k9c-from") == "both" and k9c.row_direction(out, "k9c-to") == "to_room"
    assert out.count("direction") == text.count("direction")
    assert tomllib.loads(out)["edges"]["usrp"]["endpoint"][0]["bind"] == "172.30.0.11:34022"
    again = k9c.set_direction(out, "k9c-from", "from_room")
    assert again == text


def test_set_direction_adds_a_missing_line_and_keeps_a_comment():
    text = BASE + '\n[[edges.usrp.endpoint]]\nname = "x1"\nmodule = "A"\ncallsign = "N0A"\nbind = "172.30.0.11:1"\npeer = "172.30.0.30:2"\n' \
        '\n[[edges.usrp.endpoint]]\nname = "x2"\nmodule = "B"\ndirection = "to_room"   # keep\ncallsign = "N0B"\nbind = "172.30.0.11:3"\npeer = "172.30.0.30:4"\n'
    assert k9c.row_direction(text, "x1") == "both"
    out = k9c.set_direction(text, "x1", "from_room")
    assert k9c.row_direction(out, "x1") == "from_room" and k9c.row_direction(out, "x2") == "to_room"
    out = k9c.set_direction(out, "x2", "both")
    assert 'direction = "both"   # keep' in out and k9c.row_direction(out, "x1") == "from_room"


def test_set_direction_refuses_what_it_cannot_place():
    text = rendered()
    with pytest.raises(ValueError, match="direction must be"):
        k9c.set_direction(text, "k9c-from", "up")
    with pytest.raises(ValueError, match="0 .*rows named"):
        k9c.set_direction(text, "nope", "both")
    twice = text + '\n[[edges.usrp.endpoint]]\nname = "k9c-from"\nmodule = "Z"\n'
    with pytest.raises(ValueError, match="2 .*rows named"):
        k9c.set_direction(twice, "k9c-from", "both")
    assert k9c.row_direction(text, "nope") is None


METRICS = """\
# TYPE dv_edge_drops_total counter
dv_edge_drops_total{proc="usrp",reason="deny_tx_acl"} 3
dv_edge_drops_total{proc="usrp",reason="deny_busy"} 1
dv_edge_drops_total{proc="usrp",reason="malformed"} 9
dv_edge_drops_total{proc="m17",reason="deny_tx_acl"} 2
dv_room_deny_total{reason="x"} 5
dv_edge_overs_total{dir="tx",result="complete"} 7
"""


def test_deny_series_keeps_only_deny_samples_and_tx_acl_sums_the_processes():
    series = k9c.deny_text(METRICS)
    names = {(n, dict(l).get("reason")) for n, l in series}
    assert names == {("dv_edge_drops_total", "deny_tx_acl"), ("dv_edge_drops_total", "deny_busy"), ("dv_room_deny_total", "x")}
    assert k9c.tx_acl(series) == 5 and k9c.tx_acl({}) == 0


def test_delta_and_describe():
    before = k9c.deny_text(METRICS)
    after = k9c.deny_text(METRICS.replace('proc="usrp",reason="deny_tx_acl"} 3', 'proc="usrp",reason="deny_tx_acl"} 5'))
    moved = k9c.delta(before, after)
    assert list(moved.values()) == [2.0] and k9c.tx_acl(after) - k9c.tx_acl(before) == 2
    assert k9c.describe(moved) == "dv_edge_drops_total{proc=usrp,reason=deny_tx_acl} +2"
    assert k9c.describe({}) == "nothing moved" and k9c.delta(before, before) == {}
    assert k9c.delta({}, before)[("dv_edge_drops_total", (("proc", "m17"), ("reason", "deny_tx_acl")))] == 2.0


def test_judge_switch_from_room_to_both_wants_silence_before_and_voice_after():
    t0 = 100.0
    good = [t0 + 0.5 + 0.02 * i for i in range(200)]
    bad, facts = k9c.judge_switch(good, t0, 4.0, expect_after=True)
    assert bad == [] and facts["before"] == 0 and facts["first_after"] == 0.5 and facts["after"] == 0 + len([t for t in good if t >= t0 + 4.0])
    bad, _ = k9c.judge_switch([t0 - 1.0] + good, t0, 4.0, expect_after=True)
    assert len(bad) == 1 and "BEFORE" in bad[0]
    bad, facts = k9c.judge_switch([t0 + 0.5 + 0.02 * i for i in range(50)], t0, 4.0, expect_after=True)
    assert len(bad) == 1 and "did not follow" in bad[0]
    bad, facts = k9c.judge_switch([], t0, 4.0, expect_after=True)
    assert bad and facts["first_after"] is None and facts["last"] is None


def test_judge_switch_to_room_to_from_room_wants_voice_before_and_none_after():
    t0 = 100.0
    heard = [t0 - 2.0 + 0.02 * i for i in range(100)]
    bad, facts = k9c.judge_switch(heard, t0, 4.0, expect_after=False)
    assert bad == [] and facts["before"] == 100 and facts["after"] == 0
    bad, _ = k9c.judge_switch(heard + [t0 + 4.5], t0, 4.0, expect_after=False)
    assert len(bad) == 1 and "must stop" in bad[0]
    bad, _ = k9c.judge_switch(heard[:10], t0, 4.0, expect_after=False)
    assert len(bad) == 1 and "only 10" in bad[0]


def test_heard_share():
    assert k9c.heard_share(60, 75) == 0.8 and k9c.heard_share(1, 0) == 0.0


FOLDS = """\
dv_room_overs_total{room="M",result="complete"} 4
dv_room_overs_total{room="M",result="lossy"} 1
dv_edge_overs_total{proc="usrp",dir="rx",result="end_missing"} 2
dv_edge_overs_total{proc="usrp",dir="tx",result="complete"} 7
dv_edge_uptime_seconds{proc="usrp"} 100
dv_room_floor_revoked_total{room="M"} 0
dv_edge_sessions{kind="peer"} 3
"""


def test_all_series_keeps_every_sample_and_folds_selects_the_over_ones():
    before = k9c.all_series(FOLDS)
    assert len(before) == 7
    after = k9c.all_series(FOLDS.replace('result="complete"} 4', 'result="complete"} 5').replace('end_missing"} 2', 'end_missing"} 3')
                           .replace("uptime_seconds{proc=\"usrp\"} 100", "uptime_seconds{proc=\"usrp\"} 130").replace("floor_revoked_total{room=\"M\"} 0", "floor_revoked_total{room=\"M\"} 1"))
    moved = k9c.delta(before, after)
    assert len(moved) == 4
    cut = k9c.folds(moved)
    assert {k[0] for k in cut} == {"dv_room_overs_total", "dv_edge_overs_total", "dv_room_floor_revoked_total"}      # the uptime is not a fold
    assert k9c.fold_results(moved, "dv_room_overs_total") == {"complete": 1.0}
    assert k9c.fold_results(moved, "dv_edge_overs_total", dir="rx") == {"end_missing": 1.0}
    assert k9c.fold_results(moved, "dv_edge_overs_total", dir="tx") == {}
    assert k9c.fold_results({}, "dv_room_overs_total") == {}


def test_new_lines_are_the_ones_after_the_old_text_that_name_a_word():
    old = "a start\nb over\n"
    new = old + "c nothing\nd direction changed\ne OVER cut\n"
    assert k9c.new_lines(old, new) == ["d direction changed", "e OVER cut"]
    assert k9c.new_lines(new, new) == []
