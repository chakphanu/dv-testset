# SPDX-License-Identifier: MIT
"""Diagnostic for the public state validator (ETag) on an IDLE node (wip-ac750c7 DP7 question): three pairs of GETs a few seconds apart, the bodies
diffed field by field, the ETags compared, then HEAD / conditional HEAD / conditional GET. Records only; fails when the bodies are identical but
the ETags differ (a validator computation defect) or when a changing field is found (the field name goes to the notes as a FINDING).
Run: the web profile as in docs/web-api-cases.md with cases/test_web_etag_diag.py."""
import json
import time

import pytest

from dvtest import web

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
    assert all(not diffs for _e1, _e2, diffs, _i in pairs), "%s changes on an idle node: %s" % (
        path, [(p, str(x)[:60], str(y)[:60]) for _e1, _e2, diffs, _i in pairs for p, x, y in diffs[:6]][:12])
