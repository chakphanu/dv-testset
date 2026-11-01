# SPDX-License-Identifier: MIT
"""Fixtures of the browser cases (cases/test_web_browser.py). Imported by conftest.py.

The tester container has no browsers. The rig service `browser` (profile browser, together
with web: `scripts/run.sh --profile web,browser`) runs `playwright run-server`; the cases
connect to it with the Playwright client (DVT_BROWSER_WS) and the pages are loaded by the
browsers from the address of that container, over the rig network. Every fixture skips with
the reason unless the profile is up. WebKit here is Playwright WebKit (the engine of Safari,
not Safari): results are labelled with dvtest.browser.label().

  engine           parameter: chromium, firefox, webkit
  browser_page     a BrowserPage in a NEW context of that engine (anonymous)
  user_page        the same, signed in as the browser user (API login inside the browser, signed
                   out again at the end so the account never holds more than one session)
  operator_page    the same, signed in as the browser operator (one login per engine and run,
                   the session is reused through the context storage state: a TOTP code is
                   one-use and the account has at most three sessions)

A BrowserPage keeps what the page did (console, page errors, requests with their status,
dialogs, CSP violations) in a dvtest.browser.PageLog; `problems()` is the list of things a
normal view must not do. The context ignores certificate errors (ignore_https_errors): the
proxy's certificate is self-signed and the browsers are not told about the run CA; nothing
else about TLS is relaxed. On a failed case the screenshot, the trace and the HAR go to
$DVT_OUT/web/browser/<engine>-<case>/ (local to out/, never committed; a trace holds the
typed values of a throwaway run).
"""
import json
import os
import shutil
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from dvtest import browser as brlib
from dvtest import web
from web_fixtures import WebUser

NAV_TIMEOUT_MS = 20000
ACTION_TIMEOUT_MS = 12000
CONNECT_WAIT_S = 90.0

# Reports the page script makes to the case (exposed function) and the CSP violations it sees.
# Runs before the page's own scripts, in every frame.
INIT_SCRIPT = """
(() => {
  if (window.__dvtInstalled) return;
  window.__dvtInstalled = true;
  document.addEventListener('securitypolicyviolation', (e) => {
    try {
      window.__dvtReport('csp', JSON.stringify({ directive: e.violatedDirective, blocked: e.blockedURI,
        sample: (e.sample || '').slice(0, 60), source: e.sourceFile }));
    } catch (err) { /* the binding is not there yet */ }
  }, true);
})();
"""


def _out_dir():
    return os.environ.get("DVT_OUT", "out")


# ------------------------------------------------------------- failure hook

@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


# ----------------------------------------------------------- session fixtures

@pytest.fixture(scope="session")
def browser_ws():
    if os.environ.get("DVT_BROWSER") != "on":
        pytest.skip("browser profile not up: run scripts/run.sh --profile web,browser "
                    "(the tester gets DVT_BROWSER=on and DVT_BROWSER_WS)")
    url = os.environ.get("DVT_BROWSER_WS")
    if not url:
        pytest.skip("DVT_BROWSER=on but DVT_BROWSER_WS is not set")
    parts = urlsplit(url)
    deadline = time.monotonic() + CONNECT_WAIT_S
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((parts.hostname, parts.port or 80), timeout=3):
                return url
        except OSError as exc:
            last = str(exc)
            time.sleep(1.0)
    pytest.skip("the browser service %s does not accept connections within %d s (%s)" % (url, CONNECT_WAIT_S, last))


@pytest.fixture(scope="session")
def playwright_driver(browser_ws):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("the playwright Python package is not in the tester image (rebuild it: PLAYWRIGHT_VERSION in .env.example)")
    pw = sync_playwright().start()
    yield pw
    pw.stop()


def _client_version():
    try:
        from playwright._repo_version import version
        return version
    except ImportError:
        return "?"


def _note_engine(engine, version):
    """out/<run>/web/browser/engines.txt: which browser build answered (labelled, never 'Safari')."""
    path = Path(_out_dir()) / "web" / "browser"
    try:
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "engines.txt", "a", encoding="utf-8") as fh:
            fh.write("%s %s (Playwright %s)\n" % (brlib.label(engine), version, _client_version()))
    except OSError:
        pass


MIC_TONE_HZ = 700.0                      # the tone of the fake microphone file of the talk cases
MIC_FILE = "fake-mic.wav"                # $DVT_OUT/media/fake-mic.wav = /media/fake-mic.wav in the browser service


@pytest.fixture(scope="session")
def fake_mic_file():
    """Write the fake microphone file (a 700 Hz sine, 48 kHz mono 16 bit, 30 s, looped by Chromium) into
    $DVT_OUT/media/ at run time. The browser service mounts that directory read-only at /media. The file is
    generated, never committed (*.wav is ignored). Returns the path as the tester sees it."""
    path = Path(_out_dir()) / "media" / MIC_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(brlib.tone_wav_bytes(MIC_TONE_HZ, 30.0))
    except OSError as exc:
        pytest.skip("cannot write the fake microphone file %s: %s" % (path, exc))
    return str(path)


@pytest.fixture(scope="session")
def engine_browsers(playwright_driver, browser_ws, fake_mic_file):
    """engine -> connected Browser, connected on first use and kept for the session (a context per
    case isolates the cases; one connection per engine keeps the service at three browsers).
    Each engine starts with a fake microphone (dvtest.browser.launch_options: Chromium flags and the
    file above, Firefox prefs) passed in the `launch-options` query of the run-server URL; the service
    runs `playwright run-server --unsafe` for that (rig/compose.yml)."""
    made = {}

    def get(engine):
        if engine not in made:
            kind = getattr(playwright_driver, engine)
            last = None
            for _ in range(3):
                try:
                    made[engine] = kind.connect(brlib.with_launch_options(browser_ws, brlib.launch_options(engine)),
                                                timeout=60000)
                    _note_engine(engine, made[engine].version)
                    break
                except Exception as exc:       # the service may still be starting its driver
                    last = exc
                    time.sleep(2.0)
            else:
                pytest.fail("cannot connect %s to %s: %s" % (brlib.label(engine), browser_ws, last))
        return made[engine]

    get.versions = lambda: {e: b.version for e, b in made.items()}
    yield get
    for browser in made.values():
        try:
            browser.close()
        except Exception:
            pass


@pytest.fixture(params=brlib.ENGINES)
def engine(request):
    return request.param


# ----------------------------------------------------------------- accounts

@pytest.fixture(scope="session")
def br_user(web_user):
    """The browser user (may_tx: it also posts the room messages the cases look at). Its own
    session of the tester is not used by the browser, which logs in by itself (user_page)."""
    return web_user("N0BRU", may_tx=True)


def _operator_account(web_operator, web_base_url, web_ca, callsign):
    """A second-or-later operator with TOTP enrolled and NO live session (the browser makes its own;
    an account has at most three sessions). Its client keeps the authenticator (one code per counter)."""
    password = web.new_password()
    web_operator.hide(password)
    web_operator.register_user(callsign, password, may_tx=True, role="operator")
    client = web.WebClient(web_base_url, web_ca)
    client.hide(password)
    ident = client.login(callsign, password)
    if ident.get("totp") != "enrol":
        pytest.fail("%s first login: totp state %r, wanted 'enrol'" % (callsign, ident.get("totp")))
    client.enrol_totp()
    auth = client.authenticator
    client.logout()
    client.authenticator = auth
    return WebUser(callsign, password, True, client)


@pytest.fixture(scope="session")
def br_operator(web_operator, web_base_url, web_ca):
    """The operator whose sessions the ops page cases use (one per engine)."""
    return _operator_account(web_operator, web_base_url, web_ca, "N0BRO")


@pytest.fixture(scope="session")
def br_login_operator(web_operator, web_base_url, web_ca):
    """An operator for the login and logout forms (kept apart from br_operator: its session count)."""
    return _operator_account(web_operator, web_base_url, web_ca, "N0BRL")


# ----------------------------------------------------------------- BrowserPage

# The service allows 4 event streams per client address and frees the slot of a closed stream when its next
# heartbeat write fails (every 15 s, web.md / admin.md): the browsers of every case share one address. A case
# that has just ended may still hold slots, so a new one waits until at most (4 - need) are stale.
STREAM_CAP = 4
STREAM_FREE_S = 17.0
SSE_PATHS = ("/api/v1/public/events", "/api/v1/operator/stream")
_closed_streams = []                  # (monotonic time of the close, number of streams the page had open)


def wait_for_stream_slots(need=2):
    waited = 0.0
    while True:
        now = time.monotonic()
        stale = sum(n for t, n in _closed_streams if now - t < STREAM_FREE_S)
        if stale <= STREAM_CAP - need:
            return waited
        time.sleep(1.0)
        waited += 1.0


class BrowserPage:
    """One page in one new context, with its log."""

    def __init__(self, engine, browser, base_url, name, storage_state=None, context_options=None):
        wait_for_stream_slots()
        self.engine = engine
        self.base = base_url.rstrip("/")
        self.name = name
        self.log = brlib.PageLog(origin=self.base)
        self.art = brlib.artifact_dir(_out_dir(), engine, name)
        self._har_tmp = None
        opts = dict(ignore_https_errors=True, locale="en-US", timezone_id="UTC",
                    viewport={"width": 1280, "height": 800}, service_workers="block")
        if storage_state is not None:
            opts["storage_state"] = storage_state
        self.permissions_error = ""
        if context_options:
            opts.update(context_options)
        os.makedirs(os.path.dirname(self.art), exist_ok=True)
        self._har_tmp = self.art + ".har"
        try:
            self.context = browser.new_context(record_har_path=self._har_tmp, record_har_content="omit", **opts)
            self.har = True
        except Exception as exc:                       # HAR over a remote connection unsupported: go without
            self._har_tmp = None
            self.har = False
            if "permissions" in opts and "ermission" in str(exc):
                # an engine that does not know the permission: keep going without and say so
                self.permissions_error = str(exc).splitlines()[0][:160]
                opts.pop("permissions")
            self.context = browser.new_context(**opts)
        self.context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self.context.add_init_script(INIT_SCRIPT)
        self.context.expose_function("__dvtReport", self._report)
        self.context.tracing.start(screenshots=True, snapshots=True)
        self.page = self.context.new_page()
        self._hook(self.page)

    # -- events
    def _report(self, kind, data=""):
        if kind == "csp":
            try:
                self.log.csp.append(json.loads(data))
            except ValueError:
                self.log.csp.append({"directive": "?", "blocked": str(data)[:80]})
        else:
            self.log.reports.append((kind, str(data)[:80]))

    def _hook(self, page):
        log = self.log

        def on_console(msg):
            loc = msg.location or {}
            log.console.append({"type": msg.type, "text": msg.text, "url": loc.get("url", "")})

        def on_response(resp):
            req = resp.request
            if resp.url.startswith(self.base):
                try:
                    ctype = resp.headers.get("content-type", "")
                except Exception:
                    ctype = ""
                brlib.record_request(log, req.method, resp.url, req.resource_type, resp.status, ctype)

        def on_failed(req):
            if not req.url.startswith(self.base):
                return
            why = req.failure or ""
            # the page closing an event stream or leaving a page aborts its requests: not a fault
            if req.resource_type in ("eventsource", "fetch", "xhr", "document", "other") and (
                    "abort" in why.lower() or "NS_BINDING_ABORTED" in why or "cancel" in why.lower()
                    or "interrupted" in why.lower()):
                return
            log.failed.append({"method": req.method, "path": urlsplit(req.url).path, "why": why[:80]})

        def on_dialog(dialog):
            log.dialogs.append({"type": dialog.type, "message": dialog.message})
            try:
                dialog.dismiss()
            except Exception:
                pass

        page.on("console", on_console)
        def on_pageerror(exc):
            stack = (getattr(exc, "stack", "") or "").replace("\n", " <- ")
            log.errors.append("%s: %s | %s" % (getattr(exc, "name", "Error"), getattr(exc, "message", exc), stack[:4000]))

        page.on("pageerror", on_pageerror)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)
        page.on("dialog", on_dialog)

    # -- navigation and evaluation
    def url(self, path):
        return self.base + path

    def goto(self, path, wait_until="load"):
        return self.page.goto(self.url(path), wait_until=wait_until)

    def problems(self, expected=()):
        return self.log.problems(expected)

    def install_rtc_probe(self):
        """Wrap RTCPeerConnection and tap the received audio (dvtest.browser.RTC_PROBE_JS) in every page of
        the context loaded from now on. Call before the first navigation."""
        self.context.add_init_script(brlib.RTC_PROBE_JS)

    def apply_fragment_workaround(self):
        """Serve /public.js with `<>` replaced (patch_public_js); True when the script needed it. Call before
        the first navigation of the case. Only the cases that look at the rendered dashboard use it; the case
        test_dashboard_renders_the_state runs without it and says what is broken."""
        self.patched = False

        def handler(route):
            resp = route.fetch()
            body = resp.text()
            new = brlib.patch_public_js(body)
            if new is None:
                route.fulfill(response=resp)
                return
            self.patched = True
            data = new.encode("utf-8")
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "etag")}
            headers["content-length"] = str(len(data))
            route.fulfill(status=resp.status, headers=headers, body=data)

        self.context.route("**/public.js", handler)

    def apply_recordings_workaround(self):
        """The same fragment defect (finding 1) is in recordings.js: serve it with every `<>` made a
        component (dvtest.browser.patch_fragments_js); True through self.patched_recordings when needed."""
        self.patched_recordings = False

        def handler(route):
            resp = route.fetch()
            body = resp.text()
            new = brlib.patch_fragments_js(body)
            if new is None:
                route.fulfill(response=resp)
                return
            self.patched_recordings = True
            data = new.encode("utf-8")
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "etag")}
            headers["content-length"] = str(len(data))
            route.fulfill(status=resp.status, headers=headers, body=data)

        self.context.route("**/recordings/recordings.js", handler)

    def seen(self, path_prefix, method=None):
        return self.log.paths(path_prefix, method)

    # -- API calls made by the browser itself (same origin, its cookie jar, its User-Agent: the
    # session of the web service is bound to the User-Agent, so a session made elsewhere would not work)
    def _on_origin(self):
        if not self.page.url.startswith(self.base):
            self.page.goto(self.url("/shared/img/favicon.svg"))

    def api_login(self, callsign, password, totp=None):
        """POST /api/v1/auth/session from inside the browser; returns the HTTP status."""
        web.PACER.wait("login")
        self._on_origin()
        body = {"callsign": callsign, "password": password}
        if totp:
            body["totp"] = totp
        return self.page.evaluate(
            """async (body) => {
                 const r = await fetch('/api/v1/auth/session', {method: 'POST', credentials: 'same-origin',
                   headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
                 return r.status;
               }""", body)

    def api_logout(self):
        """DELETE /api/v1/auth/session with the CSRF token of the current session; the status or None."""
        self._on_origin()
        return self.page.evaluate(
            """async () => {
                 const s = await fetch('/api/v1/auth/session', {credentials: 'same-origin'});
                 if (s.status !== 200) return s.status;
                 const csrf = (await s.json()).csrf;
                 const r = await fetch('/api/v1/auth/session', {method: 'DELETE', credentials: 'same-origin',
                   headers: {'content-type': 'application/json', 'X-DV-CSRF': csrf}, body: '{}'});
                 return r.status;
               }""")

    def cookies(self):
        return self.context.cookies()

    def session_cookie(self):
        for c in self.context.cookies():
            if c["name"].endswith("dv_web"):
                return c
        return None

    # -- end
    def _dump_page_errors(self):
        """A page exception: keep its full stack and the public data the page was rendering (the public
        JSON of the service, no secrets) in out/<run>/web/browser/pageerrors/ for the report."""
        d = os.path.join(_out_dir(), "web", "browser", "pageerrors")
        os.makedirs(d, exist_ok=True)
        data = {"engine": brlib.label(self.engine), "url": self.page.url.split("?")[0], "errors": self.log.errors}
        for key, path in (("state", "/api/v1/public/state"), ("lastheard", "/api/v1/public/lastheard?limit=50")):
            try:
                data[key] = json.loads(self.context.request.get(self.url(path)).text())
            except Exception as exc:
                data[key] = "unavailable: %s" % exc
        with open(os.path.join(d, brlib.safe_name("%s-%s" % (self.engine, self.name)) + ".json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)

    def finish(self, failed):
        """Close the context; keep screenshot, trace and HAR when the case failed."""
        if self.log.errors:
            try:
                self._dump_page_errors()
            except Exception:
                pass
        streams = sum(1 for r in self.log.requests if r["path"] in SSE_PATHS and r["status"] == 200)
        try:
            if failed:
                os.makedirs(self.art, exist_ok=True)
                try:
                    self.page.screenshot(path=os.path.join(self.art, "page.png"), full_page=True)
                except Exception:
                    pass
                try:
                    self.context.tracing.stop(path=os.path.join(self.art, "trace.zip"))
                except Exception:
                    pass
                with open(os.path.join(self.art, "log.json"), "w", encoding="utf-8") as fh:
                    json.dump({"engine": brlib.label(self.engine), "url": self.page.url.split("?")[0],
                               "console": self.log.console[-100:], "errors": self.log.errors,
                               "requests": [{k: r[k] for k in ("method", "path", "status", "resource")}
                                            for r in self.log.requests[-300:]],
                               "failed": self.log.failed, "dialogs": self.log.dialogs, "csp": self.log.csp}, fh, indent=1)
            else:
                try:
                    self.context.tracing.stop()
                except Exception:
                    pass
        finally:
            try:
                self.context.close()
            except Exception:
                pass
            if streams:
                _closed_streams.append((time.monotonic(), streams))
            # the client writes the HAR (a zip) to the path or, over a remote connection, to <path>.tmp
            for har in [h for h in (self._har_tmp, (self._har_tmp or "") + ".tmp") if h and os.path.exists(h)]:
                if failed:
                    os.makedirs(self.art, exist_ok=True)
                    shutil.move(har, os.path.join(self.art, "page.har.zip"))
                    failed = False             # one is enough
                else:
                    os.unlink(har)


def _failed(request):
    rep = getattr(request.node, "rep_call", None)
    return bool(rep is not None and rep.failed)


@pytest.fixture
def web_origin(web_base_url):
    return web_base_url


@pytest.fixture
def browser_page(engine, engine_browsers, web_base_url, request):
    bp = BrowserPage(engine, engine_browsers(engine), web_base_url, request.node.name.replace("[", "-").rstrip("]"))
    yield bp
    bp.finish(_failed(request))


@pytest.fixture
def dashboard_page(browser_page):
    """browser_page with the dashboard script made renderable (BrowserPage.apply_fragment_workaround)."""
    browser_page.apply_fragment_workaround()
    return browser_page


@pytest.fixture
def user_page(engine, engine_browsers, web_base_url, request, br_user):
    """Signed in as the browser user for the case; signed out (session revoked) afterwards."""
    bp = BrowserPage(engine, engine_browsers(engine), web_base_url, request.node.name.replace("[", "-").rstrip("]"))
    bp.apply_fragment_workaround()
    status = bp.api_login(br_user.callsign, br_user.password)
    assert status == 200, "browser API login of the user: HTTP %s" % status
    yield bp
    try:
        bp.api_logout()
    except Exception:
        pass
    bp.finish(_failed(request))


@pytest.fixture(scope="session")
def operator_state(engine_browsers, web_base_url, br_operator):
    """engine -> storage state of one operator session made inside that engine."""
    made = {}

    def get(engine):
        if engine not in made:
            bp = BrowserPage(engine, engine_browsers(engine), web_base_url, "operator-login-" + engine)
            try:
                code = br_operator.client.authenticator.next_code()
                br_operator.client.hide(code)
                status = bp.api_login(br_operator.callsign, br_operator.password, code)
                assert status == 200, "%s: browser API login of the operator: HTTP %s" % (brlib.label(engine), status)
                made[engine] = bp.context.storage_state()
            finally:
                bp.finish(False)
        return made[engine]

    return get


@pytest.fixture
def operator_page(engine, engine_browsers, web_base_url, request, operator_state):
    bp = BrowserPage(engine, engine_browsers(engine), web_base_url, request.node.name.replace("[", "-").rstrip("]"),
                     storage_state=operator_state(engine))
    yield bp
    bp.finish(_failed(request))


# ---------------------------------------------------------- stage 3b: Listen / Talk pages

@pytest.fixture(scope="session")
def br_rx(web_user):
    """A user without may_tx: it can listen, the page shows no Talk controls."""
    return web_user("N0BRX", may_tx=False)


@pytest.fixture(scope="session")
def br_tx2(web_user):
    """A second user with may_tx (the second talker of the floor cases)."""
    return web_user("N0BRT", may_tx=True)


def _rtc_page(engine, engine_browsers, web_base_url, request, user, talk):
    """Signed in as `user` (API login inside the browser, the session is bound to its User-Agent), with the
    RTCPeerConnection probe and the dashboard workaround. `talk` grants the microphone permission to the
    engines that take it as a context permission (Chromium, WebKit; Firefox uses its prefs)."""
    ctx = {"permissions": ["microphone"]} if talk and engine in ("chromium", "webkit") else None
    bp = BrowserPage(engine, engine_browsers(engine), web_base_url, request.node.name.replace("[", "-").rstrip("]"),
                     context_options=ctx)
    bp.install_rtc_probe()
    bp.apply_fragment_workaround()
    status = bp.api_login(user.callsign, user.password)
    if status != 200:
        bp.finish(True)
        pytest.fail("browser API login of %s: HTTP %s" % (user.callsign, status))
    return bp


def _rtc_end(bp, request):
    try:
        bp.api_logout()
    except Exception:
        pass
    bp.finish(_failed(request))


@pytest.fixture
def rtc_rx_page(engine, engine_browsers, web_base_url, request, br_rx):
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, br_rx, talk=False)
    yield bp
    _rtc_end(bp, request)


@pytest.fixture
def rtc_tx_page(engine, engine_browsers, web_base_url, request, br_user):
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, br_user, talk=True)
    yield bp
    _rtc_end(bp, request)


@pytest.fixture
def rtc_tx2_page(engine, engine_browsers, web_base_url, request, br_tx2):
    bp = _rtc_page(engine, engine_browsers, web_base_url, request, br_tx2, talk=True)
    yield bp
    _rtc_end(bp, request)
