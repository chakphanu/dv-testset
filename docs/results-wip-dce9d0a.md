<!-- SPDX-License-Identifier: MIT -->
# Echo round on dv-router:wip-dce9d0a (request of DV-Router)

Image `dv-router:wip-dce9d0a` (wip-2040fce plus the echo round: seq byte of the echo stream, echo drop counters, DCS RPT2). Native launch,
`DVT_SUT_GEN=c2465de`, no `--capture` unless noted, one rig at a time. This image was superseded by wip-488b89d (COMPAT1,
`docs/results-wip-488b89d.md`) before the full web / NAT / load / soak set was run on it; only the echo-related stages and the AllStar lab ran.

| Run | Out dir | Passed / failed / skipped | Verdict |
|-----|---------|---------------------------|---------|
| AllStar lab (`test_asl_edge.py`, long audio cases) | `20261106T001957Z` | 22 / 0 / 8 | pass |
| Echo E1 to E7 + telemetry, first try | `20261106T003837Z`, `T004555Z` | 30 / 1 / 2 each | the one failure was E7 ("played moved by 9, made 10"): a defect of the CASE (the control over on module M was counted in room e), not of the image; per-room tally in commit 8280dd3 |
| Echo E1 to E7 with the counter trace | `20261106T015625Z` (wip-2040fce `20261106T014318Z`) | 30 / 1 / 2 each | same E7 case defect, found by the trace |
| E9 D-STAR echo integrity, DExtra / DPlus / DCS / G3 / XLX peer | `20261106T005254Z`, `T010641Z`, `T012352Z` | failures of the judge (below) | see below |

## E9: what was found and what is judged now

- Defect B (the seq byte of the echo stream looked random on every image back to wip-0b90596): fixed on wip-dce9d0a; seq == index mod 21 with
  0x40 on the last frame and the sync pattern only at index mod 21 == 0 on all three streams of every kind.
- Defect A (one lost frame, 40 ms gap seen on production): not reproduced in 180 overs on the rig; DV-Router recorded it as probably network.
- The first E9 versions failed on headers because the judge was stricter than the protocols: DPlus stamps the suffix `DPLS` at the edge,
  and a G3 or DCS sender does not send the RPT2 the judge assumed. DV-Router ruled the DCS RPT2 relaxation out; the case now uses
  realistic senders (RPT2 = the kind's own reflector: XRF999 / REF999 / DCS999, `want_rpt2` and `live_unchanged` in `dvtest/echo.py`, commit 20a3e22)
  and the echo RPT2 is asserted for DExtra / DPlus / DCS.
- XLX kind: the tester as XLX peer (`xlx-peer-tester.toml`) is clean; with xlxd as the peer the 40 ms gaps are xlxd's own pacing.
- YSF `result=short` +2: see `docs/results-wip-488b89d.md`, "YSF short".
