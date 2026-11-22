<!-- SPDX-License-Identifier: MIT -->
# M17RL1 cases: the M17 interlink CONN rate limit and its deferred answer

Status: written offline for the image `dv-router:wip-f1f3777` (DV-Router `bc9af2b1`, unit M17RL1 = `307525dc`). **Nothing here has run on a rig.** Case file `cases/test_m17rl1.py`, pure
judgements `dvtest/m17rl1.py`, unit tests `cases/unit/test_m17rl1_lib.py`, switch `DVT_M17RL1` (passed to the tester by `rig/compose.yml`, guarded by `cases/unit/test_tester_env.py`).
Notes: stdout and `out/<run>/m17rl1.txt` (`RECORD`).

**Gate.** `DVT_M17RL1=on`; without it every case skips with the reason. The cases need the sut side with M17 peering (`DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native`, the peers `M17-DVT` /
`M17-DVU` of the template, `--profile interlink`); M17RL1-g needs the real mrefd instead (below).

## What M17RL1 is (read from `docs/design/m17.md` and the commit)

* Every valid inbound `CONN` from a configured peer endpoint gets a 37 byte `ACKN`, linked or not.
* While the row is UNLINKED, that `CONN` also triggers a local `CONN` to the source, but only when 3 s (`CONN_INTERVAL_NS`) have passed since the row last SENT any local `CONN`. The timer and
  the answers together send at most one local `CONN` per row per 3 s.
* A suppressed answer is DEFERRED: pending and backoff stay, the retry deadline moves to the end of the interval (last `CONN` + 3 s), and the `CONN` leaves there (10 ms driver tick), not at the
  later backoff step (3 / 6 / 12 / 24 / 30 s).
* A row that is still linked answers a `CONN` with the `ACKN` alone.
* Why: mrefd links on a received `ACKN` and sends its own `CONN` only from a 10 s link check while the peer is not linked. Without the deferral an mrefd that restarts inside the interval stays
  linked while we are not, up to 30 s.

## The mrefd peer in these cases

The existing M17 peer cases (`cases/proto/test_m17_peer_link.py`, fixture `m17_peers`) play the peers `M17-DVT` / `M17-DVU` at the tester (`dvtest/m17_peer.py`); the real mrefd of the rig (profile `mrefd`,
`DVT_REF_KIND=mrefd`) is judged by the same tester peers (it connects out to them), never linked to the SUT. These cases play `M17-DVT` the way mrefd behaves on the wire: no `CONN` of its own (a `CONN` is
sent by hand when "mrefd starts"), an `ACKN` to every `CONN` of the row, no answer to a `PING`, `DISC` when it stops. That is exactly the exchange m17.md describes, so the three start orders and the
peer restart can be written without a second reflector. A SUT linked to the REAL mrefd is a GAP (below).

## What is asserted

| Case | Statement | Assertion |
|------|-----------|-----------|
| M17RL1-a | every `CONN` gets an `ACKN` | linked: `ACKN` (37 B) within 2 s and NO row `CONN`, the link holds; unlinked (peer silent after a `DISC`): 10 `CONN` 0.5 s apart get 10 `ACKN` |
| M17RL1-b | our `CONN` at most once per row per 3 s | an unlinked row that is never answered gets a valid `CONN` every 0.25 s for 14 s: every one is acked, at least two row `CONN` arrive, no two closer than 3 s less 0.15 s, at most `floor(15 / 3) + 1 = 6` |
| M17RL1-c | a suppressed answer is deferred | after the row's retry `CONN` at T (silent peer) a peer `CONN` at T + 0.3 s gets an `ACKN` within 1.2 s, no row `CONN` before T + 2.85 s, and the next one by T + 3.8 s (the old behaviour answered at once, the next backoff step is 6 s later) |
| M17RL1-d-A | we start while it runs | `DISC` at 0, the peer answers: the row's first `CONN` arrives within 4.5 s (retry 3 s after the loss) and the link comes up (`PING` seen) |
| M17RL1-d-B1 | it starts while we run unlinked, our last `CONN` < 3 s old | peer `CONN` 1 s after ours: `ACKN` at once, the deferred `CONN` 3 s after the last, handshake complete within 3.8 s of the peer `CONN` |
| M17RL1-d-B2 | it starts, our last `CONN` >= 3 s old | peer `CONN` 3.6 s after ours: our `CONN` within 1.2 s, handshake complete within 1.2 s |
| M17RL1-d-C | both start within the same 3 s | peer `CONN` at 2.8 s after the loss (next to our first retry): handshake within 3.8 s of it, row `CONN` spacing >= 3 s |
| M17RL1-e-disc | a peer restart with `DISC` | `DISC`, 0.6 s later its first `CONN`: handshake within 3.8 s (at once when the last `CONN` is old) and the link up |
| M17RL1-e-crash | a peer restart without `DISC` | its first `CONN` reaches a row that still holds the link: `ACKN` alone, no row `CONN` in 3.8 s, the link holds |
| M17RL1-g | calibration, side ref only | what real mrefd does with 25 s of silence after a `DISC` (its `CONN` times) and with a `CONN` of ours: recorded, nothing asserted |

Bounds: 3 s is the protocol interval; 0.15 s is the allowance below it (transit, tester scheduling), 0.8 s above it (10 ms tick, tester poll); "at once" is 1.2 s.

## Run commands

Cases a to e (side sut, the template's tester-played peers):

```
XLXD_SUT_IMAGE=dv-router:wip-f1f3777 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_M17RL1=on \
scripts/run.sh -m rig --profile interlink -- cases/test_m17rl1.py -k "not mrefd"
```

Estimated time (not measured): about 4 minutes (the module-scope fixture links both peers first, up to 40 s; each case drops the link and waits for the row's 3 s retry and a relink, 10 to 30 s each;
the flood of case b is 15 s). One rig, nothing else at the same time.

Case g (calibration, the real mrefd as the reference, compatibility data, `docs/rig.md` "mrefd as an extra reference"):

```
DVT_SUT_PROFILE=xlxd DVT_REF_KIND=mrefd DVT_M17RL1=on scripts/run.sh --profile mrefd -m rig -- cases/test_m17rl1.py -k mrefd
```

Estimated time: about 1.5 minutes plus the build of `dvt-mrefd:local` the first time (`scripts/pre-fetch-mrefd.sh`). `run.sh` refuses `DVT_REF_KIND=mrefd` with a DV-Router SUT.

## GAPs

* **DV-Router linked to the REAL mrefd.** The rig has no such shape: the mrefd service peers with the tester (`M17-DVT` / `M17-DVU` at `.30` / `.32`), the control channel cannot stop or start `mrefd` (the
  allow-list is the transcoders, xlxd, allstar and the asl hosts), and `DVT_REF_KIND=mrefd` is refused with `DVT_SUT_PROFILE=dvrouter`. The three start orders with the real daemon need: (1) a snippet with
  a third `[[edges.m17.peers]]` row `host = "<NET>.80"` (the token `<NET>` exists), `callsign = "M17-REF"`; (2) `rig/mrefd-entry.sh` writing the SUT (`<net>.11:17000`, designation `M17-DVR`) into its
  interlink file; (3) `stop|start|restart|kill mrefd` in the control channel of `scripts/run.sh`. Not done: it changes the run script and the mrefd entry and cannot be tried offline.
  Whether mrefd matches an interlink peer by the callsign in the file or only by IP is unknown to us (m17.md: "mrefd keys peers by IP only").
* The M17 edge cannot be restarted from outside except through the admin API (`kill-proc` has no `dv-edge-m17`): order A is made by a `DISC` instead of a start of the edge, so "the edge starts" itself
  (first `CONN` after a start, name resolution) is not covered.
* The per-row state `last local CONN` is internal; the 3 s rule is judged from the `CONN` times the peer sees, with a 0.15 s allowance. A fault that sent two `CONN` 2.9 s apart would pass.
* A retry pending at 30 s (maximum backoff) behind a suppressed answer is not reached (it would need five failed attempts, about 45 s of silence per run): the deferral is proven at the 3 s and 6 s steps.

## Questions for the DV-Router team

1. Does mrefd match an interlink peer by the designation of the interlink file entry or by IP only? With IP only, the SUT could be added to mrefd's interlink file under any name.
2. Is the `PING` interval of a linked row 3 s (`PING_INTERVAL` of our peer model) and what does the row do after 10 s without `PONG` or `PING`? M17RL1-e-crash relies on "linked rows answer an
   ACKN alone" for the duration of the case only.
