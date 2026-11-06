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

`vectors/airtime/anchors.json` (`python3 -m dvtest.airtime`) is the LoRa airtime table.
