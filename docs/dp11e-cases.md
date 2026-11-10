<!-- SPDX-License-Identifier: MIT -->
# DP11e cases: the KISS TCP server of the AX.25 edge

Status: written offline against DV-Router `cfa765a` (DP11e merged) for the next image; **nothing here has run on a rig**. Sources: `data.md` 10.10 and 13.4, `config.md`
`[[edges.ax25.kiss_server]]`, and the DV-Router review 235 of the change (`review-235-dp11e.md`, read only, cited by name). Research on the apps and the earlier plan:
[dp11e-plan.md](dp11e-plan.md) (APRSdroid: strong evidence for KISS over TCP; PinPoint: Windows desktop, documents only).
Files: `cases/test_dp11e_kiss.py` (rig), `cases/test_dp11e_config.py` (host side, `cfgload` marker), helpers `dvtest/dp11e.py`, the app client `dvtest/kiss_app.py`,
snippets `rig/config/dv-router/dp11e-kiss.toml`, `dp11e-kiss-b.toml`, `dp11e-kiss-igate.toml`, unit tests `cases/unit/test_dp11e_support.py`, `test_kiss_app.py`.

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
| K | `dp11e-kiss.toml`, with `dp11e-kiss-igate.toml` and `dp15-aprsis.toml` for DP11e-g | `apps` :8201 (rf-a = ch 0, rf-b rx only = ch 1, tx, keepalive 10 s, max_clients 3), `rxonly` :8202 (tx false, keepalive 0), `txdeny` :8203 (tx_from outside the tester), `outside` :8204 (allow_from outside the tester) | a, b, c, d, e, g, h, i, j, z |
| B | `dp11e-kiss-b.toml` (rf-b `tx = "simulate"`) | `loop` 127.0.0.1:8205, `rate` :8206 (tx_per_min 2, burst 1), `sim` :8207 (rf-a ch 0, rf-b simulated ch 1) | k, l, m, z |

```
R=rig/config/dv-router
E=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml:$R/dp15-aprsis.toml
# K (about 20 minutes, ESTIMATE: d alone is 130 s, b about 3 minutes of counter settling, i about 2 minutes)
XLXD_SUT_IMAGE=<image with DP11e> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss.toml:$R/dp11e-kiss-igate.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp11e_kiss.py
# B (about 6 minutes)
... DVT_SUT_CONFIG_EXTRA=$E:$R/dp11e-kiss-b.toml scripts/run.sh -m rig --capture --profile web,aprs -- cases/test_dp11e_kiss.py
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
| DP11e-h1 | sessions `kiss_client` (callsign = listener name, IP, port) and kick (`kicked`, no timed ban) | `GET /api/v1/admin/clients`, then `POST .../edges/{kind}/clients/{circuit}/kick` | the item exists (skip with the keys seen when the shape differs: UNVERIFIED), the client is closed, the same peer reconnects at once, `kicked` +1 | the item keys |
| DP11e-h2 | no peer address or frame content in the log (config.md) | scan the SUT log lines that mention kiss | no tester address, no information field of this module, no init text | line count |
| DP11e-i (slow) | output queue 64 frames / 64 KiB, the newest dropped (`slow`); a 30 s write stall closes (`stalled`) | one client stops reading while 5000 frames of 900 bytes are injected; a healthy client reads | `out/slow` >= 1, `stalled` >= 1, the stalled client is closed after it resumes, the healthy client got frames | deltas |
| DP11e-j | Restart(child) and RFSAFE1 (config.md; radio 10.4) | `POST /api/v1/admin/children/dv-edge-ax25/restart` | the client is closed, the listener rebinds within 30 s, a frame sent at once is not transmitted inside the guard and is counted `dv_edge_drops_total{reason=rf_fenced}`, a frame after `tx_fence` ended is served | restart timings, the guard line |
| DP11e-k (B) | the default bind is loopback; `listen` has no default (config.md) | connect to the loopback listener's port on the rig address | refused | |
| DP11e-l (B) | the listener bucket `rate` (tx_per_min 2, burst 1) | four frames at once on `rate` | `queued` + `rate` = 4, `rate` >= 2, `queued` 1 or 2, as many frames at the modem as queued | deltas |
| DP11e-m (B) | a simulated port: `simulated`, no modem byte, no PTT | one frame on channel 0 (rf-a) and one on channel 1 (rf-b, simulate) | rf-a transmits it; `dv_edge_rf_tx_total{port=rf-b,result=simulated}` +1 and rf-a `sent` +1; rf-b's modem got no data frame and no KISS byte | counters |
| DP11e-z | TTL-less client frames are counted `disabled` (tx.rs `enqueue_client`) | after every case of the run | `dv_edge_rf_tx_total{result=disabled}` is 0 in a valid configuration | the non-zero series |
| config (17 params) | `listen` required; non-loopback needs `allow_from`; duplicate `listen`; four listeners at most; `keepalive_secs` <= 300; channel 0 mapped; unique channels; existing ports; `tx` needs `allow_src` and a mapped TX port; `allow_src` base = station base; name, `max_clients`, `max_hops`, `tx_burst` bounds; accept: the shipped snippet and a loopback listener without `allow_from` | `dv-router config check` on the snippet with one mutation each | non-zero exit, a positioned error that names `kiss_server` and the field (the channel-0 rule also says "channel 0"); the accept rows exit 0 | the exact error text per case |

## Limits (not testable black-box, or not testable here)

* **`disabled`**: a TTL-less client frame needs a port with no scheduler budget, which a valid configuration cannot have (TX true or simulate requires an explicit budget). DP11e-z only checks that the
  label never appears; the refusal itself is unit-level in DV-Router.
* **Loopback peers are accepted** (the positive half of the loopback rule) is not testable: the tester is in another network namespace. DP11e-k proves the bind (refused on the rig address);
  the acceptance of loopback peers and the `tx_from` default (loopback only) rest on DV-Router's own tests. `allow_from` and `tx_from` are tested with the tester's rig address inside and outside a CIDR.
* IPv4-mapped IPv6 peers (review 235 F6), the sans-IO core, the shared `admit`, the quota key `kiss/<listener>`, Nagle and TCP keepalive socket options, the per-turn cap of eight frames and the
  16 KiB/s read budget, the PTT holder token: not observable.
* Mapping a TNC2 port is refused by config; the rig has no TNC2 port to name, so that rule is not a case.
* The kick route, the item keys of `kiss_client` and the `{kind}` path part are read from the documents only (UNVERIFIED); DP11e-h1 skips with the keys it saw when they differ.
* DP11e-d depends on the idle period: the fake modems are quiet and no other case runs in parallel. A `rf-a` frame injected during the 130 s would reset the server's idle timer.
* `kissutil` (Dire Wolf) as an oracle (design 9, slow tier) is still only a proposal in dp11e-plan.md: it needs a host-side one-off container on the rig network and is not written.
