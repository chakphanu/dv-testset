<!-- SPDX-License-Identifier: MIT -->
# USRP (AllStarLink chan_usrp / DVSwitch): obligations of the reflector side

What a reflector owes a USRP peer: the AllStarLink node (app_rpt with the
`chan_usrp` channel driver) and the DVSwitch tools that speak the same
packets (Analog_Bridge, pyUC, the MMDVM_CM converters). USRP has no client
and no server. It is a symmetric pair of UDP sockets: each side sends PCM
voice frames to a fixed peer address and port. A reflector "endpoint" is
one such pair bound to one module. Each item is numbered `P-USR-NN`; the
conformance cases are listed under "Testlist rows" at the end.

**No written specification exists.** The packet header and the type list
in `chan_usrp.c` are the de-facto definition. DVSwitch extended the type
list and added TLV tags; pyUC's source is the only place that lists them.
The ASL3 manual documents only the dial string. Every Level is therefore
`convention (impl)`, naming the implementations that agree, or `disputed`,
naming who does what. Recommendation priority: client behaviour (what
app_rpt and the DVSwitch tools send and need) > other servers > xlxd.

**xlxd column.** Upstream xlxd (xlxd-ref) has no USRP support. The column
is the xlxd pull-request tree that adds USRP endpoints (repo
chakphanu/xlxd, branch pr/pcm-usrp-slim, the rig's xlxd-sut), measured
on rig instance 2 on 2026-10-30 (transcoder vambed-neo, profile
allstar, `cases/proto/test_usrp.py`: 35 passed); rows still marked
"code:" are covered by the existing B3/B6/B9 cases.

Dissector: `tools/wireshark/dvtest.lua`, protocol `usrp` (filter `usrp`).
Client: `dvtest/proto/usrp.py`. The reference sections at the end (rig
endpoints, frame layout, flows, timing, measured evidence) describe the rig.

## Sources

There are no specs or papers. The written sources are:

- **[ASLDOC]** AllStarLink 3 Manual, "rpt.conf", rxchannel entry for
  `USRP/<ip>:<port>:<port>` ("GNU Radio interface USRP"): repo
  AllStarLink/ASL3-Manual @ `5c4cf485` (2026-10-26), `docs/config/rpt_conf.md`
  L1246-1257, published at https://allstarlink.github.io/config/rpt_conf/.
- **[FORUM]** AllStarLink Community, "USRP channel driver" (2022-03):
  the driver was written by KA1RBI for GNU Radio / USRP SDR use and is now
  used mainly by DVSwitch. https://community.allstarlink.org/t/usrp-channel-driver/19262
- **[AB-INI]** DVSwitch Analog_Bridge configuration comments
  (`Analog_Bridge.ini`, `[USRP]` and `[GENERAL]` sections) and
  `Analog_Bridge.md` (branch development), the only DVSwitch prose about USRP.
- HamVOIP: nothing published was found (search 2026-10-30).

Implementations read. The repos were shallow clones. Behaviour is
described in our own words, with no code copied.

| Key | Repo @ commit (date) | Role |
|---|---|---|
| ASL3 | https://github.com/AllStarLink/app_rpt @ `b6cc1cf1` (2026-10-27), `channels/chan_usrp.c` v0.1.2 | client: app_rpt node, the reference USRP peer (the rig's AllStar node runs ASL 3.10.8) |
| ASL2 | https://github.com/AllStarLink/ASL-Asterisk @ `d6a4b48d` (2024-06-29, archived), `chan_usrp.{c,h}` v0.1.1 | client: older app_rpt nodes still in service |
| AB | https://github.com/DVSwitch/Analog_Bridge @ `dbc4c58f` (2026-07-09, branch bookworm): `Analog_Bridge.ini` comments only (closed source; behaviour not in its docs is marked "not documented") | client: DVSwitch USRP hub (ASL <-> DMR/YSF/D-STAR/NXDN/P25 bridge, DVSwitch Mobile backend) |
| MB | https://github.com/DVSwitch/MMDVM_Bridge @ `285e51e9` (2026-07-09), `dvswitch.sh` v1.6.2 | client tool: builds USRP control and TLV packets for AB |
| PYUC | https://github.com/DVSwitch/USRP_Client @ `0225dd95` (2020-12-12), `pyUC.py` | client: DVSwitch USRP client; the fullest public list of types and TLV tags |
| UAUD | https://github.com/DVSwitch/USRP_Audio @ `b10709cf` (2019-07-18) | client: minimal DVSwitch audio client |
| CM | https://github.com/nostar/MMDVM_CM @ `1dd5383c` (2024-11-06), `USRP2M17`, `USRP2DMR`, `USRP2YSF`, `USRP2P25` | client: USRP <-> digital converters (one fixed peer each) |
| URFd | https://github.com/n7tae/urfd @ `bd0c114e` (2025-12-29), `reflector/USRPProtocol.cpp` | other server: reflector with USRP clients |
| XPR | chakphanu/xlxd pr/pcm-usrp-slim @ `d8ffa415` (2026-10-29), `src/cusrpprotocol.*`, `docs/usrp.md` | the xlxd column (xlxd-sut) |

## How the cases judge

`cases/proto/test_usrp.py` drives the USRP endpoints of the system under
test. The tester is the peer of module C (`xlxd.usrp.in` row C). The rig's
AllStar node (app_rpt + chan_usrp, profile `allstar`) is the real peer of
modules A and B. Because xlxd-ref has no USRP endpoint, the comparison
types change:

- **conv**: an implementation convention that the clients rely on
  (Level column). It is asserted on the system under test for every profile.
- **dev**: disputed between implementations. Under `DVT_SUT_PROFILE=xlxd`
  the value observed on xlxd-sut is recorded and not asserted, because no
  reference exists. Under another profile the profile's spec entry decides
  (`dvtest/specs/<profile>.py`, `"usrp"` dict
  `{"P-USR-NN": {key: value}}`), and the case skips with "spec pending"
  while there is none.
- **spec (Bn)**: judged by the DV-Router behaviour spec (testlist B rows).
- **real**: checked against the real chan_usrp in the AllStar container
  (`asterisk -rx "usrp show"` frame counters via the control channel), not
  against the tester.

Dynamic clients (section 5, `cases/proto/test_usrp_dynamic.py`): no
server in the rig admits them. xlxd does not, and URFd does not either.
Under the `xlxd` profile these cases skip with "xlxd has no dynamic USRP".
Under another profile they run only when its `"usrp"` spec entry has
`"dynamic_server": True`. Until then they skip with "edge not ready". Their
dev items read `SPEC["usrp"]["P-USR-NN"]` as above. `DVT_USRP_DYNAMIC_HUB=<ip:port>` points the
cases at any hub, and its dev items are then judged by
`dvtest/usrp_hub.SPEC`. The unit test `cases/unit/test_usrp_dynamic_hub.py`
starts `dvtest.usrp_hub.FakeHub`, a hub that does what this section
recommends, and runs all 15 cases against it end to end. That proves the
cases before a real dynamic server exists.

Conventions: "USRP" (4 bytes) followed by seven 32-bit fields: seq, memory,
keyup, talkgroup, type, mpxid, reserved (32-byte header). seq and keyup are
big endian everywhere. `type` is written in host order (little endian) by
every implementation that sends a non-zero type (so TEXT = bytes 20-23
`02 00 00 00`). Voice is 160 samples of signed 16-bit little-endian PCM at
8 kHz (320 bytes, 20 ms): a 352-byte frame.

## 1. Frames sent to the peer (egress)

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers | xlxd (PR tree, xlxd-sut) | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-USR-01 | Voice is sent as 352-byte frames: "USRP", type 0, keyup 1, 320 bytes of PCM | convention (impl, agreed) | none (de-facto: ASL3 `chan_usrp.c` header struct L91-101) | ASL3 plays a frame only if it is not TEXT and has exactly 320 payload bytes (usrp_xread L495-516); AB: frame-size rules not documented; CM accepts only 352 B type 0 keyup 1; PYUC accepts any length | URFd sends 352 B keyed | measured 2026-10-29 (i5): every voice frame 352 B, type 0, keyup 1 | as stated |
| P-USR-02 | memory, talkgroup, type, mpxid and reserved are zero in voice frames | convention (impl, agreed) | none (ASL3 comments: memory "or zero (default)", mpxid/reserved "for future use") | ASL3 sends them zero (usrp_xwrite L655-662) and never reads them; CM, PYUC zero | URFd zero | measured 2026-10-29 (i5): zero in every frame; on ingress it requires them zero | zero |
| P-USR-03 | PCM byte order is little endian (a tone arrives as the same tone, not byte-swapped noise) | convention (impl, agreed) | none (ASL3 copies the payload into Asterisk slin frames: host order, little endian on every ASL host) | ASL3, PYUC host order; CM builds LE explicitly | URFd LE | measured 2026-10-30 (i2): 1 kHz in -> 1 kHz share 1.00 read LE, 0.43 read BE | LE |
| P-USR-04 | seq is big endian and counts +1 per packet sent to the peer (unkey included) | convention (impl, agreed on +1); reset point disputed | none | ASL3 counts from 0 and never resets; on receive it logs "Possible data loss" when seq is not the expected value, except when either value is 0 (L489-493); PYUC from 0; CM per process | URFd resets to 0 at each stream | measured 2026-10-29 (i5): per endpoint, +1 always, continuous across overs | continuous per endpoint (ASL3 logs no false loss; a reset to 0 is also tolerated) |
| P-USR-05 | Voice is paced in real time (about 20 ms per frame); never more than 25 frames ahead of play-out | convention (client constraint) | none | ASL3 plays one queued frame per app_rpt write (20 ms) and flushes its whole queue when it holds more than 25 frames (L81, L571-580), so a burst > 500 ms loses audio | URFd sends as frames arrive from its transcoder | measured 2026-10-30 (i2): median gap 20.1 ms (min 0, max 40 ms: transcoder jitter); worst play-out queue 1 frame, far below the 25-frame flush | pace at 20 ms; absorb jitter on the sending side |
| P-USR-06 | A transmission ends with exactly one 32-byte header-only frame: keyup 0, type 0, seq continuing | convention (impl, agreed: ASL3, CM, URFd, XPR; AB not documented); PYUC differs | none | ASL3 sends one 32-byte unkey on RADIO_UNKEY (usrp_indicate L381-415); AB: how it detects the end is not documented (its ini sets a 2.5 s minimum TX time, `minTxTimeMS` [AB-INI]); CM treats any 32-byte "USRP" frame as end of transmission and ignores a 352-byte keyup-0 frame; PYUC sends a 352-byte keyup-0 frame as its unkey but accepts both | URFd one 32-byte unkey | measured 2026-10-30 (i2): exactly one per over, last packet | one 32-byte unkey per transmission |
| P-USR-07 | The unkey is also sent when the source stream ends without a terminator (lost last frame, source timeout, source disconnect, backend loss) | convention (client constraint); timing judged by B3 | none | CM has no USRP-side receive timeout, so it stays keyed without the unkey; AB not documented; ASL3 unkeys on silence anyway | URFd sends the unkey when the stream closes (1.6 s) | measured 2026-10-30 (i2): one unkey 100 ms after the last voice | always send the unkey when an egress transmission stops, whatever the cause |
| P-USR-08 | Nothing is sent to an idle peer, and never a 352-byte frame that is not voice (no PING, no 352-byte SET_INFO typed other than TEXT) | convention (client constraint) | none | ASL3 sends nothing when idle (KEEPALIVE_TIME is defined but unused, L78-79) and plays any non-TEXT 320-byte payload as audio; PYUC sends PING (type 3) only as an optional NAT keepalive (`pingTimer`, default off), so no DVSwitch peer needs unsolicited packets | URFd sends nothing when idle | measured 2026-10-30 (i2): 0 packets in 20 s | nothing when idle |
| P-USR-09 | Talker metadata (TEXT frame carrying TLV SET_INFO, tag 8: ids + callsign) before the voice | disputed | none (tags only in PYUC L68-79) | CM and URFd send a 352-byte TEXT SET_INFO before every over; AB sends it when `exportMetadata = true` [AB-INI]; ASL3 turns any TEXT frame into an Asterisk text frame that app_rpt forwards to every linked node (app_rpt `rpt_do_sendall`), so SET_INFO toward an ASL node is sent on as stray text | URFd sends it (callsign only, ids zero) | measured 2026-10-30 (i2): no TEXT frame | off by default; a per-endpoint option for DVSwitch peers (spec item) |

## 2. Frames received from the peer (ingress)

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers | xlxd (PR tree, xlxd-sut) | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-USR-10 | The first keyed 352-byte voice frame opens a stream on the endpoint's module; its PCM reaches the module's digital clients transcoded | convention (impl, agreed) | none | ASL3 sends keyup 1 voice while app_rpt transmits; CM and PYUC the same | URFd opens a stream on the first voice frame | measured 2026-10-30 (i2): one stream, 50/50 frames to D-STAR | as stated |
| P-USR-11 | A 32-byte unkey ends the stream promptly; the module's listeners get the end of stream (terminator / last frame) | convention (impl, agreed) | none | ASL3 sends exactly one unkey per over; CM and UAUD send 32-byte unkeys | URFd only logs it and closes on its 1.6 s timeout (disagrees) | measured 2026-10-30 (i2): D-STAR last frame 213 ms after the unkey (transcoder latency included) | end at once |
| P-USR-12 | A 352-byte voice frame with keyup 0 (pyUC's unkey, which carries audio) | disputed | none | PYUC sends its unkey this way and treats keyup 0 as unkey; AB not documented; ASL3 ignores keyup and would play it; CM ignores the frame | URFd ignores it (stream ends on its timeout) | measured 2026-10-30 (i2): dropped (one stream, 49 of 50 frames) | treat it as unkey (drop its audio, end the stream) |
| P-USR-13 | Voice that stops without an unkey ends the stream after a silence timeout | convention (impl, agreed on ending); timeout disputed, judged by B3 | none | ASL3 always sends one unkey, which can be lost; ASL3 itself unkeys about 60-80 ms after the last frame; CM has no timeout; AB not documented | URFd 1.6 s | code: 100 ms | B3: 100 ms |
| P-USR-14 | seq gaps, reordering and a non-zero first seq never drop voice or split the stream | convention (impl, agreed: only ASL3 checks, and only logs) | none | ASL3 continues its counter across overs and restarts it from 0 after an Asterisk restart | URFd ignores seq | measured 2026-10-30 (i2): gap, start 100000+, swap: one stream, 50/50 each | ignore seq on ingress (record gaps for stats only) |
| P-USR-15 | Packets from any address other than the configured peer ip:port are ignored | convention (servers agree: URFd, XPR, CM); clients themselves accept anyone | none (security: USRP has no authentication) | ASL3 sends from its bound MYPORT, so an exact ip:port match works with it; ASL3 and PYUC accept packets from any source (PYUC even follows the last sender); PYUC registers with its hub by a TEXT "REG:DVSWITCH" and expects "REG:OK" back | URFd and CM match ip and port exactly | measured 2026-10-30 (i2): other port ignored, configured port works after | exact ip:port |
| P-USR-16 | TEXT, DTMF, PING, TLV, VOICE_ADPCM/ULAW, wrong-size and non-"USRP" packets open no stream and play no audio; the endpoint keeps working | convention (impl, agreed) | none | PYUC sends PING (type 3) when its NAT timer is on, TEXT "REG:DVSWITCH"/SET_INFO, DTMF (macros), TLV; MB sends TLV remote commands; ASL3 sends TEXT/DTMF never | URFd logs "Unknown USRP packet" | measured 2026-10-30 (i2): all 10 kinds open no stream, no reply; next over works | drop; never answer |
| P-USR-17 | A PING (type 3) gets no reply | convention (impl, agreed: URFd ignores it; PYUC expects no answer) | none | PYUC uses PING only as a NAT keepalive and expects nothing back | URFd ignores | measured 2026-10-30 (i2): no reply (padded and unpadded) | no reply |
| P-USR-18 | Ingress TEXT SET_INFO (tag 8) names the talker for the next over (callsign instead of the endpoint's own) | disputed | none | AB (exportMetadata), CM and URFd send it before voice; PYUC sends it after "REG:OK" | URFd uses the callsign | measured 2026-10-30 (i2): ignored, D-STAR MY = N0TEST (the row's callsign) | use the SET_INFO callsign when it is a valid callsign, else the endpoint's own (spec item) |

## 3. Routing

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers | xlxd (PR tree, xlxd-sut) | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-USR-19 | A stream from a USRP endpoint is never sent back to that endpoint | convention (impl, agreed) | none | an echo would be heard by every radio on the ASL node | URFd skips the client that is streaming in | code: no echo (also B9) | no echo |
| P-USR-20 | A digital stream on the module reaches the endpoint as keyed voice (transcoded), whatever its source protocol | convention (impl, agreed) | none | ASL3 expects voice frames while the reflector "transmits" | URFd | measured 2026-10-30 (i2): D-STAR 50, DMR 51, M17 51 keyed frames, one unkey each | as stated |
| P-USR-21 | With several endpoints, each gets only its own module's streams, and only once | convention (impl, agreed) | none | each ASL node has its own channel / port pair (up to 16 per Asterisk, ASL3 MAX_CHANS) | URFd one module for all USRP clients (each client gets the stream) | measured 2026-10-30 (i2): module C over -> tester 50 frames once, chan_usrp A/B read +0 | as stated |
| P-USR-22 | The endpoint is permanent: no login, no keepalive, and no idle timeout ever drops it; after hours of silence or a peer restart (seq back to 0) voice works at once | convention (client constraint) | none | ASL3 sends nothing between overs and restarts its seq at 0 | URFd keeps static USRP clients alive internally | measured 2026-10-30 (i2): works after 10 s idle and a seq restart at 0 | as stated |
| P-USR-23 | A USRP over on a module busy with another talker is not mixed in | spec (B3: busy module suppresses a USRP over) | none | - | URFd one stream per module | code: first talker keeps the module | B3 |
| P-USR-24 | No fabricated audio: when the transcoder is lost, no PCM (silence/zeros) is sent to the peer | spec (B6) | none | silence keeps an ASL node keyed | - | code: nothing sent without transcoder output | B6 |

## 4. Real-client checks (profile allstar)

| # | Obligation | Level | Spec / paper | Clients | Other servers | xlxd (PR tree, xlxd-sut) | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-USR-25 | An over from a digital client reaches app_rpt: chan_usrp's read counter rises by the frames sent, and it reports no wrong-size frames | convention (client) | [ASLDOC] rxchannel = USRP/ip:port:port | ASL3 (the rig node) | - | measured 2026-10-30 (i2): 50 frames sent -> chan_usrp Read +51 (incl. unkey) | as stated |
| P-USR-26 | An over from app_rpt (localplay on node 1901) opens a stream on module A and ends with the single unkey | convention (client) | [ASLDOC] | ASL3 (the rig node) | - | measured 2026-10-30 (i2): chan_usrp Write +71 -> one D-STAR stream of 71 frames, ended | as stated |

## 5. Dynamic clients (no configured row)

A dynamic client is a USRP client the server has no configured row for:
DVSwitch Mobile (DVSM, Android), pyUC and qtUC. These clients were built
to talk to a DVSwitch hub, Analog_Bridge or, since 2021, Analog_Reflector.
They open the session themselves from wherever they are, often from
behind NAT. No document specifies this exchange. pyUC's source is the
only open description. DVSM is closed source, and its two guides describe
only its settings. Of the reflectors, URFd admits no dynamic client:
its USRP clients come only from configuration and must match ip:TxPort
exactly. xlxd has none either (column "n/a"). The one open reflector
found that admits unknown senders is rusrp, which takes them on their
first packet with no reply.

Additional sources for this section:

| Key | Source (version / commit, date) | Role |
|---|---|---|
| QTUC | https://github.com/greythane/qtUC @ `79461d0` (2021-08-20) | client: pyUC-derived; corroborates pyUC's exchange |
| DVSMG | "DVSM DVSwitch Mobile Installation and Setup", revised 2020-02-17 (app v1.53), https://dvswitch.org/Documents/DVSM_02_17_2020.pdf | official DVSM guide (USRP account fields) |
| DVSIG | "DVSwitch Installation and User Guide" v1.61 (2020-11-14), https://dvswitch.org/Documents/DVSwitch%20Installation%20and%20User%20Guide%20v1.61.pdf, appendix A (DVSM) and B (pyUC) | official guide |
| ARDOC | "The DVSwitch Analog_Reflector and DVSwitch HTML Client (hUC)", document version 0.13, https://dvswitch.org/Analog%20Reflector/Analog_Reflector.html; config https://github.com/DVSwitch/Analog_Reflector @ `b63717b` | official hub doc: AR took over "servicing analog clients" from Analog_Bridge |
| PLAY | Google Play listing `org.dvswitch` "DVSwitch Mobile" (updated 2026-09-04) | app description: IAX and USRP accounts, registration tones, automatic reconnect |
| RUSRP | https://github.com/n0mjs710/rusrp @ `2af46444` (2026-08-06), `src/reflector.c` | other server: open USRP reflector, admits on first packet |
| SVXU | https://github.com/dl1hrc/svxlink branch svxlink-usrp @ `769d39b`, `usrp_protocol.txt` | third party: notes made from Analog_Bridge 1.6.2 packet dumps (not normative) |

| # | Obligation | Level | Spec / paper | Clients (send / expect / tolerate) | Other servers | xlxd | Recommended for DV-Router |
|---|---|---|---|---|---|---|---|
| P-USR-27 | A new sender that registers with TEXT "REG:DVSWITCH" is admitted and gets TEXT "REG:OK" back | convention (DVSwitch clients) | none | PYUC/QTUC send "REG:DVSWITCH" (TEXT, unpadded) at start and keep their Transmit button disabled until "REG:OK" arrives (PYUC L462-470, L1257); without it a user cannot talk; no retry timer (re-sent on Connect); DVSM: wire exchange not documented, the guides show a "registration" status and tone | URFd, rusrp: never answer (URFd ignores unknown senders; rusrp admits silently) | n/a | admit and answer "REG:OK" (TEXT, type 2 in host order) |
| P-USR-28 | Everything for a dynamic client goes to the source ip:port of its datagrams | convention (client constraint) | none | PYUC with equal rx/tx ports (the default) sends from an ephemeral port and binds nothing (L325-337), so only replies to the source port arrive; DVSM is used behind carrier NAT (guides forward only the server port); PYUC and QTUC also adopt the source IP of anything they receive | rusrp replies to the source ip:port; URFd sends to the configured ip:TxPort only | n/a | source ip:port, learned per datagram from a registered client |
| P-USR-29 | Voice from an unknown address that never registered | disputed | none | PYUC in `aslMode` skips REG (meant for chan_usrp peers); ASL3 never registers (it needs a configured row) | rusrp admits it; URFd, XPR drop it; AR has `disallowUnknownClients` (unknown clients then receive only, or are disconnected) [ARDOC] | n/a (unknown senders dropped) | ignore (no stream) unless a row is configured; spec item |
| P-USR-30 | "REG:UNREG" from a client ends its session (no further traffic to it) | convention (DVSwitch clients) | none | PYUC/QTUC send it on exit (L1471-1478) and treat a "REG:UNREG" from the hub as disconnected | rusrp: no such message (TTL only) | n/a | remove the client; send nothing more |
| P-USR-31 | A registered client that goes silent is dropped after a timeout; it can register again at once | disputed (timeout) | none | PYUC sends PING (type 3, "PING") every 20 s only if `pingTimer` > 0 (default off, L245-248); otherwise an idle registered pyUC sends nothing; DVSM reconnects automatically (PLAY); on server exit AR sends every client an unregister and DVSM/hUC reconnect [ARDOC] | rusrp 30 s after the last packet; URFd never drops | n/a | drop after a long silence (spec item; must outlast a PING-less idle client, e.g. minutes), send "REG:UNREG" when dropping and on shutdown |
| P-USR-32 | A PING from a registered client keeps it registered and gets no reply | convention (impl) | none | PYUC expects nothing back; it reads a hub PING whose seq follows the last voice seq as "missed end of transmission" (L558-564) | rusrp: any packet refreshes the TTL | n/a | refresh, no reply |
| P-USR-33 | The client picks its module: DTMF dial string (and TLV REMOTE_CMD "tgs=" / "txTg=") | disputed (encoding) | none | PYUC sends REMOTE_CMD "tgs=<tg>" plus a DTMF packet carrying the dial string (TG digits, "<id>#" private, D-STAR "REF030CL"); mode change "*<MODE>" (DTMF) | URFd one fixed module for all USRP clients; rusrp one conference | n/a | dial string = module letter or 4001..4026 (A..Z, as the XLX DMR convention); confirm with "INFO:MSG:<text>"; spec item |
| P-USR-34 | Module of a new client before it selects one | disputed | none | PYUC sends no selection until the user picks a TG | URFd: its configured module | n/a | none (receive nothing until a module is picked) or a configured default; spec item |
| P-USR-35 | The client's SET_INFO (sent after "REG:OK") names it as talker | convention (DVSwitch clients) | none | PYUC/QTUC send SET_INFO (own DMR id + callsign, unpadded TEXT) once after REG:OK | URFd accepts SET_INFO from its configured client (callsign) | n/a | use its callsign (same as P-USR-18) |
| P-USR-36 | Each call to a dynamic client starts with SET_INFO (talker callsign / ids) | convention (DVSwitch clients) | none | PYUC shows "call -> TG" from it and blocks its own TX until the call ends (a keyup-0 frame, or the PING rule), i.e. half duplex (L517-547); DVSM shows caller id | URFd sends a 352-byte SET_INFO before each stream (configured clients) | n/a | send SET_INFO (TEXT, padded to 352 B) before each call to a dynamic client; not to static ASL rows (P-USR-09) |
| P-USR-37 | "INFO:" request | disputed (optional) | none | PYUC asks once after REG:OK and fills its mode box and "Connected to" from the JSON reply (keys `tlv.ambe_mode`, `last_tune`); it works without a reply | none answer | n/a | answer with a small JSON (current module); optional, spec item |
| P-USR-38 | Several dynamic clients: each gets its module's streams once, never its own; one talker per module | convention (impl) | none | PYUC is half duplex and expects the hub to arbitrate | rusrp: first keyed sender holds the floor, no echo; AR: many clients share one resource | n/a | as stated (floor rules B1/B3, echo B9) |
| P-USR-39 | Audio to a dynamic client is SLIN (type 0, 320 B) unless it asked otherwise | convention (impl) | none | PYUC plays only type 0 / 320 B; types 5/6 are defined but never played; DVSM "Codec Types" must stay "Server Selected" [DVSMG p.13] | URFd, rusrp SLIN (rusrp adds its own Opus type 7) | n/a | SLIN; ADPCM/ULAW only on request (out of scope) |
| P-USR-40 | Admission limits: no authentication exists on the wire; a server bounds the client table and can refuse | disputed | none (XPR `docs/usrp.md`: "no login and no authentication") | identity is self-declared (SET_INFO); AR's user/password list covers hUC; how DVSM sends credentials over UDP is undocumented | rusrp 16 slots, full table blocks newcomers until TTL; AR rights mask (transmit, tune) | n/a | bounded table; refuse with "REG:UNREG" when full; optional callsign allow-list; spec item |

## Differences from xlxd (PR tree)

Where the recommendation differs from what xlxd-sut did on the rig:

1. P-USR-12: pyUC's 352-byte keyup-0 unkey is dropped as a malformed
   frame. The over carries on as one stream with 49 of 50 frames, and the
   stream ends only on the 100 ms timeout. Recommended: treat it as unkey.
2. P-USR-18: ingress SET_INFO is ignored. The talker stays the row's
   callsign. Recommended: use the SET_INFO callsign.
3. P-USR-09: xlxd never sends SET_INFO. Recommended: the same by
   default, plus an opt-in per endpoint for DVSwitch peers.
4. The PR's `docs/usrp.md` says every header field is big endian. That
   is only true for type 0; implementations write non-zero types in host
   order. This is a doc error, not a wire issue.

Agreed with the recommendation: pacing (P-USR-05: at most 1 frame ahead
of play-out), the single unkey (P-USR-06/07), idle silence (P-USR-08),
seq handling (P-USR-04/14), source filtering (P-USR-15) and non-voice
drops (P-USR-16/17).

## Reference: USRP in xlxd (PR tree)

Raw 8 kHz PCM over UDP between an AllStarLink (app_rpt) node and xlxd. Each
xlxd module with a USRP endpoint binds its own UDP port and talks to exactly
one peer; there is no login or keepalive. xlxd turns received PCM into a
stream on the module (transcoded to AMBE+/AMBE2+/Codec2 through the extended
AMBEd open with PCM input) and sends PCM from other modes' streams back.

## Reference: rig endpoints (`rig/config/xlxd.usrp.in`)

| Module | xlxd-sut bind | Peer | Peer |
|--------|---------------|------|------|
| A | 172.30.0.11:34001 | 172.30.0.40:32001 | AllStar node 1901 (profile allstar) |
| B | 172.30.0.11:34002 | 172.30.0.40:32002 | AllStar node 1902 (profile allstar) |
| C | 172.30.0.11:34003 | 172.30.0.30:32003 | tester (no AllStar needed) |

All three use source callsign N0TEST. The file is rendered only when
`DVT_SUT_USRP=on` (`scripts/run.sh --profile allstar` sets it).

## Reference: frame layout

Header, 32 B. xlxd writes and expects every field big endian; for the
only type it uses (0) the byte order makes no difference. Other
implementations write a non-zero type in host order (see Conventions above):

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 0 | 4 | ASCII | eye `USRP` |
| 4 | 4 | u32 BE | sequence, +1 per frame sent on this endpoint |
| 8 | 4 | u32 BE | memory, 0 |
| 12 | 4 | u32 BE | keyup: 1 = PTT on (voice follows), 0 = unkey |
| 16 | 4 | u32 BE | talkgroup, 0 |
| 20 | 4 | u32 BE | type: 0 voice, 1 DTMF, 2 text, 3 ping, 4 TLV, 5 ADPCM, 6 ulaw |
| 24 | 4 | u32 BE | mpxid, 0 |
| 28 | 4 | u32 BE | reserved, 0 |

Payload of a keyed voice frame (type 0, keyup 1):

| Offset | Size | Type | Meaning |
|--------|------|------|---------|
| 32 | 320 | s16 LE x 160 | 20 ms of PCM, 8 kHz mono |

| Frame | Size |
|-------|------|
| voice (keyup 1) | 352 B |
| unkey (keyup 0) | 32 B |

xlxd accepts only voice frames: size 32 or 352, every header byte from
offset 8 zero except the keyup low byte (offset 15) which is 0 or 1, and
keyup 1 exactly when the size is 352. Anything else (DTMF, text, TLV) is
dropped and logged once per endpoint. It sends frames in the same strict
form.

## Reference: message flows

AllStar (or tester) keys up into xlxd module C:

```
peer (32003)                            xlxd-sut (34003)
 |-- USRP seq=n keyup=1 + PCM (352) --->|   first frame opens a stream
 |-- USRP seq=n+1 keyup=1 + PCM ------->|   every 20 ms
 |   ...                                 |
 |-- USRP seq=m keyup=0 (32) ---------->|   unkey ends the stream
 |   (or 100 ms without voice ends it)  |
```

Another mode talks on the module, xlxd keys the peer:

```
peer (32003)                            xlxd-sut (34003)
 |<-- USRP seq=k keyup=1 + PCM (352) ---|   PCM from the transcoder
 |<-- ... every 20 ms ------------------|
 |<-- USRP seq=j keyup=0 (32) ----------|   last frame, or 100 ms dry
```

xlxd never echoes a stream back to the USRP peer it came from.

## Reference: timing (xlxd side)

From the xlxd source (`main.h`, `ctranscoder.cpp`, `cusrpprotocol.cpp`); the
frame and PING periods are confirmed in the Evidence section.

| Item | Value |
|------|-------|
| frame period | 20 ms |
| RX: no voice for | 100 ms = end of stream |
| TX: no PCM for | 100 ms = send unkey |

## Reference: evidence (2026-10-29, instance 5)

Run `out/20261029T153832Z-i5` (`TRANSCODER_IMAGE=vambed-neo:local
DVT_INSTANCE=5 scripts/run.sh --capture --profile allstar -m "wire or rig or
allstar"`; transcoder, xlxd trees and result as in
[ambed.md](ambed.md#evidence); rig instance 5, so addresses are `172.30.5.x`).

```sh
scripts/tshark.sh -r out/20261029T153832Z-i5/capture/xlxd-sut.pcapng -Y "usrp && !icmp" \
  -T fields -e ip.src -e udp.srcport -e ip.dst -e udp.dstport -e udp.length \
  -e usrp.seq -e usrp.memory -e usrp.keyup -e usrp.talkgroup -e usrp.type \
  -e usrp.mpxid -e usrp.reserved -e usrp.pcm_peak
```

xlxd-sut.pcapng (whole run), per direction:

| Direction | Voice 352 B | Unkey 32 B | Seq | Voice period (median) |
|-----------|-------------|------------|-----|-----------------------|
| xlxd .11:34001 -> node .40:32001 | 303 | 3 | 0..305, always +1 | 20.1 ms |
| xlxd .11:34002 -> node .40:32002 | 483 | 8 | 0..490, always +1 | 20.1 ms |
| xlxd .11:34003 -> tester .30:32003 | 50 | 1 | 0..50, always +1 | 20.1 ms |
| node .40:32001 -> xlxd .11:34001 | 341 | 2 | 0..342, always +1 | 20.0 ms |
| node .40:32002 -> xlxd .11:34002 | 155 | 1 | 0..155, always +1 | 20.0 ms |
| tester .30:32003 -> xlxd .11:34003 | 100 | 2 | two talks, 0..50 each | 20.0 ms |

- All 1449 frames are type 0 (voice): 1432 of 352 B with keyup 1 and 17 of
  32 B with keyup 0, no other size. Memory, talkgroup, mpxid and reserved
  are 0 in every frame from xlxd, AllStar and the tester.
- The sequence runs on per endpoint and direction across key/unkey cycles
  (xlxd: one counter per endpoint for its whole life; AllStar likewise); the
  tester starts at 0 per talk.
- 1302 of 1432 voice frames carry signal (`usrp.pcm_peak` > 0).
- allstar.pcapng + allstar.2.pcapng (the crash case kills and restarts the
  node) show the same frames from the node side, same seq rules; the node's
  32001 stream there has 335 + 2 frames because the kill cuts the first file.

```
USRP (app_rpt chan_usrp), voice seq=0 keyup=1 peak=0
    Eye: USRP
    Sequence: 0
    Memory: 0
    Keyup: 1
    Talkgroup: 0
    Type: voice (0)
    MPX id: 0
    Reserved: 0x00000000
USRP (app_rpt chan_usrp), unkey seq=100
    Keyup: 0
```

Expert info: no USRP errors or warnings in xlxd-sut.pcapng or the allstar
files. Not observed: DTMF, text, TLV, ping, ADPCM and ulaw frames (neither
xlxd nor the rig AllStar node sends them; xlxd drops them on receive).

## Testlist rows

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-USR-01 | Egress voice 352 B, type 0, keyup 1 | test_usrp::test_usrp_egress_voice_frame | conv | probe + dvcheck | covered |
| P-USR-02 | Egress spare fields zero | test_usrp::test_usrp_egress_fields_zero | conv | probe + dvcheck | covered |
| P-USR-03 | Egress PCM little endian (tone in = tone out) | test_usrp::test_usrp_pcm_byte_order | conv | probe (tone, Goertzel) | covered |
| P-USR-04 | Egress seq BE, +1 per packet; across overs continuous or reset | test_usrp::test_usrp_egress_seq | conv + dev | probe + dvcheck | covered |
| P-USR-05 | Egress paced ~20 ms, never > 25 frames ahead | test_usrp::test_usrp_egress_pacing | conv | probe (arrival times) | covered |
| P-USR-06 | One 32-byte unkey per transmission | test_usrp::test_usrp_single_unkey | conv | probe + dvcheck | covered |
| P-USR-07 | Unkey also after a source stream without last frame | test_usrp::test_usrp_unkey_after_lost_source_end | conv (timing B3) | probe | covered |
| P-USR-08 | Nothing sent to an idle peer (20 s) | test_usrp::test_usrp_idle_silent | conv | probe | covered |
| P-USR-09 | Egress SET_INFO before voice | test_usrp::test_usrp_egress_set_info | dev | probe | covered |
| P-USR-10 | Keyed voice opens a stream, transcoded to digital clients | test_usrp::test_usrp_ingress_opens_stream | conv | probe | covered |
| P-USR-11 | 32-byte unkey ends the stream (end within 0.5 s) | test_usrp::test_usrp_ingress_unkey_ends_stream | conv | probe | covered |
| P-USR-12 | 352-byte keyup-0 frame (pyUC unkey) | test_usrp::test_usrp_ingress_keyup0_voice | dev | probe | covered |
| P-USR-13 | Silence without unkey ends the stream | test_floor::test_usrp_unkey_after_silence (existing) | spec (B3) | probe | covered |
| P-USR-14 | Seq gap / non-zero start / swap: no loss, one stream | test_usrp::test_usrp_ingress_seq_ignored[gap\|start\|reorder] | conv | probe | covered |
| P-USR-15 | Other source port ignored | test_usrp::test_usrp_ingress_foreign_source | conv | probe | covered |
| P-USR-16 | Non-voice and malformed ingress ignored, endpoint keeps working | test_usrp::test_usrp_ingress_non_voice_ignored[adpcm\|dtmf\|eye\|long\|ping\|set_info\|short\|text\|tlv\|ulaw] | conv | probe | covered |
| P-USR-17 | PING gets no reply | test_usrp::test_usrp_ping_no_reply | conv | probe | covered |
| P-USR-18 | Ingress SET_INFO names the talker | test_usrp::test_usrp_ingress_set_info_callsign | dev | probe | covered |
| P-USR-19 | No echo to the origin endpoint | test_allstar::test_usrp_no_echo (existing) | conv (B9) | probe | covered |
| P-USR-20 | Digital stream reaches the endpoint as voice | test_usrp::test_usrp_egress_from_digital[dstar\|dmr\|m17] | conv | probe | covered |
| P-USR-21 | Several endpoints: own module only, once | test_usrp::test_usrp_endpoints_isolated | conv + real | probe (chan_usrp counters) | covered |
| P-USR-22 | Permanent endpoint: works after idle and a peer seq restart | test_usrp::test_usrp_endpoint_permanent | conv | probe | covered |
| P-USR-23 | Busy module suppresses a USRP over | test_floor::test_usrp_over_suppressed_when_busy (existing) | spec (B3) | probe | covered |
| P-USR-24 | No fabricated PCM without a transcoder | test_backend_loss::test_usrp_pcm_not_fabricated (existing) | spec (B6) | probe | covered |
| P-USR-25 | Digital over reaches app_rpt (chan_usrp read counter) | test_usrp::test_usrp_real_asl_receives | real | probe (control channel `asl usrp-show`) | covered |
| P-USR-26 | app_rpt over opens and closes a stream on module A | test_usrp::test_usrp_real_asl_sends | real | probe (control channel `asl play-1901`) | covered |
| P-USR-27 | REG:DVSWITCH admitted, REG:OK to the source | test_usrp_dynamic::test_usrp_dyn_register | conv | probe | covered (fake hub) |
| P-USR-28 | Replies to the source ip:port (ephemeral port) | test_usrp_dynamic::test_usrp_dyn_reply_to_source | conv | probe | covered (fake hub) |
| P-USR-29 | Unregistered voice from an unknown address | test_usrp_dynamic::test_usrp_dyn_unregistered_voice | dev | probe | covered (fake hub) |
| P-USR-30 | REG:UNREG ends the session | test_usrp_dynamic::test_usrp_dyn_unregister | conv | probe | covered (fake hub) |
| P-USR-31 | Silent registered client dropped after the timeout; re-register works | test_usrp_dynamic::test_usrp_dyn_idle_timeout | dev | probe | covered (fake hub; slow) |
| P-USR-32 | PING keeps the client, no reply | test_usrp_dynamic::test_usrp_dyn_ping_keeps | conv | probe | covered (fake hub; slow) |
| P-USR-33 | Module selection by dial string | test_usrp_dynamic::test_usrp_dyn_select_module[letter\|tg] | dev | probe | covered (fake hub) |
| P-USR-34 | Module before any selection | test_usrp_dynamic::test_usrp_dyn_default_module | dev | probe | covered (fake hub) |
| P-USR-35 | Client SET_INFO names the talker | test_usrp_dynamic::test_usrp_dyn_set_info_in | conv | probe | covered (fake hub) |
| P-USR-36 | SET_INFO before each call to the client | test_usrp_dynamic::test_usrp_dyn_set_info_out | conv | probe | covered (fake hub) |
| P-USR-37 | INFO: request | test_usrp_dynamic::test_usrp_dyn_info | dev | probe | covered (fake hub) |
| P-USR-38 | Several dynamic clients: once each, no echo, one talker | test_usrp_dynamic::test_usrp_dyn_two_clients | conv | probe | covered (fake hub) |
| P-USR-39 | SLIN to dynamic clients | test_usrp_dynamic::test_usrp_dyn_slin | conv | probe | covered (fake hub) |
| P-USR-40 | Table bound: refusal when full | test_usrp_dynamic::test_usrp_dyn_table_full | dev | probe | covered (fake hub) |
