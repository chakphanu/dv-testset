<!-- SPDX-License-Identifier: MIT -->
# Acceptance of dv-router:wip-239382b

Image `dv-router:wip-239382b`, id `sha256:d7b8ce9dbc31...` (local build; tar sha256
`121b9f728652718b4beba836781b7ae86aeae7b687cf3af7e2d87803a28a3688`; deployed on XLX345; rollback target `wip-72afd0d`). It carries LEAK2
(see `docs/results-wip-3075fb3.md`) and host-name peer rows for XLX, NXDN and P25 (SANSIO4). Native launch, `DVT_SUT_GEN=c2465de`,
`DVT_USRP_DYNAMIC=1`, no `--capture`, one rig run at a time, run from a separate worktree.

**Not confirmed by a human:** the first real over on XLX345 after the deploy has not been confirmed by a person; nothing here replaces that.

## Summary

| # | Run | Out dir | Passed / failed / skipped | Duration | Verdict |
|---|-----|---------|---------------------------|----------|---------|
| 1 | AllStar lab T3 T5 T6 T7 T8 (+T8b) T10 T11 (`DVT_ASL_EDGE_OUT=1`, `DVT_ASL_EDGE_LOOKUP=1`, `--profile asl-edge -m asl_lab`) | `20261105T132622Z` | 10 / 0 / 0 (20 deselected) | 2 min 24 s | pass |
| 2 | Listen-load W8 (`test_web_load.py`) | `20261105T132939Z` | 5 / 0 / 0 | 12 min 10 s | pass: edge cpu 11.0 %, VmHWM 32.5 MiB, worst-listener jitter p99 1.53 ms / max 10.2 ms, 0 lost (the first start `T132912Z` stopped on the buildkit snapshot error of the `dvt-webload` image build, rebuilt with `--no-cache`) |
| 3a | Web API + smoke | `20261105T134213Z` | 375 / 0 / 0 | 5 min 29 s | pass |
| 3b | TOTP1 optional | `20261105T134749Z` | 14 / 0 / 0 | 2 min 36 s | pass |
| 3c | WebRTC + recorder, first run | `20261105T135033Z` | 54 / 1 / 0 | 13 min 00 s | one failure: `test_listen_receives_the_tone[webkit]` (the known WebKit tone glitch: peak at 3600 Hz instead of 700 Hz) |
| 3d | Rerun of `test_listen_receives_the_tone` | `20261105T142729Z` | 3 / 0 / 0 (33 deselected) | 1 min 21 s | pass (3 of 3, load flake) |
| 3e | K22 / K22b / EDG1 | `20261105T140341Z` | 13 / 0 / 0 | 23 min 22 s | pass |
| 4a | XLX peer rows by host name, first runs (`xlx-peer-host`, `xlx-peer-host-bad`, literal `xlx-peer`) | `20261105T142907Z`, `T143559Z`, `T144251Z` | 12 / 1 / 1 each | 6 min 44 s each | the same one failure in all three, also with the literal IP: `test_interlink_no_xlx_transit`, a defect of the snippets (no XLX997 row), not of the image or of host names |
| 4b | The same with the XLX997 row (`xlx-peer2-host`, `xlx-peer2`; commit `3456c97`) | `20261105T174642Z`, `T175233Z`, `T175824Z` | 13 / 0 / 1 each | 5 min 43 s each | pass (the skip is the cross-family case, needs `--profile interlink,nxdn`) |
| 4c | NXDN peer row by host name (`nxdn-peer-host`, `-bad`; `test_edge_relink.py -k nxdn`) | `20261105T180416Z`, `T180551Z` | 2 / 0 each | 1 min 28 s, 1 min 48 s | pass |
| 4d | P25 peer row by host name (`p25-peer-host`, `-bad`; `-k p25`) | `20261105T180746Z`, `T180905Z` | 2 / 0 each | 1 min 11 s, 1 min 32 s | pass |
| 5a | SOAK1 standard (600 s warmup + 3000 s measured) | `20261105T145022Z` | 0 / 1 / 0 | 69 min 19 s | only `rss:dv-svc-status` fails by the whole-window rule (see below); everything else ok |
| 5b | Long soak (600 s warmup + 5100 s measured, `DVT_SOAK_WALL_CAP_S=6600`) | `20261105T160102Z` | 0 / 1 / 0 | 104 min 23 s | same single row; the plateau is shown (see below) |
| T13 | S3 superset (`-m "(rig or allstar) and not web and not load" --profile allstar,interlink,nxdn,p25`, import launch) | `20261105T181115Z` | not completed | stopped at 21 % | stopped on purpose after 39 minutes so that the gate on the unreleased `wip-2040fce` could use the rig; the part that ran had only the 2 known reference-side `test_g3` failures; to be rerun in full when wanted |

Host-name behaviour seen in the SUT logs of runs 4b to 4d: a row whose host resolves logs `static peer N: <host>:<port> resolved to <address>:<port>` at
start (and again after an edge restart); a row that does not resolve stays pending and logs `cannot resolve <host>:<port>: ... retry next poll`
every 5 seconds, without disturbing the other rows. Not covered: a name that only resolves later (the rig cannot start a peer later; optional).

## SOAK1: why the whole-window rule failed a flat plateau

| Run | dv-svc-status slope over the measured part | counted hub events at the end | slope of the last 30 / 20 / 10 min |
|-----|--------------------------------------------|-------------------------------|--------------------------------------|
| standard, 50 min | 119.5 KiB/min (5976 KiB, limit 5120) | 9213: ring (10000) not full | 118.2 / 117.9 / 117.6 KiB/min (still filling) |
| long, 85 min | 85.8 KiB/min (7295 KiB, limit 5120) | 14483: ring full after about 55 min | 4.17 / 2.09 / -1.79 KiB/min (flat) |

Ten-minute means of the long run, KiB: 15330, 16529, 17746, 18929, 20066, 21056, 21264, 21295, 21301: the RSS of `dv-svc-status` fills the 10000 event
ring (about 1.16 KiB per event, 150 events a minute) and then stays at about 21.2 MiB. The failure of the whole-window rule on that plateau was a
judge defect, not a product defect; the judge now uses a ring rule (`docs/web-soak-cases.md`, row 2b; commit `8144611`). Other rows of the long run:
`dv-router` 2.59 KiB/min over 85 minutes (last 30 minutes 0.84), `dv-edge-webrtc` 14.7 KiB/min over the window (falling at the end), threads and fds flat,
564 generator sessions with 0 failed. Standard run: `dv-router` 0.62, every `dv-room.*` 0.05 to 0.28, `dv-edge-webrtc` 24.1, every other child below 0.35 KiB/min.
