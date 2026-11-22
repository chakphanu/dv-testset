<!-- SPDX-License-Identifier: MIT -->
# MEDLOAD1 cases: per-over lost / gap / late counts in the status hub, and the loaded acceptance S7

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`; MEDLOAD1 `fb6a2a51` S1 and S2 .. `28fc1838` MEDLOAD1e, S3 / S4 `9ca5f41b` recorder). **Nothing here has run on a rig.**
Case file `cases/test_medload1.py`, arithmetic, counts and the S7 classifier `dvtest/medload1.py`, unit tests `cases/unit/test_medload1_lib.py`, switch `DVT_MEDLOAD1` (and the optional
`DVT_MEDLOAD1_BURN`, number of busy-loop processes of the S7 analogue; both passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`). Notes: stdout and
`out/<run>/medload1.txt` (`RECORD`, `FINDING`). Also changed: `cases/test_web_recorder.py` accepts the new nullable `late_units` field of the recorder rows (index version 2) next to the old field set.

**Gate.** `DVT_MEDLOAD1=on`; without it every case skips with the reason.

## What is asserted

Documents: `media-format.md` 186-340 (ingress end counts, the room tally of arrived / late / lost, conservation `published = sent + late + dropped + lost`), `status.md` 704-830 and the metrics table
(`dv_edge_over_units_total{dir,result}`, `dv_edge_overs_total`, `dv_room_over_units_total{room,result=late|lost|gap}`, `dv_room_overs_total{room,result=complete|lossy|end_missing}`), `work-units.md`
(MEDLOAD1 S1 to S7). Semantics read from the sources (UNVERIFIED on a rig): the counters are cumulative per process, folded once at the end of an over, exported by the hub about every 10 s with a `proc` label;
`gap` is a missing SOURCE position (D-STAR family: the native counter 0..20, modulus 21, delta = (pos + 21 - prev) mod 21, a duplicate and a backward step of at most 10 are ignored; USRP: the u32 wire
sequence); M17, YSF, DMR, P25, NXDN and WebRTC count none; the room's metric `lost` is the room hop only (the summary fields of an event or a recorder row add the gap to lost); `late` and `dropped` come from a
CTS stall or host starvation; an over with a source gap only is `lossy` with gap > 0 and lost = 0; a source hole is not egress loss.

| Case | Over | Assertion |
|------|------|-----------|
| ML1-a clean | DExtra, 60 frames + last, 20 ms apart | edge rx gap 0, room gap 0, room lost 0, late and dropped 0, egress lost 0, `overs{complete}` +1, the listener hears 60 frames |
| ML1-b drops | frames 10..14 and 30 never sent | gap 6 at the edge and at the room, lost 0, `overs{lossy}` +1, the listener hears the other 54 |
| ML1-c drops and a late frame | as b, frame 25 sent right after frame 28 | gap 7 (the hole of 25 counts, the late frame is ignored), lossy +1; the heard set is recorded (a FINDING when it differs from the rule) |
| ML1-d wrap and duplicate | frame 21 (counter 0) dropped, frame 40 sent twice | gap 1, the duplicate adds none, lossy +1 |
| ML1-e M17 | M17 stream, five packets missing | edge and room gap do NOT move (a FINDING when they do: not a failure) |
| ML1-f USRP | wire sequences 20..22 absent | gap 3 at the edge and the room (needs `DVT_USRP_DYNAMIC=1`, else it skips) |
| ML1-g S7 analogue | five rounds of five clean overs with 16 busy-loop processes in the tester | per round zero lost, gap, uncounted (sent minus the room's over count) and unknown (`end_missing`); `pass`, or `HOST_STARVED` when late or dropped units were counted and every over was heard in full; at most one `HOST_STARVED` round, no `FAIL` |

The wait for a counter is the room over count (+1, or +5 per S7 round) and one more read 12 s later (the edge records land on their own cadence); at most 60 s. Overs are serial; with at least 50 frames none is a
short stream, so the guard cannot interfere.

## S7: what the design requires, and what the rig can reproduce

`work-units.md` (row 237, tracker text of `2a7c2b4c`): "loaded acceptance S7c (16 loops in a scope with a 6-CPU quota, 5 rounds): zero lost, gap, uncounted or unknown, four rounds pass with bounded repeats
that are counted late, one non-pass labelled HOST_STARVED after complete delivery". `testing.md` 557-640 defines the oracle (the per-hop table, HOST_STARVED = every hop conserves, all counts known, no lost or
gap, the missing units late or dropped). That acceptance runs the K6b real-children suite (Rust, `crates/router/tests/real_children`) under 16 busy loops in a 6-CPU cgroup; the per-hop oracle is not
reproducible from outside. ML1-g is the black-box analogue: the same round structure, classification and acceptance (4 of 5 pass, at most one HOST_STARVED, zero lost / gap / uncounted / unknown), with clean
D-STAR overs and the counters of the hub. Its limit: the busy loops run in the TESTER container and are capped by its `cpus` limit; they starve the SUT only as far as both share host CPUs
(`DVT_MEDLOAD1_BURN` changes the number; the host side, `systemd-run --scope -p AllowedCPUs=` on the SUT cpuset, is not done here and needs the owner). Whether the load happened is not measurable inside the case:
read `out/<run>/` `proc-stat` of the launcher if needed. No load is generated against the production host.

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 \
DVT_MEDLOAD1=on DVT_USRP_DYNAMIC=1 \
scripts/run.sh -m rig -- cases/test_medload1.py
```

No web profile, no `--capture`, no config extra (module P must be served: `rooms = "A-Z"` does); without `DVT_USRP_DYNAMIC=1` ML1-f skips. Estimated time (not measured): ML1-a..e about 1.5 minutes each
(over, up to 60 s settle, 12 s quiet), ML1-f 1.5 minutes, ML1-g about 5 x 40 s = 4 to 5 minutes; about 13 minutes in all. Run it alone (the load rounds must not share the host with another rig; `-k "not s7"` leaves the load out).

## GAPs

* **Late and dropped on purpose**: needs a CTS stall or host starvation (S7 is the only provocation); a late unit by sequence manipulation is impossible (a reorder is ignored, not late).
* **Room `lost` > 0**: needs a fabric loss between the edge and the room; the rig cannot drop fabric traffic.
* **Per-over fields** (`frames`, `published`, `media_published`, `late`, `lost`, `gap` of the room event, LastOver and heard rows): not in `openapi-admin.yaml` (`Heard` and `LastOver` have `additionalProperties: false` without
  them) nor in the public pages, so no HTTP surface shows one over's counts except the recorder row (`late_units`, `lost_units`: `lost_units` = upstream lost including the gap, plus recorder-hop loss), which needs
  `recorder.toml` plus the decoder worker for D-STAR AMBE (K14c) or a USRP over (PCM): not written here. `media_published` has no metric.
* **YSF, DMR, P25, NXDN, M17 and WebRTC gaps** are not counted by design (ML1-e only checks M17 stays 0).
* The RTSCLOCK1 clock-domain check (the other half of `28fc1838`) refuses only when both sides know a different identity; all processes of a rig node share one host, so there is nothing to observe.

## Questions for the DV-Router team

1. Are the per-over fields meant to reach an HTTP API (the admin heard / last-over rows, the public feed)? The openapi at `bc9af2b1` lists none; with them every case above could assert the PER-OVER numbers
   instead of cumulative deltas.
2. The model counts the last frame (counter `n mod 21`) in the gap rule; confirm that the end frame takes part, and that frame counters 21..31 are dropped as `rx_ignored` without a gap count.
3. Is a rig-level load recipe for S7 agreed (cgroup CPU set of the SUT container)? The tester-side busy loops only share host CPUs.
