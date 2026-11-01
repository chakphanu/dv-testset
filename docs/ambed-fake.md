<!-- SPDX-License-Identifier: MIT -->
# Fake extended AMBEd transcoder

`dvtest/fake_ambed.py` is a standalone stand-in for an AMBEd transcoder that
speaks the extended protocol of DV-Router (`dv-codec-ambed` is the client) and
applies a deterministic, bit-exact transform instead of a vocoder. A rig case
can then check payload bytes, count, order and the single-hop property of
cross-mode traffic exactly; a real transcoder's output cannot be predicted.
Datagram code: `dvtest/proto/ambed_ext.py` (the legacy `dvtest/proto/ambed.py`
and its `FakeAmbed`, used by the netns cases, are untouched). It was written
from the DV-Router design text (transcoding.md) alone, which makes the unit
tests an independent check of that text.

## Protocol as implemented

The fake listens on `bind_ip:10100`. All control datagrams are recognised by
tag AND exact length; anything else is logged as `unknown` and gets no reply
(there is no error datagram). Integers: stream id and data port u16 LE, mask
and transaction u32 BE. Offsets are zero based.

| Datagram | Dir (fake) | Bytes | Layout |
|---|---|---:|---|
| PING | rx | 17 | 0-8 `AMBEDPING`, 9-16 callsign |
| PONG | tx | 9 | `AMBEDPONG` |
| CAPS request | rx | 9 | `AMBEDCAPS` |
| CAPS reply | tx | 15 | 0-8 `AMBEDCAPS`, 9 version (1), 10 max outputs (default 4), 11-14 mask (default 0x3E) |
| OPEN | rx | 24 | 0-6 `AMBEDOS`, 7-14 callsign, 15 codec in, 16-19 output slots, 20-23 transaction |
| STD | tx | 18 | 0-7 `AMBEDSTD`, 8-9 stream id, 10-11 data port, 12 codec in, 13 first output, 14-17 transaction |
| BUSY | tx | 13 | 0-8 `AMBEDBUSY`, 9-12 transaction |
| CS | rx | 9 | 0-6 `AMBEDCS`, 7-8 stream id |
| data request | rx (stream port) | 2 + w(in) | codec in, pid, frame |
| data reply | tx (same port) | 1 + sum(1 + w(out)) | out1, pid, frame1, out2, frame2, ... in OPEN order |

Behaviour:

- CAPS: the same reply for the life of the server (only `caps=changed` alters it).
- OPEN: key (transaction, codec in, output list). A new key gets a new stream
  (non-zero id, unique among live streams) and a data socket bound on
  `bind_ip`; the STD echoes transaction, codec in and first output. A known
  key gets the identical STD and no second stream. Strict validation is the
  default: an OPEN a worker would never send gets NO reply (there is no error
  datagram), counts `open_invalid` plus `open_invalid_<reason>` and is logged
  with note `invalid` and a `reason`: `codec_range` (an id outside 1..5, input
  or output), `no_outputs` (slot 0 empty), `gap` (an unused slot before a used
  one), `order` (outputs not strictly ascending from slot 0), `input_in_outputs`,
  `too_many_outputs` (more than CAPS max outputs), `not_in_mask` (a codec
  outside the CAPS mask), `transaction_zero`. `lenient_open` (constructor,
  `--lenient-open`, control `set`) keeps only `codec_range` and `no_outputs`;
  it is for the unit tests of the transform, which use same-profile pairs and
  unordered outputs. Every rx `open` log entry carries `tx_repeat`: its
  transaction was seen in an earlier OPEN since start or reset (note
  `tx_repeat`), so a case can tell a new OPEN with an old transaction (the
  worker's single 50 ms retry after a playback preemption) from the
  retransmission of the same key, which gets the identical STD and counts
  `open_repeat`.
- Data port: the lowest free port of the fixed range 10200-10299 (constructor
  `data_ports=(low, high)`, `--data-ports LOW-HIGH`; `None` / `0` = ephemeral for
  tests on 127.0.0.1). The field in the STD is authoritative and the real
  worker binds `DV_BIND_IP:<port>` on its OWN address with the same number, so
  an ephemeral port could already be taken there and the session would fail
  for an unrelated reason; keep the SUT container free of 10200-10299 (nothing
  uses it in the rig). A full range, or a port that cannot be bound, answers
  BUSY (`alloc_failed`). `data_port_base=N` / `--data-port-base N` is the
  alternative form port N + stream id (10100 + id like the reference ambed);
  it wins over the range and is not the default.
- Data: one reply per accepted request, sent from the stream socket to the
  request's source. Dropped (counted `data_dropped`, logged with note
  `dropped`): codec byte not the stream's input, length not 2 + width
  (`len_check=strict`; `lenient` accepts any length >= 3 and cycles over the
  received length).
- CS: frees the stream, its key and (within 50 ms) its data port; no reply;
  unknown id is ignored (`cs_unknown`).
- max sessions N: a new key while N streams are live gets BUSY; a repeated OPEN
  of a live stream still gets its STD. BUSY is not remembered.

## Modes (set at run time)

| Setting | Values | Effect |
|---|---|---|
| `open` | `std` | STD (default) |
| | `busy` | BUSY with the transaction, nothing allocated |
| | `silent` | no answer, nothing allocated |
| | `late` (`open_delay_ms`, default 1200) | stream allocated, STD sent after the delay |
| | `std_twice` | the identical STD twice |
| | `std_then_other` | STD, then a STD with the same transaction and another stream id (900 or the next free; same port, no data socket) |
| `data` | `echo` | the transform (default) |
| | `silent` | no reply |
| | `wrong_source` | right reply from another port of the same IP |
| | `delay` (`data_delay_ms`, default 100) | reply after the delay |
| | `wrong_codec` | first byte is a codec other than out1 |
| | `trailing_bytes` (`trailing_n`, default 1) | N bytes (0xA5) after the last frame |
| | `truncated_after_complete_entry` (`keep_entries`, default 1) | reply cut after K complete entries (unchanged when there are not more entries) |
| `caps` | `normal` | default |
| | `delay` (`caps_delay_ms`, default 1200) | reply after the delay |
| | `drop` (`caps_drop_n`, null = all) | ignore the next N requests, then normal |
| | `changed` | max outputs differs (3 when the base is 4, else 4) |
| `pong` | `normal`, `silent` | silent: no PONG |
| `max_sessions` | integer or null | live-stream limit |
| `len_check` | `strict`, `lenient` | data length rule |
| `lenient_open` | boolean | accept OPENs a worker never sends (default false; `reset` restores the startup value) |

A mode may carry its number inline: `"open":"late:300"`, `"data":"delay:50"`,
`"caps":"drop:2"`. A change applies to the next datagram; replies already
scheduled keep their time.

## Control socket and log

JSON, one UDP datagram per request and reply, on `bind_ip:10190` (`--control-port`;
`-1` off). Every reply is an object with `ok`.

| Request | Reply |
|---|---|
| `{"op":"set","open":"busy","max_sessions":2,...}` | `{"ok":true,"modes":{...}}`; unknown key or bad value: `{"ok":false,"error":"..."}` and nothing applied |
| `{"op":"reset"}` | default modes, counters and log cleared, all streams closed, pending delayed sends dropped; `{"ok":true,"now":t,"modes":{...}}` |
| `{"op":"stats"}` | `ok`, `now`, `counters`, `streams_live`, `streams` (id, port, codec_in, outs, transaction, requests, replies), `modes`, `caps`, `log_size`, `last_seq` |
| `{"op":"log","since":t,"since_seq":n,"kinds":[...],"limit":n}` | `ok`, `now`, `entries`, `more`, `last_seq`; the reply stays under 60000 bytes, `more` says to continue with `since_seq=last_seq` |
| anything else | `{"ok":false,"error":"unknown op: x"}` (also for bad JSON) |

Log entry: `seq`, `t` (seconds since start, monotonic), `dir` (`rx`/`tx`), `kind`,
`port` (local port), `peer`, `len`, `hex`, optional `note`. Kinds: `ping`,
`pong`, `caps_request`, `caps_reply`, `open`, `std`, `busy`, `cs`,
`data_request`, `data_reply`, `unknown`. Counters: `rx_<kind>`, `tx_<kind>`,
`open_new`, `open_repeat`, `open_invalid`, `open_silent`, `busy_mode`,
`busy_capacity`, `alloc_failed`, `caps_dropped`, `pong_silenced`,
`data_dropped`, `data_silenced`, `cs_known`, `cs_unknown`, `ctl_ops`,
`log_dropped` (the log keeps the last 200000 entries). In Python the same data
is `server.stats()`, `server.log_entries(since, since_seq, kinds)` (with raw
bytes) and `server.set_modes(**kw)`.

Command line (stdlib only, one stdout line per session):

    python -m dvtest.fake_ambed --bind IP [--port 10100] [--control-port 10190]
        [--max-sessions N] [--max-outputs 4] [--caps-mask 0x3E]
        [--data-ports 10200-10299 | 0] [--data-port-base N] [--lenient-open]

`--bind` defaults to the env `TRANSCODER_IP`. Lines: `fake ambed listening on
IP:PORT control CPORT`, `session open id=16 port=10116 in=1 outs=2,3
tx=a1b2c3d4`, `session close id=16 port=10116`, `fake ambed stopped`
(SIGTERM / SIGINT stop it cleanly).

## Transform

`out[i] = in[i mod len(in)] ^ K(src,dst) ^ (i & 0xFF)` for `0 <= i < width(dst)`;
the received length controls cycling; the index XOR wraps at 256 (PCM).
`K(1,2)=0x02`, `K(2,1)=0x03`, otherwise `((src*7 + dst*13 + 1) & 0xFF) | 0x04`.
Codec ids and unit bytes: 1 AMBE 9, 2 AMBE+2 9, 3 Codec2 3200 8, 4 IMBE 11,
5 PCM S16LE 8 kHz 320.

| K, src \ dst | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| 1 AMBE | 15 | 02 | 2F | 3C | 4D |
| 2 AMBE+2 | 03 | 2D | 36 | 47 | 54 |
| 3 Codec2 | 27 | 34 | 3D | 4E | 57 |
| 4 IMBE | 2E | 37 | 44 | 55 | 5E |
| 5 PCM | 35 | 3E | 4F | 5C | 65 |

An AMBE -> AMBE+2 -> AMBE round trip XORs each original byte with 0x01
(0x02 ^ 0x03; the index terms cancel), so a case can tell the number of hops:
a single hop shows K once. In Python: `ambed_ext.expected_output(src, dst,
frame)` and `ambed_ext.expected_reply(src, outs, pid, frame)`.

Golden vectors: `cases/unit/data/fake_transform_vectors.json` is a copy of
`crates/codec/ambed/testdata/fake_transform_vectors.json` of DV-Router (27
cases, every ordered pair plus short and long inputs); `test_ambed_ext.py`
checks all of them.

## Phase B (rig use)

`DVT_TRANSCODER_FAKE=1 scripts/run.sh ...` swaps the fake in for the system
under test's transcoder. `run.sh` builds `rig/Dockerfile.ambed-fake` (compose
service `ambed-fake-build`, build only, never started, `x-limits-small`, the
`MAKE_JOBS` build arg) from `TESTER_BASE_IMAGE` plus the `dvtest` package and
sets `TRANSCODER_SUT_IMAGE=dvt-ambed-fake:local` on the service
`transcoder-sut` at `<net>.21` (default 172.30.0.21); the entrypoint is
`python -m dvtest.fake_ambed --bind "$DVT_TRANSCODER_BIND" --control-port 10190`.
`transcoder-ref` stays the real image; the real transcoder never runs on
`<net>.21` at the same time. The dv-codec-ambed worker binds its own
`DV_BIND_IP` and the same port numbers, which is why the fake uses the fixed
range 10200-10299 by default (nothing else in the SUT container uses it). The
tester reaches the control socket at `<net>.21:10190`; `cases/crossmode_fake.py`
(fixture `fake_ambed`) skips unless the profile is dvrouter and `stats` is
answered, resets the modes before each case and reads the counters and log.
Cases: `cases/test_crossmode_exact.py` (marker `crossmode`).

## Rig cases (DV-Router, marker `crossmode`)

    XLXD_SUT_IMAGE=<dv-router image> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router \
      DVT_SUT_LAUNCH=import DVT_USRP_DYNAMIC=1 DVT_TRANSCODER_FAKE=1 \
      scripts/run.sh -m rig --capture -- cases/test_crossmode_exact.py cases/test_crossmode_failure.py

`dvtest/crossmode.py` turns what a rig client sends into the units DV-Router
ingests (media-format.md: D-STAR frames + last frame, DMR bursts + terminator,
M17 pairs without packet 0 + EOT, USRP PCM) and folds the transformed units into
what a listener receives (D-STAR voice frames, DMR bursts padded with raw zero
frames, M17 pairs with the EOT `[pending or silence, end unit]`); the counts
equal the document's "Expected counts". `dvtest/fake_ctl.py` is the control
client; `cases/crossmode_fake.py` has the fixtures (`fake_ctl` skips unless the
profile is dvrouter, `DVT_TRANSCODER_FAKE=1` is set and `stats` is answered;
`fake` resets the modes around every case) and the scenario runner.

`test_crossmode_exact.py`: 16 cross pairs (dstar, dcs, dplus, dmr, m17, usrp as
talker or listener), 7 same-codec pairs (payload equal to the input and no OPEN),
D-STAR slow data and M17 META untouched, one OPEN with every listener codec and
one K per listener, 300 units across the 8-bit pid. Every cell also checks the
link as the fake saw it: OPEN input and ascending outputs, the data requests are
exactly the source units in order with pid = index mod 256, one reply each, one CS.
`test_crossmode_failure.py`: open busy / silent / late (beyond and within 1050 ms) /
std_twice / std_then_other, data silent / wrong_source / wrong_codec /
trailing_bytes (session ends 400 ms after the first request, CS, CAPS re-probe),
delay 100 and 600 ms, a reply cut after a complete entry, max sessions 1, a KILL
of `dv-codec-ambed` mid-stream (allow-listed for `kill-proc xlxd-sut`, KILL only)
and `caps=changed` (the worker exits with status 3, the launcher restarts it;
the case ends with a KILL so the next one starts from the normal CAPS).
A point the design text states is asserted through `Fake.expect`; the ids in
`crossmode_fake.RECORD_ONLY` (none at present) would be printed as `FINDING` lines
and collected in `out/<run>/crossmode_findings.json` instead.

Observed on `dv-router:wip-265d818` (47 of 47 pass): every cross cell is byte
exact with the document's counts; CAPS re-probe 1.054 s after an unanswered OPEN;
CS 0.40 s after the first unanswered request; exit status 3 on `caps=changed`;
`dv-codec-ambed` restarted 0.5 s after a KILL. Open points: a DPlus link header
opens a transcode session for its end unit alone (FINDING `dplus-join-stream`);
the USRP listener gets exactly the voice units (the document is silent on a PCM
end unit).

Injected faults and the capture check: every failure case brackets its fault
with the control channel mark `ambed-fault-<mode>` (`Fake.set(**modes)` opens it
with the tag built by `fault_tag`, for example `ambed-fault-open-busy`,
`ambed-fault-data-delay-600`, `ambed-fault-caps-changed-open-silent`,
`ambed-fault-max_sessions-1`; `Fake.fault("worker-kill")` for the KILL; the
`fake` fixture ends it after the recovery). `dvtest.check.marks` binds such a
window to the transcoder-sut address and keeps it `AMBED_FAULT_SETTLE` (5 s) past
the end, so the late reactions fall inside. Inside it the `ambed.*` errors of the
sut <-> transcoder link (`no_reply`, `reply_missing`, `reply_bad`, `tx_mismatch`)
become warnings tagged `[expected during mark ambed-fault-<mode>]`; the same
findings outside any window, and every other protocol, stay errors. The exact
cases inject nothing and are not marked.

## Tests

`cases/unit/test_ambed_ext.py` (layouts from the spec text, golden vectors, all
25 pairs, round trip), `test_fake_ambed.py` (an in-test worker against the
server on 127.0.0.1: every mode, retransmitted OPEN, max sessions, log),
`test_fake_ambed_control.py` (JSON ops, command line), `test_crossmode.py`
(expected listener frames, session grouping, link checks, the control client).
