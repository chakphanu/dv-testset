<!-- SPDX-License-Identifier: MIT -->
# Acceptance of dv-router:wip-2040fce (WIRE2) before deploy

Image `dv-router:wip-2040fce`, id `sha256:056e3e0e17a9...` (local build; tar `dv-router-wip-2040fce.tar`, sha256
`a942069cbb556173cee930c763d2c108ec5246a1efae0a07a6f1b6739de0842c`, matches the file loaded). It is wip-239382b (deployed on XLX345) plus WIRE2:
AllStar and WebRTC own their transport by value, no shared Wire lock or driver, inputs synchronous at their original hook, outputs buffered
and flushed at defined points. Intended wire behaviour change: none. The suite ran BEFORE any deploy. Native launch (import where the row
says so), `DVT_SUT_GEN=c2465de`, `DVT_USRP_DYNAMIC=1`, no `--capture`, one rig run at a time, run from a separate worktree. The numbers of
wip-239382b in brackets are from the same rig and the same cases (`docs/results-wip-239382b.md`).

## Summary

| # | Run | Out dir | Passed / failed / skipped | Duration | Verdict |
|---|-----|---------|---------------------------|----------|---------|
| 1a | AllStar lab, all of `test_asl_edge.py` (`DVT_ASL_EDGE_OUT=1`, `DVT_ASL_EDGE_LOOKUP=1`, `DVT_ASL_LONG=1`: T3 T5 T6 T7 T8 T8b T9 T10 T11 and the long T1 / T1b / T2 audio cases) | `20261105T185046Z` | 22 / 0 / 8 | 18 min 26 s | pass; the 8 skips are T4b (needs a rig without outbound rows) and R1-R7 (need `DVT_ASL_EDGE_REG=1`) |
| 1b | AllStar T4 / T4b without outbound rows (`-k t4`) | `20261105T193338Z` | 2 / 0 / 0 | 29 s | pass |
| 1c | AllStar / USRP application cases (`test_allstar.py`, `proto/test_usrp.py`, `DVT_SUT_LAUNCH=import`, `--profile allstar`) | `20261105T191557Z` | 42 / 0 / 0 | 5 min 27 s | pass (a first try with the native launch failed 38 cases: they need the import launch; not the image) |
| 2 | Listen-load W8 (`test_web_load.py`, `web.toml:totp-required.toml`, `--profile web,webload`) | `20261105T192130Z` | 5 / 0 / 0 | 12 min | pass, table below |
| 3a | Web API + smoke (`web.toml:admin.toml:totp-required.toml`) | `20261105T193434Z` | 375 / 0 / 0 | 5 min 07 s | pass |
| 3b | TOTP1 optional (`web.toml:admin.toml`) | `20261105T193949Z` | 14 / 0 / 0 | 2 min 36 s | pass |
| 3c | Browser UI (`test_web_browser.py`, chromium, firefox, WebKit) first run | `20261105T194233Z` | 48 / 6 / 3 | 10 min 27 s | 2 cases x 3 engines failed on intended front-end changes (WEBEV1/2 events columns Mode and Edge; WEBMSG1 message panel only when the room has messages); cases updated |
| 3d | Browser UI rerun with the updated cases | `20261105T204648Z` | 56 / 1 / 3 | 11 min 30 s | the 2 updated cases pass; the new case `test_signed_in_dashboard_offers_send_message` failed on chromium (a race of the case: it judged the panel before the signed-in history loaded); fixed in `2bc9cbf`, rerun listed under "Open" |
| 3e | WebRTC + recorder (Listen, browser PTT talk, recorder; `web.toml:admin.toml:totp-required.toml:recorder.toml`, `--profile web,browser`) | `20261105T195308Z` | 55 / 0 / 0 | 12 min 58 s | pass |
| 3f | K22 / K22b / EDG1 (`web.toml:guest.toml:totp-required.toml`) | `20261105T200615Z` | 13 / 0 / 0 | 23 min 41 s | pass (known finding "offer after hub restart": two refusals "service is recovering", circuit on the third try at 3.5 s) |
| 3g | NAT short with TURN (`DVT_NAT_SHORT=1`, `web.toml:turn.toml:totp-required.toml`, `--profile web,browser,nat`) | `20261105T203004Z` | 17 / 0 / 31 | 16 min 05 s | pass: classification x3, TURN relay through symmetric NAT, ICE blackhole and resume (the reconnect case) for 3 NAT types x 3 browsers, TURN credential expiry, `/ice` 429 goes direct x3; skips are the full-profile and 404-fallback cases |
| 4 | Host-name peer rows, XLX (`xlx-peer-host`, `xlx-peer-host-bad`, literal `xlx-peer`, each with the XLX997 row; `test_xlxd_interlink.py`) | `20261105T205827Z`, `T210419Z`, `T211010Z` | 13 / 0 / 1 each | 5 min 43 s each | pass (the skip is `test_interlink_cross_family_transit`, needs `--profile interlink,nxdn`) |
| 4 | Host-name peer rows, NXDN (`nxdn-peer-host`, `-bad`; `test_edge_relink.py -k nxdn`) | `20261105T211601Z`, `T211736Z` | 2 / 0 each | 1 min 28 s, 1 min 48 s | pass |
| 4 | Host-name peer rows, P25 (`p25-peer-host`, `-bad`; `-k p25`) | `20261105T211930Z`, `T212049Z` | 2 / 0 each | 1 min 11 s, 1 min 32 s | pass |
| 5a | SOAK1 standard (600 s warmup + 3000 s measured; `web.toml:totp-required.toml:soak-edges.toml:admin.toml`, `--profile web,webload`) | `20261105T212227Z` | 1 / 0 / 0 | 69 min 22 s | pass, 48 children, no restart, table below |
| 5b | Long soak (600 s warmup + 5100 s measured, `DVT_SOAK_WALL_CAP_S=6600`) | see "Long soak" | | | |
| E | D-STAR echo integrity E9 across images (a request of DV-Router) | see "Echo" | | | defect B (seq byte) on every image, defect A not reproduced |

Load average at the start of the runs was 0.4 to 4.8 (1 minute); no timing-sensitive case failed.

## Listen-load gate (W8: 200 WebRTC listeners x 50 pps through the HTTP relay)

| Measure | gate | wip-2040fce | wip-239382b |
|---------|------|-------------|-------------|
| edge cpu, percent of one core | 25 | 10.75 | 11.00 |
| edge peak RSS (VmHWM), MiB | 64 | 32.12 | 32.54 |
| edge RSS growth over startup, KiB | 24096 | 13680 | 14028 |
| worst-listener jitter p99, ms | 20 | 1.39 | 1.53 |
| worst-listener jitter max, ms | 60 | 2.64 | 10.20 |
| lost / duplicate / reordered / invalid / source missed deadlines | 0 | 0 | 0 |
| join of all 200 listeners, s (paced by the rate-limit budget) | | 208.1 | 208.2 |
| calibration cpu at 1 / 10 / 50 listeners, % | | 0.90 / 1.56 / 3.49 | 1.08 / 1.58 / 3.61 |
| flood probe p99 / max, ms | | 0.87 / 7.3 | 0.86 / 2.3 |

The only worse number is the maximum of the 3 s flood probe (7.3 ms against 2.3 ms; p99 equal, 0 lost): a single outlier, recorded.

## SOAK1 standard

Judge: `dv-svc-status` by the ring rule (BOUND + PLATEAU, `docs/web-soak-cases.md` row 2b), every other child by the whole-window slope.

| Child | RSS slope KiB/min (wip-239382b) |
|-------|---------------------------------|
| `dv-router` | 0.33 (0.62) |
| `dv-edge-webrtc` | 21.4 (24.1) |
| every `dv-room.*` (26) | -0.80 to -0.39 (0.05 to 0.28) |
| `dv-edge-dextra` | 0.29 |
| all other children | -0.12 to 0.004 |
| `dv-svc-status` | 117.8 (119.5): ring rule, "ring not full: bound only": 9139 events counted, growth 5890 KiB <= bound 19010 KiB |

Threads and fds are flat for every child (`dv-router`: 8 threads, 124 fds at start and end; the WebRTC edge 9 and 27); VmHWM `dv-router`
19.4 MiB, WebRTC edge 26.4 MiB. The new bounded structures of WIRE2 (diagnostics buffer, outbox) show no growth in 50 minutes.

## Echo (request of DV-Router): case E9, D-STAR echo return integrity

`cases/test_echo_dstar_integrity.py`, `docs/echo-cases.md` (E9). 10 overs of 330 frames (6.6 s) per image unless noted, a DExtra sender and a
DExtra listener on module E, each of the three streams (forward copy, echo to the listener, echo to the sender) judged by payload
equality, arrival gap <= 25 ms, seq == index mod 21 with 0x40 on the last frame, sync pattern 55 2d 16 only at index mod 21 == 0.

| Image | forward copy: lost / wrong seq | echo return: echoes with a lost frame | echo return: frames with a wrong seq | gaps > 25 ms |
|-------|-------------------------------|---------------------------------------|---------------------------------------|--------------|
| wip-2040fce | 0 / 0 % | 0 of 10 | 94.7 % | 0 |
| wip-239382b | 0 / 0 % | 0 | 95.9 % | 0 |
| wip-3075fb3 | 0 / 0 % | 0 | 95.1 % | 0 |
| wip-c2465de | 0 / 0 % | 0 | 95.1 % | 0 |
| wip-a3f0351 | 0 / 0 % | 0 | 95.6 % | 0 |
| wip-c165c93 | 0 / 0 % | 0 | 94.2 % | 0 |
| wip-e1f21c4 | 0 / 0 % | 0 | 95.6 % | 0 |
| wip-0b90596 | 0 / 0 % | 0 | 95.9 % | 0 |
| wip-7efdd33 | 0 / 0 % | 0 | 95.2 % | 1 (26.5 ms) |
| wip-2040fce, 30 overs x 450 frames, exact pacing | 0 / 0 % | 0 of 30 | 95.0 % | 4 (max 26.8 ms) |
| wip-2040fce, 30 overs x 450 frames, sender jitter +-6 ms | 0 / 0 % (forward shows the sender's jitter, max 32 ms) | 0 of 30 | 95.3 % | 0 (max 24.1 ms) |
| wip-2040fce, 30 overs x 450 frames, jitter +-5 ms and a 20 ms stall on 2 % of the packets | 0 / 0 % (forward max gap 49 ms) | 0 of 30 | 95.5 % | 4 (max 26.6 ms) |

- Defect B (the seq byte of the echo stream) is on every image tested, back to wip-0b90596 (the oldest renderable): the seq values of the echo look
  random (run 1: indexes 0 to 4 carry 12, 7, 9, 9, 3), the end bit and the sync positions are right. It is not a WIRE2 effect and not new.
  The case asserts the seq and fails on all of them today. The echo header RPT1 is "N0CALL B" (forward copy "N0CALL E"), RPT2 equal.
- Defect A (one lost frame, 40 ms gap): not reproduced on the rig in 180 overs (90 single runs on 9 images plus 90 overs of the three variants on
  wip-2040fce; every over is judged at the listener and at the sender): 0 lost frames. The two single-frame losses seen on production are
  unexplained (a Python sleep-loop client over the Internet, a receiver polling every 10 ms); DV-Router recorded them as probably network, no
  code change; its new drop counters of the echo round will show whether the edge ever drops a frame.
- The echo re-paces the stream: with a jittery sender the forward copy keeps the sender's jitter and the echo does not.

## Notes

- Front-end changes seen by the browser cases and updated in the suite (not WIRE2): events table columns `Mode` and `Edge` (WEBEV1/2), the
  message panel exists only when the room has messages and a signed-in sender gets a compact Send message button (WEBMSG1).
- Not run on this image: T13 (S3 superset, about 2 h 10) by agreement; it is a wip-239382b item.
