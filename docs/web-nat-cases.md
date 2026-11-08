<!-- SPDX-License-Identifier: MIT -->
# Web unit, stage 4: NAT behaviour of the browser WebRTC path

`cases/test_web_nat.py` (8 case functions, 48 runs: classification x3 NAT types, then each browser case on three engines
x three NAT types, the expiry case behind the symmetric NAT only, the 429 case and the talk-during-blackhole case behind the port restricted NAT) and the helpers
`cases/nat_support.py`, `dvtest/nat.py`, `dvtest/turn.py`. Built on the web, browser and WebRTC stages (`docs/rig.md` "Web
profile", "Browser profile", `docs/web-browser-cases.md`) and on the new compose profile `nat` (`docs/rig.md` "NAT profile").
Calibrated against the image **`dv-router:wip-5aa7cfd`** (native launch); the blackhole case was re-judged on **`dv-router:wip-3b0724a`** for ICE1a (section "ICE1a re-judgement"). DV-Router documents read: `docs/design/web.md` (WebRTC edge,
signalling handoff, ICE/TURN), `docs/design/deployment.md` ("Test rig", "Owner-run TURN"), `docs/project/testing.md` ("Browser/NAT rig
criteria"), `config.md` (`web.turn_*`), from a tree a few documentation commits ahead of the image (the TURN issuer and the page's
use of it, WTURN1, are in the image).

Engines: Playwright Chromium 156, Playwright Firefox 157, Playwright WebKit 27.2 (the WebKit of Playwright 1.64.0; **not Safari**, no iOS).

## Topology

```
   rig network 172.30.0.0/24 (internal)                                nat-inside 172.31.0.0/24 (internal)
   ------------------------------------------------------------        ---------------------------------
   xlxd-sut .11  UDP 50000 (ICE-lite edge, public_ips = .11)
   web-proxy .90 TCP 443   (https://dvt-web)
   coturn    .94 UDP 3478, relay ports 49160-49180
   nat-ref-a .95 / nat-ref-b .96  UDP 3479 3480 (3481 poke)
   tester    .30 --- ws :3000 and probe :3001 (DNAT) ---+
                 --- control :9100 ------------------+   |
                                                      v   v
                                           +-----------------------+            +--------------------+
                                           | nat-router            |   inside   | browser  .91       |
                                           |  outside .92 (+ .93)  |<---------->|  chromium, firefox,|
                                           |  inside  .2           |   .2       |  WebKit            |
                                           |  iptables NAT         |            | + nat-edge (route  |
                                           +-----------------------+            |   via .2, probe)   |
                                                                                +--------------------+
   direct media:  browser --NAT--> xlxd-sut:50000          (the browser's ICE checks and RTP)
   TURN media:    browser --NAT--> coturn:3478 ... coturn relay port --> xlxd-sut:50000
   TURN is browser-side only: the edge has no TURN client; it answers the STUN checks that reach it from the relay address.
```

The browser has no way out except the router (internal network, no gateway from Docker; the `nat-edge` sidecar adds the default
route). It resolves `dvt-web` by `/etc/hosts`. The tester reaches the Playwright server through the router's TCP forward
`<net>.92:3000` and the probe server through `:3001`; those are control traffic (TCP) and never touch the UDP media counters. The
tester talks to the router's control server on `<net>.92:9100` (a JSON line per TCP connection; the browser cannot reach it).

## How each NAT type is built and verified

All three are Linux iptables on `nat-router` (`dvtest/nat.py`, `mode_commands`), switched at run time without restarting anything:

| Type | Rules | RFC 4787 mapping / filtering |
|------|-------|------------------------------|
| full cone | `SNAT -s <browser> --to-source <net>.93` and `DNAT -d <net>.93 --to-destination <browser>`: a static 1:1 NAT (it accepts a datagram for every port, a stronger "full cone" than a dynamic one, which is the point: any outside host reaches the mapped address) | endpoint-independent / endpoint-independent |
| port restricted | `MASQUERADE` (conntrack; the source port is kept; a reply is accepted only from the address and port the browser sent to) | endpoint-independent / address-and-port-dependent |
| symmetric | `MASQUERADE --random` for UDP (a new random port per destination), plain `MASQUERADE` for TCP | address-and-port-dependent / address-and-port-dependent |

Self-test `test_nat_classification` (no browser): a socket inside sends a probe datagram to reflector A on two ports and to
reflector B (two outside addresses); each reflector answers with the source it saw. A second inside socket sends to reflector A only;
the tester (a third address) and reflector A from another port send an unsolicited datagram to the mapped address; the probe server in
the browser's network namespace reports which arrived. `dvtest.nat.classify` turns that into the RFC 4787 terms (unit-tested).

## TURN (coturn) in the rig

`coturn` is the official project image `coturn/coturn:4.18.0` (189 MB), pinned by tag and digest in `.env.example` (`COTURN_IMAGE`),
pulled once by hand, never by `run.sh`. Configuration (generated per run by `dvtest.turn`, mode 0600 because it holds the secret):
`use-auth-secret` with `static-auth-secret` = the SUT's `turn_secret_file`, realm `dvt-rig`, UDP 3478, relay ports 49160-49180, user
quota 4 / total 20, `no-tcp-relay`, `no-multicast-peers`, **no TLS** (the rig is offline and internal; `turns:` is not exercised). The
template of DV-Router's `deployment.md` denies the RFC 1918 ranges, and this rig's subnet 172.30.0.0/24 is inside 172.16.0.0/12:
the config keeps all six denied ranges and adds `allowed-peer-ip` for **the SUT's address only** (`<net>.11`, tighter than "the rig
subnet"); coturn's explicit allow overrides the denied range exactly as the document says, and no other rig address is reachable
through the relay. The SUT gets `turn.toml`: `turn_urls = ["turn:<net>.94:3478?transport=udp"]`, `turn_secret_file =
"/run/secrets/turn-secret"` (the host-made file, mode 0600, owned by the SUT's uid, mounted read-only), `turn_ttl = 120`. The secret is
never printed, logged or in any argument; after each run `dvtest.turn scan` looked through `out/<run>/` (captures included: the secret
never goes on the wire, only the HMAC credential) and found nothing.

## What the cases assert

| Case | Asserts (engines x NAT types) |
|------|-------------------------------|
| `test_nat_classification` (3) | the router's three types classify as built (table below) |
| `test_direct_listen` (9) | answer to `connected` and answer to first audio packet within 10 s; 20 s of arrival timing in the page with p99 <= 20 ms and max <= 60 ms; one `GET /rtc/ice` (200 with TURN, 404 without); the selected pair is not a relay; the tone (700 Hz) where the engine feeds Web Audio |
| `test_turn_relay` (9) | `drop_direct` on (the router drops UDP to `<net>.11:50000`): one `GET /rtc/ice` = 200 with `X-DV-RTC-Session`; the credential has the shape `<expiry>:<opaque>`, expiry = issue time + `turn_ttl`, credential = base64(HMAC-SHA1(secret, username)); the selected pair is `relay`; audio plays within 10 s of the answer; the credential and the secret are in no page text, HTML, web storage, cookie, console line or page error; datagrams were dropped toward the media port |
| `test_blackhole_resume_and_failure` (9, slow) | ICE1a (wip-3b0724a): UDP blackholes of 2, 3, 5 s end with the same peer, the same circuit (no new `POST /rtc/offer`) and audio; for 12, 16 and 20 s the status line must have read "Connection interrupted. Waiting to resume." (asserted) and the outcome is recorded (resumed, or lost and from which side); a 20 s break (clearly longer than the 12 s grace) must end with "Connection lost. Connect again manually."; after every loss the page sends no offer by itself and a manual connect sends exactly one new offer with a new circuit and audio flows |
| `test_talk_is_released_at_disconnected_and_needs_a_fresh_press` (3, slow) | port restricted NAT only: Talk held, blackhole on; at the first 'disconnected' ("Connection interrupted...") the talk status has left "On air"; 2 s later the path returns, the session resumes, the still-held button does not go back on air and the protocol listener hears nothing new in 3 s; a fresh press goes on air and is heard |
| `test_turn_credential_expiry` (3, slow) | relay session behind the symmetric NAT for 660 s (`turn_ttl` 120 s, TURN permissions 300 s, allocation 600 s): a 3 s over every 12 s; audio must work before the credential expires; what happens after is recorded |
| `test_ice_404_goes_direct` (9, run without `turn.toml`) | `GET /rtc/ice` = 404 once, no credential, direct connection, audio |
| `test_ice_429_goes_direct` (3) | the third connect of one login within a minute gets 429 (burst of two), the page still connects direct, the fourth is 429 too |

Not reachable by configuration: ice 503 (during ownership recovery the offer route answers 503 too, so the page never gets to the ICE
request). The talker is a USRP dynamic client; its over starts when the panel says "Listening" (the edge sends audio only for an over
that begins while the listener is attached), so "first audio" in the tables includes about 0.6 s of that start (the tester starts
the over, the router fans it out). Arrival timing is read in the page from `RTCRtpReceiver.getSynchronizationSources()` (RTP timestamp
and receive time of the newest packet, polled in a message-channel loop; `getStats` is served from a cache of about 50 ms in Chromium
and cannot give a packet cadence).

## Results

Run `out/20261101T172915Z` (TURN on; classification, direct, relay, blackhole, expiry), `out/20261101T182350Z` (no TURN: direct and the
404 case), `out/20261101T183126Z` (TURN on: classification and the 429 case, after that case got its accounts up front); image `dv-router:wip-5aa7cfd`; all measurements from the
tester's `out/<run>/web/nat-results.jsonl`, each also a `FINDING:` line in `out/<run>/web-findings.txt`. The direct, relay, expiry and 429 tables below
are the 5aa7cfd measurements; they were not redone on 3b0724a except that the same cases passed in the ICE1a run (TURN on, `out/20261102T004117Z`, 1 h 2 min, wip-3b0724a):
**36 passed, 2 failed, 1 setup error, 9 skipped (the 404 cases, TURN is on)**. The failures: the Firefox talk case (N10; the case now skips for that documented reason),
and two Playwright WebKit browser-connection losses at the 40 to 60 minute mark (`Page.goto: Provisional navigation canceled` in the setup of the WebKit expiry case, the
WebKit 429 case `TargetClosedError`), the known "shared run-server dies in long sessions" matter of `docs/results-adaptation.md`. The WebKit expiry and 429 cases and the talk case were rerun alone
(`out/20261102T014441Z`, TURN on: WebKit expiry 660 s with the relay pair and all rounds heard, `/rtc/alive` 71 posts, WebKit 429 `[200, 200, 429]`, 2 passed, the Firefox talk case skipped).

### Classification self-test (mapping as seen by the reflectors; unsolicited datagrams reaching the inside socket)

| Router type | Mapping to A:3479 / A:3480 / B:3479 | Third address reaches | Same address, other port reaches | Classified |
|-------------|-------------------------------------|-----------------------|----------------------------------|------------|
| full_cone | `<net>.93`:41000 for all three | yes | yes | endpoint-independent / endpoint-independent = full cone |
| port_restricted | `<net>.92`:41000 for all three | no | no | endpoint-independent / address-and-port-dependent = port restricted |
| symmetric | `<net>.92`: three different random ports | no | no | address-and-port-dependent / address-and-port-dependent = symmetric |

### Direct path (no TURN in the run: `out/20261101T182350Z`; with TURN configured the direct numbers were the same within noise)

Times are page-side (`performance.now()`): answer applied (`setRemoteDescription`) to `connectionState connected`, and to the first
RTP packet; deviation = |inter-arrival - 20 ms| over 20 s (about 1000 packets) per listener; the worst listener of the stage is in the
last row.

| Engine | NAT | answer to connected | answer to first audio | deviation p50 / p99 / max | selected pair (local / remote) | tone |
|--------|-----|--------------------|-----------------------|---------------------------|--------------------------------|------|
| Chromium | full cone | 76 ms | 0.72 s | 0 / 0 / 0 ms | prflx / host | 700 Hz |
| Chromium | port restricted | 54 ms | 0.71 s | 0 / 0 / 1 ms | prflx / host | 700 Hz |
| Chromium | symmetric | 61 ms | 0.71 s | 0 / 0 / 0 ms | prflx / host | 700 Hz |
| Firefox | full cone | 34 ms | 0.78 s | 3 / 9 / 10 ms | prflx / host | not judged |
| Firefox | port restricted | 24 ms | 0.78 s | 4 / 10 / 10 ms | prflx / host | not judged |
| Firefox | symmetric | 23 ms | 0.68 s | 4 / 10 / 12 ms | prflx / host | not judged |
| WebKit | full cone | 299 ms | 1.01 s | 0 / 10 / 39 ms | prflx / host | 700 Hz |
| WebKit | port restricted | 192 ms | 0.84 s | 0 / 11 / 49 ms | prflx / host | 700 Hz |
| WebKit | symmetric | 198 ms | 0.94 s | 0 / 10 / 11 ms | prflx / host | 700 Hz |

Worst listener: **p99 11 ms (Playwright WebKit, port restricted), max 49 ms (Playwright WebKit, port restricted)**, inside
the criteria (20 / 60 ms); every connect was far inside 10 s. Firefox and WebKit report the receive time rounded to 10 ms (their
timestamp resolution), so their p99 of 9 to 11 ms is quantization, not jitter; Firefox p50 of 3 to 4 ms is the same effect. The tone is
not judged in Firefox (no audio device in the container: its AudioContext stays suspended, see `web-browser-cases.md`). With TURN
configured the page also gathers a server-reflexive candidate from coturn, and the direct pair of the run `out/20261101T172915Z`
is `srflx` in six of nine runs and `prflx` in three instead of always `prflx`; the remote side is always the edge's host candidate.
The offer the page sends has **no candidates** in every direct and relay run (`a=candidate` count 0), as the brief says.

### Relay path (TURN on, `drop_direct` on; `out/20261101T172915Z`)

`GET /rtc/ice` was one request, 200, `ttl` 120, `urls` = `turn:<net>.94:3478?transport=udp`, no credential or secret anywhere in the
page, the username expiry was `turn_ttl` after issue and the credential matched the HMAC in all nine runs. The router dropped 24 (Chromium),
7 (Firefox) and 25 or 26 (WebKit) datagrams toward the media port, which shows the direct checks were tried and blocked.

| Engine | NAT | answer to connected | answer to first audio | deviation p50 / p99 / max | selected pair | tone |
|--------|-----|--------------------|-----------------------|---------------------------|---------------|------|
| Chromium | full cone | 189 ms | 0.89 s | 0 / 1 / 1 ms | relay (udp) / host | 700 Hz |
| Chromium | port restricted | 186 ms | 0.92 s | 0 / 0 / 0 ms | relay (udp) / host | 700 Hz |
| Chromium | symmetric | 186 ms | 0.89 s | 0 / 0 / 0 ms | relay (udp) / host | 700 Hz |
| Firefox | full cone | 44 ms | 0.71 s | 4 / 10 / 10 ms | relay (udp) / host | not judged |
| Firefox | port restricted | 45 ms | 0.70 s | 3 / 9 / 10 ms | relay (udp) / host | not judged |
| Firefox | symmetric | 44 ms | 0.69 s | 3 / 9 / 10 ms | relay (udp) / host | not judged |
| WebKit | full cone | 201 ms | 1.10 s | 0 / 10 / 11 ms | relay / host | 700 Hz |
| WebKit | port restricted | 304 ms | 1.03 s | 0 / 10 / 11 ms | relay / host | 700 Hz |
| WebKit | symmetric | 291 ms | 1.00 s | 0 / 10 / 11 ms | relay / host | 700 Hz |

The relay path meets the direct-path limits too (worst max 11 ms). Playwright WebKit does not report `relayProtocol` in `getStats`.

### UDP blackhole and the ICE failure interval, BEFORE ICE1a (image 5aa7cfd, `out/20261101T172915Z`; superseded by the next section)

"Lost" means the page gave up: status "Connection lost. Connect again manually.", peer closed, no new offer by itself. The interval
column is the page-clock time from the start of the blackhole to the browser's ICE state `disconnected` (the page closes its peer at
that event, so the engine's own `failed` state, 30 s of consent failures, is never reached and cannot be measured through the page; the
interval that matters to a listener is the `disconnected` one).

| Engine | NAT | 2 s | 3 s | 5 s | 8 s | ICE `disconnected` after |
|--------|-----|-----|-----|-----|-----|--------------------------|
| Chromium | full cone | resumed | resumed | resumed | lost | 6.27 s |
| Chromium | port restricted | resumed | resumed | resumed | lost | 6.27 s |
| Chromium | symmetric | resumed | resumed | resumed | lost | 6.33 s |
| Firefox | full cone | **lost** | - | - | - | 5.81 s (the blackhole had ended at 2 s) |
| Firefox | port restricted | resumed | resumed | **lost** | - | 8.29 s |
| Firefox | symmetric | resumed | resumed | **lost** | - | 7.91 s |
| WebKit | full cone | resumed | resumed | resumed | lost | 5.60 s |
| WebKit | port restricted | resumed | resumed | resumed | lost | 5.83 s |
| WebKit | symmetric | resumed | resumed | resumed | lost | 5.83 s |

Earlier calibration runs (same code, other alignment of the break and the engine's consent checks) gave Chromium 5.69 to 6.98 s,
WebKit 5.16 to 7.43 s and **Firefox 4.63, 5.09, 7.54 s** (a 2 s, a 3 s and a 5 s blackhole each ended the session): Firefox's detection
time varies by several seconds from run to run with where the break falls in its consent cycle, and it reports `disconnected` after the
blackhole is over. Measured failure interval of the stage: **Chromium 5.7 to 7.0 s, WebKit 5.2 to 7.5 s, Firefox 4.6 to 8.3 s; independent
of the NAT type** (the break is on the path, not in the mapping: after it the mapping is still there, nothing was lost on the inside).
Every loss was followed by the documented behaviour: "Connection lost. Connect again manually.", no automatic offer, a manual
connect sent exactly one new `POST /rtc/offer` with a new circuit and audio flowed again (all 9 runs).

### ICE1a re-judgement (image `dv-router:wip-3b0724a`, TURN on, `out/20261102T004117Z`; 2 s, 3 s, 5 s, 12 s, 16 s, 20 s per engine x NAT type)

DV-Router ICE1a (web.md "Listen", `web/site/public.js`): at the first ICE or peer `disconnected` the page shows "Connection interrupted.
Waiting to resume.", keeps the peer, releases Talk at once, and gives up 12 s later with "Connection lost. Connect again manually." unless
connectivity returns; peer or ICE `failed` closes at once; the edge drops an established peer 15 s after the last STUN request. The case now
breaks the UDP path for 2, 3, 5, 12, 16 and 20 s in one session per (engine, NAT type); after every loss it checks the text, that no offer
was sent by itself, and that a manual connect sends exactly one new offer with a new circuit and audio. Asserted: 2, 3, 5 s resume with no new
offer and audio; for 12, 16, 20 s the status line read the interrupted text; the 20 s break is lost with the lost text. Recorded: the 12 s
and 16 s outcomes and the side. All nine blackhole runs passed. `R` = resumed with the same peer and circuit, no new offer, audio after;
`i` = the interrupted text was seen; `L` = lost; page-clock seconds are from the start of the break.

| Engine | NAT | 2 s | 3 s | 5 s | 12 s | 16 s | 20 s |
|--------|-----|-----|-----|-----|------|------|------|
| Chromium | full cone | R | R | R | R i | L i, engine `failed` at 15.9 s | L i, engine `failed` at 15.1 s |
| Chromium | port restricted | R | R | R | R i | L i, `failed` 15.9 s | L i, `failed` 15.1 s |
| Chromium | symmetric | R | R | R | R i | L i, `failed` 15.8 s | L i, `failed` 15.1 s |
| Firefox | full cone | R | R | R i | **L** i, grace expiry at 17.5 s | L i, grace 18.6 s | L i, grace 16.9 s |
| Firefox | port restricted | R | R | R i | **L** i, grace 22.1 s | L i, grace 22.3 s | L i, grace 22.8 s |
| Firefox | symmetric | R | R i | R i | **L** i, grace 20.7 s | L i, grace 17.3 s | L i, grace 17.7 s |
| WebKit | full cone | R | R | R | R i | L i, ICE `failed` at 15.4 s | L i, `failed` at 17.2 s |
| WebKit | port restricted | R | R | R | R i | L i, `failed` 15.1 s | L i, `failed` 17.2 s |
| WebKit | symmetric | R | R | R | R i | L i, `failed` 15.1 s | L i, `failed` 17.4 s |

**Interrupted text and times.** "Connection interrupted. Waiting to resume." was on the status line in every 12, 16 and 20 s run (27 of 27). First ICE
`disconnected` after the start of the break: Chromium 5.1 to 6.0 s, Playwright WebKit 5.1 to 7.4 s, Firefox 4.6 to 10.8 s (it varies by run; in the
2, 3 and 5 s runs Firefox reported `disconnected` 5.6 to 9.1 s after the start, i.e. after the break was over, and came back within the
same second: it resumed, with the interrupted text for a moment in the 5 s runs).

**N1 (Firefox 2, 3, 5 s).** Resumed with no new offer and audio in 9 of 9 (3 NAT types x 3 windows). The `xfail` for N1 is **removed** from the case.

**How the 16 s and 20 s breaks ended, and from which side.** In Chromium and Playwright WebKit the engine itself declared the session `failed`
(Chromium `conn:failed`, WebKit `ice:failed`) 10.0 s after it reported `disconnected`, i.e. 15.1 to 16.0 s after the start of the
break for Chromium and 15.1 to 17.4 s for WebKit, so the page's 12 s grace (which would end 17 to 19 s after the start) is never reached in these two
engines; the page reacts to `failed` at once. The edge's own 15 s STUN timeout falls in the same second, and nothing observable from the page tells the two apart (the
listener gauge is published every 10 s). Firefox does not fail by itself in that time: its loss is the page's grace expiry, 12.0 s after its `disconnected` (the
`judge_loss` gap is 12.0 +- 0.3 s), 16.9 to 22.8 s after the start. The 20 s break is lost in 9 of 9, in 8 of 9 while the break was still on (the exception: Firefox port restricted, 22.8 s). The 16 s break is lost in 9 of 9, in 4 of 9 while the break was still on (Chromium symmetric and the three WebKit runs).

**12 s.** Chromium and Playwright WebKit resume in 6 of 6 runs (the engine had been disconnected for about 6 s; the edge still had the peer). Firefox is **lost** in 3 of 3 (page grace
expiry 5.5 to 10.1 s after the break had ended): its ICE did not recover after the path came back inside the grace window. An earlier TURN-off run
(`out/20261101T211249Z`, wip-3b0724a) had the Firefox full cone 12 s run not lost but without audio at the check; the outcome is not uniform.

**Talk during a blackhole** (`test_talk_is_released_at_disconnected_and_needs_a_fresh_press`, port restricted NAT, direct path; runs
`out/20261102T004117Z`, `out/20261102T014441Z`, `out/20261102T015751Z` for the timeline of Chromium and WebKit): Chromium and Playwright WebKit: with the button held, the talk
status left "On air" for "Ready" **3.4 s after the start of the break** (the 4 s PTT watchdog of TALK1: no renewal ack from the edge), 2.2 s *before* the engine reported
`disconnected` (5.6 to 5.7 s) and the interrupted text appeared; the session resumed 0.02 to 0.09 s after the path returned, the held button stayed
"Ready" and the protocol listener got 0 voice frames during the interruption and in the 3 s after recovery; a fresh press went "On air" in 0.01 s and 91 to 102
voice frames reached the listener in 2 s. So Talk is gone at 'disconnected' and a fresh press is needed, as ICE1a says, but in a UDP blackhole the **watchdog ends the over first**
(no engine reports `disconnected` before about 4.6 s), so the page's release at `disconnected` has nothing left to release and is not what the case sees. Firefox:
the same 3.4 s watchdog, then the 4 s release acknowledgement deadline ran out (about 7.4 s) before Firefox reported `disconnected` and the page closed with "Talk release timed out.
Connect again manually." (N10); the case is skipped for Firefox with that reason.

### TURN credential expiry (symmetric NAT, relay only, 660 s)

| Engine | Result |
|--------|--------|
| Chromium, Firefox, Playwright WebKit | the relay pair stayed `relay`, the peer `connected`, and every 3 s over (one per 12 s, 55 rounds) arrived with all 150 packets for the full 660 s: the credential expired at 120 s, the TURN permission lifetime (300 s) and the allocation lifetime (600 s) passed; the page's `/rtc/alive` kept the lease (66 to 67 posts). Nothing happened at expiry |

### ICE fallbacks

| Case | Result |
|------|--------|
| 404 (no `turn.toml`) | all 9 runs: `GET /rtc/ice` = 404 once, no ice servers, direct connection, audio |
| 429 | all 3 engines: statuses of three connects `[200, 200, 429]`, the third and a fourth connect still connect direct (pair `prflx` / `host`) with audio |
| 503 | not reachable: ownership recovery refuses the offer first |

## Findings (observed vs documented)

Each is a `FINDING:` line; the numbered ones are the ones that differ from or add to the documents. N1 (the adopted criterion, 2-5 s
resume) is asserted by the case and now holds on all three engines; none of the others is asserted.

| # | Observed | Documented / expected |
|---|----------|-----------------------|
| N1 | (5aa7cfd) Firefox ended the session on a 2-5 s blackhole in 6 of 6 runs. **ICE1a (3b0724a): closed.** Firefox resumed 2, 3, 5 s in 9 of 9 runs (3 NAT types x 3 windows) with no new offer and audio; the xfail is removed from the case | testing.md: 2-5 s must resume without a new offer |
| N2 | (5aa7cfd) the page closed its peer at the first ICE `disconnected` (5 to 8 s). **ICE1a: changed.** The page waits up to 12 s from `disconnected`; Chromium and WebKit fail by themselves 10.0 s after `disconnected` (15 to 17 s after the start of a break) so the grace is cut short by the engine there, Firefox waits the whole 12 s and loses the session by the page's grace | web.md ICE1a: 12 s grace, `failed` closes at once; consistent |
| N3 | the page's loss does not fire `connectionstatechange` (the close is synchronous in the ICE handler): the first and only state event of the page in a loss is `iceconnectionstatechange: disconnected` | not documented |
| N4 | the relay works through all three NAT types with a real coturn and the SUT's credentials; the selected pair is `relay`; the page makes one `GET /rtc/ice` per connect; username `<expiry>:<opaque>`, credential HMAC-SHA1 as documented; no secret or credential in the page | as documented (deployment.md "Owner-run TURN", web.md) |
| N5 | an established relay session was not cut by the credential's expiry (120 s) in 660 s on any engine | deployment.md: "existing credentials expire at the issued timestamp"; what coturn does to a running allocation is not stated. The TTL of one credential is not the lifetime of a relay session |
| N6 | coturn's `allowed-peer-ip=<SUT address>` overrides the denied 172.16.0.0/12 as documented, and the template needs two more notes for a rig: `no-cli` is rejected by coturn 4.18 ("deprecated") and `no-dtls` is not a valid option there; the image's executable needs `NET_BIND_SERVICE` in the bounding set (file capability) even for an unprivileged port | template in deployment.md |
| N7 | with TURN configured the browser also gathers a `srflx` candidate from coturn's STUN and the direct pair is `srflx` where the NAT keeps the mapping; without TURN the pair is `prflx` (the edge's STUN reply names the mapping) | not documented (no STUN server of the product) |
| N8 | the capture check lists the browsers' STUN checks to `<SUT>:50000` as `other.unclassified` warnings (34 datagrams in the run) | known: dvcheck knows no STUN |
| N9 | rig: the SUT's dynamic USRP hub keeps a closed client until its idle timeout; 36 new clients in 7 minutes (one per over) made later clients get no `REG:OK`, so the long cases reuse one client | rig matter, not a product claim |
| N10 | with Talk held in a blackhole the over ends at 3.4 s (watchdog, no renewal ack for 4 s) on all three engines, before any `disconnected` (4.6 s at the earliest); on Chromium and WebKit the interrupted state follows at 5.6 s and the session resumes; on **Firefox** the 4 s release acknowledgement deadline (about 7.4 s) ran out before Firefox reported `disconnected` and the page closed the peer: "Talk release timed out. Connect again manually." | web.md: no renewal ack in 4 s ends the over; no terminal reply to the release within 4 s closes the peer, the deadline being suspended only from `disconnected` on. Consistent, but ICE1a's grace does not protect a talking Firefox, and the 'release at disconnected' rule cannot be isolated in a blackhole (the watchdog is first) |
| N11 | ICE `failed` 10.0 s after `disconnected` in Chromium and Playwright WebKit, in every 16 s and 20 s run | not documented; the page's 12 s grace is longer than that, so these two engines never use it |
| N12 | the edge's 15 s STUN timeout is not visible from a page; the silent-close probe of `docs/web-k22-cases.md` (K1) did not see the edge drop a silently closed peer within 100 s | web.md ICE1a: 15 s without a STUN request |

Other FINDING lines (arrival statistics, pairs, connection times) are measurements, not differences.

## ICE1 address change

`cases/test_web_nat_ice1.py` (2 case functions, 12 runs, table below), `dvtest/ice1.py` (pure helpers), the router words
`readdress`, `mappings` and `firsts` (`dvtest/nat.py`, the sniffer in `dvtest/tools/nat_router.py`). Written against the DV-Router spec for ICE1 (image
**`dv-router:wip-97c669d`**, commit e439f62; ICE1b had not landed).

The spec: a signed-in browser is Listening on a module, then the CLIENT ADDRESS CHANGES (a new source address and port as the SUT sees them). The page
performs an ICE restart on the SAME circuit.

**First run on wip-97c669d** (reported by the coordinator; the run directory is not in this tree, so there are no per-case numbers here): the three
`test_ice1_readdress_moves_the_mapping_and_back` runs pass; the nine browser cases FAIL. The page sees nothing (no ICE restart: a change of the NAT mapping is
not visible from inside the browser), the audio stops at once and comes back after about 14 s in Chromium and Playwright WebKit, and not within 20 s in
Firefox. DV-Router classes this as an EDGE finding (ICE1c: the edge must follow a NAT rebinding on its own); the cases stay as standing cases and fail
until ICE1c lands. The page-side expectations "the page performs an ICE restart" (new `ice-ufrag`) and the budgets below are therefore the spec as it was
written, not what the edge does today; the `firsts` evidence below was added to show when the browser's first datagrams reach the SUT from the new
mapping and when the edge starts sending to it.

| Case | Runs | What it does |
|------|------|--------------|
| `test_ice1_readdress_moves_the_mapping_and_back` | 3 (NAT types) | no browser: classifies the NAT, moves the browser's UDP to the other outside address, classifies again (the reflectors must all see the new address, the type must be unchanged), moves back and classifies a third time (the original address, the same type). It proves the router words before the browser case relies on them |
| `test_ice1_address_change_restarts_ice_on_the_same_circuit` (slow) | 9 (engine x NAT type), signed in as the browser user `br_user` | the case below |

**How the address changes.** The router control word `{"op":"readdress","state":"on"}` moves the translation of the browser's UDP to the OTHER outside
address (the router has two: `<net>.92` and the alias `<net>.93`) and flushes the UDP conntrack entries so the browser's next datagram gets a new
mapping; `off` puts the NAT type's own rules back (`mode_commands`). The NAT type does not change; only the address does. TCP keeps its address on
purpose: the web service ties a circuit to the client address of its HTTP requests, so moving TCP too would test that rule, not the ICE restart.

| NAT type | Address before -> after | Rules while moved (UDP only) |
|----------|-------------------------|------------------------------|
| full cone | alias -> router address | `SNAT -s <browser> -p udp --to-source <net>.92`; UDP to `<net>.92` DNAT to the browser; a `RETURN` rule ahead of the old DNAT closes the alias address for UDP |
| port restricted | router address -> alias | `SNAT -p udp --to-source <net>.93` (source port kept), `MASQUERADE` for the rest |
| symmetric | router address -> alias | `SNAT -p udp --to-source <net>.93 --random`, `MASQUERADE` for the rest |
| address-restricted cone | - | not built in the router (see Gaps), so not run |

Control words (unit tested in `cases/unit/test_nat_readdress.py`): `readdress on|off` (idempotent; `off` with nothing moved is a no-op, so finalizers may call it
at any time; a new `nat_mode` and `reset` also end a move), `mappings` (read only: the UDP flows from the browser to the SUT's media port as conntrack
holds them, i.e. the source address and port the SUT sees), and `status` now has `readdressed`, `last_readdress` and `dropped.unmapped_media`. The router image
(`dvt-nat:local`) copies `dvtest/`, so `run.sh` rebuilds it with the next run; the one rig change is `CAP_NET_RAW` on `nat-router` for the `firsts` sniffer (below).

**What `test_ice1_address_change_restarts_ice_on_the_same_circuit` does and asserts.** Connect to module I (the page holds one `sess_id`), key one 55 s USRP
over from a protocol talker (DVT_USRP_DYNAMIC=1) and let it run, check 4 s of steady audio, then `readdress on` and watch the inbound packet counter of the page
(`RTC_PROBE_JS`, a sample every 0.1 s, `ice1.resume_analysis`) until the audio is back and 4 more seconds have passed.

| # | Asserted |
|---|----------|
| 1 same circuit | the set of `sess_id`s of `POST /rtc/offer` is still the one id; the page still has one `RTCPeerConnection`; the session cookie is unchanged and no `POST /auth/session` was sent; the operator client list shows no new WebRTC circuit. A repeated `POST /rtc/offer` that is answered with the same circuit is a FINDING, not a failure (the spec says no new offer session) |
| 1b restart | the `a=ice-ufrag` of the local or the remote description differs after the change (an ICE restart, RFC 8445); the passwords are never read |
| 2 audio | stalled, then resumed: voice frames reach the listener again within `RESUME_BUDGET_S` of the change: **Chromium 8 s, Firefox 8 s, Playwright WebKit 12 s** (DV-Router's ICE1c figures, 2026-11-08; named constants `RESUME_BUDGET_S` in `dvtest/ice1.py`). Measured from just before the control call. A counter that never stalls counts as resumed at 0 s; the audio must also still flow in the last 3 s (at least 70 % of 150 packets) and the talker's over must not have ended early |
| 3 old pair gone | the router's conntrack shows the SUT the browser at the old address before and ONLY at the new address after (`ice1.mapping_problems`); after the audio is back (+1 s) the router counts every datagram the SUT's media port still sends to the OLD address (no mapping left, so it reaches the router itself and is dropped and counted) for 3 s: it must be **0** (`STRAY_OLD_PAIR_MAX`) |
| 4 page | no error banner and connected: Listen card `data-state` connected, the status line is exactly `Listening on module I` and holds no error word, peer `connected`, no dashboard error banner (`.alert-danger`), no page error; the card never showed the lost text or `failed` during the window |
| restore | the finally of the case sends `readdress off` (after leaving the session, so the SUT does not see a second change of a page that is ending), the fixture of the case and the module and session fixtures check that the router is at its default and not moved (an error at teardown otherwise) |

**TURN on or off.** The documented run is the NAT row (`turn.toml`, TURN on): the page gathers a server-reflexive candidate from coturn, the direct pair is
used, and the restart may fetch a credential again (`GET /rtc/ice`; the case waits for two credentials of the login before it starts and records the statuses).
Without `turn.toml` the case runs unchanged (the route answers 404). The relay path is not exercised (the relay is on the other side of the address change).

**What is observable for "the old candidate pair is removed on the SUT side", and what is not.**

| Observable | How | Status |
|------------|-----|--------|
| the address the SUT sees the browser at, before and after | conntrack of the NAT router (`mappings`); the reply tuple of the browser's flow to `<SUT>:50000` | asserted (the router's own view of what the SUT receives) |
| the SUT still sending to the old address | the router's `dvt-unmapped-media` counter in filter INPUT: the SUT's media port sending UDP to an outside address no translation maps any more; counted over a settled 3 s window with the talker on air | asserted (0 datagrams). This is the black-box evidence that the old pair is no longer the SUT's destination; it does not show the SUT's internal state |
| the browser's own view of the new address | local candidates in `getStats` before and after (a `prflx` candidate with the new address, where the engine reports it) | recorded, not asserted |
| the edge's remote address of the circuit | the operator client list (`GET /api/v1/operator/clients?protocol=webrtc`): the row of the circuit, its field names and every address-like field before and after | recorded; UNVERIFIED which field (if any) is the edge's UDP remote address. A row that still shows the old address is a FINDING (it may be the HTTP client address), not a failure |
| hub metrics | the family names and label KEYS of every `webrtc` metric and `dv_edge_sessions` | recorded once; UNVERIFIED that any carries a per-circuit remote address. `dv_edge_webrtc_listeners` counts peers (published every 10 s, cached by the hub), not addresses, and is not used |
| the edge's candidate-pair table, its STUN state | - | NOT observable from outside; marked UNVERIFIED |

**Router-side evidence: `firsts` (ICE1c).** After `readdress on` the router records, from a small AF_PACKET sniffer (`ETH_P_ALL`, so that the frames the router sends are seen too) on its outside interface
(`PacketSniffer` in `dvtest/tools/nat_router.py`; the image has no tcpdump, and `nat-router` now has `CAP_NET_RAW` besides `NET_ADMIN`, this service only,
`rig/compose.yml`), every UDP datagram of the SUT's media port, both directions, as it is on the wire after the NAT (browser to SUT) or before it is undone
(SUT to browser). It is started before the rules change, stops at `off` (or after 22 s) and is read with the read-only word `firsts` (`NatClient.firsts()`;
parsers and the collector `nat.Firsts` are unit tested in `cases/unit/test_nat_firsts.py`). Without the capability `readdress` still works and `firsts`
carries an `error`; the rig self-test asserts `sniffer: ok`. Per direction: the count, the first datagram (seconds after the router began the change, kind), the first of
each kind, the last, and the count per second for 20 s.

| Direction (SUT's view) | Meaning |
|------------------------|---------|
| `from_new` | browser to SUT, source = the NEW outside address (the new mapping) |
| `from_old` | browser to SUT, source = the OLD address (none expected: the conntrack was flushed) |
| `to_new` | SUT to browser, sent to the new address: the edge following the browser |
| `to_old` | SUT to browser, still sent to the old address: the edge's stale sends |

Kinds: `stun-binding-request` / `-response` / `-indication` / `-error` (magic cookie 0x2112A442 at bytes 4 to 7), `stun-other`, `rtp` and `rtcp` (version bits 10; RTCP
when the second byte is 192 to 223), `dtls` (first byte 20 to 63), `other`. The case waits until 21 s after the change (the talker is still on air), reads `firsts` and records,
in `out/<run>/web/ice1-results.jsonl` and as `FINDING: ICE1 ...` lines, per engine and NAT type, never as an assertion:

| Key | Value |
|-----|-------|
| `first_stun_new_s` | first STUN binding request browser to SUT from the new mapping |
| `first_udp_new_s` | first UDP datagram of any kind browser to SUT from the new mapping (with its kind in the detail key) |
| `first_sut_to_new_s` | first datagram SUT to the new mapping (the edge follows) |
| `stale_to_old_per_s` | SUT to old mapping datagrams per second, 20 values |
| `firsts: detail ...` | the kinds of the firsts, the first STUN response and first RTP to the new mapping, the stale total and the time of the last stale send, the kinds seen per direction |

`None` means not seen within 20 s. The `unmapped_media` counter above is the same stale-send count seen from the iptables side after the audio is back (asserted); `firsts` is the timeline.

**Run.** Same as the NAT row of the SKILL table with the talker hub on (about 4 to 6 minutes per engine with TURN on: 9 runs of 30 to 60 s plus the credential pacer of
one login; the first run measures it):

```sh
export XLXD_SUT_IMAGE=dv-router:wip-97c669d DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml \
  scripts/run.sh -m rig --profile web,browser,nat -- cases/test_web_nat_ice1.py
```

`-k readdress` runs the rig self-test only (about 30 s); `-k chromium` one engine. The measurements go to `out/<run>/web/ice1-results.jsonl` and, once each, to
`out/<run>/web-findings.txt` as `FINDING: ICE1 ...` lines (rig addresses stay there).

**UNVERIFIED** (not read from the page sources; each is recorded by every run and then fixed or confirmed; the first run settled that the router words work):

1. The router rules (`SNAT --random`, the `RETURN` ahead of the DNAT): the three self-tests passed on the first rig run (the reflectors saw the new address, the type was unchanged, the
   original address came back). The INPUT counter chain and the parsing of `conntrack -L` output are covered by the browser cases, which have not passed yet; their router evidence
   (`router view`, `datagrams ... to the old address`) in `ice1-results.jsonl` is where to look first.
2. What triggers the restart. The browser cannot see the change (its own interface is the same; confirmed by the first run: the page saw nothing), so the trigger is in the product; the case asserts the result (a new ufrag on the
   same peer) and records the signalling it sees (`requests the page sent after the change`, the ICE and signalling events), without assuming a route.
3. The field names of the operator client list: whether `circuit` equals the id the page holds (the case falls back to the callsign, then to the newest WebRTC row and records
   which it used), and which field, if any, is an address and of which kind.
4. The Listen card alert selector (`pages.listen_alerts`) and the error word list (`pages.LISTEN_ERROR_WORDS`): not confirmed from `public.js`. The data-state and the exact status line are the verified part.
5. The 8 s and 12 s budgets are DV-Router's ICE1c figures, not measurements.
9. The `firsts` evidence (below). First run (wip-9a348ca, 9 browser cases): `CAP_NET_RAW` was enough and the SUT directions worked (`first_sut_to_new_s` 1.15 to 5.66 s; stale sends
   50 per second for about 14 s in Chromium and WebKit, the whole 20 s in Firefox), but `first_stun_new_s` and `first_udp_new_s` were None in all nine: the packet socket was bound to
   `ETH_P_IP`, and the kernel gives the frames a host SENDS (the browser's datagrams leaving the router's outside interface after SNAT) only to sockets that listen for `ETH_P_ALL`.
   The sniffer now uses `ETH_P_ALL` (unit tests: the socket arguments, and synthetic outgoing frames for the three NAT types); this is unconfirmed until the next rig run, which
   should show `browser_to_sut_new_kinds` filled. Times come from the router's clock at the moment its packet socket receives a
   frame, a few milliseconds late at most; they are seconds after the router began the change (the tester's own change time is a few milliseconds earlier).
6. A real client address change moves TCP too; this case moves UDP only (see above), so the HTTP ownership rule under an address change is not tested.
7. Whether Firefox and Playwright WebKit report the address of a `prflx` local candidate in `getStats` (Chromium does).
8. Whether an ICMP port-unreachable (none is sent: the counter rule drops silently) would change the SUT's behaviour; a real NAT usually drops silently too.

## Gaps

| What | Why |
|------|-----|
| real Safari, iOS | Playwright WebKit is not Safari; the Safari audio route is a W8 release test |
| carrier-grade NAT, double NAT, hairpin | one NAT level, the SUT on the outside |
| IPv6, NAT64 | rig is IPv4 only (the edge needs `public_ips` in the socket's family) |
| address-restricted cone | not built (the self-test would classify it as "restricted_cone" if it were); so the ICE1 address change runs on full cone, port restricted and symmetric only |
| `turns:`, TURN over TCP, UDP-blocked networks | coturn without TLS and UDP only; TCP relay denied (`no-tcp-relay`) |
| the engines' own ICE `failed` interval beyond a break of 20 s | measured through the page: Chromium and WebKit fail 10.0 s after `disconnected`; Firefox never fails inside the page's 12 s grace, so its own interval is not seen |
| Talk (uplink) through the NAT and through the relay | Listen everywhere; Talk only in the blackhole case (port restricted NAT, direct) |
| NAT mapping timeouts (UDP idle), port-exhaustion, `none` mode in a browser case | not exercised |
| many listeners behind one NAT address (edge per-IP limits) | one listener at a time |
| ICE restart after an address change | `cases/test_web_nat_ice1.py` (section "ICE1 address change"; fails on wip-97c669d, standing case for ICE1c). Automatic reconnect after a loss is not built in DV-Router and nothing asserts it; ICE1a (grace window) is covered by the blackhole case |
| ice 503 | not reachable (see above) |
| ICE failure under packet loss or latency (netem) instead of a blackhole | netem sidecars exist but are not combined with the NAT profile |

### Short profile (`DVT_NAT_SHORT=1`, TURN on, about 19 minutes)

For a check of a final tag the whole matrix (about an hour) is too long. `DVT_NAT_SHORT=1` keeps every case but runs fewer combinations
(`dvtest/nat_plan.py`, unit tests in `cases/unit/test_nat.py`); the skipped combinations say "not part of the short profile" before any browser or
account is touched.

```sh
export XLXD_SUT_IMAGE=dv-router:wip-<tag> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_NAT_SHORT=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml \
  scripts/run.sh -m rig --profile web,browser,nat -- cases/test_web_nat.py
```

| Case | Whole matrix | Short profile | Estimated seconds (short) |
|------|--------------|---------------|---------------------------|
| `test_nat_classification` | 3 NAT types | the same (the first one includes the account setup) | 70 |
| `test_direct_listen` | 9 (engine x type) | left out (every blackhole case starts with a direct connect, first audio and ICE pair) | 0 |
| `test_turn_relay` | 9 | Chromium / symmetric | 32 |
| `test_blackhole_resume_and_failure` | 9 x (2, 3, 5, 12, 16, 20 s) | every break for Chromium / port restricted and Playwright WebKit / symmetric; the other 7 combinations (Firefox x 3, Chromium x 2, WebKit x 2) only 2 s and 20 s | 2 x 175 + 7 x 75 (Firefox 85) = 880 |
| `test_talk_is_released_at_disconnected_and_needs_a_fresh_press` | 3 | left out (about 15 s each in the whole run) | 0 |
| `test_turn_credential_expiry` | 3 x 660 s | Chromium / symmetric, 120 s (`DVT_NAT_EXPIRY_S` still overrides) | 135 |
| `test_ice_404_goes_direct` | skipped with TURN | skipped | 0 |
| `test_ice_429_goes_direct` | 3 | the same | 20 |
| | | total | about 1140 (19 min) |

The estimates come from the junit times of the TURN-on run `20261102T004117Z` (a whole blackhole case 175 s on Chromium and WebKit, 215 s on Firefox;
a direct or relay case 32 s; classification 71 s with the account setup) and from the case code (a 2 s plus a 20 s break with the connect and the manual
reconnect is about 70 s). The first short run on a tag measures them; adjust `nat_plan.py` if it is over 20 minutes.

What the short profile still proves: the NAT types are what they claim; a relay session through the symmetric NAT works; the ICE1a requirement (a 2 s
blackhole resumes without a new offer) on every engine and every NAT type; "Connection interrupted. Waiting to resume." and the 12 s grace, and the
12 s and 16 s observations (resume, or lost from which side) on Chromium and WebKit, one NAT type each; a 20 s break ends with "Connection lost. Connect
again manually." and a manual reconnect sends one new offer on every combination; the relay credential survives its 120 s TTL (it does not show the
effect after 5 minutes, which needs the 660 s observation); the 429 fallback. Not covered: Firefox at 3, 5, 12 and 16 s, the direct-path arrival-timing
numbers (jitter p99, tone) of `test_direct_listen`, Talk during a blackhole, TURN relay on Firefox and WebKit, the 5 minute TURN permission expiry.

## Rerun

```sh
export XLXD_SUT_IMAGE=dv-router:wip-3b0724a DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
# TURN on: classification, direct, relay, blackhole (ICE1a, 2-20 s) and talk, expiry, 429 (about 1 hour; the expiry cases are 11 minutes each)
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml \
  scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py
# TURN off: classification, direct, 404 fallback (7 minutes)
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml \
  scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py
```

`-k classification`, `-k direct`, `-k turn_relay`, `-k blackhole`, `-k talk`, `-k expiry`, `-k 429`, `-k 404` and `-k chromium` select parts (`--capture` is left out of the commands here: the cases do not need the capture check and a pcap makes the run directory larger); the cases
that need TURN skip without `turn.toml` and the 404 case skips with it. `DVT_NAT_EXPIRY_S` (default 660) sets the length of the expiry
case. Pull `COTURN_IMAGE` once (`docker pull coturn/coturn:4.18.0`). Run alone and one rig at a time (`scripts/run.sh` takes the lock).

ICE1c expectation (DV-Router, 2026-11-08): a pure NAT rebinding is invisible to the page, so the case no longer asserts a page-side ICE
restart (the SDP ufrag comparison is evidence only); it asserts that the EDGE follows the new mapping within the budget (8 s Chromium and
Firefox, 12 s WebKit), stops sending to the old mapping, keeps the circuit, and that the operator client list field `ip` (the UDP media
destination, peer.remote) names the new mapping. Root cause on wip-97c669d per DV-Router: every remote candidate is prflx, the rebinding
yields an equal-priority pair and the ICE library keeps the current pair on a tie (Chromium/WebKit recover at the 15 s prune, Firefox never
sends USE-CANDIDATE after nomination and does not recover). The cases run signed in (br_user), never as a guest: guest renewal is bound
to the client address (web.md guest lease), a separate rule.
