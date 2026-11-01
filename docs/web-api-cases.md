<!-- SPDX-License-Identifier: MIT -->
# Web unit, stage 2: API cases

API cases for DV-Router's HTTP APIs on the API3 path scheme: `/api/v1/{public,auth,rtc,user,operator}`
of the web service (`dv-svc-web`) and `/api/v1/admin` of the admin service (`dv-svc-admin`).
Built on the stage 1 rig (`docs/rig.md`, "Web profile"). Calibrated against `dv-router:wip-f3adb62`, rerun on
**`dv-router:wip-648e539`** (a superset of 13e7e70: API4, API2, WEB2, ADM1, LOGIN1 and the later changes; see
`docs/results-adaptation.md` for the diff against 5aa7cfd). The route tables were compared with the specs of the
DV-Router checkout (`DVT_API_SPEC_DIR=<DV-Router>/docs/api`): API2 generates the route tables from the YAML and
changed no path, method or role; the only addition is the four root probe operations of WEB2.

Sources (DV-Router, read only): `docs/api/README.md` ("API3 audience paths"), `docs/api/openapi-web.yaml`,
`docs/api/openapi-admin.yaml`, `docs/project/testing.md` ("API3 regression case handoff"),
`docs/design/web.md`, `docs/design/admin.md`.

## What is in the repo

| File | Contents |
|------|----------|
| `dvtest/api_web.py` | One record per web operation (52 operations since WEB2: GET/HEAD `/health` and `/ready` added, 36 path templates: method, template, prefix, minimum role, safe probe, notes), the 59-row migration table with the 55 REMOVED old templates (30 web, 25 admin) and the 4 unchanged RTC templates, a regex reader of the OpenAPI files and the comparison with the tables |
| `dvtest/api_admin.py` | The same for the admin service (28 operations, 25 templates, roles anonymous / viewer / operator / admin), root probes, the three mounts |
| `dvtest/api_probe.py` | Sends a probe and judges only the AUTHORIZATION class (401 below the floor and anonymous, 403 for a signed-in role that is too low, anything but 401/403/404 at or above the floor) |
| `dvtest/admin.py`, `dvtest/argon2_pure.py`, `dvtest/tools/admin_users.py` | Admin client (host and prefix mount, TOTP enrolment); the writer of the admin accounts: since ADM1 it runs the image's own `dv-svc-admin user add NAME ROLE --state-dir DIR` (password on stdin) in a throw-away capped container (`/usr/local/bin/dv-svc-admin` of the FROM-scratch image, no network, `--memory/--memory-swap/--cpus/--pids-limit` of `dvtest.limits`), and falls back to the stdlib Argon2id (`dvtest/argon2_pure.py`, RFC 9106 vector tested) when the subcommand is absent; `run.sh` logs the route (`out/<run>/web/admin-users.route`, `DVT_ADMIN_USERS=auto|image|python`). The service reads the users file at start, so no restart is needed |
| `cases/test_web_api.py` | The cases (below); results of the matrix go to `out/<run>/web-api-matrix.jsonl`, differences from the documents to `out/<run>/web-findings.txt` |
| `rig/config/dv-router/admin.toml`, `rig/compose.yml`, `dvtest/tools/tls_proxy.py`, `scripts/run.sh` | Admin service on in the web profile (always, with `web.toml`), second TLS listener `https://dvt-admin:8443` of web-proxy (`--route LISTEN=UPSTREAM`, port and target configurable), admin logins in `out/<run>/web/admin.env` |
| `scripts/lib/killproc.sh`, `dvtest/control.py` | `kill-proc xlxd-sut dv-svc-web KILL` added to the allow-list (KILL only, never the launcher; the agreement unit test counts 8 combinations) |

Consistency check of the tables with a DV-Router checkout (the unit test skips without the variable):
`DVT_API_SPEC_DIR=<DV-Router>/docs/api python3 -m pytest -q -m unit cases/unit/test_api_tables.py`
(compares `(method, path)` of both specs with the tables and the README migration table with
`api_web.MIGRATION`; additions and removals are reported).

## Coverage

Last run (`out/20261101T194729Z`, image `wip-648e539`, 5 min 10 s): **373 passed, 1 failed**; the one failure
(`GET public/events as user`, 429 "still rate limited") was a rig defect, not the SUT: the stdlib TLS proxy kept the
upstream stream open after the client closed it, so the service freed the slot only at its next 15 s heartbeat.
`dvtest/tools/tls_proxy.py` now closes the upstream at once when the client goes away (unit test
`test_client_close_closes_the_upstream_stream_at_once`) and the case passes (`-k "public and events"`, 10 passed,
`out/20261101T202437Z`), so the file is 374 of 374. Before (5aa7cfd line): 357 of 357. The 17 new cases: 12 matrix
rows (the four WEB2 root probes x anonymous/user/operator), `test_web_health_and_ready` (in `test_web_smoke.py`),
`test_declared_success_statuses_of_real_calls`, and three LOGIN1 limit cases.
Matrix lines (`web-api-matrix.jsonl`): web 156 pass, admin 112 pass, removed URLs 200 pass, limits 6 pass, success 1 pass.
FINDING lines in the whole file: 2 (was 11), see Findings.

| Case group | What it asserts | Count |
|------------|-----------------|-------|
| (a) `test_prefix_minimum_role` | All 52 web operations (48 plus the WEB2 root probes GET/HEAD `/health` and `/ready`) x anonymous / user (may_tx false; may_tx true for message POSTs) / operator = 156. By prefix: root probes 4 and public 6 operations (all roles allowed), auth 8, rtc 4, user 11, operator 19. Below the floor 401 (anonymous) or 403 `forbidden` (user on `/operator`), at or above the floor not 401/403/404 (a handler's own `not_found` problem for a missing target, or a 403 of a later guard such as `csrf`, `step_up_required`, `totp_invalid`, is a handler answer). rtc: anonymous refused (401, or the documented 404 when TURN is disabled), signed-in callers reach the handler. HEAD aliases match GET (`test_head_aliases_match_get`) | 156 |
| (h) `test_admin_minimum_role` | All 28 admin operations x anonymous / viewer / operator / admin on the host mount (`https://dvt-admin:8443`): login, TOTP enrolment, step-up window from the login. Roles: anonymous 4 operations, viewer 13, operator 1 (kick), admin 10 | 112 |
| (b) `test_removed_web_urls_are_404`, `test_removed_admin_urls_are_404` | The 55 moved templates answer 404 for every listed method (44 web methods x anonymous / operator = 88; 28 admin methods x anonymous / admin x host / prefix mount = 112), JSON-safe headers on the answers; the 4 RTC URLs still work (`test_unchanged_rtc_urls_still_work`); the two services do not serve each other's paths | 55 + 2 |
| (c) `test_public_*` | `state` / `lastheard` body shape, weak ETag, 304 for GET and HEAD, HEAD parity, CORS `*` (no credentials), `cache-control: public, max-age=2`, nosniff, 405 on other methods with problem+json and CORS, SSE framing (`retry: 3000` then `event: hello`, event names, ids `<boot>-<n>`, headers), resume with an unknown id, at most 4 streams per IP (5th is 429 `Retry-After: 5`) | 8 |
| (d) `test_own_account_*`, `test_operator_totp_rules` | Identity of `/auth/session` and `/user/me`, cookie attributes, CSRF (missing / wrong: 403), Origin (foreign, look-alike, none: refused), non-JSON and malformed bodies, own sessions listed without cookie values, revoke one by id, password change (wrong current password, too short, old password rejected, sessions revoked), revoke all, operator login needs the TOTP code, a code is accepted once, re-enrolment needs the current authenticator | 4 |
| (e) `test_room_messages_*`, `test_ids_*`, `test_prefix_floor_*`, `test_method_*` | Room-message history: anonymous 401, user and operator 200 `{items,total}`, 400 for bad `limit` / `after`, POST 403 without `may_tx`, 400 for extra field / empty text, 403 without CSRF; ID sync-status GET/HEAD: anonymous 401, user 403, operator 200 or 503, foreign Origin 404; unknown paths; 405 `method_not_allowed`; the RFC 7807 shape of 401 / 403 / 404 / 405 / 400 bodies | 4 |
| (g) `test_xss_*` | Hostile values (script tags, attribute breakouts, template markers, `javascript:`, line separators) in query parameters, path segments, account names (PATCH, then read back by three routes), invite callsign / role, register fields, message text and destinations, audit tail: every answer is JSON (or text/plain), never HTML, with nosniff; values come back unchanged or are refused 4xx; pages (`/`, `/ops/`, `/shared/core.js`, admin `/`) carry a CSP with `default-src 'self'`, no inline script, `frame-ancestors 'none'`. Rendering in a browser is stage 3 | 3 |
| (h) admin | `/health` and `/ready` at the root of the host and prefix mounts (200 / 503 with reasons, no cookie, HEAD, POST is 405), wrong Origin -> 404 on both mounts, unknown host -> 404, cookie policy of both mounts (`__Host-dv_admin; Path=/` and `__Secure-dv_admin; Path=/admin/`), security headers, enrolled login needs the code | 5 |
| (f) LOGIN1 `test_limits_login_missing_code_is_no_failure`, `..._wrong_codes_lock`, `..._wrong_passwords_count_challenges_do_not` (`slow`, throwaway enrolled operators) | the right password without a code on an enrolled operator, five times: a `totp_required` challenge each time, no lockout budget spent, then password + fresh code logs in (200); the right password with a WRONG code is a guess: 401 each and after five the pair is locked, 429 `rate_limited` with `Retry-After` (API4), also for the right code; wrong passwords count as before and a challenge in between preserves the count | 3 |
| (f) `test_declared_success_statuses_of_real_calls` | real calls of the account flow answer the success code their operation declares in the OpenAPI file (API4): 201 register / invite issue / reset, 204 logout / rtc close / alive, 200 the rest of the calls made; the matrix probes still judge the authorization class only | 1 |
| (f) `test_limits_*` (LAST, `slow`) | Register: 6 simultaneous invalid redemptions: exactly 2 pass the bucket, the rest 429 `rate_limited` with `Retry-After: 5` and matching `retry_after`; login lockout: 5 wrong passwords (401 each), then the right password is refused with 429 `rate_limited`, `Retry-After: 899` (15 min); login burst: 16 quick failures, first 429 at request 11 (burst 10), `Retry-After: 1`. `dv-svc-web` is killed once before (clean buckets, `kill-proc ... dv-svc-web KILL`) and once after (restore); the wait covers the launcher respawn (about 0.5 s) and the RTC 503 window | 3 |

Accounts (created once per session, register budget respected): operator `N0CALL` (stage 1), second operator
`N0OP` (removed-URL and session-ending probes), users `N0RX` (rx only), `N0TX` (may_tx), `N0ONE`, `N0PWD`, `N0XSS`,
`N0LCK` (throwaway ones); admin `admin`, `operator`, `viewer` (random passwords per run).

Safety of the probes (checked by `cases/unit/test_api_tables.py`): mutating probes carry an empty or invalid body
or a non-JSON body (forced ID sync, raw configuration PUT without `If-Match`), name targets that do not exist
(circuit, callsign, session id, history sequence, child), and the two operations that end the caller's own session
run on a throwaway login (a revoke-all re-logs the account in afterwards).

## Findings (observed vs documented)

The twelve findings of the f3adb62/5aa7cfd runs and what they became on 13e7e70/648e539 (the observed statuses are still
asserted, now as the documented answer; `web.finding()` is kept only for a real difference):

| Operation | Observed (unchanged) | Status now |
|-----------|----------------------|------------|
| `GET /api/v1/public/{state,lastheard}` | no `Content-Security-Policy` header on the JSON answers | documented by API4 (web.md "Public API": CSP on static files only); asserted absent |
| `OPTIONS /api/v1/public/state` | 405 with `Access-Control-Allow-Origin: *` | documented by API4 (no preflight is served); asserted |
| every unsafe route with a non-JSON body | 403 `csrf`, also for a user on an operator route | documented by API4 (web.md "Guard order": write guards before the audience floor on the web service; on the admin service the role first); asserted |
| `POST /api/v1/operator/ids/sync/force` | `text/plain` probe gets 403 `csrf` before the handler | documented by API4 (same guard order; README UI1) |
| `GET /api/v1/{user,operator}/<unknown>` | 404 for every role, anonymous included | documented by API4 (route miss is 404 before the floor) |
| unknown API paths | 404 `application/problem+json` `not_found`, the same as a handler's "no such target" | documented by API4; remains a limit of the probes (a route miss and a missing target look alike) |
| `GET /api/v1/rtc/ice` | 404 (TURN disabled) for anonymous callers too | documented by API4 (web.md RTC); asserted when TURN is off |
| `POST /api/v1/rtc/{close,alive}` | 403 `forbidden` for an unknown or foreign circuit | documented by API4 (web.md RTC rows; DOC6 adds the alive statuses); asserted |
| `POST /api/v1/auth/session/totp` | user 400; operator without `X-DV-TOTP` 403 `totp_invalid` | documented by API4 (web.md Account API); asserted |
| login lockout | right password after 5 failures: 429 `rate_limited`, `Retry-After` 899 | documented by API4 (LOGIN1 table of web.md); asserted |
| `GET /api/v1/user/recordings*` | 503 without a recorder | listed as 503 in the OpenAPI (unchanged, consistent) |
| admin legacy direct mount | not reachable from the tester | unchanged: not exercised (FINDING line) |

New observations on 648e539 (FINDING lines of the file): `GET /ready` answered 503 24 times before the first 200 although
`web_base_url` had already seen `/api/v1/public/state` (the service is up before it is ready: the readiness reasons are the hub
answer, 36 s since start and RTC ownership recovery; the browser fixtures now wait for `/ready`). The admin service has the same
probes on `/api/v1/health` and `/api/v1/ready`. Closed by this build: the nine rows above (documented), "26 web operations
declare no 2xx" (every operation declares its success code now, see below), the missing admin account tool (ADM1).

Declared success codes (API4): 201 register, invite issue and password reset; 204 logout, rtc close and rtc alive; 200 for the
other operations; `dvtest/api_web.py` / `dvtest/api_admin.py` carry the declared code per operation and
`test_declared_success_statuses_of_real_calls` checks the real calls; the matrix probes still judge only the authorization
class (a probe body is invalid on purpose).

Consistent with the documents (asserted): `retry: 3000` as the first SSE line then `event: hello` (DV-Router's
answer of 2026-11-01), hello carries `boot`, `last_id`, `view`, `now_ms`; weak ETag and 304 for GET and HEAD;
public CORS `*` without credentials on every `/api/v1/public/*` reply including errors; no CORS on credentialed
routes; per-IP SSE cap 4 with `Retry-After: 5`; RFC 7807 shape with `type = /api/v1/problems/<code>`; register
burst 2 and login burst 10 per client prefix; admin wrong Origin or unknown host -> 404; cookie names and paths of
both admin mounts.

## Not covered, and why

| What | Why |
|------|-----|
| Legacy direct admin mount (cookie `dv_admin`) | Loopback development mount, not reachable through the rig's proxy |
| The `/admin/` prefix mount from an address outside `allow_from` | `allow_from` lists the tester (the only client); an outside address does not exist in the rig |
| Admin lockout and login buckets | Only the web limits (MS9) were requested; the admin ones are separate in-memory state and would need another restart |
| Admin viewer / operator by bootstrap | `secrets/admin-bootstrap` creates one admin only; viewer and operator are added by `run.sh` with the image's `dv-svc-admin user add` (ADM1), with the pure-Python hash as the fallback |
| Real kick / ban / delete, config writes, node and child restart, forced ID sync | Destructive or acting; probes are invalid on purpose, so these paths are checked only up to the handler's validation |
| Recordings with a recorder, guest listening, TURN credentials, WebRTC media | The rig runs without recorder, `guest_listen` off, no `turn_urls`; media is stage 5 |
| `retry`/`hello` after a hub restart, `resync`, 256-message lag, 1024-stream global cap | Need hub restarts or a load generator (stage 5) |
| Rendering of stored values in a browser | Stage 3 (Playwright) |

## Gaps that need DV-Router

Closed since the first stage: a way to create viewer and operator accounts (ADM1), the status of a locked-out login (429, API4),
the statuses of a foreign circuit (API4), the success codes (API4). Still open: whether the admin `/ready` and `/health` may be
rate-limited per IP (documented for `/ready`: 5/s burst 20); the order of guards for the admin service is documented, the
admin lockout and login buckets are not exercised (separate in-memory state); distinguishing "no such route" from "no such
target" (both 404 `not_found`).

## Rerun

```sh
XLXD_SUT_IMAGE=dv-router:wip-648e539 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_web_api.py
```

`--profile web` adds `web.toml` and `admin.toml` by itself. The file takes about 4 minutes: the account
registrations (0.2/s), the 156 + 112 matrix probes, the two web restarts and the RTC wait (about 36 s). The limit
cases (and the SSE per-IP cap) are marked `slow`; select parts with `-k`. Run the file alone: the
limits cases restart `dv-svc-web`, which ends every session of the run.
