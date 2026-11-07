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
* Not covered: the IS-to-RF courtesy posit (needs a gated message to an RF-heard addressee), NXDN positions, hide_position for RF transit, the browser DOM of `#/positions`, SSE replay (`Last-Event-ID`) after a hide, object ambiguity levels, items (`)`) as opposed to objects, the 30 minute retention.
