<!-- SPDX-License-Identifier: MIT -->
# Web unit: optional TOTP (TOTP1), new coverage

**New coverage of TOTP1.** DV-Router owner decision 2026-11-03: two-factor authentication is optional per
account and not enforced by default. The keys `web.totp_required` and `admin.totp_required` switch the
mandatory policy for operators and admins on; the old keys `web.operator_totp` and `admin.totp` are ignored
with a config warning. The existing web cases (`docs/web-api-cases.md`, `cases/web_fixtures.py`) start from
"first login = enrol", so they run WITH `rig/config/dv-router/totp-required.toml`. The cases here are the other
side: the default policy, with that snippet absent.

## Run it

Needs the default policy: `DVT_SUT_CONFIG_EXTRA` is `web.toml:admin.toml` (run.sh adds both for
`--profile web`) and does NOT list `totp-required.toml`. Use a rig of its own: one `run.sh` invocation starts
a fresh state, so the first operator (N0CALL) and the admin `viewer` login have no secret yet.

```
XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native \
DVT_SUT_CONFIG=<template> \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_web_totp_optional.py
```

About 8 minutes (11 registrations paced by the register budget, and up to three authenticator codes per
account, each at most one 30 s step apart). With `totp-required.toml` in the list the first test fails ("first
operator login: totp 'enrol', wanted 'off'") and the others skip with that reason; with an account that is
not in its first state (an enrolled first operator from an earlier case in the same run) the fixtures skip
with the reason.

## Accounts

Every flow runs on a throw-away account, invited by the run's bootstrap operator, which this file only logs
in with its password and never enrols (`dvtest/totp_optional.py`, `ACCOUNTS`): users N0TOA, N0TOB, N0TOC,
N0TOD, N0TOF; operators N0TOG, N0TOH, N0TOI, N0TOJ, N0TOK for the same cases a, b, c, d, f; operator N0TOE
for case e. The admin service has no API to create accounts, so its cases use the run's `viewer` login
(`out/<run>/web/admin.env`) and leave it without a secret again (the lifecycle ends with a disable, also in a
`finally` when a step fails).

## Cases

Sources (DV-Router, read only): `docs/api/openapi-web.yaml` (webLogin, webSession, webTotpOffer,
webTotpConfirm, webTotpDisable, webStepUp, webIdCommand), `docs/api/openapi-admin.yaml` (adminLogin,
adminTotpOffer/Confirm/Disable, adminStepUp, adminClients), the TOTP1 review note.

| Case | What it asserts | Surface |
|------|-----------------|---------|
| `test_first_operator_login_is_optional` | the run's first operator logs in with the password only: totp `off`, `totp_required` false, `step_up` false, `GET /operator/invites` 200 (a full session) | web, N0CALL (read only) |
| (a) `test_a_password_only_login` | an account without a secret: totp `off`, `totp_required` false, `step_up` false, cookie and CSRF token set, `GET /user/me` 200 (operator also `/operator/invites`); a wrong password and an unknown name both get the same 401 `unauthorized` | user, operator |
| (b) `test_b_enable_needs_password_step_up` | the offer without a window: 403 `step_up_required`; password `step-up` 200 (`totp` off, `step_up_s` 1..300, identity `step_up` true); offer 200 (secret, `otpauth://totp/` uri, 6 digits, 30 s, `Cache-Control: no-store`); a wrong first code is 403 `totp_invalid`; the right first code does (`totp` ok) | user, operator |
| (c) `test_c_enrolled_login_needs_code` | after enabling: password only gets 401 `totp_required`, no cookie; a wrong code 401 `unauthorized`; password and the current code 200, totp `ok`; the password cannot open a step-up window for an enrolled account (403 `totp_invalid`) | user, operator |
| (d) `test_d_disable_needs_password_and_code` | with the window open, the right password and a wrong code is refused (403 `totp_invalid`) and the account stays enrolled; password and the current code 200 (`totp` off); then a password-only login works again | user, operator |
| (e) `test_e_operator_sensitive_action_needs_password_step_up` | `POST /operator/ids/cmd` (list) with nothing is refused with 403 `step_up_required`; a made-up `X-DV-TOTP` is refused for an account without a secret; an empty step-up request opens nothing; the password step-up opens the window and the command passes the guard | operator |
| (f) `test_f_wrong_password_keeps_session` | a wrong password at the step-up (enable) and at disable (with a valid code) is refused (web: 401 `unauthorized`, TOTP1b pending; admin: 403 `totp_invalid`) and the session still answers 200 with the same identity and state; the right password still works afterwards | user, operator |
| `test_admin_default_is_optional`, `test_admin_lifecycle` | the same a to f on the admin `viewer`: password-only session, `GET /clients` (step-up guarded) and the offer refused without a window, wrong password refused without ending the session, password step-up (`ttl_s` 1..300), offer, first code, login needs the code, wrong password / wrong code on disable refused, password and code disable, password-only login again | admin viewer |

TOTP1 superseded API4's unconditional 400 for `POST /auth/session/totp` as a `user` (dv-router ruling 2026-11-04, F2):
without a password step-up window it is 403 `step_up_required`, before the body is read. `cases/test_web_api.py`
(`test_prefix_minimum_role[POST auth/session/totp | user]`, `test_user_totp_offer_needs_step_up`) asserts it with
and without `totp-required.toml`.

Exact answers ruled by dv-router on 2026-11-04 (the first run, `out/20261103T203002Z`, 14 passed, had recorded them as
FINDINGs; they are now assertions on (status, problem code), constants in `dvtest/totp_optional.py`):

| Situation | Answer |
|-----------|--------|
| wrong first code at confirm (web user and operator, admin) | 403 `totp_invalid` |
| an enrolled account steps up with its password only (it must give a code) | 403 `totp_invalid` |
| disable with the right password and a wrong code (web, admin) | 403 `totp_invalid` |
| any sensitive action without a window: web ids command, admin `GET /clients`, admin TOTP offer | 403 `step_up_required` |
| ADMIN wrong password at step-up and at disable (admin.md: the password step-up keeps `totp_invalid`) | 403 `totp_invalid` |
| WEB wrong password at step-up and at disable | **401 `unauthorized` today, TOTP1b pending** (below) |

**TOTP1b pending.** The web side answers a wrong password at the step-up and at disable with 401 `unauthorized` where admin
answers 403 `totp_invalid`; dv-router calls this an inconsistency and will fix it. `test_f_wrong_password_keeps_session`
asserts the CURRENT 401 through `dvtest.totp_optional.WEB_WRONG_PASSWORD` and writes a "TOTP1b pending" note line (a
FINDING line in `web-findings.txt`). When the fix lands the case fails with 403 `totp_invalid` until that one constant is set to
`TOTP_INVALID` (`cases/unit/test_totp_optional.py::test_web_wrong_password_is_totp1b_pending` documents the current value and
must be changed with it). In every case the session survives a wrong password.

Not ruled, so only "refused, not 200": a made-up `X-DV-TOTP` code on an account without a secret, and an empty step-up request.
The 401 vs 403 answer for the same cases is also not asserted for `POST /auth/session/totp` after a step-up: the route takes no body,
see `docs/web-api-cases.md`.

## Findings

Only the "TOTP1b pending" note lines (two web call sites, user and operator: four lines) and the real deviations below are recorded:
"password login after an unconfirmed offer ... says totp ..." (an offer alone must not enable 2FA) and "admin enrolled login
without a code ... documented totp_required".

## Unit tests

`cases/unit/test_totp_optional.py`: the account table, surface paths, answer parsing, rotation adoption and
the enrol / disable steps against a fake client (no rig): `python3 -m pytest -q -m unit cases/unit/test_totp_optional.py`.
