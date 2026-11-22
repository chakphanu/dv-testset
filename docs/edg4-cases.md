<!-- SPDX-License-Identifier: MIT -->
# EDG4 cases: one ctl DataChannel (EDG3), consent expiry of validated sources (EDG4b), the handshake pin (EDG4c)

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`, unit `5abe7510`: EDG3, EDG4b, EDG4c). **Nothing here has run on a rig.**
Case file `cases/test_web_edg4.py` (reuses `K22Page`, `make_page` and the gauge helpers of `cases/test_web_k22.py`, the offer helpers of `dvtest/sdp_offer.py`), helpers `dvtest/edg4.py` (counters, SDP facts, the
expiry judge) and `dvtest/stun_pair.py` (a signed STUN Binding client: RFC 5389 / 8445, stdlib only, so that a tester socket can become a VALIDATED source of a peer), unit tests `cases/unit/test_edg4_lib.py`,
switch `DVT_EDG4` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`). Notes: stdout and `out/<run>/edg4.txt` (`RECORD`, `FINDING`).

**Gate.** `DVT_EDG4=on`; without it every case skips with the reason.

## What is asserted (rules read from `docs/design/web.md` at `bc9af2b1`, UNVERIFIED on a rig)

* **EDG3** (web.md 1367-1394, 1328-1333; status.md 872-873). One ordered, reliable DataChannel `ctl` per peer, shared by Listen, module select and Talk. Another open is refused: label not `ctl` gives
  `unknown_label`, a second `ctl` gives `channel_limit`, an unordered or unreliable `ctl` gives `invalid_config`, an open during a pending close gives `closing` (checked in this order). The FIRST refusal
  resets only that SCTP stream; the peer, `ctl` and media stay (`channel_refused{reason}` +1, `channel_closed{reason}` +1). The SECOND refusal over the peer's lifetime closes the whole association cleanly
  (RTCP BYE, SCTP SHUTDOWN, DTLS close_notify: the browser's connection ends at once, not after the ICE consent timeout) and adds `channel_refused{reason}` +1, `channel_closed` +0,
  `channel_limit_closes_total` +1; the edge logs `peer failed ... reason=channel_limit`. The budget is spent by refusals, not by open channels, and is never replenished.
* **EDG4b** (web.md 1149-1188). A validated source (an integrity-checked Binding success for a transaction outside the 32-entry replay window) is refreshed only by a fresh signed check or replay-protected
  traffic (SRTP, DTLS on the admitted `ctl`, handshake completion) from that same source; SRTCP, a replayed transaction, forged packets and SDP candidates do not refresh it. 30 s without refresh expires
  the source (`dv_edge_webrtc_ice_source_expired_total`, no labels, once per source); the peer is not closed (a silent whole peer dies earlier, of the 15 s ICE-lite liveness); a non-STUN datagram from an
  expired or unknown source is counted `dv_edge_webrtc_unrouted_total{reason=no_source|wrong_peer}`; a fresh signed check validates the source again. No log line is written on an expiry.
* **EDG4c** (web.md 1147, 1236-1238; status.md 1027-1036). Before the DTLS handshake completes, non-STUN input is accepted only from the selected remote (selected by a signed USE-CANDIDATE check; a strictly
  higher priority nomination switches it, a tie does not). A plaintext epoch-0 fatal alert from another validated source is dropped (`unrouted{wrong_peer}`) and the peer survives. After the handshake any
  validated source may send media and control; egress still goes to the selected remote only.

| Case | Client | Assertion |
|------|--------|-----------|
| EDG3-a | browser (Playwright Chromium, a logged-in user) | a normal Listen session records exactly one `channel-open` (`ctl`) and no `channel-close`; no `channel_refused`, `channel_closed` or `channel_limit_closes` counter moves |
| EDG3-b | the same page | `createDataChannel('x')`: a `channel-close` event for `x`, `ctl` stays, the connection stays `connected`; `refused{unknown_label}` +1, `closed{unknown_label}` +1, `limit_closes` 0. Then `createDataChannel('y')`: the connection leaves `connected` within 8 s, the listeners gauge falls below its level within 40 s, `limit_closes` +1, `refused{unknown_label}` +2 in all |
| EDG4b | browser primary + tester UDP sockets (`stun_pair`: USERNAME `<edge ufrag>:<client ufrag>`, integrity keyed with the edge `ice-pwd`, both read from the page's SDP) | control: a validated socket refreshed every 5 s for 45 s does not move `ice_source_expired_total`; silent: a validated socket expires once, seen within 80 s (30 s + the 10 s publication + hub caching), not before 25 s (`edg4.judge_expiry`); the browser's peer stays `connected`; after the expiry 3 junk datagrams from the socket raise `unrouted`; a fresh signed check validates it again. A junk datagram inside the 30 s is only recorded |
| EDG4c | hand-written offer (`sdp_offer.build_offer`, a logged-in user's `POST /api/v1/rtc/offer`) + two tester sockets | A: signed check with USE-CANDIDATE gets a Binding success; B (same IP, other port): plain signed check gets one; B sends the plaintext fatal DTLS alert (`stun_pair.dtls_fatal_alert`); A's next signed check 1.5 s later is still answered (the peer survived); `unrouted` rises by at least 1 (a FINDING when it is not `wrong_peer`) |

## Run command

```
DVT_USRP_DYNAMIC=1 DVT_EDG4=on XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml \
scripts/run.sh -m rig --profile web,browser -- cases/test_web_edg4.py
```

Plain `--profile web,browser`: no NAT, no TURN, no `webload` (aiortc is not needed: the browser is the only client that completes DTLS, the tester sockets only validate sources). Estimated time (not
measured): EDG3-a 1 minute, EDG3-b 2 minutes (each counter wait up to 60 s), EDG4b about 4 minutes (45 s control, up to 80 s silent, the settles), EDG4c 1 minute; about 8 minutes. One rig, nothing else at the
same time; the `ice_source_expired` deltas assume no other peer expires a source in the window. The hand-written offer of EDG4c is removed by the edge at `connect_timeout_secs` (default 10 s): its steps run in
under 6 s, the counters are read after.

## GAPs

* **The handshake switch itself** (EDG4c: a nominating check from an alternate source at a strictly higher priority while the handshake is held, the DTLS flight completing over the alternate pair, the old pair's
  alert rejected) needs a DTLS client that continues a handshake from another source address with controlled flight timing: the recorded vectors (`crates/edge/webrtc/testdata/two-pair-datagrams.trace`,
  `fuzz/seeds/two-pair-sources.tsv`) belong to one in-memory unit-test session (keys, ufrags) and are useless against a live edge. Only the pre-handshake pin and the counter are asserted.
* **Replay-protected refresh** (an authenticated media client keeping a second source fresh, SRTCP-only traffic not refreshing it) needs an SRTP / DTLS sender at a second address: aiortc has no `ctl`
  channel in `dvtest/tools/webload_gen.py` and the tester image has no aiortc. Only signed checks are used as the fresh traffic.
* **Exactness at the 30 s mark** (the unit tests use a virtual 10 ms clock): the rig sees the counter 10 to 40 s late (10 s publication, hub caching), so only "not before 25 s, once, within 80 s" is judged.
* **A spoofed-address test** needs raw IP injection; the tester cannot do it. An address-rebinding variant (a NAT readdress during the 30 s) is the job of `--profile nat` (`dvtest/nat.py`) and is not written.
* **Other browsers**: the cases run Chromium only (the edge rules do not depend on the engine); what a WebKit or Firefox page reports as the state of a closed association is not recorded.
* The EDG3 refusals `invalid_config` (an unordered `ctl`) and `closing`, and the log line `reason=channel_limit`, are not asserted (the first needs the budget of a fresh peer per reason, the log is host-side).

## Questions for the DV-Router team

1. EDG3: confirm that the second refusal of ANY reason (here two `unknown_label` opens) closes the association, and that the first one counts `closed{reason}` +1 while the second counts it 0.
2. EDG4b: is a signed Binding request from a tester address that knows both ufrags and the edge `ice-pwd` meant to validate a NEW source on a connected peer (up to the 100-source cap)? The cases assume yes.
3. Is `a=candidate ... typ host` always present in the answer SDP of the rig image (the cases read the edge address from it), or must the tester take the edge address from configuration?
