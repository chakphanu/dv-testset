# SPDX-License-Identifier: MIT
"""Web unit, stage 2: API cases for DV-Router's HTTP APIs on the API3 path scheme
(/api/v1/{public,auth,rtc,user,operator} of dv-svc-web, /api/v1/admin of dv-svc-admin).
Needs `scripts/run.sh --profile web` (docs/rig.md "Web profile"; the admin host
https://dvt-admin:8443 and its three logins come with the profile); every case
skips with the reason otherwise (cases/web_fixtures.py).

The route tables are dvtest/api_web.py and dvtest/api_admin.py; the probes never
act (invalid bodies, missing targets, throwaway accounts for anything that ends a
session), and the matrix judges the AUTHORIZATION class only (dvtest/api_probe.py).
Statuses, guard order and headers that DV-Router documents (since API4, DV-Router
2fab0f8: docs/api/README.md, openapi-web.yaml, web.md "Guard order", admin.md) are
asserted, as are the declared success statuses of the real successful calls
(api_web.Op.success). What the specs leave open is observed and recorded with
web.finding(), never asserted. Results of the matrix go to $DVT_OUT/web-api-matrix.jsonl (one line per
probe: operation, role, status, verdict) for docs/web-api-cases.md.

Order matters: the matrix and the read cases first, the cases that spend login or
register budgets or restart the web service LAST (limits are in the SUT's memory).

  c  test_public_*                      snapshots, ETag/304, HEAD, CORS, cache, headers, SSE framing
  a  test_prefix_minimum_role           every web operation x anonymous / user / operator
  e  test_room_messages_*, test_ids_*   history needs a user session; ID sync-status needs an operator
     test_prefix_floor_*, test_method_*  unknown paths under a prefix, 405, the RFC 7807 error shape
  b  test_removed_*                     the 55 moved templates answer 404; the 4 RTC URLs still work
  d  test_own_account_*                 login, CSRF, Origin, password, own sessions, TOTP rules
     test_declared_success_*            invite 201, register 201, logout 204, reset 201, deletes 200
  g  test_xss_*                       reflected and stored fields: JSON type, nosniff, CSP of pages
  h  test_admin_*                       the admin API: matrix, mounts, wrong Origin, cookies, /health /ready
  f  test_limits_*                      MS9 login/register budgets, lockout, LOGIN1 (a missing code is a
                                        challenge, a wrong code a guess); restarts dv-svc-web, LAST
"""
import json
import os
import re
import threading
import time
from urllib.parse import quote

import pytest

from dvtest import admin as admin_mod
from dvtest import api_admin, api_probe, api_web, web
from web_fixtures import make_operator

pytestmark = [pytest.mark.rig, pytest.mark.web]

MATRIX_FILE = "web-api-matrix.jsonl"
HTML = re.compile(r"text/html", re.I)


# ------------------------------------------------------------------ helpers

def _out():
    return os.environ.get("DVT_OUT")


def record(kind, name, role, res, verdict, why=""):
    """One line per probe in $DVT_OUT/web-api-matrix.jsonl (no bodies, no secrets)."""
    out = _out()
    if not out:
        return
    line = {"kind": kind, "op": name, "role": role, "status": res.status if res is not None else None,
            "verdict": verdict, "why": why[:160]}
    try:
        with open(os.path.join(out, MATRIX_FILE), "a", encoding="ascii") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass


def op_id(op):
    return "%s %s" % (op.method, op.path.replace("/api/v1/", ""))


def revive(user):
    """Log a WebUser in again when its session is gone (after a revoke-all)."""
    try:
        user.client.get("/api/v1/auth/session", expect=200)
        return
    except web.WebError:
        pass
    fresh = web.WebClient(user.client.base_url)
    fresh._ctx = user.client._ctx
    fresh.hide(user.password)
    auth = user.client.authenticator
    fresh.login(user.callsign, user.password, auth.next_code() if auth else None)
    fresh.authenticator = auth
    user.client = fresh


def fresh_login(user):
    """A new session of the same account (a throwaway the probe may end)."""
    other = web.WebClient(user.client.base_url)
    other._ctx = user.client._ctx
    other.hide(user.password)
    auth = user.client.authenticator
    other.login(user.callsign, user.password, auth.next_code() if auth else None)
    other.authenticator = auth
    return other


def headers_ok(res, what):
    """Security headers every API answer carries."""
    assert res.headers.get("x-content-type-options", "").lower() == "nosniff", \
        "%s: X-Content-Type-Options is %r" % (what, res.headers.get("x-content-type-options"))
    assert not HTML.search(res.headers.get("content-type", "")), "%s: HTML content type" % what


PUBLIC_HEADERS = {"x-frame-options": "DENY", "cross-origin-opener-policy": "same-origin",
                  "referrer-policy": "no-referrer"}


def public_security_headers(res, what):
    """The security headers web.md "Public API" lists for every public reply since API4. A difference is a
    finding (they were not checked before 13e7e70), not an assertion."""
    for name, value in PUBLIC_HEADERS.items():
        if res.headers.get(name, "").lower() != value.lower():
            web.finding("%s: %s is %r, web.md (Public API) documents %r" % (what, name, res.headers.get(name), value))
    perm = res.headers.get("permissions-policy", "")
    if "microphone=()" not in perm or "camera=()" not in perm:
        web.finding("%s: Permissions-Policy %r, web.md (Public API) documents microphone=(), camera=()" % (what, perm))


def success_check(kind, op, role, res):
    """A 2xx answer of a probe must be the operation's declared success status (API4). The matrix judges the
    authorization class only, so a different 2xx is a finding there, not a failure."""
    if res is not None and 200 <= res.status < 300 and res.status != op.success:
        web.finding("%s %s as %s: HTTP %d, the spec declares %d for success" % (kind, op_id(op), role, res.status,
                                                                                 op.success))


def declared(method, path, ops=None):
    """The declared success status of an operation of the tables (api_web / api_admin)."""
    for op in ops or (api_web.WEB_OPS + api_web.WEB_ROOT_OPS + api_admin.ADMIN_OPS):
        if op.key == (method, path):
            return op.success
    raise KeyError((method, path))


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="module")
def anon(web_client):
    return web_client()


@pytest.fixture(scope="module")
def rx_user(web_user):
    return web_user("N0RX", may_tx=False)


@pytest.fixture(scope="module")
def tx_user(web_user):
    return web_user("N0TX", may_tx=True)


@pytest.fixture(scope="module")
def one_user(web_user):
    """A throwaway account for everything that ends or changes its own sessions."""
    return web_user("N0ONE", may_tx=False)


# ==================================================================== (c) public

def test_public_state_and_lastheard_contract(anon):
    for path in ("/api/v1/public/state", "/api/v1/public/lastheard?limit=5"):
        resp = anon.get(path, expect=200)
        what = path.split("?")[0]
        assert resp.headers["content-type"].startswith("application/json"), what
        assert resp.headers.get("access-control-allow-origin") == "*", what
        assert "public" in resp.headers.get("cache-control", "") and "max-age=2" in resp.headers["cache-control"], \
            "%s: cache-control %r" % (what, resp.headers.get("cache-control"))
        headers_ok(resp, what)
        assert resp.headers.get("etag", "").startswith("W/"), what
        # documented by API4 (web.md "Public API"): CSP on static files only, not on API replies
        assert resp.headers.get("content-security-policy") is None, "%s: CSP on an API reply" % what
        public_security_headers(resp, "GET " + what)
    state = anon.get("/api/v1/public/state", expect=200).json()
    assert {"clients", "info", "live", "now_ms", "reflector", "rooms", "version"} <= set(state), sorted(state)
    heard = anon.get("/api/v1/public/lastheard", expect=200).json()
    assert isinstance(heard, (dict, list)), type(heard)
    if isinstance(heard, dict):
        assert "items" in heard, sorted(heard)
    text = json.dumps(state)
    assert '"ip"' not in text and '"port"' not in text, "public state carries no client addresses"


@pytest.mark.parametrize("path", ["/api/v1/public/state", "/api/v1/public/lastheard"])
def test_public_conditional_and_head(anon, path):
    first = anon.get(path, expect=200)
    etag = first.headers["etag"]
    cond = anon.get(path, headers={"If-None-Match": etag})
    assert cond.status == 304, "%s conditional GET: HTTP %d" % (path, cond.status)
    assert cond.body == b"" and cond.headers.get("etag") == etag
    assert cond.headers.get("access-control-allow-origin") == "*"
    other = anon.get(path, headers={"If-None-Match": 'W/"dvt-not-the-etag"'})
    assert other.status == 200, "a different validator must not give 304"
    head = anon.request("HEAD", path)
    assert head.status == 200 and head.body == b"", "HEAD %s: HTTP %d, %d body bytes" % (path, head.status, len(head.body))
    assert head.headers["content-type"].startswith("application/json")
    assert head.headers.get("etag", "").startswith("W/")
    assert head.headers.get("access-control-allow-origin") == "*"
    cond_head = anon.request("HEAD", path, headers={"If-None-Match": head.headers["etag"]})
    assert cond_head.status == 304, "conditional HEAD: HTTP %d" % cond_head.status


def test_public_methods_and_cors_on_errors(anon):
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        resp = anon.request(method, "/api/v1/public/state", json_body={})
        assert resp.status == 405, "%s /api/v1/public/state: HTTP %d, documented 405" % (method, resp.status)
        if resp.headers.get("access-control-allow-origin") != "*":
            web.finding("%s /api/v1/public/state -> 405 without Access-Control-Allow-Origin: * (documented: every "
                        "reply of /api/v1/public/* carries it)" % method)
        assert resp.headers.get("cache-control", "").startswith("no-store") or "no-store" in resp.headers.get("cache-control", ""), \
            "errors are no-store: %r" % resp.headers.get("cache-control")
        assert resp.headers["content-type"].startswith("application/problem+json"), resp.headers["content-type"]
        headers_ok(resp, "405")
    unknown = anon.get("/api/v1/public/nope")
    assert unknown.status == 404, "another path under /api/v1/public/: HTTP %d" % unknown.status
    headers_ok(unknown, "public 404")
    opt = anon.request("OPTIONS", "/api/v1/public/state", headers={
        "Access-Control-Request-Method": "GET", "Origin": "https://third-party.example"})
    # documented by API4 (web.md "Public API", CORS): no preflight is served; OPTIONS is 405 with the same
    # allow-origin header and no allow-methods / allow-headers
    assert opt.status == 405, "OPTIONS /api/v1/public/state (CORS preflight): HTTP %d, documented 405" % opt.status
    assert opt.headers.get("access-control-allow-origin") == "*", opt.headers.get("access-control-allow-origin")
    assert "access-control-allow-methods" not in opt.headers and "access-control-allow-headers" not in opt.headers, \
        "OPTIONS: preflight headers, documented: none"
    # no credentials involved: a simple cross-origin GET works for a third-party page
    cross = anon.get("/api/v1/public/state", headers={"Origin": "https://third-party.example"}, expect=200)
    assert cross.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in cross.headers


def test_credentialed_answers_have_no_cors_and_are_no_store(rx_user):
    resp = rx_user.client.get("/api/v1/user/me", expect=200)
    assert "access-control-allow-origin" not in resp.headers, "credentialed routes send no CORS header"
    assert "no-store" in resp.headers.get("cache-control", "")
    headers_ok(resp, "/user/me")


def test_public_events_stream_framing(anon):
    status, ctype, frames = anon.sse("/api/v1/public/events", max_events=4, timeout=15, until="hello")
    hdr = anon.last_sse_headers
    assert status == 200 and ctype.startswith("text/event-stream"), (status, ctype)
    assert frames, "no SSE frame within 15 s (proxy buffering the stream?)"
    assert frames[0].get("retry") == "3000", "the stream starts with `retry: 3000`, first frame is %r" % (frames[0],)
    events = [f for f in frames if "event" in f]
    assert events and events[0]["event"] == "hello", "first event frame is %r" % (events[:1],)
    hello = json.loads(events[0]["data"])
    assert {"boot", "last_id"} <= set(hello), "hello carries boot and last_id: %s" % sorted(hello)
    gap = {"view", "now_ms"} - set(hello)
    if gap:
        web.finding("GET /api/v1/public/events hello data has keys %s; admin.md documents {boot, last_id, view, "
                    "now_ms} for the shared stream library (missing %s)" % (sorted(hello), sorted(gap)))
    assert hdr.get("access-control-allow-origin") == "*", "SSE carries the public CORS header"
    assert "no-store" in hdr.get("cache-control", ""), "SSE cache-control %r" % hdr.get("cache-control")
    if hdr.get("x-accel-buffering") != "no":
        web.finding("GET /api/v1/public/events: X-Accel-Buffering is %r (admin.md documents `no` for its stream)"
                    % hdr.get("x-accel-buffering"))
    assert hdr.get("x-content-type-options", "").lower() == "nosniff"
    known = {"hello", "resync", "session", "room", "status", "data", "bye"}
    for frame in events:
        assert frame["event"] in known, "unknown SSE event %r (spec: %s)" % (frame["event"], sorted(known))
        if "data" in frame:
            json.loads(frame["data"])                       # every data field is JSON
    ids = [f["id"] for f in frames if "id" in f]
    for fid in ids:
        assert re.match(r"^[A-Za-z0-9]+-[0-9]+$", fid), "event id %r is <boot>-<n>" % fid


def test_public_events_resume_and_topics(anon):
    # an unknown Last-Event-ID is answered with resync (never an error), per web.md
    status, ctype, frames = anon.sse("/api/v1/public/events?last_event_id=zz-99999999", max_events=5, timeout=10,
                                     until="resync")
    assert status == 200
    names = [f.get("event") for f in frames if "event" in f]
    assert "resync" in names or "hello" in names, "frames: %s" % names
    if "resync" not in names:
        web.finding("GET /api/v1/public/events?last_event_id=zz-99999999: frames %s; documented: `resync` for an "
                    "unknown id" % names)
    status, _ctype, frames = anon.sse("/api/v1/public/events?topics=status", max_events=3, timeout=8, until="hello")
    assert status == 200 and any(f.get("event") == "hello" for f in frames)


# ================================================================ (a) role matrix

WEB_MATRIX = [(op, role) for op in api_web.WEB_OPS + api_web.WEB_ROOT_OPS for role in api_probe.WEB_ROLES]

# Answers of matrix probes that DV-Router documents exactly since API4 (asserted on top of the class):
# (method, path) -> {role: (status, problem code or None)}
DOCUMENTED_PROBE_ANSWERS = {
    # web.md Account API: offer is 400 for users; an enrolled operator without X-DV-TOTP gets 403 totp_invalid
    ("POST", "/api/v1/auth/session/totp"): {"user": (400, None), "operator": (403, "totp_invalid")},
    # web.md "Guard order": Origin/JSON (403 csrf) before the audience floor and the handler; README UI1:
    # force checks the media type of a nonempty body
    ("POST", "/api/v1/operator/ids/sync/force"): {"user": (403, "csrf"), "operator": (403, "csrf")},
    # web.md RTC: unknown and foreign circuits both get 403 forbidden
    ("POST", "/api/v1/rtc/close"): {"user": (403, "forbidden"), "operator": (403, "forbidden")},
    ("POST", "/api/v1/rtc/alive"): {"user": (403, "forbidden"), "operator": (403, "forbidden")},
}
if os.environ.get("DVT_TURN") != "on":
    # web.md RTC: empty web.turn_urls is 404 before method, Origin, cookie or circuit checks (anonymous too)
    DOCUMENTED_PROBE_ANSWERS[("GET", "/api/v1/rtc/ice")] = {r: (404, "not_found") for r in api_probe.WEB_ROLES}


@pytest.mark.parametrize("op,role", WEB_MATRIX, ids=["%s | %s" % (op_id(op), role) for op, role in WEB_MATRIX])
def test_prefix_minimum_role(op, role, anon, rx_user, tx_user, web_operator, web_operator2, one_user):
    expect = api_probe.expectation(op, role)
    if role == "anonymous":
        client = anon
    elif role == "user":
        client = tx_user.client if op.needs_tx else rx_user.client
    else:
        client = web_operator
    temp = None
    if op.fresh and expect == "allow":
        # the probe ends the session it uses: run it on a throwaway login
        temp = fresh_login(one_user if role == "user" else web_operator2)
        client = temp
    try:
        res = api_probe.send(client, op)
    finally:
        if op.fresh and role == "operator":
            revive(web_operator2)
        elif op.fresh and role == "user" and temp is not None:
            revive(one_user)                  # revoke-all ended every login of the throwaway account
    ok, why = api_probe.judge(op, expect, res)
    record("web", op_id(op), role, res, "pass" if ok else "fail", why)
    assert ok, "%s as %s: %s (expected class %s)" % (op_id(op), role, why, expect)
    documented = DOCUMENTED_PROBE_ANSWERS.get(op.key, {}).get(role)
    if documented:
        status, code = documented
        assert res.status == status and (code is None or res.code() == code), \
            "%s as %s: %s, documented by API4: HTTP %d %s" % (op_id(op), role, res.brief(), status, code or "")
    elif res.status == 403 and expect == "allow" and res.code() != "forbidden":
        web.finding("%s as %s: 403 %s although the role is at or above the floor (a guard the documents do not "
                    "list for this probe)" % (op_id(op), role, res.code()))
    success_check("web", op, role, res)


def test_head_aliases_match_get(anon, web_operator):
    """HEAD where the spec lists it: same status and headers as GET, no body."""
    for method, path, client in (("/public/state", None, anon), ("/public/lastheard", None, anon),
                                 ("/operator/ids/rows?kind=dmr&limit=1", None, web_operator),
                                 ("/operator/ids/sync", None, web_operator)):
        full = "/api/v1" + method
        got, head = client.get(full), client.request("HEAD", full)
        assert head.status == got.status, "%s: GET %d, HEAD %d" % (full, got.status, head.status)
        assert head.body == b""
        assert head.headers.get("content-type") == got.headers.get("content-type"), full


PROBLEM_KEYS = {"type", "title", "status", "detail", "instance", "code"}
PROBLEM_OPTIONAL = {"errors", "retry_after", "current_etag"}


def test_prefix_floor_covers_unknown_paths(anon, rx_user, web_operator):
    """Documented by API4 (README "API3 audience paths", web.md "Account and operator API"): the router
    classifies first; an unclassified path under /user or /operator is 404 for every caller, anonymous
    included, before authentication; API route misses and missing targets both use 404 problem+json
    `not_found` (the two cannot be told apart, by design)."""
    for prefix in ("user", "operator"):
        path = "/api/v1/%s/dvt-no-such-route" % prefix
        answers = [c.get(path) for c in (anon, rx_user.client, web_operator)]
        got = tuple(r.status for r in answers)
        assert got == (404, 404, 404), "GET %s as anonymous/user/operator: %s, documented 404 for every caller" % (
            path, got)
        for resp in answers:
            res = api_probe.Result(resp.status, resp.headers, resp.body)
            if not (res.is_problem() and res.code() == "not_found"):
                web.finding("GET %s: 404 %s; web.md documents route misses as 404 problem+json not_found"
                            % (path, res.brief()))
    for prefix in ("public", "auth", "rtc"):
        path = "/api/v1/%s/dvt-no-such-route" % prefix
        resp = anon.get(path)
        assert resp.status == 404, "%s: HTTP %d" % (path, resp.status)
    resp = anon.get("/api/v1/dvt-no-such-prefix/x")
    res = api_probe.Result(resp.status, resp.headers, resp.body)
    assert resp.status == 404, "unclassified API path: HTTP %d, documented 404" % resp.status
    assert res.is_problem() and res.code() == "not_found", \
        "unclassified API path: %s, documented 404 problem+json not_found (same as a missing target)" % res.brief()


def test_method_not_allowed_and_problem_shape(anon, rx_user, web_operator):
    """405 method_not_allowed after the guards; every error body is the shared RFC 7807 shape (admin.md Errors)."""
    sample405 = [(rx_user.client, "PUT", "/api/v1/user/me"), (web_operator, "PATCH", "/api/v1/operator/clients"),
                 (rx_user.client, "POST", "/api/v1/user/me/sessions/" + api_web.NO_SESSION_ID),
                 (anon, "DELETE", "/api/v1/public/lastheard")]
    bodies = []
    for client, method, path in sample405:
        resp = client.request(method, path, json_body={})
        assert resp.status == 405, "%s %s: HTTP %d, documented 405" % (method, path, resp.status)
        bodies.append(resp)
    bodies += [anon.get("/api/v1/user/me"), rx_user.client.get("/api/v1/operator/clients"),
               web_operator.get("/api/v1/operator/accounts/" + api_web.NO_CALL),
               web_operator.request("POST", "/api/v1/operator/invites", json_body={}),
               rx_user.client.get("/api/v1/user/rooms/%s/messages?limit=abc" % api_web.ROOM)]
    expected = [405, 405, 405, 405, 401, 403, 404, 400, 400]
    codes = ["method_not_allowed"] * 4 + ["unauthorized", "forbidden", "not_found", None, None]
    for resp, status, code in zip(bodies, expected, codes):
        assert resp.status == status, "HTTP %d, wanted %d" % (resp.status, status)
        assert resp.headers["content-type"].startswith("application/problem+json"), resp.headers["content-type"]
        body = resp.json()
        assert PROBLEM_KEYS <= set(body) <= PROBLEM_KEYS | PROBLEM_OPTIONAL, "problem keys %s" % sorted(body)
        assert body["status"] == status and body["type"] == "/api/v1/problems/" + body["code"], body
        assert isinstance(body["instance"], str) and body["instance"], body
        if code:
            assert body["code"] == code, "code %r, documented %r" % (body["code"], code)
        assert "no-store" in resp.headers.get("cache-control", ""), "errors are no-store"
        headers_ok(resp, "error %d" % status)


# ============================================================== (e) messages and ids

def test_room_messages_need_a_user_session(anon, rx_user, tx_user, web_operator):
    path = "/api/v1/user/rooms/%s/messages" % api_web.ROOM
    assert anon.get(path).status == 401, "anonymous message history"
    assert anon.post(path, {"text": "x"}).status == 401
    for cli, who in ((rx_user.client, "user"), (web_operator, "operator")):
        body = cli.get(path + "?limit=2", expect=200).json()
        assert set(body) >= {"items", "total"}, "%s: %s" % (who, sorted(body))
        assert body["total"] == len(body["items"]) <= 2
        for item in body["items"]:
            assert "lat" not in item and "lon" not in item and "position" not in item, "no positions"
    bad = rx_user.client.get(path + "?limit=abc")
    assert bad.status == 400, "bad limit: HTTP %d, documented 400" % bad.status
    bad = rx_user.client.get(path + "?after=-1")
    assert bad.status == 400, "bad after: HTTP %d, documented 400" % bad.status
    unconfigured = rx_user.client.get("/api/v1/user/rooms/%s/messages" % "ZZ")
    assert unconfigured.status in (400, 404), "a room that is not one letter: HTTP %d" % unconfigured.status
    # sending: may_tx false is 403, a valid body from a may_tx account is accepted or refused by the controller
    denied = rx_user.client.post(path, {"text": "dvt probe"})
    assert denied.status == 403, "may_tx false: HTTP %d, documented 403" % denied.status
    unknown_field = tx_user.client.post(path, {"text": "dvt probe", "extra": 1})
    assert unknown_field.status == 400, "extra field: HTTP %d, documented 400" % unknown_field.status
    empty = tx_user.client.post(path, {"text": ""})
    assert empty.status == 400, "empty text: HTTP %d, documented 400 (empty after sanitising)" % empty.status
    no_csrf = tx_user.client.post(path, {"text": "dvt probe"}, headers={"X-DV-CSRF": None})
    assert no_csrf.status == 403, "missing CSRF token: HTTP %d, documented 403" % no_csrf.status


def test_ids_sync_status_needs_an_operator(anon, rx_user, web_operator):
    for method in ("GET", "HEAD"):
        path = "/api/v1/operator/ids/sync"
        assert anon.request(method, path).status == 401, "anonymous %s" % method
        assert rx_user.client.request(method, path).status == 403, "user %s" % method
        res = web_operator.request(method, path)
        assert res.status in (200, 503), "operator %s: HTTP %d, documented 200 or 503 (idsync silent)" % (method, res.status)
        if method == "HEAD":
            assert res.body == b""
    ok = web_operator.get("/api/v1/operator/ids/sync")
    if ok.status == 200:
        assert "live" in ok.json(), sorted(ok.json())
    # a foreign Origin on a read is the opaque 404 of the router, before authentication
    foreign = web_operator.get("/api/v1/operator/ids/sync", headers={"Origin": "https://evil.example"})
    assert foreign.status == 404, "foreign Origin on an IDs read: HTTP %d, documented 404" % foreign.status
    assert foreign.text().strip() == "not found", foreign.text()[:60]
    rows = web_operator.get("/api/v1/operator/ids/rows?kind=dmr&limit=1")
    assert rows.status in (200, 503), "ids rows: HTTP %d" % rows.status


# ================================================================= (b) removed urls

def _old_requests(client, methods, template, prefix=""):
    path = prefix + api_web.removed_probe_path(template)
    for method in methods:
        body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
        resp = client.request(method, path, json_body=body)
        yield method, path, resp


@pytest.mark.parametrize("service,methods,old,new", api_web.REMOVED_WEB,
                         ids=[r[2] for r in api_web.REMOVED_WEB])
def test_removed_web_urls_are_404(service, methods, old, new, anon, web_operator2):
    for who, client in (("anonymous", anon), ("operator", web_operator2.client)):
        for method, path, resp in _old_requests(client, methods, old):
            ok = resp.status == 404
            record("removed", "%s %s" % (method, old), who, resp, "pass" if ok else "fail")
            assert ok, "%s %s as %s: HTTP %d, API3 says 404 and no alias (new: %s)" % (
                method, path, who, resp.status, new)
            headers_ok(resp, path)


@pytest.mark.parametrize("service,methods,old,new", api_web.REMOVED_ADMIN,
                         ids=[r[2] for r in api_web.REMOVED_ADMIN])
def test_removed_admin_urls_are_404(service, methods, old, new, web_url_admin, web_ca, admin_session):
    admin_host = admin_mod.AdminClient(web_url_admin, web_ca)
    admin_prefix = admin_mod.AdminClient(web_url_admin, web_ca, mount="prefix")
    for mount, who, client, prefix in (("host", "anonymous", admin_host, ""),
                                       ("host", "admin", admin_session("admin"), ""),
                                       ("prefix", "anonymous", admin_prefix, "/admin"),
                                       ("prefix", "admin", admin_session("admin", "prefix"), "/admin")):
        for method, path, resp in _old_requests(client, methods, old, prefix):
            ok = resp.status == 404
            record("removed", "%s %s [%s]" % (method, old, mount), who, resp, "pass" if ok else "fail")
            assert ok, "%s %s on the %s mount as %s: HTTP %d, API3 says 404 and no alias (new: %s)" % (
                method, path, mount, who, resp.status, new)


def test_unchanged_rtc_urls_still_work(rx_user, anon):
    assert len(api_web.UNCHANGED) == 4 and [r[2] for r in api_web.UNCHANGED] == list(api_web.RTC_PATHS)
    for op in api_web.WEB_OPS:
        if op.path not in api_web.RTC_PATHS:
            continue
        res = api_probe.send(rx_user.client, op)
        ok, why = api_probe.judge(op, "allow", res)
        assert ok, "%s as a signed-in user: %s" % (op_id(op), why)
        if op.path in ("/api/v1/rtc/close", "/api/v1/rtc/alive"):
            # documented by API4 (web.md RTC): an unknown circuit is the same 403 forbidden as a foreign one
            assert (res.status, res.code()) == (403, "forbidden"), "%s, unknown circuit: %s" % (op_id(op), res.brief())


def test_services_do_not_serve_each_others_paths(anon, web_url_admin, web_ca):
    """/admin/* and admin API paths are 404 on the web service, the public and
    operator API paths are 404 on the admin host (admin.md Mounts, web.md API split)."""
    for path in ("/api/v1/admin/status", "/api/v1/admin/health", "/admin/", "/admin/api/v1/admin/health"):
        resp = anon.get(path)
        assert resp.status == 404, "web service %s: HTTP %d" % (path, resp.status)
    adm = admin_mod.AdminClient(web_url_admin, web_ca)
    for path in ("/api/v1/public/state", "/api/v1/public/events", "/api/v1/operator/ids/sync",
                 "/api/v1/auth/session", "/api/v1/user/me"):
        resp = adm.get(path)
        assert resp.status == 404, "admin host %s: HTTP %d" % (path, resp.status)


# ================================================================ (d) own account

def test_own_account_identity_and_cookie(rx_user, web_operator):
    me = rx_user.client.get("/api/v1/user/me", expect=200)
    ident = rx_user.client.get("/api/v1/auth/session", expect=200)
    assert me.json()["callsign"] == ident.json()["callsign"] == rx_user.callsign
    for resp in (me, ident):
        assert "no-store" in resp.headers.get("cache-control", "")
        text = resp.text()
        assert '"ip"' not in text and "$argon2" not in text, "no address and no hash in an account answer"
    attrs = rx_user.client.cookie_attrs.get(web.COOKIE_NAME, {})
    assert attrs.get("secure") is True and attrs.get("httponly") is True
    assert str(attrs.get("samesite", "")).lower() == "strict" and attrs.get("path") == "/" and "domain" not in attrs


def test_own_account_csrf_origin_and_content_type(one_user):
    cli = one_user.client
    path = "/api/v1/user/me/sessions/%s" % api_web.NO_SESSION_ID
    # CSRF token missing or wrong: 403 (code csrf)
    for hdr, what in (({"X-DV-CSRF": None}, "no CSRF token"), ({"X-DV-CSRF": "wrong-token-value"}, "wrong CSRF token")):
        resp = cli.request("DELETE", path, headers=hdr)
        assert resp.status == 403, "%s: HTTP %d, documented 403" % (what, resp.status)
        if api_probe.Result(resp.status, resp.headers, resp.body).code() != "csrf":
            web.finding("DELETE own session, %s: 403 code %r, documented `csrf`" % (
                what, api_probe.Result(resp.status, resp.headers, resp.body).code()))
    # Origin: a foreign one is refused with 403 csrf (web.md "Guard order", API4), none at all is refused too
    for origin, what in (("https://evil.example", "foreign Origin"), ("https://dvt-web.evil.example", "look-alike Origin")):
        resp = cli.request("DELETE", path, headers={"Origin": origin})
        assert resp.status == 403, "%s: HTTP %d, documented 403 csrf (Origin/JSON write guard)" % (what, resp.status)
        code = api_probe.Result(resp.status, resp.headers, resp.body).code()
        if code != "csrf":
            web.finding("unsafe request with a %s: 403 code %r, web.md (Guard order) documents `csrf`" % (what, code))
    resp = cli.request("DELETE", path, headers={"Origin": None})
    assert resp.status in (403, 404), "no Origin on an unsafe request: HTTP %d" % resp.status
    resp = cli.request("DELETE", path, headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status in (403, 404), "Sec-Fetch-Site cross-site: HTTP %d" % resp.status
    resp = cli.request("POST", "/api/v1/auth/password", raw_body=b"x", headers={"Content-Type": "text/plain"})
    # documented by API4 (web.md "Guard order"): a nonempty non-JSON body of a write is 403 csrf
    code = api_probe.Result(resp.status, resp.headers, resp.body).code()
    assert (resp.status, code) == (403, "csrf"), \
        "non-JSON content type on a JSON write: HTTP %d %s, documented 403 csrf" % (resp.status, code)
    resp = cli.request("POST", "/api/v1/auth/password", raw_body=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400, "malformed JSON: HTTP %d, documented 400" % resp.status
    # the own session is still alive after all those refusals
    cli.get("/api/v1/user/me", expect=200)


def test_own_account_sessions_and_password(web_operator, web_user):
    """A dedicated account: list, revoke one by its opaque id, change the password."""
    user = web_user("N0PWD", may_tx=False)
    first = user.client
    second = fresh_login(user)
    listed = first.get("/api/v1/user/me/sessions", expect=200).json()
    assert listed["total"] >= 2 and len(listed["items"]) == listed["total"]
    text = json.dumps(listed)
    for client in (first, second):
        for value in client.cookies.values():
            assert value not in text, "the session list must not contain cookie values"
    assert '"ip"' not in text, "own session list carries no addresses"
    ids = [row.get("id") for row in listed["items"]]
    assert all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids), ids
    # revoke one login by id: the other one keeps working
    victim = None
    revoke_one = declared("DELETE", "/api/v1/user/me/sessions/{id}")              # 200 (API4)
    for sid in ids:
        probe = second.request("DELETE", "/api/v1/user/me/sessions/%s" % sid)
        assert probe.status in (revoke_one, 404), "DELETE session by id: HTTP %d, declared %d" % (
            probe.status, revoke_one)
        if probe.status == revoke_one:
            victim = sid
            break
    assert victim, "no login could be revoked by id"
    alive = [c for c in (first, second) if c.request("GET", "/api/v1/user/me").status == 200]
    assert len(alive) == 1, "exactly one of the two logins survives a revoke by id (%d)" % len(alive)
    keeper = alive[0]
    # password change: wrong current password is refused, the right one revokes every login
    new_password = web.new_password()
    keeper.hide(new_password)
    wrong = keeper.post("/api/v1/auth/password", {"current_password": "wrong-" + user.password, "new_password": new_password})
    assert wrong.status in (401, 403), "wrong current password: HTTP %d" % wrong.status
    short = keeper.post("/api/v1/auth/password", {"current_password": user.password, "new_password": "short"})
    assert short.status in (400, 422), "password shorter than 8 bytes: HTTP %d, documented 8-1024 bytes" % short.status
    ok = keeper.post("/api/v1/auth/password", {"current_password": user.password, "new_password": new_password})
    assert ok.status == declared("POST", "/api/v1/auth/password"), "password change: HTTP %d %s, declared %d" % (
        ok.status, keeper.scrub(ok.text())[:100], declared("POST", "/api/v1/auth/password"))
    assert keeper.request("GET", "/api/v1/user/me").status == 401, "a password change revokes the logins"
    anon = web.WebClient(keeper.base_url)
    anon._ctx = keeper._ctx
    anon.hide(user.password, new_password)
    assert anon.login(user.callsign, user.password, expect=401).status == 401, "the old password no longer works"
    again = anon.login(user.callsign, new_password)
    assert again["callsign"] == user.callsign
    # revoke all own logins
    gone = anon.request("DELETE", "/api/v1/user/me/sessions")
    assert gone.status == declared("DELETE", "/api/v1/user/me/sessions"), "revoke all own logins: HTTP %d, declared %d" % (
        gone.status, declared("DELETE", "/api/v1/user/me/sessions"))
    assert anon.request("GET", "/api/v1/user/me").status == 401


def test_operator_totp_rules(web_operator2, web_client):
    op2 = web_operator2
    auth = op2.client.authenticator
    # an enrolled operator: password alone is not a login
    anon = web_client()
    anon.hide(op2.password)
    resp = anon.request("POST", "/api/v1/auth/session",
                        json_body={"callsign": op2.callsign, "password": op2.password})
    assert resp.status == 401, "operator without a TOTP code: HTTP %d, documented 401" % resp.status
    code = api_probe.Result(resp.status, resp.headers, resp.body).code()
    assert code == "totp_required", "operator without a TOTP code: code %r, documented totp_required (LOGIN1)" % code
    assert "set-cookie" not in resp.headers
    code = auth.next_code()
    anon.hide(code)
    ok = anon.request("POST", "/api/v1/auth/session", json_body={"callsign": op2.callsign, "password": op2.password,
                                                                "totp": code})
    assert ok.status == 200, "operator with a fresh code: HTTP %d" % ok.status
    # the same code twice: the replay guard refuses it
    replay = web_client()
    replay.hide(op2.password, code)
    again = replay.request("POST", "/api/v1/auth/session", json_body={"callsign": op2.callsign,
                                                                     "password": op2.password, "totp": code})
    assert again.status == 401, "a TOTP code used twice: HTTP %d, documented: accepted once" % again.status
    # re-enrolment needs the current authenticator's code in X-DV-TOTP, a window is not enough; documented by
    # API4 (web.md Account API): 403 totp_invalid (and one lockout failure for the account)
    offer = op2.client.request("POST", "/api/v1/auth/session/totp", json_body={})
    assert offer.status == 403, "re-enrolment without X-DV-TOTP: HTTP %d, documented 403" % offer.status
    assert "secret" not in offer.text(), "a refused offer must not carry a new secret"
    assert offer.json().get("code") == "totp_invalid", \
        "re-enrolment without a code: 403 code %r, documented totp_invalid" % offer.json().get("code")


def _hide_body_strings(client, resp):
    """Register every string value of a JSON answer as a secret (invite and reset codes come back once)."""
    try:
        data = json.loads(resp.body.decode("utf-8")) if resp.body else None
    except (ValueError, UnicodeDecodeError):
        return
    if isinstance(data, dict):
        client.hide(*[v for v in data.values() if isinstance(v, str)])


def _operator_action(client, method, path, body=None):
    """An operator write; when the handler asks for a step-up (403 step_up_required), once more with a code."""
    resp = client.request(method, path, json_body=body)
    if resp.status == 403 and api_probe.Result(resp.status, resp.headers, resp.body).code() == "step_up_required":
        resp = client.request(method, path, json_body=body, headers=client.step_up_headers())
    _hide_body_strings(client, resp)
    return resp


def test_declared_success_statuses_of_real_calls(web_operator, web_client):
    """Real successful calls of the account flow answer the status their operation declares (API4, openapi-web.yaml):
    invite issue 201, register 201, login 200, logout 204, invite revoke 200, password-reset code 201, account
    delete 200. Throwaway names: N0SUC (registered, then deleted; one token of the 0.2/s register budget) and
    N0SUD (an invite that is revoked)."""
    seen = []

    def check(method, template, resp, path=None):
        want = declared(method, template)
        seen.append("%s %s %d" % (method, template.replace("/api/v1/", ""), resp.status))
        assert resp.status == want, "%s %s: HTTP %d %s, declared %d" % (
            method, path or template, resp.status, web_operator.scrub(resp.text())[:120], want)

    callsign, password = "N0SUC", web.new_password()
    web_operator.hide(password)
    inv = _operator_action(web_operator, "POST", "/api/v1/operator/invites",
                           {"callsign": callsign, "role": "user", "may_tx": False})
    check("POST", "/api/v1/operator/invites", inv)
    invite = inv.json().get("code")
    anon = web_client()
    anon.hide(invite, password)
    reg = anon.request("POST", "/api/v1/auth/register", json_body={"callsign": callsign, "invite": invite,
                                                                    "password": password})
    check("POST", "/api/v1/auth/register", reg)
    user = web_client()
    user.hide(password)
    login = user.request("POST", "/api/v1/auth/session", json_body={"callsign": callsign, "password": password})
    check("POST", "/api/v1/auth/session", login)
    user.csrf = login.json().get("csrf")
    user.hide(user.csrf)
    out = user.request("DELETE", "/api/v1/auth/session")
    check("DELETE", "/api/v1/auth/session", out)
    assert user.request("GET", "/api/v1/auth/session").status == 401, "the session ends with the logout"
    # an invite that is revoked again
    inv2 = _operator_action(web_operator, "POST", "/api/v1/operator/invites",
                            {"callsign": "N0SUD", "role": "user", "may_tx": False})
    check("POST", "/api/v1/operator/invites", inv2)
    path = "/api/v1/operator/invites/N0SUD"
    check("DELETE", "/api/v1/operator/invites/{callsign}", _operator_action(web_operator, "DELETE", path), path)
    # a password-reset code for the throwaway account (shown once: hidden), then the account is removed
    path = "/api/v1/operator/accounts/%s/reset" % callsign
    check("POST", "/api/v1/operator/accounts/{callsign}/reset", _operator_action(web_operator, "POST", path, {}), path)
    path = "/api/v1/operator/accounts/%s" % callsign
    check("DELETE", "/api/v1/operator/accounts/{callsign}", _operator_action(web_operator, "DELETE", path), path)
    assert web_operator.get(path).status == 404, "the deleted account is gone"
    record("success", "account flow", "operator", None, "pass", "; ".join(seen))


# ================================================================== (g) xss / injection

PAYLOADS = (
    "<script>alert(1)</script>",
    "\"><img src=x onerror=alert(1)>",
    "';alert(1)//",
    "javascript:alert(1)",
    "{{7*7}}${7*7}",
    "<svg/onload=alert(1)>",
    "a\\u2028b</script><script>",
)


def _json_only(res_status, headers, body, what):
    ctype = headers.get("content-type", "")
    assert not HTML.search(ctype), "%s: content type %r" % (what, ctype)
    assert headers.get("x-content-type-options", "").lower() == "nosniff", \
        "%s: nosniff missing (HTTP %d)" % (what, res_status)
    if body:
        assert "json" in ctype or ctype.startswith("text/plain") or ctype.startswith("text/event-stream"), \
            "%s: unexpected content type %r" % (what, ctype)


def test_xss_reflected_in_queries_paths_and_errors(anon, rx_user, web_operator):
    for payload in PAYLOADS:
        q = quote(payload, safe="")
        probes = [
            (anon, "/api/v1/public/lastheard?limit=%s" % q),
            (anon, "/api/v1/public/state?x=%s" % q),
            (anon, "/api/v1/public/%s" % q),
            (anon, "/api/v1/%s" % q),
            (anon, "/%s" % q),
            (rx_user.client, "/api/v1/user/rooms/%s/messages?limit=%s&after=%s" % (api_web.ROOM, q, q)),
            (rx_user.client, "/api/v1/user/rooms/%s/messages" % q),
            (rx_user.client, "/api/v1/user/recordings/%s" % q),
            (web_operator, "/api/v1/operator/accounts/%s" % q),
            (web_operator, "/api/v1/operator/ids/rows?kind=%s&call=%s" % (q, q)),
            (web_operator, "/api/v1/operator/clients?q=%s&sort=%s" % (q, q)),
            (web_operator, "/api/v1/operator/audit?limit=%s" % q),
        ]
        for client, path in probes:
            resp = client.get(path)
            assert resp.status < 500, "%s: HTTP %d" % (path.split("?")[0], resp.status)
            _json_only(resp.status, resp.headers, resp.body, path.split("?")[0])
    # unknown methods and hostile headers
    resp = anon.request("GET", "/api/v1/public/state", headers={"Accept": "<script>", "Referer": PAYLOADS[1]})
    assert resp.status in (200, 406), resp.status
    _json_only(resp.status, resp.headers, resp.body, "hostile Accept")


def test_xss_stored_fields_round_trip(web_operator, web_user, tx_user):
    """Names and message text are stored and echoed as JSON strings: unchanged, or refused with a 4xx."""
    user = web_user("N0XSS", may_tx=False)
    stored = 0
    for payload in PAYLOADS:
        resp = web_operator.request("PATCH", "/api/v1/operator/accounts/%s" % user.callsign, json_body={"name": payload})
        _json_only(resp.status, resp.headers, resp.body, "PATCH name")
        if resp.status in (400, 422):
            continue                                       # refused per the spec (bounded ASCII names)
        assert resp.status == 200, "PATCH account name: HTTP %d %s" % (resp.status, web_operator.scrub(resp.text())[:100])
        revive(user)                                       # "every update revokes logins and media" (web.md)
        for client, path in ((web_operator, "/api/v1/operator/accounts/%s" % user.callsign),
                             (web_operator, "/api/v1/operator/accounts?limit=1000"), (user.client, "/api/v1/user/me")):
            got = client.get(path, expect=200)
            _json_only(got.status, got.headers, got.body, path)
            assert got.headers["content-type"].startswith("application/json")
            blob = got.json()
            text = json.dumps(blob)
            if json.dumps(payload) not in text:
                web.finding("account name %r came back altered from %s: %s" % (payload, path.split("?")[0], text[:120]))
        stored += 1
    web.finding("account name payloads: %d of %d accepted and echoed as JSON strings, the rest refused (4xx)"
                % (stored, len(PAYLOADS))) if stored != len(PAYLOADS) else None
    # an invite for a hostile callsign is refused, and the error body is JSON
    for payload in PAYLOADS[:3]:
        resp = web_operator.request("POST", "/api/v1/operator/invites",
                                    json_body={"callsign": payload, "role": "user", "may_tx": False})
        assert resp.status in (400, 422), "invite for callsign %r: HTTP %d, documented 400/422" % (payload, resp.status)
        _json_only(resp.status, resp.headers, resp.body, "invite")
        resp = web_operator.request("POST", "/api/v1/operator/invites",
                                    json_body={"callsign": "N0XSS9", "role": payload, "may_tx": False})
        assert resp.status in (400, 422), "invite with role %r: HTTP %d" % (payload, resp.status)
    # registration with hostile callsign / name / invite: refused, JSON, nothing echoed as HTML
    anon = web.WebClient(web_operator.base_url)
    anon._ctx = web_operator._ctx
    for payload in PAYLOADS[:3]:
        resp = anon.request("POST", "/api/v1/auth/register",
                            json_body={"callsign": payload, "invite": payload, "password": "x" * 12, "name": payload})
        assert resp.status in (400, 401, 422), "register with %r: HTTP %d" % (payload, resp.status)
        _json_only(resp.status, resp.headers, resp.body, "register")
    # message text: stored as a JSON string, sanitised to the stored form (or refused), never as HTML
    path = "/api/v1/user/rooms/%s/messages" % api_web.ROOM
    for payload in PAYLOADS[:3]:
        time.sleep(5.2)                                    # 1 message per 5 s per account (burst 2)
        resp = tx_user.client.request("POST", path, json_body={"text": payload})
        _json_only(resp.status, resp.headers, resp.body, "message POST")
        assert resp.status in (202, 400, 403, 409, 429, 503), "message POST: HTTP %d" % resp.status
        if resp.status == 202:
            hist = tx_user.client.get(path + "?limit=8", expect=200)
            _json_only(hist.status, hist.headers, hist.body, "message history")
            bodies = [i.get("body") for i in hist.json()["items"]]
            if payload not in bodies:
                web.finding("message text %r: accepted (202) and not in the history unchanged (history bodies: %s); "
                            "sanitising to the stored form is documented (data.md 2.6)" % (payload, bodies[:3]))
        elif resp.status != 202:
            web.finding("message POST with %r from a may_tx account: HTTP %d" % (payload, resp.status))
    for payload in PAYLOADS[:3]:
        resp = tx_user.client.request("POST", path + "/directed", json_body={"text": "x", "destination": payload})
        assert resp.status in (400, 422), "directed message to %r: HTTP %d, documented validation" % (payload, resp.status)
        _json_only(resp.status, resp.headers, resp.body, "directed")
    # the audit tail is JSON too, whatever the earlier requests put into it
    audit = web_operator.get("/api/v1/operator/audit?limit=200", expect=200)
    _json_only(audit.status, audit.headers, audit.body, "audit")
    assert audit.headers["content-type"].startswith("application/json")
    text = audit.text()
    for secret in (user.password, tx_user.password):
        assert secret not in text, "a password in the audit tail"


def test_xss_pages_have_a_restrictive_csp(anon, web_url_admin, web_ca):
    """The static pages (not the API) carry the CSP; script, style and framing are locked down."""
    for client, base, paths in ((anon, "web", ("/", "/ops/", "/shared/core.js")),
                                (admin_mod.AdminClient(web_url_admin, web_ca), "admin", ("/",))):
        for path in paths:
            resp = client.get(path)
            if resp.status in (301, 302, 404):
                web.finding("GET %s on the %s host: HTTP %d (no page to check the CSP of)" % (path, base, resp.status))
                continue
            assert resp.status == 200, "%s %s: HTTP %d" % (base, path, resp.status)
            csp = resp.headers.get("content-security-policy", "")
            assert csp, "%s %s: no Content-Security-Policy header" % (base, path)
            assert "default-src 'self'" in csp, "%s %s: CSP %r" % (base, path, csp)
            assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp, "%s %s: CSP %r" % (base, path, csp)
            assert "frame-ancestors 'none'" in csp, "%s %s: CSP %r" % (base, path, csp)
            assert resp.headers.get("x-content-type-options", "").lower() == "nosniff"
            assert resp.headers.get("referrer-policy"), "%s %s: no Referrer-Policy" % (base, path)
            if resp.headers.get("x-frame-options", "").upper() != "DENY":
                web.finding("%s %s: X-Frame-Options %r (admin.md documents DENY for the admin host)" % (
                    base, path, resp.headers.get("x-frame-options")))


@pytest.mark.slow
def test_public_events_per_ip_cap(web_base_url, web_ca):
    """At most 4 SSE streams per client IP: the 5th is 429 with Retry-After: 5 (web.md)."""
    held, last = [], None
    clients = [web.WebClient(web_base_url, web_ca, timeout=10) for _ in range(6)]
    # earlier cases closed their streams, but the server frees a slot only when it notices (next write):
    # retry until four streams are admitted at once
    start = time.monotonic()
    try:
        while True:
            for cli in clients[:4]:
                cli.pacer.wait("public")
                conn = cli._connect()
                conn.request("GET", "/api/v1/public/events", headers=cli._headers("GET", {"Accept": "text/event-stream"}, False))
                resp = conn.getresponse()
                held.append((conn, resp))
                if resp.status != 200:
                    break
            if [r.status for _c, r in held] == [200] * 4:
                break
            for conn, _resp in held:
                conn.close()
            held.clear()
            assert time.monotonic() - start < 60, "no four public SSE slots free within 60 s"
            time.sleep(4)
        waited = time.monotonic() - start
        if waited > 5:
            web.finding("public SSE slots of closed connections are released late: four streams were admitted "
                        "only %.0f s after the earlier ones were closed (documented: a slot is released when the "
                        "connection ends)" % waited)
        cli = clients[4]
        cli.pacer.wait("public")
        conn = cli._connect()
        conn.request("GET", "/api/v1/public/events", headers=cli._headers("GET", {"Accept": "text/event-stream"}, False))
        last = conn.getresponse()
        held.append((conn, last))
        statuses = [r.status for _c, r in held]
        assert statuses == [200, 200, 200, 200, 429], "streams 1-5: %s, documented 429 for the one over the per-IP cap" % statuses
        if last.getheader("Retry-After") != "5":
            web.finding("5th public SSE stream: 429 with Retry-After %r, documented 5" % last.getheader("Retry-After"))
    finally:
        for conn, _resp in held:
            conn.close()
    time.sleep(1.5)                                          # let the server release the slots


# ==================================================================== (h) admin API

ADMIN_MATRIX = [(op, role) for op in api_admin.ADMIN_OPS for role in api_admin.ROLES]


@pytest.fixture(scope="module")
def admin_anon(web_url_admin, web_ca):
    return admin_mod.AdminClient(web_url_admin, web_ca)


@pytest.mark.parametrize("op,role", ADMIN_MATRIX, ids=["%s | %s" % (op_id(op), role) for op, role in ADMIN_MATRIX])
def test_admin_minimum_role(op, role, admin_anon, admin_session):
    expect = api_probe.expectation(op, role, api_admin.ROLES)
    client = admin_anon if role == "anonymous" else admin_session(role)
    temp = None
    if op.fresh and expect == "allow" and role != "anonymous":
        temp = client.clone()
        temp.authenticator = client.authenticator
        user, password = admin_session.credentials(role)
        temp.hide(password)
        temp.login_totp(user, password)
        client = temp
    res = api_probe.send(client, op)
    ok, why = api_probe.judge(op, expect, res)
    record("admin", op_id(op), role, res, "pass" if ok else "fail", why)
    assert ok, "%s as %s: %s (expected class %s)" % (op_id(op), role, why, expect)
    success_check("admin", op, role, res)


def test_admin_roles_from_the_login(admin_session):
    for role in ("viewer", "operator", "admin"):
        ident = admin_session(role).session()
        assert ident.get("role") == role or ident.get("user"), "%s: %s" % (role, sorted(ident))
        if ident.get("role") != role:
            web.finding("GET /api/v1/admin/session as %s: role field %r (keys %s)" % (role, ident.get("role"), sorted(ident)))
        assert ident.get("totp") == "ok", "after enrolment totp is ok, got %r" % (ident.get("totp"),)
        assert ident.get("csrf")


def test_admin_root_probes(admin_anon, web_url_admin, web_ca):
    pref = admin_mod.AdminClient(web_url_admin, web_ca, mount="prefix")
    for client, root in ((admin_anon, ""), (pref, "/admin")):
        health = client.get(root + "/health")
        assert health.status == 200, "%s/health: HTTP %d" % (root, health.status)
        ready = client.get(root + "/ready")
        assert ready.status in (200, 503), "%s/ready: HTTP %d" % (root, ready.status)
        body = ready.json()
        assert ("ready" in body) and (ready.status == 200) == (body["ready"] is True), body
        if ready.status == 503:
            assert isinstance(body.get("reasons"), list)
        assert "set-cookie" not in ready.headers and "no-store" in ready.headers.get("cache-control", "")
        head = client.request("HEAD", root + "/ready")
        assert head.status == ready.status and head.body == b""
        post = client.request("POST", root + "/ready", json_body={})
        assert post.status == 405, "POST %s/ready: HTTP %d, documented 405" % (root, post.status)
    # same probes on the API form
    api = admin_anon.get(admin_anon.path("/ready"))
    assert api.status in (200, 503) and api.json().get("ready") in (True, False)


def test_admin_wrong_origin_and_host_are_404(admin_anon, web_url_admin, web_ca):
    for path in ("/health", "/ready", "/api/v1/admin/health", "/api/v1/admin/version"):
        resp = admin_anon.get(path, headers={"Origin": "https://evil.example"})
        assert resp.status == 404, "%s with a wrong Origin: HTTP %d, documented 404 (fail closed)" % (path, resp.status)
        resp = admin_anon.get(path, headers={"Origin": "https://dvt-admin"})          # right name, missing port
        if resp.status != 404:
            web.finding("%s with Origin https://dvt-admin (host without the :8443 port): HTTP %d; documented: "
                        "Origin must equal https://<host>" % (path, resp.status))
    resp = admin_anon.request("POST", "/api/v1/admin/session", json_body={"user": "x", "password": "y"},
                              headers={"Origin": "https://evil.example"})
    assert resp.status == 404, "login with a wrong Origin: HTTP %d, documented 404" % resp.status
    # a host that is not the admin host and not the prefix path: nothing is served
    other = admin_mod.AdminClient(web_url_admin, web_ca, mount="prefix", prefix_host="other.example")
    for path in ("/api/v1/admin/health", "/health", "/ready", "/", "/api/v1/admin/status"):
        resp = other.get(path)
        assert resp.status == 404, "unknown host %s: HTTP %d, documented 404" % (path, resp.status)
    # the prefix mount: wrong Origin for its host is 404 too
    pref = admin_mod.AdminClient(web_url_admin, web_ca, mount="prefix")
    ok = pref.get("/admin/api/v1/admin/health", expect=200)
    assert ok.status == 200
    bad = pref.get("/admin/api/v1/admin/health", headers={"Origin": "https://evil.example"})
    assert bad.status == 404, "prefix mount, wrong Origin: HTTP %d" % bad.status
    bad = pref.get("/admin/api/v1/admin/health", headers={"Origin": "https://dvt-admin:8443"})
    assert bad.status == 404, "prefix mount, the admin host's Origin: HTTP %d, documented https://<that host>" % bad.status


def test_admin_cookie_policy_and_headers(admin_session, web_url_admin, web_ca):
    host = admin_session("viewer")
    attrs = host.cookie_attrs.get(admin_mod.COOKIE_HOST)
    assert attrs is not None, "no %s cookie (jar: %s)" % (admin_mod.COOKIE_HOST, sorted(host.cookie_attrs))
    assert attrs.get("secure") is True and attrs.get("httponly") is True
    assert str(attrs.get("samesite", "")).lower() == "strict" and attrs.get("path") == "/" and "domain" not in attrs
    pref = admin_session("viewer", "prefix")
    attrs = pref.cookie_attrs.get(admin_mod.COOKIE_PREFIX)
    assert attrs is not None, "no %s cookie (jar: %s)" % (admin_mod.COOKIE_PREFIX, sorted(pref.cookie_attrs))
    assert attrs.get("secure") is True and attrs.get("httponly") is True
    assert str(attrs.get("samesite", "")).lower() == "strict"
    assert attrs.get("path") == "/admin/", "prefix cookie Path is %r, documented /admin/" % attrs.get("path")
    assert "domain" not in attrs
    # security headers of an API answer and of a page
    resp = host.get(host.path("/status"), expect=200)
    want = {"referrer-policy": "no-referrer", "cross-origin-opener-policy": "same-origin",
            "x-frame-options": "DENY", "x-content-type-options": "nosniff"}
    for name, value in want.items():
        assert resp.headers.get(name, "").lower() == value.lower(), "%s is %r, documented %r" % (
            name, resp.headers.get(name), value)
    assert "no-store" in resp.headers.get("cache-control", "")
    assert "microphone=()" in resp.headers.get("permissions-policy", ""), resp.headers.get("permissions-policy")
    # the legacy direct mount (cookie dv_admin, Path=/api) is loopback only: the tester cannot reach it
    web.finding("legacy direct mount (cookie dv_admin, Path=/api): not exercised, it needs no [admin] host and a "
                "loopback bind; the rig serves the admin host mount behind the TLS proxy")


def test_admin_login_rules(admin_session, web_url_admin, web_ca):
    user, password = admin_session.credentials("viewer")
    admin_session("viewer")                              # enrolled by the fixture
    cli = admin_mod.AdminClient(web_url_admin, web_ca)
    cli.hide(password)
    resp = cli.request("POST", cli.path("/session"), json_body={"user": user, "password": password})
    assert resp.status == 401, "enrolled account without a TOTP code: HTTP %d, documented 401 totp_required" % resp.status
    code = api_probe.Result(resp.status, resp.headers, resp.body).code()
    if code != "totp_required":
        web.finding("admin login without the TOTP code: code %r, documented `totp_required`" % code)
    unknown = cli.request("POST", cli.path("/session"), json_body={"user": "nobody-dvt", "password": "x" * 12})
    assert unknown.status == 401 and "set-cookie" not in unknown.headers
    # a web account is not an admin account (separate stores)
    assert cli.request("POST", cli.path("/session"), json_body={"user": "N0CALL", "password": "x" * 12}).status == 401
    # real login and logout of a throwaway session: the declared success statuses (API4: login 200, logout 204)
    base = admin_session("viewer")
    temp = base.clone()
    temp.authenticator = base.authenticator
    temp.hide(password)
    code = temp.authenticator.next_code()
    temp.hide(code)
    resp = temp.request("POST", temp.path("/session"), json_body={"user": user, "password": password, "totp": code})
    assert resp.status == declared("POST", "/api/v1/admin/session"), "admin login: HTTP %d" % resp.status
    temp.csrf = resp.json().get("csrf")
    temp.hide(temp.csrf)
    out = temp.request("DELETE", temp.path("/session"))
    assert out.status == declared("DELETE", "/api/v1/admin/session"), "admin logout: HTTP %d, declared %d" % (
        out.status, declared("DELETE", "/api/v1/admin/session"))
    assert temp.request("GET", temp.path("/session")).status == 401, "the session ends with the logout"


# ============================================================== (f) limits (LAST)

class Sut:
    """Restart of dv-svc-web through the rig control channel and the wait for it."""

    def __init__(self, control, url, ca):
        self.control, self.url, self.ca = control, url, ca

    def restart_web(self, budget=150.0, rtc_user=None):
        """Kill dv-svc-web (the launcher respawns it) and wait until it answers; with `rtc_user` (a WebUser that
        can log in again) also until RTC offers stop answering 503 (about 36 s after a web restart)."""
        self.control.kill_proc("xlxd-sut", "dv-svc-web", "KILL")
        # the launcher respawns it within about half a second: give the old process time to go, then wait
        # for two answers in a row from the new one
        probe = web.WebClient(self.url, self.ca, timeout=4)
        time.sleep(2.0)
        deadline = time.monotonic() + budget
        good = 0
        while good < 2:
            if time.monotonic() > deadline:
                pytest.fail("dv-svc-web did not come back within %d s" % budget)
            try:
                good = good + 1 if probe.get("/api/v1/public/state", pace=False).status == 200 else 0
            except web.WebError:
                good = 0
            time.sleep(0.5)
        time.sleep(1.0)
        if rtc_user is None:
            return
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                revive(rtc_user)
                res = rtc_user.client.request("POST", "/api/v1/rtc/offer", json_body=api_web.WRONG_JSON)
                if res.status != 503:
                    return
            except web.WebError:
                pass
            time.sleep(3)
        pytest.fail("RTC still answers 503 %d s after the web restart" % budget)


LOGIN1_OPERATORS = ("N0LKA", "N0LKB", "N0LKC")      # throwaway enrolled operators, one per LOGIN1 case


@pytest.fixture(scope="module")
def limits_env(rig_control, web_base_url, web_ca, web_user, web_operator, web_operator2):
    """The throwaway accounts first (registration needs the operator's session; the register case below
    drains the register bucket): the user N0LCK and three enrolled operators for the LOGIN1 cases, so no
    lockout ever hits N0CALL / N0OP. Then a clean SUT; the SUT is restarted again after the module's last
    limits case. Accounts and TOTP secrets survive the restart (state dir), sessions do not."""
    acct = web_user("N0LCK", may_tx=False)
    operators = {cs: make_operator(web_operator, web_base_url, web_ca, cs, may_tx=False) for cs in LOGIN1_OPERATORS}
    sut = Sut(rig_control, web_base_url, web_ca)
    sut.restart_web()
    yield {"sut": sut, "account": acct, "operators": operators}
    # restore the SUT: a new process has empty buckets and no lockouts; wait until RTC answers again
    sut.restart_web(rtc_user=web_operator2)


def _burst(url, ca, make_body, n, workers=1, pace=0.0):
    """Send n POST /auth/session (or register) requests with no pacing; returns [(status, retry_after)]."""
    results, lock = [], threading.Lock()

    def one(i):
        cli = web.WebClient(url, ca, timeout=15)
        method, path, body = make_body(i)
        resp = cli.request(method, path, json_body=body, pace=False)
        with lock:
            results.append((i, resp.status, resp.headers.get("retry-after"),
                            api_probe.Result(resp.status, resp.headers, resp.body).code(), resp))

    if workers == 1:
        for i in range(n):
            one(i)
            if pace:
                time.sleep(pace)
    else:
        threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    results.sort(key=lambda r: r[0])
    return results


@pytest.mark.slow
def test_limits_register_budget(limits_env, web_base_url, web_ca):
    """Register: 0.2/s burst 2 per client prefix. Concurrent invalid redemptions: the first two reach the
    invite check (401), the rest are 429 with Retry-After."""
    res = _burst(web_base_url, web_ca, lambda i: ("POST", "/api/v1/auth/register",
                 {"callsign": "N0REG%d" % i, "invite": "x" * 20, "password": "y" * 12}), 6, workers=6)
    statuses = [r[1] for r in res]
    assert set(statuses) <= {401, 429, 400, 422, 503}, statuses
    assert 429 in statuses, "six simultaneous registrations: %s, documented burst 2 then 429" % statuses
    reached = [s for s in statuses if s != 429]
    if len(reached) != 2:
        web.finding("register burst: %d of 6 simultaneous requests passed the bucket (%s); documented burst 2"
                    % (len(reached), statuses))
    for _i, st, ra, code, resp in res:
        if st == 429:
            assert ra and ra.isdigit() and int(ra) >= 1, "429 without a whole-second Retry-After: %r" % ra
            assert code == "rate_limited", "429 code %r, documented rate_limited" % code
            body = resp.json()
            assert body.get("retry_after") == int(ra), "retry_after %r vs Retry-After %r" % (body.get("retry_after"), ra)
            headers_ok(api_probe.Result(resp.status, resp.headers, resp.body), "429")
    record("limits", "register burst", "anonymous", None, "pass", "statuses %s Retry-After %s" % (
        statuses, sorted({r[2] for r in res if r[1] == 429})))
    time.sleep(6)                                             # ~1.2 tokens back at 0.2/s


LOCK_S = 900                                      # first lock: 15 minutes (web.md, doubling later)


def assert_locked(resp, what):
    """Documented by API4 (web.md Account API, LOGIN1 table): a locked (callsign, prefix) pair is 429
    `rate_limited`, even with the right credentials; Retry-After is the REMAINING lock in whole seconds.
    A fresh first lock has close to 900 s left; more than 60 s tells it from the 1 s of the login bucket."""
    res = api_probe.Result(resp.status, resp.headers, resp.body)
    assert resp.status == 429, "%s: %s, documented 429 rate_limited for a locked pair" % (what, res.brief())
    assert res.code() == "rate_limited", "%s: code %r, documented rate_limited" % (what, res.code())
    ra = res.retry_after()
    assert ra is not None and 60 < ra <= LOCK_S, \
        "%s: Retry-After %r, documented: the remaining lock (a fresh first lock: up to %d s)" % (what, ra, LOCK_S)
    body_ra = resp.json().get("retry_after")
    assert body_ra in (None, ra), "retry_after %r vs Retry-After %r" % (body_ra, ra)
    assert "set-cookie" not in resp.headers, "%s: a cookie on a refused login" % what
    return ra


@pytest.mark.slow
def test_limits_login_lockout(limits_env, web_base_url, web_ca):
    """5 failures per (account, client prefix) lock the account, even for the right password."""
    acct = limits_env["account"]
    cli = web.WebClient(web_base_url, web_ca, timeout=15)
    cli.hide(acct.password)
    statuses = []
    for _ in range(5):
        resp = cli.request("POST", "/api/v1/auth/session", json_body={"callsign": acct.callsign, "password": "wrong-" + acct.password},
                           pace=False)
        statuses.append(resp.status)
        time.sleep(1.1)                                       # stay inside the 1/s login bucket
    assert statuses == [401] * 5, "five wrong passwords: %s, documented 401 each" % statuses
    locked = cli.request("POST", "/api/v1/auth/session", json_body={"callsign": acct.callsign, "password": acct.password},
                         pace=False)
    res = api_probe.Result(locked.status, locked.headers, locked.body)
    assert locked.status != 200, "the right password right after 5 failures must not log in"
    assert "set-cookie" not in locked.headers
    assert_locked(locked, "the right password after 5 wrong ones")
    record("limits", "login lockout", "anonymous", res, "pass", "5x401 then %d code %s Retry-After %s" % (
        locked.status, res.code(), locked.headers.get("retry-after")))
    # another account of the same client prefix is not locked
    other = web.WebClient(web_base_url, web_ca, timeout=15)
    resp = other.request("POST", "/api/v1/auth/session", json_body={"callsign": "N0NOBODY", "password": "x" * 12}, pace=False)
    assert resp.status == 401, "an unknown account is a plain 401, got %d" % resp.status


def _login_client(url, ca, op):
    cli = web.WebClient(url, ca, timeout=15)
    cli.hide(op.password)
    return cli


def _login(cli, callsign, password, code=None):
    """One login attempt, then 1.1 s of rest: the attempts stay inside the 1/s login bucket of the client
    prefix, so a 429 here is never the bucket's (that one is test_limits_login_burst)."""
    body = {"callsign": callsign, "password": password}
    if code is not None:
        cli.hide(code)
        body["totp"] = code
    resp = cli.request("POST", "/api/v1/auth/session", json_body=body, pace=False)
    time.sleep(1.1)
    return resp, api_probe.Result(resp.status, resp.headers, resp.body)


def _logout(cli, login):
    cli.csrf = login.json().get("csrf")
    cli.hide(cli.csrf)
    out = cli.request("DELETE", "/api/v1/auth/session")
    assert out.status == declared("DELETE", "/api/v1/auth/session"), "logout: HTTP %d" % out.status


@pytest.mark.slow
def test_limits_login_missing_code_is_no_failure(limits_env, web_base_url, web_ca):
    """LOGIN1 (web.md "Operator login and failure counting"): the right password without a code on an enrolled
    operator is the 401 `totp_required` challenge, no cookie; it neither adds nor clears lockout failures. Five
    of them (the lockout threshold) leave the pair unlocked: password + fresh code still logs in (200)."""
    op = limits_env["operators"]["N0LKA"]
    cli = _login_client(web_base_url, web_ca, op)
    for i in range(5):
        resp, res = _login(cli, op.callsign, op.password)
        assert (resp.status, res.code()) == (401, "totp_required"), \
            "password without a code, attempt %d: %s, documented 401 totp_required" % (i + 1, res.brief())
        assert "set-cookie" not in resp.headers, "the challenge issues no cookie or session"
    resp, res = _login(cli, op.callsign, op.password, op.client.authenticator.next_code())
    assert resp.status == declared("POST", "/api/v1/auth/session"), \
        "password + fresh code after 5 challenges: %s; documented 200 (a challenge is no lockout failure)" % res.brief()
    assert cli.cookies.get(web.COOKIE_NAME), "a complete login sets the session cookie"
    record("limits", "LOGIN1 challenges", "operator", res, "pass", "5x401 totp_required then %d" % resp.status)
    _logout(cli, resp)


@pytest.mark.slow
def test_limits_login_wrong_codes_lock(limits_env, web_base_url, web_ca):
    """LOGIN1: the right password with a WRONG code is a guess (401 `unauthorized`, one failure each); after
    five the pair is locked: the right code, and a password-only attempt, get 429 rate_limited with the
    remaining lock in Retry-After (API4)."""
    op = limits_env["operators"]["N0LKB"]
    cli = _login_client(web_base_url, web_ca, op)
    for i in range(5):
        resp, res = _login(cli, op.callsign, op.password, op.client.authenticator.wrong_code())
        assert (resp.status, res.code()) == (401, "unauthorized"), \
            "right password, wrong code, attempt %d: %s, documented 401 unauthorized" % (i + 1, res.brief())
        assert "set-cookie" not in resp.headers
    resp, res = _login(cli, op.callsign, op.password, op.client.authenticator.next_code())
    ra = assert_locked(resp, "the right code after 5 wrong codes")
    resp, _res = _login(cli, op.callsign, op.password)
    assert_locked(resp, "a password-only attempt of the locked pair")
    record("limits", "LOGIN1 wrong codes", "operator", res, "pass", "5x401 unauthorized then 429 Retry-After %d" % ra)


@pytest.mark.slow
def test_limits_login_wrong_passwords_count_challenges_do_not(limits_env, web_base_url, web_ca):
    """LOGIN1: wrong passwords count as before (one failure each) and a challenge in between preserves them:
    3 wrong passwords, 2 challenges, 2 wrong passwords are five counted failures; the fifth still answers
    401, the next attempt (right password and code) is 429. Had the challenges counted, the sixth request
    would already be 429."""
    op = limits_env["operators"]["N0LKC"]
    cli = _login_client(web_base_url, web_ca, op)
    plan = ("wrong",) * 3 + ("challenge",) * 2 + ("wrong",) * 2
    got = []
    for step in plan:
        if step == "wrong":
            resp, res = _login(cli, op.callsign, "wrong-" + op.password)
            want = (401, "unauthorized")
        else:
            resp, res = _login(cli, op.callsign, op.password)
            want = (401, "totp_required")
        got.append("%s:%s" % (step, res.brief()))
        assert (resp.status, res.code()) == want, "step %d (%s): %s, documented %d %s; so far %s" % (
            len(got), step, res.brief(), want[0], want[1], got)
    resp, res = _login(cli, op.callsign, op.password, op.client.authenticator.next_code())
    ra = assert_locked(resp, "the right password and code after 5 counted failures (3 + 2 wrong passwords)")
    record("limits", "LOGIN1 mixed", "operator", res, "pass", "%s then 429 Retry-After %d" % (" ".join(got), ra))


@pytest.mark.slow
def test_limits_login_burst(limits_env, web_base_url, web_ca):
    """Login: 1/s burst 10 per client prefix. Failures of distinct unknown callsigns (no lockout involved)
    pass the bucket up to the burst, then 429 with Retry-After."""
    time.sleep(10)                                            # the lockout case used some of the bucket
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    res = _burst(web_base_url, web_ca, lambda i: ("POST", "/api/v1/auth/session",
                 {"callsign": "N0%s%s" % (letters[i % 24], letters[(i // 24) % 24]), "password": "z" * 12}), 16)
    statuses = [r[1] for r in res]
    assert 429 in statuses, "16 quick logins: %s, documented burst 10 then 429" % statuses
    first429 = statuses.index(429)
    if not 8 <= first429 <= 13:
        web.finding("login burst: first 429 at request %d (documented burst 10 per client prefix, 1/s refill); "
                    "statuses %s" % (first429 + 1, statuses))
    assert all(s == 401 for s in statuses[:first429]), statuses
    for _i, st, ra, code, resp in res:
        if st == 429:
            assert ra and ra.isdigit() and int(ra) >= 1, "login 429 without Retry-After: %r" % ra
            assert code == "rate_limited", code
    record("limits", "login burst", "anonymous", None, "pass", "first 429 at request %d, Retry-After %s" % (
        first429 + 1, sorted({r[2] for r in res if r[1] == 429})))
