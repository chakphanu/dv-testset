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
| `xlx-peer-tester.toml` | `[[edges.xlx.peer]]` XLX996 at `<TESTER_IP>` on `DE`: the tester plays the XLX peer itself (the xlx kind of E9, the interlink wire); instead of or next to `xlx-peer.toml` |
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
| F11a | `test_f11a_enable_during_an_over_does_not_join_it` | `DVT_FLK11=on`. E is off; a 10 s D-STAR over starts, the `[echo]` edit that turns E on is written 1.5 s in and is applied long before the over ends: the over (its Start arrived while disabled) is NOT replayed; the next over, started after the enable, is replayed once, 0.8 to 2.0 s after its end (echo.md: "turning it on accepts the next StreamStart; an over already on air is not joined"; "a StreamStart stamped disabled remains refused after an enable") |
| F11b | `test_f11b_disable_during_an_over_finishes_it_and_refuses_the_next` | `DVT_FLK11=on`. E is on; the edit that turns E off lands 1.5 s into a 10 s over: that accepted over is replayed once, about 10 s long (+- 1 s); the next over, started after the disable, is not replayed (echo.md: "turning a module off finishes its accepted ... over, then ... refuses new overs") |
| E8 | `test_e8_two_nodes_not_reachable` | skip with the reason below |
| T | `test_zz_telemetry_counts_the_overs` | `dv_echo_overs_total{room, result}` moves by the overs the cases made: `played` within [made, made + unsure], `truncated` exactly, `short` at least (needs `DVT_RIG_CONTROL=1`; the hub refreshes every 10 s, the case polls up to 40 s and then waits one more period) |

## FLK11: admission by arrival order (DV-Router caed3841; cases F11a, F11b)

DV-Router `caed3841` makes the echo admit a StreamStart by whether its module was enabled when the start ARRIVED (the media callback snapshots the room's
admission state from the same configuration watch the loop uses and holds that borrow through the bounded hand-off), not when the paced loop dequeues it;
the loop applies a pending configuration under the same borrow before it dispatches any input, so a disable overtakes queued input and a continuous media
stream never postpones a configuration change or a stop. The unit tests of the commit (`arrival_tests.rs`, `tap_tests.rs`) race the two inputs in one poll.

What a black box can hold, and does (F11a, F11b), both on the second scale and both sides of one change: a Start that arrived disabled stays refused after the
enable, and a Start accepted while enabled is finished after the disable and is the last one admitted. Run with `DVT_FLK11=on` and the E1 to E7 command above
(the cases edit and restore the run's own rendered `dv-router.toml` like E3 / E7; about 2 minutes together; they sit before the telemetry case and add to its tally).

GAP (not a case): the ordering inside ONE loop poll. The watcher polls the file every second and applies it after two equal polls, so the instant of the publication
is not observable from outside, and the race window is the 20 ms of the paced loop: a Start would have to arrive within a few milliseconds of an unobservable
instant, with a probability of about 2 percent per attempt even for a perfect aim. A stress that repeats edit-and-over pairs would need hundreds of 5 s overs per
percent of coverage; the unit tests of the commit are the right tool. A deterministic hook would be the control action `hup xlxd-sut` (SIGHUP applies the file
immediately, `docs/secreload-cases.md`) timed against a Start; its latency is still not known to the millisecond. Not built.

## E9 Echo return integrity, five kinds (`cases/test_echo_dstar_integrity.py`)

Regression case for the defects the owner's production voice test found in the echo RETURN path of a D-STAR
(DExtra) sender, while the forward copy to another DExtra listener is bit exact, now run for five kinds of client
and peer on module E (pytest ids `dextra`, `dplus`, `dcs`, `g3`, `xlx`):

- **A** the echo loses exactly one 20 ms frame in about 2 of 5 echoes (arrival gap 40 ms: a tick that should have
  emitted two units emitted one, "no dropped unit in a catch-up tick");
- **B** the seq byte of the echo stream is wrong (about 95 percent of the frames on every image before the fix;
  the forward copy is 100 percent right); the slow data sync pattern was on the right frames;
- **C** the echo header: the node callsign + module (`N0CALL B` in the voice test) instead of the live rule below.

One run of a kind: a sender S and a listener L link to module E; S sends a header, 330 PRNG AMBE frames (6.6 s,
20 ms from a monotonic clock) and the end packet; L records the forward copy during the over and the echo about 1 s
after it; S records the echo that returns to it. A receive thread per client (`Tap` and its kinds in
`cases/echo_support.py`) stamps every datagram with `time.monotonic()` at recv and keeps the raw bytes. The streams
are split by a hole longer than 0.4 s (`echo.dstar_streams`, streams of fewer than 10 voice packets, a DPlus link
join, are not overs).

| Kind | Sender -> listener | Streams judged | Layout judged (`echo.voice_view`) |
|------|--------------------|----------------|-----------------------------------|
| `dextra` | DExtra -> DExtra | forward copy, echo to L, echo to S | 27 B DSVT; seq byte at 14 |
| `dplus` | DPlus -> DPlus | the same three | 29 B (`1d 80` + DSVT) and the 32 B last packet; seq byte at 14 / 16 |
| `dcs` | DCS -> DCS | the same three | 100 B packet starting `0001`; seq byte at 45 (mod 21, 0x40 last) AND the DCS frame counter (u24 at 58), which must equal the packet position. DCS has no header packet: the first voice packet carries the header fields |
| `g3` | G3 terminal -> DExtra | forward copy and echo to L as a DExtra client hears them; echo to the terminal (`G3Tap`, UDP 40000 of the tester, presence first) | G2 framing = DSVT as DExtra; the G3 voice frame has no counter of its own beyond the seq byte |
| `xlx` | DExtra -> XLX peer | forward copy and echo as the PEER receives them (the sender's own echo is the `dextra` kind) | 27 / 45 / 53 B DSVT (revision of the announced version, default 2.6.1 = 53 B); byte 14 is xlxd's "packet id" and is judged as the seq byte; a revision 2 / 3 frame also carries the DMR packet id (byte 27, 0..5) and sub id (byte 28, 1..3) |

Checks per stream (`echo.integrity`, `kind=`), all kinds:

| Check | Rule |
|-------|------|
| frames | every frame sent is heard, byte for byte (matched by payload, so a lost frame is found, not only counted); none extra |
| gaps | no arrival gap above `GAP_MAX_MS` = 25 ms between consecutive packets. The summary has the largest gap (`max_gap_ms`), the count of gaps above 25 ms (`gaps_over_25ms`) and, separately, the count of gaps of 35 ms or more (`gaps_ge_35ms`, a subset: the dropped unit of a catch-up tick is a 40 ms gap) |
| seq | seq byte of the packet at position n (0-based) = n mod 21, the last packet with 0x40 on top; the share of wrong packets is recorded |
| sync | slow data `55 2d 16` on positions mod 21 == 0 and on no other packet (the last packet is not judged) |
| own counter | `dcs`: frame counter == position. `xlx` (revision 2 / 3): the DMR packet id / sub id advance one step per packet (id 0..5 x sub id 1..3, wrapping) from the first packet's values, and the echo's sequence equals the forward copy's (`echo.aux_vs_forward`: the echo follows the live copy's rule). The last packet is not judged. A frame lost in the middle shows here too, as a receiver would see it |

The forward copy is the control and is judged the same way: if it fails, that is a different defect (the live relay)
and the message says so. Position is counted in the stream as heard, so a lost frame also shows as wrong seq / sync from
there on if the SUT keeps the original counters; the lost-frame count and the gaps tell A from B.

**Header rule (C), all kinds** (`echo.header_failures(sent, forward_header, echo_header)`, from DV-Router): the echo returns
the talker's flags (3 bytes), RPT1, UR, MY and suffix (MY2) exactly as the sender sent them, and its RPT2 is the live
rule, i.e. the same RPT2 the forward (live) copy of the over carries (reflector name + room module, `XRF999 E`), so that
ircDDBGateway-style listeners accept it. `sent` is the header packet the tester put on the wire (`Tap.sent_header`; for
DCS its first voice packet); `echo.header_record` carries `flags` (hex), `rpt1`, `rpt2`, `ur`, `my`, `suffix`. Every
value of every run (sent, forward, echo to L, echo to S) is in the JSON and `echo.txt`.

Needs `echo.toml` (skips without it, like `cases/test_echo.py`). Per-kind skip: a client that does not link or
answer (no such edge in this rig config) skips that kind with the reason; naming the kind in `DVT_ECHO_INTEGRITY_KINDS`
makes it a failure instead (the `dextra` kind always fails). Knobs: `DVT_ECHO_INTEGRITY_RUNS` (10),
`DVT_ECHO_INTEGRITY_FRAMES` (330), `DVT_ECHO_INTEGRITY_JITTER_MS` / `_STALL_PCT` (sender timer errors, default 0),
`DVT_ECHO_IMAGE_TAG` (image name in the JSON, default `XLXD_SUT_IMAGE`), `DVT_ECHO_INTEGRITY_KINDS` (comma list of
`dextra,dplus,dcs,g3,xlx`, default all), `DVT_ECHO_INTEGRITY_XLX_VERSION` (version the tester announces as XLX peer,
default `2.6.1` = revision 3).

```sh
# the four client kinds
DVT_ECHO_INTEGRITY_RUNS=10 DVT_ECHO_INTEGRITY_KINDS=dextra,dplus,dcs,g3 XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter \
  DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/echo.toml scripts/run.sh -m rig -- cases/test_echo_dstar_integrity.py

# the xlx kind, interlink wire (the tester plays the XLX peer XLX996; no xlxd peer container needed, still name the profile)
DVT_ECHO_INTEGRITY_KINDS=xlx XLXD_SUT_IMAGE=<image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/echo.toml:rig/config/dv-router/xlx-peer-tester.toml \
  scripts/run.sh -m rig --profile interlink -- cases/test_echo_dstar_integrity.py

# the xlx kind through the rig's xlxd peer XLX998 (a DExtra client on xlxd-peer-sut judges what the peer re-encodes)
DVT_ECHO_INTEGRITY_KINDS=xlx ... DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/echo.toml:rig/config/dv-router/xlx-peer.toml \
  scripts/run.sh -m rig --profile interlink -- cases/test_echo_dstar_integrity.py
```

`xlx-peer-tester.toml` adds the row `XLX996` at `<TESTER_IP>` on `DE`. The case reads the rendered config: a peer row that
serves module E and names the tester selects the wire mode (the tester links as that peer with `xlx.XlxPeer`, UDP 10002
on the tester, and keeps the link for the whole test); else a row that serves E (the rig's `xlx-peer.toml`) selects
the peer-reflector mode and needs `--profile interlink` (`DVT_INTERLINK=on`); with neither the kind skips and says so.
In the peer-reflector mode xlxd writes the seq byte of its own DExtra output, so the DMR packet id of the interlink wire is
not judged there. With both snippets the tester row wins. The `g3` kind needs the SUT's G3 edge (UDP 12346 / 40000); the
forward copy of a G3 over is judged as a DExtra listener hears it (no second G3 terminal is used, so the relay
`g3-term2` is not needed).

About 3 to 4 minutes per kind for 10 runs (6.6 s over + 1 s delay + 6.6 s echo + 3 s quiet + link and unlink). The case
writes `out/<run>/echo-integrity.json` (one dict per kind, merged over the tests of the run) and prints one line per
kind before it asserts, so a failing image still gives its numbers: `kind`, the image tag, `runs`, `note`, and for each of
`forward`, `echo_listener`, `echo_sender` (not for `xlx`): `echoes_with_lost_frame`, `lost_frames_total`,
`share_of_frames_with_wrong_seq`, `max_gap_ms`, `gaps_over_25ms`, `gaps_ge_35ms`, `sync_errors`, `aux_name` /
`aux_errors` / `aux_vs_forward_errors`, plus `extra_frames_total`, `missing_end_bit`, `empty_streams`, `header_failures`,
the headers of every run and the per-run `integrity` dicts (first wrong seq bytes, gap positions). Assertions at the end:
zero lost frames, zero wrong seq, no gap above 25 ms (so none of 35 ms or more), no sync error, an end bit on the last
packet, the own counter, and the header rule, for the forward copy and every echo return. Pure judges and unit tests:
`dvtest/echo.py` / `cases/unit/test_echo_lib.py`. The judges were exercised with a throwaway localhost UDP fake of the
five edges (clean, and with a dropped unit, a random seq byte, a wrong DCS counter, a wrong DMR id and a wrong echo MY);
no run against a real DV-Router image has been made for the four new kinds yet.

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

## Results (dv-router:wip-c165c93): all reachable cases pass (full run `20261104T051933Z` 28/2/2, targeted runs `20261104T052813Z`, `20261104T053017Z`, `20261104T053126Z`, final revalidation `20261104T054135Z` 7/0/0)

| Case | Result | Measured |
|------|--------|----------|
| E1a | PASS x4 | replay starts 1.00 to 1.01 s after the last packet (D-STAR, DMR, M17, YSF) |
| E1b | PASS x4 | talker callsign / DMR id kept; D-STAR header present at the listener and the talker |
| E1c | PASS D-STAR, DMR, M17, YSF (FE2 closed as ruled) | D-STAR 150/150 frames identical, 2.98 s; DMR 150/150, 2.94 s; M17 the 150 audio frames identical + 2 EOT frames (N2, accepted by dv-router: foreign-origin stream). YSF: all bits but the gain index b2 equal and b2 = M04[P10[b2]] for 150/150 frames, 155 frames in all (see below) |
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

**YSF replay (FE2, closed as ruled by dv-router 2026-11-04, report `debug-ysf-echo-egress.md`).** Packet structure of live relay and replay alike: a header
packet (`fi=0` HC, `dt=2`, `ft=7`, counter 0), voice packets (`fi=1`, `dt=2`, `ft=6`, `fn` cycling 0,1,2, counters 1..), a terminator (`fi=2`, EOT bit
set). The replay has one extra trailing voice packet (the terminator's zero unit plus 4 zero pads: 30 voice packets sent, 31 in the replay, 155 frames
for 150 sent; its first frame `202222200000000000`). The audio frames differ from the sent ones in the gain index b2 only: the YSF ingress steps b2 by
+1.0 dB (xlxd `B2_MAP_PLUS_1_0_DB`) and the YSF cross-mode egress by -0.4 dB (nearest-entry fallback map), the replay (a foreign-origin stream) passes
both, they do not cancel and the +1.0 dB map is not injective, so no egress can restore the sent frames. Recorded pairs (sent -> replay, b2): `8e38664559c199e31f`
-> `ce5a01431b895da393` (12 -> 15), `4b45860b222b9af23c` -> `6f63a30902e3debeb4` (2 -> 5), `b0ef64d2065cf7c780` -> `94cb63d022b4f38f0c` (2 -> 5); they
are pinned in `cases/unit/test_crossaudio.py` with the maps of `dvtest/crossaudio.py` (P10, M04; ambe2.rs of DV-Router). The case asserts: every bit but b2
equal and b2 = M04[P10[b2]] for the 150 sent frames, 5 frames after them; it does not require replay == live relay (the live YSF -> YSF relay is the raw fast
path and byte exact).

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
| FE2 | E1c YSF (closed) | not a bug: the two b2 gain steps (+1.0 dB ingress, -0.4 dB egress), all other bits unchanged; the case asserts exactly that. |

## Related: real-transcoder cross-mode audio into YSF (`cases/test_crossmode_real_ysf.py`)

T14 (`test_crossmode_exact.py`) uses the fake AMBEd and cannot tell whether real cross-mode audio into YSF is garbled. This case keys a 3 s,
700 Hz tone (encoded by transcoder-ref into the talker's codec) as a DMR, D-STAR or M17 talker on module H, with a YSF listener linked, and
judges the YSF listener's frames: bytes against the expected frames (DMR -> YSF is the same codec: the sent frames through the VCH coder once;
the others: transcoder-ref's output for the same input) and the decoded audio (dominant frequency +- 80 Hz, tone share of the spectrum, level
against the expected frames). Skips when `DVT_TRANSCODER_FAKE=1` or transcoder-ref is not given. Helpers and unit tests: `dvtest/crossaudio.py`,
`cases/unit/test_crossaudio.py`. Command: `... scripts/run.sh -m rig -- cases/test_crossmode_real_ysf.py` (about 80 s).

Result on wip-c165c93 (final `20261104T054135Z`, `crossmode-real-ysf.txt`, 3 passed): the audio is NOT garbled. A steady tone encodes to one repeated frame
by itself (the expected frames repeat the same way). The YSF egress changes only the gain index b2 by -0.4 dB (xlxd parity, dv-router ruling 2026-11-04);
with that step applied to the expected frames all three sources match 150/150 frames:

| Source | Dominant | Level (rms heard / expected) | Frames |
|--------|----------|------------------------------|--------|
| DMR | 690 Hz (as expected) | 5145 / 5145 | sent `fcd98eea5eb962c404` (b2 14) -> heard `9eb9e8ce38f1a2c488` (b2 12), frame 0 `954be6500310b00777` unchanged; 150/150 equal to the expected |
| D-STAR | 690 Hz (as expected) | 4740 / 4740 | transcoder-ref `53ea845598942d6e4d` (28 -> 27) and `9eb9e8ce38f1a3c488` (12 -> 10); heard `57cc8353bc7c6966c1`, `9a9fccc81819a78804`; 150/150 |
| M17 | 700 Hz | 73 / 74 | transcoder-ref `3d02234572389de7cb` (3 -> 1), `dba0613598fe41cd83` (31 -> 30); heard 155 frames (pad packet), 150/150 equal |

M17 -> YSF is quiet (rms 73 against about 5000 for DMR and D-STAR, -23 dB), and it is the transcoder, not DV-Router: the same M17 tone through the
reference reflector xlxd-ref with the same real transcoder gives rms 73 as well (SUT 73, reference reflector 73, transcoder-ref direct 74).
The case compares the SUT's level with the reference reflector (within 3 dB) and records the numbers.

## ECHO1f check (wip-5d72b94 on): joins are silent

`DVT_ECHO_JOIN_SILENT=1` turns the YSF / DMR join findings into assertions: E7 requires `result=short` == the short overs the cases made (a header +
terminator join pair no longer counts); `test_e9y_link_only_probe` requires the link of two clients to leave `result=short` unchanged and, for YSF,
`dv_echo_frames_total` in / out unchanged, and a 150 frame YSF over to move in and out equally (end unit kept) with `played` +1 and `short` +0.
`test_e2_short_over_is_not_replayed` now also runs for YSF (a real 25 frame over still counts short). Images before wip-5d72b94 fail these on purpose.
