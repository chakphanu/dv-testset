# SPDX-License-Identifier: MIT
"""cases/test_web_session_rotation.py, case 4 (admin service, no browser) against a fake admin service that coalesces role denials per
SESSION KEY (WEBSID1: stable across a step-up) or, as the images before it did, per cookie; and the helpers of the browser cases that need no
browser. The browser cases themselves are written offline and not run (docs/websid1-cases.md)."""
import sys
import types
from pathlib import Path

import pytest

try:
    import playwright.sync_api  # noqa: F401
except ImportError:                              # the playwright package is only in the tester image: a stub lets the module import
    stub = types.ModuleType("playwright.sync_api")
    for name in ("sync_playwright", "expect", "Page", "Browser", "BrowserContext", "Error", "TimeoutError"):
        setattr(stub, name, object)
    sys.modules["playwright"] = types.ModuleType("playwright")
    sys.modules["playwright.sync_api"] = stub

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_web_session_rotation as case  # noqa: E402

pytestmark = pytest.mark.unit


class Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body

    def json(self):
        return self._body


class Authenticator:
    def next_code(self):
        return "123456"


class Service:
    """Role denials are audited once per 10 s per (key, action); `model` "session" keys on the stable session, "cookie" on the cookie value."""

    def __init__(self, model):
        self.model, self.clock, self.items, self.last, self.suppressed = model, [0.0], [], {}, {}
        self.rotations = 0

    def deny(self, client, action):
        key = (client.key if self.model == "session" else client.cookie, action)
        now = self.clock[0]
        if key in self.last and now - self.last[key] < 10.0:
            self.suppressed[key] = self.suppressed.get(key, 0) + 1
            return
        self.last[key] = now
        item = {"action": action, "user": client.user, "result": "denied", "ts_ms": int(now * 1000) + len(self.items)}
        if self.suppressed.get(key):
            item["suppressed"] = self.suppressed.pop(key)
        self.items.append(item)


class Client:
    def __init__(self, service, role, user):
        self.service, self.role, self.user = service, role, user
        self.key, self.cookie, self.csrf = id(self), "cookie-0", "csrf-0"
        self.identity = {"user": user, "role": role}
        self.authenticator = Authenticator()

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def hide(self, *values):
        pass

    def session(self):
        return self.identity

    def step_up(self):
        pass

    def get(self, path, expect=None, **kw):
        if path.startswith("/api/v1/admin/audit"):
            return Resp(200, {"total": len(self.service.items), "items": list(reversed(self.service.items))})
        assert path == "/api/v1/admin/config/reload"
        self.service.deny(self, "config.reload.preview")
        return Resp(403, {"code": "forbidden"})

    def post(self, path, body=None, expect=None, **kw):
        assert path == "/api/v1/admin/session/step-up"
        self.service.rotations += 1
        self.cookie, self.csrf = "cookie-%d" % self.service.rotations, "csrf-%d" % self.service.rotations
        return Resp(200, {"ok": True})


def run(monkeypatch, model):
    service = Service(model)
    operator, admin = Client(service, "operator", "otto"), Client(service, "admin", "ada")
    monkeypatch.setattr(case.time, "monotonic", lambda: service.clock[0])
    monkeypatch.setattr(case.time, "sleep", lambda s: service.clock.__setitem__(0, service.clock[0] + s))
    sessions = {"operator": operator, "admin": admin}
    case.test_denials_coalesce_across_a_step_up(lambda role: sessions[role], True)
    return service


def test_denials_coalesce_across_the_step_up_with_a_stable_session_key(monkeypatch):
    service = run(monkeypatch, "session")
    assert [i["result"] for i in service.items] == ["denied", "denied"] and service.items[1]["suppressed"] == 5
    assert service.rotations == 1


def test_an_image_that_keys_denials_on_the_cookie_fails_the_case(monkeypatch):
    with pytest.raises(AssertionError, match="1 new denial|2 new denial|want 1"):
        run(monkeypatch, "cookie")


def test_the_probe_fixture_is_the_one_the_cases_use():
    for name in ("test_step_up_keeps_sse_circuit_and_wav", "test_step_up_does_not_multiply_the_caps", "test_session_end_ends_the_handles"):
        assert "make_login" in getattr(case, name).__code__.co_varnames[:getattr(case, name).__code__.co_argcount]
    assert case.CAUSES == ["logout", "revoke", "role", "disable"]
    assert case.rate_for(2_400_000, 40.0) == pytest.approx(60_000)
    assert case.rate_for(0) >= 1.0


@pytest.mark.parametrize("value,want", [("", False), ("on", True), ("1", True), ("off", False)])
def test_strict_switches(monkeypatch, value, want):
    monkeypatch.setenv("DVT_WEBSID1", value)
    monkeypatch.setenv("DVT_WEBSID1B", value)
    assert case.strict() is want and case.strict_1b() is want


def test_the_websid1b_positive_control_exists():
    assert "make_login" in case.test_step_up_in_the_same_tab_keeps_the_circuit.__code__.co_varnames
    assert case.card_text(types.SimpleNamespace(status=lambda: "Listening on module V")) == "Listening on module V"
    assert case.card_text(types.SimpleNamespace(status=lambda: 1 / 0)) == "(no card)"


# ---------------------------------------------------------------- the WEBSID1 probe fixture

def run_probe(monkeypatch, outcomes):
    """The websid1 fixture function with probe_once answering from `outcomes` ([(kind, text)])."""
    fixture = getattr(case.websid1, "__wrapped__", None) or getattr(case.websid1, "_fixture_function", None)
    if fixture is None:
        pytest.skip("cannot reach the fixture function of this pytest")
    queue = list(outcomes)
    monkeypatch.setattr(case, "probe_once", lambda *a: queue.pop(0))
    return fixture(None, None, None)


def test_the_probe_passes_after_a_client_timeout_retry(monkeypatch, capsys):
    monkeypatch.delenv("DVT_WEBSID1", raising=False)
    assert run_probe(monkeypatch, [("client_timeout", "the probe's own 40 s read timeout"), ("follows", "still open")]) is True
    assert "attempt 1: client_timeout" in capsys.readouterr().out


def test_a_server_bye_skips_or_fails_in_strict_mode(monkeypatch):
    monkeypatch.delenv("DVT_WEBSID1", raising=False)
    with pytest.raises(pytest.skip.Exception, match="image without WEBSID1.*bye"):
        run_probe(monkeypatch, [("bye", "session ended")])
    monkeypatch.setenv("DVT_WEBSID1", "on")
    with pytest.raises(pytest.fail.Exception, match="DVT_WEBSID1=on but"):
        run_probe(monkeypatch, [("bye", "session ended")])


def test_two_drops_in_a_row_are_a_verdict_one_is_not(monkeypatch):
    monkeypatch.delenv("DVT_WEBSID1", raising=False)
    assert run_probe(monkeypatch, [("dropped", "x"), ("follows", "y")]) is True
    with pytest.raises(pytest.skip.Exception, match="dropped without a `bye` in two attempts"):
        run_probe(monkeypatch, [("dropped", "x"), ("dropped", "y")])


def test_an_inconclusive_probe_is_neither_a_pass_nor_an_image_verdict(monkeypatch):
    monkeypatch.delenv("DVT_WEBSID1", raising=False)
    outcomes = [("client_timeout", "a"), ("no_hello", "b"), ("client_timeout", "c")]
    with pytest.raises(pytest.skip.Exception, match="inconclusive"):
        run_probe(monkeypatch, outcomes)
    monkeypatch.setenv("DVT_WEBSID1", "on")
    with pytest.raises(pytest.fail.Exception, match="inconclusive"):
        run_probe(monkeypatch, outcomes)


def test_a_failed_step_up_in_the_probe_fails_at_once(monkeypatch):
    with pytest.raises(pytest.fail.Exception, match="step-up of the probe answered HTTP 403"):
        run_probe(monkeypatch, [("step_up_failed", "HTTP 403")])


class FakeTab:
    def __init__(self):
        self.codes = []

    def evaluate(self, js, arg):
        self.codes.append(arg["code"])
        return {"status": 200, "csrf_before": "c"}

    def close(self):
        pass


class CountingAuthenticator:
    def __init__(self):
        self.taken = []

    def next_code(self):
        self.taken.append(len(self.taken))
        return "code%d" % len(self.taken)


def fake_login():
    login = object.__new__(case.Login)
    auth = CountingAuthenticator()
    client = types.SimpleNamespace(authenticator=auth, hide=lambda code: None)
    login.account = types.SimpleNamespace(client=client)
    login.pending_code = None
    login.generation = 0
    tab = FakeTab()
    login.second_tab = lambda: tab
    return login, auth, tab


def test_a_prepared_step_up_code_is_used_once_and_not_taken_again():
    login, auth, tab = fake_login()
    login.prepare_step_up()
    assert auth.taken == [0] and login.pending_code == "code1"
    assert login.step_up() == (200, "c")
    assert tab.codes == ["code1"] and auth.taken == [0] and login.pending_code is None
    login.step_up()                                       # nothing prepared: takes its own
    assert tab.codes == ["code1", "code2"] and auth.taken == [0, 1]


def test_a_step_up_moves_the_cookie_generation_only_when_it_succeeds():
    login, _auth, tab = fake_login()
    login.generation = 0
    login.step_up()
    assert login.generation == 1
    tab.evaluate = lambda js, arg: {"status": 403, "csrf_before": "c"}
    login.step_up()
    assert login.generation == 1


def test_inconclusive_is_a_skip_unless_the_image_must_have_the_rule(monkeypatch):
    monkeypatch.delenv("DVT_WEBSID1", raising=False)
    with pytest.raises(pytest.skip.Exception):
        case.inconclusive("x")
    monkeypatch.setenv("DVT_WEBSID1", "on")
    with pytest.raises(pytest.fail.Exception):
        case.inconclusive("x")
