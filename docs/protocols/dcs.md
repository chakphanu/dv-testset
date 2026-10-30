<!-- SPDX-License-Identifier: MIT -->
# DCS / D-STAR: obligations of the reflector side

What a reflector serving **DCS** on UDP 30051 owes a DCS client: a repeater
gateway (ircDDBGateway, DStarGateway, QnetGateway), a hotspot behind one of
them (Pi-Star, WPSD), a closed client (openSPOT, BlueDV) or an application
client (DroidStar). Each item is numbered `P-DCS-NN`; the cases are listed
under "Testlist rows" at the end.

DV-Router is a server for real clients, so the client side is the
contract: where xlxd differs from what the spec and the clients expect,
the "Recommended for DV-Router" column follows spec and clients. Every row
keeps its sources in separate columns: the spec text (version and
section), what the clients send, expect and tolerate, what the other
servers do (project at a commit, behaviour in our own words, no code
copied), and what xlxd-ref showed on the rig. "measured" in the xlxd
column cites the run of `cases/proto/test_dcs.py` that showed it
(`out/20261030T061551Z-i12`, short `061551Z`, the rerun `062141Z`, and
the full rerun with 63-frame overs and dvcheck marks `062633Z` + `063300Z`,
dvcheck exit 0; xlxd-sut behaved the same in every case); "code" means read in the xlxd
source only.

**Level**: MUST / SHOULD only where a spec says so; `convention (impl)`
where only implementations back it, naming the ones that agree;
`disputed` where they differ, naming who does what; `convention (XLX
family)` for rules xlxd and URFd share.

There is no DCS specification. DCS was designed by DG1HT in 2012 for the
closed-source DCS reflectors (xreflector.net); the published material is
history, operating conventions and client settings only. Its one design
statement that matters here: there is no separate header packet, every
voice packet carries the routing header, so a listener can join mid-over.
The packet layouts below are therefore defined by the implementations;
the D-STAR content carried inside (radio header fields, voice frame,
superframe, last frame) comes from the JARL standard.

## Sources

Specs and published notes:

- **[JARL7]** JARL, "Digitalization Technology Standard for Amateur Radio
  - D-STAR", Ver. 7.0 (2025-05), English, 106 pp.
  https://www.jarl.org/English/7_dstar/STD7_0_E.pdf. Used: 4.1.1 c-k p.25-28
  (flags, RPT2 destination, RPT1 departure, UR, MY, MY suffix, P_FCS:
  DCS carries the header without P_FCS), 4.1.2 a-h p.29-30 (72-bit voice +
  24-bit data per 20 ms, resync every 21 frames, last frame end pattern),
  4.2.1 p.31 (8th character of a repeater callsign = module), 5.1 (3) c
  p.55-58 (Call ID: one random 16-bit id per PTT, same in every packet;
  sequence 0..20; last-frame type), 6.1-6.4 and 6.6 (slow data
  mini-headers, 20-character message, header resend, loss filler), Ap1.3
  p.98 (scrambling; sync and last frame are not scrambled). Earlier
  English edition, same sections: Ver. 6.0a (2019-01)
  https://www.jarl.org/English/7_dstar/STD6_0_E.pdf.
- **[G4VXE]** T. Kirby (G4VXE), "D-STAR: New DCS reflectors and DV-RPTR
  boards", AmateurRadio.com, 2012-04-21: DCS by DG1HT (February 2012), no
  header packet, every AMBE packet carries the header (join mid-over),
  modules A-Z. https://www.amateurradio.com/d-star-new-dcs-reflectors-and-dv-rptr-boards/
  (same text: KB9MWR "DCS D-Star Reflectors",
  https://qsl.net/k/kb9mwr/projects/dv/dstar/dcs-reflectors.html)
- **[W6KD]** B. Scott (W6KD), "Reflections on Reflectors", Rev 1.02,
  2016-06-15, p.1-3: DCS reflectors closed source, centrally administered,
  gateways only; the client opens the link (no inbound firewall rule).
  https://www.qsl.net/kb9mwr/projects/dv/dstar/Reflections%20on%20Reflectors%201.02.pdf
- **[WCARA]** Warren County ARA, "D-Star DCS" (undated): modules A and B
  linked worldwide, module Z echo; link URCALL `DCSnnnML`, unlink
  `DCSnnnMU`; status pages `dcs_info.htm`, `dcs_rptstatus.htm`,
  `dcs_userstatus.htm`. https://www.sites.google.com/site/wcarasite/digital-comm/d-star-dcs
- **[CCS7]** DG1HT and DL5DI, notice of 2015-08-21 (reposted by WCARA,
  "CCS7 - what's that?"): DCS access will require a CCS7 id (registration);
  already enforced on some European servers; how a refusal looks is not
  said. https://sites.google.com/site/wcarasite/digital-comm/d-star-ccs7-whats-that
- **[DCC16]** AG0X / DG1HT, "Multi Protocol Digital Networks" and
  "DV4Server", TAPR DCC 2016: overview only (33 reflector systems, 26
  rooms each). https://web.tapr.org/meetings/DCC_2016/DCC2016-Multi_Protocol_Digital_Networks_Uli_AG0X_Torsten_DG1HT.pdf
- **[XRN18]** xreflector.net notices of 2018 (reposts): closure announced
  with XLX named as the replacement "using the same protocol"
  (https://www.zendamateur.com/viewtopic.php?p=216648, 2018-09-22), then
  continued under new operators
  (https://groups.google.com/g/dsc021/c/ZjhZxE6pLow, 2018-10-08). Today
  DCS numbers also map to XLX reflectors.
- **[F4FXL]** F4FXL, "REF, XRF, DCS, XLX: what's the difference",
  2018-11-08 (edited 2024-02-19): `DCSnnnML` works against XLXnnn.
  https://f4fxl.org/ref-xrf-dcs-xlx-whats-the-difference
- Client documentation (scope and defaults, no packet detail):
  **[OS]** SharkRF openSPOT2 (2018) / openSPOT3 (2021) / openSPOT4 (2023)
  manuals, "DCS/XLX connector": local module D, URCALL CQCQCQ, module
  switch by the 8th URCALL character, "DCS and XLX servers usually do not
  need a valid D-STAR registration"; the connectors page only says the RX
  timeout must exceed the keepalive interval.
  https://manuals.sharkrf.com/openspot4/en/dcs-xlx-connector.html;
  **[BDV]** PA7LIM, "BlueDV Connect Q&A": non-XLX hosts go to the DCS, XRF
  or REF hosts file; no protocol detail. https://www.pa7lim.nl/bluedv-connect-qa/;
  **[PS]** Pi-Star `DCS_Hosts.txt` (http://www.pistar.uk/downloads/DCS_Hosts.txt,
  fetched 2026-10-30: "resolved from xreflector.net DNS" plus XLX hosts;
  tab separated name, address, optional lock flag `L`); **[WPSD]** WPSD
  manual "Zero to Hero: D-Star" (2026-08-01, DTMF `Dnnnzz`)
  https://manual.wpsd.radio/zero_hero/d-star/
- Not found: any DCS protocol document (xreflector.net, DG1HT/DL5DI,
  ircDDB wiki, OpenDV), the contents and sender of the `EEEE` and 35 B
  status packets, how the original reflectors refused an unregistered
  gateway, and any openSPOT / BlueDV / Pi-Star DCS timer value.

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference reflector (xlxd-ref); `src/cdcsprotocol.cpp`. This revision carries 2026 additions absent from the classic 2.x code: ACK jitter, progressive ignore of bad links, copied DCS packet tail, echo drop, DCS peering |
| URFd | https://github.com/n7tae/urfd @ `bd0c114` (2025-12-29) | XLX-family reflector; `reflector/DCSProtocol.cpp` |
| IG | https://github.com/g4klx/ircDDBGateway @ `f5ab9f7` (2026-02-18) | gateway: DCS client (behind Pi-Star and WPSD), and DCS server for links to its own repeaters (IG-S); `Common/DCSHandler.cpp`, `DCSProtocolHandler.cpp`, `ConnectData.cpp`, `PollData.cpp`, `AMBEData.cpp` |
| DSG | https://github.com/F4FXL/DStarGateway @ `7b75de6` (2026-10-28) | IG port; same DCS logic, sizes and timers |
| QN | https://github.com/n7tae/QnetGateway @ `39468e9` (2026-07-06) | gateway client (`QnetLink.cpp`) |
| g2 | https://github.com/KA8SCP/g2_link @ `99a8ccb` | Icom G3 gateway client QN derives from; same DCS logic as QN |
| DS | https://github.com/nostar/DroidStar @ `4ef18be` (2026-10-29) | application client (`dcs.cpp`) |

No other open DCS reflector exists: the original DCS server software was
never published. openSPOT and BlueDV are closed source.

## How the cases judge

`cases/proto/test_dcs.py` probes xlxd-ref and the system under test in
parallel; the Judged column of the testlist rows says how:

- **hb**: a spec MUST or SHOULD (Level column); asserted on both sides.
- **diff**: a convention the implementations agree on, or an XLX-family
  convention; sut == ref.
- **dev**: disputed between implementations, a convention without
  agreement, or an xlxd deviation from spec or clients. Under
  `DVT_SUT_PROFILE=xlxd` sut == ref (xlxd's behaviour is recorded as data);
  under another profile the profile's spec entry (`dvtest/specs/<profile>.py`,
  `"dcs"` dict `{"P-DCS-NN": {observed key: value}}`) decides, and the
  case skips with "spec pending" while there is none.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

Egress counter, sync position and last frame are also checked passively
by dvcheck (`dvtest/check/dcs.py`); deliberately malformed tester traffic
is bracketed with control-channel marks, so dvcheck reports it as a
warning and a run ends with exit 0 unless the system under test itself
sent something wrong.

xlxd guards against loops on every protocol: a MY callsign that opens
three short overs in a row (under 50 frames each, each within 2 s of the
previous end) gets its transmissions blocked for 3 s, 10 s, then 300 s,
and at the third strike its links are refused. The cases therefore talk
63 frames (three superframes) and give every short or malformed over its
own callsign (STRnnn from `rig/config/dmrid.dat`). xlxd also ignores a
source IP for 5, 30, then 60 min after three malformed links, so the suite
sends at most two per run (P-DCS-05). Each case waits 1.2 s after a link
before it talks (xlxd refreshes its per-module client list once a second).

Conventions: offsets are into the UDP payload, 0-based. "RPT2" is the
destination field (voice offset 7), "RPT1" the departure field (offset
15). The 2-byte stream id is opaque (IG reads it low byte first, DS high
byte first; servers copy the bytes). The reflector's name on the DCS wire
is `DCS` + its 3-character number: xlxd and URFd rename themselves to
`DCSnnn` for this protocol, IG renames an `XLXnnn` target to `DCSnnn`
before it links and compares. "Silently ignored" means no reply and no
state change.

## 1. Link and ACK

Packets: link 519 B = callsign (8, byte 7 normally a space) + client
module + reflector module + byte 10 (0x00; 0x0B from DS) + reflector name
(8, IG/DSG/QN) + an HTML status snippet, zero padded (IG/DSG: gateway
type image, type HOTSPOT/DONGLE/STARNET/REPEATER and software version; QN
a fixed one; DS leaves bytes 11-518 unset); ACK / NAK 14 B = callsign (7) +
space + client module + reflector module (NAK: requested module, or a
space in IG-S) + `ACK` / `NAK` + 0x00.

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-01 | A 519 B link to a served module from a valid callsign is answered with the 14 B ACK, sent from port 30051 to the link's source ip:port | convention (impl, agreed) | not stated (no DCS spec) | IG, DSG accept it only from the target ip:port 30051, with bytes 0-6 + 8 = their repeater; QN needs bytes 0-7 = owner, 8 = its module, 9 = the requested module, `ACK` at 10; DS needs exactly 14 B with `ACK` at 10 | URFd and IG-S use this layout | measured: 14 B ACK: N0TEST, space, J, J (061551Z) | as all |
| P-DCS-02 | ACK latency | disputed | not stated | DS sends its link once and never retries; IG, DSG resend after 1 s, backing off to 60 s; QN sends once but also takes the first 22 B poll as a link confirmation | URFd, IG-S answer at once | code: random 0-2 s; measured: 1.26 and 1.7 s (ref), 0.21 and 0.67 s (sut) (061551Z) | at once |
| P-DCS-03 | Bytes 10-518 of the link are not required: any byte 10, any or no reflector name, any text | convention (XLX family) | not stated | DS sends byte 10 = 0x0B and leaves 11-518 unset; IG, DSG, QN send a name and HTML | URFd ignores bytes 10-518; IG-S reads the target repeater from bytes 11-17 and NAKs without it | measured: DroidStar form ACKed (061551Z) | ignore bytes 10-518 (DS must link) |
| P-DCS-04 | A link to a module the reflector does not serve gets NAK and no link | convention (impl, agreed) | not stated | IG stops linking on NAK; QN clears the link; DS ignores NAK (stays "connecting") | URFd NAK; IG-S NAK for an unknown repeater or a full table | measured: module Z ACKed; every module A-Z is served on the rig, so no unserved module to probe (061551Z) | NAK at once |
| P-DCS-05 | A link with an invalid callsign or a non-letter module never links; reply | disputed | not stated | no client sends one | URFd: not a link (dropped as unknown); IG-S: NAK | measured: NAK for the invalid callsign and for module `1` (one try each) (061551Z); code: NAK for the first 3 per source IP, then that IP is ignored for 5, 30, 60 min, so the suite sends at most 2 per run | no link; NAK or silence; no IP-wide ban (gateways behind one NAT) |
| P-DCS-06 | A repeated link (same callsign, module, address): ACK again, one client, traffic once | disputed | not stated | IG resends its link after 1 s while xlxd's ACK may still be pending; QN, DS once | URFd re-ACKs, no duplicate; IG-S ignores the copy silently | measured: a second copy 50 ms after the first got no ACK of its own (1 ACK); one client, traffic once (061551Z) | ACK every copy, one client |
| P-DCS-07 | No registration is required by default; an operator list may refuse a callsign (NAK) | disputed | CCS7 (2015: a CCS7 id "will be required"; refusal form not stated); OS ("usually do not need a valid registration") | no client sends a registration token; the link carries only the callsign | URFd: operator white/black list; IG-S: none | measured: N0TEST (no registration) ACKed (061551Z) | open by default; an operator list refuses with NAK |
| P-DCS-08 | A link packet of another size (for example an 11 B DExtra-style link) does not link | convention (XLX family) | not stated | every client links with 519 B | URFd: exactly 519 B; IG-S: 519 B | measured: 11 B link not answered (061551Z) | as all |

## 2. Unlink

Packets: unlink 19 B (IG, DSG, QN) = callsign (8) + client module + space
+ 0x00 + reflector name (8); 11 B (DS) = callsign (8) + client module +
space + 0x00.

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-09 | An unlink (11 B or 19 B, byte 9 a space) removes the client and is answered with a 14 B NAK (byte 9 a space) | convention (XLX family) | not stated | IG, DSG stay "unlinking" and resend the 19 B unlink (backoff) until that NAK, else give up after 60 s; QN (2 or 5 copies) and DS (once) do not wait | URFd as xlxd; IG-S accepts only 19 B and sends no reply | measured: 11 B and 19 B unlink both answered with the 14 B NAK, byte 9 a space; 0 frames afterwards (061551Z) | accept both sizes; reply NAK (IG needs it) |
| P-DCS-10 | After the unlink no further voice reaches the client | removal: convention (impl, agreed); tail: B5 | not stated | a client that unlinked may have closed its socket | URFd, IG-S stop at once | measured: 0 frames after the unlink (061551Z) | stop at once (B5) |
| P-DCS-11 | An unlink from the client that is talking ends its stream and frees the module | disputed | not stated | IG sends its radio an end frame on a local unlink | URFd keeps a talking client (unlink lost) | measured: NAK sent, but the talker still heard the next stream (not removed); the module was free after the timeout (061551Z) | end the stream (terminator, B3), remove the client |
| P-DCS-12 | An unlink from an address that is not linked (for example after the server dropped the link) is answered with a NAK | disputed | not stated | IG, DSG resend their unlink until a NAK arrives, up to 60 s; QN, DS do not wait | URFd silent | measured: no reply (061551Z) | NAK (ends IG's unlink retries) |

## 3. Keepalive

Packets: server poll 22 B = reflector name (7) + the client's linked
module + space + client callsign (7) + client module + client module +
0A 00 20 20 (IG-S: spaces); xlxd and URFd also send a 9 B packet
(reflector name + 0x00) before each poll; client keepalive 17 B =
callsign (7) + client module + 0x00 + reflector name (7) + reflector module.

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-13 | The reflector sends each linked client the 22 B poll, at 5 s or faster | convention (impl, agreed) | not stated | IG, DSG never send a keepalive of their own on an outgoing link: they answer each matching poll with 17 B and restart their 60 s timer, so without polls they drop the link after 60 s; QN resets its ~50 s countdown on a poll; DS only counts it | URFd every 1 s; IG-S every 5 s | measured: a poll every 5.0 s, the first 6-7 s after the link (061551Z) | every 5 s or faster |
| P-DCS-14 | Poll fields: bytes 0-6 = `DCS` + number, byte 7 = the client's linked module, 9-15 the client callsign, 17 the client module | convention (impl, agreed) | not stated | IG, DSG need bytes 0-7 = the linked target (an `XLXnnn` target compared as `DCSnnn`) and 9-15 + 17 = their repeater; QN needs 0-6 = the reflector, 7 = linked module, 9-15 = owner, 17 its module (A-C); DS any 22 B | URFd and IG-S send the same fields | measured: `DCS999 ` + J + space + `N0TEST ` + J + J + 0A 00 20 20 (061551Z) | `DCSnnn` + linked module, client callsign + module |
| P-DCS-15 | The extra 9 B packet (reflector name + 0x00) before each poll | convention (XLX family) | not stated | IG, QN, DS drop it as an unknown size (harmless) | URFd sends it; IG-S does not | measured: one 9 B packet with every poll (061551Z) | optional; omit or keep |
| P-DCS-16 | A client keepalive (17 B) refreshes its link; the reflector does not reply to it | convention (impl, agreed) | not stated | QN, g2 send 17 B every 1 s unsolicited, already before the ACK, and take a 22 B poll as the ACK; DS every 2 s (its 17 B carries `name` + 0x00 + module, so byte 15 is 0x00 for a 6-character name); IG, DSG only in reply to a poll | URFd: callsign (first 7) + ip:port, no reply; IG-S: callsign + ip:port | measured: the gateway and the DroidStar 17 B forms kept the link past 70 s; no reply to them (061551Z, 062633Z) | refresh on callsign + ip:port; do not require bytes 9-16 to match exactly (DS) |
| P-DCS-17 | Keepalive sizes other than 17 B (9, 15, 19, 22 B) | disputed | not stated | no client sends them (DS sends 11 + name length bytes, 17 B for a 6-character name) | URFd takes 15, 17, 22; IG-S 17 only | measured: 9, 19 and 22 B forms also kept the link past 70 s (061551Z) | accept 17 B; other sizes optional |
| P-DCS-18 | A silent client is dropped after the timeout; what it is sent | drop: convention (impl, agreed); timeout and notice: disputed | not stated | IG, DSG drop the link after 60 s without a poll; QN after about 50 s; DS never; no client acts on a server's 19 B notice (IG accepts a 19 B unlink only when it names the reflector at 0-6 + 8) | URFd 30 s, sends a 19 B notice; IG-S 60 s, silent | measured: a silent client dropped within 70 s; no notice reached it (061551Z) | 60 s or more; notice optional (clients ignore it) |
| P-DCS-19 | A dropped client gets no further voice; a talking client is never dropped | convention (impl, agreed) | not stated | IG and QN count received voice as life | URFd, IG-S keep the talker; remove the silent client | measured: the dropped client heard nothing; a client talking for 70 s was not dropped (061551Z) | as all |
| P-DCS-20 | A keepalive from an address that is not linked creates no link and gets no traffic | convention (impl, agreed) | not stated | QN keepalives start before its ACK arrives | URFd, IG-S ignore it | measured: no poll, no voice (061551Z) | as all |

## 4. Malformed, status and unknown packets

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-21 | Unknown sizes (1 B, 3 B, 1400 B), `EEEE` status packets, 35 B packets and 15 B all-zero packets get no reply; the reflector keeps serving | convention (impl, agreed) | not stated (WCARA: DCS status was published over HTTP, not in-band) | IG, DSG, QN, g2 drop `EEEE` and 35 B as "status data"; DS shows a 35 B packet as a network text message | URFd dumps unknown packets and ignores 15 B zero packets; IG-S drops | measured: no reply to 1 B, 3 B, `EEEE`, 35 B, 15 B zeros, 1400 B; a link right after was ACKed (061551Z) | ignore silently |
| P-DCS-22 | Voice shorter than 100 B is dropped; longer than 100 B | short: convention (impl, agreed); long: disputed | not stated | all clients send exactly 100 B, and accept exactly 100 B | URFd accepts 100 B or more; IG-S exactly 100 B | measured: 99 B not relayed; 101 B relayed (42 of 42 frames) (061551Z) | accept exactly 100 B; longer optional |
| P-DCS-23 | Voice from an address that is not linked is not relayed | convention (impl, agreed) | not stated | clients link before talking | URFd drops it; IG-S needs a link | measured: not relayed (061551Z) | as all |

## 5. Voice packet (100 B)

Offsets: 0-3 `0001`, 4-6 flags 1-3, 7-14 RPT2, 15-22 RPT1, 23-30 UR, 31-38
MY, 39-42 MY suffix (the radio header without P_FCS), 43-44 stream id, 45
sequence (0..20, 0x40 on the last packet), 46-54 AMBE, 55-57 slow data,
58-60 packet counter (24 bit, low byte first), 61 0x01, 62 0x00, 63-99
text area (IG: 0x21 at 63 and a 20-character text at 64-83; others zero).

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-24 | A 100 B voice packet from a linked client opens a stream on that client's module at any sequence number (no separate header) | convention (impl, agreed); design: G4VXE (every packet carries the header) | G4VXE (join mid-over); JARL7 4.1.1 (header fields), 5.1 (3) c (sequence) | IG, QN, DS send the header fields in every packet | URFd opens a stream from any packet; IG-S from a matching RPT2 | measured: a stream starting at sequence 7 relayed in full (42 of 42) (061551Z) | as all |
| P-DCS-25 | Egress packets are 100 B with RPT2 = `DCS` + number + module; RPT1, UR, MY, suffix passed | RPT2: convention (XLX family); the rest: disputed | JARL7 4.1.1 f-j (field meaning) | IG, DSG accept only RPT2 = the linked target exactly (`DCSnnn M`); QN needs bytes 7-13 = the reflector and 14 = linked module; DS no check | URFd: `DCSnnn` + module in RPT2, rest passed; IG-S rebuilds | measured: 100 B, RPT2 `DCS999 J`, RPT1, UR, MY passed, an empty suffix sent as `DCS ` (061551Z) | RPT2 = `DCSnnn` + module; suffix unchanged |
| P-DCS-26 | Egress sequence: 0..20 in order, wrapping, 0x40 on the last packet; a stream sent to a listener starts at sequence 0 | counter: MUST (JARL7 5.1 (3) c); start at 0: convention (IG) | JARL7 5.1 (3) c (sequence numbers), 4.1.2 c | IG, DSG accept a new stream only on a packet with sequence 0 (a listener joining mid-superframe waits for the next 0); QN drops repeated sequence values; DS shows it | URFd writes its counter mod 21; IG-S passes it | measured: egress starts at 0, counts 0..20 twice, last packet 0x40 (061551Z) | as JARL; pass the source sequence |
| P-DCS-27 | Egress sequence 0 carries the sync 55 2D 16 in the slow-data bytes, no other packet does | MUST (JARL7 4.1.2 c/d) | JARL7 4.1.2 c/d (resync every 21 frames) | DS checks sequence 0 + sync to decode its text; IG passes the bytes to its radio | URFd, IG-S pass the ingress bytes | measured: sync at sequence 0 only (061551Z) | pass on same-mode; write on cross-mode |
| P-DCS-28 | AMBE (9 B) and slow data (3 B, scrambled) relayed unchanged on same-mode egress | convention (impl, agreed) | JARL7 4.1.2 b/e (data frame passed as given), Ap1.3 | DS decodes its 20-character text from frames 1-8; IG passes the bytes | URFd, IG-S pass them | measured: AMBE byte exact; the slow data of the final voice frame (before the last packet) replaced by 55 55 55 (061551Z) | as all |
| P-DCS-29 | A last packet (0x40) in any client's form ends the stream at once and reaches the listeners | MUST (JARL7 4.1.2 g, 5.1 (3) c) | JARL7 4.1.2 g/h, 5.1 (3) c | IG, QN, DS end RX on 0x40; the payload differs: IG 55 55 55 55 C8 7A + zeros with slow data 55 55 55, DS a plain AMBE frame (dc 8e 0a 40 ad ed ad 39 6e), xlxd's own 55 C8 7A + zeros / 25 1A C6 | agreed | measured: exactly one last packet, at the end (061551Z) | as all |
| P-DCS-30 | Payload of the egress last packet | disputed | JARL7 4.1.2 h (RF end pattern: 55 55 55 55 C8 7A) | IG sends 55 55 55 55 C8 7A + zeros as AMBE and 55 55 55 as slow data; DS a fixed non-pattern frame; all key on 0x40 only | URFd passes the ingress payload | measured: the JARL end pattern sent was replaced by 55 C8 7A + zeros / 25 1A C6 (061551Z) | pass the ingress payload; the JARL end pattern when the server synthesizes the end |
| P-DCS-31 | Packet counter (58-60) and text area (61-99) on egress | disputed | not stated | no client reads bytes 58-99; IG writes 0x21 + text at 63-83; QN, DS write zeros (DS no 0x21) | URFd: own counter from 0 per stream, 0x01 at 61, zeros after; IG-S writes 0x21 + text | measured: the source's bytes 61-99 passed through; the counter is not renumbered (egress values start at 8 and step unevenly: copied from the module's latest received packet) (061551Z, 063300Z) | own counter from 0 per stream, 0x01 at 61, rest free (text optional) |
| P-DCS-32 | Header fields in a voice packet with characters outside A-Z, 0-9, space in MY, RPT1 or RPT2 | disputed | JARL7 4.1.1 (ASCII callsign fields, space padded) | real callsigns only | URFd drops the packet; IG-S applies its MY white/black list | measured: an invalid MY, RPT1 or RPT2 drops the packet (nothing relayed) (061551Z) | drop invalid MY; RPT1/RPT2 are rewritten anyway |
| P-DCS-33 | Flag bytes 4-6 on egress | disputed | JARL7 4.1.1 c-e | IG, QN, DS send 00 00 00 and replace them on RF | URFd passes them | measured: flags 08 00 00 passed (061551Z) | pass unchanged |

## 6. Streams and routing

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-34 | A stream reaches every other client linked to the module once | convention (impl, agreed) | not stated | each listener needs exactly one copy | URFd: every non-talking client of the module; IG-S every incoming link of the repeater | measured: both listeners of the module got the stream once, the other module none (061551Z) | as all |
| P-DCS-35 | Module of a stream: the client's linked module or the RPT2 module (byte 14) | disputed | JARL7 4.1.1 f (RPT2 = destination) | every client writes its linked module at byte 14, so both rules agree in practice | URFd routes by byte 14 (a client can key any module); IG-S needs RPT2 = its repeater | measured: heard on the linked module, not on the RPT2 module (061551Z) | the linked module |
| P-DCS-36 | The talker never receives its own stream back | convention (impl, agreed); B9 | not stated | IG, QN drop an echo by stream id; DS would play it | URFd skips the talker; IG-S skips the feeding link | measured: 0 echo packets (061551Z) and C10 | B9 |
| P-DCS-37 | One stream id per transmission on egress, equal to the ingress id | constant: MUST (JARL7 5.1 (3) c); equal: B10 | JARL7 5.1 (3) c (one Call ID per PTT) | IG, QN key streams on the id | URFd keeps the id | measured: the ingress id on every packet (061551Z) | B10 |
| P-DCS-38 | A second talker on a busy module is not mixed in | convention (impl, agreed); policy: B1/B2 | not stated | IG accepts one stream id per link until 0x40 or 2 s | URFd drops the second stream | code: one stream per module, a late talker kept as pending late entry and promoted; not measured for DCS | B1/B2 |
| P-DCS-39 | A stream that stops without a last packet ends after a silence timeout; the module is free | convention (impl, agreed); B3 | not stated | IG, DSG end it after 2 s; DS after about 2 s | URFd 1.6 s | measured: one synthesized last packet; module free within 2.5 s (061551Z) | B3 (synthesized last packet) |
| P-DCS-40 | Voice after the last packet with the same stream id is not relayed | convention (impl, agreed) | not stated | a new over uses a new id | URFd: stream closed, next packet with that id opens a new one | measured: 10 frames with the ended id relayed (a new stream) (061551Z) | dev in practice: do not reopen within the silence timeout |
| P-DCS-46 | Ingress stream id 0 | disputed (conflicts with B8 for DV-Router) | JARL7 5.1 (3) c (random id; zero not excluded) | DS draws its id with a plain random 16-bit value, so 0 occurs (1 in 65536 overs); IG uses 0 as "no stream" | URFd refuses id 0 | measured: stream id 0 not relayed (061551Z) | accept (DS); B8 says reject: the lead decides which wins for DCS |
| P-DCS-47 | DCS, DPlus and DExtra talkers on one module reach each other with AMBE unchanged | convention (XLX family) | not stated | gateways mix protocols on one reflector module | URFd same | measured: byte exact (C10, test_clients2::test_dstar_family_exact) | as all |
| P-DCS-41 | A listener that links while a stream runs starts receiving it | convention (G4VXE design) | G4VXE (join mid-over) | IG waits for the next sequence 0 (at most 420 ms); QN, DS take any packet | URFd: next packet goes to the new client | measured: the late listener heard the rest of a 6 s stream (062141Z); a 3 s stream was missed by ref once (ACK jitter + 1 s client-list refresh) | send from the next packet |

## 7. Several clients and addressing

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-42 | Several links from one IP on different ports are separate clients | convention (impl, agreed) | not stated | IG links from a pool of ports 30052-30057 (30051 is its server socket); DSG from 30052 up; DS from an ephemeral port | URFd keys by ip:port + callsign + module | measured: two ports, both heard (061551Z) | as all |
| P-DCS-43 | Several links from one ip:port to different modules (QN links its modules A-C from one socket on 30051): voice to each, unlink and routing per link | disputed | not stated | QN keeps one socket for all its modules | URFd routes voice by RPT2 (correct) but an unlink removes the first match | measured: both modules heard; unlinking own module B removed the J link (first match) and kept K (061551Z), 062141Z | key by ip:port + client module (byte 8 of link/unlink, RPT1 module in voice) |
| P-DCS-44 | Packets to the client go from source port 30051 | convention (impl, agreed) | not stated | IG, QN accept server packets only from port 30051 | URFd, IG-S send from 30051 | measured: ACK and polls from 30051 (061551Z) | as all |

## 8. Reflector peers

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DCS-45 | DCS peering between reflectors (a `DCS` callsign links with a 519 B packet, kept alive with 9 + 22 B) | disputed | not stated | n/a | URFd has no DCS peering; IG-S serves only its repeaters | code: outbound and inbound DCS peers from the interlink list (10 s connect timeout, retry 5 s, 30 s keepalive timeout); not measured | spec decision (needs interlink rig configuration) |

## Where the recommendation differs from xlxd

| # | xlxd | Recommended for DV-Router | Why |
|---|---|---|---|
| P-DCS-02 | ACK/NAK after a random 0-2 s | at once | DS links once and never retries |
| P-DCS-05 | NAK, then the IP ignored up to 60 min | no IP-wide ban | gateways behind one NAT address |
| P-DCS-06 | a link copy during a pending ACK is dropped | ACK every copy | IG resends after 1 s; each copy should be answered |
| P-DCS-11 | unlink of the talker lost, stream runs on | end the stream, remove the client | the client asked to leave |
| P-DCS-25 | empty MY suffix filled with `DCS` | suffix unchanged | no client or other server writes it |
| P-DCS-30 | last packet payload 55 C8 7A + zeros / 25 1A C6 | pass the ingress payload; JARL end pattern when synthesized | xlxd's bytes are not the JARL end pattern |
| P-DCS-31 | bytes 58-99 of a DCS source copied per module (cached tail) | own counter per stream, rest free | a late talker's tail can overwrite the running stream's; no client reads it |
| P-DCS-43 | client found by ip:port first match | key by ip:port + module | QN links several modules from one socket |
| P-DCS-12 | no reply to an unlink from an unknown address | NAK | IG keeps resending its unlink for up to 60 s otherwise |
| P-DCS-46 | stream id 0 refused | accept | DS can draw 0 |

## Testlist rows

New K-subsection "DCS" for `docs/testlist.md` (the lead numbers it).

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DCS-01 | 519 B link to a served module -> 14 B ACK | test_dcs::test_dcs_link_ack | diff | probe + dvcheck | covered |
| P-DCS-02 | ACK latency (xlxd: random 0-2 s) | test_dcs::test_dcs_ack_latency | dev | probe | covered |
| P-DCS-03 | DroidStar link form (byte 10 0x0B, no name, no text) ACKed | test_dcs::test_dcs_link_minimal_body | diff | probe | covered |
| P-DCS-04 | Link to a module not served -> NAK | test_dcs::test_dcs_link_unserved_module_nak | diff | probe | covered (skips on this rig) |
| P-DCS-05 | Invalid callsign / non-letter module: no link; reply recorded | test_dcs::test_dcs_link_invalid[callsign\|module] | dev | probe | covered |
| P-DCS-06 | Repeated link: ACKs recorded, one client | test_dcs::test_dcs_relink_single_client | dev | probe | covered |
| P-DCS-07 | Unregistered callsign links | test_dcs::test_dcs_link_ack | diff | probe | covered |
| P-DCS-08 | 11 B link form does not link | test_dcs::test_dcs_link_wrong_size | diff | probe | covered |
| P-DCS-09 | Unlink 11 B / 19 B -> 14 B NAK (byte 9 space) | test_dcs::test_dcs_unlink_nak[11\|19] | diff | probe + dvcheck | covered |
| P-DCS-10 | No voice after unlink | test_dcs::test_dcs_unlink_nak[11\|19] | spec (B5) | probe | covered |
| P-DCS-11 | Unlink by the talker: removed? module free | test_dcs::test_dcs_unlink_while_talking | dev | probe | covered |
| P-DCS-12 | Unlink from a non-linked address: reply recorded | test_dcs::test_dcs_unlink_not_linked | dev | probe | covered |
| P-DCS-13 | 22 B poll at least every 5 s | test_dcs::test_dcs_server_poll | diff | probe + dvcheck | covered |
| P-DCS-14 | Poll fields | test_dcs::test_dcs_server_poll | diff | probe | covered |
| P-DCS-15 | Extra 9 B packet per poll | test_dcs::test_dcs_server_poll | dev | probe | covered |
| P-DCS-16 | 17 B keepalive (gateway, DroidStar form) keeps the link | test_dcs::test_dcs_keepalive_forms | diff (slow) | probe | covered |
| P-DCS-17 | Keepalive sizes 9 / 19 / 22 B | test_dcs::test_dcs_keepalive_forms | dev (slow) | probe | covered |
| P-DCS-18 | Silent client dropped; notice recorded | test_dcs::test_dcs_keepalive_forms | dev (slow) | probe | covered |
| P-DCS-19 | Dropped client gets no voice; talker never dropped | test_dcs::test_dcs_keepalive_forms | diff (slow) | probe | covered |
| P-DCS-20 | Keepalive from a non-linked address: no link | test_dcs::test_dcs_keepalive_without_link | diff | probe | covered |
| P-DCS-21 | Unknown / EEEE / 35 B / 15 B zero / oversize ignored | test_dcs::test_dcs_garbage_ignored[short1\|short3\|eeee\|status35\|zero15\|oversize] | diff | probe | covered |
| P-DCS-22 | Voice of 99 / 101 B | test_dcs::test_dcs_voice_size[99\|101] | dev | probe | covered |
| P-DCS-23 | Voice from a non-linked address not relayed | test_dcs::test_dcs_voice_not_linked | diff | probe | covered |
| P-DCS-24 | Stream opens at any sequence | test_dcs::test_dcs_stream_opens_mid_superframe | diff | probe | covered |
| P-DCS-25 | Egress 100 B, RPT2 DCSnnn + module; suffix recorded | test_dcs::test_dcs_egress_fields | diff + dev | probe + dvcheck | covered |
| P-DCS-26 | Egress sequence from 0, 0..20, 0x40 last | test_dcs::test_dcs_egress_sequence | hb | probe + dvcheck | covered |
| P-DCS-27 | Egress sync at sequence 0 only | test_dcs::test_dcs_egress_sequence | hb | probe + dvcheck | covered |
| P-DCS-28 | AMBE byte exact; changed slow data recorded | test_dcs::test_dcs_passthrough_exact | diff | probe | covered |
| P-DCS-29 | Last packet in each client form ends the stream at once | test_dcs::test_dcs_last_packet, ::test_dcs_end_forms[ircddb\|droidstar\|xlxd] | hb | probe + dvcheck | covered |
| P-DCS-30 | Egress last-packet payload | test_dcs::test_dcs_last_packet_payload | dev | probe | covered |
| P-DCS-31 | Egress counter and text area | test_dcs::test_dcs_counter_and_text | dev | probe | covered |
| P-DCS-32 | Invalid MY / RPT1 / RPT2 | test_dcs::test_dcs_invalid_callsign[my\|rpt1\|rpt2] | dev | probe | covered |
| P-DCS-33 | Flag bytes on egress | test_dcs::test_dcs_egress_fields | dev | probe | covered |
| P-DCS-34 | Stream to every other client of the module once | test_dcs::test_dcs_module_fanout | diff | probe | covered |
| P-DCS-35 | RPT2 module differs from the linked module | test_dcs::test_dcs_rpt2_other_module | dev | probe | covered |
| P-DCS-36 | No echo to the talker | test_dcs::test_dcs_last_packet | spec (B9) | probe | covered |
| P-DCS-37 | Stream id kept on every packet | test_dcs::test_dcs_last_packet | hb + spec (B10) | probe + dvcheck | covered |
| P-DCS-38 | Second talker not mixed in | test_floor::test_first_talker_wins, ::test_late_entry_denied | spec (B1/B2) | probe | covered elsewhere (no DCS cell) |
| P-DCS-39 | Lost last packet: synthesized last recorded, module free | test_dcs::test_dcs_stream_timeout | spec (B3) | probe | covered |
| P-DCS-40 | Packets with an ended stream id | test_dcs::test_dcs_voice_after_last | dev | probe + dvcheck | covered |
| P-DCS-41 | Late listener hears the rest | test_dcs::test_dcs_late_listener | diff | probe | covered |
| P-DCS-42 | Two ports on one IP | test_dcs::test_dcs_two_ports_one_ip | diff | probe | covered |
| P-DCS-43 | Two modules over one ip:port (QnetGateway) | test_dcs::test_dcs_two_modules_one_port | dev | probe | covered |
| P-DCS-44 | Server packets from port 30051 | test_dcs::test_dcs_source_port | diff | probe | covered |
| P-DCS-45 | DCS peering | - | dev | out of scope (interlink rig) | out of scope |
| P-DCS-46 | Ingress stream id 0 | test_dcs::test_dcs_stream_id_zero | dev | probe | covered |
| P-DCS-47 | DCS / DPlus / DExtra on one module byte exact | test_clients2::test_dstar_family_exact | diff | probe | covered (C10) |

## Cases and builders

Cases: `cases/proto/test_dcs.py` (46 cases), markers `proto` and `rig`;
`test_dcs_keepalive_forms` is also `slow` (one 70 s window covers
P-DCS-16..19 for every keepalive form at once). Client forms in
`dvtest/proto/dcs.py` (the older compact functions are unchanged):
`gateway_link`, `droidstar_link`, `raw_link`, `unlink19`, `unlink11`,
`keepalive17(form="gateway" | "droidstar")`, `parse_reply` (ACK, NAK,
22 B poll, 9 B packet, 19 B notice, voice), `voice_packet` and
`parse_raw` (every field raw), `stream_packets` (start sequence, last
packet payload) and the last-packet payloads `END_JARL` / `END_XLXD` /
`END_DROIDSTAR`.
Unit round trips: `cases/unit/test_dcs_conf.py`.
