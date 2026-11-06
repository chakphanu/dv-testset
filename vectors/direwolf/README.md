<!-- SPDX-License-Identifier: MIT -->
# vectors/direwolf - Dire Wolf as the AX.25 / APRS / KISS reference (HWFREE1 item 3)

Written by `python3 -m dvtest.oracle_direwolf run --out-dir vectors/direwolf` from the
image `dvt-direwolf:local` (`rig/Dockerfile.direwolf`, Dire Wolf 1.8.1, pin `DIREWOLF_*`
in `.env.example`). JSON text only: no audio, no frame dumps. Reading and results:
`docs/direwolf-cases.md`.

- `rx_frames.json` (schema `dvt-direwolf-rx/1`): per TNC2 line the AX.25 frame bytes from
  Dire Wolf's encoder (read back through `atest`), the raw bytes of its KISS TCP port, our
  `dvtest.ax25` result for both, the AFSK sample count and the bit model for it.
- `kiss_tx.json` (schema `dvt-direwolf-kiss-tx/1`): KISS bytes we built or broke, and what
  Dire Wolf's KISS port, its KISSCOPY fan-out, `kissutil` and its log made of them;
  `kissutil_encoder` holds kissutil's TNC2 to KISS bytes against ours.

- `aprs_decode.json` (schema `dvt-direwolf-aprs/1`, `python3 -m dvtest.oracle_aprs run`): the
  information field (hex and text) of 93 APRS frames and what Dire Wolf's `decode_aprs` printed
  and (parsed) decoded for each: the 17 frames above plus 76 strings written from the APRS 1.0.1
  formats for DV-Router's APRS core (`dvtest/aprs_cases.py`). Reading: `docs/direwolf-cases.md`.

  Compare policy (also in the file header, `compare_policy`): every record has `expect`, the GENERATOR's
  exact values (`dvtest/aprs_expect.py`, spec-derived, never taken from Dire Wolf), and
  `expect_vs_decode_aprs`, the mismatches under this policy (empty everywhere). EXACT as encoded: source /
  dest / path, data type, symbol table and code, message addressee / text / id, ack / rej ids, status text,
  object / item name and live / killed flag, course and speed as encoded integers, altitude feet as
  encoded, PHG / RNG / DFS fields, weather fields. Positions: lat / lon equal the generator's exact integer
  (`{"int": n, "per_degree": d}` is n / d degrees) and are within half of the precision `decode_aprs` prints
  (0.00005 minute) of it. Timestamps and ambiguity: generator only (`decode_aprs` does not print them).
  `dao-base91-bare`: the generator value (DAO applied) is the authority, `decode_aprs` does not apply a DAO
  that is the whole comment (`expect.decode_aprs`). Other documented differences: `msg-brace-in-text`,
  `bln-announce-group`, the wind speed of the weather `_ccc/sss` extension (read as knots).

`kiss_tx.json` also holds the DV-Router KISS receive cases (`data-port2`, `data-port15`, `cmd-unknown-8` to
`cmd-unknown-14`, `limit-1100-*` / `limit-1101-*` with and without escapes, `fesc-before-fend`); Dire Wolf's
answers are recorded, DV-Router's side is the model `dvtest.ax25.DvrouterKiss`
(`cases/unit/test_dvr_differences.py`). `python3 -m dvtest.oracle_direwolf run --only tx` rewrites this file only.

`vectors/airtime/anchors.json` (`python3 -m dvtest.airtime`) is the LoRa airtime table;
`vectors/airtime/afsk_bound.json` (same command) is the DV-Router AFSK airtime bound in integer
nanoseconds: `(len, bps, TXDELAY, TXtail, preamble) -> expected_ns`, integer arithmetic only.
