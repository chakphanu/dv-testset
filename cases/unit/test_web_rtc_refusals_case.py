# SPDX-License-Identifier: MIT
"""cases/test_web_rtc_refusals.py (WEBSTALL1, written offline): the skip paths and the verdict logic against a scripted hub and a scripted page."""
import pytest

import test_web_rtc_refusals as case
import websid_support as ws
from dvtest import metrics, refusals as rf

pytestmark = pytest.mark.unit


def expo(edge=None, web=None, drop=None):
    lines = ["%s{proc=\"webrtc\",reason=\"%s\"} %s" % (rf.EDGE_FAMILY, r, (edge or {}).get(r, 0)) for r in rf.EDGE_REASONS]
    lines += ["%s{proc=\"web\",reason=\"%s\"} %s" % (rf.WEB_FAMILY, r, (web or {}).get(r, 0)) for r in rf.WEB_REASONS if r != drop]
    lines.append("%s{proc=\"webrtc\",reason=\"core\"} 0" % rf.CORE_FAMILY)
    return "\n".join(lines) + "\n"


class Control:
    def __init__(self, *texts):
        self.texts = list(texts)

    def metrics(self, service, timeout=30.0):
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(case.time, "sleep", lambda s: None)
    monkeypatch.setattr(rf.time, "sleep", lambda s: None)


def test_every_case_skips_without_the_switch(monkeypatch):
    monkeypatch.delenv("DVT_WEBSTALL1FIX", raising=False)
    with pytest.raises(pytest.skip.Exception) as info:
        case.test_both_families_expose_every_reason(Control(expo()), "https://dvt-web")
    assert "DVT_WEBSTALL1FIX=on" in str(info.value)
    with pytest.raises(pytest.skip.Exception):
        case.test_ninth_circuit_counts_login_circuits(object(), Control(expo()))


def test_a_passes_on_complete_families_and_fails_on_a_missing_reason(monkeypatch):
    monkeypatch.setenv("DVT_WEBSTALL1FIX", "on")
    case.test_both_families_expose_every_reason(Control(expo()), "https://dvt-web")
    with pytest.raises(AssertionError) as info:
        case.test_both_families_expose_every_reason(Control(expo(drop="turn_issues")), "https://dvt-web")
    assert "turn_issues" in str(info.value)
    with pytest.raises(AssertionError) as info:
        case.test_both_families_expose_every_reason(Control("dv_other_total 1\n"), "https://dvt-web")
    assert "not exposed" in str(info.value)


def test_a_needs_the_unchanged_core_counter(monkeypatch):
    monkeypatch.setenv("DVT_WEBSTALL1FIX", "on")
    no_core = "\n".join(l for l in expo().splitlines() if rf.CORE_FAMILY not in l) + "\n"
    with pytest.raises(AssertionError) as info:
        case.test_both_families_expose_every_reason(Control(no_core), "https://dvt-web")
    assert rf.CORE_FAMILY in str(info.value)


class Page:
    """A scripted browser page: nine offers answered like the service answers them."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []
        self.page = self

    def goto(self, path, **kw):
        self.calls.append(("goto", path))

    def evaluate(self, js, arg=None):
        if js is ws.JS_SESSION:
            return {"status": 200, "csrf": "token"}
        assert js is ws.JS_OFFER_ONLY and arg["room"] == case.ROOM
        self.calls.append(("offer", arg["room"]))
        return self.answers.pop(0)


def good_answers(detail=rf.DETAILS["login_circuits"], status=503, code="unavailable", retry_after="5"):
    return [{"status": 200, "sess_id": "s%d" % i, "code": None, "retry_after": None, "detail": None} for i in range(8)] + [
        {"status": status, "sess_id": None, "code": code, "retry_after": retry_after, "detail": detail}]


def run_b(monkeypatch, answers, hub):
    monkeypatch.setenv("DVT_WEBSTALL1FIX", "on")
    seen = []
    monkeypatch.setattr(case.web, "finding", lambda text, out_dir=None: seen.append(text))
    case.test_ninth_circuit_counts_login_circuits(Page(answers), Control(*hub))
    return seen


def test_b_passes_when_login_circuits_rises_by_one_and_the_edge_does_not_move(monkeypatch):
    hub = [expo(), expo(), expo(web={"login_circuits": 1}), expo(web={"login_circuits": 1})]
    assert run_b(monkeypatch, good_answers(), hub) == []


def test_b_records_a_finding_for_a_detail_other_than_the_documented_one(monkeypatch):
    hub = [expo(), expo(), expo(web={"login_circuits": 1}), expo(web={"login_circuits": 1})]
    seen = run_b(monkeypatch, good_answers(detail="WebRTC capacity reached"), hub)
    assert len(seen) == 1 and "WebRTC login circuit capacity reached" in seen[0]


def test_b_judges_status_code_and_retry_after_of_the_ninth(monkeypatch):
    hub = [expo(), expo(), expo(web={"login_circuits": 1})]
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, good_answers(retry_after="1"), hub)
    assert "Retry-After" in str(info.value)
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, good_answers(status=200, code=None, retry_after=None), hub)
    assert "answered 200" in str(info.value)


def test_b_fails_when_an_early_circuit_is_refused(monkeypatch):
    answers = good_answers()
    answers[3] = {"status": 429, "code": "rate_limited", "retry_after": "1", "detail": None}
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, answers, [expo()])
    assert "not all accepted" in str(info.value)


def test_b_fails_when_the_counter_never_rises(monkeypatch):
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, good_answers(), [expo()])
    assert "did not rise" in str(info.value)


def test_b_fails_when_the_counter_rises_twice_or_the_edge_moves(monkeypatch):
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, good_answers(), [expo(), expo(), expo(web={"login_circuits": 2}), expo(web={"login_circuits": 2})])
    assert "rose by 2" in str(info.value)
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, good_answers(), [expo(), expo(), expo(web={"login_circuits": 1}, edge={"slots": 1}), expo(web={"login_circuits": 1}, edge={"slots": 1})])
    assert "edge never saw it" in str(info.value)
    with pytest.raises(AssertionError) as info:
        run_b(monkeypatch, good_answers(), [expo(), expo(), expo(web={"login_circuits": 1, "web_sessions": 1}), expo(web={"login_circuits": 1, "web_sessions": 1})])
    assert "web_sessions" in str(info.value)


def test_the_families_are_not_added(monkeypatch):
    # the same refusal counted once in each family is not 2 refusals: judged one by one
    d = rf.delta(rf.snapshot(metrics.parse(expo())), rf.snapshot(metrics.parse(expo(edge={"join_window": 3}, web={"join_window": 3}))))
    assert d["edge"]["join_window"] == 3 and d["web"]["join_window"] == 3
