# SPDX-License-Identifier: MIT
"""BUD1 / BUD1b (DV-Router a15b08fa and 364a5760, in bc9af2b1, image dv-router:wip-f1f3777): byte budgets before decode for every inter-process body; aggregated hub replies bounded by definition
with typed truncation. Documents: status.md (dv_fabric_body_rejected_total, dv_status_body_rejected_total, the `truncated` object), fabric.md "bodies" (status_bounds), openapi-admin.yaml `Truncation`;
docs/bud1-cases.md. Checker for a whole run or a chain: `python3 -m dvtest.bud1 out/<run> ...` (dvtest/bud1.py, the session hook dvtest/bud1_plugin.py). Switch DVT_BUD1=on.
NOT RUN on a rig: written against the sources of bc9af2b1.

  BUD1-a  no legitimate body is refused: right now every refusal counter of the hub is 0 in every process and every process that publishes carries the complete adapter family (K6b's "the exact
          process set"); the process set is recorded. The same judgement runs at the end of EVERY pytest session with DVT_BUD1=on (final snapshot + the samples of the run).
  BUD1-b  a normal admin reply carries no `truncated` key: /status, /rooms, /clients (step-up), /lastheard are read and each of their `total` fields is at least the returned items (needs the web
          profile with admin.toml)
  BUD1-c  "and N more": the admin config validate of a TOML with more than 32 diagnostics answers 422 with `errors` cut to 32 and `truncated.errors.dropped` >= 1 and `cut` = 32 (the bound of
          crates/lib/fabric/src/admin/bounds.rs); 40 invalid `access.allow` entries give 40 diagnostics (dv-router answered 2026-11-22, Q8: one error per invalid entry, validation does not stop at the first)
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import bud1, metrics  # noqa: E402

import data_support as ds  # noqa: E402

pytestmark = [pytest.mark.rig]

LOG = "bud1.txt"
SETTLE_S = 12.0
ERRORS_CAP = 32


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target):
    if not bud1.strict_on():
        pytest.skip("DVT_BUD1=on is not set (the image under test has BUD1 / BUD1b, DV-Router a15b08fa + 364a5760: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


def test_bud1_a_no_body_is_refused_now(rig_control, target):
    case = "BUD1-a"
    time.sleep(SETTLE_S)                                              # the last records of the suite so far (10 s telemetry period)
    samples = metrics.parse(rig_control.metrics("xlxd-sut"))
    assert any(s.name == bud1.FAMILY for s in samples), "%s: no %s in the hub metrics: the image does not have BUD1" % (case, bud1.FAMILY)
    bad = bud1.refusals(samples)
    gaps = bud1.family_gaps(samples)
    note(target, case, "RECORD", "%d processes export metrics: %s" % (len(bud1.procs_of(samples)), sorted(bud1.procs_of(samples))))
    assert not bad, "%s: a body was refused by its byte budget in a legitimate run: %s" % (case, [(r.name, r.labels, r.value) for r in bad])
    assert not gaps, "%s: processes that publish without the complete %s family (K6b fails these): %s" % (case, bud1.FAMILY, gaps)
    if not any(s.name == bud1.STATUS_FAMILY for s in samples):
        note(target, case, "FINDING", "no %s series (the hub's own counter; K6b reads exactly one)" % bud1.STATUS_FAMILY)


@pytest.fixture(scope="module")
def viewer(admin_session):
    return admin_session("viewer")


def _truncated(body):
    return isinstance(body, dict) and "truncated" in body


def test_bud1_b_a_normal_admin_reply_is_not_truncated(viewer, target):
    case = "BUD1-b"
    for suffix, step_up in (("/status", False), ("/rooms", False), ("/lastheard?limit=1000", False), ("/clients?limit=1000", True)):
        if step_up:
            viewer.step_up()
        resp = viewer.get(viewer.path(suffix), expect=200)
        body = resp.json()
        assert not _truncated(body), "%s: %s carries `truncated` on a small node: %s" % (case, suffix, json.dumps(body.get("truncated"))[:200])
        if isinstance(body, dict) and isinstance(body.get("items"), list) and "total" in body:
            assert body["total"] >= len(body["items"]), "%s: %s total %s is below the %d items returned" % (case, suffix, body["total"], len(body["items"]))
        note(target, case, "RECORD", "%s: HTTP 200, %d bytes, no truncated" % (suffix, len(resp.body)))


def probe_toml(entries):
    """A configuration with `entries` invalid access entries (an entry must be printable ASCII without blanks or '#': config.md) added to the current one's tail."""
    return "\n[access]\nallow = [%s]\n" % ", ".join('"BAD#%d"' % i for i in range(entries))


def test_bud1_c_and_n_more_on_config_diagnostics(admin_session, target):
    case = "BUD1-c"
    admin = admin_session("admin")
    admin.step_up()
    raw = admin.get(admin.path("/config/raw"), expect=200)
    text, etag = raw.text(), raw.headers.get("etag", "")
    assert etag, "%s: GET config/raw sent no ETag" % case
    # a clean validate first: no truncated key on a normal reply
    admin.step_up()
    ok = admin.request("PUT", admin.path("/config/raw") + "?mode=validate&comment=bud1", raw_body=text.encode("utf-8"), headers={"Content-Type": "text/plain", "If-Match": etag})
    assert ok.status == 200, "%s: validate of the unchanged config answered HTTP %d" % (case, ok.status)
    assert not _truncated(ok.json()), "%s: a normal validate reply carries `truncated`" % case
    if "[access]" in text:
        pytest.skip("the rendered config already has an [access] table: the probe would be a duplicate table, not a list of errors (docs/bud1-cases.md)")
    admin.step_up()
    resp = admin.request("PUT", admin.path("/config/raw") + "?mode=validate&comment=bud1", raw_body=(text + probe_toml(ERRORS_CAP + 8)).encode("utf-8"),
                         headers={"Content-Type": "text/plain", "If-Match": etag})
    body = resp.json() if resp.body else {}
    errors = body.get("errors") if isinstance(body, dict) else None
    note(target, case, "RECORD", "probe with %d invalid access entries: HTTP %d, errors %s, truncated %s" % (
        ERRORS_CAP + 8, resp.status, None if errors is None else len(errors), json.dumps(body.get("truncated"))[:120] if isinstance(body, dict) else None))
    assert resp.status == 422, "%s: a configuration with invalid access entries answered HTTP %d, not 422" % (case, resp.status)
    assert isinstance(errors, list) and errors, "%s: the 422 body has no `errors` list" % case
    assert len(errors) == ERRORS_CAP, "%s: %d errors returned for 40 invalid entries, want the list cut to exactly %d (a shorter list means the validator stopped early)" % (case, len(errors), ERRORS_CAP)
    assert len(errors) <= ERRORS_CAP, "%s: %d errors returned: the list must be cut to %d (bounds.rs)" % (case, len(errors), ERRORS_CAP)
    trunc = (body.get("truncated") or {}).get("errors") if isinstance(body, dict) else None
    assert trunc and trunc.get("dropped", 0) >= 1 and trunc.get("cut") == ERRORS_CAP, "%s: errors cut to %d but truncated.errors is %s" % (case, len(errors), trunc)
