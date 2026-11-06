<!-- SPDX-License-Identifier: MIT -->
# dvt-lora-gw: scripted raw TNC2 source for the LoRa APRS gateway path (HWFREE1 item 3, DP11d)

## Decision: what this delivers

**A pure-Python scenario generator and JSON vectors, no Docker image, no rig service.** The plan
(`research-hwfree-clients.md`, 5.2) sketches a 40-line script on `python:3.13-slim` that listens on
TCP 8001 like the iGate firmware and sends scripted TNC2 lines. That script needs nothing but the
standard library, so it lives in `dvtest/lora_gw.py` and runs on the host (`python3 -m dvtest.lora_gw
serve`); an image would only wrap that one command and no rig case consumes it yet (open question 7).
Nothing was built, started or connected to DV-Router.

What the DV-Router side gets:

1. **62 scenarios x 2 profiles** in `vectors/lora_gw/streams.json` (hex): the exact reads a gateway sees,
   and per scenario which lines MUST be delivered, which MUST NOT, and which nothing decides.
   The two profiles are the two configurations of the adapter: empty prefix (`none`) and
   prefix `3cff01`.
2. **`vectors/lora_gw/lines.json`**: the three synthetic lines of the plan as TNC2 text, host-link
   bytes (CRLF, prefix stripped) and LoRa payload (3C FF 01 + line) with payload length and time on air
   at SF12 / SF9 / SF7 (equal to the airtime anchor table).
3. **A scripted TCP source** (`serve_once`) that sends one scenario with a pause between reads, for
   a manual or rig run; the serial variant is the same bytes behind `socat` (not run, see below).

**Every expectation is UNVERIFIED.** They are ours, derived from (a) the plan's account of the
firmware (the iGate strips `3C FF 01` and ends each line with CRLF), (b) the DV-Router configuration
document as the plan quotes it ("Raw `tnc2` uses CR/LF delimited text frames", "Receive requires an
exact prefix and removes it") and (c) the TNC2 line format. Neither the firmware sources nor the
DV-Router document were read in this stage. The generator is ours, so this is an independent
byte-level specification, not an independent implementation (the plan says the same). Where nothing is
known the vector says `unspecified` and a test must not assert on it.

## Files

| Path | What |
|------|------|
| `dvtest/lora_gw.py` | scenario table, `classify_line` / `classify_stream` (our restatement of the expectations, not a gateway), vector writer, `serve_once`, CLI (`write`, `list`, `serve`) |
| `vectors/lora_gw/streams.json`, `lines.json` | the vectors (schemas `dvt-lora-gw-streams/1`, `dvt-lora-gw-lines/1`); regenerate with `python3 -m dvtest.lora_gw write` (no container; a unit test checks the committed files are byte-identical) |
| `cases/unit/test_lora_gw.py` | 18 tests: table against the model for both profiles, split twins, every cut of every stream, size boundaries, vectors alone reproduce the verdicts, loopback TCP |

## Vector format

`streams.json`: `profiles` (name -> prefix hex), `limits`, `basis` (the meaning of each basis tag),
`scenarios[]` with `name`, `group`, `note`, `basis[]`, `status` (always `UNVERIFIED`) and
`variants.<profile>`:

| Field | Meaning |
|-------|---------|
| `chunks_hex` | the reads, in order; send each with a pause so it arrives as its own read |
| `eof` | true: the connection closes after the last read |
| `accept_hex` | lines that must be delivered, in this order, **with the prefix removed** |
| `reject_hex` | lines that must NOT be delivered, **as received** (prefix included) |
| `unspecified_hex` | lines (as received) a test must not assert on |

A test for the adapter: send the reads, collect what it delivers, require `accept` as an ordered
subsequence of the deliveries, require no `reject` line to appear, ignore `unspecified`.
`classify_stream` shows the same rules in 30 lines of Python.

## The rules behind the expectations

1. A line ends at CRLF. An unfinished tail is not delivered while the connection is open. A bare LF
   or CR as a terminator, and a tail left at close, are `unspecified`. (plan-config)
2. An empty line is ignored: no frame, no error. (convention)
3. With a configured prefix every line must start with exactly those bytes, which are removed; a line
   without it, with a changed or shortened or doubled prefix, or with nothing after it is rejected.
   With the empty prefix a line that still carries 3C FF 01 is not a TNC2 line and is rejected.
   (plan-config; the empty-prefix half is convention)
4. After the prefix the line must read `SRC>DEST[,DIGI[*]...]:info` with AX.25 callsigns (1 to 6
   upper case letters or digits, SSID 0 to 15, at most 8 digipeaters). Anything with no `>`, no `:`
   or an empty part is rejected. Lower case, 7 character calls, SSID over 15, nine digipeaters, an
   empty information field and control or high bytes in the information are `unspecified`. (convention / open)
5. Size, counted on the raw line (prefix included, CRLF not): up to 255 bytes accepted (a LoRa payload
   holds 255 bytes, general knowledge), 256 to 510 `unspecified`, over 510 rejected (the plan's "overlong
   line > 510 B"). After a rejected overlong line the next good line must still arrive. (size)
6. Splitting the stream into reads never changes any verdict. (invariant: the tests cut every stream at every
   position and also keep one scenario of one byte per read)

## Scenarios

Reads = number of reads in the `3cff01` variant. The two profile columns say what a gateway configured
that way must do: `accept n` lines delivered, `reject n` lines refused, `unspec n` lines undecided.

| Scenario | Group | What | Reads | Profile `none` | Profile `3cff01` | Basis |
|---|---|---|---|---|---|---|
| `line-crlf` | basic | one line, the profile's own prefix, CRLF | 1 | accept 1 | accept 1 | plan-firmware, plan-config |
| `three-lines-one-read` | basic | three lines in one read | 1 | accept 3 | accept 3 | plan-firmware, plan-config |
| `three-lines-three-reads` | basic | the same, one line per read | 3 | accept 3 | accept 3 | plan-firmware, plan-config |
| `unprefixed-line` | profile | a line WITHOUT the prefix: what the firmware sends to the host | 1 | accept 1 | reject 1 | plan-firmware, plan-config |
| `prefixed-line` | profile | a line WITH 3C FF 01: the raw radio payload | 1 | reject 1 | accept 1 | plan-firmware, plan-config, convention |
| `mixed-prefix` | profile | prefixed, plain, prefixed in one stream | 1 | accept 1, reject 2 | accept 2, reject 1 | plan-config, convention |
| `lf-only` | ending | LF is the only terminator (no CR) | 1 | unspec 1 | unspec 1 | open |
| `cr-only` | ending | CR is the only terminator (no LF); the connection closes, so a CR waiting for its LF is not the answer | 1 | unspec 1 | unspec 1 | open |
| `lf-only-two-lines` | ending | two lines, LF only: either two lines or one line holding a bare LF | 1 | unspec 1 | unspec 1 | open |
| `no-terminator-open` | ending | a complete-looking line with no terminator, connection stays open: nothing is delivered | 1 | nothing delivered | nothing delivered | invariant |
| `no-terminator-eof` | ending | the same, then the connection closes: a gateway may or may not flush it | 1 | unspec 1 | unspec 1 | open |
| `double-crlf` | ending | a line then an extra CRLF: the empty line is ignored | 1 | accept 1 | accept 1 | plan-config, convention |
| `empty-line` | empty | a single CRLF: no line, no error | 1 | nothing delivered | nothing delivered | convention |
| `empty-lines-between` | empty | lines with several empty lines between | 1 | accept 2 | accept 2 | convention |
| `space-line` | empty | a line of one space | 1 | reject 1 | reject 1 | convention |
| `prefix-3cff02` | prefix | 3C FF 02: last byte differs | 1 | reject 1 | reject 1 | plan-config |
| `prefix-3dff01` | prefix | 3D FF 01: first byte differs | 1 | reject 1 | reject 1 | plan-config |
| `prefix-3cfe01` | prefix | 3C FE 01: middle byte differs | 1 | reject 1 | reject 1 | plan-config |
| `prefix-swapped` | prefix | FF 3C 01: right bytes, wrong order | 1 | reject 1 | reject 1 | plan-config |
| `prefix-two-bytes` | prefix | 3C FF only: the third byte is missing | 1 | reject 1 | reject 1 | plan-config |
| `prefix-ascii-hex` | prefix | the text '3cff01' in front of the line | 1 | reject 1 | reject 1 | plan-config |
| `prefix-twice` | prefix | 3C FF 01 3C FF 01 and the line: the second prefix is part of the line | 1 | reject 1 | reject 1 | plan-config |
| `prefix-only` | prefix | 3C FF 01 and nothing else | 1 | reject 1 | reject 1 | plan-config, convention |
| `prefix-twice-then-good` | prefix | a doubled prefix line, then a good line: the good one still arrives | 1 | accept 1, reject 1 | accept 1, reject 1 | plan-config, invariant |
| `junk-no-gt` | garbage | no '>' after the source | 1 | reject 1 | reject 1 | convention |
| `junk-no-colon` | garbage | no ':' before the information | 1 | reject 1 | reject 1 | convention |
| `junk-empty-source` | garbage | empty source | 1 | reject 1 | reject 1 | convention |
| `junk-empty-dest` | garbage | empty destination | 1 | reject 1 | reject 1 | convention |
| `junk-empty-digi` | garbage | an empty digipeater | 1 | reject 1 | reject 1 | convention |
| `junk-text` | garbage | plain text | 1 | reject 1 | reject 1 | convention |
| `junk-binary` | garbage | binary bytes | 1 | reject 1 | reject 1 | convention |
| `junk-nul` | garbage | NUL byte in the information | 1 | unspec 1 | unspec 1 | open |
| `tnc2-lowercase` | garbage | lower case callsigns (Dire Wolf refuses them) | 1 | unspec 1 | unspec 1 | open |
| `tnc2-ssid-16` | garbage | SSID 16 | 1 | unspec 1 | unspec 1 | open |
| `tnc2-call-7` | garbage | seven character callsign | 1 | unspec 1 | unspec 1 | open |
| `tnc2-digis-9` | garbage | nine digipeaters (AX.25 allows eight) | 1 | unspec 1 | unspec 1 | open |
| `tnc2-info-empty` | garbage | valid header, empty information | 1 | unspec 1 | unspec 1 | open |
| `tnc2-info-high` | garbage | information with UTF-8 bytes | 1 | unspec 1 | unspec 1 | open |
| `tnc2-digis-8` | garbage | eight digipeaters: the AX.25 maximum, valid | 1 | accept 1 | accept 1 | convention |
| `tnc2-digi-star` | garbage | a digipeater with the has-been-repeated star | 1 | accept 1 | accept 1 | convention |
| `size-80` | size | an ordinary line | 1 | accept 1 | accept 1 | size |
| `size-255` | size | 255 raw bytes: the LoRa payload limit with the prefix | 1 | accept 1 | accept 1 | size |
| `size-256` | size | 256 raw bytes: one over the LoRa payload | 1 | unspec 1 | unspec 1 | size |
| `size-510` | size | 510 raw bytes: the plan's limit | 1 | unspec 1 | unspec 1 | size |
| `size-511` | size | 511 raw bytes: over the plan's limit | 1 | reject 1 | reject 1 | size |
| `size-1024` | size | 1024 raw bytes | 1 | reject 1 | reject 1 | size |
| `size-4096` | size | 4096 raw bytes | 1 | reject 1 | reject 1 | size |
| `overlong-then-good` | size | an overlong line (1024 raw bytes), then a good one: the good one must still arrive | 1 | accept 1, reject 1 | accept 1, reject 1 | size, invariant |
| `overlong-4096-then-good` | size | 4096 raw bytes, then a good line | 1 | accept 1, reject 1 | accept 1, reject 1 | size, invariant |
| `overlong-split-then-good` | size | an overlong line (2048 raw bytes) over three reads, then a good one | 3 | accept 1, reject 1 | accept 1, reject 1 | size, invariant |
| `overlong-no-terminator` | size | 2048 bytes and no terminator, connection open: nothing is delivered and nothing may grow without bound | 2 | reject 1 | reject 1 | size, invariant |
| `split-mid-line` | split | a line over two reads, cut in the middle | 2 | accept 1 | accept 1 | invariant |
| `split-before-crlf` | split | the line in one read, CRLF in the next | 2 | accept 1 | accept 1 | invariant |
| `split-between-cr-lf` | split | cut between CR and LF | 2 | accept 1 | accept 1 | invariant |
| `split-after-prefix` | split | the prefix in one read, the line in the next (the empty profile cuts at 0) | 2 | accept 1 | accept 1 | invariant |
| `split-in-prefix` | split | cut after the first byte of the prefix (the empty profile cuts at 1 of the line) | 2 | accept 1 | accept 1 | invariant |
| `split-in-prefix-2` | split | cut after the second byte | 2 | accept 1 | accept 1 | invariant |
| `split-bytewise` | split | every byte its own read | 53 | accept 1 | accept 1 | invariant |
| `split-two-lines-straddle` | split | two lines, the second cut in the middle | 2 | accept 2 | accept 2 | invariant |
| `split-bad-prefix` | split | a bad prefix over two reads: still rejected | 2 | reject 1 | reject 1 | invariant, plan-config |
| `mid-line-connect` | resync | the connection starts in the middle of a line (the tail of one), then good lines | 1 | accept 1, reject 1 | accept 1, reject 1 | invariant, convention |
| `garbage-between-lines` | resync | good, junk, good: the junk does not break the next line | 1 | accept 2, reject 1 | accept 2, reject 1 | invariant, convention |

`size-N` scenarios use a valid line padded to N raw bytes (prefix included for `3cff01`). The
`profile` group is the cross-over: the same bytes judged by both configurations. The base lines
are the plan's three synthetic placeholders (N0CALL-7, tocalls APLRT1 / APLRG1, which are only
plausible values: UNVERIFIED against the tocall list).

## Using it

- Unit test in DV-Router: read `streams.json`, feed `chunks_hex` to the adapter's line framer.
- Host TCP: `python3 -m dvtest.lora_gw serve --scenario split-mid-line --profile 3cff01 --port 8001`
  listens on 127.0.0.1, sends the reads to the first client with 50 ms between them and exits (the
  adapter is the TCP client, like a host talking to an iGate's TNC port). Tested on loopback only.
- Serial (not run): `socat pty,raw,echo=0,link=/tmp/lora0 TCP-LISTEN:8001,reuseaddr` and the same
  `serve` command in a second shell gives a pty for `raw-serial`; the plan's recipe uses a `/run/dvt` link.

## Not done, limits

- No rig run, no connection to DV-Router, no image (decision above).
- No u16BE-length "raw" framing: the plan found no firmware that uses it and no source for it
  (it "looks like a DV-Router-internal convention"); `dvtest.ax25` could build it from the same lines
  on request.
- No KISS-mode variant of the iGate (the plan's optional firmware `kiss_utils.cpp` oracle, 0.5 to 1
  day, UNVERIFIED that it builds with a shim); KISS is covered by the Dire Wolf vectors
  (`docs/direwolf-cases.md`).
- A CRC-failed LoRa packet, RSSI / SNR, TX (`inbound` TNC2 to LoRa, refused by DV-Router) and real
  chip behaviour are outside what a byte source can say.
- The 255 byte limit is general knowledge about the SX127x FIFO, not a source read here.

## Open questions for the DV-Router lead

1. **Which profile is real?** The plan found only firmware that strips the prefix (profile `none`);
   `prefix = "3cff01"` fits a device that forwards the raw radio payload, and none was found. Is the
   prefixed profile worth keeping in the cases, or only as a configuration test?
2. **Terminators**: CRLF only? Is a line ending in a bare LF (or CR) delivered, dropped, or glued to
   the next line? Is a tail without terminator flushed when the connection closes?
3. **Size**: what is the maximum raw line, does it count the prefix and the CRLF, and what happens beyond it
   (discard up to the next CRLF, drop the connection)? The 255 / 510 split in the vectors is a guess.
4. **Valid TNC2, invalid AX.25**: lower case callsigns, 7 character calls, SSID 16 and over, nine digipeaters,
   empty information, UTF-8 or control bytes in the information: delivered, or refused?
5. **Empty and prefix-only lines**: silently ignored, or counted as errors?
6. **How is a refusal observed?** A test can see what is delivered; to assert "rejected" it needs a counter, a log
   line or a metric. Which one does the adapter have?
7. **Rig service?** Should the scripted source become a compose service (python:3.13-slim + `serve`, a pty for
   `raw-serial`), and where does the adapter connect (reconnect behaviour after the scripted client closes)?
8. **u16BE "raw" framing**: want a generator for it (from AX.25 frames), given that no real source for it exists?
9. **Upgrading UNVERIFIED**: re-read the firmware sources (pin a commit; the iGate repository moves daily) and
   the configuration document, then change the `basis` tags from plan-* to verified or fix the expectations?
