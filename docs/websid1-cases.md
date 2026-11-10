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
* `rig/compose.yml`: the tester gets `DVT_WEBSID1` (strict switch) and `DVT_WEBSID_OVER_S` (guarded by `cases/unit/test_tester_env.py`).

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

Skip / strict: a probe fixture (`websid1`) logs the operator in with the HTTP client, opens `/api/v1/operator/stream`, steps up and watches 18 s; an image before
WEBSID1 ends the stream with a `bye`, and every case skips with that reason. `DVT_WEBSID1=on` turns the skip into a failure (use it for an image that must have the rule).

## Cases

| Case | Steps | Asserted |
|---|---|---|
| 1 `test_step_up_keeps_sse_circuit_and_wav[engine]` | operator login in the browser (TOTP); Listen circuit connected; operator EventSource; a slow WAV download (about 45 s, started and still under half done); a step-up in a SECOND TAB of the same context | the session cookie and the CSRF token both change; a second context with the OLD cookie gets 401 on `/user/me`; for 20 s (longer than a heartbeat and the 15 s lease) the stream is OPEN with no error and no `bye`, the circuit stays connected (peer and card); the WAV body arrives whole (got == Content-Length, no read error); `/rtc/alive` statuses are recorded; Disconnect with the rotated cookie closes the circuit (`/rtc/close` 204) |
| 2 `test_step_up_does_not_multiply_the_caps[engine]` | eight offer-only circuits kept alive, a ninth; two held WAV bodies (reader paused), a third; TURN credentials until the budget refuses; a step-up; the same again | the ninth circuit and the third body are refused before AND after the step-up (not 200); the held bodies survive; the TURN issue budget (one per minute, burst two) is still refused after the step-up (recorded as not run without TURN) |
| 3 `test_session_end_ends_the_handles[engine-cause]` for logout, revoke, role, disable | login, circuit, stream, a held (paused) and a slow WAV body; the end is triggered from a second tab (logout, `DELETE /user/me/sessions/{id}`) or by the operator API (`PATCH /operator/accounts/{call}` `{role:user}` / `{status:disabled}`) | the stream ends (bye or CLOSED) within one heartbeat + 2 s; the circuit leaves `connected` within the 5 s sweep + 5 s; both WAV bodies are CUT (got < Content-Length or a read error) after the paused reader is released; a body that arrives whole is reported as "fit the buffers" and the case skips |
| 4 `test_denials_coalesce_across_a_step_up` | an operator session of the admin service is refused `GET /config/reload` three times, steps up (the cookie rotates), is refused three more times, waits out 10 s and is refused once more; the admin reads `/audit` | the six denials inside the window make ONE `config.reload.preview` `denied` record; the seventh, after the window, makes a second one that carries `suppressed` >= 1 (an image that keys denials on the cookie writes a record at the rotation and fails) |

## GAPs and shapes not confirmed

* Idle expiry (operator 30 min) and absolute expiry (12 h): no setting shortens them in `config.md` (`web.*` has no session lifetime), so they are not driven.
* TOTP enable, replace and disable ("current handles survive, sibling logins end", review-248 item 1): not written; they need a second authenticator and a sibling login.
* The 100 ms WAV bound and "under 49 KiB buffered": the browser sees the end of a body only after its own buffers drain, so the case shows that the body is CUT, not when; the
  timing is covered by dv-router's own tests (`recordings_review`).
* UNVERIFIED shapes: the refusal status of a ninth circuit and a third WAV body (the documents give none; any non-200 is accepted and recorded), the fields of `GET /user/me/sessions`
  (`current`, `id`; the first entry is used when none is marked), the operator stream's event names (`bye` is documented in the hub, the others are counted when listed), `POST /rtc/offer`
  accepting an unanswered recvonly offer for the cap (`sdp`, `room`), and whether the SPA tab refreshes its CSRF token after a rotation (a stale token fails its `/rtc/alive`; the statuses
  are recorded and the circuit assertion shows the effect).
* Case 3 for the circuit relies on the page seeing the peer end; a page that went to its login screen after a 401 counts as "gone".
