<!-- SPDX-License-Identifier: MIT -->
# dvt-lora-gw: scripted raw TNC2 and u16 binary source for the LoRa APRS gateway path (HWFREE1 item 3, DP11d)

## Decision: what this delivers

**A pure-Python scenario generator and JSON vectors, no Docker image, no rig service.** The plan
(`research-hwfree-clients.md`, 5.2) sketches a 40-line script that listens on TCP 8001 like the iGate
firmware and sends scripted TNC2 lines. That script needs nothing but the standard library, so it lives in
`dvtest/lora_gw.py` and runs on the host (`python3 -m dvtest.lora_gw serve`); no rig case consumes it yet.
Nothing was built, started or connected to DV-Router. Unit-level vectors only.

What the DV-Router side gets:

1. **85 scenarios x 2 profiles** in `vectors/lora_gw/streams.json` (hex, schema `dvt-lora-gw-streams/2`): 64 text
   scenarios (the 62 of the first version, re-derived, and 2 more for the CR rule) and 21 binary (u16 length)
   scenarios. The two profiles are the two configurations of the adapter: empty prefix (`none`) and prefix `3cff01`
   (`<` FF 01, the common LoRa APRS board prefix; a real profile, tested in `serial_tests.rs:130`).
2. **`vectors/lora_gw/lines.json`**: the three synthetic lines of the plan as TNC2 text, host-link bytes, LoRa payload
   (3C FF 01 + line) with payload length and time on air at SF12 / SF9 / SF7 (equal to the airtime anchor table).
3. **A scripted TCP source** (`serve_once`) that sends one scenario with a pause between reads, for a manual or rig run;
   the serial variant is the same bytes behind `socat` (not run).
4. **Models of the adapter** (`TextFramer`, `BinaryFramer`, `worker_tnc2_ok`, `edge_ax25_ok`, `framer_events`): our
   restatement of the cited source lines, tested against every case of the adapter's own `raw_tests.rs`.

## Verification status (read this first)

**VERIFIED by reading the source, not by running anything.** Every expectation was re-derived from the DV-Router
tree `DV-Router-wip` (commit `1bb304f0`, the files named below unmodified), read read-only; each scenario in the vector
file carries `sources` (`file:line`, relative to `crates/`) and `status: "VERIFIED"`. Nothing was run against a DV-Router
binary and the firmware side (what a real iGate puts on the wire: it strips the prefix and ends lines with CRLF) is still
the plan's statement (basis tag `plan-firmware`, firmware not re-read). The old `UNVERIFIED` flag is gone from every
expectation; the `unspecified_hex` lists are empty because no case is undecided any more.

Files read:

| File | What it decides |
|---|---|
| `edge/ax25/src/raw.rs` | the framing decoder: text (60-81, 115-128) and u16 binary (82-113); limits (4-6); prefix check (35-40) |
| `edge/ax25/src/raw_tests.rs` | the decoder's own cases (10-79); reproduced by `test_model_reproduces_raw_tests_rs_*` |
| `edge/ax25/src/adapter/tcp.rs` | decoder result to counters (391-404), connection close (291-295, 304-308), receive path (416-443), codec choice (517-538) |
| `edge/ax25/src/raw_serial.rs`, `adapter/serial_tests.rs:130` | serial binary needs an idle gap (see "Not done") |
| `edge/ax25/src/raw_tx.rs`, `lib/config/src/ax25_adapter.rs:114-152` | TX payload, config rules for `prefix` and `framing` |
| `data/worker/src/core_rf.rs:128-142`, `lib/pkt/src/aprs/tnc2.rs` | what the worker does with a delivered TNC2 line |
| `lib/pkt/src/ax25.rs:94-146, 223-243` | the AX.25 check of a binary payload |

## Files

| Path | What |
|------|------|
| `dvtest/lora_gw.py` | scenario table, the framer / worker / AX.25 models, `framer_events`, `classify_stream`, vector writer, `serve_once`, CLI (`write`, `list`, `serve`) |
| `vectors/lora_gw/streams.json`, `lines.json` | the vectors (schemas `dvt-lora-gw-streams/2`, `dvt-lora-gw-lines/1`); regenerate with `python3 -m dvtest.lora_gw write` (no container; a unit test checks the committed files are byte-identical) |
| `cases/unit/test_lora_gw.py` | 26 tests: the models against `raw_tests.rs`, the rules one by one, table against the models for both profiles, split twins, every cut of every stream, size boundaries, vectors alone reproduce the verdicts, loopback TCP |

## What an expectation means

The edge only FRAMES; the TNC2 text is parsed later, by the worker. Nothing is ever answered on the wire: a rejection is a counter.

| Stage | Observed as |
|---|---|
| edge framing error (invalid or too long line, prefix mismatch, empty frame; for binary also a payload that is no AX.25 frame) | `dv_edge_modem_total{result="bad_frame"}` (`frames_bad`, `view.rs:176-182`; `tcp.rs:396-398`) |
| edge delivers a text line | `dv_edge_modem_total{result="rx"}`; the line is queued (8 deep; overflow is `result="dropped"`, `tcp.rs:432-436`) and handed to the worker (`shell.rs:322-345`) |
| worker refuses a delivered TNC2 line | `dv_pkt_raw_total{kind="aprs-tnc2",result="rejected"}` and `dv_pkt_reject_total{reason="invalid"}` (`core_rf.rs:135-141`, `core.rs:467-476`) |
| binary terminal length | the connection is closed (`tcp.rs:401-403`, `291-295`); `frames_bad` +1 first; the unpolled queue is counted `dropped` (`tcp.rs:318-329`) |

Per variant in the vector: `accept_hex` (delivered end to end, prefix removed), `reject_hex` (the frame as received),
`edge_bad_frames` (increment of the bad-frame counter for the whole stream), `worker_invalid_hex` (delivered to the worker
and refused there), `connection_closed`, `eof`. A test for the adapter: send the reads, collect what it delivers, require
`accept` as an ordered subsequence of the deliveries and no `reject` line among them; with metrics, check the two counter
increments.

## The rules behind the expectations (each is a cited line)

Text framing (`raw.rs`; `caps.framing = "tnc2"`, `raw-tcp` and `raw-serial` alike):

1. **LF ends a line** (60-66); a CR is accepted only immediately before the LF (74-77). A line holding a bare LF terminator
   is a normal line: `lf-only` is accepted (first guess: undecided).
2. **A CR followed by anything but LF, a second CR, or a NUL anywhere invalidates the line** (70-73); the rest of the line is ignored up
   to the LF (67-69) and one bad frame is counted at the LF. The first error wins (`TooLong` then NUL stays `TooLong`; both are one counter).
3. **The prefix must match exactly and is removed** (119-123); a mismatch, or nothing after the prefix, is `Invalid` (124-126). An **empty line is a bad
   frame, not an ignored line** (first guess: ignored). With the empty prefix the whole line is the payload.
4. **At most 510 bytes after the prefix** (5, 78): the line may hold 510 + len(prefix) bytes (513 for `3cff01`); one more is `TooLong`, discarded to the LF, reported at the LF (a stream with no
   terminator reports nothing and the buffer is fixed: nothing grows). A CR does not count. There is **no 255 byte LoRa limit on receive** (it applies only to TX through `caps.max_frame`, `raw_tx.rs:19`).
5. **No flush at close**: a partial line is dropped when the connection closes (`tcp.rs:304-308`; the decoder has no flush).
6. **The prefix is a hex config value**: 0 to 16 bytes, an even number of hex digits (`ax25_adapter.rs:133-139`); for text it must not hold NUL, CR or LF (`raw.rs:36-40`, `ax25_adapter.rs:140-152`).
7. Splitting the stream never changes anything: `push()` is one byte at a time (55).

Worker (`core_rf.rs:128-142`, `tnc2.rs:37-108`): **the text line is parsed with the APRS-IS style TNC2 parser, not with AX.25 limits** ("Preserve TNC2 path
semantics without narrowing its addresses to the six-character binary AX.25 address representation", `core_rf.rs:128-130`):
source and destination 1 to 9 characters of `A-Z0-9` with one optional `-[A-Z0-9]+` suffix, up to 32 path entries (each a call or a `qXX`), an optional `*`; the header must be UTF-8 and contain `>`
before the first `:`; the information field is not judged (empty, UTF-8, binary all pass). So **SSID 16, a seven character call, nine digipeaters and an empty information field are
ACCEPTED**; lower case calls, a missing `>` / `:`, empty parts and a header that is not UTF-8 (a line that still carries `3C FF 01` under the empty profile) are refused. This is NOT what the lead
stated ("valid TNC2 but not valid AX.25 is rejected by the parser"); see open question 1.

Binary framing (`raw.rs:82-113`, `caps.framing = "raw"`):

8. **Header: 2 bytes, big-endian, the length of prefix + payload** (the header itself is not counted): `0006 3C FF 01 41 42 43` is the payload `ABC` (`raw_tests.rs:29-36`).
9. **Length <= len(prefix) (0 for the empty profile) is `Invalid` and length > 1100 + len(prefix) is `TooLong`; both are TERMINAL**: the adapter closes the connection, whatever follows is never read (87-94, 56-58; `tcp.rs:401-403`). `FFFF` is `TooLong` (`raw_tests.rs:37-40`). The largest frame is 1100 bytes after the prefix (`raw_tests.rs:67-72`).
10. A prefix mismatch is `Invalid` but not terminal (the length keeps the framing aligned): one bad frame, the next frame arrives.
11. A framed payload must be an AX.25 UI frame (`tcp.rs:424-427`, `ax25.rs:94-146`): 16 to 1100 bytes, at most 8 digipeaters, six-character shifted addresses of uppercase letters, digits and trailing spaces, control `03`, PID `F0`; otherwise one bad frame and the connection stays.

## Scenarios

Outcome columns: `accept n` delivered end to end; `bad n` edge bad-frame increments; `worker-invalid n` delivered by the edge and refused by the worker; `closed` the adapter closes the connection.
The text size scenarios `size-N` count the bytes AFTER the prefix (so both profiles give the same verdict; the wire line is N + 3 for `3cff01`). Base lines are the plan's three synthetic placeholders (N0CALL-7, tocalls APLRT1 / APLRG1; the tocalls are plausible values only).

### Text framing (`caps.framing = "tnc2"`)

| Scenario | Group | What | Reads | `none` | `3cff01` | Source lines |
|---|---|---|---|---|---|---|
| `line-crlf` | basic | one line, the profile's own prefix, CRLF | 1 | accept 1 | accept 1 | raw.rs:60-66, raw.rs:115-128, raw.rs:55-114, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `three-lines-one-read` | basic | three lines in one read | 1 | accept 3 | accept 3 | raw.rs:60-66, raw.rs:115-128, raw.rs:55-114, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `three-lines-three-reads` | basic | the same, one line per read | 3 | accept 3 | accept 3 | raw.rs:60-66, raw.rs:115-128, raw.rs:55-114, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `unprefixed-line` | profile | a line WITHOUT the prefix: what the firmware sends to the host | 1 | accept 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404 |
| `prefixed-line` | profile | a line WITH 3C FF 01: the raw radio payload | 1 | worker-invalid 1 | accept 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `mixed-prefix` | profile | prefixed, plain, prefixed in one stream | 1 | accept 1, worker-invalid 2 | accept 2, bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `lf-only` | ending | LF is a complete terminator (no CR) | 1 | accept 1 | accept 1 | raw.rs:60-66, raw.rs:74-77, raw.rs:115-128 |
| `cr-only` | ending | CR with no LF, then the connection closes: nothing is delivered and nothing is counted | 1 | nothing | nothing | raw.rs:60-66, raw.rs:74-77, tcp.rs:304-308 |
| `lf-only-two-lines` | ending | two lines, LF only: two lines | 1 | accept 2 | accept 2 | raw.rs:60-66, raw.rs:115-128 |
| `no-terminator-open` | ending | a complete-looking line with no terminator, connection stays open: nothing is delivered | 1 | nothing | nothing | raw.rs:60-66 |
| `no-terminator-eof` | ending | the same, then the connection closes: the partial line is discarded, not flushed and not counted | 1 | nothing | nothing | raw.rs:60-66, tcp.rs:304-308 |
| `double-crlf` | ending | a line then an extra CRLF: the empty line is a rejected frame (one bad frame) | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:60-66, raw.rs:115-128 |
| `cr-before-text` | ending | CR followed by anything but LF invalidates the line (one bad frame), the next line is fine | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:74-77, raw.rs:70-73, raw.rs:67-69, raw.rs:60-66 |
| `cr-cr-lf` | ending | CR CR LF: the second CR invalidates the line | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:74-77, raw.rs:70-73, raw.rs:60-66 |
| `empty-line` | empty | a single CRLF: an empty frame, one bad frame, nothing delivered | 1 | bad 1 | bad 1 | raw.rs:60-66, raw.rs:115-128 |
| `empty-lines-between` | empty | lines with several empty lines between: each empty line is one bad frame | 1 | accept 2, bad 3 | accept 2, bad 3 | raw.rs:60-66, raw.rs:115-128 |
| `space-line` | empty | a line of one space: framed, then refused by the TNC2 parser | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-3cff02` | prefix | 3C FF 02: last byte differs | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-3dff01` | prefix | 3D FF 01: first byte differs | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-3cfe01` | prefix | 3C FE 01: middle byte differs | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-swapped` | prefix | FF 3C 01: right bytes, wrong order | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-two-bytes` | prefix | 3C FF only: the third byte is missing | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-ascii-hex` | prefix | the text '3cff01' in front of the line | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-twice` | prefix | 3C FF 01 3C FF 01 and the line: one prefix is removed, the second is part of the line and the TNC2 parser refuses it | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `prefix-only` | prefix | 3C FF 01 and nothing else: an empty payload with the prefix configured, else a line the TNC2 parser refuses | 1 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142 |
| `prefix-twice-then-good` | prefix | a doubled prefix line, then a good line: the good one still arrives | 1 | accept 1, worker-invalid 1 | accept 1, worker-invalid 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `junk-no-gt` | garbage | no '>' after the source | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-no-colon` | garbage | no ':' before the information | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-empty-source` | garbage | empty source | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-empty-dest` | garbage | empty destination | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-empty-digi` | garbage | an empty digipeater | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-text` | garbage | plain text | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-binary` | garbage | binary bytes (the header is not UTF-8) | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `junk-nul` | garbage | NUL byte in the information: the framer invalidates the line (one bad frame) | 1 | bad 1 | bad 1 | raw.rs:70-73, raw.rs:67-69, raw.rs:60-66 |
| `tnc2-lowercase` | garbage | lower case callsigns: refused by the TNC2 parser | 1 | worker-invalid 1 | worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-ssid-16` | garbage | SSID 16: valid for the TNC2 parser (any alphanumeric suffix; AX.25 would refuse it) | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-call-7` | garbage | seven character callsign: valid for the TNC2 parser (AX.25 would refuse it) | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-digis-9` | garbage | nine digipeaters: valid for the TNC2 parser (up to 32; AX.25 would refuse it) | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-info-empty` | garbage | valid header, empty information | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-info-high` | garbage | information with UTF-8 bytes | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-digis-8` | garbage | eight digipeaters | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `tnc2-digi-star` | garbage | a digipeater with the has-been-repeated star | 1 | accept 1 | accept 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |
| `size-80` | size | an ordinary line | 1 | accept 1 | accept 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `size-255` | size | 255 bytes after the prefix (the LoRa payload size is no limit on this path) | 1 | accept 1 | accept 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `size-256` | size | 256 bytes after the prefix | 1 | accept 1 | accept 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `size-510` | size | 510 bytes after the prefix: the limit (on the wire 510 + the prefix length) | 1 | accept 1 | accept 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `size-511` | size | 511 bytes after the prefix: TooLong, one bad frame | 1 | bad 1 | bad 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `size-1024` | size | 1024 bytes | 1 | bad 1 | bad 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `size-4096` | size | 4096 bytes | 1 | bad 1 | bad 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6 |
| `overlong-then-good` | size | an overlong line (1024 bytes), then a good one: the good one still arrives | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6, raw.rs:67-69 |
| `overlong-4096-then-good` | size | 4096 bytes, then a good line | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6, raw.rs:67-69 |
| `overlong-split-then-good` | size | an overlong line (2048 bytes) over three reads, then a good one | 3 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:78-81, raw.rs:115-128, raw.rs:35-40, raw.rs:4-6, raw.rs:67-69 |
| `overlong-no-terminator` | size | 2048 bytes and no terminator, connection open: nothing is delivered and nothing is counted yet (TooLong is reported at the next LF); the buffer is fixed, nothing grows | 2 | nothing | nothing | raw.rs:78-81, raw.rs:67-69, raw.rs:60-66, raw.rs:4-6 |
| `split-mid-line` | split | a line over two reads, cut in the middle | 2 | accept 1 | accept 1 | raw.rs:55-114 |
| `split-before-crlf` | split | the line in one read, CRLF in the next | 2 | accept 1 | accept 1 | raw.rs:55-114 |
| `split-between-cr-lf` | split | cut between CR and LF | 2 | accept 1 | accept 1 | raw.rs:55-114, raw.rs:74-77 |
| `split-after-prefix` | split | the prefix in one read, the line in the next (the empty profile cuts at 0) | 2 | accept 1 | accept 1 | raw.rs:55-114 |
| `split-in-prefix` | split | cut after the first byte of the prefix (the empty profile cuts at 1 of the line) | 2 | accept 1 | accept 1 | raw.rs:55-114 |
| `split-in-prefix-2` | split | cut after the second byte | 2 | accept 1 | accept 1 | raw.rs:55-114 |
| `split-bytewise` | split | every byte its own read | 53 | accept 1 | accept 1 | raw.rs:55-114 |
| `split-two-lines-straddle` | split | two lines, the second cut in the middle | 2 | accept 2 | accept 2 | raw.rs:55-114 |
| `split-bad-prefix` | split | a bad prefix over two reads: still refused | 2 | worker-invalid 1 | bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `mid-line-connect` | resync | the connection starts in the middle of a line (the tail of one), then good lines | 1 | accept 1, worker-invalid 1 | accept 1, bad 1 | raw.rs:115-128, tcp.rs:391-404, core_rf.rs:128-142, tnc2.rs:73-108 |
| `garbage-between-lines` | resync | good, junk, good: the junk does not break the next line | 1 | accept 2, worker-invalid 1 | accept 2, worker-invalid 1 | raw.rs:115-128, core_rf.rs:128-142, tnc2.rs:73-108, tnc2.rs:37-55, tnc2.rs:151-155 |

### Binary framing (`caps.framing = "raw"`)

| Scenario | Group | What | Reads | `none` | `3cff01` | Source lines |
|---|---|---|---|---|---|---|
| `bin-valid` | binary | one frame: header, prefix, AX.25 UI frame | 1 | accept 1 | accept 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw_tests.rs:27-41 |
| `bin-two-frames-one-read` | binary | two frames in one read | 1 | accept 2 | accept 2 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-two-frames-two-reads` | binary | one frame per read | 2 | accept 2 | accept 2 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-split-header` | binary | the 2-byte length header split over two reads | 2 | accept 1 | accept 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:55-114 |
| `bin-split-in-payload` | binary | the frame cut in the middle of the payload (and inside the prefix for the 3cff01 profile) | 2 | accept 1 | accept 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:55-114 |
| `bin-bytewise` | binary | every byte its own read | 33 | accept 1 | accept 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:55-114 |
| `bin-zero-length` | binary | length 0000: invalid and TERMINAL (the adapter closes the connection); the frame behind it is never read | 1 | bad 1, closed | bad 1, closed | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:56-58, tcp.rs:291-295, raw_tests.rs:59-79 |
| `bin-length-equals-prefix` | binary | length equal to the prefix length (0 for the empty profile): no payload, terminal | 1 | bad 1, closed | bad 1, closed | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:56-58, tcp.rs:291-295 |
| `bin-one-byte-payload` | binary | length = prefix + 1: framed, but one byte is no AX.25 frame: one bad frame, the connection stays and the next frame arrives | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-max` | binary | the largest frame: 1100 bytes after the prefix (length 1100 + prefix), a valid UI frame | 1 | accept 1 | accept 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw_tests.rs:59-79 |
| `bin-over-max` | binary | 1101 bytes after the prefix: TooLong and TERMINAL (the connection is closed) | 1 | bad 1, closed | bad 1, closed | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:56-58, tcp.rs:291-295, raw_tests.rs:59-79 |
| `bin-header-ffff` | binary | length FFFF: TooLong and terminal | 1 | bad 1, closed | bad 1, closed | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:56-58, tcp.rs:291-295, raw_tests.rs:27-41 |
| `bin-over-max-then-good` | binary | a good frame first, then a too long length: the first arrives, the connection is closed, a later frame is never read | 1 | accept 1, bad 1, closed | accept 1, bad 1, closed | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw.rs:56-58, tcp.rs:291-295 |
| `bin-wrong-prefix-then-good` | binary | a frame whose prefix is wrong (for the empty profile: a frame with a corrupt address): one bad frame, framing stays aligned, the next frame arrives | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243, raw_tests.rs:59-79 |
| `bin-not-ax25` | binary | a payload of 11 bytes: shorter than 16, no AX.25 frame: one bad frame, then a good frame | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-15-bytes` | binary | 15 bytes (two addresses and a control byte, no PID): bad frame | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-16-bytes` | binary | 16 bytes (two addresses, control and PID, empty information): the smallest frame | 1 | accept 1 | accept 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-not-ui` | binary | control byte 0x13 instead of 0x03: bad frame | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-bad-pid` | binary | PID 0xCF instead of 0xF0: bad frame | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-lowercase-address` | binary | a lower case letter in the destination address: bad frame | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |
| `bin-nine-digipeaters` | binary | nine digipeaters: more than the AX.25 maximum of 8, bad frame | 1 | accept 1, bad 1 | accept 1, bad 1 | raw.rs:82-98, raw.rs:87-94, raw.rs:99-113, tcp.rs:391-404, tcp.rs:416-443, ax25.rs:94-146, ax25.rs:223-243 |


### Where the first expectations (62 scenarios, stage "lora-gw") were wrong

Re-derived from the source; 25 of the 62 changed (`git diff` of `streams.json`):

| Scenario | First guess | From the source |
|---|---|---|
| `lf-only`, `lf-only-two-lines` | undecided | a bare LF is a terminator: accepted (`raw.rs:60`) |
| `cr-only`, `no-terminator-eof` | undecided ("may flush") | nothing delivered, nothing counted; no flush (`tcp.rs:304-308`) |
| `double-crlf`, `empty-line`, `empty-lines-between` | an empty line is ignored, no error | an empty line is `Invalid`: one bad frame each (`raw.rs:124-126`) |
| `junk-nul` | undecided | `Invalid` at the edge, one bad frame (`raw.rs:70`) |
| `tnc2-lowercase` | undecided | delivered by the edge, refused by the worker (`tnc2.rs:37-55`) |
| `tnc2-ssid-16`, `tnc2-call-7`, `tnc2-digis-9`, `tnc2-info-empty`, `tnc2-info-high` | undecided | accepted: the worker uses TNC2 rules, not AX.25 limits (`core_rf.rs:128-142`, `tnc2.rs`) |
| all `junk-*`, prefix scenarios | "rejected" (as if at the adapter) | true end to end, but only the framing errors are edge bad frames; the rest are delivered (`rx`) and refused by the worker |
| `size-256`, `size-510` | undecided | accepted (no 255 byte limit on receive) |
| `size-511` (for `3cff01`) | rejected | a 511 byte RAW line is 508 bytes after the prefix: accepted; the limit is 510 AFTER the prefix, so the size scenarios now count bytes after the prefix |
| `overlong-no-terminator` | "reject 1" | nothing: `TooLong` is reported at the next LF |
| `mixed-prefix`, `unprefixed-line` ... | rejected | for the profile that strips: one bad frame at the edge; for `none`: delivered and refused by the worker (`prefixed-line` under `none`: the header is not UTF-8) |

## Using it

- Unit test in DV-Router: read `streams.json`, feed `chunks_hex` to the adapter's decoder (`raw::Decoder::new(Framing::Text | Framing::Raw, prefix)`), compare with `accept_hex` and the bad-frame count.
- Host TCP: `python3 -m dvtest.lora_gw serve --scenario split-mid-line --profile 3cff01 --port 8001`
  listens on 127.0.0.1, sends the reads to the first client with 50 ms between them and exits (the
  adapter is the TCP client, like a host talking to an iGate's TNC port). Tested on loopback only.
  Binary scenarios work the same way with a `raw-tcp` port whose `caps.framing` is `raw`.
- Serial (not run): `socat pty,raw,echo=0,link=/tmp/lora0 TCP-LISTEN:8001,reuseaddr` and the same `serve` command in a second shell gives a pty for `raw-serial` text framing.

## Not done, limits

- No rig run, no connection to DV-Router, no image; nothing was run against a binary.
- **`raw-serial` with binary framing is not covered**: it uses `SerialDecoder` (`raw_serial.rs`), which discards everything until a byte gap of at least
  50 ms (4 characters at the baud rate, 50 ms floor) has been seen and counts an interrupted frame at each gap (`raw_tests.rs:81-153`); the vectors carry reads, not gaps.
  The text framing on a serial port is the same decoder as TCP.
- No KISS-mode variant of the iGate (KISS is covered by the Dire Wolf vectors, `docs/direwolf-cases.md`).
- A CRC-failed LoRa packet, RSSI / SNR, TX (the adapter writes `prefix + TNC2 + LF` or `u16 length + prefix + AX.25`, `tcp.rs:177-189`; not generated here) and real chip behaviour are outside what a byte source can say.
- What the worker does AFTER admission (dedupe, gating, access lists) is not modelled: "accept" here means the edge delivers the line and the TNC2 parser does not refuse it.

## Answers of the DV-Router lead (2026-11-07) and what stays pending

1. **Valid TNC2, invalid AX.25**: ANSWERED. LoRa APRS on air is a TNC2 text line after the `3C FF 01` prefix, not AX.25 (data.md:1651); raw text lines are
   format 8 `aprs-tnc2` (data.md:652, 701), so the worker applies APRS-IS TNC2 rules on the text path by design. The expectations of this file (SSID 16, 7 character call,
   9 digipeaters, empty information accepted) stay. Only a path that renders to AX.25 RF must narrow; LoRa TX is refused today.
2. **Where a line is refused**: ANSWERED. Framing errors are edge modem counters (`dv_edge_modem_total{port,result=bad_frame}`); content rejections are worker counters
   (`dv_pkt_reject_total{reason}`).
3. **Kind label of `dv_edge_raw_total` for text lines** (`shell.rs:344` fixed kind 9): PENDING. data.md:2099 says the kind is the format name, so a text line admitted as format 8
   should count under `aprs-tnc2`; status.md:347 predates DP11d. Recorded by the lead as a finding for the follow-up unit DATAV3 (fix code or docs). Expectation here: per data.md,
   `aprs-tnc2` (pending).
4. **Per-channel versus global bad frames** (`tcp.rs:425` versus `tcp.rs:397`): PENDING. The lead checks which counter `dv_edge_modem_total{result="bad_frame"}` reflects in DATAV3.
5. **Serial binary gaps**: later, low priority (after SvxLink, ipsc2hbp, ircDDBGateway).
6. **Rig service**: not wanted yet; unit-level vectors first. The `lora_gw` vectors are consumed in DATAV3 after DATAV2 merges.
7. **Firmware**: still open (re-read the iGate firmware sources at a pinned commit to turn `plan-firmware` into a verified fact).

### Original questions (kept for the record)

1. **Valid TNC2, invalid AX.25**: you said such lines are rejected by the parser. The code I read accepts SSID 16, a seven character call, nine to 32 digipeaters and an empty
   information field on the text path (`core_rf.rs:128-142`, `tnc2.rs:37-108`; the AX.25 limits apply only to the binary path). Is the TNC2 parser (APRS-IS rules) the intended
   contract for LoRa text lines, or should the gateway narrow them to AX.25?
2. **Where is a line refused, as a counter**: framing errors are `bad_frame` on the modem; TNC2 refusals are `dv_pkt_reject_total{reason="invalid"}` on the worker, not a modem counter. Is the worker counter
   what you meant by "modem counters"? (`shell.rs:335` passes format 8 for text but `shell.rs:344` always labels `dv_edge_raw_total` with kind 9: intended?)
3. **Per-channel versus global bad frames**: `received()` counts an unparseable binary payload in the channel health only (`tcp.rs:425`), the framing errors in the global health (`tcp.rs:397`); which one does `dv_edge_modem_total{result="bad_frame"}` show?
4. **Rig service?** Should the scripted source become a compose service, and where does the adapter connect (reconnect behaviour after the scripted client or a terminal length closes the connection)?
5. **Serial binary gaps**: want a generator with gaps for `SerialDecoder` (reads separated by an idle time)?
6. **Firmware**: re-read the iGate firmware sources (pin a commit) to turn `plan-firmware` into a verified fact?
