<!-- SPDX-License-Identifier: MIT -->
# Web unit: SOAK1, the 60 minute soak (WebRTC listeners and protocol clients together)

`cases/test_web_soak.py` (profiles web and webload, native launch), `cases/soak_support.py` (sampler, protocol lanes, over runner),
`dvtest/soak.py` (pure: the traffic schedule, the sample file reader and the pass criteria; unit tests `cases/unit/test_soak.py`,
`cases/unit/test_soak_support.py`, and `cases/unit/test_soak_case.py`, which runs the real case function against fakes in seconds), the
generator's soak mode (`dvtest/tools/webload_gen.py`) and the read-only control action `proc-stat` (`scripts/lib/procstat.sh`,
`dvtest/control.py`). Spec from DV-Router (2026-11-02): 60 minutes = 10 minutes warmup + 50 minutes measured, ACL on (the launcher
default). Adjusted to DV-Router's rulings of 2026-11-02 (thresholds, child names, flat counters, NXDN/P25 recipe). **Status: the short
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
   counter snapshots at the end of warmup and of the measured part (the flat families, their series and, once, the names of all families on /metrics). Each sample: pid, start time, VmRSS, VmHWM, threads, open fds, cpu ticks.
   Also per tick: the edge listener gauge and, every five minutes, the generator's status.
4. Stop: the protocol overs end, the generator finishes (every listener revokes and closes), the gauge must read 0 within 150 s, 30 s of
   settling, then the idle window of 120 s (phase `idle`): `GET /api/v1/public/events` is sampled (resumed every 30 s with the last event
   id, as `test_status_stream_is_quiet_while_idle`), samples continue, a last sample and the counter snapshots close it.
5. The raw samples are ONE file, `out/<run>/web/soak-samples.jsonl` (one JSON object per line, kinds `proc`, `metrics`, `gauge`, `mark`,
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
| 2 | RSS | growth over the measured part = least-squares slope x the measured span, must be <= max(5 MiB, 10 percent of the RSS at the end of warmup, the median of the last 3 warmup samples); the last sample is not judged. Short mode: the bound is scaled by measured/3000 (see "Short mode") |
| 3 | threads, fds | ruling of DV-Router: limit = max(10 percent, +2 threads, +4 fds) over the end-of-warmup value (median of the last 3 warmup samples); judged on the median of the last 5 measured samples. After the traffic stopped the fd count is <= the quiet value of `idle0` (before any traffic) + 5 |
| 4 | flat counters | the 12 families below are FLAT over the measured part: value at the end of warmup == value at the end, absolute zero growth, summed over all labels and processes. A family that is not on /metrics is `absent` (no assertion); a family that first appears after warmup counts from 0 (`dv_room_shell_errors_total` exports nonzero values only); a family on /metrics at the end of warmup and gone at the end is `vanished` (recorded). On growth the check `flat:<family>` fails with the family, the delta and the series that grew. `soak.FLAT_OVERRIDES` (per family, starts empty) holds a family that grows with the traffic mix: it is recorded with name and growth (`override`), not asserted, until DV-Router rules on it. All other error-like families (name contains drop, refus, reject, denied, panic, error, overflow, overrun, lost, fail, timeout, discard) are recorded with their deltas, `counters.growing` lists those that grew; nothing else is asserted |
| 5 | idle event stream | `k22.idle_stream_verdict`: no whole minute after the first holds more than 2 events |
| + | idle | the listener gauge reads 0 in every idle sample |
| + | generator | at most 2 percent of the sessions failed, at most 2 percent of the lease answers other than 204, and the listeners of each room heard at least 50 packets (this is how the USRP lanes are seen) |
| + | protocol mix | each required lane made at least 5 overs and delivered at least half of them to its listeners |
| + | sampling | no damaged line in the file, at most 3 sampling errors |

A child whose fds cannot be read (`fds=-1`) is listed under `unmeasured` and as a `FINDING:` line; it does not fail the run, but it is never reported as passed: it
shows in the report. The helper container of `proc-stat` runs as the user of the SUT container (root when the image names none, no capability)
because `/proc/<pid>/fd` is readable only for the same uid.

Flat families (`soak.FLAT_FAMILIES`, DV-Router `docs/design/status.md` "Metrics"): `dv_edge_webrtc_panics_total`, `dv_edge_drops_total`,
`dv_edge_queue_drops_total`, `dv_edge_webrtc_audio_dropped_total`, `dv_room_ingress_rejected_total`, `dv_room_shell_errors_total`,
`dv_status_telemetry_rejected_total`, `dv_status_input_dropped_total`, `dv_fabric_put_dropped_total`, `dv_recorder_dropped_total`,
`dv_codec_dropped_total`, `dv_edge_tx_refused_total`. The table of the flat families is printed after the table per child and kept in `soak.json`
(`counters_table`, `metric_families`).

## Short mode (validation)

`DVT_SOAK_SHORT=1`: 180 s warmup + 240 s measured, a sample every 15 s, about 10 minutes in all, the same mix, lanes, churn rate and criteria.
Individual settings: `DVT_SOAK_WARMUP_S`, `DVT_SOAK_MEASURE_S` (`DVT_SOAK_MEASURED_S` is the same), `DVT_SOAK_SAMPLE_S`. Any other timing than
600 + 3000 is a validation run (`smoke`, a `FINDING:` line), never the criterion. The RSS bound is a rate: max(5 MiB, 10 percent) per 3000 s,
so a measured part of T seconds gets T/3000 of it (240 s: 8 percent, 410 KiB for a small child). That is as sharp in KiB per minute as the
real run and noisier (a few allocator steps weigh more in four minutes), so an RSS failure of a short run is a lead, not a verdict; the thread
and fd rules, the restart rule and the flat counters are unchanged. The real run stays the 60 minute default. Set
`DVT_SOAK_SHORT=1` only for validation: the evidence of a short run says nothing about a leak of 1 KiB a minute.

## Run it

```sh
export XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_SOAK=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/soak-edges.toml[:rig/config/dv-router/xlx-peer.toml] \
  scripts/run.sh -m rig --profile web,webload[,interlink] -- cases/test_web_soak.py      # add DVT_SOAK_SHORT=1 for the 10 minute validation
```

Without `DVT_SOAK=1` the case skips. Expected duration: 70 minutes (about 2 minutes of set-up and initial joins inside the warmup,
60 minutes of traffic, up to 3 minutes of stopping, 2 minutes idle). No `--capture` (a pcap of an hour would fill the disk); check the disk
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

## What the first full run must watch

| Watch | Why |
|-------|-----|
| `rss:dv-router`, `rss:dv-svc-status` | both rose about 110 to 130 KiB/min in the short runs (the first 7 minutes after the listeners joined); the 50 minute slope says whether it flattens or is a leak |
| `flat:dv_edge_drops_total` | grew with the traffic mix in the short runs; expect it to fail until DV-Router rules (FLAT_OVERRIDES or a narrower family) |
| `dv_room_shell_errors_total` | exports nonzero values only: absent at the end of warmup is fine, a series that appears later counts as growth from 0 |
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

Open for DV-Router: the verdict on `dv_edge_drops_total` growth (reasons `link_timeout`, `no_session`, `rx_ignored`, `x_stale` under the soak's link/unlink overs).
