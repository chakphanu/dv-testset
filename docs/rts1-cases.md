<!-- SPDX-License-Identifier: MIT -->
# RTS1 cases: DENY 12 (no winner) in the winner gap, link-down RTS counted

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`, unit RTS1 = `b64733cd`). **Nothing here has run on a rig.** Case file `cases/test_rts1.py`, pure helpers
`dvtest/rts1.py`, unit tests `cases/unit/test_rts1_lib.py`, switch `DVT_RTS1` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`). Notes: stdout and
`out/<run>/rts1.txt` (`RECORD`, `FINDING`).

**Gate.** `DVT_RTS1=on`; without it every case skips with the reason.

## What RTS1 is (read from the sources)

Document: `docs/design/floor-control.md` ("DENY reasons": 12 no current winner; the edge state machine: CTS DENY, CTS timeout or no controller drop the rest of the over, no protocol
answer), `status.md` (`dv_room_rts_total`, `dv_edge_drops_total`). RTS (160 B) and CTS (40 B) are fabric-internal: a query and reply on key `rts` between an edge and the room controller;
no legacy client protocol (DExtra, DPlus, DCS, YSF, DMR, M17, ...) carries them. The "winner gap" is the instant a connected, non-fenced room controller sees the highest live controller token
of its room as `None` or as another id (token propagation is 0.2 ms p50, 2.3 ms max, so the gap is a few ms): the RTS is answered at once with CTS DENY, reason 12, `grant_id` 0 (a fenced but
connected room still answers 9). The edge drops the over (counter `deny_no_winner`, no Abort, no re-RTS). "An old client reads it as busy" means an EDGE BUILT BEFORE RTS1 (it maps the unknown
reason to Busy and counts `deny_busy`); a WebRTC talker reads 12 as the status `absent`. A room whose router link is down gets no CTS: the waiting RTS are counted
`dv_room_rts_total{result="link_down"}`.

## What is asserted

| Case | Statement | Assertion |
|------|-----------|-----------|
| RTS1-a | the image has RTS1 and the series follow status.md | the HELP of `dv_room_rts_total` is `RTS processing, by result` (it was `RTS answered, by result`); for every room label the five fixed results (grant, deny, busy_delayed, duplicate, clock_domain) are present; `no_winner` never exceeds `deny` (it counts inside it), nothing negative; the edge series `deny_no_winner` is recorded (a FINDING when no edge exports it before a first count) |
| RTS1-b | a healthy room has no gap | one long over on module Q is relayed in full; `grant` rises; `no_winner`, `link_down` (sparse series: absent until a first count) and `deny_no_winner` do not move |
| RTS1-c | a room restart leaves no silent loss | `dv-room` of Q is SIGKILLed for 6 s while overs are offered every 0.5 s; afterwards an over is relayed within 90 s and the next one in full; the room's `no_winner <= deny` and nothing negative; the edge reasons that moved (`deny_no_winner`, `deny_controller_absent`, `deny_cts_timeout`, `deny_busy`) are RECORDED, a `deny_busy` is a FINDING (the pre-RTS1 mapping), a `link_down` after a process kill is a FINDING (the link did not go down) |

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_RTS1=on \
scripts/run.sh -m rig -- cases/test_rts1.py
```

No web profile, no `--capture`, no config extra (the template must serve module Q). Estimated time (not measured): about 5 minutes (the hub refreshes records about every 10 s; each counter wait
is up to 45 s, the restart and the 90 s budget are rarely used). One rig, nothing else at the same time.

## GAPs

* **The winner gap cannot be provoked from outside the launcher.** It needs two controller tokens or a token view of `None` on a live controller (the unit tests `floor_sequence_tests.rs rts1_*`,
  `control_core.rs rts1_*`, `ingress_terminal.rs rts1_*` and the fuzz seeds `deny_no_winner_voice.hex`, `cts_no_winner.hex` cover it). `kill-proc` kills a process, which removes the controller
  (CTS timeout or Absent 101: `deny_cts_timeout`, `deny_controller_absent`), it does not leave a live one without a winner.
* **Reason 12 on a legacy client wire**: nothing carries it, a denied over is silent. Only the WebRTC talk path shows it (`absent`, "The controller is unavailable"); a browser case for it needs the gap.
* **A pre-RTS1 edge reading 12 as busy** cannot be mixed with this image in one node.
* **`link_down`** needs the room's router session cut while RTS are in flight: not producible (the process kill resets the room's counters, and the `fabric: router link down` lines are not written).
* "No RTS is re-sent after DENY", "no Abort", and "an over that was already granted is not interrupted" are not observable from outside.

## Answers of dv-router (2026-11-22, `answers-342-dvt.md` Q5, Q6)

There is no primitive that holds a room controller in the winner gap and none that cuts a session; both stay GAPs (FABCUT1 is a possible future seam). Nothing in the cases changes.
