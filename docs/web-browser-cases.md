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
| `test_operator_login_with_totp_step` (slow) | password alone does not sign an operator in (code field appears, no cookie), a wrong password still gives the generic text, a wrong code AFTER the challenge gives the dedicated wrong-code text (LOGIN1b), the right code signs in, the cookie flags, `role operator / totp ok`, sign out | 3 |
| `test_ops_pages_render` | the six operator pages (clients, events, accounts, invites, audit, **Radio IDs `#/ids`**) render for an operator with the headings of the UI and the five-plus-one navigation, one document | 3 |
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
| `test_admin_spa_loads` | the admin host sign-in screen renders, CSP restrictive | 3 |

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
| page | `/recordings/` lists the rows of the day (module, callsign, protocol, reason, lost units equal the API; duration "2s"), Play sets the shared `<audio>`: Chromium and WebKit reach `currentTime` 2 s with no error (Chromium requested `bytes=0-`, 206, once; WebKit's media request shows no readable status); the playback audit row is written; Firefox: `currentTime` advances to the end and the engine then raises `OnMediaSinkAudioError` (no sound card), end-of-clip and seek not judged for it |

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
