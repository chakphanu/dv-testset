<!-- SPDX-License-Identifier: MIT -->
# Web unit, stage 3a: browser cases

DV-Router's web UI in real browsers: `cases/test_web_browser.py`, 18 cases, 54 runs (every case on three
engines). Built on the stage 1 and 2 rig (`docs/rig.md`, "Web profile"; `docs/web-api-cases.md`) and on the new
`browser` profile (`docs/rig.md`, "Browser profile"). Calibrated against the image **`dv-router:wip-5aa7cfd`**
(the first calibration runs were on `wip-f3adb62`; the dashboard defect below is the same on both).

## Engines

| Label | Engine | Build in this run |
|-------|--------|-------------------|
| Playwright Chromium | Chromium | 156.0.8078.4 |
| Playwright Firefox | Firefox | 157.0 |
| Playwright WebKit | the WebKit of Playwright 1.64.0 (Linux build) | 27.2 |

"Playwright WebKit" is the engine of Safari but it is **not Safari**: a Linux build driven by Playwright, no Safari
behaviour, no iOS. Nothing here says anything about Safari or iOS; that needs real devices (the Safari-specific
audio route of web.md is a W8 release test). The browsers run in the rig service `browser` (pinned official image
`mcr.microsoft.com/playwright/python:v1.64.0-noble`, Playwright 1.64.0 on both sides); every context ignores
certificate errors (`ignore_https_errors`, the proxy certificate is self-signed) and nothing else.

## What the cases cover

| Case | What it asserts | Engines |
|------|-----------------|---------|
| `test_dashboard_renders_the_state` | the dashboard as served shows room cards after its state arrived (no workaround). **Fails on all three engines: finding 1** | 3 |
| `test_dashboard_routes_with_the_fragment_workaround` | with `/public.js` served with the one-line fix (below): the six routes render through the page's own navigation, state fetched, no error banner | 3 |
| `test_signed_out_dashboard_requests` | a signed-out dashboard requests nothing under `/api/v1/user/`, `/operator/` or `/rtc/` (no message history), the public SSE is connected (200, `text/event-stream`), the only `auth/session` answers are 401, no error banner and no "Unauthorized", "Cannot connect", "Offline" text, and the history sign-in link is shown | 3 |
| `test_login_wrong_password_text` | wrong password and unknown callsign give the same fixed text, no cookie, password field cleared, the callsign is not echoed | 3 |
| `test_user_login_cookie_and_logout` | user login in the form; the cookie as the browser sees it is `__Host-dv_web`, Secure, HttpOnly, SameSite=Strict, Path=/, host-only; `document.cookie` does not show it; password and CSRF token are in no web storage; sign out removes the cookie and the session answers 401 | 3 |
| `test_operator_login_with_totp_step` (slow) | password alone does not sign an operator in (code field appears, no cookie), a wrong code is refused, the right code signs in, the cookie flags, `role operator / totp ok`, sign out | 3 |
| `test_ops_pages_render` | the six operator pages (clients, events, accounts, invites, audit, **Radio IDs `#/ids`**) render for an operator with the headings of the UI and the five-plus-one navigation, one document | 3 |
| `test_ops_pages_match_the_api` | accounts page = `/operator/accounts` (callsign, name, role, status), invites page lists the open invite and no code value, audit page has the invite action, Radio IDs rows = `/operator/ids/rows` | 3 |
| `test_ops_pages_refuse_user_and_anonymous` | anonymous and user get "Operator access required" (also `#/ids`), the page requests no operator route, the same browser gets 401 / 403 from `/operator/accounts`, `/audit`, `/invites`, `/ids/rows` | 3 |
| `test_xss_display_names_on_the_accounts_page` | eight payloads stored as display names (registration), the accounts page shows each stored name as text, `window.__pwn` unset, no dialog, no live element made from the text | 3 |
| `test_xss_account_limits_are_inert` | free text in the `limits` object (the only other free-form account field) shown as the API's JSON | 3 |
| `test_xss_room_message_history_is_inert` (slow) | the eight payloads as room messages, a signed-in user's dashboard shows each stored body as `textContent` | 3 |
| `test_xss_room_message_arrives_live_on_the_public_stream` (slow) | a payload posted while a signed-out dashboard is Live appears as text. **Skipped with finding 2 on Chromium and Firefox in the last run (WebKit passed); it varied between runs** | 3 |
| `test_xss_invite_fields` | hostile callsign, role and expiry in invites are refused 400/422, the invites page shows no payload | 3 |
| `test_xss_audit_text` | after hostile logins, hostile path segments and a name change: every row of the audit page is a row of the API tail, hostile characters shown as text if any (none reached the tail: failed logins are recorded as `unauthenticated`) | 3 |
| `test_xss_last_heard_callsign` | six D-STAR callsigns with markup characters are refused at link time by the router, so there is nothing to render. **Skipped (3)** | 3 |
| `test_xss_reflected_in_url_and_forms` | payloads in query, hash and path of the dashboard, login, register, ops pages, typed into the Last heard filter and the login form: nothing runs, nothing becomes markup, the typed callsign is not echoed | 3 |
| `test_csp_header_and_enforcement` | CSP on `/`, `/login.html`, `/register.html`, `/totp.html`, `/ops/`: `default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, no unsafe-inline / unsafe-eval, nosniff; a deliberate inline script and inline handler are blocked and reported (`script-src-elem`, `script-src-attr`, `img-src`) | 3 |
| `test_admin_spa_loads` | the admin host sign-in screen renders, CSP restrictive | 3 |

XSS probes use the stage 1 and 2 helpers: the payloads (`dvtest/browser.py` `XSS_PAYLOADS`: script tag, `<img onerror>`,
`"><svg onload>`, `javascript:` URL, anchor with `javascript:`, template-literal and `{{ }}` markers, attribute
breakout, backtick / script breakout) go in through the API (registration, `PATCH` account, room message POST). "Inert"
means: `window.__pwn` stays undefined, no dialog, no `securitypolicyviolation` for a script, no live `img[onerror]`,
`svg[onload]`, `a[href^=javascript:]`, inline `script` in the DOM, and the visible text equals what the API stores.
The service stored display names and room messages unchanged (the page showed the literal payload); callsigns,
invite roles and expiries with markup are refused by the API.

Last run on `dv-router:wip-5aa7cfd` (`out/20261101T140506Z`, 8 min 28 s): **49 passed, 3 failed, 5 skipped**, 57
finding lines. Per engine (passed / failed / skipped): Playwright Chromium 16 / 1 / 2, Playwright Firefox 16 / 1 / 2,
Playwright WebKit 17 / 1 / 1; finding lines Chromium 6, Firefox 5, WebKit 46 (43 of them the style reports of
finding 3). The three failures are `test_dashboard_renders_the_state` (finding 1); the skips are the live-message
case (finding 2, two engines) and the last-heard case (3).

## Findings (observed vs documented)

None of these is asserted except where it is the documented behaviour (case 1). Each is a line in `web-findings.txt`.

| # | Observed | Documented / expected |
|---|----------|-----------------------|
| 1 | **The public dashboard does not render in any engine** (`wip-f3adb62` and `wip-5aa7cfd`). Once `/api/v1/public/state` has arrived, `PublicContent` in `web/site/public.js` returns ``html`<>...</>` ``; the vendored `htm-preact-3.1.1.module.js` turns `<>` into a node of type `''` (it exports no `Fragment`) and Preact calls `document.createElement('')`: `InvalidCharacterError` in Chromium, Firefox and WebKit, `main` stays "Loading", the header and the live state widget work. Reproduced outside the rig with the image's own files and the rig's state JSON. Minimal fix: a component that returns its children (the case `..._with_the_fragment_workaround` serves exactly that change, `dvtest.browser.patch_public_js`, and switches itself off when the script has no `<>`) | web.md: public dashboard with rooms, last heard, Listen/Talk and room messages |
| 2 | A room message accepted with 202, stored in the history and delivered on the public stream (a tester client sees an `event: data` frame with the body) does **not** always appear on a Live signed-out dashboard within 15 s (workaround on): missing on Chromium and Firefox in the last run, on all three in an earlier one, delivered on WebKit; the frame is `type data`, fields `body callsign id item_type protocol room ts_ms`, `item_type text`, `protocol web`, `room A`, so it looks valid; cause not found | web.md Roles: anonymous "sees the public text and status messages of a room as they arrive on the public event stream" |
| 3 | Playwright WebKit reports a CSP violation `style-src-attr` (inline style, source `htm-preact-3.1.1.module.js`) and the console error "Refused to apply a stylesheet..." on every page that renders; Chromium and Firefox report none | CSP with no violation in normal use |
| 4 | The signed-out dashboard probes `GET /api/v1/auth/session` and gets 401; Chromium and Playwright WebKit write a console error for it (Firefox does not) | not documented as expected; "no connection error shown" holds |
| 5 | At most 4 event streams per client address (public and `/api/v1/operator/stream` together) and a closed stream's slot is freed at the next 15 s heartbeat: pages opened within that time get 429, the app retries (3 s) and goes on, Firefox logs "can't establish a connection" | the cap and the 15 s heartbeat are documented; the effect on a reloading user or on one address with several tabs (the browser container is one address) is not. The cases wait for stale slots (`browser_fixtures.wait_for_stream_slots`) |
| 6 | A wrong authenticator code in the login form shows "The callsign, password or code is incorrect." The UI has a dedicated text for `totp_invalid` ("...wrong or was already used"), but the API answers a wrong code at login with plain 401. The password-only first step of an enrolled operator is audited as a denied login and counts toward the lockout (5 per (account, address)) | web.md: lockout 5 failures per 15 minutes; no word on the password-only step |
| 7 | The admin host answers 404 to a client address that is not in `[admin] allow_from` (the browser container until `<BROWSER_IP>` was added to the rig's list) | admin.md: allow_from "optional on the admin host, required for the /admin/ prefix"; whether it applies to the host when set is not stated |

Observed and consistent with the documents: CSP header on every page and enforced by each engine; cookie
`__Host-dv_web` with all documented attributes as the browser sees them; role floors in the UI and the API for
`/ops/` including Radio IDs; the user does not reach `#/ids`; message history is not requested when signed out;
page text equals the API for accounts, invites and Radio IDs; no invite code value on any page.

## RIG1 (DV-Router `docs/design/deployment.md`, "Test rig") against this rig

| Item | RIG1 | This rig (stage 1 and 3a) |
|------|------|---------------------------|
| `[web] enable` | not in the fragment (default `true`) | `web.toml` sets `enable = true` because the external template has `enable = false` |
| `trusted_proxies` | `["192.0.2.3/32"]` (a CIDR) | the bare proxy address (`config.md` accepts addresses or CIDRs) |
| `edges.webrtc.port` | `port = 50000` explicit | default (50000), not written |
| proxy `X-Forwarded-Host` / origin | `example.org`, browser origin `https://example.org` | the host the client used: `https://dvt-web` (certificate SAN) and `dvt-admin:8443`; the proxy is repo-owned stdlib Python (501 for `Upgrade`) |
| `public_ips`, one UDP port, no remap | private container address allowed | `<OWN_IP>` (172.30.x.11), port 50000, not exercised by the browser cases (stage 3b) |
| state dir, bootstrap, accounts recipe, pacing | state distinct from run dir, `secrets/web-bootstrap`, TOTP recipe, login 1/s burst 10, register 0.2/s | the same (`run.sh` writes the bootstrap, `dvtest/web.py` implements the recipe with a `Pacer`) |
| admin service | not mentioned | on in the web profile; `allow_from` lists the tester and the browser service |
| browsers | not mentioned (the fragment is for "browser/NAT/load rig") | own container at `<net>.91`, a separate client address behind the proxy: its logins and its 4-stream cap are its own, and the transport peer the web listener sees is always the proxy (web.md: 32 connections per transport peer, so one proxy carries all clients' share) |
| ACL | "keep web and the edge on the same fabric with its ACL active" | native launch with the template's ACL setting; not checked or changed by the rig |

No difference needed a change of the rig except `allow_from` (an error on our side: a browser at a new address).

## Not covered

| What | Why |
|------|-----|
| Listen / Talk (WebRTC, `/api/v1/rtc/ice`, WTURN1) and recorder playback | stage 3b |
| Safari and iOS | Playwright WebKit is not Safari |
| WebMonitor and the recordings page | no recorder in the rig, WebMonitor needs the rig's metrics |
| Last-heard rendering of a hostile callsign | the router refuses markup callsigns at link time for D-STAR (six variants); other protocols' callsign fields not tried |
| Live room text (finding 2) and the dashboard without the workaround beyond case 1 | the cases that need a rendered dashboard run with the workaround, named in each fixture (`dashboard_page`, `user_page`) |
| Operator actions (edit, reset, delete, invite form, kick, radio-ID commands) in the UI | forms are not submitted; the destructive paths are API-level in stage 2 |
| `data-testid` selectors (WEB2), `/health` and `/ready` on web | not on `wip-5aa7cfd`; selectors are in `cases/web_pages.py` only |

## Rerun

```sh
XLXD_SUT_IMAGE=dv-router:wip-5aa7cfd DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_browser.py
```

Pull the base image once (`docker pull` of `BROWSER_BASE_IMAGE` of `.env.example`, the one internet use); the
first run builds `dvt-browser:local` and the tester image. Select parts with `-k` (`-k chromium`, `-k xss`); the
file takes about 8 minutes, most of it waiting for the registration budget, TOTP steps and the event-stream slots.
Run it alone: the stage 2 limits cases restart the web service. Failure artifacts and notes (CSP headers seen,
engine builds, page-error dumps with the public state JSON) are under `out/<run>/web/browser/`; findings are in
`out/<run>/web-findings.txt`.
