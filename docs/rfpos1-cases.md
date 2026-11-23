<!-- SPDX-License-Identifier: MIT -->
# RFPOS1 step B cases: a frame lost on a node link keeps its position

Status: rig run `20261123T093254Z` (testset `08afc6e`, image `dv-router:wip-c3b24d2`, `--capture --profile twonode`, fake transcoder): **3 passed in 97 s**, dvcheck 0 errors (warnings: the PING to the stopped `transcoder-sut`, expected, and the link handshake datagrams that no protocol claims). Written offline first for the image `dv-router:wip-c3b24d2` (DV-Router code `5333e4fc`; RFPOS1 step B = `47dac7b4`). Case file `cases/test_rfpos1_link.py`,
pure helpers and the link relay `dvtest/rfpos1.py`, unit tests `cases/unit/test_rfpos1_lib.py`, switch `DVT_RFPOS1B_LINK` (passed to the tester by `rig/compose.yml`, guarded by
`cases/unit/test_tester_env.py`). Notes: stdout and `out/<run>/rfpos1.txt` (`RECORD`, `FINDING`).

**Gate.** `DVT_RFPOS1B_LINK=on`, `--profile twonode`; without them every case skips with the reason (the module fixture `link` also skips when the node link does not come up within 150 s,
naming what the relay saw).

## What 47dac7b4 is (read from the sources)

"Link edge" is DV-Router's node link, `dv-edge-link` (edge 20, `[edges.link]`, `docs/design/link.md`): an authenticated UDP link between two nodes, one side an
`[[edges.link.uplink]]` (dials, maps local rooms to remote rooms), the other an `[[edges.link.account]]` (accepts, per-account room list, `talk = true|false`, a 32-byte key in a
0600 `secret_file`). It is NOT the XLX interlink of `--profile interlink` / `twonode` (DP15 7b-7d).

Wire (`link.md` Appendix A): `type u8, session u32, counter u64, body, tag[16]` (HMAC tag, no encryption). Record 20 `Units` = `stream_id u32, profile u16, sequence u64, payload_len u16,
payload`. Link profile ids: ambe-dstar 1, ambe2-dmr 2, codec2-3200 3, imbe 4, pcm-s16le-8k 5. Before 47dac7b4 the receiving side treated a link stream like any other bundle source and the
room's transcoder numbered its output by arrival; after it:

* `sequence` is the sender's 20 ms position of the unit (fabric `media_seq` times the stream's bundle width, plus the unit index); the sender puts ONE unit in each record.
* the receiving link edge states `RTS_FRAME_POSITIONS` (`room_core.rs rts_flags`), the edge core keeps that bit for a non-D-STAR source (an `ambe-dstar` source is positioned by profile),
  and the controller restores the source `media_seq` of every transcoded unit (media-format.md "Transcoded profiles"), so a unit lost on the link is a GAP at its own position, and a link
  stream that began inside the remote over starts above zero.

The product's own test (`crates/edge/link/tests/support/frame_positions.rs`) drops the record with a given wire sequence in a relay; this case does the same black-box.

## Finding of the first rig run (20261123T092713Z): a node with a codec worker CARRIES the demanded profile

Case b failed (`missing positions [], wanted [40]`) and the verdict is HARNESS, not product. `link.md` "Demand and transcoding": the sender always sends the source profile PLUS every profile of
the receiver's demand set that ITS OWN room publishes (the output of its codec worker), announced in the StreamStart carried mask; the receiver publishes carried units directly and never transcodes
them. With `transcoder-sut` running, node 1 transcoded DMR to D-STAR itself (the fake gives the same bytes as node 2's, hence "byte for byte") and sent it as a carried ambe-dstar stream: the relay
counted 455 Units records for three overs (91 ambe2-dmr + 91 carried ambe-dstar for each of a and b, 91 for c) and the dropped ambe2-dmr record 40 was irrelevant, node 2's transcoder never saw the
over. The XLX interlink was not involved (room G is not on it). The case therefore stops `transcoder-sut` for the module (fixture `worker_off`, dvcheck mark `ambed-fault-rfpos1b-node1-worker-off`):
node 1 then sends the source profile only (the hotspot uplink shape; carried mask 0x0000, 91 ambe2-dmr records, none of ambe-dstar) and node 2's own transcoder makes the D-STAR output, which is the
path RFPOS1 step B changes. The judge also refuses the over as a harness error when node 1 carried the listener's profile (`carry_problems`), instead of passing without testing anything. Not run:
the same b with the worker ON and the drop on the carried ambe-dstar record would only repeat case c.

## What the rig has

| Need | Before | Now |
|------|--------|-----|
| A DV-Router pair | profile `twonode`: `xlxd-sut` (node 1, XLX999) and `dvr-peer` (node 2, XLX998, own transcoder `transcoder-peer`, the fake with `DVT_TRANSCODER_FAKE=1`) | unchanged |
| A node link between them | NONE: the pair is an XLX interlink (`dp15-twonode-a/b.toml`), no `[edges.link]` row anywhere in `rig/config/dv-router/` | `rfpos1-link-a.toml` (node 1: uplink for room G) and `rfpos1-link-b.toml` (node 2: account, `talk = true`), added by `scripts/run.sh` under `DVT_RFPOS1B_LINK=on` |
| A key | none | per-run `out/<run>/link-secrets/link.key` (64 hex, 0600), mounted read-only into both nodes at `/etc/dv-router/link-secrets`, never into the tester |
| Dropping ONE link datagram | `--netem` / `netem-*` sidecars (profile `netem`): `tc netem` on the egress of tester / xlxd-ref / xlxd-sut and the ASL lab, random loss by percentage or burst model only; no sidecar on `dvr-peer`; it cannot pick one datagram | an opaque UDP relay in the tester (`dvtest.rfpos1.LinkRelay`): node 1's uplink dials `<tester>:31801`, the relay forwards every datagram unchanged to node 2's server `<dvr-peer>:31800` and back, and drops exactly the first client-to-server `Units` record of a given (profile, sequence). It reads only the cleartext header; the HMAC key never leaves the nodes, so every other record stays valid |

Run changes (all inert unless `DVT_RFPOS1B_LINK=on`, except the always-created empty directory `out/<run>/link-secrets` and its read-only mount into `dvr-peer` / `xlxd-sut` for every `twonode` run):
`scripts/run.sh` (the SUT extra `rfpos1-link-a.toml`, node 2's render gets `--extra rfpos1-link-b.toml`, the key, the SUT mount), `rig/compose.yml` (the `dvr-peer` mount, `DVT_RFPOS1B_LINK` for the
tester). Room G is served by the template (A-Z) and is not in the XLX peer rows (modules DE), so there is no loop. Not run: the rendered configs were only checked by TOML parse and token
substitution (`cases/unit/test_rfpos1_lib.py`); `python3 -m dvtest.tools.config_check --image dv-router:wip-c3b24d2 ...` on both renders is the first step of the first run.

## What is asserted

Over: 90 twentieth-of-a-second units (30 DMR bursts or 90 D-STAR frames, 1.8 s, above the 50-frame short stream guard); the dropped position is 40 (burst 13, unit 1: the middle of a frame).
The listener is a D-STAR (DExtra) client on node 2, module G. Expected units: with `DVT_TRANSCODER_FAKE=1` the fake's bit-exact transform of each source unit
(`dvtest.crossmode.transform_units`), for the D-STAR source the sent frame itself; with a real transcoder the payload is not predictable and only positions are judged.

| Case | Setup | Assertion |
|------|-------|-----------|
| RFPOS1B-a | (all three: `transcoder-sut` stopped, node 1 carries nothing) DMR talker on node 1 module G, no loss, D-STAR listener on node 2 (ambe2-dmr to ambe-dstar in node 2's room) | link wire: every `Units` record of profile 2 holds one 9-byte unit, sequences 0..89 each once, none dropped; listener: positions 0..89 each once (counter modulo 21 unwrapped), none missing, units byte for byte with the fake |
| RFPOS1B-b | the same over, the record of sequence 40 dropped on the link | link wire: exactly that record marked dropped; listener: exactly ONE missing position and it is 40 (lost count 1), every other position 0..89 present with the unit of its own position (a shifted stream fails the byte compare), the output reaches position 89 (not truncated). The pre-RFPOS1 shape (a contiguous run 0..88, the gap at the end) fails as `missing positions [89], wanted [40]` |
| RFPOS1B-c | D-STAR talker on node 1 module G (ambe-dstar link stream), the record of sequence 40 dropped | link wire as above with profile 1; listener: one missing position, 40, every other frame at its own position byte for byte: the D-STAR link stream behaves as before (positioned by profile) |

## Run command

```
XLXD_SUT_IMAGE=dv-router:wip-c3b24d2 DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> DVT_TRANSCODER_FAKE=1 DVT_RFPOS1B_LINK=on \
scripts/run.sh -m rig --profile twonode -- cases/test_rfpos1_link.py
```

No web, no `--capture` needed (add it for the pcap of the link). Estimated time (not measured): 5 to 8 minutes (up to 150 s for the link to come up after the tester's relay starts listening:
node 1 dials from its start and retries, the retry delay is not in the document; three overs of about 20 s each). One rig, nothing else at the same time (the tester binds UDP 31801).

## GAPs

* **No per-link loss counter is asserted.** "Counts lost 1" is the number of missing positions at the listener (1). The Keepalive record carries a counter gap count and the room may count a lossy over,
  but both are hub-side values read at the 10 s refresh and not named in a document the case could assert exactly; a later run can add a `RECORD` of `dv_room_overs_total{result=lossy}` of node 2.
* **The talker side is a dropped record, not a lossy radio frame.** Position loss before the link (a DMR burst missing at node 1) is the RADIOSEQ1 cases (`docs/radioseq1-cases.md`).
* **Only D-STAR listeners.** The output position is observable through the D-STAR counter of node 2's listener; the other protocols (YSF, NXDN, P25) number frames by their own rules (RADIOSEQ1) and
  are not used here. codec2-3200, imbe and pcm-s16le-8k link streams (M17 talker, P25, USRP) are not covered: they share the code path, not a case.
* **A real transcoder** (no `DVT_TRANSCODER_FAKE=1`) judges positions only.
* The relay cannot test the claim "no change to an ambe-dstar link stream's RTS flags" (that is inside a room); RFPOS1B-c asserts its visible effect.
* `dvcheck` (the log scan after the run) may report the counter gap on the link as a warning; the case does not mark a window for it (nothing in the checker names it).
