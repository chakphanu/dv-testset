<!-- SPDX-License-Identifier: MIT -->
# Release-candidate regression of dv-router:wip-c165c93

Image `dv-router:wip-c165c93` (local build, the deploy candidate). The predecessor run on `dv-router:wip-71ab207` and its rulings F1-F8 are in
`docs/results-wip-71ab207.md`; the AllStar part is in `docs/asl-edge-results.md`. The repo defaults are the c165c93 dialect
(`DVT_SUT_GEN=c165c93`, `DVT_TOTP1B` on; `docs/rig.md`, "Image generations"). Native launch (import where the row says so),
`DVT_USRP_DYNAMIC=1`, no `--capture`, one rig run at a time, run from a separate worktree. The disk is checked with `touch` and the loadavg
(1 / 5 / 15 minute) recorded at the start and the end of every run (unallocated 1.00 GiB on the root filesystem throughout; owner-only).
A timing-sensitive failure at load above 6 is rerun once at load below 6 and reported as a load flake when the rerun passes.

## Summary

| # | Run | Repo head | Out dir | Passed / failed / skipped | Duration | Load start -> end | Verdict |
|---|-----|-----------|---------|---------------------------|----------|-------------------|---------|
| 1 | Web stage 1+2 (`test_web_smoke.py`, `test_web_api.py`) | | | | | | pending |
| 2 | EDG1 / K22 full file (`test_web_k22.py`, `guest.toml`) | | | | | | pending |
| 3 | Web stage 3a browser UI (`test_web_browser.py`) | | | | | | pending |
| 4 | Web stage 3b WebRTC + recorder (`test_web_webrtc.py`, `test_web_recorder.py`) | | | | | | pending |
| 5a | TOTP optional default (`test_web_totp_optional.py`, without `totp-required.toml`) | | | | | | pending |
| 5b | `test_web_api.py -k totp` with `totp-required.toml` | | | | | | pending |
| 6a | Data ident `DVT_REQUIRE_IDENT=true` | | | | | | pending |
| 6b | Data ident `DVT_REQUIRE_IDENT=false` | | | | | | pending |
| 7 | M17 (`test_m17_peer_link.py`, `test_m17_relay.py`, `test_m17_packet.py`, profile core) | | | | | | pending |
| 8 | T14 cross-mode (fake transcoder, import launch) | | | | | | pending |
| 9 | P19 edge relink (import launch, `--profile interlink,nxdn,p25`) | | | | | | pending |
| 10 | Stage 5 W8 load (`test_web_load.py`) | | | | | | pending |
| 11 | Short NAT (`DVT_NAT_SHORT=1`, `turn.toml`) | | | | | | pending |

SOAK1 GA (65 minutes) and S3 (2 hours) follow on the main session's go and are added below.

## Per-step notes

(filled in as the steps finish)

## Findings

| ID | Area | Observed on wip-c165c93 | Before | Status |
|----|------|--------------------------|--------|--------|

None so far.

## Load flakes

None so far.

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
# 3
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web,browser -- cases/test_web_browser.py
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
# 11
DVT_NAT_SHORT=1 DVT_SUT_CONFIG_EXTRA=$W:$C/turn.toml:$T scripts/run.sh -m rig --profile web,browser,nat -- cases/test_web_nat.py
```
