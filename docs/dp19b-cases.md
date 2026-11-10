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
bound RF port. The one opt-in case (DP19b-4) has its own switch because it is destructive.

Files: `cases/test_dp19b_status.py`, `cases/test_dp19b_browser.py`, helpers in `cases/dp6c_support.py` (DP6c helpers reused: feeds, `SseReplay`, pacing, `send_rf`), snippet
`rig/config/dv-router/dp19b-rf.toml` (the bound port plus the text path for `XLX999B`), unit tests `cases/unit/test_dp19b_support.py` and `test_dp19b_browser_helpers.py` (needs playwright).

## Cases

| Id | What | Surfaces |
|---|---|---|
| DP19b-1 | From one base (`N0DPA`): a status `>IO91SX/G` (SSID 7), a plain status `>Ready` and the text message `IO91SX/G` (SSID 9, to the room name). All three on the public SSE and in the user message history (the data of the message panel); the first has `spatial: true`, the plain status `spatial: false`, the text no `spatial` key. The state's `spatial_status` holds the first only: plain status and text are on the history and the feed, not in the state (openapi `SpatialStatus`) | state, SSE, user history |
| DP19b-2 | `hide_position` of the account of the base (`N0DPB`): within one state refresh (the policy is at most 10 s old; the case allows 25 s and records the seconds) the spatial status leaves the anonymous public state and the user message history, and a fresh SSE replay (`Last-Event-ID` of the first event) carries none; the history keeps exactly the plain status and the text. The flag is cleared at the end | state, history, SSE replay |
| DP19b-2 browser | The same in the OPEN message panel of the anonymous dashboard (three engines, one base per engine): the panel of room B holds the spatial status, the text (same body) and the plain status; after the hide it holds the text and the plain status only. CSP reports fail the case | dashboard panel |
| DP19b-3 | F1: an open SSE stream and NO other request to the web service for 60 s, then a position and a spatial status of an unhidden base (`N0DPC-5`): both arrive live within 20 s; the spatial status has `spatial: true` | SSE |
| DP19b-4 | Opt-in (`DVT_DP19B_ACCOUNTS_DOWN=on`), destructive, LAST in its run: `dv-svc-web` restarted while its account store is unreadable (see below). Spatial rows are withheld (not on the SSE, not in the state), text and plain status are still delivered on the SSE; the store is restored and the service restarted again at the end | SSE, state |

Frames: RF-heard statuses on the bound port; the text is the APRS message `:XLX999B  :IO91SX/G` (no message id, so never acked) heard on the same port and admitted by the path of `dp19b-rf.toml`.
Pacing: one base is 1 status per 10 s and 1 text per 5 s, so a trio takes about 12 s; every case has its own base callsigns (`N0DPA`, `N0DPB`, `N0DPC`, `N0DPD`, browser `N0DPF`/`N0DPG`/`N0DPH`).

## How the rig makes accounts unavailable (DP19b-4)

The accounts and hide flags of `dv-svc-web` live in `out/<run>/sut-state/web/accounts.jsonl`, which the tester sees (the basemap cases already write to `sut-state`). The case moves that file aside
and puts a DIRECTORY of the same name in its place, so that a read fails instead of finding nothing (a missing file could mean an empty, known store); then it kills `dv-svc-web` through the allow-listed
control channel (`kill-proc xlxd-sut dv-svc-web KILL`, the launcher respawns it) and waits until the public state answers twice. Undo: remove the directory, move the file back, restart again. Web
sessions end with each restart, so the case reads only anonymous surfaces and must run last. If dv-router answers that web refuses to start with an unreadable store, the case would fail at the
wait with "did not come back"; that is the question to ask. Not tried on a rig.

## Run shape and commands

Common: `XLXD_SUT_IMAGE=<image after wip-7bd0082 with 52db4641> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true`,
`R=rig/config/dv-router`, `W=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml`.

| Run | Command | Cases | About |
|---|---|---|---|
| D1 API | `DVT_SUT_CONFIG_EXTRA=$W:$R/dp19b-rf.toml scripts/run.sh -m rig --capture --profile web -- cases/test_dp19b_status.py` | DP19b-1, -2, -3 (DP19b-4 skips) | 6 min |
| D2 browsers | `DVT_SUT_CONFIG_EXTRA=$W:$R/dp19b-rf.toml scripts/run.sh -m rig --capture --profile web,browser -- cases/test_dp19b_browser.py` | DP19b-2 browser x3 engines | 6 min |
| D3 accounts down | `DVT_DP19B_ACCOUNTS_DOWN=on DVT_SUT_CONFIG_EXTRA=$W:$R/dp19b-rf.toml scripts/run.sh -m rig --capture --profile web -- cases/test_dp19b_status.py -k dp19b_4` | DP19b-4 | 4 min |

`DVT_DP19B_ACCOUNTS_DOWN` is passed to the tester by `rig/compose.yml`. Notes go to stdout and `out/<run>/dp6c.txt`.

## GAPs and limits

| GAP | Why |
|---|---|
| `position_optout = ['N0CALL']` plus a configuration reload (the second way to hide in review 247 case 2) | needs a config change through the admin API with `DVT_SUT_CONFIG_RW=1`; the opt-out drop itself is DP19-a (`docs/dp6c-cases.md`), the live hide is covered by `hide_position` |
| The talker status (`talkerStatus`: the newest status of the on-air talker's base within 10 s of the over) | needs a voice over of the same base as the status; the panel reconciliation is covered, the talker line is the same filter |
| Unknown hidden set before the first policy snapshot | DP19b-4 covers the accounts-unavailable restart; the window after a normal start is a race |
| Unhide returning rows admitted earlier | documented behaviour (rows withheld at admission never return); not asserted |
| Dedupe marker byte for status (`spatial` folded into the fingerprint) | internal to the controller (DV-Router tests) |
