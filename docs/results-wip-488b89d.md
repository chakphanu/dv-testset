<!-- SPDX-License-Identifier: MIT -->
# Acceptance of dv-router:wip-488b89d (COMPAT1) before deploy

Image `dv-router:wip-488b89d`. Rig prepared for the compat1 generation (`DVT_SUT_GEN=compat1`, commit 2168cd2 / 5ad7701 / d9a3fb9; see
`docs/rig.md`, "compat1"): `operator_totp = true` / `totp = true` lines dropped, other values renamed to `totp_required`, every
`[[edges.xlx.peer]]` row gets `kind = "xlx"`, `DV_DMRHB_*` environment names, 51 web operations, `dv_edge_data_total` gone (its successors
`dv_edge_raw_total` / `dv_edge_render_total` are read by sums). Native launch (import for the AllStar application cases), no `--capture`
unless noted, one rig run at a time, run from a separate worktree. The suite ran BEFORE the deploy; DV-Router gave GO after the SOAK1 and the
three reruns below and production switched to this image.

## Summary

| # | Run | Out dir | Passed / failed / skipped | Verdict |
|---|-----|---------|---------------------------|---------|
| 0 | Config load (`-m cfgload`; old keys fail with a clear error, `config check` exit 2) | local | 19 / 0 / 0 | pass |
| 1a | AllStar lab, all of `test_asl_edge.py` (long audio cases) | `20261106T021115Z` | 22 / 0 / 8 | pass; skips as on wip-2040fce |
| 1b | AllStar / USRP application cases (`DVT_SUT_LAUNCH=import`) | `20261106T022948Z` | 42 / 0 / 0 | pass |
| 2 | Listen-load W8 | `20261106T023521Z` | 5 / 0 / 0 | pass, table below |
| 3a | Web API + smoke | `20261106T024730Z` | 372 / 0 / 0 | pass |
| 3b | Browser UI (chromium, firefox, WebKit) | `20261106T025257Z` | 54 / 0 / 6 | pass |
| 3c | WebRTC + recorder | `20261106T030423Z` | 54 / 1 / 0 | the one failure is the known WebKit tone glitch (peak 3600 Hz against 700 Hz, as on 239382b); alone it passed 3 / 0 / 0 (`20261106T055606Z`) |
| 3d | K22 / K22b / EDG1 | `20261106T031728Z` | 13 / 0 / 0 | pass |
| 3e | NAT short with TURN | `20261106T034126Z` | 17 / 0 / 31 | pass; skips are the full-profile and 404-fallback cases |
| 4 | Host-name peer rows, XLX (`xlx-peer-host`, `-host-bad`, literal) | `20261106T053833Z`, `T054424Z`, `T055015Z` | 13 / 0 / 1 each | pass; a first try ran with the c2465de rows (no `kind`) and the image refused to start with a clear error, as intended |
| 5 | Echo E1 to E7 + telemetry | `20261106T040612Z` | 31 / 0 / 2 | pass |
| 6 | Echo E9 (dextra, dplus, dcs, g3) | `20261106T041229Z` | 4 / 0 / 1 | pass, see "Echo" |
| 7 | SOAK1 standard (600 s warmup + 3000 s measured) | `20261106T042615Z` | 1 / 0 / 0 | pass, 48 children, no restart |
| 8 | TOTP1 optional | `20261106T053604Z` | 14 / 0 / 0 | pass |
| 9 | Data ident `require_ident` true / false | `20261106T070903Z`, `T071409Z` | 8 / 0 / 0 each | pass, with the edge raw / render assertions |
| 10 | YSF join measurement (FICH decode, counters) | `20261106T060742Z`, DMR `20261106T065704Z` | 6 / 0 / 0, 1 / 0 / 0 | see "YSF short" |

## Listen-load gate (W8, 200 listeners x 50 pps)

| Measure | gate | wip-488b89d |
|---------|------|-------------|
| edge cpu, percent of one core | 25 | 10.53 |
| edge peak RSS (VmHWM), MiB | 64 | 31.77 |
| edge RSS growth over startup, KiB | 24096 | 13940 |
| worst-listener jitter p99 / max, ms | 20 / 60 | 1.58 / 10.29 |
| lost / duplicate / reordered / invalid / missed deadlines | 0 | 0 |

## SOAK1 standard

Judge: `dv-svc-status` by the ring rule (BOUND + PLATEAU, `docs/web-soak-cases.md` row 2b), every other child by the whole-window slope.
`dv-svc-status`: growth 5876 KiB over 3000 s (whole-window slope 117.5 KiB/min), bound 19016 KiB, 9142 events counted, "ring not full:
bound only"; ok. `dv-edge-webrtc` 23.8 KiB/min (1189 KiB over the window, limit 5120), `dv-router` 2.6, `dv-svc-web` 0.1, every other
child between -0.0 and 0.4 KiB/min; threads and fds flat, no restart, every drop / error counter flat.

## YSF `result=short` (+2): the join pair

The tester's FICH decode of the YSF client traffic (`dvtest/proto/ysf.py`, capture of the SUT): every client join sends a header
(FI=0, counter 0) and at once a terminator (FI=2, counter 1, end bit) with no voice frame (FI=1) between. Room `e` counters: before the link in
215 / out 213, short 2; after the link of two clients with no voice in 217 / out 213, short 4; after one 150 frame over and its replay in
369 / out 365, played 3, short 4. So each join pair is counted as one all-zero unit (frames in +1, result `short` +1) and nothing is
echoed; the real over loses nothing (150 in, 150 out plus the pair). The DMR join does not show it: the same probe counts 61 in / 61 out as
one `played` over and no `short`. YSF-specific, not an audio loss, not a deploy blocker (agreed with DV-Router; ECHO1f).

## Echo

E9 on this image: dextra, dplus, dcs and g3 with realistic senders (10 overs of 330 frames): no lost frame, no gap > 25 ms, seq byte
right on all three streams of every kind (defect B is fixed from wip-dce9d0a on). One recorded difference: for the G3 sender the echo to the
terminal carries RPT2 `XLX999 E` (what the terminal sent) while the copy a DExtra listener hears carries `XRF999 E`; not asserted for the
G3 kind (finding in `echo.txt`).

## Data ident counters (compat1)

`dv_edge_raw_total` moves by 1 for every data item the edge sees; `dv_edge_render_total` only for a delivered item (accepted N0CALL-7: raw +1,
render +4; unidentified under `require_ident=true`: raw +1, render 0). The cases assert exactly that by sums (requested by DV-Router); the
verdict itself still comes from `dv_room_data_total{result}` and `dv_pkt_items_total{result}`.
