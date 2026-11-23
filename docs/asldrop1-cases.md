<!-- SPDX-License-Identifier: MIT -->
# ASLDROP1 cases: a refused AllStar voice packet is counted once, as `late`

Status: run on `dv-router:wip-42d5473`, run `20261123T211158Z` (testset `b67b947` plus these files; together with `cases/test_rfpos1c.py` in one run, 7 tests, 422 s).
The run was judged with the first reading of the contract (nothing else moves) and a failed on rx gap and room gap; dv-router ruled that the product is right and the contract line was
imprecise, the asserts below are the corrected ones, and the recorded counter deltas of that run hold against them (checked offline, no new rig run): **a, b and c all pass**. Case file `cases/test_asldrop1.py`, helpers and judges `dvtest/asldrop1.py`, unit tests
`cases/unit/test_asldrop1_lib.py` and `cases/unit/test_iax2.py`, switch `DVT_ASLDROP1` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`),
profile `asl-edge` (`-m asl_lab`; with the switch `run.sh` adds `rfpos1c-usrp.toml`, the static USRP row of module L). Notes `out/<run>/asldrop1.txt` (`RECORD`, `FINDING`), numbers per case
`out/<run>/asldrop1.json`. Without `DVT_ASLDROP1=on` every case skips naming it.

## The contract (dv-router)

The AllStar edge counts a refused voice packet once, in `dv_edge_over_units_total{dir=rx,result=late}`. A packet is refused when it is before the over's first timestamp, behind
playout (more than 60 ms late) or a repeat of a pending position. Corrected line (dv-router): `late` counts packets that ARRIVED and were not used, `gap` counts MISSING positions; they do
not double count. So a frame refused as more than 60 ms late is late +1 and gap +1 (its position stays empty); a packet stamped before the over's first timestamp is late +1 and gap 0
(nothing is missing); an over with a late unit folds `lossy`, the same rule as a refused backward or duplicate D-STAR datagram. rx lost and dropped, the room's lost and the egress tx lost
and dropped never move.

## How

The tester is the IAX2 caller (account `acct`, room L, as in `docs/rfpos1c-cases.md`): an over of 90 distinct mu-law frames at 20 ms, with the D-STAR, DMR and USRP listeners of RFPOS1C.
`Client.voice_at(timestamp, frame)` sends a mini frame with an explicit timestamp: a frame keeps its slot on the call clock (`voice(drop=True)`) and goes out later. The edge's tap
capture is read for the order in which the call's voice frames arrived, and must equal the order the tester sent them (harness check `asldrop1.order_problems`).

| Case | Over | Assertion |
|------|------|-----------|
| ASLDROP1-a | frame 40 sent 110 ms late (after frame 45, before 46), more than 60 ms behind playout | D-STAR and USRP do not play frame 40, every other frame once in order; rx late +1, rx gap +1, room gap +1, over lossy +1; rx lost and dropped +0, room lost +0, every egress tx lost and dropped +0 |
| ASLDROP1-b | frame 40 sent 30 ms late (after 41, before 42), inside the window (control) | heard at its own position (D-STAR 0..89, USRP in order), rx late +0, nothing moves (gap, lost, dropped, lossy +0) |
| ASLDROP1-c | a mini stamped 20 ms before the over's first timestamp, sent after frame 2 | 90 frames heard, no extra frame; rx late +1, rx gap +0, room late +1, over lossy +1; lost, dropped, egress +0 |

The shape of the output around the refused frame (a hole at its position or the later frames closed up) is recorded, not judged: the contract does not name it.

## Result on wip-42d5473 (run 20261123T211158Z)

| | a (110 ms late) | b (30 ms late, control) | c (before the first timestamp) |
|---|---|---|---|
| capture order = send order | yes (90 frames) | yes | yes (91 frames) |
| D-STAR | 89 frames, position 40 missing, 0-39 and 41-89 at their own positions | 90, all own | 90, all own |
| USRP | 89 packets, frame 40 absent, the others in order | 90, in order | 90, in order |
| DMR (recorded) | 90 slots, slot 40 a fill, the rest own | 90 own | 90 own |
| rx late | +1 | +0 | +1 |
| rx gap / lost / dropped | gap +1 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| room gap / lost / late | gap +1 / 0 / +1 | 0 / 0 / 0 | 0 / 0 / +1 |
| over | lossy +1 | complete | lossy +1 |
| egress tx lost / dropped (dextra, dmrhb, usrp) | 0 / 0 | 0 / 0 | 0 / 0 |

Offline check of the corrected asserts against the counter deltas of this run (`out/20261123T211158Z/asldrop1.json`, `dvtest.asldrop1.judge_counters`): **a holds** (rx late +1, rx gap +1, room gap +1,
lossy +1, lost and dropped 0, egress 0), **b holds** (all 0), **c holds** (rx late +1, gap 0, room late +1, lossy +1, 90 frames heard). The listeners were judged live and did not change. No new rig run
was needed.

History: the first reading of the contract was "nothing else moves" (rx gap and room gap +0 for a); the run showed gap +1 and room gap +1 in a and the case failed on exactly those two counters, recorded as a
FINDING. dv-router's answer: the product is right (an arrived-but-unused packet is `late`, a missing position is `gap`, no double counting), the contract line was imprecise. `dv_room_over_units_total{result=late}`
is +1 in a (recorded, not asserted) and in c (asserted).

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-42d5473 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 DVT_ASLDROP1=on \
  scripts/run.sh --profile asl-edge -m asl_lab -- cases/test_asldrop1.py -rA
```

About 3 minutes for the three cases (the 422 s run also held the four RFPOS1C cases with `DVT_RFPOS1C=on DVT_BUNDLEFILL1=on`). One rig, nothing else at the same time.

## GAPs

* The 16-bit mini timestamp wrap and a repeat of a pending position (the third refusal reason) are not cases here.
* The "about 100 ms" delay is 110 ms so that the frame leaves between frames 45 and 46 (60 ms behind playout is exceeded by a margin; no case probes the 60 ms edge itself).
