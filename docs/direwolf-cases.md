<!-- SPDX-License-Identifier: MIT -->
# Dire Wolf as AX.25 / APRS / KISS reference, and LoRa airtime anchors (HWFREE1 item 3)

## Decision: what this item delivers

The plan (`research-hwfree-clients.md`, part B rows 1 and 2, sections 2a, 3 and 5.1,
5.3) asks for "dvt-direwolf" (a hardware-free packet source and decoder) and "airtime
anchors" (numbers written by somebody who did not write the code under test). It does
not say what DV-Router will consume, so the smallest deliverable that is independently
verifiable is this, all of it offline (no rig run, no DV-Router run):

1. **Byte vectors from an implementation we did not write.** Dire Wolf 1.8.1 runs as a
   process in a throw-away container and produces, for 17 TNC2 lines, (a) the AX.25 frame
   bytes, (b) the real bytes of its KISS TCP port, (c) the length of the 1200 baud AFSK
   audio. For 29 KISS byte strings that WE built (good and deliberately broken ones) it
   reports what its KISS port, its KISSCOPY fan-out, `kissutil` and its log made of them.
   `vectors/direwolf/*.json`.
2. **A pure-Python AX.25 UI / TNC2 / FCS / KISS module** (`dvtest/ax25.py`) that
   reproduces every Dire Wolf vector byte for byte, so the vectors are a regression
   test of OUR encoder and the DV-Router side can reuse them without Docker.
3. **A LoRa time-on-air anchor table** (`dvtest/airtime.py`, `vectors/airtime/anchors.json`):
   exact-fraction implementation of the Semtech formula, a second integer-microsecond
   path, and 23 rows: 4 numbers published by third parties, 3 from DV-Router's own test
   file, 16 derived (LoRa APRS shaped lines and sweeps).

Not delivered: any rig case, a KISS pty bridge into a DV-Router container (`socat`
recipe of the plan, section 2a.4), the `dvt-lora-gw` scripted TNC2/prefix source (plan
row 2, second half), AFSK audio of DV-Router's own TX (see "Not verified"). What the
DV-Router lead has to decide before those make sense is in "Open questions".

Licence rule: dv-testset is MIT. Dire Wolf is GPL-2.0-or-later and runs only as a
process in its own image; nothing of its code or text is in this repository, only
output formats and facts, cited by file and line of the pinned commit.

## Files

| Path | What |
|------|------|
| `dvtest/ax25.py` | address field, UI frames, TNC2 text (`<0xHH>` escapes), CRC-16/X-25 FCS, bit stuffing, bits on the air, KISS encode and a streaming KISS decoder |
| `dvtest/airtime.py` | LoRa time on air (exact fractions + integer microsecond path), the anchor table, `python3 -m dvtest.airtime` writes `vectors/airtime/anchors.json` |
| `dvtest/oracle_direwolf.py` | case tables (17 + 2 RX lines, 29 TX byte strings, 4 kissutil lines), debug-log parsers, record builders, the thin `docker run` call, `python3 -m dvtest.oracle_direwolf run` |
| `rig/Dockerfile.direwolf`, `rig/direwolf-entry.py`, `scripts/pre-fetch-direwolf.sh`, `.env.example` (`DIREWOLF_*`) | the image `dvt-direwolf:local`, its driver (ours, Python) and the one-time source fetch |
| `vectors/direwolf/rx_frames.json`, `kiss_tx.json`, `vectors/airtime/anchors.json` | the vectors (JSON text only, no audio) |
| `cases/unit/test_ax25.py`, `test_airtime.py`, `test_oracle_direwolf.py` | 56 unit tests; the first one also reads the Dire Wolf vectors |

## Dire Wolf image: build and run (VERIFIED, 2026-11-06)

- Pin: `DIREWOLF_REPO` / `DIREWOLF_COMMIT` = `wb2osz/direwolf` `a231971a652bfb574a4bae9a5d875fbce53d2267`
  (tag 1.8.1), base `debian:bookworm-slim` (`DIREWOLF_BASE_IMAGE`); `scripts/pre-fetch-direwolf.sh`
  fetches it into `.cache/direwolf`. The plan suggested the Debian package (1.7); the
  source build is 1.8.1 (`direwolf -h` prints "Dire Wolf Release 1.8.1, November 2025").
- Build: `scripts/contained.sh --name build -- docker build -f rig/Dockerfile.direwolf ...` (the
  command is in the Dockerfile header). Multi-stage, cmake Release, only the targets
  `direwolf gen_packets atest kissutil decode_aprs`, stripped; on Linux only ALSA is REQUIRED
  (`libasound2-dev`), gpsd, hamlib, libgpiod, udev and Avahi are optional and left out.
  Final image: bookworm-slim + `python3` + `libasound2` + five binaries + symbol/tocalls data
  files, non-root user. **Image size: 180 MB on disk (44.8 MB content)**, far under the 600 MB limit.
- Build attempts: 1 failed (cmake's CPack include checks that `README.md` exists in the source
  tree: copied in), 2 succeeded. The plan's `python3-minimal` cannot be used: it has no
  `json` module; `python3` (+ about 30 MB) is installed instead. Later rebuilds only changed
  the driver script (cached layers).
- Run: `docker run --rm -i --network none --read-only --cap-drop ALL --security-opt
  no-new-privileges`, `dvtest.limits.docker_run_args()` (1 GB, 2 CPUs, 512 pids), 64 MB
  tmpfs, one JSON job on stdin and one JSON result on stdout (`dvtest.oracle_direwolf.docker_argv`).
  Everything is on loopback inside the container. A full run (RX + TX jobs) takes about 90 s.
- Traps found while building the driver:
  * Dire Wolf's log must be read through a pty: with stdout on a pipe, the "Attached", "gone
    away" and "Ready" lines come in bursts seconds late (the driver uses `pty.openpty()` + raw mode).
  * A client socket must be `shutdown()` before `close()` when another thread is blocked in
    `recv()` on it, otherwise the connection stays open and Dire Wolf never frees the slot
    (it serves 3 clients at a time: the copy client, kissutil and the sender).
  * The KISS decoder state is per client connection, so every TX case uses a NEW connection.
  * `gen_packets` keeps a line's trailing newline as an information byte (it reads with `fgets`,
    gen_packets.c about line 705): one file per frame, no newline.
  * `ADEVICE stdin null` works: the receive audio comes from a pipe that must stay open (EOF
    ends the program), the transmit audio goes nowhere. The text "null" output means the
    AFSK audio of a transmitted frame is NOT available for decoding (no stdout audio device).

## Facts read from the oracle

"V" = in the committed vectors and checked by `cases/unit/test_ax25.py`; "S" = read from the
Dire Wolf source (cited), not shown by a vector; "M" = seen once in a manual run, not a vector.

### TNC2 text to AX.25 bytes (Dire Wolf's encoder, via `gen_packets` and `atest -h`)

| Fact | Status |
|------|--------|
| Frame = destination, source, 0 to 8 digipeaters (7 bytes each, callsign characters shifted left one bit, space padded), control 0x03, PID 0xF0, information. No flags, no FCS in what `atest -h` prints | V (17 lines) |
| Destination and source SSID bytes both have bit 7 set (command/response bit 1) and the two reserved bits 11: `N0CALL-7` source = `ee`, `APRS` destination = `e0` | V; open question 3 |
| Address extension bit set on the last address only | V |
| `*` after a digipeater sets the has-been-repeated bit on it AND on every digipeater before it; `WIDE2-1*` (last) marks both digipeaters | V (`digi-used-first`, `digi-used-last`) |
| Nine digipeaters: the ninth is dropped without a message (`neg-9-digis` frame equals the 8-digi frame) | V |
| A lower case callsign makes `gen_packets` produce no frame at all (empty WAV) | V (`neg-lowercase`) |
| `<0xHH>` in the information text is one byte; empty information is accepted; 340 information bytes (over 256) are accepted | V |
| Frame length window 15 to 2123 bytes (14 = two addresses only, refused; 15 = plus a control byte, accepted) | V (TX side message), S ax25_pad.h about 55-57, ax25_pad.c about 669 |

### Dire Wolf receive path: the real bytes on its KISS TCP port

| Fact | Status |
|------|--------|
| One frame = `C0 00 <AX.25 frame without FCS> C0`: type byte 0x00 (port 0, data command) | V (17 of 17 equal `dvtest.ax25.kiss_encode(0, 0, frame)`) |
| Consecutive frames are `... C0 C0 00 ...`: closing FEND of one and opening FEND of the next, nothing in between | V (`kiss_stream_hex` is the 17 frames back to back) |
| FEND in the information becomes `DB DC`, FESC becomes `DB DD`; the escape case (information bytes `C0 DB DB DC DB DD C0 C0`) from a real demodulator is `6d db dc 78 db dd 79 db dd dc db dd dd db dc db dc` | V (`info-fend-fesc`) |

### KISS bytes sent to Dire Wolf (our bytes, judged by its decoder; `-d n -d n` log, KISSCOPY, kissutil)

| Case | What Dire Wolf did | Status |
|------|--------------------|--------|
| canonical data frames, information up to 2029 bytes (2048 FEND to FEND) | taken; log shows the frame after unescaping, equal to ours; KISSCOPY copies the exact bytes to the second client; kissutil prints the TNC2 line | V |
| escapes `DB DC`, `DB DD` | unescaped to C0 and DB | V |
| port nibble 1 on a one-channel TNC | the frame is logged as "Invalid transmit channel 1" and not transmitted, but still copied to the other clients | V |
| TXDELAY / P / SlotTime / TXtail / FullDuplex (0 and 1) / SetHardware | each is logged as `KISS protocol set <name> = <value>, chan <n>`; none is copied to other clients; for port 1 it says `chan 1` | V |
| SetHardware with the text `TNC:` | the TNC answers the sender with a KISS frame `C0 06 "DIREWOLF 1.8" C0` (hex `c00644495245574f4c4620312e38c0`) | V (`dw_client_rx_hex`) |
| command nibble 7 | `KISS Invalid command 7`, nothing else | V |
| Return (0xFF) | logged and ignored ("end KISS mode - Ignored") | V |
| `C0 C0` (empty frame) | nothing happens, nothing logged | V |
| data frame of 0, 3 or 14 bytes | copied, then rejected: "Frame length N not in allowable range of 15 to 2123" and "Invalid KISS data frame" (kissutil says the same) | V |
| 15 bytes (two addresses + control) | taken; kissutil prints `N0CALL-7>APRS:` | V |
| no opening FEND on a new connection | the whole byte string is "Rejected Noise" (it needs a FEND to start a frame) | V |
| two FEND in front, or bytes `41 42` before the first FEND | the extra FEND is ignored; `41 42` is logged as "Rejected Noise" and the frame is taken | V |
| `DB 41` (bad escape) | log line `KISS protocol error. Found 0x41 after FESC.`; the frame is still taken and the copy to the other clients is the canonical re-encoding (the unwrapped frame in its log has both `DB 41` dropped: 38 bytes, equal to the good frame) | V |
| bad escape followed by a good frame | both taken: the decoder recovers | V |
| frame without closing FEND followed by a complete frame | the first frame is taken when the next FEND arrives, the second's leading FEND was consumed, so the remainder is "Rejected Noise" (only ONE frame comes out) | V |
| FEND in the middle of the information | the frame is cut there (18 bytes taken), the rest is "Rejected Noise" | V |
| 2030 information bytes = 2049 bytes FEND to FEND | "KISS frame should end with FEND." and "should not have FEND in the middle", frame refused; the copy it sends and logs is 2242 bytes of which only the front is ours (the rest is stale memory, different in every run): **not stored in the vectors**, lengths only. `MAX_KISS_LEN` is 2048 including both FEND and the type byte (kiss_frame.h about line 47) | V (messages, lengths), S |
| 2123-byte frame (2107 information bytes) in an earlier run | the process died (SIGSEGV, exit -11) after "KISS message exceeded maximum length" lines; not repeated in the committed run | M, not a vector |

`kissutil` as an encoder (TNC2 line dropped into its transmit directory, KISSCOPY shows the
bytes): 4 of 4 equal `kiss_encode(0, 0, from_tnc2(line))`, including the escape line.

### AFSK 1200 baud length (the "AFSK airtime" anchor)

`gen_packets` writes a random-ish quiet time of 32 to 33 symbols, then 32 flag octets, the
frame (opening flag, bit-stuffed data + FCS, closing flag) and 2 flag octets (gen_packets.c
about lines 190-207). For all 17 frames the WAV length is exactly

    samples = 1170 (+ rounding, 1170.0 to 1170.75 seen) + bits * 44100 / 1200,
    bits = 8 * (32 + 2) + 8 + 8 * (frame_bytes + 2) + stuffed_zeros + 8

with `dvtest.ax25.air_bits(frame, preamble_flags=32, postamble_flags=2)` (V). The frame
itself, without preamble and postamble flags, takes `air_bits(frame)` bits: 47-byte
`N0CALL-7>APRS,WIDE1-1:!0000.00N/00000.00E>test` = 410 bits = 341.667 ms; 17-byte `A>B:x` =
170 bits = 141.667 ms; the 356-byte frame = 2881 bits = 2400.833 ms. TXDELAY and TXtail
come on top and are not part of the frame.

## Airtime anchors (LoRa) - read this before trusting them

`dvtest/airtime.time_on_air` is written from the Semtech formula alone (section 4.1.1 of the
SX1276 datasheet / AN1200.13: symbol time 2^SF / BW, preamble + 4.25 symbols, payload symbols
from the ceil expression with CR, CRC, header and low-data-rate terms), with exact fractions.
`time_on_air_us_style` is a second path (symbol time truncated to whole microseconds, symbols
counted in quarters); the two agree exactly for 125, 250 and 500 kHz (unit test, 6 SF x 3 BW x
6 payload sizes).

| Kind | Rows | Worth |
|------|------|-------|
| `external` | PL 10 SF7 = 41.216 ms; PL 10 SF11 (low data rate on) = 577.536 ms; PL 64 SF12 = 2793.472 ms (published as 2793.5); PL 68 SF12 = 2957.312 ms (published as 2957.31); all BW 125 kHz, CR 4/5, preamble 8, CRC on, explicit header | independent of DV-Router, **but copied from the research note**: the pages (a third-party calculator, a TTN forum thread) were not re-read in this stage. UNVERIFIED as quotes; the computed values do agree with them to the printed precision |
| `project` | PL 80 SF12 = 3284.992; PL 20 SF7 = 56.576; PL 1 SF7 = 25.856 (DV-Router `airtime_tests.rs`) | reproduced, not independent: the same agent family wrote those tests and `airtime.rs` |
| `derived` | LoRa APRS shaped lines (PL = 3 + length of `N0CALL-7>APLRT1:>Test status` = 31, 51, 62 bytes) at SF12 / SF9 / SF7 BW125; CR 4/5..4/8 at PL 50 SF12; BW 250 and 500 at PL 30 SF8; low data rate forced off at PL 20 SF12 | regression numbers of this module (they also equal the research note's table) |

A typical LoRa APRS position beacon at SF12 BW125 is about 2.5 s on the air (51-byte row:
2465.792 ms). No measured airtime (SDR, oscilloscope) exists; nothing here is a measurement.
PL 62 and PL 64 both give 2793.472 ms (the symbol count steps in blocks of 5).

## Not verified

- Nothing has run against DV-Router. No DV-Router adapter, edge or config was touched.
- Dire Wolf's own TX audio for a KISS frame is not captured, so "KISS bytes in, AFSK audio
  out, decoded again" is not closed; the KISS to TNC2 decode is by `kissutil` and the log.
- The AFSK model is checked against one tool's output (its own length); no radio.
- Dire Wolf is one implementation: where it differs from the AX.25 / KISS specifications (the
  c/r bits, the silently dropped ninth digipeater, the recovery from bad escapes) the vectors
  say what Dire Wolf does, not what is right.
- The WA8LMF TNC test CD (a large real frame corpus) was not fetched; `decode_aprs` is built
  but not run; AGW (port 8000) is switched off in the config; Bluetooth/USB/serial quirks
  of real TNCs are out of reach.
- External airtime numbers: see above.

## Numbers the DV-Router side can use

| Need | Where |
|------|-------|
| TNC2 line to AX.25 bytes, 17 lines incl. digipeater `*`, SSID 0..15, 8 digipeaters, empty information, 340 bytes, binary information | `vectors/direwolf/rx_frames.json` `dw_ax25_hex` |
| The same as the KISS bytes a TNC sends (RX path of a `kiss-tcp` / `kiss-serial` adapter) | `dw_kiss_hex`; stream of all frames: concatenate them |
| What a TNC does with KISS from the host: parameter frames, port nibble, odd framing, size limits | `vectors/direwolf/kiss_tx.json` (`dw_messages`, `dw_unwrapped_hex`, `dw_copy_hex`, `kissutil_frames`) |
| TNC reply to SetHardware | `param-sethardware` `dw_client_rx_hex` |
| LoRa time on air, ms and exact | `vectors/airtime/anchors.json` (`computed_ms`, `us_style_us`, `published`) |
| AFSK frame time at 1200 baud | `ours_frame_air_ms` in `rx_frames.json`; formula `dvtest.ax25.air_bits` |

The vectors are plain JSON; a Rust test can read them verbatim (compare `from_tnc2`-equivalent
encoder output to `dw_ax25_hex`, adapter output to `dw_kiss_hex`).

## Open questions for the DV-Router lead

1. **What consumes these?** The plan names DP11b (KISS serial / rfcomm adapter, PTY tests) and
   "DP9" (not defined in this repository; I took it as the APRS path behind the adapter). The
   cheapest consumer is a DV-Router unit test that reads the JSON. A rig case (socat pty from
   a Dire Wolf TCP port into the adapter's `device`, plan section 2a.4) needs a rig service
   and the adapter's baud/pty behaviour; is it wanted?
2. **DP9 expected values**: if DP9 means APRS field parsing, `decode_aprs` (built) or the
   `aprslib` parser (not installed) could give field-level expectations for the TNC2 lines;
   which fields does DV-Router parse?
3. **Command/response bits**: Dire Wolf's text encoder sets bit 7 on both destination and
   source (`e0` / `ee`...). AX.25 v2.2 commands have destination 1 and source 0. Which does
   DV-Router emit and should tests compare bytes exactly or mask bit 7 of both SSID bytes?
4. **Error behaviour to copy or not**: what should the adapter do on noise before the first FEND,
   a bad escape (Dire Wolf keeps the frame, drops the byte), an unescaped FEND inside a frame,
   frames outside 15..2123 bytes, KISS messages over 2048 bytes? The vectors show Dire Wolf's
   choice only.
5. **Parameter frames**: does the adapter send TXDELAY/P/SlotTime/TXtail/FullDuplex at start,
   and does it expect any answer (Dire Wolf answers only SetHardware, and only that one)?
6. **Airtime**: accept the four `external` anchors as quoted from the research note, or have
   someone re-read the two source pages (and add the RadioLib `calculateTimeOnAir` cross-check
   the plan mentions, about half a day)? Is AFSK time on air used anywhere in DV-Router?
7. **Scope split**: `dvt-lora-gw` (scripted TNC2 source with and without the `3C FF 01` prefix,
   CRLF, bad prefix, overlong lines) is the other half of plan row 2 and is not done here.
