<!-- SPDX-License-Identifier: MIT -->
# Web unit: K22 / K22b cases (RTC circuit identity and the guest lease)

`cases/test_web_k22.py` (web and browser profile, no NAT), `dvtest/k22.py` (pure judgements, unit tests `cases/unit/test_k22.py`)
and the config snippet `rig/config/dv-router/guest.toml` (`[web] guest_listen = true`). Calibrated against the image
**`dv-router:wip-3b0724a`** (native launch; it contains TALK1, DOC6, K22, ICE1a and K22b). The two EDG1 cases (silent close, closed `ctl` channel) need an image with DV-Router `dbedd14` or later; an older image fails them, which is expected. DV-Router documents read:
`docs/design/web.md` (WebRTC edge, signalling handoff, the lease table, guest renewal, revocation, ICE1a), `docs/api/openapi-web.yaml`
(`/rtc/offer`, `/rtc/close`, `/rtc/alive`, `/operator/clients`, `/operator/edges/{kind}/clients/{circuit}/kick`), `web/site/public.js`
(texts and the 12 s grace). Engine: Playwright Chromium only (the circuit, lease and edge rules do not depend on the engine; the engine
matrix is the job of the NAT and WebRTC files).

## What K22 / K22b say (from the documents)

| Rule | Where |
|------|-------|
| The edge reports an optional `rtc_id` per peer; a revoke (`{op:"revoke", sess_id}`, sent on close, logout, account change) removes exactly the peers whose stored `rtc_id` equals the command id; no matching peer is a successful no-op | web.md "Revocation", openapi `/rtc/close` |
| An authenticated circuit renews to a 30 s lease; a guest lease renews only with a fresh (< 15 s) mapping of a connected peer with the circuit's own `rtc_id` on an owned edge incarnation; otherwise `/rtc/alive` is 204 with the existing deadline, then 401 at normal expiry, then 403 once cleanup removed the record | web.md lease table, openapi `/rtc/alive` |
| K22b: the guest proof is cleared only by the Disconnect of its own peer (other peers coming and going do not stop a guest's renewal); the lease query is filtered by `rtc_id` | web.md "Guest renewal" |
| Foreign circuit (another login, another guest IP/User-Agent, a login on a guest circuit and the reverse) and an unknown id: 403 `forbidden`, same detail; an invalid id: 400 | web.md lease table |
| Hub outage longer than about 30 s: guest leases expire and the sweep revokes their peers; an offer made without a fresh mapping capability omits `rtc_id` and that guest's lease ends at 15 s | web.md "Operationally ..." |
| Rollout rule: edges before or with the hub; one `[edges.webrtc]` per node | DV-Router decision |

## Setup

```sh
export XLXD_SUT_IMAGE=dv-router:wip-3b0724a DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path>
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/guest.toml \
  scripts/run.sh -m rig --profile web,browser -- cases/test_web_k22.py
```

The file skips without `guest.toml` (it reads `info.guest_listen` of `/api/v1/public/state`). Do not run `cases/test_web_webrtc.py`
with `guest.toml`: its `test_listen_is_not_offered_to_anonymous` expects guests off. The idle case defaults to 10 minutes
(`DVT_K22_IDLE_S`; `3600` is the criterion's hour); `-k "not idle"` leaves it out.

How the cases work. The edge calls a peer connected only after ICE and DTLS, so every circuit is made by the real page in a browser.
The lease calls (`POST /rtc/alive`, `/rtc/close`) are made from inside the same browser context (same IP, same User-Agent, same cookie
jar), which is what the ownership rules key on; a poller in the page calls `alive` every second and keeps `[epoch, status]`. The
page's own revocation (its Disconnect, a failure, the pagehide) is blocked in the network layer of the context where the lease alone
must decide (calls of the cases carry `x-dvt-k22` and go through). Guests are two per IPv4 address, so a case holds at most two guests
and only four pages (four event streams per address).

## Cases

| Case | Asserts |
|------|---------|
| `test_revoke_removes_exactly_that_peer` | two logged-in listeners; `POST /rtc/close` of A's circuit answers 204; the listener gauge falls from 2 to 1; B's peer stays connected and keeps hearing audio, B's own `alive` stays 204; `alive` on the closed circuit is 401 or 403 |
| `test_repeated_revoke_is_a_no_op` | the same close again and once more after a sweep: 204 or 403 (recorded), the other peer untouched |
| `test_guest_lease_without_a_peer` | a guest with a live peer: every `alive` of 45 s is 204 (longer than the 15 s initial lease and a 30 s lease); the peer is removed at the edge by the documented operator kick (a Disconnect, no revoke of the record); 204 continues, then 401 within 15 + 30 + 10 s, then 403, never going back |
| `test_silently_closed_peer_leaves_the_edge` (slow, x3) | **EDG1 (K1 fixed, DV-Router wip dbedd14; needs an image with it, an older image fails it)**: `pc.close()` with no revoke (user at once, guest at once, guest after 45 s), the page keeps calling `/rtc/alive`: the listener gauge `dv_edge_webrtc_listeners` falls below its steady value within 40 s (EDG1_GAUGE_S) and the measured seconds are recorded; for a guest the lease stops renewing: 401 within 15 + 15 + 30 + 10 s of the close, statuses never going back |
| `test_closing_only_the_ctl_channel_removes_the_peer` | **EDG1**: two logged-in listeners hear a protocol talker; only A's `ctl` DataChannel is closed (`page.evaluate` on the channel the probe recorded, `window.__dvtTap.channels`; the peer connection stays up, A's own revoke is blocked): the gauge falls by one within 40 s, a later 3 s over reaches A with under 10 packets and B with at least 100, B's peer stays `connected` and its `alive` is 204. Closing the ctl channel removes the whole peer (it used to keep listening) |
| `test_foreign_and_malformed_circuits` | other guest browser (other User-Agent) alive / close of a guest circuit, a login on a guest circuit, a guest on a login circuit, another login (the tester) on a login circuit, a never-seen id: 403; a bad id: 400; the owners are untouched |
| `test_guest_b_churn_does_not_stop_guest_a` | K22b: guest A and a login C stay connected while guest B connects and disconnects for 90 s; A's every answer is 204, A and C still hear audio |
| `test_status_stream_is_quiet_while_idle` (slow) | `GET /api/v1/public/events` sampled for `DVT_K22_IDLE_S` s with the edge up and nobody listening, reconnecting with the last event id every 30 s: no whole minute after the first holds more than 2 events (a per-5 s event is 12 a minute) |
| `test_edge_kill_stops_guest_renewal` | SIGKILL of `dv-edge-webrtc` (never the launcher) under a guest that keeps calling `alive` (the dropped-Disconnect approximation): the guest gets its 401 inside 15 + 30 + 10 s and is never renewed past that; a fresh guest connects to the restarted edge and renews; the old guest does not renew on the new process |
| `test_status_hub_outage_ends_guests` (slow) | `dv-svc-status` killed again and again for 40 s (a respawn takes 0.5 to 5 s, so one kill is no 30 s outage; every respawn is killed before it can publish): the guest's 401 follows inside the bound, its peer is dropped, and a fresh guest connects and renews afterwards |
| `test_offer_right_after_a_hub_restart` | the hub is killed and a guest offers 1, 2, 3.5, 5, 7 s later (first circuit wins): a refused offer (503), a lease ending at 15 s (no `rtc_id`) or a renewing guest (race not hit) are all legal and recorded; a lease longer than 15 s + a sweep fails |

Allow-list: `kill-proc xlxd-sut dv-edge-webrtc KILL` and `... dv-svc-status KILL` (`scripts/lib/killproc.sh`, `dvtest/control.py`, agreement
test in `cases/unit/test_control.py`, now 10 combinations); the launcher `dv-router` is never signalled.

## EDG1 bounds (what the two cases assert)

| Quantity | Bound | Derived from |
|----------|-------|--------------|
| gauge falls after a silent `pc.close()` or a closed `ctl` channel | <= 40 s (`EDG1_GAUGE_S`) | removal at once on close_notify / SCTP loss / closed channel, at the latest 15 s STUN silence; the edge publishes the gauge every ~10 s and the hub caches the snapshot (K22 saw up to 35 s after a connect) |
| gauge before the close | steady (two reads 6 s apart agree, >= 1 or >= 2) | so that a late publication of the connect is not mistaken for a removal |
| guest `/rtc/alive` after the silent close | 401 within 15 + 15 + 30 + 10 = 70 s, then 403, never back to 204 | STUN timeout + stale proof (`k22.FRESH_S`) + guest lease + margin (as `test_guest_lease_without_a_peer`) |
| a logged-in page keeps calling `/rtc/alive` | always | otherwise its own 30 s lease and the sweep would remove the peer and hide an edge that forgot it (the old K1 "30 to 38 s" was exactly that) |
| audio to the closed-ctl peer | under 10 packets in a 3 s over (150 sent) | the peer is gone; a few in-flight packets are tolerated |
| ordinary listener meanwhile | at least 100 of 150 packets, peer `connected`, `alive` 204 | the removal is per peer |

Every measured second is a `FINDING:` line and a row of `out/<run>/web/k22-results.jsonl` (keys `silent close (...)` and `ctl channel close: ...`).

## Results

Run `out/20261102T001500Z` (image `dv-router:wip-3b0724a`, `web.toml` + `guest.toml`, no capture): **12 passed in 25 min 33 s**
(10 case functions; the silent-close case runs three times). Numbers are from `out/<run>/web/k22-results.jsonl`, each also a `FINDING:` line in
`out/<run>/web-findings.txt`.

| Area | Result |
|------|--------|
| Revoke of one RTC id (K22) | two logged-in listeners, gauge `dv_edge_webrtc_listeners` 2; `POST /rtc/close` of A: 204; gauge 1; B's peer connected, B heard a 3 s over (>= 100 packets), B's `alive` 204; `alive` on A's closed circuit 403 (record already cleaned up). A's browser left `connected` 4.6 s later through the page's own "Connection ownership expired. Connect again manually." (its `alive` was refused), so the edge-side proof is the gauge, not the browser |
| Repeated revoke | statuses of three closes `[204, 403, 403]`: the second close already finds the record removed (403 `forbidden`, the lease table's "record already removed"); the edge-level no-op is not visible over HTTP; B untouched, still hearing |
| Guest lease without a peer (K22) | a guest with a live peer: 45 s of `alive`, all 204 (the initial lease is 15 s). After the operator kick of its peer (Disconnect, no revoke of the record): 204 for 29.2 s, **401 at 30.4 s**, 403 at 35.4 s, never going back. The guest lease is 30 s after the last renewal and renewal stopped at the kick |
| Foreign circuits | other guest browser (other User-Agent) alive and close of a guest circuit, a login on a guest circuit, a guest on a login circuit (alive and close), another login on a login circuit, a never-seen id: all 403; a bad id 400; owners untouched |
| K22b churn | guest A and a login C stay connected, guest B connects and disconnects 52 times in 91 s (three peers at the edge, one coming and going): A's 91 answers all 204, A and C still hear audio |
| SIGKILL of `dv-edge-webrtc` under a guest | pid killed, a fresh guest connected 2.6 s later on the new edge and renewed (all 204); the old guest: 204 for 28.7 s, **401 at 29.7 s after the kill**, i.e. the lease ran out from the last renewal before the kill: the proof stopped at once (the hub drops it when the incarnation goes), well inside the 15 s freshness bound; the old guest was not renewed by the new edge although the new edge was up 27 s before its lease ended |
| Hub killed again and again for 40 s (10 kills) | the guest: 204 for 29.2 s, **401 at 30.2 s**, 403 at 32.3 s, its peer left `connected` at 40.6 s (the sweep's revoke), a fresh guest connected 3.8 s after the outage and renewed |
| Offer right after a hub kill | offers 1, 2 and 3.5 s after the kill were refused (the page says "full: The listener limit has been reached. Try again manually."); the offer 5.0 s after the kill made a circuit, and that guest was renewed for 60 s (all 204): `rtc_id` was there, the race of web.md ("offer without a fresh mapping capability, lease ends at 15 s") was not reached in this run |
| Status stream while idle | 600 s with the edge up and nobody listening: **0** events other than `hello` on `GET /api/v1/public/events`, resumed every 30 s with the last event id (a per-5 s event would be 120). One hour was not run: 10 minutes sampled; `DVT_K22_IDLE_S=3600` repeats it |

## Findings (observed vs documented)

| # | Observed | Documented / expected |
|---|----------|-----------------------|
| K1 | **FIXED by DV-Router EDG1 (wip dbedd14), asserted since; the row below is the observation on `wip-3b0724a`.** Before: **A silently closed browser peer stays at the edge**: `pc.close()` with no revoke, then the listener gauge for 100 s. A guest that keeps calling `/rtc/alive` kept its peer counted (gauge 1 at every sample from 7 s to 100 s, in both runs: closed at once and 45 s after connecting; the edge's own minute log read `sessions 1` for three minutes) and its lease (every `alive` 204). A logged-in listener that stopped calling `alive` lost its peer 30 to 38 s later, which is its 30 s lease running out and the sweep revoking it (not distinguishable from a STUN timeout) | web.md ICE1a: "The edge independently removes an established ICE-lite peer after 15 s without a STUN Binding request". Not observed in 100 s, and not shown at all, since the peer left only with the web service's revoke. A guest that holds a ghost peer and polls `alive` keeps the edge peer and the guest slot for the guest lifetime (4 h); this is the situation K22 wants a live peer for (the mapping says `connected: true` for a peer that is not there). Chromium `pc.close()` may or may not send a DTLS close_notify; either way the edge did not remove the peer |
| K2 | the page's text for a refused offer in the hub recovery window is "full: The listener limit has been reached. Try again manually." | web.md maps edge full to 503 and a refused offer during recovery to 503; the wording does not say what the cause was (here: the hub had just been killed, no listener limit) |
| K3 | the second and later closes of the same circuit answer 403, not 204 | consistent with the lease table (a record removed after cleanup is 403); the openapi says only "no matching peer is a successful no-op" for the edge; no difference |
| K4 | the guest proof stops at once when the edge or the hub dies (401 29.7 s and 30.2 s after the kill, not "within 15 s then 30 s") | web.md bound is 15 s + 30 s; the observed behaviour is inside it |

K1 was recorded as a `FINDING:` line on `wip-3b0724a`. DV-Router fixed it as EDG1 (wip dbedd14): the edge now acts on `Event::Closed`
(DTLS close_notify, SCTP loss), a closed `ctl` DataChannel and a non-alive RTC go through the existing removal once, the 15 s STUN silence
timeout is real, and the edge removes on ICE Disconnected. The two EDG1 cases above assert this; they have NOT been run yet (no EDG1 image
at the time of writing): the first run on an EDG1 image must be watched (see "EDG1 bounds"). The other cases still use the operator kick, the
revoke or the process kill to remove a peer, which are documented removals and work on both images.

## Not done, and why

| What | Why |
|------|-----|
| the new web service with an old hub (one image) | DV-Router decided to skip: one image has one version of both |
| several WebRTC edges | a node has exactly one `[edges.webrtc]`; the `rtc_id` filter is exercised with one edge and several guests and peers (revoke, churn) |
| a hub that stays down for 30 s | the launcher respawns the hub in 0.5 to 5 s and the allow-list can only SIGKILL; the case kills every respawn for 40 s instead (the guest proof cannot be refreshed in that time, which is what the document's rule is about). The hub is a child (`dv-svc-status`), not in the launcher process; the fabric router, which is in the launcher, is not touched |
| a real dropped Disconnect | the edge killed with SIGKILL stands for it (no Disconnect is delivered); a browser that vanishes with the network cut needs the NAT profile |
| the hour of idle | ten minutes were sampled (the hour is a setting, `DVT_K22_IDLE_S=3600`); the criterion is about a per-5 s event, which 10 minutes would have shown 120 times |
| the race of "offer without rtc_id" | not reached in this run (the offers were refused until the hub knew the edge, then carried `rtc_id`); the case accepts all three legal outcomes and fails only on a longer lease |
| a dropped Disconnect by a vanished browser | the silent `pc.close()` is where it showed finding K1; the SIGKILL of the edge is the case that stands for the dropped Disconnect |
