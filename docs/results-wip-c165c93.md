<!-- SPDX-License-Identifier: MIT -->
# Release-candidate regression of dv-router:wip-c165c93

Image `dv-router:wip-c165c93` (local build, the deploy candidate). The predecessor run on `dv-router:wip-71ab207` and its rulings F1-F8 are in
`docs/results-wip-71ab207.md`; the AllStar part is in `docs/asl-edge-results.md`. The repo defaults are the c165c93 dialect
(`DVT_SUT_GEN=c165c93`, `DVT_TOTP1B` on; `docs/rig.md`, "Image generations"). Native launch (import where the row says so),
`DVT_USRP_DYNAMIC=1`, no `--capture`, one rig run at a time, run from a separate worktree. The disk is checked with `touch` and the loadavg
(1 / 5 / 15 minute) recorded at the start and the end of every run (unallocated 1.00 GiB on the root filesystem throughout; owner-only).
A timing-sensitive failure at load above 6 is rerun once at load below 6 and reported as a load flake when the rerun passes.

## Summary

Duration is the pytest time of the run. Repo head is the dv-testset commit the run started from (`2721dec` for the runs of the driver; the
`rc-step.sh` records it at the start of every step).

| # | Run | Repo head | Out dir | Passed / failed / skipped | Duration | Load start -> end | Verdict |
|---|-----|-----------|---------|---------------------------|----------|-------------------|---------|
| 1 | Web stage 1+2 (`test_web_smoke.py`, `test_web_api.py`; `web.toml:admin.toml:totp-required.toml`) | `f2287e1` | `20261103T234245Z` | 375 / 0 / 0 | 5 min 26 s | 0.30 -> 0.79 | pass |
| 2 | EDG1 / K22 / K22b, full file (`test_web_k22.py`; `web.toml:guest.toml:totp-required.toml`; includes the 4 EDG1 cases) | `f2287e1` | `20261103T234827Z` | 13 / 0 / 0 | 23 min 31 s | 0.83 -> 1.33 | pass (`test_guest_lease_without_a_peer` passes) |
| 3a | Web stage 3a browser UI (`test_web_browser.py`; `web.toml:admin.toml:totp-required.toml`), first run | `2721dec` | `20261104T003849Z` | 51 / 3 / 3 | 10 min 00 s | 0.47 -> 1.02 | 3 failures: `test_operator_login_with_totp_step` on chromium, firefox, Playwright WebKit (finding F9, a test bug); the 3 skips are `test_xss_last_heard_callsign` (the router stores no callsign with markup characters), as before |
| 3b | Rerun of `test_operator_login_with_totp_step` after the case fix 0af7aa6 (`-k test_operator_login_with_totp_step`, same extra) | lab worktree with `0af7aa6` | `20261104T012439Z` (in the lab worktree's `out/`) | 3 / 0 / 0 | 1 min 44 s | not recorded | pass; stage 3a therefore 54 / 0 / 3 (the same as d449742) |
| 4 | Web stage 3b WebRTC + recorder (`test_web_webrtc.py`, `test_web_recorder.py`; `web.toml:admin.toml:totp-required.toml:recorder.toml`) | `2721dec` | `20261104T012633Z` | 55 / 0 / 0 | 13 min 00 s | 2.34 -> 0.94 | pass |
| 5a | TOTP1 optional default (`test_web_totp_optional.py`; `web.toml:admin.toml`, WITHOUT `totp-required.toml`) | `2721dec` | `20261104T013942Z` | 14 / 0 / 0 | 2 min 42 s | 0.94 -> 0.85 | pass |
| 5b | `test_web_api.py -k totp` (`web.toml:admin.toml:totp-required.toml`) | `2721dec` | `20261104T014233Z` | 20 / 0 / 0 (338 deselected) | 28 s | 0.85 -> 0.82 | pass |
| 6a | Data ident `DVT_REQUIRE_IDENT=true` (`data-ident.toml:xlx-peer.toml`, `--profile interlink`) | `2721dec` | `20261104T014309Z` | 8 / 0 / 0 | 4 min 59 s | 0.82 -> 0.67 | pass |
| 6b | Data ident `DVT_REQUIRE_IDENT=false` | `2721dec` | `20261104T014816Z` | 8 / 0 / 0 | 4 min 37 s | 0.67 -> 0.74 | pass |
| 7 | M17 (`test_m17_peer_link.py`, `test_m17_relay.py`, `test_m17_packet.py`, native, profile core, template only) | `2721dec` | `20261104T015301Z` | 97 / 0 / 0 | 10 min 12 s | 0.74 -> 0.39 | pass (same count as Gate A / B and 71ab207) |
| 8 | T14 cross-mode (`DVT_TRANSCODER_FAKE=1`, import launch, `-m crossmode`) | `2721dec` | `20261104T020319Z` | 53 / 0 / 0 | 12 min 58 s | 0.39 -> 0.41 | pass (same as 71ab207) |
| 9 | P19 edge relink (import launch, `--profile interlink,nxdn,p25`) | `2721dec` | `20261104T021624Z` | 6 / 0 / 0 | 3 min 24 s | 0.41 -> 0.79 | pass (same as 71ab207) |
| 10 | Stage 5 W8 load (`test_web_load.py`; `web.toml:totp-required.toml`, `--profile web,webload`) | `2721dec` | `20261104T021959Z` | 5 / 0 / 0 | 11 min 59 s | 0.79 -> 1.35 | pass: 200 listeners x 50 pps gate met, worst listener jitter p99 1.57 ms (gate 20 ms, reference c90 4.6 ms), source p99 0.04 ms, see the notes |
| 11 | Short NAT (`DVT_NAT_SHORT=1`, `web.toml:turn.toml:totp-required.toml`, `--profile web,browser,nat`) | `2721dec` | `20261104T023207Z` | 17 / 0 / 31 | 16 min 03 s | 1.35 -> 3.21 | pass (same as be207d3 and 71ab207: 17 / 0 / 31) |
| A | AllStar edge (30 cases of `cases/test_asl_edge.py` plus DVSwitch Mobile, phone and radio) | see the document | see the document | 30 of 30 accounted: pass, the long and stand-in variants included; R1-R7 clean run 10 passed in 104 s on a fresh rig (`20261104T001310Z`) | n/a | n/a | pass, `docs/asl-edge-results.md` (c165c93 column; the first R1 failure was a lab artifact, F-R1) |
| S1 | SOAK1 GA (`DVT_SOAK=1 DVT_SOAK_GA=1`, `web.toml:totp-required.toml:soak-edges.toml`, `--profile web,webload`, `cases/test_web_soak.py`) | `2721dec` | `20261104T031836Z` | 0 / 1 / 0 | 59 min 20 s | 1.23 -> 3.53 | judge verdict FAIL (`rss:dv-svc-status`) recorded as is; ruled a known bounded LEAK1 item, see the notes. Everything else passes |
| S2 | S3 superset, 2 hours | | | | | | pending |
| E | ECHO1 / ECHO1c (`cases/test_echo.py`; `echo.toml:echo-peers.toml:xlx-peer.toml`, `--profile interlink`) | `8b8afea` + case fixes | `20261104T051933Z` (in the echo worktree's `out/`) | 28 / 2 / 2 | 6 min 00 s | 3.3 at the end | 2 failures, both the SUT's: E5b USRP over replayed, E1c YSF replay not the sent frames (FE1, FE2); 2 skips: NXDN / P25 and two-node (not reachable) |

## Per-step notes

### 1, 2

Step 1 ran with `totp-required.toml` (the enrol-first fixtures; `docs/rig.md`): 375 passed (smoke + API). Step 2 is the full K22 file with `guest.toml`;
the four EDG1 cases are part of it, so no separate EDG1 run was made. K22 passed 13 of 13 including `test_guest_lease_without_a_peer` (ruling F3 of
`docs/results-wip-71ab207.md`: 204 then 401 and/or 403 ending in 403).

### 3: browser UI

First run 51 / 3 / 3. All three failures are the same case on the three engines, `test_operator_login_with_totp_step`: the TOTP1b check that the login
page clears a stale wrong-code banner asserted `not_to_contain_text` on `#auth-error`; the page removes the element (`element(s) not found`,
`Actual value: None`), which that assertion does not accept. The product behaviour is the intended one (banner gone); the assertion was too narrow. Case
fix `0af7aa6` (no element may carry the wrong-code text, a removed `#auth-error` passes), ruled by the main session, not edited by this agent. Rerun of
the case alone: 3 / 0 / 0. The browser labels updated for I18N1 (F5-F7 of the earlier run) all pass. Screenshots and traces stay local in `out/`.

### 4: WebRTC + recorder

55 passed, no failure (a WebKit tone glitch or a Playwright setup error, both known from `docs/results-d449742.md`, did not occur).
The recordings page end-reason label (F8) passes.

### 5: TOTP1

5a (default policy, optional TOTP) 14 / 0 / 0. 5b (`-k totp` with `totp-required.toml`) 20 / 0 / 0, with `DVT_TOTP1B` on (the default).

### 8, 9: import launch

T14 (fake transcoder) 53 / 0 / 0 and P19 6 / 0 / 0 ran with `DVT_SUT_LAUNCH=import`, as in the earlier runs.

### 10: W8 load, jitter

`out/20261104T021959Z/web/load.json`: the 200-listener x 50 pps gate (`w8_200`) is met; worst per-listener jitter p99 1.566 ms against the gate of 20 ms
(the reference of c90 is 4.6 ms; be207d3 measured 1.54 ms), source p99 0.043 ms, event-loop lag p99 about 1.04 ms. The single-listener calibration run
shows the largest p99 (9.95 ms, one listener, gate met); with 10 and 50 listeners it is 0.95 and 1.09 ms. The case gates pass in all three; the 9.95 ms is listed so that it is
not mistaken for a regression (not analysed further here).

### 11: short NAT

17 passed, 31 skipped, 0 failed, in 16 min 03 s. The skips are 22 cases "not part of the short profile" and 9 `404 fallback` cases (TURN is configured; they need a run without `turn.toml`).

### S1: SOAK1 GA

Judge verdict FAIL on one rule, `rss:dv-svc-status`: slope 118.3 KiB/min x 2400 s = 4730 KiB against the limit of 4096 KiB (start 14528 KiB, Anon the same
slope). Ruling (dv-router, 2026-11-04): a known LEAK1 item, bounded. The hub event ring is a compile-time constant of 10 000 events
(`crates/svc/status/src/hub.rs:44`); the measured part saw 5930 events (148.2 per minute, estimate 172 KiB/min at 1.16 KiB per event, ratio 1.45) and
7640 at the end, so the ring was still filling. A definitive plateau needs a soak past ring saturation (about 68 minutes after the settle, a 90 minute
window), required only for the GA verdict. The rule is NOT tuned.

Everything else passes: no restart of any process, threads and fds flat (dv-router 8 threads, 124 fds), the 11 flat counters stay 0, the drops are
legitimate, 400 of 400 overs, the idle check is clean after a 171 s settle. dv-router RSS passes with 97.6 KiB/min x 2400 s = 3904 KiB (95 percent of
the limit; 145.8 KiB/min on be207d3), Anon 10340 -> 14200 KiB and still growing, not a plateau. dv-room x26 19.4 to 20.7 KiB/min, dv-edge-webrtc
30.6 KiB/min. Raw samples and the verdict table stay local (`out/20261104T031836Z/web/soak-samples.jsonl`, `soak.json`).

## Findings

New on wip-c165c93:

| ID | Area | Observed on wip-c165c93 | Before | Status |
|----|------|--------------------------|--------|--------|
| F9 | TOTP1b login banner case | `test_operator_login_with_totp_step` (3 engines): `#auth-error` no longer exists after the page clears the stale banner, `expect(...).not_to_contain_text(...)` on a missing element fails (`out/20261104T003849Z/pytest.log`) | the banner element stayed, empty | closed: test bug, product behaviour intended; case fixed in `0af7aa6` (ruled by the main session); rerun 3 / 0 / 0 (`20261104T012439Z`) |
| F-R1 | AllStar R1 cadence | the first run of the R1-R7 selection failed on the refresh cadence (`20261103T232417Z`) | pass | closed: lab artifact (dv-router ruling 2026-11-04, details in `docs/asl-edge-results.md`); R1-R7 judge the last edge process only; clean run `20261104T001310Z` 10 passed (`docs/asl-edge-results.md`) |

| FE1 | ECHO1 origin class | E5b: a USRP over from a dynamic-hub client on module E is replayed 1.0 s after its end (150 packets, a 1.0 s hole, 150 more; `20261104T051933Z/echo.txt`) | brief: USRP (class 2) is not replayed | open: ruling needed (are dynamic-hub clients class 0?) |
| FE2 | ECHO1 YSF replay | E1c YSF: the replay has 155 frames and none of the 150 sent; the live relay is byte-identical to the sent frames; D-STAR and DMR replays are identical | spec: replay byte-identical | open: reported to dv-router |

Carried over from `docs/results-wip-71ab207.md` and all closed there by ruling (nothing reopened on c165c93): F1 TOTP1 default is optional (steps
use `totp-required.toml` except 5a), F2 user TOTP offer needs a step-up (403 `step_up_required`), F3 K22 lease end is a test race (204 then 401 and/or
403 ending in 403), F4 the offer route ignores its body (200), F5 / F6 / F7 I18N1 text changes in the admin and ops pages and the login challenge,
F8 the recordings page label "Transmission ended". The cases carry the rulings; no assertion was edited by this agent.

### E

Cases, snippets and the per-case table: `docs/echo-cases.md`. First run 21 / 9 / 2 (`20261104T041903Z`): DMR join streams counted as overs, the M17 expectation
(the lead-in pair of packet 0 is not audio) and the telemetry counters restarting at the E7b respawn were case-side and fixed; the rerun
`20261104T043055Z` (`-k "dmr or m17 or ysf or e5b"`) 16 / 2 / 0; the full run above is the verdict. Delay 1.00 to 1.01 s on all four protocols, truncation
at max_seconds 5 gives 250 frames (D-STAR) and 249 frames (DMR, 83 bundles), the floor is busy during the replay, the TOML toggle works within 4 s,
removing the last row while a replay plays cuts it with an end of stream and the module is free 2 s later.

## Load flakes

None. The highest load at the end of any step was 3.21 (step 11); no failure occurred at a load above 6.

## Commands

```sh
export XLXD_SUT_IMAGE=dv-router:wip-c165c93 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
C=rig/config/dv-router
W=$C/web.toml A=$C/admin.toml T=$C/totp-required.toml G=$C/guest.toml R=$C/recorder.toml
# 1
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web -- cases/test_web_smoke.py cases/test_web_api.py
# 2
DVT_SUT_CONFIG_EXTRA=$W:$G:$T scripts/run.sh -m rig --profile web,browser -- cases/test_web_k22.py
# 3a
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web,browser -- cases/test_web_browser.py
# 3b (rerun after 0af7aa6)
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web,browser -- cases/test_web_browser.py -k test_operator_login_with_totp_step
# 4
DVT_SUT_CONFIG_EXTRA=$W:$A:$T:$R scripts/run.sh -m rig --profile web,browser -- cases/test_web_webrtc.py cases/test_web_recorder.py
# 5a (no totp-required.toml), 5b
DVT_SUT_CONFIG_EXTRA=$W:$A scripts/run.sh -m rig --profile web -- cases/test_web_totp_optional.py
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web -- cases/test_web_api.py -k totp
# 6a / 6b
DVT_SUT_CONFIG_EXTRA=$C/data-ident.toml:$C/xlx-peer.toml DVT_REQUIRE_IDENT=true|false \
  scripts/run.sh -m rig --profile interlink -- cases/test_data_ident.py
# 7
scripts/run.sh -m rig --profile core -- cases/proto/test_m17_peer_link.py cases/proto/test_m17_relay.py cases/proto/test_m17_packet.py
# 8
DVT_TRANSCODER_FAKE=1 DVT_SUT_LAUNCH=import scripts/run.sh -m crossmode --profile core -- cases/test_crossmode_exact.py cases/test_crossmode_failure.py
# 9
DVT_SUT_LAUNCH=import scripts/run.sh -m rig --profile interlink,nxdn,p25 -- cases/test_edge_relink.py
# 10
DVT_SUT_CONFIG_EXTRA=$W:$T scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py
# S1
DVT_SOAK=1 DVT_SOAK_GA=1 DVT_SUT_CONFIG_EXTRA=$W:$T:$C/soak-edges.toml scripts/run.sh -m rig --profile web,webload -- cases/test_web_soak.py
# 11
DVT_NAT_SHORT=1 DVT_SUT_CONFIG_EXTRA=$W:$C/turn.toml:$T scripts/run.sh -m rig --profile web,browser,nat -- cases/test_web_nat.py
# E
DVT_SUT_CONFIG_EXTRA=$C/echo.toml:$C/echo-peers.toml:$C/xlx-peer.toml scripts/run.sh -m rig --profile interlink -- cases/test_echo.py
```
