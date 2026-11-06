<!-- SPDX-License-Identifier: MIT -->
# D-STAR slow data: spec-derived generator, parser and two independent decoders (HWFREE1 item 4, feeds DP5f)

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
   small drivers and run in one throw-away image (`dvt-dstarsd:local`, 116 MB). 43 overs built
   from the spec were fed to both; the verdicts are in `vectors/dstar_sd/corpus.json` and our
   decoder reproduces all of them (event by event, including frame numbers).
3. **The answer to DP5f's factual part**, measured rather than read: MMDVMHost's receive class
   CLEARS the text segments at every sync frame, DStarGateway's collector KEEPS them. A
   conforming message never needs the difference (a message fits in one superframe), a message
   split over a sync is decoded by DStarGateway only. "Like MMDVMHost" is therefore wrong for
   the accumulation DP5e built; which behaviour DV-Router must follow is a decision for the
   DV-Router lead (open question 1), not for the test set.

Not delivered: any rig case, a feed of these overs into a DV-Router edge (the carrier builder
`over_datagrams` exists and round-trips through `dvtest.slowdata.overs`, nothing sends it), a
third decoder (DSDcc on the two real `.dis` recordings, plan row 3 of section 8, is not done),
a capture from a real Icom radio (none exists, nothing here claims one), and a decision on what
DV-Router should do. Everything the lead has to decide is in "Open questions".

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
| `dvtest/oracle_dstar_sd.py` | the 43-case corpus, driver input and output text, our views in each oracle's terms, comparison, the thin `docker run` call, `python3 -m dvtest.oracle_dstar_sd run` |
| `rig/Dockerfile.dstarsd`, `rig/dstarsd/` (two drivers, two shims), `scripts/pre-fetch-dstarsd.sh`, `.env.example` (`DSTARSD_*`) | the image `dvt-dstarsd:local`, the drivers (ours, C++) and the one-time source fetch |
| `vectors/dstar_sd/corpus.json`, `published_structure.json`, `README.md` | the vectors (JSON text only) |
| `cases/unit/test_dstar_sd.py`, `cases/unit/test_oracle_dstar_sd.py` | 43 tests (35 + 8) next to the 7 existing ones of `test_slowdata.py` (untouched, passing); the first file also reads the committed corpus and the published structure |
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

V = in `vectors/dstar_sd/corpus.json` (43 cases, `mmdvm` and `dgw` columns, our decoder equal),
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
| DStarGateway's sentence collectors add 5 bytes per block and ignore the length nibble; they cut at the last CR (D-PRS) or LF (NMEA) (`SentenceCollector.cpp:34-61`). Ours honours the nibble; the two agree on every well-formed block run, a block with a wrong length nibble is NOT in the corpus | S |
| The GPS collectors keep their buffer across syncs (a D-PRS line is 17 blocks = two superframes, `dprs-aligned`, `dprs-slot3`) | V |

### What the two decoders say about DV-Router's behaviour (DP5e, DP5f)

| DV-Router rule (data.md "Built (DP5e)") | MMDVMHost (V) | DStarGateway (V) |
|---|---|---|
| Four indexed 5-byte segments are kept across superframe units | NO: cleared at each sync | yes |
| The first complete message is consumed and clears the mask | yes | yes |
| Present numbered segments stay usable after missing ones | within one superframe only | across superframes |
| "follows `MMDVMHost/DStarSlowData.cpp:112-170`" | lines 112-159 collect, **173-181 clear at the sync**: the cited lines do NOT show accumulation over a sync | DStarGateway `TextCollector.cpp:120-123` does |

DSDcc is said by the research note to accumulate as well (`dstar.cpp:324, :405`); not run here,
UNVERIFIED.

## Not verified

- No real radio: no capture of an Icom (or any) radio sending text, GPS or header resend exists in
  the public sources searched; every over here is synthetic. Which start slot, interleave and
  repeat pattern real radios use is not known beyond the G4KLX tables; nothing here claims Icom
  conformance. `text-slot8-split` and its relatives are legal bytes, not a known radio behaviour.
- The two oracles are one code family (G4KLX lineage, both reset the pair phase at a sync): their
  agreement with our decoder is evidence about the format, not an independent proof of it. DSDcc,
  dsd-fme and DroidStar were not run (DSDcc / dsd-fme need RF audio or a patched dump hook).
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
| Wire frames (21 per superframe, `seq hex6`) for 43 cases: aligned, offset, split, straddling, lost frame, header, D-PRS, NMEA, squelch, fast data | `vectors/dstar_sd/corpus.json` `frames` |
| What a MMDVMHost-family and a DStarGateway-family receiver does with each | `mmdvm`, `dgw` (events `[kind, seq, hex]`), `text_agree` |
| Boundary lines for the GPS buffers: D-PRS sentence of 300 / 301 bytes, NMEA of 82 / 83 bytes, valid CRC / checksum | cases `dprs-len-300`, `-301`, `nmea-len-82`, `-83` |
| CRC rule: Icom CRC covers the CR; check values `crc16_x25("123456789") = 0x906E`, `"ABCDEF" = 0x6F5E` | `dstar_sd.crc16_x25`, test_dstar_sd |
| Block structure of real-radio samples (lengths 71 / 74 / 31 / 81 bytes, 17 blocks for D-PRS) | `vectors/dstar_sd/published_structure.json` |
| An over as DSVT datagrams (header, 21 voice frames with null AMBE, last frame) | `dstar_sd.over_datagrams(stream_id, frames)` |

The vectors are plain JSON (`frames` are hex strings): a Rust test can feed the 3-byte fields to
its parser and compare with `dgw` / `mmdvm`.

## Open questions for the DV-Router lead

1. **Which behaviour must DV-Router follow (DP5f)?** Accumulate over a sync like DStarGateway
   (what DP5e built) or reset like MMDVMHost? Conforming radios never split a 4-block message
   over a sync (STD6 6.3 starts it in the first four slots), so only non-conforming senders
   differ; the cost of accumulating is the mix of old and new blocks (`text-stale-mix`). The
   DP5e text and `docs/design/data.md` ("follows `MMDVMHost/DStarSlowData.cpp:112-170`") need the
   citation changed to DStarGateway `TextCollector.cpp:120-123`, or the code needs the reset. The
   test set can assert either; it needs your choice.
2. **What does DP5f consume?** The tracker row asks for "vectors from the PDF and a spec-based
   generator test". Is the intended consumer a Rust unit test over `corpus.json` frames
   (cheapest), a rig case that sends these overs to a DExtra / DPlus / DCS edge (then
   `over_datagrams` plus a case file is the next step) or both? Which expected values are
   authoritative for the GPS side: DStarGateway's `gpsa` / `nmea` lines, or DV-Router's own
   position decode?
3. **GPS-A and NMEA limits**: DV-Router uses 300 B and 82 B. DStarGateway has none; cases
   `dprs-len-300/301` and `nmea-len-82/83` are valid lines on both sides of the limit. Should
   DV-Router drop the longer ones (and count them), as data.md says ("overlong GPS lines are
   discarded through their delimiter")?
4. **CRC scope**: the Icom CRC covers the final CR. Does `dv-router` compute and check it that way?
   The standard's own example (`$$CRCEF2E,...`) does not reproduce, so the only reference is the
   DStarGateway behaviour (and the G4KLX sample, whose CRC the research note checked by hand).
5. **Length nibble**: DV-Router honours the 1..5 length of a 0x3n block, the DStarGateway
   collectors do not. For well-formed senders there is no difference; do you want a vector with a
   wrong nibble (the oracle would disagree with the standard)?
6. **A real boundary capture** (the DP5e done-when wording "real Icom boundary capture still
   wanted") stays open: nothing in this stage closes it. Who can record a radio through a hotspot
   or an SDR, and should DSDcc be run over `dstar_f1zil_*.dis` with a slow data dump hook (half a
   day, plan section 8)?
7. **Fast data**: is DV-Router expected to carry fast data (voice fields as data) or only to pass
   it through? If it must interpret it, the byte order needs a real decoder or a better reading of
   STD6 7.3 first.
