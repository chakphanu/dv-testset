<!-- SPDX-License-Identifier: MIT -->
# DV-Router RIF5: the `mmdvm-serial` radio adapter against a fake MMDVM modem

Status: written offline against DV-Router `c017e491` (RIF5 complete in software: S2 `fd58a4e` the adapter, its config keys and the D-STAR framer, S3 `43483ca` DMR simplex, S4 `c017e491`
YSF; a damaged DMR frame refreshes the over timeout). **First rig run: image `dv-router:wip-7b0fca8` (DV-Router `7b0fca87`), 16 passed, 5 failed**; the triage and the fixes are in "First rig run"
below (the documents were re-read at `7b0fca87`; `docs/design/radio.md` and `status.md` did not change between `c017e491` and `7b0fca87`). What the unit tests prove: the envelope, the setup
frames, the fake modem answering a host that speaks the adapter's dialogue, and every judgement over synthetic host logs (`cases/unit/test_mmdvm_serial.py`, `cases/unit/test_rif5_lib.py`).

DV-Router's radio edge `dv-edge-radio` has a port kind `mmdvm-serial`. It talks the MMDVM modem host protocol over a byte port that is a serial device, a pty or a UDP socket pair. It is
off unless a `[[edges.radio.port]]` of that kind is configured (`config.md:687-690`). The tester plays the modem on the UDP byte port: it answers the dialogue, records every frame the
adapter writes (room to modem) and sends RF overs (modem to room). A protocol client of the room hears the second and keys the first.

| File | What |
|------|------|
| `dvtest/mmdvm_serial.py` | the envelope (short and long form), the two setup frames read from the document, `Rif5Modem` (the fake modem: `VirtualModem` plus a 17 byte status, a log of every host frame with its time, silence, a short status, the firmware's NAKs), `PinnedUdp` |
| `dvtest/rif5.py` | the judgements over a host frame log: open dialogue, status cadence, startup guard, an over per mode, a silent modem; the port row of a rendered config, the public / admin status rows, metric deltas |
| `cases/test_rif5_mmdvm_serial.py`, `cases/rif5_support.py` | the rig cases (marker `rig`) and their helpers |
| `rig/config/dv-router/rif5-mmdvm-udp.toml` | the config snippet (`DVT_SUT_CONFIG_EXTRA`) |
| `cases/unit/test_mmdvm_serial.py`, `cases/unit/test_rif5_lib.py` | offline tests (`-m unit`) |

Reused, not rewritten: the RF frame builders and decoders of `dvtest/mmdvm_rf.py` (D-STAR header with CRC, DMR LC bursts with BPTC / RS / Golay, YSF FICH / CSD / VCH), the key-up
player and UDP transport of `dvtest/mmdvm_modem.py` (the virtual modem of `docs/mmdvm-stack-cases.md`, which answers the real MMDVMHost and is a version 2 modem with a 12 byte status:
DV-Router needs 17, `status.rs:65`), the protocol clients of `dvtest/clients.py`, `dvtest/echo.py` for what a client heard, `dvtest/txguard.py` for the fence.

## The config shape

`rig/config/dv-router/rif5-mmdvm-udp.toml`, merged after the template (the template must not define `[edges.radio]` itself):

```toml
[edges.radio]
enable = true
callsign = "N0CALL"
rules = "other"
tx_enable = true
tx_ack = "I am the control operator of N0CALL and accept responsibility for its transmissions"

[[edges.radio.port]]
name = "rif5-modem"
kind = "mmdvm-serial"
physical = "rif5-modem"
tx = true
band = "70cm"
rx_freq = 434000000
tx_freq = 434000000
udp = "<TESTER_IP>:3340"          # the modem: the tester
udp_bind = "<OWN_IP>:3341"        # the edge: the SUT
modes = { dstar = "P", dmr = "Q", ysf = "R" }
hang = { rf_mode_secs = 1, net_mode_secs = 1 }
timers = { tot_secs = 180 }
id = { mode = "none", interval_secs = 600 }
dmr = { color_code = 1, tg = 4017 }
```

| Key | Why it is there | Source |
|-----|-----------------|--------|
| `[edges.radio] enable`, `callsign` | the edge is off by default; the callsign is required (3-8 letters and digits) and is the station of the RF identity | `config.md:689,701-702` |
| `tx_enable`, `tx_ack`, `physical`, `tx = true`, `band`, `rx_freq`, `tx_freq` | room-to-modem frames are written only on a transmitting port; every gate of the document is met: master switch, the exact acknowledgement sentence, a physical id, an amateur band, RF frequencies inside it | `config.md:703-705,711,715,728-729`; `radio.md:893` (10.1) |
| `rules = "other"`, `id = { mode = "none", interval_secs = 600 }`, `timers.tot_secs` | under `other` a transmitting row states its ID mode, interval and TOT itself; the ID is off so that no `SEND_CWID` follows an over (GAP below); DV-Router's own real-children test uses the same shape | `config.md:705,745,753-760`; `crates/router/tests/real_children/radio/mmdvm.rs` `config()` |
| `kind = "mmdvm-serial"`, exactly one of `device` or `udp`, `udp` + `udp_bind` | the byte port; `udp` is the PEER (the modem), `udp_bind` the local bind; a datagram from any other address is dropped | `config.md:722-725,796-800`; `io.rs:287` |
| `modes = { dstar = "P", dmr = "Q", ysf = "R" }` | one room per mode, the modules must differ; D-STAR, DMR simplex slot 2 and YSF V/D mode 2 are the built modes; the key `module` is forbidden on an MMDVM row | `config.md:712,727,815-817`; `radio.md:690-692` |
| `hang` 1 s / 1 s | the smallest RF and network mode hang (1-60), so that the cases can change mode after a short wait; the defaults are 10 s and 3 s | `config.md:861-864`; `radio.md:696-697` |
| `dmr = { color_code = 1, tg = 4017 }` | the colour code of every transmitted burst; the LC talk group of the transmitted headers (4017 is the talk group of room Q for the DMR clients of the rig, `dvtest.proto.dmr.module_tg("Q")`) | `config.md:843-845`; `radio.md:736` |
| rooms P, Q, R | D-STAR client module P, DMR client TG 4001+16, YSF client DG-ID 10+17; the template serves A-Z | `dvtest/clients.py` |

The tester binds `<TESTER_IP>:3340` and sends from there to `<OWN_IP>:3341`; the edge sends from `<OWN_IP>:3341` to `<TESTER_IP>:3340` and drops datagrams from any other source address.
Changing anything in `[edges.radio]` restarts `dv-edge-radio` (`config.md:696`).

## MMDVM protocol facts the modem and the judges use

All from DV-Router `docs/design/radio.md` at `c017e491` unless a Rust file is named.

| Fact | Source |
|------|--------|
| envelope: short `E0 LEN TYPE payload` (total 3-255), long `E0 00 L2 TYPE payload` (total 255 + L2); invalid LEN 1 and 2 and a zero L2 drop the `E0` and rescan; no checksum | `radio.md:586-592`; `frame.rs` |
| types: GET_VERSION 00, GET_STATUS 01, SET_CONFIG 02, SET_MODE 03, SET_FREQ 04, SEND_CWID 0A, ACK 70, NAK 7F; D-STAR HEADER 10, DATA 11, LOST 12, EOT 13; DMR slot 2 DATA 1A, LOST 1B; YSF DATA 20, LOST 21 | `radio.md:594-600` |
| version reply: v1 = byte 3 is 1 and a text from byte 4; v2 = byte 3 is 2, cap1 at 4 (D-STAR 01, DMR 02, YSF 04), cap2 at 5, CPU at 6, UDID at 7-22, text from 23; a missing requested mode is a capability mismatch | `radio.md:601-615`; `status.rs:119-150` |
| open: settle 2000 ms, GET_VERSION (at most six attempts 1800 ms apart), SET_FREQ then SET_CONFIG with a 1000 ms ACK deadline and one retry each, then GET_STATUS and Up; a NAK or exhausted deadline is Down; no frame transmits voice during open; a reopened link repeats the whole dialogue | `radio.md:611-615`; `core.rs:338,466,498,882-892` |
| polling: GET_STATUS every 250 ms; a valid status refreshes inactivity; 2000 ms without one after Up gives Down/inactive, closes the TX gate and requests link recovery; the shell reopens with 2 s doubling to 60 s | `radio.md:616-620`; `core.rs:910,924,285-286` |
| SET_CONFIG v2 is 40 B, both versions always set the simplex flag 80; enables D-STAR 01, DMR 02, YSF 04; levels `(percent*255+50)/100`; DC `offset+128`; TX delay ms/10; colour code at frame 29, DMR delay 30, oscillator 80 at 31 | `radio.md:621-629`; `config.rs:209-255` |
| SET_FREQ is 17 B `E0 11 04 00`, adjusted RX and TX Hz u32 LE, RF level, POCSAG Hz = adjusted TX | `radio.md:630-633`; `config.rs:257-266` |
| status: v2 is 20 B (payload 17), mode at 3, flags at 4, D-STAR space 6, DMR slot 2 space 8, YSF space 9; a short status never refreshes inactivity | `radio.md:650-657`; `status.rs:65,83-113` |
| native sizes: D-STAR header 41 B, data 12 B; DMR one control byte + 33 B; YSF one flag + 120 B; flow control: one frame per mode per 10 ms tick, space > 1 (D-STAR header > 4, costs 4), in flight at most D-STAR 10, DMR 3, YSF 2, host queue 16 per mode | `radio.md:665-675`; `core.rs:991-995` |
| mode: the circuit claims its mode with SET_MODE before PTT; `WouldBlock` while another mode holds the modem (`mode_busy`); a normal TX end starts `hang.net_mode_secs`, the first RX frame of a mode `hang.rf_mode_secs`; a multi-mode modem returns to idle (SET_MODE 0) after the TX buffer drained and the hang expired | `radio.md:696-700,936-946` |
| D-STAR RF: header flag `0x40` set, RPT1 = station padded to 7 + `dstar.rpt_module`, RPT2 = station + `G`; wrong RPT2 = `local_only`, bad CRC or RPT1 = `bad_header`; egress: flags clear, RPT1 = station + G, RPT2 = station + RF module, UR CQCQCQ, MY the talker, sync triple `55 2D 16` on frame 0 of each 21, one EOT | `radio.md:705-706`; `mode/dstar.rs:22-52,70-93` |
| DMR RF: slot 2 only; control `41`/`42` header / terminator, `20` burst A, `1`-`5` B-F; egress: eight voice LC headers, A-F, three terminators, the row's colour code, MS data sync `D5 D7 F7 7F D7 57` on header and terminator, MS voice sync `7F 7D 5D D5 7D FD` on A, the envelope always `E0 25 1A 00`; no START, ABORT, SHORTLC | `radio.md:708-749`; `mode/dmr.rs:6-10` |
| YSF RF: V/D mode 2 only; frame = flag + 120 B; egress `E0 7C 20 00` + 120 B, one header, one terminator, FICH Dev flag = `ysf.lo_dev` | `radio.md:753-791`; `mode/ysf.rs` |
| a header or terminator copy that fails its checks inside an open over is ignored and refreshes the native-over timeout (300 ms without a native frame ends an RF over) | `radio.md:725-728`; `hub_dmr.rs:100-103`, `hub.rs:641` |
| startup guard: a new observer transmits no earlier than G = 13 s after its first successful probe; the port shows `tx_state` `fenced`, `fence` `startup_guard`, `guard_ms`; no keying byte is written while the gate is closed | `radio.md:978,1008` (10.4 rule 4), `radio.md:700,1327`; `core.rs:230` (SET_MODE withheld without `rf_allowed`), `hub_dv.rs:7-25` |
| status rows: `state` configured / up / down (observer), `down_reason` gone / protocol / inactive / closed / caps_mismatch, `firmware`, `modem` (admin only) | `radio.md:1323-1335`; `status.md:319-352` |
| metrics: `dv_edge_radio_port_total{port,result=up\|down\|caps_mismatch}`, `dv_edge_radio_modem_total{result=open\|version_fail\|config_nak\|freq_nak\|inactive\|reopen\|drain_timeout}`, `dv_edge_radio_frames_total{dir,result}` (rx ok, short, mode_busy, unsupported, lost, bad_header, local_only; tx ok, dropped, nak), `dv_edge_radio_over_total`, `dv_edge_radio_ptt_total`, `dv_edge_radio_mode` | `radio.md:1253-1270`; `status.md:637-647`; `observe.rs:6-22`; `telemetry.rs:5-150` |

## The fake modem

`Rif5Modem` is a version 2 modem with the three DV capabilities. It answers GET_VERSION, GET_STATUS (17 byte payload: mode, flags, per-mode buffer space from a fill model, the TX flag while frames
play out, the carrier-detect flag while an RF over plays), ACKs SET_FREQ, SET_CONFIG and SET_MODE (NAK 4 for a mode that is not enabled), accepts native frames (NAK 2 for a disabled mode or while
another mode holds it, NAK 5 when its buffer has no room; a frame in idle selects its mode), takes SEND_CWID only from idle without an answer (NAK 5 otherwise) and NAKs a type it does not know with
reason 1. Every frame the host writes is kept with its time, control frames included. `mute` stops all answers (the frames are still logged), `status_mode` `short` answers every poll with a
10 byte status, `none` with nothing. It plays RF overs in real time (`keyup`, `play_air`) with the carrier-detect flag up. It is not a radio and not a firmware: the buffer is a fill model, there is
no RSSI, and lockout is a flag a case may set (none does). Datagrams from an address other than the SUT's `udp_bind` are dropped, as the edge drops them.

## Run it

Needs an image with RIF5 (DV-Router `c017e491` or later), the native launch, and the web profile for the cases that read the port state and restart the edge (`totp-required.toml` because the
admin step-up of the web fixtures starts from "first login = enrol", `docs/rig.md`; `data-ident.toml` is not needed: this unit has no data worker part).

```sh
R=rig/config/dv-router
XLXD_SUT_IMAGE=<image with RIF5> DVT_SUT_PROFILE=dvrouter DVT_SUT_NAME=dv-router DVT_SUT_LAUNCH=native DVT_SUT_GEN=compat1 \
DVT_SUT_CONFIG=<template path> DVT_TRANSCODER_FAKE=1 DVT_RIF5=on \
DVT_SUT_CONFIG_EXTRA=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/rif5-mmdvm-udp.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_rif5_mmdvm_serial.py
```

About 12 minutes (estimate, one rig at a time): 3 for the rig and the web enrolment, up to 1.5 minutes for the first dialogue (the edge retries a failed open with a doubling backoff, up to 60 s,
until the modem exists), the cases about 7 (G1 about 1 minute, O 20 s, TX 3 x 10 s, RX 3 x 12 s, RXD and RXR 2 minutes with the metrics waits, LONG 3 x 12 s, D3 10 s, D2 40 s, D1 60 s, M1 15 s).
Without `--profile web` G1, O3 and the port-state assertions skip or fall back to the log (the fence is then waited out by time); without the rig control channel (`DVT_RIG_CONTROL=1` is set by
`run.sh`) the metrics assertions skip. Without `DVT_RIF5=on`, or without the snippet in the rendered config, every case skips naming the missing piece. Notes: stdout and
`out/<run>/rif5.txt` (`RECORD`, `FINDING`).

## Cases

Identities: the modem's RF source is `N0CALL` (DMR 1023007), the protocol client that keys the room is `N0TEST` (DMR 1023008), the listener of the RF cases is `N0TEST`. Callsigns, ids and talk groups
are the synthetic ones of the rig.

| Case | Test | Source | Expectation |
|------|------|--------|-------------|
| RIF5-G1 (c) | `test_g1_nothing_is_written_to_the_modem_during_the_startup_guard` | `radio.md:611-615,700,978,1008`; `core.rs:230` | after a restart of `dv-edge-radio` (admin API) the public row shows `tx_state` `fenced`, `fence` `startup_guard`, `guard_ms` an int or null; a 0.6 s D-STAR over from the room inside the guard; from the restart to 4 s after the guard no voice, EOT, CW ID or DMR control frame reaches the modem (a SET_MODE or other control frame in the window is a FINDING); the fence ends no earlier than about 13 s after the restart |
| RIF5-O1 | `test_o1_open_dialogue` | `radio.md:611-633`; `config.rs` | GET_VERSION (at most 6, 1.8 s apart), SET_FREQ, SET_CONFIG, GET_STATUS in that order; SET_FREQ bytes equal the document's for the row; SET_CONFIG is 40 B, simplex flag, and the documented offsets (enables, TX delay, DC, levels, colour code, DMR delay, oscillator) carry the row's values, the others the zeros the document names; no voice or SET_MODE before the first status; the modem never had to NAK |
| RIF5-O2 | `test_o2_status_poll_every_250_ms` | `radio.md:616-617` | median GET_STATUS interval 0.20-0.32 s, none above 0.8 s |
| RIF5-O3 | `test_o3_port_row_in_the_status` | `radio.md:1318-1335` (12.3 `RadioPortInfo`); `status.md:319-352` | public row: kind, pattern `mmdvm-host`, state `up`, band, modes, `rx_freq_hz` / `tx_freq_hz` (observer fields); admin row: `firmware` is the modem's text, `modem.protocol` 2, `down_reason` null, `physical`; the public and the admin checks both run, the case fails at the end |
| RIF5-TX-dstar (a) | `test_tx_room_to_modem[dstar]` | `radio.md:700,705-706,936-946`; `mode/dstar.rs:70-93` | a DExtra client keys module P with 22 frames (0.44 s: what the flow control carries without a drop, see "First rig run"): SET_MODE 1 claims the mode (a SET_MODE 00 of the previous over's hang may precede it); one 41 B header (flags 0, RPT2 `N0CALL B`, RPT1 `N0CALL G`, UR `CQCQCQ`, MY the talker, CRC); 12 B data frames whose 9 B words are the words sent, in order, the sync triple on the frames whose position in the over is 0 mod 21; one empty EOT after the last; nothing of another mode, no CW ID |
| RIF5-TX-dmr (a) | `test_tx_room_to_modem[dmr]` | `radio.md:736-749`; `mode/dmr.rs:6-10` | a DMR client keys TG 4017 with 18 frames: SET_MODE 2 first; every frame `1A` with a 34 B payload and control byte 0; eight voice LC headers (group, src the talker, dst 4017, colour code 1, RS ok), burst A with the MS voice sync, headers / terminators with the MS data sync, six voice bursts (the words in order), three terminators; no `17 18 19 1C 1D 1E` |
| RIF5-TX-ysf (a) | `test_tx_room_to_modem[ysf]` | `radio.md:753-791`; `mode/ysf.rs` | a YSF client keys DG-ID 27 with 15 frames: SET_MODE 3 claims the mode; frames `20` with a 121 B payload, flag 0; one header naming the talker, three V/D mode 2 frames (FICH data type 2, Dev flag `ysf.lo_dev`), one terminator; the words in order, equal in every bit but the gain index b2 (YSF ingress +1.0 dB, egress -0.4 dB, DV-Router decisions D1 / RIF5 D3); words after the sent ones are a FINDING |
| RIF5-RX-dstar (a) | `test_rx_modem_to_room[dstar]` | `radio.md:705`; `hub_dv.rs:128-166` | the modem plays a 3 s RF over (header `RPT1 N0CALL B`, `RPT2 N0CALL G`); a DExtra client on module P hears one over, talker `N0CALL`, 150 words equal to the words sent, the end; after the over no voice frame goes back to the modem |
| RIF5-RX-dmr (a) | `test_rx_modem_to_room[dmr]` | `radio.md:710-735` | the modem plays a 3 s DMR over (LC header, A-F bursts with embedded LC, terminator, control bytes 41 / 20 / 1-5 / 42); a DMR client on TG 4017 hears one over from 1023007, the 150 words, the end; no echo to RF |
| RIF5-RX-ysf (a) | `test_rx_modem_to_room[ysf]` | `radio.md:753-777` | the modem plays a 3 s V/D mode 2 over (header, 30 voice frames, terminator); a YSF client on DG-ID 27 hears one over from `N0CALL`, the words (equal in every bit but b2, as above), the end; no echo to RF |
| RIF5-RXD | `test_rx_dmr_damaged_copies_refresh_the_over_timeout` | `radio.md:725-728`; `hub_dmr.rs:100-103`, `hub.rs:641` | ten valid voice bursts, then ten header copies with a wrong colour code 100 ms apart and no terminator: the over stays open until the last copy and ends within 3 s after it (not 0.3 s after the last valid burst); the voice words are intact; the first rig run shows a PRODUCT finding ("First rig run", RXD) |
| RIF5-RXR-local_only / bad_rpt1 / bad_crc | `test_rx_dstar_header_refused[...]` | `radio.md:705`; `mode/dstar.rs:23-52` | an RF over whose header has RPT2 not station + G (`local_only`), RPT1 not station + module or a bad CRC (`bad_header`): nothing reaches the room; `dv_edge_radio_frames_total{dir=rx,result=local_only\|bad_header}` grows (read for up to 30 s) |
| RIF5-LONG-dstar / dmr / ysf | `test_long_over_is_recorded[...]` | `radio.md:665-671`; `core.rs:991-995` | a 3 s over from the room in each mode: RECORD of the frames written against the frames sent and a FINDING for every difference; asserts only that frames were written; the judges run with `lossy`: words the host queue dropped are a FINDING with their count, the superframe sync and the DMR burst letters are judged at the position of the word sent; see "First rig run", FC1 |
| RIF5-D3 | `test_d3_a_gap_below_the_timeout_keeps_the_port_up` | `radio.md:616-620` | a modem silent for 1.0 s, then answering: no GET_VERSION in the next 7 s, the port stays `up` |
| RIF5-D2 | `test_d2_a_short_status_does_not_refresh_inactivity` | `radio.md:616-620,656` | every poll answered with a 10 byte status: the public state shows `down`; the host's last poll is 1.4-2.3 s after the last valid status, then a reopen dialogue; `frames_total{dir=rx,result=short}` grows |
| RIF5-D1 (b) | `test_d1_a_silent_modem_takes_the_port_down` | `radio.md:285,298,611-620,1332` | see "(b)" below |
| RIF5-M1 | `test_m1_counters_account_for_the_overs` | `radio.md:1253-1270` | over the run (counters since the edge restart of G1, or since the first over): `over_total{dir=rx,result=started\|ended}` at least the RF overs accepted, `dir=tx result=started` and `ptt_total{key\|unkey}` at least the room overs, `frames_total{dir=rx,result=ok}` moved; `frames_total{dir=tx,result=ok}` against ALL native frames the modem logged since the counters' baseline (not only those of the tallied overs) is a FINDING when different, with the frames by type in a RECORD |

### (b) the port goes Down when the modem goes silent: the mechanism

It is the status poll, not a transport timeout and not a byte count. Source: `radio.md:616-620` and `core.rs:910-924,285-286,886-892`.

1. After Up the host sends GET_STATUS every 250 ms (the same poll feeds the modem's own watchdog). A **valid** status (version 2: at least 17 payload bytes) refreshes the inactivity time. A short status
   or any other traffic does not (`radio.md:656`).
2. When 2000 ms pass without a valid status the core fails with `PortDown::Inactive`: the TX gate closes, the link is closed (the UDP socket is released), the port is `Down`, reason `inactive`. Seen from
   the modem: the polls go on for about 2 s after its last answer (about 7 unanswered polls) and then stop.
3. The shell reopens the link after a backoff of 2 s doubling to 60 s, then runs the whole dialogue: settle 2 s, GET_VERSION up to six times 1.8 s apart. If the modem stays silent the sixth miss is a
   second Down (`version_fail`, reason `protocol`) and the backoff doubles again. When the modem answers, the dialogue completes and the port is `Up` again.
4. What status and metrics show: public state `procs[].radio.ports[]` with `state` `down` and `tx_state` `rx-only` (`radio.md:298`); the admin row (`GET /api/v1/admin/clients`, operator) `down_reason`
   `inactive` (later `protocol`), `errors` and `reconnects`; `dv_edge_radio_modem_total{result=inactive}` and `dv_edge_radio_port_total{result=down}` grow (then `version_fail`, `reopen`, `open`
   and `port_total{result=up}` after the recovery). The metrics reach the hub every 10 s.

RIF5-D1 mutes the modem, reads the public state every 250 ms and the admin row for `down_reason`, waits for the first GET_VERSION and 12 s more (the six tries), judges the log (the last poll 1.4-2.3 s
after the last answer, at least five unanswered polls, the next dialogue at least about 3.5 s after the last poll, the tries counted), then lets the modem answer and waits for the port to be `up` again
(up to 120 s). The reopen delays walked (seconds between dialogues) are a RECORD.

### (c) nothing is written to the modem during the startup guard

The guard exists only after a start, so RIF5-G1 restarts `dv-edge-radio` through the admin API (`POST /api/v1/admin/children/dv-edge-radio/restart`, admin role, step-up; the same route RFSAFE1c uses for
`dv-edge-ax25`). Window: from the restart to 4 s after the public state shows the fence gone. Judged: **no** SEND_CWID, D-STAR HEADER / DATA / LOST / EOT, DMR frame or control, YSF frame in the
window. The dialogue frames (GET_VERSION, SET_FREQ, SET_CONFIG, GET_STATUS) are expected there; a SET_MODE or any other frame is a FINDING, because the document says "no keying bytes during a fence"
(`radio.md:700,1008`) and the code withholds SET_MODE while the gate is closed (`core.rs:230`) but SET_MODE does not key. The over inside the guard is the same check as DV-Router's own test
(`real_children/radio/mmdvm.rs`, "no RF-producing bytes during the startup guard" and "the guarded over never reaches RF later").

## First rig run (`dv-router:wip-7b0fca8`, DV-Router `7b0fca87`): 16 passed, 5 failed, triage

Run `out/20261112T211337Z` (stage 3m22 s). References are to the DV-Router tree at `7b0fca87` (`docs/design/radio.md` = radio.md, line numbers of that revision). The two failures that
are the case's own error are fixed (commit below); the PRODUCT items keep their assertion, so they fail again until DV-Router changes or says the assertion is wrong.

| # | Case | Class | Cause |
|---|------|-------|-------|
| 1 | O3 `rx_freq_hz` / `tx_freq_hz` | PRODUCT | observer fields of the document, absent from the public row |
| 2 | TX-dstar word 23, sync at frame 42 | HARNESS (sizing and position), with a PRODUCT note FC1 | the 1 s real-time over exceeds what the documented flow control carries; sync is stamped at the position of the word, not the frame index |
| 3 | TX-ysf first SET_MODE 00, word 0 | HARNESS | an idle SET_MODE of the previous over's hang may precede the claim; YSF words differ in the gain index b2 by design (D1) |
| 4 | RX-ysf word 0 | HARNESS | the same b2 steps |
| 5 | RXD ends 0.20 s after the last valid burst | PRODUCT (documents disagree) | the radio edge `SILENCE = 200 ms` ends the room over; the damaged copies refresh only the hub's 300 ms timer |
| F1 | O1 SET_CONFIG bytes 15, 16, 18, 19 = 128 | HARNESS (judge too strict) | levels; 128 = pct(50), the default level |
| F2 | M1 `frames_total{dir=tx,result=ok}` 290 against 284 | HARNESS (probable) | the tally missed frames; now compared with all frames since the baseline |
| F3 | LONG-dmr burst 36 sync | consequence of FC1 | a dropped burst; the letters were judged at the written index |

### 1. O3: the public row has no `rx_freq_hz` / `tx_freq_hz` (PRODUCT)

radio.md 12.3 (line 1330) lists `rx_freq_hz`, `tx_freq_hz` (int or null, scope obs, "Configured MMDVM receive/transmit Hz; null when not configured") and status.md "Radio port telemetry" (line
329) names them among the observer fields; `RadioPortInfo::OBSERVER_FIELDS` has 19 entries including both (`crates/lib/fabric/src/events_radio.rs:126-146`) and `crates/router/tests/real_children/radio/mmdvm.rs:278`
asserts `up["rx_freq_hz"] == 434000000`. The row served at `GET /api/v1/public/state` (`procs[].radio.ports[]`) carries 17 keys (`band cos duplex fence guard_ms kind mode modes module name pattern phys_busy ptt
rssi_dbm state tx tx_state`): the two keys are absent, not null, with `rx_freq = tx_freq = 434000000` configured. Not found from outside which layer drops them
(`crates/svc/web/tests/public_projection.rs:186-205` pins a 17 field expected row, with a fixture that has no frequencies). The case asserted the values and stopped before the admin row;
it now collects the public and the admin problems and fails at the end, so one run reports both. Assertion unchanged.

### 2. TX-dstar: word 23 differs, no sync triple at frame 42 (HARNESS; PRODUCT note FC1)

* **Sync (HARNESS).** radio.md 7.3 (line 706): "frame 0 of every superframe carries the sync triple `55 2D 16` whatever the source sent" and `mode/dstar.rs` `tx_data(unit, slow, position)` stamps it by the
  `position` of the unit in the over, not by the index of the frame written. Words had been dropped before frame 42 (below), so the frame at index 42 carried word 45, position 45: no sync belongs there. The judge
  now gives every written frame the position of the word it carries (`rif5.align`) and checks the sync at positions 0 mod 21. The same document gives a gap fill (a network gap of fewer than 10 frames is
  filled with null voice); no filled frame was seen (47 words written, none foreign).
* **Word 23 (flow control, FC1).** 47 of the 50 words arrived; the first gap is at word 23 (0.46 s into a real-time 50 frames per second over), the rest are intact and in order. radio.md 7.1 (lines 665-669): "at most
  one frame per mode per 10 ms tick, space > 1; D-STAR header needs > 4 and costs 4 ... Queued modem limits are D-STAR 10 ... Host queues hold 16 per mode, dropping the oldest on overflow ... awaiting rig tuning U1", and the
  status that refills the window comes every 250 ms. After the header the window is 6 frames until the first status arrives (up to 250 ms), then at most 10 per status (40 frames per second against 50 sent): the queue
  of 16 is full at the 23rd word. This is the documented policy and not a bug of the adapter against its own text, but it cannot carry a real-time over of more than about 0.45 s. The case was sized wrong:
  `TX_FRAMES["dstar"]` is 22 (0.44 s, two superframe boundaries), a drop there is a problem, with the count and the sent index of the gap in the message. Two more numbers from the same run:
  RIF5-LONG-dstar wrote 117 of 150 words (3 s, 22 percent lost), LONG-dmr 147 of 150 words (burst 36 lost), LONG-ysf all. The hub metrics snapshots `control/8-N.out` of the run (hub counters, read
  by the cases; times in `control/control.log`) give `frames_total{dir=tx,result=dropped}`: 0 -> 32 at the G1 restart (the 32 frames of the over inside the guard, refused by the fence), +4 during the three short TX overs, +35 during the three
  LONG overs (8-3 to 8-10), 71 at the end (the 39 of M1 is the baseline of 32 to 71, not LONG alone). No per-case timeline of the status replies and the space values was written in that run (it is, from the next run: "The timeline").
  FC1 is for the DV-Router team (U1 is on their list). **Only the D-STAR loss is measured** (117 of 150 words, 3 of 50 in the 1 s over). The ceilings of 40 / 12 / 8 frames per second that follow from "cap per 250 ms status" are
  a reading of the document, not a fact: DMR wrote 147 of 150 words (3 per 250 ms would be 12 bursts/s against 16.7 needed, far more loss) and YSF wrote 155 of 150, so a simple cap per status does not explain them. The timeline is there to find out.

### 3. TX-ysf: first SET_MODE is 00, word 0 differs (HARNESS)

* **SET_MODE.** radio.md 7.2 (line 697): "A multi-mode modem returns to idle only after its TX buffer has drained and that hang expires". The idle (`SET_MODE 00`) of the previous (DMR) over's `hang.net_mode_secs` was
  the first SET_MODE of the YSF window; the claim `03` followed. Order of overs: D-STAR, DMR, YSF; the same showed in RIF5-LONG-ysf after LONG-dmr and in no case that follows a D-STAR over. `_first_mode` now
  accepts any number of `SET_MODE 00` before the claim (their distance to the claim is a RECORD, `leading_idle_s`), refuses any other SET_MODE before it and an idle between the claim and the last native
  frame ("mode switching never interrupts a stream", line 697). Not understood: why `settle_idle` had already seen an idle before the window began; a late second idle would be the product's
  behaviour, and `leading_idle_s` in the next RECORD line shows how far before the claim it is.
* **Word 0.** decisions.md D1 (line 60): "for YSF only it also applies xlxd's AMBE+2 gain-index steps ... (+1.0 dB in, -0.4 dB out)"; RIF5 D3 (line 61): the radio edge links `ambe2` for YSF "the existing 49-bit to 72-bit layout
  and YSF gain-index steps only"; echo.md (lines 67-71): a YSF over comes back with every AMBE+2 bit unchanged "except the gain index b2 (0..19 up three steps ...)". dvtest knows it (`dvtest/proto/ysf.py`
  header, `clients.wire_frames`, `cases/test_clients2.py`): the case compared the words bit for bit. `rif5.compare_ysf_words` fails on any bit but b2, and records the b2 steps; a b2 that is not
  `crossaudio` P10 then M04 is a FINDING.
* **Extra words.** voice_written 20 of 15 sent: one more V/D mode 2 frame (5 words) after the three. Not asserted (extra words were never a problem); now a FINDING `extra_words`.
  Probable cause: the YSF ending rule of echo.md (line 75, "the trailing zero end unit follows the ordinary YSF ending rule") padded to a packet.

### 4. RX-ysf: first difference at word 0, 150 heard of 150 (HARNESS)

The same b2 steps (RF ingress +1.0 dB at `wire::payload_units`, YSF egress -0.4 dB): none missing, none extra, only the gain index differs. Compared with `compare_ysf_words`.

### 5. RXD: the over ended 0.20 s after the last valid burst, the damaged copies went on for 0.96 s (PRODUCT: radio.md 7.3 against radio.md 4.4)

radio.md 7.3 (lines 725-728): a header or terminator copy that fails its checks inside an open over "is ignored ... and refreshes the native-over timeout: the firmware still tracks the carrier"; 7.1
(line 662): "RX carrier also spans native frames until EOT/LOST or 300 ms without a frame (a delivered frame whose coding fails still counts as a frame)". The hub does that
(`hub_dmr.rs` `rf_activity` sets `dv_last_ns`, `hub.rs:641` ends at 300 ms). The room over ended 0.2039 s after the last valid burst, 0.1 s before even the unrefreshed 300 ms: that is the radio edge's own
`SILENCE = 200 ms` (radio.md 4.4 line 326: "ten units: a port that stops delivering PCM while its COS is on is ended by the core"; `crates/edge/radio/src/protocol.rs:69`; `crates/lib/edge/src/core.rs:4734`, the core
finishes a stream whose `last_rx_ns` is 200 ms old). A damaged copy produces no unit and no datagram to the core, so it refreshes the hub but not the core: the room-visible over ends 200 ms after the last
valid burst whatever the modem delivers. The two statements cannot both have an effect; either 7.3 should say the refresh is only the hub's and the over ends at 200 ms, or the core's silence must be fed
by the damaged copy. The assertion is kept (a listener hears the over end 0.20 s after the last valid burst; wanted: after the last copy), the message names SILENCE, and a RECORD line gives the number.

### Second rig run (testset `a1e7952`, `dv-router:wip-7b0fca8`), triage

Default fake `out/20261113T032535Z` (4 failed: O3, TX-dstar, TX-ysf, RXD; the last two as PRODUCT items), `DVT_RIF5FW=on` `out/20261113T032918Z` (the same plus RX-ysf). Timelines in `<run>/rif5/`.

| # | Case | Class | Cause |
|---|------|-------|-------|
| 1 | TX-dstar "voice word 22 differs", 22 sent, 23 written | HARNESS (a regression of the alignment judge) | the 23rd data frame is a trailing word AFTER the last sent word (the end unit the room renders before the EOT); `_word_check` took it for a changed word. Trailing words are now `extra_words` with `extra_first_hex` |
| 2 | TX-ysf "no SET_MODE 03 among ['00']" | UNSURE, window fixed | the only SET_MODE in the window is the 00 at the end of the hang (1.7 s); the fake already reported mode 03 at its first status (0.088 s), before the first YSF write (0.108 s), so a claim or a frame was written before the window opened. The window now starts before the client links. If the claim is still absent the next run says so: then it is a PRODUCT item |
| 3a | FW, LONG-ysf b2 steps -24..+4 | HARNESS | 140 of 150 words written (the old image drops frames); the words were paired by index, so every pair after a drop was wrong. `compare_ysf_words` now aligns on everything but b2 (a dropped frame is a FINDING in LONG, a problem in the short overs) |
| 3b | FW, LONG-ysf 30 frames over 7.16 s | not a fault | the model: 7 underruns, each key-up again with a 600 ms preamble (the old host feeds 2 YSF frames per 250 ms status against 100 ms per frame) |
| 3c | FW only, RX-ysf 145 of 150 words | UNSURE (product counter) | `frames_total{dir=rx,result=mode_busy}` is 2 in the FW run, 0 in the default run (`control/8-3.out`): the host refused two RF frames (probably the header and the first voice frame, whose 5 words are the ones missing, the over then opening by late entry at FN 1; inferred from the counts, not seen) as mode_busy, i.e. it believed another mode or a TX held the modem. Nothing in the fake's RX path differs (RX is the parent's); the FW fake's TX flag does. M1 now writes a FINDING with the `mode_busy` growth, and the RX cases write timelines (status mode and flags before and during the over) to find the holder |
| 4 | FW, LONG-dstar dropped 46 (default 32) | explained | the model keeps the modem silent for the 600 ms preamble (100 ms tx delay + 500 ms); at 50 frames per second 30 words arrive meanwhile and the 16-frame queue of the old image overflows earlier. The timeline has the 12 `underrun` events (every status cycle, about 260 ms); a RECORD line per case now counts them |

The YSF extra words (RIF5k): `extra_first_hex` is `202222200000000000` in all five and it is exactly the all-zero 9 byte unit passed through the YSF egress step only (-0.4 dB, `crossaudio` M04), without the +1.0 dB ingress step: five all-zero frames,
i.e. the padding of a partial last packet whose one unit is the end unit (media-format.md: "A partial last packet is padded with all-zero frames", "the end unit is not rendered" for the network YSF egress; the radio egress renders it as a fourth V/D mode 2 frame, 100 ms).

### What the run left behind (for the flow-control analysis)

Written to `out/<run>/`: `rif5.txt` and `pytest.log` (the same RECORD / FINDING lines: frames written, words sent and written, native frames and span per LONG case), `junit.xml` (per case time), `control/8-N.out` with
`control/control.log` (16 hub metrics snapshots, cumulative per-port counters, no per-case label: the snapshot times map to the cases by `junit.xml` order). **Not written in that run:** the fake's log of the host frames
with their times (`Rif5Modem.frames`, in memory), the status replies it sent and the space values in them, and the host queue depth. From the next run the TX cases write it: "The timeline".

The fake (`dvtest/mmdvm_modem.py:70-71, 337-343`; our own choice, no firmware line cited): capacity 10 frames D-STAR, 10 per DMR slot, 8 YSF; play-out per host frame D-STAR header 80 ms, data and EOT 20 ms, DMR burst 60 ms, YSF
frame 100 ms. Space is computed from the clock at every read: `busy_until` grows by the play-out time of each frame written (`_enqueue`, line 335) and `space = capacity - round((busy_until - now) / air time)` (line 343), so it
is freed continuously at the air rate, and the status reply (`Rif5Modem.status_payload`, `dvtest/mmdvm_serial.py:336-342`) reports the value at that instant: payload `mode, flags, 0, D-STAR, DMR1, DMR2, YSF` = frame offsets 3, 4, 5, 6,
7, 8, 9 (radio.md 7.1). The status interval is the host's (250 ms, `rif5.POLL_S`); the fake answers each GET_STATUS at once. The in-flight limits 10 / 3 / 2 and the prefill are the host's (radio.md lines 665-669, `core.rs:990-996`).

### The timeline (`out/<run>/rif5/timeline-<case>.jsonl`)

Written by the short TX overs and the three LONG cases: `timeline-TX-dstar.jsonl`, `-TX-dmr`, `-TX-ysf`, `timeline-LONG-dstar.jsonl`, `-LONG-dmr`, `-LONG-ysf` (the notes file `rif5.txt` has a
`RECORD ... timeline:` line per case). One JSON object per line, in time order, `t` in seconds since the case start (the tester's monotonic clock, taken just before the talker keys the room; no secrets: frame types,
lengths and space bytes). Built by `rif5.timeline_lines` from the fake's event log (`Rif5Modem.events`, `events_between`), written by `cases/rif5_support.py write_timeline`; at most 2500 lines (the rest is cut with a
`truncated` line), a 3 s over is a few hundred.

| `ev` | fields | meaning |
|------|--------|---------|
| `frame` | `t`, `dir` (`h2m`), `event` (`write`), `type` (`DSTAR_DATA`, `SET_MODE`, ...), `mode` (`dstar` / `dmr` / `ysf` or null), `len` (payload bytes), `arg` (SET_MODE only: the mode byte in hex) | a frame the host wrote to the modem; GET_STATUS is left out (the status lines cover it) |
| `status` | `t`, `dir` (`m2h`), `event` (`status`), `type` (`GET_STATUS`), `len` (20), `mode`, `flags`, `tx` (the TX flag), `space` = `{dstar, dmr1, dmr2, ysf}`, `space_triple` = `[dstar, dmr2, ysf]`, `fifo_bytes` (DVT_RIF5FW only) | a GET_STATUS reply the fake sent, the free space per mode as reported (payload bytes 3, 4, 5, 6 = frame offsets 6, 7, 8, 9); the fake computes it at that instant from the play-out (`mmdvm_modem.py:337-343`) |
| `fw` | `t`, `event` (`write`, `pop`, `nak`, `preamble_start`, `preamble_end`, `tx_off`, `underrun`) and per event `type`, `fifo_bytes`, `duration_ms`, `air_ms`, `reason`, `dry_s`, `end_frame` | the firmware-like fake (DVT_RIF5FW) only: see "The firmware-like fake"; a past event carries the time it happened |
| `sent` | `t`, `event` (`sent`), `index`, `est` (true) | a word the room side sent. The time is ESTIMATED: the words spread evenly over the talk call (start to end without its 0.5 s tail); the case does not see the client's packets |
| `truncated` | `lines` | lines cut beyond the bound |
| `summary` | `case`, `facts` (the judge's numbers: `voice_sent`, `voice_written`, `voice_dropped`, `leading_idle_s`, ...), `problems`, `findings`, `frames_total` | last line. The `frames_total` ok / dropped deltas are NOT read by these cases (the hub metrics lag up to 10 s); the cumulative counters of the run are in `control/8-N.out` with `control/control.log` |

The `dir` / `type` / `len` / `event` / `fifo_bytes` / `tx` names and the `event` values are those DV-Router proposes (design-276 1.8); our own fields stay.

Reading it: the frames written against the `sent` lines and the `status` lines show how many frames the host put on the byte link between two replies, and the space it saw when it did. Mind that D-STAR, DMR and YSF
numbers are not alike: only the D-STAR loss has been measured so far.

### Findings that were not failures

* **F1, SET_CONFIG bytes 15, 16, 18, 19 are 128 (HARNESS).** radio.md 7.1 (lines 622-627): "12-16 mode levels, 17 reserved, 18/19 POCSAG/FM levels" and "levels use integer (percent * 255 + 50) / 100": these are the P25, NXDN, POCSAG
  and FM levels and 128 is pct(50), the default level of the other mode levels. The document gives no percent for them (a small gap in the text). `judge_open` records the values and writes a FINDING only for a value that
  is not pct(50).
* **F2, `frames_total{dir=tx,result=ok}` 290 against 284 logged (UNSURE).** The tally covered exactly the six room overs, and the metrics snapshots show the excess in both phases: `ok` 76 after the three short TX overs
  (72 native frames logged) and 290 after the LONG overs (212 more logged, 214 counted), so a tally error does not explain it. M1 now counts every native frame the fake logged since the counters' baseline and records the frames by type
  (SET_MODE claims and idles are written to the byte link too); a remaining difference stays a FINDING for DV-Router (is `ok` only native frames?).
* **F3, LONG-dmr "voice burst 36 is voice" (consequence of FC1).** One burst (words 108 to 110, sent burst 36) was dropped; the next written burst is sent burst 37 (a B) and carries no sync, correctly. The judge assumed
  written index equals sent index. It now takes the letter from the burst of the words sent.

Re-run (the same command as the run, only the RIF5 file; add `-k` to narrow): see "Run it". Expected after the fixes: 19 passed, O3 and RXD failing as PRODUCT items until DV-Router answers.

## The firmware-like fake and the cases for the fix image (RIF5j)

DV-Router analysed FC1, O3 and RXD (`design-276`, unit RIF5j) and accepted all three as its own. Its fix (a later image): a 50 ms status poll while keyed or draining, in-flight caps 10 / 4 / 3 counted by poll
sequence, a host queue of 1.7 s airtime that drops to a D-STAR sync or DMR burst A, a `tx_underrun` result of `dv_edge_radio_modem_total`, both frequencies in the public row, damaged DMR / YSF frames inside an open
over becoming codec silence, radio-edge `SILENCE` 400 ms. Two new switches, both off by default: **without them every case, every verdict and the fake are exactly what the first run used** (the current image gives the
same results as before).

| Switch | Effect |
|--------|--------|
| `DVT_RIF5FW=on` | the fake modem is firmware-like (below). Optional `DVT_RIF5FW_PROFILE=mmdvm` (default) or `hs`, `DVT_RIF5FW_TXDELAY_MS=0..500` (default: the row's SET_CONFIG value, 100 ms in the snippet) |
| `DVT_RIF5J=on` | the image has RIF5j: RIF5-J-LONG runs, RIF5-O3 and RIF5-RXD get the assertions of the fix (they keep the current ones, which the fix image also meets) |

Both are passed to the tester by `rig/compose.yml` (`cases/unit/test_tester_env.py` guards it) and documented in `dvtest/target.py`.

### The firmware-like fake (`dvtest/mmdvm_fw.py`, DVT_RIF5FW=on)

Under the switch `Rif5Modem` is replaced by `FwRif5Modem`; the dialogue, NAKs for mode and enables, the RF key-up and the silence faults are the parent's. The TX path is `FwModel`, pure and in virtual time (`cases/unit/test_mmdvm_fw.py`).
The numbers are design-276 1.2 and 1.9 (their file:line are in the module docstring; `M/` = MMDVM `1aa1034`, `HS/` = MMDVM_HS `bf66faf`). They come from source, not from a measurement: DV-Router's RIF13 checks H3 (idle space per board) and H4
(preamble length) confirm them on boards.

| What | MMDVM (default) | MMDVM_HS (`DVT_RIF5FW_PROFILE=hs`) |
|------|-----------------|------------------------------------|
| TX FIFO, D-STAR / DMR slot 2 / YSF | 370 / 370 / 4000 bytes | 1000 / 875 / 1500 bytes |
| stored per frame | D-STAR header 42, data 13, EOT 1; DMR 33; YSF 120 bytes (a frame that does not fit: NAK 5) | same |
| space in the status = free bytes / unit | 13 / 35 / 120: idle 28 / 10 / 33; DMR slot 1 byte fixed 10 | idle 76 / 25 / 12; slot 1 fixed 25 |
| a frame leaves the FIFO | when its play-out starts: one per air time (D-STAR header 141.7 ms, data 20, EOT 30; DMR 60; YSF 100) | same |
| TX-delay preamble (TX off, a frame at the head, FIFO not consumed) | D-STAR header 500 ms + tx delay; DMR and YSF 500 ms + tx delay; at most 1 s; D-STAR data after TX went off: none | D-STAR 250 ms + tx delay, DMR and YSF as MMDVM |
| TX flag in the status | on from the first write (preamble) until the FIFO runs dry | same |
| space of a mode SET_CONFIG did not enable | 0 (review-277) | same |
| underrun | PTT off when the FIFO is dry; a frame of the same mode within 1 s of that, when the last frame played was not an EOT / terminator, is an `underrun` event (`dry_s`); the next frame keys again, with a new preamble for DMR, YSF and a D-STAR header | same |

The 1 s underrun window and the end-frame test (D-STAR EOT, DMR data burst, YSF FICH FI 2) are our rule, not a firmware's. Events (`write`, `pop`, `nak`, `preamble_start`, `preamble_end`, `tx_off`, `underrun`) go to the
timeline as `fw` lines with the FIFO bytes. Not modelled: RSSI, the sample buffer, DMR fill bursts, lockout, a host that stops polling.

Against the CURRENT image this fake is a probe, not a verdict: the host there caps 10 / 3 / 2 frames per 250 ms status, so the modem runs dry and the timeline shows `underrun` events and `tx_off` lines inside the over. The existing
cases do not assert them (they run with the verdicts of the first run, plus whatever the preamble does to their timing).

### Cases for the fix image (`DVT_RIF5J=on`, with `DVT_RIF5FW=on` for the full check)

What each asserts (design-276 1.8 "Rig (dv-testset), what RIF5 should assert after the fix"):

| Case | Asserts |
|------|---------|
| RIF5-J-LONG (`test_j_long_overs_are_lossless`, about 1 minute) | a 3 s over from the room in D-STAR, DMR, YSF, judged strictly (no `lossy`): 150 of 150 voice words written in order and byte exact (YSF but for the b2 gain steps), the D-STAR sync at positions 0 mod 21, DMR burst A at 0 mod 6, no NAK; GET_STATUS while keyed: median 0.065 s at most (the fix polls every 50 ms), longest 0.12 s; with `DVT_RIF5FW=on` no `underrun` / `nak` event of the fake (without it a RECORD says the underrun is not modelled); over the three overs `frames_total{dir=tx,result=dropped}` grew by 0, `modem_total{result=tx_underrun}` is exported and grew by 0 (hub counters read 11 s before and 12 s after; needs the rig control channel, else a RECORD). Timelines `timeline-J-LONG-<mode>.jsonl` |
| RIF5-O3 | (the existing case, which already asserts both frequencies) plus: the public row has exactly the 19 observer fields |
| RIF5-RXD | under `DVT_RIF5J=on` REPLACED (the copies of this case are a clean codeword of another colour code, which the fix no longer treats as damage: review-277 F5, radio.md 7.3 at `a8692074`): nothing follows the 30 sent words, and the over ends 0.2 to 0.5 s after the last VALID burst (the 300 ms native-over timeout, no refresh). Without the switch the old assertions (the copies refresh the timeout) run unchanged |
| RIF5-J-RXD-garbled-10 / -14 (`test_j_rx_dmr_garbled_copies_are_filled_and_bounded`) | ten valid voice bursts, then 10 (14) header copies with the right colour code and a destroyed LC (`rif5.dmr_garbled_copy`), 100 ms apart, no terminator. 10: exactly 30 equal silence words after the 30 sent, the over ends 0.2 to 0.5 s after the last copy. 14: the 13th fill ends the over as LOST, so exactly 39 silence words and the end within 0.2 s of the 13th copy (the 14th finds no open over) |
| RIF5-J-RXH-dmr (`test_j_rx_dmr_header_train_keeps_one_room_over`) | a DMR RF over with 8 voice LC headers (what MMDVMHost sends in simplex, first voice at 480 ms): one room over, all 150 words and no more, ended, no echo to RF (review-277 F3: the edge core SILENCE of 400 ms used to end it) |
| RIF5-J-RXD-ysf-bad4 / -bad6_bound5 (`test_j_rx_ysf_bad_frames_are_filled_and_bounded`) | a V/D mode 2 over with 4 (6) frames whose DCH CRC fails (`rif5.ysf_dch_garbled`) after 6 good ones. 4: 20 equal silence words between the first 30 and the last 30 words sent (the b2 gain steps allowed in the good ones), the bad frames' words are not heard. 6: the 5th fill ends the over as LOST: 25 silence words and the end within 0.2 s of the 5th bad frame (no terminator is sent) |

Inside RIF5-J-LONG, besides what is above: for D-STAR the release after the EOT (`rif5.release_view`, review-277 F1): the status poll stays at 50 ms for at least two polls after the EOT write, and slows down to 250 ms only after the fake's TX went off (the last frame played; `DVT_RIF5FW=on` for that second condition).

Not built: a forced-underrun case (a network gap of 1.5 s mid-over, `tx_underrun` exactly 1: the room clients of the rig cannot pause inside one talk; design-276 TL6 covers it in the core), `modem-side over lasts the network over + E` (E unknown until RIF13 H1), the YSF extra frame (RIF5k), the firmware's "status in the same read batch as the keying frame reports TX off" (review-277 F2; UDP datagrams have no batch, so the fake cannot show the EOT race, only the release rule above), the YSF header-train limit. Also not run against hardware: the fill of damaged
frames needs a real firmware that delivers them (real firmware filters a wrong colour code itself, `M/DMRDMORX.cpp:132-180`).

Exact names the cases rely on, checked in `a8692074` (`dv-router:wip-eeb44b4`): the counter is `dv_edge_radio_modem_total{port,result="tx_underrun"}` (`crates/lib/radioport/src/mmdvm/observe.rs` `MODEM_RESULTS`, status.md line 681,
radio.md line 1374); the public port row is the 19 fields of `crates/lib/hub/src/public_core.rs:263` (`name kind module pattern mode duplex tx band state tx_state cos ptt phys_busy fence guard_ms rssi_dbm rx_freq_hz tx_freq_hz` plus the array `modes`).

**Expected verdict on `wip-eeb44b4` with `DVT_RIF5J=on DVT_RIF5FW=on`:** all 27 cases pass (the 21 of the first run, RIF5-J-LONG and the five new ones), with these remaining risks, none of them a known defect: the thresholds of the keyed
poll cadence (median 0.065 s, longest 0.12 s are our rig margins on DV-Router's 50 ms / 60 ms); the number of words the YSF cases hear around the silence (an extra end unit would fail `bad4`'s "30 words after the bad frames"); the
RIF5k items stay FINDINGs and do not fail (YSF extra frame, `frames_total ok` against the frames logged, b2 map). RIF5-RXD runs its REPLACED assertions (foreign colour: no fill, no refresh). RIF5-O3 passes with 19 fields. With only
`DVT_RIF5J=on` (the fill-model fake) the same, minus the underrun / NAK / TX-off checks of the fake. On the OLD image the same switches give: O3 and RXD (replaced) fail, RIF5-J-LONG fails (drops, 250 ms poll, no tx_underrun series), the
three fill cases and the header train fail: the new cases are red there, as intended.

Run on the fix image (the command of "Run it", with the two switches):

```sh
DVT_RIF5J=on DVT_RIF5FW=on DVT_RIF5=on DVT_SUT_NAME=dv-router DVT_SUT_CONFIG_EXTRA=$R/web.toml:$R/admin.toml:$R/totp-required.toml:$R/rif5-mmdvm-udp.toml \
  scripts/run.sh -m rig --capture --profile web -- cases/test_rif5_mmdvm_serial.py -rA
```

With `DVT_RIF5FW_PROFILE=hs` the same with the HS board's sizes. The other environment (`XLXD_SUT_IMAGE`, `DVT_SUT_LAUNCH=native`, ...) is that of "Run it".

## GAPs, things not driven black-box, and predictions

* **Serial and pty byte ports.** The modem supports a pty (`dvtest/mmdvm_modem.py` `PtyTransport`) but a pty cannot cross containers and the rig has no shared device between the tester and the SUT
  (`docs/mmdvm-stack-cases.md`). Only the UDP byte port is driven. The exclusive-open and by-id rules of `device` rows are not reachable.
* **The startup guard needs the web profile.** Without `--profile web` and an admin account (`totp-required.toml`) G1 skips: the control channel's `kill-proc` does not list `dv-edge-radio`
  (`dvtest/control.py`), and a modem Down / Up does not re-arm the fence (the fence is fabric state, not link state). No new control action was invented. A start that happens before the tester exists
  cannot be observed from the first dialogue because the guard began at the start of the node.
* **CW ID (`SEND_CWID`).** The snippet switches the ID off (`rules = "other"`, `id.mode = "none"`). The modem and the judge know the frame (NAK 5 outside idle, 50 ms per bit, 89 bits for N0CALL, TOT at least
  5 s: `config.md:786-795`), but no case drives it; it would be a second snippet with `rules = "us"` and the default ID.
* **Version 1 / `model = "mmdvm-hs"`.** The fake is a version 2 modem; the 26 B SET_CONFIG v1 layout, the v1 status and the HS name check are not covered. `duplex = true` is refused by the validator
  (`radio.md:704`) and not tested here.
* **Not driven:** the `mode_busy` results in both directions (a TX request while an RF over holds the modem, an RF frame of another mode while a TX holds it), the lockout flag and `use_cos_as_lockout`,
  the overflow flags, a NAK while Up (a reset modem NAKs data with reason 2 and the port goes Down `protocol`), a version or capability mismatch (`caps_mismatch`), the 2500 ms drain timeout, DMR / YSF
  late entry and FEC-damaged frames beyond RIF5-RXD, the data side (`dstar-slowdata`, `dmr-emb-lc`, `ysf-dch`), TOT, hang at the modem.
* **Flow control (a prediction, recorded by RIF5-LONG).** The host keeps at most 10 D-STAR, 3 DMR and 2 YSF frames in flight, and refreshes its view of the modem's space only from the status, every 250 ms
  (`core.rs:991-995,924`, `radio.md:665-671`). In 250 ms a modem plays 12.5 D-STAR frames (20 ms), 4.2 DMR bursts (60 ms) or 2.5 YSF frames (100 ms), so the host can keep a modem busy for 10 / 3 / 2 frames
  per poll at most: a real-time over arrives at 50 / 16.7 / 10 frames per second against about 40 / 12 / 8 that can be sent, and the host queue (16 per mode, oldest dropped) overflows after a few seconds.
  The cases of the TX direction therefore use short overs (1 s D-STAR, one DMR superframe, 3 YSF frames, the sizes of DV-Router's own tests) and RIF5-LONG records what a 3 s over does. This was
  seen in the first rig run ("First rig run", FC1): the D-STAR words are dropped from the 23rd on in a 1 s over, earlier than predicted, because the header costs 4 and the first refill comes up to 250 ms after PTT; the
  capacities of a real board are "awaiting rig tuning U1", `radio.md:669`.
* **Backoff reset.** The document says "2 s doubling to 60 s" (`radio.md:619`); in `core.rs` the backoff is doubled at every failure (`core.rs:285-286`) and is initialised once (`core.rs:167`): no reset after a
  successful open was found. RIF5-D1 records the dialogue delays; a modem that failed earlier in the run (an edge started long before the tester) can show a longer first reopen than 2 s + 2 s.
* **Metrics need the control channel and 10 s.** The hub gets the radio families every 10 s. The counter assertions poll for up to 30 s; without `DVT_RIG_CONTROL=1` they skip.
* **Frame counter semantics.** What `frames_total{dir=rx,result=ok}` counts exactly (whether the EOT is counted) and the equality of `tx ok` with the native frames the modem logged are not stated
  exactly by the document; RIF5-M1 asserts only lower bounds and records the rest as FINDING.
* **Not run on a rig.** Timing bounds (the 0.20-0.32 s median poll, the 1.4-2.3 s for the Down) have a margin for a loaded rig but are first guesses.

## FINDING lines the cases can write (never assertions)

SET_MODE or another control frame inside the guard window (G1); the public row carrying an admin-only field (O3);
NAK 5 from the fake's buffer model (TX); extra voice words after the words the modem sent (RX); the words missing in a 3 s over, with the counts (LONG); the number of GET_VERSION tries of a silent reopen
when it is not six (D1); `frames_total{dir=rx,result=short}` not counting short statuses (D2); `frames_total{dir=tx,result=ok}` against the frames logged (M1) against all native frames since the baseline; a fence that was already over when the
over inside the guard ended (G1); words the host queue dropped in a long over, with their count (LONG); a YSF b2 that is not the documented +1.0 / -0.4 dB map and words
after the sent ones (TX-ysf, RX-ysf); SET_CONFIG level bytes 15, 16, 18, 19 that are not pct(50) (O1).
