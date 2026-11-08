<!-- SPDX-License-Identifier: MIT -->
# DP7 position cases: public positions, drop switches, ambiguity, object kill (written, not run on a rig)

DV-Router DP7 (commit ef4ecba, image `dv-router:wip-ac750c7`) changed the position rules of `data.md` 13.4: EVERY admitted
position and object is PUBLIC (anonymous public state, public SSE `data` topic, public `#/positions` page); PRIVATE means
DROPPED before publication (`[data] positions = false`, `[data] position_optout`, an account's `hide_position`, matched by
base callsign on every source including RF whole-packet transit and the IS-to-RF courtesy posit); the position body carries
the APRS ambiguity level (29 byte layout, `data.md` section 5) and the page prints the precision the station sent; an object kill is a
coordinate-free event. The DP15 position cases of the first rig runs expected the opposite (operator scope only, anonymous
visibility only recorded); they are rebased (below) and the DP7 cases are new.

Files: `cases/test_dp15_positions.py` (the cases), `cases/position_support.py` (display rule, APRS strings with an ambiguity level,
object builder, row helpers), unit tests `cases/unit/test_position_support.py` and `cases/unit/test_dp15_positions_helpers.py`,
snippets `rig/config/dv-router/dp7-*.toml`. Notes of a run: stdout and `out/<run>/dp15-positions.txt` ("RECORD" = what was seen,
"FINDING" = differs from the documented behaviour).

Authority (read only): DV-Router `docs/design/data.md` sections 5, 9.1, 9.5, 13.2, 13.4; `web/README.md` "Position map and
position privacy"; `docs/api/openapi-web.yaml` (Position, PositionBody, ObjectPositionBody, `PATCH /api/v1/operator/accounts/{callsign}`);
`web/site/positions.js` (`formatCoordinate`); `crates/room/src/telemetry.rs`, `crates/data/worker/src/core.rs` and `core_rf*.rs` (counters, drop paths).

## What a position looks like on the surfaces

* A row (public state `rooms[].positions[]`, public SSE `data` event, operator feed `GET /api/v1/operator/events?type=data`):
  `{type "data", id, item_type "position"|"object", room, callsign, protocol, ts_ms, ident, via, body}` (the admin feed adds `hops`,
  `forwarded`). The public row has ONLY those keys (openapi `additionalProperties: false`; no radio id, account, origin, address).
* `body` of a position: `lat_e7, lon_e7, alt_dm, course_deg, speed_dmh, fix_unix_ms, symbol, source (1 D-PRS, 3 YSF, 4 DMR, 5 APRS), ambiguity (0..4), comment`;
  an object adds `name` and `state` (1 live). A KILL body is `{name, state: 0}` and nothing else.
* `ambiguity` = the number of minute digits the station omitted; `lat_e7` / `lon_e7` are the CENTRE of that box (the parser sets the first blanked
  digit to 5, the rest to 0; level 4 is 30 minutes). It is 0 for every GPS fix and nonzero only for source 1 and 5.
* The page prints decimal degrees with 5 places for level 0 and `4 - level` places for 1..4, the centre truncated toward zero
  (`formatCoordinate`, ported to `position_support.format_coordinate`, unit-tested against hand vectors; the JavaScript itself is NOT run: there is no browser in
  these cases, "the public positions listing" is the anonymous `GET /api/v1/public/state` the page reads, plus the SSE it merges).
* Controller limits per base callsign: position and object share 1 per 30 s (`denied/10`, result `rate`). Every case therefore uses its OWN base callsigns
  (module constants of `cases/test_dp15_positions.py`; a unit test checks they are distinct), `data_support.PACER` (with `Pacer.offered` right after the send)
  spaces the cases that must reuse one. A DROPPED item spends no credit (admission step 5 comes before the rate step), so the opted-out callsign can be sent repeatedly.

## Case table

| Case | DP7 statement | Steps | Asserted | Recorded |
|------|---------------|-------|----------|----------|
| DP7-a `[dstar,m17,ysf,dmr]` | "every admitted position ... is public" | one fix (10.5 N 20.25 E) per source from its own callsign: D-STAR D-PRS beacon (source 1), M17 packet-mode APRS (5), YSF GPS fix (3, symbol `/[`, comment FT-1D), DMRG (4, symbol `/-`) | the position is in the ANONYMOUS public state, on the anonymous public SSE and in the operator feed; the public rows have only the allow-listed keys; source, symbol, coordinates right; ambiguity 0; never in the user message history | protocol/ident/via of the public row, SSE latency, operator body keys |
| DP7-b `positions=false` `[7 sources]` | "`[data] positions = false` drops all positions and objects" | run with `dp7-positions-off.toml`; one item per source (D-STAR, M17 position, M17 object, YSF, DMR, RF position, RF object) | `dv_pkt_items_total{kind,result="optout"}` and `dv_room_data_total{result="optout"}` rise; NOTHING on the public state, the public SSE, the operator feed; the D-STAR over's TX-message `status` IS admitted (control) | counter deltas |
| DP7-b `optout` `[7 sources]` | "`position_optout` matches the base callsign on every source" | run with `dp7-position-optout.toml` (`N0OPT`, `N0DMP`); per source a control callsign passes, then `N0OPT` and `N0OPT-9` (`N0DMP` for DMR; no SSID for YSF/DMR) are sent | control public on all three surfaces; the opted-out ones: counters rise, absent on all three surfaces | counter deltas |
| DP7-b `hide_position` | "account `hide_position` ... same drop" | account `N0HIDE` registered through the operator; M17 position public; operator `PATCH /api/v1/operator/accounts/N0HIDE {hide_position:true}`; D-STAR, M17, YSF and M17 object of `N0HIDE` sent; flag cleared; a new M17 position | the retained row leaves the anonymous public state; the four sends are dropped (counters, absent on all surfaces); after clearing the flag a new position (comment `again`) is public | whether the row left |
| DP7-b `rf transit` (profile aprs + RF) | "the worker applies the same controls to direct RF transit" | fake KISS TNC hears a position and an object of `N0PTC-7/-8` (control) and of `N0OPT-7/-9`; private aprsc, tester login `N0TEST-7` | the control position reaches the aprsc through the path `dp7-igate`; nothing of `N0OPT` reaches it in 15 s; with `positions=false` nothing at all | the lines at the aprsc |
| DP7-b `uplink` (profile aprs) | "dropped ... before publication, rendering, ride-along or uplink" | D-STAR D-PRS beacon of `N0PUC` (control), then of `N0OPT`, path `dp7-aprsis-up` (room B to APRS-IS) | the control is uplinked (line from `N0PUC` at the aprsc); the opted-out beacon is counted `optout` and never uplinked | uplink line |
| DP7-c `m17 level 0..4` | "the position body carries the APRS ambiguity level; the page rounds to the precision the station sent" | `4903.50N/07201.75W` with 0..4 blanked digits (`dvtest.aprs_cases` strings) in an M17 packet, source 5 | body `ambiguity` = level on the public SSE, the public state and the operator feed; `lat_e7`/`lon_e7` = the box centre (within 2 e7 units); `format_coordinate` of the stored value equals the text derived from `dvtest.aprs_expect`'s exact value (`49.05833 -72.02917`, `49.059 -72.029`, `49.05 -72.02`, `49.0 -72.0`, `49 -72`) | the texts |
| DP7-c `rf level 1..4` | the same for RF-heard APRS | the fake TNC rf-a (port bound to room B) hears the same positions | as above | the texts |
| DP7-c `d-prs level 2` | "nonzero only for source 1 D-PRS and 5 APRS"; 9.5 uplink "ambiguity re-applied" | D-PRS GPS-A line with two blanked digits from `N0PDA`; with the uplink path the tester reads the uplinked line | source 1, ambiguity 2, page text `49.05 -72.02`; the uplinked report is `!` or `=` followed by `4903.  N/07201.  W>` | the uplink line |
| DP7-c `gps ambiguity 0` | "0 for every GPS fix" | reads the ledger of DP7-a | YSF GPS, DMRG, the full precision D-PRS beacon and the M17 position have ambiguity 0 on all three surfaces | |
| DP7-d | "an object kill emits only its name and killed state, without coordinates" | M17 packet object `;DVT7OBJ  *HHMMSSh...` from `N0PDO-7`; 34 s later (shared bucket) the kill `_`; HMS timestamps so the kill is later than the live object | live object public (SSE `object`, state, operator); the kill event is `item_type object`, body `{name, state 0}`, no key starting `lat`/`lon` at any depth; the object leaves the anonymous public listing | the kill event, the operator kill row; retention of a live object is 30 minutes (data.md 13.2), NOT waited for |

The GPS half of DP7-c is asserted from the same rows as DP7-a (one send per source; the case skips if DP7-a did not run in the session).

## Drop evidence

* Controller: denied/13 is counted `dv_room_data_total{room,result="optout"}` (telemetry.rs, the result list); the data worker counts the same submit
  `dv_pkt_items_total{kind="position"|"object",result="optout"}` (core.rs `denial()`). Both are published every 10 s, so the cases poll up to 30 s and
  assert that BOTH rose by at least 1 (a YSF over carries its GPS record twice and may be counted twice: the delta is recorded).
* Absence: after the counters moved, 6 s later no position/object of the base callsign is on the anonymous public state, on the public SSE (opened before the send)
  or in the operator feed (read from the baseline taken before the send).
* RF transit and the APRS-IS uplink have no item and no counter for the dropped packet: the evidence is absence at the private aprsc, after a control
  packet proved the path in the same run.

## Rebased cases (`cases/test_dp15_streams.py`)

1b, 2d, 4a and 4b used to RECORD the anonymous visibility ("public rule is being revised in DP7"); they now ASSERT it (`assert_anonymous_view`): a `position` event on the anonymous
public SSE data topic (watch opened before the send) and a row in the anonymous public state, with the allow-listed keys, the expected source (1, 5, 3, 4), symbol, ambiguity 0, and no
position in the user message history. The operator-scope assertions are unchanged. Position helpers moved to `cases/position_support.py` (`check_position`, `operator_baseline`,
`wait_operator_rows`, `position_rows`); `record_anonymous_view` is gone.

## Run shapes and commands

Common prefix (`<template>` = the DV-Router rig template as in `docs/dp15-cases.md`; `R` = `rig/config/dv-router`):

```
XLXD_SUT_IMAGE=dv-router:wip-ac750c7 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
```

1. DEFAULT, profile `web` (DP7-a, DP7-b optout for D-STAR / M17 / YSF / DMR / M17 object and hide_position, DP7-c M17 and gps, DP7-d; the RF, transit and uplink cases skip with the reason). Estimated 25 minutes (UNVERIFIED).

```
DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml:R/dp7-position-optout.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_positions.py
```

2. RF and APRS-IS, profile `web,aprs` (everything of 1, plus RF-heard position/object optout, DP7-c RF levels, DP7-c D-PRS uplink, DP7-b transit and uplink). `scripts/pre-fetch-aprsc.sh` once (the only internet use). Estimated 35 to 40 minutes (UNVERIFIED).

```
DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml:R/dp15-aprsis.toml:R/dp7-rf.toml:R/dp7-rf-igate.toml:R/dp7-aprsis-up.toml:R/dp7-position-optout.toml \
scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp15_positions.py
```

3. POSITIONS OFF, a SEPARATE run (`[data] positions = false` and the opt-out cases are mutually exclusive; each skips with the reason in the other shape). Use the shape of 2 with `dp7-positions-off.toml` in place of `dp7-position-optout.toml` (or the shape of 1 for the five non-RF sources). Runs only DP7-b `positions=false` (7 sources) and the "nothing is gated / uplinked" modes of transit and uplink. Estimated 10 minutes (UNVERIFIED).

```
DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml:R/dp15-aprsis.toml:R/dp7-rf.toml:R/dp7-rf-igate.toml:R/dp7-aprsis-up.toml:R/dp7-positions-off.toml \
scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp15_positions.py
```

4. The rebased DP15 cases keep their own run (`docs/dp15-cases.md` "Run"): `... DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml:R/dp15-paths.toml:R/xlx-peer-tester.toml scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_streams.py`
   (also runnable in the same session as shape 1: `-- cases/test_dp15_positions.py cases/test_dp15_streams.py`; the pacer is shared and the callsigns differ).

Notes on the snippets (`rig/config/dv-router/`):

| Snippet | Content | Note |
|---------|---------|------|
| `dp7-position-optout.toml` | `[data] position_optout = ["N0OPT", "N0DMP"]` | merged into the `[data]` table of `data-ident.toml`, order free; exact base callsigns (no wildcard); `N0DMP` is the callsign of DMR id 1023230 in `rig/config/dmrid.dat` |
| `dp7-positions-off.toml` | `[data] positions = false` | the separate run |
| `dp7-rf.toml` | `[edges.ax25]` with ONE kiss-tcp port `rf-a` (`<TESTER_IP>:8101`) bound to `room = "B"`, receive only, station callsign `N0TEST-1` | INSTEAD of `dp15-rf.toml` (both define `[edges.ax25]`); a room-bound port publishes the positions/objects it hears into room B without a `[[data.path]]` |
| `dp7-rf-igate.toml` | transit path `edge:ax25/rf-a -> edge:aprsis` | needs `dp15-aprsis.toml` and `dp7-rf.toml` (the launcher refuses a path to an edge that is not enabled) |
| `dp7-aprsis-up.toml` | path `room:B -> edge:aprsis`, `types = ["position"]`, `min_ident = "stream"`, `per_call_secs = 60` | needs `dp15-aprsis.toml`; the opt-in uplink of data.md 9.5 |

Config check (host side, throw-away container, `--network none`; all `valid` with `dv-router:wip-ac750c7`, done):
`python3 -m dvtest.tools.config_check --image dv-router:wip-ac750c7 --template <template> --gen compat1 --extra R/data-ident.toml --extra R/dp7-position-optout.toml`, the same with `dp7-positions-off.toml`, with `dp7-rf.toml`, and with
`--extra R/data-ident.toml --extra R/dp15-aprsis.toml --extra R/dp7-rf.toml --extra R/dp7-rf-igate.toml --extra R/dp7-aprsis-up.toml --extra R/dp7-position-optout.toml`. `dp7-rf-igate.toml` without `dp7-rf.toml` is INVALID (AX.25 endpoint not enabled), as expected.

## Findings

* **DP7-FIND1 (product, first rig run wip-ac750c7, out/20261107T144128Z): the D-PRS path drops an ambiguous position.** `DP7-c[d-prs level 2]` sends `N0PDA-...>API510,DSTAR*:!4903.  N/07201.  W>amb2` (CRC valid, the 19 byte field keeps its width; the builder is unit tested and the same position passes as full precision D-PRS in DP7-a/DP7-b and as ambiguity 2 in M17 packet mode). The anonymous SSE, the public state and the operator feed show NO position; the operator feed shows only the TX message of the over as a `status` item (body `DVT DP7 2811`, ident `stream`, protocol `dextra`, room B). Hub counters (control dumps 8-73 and 8-74, the window holds the M17 level cases and this one, and this is the only D-STAR over in it): `dv_pkt_items_total{kind="status",result="accepted"}` 5 -> 6 (the TX text), `dv_pkt_reject_total{kind="dstar-slowdata",proc="pkt",reason="bad_position"}` 0 -> 1 (first and only occurrence in the run), `dv_pkt_items_total{kind="position"}` 9 -> 15 is the six M17 level reports. `reason="invalid"` 5 -> 6 moves by about one per D-STAR over in every earlier dump too and is not specific to this case.
  Cause (read-only): `crates/lib/pkt/src/dstar_dprs.rs:105-106` passes the latitude and longitude digits to `position.rs:137 coordinate()`, which calls `decimal()` (`position.rs:117-125`: digits, `.` and `-` only), so a blanked digit gives `Reject::BadPosition`; `empty_position(1)` (`dstar_dprs.rs:104`) is never given an ambiguity. The APRS path does honour it (`crates/lib/pkt/src/aprs/position.rs:226-257`, box centre) and the M17 level 0..4 cases pass.
  Design: `docs/design/data.md:505` (ambiguity "nonzero only for sources 1 D-PRS and 5 APRS"), `:696` (GPS-A "the info field then goes through the APRS position parser (9.1)"), `:2339` and `:1727` (level carried, re-applied by the D-PRS ride-along and the uplink); the DP5a "Built scope" paragraph (`:709-711`) describes an own uncompressed `!`/`=` decoder and says nothing about ambiguity. Either the D-PRS decoder must use the 9.1 position parser (then DP7-c d-prs passes unchanged) or data.md 5/696/2339 must say source 1 is never ambiguous (then the case becomes "an ambiguous D-PRS position is refused with bad_position", and the uplink half is dropped).
  The case keeps asserting the documented behaviour; its failure text starts with `FINDING DP7-FIND1`. Rerun after a product change: `DP7-c[d-prs level 2]` alone, plain run for the room side, profile `web,aprs` run for the uplink half (which has not run yet).

## Written against the described shape, UNVERIFIED

* The operator feed body field `ambiguity` (taken from data.md 5 / 13.2 and the openapi PositionBody; the admin row is described as "the position fields"). DP7-c asserts it; DP7-a records a FINDING when it is missing.
* The kill event row on the public SSE: `item_type object`, body `{name, state 0}` (openapi Position schema; data.md 13.2 "emits only its name and killed state"). The row's other keys are checked against the allow-list.
* "The public positions listing" = `rooms[].positions` of the anonymous public state (what `#/positions` reads, web/README.md); no JSON route of its own exists in the openapi. The page text is computed with a port of `formatCoordinate`, not by running the page.
* Whether a D-STAR talker linked as a DExtra client reaches ident `stream` for the uplink path (`min_ident = "stream"`). The uplink cases ASSERT the control line and fail with that reason when it is missing (the absence check would prove nothing).
* The flag change timing: 3 s after `PATCH ... hide_position` the policy is assumed pushed to the controller and the retained row filtered at the web process (web/README.md: "a marker already open when a hide takes effect disappears on the next state refresh").
* Counter labels: `result="optout"` for denied/13 on both counters (read from telemetry.rs and core.rs, not from a running hub).
* The D-PRS uplink render re-applies the blanked digits (`!` or `=` then `4903.  N/07201.  W>`): data.md 9.5.
* Object retention: 30 minutes and the 1024 shared entries are quoted, not tested.

## Assumptions and deviations

* The lead wrote that D-PRS from a GPS-A beacon never claims ambiguity. data.md 5 says the byte is "nonzero only for sources 1 D-PRS and 5 APRS", and a GPS-A line is an APRS information field: a beacon with every digit has ambiguity 0 (asserted, DP7-a and the ledger case), a beacon with blanked digits carries the level (asserted, DP7-c d-prs). The NMEA GPS mode of a D-STAR radio (`$GPRMC` + `$GPGGA` + ID line) is not written.
* APRS-IS DOWNLINK positions are never admitted into a room (data.md 13.4), so the "APRS-IS" source of the drop cases is the UPLINK (a room position going up) and the RF-to-IS transit; there is no APRS-IS to room position to send.
* Only `N0OPT`/`N0DMP` are opted out; the account case uses `N0HIDE` over D-STAR, M17, YSF and an M17 object (DMR needs a registry callsign, RF is covered by the opt-out case).
* Not covered by the FIRST set: the IS-to-RF courtesy posit, hide_position for RF, the browser DOM of `#/positions`, SSE replay after a hide, items, the 30 minute retention, the strict ETag case: all written for the next image in "DP7 second set" below. Still not covered: NXDN positions (after the HWFREE1 generator) and object ambiguity levels.

# DP7 second set (written for the next image, NOT RUN)

For the image that carries MAP1 (the DOM cases: MAP2, image `220e45b` = source commit `8ffab80`), ETAG1, W8A1, XCOBS1, LSTOP1 and DPRS1 (tag not known when this was written; the snippets below were
checked with `dv-router:wip-ac750c7`, which parses all of them). Nothing here was run on a rig. Authority read (DV-Router tree, read
only): `docs/design/data.md` 8.3 / 8.9 / 9.3 / 13.2 / 13.4, `docs/design/config.md` (`[data]`, `[[data.path]]`, `[edges.aprsis]`,
`[[edges.ax25.port]]`), `docs/design/deployment.md` "Optional position basemap (MAP1; MAP2b / MAP2c for the DOM case)", `web/README.md`, `web/site/positions.js`,
`positions-map.js`, `public.js`, `scripts/basemap-build.py`, `docs/api/openapi-web.yaml` (Stream, LastEventId),
`crates/data/worker/src/core_rf_tx.rs`, `core_rf_state.rs`, `rf_tx_tests.rs`, `crates/lib/pkt/src/aprs/igate.rs`, `object.rs`,
`crates/lib/config/src/data_path.rs`, `crates/svc/status/src/hub/data.rs`.

Files: `cases/test_dp15_positions2.py` (DP7-e to DP7-i, imports the helpers and the `hub` / `web_ctx` / `aprsc` fixtures of
`cases/test_dp15_positions.py`), `cases/test_dp7_positions_page.py` (Playwright, three engines), the strict ETag case in
`cases/test_web_etag_diag.py`, helpers `cases/position2_support.py`, selectors in `cases/web_pages.py` ("#/positions"), unit tests
`cases/unit/test_position2_support.py`, `test_dp15_positions2_helpers.py`, `test_dp7_positions_page_helpers.py` (needs playwright),
snippets `rig/config/dv-router/dp7-courtesy-*.toml`, tester environment `DVT_BASEMAP_FILE` and `DVT_DP7_RETENTION_LONG`
(`rig/compose.yml`). Notes of a run: `out/<run>/dp7-positions2.txt` and stdout ("RECORD", "FINDING").

## Case table

| Case | Design statement | Steps | Asserted | Recorded |
|------|------------------|-------|----------|----------|
| DP7-e `courtesy` | data.md 8.3 "IS to RF": positions are never gated to RF except ONE courtesy position of the station whose message was just gated to RF, when `courtesy_posit = true` | the fake TNC rf-a hears `N0PRX-2`; the verified APRS-IS login `N0PSD-1` sends a message to it, then a position, then a second position | the message is transmitted: `N0TEST-1>APZDVR:}N0PSD-1>APDVT1,TCPIP,N0TEST-1*::N0PRX-2  :text{1`; ONE more third-party frame carries the position exactly as sent; the second position is not transmitted | frame texts, delay, `dv_pkt_rf_drop_total{reason}` and `dv_pkt_rf_downlink_total{result}` |
| DP7-e `no credit` | `Courtesy::take` needs a credit made by a gated message | `N0PSN-1` sends a position with no message before | no frame from it in 14 s | counters |
| DP7-e `optout` | data.md 13.4 / config.md: `[data] positions` and `position_optout` apply to the courtesy posit | `N0OPT-1` (opt-out run) or `N0PSB-1` (`positions = false` run): message, then position | the message IS transmitted (control), the position is not | counters |
| DP7-e `hide_position` | data.md 13.4: the account hidden set applies to the courtesy posit | account `N0PSH` hidden: message (control frame), position; flag cleared: message, position | hidden: no position frame; cleared: the position is transmitted | whether it was |
| DP7-e `path off` | config.md `courtesy_posit` default false | path `dp7-is-to-rf` with `courtesy_posit = false` | message transmitted, position not | |
| DP7-e `tx off` | config.md: no path to a port without effective TX; `tx_enable` false | receive-only run, same sequence | NO data frame at the TNC in 22 s | frame count |
| DP7-f `hide rf` | data.md 13.4: `hide_position` applies to RF-heard APRS and to RF transit | account `N0PFH`: RF position public; flag set; retained row leaves the state; RF position and RF object dropped; transit (with `dp7-rf-igate.toml`): control `N0PFC` reaches the aprsc, `N0PFH` does not; flag cleared: RF position public again | counters `optout` + absence on state / SSE / operator feed; no line at the aprsc | whether the row left |
| DP7-g `replay` | web/README.md: SSE "streamed/replayed" rows are filtered with the hidden set; openapi Stream: hidden accounts omitted | live SSE sees marker `N0PGM`, control `N0PGK`, `N0PGH`; cursor = id of the marker's event; `N0PGH` hidden; NEW connection with `?last_event_id=<cursor>`; plain new connection; flag cleared | before the hide the replay holds both; after: state lacks `N0PGH`, replay holds `N0PGK` and NOT `N0PGH`, plain connection has no `N0PGH` row; cleared: the state row and a new replay hold `N0PGH` again (design-implied, UNVERIFIED) | frame ids, event names |
| DP7-h `item m17` / `item rf` | data.md 8.3 objects/items; `object.rs` | `)DVT7ITM!1030.00N/02015.00E>` (3..9 char name, no timestamp), 34 s later the kill `)DVT7ITM_...` over M17 packet mode and over RF | live item public on the three surfaces (`item_type object`, name, state 1, source 5); kill event body `{name, state 0}`, no key starting `lat`/`lon` at any depth; item leaves the listing | rows, keys |
| DP7-i `retention` | data.md 13.2 / `hub/data.rs`: 30 minutes, a CONSTANT | one M17 position; present at 2 and 25 min; gone by 33 min | as stated | skipped unless `DVT_DP7_RETENTION_LONG=1` (about 35 minutes, own run) |
| DOM `without a basemap` (3 engines) | web.md MAP2c: "Missing, incomplete or MANIFEST v1 installs report `installed: false` and retain the list fallback"; `crates/svc/web/src/basemap.rs` (tiles.json `{"installed":false}`, ETag `"not-installed"`, `no-cache`; every other `/basemap/` path 404 `no-store`); `positions-map.js` `mount` | `<state>/basemap/` absent; HTTP from the tester, then `#/positions`, room C selected | tiles.json 200 `{"installed":false}`, content-type JSON, ETag `"not-installed"`, `cache-control: no-cache`, 304 with that ETag; 404 `no-store` for `/basemap/MANIFEST`, `/basemap/`, `regions/INDEX`, `glyphs/FONT-FACES.json`, a glyph range, tiles of two well-formed generations; page: `Basemap not installed.` shown, map container has class `hidden` and is not visible, no attribution, table rows = the public rows of room C with coordinates printed at the sent precision, tiles.json is the ONLY request under `/basemap/` (200, or 304 on a revalidation), the browser requested ONLY the page's origin | requests, findings |
| DOM `refuses a v1 basemap` (3 engines) | deployment.md MAP2b: "MANIFEST v1 (MAP1) is refused under COMPAT1"; web.md MAP2c: a v1 install reports `installed: false` | the MAP1 basemap of `DVT_BASEMAP_FILE` (old tarball, directory with MANIFEST, or bare archive) copied to `out/<run>/sut-state/basemap/` for the case, removed after | the same answers and page as `without a basemap`, and `/basemap/MANIFEST`, `/basemap/<v1 archive>` 404: "not installed" is the EXPECTED result and is recorded as such, not as a failure | `[RECORD]` line |
| DOM `with a basemap` (3 engines) | deployment.md MAP2b / MAP2c; web.md (descriptor, tiles, glyphs, attribution); `positionDescriptor` / `positionFontFaces` of `positions-map.js`; `basemap.rs` | a tree written by `dv-svc-basemap install --profile hotspot` (below) installed to `out/<run>/sut-state/basemap/`; HTTP from the tester, then the same page | tiles.json has exactly the keys `installed gen tiles minzoom maxzoom overview_maxzoom coverage builds fetch_enabled attribution files` (`installed` true, 16 hex `gen`, `tiles` the one generation-qualified URL, `minzoom` 0, `fetch_enabled` false, attribution text, zooms and detail boxes equal to the MANIFEST), strong ETag + `no-cache` + 304; FONT-FACES.json is `{NotoSansRegular: [...]}` of listed font files; a glyph range 0-9 gives 206 with `Content-Range` and `Accept-Ranges`; the z0 tile is 200 `application/vnd.mapbox-vector-tile`, `content-encoding: gzip`, `public, max-age=31536000, immutable`; an absent detail tile is 204 with no body; an unknown generation, a malformed path and an out-of-range x are 404 `no-store`; MANIFEST, INDEX and the tier archives are 404. Page: no note, map visible with a canvas, attribution link text + href, the table unchanged, requests under `/basemap/` = tiles.json + tiles of the descriptor's generation only (200/204/304) + FONT-FACES.json (+ glyph ranges), no `.pmtiles` / MANIFEST / INDEX, requested origins == {page origin} (foreign ones are aborted and fail the case), one circle per station and per object counted in the map picture | statuses, counts, picture `out/<run>/web/browser/positions-map-<engine>.png` |
| ETag `idle` / `after a real change` (state with a DExtra link, state with an M17 position, last heard with a D-STAR over) | web.md: weak ETag = hash of the body without `now_ms`; matching `If-None-Match` is 304 | settle to a quiet pair, 4 samples 4 s apart; then the protocol client acts | idle: the ETag is identical across the samples (whatever else moved in the body is only recorded), starts with `W/`, conditional GET and HEAD 304; after the change: ETag differs within 25 s, conditional GET with the OLD ETag is 200, with the CURRENT one 304, conditional HEAD with the OLD one 200 | moved leaves, latency |

`test_public_conditional_and_head` of `cases/test_web_api.py` and the diagnostic `test_public_validator_on_an_idle_node` are unchanged.

## Why these rules (for the reader of a failure)

* The courtesy credit (`Courtesy` / `RfState::credit`): a message from X gated to RF gives X one credit for 300 s; the NEXT position of X seen on
  APRS-IS spends it, once, only for sender not heard on RF in the last 60 s, only when `position_allowed` (positions on, base callsign not opted out,
  not in the hidden set, hidden set KNOWN). The frame is the same third-party wrapper as the message. A position interval of the port class
  (30 minutes for `afsk1200-shared`) applies per station, so every case uses its own correspondent.
* Flat IS-to-RF limit of the worker per RF port: 6 in 60 s and 10 in 300 s (gated messages AND courtesy frames; result `rate`). `position2_support.IGATE`
  books the frames a case expects and waits when the window is full; the positive case books 2, the hidden-account case 3.
* The launcher accepts only `types = ["transit"]` on an APRS-IS to RF path ("APRS-IS-to-RF permits transit messages only", seen with `config_check` on
  wip-ac750c7). The DV-Router login filter is only `g/<addressee>` of the paths FROM `edge:aprsis` plus `filter_extra`, so
  `dp7-courtesy-filter.toml` adds `p/N0PS/N0OPT` (aprsc prefix filter on the source); without it neither the message nor the position comes down.
* Markers are WebGL: positions-map.js puts the rows into a GeoJSON circle layer, and neither the page nor MapLibre exposes a marker element or a map
  handle. The table is the accessible list of the same rows (the DOM evidence); the circles are counted in a screenshot (`position2_support.blobs`).
* The retention window is a constant (`POSITION_AGE_MS`, 30 minutes) of the hub and the page (`KEEP_MS`); config.md has no key, so no 20 to 60 s
  case exists. The long case is opt-in.

## Snippets (`rig/config/dv-router/`)

| Snippet | Content | Note |
|---------|---------|------|
| `dp7-courtesy-tx.toml` | `[edges.ax25]` with `tx_enable = true` and ONE kiss-tcp port `rf-a` (`<TESTER_IP>:8101`) bound to `room = "B"`, `tx = true`, `physical = "radio1"`, `band = "2m"`, `class = "afsk1200-shared"`, `tx_budget = {}`, `tx_per_min = 10`, `tx_burst = 3` | INSTEAD of `dp7-rf.toml` and `dp15-rf.toml` (all define `[edges.ax25]`); the default 2 per minute / burst 1 would delay the courtesy frame by 30 s |
| `dp7-courtesy-filter.toml` | `[edges.aprsis] filter_extra = "p/N0PS/N0OPT"` | merged key by key into `dp15-aprsis.toml`'s table; name it after that file; needed in every courtesy shape |
| `dp7-courtesy-path.toml` | path `dp7-is-to-rf`: `edge:aprsis` to `edge:ax25/rf-a`, `types = ["transit"]`, `courtesy_posit = true` | only with `dp7-courtesy-tx.toml` and `dp15-aprsis.toml` |
| `dp7-courtesy-path-off.toml` | the same path with `courtesy_posit = false` | INSTEAD of `dp7-courtesy-path.toml` (same path name) |

Config check (host side, throw-away container, `--network none`, `dv-router:wip-ac750c7`, all `valid`):
`python3 -m dvtest.tools.config_check --image dv-router:wip-ac750c7 --template <template> --gen compat1 --extra R/data-ident.toml --extra R/dp15-aprsis.toml --extra R/dp7-courtesy-tx.toml --extra R/dp7-courtesy-filter.toml --extra R/dp7-courtesy-path.toml --extra R/dp7-position-optout.toml`
(also with `dp7-courtesy-path-off.toml` in place of the path, with `--extra R/dp7-rf.toml` and `dp7-courtesy-filter.toml` for the receive-only shape, and with
`dp7-rf-igate.toml` and `dp7-aprsis-up.toml` added to the first). `dp7-courtesy-path.toml` together with `dp7-courtesy-path-off.toml` is INVALID (duplicate path name), as expected.

## Run commands

Common prefix as above (`XLXD_SUT_IMAGE=<next image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template>
DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true`); `R` = `rig/config/dv-router`; `W` = `R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml:R/dp15-aprsis.toml`.

| Run | `DVT_SUT_CONFIG_EXTRA` | Command tail | Runs | Needs |
|-----|------------------------|--------------|------|-------|
| C1 courtesy on (about 25 min, UNVERIFIED) | `W:R/dp7-courtesy-tx.toml:R/dp7-courtesy-filter.toml:R/dp7-courtesy-path.toml:R/dp7-rf-igate.toml:R/dp7-position-optout.toml` | `scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp15_positions2.py` | DP7-e courtesy / no credit / optout / hide_position, DP7-f (with transit), DP7-g, DP7-h m17 + rf | profile aprs (`scripts/pre-fetch-aprsc.sh` once), `DVT_RIG_CONTROL` is set by `run.sh` |
| C2 courtesy path off (about 8 min) | as C1 with `R/dp7-courtesy-path-off.toml` instead of `R/dp7-courtesy-path.toml` | same | DP7-e path off (the others skip with the reason, DP7-f / g / h run again) | as C1 |
| C3 transmit off (about 8 min) | `W:R/dp7-rf.toml:R/dp7-courtesy-filter.toml:R/dp7-position-optout.toml` | same | DP7-e tx off, DP7-f (no transit), DP7-g, DP7-h | as C1 |
| C4 positions off, optional | as C1 with `R/dp7-positions-off.toml` instead of `R/dp7-position-optout.toml` | same | DP7-e optout (the `positions = false` correspondent) | as C1 |
| ETag (about 4 min) | `R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml` | `scripts/run.sh -m rig --capture --profile web -- cases/test_web_etag_diag.py` | the strict ETag case x3 and the diagnostic | the position trigger needs the data worker |
| DOM without a basemap (about 10 min) | `R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml` | `DVT_USRP_DYNAMIC=1 DVT_REQUIRE_IDENT=true DVT_TRANSCODER_FAKE=1 scripts/run.sh -m rig --capture --profile web,browser -- cases/test_dp7_positions_page.py` | `without a basemap` x3; `refuses a v1 basemap` and `with a basemap` skip with the reason | `docker pull` of `BROWSER_BASE_IMAGE` once; M17 module C |
| DOM v1 basemap refused (about 12 min) | as above | as above with `DVT_BASEMAP_FILE=.local/basemap-20261106.tar.gz` | `without a basemap` x3, `refuses a v1 basemap` x3 (expected: not installed) | the old MAP1 file |
| DOM with a basemap (about 15 min) | as above | as above with `DVT_BASEMAP_FILE=.local/basemap-map2` | `without a basemap` x3, `with a basemap` x3 | a tree written by `dv-svc-basemap` (below) |
| Retention (about 40 min, its own run) | as ETag | `DVT_DP7_RETENTION_LONG=1 scripts/run.sh -m rig --profile web -- cases/test_dp15_positions2.py -k retention` | DP7-i | the data worker |

**Unblocked by DV-Router MAP2g (image wip-2fcc727, fd32a71, 2026-11-09):** the tree comes from the offline fixture (below, "Producing the tree without internet"): `scripts/basemap-install-fixture.sh --bin .cache/dvr-wip-2fcc727/dv-svc-basemap --out .local/basemap-map2` (install --dry-run, install and verify, `--profile hotspot`, world z0-5 9 tiles, detail dvt z6-12 7 tiles, glyph catalog kind operator). Rig run 20261108T211741Z: `without a basemap` 3 passed; `with a basemap` passed on Chromium and Playwright WebKit; Playwright Firefox skips it because the rig's headless Firefox has no WebGL2 (the page shows its list fallback, which the case checks). The 220e45b binary has no `--glyph-base` and cannot make the tree.

## The basemap tree (what the lead supplies, MAP2)

The case does NOT run `dv-svc-basemap` itself. No control action of `scripts/run.sh` / `dvtest/control.py` runs a program in the SUT container (the
allow-list is `kill-proc`, `proc-stat`, `metrics`, `netem`, `mark`), and `install` cannot finish against a local fixture server anyway: `--source-index` and
`--source-base` replace only the build index and the tile archive, while the 431 pinned glyph and font files (45.9 MB, `crates/svc/basemap/assets/glyphs.json`)
come from `https://raw.githubusercontent.com/...` with no override (`--dry-run` is the only mode that works offline; it writes nothing). So the operator runs
the product's own tool once on a machine with network access (the image ships the binary) and names the result. A small hotspot install around the seeds
keeps it near 61 MB plus the detail (estimate from deployment.md: 15 MB overview z0-5 + 45.9 MB glyph set):

```sh
mkdir -p .local/basemap-map2
docker run --rm -v "$PWD/.local/basemap-map2:/var/lib/dv" --entrypoint dv-svc-basemap <dv-router image> \
    install --state /var/lib/dv --profile hotspot --detail dvt=99.9,13.0,101.1,14.1
docker run --rm -v "$PWD/.local/basemap-map2:/var/lib/dv" --entrypoint dv-svc-basemap <dv-router image> verify --state /var/lib/dv
```

`DVT_BASEMAP_FILE=.local/basemap-map2` (a path under this repository, the tester container mounts it at `/dvt`; `.local/` is ignored by git) may name the
`--state` directory or its `basemap/` directory (the one that holds `base/MANIFEST` and `regions/INDEX`). The case reads `base/MANIFEST` (v2), checks the
structure (version 2, schema major 4, one `world` overview, detail tiers on the shared zoom range, one build, `glyphs/OFL.txt` and a `.pbf` range) and the size and
sha256 of every tier archive and glyph file, stages the copy as `out/<run>/sut-state/basemap.dvt-staging/` and renames it to `basemap/` in one step
(never over an existing `basemap/`), puts a marker file `.dvt-installed` inside (not in the MANIFEST, never served) and at the end removes only a `basemap/`
that carries the marker. dv-svc-web reads the published base on the next request (deployment.md: "The web service may keep running"), so the case waits up to
30 s for tiles.json to say `installed: true` and does not restart anything. The seeds (three stations and one object in room C) are placed inside the first
detail box of the MANIFEST (the default Thailand box `97.3,5.6,105.7,20.5` when the base is not at hand).

Any other `DVT_BASEMAP_FILE` (a directory with a top-level `MANIFEST`, a `.tar` / `.tar.gz` of one, a bare `.pmtiles`) is a MAP1 basemap: only the
`refuses a v1 basemap` case uses it. The old tarball `.local/basemap-20261106.tar.gz` is such a file; "not installed" is the expected answer for it.

### Producing the tree without internet (MAP2g fixture)

`dvtest/basemap_fixture.py` is a loopback server plus encoders (Python standard library, deterministic bytes) for the four MAP2g overrides of
`dv-svc-basemap install`: `--source-index` (`/builds.json`: one build `20261107.pmtiles`, schema `4.15.2`), `--source-base` (`/archives/`: HEAD with size and a
strong ETag, ranged GET answered 206 with an exact `Content-Range`, `If-Match` mismatch 412, unsatisfiable range 416), `--glyph-base` (`/mirror/<catalog path>`,
plain 200) and `--glyph-catalog` (260 entries: the 256 BMP ranges, the two `OFL.txt`, `FONT-FACES.json`, `Fixture.ttf`). The archive is a PMTiles v3 file of 18
gzip MVT tiles (one per zoom 0 to 14 containing 100.5 E, 13.75 N, plus the other three tiles of z1), enough for both profiles; every tile is one `earth` square.
It is a structural fixture, not a map: the glyph ranges are empty, the font is a 32-byte sfnt envelope, no visual result is implied.

```sh
# the binary must be a dv-svc-basemap of DV-Router MAP2g or later (an older one has no --glyph-base); it runs inside scripts/contained.sh --name test
scripts/basemap-install-fixture.sh --bin <path to dv-svc-basemap> --out .local/basemap-map2
#   ... prints, last line on stdout:  DVT_BASEMAP_FILE=.local/basemap-map2
DVT_BASEMAP_FILE=.local/basemap-map2 scripts/run.sh ...      # as the DOM with a basemap row above
```

Defaults are `--profile hotspot --detail dvt=99.9,13.0,101.1,14.1` (the world overview z0-5 holds 9 tiles, the detail z6-12 holds 7); `--profile router` and any
`--detail NAME=W,S,E,N` inside Thailand also work. The script runs `install --dry-run` first (its plan is saved as `<out>/install-plan.json`, beside `basemap/`), then `install` and `verify`, and refuses an `--out` that already holds a `basemap/`. The binary is any executable file of a MAP2g build (copy it out of the image with `docker create` and `docker cp`; the image keeps it at `/usr/local/bin/dv-svc-basemap`; its entrypoint is `dv-router`, so either `docker cp` that path or run with `--entrypoint dv-svc-basemap`).
`python3 -m dvtest.basemap_fixture serve --catalog-out FILE` keeps the server up for a manual run, `info` prints the
sizes and digests (archive 2298 bytes, catalog 78981 bytes). Unit tests: `python3 -m pytest -m unit cases/unit/test_basemap_fixture.py` (31 tests; the last one installs and verifies for real
when `DVT_BASEMAP_BIN=<binary>` is set and skips otherwise).

## UNVERIFIED shapes of the second set

* Courtesy: that the rig's aprsc passes the correspondent's packets to the DV-Router login with `filter_extra = "p/N0PS/N0OPT"` (aprsc `p/` prefix filter on the
  source), that a verified login may send only with source == login (the cases never send another source), that the worker's heard table sees the TNC
  frame of `N0PRX-2` (a status) 2 s after the injection, that the courtesy frame follows within 25 s (scheduler: min gap 2 s, position class), and the counter
  names `dv_pkt_rf_drop_total{reason}` / `dv_pkt_rf_downlink_total{result}` (read from `core_rf_tx.rs`, recorded not asserted). The outer path of a transmitted frame is empty
  (`tx_path = ""`); only the inner path is asserted (`TCPIP,N0TEST-1*`).
* The hide push (3 s after `PATCH ... hide_position` the worker and the web process have the new set), and that the worker knows a hidden set at all in the run (cold
  start denies positions until the first set; the cases come late enough).
* SSE: that data event frames carry `id: <boot>-<ordinal>`, that `?last_event_id=` replays the events after it for a client that was not connected, that
  a hidden station is omitted from the replay, and that clearing the flag brings the retained row back (the hub does not know the flag).
* Items: that `)NAME!` over M17 packet mode and RF becomes the same `object` row as `;NAME`, and that the timestamp-free kill is accepted after the live item (the hub
  orders by acceptance time, `hub/data.rs`).
* DOM: the status texts and test ids are taken from `positions.js`; that `.hidden` hides the map container; that the default room selected by the page is the first
  room of the state (the case selects `C` with the room `<select>`); that module C exists and accepts an M17 client; that the tester sees `out/<run>/sut-state/`;
  that dv-svc-web notices a `basemap/` that appears while it runs, on the next request (MAP2c: the identity of `base/MANIFEST` is checked per request; the case fails with the reason after 30 s, it does not restart the service); the exact `gen` value is not recomputed (only its form); that the real hotspot base holds the z0 tile in the overview and no tile at `-179.9,80.0` of the detail maximum zoom;
  that Chromium delivers the requests of the MapLibre worker to the context-level `request` event (Firefox and WebKit: unknown; an unseen request cannot fail the origin
  audit, the aborting route is the guard); the marker colours (`#146cad`, `#b36b00`, tolerance 12 per channel) and that circles of the seeds do not touch; WebGL2 in
  the browser container (without it the case checks the list and skips the pixel part).
* ETag: that a DExtra link changes the state's client list, that a D-STAR over adds a last-heard entry, and that the validator is a weak ETag (`W/` prefix, as the first case asserts).


## D-PRS cases (`cases/test_dp7_dprs.py`, written, not run on a rig)

Requested by the DV-Router product owner for positions that arrive as a D-PRS GPS-A line in D-STAR slow data. Files: `cases/test_dp7_dprs.py` (the cases),
`cases/dprs_support.py` (decoders of the APRS compressed and Mic-E positions written from the APRS 1.0.1 text, the printable Mic-E encoder, D-PRS
line builders and parsers, the LSF META view of the M17 voice stream and the GNSS search in it), `cases/unit/test_dprs_support.py` (42 offline tests; the spec examples,
the distinctness of the callsigns). Reused, not duplicated: `cases/test_dp15_positions.py` (`ambiguity_case`, `send_dstar`, `feeds`, `collect`, `aprsc_login`,
the fixtures `hub`, `web_ctx`, `shape`, `aprsc`), `cases/position_support.py`, `cases/data_support.py`, `dvtest/aprs_cases.py` (the compressed encoder and the
Mic-E destination / information encoders, extended only by the printable speed and course form), `dvtest/dstar_sd.py`, `dvtest/proto/m17.py`, `dvtest/proto/m17v3.py`
(GNSS META layout and decoder). Notes of a run: `out/<run>/dp7-dprs.txt` (RECORD / FINDING).

| Case | Statement | Steps | Asserted | Recorded |
|------|-----------|-------|----------|----------|
| DP7-dprs-a `[level 1,3,4]` | a D-PRS position keeps its ambiguity level (level 2 is DP7-c `d-prs level 2`) | D-PRS line `!4903.50N/07201.75W>` with `level` blanked digits from `N0PQ1/3/4` (`tp.send_dstar`) | the level-2 checks (`tp.ambiguity_case`): source 1, body `ambiguity` = level on the public SSE, the anonymous public state and the operator feed, centre within 2 e7 units, the page text of `format_coordinate` equals the text of the sent precision (`49.05833`/`49.059`/`49.0`/`49` style); the DP7-FIND1 text is prefixed when no position appears | the texts |
| DP7-dprs-b `[compressed]` | a compressed position in a D-PRS line is a position | `N0PQC>API510,DSTAR*:!/5L!!<*e7>7P[` (APRS101 ch. 9) | source 1, ambiguity 0, lat/lon of the exact base-91 decode (49.5 N, -72.75000394) within `POSITION_E7` (200) on the three surfaces | symbol (sent `/>`), comment, the stored e7 |
| DP7-dprs-b `[mic-e]` | a Mic-E position in a D-PRS line is a position | `N0PQE>S32UVT,DSTAR*:` + `` `(_fn"Oj/ `` (APRS101 ch. 10) | source 1, ambiguity 0, 33 25.64 N 112 07.74 W (334273333, -1121290000 e7) within 200 on the three surfaces | symbol (sent `/j`), comment, the stored e7 |
| DP7-dprs-b `[mic-e offset 0]` | the same Mic-E frame with the destination as the spec prints it is also valid | `N0PQF>S32U6T,DSTAR*:` + the same nine bytes (fifth destination character `6` = longitude offset +0) | admitted, not rejected: source 1, ambiguity 0, 33 25.64 N and 12 07.74 W (334273333, -121290000 e7, from `mice_decode`: `(` = 12 degrees, no +100) within 200 on the three surfaces | symbol, the stored e7 |
| DP7-dprs-c `[level 1..4]` | an ambiguous D-PRS position gives NO GNSS META in the LSF META of the M17 voice stream of its room; an unambiguous one does | the fix rides along on the STREAM plane (not as a packet): during the D-STAR over the room's M17 listeners get it in the LSF META of the cross-mode voice stream (legacy GNSS META in production, v3 only where a P26j gate enables it). One M17 client (`N0PQL`, CONN, module B) listens throughout and the LSF copies in the frames of the streams it receives are read. CONTROL: D-PRS over of `N0PQB<l>` with 10.5 N 20.25 E (3.4 s, then 6 s of tail). Then the over of `N0PQA<l>` with the ambiguous box, same listener, same wait | control: the stream LSF has a GNSS-typed META (legacy `0x0025` or v3 `0x2010`: the TYPE announces the META type) with the lat/lon valid bit whose coordinates equal 10.5 N 20.25 E within 200 e7 (2e-5 degrees); otherwise the case FAILS with a FINDING that names the reason (no GNSS TYPE; typed but undecodable; wrong coordinates; position under another TYPE; position only in an M17P packet; nothing at all). Ambiguous: at least half as many stream packets as the control (the listener listened), no GNSS-typed LSF copy with nonzero META (text META may change), no other TYPE whose META decodes as a GNSS position within 1 degree of the box, no M17P packet with one; then the ambiguous position is in the anonymous public state with ambiguity = level (else the absence is a drop, DP7-FIND1, not the rule) | per stream: packets, distinct LSF META and TYPES; GNSS copies, matches, first GNSS frame number and time |
| DP7-dprs-d `[ride-along level 1..4]` | the ride-along re-applies the level that was sent | second D-STAR client `N0PQR` of module B decodes (`dstar_sd.Decoder`) the slow data of the over of `N0PQD<l>` | every D-PRS line of that callsign at the listener has a valid CRC and `!` or `=` followed by `4903.50N/07201.75W>` with `level` blanked digits exactly as sent; no such line is a FINDING and a failure | number of overs, lines, whether the line is byte-identical to the one sent |
| DP7-dprs-d `[uplink level 1..4]` | the APRS-IS uplink re-applies the level | path `room:B -> edge:aprsis` (`dp7-aprsis-up.toml`); the private aprsc tester login reads the line of `N0PQU<l>` | the same text check on the uplinked information field, as the uplink half of the level-2 case | the line at the aprsc |

Skips: no positions (`positions = false`) skips every case; the uplink case skips without the room-to-APRS-IS path and, through the `aprsc` fixture, without
`DVT_APRS=on`, `DVT_APRSC` or an enabled `[edges.aprsis]`. Every callsign above is the base callsign of exactly one position (the controller admits 1 per 30 s
per base callsign, dedupes on (type, guard callsign, body) for 60 s; the private aprsc dedupes for 30 s): a unit test checks that they are distinct from the
callsigns of `cases/test_dp15_positions.py`. Level 2 is run in DP7-dprs-c and DP7-dprs-d as well (the room-side level-2 case already exists).

Run (prefix and `R` as above; the M17 listener needs a transcoder, `DVT_TRANSCODER_FAKE=1` is enough; estimated 20 to 25 minutes, UNVERIFIED):

```
DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml:R/dp15-aprsis.toml:R/dp7-aprsis-up.toml \
scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp7_dprs.py
```

Without the uplink (`--profile web`, `DVT_SUT_CONFIG_EXTRA=R/web.toml:R/admin.toml:R/totp-required.toml:R/data-ident.toml`) the four uplink cases skip with the reason.
Validate collection without a rig: `python3 -m pytest cases/test_dp7_dprs.py --collect-only -q`; the helpers: `python3 -m pytest -m unit cases/unit/test_dprs_support.py -q`.

### Vectors and their source

The vectors come from the APRS Protocol Reference 1.0.1 (29 August 2000, `APRS101.PDF`, read as text); no number comes from DV-Router code. The decoders in
`cases/dprs_support.py` are written from the formulas of the same chapters and cross-checked in `cases/unit/test_dprs_support.py` against the worked examples:

| Vector | Where in APRS101 | What the text states | What the test derives |
|--------|------------------|----------------------|-----------------------|
| `/5L!!<*e7>7P[` (verbatim) | ch. 9, "Compressed Lat/Long Position Report Format" example list and the "13-character compressed location field" | 49 30 N, 72 45 W (-72.75), course 88, speed 36.2 knots, T byte `[` = 58 + 33 | YYYY = 15427503 -> exactly 49.5; XXXX = 20427156 -> -72.75000394 (the text rounds it); `1.08 ** 47 - 1` = 36.23 |
| `/5L!!<*e7>{?!`, `/5L!!<*e7OS]S` (verbatim) | ch. 9, same example list | radio range about 20 miles; altitude `S]` = 10004 feet | unit tests only |
| `` `(_fn"Oj/ `` (verbatim, 9 bytes) | ch. 10, "Example of Decoding the Information Field Data" | with offset +100 and West: longitude 112 07.74 W, speed 20 knots, course 251, jeep symbol `j/` | exact decode 112.129 W |
| `S32U6T` (verbatim, destination of the worked table) | ch. 10, "Destination Address Field Encoding" example | latitude 33 25.64 N, standard bits 1/0/0 (M3 Returning), North, offset +0, West | digits 3 3 2 5 6 4; with the information bytes above the longitude is 12 07.74 W (`(` = 12, no +100); sent by `[mic-e offset 0]`, expected -121290000 e7 |
| `S32UVT` (DERIVED) | the same table | the fifth byte of the table example is `6` (offset +0), but the information example needs +100 degrees (112 = 100 + 12); the consistent destination has `V` (P-Z, flag 1) there | 33 25.64 N 112 07.74 W; sent by `[mic-e]`. Both destinations are valid frames; the spec's printed `S32U6T` is inconsistent with its own longitude example |
| `T4SQZZ`, `` `(_f `` (verbatim) | ch. 10, "Mic-E Position Ambiguity" | two ambiguous digits, longitude 112 degrees 7 minutes | unit test only |

## UNVERIFIED (D-PRS cases)

* The exact TYPE and META of the stream LSF that carries the GNSS ride-along. Per DV-Router the fix is in the LSF META of the cross-mode M17 voice STREAM (not an M17P packet). The case reads
  the LSF copy in every 54 byte stream packet (an IP stream packet carries the whole LSF; the LICH chunks of the air interface are not on this plane) and accepts a META as GNSS only when the TYPE
  announces it by the M17 specification (legacy voice, no encryption, subtype 1 = `0x0025`; v3 META type 1 = `0x2010`) and `dvtest.proto.m17v3.decode_gnss` (layout from the M17 specification, also
  used for the legacy form: UNVERIFIED that the legacy production layout is identical) reads a position with the lat/lon valid bit. A position under another TYPE, an undecodable META or a position only
  in an M17P datagram fails the CONTROL with a FINDING (never accepted silently). The raw GNSS step is 1e-5 degrees (latitude) and 2e-5 (longitude, cut toward zero): the control position 10.5 N 20.25 E
  stays inside the 2e-5 degree position tolerance; another position might not.
* That the M17 listener hears the D-STAR over at all (needs the transcoder; the case fails with that reason when fewer than 20 stream packets arrive).
* That the 3.4 s over plus 6 s tail is long enough for the GNSS META of the control to arrive (the first GNSS frame number and its arrival time after the header are recorded, not asserted).
* The ride-along shape: that a second D-STAR client receives the talker's slow data with the D-PRS line (a D-STAR to D-STAR over keeps its slow data, DP15-1d), and, if the product re-emits
  a rendered line (for an M17 / web origin or after a policy change), that it still starts with `!` or `=` and has the 19 byte latitude and longitude texts in the same places. A listener
  without any D-PRS line of the talker is a failure with a FINDING; the line destination and path are recorded, not asserted.
* The ident `stream` of a D-STAR talker for the uplink path (`min_ident = "stream"`), as in the level-2 case; the uplink report type `!` or `=` for a plain position.
* A compressed or Mic-E position in a D-PRS line (data.md 696 says the GPS-A info field goes through the APRS position parser; the DP5a "Built scope" paragraph describes an own uncompressed `!`/`=` decoder).
  The Mic-E line needs the destination field of the D-PRS line (`CALL>S32UVT,DSTAR*:`); that DV-Router passes it to the parser, and that a D-PRS source accepts a destination other than `API510`, is unverified.
  A failure is reported as `FINDING DP7-DPRS-COMPRESSED` / `DP7-DPRS-MICE`.
* The symbol of the stored position (`/>` for the compressed example, `/j` for Mic-E): recorded, a difference is a FINDING note, not an assertion.
* That DV-Router reads the fifth destination character as the spec's table says (P-Z: +100 degrees, 0-9: +0) so that `S32UVT` is 112 07.74 W and `S32U6T` is 12 07.74 W; `S32UVT` is derived from the table rules,
  not printed verbatim, and the printable speed / course encoding (`n"O`) is the one the spec's example uses and `dvtest.aprs_cases.mice_info` does not.
* The `ambiguity` field of the operator feed body (as for the level cases above), and the DP7-FIND1 status of the D-PRS ambiguity path (the first rig run dropped an ambiguous D-PRS position:
  DP7-dprs-a, -c and -d then fail with that finding; -b and the control of -c are independent of it).
