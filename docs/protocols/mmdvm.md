<!-- SPDX-License-Identifier: MIT -->
# MMDVM Homebrew / DMR: obligations of the master (reflector) side

What a reflector acting as an MMDVM Homebrew **master** owes an MMDVM
Homebrew client on UDP 62030. DV-Router serves real clients, so the
client side is the contract: DMRGateway (XLX mode and DMR network mode;
MMDVMHost dropped its own Homebrew client in 2023 and talks to
DMRGateway), the MMDVM_CM cross-mode tools (YSF2DMR, NXDN2DMR, ...),
DroidStar, HBlink3 / FreeDMR in peer mode, openSPOT and BlueDV. Each item
is numbered `P-DMR-NN`; the conformance cases are in `docs/testlist.md`
section K1.

Every row keeps its sources in separate columns: the spec text (with
version and section), what the clients send, expect and tolerate, what
the other masters do (project at a commit, behaviour in our own words, no
code copied), what xlxd-ref did on the rig (measured 2026-10-30; all 51
cases passed with sut == ref; P-DMR-43 added later that day, 2 cases
passed), and the recommendation for DV-Router.

**Level**: MUST / SHOULD only where a spec says so; `convention (impl)`
where only implementations back it, naming the ones that agree;
`disputed` where masters differ, naming who does what. `convention (XLX
family)` marks routing rules that xlxd and URFd share and the XLX clients
rely on; the TG-bridge masters (HBlink, FreeDMR) route by their own rules
and are listed for completeness. The client research of 2026-10-30
changed no Level.

**Recommended for DV-Router** follows one priority: spec + client
consensus, then the other servers, then xlxd. An xlxd deviation that no
client needs is never recommended.

## Sources

Specs and published notes (four-level ETSI clause numbers are written
`5.1.2/3`, i.e. sub-clause 3 of 5.1.2, so the PII scanner does not read
them as IPv4 addresses):

- **[ETSI-1]** ETSI TS 102 361-1 V2.7.1 (2026-05), DMR Air Interface
  protocol. https://www.etsi.org/deliver/etsi_ts/102300_102399/10236101/02.07.01_60/ts_10236101v020701p.pdf
- **[ETSI-2]** ETSI TS 102 361-2 V2.5.1 (2023-05), DMR voice and generic
  services and facilities. https://www.etsi.org/deliver/etsi_ts/102300_102399/10236102/02.05.01_60/ts_10236102v020501p.pdf
- **[ETSI-3]** ETSI TS 102 361-3 V1.3.1 (2017-10), DMR packet data
  protocol. https://www.etsi.org/deliver/etsi_ts/102300_102399/10236103/01.03.01_60/ts_10236103v010301p.pdf
- **[HB15]** "IPSC Protocol Specs for homebrew DMR repeater" (file title
  "DMRplus IPSC Protocol for HB repeater"), DL5DI, G4KLX, DG1HT, revision
  2015-07-26 (the last one; 7 pages, no clause numbers, cited by page
  heading). https://wiki.brandmeister.network/images/5/54/DMRplus_IPSC_Protocol_for_HB_repeater.pdf
  (behind a bot check; archived copy
  https://web.archive.org/web/20250315074550/https://wiki.brandmeister.network/images/5/54/DMRplus_IPSC_Protocol_for_HB_repeater.pdf).
  It describes the DMR+ variant: `MSTACK` (not `RPTACK`), `MSTPING` /
  `RPTPONG`, a 53-byte `DMRD` without BER/RSSI, no `RPTO`, 32-bit values in
  "on air / ETSI" (big endian) order. No spec of the MMDVM variant
  (`RPTACK`, `RPTPING`/`MSTPONG`, 55-byte `DMRD`, `RPTO`) was published; its
  reference is G4KLX's code.
- **[BMW]** BrandMeister Wiki, "Homebrew repeater protocol", revision
  oldid 18983, last edited 2026-01-06: two variants exist (DL5DI spec vs
  G4KLX MMDVMHost, "alienated" over time); BM corrections (port 62030, ping
  every 5-15 s, 306-byte text-form config) and extensions `RPTSBKN`,
  `RPTRSSI`, `RPTINTR` (no acknowledge).
  https://wiki.brandmeister.network/index.php/Homebrew_repeater_protocol
- Secondary, not normative: HBlink4 `docs/protocol.md` (the MMDVM variant
  as HBlink4 implements it); OK-DMR `homebrew-2015.md` @ `bb3f1c8e`
  (2021-11-05, a transcription of HB15 that differs from the PDF in
  places); MMDVMHost `DMRplus_startup_options.md` (RPTO option syntax).

Clients (shallow clones or published manuals; behaviour described, not
copied):

| Key | Source @ commit / version | What was read |
|---|---|---|
| GW | https://github.com/g4klx/DMRGateway @ `d751e20e` (2026-10-22) | `DMRNetwork.cpp` (login state machine, timers, receive filter; used for the XLX link and every DMR Network N), `DMRGateway.cpp` (`linkXLX`, `writeXLXLink`, `getConfig`, XLX receive path, `CRewriteTG` slot 2 TG 9 rule), `UDPSocket.cpp`, `DMRGateway.ini` |
| MH | https://github.com/g4klx/MMDVMHost @ `9384d683` (2026-10-18) | no Homebrew client since `391e382a` (2023-06-05): `DMRNetwork.cpp` talks to DMRGateway with `DMRD`/`DMRP`/`DMRB` and no login; `DMRSlot.cpp` (network late entry, RF late entry sends a synthesised header) |
| CM | https://github.com/juribeparada/MMDVM_CM @ `7328c9e9` (2024-09-07) | `YSF2DMR`, `NXDN2DMR`, `M172DMR`, `P252DMR`, `USRP2DMR` `DMRNetwork.cpp` (two copies of the pre-2023 MMDVMHost client), `DelayBuffer.cpp`, `YSF2DMR.cpp` (XLX link, TG unlink), `YSF2DMR.ini`, `Makefile` (asserts compiled in). DMR2YSF / DMR2NXDN are masters towards MMDVMHost, not clients |
| DS | https://github.com/nostar/DroidStar @ `4ef18beb` (2026-10-29) | `dmr.cpp` / `dmr.h` (login, ping, MSTNAK/MSTCL reconnect, receive and transmit), `droidstar.cpp` (DMRHosts.txt, default TG 4000, 10 s reconnect) |
| HB3p | HB3 @ `1db27305` peer side: `hblink.py` (MODE PEER / XLXPEER), `hblink-SAMPLE.cfg` | id checks on RPTACK / MSTPONG / MSTNAK / MSTCL unless `LOOSE`; XLXPEER link bursts |
| FDp | FDMR @ `546165eb` peer side: `hblink.py` | same peer logic as HB3p |
| PS | https://github.com/AndyTaylorTweet/Pi-Star_DV_Dash @ `ca50804e` (2026-10-22) | `admin/configure.php`: DMRGateway `[XLX Network]` defaults and the id it writes |
| OS | SharkRF openSPOT3 manual, "Homebrew/MMDVM connector" page (footer (c) 2021), https://manuals.sharkrf.com/openspot3/en/homebrew-mmdvm-connector.html | XLX module linking by calling 4000 / 4001.. (call type not stated); nothing on keepalive, NAK or close |
| BD | BlueDV for Windows (AMBE) page, https://www.pa7lim.nl/bluedv-windows-ambe (read 2026-10-30) | XLX: connect with a private call to the reflector id (400x), then talk TG 9 group; private 4000 before changing module |

Not accessible: WPSD (its source repository requires a login; it ships
upstream DMRGateway, whose link-event reasons came in PR #160, merged in
`d751e20e`); openSPOT and BlueDV publish no protocol-level behaviour
(no public info on login retry, ping, NAK or close handling).

Other masters read (shallow clones):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| xlxd | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26) | reference master (xlxd-ref) |
| URFd | https://github.com/n7tae/urfd @ `bd0c114e` (2025-12-29) | XLX-family master |
| HB3 | https://github.com/HBLink-org/hblink3 @ `1db27305` (2025-10-27) | TG-bridge master |
| HB4 | https://github.com/n0mjs710/hblink4 @ `2348c3bc` (2026-10-21, v4.8.0) | TG-bridge master |
| FDMR | https://gitlab.hacknix.net/hacknix/FreeDMR @ `546165eb` (2026-10-17, v1.6.2) | TG-bridge master behind a per-peer proxy |

## Client behaviour in short

| | GW (XLX and DMR network) | CM | DS | HB3p / FDp |
|---|---|---|---|---|
| Login | RPTL, RPTK (40 B, SHA-256 of salt + password), RPTC (always 302 B), RPTO only when Options are set (never on the XLX link); each stage resent every 10 s until acked | as GW | RPTL sent once, never retried; each RPTACK advances (RPTK, RPTC 302 B); connected after the RPTC ack; RPTO sent then if set, its reply ignored | RPTL every PING_TIME (5 s in the sample) until acked |
| Ping | RPTPING every 10 s | as GW | every 5 s | every PING_TIME |
| No MSTPONG / RPTACK | 60 s, then closes the socket, opens a new one (random port) and sends RPTL 10 s later; DMRD does not reset the timer | as GW, and it sends RPTCL first when it was logged in | no timeout: stays "connected" | MAX_MISSED (3) pings, then a new RPTL |
| MSTNAK | during login: new socket, RPTL after 10 s (logged with reason login / auth / config); logged in: RPTL after 10 s on the same socket, then GW relinks its XLX module | as GW | any state: disconnect, reconnect after 10 s | re-login (id must match unless LOOSE) |
| MSTCL | 5-byte tag, any id or none: new socket, RPTL after 10 s | as GW | reconnect after 10 s | re-login (id must match unless LOOSE) |
| ACK / PONG checks | RPTACK: 6-byte tag, salt read from bytes 6-9 without a length check; MSTPONG: 7-byte tag, id ignored | as GW | RPTACK tag; MSTPONG must be 11 B, id ignored, only counted | RPTACK after RPTK/RPTC/RPTO and MSTPONG need the id, big endian, unless LOOSE; the sample's XLXPEER section sets LOOSE "to connect to a non-compliant system such as XLXD, DMR+" |
| DMRD received | 55 B (71 B with trunking) only; XLX mode keeps only slot 2 group TG 9 ("Unexpected data" otherwise); replies only from the master's IP and port | 55 B expected: a jitter buffer asserts the size and asserts are compiled in, so another size aborts the process; IP and port checked; slot filter only | exactly 55 B; no slot, TG or call-type filter; no source check | not read |
| DMRD sent | 55 B; XLX: slot 2, voice group TG 9, link bursts private 400x; stream id and sequence passed through from MMDVMHost | 55 B; YSF2DMR XLX: private link burst to 400x then voice group TG 400x, slot 2; otherwise configured TG, optional 4000 unlink | 55 B; slot (default 2), TG (default 4000) and group / private chosen by the user; one header, voice, terminator; new random stream id per over | XLXPEER: 4000 then the module as private link bursts after login |
| Stream id | written and read as opaque host-order bytes | as GW | random, host order on send; read big endian for display only | passed through |
| Close | RPTCL 9 B on unlink, reflector change and shutdown | RPTCL 9 B whenever it closes while logged in | RPTCL 9 B on disconnect | - |
| Local port | random per socket (`Local` unset in the sample and by PS) | random unless `Local` is set | random | fixed `PORT` |

Pi-Star (PS) writes DMRGateway `[XLX Network]` with Port 62030, Password
`passw0rd`, Slot 2, TG 6, Base 64000, Startup 950, Relink 60, UserControl
1, the XLX host's password and port from DMR_Hosts.txt, and sets the XLX
`Id` to the operator's 7-digit DMR id (truncated, no ESSID suffix).

## How the cases judge

`cases/proto/test_mmdvm.py` probes xlxd-ref and the system under test in
parallel; the Judged column of testlist K1 says how:

- **hb**: a spec MUST (Level column); asserted on both sides.
- **diff**: a convention the masters agree on, or an XLX-family convention;
  sut == ref.
- **dev**: disputed between masters, a convention without agreement, or
  an xlxd deviation from a spec. Under `DVT_SUT_PROFILE=xlxd` sut == ref
  (xlxd's behaviour is recorded as data); under another profile the
  profile's spec entry (`dvtest/specs/<profile>.py`, `"mmdvm"` dict
  `{"P-DMR-NN": {observed key: value}}`) decides, and the case skips with
  "spec pending" while there is none. A non-xlxd system is never forced to
  copy an xlxd choice on a disputed item.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).

Lenient authentication (a zero RPTK hash acked) stays the default
expectation for every profile: the tester sends zero hashes and no RPTC;
a strict-hash probe is recorded, not failed, unless a profile spec asks for
strict. dvcheck (testlist H3) checks the DMRD framing passively in both
directions (sizes, sequence, header -> bursts A..F order and the
voice-sync flag on A, terminator, traffic after close); it decodes no sync
pattern, EMB or embedded LC, so those are not checked anywhere yet.

Embedded LC: the tester's voice bursts B..F carry an all-zero centre
(EMB with CC 0 and LCSS 0, no LC) by default. `dvtest/proto/dmr.py`
builds the real embedded signalling on request (`voice_packets(...,
embedded_lc=True)`, `DmrClient.talk(..., embedded_lc=True)`): the group
voice channel user LC (ETSI-2 7.1.1/1) of src -> dst with its 5-bit
checksum (ETSI-1 B.3.11) in the Hamming (16,11,4) BPTC of B.2.1 (table
B.16, figure B.3), interleaved over bursts B, C, D, E with LCSS 1, 3, 3, 2
(9.3.3 table 9.20), burst F the null message with LCSS 0 (7.1.3/2, D.1),
each EMB (9.1.2 table 9.3) protected by QR (16,7,6) (B.3.2 table B.12) and
placed around the fragment in the burst centre (annex E tables E.6..E.9,
E.11). Only P-DMR-36 sends it (late entry needs it).

Conventions: ids and 32-bit control fields are big endian on the wire
unless noted. "Silently ignored" means no reply and no state change.

## Dev items: what the clients do under each server behaviour

- **P-DMR-03** unknown id. Silence: GW and CM resend RPTL every 10 s (new
  socket every 70 s), DS waits forever showing "connecting", HB3p resends
  every 5 s. MSTNAK: GW and CM retry after 10 s and log a login failure
  with a reason; DS shows disconnected and retries every 10 s. Same load,
  but only MSTNAK tells the operator why.
- **P-DMR-04** wrong hash. Every client hashes its configured password
  correctly (PS and DS take it from the host file, `passw0rd` for XLX).
  MSTNAK: GW logs reason "auth" and retries every 10 s; lenient ack: the
  client works. No client depends on a rejection.
- **P-DMR-05** RPTK without RPTL. No client sends it on purpose; it happens
  when a master restarts while a client resends RPTK. Ack: the client goes
  on with RPTC and is logged in; MSTNAK: GW and CM restart with RPTL after
  10 s. Both recover.
- **P-DMR-07** RPTC size. Every client sends exactly 302 B (GW, CM, DS);
  no client sends 301 or 303 B (the 306 B BM text form is BM's own). An
  unacked RPTC keeps GW and CM resending it and re-logging in every 60 s,
  so acking 302 B is what matters.
- **P-DMR-08** voice before RPTC. Every client sends RPTC before it calls
  itself logged in (GW and CM wait for its ack; DS is connected only after
  it). No client depends on either answer.
- **P-DMR-11** MSTPONG id. GW, CM and DS ignore the id (tag only); HB3p
  and FDp check it big endian unless LOOSE, and their samples need LOOSE
  for xlxd. Big endian serves every client.
- **P-DMR-12 / 13** drop and later packets. MSTCL: GW and CM reconnect
  from a new port after 10 s, DS after 10 s, HB3p if the id matches.
  MSTNAK: GW and CM re-login after 10 s on the same port, DS reconnects
  after 10 s. Silence (or URFd's empty datagram, which GW ignores): GW and
  CM recover through their own 60 s timeout, HB3p after 3 missed pings,
  DS never. A client dropped for silence usually cannot hear the
  notification; what it needs is MSTNAK to its next ping or DMRD.
- **P-DMR-18** DMRD size. All clients send 55 B (GW 71 B only with
  trunking). On receive GW drops other sizes, DS drops them, CM aborts on
  them. Never relay a non-55 B DMRD.
- **P-DMR-22** header with voice sync. Every client sends its header as a
  data-sync burst of data type 1; none sends a voice-sync header. No
  client depends on the answer. A refused header refuses its stream id:
  the voice after it is not a late entry (P-DMR-36), because a client
  whose call was refused does not expect it to start later.
- **P-DMR-27 / 28 / 26** egress repeater id, stream id, sequence. GW
  ignores the repeater id and passes the stream id and sequence through
  as opaque bytes; MH does not use the stream id or the sequence byte; DS
  shows them only; CM's jitter buffer is timed, not sequence-ordered. No
  client depends on them.
- **P-DMR-36** voice without a header. Clients always send a header first
  (MH synthesises one on RF late entry), so this only happens when the
  header datagram is lost; DS sends a single header. Receivers tolerate
  late entry: MH does network late entry from the embedded LC, DS plays
  any voice. Dropping it loses the whole over.
- **P-DMR-39** same id from two ports, in practice: (a) every GW and CM
  reconnect after a timeout, MSTCL or login MSTNAK opens a new socket on
  a random port while the old session still exists on the master; (b) PS
  writes the plain 7-digit id as the XLX id, so one operator's two
  hotspots arrive with the same id from two addresses. Two sessions keyed
  by (id, address) serve both; "last address wins" makes case (b) flap
  (inferred: each ping from the other address gets MSTNAK and re-logs in).
- **P-DMR-41** data bursts. MH would key data from the network on air and
  GW passes slot 2 group TG 9 data in XLX mode; DS ignores data. No XLX
  client sends data on the reflector link. Either answer is tolerated.
  The stream id of a refused data burst stays refused, so headerless voice
  under it is not a late entry (as P-DMR-22).

## 1. Login

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-01 | `RPTL` + id (8 B) from a known id is answered with `RPTACK` + 4-byte salt | MUST (HB15) | HB15 p.3 "The Login Protocol": MSTNAK, or ack + id + random 32-bit value (MMDVM variant: `RPTACK` + salt, no id; BMW "G4KLX MMDVMHost version") | GW, CM, DS, HB3p send 8 B and read the salt from bytes 6-9 (no length check: a short RPTACK hashes stale bytes) | HB3, HB4, FDMR, URFd send `RPTACK` + 4 B salt | measured: `RPTACK` + 4 B salt; same salt on a second RPTL | `RPTACK` + 4 B salt (spec; all clients) |
| P-DMR-02 | The salt is random, not a constant | MUST random (HB15); per login vs per start: free | HB15 p.3 ("a random 32-bit integer", used as a bit pattern) | clients only hash it; no client depends on per-login freshness | HB3, FDMR new salt per RPTL; HB4 per login (reused on retry); URFd one per process start | measured: one salt per start, reused for every login; changed across a restart | random per login (spec intent; clients indifferent) |
| P-DMR-03 | `RPTL` from an id the master does not accept never logs in | MUST (HB15: MSTNAK + id) | HB15 p.3 | silence: GW, CM resend RPTL every 10 s, DS hangs in "connecting", HB3p resends every 5 s; MSTNAK: GW, CM retry after 10 s with a logged reason, DS retries every 10 s | HB3, FDMR `MSTNAK` + id (FDMR also blacklists the IP 300 s); URFd silent for an id missing from the DMR-id database, MSTNAK when the gatekeeper denies it; HB4 acks any RPTL and refuses at RPTK | measured: id 0 gets no reply (silence, not MSTNAK) | `MSTNAK` + id (spec; same retry load for GW/CM, tells the operator why, DS otherwise hangs) |
| P-DMR-04 | `RPTK` + id + SHA-256(salt + passphrase) (40 B) completes the login with `RPTACK`; a wrong hash gets `MSTNAK` | MUST (HB15); hash check: disputed | HB15 p.3 (hash over the salt bytes followed by the passphrase; MSTNAK on reject); BMW: BM's own variant sends hex strings | GW, CM, DS, HB3p send a correct 40 B hash of the configured password (PS and DS take it from the host file: `passw0rd` for XLX hosts); MSTNAK: GW logs "auth" and retries every 10 s; no client depends on a rejection | HB3, HB4, FDMR verify the hash and MSTNAK (FDMR may allow an empty passphrase); URFd does not check | measured: zero hash and wrong hash both acked (bare `RPTACK`) | lenient ack (the XLX password is public; clients work either way); strict + MSTNAK only when a password is configured |
| P-DMR-05 | `RPTK` without a preceding `RPTL` | disputed | not stated (HB15 p.3 only gives the order) | no client sends it on purpose; after a master restart GW/CM may resend RPTK: ack lets them go on to RPTC, MSTNAK restarts their login after 10 s | HB3, HB4, FDMR `MSTNAK` (state check); URFd acks and logs the client in | measured: `RPTACK`, then MSTPONG answered (logged in) | `MSTNAK` (other servers; clients recover either way) |
| P-DMR-06 | `RPTC` + id + config (302 B: callsign, frequencies, power, colour code, position, height, location, description, URL, software and package ids) is acknowledged | MUST (HB15) | HB15 p.5 "The Configuration Protocol" (field table, 302 B; master replies with ack + id); BMW: BM text form 306 B | GW (fixed-width 294 B + 8), CM, DS send 302 B; GW, CM, DS check the 6-byte tag only; HB3p needs `RPTACK` + id unless LOOSE; without an ack GW/CM resend every 10 s and re-login every 60 s | HB3, HB4, FDMR `RPTACK` + id; URFd bare `RPTACK` | measured: bare 6-byte `RPTACK` (also for RPTK and RPTO) | `RPTACK` + id (spec; works for strict HB3p, tag-only clients ignore the id) |
| P-DMR-07 | `RPTC` of another size (short or long) | disputed | HB15 p.5 gives 302 B; nothing on other sizes | every client sends exactly 302 B; none sends 301 or 303 B | xlxd, URFd ack exactly 302 B only; HB3, HB4, FDMR do not check the size (state only) | measured: 301 and 303 B not acked, login kept | ack 302 B; tolerate longer (BMW 306 B form); no client depends on the rest |
| P-DMR-08 | Voice is routed for a client that never sent `RPTC` | disputed | HB15 p.5: the repeater sends its config after login (client duty; master gating not stated) | every client sends RPTC first (GW/CM wait for its ack, DS is connected only after it); no client depends on it | HB3, HB4, FDMR drop DMRD until RPTC; URFd routes without it | measured: routed (the tester relies on this) | route (harmless for clients; the tester needs it) |
| P-DMR-09 | `RPTO` + id + options is answered with `RPTACK` | convention (impl, agreed) | not in HB15 or BMW; option syntax in MMDVMHost `DMRplus_startup_options.md` | GW never sends RPTO on its XLX link; GW (DMR network with Options) and CM wait for its ack and loop like an unacked RPTC; DS sends it after login and ignores the reply | HB3 (only once connected, i.e. after RPTC), HB4, FDMR `RPTACK` + id; URFd bare `RPTACK`, options ignored | measured: `RPTACK` | `RPTACK` + id, always (GW/CM need an ack) |
| P-DMR-10 | Re-login (`RPTL`/`RPTK` again, same id and address) keeps one session; traffic once | convention (impl, agreed) | not stated | GW/CM re-login on the same socket after an MSTNAK while logged in, then GW resends its XLX link | HB3, FDMR overwrite the entry; HB4 refreshes it; URFd refreshes the (id, address) client | measured: one MSTPONG per RPTPING, 36/36 frames once | one session (all agree) |

## 2. Keepalive and close

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-11 | `RPTPING` + id (11 B) from a logged-in client is answered with `MSTPONG` + id, id big endian | MUST (HB15: reply; 32-bit values in on-air, big endian order) | HB15 p.4 "The Keep-Alive Protocol" (names MSTPING/RPTPONG; MMDVM uses RPTPING/MSTPONG) and revision note 2015-07-26; BMW: ping every 5-15 s | GW, CM ping every 10 s and re-login after 60 s without MSTPONG (DMRD does not reset that timer); DS pings every 5 s, needs an 11 B MSTPONG only to count it; GW, CM, DS ignore the id; HB3p, FDp check it big endian unless LOOSE | HB3, HB4, FDMR echo the received bytes (big endian); URFd host order (little endian on x86) | measured: MSTPONG, id little endian | `MSTPONG` + id big endian (spec; strict HB3p/FDp) |
| P-DMR-12 | `RPTPING` from an address/id that is not logged in gets no `MSTPONG` | MUST (HB15: packets after a dead link get MSTNAK) | HB15 p.4 | MSTNAK: GW, CM re-login after 10 s, DS reconnects after 10 s; silence: GW, CM wait for their 60 s timeout, DS never recovers (no pong timeout) | HB3, HB4, FDMR answer `MSTNAK`; URFd silent | measured: no MSTPONG, no MSTNAK (silent) | `MSTNAK` + id (spec; the only answer that brings DS back, 10 s instead of 70 s for GW) |
| P-DMR-13 | A client silent past the keepalive timeout is dropped; what it is sent | drop: MUST (HB15); notification and timeout: disputed | HB15 p.4: the master marks the link dead and answers later packets with MSTNAK; MSTCL is defined for a master that shuts down; no timeout value (pings every minute) | MSTCL (tag only for GW, CM, DS; id checked by HB3p unless LOOSE): reconnect after 10 s; MSTNAK: re-login after 10 s; silence or an empty datagram (GW ignores 0-length reads): GW/CM own 60 s timeout, DS nothing | HB3 sends nothing (5 s x 3 missed); HB4 `MSTNAK` (about 90-120 s); FDMR `MSTCL` + id after 30 s (its proxy: 3 bare MSTCL); URFd an empty datagram after 100 s | measured: MSTCL after 100.7 s (checked every 10 s) | drop after a timeout well above the slowest ping (GW 10 s); MSTCL + id is tolerated by all, but what clients need is MSTNAK on their next packet (P-DMR-12) |
| P-DMR-14 | After the drop the client gets no further voice | MUST (HB15) | HB15 p.4 | nothing expected; a client that still believes it is logged in re-logs in on MSTNAK | all remove the peer | measured: 0 frames | no voice (spec) |
| P-DMR-15 | `RPTCL` + id (9 B) closes the session (no further traffic, a floor it held is released) | SHOULD (HB15: a closing repeater should send RPTCL; the master's action is implied) | HB15 p.4 | GW (unlink, reflector change, shutdown), CM (any close while logged in), DS (disconnect) send 9 B and close the socket; none waits for a reply | HB3, FDMR delete the peer and reply MSTNAK + id; HB4 deletes, no reply; URFd parses only 13 B and reads the id one byte early, so neither form closes | measured: neither 13 B nor 9 B closes (voice still heard, MSTPONG answered); DV-Router closes (B15) | close on 9 B, no reply needed (spec; every client sends 9 B) |
| P-DMR-16 | `RPTINTR` (17 B) gets no reply; the login stays | MUST no reply (BMW: "no acknowledge"); acting on it: optional | BMW "Incoming call interrupt" (RPTINTR + 8 hex id + slot) | no client sends it (GW, CM, DS, HB3p) | HB3, HB4 log it; FDMR proxy drops it; URFd treats it as an RSSI report and ignores it | measured: no reply, MSTPONG still answered | no reply (BMW) |

## 3. Malformed and unknown packets

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-17 | Unknown tags, short packets (3 B, truncated RPTL) and oversize packets get no reply; the master keeps serving | convention (impl, agreed) | not stated | GW sends extra tags on DMR Network links (`DMRA` talker alias, `DMRG` / `RPTG` positions when Location is on; none on its XLX link); GW, CM log unknown tags from the master and go on | HB3, HB4 log and ignore; FDMR drops; URFd ignores | measured: no reply to `XXXX`, 3 B, 7 B RPTL, 1400 B DMRD; MSTPONG still answered | ignore silently (all agree; DMRA / RPTG must not break the session) |
| P-DMR-18 | A `DMRD` of a size other than 55 B is not relayed | disputed | HB15 p.6 "The Data Protocol": 53 B; the MMDVM variant appends BER and RSSI (55 B) | all send 55 B (GW 71 B only with trunking on); on receive GW keeps 55 / 71 B, DS exactly 55 B, CM aborts on another size (asserted jitter buffer) | xlxd, URFd exactly 55 B; HB4 at least 55; FDMR at least 53 (pads); HB3 no check | measured: 54/56 B voice not relayed (the 55 B header and terminator were) | not relayed; egress always 55 B (clients) |
| P-DMR-19 | `DMRD` from an address that is not logged in is not relayed | MUST (HB15: no data before login; dead link gets MSTNAK) | HB15 p.3-4 | after a master restart GW/CM keep sending DMRD and pings; MSTNAK brings them back after 10 s | HB3, FDMR drop silently; HB4 MSTNAK; URFd ignores | measured: 0 packets relayed | drop + `MSTNAK` (spec; rate-limited per address) |

## 4. DMRD layout (55 B)

Offsets: 0 `DMRD`, 4 sequence, 5-7 source id, 8-10 destination (TG), 11-14
repeater id, 15 flags (bit 7 slot 2, bit 6 private call, bits 5-4 frame
type: 0 voice, 1 voice sync, 2 data sync; bits 3-0 voice sequence A-F or
data type: 1 voice LC header, 2 terminator), 16-19 stream id, 20-52 the
33-byte DMR burst, 53 BER, 54 RSSI. HB15 p.6 lists the same fields in a
53-byte packet (no BER/RSSI); its printed bit masks do not match the MMDVM
wire layout above, which follows MMDVMHost/DMRGateway.

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-20 | Only slot 2 is a reflector slot: header, voice and terminator on slot 1 are ignored | convention (XLX family) | ETSI-1 4.2.2 / 5.1 define the two time slots; nothing on network use | GW XLX mode maps its RF slot/TG to slot 2 TG 9; YSF2DMR and HB3p XLX links use slot 2; DS defaults to slot 2 (user can pick 1) | xlxd, URFd slot 2 only; HB3, HB4, FDMR route both slots by their bridge rules | measured: 0 packets | slot 2 only (XLX family; all XLX clients use it) |
| P-DMR-21 | Group calls are relayed; private-call (bit 6) voice is not | convention (XLX family) | ETSI-2 5.2.1 (group call), 5.2.2 (individual call); ETSI-1 9.3.11 (FLCO) | GW, CM, HB3p link with private-call bursts and talk group calls; BD: private call to connect, then TG 9 group; DS sends private voice only when its PC switch is on | xlxd, URFd drop private voice (URFd still acts on private 4000/400x headers); HB3, HB4 route unit calls (map, opt-in); FDMR uses private calls as dial-a-TG control | measured: 0 voice frames (private voice after a group header: header and terminator relayed) | relay group, not private voice (XLX family) |
| P-DMR-22 | A stream opens with a data-sync burst of data type 1 (voice LC header) with a BS or MS data sync pattern; a header with another sync opens nothing | form: MUST (ETSI); rejecting another sync: disputed | ETSI-1 5.1.2/2 (voice initiation), 7.1.1 (LC header in a general data burst), 9.1.1 table 9.2 (sync patterns), 9.3.6 table 9.22 (data type 1) | every client sends a data-sync header of data type 1 (GW link bursts BS data sync); none sends a voice-sync header; receivers re-derive the framing | xlxd, URFd check BS/MS data sync on the header, but URFd then opens the stream on the following voice (late entry); HB3, HB4, FDMR do not check the sync | measured: MS data sync header 36/36 frames; header with BS voice sync 0 frames | accept BS/MS data sync; reject another sync (no client affected); the refused header marks its stream id refused, so the voice after it is not relayed and no late entry (P-DMR-36) happens for that id: a refused call stays refused, clients do not expect a refused call to start later |
| P-DMR-23 | Voice comes as superframes of bursts A-F (A voice sync, B-F embedded signalling), three 72-bit AMBE+2 frames per burst; the master relays every frame unchanged | structure: MUST (ETSI); unchanged relay: convention (impl, agreed; HB15 allows FEC fixes) | ETSI-1 4.2.2 (three 72-bit vocoder frames + 48-bit sync), 5.1.2/1 (superframe), 7.1.3 (embedded LC); HB15 p.6 "DMR Data" | GW, CM, DS send A-F with the voice sequence in bits 3-0; DS extracts the three AMBE frames; embedded LC use on receive: MH late entry (not checked further) | AMBE passthrough agreed: HB3, FDMR rewrite only the embedded LC when the TG changes (HB3 also on data bursts); URFd null embedded LC; xlxd re-encodes it | measured: 60/60 frames byte exact | AMBE frames unchanged (all agree) |
| P-DMR-24 | A stream ends with a data-sync burst of data type 2 (terminator with LC), passed to listeners | MUST (ETSI) | ETSI-1 5.1.2/3 (voice termination), 7.1.2, 9.3.6 | GW, CM, DS send it; DS ends playback on it | agreed | measured: listeners get it; a second talker 100 ms later is relayed whole | relay it (spec) |
| P-DMR-25 | Egress: header, voice A-F (A voice sync), terminator, slot 2, group call, one stream id per stream; destination TG 9 | structure: MUST (ETSI); TG 9: convention (XLX family, the client requires it) | ETSI-1 5.1.2/1-5.1.2/3 | GW XLX mode accepts only slot 2 group TG 9 from the reflector (anything else logged as unexpected and dropped) and rewrites it to its RF slot/TG; DS and CM accept any slot (CM: enabled slots) and TG | xlxd, URFd always TG 9, slot 2, group; HB3, HB4, FDMR write the target bridge TG | measured: structure ok (14 packets), dst TG 9 | slot 2, group, TG 9 (GW requires it) |
| P-DMR-26 | The egress sequence byte starts at 0 and counts up per stream | MUST (HB15: starts at zero for each transmission, wraps) | HB15 p.6 "Seq No" | no client depends on it: GW passes it through, MH and CM do not use it, DS shows it | URFd per-module counter (header 0); HB3, HB4, FDMR pass the ingress byte through | measured: first 0, consecutive | count from 0 (spec) |
| P-DMR-27 | Egress source id = the talker's radio id; egress repeater id field | source id: convention (impl, agreed); repeater id: disputed | HB15 p.6 (RptrId defined for the sending repeater only) | source id shown by DS and passed on by GW; repeater id: GW and MH ignore it, DS shows it; no client depends on it | source id kept by all; repeater id: HB3, FDMR the receiving peer's id; HB4 the source repeater's id; URFd the talker's login (repeater) id | measured: src = radio id; repeater id = the source id, not the login id | src = radio id; repeater id free (no client reads it) |
| P-DMR-28 | Egress stream id | disputed | HB15 p.6: stream id stays the same from PTT press to release | GW, CM, DS write a random id in host byte order and GW passes received ids through as opaque bytes; MH ignores it; DS shows it; no client depends on its value, only on one id per over | HB3, HB4, FDMR pass it through; URFd keeps 16 bits (upper bytes zero); xlxd folds 32 to 16 bits (high XOR low, 0 -> 1); DV-Router keeps it (B10) | measured: folded, not kept | keep (B10; one id per over is all clients need) |

## 5. Talkgroup routing

No spec covers module selection; these are the XLX conventions the XLX
clients are built on (unlink 4000, modules 4001-4026, voice on slot 2
TG 9). Two link forms are in use: the private-call burst of GW, YSF2DMR
and HB3p (P-DMR-43) and a plain over to TG 400x from DS, BD and openSPOT
(P-DMR-29).

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-29 | A header to TG 4001..4026 links the client to module A..Z | convention (XLX family) | not stated | DS: the user sets TG 4001.. (group by default) and keys up; BD: private call to 400x, "press PTT once"; OS: "call 4001" (call type not stated); GW, YSF2DMR, HB3p: private link burst (P-DMR-43) | xlxd; URFd (only to an existing module, only when unlinked); HB3, HB4 no meaning; FDMR dial-a-TG by private call | measured: 4001 and 4026 link (group-call header) | link on group and private headers (clients use both) |
| P-DMR-30 | A header to TG 4000 unlinks; no further voice | convention (XLX family) | not stated | GW (private, before a module change and on relink), BD (private 4000), DS (group 4000 is its default TG), YSF2DMR (optional TG unlink), OS ("call 4000") | xlxd, URFd; FDMR private 4000 unlinks its dial-a-TG | measured: the leaver gets no voice, a linked control does | unlink on group and private 4000 |
| P-DMR-31 | A linked client talking to another module's TG stays on its module; a module change needs 4000 first | convention (XLX family) | not stated | GW sends 4000 before a new 400x; BD documents "MUST disconnect (private 4000) before changing"; YSF2DMR talks group TG 400x of its own module after linking | xlxd, URFd | measured: own module hears it, the other module does not | stay on the module (XLX family; clients follow it) |
| P-DMR-32 | TG 9 (or any TG that maps to no module) from a linked client goes to its module; from an unlinked client it is ignored | convention (XLX family) | not stated | GW sends its XLX voice as TG 9 slot 2; BD talks TG 9 group after linking | xlxd, URFd | measured: linked heard, unlinked not | as xlxd (XLX family) |
| P-DMR-33 | A header links; an empty stream (header + terminator) is enough to join and is not relayed as voice | convention (XLX family) | not stated | GW, YSF2DMR, HB3p link bursts are header + terminator only | xlxd; URFd acts on the header (private headers never open a stream) | measured: joined; listeners got 0 packets of the join | join on the header, relay nothing of an empty stream |
| P-DMR-43 | The private-call link burst is honoured: 3 private-call voice LC headers + 2 terminators (one stream id, no voice) to TG 4001..4026 link the module, to TG 4000 unlink; nothing of the burst is relayed | convention (XLX family) | not stated: ETSI-2 5.2.2 defines the individual (private) call and ETSI-1 9.3.11 its FLCO, nothing on using it as a network command; HB15 and BMW: no link commands | GW (DMRGateway.cpp writeXLXLink) sends slot 2, private-call flag, BS data sync, headers seq 0-2 then terminators seq 3-4, back to back, one random stream id, repeater id = its login id, src = its login id (startup link, relink) or the RF user's id (user control, private call to the XLX base + 0..26), 4000 first when already linked, and relinks after every re-login; YSF2DMR (writeXLXLink) and HB3p XLXPEER (send_xlxmaster, 4000 then the module after login) send the same 3 + 2 form | xlxd (cdmrmmdvmprotocol.cpp IsValidDvHeaderPacket / OnDvHeaderPacketIn) and URFd (DMRMMDVMProtocol.cpp, same functions) derive link/unlink from the header's TG whatever the call type (link only while unlinked, 4000 only while linked), open no stream for a private header and ignore the terminators (no stream); later headers of the burst change nothing | measured: linked (36/36 frames heard) and unlinked (0 frames, linked control heard), with src = login id and src = another radio id; listeners got 0 packets of the burst | honour it (three clients send it) |

## 6. Stream lifetime

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-34 | A stream whose terminator is lost ends after a silence timeout; the module is then free | convention (impl, agreed); timeout and closing terminator: differ (judged by B3) | not stated (ETSI-1 5.2.1/4 hang times are air-interface only) | MH and DS end a stream without a terminator by their own watchdogs; a closing terminator is tolerated | xlxd, URFd 1.6 s; HB3, FDMR 0.36 s contention and 5 s cleanup; HB4 2 s | measured: module free within 2.2 s; listeners got a terminator | per B3 |
| P-DMR-35 | A second header on a busy module is not mixed into the running stream | convention (impl, agreed); late-entry policy judged by B1/B2 | not stated | no client mixes two streams on one slot; GW keeps one network per slot at a time | URFd one stream per module; HB3, FDMR contention window plus hang time; HB4 busy-slot check, 200 ms | measured: first stream whole, no mixing, late talker 0 frames | no mixing (all agree); policy per B1/B2 |
| P-DMR-36 | Voice bursts whose stream id had no header are not relayed | disputed (ETSI provides late entry) | ETSI-1 4.3 and 7.1.3 (embedded LC "to facilitate late entry"), 5.1.2/2 (trunked voice without header) | clients always send a header first (MH synthesises one on RF late entry; DS sends exactly one), so this is a lost header datagram; MH does network late entry, DS plays any voice | URFd builds a header from the voice and opens the stream; HB3, HB4 synthesise the LC and relay; FDMR not checked | measured: 0 frames (xlxd only buffers them for late-entry promotion); still 0 with a valid embedded LC in B..E (rerun 2026-10-30) | late entry: open from the embedded LC with a synthesised header (ETSI, URFd, HB3, HB4; receivers tolerate it), never for a stream id whose header or data burst was refused (P-DMR-21, 22, 41) |
| P-DMR-37 | The talker never receives its own stream back | convention (impl, agreed) | not stated | not checked whether clients filter their own source id; GW would hand an echo to MH | HB3, HB4, FDMR skip the source system; URFd skips the stream owner | measured: 0 packets to the talker | no echo (all agree) |
| P-DMR-41 | Data-sync bursts that are not voice (CSBK, data header, rate 1/2, 3/4: data type >= 3) open no voice stream; whether they or a following headerless voice are relayed | non-voice: MUST (ETSI); forwarding: disputed | ETSI-1 9.3.6 table 9.22, 7.2.1 (CSBK), 8.2.1 (data headers), 9.2.2 / 9.2.7 (rate 3/4, 1/2); ETSI-3 5.3.1 | no XLX client sends data on the reflector link; GW passes slot 2 group TG 9 data to MH, which would key it on air; DS ignores data | xlxd, URFd, HB4 do not forward data (URFd opens a stream on the voice that follows); HB3 forwards data (and overwrites part of it with embedded LC); FDMR forwards group data | measured: 0 packets (data and the voice after it) | do not relay data (no client needs it); the refused burst marks its stream id refused, so the voice after it is not relayed and no late entry (P-DMR-36) happens for that id: a refused call stays refused, clients do not expect a refused call to start later |
| P-DMR-42 | The ingress sequence byte wraps 255 -> 0 in a long over without loss or a new stream | MUST (HB15: wraps to zero) | HB15 p.6 "Seq No" | GW and CM pass the caller's byte through; DS counts per over; no client resets anything on a wrap | xlxd, URFd ignore the byte; HB3, HB4 track streams by stream id | measured: 120/120 frames, one stream, one header | track streams by stream id (spec) |

## 7. Several clients

| # | Obligation | Level | Spec / paper | Clients (what they send / expect / tolerate) | Other servers | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-DMR-38 | Two ids from one IP (different ports) are two sessions, each gets the module's traffic once | convention (impl, agreed) | not stated | two hotspots behind one NAT; GW opens a separate socket per network | HB3, HB4 key by id and check the address; FDMR one backend per id; URFd keys by (id, address) | measured: 36/36 frames once each | two sessions (all agree) |
| P-DMR-39 | The same id from two ports | disputed | not stated | GW and CM open a new random-port socket on every reconnect while the old session still exists; PS writes the plain 7-digit id as the XLX id, so one operator's two hotspots share an id | URFd two sessions keyed by (id, address); HB3 a re-login overwrites (last address wins); HB4 the newcomer must pass a takeover login, then the old address gets MSTNAK; FDMR's proxy follows the latest address | measured: two sessions, 36/36 frames once each | two sessions keyed by (id, address); the stale one ages out by P-DMR-13 |
| P-DMR-40 | A keepalive from one client never refreshes another client's session | convention (impl, agreed) | not stated | each client pings for itself (GW, CM 10 s; DS 5 s) | all keep per-peer timers | measured: the pinging client kept voice and MSTPONG while another was dropped | per-session timers (all agree) |
