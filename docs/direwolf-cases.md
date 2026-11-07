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
   audio. For 43 KISS byte strings that WE built (good and deliberately broken ones) it
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
recipe of the plan, section 2a.4), AFSK audio of DV-Router's own TX (see "Not verified").
What the DV-Router lead has to decide before those make sense is in "Open questions".
The other half of plan row 2, the `dvt-lora-gw` scripted TNC2 / prefix source, is delivered
separately: `docs/lora-gw-cases.md`. Later stages added the APRS decode oracle (DP9) and the
documented differences with DV-Router (sections below).

Licence rule: dv-testset is MIT. Dire Wolf is GPL-2.0-or-later and runs only as a
process in its own image; nothing of its code or text is in this repository, only
output formats and facts, cited by file and line of the pinned commit.

## Files

| Path | What |
|------|------|
| `dvtest/ax25.py` | address field, UI frames, TNC2 text (`<0xHH>` escapes), CRC-16/X-25 FCS, bit stuffing, bits on the air, KISS encode and a streaming KISS decoder; the model of the DV-Router lead's statements (`mask_cr_bits`, `dvrouter_accepts_frame`, `DvrouterKiss`, `dvrouter_startup_params`) |
| `dvtest/airtime.py` | LoRa time on air (exact fractions + integer microsecond path), the anchor table, `python3 -m dvtest.airtime` writes `vectors/airtime/anchors.json` and `vectors/airtime/afsk_bound.json`; the DV-Router AFSK airtime bound in integer nanoseconds (`afsk_airtime_ns`, `afsk_frame_ns`, `afsk_rows`; `afsk_upper_bound_ms` is a display helper only) |
| `dvtest/oracle_direwolf.py` | case tables (17 + 2 RX lines, 43 TX byte strings, 4 kissutil lines; `run --only tx` rewrites `kiss_tx.json` alone), debug-log parsers, record builders, the thin `docker run` call, `python3 -m dvtest.oracle_direwolf run` |
| `dvtest/aprs_expect.py` | the generator's EXACT expected values for all 93 APRS cases (`expect` in every record) and the compare policy (`compare`, `position_within_half_precision`); see "APRS compare policy" |
| `dvtest/aprs_cases.py`, `dvtest/oracle_aprs.py` | 76 spec-derived APRS information strings (one group per APRS type) with their small encoders (compressed, Mic-E); the `aprs` job, the `decode_aprs` output parser and the record builder, `python3 -m dvtest.oracle_aprs run` |
| `rig/Dockerfile.direwolf`, `rig/direwolf-entry.py`, `scripts/pre-fetch-direwolf.sh`, `.env.example` (`DIREWOLF_*`) | the image `dvt-direwolf:local`, its driver (ours, Python; also the modem loop service `entry.py loop` and the job `looptest`) and the one-time source fetch |
| `cases/unit/test_direwolf_loop.py` | unit tests of the pure parts of the modem loop (sample aligned datagrams, configuration text, FIFO relay, KISS reader) |
| `vectors/direwolf/rx_frames.json`, `kiss_tx.json`, `aprs_decode.json`, `vectors/airtime/anchors.json`, `afsk_bound.json` | the vectors (JSON text only, no audio) |
| `cases/unit/test_ax25.py`, `test_airtime.py`, `test_oracle_direwolf.py`, `test_oracle_aprs.py`, `test_dvr_differences.py` | unit tests; the first and the last also read the Dire Wolf vectors |

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

## Modem loop: two Dire Wolf instances without hardware (VERIFIED, 2026-11-07)

The DP15 tier B RF cases (`docs/dp15-cases.md`) need a transmit on one real Dire Wolf to be heard by another. Both
directions of the question were read in the pinned source (`.cache/direwolf/src/audio.c`, facts only):

| Direction | What 1.8.1 accepts | Used for the loop |
|-----------|--------------------|-------------------|
| audio in | an ALSA capture device, `stdin` (or `-`), `udp:<port>` (datagrams of at most 2000 bytes, raw 16 bit samples, bound on all addresses) | `udp:7355` |
| audio out | an ALSA PCM name only ("Only soundcard is supported"); no stdout, pipe or UDP | the ALSA `file` PCM |

The ALSA `file` PCM (`ADEVICE udp:7355 file:/tmp/dw/tx-a.raw`; the `pcm.file` of `/usr/share/alsa/alsa.conf`, plugin
code inside libasound2, slave `null`, raw format) writes every sample Dire Wolf transmits to a path. The path is a FIFO.
A relay of ours (`Relay` in `rig/direwolf-entry.py`, stdlib Python) reads the FIFO and sends the samples as
1024-byte UDP datagrams to the audio input of the peer: so Dire Wolf A's modulator output reaches Dire Wolf B's
demodulator, in both directions, in one container (self test) or in two containers (rig services `dw-a` / `dw-b`).
**No change to the image** (still 180 MB; `libasound2` already carried the plugin), no sound card, no privilege.

What the relay must do (each point was an experiment, not a guess):

* open the FIFO read/write, so there is never an end of file, and Dire Wolf's open for writing never blocks;
* keep the datagrams sample aligned (an odd split would turn every later sample into noise);
* pace the datagrams at 4 times real time: Dire Wolf writes a whole frame at once (no sound card to wait for), a
  600-byte frame is 400 KB of audio; the FIFO (64 KB) gives the back pressure, the UDP receive buffer never dropped
  a datagram (`/proc/net/udp` drops 0 in every run);
* send 0.3 s of silence after the last sample, so the receiving modem sees the carrier go away (a real radio does).

Evidence (`python3 -m dvtest.oracle_direwolf loop`, a throw-away container, `--network none`, the repository limits):

| Run | Result |
|-----|--------|
| first spike, FIFO read by a bridge into the other instance's `stdin`, 1 frame A to B | the frame came out of B's KISS port byte for byte (`B [0.3] N0TEST-7>APDVT1::XLX999B  :DVT spike1{3`, audio 60 KB) |
| two instances, UDP audio, 8 frames of 40 information bytes, alternating A to B and B to A, 0.3 s apart | 8 of 8 equal; latency from the KISS write to the KISS read 0.5 to 1.6 s (CSMA wait, TXDELAY, the frame itself 0.7 s of audio) |
| 4 frames of 600 information bytes (629 bytes, 4.5 s of audio each), one direction | 4 of 4 equal, latency 2.6 to 5.5 s |
| the same, alternating, 8 s between frames; and 4 s between frames | 4 of 4 and 4 of 4 equal |
| two containers of the final image on an internal Docker network with the compose limits (256 MB, 0.5 CPU, 128 pids), 3 frames of 300 bytes A to B, B to A, A to B | 3 of 3 equal, 1.7 to 2.0 s; each container 9.5 MB and 11 processes at rest, 0.2 % CPU |
| `atest` on the audio the relay copied (job `tap`) | every transmission decodes, `audio level = 199`: a clean full-scale signal (Dire Wolf warns "input level too high" at each frame; harmless) |

Traps found:

* **A node is deaf to what arrives while its own transmission would still be on the air.** With 600-byte frames and
  2 s between the frames, the frame in the other direction was transmitted (the relay carried it, `atest` decodes it)
  but the node that had just transmitted printed nothing and sent nothing to its KISS clients; with 4 s or more between
  the frames every frame arrived. The cause was not traced to a source line; it fits a half duplex TNC (the KISS
  FullDuplex parameter that DV-Router writes is 0) that counts the nominal air time of its own frame. The tier B cases therefore
  keep a pause of 30 s or more between a transmission and a frame in the other direction (their spacing is 31 s anyway).
* `KISSPORT 8002` in the configuration file ADDS a port to the built-in default 8001; the second instance of a
  container then fights for 8001. `KISSPORT 0` first removes the default.
* A KISS client socket made with `create_connection(timeout=3)` keeps that timeout for `recv()`: the reader thread dies
  after 3 s of quiet and the client "never receives" (a test bug, not Dire Wolf).
* The loop is per DIRECTION: A transmits, B hears. A node does not hear its own transmission and Dire Wolf does not give a
  client its own transmitted frame back (unless `KISSCOPY`, which copies a frame one KISS client sends to the others;
  `DW_KISSCOPY=1` of the entry script; not used by the rig).

Not shown: a loop with noise, fading or two transmissions at once (no collision model); the timing is not that of a radio
(the audio leaves 4 times faster than real time).

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
| port nibble 2 and 15 (like 1) | logged "Invalid transmit channel N from KISS client app.", frame not transmitted, but taken by the KISS layer and copied | V (`data-port2`, `data-port15`) |
| command nibbles 8 to 14 (like 7) | `KISS Invalid command N` and the one data byte as "unwrapped"; nothing copied | V (`cmd-unknown-8` .. `cmd-unknown-14`) |
| 1100 and 1101 data bytes after the type byte, plain and with 20 escape pairs inside (1120 / 1121 bytes on the wire) | all four taken, copies equal the bytes sent, kissutil prints the TNC2 line: Dire Wolf's own limit (2048 FEND to FEND) is far above DV-Router's, so these are DV-Router boundary vectors only | V (`limit-1100-plain`, `limit-1101-plain`, `limit-1100-escapes`, `limit-1101-escapes`) |
| FESC directly before the closing FEND, then a complete frame | the first frame is taken with the FESC dropped (20 bytes), the following frame is "Rejected Noise" (the FEND closed the first one and did not open the next) | V (`fesc-before-fend`) |
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

## APRS decode oracle (DP9)

Answer of the DV-Router lead: DP9 is the "APRS core" of `dv-lib-pkt`: field parsing of messages,
acks, rejects, bulletins and announcements; positions plain, compressed and Mic-E, with
timestamps; PHG / RNG / DFS / course-speed; weather; DAO; ambiguity; status; objects and items;
third party (one level). The wanted oracle is Dire Wolf's own APRS decoder, `decode_aprs`.

- `decode_aprs` was already in the image (the Dockerfile builds it as the fifth target and
  installs it into `/usr/local/bin`); only a driver mode was added (`aprs` in
  `rig/direwolf-entry.py`, our code), so the image content is unchanged: **180 MB on disk,
  44.8 MB content** (one cached rebuild, no build error). Run as before: `--network none`,
  `dvtest.limits.docker_run_args()`, `--read-only`, `--cap-drop ALL`, a timeout.
- Input: for each case the AX.25 frame (`N0CALL-9>DEST,WIDE1-1:` plus the information bytes,
  built by `dvtest.ax25`) as one line of hex, which `decode_aprs` reads in its raw-frame mode, so
  the information bytes arrive exactly as written (Mic-E has control characters).
- Cases: the 17 information fields of `rx_frames.json` (group `existing`, names `ex-<case>`) and 76
  new strings in `dvtest/aprs_cases.py`, 93 in all, 19 groups (message, ack, reject, bulletin,
  announcement, position, ambiguity, timestamp, compressed, mic-e, extension [PHG / RNG / DFS /
  course-speed], weather, dao, status, object, item, third-party, negative). Two or more strings
  per type; edge cases: ambiguity 1 to 4, compressed with course/speed, with altitude, with radio
  range and with a blank cs pair, eight Mic-E strings (message codes M0 to M6 and Emergency,
  custom C1, both data type indicators, longitude under 10 and over 100 and over 110 degrees,
  status text, altitude in the comment), weather with and without position, timestamps in DHM
  (zulu and local), HMS and MDHM (the MDHM form exists only in the positionless weather report),
  killed object and item, third party position / message / status. Callsigns are N0CALL / N0TEST /
  N0DEST style, the destination is `APZ000`, coordinates are invented (open sea, the equator)
  or the specification's own examples; no real callsign, e-mail address or location of a person.
- Output: `vectors/direwolf/aprs_decode.json` (schema `dvt-direwolf-aprs/1`). Per case: `info_hex`
  (the raw information string), `info_text`, `frame_header`, `dw_lines` (everything `decode_aprs`
  printed after its AX.25 dump: the authority) and `fields`, a best-effort parse of those lines
  with normalised keys: `data_type`, `name`, `symbol` (or `symbol_and_vendor` when the vendor is
  known), `mic_e_status`, `phg` (`power_w`, `height_ft`, `height_m`, `gain_dbi`, `directivity`),
  `range_miles`, `message_number` / `message_from` / `message_to`, `by_station` / `of_station`
  (ack, reject), `bulletin_id` / `bulletin_group`, `position` (`lat`, `lon` as signed decimal
  degrees from the printed degrees and minutes, plus the printed text, `speed_kmh`, `speed_mph`,
  `course_deg`, `alt_m`, `alt_ft`, `grid`), `weather` (`wind_mph`, `wind_dir_deg`, `gust_mph`,
  `temp_f`, `rain_1h_in`, `rain_24h_in`, `rain_midnight_in`, `humidity_pct`, `barometer_inhg`,
  `luminosity_wm2`, `software`), `comment`, `warnings`, `extra_lines`, `error`. Two runs give
  byte-identical files. Dire Wolf's code and text are not in the repository, only what it printed.

### Facts read from the oracle (V = in `aprs_decode.json`, checked by `test_oracle_aprs.py`)

| Fact | Status |
|------|--------|
| The decoded position of every Mic-E string equals the latitude / longitude / speed / course / message code the string was built from (lat and lon within 0.0002 degree); the status words are `Off Duty`, `En Route`, `In Service`, `Returning`, `Committed`, `Special`, `Priority`, `Emergency` for the standard codes and `Custom-N` for custom ones. So the Mic-E encoder in `aprs_cases.py` and Dire Wolf agree | V |
| The compressed example of APRS 1.0.1 (`!/5L!!<*e7>7P[`) decodes to 49 30.0000 N, 72 45.0002 W, 67 km/h (42 MPH), course 88; our encoder reproduces that string exactly. The 0.0002 minute is the compression resolution | V |
| Compressed altitude 1000 ft prints as 999 ft (305 m); radio range prints as `range=20.1` for 20 miles; a blank cs pair prints position only | V |
| Timestamps (DHM zulu / local, HMS, MDHM) are consumed but NOT printed: the output says `Position with time` or `Positionless Weather Report` and nothing of the time. A time value cannot be checked against this oracle | V |
| Ambiguity: the blanked digits come out as zeros (`49 00.0000` for level 3 and 4) and the level is not printed: DV-Router has to be checked against the string, not against a printed level | V |
| Weather: the wind speed is mph in both forms but the input differs in meaning: `s004` in a positionless report prints `4.0 mph`, the `/004` data extension of a position report prints `4.6 mph` (read as knots); missing fields (dots) are simply absent from the line; the software tag `wRSW` is printed quoted | V |
| Weather with a position is a `Weather Report` whose symbol is `WEATHER Station (blue)`; without a position it is a `Positionless Weather Report` with no location line | V |
| DAO: `!W83!` (human readable) adds 0.0080 minute to latitude and 0.0030 to longitude and the `!W83!` text is removed from the comment; base-91 `!wfe!` adds 0.0076 / 0.0075; BUT a DAO that is the whole comment (nothing in front of it) is left in the comment and not applied (`dao-base91-bare`) | V |
| Message: the number is the text after the first `{`; the text `text {with} braces inside{77` (a brace is not allowed in the text by the specification) gives the number `with} brace`, a warning about its length, and the decoder keeps going | V |
| Ack and reject are printed as `"<packet source>" ACKnowledged message number "N" from "<addressee>"`; a bulletin as `Bulletin with identifier "3"`, with a group as `Group Bulletin with identifier "1", group name "WX"`; an announcement (`BLNA`) is printed as a bulletin with identifier `A` (it is not told apart) | V |
| Object and item: name in quotes, `Killed Object` / `Killed Item` for the `_` form | V |
| Third party: one level is decoded as the inner packet (the inner message prints the inner `from` and `to`); the outer header is not printed | V |
| Status with a Maidenhead locator (4 or 6 characters, symbol, space) prints `Grid square = JJ00AA, N 00 01.2500, E 000 02.5000` (the centre of the square) | V |
| PHG 5132 prints `25 W height(HAAT)=20ft=6m 3dBi E`; PHG with power 0 prints nothing; `RNG0050` prints `range=50.0`; speed and course print as rounded `km/h (MPH)` and `course N` | V |
| The 17 existing frames: only the position, status and message lines decode; the information of the others starts with a letter that is no APRS data type (`h`, `x`, `s`, `a`) and prints `ERROR!!!  Unknown APRS Data Type Indicator` (except `m...`, which is `Morse Code Data`); an empty information field prints `unprintable 0x00`; a destination of `APRS` makes it add a warning "Use of APRS in the destination field is obsolete" | V |
| Invalid input is tolerated, not refused: a cut-off position prints the latitude and `Invalid Longitude`; a latitude with `X` prints `Invalid Latitude` and a warning; a message shorter than an addressee prints a warning and no fields | V |

### APRS compare policy (ANSWERED by the DV-Router lead; also the header of `aprs_decode.json` and `vectors/direwolf/README.md`)

Every record of `aprs_decode.json` has `expect`: the GENERATOR's exact values, written in
`dvtest/aprs_expect.py` from the numbers and the APRS 1.0.1 formats the string was built from (never from Dire Wolf's
output), and `expect_vs_decode_aprs`, the mismatches found under this policy (empty in all 93 records).

| Kind of value | Policy |
|---|---|
| strings and integers as encoded: source, destination, path (a `*` stays on its digipeater), data type indicator, symbol table and code, message addressee / text / id, ack and reject ids, status text, object and item name and live / killed flag, course and speed as encoded integers, altitude feet as encoded, PHG / RNG / DFS fields, weather fields | EXACT, against the generator (and against `decode_aprs` wherever it prints the field) |
| position latitude and longitude | equal to the generator's exact value, an integer in the encoded resolution (`{"int": n, "per_degree": d}` = n / d degrees: 6000 for plain and Mic-E hundredths of a minute, 60000 human DAO, 546000 base-91 DAO, 380926 / 190463 compressed); and within HALF of `decode_aprs`'s printed precision of it (it prints 0.0001 minute, so 0.00005 minute; compared as exact fractions of the printed text, `position_within_half_precision`) |
| timestamps (`dhm`, `hms`, `mdhm`, zone) and the ambiguity level | against the generator only: `decode_aprs` consumes the timestamp and does not print the ambiguity (the blanked digits come out as zeros); the unit tests check the generator value against the string |
| `dao-base91-bare` | the generator value (DAO applied) is the authority; `decode_aprs` does not apply a DAO that is the whole comment, recorded in `expect.decode_aprs` (`applies_dao: false`, its printed position without the DAO, its comment `!wfe!`); the generator value is NOT within half precision of what it prints (the DAO moves it by 0.0076 / 0.0075 minute) |

Where the generator and `decode_aprs` differ (all recorded in `expect.decode_aprs`, `test_documented_decode_aprs_differences_are_the_only_ones`):
`dao-base91-bare` (above); `msg-brace-in-text` (generator: text `text {with} braces inside`, id `77`; `decode_aprs`:
number `with} brace`, text `text `); `bln-announce-group` (generator: id `Z`, group `GRP`; `decode_aprs`: identifier `ZGRP`);
`wx-pos`, `wx-pos-negative`, `wx-pos-ts` (the speed of the `_ccc/sss` extension is 4 / 10 / 4 as encoded, `decode_aprs` reads it as knots and
prints 4.6 / 11.5 / 4.6 mph). Rounding rules used by `compare`: speed within 1 km/h, compressed altitude within 1 ft of
`1.002^code` (it prints 999 for 999.4), compressed range within 0.05 mile of `2 * 1.08^code`, barometer within 0.011 inHg of
`tenths of mbar / 10 * 0.02953`.

Other things to know when reading its output:

- The decoder prints rounded values: positions to 0.0001 minute, speed in whole km/h and mph,
  altitude in whole metres and feet.
- The comment is printed after Dire Wolf has removed what it parsed (altitude `/A=`, DAO, the PHG
  or course/speed extension, base-91 telemetry): a comparison of the comment must take the same
  fields out first. The raw comment is `info_text`.
- Time values, the ambiguity level, the Mic-E message code number (only its word), the raw
  message-capable flag (`!` versus `=`) and the third-party header are not in the output.

## Documented differences: Dire Wolf versus DV-Router

These are DIFFERENCES to know when a Dire Wolf vector meets DV-Router, not failures of either.
Dire Wolf's side is read from the vectors (V) or its source (S). DV-Router's side is what the
DV-Router lead STATED (L): nothing was run against DV-Router. The lead's statements are restated
as a small model in `dvtest/ax25.py` (`dvrouter_accepts_frame`, `DvrouterKiss`, `mask_cr_bits`,
`dvrouter_startup_params`) and compared with the committed Dire Wolf vectors by
`cases/unit/test_dvr_differences.py`. A restatement is not DV-Router: where the model needed a
fact the lead did not give, the model says so ("UNVERIFIED" in its docstring) and the
question is listed below.

### Frame size and type

| | Dire Wolf | DV-Router (L) | Consequence |
|---|---|---|---|
| Frame length | 15 to 2123 bytes (S ax25_pad.h; V: 14 refused, 15 taken) | 16 to 1100 bytes | **15 bytes** (two addresses + control, no PID): taken by Dire Wolf (V `frame-15-control-only`, kissutil prints `N0CALL-7>APRS:`), refused by DV-Router |
| KISS data limit | 2048 bytes FEND to FEND, i.e. frames up to 2045 bytes through the KISS port (V `info-2029` taken, `info-2030` refused; S `MAX_KISS_LEN`) | 1100 bytes, over: discard up to the next FEND | **1101 to 2123 bytes**: taken by Dire Wolf (V: a 2045 byte frame), refused by DV-Router. Frames of 2046 to 2123 bytes cannot even be sent to Dire Wolf over KISS (its buffer); a 2123 byte frame once killed the process (M, not a vector) |
| Frame type | the KISS port passes any frame of 15 to 2123 bytes to the radio; the control / PID is not looked at there (not tested with a non-UI control byte) | UI frames only: control 0x03, PID 0xF0 | non-UI frames: DV-Router refuses; Dire Wolf behaviour not shown |

So a vector longer than 1100 bytes or shorter than 16 can be used to check that DV-Router
REFUSES; do not use it as an accept case. `test_frame_windows` and `test_only_ui_frames_with_pid_f0`
pin the window and the type rule in the model.

### Command / response bits and SSID

| | Dire Wolf | DV-Router (L) |
|---|---|---|
| Transmit | C/R = 1 on BOTH destination and source SSID byte (`e0` / `ee`; SSID 9 source = `f2`, or `f3` when last) (V, 17 of 17) | C/R = 1 on the destination (`APZDVR-0` = `0xE0`), C/R = 0 on the source (`0x72`, `0x73` when it is the last address), reserved bits `0x60` set |
| Receive | | masks the SSID with `& 0x0f` and ignores C/R; bit 7 of a digipeater byte is the has-been-repeated flag (H) |

Comparisons between the two must **mask bit 7 of the destination and source SSID bytes**
(`ax25.mask_cr_bits`): the two encodings of the same text differ in exactly the source bit 7
(`from_tnc2(text, cr_dest=True, cr_src=False)` is the DV-Router form). Do NOT mask a
digipeater byte (its bit 7 is data). Tested with the 17 frames in `test_cr_bits_dire_wolf_versus_dv_router`.

### KISS receive decoder (DV-Router's, as stated) against Dire Wolf's (V)

DV-Router's side was ANSWERED by its lead from `crates/edge/ax25/src/kiss.rs` (the decoder's
`push()`, lines 36-80, and `MAX_FRAME = 1100`, line 4) and `crates/edge/ax25/src/adapter/tcp.rs`
(about lines 373-379 and 417-419: command and channel filtering). The model is
`ax25.DvrouterKiss` (`channels=(0,)` by default).

| Input | Dire Wolf | DV-Router (L, kiss.rs / tcp.rs) | Same? |
|---|---|---|---|
| bytes before the first FEND | `41 42 C0 ...`: logged "Rejected Noise", the frame is taken; a string with no FEND at all is all noise (V `garbage-before`, `no-leading-fend`) | dropped | same |
| bad escape `DB 41` | `KISS protocol error. Found 0x41 after FESC.`; both bytes dropped, the frame is kept (V `bad-escape`, `bad-escape-then-good`) | both bytes dropped, the frame is kept, counted (`frames_bad`) | same |
| FESC directly before FEND | the frame is closed, the FESC is dropped, the next frame is noise (V `fesc-before-fend`) | every FEND wins over a pending FESC: the frame is closed, NO bad escape is counted, the next frame starts | differs only in the next frame (below) |
| unescaped FEND inside a frame | closes it at that point (18 bytes taken) (V `fend-in-info`) | closes it | same for the first part |
| bytes after that closing FEND, before the next FEND | "Rejected Noise" (the closing FEND does not open a frame) (V `fend-in-info`, `truncated-then-frame`, `fesc-before-fend`) | **every FEND both closes the current frame and opens the next one**: the next byte IS the type byte of a new frame (ANSWERED, L) | **DIFFERENT, documented**: a frame without its closing FEND followed by a complete frame gives TWO frames at DV-Router, ONE at Dire Wolf (keep the case `truncated-then-frame`; `fesc-before-fend` is the same difference) |
| `C0 C0` | nothing (V `frame-empty`) | nothing (empty buffer) | same |
| command nibble 1 to 6, byte `FF` | Dire Wolf is the TNC: it APPLIES them (logs `KISS protocol set TXDELAY = 30...`, `Return` ignored) and does not copy them (V `param-*`, `cmd-return`) | ignored silently | different roles, not a conflict |
| command nibble 7 to 14 | `KISS Invalid command N`, nothing else (V `cmd-unknown-7` .. `cmd-unknown-14`) | ANSWERED: every command nibble other than 0 (1..15) is ignored silently | both pass nothing on |
| port nibble other than 0 | "Invalid transmit channel N from KISS client app.", frame not transmitted but taken by the KISS layer and copied (V `data-port1`, `data-port2`, `data-port15`) | ANSWERED: a data frame whose port nibble is not a configured channel is dropped silently | **DIFFERENT, documented** (3 cases) |
| data length limit | 2049 bytes FEND to FEND: "KISS frame should end with FEND.", refused (V `info-2030`; its copy holds stale memory and is not stored); 1101..2045 byte frames taken (V `limit-1101-*`, `info-2029`) | ANSWERED: the limit counts the UNESCAPED bytes after the type byte (the buffer holds the type byte and at most 1100 data bytes; `buffer.len() > MAX_FRAME` is checked before each push, escapes are resolved first and not counted); the 1101st byte starts a discard to the next FEND, which reports the frame as too long | **DIFFERENT, documented**: exactly 1100 is taken by both, 1101 only by Dire Wolf, with or without escapes inside (V `limit-1100-plain` / `limit-1100-escapes` taken, `limit-1101-plain` / `limit-1101-escapes` refused by the model) |

Comparison run over the 42 non-volatile cases of `kiss_tx.json` (`info-2030` is left out, its
copy holds stale memory): the model and Dire Wolf take the same AX.25 frames in 33 cases and
differ in exactly nine: `frame-15-control-only` (frame window), `info-2029`, `limit-1101-plain`,
`limit-1101-escapes` (length limit), `truncated-then-frame`, `fesc-before-fend` (the FEND opens the next frame),
`data-port1`, `data-port2`, `data-port15` (channel filter); all nine rows above
(`test_model_and_dire_wolf_take_the_same_frames_except_the_documented_ones`). The rules themselves
are also pinned directly in `test_limit_counts_unescaped_bytes_after_the_type_byte`,
`test_every_fend_closes_and_opens` and `test_command_nibbles_other_than_zero_are_ignored_and_other_ports_dropped`.
Counting detail: the model counts `overlong` at the closing FEND (like the edge's `frames_bad` for `Error::TooLong`),
not at the 1101st byte; a stream that never sends the FEND reports nothing.

### Start-up parameter frames

On every (re)connect DV-Router sends, per channel, in this order and never SetHardware or Return:

    C0 01 1E C0  C0 02 3F C0  C0 03 0A C0  C0 04 03 C0  C0 05 00 C0
    TXDELAY 30   P 63         SlotTime 10  TXtail 3     FullDuplex 0   (10 ms units: 300 ms, 100 ms, 30 ms)

(`ax25.dvrouter_startup_params(port)` builds exactly these bytes.) Dire Wolf logs each one as
`KISS protocol set <name> = <value>, chan <n>`, does not copy it to other clients and does not
answer (V; the vectors use TXtail 5, the other four values equal DV-Router's). Dire Wolf answers
only SetHardware, which DV-Router never sends.

### Airtime is an upper bound in DV-Router

DV-Router's AFSK airtime (`crates/lib/txsched/src/airtime.rs`, `ax25_airtime_ns` lines 56-71 and
`ceil_duration` lines 142-145, read by its lead) is deliberately, in **integer nanoseconds** (the unit is
1 ns, ANSWERED; the earlier "1 ms" was an assumption and is gone):

    frame_ns = ceil((len + 4) * 8 * 6 * 10^9 / (bps * 5))      one integer division rounded up
    airtime  = frame_ns + preamble_ns + TXDELAY_ns + TXtail_ns   each addition checked for u64 overflow
    errors   = zero bit rate; overflow of the u64 nanosecond result

`len + 4` = frame + 2 FCS bytes + 2 flags, `6/5` = the worst-case bit stuffing on every bit (FCS and flags
included), TXDELAY and TXtail from the KISS parameters, the preamble is extra to TXDELAY. The exact stuffed
bit counts of this item (`ax25.air_bits`, from Dire Wolf's audio length, 17 of 17) are therefore a **lower**
bound for it. `airtime.afsk_airtime_ns(len, bps, txdelay_ns, tail_ns, preamble_ns)` is that formula with
integers only (no float, no Fraction); `afsk_frame_ns` is the frame term; defaults 1200 bps, TXDELAY 300 ms
and TXtail 30 ms (DV-Router's start-up values). `afsk_upper_bound_ms` stays only as a display helper that rounds
UP to whole milliseconds and is never below the ns bound.

Vector table: `vectors/airtime/afsk_bound.json` (`python3 -m dvtest.airtime`), 25 rows
`(len, bps, TXDELAY, TXtail, preamble) -> expected_ns` or an error ("overflow", "zero-bit-rate"), integer
arithmetic only; examples: 47 bytes at 1200 bps = 408 000 000 ns frame, 738 000 000 ns with TXDELAY/TXtail; 47 bytes
at 7 bps = 69 942 857 143 ns (the ceil adds 0.143 ns); a frame length of `2^64 - 1` overflows. Unit tests
(`test_airtime.py`): the table equals a second path (`Fraction` ceil), the ceil property
`(ns - 1) * bps * 5 < (len + 4) * 8 * 6 * 10^9 <= ns * bps * 5` for 2000 random inputs, the bound is
within one nanosecond of the exact 6/5 time and at or above the exact stuffed time for the 17 frames, 300 random
frames and all-0xFF frames (the worst case, where it loses under 2%).

LoRa airtime anchors: the four `external` rows are ACCEPTED AS QUOTED by the DV-Router lead
(the two source pages were still not re-read here).

### Questions this section left open (all ANSWERED by the DV-Router lead)

- After an unescaped FEND, does the next byte start a new frame? YES (every FEND closes and opens); Dire Wolf drops
  it, kept as a documented difference (`truncated-then-frame`).
- What does the 1100 limit count? The UNESCAPED bytes after the type byte; escapes are not counted.
- Command nibble 7 to 14 and a port nibble other than 0? Both ignored / dropped silently (rows above).
- Unit of the airtime `ceil`? 1 ns (above).

## Not verified

- Nothing has run against DV-Router. No DV-Router adapter, edge or config was touched. DV-Router's rules in the
  sections above are the lead's statements, checked by us against the quoted lines of its read-only source tree
  (kiss.rs 4-80, tcp.rs 373-379 and 417-419, airtime.rs 56-71 and 142-145); no DV-Router binary was run.
- "KISS bytes in, AFSK audio out, decoded again" is closed by the modem loop above (the relay copy of a node's
  transmit audio decodes with `atest`); the KISS to TNC2 decode of the earlier vectors is still by `kissutil` and the log.
- The AFSK model is checked against one tool's output (its own length); no radio.
- Dire Wolf is one implementation: where it differs from the AX.25 / KISS specifications (the
  c/r bits, the silently dropped ninth digipeater, the recovery from bad escapes) the vectors
  say what Dire Wolf does, not what is right.
- The WA8LMF TNC test CD (a large real frame corpus) was not fetched; `decode_aprs` has run
  since the APRS stage (below); AGW (port 8000) is switched off in the config; Bluetooth/USB/serial quirks
  of real TNCs are out of reach.
- External airtime numbers: see above.

## Numbers the DV-Router side can use

| Need | Where |
|------|-------|
| TNC2 line to AX.25 bytes, 17 lines incl. digipeater `*`, SSID 0..15, 8 digipeaters, empty information, 340 bytes, binary information | `vectors/direwolf/rx_frames.json` `dw_ax25_hex` |
| The same as the KISS bytes a TNC sends (RX path of a `kiss-tcp` / `kiss-serial` adapter) | `dw_kiss_hex`; stream of all frames: concatenate them |
| What a TNC does with KISS from the host: parameter frames, port nibble, odd framing, size limits | `vectors/direwolf/kiss_tx.json` (`dw_messages`, `dw_unwrapped_hex`, `dw_copy_hex`, `kissutil_frames`) |
| TNC reply to SetHardware | `param-sethardware` `dw_client_rx_hex` |
| AFSK airtime bound in integer nanoseconds, with errors | `vectors/airtime/afsk_bound.json` (`expected_ns`, `frame_ns`, `error`) |
| Exact APRS values per case and the compare policy | `vectors/direwolf/aprs_decode.json` (`expect`, `expect_vs_decode_aprs`, `compare_policy`) |
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
2. **DP9 expected values**: ANSWERED by the DV-Router lead (DP9 = the "APRS core" of dv-lib-pkt)
   and delivered in the APRS stage below. ANSWERED too: the compare policy (section "APRS compare policy").
3. **Command/response bits**: ANSWERED: DV-Router emits C/R 1 on the destination and 0 on the
   source; comparisons mask bit 7 of both SSID bytes (section "Documented differences").
4. **Error behaviour to copy or not**: ANSWERED by the lead's statements (section "Documented
   differences": noise before the first FEND dropped, bad escape drops both bytes and keeps the
   frame, every FEND closes and opens, window 16..1100, limit 1100 unescaped bytes, command nibbles
   other than 0 ignored, other ports dropped); the four details that were open are ANSWERED.
5. **Parameter frames**: ANSWERED: it sends TXDELAY/P/SlotTime/TXtail/FullDuplex per channel on
   every (re)connect, in that order, never SetHardware or Return (section "Documented differences").
6. **Airtime**: ANSWERED: the four `external` anchors are accepted as quoted; the AFSK airtime in
   DV-Router is an upper bound (section "Documented differences"). The unit of its `ceil` is 1 ns (ANSWERED, section "Airtime").
7. **Scope split**: `dvt-lora-gw` (scripted TNC2 source with and without the `3C FF 01` prefix,
   CRLF, bad prefix, overlong lines) is the other half of plan row 2: done as a pure-Python
   generator with vectors, see `docs/lora-gw-cases.md`.
