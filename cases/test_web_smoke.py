# SPDX-License-Identifier: MIT
"""Web unit, stage 1: smoke of DV-Router's web service (dv-svc-web) behind the
rig's TLS proxy. Needs `scripts/run.sh --profile web` (DVT_SUT_LAUNCH=native, the
template in DVT_SUT_CONFIG, rig/config/dv-router/web.toml; docs/rig.md "Web
profile"); every case skips with the reason otherwise (cases/web_fixtures.py).

Documented behaviour only (docs/design/web.md, docs/api/openapi-web.yaml, API3
paths /api/v1/{public,auth,rtc,user,operator}); what the specs leave open is
observed and recorded as a FINDING (dvtest.web.finding), never asserted. The
register and login budgets are respected by dvtest.web.Pacer, so the whole file
takes well under two minutes. Browser-level behaviour (the signed-out dashboard
not fetching authenticated history, the cookie in a real browser) is stage 3.

  a  test_public_state_is_json           /api/v1/public/state: JSON, ETag, CORS *, 304; lastheard
  b  test_public_events_stream           /api/v1/public/events: SSE, prelude `retry: 3000`, first event `hello`, through the proxy
  c  test_old_public_paths_are_gone      the pre-API3 paths answer 404
  d  test_operator_enrolment             first login asks for enrolment, TOTP confirm rotates the session
  e  test_session_cookie                 __Host-dv_web, Secure, HttpOnly, SameSite=Strict, Path=/, no Domain
  f  test_operator_step_up_and_invites   X-DV-TOTP step-up on an ids command; invite list; account list
  g  test_users_register_and_login       may_tx false and true: register, no auto sign-in, login, /user/me
  h  test_roles                          anonymous 401 on user routes, user 403 on operator routes
  i  test_wrong_password                 documented 401 for a wrong password (own account: lockout)
  j  test_admin_health                   GET /health and /ready on the admin host (https://dvt-admin:8443)
  k  test_web_health_and_ready           WEB2: GET and HEAD /health and /ready of dv-svc-web through the proxy, no cookie
"""
import time

import pytest

from dvtest import web

pytestmark = [pytest.mark.rig, pytest.mark.web]

PUBLIC_STATE_KEYS = {"clients", "info", "live", "now_ms", "reflector", "rooms", "version"}   # openapi PublicState


def test_public_state_is_json(web_client):
    anon = web_client()
    resp = anon.get("/api/v1/public/state", expect=200)
    assert resp.headers["content-type"].startswith("application/json")
    state = resp.json()
    missing = PUBLIC_STATE_KEYS - set(state)
    assert not missing, "public state lacks %s (keys: %s)" % (sorted(missing), sorted(state))
    assert resp.headers.get("access-control-allow-origin") == "*"      # owner decision 2026-10-31
    assert "max-age=2" in resp.headers.get("cache-control", "")
    etag = resp.headers.get("etag")
    assert etag and etag.startswith("W/"), "weak ETag expected, got %r" % etag
    again = anon.get("/api/v1/public/state", headers={"If-None-Match": etag})
    assert again.status == 304, "conditional GET: HTTP %d" % again.status
    heard = anon.get("/api/v1/public/lastheard?limit=5", expect=200)
    assert heard.headers["content-type"].startswith("application/json")
    for row in state["clients"]["items"]:
        assert "ip" not in row and "port" not in row, "public client rows carry no address (web.md)"


def test_public_events_stream(web_client):
    status, ctype, frames = web_client().sse("/api/v1/public/events", max_events=5, timeout=15, until="hello")
    assert status == 200 and ctype.startswith("text/event-stream"), (status, ctype)
    assert frames, "no SSE frame within 15 s (proxy buffering the stream?)"
    # DV-Router (answer of 2026-11-01): the stream prelude is `retry: 3000` (the shared SSE library; admin.md says
    # "first line retry: 3000"; web.md and the OpenAPI do not mention it), then `event: hello` is the first event
    assert frames[0].get("retry") == "3000", "the stream starts with `retry: 3000`, first frame is %r" % (frames[0],)
    events = [f for f in frames if "event" in f]
    assert events and events[0]["event"] == "hello", "first event frame is %r" % (events[:1],)


@pytest.mark.parametrize("path", [
    "/api/public/state", "/api/public/lastheard", "/api/public/events",
    "/api/v1/session", "/api/v1/me", "/api/v1/register",
])
def test_old_public_paths_are_gone(web_client, path):
    resp = web_client().get(path)
    assert resp.status == 404, "%s: HTTP %d, API3 says 404 (no aliases)" % (path, resp.status)


def test_operator_enrolment(web_operator):
    ident = web_operator.session()
    assert ident["role"] == "operator"
    assert ident["totp"] == "ok", "after confirm the TOTP state is ok, got %r" % (ident["totp"],)
    assert web_operator.authenticator is not None


def test_session_cookie(web_operator):
    attrs = web_operator.cookie_attrs.get(web.COOKIE_NAME)
    assert attrs is not None, "no %s cookie (jar: %s)" % (web.COOKIE_NAME, sorted(web_operator.cookie_attrs))
    assert attrs.get("secure") is True
    assert attrs.get("httponly") is True
    assert str(attrs.get("samesite", "")).lower() == "strict"
    assert attrs.get("path") == "/"
    assert "domain" not in attrs, "a __Host- cookie must not carry Domain"


def test_operator_step_up_and_invites(web_operator):
    # the step-up window is open after enrolment; a fresh code in X-DV-TOTP also satisfies the gate
    cmd = web_operator.post("/api/v1/operator/ids/cmd", {"v": 1, "op": "list", "kind": "dmr"},
                            headers=web_operator.step_up_headers())
    assert cmd.status not in (401, 403), "ids command with step-up: HTTP %d %s" % (cmd.status, cmd.text()[:120])
    if cmd.status != 200:
        web.finding("POST /api/v1/operator/ids/cmd list/dmr -> HTTP %d (spec: 200 when the registry answers, 503 "
                    "otherwise): %s" % (cmd.status, web_operator.scrub(cmd.text())[:120]))
    invites = web_operator.get("/api/v1/operator/invites", expect=200).json()
    assert set(invites) >= {"items", "total"}, sorted(invites)
    accounts = web_operator.get("/api/v1/operator/accounts", expect=200).json()
    assert set(accounts) >= {"items", "total"}, sorted(accounts)
    assert accounts["total"] >= 1


@pytest.mark.parametrize("may_tx", [False, True], ids=["rx_only", "may_tx"])
def test_users_register_and_login(web_user, web_operator, may_tx):
    user = web_user(may_tx=may_tx)
    me = user.client.get("/api/v1/user/me", expect=200).json()
    assert me["callsign"] == user.callsign
    assert me["role"] == "user"
    assert me["may_tx"] is may_tx
    assert me["totp"] == "off", "users have no TOTP, got %r" % (me["totp"],)
    # same identity as GET /auth/session (web.md)
    same = user.client.session()
    assert same["callsign"] == me["callsign"] and same["role"] == me["role"]
    listed = web_operator.get("/api/v1/operator/accounts/%s" % user.callsign, expect=200).json()
    assert listed.get("callsign") == user.callsign
    sessions = user.client.get("/api/v1/user/me/sessions", expect=200).json()
    assert sessions["total"] >= 1


def test_roles(web_client, web_user):
    anon = web_client()
    for path in ("/api/v1/user/me", "/api/v1/user/rooms/A/messages", "/api/v1/operator/accounts"):
        assert anon.get(path).status == 401, "anonymous %s" % path
    user = web_user(may_tx=False)
    resp = user.client.get("/api/v1/operator/accounts")
    assert resp.status == 403, "user on an operator route: HTTP %d" % resp.status
    # room message history needs a user session (API3); a room of the web service
    msgs = user.client.get("/api/v1/user/rooms/A/messages", expect=200).json()
    assert set(msgs) >= {"items", "total"}, sorted(msgs)


def test_wrong_password(web_operator, web_client):
    # an own account, so the lockout of (account, address) touches no other case
    password = web.new_password()
    web_operator.hide(password)
    web_operator.register_user("N0CALL9", password, may_tx=False)
    anon = web_client()
    anon.hide(password)
    resp = anon.request("POST", "/api/v1/auth/session",
                        json_body={"callsign": "N0CALL9", "password": password + "x"})
    assert resp.status == 401, "wrong password: HTTP %d, documented 401" % resp.status
    assert "set-cookie" not in resp.headers
    unknown = anon.request("POST", "/api/v1/auth/session",
                           json_body={"callsign": "N0TEST", "password": password})
    assert unknown.status == 401, "unknown account: HTTP %d, documented as the same failure" % unknown.status


def test_admin_health(web_url_admin, web_ca):
    # dv-svc-admin is on in the web profile (rig/config/dv-router/admin.toml); it answers only for its host name,
    # so the cases reach it through the proxy's second listener (https://dvt-admin:8443)
    client = web.WebClient(web_url_admin, web_ca, timeout=5)
    resp = client.get("/health")
    assert resp.status == 200, "GET /health on the admin host: HTTP %d" % resp.status
    ready = client.get("/ready")
    assert ready.status in (200, 503), "GET /ready on the admin host: HTTP %d" % ready.status


READY_WAIT_S = 90.0       # /ready: at least 36 s since the start of dv-svc-web and RTC ownership recovery done


def _probe_checks(resp, method, path):
    """The documented shape of a probe answer (openapi-web.yaml webHealth / webReady): no-store, no cookie."""
    assert "no-store" in resp.headers.get("cache-control", ""), "%s %s: Cache-Control %r" % (
        method, path, resp.headers.get("cache-control"))
    assert "set-cookie" not in resp.headers, "%s %s sets a cookie" % (method, path)


def test_web_health_and_ready(web_client):
    """WEB2 (docs/api/openapi-web.yaml /health, /ready; web.md): the web service's own probes at the root of
    https://dvt-web, public (no session, no cookie sent or set), GET with a JSON body and HEAD with none, 405 for
    other methods. /ready answers 503 {"ready": false, "reasons": [...]} with Retry-After 5 until the hub answers,
    36 s since startup and RTC recovery are done; the case waits for its 200."""
    anon = web_client()
    resp = anon.request("GET", "/health")
    assert resp.status == 200, "GET /health: HTTP %d" % resp.status
    assert resp.headers.get("content-type", "").startswith("application/json"), resp.headers.get("content-type")
    assert resp.json() == {"status": "ok"}, resp.text()[:120]
    _probe_checks(resp, "GET", "/health")
    head = anon.request("HEAD", "/health")
    assert head.status == 200 and head.body == b"", ("HEAD /health", head.status, len(head.body))
    _probe_checks(head, "HEAD", "/health")
    deadline = time.monotonic() + READY_WAIT_S
    seen = []
    while True:
        ready = anon.request("GET", "/ready")
        seen.append(ready.status)
        if ready.status == 200 or time.monotonic() >= deadline:
            break
        assert ready.status == 503, "GET /ready: HTTP %d (documented 200 or 503)" % ready.status
        body = ready.json()
        assert body.get("ready") is False and set(body.get("reasons") or []) <= {"hub", "rtc_recovery"}, body
        assert ready.headers.get("retry-after") == "5", ready.headers.get("retry-after")
        time.sleep(1.0)
    if seen.count(503):
        web.finding("web /ready answered 503 %d time(s) before 200 (the rig had started its tests before dv-svc-web was "
                    "ready)" % seen.count(503))
    assert ready.status == 200, "GET /ready not 200 within %.0f s (statuses %s)" % (READY_WAIT_S, sorted(set(seen)))
    assert ready.headers.get("content-type", "").startswith("application/json")
    assert ready.json() == {"ready": True}, ready.text()[:120]
    _probe_checks(ready, "GET", "/ready")
    head = anon.request("HEAD", "/ready")
    assert head.status == 200 and head.body == b"", ("HEAD /ready", head.status, len(head.body))
    _probe_checks(head, "HEAD", "/ready")
    for path in ("/health", "/ready"):
        other = anon.request("POST", path, json_body={})
        assert other.status == 405, "POST %s: HTTP %d, documented 405" % (path, other.status)
    assert not anon.cookie_attrs, "the probes left cookies in the client: %s" % sorted(anon.cookie_attrs)
