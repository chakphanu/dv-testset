<!-- SPDX-License-Identifier: MIT -->
# DExtra / D-STAR: obligations of the reflector side

What a reflector serving **DExtra** on UDP 30001 owes a DExtra client: a
repeater gateway (ircDDBGateway, DStarGateway, QnetGateway), a hotspot
behind one of them (Pi-Star, WPSD), an application client (DroidStar), or
another reflector that links with an `XRF` callsign. Each item is numbered
`P-DXT-NN`; the cases are listed under "Testlist rows" at the end
(`docs/testlist.md` section K2).

DV-Router is a server for real clients, so the client side is the
contract: where xlxd differs from what the spec and the clients expect,
the "Recommended for DV-Router" column follows spec and clients. Every
row keeps its sources in separate columns: the spec text (version and
section), what the clients send, expect and tolerate, what the other
servers do (project at a commit, behaviour in our own words, no code
copied), and what xlxd-ref showed on the rig. "measured" in the xlxd
column cites the case or capture that showed it (Stage 2 runs
`out/20261030T061716Z` and `out/20261030T060723Z`, slow rows
`out/20261030T061518Z`; xlxd-sut showed the same as xlxd-ref in every
row); "code" adds what was read in the xlxd source.

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

Out of scope: Pi-Star's "Use DPlus for XRF" option moves XRF hosts to the
DPlus connector (UDP 20001), so such a hotspot never speaks DExtra to the
reflector; Icom terminal and access-point modes use the G2 gateway route,
not DExtra; DCS and DPlus have their own documents.

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
  pattern), 4.2.1 (module letter in the 8th character of a repeater
  callsign), 5.1 item c "Voice data" p.57-58 (trunk header; Call ID: one
  random id per PTT, the same in every packet of the call; management
  byte: bits 7-6 00 voice / 01 last frame / 10 radio header, bit 5 header
  error, bits 4-0 sequence 0..20; recommendation: a radio header before
  every 21-frame block, a receiver that already has the header discards
  the later ones), 5.2 b p.60 (GW <-> GW voice over UDP: `DSVT` 4 B, flag
  2 B (0001 radio header, 0010 data), reserved 2 B, trunk header 7 B, then
  the 41-byte radio header or 9 B voice + 3 B data), 6.1-6.4 (slow data
  mini-headers: 0x3x GPS, 0x40-0x43 message, 0x5x header resend, 0x66
  filler; header resend in 6.4), 6.6 (AMBE silence frame), Ap1 (scrambler;
  it yields the per-frame XOR 70 4F 93), Ap2 (CRC constant 0x8408, the
  reflected form of 0x1021). Earlier English edition, same sections:
  Ver. 6.0a (2019-01) https://www.jarl.org/English/7_dstar/STD6_0_E.pdf.
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
- **[F4FXL]** F4FXL, "REF, XRF, DCS, XLX: what's the difference",
  2018-11-08 (edited 2024-02-19): XRF reflectors speak DExtra, a protocol
  of its own, not compatible with DPlus.
  https://www.f4fxl.org/ref-xrf-dcs-xlx-whats-the-difference/
- Client documentation (no protocol detail, cited for scope and defaults):
  openSPOT4 manual "REF/XRF connector" (SharkRF, 2023; local module D for
  a reflector, E for echo, no timer values)
  https://manuals.sharkrf.com/openspot4/en/ref-xrf-connector.html; WPSD
  manual "D-STAR" (link by dashboard, URCALL or DTMF)
  https://manual.wpsd.radio/zero_hero/d-star/; Pi-Star
  `HostFilesUpdate.sh` (AndyTaylorTweet/Pi-Star_Binaries_sbin @ `33775f3e`,
  2024-02-11: DExtra hosts list from pistar.uk, "Use DPlus for XRF"
  switch); BlueDV "BlueDV for Windows" guide (pa7lim.nl, 2021) and FAQ: UI
  only, no protocol detail.
- Not found: any DExtra protocol text (ircDDB wiki, OpenDV, dxrfd docs,
  the original author KI4LKF); no ETSI, ARIB or ITU document covers
  D-STAR. JARL does not state the CRC initial value, final XOR or byte
  order, nor whether slow data is scrambled on the network: both come
  from the implementations (CRC as IG computes it equals CRC-16/X-25 over
  header bytes 0..38, low byte first; slow data travels scrambled).

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26, v2.6.1) | reference reflector (xlxd-ref); `src/cdextraprotocol.cpp` |
| URFd | https://github.com/n7tae/urfd @ `bd0c114e` (2025-12-29) | XLX-family reflector; `reflector/DExtraProtocol.cpp` |
| dxrfd | https://github.com/w1bsb/dxrfd @ `a4df63e9` (2019-03-03, 3.08a) | the original XRF reflector |
| dxrfd311 | https://github.com/K2IE/dxrfd-3.11 @ `05202d21` (2019-10-14) | last dxrfd release; only its keepalive change is cited |
| IG | https://github.com/g4klx/ircDDBGateway @ `f5ab9f7c` (2026-02-18) | gateway: DExtra client of reflectors (behind Pi-Star and WPSD), and server for incoming links and dongles |
| DSG | https://github.com/F4FXL/DStarGateway @ `7b75de60` (2026-10-28) | IG fork; same DExtra behaviour, plus a 1-byte 0x00 NAT hole-punch packet |
| QN | https://github.com/n7tae/QnetGateway @ `39468e99` (2026-07-06) | gateway (QnetLink): DExtra client, and server for listed gateways |
| DS | https://github.com/nostar/DroidStar @ `4ef18be` (2026-10-29) | application client (`xrf.cpp`) |
| MH / DSR | https://github.com/g4klx/MMDVMHost @ `9384d683`; https://github.com/g4klx/DStarRepeater @ `6f2550b9` | cited for the header CRC check only |

DummyRepeater (g4klx) talks to a gateway over the repeater protocol, and
mvoice is M17 only: neither speaks DExtra.

## How the cases judge

`cases/proto/test_dextra.py` probes xlxd-ref and the system under test in
parallel; the Judged column of the testlist rows says how:

- **hb**: a spec MUST or SHOULD (Level column); asserted on both sides.
- **diff**: a convention the implementations agree on, or an XLX-family
  convention; sut == ref.
- **dev**: disputed between implementations, a convention without
  agreement, or an xlxd deviation from spec or clients. Under
  `DVT_SUT_PROFILE=xlxd` sut == ref (xlxd's behaviour is recorded as
  data); under another profile the profile's spec entry
  (`dvtest/specs/<profile>.py`, `"dextra"` dict `{"P-DXT-NN": {observed
  key: value}}`) decides, and the case skips with "spec pending" while
  there is none. A non-xlxd system is never forced to copy an xlxd choice
  on a disputed item.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

The tester sends spaces as the header CRC (B12: not checked by any
reflector); a case that needs a real CRC computes it. Egress counter,
sync position and last frame are also checked passively by dvcheck
(testlist H2). Ingress that a case makes malformed or out-of-protocol on
purpose (P-DXT-04 invalid links, 14 and 54 streams without a last frame,
15 garbage, 16 stream without a link, 17 header prefix, 19 bad CRC, 21
lower-case fields, 22 flags, 31 counters, 34 voice after the last frame,
42 voice without a header, 52 polls without a link) is bracketed with the
control-channel `mark begin|end dextra-<tag>-<reflector ip>`, so dvcheck
reports findings on those tester flows as WARN `[during mark ...]`.

Conventions: offsets are into the UDP payload. "RPT2" is the destination
field at offset 18, "RPT1" the departure field at offset 26 (QnetGateway
and dxrfd name them the other way round; the bytes agree). The 2-byte
stream id is opaque: JARL and DS put the upper byte first, xlxd and URFd
copy the bytes, IG reads it high byte first and writes it low byte first.
"Silently ignored" means no reply and no state change.

## 1. Link, keepalive, unlink

Packets: link 11 B = callsign (8) + client module + reflector module +
last byte (0x00, or 11 from DS); unlink 11 B = the same with a space as
the reflector module; keepalive 9 B = callsign (8) + 0x00; ACK / NAK
14 B = the first 10 link bytes + `ACK` or `NAK` + 0x00.

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-01 | A link to a served module from a valid callsign is answered with the 14 B ACK (link bytes 0-9 echoed + `ACK` 0x00) | convention (impl, agreed) | not stated (no DExtra spec) | IG, DSG accept only bytes 0-6 = own callsign and byte 8 = own module; QN needs byte 8 = own module and `ACK` at 10; DS needs exactly 14 B with `ACK` at 10 | URFd, dxrfd, QN (server) echo bytes 0-9 + `ACK` 0x00; IG (server) rebuilds the same fields, sent twice | measured: 14 B ACK, bytes 0-9 echoed + `ACK` 0x00 (test_dextra_link_ack); also all 25 links of the clients.md capture | as all: echo bytes 0-9 exactly, then `ACK` 0x00 |
| P-DXT-02 | ACK latency | disputed | not stated | DS sends its link once and never retries (a lost or late ACK leaves it "connecting"); IG, DSG retry after 1 s, backing off to 60 s; QN sends the link 5 times once | URFd, IG, QN, dxrfd answer at once | measured: first ACK 0.35-1.99 s after the link (10 links, two runs); a link repeated while linked is ACKed in 5 ms (P-DXT-06); code: random 0-2 s, one pending ACK per IP | answer at once (well under 1 s) |
| P-DXT-03 | A link to a module letter the reflector does not serve gets NAK and no link | convention (impl, agreed) | not stated | DS ignores NAK (stays "connecting"); IG, QN stop on NAK | URFd NAK; dxrfd NAK (modules A-E, client band A-D); IG (server) NAK without a repeater of that module; QN (server) accepts A-C only | not measurable on the rig: both reflectors serve A-Z (26 modules), the case skips; code: NAK after the random delay | NAK at once, no link |
| P-DXT-04 | A link with an invalid callsign or a non-letter reflector module never links; reply | disputed | not stated | no client sends one | URFd silent (unparsed); dxrfd checks a callsign pattern, NAK; IG does not check the remote callsign; QN (server) requires the callsign in its gateway list | measured: NAK at once to a lower-case callsign and to module `5`, never linked (test_dextra_link_invalid); code: after 3 malformed 11 B links per IP their NAK is suppressed for 5, 30, 60 min; valid links and traffic from that IP are not affected | no link; NAK or silence both acceptable; never block valid clients behind the same NAT address |
| P-DXT-05 | A link whose last byte is 11 ("revision 1", DS) is ACKed like any link; its unlink is answered with `DISCONNECTED` (12 B) | convention (XLX family) | not stated (xlxd comment names UP4DAR) | DS sends 11 with client module = reflector module and reads only the ACK; IG, QN send 0x00 | URFd as xlxd; IG, QN ignore byte 10 (normal link, no unlink reply) | measured: ACKed (14 B, same form as revision 0) and served; its unlink is answered `DISCONNECTED` (12 B) (test_dextra_link_rev1); the old tester note (not ACKed) was wrong | ACK; the `DISCONNECTED` reply is harmless and optional |
| P-DXT-06 | A repeated link (same callsign, module, address) while linked: ACK again, one client | disputed | not stated | IG, DSG send each link twice; QN five times; every copy must leave one client | URFd ACKs, no duplicate; QN (server) re-ACKs; IG (server) ignores the copy silently | measured: ACK again within 5 ms, one client, the stream heard once (test_dextra_relink_single_client) | ACK every copy, one client, traffic once |
| P-DXT-07 | The reflector sends each client a 9 B keepalive (its callsign + 0x00) at a fixed period | form: convention (impl, agreed); period: disputed | not stated (JARL7 5.2 c dummy packets every ~10 s are a different packet) | timeouts that the period must beat: IG, DSG 60 s; QN about 50 s; DS none; QN and IG count any DSVT as life too | URFd every 3 s (to talkers too); IG (server) every 10 s; QN (server) about every 1 s; dxrfd about every 1 s (3.11: 10 B form) | measured: 9 B `XRF999` + 2 spaces + 0x00 every 10.0 s, the first 5.6-10.4 s after the link (test_dextra_reflector_keepalive) | 9 B, own DExtra name + 0x00, every 10 s or less |
| P-DXT-08 | A 9 B keepalive with the client's own callsign refreshes its link; an echo of the reflector's keepalive does not | disputed | not stated | IG, DSG every 10 s, repeater callsign with byte 7 a space; DS every 3 s, own callsign padded; QN every 1 s | URFd: callsign and address must match a client; IG (server): first 7 chars from its ip:port, or voice of the active stream; QN: any 9 B (or DSVT) from the ip:port; dxrfd: any 9/11 B or DSVT from the IP | measured: a client echoing `XRF999` was dropped 68-69 s after its link; the own-callsign answer keeps the link (test_dextra_keepalive_echo_not_refresh) | refresh on any 9 B keepalive from the linked address whose callsign matches the link (first 7 chars, module byte ignored) |
| P-DXT-09 | A silent client is dropped after the keepalive timeout; what it is sent | drop: convention (impl, agreed); timeout and notice: disputed | not stated | IG, DSG accept an unlink only when bytes 0-6 and 8 name the linked reflector; QN needs byte 8 = linked module and byte 9 a space: both ignore a notice without the reflector name | URFd 30 s, sends its callsign + module + space + 0x00; IG 60 s, QN about 50 s, dxrfd about 60 s, all send nothing | measured: dropped 60-70 s after its link (60.2 s, 69.2 s), notice 11 B = 10 spaces + 0x00, voice stopped 0.4-0.8 s later (test_dextra_silent_client_dropped) | drop after 60 s or more; notice either none or callsign + module + space + 0x00 (URFd form) |
| P-DXT-10 | A dropped client gets no further voice | convention (impl, agreed) | not stated | IG, QN relink on their own timeout when set to reconnect | all remove the client | measured: no voice after the drop, none of the next stream (test_dextra_dropped_client_no_voice) | as all |
| P-DXT-11 | A client is not dropped while it is talking (stream owner) | convention (impl, agreed) | not stated | IG sends no keepalive gap while talking (its poll timer runs on); DS keeps pinging every 3 s | URFd refreshes the stream owner; IG and dxrfd count voice as life; QN counts DSVT as life | measured: a talker sending no keepalive through an 85 s stream keeps its link (test_dextra_talker_not_dropped) | as all |
| P-DXT-12 | Unlink (11 B, byte 9 space) removes the client; no reply (except the revision 1/2 forms) | convention (impl, agreed) | not stated | IG, DSG send their unlink twice, QN five times, DS once on closing; none waits for a reply | URFd: no reply (revision 1 `DISCONNECTED`, revision 2 echo); IG, QN, dxrfd no reply | measured: no reply, client removed (test_dextra_unlink_no_reply; also 25 unlinks of the clients.md capture) | remove on the first copy; later copies silently ignored |
| P-DXT-13 | After the unlink no further voice reaches the client | removal: convention (impl, agreed); tail: judged by B5 | not stated | a client that unlinked may already have closed its socket (ICMP unreachable) | URFd, IG stop at once | measured: tail <= 1.0 s (test_xlxd_clients::test_leave_mid_stream_tail[dstar], C7b); code: per-module client list refreshed once a second | stop at once (B5) |
| P-DXT-14 | An unlink from the client that is talking ends its stream and frees the module | disputed | not stated | IG sends its local repeater an end frame on a local unlink; QN clears the slot and lets its 2 s timer end the stream | URFd refuses to remove a stream owner (unlink lost); | measured: the talker's unlink is lost (still linked afterwards); the listener got a last frame 0.7 s after the unlink; the module carried the next stream 2.4 s later (test_dextra_unlink_while_talking) | end the stream (terminator to listeners, B3) and remove the client |
| P-DXT-54 | An unlink from a client whose own stream is still open (no last frame sent) removes it at once: no further keepalives, no later stream | disputed | not stated (JARL has nothing on unlink; there is no DExtra spec) | IG, DSG send the unlink without closing their outgoing stream (the end frame on unlink goes only to the local repeater, for incoming traffic), then stop polling; DS sends its unlink once on disconnect, also mid-transmission with no last frame, then closes its socket; QN not checked | URFd keeps a stream owner on unlink (same rule as xlxd, code; not run); dxrfd removes the link on the unlink with no stream-owner check (code) | measured: unlink ignored: 2 keepalives in the 25 s after it (8.2 s, 18.2 s), and the header and all 30 voice frames + last frame of B's stream 3 s later (test_dextra_unlink_with_open_stream); first seen in a client capture; code: a stream owner is not removed | remove at once even while the client owns a stream: end its stream (B3), no keepalive, no later stream (B5) |
| P-DXT-52 | A 9 B keepalive from an address that is not linked creates no link and gets no traffic | disputed | not stated | dongle-type clients (byte 8 not 0, "DExtra dongle") poll without a link packet; no dongle client was identified in this survey | IG (server) creates a dongle entry for a poll whose byte 8 is not 0 and answers it; URFd ignores it | measured: no reply, no traffic (test_dextra_poll_without_link) | ignore (no dongle mode) |

## 2. Malformed and unknown packets

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-15 | Unknown sizes (1 B, 3 B, 10 B link, 1400 B) and DSVT of a wrong size (55, 57, 26, 28 B) get no reply; the reflector keeps serving | convention (impl, agreed) | not stated | DSG sends a 1-byte 0x00 NAT hole-punch packet before linking; others send only the defined sizes | URFd dumps and drops; IG classifies non-DSVT by size (9 poll, 11/14 link) and drops the rest; QN, dxrfd drop | measured: no reply to 1 B 0x00 (before the link), 3 B, 10 B link, 1400 B, DSVT 55/57/26/28 B; the next stream arrives whole (test_dextra_garbage_ignored) | ignore silently; never ban for it |
| P-DXT-16 | DSVT header and voice from an address that is not linked are not relayed | convention (impl, agreed) | not stated | clients link before talking | URFd opens no stream without a client; IG needs the link's ip:port; QN a linked ip:port; dxrfd a known IP | measured: not relayed (test_dextra_dsvt_not_linked) | as all |
| P-DXT-17 | DSVT prefix checks: header with byte 8 not 0x20, byte 14 not 0x80, other bytes 9-11 | disputed | JARL7 5.2 b (flag, reserved, trunk header: type, three ids, Call ID, management) | IG, DSG send band bytes from the repeater (default 00 02 01); DS, QN 00 01 02 (QN puts its own module in byte 11) | URFd: size, `DSVT`, byte 4 and byte 8 checked, bytes 9-11 and 14 ignored; IG: only `DSVT` and byte 14 (0x80 header, else voice), size unchecked; QN: size, `DSVT`, bytes 4 and 8 | measured: byte 8 = 0x21 dropped; byte 14 = 0x00 and ids 00 02 01 relayed (test_dextra_header_prefix) | accept any bytes 9-11; require size, `DSVT`, byte 4, byte 8 |

## 3. DSVT header (56 B)

Offsets: 0-3 `DSVT`, 4 0x10, 5-7 0, 8 0x20, 9-11 trunk ids (xlxd and
URFd send 00 01 02), 12-13 stream id, 14 0x80, 15-17 flags, 18 RPT2, 26
RPT1, 34 UR, 42 MY, 50 MY suffix, 54-55 P_FCS (low byte first in IG and
xlxd).

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-18 | A 56 B header laid out as above opens a stream on a linked client's module | MUST (JARL7 layout) | JARL7 5.2 b p.60 (DSVT, flag 0001, trunk header), 5.1 c p.58 (management 10 = radio header), 4.1.1 (radio header); ON1ARF byte map | all send this layout: IG, DSG 5 copies; QN 5; DS once (RPT1 = own callsign + module, RPT2 = reflector + module, suffix `AMBE`, CRC computed) | agreed: URFd, IG, QN, dxrfd | measured: a JARL header with valid P_FCS opens the stream: 5 header copies + every frame (test_dextra_header_opens_stream); spaces as CRC in every rig case | as all |
| P-DXT-19 | The ingress header CRC (P_FCS) is not required to be valid | field: MUST (JARL7 4.1.1 k); receiver action: not stated | JARL7 4.1.1 k (CRC-CCITT; no init, bit order or bad-CRC action given) | IG writes FF FF when it does not compute the CRC; DS, QN compute it | URFd, QN, dxrfd never check; IG skips the check ("DExtra checksums are unreliable"); MH does not check; DSR drops a bad CRC unless it is FF FF | measured: spaces, FF FF, a wrong and a valid CRC all relayed (test_dextra_header_crc_ingress) | accept any CRC (checking would drop IG headers) |
| P-DXT-20 | The egress header carries a valid P_FCS for the fields it actually sends | MUST (JARL7 4.1.1 k) | JARL7 4.1.1 k | clients do not check it on DExtra; a radio behind a gateway that passes the CRC through does | URFd keeps the ingress value though it rewrote RPT2 (stale); IG and QN compute it; dxrfd has no CRC code | measured: every egress header carries a valid P_FCS after the RPT2 rewrite (test_dextra_egress_header_crc) | compute it over the bytes sent |
| P-DXT-21 | A header whose MY, RPT1 or RPT2 holds characters outside A-Z, 0-9, space (or starts with three digits) is dropped | disputed | JARL7 4.1.1 (callsign fields: ASCII, space padded) | real callsigns only; DS sends lower case only if the user types it (not checked) | URFd drops it (RPT1, RPT2, MY); dxrfd checks MY against its callsign pattern; IG only applies its white/black list to MY; QN does not check | measured: lower-case MY, RPT1 or RPT2 dropped (test_dextra_header_invalid_callsign) | drop invalid MY; RPT1/RPT2 rewritten anyway, so only MY matters |
| P-DXT-22 | The flag bytes (flag 1-3) reach egress unchanged | disputed | JARL7 4.1.1 (flag 1: data, repeater, interruption, control, emergency bits) | IG, DSG write 00 00 00 on egress to the reflector; QN forwards flag 1 only in {00, 08, 20, 28} and clears 0x40 from hotspots | URFd passes them; IG (server) writes 00 00 00 | measured: 40 01 02 passed unchanged (test_dextra_header_flags_passed) | pass unchanged (the caller's flags; QN cleans its own ingress) |
| P-DXT-23 | A header with stream id 0 opens no stream | disputed; judged by B8 | JARL7 5.1 c (Call ID random; zero not mentioned) | IG uses 0 as "no stream"; DS clears its id to 0 after a stream (sends a fresh one next time) | URFd refuses it; IG treats 0 as idle | code: refused; not measured (test_floor::test_admission_sid_zero runs it) | refuse (B8) |
| P-DXT-24 | A header repeated with the same stream id while the stream runs is discarded: no second stream, no restart | SHOULD (JARL7 5.1 c recommendation) | JARL7 5.1 c p.58 (a receiver that already has the header discards later ones) | IG, DSG, QN send 5 copies at stream start | URFd only refreshes the stream; IG ignores a header while a stream is active | measured: the repeat is discarded: frames and counters continuous, one id, no egress header mid-stream (test_dextra_header_repeat_discarded) | as JARL: discard |
| P-DXT-25 | A header whose stream id is already open on another module is dropped (loop guard) | convention (XLX family); judged by B8 | not stated | clients pick random ids | URFd refuses an id open anywhere; IG, QN have no such check | measured: same id in a second room dropped (testlist B8 notes) | B8 |
| P-DXT-26 | A header or voice whose id equals a module's last outbound id, from an address other than that stream's owner, is dropped | disputed | not stated | a client that relays a stream back (loop) reuses the id; a fresh client id can collide by chance (1 in 65536) | xlxd only; URFd, IG, QN, dxrfd have no such filter | measured: a stream on Q reusing the id that P just sent (stream ended) from another port is dropped (test_dextra_outbound_id_echo) | do not drop on id reuse alone once the earlier stream has ended; loops are B8/B11 |

## 4. Voice, superframe, last frame (27 B)

Offsets: 0-13 as the header but byte 4 0x20, 14 counter (0..20, 0x40 on
the last frame), 15-23 AMBE (9 B), 24-26 slow data (3 B).

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-27 | The 9 AMBE bytes are relayed unchanged on same-mode egress | convention (impl, agreed) | JARL7 4.1.2 b (72-bit voice per 20 ms) | all decode the AMBE as received | agreed: URFd, IG, QN, dxrfd | measured: byte exact (test_xlxd_matrix::test_passthrough_exact, C1) | as all |
| P-DXT-28 | The 3 slow-data bytes (text, header resend, GPS, filler; scrambled) are relayed unchanged on same-mode egress | convention (impl, agreed) | JARL7 4.1.2 e (data frame passed as given), 6.1-6.4, Ap1; SLOW | DS writes scrambled text (mini-header 0x40-0x43) and reads sync at counter 0; IG, QN pass the bytes | URFd, IG, QN pass them | measured: passed on same-mode except the frame before the last frame, whose slow data becomes 55 55 55 (P-DXT-33) (test_dextra_slow_data_passthrough) | as all |
| P-DXT-29 | Egress counter: 0..20 in order, wrapping, header byte 0x80, last frame 0x40 | MUST (JARL7 5.1 c) | JARL7 5.1 c p.58 (management byte) | DS shows the counter; IG, QN pass it to the radio side | URFd writes its counter mod 21 (its last frame lands one higher); IG, QN pass the byte | measured: header 0x80, voice 0..20 in order, last 0x40 + the next counter (test_dextra_egress_counter); dvcheck H2 clean | as JARL |
| P-DXT-30 | Egress frame 0 of every 21 carries the sync pattern 55 2D 16, no other frame does | MUST (JARL7 4.1.2 c/d) | JARL7 4.1.2 c/d (resync every 21 frames; byte form per ON1ARF) | DS checks counter 0 + sync bytes to find the superframe | URFd, IG, QN pass the ingress bytes and neither check nor restore it | measured: sync at counter 0 only (test_dextra_egress_sync) | pass on same-mode; write on cross-mode |
| P-DXT-31 | Voice with a counter outside 0..20 (or bit 0x80 set) | disputed | JARL7 5.1 c (0..20) | no client sends one | URFd keeps the received byte as the frame number and writes it mod 21; IG masks it with 0x1F | measured: relayed whole; egress counter = received byte mod 21 (21..31 -> 0..10; with 0x80 set the whole byte mod 21) (test_dextra_counter_out_of_range) | relay with the counter written mod 21 (no drop) |
| P-DXT-32 | A last frame (0x40) ends the stream and reaches the listeners | MUST (JARL7 4.1.2 g, 5.1 c) | JARL7 4.1.2 g/h (last frame), 5.1 c (frame type 01) | DS ends RX on 0x40; IG passes it to its repeater | agreed | measured: the last frame reaches the listener; the next talker 0.6 s later is relayed whole (test_dextra_last_frame_ends_stream) | as all |
| P-DXT-33 | Payload of the egress last frame | disputed | JARL7 4.1.2 h (RF end pattern: sync 32 bits + 000100110101111 + 0, i.e. 55 55 55 55 C8 7A) | DS sends real AMBE in its last frame; IG writes 55 55 55 55 C8 7A + zeros only on a local unlink, else passes it | URFd, QN pass the ingress payload | measured: last frame = 55 C8 7A + six zeros as AMBE, 25 1A C6 as slow data, and the slow data of the frame before it replaced by 55 55 55: the end pattern 55 55 55 55 C8 7A runs across the two packets (test_dextra_last_frame_payload) | pass the ingress payload on same-mode; on a synthesized end use the JARL end pattern |
| P-DXT-34 | Voice after the last frame with the same stream id is not relayed | convention (impl, agreed) | not stated | clients start a new id per PTT (DS clears it, IG new per stream) | URFd: the stream is closed, a new header is needed; IG one id per stream; dvcheck rule `after_last` | measured: not relayed (test_dextra_voice_after_last) | as all |
| P-DXT-53 | A stream whose first voice counter is not 0 (DS starts at 1) is relayed from its first frame | convention (impl) | JARL7 5.1 c (sequence numbers of voice packets; start value not stated for the network) | DS: the header takes counter slot 0, its first voice frame is 1, the first sync frame comes 20 frames later | URFd, IG, QN take the counter as given | measured: relayed from its first frame, counter 1 kept (test_dextra_first_counter_not_zero) | relay from the first frame, counter as given |

## 5. Routing per module and RPT fields

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-35 | A header with RPT2 = reflector + the linked module reaches every other client on that module once | convention (impl, agreed) | JARL7 4.2.1 (module letter in the 8th character) | IG, DSG, QN, DS put reflector + linked module into RPT2 | agreed | measured: both listeners of P once, the Q listener nothing (test_dextra_module_fanout); C1/C3 | as all |
| P-DXT-36 | A header whose RPT2 module differs from the client's linked module | disputed | JARL7 4.1.1 (RPT2 = destination) | no client sends one in normal use (all write the linked module) | URFd routes by the linked module; dxrfd by the linked band; IG drops a header whose RPT fields do not name the link | measured: routed by the RPT2 module: the listener on Q hears it, the linked module P does not (test_dextra_rpt2_other_module) | route by the linked module (the link is the contract; never into a module the client is not linked to) |
| P-DXT-37 | Egress RPT fields: RPT2 = reflector name + module; RPT1, UR, MY pass | RPT2: convention (XLX family); the rest: disputed | JARL7 4.1.1 (RPT2 destination, RPT1 departure) | IG, DSG accept a header only when RPT2 or RPT1 equals the linked reflector name + module, and set UR to CQCQCQ; QN needs RPT2 (offset 18) = linked reflector + module; whether a gateway linked as `XLX999` accepts `XRF999`: not checked | URFd its DExtra name + module in RPT2, the rest passed (stale CRC); IG (server) rebuilds the header | measured: RPT2 `XRF999 P`; RPT1, UR, MY kept; empty suffix filled with `DXTR` (test_dextra_egress_rpt_fields) | RPT2 = DExtra name + module; RPT1, UR, MY, suffix unchanged; CRC recomputed |
| P-DXT-38 | Module change: unlink, link to another module; then heard on the new module only | convention (impl, agreed) | not stated | IG, QN, DS change module by unlink + link | agreed | measured: test_xlxd_clients::test_module_change_follows_talker[dstar] (C7) | as all |
| P-DXT-39 | A link to a second module from the same address without unlink | disputed | not stated | IG (client) links one module per local repeater, each from its own repeater callsign; DS one link per session | URFd adds a client per module; IG (server) one entry per module (5 max); dxrfd one user per IP with a module table; QN one peer per local module | measured: ACK; hears both modules; one unlink removes the P link only, Q stays (test_dextra_second_module_link) | treat as a module change (one link per callsign and address) |

## 6. Stream lifetime

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-40 | A stream whose last frame is lost ends after a silence timeout; the module is then free | convention (impl, agreed); timeout and end marker judged by B3 | not stated | IG, DSG end a stream after 2 s of silence; DS after about 2 s (RX watchdog); QN after 2 s and sends its radio an end frame | URFd 1.6 s, sends nothing | measured: module free within 2.0 s; every egress gets an end marker, D-STAR listeners a last frame (test_xlxd_clients::test_stream_timeout_frees_module, C8; testlist "Upstream behaviour") | B3: end within 1.6 s with a synthesized last frame (JARL end pattern) |
| P-DXT-41 | A second header on a busy module is not mixed into the running stream | convention (impl, agreed); late-entry policy judged by B1/B2 | not stated | IG, QN accept one stream per link at a time | URFd one stream per module, the late header dropped; IG one stream per link | measured: first stream whole; late talker relayed after the first ends, without a header (testlist B notes) | B1/B2 |
| P-DXT-42 | Voice whose stream id had no header is not relayed (no late entry) | disputed | JARL7 4.1.2 c (resync allows reception from mid-call), 6.4 (header resend in slow data) | IG, DS need a header before voice | URFd, IG drop it; QN passes it to its own radio side | measured: not relayed (test_dextra_voice_without_header) | drop |
| P-DXT-43 | The talker never receives its own stream back | convention (impl, agreed) | not stated | IG, QN drop their own stream if echoed (id check); DS would play it | URFd skips the stream owner; IG skips the link it receives from; QN the source ip:port | measured: no echo of a normal stream (test_dextra_no_self_echo); a promoted late entrant was echoed (71 frames) in some runs (testlist B16 notes) | never echo (B9) |
| P-DXT-44 | A client that links while a stream runs gets that stream's header before its voice | disputed | JARL7 5.1 c recommendation (header before every 21-frame block) | IG, DS drop voice without a header, so a mid-stream joiner hears nothing without one | URFd sends nothing (the client hears nothing until the next stream); dxrfd not checked | measured: the late listener's first packet of the running stream is a header (test_dextra_late_listener_header) | send the running stream's header to a new listener before its first voice |
| P-DXT-45 | Header copies on egress | disputed | JARL7 5.1 c recommendation (a header before every 21-frame block) | IG, DSG, QN send 5 copies; DS 1; all receivers tolerate duplicates | URFd, QN send 5 copies at stream start and none later; IG 5 to a reflector | measured: 5 copies before the first voice, none later (test_dextra_egress_header_copies; 13 in / 90 out in the clients.md capture) | at least 1 before the first voice; repeats allowed (JARL 5.1 c) |
| P-DXT-46 | One stream id per transmission on egress; equal to the ingress id | constant: MUST (JARL7 5.1 c); equal to ingress: judged by B10 | JARL7 5.1 c (same Call ID for every packet of one PTT) | IG, QN, DS match header and voice by id | URFd, QN pass the id; IG byte-swaps it (constant, but not the ingress bytes) | measured: one id per transmission, equal to the ingress id (test_dextra_egress_stream_id; B10) | B10 |

## 7. Several clients

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-47 | Two callsigns from one IP (different ports) are two clients; each gets the module's traffic once | convention (impl: xlxd, URFd, IG; dxrfd differs) | not stated | hotspots behind one NAT address link with their own callsigns and ports | URFd keys by callsign + ip:port + module; IG by ip:port + callsigns; dxrfd keys users by IP only | measured: two callsigns from one IP each get the stream once (test_dextra_two_callsigns_one_ip; C1) | as xlxd and URFd |
| P-DXT-48 | The same callsign from two ports | disputed | not stated | an IG that restarts may link again from a new port before the old link times out | URFd two clients; IG two entries (ip:port); dxrfd one user (IP) | measured: two clients, each gets the stream once (test_dextra_same_callsign_two_ports) | two clients (the old one times out) |
| P-DXT-49 | A keepalive from one client never refreshes another client | convention (impl, agreed) | not stated | each client polls with its own callsign | URFd matches callsign + address; IG per link; QN per ip:port; dxrfd per IP (so two clients on one IP share a timer) | measured: the keeper on the same IP kept its link and the whole stream while the silent client was dropped (test_dextra_keepalive_isolated) | as xlxd and URFd |

## 8. Reflector peers (`XRF` callsign)

xlxd also opens DExtra links to other reflectors from its interlink list
(link with its `XRF` name, keepalive 10 s, timeout 60 s); that needs rig
configuration and is out of scope here. In scope: how a reflector answers
a peer that links in.

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DXT-50 | A link from an `XRF` callsign: reply form | disputed | not stated | IG, QN, DS never link with an `XRF` callsign; a reflector does | URFd treats it as "revision 2": an 11 B reply (own DExtra name + both modules + 0x00, no `ACK` tag), unlink echoed; dxrfd links only peers in its XRF list and sends them no ACK; IG treats it as an ordinary incoming link (14 B ACK) | measured: 11 B reply `XRF999  PB` 0x00 (own DExtra name, linked module, peer module), no `ACK` tag; its unlink is echoed (test_dextra_xrf_peer_link) | spec decision (peering is server-to-server; follow the XLX-family form if DV-Router accepts XRF peers) |
| P-DXT-51 | A stream from an `XRF` peer is routed to the linked local module whatever its RPT2 module says | disputed | not stated | n/a | URFd rewrites RPT2 to the linked module (revision 2); dxrfd maps the peer's band to its local module through the link table and rewrites both RPT fields | measured: routed to the linked module P although RPT2 named Q (test_dextra_xrf_peer_routing) | as URFd |

## Where the recommendation differs from xlxd

| # | xlxd | Recommended for DV-Router | Why |
|---|---|---|---|
| P-DXT-02 | first ACK after a random 0-2 s | at once | DS sends its link once and never retries |
| P-DXT-09 | timeout notice of 10 spaces + 0x00 | none, or callsign + module + space + 0x00 | IG and QN ignore xlxd's form; URFd's form is the one they parse |
| P-DXT-14 | unlink of the talker lost, stream runs on | end the stream, remove the client | the client asked to leave |
| P-DXT-54 | unlink with an open stream lost: keepalives and later streams keep going to the client | remove at once (B5) | IG, DSG and DS do unlink mid-stream; the client may have closed its socket |
| P-DXT-26 | id equal to the last outbound id dropped, also after the stream ended | no drop once the earlier stream ended | random ids collide; loops are handled by B8/B11 |
| P-DXT-33 | last frame payload replaced by 55 C8 7A + zeros / 25 1A C6, and the previous frame's slow data by 55 55 55 | pass the ingress payload and slow data; JARL end pattern only when synthesized | clients and URFd pass them; xlxd's end pattern starts 3 bytes early, inside the slow data of the last voice frame |
| P-DXT-36 | routed by the RPT2 module | route by the linked module | the link is the contract; RPT2 into another module bypasses it |
| P-DXT-37 | empty MY suffix filled with `DXTR` | suffix unchanged | no client or other server writes it |
| P-DXT-39 | second link from the same address adds a second client | one link per callsign and address (module change) | an unlink removes only one of them in xlxd |

## Testlist rows

The same rows as `docs/testlist.md` K2, plus P-DXT-52, P-DXT-53 and P-DXT-54 (new;
the lead merges them into the testlist).

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DXT-01 | Link to a served module -> 14 B ACK (bytes 0-9 + ACK 0x00) | test_dextra::test_dextra_link_ack | diff | probe + dvcheck | covered |
| P-DXT-02 | ACK latency recorded (xlxd: random 0-2 s) | test_dextra::test_dextra_ack_latency | dev | probe | covered |
| P-DXT-03 | Link to a module not served -> NAK, no link | test_dextra::test_dextra_link_unserved_module_nak | diff | probe | covered (skips: rig serves A-Z) |
| P-DXT-04 | Link with invalid callsign / non-letter module: no link; reply (NAK or silence) recorded | test_dextra::test_dextra_link_invalid[callsign\|module] | dev | probe | covered |
| P-DXT-05 | Link with last byte 11 (revision 1) ACKed; unlink answered DISCONNECTED | test_dextra::test_dextra_link_rev1 | diff | probe | covered |
| P-DXT-06 | Repeated link while linked: re-ACK, one client, traffic once | test_dextra::test_dextra_relink_single_client | dev | probe | covered |
| P-DXT-07 | Reflector keepalive: 9 B callsign + 0x00; period recorded | test_dextra::test_dextra_reflector_keepalive | diff + dev | probe + dvcheck | covered |
| P-DXT-08 | Own-callsign keepalive refreshes; echo of the reflector keepalive does not | test_dextra::test_dextra_keepalive_echo_not_refresh | dev (slow) | probe | covered |
| P-DXT-09 | Silent client dropped after the timeout; notice recorded | test_dextra::test_dextra_silent_client_dropped | dev (slow) | probe | covered |
| P-DXT-10 | Dropped client gets no further voice | test_dextra::test_dextra_dropped_client_no_voice | diff (slow) | probe | covered |
| P-DXT-11 | Talking client not dropped past the keepalive timeout | test_dextra::test_dextra_talker_not_dropped | diff (slow) | probe | covered |
| P-DXT-12 | Unlink removes the client, no reply | test_dextra::test_dextra_unlink_no_reply | diff | probe + dvcheck | covered |
| P-DXT-13 | No voice after unlink (tail) | test_xlxd_clients::test_leave_mid_stream_tail[dstar] | spec (B5) | probe | covered (C7b) |
| P-DXT-14 | Unlink by the talker ends its stream, frees the module | test_dextra::test_dextra_unlink_while_talking | dev | probe | covered |
| P-DXT-54 | Unlink with the client's own stream open: no keepalive, no later stream to it | test_dextra::test_dextra_unlink_with_open_stream | dev | probe | covered |
| P-DXT-15 | Unknown / wrong-size packets ignored, reflector keeps serving | test_dextra::test_dextra_garbage_ignored[nat1\|short3\|link10\|oversize\|dsvt55\|dsvt57\|voice26\|voice28] | diff | probe | covered |
| P-DXT-16 | DSVT from a non-linked address not relayed | test_dextra::test_dextra_dsvt_not_linked | diff | probe | covered |
| P-DXT-17 | Header prefix variants (byte 8, byte 14, bytes 9-11) | test_dextra::test_dextra_header_prefix[byte8\|byte14\|ids] | dev | probe | covered |
| P-DXT-18 | 56 B JARL-layout header opens a stream | test_dextra::test_dextra_header_opens_stream | hb | probe | covered |
| P-DXT-19 | Ingress header CRC not required (spaces / FF FF / wrong / valid) | test_dextra::test_dextra_header_crc_ingress[spaces\|ffff\|wrong\|valid] | diff (B12) | probe | covered |
| P-DXT-20 | Egress header P_FCS valid | test_dextra::test_dextra_egress_header_crc | hb | probe + dvcheck | covered |
| P-DXT-21 | Header with invalid MY / RPT1 / RPT2 characters | test_dextra::test_dextra_header_invalid_callsign[my\|rpt1\|rpt2] | dev | probe | covered |
| P-DXT-22 | Flag bytes reach egress unchanged | test_dextra::test_dextra_header_flags_passed | dev | probe | covered |
| P-DXT-23 | Stream id 0 opens no stream | test_floor::test_admission_sid_zero | spec (B8) | probe | covered (B8) |
| P-DXT-24 | Repeated header with the same id mid-stream: no second stream, no restart | test_dextra::test_dextra_header_repeat_discarded | hb | probe | covered |
| P-DXT-25 | Id already open on another module dropped | test_floor::test_admission_same_sid_two_rooms | spec (B8) | probe | covered (B8) |
| P-DXT-26 | Reuse of a module's last outbound id from another address | test_dextra::test_dextra_outbound_id_echo | dev | probe | covered |
| P-DXT-27 | AMBE relayed byte exact | test_xlxd_matrix::test_passthrough_exact | diff | probe | covered (C1) |
| P-DXT-28 | Slow data (text, header resend, filler) relayed unchanged; frame before the last recorded | test_dextra::test_dextra_slow_data_passthrough[text\|header\|filler] | diff | probe | covered |
| P-DXT-29 | Egress counter 0..20, 0x80 header, 0x40 last | test_dextra::test_dextra_egress_counter | hb | probe + dvcheck | covered |
| P-DXT-30 | Egress sync 55 2D 16 in frame 0 only | test_dextra::test_dextra_egress_sync | hb | probe + dvcheck | covered |
| P-DXT-31 | Voice with counter > 20 or bit 0x80 | test_dextra::test_dextra_counter_out_of_range | dev | probe | covered |
| P-DXT-32 | Last frame ends the stream and reaches listeners | test_dextra::test_dextra_last_frame_ends_stream | hb | probe + dvcheck | covered |
| P-DXT-33 | Egress last-frame payload and the slow data of the frame before it recorded | test_dextra::test_dextra_last_frame_payload | dev | probe | covered |
| P-DXT-34 | Voice after the last frame (same id) not relayed | test_dextra::test_dextra_voice_after_last | diff | probe + dvcheck | covered |
| P-DXT-35 | Header to the linked module reaches every other client once | test_dextra::test_dextra_module_fanout | diff | probe | covered |
| P-DXT-36 | RPT2 module differs from the linked module | test_dextra::test_dextra_rpt2_other_module | dev | probe | covered |
| P-DXT-37 | Egress RPT2 / RPT1 / UR / MY / suffix recorded | test_dextra::test_dextra_egress_rpt_fields | diff + dev | probe | covered |
| P-DXT-38 | Module change via unlink + relink | test_xlxd_clients::test_module_change_follows_talker[dstar] | diff | probe | covered (C7) |
| P-DXT-39 | Link to a second module without unlink | test_dextra::test_dextra_second_module_link | dev | probe | covered |
| P-DXT-40 | Lost last frame: stream ends after the timeout, module free | test_xlxd_clients::test_stream_timeout_frees_module | spec (B3) | probe | covered (C8) |
| P-DXT-41 | Second header on a busy module not mixed in | test_floor::test_first_talker_wins, ::test_late_entry_denied | spec (B1/B2) | probe | covered (B1/B2) |
| P-DXT-42 | Voice without a header not relayed | test_dextra::test_dextra_voice_without_header | dev | probe | covered |
| P-DXT-43 | Talker never gets its own stream back | test_dextra::test_dextra_no_self_echo | diff | probe | covered |
| P-DXT-44 | Listener linking mid-stream gets the header first | test_dextra::test_dextra_late_listener_header | dev | probe | covered |
| P-DXT-45 | Egress header copies counted | test_dextra::test_dextra_egress_header_copies | dev | probe + dvcheck | covered |
| P-DXT-46 | One stream id per transmission on egress; equal to ingress | test_dextra::test_dextra_egress_stream_id | hb + spec (B10) | probe + dvcheck | covered |
| P-DXT-47 | Two callsigns from one IP: two clients, traffic once each | test_dextra::test_dextra_two_callsigns_one_ip | diff | probe | covered |
| P-DXT-48 | Same callsign from two ports | test_dextra::test_dextra_same_callsign_two_ports | dev | probe | covered |
| P-DXT-49 | Keepalive of one client does not refresh another | test_dextra::test_dextra_keepalive_isolated | diff (slow) | probe | covered |
| P-DXT-50 | Link from an XRF callsign: reply form | test_dextra::test_dextra_xrf_peer_link | dev | probe | covered |
| P-DXT-51 | Stream from an XRF peer routed to the linked local module | test_dextra::test_dextra_xrf_peer_routing | dev | probe | covered |
| P-DXT-52 | Keepalive from a non-linked address: no link, no traffic | test_dextra::test_dextra_poll_without_link | dev | probe | covered |
| P-DXT-53 | First voice counter 1 (DroidStar): relayed from the first frame | test_dextra::test_dextra_first_counter_not_zero | diff | probe | covered |

## Cases and builders

Cases: `cases/proto/test_dextra.py`, one `test_dextra_<obligation>` per
row above (rows covered elsewhere point there), markers `proto` and
`rig`, `slow` for the keepalive-timeout rows (P-DXT-08, 09, 10, 11, 49
share one 85 s scenario). Modules P (main) and Q (other); no cross-mode.
Unit round trips: `cases/unit/test_dextra_conf.py`.

Builders in `dvtest/proto/dextra.py` (existing functions keep their
signatures and bytes; `parse_header` keeps its five keys):

- `link_packet(..., tail=0)` (11 for revision 1), `raw_link(callsign8,
  own, reflector, tail)`, `xrf_link_packet(name, local, remote)`,
  `NAT_PUNCH` (1 B 0x00), `parse_link_reply(data)` (ack, nak, rev2,
  disconnected, notice_blank, unlink, keepalive).
- `radio_header(my, rpt1, rpt2, ur, suffix, flags, crc, module,
  reflector)`, `header_packet(sid, radio, ids, byte8, byte14, **fields)`,
  `header_crc(radio39)` (CRC-16/X-25, low byte first), `crc_ok(packet)`,
  `parse_header_full(data)` (prefix bytes, flags, suffix, CRC, crc_ok).
- `voice_packet(sid, counter_byte, ambe, slow)`, `last_frame(sid,
  counter, payload=END_PATTERN)`, `parse_voice_raw(data)`, `NULL_AMBE`.
- Slow data: `scramble(b3)` (XOR 70 4F 93), `slow_text(text)`,
  `slow_header_resend(radio41)`, `NULL_SLOW` (filler 16 29 F5).
- `DstarClient(..., keepalive="own" | "echo" | "none")`, `link_to(module)`.
