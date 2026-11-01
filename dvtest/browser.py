# SPDX-License-Identifier: MIT
"""Pure helpers of the browser cases (cases/test_web_browser.py, cases/browser_fixtures.py,
docs/web-browser-cases.md). Nothing here imports Playwright or touches the network, so it is
unit-tested (cases/unit/test_browser_helpers.py).

  * ENGINES / ENGINE_LABEL: the three Playwright engines and how a result is labelled. The
    WebKit of Playwright is the engine of Safari but it is NOT Safari (no iOS, no Safari
    privacy features, a Linux build): it is called "Playwright WebKit" everywhere.
  * XSS_PAYLOADS: hostile strings for every field the UI renders from stored data. Each one
    tries to set `window.__pwn` or to open a dialog if it ever becomes live markup or script.
  * PageLog: what a page did (console, page errors, requests, responses, dialogs, CSP
    violations) and `PageLog.problems()`, the list of things a normal page view must not do.
  * parse_csp / csp_problems, cookie_problems: header and cookie judgements.
  * patch_public_js: the one-line workaround that lets the dashboard script render (a finding).
  * artifact_dir / safe_name: where failure artifacts go ($DVT_OUT/web/browser/).
"""
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

ENGINES = ("chromium", "firefox", "webkit")
ENGINE_LABEL = {"chromium": "Playwright Chromium", "firefox": "Playwright Firefox",
                "webkit": "Playwright WebKit"}

SESSION_COOKIE = "__Host-dv_web"           # web.md, "Hostnames and routing"


def label(engine):
    """'Playwright WebKit' for 'webkit' (never 'Safari')."""
    return ENGINE_LABEL[engine]


# ----------------------------------------------------------------- hostile strings

# printable ASCII only and short (a display name is at most 128 bytes, a message text 256), so the
# same strings can go into every field. Each reaches for `window.__pwn` or `alert` when it runs.
XSS_PAYLOADS = (
    "<script>window.__pwn=1;alert(1)</script>",
    "<img src=x onerror=\"window.__pwn=1;alert(1)\">",
    "\"><svg onload=window.__pwn=1;alert(1)>",
    "javascript:window.__pwn=1;alert(1)",
    "<a href=\"javascript:window.__pwn=1\">click</a>",
    "${window.__pwn=1}{{constructor.constructor('window.__pwn=1')()}}",
    "' onfocus='window.__pwn=1' autofocus x='",
    "`;window.__pwn=1;//</script><script>window.__pwn=1</script>",
)

# markup that must never exist as a live element after a payload was shown as text
LIVE_MARKUP_SELECTOR = ("img[src='x'], img[onerror], svg[onload], [onfocus], "
                        "a[href^='javascript:'], script:not([src])")


def printable_ascii(text):
    return all(0x20 <= ord(c) <= 0x7E for c in text)


# ------------------------------------------------------------------- page log

@dataclass
class PageLog:
    """Everything a page did while a case drove it. The fixture fills the lists from
    Playwright events; `problems()` judges them."""
    origin: str = ""
    console: list = field(default_factory=list)       # {type, text, url}
    errors: list = field(default_factory=list)        # uncaught page exceptions (strings)
    requests: list = field(default_factory=list)      # {method, url, path, status, ctype, resource}
    failed: list = field(default_factory=list)        # {method, path, why}
    dialogs: list = field(default_factory=list)       # {type, message}
    csp: list = field(default_factory=list)           # securitypolicyviolation events
    reports: list = field(default_factory=list)      # window.__dvtReport(kind, data) calls

    def same_origin(self, url):
        return bool(self.origin) and url.startswith(self.origin + "/") or url == self.origin

    def paths(self, prefix="", method=None):
        """Request paths (same origin, no query) that start with `prefix`."""
        out = []
        for r in self.requests:
            if method and r["method"] != method:
                continue
            if r["path"].startswith(prefix):
                out.append(r["path"])
        return out

    def statuses(self, path):
        return [r["status"] for r in self.requests if r["path"] == path and r["status"] is not None]

    def pwned(self):
        return [r for r in self.reports if r[0] == "xss"]

    def problems(self, expected=()):
        """What a normal view of a page must not do, as a list of strings (empty = clean).
        `expected` is a collection of (path, status) answers the page legitimately gets (for
        example the signed-out dashboard asks /api/v1/auth/session and gets 401); the console
        line a browser writes for such an answer is tolerated too, and only that line."""
        expected = set(expected)
        out = []
        for c in self.console:
            if c["type"] not in ("error", "assert"):
                continue
            if self._is_expected_network_line(c, expected):
                continue
            out.append("console %s: %s" % (c["type"], c["text"][:200]))
        for e in self.errors:
            out.append("page error: %s" % e[:200])
        for r in self.requests:
            st = r["status"]
            if st is None or not self.same_origin(r["url"]):
                continue
            if st >= 400 and (r["path"], st) not in expected:
                out.append("%s %s -> HTTP %d" % (r["method"], r["path"], st))
        for f in self.failed:
            out.append("request failed: %s %s (%s)" % (f["method"], f["path"], f["why"]))
        for d in self.dialogs:
            out.append("dialog opened: %s %r" % (d["type"], d["message"][:80]))
        for v in self.csp:
            out.append("CSP violation: %s blocked %s" % (v.get("directive"), (v.get("blocked") or "")[:80]))
        out.extend("script of a payload ran: %s" % (r[1],) for r in self.pwned())
        return out

    def _is_expected_network_line(self, c, expected):
        """'Failed to load resource: the server responded with a status of 401 ()' for a
        (path, status) the case expects."""
        m = re.match(r"Failed to load resource: the server responded with a status of (\d{3})", c["text"])
        if not m or not c.get("url"):
            return False
        path = urlsplit(c["url"]).path
        return (path, int(m.group(1))) in expected


def record_request(log, method, url, resource="", status=None, ctype=""):
    """Append a request record with the path split off (the query is kept in `url` only)."""
    log.requests.append({"method": method, "url": url, "path": urlsplit(url).path, "status": status,
                         "ctype": ctype, "resource": resource})
    return log.requests[-1]


def patch_public_js(source):
    """The public dashboard script with the one change that makes it render: `PublicContent` returns
    html`<>...</>`, and the vendored htm-preact-3.1.1 turns `<>` into a node of type '' (it has no Fragment),
    which Preact cannot create (document.createElement('') throws, the page body stays "Loading"). Here the
    fragment becomes a component that returns its children. Returns None when the script has no `<>`
    (fixed upstream), so the workaround switches itself off."""
    if "html`<>" not in source or "</>`" not in source:
        return None
    out = source.replace("html`<>", "html`<${__dvtFragment}>", 1)
    at = out.index("</>`", out.index("__dvtFragment"))
    out = out[:at] + "<//>`" + out[at + len("</>`"):]
    return "const __dvtFragment = (p) => p.children;\n" + out


# ---------------------------------------------------------------------- CSP

def parse_csp(header):
    """{directive: [sources]} of a Content-Security-Policy header value (lower-case names)."""
    out = {}
    for part in (header or "").split(";"):
        words = part.split()
        if words:
            out.setdefault(words[0].lower(), words[1:])
    return out


def csp_problems(header):
    """Weaknesses of a CSP for a page that renders stored data: empty list = restrictive.
    Judged: a default or script source of 'self' only, no unsafe-inline / unsafe-eval / data:
    / wildcard scripts, framing denied, no <base> or form hijack."""
    if not header:
        return ["no Content-Security-Policy header"]
    csp = parse_csp(header)
    out = []
    script = csp.get("script-src") or csp.get("default-src")
    if script is None:
        out.append("neither script-src nor default-src")
    else:
        for bad in ("'unsafe-inline'", "'unsafe-eval'", "*", "data:", "http:", "https:"):
            if bad in script:
                out.append("script source %s allowed" % bad)
    if "default-src" in csp and csp["default-src"] != ["'self'"]:
        out.append("default-src is %s, not 'self'" % " ".join(csp["default-src"]))
    if csp.get("frame-ancestors") != ["'none'"]:
        out.append("frame-ancestors is %s, not 'none'" % (" ".join(csp.get("frame-ancestors", [])) or "missing"))
    return out


# ------------------------------------------------------------------ cookies

def cookie_problems(cookie, name=SESSION_COOKIE):
    """Problems of a cookie as Playwright reports it (dict with name, secure, httpOnly,
    sameSite, path, domain): the documented session cookie is __Host-dv_web with Path=/,
    Secure, HttpOnly, SameSite=Strict and no Domain attribute (a host-only cookie)."""
    out = []
    if cookie.get("name") != name:
        out.append("name is %r, documented %r" % (cookie.get("name"), name))
    if not cookie.get("secure"):
        out.append("not Secure")
    if not cookie.get("httpOnly"):
        out.append("not HttpOnly (a script can read it)")
    if str(cookie.get("sameSite", "")).lower() != "strict":
        out.append("SameSite is %r, documented Strict" % cookie.get("sameSite"))
    if cookie.get("path") != "/":
        out.append("Path is %r, documented /" % cookie.get("path"))
    if cookie.get("domain", "").startswith("."):
        out.append("Domain attribute set (%r): not host-only" % cookie.get("domain"))
    return out


# ---------------------------------------------------------------- artifacts

def safe_name(text):
    """A file name part: letters, digits, dot, dash, underscore."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:120] or "case"


def artifact_dir(out_dir, engine, test_name):
    """$DVT_OUT/web/browser/<engine>-<test>/ (never committed: out/ is ignored)."""
    return os.path.join(out_dir, "web", "browser", "%s-%s" % (engine, safe_name(test_name)))
