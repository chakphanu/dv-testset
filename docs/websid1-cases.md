<!-- SPDX-License-Identifier: MIT -->
# DV-Router WEBSID1: sessions, streams, RTC leases and WAV transfers across a step-up (browser cases)

WRITTEN OFFLINE, NOT RUN on a rig. For the image after wip-7bd0082 (WEBSID1 is DV-Router merge `a1ff890c`, not in the earlier candidate). Sources:
DV-Router `web.md` ("Built (WEBSID1)", the route table), `openapi-web.yaml`, `admin.md` and review-248 ("dv-testset browser case to request").

## What is specified

A step-up, TOTP enrolment, replacement or disable ROTATES the cookie and the CSRF token of the same login session. The old cookie answers 401 at
once; new requests need the current cookie and writes the current CSRF token. Open operator SSE streams, RTC / talk ownership leases and WAV transfers
follow the stable server-side `SessionKey` (never sent to or accepted from a client), so they survive a rotation; the caps (eight circuits per login,
two WAV bodies, the TURN issue budget) count per login across rotations. Logout, own-session revoke, idle or absolute expiry, account disable and role
change end those handles: the operator stream within one heartbeat (15 s), RTC within the 5 s sweep, a WAV body within 100 ms (under 49 KiB still
buffered, also with a paused reader). An IDS / reload role denial is coalesced per session, route and 10 s across a step-up; the next record carries
`suppressed`.

## Files

* `cases/test_web_session_rotation.py`: the cases below; pure verdicts and the JavaScript run in the page: `cases/websid_support.py`; unit tests
  `cases/unit/test_websid_support.py` (verdicts) and `cases/unit/test_web_session_rotation_case.py` (case 4 against a fake admin service in both
  keying models, helpers).
* `rig/compose.yml`: the tester gets `DVT_WEBSID1` (strict switch), `DVT_WEBSID1B` (strict switch of the WEBSID1b branch) and `DVT_WEBSID_OVER_S` (guarded by `cases/unit/test_tester_env.py`).

## Run it (about 20 minutes; the long over alone is 150 s)

```sh
DVT_USRP_DYNAMIC=1 DVT_WEBSID1=on \
XLXD_SUT_IMAGE=<dv-router image after wip-7bd0082> DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> \
DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/web.toml:rig/config/dv-router/admin.toml:rig/config/dv-router/totp-required.toml:rig/config/dv-router/recorder.toml \
  scripts/run.sh -m rig --profile web,browser -- cases/test_web_session_rotation.py
```

Case 2's TURN part needs `--profile web,browser,nat` with `turn.toml` (otherwise `GET /rtc/ice` is 404 and that part is recorded as not run). The admin service
(case 4) needs `admin.toml`. Rooms: the Listen circuits and the long over use module `V`. Runtime: the probe 25 s, the long over 150 s once, case 1 about 100 s
per engine, case 2 about 60 s per engine, case 3 four causes of about 60 s per engine, case 4 about 40 s: about 20 minutes for the three engines.
`DVT_WEBSID_OVER_S` shortens or lengthens the over (a body that fits the buffers on the way cannot be shown cut: case 3 then skips and says so).

FINDING WEBSID1b (dv-router, unit WEBSID1b): the CSRF token rotates with a step-up but reaches a page only in JSON replies. The OTHER tab therefore gets 403 `csrf` on its next
write, and its RTC keepalive (`POST /rtc/alive`) takes any 401 / 403 for lost ownership and drops the circuit ("Connection ownership expired. Connect again manually.") although the
server lease is alive. On a WEBSID1 image without WEBSID1b case 1 sees no SSE `bye` and a complete WAV, but tab 1's circuit ends at its next keepalive: the case records that as
`web.finding("WEBSID1b: ...")` and a RECORD line, keeps asserting everything else (including that the rotated cookie renews and closes the original circuit from a tab with the current
token), and passes. `DVT_WEBSID1B=on` (an image with the fix) makes the circuit staying up past two keepalive periods (32 s) an assertion. A step-up in the SAME tab is fine and is the
positive control `test_step_up_in_the_same_tab_keeps_the_circuit`.

Skip / strict: a probe fixture (`websid1`) logs the operator in with the HTTP client, opens `/api/v1/operator/stream`, steps up and watches 18 s; an image before
WEBSID1 ends the stream with a `bye`, and every case skips with that reason. `DVT_WEBSID1=on` turns the skip into a failure (use it for an image that must have the rule).

## How a WAV body is held (first rig run, wip-a31709a, `out/20261110T220224Z`)

A WAV body read by a browser `fetch` cannot be held back: the 2.4 MB body of the 150 s over reached the proxy in about 130 ms whatever the page did (the proxy log shows
each body completing in 100 to 160 ms), because the browser's network stack and the autotuned TCP buffers swallow it (Chromium handed the page 2 MiB in ONE chunk at 3 s,
Firefox all of it). The server-side transfer was over and its slot free, so a "third concurrent body" was accepted when the requests did not overlap within that window
(429 only in Chromium's first round, in the proxy log: a refusal in 0 ms next to two bodies completing). That was the harness, not the 2-per-(role, session) cap, and it made all
twelve case 3 runs skip ("fit the buffers"). The cases now read the bodies in the TESTER with `websid_support.RawWav`: a TLS socket with a 4 KiB receive buffer, the cookie and the
User-Agent of the browser login (the session is bound to the UA), a `hold` that reads nothing after the response headers and a `rate` throttle. The unit tests show that a 32 MiB
body is still in flight on the server 0.6 s later behind a held reader.

## Cases

| Case | Steps | Asserted |
|---|---|---|
| 1 `test_step_up_keeps_sse_circuit_and_wav[engine]` | operator login in the browser (TOTP); Listen circuit connected; operator EventSource; a slow WAV download read by the tester with the login's cookie (about 45 s, started and still under half done); a step-up in a SECOND TAB of the same context | the session cookie and the CSRF token both change; a second context with the OLD cookie gets 401 on `/user/me`; the rotated cookie renews the original circuit at once (`POST /rtc/alive` from a tab with the current token: 204); for 20 s (longer than a heartbeat and the 15 s lease) the stream is OPEN with no error and no `bye`; the WAV body arrives whole (got == Content-Length, no read error); the circuit of tab 1 stays connected (a FINDING WEBSID1b when it does not, an assertion with `DVT_WEBSID1B=on`); `/rtc/alive` statuses of the page are recorded; `POST /rtc/close` with the rotated cookie answers 204 |
| 1b `test_step_up_in_the_same_tab_keeps_the_circuit[engine]` | Listen circuit; a step-up in the SAME tab | the circuit stays connected for 25 s (the positive control) |
| 2 `test_step_up_does_not_multiply_the_caps[engine]` | eight offer-only circuits kept alive, a ninth; two held WAV bodies (the tester's reader paused), a third; TURN credentials until the budget refuses; a step-up; the same again | the ninth circuit is refused with 503 `unavailable` (detail "WebRTC capacity reached", Retry-After 5) and the third body with 429 `rate_limited` ("recording rate limit", Retry-After 1, at once) before AND after the step-up (shapes pinned by dv-router from a1ff890c; the cap of 8 counts across rotations); the held bodies survive; the TURN issue budget (one per minute, burst two) is still refused after the step-up (recorded as not run without TURN) |
| 3 `test_session_end_ends_the_handles[engine-cause]` for logout, revoke, role, disable | login, circuit, stream, a held (paused) and a slow WAV body; the end is triggered from a second tab (logout, `DELETE /user/me/sessions/{id}` of the entry marked `current` in `GET /user/me/sessions` = `{total, items:[{id, current, age_s, idle_s}]}`, 200 `{ok:true}`; the revoke also ends that login's circuits) or by the operator API (`PATCH /operator/accounts/{call}` `{role:user}` / `{status:disabled}`) | the stream ends (bye or CLOSED) within one heartbeat + 2 s; the circuit leaves `connected` within the 5 s sweep + 5 s; both WAV bodies are CUT (got < Content-Length or a read error) after the paused reader is released and the throttle removed; a body that arrives whole is reported as "fit the buffers" and the case skips |
| 4 `test_denials_coalesce_across_a_step_up` | an operator session of the admin service is refused `GET /config/reload` three times, steps up (the cookie rotates), is refused three more times, waits out 10 s and is refused once more; the admin reads `/audit` | the six denials inside the window make ONE `config.reload.preview` `denied` record; the seventh, after the window, makes a second one that carries `suppressed` >= 1 (an image that keys denials on the cookie writes a record at the rotation and fails) |

## GAPs and shapes not confirmed

* Idle and absolute expiry: hard-coded (users idle 12 h, absolute 7 d; operators idle 30 min, absolute 12 h; `crates/svc/web/src/auth/mod.rs:72-77`; the 15 min idle / 4 h absolute / at most 3 per account of `web.md` line 623 belong to the ADMIN host sessions of dv-svc-admin, not to dv-svc-web accounts, and are not wrong) and no setting shortens them, so they are not driven.
* TOTP enable, replace and disable ("current handles survive, sibling logins end", review-248 item 1): not written; they need a second authenticator and a sibling login.
* The 100 ms WAV bound and "under 49 KiB buffered": a reader sees the end of a body only after the kernel buffers on the way drain (a few hundred KB even with a 4 KiB receive buffer), so the case shows that the body is CUT, not when;
  the timing is covered by dv-router's own tests (`recordings_review`).
* Other limits on the WAV path, kept out of the way of cases 1 to 3 (a handful of requests per case): 20 audio requests per 10 s, global browse 2 and play 4 slots with a 5 s wait.
* UNVERIFIED shapes: the operator stream's event names (`bye` is documented in the hub, the others are counted when listed), `POST /rtc/offer`
  accepting an unanswered recvonly offer for the cap (`sdp`, `room`).
* Case 3 for the circuit relies on the page seeing the peer end; a page that went to its login screen after a 401 counts as "gone".
