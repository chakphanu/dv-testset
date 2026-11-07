# SPDX-License-Identifier: MIT
"""Web unit, CSP1 probe: EVIDENCE for a product finding, in real browsers (Playwright Chromium, Firefox and Playwright WebKit, the
fixtures of cases/browser_fixtures.py). Needs `scripts/run.sh --profile web,browser` (docs/web-browser-cases.md, "CSP probe").

The finding: Playwright WebKit reports `style-src-attr` (inline) CSP violations on the product's pages although the web rules
forbid inline styles, and DASH1 (which removed every `style=` of web/site) did not remove them. The questions this probe answers
for the product team, per page and per engine:

  1. Which directive governs a style attribute here (the CSP header actually served: style-src-attr, else style-src, else
     default-src) and does it allow inline?
  2. Which exact violation events fire (violatedDirective, effectiveDirective, blockedURI, sourceFile:line:col, sample, disposition,
     originalPolicy, the event target)?
  3. Which script wrote the inline style that was reported: the probe wraps setAttribute('style'), the `style` property setter,
     CSSStyleDeclaration.cssText / property setters / setProperty (with the owning element) and markup with `style=`, and keeps the
     element (tag, id, class, 3 parents), the value and the first 8 stack frames of every write
     (the probe is cases/csp_probe_support.py PROBE_JS, installed with add_init_script BEFORE the page's scripts)?
  4. Which elements carry a `style` attribute in the DOM when a violation fires and at the end?
  5. Do Chromium and Firefox report anything at all?

The cases NEVER fail on a violation (it is evidence). They fail only when a page that must load cannot be loaded (no response, a
5xx, a navigation error) or when the probe is missing from a document (the evidence would be void). Every distinct
(page, engine, directive, sourceFile:line:col) tuple becomes one web.finding line, and each page one compact `[RECORD] csp-probe`
line on stdout. The full record is out/<run>/web/csp-probe-<engine>.json (one file per engine, rewritten after each case).

  test_csp_probe_anonymous   signed out: login, register, totp, the eight dashboard routes (#/ #/map #/positions #/heard #/modules
                             #/nodes #/peers #/about; on the Map a room tile is clicked, on #/positions the room list is read),
                             /ops/ (the denial view) and, when the admin host is reachable, its sign-in screen
  test_csp_probe_user        a signed-in user: the dashboard routes (the Listen panel and the message composer render), the signed-in
                             login page and /recordings/ (optional: needs a recorder)
  test_csp_probe_operator    a signed-in operator: the six /ops/ pages and the signed-in login page

Run (about 6 to 10 minutes): docs/web-browser-cases.md, "CSP probe". NOT RUN on a rig yet; written against the page sources of
dv-router:wip-1bf347c (no `style=` and no `.style` write in web/site apart from MapLibre's own canvas sizing; the vendored
htm-preact module writes `el.style.cssText = ...` for a `style` prop, which is the first suspect the stack traces will confirm or clear).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import csp_probe_support as cps  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import browser as brlib, web  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.browser]

LOAD_MS = 20000
SETTLE_MS = 1500            # after load: the page renders, the first state arrives, the stack of widgets is drawn
LIVE_MS = 6000              # wait for the live-update indicator; not reaching it is recorded, never a failure
RESULTS = {}                # engine -> the JSON document of csp-probe-<engine>.json
_FINDINGS = set()           # (page, engine, directive, source) tuples already written as a finding


# ---------------------------------------------------------------- output

def out_file(engine):
    return Path(os.environ.get("DVT_OUT", "out")) / "web" / ("csp-probe-%s.json" % engine)


def document(bp):
    """The JSON document of one engine (created on first use)."""
    doc = RESULTS.get(bp.engine)
    if doc is None:
        try:
            version = bp.context.browser.version
        except Exception:                         # noqa: BLE001 - the version is a label only
            version = "?"
        doc = RESULTS[bp.engine] = {"engine": bp.engine, "label": brlib.label(bp.engine), "browser_version": version,
                                    "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "pages": [], "summary": {}}
    return doc


def write_document(bp):
    doc = document(bp)
    doc["summary"] = cps.summarize(doc["pages"])
    doc["summary"]["pages"] = len(doc["pages"])
    doc["summary"]["pages_with_violation"] = sum(1 for p in doc["pages"] if p.get("violations"))
    path = out_file(bp.engine)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print("[RECORD] csp-probe: cannot write %s: %s" % (path, exc))


def record_line(label, rec):
    """One compact stdout line per page: status, governing directive, violations, writes, [style] elements."""
    dirs = {}
    for v in rec.get("violations", []):
        d = v.get("violatedDirective") or "?"
        dirs[d] = dirs.get(d, 0) + int(v.get("count", 1))
    ends = [d for d in rec.get("dumps", []) if d.get("reason") == "end"]
    print("[RECORD] csp-probe %s page=%s role=%s status=%s governs=%s inline_allowed=%s violations=%s distinct=%d writes=%d "
          "style_attr_at_end=%s consoleCSP=%d" % (
              label, rec["page"], rec["role"], rec.get("status"), rec.get("style_governor"), rec.get("inline_allowed"),
              json.dumps(dirs, sort_keys=True), len(rec.get("violations", [])), rec.get("counts", {}).get("writes", 0),
              ends[-1]["total"] if ends else "?", len(rec.get("console_csp", []))))


def findings_for(bp, rec):
    """One web.finding per distinct (page, engine, directive, sourceFile:line:col) tuple of the whole run."""
    for v in rec.get("violations", []):
        key = cps.violation_key(rec["page"], bp.engine, v)
        if key in _FINDINGS:
            continue
        _FINDINGS.add(key)
        writer = ""
        index = v.get("last_write")
        if index is not None and index < len(rec.get("writes", [])):
            w = rec["writes"][index]
            writer = "; last style write before it: %s %s on <%s id=%s class=%s> value %r at %s" % (
                w["kind"], w["prop"], (w.get("el") or {}).get("tag", "-"), (w.get("el") or {}).get("id", ""),
                (w.get("el") or {}).get("cls", ""), w["value"][:60], (w.get("stack") or ["-"])[0][:100])
        web.finding("%s: CSP probe, page %s: %s (effective %s) blocked %r at %s, disposition %s, target %s, count %s%s" % (
            brlib.label(bp.engine), rec["page"], key[2], v.get("effectiveDirective"), v.get("blockedURI"), key[3],
            v.get("disposition"), (v.get("target") or {}).get("tag", "-"), v.get("count", 1), writer))


# ---------------------------------------------------------------- one page visit

class Capture:
    """The request audit of a context: every document response in order, with the CSP headers it carried (frames included)."""

    def __init__(self, context):
        self.documents = []
        context.on("response", self._on_response)

    def _on_response(self, resp):
        try:
            if resp.request.resource_type != "document":
                return
            self.documents.append({"url": resp.url.split("#")[0].split("?")[0], "status": resp.status,
                                   "csp": resp.headers.get("content-security-policy", ""),
                                   "csp_report_only": resp.headers.get("content-security-policy-report-only", "")})
        except Exception:                          # noqa: BLE001 - the audit never breaks a visit
            pass


def settle(bp):
    bp.page.wait_for_timeout(SETTLE_MS)


def wait_heading(bp):
    try:
        expect(pages.heading(bp.page)).to_be_visible(timeout=LOAD_MS)
        return True
    except AssertionError:
        return False


def wait_live(bp):
    """True when the live-update indicator says connected; the probe goes on either way."""
    try:
        expect(pages.connection_state(bp.page)).to_have_attribute("data-state", "connected", timeout=LIVE_MS)
        return True
    except AssertionError:
        return False


def act_dashboard(bp):
    wait_heading(bp)
    wait_live(bp)


def act_map(bp):
    """The Map room page: wait for the building, click the first room tile (selection draws extra shapes), click it again."""
    wait_heading(bp)
    wait_live(bp)
    try:
        expect(pages.map_building(bp.page)).to_be_visible(timeout=LOAD_MS)
        tile = pages.map_rooms(bp.page).first
        tile.click(timeout=5000)
        bp.page.wait_for_timeout(500)
        tile.click(timeout=5000)
    except Exception as exc:                       # noqa: BLE001 - a missing tile is a note of the page record
        return "map interaction: %s" % str(exc).splitlines()[0][:120]
    return ""


def act_positions(bp):
    """#/positions: wait for the section and the room list; the map needs a basemap that the rig does not install."""
    wait_heading(bp)
    wait_live(bp)
    try:
        expect(pages.positions_page(bp.page)).to_be_visible(timeout=LOAD_MS)
    except AssertionError:
        return "positions section not visible"
    return ""


def act_heading_only(bp):
    wait_heading(bp)
    return ""


def visit(bp, cap, name, role, url, act, required=True):
    """Load `url` in a fresh document (through about:blank so that a hash-only change is a real load), run `act`, collect the
    probe, build the page record. Returns (record, load_error or None)."""
    page = bp.page
    rec = {"page": name, "role": role, "url": url.split("?")[0], "required": required, "status": None, "csp_header": "",
           "csp_report_only": "", "style_governor": None, "style_governor_sources": [], "inline_allowed": None, "probe_installed": False,
           "note": "", "load_error": None}
    console_from = len(bp.log.console)
    docs_from = len(cap.documents)
    try:
        page.goto("about:blank")
        resp = page.goto(url, wait_until="load", timeout=LOAD_MS)
        if resp is None:
            rec["load_error"] = "no response object for %s" % url
        else:
            rec["status"] = resp.status
            headers = resp.headers
            rec["csp_header"] = headers.get("content-security-policy", "")
            rec["csp_report_only"] = headers.get("content-security-policy-report-only", "")
            if resp.status >= 500:
                rec["load_error"] = "HTTP %d" % resp.status
        note = act(bp) if rec["load_error"] is None else ""
        rec["note"] = note or ""
        settle(bp)
    except Exception as exc:                       # noqa: BLE001 - recorded, judged by the case at the end
        rec["load_error"] = "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0][:200] if str(exc) else "")
    name_, sources = cps.style_governor(rec["csp_header"])
    rec["style_governor"] = name_
    rec["style_governor_sources"] = sources
    rec["inline_allowed"] = cps.attr_allowed_inline(rec["csp_header"]) if rec["csp_header"] else None
    probe = None
    if rec["load_error"] is None or page.url.startswith(("http://", "https://")):
        try:
            probe = page.evaluate("() => (window.__dvtCspFinish ? window.__dvtCspFinish() : null)")
        except Exception as exc:                   # noqa: BLE001
            rec["note"] = (rec["note"] + " probe read failed: %s" % str(exc).splitlines()[0][:120]).strip()
    if probe:
        rec["probe_installed"] = True
        rec.update({"counts": probe["counts"], "violations": probe["violations"], "writes": probe["writes"], "dumps": probe["dumps"],
                    "probe_errors": probe["errors"], "ua": probe["ua"]})
    else:
        rec.update({"counts": {}, "violations": [], "writes": [], "dumps": [], "probe_errors": []})
    rec["document_responses"] = cap.documents[docs_from:][:10]
    rec["console_csp"] = [c["text"][:240] for c in bp.log.console[console_from:]
                          if any(k in c["text"] for k in ("Content Security Policy", "Content-Security-Policy", "Refused to", "violates the following"))][:20]
    rec["csp_events_seen_by_fixture"] = [{"directive": v.get("directive"), "blocked": v.get("blocked"), "source": v.get("source")}
                                         for v in bp.log.csp][:50]
    bp.log.csp.clear()
    return rec


def run_pages(bp, role, plan):
    """Visits every (name, url, act, required) of `plan`, records and returns the list of load errors of required pages."""
    cap = Capture(bp.context)
    doc = document(bp)
    errors = []
    for name, url, act, required in plan:
        rec = visit(bp, cap, name, role, url, act, required)
        doc["pages"].append(rec)
        record_line(brlib.label(bp.engine), rec)
        findings_for(bp, rec)
        if required and rec["load_error"]:
            errors.append("%s %s: %s" % (name, rec["url"], rec["load_error"]))
        elif required and rec["status"] not in (200, 304):
            errors.append("%s %s: HTTP %s" % (name, rec["url"], rec["status"]))
        elif not rec["probe_installed"]:
            errors.append("%s %s: the probe is not in the document (window.__dvtCspFinish is missing): the evidence is void" % (name, rec["url"]))
        write_document(bp)
    return errors


def dashboard_plan(bp, tag=""):
    plan = []
    for route in pages.DASHBOARD_ROUTES:
        name = ("dashboard" if route == "/" else "route-" + route.strip("/")) + tag
        act = act_map if route == "/map" else act_positions if route == "/positions" else act_dashboard
        plan.append((name, bp.url(pages.dashboard_url(route)), act, True))
    return plan


def conclude(bp, errors):
    """The per-engine statement the report needs, printed once per case; then the load failures (the only failure)."""
    doc = document(bp)
    summ = doc["summary"]
    print("[RECORD] csp-probe %s so far: %d page(s), any violation: %s, events by directive: %s, distinct (directive, source): %d" % (
        brlib.label(bp.engine), len(doc["pages"]), summ.get("any_violation"), json.dumps(summ.get("events_by_directive", {}), sort_keys=True),
        len(summ.get("distinct", []))))
    for directive, source in summ.get("distinct", [])[:20]:
        print("[RECORD] csp-probe %s distinct: %s at %s" % (brlib.label(bp.engine), directive, source))
    if bp.engine in ("chromium", "firefox") and not summ.get("any_violation"):
        print("[RECORD] csp-probe %s: NO CSP violation fired on any of the %d page(s) so far" % (brlib.label(bp.engine), len(doc["pages"])))
    assert not errors, "pages that could not be loaded (the only failure of the probe): %s" % "; ".join(errors)


# ================================================================ the cases

def test_csp_probe_anonymous(browser_page, request):
    """Signed out: login, register, totp, every dashboard route, /ops/ (the denial view), the admin sign-in screen when reachable."""
    bp = browser_page
    bp.context.add_init_script(cps.PROBE_JS)                 # before the first navigation: runs before the page's scripts
    plan = [("login", bp.url(pages.LOGIN), act_heading_only, True),
            ("register", bp.url(pages.REGISTER), act_heading_only, True),
            ("totp", bp.url(pages.TOTP), act_heading_only, False)]
    plan += dashboard_plan(bp)
    plan.append(("ops-anonymous", bp.url(pages.ops_url("accounts")), act_heading_only, True))
    try:
        admin = request.getfixturevalue("web_url_admin")
    except pytest.skip.Exception:
        admin = None
        print("[RECORD] csp-probe %s: the admin host is not reachable here: its sign-in screen is not probed" % brlib.label(bp.engine))
    if admin:
        plan.append(("admin-sign-in", admin.rstrip("/") + "/", act_heading_only, False))
    conclude(bp, run_pages(bp, "anonymous", plan))


def test_csp_probe_user(user_page):
    """A signed-in user (the Listen panel, the message composer and the account menu render)."""
    bp = user_page
    bp.context.add_init_script(cps.PROBE_JS)
    plan = dashboard_plan(bp, "-user")
    plan.append(("login-signed-in", bp.url(pages.LOGIN), act_heading_only, True))
    plan.append(("recordings", bp.url(pages.RECORDINGS), act_heading_only, False))     # needs a recorder in the SUT
    plan.append(("ops-user", bp.url(pages.ops_url("accounts")), act_heading_only, True))    # a user gets the denial view
    conclude(bp, run_pages(bp, "user", plan))


def test_csp_probe_operator(operator_page):
    """A signed-in operator: the six operator pages."""
    bp = operator_page
    bp.context.add_init_script(cps.PROBE_JS)
    plan = [("ops-" + name, bp.url(pages.ops_url(name)), act_heading_only, True) for name in pages.OPS_PAGES]
    plan.append(("login-operator", bp.url(pages.LOGIN), act_heading_only, True))
    plan.append(("dashboard-operator", bp.url(pages.dashboard_url("/")), act_dashboard, True))
    conclude(bp, run_pages(bp, "operator", plan))
