# SPDX-License-Identifier: MIT
"""dvtest/rfsafe1c.py: the RFSAFE1c statements a black-box case can assert (procs[] against the launcher child, no session/update hint item, the probe failed log
line, the latency of a mid-run fence in the view)."""
import io
import json

import pytest

from dvtest import rfsafe1c as rc
from dvtest.txguard import Observation

pytestmark = pytest.mark.unit

NEW, OLD = 4242, 4100
START_NEW, START_OLD = 1_794_000_020_000, 1_794_000_000_000


def body(child_pid=NEW, child_since=START_NEW, row_pid=NEW, row_since=START_NEW, row=True, extra_children=(), up=True):
    children = [{"name": "dv-edge-ax25", "pid": child_pid, "restarts": 3, "since_ms": child_since, "state": "up"}] + list(extra_children)
    procs = []
    if row:
        entry = {"kind": "edge", "proc": "ax25", "up": up, "source": "token", "state": "up", "restarts": 3, "pid": row_pid}
        if row_since is not None:
            entry["since_ms"] = row_since
        procs.append(entry)
    return {"launcher": {"children": children}, "procs": procs}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_wait(bodies, **kw):
    clk = Clock()
    it = iter(bodies)
    last = [None]

    def read():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return rc.wait_origin(read, clock=clk, sleep=clk.sleep, **kw)


# ---------------------------------------------------------------- (4) origin

def test_child_proc_follows_the_hub_mapping():
    assert rc.child_proc("dv-edge-ax25") == ("edge", "ax25")
    assert rc.child_proc("dv-room[AB]") == ("controller", "AB")
    assert rc.child_proc("dv-codec-ambed[ambed]") == ("codec-worker", "ambed")
    assert rc.child_proc("dv-data-worker[pkt]") == ("data-worker", "pkt")
    assert rc.child_proc("dv-svc-web") == ("web", "dv-svc-web")
    assert rc.child_proc("something") == ("launcher", "something")


def test_pairs_keep_token_kinds_only_and_split_a_room_set():
    extra = [{"name": "dv-svc-web", "pid": 9, "since_ms": 1}, {"name": "dv-room[AB]", "pid": 7, "since_ms": 5, "restarts": 0}, {"name": "dv-edge-xlx", "pid": 8, "since_ms": 6}]
    pairs = rc.origin_pairs(body(extra_children=extra))
    assert [(p.kind, p.proc) for p in pairs] == [("edge", "ax25"), ("controller", "A"), ("controller", "B"), ("edge", "xlx")]
    assert pairs[0].row["pid"] == NEW and pairs[1].row is None
    assert [p.child for p in rc.origin_pairs(body(extra_children=extra), only=["dv-edge-ax25"])] == ["dv-edge-ax25"]
    assert rc.origin_pairs(None) == [] and rc.origin_pairs({"launcher": 5, "procs": "x"}) == []


def test_classify_states():
    def one(**kw):
        return rc.classify(rc.origin_pairs(body(**kw))[0])
    assert one()[0] == "match"
    assert one(row=False)[0] == "no_row"
    assert one(row_pid=OLD, row_since=START_OLD)[0] == "stale"                       # the new token with the previous child's pid and since_ms
    assert one(row_pid=None, row_since=None)[0] == "stale"
    assert one(child_pid=None)[0] == "no_pid"
    assert one(row_since=START_OLD)[0] == "mismatch"                                 # same pid, other since_ms
    assert one(row_since=None)[0] == "mismatch"                                      # the launcher start is known, the row has none
    assert one(child_since=0, row_since=None)[0] == "match"                          # an unknown launcher start is omitted from the row
    assert one(child_since=0, row_since=START_OLD)[0] == "mismatch"
    assert rc.classify(rc.Pair("edge", "ax25", "dv-edge-ax25", True, 1, 0, {"pid": True}))[0] in ("stale", "mismatch")   # a bool is not a pid


def test_wait_origin_matches_at_once_in_steady_state():
    res = run_wait([body()])
    assert res.problems == [] and res.stale == [] and res.polls == 1 and res.per_key[("edge", "ax25")][0] == "match"


def test_wait_origin_tolerates_and_records_a_stale_row_then_matches():
    stale = body(row_pid=OLD, row_since=START_OLD)
    res = run_wait([stale, stale, stale, body()], previous={("edge", "ax25"): OLD})
    assert res.problems == [] and res.polls == 4
    assert len(res.stale) == 1 and res.stale[0][1] == ("edge", "ax25") and res.stale[0][3] is True      # recorded once, flagged as the previous child
    assert "previous child" in rc.describe_origin(res)


def test_wait_origin_fails_when_the_row_never_catches_up():
    stale = body(row_pid=OLD, row_since=START_OLD)
    res = run_wait([stale], max_wait_s=6.0)
    assert len(res.problems) == 1 and "did not reach the launcher child's pid within 6 s" in res.problems[0]
    missing = run_wait([body(row=False)], max_wait_s=3.0)
    assert any("did not reach" in p for p in missing.problems)


def test_wait_origin_confirms_a_mismatch_over_two_reads():
    bad = body(row_since=START_OLD)
    res = run_wait([bad, bad])
    assert res.polls == 2 and len(res.problems) == 1 and "row since_ms %r, launcher child since_ms %r" % (START_OLD, START_NEW) in res.problems[0]
    # a difference seen once (a launcher put between the two halves of one reply) is not enough: the next read matches
    assert run_wait([bad, body()]).problems == []
    # two different differences do not confirm each other
    res = run_wait([bad, body(row_since=START_OLD + 1), body()])
    assert res.problems == []


def test_wait_origin_with_no_token_child_or_no_status():
    assert "no token-bearing child" in run_wait([{"launcher": {"children": []}, "procs": []}], max_wait_s=1.0).problems[0]
    assert "no token-bearing child" in run_wait([None], max_wait_s=1.0).problems[0]
    assert "among ['dv-edge-nope']" in run_wait([body()], only=["dv-edge-nope"], max_wait_s=1.0).problems[0]


def test_the_old_child_is_not_the_child_after_the_restart():
    old = body(child_pid=OLD, child_since=START_OLD, row_pid=OLD, row_since=START_OLD)
    prev = {("edge", "ax25"): OLD}
    assert run_wait([old], max_wait_s=2.0).problems == []                                  # without `previous` the old child is just a child
    res = run_wait([old, old, body()], previous=prev)
    assert res.problems == [] and res.polls == 3                                          # the match with the old child at once is not accepted
    res = run_wait([old], previous=prev, max_wait_s=3.0)
    assert len(res.problems) == 1 and "pre-restart pid" in res.problems[0]


def test_a_child_without_a_pid_is_recorded_not_failed():
    res = run_wait([body(child_pid=None)], max_wait_s=2.0)
    assert res.problems == [] and res.per_key[("edge", "ax25")][0] == "no_pid"


# ---------------------------------------------------------------- (2) hints

def test_event_hints_find_session_update_and_ignore_the_rest():
    items = [{"id": 9, "type": "session", "ev": "update", "ts_ms": 5}, {"id": 8, "type": "session", "ev": "snapshot", "ts_ms": 4},
             {"id": 7, "type": "status", "ev": "update", "ts_ms": 3}, {"id": 6, "type": "session", "ev": "disconnect", "ts_ms": 2},
             {"id": 3, "type": "session", "ev": "update", "ts_ms": 1}, "junk"]
    h = rc.event_hints(items, after_id=4)
    assert [x["id"] for x in h.hints] == [9] and [x["id"] for x in h.others] == [7] and h.seen == 4
    assert h.evs == {"session": {"update": 1, "snapshot": 1, "disconnect": 1}, "status": {"update": 1}}
    assert [x["id"] for x in rc.event_hints(items).hints] == [9, 3]
    assert rc.event_hints(None) == ([], [], 0, {})


def frame(name, obj, ident=None):
    return {"event": name, "id": ident, "data": json.dumps(obj) if not isinstance(obj, str) else obj}


def test_frame_hints_on_public_and_admin_streams():
    frames = [frame("hello", {"boot": "a", "last_id": 3}), frame("session", {"ev": "snapshot", "type": "session"}, "a-4"),
              frame("session", {"ev": "update", "edge": "ax25-0000000000000007"}, "a-5"), frame("room", {"ev": "onair"}, "a-6"),
              frame("update", "not json", "a-7"), frame("status", {"type": "status", "ev": "update"}, "a-8"), frame("hb", {}), frame("data", "{", "a-9")]
    h = rc.frame_hints(frames)
    assert [x["id"] for x in h.hints] == ["a-5", "a-7"] and [x["id"] for x in h.others] == ["a-8"]
    assert h.seen == 6 and h.evs["session"] == {"snapshot": 1, "update": 1}
    assert rc.frame_hints(None).hints == []


# ---------------------------------------------------------------- (3) log

GOOD = "RF TX fenced on radio1: probe failed part=%s; recovery is automatic"


def test_parse_and_judge_a_well_formed_line_with_a_prefix():
    for part in rc.PROBE_PARTS:
        text = "2026-11-11T10:00:00Z " + GOOD % part + "\nother line\n"
        j = rc.judge_log(text, {"radio1"})
        assert j.problems == [] and [(i.physical, i.part) for i in j.lines] == [("radio1", part)]
        assert any("1 probe failed line" in f for f in j.findings)
    assert rc.judge_log("nothing here\n", {"radio1"}) == ([], [], [])
    assert rc.judge_log("", None) == ([], [], [])


def test_judge_log_rejects_other_parts_physicals_and_extra_text():
    assert any("outside the fixed set" in p for p in rc.judge_log(GOOD % "socket", {"radio1"}).problems)
    assert any("not a configured physical" in p for p in rc.judge_log(GOOD % "task", {"rf-a"}).problems)
    assert rc.judge_log(GOOD % "task", None).problems == []
    extra = "RF TX fenced on radio1: probe failed part=holder_query; recovery is automatic (key rf/radio1/ptt/x: timeout)"
    assert any("continues with" in p for p in rc.judge_log(extra, {"radio1"}).problems)
    old = "dv-edge-ax25: RF TX fenced on radio1: probe failed; recovery is automatic"                # the pre-RFSAFE1c wording
    assert any("not the shape" in p for p in rc.judge_log(old, {"radio1"}).problems)
    assert any("not the shape" in p for p in rc.judge_log("probe failed somewhere", None).problems)


def test_judge_log_repeat_without_recovery_is_a_finding_not_a_failure():
    text = "\n".join([GOOD % "own_token", "x", GOOD % "link_check"])
    j = rc.judge_log(text, {"radio1"})
    assert j.problems == [] and any("no recovery line between" in f for f in j.findings)
    ok = "\n".join([GOOD % "own_token", "dv-edge-ax25: RF TX observation on radio1 recovered; transmit after the guard time", GOOD % "link_check"])
    assert not any("no recovery line" in f for f in rc.judge_log(ok, {"radio1"}).findings)
    other = "\n".join([GOOD % "task", "RF TX fenced on radio2: probe failed part=task; recovery is automatic"])      # two physicals: one line each
    assert not any("no recovery line" in f for f in rc.judge_log(other, {"radio1", "radio2"}).findings)


def test_configured_physicals():
    doc = {"edges": {"ax25": {"port": [{"name": "rf-a", "physical": "radio1"}, {"name": "rf-b", "physical": "radio1"}, {"name": "x"}]},
                     "radio": {"port": [{"physical": "radio2"}]}}}
    assert rc.configured_physicals(doc) == {"radio1", "radio2"}
    assert rc.configured_physicals({}) is None and rc.configured_physicals(None) is None


def test_host_scan_of_a_run_directory(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "xlxd-config" / "xlxd-sut").mkdir(parents=True)
    (tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml").write_text('[edges.ax25]\n[[edges.ax25.port]]\nname = "rf-a"\nphysical = "radio1"\n')
    (tmp_path / "logs" / "dvt-xlxd-sut.log").write_text("boot\n" + GOOD % "own_token" + "\n")
    out = io.StringIO()
    assert rc.main([str(tmp_path)], out) == 0
    assert "configured physicals ['radio1']" in out.getvalue() and "FINDING" in out.getvalue() and "rfsafe1c: ok" in out.getvalue()
    (tmp_path / "logs" / "dvt-xlxd-sut.log").write_text("RF TX fenced on radio9: probe failed part=task; recovery is automatic\n")
    out = io.StringIO()
    assert rc.main([str(tmp_path)], out) == 1 and "FAIL" in out.getvalue()
    assert rc.main([str(tmp_path / "none")], io.StringIO()) == 2
    assert rc.main([], io.StringIO()) == 2


# ---------------------------------------------------------------- (1) view latency

def obs(fenced, fence="foreign_holder", scope=None):
    return Observation(True, True, fenced, fence if fenced else None, 5000 if fenced else None, [], "kind=modem items[].modem", scope)


def run_view(reads, cause_at=0.0, **kw):
    clk = Clock()
    it = iter(reads)
    last = [None]

    def read():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    def cause():
        clk.t += cause_at
        return clk.t
    return rc.view_latency(read, cause, clock=clk, sleep=clk.sleep, **kw)


def test_view_latency_counts_from_the_cause_and_is_judged():
    v = run_view([obs(False), obs(False), obs(False), obs(True)], poll_s=0.5)
    assert v.state == "fenced" and v.fence == "foreign_holder" and abs(v.latency_s - 1.0) < 1e-9 and v.reads == 4
    assert rc.judge_latency(v) == []
    slow = run_view([obs(False)] * 6 + [obs(True)], poll_s=0.5)
    assert slow.latency_s == 2.5 and "bound 2.0 s" in rc.judge_latency(slow)[0]
    assert rc.judge_latency(slow, max_s=3.0) == []


def test_view_latency_other_outcomes():
    assert run_view([obs(True)]).state == "already_fenced" and "precondition" in rc.judge_latency(run_view([obs(True)]))[0]
    never = run_view([obs(False)], max_wait_s=2.0)
    assert never.state == "never" and "never showed" in rc.judge_latency(never)[0]
    scope = run_view([Observation(False, False, False, None, None, [], "x", "no_modem_object")])
    assert scope.state == "scope" and "modem" in rc.judge_latency(scope)[0]


def test_strict_switch():
    assert rc.strict_on({"DVT_RFSAFE1C": "on"}) and rc.strict_on({"DVT_RFSAFE1C": "1"}) and not rc.strict_on({}) and not rc.strict_on({"DVT_RFSAFE1C": "off"})
