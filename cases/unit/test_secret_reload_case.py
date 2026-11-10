# SPDX-License-Identifier: MIT
"""cases/test_secret_reload.py: the file and step-up helpers of the SECRELOAD1 case (no rig)."""
import os
import stat
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_secret_reload as case  # noqa: E402

pytestmark = pytest.mark.unit


def test_write_in_place_keeps_the_inode_the_size_and_the_mode(tmp_path):
    path = tmp_path / "passcode"
    path.write_text("12345\n")
    os.chmod(path, 0o600)
    before = path.stat()
    case.write_in_place(path, "54321\n")
    after = path.stat()
    assert path.read_text() == "54321\n"
    assert (after.st_ino, after.st_size, stat.S_IMODE(after.st_mode)) == (before.st_ino, before.st_size, 0o600)


def test_write_atomic_makes_a_new_inode_with_mode_0600_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "passcode"
    path.write_text("12345\n")
    os.chmod(path, 0o640)
    case.write_atomic(path, "54321\n")
    assert path.read_text() == "54321\n" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["passcode"]


class FakeClient:
    def __init__(self):
        self.steps = 0

    def step_up(self):
        self.steps += 1


def test_step_up_is_renewed_only_when_the_window_is_old(monkeypatch):
    ctx = types.SimpleNamespace(client=FakeClient(), stepped=None)
    clock = [1000.0]
    monkeypatch.setattr(case.time, "monotonic", lambda: clock[0])
    case.step_up(ctx)
    case.step_up(ctx)
    assert ctx.client.steps == 1
    clock[0] += case.STEP_UP_S - 1
    case.step_up(ctx)
    assert ctx.client.steps == 1
    clock[0] += 2
    case.step_up(ctx)
    assert ctx.client.steps == 2


def test_wait_for_returns_the_first_truthy_value_or_the_last(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(case.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(case.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    values = iter([None, 0, "ok", "late"])
    assert case.wait_for(lambda: next(values), 10.0, 1.0) == "ok"
    assert case.wait_for(lambda: None, 3.0, 1.0) is None and clock[0] >= 3.0


def test_say_refuses_a_line_that_holds_a_value(tmp_path):
    from dvtest import secret_reload as sr
    entries = sr.entries_for("4242\n", "31337\n")
    ctx = types.SimpleNamespace(leaks=sr.Leaks(entries), target=types.SimpleNamespace(out=str(tmp_path)))
    case.say(ctx, "x", "old value 4 characters, pass-free text")
    with pytest.raises(AssertionError, match="secret value"):
        case.say(ctx, "x", "the passcode is 31337")


def test_pid_text():
    assert case.pid_text(None) == "none" and case.pid_text((12, 345)) == "12"


def _fake(monkeypatch, pids, hubs):
    """pid_of_edge / hub answer from the given sequences (the last value repeats); time does not pass."""
    pid_seq, hub_seq = list(pids), list(hubs)
    monkeypatch.setattr(case, "pid_of_edge", lambda ctx: pid_seq.pop(0) if len(pid_seq) > 1 else pid_seq[0])
    monkeypatch.setattr(case, "hub", lambda ctx: hub_seq.pop(0) if len(hub_seq) > 1 else hub_seq[0])
    clock = [0.0]
    monkeypatch.setattr(case.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(case.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))


def before(pid=(10, 1), restarts=0.0, total=3.0, verified=True):
    return types.SimpleNamespace(pid=pid, restarts=restarts, all_restarts=total, verified=verified)


def test_assert_nothing_restarted_accepts_an_unchanged_edge(monkeypatch):
    _fake(monkeypatch, [(10, 1)], [(True, 0.0, 3.0)])
    case.assert_nothing_restarted(types.SimpleNamespace(), before(), "x")


@pytest.mark.parametrize("pid,hubs,needle", [
    ((11, 2), (True, 0.0, 3.0), "restarted"),
    ((10, 1), (True, 1.0, 4.0), "restarts_total moved"),
    ((10, 1), (False, 0.0, 3.0), "verified"),
])
def test_assert_nothing_restarted_names_what_moved(monkeypatch, pid, hubs, needle):
    _fake(monkeypatch, [pid], [hubs])
    with pytest.raises(AssertionError, match=needle):
        case.assert_nothing_restarted(types.SimpleNamespace(), before(), "x")


REPLY = {"actions": [{"class": "Restart", "child": "dv-edge-aprsis", "path": "edges.aprsis.passcode_file",
                      "impact": "edges.aprsis.passcode_file content changed; restart"}]}


def test_assert_restarted_needs_the_action_a_new_process_and_one_more_restart(monkeypatch):
    ctx = types.SimpleNamespace(leaks=None)
    _fake(monkeypatch, [(10, 1), (11, 2)], [(False, 1.0, 4.0)])
    monkeypatch.setattr(case, "state", lambda c: types.SimpleNamespace(pid=(11, 2), restarts=1.0, all_restarts=4.0, verified=False))
    after = case.assert_restarted(ctx, before(), REPLY, "3")
    assert after.restarts == 1.0
    with pytest.raises(AssertionError, match="lists no restart"):
        case.assert_restarted(ctx, before(), {"actions": []}, "3")
    with pytest.raises(AssertionError, match="content changed"):
        case.assert_restarted(ctx, before(), {"actions": [{"class": "Restart", "child": "dv-edge-aprsis"}]}, "3")


def test_assert_restarted_fails_when_the_counter_rose_twice(monkeypatch):
    ctx = types.SimpleNamespace(leaks=None)
    _fake(monkeypatch, [(11, 2)], [(False, 2.0, 5.0)])
    monkeypatch.setattr(case, "state", lambda c: types.SimpleNamespace(pid=(11, 2), restarts=2.0, all_restarts=5.0, verified=False))
    with pytest.raises(AssertionError, match="want 1"):
        case.assert_restarted(ctx, before(), REPLY, "3")


class Resp:
    def __init__(self, status, text):
        self.status, self._text = status, text

    def text(self):
        return self._text


def test_assert_refused_needs_the_key_and_the_path():
    ctx = types.SimpleNamespace(client=types.SimpleNamespace(scrub=lambda t: t))
    body = {"errors": [{"path": "edges.aprsis.passcode_file", "msg": "cannot read /etc/dv-router/aprs-secrets/passcode"}]}
    case.assert_refused(ctx, Resp(422, json_text(body)), body, "/etc/dv-router/aprs-secrets/passcode", "2a")
    with pytest.raises(AssertionError, match="names the key"):
        case.assert_refused(ctx, Resp(422, "{}"), {"detail": "bad"}, "/etc/dv-router/aprs-secrets/passcode", "2a")
    with pytest.raises(AssertionError, match="want a refusal"):
        case.assert_refused(ctx, Resp(200, "{}"), body, "/etc/dv-router/aprs-secrets/passcode", "2a")


def json_text(value):
    import json
    return json.dumps(value)
