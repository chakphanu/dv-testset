<!-- SPDX-License-Identifier: MIT -->
# RFPOS1 step C cases: a lost AllStar (IAX2) voice frame keeps its position

Status (counter expectations corrected by dv-router after the first commit; the baseline numbers below are unchanged and still hold against the corrected asserts): written against DV-Router `fb3167bb` ("AllStar streams state frame positions through the transcoder", RFPOS1 step C), run on the image BEFORE it,
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

The tester IS the IAX2 caller: `dvtest.proto.iax2.Client` (account `acct` of the edge, node 2900, room L, ulaw, the way `cases/test_asl_edge.py` T1b does) and it
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
and 79.8 ms (case c) between the packets of frames 39 and 41 (normal 20 ms, longest gap of a lossless over 20.9 ms), that is the position is kept in time (80 ms = 20 ms spacing plus the
edge's 60 ms late window, expected by dv-router; the next two packets then follow within 1 ms and the rest of the over runs about 9 ms late; recorded, not asserted). The judge
(`rfpos1c.judge_usrp`) asserts the content (every frame but 40, once, in order: a PCM unit is a source unit, so the content cannot be shifted) and the hole in time
(>= 35 ms; with no loss no gap >= 60 ms).

The 80 ms is expected (dv-router): the 20 ms frame spacing plus the edge's 60 ms late window. It stays recorded (`hole_gap_ms` in the JSON) and is not asserted beyond the
35 ms floor.

## Counters (hub metrics, `metrics xlxd-sut`, one reading about 12 s after the over and the folds)

Read the way `cases/test_rfpos1b.py` does (`rfpos1b.stable_baseline`, `wait_settled`, room label `l`): `dv_room_over_units_total{room=l}`, `dv_room_overs_total{room=l}`,
`dv_edge_over_units_total{dir=rx|tx,proc=...}`, `dv_edge_overs_total{dir=tx,proc=...}` for the AllStar edge (`proc=allstar`) and the egress edges `dextra`, `dmrhb` and
`usrp`. Corrected by dv-router after the first version of this case: step C does not change the room's counters, and since MEDLOAD1b an egress counts only loss on ITS OWN
leg (a hole the room already published as an upstream gap is upstream loss, not tx loss). Asserted (`rfpos1c.judge_counters`):

| Counter (delta over the over) | a, d (no loss) | b, c (frame 40 lost) |
|-------|------|------|
| ingress `dv_edge_over_units_total{dir=rx,proc=allstar,result=gap}` | 0 | +1 |
| room `dv_room_over_units_total{result=gap}` | 0 | +1 |
| room `dv_room_over_units_total{result=lost}` | 0 | 0 |
| room `dv_room_overs_total{result=lossy}` | 0 | +1 |
| egress `dv_edge_over_units_total{dir=tx,result=lost}` at dextra, dmrhb, usrp | 0 | 0 |

An egress `tx lost` rise is NOT asserted: it is written as a `[FINDING]` line. Each egress edge must have folded one over and the room closed one (else a `harness` problem).
`late` and `dropped` are recorded as FINDING.

## Cases

| Case | Over | Assertion |
|------|------|-----------|
| RFPOS1C-a | no loss (control) | all three listeners hold frames 0..89 at their own positions; no gap, lost or lossy over anywhere |
| RFPOS1C-b | mini frame 40 lost | D-STAR: positions 0..89 except 40, own frames; DMR: slot 40 is a fill, frames 39 and 41 carried, the rest own frames; USRP: frames except 40 in order with a hole in time; room counters gap +1, lost +0, lossy +1; egress tx lost +0 (a rise is a FINDING) |
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

So on the old image the room counts the gap (the ingress edge and the room both see one missing position and the over is `lossy`: exactly what step C keeps), the PCM listener
keeps the position in time, but both transcoded listeners close the hole up: D-STAR is one frame short with a continuous counter and every later frame is one position early, DMR
has every later frame one slot early and a padded last burst. The counter asserts (gap +1, lost +0, lossy +1, egress tx lost +0) PASS on the old image; only the position asserts
of D-STAR and DMR fail there. The failures of b and c are therefore the product: the controls a and d pass (listeners,
counters, capture all as planned), the hole did arrive at the edge (capture), and the USRP control gives the same answer on both sides of the fix.

## The step C image, dv-router:wip-e9fcc38 (fb3167bb), run 20261123T153031Z (testset 293191d), DMR triaged

Result as run: 2 passed (a, d), 2 failed (b, c), only on DMR. D-STAR misses exactly position 40 and 41-89 hold 41-89 (fixed), USRP unchanged (hole gap 79.3 ms), room counters
as expected. The first DMR verdict (87 frames, "shifted by one burst") came from placing frames by ARRIVAL ORDER, which is wrong for a burst the edge did not send; the DMR judge now
places each burst by its voice sequence letter (`rfpos1c.dmr_bursts`: flags byte low nibble A..F, a letter that does not rise starts the next superframe). The edge's tap capture of
that run (`asl-lab/rfpos1c-b/edge.pcap`, DMRD to the tester, same in c):

| | a (control) | b (frame 40 lost) |
|---|---|---|
| voice bursts sent / DMRD seq skips | 30 / none | 29 / none (seq renumbered, 14 follows 13) |
| around the hole | S0(A) +757 ms, seq 13; V1(B) +818, seq 14; V2(C) +877 | S0(A) +757, seq 13; **V2(C) +877, seq 14: the burst B (frames 39, 40, 41) was never sent** |
| over end | last burst +1777 ms | last burst +1777 ms (same timeline) |
| `dmrhb` tx | complete 1, dropped 0 | lossy 1, dropped +2, lost 0, late 0 |

Verdict: HARNESS for the "shifted" reading (the later 29 bursts hold their own frames 42-89 at their own letters and times: positions are kept, nothing is compacted), PRODUCT for what
remains: a PARTIAL result. The edge keeps the position at BURST granularity (a 120 ms hole in time, the letters B skipped, frames 42-89 intact) but drops the WHOLE burst of the lost frame:
frames 39 and 41 are not carried, whereas dv-router's expectation is that the burst still carries the other two frames. `dropped +2` at `dmrhb` matches the two surviving units of that burst
(inference from the counts: 3 units, 1 missing, 2 dropped; the egress fold is `lossy`). The judge reports exactly this as one sentence ("the whole burst of the lost frame ... was not sent:
frames [39, 41] are not carried; ... positions kept at burst granularity") and the case still fails on it until the burst carries them.

## BUNDLEFILL1 (switch DVT_BUNDLEFILL1=on): the bundled egresses

dv-router ruled the DMR whole-burst drop a defect and opened BUNDLEFILL1: an egress that packs several 20 ms units into one native frame (DMR burst 3, YSF frame 5, NXDN frame 4,
P25 LDU 9) fills a missing unit with the codec's fill and still sends the native frame at its own position and time; egress `dropped` and `tx lost` stay 0 (the loss is upstream).
Not a different image gate: the switch only decides whether the extra listeners run. `DVT_BUNDLEFILL1=on` makes `run.sh` add `rig/config/dv-router/bundlefill1-edges.toml` (the SUT's P25
port 41000 and NXDN port 41400 serving module L, TG 31672) and export `DVT_P25_MODULE=L` (the module of the P25 port is pinned by the environment `DV_P25_SERVER_MODULE`, which beat
the file in the first run: module G, no voice at the P25 listener). Without the switch the YSF, P25 and NXDN listeners do not run.

| Listener | Native frame | Position from | Time step |
|----------|--------------|---------------|-----------|
| DMR (always) | burst, 3 units | voice letter A..F | 60 ms |
| YSF | frame, 5 units | FICH FN modulo 7 | 100 ms |
| NXDN | frame, 4 units (7 B each) | SACCH structure 3, 2, 1, 0 | 80 ms |
| P25 | LDU, 9 records of 1 unit | record type 0x62..0x73 (LDU1 + LDU2) | 20 ms per record |

Units are placed by that counter (`rfpos1c.cyclic_positions`: a value that does not advance starts the next cycle), never by arrival order. Content: the frames of YSF / P25 / NXDN are not
predicted; the CONTROL over (case a, which must hold all 90 positions once each, distinct) is the reference, and a loss case compares each position with what the same listener got at that position
in the control. Asserted in b and c, for each of DMR, YSF, P25, NXDN: every position but 40 holds its own frame; the native frame holding 40 is sent; the lost unit's slot is a fill that is neither frame 40 nor
another position's frame (its bytes are recorded, not asserted); the timing contract below holds; egress `dropped` +0 on every
edge and `tx lost` +0 on YSF, P25 and NXDN (DMR, D-STAR and USRP: a rise is a FINDING).

### Timing contract (dv-router, replaces the old "control + 40 ms" slack)

The AllStar edge holds unit 41 until its 60 ms late window gives up on unit 40, so a native frame that carries the fill cannot leave earlier than 60 ms + one unit (20 ms)
after its position; it is expected, not a defect. Per bundled protocol (DMR burst, YSF frame, NXDN frame, P25 LDU), cases b and c assert, against the control (case a) and
measured as the send time of a native frame (a P25 LDU: its latest record) after the first datagram of the over minus the same time in the control (`rfpos1c.lateness`, from the
tap capture times):

1. the native frame that carries the fill is at most 80 ms over the control (`FILL_MAX_MS`);
2. the next native frame (DMR burst C, the next YSF frame, the next NXDN frame, the next P25 LDU) is back within 20 ms of the control (`RECOVER_MAX_MS`);
3. every later native frame up to the end of the over is within 20 ms of the control (no lasting drift).

More than 80 ms or a lasting drift is a defect and is asserted. Numbers per protocol (`fill_ms`, `next_ms`, `after_max_ms`) go to the notes (`RECORD`) and to the case JSON
(`dmr.lateness`, `bundled.<kind>.lateness`); case d (a second control) records the same numbers against case a as the measuring noise. A run directory can be re-judged offline from its
tap captures: `python3 -m dvtest.rfpos1c out/<run>` (exit 1 on a defect).

Re-judged from the saved captures of run `20261123T191840Z` (wip-42d5473, testset 6a091a3, which failed b and c on the old 40 ms slack only; the captures were enough):

| ms over the control | b: fill frame / next / later max | c: fill frame / next / later max | d (noise): fill / next / later max |
|---|---|---|---|
| DMR | 40.8 / -0.5 / 0.8 | 41.4 / -0.2 / 1.4 | 0.1 / -1.0 / 0.4 |
| YSF | -0.9 / -0.9 / -0.5 | -0.2 / -0.3 / 1.1 | -0.9 / -1.2 / 0.3 |
| P25 | 59.2 / -0.4 / 0.1 | 61.0 / 1.6 / 1.8 | 0.5 / 0.8 / 0.9 |
| NXDN | 0.7 / -0.3 / 7.4 | 1.3 / 0.9 / 1.5 | 0.3 / 0.3 / 7.8 |

All within the contract (80 / 20 / 20): the lateness of the fill frame is 41 ms (DMR) and 59-61 ms (P25), recovered by the next frame; the 7.4-7.8 ms of NXDN's later max is also in the
control pair (d against a), the padded last frame, so it is noise. YSF and NXDN send the fill frame on time.

Live, with the contract asserted (run `20261123T211158Z`, wip-42d5473, `DVT_RFPOS1C=on DVT_BUNDLEFILL1=on`, together with ASLDROP1): all four cases pass. ms over the control, fill frame / next
frame / later max: DMR b 41.3 / -0.8 / 0.6, c 40.1 / -0.1 / 0.8; YSF b -0.8 / 0.1 / 0.5, c 0.9 / 1.0 / 1.0; P25 b 62.4 / 1.5 / 1.8, c 61.5 / 1.4 / 2.0; NXDN b 1.2 / 0.0 / 0.3, c 3.5 / 2.7 / 4.0
(case d, the noise: at most 5.9 ms, NXDN).

### The before, dv-router:wip-e9fcc38 (step C, no BUNDLEFILL1)

Run `20261123T160335Z` (testset of this commit's parent plus the BUNDLEFILL1 additions, `DVT_RFPOS1C=on DVT_BUNDLEFILL1=on`): 2 passed (a, d), 2 failed (b, c), 250 s; the same shapes in b and c. The run before it,
`20261123T155348Z`, had the P25 module pinned to G (harness, fixed) and gave the same YSF, NXDN and DMR shapes. Controls a and d: every listener holds all positions, all egress counters 0.

| | Native frame of unit 40 | Other units | Slot of unit 40 | Egress counters (b, c) | Time |
|---|---|---|---|---|---|
| DMR | NOT sent: burst B (units 39, 40, 41) absent, seq contiguous, letters C.. follow | 0-38 and 42-89 at their own positions | (absent with its burst) | dropped +2, lossy over | bursts at their own times, 0.7-0.9 ms |
| YSF | sent (18 frames, FN contiguous) | 0-39 and 41-89 own | the frame of position 35 (a copy of another unit, not a fill) | dropped 0, lost 0 | 1.1 ms (control 3.9) |
| P25 | the LDU's other 8 records are sent | 0-39 and 41-89 own | record 0x66 NOT sent (no fill, the unit is absent) | dropped 0, lost 0 | +30 ms over the control (36-40 ms, control 6.6) |
| NXDN | NOT sent: frame 10 (units 40..43) absent, 22 frames instead of 23, the SACCH structure continues without a gap | 0-39 own; 40-85 hold 44-89 (counter closed up), 86-87 pad | (absent with its frame) | dropped +3 (the three survivors), lossy over | 144-149 ms (control 69): later frames are one native frame (80 ms) late |
| D-STAR, USRP (unchanged) | counter skips 40 / hole in time 79.8-80 ms | own | absent | lost 0, dropped 0 | |

So on wip-e9fcc38: only YSF keeps the native frame (its fill is a copy of unit 35); P25 omits the single record; DMR and NXDN drop the whole native frame, and NXDN also closes the SACCH counter up (the later
frames keep their time only up to a native frame of delay). The judge words are `the whole native frame of the lost unit ... was not sent` (DMR, NXDN), `the lost unit's slot ... holds the frame of position N, a copy
of another unit and not a fill` (YSF) and `was not sent at all (no fill)` (P25). The `lost unit's slot` text in the case's own JSON of this run (`bundled.<kind>.dropped_slot`, field written by the first version) says
"OWN FRAME" for YSF and NXDN when the slot held any matched frame: read the judge sentences instead (fixed in the code, not in the recorded file).

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-3f142cd DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template> DVT_SUT_GEN=compat1 DVT_RFPOS1C=on \
  scripts/run.sh --profile asl-edge -m asl_lab -- cases/test_rfpos1c.py -rA
```

About 4 minutes (the baseline took 233 s of pytest). No web, no `--capture` (the lab agent captures the edge's namespace itself), no `DVT_ASL_EDGE_OUT` (no outbound rows).
For the step C image use its tag in `XLXD_SUT_IMAGE`. With the BUNDLEFILL1 listeners add `DVT_BUNDLEFILL1=on` (about 4 minutes, 250 s measured; the YSF, P25 and NXDN clients link in `Listeners`). One rig, nothing else at the same time. `run.sh` adds `allstar-lab.toml` and, under the switch, `rfpos1c-usrp.toml` to
`DVT_SUT_CONFIG_EXTRA`.

## GAPs

* Not covered: the 16-bit mini-timestamp wrap (needs an over longer than 65.5 s from the call's timestamp origin; the product's Rust tests cover it), a hole of several positions,
  reordered minis, a lost first frame (the first received packet is position 0 by definition), and a real lost full frame that the sender retransmits.
* The egress `tx lost` +0 and the room counters come from dv-router's correction (MEDLOAD1b), not from a run on the step C image; a `tx lost` rise there is a FINDING.
* The USRP edge's 80 ms hole for a 20 ms position is recorded, not asserted (only >= 35 ms).
* The AllStar edge's egress to a real app_rpt node (IAX2 out) is not a listener here.
