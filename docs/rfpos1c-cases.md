<!-- SPDX-License-Identifier: MIT -->
# RFPOS1 step C cases: a lost AllStar (IAX2) voice frame keeps its position

Status: written against DV-Router `fb3167bb` ("AllStar streams state frame positions through the transcoder", RFPOS1 step C), run on the image BEFORE it,
`dv-router:wip-3f142cd`: **2 passed (the controls), 2 failed (the loss cases): that is the recorded "before"**, run `20261123T152004Z` (repeat of
`20261123T145942Z`, same numbers; the first run's USRP judge was still a seq judge, see "USRP"). The run on the step C image has not been made. Case file
`cases/test_rfpos1c.py`, pure helpers and judges `dvtest/rfpos1c.py`, unit tests `cases/unit/test_rfpos1c_lib.py` and `cases/unit/test_iax2.py`, snippet
`rig/config/dv-router/rfpos1c-usrp.toml`, switch `DVT_RFPOS1C` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`).
Notes: stdout and `out/<run>/rfpos1c.txt` (`RECORD`, `FINDING`), numbers per case in `out/<run>/rfpos1c.json`.

**Gate.** `DVT_RFPOS1C=on` and `--profile asl-edge`; without the switch every case skips naming it. The switch means "assert the step C behaviour": on an image
without step C the loss cases fail on purpose and the notes are the before.

## What step C is (read with `git show`, not run)

The AllStar edge (`crates/edge/allstar`) rechunks timestamped IAX2 audio into one 320 B PCM unit per 20 ms (`allstar.md`, "Runtime and media") and now states
`RTS_FRAME_POSITIONS` (`protocol.rs rts_flags`). The controller then restores the source `media_seq` of every transcoded unit (`media-format.md`,
"Transcoded profiles"): a unit that never arrived is a GAP at its own position in the D-STAR and DMR output, not a compaction. The first received packet of an over
is position 0; the 16-bit mini timestamp is expanded against the last full frame (the wrap at 65.5 s is covered by the product's Rust tests and is not a case here).
The product test (`crates/edge/allstar/tests/runtime_support/frame_positions.rs`) lets a node send mini frames and leaves some out.

## How one frame is lost

The tester IS the IAX2 caller: `dvtest.proto.iax2.Client` (account `acct` of the edge, node 2999, room L, ulaw, the way `cases/test_asl_edge.py` T1b does) and it
does not send frame 40 (`Client.voice(drop=True)`): the call clock still advances by 20 ms, nothing leaves the socket, no sequence number is used (a real lost
full frame would be NAKed and sent again by a real client; the tester has no retransmission to send, so "lost" means never delivered). No relay is needed: the
cheapest deterministic loss is the sender's own, and the tester knows the index. The hole is proved at the receiving side: the edge's tap capture
(`out/<run>/asl-lab/rfpos1c-<case>/edge.pcap`) is read for the voice timestamps of the call (`rfpos1c.voice_stamps`, mini timestamps expanded) and must show
exactly the planned hole (`holes == [40]`, 89 frames, none off the 20 ms grid). Case c sends position 40 as a FULL voice frame (`voice(full=True)`, 32-bit
timestamp, reliable) and loses that one; case d is the control that sends it and loses nothing.

Why position 40: it is the middle frame of DMR burst 13 (40 = 13 x 3 + 1), above the 50 frame short stream guard the over is 90 frames (1.8 s).

## Why PCM identity and no marker

The frames are 90 distinct seeded random mu-law frames (160 B each). The edge decodes G.711, so the PCM of position `p` is `ulaw_to_pcm(frame p)` byte for byte and
the fake transcoder (`--profile asl-edge` forces `DVT_TRANSCODER_FAKE=1`) turns it into a predictable 9 B frame (`out[i] = pcm[i] ^ K ^ i`, `dvtest/crossmode.py
transform_units(5, codec, ...)`; it reads the first nine PCM bytes, and the 90 outputs are pairwise different, checked by the unit test). Every received frame is looked
up in that list, so each listener reports "position q holds the frame of position p": a closed-up or shifted stream shows as `q != p` with no marker bytes and no
extra codec in the path. With a real transcoder the D-STAR and DMR frames are not predictable and the cases skip.

## Listeners (all in room L, module L)

| Listener | How | Position | Expected after step C (position 40 lost) |
|----------|-----|----------|------------------------------------------|
| D-STAR (DExtra, `ds.dstar_listener`) | transcoded PCM -> AMBE | the unwrapped 5-bit counter (`rfpos1.dstar_voice`) | positions 0..89 except 40, each holding its own frame; the output reaches 89 |
| DMR (`clients.make("dmr", "L")`) | transcoded PCM -> AMBE+2 | index in the flat list of the bursts' 3 frames | 90 slots (30 bursts): every slot but 40 holds its own frame, slot 40 does NOT hold frame 40 (a fill), frames 39 and 41 intact |
| USRP (static row, `rfpos1c-usrp.toml`: `<OWN_IP>:34004` <-> tester `:32004`) | PCM, no codec: the control | content in arrival order and arrival time | every frame but 40 once, in order, and the packets of frames 39 and 41 at least 35 ms apart (the position is kept in time) |

### USRP

The USRP packet `seq` is the edge's running packet counter: it continues across overs (0..89, 91..179, 181..269, ...) and has no hole, so the first version of the
judge (position = seq - first seq) called the control wrong. A USRP listener sees a lost position only in TIME: the edge's tap capture shows a gap of 80.6 ms (case b)
and 79.8 ms (case c) between the packets of frames 39 and 41 (normal 20 ms, longest gap of a lossless over 20.9 ms), that is the position is kept in time (and more:
the gap is 4 ticks for a 1 tick hole; the next two packets follow within 1 ms and the rest of the over runs about 9 ms late; recorded, not asserted). The judge
(`rfpos1c.judge_usrp`) asserts the content (every frame but 40, once, in order: a PCM unit is a source unit, so the content cannot be shifted) and the hole in time
(>= 35 ms; with no loss no gap >= 60 ms). The `dv_edge_over_units_total{tx,proc=usrp,result=lost}` series did not move on wip-3f142cd although the position was kept in
time; the case accepts 0 or 1 there (`at_most`) and records the value.

## Counters (hub metrics, `metrics xlxd-sut`, one reading about 12 s after the over and the folds)

Read the way `cases/test_rfpos1b.py` does (`rfpos1b.stable_baseline`, `wait_settled`, room label `l`): `dv_room_over_units_total{room=l}`, `dv_room_overs_total{room=l}`,
`dv_edge_over_units_total{dir=rx|tx,proc=...}`, `dv_edge_overs_total{dir=tx,proc=...}` for the edges `dextra`, `dmrhb` and `usrp`. After step C the case asserts:
`tx lost` rises by exactly 1 at `dextra` and `dmrhb` (at most 1 at `usrp`), the room's `lost` by at most 1, each egress edge folded one over; with no loss
nothing is gap, lost or lossy anywhere. `late` and `dropped` are recorded as FINDING. The `proc` label of the AllStar ingress edge is `allstar` (seen in the rx series).

## Cases

| Case | Over | Assertion |
|------|------|-----------|
| RFPOS1C-a | no loss (control) | all three listeners hold frames 0..89 at their own positions; no gap, lost or lossy over anywhere |
| RFPOS1C-b | mini frame 40 lost | D-STAR: positions 0..89 except 40, own frames; DMR: slot 40 is a fill, the rest own frames; USRP: frames except 40 in order with a hole in time; egress lost +1 (dextra, dmrhb) |
| RFPOS1C-c | full frame 40 lost | the same as b |
| RFPOS1C-d | full frame 40 sent, nothing lost (control for c) | the same as a |

Verdicts are kept apart in the failure text and the JSON: `harness` (the capture hole is not the planned one, a listener heard nothing, an egress edge folded no over, the
USRP capture and the listener disagree), `usrp`, `dstar`, `dmr`, `counters`.

## The recorded before (dv-router:wip-3f142cd, run 20261123T152004Z; the first run, 20261123T145942Z, gave the same shapes)

| | a (control) | b (mini 40 lost) | c (full 40 lost) | d (control) |
|---|---|---|---|---|
| edge capture: voice frames / timestamp holes | 90 / none | 89 / [40] | 89 / [40] | 90 / none |
| D-STAR | 90 frames, 0-89 hold 0-89 | 89 frames, NO gap, counters 0..88: 0-39 hold 0-39, 40-88 hold 41-89 | same | as a |
| DMR | 90 slots, own frames | 90 slots: 0-39 own, 40-88 hold 41-89, slot 89 a zero pad (the burst is filled at the END) | same | as a |
| USRP (control) | 90 packets, max gap 20.9 ms | 89 packets, frames except 40 in order, hole in time 80.6 ms | 79.8 ms | max gap 20.8 ms |
| `tx lost` dextra / dmrhb / usrp | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| room gap / lost / lossy over; ingress `allstar` rx gap | 0 / 0 / 0; 0 | 1 / 0 / 1; 1 | 1 / 0 / 1; 1 | 0 / 0 / 0; 0 |

So on the old image the room counts the gap (the ingress edge and the room both see one missing position and the over is `lossy`), the PCM listener keeps the position in
time, but both transcoded listeners close the hole up: D-STAR is one frame short with a continuous counter and every later frame is one position early, DMR has every later
frame one slot early and a padded last burst, and no egress edge counts a lost unit. The failures of b and c are therefore the product: the controls a and d pass (listeners,
counters, capture all as planned), the hole did arrive at the edge (capture), and the USRP control gives the same answer on both sides of the fix.

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-3f142cd DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 DVT_RFPOS1C=on \
  scripts/run.sh --profile asl-edge -m asl_lab -- cases/test_rfpos1c.py -rA
```

About 4 minutes (the baseline took 233 s of pytest). No web, no `--capture` (the lab agent captures the edge's namespace itself), no `DVT_ASL_EDGE_OUT` (no outbound rows).
For the step C image use its tag in `XLXD_SUT_IMAGE`. One rig, nothing else at the same time. `run.sh` adds `allstar-lab.toml` and, under the switch, `rfpos1c-usrp.toml` to
`DVT_SUT_CONFIG_EXTRA`.

## GAPs

* Not covered: the 16-bit mini-timestamp wrap (needs an over longer than 65.5 s from the call's timestamp origin; the product's Rust tests cover it), a hole of several positions,
  reordered minis, a lost first frame (the first received packet is position 0 by definition), and a real lost full frame that the sender retransmits.
* The `tx lost` expectation of the DMR edge (+1 for one missing unit inside a burst) and of the room's own `lost` are inferred, not documented: the run on the step C image decides
  whether `dmrhb` counts one lost unit or a different amount.
* The USRP edge's 80 ms hole for a 20 ms position is recorded, not asserted (only >= 35 ms).
* The AllStar edge's egress to a real app_rpt node (IAX2 out) is not a listener here.
