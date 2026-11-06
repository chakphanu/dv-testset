<!-- SPDX-License-Identifier: MIT -->
# D-STAR slow data: spec-derived generator, parser and two independent decoders (HWFREE1 item 4, feeds DP5f)

Stage 1 (the generator, the oracles, 43 cases) is extended by stage 1b (56 cases): the DP5f decision,
the DV-Router unit, the length nibble, the GPS limits and the frame-pair holes are in "Stage 1b: the
DV-Router contract" below. Stage 1c (this revision, 63 cases) replaces the lead's statements by the
DV-Router source, `crates/lib/pkt/src/dstar.rs`, read line by line: the bitmap is LITTLE endian (the stage 1b
MSB first assumption was wrong), either CR or LF ends a GPS line, 0x36..0x3F headers are ignored, a bad
checksum is a `Reject::Crc`, text still accumulates across units today; "Stage 1c: the DV-Router source
contract" below holds the rule table with file:line and what is VERIFIED. Where stage 1b text contradicts
stage 1c, stage 1c is right.

## Decision: what this item delivers

The plan (`research-hwfree-clients.md`, part B row 5, "Item 3", sections 6A, 7, 8) asks for a
layout-parameterised slow data generator and an oracle image from MMDVMHost and DStarGateway
classes. The DV-Router tracker row DP5f says "re-verify which accumulation behaviour DV-Router
must follow ... add vectors from the PDF and a spec-based generator test". It does not say what
DV-Router consumes, so the smallest deliverable that is independently verifiable is this, all
of it offline (no rig run, no DV-Router run):

1. **A spec-derived generator and parser** (`dvtest/dstar_sd.py`, pure Python) for every
   slow data message type of the standard: 20-character message (4 blocks 0x40..0x43), simple
   data / D-PRS `$$CRC` / NMEA (blocks 0x3n), radio header resend (0x55 x 8 + 0x51), code
   squelch (0xC2), fast data (0x8n / 0x9n, one superframe), filler (0x66); the 21-frame
   superframe with the sync pattern at frame 0 and no slow data in it, the scrambler (DERIVED from
   its polynomial, not typed in), block offsets, spill over a sync, G4KLX interleave patterns,
   a block that straddles a sync (negative), the packet loss rewrite table, and DSVT carriers.
2. **Two independent decoders as oracles**: MMDVMHost's `CDStarSlowData` and DStarGateway's
   `CTextCollector` / `CGPSACollector` / `CNMEASentenceCollector` are linked unmodified into two
   small drivers and run in one throw-away image (`dvt-dstarsd:local`, 116 MB). 58 overs built
   from the spec were fed to both; the verdicts are in `vectors/dstar_sd/corpus.json` and our
   decoder reproduces all of them (event by event, including frame numbers).
3. **The answer to DP5f's factual part**, measured rather than read: MMDVMHost's receive class
   CLEARS the text segments at every sync frame, DStarGateway's collector KEEPS them. A
   conforming message never needs the difference (a message fits in one superframe), a message
   split over a sync is decoded by DStarGateway only. "Like MMDVMHost" is therefore wrong for
   the accumulation DP5e built; which behaviour DV-Router must follow was a decision for the
   DV-Router lead: DECIDED (stage 1b): reset at every sync, like MMDVMHost; the code read in stage 1c does not do
   that yet (`state.text` persists across units, `dstar.rs:161-167`), so the decision is the "after DP5f" column.

Not delivered: any rig case, a feed of these overs into a DV-Router edge (the carrier builder
`over_datagrams` exists and round-trips through `dvtest.slowdata.overs`, nothing sends it), a
third decoder: DSDcc is done in stage 2 (image `dvt-dsdcc:local`, the two published `.dis` recordings and
our own corpus on a synthetic RF stream, section "Stage 2" below),
a capture from a real Icom radio (none exists, nothing here claims one), and a verification of the DV-Router lead's statements (nibble rule, limits, frame-pair
holes: stage 1b records them, no oracle checks them). The decisions taken and the questions left are in
"Stage 1b" and "Open questions".

Licence rule: dv-testset is MIT. MMDVMHost and DStarGateway files used are GPL-2.0-or-later and
run only as processes in their own image; nothing of their code or text is in this repository,
only the output formats and facts, cited by file and line of the pinned commits. The JARL
standard and the G4KLX note are copyright documents: the layouts are described in our own words;
the hex dumps of the G4KLX note hold a personal callsign and position and are NOT copied (only
their block structure is, `vectors/dstar_sd/published_structure.json`).

## Files

| Path | What |
|------|------|
| `dvtest/dstar_sd.py` | scrambler (LFSR), CRC-16, NMEA checksum, `dprs_line`, block builders, `place` / `interleave` / `render` layout engine, `fast_data_superframe`, `packet_loss_rewrite`, `Decoder` (policy "accumulate" or "reset"), `over_datagrams` |
| `dvtest/oracle_dstar_sd.py` | the 63-case corpus, driver input and output text, our views in each oracle's terms, comparison, the thin `docker run` call, `python3 -m dvtest.oracle_dstar_sd run` |
| `rig/Dockerfile.dstarsd`, `rig/dstarsd/` (two drivers, two shims), `scripts/pre-fetch-dstarsd.sh`, `.env.example` (`DSTARSD_*`) | the image `dvt-dstarsd:local`, the drivers (ours, C++) and the one-time source fetch |
| `vectors/dstar_sd/corpus.json`, `dsdcc.json`, `published_structure.json`, `README.md` | the vectors (JSON text only): 63 cases and 19 unit level vectors (`unit_vectors`) |
| `cases/unit/test_dstar_sd.py`, `cases/unit/test_oracle_dstar_sd.py` | 43 tests (35 + 8) next to the 7 existing ones of `test_slowdata.py` (untouched, passing); the first file also reads the committed corpus and the published structure |
| `cases/unit/test_dstar_sd_dvrouter.py` | 26 tests of stages 1b and 1c: the 66 byte unit (little endian), nibble policies, the unit model of `dstar.rs` (today and DP5f), the expectation fields, the boundary, CRC reject, terminator, nibble and pair-hole vectors, the unit level vectors |
| `dvtest/slowdata.py` | unchanged except a docstring pointer; `encode_text_over` equals our `text x2` layout (unit test) |

## What is read from the standard and what is derived

Keys: STD6 = JARL "Digitalization Technology Standard for Amateur Radio" Ver 6.0a (English
edition; text and the figures of pages 63-84 and 89-91 read, the PDF pages were rendered and
looked at where the text extraction loses the figure), SLOW = J. Naylor (G4KLX), "The Format of
D-Star Slow Data" v0.2, ORC = the oracle run below.

| Item | Source | Status |
|---|---|---|
| 21 frames per superframe, frame 0 carries 55 2D 16 in its data field and no slow data, frames 1..20 carry 3 bytes each | STD6 4.1.3, SLOW intro; figures p.68, 70, 71 number the frames 1 (sync) to 21 | read; ORC (both decoders pair frames 1+2, 3+4 ... after a sync) |
| Scrambler `S(x) = x^7 + x^4 + 1`, all ones at the start, bits LSB first, restarted for every frame: first 9 bytes `70 4F 93 40 64 74 6D 30 2B` | STD6 Ap1.1 | DERIVED by `lfsr_bytes` and equal to the constant both decoders use (unit test); the voice-field use of bytes 4..9 in fast data is UNVERIFIED |
| Sync frame and last frame are not scrambled | STD6 Ap1.3 | read |
| Block = 2 data fields = 6 bytes: mini-header (type high nibble, length or number low nibble) + 5 payload bytes, unused payload bytes `f` (0x66) not counted in the length | STD6 6.1.1-6.1.3, SLOW | read; ORC (collector lengths, MMDVMHost block types) |
| Types: 3x simple data (1..5 valid characters), 4x message (block number 0..3), 5x header resend, 66 no data, 8x / 9x fast data (1..15 / 16..28 bytes), Cx code squelch (2) | STD6 6.1.3 table | read; ORC for 3x, 4x, 5x, Cx, 66, 9C, 9D |
| A superframe holds 10 blocks; no block spans a sync | STD6 figures p.68, 70; SLOW "ten blocks between data re-sync points" | read; a block that straddles one is undecodable by both oracles (`text-straddle`) |
| Message: 20 characters, 4 blocks of 5, may start at an even data frame (frames 2, 4, 6, 8 counted with the sync as frame 1) | STD6 6.3 and its figure | read; ORC (`text-aligned`, `text-slot1`, `text-slot3`, `text-slot6`) |
| Header resend: 41 bytes as 8 blocks 0x55 and one 0x51 (the last byte), after the sync or after the code squelch | STD6 6.4 figure p.70, SLOW | read; ORC (MMDVMHost decodes it with a valid FCS, `header-aligned`, `header-after-squelch`) |
| Code squelch right after the sync: `C2 xx xx` + filler, the code is the hex of the decimal digits shown (19 -> 0x19) | STD6 6.5 figure, SLOW | read; the generator reproduces the published bytes `c2 19 19 66 66 66` (unit test); ORC sees block type C2 only, neither decoder implements the code |
| D-PRS: `$$CRC` + 4 hex digits + `,` + APRS string + CR; CRC-16 reflected 0x8408, init 0xFFFF, result complemented, **covering the string AND its final CR** | STD6 6.8.1, Ap2.1 (polynomial); SLOW | the CR rule is ORC only: a CRC computed without the CR is refused (`dprs-crc-without-cr`), with it accepted (`dprs-aligned`); the example `$$CRCEF2E,...` of STD6 6.8.1 could NOT be reproduced (eight readings of the figure tried, none gives EF2E): the figure may be illustrative, UNVERIFIED |
| NMEA sentences travel as simple data ending CR LF, checksum = XOR between `$` and `*` | STD6 6.7, SLOW | read; ORC (six sentence types accepted, bad checksum refused) |
| 6.7 "1 to 3 NMEA sentences + callsign (8) + message (20)" as ONE comma-joined line | STD6 6.7 figure | NOT generated: SLOW shows them as separate sets; the free-form line is only a test of "no `$GP`, ignored" |
| Simple data must not carry 00, 11, 13, 76, 84, E7, FE | STD6 6.2 | read (`simple_data_blocks` refuses them); the oracles do not check it |
| Fast data: block 1 (holds the sync frame) 28 data bytes, blocks 2..10 20 bytes, 3 + 9 x 2 = 21 frames; each block starts with mini-header 0x9C / 0x94, a guard byte in the second data field, voice fields as 4 data + 0x02 + 4 data | STD6 7.1, 7.3 and the two tables of p.80 | read; ORC: MMDVMHost sees the block types `9C` then `94` x 9; the BYTE ORDER of the 28 / 20 bytes (the order of the figure: data fields first, then the voice fields of frames 2, 3, 1 resp. 4, 5) and the scrambling range (p.81 figure, low resolution) are UNVERIFIED: no decoder reads fast data payload |
| Packet loss: rewrite rows `31 80 66`, `32 xx xx`, `33`, `34`, `35` followed by `80 66 66`, `80 00 66`, `80 00 00` in the second field | STD6 6.6 table | read; implemented as `packet_loss_rewrite`, tested against the table only; no decoder implements it (UNVERIFIED) |

## Oracle image: build and run (VERIFIED, 2026-11-06)

- Pins (`.env.example`, never in a script): `DSTARSD_MMDVM_REPO` / `_COMMIT` = `g4klx/MMDVMHost`
  `9384d683c9993f67af52b062b1bf9d4df11b6446`; `DSTARSD_DGW_REPO` / `_COMMIT` =
  `F4FXL/DStarGateway` `7b75de605dca542a2953dd74c16f0eac96a8944e`; base `debian:bookworm-slim`
  (`DSTARSD_BASE_IMAGE`). `scripts/pre-fetch-dstarsd.sh` fetches both into `.cache/dstarsd-*`.
  These are the commits the research note read, so its file:line numbers below apply.
- Build: `scripts/contained.sh --name build -- docker build -f rig/Dockerfile.dstarsd ...` (command
  in the Dockerfile header). Multi-stage; only the files each decoder needs are copied into a
  flat directory per driver (MMDVMHost: `DStarSlowData`, `DStarHeader`, `CRC`, `Utils`; DStarGateway:
  `TextCollector`, `SlowDataCollector`, `SentenceCollector`, `GPSACollector`,
  `NMEASentenceCollector`, `APRSUtils`, `BaseCommon/{Log*,StringUtils,Utils}`), two shims replace
  `Log.h` (MMDVMHost: JSON + MQTT) and `AMBEData.h` (DStarGateway: network stack; the unmodified
  `TextCollector.cpp` uses its `isSync()` and `getData()` only), static C++ runtime, non-root user.
  The two drivers are NOT linked together (both define `CUtils`).
  **Image size: 116 MB on disk (28.7 MB content)**, far under the 600 MB limit.
- Build attempts: 1 failed (MMDVMHost `Utils.cpp` uses `struct tm` without `<ctime>`: the compiler is
  run with `-include ctime`, the file is not edited), 2 succeeded.
- Run: `docker run --rm -i --network none --read-only --cap-drop ALL --security-opt
  no-new-privileges`, `dvtest.limits.docker_run_args()`, `--entrypoint /usr/local/bin/dstarsd-mmdvm`
  (or `-dgw`), text on stdin (`B name`, `F seq hex6`, `E`), results on stdout. Two runs of a few
  seconds cover the whole corpus.
- What the drivers call (cited from the pinned commits): MMDVMHost network path: a sync frame calls
  `start()`, every other frame `add()`, then `getText()` (`DStarControl.cpp:814`, `:885-888`; RF path
  `:377`, `:489-491`); DStarGateway: every frame goes to `CTextCollector::writeData()`, which handles
  the sync itself (`RepeaterHandler.cpp:798-806`), the GPS collectors get `sync()` at a sync frame
  and `writeData()` otherwise, then `getData()` and `reset()` (`APRSHandler.cpp:96-137`).

## Facts read from the oracles

V = in `vectors/dstar_sd/corpus.json` (the first 58 cases, `mmdvm` and `dgw` columns, our decoder equal; stage 1c re-ran all 63 and the 58 old columns came back byte for byte the same),
S = read from the pinned source, not shown by a vector.

| Fact | Status |
|------|--------|
| Aligned messages (one superframe, start slot 0, 1, 3 or 6) are decoded by both; a text over that repeats the 4 blocks (what DV-Router renders, `text-dvrouter-twice`) gives the text twice per superframe at the library level | V |
| **MMDVMHost clears the text segments at every sync frame** (`start()` zeroes the mask and the pair phase, `DStarSlowData.cpp:173-181`); a message with blocks on both sides of a sync is never emitted (`text-slot8-split`, `-slot9-split`, `text-missing-block-late`) | V, S |
| **DStarGateway keeps them**: its `sync()` only resets the pair phase (`TextCollector.cpp:120-123`); the same overs decode, and a late block completes an earlier message | V, S |
| Both clear the 4-segment mask when a message is handed out, so a second message needs 4 new blocks (`text-two-messages-split`: DStarGateway gives both messages, MMDVMHost only the first) | V, S |
| Segments carry no message id: old and new blocks MIX (`text-stale-mix`: DStarGateway printed `N0CALL: CQ DE NORLD.` from message A blocks 0..2 and message B block 3) | V |
| Both strip bit 7 of every text byte (`text-high-bit`) | V, S (mask 0x7F) |
| A block that straddles a sync (blocks start in an even frame) is garbled by the pair phase of BOTH decoders and never yields text; a frame lost inside a block shifts the pairing until the next sync, then text in the next superframe decodes (`text-lost-frame`) | V |
| A missing sync frame does nothing when the previous superframe ended on a pair boundary (`text-sync-missing`) | V |
| MMDVMHost decodes the header resend only when all 9 blocks follow one sync (it zeroes its buffer at the sync) and the FCS is right; two superframes give two events; split header, bad FCS: nothing | V, S (`DStarSlowData.cpp:79-110`) |
| MMDVMHost does not decode GPS, squelch or fast data payload: it reports the block type byte (C2, 9C, 94, 9D) and its caller treats 0x80..0x9C as fast data (`DStarControl.cpp:396-461`, `0x9D` and above invalid) | V (types), S |
| DStarGateway `$$CRC` collector (D-PRS): the CRC must match the string **including the CR**; a wrong CRC, or one computed without the CR, gives nothing; 300 and 301 byte lines are both accepted (no length limit, DV-Router keeps 300 B), two lines in a row give two events | V |
| DStarGateway NMEA collectors: `$GPGGA`, `$GPGLL`, `$GPVTG`, `$GPRMC`, `$GPGSA`, `$GPGSV` (six, `APRSCollector.cpp:38-43`); a bad checksum gives nothing; the output line is `N0CALL-5>GPS30,DSTAR*:` + the sentence with its checksum cut and recomputed as two LOWER case hex digits, no CR LF; 82 and 83 byte sentences are both accepted; a `000000.00` time stamp would be replaced by the clock (S, `NMEASentenceCollector.cpp:128-141`, not in the corpus: not reproducible) | V, S |
| DStarGateway's `RepeaterHandler` only collects text while its stored text is empty, i.e. the FIRST message of a stream (`RepeaterHandler.cpp:798`); the corpus shows the library, not that gate | S |
| DStarGateway's sentence collectors add 5 bytes per block and ignore the length nibble; they cut at the last CR (D-PRS) or LF (NMEA) (`SentenceCollector.cpp:34-61`). Ours honours the nibble (policy `strict`, `ignore` reproduces DStarGateway); the two agree on every well-formed block run; wrong-nibble blocks are in the corpus since stage 1b (`nmea-nibble-*`, named difference DIFF-NIBBLE) | S, V | S |
| The GPS collectors keep their buffer across syncs (a D-PRS line is 17 blocks = two superframes, `dprs-aligned`, `dprs-slot3`) | V |

### What the two decoders say about DV-Router's behaviour (DP5e, DP5f)

| DV-Router rule (data.md "Built (DP5e)") | MMDVMHost (V) | DStarGateway (V) |
|---|---|---|
| Four indexed 5-byte segments are kept across superframe units | NO: cleared at each sync | yes |
| The first complete message is consumed and clears the mask | yes | yes |
| Present numbered segments stay usable after missing ones | within one superframe only | across superframes |
| "follows `MMDVMHost/DStarSlowData.cpp:112-170`" | lines 112-159 collect, **173-181 clear at the sync**: the cited lines do NOT show accumulation over a sync (the DV-Router data.md citation of MMDVMHost for accumulate-across-sync was WRONG; the decision below makes the code match the citation) | DStarGateway `TextCollector.cpp:120-123` does |

Stage 1c: the code read (`dstar.rs:161-167`) still keeps `state.text` and the mask across units, i.e. it accumulates
like DStarGateway (the DP5e behaviour); the reset is the DP5f decision, not yet in the code.

DSDcc is said by the research note to accumulate as well (`dstar.cpp:324, :405`): VERIFIED in stage 2
(it shows the full text of `text-slot8-split`, see "Stage 2").

## Not verified

- No real radio: no capture of an Icom (or any) radio sending text, GPS or header resend exists in
  the public sources searched; every over here is synthetic. Which start slot, interleave and
  repeat pattern real radios use is not known beyond the G4KLX tables; nothing here claims Icom
  conformance. `text-slot8-split` and its relatives are legal bytes, not a known radio behaviour.
- The two oracles are one code family (G4KLX lineage, both reset the pair phase at a sync): their
  agreement with our decoder is evidence about the format, not an independent proof of it. DSDcc (not G4KLX
  lineage) was run in stage 2; dsd-fme and DroidStar were not run (dsd-fme needs RF audio).
- Fast data: only the mini-headers are seen by a decoder; the byte order inside a block, the guard
  byte and the scrambling range are read from low resolution figures. Fast data in the voice field is
  `fast_data_superframe`'s own model.
- Packet loss rewrite, forbidden characters, D-PRS field rules (STD6 6.8.1: symbol, data extension,
  comment), the `EF2E` example, the 6.7 comma-joined line: not covered by any decoder.
- Timing (420 ms cadence, 20 ms frames, the end-of-stream pattern) is not modelled; `over_datagrams`
  builds DSVT bytes only and was not sent to any server.

## Numbers and vectors the DV-Router side can use

| Need | Where |
|------|-------|
| The scrambler as bytes, and its derivation | `dstar_sd.SCRAMBLER` (`70 4F 93 40 64 74 6D 30 2B`), `lfsr_bytes` |
| Wire frames (21 per superframe, `seq hex6`) for 63 cases: aligned, offset, split, straddling, lost frame, header, D-PRS, NMEA, squelch, fast data | `vectors/dstar_sd/corpus.json` `frames` |
| What a MMDVMHost-family and a DStarGateway-family receiver does with each | `mmdvm`, `dgw` (events `[kind, seq, hex]`), `text_agree`; DV-Router: `expected_dvrouter` (CURRENT code: `basis`, `text`, `gps`, `outcome` per unit), `expected_dvrouter_after_dp5f` (only where it differs), `lenient_alternative`, `unit66_hex` (little endian bitmap, stage 1c), `unit_vectors` (reject outcomes) |
| Boundary lines for the GPS buffers: D-PRS sentence of 299 / 300 / 301 bytes, NMEA of 80 .. 85 bytes including CR LF, valid CRC / checksum; today 84 are accepted and 85 rejected, DP5f will accept 82 at most | cases `dprs-len-299`, `-300`, `-301`, `nmea-len-80` .. `-85` (`expected_dvrouter_after_dp5f` on `-83`, `-84`) |
| CRC rule: Icom CRC covers the CR; check values `crc16_x25("123456789") = 0x906E`, `"ABCDEF" = 0x6F5E` | `dstar_sd.crc16_x25`, test_dstar_sd |
| Block structure of real-radio samples (lengths 71 / 74 / 31 / 81 bytes, 17 blocks for D-PRS) | `vectors/dstar_sd/published_structure.json` |
| An over as DSVT datagrams (header, 21 voice frames with null AMBE, last frame) | `dstar_sd.over_datagrams(stream_id, frames)` |

The vectors are plain JSON (`frames` are hex strings): a Rust test can feed the 3-byte fields to
its parser and compare with `dgw` / `mmdvm`.

## Stage 1b: the DV-Router contract (answers of the DV-Router lead, taken as given)

Everything in this section that comes from the lead is a STATEMENT, not an oracle result. Stage 1c has since
read the DV-Router source: where this section disagrees with "Stage 1c" below (the bitmap bit order, the NMEA
limit readings A and B, the text reset being already in the code, 0x36..0x3F and bad checksums), stage 1c is
right. In the corpus every `expected_dvrouter` now has the basis `dvrouter-source`; `lead-statement-unverified`
is left for `expected_dvrouter_after_dp5f` (the planned rules, not in the code read).

### Decision (DP5f): reset at every sync, like MMDVMHost

DV-Router RESETS the text segments at every superframe sync, as MMDVMHost does
(`DStarSlowData.cpp:173-181`). Consequences, all in `vectors/dstar_sd/corpus.json`:

- Stage 1b put the DP5f text (MMDVMHost's, verbatim) into `expected_dvrouter`. Stage 1c: that is now
  `expected_dvrouter_after_dp5f.text` (a test checks it equals the `mmdvm` text events for every case);
  `expected_dvrouter` is what the code does today (accumulate, `dstar.rs:161-167`).
- `text-slot8-split`, `text-slot9-split`, `text-missing-block-late` (split over a sync) and
  `text-stale-mix` (old and new blocks mixed) must NOT emit text in DV-Router after DP5f
  (`expected_dvrouter_after_dp5f.text` is empty); today they do. `text-two-messages-split` gives one message
  (MMDVMHost) after DP5f, two today; `text-lost-frame` gives its text at frame 8 after DP5f, at frame 4 today.
- `lenient_alternative` keeps the DStarGateway result (the same events as `dgw`), `lenient_differs`
  says whether it differs from the current expectation (events or a reject outcome); every difference
  has a name, see "Stage 1c".
- The DV-Router `docs/design/data.md` ("Built (DP5e)") cites `MMDVMHost/DStarSlowData.cpp:112-170` for
  accumulating segments across a sync. That citation was WRONG (the cited code clears at the sync,
  lines 173-181; accumulation is DStarGateway's `TextCollector.cpp:120-123`). With the reset
  decision the code and the citation agree and the DP5e accumulation is to be removed or confined
  to one unit.

### The 66 byte unit

DV-Router's decoder is fed ONE unit per superframe, 66 bytes: a 3 byte present-frame bitmap, then
21 x 3 raw slow data bytes, frame 0 included, as received (frame 0 normally still the sync pattern
`55 2d 16`, the other frames still scrambled; an absent frame is 3 zero bytes). The corpus field is
`unit66_hex`: a list with one 132 character hex string per unit. A new unit starts when the frame
number does not increase (so a lost sync frame still ends the unit, `text-sync-missing`).

**Bitmap bit order (stage 1c, VERIFIED `dstar.rs:132-136`):** the stage 1b text here assumed MSB first
(`fffff8`); that was WRONG. The bitmap is `b0 | b1 << 8 | b2 << 16`, LSB first, bit i = frame i: frame 0 =
mask 0x01 of byte 0, frame 7 = 0x80 of byte 0, frame 8 = 0x01 of byte 1, frame 16 = 0x01 of byte 2, frame 20 =
mask 0x10 of byte 2; the top 3 bits of byte 2 must be zero. A complete unit starts `ffff1f 552d16`.
`dstar_sd.unit66()` / `parse_unit66()` and every `unit66_hex` of the corpus were regenerated; the old layout
is kept as the negative vector `unit-old-msb-first-bitmap` (`fffff8` is `Invalid`).

### Block pairing and holes (statement 7; VERIFIED in stage 1c, `dstar.rs:144-151`; no oracle does this)

Blocks are the fixed frame pairs (1,2) (3,4) ... (19,20) of a unit. A block that straddles a sync
cannot form; an offset pairing is not resynced (`text-straddle`: no text). A pair with a missing
frame skips that block and resets the GPS line buffer only (the text segments are kept until the
next unit). The oracles do the opposite: they pair the frames they RECEIVE, so one lost frame shifts
their pairing until the next sync. Three cases, `expected_dvrouter` from the unit model
`dstar_sd.DvrUnitDecoder` (now a line by line model of `dstar.rs`):

| Case | Holes | DV-Router (statement) | MMDVMHost / DStarGateway (measured) |
|---|---|---|---|
| `pair-hole-text-once` | frame 6 (pair 5,6) | no text | no text |
| `pair-hole-text-twice` | frame 4 (pair 3,4), message sent twice in the unit | text at frame 12 (the second copy fills block 1) | none (named difference DIFF-PAIR-HOLE) |
| `pair-hole-nmea-then-ok` | frame 6 in the first unit of GGA + RMC | only the RMC | only the RMC (agree) |

The existing `text-lost-frame` is the case "hole, then a complete repeat": all three give the text
in the second superframe.

### Length nibble of text and data blocks: NAMED DIFFERENCE DIFF-NIBBLE

DV-Router reads the length nibble of the 0x3n data blocks: only 0x30..0x35 carry bytes (n = 0 means 5);
0x36..0x3F are IGNORED like every other header byte and do not reset the GPS line (`dstar.rs:181-185`, `205`,
VERIFIED in stage 1c; the open question below is answered). DStarGateway's collectors ignore the nibble and add 5
bytes per block. This is a documented difference, not a failure of either. Because the line then has a hole its
checksum fails and the unit is `Reject::Crc` (stage 1c). Vectors (GGA sentence in simple data blocks, both
oracle results recorded):

| Case | Change | DStarGateway | DV-Router (statement) |
|---|---|---|---|
| `nmea-nibble-0x36`, `nmea-nibble-0x3f` | one full block (5 characters) gets 0x36 / 0x3F | sentence decoded | block ignored, checksum fails: `reject:Crc` |
| `nmea-nibble-low` | a block of 5 characters is marked 0x33 (disagrees with the content) | sentence decoded | 3 characters used, checksum fails: `reject:Crc` |
| `nmea-nibble-0x30` | a full block gets 0x30 | decoded | decoded (n = 0 means 5), agree (only the CR / LF frame number differs, `DIFF-NMEA-DELIM`) |
| `nmea-nibble-short-block` | blocks of 5,5,5,3,5,...: a legal short block (3 characters + 2 filler) in the middle | a GARBLED sentence (the filler `f` bytes are inside) that still passes the XOR checksum, because equal pairs cancel | the correct sentence |

MMDVMHost has no GPS decoder: its column is block types only. Note for the last row: the standard allows
a short block anywhere; DStarGateway's output there is wrong, DV-Router's is right.

### GPS limits and boundaries (statements 3 and 4)

- D-PRS limit: 300 bytes INCLUDING the CR; longer is rejected and the line discarded to the next
  delimiter. Vectors `dprs-len-299`, `-300` (accepted), `-301` (rejected; DStarGateway accepts all,
  named difference DIFF-GPS-LIMIT).
- NMEA / ID line limit (stage 1c, VERIFIED `dstar.rs:20`, `62-66`, `186-187`): the stored line is at most 82 bytes
  NOT counting the terminator, and EITHER CR or LF ends a line; so with CR LF a total of 84 bytes is accepted today
  and 85 is rejected (`Reject::Size`, the rest of the line is discarded). Stage 1b guessed the delimiter was the LF
  and counted the CR (reading A: 83 accepted, 84 rejected): wrong by one. Reading B (84 accepted, 85 rejected) has the right numbers, for a different reason: the terminator is simply not counted. Vectors with the
  length of the whole sentence INCLUDING CR LF: `nmea-len-80` (line body 78), `-81` (79), `-82` (80), `-83` (81),
  `-84` (82, accepted today), `-85` (83, rejected). DP5f will tighten this to NMEA 0183 (82 including CR LF, i.e. 80
  content bytes): `-83` and `-84` then become `reject:Size`; they carry `expected_dvrouter_after_dp5f`. DStarGateway
  accepts all six.
- `$$CRC` covers the trailing CR: CRC-16/X.25 (polynomial 0x8408 reflected, initial 0xFFFF, result
  complemented). Already verified by the DStarGateway oracle (`dprs-aligned` accepted,
  `dprs-crc-without-cr` refused) and stated by the lead.

### Passed through opaque (statement 6)

Fast data (0x8n / 0x9n), the radio header resend (0x5n), code squelch (0xCn) and the filler 0x66 are
passed through by DV-Router without interpretation. No interpretation vectors are needed; the
existing `header-*`, `squelch-*`, `fast-*` and `filler-only` cases stay in the corpus with
`expected_dvrouter` empty (no events). MMDVMHost's header decode (`header-aligned` ...) is only the
reference of what a decoder COULD do.

### Verified and unverified in stage 1b (updated by stage 1c)

| Item | Status |
|---|---|
| Text and GPS expectations of the 43 stage 1 cases, the 299 / 300 / 301 and NMEA 80 .. 85 vectors (DStarGateway result before the DV-Router limit) | VERIFIED against the oracle run (`image dvt-dstarsd:local`, same pins); stage 1c re-ran all of it |
| 66 byte layout of the unit (3 byte bitmap + 21 x 3) | VERIFIED `dstar.rs:122-136`; the bit order of stage 1b was wrong, see above |
| Reset at every sync (text) | decision of the lead; equals MMDVMHost (VERIFIED by the oracle); NOT in the code read, see "Stage 1c" |
| Length nibble rule, limits 300 / 82, hole behaviour, opaque types | VERIFIED by reading `dstar.rs` in stage 1c (limits: 82 is the stored line, the terminator is not counted); still no oracle |

## Stage 1c: the DV-Router source contract (read from `dstar.rs`, VERIFIED line by line)

Authority: `crates/lib/pkt/src/dstar.rs` of the DV-Router work tree (read only, clean tree at commit `0c27de06`),
222 lines, all of them read for this stage, plus the helpers it calls: `dstar_nmea.rs` and `dstar_dprs.rs` (both read
completely) and from `position.rs` the functions `status`, `identity`, `hex` and the first part of `source`. Nothing
of that code is copied here, only facts; `dvtest.dstar_sd.DvrUnitDecoder` is an independent model written from
them, the corpus field `dvrouter_source.rules` carries the same citations. "VERIFIED" below = the cited lines
were read for this stage. Nothing was run on the Rust side (no rig, no `cargo`): the model is checked against
the facts of the source and against the oracles, not against the compiled decoder.

| # | Rule | Lines | Status |
|---|---|---|---|
| 1 | The unit is exactly 66 bytes: shorter `TooShort`, longer `Size`, state reset in both cases | `dstar.rs:123-130` | VERIFIED |
| 2 | The bitmap is little endian, LSB first: `b0 \| b1 << 8 \| b2 << 16`, bit i = frame i | `dstar.rs:136` | VERIFIED |
| 3 | `input[2] & 0xe0 != 0`: state reset, `Invalid` | `dstar.rs:131-135` | VERIFIED |
| 4 | Bit 0 set and frame 0 != `55 2d 16`: state reset, `Invalid` (bit 0 clear: frame 0 bytes not looked at) | `dstar.rs:137-141` | VERIFIED |
| 5 | Blocks are the pairs (1,2) .. (19,20); both bits set or the block is skipped and the GPS line reset (text kept) | `dstar.rs:144-151` | VERIFIED |
| 6 | Unscramble `70 4f 93` repeating over the 6 bytes of a block | `dstar.rs:152-156` | VERIFIED |
| 7 | Only `0x30..0x35` carry GPS bytes (n = low nibble, 0 means 5); every other header byte, `0x36..0x3F` too, is ignored and does NOT reset the GPS line | `dstar.rs:181-185`, `205` | VERIFIED |
| 8 | CR or LF each call `finish`; the stored line is at most 82 bytes (NMEA) / 300 bytes (D-PRS) NOT counting an NMEA terminator; an empty line (CR then LF) produces nothing | `dstar.rs:18-23`, `57-66`, `96-99`, `186-187` | VERIFIED |
| 9 | A push past the limit: `Size`, mode discard, everything up to the next CR or LF is dropped | `dstar.rs:56`, `71-75`, `199-201` | VERIFIED |
| 10 | D-PRS starts when the second byte is `$` too; it needs the CR (an LF is `Invalid`) and a slot for it in the 300 | `dstar.rs:50-54`, `77-81` | VERIFIED |
| 11 | A line error resets the GPS line (`reset_gps`); it is reported as the unit's `Reject` only when the unit produced no item | `dstar.rs:194-197`, `208-212` | VERIFIED (the counting as a worker parse reject is outside the file: UNVERIFIED) |
| 12 | `$GPRMC` / `$GPGGA` without `*`, with a suffix that is not exactly 2 digits, with a non-upper-case-hex digit or with a wrong XOR: `Reject::Crc`, no item; every other sentence is ignored without a check | `dstar_nmea.rs:21-34`, `107-113`; `position.rs:167-178` | VERIFIED |
| 13 | D-PRS: prefix / comma wrong `Invalid`; hex or CRC mismatch (CRC covers the CR) `Reject::Crc` | `dstar_dprs.rs:15-33` | VERIFIED |
| 14 | Text: `0x40..0x43`, 5 bytes & 0x7f into `state.text`, mask bit; a complete set is handed out and the mask cleared; at most ONE text item per unit; `state.text` and the mask persist across units (no clear at a unit start) | `dstar.rs:157-180` | VERIFIED |
| 15 | A rejected unit (rules 1, 3, 4) resets the whole state, text included | `dstar.rs:124`, `133`, `139` | VERIFIED |

### Differences between this reading and the lead's statements

1. **Text still accumulates across units** (rule 14). The lead's list does not mention it; the DP5f decision of stage
   1b (reset at every sync) is therefore recorded as `expected_dvrouter_after_dp5f`, and `expected_dvrouter` is what the code
   does today. Six cases change: `text-slot8-split`, `-slot9-split`, `text-missing-block-late`, `text-two-messages-split`,
   `text-stale-mix`, `text-lost-frame` (and the sequence vector `seq-no-reset-without-reject`).
2. **One text item per unit** (rule 14, `dstar.rs:173-175`): `text-dvrouter-twice` gives ONE text event, the oracles two
   (`DIFF-ONE-TEXT-PER-UNIT`).
3. **Bad checksum scope** (rule 12): `Reject::Crc` is raised for `$GPRMC` / `$GPGGA` only. A GLL / VTG / GSA / GSV with a
   bad checksum is ignored silently, and a valid one is ignored too; a valid RMC / GGA yields no item by itself (only
   the 29 byte station ID line after an RMC fix does, `dstar_nmea.rs:107-148`, not modelled). The corpus `gps` list
   therefore holds RMC / GGA only (`nmea-six-types`: 2 of 6, `DIFF-NMEA-TYPES`), and means "the line passed the line level
   checks". Lower case checksum digits are a `Crc` too (upper-case hex only).
4. **A D-PRS line with a bad CRC** is a `Reject::Crc` as well (rule 13), also one whose CRC leaves out the CR; the
   lead named NMEA only. `dprs-bad-crc`, `dprs-crc-without-cr` carry `reject:Crc`.
5. **The reject is not always visible**: with a text item in the same unit the line error is swallowed (rule 11), see
   `test_model_reports_a_line_error_only_when_the_unit_has_no_text_item`.
6. **An LF in place of the CR that ends a D-PRS line** is `Invalid` (rule 10); `dprs-term-lf`. (After a CR the LF is
   an empty line and gives nothing, for NMEA and D-PRS alike.)
7. The lead's "present bitmap ... frame 20 = bit 4 of byte 2, mask 0x10" matches (rule 2). The "NMEA 82 bytes not counting the
   terminator, 84 with CR LF accepted, 85 rejected" matches (rule 8). The sentence "CR or LF ends a GPS/NMEA line" matches
   (rule 8). No other rule of the lead's statement differs from the source.

### What changed in the corpus

- `unit66_hex` of every case regenerated with the little endian layout (schema `dvt-dstar-sd-corpus/3`).
- `expected_dvrouter` of every case = the model of the code TODAY (basis `dvrouter-source`), with `outcome`: one string per
  unit, `parsed` or `reject:<Reject name>`. `expected_dvrouter_after_dp5f` (basis `lead-statement-unverified`, with the `rule`
  text) is present only where the DP5f rules (NMEA 0183 limit: 80 content bytes, 82 with CR LF; text cleared at every unit)
  change the result: `nmea-len-83`, `nmea-len-84` (now `reject:Size`) and the six text cases of difference 1 (8 cases in all).
  `DvrUnitDecoder(dp5f=True)` models them; the default models the code.
- New cases (63 in all): `nmea-reject-bad-checksum`, `nmea-reject-no-star` (both `reject:Crc`; DStarGateway and DSDcc silent),
  `nmea-term-lf-only` (same as DStarGateway), `nmea-term-cr-only` (DV-Router accepts, DStarGateway waits for an LF),
  `dprs-term-lf` (`reject:Invalid`). The older `nmea-bad-checksum` now carries `reject:Crc` too.
- New top-level `unit_vectors` (19 entries, outcomes stated by hand from the rules, the tests compare them with the model):
  65 and 67 byte units, empty and 132 byte units, spare bits 5, 6, 7, the retired MSB first layout, a wrong frame 0
  (last byte, zeros, scrambled), garbage frame 0 with bit 0 clear (decodes), empty bitmap, frame 20 alone, and four
  sequences that show the state reset of a reject (text segments cleared) and the persistence without one.
- Named differences added: `DIFF-ONE-TEXT-PER-UNIT`, `DIFF-NMEA-DELIM` (the frame number of a CR-ended line differs from
  DStarGateway's LF-ended one), `DIFF-NMEA-TYPES`, `DIFF-CRC-REJECT`, `DIFF-TERMINATOR`; `DIFF-PAIR-HOLE` also covers
  `text-lost-frame`. 22 of the 63 cases differ from DStarGateway's result, every one is named.

### Not modelled (the corpus says so in `dvrouter_source.model_limits`)

The field parsing of a valid `$GPRMC` / `$GPGGA` (time, date, coordinates: `BadPosition` and friends), the 29 byte
station ID line (`nmea-gga-rmc-free` carries `expected_dvrouter.unmodelled`: the free-form line takes that path and may
really be a reject), the D-PRS source binding and position fields after the CRC (`Spoof`, `Source`, `BadPosition`; the
sample line starts its info field with `/`, a timestamped position, which returns no item), the blank-text rule of the
status item. The first two depend on the stream's source callsign, which the corpus does not carry.

## Stage 2: DSDcc, a decoder outside the G4KLX family

Status: delivered and run (2026-11-07), image `dvt-dsdcc:local`, **114 MB**. Question answered: what does a
non-G4KLX decoder make of (a) the two published real RF recordings and (b) our own corpus?

### What was built

- Pins (`.env.example`, never in a script): `DSDCC_REPO` = `f4exb/dsdcc`, `DSDCC_COMMIT`
  `f27b32d2df131ae3a376fe72d3fb880ae1f9ede1` (the commit the research note read, `dstar.cpp` line numbers
  below refer to it), `DSDCC_SAMPLE_WITH_HEADER_SHA256` / `DSDCC_SAMPLE_ON_THE_FLY_SHA256` (the two recordings
  of its `samples/` folder, S16LE 48 kS/s discriminator output: `dstar_f1zil_1.dis` with header,
  `dstar_f1zil_2.dis` captured on the fly), base `debian:bookworm-slim`. The research note's local copy of the
  recordings is the same files (same hash); `scripts/pre-fetch-dsdcc.sh` fetches the pinned commit into
  `.cache/dsdcc` and checks both hashes. The recordings are never committed.
- `rig/Dockerfile.dsdcc`: multi-stage, cmake build of the UNMODIFIED library and its stock tool `dsdccx`,
  `-DUSE_MBELIB=OFF` (no vocoder, no serialDV), non-root user. Three builds, no build error (the first image had only the
  stock tool, the second added our driver, the third its two content-free flags). Run: `docker run --rm -i --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges`, `dvtest.limits.docker_run_args()`, a tmpfs `/tmp`, a timeout.
- The stock tool prints the slow data state with `-M`, but its `-m` option ignores any value below 0.1 s (one line
  per five voice frames, `dsd_main.cpp:260-263`), too coarse. `rig/dsdcc/oracle-dsdcc.cpp` (ours, MIT) therefore
  links the unmodified shared library through its public headers, feeds it the same samples in D-STAR-only mode
  and prints every change of the info text, the locator and which header strings are non-empty. Header strings
  are never printed (flags only).
- `dvtest/oracle_dsdcc.py`: `modulate()` turns the 21-frame superframes of a corpus case into a headerless 4800
  baud two-level stream (bit 1 = positive, the 24 bit slow data field of frame 0 is the sync, bytes LSB first;
  a lead-in of alternating bits, one more sync frame at the end), `run_case()`, `summarize_recording()`.
  `python3 -m dvtest.oracle_dsdcc run` writes `vectors/dstar_sd/dsdcc.json` (per case: the state changes,
  whether the MMDVMHost / DV-Router texts and the DStarGateway texts were shown at some time, the final text).
  The 5 cases with a missing frame (`text-lost-frame`, `text-sync-missing`, `pair-hole-*`) are skipped: an RF
  frame is never absent, it is garbled.
- Tests: `cases/unit/test_oracle_dsdcc.py` (10 tests, no Docker; they also read `dsdcc.json`). `python3 -m dvtest.oracle_dsdcc run --only a,b`
  runs DSDcc for the named cases only and keeps the recorded rows of the others (their text view is recomputed from the recorded states).
  `expected_texts_shown` refers to the DP5f decision (`expected_dvrouter_after_dp5f` when present).

### The two published recordings (VERIFIED run, content NOT recorded)

Both are real voice conversations over a repeater: callsigns, a name, a place and a position are personal data
and are neither printed here nor stored; only counts and shapes are.

| Recording | What DSDcc shows |
|---|---|
| `dstar_f1zil_1.dis` (with header) | a complete printable header (MY, UR, RPT1, RPT2); the info text grows in 8 steps from a 5 character fragment to a **18 character message** (padded to the 20 character field), i.e. the sender transmits text blocks (0x40..0x43) and DSDcc shows them block by block; no valid locator |
| `dstar_f1zil_2.dis` (on the fly) | no usable header (the header strings it holds are not printable: a spurious header sync, bit errors); 3 short text fragments of at most 5 characters (partial or noise, no full message); **a locator of valid shape**, so at least one D-PRS line (`$$CRC` with a valid CRC, plain position) was received |

So the answer to "do the recordings carry text or GPS" (research note, UNVERIFIED until now): recording 1 carries
a text message, recording 2 carries a D-PRS position. What this does NOT give: the block layout (start slot,
interleave pattern, repeat rate) of the real radio, because DSDcc keeps no block events and the driver does not
know where the sync frames fall; a raw dump of the slow data bytes would need a patched decoder, which is not
done (it would mean a modified copy of GPL source). This is NOT an Icom conformance statement.

### Our corpus through DSDcc (VERIFIED, synthetic RF)

58 of the 63 cases decode; all 5 skipped have a hole (the 5 stage 1c cases were added with `--only`: nothing is visible for them, as for every NMEA and D-PRS case). DSDcc has no "message complete" event: the info text field
is overwritten byte by byte, so the comparison is "was the text ever shown" (`expected_texts_shown`,
`lenient_texts_shown`) and what the field holds at the end (`final_text`).

| Finding | Cases | Compared with the oracles |
|---|---|---|
| Aligned and offset messages (slots 0, 1, 3, 6), repeats, `text-dvrouter-twice`, text interleaved with D-PRS / NMEA | `text-aligned` ... `nmea-text-interleave` | shows the same 20 characters as everyone |
| **Accumulates over a sync**: the split messages show the full text | `text-slot8-split`, `-slot9-split`, `text-missing-block-late` | like DStarGateway, NOT like MMDVMHost and not like the DP5f decision for DV-Router (`expected_dvrouter` empty) |
| Old and new blocks mix | `text-stale-mix` shows `N0CALL: CQ DE NORLD.` | identical to the DStarGateway string |
| Both messages of `text-two-messages-split` are shown; the field passes through mixed states while the second one arrives | `text-two-messages-split` | like DStarGateway |
| A missing block leaves its place as it was | `text-missing-block-never` ends as `N0CALL: CQ     0CALL` (block 2 stays blank) | the oracles emit nothing |
| **Self-aligning byte counter, no fixed frame pairs**: the layout that straddles a sync decodes | `text-straddle` shows the full text | MMDVMHost, DStarGateway and the DV-Router unit model all emit nothing; DSDcc is the only decoder here that reads it |
| Bit 7 is NOT stripped: any byte outside 32..127 becomes `.` (`dstar.cpp:400,405`) | `text-high-bit` shows 20 dots | both oracles strip bit 7 and show the text |
| **Radio header resend is practically not decoded**: the 41 header bytes go through the same `.` mapping before the FCS check (`dstar.cpp:400`), so the flag bytes 00 00 00 become 2E 2E 2E and the check fails | `header-aligned`, `-after-squelch`, `-twice`: not decoded; `header-printable-bytes` (all 41 bytes printable ASCII) is the only header it accepts | MMDVMHost decodes the normal header and rejects `header-printable-bytes` (its `getHeader()` masks the flags byte with 0x68 and zeroes bytes 2 and 3 before the FCS check); no header satisfies both |
| D-PRS is decoded (locator `JJ00AA` for the 0 N 0 E test position) only when a **text or header block starts a LATER superframe** (`dstar.cpp:340-355`: the GPS line is evaluated at that moment) and the line contains `DSTAR*:/` | only `dprs-then-text`; `dprs-aligned`, `-slot3`, `-two-lines`, `dprs-text-interleave` give no locator | DStarGateway decodes all of these (`gpsa`); the new case `dprs-then-text` exists for this |
| **256 byte GPS buffer without a bound check** (`dstar.h` `gpsNMEA[256]`, `dstar.cpp:408-409`): lines of 299 / 300 / 301 bytes (and the six NMEA sentences of `nmea-six-types` together) run over into the neighbouring state fields | the `locator` field shows pieces of the line (`xxxxxx`) | DV-Router's 300 byte limit does not fit this reference: do not take DSDcc's behaviour on long lines as a model; it is a robustness defect in the reference |
| NMEA sentences, squelch, fast data, filler: nothing visible | `nmea-*`, `squelch-*`, `fast-*`, `filler-only` | MMDVMHost shows block types, DStarGateway decodes NMEA |

Summary for the DV-Router lead: the three decoders agree on every well-formed, aligned message. They disagree
exactly where the corpus is designed to disagree (split over a sync: DSDcc accumulates, MMDVMHost resets), and
DSDcc adds two behaviours no one else shows (it decodes the straddling layout; it does not decode the normal
header resend). None of this changes the DP5f decision (reset at every sync), but DSDcc must not be cited as a
precedent for the reset: it accumulates (the research note was right). The third decoder next to MMDVMHost and
DStarGateway, for the documented difference `nmea-nibble-short-block` too: DStarGateway emits a garbled
sentence there (it ignores the short block's nibble) that still passes the XOR checksum; DSDcc decodes no NMEA at
all, so for NMEA sentences it only confirms that nothing is visible.

### Verified and unverified in stage 2

| Item | Status |
|---|---|
| Image builds and runs, driver output on the two recordings and on 58 synthetic overs | VERIFIED (this run, 53 at stage 2, 5 more in stage 1c) |
| The synthetic RF stream is what DSDcc locks onto (headerless, 4800 baud, sync = the 24 bit slow data field) | VERIFIED indirectly: the text of every aligned case decodes frame by frame; a real radio's analogue chain is not modelled |
| Content of the recordings | not recorded (personal data) |
| Block layout of the real radios | UNVERIFIED (see above) |
| `dsd-fme`, DroidStar | not run |

## Open questions for the DV-Router lead

Answered in stage 1c (from the source or the lead): NMEA limit reading (reading B's numbers: 82 stored bytes, terminator not
counted, 84 with CR LF accepted, 85 rejected; A was wrong), bitmap bit order (little endian, LSB first), a dropped 0x36..0x3F block (ignored, no
reset; the sentence then fails its checksum), a bad checksum (`Reject::Crc`, line reset; whether the worker counts it is
outside `dstar.rs`).

1. **Text accumulation**: `dstar.rs:161-167` keeps the text segments across units today. Is the DP5f change (clear at
   every unit, like MMDVMHost) still planned as decided? The corpus holds both (`expected_dvrouter` / `..._after_dp5f`).
2. **NMEA types and the ID line**: the code ignores GLL / VTG / GSA / GSV and gives a position item only for the 29 byte
   station ID line after an RMC fix. Should the corpus carry ID lines (they need the stream's source callsign), or is the
   line level view enough? The free-form line of `nmea-gga-rmc-free` takes that path.
3. **What consumes the corpus?** A Rust unit test over `unit66_hex` (cheapest) or also a rig case that sends
   the overs (`over_datagrams` exists, nothing sends it)? The expected values to assert are in
   `expected_dvrouter.outcome` / `.text`; the GPS event `hex` is DStarGateway's APRS line format, only the presence and
   the frame number are meaningful for DV-Router.
4. **A real boundary capture** (the DP5e done-when wording "real Icom boundary capture still wanted")
   stays open: the two DSDcc recordings prove that text and a D-PRS position occur on real RF, but not the block
   layout. A patched decoder that dumps the slow data bytes with the sync position (half a day, modifies GPL code
   locally, would not be committed) or a capture from a radio through a hotspot would close it; do you want it?
5. **DSDcc and the long line**: DSDcc's 256 byte unchecked GPS buffer is a reference defect, not a model; should the
   corpus keep `dprs-len-299` .. `-301` as a DV-Router-only check (it does)?

Answered by the lead and recorded above: reset or accumulate (reset after DP5f, accumulate today), CRC scope (covers the CR),
length nibble (read, named difference), fast data (opaque), GPS limits (300 / 82, terminator not counted).
