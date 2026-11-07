# SPDX-License-Identifier: MIT
"""Helpers of the Map room fade cases (cases/test_web_map_fade.py, TWIN2b): the in-page sampler, readers of the public state, and the
PURE judges that read a sample series (unit tests: cases/unit/test_map_fade_support.py). Not a test module.

The owner's spec (TWIN2b) for ONE over and its end on the Map room view:

  t ~ 0 (during the over)      the room shows the on-air look and the talker
  after the end                for 5 s the room fades from rooms[].last.end_ms (public state, the server clock); the caption says
                               "<call> finished" (the page: "Room R: <call> finished.")
  ~ 2.5 s after the end        a fade step (a lower-opacity class and/or a computed opacity strictly between full and rest)
  >= 6.5 s after the end       at rest (the sampling lag is documented as up to 1.25 s: 1 s tick + 250 ms publication gate)
  a NEW over during the fade   the full on-air look at once (within one sampling step)
  prefers-reduced-motion       a static dimmed state, no CSS transition (computed transition-duration 0s)

What a sample is (one page.evaluate per sample, SAMPLER_JS): the room tile's data-state, class list and computed fill; the caption text;
the computed opacity and transition-duration of the fading art elements; the callsign text drawn on the room. The series is annotated
with `t` = seconds since rooms[].last.end_ms on the server clock (the tester's wall clock minus the skew measured against
`now_ms` of the public state; browser, tester and router share the host clock on the rig, the skew is recorded).

Everything about the DOM is SOURCE-DERIVED from the page sources of dv-router:wip-1bf347c and NOT verified against a running page
(cases/web_pages.py, "#/map"); the judges prefer computed style to class names, and use the class `twin-fade-N` as a second witness.
"""
import time

import web_pages as pages

SAMPLE_S = 0.25            # sampling interval of the cases
LAG_S = 1.25               # documented sampling lag of the page (1 s tick + 250 ms publication gate)
FADE_S = 5.0               # the fade lasts 5 s of the server clock
MID_S = 2.5                # the mid-fade check
REST_S = 6.5               # at rest from here on
START_TOL_S = 0.3          # clock tolerance around t = 0
TAIL_S = 8.5               # sample until this long after the end
RESTORE_S = 1.6            # a new over is on the page this soon after its first frame (stream latency + 250 ms gate + one sample)
REDUCED_LEVEL = 0.45       # --twin-fade-reduced
CALL_TOL = 3               # a truncated call on the tile ("N0FA.") needs at least this many characters

SAMPLER_JS = r"""
(sel) => {
  const q = (s) => document.querySelector(s);
  const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : null; };
  const out = { found: false };
  const room = q(sel.room);
  const cap = q(sel.caption);
  out.caption = cap ? (cap.textContent || '').trim() : null;
  out.hidden = !!document.hidden;
  out.reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  out.onair_rooms = Array.from(document.querySelectorAll(sel.room_any)).filter((r) => r.getAttribute('data-state') === 'onair').length;
  if (room) {
    const cs = getComputedStyle(room);
    out.found = true;
    out.state = room.getAttribute('data-state');
    out.room_class = room.getAttribute('class') || '';
    out.fill = cs.fill;
    out.room_opacity = num(cs.opacity);
    out.room_transition = cs.transitionDuration;
    out.label = room.getAttribute('aria-label');
  }
  const fading = Array.from(document.querySelectorAll(sel.fading));
  const art = fading.filter((e) => !e.matches(sel.room_any));
  out.fading_n = fading.length;
  out.art_n = art.length;
  const ops = art.map((e) => num(getComputedStyle(e).opacity)).filter((v) => v !== null);
  out.art_min = ops.length ? Math.min(...ops) : null;
  out.art_max = ops.length ? Math.max(...ops) : null;
  out.art_transition = Array.from(new Set(art.map((e) => getComputedStyle(e).transitionDuration)));
  const call = q(sel.call);
  if (call) {
    const ccs = getComputedStyle(call);
    out.call_text = (call.textContent || '').trim();
    out.call_opacity = num(ccs.opacity);
    out.call_class = call.getAttribute('class') || '';
  }
  return out;
}
"""


# ------------------------------------------------------------------ pure readers of one sample

def classes(sample):
    return (sample.get("room_class") or "").split()


def step_of(sample):
    """The fade step N of `twin-fade-N` on the room tile, or None."""
    for c in classes(sample):
        m = pages.MAP_FADE_CLASS_RE.fullmatch(c)
        if m:
            return int(m.group(1))
    return None


def is_fading(sample):
    return bool(sample.get("found")) and pages.MAP_FADING_CLASS in classes(sample)


def is_onair_look(sample):
    """The on-air look without a fade: data-state onair and no fading class."""
    return bool(sample.get("found")) and sample.get("state") == "onair" and not is_fading(sample)


def is_rest(sample):
    return bool(sample.get("found")) and sample.get("state") == "idle" and not is_fading(sample) and not sample.get("fading_n")


def caption_has(sample, text):
    return text in (sample.get("caption") or "")


def talking_caption(sample, room, call):
    return caption_has(sample, pages.MAP_CAPTION_TALKING.format(room=room, call=call))


def finished_caption(sample, room, call):
    return caption_has(sample, "Room %s: %s." % (room, pages.MAP_CAPTION_FINISHED.format(call=call)))


def tile_call_matches(text, call):
    """The callsign drawn on the room tile: the full call, or a truncated one ending in '.' (the page cuts it to the tile width)."""
    if not text:
        return False
    if text == call:
        return True
    return text.endswith(".") and len(text) - 1 >= CALL_TOL and call.startswith(text[:-1])


def parse_color(text):
    """(r, g, b) in 0..255 of a computed colour: rgb(), rgba(), `color(srgb r g b [/ a])`; None for anything else (oklab, ...)."""
    s = (text or "").strip().lower()
    try:
        if s.startswith("color(srgb"):
            body = s[s.index("srgb") + 4:s.rindex(")")]
            parts = body.replace("/", " ").replace(",", " ").split()
            r, g, b = (float(p) * 255.0 for p in parts[:3])
            return (r, g, b)
        if s.startswith("rgb"):
            body = s[s.index("(") + 1:s.rindex(")")]
            parts = body.replace("/", " ").replace(",", " ").split()
            vals = []
            for p in parts[:3]:
                vals.append(float(p[:-1]) * 2.55 if p.endswith("%") else float(p))
            return tuple(vals) if len(vals) == 3 else None
    except (ValueError, IndexError):
        return None
    return None


def fill_fraction(fill, lit, rest):
    """How far a computed fill is on the way from the on-air colour `lit` (0.0) to the rest colour `rest` (1.0), projected onto the
    line between them; None when a colour cannot be read or the two references are too close to tell apart."""
    p, a, b = parse_color(fill), parse_color(lit), parse_color(rest)
    if p is None or a is None or b is None:
        return None
    d = [b[i] - a[i] for i in range(3)]
    dd = sum(x * x for x in d)
    if dd < 25.0:
        return None
    return sum((p[i] - a[i]) * d[i] for i in range(3)) / dd


def color_distance(c1, c2):
    a, b = parse_color(c1), parse_color(c2)
    if a is None or b is None:
        return None
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def durations_zero(text):
    """True when every duration of a computed `transition-duration` list is 0 ('0s', '0s, 0s', '0ms'); an empty value counts as none."""
    items = [x.strip() for x in (text or "").split(",") if x.strip()]
    for item in items:
        try:
            value = float(item[:-2]) / 1000.0 if item.endswith("ms") else float(item[:-1]) if item.endswith("s") else float(item)
        except ValueError:
            return False
        if value > 0.0:
            return False
    return True


def all_zero(values):
    return all(durations_zero(v) for v in values)


def reference_fills(series):
    """(lit, rest) fills: the last on-air-look sample before the end and the last rest sample."""
    lit = next((s["fill"] for s in reversed(series) if s.get("t", 0) < -START_TOL_S and is_onair_look(s) and s.get("fill")), None)
    rest = next((s["fill"] for s in reversed(series) if is_rest(s) and s.get("fill")), None)
    return lit, rest


def annotate(series, end_ms, skew_ms=0.0):
    """Adds `t` (s since end_ms on the server clock) to every sample of the series."""
    for s in series:
        s["t"] = round((s["wall_ms"] - skew_ms - end_ms) / 1000.0, 3)
    return series


def fade_witness(sample, lit=None, rest=None):
    """The ways a sample can show 'in a fade step': (class step >= 1, opacity strictly between 0 and 1, fill strictly between)."""
    step = step_of(sample)
    by_class = step is not None and step >= 1
    lo = sample.get("art_min")
    by_opacity = lo is not None and 0.0 < lo < 1.0
    frac = fill_fraction(sample.get("fill"), lit, rest) if lit and rest else None
    by_fill = frac is not None and 0.02 < frac < 0.98
    return by_class, by_opacity, by_fill


# ------------------------------------------------------------------ the judges (lists of failure strings)

def judge_fade(series, room, call):
    """One over, then its end: every statement of the spec above. `series` is annotated (`t`). Returns (failures, metrics)."""
    fail = []
    lit, rest = reference_fills(series)
    fades = [s for s in series if is_fading(s)]
    metrics = {"samples": len(series), "fading_samples": len(fades), "lit_fill": lit, "rest_fill": rest}
    # 1. during the over: the on-air look and the talker
    during = [s for s in series if s["t"] < -START_TOL_S and is_onair_look(s)]
    metrics["over_samples"] = len(during)
    if len(during) < 2:
        fail.append("during the over fewer than 2 samples show the on-air look (data-state onair without a fade class): %d" % len(during))
    if during and not any(talking_caption(s, room, call) for s in during):
        fail.append("during the over the caption never says %r (last: %r)" % (pages.MAP_CAPTION_TALKING.format(room=room, call=call), during[-1].get("caption")))
    if during and not any(tile_call_matches(s.get("call_text"), call) for s in during):
        fail.append("during the over the talker %s is not drawn on the room tile (text seen: %s)" % (call, sorted({s.get("call_text") for s in during}, key=str)))
    if lit and rest:
        dist = color_distance(lit, rest)
        metrics["lit_rest_distance"] = None if dist is None else round(dist, 1)
        if dist is not None and dist < 8.0:
            fail.append("the on-air fill %s and the rest fill %s are not visibly different" % (lit, rest))
    else:
        fail.append("no reference fill (lit=%r rest=%r): the over or the rest was never sampled" % (lit, rest))
    # 2. the fade starts at the end (within the documented lag) and says finished
    if not fades:
        fail.append("no sample shows a fade class (`%s`) after the end" % pages.MAP_FADING_CLASS)
        return fail, metrics
    first, last = fades[0], fades[-1]
    metrics.update({"fade_first_t": first["t"], "fade_last_t": last["t"], "fade_first_step": step_of(first), "fade_last_step": step_of(last),
                    "steps_seen": sorted({step_of(s) for s in fades if step_of(s) is not None})})
    if not (-START_TOL_S <= first["t"] <= LAG_S + SAMPLE_S + START_TOL_S):
        fail.append("the fade starts at t=%.2f s, wanted within 0 .. %.2f s of rooms[].last.end_ms (lag %.2f s)" % (first["t"], LAG_S + SAMPLE_S, LAG_S))
    early_idle = [s for s in series if -START_TOL_S < s["t"] < first["t"] and s.get("state") == "idle"]
    if early_idle:
        fail.append("the room went idle at t=%.2f s before the fade began at t=%.2f s" % (early_idle[0]["t"], first["t"]))
    bad_caption = [s for s in fades if not finished_caption(s, room, call)]
    if bad_caption:
        fail.append("%d of %d fade samples lack the caption %r (first: t=%.2f %r)" % (
            len(bad_caption), len(fades), "Room %s: %s." % (room, pages.MAP_CAPTION_FINISHED.format(call=call)), bad_caption[0]["t"], bad_caption[0].get("caption")))
    # 3. about 2.5 s after the end: a fade step
    near = [s for s in series if abs(s["t"] - MID_S) <= SAMPLE_S + 0.05]
    metrics["mid_samples"] = [{"t": s["t"], "step": step_of(s), "art_min": s.get("art_min"), "fade_fill": None if not (lit and rest) else fill_fraction(s.get("fill"), lit, rest)} for s in near]
    if not near:
        fail.append("no sample within %.2f s of t=%.1f s" % (SAMPLE_S + 0.05, MID_S))
    else:
        mid = min(near, key=lambda s: abs(s["t"] - MID_S))
        by_class, by_opacity, by_fill = fade_witness(mid, lit, rest)
        metrics["mid"] = {"t": mid["t"], "step": step_of(mid), "class_step": by_class, "opacity_between": by_opacity, "fill_between": by_fill,
                          "art_min": mid.get("art_min"), "caption": mid.get("caption")}
        if not is_fading(mid):
            fail.append("at t=%.2f s the room is not in a fade step (class %r, state %r)" % (mid["t"], mid.get("room_class"), mid.get("state")))
        else:
            if not (by_class or by_opacity or by_fill):
                fail.append("at t=%.2f s no sign of a fade: class step %r, art opacity %r, fill %r" % (mid["t"], step_of(mid), mid.get("art_min"), mid.get("fill")))
            if step_of(mid) not in (1, 2):
                fail.append("at t=%.2f s the fade step is %r, wanted 1 or 2 (2.5 s minus up to %.2f s of lag)" % (mid["t"], step_of(mid), LAG_S))
            if mid.get("art_min") is not None and not (0.0 < mid["art_min"] <= 1.0):
                fail.append("at t=%.2f s the computed art opacity is %r, wanted 0 < opacity <= 1" % (mid["t"], mid["art_min"]))
            if not finished_caption(mid, room, call):
                fail.append("at t=%.2f s the caption is %r, wanted %r" % (mid["t"], mid.get("caption"), pages.MAP_CAPTION_FINISHED.format(call=call)))
    # 4. monotone: the step never goes back, the art opacity never rises, the fill never returns to lit
    steps = [step_of(s) for s in fades if step_of(s) is not None]
    if any(b < a for a, b in zip(steps, steps[1:])):
        fail.append("the fade step goes backwards: %s" % steps)
    ops = [s["art_min"] for s in fades if s.get("art_min") is not None]
    if any(b > a + 0.03 for a, b in zip(ops, ops[1:])):
        fail.append("the art opacity rises during the fade: %s" % [round(x, 2) for x in ops])
    fracs = [fill_fraction(s.get("fill"), lit, rest) for s in fades] if lit and rest else []
    fracs = [f for f in fracs if f is not None]
    metrics["fill_fractions"] = [round(f, 2) for f in fracs]
    if any(b < a - 0.03 for a, b in zip(fracs, fracs[1:])):
        fail.append("the room fill moves back toward the on-air colour during the fade: %s" % [round(x, 2) for x in fracs])
    # 5. the fade lasts about 5 s and the room is at rest from 6.5 s
    if last["t"] - first["t"] < FADE_S - LAG_S - 0.5:
        fail.append("the fade lasts %.2f s (t=%.2f .. %.2f), wanted about %.0f s" % (last["t"] - first["t"], first["t"], last["t"], FADE_S))
    late = [s for s in series if s["t"] >= REST_S]
    metrics["rest_samples"] = len(late)
    rest_first = next((s for s in series if s["t"] >= 0.0 and is_rest(s)), None)
    metrics["rest_first_t"] = None if rest_first is None else rest_first["t"]
    if rest_first is not None and rest_first["t"] < FADE_S - START_TOL_S:
        fail.append("the room is at rest at t=%.2f s, before the 5 s fade is over" % rest_first["t"])
    if not late:
        fail.append("the series ends before t=%.1f s: the rest cannot be judged" % REST_S)
    for s in late:
        problems = []
        if not is_rest(s):
            problems.append("state %r class %r fading elements %r" % (s.get("state"), s.get("room_class"), s.get("fading_n")))
        if caption_has(s, "finished"):
            problems.append("caption still says finished: %r" % s.get("caption"))
        if problems:
            fail.append("at t=%.2f s (>= %.1f s) the room is not at rest: %s" % (s["t"], REST_S, "; ".join(problems)))
            break
    if late and not any(caption_has(s, pages.MAP_CAPTION_NO_ONE) for s in late):
        fail.append("at rest the caption never says %r (last: %r)" % (pages.MAP_CAPTION_NO_ONE, late[-1].get("caption")))
    return fail, metrics


def judge_restore(series, room, call, lit, rest, over_s):
    """A NEW over during the fade (`u` = seconds since the first frame of the new over was sent): the full on-air look at once.
    `series` carries `u`. Returns (failures, metrics)."""
    fail = []
    metrics = {}
    before = [s for s in series if s["u"] < 0 and is_fading(s)]
    if not before or max(step_of(s) or 0 for s in before) < 1:
        fail.append("the new over did not start inside a fade of step >= 1 (fading samples before it: %d)" % len(before))
    after = [s for s in series if s["u"] >= 0]
    restored = next((s for s in after if is_onair_look(s) and talking_caption(s, room, call)), None)
    if restored is None:
        fail.append("after the new over's first frame no sample shows the on-air look again (%d samples, last class %r, caption %r)" % (
            len(after), after[-1].get("room_class") if after else None, after[-1].get("caption") if after else None))
        return fail, metrics
    metrics.update({"restored_u": restored["u"], "restored_state": restored.get("state"), "restored_fill": restored.get("fill"),
                    "restored_call_opacity": restored.get("call_opacity")})
    if restored["u"] > RESTORE_S:
        fail.append("the full on-air look is back %.2f s after the new over's first frame, wanted within %.2f s" % (restored["u"], RESTORE_S))
    # from the first restored sample until the new over ends, every sample is the full look: no dimmed intermediate
    window = [s for s in after if s["u"] >= restored["u"] and s["u"] <= over_s - 0.4]
    for s in window:
        if is_fading(s) or s.get("art_n"):
            fail.append("at u=%.2f s a fade class or fading element is back during the new over (class %r)" % (s["u"], s.get("room_class")))
            break
        if s.get("call_opacity") is not None and s["call_opacity"] < 0.99:
            fail.append("at u=%.2f s the talker text is dimmed (opacity %r) during the new over" % (s["u"], s["call_opacity"]))
            break
        frac = fill_fraction(s.get("fill"), lit, rest) if lit and rest else None
        if frac is not None and frac > 0.05:
            fail.append("at u=%.2f s the room fill is %.2f of the way to rest during the new over (not the full on-air colour)" % (s["u"], frac))
            break
    metrics["window_samples"] = len(window)
    return fail, metrics


def judge_reduced(series, room, call):
    """prefers-reduced-motion: during the fade a static dimmed state (opacity 0.45, fill between the two colours and constant) and no
    CSS transition on the room tile or on a fading element; then at rest. Returns (failures, metrics)."""
    fail = []
    lit, rest = reference_fills(series)
    fades = [s for s in series if is_fading(s)]
    metrics = {"samples": len(series), "fading_samples": len(fades), "lit_fill": lit, "rest_fill": rest}
    if not series or not all(s.get("reduced") for s in series):
        fail.append("the page does not see prefers-reduced-motion: reduce (emulate_media did not apply)")
    if len(fades) < 4:
        fail.append("fewer than 4 samples inside the fade: %d" % len(fades))
        return fail, metrics
    bad_t = [s for s in fades if not all_zero([s.get("room_transition") or ""])]
    if bad_t:
        fail.append("the room tile has a CSS transition under reduced motion at t=%.2f s: %r" % (bad_t[0]["t"], bad_t[0].get("room_transition")))
    bad_a = [s for s in fades if s.get("art_n") and not all_zero(s.get("art_transition") or [])]
    if bad_a:
        fail.append("a fading element has a CSS transition under reduced motion at t=%.2f s: %r" % (bad_a[0]["t"], bad_a[0].get("art_transition")))
    if not any(s.get("art_n") for s in fades):
        fail.append("no fading art element was ever sampled: the dimmed opacity cannot be judged")
    levels = [(s["art_min"], s["art_max"]) for s in fades if s.get("art_n") and s.get("art_min") is not None]
    metrics["art_levels"] = sorted({(round(a, 3), round(b, 3)) for a, b in levels})
    off = [(a, b) for a, b in levels if abs(a - REDUCED_LEVEL) > 0.02 or abs(b - REDUCED_LEVEL) > 0.02]
    if off:
        fail.append("the dimmed opacity under reduced motion is not the static %.2f: %s" % (REDUCED_LEVEL, off[:3]))
    fracs = [fill_fraction(s.get("fill"), lit, rest) for s in fades] if lit and rest else []
    fracs = [f for f in fracs if f is not None]
    metrics["fill_fractions"] = [round(f, 2) for f in fracs]
    if fracs:
        if max(fracs) - min(fracs) > 0.06:
            fail.append("the room fill is not static during the reduced-motion fade: fractions %s" % [round(x, 2) for x in fracs])
        if not (0.2 < sum(fracs) / len(fracs) < 0.9):
            fail.append("the reduced-motion fill is not a dimmed state between the on-air and the rest colour: mean fraction %.2f" % (sum(fracs) / len(fracs)))
    bad_caption = [s for s in fades if not finished_caption(s, room, call)]
    if bad_caption:
        fail.append("%d fade sample(s) lack the caption '%s finished' (first t=%.2f: %r)" % (len(bad_caption), call, bad_caption[0]["t"], bad_caption[0].get("caption")))
    late = [s for s in series if s["t"] >= REST_S]
    if not late:
        fail.append("the series ends before t=%.1f s" % REST_S)
    elif not all(is_rest(s) for s in late):
        bad = next(s for s in late if not is_rest(s))
        fail.append("at t=%.2f s the room is not at rest under reduced motion (class %r)" % (bad["t"], bad.get("room_class")))
    metrics.update({"fade_first_t": fades[0]["t"], "fade_last_t": fades[-1]["t"]})
    return fail, metrics


# ------------------------------------------------------------------ the page and the public state

def take(page, sel):
    """One sample: the in-page measurements plus the tester's wall clock around the call (ms)."""
    t0 = time.time() * 1000.0
    raw = page.evaluate(SAMPLER_JS, sel)
    t1 = time.time() * 1000.0
    raw["wall_ms"] = round((t0 + t1) / 2.0, 1)
    raw["eval_ms"] = round(t1 - t0, 1)
    return raw


def room_entry(state, room):
    """rooms[] entry of one module in the public state, or None."""
    for entry in state.get("rooms") or []:
        if isinstance(entry, dict) and entry.get("room") == room:
            return entry
    return None


def read_end(anon, room, since_ms):
    """(end_ms, skew_ms, entry) once the module is off the air and rooms[].last.end_ms is newer than `since_ms`; (None, 0.0, entry) before.
    skew_ms = the tester's wall clock at the middle of the request minus `now_ms` of the state (it includes half the round trip)."""
    t0 = time.time() * 1000.0
    state = anon.get("/api/v1/public/state", pace=False, expect=200).json()
    t1 = time.time() * 1000.0
    entry = room_entry(state, room) or {}
    last = entry.get("last") or {}
    end = last.get("end_ms")
    now_ms = state.get("now_ms")
    if entry.get("onair") or not isinstance(end, (int, float)) or end < since_ms or not isinstance(now_ms, (int, float)):
        return None, 0.0, entry
    return float(end), (t0 + t1) / 2.0 - float(now_ms), entry
