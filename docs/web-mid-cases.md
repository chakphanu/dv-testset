<!-- SPDX-License-Identifier: MIT -->
# FLK10: the WebRTC edge refuses an offer without a usable `a=mid` (written offline, NOT run)

For the dv-router image that carries FLK10 (DV-Router commit `98c88b2`, image `wip-bd3ceb9`). Cases: `cases/test_web_rtc_mid.py`; helper
`dvtest/sdp_offer.py` (pure: builds the offers, mirrors the rule, judges the answer and the counters); unit tests `cases/unit/test_sdp_offer.py`.
Sources read (only `git show 98c88b2:<path>`): `crates/edge/webrtc/src/peer.rs` (`check_offer`, `check_offer_mids`), `transport.rs` (`offer_inner`),
`crates/svc/web/src/rtc/rtc_core/offer/reply.rs`, `rtc/sdp.rs`, `malformed_tests.rs`, `offer_tests.rs`, `docs/design/web.md`, `docs/design/status.md`,
`docs/api/openapi-web.yaml`.

## The rule (DV-Router 98c88b2)

`peer.rs:891` `check_offer_mids`, called at the end of `check_offer` (`peer.rs:853`, `:887`), which `Peer::new` runs first (`peer.rs:161`) and which
the ICE-restart path runs too (`peer.rs:289`). It runs after the media envelope check (one `m=audio`, at most one `m=application`, nothing else), so at
most two sections are checked.

| Item | Rule |
|---|---|
| Per media section | exactly one `a=mid:` line: none is refused (`peer.rs:899-900` when the next `m=` line arrives, `peer.rs:923-924` at the end), a second one is refused (`current.is_some()`, `peer.rs:907`) |
| Value | nonempty, at most **16 bytes** (`value.len() > 16` refused, `peer.rs:909`), every byte graphic ASCII (`is_ascii_graphic`, so no space, `peer.rs:910`; an empty value `peer.rs:908`) |
| Duplicates | across the sections of the offer: a section whose mid equals the PREVIOUS section's is refused (`previous == Some(mid)`, `peer.rs:917-918`); with at most two sections that is every pair. The comparison is on the stack's `Mid::from`, which normalises punctuation to underscores, so two distinct raw tokens can alias; the cases use identical or plain alphanumeric tokens only |
| Also new | the offer text must be ASCII and at most `MAX_ASSERTION` long, and a second `m=audio` or `m=application` is refused (`peer.rs:855-856`, `:865`, `:870`) |
| Reply | the edge answers `denied` (`transport.rs:815`); the web service maps `Answer::Denied` to HTTP **403**, problem code **`forbidden`**, detail **`WebRTC offer denied`** (`reply.rs:85`; `offer_contract.rs:232`); no `Retry-After` (that belongs to the 503 capacity refusals). It is the existing 403 SDP refusal, "real browsers always send mids" |
| Counters | NONE of the two is bumped. `status.md` (WebRTC offer contract): the refusal "does not increment the malformed counter or the capacity-only `dv_edge_webrtc_offer_refused_total` reason set"; `dv_web_rtc_refused_total` has the same ten edge reasons plus seven web ones, all capacity (`Answer::Full`); `Answer::Denied` takes the `offer_reject` path without a reason. The edge only records the fixed `stack_error` peer-failure diagnostic (`PeerReason::StackError`, `transport.rs:280`), which is not a metric family in the black box. A refused offer still consumes its nonce and its join slot (`transport.rs:253`: `joins.push_back` precedes `Peer::new` at `:267`), which is why the cases space the offers by 0.7 s |

Route: `POST /api/v1/rtc/offer` with the JSON body `{"sdp": <offer>, "room": "<module>"}`, as a logged-in user (or a guest lease) with the CSRF header;
the web service checks only an envelope (`rtc/sdp.rs`: ASCII, `v=0`, line shape, one consistent sha-256 fingerprint, an `m=audio`), signs an assertion and
hands it to the edge, so the mid refusal comes from the edge through the relay. A refused offer releases its circuit record at once (`offer_reject`).
An unserved room is ALSO 403 `forbidden` (`transport.rs:224`), which is why the cases first prove that a well-formed offer to the same room is admitted.

## Cases (`cases/test_web_rtc_mid.py`)

| Case | Offer | Expected |
|---|---|---|
| precondition (module fixture) | audio section, mid `audio`, PCMU receive-only, hand-written | 200 with an answer and a `sess_id`, closed at once; otherwise the module fails with the reason (room not served, or the offer text not accepted) |
| `test_refused_offer[no_mid]` | audio section without `a=mid` | 403 `forbidden`, no Retry-After |
| `test_refused_offer[duplicate_mid]` | audio and application sections, both `a=mid:audio` | 403 `forbidden` |
| `test_refused_offer[empty_mid]` | `a=mid:` with no value | 403 `forbidden` |
| `test_refused_offer[over_long_mid]` | a 17 byte mid | 403 `forbidden` |
| `test_refused_offer[second_section_without_mid]` | valid audio mid, application section without one | 403 `forbidden` |
| `test_refused_offer[two_mids_in_one_section]` | two `a=mid:` lines in the audio section | 403 `forbidden` |
| `test_refused_offer[mid_with_a_space]` | `a=mid:au dio` | 403 `forbidden` |
| `test_mid_at_the_limit_is_not_refused` | a 16 byte mid | NOT 403 `forbidden`; also 200 with a `sess_id` (the control; closed after) |
| `test_mid_refusals_move_no_refusal_counter` | the seven refused offers again, between two settled reads | no reason of `dv_edge_webrtc_offer_refused_total` and none of `dv_web_rtc_refused_total` moves (delta zero in both; both families must be exposed, which they are on an image with WEBSTALL1) |

Asserted: status 403, code `forbidden`, no Retry-After. The detail text is only compared: a different one is a `web.finding()` ("FLK10 ...: the refusal
says ..."), never a failure. The counter case reads the hub through the control channel (`DVT_RIG_CONTROL=1`, set by `run.sh`); the families are published
about every 10 s and cached, so `refusals.settled` waits for two equal reads 12 s apart (`docs/web-refusals-cases.md`).

The user is `N0MID` (made by the `web_user` fixture, no TX right); the offers are posted by the tester's own HTTPS client (`dvtest/web.py`), so no browser is
needed. They are hand-written from the edge's own fixture (`crates/edge/webrtc/testdata/obs1-offer.sdp`) with CRLF line ends, ICE credentials, one
documentation-range host candidate (192.0.2.2) and a sha-256 fingerprint of the right shape (no DTLS handshake ever runs, the offer is only admitted or
refused). The control circuits are closed through `POST /api/v1/rtc/close`; the cap of eight circuits per login is never near.

## The switch

`DVT_FLK10=on` (passed to the tester by `rig/compose.yml`, listed in `dvtest/target.py`; `cases/unit/test_tester_env.py` guards the pass-through). Without it every
case skips with the reason `DVT_FLK10=on not set: the mid cases are for an image with FLK10 ...` (a module level `skipif`, so nothing is registered or posted).
On an image before FLK10 the four refusal cases would fail (a mid-less offer is accepted or fails inside the stack), which is the reason for the switch.

## Run command

The cases need the web profile (`web_user`, `rig_control`) and a USRP dynamic hub so that the Listen room V is served. They use no browser, so the profile is
`web` alone; adding `browser` (as `cases/test_web_rtc_refusals.py` does for its ninth-circuit case) starts a service nothing here uses. `totp-required.toml` is
needed because the `web_operator` fixture starts from "first login = enrol" (`rig/config/dv-router/totp-required.toml`); `run.sh` appends `web.toml` and `admin.toml`
itself when they are missing, naming them is the same.

```
DVT_FLK10=on DVT_USRP_DYNAMIC=1 XLXD_SUT_IMAGE=<dv-router image wip-bd3ceb9> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml \
scripts/run.sh -m rig --profile web -- cases/test_web_rtc_mid.py
```

About 4 minutes after the rig is up (about 20 offers 0.7 s apart, one operator and one user registration, two settled counter reads of 12 to 48 s each). No
`--capture`. It can share a run with `cases/test_web_rtc_refusals.py` (same config; add `DVT_WEBSTALL1FIX=on`, profile `web,browser`) because the circuits
are closed and the counter case compares deltas; run it alone for a clean counter verdict.

## Audit: every rig-side client that sends an SDP offer

Searched `dvtest/`, `cases/` (including the NAT, K22, ICE1, talk, echo and WebRTC support code) and `dvtest/tools/` for `RTCPeerConnection`, `createOffer`,
`setLocalDescription`, `a=mid`, `m=audio`, `m=application`, `rtc/offer`, `sdp`. Only `dvtest/tools/webload_gen.py` rewrites an offer (`relay_sdp`); nothing in the rig
munges a browser offer, and the only hand-built offers are the new ones of this unit.

| Sender | Used by | SDP built by | Munged | Every m-line keeps `a=mid`? |
|---|---|---|---|---|
| The product's own web page (Listen and Talk panels, the SPA of dv-svc-web) in Playwright Chromium / Firefox / WebKit | `cases/test_web_webrtc.py`, `test_web_k22.py`, `test_web_nat.py`, `test_web_nat_ice1.py`, `test_web_talk_echo_return.py`, `test_web_session_rotation.py`, `test_web_recorder.py`, `test_web_talk_cycles.py`, `test_web_browser.py` (via `cases/browser_fixtures.py`, `webrtc_support.py`, `nat_support.py`, `talk_echo_support.py`) | the browser (`RTCPeerConnection`, `createOffer`) | no; the init scripts `dvtest/browser.py` `RTC_PROBE_JS` and `cases/talk_echo_support.py` `ECHO_RECORDER_JS` / `RELAY_JS` only wrap methods to log events (relay forces `iceTransportPolicy`); they never touch the SDP text; `cases/test_web_nat_ice1.py` and `dvtest/ice1.py` only READ the local and remote SDP | yes, browsers always send mids |
| Page script `JS_OFFER_ONLY` (`cases/websid_support.py`): `new RTCPeerConnection()` + `addTransceiver('audio', recvonly)`, then `fetch('/api/v1/rtc/offer')` | `test_web_session_rotation.py`, `test_web_rtc_refusals.py` (ninth circuit) | the browser, posted as is (`pc.localDescription.sdp`) | no | yes |
| Load generator `dvtest/tools/webload_gen.py` (service `webload`, image with aiortc) | `cases/test_web_load.py`, `test_web_soak.py`, `test_web_rss_isolation.py` | aiortc (`addTransceiver("audio", recvonly)`, `createOffer`; aiortc numbers its mids "0", "1") | `relay_sdp()` drops only the `a=fingerprint:` lines that are not sha-256 (aiortc offers sha-256, sha-384 and sha-512; the relay accepts one); all other lines pass through, so `a=mid` is kept (unit `test_the_load_generator_keeps_every_mid_it_gets_from_aiortc`) | yes |
| `dvtest/tools/prod_listen.py` (EXTERNAL production listen check, runs in the `dvt-webload` image) | owner-approved production runs only (`docs/prod-checks.md`) | aiortc, same code path | `gen.relay_sdp()` (as above) | yes |
| `dvtest/tools/prod_talk.py` (EXTERNAL browser Hold Talk) | owner-approved production runs only | the browser of the Playwright client | no (probe wrap only) | yes |
| `dvtest/sdp_offer.py` (this unit) | `cases/test_web_rtc_mid.py` | by hand, on purpose | n/a | the control offers: yes; the refused offers: no, on purpose |
| ICE restart in `cases/test_web_nat_ice1.py` | a browser `restartIce()` / re-offer | the browser | no | yes |

Rig clients that never send an SDP offer: the protocol clients (`dvtest/proto/*`), `dvtest/web.py` API cases (`cases/test_web_api.py` posts `/api/v1/rtc/offer` only with
a deliberately WRONG JSON body, `dvtest/api_web.py` `WRONG_JSON`, no SDP), `dvtest/k22.py`, `dvtest/turn.py`, `dvtest/nat.py` and the NAT router tools (ICE/STUN/TURN
traffic only).

Unit-only SDP strings (never reach the rig, no change needed): `cases/unit/test_webload_gen.py` (`test_relay_sdp_keeps_only_the_sha256_fingerprint`: no `a=mid`, tests the
fingerprint filter only), `cases/unit/test_ice1.py` (`SDP_A`, `SDP_B`: ufrag/pwd comparison for the restart detector), `cases/unit/test_talk_echo_support.py` (offer rows of an
audit table), the test in `cases/unit/test_sdp_offer.py` (which also asserts that no other non-unit Python file writes `m=audio` lines: a new one must keep its mids).

Result of the audit: no rig-side SDP loses or duplicates a mid; no change to an existing sender was needed.

## GAPs

* The punctuation normalisation of `Mid::from` (two raw tokens that alias, e.g. `a.b` and `a_b`) is documented in the rule but not driven: the exact mapping is the
  pinned stack's (str0m 0.24.1) and was not read.
* A restart (`ice_restart`) offer with a bad mid goes through the same `check_offer` (`peer.rs:289`), but needs an established circuit and the original offer; not driven.
* The `stack_error` peer-failure diagnostic is not visible to a black box (no metric family); the counters judged are the two offer-refusal families only.
* The malformed-signalling counter is not exposed under a name the rig reads; the image says the mid refusal does not touch it (not asserted).
