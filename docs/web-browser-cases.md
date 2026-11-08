<!-- SPDX-License-Identifier: MIT -->
# Web unit, stage 3a: browser cases

DV-Router's web UI in real browsers: `cases/test_web_browser.py`, 19 cases, 57 runs (every case on three
engines). Built on the stage 1 and 2 rig (`docs/rig.md`, "Web profile"; `docs/web-api-cases.md`) and on the
`browser` profile (`docs/rig.md`, "Browser profile"). Calibrated against `dv-router:wip-5aa7cfd` and **rerun on
`dv-router:wip-648e539`** (superset of 13e7e70: DASH1, WEB2, LOGIN1b, TALK1; diff in `docs/results-adaptation.md`). Since WEB2
every selector in `cases/web_pages.py` is a `data-testid` (naming scheme: DV-Router `web/README.md`, "Stable browser rig
hooks"), and the connection widgets are read from `data-state` (`listen-connection`, `dashboard-connection`: idle,
connecting, connected, failed, closed). Controls without a hook keep a text/role/id fallback in the same module (h1,
`#auth-error`, `#auth-notice`, `#talk-status`, `p[role=status]`, `main .card`, the ops tables, `.ops-identity`, the recordings
page). The browser fixtures wait for `GET /ready` of `dv-svc-web` (200) instead of polling a page.

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
| `test_dashboard_renders_the_state` | the dashboard as served (no workaround) shows room cards after its state arrived, the six routes render through the page's own navigation, no page error and no CSP report (Playwright WebKit: recorded, see finding 3). **Passes on all three engines since DASH1** (failed on all three before) | 3 |
| `test_event_stream_slot_is_freed_on_reload` (record) | six reloads of a Live dashboard: `pagehide` closes the EventSource, so no reload gets a 429 (0 of 6 on every engine, the next stream is live within 0.0 to 0.3 s) | 3 |
| `test_signed_out_dashboard_requests` | a signed-out dashboard requests nothing under `/api/v1/user/`, `/operator/` or `/rtc/` (no message history), the public SSE is connected (200, `text/event-stream`), the only `auth/session` answers are 401, no error banner and no "Unauthorized", "Cannot connect", "Offline" text, and the history sign-in link is shown | 3 |
| `test_login_wrong_password_text` | wrong password and unknown callsign give the same fixed text, no cookie, password field cleared, the callsign is not echoed, and (LOGIN1b) neither the wrong-code text nor a code field | 3 |
| `test_user_login_cookie_and_logout` | user login in the form; the cookie as the browser sees it is `__Host-dv_web`, Secure, HttpOnly, SameSite=Strict, Path=/, host-only; `document.cookie` does not show it; password and CSRF token are in no web storage; sign out removes the cookie and the session answers 401 | 3 |
| `test_operator_login_with_totp_step` (slow) | password alone does not sign an operator in (code field appears, no cookie), a wrong password still gives the generic text, a wrong code AFTER the challenge gives the dedicated wrong-code text (LOGIN1b), the right code signs in, the cookie flags, `role operator / totp ok`, sign out Updated by dv-router ruling 2026-11-04 (TOTP1 f1e2f09d: `clearLoginChallenge` also clears the auth controller's challenge): after the wrong code ONLY the code is retyped (the password field is not touched; any input event on it, even refilling the same value, abandons the challenge) and signs in; path 2: sign out, ask again, refill the password (the code field disappears), press Sign in (a new challenge, the code field is back), right code signs in. The stale wrong-code banner after a password edit: asserted cleared only with `DVT_TOTP1B=1` (TOTP1b, wip-c165c93 and later: one more wrong code, then the password refill clears the banner); not asserted on 71ab207 (known small UX defect) | 3 |
| `test_ops_pages_render` | the six operator pages (clients, events, accounts, invites, audit, **Radio IDs `#/ids`**) render for an operator with the headings of the UI and the five-plus-one navigation, one document Since I18N1 the time headings say "(local)": Events `Time (local)`, Audit `Time (local)`, Invites `Expires (local)`, Radio IDs `Seen (local)` (were "(UTC)"; verified in DV-Router `web/site/ops/ops.js`); the Events time cells match `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$` (Gregorian, 24-hour, Latin digits, web.md "English and Thai") and, in the rig's UTC context, equal the API `ts_ms` floored to the second (FINDING and format only when the events API exposes no `ts_ms`). Dv-router ruling 2026-11-04 | 3 |
| `test_ops_pages_match_the_api` | accounts page = `/operator/accounts` (callsign, name, role, status), invites page lists the open invite and no code value, audit page has the invite action, Radio IDs rows = `/operator/ids/rows` | 3 |
| `test_ops_pages_refuse_user_and_anonymous` | anonymous and user get "Operator access required" (also `#/ids`), the page requests no operator route, the same browser gets 401 / 403 from `/operator/accounts`, `/audit`, `/invites`, `/ids/rows` | 3 |
| `test_xss_display_names_on_the_accounts_page` | eight payloads stored as display names (registration), the accounts page shows each stored name as text, `window.__pwn` unset, no dialog, no live element made from the text | 3 |
| `test_xss_account_limits_are_inert` | free text in the `limits` object (the only other free-form account field) shown as the API's JSON | 3 |
| `test_xss_room_message_history_is_inert` (slow) | the eight payloads as room messages, a signed-in user's dashboard shows each stored body as `textContent` | 3 |
| `test_xss_room_message_arrives_live_on_the_public_stream` (slow) | a payload posted while a signed-out dashboard is Live appears as text. **Passes on all three engines since DASH1** (live text before the room snapshot is buffered); no skip branch | 3 |
| `test_xss_invite_fields` | hostile callsign, role and expiry in invites are refused 400/422, the invites page shows no payload | 3 |
| `test_xss_audit_text` | after hostile logins, hostile path segments and a name change: every row of the audit page is a row of the API tail, hostile characters shown as text if any (none reached the tail: failed logins are recorded as `unauthenticated`) | 3 |
| `test_xss_last_heard_callsign` | six D-STAR callsigns with markup characters are refused at link time by the router, so there is nothing to render. **Skipped (3)** | 3 |
| `test_xss_reflected_in_url_and_forms` | payloads in query, hash and path of the dashboard, login, register, ops pages, typed into the Last heard filter and the login form: nothing runs, nothing becomes markup, the typed callsign is not echoed | 3 |
| `test_csp_header_and_enforcement` | CSP on `/`, `/login.html`, `/register.html`, `/totp.html`, `/ops/`: `default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`, no unsafe-inline / unsafe-eval, nosniff; a deliberate inline script and inline handler are blocked and reported (`script-src-elem`, `script-src-attr`, `img-src`); normal use (dashboard, login, ops) reports nothing on Chromium and Firefox (asserted); Playwright WebKit still reports `style-src-attr` (recorded, finding 3) | 3 |
| `test_admin_spa_loads` | the admin host sign-in screen renders: h1 "Admin sign in" (was "Sign in to the admin interface"), "Manage node configuration and operation.", the EN | TH switch with EN pressed (UIR1 / I18N1, dv-router ruling 2026-11-04); CSP restrictive | 3 |

XSS probes use the stage 1 and 2 helpers: the payloads (`dvtest/browser.py` `XSS_PAYLOADS`: script tag, `<img onerror>`,
`"><svg onload>`, `javascript:` URL, anchor with `javascript:`, template-literal and `{{ }}` markers, attribute
breakout, backtick / script breakout) go in through the API (registration, `PATCH` account, room message POST). "Inert"
means: `window.__pwn` stays undefined, no dialog, no `securitypolicyviolation` for a script, no live `img[onerror]`,
`svg[onload]`, `a[href^=javascript:]`, inline `script` in the DOM, and the visible text equals what the API stores.
The service stored display names and room messages unchanged (the page showed the literal payload); callsigns,
invite roles and expiries with markup are refused by the API.

Before, on `dv-router:wip-5aa7cfd` (`out/20261101T140506Z`, 8 min 28 s): 49 passed, 3 failed, 5 skipped (the dashboard case on three
engines; the live-message case skipped on two; the last-heard case skipped on three).

Now, on `dv-router:wip-648e539` (`out/20261101T203217Z` as part of the 3a+3b session, 21 min 13 s, and `out/20261101T202754Z`
`-k chromium`: 18 passed, 1 skipped): the browser file is 57 runs: **52 passed, 2 failed, 3 skipped** in the first full session, and the
two failures were case assertions that were too strict for Playwright WebKit (CSP reports of finding 3; the cases now record them
for that engine and still assert none for Chromium and Firefox): re-run in `out/20261101T205603Z` (`-k` selection of the recorder file and the two WebKit cases: 21 passed). So **54 passed, 0 failed, 3 skipped** (the skips are the last-heard case, see
Not covered). That session also showed a rig matter (see "Browser connection loss" below): a long session lost browsers
(`Browser closed`) in 4 recorder setups; run 3a and 3b as separate runs.

## Findings (observed vs documented)

Each is a line in `web-findings.txt`. Status on `wip-648e539` (and 13e7e70):

| # | Observed before | Status now |
|---|-----------------|------------|
| 1 | the public dashboard did not render in any engine (`<>` fragment, `InvalidCharacterError`) | **CLOSED by DASH1**: `test_dashboard_renders_the_state` passes on Chromium, Firefox and Playwright WebKit with no workaround, no page error; `patch_public_js` and `patch_fragments_js` are deleted |
| 2 | a room message delivered on the public stream did not always appear on a Live signed-out dashboard | **CLOSED by DASH1**: the live-message case passes on all three engines, no skip branch |
| 3 | Playwright WebKit reports CSP `style-src-attr` (inline) from `htm-preact-3.1.1.module.js` and "Refused to apply a stylesheet" | **STILL OPEN, WebKit only**: DASH1 removed every `style=` of `web/site` (README rule), yet WebKit reports it on every page that renders (dashboard, login, ops, recordings, 100+ lines in the run); Chromium and Firefox report none and that is asserted. Cause not determined here: the reported source is the vendored htm-preact module, `web/site` has no `style=`; to DV-Router |
| 4 | the signed-out dashboard's `GET /auth/session` 401 is a console error on Chromium and WebKit | unchanged, documented contract: recorded note only (Chromium; WebKit the same) |
| 5 | event-stream cap 4 per address, a closed stream freed at the next 15 s heartbeat; reload gets 429 | **CLOSED for the page**: `pagehide` closes the EventSource, 0 of 6 reloads got a 429 on every engine (`test_event_stream_slot_is_freed_on_reload`); `wait_for_stream_slots` is kept for pages closed by the harness. The rig's stdlib TLS proxy used to delay the release of an API client's slot (it did not pass the client close upstream); fixed in `dvtest/tools/tls_proxy.py` |
| 6 | the login form showed the generic text for a wrong code (the dedicated text was dead) | **CLOSED by LOGIN1b** (page) and LOGIN1 (budget): the wrong-code text appears only after a `totp_required` challenge for the same pair, never for a plain wrong password; the password-only step no longer counts toward the lockout |
| 7 | admin host 404 for an address outside `[admin] allow_from` | unchanged (not stated by admin.md) |

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
| Listen / Talk (WebRTC) and recorder playback | stage 3b, below |
| Safari and iOS | Playwright WebKit is not Safari |
| WebMonitor | needs the rig's metrics |
| Last-heard rendering of a hostile callsign | the router refuses markup callsigns at link time for D-STAR (six variants); other protocols' callsign fields not tried |
| Last-heard rendering with markup | skipped on 3 engines (the router refuses the callsigns at link time) |
| Operator actions (edit, reset, delete, invite form, kick, radio-ID commands) in the UI | forms are not submitted; the destructive paths are API-level in stage 2 |
| Controls without a `data-testid` | h1, the auth error/notice, the talk status line, the ops tables, the recordings page keep a text/role/id fallback in `cases/web_pages.py` |

## Rerun

```sh
XLXD_SUT_IMAGE=dv-router:wip-648e539 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_browser.py
```

Pull the base image once (`docker pull` of `BROWSER_BASE_IMAGE` of `.env.example`, the one internet use); the
first run builds `dvt-browser:local` and the tester image. Select parts with `-k` (`-k chromium`, `-k xss`); the
file takes about 8 minutes, most of it waiting for the registration budget, TOTP steps and the event-stream slots.
Run it alone (and not in one session with the 3b files: see "Browser connection loss"): the stage 2 limits cases restart the web service. Failure artifacts and notes (CSP headers seen,
engine builds, page-error dumps with the public state JSON) are under `out/<run>/web/browser/`; findings are in
`out/<run>/web-findings.txt`.

# Stage 3b: WebRTC Listen / Talk and recorder playback

`cases/test_web_webrtc.py` (12 cases, 36 runs) and `cases/test_web_recorder.py` (7 API cases and 4 page cases on three engines, 19 runs). Calibrated against `dv-router:wip-5aa7cfd`, rerun on **`dv-router:wip-648e539`** (55 of 55 pass, `out/20261101T205817Z`, 12 min 50 s; before: 46 runs, 45 passed and the hold case an expected failure); docs read: DV-Router `docs/design/web.md`, `deployment.md` ("Test rig"),
`docs/project/testing.md`, `docs/api/openapi-web.yaml`, `config.md`, `recorder.md` and the page sources `web/site/public.js`,
`web/site/recordings/recordings.js` (a tree two documentation commits ahead of the image).

## Rig changes

| What | Detail |
|------|--------|
| protocol talker | a USRP dynamic client (`cases/crossmode_fake.py` `UsrpPeer`, PCM straight into the room, no transcoder): the run needs `DVT_USRP_DYNAMIC=1` (the cases skip with that reason otherwise) |
| fake microphone | `playwright run-server --unsafe` in the `browser` service lets a client pass `args` / `firefoxUserPrefs` in the `launch-options` query of the websocket URL (`dvtest/browser.py` `launch_options`, `with_launch_options`). Chromium: `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream --use-file-for-fake-audio-capture=/media/fake-mic.wav --autoplay-policy=no-user-gesture-required`; the 700 Hz WAV is written by the tester at run time into `out/<run>/media/` (mounted read-only at `/media`; generated, never committed). Firefox: `media.navigator.streams.fake`, `media.navigator.permission.disabled`, autoplay prefs. Playwright WebKit has no flag: the microphone permission is granted per context and its own mock device answers |
| `/var/tmp` | the browser service has a 256m tmpfs there: Playwright WebKit buffers media in `/var/tmp/WebKit-Media-*` (playback failed with "Could not create temp file" on the read-only root) |
| recorder | `rig/config/dv-router/recorder.toml` (`[recorder] dir = "/var/lib/dv-recorder"`, `keep_days = 7`); naming it in `DVT_SUT_CONFIG_EXTRA` makes `run.sh` create `out/<run>/recorder` (0700) and mount it read-write, and sets `DVT_RECORDER=on` for the tester; without it the recorder cases skip and the stage 2 cases still see 503 |
| page probe | `dvtest/browser.py` `RTC_PROBE_JS` (installed with `add_init_script`): wraps `RTCPeerConnection`, records state changes and the page's own click with `performance.now()`, taps the received track (a `ScriptProcessorNode`: an AudioWorklet module would need a `blob:` URL, which the CSP forbids; a muted `<audio>` also holds the stream because Chromium feeds Web Audio from a remote stream only then), reads `getStats`, and runs the Goertzel scan (`goertzel_amplitude` / `scan_peak` in Python, the same algorithm in JavaScript; `cases/unit/test_browser_audio.py` runs both on the same synthetic data, the JavaScript with node when it is installed). Playwright's `wait_for_function` is not usable on these pages (it `eval`s a string, the CSP forbids that): `webrtc_support.wait_js` polls `evaluate` |
| dvcheck | the capture check claimed the WebRTC datagrams on UDP 50000 as USRP (the rig's USRP link rows use that port too) and ended the run with "wire check exit 1"; with `DVT_WEB=on` the USRP checker now claims only datagrams that carry a USRP header (`dvtest/check/usrp.py`) |

## What is covered

| Case | What it asserts | Engines |
|------|-----------------|---------|
| `test_listen_is_not_offered_to_anonymous` | `guest_listen` is off: a signed-out dashboard has no Listen panel and sends no `/rtc/` POST | 3 |
| `test_listen_receives_the_tone` | user without `may_tx`: no Talk controls; Listen / Connect on module H: connected, "Listening on module H", `GET /rtc/ice` once (404, TURN off), answer SDP has `PCMU/8000`, `ptime:20`, `ice-lite`, one host candidate `<net>.11:50000`; silence (rms < 0.004) before the over; a 700 Hz USRP over: `inbound-rtp` packets and bytes grow, 160 bytes per packet, 50 packets/s (19.96 to 20.09 ms), loss 0, codec `audio/PCMU`, jitter under 60 ms, the Goertzel scan in the page finds 700 Hz within 20 Hz (best of three 1 s windows); no packets and silence after the over; Disconnect sends `/rtc/close` and the circuit answers 403 afterwards | 3 (frequency: Chromium, WebKit; Firefox: see below) |
| `test_listen_lease_stays_alive_past_30_s` (slow) | 36 s of listening: connected throughout, four `POST /rtc/alive` (204) at connect and every 10 s, then a 2.5 s over still arrives (125 packets) and the tone is 700 Hz | 3 |
| `test_talk_reaches_a_protocol_listener` (slow) | Hold Talk disabled before Listen; pointer down on `talk-ptt`: mic, re-offer, "On air"; a 6 s hold stays "On air" until the release (TALK1: **passes, the expected-failure branch is gone**), then "Ended: Button released."; a USRP client on the module hears the over: frame count (>= 80 % of 50/s of the time on air), no gap over 120 ms (WebKit 250 ms), dominant frequency (Chromium 700 Hz within 20 Hz), the public state names `N0BRU` in `rooms[].onair.callsign`; the `renewed` acks (below) | 3 |
| `test_talk_hold_10_s_stays_on_air_until_release` (slow, new) | a 10 s hold on each engine: "On air" the whole time, the router hears about 10 s of frames (494 to 506), "Ended: Button released." at the release, at least 4 `renewed` acks (10 seen) | 3 |
| `test_talk_release_inside_the_watchdog_window` (new) | a 2 s hold: "Ended: Button released." at the release, 2 acks | 3 |
| `test_listen_gauges_in_the_hub_metrics` (new) | WEB2 gauges `dv_edge_webrtc_listeners` and `dv_edge_webrtc_room_listeners{room}` read from the hub metrics (`metrics xlxd-sut`): 1 while listening on room M, 0 after Disconnect (all three engines; skips when the family is absent) | 3 |
| `test_talk_denied_without_may_tx` (slow) | no `#talk-button`, no `.talk-controls`; a raw `{"op":"ptt","state":"down",...}` on the control channel is answered `{"op":"ptt","state":"denied","code":"denied"}`, never granted, nothing sent upstream | 3 |
| `test_talk_floor_busy_when_the_room_is_taken` (slow) | a USRP talker holds the module (1000 Hz, 24 s): Hold Talk shows "Denied: The floor is busy." within 0.22 to 0.4 s | 3 |
| `test_talk_second_talker_is_denied` (slow) | the browser is on air; a second USRP client sends 1700 Hz on the module: a third client hears no 1700 Hz | 3 |
| `test_reconnect_after_disconnect_and_reload` (record) | see Reconnect | 3 |
| `test_reconnect_after_the_lease_expires` (slow, record) | see Reconnect | 3 |
| `test_recording_notice_is_published` .. `test_audio_rate_limit` | see Recorder | API |
| `test_recordings_page_*`, `test_dashboard_shows_the_recording_notice` | see Recorder | 3 |

## TALK1: lease acknowledgements (wip-648e539)

The probe (`dvtest/browser.py` `RTC_PROBE_JS`) now records the frames of the control DataChannel. During a hold the edge answers
every accepted lease renewal of the live over with `{"op":"ptt","state":"renewed","code":"ok"}` and the page's 4 s watchdog is
reset by it: 6 acks in a 6 s hold and 10 in a 10 s hold on every engine, spaced 999 to 1025 ms (the page sends the lease every
second; the documented cap is one per 500 ms per peer, so the minimum interval asserted is 450 ms and the observed minimum is
999.0 ms); no other ptt frame but `ended/released` at the release; the 2 s hold gives 2 acks. Hold results (router side): Chromium
10 s: 506 frames, max gap 30 ms, peak 700 Hz; Firefox: 504 frames, max gap 45 ms, 1000 Hz (its fake source); Playwright WebKit:
494 frames, max gap 97 ms, peak 500 Hz (its mock device, as before). The status never leaves "On air" in the 0.5 s timeline of any
hold, the release text is "Ended: Button released." (finding 10 closed). The lease denied/rate cap of one reply per 500 ms per peer
is not driven.

## Results per engine (Listen / Talk)

| | Playwright Chromium | Playwright Firefox | Playwright WebKit |
|---|---|---|---|
| connect, click to `connected` (page clock) | 84 to 121 ms | 91 to 116 ms | 379 to 638 ms (first connect), 126 to 240 ms (later) |
| click to "Listening on module X" text | 165 to 195 ms | 189 to 224 ms | 1775 to 2295 ms (first connect) |
| received audio analysed in the page | yes: 700 Hz, amplitude 0.2435 (sent 8000/32768 = 0.2441), tone fraction 0.998 | **no**: the AudioContext stays `suspended` (no audio device in the container, autoplay) and the tap reads nothing; packets, codec and rate are judged from `getStats`; the frequency is not | yes: 700 Hz, amplitude 0.2437, tone fraction 0.9999 (the tap sometimes drops buffers: best of three windows) |
| packet interval / loss / jitter (`getStats`) | 20.0 ms, 0, 0 s | 20.0 ms, 0, 0 s | 20.0 ms, 0, 0 to 0.007 s |
| fake microphone | the 700 Hz file: arrives as 700 Hz (peak within 20 Hz), tone fraction about 0.17 | its built-in fake source: a clean tone at 1000 Hz (fraction 1.0) | its mock device: not a tone (peak 500 Hz, fraction 0.01), a gap of up to 191 ms |
| Hold Talk to "On air" | 0.22 to 0.33 s | 0.13 to 0.25 s | 0.41 to 0.52 s |
| recordings playback | passes | start works, then the engine reports `OnMediaSinkAudioError` (no audio device) | passes |

Connection times are page-side: `performance.now()` at the click event of the page to the `connectionstatechange` `connected`, on
the rig network (no NAT, host candidate, ICE-lite edge). The criteria of DV-Router `testing.md` (connected and first audio within
10 s, p99 of the inter-arrival deviation) are met by a wide margin in this setup; they are NAT-stage numbers, not these.

Fake microphone tone: the page asks for `getUserMedia({audio: true})`, so echo cancellation, noise suppression and automatic gain
control stay on; they treat a pure tone as noise and Chromium's output arrives at a tone fraction of about 0.17 (dominant
frequency still 700 Hz, 200 frames in 4.2 s with a largest gap of 28 ms). The case therefore judges the frequency and the frame
count, not the purity.

## Reconnect (record only)

ICE restart and automatic reconnect are not built (DV-Router unit ICE1); nothing here asserts a reconnect.

| Step | Observed (all three engines) |
|------|------------------------------|
| Disconnect, then Listen / Connect | a new offer on a new circuit (two distinct `sess_id`); the old circuit answers `/rtc/alive` with 403; second connect 89 to 215 ms |
| reload of a connected page | `pagehide` revokes the circuit (keepalive `/rtc/close`); the new page shows "Idle", the old circuit answers 403, a click is needed (91 to 142 ms) |
| `/rtc/alive` blocked in the browser | the page gives up after 21.5 to 23.3 s (25 s from the last renewal, minus the time between): "Connection ownership expired. Connect again manually.", the peer is `closed`, the page sends `/rtc/close`; the server then answers the old circuit with 403; a click connects again (103 to 240 ms) |
| ICE or media loss | not tried: network blackholes belong to the NAT stage (no NET_ADMIN here) |

## Recorder (K14b)

Seeding: three real overs sent by USRP dynamic clients into modules S and T (2.0 s 700 Hz and 1.0 s 1000 Hz by two callsigns, 0.8 s
700 Hz); each appeared in the list within seconds as one row (`profile pcm-s16le-8k`, `protocol usrp`, `reason terminator`,
`lost_units 0`). Vocoder profiles (`ambe-dstar`, `ambe2-dmr`, `codec2-3200`, `imbe`) need the decoder worker: K14c, with the fake AMBEd;
not covered.

| Case | Result |
|------|--------|
| notice | `public/state` `info.recording_enabled` true and `recording_keep_days` 7; dashboard callout "Recording is enabled. Room transmissions may be retained." and "Recordings are kept for 7 days." and a `Recordings` link (`./recordings/`) for a signed-in user |
| list visibility | anonymous 401; a user without `may_tx` and an operator get the identical ids; exactly the eleven documented fields; `duration_ms = decoded_bytes / 16`; filters `room`, `callsign` (case-insensitive), `profile`, `date_from_ms` / `date_to_ms`; `limit=1` paging with `cursor` reaches the same rows, oldest first; 400 for `limit=0`, `limit=101`, `room=a`, `cursor=zz`, `date_from_ms=-1`, `profile=nope`; detail route returns the same row, unknown id 404, malformed id 400 |
| audio | 200 `audio/wav`, `Accept-Ranges: bytes`, `Cache-Control: no-store`, `Content-Disposition: inline`, `Cross-Origin-Resource-Policy: same-origin`, no ETag, no Last-Modified, no Vary; 44-byte RIFF header (PCM, mono, 8000 Hz, 16 bit), total `44 + decoded_bytes`; the tone of each over is found at the sent frequency (700 / 1000 Hz); HEAD gives the length and no body |
| Range | `bytes=0-1` 206 `bytes 0-1/32044` (2 bytes); `0-`, `0-43`, `44-`, a middle slice, `total-2-`, suffix `-2`, `-100000000` (whole file), an end past EOF (clamped) all 206 with matching `Content-Range`, `Content-Length` and bytes; `bytes=<total>-`, a start past EOF, `100-99` (reversed) and `-0` give 416 with `Content-Range: bytes */<total>` and an empty body; a multi-range, `bytes=example` and `items=0-1` give 200 with the whole body; `If-Range` (stale validator), `If-None-Match: *` and a future `If-Modified-Since` are ignored (206 / 200, never 304); HEAD with a Range gives 200 with the full length |
| guards | `Sec-Fetch-Site: none` or `cross-site` 403, foreign `Origin` 403, anonymous 401, another User-Agent 401 on audio and on the list without ending the session, an `AppleCoreMedia/` User-Agent accepted on audio; 20 requests in 10 s: the 21st is 429 `Retry-After: 1` |
| page | `/recordings/` lists the rows of the day (module, callsign, protocol, the end-reason LABEL, lost units equal the API; duration "2s"; since I18N1 the page maps the fixed end-reason vocabulary to labels, e.g. `terminator` is shown as "Transmission ended": dv-router ruling F8 2026-11-04, INTENDED; the API keeps the raw `terminator`; map and unit test in `dvtest/browser.py` `END_REASON_LABELS`, any other value is shown raw), Play sets the shared `<audio>`: Chromium and WebKit reach `currentTime` 2 s with no error (Chromium requested `bytes=0-`, 206, once; WebKit's media request shows no readable status); the playback audit row is written; Firefox: `currentTime` advances to the end and the engine then raises `OnMediaSinkAudioError` (no sound card), end-of-clip and seek not judged for it |

## Findings 3b (observed vs documented)

Each is a `FINDING:` line in `out/<run>/web-findings.txt`; none of them is asserted except where stated.

| # | Area | Observed | Documented / expected |
|---|------|----------|-----------------------|
| 8 | Talk | **Hold Talk ends by itself after about 4 s**: the status goes from "On air" to "Ready" 4.2 to 4.4 s after "On air" while the button is still pressed (timeline of 0.5 s samples in `webrtc-notes.jsonl`); the router heard 200 frames (4.0 s) of a 6 s hold. Cause in `public.js`: the 4 s watchdog `armWatchdog` is armed at `down` and at `granted` / `pending` feedback, and the edge sends no further feedback while on air (the edge does not acknowledge `lease`) | web.md: "Page watchdog 4 s from `down`, reset by granted / pending feedback"; a held button is to transmit until released or the TOT |
| 9 | Talk | an ordinary user with `may_tx` talks after the first press with no step-up and no TOTP; only operators have TOTP, and the page hides Talk while an operator is still enrolling | web.md: step-up (`X-DV-TOTP`) is for the radio-ID POSTs only; the stage brief assumed one for talking |
| 10 | Talk | after the page's own release at 4 s the status reads "Ready"; "Ended: Button released." is not seen in the 6 s hold cases (a release inside the 4 s window was not timed) | texts of `talkStatus` |
| 11 | Talk | a raw `ptt down` from a recvonly peer of a user without `may_tx` is answered `{"op":"ptt","state":"denied","code":"denied"}`; the first frame on that channel is `{"op":"selected","room":"L"}` | web.md names `denied` for authorization; not stated for a recvonly peer |
| 12 | Talk | floor taken by a USRP talker: "Denied: The floor is busy." (`busy`) after 0.22 to 0.4 s; the SUT edge counts `Deny(Busy)` and a large `XStale` counter (uplink frames of the denied press: about 50 per second while the button is held) | `busy` / `held` both listed; which one a lost race gets is not stated |
| 13 | TX identity | the public state shows the web talker in `rooms[].onair` with `callsign`, `callsign_base`, `via`, and in `clients.items[]`; the USRP listener is sent no SET_INFO with the talker's callsign | web.md: account callsign, "never verified" |
| 14 | Listen | the lease: first `POST /rtc/alive` at connect, then every 10 s (204 each); a closed, reloaded or lease-expired circuit answers `/rtc/alive` with **403** | web.md: 403 for an unknown circuit, 401 for a retained circuit whose lease expired; after 36 s the circuit is gone, not retained |
| 15 | Listen | the answer offers one host candidate `<net>.11:50000` and an `application` (SCTP) section; `a=ice-lite`, `a=setup:passive` | as documented (RIG1) |
| 16 | Listen | Playwright WebKit: first connect `connected` after 304 to 638 ms but the page says "Listening" only after 1.8 to 2.3 s (later connects 130 to 240 ms) | no figure documented |
| 17 | Recorder | `protocol` is `usrp`, `reason` is `terminator` for a PCM over | not enumerated in the docs |
| 18 | Recorder | the same fragment defect as finding 1 is in `recordings.js` (`html\`<>\``): the page stays "Loading recordings..." in all three engines as served; the cases serve a patched script (`dvtest.browser.patch_fragments_js`) | DASH1 |
| 19 | Recorder | signed out, the page shows "Sign in to browse recordings." and a Sign in link | texts of `errorText` |
| 20 | Recorder | Playwright WebKit reports the CSP style violation of finding 3 on the recordings page too | CSP with no violation in normal use |
| 21 | Rig | the browser container has no audio device: Firefox raises `OnMediaSinkAudioError` after the clock ran; its Web Audio context stays `suspended` (`media.cubeb.force_null_context` and the autoplay prefs did not change that) | not a product matter |

### Status of the 3b findings on 648e539

| # | Status |
|---|--------|
| 8 | **CLOSED by TALK1**: the over lasts the whole hold (6 s and 10 s) on all three engines; acks as above |
| 10 | **CLOSED**: "Ended: Button released." is seen after every hold (2, 6, 10 s) |
| 17 | **explained (DOC6, recorder.md "PCM talk metadata")**: the `usrp` / `terminator` rows were the USRP overs this run sent into rooms S and T; a browser Talk over is recorded as `protocol webrtc`, reason `terminator` (`test_browser_talk_is_recorded_as_webrtc`, room U: one row, `pcm-s16le-8k`, passes on all three engines); the seeded-rows case now asserts `usrp` for its own overs only |
| 18 | **CLOSED by DASH1**: `recordings.js` renders as served; `/recordings/` lists the rows, no patch |
| 20 | WebKit CSP `style-src-attr` on the recordings page: still open with finding 3 |
| 9, 11, 12, 13, 14, 15, 16, 19, 21 | unchanged |

## Browser connection loss in a long session

In the combined 3a+3b session (`out/20261101T203217Z`, 21 min) four recorder setups failed with `Browser closed` / "Provisional navigation
canceled" (one per engine and one more), and in the NAT run without TURN (`out/20261101T211249Z`, 26 min) the three WebKit
404 cases did (`Target page, context or browser has been closed`); each passes when rerun alone (`out/20261101T205603Z`, and
`-k "404 and webkit"`: 3 passed). The service log shows no restart and the container's memory peaked at about 510 MiB of 3 GiB in a
short run; the cause (a browser process dying inside the shared `playwright run-server`) is not established. Mitigation:
`engine_browsers` reconnects when its cached browser is no longer connected (`is_connected()`), and the stages are run as separate
sessions as documented. Not hidden: a case that loses its browser still fails.

## Gaps

| What | Why |
|------|-----|
| an operator talking | needs a TOTP-enrolled operator session in the browser plus the step-up window; not tried |
| Hold Talk released inside the 4 s window; Space / Enter keys | the cases use pointer events only; the release text for a normal release is therefore not asserted |
| Listen frequency in Firefox | no audio device and a suspended AudioContext in the container; `getStats` is judged, the frequency is not |
| WebKit fake microphone | its mock device is not a tone: frame count and level are judged, not the frequency |
| TURN (`/rtc/ice` 200), NAT, ICE restart, blackholes | NAT stage 4 and unit ICE1 |
| AMBE and other vocoder recordings, seeking in them | K14c (the fake AMBEd) |
| Safari, iOS | Playwright WebKit is not Safari; the Safari-specific audio route of web.md is a W8 release test |
| module switch on the control channel (`select`) | not driven; connect picks the module |

## Rerun (stage 3b)

```sh
DVT_USRP_DYNAMIC=1 XLXD_SUT_IMAGE=dv-router:wip-648e539 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/recorder.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_webrtc.py cases/test_web_recorder.py
```

About 13 minutes (the 36 s lease cases and the 25 s lease-expiry cases, three engines each). `-k listen`, `-k talk`, `-k reconnect`,
`-k recordings` select parts; the recorder cases skip without `recorder.toml`, all WebRTC cases that talk skip without
`DVT_USRP_DYNAMIC=1`. Measurements and observations are also in `out/<run>/web/webrtc-notes.jsonl`.

# CSP probe (finding 3: WebKit `style-src-attr`)

`cases/test_web_csp_probe.py` (3 cases, 9 runs) with `cases/csp_probe_support.py` (the in-page recorder `PROBE_JS` and the pure readers;
unit tests `cases/unit/test_csp_probe_support.py`, which also runs `node --check` on the recorder when node is installed). **NOT RUN on a
rig yet.** It is evidence for the product team, not a verdict: it never fails on a violation. It fails only when a page that must load
cannot be loaded (no response, HTTP 5xx, a navigation error) or when the recorder is missing from a document (the evidence would be void).

Why: finding 3 above says Playwright WebKit reports `style-src-attr` on every page that renders although DASH1 removed every `style=` of
`web/site`. The probe answers, per page and per engine: which directive governs a style attribute in the served CSP (`style-src-attr`, else
`style-src`, else `default-src`, from the response header of the document), which exact events fire, which script wrote the inline style (element,
value, first 8 stack frames), which elements carry a `style` attribute when it fires and at the end, and whether Chromium and Firefox report
anything at all.

| Case | Pages (each loaded fresh through `about:blank`) |
|------|-------|
| `test_csp_probe_anonymous` | `/login.html`, `/register.html`, `/totp.html` (optional), the eight dashboard routes `#/ #/map #/positions #/heard #/modules #/nodes #/peers #/about` (on the Map a room tile is clicked twice), `/ops/` (the denial view), the admin host sign-in (optional, only when the admin host is reachable) |
| `test_csp_probe_user` | the eight dashboard routes signed in (Listen panel, message composer), the signed-in login page, `/recordings/` (optional: needs a recorder), `/ops/` as a user (denial) |
| `test_csp_probe_operator` | the six `/ops/` pages, the signed-in login page, the dashboard |

What the recorder keeps (installed with `context.add_init_script`, so it runs before any script of the page; every wrapper calls the original):
`securitypolicyviolation` events on `document` (violatedDirective, effectiveDirective, blockedURI, sourceFile, lineNumber, columnNumber, sample,
disposition, originalPolicy, the event target and 3 parents; repeated events with the same key only count up); every write of an inline
style it can see: `setAttribute` / `setAttributeNS` / `setAttributeNode(NS)` with the name `style`, the `style` property setter, the
CSSStyleDeclaration setters (`cssText` and each property, `setProperty`; the owning element when the declaration came from `el.style`), markup
with `style=` given to `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `createContextualFragment` / `DOMParser`, and a MutationObserver on the
`style` attribute (no stack); each with tag, id, class, 3 parents, value and a stack; `document.querySelectorAll('[style]')` (tag, id, class,
style attribute, outerHTML first 200 characters) at the first occurrence of each distinct violation and at the end.

Output: `out/<run>/web/csp-probe-<engine>.json` (one file per engine, rewritten after each case: `pages[]` with `status`, `csp_header`,
`style_governor`, `inline_allowed`, `document_responses`, `violations[]`, `writes[]`, `dumps[]`, `console_csp`; `summary` with the events per
directive and the distinct (directive, source) pairs), one `[RECORD] csp-probe ...` line per page on stdout, and one `web.finding` line per distinct
(page, engine, directive, `sourceFile:line:col`) tuple (with the last style write seen before it). For Chromium and Firefox a line
`NO CSP violation fired on any of the N page(s)` is printed when none did.

Reading it: the served policy is `default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'` (finding above), so a `style=`
attribute and `setAttribute('style', ...)` are blocked by `default-src` through the fallback chain, while CSSOM writes (`el.style.x = ...`,
`cssText`) are allowed by CSP3; an engine that reports `style-src-attr` for a CSSOM write is the interesting result, and the `writes[]` stack
names the caller. First suspect (not established): the vendored `htm-preact-3.1.1.module.js` writes `e.style.cssText = ""` / `cssText = string` for a
`style` prop; the page sources of `dv-router:wip-1bf347c` contain no `style=` and no `.style` write of their own apart from MapLibre's
canvas sizing on `#/positions`. The probe's own wrappers sit in the stacks only as dropped frames.

Run (about 6 to 10 minutes; the user and operator cases register one account each, which waits for the register budget):

```sh
XLXD_SUT_IMAGE=dv-router:wip-1bf347c DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
  DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_csp_probe.py
```

`-k anonymous`, `-k chromium` select parts. `admin.toml` makes the admin sign-in screen reachable; without it that page is skipped with a line.

# TWIN2b map fade

`cases/test_web_map_fade.py` (3 cases, 9 runs), `cases/map_fade_support.py` (the in-page sampler and the pure judges), unit tests
`cases/unit/test_map_fade_support.py` (the judges against a model of the page built from its sources). **NOT RUN on a rig yet.**

Spec (product owner), on the Map room view (`#/map`) for one over and its end: at t ~ 0 (during the over) the room shows the on-air look and the
talker; after the end, over 5 s, the room fades from `rooms[].last.end_ms` (public state) with the caption "<call> finished" (call = the talker
callsign); at ~2.5 s after the end it is in a fade step (a lower-opacity class and/or a computed opacity strictly between full and rest) and the
caption says "<call> finished"; at >= 6.5 s after the end the room is at rest (the documented sampling lag is up to 1.25 s); a NEW over during the
fade restores the full on-air look at once (within one sampling step); with prefers-reduced-motion the fade shows a static dimmed state with no CSS
transition (computed transition-duration 0s).

| Case | What it does and asserts |
|------|--------------------------|
| `test_map_room_fades_after_an_over` | a 3 s USRP over from `N0FAD` in room N, then 8.5 s of sampling every 250 ms after `rooms[N].last.end_ms` (read from the public state, tester clock corrected by the skew against `now_ms`). `judge_fade`: >= 2 samples of the on-air look (data-state `onair`, no fade class) during the over with the caption "Room N: N0FAD talking from" and the callsign drawn on the tile; the public state names the talker while it talks; the first fade sample within 0 .. 1.5 s of the end, every fade sample has the caption "Room N: N0FAD finished."; the sample nearest 2.5 s is in a fade step 1 or 2 with a witness (class step >= 1, or art opacity strictly between 0 and 1, or fill strictly between the on-air and the rest colour); step never goes back, art opacity never rises, fill never returns toward on-air; the fade lasts about 5 s; every sample from 6.5 s is at rest (data-state `idle`, no fading element, caption without "finished" and with "No one is on air.") |
| `test_map_new_over_during_the_fade_restores_the_look` | a 1.5 s over; the moment a sample shows fade step >= 1 the same client sends a 3 s over. `judge_restore`: the on-air look (no fade class, talking caption) is back within 1.6 s of the first frame (stream latency, the 250 ms publication gate and one sample), and from that sample on every sample is the full look (no fade class, no fading art element, talker text opacity >= 0.99, fill within 0.05 of the on-air colour): no dimmed intermediate. Then `judge_fade` runs on the second over's end |
| `test_map_fade_under_reduced_motion` | `page.emulate_media(reduced_motion="reduce")` before the first navigation, a 2 s over. `judge_reduced`: the page sees the media query; every fade sample has computed art opacity 0.45 (+-0.02) at every step, the room fill constant (spread <= 0.06) and between the on-air and the rest colour, computed `transition-duration` all `0s` on the room tile and on the fading elements, the caption "<call> finished"; at rest from 6.5 s |

Room N (an own module: the other browser cases use H J K L Q W R X Y Z M S T U and B C). Before each over the case waits up to 25 s for a quiet Map
(no room `onair`, no fading element); otherwise it fails with that reason. Evidence: `out/<run>/web/map-fade-<engine>-<timeline|restart|reduced>.json`
(every sample with `t` since the end and, for the restart case, `u` since the second over's first frame: data-state, class list, computed fill,
art opacity min / max, transition durations, caption, tile text, plus the judges' metrics and failures) and `[RECORD] map-fade-series` lines
(one per sample) and `[RECORD] map-fade ...` summary lines on stdout.

How the page is built (read from `web/site/map-layout.js`, `map-building.js`, `map.js`, `shared/dv.css` of `dv-router:wip-1bf347c`, read only):
`fading = !onair && controller != down && at >= end_ms && at < end_ms + 5000` with `at` on the server clock; the fade step is
`floor((at - end_ms) / 1000)`, 0 to 4; the room keeps `data-state=onair` while fading and gets the classes `twin-fading twin-fade-N`;
`.map-building .twin-fading` has `opacity: var(--twin-fade-level)` (1, .75, .5, .25, 0) and `transition: opacity 1s linear`; the room tile
itself has `opacity: 1` and fades its FILL (`color-mix(--twin-room-lit, --twin-room)`, also 1 s linear); under `prefers-reduced-motion: reduce`
`.twin-fading` is `opacity: .45; transition: none` and the tile fill is the static `.45` mix. The page redraws on a 1 s tick behind a 250 ms
publication gate (`coalesceUpdates`), which is the "sampling lag up to 1.25 s". The caption line is `Room R: <call> finished.`; at rest `No one is on air.`.

Run (about 12 minutes: nine runs of about 15 to 20 s each plus the setup; needs a USRP dynamic hub):

```sh
DVT_USRP_DYNAMIC=1 XLXD_SUT_IMAGE=dv-router:wip-1bf347c DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
  DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_map_fade.py
```

Without `DVT_USRP_DYNAMIC=1` every case skips with the reason (`usrp_client`). `-k timeline` / `-k restart` / `-k reduced` and `-k chromium` select parts.

## UNVERIFIED (TWIN2b)

Everything below was derived from the page sources, never from a running page or a run of these cases.

1. Selectors: `data-testid` `map-page`, `map-building`, `map-room` (with `data-id` = room letter and `data-state` `onair` / `idle` / `down`), `map-caption` are WEB2-style hooks seen in `map.js` / `map-building.js`. The class names `twin-fading`, `twin-fade-0..4`, `twin-room-call` (the callsign `<text>`), `twin-lamp` and the container `.map-building` have NO hook (marked `# UNVERIFIED selector` in `cases/web_pages.py`); the judges use computed style first.
2. The room tile's own computed `opacity` stays 1 while it fades (CSS `.map-room.is-onair.twin-fading`); the case therefore reads the opacity of the fading ART elements (`.map-building .twin-fading` that are not a room tile) and the tile's computed `fill`. If the product moves the fade to the tile's opacity the opacity witness is empty and only the class step and the fill remain.
3. The fill colours: computed `fill` of the tile is parsed as `rgb()` or `color(srgb ...)`; an engine that returns another colour space (oklab, lab) makes the fill witness `None` (the class and opacity witnesses still judge). `lit` and `rest` references are taken from the same run (last on-air sample, last rest sample).
4. `rooms[N].onair.callsign_base` is the USRP SET_INFO callsign `N0FAD` and `rooms[N].last.end_ms` is set at the unkey; `read_end` accepts the first `end_ms` newer than the over's start. If the hub holds the floor after the unkey (a hang time), `end_ms` and the page move together and nothing changes, but the "t ~ 0" window shifts.
5. The callsign on the tile is cut to the tile width (`N0FA.`): `tile_call_matches` accepts a truncated text of at least 3 characters; the full callsign is judged in the caption.
6. Timing: step at 2.5 s must be 1 or 2 (2.5 s minus up to 1.25 s of lag); the first fade sample within 0 .. 1.5 s of the end; rest not before 4.7 s and from 6.5 s. The skew between the tester clock and the router clock (`now_ms` of the state) is measured per run and recorded; browser, tester and router share the host clock on the rig. The page reads time from the server clock (`clock.sync(now_ms)`), the cases from `end_ms`, so a drifting clock in the browser container does not matter, a stale offset in the page would show as a shifted fade.
7. The restart case sends the second over from the same USRP client 0.2 s .. one sample after the page showed step >= 1 and expects the router to accept it (a new stream on the same module); a refusal appears as "no sample shows the on-air look again".
8. A hidden or paused page (`document.hidden`) freezes the map (`map-paused`); headless Playwright pages are visible, the sampler records `hidden`.
9. Playwright WebKit and Firefox may not apply `emulate_media(reduced_motion=...)` the same way; `reduced` is read back from `matchMedia` in every sample and a mismatch fails the case with that reason.

# TWIN1d Thai screenshot matrix and map-status

`cases/test_web_map_twin1d.py` (3 test functions, 18 runs), `cases/map_matrix_support.py` (the in-page measurement, the recorder and the pure
judges), unit tests `cases/unit/test_map_matrix_support.py` (28 cases, offline). **NOT RUN on a rig yet.** Selectors only in `cases/web_pages.py`
("TWIN1d").

Spec (product owner): decision section 3, the "Thai screenshot matrix": scenarios `map-busy`, `map-quiet`, `map26`, `map-hub-down` x language EN/TH x
colour scheme light/dark x viewport width 360 / 1000 / 1440 px (4 x 2 x 2 x 3 = 48 combinations per engine). TWIN1d (next image, NOT in
`dv-router:wip-efd9eca`): a polite status region `data-testid="map-status"` (`role=status`, `aria-live=polite`, `sr-only`) on `#/map` that announces
over start / over end and desk open / close, at most once per 2 s, silent on first render, refetch, timers and the fade.

| Case | What it does and asserts |
|------|--------------------------|
| `test_map_thai_screenshot_matrix[<scenario>-<engine>]` (4 scenarios x 3 engines) | one new browser context, the page visited once per language (the page's own switch `language-en` / `language-th`, which stores `dv-lang` and reloads; the storage key is the fallback when the hook is missing); colour scheme (`emulate_media(color_scheme=...)`) and viewport (`set_viewport_size`) changed in place; per combination: settle (the SVG viewBox and width stable), one measurement, one full-page screenshot, `judge_measure`. All failures are collected and asserted once, each prefixed `scenario/lang/scheme/width` |
| `test_map_closed_desk_sign_thai[<engine>]` (3 runs) | SIGSTOP of `dv-edge-xlx`, poll until `live.edges` drops `xlx`, then the 12 combinations on a quiet Map with a closed desk required and the sign text judged (EN `Closed`, TH the Thai string of the key); CONT, wait for the edge and the state to be back (details below) |
| `test_map_status_announces_over_start_and_end[<engine>]` | a `MutationObserver` injected with `context.add_init_script` (before the page scripts; it also waits for the region to appear) records every change of the region's `textContent`; one 6 s over from a USRP client in room I. `judge_status_log`: first sight empty and no change before the over; exactly one change within 3.5 s of the first voice packet; none during steady voice; exactly one within -0.3 .. +4 s of the unkey; none afterwards to the end of the fade (7.5 s after the unkey); consecutive changes >= 1.9 s apart. `judge_status_facts` (before, during, after): exactly one element, `role=status`, `aria-live=polite`, not `display:none` / `visibility:hidden` / `aria-hidden` (self or ancestor), class `sr-only`. SKIPS with the reason when the page has no `map-status` (probed once, after the Map is drawn); the matrix does not skip, it records a finding per scenario and engine |

The three checks of the matrix (defined once, in `cases/map_matrix_support.py`, header and `judge_measure`):

1. No horizontal overflow: `document.scrollingElement.scrollWidth <= clientWidth`.
2. No clipped caption: (a) every visible HTML text element under `<main>` and the heading block that is not `display:inline` has `scrollWidth <=
   clientWidth + 1` (nothing cut off, no ellipsis truncation, no spill into a neighbour), and no visible HTML element sticks out of the viewport;
   (b) every visible SVG `<text>` of the building (room sign, count, callsign, desk label and count, service sign, roof sign, closed sign) lies inside
   the shape it belongs to (the smallest of room / desk / service box / roof sign that contains its centre; the closed sign: its own rectangle) and
   inside the drawing, 1 px of slack. "Visible": displayed, not `visibility:hidden`, opacity > 0, larger than 1.5 px (so `sr-only` is not measured).
3. Closed desk sign: EN shows `Closed`, TH shows the Thai string of the key `Closed` of `shared/i18n/public-th.js` (`CLOSED_SIGN_TEXT` in
   `web_pages.py`, written as `\u` escapes) and not the English one. Also: the colour scheme and `html lang` were applied, and the TH caption contains
   Thai text and the EN one does not.

Also asserted per scenario: `map-busy` at least 3 rooms `data-state=onair` (five USRP clients in rooms F G I O P keying a tone for the whole run),
`map-quiet` none, `map-hub-down` the `map-hub-down` callout on the page. No page error (a finding for `map-hub-down`).

How the scenarios are made (the product definition, `web/README.md` of the web assets, is not in the image, so these are by traffic seeding):

| Scenario | Seeding |
|----------|---------|
| `map-busy` | five rooms on air at once from USRP dynamic clients (`N0MBF` F, `N0MBG` G, `N0MBI` I, `N0MBO` O, `N0MBP` P), one `OverLoop` thread each, until the scenario ends |
| `map-quiet` | nothing; the case waits up to 25 s for no room on air and nothing fading |
| `map26` | the template serves modules A-Z, so 26 rooms are drawn (`MODULE_LIMIT` 26 in `map-layout.js`); one talker in room V. A Map that draws another number of rooms is a finding, the scenario goes on with what the rig serves |
| `map-hub-down` | per language, after the language was switched with the hub up: `kill_proc("xlxd-sut", "dv-svc-status", "STOP")` (SIGSTOP; DV-Router: the public state becomes unavailable, the page shows the hub-down banner and dims the building); the case waits 25 s for the `map-hub-down` callout, else reloads once and waits 40 s; then `CONT` (twice) and the public state must answer 200 again (150 s) before the next language and before the test ends |

The Closed sign has its own test, `test_map_closed_desk_sign_thai[<engine>]` (scenario name `map-closed-desk`, quiet Map, the same 12 combinations,
the sign text judged and a closed desk required): DV-Router's answer is that a closed desk is "edge enabled but not in `live.edges`" (web.md, Map), so
the case sends `STOP` to `dv-edge-xlx` through `kill_proc("xlxd-sut", "dv-edge-xlx", "STOP")` (KILL is too short: the launcher restarts a killed child
within 500 ms), polls the public state until `live.edges` no longer lists `xlx` (90 s), runs the combinations, sends `CONT` and waits until `xlx` is listed
again (150 s) and the state answers. It skips with a reason when `xlx` is not in `live.edges` before the stop or no `dv-edge-xlx` process exists.
Off by default and NOT evidence: `DVT_MAP_ROUTE_FALLBACK=1` skips the STOP and rewrites the state in the browser (a page route removes the last up edge that
is not `usrp` / `webrtc-*` from `live.edges`), for screenshots only; the JSON says `route_fallback: true`.

STOP / CONT rules (both stoppable processes, `dvtest/control.py` `KILL_PROC_ALLOWED`: `dv-edge-xlx` KILL USR1 STOP CONT, `dv-svc-status` KILL STOP CONT;
the host side `scripts/lib/killproc.sh` must carry the same, dv-testset 5e84038): every STOP is armed first (`stop_process` records the control channel and
registers `atexit`), every case sends `CONT` twice in `finally`, a module-scope autouse fixture sends `CONT` twice to both processes at the end of the
module even after a crash, and `atexit` does it once more; each case waits for the recovery before it ends. Only `map-hub-down` and
`map-closed-desk` ever see a stopped process; both skip without `DVT_RIG_CONTROL`.

Evidence: `out/<run>/web/map-matrix-<engine>-<scenario>.json` (per combination: layout, mode, counts, closed desks and sign texts, failures, notes),
`out/<run>/web/map-matrix/<engine>/<scenario>-<lang>-<scheme>-<width>.png` (full-page screenshots, local only, NEVER committed; the engine directory keeps
the three engines apart), `out/<run>/web/map-status-<engine>.json` (every change with its time since the first voice packet, the region facts three
times, the clock offset) and `[RECORD]` lines. The notes of the status case (same text written again, end text equal to start text) are findings.

Run (same profile as the TWIN2b fade case; `DVT_USRP_DYNAMIC=1` for the talkers; `DVT_RIG_CONTROL=1` is set by `scripts/run.sh` and the hub-down
scenario skips without it; `--capture` is not needed):

```sh
DVT_USRP_DYNAMIC=1 XLXD_SUT_IMAGE=<next image with TWIN1d> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
  DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml \
  scripts/run.sh -m rig --profile web,browser -- cases/test_web_map_twin1d.py
```

`-k map-busy` / `-k map-quiet` / `-k map26` / `-k hub-down` / `-k closed` / `-k status` and `-k chromium` select parts; `-k "not hub-down"` leaves out the case that
stops the hub (`-k "not hub-down and not closed"` leaves out both cases that SIGSTOP a process; do not run the file beside `cases/test_web_k22.py`). On an older image (for example
`dv-router:wip-efd9eca`) the matrix runs and records "no map-status" as a finding; the status case skips.

Estimated duration (nothing was run): per engine matrix `map-quiet` about 1 minute (25 s quiet wait at most, 12 combinations of about 2 s, two page
loads), `map-busy` and `map26` about 1.5 minutes, `map-hub-down` 3 to 6 minutes (notice 25 s or 25 + 40 s per language, recovery up to 150 s
each), `map-closed-desk` 2 to 5 minutes (drop of the edge up to 90 s, return up to 150 s), the status case about 40 s: about 9 to 13 minutes per engine, 28 to 40 minutes for the three engines. Cap: a seeded over stops after 100 s.

## UNVERIFIED (TWIN1d)

Everything below was derived from the page sources of `dv-router:wip-efd9eca` (read only: `docker create` + `docker cp`), never from a running page or a run.

1. Scenario meanings: `web/README.md` is not in the image (the web assets hold `admin`, `shared`, `site` only), no URL query, hash or rig hook selecting
   `map-busy`, `map-quiet`, `map26` or `map-hub-down` exists in `map.js`, `map-layout.js` or `public.js`. The four scenarios are the case's reading
   of the names (see the table above); `map26` is taken as "26 modules drawn" (`MODULE_LIMIT`), with the one talker an addition of the case. Replace them
   with the README definitions when the owner sends them.
2. `map-status`: the testid, `role=status`, `aria-live=polite` and the `sr-only` class are the owner's TWIN1d spec; nothing of it is in `wip-efd9eca`
   (`# UNVERIFIED selector` in `web_pages.py`). Where in the DOM it lives (inside the Map view or the page shell) is not assumed: the case probes after the
   caption is drawn. The text of the announcements is not asserted (only that start and end are non-empty); a changed text is what counts. "At most once per
   2 s" is judged as consecutive changes >= 1.9 s apart. Desk open / close announcements are NOT driven by any case here.
3. The windows around the over (start within 3.5 s of the first voice packet, end -0.3 .. +4 s around the unkey, quiet afterwards) are the case's
   reading of "once at start, once at end, silent in the fade"; the unkey time is the scheduled end of the paced frames (6.0 s after the first
   frame), the browser clock is aligned to the tester clock by one `Date.now()` read (same host on the rig).
4. How long until the banner shows after `STOP` of `dv-svc-status` is not known: whether the web service answers `GET /api/v1/public/state` with an error
   at once or serves a cache, and whether the event stream closes, so that the page sets `hubDown` (`err` or `mode === 'closed'` in `public.js`). The
   case records `noticed` (`live` or `reload`) and the seconds in `map-matrix-<engine>-map-hub-down.json` (`hub`) and fails with that reason if the
   callout never shows within 25 s (+ 40 s after one reload). A page reloaded under the stopped hub draws the empty "current state unavailable" callout,
   not the stale building ("Showing the state as of ..."). How long until the public state answers again after `CONT` is recorded (`state_back`), limit 150 s.
   Whether the launcher or a watchdog restarts or kills a stopped `dv-svc-status` / `dv-edge-xlx` is also unknown (the case sends one STOP and relies on
   the process staying stopped).
5. The language switch testids `language-en` / `language-th` and the key `dv-lang` are read from `shared/i18n-ui.js` / `shared/i18n.js`; the switch is
   expected to reload the page (it does when localStorage works). The default colour theme is `auto` (follows `prefers-color-scheme`), so
   `emulate_media(color_scheme=...)` is enough; a stored `dv-theme` would override it (a fresh context stores none).
6. The container of each SVG text is found by geometry (the smallest room / desk / service rectangle containing the text centre); a text whose centre
   lies in no rectangle is judged against the drawing only. Classes without a hook (`twin-roof-sign`, `twin-sign-text`, `.content-header`) are marked
   `# UNVERIFIED selector`. A scrollable wrapper with intended horizontal scrolling would be reported as a clipped caption.
7. How long until the hub drops the SIGSTOPped `dv-edge-xlx` from `live.edges` (limit in the case: 90 s) and until it lists it again after `CONT` (150 s)
   is not known; both are recorded as `timing.edge_dropped_s` / `edge_back_s` in `map-matrix-<engine>-map-closed-desk.json`. The edge name in
   `live.edges` is assumed to be `xlx` (an optional `-<16 hex>` suffix is cut, as `map-layout.js` does); a rig whose `xlx` edge is not up skips the case.
   The fallback route (`close_one_edge`) assumes `info.edges_enabled` and `live.edges` as in `projectEdges` and is not evidence.
8. Several USRP dynamic clients keying at once from one tester address (five in `map-busy`) is assumed to work like the single client of the fade case;
   a hub that refuses it fails the scenario with "seeding failed: N rooms show data-state=onair".
9. Timing is estimated, not measured; the 25 minute budget per engine has a wide margin only if the hub-down detection is quick.

# Talk echo return

`cases/test_web_talk_echo_return.py` (1 case, 3 runs: Playwright Chromium, Firefox and Playwright WebKit; the fake microphone setup of `cases/browser_fixtures.py` exists for all three), helpers and the pure judges in `cases/talk_echo_support.py`, unit tests `cases/unit/test_talk_echo_support.py` (46 tests; they also run the in-page recorder in node against a fake `RTCPeerConnection` / `AudioContext` when node is installed). **NOT RUN on a rig yet.**

Why: a PRODUCT FINDING on production image `wip-97c669d` (not on `wip-efd9eca`): a signed-in user in headless Chromium with a 700 Hz fake microphone listens on module E (the built-in ECHO), holds Talk 3 s and releases. Login, listen, "On air", the public state `onair E`, the echo replay (`units_published` rises) and "Ended: Button released." all happen, but the page's analyser sees no sample above rms 0.01 for 14 s (twice). On `wip-efd9eca` the same harness hears the echo at 5.65 to 8.6 s, 700 Hz. `rtc/offer` answered 200 twice (a listen session and a talk session), alive 204, close 204, ice 404 (no TURN). The question the case answers: does the media stop at the edge, or in the page?

What it does: a signed-in user with `may_tx` (`N0BRU`, the `rtc_tx_page` fixture of the Talk cases) and a SECOND page, the user without `may_tx` (`N0BRX`, `br_rx`; guests are off in this rig), both listen on module E. Both pages are sampled every 500 ms from 2 s before the press to 14 s after the release. The user holds Talk (mouse down on `talk-ptt`) for 3.0 s and releases.

| Check | Kind | What it asserts |
|-------|------|-----------------|
| listening / "On air" / "Ended: Button released." | evidence | both pages reach "Listening on module E"; the talk status becomes "On air" and, after the release, "Ended: Button released." (as the Talk cases) |
| `talker_page_audio_element_hears_echo` | **PRODUCT CHECK** | what the USER HEARS in the TALKER page: an `AnalyserNode` on the CURRENT `srcObject` of the page's audio element (`pages.MODULE_AUDIO_CSS`, `[data-testid='listen-audio']`) has rms >= 0.01 (two 0.5 s samples; one for an over under 1.5 s) in the echo window `[anchor, anchor + the SUT over's duration + 3 s]` |
| `listener_page_audio_element_hears_live` | **PRODUCT CHECK** | the same in the SECOND page, during the over the SUT reports (its start to its end + 0.4 s) |
| `listener_page_audio_element_hears_echo` | **PRODUCT CHECK** | the same in the SECOND page, in the echo window opened 0.6 s after the anchor |
| `over_not_cut_short` | **PRODUCT CHECK** | the SUT's over lasts within 0.5 s of the tester's hold (On air to release); it FAILS with both durations ("the SUT ended the over after 1.00 s while Talk was held 3.00 s ... (PTTRX1)"). Only judged when the public state gave an over duration; otherwise an evidence row `not_observed` |
| `talker_hears_echo`, `listener_hears_live`, `listener_hears_echo` | evidence | the RECEIVER level (the first version's product checks, kept as evidence rows; they still fail the case): the analyser on the track of every RTCPeerConnection, in the same windows, rms >= 0.01 (Chromium also 700 Hz within 20 Hz, tone fraction >= 0.1; the judgement and thresholds of `dvtest.tools.prod_talk`, deploy check g) |

The audio element (what the user hears). Production check g on `wip-9a348ca` failed while this case passed: its analyser sits on `audio[data-testid=listen-audio].srcObject` of the page, our recorder first tapped the receiver track of every RTCPeerConnection directly, so a page that does not re-bind the element to the talk session's stream was invisible. The recorder now watches the element too (`rec.el`): `elPoll` every 250 ms, an `AnalyserNode` (fftSize 2048) re-attached whenever `srcObject` or its audio tracks change, on `addtrack` / `removetrack` of the element's current MediaStream and on `ended` / `unmute` of its tracks (production's analyser attached once per stream identity, the page adds the track to the SAME stream later, so it never re-attached). Evidence per 500 ms sample (`samples[].el`): the srcObject's stream id, its audio tracks (id, `readyState`, `enabled`, `muted`, and `pc` = the id of the RTCPeerConnection whose receiver track it is, matched by track id), the element's `paused` / `muted` / `volume` / `readyState`, the AudioContext state, the analyser's best rms since the last sample; `recorder.element.log` lists every re-attachment. A failing element check names the stage and prints BOTH sides: `element_on_closed_stream` ("the page plays the closed listen stream (track ended) while the talk session receives the echo"), `element_on_other_session`, `element_without_stream`, `element_stream_without_audio_track`, `element_stream_foreign`, `element_paused_or_muted`, `element_silent_on_live_stream`, `no_element_data`, `no_audio_element`, `not_observable` (the AudioContext is not running, Firefox in the container; judged by the receiver), and every detail ends with `getStats inbound-rtp in the window: pc0(closed) +0 bytes ...; pc1(talk, live) +32000 bytes ...` and `audio element analyser: best rms ..., stream, tracks [...]`.

Primary result to compare between images, per page in `analysis.<page>.element_timing` and as `[RECORD] ... audio element srcObject ...` lines: for every `srcObject` assignment (the setter of `HTMLMediaElement.srcObject` is wrapped, so the time is exact; a poll notes one it missed, `via: poll`) the time from the assignment to the first audio track in that stream and to the track's first `unmute`, each relative to the Talk press and to the answer of the Talk circuit's `rtc/offer` (the first offer answered at or after the press), all in ms on the page clock. `0` = the stream already had an unmuted track at the assignment. Also `ice1.<page>.session_order`: the order and times of `POST /rtc/close` and `/rtc/offer` around the press (production: close, then offer).

Status timelines (evidence only, no assertion): a MutationObserver (childList, characterData, subtree, plus the Listen card's `data-state`) installed by the recorder's init script before the page's scripts logs EVERY text change of the Talk status line (`pages.TALK_STATUS_CSS`, `#talk-status`) and of the Listen status line (`pages.LISTEN_STATUS_CSS`) with the page-clock time relative to the recorder's t0. Per page the JSON has `talk_status_timeline` and `listen_status_timeline` (each row: `text`, `t_ms`, `rel_press_ms`, `rel_release_ms`, `held_ms` = how long the text stayed), and a `[RECORD] talk-echo ... talk status: ...` line prints them, for example `'Pending - queued / waiting for the floor' @ +10 ms from the press (-3490 from the release), held 34 ms; 'On air' @ +44 ms ...`. This is what shows the transient "Pending - queued / waiting for the floor" that the polled `texts.after_press` (sampled at 0.22 s) cannot. First rig result (six runs on `wip-9a348ca`, `max_tx = 1`) showed ONE row ('Ready'): the observer had been put on `document.documentElement`, which does not exist yet when an init script runs, so it threw and no observer existed. It now observes the `document` node (subtree), re-queries both selectors at every callback (the CURRENT text of the CURRENT first match, a serial per DOM node as `el`, the number of matches as `matches`; a row is kept when the text OR the node changed, `el_changed`), and a 10 ms sampler runs from the `listening` mark until 15 s after the `press` mark as a fallback; `recorder.observer` says what was observed and how many callbacks came. Two changes made by the page in one task are seen as one (the observer reads the text when its callback runs); the page's own transient of tens of ms is seen when the page renders it in a separate task.

Optional config `rig/config/dv-router/webrtc-max-tx-1.toml` (`[edges.webrtc] max_tx = 1`, as production has it; the edge default is 20): name it after `web.toml` in `DVT_SUT_CONFIG_EXTRA`:

```sh
... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/echo.toml:rig/config/dv-router/webrtc-max-tx-1.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_talk_echo_return.py -k "chromium"
```

Where the echo window opens (the anchor), recorded in `windows` of the JSON: the echo is judged against the over THE SUT REPORTED, not against the tester's release, because the SUT can end an over long before the user lets go (PTTRX1: a forced connection change ended a 3 s hold's over after about 1 s; the echo replayed that 1 s over BEFORE the tester released, so a window that opened at the release found no bytes, which was a timing artefact and not an edge fault: the second page had received the live over and its echo). The tester polls the public state of room E every 0.5 s (`OverTracker`): the end of the first over after the first poll is `rooms[E].last.end_ms` (the server clock, tied to the tester's by the skew against `now_ms` of the same state, then to each page's clock by the recorder's `t0_wall`); the start is a `start_ms`/`started_ms`/`since_ms`/`begin_ms` field of `onair` or `last` when the state has one, else the first poll that showed the module on air less one poll interval (an earlier bound, so a cut-short over is never invented). Anchors, in order: `state` (that end), `first_packet` (fallback: the first inbound packet after a quiet gap that followed the live burst in the second page's series, less one sampling interval), `release` (last resort: the old tester-anchored window, release to +14 s). `windows.echo_duration_source` says where the echo length came from (`state`, `live_burst`, `tester_hold`); `sut_over` (also top level) has the SUT's duration next to `windows.tester_held_s`, and `sut_room_entries` every distinct room entry seen in the state. An end seen later than 1.5 s after the release is the echo's own over and is not used (the state is then not an anchor).

The audio checks fail with the stage that stopped (`judge_window`), so the message says where the media stopped: `no_inbound_bytes` (no `inbound-rtp` bytes or packets on any RTCPeerConnection of the page in the window: the media stops before the page, at or before the edge), `decoder_silent` (bytes arrive, the decoded audio is silent: page/decoder), `analyser_silent_decoder_audible` (bytes arrive and `totalAudioEnergy` grows but the page's analyser is silent), `tap_dead_*` (the analyser got no audio callbacks: Firefox's suspended AudioContext), `wrong_frequency`. A one-line diagnosis follows the failures (for instance "the echo reaches the SECOND page but no bytes reach the TALKER page: it stops at the edge for the talker's session"). On Firefox and WebKit the fake source is not 700 Hz (about 1000 Hz and 500 Hz): the frequency is recorded and not asserted; when the analyser cannot run (no callbacks), the level is judged from `totalAudioEnergy` of getStats, then from the packet count (>= 50 packets), and the verdict says `via`.

The shared probe (`RTC_PROBE_JS`) taps only the RTCPeerConnection that was the last one when `tap.start()` ran, so a page that swaps the Listen session for a new one at the Talk press would look silent to it even if the new session carried the echo. The case therefore installs its own recorder (`ECHO_RECORDER_JS`, after the probe) that taps the receiver of EVERY RTCPeerConnection the page creates, and compares the analyser with the decoder (`totalAudioEnergy`) and with the transport (bytes).

Evidence, written in a `finally` block to `out/<run>/web/talk-echo-<engine>.json` also when a check fails or the flow breaks: per page (`talker`, `listener`): `recorder` (every RTCPeerConnection with `created_at`, `closed_at` and the three states, every 500 ms sample with the states, `inbound-rtp` bytes / packets / lost / `audioLevel` / `totalAudioEnergy` / samples, `outbound-rtp` bytes, the selected candidate pair's bytes, and the analyser of each live pc: rms of the last 0.5 s and 1 s, dominant frequency, tone fraction, audio callbacks; marks `listening`, `press`, `on_air`, `release`, `ended`, `final`), `series` (the same summed over the pcs), `analysis` (sampling regularity, `pcs`: whether the page created a NEW RTCPeerConnection at the Talk press and at the release, and which pc closed), `requests` and `requests_summary` (every `/api/v1/rtc/*` request with method, status, phase and time; no body, so no session id or SDP), `ctl` (the talker's ctl DataChannel frames, op / state / code / reason only); `resources` inside `recorder` is the same audit on the page's clock (Resource Timing). Top level: `meta`, `phases` (tester wall clock), `texts`, `timeline` (the status lines on change), `public_state_room_E` (is the module on air, and whose callsign, once a second, every 0.5 s during the hold), `checks`, `verdicts`, `diagnosis`, `failures`. The `[RECORD] talk-echo` line on stdout repeats the diagnosis.

ICE1 evidence (added after DV-Router's analysis `analyze-196-pttaudio.md`: the page listens to `navigator.connection` `change` in `connect()`; headless Chromium can fire it from its network-quality estimate; the page then runs `interrupted(true)`, which shows the SAME text "Ended: Button released.", and restarts ICE on the sendrecv circuit; the edge can fail the peer on a re-offer it rejects, and an accepted restart drops routes). Written to the same JSON, also on failure, under `ice1`: (1) every `POST /api/v1/rtc/offer` of each page with its sanitised request body (SDP, `sess_id`, other fields; any key that looks like a cookie, CSRF token, password, token, secret or TOTP is replaced by `<removed>`; no headers are read), the response status and time, and `offers` (index, phase, time, status, `sess_id_in_body` = an offer for an EXISTING circuit, i.e. a restart or renegotiation, media lines); (2) `edge_metrics_before_talk` / `edge_metrics_after_echo` / `edge_metrics_delta` / `restarts_delta`: the `dv_edge_webrtc_*` families with `restart` or the word `ice` in the name (so `dv_edge_webrtc_ice_restarts_total`), read through the control channel (`metrics xlxd-sut`, `DVT_RIG_CONTROL=1`, set by `run.sh`; `null` without it; the edge publishes every 10 s, the second read comes 14 s after the release); (3) `recorder.connection` (`navigator.connection` initial values and every `change` with page-clock time, effectiveType, rtt, downlink, saveData, type; the listener is installed in the init script before the page's scripts) and `recorder.ui` (the talk status line, Listen status line and Listen `data-state` with the page-clock time of each change, via a MutationObserver), so "Ended: Button released." can be placed against the release; (4) `pcs_inbound_in_echo_window`: for every RTCPeerConnection its inbound-rtp gain and last snapshot in the 14 s after the release, so the pc created at Talk is told from the Listen pc the page closed. The recorder samples every pc until it is closed (a closed pc keeps its last counters, taken just before `close()` where the engine allows it), and taps the receiver of every pc, not only the first. `ice1.<page>.diagnosis` and the `[RECORD] talk-echo ... ICE1` line say, for example: `connection-change fired before the echo window: 1, first at 4.5 s (-1000 ms from the release) effectiveType 3g rtt 300 downlink 10; offers: 3 (1 with sess_id = restart or renegotiation); ice restarts counter delta: 1.0; 'Ended: Button released.' shown -1000 ms from the release (BEFORE the release: the page's interrupted() path)`.

Forced connection change (first rig runs: on `wip-efd9eca` AND `wip-97c669d` the plain flow passes on Chromium, the rig network never fires `navigator.connection` `change`, so the suspected ICE1 path is forced): `test_talk_echo_after_a_forced_connection_change[chromium-talk|release|echo]` runs the same case and dispatches a synthetic `navigator.connection.dispatchEvent(new Event('change'))` in the TALKER page: `talk` = 1 s after the press while Talk is held, `release` = right after the release, `echo` = 1 s after the release (while the echo should arrive). The product checks are unchanged, so the variant fails when the forced change breaks the audio; every failure line then starts with `after a forced connection change at <phase>:`. Evidence: `talk-echo-chromium-conn-<phase>.json` with the same content plus `forced_change` (phase, wall time, recorder-clock time of the `forced_change` mark, `since_start_s`) and `meta.forced_change_phase`; the recorder's own `connection.events` lists the synthetic event next to any real one. The siblings run on Chromium only and skip with a reason when `navigator.connection` is undefined. The same switch for the plain test and any engine: `DVT_TALK_ECHO_CONN_CHANGE=off|talk|release|echo` (default off; `rig/compose.yml` forwards it to the tester).

```sh
# the four Chromium runs (plain + talk + release + echo, about 4 to 5 minutes), same environment as below
... scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_talk_echo_return.py -k "chromium"
# only the forced variants
... scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_talk_echo_return.py -k "conn and chromium"
```

Run (about 3 minutes: three runs of about 40 s; the echo needs no USRP client, so `DVT_USRP_DYNAMIC` is not needed):

```sh
XLXD_SUT_IMAGE=dv-router:wip-97c669d DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
  DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_FAKE=1 \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/echo.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_talk_echo_return.py
```

`-k chromium` runs the engine of the finding only. Run it on the image with the finding (`wip-97c669d`) and on the good one (`wip-efd9eca`) and compare the two `talk-echo-chromium.json`: where the sums of `bytes` stop growing, whether `pcs.new_pc_at_talk` differs, which pc the echo arrives on. With `--capture` the pcap of the run shows whether RTP leaves the edge towards the browser (UDP 50000) after the release. The case skips with a reason unless the profile is DV-Router, the launch is native and the rendered config has `[echo] modules = { E = true }` (it reads `out/<run>/xlxd-config/xlxd-sut/dv-router.toml`, never edits it).

## UNVERIFIED (Talk echo return)

Everything below is assumed, not seen on a running rig; the case was written from the other Talk cases and the production check `prod_talk` only.

1. The rig can run `web.toml`, `admin.toml`, `totp-required.toml` and `echo.toml` together, and module E is served by the template's `[node] rooms` (A-Z), as `echo.toml` says. No other web case combines the web profile with `echo.toml`.
2. `N0BRU` (`may_tx`) may transmit on module E: the case assumes the permission is the account's, not per module. If the page shows no Hold Talk or "Denied", the case fails at the evidence step with the text.
3. A local WebRTC over of 50 frames or more is replayed by `dv-svc-echo` 1 s after its end and is delivered to the WebRTC listeners of E, including the page that talked; 3 s of speech is 150 frames, well above the limit. The window of the echo check starts 1.0 s after the release (the `delay_ms` default) and ends 14 s after it.
4. The `speech` the echo replays reaches the page as PCMU RTP on the same kind of session as a normal Listen; `inbound-rtp` is expected to exist (audio) on a pc that receives it. A page that receives the echo on a pc the recorder never saw (a pc created before the init script, in another frame) would read as silent.
5. `window.AudioContext` can be created in a `track` event handler without a user gesture (Chromium: the `--autoplay-policy` flag of `launch_options`; Firefox: its prefs; Playwright WebKit: not known). A suspended context is told apart from a silent page by the callback count (`tap_callbacks`), and the level then falls back to `totalAudioEnergy` and the packet count.
6. `totalAudioEnergy` and `responseStatus` of Resource Timing exist in the engines (Chromium: yes; Firefox and WebKit: not checked). A missing one is recorded as `null` and the judges skip it.
7. `RTCPeerConnection.close()` is called by the page (the recorder wraps it); a pc closed by the page without `close()` (a discarded object) is noticed from `connectionState === "closed"` at the next sample, up to 500 ms late.
8. Two pages (two contexts of one engine) from one address fit the service's cap of 4 event streams per client address: each user page opens one `public/events` stream.
9. The headless pages are not throttled: `hidden_samples` and `max_gap_ms` in `analysis.sampling` show it. A sampler gap over 1 s would make the 0.5 s windows coarser and the `min_loud = 2` rule stricter.
10. The tester's and the browsers' clocks are the host clock (`phases` against the recorder's `t0_wall`), as in the other cases; the page clock is used for every judgement, the wall clock only for the request audit.
11. The thresholds (rms floor 0.01, 700 Hz within 20 Hz, tone fraction 0.1, `MIN_LOUD` = two 0.5 s samples, energy floor 1e-4, 50 packets) are `prod_talk`'s or derived from it (1 s at the floor); `MIN_LOUD` and the packet count are this case's own.
12. `--capture` with the echo module and the web rig together (the wire check of the run) was not tried.
13. ICE1 evidence: the Talk status line is `#talk-status` and the Listen card `[data-testid='listen-connection']` with `p[role=status]` (web_pages.py selectors); `navigator.connection` exists in the engine (Chromium yes, Firefox and WebKit usually not: `supported: false`); `request.post_data` of the offer is readable through the proxy; the edge family name contains `restart` (the exact name `dv_edge_webrtc_ice_restarts_total` is the coordinator's, not seen on a rig); a timestamp from the tester's wall clock and one from a page clock differ by the sampling skew, so "-1000 ms from the release" is page-clock against the recorder's `release` mark, which Python sets a few milliseconds after the click.
14. Forced change: a synthetic `Event('change')` on `navigator.connection` reaches the page's ICE1 listener like a real one (the page does not check `isTrusted`) (not checked on a page). The compose forwarding of `DVT_TALK_ECHO_CONN_CHANGE` was added without a rig run; the `-k conn` selection needs no variable.

### PTTRX1 standing regression

`test_pttrx1_talk_survives_a_connection_change[chromium-1..5]` (cases/test_web_talk_echo_return.py): the forced `talk` phase (one synthetic
`navigator.connection` 'change' 1 s into a held Talk) five times per image, plus the plain flow `test_talk_echo_returns_to_the_page`.
Reproduced on wip-97c669d (out 20261108T092955Z: the talker page sent 4 extra offers with sess_id, the edge's ICE restart counter +4, the
SUT ended the over about 1 s after the press although Talk was held 3 s; an earlier reading of that run, that the second page received no media at all, was a window artefact: the echo replayed the 1 s over before the tester's release, where the old window opened, and the second page had 108 live packets and a 9 packet tail; the windows now follow the SUT's over, and `over_not_cut_short` is the check that fails); wip-efd9eca passes (no ICE1 listener). Expected to FAIL before
the PTTRX1 fix and PASS after it. Run (about 6 minutes): the "Talk echo return" command with `-k "chromium and (pttrx1 or returns_to_the_page)"`.
Evidence per repeat: `out/<run>/web/talk-echo-chromium-conn-talk-<n>.json`.
15. Over window: the public state of room E has `last.end_ms` (the Map fade case reads it) and `now_ms`; whether `onair` or `last` carries a start field (`start_ms`, `started_ms`, `since_ms`, `begin_ms`) and a `duration_ms` is not known (the tracker uses them when present and records every distinct room entry in `sut_room_entries`); the 0.5 s poll cadence catches the original over's end before the echo's own over replaces `last` 1 s later (a miss gives the `first_packet` anchor); the server clock and the tester's differ by the skew measured at each poll (the host clock on the rig); the 0.5 s tolerance of `over_not_cut_short` and the 0.6 s guard of the listener's echo window are this case's own.

## After the first rig run (wip-97c669d)

10 of 18 passed. Three corrections, one product finding:

1. Case defect, fixed: the text checks flagged a span of the `sr-only` live text ("spans 15..1286 of 360 px") although the page did not overflow. Visually hidden
   elements are no longer judged: `visually_hidden()` in `cases/map_matrix_support.py` (a clip of `rect(0,0,0,0)`, a clip-path of `inset(50%)` or more, a box of
   at most 1 x 1 px, or an absolutely positioned box with hidden overflow that is at most 1 px wide or high; `aria-hidden` alone is not a reason), applied to the
   element and to each ancestor (`hidden_chain`, the style facts the measurement returns), with unit tests.
2. Case defect (probable), fixed: Playwright WebKit reported `Fetch API cannot load .../api/v1/auth/session due to access control checks` as a page error. The
   case now records the time of every navigation or reload it starts (`open_map`, the language switch, the hub-down reload) and every page error; exactly that
   message on WebKit within 1 s of such a navigation is tolerated and recorded (`tolerated_navigation_errors` in the JSON, a `[RECORD]` line); the same message
   anywhere else, or any other message, still fails the case. Whether the fetch really is the page's session probe aborted by the reload is an inference.
3. Product evidence, `test_map_closed_desk_sign_thai`: after SIGSTOP of `dv-edge-xlx` the page drew 11 desks instead of 12 and none closed, i.e. the desk of the
   stopped edge disappeared. The case now opens the Map before the stop, records `data-id:data-state` of every desk before and after, and the public state's
   `info.edges_enabled` and `live.edges` at the moment of the check (`desks_and_state_lists` in the JSON), and fails with a `FINDING: the desk of the stopped edge
   xlx is not drawn (desks 12 -> 11), want it drawn with the Closed sign (web.md: enabled but not in live.edges)` plus the two lists, so DV-Router can tell whether
   the page or the state is wrong. The assertion stays (it is the spec); the 12 combinations still run for the overflow and Thai checks, the sign is not judged.
16. Audio element: `[data-testid='listen-audio']` is the element the page plays the module audio with (`pages.MODULE_AUDIO_CSS`, a WEB2 hook); the page sets `srcObject` on it (a property assignment the wrapped setter sees; a stream set through an attribute or a `srcObject` of a different prototype would be noticed by the 250 ms poll only); the element's stream is the receiver stream of one RTCPeerConnection, so its track ids match a `track` event of that pc (a pc created before the init script is not seen); an `AnalyserNode` on a remote stream reads audio in Chromium because the page's own element plays it; `webrtc-max-tx-1.toml`: the key `[edges.webrtc] max_tx` and its meaning come from DV-Router's reviews (`assertion.rs`, `tx.rs`), no run here has used it.


## After the second rig run (wip-220e45b)

12 passed, 6 failed; two case-side corrections:

1. Closed desk timing: the first combination was judged before the page had reacted to `live.edges` dropping `xlx` (its next state poll or stream event). The case
   now polls the desk's `data-state` every 250 ms, up to 15 s, until it is drawn and not `up`, before it judges anything; the delay from the moment `live.edges`
   no longer listed the edge is `timing.desk_left_up_after_s` (`desk_wait_s` when it never changed) in the JSON, and the FINDING text carries the waited time.
2. WebKit `auth/session` page error: the tolerance window is now 1 s around ANY main-frame navigation, whoever started it: `NavWatch` records `framenavigated`
   of the main frame, `load`, the navigations the case starts and the case actions (goto, click, reload, `set_viewport_size`, `emulate_media`) with times. The JSON
   (`navigation_evidence`) lists those times and every page error with its distance to the nearest navigation and its three nearest case actions; an error
   with no navigation near it still fails the case and the failure text prints that distance and the three actions.
