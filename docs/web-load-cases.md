<!-- SPDX-License-Identifier: MIT -->
# Web unit, stage 5: W8 load (200 WebRTC listeners x 50 pps through the HTTP relay)

`cases/test_web_load.py`, `cases/webload_support.py`, `dvtest/webload.py` (pure parts, unit tests `cases/unit/test_webload.py`),
`dvtest/tools/webload_gen.py` (the load generator, unit tests `cases/unit/test_webload_gen.py`), the rig service `webload`
(profile `webload`, `rig/Dockerfile.webload`, `docs/rig.md` "Web load profile") and the read-only control action `proc-stat`
(`scripts/lib/procstat.sh`, `cases/unit/test_procstat.py`). Calibrated against the image **`dv-router:wip-3b0724a`** (native launch).
DV-Router documents read: `docs/project/testing.md` ("Gates", "Browser/NAT rig criteria"), `docs/design/web.md` (signalling handoff,
"Resource bounds (MS9)", WebRTC limits, load test C90), `crates/edge/webrtc/examples/load/README.md`, `scripts/webrtc-listen-load.py`.

**Status in one paragraph.** The generator works through the real path (login, `POST /api/v1/rtc/offer`, ICE, DTLS-SRTP, `POST
/api/v1/rtc/alive`) and all 200 listeners connect (answer in 0.06 s at most). The FULL-LOAD gate (200 listeners x 50 pps, both rooms
sourced, run `out/20261102T031612Z`, `dv-router:wip-3b0724a`) is measured: every item passes (edge CPU 8.16 % of one core, peak RSS
27.0 MiB, growth 33 KiB per listener, worst-listener jitter p99 1.35 ms / max 10.85 ms, no duplicate, reordered or invalid packet,
source lateness 0.57 ms, no missed deadline) EXCEPT **lost packets: 216,000 (limit 0)**. Those are the 72 listeners with index 128-199
(room B, logins 16-24): they connected and answered like all the others, renewed their leases (204), and received nothing; the edge
gauge `dv_edge_webrtc_listeners` stays at 128. A hard cap of 128 WebRTC listeners per edge (or per transport peer) is the pattern
(finding F9), not the load generator: the receiving set is exactly the first 128 connections. An earlier half-load run
(`out/20261102T025906Z`, floor denial of the second source, finding F3) is kept below as an extra data point. Rerun command at the end.

## Method

Topology (rig network 172.30.0.0/24, internal):

```
 tester .30 --registers 9 accounts (operator, invites) --> web-proxy .90 (TLS, trusted proxy of dv-svc-web) --> xlxd-sut .11 :8081
 tester .30 --USRP dynamic client per room, marker frames at 50 pps on an absolute schedule--> xlxd-sut .11 :34001
 tester .30 --HTTP/JSON control, proc-stat (host-side read of /proc of the SUT container) --> webload .97 :3100
 webload .97 --login, offer, alive, close (HTTPS)--> web-proxy .90 --> dv-svc-web --> hub --> dv-edge-webrtc
 webload .97 <--ICE-lite host candidate, DTLS-SRTP media, UDP 50000-- xlxd-sut .11 (dv-edge-webrtc)
```

**Client stack: (b), aiortc in a new rig service.** Chosen because (c) DV-Router's harness is Rust code of their repo (not to be built
here) and speaks no HTTP, (a) a browser per listener does not fit 200 peers in 3 GiB, and the edge's media (ICE, DTLS-SRTP, PCMU RTP)
can only be received by a real WebRTC stack. aiortc 1.14.0 and its resolved dependency set are pinned in `.env.example`
(`WEBLOAD_PIP_PINS`, wheels only, `pip check` at build); the tester image stays stdlib only. Everything above the socket is repo code:
`dvtest.web.WebClient` does the login, offer and alive exactly as the page does. What aiortc does not do here: its jitter buffer,
depacketiser and decoder are bypassed (they would add a thread hop per packet and hide arrival times). Three small hooks in
`webload_gen.install_patches()`: (1) `aioice.ice.StunProtocol.datagram_received` takes `perf_counter_ns` the moment asyncio hands the
datagram to the protocol, before DTLS, SRTP and parsing; (2) `pylibsrtp.Session.unprotect` passes that stamp on and counts
authentication failures; (3) `RTCDtlsTransport._handle_rtp_data` gives each authenticated RTP packet to the listener instead of the
receiver. ICE, the DTLS handshake, SRTP authentication and RTCP are aiortc's own.

**Source and markers.** One USRP dynamic client per room (`cases/webload_support.py` `Source`) sends 320-byte PCM frames every 20 ms
on an absolute schedule (frame i due at start + i x 20 ms; late frames are never followed by a catch-up burst; the lateness of every
frame is recorded). Frame i is a 700 Hz tone whose first four samples are exactly representable mu-law levels spelling i in base 127,
so a listener knows which source frame each received packet is (`dvtest/webload.py`; the standard G.711 table of the edge keeps the
levels). Loss, duplicates, reordering and invalid payloads are counted from the markers per listener; the SRTP authentication failures
of libsrtp (including replays) are added to "invalid". Frames 0-249 are the warmup (5 s), 250-3249 the measured window (60 s), 3250-3299
a tail the listeners may still receive.

**Statistics.** Per listener, abs(spacing of successive unique in-window packets - 20 ms), nearest-rank p50 / p99 / max; the gate takes
the WORST listener. Edge CPU = (utime + stime) of the `dv-edge-webrtc` process read from `/proc` of the SUT container by a throwaway
helper (`proc-stat`) at the two window edges, divided by the helper's own clock difference (percent of ONE core; actual windows
59.8-60.2 s). Peak RSS = VmHWM at the end of the window. The sample is requested early by the measured request latency (0.57-0.67 s).

**Set-up budgets respected.** 9 accounts (8 circuits per login, 3 sessions per account => 25 logins; `N0LD01..N0LD09`, registered at the
register budget by the tester); the generator has its own address (the web service counts per client address); offers are paced under
the per-address join gate (1/s, burst 5: 4 at once, then one every 1.05 s); two rooms of 100. Accounts, not guests: guests are limited
to 2 per address and 50 globally (`guest_listen`), so they cannot carry 200 listeners, and logged-in listeners are the main real use.

## Adopted gate (DV-Router, 2026-11-01) and the reference

200 listeners x 50 pps, 60 s window after 5 s warmup: edge CPU <= 25 percent of one core; peak RSS <= 64 MiB; growth <= 100 KiB per
listener + 4 MiB over the pre-client startup; worst-listener jitter p99 <= 20 ms and max <= 60 ms; 0 lost / duplicate / reordered /
invalid; source missed deadlines 0. C90 (edge-only, 8-core job quota): 16.2 percent of one core, 33.6 MiB, p99 4.6 ms, max 12.5 ms,
0 lost over 600,000 packets.

## Results (run `out/20261102T025906Z`, `dv-router:wip-3b0724a`, no capture)

SUT container caps as shipped: 1g, 2 cpus, 1024 pids (nothing raised; nothing was hit: the SUT container did not restart, the edge pid
stayed the same, the edge had 8 threads at start and 10 at 200 listeners). Host: 32 logical cpus, load average 2-3.5 (other sessions
share it); `dvt-rig.scope` 800 percent CPU. Rig cpus: SUT 2, tester 2, webload 4 (used 22.6 percent of one core at 200 listeners).

| Case | Listeners connected | Receiving | Edge CPU (one core) | Peak RSS MiB (start of case -> end) | Worst p99 / max ms | Lost | Dup / reorder / invalid | Source late max / missed | Generator cpu (one core) |
|------|---------------------|-----------|---------------------|---------------------------------------|--------------------|------|--------------------------|--------------------------|--------------------------|
| calibration 1 | 1 | 1 (3000 pkts) | 0.83 % | 14.46 -> 17.26 | 0.88 / 8.13 | 0 | 0 / 0 / 0 | 1.0 ms / 0 | 1.24 % |
| calibration 10 | 10 | 5 (room A) | 1.30 % | 17.26 -> 18.05 | 0.95 / 7.80 | 0 on A (all of B) | 0 / 0 / 0 | 0.6 ms / 0 | 2.07 % |
| calibration 50 | 50 | 25 (room A) | 2.86 % | 18.05 -> 20.86 | 1.12 / 11.70 | 0 on A (all of B) | 0 / 0 / 0 | 1.0 ms / 0 | 6.63 % |
| **W8 200** | **200 of 200** | **100 (room A), 5000 pps** | **7.44 %** | 20.86 -> 27.29 | **1.42 / 12.64** | 0 on A (all 300,000 of B) | 0 / 0 / 0 | 0.8 ms / 0 | 22.59 % |

Per gate item at 200 listeners (HALF LOAD: 100 receiving, 100 connected idle; NOT the gate measurement):

| Gate item | Measured | Limit | Note |
|-----------|----------|-------|------|
| edge CPU | 7.44 % of one core (4.45 s in 59.85 s) | 25 % | half the packets: 1 receiving listener (50 pps) 0.83 %, 25 receiving (1250 pps) 2.86 %, 100 receiving (5000 pps) 7.44 %, i.e. about 1.4 % per 1000 pps; a linear estimate for 10,000 pps is about 14 %, which is an estimate and not a measurement |
| peak RSS | 27.29 MiB (VmHWM at window end) | 64 MiB | |
| RSS growth | 12.8 MiB over the true startup (14.46 MiB, first case) = 65 KiB per listener; the case's own pre-sample (20.86 MiB, VmHWM kept from the earlier cases) gives 6.4 MiB | 100 KiB per listener + 4 MiB = 24,096 KiB | VmHWM never falls, so the pre-client value of later cases includes earlier cases |
| worst-listener jitter (100 listeners of A) | p99 1.42 ms, max 12.64 ms | 20 / 60 ms | |
| lost / duplicate / reordered / invalid | 0 / 0 / 0 / 0 on the 100 receiving listeners (300,000 packets); 300,000 lost on the idle room B (source denied) | 0 | srtp authentication failures 0; the tone after the marker equals the reference G.711 encoder |
| source missed deadlines | 0 of 6600 frames, max lateness 0.81 ms | 0 | two rooms, two sender threads |
| signalling | 200 offers, 200 connected, connect-all 208.1 s (join gate 1/s per address), offer to connected 0.047 s p50 / 0.069 s max, 4572 `/rtc/alive` all 204 | | |

Versus C90 (full load): not comparable yet; at half load CPU is 7.4 % (C90: 16.2 % at 10,000 pps), RSS 27.3 MiB (C90: 33.6 MiB), jitter p99 1.4 ms / max 12.6 ms (C90: 4.6 / 12.5 ms). The HTTP path
added no visible cost to the edge (signalling is done by the web service, which shares the SUT's 2-cpu cap but not the edge process).

**The generator's own cost and what limits the measurement.** 4 worker processes (50 listeners each), one control server. At 200
listeners the whole container used 22.6 percent of one core (about 5.5 percent per worker, 4.5 cpus of headroom against a 4-cpu cap), 300 MiB
peak (cap 3g), no cpu throttling. The event loop lag of every worker (a 5 ms timer sampled over the window): p99 1.04 ms, max 2.5 ms (8.6 and
10.7 ms in the 50-listener case, one burst). The arrival time includes this scheduling and the socket queue, like the Rust harness
("application UDP receive"), but not kernel or pcap timing. A single listener (calibration 1, 1.2 percent cpu, lag max 1.1 ms) gives the
floor: p99 0.9 ms, max 8.1 ms with 3000 packets; the 8 ms maximum at one listener is therefore scheduling of the container, not the
SUT. The tester (source, sampler) used 7.7 percent of a core. The generator is not the bottleneck at 200 listeners; at full load it
doubles its packet work (about 45 percent of one core estimated) and still has headroom.

### Confirmation of the F9 fix (run `out/20261102T044726Z`, `dv-router:wip-81dcc76`, EDG2, no capture)

All 5 cases pass (717 s). W8 at 200 listeners x 50 pps x 60 s: 600000 of 600000 received, 0 lost, 0 duplicate, 0 reordered,
0 invalid, `dv_edge_webrtc_listeners` 200, both rooms served, `dv_edge_webrtc_join_refused_total{reason="core"}` 0 (the case now
reads it and fails on any refusal). Edge: 10.4 percent cpu (limit 25), peak RSS 32.1 MiB (limit 64), worst-listener jitter
p99 11.0 ms (limit 20) and max 12.1 ms (limit 60), 0 source deadlines missed. The 128-listener cap of F9 is gone.

## Offer retries and the stall detector (WEBSTALL1; run `out/20261111T123937Z`, wip-7bd0082)

w8_200 connected 198 of 200: two offers were refused 503 "WebRTC capacity reached". The web-proxy log shows every request of dv-svc-web (logins, `/rtc/alive`, offers) held for about
5.6 s (12:49:12.3 to 12:49:18.86, longest offer 5570 ms); the generator sends its offers on a schedule without waiting for the replies, so the queued ones were released together
and five joins in 59 ms filled the edge's join window (`joins_per_second` 5, `edge/webrtc/src/transport.rs`), the sixth and, 0.75 s later, the seventh were refused. No circuit of
an earlier phase was left (calibration leases end 30 s after the last `/rtc/alive`; the previous phase had ended more than 3 minutes before). The stall itself is a product finding
(dv-router unit WEBSTALL1). The run before it (`out/20261111T122626Z`, wip-a31709a) had no request slower than 1 s except the deliberate 2 s `auth/register`.

Harness changes:
* The generator (`dvtest/tools/webload_gen.py`) sends an offer again when it is refused 503 "WebRTC capacity reached": at most 3 retries, after max(Retry-After, 1.1 s). The case counts
  them (`offer_retries` in `load.json`: offers, retried, retries, exhausted, share) and FAILS when more than 5 % of the offers needed a retry or one ran out of retries.
* Every web request slower than 1 s (the generator's own network time, its client side pacing excluded) is an event; the case reports FINDING WEBSTALL1 with the count and the longest
  (`slow_requests` in `load.json`, each with the dv-svc-web cpu while it ran). It does not fail the case.
* `web_proc` in `load.json`: dv-svc-web cpu (one core = 100) and thread count about once a second through the control channel (`proc-stat xlxd-sut dv-svc-web`) while the listeners
  connect; a stall with low cpu is a wait (a lock, a hub query), one with high cpu is work on the wrong pool (a password hash). The sampler stops before the measured window starts, so
  the gate numbers are not shared with it.
* `io_pressure` in `load.json`: `/proc/pressure/io` of the host once a second for the whole run; every slow request carries the host I/O stall in its window and the nearest login in
  `sut-state/web/audit.jsonl` (dv-router: a login's audit fsync runs on the service's only worker). After a run: `python3 -m dvtest.stallscan out/<run>`. The case that provokes the
  stall is prepared in `docs/webstall1-cases.md`.
* Not recorded: the fabric round trip of an rtc offer. The hub metrics hold counters only (`dv_web_http_requests_total`, `dv_web_hub_unavailable_total`, `dv_web_rate_limited_total`,
  no latency); `answer_s` in the listener totals is the offer round trip seen by the client (offer sent to answer received).

### The fix image (WEBSTALL1, dv-router 17b05933; written offline, NOT run)

Details, run commands and GAPs: `docs/web-refusals-cases.md`. In this file's cases:
* The generator's offer retry rule is status 503 + problem code `unavailable` + a positive Retry-After (`dvtest/refusals.py is_capacity_refusal`), never the detail text: the fix image names
  the limit in it ("WebRTC join window capacity reached", ...), the earlier images say "WebRTC capacity reached". A body without a code still falls back to the old text rule, so both
  shapes are retried. The detail text of each retried refusal is counted in the generator status (`refused_details`) and ends up in `offer_retries.refused_details` of `load.json`: recorded, not judged.
* `refusals` in `load.json` (every load case, any image): `dv_edge_webrtc_offer_refused_total{reason}` and `dv_web_rtc_refused_total{reason}` read before the run and after the gate (two equal
  reads 12 s apart, so the hub's cache has caught up), by reason, with `delta`, `moved`, the shape problems of the exposition, and what the generator saw (`generator_saw`: retries, exhausted, details).
  One printed line per case: `refusals edge(...): none; web(...): none`. The families are never added together. An image without them records `null` (no failure).
* `DVT_WEBSTALL1FIX=on`: the case also FAILS when a family or a reason is missing, and when a web request other than `/auth/register` took longer than 1 s (the slow-request detector is a
  failure, not only FINDING WEBSTALL1). Off: unchanged (finding only), so the cases pass on the current image `wip-a31709a`.
* `test_join_burst_counts_join_window`: four offers at once from the generator with the edge's join window set to 1 per second (`rig/config/dv-router/join-window.toml`), see the new file.
  Run it alone: `DVT_WEBSTALL1FIX=on DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/join-window.toml ... scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py::test_join_burst_counts_join_window`
  (3 to 4 minutes). The full file runs without the snippet (about 17 minutes with the settled reads); the burst case then skips with the reason.

## Flood (short, bounded; MS9 limits)

From the tester's address while a probe listener (generator address) kept hearing a 36 s over, in the same run: **passed**.

| Probe | Observed | Documented |
|-------|----------|------------|
| 150 public reads (`GET /api/v1/public/state`) in 0.4 s | 21 x 200, 129 x 429 with `Retry-After: 1` | 5/s burst 20 per address |
| 16 logins, 16 different unknown callsigns | 10 x 401, then 6 x 429 `rate_limited`, `Retry-After: 1` | 1/s burst 10 per address |
| 6 concurrent registrations with a bad invite | 2 x 401, 4 x 429 | 0.2/s burst 2 |
| 40 simultaneous connections of one untrusted address to :8081 | 14 x 200, 18 x 429 (public bucket), **8 x 503** | cap 32 per transport peer: 40 - 32 = 8 |
| 40 simultaneous connections through the trusted proxy | 1 x 200, 39 x 429, **no 503** | trusted proxies exempt |
| after the flood | `/ready` 200 and public state 200 six seconds later, edge pid unchanged, probe listener 1800 of 1800 packets, jitter max 4.3 ms, 0 loss, source missed 0 | no crash |

The 429 answers are RFC 7807 `rate_limited`; the tester's own address stays limited for a while (`/health` is behind the same public
bucket), while the generator (another address) was not affected: the buckets are per address.

## FINDINGS (observed vs documented)

| # | Observed | Documented / expected |
|---|----------|-----------------------|
| F1 | The relay's envelope check refuses an offer with several fingerprint lines (`400 invalid SHA-256 DTLS fingerprint`). aiortc offers sha-256, sha-384 and sha-512 (RFC 8122 allows several); a browser sends one. The generator drops the extra lines (`relay_sdp`); the certificate is the same | web.md: "SHA-256 DTLS fingerprint"; the document does not say that other algorithms in the same SDP are refused |
| F2 | **Per-session unsafe-request budget (2/s, burst 10) vs eight circuits per login.** `POST /rtc/offer` and `POST /rtc/alive` of one login share the session's budget of unsafe requests; the answer is `429 rate_limited` "authentication rate limit" (a text that reads like a login limit). With the page's renewal schedule (at once, then every second until six answers, then every 10 s) eight circuits of one login need 48 requests in 5 s; the earlier runs connected 7 of 10, 27 of 50 and 105 of 200 listeners before the generator renewed once per 10 s through a per-login bucket (then 200 of 200 connected, 4572 alive all 204) | web.md: "One HTTP login may own at most eight circuit records" and the page schedule; the 2/s budget per session (which `dvtest.web.Pacer` already carried as `action`) is not mentioned with the RTC routes |
| F3 | **A second USRP talker in another room is denied while the first talks**: `Deny(OriginHoldsFloor)` counted by `dv-edge-usrp` (1 per over of room B), every frame dropped (3300). Two dynamic clients of one rig, different ports, same callsign `N0TEST`, rooms A and B. floor-control.md: "one floor per client" / "origin already holds a floor". Whether the client identity is the callsign or the edge is not documented; the source now uses one callsign per room (not run) | floor-control.md DENY reason 6; `docs/design/floor-control.md` does not say what identifies a USRP client for this rule |
| F4 | `dv_edge_webrtc_listeners` read **128** while 200 listeners were connected, renewed their leases (4572 x 204) and 100 of them received audio; 10 and 50 listeners read exactly 10 and 50. The gauge stayed at 128 for 36 s | web.md: the edge publishes its listener gauge; the `rtc_peers` snapshot at the hub may be bounded; not documented. Unresolved: which of the two numbers is right for room B (idle listeners could not be told apart because the source was denied) |
| F5 | The edge publishes the gauge slowly (up to about 10 s plus hub caching: 1 -> 11 -> 11 -> 10 seen after the previous case closed) | web.md, K22 note (up to 35 s) |
| F6 | Per-address join gate 1/s burst 5 makes connect-all take 208 s for 200 listeners from one address (C90's harness paced at 5 joins/s: 41.8 s). Five addresses would take 41 s | web.md: join gate 1/s burst 5 per address, global 5/s |
| F7 | Edge pre-client startup VmHWM is 14.1-14.5 MiB; a first listener adds 2.8 MiB (VmHWM 17.3 MiB) | no document; the 4 MiB fixed allowance covers it |
| F8 | `/health` and `/ready` of the web service are behind the per-address public bucket (5/s, burst 20), also through the trusted proxy (client address from `X-Forwarded-For`) | web.md "probe contract" documents `/ready` at 5/s burst 20; `/health` is not named |

## Not covered, and why

| What | Why |
|------|-----|
| The 200 x 50 pps gate itself | room B had no source (F3); rerun with the fixed source (one callsign per room) |
| Browsers at scale, talk, TURN, NAT | stages 3b and 4 cover a handful of real browsers; 200 browser peers do not fit; talk and TURN are other cases |
| Kernel or pcap timing of the media | the arrival time is where the client reads the datagram, as in DV-Router's harness; `--capture` was not used (disk) |
| W8 floods against the whole public API and the full MS9 matrix | only the short bounded flood above; the 30 minute flood of testing.md is a separate follow-up |
| 5 joins/s connect-all | a single generator address is held to 1/s by the join gate (F6) |
| Several source protocols for the second room | the USRP source is the only one used; if the denial of F3 is by edge, a second protocol client (not built) is needed |

## Rerun

```sh
DVT_USRP_DYNAMIC=1 XLXD_SUT_IMAGE=dv-router:wip-3b0724a DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \| F9 | **The edge serves only 128 of 200 connected listeners.** The 72 listeners with index 128-199 (room B, logins 16-24) get a normal answer (<= 0.06 s), renew their leases (204) and receive 0 of 3000 packets; `dv_edge_webrtc_listeners` reads 128 for the whole run (12 samples); all 128 listeners that receive have 0 loss, 0 duplicates, 0 reordering (p99 1.35 ms, max 10.85 ms). 216,000 packets lost = 72 x 3000 | the W8 gate and C90 (edge-only harness: 200 listeners, 0 loss); no document names a limit of 128 (web.md WebRTC limits: ask DV-Router whether it is a per-edge cap, a per-transport cap or a bug) |\n
  DVT_SUT_CONFIG=<template path> scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py
```

About 13 minutes (nine registrations, calibration 1 / 10 / 50, the 208 s connect-all of the 200 case and its 70 s window, the flood). Select
parts with `-k calibration`, `-k w8` or `-k flood`. Result: `out/<run>/web/load.json` (one file, rewritten after each case: aggregates, the
gate table, the generator and edge numbers, one compact row per listener). Check the disk before every run; the case writes nothing else.
The image `dvt-webload:local` is built from `rig/Dockerfile.webload` (pip on the daemon's network, the only internet use).

## RFWD1 false-kill guard (`dv_launcher_child_stall_kills_total`)

DV-Router RFWD1 (image `wip-8891cc7` and later): the launcher SIGKILLs an RF child whose heartbeat stalls for 2 s and counts it in
`dv_launcher_child_stall_kills_total{child}` (hub metrics). this file (W8, calibration, flood) drives load and stalls no RF child on purpose, so DV-Router asks that the counter
stays 0 for every child: any other value is a false kill and is reported.

* `cases/stall_kills_support.py` (pure logic, unit tests `cases/unit/test_stall_kills_support.py`): an autouse module fixture reads the counter through the
  control channel (`metrics xlxd-sut`) before the first test of the module, and the last test of the module, `test_zz_rfwd1_no_stall_kills`, reads it
  again after one hub publication (12 s; the hub publishes every 10 s). Both reads are outside every measured window and add no load; the timing
  behaviour and the gates of the cases are unchanged.
* Result file `out/<run>/web/stall-kills.json` (merged per module): per child `start`, `end`, `delta`, `series_present`, `series_at_start`,
  `readable_at_end`, `verdict` (`ok`, `false_kill`, `absent`, `unreadable`) and the message.
* Judged: the test FAILS (a failed test in the pytest summary, not a teardown error) when any child's end value or delta is above 0; the message names
  every such child with start, end and delta, and a `web.finding()` line (`out/<run>/web-findings.txt`) repeats it. It FAILS too when the end read does not
  answer (a false kill cannot be excluded). An image whose hub has no series (no RFWD1) is recorded as `absent` and the test SKIPS with the reason; without
  the control channel it skips as well. A start read that failed but an all-zero end is a pass with a `[RECORD]` line (a counter starts at 0).
* Select it alone: `-k test_zz_rfwd1_no_stall_kills` (the start read still happens in the fixture).
