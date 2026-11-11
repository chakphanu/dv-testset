# SPDX-License-Identifier: MIT
"""dvtest/refusals.py (WEBSTALL1): the counter families, the refusal contract and the load harness retry rule. Pure logic, no rig."""
import json

import pytest

from dvtest import metrics, refusals as rf

pytestmark = pytest.mark.unit


def text(edge=None, web=None, proc_edge="webrtc", proc_web="web"):
    """A hub exposition with every reason of both families (zeros included) unless overridden."""
    edge, web = dict(edge or {}), dict(web or {})
    lines = ["# HELP %s refused" % rf.EDGE_FAMILY, "# TYPE %s counter" % rf.EDGE_FAMILY]
    for r in rf.EDGE_REASONS:
        lines.append('%s{proc="%s",reason="%s"} %s' % (rf.EDGE_FAMILY, proc_edge, r, edge.get(r, 0)))
    for r in rf.WEB_REASONS:
        lines.append('%s{proc="%s",reason="%s"} %s' % (rf.WEB_FAMILY, proc_web, r, web.get(r, 0)))
    lines.append('%s{proc="%s",reason="core"} 0' % (rf.CORE_FAMILY, proc_edge))
    return "\n".join(lines) + "\n"


def snap(**kw):
    return rf.snapshot(metrics.parse(text(**kw)))


def test_the_reason_sets_are_the_documented_ones():
    assert len(rf.EDGE_REASONS) == 10 and len(rf.WEB_ONLY_REASONS) == 7 and len(rf.WEB_REASONS) == 17
    assert rf.WEB_REASONS[:10] == rf.EDGE_REASONS and len(set(rf.WEB_REASONS)) == 17
    assert set(rf.DETAILS) == set(rf.WEB_REASONS)
    assert all(d.startswith("WebRTC ") and d.endswith(" capacity reached") for d in rf.DETAILS.values())
    assert len(set(rf.DETAILS.values())) == 17                      # one fixed string per limit
    assert rf.DETAILS["login_circuits"] == "WebRTC login circuit capacity reached"
    assert rf.DETAILS["join_window"] == "WebRTC join window capacity reached"


def test_a_complete_exposition_has_no_shape_problems_and_zeros_count_as_present():
    s = snap()
    assert set(s["edge"]) == set(rf.EDGE_REASONS) and set(s["web"]) == set(rf.WEB_REASONS)
    assert all(v == 0 for v in s["edge"].values())
    assert rf.shape_problems(s) == []


def test_missing_families_and_reasons_are_reported():
    assert rf.by_reason(metrics.parse("dv_other_total 1\n"), rf.EDGE_FAMILY) is None
    s = rf.snapshot(metrics.parse(""))
    assert s == {"edge": None, "web": None}
    problems = rf.shape_problems(s)
    assert len(problems) == 2 and rf.EDGE_FAMILY in problems[0] and rf.WEB_FAMILY in problems[1]
    partial = text().replace('%s{proc="web",reason="turn_issues"} 0\n' % rf.WEB_FAMILY, "")
    got = rf.shape_problems(rf.snapshot(metrics.parse(partial)))
    assert len(got) == 1 and "turn_issues" in got[0] and "zeros included" in got[0]
    extra = text() + '%s{proc="web",reason="peer-198-51-100-7"} 1\n' % rf.WEB_FAMILY
    got = rf.shape_problems(rf.snapshot(metrics.parse(extra)))
    assert len(got) == 1 and "peer-198-51-100-7" in got[0] and "not fixed" in got[0]


def test_by_reason_sums_over_the_other_labels_only():
    two = text(edge={"join_window": 2}) + '%s{proc="webrtc2",reason="join_window"} 3\n' % rf.EDGE_FAMILY
    assert rf.by_reason(metrics.parse(two), rf.EDGE_FAMILY)["join_window"] == 5
    # a sample without a reason label is ignored but the family counts as seen
    assert rf.by_reason(metrics.parse('%s{proc="x"} 4\n' % rf.EDGE_FAMILY), rf.EDGE_FAMILY) == {}


def test_the_families_are_never_added_and_delta_is_per_family():
    before = snap(edge={"join_window": 1}, web={"join_window": 1})
    after = snap(edge={"join_window": 4}, web={"join_window": 4, "login_circuits": 1})
    d = rf.delta(before, after)
    assert d["edge"]["join_window"] == 3 and d["web"]["join_window"] == 3 and d["web"]["login_circuits"] == 1
    assert rf.moved(d["edge"]) == {"join_window": 3} and rf.moved(d["web"]) == {"join_window": 3, "login_circuits": 1}
    assert rf.total(d["edge"]) == 3 and rf.total(d["web"]) == 4
    # an image without the family: no delta, not zero
    assert rf.delta({"edge": None, "web": None}, after) == {"edge": None, "web": None}
    # a restart (counter reset) is shown as it is
    assert rf.delta(after, before)["edge"]["join_window"] == -3


def test_record_and_line_for_the_load_summary():
    before, after = snap(), snap(edge={"join_window": 2}, web={"join_window": 2})
    rec = rf.record(before, after)
    assert rec["before"] == before and rec["after"] == after and rec["shape_problems"] == []
    assert rec["moved"] == {"edge": {"join_window": 2}, "web": {"join_window": 2}}
    json.dumps(rec)                                                  # goes into load.json
    out = rf.line(rec)
    assert "join_window +2" in out and rf.EDGE_FAMILY in out and rf.WEB_FAMILY in out
    quiet = rf.line(rf.record(before, before))
    assert quiet.count("none") == 2
    absent = rf.line(rf.record({"edge": None, "web": None}, {"edge": None, "web": None}))
    assert absent.count("n/a") == 2
    assert any("not exposed" in p for p in rf.record({"edge": None, "web": None}, {"edge": None, "web": None})["shape_problems"])


# ---------------------------------------------------------------- the refusal itself

def test_problem_code_from_code_or_type():
    assert rf.problem_code({"code": "unavailable"}) == "unavailable"
    assert rf.problem_code({"type": "https://example.invalid/problems/unavailable"}) == "unavailable"
    assert rf.problem_code({"type": "/problems/"}) == "problems"
    assert rf.problem_code({"detail": "x"}) is None and rf.problem_code("x") is None and rf.problem_code(None) is None


def test_refusal_contract_judges_status_code_and_retry_after_not_the_detail():
    assert rf.refusal_shape_problems("x", 503, "unavailable", "5") == []
    assert rf.refusal_shape_problems("x", 503, "unavailable", 5) == []
    bad = rf.refusal_shape_problems("x", 429, "rate_limited", "1")
    assert len(bad) == 3
    assert any("Retry-After" in p for p in rf.refusal_shape_problems("x", 503, "unavailable", None))


def test_detail_note_names_the_limit_or_the_old_text():
    assert rf.detail_note("WebRTC listener capacity reached") == "listeners"
    assert rf.detail_note("WebRTC capacity reached") == "old"
    assert rf.detail_note("something else") == "other" and rf.detail_note(None) == "other"


NEW = json.dumps({"type": "https://example.invalid/problems/unavailable", "code": "unavailable", "detail": "WebRTC join window capacity reached"})
OLD = json.dumps({"code": "unavailable", "detail": "WebRTC capacity reached"})


@pytest.mark.parametrize("body", [NEW, OLD])
def test_the_retry_rule_accepts_the_old_and_the_new_shape_by_status_and_code(body):
    assert rf.is_capacity_refusal(503, body, {"retry-after": "5"})
    assert rf.is_capacity_refusal(503, body, {"Retry-After": "5"})


def test_the_retry_rule_does_not_depend_on_the_detail_text():
    renamed = json.dumps({"code": "unavailable", "detail": "limit reached, try later"})
    assert rf.is_capacity_refusal(503, renamed, {"retry-after": "5"})
    # the detail of a capacity refusal on a reply with another code is not enough
    other = json.dumps({"code": "rate_limited", "detail": "WebRTC join window capacity reached"})
    assert not rf.is_capacity_refusal(503, other, {"retry-after": "5"})
    # a 503 `unavailable` without a Retry-After is not a capacity refusal (hub down, edge silent)
    assert not rf.is_capacity_refusal(503, NEW, {}) and not rf.is_capacity_refusal(503, NEW, None)
    assert not rf.is_capacity_refusal(503, NEW, {"retry-after": "0"}) and not rf.is_capacity_refusal(503, NEW, {"retry-after": "soon"})
    assert not rf.is_capacity_refusal(429, NEW, {"retry-after": "5"}) and not rf.is_capacity_refusal(200, "", {"retry-after": "5"})


def test_a_body_without_a_code_falls_back_to_the_old_text_rule():
    assert rf.is_capacity_refusal(503, '{"detail":"WebRTC capacity reached"}')
    assert rf.is_capacity_refusal(503, "WebRTC capacity reached")
    assert not rf.is_capacity_refusal(503, '{"detail":"the edge does not answer"}')
    assert not rf.is_capacity_refusal(503, "") and not rf.is_capacity_refusal(503, None)


def test_problem_detail_is_shortened_and_tolerant():
    assert rf.problem_detail(NEW) == "WebRTC join window capacity reached"
    assert rf.problem_detail(json.dumps({"detail": "x" * 500}), limit=10) == "x" * 10
    assert rf.problem_detail("not json") == "" and rf.problem_detail(None) == "" and rf.problem_detail("[1]") == ""


# ---------------------------------------------------------------- the switch and the config

def test_the_switch():
    assert rf.fix_on({"DVT_WEBSTALL1FIX": "on"}) and rf.fix_on({"DVT_WEBSTALL1FIX": " ON "}) and rf.fix_on({"DVT_WEBSTALL1FIX": "1"})
    assert not rf.fix_on({}) and not rf.fix_on({"DVT_WEBSTALL1FIX": ""}) and not rf.fix_on({"DVT_WEBSTALL1FIX": "off"})
    assert "DVT_WEBSTALL1FIX=on" in rf.skip_reason()


def test_join_window_setting_from_the_rendered_config_or_the_env():
    assert rf.join_window_setting("[edges.webrtc]\njoins_per_second = 1\n") == {"joins_per_second": 1, "source": "rendered"}
    assert rf.join_window_setting("[edges.webrtc]\nenable = true\n") == {"joins_per_second": None, "source": "rendered"}
    assert rf.join_window_setting("not toml [[[")["joins_per_second"] is None
    assert rf.join_window_setting("", "rig/config/dv-router/web.toml:rig/config/dv-router/join-window.toml") == {"joins_per_second": 1, "source": "env"}
    assert rf.join_window_setting("", "rig/config/dv-router/web.toml") == {"joins_per_second": None, "source": "env"}
    assert rf.join_window_setting(None, None) == {"joins_per_second": None, "source": "unknown"}
    # the rendered text wins over the variable
    assert rf.join_window_setting("[edges.webrtc]\njoins_per_second = 5\n", "x/join-window.toml")["joins_per_second"] == 5


def test_the_snippet_sets_the_documented_key():
    from pathlib import Path
    import tomllib
    path = Path(__file__).resolve().parents[2] / "rig" / "config" / "dv-router" / "join-window.toml"
    assert tomllib.loads(path.read_text(encoding="utf-8"))["edges"]["webrtc"] == {"joins_per_second": 1}


# ---------------------------------------------------------------- reading the hub

class Control:
    def __init__(self, *texts):
        self.texts = list(texts)

    def metrics(self, service, timeout=30.0):
        assert service == "xlxd-sut"
        item = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        if isinstance(item, Exception):
            raise item
        return item


def test_read_returns_a_snapshot_and_survives_a_failed_read():
    assert rf.read(Control(text(edge={"slots": 1})))["edge"]["slots"] == 1
    bad = rf.read(Control(RuntimeError("metrics error 1")))
    assert bad == {"edge": None, "web": None, "read_error": True}


def test_settled_waits_for_two_equal_reads():
    reads = iter([snap(), snap(edge={"join_window": 1}), snap(edge={"join_window": 2}), snap(edge={"join_window": 2})])
    sleeps = []
    got, ok = rf.settled(lambda: next(reads), sleep=sleeps.append, step_s=12.0, max_s=48.0)
    assert ok and got["edge"]["join_window"] == 2 and sleeps == [12.0, 12.0, 12.0]


def test_settled_gives_up_with_the_last_read():
    n = iter(range(100))
    got, ok = rf.settled(lambda: {"n": next(n)}, sleep=lambda s: None, step_s=10.0, max_s=30.0)
    assert not ok and got == {"n": 3}


def test_poll_returns_as_soon_as_the_predicate_holds_or_the_budget_ends():
    reads = iter([1, 2, 3, 4])
    sleeps = []
    got, ok = rf.poll(lambda: next(reads), lambda v: v >= 3, 60.0, step_s=3.0, sleep=sleeps.append)
    assert ok and got == 3 and sleeps == [3.0, 3.0]
    got, ok = rf.poll(lambda: 0, lambda v: v > 0, 6.0, step_s=3.0, sleep=lambda s: None)
    assert not ok and got == 0
