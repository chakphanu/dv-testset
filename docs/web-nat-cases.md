<!-- SPDX-License-Identifier: MIT -->
# Web unit, stage 4: NAT behaviour of the browser WebRTC path

`cases/test_web_nat.py` (7 case functions, 45 runs: classification x3 NAT types, then each browser case on three engines
x three NAT types, the expiry case behind the symmetric NAT only, the 429 case behind the port restricted NAT) and the helpers
`cases/nat_support.py`, `dvtest/nat.py`, `dvtest/turn.py`. Built on the web, browser and WebRTC stages (`docs/rig.md` "Web
profile", "Browser profile", `docs/web-browser-cases.md`) and on the new compose profile `nat` (`docs/rig.md` "NAT profile").
Calibrated against the image **`dv-router:wip-5aa7cfd`** (native launch). DV-Router documents read: `docs/design/web.md` (WebRTC edge,
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
| `test_blackhole_resume_and_failure` (9, slow) | UDP blackholes of 2, 3, 5 s end with the same peer, the same circuit (no new `POST /rtc/offer`) and audio; then 8, 12, 20, 30 s and an open one (cap 150 s) until the page gives up: the failure interval is recorded; after the loss the page says to connect again, sends no offer by itself, and a manual connect sends exactly one new offer with a new circuit and audio flows |
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
tester's `out/<run>/web/nat-results.jsonl`, each also a `FINDING:` line in `out/<run>/web-findings.txt`.

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

### UDP blackhole and the ICE failure interval (`out/20261101T172915Z`; the router's counters showed datagrams dropped in every window)

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

Each is a `FINDING:` line; the numbered ones are the ones that differ from or add to the documents. The first is asserted by the case
(the adopted criterion) and fails on Firefox; none of the others is asserted.

| # | Observed | Documented / expected |
|---|----------|-----------------------|
| N1 | **Firefox does not meet "a 2-5 s blackhole resumes without a new offer"**: in 6 of 6 runs (3 in the last full run, 3 earlier) a break of 2 s (twice), 3 s (once) or 5 s (three times) ended the session (ICE `disconnected` 4.6 to 8.3 s after the start of the break, even after the break was over). Chromium and Playwright WebKit pass 2, 3 and 5 s in every run (loss at 8 s) | testing.md: 2-5 s "shorter than the measured ICE failure interval" must resume; the interval was to be measured. It is 4.6 to 8.3 s for Firefox, so 5 s is not below it |
| N2 | the page closes its peer at the first ICE `disconnected` of the engine (5 to 8 s), so the 30 s ICE `failed` of the engines is never reached and a listener loses audio after 5 to 8 s of silence on the path, not 30 s | web.md: "When the browser reports peer/ICE failure or disconnection ... closes the old peer ... Connection lost. Connect again manually." and "Browser detection time is not fixed by the page"; consistent, but the practical interval is the `disconnected` one |
| N3 | the page's loss does not fire `connectionstatechange` (the close is synchronous in the ICE handler): the first and only state event of the page in a loss is `iceconnectionstatechange: disconnected` | not documented |
| N4 | the relay works through all three NAT types with a real coturn and the SUT's credentials; the selected pair is `relay`; the page makes one `GET /rtc/ice` per connect; username `<expiry>:<opaque>`, credential HMAC-SHA1 as documented; no secret or credential in the page | as documented (deployment.md "Owner-run TURN", web.md) |
| N5 | an established relay session was not cut by the credential's expiry (120 s) in 660 s on any engine | deployment.md: "existing credentials expire at the issued timestamp"; what coturn does to a running allocation is not stated. The TTL of one credential is not the lifetime of a relay session |
| N6 | coturn's `allowed-peer-ip=<SUT address>` overrides the denied 172.16.0.0/12 as documented, and the template needs two more notes for a rig: `no-cli` is rejected by coturn 4.18 ("deprecated") and `no-dtls` is not a valid option there; the image's executable needs `NET_BIND_SERVICE` in the bounding set (file capability) even for an unprivileged port | template in deployment.md |
| N7 | with TURN configured the browser also gathers a `srflx` candidate from coturn's STUN and the direct pair is `srflx` where the NAT keeps the mapping; without TURN the pair is `prflx` (the edge's STUN reply names the mapping) | not documented (no STUN server of the product) |
| N8 | the capture check lists the browsers' STUN checks to `<SUT>:50000` as `other.unclassified` warnings (34 datagrams in the run) | known: dvcheck knows no STUN |
| N9 | rig: the SUT's dynamic USRP hub keeps a closed client until its idle timeout; 36 new clients in 7 minutes (one per over) made later clients get no `REG:OK`, so the long cases reuse one client | rig matter, not a product claim |

Other FINDING lines (arrival statistics, pairs, connection times) are measurements, not differences.

## Gaps

| What | Why |
|------|-----|
| real Safari, iOS | Playwright WebKit is not Safari; the Safari audio route is a W8 release test |
| carrier-grade NAT, double NAT, hairpin | one NAT level, the SUT on the outside |
| IPv6, NAT64 | rig is IPv4 only (the edge needs `public_ips` in the socket's family) |
| address-restricted cone | not built (the self-test would classify it as "restricted_cone" if it were) |
| `turns:`, TURN over TCP, UDP-blocked networks | coturn without TLS and UDP only; TCP relay denied (`no-tcp-relay`) |
| the engines' own ICE `failed` interval (30 s) | the page closes the peer at `disconnected`; a raw `RTCPeerConnection` page would be needed |
| Talk (uplink) through the NAT and through the relay | only Listen is covered |
| NAT mapping timeouts (UDP idle), port-exhaustion, `none` mode in a browser case | not exercised |
| many listeners behind one NAT address (edge per-IP limits) | one listener at a time |
| ICE restart, automatic reconnect | not built (DV-Router ICE1); nothing here asserts them |
| ice 503 | not reachable (see above) |
| ICE failure under packet loss or latency (netem) instead of a blackhole | netem sidecars exist but are not combined with the NAT profile |

## Rerun

```sh
export XLXD_SUT_IMAGE=dv-router:wip-5aa7cfd DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
# TURN on: classification, direct, relay, blackhole, expiry, 429 (about 1 hour; the expiry cases are 11 minutes each)
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/turn.toml \
  scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py
# TURN off: classification, direct, 404 fallback (7 minutes)
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml \
  scripts/run.sh -m rig --capture --profile web,browser,nat -- cases/test_web_nat.py
```

`-k classification`, `-k direct`, `-k turn_relay`, `-k blackhole`, `-k expiry`, `-k 429`, `-k 404` and `-k chromium` select parts; the cases
that need TURN skip without `turn.toml` and the 404 case skips with it. `DVT_NAT_EXPIRY_S` (default 660) sets the length of the expiry
case. Pull `COTURN_IMAGE` once (`docker pull coturn/coturn:4.18.0`). Run alone and one rig at a time (`scripts/run.sh` takes the lock).
