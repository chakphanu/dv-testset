<!-- SPDX-License-Identifier: MIT -->
# DP19b cases: spatial status privacy on every public path

Status: written offline for the image AFTER wip-7bd0082 (merge `52db4641`, not in the current candidate); **nothing here has run on a rig**. Sources (read only): DV-Router review 247
("Public path enumeration", "Rig case to request"), `docs/design/web.md` (message panel, "Built (DP19b)"), `docs/design/data.md` 13.4, `docs/api/openapi-web.yaml` (`StatusMessage`, `SpatialStatus`,
`Rooms.spatial_status`), all at `52db4641`.

What DP19b adds: every status row carries an explicit boolean `spatial`, copied from the worker's classification of the original APRS information bytes (a locator status such as `>IO91SX/G`).
The public state (`rooms[].spatial_status`, a projection of the 64-row/24 h status store), the live and replayed public SSE and the user message history withhold a SPATIAL status under the
same current hidden-base, opt-out and positions-enabled policy as positions, and before the hidden set is known. Plain status (`spatial: false`) and text, the look-alike TEXT `IO91SX/G` included,
are unaffected. The browser reconciles its message panels and the talker status against the filtered `spatial_status` array at each state snapshot. F1 (review 247): the SSE callback needed a
fresh policy cache that only requests refreshed, so on a quiet node every live position and spatial status was dropped; the fix refreshes the cache every 5 s.

**Probe, no switch.** The cases skip with the reason on an image whose public state has no `spatial_status` key in the room (the key is required there) and when the run has `positions = false` or no
bound RF port. There is no switch and no new tester environment variable.

Files: `cases/test_dp19b_status.py`, `cases/test_dp19b_browser.py`, helpers in `cases/dp6c_support.py` (DP6c helpers reused: feeds, `SseReplay`, pacing, `send_rf`), snippet
`rig/config/dv-router/dp19b-rf.toml` (the bound port plus the text path for `XLX999B`), unit tests `cases/unit/test_dp19b_support.py` and `test_dp19b_browser_helpers.py` (needs playwright).

## Cases

| Id | What | Surfaces |
|---|---|---|
| DP19b-1 | From one base (`N0DPA`): a status `>IO91SX/G` (SSID 7), a plain status `>Ready` and the text message `IO91SX/G` (SSID 9, to the room name). All three on the public SSE and in the user message history (the data of the message panel); the first has `spatial: true`, the plain status `spatial: false`, the text no `spatial` key. The state's `spatial_status` holds the first only: plain status and text are on the history and the feed, not in the state (openapi `SpatialStatus`) | state, SSE, user history |
| DP19b-2 | `hide_position` of the account of the base (`N0DPB`): within one state refresh (the policy is at most 10 s old; the case allows 25 s and records the seconds) the spatial status leaves the anonymous public state and the user message history, and a fresh SSE replay (`Last-Event-ID` of the first event) carries none; the history keeps exactly the plain status and the text. The flag is cleared at the end | state, history, SSE replay |
| DP19b-2 browser | The same in the OPEN message panel of the anonymous dashboard (three engines, one base AND one body suffix per engine: an earlier engine's locator status is still retained and returns after its unhide, data.md 13.4, so the first rig run on wip-a31709a counted 2, 3 and 4 rows with a shared body; a failure now prints the panel text and the state's `spatial_status`): the panel of room B holds the spatial status, the text (same body) and the plain status; after the hide it holds the text and the plain status only. CSP reports fail the case | dashboard panel |
| DP19b-3 | F1: an open SSE stream and NO other request to the web service for 60 s, then a position and a spatial status of an unhidden base (`N0DPC-5`): both arrive live within 20 s; the spatial status has `spatial: true` | SSE |
| DP19b-4 | GAP, no case (dv-router, code at `52db4641`): see "GAPs and limits" | - |

Frames: RF-heard statuses on the bound port; the text is the APRS message `:XLX999B  :IO91SX/G` (no message id, so never acked) heard on the same port and admitted by the path of `dp19b-rf.toml`.
Pacing: one base is 1 status per 10 s and 1 text per 5 s, so a trio takes about 12 s; every case has its own base callsigns (`N0DPA`, `N0DPB`, `N0DPC`, `N0DPD`, browser `N0DPF`/`N0DPG`/`N0DPH`).

## Run shape and commands

Common: `XLXD_SUT_IMAGE=<image after wip-7bd0082 with 52db4641> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true`,
`R=rig/config/dv-router`, `W=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml`.

| Run | Command | Cases | About |
|---|---|---|---|
| D1 API | `DVT_SUT_CONFIG_EXTRA=$W:$R/dp19b-rf.toml scripts/run.sh -m rig --capture --profile web -- cases/test_dp19b_status.py` | DP19b-1, -2, -3 | 6 min |
| D2 browsers | `DVT_SUT_CONFIG_EXTRA=$W:$R/dp19b-rf.toml scripts/run.sh -m rig --capture --profile web,browser -- cases/test_dp19b_browser.py` | DP19b-2 browser x3 engines | 6 min |

Notes go to stdout and `out/<run>/dp6c.txt`. DP19b-3 needs the web service to see no other request for 60 s: run it in a run where no other client uses the web service at that time.

## GAPs and limits

| GAP | Why |
|---|---|
| `position_optout = ['N0CALL']` plus a configuration reload (the second way to hide in review 247 case 2) | needs a config change through the admin API with `DVT_SUT_CONFIG_RW=1`; the opt-out drop itself is DP19-a (`docs/dp6c-cases.md`), the live hide is covered by `hide_position` |
| The talker status (`talkerStatus`: the newest status of the on-air talker's base within 10 s of the over) | needs a voice over of the same base as the status; the panel reconciliation is covered, the talker line is the same filter |
| Restart of `dv-svc-web` with the accounts unavailable (review 247 case 4) | dv-router: `dv-svc-web` loads the account store at startup with a hard error (`Store::load(path)?`, `crates/svc/web/src/auth/mod.rs:142`; `accounts.rs:235-250`), so an unreadable store makes the child crash-loop and the case would test "web is down", not "spatial rows withheld". The fail-closed branch is about an unknown POLICY inside a RUNNING web; unit tests of review 247 cover it |
| Unknown hidden set before the first policy snapshot | the window after a normal start is a race; the unknown-policy branch is a unit-test matter (above) |
| Unhide returning rows admitted earlier | documented behaviour (rows withheld at admission never return); not asserted |
| Dedupe marker byte for status (`spatial` folded into the fingerprint) | internal to the controller (DV-Router tests) |
