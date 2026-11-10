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


# ---------------------------------------------------------------- the SIGHUP variant against a fake SUT

class FakeSut:
    """A SUT that follows config.md: SIGHUP re-reads the passcode file; a content change restarts the APRS-IS edge (restart counter + 1,
    new pid) and its verdict is `verified` only for the fixture value; an unreadable file is refused and everything stays."""

    def __init__(self, path, fixture_value, follows=True, restart_on_unreadable=False):
        self.path, self.fixture, self.follows, self.restart_on_unreadable = path, fixture_value, follows, restart_on_unreadable
        self.running = path.read_text()
        self.pid, self.edge_restarts, self.other_restarts = 100, 0.0, 2.0
        self.hups = []

    @property
    def verified(self):
        return self.running == self.fixture

    def hup(self):
        self.hups.append(len(self.hups))
        if not self.follows:
            return 1700000000.0 + len(self.hups)
        try:
            content = self.path.read_text()
        except OSError:
            if self.restart_on_unreadable:
                self.pid, self.edge_restarts = self.pid + 1, self.edge_restarts + 1
            return 1700000000.0 + len(self.hups)
        if content != self.running:
            self.running = content
            self.pid, self.edge_restarts = self.pid + 1, self.edge_restarts + 1
        return 1700000000.0 + len(self.hups)


def run_variant(monkeypatch, tmp_path, sut_kwargs=None, strict=False):
    from dvtest import secret_reload as sr
    if os.geteuid() == 0:
        pytest.skip("mode 000 does not stop root")
    out = tmp_path / "out"
    (out / "aprs-secrets").mkdir(parents=True)
    (out / "secreload").mkdir()
    path = out / "aprs-secrets" / "passcode"
    path.write_text("4242\n")
    os.chmod(path, 0o600)
    sut = FakeSut(path, "4242\n", **(sut_kwargs or {}))
    entries = sr.entries_for("4242\n", "31337\n")
    ctx = types.SimpleNamespace(out=out, path=path, old="4242\n", new="31337\n", entries=entries, leaks=sr.Leaks(entries),
                                control=sut, target=types.SimpleNamespace(out=str(out)), marker=0)
    clock = [0.0]
    monkeypatch.setattr(case.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(case.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(case, "hub", lambda c: (sut.verified, sut.edge_restarts, sut.edge_restarts + sut.other_restarts))
    monkeypatch.setattr(case, "pid_of_edge", lambda c: (sut.pid, 1))
    monkeypatch.setenv("DVT_SECRELOAD1", "on" if strict else "")
    case.test_secreload1_sighup_reload_of_unchanged_toml(ctx.target, sut, ctx)
    return ctx, sut


def test_variant_follows_the_documented_rules(monkeypatch, tmp_path):
    ctx, sut = run_variant(monkeypatch, tmp_path)
    assert sut.edge_restarts == 2.0 and len(sut.hups) == 5      # in place -> restart, nothing, atomic -> restart, unreadable, back
    assert ctx.path.read_text() == "4242\n"
    expect = __import__("json").loads((ctx.out / "secreload" / "log-expect.json").read_text())
    assert expect["key"] == "passcode_file" and expect["path"] == "/etc/dv-router/aprs-secrets/passcode" and expect["t_hup"] == 1700000004.0
    assert oct(os.stat(ctx.out / "secreload" / "log-expect.json").st_mode & 0o777) == "0o600"
    notes = (ctx.out / case.LOG).read_text()
    assert "31337" not in notes and "4242" not in notes


def test_variant_skips_when_sighup_changes_nothing_and_fails_in_strict_mode(monkeypatch, tmp_path):
    with pytest.raises(pytest.skip.Exception, match="image without SECRELOAD1"):
        run_variant(monkeypatch, tmp_path / "a", {"follows": False})
    with pytest.raises(pytest.fail.Exception, match="DVT_SECRELOAD1=on"):
        run_variant(monkeypatch, tmp_path / "b", {"follows": False}, strict=True)


def test_variant_fails_when_an_unreadable_file_restarts_the_edge(monkeypatch, tmp_path):
    with pytest.raises(AssertionError, match="H4 mode 000: dv-edge-aprsis restarted"):
        run_variant(monkeypatch, tmp_path, {"restart_on_unreadable": True})
    # the case put the mode back before it failed
    assert (tmp_path / "out" / "aprs-secrets" / "passcode").stat().st_mode & 0o777 == 0o600
