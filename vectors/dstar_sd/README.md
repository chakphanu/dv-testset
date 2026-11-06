<!-- SPDX-License-Identifier: MIT -->
# vectors/dstar_sd - D-STAR slow data, spec-derived overs and two decoders' verdicts (HWFREE1 item 4)

JSON text only: no audio, no frame dumps. Reading and results: `docs/dstar-slowdata-cases.md`.

- `corpus.json` (schema `dvt-dstar-sd-corpus/3`, 63 cases, 19 `unit_vectors`), written by
  `python3 -m dvtest.oracle_dstar_sd run --out vectors/dstar_sd/corpus.json` from the image
  `dvt-dstarsd:local` (`rig/Dockerfile.dstarsd`, pins `DSTARSD_*` in `.env.example`). Per case:
  `frames` ("seq hex6": the 21 frames of each superframe as they go on the wire, frame 0 = sync,
  the rest scrambled), what MMDVMHost's `CDStarSlowData` printed (`mmdvm`: block type bytes, header,
  text), what DStarGateway's collectors printed (`dgw`: text, `gpsa`, `nmea:<ID>`), the same from
  `dvtest.dstar_sd.Decoder` (`ours_reset`, `ours_accumulate`) and the match flags. Events are
  `[kind, frame seq, hex]`. All frames are built from the specification by `dvtest/dstar_sd.py`;
  nothing is a capture. Callsigns are N0CALL / N0TEST, positions are 0 N 0 E.
- `published_structure.json`: the block layout (types and byte counts, no content) of the
  real-radio hex tables of G4KLX's slow data note, which `cases/unit/test_dstar_sd.py` reproduces
  with synthetic strings of the same length. The dumps themselves are not copied (personal data).
- DV-Router fields (stages 1b and 1c, `docs/dstar-slowdata-cases.md`): `unit66_hex` (one 66 byte unit per superframe:
  3 byte presence bitmap, LITTLE endian, LSB first, bit i = frame i, a complete unit starts `ffff1f 552d16`, plus 21 x 3 raw
  bytes; VERIFIED against `dstar.rs:132-136`), `expected_dvrouter` (what the DV-Router code does TODAY: `basis`
  `dvrouter-source`, `text`, `gps`, `outcome` = one `parsed` / `reject:<Name>` per unit), `expected_dvrouter_after_dp5f`
  (only where the planned DP5f rules change it: NMEA 0183 line limit, text cleared at every unit), `lenient_alternative`
  (DStarGateway's events), `ours_dvrouter` (unit model), `named_difference` (key into the top-level `named_differences`).
  `unit_vectors` is a separate section: the reject outcomes of the unit itself (TooShort / Size / Invalid: 65 and 67 byte
  units, spare bits, a wrong frame 0, the retired MSB first layout) and sequences that show the state reset, each with its
  rule (`dstar.rs:line`) and the expected outcome per unit. `dvrouter_source` lists the rules with file:line and what the
  model leaves out.
- `dsdcc.json` (schema `dvt-dstar-sd-dsdcc/1`, stage 2): what DSDcc (image `dvt-dsdcc:local`, pins `DSDCC_*`) shows for each
  case on a synthetic RF stream (`python3 -m dvtest.oracle_dsdcc run`): the changes of its 20 character info text
  and locator per frame, whether the expected and the lenient texts were shown, the final text. Five cases with a
  missing frame are skipped; `--only a,b` runs DSDcc for the named cases and keeps the recorded rows of the others. Nothing from the two published recordings is stored.
