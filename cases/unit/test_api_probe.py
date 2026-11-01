# SPDX-License-Identifier: MIT
"""dvtest/api_probe.py: the authorization verdicts of the role matrix and the probe sender."""
import json

import pytest

from dvtest import api_admin, api_probe, api_web
from dvtest.api_probe import Result, expectation, judge

pytestmark = pytest.mark.unit

PROBLEM = "application/problem+json"


def res(status, code=None, ctype=PROBLEM, headers=None):
    body = json.dumps({"code": code}).encode() if code else b""
    return Result(status, dict({"content-type": ctype}, **(headers or {})), body)


def op(path, method="GET"):
    return next(o for o in api_web.WEB_OPS + api_web.WEB_ROOT_OPS + api_admin.ADMIN_OPS
                if o.path == path and o.method == method)


def test_expectation_by_floor():
    state, me, invites = op("/api/v1/public/state"), op("/api/v1/user/me"), op("/api/v1/operator/invites")
    assert [expectation(state, r) for r in api_probe.WEB_ROLES] == ["allow"] * 3
    assert [expectation(me, r) for r in api_probe.WEB_ROLES] == ["401", "allow", "allow"]
    assert [expectation(invites, r) for r in api_probe.WEB_ROLES] == ["401", "403", "allow"]
    offer = op("/api/v1/rtc/offer", "POST")
    assert [expectation(offer, r) for r in api_probe.WEB_ROLES] == ["refuse", "allow", "allow"]
    cfg, kick = op("/api/v1/admin/config/raw"), op("/api/v1/admin/edges/{kind}/clients/{circuit}/kick", "POST")
    assert [expectation(cfg, r, api_admin.ROLES) for r in api_admin.ROLES] == ["401", "403", "403", "allow"]
    assert [expectation(kick, r, api_admin.ROLES) for r in api_admin.ROLES] == ["401", "403", "allow", "allow"]
    health = op("/api/v1/admin/health")
    assert [expectation(health, r, api_admin.ROLES) for r in api_admin.ROLES] == ["allow"] * 4


def test_judge_denials():
    me = op("/api/v1/user/me")
    assert judge(me, "401", res(401, "unauthorized"))[0]
    assert not judge(me, "401", res(403, "forbidden"))[0]
    assert judge(me, "403", res(403, "forbidden"))[0]
    assert not judge(me, "403", res(403, "step_up_required"))[0], "a step-up 403 is not a role refusal"
    assert not judge(me, "403", res(401, "unauthorized"))[0]
    assert judge(me, "refuse", res(401))[0] and judge(me, "refuse", res(403))[0] and not judge(me, "refuse", res(400))[0]


def test_judge_allow_accepts_handler_answers_only():
    inv = op("/api/v1/operator/invites", "POST")
    for status in (200, 202, 400, 405, 415, 422, 428, 503):
        assert judge(inv, "allow", res(status, "validation" if status == 422 else None))[0], status
    assert not judge(inv, "allow", res(401, "unauthorized"))[0]
    assert not judge(inv, "allow", res(403, "forbidden"))[0]
    for code in ("csrf", "step_up_required", "totp_invalid", "totp_enrolment_required"):
        assert judge(inv, "allow", res(403, code))[0], code
    assert not judge(inv, "allow", res(404, ctype="text/plain"))[0]
    assert not judge(inv, "allow", res(429, "rate_limited"))[0]
    assert not judge(inv, "allow", res(500, "internal"))[0] and not judge(inv, "allow", res(502))[0]


def test_judge_404_and_401_exceptions():
    gone = op("/api/v1/operator/accounts/{callsign}")
    assert gone.missing_ok
    assert judge(gone, "allow", res(404, "not_found"))[0], "the handler's own problem+json 404"
    assert not judge(gone, "allow", res(404, ctype="text/plain"))[0], "the router's fail-closed 404 is no handler answer"
    head = op("/api/v1/user/recordings/{id}/audio.wav", "HEAD")
    assert judge(head, "allow", Result(404, {}, b""))[0], "a HEAD 404 has no body to tell"
    login = op("/api/v1/auth/session", "POST")
    assert login.credential_401 and judge(login, "allow", res(401, "unauthorized"))[0]
    listing = op("/api/v1/operator/accounts")
    assert not listing.missing_ok and not judge(listing, "allow", res(404, "not_found"))[0]


def test_result_helpers():
    r = Result(429, {"content-type": PROBLEM, "retry-after": "5"}, b'{"code": "rate_limited", "retry_after": 5}')
    assert r.code() == "rate_limited" and r.retry_after() == 5 and r.is_problem()
    assert Result(200, {"content-type": "text/plain"}, b"x").code() is None
    assert Result(200, {"content-type": "application/json"}, b"not json").code() is None
    assert Result(200, {"retry-after": "soon"}).retry_after() is None
    assert "429 rate_limited" in r.brief()


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def request(self, method, path, json_body=None, headers=None, raw_body=None):
        self.calls.append((method, path, json_body, headers, raw_body))
        status, hdr = self.answers.pop(0)
        return api_probe.web.Response(status, hdr, b"")


def test_send_builds_the_probe_and_retries_a_429(monkeypatch):
    monkeypatch.setattr(api_probe.time, "sleep", lambda s: None)
    force = op("/api/v1/operator/ids/sync/force", "POST")
    cli = FakeClient([(429, {"retry-after": "1"}), (415, {})])
    out = api_probe.send(cli, force)
    assert out.status == 415 and len(cli.calls) == 2
    method, path, body, headers, raw = cli.calls[-1]
    assert (method, path, body, raw) == ("POST", "/api/v1/operator/ids/sync/force", None, b"x")
    assert headers["Content-Type"] == "text/plain"
    kick = op("/api/v1/operator/edges/{kind}/clients/{circuit}/kick", "POST")
    cli = FakeClient([(404, {})])
    api_probe.send(cli, kick, headers={"X-Extra": "1"})
    assert cli.calls[0][1] == "/api/v1/operator/edges/xlx/clients/0.0/kick" and cli.calls[0][2] == {}
    assert cli.calls[0][3]["X-Extra"] == "1"
    cli = FakeClient([(429, {})] * 5)
    assert api_probe.send(cli, op("/api/v1/user/me"), retries=2).status == 429 and len(cli.calls) == 3


def test_judge_documented_guard_orders():
    """API4: web write guards (403 csrf) before the audience floor; admin checks the role first."""
    force = op("/api/v1/operator/ids/sync/force", "POST")
    assert force.raw is not None
    assert judge(force, "403", res(403, "csrf"))[0], "non-JSON probe: the unsafe-request guard answers before the floor"
    assert not judge(force, "403", res(403, "forbidden"))[0], "web: documented csrf before the floor"
    raw_put = op("/api/v1/admin/config/raw", "PUT")
    assert judge(raw_put, "403", res(403, "forbidden"))[0], "admin: the role floor before CSRF/content type"
    assert not judge(raw_put, "403", res(403, "csrf"))[0]
    inv = op("/api/v1/operator/invites", "POST")
    assert not judge(inv, "403", res(403, "csrf"))[0], "a JSON probe gets the role floor first"
    close = op("/api/v1/rtc/close", "POST")
    assert judge(close, "allow", res(403, "forbidden"))[0], "rtc ownership check of a circuit nobody owns"
    assert not judge(op("/api/v1/user/me"), "allow", res(403, "forbidden"))[0]
    ice = op("/api/v1/rtc/ice")
    assert judge(ice, "refuse", res(404, "not_found"))[0], "TURN disabled answers before the session check"
    assert not judge(ice, "refuse", res(404, ctype="text/plain"))[0]
    assert not judge(op("/api/v1/rtc/offer", "POST"), "refuse", res(404, "not_found"))[0]


def test_stream_probes_retry_longer(monkeypatch):
    slept = []
    monkeypatch.setattr(api_probe.time, "sleep", slept.append)

    class Stream:
        calls = 0

        def sse(self, path, max_events, timeout):
            Stream.calls += 1
            if Stream.calls < 4:
                raise api_probe.web.WebError("429", 429, "{}")
            return 200, "text/event-stream", [{"event": "hello"}]
    out = api_probe.send(Stream(), op("/api/v1/public/events"))
    assert out.status == 200 and out.frames == [{"event": "hello"}] and Stream.calls == 4 and len(slept) == 3
