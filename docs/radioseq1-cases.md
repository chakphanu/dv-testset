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

One test, parametrized: `test_radioseq1_rx_over_is_numbered_by_frame[dstar|dmr|ysf|nxdn|p25]` (ids `RADIOSEQ1-RX-<mode>`), and one skip `test_radioseq1_c_radio_to_radio_dmr`. Each RX case plays ONE lossless RF
over of 3 s from the fake modem (D-STAR, DMR, YSF 150 words; NXDN 148; P25 144, the shapes of RIF5-RX / RIF5N-RX), with clients of the room as listeners, and reads the hub counters (up to 10 s old) before and after.
NXDN and P25 need `DVT_RIF5N=on` and `rif5n-modes.toml` (they skip otherwise, like every RIF5N case). Lossless on the air: the fake sends every frame; the cases do not damage anything.

| Item | Case | Listener | Judge | Asserted with `DVT_RADIOSEQ1=on` |
|------|------|----------|-------|-----------------------------------|
| (a) DMR letters | RX-dmr | DMR (DMRD) client of room Q | `radioseq1.superframe_verdict` over the DMRD flags byte 15 | the first voice burst is A, every next one is the following letter (F then A), sync (frame type 1) on A and on no other letter, none repeated, none skipped, one stream; one over, every word (same words as the modem sent) |
| (a) cross-mode | RX-nxdn | DMR client of room S (NXDN -> DMR, no transcoder) | the same, plus `nxdn.to_dmr` of the 49-bit words sent | the same letters; one over; all 148 words in order (78 of 148 on `bc9af2b1`); `dv_edge_over_units_total{dir=tx,result=dropped}` grew by 0 |
| (b) counters | RX-dstar (control), RX-dmr, RX-ysf, RX-nxdn, RX-p25 | none needed (D-STAR has no listener) | `radioseq1.counts` / `judge_counts` (before and after, `medload1.wait_settled`) | `dv_edge_over_units_total{dir=rx,proc=radio,result=gap}` grew by 0, `dv_room_over_units_total{room,result=gap}` by 0, `dv_room_overs_total{result=lossy}` by 0, and the over closed in the room (`complete`; `end_missing` is a FINDING) |
| (c) radio to radio | `test_radioseq1_c_radio_to_radio_dmr` | second fake modem | none | SKIP, see below |
| (d) YSF FN | RX-ysf | YSF client of room R | `radioseq1.cyclic_progression` of the FICH FN of every voice packet | FN advances by +1 modulo 7 (FN 0..6, as `ysf.stream_packets`) or modulo 8 (the 3-bit field), no repeat, no skip |
| (d) NXDN SACCH | RX-nxdn | NXDN client of room S (heard together with the DMR client in the SAME over) | the SACCH structure (payload byte 1, bits 7-6) of every voice packet | 3, 2, 1, 0, 3, 2, 1, 0 ... (NXDN [1-B] 4.1.6; DV-Router's own test `radioseq1_claims.rs` pins the same): each packet one step down modulo 4 |
| P25 egress | RX-p25 | P25 client of room T | `rif5n.heard_p25` | not asserted beyond the counters: the record types, the lifecycle problems and the word count are RECORDed (the P25 egress numbering is not decoded) |

With the switch off or unset the same cases run on any image: they play the over and only RECORD (`out/<run>/rif5.txt`, lines `[RECORD] RADIOSEQ1-RX-<mode>: ...`): the letter histogram and its first 24 letters
(`{'A': 25, 'D': 25}` on `bc9af2b1`), the repeated / skipped / sync counts, the FN and SACCH steps, the words wanted and heard, the counter deltas, and for every difference the switch would assert a line
`not asserted (DVT_RADIOSEQ1 is off): ...`. Only a harness failure (the modem could not play the over) fails a case with the switch off, so an older image keeps its current verdicts.

### (c) between radio ports: SKIP, and what it needs

The rig has ONE radio port: every `rif5*.toml` defines exactly one `[[edges.radio.port]]` (`rif5-modem`, `udp <tester>:3340`, `udp_bind <sut>:3341`) and the tester plays one fake modem (`ctx.modem`). The case records the number of
`mmdvm-serial` rows of the rendered config and skips. To run it: a snippet `rif5-two-ports.toml` with a second `mmdvm-serial` row (own `name`, `physical`, a second `udp` / `udp_bind` pair such as :3342 / :3343) serving the same DMR
room, a second `Rif5Modem` in the tester, and the letters read from the host frames of the second modem (`rif5.judge_tx_dmr` already reads them). Nothing of this is invented in the repository because the comments of
`rif5n-modes.toml` say the modes of one port serve different modules and the documents do not say whether two ports may share a room.

## Run it

Shape A of docs/rif5-cases.md ("The image wip-f1f3777", `R=rig/config/dv-router`, as `.ff11.sh` rif5n-A and `.ff12.sh` rif5n-p25 of the hold run), only the new cases (about 8 minutes: the rig 3, five overs at 11 s + 3 s + about 20 s of counter wait):

```sh
R=rig/config/dv-router
B=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/data-ident.toml
XLXD_SUT_IMAGE=dv-router:wip-37b7960 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_CONFIG=<template path> DVT_SUT_GEN=compat1 \
DVT_TRANSCODER_FAKE=1 DVT_REQUIRE_IDENT=true \
DVT_RADIOSEQ1=on DVT_RIF5=on DVT_RIF5J=on DVT_RIF5N=on DVT_RIF5FW=on DVT_P25_MODULE=T DVT_P25_TG=31681 DVT_SUT_NAME=dv-router \
DVT_SUT_CONFIG_EXTRA=$B:$R/rif5n-modes.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_rif5_mmdvm_serial.py -rA -k radioseq1
```

The whole file in one run (as rif5n-A: the new cases sit before the D cases and RIF5-M1, whose tally counts their five RF overs) is the same command without `-k radioseq1`. For the evidence run on the previous image
(`wip-f1f3777`, code `bc9af2b1`) leave `DVT_RADIOSEQ1` unset: the cases record the letter histogram and gap values; the notes are in `out/<run>/rif5.txt`. The counters need the rig control channel (`DVT_RIG_CONTROL=1`, set by
`scripts/run.sh`); without it the letters, FN and SACCH are still judged and the counters are not read.

## Expected on `wip-37b7960`

All five RX cases and the letters pass; (c) skips. On `wip-f1f3777` with the switch on the DMR letters fail with `{'A': n, 'D': n}`, the NXDN -> DMR words fail (78 of 148), the gap counters fail (about 2 per DMR burst, 4 per YSF frame,
NXDN and P25 per frame), D-STAR passes (control).

## Not asserted, and questions

* Not asserted: the YSF FN modulus (7 or 8 both accepted, the one found is RECORDed); the NXDN SACCH start value (the first structure is RECORDed, the steps are asserted); P25 egress numbering; the DMRD sequence byte 4 (neither judged nor
  recorded); timing of the bursts; TX direction (room to modem) numbering, which RIF5-TX / RIF5-J-LONG already judge on the modem side.
* A frame the serial link never delivered, an RF frame the modem marks lost and a damaged copy (RIF5-J garbled copies, `rx_fill`) are outside these cases: the fix "does not infer missing serial frames", so they are not lossless on the air.
* Question for DV-Router: the FN modulus of a YSF egress (0..6 or 0..7)? Question: may two `mmdvm-serial` ports serve the same room (item c)?
