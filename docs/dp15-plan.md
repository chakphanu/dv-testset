<!-- SPDX-License-Identifier: MIT -->
# DP15 plan: the data-channel cases of DV-Router data.md 15.5 (planning only, no case code yet)

Status: plan, written 2026-11-07 against the worktree HEAD `ae78bd4` of this repository. Nothing here was run: no rig,
no compose, no netns, no docker build, no DV-Router binary. Every statement is taken from a file that was READ; the
source is cited as `file:line`, and a statement that is a guess or was not checked against a source carries the word
UNVERIFIED.

Sources and how they are cited:

| Short name | File (read-only) |
|------------|------------------|
| `data.md:N` | DV-Router `docs/design/data.md` (tree `DV-Router-wip`, commit `3969899c`), line N of the working copy; 15.5 is lines 2442-2450 |
| `web.md:N` | DV-Router `docs/design/web.md` |
| `status.md:N` | DV-Router `docs/design/status.md` |
| `cfg:aprsis.rs:N`, `cfg:ax25.rs:N` | DV-Router `crates/lib/config/src/aprsis.rs`, `ax25.rs` |
| `repo:path:N` | this repository (worktree `ae78bd4`) |

Words: "room" = a DV-Router module letter (the rig serves A-Z); the room's APRS name is the reflector callsign plus the
module letter (`XLX999B` for module B, `data.md:1405`). Callsigns below are synthetic (N0CALL, N0TEST, XLX999).

## 0. Summary

### 0.1 Verdict table (one row per case)

"now" = buildable with the repository as it is plus new case code and snippets only. "after X" = needs the named new rig
piece or control-channel action first. All cases need `DVT_SUT_PROFILE=dvrouter`, `DVT_SUT_LAUNCH=native` and a
DV-Router template that serves the module (the rig template serves A-Z, `repo:docs/rig.md:327`) with the data worker ON:
the native template has `[data]` off (DV-Router `docs/project/work-units.md` T13 note), so every run names
`rig/config/dv-router/data-ident.toml` in `DVT_SUT_CONFIG_EXTRA` (`repo:rig/config/dv-router/data-ident.toml:11-15`).

| Case | One line | Verdict |
|------|----------|---------|
| DP15-1a | D-STAR radio TX message becomes the talker's status on the web within one superframe + pipeline | now (web profile) |
| DP15-1b | D-PRS GPS-A beacon becomes a position, visible to logged-in roles only | now (web profile); the "users only" half is partly a finding, see 1b |
| DP15-2a | M17 SMS during a D-STAR QSO: M17 clients at once, D-STAR clients only after a pause of at least 2 s (M17 sender) | now |
| DP15-2b | the same with a web sender (the render path, not the native relay) | now (web profile) |
| DP15-2c | META text of an M17 talker becomes D-STAR text blocks of that over's cross-mode egress; a D-STAR-sourced over untouched | now (fake transcoder), needs a new META builder helper |
| DP15-3a | kill the data worker during traffic: raw counters, voice untouched, web chat works | after U1 (control-channel allow-list entry `dv-data-worker KILL`) |
| DP15-3b | restart: renders resume within 2 s of the worker token | after U1 |
| DP15-4a | YSF over with a GPS fix gives one position | now, needs a new YSF GPS DCH builder |
| DP15-4b | DMR over with a `DMRG` packet gives one position | now, needs a new `DMRG`/`DMRA` builder and a DMR id that resolves to the talker callsign |
| DP15-4c | `DMRA` gives a status and never changes the displayed callsign | now (same helper as 4b) |
| DP15-5a | APRS message to `XLX999B` from a client of a PRIVATE aprsc reaches the room (D-STAR text over, M17 SMS, web) and is acked once | after U4 + U5 (aprsc image and rig service) |
| DP15-5b | a repeated message is acked again and not delivered twice | after U4 + U5 |
| DP15-6a | message to the room name on RF reaches the room (D-STAR text over, M17 SMS, web) | A: now with a fake KISS TNC in the tester (U2); B: after U6 + U7 (Dire Wolf on a loop) |
| DP15-6b | RF transmit is off by default: room text produces no frame while `tx_enable` is false | A: now (U2); B: after U6 + U7 |
| DP15-6c | with `tx_enable` + port `tx` + budget the room text goes out as a group message `:XLX999B :text`, no id | A: now (U2); B: after U6 + U7 |
| DP15-6d | never above the duty cycle / `tx_per_min` | A: now (U2); B: after U6 + U7 (15.5 bullet, not in the request) |
| DP15-6e | a frame heard on two ports is one item | A: now (U2, two fake TNCs); B: after U6 + U7 (tee in the loop) |
| DP15-7a | wire shape and timing of the synthetic text over as an XLX peer receives it | now (tester plays the peer, `xlx-peer-tester.toml`) |
| DP15-7b | remote floor effect of that over | after U8 (a second DV-Router node as interlink peer); the xlxd peer cannot show DV-Router floor/recording |
| DP15-7c | recording: sending node does not record it (local exclusion); receiving node records it | sending side: now (recorder profile); receiving side: after U8 |
| DP15-7d | cross-mode effect (DMR/M17 listeners on the receiving peer) | after U8 with the fake transcoder on the peer; the rig's xlxd peer has no transcoder (`repo:rig/compose.yml:1070`) |

Extra 15.5 bullets that the request did not list but that fall out of the same helpers (each marked "extra"):
DP15-1c partial text never gives a partial status (`data.md:2444`), DP15-1d D3 native D-STAR slow data byte-identical
with and without the worker (`data.md:2444`, combined with 3a), DP15-2d M17 packet-mode APRS payload (specifier 0x02)
gives a position (`data.md:2445`).

### 0.2 Implementation order in one line

U0 snippets and fixture helpers -> U1 kill allow-list -> U2 fake KISS TNC -> U3 stream-metadata cases (groups 1, 2, 4)
-> U1 cases (group 3) -> U2 cases (group 6 tier A) -> U4/U5 aprsc (group 5) -> U6/U7 Dire Wolf loop (group 6 tier B)
-> U8 second DV-Router node (group 7b-d). Details, sizes and dependencies in section 5.

## 1. What the repository already covers (data-ident) and what the shared observables are

### 1.1 `cases/test_data_ident.py` (read in full: `repo:cases/test_data_ident.py:1-549`)

| data-ident case | What it proves | DP15 relation |
|-----------------|----------------|---------------|
| a `test_direct_sms_becomes_a_dstar_text_over` (`:313`) | M17P SMS from a direct client reaches an IDLE-module DExtra listener as a 22-frame text over `N0CALL: <body>`, header MY `XLX999`, suffix `M17 `; counters `dv_room_data_total` / `dv_pkt_items_total` move | covers the idle-module half of group 2; DP15-2a adds the "during a QSO" half and the 2 s pause |
| b `test_direct_sms_src_variants` (`:349`) | SRC variants, RECORD only | none |
| c `test_peer_sms_needs_an_identity` (`:379`) | M17 peer SMS has ident 0, denied under `require_ident` | none |
| d `test_xlx_peer_slow_data_counters` (`:478`) | a D-STAR talker on the XLX peer: listener on the SUT gets voice AND slow data byte-identical, one stream, status item counted (unidentified or accepted), never a text item | partly covers D3 and the XLX side of group 7 for SLOW DATA (not for a synthetic text over) |
| e `test_interlink_voice_unchanged` (`:531`) | interlink voice byte-identical both directions | none |

Helpers to REUSE as they are: `Hub` (metrics through `rig_control.metrics("xlxd-sut")`, `settle()` quiet baseline of
12 s, `wait_for()` polling up to 25 s; counters refresh every 10 s, `repo:cases/test_data_ident.py:140-190`),
`dstar_listener`, `m17_client`, `sms_packet`, `wait_for_overs`, `texts_of`, `slowdata.overs` / `slowdata.over_text`
(`repo:dvtest/slowdata.py:80,109`). They should move into a shared module (`cases/data_support.py`, U0) instead of being
imported from another case file.

NOT covered by data-ident: anything on the web, any position, any DMR/YSF data, the worker lifecycle, APRS-IS, RF, the
text over as seen by an XLX peer.

### 1.2 Observables (what the tester can read)

| Observable | How | Source |
|------------|-----|--------|
| Status / text rows of a room | `GET /api/v1/user/rooms/{R}/messages?limit=&after=` with a user or operator session; rows `{type, id, item_type, room, callsign, protocol, ts_ms, body}`, `item_type` text or status only, newest first, never a position | `web.md:1170-1181`, `repo:docs/web-api-cases.md:53` |
| Same rows live | public SSE `GET /api/v1/public/events`: a `data` event carries text/status with the body, never a position; replay with `Last-Event-ID` | `web.md:1117`, `status.md:223` |
| Positions | ONLY the admin-scope feed: `admin/status/events` (`data` events for positions) and `admin/status/data`; over HTTP that is `GET /api/v1/operator/events?type=...` and `GET /api/v1/operator/stream` (operator session). The user message history, the public SSE and the observer feed never carry a position. The map is planned (DP7), so there is NO page or user API that shows a position today | `status.md:223-225`, `data.md:2285-2288,2310`, `web.md:1032,1178` |
| Whether the operator events pass the position body through unchanged | not stated in web.md | UNVERIFIED, first thing the 1b spike must check |
| Counters | `rig_control.metrics("xlxd-sut")` = the hub Prometheus text: `dv_room_data_total{room,result}`, `dv_pkt_items_total{kind,result}`, `dv_pkt_raw_total{kind,result}`, `dv_pkt_reject_total{kind,reason}`, `dv_edge_raw_total{kind,result=sent\|no_worker\|raw_rate\|...}`, `dv_edge_render_total{kind,dir,result}`, `dv_edge_text_overs_total{result}`, `dv_pkt_ack_total{result}`, `dv_edge_aprsis_verified`, `dv_edge_modem_total{port,result}`, `dv_edge_rf_*`; refresh every 10 s | `data.md:2195,2203-2223`, `repo:dvtest/metrics.py` (used by data-ident) |
| Worker up/down | public state `live.data_workers` (a count), the status events `data_worker_up` / `data_worker_down`, gauge `dv_pkt_ready`; `proc-stat xlxd-sut dv-data-worker` reads pid and start time (`dv-data-worker` is in `PROC_STAT_NAMES`) | `web.md:398,1122`, `status.md:332-333`, `repo:dvtest/control.py:69`, `repo:scripts/lib/procstat.sh:41` |
| Protocol packets | the tester's own clients (DExtra `DstarClient`, `M17Client`, `YsfClient`, `DmrClient`, `XlxPeer`) with `inbox` and `inbox_times` | `repo:dvtest/proto/*.py` |

### 1.3 Facts that shape every case (all from data.md unless noted)

1. Data never takes the voice floor (`data.md:99`). A D-STAR text over is queued per edge and room, starts only after the
   controller grants a text slot, needs the floor idle for `[data] idle_gap_ms` (default 2000 ms, `data.md:1109`), 1 s
   between two text overs of a room, 4 queued items, 3 starts per item (`data.md:1107-1115`), a text over is 22 frames /
   440 ms (`data.md:1167`).
2. M17 packet mode, the web page and APRS-IS deliver at once, also during voice (`data.md:100,1012,1116`). M17 renders
   nothing for an item of M17 origin: the native relay already delivered the packet byte-identical (`data.md:1012`). So
   "M17 clients at once" for an M17 sender is the native relay; the data-channel render is only proven by a WEB sender.
3. Raw units: each edge emits one `dstar-slowdata` unit per superframe of every granted inbound over (`data.md:695`);
   10 raw units per second per circuit, burst 20 (`data.md:686`); an edge drops raw units with `no_worker` when no worker
   token is live (`data.md:368`).
4. A D-STAR TX message needs all four five-byte segments within ONE superframe unit; it becomes a `status` of 20 characters,
   once per over, and "a changed status at most once per 30 seconds" (`data.md:724-737`). Consequence for the cases: use a
   different talker callsign (or at least 31 s) between two status expectations.
5. Item lifetimes: status 10 s, position 300 s, text 60 s (`data.md:122-124`); the hub keeps the last 64 text/status per
   room for 24 h and the newest position per callsign for 30 min (`data.md:2285`).
6. Identity: a stream item needs the talker callsign bound to a class 0 circuit; `require_ident` true (default) denies
   ident none (`data.md:1532`, `repo:rig/config/dv-router/data-ident.toml:3-9`). A parsed APRS source `N0CALL`, `NOCALL`,
   `SERVER` or an own identity is REJECTED (`data.md:1653`): use `N0TEST-n` as the APRS source in all cases.
7. Position values are rejected when 0,0, out of range, or a known fix time is more than 1 h away from the receive time
   (`data.md:690`); use a fixed synthetic non-zero coordinate (for example 10.5 N, 20.25 E).
8. Web chat: `POST /api/v1/user/rooms/{R}/messages` `{text}` needs a logged-in user with `may_tx`, the CSRF token; 1 per
   5 s per account, burst 2 (`web.md:1180-1187`); fixtures `web_user(may_tx=True)` (`repo:cases/web_fixtures.py:118`).

## 2. Common rig and config design

### 2.1 Run shape

One rig run per config family (the snippets are appended to the template, `repo:docs/rig.md:327-345`):

| Run | Profiles | `DVT_SUT_CONFIG_EXTRA` | Groups |
|-----|----------|------------------------|--------|
| R1 stream data | `web` (plus `interlink` if 1d/D3 is run against the peer) | `data-ident.toml` (+ `web.toml`, `admin.toml` added by `--profile web`) | 1, 2, 3, 4 |
| R2 RF fake TNC | `web` | R1 + `dp15-ax25-fake.toml` | 6 tier A |
| R3 APRS-IS | `web,aprsc` (new profile) | R1 + `dp15-aprsis.toml` | 5 |
| R4 RF Dire Wolf | `web,dwloop` (new profile) | R1 + `dp15-ax25-dw.toml` | 6 tier B |
| R5 XLX peer | `web,interlink` or `web,dvrpeer` | R1 + `xlx-peer-tester.toml` (7a) / peer node snippet (7b-d) + `recorder.toml` (7c) | 7 |

`DVT_TRANSCODER_FAKE=1` for every run that needs cross-mode audio (2a, 2c, 4, 7d): the fake extended AMBEd gives the
byte-exact transform (`repo:docs/ambed-fake.md`, SKILL table row "byte-exact cross-mode"). 2a needs M17 voice and D-STAR
voice on one module; with the fake that works as in `cases/test_crossmode_exact.py`.

### 2.2 Snippets to create (U0; all under `rig/config/dv-router/`, tokens `<NET>`, `<TESTER_IP>`, `<OWN_IP>` as in the existing snippets)

| File | Content (sketch) |
|------|------------------|
| `dp15-paths.toml` | `[[data.path]]` rows for modules under test, as in `data.md:1400-1423`: `aprsis-down` (`from = "edge:aprsis"`, `to = "room:B"`, `types = ["text"]`, `addressee = "XLX999B"`, `per_min = 30`), `rf-in` (`edge:ax25/rf-b -> room:B`, `types = ["text","position","status"]`, `addressee = "XLX999B"`), `rf-out` (`room:B -> edge:ax25/rf-a`, `types = ["text"]`, `addressee = "XLX999B"`, `min_ident = "session"`, `per_min = 2`). Split per run (aprsis rows only in R3, ax25 rows only in R2/R4) so a run does not carry an edge it does not start |
| `dp15-aprsis.toml` | `[edges.aprsis] enable = "on"`, `server = "<NET>.46"`, `port = 14580`, `callsign = "N0TEST-5"`, `passcode_file = "/etc/dv-router/aprs-secrets/passcode"`, `filter_extra = ""`; keys from `cfg:aprsis.rs:17-27`, `valid_station` rule base <= 6 chars with a digit and an upper-case letter (`cfg:aprsis.rs:46`). `enable` is the `Enable` type (`on`/`off`; exact TOML spelling UNVERIFIED, copy the form of the other snippets) |
| `dp15-ax25-fake.toml` / `dp15-ax25-dw.toml` | `[edges.ax25] enable`, `callsign = "N0TEST-1"`, `tx_enable = <DP15_TX_ENABLE>` (token from env, true/false, like `<REQUIRE_IDENT>`), `alias_calls = []`, ports `rf-a` (kind `kiss-tcp`, `host`, `port`, `channel = 0`, `tx = true`, `physical = "radio1"`, `band = "2m"`, `class = "afsk1200-shared"`, `tx_budget = {}`) and `rf-b` (RX only, `room = "B"`); field list from `data.md:1865-1890`, validation from `cfg:ax25.rs:219-300` (RF TX needs callsign + master switch + port switch + explicit budget; PASSALL refused). The new env token `DP15_TX_ENABLE` needs a small addition to `dvtest/sut_config.py` and `scripts/run.sh` (same mechanism as `<REQUIRE_IDENT>`, `repo:scripts/run.sh:150-153`) |
| `dp15-recorder` | not needed: `recorder.toml` exists |

Port-room binding and paths: a port with `room = "B"` publishes its stations' positions and statuses straight to room B,
while its TEXT still needs a path with the addressee (`data.md:1585-1592,1702-1704`).

## 3. The cases

Every table row: wording quoted from 15.5 with the line number, steps, observable, reuse, new pieces, time, verdict.
"Time" is the wall time of the case body, excluding the rig start (about 1 to 2 minutes, UNVERIFIED) and excluding the
12 s quiet baseline of `Hub.settle()` that every counter case pays once.

### 3.1 Group (1) D-STAR

15.5 text (`data.md:2444`): "D-STAR: a radio TX message appears as the talker's status on the web within one superframe
plus pipeline; a D-PRS GPS-A beacon appears as the talker's position (users only); loss of a required text block loses
the text and never produces a partial status; the native D-STAR to D-STAR slow data is byte-identical with and without the
worker (D3)."

| Id | DP15-1a |
|----|---------|
| 15.5 | `data.md:2444` "a radio TX message appears as the talker's status on the web within one superframe plus pipeline" |
| Steps | 1. user `web_user(may_tx=False)` logs in; open the public SSE (`WebClient.sse` with `on_frame` stamps, `repo:dvtest/web.py:329`) before the over. 2. A linked DExtra client (callsign `N0TEST`, module B) sends a header with MY = its link callsign, then >= 63 voice frames (3 superframes) whose slow data carries the 20-character TX message in every superframe (build with `dstar_sd.text_blocks` / `place` / `render` / `over_datagrams`, `repo:dvtest/dstar_sd.py:153,224,283,820`; or `dextra.slow_text`, `repo:dvtest/proto/dextra.py:233`). 3. Record the send time of the last frame of the first superframe (frame 20) and the arrival time of the `data` event with `item_type = status` and the text. 4. Also read the row through `GET .../messages` |
| Expectation | the status row `callsign = N0TEST`, `protocol` D-STAR family, body = the 20 characters (sanitised stored form, `data.md:217-225` UNVERIFIED line range for the sanitiser), shown once per over; latency from the end of the first superframe to the SSE event <= a bound. The bound is not stated in 15.5 ("one superframe plus pipeline"): propose 420 ms superframe + 1.0 s pipeline = 1.5 s measured from the first frame sent, and ask the DV-Router lead (open question Q1) |
| Observable | SSE `data` event timestamps; `GET messages` row; `dv_pkt_items_total{kind="status",result="accepted"}` +1, `dv_edge_raw_total{kind="dstar-slowdata",result="sent"}` >= 1 (format names: UNVERIFIED, take from the registry in the counters) |
| Reuse | `dstar_listener`/`DstarClient` (`test_data_ident.py:228`), `dstar_sd` generator (stage 1 of `docs/dstar-slowdata-cases.md`, "nothing sends it" today, line 62 there), `web_user`, `Hub` |
| New | `cases/data_support.py` (shared helpers), a web-event waiter that returns the arrival time of the first matching `data` frame (`sse(..., until=, on_frame=)` exists); snippet: `data-ident.toml` only |
| Time | about 15 s per over, 40 s with a second callsign for the negative twin |
| Verdict | **buildable now** (R1) |

| Id | DP15-1b |
|----|---------|
| 15.5 | `data.md:2444` "a D-PRS GPS-A beacon appears as the talker's position (users only)" |
| Steps | 1. Operator session + user session + anonymous client, SSE open on each. 2. A D-STAR talker `N0TEST` (own callsign in MY) sends an over whose slow data carries `dstar_sd.dprs_line("N0TEST>API510,DSTAR*:!1030.00N/02015.00E>test")` (CRC over the line including CR, `repo:dvtest/dstar_sd.py:123`; `$$CRC` + four hex digits, GPS-A <= 300 B, `data.md:695`) interleaved with the text blocks as ircDDBGateway does (`dstar_sd.interleave`, `repo:dvtest/dstar_sd.py:250`). 3. Wait for the position. 4. Repeat with a GPS-A whose APRS source is NOT the talker (spoof) |
| Expectation | one `position` item `source 1` (D-PRS), callsign N0TEST, within the same latency class as 1a; visible in `operator/events` / `operator/stream` as a `data` event of the admin feed (`status.md:224-225`); NOT in the public SSE, NOT in `public/state`, NOT in `GET user/rooms/B/messages` (`web.md:1178`); the spoof variant gives no position item and `dv_pkt_reject_total{kind=...,reason="spoof"}` (or ident lowered, `data.md:690`). "Users only": there is no user-visible position surface today (the map is DP7, planned, `data.md:2308-2312`), so the "users only" half can only be asserted as ABSENT from the anonymous and observer surfaces and PRESENT on the operator surface; whether a plain user session should see it is Q2 |
| Observable | operator events/stream, public SSE (absence over the whole over + 5 s), counters `dv_pkt_items_total{kind="position",result="accepted"}`, `dv_room_data_total{result="accepted"}` |
| Reuse | as 1a + `web_operator` |
| New | `operator/events` reader helper in `dvtest/web.py` (a `WebClient.get` call; row shape UNVERIFIED), a GPS-A over builder `gps_over(call, lat, lon)` in `dvtest/dstar_sd.py` (wraps existing functions) |
| Time | about 25 s |
| Verdict | **buildable now** (R1) after the spike confirms the operator event carries the position body; if it does not, the position can only be shown by the counters (weaker) and that is reported as a gap to the lead (Q3) |

Extras of the same bullet: **DP15-1c** (extra) text with one block missing in a superframe yields no status and no partial
text (`data.md:724-731`; builder: `dstar_sd` frame drop, the pair-hole vectors exist in `vectors/dstar_sd/corpus.json`),
observable: no status row, `dv_pkt_reject_total` unchanged or a counted reject; **now**. **DP15-1d** (extra) D3: the
same D-STAR over listened to on a second DExtra client is byte-identical with the worker alive and killed; needs 3a's kill,
so **after U1**.

### 3.2 Group (2) M17

15.5 text (`data.md:2445`): "M17: an SMS during a D-STAR QSO reaches M17 clients at once and D-STAR clients only in a pause
of at least 2 s; META text of an M17 talker appears as D-STAR text blocks in the cross-mode egress of that over and the
D-STAR-sourced over is untouched; an APRS payload in M17 packet mode (specifier 0x02) produces a position."

| Id | DP15-2a |
|----|---------|
| 15.5 | "an SMS during a D-STAR QSO reaches M17 clients at once and D-STAR clients only in a pause of at least 2 s" |
| Steps | 1. Module B: M17 sender S (callsign N0CALL, link callsign = SRC so ident is 1, `repo:cases/test_data_ident.py:12-17`), M17 listener L (second `M17Client`), D-STAR talker T and D-STAR listener D (DExtra, `DstarClient`). 2. T keys a long over (`dextra.talk`, about 5 s of tone frames from `signals`, `repo:dvtest/audio/signals.py`). 3. 1.5 s into the over S sends `sms_packet(body, "N0CALL")`. 4. Record the arrival times at L (`inbox_times`) and at D, and the voice-end time of T. 5. After T's last frame, wait up to 10 s for a text over on D |
| Expectation | L receives the SMS packet during the over (native relay, byte-identical to what S sent); D receives NO text frame while T's voice is open (no text header between T's header and T's last frame); the text over `N0CALL: <body>` starts on D no earlier than 2.0 s after T's last frame (`idle_gap_ms`, V4 `data.md:1122`) and within a bounded delay after that (propose 2.0 s + 1.5 s, Q1); D's voice from T is intact (byte-identical frames). Honest limit: with an M17 sender the "at once" half is the native relay (`data.md:1012`), so it proves nothing about the data channel; 2b does |
| Observable | `inbox_times` of L and D, `slowdata.overs(D.inbox)` header/frame timing, counters `dv_edge_text_overs_total{result="sent"}` +1, `dv_edge_render_total{kind="m17-pkt"}` |
| Reuse | data-ident helpers `m17_client`, `sms_packet`, `dstar_listener`, `wait_for_overs`, `texts_of`; the long talk from `cases/test_crossmode_exact.py` (UNVERIFIED which helper) |
| New | a timeline helper (T0 reference, merged event list) in `cases/data_support.py`; fake transcoder for M17 listeners to receive the D-STAR voice |
| Time | about 25 s |
| Verdict | **buildable now** |

| Id | DP15-2b |
|----|---------|
| 15.5 | same bullet, "reaches M17 clients at once" through the render path; the DV-Router built test is `dstar_voice_keeps_identical_m17_bytes_while_web_sends_at_once` (`data.md:2366`) |
| Steps | as 2a but the sender is a web user (`POST /api/v1/user/rooms/B/messages`, `may_tx=True`) at +1.5 s |
| Expectation | L gets an `M17P` SMS (SRC = reflector side, specifier 0x05, valid CRC; `m17.parse_m17p`, `repo:dvtest/proto/m17.py:282`) within a small bound (propose 500 ms, Q1) while T's voice is open; the M17 voice bytes that L sees from T are identical to a control run without the web message (`data.md:2366`); D gets the text over only after the 2 s pause; POST answered 202 |
| Observable | as 2a + the POST status |
| Reuse | as 2a + `web_user` |
| New | control-run compare: run the same over twice, with and without the message (about 2x time) |
| Time | about 50 s |
| Verdict | **buildable now** (R1, web profile) |

| Id | DP15-2c |
|----|---------|
| 15.5 | "META text of an M17 talker appears as D-STAR text blocks in the cross-mode egress of that over and the D-STAR-sourced over is untouched" |
| Steps | 1. M17 talker sends a voice stream whose LSF META carries a legacy text block (control byte high nibble = number of blocks, low nibble = one-hot index, 13 B UTF-8; `data.md:694,1068`); the TYPE has the META-text bits and the stream is plain plaintext. 2. A DExtra listener D collects the cross-mode egress. 3. Decode D's slow data with `dstar_sd.Decoder` / `DvrUnitDecoder` (`repo:dvtest/dstar_sd.py:427,633`) and `slowdata.over_text`. 4. Control: a D-STAR-sourced over with native slow data heard on a second D-STAR listener is byte-identical (as data-ident d) |
| Expectation | text blocks 0x40..0x43 at the filler positions (slots 0,2,4,6, frames 1-2, 5-6, 9-10, 13-14, sync at frame 0, `data.md:1026,1041-1043`), repeated each superframe, text = the META text (<= 52 B legacy, the egress carries 20 characters); a status item for the M17 talker appears (META text -> `status`, `data.md:694`); `dstar-text20` render counted `framed`; D-STAR-sourced over frames byte-identical (`data.md:1022,1038`) |
| Observable | D's frames; `dv_edge_render_total{kind="dstar-text20",result="framed"}`; `dv_pkt_items_total{kind="status"}` |
| Reuse | `M17Client`, `m17.stream_packet(meta=...)` (`repo:dvtest/proto/m17.py:188`), `dvtest/proto/m17v3.py` (v3 text/GNSS builders only; legacy text builder is NEW), crossmode helpers `cases/test_crossmode_exact.py:196` (`test_m17_meta_untouched`) |
| New | `m17.meta_text(text)` legacy block builder + unit round-trip in `cases/unit/` (SKILL rule), `dvtest/slowdata` decode of the egress positions |
| Time | about 30 s |
| Verdict | **buildable now** (fake transcoder). Caveat: the demand token for `dstar-text20` exists only while a D-STAR listener is on the module (`data.md:363,1031-1034`) |

**DP15-2d** (extra, `data.md:2445` last clause): an `M17P` packet with specifier 0x02 and a TNC2 payload
(`N0TEST-7>APDVT1:!1030.00N/02015.00E>test`) yields a `position` (source 5) when the APRS source equals the LSF SRC
(else ident lowered, `data.md:694`). Builder: `m17.m17p_packet(0x02, data, src=...)`. **now**, observation as 1b.

### 3.3 Group (3) worker lifecycle

15.5 text (`data.md:2446`): "Worker: kill the worker during traffic: raw counters, voice untouched, web chat works;
restart: renders resume within 2 s of the token."

Mechanics: `data.md:442-443` (no worker -> edges drop raw units with `no_worker`, web POST still works, voice untouched;
worker crash -> the launcher restarts it, items published meanwhile are not rendered later), restart backoff 0.5-5 s,
exit 2 final (`data.md:323`), worker-only restart procedure `restart_child` about 1 s gap (`data.md:452`).

Control channel today: `kill-proc xlxd-sut <process> KILL` is allow-listed ONLY for `dv-edge-xlx/nxdn/p25/allstar`,
`dv-codec-ambed`, `dv-svc-web`, `dv-edge-webrtc`, `dv-svc-status`, `dv-room` (with `rooms=`)
(`repo:scripts/lib/killproc.sh:52`, `repo:dvtest/control.py:39-49`, header of `repo:scripts/run.sh:166-183`);
`dv-data-worker` is in `proc-stat` (read only) but NOT in `kill-proc`. The generic child kill used by `cases/test_web_k22.py`
(`repo:cases/test_web_k22.py:707,735`) is `rig_control.kill_proc("xlxd-sut", "dv-edge-webrtc"|"dv-svc-status", "KILL")`, one
allow-list entry per process, not a generic kill. So the worker kill needs **one new allow-list entry in three places**:
`dvt_killproc_allowed` case list in `scripts/lib/killproc.sh`, `KILL_PROC_ALLOWED` in `dvtest/control.py`, the header text of
`scripts/run.sh` and the table test `cases/unit/test_control.py` (U1). The comm `dv-data-worker` is 14 characters, within the
15-character comm limit (UNVERIFIED that the kernel comm equals the binary name; `proc-stat` already lists it, so it does).
The dv-room and dv-svc-status kills are the model; the launcher respawns the child (`data.md:323`).

| Id | DP15-3a |
|----|---------|
| 15.5 | "kill the worker during traffic: raw counters, voice untouched, web chat works" |
| Steps | 1. Baseline counters. 2. D-STAR talker T keys a long over (about 20 s) with slow data; D-STAR listener D and an M17 listener L (fake transcoder) collect. 3. At +5 s `rig_control.kill_proc("xlxd-sut", "dv-data-worker", "KILL")` (returns pid, `t_kill`). 4. While the worker is down (a second kill if it already returned) T's superframes continue; at +8 s a web user POSTs a chat message. 5. Wait for T's end; read counters |
| Expectation | `dv_edge_raw_total{result="no_worker"}` rises for the units sent while down (`data.md:368,442`); `dv_pkt_ready` returns to 1 after the restart; D's voice frames from T are byte-identical to a control over without a kill (`data.md:443` "Voice: Untouched"); the web POST answers 202 and the row appears in `GET messages` (`data.md:442`: the controller admits, the hub shows it); the rendered text over for that web message is NOT delivered while down and not later (`data.md:443` "no history"); `live.data_workers` goes 1 -> 0 -> 1 |
| Observable | counters, `live.data_workers` via SSE `data_worker_down/up` events (`status.md:332`), `proc-stat dv-data-worker` start time changes, D's frames |
| Reuse | `Hub`, `dstar_listener`, `web_user`, `rig_control.proc_stat` (`repo:dvtest/control.py:243`), the kill pattern of `test_web_k22.py` |
| New | **U1** allow-list entry; a case file |
| Time | about 40 s (+ the 10 s counter lag) |
| Verdict | **after U1** |

| Id | DP15-3b |
|----|---------|
| 15.5 | "restart: renders resume within 2 s of the token" |
| Steps | 1. Kill the worker, 2. watch the public SSE for `data_worker_up` (status event, `status.md:332`) and `live.data_workers` in `public/state` for the token's reappearance, stamp T_token (SSE arrival, accuracy bounded by SSE latency), 3. immediately and every 250 ms POST web chat messages with distinct texts (rate 1 per 5 s per account -> use 2 accounts or the burst 2, `web.md:1182`) and watch the M17 listener L for the SMS render and the D-STAR listener for the text over, 4. T_first_render = the first render whose text was POSTED AFTER T_token |
| Expectation | T_first_render - T_token <= 2.0 s for the M17 path (the independent path delivers at once); for the D-STAR path the text over additionally waits for the 2 s idle gap and the 1 s spacing, so state the D-STAR bound as max(2 s + idle gap) or ask Q4; messages posted while down are never rendered later |
| Observable | M17 `inbox_times`, D-STAR text over header time, SSE worker events |
| Reuse | as 3a |
| New | U1; an SSE waiter with event-name filter (`WebClient.sse(until=...)`) |
| Time | about 30 s, repeated 3 times (the launcher backoff is random 0.5-5 s, UNVERIFIED how it grows) -> 90 s |
| Verdict | **after U1** |

### 3.4 Group (4) YSF and DMR

15.5 text (`data.md:2447`): "YSF, DMR: a GPS fix in a YSF over and a `DMRG` in a DMR over give one position each; a
`DMRA` gives a status and never changes the displayed callsign."

Edge facts (`data.md:697,699`): YSF `ysf-dch` for frames with FI = communications, DT = 2 and FN 6 or 7: 20 bytes, bytes
1..2 `22 62` (short) or `47 64` (long), 0x03 end marker and a sum byte, byte 4 radio type, bytes 5..13 Mic-E-like
(`YG/GPS.cpp:30-31,99-134,150-244`), one fix per over then one per 30 s when changed, symbol by radio type. DMR
Homebrew `dmrhb-dmra` (15 B, `DMRA`) and `dmrhb-dmrg` (14 B, `DMRG`) with the `stream_id` of the open stream whose source id
equals bytes 4..6 on that slot (none -> `unbound`, rejected); `DMRA` blocks 0..3 at byte 7, 7 payload bytes each into 32 B,
header byte 0 format bits and length; `DMRG` LC bytes 2..8 copied into packet bytes 7..13, longitude 25-bit, latitude
24-bit (`MH/DMRNetwork.cpp:259-297`, `MH/DMRSlot.cpp:2061-2104`, `MH/DMRTA.cpp:34-136`).

| Id | DP15-4a (YSF) |
|----|---------------|
| 15.5 | "a GPS fix in a YSF over ... give one position" |
| Steps | `YsfClient` (callsign N0TEST, module B) links, sends a header, voice frames with FN 6/7 DCH carrying the GPS record (position 10.5 N 20.25 E, a radio type of the FT-3D class), terminator. Second over later with a changed fix inside 30 s (expect none) and after 30 s (expect one) |
| Expectation | exactly one `position` item per over, `source 3` (YSF GPS), symbol `[` for FT-1D/FT-2D/FT-3D/FT-5D, comment = radio name (`data.md:699`); operator-feed event; none on anonymous surfaces; `dv_pkt_items_total{kind="position",result="accepted"}` +1 |
| Reuse | `YsfClient` (`repo:dvtest/proto/ysf.py:690`), `ysf.vd2_payload(frames, dch10)` (`:428`), `voice_frame(fn, frames, dch10=...)` (`:512`), `dch_encode` (`:355`) |
| New | `dvtest/ysf_gps.py` builder of the 20-byte GPS record (frame bytes derived from the YSF reference layout named in `data.md:699`; the YSF client source is not pinned in this repo, so the layout must be fetched with a new pin or taken from `data.md` only: UNVERIFIED), unit round-trip |
| Time | about 20 s (+ 35 s for the 30 s rule, optional) |
| Verdict | **buildable now** (after the builder exists) |

| Id | DP15-4b (DMR `DMRG`) |
|----|----------------------|
| 15.5 | "a `DMRG` in a DMR over give one position" |
| Steps | `DmrClient` (`repo:dvtest/proto/dmr.py:383`, source id DMR_ID 1023007 must resolve to N0TEST in the rig `dmrid.dat`: UNVERIFIED mapping, `repo:docs/rig.md:169,252`) links to module B, starts a voice stream; inside the open stream send one `DMRG` packet (14 B, same source id, same slot, valid LC position bytes) via `client.client.send(...)` |
| Expectation | one `position` item, `source 4`, symbol `/-` (`data.md:697`), identity from the registry callsign; a `DMRG` outside an open stream is rejected `unbound` (negative twin, `dv_pkt_reject_total{reason="unbound"}`); LC error estimate 7 means unknown accuracy, not invalid (`data.md:697`) |
| Reuse | `DmrClient.talk`, `link` |
| New | `dmr.dmrg_packet(...)` / `dmr.dmra_packet(...)` builders (layout per `MH/DMRNetwork.cpp:259-297` as quoted in `data.md:697`; the file itself is not available here: UNVERIFIED until the dstarsd MMDVMHost checkout is read) + unit tests; a way to send a raw datagram during `talk` (needs `DmrClient.talk` split into start/frames/end or a thread) |
| Time | about 20 s |
| Verdict | **buildable now** (after builders) |

| Id | DP15-4c (DMR `DMRA`) |
|----|----------------------|
| 15.5 | "a `DMRA` gives a status and never changes the displayed callsign" |
| Steps | as 4b with 1..3 `DMRA` packets (blocks 0..3, ASCII alias `N0TEST ALIAS`); before/after read `public/state` `rooms[].onair` callsign, `public/lastheard`, the room's active talker |
| Expectation | one `status` item (display only); the on-air callsign and last-heard callsign stay the registry callsign (the alias never becomes identity, `data.md:697` "display only, never identity"); an alias with the callsign of ANOTHER station (a spoof attempt, text `N0CALL`) still does not change the displayed callsign |
| Reuse | as 4b + `public/state` helpers of `test_web_api.py` |
| New | as 4b; a public-state reader for the on-air callsign (shape UNVERIFIED, take from `docs/api/openapi-web.yaml`) |
| Time | about 20 s |
| Verdict | **buildable now** (after builders) |

### 3.5 Group (5) APRS-IS against a private aprsc

15.5 text (`data.md:2448`): "APRS-IS: a message to `XLX999B` from a client on a private aprsc reaches the room (D-STAR text
over, M17 SMS, web) and is acked once; a repeated message is acked again and not delivered twice." The 15.4 slow-tier list
adds login verified and unverified, `qAC` on uplink, `g/` filter delivery, dedupe of the repeated ack, `NOGATE` never gated,
class 1 position never gated, rate (`data.md:2440`).

DV-Router side (read): `[edges.aprsis]` keys `enable`, `server`, `port`, `callsign`, `passcode_file`, `filter_extra`,
`tocall` (default `APZDVR`), `keepalive_secs` (default 30, 1-60); `server` and `port` required when enabled; callsign must
satisfy `valid_station` (`cfg:aprsis.rs:17-27,46,118-170`). The filter is the union of `g/<addressee>` of every enabled
`edge:aprsis` path plus `filter_extra`, <= 384 bytes (`cfg:aprsis.rs:56-100`). Login after the banner:
`user <callsign> pass <passcode> vers DV-Router <version>` + ` filter <f>`; passcode read from the passcode file (0600, never
logged); WITHOUT the file it logs in with `pass -1`, is receive only and refuses to transmit while unverified
(`data.md:1666`); 30 s keepalive comment, 60 s of server silence closes the socket, reconnect backoff 5 s doubling to 5 min
(`data.md:1665,1668`); a message whose exact addressee matches an enabled path becomes room text, the ack is
`<ROOMNAME>>APZDVR,TCPIP*::<SRC padded to 9>:ack<id>` sent as the room name, acked once the controller answered accepted or
duplicate, `rej` for access/invalid, nothing for rate/busy (`data.md:1504,1566-1571,1709-1711,1573-1578`); messages without
an id are never acked; a duplicate is acked again, the controller dedupes the body for 60 s (`data.md:1504,377`); line limit 510 B
(`data.md:1669`); TX bucket 30 lines per minute (`data.md:1670`).

| Id | DP15-5a |
|----|---------|
| 15.5 | "a message to XLX999B from a client on a private aprsc reaches the room (D-STAR text over, M17 SMS, web) and is acked once" |
| Steps | 1. aprsc service up (U4/U5), DV-Router logs in verified (`dv_edge_aprsis_verified` = 1, `data.md:2212`). 2. Tester APRS-IS client (new `dvtest/aprsis.py`): login `N0TEST-7` with `vers dvtest 1.0` and the passcode computed by the rig's helper (see 4.1); no filter is needed (a message to the client's own login is delivered unfiltered, `aprsc:filter.c:2467-2486`). 3. Listeners on module B: DExtra D, M17 L, web SSE. 4. Send `N0TEST-7>APDVT1,TCPIP*::XLX999B  :DVT <nonce>{12` (message with id). 5. Collect for 15 s |
| Expectation | D gets the text over `N0TEST: DVT <nonce>` (source display callsign per `data.md:1167`; header UR `CQCQCQ`, suffix `APRS`, `data.md:1168`); L gets an `M17P` SMS; the web row `text` appears (`GET messages`, SSE); exactly ONE ack line `XLX999B>APZDVR,TCPIP*,qAS,N0TEST-5::N0TEST-7 :ack12` arrives at the tester's client within 3 s (the server adds `qAS,<login>` because source != login; the part before `qAS` is what DV-Router wrote, `data.md:1711`); `dv_pkt_ack_total{result="ack"}` +1; `dv_pkt_items_total{kind="text",result="accepted"}` +1; the item is not an APRS-IS uplink (text never goes up, `data.md:1518`) |
| Observable | the tester's own aprsc socket (ack line), D, L, SSE, counters |
| Reuse | data-ident helpers; the passcode scheme of 4.1 |
| New | **U4** aprsc image, **U5** compose service `aprsc` (profile `aprsc`) + secrets file + `dp15-aprsis.toml`, `dvtest/aprsis.py` client (stdlib socket, login, send, line reader), `dvtest/aprs_passcode.py` |
| Time | about 40 s; the DV-Router edge needs the login first (first connect immediate; a failed first connect waits the 5 s backoff) |
| Verdict | **after U4 + U5** |

| Id | DP15-5b |
|----|---------|
| 15.5 | "a repeated message is acked again and not delivered twice" |
| Steps | send the line at t = 0 (id 12); send the identical line (same source, addressee, information field) again at t = 2 s and at t = 35 s. The server drops an identical packet inside its 30 s window (key = source + destination without SSID + information; the path is not part of it, `aprsc:dupecheck.c:542-593`), so the t = 2 s repeat never reaches DV-Router (assert that no second ack and no counter move: it proves the server dupe, not DV-Router); the t = 35 s repeat passes the server and meets the controller dedupe (60 s, `data.md:377`) |
| Expectation | the room shows ONE `text` row, one text over and one SMS in total (the t = 35 s repeat is a controller `duplicate`: `dv_room_data_total{result="duplicate"}` +1, `data.md:377`); TWO ack lines in total (a duplicate is acked again because the sender missed the ack, `data.md:1504`); `dv_pkt_ack_total{result="ack"}` +2 or `repeated` +1 (which label the second one gets: UNVERIFIED, `data.md:2218`). The 60 s controller window bounds the schedule: the repeat must come between 30 s and 60 s after the first; after 60 s the same text would be delivered again, which is a documented consequence, not a defect |
| Observable | as 5a, counters `dv_pkt_ack_total{result="ack"}` and `repeated` (`data.md:2218`), `dv_room_data_total{result="duplicate"}` |
| New | as 5a |
| Time | about 60 s |
| Verdict | **after U4 + U5** |

Extras from 15.4 (not requested now, same rig): unverified login -> `# logresp ... unverified`, aprsc drops its packets, edge refuses to
transmit (no passcode file variant), `qAC` on uplink of a room position, `NOGATE` never gated. List them as DP15-5c..5e when
the rig exists; they need class 0 positions on an uplink path (`data.md:1393-1398`).

### 3.6 Group (6) RF through AX.25 / KISS

15.5 text (`data.md:2449`): "RF: with Dire Wolf on a loop, a message to the room name on RF reaches the room; room text goes
out as a group message only when `tx_enable` and the budget allow, never above the duty cycle; a frame heard on two ports is
one item."

DV-Router side (read): `[edges.ax25]` with `callsign`, `tx_enable` (false), `alias_calls`, `tocall`, ports of kind
`kiss-tcp|agwpe|kiss-serial|raw-tcp|raw-serial`, `host`, `port`, `channel` 0..15, `room`, `tx`, `physical`, `band`, `class`,
`tx_budget`, `tx_per_min` (default 2, 1..30), `kiss` parameters written at connect (txdelay 300, persist 63, slottime
100, txtail 30, full_duplex false); the edge is a KISS TCP CLIENT (host, port = the TNC) (`data.md:1865-1890`,
`cfg:ax25.rs:48-165,219-300`); RX only by default, TX needs master switch + port `tx` + station callsign + explicit
budget (`data.md:1947,1899-1902`); renders on `edge/<id>/tx/aprs-info`; room text goes out as `:ROOMNAME :text` without id, at
`tx_per_min` (default 2), burst 1 (`data.md:1098`); duplicate RF frames on two ports within 30 s are one packet with
`heard_ports` counted (`data.md:1951,1590-1594`); ack on RF only when `alias_calls` lists the room name (`data.md:1711`);
a path `edge:ax25/<port> -> room:B` with `addressee = "XLX999B"` is required for text (`data.md:1410-1414`). KISS parameter
frames per channel after every connect (`repo:dvtest/ax25.py:327-331`). The PTT watch: TX is fenced until the PTT token
snapshot completes; no PTT tokens exist in the rig, UNVERIFIED that an empty snapshot completes (`data.md:1954-1957`).

Two tiers, both specified here:

* **Tier A, fake TNC (no new image)**: the tester listens on a TCP port as a KISS TNC (class `FakeKissTnc` in `dvtest/ax25.py` or `dvtest/kiss_tnc.py`, built on `kiss_encode` / `KissDecoder` / `DvrouterKiss`, `repo:dvtest/ax25.py:236-300,356`). DV-Router's `kiss-tcp` port connects to `<TESTER_IP>:<port>` (the same trick as `xlx-peer-tester.toml`). The TNC (1) sends UI frames as RX, (2) records the KISS frames DV-Router writes (parameter frames, data frames), decoded with `ax25.parse_frame` / `to_tnc2`. Fully deterministic, no audio, no modem, runs in seconds. The fake `raw-tcp` source for LoRa-style TNC2 lines already exists (`dvtest/lora_gw.py:756` `serve_once`).
* **Tier B, real Dire Wolf on a loop** (section 4.2): proves the real KISS bytes and a real AFSK round trip.

| Id | DP15-6a |
|----|---------|
| 15.5 | "a message to the room name on RF reaches the room" |
| Steps | tier A: FakeKissTnc sends one UI frame `N0TEST-7>APDVT1,WIDE1-1::XLX999B  :DVT <nonce>{3` (`ax25.from_tnc2` + `kiss_encode(0, 0, frame)`, no FCS, `repo:dvtest/ax25.py:121`); listeners D, L, SSE as in 5a. Tier B: the tester sends the same frame to Dire Wolf A's KISS port, A modulates, B demodulates, B's KISS port feeds DV-Router (section 4.2) |
| Expectation | text over on D, SMS on L, web row, counters `dv_edge_modem_total{port="rf-b",result="rx"}` +1, `dv_pkt_raw_total{kind="ax25-ui",result="parsed"}`, `dv_pkt_items_total{kind="text",result="accepted"}`; NO ack frame on RF because `alias_calls` is empty (`data.md:1711`), and with `alias_calls = ["XLX999B"]` an ack as a plain UI frame (negative/positive pair; the alias must fit an AX.25 address, base <= 6 characters: `XLX999B` has 7, so it can NOT be an RF ack source, `data.md:1711`, mention in the case doc) |
| Reuse | data-ident helpers, `ax25.py` |
| New | **U2** `FakeKissTnc` + `dp15-ax25-fake.toml` + `dp15-paths.toml`; tier B: U6/U7 |
| Time | about 25 s |
| Verdict | tier A **now**; tier B **after U6 + U7** |

| Id | DP15-6b |
|----|---------|
| 15.5 | "room text goes out as a group message only when `tx_enable`" (negative half) |
| Steps | with `tx_enable = false` (default, `data.md:1947`) and port `tx = true`: web user POSTs a message to B; watch the TNC for 12 s |
| Expectation | no data frame on the TNC; KISS parameter frames only (`dvrouter_startup_params`); `dv_edge_modem_total{result="tx"}` unchanged; the room still delivers to D and L (RF is independent of the room, `data.md:1116`); the control run with `tx_enable = true` proves the TNC would have seen it (6c) |
| New | the `<DP15_TX_ENABLE>` token (U0); two runs, or one run with a live edit of the rendered config for the `ax25` table (it restarts the edge, `data.md:1906-1907`; `test_echo.py` edits and restores the run's own `dv-router.toml`, SKILL table row "echo", pattern to copy) |
| Time | about 30 s |
| Verdict | tier A **now**; tier B **after U6 + U7** |

| Id | DP15-6c |
|----|---------|
| 15.5 | "room text goes out as a group message only when `tx_enable` and the budget allow" |
| Steps | `tx_enable = true`, port `tx = true`, `physical = "radio1"`, explicit `tx_budget = {}` (class default), path `rf-out`; web user POSTs one message |
| Expectation | exactly one UI frame on the TNC: source = `[edges.ax25] callsign` (`N0TEST-1`), destination = `tocall` `APZDVR`, via = `tx_path` (empty), information field `:XLX999B :<text>` with NO message id (`data.md:1098,1952`); `dv_edge_modem_total{result="tx"}` +1, `dv_edge_rf_tx_total{result="sent"}`; 67-character text bound (`data.md:1610`: messages at most 67 B), so a 100-character web text is cut or refused (record which) |
| Time | about 30 s |
| Verdict | tier A **now**; tier B **after U6 + U7** (tier B also decodes the AFSK audio with `atest`) |

| Id | DP15-6d (15.5 clause "never above the duty cycle") |
|----|------|
| Steps | web users send N messages in quick succession (limited by 1 per 5 s per account and burst 2: use several `web_user` accounts); the TNC records frame timestamps |
| Expectation | at most `tx_per_min` frames per rolling minute (default 2, burst 1, `data.md:1098`), spacing >= scheduler min gap, airtime sum <= duty budget (`dvtest/airtime.py` `afsk_exact_frame_ms`, `afsk_airtime_ns`, `repo:dvtest/airtime.py:189-304`); excess dropped and counted `dropped_budget` / `dropped_quota` (`data.md:2211`); the airtime numbers are an UPPER BOUND in DV-Router (`repo:docs/direwolf-cases.md:343`) |
| Time | about 2 to 4 minutes (rolling minute windows) |
| Verdict | tier A **now** (the budget is checked on frame times, not on audio); tier B adds nothing |

| Id | DP15-6e (15.5 clause "a frame heard on two ports is one item") |
|----|------|
| Steps | two fake TNCs (or two Dire Wolf instances behind a tee) on ports `rf-a` and `rf-b`, both bound to B; both send the same UI frame within 30 s |
| Expectation | ONE text item/over/SMS; `dv_pkt_rf_heard_ports_total` +1 and `dv_pkt_rf_duplicates_total` +1 (`data.md:2221`); a different info field heard on the second port after 31 s is a second item |
| Verdict | tier A **now**; tier B after U6 + U7 with the tee |

### 3.7 Group (7) XLX peer: synthetic text over

15.5 text (`data.md:2450`): "XLX peer: the phase 1 request about remote floor, recording and cross-mode effects of a
synthetic text over stays open." Context (`data.md:1171`): "A receiving XLX peer sees a synthetic text over as ordinary class 1
voice from the reflector callsign: it takes that node's floor for about 440 ms, can be recorded there and can be transcoded
from null AMBE to its DMR or M17 listeners. The edge-local fabric and recorder exclusion applies only on the sending node."
XLX text overs depend on `[data] xlx_text_overs` (default true) and the origin class: never a text over on XLX for class 1 or 2
(`data.md:1518,1117`), so the trigger is a class 0 text (web chat or a direct M17 SMS).

| Id | DP15-7a |
|----|---------|
| 15.5 | the XLX peer request, wire half |
| Steps | `--profile web`, `xlx-peer-tester.toml` (XLX996 = the tester on modules DE, `repo:rig/config/dv-router/xlx-peer-tester.toml`); `XlxPeer` (`repo:dvtest/proto/xlx.py:179`) links to the SUT; a web user POSTs to module D; the peer collects with `packets(ip, ...)`, `stream_of` (`:156`) |
| Expectation | one stream from the SUT: header with the reflector callsign (`XLX999`) as MY, 22 frames / 440 ms with null AMBE (`dextra.NULL`/`NULL_AMBE` frame) and the text blocks (`slowdata.over_text` on the extracted slow data), sync at frame 0; a second web message within 1 s waits (1 s spacing, `data.md:1112`); with `[data] xlx_text_overs = false` (live edit) no stream (`data.md:1170`); a voice stream from the peer preempts a running text over (`data.md:1127`); the sending node's own XLX listener does not get its own text over back (no fabric re-entry, `data.md:1129,686`) |
| Reuse | `XlxPeer`, `slowdata`, data-ident helper style |
| New | text over extraction from DSVT frames (small adapter in `cases/data_support.py`) |
| Time | about 40 s |
| Verdict | **now** (R5) |

| Id | DP15-7b, 7c, 7d |
|----|------------------|
| 15.5 | remote floor, recording, cross-mode effects |
| What is needed | a RECEIVING node that has DV-Router semantics: a floor that the text over takes for 440 ms (observable as a `room` event / on-air state at the receiver), a recorder, and a transcoder to DMR/M17 listeners. The rig's interlink peer is a reference xlxd (`xlxd-peer-sut`, `DVT_TRANSCODER: 127.0.0.1`, "no transcoder: the interlink carries every codec as is", `repo:rig/compose.yml:1070`), which can show only: the XLX999 stream arrives and a DExtra listener on the peer reflector hears 22 frames of null AMBE (a weak "remote floor" proxy via last-heard at the xlxd peer). Recording and cross-mode on the receiver need a second DV-Router instance |
| 7b remote floor | with the xlxd peer: peer last heard / DExtra listener sees the over (weak). With a second DV-Router peer: its room event feed shows an on-air stream from `XLX999` of about 440 ms, class 1, taking the module floor: a real talker arriving inside it is `busy`/queued (`data.md:1171`) |
| 7c recording | sending node: `recorder.toml` + `web,browser`-free recordings API (`GET /api/v1/user/recordings`, `web.md:1005`): a web text over must NOT create a recording on the SUT (local exclusion, `data.md:1129,1171`); receiving DV-Router node with its recorder on: the over IS recorded as a 440 ms stream from `XLX999` |
| 7d cross-mode | receiving DV-Router node with the fake transcoder and an M17/DMR listener: it receives a transcoded (silent) stream of about 440 ms; check no data item is derived from it on the receiver (the re-injection guard: a header naming the reflector callsign or the `WEB ` / `M17 ` / `APRS` suffix is not turned into a status, `data.md:686`) |
| New | **U8**: a second DV-Router node as a rig service `dvr-peer` (same `XLXD_SUT_IMAGE`, native launch, its own rendered config with callsign XLX998 and the SUT as XLX peer, own transcoder-peer fake), reading the SKILL native-launch pattern; only the compose service and a second config render are new (no image build) |
| Time | 7c sending side about 60 s; receiving side about 2 minutes |
| Verdict | 7b: **after U8** (weak proxy now); 7c sending side **now** (recorder profile); 7c receiving side and 7d **after U8** |

## 4. Design notes for the two new rig services and the kill action

### 4.1 Group (5): a private aprsc

All aprsc facts are `aprsc:file:line` of the upstream source at tag `release/3.0.0` (commit `8ac04b4049c9fb3bf74db1c904c15f8f552820dd`,
BSD 3-clause, `LICENSE`; the licence is NOT GPL), read by a separate read-only pass over a shallow checkout; nothing was built or
run (UNVERIFIED = not checked even in the source).

| Topic | Fact | Source |
|-------|------|--------|
| Build | autotools, `configure` is committed; `./configure && make` in `src/`; Debian bookworm build deps `build-essential libevent-dev libssl-dev libprotobuf-c-dev protobuf-c-compiler` (libprotobuf-c is mandatory even with IS2 disabled); `libcap-dev` optional; no internet needed to build. Whether the bookworm header path `google/protobuf-c/protobuf-c.h` works is UNVERIFIED | `aprsc:src/debian/control`, `aprsc:configure.ac:56,67,68,84,289-300`, `aprsc:Makefile.in:205` |
| Runtime | links libevent, libssl/libcrypto, libprotobuf-c, libm, libz, libpthread/librt; static linking is not supported (only `--enable-static-libevent`), so use a multi-stage image that copies the binary and installs only the runtime libraries (`libevent-2.1-7 libssl3 libprotobuf-c1 zlib1g`; package names UNVERIFIED) | `aprsc:Makefile.in:65-69`, `aprsc:configure.ac` |
| Install layout | `/opt/aprsc/sbin/aprsc`, config `/opt/aprsc/etc/aprsc.conf`, dirs `logs data web`; none is needed at run time except `RunDir` | `aprsc:Makefile.in:8-14,231-239`, `aprsc:config.c:1535,1545` |
| Run | `aprsc -c /etc/aprsc/aprsc.conf -o stderr -e info -p /tmp/aprsc.pid` in the foreground (do NOT use `-f`, it forks); `-c` is mandatory (default is a relative path); refuses to run as root without `-u`, so the image runs as a non-root `USER` with no `-u`; pid file write is fatal if its directory is missing, hence `-p /tmp/...`; `-y` checks the config and exits (use it as a unit-test-free smoke check); `-e debug` logs why a packet was dropped | `aprsc:aprsc.c:11,89,544-551,858,1038,1054`, `aprsc:config.c:38,1775`, `aprsc:hlog.c:62,74,525-535`, `aprsc:incoming.c:1252` |
| Config | mandatory `ServerId` (3-9 alphanumeric or `-`), `PassCode`, `MyAdmin`, `MyEmail`, `RunDir`; `Listen "name" igate tcp 0.0.0.0 14580 noIS2upgrade` is the client-defined-filter port; no `Uplink` line = standalone; no `HTTPStatus` line = no HTTP listener; an unknown directive is fatal (so `UpstreamRetryDelay` must not appear); the sample config's invalid `MagicBadness` line must not be copied; no configurable rate limits found | `aprsc:config.c:177-220,1165-1370,1564-1610`, `aprsc:cfgfile.c:293`, `aprsc:http.c:980`, `aprsc:tests/cfg-aprsc/basic` |
| Login | banner `# aprsc 3.0.0-<rev> ...`; client line `user CALL pass N vers NAME VER filter F`; `vers NAME` without a version is rejected, send `vers dvtest 1.0`; answer `# logresp CALL verified|unverified, server <ServerId>`; wrong or missing passcode stays connected as unverified; unverified clients' packets are dropped by default (`disallow_unverified` = 1); 30 s login timeout; login callsign alphanumeric or `-`, <= 9 characters, not the ServerId; a second verified login with the same name kicks the first, so the tester and the DV-Router edge need DISTINCT login callsigns | `aprsc:login.c:30,335,396,422,469-471,482,518`, `aprsc:config.c:129,131`, `aprsc:incoming.c:1164-1166`, `aprsc:parse_qc.c:31-48` |
| Source callsigns | a packet source starting with `N0CALL`, `NOCALL` or `SERVER` is DROPPED (prefix match, label `disallow_srccall`), so `N0CALL-1` can log in but nothing it sends is forwarded. Use `N0TEST-n` and `XLX999..` everywhere on this server | `aprsc:incoming.c:97-101,566-577,925` |
| Passcode | the standard public hash over the upper-cased base callsign (text before the first `-`, at most 9 characters, seed 0x73e2), compared as an integer with the `pass` value; the Python reproduction is 8 lines (research pass computed N0CALL = 13023, the well-known value, N0TEST-7 = 15043, XLX999B = 2222) | `aprsc:passcode.c:11-34`, `aprsc:login.c:469-471` |
| Routing / q | a verified client whose source equals its login gets the path rewritten to `,TCPIP*,qAC,<ServerId>`; a verified client sending a DIFFERENT source gets `,qAS,<login>` appended and the packet is forwarded (no drop, no counter); case-sensitive source/login compare; the only catch: if the source callsign is itself logged in as a verified client on another socket, the packet is marked a dupe and not forwarded | `aprsc:parse_qc.c:336,349,597-598`, `aprsc:incoming.c:957-959,1049-1058`, `aprsc:clientlist.c:111-112` |
| Delivery | a message addressed to the receiver's own login is delivered to that connection with NO filter; otherwise the filter applies (`g/XLX999B` matches by message addressee, `b/` by source, `t/m` any message); the sender never gets its own packet back | `aprsc:filter.c:1451,1668,1892,2467-2486`, `aprsc:outgoing.c:99,113` |
| Duplicates | server-side 30 s window; the key is source callsign + destination without SSID + information field; the PATH is NOT part of the key; a duplicate goes only to dupefeed clients and is dropped for all others | `aprsc:dupecheck.c:542-593`, `aprsc:config.c:117`, `aprsc:outgoing.c:55-70` |
| Limits / keepalive | line <= 512 B including CRLF; a comment line `# aprsc 3.0.0-<rev> <date> GMT <ServerId> <addr:port>` every 20 s to each client (readers must skip `#` lines) | `aprsc:worker.h:52`, `aprsc:incoming.c:1219`, `aprsc:worker.c:54,1771-1797,1847` |

Consequences for the design:

1. **Pin** (U4): `.env.example` gets `APRSC_REPO=https://github.com/hessu/aprsc`, `APRSC_COMMIT=8ac04b4049c9fb3bf74db1c904c15f8f552820dd`
   (tag `release/3.0.0`), `APRSC_BASE_IMAGE=debian:bookworm-slim`; `scripts/pre-fetch-aprsc.sh` (same shape as
   `scripts/pre-fetch-direwolf.sh`, `fetch_verified` from `scripts/lib/env.sh`) fills `.cache/aprsc`; `rig/Dockerfile.aprsc` takes it as the named
   build context `src`, exactly like `rig/Dockerfile.direwolf` (`repo:rig/Dockerfile.direwolf:19-30`). Stage 1 installs the build
   dependencies and runs `./configure --disable-is2 && make -j${MAKE_JOBS}` (the flag name is read in the report as `configure.ac:56,67`; whether
   `--disable-is2` is its spelling is UNVERIFIED), stage 2 is `debian:bookworm-slim` + the four runtime libraries + `/usr/local/sbin/aprsc` +
   the fixture config, `USER aprsc` (uid 10004, a free number next to 10003 of Dire Wolf), `ENTRYPOINT ["aprsc","-c","/etc/aprsc/aprsc.conf","-o","stderr","-e","info","-p","/tmp/aprsc.pid"]`. Expected size: bookworm-slim (about 75 MB) +
   about 10 MB = **about 85 to 110 MB, far under 600 MB** (UNVERIFIED, estimate only). Build with `scripts/contained.sh --name build`, never while the rig runs.
2. **No internet at run time**: the service sits on the internal rig network (`internal: true`, `repo:docs/rig.md:8`); the config has no `Uplink`, no
   `HTTPStatus`, no `HTTPUpload`; `ServerId DVTEST`, `PassCode 0`, `MyAdmin`/`MyEmail` synthetic (`example.invalid`), `RunDir /tmp` on a tmpfs.
3. **Fixture config** `rig/aprsc/aprsc.conf` (mounted read-only): the six mandatory keys, `ClientTimeout 48h`, `LogInTimeout 30s`, one `Listen "Client-Defined Filters" igate tcp 0.0.0.0 14580 noIS2upgrade`, nothing else; `aprsc -y` in the build stage validates it (a failing check fails the image build).
4. **Passcode scheme**: the server verifies each login against the public hash of the login's base callsign. The rig computes
   `aprs_passcode(call)` (8-line Python in `dvtest/aprs_passcode.py`, unit-tested against the known value 13023 for the literal base `N0CALL`, which is
   public) at run time: the DV-Router edge login `N0TEST-5` gets its passcode written by `run.sh` (or by a fixture) to
   `out/<run>/aprs-secrets/passcode` (mode 600, mounted read-only at `/etc/dv-router/aprs-secrets/passcode`, like the ASL secrets;
   `passcode_file` is a file path, `cfg:aprsis.rs:23`); the tester client `N0TEST-7` computes its own. The value is a function of a synthetic callsign,
   not a real credential, but the SKILL rule stays: never print it, scan the run directory for it. Variants: no file -> `pass -1`, unverified, receive only
   (`data.md:1666`), and a wrong passcode -> `# logresp ... unverified` (5c, extra).
5. **Callsign plan** (all synthetic): aprsc `ServerId DVTEST`; DV-Router login `N0TEST-5` (`edges.aprsis.callsign`; base N0TEST has 6
   characters, a digit and upper-case letters: passes `valid_station`, `cfg:aprsis.rs:46-53`); tester client login and message source `N0TEST-7`
   (source == login, so the server rewrites the path to `TCPIP*,qAC,DVTEST` and the tester can assert `qAC`); the room name `XLX999B`
   (path addressee, 7 characters; ack source with source != login, which the server forwards with `qAS`).
6. **What the tester sees**: the ack `XLX999B>APZDVR,TCPIP*,qAS,N0TEST-5::N0TEST-7 :ack12` reaches the client logged in as `N0TEST-7` without any filter
   (message to its own login). The tester must not log in as `XLX999B`: that would mark the ack a dupe (`aprsc:incoming.c:1049-1058`).
7. **Services and address**: service `aprsc` at `<NET>.46`, profile `aprsc`, `x-limits-small`, `read_only`, `cap_drop: [ALL]`, tmpfs `/tmp`, port 14580
   reachable by the SUT and the tester only (internal network), `init: true`. DV-Router `[edges.aprsis] server = "<NET>.46"` (an IP literal or the host name `aprsc` resolved by the rig DNS, both accepted by `valid_host`, `cfg:aprsis.rs:118-130`).


### 4.2 Group (6): Dire Wolf "on a loop", headless

What exists: image `dvt-direwolf:local` = Dire Wolf 1.8.1 (commit `a231971a652bfb574a4bae9a5d875fbce53d2267`,
`repo:.env.example:289-291`), bookworm-slim + python3 + libasound2 + `direwolf gen_packets atest kissutil decode_aprs` + a
Python entry script, 180 MB, non-root uid 10003, run with `--network none` as a throw-away job (`repo:rig/Dockerfile.direwolf:20-56`,
`repo:docs/direwolf-cases.md:62-80`). No compose service uses it yet. What is verified there (V in that document): Dire
Wolf serves its KISS TCP port 8001 to 3 clients at a time; `ADEVICE stdin null` works (RX audio from a pipe that must stay
open, TX audio goes nowhere); KISSCOPY copies frames between clients; a frame sent by a KISS client is logged and, on port
nibble 0, transmitted; the log must be read through a pty (`repo:docs/direwolf-cases.md:62-80,83-100`).

What Dire Wolf's source allows for audio (read from the pinned checkout `.cache/direwolf`, `repo:` ignored cache):

| Direction | Choices in 1.8.1 | Source |
|-----------|------------------|--------|
| Input | ALSA/OSS sound card, `stdin` (or `-`), `udp:<port>` | `src/audio.c:297-308,405-433,438-445` |
| Output | "Only soundcard is supported at this time": an ALSA PCM name (`null` works; no stdout/pipe/UDP output) | `src/audio.c` comment before `snd_pcm_open` of the output device |

Therefore "Dire Wolf on a loop" has these options:

| Option | Mechanism | Feasible? |
|--------|-----------|-----------|
| L1 ALSA `file` plugin to a FIFO, second Dire Wolf reads it on stdin | An `asound.conf` in the container defines `pcm.dwtx { type file; slave.pcm "null"; file "/tmp/dw/loop.pcm"; format raw }` (FIFO created by the entry script); Dire Wolf A uses `ADEVICE stdin dwtx` (RX from a paced silence feeder, TX into the FIFO); Dire Wolf B uses `ADEVICE stdin null` with `stdin` = the FIFO. The `file` and `null` plugins are part of libasound itself (alsa-lib `pcm_file.c`, `pcm_null.c`; not in the Debian plugin packages) | UNVERIFIED but the only no-privilege path; needs a spike (U6a). Risks: Dire Wolf may pace the null device differently; sample format/rate must match (44100 Hz, 16 bit, 1 channel, Dire Wolf defaults `ARATE`, `ACHANNELS`, UNVERIFIED in `config.c`); FIFO needs a dummy writer to avoid EOF on B |
| L2 ALSA `snd-aloop` kernel loopback | needs the host kernel module and `/dev/snd` in the container | Rejected: privileged, host dependent |
| L3 two instances with UDP audio | output has no UDP | Not possible (see table above) |
| L4 KISS-to-KISS bridging of two TNC ports | a python bridge copies A's KISS data frames to B's KISS clients | Possible but no modem path; equivalent to tier A, only with the real KISS decoder of Dire Wolf; useful as fallback |
| L5 RX by injected AFSK audio only | `gen_packets` makes AFSK WAV of a TNC2 line, written into the RX stdin of ONE Dire Wolf (this is the existing `rx` job of the driver) | Feasible today for the RX half (6a, 6e); TX half (6b-6d) is then observed through the KISS port with a second KISS client (KISSCOPY) |

Recommendation: L1 for the full loop (spike first), with L5 as the RX fallback and tier A as the logic fallback.

What the DV-Router edge connects to: `kind = "kiss-tcp"`, `host`/`port` of the Dire Wolf KISS TCP port (8001) as a CLIENT
(`data.md:1873-1877`); one port per Dire Wolf instance (`rf-a` to instance A with `tx = true`, `rf-b` to instance B RX only,
`room = "B"`). Dire Wolf serves only 3 clients per instance (`repo:docs/direwolf-cases.md:73-75`): DV-Router 1, the tester 1,
one spare. The tester injects "RF" by sending a data frame to instance A's KISS port (A transmits, B hears it, B forwards to
DV-Router): that is the loop for 6a. DV-Router's own TX leaves via A, passes the loop, B demodulates it, and the tester (second
KISS client on B) reads the frame; DV-Router's RX port also hears its own frame, which it must ignore as an own identity
(`data.md:1653,1679`), a built-in negative check. For 6e a feeder tees the same audio into both instances.

New service (U7): `dwloop` (profile `dwloop`), address `<NET>.47` (A) and `<NET>.48` (B) as two containers of the same image,
or one container with two processes and two KISS ports (8001 and 8002; one container is simpler: one `mem_limit` set). Free
host numbers in the rig: .46-.49 and .98-.99 (used: `repo:docs/rig.md:55-87`, `repo:rig/compose.yml:5-75`). Limits `x-limits-small`
(a Dire Wolf process is a few MB; the image is already 180 MB), no `--capture`, no internet, `read_only`, `cap_drop ALL`, tmpfs
`/tmp`. The entry script gets a new mode `loop` (supervisor: create the FIFO, write the config files, start A and B, the paced
silence feeder, restart on exit, print the KISS ports); the tests never get a docker socket, they only open TCP.

### 4.3 Group (3): the kill

Covered in 3.3. The new allow-list entry is `("xlxd-sut", "dv-data-worker"): ("KILL",)`; in `killproc.sh` add
`dv-data-worker:KILL` to the `case "$2:$3"` list; extend the header text of `scripts/run.sh`, the docstring of
`dvtest/control.py`, the table in `cases/unit/test_control.py`, `docs/rig.md` "Control channel" and the SKILL row. Because
the launcher respawns the worker with a backoff of 0.5-5 s (`data.md:323`) no restart action is needed. A `restart_child`
command exists only on the fabric (`admin/launcher/cmd`, `data.md:452`) and is not reachable from the tester; the SIGKILL is
the right model of a crash (`data.md:443`).

## 5. Implementation plan (ordered, small units)

Sizes: "image" = on-disk size; "run" = wall time of one rig run of the units' cases, excluding rig start (about 1-2 minutes) and
one `Hub.settle()` per case.

| Unit | Files to create or change | Image / size | Run | Depends on |
|------|--------------------------|--------------|-----|------------|
| U0 shared helpers and snippets | `cases/data_support.py` (move `Hub`, listeners, SMS builder, timeline, web event waiter out of `test_data_ident.py`, which then imports them: behaviour unchanged, `pytest -m unit` and the data-ident rig run must stay identical); `dvtest/web.py` operator-events reader; `rig/config/dv-router/dp15-paths.toml`; `DP15_TX_ENABLE` token in `dvtest/sut_config.py` + `run.sh`; unit tests | none | unit only | - |
| U1 worker kill action | `scripts/lib/killproc.sh`, `dvtest/control.py`, `cases/unit/test_control.py`, `scripts/run.sh` header, `docs/rig.md`, SKILL row | none | unit only, 1 spike rig run (kill + restart seen in `proc-list`) 3 min | - |
| U2 fake KISS TNC | `dvtest/kiss_tnc.py` (`FakeKissTnc`), `cases/unit/test_kiss_tnc.py`, `rig/config/dv-router/dp15-ax25-fake.toml`, `docs/dp15-cases.md` section | none | unit only | U0 |
| U3 stream-metadata cases | `dvtest/ysf_gps.py`, `dvtest/dmr` builders (`dmrg_packet`, `dmra_packet`), `m17.meta_text`, `dstar_sd.gps_over`, unit tests for each builder; `cases/test_dp15_stream.py` (1a, 1b, 1c, 2a, 2b, 2c, 2d, 4a, 4b, 4c) | none | about 10 min (R1) | U0 |
| U3b worker cases | `cases/test_dp15_worker.py` (3a, 3b, 1d) | none | about 5 min | U0, U1, U3 helpers |
| U3c RF cases tier A | `cases/test_dp15_rf.py` (6a-6e) | none | about 8 min (R2; 6d is the long one) | U0, U2 |
| U4 aprsc image | `rig/Dockerfile.aprsc`, `scripts/pre-fetch-aprsc.sh`, `.env.example` pins `APRSC_REPO` / `APRSC_COMMIT` / `APRSC_BASE_IMAGE`, `docs/aprsc-cases.md` (build notes) | see 4.1 (aim < 150 MB; hard limit 600 MB) | build 3 to 5 minutes (UNVERIFIED), inside `contained.sh --name build` | - |
| U5 aprsc service and cases | `rig/compose.yml` service `aprsc` (profile `aprsc`, `<NET>.46`, `x-limits-small`, fixture config mounted read-only, `read_only`, tmpfs), `rig/aprsc/aprsc.conf`, passcode helper `dvtest/aprs_passcode.py`, `dvtest/aprsis.py`, run.sh fixture secret `out/<run>/aprs-secrets/passcode` (mode 600, mounted at `/etc/dv-router/aprs-secrets`, like the ASL secrets), `dp15-aprsis.toml`, `cases/test_dp15_aprsis.py` (5a, 5b), `cases/unit/test_aprs_passcode.py` | none beyond U4 | about 4 min (R3) | U0, U4 |
| U6 Dire Wolf loop spike | `rig/direwolf-entry.py` new mode `loop`, `rig/dwloop/asound.conf`, `rig/dwloop/*.conf`; one manual `docker run` of the image with `--network none` (spike: does the `file` plugin write TX audio into a FIFO, does B decode it, timing) | image unchanged in layers; size stays about 180 MB | spike 10 min | none; do NOT start before the rig chain in the other worktree has finished |
| U7 Dire Wolf service and cases tier B | `rig/compose.yml` service `dwloop` (profile `dwloop`, `<NET>.47`), `dp15-ax25-dw.toml`, `cases/test_dp15_rf_dw.py` (6a, 6b, 6c with `atest` decode of the TX audio, 6e with tee), docs | about 180 MB (existing image) | about 6 min (R4) | U6, U3c (shares the case logic through `data_support`) |
| U8 second DV-Router peer | `rig/compose.yml` service `dvr-peer` (profile `dvrpeer`, `<NET>.16`, the same image, its own `out/<run>/xlxd-config/dvr-peer/dv-router.toml`), `run.sh` render of the second config, `cases/test_dp15_xlx.py` (7a now, 7b-7d with the peer) | none (no image build) | about 6 min | U0; 7a can go first without U8 |
| U9 docs | `docs/dp15-cases.md` (results and findings), `docs/README.md` row, SKILL "Pick the level" rows for each run shape | - | - | after the cases |

Order of work (each step is one commit):
1. U0, 2. U1 (spike rig run), 3. U2, 4. U3 builders and unit tests (no rig), 5. `test_dp15_stream.py` first on 1a, 2b (the web
path), then the rest, 6. U3b, 7. U3c tier A, 8. U4 then U5, 9. U6 spike then U7, 10. U8 and group 7, 11. U9.
Parallel lanes are possible after U0: (U1 -> U3b), (U2 -> U3c), (U4 -> U5), (U6 -> U7) touch disjoint files, except `rig/compose.yml`
and `docs/rig.md`, which every service unit edits (merge carefully, one unit at a time).
Every rig run is one at a time (`dvt-rig.scope`), so the six runs R1..R5 + the spikes are serial: about 35 minutes of rig time for the
whole plan, plus 5 to 10 minutes per run for startup and counter settling.

## 6. Open questions for the DV-Router lead

* Q1 Latency bounds. "Within one superframe plus pipeline" (1a), "at once" (2b) and "within 2 s of the token" (3b) have no
  numbers in 15.5. Proposed: status visible on the public SSE <= 1.5 s from the first frame of the over; web SMS to an M17 listener
  <= 500 ms; first M17 render after the worker token <= 2.0 s; first D-STAR text over after the token <= 2.0 s idle gap + 1.0 s.
  Which are right?
* Q2 "Users only" for positions: positions are on the operator/admin feed only; no user-visible surface exists until the map (DP7).
  Should the case assert operator-only today, or wait for DP7 and assert a user API?
* Q3 Does `GET /api/v1/operator/events` and `/operator/stream` carry `data` events with `item_type = position` and the position
  fields (`status.md:224-225` says the admin feed does; `web.md:1032` says "operator hub admin-scope list")? Row shape?
* Q4 After a worker kill, are D-STAR text overs expected to resume within 2 s of the token, or 2 s + idle gap?
* Q5 Allow-list: may the control channel kill `dv-data-worker` (U1)? Is the comm name exactly `dv-data-worker` when the worker id is not `pkt`?
* Q6 APRS-IS: does the controller dedupe a repeated message by (type, guard callsign, body) only (`data.md:377`), ignoring the message id? And is the repeat window between the server's 30 s dupe window and the controller's 60 s window (5b) the intended way to test "acked again, not delivered twice"?
* Q7 The ack is sent with source `XLX999B` while the login is another callsign. aprsc 3.0.0 forwards it with `qAS` (research pass: `aprsc:parse_qc.c:597-598`); the public network may treat it differently. Is `qAS` the intended q-construct for the room acks, or should the edge log in as the room name? (Only a private aprsc is used here.)
* Q8 RF ack: the room name `XLX999B` has 7 characters and cannot be an AX.25 source (`data.md:1711`), so no RF ack is possible for it. Confirm that "messages heard on RF for the room are accepted without an ack" is the expected result for the standard room names.
* Q9 YSF GPS / DMR `DMRG`: which real or reference byte layouts may the cases use (the YSF client source is not pinned in dv-testset)? May dv-testset pin `g4klx/YSFClients` and use `MMDVMHost` `DMRNetwork.cpp` (already pinned for slow data) as the layout source?
* Q10 PTT watch for RF TX: with no PTT publishers in the rig, does the AX.25 edge complete its token snapshot and transmit (`data.md:1954-1957`)? If not, is there a config or fabric stub for `rf/<physical>/ptt`?
* Q11 A second DV-Router node as XLX peer for 7b-7d: is the all-in-one launcher with an `[edges.xlx.peer]` row and the fake transcoder supported on a second container, and which interlink version (`edges.xlx.port`) do two DV-Router nodes speak?
* Q12 One status per 30 s: is the "changed status at most once per 30 seconds" limit per stream, per callsign, or per room (`data.md:735-737`)? It decides whether 1a/4c need a distinct callsign per case.

## 7. Not verified (summary of the UNVERIFIED marks)

The `file` ALSA plugin loop (4.2), the operator-event body of a position (1b), the exact TOML spelling of `Enable` values, the DMRA/DMRG
packet byte offsets beyond what `data.md:697` quotes, the YSF GPS DCH layout, whether `dmrid.dat` maps the DMR test id to a test callsign,
the PTT-watch behaviour without publishers (6c), all timing bounds (Q1), all run-time estimates, the rig start time, the aprsc build time,
and whether the status/text dedupe keys are body-only. Nothing in this plan was executed.
