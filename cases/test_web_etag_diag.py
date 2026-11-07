# SPDX-License-Identifier: MIT
"""Diagnostic for the public state validator (ETag) on an IDLE node (wip-ac750c7 DP7 question): three pairs of GETs a few seconds apart, the bodies
diffed field by field, the ETags compared, then HEAD / conditional HEAD / conditional GET. Records only; fails when the bodies are identical but
the ETags differ (a validator computation defect) or when a changing field is found (the field name goes to the notes as a FINDING).
Run: the web profile as in docs/web-api-cases.md with cases/test_web_etag_diag.py.

ETAG1 (the next image): `test_public_etag_idle_and_after_a_real_change` is the strict case, both ways, for the state and the last-heard
routes: (1) on an UNCHANGED node the ETag is identical across requests several seconds apart (whatever else in the body moves: the
case records which leaves moved and asserts only the observable, an equal validator) and the conditional GET / HEAD give 304; (2) after a
REAL change made by a protocol client (a DExtra link, an M17 position, a D-STAR over) the ETag changes, a conditional request with the OLD
ETag gets 200 with the new one, and with the NEW ETag 304. No field name is asserted. Written against web.md ("weak ETag, hash of the body
without now_ms") and the ETAG1 request; NOT RUN yet. Needs the rig (protocol clients) as well as the web profile; the position trigger needs
the data worker (data-ident.toml) and skips with the reason otherwise."""
import contextlib
import json
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dvtest import web  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web]


def _walk(a, b, path=""):
    """[(path, a, b)] of the leaves that differ between two JSON values."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            out += _walk(a.get(key), b.get(key), "%s/%s" % (path, key))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _walk(x, y, "%s[%d]" % (path, i))
        return out
    return [] if a == b else [(path, a, b)]


@pytest.fixture(scope="module")
def anon(web_client):
    return web_client()


@pytest.mark.parametrize("path", ["/api/v1/public/state", "/api/v1/public/lastheard"])
def test_public_validator_on_an_idle_node(anon, path):
    pairs = []
    for _ in range(3):
        first = anon.get(path, expect=200)
        time.sleep(4.0)
        second = anon.get(path, expect=200)
        diffs = _walk(json.loads(first.body), json.loads(second.body))
        pairs.append((first.headers.get("etag"), second.headers.get("etag"), diffs, first.body == second.body))
        web.finding("etag-diag %s: etag1 %s etag2 %s bodies identical %s; changed leaves %s" % (
            path, pairs[-1][0], pairs[-1][1], pairs[-1][3], [(p, str(x)[:60], str(y)[:60]) for p, x, y in diffs[:8]])) if (diffs or pairs[-1][0] != pairs[-1][1]) else None
    head = anon.request("HEAD", path)
    get = anon.get(path, expect=200)
    cond_head = anon.request("HEAD", path, headers={"If-None-Match": head.headers.get("etag", "")})
    cond_get = anon.get(path, headers={"If-None-Match": get.headers.get("etag", "")})
    print("[RECORD] etag-diag %s: HEAD etag %s GET etag %s; conditional HEAD %s conditional GET %s; pairs %s" % (
        path, head.headers.get("etag"), get.headers.get("etag"), cond_head.status, cond_get.status,
        [(e1 == e2, ident) for e1, e2, _d, ident in pairs]))
    for e1, e2, diffs, ident in pairs:
        assert not (ident and e1 != e2), "%s: identical bodies, different ETags (%s vs %s): validator computation defect" % (path, e1, e2)
    # now_ms is the one leaf that moves by design (web.md: the validator is a hash of the body without now_ms). Any other
    # leaf that moves is a real change of the node (an event, a link expiring) and must move the ETag with it; a pair
    # in which only now_ms moved must keep the ETag. Real changes are recorded, they do not fail an idle diagnostic.
    for e1, e2, diffs, _ident in pairs:
        moved = [(p, str(x)[:60], str(y)[:60]) for p, x, y in diffs if p != "/now_ms"]
        if moved:
            print("[RECORD] etag-diag %s: real change in the window %s, ETag %s -> %s" % (path, moved[:6], e1, e2))
            assert e1 != e2, "%s: leaves changed %s but the ETag did not (%s)" % (path, moved[:6], e1)
        else:
            assert e1 == e2, "%s: only now_ms moved but the ETag changed (%s -> %s)" % (path, e1, e2)


def _keys(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            _keys(value, found)
    elif isinstance(node, list):
        for item in node:
            _keys(item, found)
    return found


def test_public_state_has_no_moving_uptime(anon):
    """ETAG1 (6ea130a): the public state carries no uptime_s / node_uptime_s (they moved the validator every second);
    started_ms and node_started_ms stay. The admin status keeping its uptime values is not asserted here (admin route cases)."""
    keys = _keys(json.loads(anon.get("/api/v1/public/state", expect=200).body))
    present = sorted(k for k in ("uptime_s", "node_uptime_s") if k in keys)
    assert not present, "public state still carries %s (ETAG1)" % present
    missing = sorted(k for k in ("started_ms", "node_started_ms") if k not in keys)
    assert not missing, "public state lost %s (ETAG1 keeps them)" % missing


def _find(node, key):
    """First value of `key` anywhere in a JSON tree, else None."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            got = _find(value, key)
            if got is not None:
                return got
    elif isinstance(node, list):
        for item in node:
            got = _find(item, key)
            if got is not None:
                return got
    return None


def test_admin_status_keeps_the_uptime_values(anon, admin_session):
    """ETAG1 / admin.md 'Status fields' (278): GET /api/v1/admin/status, block `live` only (no top-level fallback; a missing block fails): uptime_s and node_uptime_s are present
    and increase between two requests a few seconds apart, started_ms / node_started_ms equal the public state's values,
    uptime_source is present; the response has no ETag and Cache-Control no-store. Role viewer (the lowest floor of the route)."""
    admin = admin_session("viewer")
    path = admin.path("/status")
    first = admin.get(path, expect=200)
    time.sleep(3.0)
    second = admin.get(path, expect=200)
    a, b = json.loads(first.body), json.loads(second.body)
    assert isinstance(a.get("live"), dict) and isinstance(b.get("live"), dict), \
        "admin status has no `live` block (admin.md 'Status fields'): top-level keys %s" % sorted(a)[:20]
    live_a, live_b = a["live"], b["live"]
    for key in ("uptime_s", "node_uptime_s"):
        assert key in live_a and key in live_b, "admin status lacks %s in block live: %s" % (key, sorted(live_a)[:20])
        assert live_b[key] > live_a[key], "%s did not increase: %s then %s" % (key, live_a[key], live_b[key])
    assert "uptime_source" in live_b, "admin status lacks uptime_source"
    public = json.loads(anon.get("/api/v1/public/state", expect=200).body)
    for key in ("started_ms", "node_started_ms"):
        assert live_b.get(key) is not None, "admin status lacks %s" % key
        assert live_b[key] == _find(public, key), "%s differs from the public state: %s vs %s" % (key, live_b[key], _find(public, key))
    assert "etag" not in {k.lower() for k in second.headers}, "admin status carries an ETag: %s" % second.headers.get("etag")
    assert "no-store" in second.headers.get("cache-control", "").lower(), "Cache-Control %r" % second.headers.get("cache-control")


# ================================================================ ETAG1: the strict case, both ways

SETTLE_S = 60.0                 # an earlier case may still be moving the state (links expiring): wait for a quiet pair
SAMPLE_GAP_S = 4.0
IDLE_SAMPLES = 4                # 12 s of idle sampling
CHANGE_S = 25.0                 # a protocol client's change shows up in the public routes within this
TRIGGER_CALL = {"link": "N0PEL", "position": "N0PEP", "over": "N0PEO"}


def _etag_of(resp):
    return resp.headers.get("etag", "")


def settle(anon, path):
    """Polls until two requests 3 s apart carry the same ETag (the node is quiet); returns that ETag, else None after SETTLE_S."""
    end = time.monotonic() + SETTLE_S
    last = _etag_of(anon.get(path, expect=200))
    while time.monotonic() < end:
        time.sleep(3.0)
        current = _etag_of(anon.get(path, expect=200))
        if current and current == last:
            return current
        last = current
    return None


@contextlib.contextmanager
def trigger_link(rig):
    """A DExtra client linked to module B: the public client list (state) changes while it is linked."""
    import data_support as ds
    with ds.dstar_listener(rig.xlxd_sut, module="B", callsign=TRIGGER_CALL["link"]):
        yield


@contextlib.contextmanager
def trigger_position(rig):
    """One M17 packet-mode position (needs the data worker): the room's retained position list changes."""
    import position_support as ps
    import test_dp15_positions as p1
    p1.send_m17(types.SimpleNamespace(rig=rig, rf=None), TRIGGER_CALL["position"], ps.LAT, ps.LON, comment="etag")
    yield


@contextlib.contextmanager
def trigger_over(rig):
    """A D-STAR voice over of 40 frames from a linked DExtra client: the last-heard list gets an entry."""
    import data_support as ds
    base = TRIGGER_CALL["over"]
    with ds.dstar_listener(rig.xlxd_sut, module="B", callsign=base) as talker:
        _sid, packets = ds.build_over(base, ds.plain_fields(40), ds.ambe(40, "etag"))
        ds.send_over(talker, packets)
        ds.pump_for(1.0, talker)
        yield


TRIGGERS = {"link": trigger_link, "position": trigger_position, "over": trigger_over}


def wait_etag_change(anon, path, old, seconds=CHANGE_S):
    """(new response, seconds) once the route's ETag differs from `old`, else (last response, None)."""
    t0 = time.monotonic()
    while True:
        resp = anon.get(path, expect=200)
        if _etag_of(resp) and _etag_of(resp) != old:
            return resp, time.monotonic() - t0
        if time.monotonic() - t0 >= seconds:
            return resp, None
        time.sleep(1.0)


@pytest.mark.parametrize("path,trigger", [("/api/v1/public/state", "link"), ("/api/v1/public/state", "position"),
                                          ("/api/v1/public/lastheard", "over")])
def test_public_etag_idle_and_after_a_real_change(rig, anon, path, trigger):
    # -- 1. an unchanged node: one validator, 304 for it
    base = settle(anon, path)
    assert base, ("%s never had a stable ETag: two requests 3 s apart differed for %.0f s on a node that nothing talks to (ETAG1: the "
                  "validator must not move on an idle node)" % (path, SETTLE_S))
    samples = [anon.get(path, expect=200)]
    for _ in range(IDLE_SAMPLES - 1):
        time.sleep(SAMPLE_GAP_S)
        samples.append(anon.get(path, expect=200))
    etags = [_etag_of(r) for r in samples]
    moved = _walk(json.loads(samples[0].body), json.loads(samples[-1].body))
    print("[RECORD] etag %s idle: etags %s; leaves that moved in the body over %.0f s (not asserted): %s" % (
        path, etags, SAMPLE_GAP_S * (IDLE_SAMPLES - 1), [(p, str(x)[:40], str(y)[:40]) for p, x, y in moved[:8]]))
    assert len(set(etags)) == 1 and etags[0], "%s: the ETag moved on an idle node over %.0f s: %s (leaves that moved: %s)" % (
        path, SAMPLE_GAP_S * (IDLE_SAMPLES - 1), etags, [p for p, _x, _y in moved[:8]])
    assert etags[0] == base, "%s: the settled ETag %s differs from the idle samples %s" % (path, base, etags)
    assert etags[0].startswith("W/"), "%s: the validator is documented as a weak ETag: %r" % (path, etags[0])
    cond = anon.get(path, headers={"If-None-Match": etags[0]})
    assert cond.status == 304 and cond.body == b"" and _etag_of(cond) == etags[0], "%s idle conditional GET: HTTP %d" % (path, cond.status)
    head = anon.request("HEAD", path)
    assert head.status == 200 and _etag_of(head) == etags[0], "%s idle HEAD: HTTP %d etag %r (GET etag %r)" % (path, head.status, _etag_of(head), etags[0])
    cond_head = anon.request("HEAD", path, headers={"If-None-Match": etags[0]})
    assert cond_head.status == 304, "%s idle conditional HEAD: HTTP %d" % (path, cond_head.status)
    # -- 2. a real change by a protocol client: the validator moves, the old one no longer matches
    with TRIGGERS[trigger](rig):
        fresh, seconds = wait_etag_change(anon, path, etags[0])
        if seconds is None and trigger == "position":
            pytest.skip("a position did not change %s within %.0f s: the data worker is probably not on (data-ident.toml)" % (path, CHANGE_S))
        new = _etag_of(fresh)
        print("[RECORD] etag %s after a %s: %s -> %s after %s s" % (path, trigger, etags[0], new, None if seconds is None else round(seconds, 1)))
        assert seconds is not None, "%s: the ETag did not change within %.0f s of a real change (%s by a protocol client): still %s" % (
            path, CHANGE_S, trigger, new)
        old_cond = anon.get(path, headers={"If-None-Match": etags[0]})
        assert old_cond.status == 200 and _etag_of(old_cond) and _etag_of(old_cond) != etags[0], \
            "%s: a conditional GET with the OLD ETag after the change: HTTP %d etag %r" % (path, old_cond.status, _etag_of(old_cond))
        # the NEW validator matches (one retry: a client list refresh may move the state between the two requests)
        for attempt in range(2):
            current = anon.get(path, expect=200)
            new_cond = anon.get(path, headers={"If-None-Match": _etag_of(current)})
            if new_cond.status == 304:
                break
        assert new_cond.status == 304, "%s: a conditional GET with the CURRENT ETag %s after the change: HTTP %d (want 304)" % (
            path, _etag_of(current), new_cond.status)
        old_head = anon.request("HEAD", path, headers={"If-None-Match": etags[0]})
        assert old_head.status == 200, "%s: conditional HEAD with the OLD ETag after the change: HTTP %d (want 200)" % (path, old_head.status)
