<!-- SPDX-License-Identifier: MIT -->
# vectors/lora_gw - scripted raw TNC2 and u16 binary streams for the LoRa APRS gateway path (HWFREE1 item 3)

Written by `python3 -m dvtest.lora_gw write` (pure Python, no container). Hex only.
Expectations are VERIFIED by reading the DV-Router source (tree `DV-Router-wip` commit `1bb304f0`; the `file:line`
list of each scenario is in its `sources`), not by running a gateway. Reading and use: `docs/lora-gw-cases.md`.

- `streams.json` (schema `dvt-lora-gw-streams/2`): 85 scenarios x 2 profiles (empty prefix, `3cff01`), 64 with text
  framing and 21 with binary (u16 big-endian length) framing: the reads to send, the frames that must be delivered
  (`accept_hex`), the frames that must not (`reject_hex`), the edge bad-frame count (`edge_bad_frames`), the lines the
  edge delivers and the worker's TNC2 parser refuses (`worker_invalid_hex`) and whether the adapter closes the
  connection (`connection_closed`).
- `lines.json` (schema `dvt-lora-gw-lines/1`): the three synthetic lines of the plan as TNC2 text, host-link
  bytes, LoRa payload bytes and time on air.
