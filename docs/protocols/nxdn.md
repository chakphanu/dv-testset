<!-- SPDX-License-Identifier: MIT -->
# NXDN reflector network: obligations of a peer and of a server

NXDN reflectors talk UDP (port 41400 by convention). A system can take
part in two ways:

- **Peer** (sections 1-3): it joins an NXDNReflector talkgroup and polls
  it like a repeater would. The reflector relays between the peer and the
  real clients (NXDNGateway behind MMDVMHost, DroidStar, MMDVM_CM
  NXDN2DMR). xlxd works only this way, and so does DV-Router's
  `dv-edge-nxdn` today. Here the "client side" contract has two parts:
  - what the reflector accepts from a peer;
  - what the gateways and radios behind it accept in the payload the peer
    originates (an NXDN air-interface call carried in 33 bytes).
- **Server** (section 4): it is the reflector, and NXDNGateway and
  DroidStar link to it directly, as NXDNReflector and URFd do. DV-Router
  will also serve this way (owner decision 2026-10-30). xlxd has no
  server, so section 4 compares against NXDNReflector as the reference.

Each item is numbered `P-NXD-NN`. The cases are in
`cases/proto/test_nxdn_peer.py` (sections 1-3) and
`cases/proto/test_nxdn_server.py` (section 4). They are not named
`test_nxdn.py`: that basename is taken by `cases/test_nxdn.py`, and
pytest cannot collect two files of the same name without
`__init__.py`. Their testlist rows are at the end of this file.

Every row says where an obligation comes from, in separate columns:
- the spec text (version and section);
- what the clients and the other implementations do (project at a
  commit, behaviour in our own words, no code copied);
- what xlxd-ref does.

Measured on the rig on 2026-10-30:
- the xlxd column: run `20261030T062727Z-i8`, all 45 cases passed, sut ==
  ref on every row, both xlxd built from LX3JL xlxd `a978f03`;
- section 4: the same run, NXDNReflector on both sides;
- dvcheck: 0 errors (the probes run inside dvcheck mark windows).

**Level** follows the same rules as `mmdvm.md`:
- MUST / SHOULD only where a spec says so;
- `convention (impl)` where only implementations back it, naming the ones
  that agree;
- `disputed` where they differ, naming who does what.

The NXDNReflector protocol has no published spec (see Sources), so every
network-framing row is a convention. Air-interface rows cite the NXDN
Technical Specifications.

## Sources

Specs. Four-level clause numbers are written `6.4.1/6`, i.e. sub-clause
6 of 6.4.1. Read: v1.3 of TS 1-A and 1-B. The NXDN Forum lists v2.0
(2016-07) as current for both; it is behind the Forum's guest form and
was not read. The cited clauses are to be checked against v2.0 when it
is available.

- **[1-A]** NXDN Technical Specifications Part 1: Air Interface,
  Sub-part A: Common Air Interface (NXDN TS 1-A), Version 1.3, 2011-11,
  NXDN Forum (JVC KENWOOD, Icom).
  - Public mirror: https://www.qsl.net/k/kb9mwr/projects/dv/nxdn/NXDN-TS-1-A_v0103.pdf
  - Current version: https://www.nxdn-forum.com/download_file/
- **[1-B]** NXDN TS 1-B, Basic Operation, Version 1.3, 2011-11, same
  mirror (`NXDN-TS-1-B_v0103.pdf`).
- **[FORUM]** NXDN Forum, "Introduction NXDN" (IWCE 2019) and "NXDN
  Industry Update" (IWCE 2017), https://www.nxdn-forum.com/resources/data/:
  - "IP network specification is manufacturer specific";
  - no public NXDN standard covers repeater or system IP links.
- The G4KLX NXDNReflector protocol (`NXDNP`, `NXDNU`, `NXDND`) has no
  published description. Its reference is the code below. The NXDNClients
  README says only that the gateway "speaks the same language as Icom
  repeaters" (the Icom framing is reverse-engineered).

Implementations read (shallow clones; behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| REF | https://github.com/g4klx/NXDNClients @ `3bc2a14` (2023-02-22), `NXDNReflector/` | the reflector the peer links to; removed upstream in `486106a` (2023-02-23), maintained as https://github.com/nostar/DVReflectors @ `8b10868` (2026-03-10), same protocol behaviour (only a log buffer differs) |
| GW | https://github.com/g4klx/NXDNClients @ `b4b041d` (2026-10-25), `NXDNGateway/` | client: links MMDVMHost (Icom or Kenwood framing on its RF side) to reflectors |
| MH | https://github.com/g4klx/MMDVMHost @ `9384d68` (2026-10-18), NXDN files | the repeater/hotspot that puts the payload on air (behind GW) |
| DS | https://github.com/nostar/DroidStar @ `4ef18be` (2026-10-29) | client: an app talking to the reflector directly |
| CM | https://github.com/juribeparada/MMDVM_CM @ `7328c9e` (2024-09-07) | NXDN2DMR (reflector client or pseudo-reflector), DMR2NXDN / YSF2NXDN (Icom framing to GW) |
| URFd | https://github.com/n7tae/urfd @ `bd0c114` (2025-12-29) | another multi-mode server; NXDN as a reflector server (port 41400), no peer mode |
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference peer (xlxd-ref): `cnxdnprotocol`, `cnxdnutils` |
| PS / WPSD | Pi-Star_DV_Dash `ca50804`, Pi-Star_Binaries_sbin `3c73612` (2026-10-22); WPSD-WebCode `1189868`, WPSD-Scripts `ed33e9f` (2026-10) | hotspot images: run GW with Icom framing to MH, Startup moved to Static, hang times 0 (MMDVMHost mode hang), hosts from pistar.uk / w0chp host files |

## How the cases judge

The rig (profile `nxdn`) puts an NXDNReflector between the tester and
each xlxd: nxdn-ref peered with xlxd-ref, nxdn-sut with xlxd-sut. The
tester joins as a gateway (`dvtest.proto.nxdn.NxdnClient`).

REF relays any 43-byte group packet whose destination is its TG, and
does not look at the payload. So payload-level ingress probes (a header
missing, repeated, stolen halves, data flags) reach the peer unchanged
through it.

REF filters three things before the peer sees them:
- private calls;
- destinations other than its TG;
- the peer's own polls.

The rows that need them (marked "tester-as-reflector") use a second
interlink row that points each xlxd at a tester socket: see "Tester as
the reflector" at the end.

Judging, as in `test_mmdvm.py`:
- **hb**: a spec MUST, or what every client requires; asserted on both
  sides.
- **diff**: implementations agree; sut == ref.
- **dev**: disputed, or xlxd deviates:
  - under `DVT_SUT_PROFILE=xlxd`, sut == ref;
  - under another profile, `SPEC["nxdn"]["P-NXD-NN"]` decides;
  - "spec pending" skips.
- **spec (Bn)**: the DV-Router behaviour rows of the testlist.

Conventions: ids and the TG are big endian. "Payload" is the 33 bytes
after the 10-byte NXDND header:
- LICH at byte 0;
- SACCH at bytes 1-4;
- block 1 at bytes 5-18 and block 2 at bytes 19-32, each two 49-bit
  AMBE+2 frames at bits 0 and 49, or one FACCH1 (10 bytes layer 3 +
  CRC-12 + 4 zero bits).

## 1. Link and keepalive (peer -> reflector)

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers / peers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-NXD-01 | Poll `NXDNP` + 10-byte callsign (upper case, space padded) + TG (BE16), 17 B; the TG is the reflector's TG | convention (impl, agreed) | none (no published spec) | GW, CM NXDN2DMR, DS send exactly this (DS does not truncate a callsign over 10 chars); REF answers only a poll whose TG equals its own, echoing the 17 B unchanged, and keys the peer by IP and port | URFd (as server) ignores the TG | measured: `XLX999` padded to 10, TG of the row, 17 B | as xlxd |
| P-NXD-02 | Poll every 5 s while linked; never let 120 s pass without one | 120 s: convention (REF expiry); 5 s: convention (impl, agreed by GW, CM; DS 1 s) | none | GW 5 s (3 polls at link time); CM 5 s (3 at start); DS 1 s after the first echo | REF drops a repeater after 120 s without poll or relayed data; URFd drops after 30 s | measured: 5 s (largest gap 5.0 s) | 5 s |
| P-NXD-03 | A reflector that stops echoing: keep polling, relink by itself once it answers again; traffic resumes | disputed | none | GW and DS have no loss detection (keep polling / do nothing); CM NXDN2DMR keeps polling | URFd n/a (server) | measured: 7 polls in 40 s without echo (keeps polling); traffic from the reflector not routed after 30 s without an echo; routed again 5.4 s after echoes returned | keep polling every 5 s; resume within one poll period after the echo returns (roadmap: relink within 10 s) |
| P-NXD-04 | `NXDNU` + callsign + TG (17 B) when leaving the TG (row removed, shutdown) | convention (impl: GW, CM, DS send it; xlxd never) | none | GW 3x on TG change and hang expiry (not at shutdown); CM 3x on exit; DS 1x on disconnect; REF removes the sender at once | - | measured: no NXDNU when xlxd restarts | send 3x on row removal and edge shutdown (dev; REF otherwise keeps relaying to a dead address for 120 s) |

## 2. Egress: what the peer originates (peer -> reflector -> gateways -> radios)

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers / peers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-NXD-05 | Every packet is 43 B `NXDND`, flags bit 0x01 (group), dst = the TG | dst == TG and group: convention (REF relays nothing else; GW forwards nothing else) | none | GW forwards only group packets from the linked TG's address with dst == TG; DS accepts any 43 B; MH does not check dst | URFd sends dst = its ReflectorID (not the client's TG); CM dst = TG | measured: 17/17 packets 43 B, group, dst = TG | as xlxd (hb) |
| P-NXD-06 | Flags match the payload: header 0x05 (LICH 0x81/0x83 + FACCH1 type VCALL), trailer 0x09 (TX_REL), voice 0x01, never 0x02 for voice | convention (impl, agreed: GW, REF and MH derive the same bits from the payload) | [1-A] 6.4.5 (message types VCALL 0x01, TX_REL 0x08) | GW sets flags from LICH and byte 5; DS, CM, URFd write 0x05 / 0x01 / 0x09 | agreed | measured: flags match the payload on every packet; header first, trailer last | as xlxd (hb) |
| P-NXD-07 | A call starts with one header frame: LICH RDCH, SACCH non-superframe, both halves FACCH1, direction inbound (0x81); SACCH single (SR 00) carrying IDLE; FACCH1 = VCALL with valid CRC-12 in both blocks | form: MUST ([1-B] first frame); IDLE in the header SACCH: disputed | [1-B] 4.1.3 fig. 4.1-7 (frame 1: single SACCH IDLE + 2 x FACCH1 VCALL); [1-A] 5.2.1 table 5.2-1 (LICH), 4.5.2/2 (FACCH1, CRC-12 X^12+X^11+X^3+X^2+X+1, preset ones), 6.4.1/1 (VCALL) | MH starts a network call on a FCT-0 frame whose FACCH1 type is VCALL (no CRC check); MH's own header SACCH carries IDLE (0x10); DS sends IDLE with RAN 1 and no FACCH CRC; GW passes it on | URFd copies DS; CM LICH 0x81, SACCH single idle | measured: LICH 0x81, FACCH1 VCALL in both blocks, CRC-12 good; single SACCH with message type 0 (not IDLE 0x10), RAN 0 | header per spec, SACCH carries IDLE (dev) |
| P-NXD-08 | VCALL fields: call type 001 (conference = group call), CC option 0, src = the talker's unit id, dst = TG, cipher 00 | MUST ([1-B] conventional group call settings) | [1-A] 6.4.1/1, 6.5.11-6.5.13, 6.5.27; [1-B] 8.1.1/2 | MH and GW read call type bit 5 of octet 2 as group; DS always sets 0x20 | URFd, CM 0x20 | measured: call type 001, CC 0, cipher 0, src 1001, dst TG | as xlxd (hb) |
| P-NXD-09 | Voice frames: LICH RDCH, SACCH superframe, no steal, inbound (0xAC); four 49-bit AMBE+2 frames per 80 ms frame | MUST (frame structure) | [1-A] 4.4.2/2 (4 x VCH per 4800 bps frame), 5.2.1, 7.1 (EHR 2450 bps voice in 49 bits per 20 ms) | MH rebuilds the 72-bit VCH from the 49 bits; DS, CM pack frames at bits 0 and 49 of each block | URFd, CM 0xAC | measured: 15/15 voice packets 0xAC, 4 frames each | as xlxd (hb) |
| P-NXD-10 | The voice SACCH superframe carries VCALL 18 bits per frame, structure 1/4, 2/4, 3/4, 4/4 in order starting with the first voice frame, valid CRC-6 | MUST (spec superframe); start at 1/4: convention (MH late entry waits for all four in order) | [1-A] 6.3 table 6.3-2 (SR), 6.2.3/1, 4.5.2/1 (CRC-6 X^6+X^5+X^2+X+1, preset ones); [1-B] 4.1.3, 4.1.6 (late entry by collecting the 4 parts) | MH late entry needs 1/4..4/4 in order; DS starts at 2/4 (deviates) | URFd, CM start at 1/4 | measured: 1/4..4/4 repeating from the first voice packet, CRC-6 good, parts rebuild VCALL (src, dst) | as xlxd (hb) |
| P-NXD-11 | The call ends with exactly one trailer frame: LICH 0x81, FACCH1 TX_REL (CRC-12) in both blocks, flags 0x09 | MUST ([1-B] last frame TX_REL); one: convention | [1-B] 4.1.3 fig. 4.1-7; [1-A] 6.4.1/6 (TX_REL) | REF ends the transmission on flag 0x08; MH ends on TX_REL; DS sends one | URFd one (src possibly wrong); CM one | measured: one trailer, last, TX_REL, CRC-12 good | as xlxd (hb) |
| P-NXD-12 | Voice frames are the source's 49 data bits: 72 -> 49 drops the Golay parity and the PRNG whitening, no error correction, no other change | conversion: convention (impl, agreed; the VCH coding is in DVSI documents, not the NXDN spec) | [1-A] 4.5.2 and 7.1 (VCH coding deferred to DVSI); test vectors [1-A] 7.2.1/1, 7.2.1/2 | MH, DS, CM, URFd convert the same way, no gain | none apply gain | measured: 60/60 frames each way identical outside the 5 gain-index bits; DMR FEC valid | disputed with D1: see P-NXD-13 |
| P-NXD-13 | No gain step on AMBE+2 parameters in either direction | disputed (xlxd only) | none (no spec defines a network gain) | MH, GW, DS: none | URFd, CM: none | measured: b2 - 1 on 58/58 frames with b2 > 0, both directions (-0.3 dB); not on a partial last packet | spec + clients + other servers: no gain. DV-Router decision D1 currently copies xlxd; owner decision needed |
| P-NXD-14 | A last group of fewer than 4 frames is padded to 4 with AMBE+2 silence, not dropped | convention (impl: CM pads with silence; xlxd pads); disputed (DS, URFd drop a partial group) | [1-A] 7.2.1/2 (EHR silence vector: 72-bit B9E8815261 73002A6B, 49-bit F801A99F8CE08) | DS drops the partial group (up to 60 ms lost) | URFd drops it; CM pads | measured: 9 frames -> 12; the 3 pad frames equal the spec silence frame; the real frame of that packet has no gain step | pad with the spec silence frame |
| P-NXD-15 | One voice packet per 80 ms frame, paced at source rate (no bursts of several packets) | convention (impl, agreed); air timing [1-A] 4.3 | [1-A] 4.3, 4.4 (80 ms frame at 4800 bps) | MH plays network frames at air rate (buffered); DS TX every 4 audio frames | CM every > 75 ms; URFd as frames arrive | measured: from DMR (one burst per 60 ms) mean 79-80 ms, gaps 50-120 ms, no bursts | as xlxd (diff) |
| P-NXD-16 | src for a talker from another mode: NXDN id from the NXDN id table by callsign; unknown -> a fixed id | lookup: convention (impl, agreed); fallback: disputed | [1-A] 6.5.3 table 6.5-2 (unit id 0 = null, 0001-FFEF standard, FFFF all units) | GW and its voice announcements use 9999 as a service id | CM falls back to the last 5 digits of the DMR id, else 65519; URFd and DS fall back to 0 (null id, not valid as a source) | measured: N0CALL -> 1001; a DMR talker without an NXDN id -> 9999 | lookup, else 9999 (as xlxd; never 0) |
| P-NXD-30 | An empty stream from another mode (header + terminator, no voice: a DMR join, a D-STAR kerchunk) is not sent to NXDN as a VCALL + TX_REL call | convention (impl: xlxd sends none of it to DMR listeners, P-DMR-33) | [1-B] 4.1.3 (a call is VCALL, voice, TX_REL; an empty call keys the radios) | MH transmits a VCALL + TX_REL pair as a short over | URFd, CM: not checked | measured: the DMR join reaches NXDN as header + trailer | drop empty streams (dev) |
| P-NXD-17 | The peer never sends a stream back to the reflector it came from (no echo); reflector traffic is relayed to the peer's other protocols only | convention (impl, agreed) | none | REF never relays to the sender; GW does not loop | URFd skips the streaming client | measured: 0 packets back to the gateway | as xlxd (diff) |

## 3. Ingress: what the peer accepts from the reflector

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers / peers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-NXD-18 | Accept only 17 B `NXDNP` echoes and 43 B `NXDND` from the reflector's address; anything else is ignored and the link stays | convention (impl, agreed) | none | GW and CM take only `NXDN` + 17/43 B; DS takes any 17/43 B | URFd similar | measured: 5 garbage packets (non-NXDN, 42 B, 44 B, wrong-TG poll, unknown tag) ignored; the next stream routed whole | as xlxd (diff) |
| P-NXD-19 | A group header (flags 0x04, FACCH1 VCALL) opens a stream on the peered module; the following voice is routed; the trailer (0x08, TX_REL) ends it at once | MUST (call start/end, [1-B] 4.1.3) | [1-B] 4.1.3 | MH, DS act on header and trailer | URFd misclassifies the header and trailer (LICH compared with 0) and ends by timeout | measured: 60/60 frames, one DMR header and terminator; terminator 181 ms after the trailer | as xlxd (hb) |
| P-NXD-20 | Voice without a header (missed first frame) joins late once the SACCH superframe has delivered VCALL 1/4..4/4 | SHOULD (late entry, [1-B] 4.1.6) | [1-B] 4.1.6; [1-A] 2.2.12 | MH waits for the four SACCH parts and then synthesizes the header; DS plays at once | URFd opens on the first packet; CM builds a header | measured: 0 frames (no late entry) | late entry after the 4 SACCH parts (dev) |
| P-NXD-21 | A repeated header inside a running call does not split or restart it | convention (impl: MH and xlxd agree) | [1-B] 4.1.1 (FACCH1 may be used in-call) | MH ignores repeated VCALL; DS treats a header as a new stream | URFd n/a (see 19) | measured: 60/60 frames, one DMR stream (the second VCALL changes nothing) | as xlxd (diff) |
| P-NXD-22 | Repeated trailers (radios and gateways may send several) end the call once; the next call opens normally | convention (impl, agreed) | [1-B] 4.1.3 (one TX_REL from an SU; a repeater may add its own) | MH ends on the first; DS sets END again | CM pads and ends again | measured: 3 trailers then a new call: 2 DMR streams, 2 terminators, all frames | as xlxd (diff) |
| P-NXD-23 | A frame whose LICH steals a half for FACCH1 (option 01 / 10, LICH 0xA4/0xA8) delivers only its two real voice frames; the FACCH1 half is not played as audio | MUST (the stolen half is FACCH1, not VCH) | [1-A] 5.2.1 table 5.2-1 (steal flag), [1-B] 4.1.1 | MH decodes per LICH option; DS decodes all four as voice | CM accepts voice only with no steal; URFd decodes all four | measured: the FACCH1 half comes out as voice frames (60 frames for 60 slots) | treat the stolen half as signalling (substitute silence) (dev) |
| P-NXD-24 | Data frames (flags 0x02, LICH 0x9X) open no voice stream | MUST (UDCH is data, [1-A] 5.2.1) | [1-A] 5.2.1 (FCT UDCH), [1-B] data call | MH handles data separately; GW forwards data | CM, URFd do not handle data | measured: 0 DMR packets | as xlxd (hb) |
| P-NXD-25 | Private calls and destinations other than the TG are not routed into the module (tester-as-reflector; REF never relays them) | convention (impl: GW drops both; REF drops both) | [1-A] 6.5.12 (call type 100 individual) | GW forwards only group dst == TG | URFd does not check | measured: private 0 frames; group call to another TG routed (21 frames) | drop private and dst != TG (dev for dst) |
| P-NXD-26 | Encrypted calls (VCALL cipher type != 00) are not transcoded as clear audio | convention (none of the clients decrypt) | [1-A] 6.5.27 (cipher type), [1-D] security | MH passes them; DS plays them (garbled) | not checked by others | measured: routed (21 frames) | drop or pass unchanged NXDN-to-NXDN only (dev; record) |
| P-NXD-27 | Two NXDN talkers at once through the reflector (REF does not enforce a single source: it relays both) give one winner and no mixing; the second talker's src is not merged into the first stream | convention (impl, floor rules; judged by B1/B2) | none | GW forwards both (same reflector address); MH plays the first call and drops VCALL during it | URFd shares one stream id for all NXDN clients | measured: one DMR stream carrying 60 frames of the first talker and 38 of the second (mixed) | one stream per src id from one address; floor per B1 (spec) |
| P-NXD-28 | A call whose trailer is lost ends after the silence timeout; the module is free and every egress gets a terminator | convention; timeout judged by B3 | [1-B] 4.1.3 (TX_REL); REF watchdog 1.5 s | MH network watchdog 1.5 s; DS 0.5 s | URFd ends by the 1.6 s stream timeout | measured: DMR terminator 0.7-0.8 s after the last voice; the next call routed | per B3 (spec) |
| P-NXD-29 | The src id of an ingress call maps to a callsign from the NXDN id table for the other modes (D-STAR MY, DMR src); an unknown id gets a stable fallback | convention (impl, agreed for lookup) | none | GW and REF look up NXDN.csv for logs only | URFd uses its NXDN id database; CM maps to DMR ids | measured: 1001 -> MY N0CALL, DMR src 1023007; unknown 4242 -> MY N04242, DMR src 0 | as xlxd (diff) |

## 4. Server: obligations toward gateways (NXDNGateway, DroidStar)

The server is the reflector itself. Clients poll it, it relays between
them on a talkgroup, and its egress payload follows section 2 (P-NXD-05
to P-NXD-11, P-NXD-14 to P-NXD-16).

`cases/proto/test_nxdn_server.py` plays several gateways against
`DVT_NXDN_SUT` (the sut's server endpoint) and against `DVT_NXDN_REF`.
`DVT_NXDN_REF` is an NXDNReflector, the reference server; xlxd has no
server. Under the `xlxd` profile both addresses are the rig's
NXDNReflectors, so the REF column was measured on a G4KLX reflector
(nostar/DVReflectors behaviour) against itself.

Judging:
- **hb**: what every client needs; asserted on both sides.
- **conv**: the clients' convention; asserted on the sut, ref recorded.
- **dev**: disputed; under another profile, `SPEC["nxdn"]` decides.

Under any profile other than `xlxd`, every server case skips with "edge
not ready" until `SPEC["nxdn"]["server"]` is True.

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-NXD-31 | A poll `NXDNP` + callsign + TG (17 B) for a TG the server carries gets the identical 17 bytes back, once per poll; this registers the gateway (keyed by IP and port) | convention (impl, agreed); the echo: needed by DS | none | GW and CM poll every 5 s and ignore the echo; DS treats the first 17 B reply as "connected" and polls every 1 s after it (no reply: never connects) | REF echoes only its TG and keys by IP and port; URFd echoes any TG | n/a (no server); REF measured: identical echo, one reply | echo identically (hb) |
| P-NXD-32 | A poll for a TG the server does not carry | disputed | none | GW finds the server through a hosts entry "TG host port", so one port may serve many TGs; DS shows "connecting" with no reply | REF: no reply, not registered; URFd: echo, and every TG goes to its auto-link module | n/a; REF measured: no reply | one TG per room from the config; an unknown TG gets no reply (dev) |
| P-NXD-33 | A group call (dst = TG) from a registered gateway reaches every other gateway on that TG: header, all frames, trailer; dst = the TG that gateway linked to, group set, src kept | convention (impl: REF relays unchanged); dst = linked TG: needed by GW | [1-B] 4.1.3 (call sequence) | GW forwards to MMDVMHost only group packets with dst == its TG from the linked reflector's address; DS accepts any 43 B | REF relays the bytes unchanged; URFd rebuilds the packets with dst = its ReflectorID (GW drops them unless the TG equals that id) | n/a; REF measured: two listeners got header, 40/40 frames exact, trailer, dst TG, src kept | as REF; same-mode frames unchanged (hb) |
| P-NXD-34 | `NXDNU` + callsign + TG removes the gateway at once; no reply | convention (impl, agreed) | none | GW sends 3 on TG change and hang expiry; DS 1 on disconnect | REF removes by address and TG; URFd by IP only | n/a; REF measured: no reply, 0 packets after the unlink | as REF (conv) |
| P-NXD-35 | NXDND from an address that never polled is not relayed | convention (impl, agreed) | none | - | REF drops it and logs "unknown source"; URFd needs a client entry | n/a; REF measured: 0 packets relayed | as REF (hb) |
| P-NXD-36 | The talking gateway gets nothing of its own call back | convention (impl, agreed) | none | GW and DS would replay it | REF skips the sender address; URFd skips the streaming client | n/a; REF measured: 0 | as REF (hb, in P-NXD-33) |
| P-NXD-37 | Individual calls, and group calls to another TG, from a registered gateway are not relayed | convention (REF) | [1-A] 6.5.12 | GW sends whatever its RF side sends; GW drops dst != TG on receive | REF drops both; URFd checks neither | n/a; REF measured: not relayed | as REF (conv) |
| P-NXD-38 | Two gateways talk at once: one winner, no mixing | disputed; judged by B1 | none | MH plays the first call and drops VCALL during it, but takes voice from both | REF relays both (it does not enforce one source among repeaters); URFd shares one stream id for all NXDN clients | n/a; REF measured: listener got both talkers' voice (mixed) | B1: first talker wins (dev) |
| P-NXD-39 | A call without a trailer ends after a silence timeout; listeners get a trailer; the next call is relayed | trailer: disputed; judged by B3 | [1-B] 4.1.3 | MH watchdog 1.5 s, DS 0.5 s (both cope without a trailer) | REF: watchdog 1.5 s, sends nothing; URFd: stream timeout 1.6 s | n/a; REF measured: no trailer; next call relayed | synthesize TX_REL at the timeout (B3, dev) |
| P-NXD-40 | Non-NXDN, short, long and unknown-tag packets get no reply; the gateway stays registered | convention (impl, agreed) | none | - | REF reads only "NXDN" + 17 or 43 B; URFd similar | n/a; REF measured: no reply, still relayed | as REF (conv) |
| P-NXD-41 | The server sends no polls of its own and does not relay one gateway's polls to another | convention (impl, agreed) | none | GW and DS poll; neither expects server polls | REF, URFd: none | n/a; REF measured: 0 packets to an idle gateway in 11 s | as REF (conv) |
| P-NXD-42 | Data frames (flags 0x02, LICH 0x9X) from a gateway | disputed | [1-A] 5.2.1 (UDCH) | MH sends and plays NXDN data; GW forwards it | REF relays it unchanged (group, dst = TG); URFd parses it as voice | n/a; REF measured: relayed | relay NXDN-to-NXDN unchanged, never into other modes (dev) |
| P-NXD-43 | A gateway that stops polling is dropped after a timeout, and then gets no traffic | timeout: disputed | none | GW polls every 5 s, CM every 5 s, DS every 1 s | REF 120 s; URFd 30 s | n/a; REF measured: still served after 35 s, dropped by 130 s | 120 s, as REF (dev) |

## Recommendations that differ from xlxd

| # | xlxd | Recommended | Why |
|---|---|---|---|
| P-NXD-04 | never sends NXDNU | 3x NXDNU on row removal and shutdown | GW, CM, DS all unlink; REF otherwise relays to a dead address for 120 s |
| P-NXD-07 | header SACCH has 18 zero bits | SACCH IDLE (message type 0x10) | [1-B] 4.1.3 frame 1; MH and DS send IDLE |
| P-NXD-13 | -0.3 dB gain-index step both ways | no gain | no spec, no client and no other server changes the bits; conflicts with DV-Router D1 (owner decision) |
| P-NXD-20 | headerless voice dropped | late entry after the 4 SACCH parts | [1-B] 4.1.6; MH does it |
| P-NXD-23 | stolen FACCH1 half decoded as voice | silence for the stolen half | [1-A] 5.2.1; MH, CM |
| P-NXD-25 | dst not checked | drop dst != TG | GW, REF |
| P-NXD-30 | an empty DMR stream (the join) goes out as VCALL + TX_REL | drop empty streams | consistent with P-DMR-33; avoids a kerchunk on every NXDN radio |
| P-NXD-26 | cipher not read | record; candidate: no clear transcode of encrypted calls | [1-A] 6.5.27 |
| P-NXD-27 | streams keyed by reflector address (two talkers interfere) | key by (address, src id), floor per B1 | REF relays both talkers |

## Tester as the reflector (rig)

Rows P-NXD-01..04, 18 and 25 use a second interlink row on both xlxd,
`NX<tg> <tester>:<port> <module>`, so the tester is that row's reflector
(`dvtest.proto.nxdn.FakeReflector`). With it the tester can:
- answer or withhold poll echoes;
- time the polls;
- send private, wrong-dst, oversized and non-`NXDN` packets that REF
  would filter.

The rig has it (profile nxdn): `NX31673 <tester>:41410 I` on xlxd-ref and
`NX31673 <tester>:41411 I` on xlxd-sut (`DVT_NXDN_PROBE_*` in
`rig/compose.yml`). Everything else runs through the NXDNReflectors.

## Testlist rows

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-NXD-01 | Poll format, TG = reflector TG, echo | test_nxdn_peer::test_nxdn_poll_format | diff | probe (tester-as-reflector) + dvcheck | covered |
| P-NXD-02 | Poll period 5 s, never 120 s silent | test_nxdn_peer::test_nxdn_poll_period | diff | probe (tester-as-reflector) + dvcheck | covered |
| P-NXD-03 | Reflector stops echoing: keep polling, resume after it answers | test_nxdn_peer::test_nxdn_reflector_loss_relink | dev | probe (tester-as-reflector) | covered (slow) |
| P-NXD-04 | NXDNU on leave | test_nxdn_peer::test_nxdn_unlink_on_leave | dev | probe (rig_control restart of xlxd) | covered (slow) |
| P-NXD-05 | 43 B, group, dst = TG on every packet | test_nxdn_peer::test_nxdn_egress_framing | hb | probe + dvcheck | covered |
| P-NXD-06 | Flags match the payload (0x05 / 0x01 / 0x09) | test_nxdn_peer::test_nxdn_egress_flags | hb | probe + dvcheck | covered |
| P-NXD-07 | Header frame: LICH 0x81, FACCH1 VCALL + CRC-12 both blocks; SACCH IDLE | test_nxdn_peer::test_nxdn_egress_header | hb + dev | probe + dvcheck | covered |
| P-NXD-08 | VCALL fields: group, src, dst, no cipher | test_nxdn_peer::test_nxdn_egress_vcall_fields | hb | probe | covered |
| P-NXD-09 | Voice frames LICH 0xAC, 4 x 49-bit | test_nxdn_peer::test_nxdn_egress_voice | hb | probe + dvcheck | covered |
| P-NXD-10 | SACCH superframe VCALL 1/4..4/4 from the first voice frame, CRC-6 | test_nxdn_peer::test_nxdn_egress_sacch | hb | probe + dvcheck | covered |
| P-NXD-11 | Exactly one TX_REL trailer | test_nxdn_peer::test_nxdn_egress_trailer | hb | probe + dvcheck | covered |
| P-NXD-12 | 72 <-> 49 conversion bit-exact outside the gain bits | test_nxdn_peer::test_nxdn_frame_conversion | diff | probe | covered |
| P-NXD-13 | Gain step on AMBE+2 parameters | test_nxdn_peer::test_nxdn_gain_step | dev | probe | covered |
| P-NXD-14 | Partial last group padded with the spec silence frame | test_nxdn_peer::test_nxdn_partial_group_padded | diff | probe | covered |
| P-NXD-15 | One packet per 80 ms, no bursts | test_nxdn_peer::test_nxdn_egress_pacing | diff | probe | covered |
| P-NXD-16 | src from the id table, unknown -> 9999 | test_nxdn_peer::test_nxdn_egress_src_id[known\|unknown] | diff | probe | covered |
| P-NXD-17 | No echo to the reflector | test_nxdn_peer::test_nxdn_no_echo | diff | probe | covered |
| P-NXD-30 | Empty stream from another mode not sent to NXDN | test_nxdn_peer::test_nxdn_empty_stream_not_relayed | dev | probe | covered |
| P-NXD-18 | Non-NXDN / wrong-size packets ignored, link kept | test_nxdn_peer::test_nxdn_garbage_ignored | diff | probe (tester-as-reflector) | covered |
| P-NXD-19 | Header opens, trailer ends the stream at once | test_nxdn_peer::test_nxdn_ingress_header_trailer | hb | probe | covered |
| P-NXD-20 | Late entry from the SACCH superframe | test_nxdn_peer::test_nxdn_ingress_late_entry | dev | probe | covered |
| P-NXD-21 | Repeated header inside a call does not split it | test_nxdn_peer::test_nxdn_ingress_repeated_header | diff | probe | covered |
| P-NXD-22 | Repeated trailers end once; next call opens | test_nxdn_peer::test_nxdn_ingress_repeated_trailer | diff | probe | covered |
| P-NXD-23 | FACCH1-stolen half not played as voice | test_nxdn_peer::test_nxdn_ingress_steal | dev | probe | covered |
| P-NXD-24 | Data frames open no voice stream | test_nxdn_peer::test_nxdn_ingress_data | hb | probe | covered |
| P-NXD-25 | Private / dst != TG not routed | test_nxdn_peer::test_nxdn_ingress_private_or_other_tg[private\|other_tg] | dev | probe (tester-as-reflector) | covered |
| P-NXD-26 | Encrypted call handling recorded | test_nxdn_peer::test_nxdn_ingress_encrypted | dev | probe | covered |
| P-NXD-27 | Two talkers through the reflector: one winner, no mixing | test_nxdn_peer::test_nxdn_two_talkers | spec (B1) | probe | covered |
| P-NXD-28 | Lost trailer: timeout frees the module, terminators sent | test_nxdn_peer::test_nxdn_lost_trailer_timeout | spec (B3) | probe | covered |
| P-NXD-29 | Ingress src id -> callsign for other modes | test_nxdn_peer::test_nxdn_ingress_src_mapping | diff | probe | covered |
| P-NXD-31 | Poll echoed identically; registers the gateway | test_nxdn_server::test_nxdn_server_poll_echo | hb | probe | covered |
| P-NXD-32 | Poll for a TG the server does not carry | test_nxdn_server::test_nxdn_server_poll_other_tg | dev | probe | covered |
| P-NXD-33 | Group call relayed to every other gateway; dst = TG, frames unchanged; no echo (P-NXD-36) | test_nxdn_server::test_nxdn_server_relay | hb | probe + dvcheck | covered |
| P-NXD-34 | NXDNU removes the gateway, no reply | test_nxdn_server::test_nxdn_server_unlink | conv | probe | covered |
| P-NXD-35 | NXDND from an unregistered address not relayed | test_nxdn_server::test_nxdn_server_unknown_source | hb | probe | covered |
| P-NXD-36 | No echo to the talking gateway | test_nxdn_server::test_nxdn_server_relay | hb | probe | covered |
| P-NXD-37 | Individual / other-TG calls not relayed | test_nxdn_server::test_nxdn_server_private_or_other_tg[private\|other_tg] | conv | probe | covered |
| P-NXD-38 | Two talkers: one winner, no mixing | test_nxdn_server::test_nxdn_server_two_talkers | spec (B1) | probe | covered |
| P-NXD-39 | Lost trailer: trailer synthesized, next call relayed | test_nxdn_server::test_nxdn_server_lost_trailer | spec (B3) | probe | covered |
| P-NXD-40 | Garbage ignored, gateway stays | test_nxdn_server::test_nxdn_server_garbage_ignored | conv | probe | covered |
| P-NXD-41 | No server polls, polls not relayed | test_nxdn_server::test_nxdn_server_no_unsolicited | conv | probe | covered |
| P-NXD-42 | Data frames relayed or dropped | test_nxdn_server::test_nxdn_server_data_frames | dev | probe | covered |
| P-NXD-43 | Silent gateway dropped after the timeout | test_nxdn_server::test_nxdn_server_client_expiry | dev | probe | covered (slow) |
