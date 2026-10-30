<!-- SPDX-License-Identifier: MIT -->
# DExtra / D-STAR: obligations of the reflector side

What a reflector serving **DExtra** on UDP 30001 owes a DExtra client: a
repeater gateway (ircDDBGateway, DStarGateway, QnetGateway), a hotspot
behind one of them, or another reflector that links with an `XRF`
callsign. Each item is numbered `P-DXT-NN`; the conformance cases are
planned in `docs/testlist.md` section K2.

Every row says where the obligation comes from, in separate columns: the
spec text (with version and section), what the other reflectors and the
gateways do (project at a commit, behaviour in our own words, no code
copied), and what xlxd-ref showed on the rig. "measured" in the last
column cites the existing case or capture that showed it; "not measured"
means no case has looked yet (code reading goes in the Implementations
column, never in the observed one).

**Level**: MUST / SHOULD only where a spec says so; `convention (impl)`
where only implementations back it, naming the ones that agree;
`disputed` where they differ, naming who does what. `convention (XLX
family)` marks rules that xlxd and URFd share.

There is no DExtra specification. The link, keepalive and unlink packets
are defined only by code (dxrfd, then ircDDBGateway). The `DSVT` framing
of header and voice comes from the JARL gateway-to-gateway packet (JARL
5.2, with the trunk header of 5.1); the radio header, voice frame,
superframe and last frame come from the JARL air interface (JARL 4.1).
JARL marks its chapter 5 as specific to Japan and published for alignment
with the Japanese text; every DExtra implementation uses its layout all
the same, so the Level column cites it as the spec for the DSVT bytes.

## Sources

Specs and published notes:

- **[JARL7]** JARL, "Digitalization Technology Standard for Amateur Radio
  - D-STAR (Digital Smart Technologies for Amateur Radio)", Ver. 7.0
  (2025-05), English translation, 106 pp.
  https://www.jarl.org/English/7_dstar/STD7_0_E.pdf (archived copy
  http://web.archive.org/web/20250718141108/https://www.jarl.org/English/7_dstar/STD7_0_E.pdf).
  Used: 4.1.1 radio header (items a-k: flags, RPT2 destination, RPT1
  departure, UR, MY, MY suffix, P_FCS = CRC-CCITT x^16+x^12+x^5+1; no
  initial value, bit order or receiver action stated), 4.1.2 voice packet
  (b: 72-bit voice + 24-bit data frame per 20 ms; c/d: resync in every 21st
  frame; e: data frame passed as given; g/h: last frame carries the end
  pattern), 5.1 item c "Voice data" p.57-58 (trunk header; Call ID: one
  random id per PTT, the same in every packet of the call; management
  byte: bits 7-6 00 voice / 01 last frame / 10 radio header, bit 5 header
  error, bits 4-0 sequence 0..20; recommendation: a radio header before
  every 21-frame block, a receiver that already has the header discards
  the later ones), 5.2 b p.60 (GW <-> GW voice over UDP: `DSVT` 4 B, flag
  2 B (0001 radio header, 0010 data), reserved 2 B, trunk header 7 B, then
  the 41-byte radio header or 9 B voice + 3 B data), 6.1-6.4 (slow data
  mini-headers: 0x3x GPS, 0x40-0x43 message, 0x5x header resend, 0x66
  filler; header resend in 6.4), Ap1 (scrambler; it yields the per-frame
  XOR 70 4F 93). Earlier English edition: Ver. 6.0a (2019-01)
  https://www.jarl.org/English/7_dstar/STD6_0_E.pdf.
- **[SHOGEN]** JARL, "D-STAR System" (old short English translation,
  roughly Ver. 4.3, 12 pp): 2.1.1 radio header, 2.1.2 voice packet and end
  pattern. https://www.jarl.com/d-star/shogen.pdf
- **[ON1ARF]** K. Bonne (ON1ARF), "Format of files and UDP-streams used on
  D-STAR", 2011-06-06: byte map of the 56 B DSVT header (offsets 0-3
  `DSVT`, 4 0x10, 8 0x20, 9-11 band/ids, 12-13 stream id, 14 0x80, 15-55
  radio header) and the 27 B voice packet (14 counter 0..20, +0x40 on the
  last). Written for the gateway stream, not DExtra. File "formats of
  files and UDP-streams used on D-STAR.pdf" in
  https://www.qsl.net/kb9mwr/projects/dv/dstar/
- **[SLOW]** J. Naylor (G4KLX), "The Format of D-Star Slow Data" v0.2,
  2009-03-10. https://www.qsl.net/kb9mwr/projects/dv/dstar/Slow%20Data.pdf
- Not found: any DExtra protocol text (ircDDB wiki, OpenDV, dxrfd docs);
  no ETSI, ARIB or ITU document covers D-STAR.

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26, v2.6.1) | reference reflector (xlxd-ref); `src/cdextraprotocol.cpp` |
| URFd | https://github.com/n7tae/urfd @ `bd0c114e` (2025-12-29) | XLX-family reflector; `reflector/DExtraProtocol.cpp` |
| dxrfd | https://github.com/w1bsb/dxrfd @ `a4df63e9` (2019-03-03, 3.08a) | the original XRF reflector |
| dxrfd311 | https://github.com/K2IE/dxrfd-3.11 @ `05202d21` (2019-10-14) | last dxrfd release; only its keepalive change is cited |
| IG | https://github.com/g4klx/ircDDBGateway @ `f5ab9f7c` (2026-02-18) | gateway: DExtra client of reflectors, and server for incoming links and dongles |
| DSG | https://github.com/F4FXL/DStarGateway @ `7b75de60` (2026-10-28) | IG fork; same DExtra behaviour unless noted |
| QN | https://github.com/n7tae/QnetGateway @ `39468e99` (2026-07-06) | gateway (QnetLink): DExtra client and peer |
| MH / DSR | https://github.com/g4klx/MMDVMHost @ `9384d683`; https://github.com/g4klx/DStarRepeater @ `6f2550b9` | cited for the header CRC check only |

## How the cases judge

`cases/proto/test_dextra.py` probes xlxd-ref and the system under test in
parallel; the Judged column of testlist K2 says how:

- **hb**: a spec MUST or SHOULD (Level column); asserted on both sides.
- **diff**: a convention the implementations agree on, or an XLX-family
  convention; sut == ref.
- **dev**: disputed between implementations, a convention without
  agreement, or an xlxd deviation from a spec. Under `DVT_SUT_PROFILE=xlxd`
  sut == ref (xlxd's behaviour is recorded as data); under another profile
  the profile's spec entry (`dvtest/specs/<profile>.py`, `"dextra"` dict
  `{"P-DXT-NN": {observed key: value}}`) decides, and the case skips with
  "spec pending" while there is none. A non-xlxd system is never forced to
  copy an xlxd choice on a disputed item.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

The tester sends spaces as the header CRC (B12: not checked by any
reflector); a case that needs a real CRC computes it. Egress counter,
sync position and last frame are also checked passively by dvcheck
(testlist H2).

Conventions: offsets are into the UDP payload. "RPT2" is the destination
field at offset 18, "RPT1" the departure field at offset 26 (QnetGateway
and dxrfd name them the other way round). The 2-byte stream id is opaque:
JARL puts the upper byte first, xlxd and URFd use host order, IG reads it
high byte first and writes it low byte first. "Silently ignored" means no
reply and no state change.

## 1. Link, keepalive, unlink

Packets: link 11 B = callsign (8) + client module + reflector module +
0x00; unlink 11 B = the same with a space as the reflector module; keepalive
9 B = callsign (8) + 0x00; ACK / NAK 14 B = the first 10 link bytes + `ACK`
or `NAK` + 0x00.

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-01 | A link to a served module from a valid callsign is answered with the 14 B ACK | convention (impl, agreed) | not stated (no DExtra spec) | agreed form: xlxd, URFd, dxrfd, QN echo bytes 0-9 + `ACK` 0x00; IG (incoming) the same fields rebuilt, sent twice; IG/QN (client) match `ACK` at 10-12 and their own callsign (IG bytes 0-8, QN byte 8) | measured: 14 B ACK to all 25 links of the clients.md capture |
| P-DXT-02 | ACK latency | disputed | not stated | xlxd delays a first ACK by a random 0-2 s (one pending ACK per IP); URFd, IG, QN, dxrfd answer at once; IG (client) retries after 1 s, backing off to 60 s; QN sends its link 5 times once and waits 50 s | measured: every ACK inside the tester's 2.5 s wait (latency not recorded) |
| P-DXT-03 | A link to a module letter the reflector does not serve gets NAK and no link | convention (impl, agreed) | not stated | xlxd NAK (after the same random delay); URFd NAK; dxrfd NAK (modules A-E, client band A-D); IG (incoming) NAK when no repeater of that module; QN accepts A-C only | not measured (no NAK seen in captures) |
| P-DXT-04 | A link with an invalid callsign or a non-letter reflector module never links; reply | disputed | not stated | xlxd NAK for the first 3 tries per IP, then ignores that IP for 5, 30, 60 min; URFd silent (unparsed); dxrfd checks a callsign pattern, NAK; IG does not check the remote callsign; QN requires the callsign in its gateway list | not measured |
| P-DXT-05 | A link whose last byte is 11 (UP4DAR "revision 1") is ACKed like any link; its unlink is answered with `DISCONNECTED` (12 B) | convention (XLX family) | not stated (xlxd comment names UP4DAR) | xlxd, URFd; IG and QN ignore byte 10 (normal link, no unlink reply) | not measured; the tester note in `dvtest/proto/dextra.py` says it is not ACKed, the code reads it as revision 1 and ACKs: re-measure |
| P-DXT-06 | A repeated link (same callsign, module, address) while linked: ACK again, one client | disputed | not stated | xlxd ACKs at once and refreshes the client; URFd ACKs, no duplicate; QN re-ACKs; IG (incoming) ignores it silently (no re-ACK) | not measured |
| P-DXT-07 | The reflector sends each client a 9 B keepalive (its callsign + 0x00) at a fixed period | form: convention (impl, agreed); period: disputed | not stated (JARL 5.2 c dummy packets every ~10 s are a different packet) | xlxd every 10 s per client, the first one jittered up to 5 s, callsign `XRF` + number; URFd every 3 s; IG every 10 s; QN about every 1 s; dxrfd about every 1 s (3.11: 10 B form, 9 B no longer accepted) | measured: 9 B `XRF999` + 2 spaces + 0x00 (clients.md capture), DExtra name, not `XLX999` |
| P-DXT-08 | A 9 B keepalive with the client's own callsign refreshes its link; an echo of the reflector's keepalive does not | disputed | not stated | xlxd, URFd: callsign and address must match a client; IG: first 7 chars of the peer callsign, from its ip:port, or voice of the active stream; QN: any 9 B (or DSVT) from the ip:port; dxrfd: any 9/11 B or DSVT from the IP | measured (development note in `dvtest/proto/dextra.py`, no case): echoing `XRF999` gave "keepalive timeout" about 40 s after linking; the own-callsign answer keeps the link |
| P-DXT-09 | A silent client is dropped after the keepalive timeout; what it is sent | drop: convention (impl, agreed); timeout and notice: disputed | not stated | xlxd 60 s (checked at its 10 s keepalive), sends 11 B (10 spaces + 0x00); URFd 30 s, sends its callsign + module + space + 0x00; IG 60 s, QN about 50 s, dxrfd about 60 s, all send nothing | not measured |
| P-DXT-10 | A dropped client gets no further voice | convention (impl, agreed) | not stated | all remove the client | not measured |
| P-DXT-11 | A client is not dropped while it is talking (stream owner) | convention (impl, agreed) | not stated | xlxd, URFd refresh the stream owner at each keepalive round; IG and dxrfd count voice as life; QN counts DSVT as life | not measured |
| P-DXT-12 | Unlink (11 B, byte 9 space) removes the client; no reply (except the revision 1/2 forms) | convention (impl, agreed) | not stated | xlxd, URFd: no reply (revision 1 `DISCONNECTED`, revision 2 echo); IG, QN, dxrfd no reply; IG and QN send their unlink 2 and 5 times | measured: no reply to 25 unlinks (clients.md capture) |
| P-DXT-13 | After the unlink no further voice reaches the client | removal: convention (impl, agreed); tail: judged by B5 | not stated | xlxd refreshes its per-module client list once a second, so up to 1 s of a running stream still goes out; URFd, IG stop at once | measured: tail <= 1.0 s (test_xlxd_clients::test_leave_mid_stream_tail[dstar], C7b) |
| P-DXT-14 | An unlink from the client that is talking ends its stream and frees the module | disputed | not stated | xlxd does not remove a client that owns a stream: the unlink is lost and the stream runs on; URFd likewise refuses to remove a stream owner; IG (local unlink) sends its repeater an end frame; QN clears the slot and lets its 2 s timer end the stream | not measured (compare DMR RPTCL, B15) |

## 2. Malformed and unknown packets

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-15 | Unknown sizes (3 B, 10 B link, 1400 B) and DSVT of a wrong size (55, 57, 26, 28 B) get no reply; the reflector keeps serving | convention (impl, agreed) | not stated | xlxd logs and drops (an 11 B packet that fails the link check is P-DXT-04); URFd dumps and drops; IG classifies non-DSVT by size (9 poll, 11/14 link) and drops the rest; QN, dxrfd drop | not measured |
| P-DXT-16 | DSVT header and voice from an address that is not linked are not relayed | convention (impl, agreed) | not stated | xlxd, URFd open no stream without a client; IG needs the link's ip:port; QN a linked ip:port; dxrfd a known IP | not measured |
| P-DXT-17 | DSVT prefix checks: header with byte 8 not 0x20, byte 14 not 0x80, other bytes 9-11 | disputed | JARL7 5.2 b (flag, reserved, trunk header: type, three ids, Call ID, management) | xlxd, URFd: size, `DSVT`, byte 4 and byte 8 checked, bytes 9-11 and 14 ignored; IG: only `DSVT` and byte 14 (0x80 header, else voice), size unchecked; QN: size, `DSVT`, bytes 4 and 8 | not measured |

## 3. DSVT header (56 B)

Offsets: 0-3 `DSVT`, 4 0x10, 5-7 0, 8 0x20, 9-11 trunk ids (xlxd and
URFd send 00 01 02), 12-13 stream id, 14 0x80, 15-17 flags, 18 RPT2, 26
RPT1, 34 UR, 42 MY, 50 MY suffix, 54-55 P_FCS (low byte first in IG and
xlxd).

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-18 | A 56 B header laid out as above opens a stream on a linked client's module | MUST (JARL7 layout) | JARL7 5.2 b p.60 (DSVT, flag 0001, trunk header), 5.1 c p.58 (management 10 = radio header), 4.1.1 (radio header); ON1ARF byte map | agreed: xlxd, URFd, IG, QN, dxrfd | measured: the tester's header (spaces as CRC) opens streams in every rig case |
| P-DXT-19 | The ingress header CRC (P_FCS) is not required to be valid | field: MUST (JARL7 4.1.1 k); receiver action: not stated | JARL7 4.1.1 k (CRC-CCITT; no init, bit order or bad-CRC action given) | reflectors and gateways agree: xlxd, URFd, QN, dxrfd never check; IG skips the check ("DExtra checksums are unreliable"); MH does not check; DSR drops a bad CRC unless it is FF FF | measured: spaces as CRC accepted in every rig case (B12) |
| P-DXT-20 | The egress header carries a valid P_FCS for the fields it actually sends | MUST (JARL7 4.1.1 k) | JARL7 4.1.1 k | xlxd recomputes after its RPT2 rewrite; IG and QN compute it; URFd keeps the ingress value though it rewrote RPT2 (stale); dxrfd has no CRC code | not measured |
| P-DXT-21 | A header whose MY, RPT1 or RPT2 holds characters outside A-Z, 0-9, space (or starts with three digits) is dropped | disputed | JARL7 4.1.1 (callsign fields: ASCII, space padded) | xlxd, URFd drop it (RPT1, RPT2, MY); dxrfd checks MY against its callsign pattern; IG only applies its white/black list to MY; QN does not check | not measured |
| P-DXT-22 | The flag bytes (flag 1-3) reach egress unchanged | disputed | JARL7 4.1.1 (flag 1: data, repeater, interruption, control, emergency bits) | xlxd, URFd pass them; IG writes 00 00 00; QN clears 0x40 in some flag 1 values | not measured |
| P-DXT-23 | A header with stream id 0 opens no stream | disputed; judged by B8 | JARL7 5.1 c (Call ID random; zero not mentioned) | xlxd, URFd refuse it; IG uses 0 as "no stream", so an idle link takes it | not measured (test_floor::test_admission_sid_zero runs it, outcome not recorded) |
| P-DXT-24 | A header repeated with the same stream id while the stream runs is discarded: no second stream, no restart | SHOULD (JARL7 5.1 c recommendation) | JARL7 5.1 c p.58 (a receiver that already has the header discards later ones) | agreed: xlxd, URFd only refresh the stream; IG ignores a header while a stream is active | not measured |
| P-DXT-25 | A header whose stream id is already open on another module is dropped (loop guard) | convention (XLX family); judged by B8 | not stated | xlxd, URFd refuse a stream id that is open anywhere; IG, QN have no such check | measured: same id in a second room dropped (testlist B8 notes) |
| P-DXT-26 | A header or voice whose id equals a module's last outbound id, from an address other than that stream's owner, is dropped | disputed | not stated | xlxd only (it remembers the last outbound id per module, also after the stream ended); URFd, IG, QN, dxrfd have no such filter | measured (development note in `dvtest/clients.py` fresh_stream_id, no case): a reused id is dropped |

## 4. Voice, superframe, last frame (27 B)

Offsets: 0-13 as the header but byte 4 0x20, 14 counter (0..20, 0x40 on
the last frame), 15-23 AMBE (9 B), 24-26 slow data (3 B).

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-27 | The 9 AMBE bytes are relayed unchanged on same-mode egress | convention (impl, agreed) | JARL7 4.1.2 b (72-bit voice per 20 ms) | agreed: xlxd, URFd, IG, QN, dxrfd | measured: byte exact (test_xlxd_matrix::test_passthrough_exact, C1) |
| P-DXT-28 | The 3 slow-data bytes (text, header resend, GPS, filler; scrambled) are relayed unchanged on same-mode egress | convention (impl, agreed) | JARL7 4.1.2 e (data frame passed as given), 6.1-6.4, Ap1; SLOW | agreed: xlxd (it writes its own slow data only on cross-mode egress), URFd, IG, QN | not measured |
| P-DXT-29 | Egress counter: 0..20 in order, wrapping, header byte 0x80, last frame 0x40 | MUST (JARL7 5.1 c) | JARL7 5.1 c p.58 (management byte) | agreed: xlxd and URFd write their counter mod 21; IG, QN pass the byte | measured passively: dvcheck H2 found nothing in the clients.md capture |
| P-DXT-30 | Egress frame 0 of every 21 carries the sync pattern 55 2D 16, no other frame does | MUST (JARL7 4.1.2 c/d) | JARL7 4.1.2 c/d (resync every 21 frames; byte form per ON1ARF) | xlxd passes it on same-mode and writes it on cross-mode; URFd, IG, QN pass the ingress bytes and neither check nor restore it | measured passively: no sync findings by dvcheck H2 (tester sends sync at frame 0) |
| P-DXT-31 | Voice with a counter outside 0..20 (or bit 0x80 set) | disputed | JARL7 5.1 c (0..20) | xlxd and URFd keep the received byte as the frame number and write it mod 21 on egress (no range check); IG masks it with 0x1F | not measured |
| P-DXT-32 | A last frame (0x40) ends the stream and reaches the listeners | MUST (JARL7 4.1.2 g, 5.1 c) | JARL7 4.1.2 g/h (last frame), 5.1 c (frame type 01) | agreed | measured: every rig stream ends this way; listeners get it (dvcheck H2) |
| P-DXT-33 | Payload of the egress last frame | disputed | JARL7 4.1.2 h (RF end pattern: sync 32 bits + 000100110101111 + 0, i.e. 55 55 55 55 C8 7A) | xlxd writes 55 C8 7A + six zeros as AMBE and 25 1A C6 as slow data; IG and URFd pass the ingress payload (IG writes 55 55 55 55 C8 7A + zeros only on a local unlink); QN passes it | not measured |
| P-DXT-34 | Voice after the last frame with the same stream id is not relayed | convention (impl, agreed) | not stated | xlxd, URFd: the stream is closed, a new header is needed; IG one id per stream; dvcheck rule `after_last` | not measured |

## 5. Routing per module and RPT fields

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-35 | A header with RPT2 = reflector + the linked module reaches every other client on that module once | convention (impl, agreed) | JARL7 4.2.1 (module letter in the 8th character) | agreed | measured: C1/C3 rig cases (test_xlxd_matrix) |
| P-DXT-36 | A header whose RPT2 module differs from the client's linked module | disputed | not stated | xlxd routes by the RPT2 module (revision 0 clients); URFd by the linked module; dxrfd by the linked band (byte 11 picks the source band when one repeater links several); IG drops a header whose RPT fields do not name the link | not measured |
| P-DXT-37 | Egress RPT fields: RPT2 = reflector name + module; RPT1, UR, MY pass | RPT2: convention (XLX family); the rest: disputed | JARL7 4.1.1 (RPT2 destination, RPT1 departure) | xlxd writes `XRF` + number + module (its DExtra name, not `XLX`) into RPT2, keeps RPT1, UR, MY, fills an empty MY suffix with `DXTR`; URFd its DExtra name + module; IG (as client) accepts a header only when RPT2 or RPT1 equals the linked reflector name + module and sets UR to CQCQCQ (whether a gateway linked as `XLX999` accepts `XRF999`: not checked) | not measured |
| P-DXT-38 | Module change: unlink, link to another module; then heard on the new module only | convention (impl, agreed) | not stated | agreed | measured: test_xlxd_clients::test_module_change_follows_talker[dstar] (C7) |
| P-DXT-39 | A link to a second module from the same address without unlink | disputed | not stated | xlxd adds a second client (hears both; an unlink removes only the first found); URFd adds a client per module; IG keeps one entry per module (5 max); dxrfd one user per IP with a module table; QN one peer per local module | not measured |

## 6. Stream lifetime

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-40 | A stream whose last frame is lost ends after a silence timeout; the module is then free | convention (impl, agreed); timeout and end marker judged by B3 | not stated | xlxd, URFd 1.6 s; IG 2 s, sends nothing; QN 2 s at the gateway, which sends its radio an end frame | measured: module free within 2.0 s; no last frame to D-STAR listeners (test_xlxd_clients::test_stream_timeout_frees_module, C8) |
| P-DXT-41 | A second header on a busy module is not mixed into the running stream | convention (impl, agreed); late-entry policy judged by B1/B2 | not stated | xlxd, URFd one stream per module (xlxd keeps a late header and promotes it when the first ends); IG one stream per link | measured: first stream whole; late talker relayed after the first ends, without a header (testlist B notes) |
| P-DXT-42 | Voice whose stream id had no header is not relayed (no late entry) | disputed | JARL7 4.1.2 c (resync allows reception from mid-call), 6.4 (header resend in slow data) | xlxd, URFd, IG drop it; QN passes it to its own radio side | not measured |
| P-DXT-43 | The talker never receives its own stream back | convention (impl, agreed) | not stated | xlxd skips the stream owner's address; URFd skips the stream owner; IG skips the link it receives from; QN the source ip:port | not measured for a normal stream; a promoted late entrant was echoed (71 frames) in some runs (testlist B16 notes) |
| P-DXT-44 | A client that links while a stream runs gets that stream's header before its voice | disputed | JARL7 5.1 c recommendation (header before every 21-frame block) | xlxd sends the module's cached header 5 times right after the ACK; URFd sends nothing (the client hears nothing until the next stream); dxrfd not checked | not measured |
| P-DXT-45 | Header copies on egress | disputed | JARL7 5.1 c recommendation (a header before every 21-frame block) | xlxd, URFd, QN send 5 copies at stream start and none later; IG sends 5 to a reflector; none repeats mid-stream on DExtra | measured: 13 headers in, 90 out in the clients.md capture (5 per listener) |
| P-DXT-46 | One stream id per transmission on egress; equal to the ingress id | constant: MUST (JARL7 5.1 c); equal to ingress: judged by B10 | JARL7 5.1 c (same Call ID for every packet of one PTT) | xlxd, URFd, QN pass the id; IG byte-swaps it (constant, but not the ingress bytes) | measured: kept (test_floor::test_sid_preserved[dstar], B10); constant (dvcheck H2) |

## 7. Several clients

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-47 | Two callsigns from one IP (different ports) are two clients; each gets the module's traffic once | convention (impl: xlxd, URFd, IG; dxrfd differs) | not stated | xlxd, URFd key by callsign + ip:port + module; IG by ip:port + callsigns; dxrfd keys users by IP only | measured: N0CALL and N0TEST link from one host in every rig case and both hear their module (C1) |
| P-DXT-48 | The same callsign from two ports | disputed | not stated | xlxd, URFd two clients; IG two entries (ip:port); dxrfd one user (IP) | not measured |
| P-DXT-49 | A keepalive from one client never refreshes another client | convention (impl, agreed) | not stated | xlxd, URFd match callsign + address; IG per link; QN per ip:port; dxrfd per IP (so two clients on one IP share a timer) | not measured |

## 8. Reflector peers (`XRF` callsign)

xlxd also opens DExtra links to other reflectors from its interlink list
(link with its `XRF` name, keepalive 10 s, timeout 60 s); that needs rig
configuration and is out of scope here. In scope: how a reflector answers
a peer that links in.

| # | Obligation | Level | Spec / paper (document, version, section; URL) | Implementations (project @ version or commit: behaviour) | xlxd-ref observed |
|---|---|---|---|---|---|
| P-DXT-50 | A link from an `XRF` callsign: reply form | disputed | not stated | xlxd, URFd treat it as "revision 2": an 11 B reply (own DExtra name + both modules + 0x00, no `ACK` tag), unlink echoed; dxrfd links only peers in its XRF list and sends them no ACK; IG treats it as an ordinary incoming link (14 B ACK) | not measured |
| P-DXT-51 | A stream from an `XRF` peer is routed to the linked local module whatever its RPT2 module says | disputed | not stated | xlxd, URFd rewrite RPT2 to the linked module (revision 2); dxrfd maps the peer's band to its local module through the link table and rewrites both RPT fields | not measured |

## Planned cases and builders

Cases: `cases/proto/test_dextra.py`, one `test_dextra_<obligation>` per
row (names in testlist K2), markers `proto` and `rig`, `slow` for the
keepalive-timeout rows.

Builders to add to `dvtest/proto/dextra.py`:

- `link_packet(..., tail=0x00)`: last byte selectable (0x00, 11);
  `raw_link(callsign8, own, reflector, tail)` for invalid callsigns and
  module bytes; `xrf_link_packet(name, local, remote)`.
- `parse_link_reply(data)`: ACK / NAK (14 B), revision 2 reply (11 B),
  `DISCONNECTED` (12 B), and the two disconnect notices of P-DXT-09.
- `header_packet(sid, my, rpt1, rpt2, ur, suffix, flags, crc, ids,
  byte8, byte14)`: every field settable; `header_crc(radio_header)`
  (CRC-CCITT as IG and xlxd compute it) and `crc_ok(packet)`.
- `parse_header` extended with flags, suffix, CRC and the raw prefix.
- `voice_packet(sid, counter_byte, ambe, slow)`: raw counter byte;
  `last_frame(sid, seq, payload)` with `END_PATTERN`; `parse_voice_raw`
  returning counter byte, AMBE and slow data.
- Slow data: `scramble(b3)` (XOR 70 4F 93), `slow_text(text)` (20 frames,
  mini-headers 0x40-0x43), `slow_header_resend(radio_header)` (0x5x),
  `NULL_SLOW` (filler).
- `DstarClient(keepalive="own" | "echo" | "none")`: keepalive answer mode;
  `link_to(module)` without unlink, for P-DXT-39.
