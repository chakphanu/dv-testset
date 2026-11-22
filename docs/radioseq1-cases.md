<!-- SPDX-License-Identifier: MIT -->
# DV-Router RADIOSEQ1: the radio hub numbers received voice by frame ordinal

Status: written offline against DV-Router `917b9e3b` (image `dv-router:wip-37b7960`), NOT run on a rig. The cases are in `cases/test_rif5_mmdvm_serial.py` (section RADIOSEQ1), the judges in
`dvtest/radioseq1.py`, their offline tests in `cases/unit/test_radioseq1_lib.py`. Switch: `DVT_RADIOSEQ1=on` (documented in `dvtest/target.py`, passed to the tester by `rig/compose.yml`).

## The defect and the fix

On `bc9af2b1` (`dv-router:wip-f1f3777`) the radio edge (`mmdvm-serial`) numbered received DMR, YSF, NXDN and P25 voice in 20 ms units instead of frame ordinals
(`hub_dmr.rs:195` += 3, `hub_nxdn.rs:183,195` += 4, `hub_p25.rs:37` += 9; YSF 5). Evidence, RIF5J run `20261122T075521Z` (`capture/xlxd-sut.pcapng`, `logs/xlxd-sut.log`): a lossless 150 word radio DMR over reached the
tester's DMR Homebrew client with the voice sequence letters only A (sync) and D (voice) instead of A..F (DMRD byte 15: bits 5-4 frame type, bits 3-0 voice sequence); the room `over_gap` counted 100 for that over (2 per burst), the
over was `over_lossy`; YSF counted 4 gap per frame; D-STAR 0. RIF5N run `20261122T160037Z`: an NXDN radio over heard by a DMR client got 78 of 148 words (`dmrhb EgressDropped 36`).

`917b9e3b` (radio.md "Built (RADIOSEQ1)", media-format.md "Unit granularity", status.md `dv_room_over_units_total`): the DV `v` sequence is the zero-based native frame ordinal per over, one step per DMR burst,
YSF frame, NXDN frame or P25 LDU, including a silence fill; D-STAR and PCM are unchanged; the radio no longer states `RTS_FRAME_POSITIONS`. A gapless over has `gap = 0`; an accepted undecodable RF frame
replaced by silence is not a position gap. The fix does not infer frames the serial link never delivered.

## Cases

One test, parametrized: `test_radioseq1_rx_over_is_numbered_by_frame[dstar|dmr|ysf|nxdn|p25]` (ids `RADIOSEQ1-RX-<mode>`), and `test_radioseq1_c_radio_to_radio_dmr` (id `RADIOSEQ1-C`, item c, with the second port). Each RX case plays ONE lossless RF
over of 3 s from the fake modem (D-STAR, DMR, YSF 150 words; NXDN 148; P25 144, the shapes of RIF5-RX / RIF5N-RX), with clients of the room as listeners, and reads the hub counters (up to 10 s old) before and after.
NXDN and P25 need `DVT_RIF5N=on` and `rif5n-modes.toml` (they skip otherwise, like every RIF5N case). Lossless on the air: the fake sends every frame; the cases do not damage anything.

| Item | Case | Listener | Judge | Asserted with `DVT_RADIOSEQ1=on` |
|------|------|----------|-------|-----------------------------------|
| (a) DMR letters | RX-dmr | DMR (DMRD) client of room Q | `radioseq1.superframe_verdict` over the DMRD flags byte 15 | the first voice burst is A, every next one is the following letter (F then A), sync (frame type 1) on A and on no other letter, none repeated, none skipped, one stream; one over, every word (same words as the modem sent) |
| (a) cross-mode | RX-nxdn | DMR client of room S (NXDN -> DMR, no transcoder) | the same, plus `nxdn.to_dmr` of the 49-bit words sent | the same letters; one over; all 148 words in order (78 of 148 on `bc9af2b1`); `dv_edge_over_units_total{dir=tx,result=dropped}` grew by 0 |
| (b) counters | RX-dstar (control), RX-dmr, RX-ysf, RX-nxdn, RX-p25 | none needed (D-STAR has no listener) | `radioseq1.counts` / `judge_counts` (before and after, `medload1.wait_settled`) | `dv_edge_over_units_total{dir=rx,proc=radio,result=gap}` grew by 0, `dv_room_over_units_total{room,result=gap}` by 0, `dv_room_overs_total{result=lossy}` by 0, and the over closed in the room (`complete`; `end_missing` is a FINDING) |
| (c) radio to radio | `test_radioseq1_c_radio_to_radio_dmr` | second fake modem (board B) | `rif5.judge_tx_dmr` + `radioseq1.relay_verdict` over the host frames of the receiving board | both directions, see below |
| (d) YSF FN | RX-ysf | YSF client of room R | `radioseq1.ysf_fn_check` of the FICH FN (and FT) of every voice packet | FN advances by +1 modulo 7 (FN 0..6, FT 6: dv-router `crates/edge/ysf/src/wire.rs` `VOICE_FT`, `voice_fn`), no repeat, no skip. FN 7 on a voice packet (modulus 8) is a FINDING, not a failure; an FT other than 6 on a voice packet is a FINDING; the modulus seen (7, 8 or none) is RECORDed |
| (d) NXDN SACCH | RX-nxdn | NXDN client of room S (heard together with the DMR client in the SAME over) | the SACCH structure (payload byte 1, bits 7-6) of every voice packet | 3, 2, 1, 0, 3, 2, 1, 0 ... (NXDN [1-B] 4.1.6; DV-Router's own test `radioseq1_claims.rs` pins the same): each packet one step down modulo 4 |
| P25 egress | RX-p25 | P25 client of room T | `rif5n.heard_p25` | not asserted beyond the counters: the record types, the lifecycle problems and the word count are RECORDed (the P25 egress numbering is not decoded) |

With the switch off or unset the same cases run on any image: they play the over and only RECORD (`out/<run>/rif5.txt`, lines `[RECORD] RADIOSEQ1-RX-<mode>: ...`): the letter histogram and its first 24 letters
(`{'A': 25, 'D': 25}` on `bc9af2b1`), the repeated / skipped / sync counts, the FN and SACCH steps, the words wanted and heard, the counter deltas, and for every difference the switch would assert a line
`not asserted (DVT_RADIOSEQ1 is off): ...`. Only a harness failure (the modem could not play the over) fails a case with the switch off, so an older image keeps its current verdicts.

### (c) between radio ports: RADIOSEQ1-C

dv-router's answer: two `mmdvm-serial` ports may serve the same room for the same mode. The validator (`crates/lib/config/src/radio_validate.rs`, `radio_mmdvm.rs`) asks for a distinct `name` and a distinct byte link per row (`device`, or `udp_bind` here: "is already used by edges.radio.port[n]"), a shared `physical`
must agree on `band`, and one physical is one transmitter, so the second row has its OWN `physical`. At run time every circuit is its own client session and the fan-out skips only the origin circuit (`crates/edge/radio/src/hub.rs`, `crates/lib/edge/src/core.rs`): an over received on port A is
transmitted by port B when B has `tx` on, and the reverse. `tx_ack` is a key of the EDGE (`[edges.radio]`, config.md), so the one sentence of `rif5-mmdvm-udp.toml` / `rif5n-modes.toml` covers both rows; the key names `name`, `kind`, `physical`, `tx`, `band`, `rx_freq`, `tx_freq`, `udp`, `udp_bind`, `modes`,
`hang`, `timers`, `id`, `dmr` are those of config.md "[[edges.radio.port]]".

How the second board is hosted: the first fake modem is a UDP byte port pair (`PinnedUdp`, tester `:3340`, SUT `udp_bind :3341`), a pure tester-side socket, so a second board is the same thing on `:3342` / `:3343`. No compose service, no published port, no change of `scripts/run.sh`:

* `rig/config/dv-router/rif5-two-ports.toml`: ONLY the second row `rif5-modem-b` (`physical = "rif5-modem-b"`, band 70cm, 434.5 MHz, `modes = { dmr = "Q" }`, `dmr = { color_code = 1, tg = 4017 }`, hang 1 s, ID off). It is an ADDITION: name it after `rif5-mmdvm-udp.toml` or `rif5n-modes.toml`
  (`merge_extra` appends the array of tables; `radioseq1.port_pair` takes the row `rif5-modem` as A and `rif5-modem-b` as B in any order).
* `cases/test_rif5_mmdvm_serial.py` fixture `ctx`: when the rendered config has the row, a second modem of the same class as the first (`Rif5Modem`, or `FwRif5Modem` under `DVT_RIF5FW=on`) is started on `:3342` with the first (`ctx.modem_b`, `ctx.port_b`), so both ports open together; without the row nothing changes.

The case `RADIOSEQ1-C` (`test_radioseq1_c_radio_to_radio_dmr`): it skips with the reason when the rendered config has no row `rif5-modem-b` (or the pair is unfit: `radioseq1.pair_problems` names a shared name, `udp_bind` or `physical`, another room, `tx` off); it fails when board B gets no SET_CONFIG within 150 s.
Then, both ports Up and idle, with the hub counters read before (11 s settle, as the RX cases):

1. A -> B: board A plays ONE lossless RF DMR over (source 1023007, TG 4017, colour code 1; 18 words, one superframe, or 144 words = 8 superframes with `DVT_RIF5J=on`, the lengths the host flow control of the image carries without a drop, as RIF5-TX-dmr and RIF5-J-LONG). Board B must get SET_MODE 2, eight voice LC headers (group 4017,
   colour code 1, one source id), the voice bursts with the letters A..F in order (the voice sync on A alone; `radioseq1.dmr_tx_letters` counts the letters from the sync burst), every word of the over in order (`rif5.judge_tx_dmr`), three terminators. The source id carried is RECORDed (a FINDING if it is not 1023007).
2. B -> A: the same over played on board B; board A must get the same.
3. The origin board must not get its own over back: no RF-type host frame (`rif5.RF_TYPES`: voice, header, EOT, CW ID, DMR control) on the origin board after its own over, in either direction.
4. The hub counters over both overs (`dv_edge_over_units_total{dir=rx,proc=radio,result=gap}`, `dv_room_over_units_total{room=q,result=gap}`, `dv_room_overs_total{result=lossy}`): +0, an over closed in the room.

`DVT_RADIOSEQ1=on`: every difference is a problem and fails the case. Off or unset: the same over is played and only RECORDed (`[RECORD] RADIOSEQ1-C: A->B: board rif5-modem-b got the over, voice letters ...`, `not asserted (DVT_RADIOSEQ1 is off): ...`); a harness failure (the modem could not play, the board never opened) still fails.
Port B's TX of an RF over of room Q also happens in every other case that plays DMR RF on A when the snippet is in the config, so the evidence of the other RIF5 cases and of RIF5-M1 (tallies of port A only) is taken WITHOUT `rif5-two-ports.toml`.

## Run it

Shape A of docs/rif5-cases.md ("The image wip-f1f3777", `R=rig/config/dv-router`, as `.ff11.sh` rif5n-A and `.ff12.sh` rif5n-p25 of the hold run), only the new cases (about 8 minutes without C: the rig 3, five overs at 11 s + 3 s + about 20 s of counter wait):

```sh
R=rig/config/dv-router
B=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml
XLXD_SUT_IMAGE=dv-router:wip-37b7960 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> DVT_SUT_GEN=compat1 \
DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
DVT_RADIOSEQ1=on DVT_RIF5=on DVT_RIF5J=on DVT_RIF5N=on DVT_RIF5FW=on DVT_P25_MODULE=T DVT_P25_TG=31681 DVT_SUT_NAME=dv-router \
DVT_SUT_CONFIG_EXTRA=$B:$R/rif5n-modes.toml:$R/rif5-two-ports.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_rif5_mmdvm_serial.py -rA -k radioseq1
```

`rif5-two-ports.toml` (last) is what RADIOSEQ1-C needs; without it that case skips and the five RX cases are the same. About 10 minutes with C (the 11 s counter settle, two overs of 3 s at most, two waits of 1.5 s and about 20 s of counter wait, plus the second port's open).

The whole file in one run (as rif5n-A: the new cases sit before the D cases and RIF5-M1, whose tally counts their five RF overs) is the same command without `-k radioseq1`. For the evidence run on the previous image
(`wip-f1f3777`, code `bc9af2b1`) leave `DVT_RADIOSEQ1` unset: the cases record the letter histogram and gap values; the notes are in `out/<run>/rif5.txt`. The counters need the rig control channel (`DVT_RIG_CONTROL=1`, set by
`scripts/run.sh`); without it the letters, FN and SACCH are still judged and the counters are not read.

## Expected on `wip-37b7960`

All five RX cases and the letters pass; the YSF FN is +1 modulo 7 (FN 7 on a voice packet would be a FINDING line, not a failure); (c) passes with `rif5-two-ports.toml` in the extras and skips without it. On `wip-f1f3777` with the switch on the DMR letters fail with `{'A': n, 'D': n}`, the NXDN -> DMR words fail (78 of 148), the gap counters fail (about 2 per DMR burst, 4 per YSF frame,
NXDN and P25 per frame), D-STAR passes (control).

## Not asserted, and questions

* Not asserted: nothing about the YSF FN modulus beyond +1 per voice packet (modulus 7 is the answer of dv-router; modulus 8 with FN 7 is a FINDING and is RECORDed as seen); the NXDN SACCH start value (the first structure is RECORDed, the steps are asserted); P25 egress numbering; the DMRD sequence byte 4 (neither judged nor
  recorded); timing of the bursts; TX direction (room to modem) numbering, which RIF5-TX / RIF5-J-LONG already judge on the modem side.
* A frame the serial link never delivered, an RF frame the modem marks lost and a damaged copy (RIF5-J garbled copies, `rx_fill`) are outside these cases: the fix "does not infer missing serial frames", so they are not lossless on the air.
* Answered by DV-Router: the FN of a YSF egress voice packet cycles 0..6 (FT 6; FT 7 is header / terminator); two `mmdvm-serial` ports may serve the same room for the same mode, with distinct name, byte link and physical (one physical is one transmitter).
* Open: the source id the second board's headers carry for a radio-origin over (RECORDed; a FINDING when it is not the RF source id of the over); the lengths of C (18 words, 144 with `DVT_RIF5J=on`) are the flow-control limits of RIF5-TX / RIF5-J-LONG, not a property of the relay.
* Not run: the case and the snippet are written offline (unit tests of the judges and of the snippet only); the first rig run may show harness corrections.
