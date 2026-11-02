<!-- SPDX-License-Identifier: MIT -->
# Web unit: SOAK1, the 60 minute soak (WebRTC listeners and protocol clients together)

`cases/test_web_soak.py` (profiles web and webload, native launch), `cases/soak_support.py` (sampler, protocol lanes, over runner),
`dvtest/soak.py` (pure: the traffic schedule, the sample file reader and the pass criteria; unit tests `cases/unit/test_soak.py`,
`cases/unit/test_soak_support.py`, and `cases/unit/test_soak_case.py`, which runs the real case function against fakes in seconds), the
generator's soak mode (`dvtest/tools/webload_gen.py`) and the read-only control action `proc-stat` (`scripts/lib/procstat.sh`,
`dvtest/control.py`). Spec from DV-Router (2026-11-02): 60 minutes = 10 minutes warmup + 50 minutes measured, ACL on (the launcher
default). Adjusted to DV-Router's rulings of 2026-11-02 (thresholds, child names, flat counters, NXDN/P25 recipe) and to the later rulings of the DV-Router owner (lazy counters, `dv_edge_drops_total` by reason, RSS attribution, the 170 s settle, the median rule for threads and fds, smaps_rollup; see "Rulings of DV-Router"). **Status: the short
mode (below) was run on a rig to validate the lanes and the sampling; the full 60 minute run has not been made.** The first full run must
be watched, see "What the first run must watch".

## Mix

| Part | What | Notes |
|------|------|-------|
| WebRTC listeners | 100 receive-only listeners (aiortc, rig service `webload`), 50 in room A and 50 in room B, 2 worker processes | join paced by the W8 offsets (4 at once, then one every 1.05 s: the 1 per second per address budget with burst 5 and one token kept back); 13 logins of 8 circuits on 5 accounts; every circuit renews through its login's bucket (2 per second, burst 10) at once and every 10 s |
| churn | about 5 a minute: a seat leaves (`POST /rtc/close`, then the peer closes) and the same seat rejoins 3 to 10 s later, seats taken in a shuffled round (about 20 minutes per seat) | first churn 30 s after the last initial join; rejoins keep the seat's login, account and room, so a login never holds more than 8 circuits; about 390 sessions in an hour, all checked by `soak.validate_sessions` before the run (also by the generator) |
| `usrp-a`, `usrp-b` | USRP dynamic client, a 700 Hz tone into module A and B | the audience are the WebRTC listeners of rooms A and B (one callsign per room, `docs/web-load-cases.md` F3) |
| `dstar` (module D) | DExtra talker, DPlus listener, DCS listener; with profile interlink also a DExtra listener on the XLX998 peer | one lane covers all three D-STAR link protocols; passthrough frames |
| `dmr` (E) | MMDVM talker and listener | passthrough |
| `ysf` (C), `m17` (J) | one talker and one listener each | passthrough |
| `nxdn` (F), `p25` (G) | the SUT's own NXDN (UDP 41400) and P25 (UDP 41000) server ports, switched on by `soak-edges.toml` (see below) | NXDN talker with a DMR listener; P25 talker with a D-STAR listener (IMBE needs the codec worker) |

Every lane makes one over of 9 to 11 s per minute (`soak.build_overs`, deterministic from `DVT_SOAK_SEED`): the lanes are spread over the
first 45 s of the minute so that neighbours overlap, and every fifth minute all lanes start within a second of each other. Each over makes new
sockets, links, talks seeded PRNG frames, collects and unlinks (the clients of `cases/test_xlxd_matrix.py` / `dvtest/clients.py`,
`dvtest/proto/nxdn.py`, `p25.py` and the USRP peer of `cases/crossmode_fake.py`), so a leak in the SUT's client tables shows as a growing child
and no client has to keep a link alive. A short probe over per lane runs before the clock starts: a required lane (usrp-a, usrp-b, dstar, dmr,
ysf, m17) that does not work fails the case at once; nxdn and p25 are dropped with the reason (`soak.json` `lanes_dropped`).

**NXDN and P25 under the native launch.** With a config file there is no importer, so the `DVT_NXDN` / `DVT_P25` reflector peers of the
rig are not used and no `--profile nxdn,p25` is needed. The template has no `xlxd.*` files; the snippet `rig/config/dv-router/soak-edges.toml`
(DV-Router's manual, `docs/design/config.md` edge settings) switches on the SUT's own client ports, the ones `cases/proto/test_nxdn_server.py`
and `test_p25_network.py` talk to:

| Edge | Keys in `soak-edges.toml` | Lane |
|------|---------------------------|------|
| `[edges.nxdn]` | `enable = true`, `tg_modules = "31672:F"` (UDP 41400, the default port) | NXDN poll and talk with TG 31672 (`DVT_NXDN_TG`) to `<sut>:41400`, DMR listener on module F |
| `[edges.p25]` | `enable = true`, `server_module = "G"`, `server_tg = 31672` (UDP 41000) | P25 poll and talk with TG 31672 (`DVT_P25_TG`) to `<sut>:41000`, D-STAR listener on module G |

The template's own `[edges.nxdn]` / `[edges.p25]` tables (`enable = "auto"`) are merged key by key by `dvtest/sut_config.py` (a unit test
renders the snippet into a template with and without them). `DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/soak-edges.toml`.
`run.sh` also pins `DV_P25_SERVER_MODULE` / `DV_P25_SERVER_TG` for the SUT (`DVT_P25_MODULE` G, `DVT_P25_TG` 31672); the snippet carries the same
values. If `DVT_SOAK_MODULES` moves the nxdn or p25 lane, change the letters in the snippet too. Without the snippet the probe over fails
and the lane is dropped with a reason (`soak.json` `lanes_dropped`, a `FINDING:` line).

## Method

1. The case lists the processes of the SUT container (`proc-stat xlxd-sut list`: pid, ppid and name, nothing else) and takes the
   allow-listed children of the launcher plus the launcher itself; children outside the allow-list are reported (`children_unsampled`).
   A child that is not running (an edge or service that is not enabled) is simply not listed: not an error. The allow-list is explicit
   (`scripts/lib/procstat.sh`, `dvtest/control.py`, a unit test compares both): `dv-router` (the launcher), the edges `dv-edge-dextra`, `-dplus`,
   `-dcs`, `-mmdvm`, `-dmrplus`, `-ysf`, `-m17`, `-g3`, `-imrs`, `-xlx`, `-nxdn`, `-p25`, `-usrp`, `-webrtc`, `-aprsis`, `-ax25` (every edge is its own
   binary, there is no combined edge), `dv-room`, `dv-data-worker`, `dv-codec-ambed` and `dv-svc-status`, `-web`, `-admin`, `-iddb`, `-idsync`,
   `-callhome`, `-recorder`.
2. Before any traffic: three samples 10 s apart (phase `idle0`: the quiet baseline) and a counter snapshot.
3. Warmup 600 s, measured 3000 s, one sample of every child every 30 s on an absolute schedule in a thread (phases `warmup`, `measured`),
   counter snapshots at the end of warmup and of the measured part (the flat families, their series, the per-reason values of `dv_edge_drops_total` and, once, the names of all families on /metrics). Each sample: pid, start time, VmRSS, VmHWM, threads, open fds, cpu ticks.
   Also per tick: the edge listener gauge, the attribution row (the size gauges of the hub, see "RSS attribution") and, every five minutes, the generator's status.
4. Stop: the protocol overs end, the generator finishes (every listener revokes and closes), the gauge must read 0 within 150 s, then a
   settle of 170 s (`SETTLE_S`, see "Settle before the idle window"), then the idle window of 120 s (phase `idle`): `GET /api/v1/public/events` is sampled (resumed every 30 s with the last event
   id, as `test_status_stream_is_quiet_while_idle`), samples continue, a last sample and the counter snapshots close it.
5. The raw samples are ONE file, `out/<run>/web/soak-samples.jsonl` (one JSON object per line, kinds `proc`, `metrics`, `gauge`, `attr`, `mark`,
   flushed per row, about 1500 rows). The verdict is computed by re-reading that file (`soak.parse_jsonl`, `soak.evaluate`), so the
   file is what was judged. `out/<run>/web/soak.json` holds the table per child, every check, the counter deltas, the traffic and generator
   summaries and the plan; it is rewritten at every milestone, also by an aborted run.
6. Abort and teardown: a hard wall clock cap of 75 minutes (`soak.WALL_CAP_S`), `touch out/<run>/web/soak.stop` for a clean stop, Ctrl-C or a
   failure: in every case the sampler stops, the over runner stops, and the generator is told to abort (it revokes and closes what it holds).
   A stopped or capped run fails with "no verdict".

## How it is judged (`dvtest/soak.py`, lead defaults)

| # | Criterion | Rule |
|---|-----------|------|
| 1 | no child restarts | one (pid, start time) per child over every phase; a child that vanishes or first appears after warmup fails |
| 2 | RSS | growth over the measured window = least-squares slope x the measured window (3000 s, not the span of the samples), must be <= max(5 MiB, 10 percent of the RSS at the end of warmup, the median of the last 3 warmup samples); the last sample is not judged. In a short run (`DVT_SOAK_SHORT=1`) the RSS slope is NOT a verdict (warmup dominates): the check `rss:<child>` is recorded as a lead (detail starts with `LEAD ONLY`, a warning `RSS lead <child>` when over the scaled bound) and never fails the case; the full run judges it. See "RSS attribution" and "Short mode" |
| 3 | threads, fds | ruling of DV-Router (2026-11-02, later): NOT judged by the last sample. Per child the median over the last 10 minutes of the measured part against the median over the first 10 minutes after warmup; FAIL only when the later median exceeds the earlier by more than max(10 percent of the earlier, 3 threads) (fds: max(10 percent, 4 fds)). A last sample above the old limit (end of warmup + max(10 percent, +2 threads / +4 fds)) is only FLAGGED (`FLAG threads <child>` / `FLAG fds <child>` warning, `children.<name>.flags` in `soak.json`). After the traffic stopped the fd count is <= the quiet value of `idle0` (before any traffic) + 5 (unchanged). See "Threads and fds: the median rule" |
| 4 | flat counters | the 11 families below are FLAT over the measured part: value at the end of warmup == value at the end, absolute zero growth, summed over all labels and processes. A series that is not on /metrics counts as 0 (status `absent`, no assertion): `dv_edge_tx_refused_total`, `dv_recorder_dropped_total` and `dv_room_shell_errors_total` are created at their first increment, so they are never required to be present, they only must not APPEAR or grow in the window (appearing is growth from 0). A family on /metrics at the end of warmup and gone at the end is `vanished` (recorded). On growth the check `flat:<family>` fails with the family, the delta and the series that grew. `soak.FLAT_OVERRIDES` (per family, starts empty) holds a family that grows with the traffic mix: it is recorded with name and growth (`override`), not asserted. All other error-like families (name contains drop, refus, reject, denied, panic, error, overflow, overrun, lost, fail, timeout, discard) are recorded with their deltas, `counters.growing` lists those that grew; nothing else is asserted |
| 4b | `dv_edge_drops_total` | counts legitimate events of the lane mix, so it is NOT flat. Legit reasons (`soak.DROPS_LEGIT`): `link_timeout`, `no_session`, `rx_ignored`, `x_stale`. `drops-idle`: in the idle window (it starts 170 s after the last client left, see "Settle before the idle window") no reason grows (any reason, a missing series is 0). `drops-reasons`: in the measured window the check fails only when a reason OUTSIDE the four appears or grows. The four are recorded with delta and rate per second (table after the flat families, `counters.drops` in `soak.json`); when their total rate exceeds 1 per second (`soak.DROPS_RATE_FLAG_PER_S`) the summary carries a `FLAG dv_edge_drops_total` warning (a `FINDING:` line, `warnings` in `soak.json`), not a failure. `dv_edge_queue_drops_total` and `dv_edge_webrtc_audio_dropped_total` stay strict (flat family list, missing = 0) |
| 5 | idle event stream | `k22.idle_stream_verdict`: no whole minute after the first holds more than 2 events |
| + | idle | the listener gauge reads 0 in every idle sample |
| + | generator | at most 2 percent of the sessions failed, at most 2 percent of the lease answers other than 204, and the listeners of each room heard at least 50 packets (this is how the USRP lanes are seen) |
| + | protocol mix | each required lane made at least 5 overs and delivered at least half of them to its listeners |
| + | sampling | no damaged line in the file, at most 3 sampling errors |

A child whose fds cannot be read (`fds=-1`) is listed under `unmeasured` and as a `FINDING:` line; it does not fail the run, but it is never reported as passed: it
shows in the report. The helper container of `proc-stat` runs as the user of the SUT container (root when the image names none, no capability)
because `/proc/<pid>/fd` is readable only for the same uid.

Flat families (`soak.FLAT_FAMILIES`, DV-Router `docs/design/status.md` "Metrics"; `dv_edge_drops_total` was taken out, see 4b): `dv_edge_webrtc_panics_total`,
`dv_edge_queue_drops_total`, `dv_edge_webrtc_audio_dropped_total`, `dv_room_ingress_rejected_total`, `dv_room_shell_errors_total`,
`dv_status_telemetry_rejected_total`, `dv_status_input_dropped_total`, `dv_fabric_put_dropped_total`, `dv_recorder_dropped_total`,
`dv_codec_dropped_total`, `dv_edge_tx_refused_total`. The table of the flat families (and of the drop reasons) is printed after the table per child and kept in `soak.json`
(`counters_table`, `metric_families`).

## Settle before the idle window

The idle window must show no growth of any `dv_edge_drops_total` reason, so every link of a departed client has to be gone before it starts.
The case therefore waits `SETTLE_S` = 170 s after the last client left (listener gauge 0, protocol overs ended) and takes the `idle_start` snapshot
only then. 170 s = the longest link timeout (imrs, 150 s) plus a sweep tick (`soak.SETTLE_MIN_S`; a unit test keeps `SETTLE_S` at or above it).
Link timeouts of the SUT (DV-Router ruling, `soak.LINK_TIMEOUTS_S`):

| Protocol (edge) | Link timeout |
|-----------------|--------------|
| imrs (link) | 150 s |
| mmdvm | 100 s |
| dextra, dcs, ysf | 60 s |
| xlx, dplus, m17, nxdn, p25 | 30 s |
| usrp (dynamic) | never expires |

## Threads and fds: the median rule

The `dv-edge-*` thread counts are not constant: the tokio blocking pool spawns threads lazily and keeps one about 10 s, so a single sample (the last)
says little. Per child, over the measured samples of the child:

- early = median over the FIRST 10 minutes after warmup, late = median over the LAST 10 minutes of the measured part;
- `threads:<child>` fails only when late > early + max(10 percent of early, 3 threads); `fds:<child>` when late > early + max(10 percent of early, 4 fds);
- a last sample above the old limit (the median of the last 3 warmup samples + max(10 percent, +2 threads / +4 fds)) is only a warning (`FLAG ...`);
- the fds-idle check (`fds-idle:<child>`, <= `idle0` + 5 after the traffic) is unchanged.

The 10 minute windows are for the 50 minute measured part. In a shorter run they are scaled with it (10/50 of the measured part, `median_window_s`
in `soak.json`): 48 s of the 240 s of `DVT_SOAK_SHORT=1`. A window that then holds fewer than 3 samples (a tick takes 19 s) takes the first / last 3
samples instead, so the short run is a rough check of the sampling, not of the criterion. The detail of every check names both medians, their sample
counts, the limit, the maximum and the last sample.

## smaps_rollup (recorded, not asserted)

When the `proc-stat` reply of a process carries `smaps_rss_kb`, `smaps_anon_kb`, `smaps_private_dirty_kb` and `smaps_pss_anon_kb` (the host helper reads
`/proc/<pid>/smaps_rollup`: Rss, Anonymous, Private_Dirty, Pss_Anon; the keys are left out when the file is missing), the sampler stores them in the
`proc` rows of `soak-samples.jsonl` as `smaps_rss_kib`, `smaps_anon_kib`, `smaps_private_dirty_kib`, `smaps_pss_anon_kib` (`dvtest.webload.ProcSample.smaps`).
`soak.json` keeps per child (`result.children.<name>.smaps`) first, last, min, max and the least-squares slope per minute over the measured part, and
`smaps_table` prints them (the dv-router and dv-svc-status rows tell anonymous growth from file-backed or shared pages next to the RSS slope). Absent keys
are normal: older helpers and runs have none, nothing is asserted. The host side is `scripts/lib/procstat.sh` (a unit test accepts a reply with or without the keys).

## Re-judging a finished run offline

`python3 -m dvtest.soak out/<run>` (read only; also accepts `out/<run>/web` or the `soak-samples.jsonl`; `--all` lists the passing per-child checks,
`--measured-s` and `--short` replace the missing `soak.json`; exit status 1 on a failing verdict) re-evaluates `web/soak-samples.jsonl` with the
current rules and prints the table per child, the counter, attribution and smaps tables, the warnings, the checks and the verdict. `soak.json` next to the
samples supplies the timing, the children, the idle event stream verdict and the checks of the parts that are not re-judged from the samples (generator,
protocol mix, sampling, drained; shown "as saved"), and the line "failed before / failed now" compares with the saved verdict. A run made with an
older settle cannot be fixed offline: the tool prints the settle it had (from the marks of the sample file) and says so when `drops-idle` was measured on a
window that started early. The same function is `soak.rejudge(path)`.

## RSS attribution (recorded, not asserted)

A failing RSS slope of `dv-router` or `dv-svc-status` needs a cause, so every sample tick (30 s; 15 s in the short mode) writes one `attr` row
to `soak-samples.jsonl` next to the `proc` rows, from the same hub scrape that feeds the listener gauge (`RigControl.metrics("xlxd-sut")`,
`dvtest/metrics.py`):

- the size gauges of dv-svc-status and dv-router: every series whose name matches `soak.ATTR_INCLUDE` and none of `soak.ATTR_EXCLUDE` (counters,
  histogram parts, `_up`, timestamps), kept as `name{label=value,...}` (the `proc` label stays), at most `soak.ATTR_MAX_SERIES` a scrape;
  the names are one constant, set from the live `/metrics` text (see "Attribution names" below);
- the total number of series of the scrape (`series_total`).

The report adds `attribution_table` (printed after the counter tables): per series that moved, first and last value and the least-squares slope
per minute over the measured part, the largest slopes first, plus `scrape:series_total`. `soak.json` keeps, under `result.attribution`, the stats of
every series (`series`, `series_total`) and the raw series (`raw`: `[seconds since the first row, value]`, `scrape`), and `gauge_families` (every
non-counter family with its series count and sum at `idle0` and `measured_end`: the place to look for names that should join `ATTR_INCLUDE`).
Read a failing `rss:dv-svc-status` against the series that grew at a matching rate (events ring, positions, text history, series count).

Phases are assigned by time, not by the tick that started them: a sampling tick takes about 19 s and carries the phase it started in, so a row
of phase `measured` written after the `measured_end` snapshot (the listeners are gone: the edge RSS and the gauges drop) counts as `stopping`,
and one of phase `warmup` written after the `warmup_end` snapshot counts as `measured` (`soak.assign_phases`, applied before every judgement).

**Attribution names** (live `/metrics` of dv-router:wip-81dcc76, 142 attributed series of about 3050 in the scrape): `dv_status_clients{protocol,kind}`,
`dv_status_room_listeners{room,protocol}`, `dv_status_room_onair{room}`, `dv_status_rooms_onair`, `dv_status_controller_tokens{room}`, `dv_status_process_up`
(excluded by `_up`), `dv_pkt_presence_entries`, `dv_pkt_seen_entries`, `dv_pkt_streams_open`, `dv_pkt_ready`, `dv_edge_sessions{kind,proc}`,
`dv_edge_webrtc_listeners`, `dv_edge_webrtc_room_listeners{room}`, `dv_codec_active_sessions`, `dv_codec_queue_frames`, `dv_idsync_entries`,
`dv_launcher_children_unlabelled`, `dv_launcher_config_generation`. There is NO gauge for the status events ring length, positions or text history
in this image (the `/metrics` of the hub has none), so those cannot be attributed yet: a gauge for each would have to come from DV-Router.

## Short mode (validation)

`DVT_SOAK_SHORT=1`: 180 s warmup + 240 s measured, a sample every 15 s, about 10 minutes in all, the same mix, lanes, churn rate and criteria.
The thread and fd medians use windows of 10/50 of the measured part (48 s here, see "Threads and fds: the median rule"). Individual settings: `DVT_SOAK_WARMUP_S`, `DVT_SOAK_MEASURE_S` (`DVT_SOAK_MEASURED_S` is the same), `DVT_SOAK_SAMPLE_S`. Any other timing than
600 + 3000 is a validation run (`smoke`, a `FINDING:` line), never the criterion. The RSS bound is a rate: max(5 MiB, 10 percent) per 3000 s,
so a measured part of T seconds gets T/3000 of it (240 s: 8 percent, 410 KiB for a small child). In a short run (`DVT_SOAK_SHORT=1`) the RSS
slope is not a verdict at all: warmup dominates four minutes (state tables fill after the listeners joined), so `rss:<child>` is a lead that is
recorded and warned about (`RSS lead ...`), never a failure; the attribution series next to it say what grew. The restart rule, the flat counters and the drop-reason rules are unchanged. The real run stays the 60 minute default. Set
`DVT_SOAK_SHORT=1` only for validation: the evidence of a short run says nothing about a leak of 1 KiB a minute.

## Run it

```sh
export XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_SOAK=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/soak-edges.toml[:rig/config/dv-router/xlx-peer.toml] \
  scripts/run.sh -m rig --profile web,webload[,interlink] -- cases/test_web_soak.py      # add DVT_SOAK_SHORT=1 for the 10 minute validation
```

Without `DVT_SOAK=1` the case skips. Expected duration: about 72 minutes (about 2 minutes of set-up and initial joins inside the warmup,
60 minutes of traffic, up to 3 minutes of stopping, the 170 s settle, 2 minutes idle; the cap is 75). No `--capture` (a pcap of an hour would fill the disk); check the disk
with `touch` first. Optional settings (passed to the tester by `rig/compose.yml`): `DVT_SOAK_WARMUP_S`, `DVT_SOAK_MEASURE_S` (shorter
values are validation runs and say so; see "Short mode"), `DVT_SOAK_SAMPLE_S`, `DVT_SOAK_SHORT`, `DVT_SOAK_SEED`, `DVT_SOAK_MODULES` (`dstar=D,dmr=E,ysf=C,m17=J,usrp-a=A,usrp-b=B,nxdn=F,p25=G`; two lanes
may not share a module). With `interlink`, add `xlx-peer.toml` to `DVT_SUT_CONFIG_EXTRA` and keep modules D and E in `DVT_INTERLINK_MODULES`.
Stop early: `touch out/<run>/web/soak.stop`. One rig at a time; nothing else on the host that needs the SUT's cpu.

## The short runs (2026-11-02, image dv-router:wip-dbedd14, native launch)

Two validation runs (`DVT_SOAK_SHORT=1`, about 14 minutes each with set-up and teardown). Results that shaped the code:

- **Children** (23 distinct names, 48 processes): the launcher `dv-router`; edges `dv-edge-dcs`, `-dextra`, `-dmrplus`, `-dplus`, `-g3`, `-imrs`, `-m17`,
  `-mmdvm`, `-nxdn`, `-p25`, `-usrp`, `-webrtc`, `-xlx`, `-ysf`; `dv-codec-ambed`, `dv-data-worker`, `dv-svc-admin`, `-iddb`, `-idsync`, `-status`, `-web`
  (no `dv-svc-callhome` or `-recorder`: not enabled; no aprsis, ax25); and **26 `dv-room` processes under one name** (one per module A to Z). The
  helper therefore reports every process of a name and the sampler names them `dv-room.01` to `dv-room.26` (rank in pid order); a first version that
  read only the first match sampled one room.
- **fds** are readable for every child (the helper runs as the SUT container's user); about 27 per child, 124 for the launcher.
- **NXDN and P25** worked with `soak-edges.toml` (7 of 7 overs each, the edges started without a config error); the probe over reached both ports.
- **Cost of the sampling tick**: 23 helper containers take about 18 s (0.8 s each, one control request at a time). The 30 s period of the real run has
  12 s to spare; the 15 s period of the short mode runs back to back (`slow_ticks` about 31, effective period about 19 s).
- **Flat counters on /metrics** (74 families in all): present `dv_codec_dropped_total`, `dv_edge_drops_total`, `dv_edge_queue_drops_total`,
  `dv_edge_webrtc_audio_dropped_total`, `dv_edge_webrtc_panics_total`, `dv_fabric_put_dropped_total`, `dv_room_ingress_rejected_total`,
  `dv_status_input_dropped_total`, `dv_status_telemetry_rejected_total`; absent `dv_edge_tx_refused_total` (no RF/AX.25 edge), `dv_recorder_dropped_total`
  (no recorder), `dv_room_shell_errors_total` (nonzero values only). `dv_edge_drops_total` grew by 47 to 50 in 240 s with the traffic mix (reasons
  `link_timeout`, `no_session`, `rx_ignored`, `x_stale`: the protocol clients link, talk and leave every minute): the case fails it as ruled, it is for
  DV-Router to rule on (put it in `soak.FLAT_OVERRIDES`, or by reason). Recorded, growing, not asserted: `dv_pkt_onair_dropped_total`, `dv_pkt_reject_total`.
- **RSS** of `dv-router` (about 130 KiB/min) and `dv-svc-status` (about 110 to 125 KiB/min) rose slowly in both runs, a little over the scaled 410 KiB
  bound: two warm-up leads that the 50 minute slope must settle (state tables filling after the listeners joined?).

Table of run 2 (`out/20261102T042912Z`; the 26 `dv-room` rows are alike, 10.3 to 10.6 MiB, 15 to 21 KiB/min, 7 to 10 threads, 26 fds):

```
child             rss0 KiB  rssN KiB   slope/min    thr0    thrN    fds0    fdsN fdsIdle restart
dv-codec-ambed        8904      8904        -1.5       7       7      27      27      27       0
dv-data-worker       10536     10504         2.5       9       9      26      26      26       0
dv-edge-dcs          12212     12216        -3.9       9       8      27      27      27       0
dv-edge-dextra       12144     12176         5.3      11      11      27      27      27       0
dv-edge-dmrplus      12104     12104           0       8       8      27      27      27       0
dv-edge-dplus        12496     12512       -72.9       9       8      27      27      27       0
dv-edge-g3           11948     11948           0       8       8      29      29      29       0
dv-edge-imrs         12216     12216           0       8       8      27      27      27       0
dv-edge-m17          12112     12136         4.4      10      12      27      27      27       0
dv-edge-mmdvm        12396     12396        -0.7       9       8      27      27      27       0
dv-edge-nxdn         12328     12328        -0.7       9       8      27      27      27       0
dv-edge-p25          12424     12428         2.0       8       8      27      27      27       0
dv-edge-usrp         12368     12380        -0.3       9       8      27      27      27       0
dv-edge-webrtc       25252     25372        21.5       9       9      27      27      27       0
dv-edge-xlx          12100     12100           0       8       8      27      27      27       0
dv-edge-ysf          12136     12140         2.9       9       8      27      27      27       0
dv-router            22504     23036       129.9       8       8     124     124     124       0
dv-svc-admin         10092     10092           0       8       8      27      27      27       0
dv-svc-iddb           8948      8948           0      10      10      26      26      26       0
dv-svc-idsync         9124      9124           0       8       8      26      26      26       0
dv-svc-status        13808     14252       125.8      11      11      27      27      27       0
dv-svc-web           11560     11564         2.3       9       9      27      27      27       0
```

Verdict of run 2 (re-evaluated with the fixed `present:` rule): FAIL on `rss:dv-router` (454 of 410 KiB), `rss:dv-svc-status` (440 of 410 KiB) and
`flat:dv_edge_drops_total`; everything else passed (restarts none, threads and fds inside the limits, fds back to the idle value, 8 lanes delivered 7 of
7 overs, 121 sessions with 0 failures, 3782 lease renewals all 204, both rooms heard the USRP talkers, the idle event stream quiet, the listener gauge 0).

## The short run with attribution (2026-11-02, image dv-router:wip-81dcc76, `out/20261102T051539Z`)

One validation run after the three rulings below (`DVT_SOAK_SHORT=1`, 180 s + 240 s). Results per check:

- `counters-flat` passed (8 flat, 3 absent = 0: `dv_edge_tx_refused_total`, `dv_recorder_dropped_total`, `dv_room_shell_errors_total`; none appeared or grew).
- `drops-reasons` passed: `link_timeout` 12, `no_session` 16, `rx_ignored` 8, `x_stale` 15 in 240 s (0.21/s in all, no flag); `id_held` stayed at 7 (no growth).
- `drops-idle` FAILED: `link_timeout` grew by 3 in the 147 s idle window (it starts 45 s after the measured part ended, 30 s settle after the listeners were zero).
  A link that timed out after its client's last over is the likely source; for DV-Router to rule on (a longer settle before the idle window, or `link_timeout` allowed while idle).
  Decision (stage SOAK1 full run): SETTLE_S was raised to 120 s, then by ruling 8 to 170 s (the longest link timeout, imrs 150 s, plus a sweep tick), so that a link that ages out after its client left is not counted as idle growth.
- RSS is a lead only: `dv-router` about 155 to 170 KiB/min, `dv-svc-status` about 123 to 127 KiB/min (warnings, not failures), the 26 `dv-room` about 18 KiB/min, the rest flat.
- `threads:dv-edge-dextra` FAILED (end 11, end-of-warmup 8, limit 10): the end-of-warmup value was 8 here and 11 in the earlier runs, so the thread pool of that edge is demand driven; this is what ruling 9 (median rule) addresses. Re-judged with the median rule (`python3 -m dvtest.soak out/20261102T051539Z`) the check passes (11 vs 11 over 48 s windows) and the last sample of 11 is only flagged; `drops-idle` still fails (that run settled 31 s).
- Attribution: the scrape held 3051 to 3057 series (flat); none of the attributed gauges moved with the RSS (only the client counts of the lane mix), see "Attribution names".

## What the first full run must watch

| Watch | Why |
|-------|-----|
| `rss:dv-router`, `rss:dv-svc-status` | both rose about 110 to 130 KiB/min in the short runs (the first 7 minutes after the listeners joined); the 50 minute slope says whether it flattens or is a leak |
| `drops-reasons`, `drops-idle`, the `FLAG dv_edge_drops_total` warning | `dv_edge_drops_total` is judged by reason (see 4b): the four legit reasons only recorded and flagged above 1/s, any other reason fails, nothing may grow while idle |
| `dv_room_shell_errors_total`, `dv_recorder_dropped_total`, `dv_edge_tx_refused_total` | created at the first increment: absent is 0 and fine, a series that appears in the measured window counts as growth from 0 and fails |
| the attribution table | with a failing `rss:dv-router` or `rss:dv-svc-status`, the series that grew at a matching rate (events ring, positions, text history, series count) name the cause |
| the 26 `dv-room.NN` rows | ranks follow pid order; a restarted room shows as `no-restart:` on its rank |
| `slow_ticks` | the tick takes about 18 s against the 30 s period; a loaded host or more children would push it over |
| web session lifetime | the generator logs in once per login and keeps the cookie for the hour; an expiry would show as lease answers other than 204 (`generator-leases`) |
| churn rate seen by the SUT | `generator.left` and the status notes in the sample file against the plan (`plan.leaves`) |
| the tester | 2 GiB and 512 pids carry about 16 over threads and the sampler; a limit hit shows as sampling errors (none in the short runs) |
| disk | no `--capture`; a run directory is about 22 MiB; `touch` in the repo before the run |

## Rulings of DV-Router (2026-11-02)

1. Thresholds: limit = max(10 percent, +2 threads, +4 fds) over the end-of-warmup value; the idle fd check stays within +5 of the idle value. Done.
2. Children: every edge is its own binary (no combined edge); the allow-list is the explicit list in "Method". Done.
3. Flat counters: the 12 families above, asserted flat over the measured part, growth fails naming family and delta, per-family overrides start empty. Done.
4. NXDN and P25: `soak-edges.toml` (manual recipe, tested on dv-router:wip-dbedd14: both edges start and serve). Done.

Rulings of the DV-Router owner session (2026-11-02, later):

5. `dv_edge_tx_refused_total`, `dv_recorder_dropped_total` and `dv_room_shell_errors_total` are created at their first increment: a missing series counts as 0; assert only that none of them appears or grows in the window. No "must be present" expectation, no zero-initialisation asked for. Done (`soak.LAZY_FAMILIES`, status `absent` = 0).
6. `dv_edge_drops_total` is not flat: legit reasons `link_timeout`, `no_session`, `rx_ignored`, `x_stale`. Idle window: no reason grows. Measured window: fail only on a reason outside the four; record the four with their rate per second; flag (warning) above 1/s in total. `dv_edge_queue_drops_total` and `dv_edge_webrtc_audio_dropped_total` stay strict (missing = 0). Done (`soak.judge_drops`).
7. The RSS slope in the short run is not a verdict (warmup). The full-run rule stays: least-squares slope over the 50 measured minutes after 10 minutes of warmup <= max(5 MiB, 10 percent of the end-of-warmup RSS); the code now multiplies the slope by the whole measured window (3000 s), not by the span of the samples. Next to the RSS the case records the attribution series every tick (recorded, not asserted). Done.
8. Settle: `SETTLE_S` is 170 s after the last client left (imrs link timeout 150 s + sweep tick; other timeouts xlx/dplus/m17/nxdn/p25 30 s, dextra/dcs/ysf 60 s, mmdvm 100 s, usrp dynamic never expires, table in "Settle before the idle window"). The idle window starts there and no reason of `dv_edge_drops_total` may grow in it. Done.
9. Threads and fds are judged by the median of the last 10 minutes against the median of the first 10 minutes after warmup, FAIL only above max(10 percent, 3 threads) resp. max(10 percent, 4 fds); a single end sample above the old limit is only flagged; the window is scaled proportionally in the short run; the idle fds check (+5 over `idle0`) stays. Done (`soak.window_medians`).
10. Next to the RSS the case records the smaps_rollup values (Rss, Anonymous, Private_Dirty, Pss_Anon) of every process whose `proc-stat` reply carries them: first, last, slope per minute, not asserted. Tester side done; the host side is a patch of `scripts/lib/procstat.sh` that the main session applies after the live run.
