<!-- SPDX-License-Identifier: MIT -->
# DV-Router open-stream registry (LEAK2): black-box cases

Purpose: check, from outside, the node-wide open-stream registry that DV-Router uses to refuse a looped
REMOTE-origin stream (design: `fabric.md` "streamev" and `floor-control.md` "Remote-origin loop detection" of
the DV-Router checkout). A room controller (process `dv-room --rooms <letter>`, one per module) no longer
declares a liveliness token per over. It re-puts the `streamev` open announcement every 1 s while a stream is
open; every other controller drops an entry 5 s after the last refresh it received. A close removes the entry
at once; a killed controller sends none. An RTS for a remote-origin stream (it arrives over an XLX interlink
peer) whose sid is open in another room gets DENY 5: silent, no frame delivered. Accepted by the design: with
four or more refreshes lost a looped stream may be admitted for 0 to 1 s.

Files: `cases/test_stream_registry.py`, judging and timing arithmetic `dvtest/registry.py` (unit tests
`cases/unit/test_registry.py`), the harness addition `kill-proc xlxd-sut dv-room KILL rooms=<LETTER>`
(`scripts/lib/killproc.sh`, `dvtest/control.py`, `docs/rig.md` "Control channel"). SUT only: the file skips
with its reason unless `DVT_SUT_PROFILE=dvrouter`, `DVT_SUT_LAUNCH=native`, the interlink profile is up and the
control channel is on (`run.sh` sets `DVT_RIG_CONTROL=1` in the tester by itself).

## Run it

```sh
XLXD_SUT_IMAGE=dv-router:wip-3075fb3 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native \
  DVT_SUT_CONFIG=<template path> DVT_SUT_GEN=c2465de \
  DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/xlx-peer.toml \
  scripts/run.sh -m rig --profile interlink -- cases/test_stream_registry.py
```

`xlx-peer.toml` is needed: it gives the SUT the XLX998 peer (`xlxd-peer-sut`, modules `DE`) through which the
remote-origin streams arrive; the template alone has no XLX peer and the cases then fail at the link. The
template must serve module `H` (`rooms = "A-Z"` does) and module `H` stays out of `DVT_INTERLINK_MODULES`.
About 2 minutes of traffic plus the links, no `--capture`. Do not run it next to another rig run.
The L3 / L4 raw numbers are written to `out/<run>/stream-registry-l3.txt` and `stream-registry-l4.txt`
(the L4 line is also printed with `-s`).

## Cases

Clients: a local holder on `H` (module `HOLD_MODULE` of `cases/test_xlxd_interlink.py`, not interlinked), a
remote-origin talker linked to the XLX peer on the interlinked module (D), and the SUT's listener on D. The
probe is the same sid as the holder, sent through the peer so the SUT sees origin class 1. A frame count is the
D-STAR voice frames the listener received.

| Case | What happens | Pass |
|------|--------------|------|
| L1 | holder over 15 s (sid S); at +1.0 s the remote probe with sid S; at +4.0 s a control with another sid in the same holder over | probe: 0 frames; control: all 60 frames |
| L2 | the same; the probe at +8.0 s (more than 5 s: only the 1 s refresh can keep the entry); control at +11.0 s | probe: 0 frames; control: all 60 frames |
| L3 | holder over 16 s (sid S); at +3.0 s `kill-proc xlxd-sut dv-room KILL rooms=H`; P1 = remote same-sid over at t_kill + 1.5 s; P2 = the same sid again at t_kill + 6.5 s | P1: 0 frames; P2: at least 80 percent of 60 frames |
| L4 | after the kill, a poll (a fresh local over on `H` heard by a second local client on `H`, every few seconds, at most 30 s); then `proc-stat` of all `dv-room` and the launcher before and after | some over heard (the time is printed); exactly one `dv-room` has a new (pid, start time) and one is gone; every other one and the launcher `dv-router` are the same processes |

L3 and L4 are one run (one fixture): the same kill. The probes are on D, so they reach the controller of D,
which holds the entry the dead controller of `H` can no longer refresh.

## Timing arithmetic (L3)

Last refresh before the kill: between 0 and 1 s earlier (refresh period 1 s). Entry expiry: last refresh + 5 s,
so between t_kill + 4.0 s and t_kill + 5.0 s. Admission is decided at the RTS (the header), so what counts is the
header time of the probe: P1 at + 1.5 s has 2.5 s of margin before the earliest expiry (must be refused); P2 at
+ 6.5 s has 1.5 s of margin after the latest (must pass). `registry.check_plan()` states this with 0.5 s margins
and the fixture refuses to run a plan that breaks it. t_kill is the time the helper printed (taken in the
container right after the signals; `KillResult.t_kill_monotonic`); `registry.pick_t_kill` fails the run when it
lies more than 0.5 s outside the control call, i.e. when the container and tester clocks disagree. The offsets
of holder start, kill, both probes and the holder end are in every L3 failure text.

## Reading a failure

- L1 / L2 probe heard frames: the looped stream was admitted. L2 only: the refresh did not keep the entry (or the
  expiry is shorter than 5 s). Frames in the 0 to 1 s range are the accepted "4+ refreshes lost" case: rerun once.
- L1 / L2 control missing frames: the path itself (peer, interlink) is broken, not the registry.
- L3 P1 heard frames: the entry died too early after the kill. L3 P2 below 80 percent with P1 silent: the entry
  did not expire in 5 s; three other causes are possible and the failure text names them: the restarted
  controller re-admitted the holder's continuing over (sid S) and refreshes again (look at the L4 poll: a first
  success only after the holder ended at about t_kill + 13 s points at this), the XLX peer (xlxd) dropped the
  repeated sid from the same talker, or a probe was lost on the interlink (control: L1).
- L4 poll never heard: the restarted controller serves nobody. The failure says whether a freshly linked pair
  worked (the pre-linked clients lost their membership with the controller) or not.
- L4 "exactly 1 gone / 1 new": another controller restarted, the launcher respawned twice, or none came back.

## Assumptions not verified offline

The launcher starts each controller as `dv-room --rooms <key>` (two argv words; `crates/router/src/children.rs`
`room_specs`); the key is the display letter of the module by default. The host helper also accepts
`--rooms=<L>` and a comma list, so a controller that owns several rooms matches too. The case was written
without running a rig.

## First runs on wip-3075fb3 (what was learned)

- L1, L2 and L4 passed at the first run. L3 as first written (P1 at kill+1.5 s asserted silent, a 16 s holder over that keeps sending)
  failed on P2: the loop stayed refused through kill+6.5 s because the over was still being sent to the restarted controller.
- With a 3.8 s holder over (it ends 0.5 s after the kill, the terminator reaches the restarted controller, which starts 500 ms after the
  kill) the looped stream was already admitted at kill+1.5 s (60 of 60 frames): the entry was cleaned up earlier than the 5 s expiry. A black-box
  case cannot tell the 5 s expiry from that clean-up because the edge always reaches the restarted controller (terminator or the 1.6 s silence
  timer), so P1 is recorded (`stream-registry-l3.txt`, `p1`) and only the bound is asserted: P2 at kill+6.5 s must be admitted.
- L4: the first fresh local over that was served came 3.0 s after the kill (the attempt at +0.2 s reached nobody: 1 of 60 frames).
