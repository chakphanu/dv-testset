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
def test_strict_switch(monkeypatch, value, want):
    monkeypatch.setenv("DVT_WEBSID1", value)
    assert case.strict() is want
