# SPDX-License-Identifier: MIT
"""Web unit, MAP2d: the lazy basemap region worker of DV-Router (`dv-svc-basemap serve`, a launcher-supervised child) against a
LOCAL source. Needs `scripts/run.sh --profile web,mapsrc` (the loopback source in the SUT's network namespace, the request log on
DVT_MAPSRC_STATS) with the data worker on (data-ident.toml); every case skips with the reason otherwise. Case table, run command
and what is asserted versus only recorded: docs/map2d-cases.md. NOT RUN on a rig yet: written against DV-Router 15b07b8 (image
wip-f9aaebc): crates/svc/basemap/src/{core/worker.rs,shell/worker.rs,shell/service.rs}, docs/design/deployment.md "Automatic detail
regions (MAP2d)", config.md "[basemap]".

The worker watches the public positions of the hub every 5 s, takes the z10 cell of each one outside the installed detail
coverage and extracts its detail zooms (overview_maxzoom+1 .. detail_maxzoom of the MANIFEST) from the ONE source archive with
HEAD, ranged GETs and If-Match, then publishes `regions/r-10-<x>-<y>-<build>.pmtiles` and a new `regions/INDEX`; dv-svc-web's
tiles.json `gen` is a hash over the listed archives, so it changes. The source is dvtest/basemap_fixture.py: one archive with the
chains of tiles of Bangkok (the installed detail box), Chiang Mai and Phuket (cells outside it). `daily_regions = 1` (the snippet
map2d-source.toml) makes the daily cap observable.

  test_hidden_and_ambiguous_positions_start_no_job   an account with hide_position (N0HIDE) and APRS positions of ambiguity 3 and 4
                                          (public rows: the worker sees them) are outside the installed coverage, yet after 30 s
                                          (six worker cycles) the source saw NO request at all, the INDEX lists no region and has
                                          day_regions 0 with an unchanged generation
  test_a_public_position_outside_coverage_publishes_a_region  the same position style at ambiguity 0 in Chiang Mai: before, its
                                          z12 tile is absent (204) under the base generation; then within 180 s a region of cell
                                          10/<x>/<y> is listed in the INDEX (MAP2d shape, ready, archive file present with the
                                          recorded size and sha256), tiles.json gen changed, the source saw exactly ONE job (one
                                          builds.json, one HEAD, ranged GETs only, 206 each with If-Match, no whole-archive GET, no
                                          glyph request), and the z12 tile is served 200 gzip MVT from the region; the tile carries
                                          the coordinates the fixture wrote (so it is that tile)
  test_the_daily_cap_holds                the cell of Phuket (public, ambiguity 0, tiles exist in the source): 30 s later still ONE
                                          job, the INDEX has the one region and day_regions 1, gen unchanged, the tile is still 204;
                                          cap_hit_total >= 1 when the exporter carries dv_basemap_* (recorded otherwise)

Recorded, not asserted: the dv_basemap_* telemetry (the hub endpoint may not export it), the INDEX counters (day_bytes,
generation), the descriptor keys after the publish, the status of the old generation's tile URL, the time to publish.

The tree: DVT_BASEMAP_FILE names a tree made by a MAP2d-or-later `dv-svc-basemap install` (scripts/basemap-install-fixture.sh with
the binary of the image under test): the INDEX must be in the MAP2d shape (an older one is refused by the worker: the case skips
with that reason). It is installed like the DP7 case does (position_map2_support.install_tree) and removed at the end.
"""
import hashlib
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

import data_support as ds  # noqa: E402
import position_map2_support as m2  # noqa: E402
import position_support as ps  # noqa: E402
import web_pages as pages  # noqa: E402
from dvtest import basemap_fixture as fx, map2d, web  # noqa: E402
from dvtest.proto import m17  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web]

ROOT = Path(__file__).resolve().parents[1]
ROOM = "C"                                  # an own room, as the DP7 map case
HIDE_CALL = "N0HIDE"                        # the account of the hide_position step
# (callsign, lat, lon, ambiguity) -- every one outside the installed detail box dvt=99.9,13.0,101.1,14.1
HIDDEN = (HIDE_CALL, 16.43, 102.83)         # Khon Kaen; the account has hide_position
AMBIGUOUS = [("N0MA3", 15.24, 104.85, 3), ("N0MA4", 8.43, 99.96, 4)]   # Ubon, Nakhon Si Thammarat
REGION = ("N0MRG", 18.79, 98.98)            # Chiang Mai: a cell with tiles in the source
CAPPED = ("N0MCP", 7.88, 98.39)             # Phuket: tiles in the source, so a wrongly admitted cell would publish
QUIET = {}                                  # the tiles.json snapshot after the quiet period, for the next case
SETTLE_S = 30.0                             # six 5 s worker cycles: "nothing happens" is watched this long
PUBLISH_S = 180.0
INSTALL_S = 30.0
PUBLIC_S = 45.0


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
        pytest.skip("%s/regions/INDEX is not in the MAP2d shape (\"region INDEX was written before MAP2d; re-run dv-svc-basemap install\"): "
                    "install with the binary of the image under test" % root)
    return types.SimpleNamespace(path=path, root=root, manifest=m2.read_manifest2(root))


@pytest.fixture(scope="module")
def state_dir(target):
    state = Path(target.out) / "sut-state"
    if not state.is_dir():
        pytest.skip("the SUT state directory %s is not visible in the tester (the web profile mounts out/<run>/sut-state)" % state)
    return state


def descriptor(anon):
    resp = anon.get(pages.BASEMAP_TILES_JSON, pace=False)
    try:
        return resp, resp.json()
    except web.WebError:
        return resp, None


def snapshot(anon):
    """(ETag, descriptor) of tiles.json now."""
    resp, body = descriptor(anon)
    assert resp.status == 200 and isinstance(body, dict), "GET %s answered %d" % (pages.BASEMAP_TILES_JSON, resp.status)
    return resp.headers.get("etag", ""), body


def assert_shape(desc, label):
    """The MAP2d descriptor: preparing required with its specified shape, fetch_enabled a boolean (cases/position_map2_support.py)."""
    problems = m2.descriptor_problems(desc, map2d=True)
    assert not problems, "%s: tiles.json descriptor: %s" % (label, "; ".join(problems[:4]))


def assert_relation(before, after, label):
    """The ETag follows the descriptor; a status-only change leaves gen and the tile URLs alone. Returns the kind."""
    kind, problems = map2d.descriptor_relation(before, after)
    assert not problems, "%s: %s" % (label, "; ".join(problems))
    return kind


@pytest.fixture(scope="module")
def web_ctx(web_operator, web_user, web_client):
    return types.SimpleNamespace(op=web_operator, user=web_user, anon=web_client)


@pytest.fixture(scope="module")
def installed(basemap, state_dir, web_client, stats_url):
    """The tree installed under out/<run>/sut-state/basemap (never over an existing one), tiles.json says installed: true, the
    source has seen nothing yet. Yields (manifest, first descriptor, gen, its ETag)."""
    anon = web_client()
    if descriptor(anon)[1] and descriptor(anon)[1].get("installed") is True:
        pytest.skip("a basemap is already served by this run: the cases do not replace an installed one")
    boxes = map2d.detail_boxes(m2.read_manifest2(basemap.root))
    for call, lat, lon, *_level in [HIDDEN] + AMBIGUOUS + [REGION, CAPPED]:
        if map2d.cell_covered(fx.cell_of(lat, lon), boxes):
            pytest.skip("the cell %s of %s lies wholly inside a detail tier of the installed base %s: the worker never fetches a covered cell "
                        "(install with the script's default box dvt=99.9,13.0,101.1,14.1, not the Thailand box)" % (fx.cell_of(lat, lon), call, boxes))
    _rows, before = map2d.source_stats(stats_url)
    if before["total"]:
        pytest.skip("the source already saw %d request(s) before the tree was installed: not a fresh run" % before["total"])
    try:
        manifest = m2.install_tree(state_dir, basemap.path)
    except (ValueError, OSError) as exc:
        pytest.fail("cannot install the provided basemap %s: %s" % (basemap.path, exc))
    try:
        end = time.monotonic() + INSTALL_S
        while True:
            resp, body = descriptor(anon)
            if resp.status == 200 and isinstance(body, dict) and body.get("installed") is True:
                break
            if time.monotonic() >= end:
                pytest.fail("GET %s still answers %d %s %.0f s after the tree was installed" % (pages.BASEMAP_TILES_JSON, resp.status, json.dumps(body)[:100], INSTALL_S))
            time.sleep(1.0)
        yield manifest, body, body["gen"], resp.headers.get("etag", "")
    finally:
        m2.remove_tree(state_dir)


@pytest.fixture(scope="module")
def control(rig_control):
    """The control channel; the hub's Prometheus text is read through it (dv_basemap_* if the exporter carries it)."""
    return rig_control


def note_series(series, where):
    """The hub endpoint is expected to carry dv_basemap_* (dv-svc-status ingests the basemap telemetry kind, every 10 s); a run
    without them is a finding, not a failure of the worker case."""
    if not series:
        web.finding("%s: the hub metrics carry no dv_basemap_* series (expected from dv-svc-status, status/src/metrics.rs)" % where)
    return series


METRICS_WAIT_S = 25.0                       # dv-svc-status ingests the worker's telemetry every 10 s: a counter lags its event


def wait_series(control, ready, where):
    """The dv_basemap_* series once `ready(series)` holds (up to METRICS_WAIT_S), the seconds waited, and whether it held."""
    series, waited, ok = map2d.wait_for_series(lambda: basemap_series(control), ready, timeout=METRICS_WAIT_S)
    note_series(series, where)
    return series, waited, ok


def basemap_series(control):
    try:
        return map2d.basemap_metrics(control.metrics("xlxd-sut"))
    except (RuntimeError, TimeoutError, ValueError, OSError):
        return None


# ---------------------------------------------------------------- helpers

def send_position(rig, call, lat, lon, level=0):
    """One APRS position as an M17 packet-mode report from a client linked to the room (as cases/test_dp7_positions_page.py)."""
    info = ps.position_info(lat, lon, level, "/>", "map2d")
    packet = m17.m17p_packet(0x02, ps.tnc2(call + "-7", info).encode("ascii"), src=call + "-7")
    with ds.m17_client(rig.xlxd_sut, callsign=call, module=ROOM) as sender:
        sender.client.send(sender.dest, packet)
        ds.pump_for(1.0, sender)


def public_row(anon, call):
    row, _rows = ps.wait_public_row(anon, call, PUBLIC_S, room=ROOM)
    return row


def set_hide_position(web_ctx, callsign, value):
    resp = web_ctx.op.request("PATCH", "/api/v1/operator/accounts/%s" % callsign, json_body={"hide_position": bool(value)}, expect=None)
    assert resp.status == 200, "PATCH account %s hide_position=%s: HTTP %d" % (callsign, value, resp.status)
    account = web_ctx.op.get("/api/v1/operator/accounts/%s" % callsign, expect=200).json()
    assert account.get("hide_position") is bool(value), "the account reads hide_position=%r after the PATCH" % (account.get("hide_position"),)


def watch_quiet(stats_url, seconds, expect_jobs):
    """Polls the source's log for `seconds`; returns the last summary. Fails at once if a job beyond `expect_jobs` starts."""
    end = time.monotonic() + seconds
    while True:
        _rows, summary = map2d.source_stats(stats_url)
        assert map2d.jobs(summary) <= expect_jobs and summary["head"] <= expect_jobs and summary["builds"] <= expect_jobs, \
            "the worker started a job (%d builds.json, %d HEAD) where %d were expected: %s" % (summary["builds"], summary["head"], expect_jobs, summary)
        if time.monotonic() >= end:
            return summary
        time.sleep(2.0)


def tile_at(anon, gen, lat, lon, zoom):
    z, x, y = map2d.tile_for(lat, lon, zoom)
    return anon.get(map2d.tile_path(gen, z, x, y), pace=False), (z, x, y)


def record(text):
    print("[RECORD] map2d: " + text)


# ---------------------------------------------------------------- cases

def test_hidden_and_ambiguous_positions_start_no_job(rig, installed, web_ctx, state_dir, stats_url, control):
    manifest, desc0, gen, etag0 = installed
    anon = web_ctx.anon()
    # a hidden station: the account exists and hide_position is set before its position is sent
    web_ctx.user(HIDE_CALL, may_tx=False)
    set_hide_position(web_ctx, HIDE_CALL, True)
    send_position(rig, *HIDDEN)
    time.sleep(5.0)
    assert ps.wait_gone(anon, HIDE_CALL, 10.0, room=ROOM), "the hidden account's position is public: hide_position is not in force"
    # ambiguity 3 and 4: public rows, which the worker sees but must not act on (ambiguity <= 2 only)
    for call, lat, lon, level in AMBIGUOUS:
        send_position(rig, call, lat, lon, level)
        row = public_row(anon, call)
        assert row is not None, "%s (ambiguity %d) never became public in room %s" % (call, level, ROOM)
        assert row["body"].get("ambiguity") == level, "%s: ambiguity %r in the public row, want %d" % (call, row["body"].get("ambiguity"), level)
    index0 = map2d.read_index(state_dir / "basemap")
    summary = watch_quiet(stats_url, SETTLE_S, expect_jobs=0)
    assert summary["total"] == 0, "the source saw %d request(s) for hidden / ambiguous positions: %s" % (summary["total"], summary)
    index = map2d.read_index(state_dir / "basemap")
    assert map2d.is_map2d_index(index) and not map2d.index_problems(index), map2d.index_problems(index)
    assert map2d.region_cells(index) == [] and index["day_regions"] == 0, \
        "INDEX after the quiet period: regions %s, day_regions %s" % (map2d.region_cells(index), index["day_regions"])
    assert index["generation"] == index0["generation"], \
        "INDEX generation moved from %s to %s without a region" % (index0["generation"], index["generation"])
    after = snapshot(anon)
    assert after[1].get("gen") == gen, "tiles.json gen changed (%s -> %s) without a region" % (gen, after[1].get("gen"))
    assert_shape(after[1], "after the quiet period")
    kind0 = assert_relation((etag0, desc0), after, "tiles.json after the quiet period")
    QUIET["snapshot"] = after
    series = note_series(basemap_series(control), "hidden/ambiguous")
    record("hidden + ambiguity 3/4 quiet for %.0f s; source requests %d; INDEX day_regions %s; tiles.json since the install: %s (ETag %s -> %s, "
           "fetch_enabled %r -> %r, %s); dv_basemap_* %s" % (
               SETTLE_S, summary["total"], index["day_regions"], kind0, etag0, after[0], desc0.get("fetch_enabled"), after[1].get("fetch_enabled"),
               m2.optional_notes(after[1]), series if series else "not exported by the hub endpoint"))
    if series:
        assert series.get("dv_basemap_fetches_total", 0) == 0 and series.get("dv_basemap_regions", 0) == 0, series


def test_a_public_position_outside_coverage_publishes_a_region(rig, installed, web_ctx, state_dir, stats_url, control):
    manifest, desc_first, gen0, etag_first = installed
    anon = web_ctx.anon()
    call, lat, lon = REGION
    zoom = manifest["detail_maxzoom"]
    cell = fx.cell_of(lat, lon)
    before, _tile = tile_at(anon, gen0, lat, lon, zoom)
    assert before.status == 204, "the z%d tile of %s answers %d under the base generation, want 204 (outside coverage)" % (zoom, cell, before.status)
    generation0 = map2d.read_index(state_dir / "basemap")["generation"]
    t0 = time.monotonic()
    send_position(rig, call, lat, lon)
    # poll fast from the send on: a cell is in `preparing` only for the length of one job (about a second), and every pair of
    # consecutive reads must obey the ETag / generation / tile URL relation (a status-only change moves the ETag alone)
    index, desc, kinds, preparing_seen = None, None, [], False
    prev = (etag_first, desc_first)
    end = time.monotonic() + PUBLISH_S
    while time.monotonic() < end:
        index = map2d.read_index(state_dir / "basemap")
        now = snapshot(anon)
        desc = now[1]
        kinds.append(assert_relation(prev, now, "tiles.json while the region is fetched"))
        prev = now
        preparing_seen = preparing_seen or (isinstance(desc.get("preparing"), list) and cell in desc["preparing"])
        if map2d.is_map2d_index(index) and cell in map2d.region_cells(index) and desc.get("gen") not in (None, gen0):
            break
        time.sleep(0.5)
    assert public_row(anon, call) is not None, "%s never became public in room %s" % (call, ROOM)
    status_only = kinds.count("status-only")
    took = time.monotonic() - t0
    assert map2d.is_map2d_index(index) and cell in map2d.region_cells(index), \
        "no region %s in the INDEX %.0f s after the position was public (INDEX: %s)" % (cell, PUBLISH_S, json.dumps(index)[:300])
    assert not map2d.index_problems(index), map2d.index_problems(index)
    assert map2d.region_cells(index) == [cell], \
        "the INDEX lists %s, want only %s (a hidden or ambiguous cell admitted first would have taken the day's only slot)" % (map2d.region_cells(index), cell)
    (entry,) = [r for r in index["regions"] if r["cell"] == cell]
    archive = state_dir / "basemap" / "regions" / entry["archive"]
    assert archive.is_file() and archive.stat().st_size == entry["bytes"], "%s missing or not %d bytes" % (archive, entry["bytes"])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == entry["sha256"], "sha256 of %s differs from the INDEX" % archive.name
    assert entry["build"] == manifest["tiers"][0]["build"] and entry["schema"] == manifest["schema"], \
        "region build/schema %s/%s, the base has %s/%s" % (entry["build"], entry["schema"], manifest["tiers"][0]["build"], manifest["schema"])
    assert index["day_regions"] == 1 and index["generation"] > generation0, \
        "INDEX day_regions %s generation %s (was %s)" % (index["day_regions"], index["generation"], generation0)
    gen1 = desc["gen"]
    assert gen1 != gen0 and len(gen1) == 16, "tiles.json gen %s -> %s" % (gen0, gen1)
    published = snapshot(anon)
    assert_shape(published[1], "after the publish")
    kind1 = assert_relation(QUIET.get("snapshot") or (etag_first, desc_first), published, "tiles.json across the publish")
    assert kind1 == "new-generation", "tiles.json across the publish is a %s change, want a new generation" % kind1
    QUIET["published"] = published
    _rows, summary = map2d.source_stats(stats_url)
    problems = map2d.job_problems(summary, 1)
    assert not problems, "source traffic: %s (%s)" % ("; ".join(problems), summary)
    resp, (z, x, y) = tile_at(anon, gen1, lat, lon, zoom)
    assert resp.status == 200, "GET the z%d tile of %s under gen %s answers %d, want 200 (served from the region)" % (zoom, cell, gen1, resp.status)
    assert resp.headers.get("content-type") == m2.MVT_TYPE and resp.headers.get("content-encoding") == "gzip", \
        "tile headers: %r / %r" % (resp.headers.get("content-type"), resp.headers.get("content-encoding"))
    assert map2d.tile_marker(resp.body) == "%d/%d/%d" % (z, x, y), "the tile body is not the fixture tile %d/%d/%d" % (z, x, y)
    old = anon.get(map2d.tile_path(gen0, z, x, y), pace=False)
    series, waited, ok = wait_series(control, lambda v: v.get("dv_basemap_fetches_total", 0) >= 1 and v.get("dv_basemap_regions", 0) >= 1, "region")
    record("region %s published %.0f s after the position was public: %d bytes, INDEX generation %s day_regions %s day_bytes %s, gen %s -> %s, "
           "old-generation tile URL now %d; descriptor keys %s, %s; the cell %s in `preparing` while the job ran; %d status-only change(s) between consecutive "
           "reads (none seen means no moment without a gen change was caught, the relation is still asserted on every pair); source %s; dv_basemap_* %s" % (
               cell, took, entry["bytes"], index["generation"], index["day_regions"], index["day_bytes"], gen0, gen1, old.status,
               sorted(desc), m2.optional_notes(desc), "WAS SEEN" if preparing_seen else "was not seen", status_only, json.dumps(summary, sort_keys=True), (("%s after %.0f s of waiting" % (series, waited)) if series else "not exported by the hub endpoint")))
    if series:
        assert ok, "after %.0f s of waiting the telemetry still reads %s, want dv_basemap_fetches_total >= 1 and dv_basemap_regions >= 1" % (waited, series)


def test_the_daily_cap_holds(rig, installed, web_ctx, state_dir, stats_url, control):
    manifest, _desc, _gen, _etag = installed
    anon = web_ctx.anon()
    index0 = map2d.read_index(state_dir / "basemap")
    if not (map2d.is_map2d_index(index0) and index0["day_regions"] == 1 and len(index0["regions"]) == 1):
        pytest.skip("the region case did not leave exactly one region and day_regions 1 (INDEX: %s)" % json.dumps(index0)[:200])
    gen1 = descriptor(anon)[1]["gen"]
    call, lat, lon = CAPPED
    cell = fx.cell_of(lat, lon)
    send_position(rig, call, lat, lon)
    assert public_row(anon, call) is not None, "%s never became public in room %s" % (call, ROOM)
    summary = watch_quiet(stats_url, SETTLE_S, expect_jobs=1)
    index = map2d.read_index(state_dir / "basemap")
    assert cell not in map2d.region_cells(index), "the cell %s was published although daily_regions is 1" % cell
    assert index["day_regions"] == 1 and len(index["regions"]) == 1 and index["generation"] == index0["generation"], \
        "INDEX day_regions %s, %d region(s), generation %s -> %s" % (index["day_regions"], len(index["regions"]), index0["generation"], index["generation"])
    now = snapshot(anon)
    assert now[1].get("gen") == gen1, "tiles.json gen changed while the cap was reached"
    kind2 = assert_relation(QUIET.get("published") or now, now, "tiles.json while the cap held")
    resp, _ = tile_at(anon, gen1, lat, lon, manifest["detail_maxzoom"])
    assert resp.status == 204, "the tile of the capped cell answers %d, want 204" % resp.status
    assert not map2d.job_problems(summary, 1), map2d.job_problems(summary, 1)
    series, waited, ok = wait_series(control, lambda v: v.get("dv_basemap_cap_hit_total", 0) >= 1, "cap")
    record("cap held for %.0f s with %s public: jobs %d, INDEX day_regions %s backoff %s too_large %s; tiles.json since the publish: %s (%s); dv_basemap_* %s" % (
        SETTLE_S, cell, map2d.jobs(summary), index["day_regions"], index["backoff"], index["too_large"], kind2, m2.optional_notes(now[1]),
        (("%s after %.0f s of waiting" % (series, waited)) if series else "not exported by the hub endpoint")))
    if series:
        assert ok, "after %.0f s of waiting dv_basemap_cap_hit_total is %s: the worker did not report the cap" % (waited, series.get("dv_basemap_cap_hit_total"))
