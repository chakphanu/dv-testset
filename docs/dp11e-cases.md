<!-- SPDX-License-Identifier: MIT -->
# DP11e cases: the KISS TCP server of the AX.25 edge

Status: written offline against DV-Router `cfa765a` (DP11e merged) for the next image; **nothing here has run on a rig**. Sources: `data.md` 10.10 and 13.4, `config.md`
`[[edges.ax25.kiss_server]]`, and the DV-Router review 235 of the change (`review-235-dp11e.md`, read only, cited by name). Research on the apps and the earlier plan:
[dp11e-plan.md](dp11e-plan.md) (APRSdroid: strong evidence for KISS over TCP; PinPoint: Windows desktop, documents only).
Files: `cases/test_dp11e_kiss.py` (rig), `cases/test_dp11e_config.py` (host side, `cfgload` marker), helpers `dvtest/dp11e.py`, the app client `dvtest/kiss_app.py`,
snippets `rig/config/dv-router/dp11e-kiss.toml`, `dp11e-kiss-b.toml`, `dp11e-kiss-budget.toml`, `dp11e-kiss-igate.toml`, `dp11e-kiss-room.toml`, unit tests `cases/unit/test_dp11e_support.py`, `test_kiss_app.py`.

**Skip rule.** Every rig case skips with the reason when the rendered config has no `[[edges.ax25.kiss_server]]`, when the served configuration schema
(`GET /api/v1/admin/schema`) has no `kiss_server` (an image without DP11e), when a listener of its shape is missing, or when a prerequisite service is missing.

## Drift of the earlier plan against the merged design (fixed in dp11e-plan.md)

| Plan said | Merged text |
|-----------|-------------|
| every validated AX.25 frame is fanned out | only an AX.25 UI frame with PID 0xF0 (data 10.10); a non-UI frame is not |
| text between frames is "ignored" | bytes BEFORE the first FEND are discarded uncounted; text BETWEEN frames is one non-data record counted `command` or `bad_frame`; neither closes the client |
| a listener may map only non-zero channels | channel 0 MUST be mapped (it is the default port); an unmapped channel is `channel` |
| keepalive an open question | `keepalive_secs` 0-300, default 60, 0 disables; a lone FEND, not counted `sent` |
| `disabled` not mentioned | `TxCore::enqueue_client` derives the class TTL and refuses with the existing label `disabled` when the port has no scheduler budget |
| info 1-256 bytes | confirmed: 0 bytes or more than 256 are `too_long` |

## Shapes, snippets, runs

| Shape | Snippets (INSTEAD of `dp15-rf.toml`) | Listeners | Cases |
|-------|--------------------------------------|-----------|-------|
| K | `dp11e-kiss.toml`, with `dp11e-kiss-igate.toml` and `dp15-aprsis.toml` for DP11e-g | `apps` :8201 (rf-a = ch 0, rf-b rx only = ch 1, tx, keepalive 10 s, max_clients 3), `rxonly` :8202 (tx false, keepalive 0), `txdeny` :8203 (tx_from outside the tester), `outside` :8204 (allow_from outside the tester) | a, b, c, d, e, g, h, i, j, n, y, z |
| B | `dp11e-kiss-b.toml` (rf-b `tx = "simulate"`) | `loop` 127.0.0.1:8205, `rate` :8206 (tx_per_min 2, burst 1), `sim` :8207 (rf-a ch 0, rf-b simulated ch 1), `txdefault` :8208 (tx true, allow_from the tester, NO tx_from) | k, l, m, q, y, z |
| budget | `dp11e-kiss-budget.toml` INSTEAD of `dp11e-kiss-b.toml`: B with the class-default `tx_budget = {}` on both ports (not duty_pct 20 / per_source_pct 100), the port packet bucket at its maximum (30 / 30), and the listener `budget` :8209 (tx true, tx_per_min 30, tx_burst 10) in place of `txdefault` | `loop`, `rate`, `sim`, `budget` | p (alone: `-k dp11e_p`), k, l, m, y, z |
| room control | `dp11e-kiss-room.toml` ADDED to shape K (the path `dp11e-room`: text from `edge:ax25/rf-a` to `room:B`, addressee `XLX999B`) with `data-ident.toml` | as K | g2 (with a, b, c, d, e, g, h, i, j, n, y, z as in K) |

```
R=rig/config/dv-router
E=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml:$R/dp15-aprsis.toml
# K (about 20 minutes, ESTIMATE: d alone is 130 s, b about 3 minutes of counter settling, i about 2 minutes)
XLXD_SUT_IMAGE=<image with DP11e> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss.toml:$R/dp11e-kiss-igate.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp11e_kiss.py
# K with the room control (DP11e-g2; the same run as K plus the path of dp11e-kiss-room.toml: about 22 minutes, g2 adds about 2 minutes: 20 s for the client frame to settle, up to 60 s for the heard control)
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss.toml:$R/dp11e-kiss-igate.toml:$R/dp11e-kiss-room.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp11e_kiss.py
# B (about 7 minutes with DP11e-q, which adds about 1 minute)
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss-b.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp11e_kiss.py
# budget: the client TX airtime budget, DP11e-p ALONE (about 8 minutes, ESTIMATE: about 3 minutes of start and guard, the case waits for the position class TTL of 300 s and stops at the first dropped_* after 30 s)
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss-budget.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp11e_kiss.py -k dp11e_p
# configuration refusals (host side, about 1 minute: 17 throw-away containers one after the other; no rig)
DVT_CFGLOAD_IMAGE=<image with DP11e> DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_dp11e_config.py
```

The tester reaches the listeners on the SUT's rig address (`<OWN_IP>`, .11); the fake modems rf-a / rf-b are the tester's :8101 / :8102 as in tier A. The `slow` mark is only
a label (DP11e-i): `-m rig` runs it. Notes: stdout and `out/<run>/dp11e-kiss.txt`; the configuration rows go to `$DVT_OUT/config-load/dp11e-config.json`.

## Case table (rules one case or parameter each)

| Case | Rule (source) | Steps | Assert | Record |
|------|---------------|-------|--------|--------|
| DP11e-a | ASCII outside frames is ignored (data 10.10) | an APRSdroid init string (`ATE0`, `KISS ON`, CRLF lines) then a frame; later CRLF between two frames | both frames reach rf-a AS SENT, the client stays open; `queued` +2 and exactly ONE `command`/`bad_frame` (the CRLF between frames; the init text before the first FEND counts nothing) | all deltas |
| DP11e-b (11 params) | the client TX gate in order (data 10.10): `tx_off` (listener `rxonly`, port rf-b via channel 1), `tx_addr` (listener `txdeny`), `channel` (2 and 3, the PinPoint TX port), `bad_frame`, `not_ui` (control 0x2F, PID 0xCC), `src` (unlisted SSID, other base), `path` (H bit, TCPIP, QAR, hops 3 > 2), `too_long` (257 bytes, empty), `third_party`, `command` (TXDELAY, Return), `queued` | one client per listener sends the 20 frames of `dvtest/dp11e.refusal_frames()`, each with its own information field | per label and listener `dv_edge_kiss_frames_total{dir=in,result}` moves by exactly the number of frames that must fail with it; only the control reaches the modem and is `SRC>APDR16,WIDE1-1:...` byte for byte (no tocall or tx_path rewrite); no refusal closes a client | the deltas, the modem frames |
| DP11e-c1 | peers outside `allow_from` are closed before any byte (`refused_addr`) | the tester connects to `outside` | `wait_closed`, zero bytes, `refused_addr` +1, `accepted` unchanged | deltas |
| DP11e-c2 | `max_clients` (3): the next client is `full` | four clients on `apps` | the fourth is closed at once with zero bytes, `full` +1, the gauge `dv_edge_kiss_clients{port=apps}` is 3 | deltas, gauge |
| DP11e-d | lone-FEND keepalive on and off (config `keepalive_secs`) | 130 s idle: a client on `apps` (10 s) and one on `rxonly` (0), nothing at the modem | on: open, at least 10 lone FENDs about 10 s apart (+-30 %), no other byte, an app with a 120 s timeout would not drop; off: zero bytes, open, an app with a 120 s timeout WOULD drop | intervals, longest silence |
| DP11e-e | fan-out of UI/F0 frames to other clients, no echo to the sender (data 10.10, 13.4) | frames injected at rf-a (a normal one and a NOGATE one), a non-UI frame at rf-a, a frame at rf-b; two clients on `apps`, one on `rxonly`; then client 1 transmits | `apps` clients get the rf-a frames on channel 0 and the rf-b frame on channel 1, `rxonly` only the rf-a frames, nobody the non-UI frame (raw RF monitoring, unfiltered by position policy or path markers); the APRSdroid reader model takes channel 0 only; nobody, the sender included, gets the transmitted frame back (8 s); `dv_edge_kiss_frames_total{dir=out,result=sent}` +6 on `apps`, +2 on `rxonly` | counters |
| DP11e-g | client frames never reach APRS-IS or rooms (data 10.10, K4) | a client frame is transmitted; then a frame HEARD on rf-a as the control | raw / item / room counter totals do not move; the private aprsc sees nothing from the client's source; the control reaches the aprsc over `dp11e-kiss-igate.toml` (proof that the path is alive). Without the aprsc only the counter half runs | totals |
| DP11e-g2 (room control) | client frames never reach rooms (data 10.10) | with `dp11e-kiss-room.toml`: a client TX message addressed to `XLX999B` (it reaches the modem), then a message to `XLX999B` HEARD on rf-a | `dv_pkt_items_total` and `dv_room_data_total` (all labels) do not move after the client frame; after the heard frame `dv_pkt_items_total{kind=text,result=accepted}` and `dv_room_data_total{result=accepted}` grow (the proof that the path is alive). Skips without the path | totals |
| DP11e-h1 | sessions `kiss_client` in `admin/edge/<id>/sessions` (callsign = listener name, IP and port in the administrative scope; status.md) and kick (`kicked`, no timed ban) | `GET /api/v1/admin/clients` (operator or higher), then `POST /api/v1/admin/edges/ax25/clients/{circuit}/kick` (step-up) | the item exists with `callsign`, `ip`, `port` and `circuit` (a failure names the kinds, callsigns or keys seen), the kick is accepted, the client is closed, the same peer reconnects at once, `kicked` +1 | the item keys |
| DP11e-h2 | no peer address or frame content in the log (config.md) | scan the SUT log lines that mention kiss. The SUT logs are collected on the host after pytest ends: the case writes `out/<run>/logscan/DP11e-h.json` and SKIPS; the host then runs `python3 -m dvtest.logscan out/<run>` (exit 0 = clean) | no tester address, no information field of this module, no init text | line count |
| DP11e-i (slow) | output queue 64 frames / 64 KiB, the newest dropped (`slow`); a 30 s write stall closes (`stalled`) | one client with a 2 KiB receive buffer stops reading while about 4500 frames of 800 bytes are injected at 75 a second (a burst is dropped by the modem adapter's receive queue at about 160 frames and fits the kernel buffers: the first run never reached the 64 frame queue); a healthy client reads | `out/slow` >= 1, `stalled` >= 1, the stalled client is closed after it resumes, the healthy client got frames | deltas |
| DP11e-j | Restart(child) and RFSAFE1 (config.md; radio 10.4 rule 4; data 10.5) | `POST /api/v1/admin/children/dv-edge-ax25/restart`, then a client frame about 3 s after the call returned (past the fence proper of 0.1-1.1 s, inside the 13 s guard) | the client is closed, the listener rebinds within 30 s; the frame is never on air within 8 s of the restart; it is HELD and released at or after the guard end (>= 12 s after the call; about 14 s on this image) or counted `dv_edge_rf_tx_total{result=dropped_ttl}` +1 when its class TTL is shorter; `dv_edge_drops_total{reason=rf_fenced}` does not move; never lost without a count; the fence view may lag up to about 24.5 s (RFSAFE1b), the wait allows 30 s; a frame after `tx_fence` ended is served | restart timings, release delay, the guard line |
| DP11e-k (B) | the default bind is loopback; `listen` has no default (config.md) | connect to the loopback listener's port on the rig address | refused | |
| DP11e-l (B) | the listener bucket `rate` (tx_per_min 2, burst 1) | four frames at once on `rate` | `queued` + `rate` = 4, `rate` >= 2, `queued` 1 or 2, as many frames at the modem as queued | deltas |
| DP11e-m (B) | a simulated port: `simulated`, no modem byte, no PTT | one frame on channel 0 (rf-a) and one on channel 1 (rf-b, simulate) | rf-a transmits it; `dv_edge_rf_tx_total{port=rf-b,result=simulated}` +1 and rf-a `sent` +1; rf-b's modem got no data frame and no KISS byte | counters |
| DP11e-n | a frame to a receive-only exposed port is refused at the KISS layer (data 10.10 step 2) | a valid frame on channel 1 of `apps` (rf-b, receive only), the peer and listener allow TX | `dv_edge_kiss_frames_total{dir=in,result=tx_off}` +1 exactly, no `queued`, nothing at either modem (no data frame, no KISS byte at rf-b), the client stays open | deltas |
| DP11e-p (budget) | the client frames share the port airtime budget and the class shares (data 10.8, 10.10 step 7: "class TTL, class shares, per-source airtime, packet limits and physical airtime budget are unchanged") | eight position frames (`dp11e-bud0..7`) in one burst on `budget`; the listener bucket (burst 10) and the port bucket (30 / 30) admit all; wait for the class TTL (300 s) | kiss `queued` +8, `rate` 0; `dv_edge_rf_tx_total{port=rf-a}`: `delayed` grows, some `dropped_*` (normally `dropped_ttl`), `sent` equals the frames at the modem, and the modem got at least one and fewer than eight; `sent` + `dropped_*` + the change of `dv_edge_rf_queue` = 8 (not checked when the gauge has no series). Skips unless rf-a has `tx_budget = {}` | all deltas, the frames at the modem |
| DP11e-q (B) | `tx_from` independently permits TX; its empty list permits only loopback peers (data 10.10) | the tester (not loopback) connects to `txdefault` (tx true, allow_src, allow_from includes the tester, no tx_from), sends one valid frame, then a frame is injected at rf-a | `tx_addr` +1 exactly, no `queued`, nothing at the modem, the client stays open and RECEIVES the injected frame (RX is allowed, only TX is refused) | deltas |
| DP11e-y | the fixed counter label sets (data 13.1, status.md) | the hub metrics after the run | every `dv_edge_kiss_frames_total` has `dir` in/out and a result of its set (in: queued, tx_off, tx_addr, channel, bad_frame, not_ui, src, path, too_long, third_party, rate, command; out: sent, slow); every `dv_edge_kiss_clients_total` result is one of accepted, refused_addr, full, closed, stalled, kicked; `port` is a configured listener name, no other label | the series count |
| DP11e-z | `disabled` is defensive only (`TxCore::admit`, crates/edge/ax25/src/tx.rs 334-383: no valid configuration reaches it) | after every case of the run | `dv_edge_rf_tx_total{result=disabled}` is 0 | the non-zero series |
| config (17 params) | `listen` required; non-loopback needs `allow_from`; duplicate `listen`; four listeners at most; `keepalive_secs` <= 300; channel 0 mapped; unique channels; existing ports; `tx` needs `allow_src` and a mapped TX port; `allow_src` base = station base; name, `max_clients`, `max_hops`, `tx_burst` bounds; accept: the shipped snippet and a loopback listener without `allow_from` | `dv-router config check` on the snippet with one mutation each | non-zero exit, a positioned error that names `kiss_server` and the field (the channel-0 rule also says "channel 0"); the accept rows exit 0 | the exact error text per case |

## GAPs: not testable black-box here, covered by DV-Router's unit and seam tests (review 235)

Recorded in [coverage-matrix.md](coverage-matrix.md) as GAP rows. None of these is a missing case that could be written with this rig; each rests on the tests of the merge
(core tables, `shell_kiss_server_tests.rs` with a fake TNC and app sockets, the config fixtures).

| GAP | Why not black-box | Covered by |
|-----|-------------------|------------|
| A loopback peer is accepted (RX and TX with an empty `tx_from`) | the tester is in another network namespace than the SUT. DP11e-k proves the bind (refused on the rig address); `allow_from` and `tx_from` are tested with the tester's rig address inside and outside a CIDR; the refusal half of `tx_from` empty (a non-loopback peer: `tx_addr`) is DP11e-q | the shell tests (loopback peer, `tx_from` default) |
| A mapped TNC2 port is refused | the rig has no TNC2 port to name | config fixtures |
| IPv4-mapped IPv6 peers match the IPv4 rules (review 235 F6) | the rig has no dual-stack listener or IPv6 peer | `ipv4_mapped_peers_match_ipv4_rules` |
| The source quota key `kiss/<listener>` shared by all clients of a listener | only its effect is visible: DP11e-p exhausts the class budget with one client of one listener, but cannot tell the class share from the source quota (`dropped_quota` is not forced); two clients sharing the key are not told apart from one | the scheduler tests |
| Nagle off and TCP keepalive socket options | not visible from the peer | the shell tests |
| At most eight frames per client per turn; the 16 KiB/s read budget with TCP back-pressure | needs the SUT's scheduling turns | the core tests (a throughput flood would only show the budget indirectly) |
| The PTT holder token of the client TX path | a fabric key the control channel does not expose (as DP18-s) | `shell_kiss_server_tests.rs` |
| `disabled` (a TTL-less client frame) | `TxCore::admit` (tx.rs 334-383) refuses it only for a port without a scheduler budget, which no valid config has; DP11e-z only checks that it never appears | the tx tests |

Other limits:

* DP11e-j does not drive the fence proper (a client frame in the first 100 ms after the restart is REFUSED and counted `rf_fenced`, shell.rs 407-417): the window is racy through a restart.
* The `rf-a` port of the shapes K and B has `tx_budget = { duty_pct = 20, per_source_pct = 100 }` (only the shape budget keeps the class defaults, for DP11e-p): with the afsk1200-shared defaults (5 % per 600 s, position class 1/8, one source 25 %) the fifth position frame of the listener key `kiss/apps` is delayed, which the first run showed in DP11e-g. The run sends about ten frames through `apps`.
* The label-set check (DP11e-y) allows the `proc` label: the hub adds it to every edge series.
* The first connect of a case can get `full` while the previous case's client is still being freed (the server frees the slot when it reads EOF, tens of ms later): the cases retry a connect that is closed within 0.4 s, except where a refusal is the subject.

* DP11e-d needs the fake modems quiet for 130 s and no parallel case: a frame injected at rf-a during the idle period would reset the keepalive timer of the server.
* The exact texts of the configuration refusals are not a contract except where the documents quote them (channel 0); `cases/test_dp11e_config.py` asserts a positioned error that names `kiss_server` and the field.
* The shapes of the sessions list and the kick route are the documented ones (status.md: kind `kiss_client`; `POST /api/v1/admin/edges/ax25/clients/{circuit}/kick`); a deviation FAILS with the kinds, callsigns or keys seen.
* `kissutil` (Dire Wolf) as an oracle (design 9, slow tier) is still only a proposal in dp11e-plan.md: it needs a host-side one-off container on the rig network and is not written.
* DP11e-p reads the budget outcome per port, not per listener: `dv_edge_rf_tx_total` has no listener label. Run it alone: the earlier cases spend position budget on rf-a, which only makes the outcome clearer
  (fewer frames fit), but the run then takes the class TTL on top of the cases before it. `delayed` is read as "grew", not as a count (it may be counted at every dispatch attempt). The wait for
  `dropped_*` is bounded at 360 s from the burst; a drop only appears with the TTL (300 s) unless the queue (8 per port) overflows first (`dropped_queue`), which eight frames do not reach.
* DP11e-g2 proves the room half with `types = ["text"]`: a client message addressed to the room name is the frame most likely to leak into a room. Position and status items reach a room only through
  a port bound to a room (`room = "B"`, dp7-rf.toml), which the shape K ports are not; that route is not exercised here.

## Done when (DV-Router work unit DP11e)

"A KISS client receives RF frames and its TX passes the gates and the airtime budget, refused frames are counted; an unauthenticated or non-local client is refused per the design; fake-KISS rig case."

| Clause | Cases | Status |
|--------|-------|--------|
| a KISS client receives RF frames | DP11e-e (fan-out, channel mapping, no echo), DP11e-q (RX allowed while TX is refused), DP11e-d (keepalive) | K stage green on wip-8891cc7 (work-units.md DP11e row); q written, not run |
| its TX passes the gates | DP11e-b (one frame per refusal label and the control sent AS SENT), DP11e-a, DP11e-m (simulated port), DP11e-n | K and B stages green on wip-8891cc7 (work-units.md DP11e row) |
| its TX meets the airtime budget | DP11e-p (budget shape): queued, then `delayed`, `dropped_*`, fewer frames on air than sent; DP11e-l (listener bucket) | p written, not run; l: B stage green on wip-8891cc7 |
| refused frames are counted | DP11e-b, DP11e-y (fixed label sets), DP11e-z (`disabled` never) | K and B stages green on wip-8891cc7 |
| an unauthenticated or non-local client is refused per the design | DP11e-c1 (`allow_from`, `refused_addr`), DP11e-b `tx_addr` via `txdeny`, DP11e-q (empty `tx_from` = loopback only), DP11e-k (loopback bind), DP11e-c2 (`max_clients`), DP11e-config (17 params) | c1 / b / k / config: green on wip-8891cc7 (work-units.md DP11e row); q written, not run; the loopback peer itself is a GAP |
| client frames never reach APRS-IS or rooms | DP11e-g (APRS-IS: control over `dp11e-kiss-igate.toml`), DP11e-g2 (rooms: control over `dp11e-kiss-room.toml`) | g: K stage green on wip-8891cc7; g2 written, not run |
| sessions, kick, no peer address in the log | DP11e-h1, DP11e-h2 | K stage green on wip-8891cc7 |
