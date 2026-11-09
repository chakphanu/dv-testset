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
