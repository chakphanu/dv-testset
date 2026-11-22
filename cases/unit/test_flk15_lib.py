# SPDX-License-Identifier: MIT
"""dvtest/flk15.py: the sampler, the kill loop, the log judge (FLK15 / GUARDSYNC1 line shapes). Pure, no rig."""
import io

import pytest

from dvtest import flk15

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_strict_switch():
    assert flk15.strict_on({"DVT_FLK15": "on"})
    assert not flk15.strict_on({})
    assert not flk15.strict_on({"DVT_FLK15": "off"})


def test_resync_wait_default_and_override():
    assert flk15.resync_wait_s({}) == flk15.BOUND_S + flk15.SLACK_S
    assert flk15.resync_wait_s({"DVT_FLK15_BOUND_S": "20"}) == 20.0
    assert flk15.resync_wait_s({"DVT_FLK15_BOUND_S": "x"}) == flk15.BOUND_S + flk15.SLACK_S
    assert flk15.resync_wait_s({"DVT_FLK15_BOUND_S": "-1"}) == flk15.BOUND_S + flk15.SLACK_S


def test_sampler_sees_a_leak_and_an_outage():
    clock = Clock()
    answers = [[{"callsign": "N0AAA"}], RuntimeError("down"), RuntimeError("down"), [{"callsign": "N0HID"}], []]

    def read():
        item = answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    s = flk15.Sampler(read, lambda r: r["callsign"] == "N0HID", clock=clock, sleep=clock.sleep)
    for _ in range(5):
        s.step()
        clock.sleep(1.0)
    assert [x.status for x in s.samples] == ["ok", "down", "down", "ok", "ok"]
    assert len(flk15.leaks(s.samples)) == 1
    first, last, count = flk15.outage_window(s.samples)
    assert (first, last, count) == (1.0, 2.0, 2)
    assert "1 showed a hidden row" in flk15.describe_samples(s.samples)


def test_outage_window_none_when_always_up():
    assert flk15.outage_window([flk15.Sample(0.0, "ok", [])]) is None


def test_kill_loop_counts_landed_kills_and_tolerates_error_3():
    clock = Clock()
    calls = []

    def kill():
        calls.append(clock.now)
        if len(calls) % 2 == 0:
            raise RuntimeError("kill-proc: error 3")

    landed, t0, t1 = flk15.kill_loop(kill, 3.0, clock=clock, sleep=clock.sleep, period_s=0.5)
    assert len(calls) == 6 and landed == 3
    assert t1 - t0 >= 3.0


def test_identities():
    class P:
        def __init__(self, pid, starttime):
            self.pid, self.starttime = pid, starttime
    assert flk15.identities([P(1, 10), P(2, 20)]) == {(1, 10), (2, 20)}
    assert flk15.identities([P(1, 10)]) != flk15.identities([P(1, 11)])


GOOD = """\
dv-svc-web: position policy published (start): revision 1, 2 hidden accounts
dv-svc-web: position policy published (durable change): revision 2, 3 hidden accounts
dv-svc-web: position policy published (own link up): revision 2, 3 hidden accounts
dv-room: position policy withheld after link loss 1: spatial data denied until reconciled
dv-room: fabric: router link down (loss 1)
dv-room: fabric: router link up again after loss 1
dv-room: position policy reconciled after link loss: revision 2, 3 hidden
dv-data-worker: guard records reconciled after link loss 1: 4 records from 2 controllers
"""


def test_judge_log_accepts_the_documented_lines():
    got = flk15.judge_log(GOOD)
    assert got.problems == []
    assert len(got.published) == 3 and len(got.withheld) == 1 and len(got.reconciled) == 1 and len(got.guard) == 1


def test_judge_log_refuses_a_callsign_on_a_policy_line():
    bad = "dv-svc-web: position policy published (start): revision 1, 2 hidden accounts N0TEST\n"
    assert flk15.judge_log(bad).problems


def test_judge_log_refuses_an_unknown_reason():
    bad = "dv-svc-web: position policy published (because): revision 1, 2 hidden accounts\n"
    got = flk15.judge_log(bad)
    assert any("outside" in p for p in got.problems)


def test_judge_log_needs_a_reconciled_line_after_withheld():
    bad = "dv-room: position policy withheld after link loss 3: spatial data denied until reconciled\n"
    assert flk15.judge_log(bad).problems
    other = bad + "dv-data-worker: position policy reconciled after link loss: revision 1, 0 hidden\n"
    assert flk15.judge_log(other).problems          # another program's line does not settle it


def test_link_down_without_up_is_a_finding_not_a_failure():
    got = flk15.judge_log("dv-room: fabric: router link down (loss 1)\n")
    assert got.problems == []
    assert any("no 'up again'" in f for f in got.findings)


def test_main_usage_and_missing_logs(tmp_path):
    out = io.StringIO()
    assert flk15.main([], out) == 2
    out = io.StringIO()
    assert flk15.main([str(tmp_path)], out) == 2
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "xlxd-sut.log").write_text(GOOD)
    out = io.StringIO()
    assert flk15.main([str(tmp_path)], out) == 0
    assert "flk15: ok" in out.getvalue()
    (logs / "xlxd-sut.log").write_text("dv-svc-web: position policy published (x y\n")
    out = io.StringIO()
    assert flk15.main([str(tmp_path)], out) == 1
