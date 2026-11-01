# SPDX-License-Identifier: MIT
"""Web unit, stage 3a: DV-Router's web UI in real browsers (Playwright Chromium, Firefox and
Playwright WebKit). Needs `scripts/run.sh --profile web,browser` (docs/rig.md "Browser profile",
docs/web-browser-cases.md): the browsers run in the rig service `browser` and load
https://dvt-web through the proxy; every case skips with the reason otherwise
(cases/browser_fixtures.py). WebKit here is Playwright WebKit, the engine of Safari but not Safari.

All page knowledge (paths, roles, texts, ids) is in cases/web_pages.py. Differences from the
documents are recorded with web.finding() (web-findings.txt), never asserted. WebRTC listen and talk
and recorder playback are stage 3b.

  a  test_dashboard_loads_clean                    each route renders, SSE live, no console error, no CSP report
  b  test_signed_out_dashboard_requests            no message-history fetch when signed out, the 401 of the identity probe is silent
  c  test_login_wrong_password_text                documented fixed error text, no cookie, field cleared
  d  test_user_login_cookie_and_logout             cookie flags as the browser sees them, document.cookie, storage, sign out
  e  test_operator_login_with_totp_step            operator: password, then the authenticator code in the form
  f  test_ops_page_renders / ..._matches_the_api   the five operator pages for an operator
  g  test_ops_pages_refuse_user_and_anonymous      role floor in the UI and in the API from the browser
  h  test_xss_*                                    stored and reflected text is shown inert (names, limits, messages, invites, audit, last heard)
  i  test_csp_*                                    CSP header on every page, enforced by each engine
  j  test_admin_spa_loads                          the admin host's sign-in screen
"""
import json
import re
import time

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

import web_pages as pages  # noqa: E402
from dvtest import api_web, web  # noqa: E402
from dvtest import browser as brlib  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

ROOM = api_web.ROOM
AUTH_SESSION = "/api/v1/auth/session"
EXPECT_401 = {(AUTH_SESSION, 401)}          # the identity probe of a signed-out page
SSE_CAP = ("/api/v1/public/events", 429)    # at most 4 public streams per client address (web.md); a closed one is freed late
LIVE_MS = 20000


_SEEN = set()
_NO_LIVE = set()                       # engines whose dashboard never reached Live in this run
SSE_PATHS = ("/api/v1/public/events", "/api/v1/operator/stream")


def finding(engine, text):
    """A difference or an observation of one engine, recorded once per run (web-findings.txt)."""
    key = (engine, text)
    if key not in _SEEN:
        _SEEN.add(key)
        web.finding("%s: %s" % (brlib.label(engine), text))


def report(bp, expected=(), what="page", ignore=None):
    """Record what the page did wrong as FINDINGs (a JS exception, a console error, a failed or error
    request, a CSP report: this is a test of someone else's product, not an assertion about it) and
    return the list. The 429 of the per-address stream cap is its own finding (4 streams per address,
    slots of closed streams are released late: docs/web-api-cases.md); every case of this run shares
    the address of the browser container."""
    problems = bp.problems(set(expected) | {(path, 429) for path in SSE_PATHS})
    if ignore:
        problems = [p for p in problems if not re.search(ignore, p)]
    sse = [p for p in problems if any(path in p for path in SSE_PATHS)]
    other = [p for p in problems if p not in sse]
    codes = [st for path in SSE_PATHS for st in bp.log.statuses(path)]
    if sse or 429 in codes:
        finding(bp.engine, "%s: an event stream was refused (HTTP %s; at most 4 streams per address, a closed "
                           "stream is freed late) and the page went on without it" % (what, sorted(set(codes)) or "no status"))
    for p in other:
        finding(bp.engine, "%s: %s" % (what, p))
    return other


def assert_inert(bp, what="page"):
    """Nothing hostile ran and no hostile markup became a live element. A CSP report for a script is a
    failure too (an injected script reached the DOM and only the policy stopped it); style reports are
    not about the payloads and go to the findings."""
    flag = bp.page.evaluate("typeof window.__pwn")
    assert flag == "undefined", "%s on %s: a payload set window.__pwn (%s)" % (what, brlib.label(bp.engine), flag)
    live = bp.page.locator(brlib.LIVE_MARKUP_SELECTOR).count()
    assert live == 0, "%s on %s: %d live element(s) created from stored text" % (what, brlib.label(bp.engine), live)
    assert not bp.log.dialogs, "%s on %s: dialog %r" % (what, brlib.label(bp.engine), bp.log.dialogs[:1])
    scripts = [v for v in bp.log.csp if str(v.get("directive", "")).startswith(("script-src", "default-src"))]
    assert not scripts, "%s on %s: script blocked by the CSP %r (markup reached the DOM?)" % (what, brlib.label(bp.engine), scripts[:1])
    for v in bp.log.csp:
        if v not in scripts:
            finding(bp.engine, "%s: CSP report %s blocked %s from %s" % (what, v.get("directive"), v.get("blocked"),
                                                                       (v.get("source") or "-").rsplit("/", 1)[-1]))


def wait_live(bp):
    """True when the dashboard shows Live (the SSE stream is connected). When it does not, that is a
    finding of this engine and the case goes on (the page polls)."""
    ms = 3000 if bp.engine in _NO_LIVE else LIVE_MS
    try:
        expect(pages.connection_state(bp.page)).to_contain_text("Live", timeout=ms)
        return True
    except AssertionError:
        state = pages.connection_state(bp.page).inner_text().strip()
        _NO_LIVE.add(bp.engine)
        failed = [f["path"] for f in bp.log.failed if f["path"] in SSE_PATHS]
        try:      # what a bare EventSource to the same URL does in this engine
            probe = bp.page.evaluate("""() => new Promise((res) => {
                const es = new EventSource('/api/v1/public/events?topics=session');
                es.onopen = () => { es.close(); res('open'); };
                es.onerror = () => { res('error, readyState ' + es.readyState); es.close(); };
                setTimeout(() => res('no event in 6 s'), 6000); })""")
        except Exception as exc:
            probe = "probe failed: %s" % str(exc)[:60]
        finding(bp.engine, "the dashboard did not reach Live (shows %r after %d s; event stream requests: %s; failed: %s; "
                           "a bare EventSource to the same URL: %s)" % (
            state, ms // 1000, [(r["status"]) for r in bp.log.requests if r["path"] in SSE_PATHS][:3], failed[:2], probe))
        _NO_LIVE.discard(bp.engine)        # decided per case now: the slots of the cap come and go
        return False


def wait_for_state(bp, seconds=15):
    """The dashboard fetched /api/v1/public/state (it does so on the first stream event or the first poll)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and bp.log.statuses("/api/v1/public/state")[:1] != [200]:
        bp.page.wait_for_timeout(250)
    assert bp.log.statuses("/api/v1/public/state")[:1] == [200], "the dashboard never fetched its state (%s)" % \
        bp.log.statuses("/api/v1/public/state")


def open_dashboard(bp, route="/"):
    resp = bp.goto(pages.dashboard_url(route))
    expect(pages.heading(bp.page)).to_have_text(pages.ROUTE_TITLES[route])
    wait_live(bp)
    return resp


# ========================================================================== a, b: dashboard

def test_dashboard_renders_the_state(browser_page):
    """The public dashboard as served: it shows the room cards of the state it fetched (web.md: the dashboard
    lists rooms, last heard and room messages). No workaround here."""
    bp = browser_page
    resp = open_dashboard(bp)
    assert resp.status == 200
    wait_for_state(bp)
    cards = bp.page.locator("main .card")
    try:
        expect(cards.first).to_be_visible(timeout=8000)
    except AssertionError:
        errors = [e.split(" | ")[0] for e in bp.log.errors]
        body = bp.page.locator("main").inner_text().strip()[:40]
        finding(bp.engine, "the dashboard does not render after its state arrived: main shows %r, page errors %s "
                           "(vendored htm-preact-3.1.1 turns the fragment `<>` of PublicContent into a node of type '' "
                           "and Preact calls document.createElement(''); see docs/web-browser-cases.md)" % (body, errors[:1]))
        raise
    report(bp, EXPECT_401, "public dashboard")


def test_dashboard_routes_with_the_fragment_workaround(dashboard_page):
    """Everything else about the dashboard, with /public.js patched so that it renders (BrowserPage.apply_
    fragment_workaround): routes, live state, no console error."""
    bp = dashboard_page
    resp = open_dashboard(bp)
    assert resp.status == 200
    assert bp.page.title(), "the dashboard has no title"
    wait_for_state(bp)
    expect(pages.dashboard_alerts(bp.page)).to_have_count(0)
    expect(bp.page.locator("main .card").first).to_be_visible()
    assert not bp.log.errors, "page error with the workaround: %s" % bp.log.errors[:1]
    # the other routes, through the page's own navigation (one document: no new event stream)
    for route, title in pages.ROUTE_TITLES.items():
        if route == "/":
            continue
        bp.page.get_by_role("link", name=title, exact=True).first.click()
        expect(pages.heading(bp.page)).to_have_text(title)
    report(bp, EXPECT_401, "public dashboard (workaround)")
    if any("Failed to load resource" in c["text"] and "401" in c["text"] for c in bp.log.console):
        finding(bp.engine, "the signed-out dashboard logs a console error for the 401 of its identity probe "
                           "GET %s (the browser writes it for any non-2xx answer; no document says the probe "
                           "is expected to fail)" % AUTH_SESSION)


def test_signed_out_dashboard_requests(dashboard_page):
    bp = dashboard_page
    live = wait_live_after(bp)
    for room in ("A", "B"):
        expect(pages.room_messages(bp.page, room)).to_be_visible()
        expect(pages.sign_in_for_history_link(bp.page, room)).to_be_visible()
    bp.page.wait_for_timeout(5000)           # past the first refresh and one resync interval
    # web.md "Roles": message history needs a user session. The page must not even ask.
    asked = bp.seen("/api/v1/user/") + bp.seen("/api/v1/operator/") + bp.seen("/api/v1/rtc/")
    assert not asked, "%s: a signed-out dashboard requested %s" % (brlib.label(bp.engine), sorted(set(asked)))
    if live:
        events = [r for r in bp.log.requests if r["path"] == "/api/v1/public/events" and r["status"] == 200]
        assert events and events[0]["ctype"].startswith("text/event-stream"), \
            "Live shown but no public SSE answer: %s" % [r["status"] for r in bp.log.requests if r["path"].endswith("/events")]
    probes = bp.log.statuses(AUTH_SESSION)
    assert probes and set(probes) == {401}, "the identity probe: %s" % probes
    # the expected 401 is not shown to the visitor as a problem
    expect(pages.dashboard_alerts(bp.page)).to_have_count(0)
    text = bp.page.locator("body").inner_text()
    for bad in ("Unauthorized", "Cannot connect", "HTTP 401", "Offline"):
        assert bad not in text, "%s: %r on the signed-out dashboard" % (brlib.label(bp.engine), bad)
    report(bp, EXPECT_401, "signed-out dashboard")


def wait_live_after(bp):
    """Open the dashboard and wait for its first render (the message panels) and for Live."""
    resp = bp.goto(pages.dashboard_url("/"))
    assert resp.status == 200
    expect(pages.heading(bp.page)).to_have_text("Dashboard")
    return wait_live(bp)


# ====================================================================== c, d, e: login and cookie

def ui_sign_in(bp, callsign, password, totp=None):
    web.PACER.wait("login")
    pages.callsign_field(bp.page).fill(callsign)
    pages.password_field(bp.page).fill(password)
    if totp is not None:
        pages.totp_field(bp.page).fill(totp)
    pages.sign_in_button(bp.page).click()


def test_login_wrong_password_text(browser_page, br_user):
    bp = browser_page
    bp.goto(pages.LOGIN)
    expect(pages.heading(bp.page)).to_have_text("Sign in")
    texts = []
    for callsign, password in ((br_user.callsign, br_user.password + "x"), ("N0NOBODY", "not-a-real-password-1")):
        ui_sign_in(bp, callsign, password)
        expect(pages.auth_error(bp.page)).to_be_visible()
        texts.append(pages.auth_error(bp.page).inner_text().strip())
        assert bp.session_cookie() is None, "a cookie after a failed login"
        expect(pages.password_field(bp.page)).to_have_value("")      # "Credentials stay in form controls": cleared
        expect(pages.auth_error(bp.page)).not_to_contain_text(callsign)
        # wait until the form is usable again for the next attempt
        expect(pages.sign_in_button(bp.page)).to_be_enabled()
        bp.page.wait_for_timeout(300)
    # the same words for a known and an unknown callsign (no account enumeration), a fixed text
    assert texts[0] == texts[1] and texts[0], "known vs unknown callsign: %r" % texts
    if texts[0] != pages.LOGIN_FAILED_TEXT:
        finding(bp.engine, "wrong-password text is %r, the UI source says %r" % (texts[0], pages.LOGIN_FAILED_TEXT))
    expect(pages.signed_in_as(bp.page)).to_have_count(0)
    report(bp, {(AUTH_SESSION, 401)}, "login page")


def cookie_checks(bp):
    cookie = bp.session_cookie()
    assert cookie is not None, "no session cookie in %s" % [c["name"] for c in bp.cookies()]
    problems = brlib.cookie_problems(cookie)
    assert not problems, "%s: session cookie %s" % (brlib.label(bp.engine), "; ".join(problems))
    assert cookie["name"] == brlib.SESSION_COOKIE
    return cookie


def test_user_login_cookie_and_logout(browser_page, br_user):
    bp = browser_page
    bp.goto(pages.LOGIN)
    ui_sign_in(bp, br_user.callsign, br_user.password)
    expect(pages.signed_in_as(bp.page)).to_have_text("Signed in as %s." % br_user.callsign)
    expect(pages.operator_pages_link(bp.page)).to_have_count(0)          # a user has no operator pages
    cookie_checks(bp)
    # a script on the page cannot read the session cookie; nothing secret in the storages
    assert "dv_web" not in bp.page.evaluate("document.cookie"), "document.cookie exposes the session cookie"
    stored = bp.page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    assert br_user.password not in stored, "the password is in web storage"
    csrf = bp.page.evaluate("fetch('/api/v1/auth/session').then(r => r.json()).then(j => j.csrf)")
    assert csrf and csrf not in stored, "the CSRF token is in web storage"
    assert br_user.password not in bp.page.url
    pages.sign_out_button(bp.page).click()
    expect(pages.auth_notice(bp.page)).to_have_text(pages.SIGNED_OUT_TEXT)
    assert bp.session_cookie() is None, "the session cookie survives sign-out"
    assert bp.page.evaluate("fetch('/api/v1/auth/session').then(r => r.status)") == 401
    report(bp, EXPECT_401, "user login")


@pytest.fixture
def login_operator(engine, web_operator, web_base_url, web_ca):
    """An operator for this engine's login form. The service locks a (callsign, address) after 5 failures
    for 15 minutes and counts the password-only step of an operator as one, so every engine has its own."""
    from browser_fixtures import _operator_account
    return _operator_account(web_operator, web_base_url, web_ca, {"chromium": "N0BRL1", "firefox": "N0BRL2",
                                                                 "webkit": "N0BRL3"}[engine])


@pytest.mark.slow
def test_operator_login_with_totp_step(browser_page, login_operator):
    bp = browser_page
    op = login_operator
    bp.goto(pages.LOGIN)
    ui_sign_in(bp, op.callsign, op.password)
    # the password alone does not sign an operator in: the form asks for the authenticator code
    expect(pages.totp_field(bp.page)).to_be_visible()
    expect(pages.signed_in_as(bp.page)).to_have_count(0)
    assert bp.session_cookie() is None, "an operator session before the TOTP step"
    web.PACER.wait("login")
    pages.totp_field(bp.page).fill("000000")
    pages.sign_in_button(bp.page).click()
    expect(pages.auth_error(bp.page)).to_be_visible()
    wrong = pages.auth_error(bp.page).inner_text().strip()
    note("%s: wrong TOTP code in the login form shows %r" % (brlib.label(bp.engine), wrong))
    assert bp.session_cookie() is None
    assert op.password not in wrong
    code = op.client.authenticator.next_code()
    op.client.hide(code)
    web.PACER.wait("login")
    expect(pages.sign_in_button(bp.page)).to_be_enabled()
    pages.password_field(bp.page).fill(op.password)           # the form keeps the password for the retry; make sure
    pages.totp_field(bp.page).fill(code)
    pages.sign_in_button(bp.page).click()
    expect(pages.signed_in_as(bp.page)).to_have_text("Signed in as %s." % op.callsign)
    expect(pages.operator_pages_link(bp.page)).to_be_visible()
    cookie_checks(bp)
    assert "dv_web" not in bp.page.evaluate("document.cookie")
    ident = bp.page.evaluate("fetch('/api/v1/auth/session').then(r => r.json())")
    assert ident["role"] == "operator" and ident["totp"] == "ok", {k: ident.get(k) for k in ("role", "totp")}
    pages.sign_out_button(bp.page).click()
    expect(pages.auth_notice(bp.page)).to_have_text(pages.SIGNED_OUT_TEXT)
    assert bp.session_cookie() is None
    report(bp, {(AUTH_SESSION, 401)}, "operator login")


# ================================================================================ f, g: ops pages

@pytest.fixture(scope="module")
def ops_data(web_operator, br_user):
    """Some data for the lists: an open invite (the invites page lists it)."""
    try:
        web_operator.create_invite("N0INV", role="user", may_tx=False)
    except web.WebError as exc:
        web.finding("ops fixture: invite for N0INV: %s" % exc)
    return {"invite": "N0INV"}


def open_ops(bp, page, fresh=True):
    """The operator page `page`; fresh = a new document (a new operator event stream), else the page's own
    navigation link (the same document)."""
    title = pages.OPS_PAGES[page]
    if fresh:
        bp.goto(pages.ops_url(page))
    else:
        pages.ops_header_nav(bp.page).get_by_role("link", name=title, exact=True).click()
    expect(pages.heading(bp.page)).to_have_text("Operator: " + title)
    expect(pages.ops_table(bp.page, title)).to_be_visible()
    expect(bp.page.locator("[aria-busy='true']")).to_have_count(0, timeout=LIVE_MS)
    return title


def test_ops_pages_render(operator_page, ops_data):
    bp = operator_page
    for i, page in enumerate(pages.OPS_PAGES):
        title = open_ops(bp, page, fresh=(i == 0))
        headings = [t.strip() for t in pages.ops_headings(bp.page, title).all_inner_texts()]
        assert headings == pages.OPS_HEADINGS[page], "%s: %s" % (page, headings)
        expect(pages.ops_header_nav(bp.page).get_by_role("link")).to_have_count(len(pages.OPS_PAGES))
        expect(bp.page.locator(".ops-identity")).to_have_text("N0BRO")
        expect(pages.ops_denied(bp.page)).to_have_count(0)
        assert bp.page.title().endswith("DV-Router operator"), bp.page.title()
    bp.page.wait_for_timeout(1500)
    report(bp, set(), "operator pages")


def test_ops_pages_match_the_api(operator_page, web_operator, ops_data):
    bp = operator_page
    # accounts: the callsigns, names and roles on the page are the API's
    api = op_request(web_operator, "GET", "/api/v1/operator/accounts?limit=50").json()
    title = open_ops(bp, "accounts")
    rows = pages.ops_rows(bp.page, title)
    expect(rows).to_have_count(min(len(api["items"]), 50))
    shown = {tuple(c.strip() for c in row.locator("td").all_inner_texts()[:4]) for row in rows.all()}
    want = {(a["callsign"], a["name"], a["role"], a["status"]) for a in api["items"]}
    assert shown == want, "accounts page differs from the API: %s" % sorted(shown ^ want)[:4]
    # invites: the open invite is listed, with no code value anywhere
    title = open_ops(bp, "invites", fresh=False)
    body = pages.ops_table(bp.page, title).inner_text()
    assert ops_data["invite"] in body, body[:200]
    assert not re.search(r"\b[0-9a-f]{64}\b", bp.page.locator("body").inner_text()), "an invite code value on the invites page"
    # audit: the invite we made is in the tail
    title = open_ops(bp, "audit", fresh=False)
    column = pages.ops_column(bp.page, title, 4).all_inner_texts()
    assert any("invite" in c.lower() for c in column), "no invite action in the audit page: %s" % sorted(set(column))[:8]
    open_ops(bp, "clients", fresh=False)
    open_ops(bp, "events", fresh=False)
    # radio IDs (UI1): the rows on the page are the API's rows (the page shows at most 400)
    ids = op_request(web_operator, "GET", "/api/v1/operator/ids/rows")
    title = open_ops(bp, "ids", fresh=False)
    body = ids.json() if ids.status == 200 else {}
    if body.get("ok") is True:
        rows = body.get("rows") or []            # the registry answers without `rows` when it has none
        if 0 < len(rows) <= 400 and not body.get("truncated"):
            expect(pages.ops_rows(bp.page, title)).to_have_count(len(rows))
        elif not rows:
            expect(pages.ops_table(bp.page, title)).to_contain_text("No radio IDs found.")
    else:
        finding(bp.engine, "GET /api/v1/operator/ids/rows -> HTTP %d, body keys %s" % (ids.status, sorted(body)))
    report(bp, set(), "operator pages and the API")


def test_ops_pages_refuse_user_and_anonymous(browser_page, br_user):
    bp = browser_page
    # anonymous
    bp.goto(pages.ops_url("accounts"))
    expect(pages.ops_denied(bp.page)).to_be_visible()
    expect(bp.page.get_by_role("link", name="Sign in")).to_be_visible()
    expect(pages.ops_table(bp.page, "Accounts")).to_have_count(0)
    bp.page.goto(bp.url(pages.ops_url("ids")))
    expect(pages.ops_denied(bp.page)).to_be_visible()
    expect(pages.ops_table(bp.page, "Radio IDs")).to_have_count(0)
    assert not bp.seen("/api/v1/operator/"), "anonymous ops page asked %s" % bp.seen("/api/v1/operator/")
    # the API refuses the same browser (no session)
    assert bp.page.evaluate("fetch('/api/v1/operator/accounts').then(r => r.status)") == 401
    assert bp.page.evaluate("fetch('/api/v1/operator/ids/rows').then(r => r.status)") == 401
    # a signed-in user
    assert bp.api_login(br_user.callsign, br_user.password) == 200
    asked_before = len(bp.seen("/api/v1/operator/"))
    try:
        for page_name, caption in (("audit", "Audit"), ("ids", "Radio IDs")):
            bp.goto(pages.ops_url(page_name))
            expect(pages.ops_denied(bp.page)).to_be_visible()
            expect(pages.ops_table(bp.page, caption)).to_have_count(0)
            bp.page.reload()
            expect(pages.ops_denied(bp.page)).to_be_visible()
        assert len(bp.seen("/api/v1/operator/")) == asked_before, "a user's ops page asked %s" % bp.seen("/api/v1/operator/")[asked_before:]
        for path in ("/api/v1/operator/accounts", "/api/v1/operator/audit", "/api/v1/operator/invites",
                     "/api/v1/operator/ids/rows"):
            got = bp.page.evaluate("p => fetch(p).then(r => r.status)", path)
            assert got == 403, "user in the browser: GET %s -> HTTP %s, documented 403" % (path, got)
        report(bp, {(AUTH_SESSION, 401), ("/api/v1/operator/accounts", 401), ("/api/v1/operator/accounts", 403),
                    ("/api/v1/operator/audit", 403), ("/api/v1/operator/invites", 403),
                    ("/api/v1/operator/ids/rows", 401), ("/api/v1/operator/ids/rows", 403)}, "ops pages for a user")
    finally:
        bp.api_logout()


# ===================================================================== h: stored and reflected text

PAYLOADS = brlib.XSS_PAYLOADS
MSG_PATH = "/api/v1/user/rooms/%s/messages" % ROOM
_last_post = [0.0]


def op_request(op, method, path, body=None):
    """An operator request; a step-up gate (window older than 5 minutes) is answered with a fresh code."""
    resp = op.request(method, path, json_body=body)
    if resp.status == 403 and "step_up" in resp.text():
        resp = op.request(method, path, json_body=body, headers=op.step_up_headers())
    return resp


def post_text(user, text):
    """POST a room message (1 per 5 s per account, burst 2); returns the HTTP status."""
    wait = 5.3 - (time.monotonic() - _last_post[0])
    if wait > 0:
        time.sleep(wait)
    for _ in range(4):
        resp = user.client.request("POST", MSG_PATH, json_body={"text": text})
        _last_post[0] = time.monotonic()
        if resp.status != 429:
            return resp.status
        time.sleep(float(resp.headers.get("retry-after", "5")) + 0.3)
    return 429


def history(user):
    return user.client.get(MSG_PATH + "?limit=64", expect=200).json()["items"]


def note(text):
    """Observations that are not differences from the documents: out/<run>/web/browser/notes.txt."""
    import os
    out = os.environ.get("DVT_OUT")
    if out:
        os.makedirs(os.path.join(out, "web", "browser"), exist_ok=True)
        with open(os.path.join(out, "web", "browser", "notes.txt"), "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


@pytest.fixture(scope="module")
def xss_accounts(web_operator):
    """One account per payload, the payload as its display name (registration, then read back). Returns
    [{callsign, payload, name or None when refused}] where `name` is what the API stores."""
    made = []
    for i, payload in enumerate(PAYLOADS):
        callsign = "N0XB%d" % i
        password = web.new_password()
        web_operator.hide(password)
        try:
            web_operator.register_user(callsign, password, may_tx=False, name=payload)
        except web.WebError as exc:
            made.append({"callsign": callsign, "payload": payload, "name": None, "why": str(exc)[:80]})
            continue
        got = op_request(web_operator, "GET", "/api/v1/operator/accounts/%s" % callsign).json()
        made.append({"callsign": callsign, "payload": payload, "name": got.get("name"), "why": ""})
    return made


@pytest.fixture(scope="module")
def stored_messages(br_user):
    """The payloads as room messages of the may_tx user; returns the bodies the history stores for them."""
    before = {m["id"] for m in history(br_user)}
    results = []
    for payload in PAYLOADS:
        results.append((payload, post_text(br_user, payload)))
    after = history(br_user)
    bodies = [m["body"] for m in after if m["id"] not in before]
    return {"results": results, "bodies": bodies}


def _public_stream_while_posting(user, web_client, text, seconds=10):
    """Frames a tester client reads from the public stream while `text` is posted."""
    import threading
    out = {}

    def read():
        try:
            out["frames"] = web_client().sse("/api/v1/public/events?topics=session,room,status,data",
                                             max_events=50, timeout=seconds)[2]
        except web.WebError as exc:
            out["frames"] = []
            out["error"] = str(exc)

    t = threading.Thread(target=read)
    t.start()
    time.sleep(1.5)
    post_text(user, text)
    t.join(seconds + 3)
    return out.get("frames", [])


def wait_at_least(locator, n, ms=LIVE_MS):
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        if locator.count() >= n:
            return
        time.sleep(0.25)
    assert locator.count() >= n, "only %d of %d expected elements appeared" % (locator.count(), n)


def test_xss_display_names_on_the_accounts_page(operator_page, xss_accounts):
    bp = operator_page
    stored = [a for a in xss_accounts if a["name"] is not None]
    refused = [a for a in xss_accounts if a["name"] is None]
    for a in refused:
        note("display name %r refused at registration (%s)" % (a["payload"], a["why"]))
    if not stored:
        pytest.skip("the API refused every payload as a display name; nothing to render")
    title = open_ops(bp, "accounts")
    names = {c.strip() for c in pages.ops_column(bp.page, title, 1).all_inner_texts()}
    for a in stored:
        # inert: the page shows exactly the stored string as text
        assert a["name"].strip() in names, "%s: name of %s not shown as text: stored %r, page %s" % (
            brlib.label(bp.engine), a["callsign"], a["name"], sorted(names)[:6])
        if a["name"] != a["payload"]:
            note("display name %r is stored as %r" % (a["payload"], a["name"]))
    assert_inert(bp, "accounts page")
    report(bp, set(), "accounts page with hostile names")


def test_xss_account_limits_are_inert(operator_page, web_operator, xss_accounts):
    """The only free-form per-account data besides the name is `limits` (a JSON object shown in the Limits column)."""
    bp = operator_page
    target = xss_accounts[0]["callsign"]
    payload = PAYLOADS[1]
    resp = op_request(web_operator, "PATCH", "/api/v1/operator/accounts/%s" % target,
                      {"limits": {"max_tx": 1, "note": payload}})
    if resp.status != 200:
        note("limits with an extra string key refused: HTTP %d" % resp.status)
        pytest.skip("limits with free text refused by the API (HTTP %d): nothing to render" % resp.status)
    got = op_request(web_operator, "GET", "/api/v1/operator/accounts/%s" % target).json()
    title = open_ops(bp, "accounts")
    cells = [c.strip() for c in pages.ops_column(bp.page, title, 5).all_inner_texts()]
    want = json.dumps(got["limits"], separators=(",", ":"), ensure_ascii=False)
    assert want in cells, "%s: limits of %s: page %s, API %s" % (brlib.label(bp.engine), target, cells[:4], want)
    assert_inert(bp, "accounts page (limits)")
    report(bp, set(), "accounts page with hostile limits")


@pytest.mark.slow
def test_xss_room_message_history_is_inert(user_page, stored_messages):
    bp = user_page
    bodies = stored_messages["bodies"]
    rejected = [p for p, st in stored_messages["results"] if st != 202]
    for p, st in stored_messages["results"]:
        if st != 202:
            note("room message %r refused: HTTP %d" % (p, st))
    if not bodies:
        pytest.skip("no payload was accepted as a room message (statuses %s)" % [s for _p, s in stored_messages["results"]])
    open_dashboard(bp)
    texts = pages.message_texts(bp.page, ROOM)
    wait_at_least(texts, len(bodies))
    shown = [t.strip() for t in texts.all_inner_texts()]
    for body in bodies:
        assert body.strip() in shown, "%s: message %r not shown as text (page: %s)" % (
            brlib.label(bp.engine), body, shown[:5])
    stored_literally = sum(1 for body in bodies if body in PAYLOADS)
    if stored_literally != len(bodies):
        note("room messages: %d of %d stored unchanged" % (stored_literally, len(bodies)))
    assert len(rejected) < len(PAYLOADS)
    assert_inert(bp, "dashboard, message history")
    report(bp, set(), "dashboard with hostile messages")


@pytest.mark.slow
def test_xss_room_message_arrives_live_on_the_public_stream(dashboard_page, br_user, web_client):
    """A signed-out visitor sees room text as it arrives on the public SSE stream (web.md, Roles)."""
    bp = dashboard_page
    open_dashboard(bp)
    if not wait_live(bp):
        pytest.skip("%s: the dashboard never reached Live, so there is no public stream to deliver the message" % brlib.label(bp.engine))
    before = {m["id"] for m in history(br_user)}
    payload = PAYLOADS[1] + " live-" + bp.engine          # a new text: the service refuses a repeated one (409)
    status = post_text(br_user, payload)
    if status != 202:
        pytest.skip("the message was refused (HTTP %d)" % status)
    new = [m for m in history(br_user) if m["id"] not in before]
    assert new, "the accepted message is not in the history"
    body = new[-1]["body"]
    texts = pages.message_texts(bp.page, ROOM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and body.strip() not in [t.strip() for t in texts.all_inner_texts()]:
        time.sleep(0.3)
    shown = [t.strip() for t in texts.all_inner_texts()]
    if body.strip() not in shown:
        # not a rendering question until the stream is known to carry the text: read the public stream from the tester
        frames = _public_stream_while_posting(br_user, web_client, payload + " again")
        kinds = sorted({f.get("event", "?") for f in frames})
        with_body = [f for f in frames if f.get("event") == "data" and '"body"' in f.get("data", "")]
        shape = ""
        if with_body:
            try:
                row = json.loads(with_body[0]["data"])
                shape = "; the frame's fields: %s, item_type=%r protocol=%r room=%r dst_kind=%r" % (
                    sorted(row), row.get("item_type"), row.get("protocol"), row.get("room"), row.get("dst_kind"))
            except ValueError:
                shape = "; the frame's data is not JSON"
        finding(bp.engine, "a room message accepted with 202 (and in the history) did not appear on the signed-out "
                           "dashboard within 15 s although the stream was Live (documented: web.md Roles, 'sees the public "
                           "text and status messages of a room as they arrive on the public event stream'); a tester "
                           "client on /api/v1/public/events during a second post saw events %s, %d with a message body" % (
                               kinds, len(with_body)) + shape)
        pytest.skip("live room text not delivered to the dashboard (finding recorded)")
    # signed out: still no history fetch
    assert not bp.seen("/api/v1/user/")
    assert_inert(bp, "dashboard, live message")
    report(bp, EXPECT_401, "signed-out dashboard with a live hostile message")


def test_xss_invite_fields(operator_page, web_operator, ops_data):
    """Invite fields are a callsign, a role and a flag: hostile values are refused; the invites page shows no payload."""
    bp = operator_page
    for payload in PAYLOADS[:4]:
        for body in ({"callsign": payload, "role": "user", "may_tx": False},
                     {"callsign": "N0XSS9", "role": payload, "may_tx": False},
                     {"callsign": "N0XSS9", "role": "user", "may_tx": False, "expires_in_secs": payload}):
            resp = op_request(web_operator, "POST", "/api/v1/operator/invites", body)
            assert resp.status in (400, 422), "invite %s: HTTP %d, documented validation (400/422)" % (
                {k: v for k, v in body.items() if v == payload}, resp.status)
    title = open_ops(bp, "invites")
    text = pages.ops_table(bp.page, title).inner_text()
    assert "__pwn" not in text and "<" not in text, "hostile text on the invites page: %s" % text[:200]
    assert_inert(bp, "invites page")
    report(bp, set(), "invites page after hostile invites")


def test_xss_audit_text(operator_page, web_operator, xss_accounts):
    """Whatever text reaches the audit tail is shown as the API has it, as text. Hostile requests are made
    first (failed logins, hostile path segments, a name change) and the audit page is read afterwards."""
    from urllib.parse import quote
    bp = operator_page
    anon = web.WebClient(web_operator.base_url)
    anon._ctx = web_operator._ctx
    for payload in PAYLOADS[:3]:
        anon.request("POST", AUTH_SESSION, json_body={"callsign": payload, "password": "wrong-password-1"})
    for payload in PAYLOADS[:2]:
        seg = quote(payload, safe="")
        op_request(web_operator, "DELETE", "/api/v1/operator/invites/%s" % seg)
        op_request(web_operator, "DELETE", "/api/v1/operator/accounts/%s" % seg)
        op_request(web_operator, "POST", "/api/v1/operator/edges/%s/clients/%s/kick" % (seg, seg), {})
    if xss_accounts and xss_accounts[0]["name"] is not None:
        op_request(web_operator, "PATCH", "/api/v1/operator/accounts/%s" % xss_accounts[0]["callsign"],
                   {"name": PAYLOADS[2]})
    api = op_request(web_operator, "GET", "/api/v1/operator/audit?limit=50").json()
    rows = api["items"]
    hostile = [r for r in rows if any(isinstance(v, str) and re.search(r"[<>`$]|javascript:", v) for v in r.values())]
    note("audit tail: %d rows, %d with attacker-chosen characters" % (len(rows), len(hostile)))
    title = open_ops(bp, "audit")
    page_rows = pages.ops_rows(bp.page, title)
    expect(page_rows.first).to_be_visible()
    def norm(v):
        return "" if v is None else str(v)

    shown = set()
    for row in page_rows.all():
        cells = [c.strip() for c in row.locator("td").all_inner_texts()]
        shown.add(tuple(cells[1:3] + cells[4:7]))
    # the page lists the tail at the time it loaded; every row it shows was in the API tail then or since
    api2 = op_request(web_operator, "GET", "/api/v1/operator/audit?limit=250").json()["items"]
    known = {tuple(norm(r.get(k)) for k in ("user", "role", "action", "target", "result"))
             for r in api2 if r.get("action") != "recording_play"}
    odd = [x for x in shown if x not in known]
    assert not odd, "%s: audit rows on the page that the API does not have: %s" % (brlib.label(bp.engine), odd[:3])
    text = pages.ops_table(bp.page, title).inner_text()
    for r in hostile:
        for v in r.values():
            if isinstance(v, str) and re.search(r"[<>`$]|javascript:", v):
                assert v.strip() in text, "audit text %r not shown as text" % v
    assert_inert(bp, "audit page")
    report(bp, set(), "audit page with hostile requests behind it")


def test_xss_last_heard_callsign(dashboard_page, rig, web_client):
    """A callsign with markup characters in the 8 bytes of a D-STAR link and header (the protocol carries any
    byte): the router either refuses it (noted) or hears it, and then the page shows it as text."""
    from dvtest import clients
    from dvtest.audio import signals
    from dvtest.proto import dextra
    bp = dashboard_page
    heard_as = []
    for hostile in ("<b>X</b>", "N0<B>X", "N0&AMP;X", "N0\"X", "N0'X", "N0`X"):
        client = dextra.DstarClient("0.0.0.0", callsign=hostile, module="B")
        try:
            if not client.link(rig.xlxd_sut):
                note("last heard: no D-STAR link ACK for callsign %r (refused by the router's validation)" % hostile)
                continue
            client.talk(signals.prng_frames("dstar", 63, seed="web-xss-heard"),
                        clients.fresh_stream_id("dstar"), tail=1.0)
            heard_as.append(hostile)
        finally:
            try:
                client.unlink()
            except Exception:
                pass
            client.close()
    items = web_client().get("/api/v1/public/lastheard?limit=50", expect=200).json()["items"]
    bad = [i for i in items if re.search(r"[<>&\"'`]", str(i.get("callsign", "")) + str(i.get("callsign_base", "")))]
    note("last heard: talked as %s, %d of %d rows carry markup characters in the callsign" % (heard_as, len(bad), len(items)))
    if not bad:
        pytest.skip("the router stored no callsign with markup characters (links refused or sanitised: %s)" % heard_as)
    open_dashboard(bp)
    cells = bp.page.locator("main table tbody td").all_inner_texts()
    for row in bad:
        base = str(row.get("callsign_base") or row.get("callsign"))
        assert any(base in c for c in cells), "heard callsign %r not shown as text" % base
    assert_inert(bp, "dashboard last heard")
    report(bp, EXPECT_401, "dashboard last heard")


def test_xss_reflected_in_url_and_forms(dashboard_page):
    """Hostile text in the query string, the hash, the path and the form fields is never executed or echoed as markup."""
    from urllib.parse import quote
    bp = dashboard_page
    # this case loads many pages: the live stream (4 per address) is not what it is about, so it is refused
    # here and the dashboard polls (the stream itself is covered by the other cases)
    bp.context.route("**/api/v1/public/events*", lambda route: route.abort())
    for payload in PAYLOADS[:5]:
        q = quote(payload, safe="")
        for path in ("/?x=%s#/heard?call=%s" % (q, q), "/login.html?callsign=%s&next=%s#%s" % (q, q, q),
                     "/register.html?invite=%s#%s" % (q, q), "/ops/?x=%s#/%s" % (q, q), "/%s" % q):
            resp = bp.page.goto(bp.url(path))
            assert resp is not None and resp.status < 500, (path, resp and resp.status)
            bp.page.wait_for_timeout(300)
            assert_inert(bp, "reflected " + path.split("?")[0])
    # typed into the forms of the dashboard and the login page
    bp.goto(pages.dashboard_url("/heard"))
    try:
        expect(pages.heard_filter(bp.page)).to_be_visible(timeout=20000)    # the events are refused here: the first poll is ~10 s away
    except AssertionError:
        pass
    if pages.heard_filter(bp.page).count() == 0:
        finding(bp.engine, "the Last heard page (#/heard) did not render its filter (page errors: %s)" % [e[:80] for e in bp.log.errors[-1:]])
    else:
        for payload in PAYLOADS:
            pages.heard_filter(bp.page).fill(payload)
            bp.page.wait_for_timeout(100)
            assert_inert(bp, "heard filter")
    bp.goto(pages.LOGIN)
    for payload in PAYLOADS[:3]:
        ui_sign_in(bp, payload[:60], "typed-in-a-test-1")
        expect(pages.auth_error(bp.page)).to_have_text(pages.LOGIN_FAILED_TEXT)
        assert payload not in bp.page.locator("body").inner_text(), "the typed callsign is echoed in the page"
        assert_inert(bp, "login form")
    report(bp, {(AUTH_SESSION, 401)}, "reflected text", ignore=r"Failed to load resource|/%[0-9A-F]{2}|/javascript%3A")


# ============================================================================ i: CSP

CSP_PAGES = ("/", "/login.html", "/register.html", "/totp.html", "/ops/")


def test_csp_header_and_enforcement(browser_page):
    bp = browser_page
    for path in CSP_PAGES[:5]:
        resp = bp.page.goto(bp.url(path))
        assert resp.status == 200, (path, resp.status)
        csp = resp.headers.get("content-security-policy")
        weak = brlib.csp_problems(csp)
        assert not weak, "%s %s: CSP %r: %s" % (brlib.label(bp.engine), path, csp, "; ".join(weak))
        assert resp.headers.get("x-content-type-options", "").lower() == "nosniff"
        note("%s: CSP of %s: %s" % (brlib.label(bp.engine), path, csp))
    # normal use: the dashboard, its routes and the login page raise no report (a report is a finding)
    open_dashboard(bp)
    bp.page.wait_for_timeout(500)
    bp.goto(pages.LOGIN)
    bp.page.wait_for_timeout(500)
    for v in list(bp.log.csp):
        finding(bp.engine, "CSP report in normal use: %s blocked %s (source %s)" % (
            v.get("directive"), v.get("blocked"), (v.get("source") or "-").rsplit("/", 1)[-1]))
    bp.log.csp.clear()
    # deliberate violations: an inline script and an inline event handler are blocked by this engine and reported
    bp.goto(pages.LOGIN)
    bp.page.evaluate("""() => {
        const s = document.createElement('script');
        s.textContent = 'window.__pwn = 1';
        document.body.appendChild(s);
        const img = document.createElement('img');
        img.setAttribute('onerror', 'window.__pwn = 2');
        img.src = 'x:';
        document.body.appendChild(img);
    }""")
    bp.page.wait_for_timeout(600)
    assert bp.page.evaluate("typeof window.__pwn") == "undefined", "inline script or handler ran: CSP not enforced"
    directives = sorted({v.get("directive", "") for v in bp.log.csp})
    assert any(d.startswith("script-src") or d == "default-src" for d in directives), \
        "the engine reported no script violation for an inline script (reports: %s)" % directives
    note("%s: deliberate violations reported as %s" % (brlib.label(bp.engine), directives))
    bp.log.csp.clear()
    bp.log.dialogs.clear()


# ================================================================== j: admin SPA

def test_admin_spa_loads(browser_page, web_url_admin):
    bp = browser_page
    bp.log.origin = web_url_admin.rstrip("/")
    resp = bp.page.goto(web_url_admin + "/")
    if resp.status != 200:
        finding(bp.engine, "GET / on the admin host from the browser address: HTTP %d (allow_from lists the tester and the browser)" % resp.status)
        pytest.skip("admin host answered HTTP %d" % resp.status)
    expect(pages.admin_sign_in_heading(bp.page)).to_be_visible(timeout=LIVE_MS)
    weak = brlib.csp_problems(resp.headers.get("content-security-policy"))
    assert not weak, "admin host CSP: %s" % "; ".join(weak)
    bp.page.wait_for_timeout(500)
    for p in bp.problems():
        if "HTTP 401" in p or "Failed to load resource" in p:
            continue
        finding(bp.engine, "admin sign-in screen: %s" % p)
