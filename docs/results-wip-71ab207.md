<!-- SPDX-License-Identifier: MIT -->
# Release-candidate regression of dv-router:wip-71ab207

Image content: ACL0a/ACL0b, TOTP1 (optional TOTP), I18N1/I18N1b, IDS1, ECHO1c, ASL. The AllStar part (25 cases + DVSwitch Mobile) is in
`docs/asl-edge-results.md` and is not repeated here. Native launch, `DVT_USRP_DYNAMIC=1`, no `--capture` for the heavy runs, one rig run at a
time, loadavg and the disk checked before every run (unallocated 1.00 GiB on the root filesystem throughout; owner-only). Every run's load
is the 1 / 5 / 15 minute average at start and end.

## Summary

| # | Run | Out dir | Passed / failed / skipped | Duration | Load start -> end | Verdict |
|---|-----|---------|---------------------------|----------|-------------------|---------|
| 1a | Web stage 1+2, rig config as before (`web.toml:admin.toml`) | `20261103T193125Z` | 130 / 31 / 0 (+213 errors) | 1 min 12 s | 2.55 -> 3.13 | FAIL: TOTP1 default (finding F1) |
| 1b | Web stage 1+2 with `totp-required.toml` | `20261103T193312Z` | 373 / 1 / 0 | 5 min 06 s | 2.87 -> 3.05 | 1 failure: finding F2 |
| 1c | Optional default, login/TOTP cases only (evidence run) | `20261103T193838Z` | 0 / 2 / 0 (+6 errors) | 16 s | 2.68 -> 2.73 | evidence, no verdict (F1) |
| 1d | Optional default (no `totp-required.toml`), `cases/test_web_totp_optional.py` | `20261103T203002Z` | 14 / 0 / 0 | 2 min 30 s | 4.17 -> 3.21 | pass (TOTP1 FINDING lines are open answers, `web/web-findings.txt`) |
| 1e | F2 rerun, `test_web_api.py -k totp` with `totp-required.toml` | `20261103T203247Z` | 19 / 1 / 0 | 35 s | 3.59 -> 3.16 | F2 case now passes; 1 failure: finding F4 |
| 2a | EDG1 / K1 (`test_web_k22.py -k "silently_closed or closing_only_the_ctl"`), first try | `20261103T194114Z` | not run | 6 s | 4.15 -> 4.29 | infra error, rerun (docker build `failed to prepare extraction snapshot ... parent snapshot ... does not exist` on the web-proxy image; nothing of ours tested) |
| 2b | EDG1 / K1 rerun | `20261103T202532Z` | 4 / 0 / 0 | 4 min 15 s | 2.09 -> 4.71 | pass |
| 2c | K22 / K22b, full file | `20261103T194120Z` | 12 / 1 / 0 | 24 min 47 s | 4.29 -> 3.69 | 1 failure: finding F3 (needs ruling) |
| 3a | Web stage 3a browser UI (`test_web_browser.py`, `web.toml:admin.toml:totp-required.toml`) | `20261103T203332Z` | 45 / 9 / 3 | 11 min 04 s | 3.46 -> 6.14 | FAIL: 3 cases x 3 engines, findings F5, F6, F7 (deterministic, not load) |
| 3b | Web stage 3b WebRTC + recorder (`test_web_webrtc.py`, `test_web_recorder.py`, `recorder.toml`) | `20261103T204447Z` | 52 / 3 / 0 | 13 min 02 s | 5.43 -> 2.86 | FAIL: 1 case x 3 engines, finding F8; no WebKit tone glitch and no Playwright setup error this time |
| 5a | M17 (`test_m17_peer_link.py`, `test_m17_relay.py`, `test_m17_packet.py`, native, `--capture`) | `20261103T205833Z` | 97 / 0 / 0 | 10 min 27 s | 2.26 -> 1.56 | pass (same count as Gate B) |
| 5b | Data ident `DVT_REQUIRE_IDENT=true` (`--profile interlink`, `--capture`) | `20261103T210903Z` | 8 / 0 / 0 | 5 min 11 s | 1.56 -> 1.76 | pass |
| 5c | Data ident `DVT_REQUIRE_IDENT=false` | `20261103T211418Z` | 8 / 0 / 0 | 4 min 52 s | 1.94 -> 1.97 | pass |
| 5d | T14 cross-mode (fake transcoder, import launch, `--capture`) | `20261103T211918Z` | 53 / 0 / 0 | 13 min 05 s | 1.90 -> 1.52 | pass (same as the adaptation pass) |
| 5e | S3 superset, first try | `20261103T205803Z` | not run | 15 s | 5.4 -> 2.3 | infra error: pytest collection error, `cases/unit/test_echo.py` and uncommitted `cases/test_echo.py` share a basename (another session's echo work in the shared tree); to be rerun |
| 5f | P19 edge relink (import launch, `--profile interlink,nxdn,p25`, `--capture`) | `20261103T213234Z` | 6 / 0 / 0 | 3 min 43 s | 1.43 -> 1.89 | pass (same as the adaptation pass) |
| 6a | Stage 5 W8 load, first try | `20261103T213619Z` | not run | 6 s | 1.89 -> 2.21 | infra error: the same docker build snapshot error as 2a (`parent snapshot ... does not exist` on `dvt-web-proxy`); rerun queued as 6b |
| 7 | Short NAT (`DVT_NAT_SHORT=1`, `turn.toml`, `--profile web,browser,nat`) | `20261103T213634Z` | 17 / 0 / 31 | 16 min 20 s | 2.04 -> 0.86 | pass (same as be207d3: 17 / 0 / 31, 16 min 03 s) |
| 8a | SOAK1 GA, run 1 | `20261103T215304Z` | no verdict | killed at 2287 s elapsed (about 26.5 min of the 40 min measured part sampled) | 0.72 -> 2.09 | KILLED EXTERNALLY (SIGTERM of the `systemd-run` scope at 22:34:19 UTC; incident below); partial numbers only |
| 8b | SOAK1 GA, run 2 | `20261104T001943Z` | no verdict | killed at 483 s elapsed | n/a | KILLED EXTERNALLY at about 00:30:40 UTC (incident below) |
| 6b | Stage 5 W8 load, rerun (`load2`) | `20261103T223432Z` | no verdict | killed after about 80 s | 1.44 -> n/a | KILLED EXTERNALLY at 22:35:51 UTC (incident below) |
| 5e | S3 superset | n/a | not run | n/a | n/a | not rerun: the echo basename collision was fixed in the tree (`cases/unit/test_echo_lib.py`), but the rig was never free for the 2 h run; superseded by the c165c93 chain |

## Per-step notes

### 1: web stage 1+2

The rig snippets `web.toml` (`operator_totp = true`) and `admin.toml` (`totp = true`) use keys that TOTP1 retired: the web service prints
`DV_WEB_OPERATOR_TOTP is ignored; use DV_WEB_TOTP_REQUIRED` and the image has the keys `web.totp_required` / `admin.totp_required`. With the old
keys TOTP is optional: the first operator login answers totp state `off` (the fixtures want `enrol`: 213 setup errors), an admin account without
a TOTP code logs in with HTTP 200 (documented before: 401 `totp_required`), and the admin session shows totp `off` after the fixture's enrolment.
A new snippet `rig/config/dv-router/totp-required.toml` (`[web] totp_required = true`, `[admin] totp_required = true`; documented in `docs/rig.md`,
unit test in `cases/unit/test_sut_config.py`) restores the enrol-first behaviour; no assertion was edited. With it: 373 / 1 / 0.

### 2: EDG1 and K22

The first EDG1 attempt died in the image build before any test ran (containerd snapshot error on `dvt-web-proxy`; the next build, started by the
K22 run seconds later, worked). EDG1 rerun: 4 / 0 / 0, same cases as `docs/results-be207d3.md`. Full K22 file: 12 passed, 1 failed:
`test_guest_lease_without_a_peer`: after the peer is kicked `/rtc/alive` answers 204 for 29.4 s (30 answers) and then 403 (90 answers up to 119.8 s);
the case asserts 401 (be207d3: 204 for about 29 s, then 401). Assertion not edited; forwarded to dv-router (F3).

### 1d / 1e: TOTP1 cases (added by the main session's ruling)

1d ran WITHOUT `totp-required.toml` (default policy) and alone: 14 / 0 / 0. 1e: with F2 settled by ruling (user TOTP offer is 403 `step_up_required` without a
password step-up) `test_prefix_minimum_role[POST auth/session/totp | user]` and `test_user_totp_offer_needs_step_up`'s first four assertions pass; the last
assertion (a malformed body after the step-up must answer 400) fails: the route answers 200 (F4).

### 3a: browser UI

45 passed, 3 skipped (the same 3 as before), 9 failed: `test_admin_spa_loads`, `test_ops_pages_render` and `test_operator_login_with_totp_step`, each on
chromium, firefox and Playwright WebKit. Screenshots under `out/20261103T203332Z/web/browser/<engine>-<case>-<engine>/page.png` (local to `out/`).

### 3b: WebRTC + recorder

52 passed, 3 failed: `test_recordings_page_lists_and_plays` on all three engines (F8). Every Listen / Talk case passed on all three engines, including
the four WebKit talk cases and `test_listen_receives_the_tone[webkit]` (the known tone glitch did not appear), and there was no Playwright setup error.

### Echo (ECHO1c)

At the time of the run no case for ECHO1c existed (the ECHO1 / ECHO1c cases `cases/test_echo.py`, commit 8b8afea, arrived later and were NOT run on wip-71ab207): `grep -ri echo cases docs/testlist.md` finds only the protocol "no echo to the talker" / "poll echoed" cases (P-DPL-28, P-NXD-17,
`test_allstar::test_usrp_no_echo`, ...) and `test_m17::test_m17_parrot` (P-M17-42, record only). They are members of the S3 and M17 runs below; nothing was
added.

## Findings

| ID | Area | Observed on wip-71ab207 | Before | Status |
|----|------|--------------------------|--------|--------|
| F1 | TOTP1 default | With `operator_totp = true` / `admin.totp = true` only: first operator login `totp: off` (no enrolment step), admin password-only login 200, admin session `totp: off` | enrol-first, password-only login 401 `totp_required` | EXPECTED (TOTP1), ruled by dv-router: the old keys only warn `is ignored; use ..._totp_required`; `totp-required.toml` is the right setting for the mandatory-enrolment cases; the optional default is covered by `cases/test_web_totp_optional.py` (step 1d) |
| F2 | TOTP1 step-up | `POST auth/session/totp` as a `user` account answers 403 `step_up_required` (case `test_prefix_minimum_role[POST auth/session/totp \| user]`, `out/20261103T193312Z`) | 400 (API4 documented) | ruled 2026-11-04 (dv-router): TOTP1 supersedes API4's 400; the matrix assertion is now 403 `step_up_required` and `test_user_totp_offer_needs_step_up` covers the step-up path (200 offer, 400 for a malformed body after it). Results: before the update `out/20261103T193312Z` (1 failure), after the update `out/20261103T203247Z` (the matrix case passes; the new case fails on its last assertion, F4). F2 expected (TOTP1), case updated by ruling |
| F3 | K22 guest lease after the peer is gone | `/rtc/alive` 204 for 29.4 s then 403 `forbidden` (`out/20261103T194120Z`, `test_guest_lease_without_a_peer`) | 204 then 401 (be207d3) | needs ruling (forwarded to dv-router); assertion unchanged |
| F4 | TOTP1 user offer | after the password step-up a malformed offer body (raw `{`) answers 200, `test_user_totp_offer_needs_step_up` (`cases/test_web_api.py:695`) expects 400 `bad_request` | n/a (new case) | needs ruling (asl1-lab / dv-router); route seems to ignore its body; `out/20261103T203247Z` |
| F5 | I18N1 admin sign-in text | admin page shows "Admin sign in" / "Manage node configuration and operation." with an EN\|TH switch; `test_admin_spa_loads` waits for "Sign in to the admin interface" (3 engines) | the old text | needs ruling (text change expected under I18N1); assertion unchanged; `out/20261103T203332Z` |
| F6 | I18N1 ops events table | header cell 2 is "Time (local)"; `test_ops_pages_render` expects "Time (UTC)" (3 engines) | "Time (UTC)" | needs ruling; assertion unchanged; `out/20261103T203332Z` |
| F7 | login TOTP step | after a wrong code, refilling the password field drops the TOTP code field, `test_operator_login_with_totp_step` times out on `login-totp` (3 engines); the dedicated wrong-code text itself shows | refill kept the challenge or the case did not refill | needs ruling; assertion unchanged; `out/20261103T203332Z` |
| F8 | recordings page text | the Recordings page row says "Transmission ended" where `test_recordings_page_lists_and_plays` expects the end reason text "terminator" (3 engines) | "terminator" | needs ruling (I18N1 text change?); assertion unchanged; `out/20261103T204447Z` |

## Incidents: the rig was shared, runs were killed (not product results)

Project `dvt` has one compose project and one rig; at least two other sessions started runs of their own while this chain was running
(worktrees `dvt-wt-recordings`: `--profile asl-edge -m asl_lab` loops, and `dvt-wt-rc`: the K22 / WebRTC / recorder reruns and the c165c93 chain).
Each new `scripts/run.sh` start of another session coincided with a SIGTERM of this chain's run (`Terminated  systemd-run --user --scope ...`
at the end of the logs) and of the chain scripts themselves (22:34 UTC SOAK1 run 1; 22:35 UTC load2; 00:30 UTC SOAK1 run 2). The chain never
signalled anything and never ran `--down` (no hang deadline was reached). Consequence: SOAK1 GA, the Stage 5 rerun and S3 have NO verdict on
`wip-71ab207`; the c165c93 regression (`docs/results-wip-c165c93.md`) supersedes this image for them.

Partial LEAK1 numbers from the first SOAK1 run (measured phase only, 26.5 minutes, least-squares slope of RSS, `soak-samples.jsonl`; NOT a verdict,
the window is 2/3 of the gate and the ring of the status service had not filled):

| Child | RSS first KiB | RSS last KiB | slope KiB/min (RSS / Anon) | be207d3 (full 40 min) |
|-------|---------------|--------------|----------------------------|-----------------------|
| dv-router | 20720 | 22928 | 92.1 / 93.8 | 145.8 |
| dv-svc-status | 14944 | 17908 | 117.9 / 119.6 | 119.4 |
| dv-edge-webrtc | 25620 | 26328 | 32.1 / 33.4 | 29.5 |

## Load flakes

None. No failure of this chain was rerun as a load flake: every failure is deterministic (findings F3 to F8) and the loads were 0.7 to 6.1.

## Commands

```sh
export XLXD_SUT_IMAGE=dv-router:wip-71ab207 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
W=rig/config/dv-router/web.toml A=rig/config/dv-router/admin.toml T=rig/config/dv-router/totp-required.toml
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web -- cases/test_web_smoke.py cases/test_web_api.py
```
