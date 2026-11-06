<!-- SPDX-License-Identifier: MIT -->
# vectors/lora_gw - scripted raw TNC2 streams for the LoRa APRS gateway path (HWFREE1 item 3)

Written by `python3 -m dvtest.lora_gw write` (pure Python, no container). Hex only.
Every expectation is UNVERIFIED; reading and use: `docs/lora-gw-cases.md`.

- `streams.json` (schema `dvt-lora-gw-streams/1`): 62 scenarios x 2 profiles (empty prefix, `3cff01`):
  the reads to send and the lines that must be accepted, must be rejected, or are not decided.
- `lines.json` (schema `dvt-lora-gw-lines/1`): the three synthetic lines of the plan as TNC2 text, host-link
  bytes, LoRa payload bytes and time on air.
