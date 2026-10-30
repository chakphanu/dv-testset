<!-- SPDX-License-Identifier: MIT -->
# XLX interlink: obligations of a reflector toward its XLX peers

What a reflector owes another reflector on the XLX interlink (UDP 10002,
`DSVT` streams in DExtra framing plus a link/keepalive layer). Both ends
are servers; "peer" below is the other reflector. Each item is numbered
`P-XLX-NN`; the conformance cases are listed under "Testlist rows" at the
end (section K of `docs/testlist.md` once merged).

The XLX interlink has **no published specification**. The xlxd README
documents the revision table and the frame sizes, the xlxd.interlink file
comment the configuration; everything else (byte layout of the link
packets, timers, routing) is what the implementations do. So almost every
row is `convention (impl)` or `disputed`; no row is MUST.

**Level**: `convention (impl)` names the implementations that agree;
`disputed` names who does what. `convention (XLX family)` = xlxd and the
peers built to talk to it (BrandMeister, URFd's BM link) agree.

## Sources

Documents (no author protocol note, changelog or mailing-list post was
found; the dashboard `changes.txt` and the XLX API do not cover the
interlink):

- **[README]** xlxd README.md, section "XLX Interlink Protocol" (revision
  table 0/1, 2, 3; frame sizes 27 / 45 / 53 B; "the sender chooses the
  frame size from the receiver's version") and the port table, @ `a978f03`
  (2026-10-26). https://github.com/LX3JL/xlxd/blob/a978f03671d87c6d9ac13453689755991e5cb668/README.md
- **[CONF]** xlxd `config/xlxd.interlink`, header comment: one line
  `<callsign> <ip> <modules>` per peer, the remote must list this reflector
  too, @ `a978f03`.
- **[I215]** xlxd issue #215 "Documentation on the XLX Interlink
  protocol?" (2022-03-08; answer 2023-02-12: no documentation, derived from
  DExtra). https://github.com/LX3JL/xlxd/issues/215
- **[I228]** xlxd issue #228 "xlxd never sends NAK packets ... if
  xlxd.interlink is empty" (2022-09-21, closed; the peer IP is not checked).
  https://github.com/LX3JL/xlxd/issues/228
- **[I73]** xlxd issue #73 (2018-01-15): the interlink carries D-Star
  headers only, no DMR LC or ids. https://github.com/LX3JL/xlxd/issues/73
- **[BMW]** BrandMeister Wiki "XLX" (last edited 2020-03-19): a BM master
  is an xlxd.interlink line `BM<id> <host> <modules>`.
  https://wiki.brandmeister.network/index.php/XLX
- **[BMN]** BrandMeister news "xlxd 2.2.1 interlink now supported on
  BrandMeister Master Servers" (F4BWG, 2018-01-18): BM needs xlxd >= 2.2.1
  (2.2.2 fully); D-Star -> BM is transcoded by xlxd. https://news.brandmeister.network/?p=231
- **[URFR]** URFd README.md @ `bd0c114`: "you can't interlink urfd with
  xlxd"; UDP 10002 is the BM connection, URF-to-URF uses UDP 10017.
  https://github.com/n7tae/urfd

Implementations read (shallow clones, behaviour described, not copied):

| Key | Repo @ commit (date) | Role |
|---|---|---|
| XH | https://github.com/LX3JL/xlxd @ `a978f03` (2026-10-26), 2.6.1 | reference (xlxd-ref, and the XLX998/XLX997 peers of the rig); revisions 0-3 |
| XC | https://github.com/LX3JL/xlxd @ `6b7658b` (2023-10-27), 2.5.3 | "classic" xlxd still widely deployed; revisions 0-2; jxmx/xlxd-evo @ `1d2d21f` (2025-11-21) has the same XLX code. `6b7658b` itself ("allow pass dmr/ysf to peers, without transcoding") removed the older 27 B fallback (`XO` below: xlxd before it) |
| UR | https://github.com/n7tae/urfd @ `bd0c114` (2025-12-29), `BMProtocol` last changed `cb7bb2a` (2024-05-08) | speaks the XLX wire protocol on 10002 only towards BM peers, revision 2 |
| BM | BrandMeister master | closed source; only [BMW], [BMN] |

## How the cases judge

`cases/proto/test_xlx.py` drives the tester as an XLX peer (callsign
XLX996, listed in xlxd-sut's and xlxd-ref's xlxd.interlink; see "Rig needs")
and observes the rig peers XLX998 / XLX997 and ordinary clients:

- **diff**: an XLX-family convention xlxd and the other peers agree on;
  sut == ref.
- **dev**: disputed, or xlxd deviates from the other peers; sut == ref under
  `DVT_SUT_PROFILE=xlxd`, else `SPEC["xlx"]["P-XLX-NN"]` of the profile,
  skip "spec pending" without one.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B).

No row is **hb**: there is no spec MUST. Byte order: the stream id is
written in host order by xlxd and URFd, i.e. little endian on every
deployment (de facto, [README] silent); version bytes are single bytes.

## 1. Link setup

Link packets (XH, XC, UR agree on the layout): `L` connect and `A` ack,
39 B: [0] tag, [1-8] reflector callsign space padded, [9-11] version
major / minor / revision, [12-38] module letters, NUL terminated and zero
padded. `N` nack and `U` disconnect, 10 B: [0] tag, [1-8] callsign, [9] 0.

| # | Obligation | Level | Docs | Peers (project @ commit: behaviour) | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|
| P-XLX-01 | `L` from a listed peer with modules in common is answered with `A` (39 B) carrying the own callsign, the own version and the shared modules | convention (impl) | [CONF] (both sides list each other); layout not documented | XH, XC: `A` with the intersection of requested and configured modules; UR: `A` echoing the requested modules; BM: answers xlxd >= 2.2.1 [BMN] | measured: `A`, callsign XLX999, version 2.6.1, modules `DE` | as xlxd |
| P-XLX-02 | The module list of the `A` | disputed | [CONF] "modules shared" | XH, XC: intersection of the `L` list and the configured list; UR: refuses unless the sets are equal | measured: `L` offering `DEH` -> `A` with `DE` | intersection (tolerates a peer that offers more modules) |
| P-XLX-03 | `L` from a callsign that is not listed gets `N` (10 B); no link | convention (impl) | [CONF], [I228] | XH, XC: `N`, except that an empty xlxd.interlink accepts anyone [I228]; UR: refused | measured: `N` (XLX995) | `N`; never accept-all |
| P-XLX-04 | `L` whose modules do not overlap the configured ones gets `N` | convention (impl) | not stated | XH, XC: `N` (empty intersection); UR: `N` (set mismatch) | measured: `N` (`L` offering `JK`) | as xlxd |
| P-XLX-05 | `L` with a listed callsign from an address that is not the listed one | disputed | [CONF] lists the IP; [I228] notes it is not checked | XH, XC: accepted, the IP is only used as the destination of outgoing `L`; a second peer object is created for the new address; UR: refused (IP and port must match) | measured: `A` (`DE`) to XLX998's callsign from the tester address | refuse (address check), as URFd |
| P-XLX-06 | A link carries traffic only after both directions completed (`L` -> `A` each way) | convention (impl) | not stated | XH, XC: the peer is created when an `A` is received, not when one is sent; UR: same structure | measured: our `L` acked, their `L` unanswered: 0 frames | as xlxd |
| P-XLX-07 | The reflector initiates: every listed peer that is not linked gets an `L` (own callsign, version, configured modules for that peer) every 5 s, also after an `N` | convention (impl) | not stated | XH, XC: 5 s, no backoff; UR: same, BM entries only | measured: `L` every 5 s, callsign XLX999, modules `DE` (XLX996 row) and `D` (BM3109 row) | as xlxd (a backoff after `N` is allowed) |
| P-XLX-08 | Protocol revision from the version in the peer's `A`: >= 2.6.0 -> 3 (53 B frames), >= 2.2.0 -> 2 (45 B), else 1 (27 B; < 1.4.0 is revision 0, same frame) | convention (impl); README table differs | [README] revision table (0/1 = "< 2.2.0") | XH: as stated; XC: stops at revision 2; UR: always revision 2, announces 2.4.31; BM: revision 2 [BMN] | measured: 1.3.9 -> 27 B, 1.4.0 -> 27 B, 2.2.0 -> 45 B, 2.6.1 -> 53 B | as XH; never send a revision the peer's version does not announce |
| P-XLX-09 | A peer whose callsign starts with BM (XH also FD, FS, PH) is a DMR peer: revision 2, only the AMBE+2 slot is used | disputed | [BMW] (BM line form only) | XH: BM/FD/FS/PH prefixes; XC: BM only; UR: every 10002 peer is BM | measured (as BM3109): 45 B frames, D-Star slot carries the stream | configured peer type (dv-router.toml kind; its xlxd-file converter maps BM/FD/FS/PH rows), never the callsign at run time; the rig's BM3109 row is a BM peer either way: 45 B, D-Star slot filled |
| P-XLX-10 | A repeated `L` or `A` while linked keeps one link; traffic is sent once | convention (impl) | not stated | XH, XC: `L` re-acked (revision >= 1), a second `A` ignored; changed modules apply only after `U` + relink | measured: 21/21 frames once after a repeated `L` and `A` | as xlxd |

## 2. Keepalive and disconnect

| # | Obligation | Level | Docs | Peers (project @ commit: behaviour) | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|
| P-XLX-11 | Keepalive: 9 B (own callsign, 8 B, then NUL) to every linked peer about every second | convention (impl) | not stated | XH, XC, UR: period 1 s; receiver checks size and callsign form | measured: 9 B, `XLX999`, 5 per 5 s | as xlxd |
| P-XLX-12 | A peer silent for 30 s is sent `U` and removed (no further traffic); the reflector then retries `L` every 5 s | convention (impl) | not stated | XH, XC, UR: 30 s; a peer that is streaming in counts as alive | measured: `U` 30 s after the last keepalive, `L` again, 0 frames afterwards | as xlxd |
| P-XLX-13 | `U` from the peer removes the link at once (no further traffic); `L` retries resume | convention (impl) | not stated | XH, XC, UR: removed; looked up by source address only | measured: 0 frames after `U`, `L` again within 8 s | as xlxd |
| P-XLX-14 | Keepalive or `U` carrying another callsign than the linked peer's, from the peer's address | disputed | not stated | XH, XC, UR: the callsign is not compared; the address alone selects the peer (a `U` with any callsign drops it) | measured: `U` with callsign XLX995 from the peer's address drops the link (0 frames afterwards) | match address and callsign |

## 3. Frames

Header 56 B (DExtra form): `DSVT`, [4] 0x10, [8] 0x20, [12-13] stream id
(little endian), [14] 0x80, [15-55] D-Star header (flags, RPT2, RPT1, UR,
MY, suffix, CRC). Voice: `DSVT`, [4] 0x20, [8] 0x20, [12-13] stream id,
[14] frame counter mod 21 (0x40 on the last frame), [15-23] AMBE+
(D-Star), [24-26] slow data; revision 2 adds [27] DMR packet id, [28] DMR
sub id, [29-37] AMBE+2, [38-44] DMR sync; revision 3 adds [45-52] Codec2
3200. [README] shows the same sizes and calls the slots "AMBE / AMBE+ /
Codec2".

| # | Obligation | Level | Docs | Peers (project @ commit: behaviour) | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|
| P-XLX-15 | Egress header: 56 B, RPT2 = own reflector callsign + module, stream id of the source stream | convention (impl) | [I73] (D-Star header only) | XH, XC: RPT2 rewritten to the sender; UR: same form | measured: 56 B, RPT2 `XLX999 D`, RPT1 `N0CALL D`, MY `N0CALL`, UR `CQCQCQ`, talker's sid, one header | as xlxd |
| P-XLX-16 | Egress voice has the size of the negotiated revision (27 / 45 / 53 B) and the field layout above; counter mod 21, last frame flagged | convention (impl) | [README] sizes | XH, XC: always the full size of the revision; XO (before `6b7658b`): 27 B while no transcoder is connected; UR: 45 B, 27 B without a transcoder | measured: 53 B, counter mod 21, D-Star 40/40 exact, last frame flagged | as XH for a running transcoder |
| P-XLX-17 | With the transcoder running, the AMBE+2 and Codec2 slots carry the transcoded audio of a D-Star source | convention (impl) | [README], [BMN] (D-Star -> BM transcoded) | XH, XC, UR agree | measured: AMBE+2 and Codec2 slots non-zero in 40/40 frames | as xlxd |
| P-XLX-18 | A transcoded output missing inside a transcoded stream (transcoder lost mid-stream): the frame is still sent at the revision size, the missing slots are zero | convention (impl); B6b | not stated | XH, XC: zero slots, full size (the receiver treats the slots as present by link revision and does not transcode them); XO, UR: frame size chosen per stream, not per frame | measured: frames before the stop carry AMBE+2 / Codec2, after it zero slots at 53 B; 201/201 frames sent | zero slots (dv-router: `zero_slot`, filled at the 120 ms deadline) |
| P-XLX-33 | No transcoder for the whole stream: frame form sent to the peer | disputed; B6b | not stated | XH, XC: full revision size, zero slots; XO (before `6b7658b`): 27 B frames; UR: 27 B frames from its sender, but its own receiver accepts 45 B only | measured: 53 B, AMBE+2 / Codec2 zero, D-Star 40/40 exact | zero slots (see D3) |
| P-XLX-19 | Last frame: D-Star slot is the DExtra end pattern, other slots carry data when the stream had them | convention (impl) | not stated | XH: end pattern; DMR slots real, Codec2 zero when the stream had none | measured: end pattern `55c87a` + zeros, slow data `251ac6`, flagged | as xlxd |
| P-XLX-20 | Ingress revision 3 frame: the slots are used as given (AMBE+2 to DMR/NXDN clients, Codec2 to M17 clients), no local transcoding of present slots | convention (impl) | [README] (the receiver gets what the sender transcoded) | XH: as stated; XC: revision <= 2 only; UR: 45 B only | measured: D-Star exact, DMR clients get the sent AMBE+2 (40/40), M17 clients 85 % of the sent Codec2 frames (pairing at the edges) | as xlxd |
| P-XLX-21 | Ingress revision 1 frame (27 B): the reflector transcodes the D-Star slot for its other modes | convention (impl) | [README] | XH, XC: local transcoder fills the codecs the revision lacks | measured: DMR clients get transcoded frames (>= 80 % voiced), sut == ref | as xlxd |
| P-XLX-22 | Ingress frame whose size does not match the negotiated revision | disputed | not stated | XH, XC: accepted by size, but the present-codec flags follow the negotiated revision (a short frame marks absent slots as present) | measured: linked as revision 1, 53 B frames: DMR clients get the transcoded D-Star slot, not the sent AMBE+2; linked as revision 3, 27 B frames: DMR clients get all-zero frames | judge slots by the received size |
| P-XLX-23 | Malformed packets (bad size, unknown tag, bad keepalive callsign) are ignored; the link stays | convention (impl) | not stated | XH, XC, UR: dropped by size/form checks | measured: no reply to 5 forms (5 B, `L` without NUL, keepalive with a bad callsign, 60 B DSVT, unknown tag), link kept | as xlxd |

## 4. Streams and routing

| # | Obligation | Level | Docs | Peers (project @ commit: behaviour) | xlxd-ref observed | Recommended for DV-Router |
|---|---|---|---|---|---|---|
| P-XLX-24 | The stream id of an ingress stream is kept on every egress (peers, D-Star/DExtra clients) | convention (impl) | not stated | XH, XC, UR keep it | measured: kept | keep (floor-control interop obligations) |
| P-XLX-25 | A header with stream id 0 opens no stream | convention (impl) | not stated | XH, XC refuse sid 0 | measured: 0 frames | refuse |
| P-XLX-26 | A peer stream whose id is already open on any module is refused | convention (impl); spec B8c | not stated | XH, XC: "stream loop" refusal on any module; UR: not checked | measured: 0 frames (control with another sid: 20/20) | spec B8c (`deny`) |
| P-XLX-27 | Module mapping: the module is the RPT2 module letter, the same letter on both sides; a header for a module that is not shared opens nothing, and nothing is sent to the peer on such a module | convention (impl) | [CONF] "modules shared" | XH, XC: same letter; unshared module dropped on receive, never sent | measured: module H in 0, out 0; D 20/20 | as xlxd |
| P-XLX-28 | No XLX transit: a stream that came from an XLX/BM peer is never sent to an XLX/BM peer (neither back to its origin nor on to another) | disputed (XLX family vs UR) | not stated | XH, XC: XLX ingress is marked peer-origin and the XLX sender sends local-origin streams only; UR: BM ingress is not marked (BM -> BM transit possible with several BM peers) | measured on the wire: nothing back to us, nothing on to XLX998/XLX997, nothing of XLX998's stream to us; own clients 20/20. Exception: a cut stream's closing frame (D8) | never (floor-control interop obligations) |
| P-XLX-29 | Cross-family transit: XLX ingress reaches the reflector's clients and its non-XLX peers (DExtra/DCS/NXDN/P25/USRP) | convention (impl) | not stated | XH, XC: only the XLX sender checks the origin; UR: n/a | measured: D-Star, DMR, M17 clients >= 80 %; XLX998 DMR -> NXDN delivered (test_xlxd_interlink) | allowed by default (knob) |
| P-XLX-30 | A peer stream without a last frame ends after 1.6 s of silence; the module is free afterwards | convention (impl) | not stated | XH, XC, UR: 1.6 s stream timeout | measured: a local talker 0.5 s after the last frame is not heard, 2.2 s after it is | 1.6 s (floor policy) |
| P-XLX-31 | A repeated header of an open stream does not open a second stream | convention (impl) | not stated | XH, XC: only resets the stream timer | measured: one stream, 20/20 frames | as xlxd |
| P-XLX-32 | Voice frames whose stream id had no header | disputed | not stated | XH: held for late-entry promotion; XC: dropped; UR: dropped | measured: 0 frames | not relayed without a header |
| P-XLX-34 | A stream from one XLX peer that is cut on the reflector (a local header while it has no last frame) sends nothing of it to another XLX peer, the closing last frame included | disputed (D8) | not stated | XH: the closing last frame (53 B, the stream's id) goes to the other XLX peers, which never got the header | measured: 1 packet (the last frame) to XLX996, ref and sut | none (dv-router: `closing_frame_to_other_peers` 0) |

## Where the recommendation differs from xlxd

- **D1** (P-XLX-05): check the peer's address as URFd does; xlxd accepts a
  listed callsign from any address and creates a second peer.
- **D2** (P-XLX-14): keepalive and `U` must match address and callsign;
  xlxd selects the peer by address alone.
- **D3** (P-XLX-18, P-XLX-33), resolved with dv-router: zero slots at the
  revision size, also when no transcoder serves the whole stream; not
  27 B frames. Reasons, checked in the source: a receiving xlxd decides
  what to transcode by the link revision, not the frame size (xlxd
  `CXlxClient::GetCodec` / `GetAvailableCodecs` at `a978f03`, the same
  `GetCodec` at `6b7658b`), so 27 B frames on a revision 2/3 link are not
  transcoded there either (measured, P-XLX-22: all-zero DMR frames);
  URFd's BM receiver accepts 45 B frames only; the 27 B fallback was
  removed from xlxd in `6b7658b` (2023-10-27). Only xlxd before that
  commit and URFd's sender still send 27 B. An all-zero Codec2 3200 frame
  is not a defined silence frame (recorded, not judged).
- **D4** (P-XLX-22): as a receiver, judge which slots are present by the
  received frame size; xlxd uses the negotiated revision (a 27 B frame on
  a revision 3 link gives DMR clients zero frames; 53 B on a revision 1
  link has its AMBE+2 slot ignored and re-transcoded).
- **D5** (P-XLX-03): never accept-all on an empty peer list; xlxd acks
  anyone when xlxd.interlink is empty.
- **D6** (P-XLX-09): peer type from configuration, not a callsign prefix.
- **D7** (P-XLX-32): do not relay voice that had no header; xlxd HEAD holds
  it for late-entry promotion (classic xlxd and URFd drop it).
- Not a difference, recorded: the README revision table (0/1 below 2.2.0)
  merges revisions 0 and 1, which xlxd treats differently only in the ack
  of a repeated `L`.

- **D8** (P-XLX-34; seen first in P-XLX-28 / P-XLX-30): when a stream that came from an XLX peer
  is cut (a local header arrives on the module 0.5 s after its last
  frame), xlxd sends that stream's closing last frame (53 B, the peer's
  stream id) to its other XLX peers, which never got the header: a
  one-packet transit, seen by dvcheck `xlx.transit` (warning, on ref and
  sut alike, run `out/20261030T062157Z-i11`). DV-Router: no closing frame
  towards a peer that was never sent the stream.

## Rig

Profile interlink: rig/xlxd-entry.sh adds two rows for the tester
(`DVT_INTERLINK_TESTER`, set to `<net>.30` for xlxd-ref and xlxd-sut in
rig/compose.yml and for prebuilt images in scripts/run.sh):
`XLX996 <net>.30 <DVT_INTERLINK_MODULES>` and `BM3109 <net>.30 <first
module>`. The tester binds UDP 10002 and is XLX996 (or BM3109) towards
both reflectors from one socket (dvtest.proto.xlx.XlxPeer). Both reflectors
then also send `L` to the tester every 5 s while it is not linked.

## Testlist rows

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-XLX-01 | `L` -> `A` with shared modules | test_xlx::test_xlx_connect_ack | diff | probe | covered |
| P-XLX-02 | `A` module list = intersection | test_xlx::test_xlx_ack_module_intersection | dev | probe | covered |
| P-XLX-03 | unlisted callsign -> `N` | test_xlx::test_xlx_unlisted_nack | diff | probe | covered |
| P-XLX-04 | no common module -> `N` | test_xlx::test_xlx_no_common_module_nack | diff | probe | covered |
| P-XLX-05 | listed callsign from another address | test_xlx::test_xlx_wrong_address | dev | probe | covered |
| P-XLX-06 | traffic only after both acks | test_xlx::test_xlx_half_link_no_traffic | diff | probe | covered |
| P-XLX-07 | `L` every 5 s to an unlinked peer | test_xlx::test_xlx_initiates_connect | diff | probe + dvcheck | covered |
| P-XLX-08 | revision from the peer's version (27/45/53 B) | test_xlx::test_xlx_revision_by_version[..] | diff | probe | covered |
| P-XLX-09 | BM-prefixed peer is a DMR peer | test_xlx::test_xlx_bm_peer_class | dev | probe | covered |
| P-XLX-10 | repeated `L`/`A`: one link, traffic once | test_xlx::test_xlx_relink_single | diff | probe | covered |
| P-XLX-11 | keepalive 9 B about every second | test_xlx::test_xlx_keepalive_form | diff | probe + dvcheck | covered |
| P-XLX-12 | silent peer: `U` after 30 s, removed | test_xlx::test_xlx_silent_peer_dropped | diff | probe (slow) | covered |
| P-XLX-13 | `U` removes the link | test_xlx::test_xlx_disconnect | diff | probe | covered |
| P-XLX-14 | keepalive/`U` with another callsign | test_xlx::test_xlx_foreign_callsign_unlink | dev | probe | covered |
| P-XLX-15 | egress header form | test_xlx::test_xlx_egress_header | diff | probe + dvcheck | covered |
| P-XLX-16 | egress voice size and layout | test_xlx::test_xlx_egress_voice_layout | diff | probe + dvcheck | covered |
| P-XLX-17 | transcoded slots filled | test_xlx::test_xlx_egress_slots_transcoded | diff | probe (crossmode) | covered |
| P-XLX-18 | missing slot inside a transcoded stream: zero, frame kept | test_xlx::test_xlx_egress_slot_missing_mid_stream | dev (B6b) | probe (rig_control) | covered |
| P-XLX-19 | last frame form | test_xlx::test_xlx_egress_last_frame | diff | probe | covered |
| P-XLX-20 | revision 3 ingress slots used as given | test_xlx::test_xlx_ingress_rev3_slots | diff | probe | covered |
| P-XLX-21 | revision 1 ingress transcoded locally | test_xlx::test_xlx_ingress_rev1_transcoded | diff | probe (crossmode) | covered |
| P-XLX-22 | size not matching the revision | test_xlx::test_xlx_ingress_size_mismatch | dev | probe | covered |
| P-XLX-23 | malformed packets ignored, link kept | test_xlx::test_xlx_garbage_ignored | diff | probe | covered |
| P-XLX-24 | stream id kept | test_xlx::test_xlx_stream_id_kept | diff | probe | covered |
| P-XLX-25 | stream id 0 refused | test_xlx::test_xlx_sid_zero | diff | probe | covered |
| P-XLX-26 | sid open elsewhere refused | test_xlx::test_xlx_sid_open_elsewhere | spec (B8c) | probe | covered |
| P-XLX-27 | same module letter; unshared module dropped | test_xlx::test_xlx_module_mapping | diff | probe | covered |
| P-XLX-28 | no XLX transit (tester sees the wire) | test_xlx::test_xlx_no_transit | spec (B11) | probe + dvcheck xlx.transit | covered |
| P-XLX-29 | cross-family transit | test_xlx::test_xlx_cross_family | diff | probe | covered |
| P-XLX-30 | 1.6 s stream timeout | test_xlx::test_xlx_stream_timeout | diff | probe | covered |
| P-XLX-31 | repeated header, one stream | test_xlx::test_xlx_repeated_header | diff | probe | covered |
| P-XLX-32 | voice without header | test_xlx::test_xlx_headerless_voice | dev | probe | covered |
| P-XLX-33 | no transcoder for the whole stream: frame form | test_xlx::test_xlx_egress_transcoder_down | dev (B6b) | probe (rig_control) | covered |
| P-XLX-34 | cut XLX stream: no closing frame to another XLX peer | test_xlx::test_xlx_cut_stream_no_closing_frame | dev | probe + dvcheck xlx.transit | covered |
