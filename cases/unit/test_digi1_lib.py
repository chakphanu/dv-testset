# SPDX-License-Identifier: MIT
"""dvtest/digi1.py: the DIGI1 model (ten decision steps, duplicate table, timeline, key rules), the flood bounds, the counters and their conservation, the
shape of the rendered config. Offline, no rig. The model is also checked against DV-Router's own hand-built fixtures in cases/unit/test_ax25_digi.py."""
import tomllib
from pathlib import Path

import pytest

from dvtest import ax25, digi1, metrics

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
VECTORS = list(digi1.GOLDENS) + list(digi1.extra_vectors())


@pytest.mark.parametrize("g", VECTORS, ids=[g.id for g in VECTORS])
def test_every_vector_decides_as_its_table_row_says(g):
    frame = digi1.golden_frame(g, b" suffix")
    result, out = digi1.decide(frame, g.ours)
    assert result == g.result
    if g.result == "repeat":
        assert out == ax25.replace_hop(frame, g.replaced, g.ours) == digi1.golden_expect(g, frame)
        assert not digi1.repeat_problems(frame, out)
    else:
        assert out is None and digi1.golden_expect(g) is None


def test_the_vector_ids_are_unique_and_the_documented_deviations_are_marked():
    ids = [g.id for g in VECTORS]
    assert len(ids) == len(set(ids)) and {"G%d" % n for n in range(1, 14)} <= set(ids)
    deviations = {g.id for g in digi1.GOLDENS if g.dw.startswith("differs")}
    assert deviations == {"G9", "G10", "G11"}          # design-271 5.7 and data.md 15.5: own source, non-contiguous H, Internet path
    assert all(g.ours == digi1.OURS for g in VECTORS)


def test_g2b_is_a_station_without_ssid_and_not_part_of_the_rig_table():
    assert digi1.G2B not in digi1.GOLDENS and digi1.G2B.ours == "N0TEST"
    frame = digi1.golden_frame(digi1.G2B)
    result, out = digi1.decide(frame, "N0TEST")
    assert result == "repeat" and ax25.hops(out)[0] == ("N0TEST", True, 3)


def test_step_order_the_first_matching_step_wins():
    d = lambda hops, src="N0TEST-9", ours=digi1.OURS, **kw: digi1.decide(ax25.build_ui("APRS", src, hops, b">x"), ours, **kw)[0]    # noqa: E731
    assert d([]) == "no_match"
    assert d([("WIDE1-1", True)]) == "no_match"                                    # all used: step 1 before everything
    assert d(["WIDE2-1", ("TCPIP", True)]) == "bad_path"                           # step 2 before the Internet marker of step 3
    assert d(["WIDE1-1", "TCPIP"]) == "internet_path"
    assert d(["WIDE1-1", "TCPIP"], src=digi1.OURS) == "internet_path"              # step 3 before own_source
    assert d(["WIDE1-1"], src=digi1.OURS) == "own_source"
    assert d([(digi1.OURS, True), "WIDE1-1"], src=digi1.OURS) == "own_source"      # step 4 before own_path
    assert d([(digi1.OURS, True), "WIDE1-1"]) == "own_path"
    assert d([(digi1.OURS, True), "WIDE2-1"]) == "own_path"                        # step 5 before the first-hop test of step 6
    assert d(["WIDE2-1"]) == "no_match"
    assert d(["WIDE1-1"], own_sources=("N0TEST-9",)) == "own_source"
    assert d([digi1.OURS]) == "repeat"


def test_dedupe_table_window_from_first_acceptance_boundary_and_capacity():
    table = digi1.DedupeTable(30)
    assert table.seen("k", 0.0) == "new"
    table.remember("k", 0.0)
    assert table.seen("k", 10.0) == "duplicate" and table.seen("k", 29.999) == "duplicate"
    assert table.seen("k", 30.0) == "new"                        # exactly at the boundary the key is new
    table.remember("k", 30.0)
    assert table.seen("k", 59.0) == "duplicate"
    full = digi1.DedupeTable(30, capacity=3)
    for i in range(3):
        full.remember(i, 0.0)
    assert full.seen("x", 1.0) == "full" and full.seen(1, 1.0) == "duplicate"
    assert full.seen("x", 31.0) == "new"                         # the entries expired


def test_a_duplicate_observation_does_not_refresh_the_window():
    table = digi1.DedupeTable(30)
    frame = ax25.build_ui("APRS", "N0TEST-9", ["WIDE1-1"], b">x")
    assert digi1.decide(frame, digi1.OURS, table, 0.0)[0] == "repeat"
    assert digi1.decide(frame, digi1.OURS, table, 20.0)[0] == "duplicate"
    assert digi1.decide(frame, digi1.OURS, table, 31.0)[0] == "repeat"      # 31 s after the FIRST acceptance, 11 s after the duplicate


def test_a_fenced_frame_is_not_remembered_and_a_full_table_fails_closed():
    table = digi1.DedupeTable(30)
    frame = ax25.build_ui("APRS", "N0TEST-9", ["WIDE1-1"], b">x")
    assert digi1.decide(frame, digi1.OURS, table, 0.0, fenced=True) == ("fenced", None)
    assert digi1.decide(frame, digi1.OURS, table, 1.0)[0] == "repeat"      # DIGI-R5: the retry after the guard repeats
    small = digi1.DedupeTable(30, capacity=1)
    assert digi1.decide(frame, digi1.OURS, small, 0.0)[0] == "repeat"
    other = ax25.build_ui("APRS", "N0TEST-8", ["WIDE1-1"], b">x")
    assert digi1.decide(other, digi1.OURS, small, 1.0)[0] == "dedupe_full"
    assert digi1.decide(frame, digi1.OURS, small, 2.0)[0] == "duplicate"


def test_timeline_expect_and_the_default_times():
    assert digi1.TIMELINE_S == (0.0, 10.0, 29.0, 31.0, 45.0)
    assert digi1.timeline_expect() == ["repeat", "duplicate", "duplicate", "repeat", "duplicate"]
    assert digi1.timeline_expect(window_s=10) == ["repeat", "repeat", "repeat", "duplicate", "repeat"]       # a shorter window repeats more often
    assert digi1.timeline_expect((0, 5, 12, 13), window_s=10) == ["repeat", "duplicate", "repeat", "duplicate"]


def test_key_vectors_agree_with_the_model_and_with_each_other():
    table = digi1.DedupeTable(30)
    rows = digi1.key_vectors(tag=b" t1")
    assert [r[0] for r in rows] == ["K%d" % n for n in range(8)]
    for kid, _what, frame, expect in rows:
        assert digi1.decide(frame, digi1.OURS, table, 1.0)[0] == expect, kid
    assert digi1.dedupe_key(rows[2][2]) == digi1.dedupe_key(rows[0][2])           # CR LF and spaces are not part of the key
    assert digi1.dedupe_key(rows[1][2]) == digi1.dedupe_key(rows[0][2])           # neither is the path
    assert len({digi1.dedupe_key(r[2]) for r in rows}) == 5


def test_repeat_problems_flags_growth_identity_and_stray_bytes():
    frame = ax25.build_ui("APRS", "N0TEST-9", ["WIDE1-1", "WIDE2-1"], b">x")
    good = ax25.replace_hop(frame, 0, digi1.OURS)
    assert digi1.repeat_problems(frame, good) == []
    assert "path must not grow" in digi1.repeat_problems(frame, good + b"x")[0]
    assert "byte-identical" in digi1.repeat_problems(frame, frame)[0]
    stray = bytearray(good)
    stray[-1] ^= 1
    assert "outside one via address" in digi1.repeat_problems(frame, bytes(stray))[0]
    two = ax25.replace_hop(ax25.replace_hop(frame, 0, digi1.OURS), 1, "N0TEST-3")
    assert "outside one via address" in digi1.repeat_problems(frame, two)[0]


# ---------------------------------------------------------------- counters

def sample(name, value, **labels):
    return metrics.Sample(name, labels, float(value))


def test_digi_counts_and_rf_tx_digi_read_one_port_and_one_kind():
    s = [sample("dv_edge_digi_total", 3, port="rf-a", result="repeat"), sample("dv_edge_digi_total", 2, port="rf-a", result="duplicate"),
         sample("dv_edge_digi_total", 9, port="rf-b", result="repeat"),
         sample("dv_edge_rf_tx_total", 3, port="rf-a", kind="digi", result="sent"), sample("dv_edge_rf_tx_total", 1, port="rf-a", kind="message", result="sent"),
         sample("dv_edge_rf_tx_total", 2, port="rf-a", kind="digi", result="dropped_ttl"), sample("dv_edge_rf_rejected_total", 4, result="busy"),
         sample("dv_edge_drops_total", 5, reason="rf_fenced"), sample("dv_edge_rf_queue", 2, port="rf-a")]
    counts = digi1.digi_counts(s, "rf-a")
    assert counts["repeat"] == 3 and counts["duplicate"] == 2 and counts["fenced"] == 0 and set(counts) == set(digi1.RESULTS)
    assert digi1.rf_tx_digi(s, "rf-a") == {"sent": 3, "dropped_ttl": 2}
    assert digi1.rf_tx_kinds(s, "rf-a") == ["digi", "message"] and digi1.rf_tx_kind_result(s, "rf-a", "message", "sent") == 1
    assert digi1.rejected_total(s) == 4 and digi1.rf_fenced_total(s) == 5 and digi1.queue_len(s, "rf-a") == 2 and digi1.queue_len(s, "rf-b") is None


def test_the_nine_results_are_the_documented_ones():
    assert set(digi1.RESULTS) == {"repeat", "duplicate", "own_source", "own_path", "internet_path", "bad_path", "no_match", "dedupe_full", "fenced"}
    assert set(digi1.TERMINAL_TX) == {"sent", "simulated", "sent_over_budget", "dropped_ttl", "dropped_queue", "dropped_exceeds_quota", "dropped_too_long"}
    assert "delayed" not in digi1.TERMINAL_TX


def test_expected_counts_match_and_mismatches_are_named():
    exp = digi1.Expected()
    exp.add("repeat", b"x")
    exp.add("duplicate")
    exp.add("duplicate")
    assert exp.nonzero() == {"repeat": 1, "duplicate": 2} and exp.total() == 3
    assert digi1.count_problems(exp, {"repeat": 1, "duplicate": 2}) == []
    assert "repeat" in digi1.count_problems(exp, {"repeat": 0, "duplicate": 2})[0]
    assert "outside the documented nine" in digi1.count_problems(exp, {"repeat": 1, "duplicate": 2, "viscous": 1})[0]
    with pytest.raises(ValueError):
        exp.add("viscous")


def test_conservation_of_digi_copies():
    ok = digi1.conservation_problems({"repeat": 15}, {"sent": 11, "dropped_ttl": 4, "delayed": 4}, rejected=0, queued=0, rf_fenced=0)
    assert ok == []                                              # delayed is not terminal and is not added
    assert digi1.conservation_problems({"repeat": 15}, {"sent": 11, "dropped_ttl": 3}, 0, 1, 0) == []      # one copy still queued
    assert digi1.conservation_problems({"repeat": 15}, {"sent": 10}, 1, 0, 0)                            # 4 unaccounted
    assert digi1.conservation_problems({"repeat": 5}, {"sent": 6}, 0, 0, 0)                              # more results than copies
    assert digi1.conservation_problems({"repeat": 5, "fenced": 1}, {"sent": 4}, 0, 0, 2) == []          # one copy canceled by a fence (rf_fenced 2 = 1 fenced + 1 canceled)
    assert digi1.conservation_problems({"repeat": 0, "fenced": 2}, {}, 0, 0, 1)                         # digi{fenced} above rf_fenced


def test_restart_counts_match_reset_and_continued():
    exp = {"fenced": 2, "repeat": 1, "duplicate": 1}
    before = {r: 0 for r in digi1.RESULTS}
    before.update(repeat=14, no_match=9)
    assert digi1.restart_counts_match(before, dict(exp), exp) == "reset"
    cont = dict(before)
    for r, n in exp.items():
        cont[r] = cont.get(r, 0) + n
    assert digi1.restart_counts_match(before, cont, exp) == "continued"
    assert digi1.restart_counts_match(before, dict(before), exp) is None
    assert digi1.restart_counts_match({}, dict(exp), exp) == "reset"


# ---------------------------------------------------------------- the flood

def test_flood_plan_is_sixty_distinct_eligible_frames_from_ten_sources():
    plan = digi1.flood_plan("1234")
    assert len(plan) == 60 and plan[0][0] == 0.0 and plan[-1][0] == pytest.approx(59.0)
    sources = digi1.flood_sources()
    assert len(sources) == 10 and len(set(sources)) == 10 and digi1.OURS not in sources
    assert {p[1] for p in plan} == set(sources) and all(sum(1 for p in plan if p[1] == s) == 6 for s in sources)
    assert len({digi1.dedupe_key(p[3]) for p in plan}) == 60
    table = digi1.DedupeTable()
    assert all(digi1.decide(p[3], digi1.OURS, table, p[0])[0] == "repeat" for p in plan)


def injected(plan, t0=1000.0):
    return [(t0 + t, src, info, frame) for t, src, info, frame in plan]


def test_flood_within_the_digi_class_budget_has_no_problems():
    plan = injected(digi1.flood_plan("1"))
    written = [(t + 0.5, ax25.replace_hop(frame, 0, digi1.OURS)) for t, _s, _i, frame in plan[:11]]
    # 11 copies, one per second as injected: about 10 s of airtime in a 10 s class bucket
    assert digi1.flood_problems(plan, written) == []


def test_flood_flags_too_much_airtime_late_copies_duplicates_wrong_bytes_and_gap():
    plan = injected(digi1.flood_plan("1"))
    all_copies = [(t + 0.5, ax25.replace_hop(frame, 0, digi1.OURS)) for t, _s, _i, frame in plan]
    problems = digi1.flood_problems(plan, all_copies)
    assert any("digi class holds" in p for p in problems), problems           # 60 copies of about 0.9 s each cannot fit 10 s
    late = [(plan[0][0] + 9.0, ax25.replace_hop(plan[0][3], 0, digi1.OURS))]
    assert any("digi TTL" in p for p in digi1.flood_problems(plan, late))
    dup = [(plan[0][0] + 0.5, ax25.replace_hop(plan[0][3], 0, digi1.OURS))] * 2
    assert any("twice" in p for p in digi1.flood_problems(plan, dup))
    wrong = [(plan[0][0] + 0.5, plan[0][3])]
    assert any("expected" in p for p in digi1.flood_problems(plan, wrong))
    stranger = [(plan[0][0] + 0.5, ax25.build_ui("APRS", "N0TEST-3", [(digi1.OURS, True)], b">nobody"))]
    assert any("matches no injected frame" in p for p in digi1.flood_problems(plan, stranger))
    close = [(plan[0][0] + 0.5, ax25.replace_hop(plan[0][3], 0, digi1.OURS)), (plan[1][0] + 0.2, ax25.replace_hop(plan[1][3], 0, digi1.OURS))]
    assert any("minimum gap" in p for p in digi1.flood_problems(plan, close))


def test_class_and_port_capacity_follow_the_budget():
    assert digi1.class_capacity_s() == pytest.approx(10.0)                    # 4/12 of 5 % of 600 s
    assert digi1.class_capacity_s({"duty_pct": 20}) == pytest.approx(40.0)
    assert digi1.port_capacity_s() == pytest.approx(30.0) and digi1.port_capacity_s({"duty_pct": 20}) == pytest.approx(120.0)
    assert digi1.frame_airtime_ns(47) == 300_000_000 + 30_000_000 + 51 * 8 * 6 * 10**9 // (1200 * 5)


# ---------------------------------------------------------------- the rendered config

def rendered(name):
    return tomllib.loads((SNIPPETS / name).read_text().replace("<TESTER_IP>", "172.30.0.30").replace("<NET>", "172.30.0"))


def test_floor_snippet_is_the_rate_floor_with_class_defaults_and_the_room_path():
    doc = rendered("digi1.toml")
    shape = digi1.read_shape(doc)
    assert shape.name == "floor" and shape.problems == [] and shape.class_capacity_s == pytest.approx(10.0)
    row = shape.row
    assert (row["tx_per_min"], row["tx_burst"], row["tx_budget"], row["digi"]) == (10, 3, {}, {"enable": True})
    assert doc["edges"]["ax25"]["callsign"] == digi1.OURS and doc["edges"]["ax25"]["alias_calls"] == []
    assert [p["name"] for p in doc["data"]["path"]] == ["rf-out"] and doc["data"]["path"][0]["to"] == "edge:ax25/rf-a"
    other = next(p for p in doc["edges"]["ax25"]["port"] if p["name"] == "rf-b")
    assert other["tx"] is False and "digi" not in other


def test_roomy_and_dw_snippets_have_a_big_class_bucket_and_no_path():
    for name in ("digi1-roomy.toml", "digi1-dw.toml"):
        doc = rendered(name)
        shape = digi1.read_shape(doc)
        assert shape.name == "roomy" and shape.problems == [] and shape.class_capacity_s == pytest.approx(40.0), name
        assert "path" not in (doc.get("data") or {})
    assert [p["host"] for p in rendered("digi1-dw.toml")["edges"]["ax25"]["port"]] == ["172.30.0.48", "172.30.0.49"]
    assert [p["port"] for p in rendered("digi1-roomy.toml")["edges"]["ax25"]["port"]] == [8101, 8102]


def test_read_shape_reports_what_is_missing():
    assert digi1.read_shape({}).name == "none"
    doc = rendered("digi1-roomy.toml")
    doc["edges"]["ax25"]["tx_enable"] = False
    assert digi1.read_shape(doc).problems
    doc = rendered("digi1-roomy.toml")
    del doc["edges"]["ax25"]["port"][0]["digi"]
    assert digi1.read_shape(doc).name == "none"
    doc = rendered("digi1-roomy.toml")
    doc["edges"]["ax25"]["port"][0]["tx_budget"] = {"duty_pct": 5}
    doc["edges"]["ax25"]["port"][0]["tx_per_min"] = 12
    assert digi1.read_shape(doc).name == "other"
