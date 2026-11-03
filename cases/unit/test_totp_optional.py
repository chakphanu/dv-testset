# SPDX-License-Identifier: MIT
"""dvtest/totp_optional.py: the throw-away account table, answer parsing and the account-flow helpers,
against a fake client (no rig)."""
import json

import pytest

from dvtest import totp_optional as to
from dvtest import web

pytestmark = pytest.mark.unit

SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"           # RFC 6238 test key "12345678901234567890"


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self.body = json.dumps(body).encode() if body is not None else b""
        self.headers = headers or {}
        self.set_cookies = []

    def json(self):
        return json.loads(self.body)


class FakeClient:
    """Answers POST/GET from a script: {path: [Resp, ...]} consumed in order."""

    def __init__(self, script):
        self.script, self.calls, self.csrf, self.hidden = script, [], "csrf0", []
        self.sessions = 0
        self.authenticator = None

    def hide(self, *values):
        self.hidden.extend(v for v in values if v)

    def post(self, path, json_body=None, **kw):
        self.calls.append(("POST", path, json_body))
        return self.script[path].pop(0)

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        return self.script[path].pop(0)

    def session(self):
        self.sessions += 1
        return {}


def test_callsigns_are_unique_ascii_and_not_shared_ones():
    names = list(to.ACCOUNTS.values())
    assert len(names) == len(set(names))
    for name in names:
        assert name.isascii() and name.isupper() and name.startswith("N0TO") and len(name) <= 8
        assert name not in ("N0CALL", "N0OP")
    assert to.callsign_for("e", "operator") == "N0TOE"
    with pytest.raises(ValueError):
        to.callsign_for("e", "user")


def test_surface_paths():
    assert to.WEB.path("/totp/confirm") == "/api/v1/auth/session/totp/confirm"

    class Admin:
        @staticmethod
        def path(suffix):
            return "/api/v1/admin" + suffix
    assert to.admin_surface(Admin).path("/step-up") == "/api/v1/admin/session/step-up"


def test_problem_code_and_brief():
    problem = Resp(403, {"code": "step_up_required"})
    assert to.problem_code(problem) == "step_up_required"
    assert to.brief(problem) == "HTTP 403 step_up_required"
    assert to.problem_code(Resp(200)) is None and to.brief(Resp(204)) == "HTTP 204"
    assert to.problem_code(Resp(500, [1])) is None


def test_unspecified_names_what_the_spec_lists():
    text = to.unspecified("step-up with a wrong password", Resp(401, {"code": "unauthorized"}), "401 and 403")
    assert text == ("TOTP1: step-up with a wrong password -> HTTP 401 unauthorized; "
                    "the spec lists 401 and 403 and names no single answer")


def test_parse_offer():
    ok = Resp(200, {"secret": SECRET, "uri": "otpauth://totp/N0CALL?secret=" + SECRET, "digits": 6, "period": 30},
              {"cache-control": "no-store"})
    assert to.parse_offer(ok) == (SECRET, "otpauth://totp/N0CALL?secret=" + SECRET, 6, 30)
    assert to.no_store(ok) and not to.no_store(Resp(200))
    for bad in (Resp(403, {"code": "step_up_required"}),
                Resp(200, {"secret": SECRET}),
                Resp(200, {"secret": "not base32 !", "uri": "otpauth://totp/x", "digits": 6, "period": 30}),
                Resp(200, {"secret": SECRET, "uri": "https://x", "digits": 6, "period": 30})):
        with pytest.raises(ValueError):
            to.parse_offer(bad)


def test_adopt_rotation_takes_the_csrf_of_the_answer_or_reads_the_session():
    client = FakeClient({})
    to.adopt_rotation(client, Resp(200, {"ok": True, "csrf": "csrf1", "totp": "ok"}))
    assert client.csrf == "csrf1" and client.sessions == 0 and "csrf1" in client.hidden
    to.adopt_rotation(client, Resp(200, {"ok": True, "ttl_s": 300}))
    assert client.sessions == 1


def test_step_up_password_adopts_only_a_success():
    sf = to.WEB
    client = FakeClient({sf.path("/step-up"): [Resp(401, {"code": "unauthorized"}),
                                               Resp(200, {"ok": True, "csrf": "c2", "totp": "off", "step_up_s": 300})]})
    assert to.step_up_password(client, sf, "pw-wrong").status == 401 and client.csrf == "csrf0"
    assert to.step_up_password(client, sf, "pw-right").status == 200 and client.csrf == "c2"
    assert client.calls[0] == ("POST", "/api/v1/auth/session/step-up", {"password": "pw-wrong"})
    assert "pw-wrong" in client.hidden


def test_enable_totp_runs_the_documented_steps_in_order():
    sf = to.WEB
    offer = {"secret": SECRET, "uri": "otpauth://totp/N0TOA?secret=" + SECRET, "digits": 6, "period": 30}
    client = FakeClient({
        sf.path("/step-up"): [Resp(200, {"ok": True, "csrf": "c1", "totp": "off", "step_up_s": 300})],
        sf.path("/totp"): [Resp(200, offer)],
        sf.path("/totp/confirm"): [Resp(200, {"ok": True, "csrf": "c2", "totp": "ok"})],
    })
    auth = to.enable_totp(client, sf, "pw")
    assert [c[1] for c in client.calls] == [sf.path("/step-up"), sf.path("/totp"), sf.path("/totp/confirm")]
    code = client.calls[2][2]["code"]
    assert len(code) == 6 and code.isdigit()
    assert client.authenticator is auth and client.csrf == "c2"
    assert SECRET in client.hidden


def test_enable_totp_stops_at_a_refused_step():
    sf = to.WEB
    client = FakeClient({sf.path("/step-up"): [Resp(401, {"code": "unauthorized"})]})
    with pytest.raises(web.WebError) as exc:
        to.enable_totp(client, sf, "pw")
    assert exc.value.status == 401 and len(client.calls) == 1


def test_disable_sends_password_then_code_and_adopts():
    sf = to.WEB
    client = FakeClient({sf.path("/totp/disable"): [Resp(200, {"ok": True, "csrf": "c3", "totp": "off"})]})
    assert to.disable(client, sf, "pw", "123456").status == 200
    assert client.calls[0] == ("POST", sf.path("/totp/disable"), {"password": "pw", "code": "123456"})
    assert client.csrf == "c3"


def test_session_alive():
    sf = to.WEB
    client = FakeClient({sf.session: [Resp(200, {"callsign": "N0TOA"}), Resp(401, {"code": "unauthorized"})]})
    assert to.session_alive(client, sf) == (True, {"callsign": "N0TOA"})
    alive, resp = to.session_alive(client, sf)
    assert not alive and resp.status == 401


def test_ruled_answers_are_named_constants():
    assert to.TOTP_INVALID == (403, "totp_invalid") and to.STEP_UP_REQUIRED == (403, "step_up_required")
    assert to.ADMIN_WRONG_PASSWORD == to.TOTP_INVALID
    assert to.want_text(to.TOTP_INVALID) == "403 totp_invalid"
    assert to.is_answer(Resp(403, {"code": "totp_invalid"}), to.TOTP_INVALID)
    assert not to.is_answer(Resp(403, {"code": "step_up_required"}), to.TOTP_INVALID)
    assert not to.is_answer(Resp(401, {"code": "totp_invalid"}), to.TOTP_INVALID)


def test_web_wrong_password_is_totp1b_pending():
    """TOTP1b pending (dv-router): the web side answers a wrong password at the step-up and at disable with 401
    unauthorized, admin with 403 totp_invalid. When the web fix lands, set WEB_WRONG_PASSWORD to TOTP_INVALID in
    dvtest/totp_optional.py and change this test with it (it is meant to fail then)."""
    assert to.WEB_WRONG_PASSWORD == (401, "unauthorized")
    assert to.WEB_WRONG_PASSWORD != to.ADMIN_WRONG_PASSWORD
