<!-- SPDX-License-Identifier: MIT -->
# vectors/dstar_sd - D-STAR slow data, spec-derived overs and two decoders' verdicts (HWFREE1 item 4)

JSON text only: no audio, no frame dumps. Reading and results: `docs/dstar-slowdata-cases.md`.

- `corpus.json` (schema `dvt-dstar-sd-corpus/1`), written by
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
