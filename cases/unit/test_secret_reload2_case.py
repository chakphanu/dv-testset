# SPDX-License-Identifier: MIT
"""cases/test_secret_reload.py, SECRELOAD2: the two admin reload cases run against a fake admin service that follows the DV-Router
documents (openapi-admin.yaml ReloadPreview / ReloadApplied, admin.md "Secret reload", 11e19ace): roles, step-up window, If-Match,
audit, the launcher's restart of the owning child. No rig."""
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_secret_reload as case  # noqa: E402
from dvtest import secret_reload as sr  # noqa: E402

pytestmark = pytest.mark.unit

KEY = "edges.aprsis.passcode_file"
SUT_PATH = "/etc/dv-router/aprs-secrets/passcode"
RAW = '[node]\ncallsign = "XLX999"\n\n[edges.aprsis]\nenable = true\npasscode_file = "%s"\n' % SUT_PATH


class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.body.decode())


def problem(status, code, **extra):
    return Resp(status, dict({"code": code, "status": status}, **extra), {"content-type": "application/problem+json"})


class Sut:
    """The fake admin service plus the launcher and the APRS-IS edge behind it."""

    def __init__(self, path, fixture, has_route=True, weak_preview=False):
        self.path, self.fixture, self.has_route = path, fixture, has_route
        self.running = path.read_text()
        self.pid, self.edge_restarts, self.other_restarts = 100, 0.0, 2.0
        self.inc, self.gen = 1, 3
        self.etag = "f-00112233445566"
        self.history, self.audit_items = [], []
        self.clock = [0.0]
        self.posts = []

    @property
    def verified(self):
        return self.running == self.fixture

    def audit(self, action, user, result, changed=None):
        item = {"action": action, "user": user, "result": result, "role": "admin", "target": "x", "remote": "r", "ts_ms": 0}
        if changed is not None:
            item["diff"] = {"changed": changed}
        self.audit_items.append(item)

    def content(self):
        try:
            return self.path.read_text(), None
        except OSError:
            return None, [{"path": KEY, "pointer": "/edges/aprsis/passcode_file", "msg": "cannot read secret file %s: permission denied" % SUT_PATH}]


class Client:
    def __init__(self, sut, role="admin", user="ada", session=True):
        self.sut, self.role, self.user, self.has_session = sut, role, user, session
        self.identity = {"user": user, "role": role, "csrf": "c"}
        self.stepped_at = None

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def scrub(self, text):
        return text

    def clone(self):
        return Client(self.sut, session=False)

    def session(self):
        return self.identity

    def step_up(self):
        self.stepped_at = self.sut.clock[0]

    def stepped(self):
        return self.stepped_at is not None and self.sut.clock[0] - self.stepped_at <= 300.0

    # -- guards: session -> role -> step-up
    def guard(self, action, denied_action=None):
        if not self.has_session:
            return problem(401, "unauthorized")
        if self.role != "admin":
            if denied_action:
                self.sut.audit(denied_action, self.user, "denied")
            return problem(403, "forbidden")
        if not self.stepped():
            return problem(403, "step_up_required")
        return None

    def get(self, path, expect=None, **kw):
        sut = self.sut
        route = path[len("/api/v1/admin"):].split("?")[0]
        if route == "/config/raw":
            denied = self.guard("raw")
            return denied or Resp(200, RAW.encode(), {"ETag": '"%s"' % sut.etag})
        if route == "/config/history":
            return self.guard("history") or Resp(200, sut.history)
        if route == "/audit":
            return self.guard("audit") or Resp(200, {"total": len(sut.audit_items), "items": sut.audit_items})
        if route == "/config/reload":
            if not sut.has_route:
                return problem(404, "not_found")
            denied = self.guard("preview", "config.reload.preview")
            if denied:
                return denied
            text, errors = sut.content()
            if errors:
                return problem(422, "validation", errors=errors, current_etag=sut.etag)
            changed = [KEY] if text != sut.running else []
            actions = [{"class": "Restart", "child": "dv-edge-aprsis", "path": KEY, "impact": "%s content changed; restart dv-edge-aprsis" % KEY}] if changed else []
            return Resp(200, {"mode": "preview", "etag": sut.etag, "changed": changed, "no_change": not changed, "actions": actions, "warnings": []},
                        {"ETag": '"%s"' % sut.etag})
        if route == "/session":
            return Resp(200, self.identity)
        return problem(404, "not_found")

    def post(self, path, json_body=None, headers=None, **kw):
        sut = self.sut
        assert path.endswith("/config/reload"), path
        if not sut.has_route:
            return problem(404, "not_found")
        denied = self.guard("reload", "config.reload")
        if denied:
            return denied
        if_match = (headers or {}).get("If-Match")
        if if_match is None:
            sut.audit("config.reload", self.user, "denied")
            return problem(428, "precondition_required", current_etag=sut.etag)
        if if_match.startswith("W/") or if_match.strip('"') != sut.etag:
            sut.audit("config.reload", self.user, "denied")
            return problem(412, "etag_mismatch", current_etag=sut.etag)
        sut.audit("config.reload", self.user, "requested", [])
        sut.posts.append(if_match)
        text, errors = sut.content()
        if errors:
            sut.audit("config.reload", self.user, "invalid", [e["path"] for e in errors])
            return problem(422, "validation", errors=errors, current_etag=sut.etag)
        changed = [KEY] if text != sut.running else []
        actions = []
        if changed:
            sut.running = text
            sut.pid += 1
            sut.edge_restarts += 1
            sut.gen += 1
            actions = [{"class": "Restart", "child": "dv-edge-aprsis", "path": KEY, "impact": "%s content changed; restart dv-edge-aprsis" % KEY}]
        sut.audit("config.reload", self.user, "applied" if changed else "unchanged", changed)
        return Resp(200, {"status": "applied", "etag": sut.etag, "changed": changed, "no_change": not changed, "inc": sut.inc, "gen": sut.gen,
                          "actions": actions, "pending_restart": []}, {"ETag": '"%s"' % sut.etag})


def build(monkeypatch, tmp_path, **kw):
    if os.geteuid() == 0:
        pytest.skip("mode 000 does not stop root")
    out = tmp_path / "out"
    (out / "aprs-secrets").mkdir(parents=True)
    (out / "secreload").mkdir()
    path = out / "aprs-secrets" / "passcode"
    path.write_text("4242\n")
    os.chmod(path, 0o600)
    sut = Sut(path, "4242\n", **kw)
    entries = sr.entries_for("4242\n", "31337\n")
    ctx = types.SimpleNamespace(out=out, path=path, old="4242\n", new="31337\n", entries=entries, leaks=sr.Leaks(entries),
                                control=None, target=types.SimpleNamespace(out=str(out)), marker=0, client=Client(sut), stepped=None)
    monkeypatch.setattr(case.time, "monotonic", lambda: sut.clock[0])
    monkeypatch.setattr(case.time, "sleep", lambda s: sut.clock.__setitem__(0, sut.clock[0] + s))
    monkeypatch.setattr(case, "hub", lambda c: (sut.verified, sut.edge_restarts, sut.edge_restarts + sut.other_restarts))
    monkeypatch.setattr(case, "pid_of_edge", lambda c: (sut.pid, 1))
    monkeypatch.delenv("DVT_SECRELOAD2", raising=False)
    return ctx, sut


def test_main_case_follows_the_documented_reload_routes(monkeypatch, tmp_path):
    ctx, sut = build(monkeypatch, tmp_path)
    case.test_secreload2_preview_and_reload_apply_a_replaced_secret(ctx.target, None, ctx)
    assert sut.edge_restarts == 2.0 and len(sut.posts) == 4 and sut.history == []
    assert ctx.path.read_text() == "4242\n" and oct(ctx.path.stat().st_mode & 0o777) == "0o600"
    notes = (ctx.out / case.LOG).read_text()
    assert "31337" not in notes and "4242" not in notes and "R6" in notes and "R7" in notes
    seq = [(r) for _a, _u, r, _c in sr.reload_audit({"items": sut.audit_items}, user="ada")]
    assert seq == ["requested", "applied", "requested", "unchanged", "requested", "applied", "requested", "invalid"]


def test_main_case_skips_an_image_without_the_route_and_fails_in_strict_mode(monkeypatch, tmp_path):
    ctx, _sut = build(monkeypatch, tmp_path / "a", has_route=False)
    with pytest.raises(pytest.skip.Exception, match="image without SECRELOAD2"):
        case.test_secreload2_preview_and_reload_apply_a_replaced_secret(ctx.target, None, ctx)
    ctx, _sut = build(monkeypatch, tmp_path / "b", has_route=False)
    monkeypatch.setenv("DVT_SECRELOAD2", "on")
    with pytest.raises(pytest.fail.Exception, match="DVT_SECRELOAD2=on"):
        case.test_secreload2_preview_and_reload_apply_a_replaced_secret(ctx.target, None, ctx)


def test_main_case_fails_when_the_reload_edits_the_toml_or_ignores_the_secret(monkeypatch, tmp_path):
    ctx, sut = build(monkeypatch, tmp_path / "a")
    original = Client.post

    # a service that restarts nothing for a replaced secret
    monkeypatch.setattr(Client, "post", lambda self, path, json_body=None, headers=None, **kw: Resp(
        200, {"status": "applied", "etag": sut.etag, "changed": [], "no_change": True, "inc": 1, "gen": 3, "actions": [], "pending_restart": []},
        {"ETag": '"%s"' % sut.etag}))
    with pytest.raises(AssertionError, match="changed"):
        case.test_secreload2_preview_and_reload_apply_a_replaced_secret(ctx.target, None, ctx)
    monkeypatch.setattr(Client, "post", original)


def test_guards_case_follows_the_documented_refusals(monkeypatch, tmp_path):
    ctx, sut = build(monkeypatch, tmp_path)
    sessions = {"viewer": Client(sut, "viewer", "vic"), "operator": Client(sut, "operator", "otto")}
    case.test_secreload2_guards_and_preconditions(ctx.target, None, ctx, lambda role: sessions[role])
    assert sut.posts == [] and sut.pid == 100
    denied = [(i["action"], i["user"]) for i in sut.audit_items if i["result"] == "denied"]
    for user in ("vic", "otto"):
        assert ("config.reload.preview", user) in denied and ("config.reload", user) in denied
    assert [i["result"] for i in sut.audit_items if i["user"] == "ada"] == ["denied", "denied", "denied"]
    assert sut.clock[0] >= 305.0          # the case waited out the 5 minute step-up window
    assert ctx.stepped is None


def test_guards_case_fails_when_a_weak_etag_is_accepted(monkeypatch, tmp_path):
    ctx, sut = build(monkeypatch, tmp_path)
    original = Client.post

    def lax(self, path, json_body=None, headers=None, **kw):
        h = dict(headers or {})
        if h.get("If-Match", "").startswith("W/"):
            h["If-Match"] = '"%s"' % sut.etag
        return original(self, path, json_body, h, **kw)

    monkeypatch.setattr(Client, "post", lax)
    sessions = {"viewer": Client(sut, "viewer", "vic"), "operator": Client(sut, "operator", "otto")}
    with pytest.raises(AssertionError, match="weak If-Match"):
        case.test_secreload2_guards_and_preconditions(ctx.target, None, ctx, lambda role: sessions[role])


def test_guards_case_fails_when_a_viewer_may_reload(monkeypatch, tmp_path):
    ctx, sut = build(monkeypatch, tmp_path)
    sessions = {"viewer": Client(sut, "admin", "vic"), "operator": Client(sut, "operator", "otto")}
    with pytest.raises(AssertionError, match="viewer"):
        case.test_secreload2_guards_and_preconditions(ctx.target, None, ctx, lambda role: sessions[role])


def test_helpers_jbody_is_subsequence_and_history_len(monkeypatch, tmp_path):
    ctx, sut = build(monkeypatch, tmp_path)
    assert case.jbody(Resp(200, b"not json")) == "not json" and case.jbody(Resp(200, {"a": 1})) == {"a": 1}
    assert case.is_subsequence(["b", "d"], ["a", "b", "c", "d"]) and not case.is_subsequence(["d", "b"], ["a", "b", "c", "d"])
    assert case.is_subsequence([], []) and not case.is_subsequence(["x"], [])
    assert case.history_len(ctx) == 0
    sut.history.append({"seq": 1})
    assert case.history_len(ctx) == 1
