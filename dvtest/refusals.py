# SPDX-License-Identifier: MIT
"""WEBSTALL1: the WebRTC capacity refusals of DV-Router, as the black box sees them (docs/webstall1-cases.md, docs/web-refusals-cases.md).

Source: DV-Router merge 17b05933 (docs/design/status.md "WebRTC offer refusals" and "RTC capacity refusals", docs/design/web.md "signalling
handoff", crates/svc/web/src/rtc/rtc_core/table.rs). Stdlib only; no rig, no network.

What the image says (the fix image, not the current one):
  * Every capacity refusal of an offer (or a TURN issue) is HTTP 503, problem code `unavailable`, `Retry-After: 5`. Its detail names the
    limit with a fixed string per limit (DETAILS below); before the fix it was the single "WebRTC capacity reached". The detail is recorded,
    NEVER judged: status, code and Retry-After are the contract.
  * Two counter families, both with every reason present from the first record (zeros included):
      dv_edge_webrtc_offer_refused_total{reason}   at the edge: the ten EDGE_REASONS
      dv_web_rtc_refused_total{reason}             at the web layer: the ten edge reasons plus the seven WEB_ONLY_REASONS
    They describe different boundaries (the deciding edge limit, the HTTP reply) and MUST NOT be summed as unique refused joins: an edge
    refusal relayed as a 503 is counted once in each family.
  * dv_edge_webrtc_join_refused_total{reason="core"} is a different counter (a JOIN refused by the room core after the offer was accepted).
"""
import json
import os
import time

EDGE_FAMILY = "dv_edge_webrtc_offer_refused_total"
WEB_FAMILY = "dv_web_rtc_refused_total"
CORE_FAMILY = "dv_edge_webrtc_join_refused_total"

EDGE_REASONS = ("join_window", "listeners", "room", "pending", "retired", "guests", "guest_ip", "nonce_cache", "slots", "signalling_busy")
WEB_ONLY_REASONS = ("web_records", "web_sessions", "login_circuits", "web_guest_records", "web_guests", "web_guest_ip", "turn_issues")
WEB_REASONS = EDGE_REASONS + WEB_ONLY_REASONS

# The problem detail of each reason on the fix image (web.md tables, rtc_core/table.rs Capacity::detail). Recorded, never judged.
DETAILS = {
    "join_window": "WebRTC join window capacity reached",
    "listeners": "WebRTC listener capacity reached",
    "room": "WebRTC room listener capacity reached",
    "pending": "WebRTC pending output capacity reached",
    "retired": "WebRTC retired peer capacity reached",
    "guests": "WebRTC guest listener capacity reached",
    "guest_ip": "WebRTC guest IP capacity reached",
    "nonce_cache": "WebRTC nonce cache capacity reached",
    "slots": "WebRTC peer slot capacity reached",
    "signalling_busy": "WebRTC signalling queue capacity reached",
    "web_records": "WebRTC ownership record capacity reached",
    "web_sessions": "WebRTC web session capacity reached",
    "login_circuits": "WebRTC login circuit capacity reached",
    "web_guest_records": "WebRTC guest ownership record capacity reached",
    "web_guests": "WebRTC web guest session capacity reached",
    "web_guest_ip": "WebRTC web guest IP capacity reached",
    "turn_issues": "WebRTC TURN issue table capacity reached",
}
OLD_DETAIL = "WebRTC capacity reached"      # the single detail of the images before the fix (wip-a31709a)

CAPACITY_STATUS = 503
CAPACITY_CODE = "unavailable"
CAPACITY_RETRY_AFTER = 5
LOGIN_CIRCUIT_CAP = 8                       # circuits (offers) per login; the ninth is refused with reason login_circuits
EDGE_JOINS_PER_SECOND = 5                   # [edges.webrtc] joins_per_second, 1..5, default 5 (a join window of one second)

SWITCH = "DVT_WEBSTALL1FIX"
TRUE = ("on", "1", "true", "yes")


def fix_on(environ=None):
    """True when DVT_WEBSTALL1FIX=on: the image has the WEBSTALL1 fix, so the strict assertions of the new cases apply (otherwise they skip with a reason)."""
    env = os.environ if environ is None else environ
    return str(env.get(SWITCH, "")).strip().lower() in TRUE


def skip_reason():
    return "%s=on not set: the strict WEBSTALL1 assertions are for the image with the fix (docs/webstall1-cases.md)" % SWITCH


# ---------------------------------------------------------------- the counters

def by_reason(samples, family):
    """{reason: value} of one family from parsed metric samples (dvtest.metrics.parse), summed over every other label (the hub adds `proc`).
    None when the family has no sample at all (an image without the counters); a sample without a reason label is ignored."""
    seen = False
    out = {}
    for sample in samples:
        if sample.name != family:
            continue
        seen = True
        reason = sample.labels.get("reason")
        if reason is None:
            continue
        out[reason] = out.get(reason, 0.0) + sample.value
    return out if seen else None


def missing_reasons(counts, expected):
    """The expected reasons that a {reason: value} dict lacks (all of them for None), in the order of `expected`."""
    have = counts or {}
    return [reason for reason in expected if reason not in have]


def unexpected_reasons(counts, expected):
    """Reasons present that are not in `expected` (a label that is not one of the fixed ones), sorted."""
    return sorted(set(counts or {}) - set(expected))


def snapshot(samples):
    """{"edge": {reason: n} or None, "web": {reason: n} or None} of one metrics read."""
    return {"edge": by_reason(samples, EDGE_FAMILY), "web": by_reason(samples, WEB_FAMILY)}


def delta(before, after):
    """Per family, {reason: after - before} over the reasons of `after` (a reason that was absent before counts from 0); None when either read has
    no such family. Counter resets (a negative step: the process restarted) are reported as they are, never hidden."""
    out = {}
    for fam in ("edge", "web"):
        b, a = (before or {}).get(fam), (after or {}).get(fam)
        out[fam] = None if b is None or a is None else {r: round(a[r] - b.get(r, 0.0), 6) for r in a}
    return out


def moved(counts):
    """{reason: n} of the reasons with a non-zero value (the 'what happened' view of a snapshot or a delta family)."""
    return {r: v for r, v in (counts or {}).items() if v}


def total(counts):
    """Sum of one family's reasons. Useful within ONE family only: never add the edge and the web families together."""
    return sum((counts or {}).values())


def shape_problems(snap):
    """What is wrong with the exposition of a snapshot: a family that is absent, a fixed reason that is missing (zeros must be present) and a label
    value that is not one of the fixed ones. [] when both families are complete."""
    out = []
    for fam, family, expected in (("edge", EDGE_FAMILY, EDGE_REASONS), ("web", WEB_FAMILY, WEB_REASONS)):
        counts = (snap or {}).get(fam)
        if counts is None:
            out.append("%s is not exposed" % family)
            continue
        lacking = missing_reasons(counts, expected)
        if lacking:
            out.append("%s lacks the reasons %s (every fixed reason must be present, zeros included)" % (family, ", ".join(lacking)))
        extra = unexpected_reasons(counts, expected)
        if extra:
            out.append("%s has the reasons %s, which are not fixed ones" % (family, ", ".join(extra)))
    return out


def record(before, after):
    """The shape that goes into out/<run>/web/load.json: before, after, the moved reasons and the shape problems of `after`."""
    d = delta(before, after)
    return {"before": before, "after": after, "delta": d, "moved": {fam: moved(d[fam]) for fam in d},
            "shape_problems": shape_problems(after)}


def line(rec):
    """One printed line for the load summary: the reasons that moved in each family (and 'none' when nothing did), 'n/a' for a family that is absent."""
    def one(fam):
        counts = (rec.get("delta") or {}).get(fam)
        if counts is None:
            return "n/a"
        mv = moved(counts)
        return ", ".join("%s +%g" % (r, v) for r, v in mv.items()) or "none"
    return "refusals edge(%s): %s; web(%s): %s" % (EDGE_FAMILY, one("edge"), WEB_FAMILY, one("web"))


# ---------------------------------------------------------------- the refusal itself

def problem_code(body):
    """The problem code of a decoded problem body (a dict): `code`, else the last path segment of `type`; None when there is neither."""
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if isinstance(code, str) and code:
        return code
    kind = body.get("type")
    if isinstance(kind, str):
        tail = [seg for seg in kind.split("/") if seg]
        if tail:
            return tail[-1]
    return None


def refusal_shape_problems(what, status, code, retry_after):
    """The contract of a capacity refusal (status, code, Retry-After; the detail is never judged). `retry_after` is the header value (str or int)."""
    out = []
    if status != CAPACITY_STATUS:
        out.append("%s answered %s, want %d" % (what, status, CAPACITY_STATUS))
    if code != CAPACITY_CODE:
        out.append("%s code %r, want %r" % (what, code, CAPACITY_CODE))
    if str(retry_after) != str(CAPACITY_RETRY_AFTER):
        out.append("%s Retry-After %r, want %d" % (what, retry_after, CAPACITY_RETRY_AFTER))
    return out


def detail_note(detail):
    """What a detail string is, for the record only: the reason it names on the fix image, 'old' for the single pre-fix text, 'other' otherwise."""
    for reason, text in DETAILS.items():
        if detail == text:
            return reason
    return "old" if detail == OLD_DETAIL else "other"


def join_window_setting(config_text, extra_env=None, snippet="join-window.toml"):
    """[edges.webrtc] joins_per_second of the run: {"joins_per_second": int or None, "source": "rendered"|"env"|"unknown"}. A rendered dv-router.toml
    text is definitive; without it DVT_SUT_CONFIG_EXTRA naming the snippet means 1; otherwise unknown."""
    if config_text:
        try:
            import tomllib
            value = ((tomllib.loads(config_text).get("edges") or {}).get("webrtc") or {}).get("joins_per_second")
        except Exception:                                    # noqa: BLE001 - evidence only
            value = None
        ok = isinstance(value, int) and not isinstance(value, bool)
        return {"joins_per_second": value if ok else None, "source": "rendered"}
    if extra_env:
        names = [os.path.basename(x) for x in str(extra_env).split(":") if x]
        if snippet in names:
            return {"joins_per_second": 1, "source": "env"}
        return {"joins_per_second": None, "source": "env"}
    return {"joins_per_second": None, "source": "unknown"}


def retry_after_s(headers):
    """The Retry-After header (any case of the name) as a number of seconds, None when it is missing or not a number."""
    for name, value in (headers or {}).items():
        if str(name).lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def is_capacity_refusal(status, text, headers=None):
    """True for an offer the service refused for capacity, judged on STATUS, problem CODE and Retry-After, never on the detail text (it names the
    limit on the fix image and is the single "WebRTC capacity reached" before it): HTTP 503, code `unavailable`, a positive Retry-After.
    Both shapes are accepted: a body with a code is judged by it; a body without one (not JSON, or JSON without a code or a type) falls back to the
    old text rule ("capacity reached" in the body), so a reply the harness cannot decode is still retried as before."""
    if status != CAPACITY_STATUS:
        return False
    body = None
    try:
        body = json.loads(text) if text else None
    except (TypeError, ValueError):
        body = None
    code = problem_code(body)
    if code is not None:
        wait = retry_after_s(headers)
        return code == CAPACITY_CODE and wait is not None and wait > 0
    return "capacity reached" in (text or "")


def problem_detail(text, limit=120):
    """The detail of a problem body, shortened, for the record ('' when the body is not a problem document)."""
    try:
        body = json.loads(text) if text else None
    except (TypeError, ValueError):
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail)[:limit] if isinstance(detail, str) else ""


# ---------------------------------------------------------------- reading the counters from the hub

READ_STEP_S = 12.0              # the edge and the web service publish their counters about every 10 s and the hub caches them: two reads this far apart
READ_MAX_S = 48.0               # ... that agree are a settled value; give up (and use the last read) after this long


def read(rig_control):
    """One read of both families through the control channel (`metrics xlxd-sut`, dvtest/metrics.py): {"edge": {reason: n} or None, "web": ...};
    both None and read_error True when the hub cannot be read."""
    from dvtest import metrics
    try:
        return snapshot(metrics.parse(rig_control.metrics("xlxd-sut")))
    except (RuntimeError, TimeoutError, ValueError, OSError):
        return {"edge": None, "web": None, "read_error": True}


def settled(read_fn, sleep=None, step_s=READ_STEP_S, max_s=READ_MAX_S):
    """The counters once they stopped moving: the producers publish about every 10 s and the hub caches, so a single read can lag a refusal that
    already happened. Read, wait `step_s`, read again; two equal reads in a row are the value; after `max_s` the last read is used (a counter that
    never settles is still recorded). Returns (snapshot, settled)."""
    sleep = sleep or time.sleep
    last = read_fn()
    waited = 0.0
    while waited < max_s:
        sleep(step_s)
        waited += step_s
        now = read_fn()
        if now == last:
            return now, True
        last = now
    return last, False


def poll(read_fn, ok, budget_s, step_s=3.0, sleep=None):
    """Read until ok(snapshot) or the budget (counted in sleeps, so a fake sleep makes it instant) is used up. Returns (last snapshot, ok)."""
    sleep = sleep or time.sleep
    waited = 0.0
    last = read_fn()
    while not ok(last):
        if waited >= budget_s:
            return last, False
        sleep(step_s)
        waited += step_s
        last = read_fn()
    return last, True
