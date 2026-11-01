# SPDX-License-Identifier: MIT
"""Pure helpers of the browser cases (dvtest/browser.py): labels, payloads, the page log
verdicts, CSP and cookie judgements, artifact paths."""
import os

import pytest

from dvtest import browser as b

pytestmark = pytest.mark.unit

ORIGIN = "https://dvt-web"


def log_with(*requests, console=(), errors=(), dialogs=(), csp=(), reports=(), failed=()):
    log = b.PageLog(origin=ORIGIN)
    for method, url, status in requests:
        b.record_request(log, method, url, status=status)
    log.console.extend(console)
    log.errors.extend(errors)
    log.dialogs.extend(dialogs)
    log.csp.extend(csp)
    log.reports.extend(reports)
    log.failed.extend(failed)
    return log


def test_webkit_is_never_called_safari():
    assert b.label("webkit") == "Playwright WebKit"
    assert b.label("chromium") == "Playwright Chromium" and b.label("firefox") == "Playwright Firefox"
    assert all("safari" not in v.lower() for v in b.ENGINE_LABEL.values())
    assert set(b.ENGINES) == set(b.ENGINE_LABEL)


def test_payloads_are_printable_ascii_and_fit_a_name_and_a_message():
    assert len(b.XSS_PAYLOADS) >= 8
    for p in b.XSS_PAYLOADS:
        assert b.printable_ascii(p), p
        assert len(p) <= 128, p                  # a display name is at most 128 bytes
        assert "__pwn" in p                      # every one reaches for the marker
    assert len(set(b.XSS_PAYLOADS)) == len(b.XSS_PAYLOADS)
    kinds = " ".join(b.XSS_PAYLOADS)
    for needle in ("<script>", "onerror", "onload", "javascript:", "${", "{{", "`", "' onfocus='"):
        assert needle in kinds, needle


def test_clean_page_has_no_problems():
    log = log_with(("GET", ORIGIN + "/", 200), ("GET", ORIGIN + "/api/v1/public/state?x=1", 200),
                   ("GET", "https://elsewhere.invalid/x", 500))        # not the page's origin: not judged
    log.console.append({"type": "log", "text": "hello", "url": ""})
    assert log.problems() == []


def test_problems_lists_errors_failures_dialogs_csp_and_payloads():
    log = log_with(("GET", ORIGIN + "/api/v1/public/state", 500), ("GET", ORIGIN + "/api/v1/user/x", 404),
                   console=[{"type": "error", "text": "boom", "url": ""}, {"type": "warning", "text": "meh", "url": ""}],
                   errors=["TypeError: x"], dialogs=[{"type": "alert", "message": "1"}],
                   csp=[{"directive": "script-src-elem", "blocked": "inline"}],
                   reports=[("xss", "p1")], failed=[{"method": "GET", "path": "/a", "why": "net::ERR"}])
    out = log.problems()
    joined = "\n".join(out)
    for needle in ("console error: boom", "page error: TypeError", "GET /api/v1/public/state -> HTTP 500",
                   "GET /api/v1/user/x -> HTTP 404", "request failed: GET /a", "dialog opened: alert",
                   "CSP violation: script-src-elem", "script of a payload ran: p1"):
        assert needle in joined, (needle, out)
    assert "meh" not in joined                       # a warning is not an error


def test_expected_answers_and_their_console_line_are_tolerated_only_for_that_url_and_status():
    line = "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
    session = ORIGIN + "/api/v1/auth/session"
    log = log_with(("GET", session, 401), console=[{"type": "error", "text": line, "url": session}])
    assert log.problems() != []
    assert log.problems({("/api/v1/auth/session", 401)}) == []
    # another status, another path or a console line without the URL stays a problem
    other = log_with(("GET", ORIGIN + "/api/v1/user/me", 401),
                     console=[{"type": "error", "text": line, "url": ORIGIN + "/api/v1/user/me"}])
    assert len(other.problems({("/api/v1/auth/session", 401)})) == 2
    log2 = log_with(("GET", session, 403), console=[{"type": "error", "text": line.replace("401", "403"), "url": session}])
    assert len(log2.problems({("/api/v1/auth/session", 401)})) == 2


def test_paths_and_statuses_drop_the_query():
    log = log_with(("GET", ORIGIN + "/api/v1/user/rooms/A/messages?limit=5", 200),
                   ("POST", ORIGIN + "/api/v1/user/rooms/A/messages", 202), ("GET", ORIGIN + "/", 200))
    assert log.paths("/api/v1/user/") == ["/api/v1/user/rooms/A/messages"] * 2
    assert log.paths("/api/v1/user/", method="POST") == ["/api/v1/user/rooms/A/messages"]
    assert log.paths("/api/v1/operator/") == []
    assert log.statuses("/api/v1/user/rooms/A/messages") == [200, 202]


def test_same_origin_is_exact():
    log = b.PageLog(origin=ORIGIN)
    assert log.same_origin(ORIGIN + "/x") and log.same_origin(ORIGIN)
    assert not log.same_origin("https://dvt-web.evil.invalid/x")
    assert not log.same_origin("http://dvt-web/x")


def test_parse_csp_and_judgement():
    csp = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    assert b.parse_csp(csp)["default-src"] == ["'self'"]
    assert b.parse_csp(csp)["frame-ancestors"] == ["'none'"]
    assert b.csp_problems(csp) == []
    assert b.csp_problems(None) == ["no Content-Security-Policy header"]
    assert any("unsafe-inline" in p for p in b.csp_problems("default-src 'self' 'unsafe-inline'; frame-ancestors 'none'"))
    assert any("unsafe-eval" in p for p in b.csp_problems("script-src 'self' 'unsafe-eval'; frame-ancestors 'none'"))
    assert any("frame-ancestors" in p for p in b.csp_problems("default-src 'self'"))
    assert any("script source" in p for p in b.csp_problems("script-src *; default-src 'self'; frame-ancestors 'none'"))
    assert any("neither" in p for p in b.csp_problems("frame-ancestors 'none'"))
    assert b.csp_problems("DEFAULT-SRC 'self'; Frame-Ancestors 'none'") == []      # names are case-insensitive


def test_cookie_judgement():
    good = {"name": "__Host-dv_web", "secure": True, "httpOnly": True, "sameSite": "Strict", "path": "/",
            "domain": "dvt-web"}
    assert b.cookie_problems(good) == []
    for key, bad, word in (("secure", False, "Secure"), ("httpOnly", False, "HttpOnly"),
                           ("sameSite", "Lax", "SameSite"), ("path", "/api", "Path"),
                           ("domain", ".dvt-web", "Domain"), ("name", "dv_web", "name")):
        problems = b.cookie_problems(dict(good, **{key: bad}))
        assert any(word in p for p in problems), (key, problems)


def test_artifact_paths_are_flat_and_safe(tmp_path):
    assert b.safe_name("test_x[firefox]/../../etc") == "test_x_firefox_.._.._etc"
    assert b.safe_name("") == "case"
    path = b.artifact_dir(str(tmp_path), "webkit", "test_dashboard_loads_clean[webkit]")
    assert path.startswith(os.path.join(str(tmp_path), "web", "browser"))
    assert os.path.basename(path) == "webkit-test_dashboard_loads_clean_webkit"
    assert os.path.dirname(path) == os.path.join(str(tmp_path), "web", "browser")


def test_stream_slot_gate_waits_for_stale_streams_only(monkeypatch):
    import browser_fixtures as bf
    clock = {"t": 1000.0}
    monkeypatch.setattr(bf.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(bf.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(bf, "_closed_streams", [])
    assert bf.wait_for_stream_slots() == 0.0                       # nothing closed yet
    bf._closed_streams.append((clock["t"], 2))                     # two streams closed just now
    assert bf.wait_for_stream_slots(need=2) == 0.0                 # 2 stale + 2 needed fits the cap of 4
    bf._closed_streams.append((clock["t"], 1))
    waited = bf.wait_for_stream_slots(need=2)                      # 3 stale: wait until they are older than 17 s
    assert 16.0 <= waited <= 18.0, waited
    assert bf.wait_for_stream_slots(need=2) == 0.0


class _Resp:
    def __init__(self, status, body=None):
        self.status, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _Probe:
    """A client whose GET /ready answers come from a list (the last one repeats)."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, path, pace=True):
        self.calls.append((path, pace))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _clock():
    t = {"now": 0.0}
    return (lambda: t["now"]), (lambda s: t.__setitem__("now", t["now"] + s))


def test_poll_ready_waits_through_503_then_ready():
    import browser_fixtures as bf
    clock, sleep = _clock()
    probe = _Probe([_Resp(503, {"ready": False, "reasons": ["rtc_recovery"]})] * 3 + [_Resp(200, {"ready": True})])
    assert bf.poll_ready(probe, wait_s=60, sleep=sleep, clock=clock) == ("ready", 3.0)
    assert probe.calls[0] == ("/ready", False)                    # unpaced: one per second, under the public bucket


def test_poll_ready_absent_and_never_ready():
    import browser_fixtures as bf
    clock, sleep = _clock()
    assert bf.poll_ready(_Probe([_Resp(404)]), sleep=sleep, clock=clock) == ("absent", 404)
    clock, sleep = _clock()
    verdict, last = bf.poll_ready(_Probe([_Resp(503, {"ready": False, "reasons": ["hub"]})]), wait_s=5, sleep=sleep, clock=clock)
    assert verdict == "not ready" and "503" in last and "hub" in last


def test_page_hooks_follow_the_web2_naming_scheme():
    """web/README.md "Stable browser rig hooks": lowercase kebab-case, the fixed data-state vocabulary, the
    route and page names appended to the navigation hooks."""
    import re

    import web_pages as wp
    kebab = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
    for key, tid in wp.TESTIDS.items():
        assert kebab.match(tid), (key, tid)
        assert tid.replace("-", "_") == key, "the key of %r is its name with underscores" % tid
    assert wp.CONNECTION_STATES == ("idle", "connecting", "connected", "failed", "closed")
    assert wp.dashboard_nav_testid("/") == "dashboard-nav-overview"
    assert [wp.dashboard_nav_testid(r) for r in wp.ROUTE_TITLES][1:] == [
        "dashboard-nav-heard", "dashboard-nav-modules", "dashboard-nav-nodes", "dashboard-nav-peers", "dashboard-nav-about"]
    assert [wp.ops_nav_testid(p) for p in wp.OPS_PAGES] == [
        "ops-nav-clients", "ops-nav-events", "ops-nav-accounts", "ops-nav-invites", "ops-nav-audit", "ops-nav-ids"]
