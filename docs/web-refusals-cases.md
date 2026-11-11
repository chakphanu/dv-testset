<!-- SPDX-License-Identifier: MIT -->
# WEBSTALL1 fix image: WebRTC capacity refusals, their counters and the quiet worker (written offline, NOT run)

For the dv-router image that carries the WEBSTALL1 fix (merge `17b05933`; not built when this was written). Cases: `cases/test_web_rtc_refusals.py`
(a, b), `cases/test_web_load.py` (c, d and the slow-request rule), helpers `dvtest/refusals.py`, the load generator `dvtest/tools/webload_gen.py`,
the snippet `rig/config/dv-router/join-window.toml`. Unit tests: `cases/unit/test_refusals.py`, `cases/unit/test_web_rtc_refusals_case.py`,
`cases/unit/test_webload.py`, `cases/unit/test_webload_gen.py`. Sources read (only `git show 17b05933:<path>`): `docs/design/status.md` (telemetry
table), `docs/design/web.md` ("signalling handoff", the per-limit tables, the join gate), `crates/svc/web/src/rtc/rtc_core/table.rs` (`Capacity`),
`crates/svc/web/src/rtc/http.rs`, `crates/svc/web/src/telemetry.rs`, `crates/edge/webrtc/src/transport.rs`, `settings.rs`.

## The switch

`DVT_WEBSTALL1FIX=on` (passed to the tester by `rig/compose.yml`; `cases/unit/test_tester_env.py` guards that). Without it the new strict assertions
skip with the reason `DVT_WEBSTALL1FIX=on not set`, and every existing case behaves as before, so the current image `wip-a31709a` still passes them.
The load cases record the new counters whether the switch is on or not (an image without the families records `null`).

## What the image says, and what is asserted

| Item | Statement of dv-router | Here |
|---|---|---|
| 1 | Durable writes (audit, accounts, RTC journal) no longer run on the runtime worker of `dv-svc-web` / `dv-svc-admin`; required records still fail closed, login records are best effort, order is kept | The slow-request detector of the load cases (`ws.slow_problems`) is a FAILURE under the switch: any web request above 1 s fails the case unless its path ends with `/auth/register` (the deliberate 2 s delay). A login is NOT exempt. Without the switch it stays FINDING WEBSTALL1. The fail-closed / best-effort / order semantics are not black-box visible without a disk fault: GAP. The deterministic disk-throttle stall case stays a GAP pending the owner's go for root (`docs/webstall1-cases.md`) |
| 2 | 503 keeps `Retry-After: 5`; the problem detail names the limit | Asserted: status 503, code `unavailable`, `Retry-After: 5` (`refusals.refusal_shape_problems`). The detail is RECORDED (`refusals.detail_note`), never judged; `web.finding` when the ninth circuit says something other than the documented string. The load generator's retry rule is status 503 + code `unavailable` + a positive Retry-After, with the old text rule ("capacity reached" in a body without a code) kept as the fallback for the old shape |
| 3a | `dv_edge_webrtc_offer_refused_total{reason}` (10 reasons) and `dv_web_rtc_refused_total{reason}` (17), zeros included | Asserted (`test_both_families_expose_every_reason`); `dv_edge_webrtc_join_refused_total{reason="core"}` must still be there with only that reason |
| 3b | the ninth circuit of one login is refused and counts `login_circuits` | Asserted (`test_ninth_circuit_counts_login_circuits`): exactly +1 at the web family, no other web reason, no edge reason |
| 3c | a burst of joins in the edge's window counts `join_window` | Asserted with a window of 1 per second (`join-window.toml`); with the default window of 5 it is a GAP (below) |
| 3d | the load summary records both counters by reason, before and after the gate | Recorded in every load case (`refusals` in `out/<run>/web/load.json` and one printed line); asserted only that the exposition is complete (shape) under the switch |

The two families are never added: an edge refusal relayed as a 503 is counted once in each (`status.md`: "must not be summed as unique refused joins").

### Problem code and per-limit strings (DV-Router 17b05933)

Code `unavailable` (HTTP 503, `Retry-After: 5`) for all of them. Edge reasons (also in the web family): `join_window` "WebRTC join window capacity reached",
`listeners` "WebRTC listener capacity reached", `room` "WebRTC room listener capacity reached", `pending` "WebRTC pending output capacity reached",
`retired` "WebRTC retired peer capacity reached", `guests` "WebRTC guest listener capacity reached", `guest_ip` "WebRTC guest IP capacity reached",
`nonce_cache` "WebRTC nonce cache capacity reached", `slots` "WebRTC peer slot capacity reached", `signalling_busy` "WebRTC signalling queue capacity reached".
Web only: `web_records` "WebRTC ownership record capacity reached", `web_sessions` "WebRTC web session capacity reached", `login_circuits` "WebRTC login circuit
capacity reached", `web_guest_records` "WebRTC guest ownership record capacity reached", `web_guests` "WebRTC web guest session capacity reached",
`web_guest_ip` "WebRTC web guest IP capacity reached", `turn_issues` "WebRTC TURN issue table capacity reached". The table is `refusals.DETAILS`.

## Run commands (next image)

Common prefix (as `docs/web-api-cases.md`): `XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template>`; add
`DVT_WEBSTALL1FIX=on`. Counters are published about every 10 s and cached by the hub, so every comparison waits for two equal reads 12 s apart (the times below include that).

| Run | Command | Time |
|---|---|---|
| a + b | `DVT_WEBSTALL1FIX=on DVT_USRP_DYNAMIC=1 ... scripts/run.sh -m rig --profile web,browser -- cases/test_web_rtc_refusals.py` | about 5 minutes after the rig is up (a: up to 90 s of waiting for the first records, normally 15 s; b: nine offers 0.7 s apart, one baseline and two counter reads, 1.5 to 2.5 minutes). `-k expose` runs (a) alone with `--profile web` |
| c | `DVT_WEBSTALL1FIX=on DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/join-window.toml ... scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py::test_join_burst_counts_join_window` | 3 to 4 minutes (four offers at once, the generator's retries at +5 s, +10 s, +15 s, abort, two settled reads). Run it ALONE with this snippet: the 200 listener cases pace their joins for the default window of 5 |
| d | `DVT_WEBSTALL1FIX=on DVT_USRP_DYNAMIC=1 ... scripts/run.sh -m rig --profile web,webload -- cases/test_web_load.py` (without the snippet; the burst case then skips with the reason) | the W8 run (about 15 minutes) plus 12 to 24 s per load run for the settled reads before and after, 4 runs: about 17 minutes. The load runs never use `--capture` |

Without `DVT_WEBSTALL1FIX` the same commands run on the current image: a, b and c skip, d records `null` families and the slow requests stay a finding.

### (c) why a window of 1 per second

The web service gates one address before the edge: per address 1/s with a burst of 5, then globally 5/s with a burst of 5. The edge's window is `joins_per_second`
(default and maximum 5, one second, `transport.rs`: `joins.len() >= joins_per_second` after dropping entries older than 1 s). From ONE address at most five offers reach the
edge inside a second, so the default window is never exceeded. `join-window.toml` sets `[edges.webrtc] joins_per_second = 1`; the generator then sends 4 offers at once
(`"burst": true`, all at offset 0): the first is admitted, the others are refused `join_window` at the edge and again at the web, and each retry round (5 s later) admits one
more. Expected: both `join_window` counters rise by at least 3 (6 with the retry rounds), no other reason in either family moves, the generator retried at least 3 offers
(its retry rule sees the refusals as capacity refusals by status and code), `dv_edge_webrtc_join_refused_total{reason="core"}` does not move.

## GAPs

* **More than 5 joins per second at the default window.** Needs two client addresses: five offers from address A at t0, then a sixth from address B 0.2 to 0.9 s later (the
  global bucket has refilled a token, the first five are still inside the edge's 1 s window). The rig has the tester (.30, no WebRTC client), the load generator (.97) and the
  browser (.91); a timed second wave from two of them is not built. The single-address variant above covers the counters and the refusal shape.
* **Reasons not provoked:** `listeners`, `room` (caps of 200 / 100 listeners; `max_listeners` could be lowered by a snippet), `pending`, `retired`, `guests`, `guest_ip` (a
  third guest from one address, `guest.toml`), `nonce_cache`, `slots`, `signalling_busy`, and the web-only `web_records`, `web_sessions`, `web_guest_records`, `web_guests`,
  `web_guest_ip`, `turn_issues` (the TURN case of `docs/websid1-cases.md` spends the budget but needs `--profile nat`). Their exposure (zeros included) is asserted by (a); they
  are not driven. The per-limit strings of these are in `refusals.DETAILS` for a later case.
* **Durable-write semantics** (fail closed / best effort / order) need a disk fault; the deterministic stall needs root (`docs/webstall1-cases.md`, "GAP").

## Shapes I was not sure of (check on the first run)

* That the hub's `/metrics` carries `dv_web_rtc_refused_total` next to the edge families: `dv_web_http_requests_total` is read there today (`docs/web-load-cases.md`), and the
  web service publishes its record through the same fabric telemetry, so the sum over the extra `proc` label is taken (`refusals.by_reason`). If the label values of the two
  families differ from `proc="webrtc"` / `"web"` nothing here depends on them.
* The publish period (assumed about 10 s, as for the edge gauge) and the hub cache (K22 saw up to 35 s): `settled` gives up after 48 s and `poll` after 60 s.
* The room `V` of (b) needs the USRP dynamic hub (`DVT_USRP_DYNAMIC=1`) to be a served module, like the WEBSID1 cases.
* A hand written SDP is not used anywhere: every offer is made by Chromium (b) or aiortc (c, d).
