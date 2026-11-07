<!-- SPDX-License-Identifier: MIT -->
# DP15 cases: stream metadata (groups 1, 2, 4), the worker lifecycle (3), RF tier A (6) and the XLX peer wire (7a)

Status of every case in this file: **written, not run on a rig.** Nothing here was executed against a DV-Router
image: the cases were collected (`pytest --collect-only`), the byte builders have offline unit tests, and the two
snippets were checked with `dvtest.tools.config_check` against `dv-router:wip-0348b98`. Expect first-run
corrections. Specification and the other groups: [dp15-plan.md](dp15-plan.md). Source of the 15.5 wording:
DV-Router `docs/design/data.md` lines 2442-2450.

Case file `cases/test_dp15_streams.py`, shared helpers `cases/data_support.py` (moved out of
`cases/test_data_ident.py` without a change of behaviour), builders in `dvtest/proto/ysf_gps.py`,
`dvtest/proto/dmr.py` (DMRG, DMRA), `dvtest/proto/m17.py` (legacy META text), `dvtest/dstar_sd.py`
(`tx_message_fields`, `text_gps_fields`, `over_packets`, `aprs_position_payload`), web readers in `dvtest/web.py`
(`operator_events`, `room_messages`, `post_room_text`).

## Run

One rig run, profile `web` (the cases read the web API; the tester plays the XLX peer, no `interlink` profile):

```
XLXD_SUT_IMAGE=dv-router:wip-0348b98 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml:rig/config/dv-router/dp15-paths.toml:\
rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:\
rig/config/dv-router/xlx-peer-tester.toml \
scripts/run.sh -m rig --capture --profile web -- cases/test_dp15_streams.py
```

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
  The tool cannot check `web.toml` in the combination (it merges before the token substitution; same for `web.toml` alone).

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

### Not written, and why

| Case | Reason |
|------|--------|
| DP15-5a, 5b | need a private aprsc image and rig service (units U4, U5) |
| DP15-7b, 7c (receiving side), 7d | need a second DV-Router node as XLX peer (U8; the lead confirms a plain `[[edges.xlx.peer]]` row works and the text over needs no transcoder, answer 11) |
| 7a parts | `xlx_text_overs = false` (live config edit), preemption of a text over by peer voice, and "the sending node's own XLX listener does not get its own text over back" are not written |

### dp15-paths.toml

Only the room row `dp15-b-to-c` (room B to room C, class 0 text, 10 per minute) is in it. The launcher refuses a
`[[data.path]]` whose endpoint is not enabled, also with `enable = false`: `data.path[0]: APRS-IS endpoint is not
enabled` (config check, `dv-router:wip-0348b98`). So the gateway rows of the plan (`aprsis-down`, `rf-in`, `rf-out`)
move into the snippets that enable their edge (units U2, U5). No case depends on the room path; 2b records whether
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
