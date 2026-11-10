# SPDX-License-Identifier: MIT
"""cases/stall_kills_support.py: the RFWD1 false-kill guard of the timing cases (parse, delta, verdict, record file, reads)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stall_kills_support as sk  # noqa: E402
from dvtest import metrics  # noqa: E402

pytestmark = pytest.mark.unit

ZERO = """\
# TYPE dv_launcher_child_stall_kills_total counter
dv_launcher_child_stall_kills_total{child="dv-edge-ax25"} 0
dv_launcher_child_stall_kills_total{child="dv-edge-aprsis"} 0
dv_launcher_child_up{child="dv-edge-ax25"} 1
"""
KILLED = ZERO.replace('{child="dv-edge-ax25"} 0', '{child="dv-edge-ax25"} 2')
OLD_IMAGE = "dv_launcher_child_up{child=\"dv-edge-ax25\"} 1\ndv_launcher_child_restarts_total{child=\"dv-edge-ax25\"} 0\n"


def counters(text):
    return sk.parse_stall_kills(metrics.parse(text))


# ---------------------------------------------------------------- parse / compute / verdict

def test_parse_reads_the_family_per_child_and_none_when_absent():
    assert counters(ZERO) == {"dv-edge-ax25": 0.0, "dv-edge-aprsis": 0.0}
    assert counters(KILLED)["dv-edge-ax25"] == 2.0
    assert counters(OLD_IMAGE) is None and counters("") is None


def test_parse_sums_series_of_one_child_and_labels_a_missing_child():
    text = 'dv_launcher_child_stall_kills_total{child="a"} 1\ndv_launcher_child_stall_kills_total{child="a",x="y"} 2\ndv_launcher_child_stall_kills_total 4\n'
    assert counters(text) == {"a": 3.0, "?": 4.0}


def test_compute_delta_per_child():
    rows = sk.compute({"a": 1.0, "b": 0.0}, {"a": 3.0, "b": 0.0, "c": 2.0})
    assert rows["a"] == {"start": 1.0, "end": 3.0, "delta": 2.0}
    assert rows["b"] == {"start": 0.0, "end": 0.0, "delta": 0.0}
    assert rows["c"] == {"start": None, "end": 2.0, "delta": 2.0}           # first seen at the end: a counter starts at 0
    assert sk.compute({"gone": 5.0}, {})["gone"] == {"start": 5.0, "end": None, "delta": 0.0}
    assert sk.compute(None, {"a": 0.0}) == {"a": {"start": None, "end": 0.0, "delta": 0.0}}


def test_verdict_ok_when_every_child_is_zero():
    kind, message = sk.verdict(counters(ZERO), counters(ZERO))
    assert kind == "ok" and "2 children" in message


def test_verdict_false_kill_names_every_child_with_its_values():
    kind, message = sk.verdict(counters(ZERO), counters(KILLED.replace('"dv-edge-aprsis"} 0', '"dv-edge-aprsis"} 1')))
    assert kind == "false_kill"
    assert "dv-edge-ax25 start 0 end 2 delta 2" in message and "dv-edge-aprsis start 0 end 1 delta 1" in message
    assert sk.verdict(None, counters(KILLED))[0] == "false_kill"                       # no start reading: the end value counts


def test_verdict_end_value_above_zero_fails_even_with_equal_start():
    # a non-zero start (an earlier module of the same run killed something): the end value alone is a finding
    kind, message = sk.verdict({"a": 1.0}, {"a": 1.0})
    assert kind == "false_kill" and "a start 1 end 1 delta 0" in message


def test_verdict_absent_and_unreadable():
    assert sk.verdict(None, None)[0] == "absent" and "no RFWD1" in sk.verdict(None, None)[1]
    assert sk.verdict(counters(ZERO), None, readable=True)[0] == "absent"
    assert sk.verdict(counters(ZERO), counters(ZERO), readable=False)[0] == "unreadable"


def test_record_and_merge_write(tmp_path):
    rec = sk.record("test_web_load", counters(ZERO), counters(KILLED), "false_kill", "msg")
    assert rec["series_present"] and rec["series_at_start"] and rec["children"]["dv-edge-ax25"]["delta"] == 2.0
    path = tmp_path / "web" / "stall-kills.json"
    doc = sk.merge_write(path, "test_web_load", rec)
    assert set(doc["modules"]) == {"test_web_load"}
    absent = sk.record("test_web_soak", None, None, "absent", "no series")
    doc = sk.merge_write(path, "test_web_soak", absent)
    on_disk = json.loads(path.read_text())
    assert set(on_disk["modules"]) == {"test_web_load", "test_web_soak"} and on_disk["family"] == sk.FAMILY
    assert on_disk["modules"]["test_web_soak"]["series_present"] is False and on_disk["modules"]["test_web_soak"]["children"] == {}


def test_merge_write_survives_a_broken_old_file_and_an_unwritable_dir(tmp_path):
    path = tmp_path / "stall-kills.json"
    path.write_text("not json")
    assert sk.merge_write(path, "m", {"verdict": "ok"})["modules"] == {"m": {"verdict": "ok"}}
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert sk.merge_write(blocker / "sub" / "x.json", "m", {}) is None


# ---------------------------------------------------------------- reads

class FakeControl:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def metrics(self, service="xlxd-sut", timeout=30.0):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_read_counter():
    assert sk.read_counter(FakeControl([ZERO])) == ({"dv-edge-ax25": 0.0, "dv-edge-aprsis": 0.0}, True)
    assert sk.read_counter(FakeControl([OLD_IMAGE])) == (None, True)
    assert sk.read_counter(FakeControl([RuntimeError("error 1")])) == (None, False)
    assert sk.read_counter(FakeControl([TimeoutError("no ack")])) == (None, False)


def test_read_start_retries_until_the_hub_answers_and_then_gives_up():
    clock = [0.0]
    ctl = FakeControl([RuntimeError("error 1"), RuntimeError("error 1"), ZERO])
    got = sk.read_start(ctl, sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
    assert got["readable"] and got["counters"] == {"dv-edge-ax25": 0.0, "dv-edge-aprsis": 0.0} and ctl.calls == 3
    clock[0] = 0.0
    never = FakeControl([RuntimeError("error 1")])
    got = sk.read_start(never, sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0], wait=10.0)
    assert got == {"counters": None, "readable": False} and never.calls >= 5


# ---------------------------------------------------------------- the last test of a module

def run_end(monkeypatch, tmp_path, start_text, end_text, start_readable=True, control=True):
    monkeypatch.setenv("DVT_OUT", str(tmp_path))
    start = {"counters": counters(start_text) if start_text else None, "readable": start_readable,
             "control": FakeControl([end_text]) if control else None}
    written, found, slept = [], [], []
    sk.check_end("test_web_load", start, sleep=slept.append, write=lambda p, m, r: written.append((p, m, r)),
                 finding=found.append)
    return written, found, slept


def test_check_end_passes_and_records_when_all_zero(monkeypatch, tmp_path, capsys):
    written, found, slept = run_end(monkeypatch, tmp_path, ZERO, ZERO)
    assert slept == [sk.FLUSH_S] and not found
    assert written[0][1] == "test_web_load" and written[0][2]["verdict"] == "ok" and written[0][0] == tmp_path / "web" / "stall-kills.json"
    assert "[RECORD] RFWD1 test_web_load" in capsys.readouterr().out


def test_check_end_fails_on_a_false_kill_names_children_and_emits_a_finding(monkeypatch, tmp_path):
    found = []
    monkeypatch.setenv("DVT_OUT", str(tmp_path))
    start = {"counters": counters(ZERO), "readable": True, "control": FakeControl([KILLED])}
    with pytest.raises(pytest.fail.Exception, match="dv-edge-ax25 start 0 end 2 delta 2"):
        sk.check_end("test_web_soak", start, sleep=lambda s: None, finding=found.append)
    assert len(found) == 1 and "RFWD1 test_web_soak" in found[0] and "dv-edge-ax25" in found[0]
    doc = json.loads((tmp_path / "web" / "stall-kills.json").read_text())
    assert doc["modules"]["test_web_soak"]["verdict"] == "false_kill"


def test_check_end_skips_on_an_image_without_the_series_and_still_records_it(monkeypatch, tmp_path):
    monkeypatch.setenv("DVT_OUT", str(tmp_path))
    start = {"counters": None, "readable": True, "control": FakeControl([OLD_IMAGE])}
    with pytest.raises(pytest.skip.Exception, match="no RFWD1"):
        sk.check_end("test_web_load", start, sleep=lambda s: None, finding=lambda t: None)
    rec = json.loads((tmp_path / "web" / "stall-kills.json").read_text())["modules"]["test_web_load"]
    assert rec["verdict"] == "absent" and rec["series_present"] is False


def test_check_end_fails_when_the_end_read_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("DVT_OUT", str(tmp_path))
    start = {"counters": counters(ZERO), "readable": True, "control": FakeControl([RuntimeError("error 1")])}
    with pytest.raises(pytest.fail.Exception, match="could not be read"):
        sk.check_end("test_web_load", start, sleep=lambda s: None, finding=lambda t: None)


def test_check_end_with_a_failed_start_read_but_zero_at_the_end_passes(monkeypatch, tmp_path, capsys):
    written, found, _ = run_end(monkeypatch, tmp_path, None, ZERO, start_readable=False)
    assert written[0][2]["verdict"] == "ok" and not found
    assert "the start read failed" in capsys.readouterr().out


def test_check_end_skips_without_a_control_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("DVT_OUT", str(tmp_path))
    with pytest.raises(pytest.skip.Exception, match="control channel"):
        sk.check_end("test_web_load", {"counters": None, "readable": False, "control": None}, sleep=lambda s: None)
    assert not (tmp_path / "web").exists()


def test_the_case_files_run_the_guard_last_and_import_the_fixture():
    root = Path(__file__).resolve().parent.parent
    for name in ("test_web_load", "test_web_soak"):
        text = (root / (name + ".py")).read_text()
        assert "from stall_kills_support import check_end, stall_kills_start" in text
        body = text[text.index("def test_zz_rfwd1_no_stall_kills"):]
        assert 'check_end("%s", stall_kills_start)' % name in body
        tests = [ln for ln in text.splitlines() if ln.startswith("def test_")]
        assert tests[-1].startswith("def test_zz_rfwd1_no_stall_kills")
