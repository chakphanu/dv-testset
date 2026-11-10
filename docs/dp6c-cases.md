<!-- SPDX-License-Identifier: MIT -->
# DP6c and DP19 cases: weather, telemetry and locator status on the map

Status: written offline for the image with code `4ac79f26`; **nothing here has run on a rig**. They run after the deploy set and do not gate a GO unless they show a regression.
Sources (read only): DV-Router `docs/design/data.md` (3.1, 5.3, 8.2, 9.1, 9.2, 13.2, 13.4 "Built (DP19)", 13.5, 15.3, 15.5), `docs/design/web.md` ("Position map"), the golden frames G1-G19 of
`crates/lib/pkt/testdata/WXTLM.md`, the planned acceptance of the DP6c report and section 6 of review 244.

What changed (the key point): the public map rows are keyed by the **guard identity** (`N0WXA-13`, SSID kept), carry `hops` and `forwarded`, and privacy is per **base** callsign. Types 5 (weather) and 6
(telemetry) are annexes of a station: they appear in the positions view (public state `rooms[].positions[]`, public SSE `data`, the operator feed) and nowhere else (not in the message history, not in the
event views). The existing position cases are updated for the rows' new keys by a separate change; these cases never look up a row by the bare base.

Files: cases `cases/test_dp6c_rf.py`, `test_dp6c_aprsis.py`, `test_dp19_locator.py`, `test_dp6c_browser.py`, `test_dp6c_config.py` (cfgload), and OBSPKT1-g in `test_obspkt1_monitor.py`; helpers
`cases/dp6c_support.py` and `dvtest/dp6c.py` (the pure judging: metric conversions mirrored from `positions-station.js`, telemetry G9 under G10, row shape, config cases); frame builders in
`dvtest/proto/aprs.py` (G1 `weather_info`, G5/G6 `weather_position_info`, G7 `weather_compressed_info`, G9 `telemetry_info`, G10 `definition_info` / `telemetry_definitions`, DP19 `locator_status`,
`LOCATOR_VALID`, `LOCATOR_LOOKALIKES`, `is_locator_status`); snippets `rig/config/dv-router/dp6c-rf.toml`, `dp6c-aprsis-filter.toml`, `dp6c-aprsis-wx.toml`; unit tests `cases/unit/test_proto_aprs.py`,
`test_dp6c_support.py`, `test_dp6c_browser_helpers.py` (needs playwright), `test_web.py` (the SSE `Last-Event-ID` header). No new switch: every case skips with the reason when its shape is missing.

## Frames (unit round trips)

| Vector | Frame | Used by |
|---|---|---|
| G1 | `_10090556c220s004g005t077r000p000P000h50b09900wRSW` (positionless, month 11 in the cases) | DP6c-1 (N0WXC-5), DP6c-2 (N0WXJ-3), browser |
| G5 | `!4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW` | DP6c-1, DP6c-2, hide, opt-out, browser |
| G6 | `@092345z4903.50N/07201.75W_220/004g005t-07...` | builder and unit test (a rig case may add it) |
| G7 / G7z | `=/5L!!<*e7_7P[g005t077...` and the same with speed byte `z` | DP6c-8 |
| G9 | `T#005,199,000,255,073,123,01101001` | DP6c-1, DP6c-2, hide, browser |
| G10 | `PARM.` / `UNIT.` / `EQNS.` / `BITS.` messages from the SAME BASE callsign as the telemetering station (`N0WXA-1` defines `N0WXA-11`) | DP6c-1, DP6c-2, browser |
| DP19 | valid `>IO91/- text`, `>IO91SX/- text`, lower case `>io91sx\- text`, `>JN58/-`; look-alikes: leading space, no symbol pair, a space where the table byte belongs, field letter beyond R, a letter where a digit belongs, subsquare beyond X, five or eight characters, text without the leading space | DP19-a/b, OBSPKT1-g |

G9 under G10 gives the calibrated values `1034.8`, `-32.00`, `196243.45`, `-170291`, `15378` (hand arithmetic of WXTLM.md; the cases compare numerically, not the string), digital bits `01101001` with sense
`10110000` (active = bit equals sense: B3, B6, B7), names from PARM and labels from UNIT, project `N0CALL Balloon`. The weather expectation of G5 is the native body (wind 220 deg 4 mph, gust 5 mph, 77 F, rain 0,
humidity 50 %, 990.0 hPa, software `w`, unit `RSW`); the Weather column is `25.0 C 6 km/h`. G7 gives wind 88 degrees and 42 mph from `cs`; with `z` the speed is out of range and dropped.

## Confirmed by dv-router (at 4ac79f26)

| Assumption | Status |
|---|---|
| A telemetry definition (`PARM` / `UNIT` / `EQNS` / `BITS`) is accepted only when the sender's BASE callsign equals the addressee's base, case-insensitive (`crates/lib/pkt/src/aprs/telemetry/definitions.rs` 231-240; data.md line 141). It applies only to `T#` reports from that exact SSID; a definition that arrives late applies from the next report; a restart of the worker forgets every definition (the cases never restart it, so each run defines before it reports) | confirmed |
| The APRS-IS login filter is `g/<addressee>` plus `[edges.aprsis] filter_extra` only, so station reports need the filter of `dp6c-aprsis-filter.toml` | confirmed |

## Cases

| Id | What | Shape |
|---|---|---|
| DP6c-1 | One base `N0WXA` over RF: `-13` position + weather (G5), `-11` a position, the definitions from `N0WXA-1`, then G9, `-9` a separate position; `N0WXC-5` positionless weather (G1). The order is: the valid definition (from `N0WXA-1`), a FOREIGN definition (the four messages from `N0WXB-1` addressed to `N0WXA-11`, with other names, units, coefficients, sense and project), then the next `T#` report: the calibrated values, names, units, sense and project stay as `N0WXA-1` defined them and no foreign string appears in the body. Three position rows (one per SSID), weather and telemetry on the right stations, native bodies, calibrated telemetry, `hops` 0 and `forwarded` false, no row of the bare base, no marker for the weather-only station, rows on the public SSE (and the operator feed, recorded as a FINDING when absent), no map item in the user message history. Positions of one base are 31 s apart (the base bucket); weather and telemetry have their own buckets | R1 |
| DP6c-8 | G7z from `N0WXZ-2` keeps its position (49.5 N, 72.75 W) and its weather without `wind_mph` (the summary is `25.0 C`); control G7 `P` from `N0WXY-2` has 42 mph | R1 |
| DP6c-3 hide | Account `N0WXH` (created through the operator API): `-13` position + weather and `-9` position + telemetry are public; `hide_position` removes all four rows from the state on the next refresh, a reconnect with `Last-Event-ID` replays none, new items of the base are dropped (`dv_pkt_items_total{kind,result="optout"}` and `dv_room_data_total{result="optout"}` rise) and appear on no surface; clearing the flag brings a new weather report back (the case waits for the NEW report, `temp_f` 55: the row admitted before the hide may return while it is still retained, data.md 13.4; first rig run R2) | R1 |
| DP6c-3 opt-out | `position_optout` names `N0OPT`: G5, G9 and positionless G1 from three SSIDs are denied/13 (counters), absent everywhere; the control `N0WXK-13` is public | R2 |
| DP6c-3 off | `positions = false`: the same frames are all denied | R3 |
| DP6c-2 | Private aprsc, typed path (the login filter `p/N0WXI/N0WXJ` of `dp6c-aprsis-filter.toml` names BOTH station bases; the first rig run named N0WXI only and never received the positionless weather of N0WXJ-3): G5 from `N0WXI-3`, the G10 definitions from `N0WXI-1`, G9 from `N0WXI-4`, G1 from `N0WXJ-3`, each from its own verified login (the public passcode hash is computable). Weather and telemetry rows of the exact guard identity with the values, `via` and `ident` recorded, NO position row and no marker (IS positions stay refused, also the one inside G5) | R4 |
| DP6c-2 off | The same packets without the typed path: no row on any surface; the control is `dv_edge_raw_total{proc="aprsis"}` rising (the filter delivered the packets, the path rows refused them) | R5 |
| DP6c config | `dv-router config check`: the path `edge:aprsis -> room:B` with `weather` and `telemetry` loads (review 244 D3); an unknown type (alone or next to valid ones), a room source with weather, `room -> edge:aprsis` with weather are refused with a positioned error naming the data path | R9 |
| DP6c-4 | Browsers (Chromium, Firefox, WebKit) x EN and TH: the table row per station, the Weather column (headings read from the first table only: the details of a row hold nested tables), "No position received" in both coordinate cells of the weather-only station, "Show details" opens the native `<details>`: weather values metric first with the native unit in parentheses, telemetry names, calibrated values, units and raw values, the project; a `<b>` in a received name and an `<i>` in the project stay literal text (no element); only the page's origin is requested, no CSP report beyond the documented WebKit CSP2/CSP3 ones | R7 |
| DP6c-9 | The operator events page (`#/events`) has no row of the weather-only station and at most the position event of the others, and no weather or telemetry row; the case is about the RENDERED event display (`shared/event-display.js`), not the API: `status/events`, the operator feed and the SSE `data` events DO carry weather and telemetry (status.md, web.md), which the case only records | R7 |
| DP19-a | RF locator statuses: a private base's (position_optout `N0OPT`, or any base with `positions = false`) valid forms are all withheld BY THE WORKER before submission (`dv_pkt_items_total{kind="status",result="optout"}` rises, the controller's `dv_room_data_total` does not: first rig run R2), nine look-alikes and a plain status are public statuses, no valid locator status of the private base is on the public SSE or in the message history; a public base's valid locator status is a public status | R2, R3 |
| DP19-b | RF transit to the private aprsc (`dp7-rf-igate.toml`): the private base's valid locator statuses are not gated, a look-alike and a plain status are gated with the information field unchanged, a public base's locator status is gated | R6 |
| OBSPKT1-g | In the OBSPKT1 shape: the worker row of a hidden base's RF locator status is `SRC>DST,PATH:` with `redacted: position_policy`, a visible base's is in full, a look-alike and a plain status of the hidden base are in full, edge RX rows are header only, the local KISS client's locator status is blanked and marked in its TX row (look-alike and plain stay in full), and hiding the account later blanks the retained worker row | OBSPKT1 run |

## Run shapes and commands

Common (as `docs/dp7-cases.md`): `XLXD_SUT_IMAGE=<image with 4ac79f26> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true`,
`R=rig/config/dv-router`, `W=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml`. Runtimes are estimates (the 31 s gaps of one base dominate).

| Run | `DVT_SUT_CONFIG_EXTRA` and command | Cases | About |
|---|---|---|---|
| R1 RF | `$W:$R/dp6c-rf.toml` `scripts/run.sh -m rig --capture --profile web -- cases/test_dp6c_rf.py` | DP6c-1, -8, -3 hide (the opt-out and positions-off cases skip) | 8 min |
| R2 RF opt-out | `$W:$R/dp6c-rf.toml:$R/dp7-position-optout.toml`, `--profile web -- cases/test_dp6c_rf.py cases/test_dp19_locator.py` | DP6c-3 opt-out, DP19-a (the DP6c-1 family runs too) | 10 min |
| R3 positions off | `$W:$R/dp6c-rf.toml:$R/dp7-positions-off.toml`, `--profile web -- cases/test_dp6c_rf.py cases/test_dp19_locator.py` | DP6c-3 off, DP19-a | 6 min |
| R4 aprsc, typed path | `$W:$R/dp15-aprsis.toml:$R/dp6c-aprsis-filter.toml:$R/dp6c-aprsis-wx.toml`, `--profile web,aprs -- cases/test_dp6c_aprsis.py` | DP6c-2 | 4 min |
| R5 aprsc, no weather types | `$W:$R/dp15-aprsis.toml:$R/dp6c-aprsis-filter.toml`, `--profile web,aprs -- cases/test_dp6c_aprsis.py` | DP6c-2 off | 3 min |
| R6 transit | `$W:$R/dp15-aprsis.toml:$R/dp6c-rf.toml:$R/dp7-rf-igate.toml:$R/dp7-position-optout.toml`, `--profile web,aprs -- cases/test_dp19_locator.py` | DP19-b (and DP19-a) | 8 min |
| R7 browsers | `$W:$R/dp6c-rf.toml`, `--profile web,browser -- cases/test_dp6c_browser.py` | DP6c-4 x3 engines x2 languages, DP6c-9 x3 | 12 min |
| R8 OBSPKT1 | the OBSPKT1 run of `docs/obspkt1-cases.md` | OBSPKT1-g | adds 2 min |
| R9 config (the APRS-IS passcode file stand-in holds a valid passcode: the router validates its content; first rig run) | `DVT_CFGLOAD_IMAGE=<image> DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_dp6c_config.py` | DP6c config | 10 s |

R2 and R3 are separate runs because `position_optout` and `positions = false` exclude each other. R1 may carry the aprsc as well (`dp15-aprsis.toml`, `dp6c-aprsis-filter.toml`, `dp6c-aprsis-wx.toml`, profile
`web,aprs`) to run DP6c-2 in the same run; R5 needs the shape WITHOUT `dp6c-aprsis-wx.toml`. The notes go to stdout and `out/<run>/dp6c.txt` ("RECORD" = what was seen, "FINDING" = differs from the
documented behaviour).

## GAPs and limits

| GAP | Why |
|---|---|
| The marker click that opens and focuses the station details | the marker is a WebGL feature without a DOM element; the existing position cases count markers from pixels |
| The admin SPA event view | the admin host needs a TOTP sign-in; the operator events page is covered |
| Embedded M17 locator status; DP19 over APRS-IS as a source | the rig has no room-bound M17 status seam for a private base; IS positions and statuses never enter a room, so the IS side of DP19 is the RF-to-IS gate (DP19-b) |
| The 30 minute retention and the 512-entry annex store | a constant and a bound DV-Router tests; the rig would need half an hour or 512 stations |
| Telemetry without definitions (raw values only) | `dvtest/dp6c.telemetry_problems(defined=False)` is unit tested; a rig case would need a station whose definitions were never heard (and the 30 s telemetry bucket) |
| Rate-limited weather and telemetry (1 per 30 s per base, a `denied/10`) | the cases pace their frames and never send two of one kind from one base inside 31 s |
