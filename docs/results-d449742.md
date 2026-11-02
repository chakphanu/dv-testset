<!-- SPDX-License-Identifier: MIT -->
# Results on dv-router:wip-d449742 (Astra security pass) and the WebKit talk bisect

Native launch unless the row says import; no `--capture` for heavy runs. The earlier accepted numbers are in `docs/results-adaptation.md`
(web stages on 648e539 / 3b0724a, Gate B and T14 on 265d818 / 13e7e70). An earlier T13 attempt on wip-55ab7db was ABORTED at 94 percent of S3
(partial: everything passed except the 3 known `test_g3` failures on the reference side) and superseded by this image; nothing else ran on 55ab7db
except the bisect controls below.

## Runs on d449742

| Run | Out dir | Passed / failed / skipped | Verdict |
|-----|---------|---------------------------|---------|
| EDG1 / K1 (`test_web_k22.py -k "silently_closed or closing_only_the_ctl"`) | `20261102T084315Z` | 4 / 0 / 0 | pass: gauge falls 9.3 s (user), 9.5 s (guest at once), 3.7 s (guest after 45 s), 6.6 s (ctl channel closed); guest lease 401 after 29.8 to 30.1 s |
| S3 superset, import launch (`(rig or allstar) and not web and not load`, profile allstar,interlink,nxdn,p25: S1 + S2 + slow + P19 + nxdn / p25 server cases, DMR+ lane) | `20261102T084732Z` | 670 / 3 / 177 (850 selected, 2 h 08 min; SUT peak 147 MiB, 467 pids) | the 3 failures are `test_g3` on the REFERENCE side (xlxd-ref; the DV-Router side passes): `test_g3_presence_replaces`, `test_g3_module_follows_talk` (both also failed in the accepted Gate B) and `test_g3_listen` (new on the reference; its SUT result is correct). Not a SUT bug. DMR+ lane: 39 passed, 1 skipped (the known blacklist case), 0 failed |
| M17 peer cases (T7), native | `20261102T105604Z` | 97 / 0 / 0 | same as Gate A / B |
| Data ident, `DVT_REQUIRE_IDENT=true` | `20261102T110631Z` | 8 / 0 / 0 | same |
| Data ident, `DVT_REQUIRE_IDENT=false` | `20261102T111132Z` | 8 / 0 / 0 | same |
| T14 cross-mode (fake transcoder) | `20261102T111622Z` | 53 / 0 / 0 | same as the adaptation pass |
| Web stages 1+2 (smoke + API) | `20261102T112932Z` | 374 / 0 / 0 | same |
| Web stage 3a (browsers) | `20261102T113449Z` | 54 / 0 / 3 | same |
| Web stage 3b (WebRTC + recorder), run A | `20261102T114421Z` | 53 / 2 / 0 | FAIL, see the bisect: `test_talk_reaches_a_protocol_listener[webkit]`, `test_talk_hold_10_s_stays_on_air_until_release[webkit]` |
| Web stage 3b, run B (rerun) | `20261102T122810Z` | 53 / 2 / 0 | FAIL: `test_talk_hold_10_s_stays_on_air_until_release[webkit]` and the known `test_listen_receives_the_tone[webkit]` (3600 Hz peak, an engine glitch recorded in the case) |
| K22 / K22b (full file) | `20261102T115727Z` | 12 / 1 / 0 | one failure, `test_foreign_and_malformed_circuits`: TEST BUG (below), fixed |
| K22 `test_foreign_and_malformed_circuits` alone, fixed | `20261102T124103Z` | 1 / 0 / 0 | pass (the other login answers 403 `forbidden`) |

NAT with TURN, stage 5, SOAK1 and the isolation modes were NOT run on d449742: the owner asked for a shorter plan and they ran once on the final tag
(`docs/results-be207d3.md`).

### K22 `test_foreign_and_malformed_circuits`: test bug, not a SUT change

The case used the tester login of account N0KB made at the start of the module. The earlier cases log N0KB in three more times through the
browser; an account keeps at most 3 sessions and ends the OLDEST, so that fixture login was dead by the time of the case (`GET /api/v1/user/me`
answered 401; in the final run on be207d3 the diagnostic shows `fixture_login_user_me` 401, `fresh_user_me` 200, 3 sessions of the account, and the
foreign-circuit `alive` 403 `forbidden` with a fresh login). The case now logs in fresh right before the request and records the status of both
logins, the session count and the answer code. The expectation (403) is unchanged.

## WebKit talk failure (stage 3b) and the bisect

Symptom on d449742 in the long stage 3b session: the Playwright WebKit page leaves "On air" about 1.6 s after the press with
"Ended: Transmit permission was revoked." (or "Transmission ended after silence."); Chromium and Firefox pass. The ctl trace of the failing 10 s
hold (page clock ms after On air): `ptt pending` and `granted` at about -16 ms, ONE `lease` sent at +970 ms with no `renewed` ack, then
`{"op":"ptt","state":"ended","code":"revoked"}` from the edge at +1600 ms. The web service side stayed healthy (`/rtc/alive` 204 throughout; the
page had closed its old circuit and sent a new offer 24 ms before the press). The router log has only per-minute room statistics (no revoke line).

| Set | Image | Runs | Result |
|-----|-------|------|--------|
| The two WebKit talk cases alone (6 s and 10 s hold), fresh session | d449742 | 3 (`20261102T122028Z`, `122144Z`, `122259Z`) | 3 of 3 pass |
| the same | 55ab7db (pre-Astra) | 3 (`20261102T122415Z`, `122538Z`, `122654Z`) | 3 of 3 pass |
| whole stage 3b (long shared browser session) | d449742 | 2 (`20261102T114421Z`, `122810Z`) | 2 of 2 FAIL |
| whole stage 3b | 55ab7db | 2 (`20261102T124157Z`, `125451Z`) | 2 of 2 talk-clean (only the known tone glitch once) |
| whole stage 3b, debug log on | wip-edg4a (= be207d3, EDG4) | 2 (`20261102T130802Z`, `132108Z`) | 2 of 2 talk-clean (the second run had 3 Playwright setup errors in recorder / dashboard cases: `Provisional navigation canceled`, `Browser closed`) |
| 30 quick talk cycles (2 s / 3 s alternating, rest 1 s), one WebKit session, debug log on | d449742 | 1 (`20261102T133740Z`) | 30 of 30 pass, 3 min 29 s |
| the same | 55ab7db | 1 (`20261102T134117Z`) | 30 of 30 pass, 3 min 31 s |

So the failure needs the long stage 3b session; it appeared on d449742 in 2 of 2 long runs and on neither 55ab7db nor edg4a / be207d3 in 2 of 2.

Trace hook evidence (new `talk-trace-<engine>.jsonl`, local to `out/`): in passing runs the WebKit page often holds TWO ICE pairs, the nominated one with
traffic and a second pair `succeeded`, not nominated, 0 bytes, on a different local port (55ab7db 10 s and 2 s holds; edg4a 10 s and 2 s holds): the
second STUN-validated pair of DV-Router's EDG4 hypothesis. `dv_edge_webrtc_audio_dropped_total` exists on 55ab7db and grows by about 80 to 290 during
talks; `dv_edge_webrtc_unrouted_total{reason=no_source|unvalidated_source|wrong_peer}` exists on edg4a (6 s hold: no_source 19 -> 25, wrong_peer 10 -> 12,
unvalidated_source 0 -> 0; the 2 s hold changes nothing).

Added for the bisect (additive, no expectation changed): `dump_talk_trace`, `pair_trace`, `edge_counters` in `cases/test_web_webrtc.py` (the three talk cases
take `rig_control`), `cases/test_web_talk_cycles.py` with `dvtest/talk_cycles.py` (`DVT_TALK_CYCLES=1`), `rig/config/dv-router/debug-log.toml`.

## Files changed in this work

`cases/test_web_webrtc.py` (talk trace hook), `cases/test_web_k22.py` (fresh login in the foreign-circuit case, error code helper),
`cases/test_web_talk_cycles.py`, `dvtest/talk_cycles.py`, `cases/unit/test_talk_cycles.py`, `rig/config/dv-router/debug-log.toml`,
`cases/unit/test_sut_config.py`, `docs/rig.md`, `rig/compose.yml` (forwards `DVT_NAT_SHORT`, `DVT_TALK_CYCLES*`, `DVT_SOAK_GA`; `DVT_NAT_EXPIRY_S` now empty by
default), `dvtest/nat_plan.py`, `cases/test_web_nat.py`, `cases/unit/test_nat.py`, `docs/web-nat-cases.md` (short NAT profile), `dvtest/soak.py`,
`cases/test_web_soak.py`, `cases/unit/test_soak.py`, `docs/web-soak-cases.md` (GA soak profile). Unit tests, ruff and `scripts/pii-check.sh` are clean; nothing is committed.
