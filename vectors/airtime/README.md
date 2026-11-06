<!-- SPDX-License-Identifier: MIT -->
# vectors/airtime - LoRa time-on-air anchors (HWFREE1 item 3)

`anchors.json` (schema `dvt-airtime-anchors/1`): regenerated with
`python3 -m dvtest.airtime`; no container. Each row has the inputs, the expected
milliseconds, the number as a third party printed it (`published`, for the four
`external` rows), the value of `dvtest.airtime.time_on_air` (exact) and of the
integer-microsecond path (`us_style_us`). Row kinds and what they are worth are in
`docs/direwolf-cases.md` (section "Airtime anchors").
