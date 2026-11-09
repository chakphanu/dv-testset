# MAP2d lazy basemap regions: the rig case

`cases/test_map2d_regions.py` tests the lazy region worker of DV-Router MAP2d (commit 15b07b8, image wip-f9aaebc): `dv-svc-basemap serve`,
a child of the launcher that runs when `[basemap] fetch` is on (the default) and the node has a persistent state directory. NOT RUN on a rig yet.

## What the worker does (from the DV-Router sources, `git show 15b07b8:<path>`)

* Every 5 s it reads the public positions and live objects of the hub (fresh for 30 minutes, at most 1024) and the hidden set (account
  `hide_position`, `data.position_optout`). Nothing happens until the hidden set is known.
* The cell of a position is a z10 tile `10/<x>/<y>` (about 39 km wide) of its DISPLAYED coordinate (`crates/svc/basemap/src/core/worker.rs`
  `position_cell`). A cell is eligible only if the base callsign is not hidden, the ambiguity is 0 to 2 (3 and 4 never trigger), the cell is
  not inside a detail tier of the installed base, and it holds no region younger than `region_max_age_days`.
* One job at a time, FIFO of at most 64 cells. A job reads `source_index` (builds.json), pins the archive with HEAD (exact Content-Length, strong ETag)
  and range-reads it with `If-Match`, plans the cell box at zooms `overview_maxzoom+1 .. detail_maxzoom` of the MANIFEST and writes
  `regions/r-10-<x>-<y>-<build>.pmtiles`, then a new `regions/INDEX` (generation + 1). One source archive is enough; there are no per-region builds.
  A cell with no tile in the source fails ("selected cell has no source tiles") and backs off 1 h, doubling to 24 h.
* Caps: `daily_regions` (the slot is taken when a cell is admitted, before any HTTP), `daily_bytes`, `region_max_bytes`, `regions_max_bytes`.
  The day is the UTC day; the counters live in the INDEX (`day`, `day_regions`, `day_bytes`).
* dv-svc-web's tiles.json `gen` is a hash over the listed archives, so a published region changes it and the tile URL.
* An INDEX written before MAP2d (no `fetch_enabled`, `preparing`, `too_large`) is refused by `verify` and by the worker.

## The source in the rig (compose profile `mapsrc`)

`dv-svc-basemap` accepts an http source only at the literal address `127.0.0.1` or `[::1]` and follows no redirect, so the source has to be on
the SUT's own loopback. The SUT image has no shell and no python and no control action starts a process in it, so:

* `basemap-src` (rig/compose.yml, profile `mapsrc`, limit set `limits-small`) runs `python3 -m dvtest.basemap_fixture serve --host 127.0.0.1 --port 18700
  --stats-bind 0.0.0.0:18701 --access-log` with `network_mode: "service:xlxd-sut"`: the same network namespace, so `127.0.0.1:18700` is the SUT's loopback.
  It uses the tester image (python 3.12, `dvtest` from the repository mounted read-only), cap_drop ALL, read-only root.
* `rig/config/dv-router/map2d-source.toml` (added by `--profile mapsrc`) sets `[basemap] fetch = true`, `source_index = "http://127.0.0.1:18700/builds.json"`,
  `source_base = "http://127.0.0.1:18700/archives"` and `daily_regions = 1`.
* The stats listener (`0.0.0.0:18701`, the request log only: `GET /requests`) is what the tester reads at `DVT_MAPSRC_STATS` (the SUT's rig address);
  the same lines go to `out/<run>/logs/basemap-src.log` at the end of the run.
* The fixture archive (`dvtest/basemap_fixture.py`, 37 tiles, 4268 bytes) holds the tile chain z0 to z14 of Bangkok (inside the installed detail box),
  Chiang Mai (`10/793/457`) and Phuket (`10/791/489`), and every tile names its own `z/x/y`.
* The sidecar shares the SUT's namespace for the life of the SUT container: do not combine the case with a control-channel restart of `xlxd-sut`
  (the capture and netem sidecars are recreated by the watcher, this one is not).

## Startup order and coverage (read from the source)

* A tree installed AFTER the worker started is fine: a missing `<state>/basemap` leaves the worker idle, and each 5 s cycle re-reads
  `base/MANIFEST`; a changed identity runs `verify` (complete tree, MAP2d INDEX, hashes) and rebuilds the worker, no restart. An incomplete tree
  is retried every cycle (failures counted).
* A cell is covered only when it lies WHOLLY inside a detail tier box (tile-aligned at the tier's maxzoom). With the installed box
  `dvt=99.9,13.0,101.1,14.1` Chiang Mai, Phuket and the other test cells are outside it; with the default Thailand box all of them would be covered and the
  cases skip with that reason.
* A hidden account (`hide_position`) is dropped at the room controller, so its position never reaches the hub state at all; the worker's own hidden set
  (policy set plus `data.position_optout`) protects rows retained from before a change. The case sets the flag before the position, so it asserts the
  end result (no public row, no job), not the worker's filter in isolation.
* The negative cases come first and the daily cap is 1: a hidden or ambiguous cell that the worker wrongly admitted would take the day's only slot, and
  the Chiang Mai region of the next case would not publish (the INDEX must list exactly that one cell).

## The tree

The worker needs an installed base. The rig cannot preseed the state directory, so the case installs a tree into `out/<run>/sut-state/basemap/`
(`position_map2_support.install_tree`, removed at the end) while the worker is already running. Make it with the `dv-svc-basemap` of the image under
test (an older binary writes an INDEX the worker refuses; the case skips with that reason):

```sh
scripts/basemap-install-fixture.sh --bin <dv-svc-basemap of wip-f9aaebc> --out .local/basemap-map2d
```

## Run (about 6 minutes, its own run; do not start it while another rig run is going)

```sh
XLXD_SUT_IMAGE=<dv-router:wip-f9aaebc> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> \
DVT_USRP_DYNAMIC=1 DVT_REQUIRE_IDENT=true DVT_TRANSCODER_FAKE=1 \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/data-ident.toml \
DVT_BASEMAP_FILE=.local/basemap-map2d \
scripts/run.sh -m rig --capture --profile web,mapsrc -- cases/test_map2d_regions.py
```

`--profile mapsrc` needs `--profile web` and adds `map2d-source.toml` itself. Cases skip with the reason without `DVT_MAPSRC=on`, a reachable request log,
`DVT_BASEMAP_FILE` or a MAP2d-shaped INDEX in it.

## Cases

| Case | Steps | Asserted | Recorded only |
|------|-------|----------|---------------|
| hidden and ambiguous positions start no job | the account N0HIDE gets `hide_position`, then sends a position in Khon Kaen; APRS positions of ambiguity 3 (Ubon) and 4 (Nakhon Si Thammarat) are sent and are public rows; 30 s of watching the source | the hidden position never becomes public; the ambiguity rows are public with their level; the source saw ZERO requests; INDEX in MAP2d shape with no region, `day_regions` 0 and an unchanged generation; tiles.json `gen` unchanged; `dv_basemap_fetches_total` and `dv_basemap_regions` are 0 when the hub exports them | dv_basemap_* values or their absence |
| a public position outside coverage publishes a region | before: the z12 tile of the Chiang Mai cell is 204 under the base `gen`; an ambiguity 0 position in Chiang Mai; up to 180 s | the INDEX lists cell `10/793/457` (`ready`, archive named by cell and build, file present with the recorded size and sha256, build and schema equal the base's), `day_regions` 1, generation above its value before; `gen` changed (16 hex); the source saw ONE job: one builds.json, one HEAD, ranged GETs only, each 206 with `If-Match`, no whole-archive GET, no glyph request; the z12 tile is 200, `application/vnd.mapbox-vector-tile`, gzip, and carries its own coordinates; `dv_basemap_fetches_total` >= 1 and `dv_basemap_regions` >= 1 when exported | time to publish, bytes, `day_bytes`, descriptor keys, status of the old generation's tile URL, the full source summary |
| the daily cap holds | an ambiguity 0 position in Phuket (tiles exist in the source), 30 s | still ONE job; INDEX has the one region, `day_regions` 1, generation unchanged, the Phuket cell is not listed; `gen` unchanged; the Phuket tile is 204; `dv_basemap_cap_hit_total` >= 1 when exported | `backoff`, `too_large`, the dv_basemap_* values |

Not covered here (the in-repo tests cover them, `crates/svc/basemap/tests/worker.rs`, `src/core/worker/tests.rs`): eviction, backoff timing (1 h and up, not
shortenable), the 412 restart, `too_large`, midnight roll-over of the daily counters, a refresh of an old region, integrity failures (MAP2e R4 to R6).

## UNVERIFIED

* That the 30 s windows are long enough (read from the source: the worker cycle is 5 s, one cell is admitted per cycle, there is no minimum position age,
  and about 10 s pass after a worker start before it first adopts the state).
* That the hub's Prometheus endpoint carries `dv_basemap_*`. By the source it does (dv-svc-status ingests the basemap telemetry kind every 10 s and adds the
  label `proc="dv-svc-basemap"`); the cases assert the values when the series are there and record a finding when they are not.
* That the stats port 18701 is reachable from the tester on the SUT's rig address (the sidecar shares the SUT's namespace and listens on 0.0.0.0).
* That tile `z12` of the Chiang Mai point is inside the extracted cell (it is by construction: the point lies in the cell and the tile at a deeper zoom
  contains it).
