<!-- SPDX-License-Identifier: MIT -->
# DP15 cases: stream metadata (groups 1, 2, 4), the worker lifecycle (3), RF tiers A and B (6) and the XLX peer wire (7a)

Status: the first rig run (image `dv-router:wip-0348b98`, native, `DVT_SUT_GEN=compat1`; streams 8 passed / 6 failed, worker
0 / 2, RF tier A with `DVT_DP15_TX_ENABLE=false` 4 passed / 2 failed / 3 skipped) found no DV-Router defect: every failure
traces to a harness defect (re-verification pending), fixed in commits 98415bb, 4b35fc3, ecf47ee and 47c6efd (section "First rig run:
diagnosis"). The corrected cases have NOT been run again. Cases that passed in that run stay `passed` in the tables;
cases marked "written, not run on a rig" in the tables below have either not run yet or are listed in the diagnosis.
Specification and the other groups: [dp15-plan.md](dp15-plan.md). Source of the 15.5 wording:
DV-Router `docs/design/data.md` lines 2442-2450.

Case file `cases/test_dp15_streams.py` (APRS-IS: `cases/test_dp15_aprsis.py`, below), shared helpers `cases/data_support.py` (moved out of
`cases/test_data_ident.py` without a change of behaviour), builders in `dvtest/proto/ysf_gps.py`,
`dvtest/proto/dmr.py` (DMRG, DMRA), `dvtest/proto/m17.py` (legacy META text), `dvtest/dstar_sd.py`
(`tx_message_fields`, `text_gps_fields`, `over_packets`, `aprs_position_payload`), web readers in `dvtest/web.py`
(`operator_events`, `room_messages`, `post_room_text`).

## Run

One rig run, profile `web` (the cases read the web API; the tester plays the XLX peer, no `interlink` profile):

```
XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-paths.toml:\
rig/config/dv-router/xlx-peer-tester.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_streams.py
```

* Snippet ORDER: the web snippets first, then `data-ident.toml` (as written above). Before commit 4b35fc3 the opposite
  order failed (`data-ident.toml` carries the bare token `require_ident = <REQUIRE_IDENT>`; the merge of the next extra,
  `web.toml` into `[web]`, parsed the merged text as TOML BEFORE `render()` replaced the tokens: "merged extra is not
  valid TOML"; `run.sh` appends `web.toml` itself when it is not listed, so naming `data-ident.toml` alone also hit it).
  The check now parses the text with the tokens replaced, so the order no longer matters; the documented order is kept.
  Reproduce: `python3 -m dvtest.sut_config TEMPLATE OUT --net 172.30.0 --require-ident true --extra rig/config/dv-router/data-ident.toml --extra rig/config/dv-router/web.toml`.
* `DVT_REQUIRE_IDENT=true` is set explicitly (false also works; the case notes print `require_ident=<value>`).
* `data-ident.toml` turns the data worker on (`DVT_REQUIRE_IDENT` true or false, both fine; default true);
  `dp15-paths.toml` the room path; `web.toml:admin.toml:totp-required.toml` as for every web run (the web fixtures
  start from "first login = enrol", `docs/rig.md`); `xlx-peer-tester.toml` only for 7a (without it 7a skips).
* `DVT_TRANSCODER_FAKE=1`: the fake extended AMBEd (deterministic transform, `docs/ambed-fake.md`). Needed to
  ASSERT the M17 voice bytes of 2b; without it the comparison is only recorded. 2c needs a transcoder (the fake or the
  real one) for the M17 voice towards D-STAR.
* A case skips with its reason when a prerequisite is missing: not DV-Router / not native launch / no `[data]` family
  in the hub metrics (hub fixture, as in `test_data_ident.py`), no web profile (web fixtures), no XLX link (7a).
* Wall time estimate (UNVERIFIED): 12 s quiet baseline per counter case, 11 s / 31 s waits of the pacer between cases
  that reuse a callsign, 2b runs its QSO twice: roughly 8 to 12 minutes in all.
* Notes: stdout and `out/<run>/dp15-streams.txt` (`RECORD` what was seen, `FINDING` differs from the documented
  behaviour).
* Config check of the snippets (host side, throw-away container, `--network none`):
  `python3 -m dvtest.tools.config_check --image dv-router:wip-0348b98 --template <template> --gen compat1 --extra rig/config/dv-router/data-ident.toml --extra rig/config/dv-router/dp15-paths.toml` prints `valid` (done).
  The tool could not check `web.toml` in that combination before commit 4b35fc3 (same cause as the snippet order above); it can now.

## First rig run: diagnosis (all harness defects)

Evidence: the run directories of the first chain (pytest.log, `dp15-*.txt`, `logs/xlxd-sut.log`, `logs/web-proxy.log`) and
the DV-Router sources read-only. Re-verification means one more chain with the same commands as in "Run".

| Case | Symptom | Root cause | Fix | Re-verified by |
|------|---------|-----------|-----|----------------|
| 1a, 1c (control), 4c [alias], 4c [foreign-callsign], 1b (stream half), 3a, 3b, 6a / 6e (SSE 0) | no `status` / `text` / `data_worker_*` event on an SSE watcher | `dvtest.web.WebClient.sse` with `stop=` polled the buffered response with a 0.5 s socket timeout; after a timeout inside a buffered read the response raises `OSError: cannot read from timed out object`. Every `SseWatch` therefore died 0.5 s after the hello (`logs/web-proxy.log`: every `GET /api/v1/public/events` and `/operator/stream` ends after 541 ms with "IncompleteRead", although the cases ran 7 to 20 s). The product delivered the items: `dv_room_data_total` and the controller counters moved for 1a, 1b, 1c, 2c, 4c; 1b's position was in the operator history (`position row keys [...]`) while its stream saw nothing. 4a / 4b / 2c passed because they read the history (`GET /operator/events`), not an SSE | the read blocks for the time left; `stop` shuts the socket down from a helper thread (`dvtest/web.py`); unit tests with a quiet gap after the hello and with `stop` inside a gap | 1a, 1b, 1c, 4c [both], 3a, 3b, 6a, 6e |
| 2d | no position row (operator rows 0), first run and again in the second run (`out/20261107T083645Z`, 13 of 14 passed) | pacing: the controller limit is 1 position per 30 s per base callsign (`crates/room/src/data.rs` `BaseRate.position`, bucket 30 s, burst 1; `N0TEST-7` and `N0TEST` are one base), and `PACER` stamped the CALL, not the offer. Measured in the second run (capture, hub dumps `control/8-*.out`, case notes): 2c's pacer call 08:41:25.1 (2d slept to 08:41:59.1 = call + 34 s), then two `dl()` DExtra clients (about 3.7 s each: link plus 1.2 s client list refresh), header at 08:41:32.52 (capture), D-PRS line complete 1.1 s later, accepted about 08:41:33.7; 2d's M17P datagram (capture: 77 bytes, SRC N0TEST-7, specifier 0x02, TNC2 text) at 08:42:00.32, 1.2 s after its own call (link): 26.6 s after 2c's position, under 30 s. The datagram reached the controller (the packet bytes parse as a position; a bad packet would count a reject, not `rate`): `dv_room_data_total{room=b,result=rate}` 1 -> 2 between the dumps 08:41:24 and 08:42:33. Counters of the second run, worker restarted at 08:39:35 by 1d[killed]: position `accepted` 3 = 2c (08:41:33), 4a (08:42:2x), 4b (08:42:55); `rate` 1 = 2d; `duplicate` 1 = 1d[killed], NOT 4a's second GPS record (dump 8-25 at 08:40:47 already had duplicate=1, before 2c, 2d and 4a; 1d[killed] re-sends the body of 1b and 1d[alive], the controller checks the fingerprint item type + base + body before the rate bucket); `received` 3 = the accepted items the worker got back (it is the watermark counter, not an offer count). The first-run reading (offer delay 1.6 s, constant margin `OFFER_DELAY_S` 3.0) was too small: the real lag between the call and the offer of a D-STAR case is 8.6 s (2 links), more than the margin. The operator feed baseline of 4a (330 bytes) is still 2c's position row | `Pacer.offered(callsign, kind)` called right after the send in every DP15 stream case that offers a status or a position (1a, 1b, 1c control, 1d, 2c, 2d, 4a, 4b, 4c); the next `wait()` counts from the real offer (unit test `test_pacer_offered_restamps_the_call_with_the_real_offer`); `OFFER_DELAY_S` stays as margin | 2d: not run again yet. Expected on a run: position row with source 5 (`dv_room_data_total{result=accepted}` +1, no `rate`); if 2d still shows `rate`, the offer is closer than 30 s to another position of N0TEST and the notes show which |
| 6a, 6e (D-STAR 5) | "5 D-STAR text overs, want 1" | DV-Router's DExtra edge sends every header 5 times at the start of an over (xlxd `repeatCount 5`, DV-Router `crates/edge/dextra/src/wire.rs` `HEADER_REPEAT`, edge README fast path); the helper counted header datagrams (arrival times differ by microseconds), not streams | `data_support.first_header_times` (one entry per stream id, first arrival) used by `text_headers`, `dstar_times` (RF) and `dstar_text_times` (worker); unit test | 6a, 6e, 6b |
| 3a, 3b | `data_worker_down` / `data_worker_up` never seen | the same SSE defect. The events exist: `crates/svc/status/src/hub_state.rs` `Input::DataWorker` emits a PUBLIC `status` event `{"ev": "data_worker_up" or "data_worker_down", "ts_ms", "worker", "kind": "data-worker"}` when the data-worker token toggles (`docs/design/status.md` 332). The kill itself worked: launcher log `dv-data-worker[pkt]: exited: signal: 9 (SIGKILL)`, `restarting in 500 ms`, `started (pid <new>)` 0.5 s later, `proc-stat` gave the new pid. The plan's "UNVERIFIED" for these events is resolved | SSE fix; the case already falls back to the new pid from `proc-stat` as the token when no up event arrives (the `data_worker_*` assertions stay: they are in the status contract) | 3a, 3b |

Observable signals of a kill and restart (for the cases): launcher log lines `dv-data-worker[<id>]: exited: signal: 9 (SIGKILL)`,
`restarting in 500 ms`, `started (pid N)`; the public SSE `status` events `data_worker_down` then `data_worker_up` (token); `proc-stat
xlxd-sut dv-data-worker` with a new pid; `dv_edge_raw_total{result="no_worker"}` while down.

Not diagnosed here: 5 skipped RF cases (`DVT_DP15_TX_ENABLE=true` run) and the passing cases of the first run.

## Bounds (module constants of `cases/data_support.py`)

From the DV-Router lead's answer to the DP15 questions (read-only, section numbers of that answer):

| Constant | Value | Meaning | Where it comes from |
|----------|-------|---------|---------------------|
| `STATUS_CEILING_S` | 2.0 s | "within one superframe plus pipeline": measured from the send time of the first superframe's last frame (frame 20) to the SSE arrival. A superframe is 420 ms, one raw unit per superframe, so 0.42 to 0.84 s plus tens of ms; the 2.0 s is a CEILING, the measured value is recorded | answer 1 |
| `AT_ONCE_S` | 0.5 s | "at once": M17 listener datagram after the submission (M17 SMS / web POST); the independent paths have no timer | answer 1 |
| `IDLE_GAP_S` | 2.0 s | "only in a pause of at least 2 s": `[data] idle_gap_ms` 2000 from the last voice end (asserted `>= 2.0 - 0.05`) | answer 1, `data.md:1109,1122` |
| `TEXT_OVER_LATE_S` | 4.1 s | latest start of the text over after the voice end: idle gap + slot 600 ms + 1.5 s pipeline (no documented number) | answer 1 (`slot_ms` 600) |
| `TOKEN_RENDER_S` | 2.0 s | worker token to first render (group 3, not used by these cases) | answer 4 |
| `MIN_GAP_S` | status 11 s, position 31 s, text 6 s | controller limits per base callsign (status 1 per 10 s, position 1 per 30 s, burst 1) and the room text bucket (1 per 5 s, burst 4; web account 1 per 5 s, burst 2). `data_support.PACER` waits so the cases do not rate-limit each other | answer 12 |

Applied answers: the position assertions are for the OPERATOR scope only (answer 2); what anonymous clients and the
user history show is a `RECORD`, never an assertion (`GET .../messages` carrying a position is a `FINDING` against
web.md 1178, not a failure). The `/operator/events` row shape is the one of answer 3.

## Case table

"Assert" = the test fails; "record" = a line in `dp15-streams.txt`.

| Case | 15.5 wording (`data.md:2444-2450`) | Steps | Assert | Record | Status |
|------|-----------------------------------|-------|--------|--------|--------|
| DP15-1a | "a radio TX message appears as the talker's status on the web within one superframe plus pipeline" | D-STAR talker N0TEST, 3 superframes with the 20 character TX message in blocks 0x40..0x43 of every superframe; public SSE open before | ONE `status` data event of N0TEST with the text, <= `STATUS_CEILING_S` after frame 20, room B; the row in `GET /user/rooms/B/messages`; `dv_pkt_items_total{kind=status,result=accepted}` and `dv_room_data_total{result=accepted}` move | latency from the end of the first superframe and from the header; a status BEFORE the superframe ended is a FINDING | written, not run on a rig |
| DP15-1b | "a D-PRS GPS-A beacon appears as the talker's position (users only)" | over with the TX message and `$$CRC...,N0TEST>API510,DSTAR*:!1030.00N/02015.00E>test` interleaved (pattern text-gps); operator stream + events, anonymous SSE | ONE position row of N0TEST in the operator feed: source 1, 10.5 N 20.25 E (+-2e-5 deg), symbol `/>`, room B, within `STATUS_CEILING_S` of the superframe that completes the line. Spoof: a line with source `N0XYZ` gives no position and `dv_pkt_reject_total{reason=spoof}` moves | what anonymous SSE / `public/state` / the user history show of positions (the "users only" half has no user surface yet, answer 2) | written, not run on a rig |
| DP15-1c | "loss of a required text block loses the text and never produces a partial status" | every superframe lacks block 2; control over with all four blocks | no status event, no history row, no accepted status counter; the control over gives the status | counter deltas | written, not run on a rig |
| DP15-1d | "the native D-STAR to D-STAR slow data is byte-identical with and without the worker (D3)" | over with text + D-PRS, a listener on the module; `alive` and `killed` | every AMBE frame and every slow data field at the listener equals what was sent | frame counts | `alive`: written, not run on a rig. `killed`: written, not run on a rig (the allow-list entry `kill-proc xlxd-sut dv-data-worker KILL` exists since U1; the case still skips with a reason when `KILL_PROC_ALLOWED` lacks it) |
| DP15-2a | "an SMS during a D-STAR QSO reaches M17 clients at once and D-STAR clients only in a pause of at least 2 s" | T (N0TEST) keys 5 s of voice on D-STAR, D (D-STAR) and L (M17) listen, S (M17, N0CALL) sends an SMS 1.5 s into the over | L gets the packet byte for byte within `AT_ONCE_S` and before T's last frame; no text over header at D while T is on air; the text over `N0CALL: <body>` starts `>= IDLE_GAP_S` and `<= TEXT_OVER_LATE_S` after T's last frame; T's voice at D is unchanged | timings | written, not run on a rig. Honest limit: with an M17 sender "at once" is the native relay (`data.md:1012`) |
| DP15-2b | same, the render path | as 2a with a web user (`may_tx`) POST at +1.5 s, after a control run without the message | POST 202; L gets an `M17P` SMS containing the text within `AT_ONCE_S` of the POST and before T's last frame; D-STAR rules of 2a with prefix `N0DPT: `; M17 voice payloads equal to the control run (asserted with `DVT_TRANSCODER_FAKE=1`) | header of the text over, whether room C got the forwarded copy (`dp15-b-to-c`), the voice comparison without the fake | written, not run on a rig |
| DP15-2c | "META text of an M17 talker appears as D-STAR text blocks in the cross-mode egress of that over and the D-STAR-sourced over is untouched" | M17 talker N0CALL, legacy 2 block META text (control bytes 0x31/0x32, each block held 1.12 s); a D-STAR listener; then a D-STAR over with text + D-PRS and a second D-STAR listener | the egress slow data decodes to the first 20 characters of the text, with the blocks at frames 1, 5, 9, 13 of a superframe; a `status` row of the talker; `dv_edge_render_total{kind=dstar-text20,result=framed}` moves; the D-STAR-sourced over arrives unchanged | decoded events | written, not run on a rig |
| DP15-2d | "an APRS payload in M17 packet mode (specifier 0x02) produces a position" | M17 client N0TEST sends `M17P` with specifier 0x02 and `N0TEST-7>APDVT1:!1030.00N/02015.00E>test`, SRC `N0TEST-7` | one position row (source 5), 10.5 N 20.25 E | callsign and protocol of the row | written, not run on a rig |
| DP15-4a | "a GPS fix in a YSF over ... give[s] one position" | YSF client N0CALL, 16 packets, FN 6/7 carry the GPS record twice (FT 7) | ONE position row, source 3, symbol `/[`, comment `FT-1D`, 10.5 N 20.25 E | rows | written, not run on a rig. The optional "changed fix inside 30 s gives none" check is NOT written |
| DP15-4b | "a `DMRG` in a DMR over give[s] one position" | DMR id 1023008 (= N0TEST in `rig/config/dmrid.dat`), DMRG inside the stream; then a DMRG with no open stream | ONE position row, source 4, symbol `/-`; the outside DMRG gives no position and `dv_pkt_reject_total{reason=unbound}` moves | rows | written, not run on a rig |
| DP15-4c | "a `DMRA` gives a status and never changes the displayed callsign" | DMR id 1023008, two DMRA blocks of the alias `N0TEST ALIAS` (and `N0CALL`, another station's call) inside the stream; `public/state` sampled every 0.4 s | a status event of N0TEST with the alias text; the on-air callsign of room B is only `N0TEST`; the first last-heard entry is N0TEST | samples, last heard | written, not run on a rig |
| DP15-7a | "XLX peer: ... synthetic text over" (wire half only) | the tester is XLX996 (modules DE, `xlx-peer-tester.toml`), two web texts to module D back to back | two complete streams; header MY `XLX999`; 22 frames, 21 with null AMBE; the slow data is a valid text over `N0DPT: <body>`; header to last frame 0.3 to 0.9 s; the second over starts >= 0.85 s after the first ended (V3: 1 s) | per-stream timing | written, not run on a rig |

### Worker lifecycle (group 3): `cases/test_dp15_worker.py`

The kill action exists since unit U1: `kill-proc xlxd-sut dv-data-worker KILL` (`dvtest.control.KILL_PROC_ALLOWED`,
`scripts/lib/killproc.sh`, header of `scripts/run.sh`, `docs/rig.md` "Control channel"). The process name is always
`dv-data-worker` (the worker id is only in the env `DV_DATA_WORKER_ID`, the launcher label `dv-data-worker[<id>]` and the
token `pkt/<id>`), the launcher respawns it after 0.5 s doubling to 5 s, back to 0.5 s after a run of 10 s (answer 5).
Same run shape as above (profile `web`, `DVT_TRANSCODER_FAKE=1` for the M17 voice of 3a, no `xlx-peer-tester.toml`
needed); `scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_worker.py`. Notes: `out/<run>/dp15-worker.txt`.
Accounts: reader `N0DWR`, posters `N0DWA` (the text posted while the worker is down) and `N0DWB` (the new text at the
token; a second account so the per-account bucket of 1 per 5 s never delays it). Skips: not DV-Router / not native / no `[data]`
family (hub fixture), no web profile, no control channel (`DVT_RIG_CONTROL`), no `dv-data-worker` process.

The token is the `data_worker_up` status event of the public SSE (`status` events carry `ev`, `worker`, `kind`; status.md,
web.md `live.data_workers`); the first successful `proc-stat xlxd-sut dv-data-worker` of the new pid (pid differs, `t` read
inside the container) is recorded next to it.

| Case | 15.5 wording (`data.md:2446`) | Steps | Assert | Record | Status |
|------|-------------------------------|-------|--------|--------|--------|
| DP15-3a | "kill the worker during traffic: raw counters, voice untouched, web chat works" | D-STAR talker N0TEST, 30 superframes (12.6 s) with the TX message; D-STAR listener N0CALL, M17 listener N0TEST; 4 s in `kill-proc ... KILL`; web text G (account N0DWA) right after the kill; wait for down and up events and a new pid; new web text N (account N0DWB) at the token | listener hears every AMBE frame and slow data field unchanged, no silence over 0.25 s; M17 stream (if the fake transcoder is on) no silence over 0.5 s; down then up event, new pid; POST 202 and the row in `GET /user/rooms/B/messages`; G never rendered (no M17 SMS, no D-STAR text over); N rendered on M17 (<= `AT_ONCE_S + TOKEN_RENDER_S` after the POST) and as a D-STAR text over; `dv_edge_raw_total` +5 or more; `no_worker` > 0 when the down window lasted 1.5 s or more; `dv_pkt_ready` back to 1 | window, pids, silences, counter deltas | written, not run on a rig |
| DP15-3b | "restart: renders resume within 2 s of the token" | 3 rounds (the worker ran 11 s before each kill, so the backoff is 0.5 s again): kill, text G while down (N0DWA), wait for `data_worker_up` (T_token), text N at once (N0DWB) | N reaches the M17 listener as an SMS within `TOKEN_RENDER_S` (2.0 s) of T_token; the D-STAR text over of N starts within `TOKEN_RENDER_S + IDLE_GAP_S` (4.0 s: answer 4, "2 s + idle gap" is the safe upper bound for an idle room); G is never rendered, also not later (judged only when its POST fell inside the measured down window) | down event, token, POST, M17 and D-STAR times, new pid | written, not run on a rig |

Honest limits: the down window is the interval between the `data_worker_down` and `data_worker_up` events (SSE latency
included); a kill that is answered after the worker is back (window too short) makes G a normal item, which the case then
records and does not judge. The lead says the D-STAR text over after the token is "within 2 s for an idle room, voice end
+ idle gap otherwise"; the case asserts the safe upper bound and records the measured value. `no_worker` is asserted only for
a down window of 1.5 s or more (the edges learn the token state with some delay; UNVERIFIED how long).

### RF through AX.25 / KISS, tier A (group 6): `cases/test_dp15_rf.py`

The tester plays the KISS TNCs: `dvtest/kiss_tnc.py` (`FakeKissTnc`) listens on TCP, the DV-Router AX.25 edge connects to it as a
`kiss-tcp` CLIENT (`[[edges.ax25.port]] kind = "kiss-tcp"`, `host`, `port`), writes the five KISS parameter frames per channel
after every connect (TXDELAY 30, P 63, SlotTime 10, TXtail 3, FullDuplex 0 in 10 ms units = the `kiss` defaults), reads the UI frames
the TNC injects (what a modem hears, built with `dvtest/ax25.py`) and writes the frames it transmits. The fake TNC records the
parameter frames per connection, the transmitted data frames with arrival times, and any other KISS command (the edge never
writes SetHardware or Return). No audio, no Dire Wolf; tier B (two real Dire Wolf TNCs on a modem loop) is the next section.

Snippets (`docs/rig.md`): `rig/config/dv-router/dp15-rf.toml` (the edge enabled, station callsign `N0TEST-1`, ports `rf-a`
`<TESTER_IP>:8101` and `rf-b` `<TESTER_IP>:8102`, reconnect 1 to 5 s, the path `rf-in` from all ports to room B for text addressed to
`XLX999B`) and `rig/config/dv-router/dp15-rf-out.toml` (the path `rf-out`, room B to `edge:ax25/rf-a`). The token
`<DP15_TX_ENABLE>` (env `DVT_DP15_TX_ENABLE`, `true` or `false`, default `false`, any other value is exit 2) sets `tx_enable` and the
`tx` switch of `rf-a` together. **Why the rf-out path is a second file:** DV-Router refuses `tx = true` without `tx_enable = true`
and a path TO an RF port without effective TX on it (`data.path[N].to: RF TX refused: destination requires effective TX on every
selected port`, checked with `dv-router:wip-0348b98`), so the room-to-RF row cannot sit in the same file as a token that can be
`false`. The two run shapes:

| Shape | Env / snippets | Cases that run |
|-------|----------------|----------------|
| RF-OFF (receive only) | `DVT_DP15_TX_ENABLE=false`, `dp15-rf.toml` | 6s, 6a, 6b `[tx_enable=false]`, 6e |
| RF-ON | `DVT_DP15_TX_ENABLE=true`, `dp15-rf.toml` AND `dp15-rf-out.toml` | 6s, 6a, 6b `[port rf-b rx only]`, 6c, 6d, 6e |

The cases read the RENDERED config of the run (`out/<run>/xlxd-config/xlxd-sut/dv-router.toml`) to learn the shape and skip with
the reason in the other one, or when the run has no AX.25 edge. The fixture starts both TNCs and fails when the edge does not connect
in 90 s. Notes: `out/<run>/dp15-rf.txt`.

Run commands (two runs; profile `web` because the posters are web accounts; image, template and the fake transcoder as above):

```
XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=false \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_rf.py

XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=true \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf.toml:rig/config/dv-router/dp15-rf-out.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_rf.py
```

Config check (host side, throw-away container, `--network none`; `valid` for both): `DVT_DP15_TX_ENABLE=false python3 -m
dvtest.tools.config_check --image dv-router:wip-0348b98 --template <template> --gen compat1 --extra rig/config/dv-router/data-ident.toml
--extra rig/config/dv-router/dp15-rf.toml`, and the same with `DVT_DP15_TX_ENABLE=true` and `--extra rig/config/dv-router/dp15-rf-out.toml`
added. `dp15-rf.toml` with `false` plus `dp15-rf-out.toml` is INVALID (the RF TX refusal above). The tool cannot check `web.toml` in any
combination before commit 4b35fc3 (the merge was validated before the token substitution); it can now.

Wall time estimate (UNVERIFIED): RF-OFF about 5 minutes (two 12 s baselines, 6e waits 31 s), RF-ON about 9 minutes (6c waits for
the 30 s TX spacing twice, 6d watches 140 s).

| Case | 15.5 wording (`data.md:2449`) | Steps | Assert | Record | Status |
|------|-------------------------------|-------|--------|--------|--------|
| DP15-6s | (data.md 10.4: KISS parameters written at connect and on reconnect) | each TNC reads its connection; then rf-a drops it | TXDELAY, P, SlotTime, TXtail, FullDuplex in this order with the `kiss` defaults on channel 0 on both ports; the edge reconnects within 30 s and writes them again; no other KISS command | the parameter lists | written, not run on a rig |
| DP15-6a | "a message to the room name on RF reaches the room" | rf-a injects `N0TEST-7>APDVT1,WIDE1-1::XLX999B  :DVT nnnn{3`; D-STAR listener, M17 listener, public SSE, room history | exactly one M17 SMS within 1.0 s, one D-STAR text over within idle gap + 2.1 s, one SSE `text` event, one history row in room B; NO frame at either TNC for 8 s (no RF ack: the room name has 7 characters, `alias_calls` is empty); `dv_pkt_items_total{kind=text,result=accepted}` and `dv_edge_modem_total{port=rf-a,result=rx}` rise | latencies, the D-STAR text as displayed, the row's callsign and protocol | written, not run on a rig |
| DP15-6b off | "room text goes out as a group message only when `tx_enable` ..." (negative half) | RF-OFF shape: a web user posts a text to module B | the room gets it (M17 SMS and D-STAR text over); 12 s with no data frame at either TNC and no KISS command but the parameters; `dv_edge_modem_total{result=tx}` unchanged | counts | written, not run on a rig |
| DP15-6b port | the same for a port without `tx` | RF-ON shape: one text; rf-a transmits, rf-b is receive only | a frame at rf-a (the control) and none at rf-b | counts | written, not run on a rig |
| DP15-6c | "room text goes out as a group message only when `tx_enable` and the budget allow" | RF-ON shape: a web text; then, 31 s later, a 100 character text | exactly ONE data frame at rf-a: UI frame from `N0TEST-1` to `APZDVR`, no digipeaters, information field `:XLX999B  :<text>`, no `{id`, text <= 67 characters and containing the posted body; none at rf-b; `dv_edge_modem_total{port=rf-a,result=tx}` rises; the 100 character text: cut to <= 67 characters or refused (recorded which) | frame, scheduler counters | written, not run on a rig |
| DP15-6d | "never above the duty cycle" | RF-ON shape: six texts from three accounts in about 8 s, 140 s of watching | at most `tx_per_min + tx_burst` (3) frames in any rolling minute; consecutive frames >= the budget's min gap (1 s); DV-Router's airtime upper bound (`dvtest.airtime.afsk_airtime_ns`) of the frames in any budget window within duty x window (5 % x 600 s); none at rf-b | frame times, drop counters | written, not run on a rig. Only the class defaults are exposed (`tx_budget = {}`); a low budget would starve 6c in the same run, so a low-budget run is NOT written |
| DP15-6e | "a frame heard on two ports is one item" | the same message heard on rf-a and 0.4 s later on rf-b (other via list); 31 s later a different message on rf-b | one M17 SMS, one D-STAR text over, one SSE event and one history row for the first; `dv_pkt_rf_heard_ports_total` and `dv_pkt_rf_duplicates_total` rise (when the families exist); the different message is a second item; no frame transmitted at either TNC | counters | written, not run on a rig |

Assumptions about the AX.25 edge that could not be verified without a rig (all from the DV-Router sources, none executed):

* The edge connects to a TNC that is listening only after it started; the fixture relies on the snippet's `reconnect = 1..5 s` (the
  default 2..60 s would make the first connection slow, never wrong).
* TX is fenced until the PTT token snapshot completes; the lead's answer 10 says an empty snapshot (no PTT publisher in the rig) completes
  within its 2 s probe and allows TX. If the first frame never arrives, 6c fails with "exactly 1 frame, got 0" and the fence is the suspect.
* The group message is `:XLX999B  :<text>` with NO sender prefix (the worker test `rf_group_uses_configured_name...` shows
  `:XLX999-1 :Hello` for the body `Hello`); 6c asserts only that the body is contained.
* The M17 SMS and the D-STAR text over of an RF message show a source display callsign (`N0TEST-7` or `N0TEST`): the cases match on the
  body only. Bodies are at most 8 characters so `N0TEST-7: ` plus the body fits the 20 character D-STAR display.
* An RF-heard message to the room is accepted although `require_ident = true`: gateway origins (AX.25, APRS-IS) are exempt (data.md 2.3, 3.1 step 4b).
* Counter names and labels (`dv_edge_modem_total{port,result}`, `dv_edge_rf_tx_total{result}`, `dv_pkt_rf_heard_ports_total`,
  `dv_pkt_rf_duplicates_total`) are from data.md 13.1; a missing family is recorded, an existing one asserted.
* `tx_per_min` is a token bucket (rate `tx_per_min` per minute, burst `tx_burst`); 6d bounds the count by `tx_per_min + tx_burst` in a rolling minute, which holds for
  that reading and for a plain per-minute counter.
* The tester can accept TCP connections from the SUT on 8101 and 8102 on the rig network (nothing blocks them: the network is internal and the
  tester publishes nothing); not tried.

### RF through AX.25 / KISS, tier B: real Dire Wolf TNCs on a modem loop (group 6): `cases/test_dp15_rf_dw.py`

**What is real.** The AX.25 edge of DV-Router is the `kiss-tcp` client of two Dire Wolf 1.8.1 processes (rig services `dw-a`
172.30.0.48 and `dw-b` 172.30.0.49, profile `dw`, image `dvt-direwolf:local`, 180 MB, one process per container, KISS TCP port 8001).
The KISS bytes are Dire Wolf's own (its decoder reads the edge's parameter and data frames, its encoder writes what it
demodulated), and the **modem is real**: a frame sent into one TNC's KISS port is modulated by Dire Wolf's AFSK 1200 baud modulator
(300 ms TXDELAY flags from the edge's KISS parameter, CSMA, the frame with its FCS), the samples leave through the ALSA file PCM into a
FIFO, a relay of ours sends them as UDP datagrams to the other container, and the other Dire Wolf's demodulator decodes them and hands the
frame to its KISS clients. It is the full modem loop (not "tier B-lite", the KISS-level relay that was the fallback): VERIFIED outside the rig,
two real instances, both directions, byte for byte, in one container and in two containers on an internal network
([direwolf-cases.md](direwolf-cases.md), "Modem loop"). What is NOT real: no radio, no noise, no collision (the two directions do not interfere),
the audio leaves 4 times faster than real time, and a TNC is deaf for about the air time of its own last transmission (the cases pause).

Roles: the tester is the second KISS client of each TNC (`dvtest/kiss_client.py`; Dire Wolf serves three at a time), the edge's port `rf-a`
is dw-a (transmit and receive), `rf-b` is dw-b (receive only). "RF heard by DV-Router" is a frame the tester sends into dw-b: dw-b
transmits, dw-a hears it and gives it to the edge. DV-Router's own transmission leaves dw-a; dw-b hears it, gives it to the tester (the
observation) and to the edge on `rf-b` (its own frame: an own identity, ignored). Snippet `rig/config/dv-router/dp15-rf-dw.toml`: the
same ports `rf-a` / `rf-b`, the same path `rf-in`, the same `<DP15_TX_ENABLE>` token and the same split as tier A (`dp15-rf-out.toml`
unchanged), with `host = "<NET>.48"` / `"<NET>.49"`, `port = 8001`. `run.sh --profile web,dw` adds the snippet when it is not listed. Tier A skips itself
when the rendered config points at the TNCs (and the other way round).

| Shape | Env / snippets | Cases that run |
|-------|----------------|----------------|
| RF-OFF (receive only) | `DVT_DP15_TX_ENABLE=false`, `dp15-rf-dw.toml` | 6l, 6a, 6b `[tx_enable=false]` |
| RF-ON | `DVT_DP15_TX_ENABLE=true`, `dp15-rf-dw.toml` AND `dp15-rf-out.toml` | 6l, 6a, 6c |

Run commands (two runs; one rig at a time; the image `dvt-direwolf:local` is built by `run.sh` from `.cache/direwolf`, once `scripts/pre-fetch-direwolf.sh` ran):

```
XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=false \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf-dw.toml \
scripts/run.sh -m rig --capture --profile web,dw -- cases/test_dp15_rf_dw.py

XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true DVT_DP15_TX_ENABLE=true \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-rf-dw.toml:rig/config/dv-router/dp15-rf-out.toml \
scripts/run.sh -m rig --capture --profile web,dw -- cases/test_dp15_rf_dw.py
```

Config check (host side, throw-away container, `--network none`; `valid` for both shapes, done with `dv-router:wip-0348b98`): `DVT_DP15_TX_ENABLE=false python3 -m
dvtest.tools.config_check --image dv-router:wip-0348b98 --template <template> --gen compat1 --extra rig/config/dv-router/data-ident.toml --extra rig/config/dv-router/dp15-rf-dw.toml`,
and with `true` plus `--extra rig/config/dv-router/dp15-rf-out.toml`. The loop without the rig: `python3 -m dvtest.oracle_direwolf loop`.

Wall time estimate (UNVERIFIED), after the 1 to 2 minutes of rig start: RF-OFF about 4 minutes (the fixture's probe frames until the edge counts one, 6l about 20 s,
6a about 45 s with the 12 s baseline, 6b about 40 s); RF-ON about 4 minutes (6c waits 31 s after the last frame on the air). Notes: `out/<run>/dp15-rf-dw.txt`; the TNC logs `out/<run>/logs/dw-a.log`, `dw-b.log`.

| Case | Steps | Assert | Status |
|------|-------|--------|--------|
| DP15-6l (control) | the tester sends a frame addressed to nobody into dw-b, 8 s later one into dw-a | it comes out of the other TNC's KISS port, same bytes, channel 0, within 20 s, both ways; the latencies are recorded | written, not run on a rig |
| DP15-6a | the tester sends `N0TEST-7>APDVT1,WIDE1-1::XLX999B  :DVT nnnn{3` into dw-b (as tier A's 6a, but through the air) | the frame comes out of dw-a unchanged; one M17 SMS within 1.0 s and one D-STAR text over within idle gap + 2.1 s of the moment it left dw-a; one SSE `text` event, one history row; nothing comes out of dw-b for the rest of the case (no RF ack: dw-a would have to transmit); `dv_pkt_items_total{kind=text,result=accepted}` and `dv_edge_modem_total{port=rf-a,result=rx}` rise | written, not run on a rig |
| DP15-6b off | RF-OFF shape: a web user posts a text to module B | the room gets it; 12 s with nothing demodulated by either TNC; `dv_edge_modem_total{result=tx}` unchanged | written, not run on a rig |
| DP15-6c | RF-ON shape: a web text | ONE frame comes out of dw-b: UI frame from `N0TEST-1` to `APZDVR`, no digipeaters, `:XLX999B  :<text>`, no `{id`, text <= 67 characters and containing the body; nothing comes out of dw-a (rf-b never transmits); `dv_edge_modem_total{port=rf-a,result=tx}` rises; ONE history row (the edge's own frame heard on rf-b is not delivered again) | written, not run on a rig |

Assumptions that could not be verified without the rig (all in addition to those of tier A):

* The edge counts a frame heard through the loop as `dv_edge_modem_total{port=rf-a,result=rx}` even when no path takes it (the fixture sends probe frames to
  the addressee `N0PRB` and waits for the counter, up to 120 s; when it never rises the cases go on and say so in the notes; 6a asserts the counter itself).
* Dire Wolf accepts the edge's five KISS parameter frames as it accepted ours (VERIFIED for hand built frames, `direwolf-cases.md`: each is logged and none is copied), and TXDELAY 300 ms
  from the edge makes a 0.7 to 1.0 s transmission (no assertion on the length).
* The edge does not mind that its own frame comes back on `rf-b` (an own identity, data.md 9.1; the cases assert one history row) and does not use received frames for any
  PTT or channel-busy logic (KISS has no carrier-detect signal).
* A web text becomes a frame at dw-b within `TX_WAIT_S + LOOP_WAIT_S` (45 s); the fence of the PTT snapshot is the suspect when it does not (see the tier A assumptions).
* 6l transmits two probe frames from the tester through the TNCs; they are addressed to nobody and from callsigns of their own, so the room's text bucket is untouched.
* The compose services start in seconds; the fixture waits up to 120 s for the KISS ports and the loop.

### APRS-IS against a private aprsc (group 5): `cases/test_dp15_aprsis.py`

The server side is the rig service `aprsc` (profile `aprs`, 172.30.0.47, aprsc 3.0.0 pinned in `.env.example`, standalone, no internet; build, config, logins and
the passcode scheme: [rig.md](rig.md), "Private APRS-IS server"). DV-Router's APRS-IS edge logs in as `N0TEST-5`
(`rig/config/dv-router/dp15-aprsis.toml`, which also carries the path row `aprsis-down` `edge:aprsis -> room:B`, addressee `XLX999B`), the tester as `N0TEST-7`
with `dvtest/aprsis_client.py` (login with the computed passcode, TNC2 message lines). Both cases skip with the reason when the aprsc service is not up
(`DVT_APRS` not `on`, or no answer on `<net>.47:14580`) and `pytest.fail` when the edge never reports `dv_edge_aprsis_verified` = 1.

One rig run, profile `web,aprs` (the cases read the web API):

```
scripts/pre-fetch-aprsc.sh            # once, the only internet use
XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-aprsis.toml \
scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp15_aprsis.py
```

* Snippet order as for the streams run: the web snippets first, then `data-ident.toml` (the order no longer matters since commit 4b35fc3), then
  `dp15-aprsis.toml`; `run.sh --profile aprs` appends it when it is not listed. `dp15-paths.toml` is not needed.
* `run.sh` writes the fixture passcode of `N0TEST-5` (a function of the callsign, not a secret) to `out/<run>/aprs-secrets/passcode` (mode 600), mounts it read-only
  at `/etc/dv-router/aprs-secrets` and starts the SUT as the host user. `.cache/aprsc` must exist (`scripts/pre-fetch-aprsc.sh`); the image is built by
  `run.sh` at the start of the run (or by hand, [rig.md](rig.md)).
* Config check of the snippets (host side, throw-away container, `--network none`):
  `python3 -m dvtest.tools.config_check --image dv-router:wip-0348b98 --template <template> --gen compat1 --extra rig/config/dv-router/data-ident.toml --extra rig/config/dv-router/dp15-aprsis.toml` prints `valid` (done, also in the web-first order above).
* Estimated wall time (UNVERIFIED): the two cases about 4 minutes (5a: 12 s quiet baseline, listeners, the text over after the idle gap, 6 s of quiet, up to 25 s for the
  worker counters; 5b: the same plus the 40 s wait for the repeat); the whole run 8 to 12 minutes with the rig start, the web enrolment and, the first time, the aprsc image build.
* Notes: stdout and `out/<run>/dp15-aprsis.txt`; the server log is `out/<run>/logs/aprsc.log`. With `--capture` the TCP stream between `xlxd-sut` and `aprsc` (port 14580) is in
  `capture/xlxd-sut.pcapng`: the only place where the line DV-Router itself WROTE (before the server added its q construct) can be read.

| Case | 15.5 wording (`data.md:2448`) | Steps | Assert | Record |
|------|-------------------------------|-------|--------|--------|
| DP15-5a | "a message to XLX999B from a client on a private aprsc reaches the room (D-STAR text over, M17 SMS, web) and is acked once" | wait for `dv_edge_aprsis_verified` = 1; D-STAR listener (N0CALL) and M17 listener (N0TEST) on module B, public SSE open; the tester logs in on aprsc (verified) and sends `N0TEST-7>APDVT1,TCPIP*::XLX999B  :DVT <nonce>{51`; collect 20 s plus 6 s of quiet | ONE D-STAR text over `N0TEST...: <body>` with header UR `CQCQCQ`, suffix `APRS`; ONE M17 SMS carrying the body; ONE `text` row in the room history; ONE ack line `XLX999B>APZDVR,TCPIP*,q..,N0TEST-5::N0TEST-7 :ack51` within 3 s, whose path before the server's q construct is exactly `TCPIP*` and which has exactly one q construct (DV-Router wrote none: the tester's own line also has none); `dv_pkt_ack_total{result=ack}` +1 and `dv_pkt_items_total{kind=text,result=accepted}` +1 | latencies, the q token (`qAS` in the smoke test of the image; the DV-Router lead expects the server's choice, `qAS` or `qAO`; not asserted), the public SSE text event (a missing one is a FINDING, not a failure), counter deltas |
| DP15-5b | "a repeated message is acked again and not delivered twice" | as 5a with its own body and id 52; the identical line is sent again 40 s after the first (after aprsc's 30 s duplicate window, inside the controller's 60 s window); the listeners keep pumping | two ack lines in all, byte for byte equal, the second within 3 s of the repeat and more than 30 s after the first; still ONE text over, ONE SMS, ONE web row; `dv_pkt_ack_total{result=ack}` +1 and `{result=repeated}` +1; `dv_pkt_items_total{kind=text,result=accepted}` +1 | whether `dv_pkt_items_total{text,duplicate}` or `dv_room_data_total{duplicate}` moved by 1 (a FINDING if neither) |

Why 40 s: the controller dedupes by (type, guard callsign including SSID, body) for 60 s; the ack for a duplicate is byte for byte the same as the first, and aprsc drops an identical
packet inside its own 30 s window, so a repeat sooner than 30 s is acked by DV-Router (`repeated`) but the second ack never reaches the client. A repeat after 60 s would be delivered again
(a documented consequence). Not tested: the 2 s repeat of the plan (it only proves the server's dupecheck), the unverified login, `qAC` on an uplink and NOGATE (DP15-5c to 5e of the plan; they need
class 0 positions on an uplink path).

Honest limits: the line DV-Router writes is not visible to the case (aprsc adds the q construct on receipt and forwards the result), so "no q construct on the sent line" is inferred from the forwarded ack
(path before the q is exactly `TCPIP*`, one q construct); the pcapng above shows the raw line. The check of `dv_edge_aprsis_verified` is skipped (a fixed 10 s wait) on an image without that family.
Nothing here has run on a rig; what was run is the aprsc image (login verified with the computed passcode, a message forwarded with `qAC`, the ack with `qAS`, an identical ack inside 30 s dropped, a wrong passcode
`unverified`) in a throw-away container with `--network none`, and the unit tests (`cases/unit/test_aprs_passcode.py`, `test_aprsis_client.py` against a fake server, `test_dp15_aprsis_config.py`).

### Not written, and why

| Case | Reason |
|------|--------|
| DP15-6d, 6e, 6s on tier B | not written: 6d checks frame times against the budget (the audio adds nothing), 6e needs the same audio in two Dire Wolf instances (no hardware-free way short of a tee; the dedupe is DV-Router's, tier A covers it), 6s reads what the fake TNC records (Dire Wolf only logs the parameters). Also not written: a low `tx_budget` run for 6d, an RF ack with `alias_calls` (the room name `XLX999B` cannot be an AX.25 source; answer 8) |
| DP15-7b, 7c (receiving side), 7d | need a second DV-Router node as XLX peer (U8; the lead confirms a plain `[[edges.xlx.peer]]` row works and the text over needs no transcoder, answer 11) |
| 7a parts | `xlx_text_overs = false` (live config edit), preemption of a text over by peer voice, and "the sending node's own XLX listener does not get its own text over back" are not written |

### dp15-paths.toml

Only the room row `dp15-b-to-c` (room B to room C, class 0 text, 10 per minute) is in it. The launcher refuses a
`[[data.path]]` whose endpoint is not enabled, also with `enable = false`: `data.path[0]: APRS-IS endpoint is not
enabled` (config check, `dv-router:wip-0348b98`). So the gateway rows of the plan move into the snippets that enable their
edge: `rf-in` into `dp15-rf.toml` and `rf-out` into `dp15-rf-out.toml` (RF tier A, above), `aprsis-down` into `dp15-aprsis.toml` (APRS-IS, group 5 below). No case depends on the room path; 2b records whether
room C received the forwarded copy.

## Byte layouts and how they were established

The expected bytes of every builder are worked out by hand in `cases/unit/test_dp15_builders.py` and compared with
an independent decoder written from the same layout. Sources were READ (facts only, no code or text copied). Pinned
checkouts are under `.cache/` (not committed).

| Builder | Layout | Status | Sources (file:line) |
|---------|--------|--------|---------------------|
| YSF GPS record in the DCH of FN 6 / FN 7 (`ysf_gps.gps_record`, `gps_stream_packets`) | 20 byte record: magic `22 62` / `47 64` at bytes 1..2, radio type byte 4, Mic-E-like position bytes 5..13, end marker 0x03 followed by the 8 bit sum of bytes 0..marker; FN 6 = bytes 0..9, FN 7 = bytes 10..19, FT 7, DT 2 | **VERIFIED** against two sources | DV-Router `crates/lib/pkt/src/ysf.rs:25-123` (checks 35-71, position 74-116, radio names 125-140); YSFClients `GPS.cpp` in `.cache/ysf-research/src/YSFClients/YSFGateway/` lines 30-31, 99-117, 142-246, 249-282; edge side `crates/edge/ysf/src/protocol.rs:641-680` (FI 1, DT 2, FN 6/7, gateway and source must match the header). Hand vectors reproduce the bytes of the DV-Router fixture (30 N 120 E, FT-1D) |
| DMRG (`dmr.dmrg_packet`) | `DMRG` + id (3) + 7 bytes: byte 0 bit 0 = longitude bit 24, bits 1..3 error estimate (7 = unknown); bytes 1..3 longitude low 24 bits, 360/2^25 per step; bytes 4..6 latitude 24 bit, 180/2^24 per step | **VERIFIED** | MMDVMHost `DMRNetwork.cpp:259-276`, `DMRSlot.cpp:2059-2090` (`.cache/dstarsd-mmdvm/`); DV-Router `crates/lib/pkt/src/dmr.rs:26-76`, edge `crates/edge/dmrhb/src/protocol.rs:607-632` (a DMRG binds to the open stream of the same source id on slot 2) |
| DMRA (`dmr.dmra_packets`) | `DMRA` + id (3) + block (1) + 7 bytes of a 28 byte buffer; buffer byte 0: format (2 bits), character count (5 bits); 7 bit packed characters start at bit 0 of byte 0, ISO 8 / UTF-8 one byte per character from byte 1, UTF-16 low byte pairs | **VERIFIED** (7 bit, ISO 8 and UTF-16 hand vectors; UTF-8 shares the ISO 8 layout) | MMDVMHost `DMRNetwork.cpp:278-296`, `DMRTA.cpp:34-130`; DV-Router `dmr.rs:77-140` |
| Legacy M17 META text (`m17.meta_text_blocks`, `meta_text_stream`) | META byte 0: high nibble = bit map of all blocks (0x1 / 0x3 / 0x7 / 0xF), low nibble = one-hot bit of this block; 13 UTF-8 bytes, space padded, up to 4 blocks; TYPE bits 5..6 = 0 (plain voice TYPE 0x0005) | **VERIFIED** (note: the DP15 plan said "high nibble = number of blocks"; the code and the spec say bit map) | M17 spec `.cache/m17src/M17_spec/M17_spec.tex:1306-1316`; DV-Router `crates/lib/radio/src/m17.rs:199-211`, `crates/lib/pkt/src/m17_meta.rs:105-188`; LSF publication on META change at most once a second `crates/lib/edge/src/core/raw_core.rs:386-403` |
| D-STAR TX message and D-PRS overs (`dstar_sd.tx_message_fields`, `text_gps_fields`) | existing spec-derived generator (`dvtest/dstar_sd.py`, `docs/dstar-slowdata-cases.md`); the new functions only lay the existing blocks into whole overs and are checked with `DvrUnitDecoder` | **VERIFIED** offline (decoder model of `dstar.rs`); the rig behaviour is not | `cases/unit/test_dstar_sd.py` |
| M17P APRS (specifier 0x02, 2d) | existing `m17.m17p_packet`; payload is the TNC2 text without NUL | VERIFIED against the fixture family `crates/lib/pkt/testdata/m17-specifiers/` | `data.md:694`, `build_vectors.py` there |

Nothing is marked UNVERIFIED in the layouts; what is unverified is the BEHAVIOUR on a rig (everything in this file).

## Unverified until a rig run

* Every timing (the constants above are ceilings from the lead's answer, not measurements).
* The exact event and row shapes of the public SSE `data` event, `public/state` `rooms[].onair` and `public/lastheard`
  (taken from `docs/api/openapi-web.yaml` and the answer; the helpers read them defensively).
* Whether `dv_edge_render_total{kind="dstar-text20",result="framed"}` and `dv_pkt_reject_total{reason=spoof|unbound}` are
  spelled as in data.md 13.1 on this image (2c, 1b, 4b assert them).
* That the D-STAR header with a valid CRC and `RPT1 = <call> + module` is accepted for talkers other than N0CALL/N0TEST
  (only those two callsigns are used).
* Open question Q12 (is "a changed status at most once per 30 s" per stream, per callsign or per room): the cases avoid it
  by waiting (`MIN_GAP_S`), they do not test it.
