<!-- SPDX-License-Identifier: MIT -->
# DV-Router XSTALE1c: a leaving listener settles its egress over at the leave

Status: DV-Router `5072bf57` (image `dv-router:wip-3f142cd`); rig run `20261123T124042Z` 18 / 0 (the 9 XSTALE1 cases and the 9 XSTALE1c cases, see "Results"). Cases: `cases/test_xstale1c.py` (helpers `cases/xstale1c_support.py`, the leave / rejoin of the kits in
`cases/xstale1_support.py`), arithmetic and verdicts `dvtest/xstale1c.py`, offline tests `cases/unit/test_xstale1c_lib.py`. Switch: `DVT_XSTALE1C=on` (the image has XSTALE1c); off, the rig
cases skip with the reason. Builds on `docs/xstale1-cases.md` (the same edge counters and the same `edge` fixture).

## What the image promises

* A listener that leaves mid-over settles its egress over at the moment it leaves. After the leave no lost frames are counted (before the fix the rest of the over counted as lost, for example
  sent 150 lost 350). Real losses before the leave are still counted.
* The same listener leaving and rejoining folds as `joined`, without double counting.
* A stream refused at its start for want of a radio id adds no dropped frames: only the `dv_edge_drops_total{reason="no_radio_id"}` event, once, and no egress over is folded.

## Cases

Talker A and listener B are two clients of the SAME server port of the edge under test (as in XSTALE1), counters are the hub's tx fold family of that edge (`dvtest/xstale1.py`) plus
`dv_edge_drops_total{reason="no_radio_id"}`. Every case takes a quiet baseline, runs ONE scenario and reads once the edge has folded, 12 s later.

| Case | Scenario | Asserted |
|------|----------|----------|
| XS1c-a[usrp, p25, nxdn] | B listens from the start, A talks 8 s, B leaves (USRP unregister, P25 / NXDN unlink) 3 s in, about 150 units | one fold at the edge, not joined; tx `lost`, `late`, `dropped`, `lossy`, `end_missing` all 0; x_stale <= 2; A hears nothing of itself. B must have heard 60 percent of what was sent before the leave (a harness check) |
| XS1c-b[p25, leave] | a D-STAR talker (DExtra, N0CALL, transcoded to the P25 listener) talks 8 s on the module of the P25 edge, B leaves 3 s in | one fold, not joined; `lost`, `late`, `dropped`, `lossy` 0, x_stale <= 2. `end_missing` is a FINDING (below, XSTALE1d), asserted 0 with `complete` 1 under `DVT_XSTALE1D=on` |
| XS1c-b[p25, stay] | the same over, B listens to the End | the control: `complete` 1, all of the above 0 |
| XS1c-c[usrp, p25, nxdn] | as a), B is back 4 s in (same socket, the same address; the edge answers in 0.3 to 1.5 s) | one fold, `joined` exactly 1, `lost`, `late`, `dropped`, `lossy`, `end_missing` 0 (nothing counted twice); B hears at least 80 percent of the units to come after its link completed and no more than was sent (+ padding) |
| XS1c-e[dcs, leave] | XSTALE1d, same mode and another edge, no transcoding: a D-STAR talker on DExtra (N0CALL, module H), the only listener a DCS client (N0TEST) of the same module; it unlinks 3 s into the 8 s over. Counters of the `dcs` edge. Needs `DVT_XSTALE1D=on` (skips otherwise: wip-3f142cd folds this over `end_missing`, per DV-Router) | one fold, `complete` exactly 1, not joined; `end_missing`, `lost`, `late`, `dropped`, `lossy` 0; x_stale <= 2. The listener must have heard 60 percent of what was sent before leaving (a harness check) |
| XS1c-e[dcs, stay] | the control: the same over, the DCS listener hears the End | the same verdict (`complete` 1, all else 0) |
| XS1c-d[p25] | a D-STAR talker with the callsign N0UNK (in no `dmrid.dat`) talks 4 s to the P25 listener B | `no_radio_id` rose by exactly 1 over all edges (the P25 edge), no egress over folded (all tx folds 0), `dropped`, `late`, `lost` 0, B hears nothing |

Numbers of every case: `out/<run>/xstale1c.json` (one dict per case), the readable lines in `out/<run>/xstale1c.txt`.

### Real losses before the leave: not testable from outside

The promise "real losses before the leave stay counted" needs a loss at the egress edge of the listener, i.e. a hole in the publication ordinals. The tester cannot make one:

* a same-protocol talker that leaves frames out (USRP, P25, NXDN) gets its ordinals from the frames that ARRIVED; the wire gap is counted separately at the ingress (`ingress_gap_position`, "transport gaps stay
  separate from ordinal media_seq"). Nothing is lost at the listener's egress;
* a D-STAR talker is placed by its own DSVT counter, so skipped counters leave ordinal holes at the ingress. Rig run `20261123T122420Z` tried it (three counters 30, 60 and 90 left out, `DstarTalker.over(skip=...)`):
  with B leaving at 3 s the over folded `end_missing` with lost 0, and with B staying to the End (the control) it folded `complete` with lost 0. The transcoder path (AMBE to IMBE) does not carry the holes to the P25 edge.

The Rust unit tests of the product (`xstale1c_leave_keeps_real_losses_before_the_leave`, lost 3 of 150) are the only check of that sentence; the rig cases assert the other half (nothing counted after the leave).

## Findings

* **XS1c-b[leave]: `end_missing`.** A cross-protocol (transcoded) over whose only listener left folds as `end_missing` 1 at the P25 edge, lost 0. The same over with the listener staying folds `complete`, and the
  same-protocol leave (XS1c-a, the same edge's own talker) folds `complete`. The promise (no lost after the leave) holds; the fold RESULT differs. A likely cause (not verified from outside): with no listener left
  for the IMBE profile the room stops the transcoded stream, so its End never reaches the P25 edge, and the settled interval has no End count to close on. Whether `end_missing` is the intended result for that
  over is for DV-Router to say: the case records it as `[FINDING]` and does not assert it.
  DV-Router answer (2026-11-23): NOT intended; the leave settlement must apply to the transcoded leg too (fold `complete`, lost 0, `end_missing` 0). Open as
  **XSTALE1d**. With `DVT_XSTALE1D=on` (the fix image) XS1c-b[leave] asserts `complete` 1 and `end_missing` 0; without it the case keeps recording the finding.
* **XS1c-d** also moved `dv_edge_drops_total{proc=dextra,reason=no_session}` by 1 and `proc=p25,reason=id_held` by 1 (the Start held for the radio id lookup before the refusal): recorded, not asserted.

## Run

Same environment as `docs/xstale1-cases.md` (native launch, `DVT_USRP_DYNAMIC=1`, `soak-edges.toml`); case b and d use the real transcoder of the rig and the DExtra edge on module G (the P25 module).

```sh
export XLXD_SUT_IMAGE=dv-router:wip-3f142cd DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> DVT_SUT_GEN=compat1
DVT_XSTALE1C=on DVT_RIG_CONTROL=1 DVT_SUT_NAME=dv-router DVT_USRP_DYNAMIC=1 DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/soak-edges.toml \
  scripts/run.sh -m rig -- cases/test_xstale1c.py -rA
```

Add `DVT_XSTALE1=on` and `cases/test_xstale1.py` to run the XSTALE1 cases in the same run. About 8 minutes for the 9 cases.

## Results (image dv-router:wip-3f142cd)

| Run | Cases | Result |
|-----|-------|--------|
| `20261123T120952Z` | the first 9 (b with three D-STAR counters left out, clean and gaps) | 7 passed; b[clean] and b[gaps] failed on `end_missing` 1 (the finding above); b[gaps] lost 0 (no loss to count, see "Real losses") |
| `20261123T122420Z` | b only: leave / stay x clean / gaps | clean-stay `complete` passed; clean-leave `end_missing`; gaps-leave and gaps-stay lost 0 (the control shows the holes are not carried) |
| `20261123T124042Z` | `cases/test_xstale1.py` (9, both switches on) + `cases/test_xstale1c.py` (9) | 18 passed, 0 failed, 902 s |

Numbers of the final run (tx deltas of the edge; every unlisted counter 0, x_stale 0):

| Case | Scenario numbers | Fold |
|------|------------------|------|
| XS1c-a[usrp] | 400 sent, B heard 150 before leaving at 3.00 s | complete 1, lost 0 |
| XS1c-a[p25] | 396 sent, B heard 151 | complete 1, lost 0 |
| XS1c-a[nxdn] | 396 sent, B heard 148 | complete 1, lost 0 |
| XS1c-b[p25, leave] | 400 D-STAR frames, B heard 136 before leaving | end_missing 1, lost 0, dropped 0, late 0 (FINDING) |
| XS1c-b[p25, stay] | 400 D-STAR frames, B heard 405 in all | complete 1, lost 0 |
| XS1c-c[usrp] | 400 sent, left 3.00 s (heard 150), back 4.53 s, heard 174 of 173 to come | joined 1, lost 0, late 0, dropped 0 |
| XS1c-c[p25] | 396 sent, left 3.00 s (151), back 4.30 s, heard 183 of 184 | joined 1, lost 0 |
| XS1c-c[nxdn] | 396 sent, left 3.00 s (148), back 4.30 s, heard 184 of 184 | joined 1, lost 0 |
| XS1c-d[p25] | 200 D-STAR frames from N0UNK, B heard 0 | `no_radio_id` +1 (proc p25), all tx folds 0, dropped 0, lost 0, late 0 |

The XSTALE1 cases kept their results (b complete, c joined, d two overs, x_stale 0) with the same image. The pre-fix signature (sent 150 lost 350) is what `judge_leave` would flag as `tx lost rose by 350`;
the rig has no pre-fix image loaded, so the cases were not shown failing on it.

### Image dv-router:wip-e9fcc38 (XSTALE1d, code `4f454c71`), `DVT_XSTALE1C=on DVT_XSTALE1D=on`

| Run | Cases | Result |
|-----|-------|--------|
| `20261123T153442Z` | XS1c-b (leave, stay) and XS1c-e (leave, stay) | 4 passed, 0 failed, 199 s |

| Case | Scenario numbers | Fold (tx deltas of the edge, every unlisted counter 0) |
|------|------------------|---------|
| XS1c-b[p25, leave] | 400 D-STAR frames, B heard 136 before leaving at 3.00 s | complete 1, end_missing 0, lost 0 (was end_missing 1 on wip-3f142cd) |
| XS1c-b[p25, stay] | 400 frames, B heard 405 in all | complete 1 |
| XS1c-e[dcs, leave] | 400 D-STAR frames on DExtra, DCS listener heard 136 before leaving at 3.00 s | complete 1, end_missing 0, lost 0, late 0, dropped 0 |
| XS1c-e[dcs, stay] | 400 frames, DCS listener heard 400 in all | complete 1, all else 0 |

XS1c-e was written for the fix image and was not run on wip-3f142cd: it was not shown failing on the old behaviour (DV-Router says the false `end_missing` was not limited to transcoded streams).
