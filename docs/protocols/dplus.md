<!-- SPDX-License-Identifier: MIT -->
# DPlus (REF, D-STAR): obligations of the reflector side

What a reflector speaking DPlus owes a DPlus **client** on UDP 20001. The
clients are hotspot and repeater gateways (ircDDBGateway, its port
DStarGateway, QnetGateway), apps (DroidStar, BlueDV) and the images built on
them (Pi-Star ships ircDDBGateway). Each item is numbered `P-DPL-NN`; the
planned cases are listed under "Testlist rows" at the end.

DPlus was written by AA4RC for the closed REF network and was never
published: no protocol document, no auth-server description, no DSVT spec
was found (searched: AA4RC/dstargateway.org/dstarusers.org/opendstar.org
pages and archives, OpenDV, groups.io, Wireshark). Only the **radio layer**
it carries (header fields and CRC, 20 ms frames, superframe sync, end
pattern) is specified, by JARL. So MUST / SHOULD appear only on radio-layer
rows; the UDP exchange is `convention (impl)` (naming who agrees) or
`disputed` (naming who does what). Where xlxd differs from what the clients
need, the recommendation follows the clients (owner direction 2026-10-30).

**Level**: MUST / SHOULD only where JARL says so; `convention (impl)` where
only implementations back it; `disputed` where servers or clients differ.

## Sources

Specs and published descriptions:

- **[JARL-6]** JARL, "Digitalization Technology Standard for Amateur Radio",
  Ver. 6.0a (English, revision history to 2019-01). Sections used: 4.1.1
  radio header (flags 1-3, RPT2 = destination repeater, RPT1 = departure
  repeater, UR, MY, MY suffix, P_FCS = CRC-CCITT x^16 + x^12 + x^5 + 1),
  4.1.2 voice frame (72-bit AMBE + 24-bit data every 20 ms; sync
  `55 2D 16` in every 21st data slot; last frame `55 55 55 55 C8 7A`),
  chapter 6 slow data, 5.1(3) one random call id per transmission, header
  repeated every 21 frames on the gateway link, Ap1 scrambler.
  https://www.jarl.org/English/7_dstar/STD6_0_E.pdf
- **[SHOGEN]** JARL, "D-STAR System" technical requirements, English
  translation (2005-08-23), 2.1.1 header, 2.1.2 voice and end pattern.
  https://www.qsl.net/kb9mwr/projects/dv/dstar/shogen.pdf (archive of the
  JARL original: http://web.archive.org/web/20260715044803/https://www.jarl.com/d-star/shogen.pdf)
- **[K7VE]** K7VE, "D-STAR for the Technically Curious", TAPR DCC 2010:
  DPlus developed by AA4RC, closed source.
  https://web.tapr.org/meetings/DCC_2010/DCC2010-D-STAR-technical-K7VE.pdf
- **[QNET-DOC]** QnetGateway `CONFIG+INSTALL` @ `39468e9`: REF reflectors
  need a registration at dstargateway.org; an unregistered system is
  unlinked when it transmits; the client cannot see whether its
  authorisation succeeded. https://github.com/n7tae/QnetGateway/blob/39468e9/CONFIG%2BINSTALL
- **[OPENSPOT]** SharkRF openSPOT2 manual, "REF/XRF connector": classic REF
  servers may allow only a read-only link without a D-STAR registration.
  https://manuals.sharkrf.com/openspot2/en/ref-xrf-connector.html
- **[PA7LIM]** BlueDV Connect Q&A: REF needs a US-Trust registration to
  transmit; "reflector is busy" refusals. https://www.pa7lim.nl/bluedv-connect-qa
- **[DSU]** dstarusers.org, "Adding a new D-STAR Gateway to the Network"
  (2013): dplus is required for reflectors.
  https://www.dstarusers.org/JoiningTheNetwork8.pdf
- Secondary, not normative: P. Loveall AE5PL, "D-STAR Uncovered" (2008)
  https://www.qsl.net/kb9mwr/projects/dv/dstar/DSTARUncovered.pdf ;
  F4FXL, "REF, XRF, DCS, XLX: what's the difference?" (2018, upd. 2024)
  https://f4fxl.org/ref-xrf-dcs-xlx-whats-the-difference ; Pi-Star uses
  ircDDBGateway https://www.pistar.uk and
  https://github.com/AndyTaylorTweet/Pi-Star_Binaries_sbin . An unverified
  forum snippet says a REF reply `OKRO` means read-only (unregistered).

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| IRC | https://github.com/g4klx/ircDDBGateway @ `f5ab9f7` (2026-02-18) | client (outgoing REF links); small server for incoming dongles; used by Pi-Star |
| DSG | https://github.com/F4FXL/DStarGateway @ `7b75de6` (2026-10-28) | client; DPlus code behaves as IRC |
| QNET | https://github.com/n7tae/QnetGateway @ `39468e9` (2026-07-06) | client (QnetLink) and dongle server |
| DROID | https://github.com/nostar/DroidStar @ `4ef18be` (2026-10-29) | client (app) |
| BlueDV | closed source, [PA7LIM] only | client |
| URFD | https://github.com/n7tae/urfd @ `bd0c114` (2025-12-29) | server (reflector) |
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference server (xlxd-ref); also a DPlus peer client |
| xlxd-old | https://github.com/LX3JL/xlxd @ `c3de5fa` (2021-10-30) | older xlxd, one routing comparison only |

## How the cases judge

`cases/proto/test_dplus.py` probes xlxd-ref and the system under
test in parallel, like `test_mmdvm.py`:

- **hb**: a JARL MUST; asserted on both sides.
- **diff**: a convention the servers agree on and the clients rely on;
  sut == ref.
- **dev**: disputed, or xlxd deviates from what the clients need. Under
  `DVT_SUT_PROFILE=xlxd` sut == ref (xlxd's behaviour is recorded as
  data); under another profile `SPEC["dplus"]["P-DPL-NN"]` decides, and the
  case skips with "spec pending" while there is none.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

The xlxd-ref column is measured on the rig, run `out/20261030T060947Z-i13`
(`scripts/run.sh --capture -m "proto and rig" -- cases/proto/test_dplus.py`,
29 cases passed with sut == ref, dvcheck 0 errors); "source" adds what the
xlxd source says beyond the probe.

xlxd loop guard (all protocols, found while writing these cases): when
the same MY callsign opens three short streams (under 50 frames) in a row,
each within 2 s of the previous close, xlxd blocks that callsign's
transmissions for 3 s, then 10 s, and at the third strike for 300 s with
its links refused and the client disconnected. Joins, kerchunks and test
bursts trip it; the cases therefore talk 63 frames or more and give every
transmitting peer of a probe its own callsign. Whether DV-Router does the
same is a behaviour question for the B rows, not a DPlus obligation.

dvcheck on that capture (WARN, also on the reference): xlxd sometimes
sends a just-joined listener voice before its first header (its client
list is refreshed once a second), so a client that starts only on a
header (ircDDBGateway, DroidStar) waits for the next repeat (P-DPL-25).

Byte offsets are 0-based into the UDP payload. DSVT header: 0-1 prefix
`3A 80`, 2-5 `DSVT`, 6 `10`, 10 `20`, 11-13 band bytes, 14-15 stream id,
16 `80`, 17-19 flags 1-3, 20-27 RPT2, 28-35 RPT1, 36-43 UR, 44-51 MY,
52-55 suffix, 56-57 CRC. The prefix is length (byte 0, low 8 bits) plus a
type in byte 1 (`80` DV, `C0` login family, `60` keepalive, `00`
connect/disconnect).

## 1. Connect and login

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DPL-01 | Connect `05 00 18 00 01` (5 B) is echoed byte for byte to the sender | convention (impl, agreed) | none | IRC/DSG send it and retry with backoff 1..60 s until a 5 B reply with byte 4 = 01; QNET sends once, needs the exact echo; DROID sends once, takes any 5 B packet starting `05` as the echo | URFD echoes to anyone (stateless); IRC/QNET dongle servers echo while below their dongle limit | measured: echoed byte for byte | echo to any sender, no state |
| P-DPL-02 | Replies go to the packet's source IP:port and leave from UDP 20001 | convention (impl, agreed) | none | QNET sends from 20001 and accepts replies only from source port 20001; IRC/DSG send from a pool 20002-20007; DROID from an ephemeral port | URFD, IRC, QNET reply to IP:port from 20001 | measured: from 20001 to the tester's ephemeral port | same |
| P-DPL-03 | Login `1C C0 04 00` + callsign field (16 B) + client id (8 B), 28 B, with a valid callsign is answered `08 C0 04 00 OKRW` | convention (impl, agreed) | none | IRC/DSG: callsign NUL padded at 4, `DV019999` at 20; QNET: 8 chars, spaces turned to NUL, `DV019999`; DROID: 6 chars only, then `HS` + 6 random digits; all need OKRW to go linked | URFD OKRW if allowed; IRC dongle server OKRW for any callsign; QNET dongle server OKRW after a format check | measured: OKRW to the IRC (`DV019999`), QNET and DROID (`HS314159`) logins | OKRW; never require `DV019999` (DROID) |
| P-DPL-04 | The login reply comes within a few seconds | convention (impl) | none | no client retries the login: IRC/DSG fail the link 30 s after it started; DROID and QNET wait without a timer | URFD, IRC, QNET reply at once | measured: 0.5-1.95 s for a new client (random 0-2 s by source), at once for a known IP:port | reply at once (judged: within 3 s on both) |
| P-DPL-05 | No reply other than `OKRW` or `BUSY` | convention (impl) | none | IRC/DSG accept only `OKRW`; any other 8 B reply (incl. `OKRO`) drops the link, even later while linked; QNET and DROID also accept `OKRO`; DROID `FAIL` = disconnect, `BUSY` = stays "connecting" | URFD OKRW/BUSY; QNET dongle server `FAIL` on a bad callsign | measured: only OKRW, nothing more while linked | OKRW or BUSY only |
| P-DPL-06 | A login the server refuses (policy, invalid callsign) is refused; what it is sent | disputed | none | see P-DPL-05: BUSY is the only refusal every client handles without a crash or hang (DROID stays connecting) | URFD BUSY for policy, silence for a malformed callsign; QNET `FAIL` | measured: BUSY to one malformed login; a valid login right after got OKRW (source: from the 4th, the IP is ignored 5 / 30 / 60 min) | BUSY per login; no IP-wide ban (several gateways share an IP behind NAT; IRC uses 6 ports) |
| P-DPL-07 | The server does not consult the US-Trust / dstargateway.org registration | convention (impl, agreed) | none published; [QNET-DOC], [OPENSPOT], [PA7LIM]: the closed REF network relays only registered callsigns | clients authenticate to auth.dstargateway.org themselves (TCP 20001, every 6 h in IRC) to fetch the REF list; they cannot tell whether it worked | URFD, IRC, QNET: no registration check, allow/deny lists only | measured: the unregistered test calls link | no registration check; local allow/deny policy |
| P-DPL-08 | A login without a preceding connect | disputed | none | every client sends connect first | URFD, xlxd stateless (login accepted); IRC dongle server needs the connect | measured: OKRW | accept (no client depends on refusal) |
| P-DPL-09 | A repeated login from a linked IP:port keeps one session; traffic arrives once | convention (impl) | none | DROID logs in again on any `05`-first 5 B packet | URFD refreshes the client | measured: OKRW again, every frame once | one session per IP:port |

## 2. Keepalive and close

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DPL-10 | The server sends keepalive `03 60 00` to every linked client about once a second | convention (impl, agreed period) | none | IRC/DSG drop the link 30 s after the last 3 B packet from the server; QNET after about 50 s; DROID has no timeout | URFD and IRC dongle server every 1 s; QNET about 1 s | measured: first 2.7-5.8 s after OKRW, then every 1.0 s | every 1 s from link (judged: first within 6 s, then at least one per 2 s) |
| P-DPL-11 | Client keepalives keep the link; the server does not answer them one for one | convention (impl, agreed) | none | IRC/DSG, QNET, DROID send `03 60 00` every 1 s after OKRW and never echo the server's | URFD, IRC, QNET: independent keepalives | measured: 3-4 server keepalives in 4 s while the client sent 16 | same |
| P-DPL-12 | A client silent past the timeout is dropped and sent `05 00 18 00 00`; then no traffic | drop and notice: convention (impl, agreed); timeout: disputed | none | IRC deletes the link quietly on it; QNET echoes and drops; DROID takes it as a connect echo and logs in again | URFD 10 s; IRC dongle server 10 s; QNET about 50 s | measured: dropped 31-33 s after the last client packet, disconnect sent, 0 frames after | 30 s or more (clients ping every 1 s; IRC itself waits 30 s) |
| P-DPL-13 | A client that is transmitting is not timed out for missing keepalives | convention (impl) | none | clients keep pinging while they talk | URFD keeps a transmitting client alive | measured: 36 s talk without one keepalive, not dropped, 1800/1800 frames | same |
| P-DPL-14 | Disconnect `05 00 18 00 00` from a linked client is echoed and ends the session (no further traffic) | convention (impl, agreed) | none | IRC/DSG send it twice and repeat it (1..60 s backoff) until it is echoed; QNET, DROID send once | URFD and IRC dongle server echo known clients; QNET dongle server removes without echo | measured: echoed, 0 frames after | echo and remove |
| P-DPL-15 | Disconnect from an IP:port that is not linked (e.g. after a server timeout) is echoed too | disputed | none | IRC/DSG resend the disconnect every 60 s forever until echoed | URFD and IRC echo known clients only | measured: no reply | echo it (stops IRC's endless retries) |

## 3. Header and voice (DSVT in DPlus framing)

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DPL-16 | A 58 B header opens a stream on the module in RPT2 byte 8 (offset 27) | module from RPT2: convention (impl, clients agree); RPT1 check: disputed | [JARL-6] 4.1.1 f-g: RPT2 = destination repeater, RPT1 = departure | all put the target module in RPT2 byte 8; RPT1 = login callsign + the hotspot's own module (IRC, QNET) or + the reflector module (DROID) | URFD: stream on the RPT2 module, RPT2 checked | measured: RPT1 module B, RPT2 P: relayed on P; RPT1 module space: header dropped | RPT2 only; ignore RPT1's module |
| P-DPL-17 | Header CRC is not checked on input | convention (impl, agreed) | [JARL-6] 4.1.1 k defines P_FCS on the air | all compute it on send; IRC ignores it on receive ("unreliable"), DROID ignores it, QNET recomputes | URFD passes it through | measured: relayed | accept any CRC |
| P-DPL-18 | Egress headers carry a correct CRC-CCITT | MUST (radio header) | [JARL-6] 4.1.1 k, [SHOGEN] 2.1.1 | clients relay the header to RF (DROID recomputes for its modem; IRC/QNET relay it) | URFD passes the input CRC through (0 for a cross-mode header) | measured: correct CRC on egress from DPlus and from DMR talkers | correct CRC on every egress header |
| P-DPL-19 | Egress RPT2 = `REF` + the reflector's number + module (e.g. `REF999 P` for XLX999) | convention (impl, clients require it) | none | clients drop a header whose RPT2 (or RPT1) is not the name + module they linked to (IRC exact 8 chars, QNET 7 chars + module, DROID normalised). The DPlus login carries no reflector name, so the server cannot know which name was used: Pi-Star's DPlus host list (https://www.pistar.uk/downloads/DPlus_Hosts.txt, 2026-10-30) lists XLX reflectors as `REFnnn` with the XLX number (sourced from the XLX API; 662 REF numbers >= 100); IRC's bundled `DPlus_Hosts.txt` holds only `REF` entries; IRC routes an `XLXnnn` host-list entry over DCS, then DExtra, and over DPlus only when both are off (then it expects `XLXnnn`); DSG links `XLX` names over DCS only | URFD rewrites RPT2 to its DPlus name | measured: `REF999 P` (XLX999 on DPlus) | `REF` + XLX number + module, as Pi-Star's list names it |
| P-DPL-20 | Egress header flags are 00 00 00 (a plain voice relay) | convention (impl) | [JARL-6] 4.1.1 flag bits | IRC sets flags to 0 on relay; QNET relays only flag 1 in {00, 08, 20, 28} | URFD passes through | measured: 00 00 00; RPT1 = the talker's RPT1 (DPlus), `STR001 B` = callsign + B (DMR source); UR `CQCQCQ` | 00 00 00 |
| P-DPL-21 | 29 B voice frames are relayed with AMBE and slow data unchanged, seq mod 21 | frame: MUST; unchanged relay: convention (impl, agreed) | [JARL-6] 4.1.2 b (9 B AMBE + 3 B data per 20 ms) | all send `1D 80` + DSVT voice | URFD relays unchanged | measured: 63/63 byte exact | unchanged |
| P-DPL-22 | Egress seq 0 frames carry the sync `55 2D 16` (also for streams from other modes) | MUST | [JARL-6] 4.1.2 c-d | IRC/DROID rely on it for slow-data framing | URFD relays the source bytes | measured: sync on every seq 0 frame of a DMR-sourced stream, first seq 0 | sync on every seq 0 frame |
| P-DPL-23 | A 32 B end frame (`20 80`, seq or 0x40) closes the stream, whatever its tail bytes | convention (impl, agreed) | [JARL-6] 4.1.2 g-h (end pattern) | IRC: null AMBE + `55 55 55 55 C8 7A` at 26-31; DROID: zero AMBE + the pattern; QNET: the pattern at 26-31 | URFD accepts 32 B with byte 0 = 20 | measured: IRC, DROID and xlxd end forms all close at once; a second talker 0.5 s later is heard | accept; key on the 0x40 bit |
| P-DPL-24 | Egress end frame is 32 B with the 0x40 bit and the JARL end pattern `55 55 55 55 C8 7A` at 26-31 | pattern: MUST (radio last frame); clients check only the 0x40 bit and the length | [JARL-6] 4.1.2 g-h | IRC, QNET, DROID send the pattern at 26-31; they detect the end by the 0x40 bit (IRC, QNET) or 32 B length (DROID) | URFD: 32 B but byte 0 = `1D` and a different tail | measured: byte 8 = 81, tail `55 C8 7A 55 x9 25 1A C6` from DPlus and DMR sources; pattern not at 26-31 | `20 80`, 0x40 bit, pattern at 26-31 |
| P-DPL-25 | The egress header is repeated once per superframe (21 frames) so a late listener can start | convention (impl, agreed); JARL describes it for the gateway link | [JARL-6] 5.1(3): header repeated every 21 frames on the gateway link | IRC and DROID start a stream only on a header and drop voice of an unknown id | URFD after every frame with counter % 21 = 20 (counter per client) | measured: header before frames 0, 21, 42, 63, 84 | once per superframe |
| P-DPL-26 | Repeated headers from a talker (IRC/QNET send 5 at the start, DROID one per superframe) neither restart nor duplicate the stream | convention (impl, agreed) | none | as stated | URFD keys on stream id | measured: one stream, every frame once | same |
| P-DPL-27 | One nonzero stream id per egress stream; voice and end carry the header's id | MUST one id per transmission | [JARL-6] 5.1(3) | IRC/DROID accept voice only with the accepted header's id; IRC treats id 0 as none | URFD keeps the source id | measured: one id across header, voice and end, kept from the talker | keep the source id (B10), never 0 |
| P-DPL-28 | The talker's own IP:port gets nothing of its stream | convention (impl, agreed) | none | clients would replay their own audio | URFD skips the transmitting client | measured: 0 echo frames | no echo (B9) |
| P-DPL-29 | A stream whose end frame is lost is closed after a timeout and the module freed | spec (B3) | none | IRC ends a received stream after 2 s silence; DROID after about 1 s | URFD own timeout | measured: end frame sent after the 1.6 s timeout; module free 2.2 s after the last frame | B3 |

## 4. Who hears what

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DPL-30 | A client that has linked but not yet transmitted hears the module it linked to | disputed | none (the login carries no module) | all clients filter received headers by RPT2/RPT1 = reflector + linked module, i.e. they expect the server to send at least that module; QNET links several local modules over one login and needs each one's traffic | URFD and xlxd-old send every module to every client and let it filter | measured: 0 frames until the client has transmitted | send every module (the clients filter), or at least deliver before the first transmission |
| P-DPL-31 | After transmitting on module M the client hears module M | convention (impl, agreed) | none | as P-DPL-30 | URFD: all modules anyway | measured: 63/63 frames | same |
| P-DPL-32 | Each IP:port is its own client (several gateways behind one NAT address, IRC's port pool) | convention (impl) | none | IRC uses 20002-20007 for parallel links | URFD keys on IP:port; QNET dongle server on IP only | measured: both links hear 63/63 | IP:port |
| P-DPL-33 | DPlus, DExtra and DCS talkers on one module reach each other with AMBE unchanged | convention (XLX family) | none | gateways relay between their own links unchanged | URFD same | C10: byte exact | same |

## 5. Malformed and unknown packets

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DPL-34 | Unknown, short or oversize packets (also QNET's `04 C0 0x 00` status requests) get no reply; the server keeps serving | convention (impl, agreed) | none | QNET's dongle server answers status requests; no client sends them to a reflector | URFD ignores | measured: no reply to `XXXX`, 1-2 B, 1400 B, `04 C0 03 00`, 57 B header, 28 B voice; connect still echoed | ignore silently |
| P-DPL-35 | DV packets from an IP:port that has not logged in are not relayed | convention (impl, agreed) | none | clients always log in first | URFD needs a client | measured: 0 frames | drop |
| P-DPL-36 | A header with a malformed MY / RPT callsign is dropped | disputed | [JARL-6] 4.2.1 call sign rules | clients send valid callsigns | URFD to measure | measured: header with MY `N0-CALL` dropped | drop malformed MY only |

## Testlist rows

For docs/testlist.md section K (merged by the lead). All in
`cases/proto/test_dplus.py`, markers `proto` + `rig`.

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DPL-01 | Connect echoed byte for byte | test_dplus::test_dplus_connect_echo | diff | probe | covered |
| P-DPL-02 | Replies to source IP:port from 20001 | test_dplus::test_dplus_reply_source_port | diff | probe | covered |
| P-DPL-03 | Login -> OKRW; DV019999 and HSnnnnnn ids | test_dplus::test_dplus_login_okrw | diff | probe | covered |
| P-DPL-04 | Login reply within 3 s | test_dplus::test_dplus_login_reply_time | diff | probe | covered |
| P-DPL-05 | Only OKRW / BUSY replies | test_dplus::test_dplus_login_reply_set | diff | probe | covered |
| P-DPL-06 | Malformed-callsign login refused (one try only) | test_dplus::test_dplus_login_invalid_callsign | dev | probe | covered |
| P-DPL-07 | No registration check (unregistered test call links) | test_dplus::test_dplus_login_okrw | diff | probe | covered |
| P-DPL-08 | Login without connect | test_dplus::test_dplus_login_without_connect | dev | probe | covered |
| P-DPL-09 | Re-login keeps one session | test_dplus::test_dplus_relogin_one_session | diff | probe | covered |
| P-DPL-10 | Server keepalive period | test_dplus::test_dplus_server_keepalive | diff | probe | covered |
| P-DPL-11 | Client keepalive not answered one for one | test_dplus::test_dplus_server_keepalive | diff | probe | covered |
| P-DPL-12 | Silent client dropped, disconnect sent | test_dplus::test_dplus_timeout_drop | dev | probe | covered |
| P-DPL-13 | Transmitting client not timed out | test_dplus::test_dplus_talker_kept_alive | diff | probe (36 s) | covered |
| P-DPL-14 | Disconnect echoed, no further traffic | test_dplus::test_dplus_disconnect | diff | probe | covered |
| P-DPL-15 | Disconnect from an unknown IP:port echoed | test_dplus::test_dplus_disconnect_unknown | dev | probe | covered |
| P-DPL-16 | Module from RPT2; RPT1 module ignored | test_dplus::test_dplus_module_from_rpt2 | dev | probe | covered |
| P-DPL-17 | Bad header CRC accepted | test_dplus::test_dplus_header_crc_ignored | diff | probe | covered |
| P-DPL-18 | Egress header CRC correct | test_dplus::test_dplus_egress_header | hb | probe + dvcheck | covered |
| P-DPL-19 | Egress RPT2 = REF + XLX number + module | test_dplus::test_dplus_egress_header | diff | probe | covered |
| P-DPL-20 | Egress flags 00 00 00 | test_dplus::test_dplus_egress_header | diff | probe | covered |
| P-DPL-21 | Voice relayed unchanged | test_dplus::test_dplus_passthrough | diff | probe | covered |
| P-DPL-22 | Sync on seq 0 (also cross-mode) | test_dplus::test_dplus_egress_sync | hb | probe + dvcheck | covered |
| P-DPL-23 | Client end forms (IRC, DROID, QNET) close the stream | test_dplus::test_dplus_end_forms | diff | probe | covered |
| P-DPL-24 | Egress end frame form | test_dplus::test_dplus_egress_end | dev | probe + dvcheck | covered |
| P-DPL-25 | Header repeated per superframe; late listener starts | test_dplus::test_dplus_late_listener | diff | probe | covered |
| P-DPL-26 | Repeated talker headers: one stream | test_dplus::test_dplus_repeated_headers | diff | probe | covered |
| P-DPL-27 | One stream id per stream, voice matches | test_dplus::test_dplus_passthrough | diff | probe + dvcheck | covered |
| P-DPL-28 | No echo to the talker | test_dplus::test_dplus_passthrough | diff | probe | covered |
| P-DPL-29 | Lost end frame: stream closed | test_dplus::test_dplus_lost_end | spec (B3) | probe | covered |
| P-DPL-30 | Linked, never transmitted: hears its module | test_dplus::test_dplus_listen_before_transmit | dev | probe | covered |
| P-DPL-31 | After transmitting: hears the module | test_dplus::test_dplus_listen_after_transmit | diff | probe | covered |
| P-DPL-32 | Two clients on one IP, different ports | test_dplus::test_dplus_two_ports_one_ip | diff | probe | covered |
| P-DPL-33 | DPlus <-> DExtra / DCS byte exact | test_clients2::test_dstar_family_exact | diff | probe | covered |
| P-DPL-34 | Unknown / short / oversize ignored | test_dplus::test_dplus_malformed_ignored | diff | probe | covered |
| P-DPL-35 | DV from a non-client not relayed | test_dplus::test_dplus_dv_without_login | diff | probe | covered |
| P-DPL-36 | Malformed MY dropped | test_dplus::test_dplus_header_bad_my | dev | probe | covered |
