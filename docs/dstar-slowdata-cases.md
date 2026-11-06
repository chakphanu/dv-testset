<!-- SPDX-License-Identifier: MIT -->
# D-STAR slow data: spec-derived generator, parser and two independent decoders (HWFREE1 item 4, feeds DP5f)

Stage 1 (the generator, the oracles, 43 cases) is extended by stage 1b (this revision, 56 cases):
the DP5f decision, the DV-Router unit, the length nibble, the GPS limits and the frame-pair holes
are in "Stage 1b: the DV-Router contract" below.

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
   DV-Router lead: DECIDED (stage 1b): reset at every sync, like MMDVMHost.

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
| `dvtest/oracle_dstar_sd.py` | the 58-case corpus, driver input and output text, our views in each oracle's terms, comparison, the thin `docker run` call, `python3 -m dvtest.oracle_dstar_sd run` |
| `rig/Dockerfile.dstarsd`, `rig/dstarsd/` (two drivers, two shims), `scripts/pre-fetch-dstarsd.sh`, `.env.example` (`DSTARSD_*`) | the image `dvt-dstarsd:local`, the drivers (ours, C++) and the one-time source fetch |
| `vectors/dstar_sd/corpus.json`, `published_structure.json`, `README.md` | the vectors (JSON text only) |
| `cases/unit/test_dstar_sd.py`, `cases/unit/test_oracle_dstar_sd.py` | 43 tests (35 + 8) next to the 7 existing ones of `test_slowdata.py` (untouched, passing); the first file also reads the committed corpus and the published structure |
| `cases/unit/test_dstar_sd_dvrouter.py` | 18 tests of stage 1b: the 66 byte unit, nibble policies, the unit model, the expectation fields, the boundary, nibble and pair-hole vectors |
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

V = in `vectors/dstar_sd/corpus.json` (58 cases, `mmdvm` and `dgw` columns, our decoder equal),
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
| Wire frames (21 per superframe, `seq hex6`) for 58 cases: aligned, offset, split, straddling, lost frame, header, D-PRS, NMEA, squelch, fast data | `vectors/dstar_sd/corpus.json` `frames` |
| What a MMDVMHost-family and a DStarGateway-family receiver does with each | `mmdvm`, `dgw` (events `[kind, seq, hex]`), `text_agree`; DV-Router: `expected_dvrouter`, `lenient_alternative`, `unit66_hex` (stage 1b) |
| Boundary lines for the GPS buffers: D-PRS sentence of 299 / 300 / 301 bytes, NMEA of 80 .. 85 bytes including CR LF, valid CRC / checksum | cases `dprs-len-299`, `-300`, `-301`, `nmea-len-80` .. `-85` |
| CRC rule: Icom CRC covers the CR; check values `crc16_x25("123456789") = 0x906E`, `"ABCDEF" = 0x6F5E` | `dstar_sd.crc16_x25`, test_dstar_sd |
| Block structure of real-radio samples (lengths 71 / 74 / 31 / 81 bytes, 17 blocks for D-PRS) | `vectors/dstar_sd/published_structure.json` |
| An over as DSVT datagrams (header, 21 voice frames with null AMBE, last frame) | `dstar_sd.over_datagrams(stream_id, frames)` |

The vectors are plain JSON (`frames` are hex strings): a Rust test can feed the 3-byte fields to
its parser and compare with `dgw` / `mmdvm`.

## Stage 1b: the DV-Router contract (answers of the DV-Router lead, taken as given)

Everything in this section that comes from the lead is a STATEMENT, not an oracle result. Where a
corpus case rests on a statement only, its `expected_dvrouter.basis` is `lead-statement-unverified`;
for the others it is `oracle`.

### Decision (DP5f): reset at every sync, like MMDVMHost

DV-Router RESETS the text segments at every superframe sync, as MMDVMHost does
(`DStarSlowData.cpp:173-181`). Consequences, all in `vectors/dstar_sd/corpus.json`:

- Every case carries `expected_dvrouter` (`basis`, `text`, `gps`): the text events are MMDVMHost's,
  verbatim; the GPS events are DStarGateway's collectors' (MMDVMHost has no GPS decoder) minus the
  lines the DV-Router limits reject. A test checks that `text` equals the `mmdvm` text events for
  every case with basis `oracle`.
- `text-slot8-split`, `text-slot9-split`, `text-missing-block-late` (split over a sync) and
  `text-stale-mix` (old and new blocks mixed) must NOT emit text in DV-Router: `expected_dvrouter.text`
  is empty. `text-two-messages-split` gives one message (MMDVMHost), not two.
- `lenient_alternative` keeps the DStarGateway result (the same events as `dgw`), `lenient_differs`
  says whether it differs from the expectation. Five cases differ by the sync decision alone (the
  ones above); the other differences are named below.
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

**Bitmap bit order (the GENERATOR'S ASSUMPTION, the lead builds the unit from the `seq hex6` lines
himself, this field is a convenience):** a 24 bit big-endian number, MSB first over frames 0..20.
Frame f is bit (23 - f): frame 0 = bit 7 of byte 0, frame 7 = bit 0 of byte 0, frame 8 = bit 7 of
byte 1, frame 16 = bit 7 of byte 2, frame 20 = bit 3 of byte 2; the 3 spare bits (2..0 of byte 2)
are zero. A complete unit starts `fffff8 552d16`. `dstar_sd.unit66()` / `parse_unit66()`; the
corpus is checked to round-trip to the `frames`.

### Block pairing and holes (statement 7; UNVERIFIED by any oracle)

Blocks are the fixed frame pairs (1,2) (3,4) ... (19,20) of a unit. A block that straddles a sync
cannot form; an offset pairing is not resynced (`text-straddle`: no text). A pair with a missing
frame skips that block and resets the GPS line buffer only (the text segments are kept until the
next unit). The oracles do the opposite: they pair the frames they RECEIVE, so one lost frame shifts
their pairing until the next sync. Three cases, `expected_dvrouter` from the unit model
`dstar_sd.DvrUnitDecoder` (the generator's reading of the statement):

| Case | Holes | DV-Router (statement) | MMDVMHost / DStarGateway (measured) |
|---|---|---|---|
| `pair-hole-text-once` | frame 6 (pair 5,6) | no text | no text |
| `pair-hole-text-twice` | frame 4 (pair 3,4), message sent twice in the unit | text at frame 12 (the second copy fills block 1) | none (named difference DIFF-PAIR-HOLE) |
| `pair-hole-nmea-then-ok` | frame 6 in the first unit of GGA + RMC | only the RMC | only the RMC (agree) |

The existing `text-lost-frame` is the case "hole, then a complete repeat": all three give the text
in the second superframe.

### Length nibble of text and data blocks: NAMED DIFFERENCE DIFF-NIBBLE

DV-Router reads the length nibble of the 0x3n data blocks: only 0x30..0x35 are valid (n = 0 means 5),
0x36..0x3F are dropped. DStarGateway's collectors ignore the nibble and add 5 bytes per block. This is a
documented difference, not a failure of either. Vectors (GGA sentence in simple data blocks, both
oracle results recorded, DV-Router expectation = statement):

| Case | Change | DStarGateway | DV-Router (statement) |
|---|---|---|---|
| `nmea-nibble-0x36`, `nmea-nibble-0x3f` | one full block (5 characters) gets 0x36 / 0x3F | sentence decoded | block dropped, checksum fails, nothing |
| `nmea-nibble-low` | a block of 5 characters is marked 0x33 (disagrees with the content) | sentence decoded | 3 characters used, checksum fails, nothing |
| `nmea-nibble-0x30` | a full block gets 0x30 | decoded | decoded (n = 0 means 5), agree |
| `nmea-nibble-short-block` | blocks of 5,5,5,3,5,...: a legal short block (3 characters + 2 filler) in the middle | a GARBLED sentence (the filler `f` bytes are inside) that still passes the XOR checksum, because equal pairs cancel | the correct sentence |

MMDVMHost has no GPS decoder: its column is block types only. Note for the last row: the standard allows
a short block anywhere; DStarGateway's output there is wrong, DV-Router's is right.

### GPS limits and boundaries (statements 3 and 4)

- D-PRS limit: 300 bytes INCLUDING the CR; longer is rejected and the line discarded to the next
  delimiter. Vectors `dprs-len-299`, `-300` (accepted), `-301` (rejected; DStarGateway accepts all,
  named difference DIFF-GPS-LIMIT).
- NMEA / ID line limit: 82 bytes NOT counting the delimiter, one byte looser than NMEA 0183 (82 including
  CR LF). Vectors with the length of the whole sentence INCLUDING CR LF: `nmea-len-80` (line body 78),
  `-81` (79), `-82` (80, the previous vector), `-83` (81), `-84` (82), `-85` (83). The generator reads the
  statement as READING A: the delimiter is the LF, the CR is counted, so 83 bytes with the CR LF are
  accepted and 84 rejected (this matches "one byte looser than 82"). Under reading B (the delimiter is
  CR LF) 84 would be accepted and 85 rejected. `expected_dvrouter.limit_reading` marks `nmea-len-83`
  and `nmea-len-84` as reading dependent; 80, 81, 82 are accepted and 85 rejected in both readings.
  DStarGateway accepts all six.
- `$$CRC` covers the trailing CR: CRC-16/X.25 (polynomial 0x8408 reflected, initial 0xFFFF, result
  complemented). Already verified by the DStarGateway oracle (`dprs-aligned` accepted,
  `dprs-crc-without-cr` refused) and stated by the lead.

### Passed through opaque (statement 6)

Fast data (0x8n / 0x9n), the radio header resend (0x5n), code squelch (0xCn) and the filler 0x66 are
passed through by DV-Router without interpretation. No interpretation vectors are needed; the
existing `header-*`, `squelch-*`, `fast-*` and `filler-only` cases stay in the corpus with
`expected_dvrouter` empty (no events). MMDVMHost's header decode (`header-aligned` ...) is only the
reference of what a decoder COULD do.

### Verified and unverified in stage 1b

| Item | Status |
|---|---|
| Text and GPS expectations of the 43 stage 1 cases, the 299 / 300 / 301 and NMEA 80 .. 85 vectors (DStarGateway result before the DV-Router limit) | VERIFIED against the oracle run of this revision (`image dvt-dstarsd:local`, same pins) |
| Unit model reproduces `expected_dvrouter` for all 58 cases | VERIFIED (test) |
| 66 byte layout of the unit (3 byte bitmap + 21 x 3) | statement of the lead; the bit order is the generator's assumption |
| Reset at every sync (text) | decision of the lead; equals MMDVMHost, VERIFIED by the oracle |
| Length nibble rule, limits 300 / 82, hole behaviour, opaque types | STATEMENT, UNVERIFIED by any oracle; the DStarGateway column shows only what it does instead |

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
- Tests: `cases/unit/test_oracle_dsdcc.py` (9 tests, no Docker; they also read `dsdcc.json`).

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

53 of the 58 cases decode; all 5 skipped have a hole. DSDcc has no "message complete" event: the info text field
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
precedent for the reset: it accumulates (the research note was right).

### Verified and unverified in stage 2

| Item | Status |
|---|---|
| Image builds and runs, driver output on the two recordings and on 53 synthetic overs | VERIFIED (this run) |
| The synthetic RF stream is what DSDcc locks onto (headerless, 4800 baud, sync = the 24 bit slow data field) | VERIFIED indirectly: the text of every aligned case decodes frame by frame; a real radio's analogue chain is not modelled |
| Content of the recordings | not recorded (personal data) |
| Block layout of the real radios | UNVERIFIED (see above) |
| `dsd-fme`, DroidStar | not run |

## Open questions for the DV-Router lead

1. **NMEA limit reading**: is the delimiter of the 82 byte limit the LF (reading A, 83 bytes including CR LF
   accepted) or CR LF (reading B, 84 accepted)? Only `nmea-len-83` and `-84` depend on it.
2. **Bitmap bit order**: the generator uses MSB first, frame 0 = bit 7 of byte 0. Is that your layout?
   (`unit66_hex` is a convenience; the `frames` lines are the source.)
3. **Hole and nibble expectations**: the `lead-statement-unverified` cases (`nmea-nibble-*`,
   `pair-hole-*`) encode your statements. Does a dropped 0x36..0x3F block also reset the GPS line (the
   corpus outcome is the same either way: no sentence)? Is a sentence with a bad checksum counted or
   silent?
4. **What consumes the corpus?** A Rust unit test over `unit66_hex` (cheapest) or also a rig case that sends
   the overs (`over_datagrams` exists, nothing sends it)? The expected values to assert are in
   `expected_dvrouter`; the GPS event `hex` is DStarGateway's APRS line format, only the presence and
   the frame number are meaningful for DV-Router.
5. **A real boundary capture** (the DP5e done-when wording "real Icom boundary capture still wanted")
   stays open: the two DSDcc recordings prove that text and a D-PRS position occur on real RF, but not the block
   layout. A patched decoder that dumps the slow data bytes with the sync position (half a day, modifies GPL code
   locally, would not be committed) or a capture from a radio through a hotspot would close it; do you want it?
6. **DSDcc and the long line**: DSDcc's 256 byte unchecked GPS buffer is a reference defect, not a model; should the
   corpus keep `dprs-len-299` .. `-301` as a DV-Router-only check (it does)?

Answered by the lead and recorded above: reset or accumulate (reset), CRC scope (covers the CR),
length nibble (read, named difference), fast data (opaque), GPS limits (300 / 82).
