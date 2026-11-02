<!-- SPDX-License-Identifier: MIT -->
# Web unit: the RSS isolation experiment (one activity per run)

`cases/test_web_rss_isolation.py` (profiles web and webload, native launch), `dvtest/isolation.py` (pure: the mode table, the plan, the
statistics, the "activity ran" checks, the document and the offline summarizer `python3 -m dvtest.isolation`), `dvtest/isolation_plugin.py`
(pytest options), `dvtest/events.py` (pure: the hub events and streams records, shared with SOAK1), and the SOAK1 machinery it reuses (`cases/soak_support.py`, `cases/test_web_soak.py`, `docs/web-soak-cases.md`).
Unit tests: `cases/unit/test_isolation.py` (pure logic, summarizer, plugin), `cases/unit/test_events.py` (events and streams) and `cases/unit/test_isolation_case.py` (the real case function
against fakes in seconds, the lane login cycle, the `Traffic` method switch).

## Purpose

In SOAK1 `dv-router` (the launcher) and `dv-svc-status` grow linearly (about 137 and 113 KiB/min). SOAK1 runs everything at once, so it cannot say
which activity drives the growth. This case runs ONE activity per rig run, with the same sampler, traffic lanes, WebRTC listener generator
(join/leave churn), `proc-stat` samples (with the smaps_rollup keys Rss, Anon, Private_Dirty, Pss_Anon) and hub `/metrics` scrape as SOAK1, and
writes DATA. It judges nothing except that the activity ran (a lightweight check), so a run is never "failed" for growing.

## Modes

| Mode | Name | Activity | Not running |
|------|------|----------|-------------|
| A | `idle` | all children up, no clients at all | everything |
| B | `webrtc-churn` | 100 stable WebRTC listeners (50 in room A, 50 in room B) + about 5 joins/leaves a minute (the SOAK1 churn plan) | no voice: no source talks |
| C | `voice` | the protocol lanes (usrp-a, usrp-b, dstar, dmr, ysf, m17, and nxdn, p25 when their probe works) with overs as in SOAK1: about 10 s per module per minute, spread over the minute, every fifth minute all at once | no WebRTC listeners |
| D | `proto-churn` | protocol clients log in and out every few seconds on all lanes (each lane has its own period of 4 to 8 s, +- 25 percent jitter; a login lives 1 to 2.5 s): the same clients as the lanes (D-STAR DExtra talker with DPlus and DCS listeners, MMDVM, YSF, M17, USRP, NXDN, P25) | no voice (the pre-clock probe is a login too) |
| E | `webrtc-voice` | 100 stable WebRTC listeners, voice in ONE room: one protocol source (`usrp-a` into module A, about 10 s a minute; `DVT_ISOLATION_E_LANE=usrp-b` talks into room B instead) | no churn |
| F | `voice-x3` | the SAME lane mix as C with **3x as many overs**: three per lane and minute instead of one (exact factor below) | no WebRTC listeners, no churn |

Mode F is C at three times the over frequency (`iso.build_overs_scaled`, `Mode.voice_factor` 3): every minute is split into three slots of 20 s and every slot
holds one over of every lane (about 10 s, 9 to 11 s, as in SOAK1), at the SOAK1 offset scaled by 1/3 (lanes spread over 15 s of the slot plus 0 to 2 s), so overs of
different lanes overlap as in C; every fifth minute all lanes start together in each of the three slots (the SOAK1 storm, three times). Two overs of one lane are at
least 15 s apart (`OVER_GAP_S`, longer than an over), so a lane never talks over itself; a start that would be closer (a normal slot into a storm slot) is moved
later. The 15 minute plan is therefore exactly 3 x the C plan: 45 overs per lane instead of 15 (`plan.overs` = 3 x 15 x lanes). Streams per minute are about three times
those of C, the other activity is identical, so C against F says what the number of distinct streams does to dv-router, dv-room and dv-svc-status.

Timing: 5 minutes warmup + 10 minutes measured, a sample of every child every 30 s (20 samples in the measured part), 15 minutes in all;
`DVT_ISOLATION_SHORT=1` is the smoke run (2 + 3 minutes, a sample every 15 s, a `FINDING:` line says so). There is no idle or settle phase:
the sampler stops at the end of the measured part, then the traffic and the listeners are torn down. Phases follow the counter snapshots
`warmup_end` and `measured_end` (`soak.assign_phases`), as in SOAK1. The initial joins of the 100 listeners take about 105 s (the W8 pacing), inside the warmup.

## What it writes

`out/<run>/web/isolation-<letter>.json` (the letter of the mode: `isolation-A.json` ... `isolation-F.json`; rewritten at every milestone, an
aborted run leaves its state), the raw rows `out/<run>/web/isolation-<letter>-samples.jsonl` (the SOAK1 sample format), and the stop file
`out/<run>/web/isolation.stop`.

| Key | Content |
|-----|---------|
| `mode`, `name`, `activity`, `run`, `timing`, `plan` | what ran: the plan counts (sessions, leaves, overs per lane, cycles per lane) |
| `focus` | `dv-router` and `dv-svc-status`: per child record (below), `null` when the child was not sampled |
| `children`, `children_table` | the same record for EVERY child (`dv-room.NN` ranks included) and a text table ordered by the VmRSS slope |
| child record | `n` measured samples; `vmrss`, `threads`, `fds`: `{n, first, last, min, max, slope_per_min}`; `smaps`: the same stats for `rss`, `anon`, `private_dirty`, `pss_anon` (KiB; absent keys when the `proc-stat` helper has no smaps keys); `restarts` |
| `series_total` | the total series count of the hub scrape: first, last, slope per minute over the measured part |
| `gauge`, `traffic`, `generator` | the edge listener gauge (min, max in the measured part), the overs/cycles per lane, the generator summary (sessions, leaves, packets heard per room: recorded, so a mode without voice can be checked to be silent) |
| `checks`, `ok` | the activity checks (below); `ok` is the AND |
| `hub_events`, `events_note` | the events of the hub, counted continuously (below) |
| `streams` | the distinct streams (overs delivered) and the RSS slope per stream (below) |

All slopes are least squares over the measured samples, KiB per minute, from the time stamp the `proc-stat` helper takes inside the container.

The activity checks (`activity:*`, the only judgement): `sampled` (dv-router and dv-svc-status have at least 5 measured samples), `sampling` (no damaged
line, at most 3 sampling errors), `ended`; with listeners `listeners-connected` (at least 90 percent of the seats connected, at most 2 percent failed)
and `listeners-gauge` (the hub gauge peaked at 90 percent of the seats); with churn `churn` (at least half of the planned leaves happened); with voice
`voice:<lane>` per lane (at least half of the planned overs ran and half of those worked; for the USRP lanes: the talk was sent); in E
`room-heard:A` (the listeners of the talked room received packets); in D `cycles:<lane>` per lane. A failed check fails the case AFTER the data is written.

## Hub events and streams (recorded, never asserted)

Two records next to every RSS slope, to test the DV-Router hypotheses that the growth of `dv-svc-status` (about 113 KiB/min in SOAK1) is the fill of its events ring
(10,000 events of about 1.16 KiB, not full after 50 minutes) and that `dv-router` / `dv-room` grow once per distinct stream (a new Zenoh liveliness token key per over).
SOAK1 writes the same keys into `soak.json` (`docs/web-soak-cases.md`).

**`hub_events`** (`events_note` repeats its sentence at the top of the document). A thread (`ss.EventsReader`) reads `GET /api/v1/public/events` (the endpoint
`idle_events` uses, no login) from the start of warmup to the end of the measured part and stamps every event with its arrival time. It keeps one connection for 60 s and
resumes at once with `last_event_id` (a roll-over, `rollovers`, no gap); a stream that ends early, an error or a client that cannot be built is a **gap** (from the loss to
the next connection, `gaps`, `reconnects`, `gap_s_total`) and the reader reconnects after 1, 2, 5 s; it never raises and a replayed id is counted once (`duplicates`).

| Key | Meaning |
|-----|---------|
| `events_total`, `by_kind` | events counted from the start of warmup to the end of the measured part, and per event name (`hello` is not an event) |
| `events_per_min_series` | `[{minute, t, events, gap_s}]` per minute from the start of reading (`gap_s`: seconds of that minute inside a gap) |
| `first_event`, `last_event` | `{t, event, id}` |
| `measured.events`, `measured.mean_per_min`, `measured.observed_s`, `measured.by_kind` | the measured part only; the mean divides by the window minus the gaps |
| `events_kib_per_min` | the estimate `mean_per_min x 1.16` (`kib_per_event`) |
| `status_rss_slope_kib_per_min`, `status_anon_slope_kib_per_min` | the measured `dv-svc-status` slopes (smaps Rss, VmRSS when the run has none; Anon when present) |
| `ratio_to_rss`, `ratio_to_anon`, `match` | estimate / slope (1.0: the events alone explain the slope); `match` is `matches` for 0.7 to 1.4, `part` below, `above` over, `unknown` without a positive slope |
| `cumulative_at_end`, `ring`, `ring_full`, `minutes_to_fill` | events counted by the end of the measured part against the ring of 10000 and the minutes left at this rate. Events before the reader started are not seen, so the true count is at least this |
| `hub_metric` | any family of the hub `/metrics` with "event" in its name, summed over labels, first/last/delta (`per_min` for `_total` counters) in the measured part. `names_seen` is `[]` when there is none: the SOAK1 image of 2026-11-02 (`out/20261102T054042Z`) has none (its families are `dv_pkt_*`, `dv_status_*`, `dv_web_sse_streams`; `event_dropped` is only a reason label of `dv_edge_drops_total`), so the stream is the only count |
| `events_note` | one sentence: events and rate, the estimate against the slope and the ratio, whether the ring would be full by the end |

**`streams`**. From the per-over results of the traffic (mode D logs in and out without voice: `streams_total` 0). An over counts when it was delivered (`ok`; for the USRP lanes:
the talk was sent) and started inside the measured part.

| Key | Meaning |
|-----|---------|
| `streams_total`, `streams_per_min` | overs delivered in the measured part and per minute of it; `run_total` is the whole run, `scheduled_total` includes failed overs |
| `per_lane`, `per_module`, `per_room` | the same count per lane, per module letter and per room (a room is the module's room, so `per_room` equals `per_module`) |
| `slope_per_stream` | for `dv-router`, `dv-svc-status` and `dv-room` (the SUM of its `dv-room.NN` ranks): `rss_kib_per_min`, `anon_kib_per_min` and `rss_kib_per_stream`, `anon_kib_per_stream` = KiB/min divided by streams/min. A child that grows per stream shows a similar KiB/stream across C and F; one that grows with time shows a KiB/stream that falls to a third in F |

## Summarizer

```sh
python3 -m dvtest.isolation out/<run> [out/<run> ...]     # run directories, their web directories or isolation-*.json files
python3 -m dvtest.isolation --children out/2*             # also the table of every child per run
```

One table across the available files: mode x (dv-router, dv-svc-status) x (Rss, Anon, Private_Dirty) slope in KiB/min, one row per file (`ok` = the
activity ran). Rss is the smaps_rollup Rss, VmRSS when the run has no smaps keys; `-` is not measured. Exit status 1 when no file was found.

A second table follows when a file has the records above (older files have none): one row per file with `events/min` (mean over the measured part), `est KiB/m`
(the estimate), `svc Rss/m` (the measured dv-svc-status Rss slope), `ratio` (estimate / slope), `streams/m` (streams per minute) and `router/str`, `room/str`,
`status/str` (the Rss slope per stream of dv-router, dv-room and dv-svc-status in KiB). `--children` also prints the `events_note` of each file.

## Run it

The same environment as the SOAK1 full run (`docs/web-soak-cases.md`), profile `web,webload`, one rig run per mode, no `--capture`, native launch.
`soak-edges.toml` is needed for the NXDN and P25 lanes of modes C and D (without it they are dropped with a reason, as in SOAK1).

```sh
export XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
export DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/soak-edges.toml

scripts/run.sh -m rig --profile web,webload -- -p dvtest.isolation_plugin --isolation=A cases/test_web_rss_isolation.py    # mode A, idle
# B: --isolation=B   C: --isolation=C   D: --isolation=D   E: --isolation=E   F: --isolation=F
#   (or the names: idle, webrtc-churn, voice, proto-churn, webrtc-voice, voice-x3)
# add --isolation-short for the 2 + 3 minute smoke run; --isolation-env DVT_ISOLATION_SEED=2 / DVT_ISOLATION_E_LANE=usrp-b / DVT_SOAK_MODULES=...
```

`rig/compose.yml` forwards only the variables it lists to the tester container and `DVT_ISOLATION*` is not among them, so the mode is given as
pytest options through the plugin `dvtest/isolation_plugin.py` (`-p dvtest.isolation_plugin`; `scripts/run.sh` passes everything after `--` to pytest). The
case itself reads `DVT_ISOLATION`, `DVT_ISOLATION_SHORT`, `DVT_ISOLATION_WARMUP_S` (300), `DVT_ISOLATION_MEASURE_S` (600), `DVT_ISOLATION_SAMPLE_S`
(30), `DVT_ISOLATION_SEED` (`DVT_SOAK_SEED`, 1), `DVT_ISOLATION_E_LANE` (usrp-a) and `DVT_SOAK_MODULES`; without `DVT_ISOLATION` it skips. If you prefer plain
environment variables, add these to the `tester` environment of `rig/compose.yml` next to the `DVT_SOAK_*` lines (and drop the plugin arguments):

```yaml
      DVT_ISOLATION: ${DVT_ISOLATION:-}
      DVT_ISOLATION_SHORT: ${DVT_ISOLATION_SHORT:-}
      DVT_ISOLATION_WARMUP_S: ${DVT_ISOLATION_WARMUP_S:-}
      DVT_ISOLATION_MEASURE_S: ${DVT_ISOLATION_MEASURE_S:-}
      DVT_ISOLATION_SAMPLE_S: ${DVT_ISOLATION_SAMPLE_S:-}
      DVT_ISOLATION_SEED: ${DVT_ISOLATION_SEED:-}
      DVT_ISOLATION_E_LANE: ${DVT_ISOLATION_E_LANE:-}
```

Check the disk with `touch` first (no capture; a run directory is a few MiB). Expected duration per mode: about 18 to 20 minutes (set-up and the lane probes about 2 minutes, 5 + 10 minutes of activity, up to 3 minutes of teardown;
the hard cap is 25 minutes). The generator service (`webload`) is used only by B and E; A, C, D and F need the profile but not the generator. Stop early with
`touch out/<run>/web/isolation.stop` (the case then fails with "no data", the teardown is the same). One rig at a time.

All six, one after the other (about 2 hours), then the tables:

```sh
for m in A B C D E F; do
  scripts/run.sh -m rig --profile web,webload -- -p dvtest.isolation_plugin --isolation=$m cases/test_web_rss_isolation.py || echo "mode $m: exit $?"
done
python3 -m dvtest.isolation out/2*
```

## Reading the result

Run A is the floor: whatever dv-router and dv-svc-status grow by at idle (timers, scrape, the status poll). B adds the listeners and their churn (circuits,
leases, presence), C adds only protocol voice, D only protocol login state, E adds voice heard by held listeners, F is C with three times the streams.
Read the events and streams table with it: if `ratio` is near 1 the `dv-svc-status` slope is what the events alone would fill, and a ring that is not full by the end (`ring_full` false)
points to a fill, not a leak; if the slope stays when the ring is full, or `ratio` is far from 1, it is something else. For dv-router and dv-room compare KiB per stream
between C and F: equal values mean growth per stream, values that fall to about a third mean growth with time. Compare the Anon and Private_Dirty slopes with the
Rss slope: a growth in Rss that is not in Anon or Private_Dirty is file-backed or shared pages, not heap. `series_total` slope says whether the scrape itself grows.
A 10 minute window gives a slope to within a few KiB/min for a 100 KiB/min leak; below about 10 KiB/min the numbers are noise (the table of `--children` shows the quiet children
of the same run for comparison). The 5 minute warmup excludes the initial fill (listeners join in the first 105 s); a child that is still filling at 5 minutes shows a first-to-last
change with a falling rate, not a straight line.
