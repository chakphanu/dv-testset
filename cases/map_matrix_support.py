# SPDX-License-Identifier: MIT
"""Helpers of the TWIN1d Map cases (cases/test_web_map_twin1d.py): the in-page measurement and the PURE judges of the Thai screenshot
matrix, and the in-page recorder and the PURE judge of the polite `map-status` region. Not a test module; no Playwright import, so the
judges run in the unit tests (cases/unit/test_map_matrix_support.py).

The matrix (scenario x language x colour scheme x viewport width) takes one measurement per combination (MEASURE_JS, one
page.evaluate) and judges it (judge_measure). The three checks, defined here once:

  no horizontal overflow   document.scrollingElement.scrollWidth <= clientWidth (the page does not scroll sideways).
  no clipped caption       (a) every visible HTML text element under <main> and the page heading block that is NOT display:inline
                           has scrollWidth <= clientWidth + CLIP_TOL_PX (its text fits its box: nothing is cut off, no ellipsis
                           truncation, no overflow into a neighbour), and no visible HTML element sticks out of the viewport
                           (right > innerWidth + CLIP_TOL_PX or left < -CLIP_TOL_PX);
                           (b) every visible SVG <text> of the building lies inside the shape it belongs to (the room, desk,
                           service box, roof sign, or for the closed desk sign the sign's own rectangle) with CLIP_TOL_PX of
                           slack, and inside the SVG itself. "Visible" = displayed, not visibility:hidden, opacity > 0 and
                           larger than 1.5 px, so the sr-only status region and fully faded elements are not measured.
  closed desk sign         in TH the text of the sign of a closed desk is the Thai string of the key "Closed" and not the English
                           one; in EN it is "Closed". Judged only when the measurement saw a closed desk; when the case forced one
                           (closed_required) a missing closed desk is itself a failure.

Not a screenshot comparison: the screenshots (out/<run>/web/map-matrix/<engine>/<scenario>-<lang>-<scheme>-<width>.png) are local
evidence for a human to look at. The DOM facts are SOURCE-DERIVED from dv-router:wip-efd9eca and NOT verified on a running page.
"""
import re

import web_pages as pages

CLIP_TOL_PX = 1.0                      # slack of every clip comparison (sub-pixel layout, rounding of scrollWidth)
THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
SCENARIOS = ("map-busy", "map-quiet", "map26", "map-hub-down")
LANGS = ("en", "th")
SCHEMES = ("light", "dark")
WIDTHS = (360, 1000, 1440)

MEASURE_JS = r"""
(sel) => {
  const doc = document.scrollingElement || document.documentElement;
  const out = { doc_scroll: doc.scrollWidth, doc_client: doc.clientWidth, inner: window.innerWidth, lang: document.documentElement.lang,
    dark: !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches), items: [], svg_texts: [], closed_texts: [] };
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1.5 && r.height > 1.5;
  };
  const desc = (el) => {
    const id = el.getAttribute('data-testid') || el.id || (el.getAttribute('class') || '').split(/\s+/)[0] || '';
    return el.tagName.toLowerCase() + (id ? '[' + id + ']' : '') + ' "' + (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40) + '"';
  };
  const cache = new Map();
  const facts = (e) => {
    if (cache.has(e)) return cache.get(e);
    const c = getComputedStyle(e), r = e.getBoundingClientRect();
    const f = { clip: c.clip, clip_path: c.clipPath, w: r.width, h: r.height, position: c.position, overflow_x: c.overflowX, overflow_y: c.overflowY };
    cache.set(e, f);
    return f;
  };
  const seen = new Set();
  for (const scope of sel.scopes) {
    for (const root of document.querySelectorAll(scope)) {
      for (const el of root.querySelectorAll('*')) {
        if (seen.has(el) || el instanceof SVGElement) continue;
        seen.add(el);
        const own = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
        if (!own || !visible(el) || out.items.length >= 400) continue;
        const cs = getComputedStyle(el), r = el.getBoundingClientRect();
        // the style facts of the element and of every ancestor that could hide it: Python decides (visually_hidden)
        const chain = [];
        for (let a = el; a && a !== document.documentElement; a = a.parentElement) {
          const f = facts(a);
          if (f.clip !== 'auto' || (f.clip_path && f.clip_path !== 'none') || f.w <= 1 || f.h <= 1) chain.push(f);
        }
        out.items.push({ desc: desc(el), sw: el.scrollWidth, cw: el.clientWidth, display: cs.display, overflow_x: cs.overflowX,
          text_overflow: cs.textOverflow, left: r.left, right: r.right, hidden_chain: chain });
      }
    }
  }
  const svg = document.querySelector(sel.building);
  out.svg = null;
  if (svg) {
    const sr = svg.getBoundingClientRect();
    out.svg = { left: sr.left, right: sr.right };
    const boxes = [];
    for (const s of [sel.room_any, sel.desk_any, sel.service_any, sel.roof_sign]) {
      for (const e of document.querySelectorAll(s)) {
        const r = e.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) boxes.push({ l: r.left, r: r.right, t: r.top, b: r.bottom, d: desc(e).split(' "')[0] });
      }
    }
    for (const el of svg.querySelectorAll('text')) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect(), cx = (r.left + r.right) / 2, cy = (r.top + r.bottom) / 2;
      let best = null;
      const prev = el.previousElementSibling;
      if (prev && prev.tagName.toLowerCase() === 'rect' && el.classList.contains('twin-sign-text')) {
        const pr = prev.getBoundingClientRect();
        best = { l: pr.left, r: pr.right, t: pr.top, b: pr.bottom, d: 'closed sign' };
      }
      if (!best) {
        for (const b of boxes) {
          if (cx >= b.l && cx <= b.r && cy >= b.t && cy <= b.b && (!best || (b.r - b.l) * (b.b - b.t) < (best.r - best.l) * (best.b - best.t))) best = b;
        }
      }
      if (out.svg_texts.length < 400) out.svg_texts.push({ text: (el.textContent || '').trim().slice(0, 40), l: r.left, r: r.right, in: best });
    }
  }
  out.closed_texts = Array.from(document.querySelectorAll(sel.closed_text)).map((e) => (e.textContent || '').trim());
  out.closed_desks = document.querySelectorAll(sel.desk_down).length;
  out.desk_ids = Array.from(document.querySelectorAll(sel.desk_any)).map((e) => (e.getAttribute('data-id') || '') + ':' + (e.getAttribute('data-state') || ''));
  out.desks = document.querySelectorAll(sel.desk_any).length;
  out.rooms = document.querySelectorAll(sel.room_any).length;
  out.onair_rooms = Array.from(document.querySelectorAll(sel.room_any)).filter((r) => r.getAttribute('data-state') === 'onair').length;
  const cap = document.querySelector(sel.caption);
  out.caption = cap ? (cap.textContent || '').trim() : null;
  const pg = document.querySelector(sel.page);
  out.mode = pg ? pg.getAttribute('data-mode') : null;
  out.hub_down_callout = !!document.querySelector(sel.hub_down);
  out.status_present = !!document.querySelector(sel.status);
  out.viewBox = svg ? svg.getAttribute('viewBox') : null;
  return out;
}
"""


# ------------------------------------------------------------------ the matrix judge

def combo_id(scenario, lang, scheme, width):
    return "%s/%s/%s/%d" % (scenario, lang, scheme, width)


def shot_name(scenario, lang, scheme, width):
    """File name of the screenshot of one combination."""
    return "%s-%s-%s-%d.png" % (scenario, lang, scheme, width)


def all_combos(scenario):
    return [(scenario, lang, scheme, width) for lang in LANGS for scheme in SCHEMES for width in WIDTHS]


def has_thai(text):
    return bool(THAI_RE.search(text or ""))


CLIP_ZERO_RE = re.compile(r"^rect\(\s*0(px)?[,\s]+0(px)?[,\s]+0(px)?[,\s]+0(px)?\s*\)$")
CLIP_PATH_HIDE_RE = re.compile(r"^inset\(\s*(50|100)(\.0+)?%")


def visually_hidden(f):
    """True when the style facts `f` of ONE element (clip, clip_path, w, h in px, position, overflow_x, overflow_y) make it
    visually hidden the way a screen-reader-only class does: a clip of rect(0,0,0,0), a clip-path of inset(50%) or more, a box of at
    most 1 x 1 px, or an absolutely positioned box with hidden overflow that is at most 1 px wide or high. aria-hidden is NOT a reason
    (an aria-hidden element can be on screen)."""
    clip = (f.get("clip") or "").strip().lower()
    if CLIP_ZERO_RE.match(clip):
        return True
    if CLIP_PATH_HIDE_RE.match((f.get("clip_path") or "").strip().lower()):
        return True
    w, h = f.get("w"), f.get("h")
    if w is not None and h is not None and w <= 1.0 and h <= 1.0:
        return True
    clipping = (f.get("overflow_x") not in (None, "visible")) or (f.get("overflow_y") not in (None, "visible"))
    small = (w is not None and w <= 1.0) or (h is not None and h <= 1.0)
    return f.get("position") == "absolute" and clipping and small


def item_hidden(item):
    """True when the measured element, or an ancestor in its `hidden_chain`, is visually hidden: its text is not on screen."""
    return any(visually_hidden(f) for f in item.get("hidden_chain") or [])


def _html_failures(item, inner):
    if item_hidden(item):
        return []
    out = []
    display = item.get("display") or ""
    sw, cw = item.get("sw", 0), item.get("cw", 0)
    if display != "inline" and sw > cw + CLIP_TOL_PX:
        how = "clipped" if (item.get("overflow_x") not in (None, "visible") or item.get("text_overflow") == "ellipsis") else "overflows its box"
        out.append("caption text %s: %s scrollWidth %.0f > clientWidth %.0f" % (how, item.get("desc"), sw, cw))
    if item.get("right", 0) > inner + CLIP_TOL_PX or item.get("left", 0) < -CLIP_TOL_PX:
        out.append("text element outside the viewport: %s spans %.0f..%.0f of %.0f px" % (item.get("desc"), item.get("left", 0), item.get("right", 0), inner))
    return out


def _svg_failures(entry, svg):
    out = []
    box = entry.get("in")
    if box is not None and (entry["l"] < box["l"] - CLIP_TOL_PX or entry["r"] > box["r"] + CLIP_TOL_PX):
        out.append("SVG text %r spans %.0f..%.0f but its %s spans %.0f..%.0f" % (entry.get("text"), entry["l"], entry["r"], box.get("d"), box["l"], box["r"]))
    if svg is not None and (entry["l"] < svg["left"] - CLIP_TOL_PX or entry["r"] > svg["right"] + CLIP_TOL_PX):
        out.append("SVG text %r spans %.0f..%.0f outside the drawing %.0f..%.0f" % (entry.get("text"), entry["l"], entry["r"], svg["left"], svg["right"]))
    return out


def judge_measure(m, lang, scheme, closed_required=False):
    """(failures, notes) of one measurement `m` (MEASURE_JS) in language `lang`, colour scheme `scheme`. `closed_required`: the case
    forced a closed desk, so seeing none is a failure."""
    failures, notes = [], []
    if m.get("doc_scroll", 0) > m.get("doc_client", 0):
        failures.append("horizontal overflow: scrollWidth %s > clientWidth %s" % (m.get("doc_scroll"), m.get("doc_client")))
    for item in m.get("items") or []:
        failures += _html_failures(item, m.get("inner", 0))
    for entry in m.get("svg_texts") or []:
        failures += _svg_failures(entry, m.get("svg"))
    if (m.get("dark") is True) != (scheme == "dark"):
        failures.append("colour scheme %s was not applied (prefers-color-scheme dark is %s)" % (scheme, m.get("dark")))
    if (m.get("lang") or "") != lang:
        failures.append("document language is %r, wanted %r" % (m.get("lang"), lang))
    caption = m.get("caption")
    if caption is None:
        failures.append("no map caption on the page")
    elif lang == "th" and not has_thai(caption):
        failures.append("TH: the caption has no Thai text: %r" % caption[:80])
    elif lang == "en" and has_thai(caption):
        failures.append("EN: the caption has Thai text: %r" % caption[:80])
    want = pages.CLOSED_SIGN_TEXT[lang]
    other = pages.CLOSED_SIGN_TEXT["en" if lang == "th" else "th"]
    signs = [s for s in (m.get("closed_texts") or []) if s]
    if m.get("closed_desks"):
        if not signs:
            failures.append("%d closed desk(s) but no closed sign text in the drawing" % m["closed_desks"])
        for sign in signs:
            if sign != want:
                failures.append("%s: the closed desk sign says %r, wanted %r (the %s string is %r)" % (lang.upper(), sign, want, "EN" if lang == "th" else "TH", other))
    elif closed_required:
        failures.append("no closed desk in the drawing although the case removed an edge from live.edges: the closed sign was not checked")
    else:
        notes.append("no closed desk: the closed sign was not checked")
    return failures, notes


# ------------------------------------------------------------------ a closed desk on demand

EDGE_SUFFIX_RE = re.compile(r"-[0-9a-f]{16}$")        # map-layout.js: an instance suffix is cut off a live.edges name
NOT_CLOSABLE = ("usrp", "webrtc")                     # the talkers of the cases and the page's own Listen edge stay open


def close_one_edge(state, chosen=None):
    """(state, protocol): `state` (the parsed public state) with every entry of ONE enabled edge removed from live.edges, so the Map
    draws that desk closed (map-layout.js: a desk is `down` when its protocol is not in live.edges). The edge is `chosen` when
    given and still present, else the last enabled protocol that is up and not in NOT_CLOSABLE. (state, None) when the state has
    no such edge (the document is returned unchanged). The state is edited in place."""
    live = state.get("live") if isinstance(state, dict) else None
    info = state.get("info") if isinstance(state, dict) else None
    if not isinstance(live, dict) or not isinstance(info, dict) or not isinstance(live.get("edges"), list):
        return state, None
    up = {EDGE_SUFFIX_RE.sub("", e) for e in live["edges"] if isinstance(e, str)}
    enabled = [e for e in (info.get("edges_enabled") or []) if isinstance(e, str)]
    candidates = [e for e in enabled if e in up and not e.startswith(NOT_CLOSABLE)]
    pick = chosen if chosen in up else (candidates[-1] if candidates else None)
    if pick is None:
        return state, None
    live["edges"] = [e for e in live["edges"] if not (isinstance(e, str) and EDGE_SUFFIX_RE.sub("", e) == pick)]
    if isinstance(live.get("edges_up"), int):
        live["edges_up"] = len(live["edges"])
    return state, pick


# ------------------------------------------------------------------ page errors that a navigation of the case itself causes

NAV_ERROR_WINDOW_S = 1.0
# Playwright WebKit: a fetch that the page still had in flight when the case reloaded or navigated it fails with this message
TOLERATED_NAV_ERROR_RE = re.compile(r"Fetch API cannot load \S*/api/v1/auth/session due to access control checks")


def split_tolerated_errors(errors, navs, engine, window_s=NAV_ERROR_WINDOW_S):
    """(unexpected, tolerated) of a list of page errors `(t, text)` (t in seconds on the clock of `navs`, the times at which the
    case started a navigation or reload). EXACTLY one message is tolerated, only on Playwright WebKit, and only when it falls within
    `window_s` of a navigation the case started; everything else stays unexpected. `tolerated` are `(t, text, seconds from the
    nearest navigation)` for the notes."""
    unexpected, tolerated = [], []
    for t, text in errors:
        near = min((abs(t - n) for n in navs), default=None)
        if engine == "webkit" and near is not None and near <= window_s and TOLERATED_NAV_ERROR_RE.search(text or ""):
            tolerated.append((t, text, round(near, 2)))
        else:
            unexpected.append((t, text))
    return unexpected, tolerated


# ------------------------------------------------------------------ the stopped edge: is its desk drawn closed?

def edge_lists(state):
    """{'edges_enabled': [...], 'live_edges': [...]} of the parsed public state (None for a missing list)."""
    info = state.get("info") if isinstance(state, dict) else None
    live = state.get("live") if isinstance(state, dict) else None
    return {"edges_enabled": info.get("edges_enabled") if isinstance(info, dict) else None,
            "live_edges": live.get("edges") if isinstance(live, dict) else None}


def closed_desk_finding(protocol, ids_before, ids_after, lists_after):
    """None when the desk of `protocol` is drawn closed (a `<protocol>:down` entry in `ids_after`), else the text of the FINDING for the
    product owner. `ids_*` are the `data-id:data-state` entries of the drawn desks, `lists_after` is edge_lists() of the public state
    at the time of the check (enabled but not in live.edges is the spec, web.md Map)."""
    if "%s:down" % protocol in (ids_after or []):
        return None
    drawn = [i.split(":")[0] for i in ids_after or []]
    if protocol in drawn:
        state = [i.split(":", 1)[1] for i in ids_after if i.split(":")[0] == protocol][0]
        what = "the desk of the stopped edge %s is drawn but not closed (data-state %s)" % (protocol, state)
    else:
        what = "the desk of the stopped edge %s is not drawn (desks %d -> %d)" % (protocol, len(ids_before or []), len(ids_after or []))
    return ("FINDING: %s, want it drawn with the Closed sign (web.md: enabled but not in live.edges); info.edges_enabled=%s live.edges=%s desks before=%s after=%s" % (
        what, lists_after.get("edges_enabled"), lists_after.get("live_edges"), list(ids_before or []), list(ids_after or [])))


# ------------------------------------------------------------------ SIGSTOP / SIGCONT of the edge and the hub (control channel)

CLOSED_SCENARIO = "map-closed-desk"                  # the quiet Map with the xlx edge stopped: the one place the Closed sign is judged
EDGE_PROCESS, EDGE_PROTOCOL = "dv-edge-xlx", "xlx"   # SIGSTOP of this process: the hub drops the edge, the desk is closed (web.md, Map)
HUB_PROCESS = "dv-svc-status"                        # SIGSTOP of this process: the public state is unavailable, the Map shows the banner
STOPPABLE = (EDGE_PROCESS, HUB_PROCESS)
SERVICE = "xlxd-sut"


def edge_listed(state, protocol):
    """True when `protocol` is in live.edges of the parsed public state (an instance suffix -<16 hex> is cut off, as the page does)."""
    live = state.get("live") if isinstance(state, dict) else None
    edges = live.get("edges") if isinstance(live, dict) else None
    return isinstance(edges, list) and any(isinstance(e, str) and EDGE_SUFFIX_RE.sub("", e) == protocol for e in edges)


def poll_until(pred, timeout_s, every_s=2.0, clock=None, sleep=None):
    """Seconds until pred() is truthy (checked at once, then every `every_s`), or None after `timeout_s`. A pred that raises counts
    as false."""
    import time
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    t0 = clock()
    while True:
        try:
            if pred():
                return round(clock() - t0, 2)
        except Exception:                           # noqa: BLE001 - a dead hub or a closing page is "not yet"
            pass
        if clock() - t0 >= timeout_s:
            return None
        sleep(every_s)


def cont_all(rig, rounds=2):
    """SIGCONT to every stoppable process, `rounds` times (CONT is idempotent; the second round covers a first one that met a
    respawn or a timeout). Never raises. Returns the number of CONT requests that were acknowledged."""
    acked = 0
    for _ in range(rounds):
        for proc in STOPPABLE:
            try:
                rig.kill_proc(SERVICE, proc, "CONT")
                acked += 1
            except Exception:                       # noqa: BLE001 - error 3 (no such process), a timeout, a refused request: go on
                pass
    return acked


# ------------------------------------------------------------------ the map-status recorder and judge

STATUS_INIT_JS = r"""
(sel) => {
  if (window.__dvtStatus) return;
  const S = window.__dvtStatus = { attaches: [], changes: [], rewrites: [], batches: 0 };
  let target = null, obs = null;
  const read = () => (target ? (target.textContent || '') : '');
  const attach = (el) => {
    if (obs) obs.disconnect();
    target = el;
    S.attaches.push({ t: Date.now(), text: read() });
    S.last = read();
    obs = new MutationObserver(() => {
      S.batches += 1;
      const now = read(), t = Date.now();
      if (now !== S.last) { S.changes.push({ t: t, from: S.last, text: now }); S.last = now; }
      else S.rewrites.push({ t: t, text: now });
    });
    obs.observe(el, { childList: true, characterData: true, subtree: true });
  };
  const scan = () => { const el = document.querySelector(sel); if (el && el !== target) attach(el); };
  new MutationObserver(scan).observe(document, { childList: true, subtree: true });
  scan();
}
"""

STATUS_FACTS_JS = r"""
(sel) => {
  const els = document.querySelectorAll(sel);
  const el = els[0];
  if (!el) return { found: false, count: 0 };
  let hidden = false;
  for (let n = el; n; n = n.parentElement) {
    const c = getComputedStyle(n);
    if (n.getAttribute('aria-hidden') === 'true' || n.hidden || c.display === 'none' || c.visibility === 'hidden') hidden = true;
  }
  const cs = getComputedStyle(el), r = el.getBoundingClientRect();
  return { found: true, count: els.length, role: el.getAttribute('role'), live: el.getAttribute('aria-live'), atomic: el.getAttribute('aria-atomic'),
    display: cs.display, visibility: cs.visibility, position: cs.position, cls: (el.getAttribute('class') || '').split(/\s+/).filter(Boolean),
    w: r.width, h: r.height, hidden_chain: hidden, text: el.textContent || '' };
}
"""

START_WINDOW_S = 3.5       # a start announcement lands within this long after the first voice packet (latency, 250 ms gate, 2 s limit)
END_BEFORE_S = 0.3         # an end announcement may land this much before the unkey was sent (clock skew)
END_WINDOW_S = 4.0         # ... and up to this long after it
MIN_GAP_S = 1.9            # the spec's "at most once per 2 s", with timer jitter


def judge_status_facts(facts):
    """Failures of the region's static facts: one region, role=status, aria-live=polite, in the accessibility tree, sr-only."""
    if not facts.get("found"):
        return ["the region data-testid=%s is not in the DOM" % pages.MAP_STATUS_TESTID]
    out = []
    if facts.get("count") != 1:
        out.append("%d elements match data-testid=%s, wanted exactly 1" % (facts.get("count", 0), pages.MAP_STATUS_TESTID))
    if facts.get("role") != "status":
        out.append("role is %r, wanted 'status'" % facts.get("role"))
    if facts.get("live") != "polite":
        out.append("aria-live is %r, wanted 'polite'" % facts.get("live"))
    if facts.get("display") == "none" or facts.get("visibility") == "hidden" or facts.get("hidden_chain"):
        out.append("the region is out of the accessibility tree (display %s, visibility %s, hidden ancestor/aria-hidden %s)" % (
            facts.get("display"), facts.get("visibility"), facts.get("hidden_chain")))
    if pages.MAP_STATUS_CLASS not in (facts.get("cls") or []):
        out.append("class list %s lacks %s" % (facts.get("cls"), pages.MAP_STATUS_CLASS))
    return out


def judge_status_log(log, t_start_ms, t_unkey_ms, t_end_ms):
    """(failures, notes, metrics) of the recorded changes of the region (STATUS_INIT_JS) around ONE over: voice from `t_start_ms` to
    `t_unkey_ms`, observation until `t_end_ms` (the fade is over), all on the browser clock (Date.now()).

      first render / idle   the first sight of the region is empty and nothing changes before the over starts
      over start            exactly one change in [t_start, t_start + START_WINDOW_S]
      steady voice          no change from there until END_BEFORE_S before the unkey
      over end              exactly one change in [t_unkey - END_BEFORE_S, t_unkey + END_WINDOW_S]
      fade and after        no change until t_end
      rate                  consecutive changes at least MIN_GAP_S apart
    Writes of the same text (rewrites) are notes, not failures: a screen reader may announce them again."""
    failures, notes = [], []
    attaches = log.get("attaches") or []
    changes = sorted(log.get("changes") or [], key=lambda c: c["t"])
    if not attaches:
        return ["the region never appeared in the DOM"], notes, {}
    if attaches[0].get("text"):
        failures.append("the region was not empty on first render: %r" % attaches[0]["text"][:80])
    if len(attaches) > 1:
        notes.append("the region element was replaced %d time(s) (re-attached); texts %s" % (len(attaches) - 1, [a.get("text", "")[:30] for a in attaches[1:]]))
        for a in attaches[1:]:
            if a.get("text") and t_start_ms - 500 > a["t"]:
                failures.append("the region was re-created with text %r before the over" % a["text"][:80])
    s = lambda ms: (ms - t_start_ms) / 1000.0           # noqa: E731 - seconds since the first voice packet
    unkey = s(t_unkey_ms)
    before = [c for c in changes if s(c["t"]) < -0.2]
    start = [c for c in changes if -0.2 <= s(c["t"]) <= START_WINDOW_S]
    steady = [c for c in changes if START_WINDOW_S < s(c["t"]) < unkey - END_BEFORE_S]
    end = [c for c in changes if unkey - END_BEFORE_S <= s(c["t"]) <= unkey + END_WINDOW_S]
    after = [c for c in changes if s(c["t"]) > unkey + END_WINDOW_S and c["t"] <= t_end_ms]
    # a start window that reaches into the end window (an over shorter than the windows) is a case bug, not a product result
    if unkey - END_BEFORE_S <= START_WINDOW_S:
        failures.append("case error: the over (%.1f s) is too short for the start and end windows" % unkey)
    if before:
        failures.append("%d change(s) before the over started (first %.1f s before): %r" % (len(before), -s(before[0]["t"]), before[0]["text"][:60]))
    if len(start) != 1:
        failures.append("%d change(s) of the region at the over start (wanted exactly 1 within %.1f s): %s" % (len(start), START_WINDOW_S, [round(s(c["t"]), 2) for c in start]))
    elif not start[0]["text"].strip():
        failures.append("the over-start change emptied the region (nothing announced)")
    if steady:
        failures.append("%d change(s) during steady voice (wanted none): %s" % (len(steady), [(round(s(c["t"]), 2), c["text"][:40]) for c in steady]))
    if len(end) != 1:
        failures.append("%d change(s) of the region at the over end (wanted exactly 1 within %.1f s after the unkey): %s" % (len(end), END_WINDOW_S, [round(s(c["t"]) - unkey, 2) for c in end]))
    elif not end[0]["text"].strip():
        failures.append("the over-end change emptied the region (nothing announced)")
    if after:
        failures.append("%d change(s) after the end announcement during the fade and the rest (wanted none): %s" % (len(after), [(round(s(c["t"]) - unkey, 2), c["text"][:40]) for c in after]))
    for a, b in zip(changes, changes[1:]):
        if (b["t"] - a["t"]) / 1000.0 < MIN_GAP_S:
            failures.append("two changes %.2f s apart (the limit is one per 2 s): %r then %r" % ((b["t"] - a["t"]) / 1000.0, a["text"][:30], b["text"][:30]))
    if len(start) == 1 and len(end) == 1:
        if start[0]["text"] == end[0]["text"]:
            notes.append("the end announcement has the same text as the start announcement: %r" % end[0]["text"][:60])
    rewrites = [w for w in (log.get("rewrites") or [])]
    if rewrites:
        notes.append("%d write(s) of the same text (no change of textContent): a screen reader may repeat it" % len(rewrites))
    metrics = {"changes": [{"t_s": round(s(c["t"]), 2), "text": c["text"][:80]} for c in changes], "batches": log.get("batches"),
               "rewrites": len(rewrites), "attaches": len(attaches)}
    return failures, notes, metrics
