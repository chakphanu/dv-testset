<!-- SPDX-License-Identifier: MIT -->
# Adaptation to the newest DV-Router builds: results and diff

The cases were adapted to the DV-Router changes DASH1, WEB2, API2, API4, ADM1, XC1, LOGIN1, LOGIN1b and TALK1
(image `dv-router:wip-13e7e70`), and rerun. The runs that were not started when newer images appeared ran on the
superset images named per row (`wip-648e539` adds K22 and ICE1a, `wip-3b0724a` adds K22b; both contain TALK1 and
DOC6). The "before" column is the 5aa7cfd / Gate B state recorded in the stage documents and commits. Nothing was
loosened: a difference is a FINDING line or a documented status, never a weaker assertion.

## Counts per suite (passed / failed / skipped)

| Suite | Image of the "after" run | Out dir | Before | After |
|-------|--------------------------|---------|--------|-------|
| Stage 1+2: smoke + API (`test_web_smoke.py`, `test_web_api.py`) | 648e539 | `20261101T194729Z` | 357 / 0 / 0 | 373 / 1 / 0, then 374 / 0 / 0 (the one failure was a rig defect, fixed and rerun with `-k`, `20261101T202437Z`) |
| Stage 3a: browser UI (`test_web_browser.py`) | 648e539 | `20261101T203217Z`, `20261101T205603Z` | 49 / 3 / 5 | 52 / 2 / 3, then 54 / 0 / 3 (two WebKit CSP assertions made findings for that engine; rerun passes) |
| Stage 3b: WebRTC + recorder (`test_web_webrtc.py`, `test_web_recorder.py`) | 648e539 | `20261101T205817Z` | 45 / 1 (the TALK1 expected failure) / 0 | 55 / 0 / 0 |
| Stage 4 NAT, TURN off | 3b0724a | `20261101T211249Z`, `20261101T214145Z` | 21 / 0 / 3 (a subset) | 26 / 1 (+2 setup errors) / 15 (1 xfailed); the 3 WebKit failures pass alone (3 / 0 / 0) |
| Stage 4 NAT, TURN on | 3b0724a | `20261101T214325Z` | 30 / 6 / 8 (+1 error) | NOT COMPLETE: the host filesystem ran out of metadata space after about 24 cases (see Blocked) |
| T14 cross-mode (`test_crossmode_exact.py`, `test_crossmode_failure.py`) | 13e7e70 (the 4 header-gap cases again on 3b0724a) | `20261101T192333Z`, `20261101T214004Z` | 47 / 0 / 0 (wip-265d818) | 49 / 4 / 0 on 13e7e70, then 53 / 0 / 0 (4 new measurement cases fixed in the case, rerun) |
| M17 (`test_m17_peer_link.py`, `test_m17_relay.py`, `test_m17_packet.py`, native, template only) | 13e7e70 | `20261101T184327Z` | 97 / 0 / 0 | 97 / 0 / 0 (unaffected by the changes; run to prove it) |
| Data ident, `DVT_REQUIRE_IDENT=true` / `false` | 13e7e70 | `20261101T185433Z`, `20261101T185933Z` | pass | 8 / 0 / 0 each |
| P19 edge relink (`--profile interlink,nxdn,p25`, import) | 13e7e70 | `20261101T190415Z` | 6 / 0 / 0 | 6 / 0 / 0 |

Static checks at the end of the work: unit tests green, ruff clean, `scripts/pii-check.sh` clean.

## Cases that changed status, and why

| Case | Before | After | Why |
|------|--------|-------|-----|
| `test_dashboard_renders_the_state` (3 engines) | fail | pass | DASH1: fragments removed from `public.js`; the workaround patch and its case are gone |
| `test_xss_room_message_arrives_live_on_the_public_stream` | skip on 2 engines | pass on 3 | DASH1: live text before the room snapshot is buffered |
| recordings page cases | patched script served | pass as served | DASH1 for `recordings.js` |
| `test_talk_reaches_a_protocol_listener` (6 s hold) | expected failure (over ended after about 4 s) | pass on 3 | TALK1: the edge answers each lease renewal with `renewed`, the page resets its watchdog |
| `test_login_wrong_password_text`, `test_operator_login_with_totp_step` | generic text for a wrong code | dedicated wrong-code text only after a `totp_required` challenge | LOGIN1b |
| `test_cross_pair_exact[dplus-m17]` | one OPEN for the join header (FINDING `dplus-join-stream`) | zero OPEN, zero data, zero CS after the join | XC1 |
| `test_cross_pair_exact[dstar-usrp, dmr-usrp, m17-usrp]` and the one-OPEN USRP case | exact voice units plus a synthetic PCM end unit (prefix compare, NOTE) | exact bytes, voice units then exactly one 32 B unkey | XC1: no synthetic USRP end unit |
| `test_prefix_minimum_role` | 144 rows | 156 | WEB2 root probes GET/HEAD `/health` and `/ready` x 3 roles |
| `test_public_events_per_ip_cap` and the events matrix rows | slot released at the next 15 s heartbeat | released at once | rig proxy now passes the client close upstream (DV-Router's own SSE-disconnect test relies on that) |
| Playwright WebKit CSP assertions (dashboard, normal use) | n/a | recorded as FINDING, still asserted for Chromium and Firefox | WebKit still reports `style-src-attr` after DASH1 |

New cases: `test_event_stream_slot_is_freed_on_reload`, `test_talk_hold_10_s_stays_on_air_until_release`,
`test_talk_release_inside_the_watchdog_window`, `test_listen_gauges_in_the_hub_metrics`,
`test_browser_talk_is_recorded_as_webrtc`, `test_web_health_and_ready`, `test_declared_success_statuses_of_real_calls`,
`test_limits_login_missing_code_is_no_failure`, `test_limits_login_wrong_codes_lock`,
`test_limits_login_wrong_passwords_count_challenges_do_not`, `test_voiceless_stream_opens_no_session[header, header-end]`,
`test_header_gap_costs_no_hidden_open[dstar|dplus x open0|open300]`.

## Findings CLOSED by the new builds

| Finding | Closed by | Evidence |
|---------|-----------|----------|
| 3a-1 the public dashboard does not render | DASH1 | plain dashboard case passes on Chromium, Firefox, Playwright WebKit |
| 3a-2 live room text not always shown | DASH1 | live-message case passes on 3 engines |
| 3b-18 `recordings.js` fragment defect | DASH1 | `/recordings/` renders as served |
| 3a-5 reload gets 429 (stream slot freed late) | DASH1 (EventSource closed on pagehide) | 0 of 6 reloads refused on every engine |
| 3a-6 dead `totp_invalid` text, password-only step spends lockout | LOGIN1, LOGIN1b | API cases: 5 missing-code logins leave the pair unlocked; wrong codes still lock (429); page shows the wrong-code text only after the challenge |
| 3b-8 Hold Talk ends by itself after about 4 s; 3b-10 no release text | TALK1 | 6 s and 10 s holds stay "On air", `renewed` acks every 1.0 s (6 and 10 seen, minimum interval 999 ms), "Ended: Button released." |
| 3b-17 recorder row `usrp`/`terminator` | DOC6 (explanation) | it was the USRP talker's own over; a browser Talk is recorded as `webrtc`/`terminator` (case added, passes) |
| stage 2: nine observed statuses outside the specs | API4 | now documented (guard order, 404 route misses, rtc 403, ice 404, totp 400/403, lockout 429, OPTIONS 405, CSP only on static files); the cases assert them as the documented answer |
| stage 2: 26 operations declare no 2xx; no way to make admin viewer/operator accounts | API4, ADM1 | success codes asserted on real calls; `dv-svc-admin user add` works in a throw-away capped container of the image (`/usr/local/bin/dv-svc-admin`), `argon2_pure.py` kept as fallback |
| T14 `dplus-join-stream`, USRP end unit open point | XC1 | see above |
| stage 4 N1 (Firefox 2, 3, 5 s blackhole ends the session) | ICE1a (wip-648e539 and later; wip-3b0724a measured) | with TURN off, all three engines resume 2, 3, 5 and 8 s blackholes on all three NAT types without a new offer (27 of 27 for 2, 3, 5 s). The xfail in `cases/test_web_nat.py` was NOT removed (see New findings) |
| stage 4 N2 (page closes the peer at the first `disconnected`) | ICE1a | the page now waits: Chromium and Playwright WebKit resume through a 12 s blackhole and give up on a 20 s one about 16.4 s after the start |

## NEW findings (observed vs documented)

| Area | Observed | Documented / expected |
|------|----------|----------------------|
| WebKit CSP | Playwright WebKit still reports `style-src-attr` (inline) from `htm-preact-3.1.1.module.js` and the console error "Refused to apply a stylesheet" on every rendering page (dashboard, login, ops, recordings), 100+ lines; Chromium and Firefox report none | DASH1 removed every `style=` of `web/site`; expected no report in any engine |
| `/ready` | answers 503 24 times before the first 200 while `/api/v1/public/state` already answers | `/ready` is 200 only when the hub answers, 36 s have passed since start and RTC ownership recovery is done (WEB2 docs); consistent, but a page-level wait is too early |
| ICE1a, Firefox | with TURN off a 12 s blackhole: full cone: not lost but no audio at the check (ICE `disconnected` at 10.9 s, not recovered when checked); port restricted and symmetric: lost. The `N1` xfail branch of `test_blackhole_resume_and_failure` fires on this 12 s condition, not on the 2 to 5 s requirement it was written for. With TURN on (partial run) the same full-cone 12 s case resumed | testing.md: 2 to 5 s must resume without a new offer (holds); behaviour beyond 5 s depends on the page's 12 s grace |
| Browser container | in sessions of 20+ minutes one browser connection of the shared `playwright run-server` died (`Browser closed`): 4 recorder setups in the combined 3a+3b session, 3 WebKit 404 cases in the NAT run without TURN; each passes when rerun alone | not a product matter; cause not established (memory peaked at 1.1 GiB of 3 GiB); `engine_browsers` now reconnects, stages stay separate runs |
| Rig proxy | the stdlib TLS proxy did not pass a client close upstream, so API clients held stream slots until the next heartbeat | fixed in `dvtest/tools/tls_proxy.py` (unit test added) |
| T14 measurement | with a 0.5 s voiceless header gap and without it, the first transcoded output arrives the same time after the first voice frame: 0.022 s at an OPEN delay of 0 ms and 0.304 s at 300 ms (D-STAR and DPlus); the OPEN reaches the fake within 1 ms of the first voice unit and never during the gap | XC1 / transcoding.md: the OPEN no longer overlaps the header-to-voice gap; the cost is min(OPEN time, gap) = 0.30 s here. Matches |

## Blocked

The TURN-on NAT run (about 1 hour) did not complete: after about 20 minutes the host root filesystem (btrfs, 0 bytes
unallocated, 226 GiB free by `df`) refused every new file in the repository, the tester's results and `run.sh` own
lock file ("No space left on device"; docker could not remove containers either). Freeing it needs a `btrfs balance`
as root or deleting old `out/` runs, both of which need the owner's approval (the workspace rules forbid deleting or
changing system state unasked). The partial results above (24 cases passed before the failure; relay and Chromium
blackhole rows as in the TURN-off run) are in `out/20261101T214325Z/web-findings.txt`; no result is claimed from them.
Rerun after freeing space: see the NAT rerun command in `docs/web-nat-cases.md`.

## Commands

```sh
export XLXD_SUT_IMAGE=dv-router:wip-3b0724a DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path>
# stage 1+2
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml scripts/run.sh -m rig --capture --profile web -- cases/test_web_smoke.py cases/test_web_api.py
# stage 3a, then 3b (separate runs)
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_browser.py
DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/recorder.toml \
  scripts/run.sh -m rig --capture --profile web,browser -- cases/test_web_webrtc.py cases/test_web_recorder.py
# stage 4 (TURN on, about 1 h; TURN off: drop turn.toml)
DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml \
  scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py
# T14
DVT_TRANSCODER_FAKE=1 DVT_USRP_DYNAMIC=1 scripts/run.sh -m crossmode --capture -- cases/test_crossmode_exact.py cases/test_crossmode_failure.py
# M17, data ident, P19 (import launch for P19)
scripts/run.sh -m rig --capture -- cases/proto/test_m17_peer_link.py cases/proto/test_m17_relay.py cases/proto/test_m17_packet.py
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml:rig/config/dv-router/xlx-peer.toml DVT_REQUIRE_IDENT=true|false \
  scripts/run.sh -m rig --capture --profile interlink -- cases/test_data_ident.py
DVT_SUT_LAUNCH=import scripts/run.sh -m rig --capture --profile interlink,nxdn,p25 -- cases/test_edge_relink.py
```
