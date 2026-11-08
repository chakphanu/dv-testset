<!-- SPDX-License-Identifier: MIT -->
# Protocol-edge fuzz cases (DV-Router, netns)

Does a DV-Router protocol edge survive malformed input, keep serving a valid client
while it does, stay inside a memory bound, and count what it drops? One scenario per
UDP port of DV-Router: a deterministic, seeded stream of mutated packets, built from
valid packets of `dvtest/proto/*`, with a valid link and over from a second client
every 200 datagrams.

Files:

| File | Role |
|------|------|
| `dvtest/fuzz.py` | pure mutators and the seed packets (no sockets); a case is a function of `(protocol, seed, index)` |
| `dvtest/fuzz_run.py` | run side: sources, `/proc` readers, the last-50 ring, replay file, valid clients and link probes |
| `dvtest/tools/fuzz_replay.py` | `python3 -m dvtest.tools.fuzz_replay`: resend a recorded failure (or regenerate from a seed) |
| `cases/test_fuzz_netns.py` | marker `netns`, DV-Router profile only: 13 scenarios x 4 cases |
| `cases/unit/test_fuzz.py` | offline: determinism, limits, every protocol has a valid seed, run-side helpers, the case file's structure |

## What is mutated

Every seed packet is built with the protocol's own builders (`dextra.header`, `dmr.packet`,
`ysf.stream_packets`, ...), never written by hand. The fields of a packet are not a table of
offsets: `dvtest.fuzz.fields_of` builds the packet twice with two values of one parameter and
takes the bytes that differ, so a builder change moves the field with it. A leading tag (the
magic, `DSVT`, `DMRD`, `M17 `, `YSFD`, `USRP`, ...) is declared by its length only.

A case is `generate(protocol, seed, index)`: the random stream is
`random.Random("<protocol>:<seed>:<index>")` and the mutator is `MUTATORS[index % 17]`, so a run
is reproducible from `(protocol, seed, index)` and independent of what ran before.

| Mutator (slot) | What it does |
|----------------|--------------|
| `truncate` (2 slots of 17) | cut a seed packet at a boundary: lengths 0 to 5, half, size-2, size-1, the start and end of every field. A fixed plan (`truncation_plan`, 59 to 112 cuts per protocol) is walked in order, so the default run of 2000 datagrams cuts every boundary of every seed packet |
| `extend` | append 1 to 256 junk bytes (random, zeros, 0xFF, a repeat of the packet's tail) |
| `flip1`, `flipn` | flip one bit; flip 2 to 16 bits |
| `off_by_one` | length / count / type fields +1, -1, 0, max, max-1; the datagram length itself +-1 |
| `extreme` | 1 to 3 numeric fields set to 0, 1, 2, 0x7F, 0x80, 0xFF, the field maximum, half of it, or random |
| `magic` | the leading tag replaced by the tag of another packet of the protocol, random bytes, zeros, 0xFF, or case-flipped (no tag: the first byte inverted) |
| `module` | module letters outside A-Z (`@`, `[`, NUL, 0xFF, space, `a`, `0`, DEL); a callsign byte outside the alphabet when the packet has no module |
| `ids` | TG, radio id, stream id and sequence out of range (0, 1, 4000, 4027, 4096, 9999, 0x7FFF, 0x8000, the maximum, neighbours of the valid value) |
| `fill` | the whole packet, everything after the tag, or a span, filled with 0x00 or 0xFF; or a prefix followed by a fill |
| `duplicate` | the stream (header, voice, terminator) with frames repeated 1 to 8 times, sometimes the whole stream twice (at most 64 datagrams) |
| `reorder` | the stream reversed, shuffled, terminator first, header last |
| `header_only` | 1 to 4 headers (or control packets) and no voice |
| `voice_only` | 1 to 32 voice packets and no header |
| `term_storm` | the terminator 8 to 64 times |
| `compound` | two or three of the packet mutators one after the other |

Limits: a datagram is at most `MAX_DATAGRAM` = 1400 bytes (the longest seed is 519), a case at
most `MAX_BURST` = 64 datagrams (`cases/unit/test_fuzz.py` checks both for every protocol and
three seeds).

Ports, DV-Router edge processes and the seed packets (`fuzz.protocol(name)`), with the
non-tag fields each mutator can use (`fuzz.interesting_fields(name)`; a `2` or `3` after a name is
the same parameter found at a second place of the packet, e.g. the module in RPT1 and RPT2):

| Protocol | UDP | Edge process | Seed packets | Cuts | Fields (kind) |
|----------|-----|--------------|--------------|------|---------------|
| dextra | 30001 | dv-edge-dextra | 3 control, 1 header, 1 voice, 1 terminator | 67 | callsign (callsign), module (module), stream_id (stream_id), module2 (module), seq (seq) |
| dplus | 20001 | dv-edge-dplus | 4 control, 1 header, 1 voice, 1 terminator | 62 | callsign (callsign), stream_id (stream_id), module (module), module2 (module) |
| dcs | 30051 | dv-edge-dcs | 3 control, 1 voice, 1 terminator | 62 | callsign (callsign), module (module), module2 (module), stream_id (stream_id), counter (count) |
| dmr (MMDVM) | 62030 | dv-edge-dmrhb (dv-edge-mmdvm before c2465de) | 6 control (RPTL, RPTK, RPTC, RPTPING, RPTCL, RPTO), 1 header, 1 voice, 1 terminator | 107 | repeater (id), stream_id (stream_id), dst (tg), src (id), bitfield (flags) |
| dmrplus | 8880 | dv-edge-dmrplus | 2 control, 1 header, 1 voice, 1 terminator | 68 | dmrid (id), reflector (tg), module (module), dst (tg), src (id) |
| ysf | 42000 | dv-edge-ysf | 3 control, 1 header, 1 voice, 1 terminator | 69 | callsign (callsign), gateway / frame callsigns (callsign) |
| m17 | 17000 | dv-edge-m17 | 4 control, 1 header, 1 voice, 1 terminator | 76 | callsign (callsign), module (module), stream_id (stream_id), src (callsign), fn (seq) |
| m17v3 (same scenario as m17) | 17000 | dv-edge-m17 | 3 header (TYPE, text META, GNSS META), 2 voice (FN and signature FN), 1 terminator | 72 | type (type), stream_id (stream_id), meta (flags), fn (seq) |
| nxdn | 41400 | dv-edge-nxdn | 2 control, 1 header, 1 voice, 1 terminator | 73 | callsign (callsign), tg (tg), src / dst in the datagram and in the layer 3 block (id, tg) |
| p25 | 41000 | dv-edge-p25 | 2 control, 1 header (LDU1 first record), 3 voice, 1 terminator | 61 | callsign (callsign), record type (type), src (id) |
| usrp | 34001 | dv-edge-usrp | 3 control (SET_INFO, PING, REMOTE_CMD TLV), 2 voice (PCM, u-law type), 1 unkey | 75 | seq (seq), tlv_value (tg), tlv_len (len), talkgroup (tg), keyup (flags), type (type) |
| xlx | 10002 | dv-edge-xlx | 5 control (L, A, N, U, keepalive), 1 header, 3 voice (revisions 1 to 3), 1 terminator | 112 | callsign (callsign), version (count), modules (module), stream_id (stream_id), module (module) |
| imrs | 21110 | dv-edge-imrs | 2 control (PING, info), 1 header, 1 voice, 1 terminator | 60 | callsign (callsign), dgid_bitmap (flags), stream_id (stream_id) |
| g3 | 12346 / 12345 / 40000 | dv-edge-g3 | 5 control (presence x2, route x2, HPCH), 1 header, 1 voice, 1 terminator | 89 | terminal (callsign), destination (callsign), stream_id (stream_id) |

Not fuzzed here: AMBEd (`dvtest/proto/ambed*.py`; DV-Router is the client of the transcoder, the
fake would have to send the mutated replies), IAX2 / AllStar, IPSC, SVX, the web service and WebRTC.

## What a scenario does

Each scenario is its own namespace (`unshare -rn`), like `cases/test_xlxd_usrp_netns.py`: DV-Router on
`127.0.0.1` through `scripts/dvr-import-wrap.sh`, a `FakeAmbed` on `127.0.0.2`, the valid talker on
`127.0.0.3`, the fuzz client on `127.0.0.5`, the valid listener on `127.0.0.6`, the link probe on
`127.0.0.7`, a stranger socket on `127.0.0.9`, the USRP peer on `127.0.0.4:32001` (`xlxd.usrp`
written for the `usrp` scenario only).

1. Wait for the transcoder ping, then (up to 12 s) for the UDP port(s) in `/proc/net/udp` of the
   namespace. No port bound: the four cases skip with the port list.
2. Warm-up valid round, baseline: the launcher's descendants (`pid`, start time, `comm`, VmRSS from
   `/proc`), the hub counters (`http://127.0.0.1:19108/metrics`, the one the H5 to H8 scenarios read).
3. `DVT_FUZZ_N` (2000) datagrams, `DVT_FUZZ_GAP_MS` (1) apart, two cases in three from a linked session
   of the fuzz client (its own `dvtest/proto` client, identity `N0FUZZ`, so the valid clients are never
   the ones attacked by name), one in three from a socket that never linked. The m17 scenario alternates
   `m17` and `m17v3`.
4. After every 200 datagrams a valid round, then the process table is compared with the baseline.
   The first failure stops the run and keeps the evidence.
5. The hub is read again; when no counter moved yet it is polled for up to 14 s more (the controllers
   publish every 10 s).

The valid round, per protocol:

| Protocol | Valid round |
|----------|-------------|
| dextra, dplus, dcs, dmr, dmrplus, m17, ysf, nxdn, p25 | the talker (`N0CALL` / 1023007 / 1001) and listener (`N0TEST` / 1023008 / 1002) linked once on module B (nxdn / p25: TG `DVT_FUZZ_NXDN_TG` / `DVT_FUZZ_P25_TG`, default 31672); every round a fresh `N0PROB` client gets its link acknowledged (in a thread beside the over, because DPlus and DCS ack after a random delay of up to 2 s on xlxd) and the talker sends an over of `VALID_FRAMES` = 54 frames (not below 50: the short-stream guard counts overs under 50) that the listener must hear at `HEARD_MIN` = 50 percent or more |
| usrp | the configured peer sends a 54-frame PCM over; a D-STAR listener on module B must hear it with the fake transcoder's AMBE+ payload (same shape as `test_open_wait_`) |
| xlx, g3 | a valid connect (xlx) or presence request (g3) must be answered (ack or nack; any reply) |
| imrs | liveness only (the peer list is static, no reply to check) |

If the valid round is not served before any fuzz (warm-up), the cases for nxdn, p25, xlx, imrs and g3
skip `valid_client_served` ("liveness only") and go on; for the other protocols it is a failure.

## Cases

Four cases per protocol, all reading the one scenario (`-k test_fuzz_dextra_` runs only that
scenario). Ids are `FUZZ-<protocol>-<check>` in the docstrings.

| Case | Asserts |
|------|---------|
| `test_fuzz_<p>_edge_survives` | the launcher is alive at the end; no process of the launcher's tree is gone or new (pid and start time) after any batch, so no crash and no restart of the edge, a room or a service; the run was not cut by `SCENARIO_BUDGET` (150 s) |
| `test_fuzz_<p>_valid_client_served` | every valid round: link acknowledged, over heard |
| `test_fuzz_<p>_memory_bounded` | VmRSS of the edge after the run minus before is at most `RSS_GROWTH_MAX_KIB` (8192) |
| `test_fuzz_<p>_counters_move` | `dv_edge_drops_total` (of the edge's `proc` label) plus `dv_pkt_reject_total` grew while the malformed datagrams were sent |

Protocols (`<p>`): dextra, dplus, dcs, dmr, dmrplus, ysf, m17, nxdn, p25, usrp, xlx, imrs, g3.

## Run

```
DVT_DVR_BIN_DIR=.cache/dvr-<tag> DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns cases/test_fuzz_netns.py -q
```

One protocol: add `-k test_fuzz_ysf_`. Smaller or different run: `DVT_FUZZ_N=400`, `DVT_FUZZ_SEED=7`,
`DVT_FUZZ_GAP_MS=2`. `DVT_NETNS_PARALLEL` stays 1 (a DV-Router scenario peaks near 435 of the 1024
tasks of the scope, `docs/rig.md`). Under `DVT_SUT_PROFILE` xlxd the whole file skips.

Budget (estimate, never run when this was written): a scenario is about 4 s start-up, 2000 datagrams
(about 3 s), 11 valid rounds of about 1.7 to 2 s each and the hub read, so about 30 to 40 s; 13
scenarios one after the other come to about 7 to 9 minutes, inside the 10 minute budget. The wait limit
of a scenario is `fuzz_run.timeout_s(n)` (at least 240 s). `DVT_FUZZ_N` above 2000 grows the file by
about 5 ms per datagram plus 3.5 s per 200.

The run directory of a scenario is `out/<utc>/netns/fuzz-<protocol>/`: `result.json` (rounds, process
tables before and after, RSS, hub lines, ports, the last 50 datagrams), `xlxd.log`, `harness.err`.

## Replay a failure

A failing case writes `fuzz-replay-<protocol>.json` next to `result.json` and names it in the failure
text: `format`, `scenario`, `seed`, the reason, and the last 50 datagrams (the 50 before the failure when
the run stopped on it) as `{protocol, seed, index, sub, port, src, hex}`.

```
python3 -m dvtest.tools.fuzz_replay --file out/<utc>/netns/fuzz-dextra/fuzz-replay-dextra.json --list
python3 -m dvtest.tools.fuzz_replay --file FILE --host 127.0.0.1 [--link] [--gap-ms 1] [--module B]
python3 -m dvtest.tools.fuzz_replay --regen dextra --seed 1 --from 1180 --count 50 --host 127.0.0.1
```

`--host` is required (no default) and only a loopback, private or documentation address is accepted
without `--i-have-owner-approval`. `--link` sends the `linked` entries from a live session of the fuzz
client of that protocol. To run the same datagrams inside the same kind of namespace and let the
valid-client checks judge them, name the file for the scenario instead of sending by hand:

```
DVT_FUZZ_REPLAY=out/<utc>/netns/fuzz-dextra/fuzz-replay-dextra.json  DVT_DVR_BIN_DIR=... \
  DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns cases/test_fuzz_netns.py -k test_fuzz_dextra_ -q
```

(only the scenarios whose protocol appears in the file are launched; the datagrams go out in the recorded
order, the valid round still runs after every 200). A single datagram can be rebuilt without any file:
`dvtest.fuzz.generate(protocol, seed, index).packets[sub]`.

## UNVERIFIED (not confirmed when this was written; nothing here ran against a DV-Router build)

1. **Which ports the converted config opens.** `docs/rig.md` lists DExtra 30001, MMDVM 62030, M17 17000,
   DMR+ 8880, XLX 10002 and the `xlxd.usrp` ports as the xlxd contract; DPlus 20001, DCS 30051, YSF 42000,
   IMRS 21110, G3 (12346 / 12345 / 40000), NXDN 41400 and P25 41000 are not in that contract (NXDN / P25
   are switched on by `[edges.nxdn]` / `[edges.p25]` in the rig). The cases read the port table of the
   namespace and skip with the list when a port is not bound; a skip is not a pass. Whether the netns
   config of a given build enables them is open.
2. **Edge process names** are the children in the `proc-stat` allow-list (`cases/unit/test_procstat.py`);
   `dv-edge-dmrhb` against `dv-edge-mmdvm` depends on the build (`DVT_SUT_GEN`), both are tried. M17 v3
   packets are assumed to go to the same edge and port 17000 as M17 v2.
3. **Rate limits.** No ingress rate limit of any UDP edge is documented in this repository. The 1 ms gap
   (1000 datagrams per second, 50 times the 20 ms voice rate, below the 200 x 50 pps relay load of
   `docs/web-load-cases.md`) is a choice. The only documented limiter that touches this harness is the
   short-stream guard (`cases/test_short_stream_guard.py`, `dvtest/specs/dvrouter.py` B18: three overs under 50
   frames from one callsign refuse TX 3 s / 10 s / 300 s), avoided by 54-frame valid overs. A per-address
   login or auth back-off (DMR RPTL, DPlus, DCS) is not documented; it would act on the fuzz address
   (127.0.0.5) only.
4. **The error counters.** `dv_edge_drops_total{proc,reason}` and `dv_pkt_reject_total{reason}` are on the
   hub (`docs/web-soak-cases.md`, `docs/dp15-cases.md`); that a malformed datagram at every edge moves one
   of them, that the `proc` label is the child name, and that they appear within 10 s plus the 14 s wait,
   is assumed. An edge that drops silently fails `counters_move`: a finding for DV-Router to rule on.
5. **The memory bound** of 8192 KiB is a guess, not a measured slope (SOAK1 and the RSS isolation runs use
   other methods). It is a named constant (`RSS_GROWTH_MAX_KIB`); the baseline is taken after the warm-up
   round, so first-use allocations of the edge are mostly not counted.
6. **Valid-client details**: `HEARD_MIN` = 0.5 and the 54-frame over; the identity ids (`N0PROB`, DMR
   1023009 / 1023010, NXDN 1003 / 1004) being accepted by the converted config (no `dmrid.dat` in the
   namespace); the NXDN and P25 TG mapping (`DVT_FUZZ_NXDN_TG`, `DVT_FUZZ_P25_TG`); that DV-Router answers an
   unknown XLX peer (ack or nack) and a G3 presence request at all; the DPlus / DCS link delay of up to 2 s
   (taken from xlxd); the USRP cross-codec over needing the DExtra edge on module B and the fake transcoder
   (`LISTEN_SETTLE` 1 s as in `test_open_wait_`).
7. **The budget** (about 7 to 9 minutes for 13 scenarios) is an estimate from the pieces; the measured wall
   time of the first run replaces it.
8. **Respawns**: any child of the launcher that restarts for a legitimate reason during the run (an idle
   timeout) is reported as a restart; none is known.
