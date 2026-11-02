<!-- SPDX-License-Identifier: MIT
-->
# Final pass on dv-router:wip-be207d3 (EDG4 merged; the same image as wip-edg4a)

Native launch, `DVT_USRP_DYNAMIC=1`, no `--capture`, one rig run at a time, host quiet (loadavg 0.8 to 1.7 across the runs; dv-router's low-priority
fuzz did not disturb any timing). Stage 3b (WebRTC + recorder) was NOT rerun on be207d3: the two long runs on the identical image wip-edg4a count
(`docs/results-d449742.md`, bisect: all WebKit talks pass; 54 / 1 and 51 / 1 / 3 with the known tone glitch and three Playwright setup errors).
The protocol lanes (S3, M17, ident, T14) and the isolation modes were not rerun.

| # | Run | Out dir | Passed / failed / skipped | Duration | Verdict |
|---|-----|---------|---------------------------|----------|---------|
| 1 | EDG1 / K1: `test_web_k22.py -k "silently_closed or closing_only_the_ctl"` | `20261102T134504Z` | 4 / 0 / 0 | 4 min 9 s | pass |
| 2 | K22 / K22b, full file | `20261102T134922Z` | 13 / 0 / 0 | 22 min 59 s | pass |
| 3 | Short NAT, `DVT_NAT_SHORT=1`, TURN on | `20261102T141229Z` | 17 / 0 / 31 (the left-out combinations) | 16 min 03 s | pass; estimate was 19 min, limit 20 |
| 4 | Stage 5 W8 load (`test_web_load.py`) | `20261102T142849Z` | 5 / 0 / 0 | 11 min 57 s | pass |
| 5 | SOAK1, GA profile (`DVT_SOAK_GA=1`: 600 s + 2400 s) | `20261102T144054Z` | 0 / 1 / 0 | 59 min 19 s | FAIL: two RSS slopes (below) |

## 1 and 2: EDG1 and K22

EDG1 (silently closed peer, closed `ctl` channel): the listener gauge falls after 9.3 s (user, closed at once), 9.2 s (guest at once), 6.3 s (guest
after 45 s) and 9.0 s (only the `ctl` channel closed), bound 40 s; the guest lease answers 204 for about 29 s and then 401 (29.3 s and 29.8 s); a later
3 s over reaches the browser whose `ctl` channel was closed with 0 packets and the other listener with 150. The full K22 file (13 cases: revoke of one
RTC id, repeated revoke, guest lease without a peer, three silent closes, ctl channel close, foreign / malformed circuits, churn of guest B, idle
event stream, edge kill, hub outage, offer right after a hub restart) passes. Compared with `docs/results-adaptation.md` (12 / 0 / 0 on 3b0724a): the
two EDG1 cases now assert and pass (they could not pass on 3b0724a), and `test_foreign_and_malformed_circuits` now logs the other login in fresh
(test bug found on d449742: the fixture login was the oldest of more than 3 sessions of the account and had ended; see `docs/results-d449742.md`).

## 3: Short NAT profile

Measured per case: classification 54 s (with the account set-up) + 1 + 1, TURN relay Chromium / symmetric 32 s, blackhole Chromium / port restricted (every
break 2, 3, 5, 12, 16, 20 s) 175 s, WebKit / symmetric 177 s, the other seven combinations (2 s and 20 s) 52 to 56 s each, credential expiry Chromium /
symmetric 121 s (120 s observation), 429 fallback 7 + 7 + 14 s. Total 963 s against the estimate of 1140 s in `docs/web-nat-cases.md`; the document keeps the
whole-matrix command for the hour-long run. Everything the short profile still proves is listed there.

## 4: Stage 5

200 listeners x 50 pps x 60 s through the HTTP relay: 600000 of 600000 packets, 0 lost, 0 duplicate, 0 reordered, 0 invalid, listener gauge 200,
`join_refused_total{reason=core}` 0, edge CPU 10.9 percent of one core (gate 25), edge peak RSS 32.2 MiB (gate 64; 69.7 KiB per listener), worst-listener
jitter p99 1.54 ms (gate 20) and max 8.98 ms, source missed no deadline (worst 0.062 ms), edge pid unchanged. The calibration steps (1 / 10 / 50 listeners,
CPU 0.8 / 1.4 / 3.5 percent) and the flood case (public reads 429 after 21, login 429 after 10, per-address cap 503, trusted proxy exempt) are as before.
Compared with the EDG2 confirmation (81dcc76, 600000 / 600000): same; the earlier failure on 3b0724a (216000 lost, F9) is gone.

## 5: SOAK1, GA profile: FAIL (two RSS slopes)

`FINDING: SOAK1 GA profile`: warmup 600 s + measured 2400 s, a sample every 30 s, every limit scaled by 0.8 (RSS bound 4096 KiB for a small child),
settle 170 s, idle 2 minutes; no child restart. Lanes: D-STAR, DMR, YSF, M17, NXDN, P25, USRP-A, USRP-B (8 lanes x 50 overs, 400 of 400 delivered, 0 errors) and
two WebRTC rooms with 100 listeners and churn (337 sessions, 0 failed, 29582 lease answers all 204, 1.25 M and 1.21 M packets heard in rooms A and B).

| Child | RSS start KiB | RSS end KiB | slope KiB/min | x 2400 s KiB | limit KiB | verdict |
|-------|---------------|-------------|---------------|--------------|-----------|---------|
| dv-router (launcher) | 23512 | 29428 | 145.8 | 5830 | 4096 | FAIL (Anon grows the same 145.8) |
| dv-svc-status | 14728 | 19420 | 119.4 | 4776 | 4096 | FAIL (Anon 119.4) |
| dv-edge-webrtc | 25552 | 26648 | 29.5 | 1180 | 4096 | pass |
| dv-room.01 .. .26 (each) | about 10.5 MiB | about 11.3 MiB | 19.3 to 20.8 | about 800 | 4096 | pass |
| other edges (dcs, dextra, dmrplus, dplus, g3, imrs, m17, mmdvm, nxdn, p25, usrp, xlx, ysf) | 12.1 to 12.7 MiB | same | 0 to 0.8 | < 30 | 4096 | pass |
| dv-codec-ambed, dv-data-worker, dv-svc-admin, -iddb, -idsync, -web | 8.9 to 11.5 MiB | same | 0 to 0.5 | < 20 | 4096 | pass |

Threads and fds: flat and equal to the idle value for every child (dv-router 8 threads and 124 fds; dv-edge-m17 10 to 13 threads, inside the median rule;
fds after the idle check equal to the end value for all). The 12 flat counters stay 0 (`dv_edge_webrtc_audio_dropped_total` 0, panics 0, queue drops 0,
`dv_status_*` and `dv_fabric_put_dropped_total` 0; three families absent: `dv_edge_tx_refused_total`, `dv_recorder_dropped_total`, `dv_room_shell_errors_total`).
`dv_edge_drops_total` by reason: link_timeout 27 -> 147, no_session 40 -> 180, rx_ignored 22 -> 102, x_stale 29 -> 113, id_held flat; all legit reasons,
0.177 per s over 2401 s (flag at 1 per s).

What grew, from the recorded series (smaps, events, streams): for dv-svc-status the growth fits the hub event ring: 6035 events in the measured part (150.9 per
minute, 8 streams per minute), an estimate of 175 KiB/min at 1.16 KiB per event against the slope of 119 KiB/min; 7774 events had been counted by the end and
the ring of 10000 events would fill in about 15 more minutes, so a plateau after about 55 minutes is expected. For dv-router the slope is 18.2 KiB per stream
(320 streams in the measured part), for the 26 rooms together 63.7 KiB per stream. Of 141 attribution series only 8 moved, all gauges of sessions and
listeners. The case therefore fails honestly on the limit; whether the growth is bounded (ring) is what the isolation modes A, C and F were designed to show
and they were not run here.

## Differences from docs/results-adaptation.md

| Item | Before | Now |
|------|--------|-----|
| K22 | 12 / 0 / 0 (3b0724a) | 13 / 0 / 0 (EDG1 cases included, foreign-circuit case with a fresh login) |
| NAT with TURN | whole matrix 36 / 2 / 9 in 1 h 2 min | short profile 17 / 0 / 31 in 16 min (a subset by design) |
| Stage 5 | 3b0724a: 216000 lost (F9); 81dcc76: 600000 of 600000 | be207d3: 600000 of 600000, CPU 10.9 percent, RSS 32.2 MiB |
| SOAK1 | no full run before (validation runs only) | GA profile: two RSS slope failures (dv-router, dv-svc-status) |

## Commands

```sh
export XLXD_SUT_IMAGE=dv-router:wip-be207d3 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
W=rig/config/dv-router/web.toml
DVT_SUT_CONFIG_EXTRA=$W:rig/config/dv-router/guest.toml scripts/run.sh -m rig --profile web,browser -- cases/test_web_k22.py -k "silently_closed or closing_only_the_ctl"
DVT_SUT_CONFIG_EXTRA=$W:rig/config/dv-router/guest.toml scripts/run.sh -m rig --profile web,browser -- cases/test_web_k22.py
DVT_NAT_SHORT=1 DVT_SUT_CONFIG_EXTRA=$W:rig/config/dv-router/turn.toml scripts/run.sh -m rig --profile web,browser,nat -- cases/test_web_nat.py
scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py
DVT_SOAK=1 DVT_SOAK_GA=1 DVT_SUT_CONFIG_EXTRA=$W:rig/config/dv-router/soak-edges.toml scripts/run.sh -m rig --profile web,webload -- cases/test_web_soak.py
```
