# SPDX-License-Identifier: MIT
"""dvtest/map2d.py (helpers of cases/test_map2d_regions.py), the mapsrc compose profile and the [basemap] snippet (offline)."""
import copy
import gzip
import json
import re
import tomllib
from pathlib import Path

import pytest

from dvtest import basemap_fixture as fx, map2d, sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "rig" / "compose.yml").read_text()
RUN_SH = (ROOT / "scripts" / "run.sh").read_text()
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "map2d-source.toml"


def good_index():
    return {"version": 1, "generation": 3, "regions": [
        {"cell": "10/793/457", "archive": "r-10-793-457-20261107.pmtiles", "build": "20261107", "schema": "4.15.2", "bytes": 1000,
         "sha256": "a" * 64, "fetched_ms": 1, "last_shown_ms": 2, "state": "ready"}],
        "day": 20767, "day_regions": 1, "day_bytes": 9000, "backoff": [], "fetch_enabled": True, "preparing": [], "too_large": []}


# ---------------------------------------------------------------- coverage

def test_cell_coverage_is_whole_cell_containment():
    manifest = {"tiers": [{"role": "overview", "bbox": [-180, -85, 180, 85]}, {"role": "detail", "bbox": list(map(float, fx.DEFAULT_DETAIL[1].split(",")))}]}
    boxes = map2d.detail_boxes(manifest)
    assert boxes == [[99.9, 13.0, 101.1, 14.1]]            # the overview tier is not a detail box
    assert not map2d.cell_covered(fx.cell_of(18.79, 98.98), boxes) and not map2d.cell_covered(fx.cell_of(7.88, 98.39), boxes)
    assert map2d.cell_covered(fx.cell_of(13.75, 100.5), boxes)           # the base point's own cell is inside the installed box
    assert not map2d.cell_covered(fx.cell_of(13.05, 99.95), boxes)      # a cell that only reaches into the box is not covered
    thailand = [[97.3, 5.6, 105.7, 20.5]]
    for lat, lon in ((18.79, 98.98), (7.88, 98.39), (13.75, 100.5)):
        assert map2d.cell_covered(fx.cell_of(lat, lon), thailand)       # with the default Thailand box all three would be covered
    assert map2d.detail_boxes(None) == [] and not map2d.cell_covered("10/793/457", [])


# ---------------------------------------------------------------- INDEX

def test_a_well_formed_index_has_no_problems_and_lists_its_cells():
    doc = good_index()
    assert map2d.is_map2d_index(doc) and map2d.index_problems(doc) == []
    assert map2d.region_cells(doc) == ["10/793/457"] and map2d.region_cells(None) == []


def test_the_pre_map2d_index_is_recognised():
    doc = {"version": 1, "generation": 0, "regions": [], "day": 1, "day_regions": 0, "day_bytes": 0, "backoff": []}
    assert not map2d.is_map2d_index(doc)
    assert any("lacks" in p for p in map2d.index_problems(doc))
    assert not map2d.is_map2d_index(None) and not map2d.is_map2d_index({"version": 2, "fetch_enabled": True, "preparing": [], "too_large": []})


@pytest.mark.parametrize("mutate,word", [
    (lambda d: d.update(generation=-1), "generation"),
    (lambda d: d.update(fetch_enabled="yes"), "fetch_enabled"),
    (lambda d: d.update(extra=1), "unknown"),
    (lambda d: d["regions"][0].update(state="preparing"), "ready"),
    (lambda d: d["regions"][0].update(cell="10/1/1"), "does not name"),
    (lambda d: d["regions"][0].update(archive="r-10-793-457-2026.pmtiles"), "does not name"),
    (lambda d: d["regions"][0].pop("sha256"), "region entry"),
    (lambda d: d.update(preparing="none"), "preparing"),
])
def test_index_problems_name_the_defect(mutate, word):
    doc = copy.deepcopy(good_index())
    mutate(doc)
    assert any(word in p for p in map2d.index_problems(doc)), map2d.index_problems(doc)


def test_read_index_reads_the_basemap_dir_layout(tmp_path):
    base = tmp_path / "basemap"
    assert map2d.read_index(base) is None
    (base / "regions").mkdir(parents=True)
    (base / "regions" / "INDEX").write_text("not json")
    assert map2d.read_index(base) is None
    (base / "regions" / "INDEX").write_text(json.dumps(good_index()))
    assert map2d.read_index(base)["generation"] == 3
    (base / "regions" / "INDEX").write_text("[1]")
    assert map2d.read_index(base) is None


# ---------------------------------------------------------------- the request log

def row(method, path, rng=None, if_match=None, status=200):
    return {"method": method, "path": path, "range": rng, "if_match": if_match, "status": status}


ARCHIVE = "/archives/" + fx.ARCHIVE_KEY


def one_job():
    return [row("GET", "/builds.json"), row("HEAD", ARCHIVE),
            row("GET", ARCHIVE, "bytes=0-126", '"t"', 206), row("GET", ARCHIVE, "bytes=127-300", '"t"', 206)]


def test_one_clean_job_is_one_builds_get_one_head_and_ranges():
    summary = fx.summarize(one_job())
    assert map2d.jobs(summary) == 1 and map2d.job_problems(summary, 1) == []
    assert map2d.jobs(fx.summarize([])) == 0 and map2d.job_problems(fx.summarize([]), 0) == []


def test_job_problems_flag_extra_jobs_missing_ranges_whole_gets_and_glyphs():
    two = fx.summarize(one_job() + one_job())
    assert map2d.jobs(two) == 2 and any("want 1" in p for p in map2d.job_problems(two, 1))
    assert any("at least 2" in p for p in map2d.job_problems(fx.summarize(one_job()[:2]), 1))
    whole = fx.summarize(one_job() + [row("GET", ARCHIVE)])
    assert any("whole archive" in p for p in map2d.job_problems(whole, 1))
    stale = fx.summarize(one_job() + [row("GET", ARCHIVE, "bytes=0-1", None, 206)])
    assert any("If-Match" in p for p in map2d.job_problems(stale, 1))
    glyph = fx.summarize(one_job() + [row("GET", "/mirror/glyphs/OFL.txt")])
    assert any("glyph" in p for p in map2d.job_problems(glyph, 1))
    assert any("want 0" in p for p in map2d.job_problems(fx.summarize(one_job()), 0))


def test_source_stats_reads_the_fixture_stats_listener():
    with fx.BasemapFixture(stats_bind=("127.0.0.1", 0)) as fixture:
        host, port = fixture._stats.server_address[:2]
        base = "http://%s:%d" % (host, port)
        rows, summary = map2d.source_stats(base)
        assert rows == [] and summary["total"] == 0
        import http.client
        conn = http.client.HTTPConnection(*fixture._server.server_address[:2], timeout=10)
        conn.request("GET", "/builds.json")
        conn.getresponse().read()
        conn.close()
        rows, summary = map2d.source_stats(base + "/")
        assert [r["path"] for r in rows] == ["/builds.json"] and summary["builds"] == 1


# ---------------------------------------------------------------- tiles

def test_tile_marker_reads_the_coordinates_the_fixture_wrote():
    for z, x, y in ((0, 0, 0), (12, 3100, 1829), (14, 12403, 7319)):
        assert map2d.tile_marker(fx.mvt_tile(z, x, y)) == "%d/%d/%d" % (z, x, y)
    assert map2d.tile_marker(b"not gzip") is None
    assert map2d.tile_marker(gzip.compress(b"")) is None
    assert map2d.tile_marker(gzip.compress(b"\x1a\x04\x0a\x02ab")) is None


def test_tile_for_and_tile_path():
    z, x, y = map2d.tile_for(18.79, 98.98, 12)
    assert z == 12 and (x, y) == fx.lonlat_tile(98.98, 18.79, 12)
    assert map2d.tile_path("0123456789abcdef", z, x, y) == "/basemap/tiles/0123456789abcdef/%d/%d/%d.mvt" % (z, x, y)
    # the tile of a cell's own point at a detail zoom lies inside that cell
    west, south, east, north = fx.cell_bbox(fx.cell_of(18.79, 98.98))
    lon_w = x / (1 << z) * 360.0 - 180.0
    assert west <= lon_w < east


# ---------------------------------------------------------------- telemetry

def test_basemap_metrics_keeps_present_series_only():
    text = "# TYPE dv_basemap_regions gauge\ndv_basemap_regions 1\ndv_basemap_fetches_total 2\ndv_other 5\n"
    assert map2d.basemap_metrics(text) == {"dv_basemap_regions": 1.0, "dv_basemap_fetches_total": 2.0}
    assert map2d.basemap_metrics("") == {}


# ---------------------------------------------------------------- the rig wiring

def service_block(name):
    m = re.search(r"^  %s:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|^\S|^  #)" % re.escape(name), COMPOSE, re.M | re.S)
    assert m, name
    return m.group(1)


def test_snippet_is_a_valid_basemap_table_that_names_the_fixture_ports():
    doc = tomllib.loads(SNIPPET.read_text())
    assert set(doc) == {"basemap"}
    table = doc["basemap"]
    assert table["fetch"] is True and table["daily_regions"] == 1
    for key in ("source_index", "source_base"):
        assert re.fullmatch(r"http://127\.0\.0\.1:18700/\S+", table[key]), key       # literal loopback, the only http the product accepts
    assert table["source_index"].endswith(fx.INDEX_PATH) and table["source_base"].endswith(fx.ARCHIVE_PREFIX.rstrip("/"))


def test_snippet_merges_into_a_template_with_and_without_a_basemap_table():
    base = 'schema = 1\n[node]\ncallsign = "XLX999"\nbind_ip = "<OWN_IP>"\n'
    for template, kept in ((base, None), (base + "[basemap]\nfetch = false\ndaily_bytes = 1048576\n", 1048576)):
        got = tomllib.loads(sut_config.render(template, "172.30.0", extras=[SNIPPET.read_text()]))["basemap"]
        assert got["source_index"].startswith("http://127.0.0.1:18700") and got["daily_regions"] == 1 and got["fetch"] is True
        assert got.get("daily_bytes") == kept                       # a key the snippet does not name survives


def test_sidecar_shares_the_sut_namespace_with_limits_and_the_snippet_ports():
    block = service_block("basemap-src")
    assert "<<: *limits-small" in block and "profiles: [mapsrc]" in block
    assert 'network_mode: "service:xlxd-sut"' in block and "networks:" not in block
    for need in ("cap_drop: [ALL]", "no-new-privileges:true", "read_only: true", "..:/dvt:ro", "dvt-tester:local"):
        assert need in block
    assert re.search(r"- --host\n\s+- 127\.0\.0\.1\n\s+- --port\n\s+- \"18700\"", block)       # loopback only, the snippet's port
    assert re.search(r"- --stats-bind\n\s+- 0\.0\.0\.0:18701", block)
    assert "DVT_MAPSRC_STATS: http://${DVT_NET:-172.30.0}.11:18701" in service_block("tester")


def test_run_sh_wires_the_profile():
    assert re.search(r"^    mapsrc\)\n.*?profiles\+=\(--profile mapsrc\)\n\s+export DVT_MAPSRC=on", RUN_SH, re.M | re.S)
    assert "--profile mapsrc" in re.search(r"compose_all=\((.*?)\)\n", RUN_SH, re.S).group(1)
    assert "--profile mapsrc needs --profile web" in RUN_SH and "map2d-source.toml" in RUN_SH
    assert "mmdvm, mapsrc, nat" in RUN_SH


# ---------------------------------------------------------------- what the image reports, the ETag relation

def test_image_reports_map2d_from_the_hub_series_and_the_marker_wins():
    text = "# TYPE dv_basemap_regions gauge\ndv_basemap_regions 0\ndv_other 5\n"
    assert map2d.image_reports_map2d(text, env="")[0] is True
    assert map2d.image_reports_map2d("dv_other 5\n", env="")[0] is False
    assert map2d.image_reports_map2d(None, env="") == (False, "no metrics to probe and no DVT_SUT_MAP2D marker")
    assert map2d.image_reports_map2d("dv_other 5\n", env="1")[0] is True          # the marker wins over the probe
    assert map2d.image_reports_map2d(text, env="0")[0] is False
    assert map2d.image_reports_map2d(text, env="maybe")[0] is True                   # anything but 0 / 1 is not a marker
    assert "DVT_SUT_MAP2D=1" in map2d.image_reports_map2d(None, env="1")[1]


GEN_A, GEN_B = "0123456789abcdef", "fedcba9876543210"


def read(gen=GEN_A, etag='"e1"', **over):
    desc = {"installed": True, "gen": gen, "tiles": ["/basemap/tiles/%s/{z}/{x}/{y}.mvt" % gen], "fetch_enabled": False, "preparing": [],
            "coverage": {"boxes": [[99.9, 13.0, 101.1, 14.1]], "cells": []}}
    desc.update(over)
    return etag, desc


def test_identical_reads_keep_the_etag():
    assert map2d.descriptor_relation(read(), read()) == ("same", [])
    kind, problems = map2d.descriptor_relation(read(), read(etag='"e2"'))
    assert kind == "same" and any("identical" in p for p in problems)


def test_a_status_only_change_moves_the_etag_but_not_the_tile_urls():
    for over in ({"fetch_enabled": True}, {"preparing": ["10/793/457"]}, {"fetch_enabled": True, "preparing": ["10/793/457"]}):
        assert map2d.descriptor_relation(read(), read(etag='"e2"', **over)) == ("status-only", []), over


def test_a_status_only_change_with_the_same_etag_is_a_problem():
    kind, problems = map2d.descriptor_relation(read(), read(fetch_enabled=True))
    assert kind == "status-only" and any("did not move" in p for p in problems)


def test_a_new_generation_moves_the_etag_and_the_tile_urls():
    after = read(gen=GEN_B, etag='"e2"', coverage={"boxes": [[99.9, 13.0, 101.1, 14.1]], "cells": ["10/793/457"]})
    assert map2d.descriptor_relation(read(), after) == ("new-generation", [])
    stale_tiles = ('"e2"', dict(read(gen=GEN_B)[1], tiles=read()[1]["tiles"]))          # gen moved, the tile URL did not
    kind, problems = map2d.descriptor_relation(read(), stale_tiles)
    assert kind == "new-generation" and any("do not follow" in p for p in problems)
    kind, problems = map2d.descriptor_relation(read(), read(gen=GEN_B))
    assert kind == "new-generation" and any("did not move" in p for p in problems)


def test_other_changes_under_one_generation_are_problems():
    kind, problems = map2d.descriptor_relation(read(), read(etag='"e2"', coverage={"boxes": [], "cells": ["10/1/1"]}))
    assert kind == "other-change" and any("coverage" in p for p in problems)
    kind, problems = map2d.descriptor_relation(read(), read(etag='"e2"', tiles=["/basemap/tiles/%s/other" % GEN_A]))
    assert any("tile URLs changed" in p for p in problems)


# ---------------------------------------------------------------- waiting for the telemetry (10 s ingest lag)

class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_for_series_returns_at_once_when_ready():
    fake = FakeClock()
    got = map2d.wait_for_series(lambda: {"dv_basemap_regions": 1.0}, lambda v: v["dv_basemap_regions"] >= 1, sleep=fake.sleep, clock=fake.clock)
    assert got == ({"dv_basemap_regions": 1.0}, 0.0, True)


def test_wait_for_series_rides_out_the_ingest_lag():
    fake, reads = FakeClock(), [{"dv_basemap_regions": 0.0}] * 4 + [{"dv_basemap_regions": 1.0}]
    series, waited, ok = map2d.wait_for_series(lambda: reads.pop(0), lambda v: v["dv_basemap_regions"] >= 1, timeout=25.0, poll=2.0,
                                               sleep=fake.sleep, clock=fake.clock)
    assert ok and series == {"dv_basemap_regions": 1.0} and waited == 8.0 and not reads


def test_wait_for_series_gives_up_after_the_timeout_with_the_last_read():
    fake = FakeClock()
    series, waited, ok = map2d.wait_for_series(lambda: {"dv_basemap_regions": 0.0}, lambda v: v["dv_basemap_regions"] >= 1, timeout=25.0, poll=2.0,
                                               sleep=fake.sleep, clock=fake.clock)
    assert not ok and series == {"dv_basemap_regions": 0.0} and 25.0 <= waited <= 27.0


def test_wait_for_series_does_not_wait_for_a_hub_without_the_series():
    fake = FakeClock()
    for empty in (None, {}):
        series, waited, ok = map2d.wait_for_series(lambda: empty, lambda v: True, sleep=fake.sleep, clock=fake.clock)
        assert series == empty and waited == 0.0 and not ok


# ---------------------------------------------------------------- MAP2h: the unavailable gauge, the old INDEX, the held lock

def gauge_text(active=None, failures=0, extra=""):
    lines = ["# TYPE dv_basemap_unavailable gauge"]
    for reason in map2d.UNAVAILABLE_REASONS:
        lines.append('dv_basemap_unavailable{proc="dv-svc-basemap",reason="%s"} %d' % (reason, 1 if reason == active else 0))
    lines.append('dv_basemap_failures_total{proc="dv-svc-basemap"} %d' % failures)
    lines.append('dv_basemap_fetches_total{proc="dv-svc-basemap"} 2')
    return "\n".join(lines) + "\n" + extra


def test_the_documented_reason_strings():
    assert map2d.UNAVAILABLE_REASONS == ("old_index", "writer_active", "no_install")       # status.md, deployment.md (MAP2h)
    assert map2d.INSTALL_LOCK == ".basemap-install.lock"


def test_snapshot_reads_the_labelled_series_and_the_counters():
    snap = map2d.unavailable_snapshot(gauge_text("old_index", failures=3))
    assert snap == {"reasons": {"old_index": 1.0, "writer_active": 0.0, "no_install": 0.0}, "failures": 3.0, "fetches": 2.0}
    assert map2d.snapshot_problems(snap) == []
    assert map2d.unavailable_snapshot("dv_basemap_regions 0\n") == {}                  # an image before MAP2h
    assert map2d.unavailable_snapshot("") == {}
    no_counters = map2d.unavailable_snapshot('dv_basemap_unavailable{reason="no_install"} 1\n')
    assert no_counters["failures"] is None and no_counters["fetches"] is None


def test_reason_is_needs_a_well_formed_one_hot_gauge():
    for active in map2d.UNAVAILABLE_REASONS:
        snap = map2d.unavailable_snapshot(gauge_text(active))
        assert map2d.reason_is(snap, active)
        assert not map2d.reason_is(snap, None)
        assert all(not map2d.reason_is(snap, other) for other in map2d.UNAVAILABLE_REASONS if other != active)
    available = map2d.unavailable_snapshot(gauge_text(None))
    assert map2d.reason_is(available, None) and not map2d.reason_is(available, "no_install")
    assert not map2d.reason_is({}, None) and not map2d.reason_is(None, "no_install")


@pytest.mark.parametrize("text,needle", [
    ('dv_basemap_unavailable{reason="old_index"} 1\ndv_basemap_unavailable{reason="no_install"} 1\ndv_basemap_unavailable{reason="writer_active"} 0\n', "more than one"),
    ('dv_basemap_unavailable{reason="old_index"} 2\ndv_basemap_unavailable{reason="no_install"} 0\ndv_basemap_unavailable{reason="writer_active"} 0\n', "not 0 or 1"),
    ('dv_basemap_unavailable{reason="old_index"} 0\ndv_basemap_unavailable{reason="no_install"} 0\n', "want exactly"),
    ('dv_basemap_unavailable{reason="old_index"} 0\ndv_basemap_unavailable{reason="no_install"} 0\ndv_basemap_unavailable{reason="writer_active"} 0\n'
     'dv_basemap_unavailable{reason="hub_not_ready"} 0\n', "want exactly"),
])
def test_snapshot_problems_name_a_malformed_gauge(text, needle):
    problems = map2d.snapshot_problems(map2d.unavailable_snapshot(text))
    assert any(needle in p for p in problems), problems
    assert not map2d.reason_is(map2d.unavailable_snapshot(text), "old_index")


def test_old_index_bytes_drop_exactly_the_fields_map2d_added():
    index = good_index()
    old = map2d.old_index_bytes(index)
    doc = json.loads(old)
    assert old.isascii() and old.endswith(b"\n")
    assert not ({"fetch_enabled", "preparing", "too_large"} & set(doc))
    assert doc["version"] == 1 and {k: v for k, v in doc.items()} == {k: v for k, v in index.items() if k not in map2d.PRE_WORKER_INDEX_KEYS}
    assert map2d.is_pre_worker_index(doc) and not map2d.is_pre_worker_index(index)
    assert not map2d.is_map2d_index(doc)                                            # the helper that the other cases skip on agrees
    assert map2d.old_index_bytes(json.dumps(index).encode()) == old                 # bytes in, same out
    assert good_index() == index                                                    # the input is not mutated
    assert not map2d.is_pre_worker_index(None) and not map2d.is_pre_worker_index({"version": 2})


def test_replace_file_is_atomic_and_leaves_no_temp(tmp_path):
    target = tmp_path / "INDEX"
    target.write_bytes(b"old")
    inode = target.stat().st_ino
    map2d.replace_file(target, b"new")
    assert target.read_bytes() == b"new" and not (tmp_path / "INDEX.dvt-tmp").exists()
    assert target.stat().st_ino != inode                                            # a rename: readers see the old or the new file whole


def test_held_lock_conflicts_with_a_second_flock_like_the_workers_and_releases(tmp_path):
    import fcntl
    import os
    lock = map2d.HeldLock(tmp_path)
    assert not lock.held and not lock.path.exists()
    lock.acquire()
    assert lock.held and lock.path.is_file() and lock.path.name == ".basemap-install.lock"
    fd = os.open(lock.path, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):                                        # the worker's flock(LOCK_EX | LOCK_NB) would be refused
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            map2d.HeldLock(tmp_path).acquire()
        lock.release()
        assert not lock.held
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)                              # free again
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    lock.release()                                                                  # releasing twice is harmless


def test_held_lock_context_waits_for_a_busy_lock_and_refuses_odd_files(tmp_path):
    import threading
    import time
    first = map2d.HeldLock(tmp_path).acquire()
    threading.Timer(0.4, first.release).start()
    started = time.monotonic()
    with map2d.HeldLock(tmp_path) as second:                                        # retried until the first one lets go
        assert second.held and time.monotonic() - started >= 0.3
    assert not second.held
    link = tmp_path / "linked"
    link.mkdir()
    (link / ".basemap-install.lock").symlink_to(tmp_path / ".basemap-install.lock")
    with pytest.raises(OSError):                                                    # O_NOFOLLOW: the worker refuses a symlink too
        map2d.HeldLock(link).acquire()
    folder = tmp_path / "dir"
    (folder / ".basemap-install.lock").mkdir(parents=True)
    with pytest.raises(OSError):
        map2d.HeldLock(folder).acquire()
    with pytest.raises(BlockingIOError):
        busy = map2d.HeldLock(tmp_path).acquire()
        try:
            map2d.HeldLock(tmp_path).acquire_retry(timeout=0.3, poll=0.05)
        finally:
            busy.release()


def test_the_map2h_case_names_only_documented_reasons():
    text = (ROOT / "cases" / "test_map2h_unavailable.py").read_text()
    import re
    used = set(re.findall(r'"(old_index|writer_active|no_install|[a-z]+_[a-z]+_[a-z]+)"', text)) & {"old_index", "writer_active", "no_install"}
    assert used == set(map2d.UNAVAILABLE_REASONS)
    assert "python3 -m pytest" not in text
