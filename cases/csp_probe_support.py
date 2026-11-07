# SPDX-License-Identifier: MIT
"""Helpers of the CSP probe (cases/test_web_csp_probe.py): the in-page recorder and the pure functions that read its output.

PROBE_JS is installed with context.add_init_script, so it runs BEFORE any script of the page, in every document the context loads.
It records, inside the page:

  violations   every `securitypolicyviolation` event on `document` (capture): violatedDirective, effectiveDirective, blockedURI,
               sourceFile, lineNumber, columnNumber, sample, disposition, originalPolicy, the event target (tag, id, class, 3
               parents) and the index of the last style write seen before it; repeated events with the same key only count up
  writes       every write of an inline style that the page makes through a path the probe can wrap: Element.setAttribute /
               setAttributeNS / setAttributeNode(NS) with the name `style`; the `style` property setter (`el.style = 'a:b'`);
               every CSSStyleDeclaration setter (cssText and each property) and setProperty, with the OWNING element when the
               declaration came from `el.style`; markup with `style=` handed to innerHTML / outerHTML / insertAdjacentHTML /
               createContextualFragment / DOMParser; and a MutationObserver on the `style` attribute (it also sees writes the
               wrappers cannot, but has no stack). Each write: kind, element tag / id / class, 3 parents, the value, the first 8 stack
               frames (the probe's own frames dropped)
  dumps        document.querySelectorAll('[style]') (tag, id, class, style attribute, outerHTML first 200 characters) at the first
               occurrence of each distinct violation and once more at the end (`window.__dvtCspFinish()`)

Nothing here changes what the page does: every wrapper calls the original. The probe is a measurement of someone else's product;
a violation is evidence, never a failure of the case.
"""
from urllib.parse import urlsplit

from dvtest import browser as brlib

PROBE_JS = r"""
(() => {
  if (window.__dvtCsp) return;
  const MAX_WRITES = 300, MAX_VIOLATIONS = 200, MAX_DUMPS = 40, MAX_ELEMENTS = 60;
  const t0 = performance.now();
  const rec = { violations: [], writes: [], dumps: [], errors: [],
                counts: { writes: 0, violations: 0, dropped_writes: 0, dropped_violations: 0 } };
  window.__dvtCsp = rec;
  const now = () => Math.round((performance.now() - t0) * 10) / 10;
  const clip = (s, n) => String(s === undefined || s === null ? '' : s).slice(0, n);
  const classOf = (el) => { try { return clip((el.getAttribute && el.getAttribute('class')) || '', 120); } catch (e) { return ''; } };
  const brief = (el) => (el && el.nodeType === 1) ? { tag: el.localName, id: clip(el.id || '', 60), cls: classOf(el) } : null;
  const chain = (el) => {
    const out = [];
    let p = el && el.parentNode;
    for (let i = 0; i < 3 && p; i++) { out.push(p.nodeType === 1 ? brief(p) : { tag: '#' + String(p.nodeName || 'node').toLowerCase() }); p = p.parentNode; }
    return out;
  };
  const frames = () => {
    let s = '';
    try { s = new Error().stack || ''; } catch (e) { s = ''; }
    const lines = s.split('\n').map((x) => x.trim()).filter(Boolean);
    if (lines.length && /^Error/.test(lines[0])) lines.shift();
    lines.shift(); lines.shift();            // frames() itself and record()
    lines.shift();                            // the wrapper
    return lines.slice(0, 8).map((x) => clip(x, 220));
  };
  const seen = new Map();
  const record = (kind, el, prop, value, withStack) => {
    try {
      rec.counts.writes++;
      const b = brief(el);
      const f = withStack === false ? [] : frames();
      const key = [kind, prop, clip(value, 200), b ? b.tag + '#' + b.id + '.' + b.cls : '-', f[0] || ''].join('|');
      const old = seen.get(key);
      if (old) { old.count++; return; }
      if (rec.writes.length >= MAX_WRITES) { rec.counts.dropped_writes++; return; }
      const w = { n: rec.writes.length, t: now(), kind, prop, value: clip(value, 300), el: b, parents: chain(el),
                  connected: !!(el && el.isConnected), stack: f, count: 1 };
      rec.writes.push(w);
      seen.set(key, w);
    } catch (e) { rec.errors.push('record: ' + clip(e, 120)); }
  };
  const wrapMethod = (proto, name, make) => {
    try {
      if (!proto) return;
      const d = Object.getOwnPropertyDescriptor(proto, name);
      if (!d || typeof d.value !== 'function') return;
      Object.defineProperty(proto, name, Object.assign({}, d, { value: make(d.value) }));
    } catch (e) { rec.errors.push('wrapMethod ' + name + ': ' + clip(e, 120)); }
  };
  const wrapSetter = (proto, name, make) => {
    try {
      if (!proto) return;
      const d = Object.getOwnPropertyDescriptor(proto, name);
      if (!d || typeof d.set !== 'function') return;
      Object.defineProperty(proto, name, { configurable: true, enumerable: d.enumerable, get: d.get, set: make(d.set) });
    } catch (e) { rec.errors.push('wrapSetter ' + name + ': ' + clip(e, 120)); }
  };

  // setAttribute family with the name `style`
  wrapMethod(Element.prototype, 'setAttribute', (orig) => function setAttribute(name) {
    try { if (String(name).toLowerCase() === 'style') record('setAttribute', this, 'style', arguments[1]); } catch (e) { /* keep going */ }
    return orig.apply(this, arguments);
  });
  wrapMethod(Element.prototype, 'setAttributeNS', (orig) => function setAttributeNS(ns, name) {
    try { if (ns === null && String(name).toLowerCase() === 'style') record('setAttributeNS', this, 'style', arguments[2]); } catch (e) { /* keep going */ }
    return orig.apply(this, arguments);
  });
  for (const m of ['setAttributeNode', 'setAttributeNodeNS']) {
    wrapMethod(Element.prototype, m, (orig) => function setAttributeNode(attr) {
      try { if (attr && String(attr.name).toLowerCase() === 'style') record(m, this, 'style', attr.value); } catch (e) { /* keep going */ }
      return orig.apply(this, arguments);
    });
  }

  // `el.style` getter (remembers which element a declaration belongs to) and setter (forwards to cssText)
  const owner = new WeakMap();
  for (const C of [window.Element, window.HTMLElement, window.SVGElement, window.MathMLElement]) {
    try {
      if (!C) continue;
      const d = Object.getOwnPropertyDescriptor(C.prototype, 'style');
      if (!d) continue;
      Object.defineProperty(C.prototype, 'style', {
        configurable: true, enumerable: d.enumerable,
        get: d.get && function style() {
          const decl = d.get.call(this);
          try { if (decl && !owner.has(decl)) owner.set(decl, this); } catch (e) { /* keep going */ }
          return decl;
        },
        set: d.set && function style(v) { try { record('style-property', this, 'style', v); } catch (e) { /* keep going */ } return d.set.call(this, v); },
      });
    } catch (e) { rec.errors.push('style property: ' + clip(e, 120)); }
  }

  // CSSOM: cssText, every property setter and setProperty. Declarations of stylesheet rules have no owner element.
  const declProtos = [];
  for (const n of ['CSSStyleDeclaration', 'CSS2Properties', 'CSSStyleProperties']) {
    try { if (window[n] && window[n].prototype && declProtos.indexOf(window[n].prototype) < 0) declProtos.push(window[n].prototype); } catch (e) { /* keep going */ }
  }
  for (const proto of declProtos) {
    let names = [];
    try { names = Object.getOwnPropertyNames(proto); } catch (e) { names = []; }
    for (const name of names) {
      wrapSetter(proto, name, (orig) => function setter(v) {
        try { record('cssom', owner.get(this) || null, name, v); } catch (e) { /* keep going */ }
        return orig.call(this, v);
      });
    }
    wrapMethod(proto, 'setProperty', (orig) => function setProperty(name, value) {
      try { record('cssom', owner.get(this) || null, 'setProperty:' + name, value); } catch (e) { /* keep going */ }
      return orig.apply(this, arguments);
    });
  }

  // markup that carries a style attribute
  const styleInMarkup = (s) => /(^|[\s"'<])style\s*=/i.test(String(s));
  for (const name of ['innerHTML', 'outerHTML']) {
    wrapSetter(Element.prototype, name, (orig) => function setter(v) {
      try { if (styleInMarkup(v)) record('markup:' + name, this, 'style', v); } catch (e) { /* keep going */ }
      return orig.call(this, v);
    });
  }
  wrapMethod(Element.prototype, 'insertAdjacentHTML', (orig) => function insertAdjacentHTML(pos, html) {
    try { if (styleInMarkup(html)) record('markup:insertAdjacentHTML', this, 'style', html); } catch (e) { /* keep going */ }
    return orig.apply(this, arguments);
  });
  wrapMethod(window.Range && Range.prototype, 'createContextualFragment', (orig) => function createContextualFragment(html) {
    try { if (styleInMarkup(html)) record('markup:createContextualFragment', null, 'style', html); } catch (e) { /* keep going */ }
    return orig.apply(this, arguments);
  });
  wrapMethod(window.DOMParser && DOMParser.prototype, 'parseFromString', (orig) => function parseFromString(html) {
    try { if (styleInMarkup(html)) record('markup:parseFromString', null, 'style', html); } catch (e) { /* keep going */ }
    return orig.apply(this, arguments);
  });

  // the `style` attribute as the document sees it (no stack: it fires after the write)
  try {
    new MutationObserver((list) => {
      for (const m of list) { if (m.type === 'attributes' && m.attributeName === 'style') record('mutation', m.target, 'style', m.target.getAttribute('style'), false); }
    }).observe(document, { attributes: true, attributeFilter: ['style'], subtree: true });
  } catch (e) { rec.errors.push('observer: ' + clip(e, 120)); }

  const dump = (reason) => {
    try {
      const els = Array.from(document.querySelectorAll('[style]'));
      const d = { reason, t: now(), total: els.length, elements: els.slice(0, MAX_ELEMENTS).map((el) => ({
        tag: el.localName, id: clip(el.id || '', 60), cls: classOf(el), style: clip(el.getAttribute('style'), 200), outer: clip(el.outerHTML, 200) })) };
      if (rec.dumps.length < MAX_DUMPS) rec.dumps.push(d);
      return d;
    } catch (e) { rec.errors.push('dump: ' + clip(e, 120)); return null; }
  };

  const vseen = new Map();
  document.addEventListener('securitypolicyviolation', (e) => {
    try {
      rec.counts.violations++;
      const key = [e.violatedDirective, e.effectiveDirective, e.sourceFile, e.lineNumber, e.columnNumber, e.blockedURI, clip(e.sample, 60)].join('|');
      const old = vseen.get(key);
      if (old) { old.count++; return; }
      if (rec.violations.length >= MAX_VIOLATIONS) { rec.counts.dropped_violations++; return; }
      const v = { n: rec.violations.length, t: now(), violatedDirective: e.violatedDirective, effectiveDirective: e.effectiveDirective,
                  blockedURI: e.blockedURI, sourceFile: e.sourceFile, lineNumber: e.lineNumber, columnNumber: e.columnNumber,
                  sample: clip(e.sample, 120), disposition: e.disposition, originalPolicy: clip(e.originalPolicy, 1000),
                  statusCode: e.statusCode, documentURI: clip(e.documentURI, 200), target: brief(e.target), target_parents: chain(e.target),
                  last_write: rec.writes.length ? rec.writes.length - 1 : null, writes_so_far: rec.counts.writes, count: 1 };
      rec.violations.push(v);
      vseen.set(key, v);
      v.dump = rec.dumps.length;                       // index of the [style] dump taken right now
      dump('violation:' + v.n + ':' + clip(e.violatedDirective, 40));
    } catch (err) { rec.errors.push('violation: ' + clip(err, 120)); }
  }, true);

  window.__dvtCspFinish = () => {
    dump('end');
    return JSON.parse(JSON.stringify({ url: location.href, ua: navigator.userAgent, t: now(), counts: rec.counts, violations: rec.violations,
                                       writes: rec.writes, dumps: rec.dumps, errors: rec.errors }));
  };
})();
"""


def style_governor(header):
    """Which directive of a Content-Security-Policy header governs a style="" attribute: style-src-attr, else style-src, else
    default-src (CSP3 fallback chain). Returns (directive, [sources]) or (None, []) when nothing governs (no restriction).
    A directive that lists 'unsafe-inline' allows the attribute: the caller reads the sources."""
    csp = brlib.parse_csp(header)
    for name in ("style-src-attr", "style-src", "default-src"):
        if name in csp:
            return name, list(csp[name])
    return None, []


def attr_allowed_inline(header):
    """True when the policy lets a style attribute through ('unsafe-inline' without a nonce / hash in the governing directive,
    or no governing directive); False when it blocks it."""
    name, sources = style_governor(header)
    if name is None:
        return True
    lowered = [s.lower() for s in sources]
    if "'unsafe-inline'" in lowered and not any(s.startswith("'nonce-") or s.startswith("'sha") for s in lowered):
        return True
    return False


def short_source(url):
    """Path of a script URL (no origin, no query) for a finding line; '-' when empty."""
    if not url:
        return "-"
    parts = urlsplit(url)
    return (parts.path or url)[:120]


def violation_key(page, engine, violation):
    """(page, engine, directive, 'path:line:col') of one recorded violation: the unit of one finding line."""
    return (page, engine, violation.get("violatedDirective") or violation.get("effectiveDirective") or "?",
            "%s:%s:%s" % (short_source(violation.get("sourceFile")), violation.get("lineNumber", 0), violation.get("columnNumber", 0)))


def summarize(pages):
    """{directive: number of events} and the distinct (directive, source) pairs of a list of page records (as the test builds them)."""
    by_directive = {}
    pairs = set()
    for page in pages:
        for v in page.get("violations", []):
            d = v.get("violatedDirective") or v.get("effectiveDirective") or "?"
            by_directive[d] = by_directive.get(d, 0) + int(v.get("count", 1))
            pairs.add((d, "%s:%s:%s" % (short_source(v.get("sourceFile")), v.get("lineNumber", 0), v.get("columnNumber", 0))))
    return {"events_by_directive": by_directive, "distinct": sorted(pairs), "any_violation": bool(by_directive)}
