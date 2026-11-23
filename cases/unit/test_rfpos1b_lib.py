# SPDX-License-Identifier: MIT
"""RFPOS1 step B (docs/rfpos1b-cases.md): dvtest/rfpos1b.py (readings, settle waits, the verdict) from hub metrics text."""
import pytest

from dvtest import metrics, rfpos1b as r


def hub_text(room_over=0, rx_gap=0, room_gap=0, room_lost=0, lossy_room=0, egress=None):
    """A hub export: the YSF ingress edge, room y and the egress edges of `egress` {proc: dict(lost, late, dropped, complete, lossy, end_missing, joined)}."""
    lines = ['dv_edge_over_units_total{dir="rx",proc="ysf",result="%s"} %g' % (k, v) for k, v in (("gap", rx_gap), ("late", 0), ("dropped", 0))]
    lines += ['dv_room_over_units_total{room="y",result="gap"} %g' % room_gap, 'dv_room_over_units_total{room="y",result="lost"} %g' % room_lost,
              'dv_room_over_units_total{room="y",result="late"} 0',
              'dv_room_overs_total{room="y",result="complete"} %g' % room_over, 'dv_room_overs_total{room="y",result="lossy"} %g' % lossy_room,
              'dv_room_overs_total{room="y",result="end_missing"} 0']
    for proc, values in (egress or {}).items():
        for result in ("lost", "late", "dropped"):
            lines.append('dv_edge_over_units_total{dir="tx",proc="%s",result="%s"} %g' % (proc, result, values.get(result, 0)))
        for result in ("complete", "lossy", "end_missing", "joined"):
            lines.append('dv_edge_overs_total{dir="tx",proc="%s",result="%s"} %g' % (proc, result, values.get(result, 0)))
        lines.append('dv_edge_drops_total{proc="%s",reason="x_stale"} 0' % proc)
    return "\n".join(lines) + "\n"


PROCS = ["ysf", "dmrhb"]


def read(**kw):
    return r.reading(metrics.parse(hub_text(**kw)), PROCS)


CLEAN = dict(room_over=1, egress={"ysf": dict(complete=1), "dmrhb": dict(complete=1)})
FALSE_GAP = dict(room_over=1, lossy_room=0, egress={"ysf": dict(lossy=1, lost=1), "dmrhb": dict(lossy=1, lost=1)})


def test_switch():
    assert r.on({"DVT_RFPOS1B": "on"}) and r.on({"DVT_RFPOS1B": " 1 "}) and not r.on({}) and not r.on({"DVT_RFPOS1B": "off"})


def test_reading_and_delta_per_edge():
    before = read(room_over=4, egress={"ysf": dict(complete=2, lost=1), "dmrhb": dict(complete=3)})
    after = read(room_over=5, egress={"ysf": dict(complete=3, lost=1), "dmrhb": dict(complete=4, lost=2)})
    change = r.delta(after, before)
    assert change.room.overs_complete == 1 and change.room.edge_gap == 0
    got = dict(change.egress)
    assert got["ysf"].complete == 1 and got["ysf"].lost == 0 and got["dmrhb"].lost == 2
    assert r.egress_folds(change, "dmrhb") == 1 and r.egress_folds(change, "ysf") == 1


def test_clean_over_has_no_problem():
    verdict = r.judge(r.delta(read(**CLEAN), read()), fold_procs=("dmrhb",))
    assert verdict.problems == [] and verdict.findings == []
    assert verdict.facts["egress"]["dmrhb"]["complete"] == 1


def test_false_end_of_over_gap_on_every_egress_edge_is_named_per_edge():
    verdict = r.judge(r.delta(read(**FALSE_GAP), read()), fold_procs=("dmrhb",))
    text = " | ".join(verdict.problems)
    assert "proc=ysf,result=lost" in text and "proc=dmrhb,result=lost" in text
    assert "proc=ysf,result=lossy" in text and "proc=dmrhb,result=lossy" in text


def test_room_level_gap_lost_and_lossy_are_problems():
    verdict = r.judge(r.delta(read(room_over=1, room_gap=2, room_lost=3, lossy_room=1, rx_gap=1, egress={"ysf": {}, "dmrhb": dict(complete=1)}), read()), fold_procs=("dmrhb",))
    text = " | ".join(verdict.problems)
    assert "result=gap} rose by 1" in text and "room=y,result=lost" in text and "lossy" in text
    assert len(verdict.problems) >= 4


def test_no_closed_over_is_a_problem_and_late_dropped_are_findings_only():
    verdict = r.judge(r.delta(read(egress={"ysf": dict(late=2, dropped=1), "dmrhb": {}}), read()))
    assert any("no over closed" in p for p in verdict.problems)
    assert len(verdict.findings) == 1 and "ysf" in verdict.findings[0] and not any("late" in p for p in verdict.problems)


def test_a_cross_edge_that_never_folded_is_a_harness_slip():
    change = r.delta(read(room_over=1, egress={"ysf": dict(complete=1), "dmrhb": {}}), read())
    assert any("dmrhb folded no over" in p for p in r.judge(change, fold_procs=("dmrhb",)).problems)
    assert r.judge(change, fold_procs=()).problems == []                      # a YSF listener may be served by the fast path and fold none


def test_procs_missing_names_what_the_hub_has():
    samples = metrics.parse(hub_text(**CLEAN))
    assert r.procs_missing(samples, ["ysf", "dextra"]) == (["dextra"], ["dmrhb", "ysf"])


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_settled_waits_for_the_room_and_the_cross_fold_then_reads_once_more():
    clock = Clock()
    steps = [read(), read(room_over=1, egress={"ysf": dict(complete=1), "dmrhb": {}}), read(**CLEAN), read(**dict(CLEAN, room_over=1))]
    reads = []

    def reader():
        reads.append(clock.now)
        return steps[min(len(reads) - 1, len(steps) - 1)]

    final = r.wait_settled(reader, read(), fold_procs=("dmrhb",), clock=clock.time, sleep=clock.sleep)
    assert r.egress_folds(final, "dmrhb") == 1 and len(reads) == 4
    assert reads[-1] - reads[-2] == r.QUIET_S                                  # one more read after the quiet period


def test_wait_settled_times_out_naming_the_missing_fold():
    clock = Clock()
    with pytest.raises(TimeoutError) as exc:
        r.wait_settled(lambda: read(room_over=1, egress={"ysf": dict(complete=1), "dmrhb": {}}), read(), fold_procs=("dmrhb",), budget_s=20.0,
                       clock=clock.time, sleep=clock.sleep)
    assert "dmrhb" in str(exc.value)


def test_stable_baseline_returns_the_first_unchanged_read():
    clock = Clock()
    values = [read(room_over=1), read(room_over=2), read(room_over=2)]
    seen = []

    def reader():
        seen.append(1)
        return values[min(len(seen) - 1, 2)]

    got = r.stable_baseline(reader, clock=clock.time, sleep=clock.sleep)
    assert medload_total(got) == 2 and len(seen) == 3


def medload_total(reading):
    return reading.room.overs_complete
