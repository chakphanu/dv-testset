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

## Per-step notes

### 1: web stage 1+2

The rig snippets `web.toml` (`operator_totp = true`) and `admin.toml` (`totp = true`) use keys that TOTP1 retired: the web service prints
`DV_WEB_OPERATOR_TOTP is ignored; use DV_WEB_TOTP_REQUIRED` and the image has the keys `web.totp_required` / `admin.totp_required`. With the old
keys TOTP is optional: the first operator login answers totp state `off` (the fixtures want `enrol`: 213 setup errors), an admin account without
a TOTP code logs in with HTTP 200 (documented before: 401 `totp_required`), and the admin session shows totp `off` after the fixture's enrolment.
A new snippet `rig/config/dv-router/totp-required.toml` (`[web] totp_required = true`, `[admin] totp_required = true`; documented in `docs/rig.md`,
unit test in `cases/unit/test_sut_config.py`) restores the enrol-first behaviour; no assertion was edited. With it: 373 / 1 / 0.

## Findings

| ID | Area | Observed on wip-71ab207 | Before | Status |
|----|------|--------------------------|--------|--------|
| F1 | TOTP1 default | With `operator_totp = true` / `admin.totp = true` only: first operator login `totp: off` (no enrolment step), admin password-only login 200, admin session `totp: off` | enrol-first, password-only login 401 `totp_required` | needs dv-router ruling (forwarded by the main session); rig now sets `totp_required = true`; no case asserts the optional default |
| F2 | TOTP1 step-up | `POST auth/session/totp` as a `user` account answers 403 `step_up_required` (case `test_prefix_minimum_role[POST auth/session/totp \| user]`, `out/20261103T193312Z`) | 400 (API4 documented) | ruled 2026-11-04 (dv-router): TOTP1 supersedes API4's 400; the matrix assertion is now 403 `step_up_required` and `test_user_totp_offer_needs_step_up` covers the step-up path (200 offer, 400 for a malformed body after it) |

## Load flakes

None so far.

## Commands

```sh
export XLXD_SUT_IMAGE=dv-router:wip-71ab207 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
W=rig/config/dv-router/web.toml A=rig/config/dv-router/admin.toml T=rig/config/dv-router/totp-required.toml
DVT_SUT_CONFIG_EXTRA=$W:$A:$T scripts/run.sh -m rig --profile web -- cases/test_web_smoke.py cases/test_web_api.py
```
