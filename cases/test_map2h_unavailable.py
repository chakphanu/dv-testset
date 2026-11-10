# SPDX-License-Identifier: MIT
"""Web unit, MAP2h: worker UNAVAILABILITY is a gauge, not a failure. DV-Router MAP2h (commit 6ef82fd9, on main d20532de) splits the
telemetry of the lazy basemap worker: `dv_basemap_failures_total` counts only failed jobs, and a worker that cannot work reports
`dv_basemap_unavailable{reason}`: three series, always exported, one-hot (at most one is 1, all are 0 while it can work):

  old_index       the region INDEX predates MAP2d ("region INDEX was written before MAP2d; re-run dv-svc-basemap install")
  writer_active   another writer holds <state>/.basemap-install.lock ("another basemap writer is active")
  no_install      there is no <state>/basemap/base/MANIFEST ("no basemap base is installed")

Needs `scripts/run.sh --profile web,mapsrc` like cases/test_map2d_regions.py (docs/map2d-cases.md, "MAP2h"); skips with the reason on an
image without `dv_basemap_unavailable` (before MAP2h). NOT RUN on a rig yet: written against the sources of d20532de (no image yet).

  test_no_install_is_a_reason_not_a_failure   no tree installed: reason no_install is 1 and the other two 0, failures_total did not
                                          move, the source never saw a request
  test_an_old_index_is_a_reason_not_a_failure  a tree whose regions/INDEX predates MAP2d is installed: reason old_index is 1 (and
                                          only it), the worker retries every 5 s tick yet failures_total stays put over 25 s more,
                                          tiles.json says fetch_enabled false and preparing []; the INDEX is restored (atomic
                                          replace of the saved MAP2d bytes): the gauge returns to all 0, the worker rewrites the
                                          INDEX in the MAP2d shape, tiles.json is installed
  test_a_held_install_lock_is_a_reason_not_a_failure  the tester holds flock(LOCK_EX|LOCK_NB) on <state>/.basemap-install.lock (the
                                          sut-state directory is one inode on both sides of the bind mount): reason writer_active is
                                          1 (and only it), failures_total stays put, fetch_enabled false; the lock is released: all 0
                                          again and fetch_enabled true

Recorded, not asserted: the values and the seconds each transition took, the descriptor `installed` flag while the INDEX is old, the
source's request summary. The INDEX swap works on a tree that is installed as a whole (rename of basemap/) while the worker has no
base: its `open` re-reads the INDEX every 5 s tick. Swapping an INDEX under a RUNNING worker is not noticed (the worker re-reads it
only when the MANIFEST identity changes), so the case never does that. Not covered: an INDEX of another version or with unknown keys
is a counted failure by design (not old_index); the generation is never compared.

The ingest lag of dv-svc-status is about 10 s and the worker ticks every 5 s: every transition is polled for up to 45 s
(dvtest.map2d.wait_for_series), and "stays put" is read over a further 25 s. Do not restart xlxd-sut in the run (the mapsrc sidecar
shares its network namespace). The tree is removed, the lock released and the INDEX restored at the end, whatever happens.
"""
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

import position_map2_support as m2  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import map2d, web  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web]

ROOT = Path(__file__).resolve().parents[1]
TRANSITION_S = 45.0                 # tick 5 s + publish 10 s + ingest 10 s, with margin
HOLD_S = 25.0                       # "stays put": five worker ticks and two publishes
INSTALL_S = 30.0
EMPTY = {"reasons": {}, "failures": None, "fetches": None}


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def stats_url():
    if os.environ.get("DVT_MAPSRC", "off") != "on":
        pytest.skip("DVT_MAPSRC is not on: run with --profile web,mapsrc (docs/map2d-cases.md)")
    url = os.environ.get("DVT_MAPSRC_STATS", "")
    if not url:
        pytest.skip("DVT_MAPSRC_STATS is not set")
    try:
        map2d.source_stats(url)
    except OSError as exc:
        pytest.skip("the source's request log %s is not reachable (%s): is the service basemap-src up?" % (url, exc))
    return url


@pytest.fixture(scope="module")
def basemap():
    value = os.environ.get("DVT_BASEMAP_FILE", "").strip()
    if not value:
        pytest.skip("DVT_BASEMAP_FILE is not set: make a tree with scripts/basemap-install-fixture.sh and the dv-svc-basemap of the image under test")
    path = Path(value)
    path = path if path.is_absolute() else ROOT / path
    root = m2.map2_root(path) if path.is_dir() else None
    if root is None:
        pytest.skip("DVT_BASEMAP_FILE=%s holds no base/MANIFEST (a tree written by `dv-svc-basemap install`)" % value)
    index = map2d.read_index(root)
    if not map2d.is_map2d_index(index):
        pytest.skip("%s/regions/INDEX is not in the MAP2d shape: install with the binary of the image under test" % root)
    return types.SimpleNamespace(path=path, root=root, index_bytes=(root / "regions" / "INDEX").read_bytes())


@pytest.fixture(scope="module")
def state_dir(target):
    state = Path(target.out) / "sut-state"
    if not state.is_dir():
        pytest.skip("the SUT state directory %s is not visible in the tester (the web profile mounts out/<run>/sut-state)" % state)
    return state


@pytest.fixture(scope="module")
def control(rig_control):
    return rig_control


def read_snapshot(control):
    """The gauge snapshot now; EMPTY (never falsy) when the read fails, so a wait does not give up on a blip."""
    try:
        return map2d.unavailable_snapshot(control.metrics("xlxd-sut")) or EMPTY
    except (RuntimeError, TimeoutError, ValueError, OSError):
        return EMPTY


@pytest.fixture(scope="module")
def gauge(control):
    """Skips on an image without dv_basemap_unavailable (the worker publishes every 10 s from its start: waited for)."""
    snap, waited, _ok = map2d.wait_for_series(lambda: read_snapshot(control), lambda s: bool(s["reasons"]), timeout=40.0, poll=3.0)
    if not snap["reasons"]:
        pytest.skip("the hub metrics carry no dv_basemap_unavailable after %.0f s: an image before MAP2h (or no basemap worker)" % waited)
    problems = map2d.snapshot_problems(snap)
    assert not problems, "dv_basemap_unavailable is malformed: %s" % "; ".join(problems)
    return snap


@pytest.fixture(scope="module", autouse=True)
def cleanup(state_dir):
    """Whatever the cases did: the lock released, the tree removed (only one this suite installed), nothing left behind."""
    held = []
    yield held
    for lock in held:
        lock.release()
    m2.remove_tree(state_dir)


def descriptor(anon):
    resp = anon.get(pages.BASEMAP_TILES_JSON, pace=False)
    try:
        return resp, resp.json()
    except web.WebError:
        return resp, None


def wait_reason(control, reason):
    """(snapshot, seconds waited, reached) for the gauge to be one-hot on `reason` (None = all zero)."""
    return map2d.wait_for_series(lambda: read_snapshot(control), lambda s: map2d.reason_is(s, reason), timeout=TRANSITION_S, poll=3.0)


def assert_reached(snap, waited, reached, reason, label):
    assert reached, "%s: after %.0f s the gauge is %s (failures_total %s), want %s" % (
        label, waited, snap["reasons"], snap["failures"], "only %s at 1" % reason if reason else "all three at 0")


def failures_stay(control, baseline, label, seconds=HOLD_S):
    """Reads the gauge snapshot for `seconds` and asserts failures_total never rose above `baseline`. Returns the last snapshot."""
    end = time.monotonic() + seconds
    snap = read_snapshot(control)
    while True:
        if snap["failures"] is not None:
            assert snap["failures"] <= baseline, "%s: dv_basemap_failures_total rose from %s to %s while the worker was only unavailable" % (label, baseline, snap["failures"])
        if time.monotonic() >= end:
            return snap
        time.sleep(5.0)
        snap = read_snapshot(control)


def source_quiet(stats_url, label):
    _rows, summary = map2d.source_stats(stats_url)
    assert summary["total"] == 0, "%s: the source saw %d request(s): an unavailable worker must not start a job: %s" % (label, summary["total"], summary)
    return summary


def record(text):
    print("[RECORD] map2h: " + text)


# ---------------------------------------------------------------- cases

def test_no_install_is_a_reason_not_a_failure(gauge, control, state_dir, web_client, stats_url):
    anon = web_client()
    if (state_dir / "basemap").exists():
        pytest.skip("%s exists already: the cases do not replace an installed tree" % (state_dir / "basemap"))
    snap, waited, reached = wait_reason(control, "no_install")
    assert_reached(snap, waited, reached, "no_install", "no tree installed")
    baseline = snap["failures"] or 0.0
    snap = failures_stay(control, baseline, "no tree installed")
    assert map2d.reason_is(snap, "no_install"), "no_install did not hold for %.0f s: %s" % (HOLD_S, snap["reasons"])
    summary = source_quiet(stats_url, "no tree installed")
    resp, body = descriptor(anon)
    record("no_install after %.0f s, held %.0f s; failures_total %s; fetches_total %s; tiles.json %d %s; source %s" % (
        waited, HOLD_S, snap["failures"], snap["fetches"], resp.status, body, summary))


def test_an_old_index_is_a_reason_not_a_failure(gauge, control, basemap, state_dir, web_client, stats_url):
    anon = web_client()
    if (state_dir / "basemap").exists():
        pytest.skip("%s exists already: the cases do not replace an installed tree" % (state_dir / "basemap"))
    old = map2d.old_index_bytes(basemap.index_bytes)
    assert map2d.is_pre_worker_index(json.loads(old)), "the synthetic old INDEX is not in the pre-MAP2d shape"
    baseline = read_snapshot(control)["failures"] or 0.0
    m2.install_tree(state_dir, basemap.path, index_bytes=old)
    index_path = state_dir / "basemap" / "regions" / "INDEX"
    try:
        snap, waited, reached = wait_reason(control, "old_index")
        assert_reached(snap, waited, reached, "old_index", "INDEX written before MAP2d")
        held = failures_stay(control, baseline, "INDEX written before MAP2d")
        assert map2d.reason_is(held, "old_index"), "old_index did not hold for %.0f s: %s" % (HOLD_S, held["reasons"])
        assert map2d.is_pre_worker_index(map2d.read_index(state_dir / "basemap")), "the worker rewrote the old INDEX: it must stay refused until `install` is re-run"
        resp, body = descriptor(anon)
        if resp.status == 200 and isinstance(body, dict) and body.get("installed") is True:
            assert body.get("fetch_enabled") is False and body.get("preparing") == [], \
                "tiles.json says fetch_enabled %r, preparing %r with no live worker" % (body.get("fetch_enabled"), body.get("preparing"))
        summary = source_quiet(stats_url, "INDEX written before MAP2d")
        record("old_index after %.0f s, held %.0f s; failures_total %s (baseline %s); tiles.json %d installed=%r; source %s" % (
            waited, HOLD_S, held["failures"], baseline, resp.status, body.get("installed") if isinstance(body, dict) else None, summary))
    finally:
        map2d.replace_file(index_path, basemap.index_bytes)               # restored whatever happened
    snap, waited, reached = wait_reason(control, None)
    assert_reached(snap, waited, reached, None, "INDEX restored")
    end = time.monotonic() + INSTALL_S
    while True:
        resp, body = descriptor(anon)
        if resp.status == 200 and isinstance(body, dict) and body.get("installed") is True:
            break
        if time.monotonic() >= end:
            pytest.fail("tiles.json still says %d %s %.0f s after the INDEX was restored" % (resp.status, body, INSTALL_S))
        time.sleep(1.0)
    index = map2d.read_index(state_dir / "basemap")
    assert map2d.is_map2d_index(index) and not map2d.index_problems(index), "after the restore the INDEX is %s" % map2d.index_problems(index)
    held = failures_stay(control, baseline, "INDEX restored", seconds=10.0)
    record("INDEX restored: gauge all 0 after %.0f s; failures_total %s; INDEX fetch_enabled %r" % (waited, held["failures"], index["fetch_enabled"]))


def test_a_held_install_lock_is_a_reason_not_a_failure(gauge, control, state_dir, web_client, stats_url, cleanup):
    anon = web_client()
    resp, body = descriptor(anon)
    if not (resp.status == 200 and isinstance(body, dict) and body.get("installed") is True):
        pytest.skip("no installed tree is served (the old-INDEX case did not leave one): tiles.json %d %s" % (resp.status, body))
    snap, waited, reached = wait_reason(control, None)
    assert_reached(snap, waited, reached, None, "before the lock is taken")
    baseline = snap["failures"] or 0.0
    lock = map2d.HeldLock(state_dir)
    cleanup.append(lock)
    lock.acquire_retry(timeout=30.0)
    t0 = time.monotonic()
    try:
        snap, waited, reached = wait_reason(control, "writer_active")
        assert_reached(snap, waited, reached, "writer_active", "install lock held")
        held = failures_stay(control, baseline, "install lock held")
        assert map2d.reason_is(held, "writer_active"), "writer_active did not hold for %.0f s: %s" % (HOLD_S, held["reasons"])
        end = time.monotonic() + 20.0
        while True:                                     # the dropped lease: web finds no live worker (its probe runs per request)
            resp, body = descriptor(anon)
            if isinstance(body, dict) and body.get("fetch_enabled") is False:
                break
            if time.monotonic() >= end:
                pytest.fail("tiles.json still says fetch_enabled %r with the install lock held for %.0f s" % (body and body.get("fetch_enabled"), time.monotonic() - t0))
            time.sleep(1.0)
        assert body.get("preparing") == [], "tiles.json lists preparing %r with no live worker" % (body.get("preparing"),)
        summary = source_quiet(stats_url, "install lock held")
        record("writer_active after %.0f s, held %.0f s in all; failures_total %s (baseline %s); tiles.json fetch_enabled %r; source %s" % (
            waited, time.monotonic() - t0, held["failures"], baseline, body.get("fetch_enabled"), summary))
    finally:
        lock.release()
    snap, waited, reached = wait_reason(control, None)
    assert_reached(snap, waited, reached, None, "install lock released")
    end = time.monotonic() + 30.0
    while True:
        resp, body = descriptor(anon)
        if isinstance(body, dict) and body.get("fetch_enabled") is True:
            break
        if time.monotonic() >= end:
            web.finding("tiles.json still says fetch_enabled %r 30 s after the install lock was released (the worker is available again: the gauge is all 0)" % (body and body.get("fetch_enabled"),))
            break
        time.sleep(1.0)
    held = failures_stay(control, baseline, "install lock released", seconds=10.0)
    record("lock released: gauge all 0 after %.0f s; fetch_enabled %r; failures_total %s" % (waited, body.get("fetch_enabled") if isinstance(body, dict) else None, held["failures"]))
