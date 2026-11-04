<!-- SPDX-License-Identifier: MIT -->
# DV-Router built-in echo (ECHO1, ECHO1c): black-box cases

DV-Router's `dv-svc-echo` records a local over on a configured module and replays it once, `delay_ms`
after the end, as an ordinary over of the same source profile (design doc `docs/design/echo.md` of
DV-Router; read from the checkout at `~/tmp/dvr/codex-c220`, the version with the ECHO1c shutdown rules).
The cases here look at it only from outside: UDP clients on module E, the hub metrics through the control
channel (`metrics xlxd-sut`) and the rendered `dv-router.toml` of the run. File: `cases/test_echo.py`
(helpers `cases/echo_support.py`, pure part and unit tests `dvtest/echo.py` / `cases/unit/test_echo_lib.py`).

## Run it

```sh
XLXD_SUT_IMAGE=dv-router:wip-c165c93 DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG=<template path> \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/echo.toml:rig/config/dv-router/echo-usrp-peer.toml:rig/config/dv-router/echo-peers.toml:rig/config/dv-router/xlx-peer.toml \
  scripts/run.sh -m rig --profile interlink -- cases/test_echo.py
```

About 8 to 10 minutes, no `--capture`. `echo.toml` alone is enough for E1 to E4, E6, E7 and the USRP part of E5;
`xlx-peer.toml` with `--profile interlink` adds the XLX peer part of E5, `echo-peers.toml` the M17 peer part
(it switches echo on for module M too, the module the rig's M17 peers share with the SUT). A part whose
prerequisite is missing skips with the reason. Without `echo.toml` the whole file skips ("no [echo] table in the
rendered config"); with it but with no replay at all the fixture fails ("does not replay within 60 s").

Snippets (`rig/config/dv-router/`, merged key by key after the template; unit-tested in
`cases/unit/test_echo_lib.py`):

| File | Adds |
|------|------|
| `echo.toml` | `[echo] modules = { E = true }`; `max_seconds` (30) and `delay_ms` (1000) are left out so the defaults are what is tested |
| `echo-short.toml` | `[echo] max_seconds = 5` (after `echo.toml`): E3 then needs no live edit |
| `echo-off.toml` | `[echo] modules = { E = false }` (after `echo.toml`): the service runs, module E accepts nothing |
| `echo-peers.toml` | `[echo] modules = { E = true, M = true }` (after `echo.toml`): echo on module M for the M17 peer part of E5 |
| `echo-usrp-peer.toml` | `[[edges.usrp.endpoint]]` module E, peer `<TESTER_IP>:32003`, bind `<OWN_IP>:34003`: a STATIC USRP peer (origin class 2) for the negative case of E5b |

## Editing the live config (E3, E7)

The rendered file `out/<run>/xlxd-config/xlxd-sut/dv-router.toml` is bind-mounted read-only into the SUT
container, and the tester sees the same file under `$DVT_OUT/xlxd-config/xlxd-sut/`. The cases edit it in place
(`EchoCfg.apply`: the new bytes are written over the old and the file is cut to the new length, so the inode
stays the one the bind mount holds and the file is never empty) and restore the original text in a `finally`.
The launcher's watcher polls the file every second and applies it after two equal polls (design `config.md`,
"Reload"); the cases wait `echo.reload_wait()` = 4 s. When the file is not visible or not writable the cases
that need it skip with "not reachable: ..." (E3 then needs `echo-short.toml`). Only this run's own file is
touched; no docker object, no other run's `out/`.

## Cases

Four protocols are used where a case says "per protocol": D-STAR (DExtra), DMR (MMDVM), M17 and YSF, each as a
talker and a second client of the same kind on module E (both timed by a drain thread; `Scene` in
`cases/echo_support.py`). Over of 150 frames = 3 s; every over carries PRNG frames
(`dvtest.echo.make_frames`; for YSF they pass the VCH coder once, an arbitrary 9-byte frame is not a codeword).

| Case | Test | Asserts |
|------|------|---------|
| E1a | `test_e1a_one_replay_after_the_delay` [dstar dmr m17 ysf] | exactly one replay over reaches the listener; it starts 0.8 to 2.0 s after the last packet of the over (spec: about 1.0 s; the value is recorded) |
| E1b | `test_e1b_talker_identity_is_kept` | the replay names the talker (callsign / DMR id) as the talker is, and as the live relay does; D-STAR: a header is synthesized before the voice |
| E1c | `test_e1c_payload_is_byte_identical_and_about_3_s` | every frame sent is in the replay, in order, byte for byte, at its start (the audio frames the talker sent; the live relay is judged the same way and a difference there is only a finding); length 2.5 to 3.6 s; the stream ends. Frames the protocol adds (M17 EOT repeat, pads) are findings, not failures |
| E1d | `test_e1d_talker_hears_the_replay_too` | the talker's own client hears one replay, the same payload and name as the other listener (D-STAR header included) |
| E2 | `test_e2_short_over_is_not_replayed` [dstar dmr] | an over of 25 frames (0.5 s) gets no replay within 5.5 s, at the listener and at the talker |
| E3 | `test_e3_long_over_is_truncated` | with `max_seconds` = 5 (live edit, or `echo-short.toml`) an 8 s over (D-STAR and DMR) is replayed once, 5 s long (+- 0.5), at most `max_seconds * 50` frames and at least 3 fewer than that (whole bundles), the retained prefix identical to what was sent |
| E4 | `test_e4_no_storm_after_the_replay` | after the replay nothing more reaches the listener or the talker for 3 x `delay_ms`: the replay is not recorded again |
| E5a | `test_e5a_xlx_peer_over_is_not_replayed` | an over keyed on the XLX peer reflector (origin class 1) reaches the SUT's listener once, byte for byte, and is not replayed (needs `--profile interlink` and `xlx-peer.toml`) |
| E1e | `test_e1e_dynamic_usrp_client_over_is_replayed` | clients of the dynamic USRP hub are local (class 0, dv-router ruling 2026-11-04): a 150 packet over is heard, then replayed once 0.8 to 2.0 s later, 150 packets, PCM identical |
| E5b | `test_e5b_static_usrp_peer_over_is_not_replayed` | an over from a STATIC USRP peer row (class 2, `echo-usrp-peer.toml`) is heard once by a dynamic USRP listener, is not replayed, and nothing comes back to the peer |
| E5c | `test_e5c_m17_peer_over_is_not_replayed` | control: a local M17 over on M is replayed; an over from the M17 peer M17-DVT on M (class 2) reaches a local listener once and is not replayed (needs `echo-peers.toml`) |
| E5d | `test_e5d_nxdn_p25_not_reachable` | skip with the reason below |
| E6 | `test_e6_floor_is_busy_during_the_replay` | a second talker keying 0.7 s into the replay gets nothing relayed (not one of its 60 frames), the replay itself is undisturbed (payload, length), and a human over 1.5 s after the replay is relayed in full |
| E7a | `test_e7a_toggle_off_and_on` | `modules = { E = false }`: after the reload wait the next over is not replayed; back to `true`: the next over is replayed once, in the delay window |
| E7b | `test_e7b_row_removed_while_playing` | `modules = {}` while a 4 s replay plays: the replay finishes or is cut and ends in an end of stream; a human over 2 s after the replay's last packet is relayed in full (the floor is free) and is not replayed; then the row is restored and a replay is seen again (the child respawns) |
| E8 | `test_e8_two_nodes_not_reachable` | skip with the reason below |
| T | `test_zz_telemetry_counts_the_overs` | `dv_echo_overs_total{room, result}` moves by the overs the cases made: `played` within [made, made + unsure], `truncated` exactly, `short` at least (needs `DVT_RIG_CONTROL=1`; the hub refreshes every 10 s, the case polls up to 40 s and then waits one more period) |

## What could not be reached, and why

- **E5, NXDN and P25 peers.** The rig template has no NXDN or P25 peer rows, and the native launch ignores `DVT_NXDN`
  and `DVT_P25` (`docs/rig.md`, native launch). `soak-edges.toml` gives the SUT its own NXDN and P25 client ports,
  but the design does not say which origin class those clients have, so an over from them would not be a
  test of "class 2 is not replayed". Not faked; a peer row snippet for the rig's `nxdn` / `p25` profile would
  close it.
- **E5, WebRTC talk (class 0, must be replayed).** Needs `--profile web,browser` and a Talk case; not in this file.
- **Origin protocol `echo` (edge 19) of the replay.** The UDP clients see a normal stream; the origin shows only in
  the hub events (protocol `echo`) of the web profile. Not asserted here.
- **E8 (two XLX-linked nodes, both echo on E).** The rig has one DV-Router node; `xlxd-peer-sut` is an xlxd (no echo).
- **E7, latency lower bound.** The cases wait 4 s after an edit; the design says "about two seconds", and the
  watcher's own arithmetic (1 s polls, two equal polls) is 2 to 3 s. The time to the first effect is not measured.
- Validation of out-of-range values (`max_seconds` 5..120, `delay_ms` 200..10000) and of an unknown module row is not covered.
- The bridge ping-pong of the design (a bridge to an external parrot) needs a bridge; not built.

## Results (dv-router:wip-c165c93): 29 passed, 1 failed (full run `20261104T051933Z` plus targeted runs `20261104T052813Z`, `20261104T053017Z`, `20261104T053126Z`)

| Case | Result | Measured |
|------|--------|----------|
| E1a | PASS x4 | replay starts 1.00 to 1.01 s after the last packet (D-STAR, DMR, M17, YSF) |
| E1b | PASS x4 | talker callsign / DMR id kept; D-STAR header present at the listener and the talker |
| E1c | PASS D-STAR, DMR, M17; **FAIL YSF** (FE2, open) | D-STAR 150/150 frames identical, 2.98 s; DMR 150/150, 2.94 s; M17 the 150 audio frames identical + 2 EOT frames (N2, accepted by dv-router: foreign-origin stream). YSF: see below |
| E1d | PASS x4 | the talker's own client hears the same replay |
| E1e | PASS | dynamic-hub USRP client: runs of 150 + 150 voice packets, replay 1.01 s after the over, PCM identical |
| E2 | PASS (D-STAR, DMR) | 25 frames: no replay in 5.5 s |
| E3 | PASS (D-STAR, DMR) | max_seconds 5 (live edit): D-STAR 4.98 s / 250 frames, DMR 4.92 s / 249 frames (83 bundles, as the design says); `truncated` +2 |
| E4 | PASS x4 | exactly one replay, nothing for 3 x delay_ms after it |
| E5a | PASS | over from the XLX peer XLX998 heard once, not replayed |
| E5b | PASS | static USRP peer over heard once by the dynamic listener (150 packets), not replayed, nothing sent back to the peer (the first version used a dynamic client, which is class 0: FE1 closed, intended) |
| E5c | PASS | M17 peer over heard once, not replayed; control: the local M17 over on M is replayed |
| E5d | not reachable | no NXDN / P25 peer rows in the native rig |
| E6 | PASS | none of the 60 frames keyed 0.7 s into the replay relayed; replay intact (payload, length); a human over 1.5 s after the replay relayed in full |
| E7a | PASS | `modules = { E = false }`: no replay; back to true: replay at 1.0 s (4 s waited after each edit) |
| E7b | PASS | `modules = {}` 0.5 s into a 4 s replay: replay cut at 2.46 s (124 of 200 frames) with an end of stream, a human over 2 s later relayed and not replayed; launcher log "stopping dv-svc-echo" then "starting dv-svc-echo" after the row came back, replay works again |
| E8 | not reachable | one DV-Router node in the rig |
| T | PASS | `played` +9 for the 9 overs made in the full run (the later 2-over run: +2 exactly), `truncated` +2 (2 made), `short` +4 for 2 deliberate short overs (the other 2 are DMR / YSF link joins: dv-router says counting a zero-voice stream as short is not intended, a follow-up will fix it; informational) |

**YSF replay (FE2).** The FICH of the packets, live relay and replay alike: a header packet (`fi=0` HC, `dt=2`, `ft=7`, counter 0), voice packets
(`fi=1`, `dt=2`, `ft=6`, `fn` cycling 0,1,2, counters 1..), a terminator (`fi=2`, EOT bit set). There is no extra leading voice packet: the
replay has one extra trailing voice packet (30 voice packets sent, 31 in the replay; the last one starts with the pad frame `202222200000000000`),
so 155 frames for 150 sent. The audio frames themselves differ: the live relay equals the sent frames, the replay does not (0 of 150 equal at the
same position, the sent frames are at no offset of the replay). PRNG frames (`20261104T051933Z`, `20261104T052813Z`): sent / live frame 0
`8e38664559c199e31f`, frame 1 `f20efac02aa36777ab`, frame 2 `70084b810b8b1abdd7`; replay frame 0 `ce5a01431b895da393`, frame 1
`db53ba3f7eee9c5254`, frame 2 `ca077d7d2d46f9d354`. A tone encoded by transcoder-ref (valid AMBE+2, passed once through the YSF VCH coder,
`20261104T053126Z`): sent / live frame 0 `4b45860b222b9af23c`, replay frame 0 `6f63a30902e3debeb4`, then `94cb63d022b4f38f0c` repeated. D-STAR and
DMR replays of the same kind of over are bit-identical. Reported to dv-router; the assertion stays.

## Findings

Lines of the form `[FINDING] case: text` go to `out/<run>/echo.txt` and to the pytest output. Not asserted because the
design leaves the point open:

| ID | Point | Where |
|----|-------|-------|
| N1 | Room label of `dv_echo_overs_total`: the design says the lower case module letter (`room="e"`), the brief said `room="E"`. The cases compare without regard to case and record the spelling. | T |
| N2 | Frames the replay carries beyond the frames sent (end pad, EOT repeat): not specified; the count and whether they are silent is recorded. | E1c |
| N3 | A replay cut by `modules = {}`: complete or closed, both accepted by the spec; the case records which, and asserts that a cut replay ends in an end of stream. | E7b |
| N4 | `result=short` also moving for link joins of DMR and YSF (empty streams): +4 for 2 short overs made; recorded, not asserted. | T |
| N5 | Counters of `dv_echo_overs_total` restart from zero when `dv-svc-echo` is stopped by removing the last row and respawned: the telemetry case runs before E7b. | T |

Failures that are the SUT's (assertion unchanged, reported to dv-router):

| ID | Case | Observed |
|----|------|----------|
| FE1 | E5b (closed) | a USRP over of a dynamic-hub client is replayed: INTENDED (dynamic-hub clients are `SessionKind::Client`, class 0; static `[[edges.usrp.endpoint]]` rows are peers, class 2). E5b now uses a static peer row; the dynamic client is the positive case E1e. |
| FE2 | E1c YSF (open) | the replay of a YSF over does not carry the sent audio frames (155 frames for 150, none equal); bytes above. |

## Related: real-transcoder cross-mode audio into YSF (`cases/test_crossmode_real_ysf.py`)

T14 (`test_crossmode_exact.py`) uses the fake AMBEd and cannot tell whether real cross-mode audio into YSF is garbled. This case keys a 3 s,
700 Hz tone (encoded by transcoder-ref into the talker's codec) as a DMR, D-STAR or M17 talker on module H, with a YSF listener linked, and
judges the YSF listener's frames: bytes against the expected frames (DMR -> YSF is the same codec: the sent frames through the VCH coder once;
the others: transcoder-ref's output for the same input) and the decoded audio (dominant frequency +- 80 Hz, tone share of the spectrum, level
against the expected frames). Skips when `DVT_TRANSCODER_FAKE=1` or transcoder-ref is not given. Helpers and unit tests: `dvtest/crossaudio.py`,
`cases/unit/test_crossaudio.py`. Command: `... scripts/run.sh -m rig -- cases/test_crossmode_real_ysf.py` (about 80 s).

Result on wip-c165c93 (`20261104T053737Z`, `crossmode-real-ysf.txt`): the audio is NOT garbled, a steady tone encodes to one repeated frame by itself
(that is not a fault: the expected frames repeat the same way).

| Source | Dominant | Level (rms heard / expected) | Frames |
|--------|----------|------------------------------|--------|
| DMR | 690 Hz (as expected) | 5145 / 6058 (-1.4 dB) | frame 0 `954be6500310b00777` unchanged, frames 1..149 `9eb9e8ce38f1a2c488` where the sent (and expected) frame is `fcd98eea5eb962c404`: the same codec, but the YSF egress replaces the frames; assertion "heard == sent" fails |
| D-STAR | 690 Hz (as expected) | 4740 / 5584 (-1.4 dB) | `57cc8353bc7c6966c1`, then `9a9fccc81819a78804` x149; transcoder-ref gives `53ea845598942d6e4d`, `9eb9e8ce38f1a3c488`: audio equal, bytes not (informational); PASS |
| M17 | 700 Hz | 73 / 441 (15.6 dB down) | 155 frames heard (pad packet); `dba0603598fe41cd83` vs expected `dba0613598fe41cd83`, then `7f404507705419a347` vs `3d02234572389de7cb`; level check fails |

FE2 (echo YSF replay) is the same behaviour: the replay frames after frame 0 are the re-encoded steady-state codeword; the live YSF -> YSF relay is the
raw fast path and byte exact.
