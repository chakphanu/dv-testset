<!-- SPDX-License-Identifier: MIT -->
# Protocol coverage matrix

One row per protocol obligation that a doc under `docs/` states, mapped to the
test function(s) that ASSERT it. A case counts only if it asserts the
obligation; a case that merely exercises the path, or only records an observed
value as data, does not make a row OK. Keep it from rotting with
`scripts/coverage-check.py` (checks that every case id still exists and that
the summary below matches the rows; `--write` regenerates the summary; the
unit test `cases/unit/test_coverage_matrix.py` runs it).

## Summary

<!-- summary:begin -->
| Protocol | Rows | OK | PARTIAL | GAP | NOT-IMPL |
|---|---|---|---|---|---|
| DCS | 47 | 43 | 3 | 1 | 0 |
| DExtra | 54 | 35 | 19 | 0 | 0 |
| DPlus | 36 | 34 | 2 | 0 | 0 |
| G3 | 28 | 21 | 5 | 2 | 0 |
| D-STAR slow data | 76 | 39 | 28 | 9 | 0 |
| DP7 (D-PRS positions) | 39 | 33 | 2 | 4 | 0 |
| Stream registry (LEAK2) | 10 | 6 | 2 | 2 | 0 |
| Config load (COMPAT1) | 19 | 18 | 0 | 1 | 0 |
| Secret-file reload (SECRELOAD1) | 18 | 15 | 1 | 2 | 0 |
| Session rotation (WEBSID1) | 7 | 3 | 2 | 2 | 0 |
| M17 | 86 | 65 | 20 | 1 | 0 |
| M17 v3 | 29 | 0 | 25 | 4 | 0 |
| IMRS | 24 | 18 | 5 | 1 | 0 |
| XLX interlink | 34 | 32 | 2 | 0 | 0 |
| AMBEd | 34 | 16 | 15 | 3 | 0 |
| USRP | 41 | 34 | 6 | 1 | 0 |
| Transcoder levels | 12 | 4 | 8 | 0 | 0 |
| MMDVM / Homebrew DMR | 43 | 23 | 20 | 0 | 0 |
| DMRplus | 35 | 16 | 19 | 0 | 0 |
| YSF | 37 | 29 | 8 | 0 | 0 |
| NXDN | 43 | 37 | 6 | 0 | 0 |
| NXDN GPS | 47 | 17 | 18 | 11 | 1 |
| P25 | 37 | 27 | 10 | 0 | 0 |
| IPSC (phase 0, preparation) | 46 | 14 | 6 | 24 | 2 |
| IAX2 / AllStar | 113 | 65 | 33 | 15 | 0 |
| SvxReflector (SVX) | 81 | 8 | 0 | 68 | 5 |
| netns vs DV-Router classification | 45 | 40 | 4 | 1 | 0 |
| Built-in echo (ECHO1) | 27 | 19 | 2 | 6 | 0 |
| Echo return integrity E9 (D-STAR kinds) | 14 | 13 | 1 | 0 | 0 |
| Real-transcoder cross-mode audio into YSF (related to ECHO1) | 3 | 3 | 0 | 0 | 0 |
| DP15 (APRS / AX.25 / KISS data path) | 99 | 70 | 6 | 23 | 0 |
| DP6c / DP19 (weather, telemetry, locator status) | 22 | 17 | 1 | 4 | 0 |
| **Total** | **1286** | **814** | **279** | **185** | **8** |
<!-- summary:end -->

## Top 15 gaps (ranked by how much real-world traffic they touch)

GAP, NOT-IMPLEMENTED and the weakest PARTIAL rows, most traffic first. The
reason is one line each; the rows are in the section named.

1. No real upstream client talks to the SUT for D-STAR, DMR, YSF or M17 (client `a` on nearly every row of those sections): MMDVMHost, DStarGateway, YSFGateway and m17-fme behaviour is only imitated by our own clients, and these four carry most of the traffic.
2. D-STAR slow data / GPS rules on the SUT (D-STAR slow data, DP5f, 13 stage 1c rules): only a Python model of the DV-Router code is tested, a bad CRC or over-long line is never sent to the SUT; text and position ride on every D-STAR over.
3. 88 rows (DMR 19, DMR+ 18, DExtra 17, M17 11, P25 8, NXDN 5, ...) assert only "sut == xlxd-ref", so a shared defect passes; D-STAR, DMR and YSF are the bulk of reflector traffic. The "unserved module gets NAK" rows (P-DCS-04, P-DXT-03, P-M17-03) also skip on this rig.
4. P-DMR-04: a wrong RPTK hash must get MSTNAK is only recorded (the client sends zeros), yet every hotspot and repeater logs in this way; the auth failure path of DMR is untested.
5. P-DMP-34 and the transcoder failure paths (P-USR-24, AMBEd legacy retransmit and 20 ms rows): DMR+ to P25 or NXDN is untested and "no fabricated audio when the transcoder is lost" is asserted only in one direction; cross-mode is the product's main feature.
6. M17 v3 META (GNSS, text, ECD; P26j-7 ECD refusal on an Internet link): all 29 rows are unit-level or manual (m17-fme), nothing runs against DV-Router, and v3 radios are now common on M17.
7. AllStar T5 with a routed NAT (ASL-LAB, ASL-EDGE): written or blocked, never run; most AllStar nodes sit behind a NAT.
8. AllStar edge loop guard (60 s hold-down of the dropped branch): only "some HANGUP happened" is asserted; a node loop floods the whole AllStar network.
9. UDP flood and spoofing (T11s, T11m, real source-address spoofing): not built; every reflector port is an open internet UDP service.
10. P-USR-13 (voice that stops without an unkey ends the stream after a silence timeout) has no case, and the dynamic USRP idle rows (P-USR-31/32) skip: a lost unkey on a bridge sticks a module for every listener.
11. M17 keepalive and malformed input: P-M17-12 (30 s drop, timing only printed), P-M17-28 and P-XLX-25 (stream id 0 not relayed / opens no stream) have no asserting case; stuck links and bogus streams hit every M17 user.
12. NXDN data calls and GPS (NXG1 relay, NXG4 CRC refusal, NXG5 negatives, NXG6 short reports): cases not written, the gateway oracle is a manual run; radio GPS beacons depend on them.
13. Stream registry (LEAK2): a close removing the entry at once is not asserted and the L3 P1 hold is only recorded; a leaked open stream blocks a module for hours.
14. APRS-IS gating and two-node text overs (DP15-5c to 5e login/qAC/NOGATE, 7a preemption by peer voice, own text over back, 7e/7f): wrong gating pollutes the public APRS-IS network and loops text overs over XLX peers.
15. P25 Reed-Solomon parity (P-P25-28 asserts today's constant bytes) and the 1.6 s floor timeout (P-P25-15): real P25 radios and DSD decoders may reject or hang on our egress; lower volume than the rows above.

## How to read the tables

Columns: Obligation (doc id and short statement) | Source (`docs/` path and
line of the obligation) | Case ids (test file name, two colons, function name; the
parameter part is dropped) | Marker (run level of the asserting case) | Client | Status.

- Client: `a` = our own Python client or fake in `dvtest/`; `b:<program>` = a
  real upstream program in the rig (Dire Wolf, aprsc, Asterisk app_rpt,
  NXDNReflector, NXDNGateway, DSDcc, m17-fme, a browser, ...); `c` = both.
  The reference servers (xlxd, URFd, mrefd) are a comparison side, not the
  client. Almost every protocol row is `a`: no real upstream D-STAR, DMR, YSF
  or M17 client talks to the SUT in a case.
- Status: OK = a case asserts the whole obligation on the SUT and it is not
  permanently skipped on the rig. PARTIAL = asserted in part (one form of
  several, only a sub-field, only "sut == xlxd-ref" with no fixed expected
  value or `dvtest/specs/dvrouter.py` entry, only recorded as data, skipped on
  this rig, or only some protocols of a cross-protocol obligation); the reason
  is at the end of the Obligation cell. GAP = stated, no asserting case (also
  "not built", "planned only", "out of scope"). NOT-IMPLEMENTED = our client
  lacks the feature (`docs/protocols/clients.md`, "Does not implement") and no
  real upstream program asserts it.
- Markers come from the collected tests (`python3 -m pytest --collect-only`);
  `+slow` means only a slow case asserts it.
- The matrix was built from the docs' own claims, checked against the test
  bodies; stale doc claims are noted in the rows. Rows are not independent
  tests: many rows map to one function. Sections for docs that are not one
  protocol (slow data, DP7, DP15, echo, registry, config load, transcoder
  levels, netns classification) list the obligations those docs state.


## DCS
Sources: protocols/dcs.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-DCS-01 519 B link to a served module answered by 14 B ACK from port 30051 | protocols/dcs.md:171 | `test_dcs::test_dcs_link_ack` | rig | a | OK |
| P-DCS-02 ACK latency (DV-Router: at once, profile max 0.5 s) | protocols/dcs.md:172 | `test_dcs::test_dcs_ack_latency` | rig | a | OK |
| P-DCS-03 Bytes 10-518 of the link not required (DroidStar form must link) | protocols/dcs.md:173 | `test_dcs::test_dcs_link_minimal_body` | rig | a | OK |
| P-DCS-04 Link to a module not served gets NAK and no link (PARTIAL: skips on this rig, needs DVT_UNSERVED_MODULE) | protocols/dcs.md:174 | `test_dcs::test_dcs_link_unserved_module_nak` | rig | a | PARTIAL |
| P-DCS-05 Link with invalid callsign or non-letter module never links; NAK per profile | protocols/dcs.md:175 | `test_dcs::test_dcs_link_invalid` | rig | a | OK |
| P-DCS-06 Repeated link: ACK every copy, one client, traffic once | protocols/dcs.md:176 | `test_dcs::test_dcs_relink_single_client` | rig | a | OK |
| P-DCS-07 No registration required by default (PARTIAL: operator-list NAK not tested) | protocols/dcs.md:177 | `test_dcs::test_dcs_link_ack` | rig | a | PARTIAL |
| P-DCS-08 Link packet of another size (11 B DExtra-style) does not link | protocols/dcs.md:178 | `test_dcs::test_dcs_link_wrong_size` | rig | a | OK |
| P-DCS-09 Unlink 11 B or 19 B removes client, answered by 14 B NAK | protocols/dcs.md:188 | `test_dcs::test_dcs_unlink_nak` | rig | a | OK |
| P-DCS-10 No voice reaches the client after the unlink | protocols/dcs.md:189 | `test_dcs::test_dcs_unlink_nak` | rig | a | OK |
| P-DCS-11 Unlink by the talking client ends its stream, frees the module | protocols/dcs.md:190 | `test_dcs::test_dcs_unlink_while_talking` | rig | a | OK |
| P-DCS-12 Unlink from a non-linked address answered with NAK | protocols/dcs.md:191 | `test_dcs::test_dcs_unlink_not_linked` | rig | a | OK |
| P-DCS-13 Reflector sends each linked client a 22 B poll at 5 s or faster | protocols/dcs.md:203 | `test_dcs::test_dcs_server_poll` | rig | a | OK |
| P-DCS-14 Poll fields: DCSnnn, linked module, client callsign and module | protocols/dcs.md:204 | `test_dcs::test_dcs_server_poll` | rig | a | OK |
| P-DCS-15 Extra 9 B packet before each poll (DV-Router: one per poll) | protocols/dcs.md:205 | `test_dcs::test_dcs_server_poll` | rig | a | OK |
| P-DCS-16 Client 17 B keepalive (gateway, DroidStar form) refreshes the link, no reply | protocols/dcs.md:206 | `test_dcs::test_dcs_keepalive_forms` | rig+slow | a | OK |
| P-DCS-17 Keepalive sizes 9, 19, 22 B also refresh the link (DV-Router spec) | protocols/dcs.md:207 | `test_dcs::test_dcs_keepalive_forms` | rig+slow | a | OK |
| P-DCS-18 Silent client dropped after the timeout, no notice (DV-Router spec) | protocols/dcs.md:208 | `test_dcs::test_dcs_keepalive_forms` | rig+slow | a | OK |
| P-DCS-19 Dropped client gets no voice; a talking client is never dropped | protocols/dcs.md:209 | `test_dcs::test_dcs_keepalive_forms` | rig+slow | a | OK |
| P-DCS-20 Keepalive from a non-linked address creates no link, no traffic | protocols/dcs.md:210 | `test_dcs::test_dcs_keepalive_without_link` | rig | a | OK |
| P-DCS-21 Unknown sizes, EEEE, 35 B, 15 B zero packets get no reply, server keeps serving | protocols/dcs.md:216 | `test_dcs::test_dcs_garbage_ignored` | rig | a | OK |
| P-DCS-22 Voice of 99 B dropped; 101 B relayed (DV-Router spec heard 63) | protocols/dcs.md:217 | `test_dcs::test_dcs_voice_size` | rig | a | OK |
| P-DCS-23 Voice from a non-linked address is not relayed | protocols/dcs.md:218 | `test_dcs::test_dcs_voice_not_linked` | rig | a | OK |
| P-DCS-24 100 B voice opens a stream at any sequence number (start at 7) | protocols/dcs.md:230 | `test_dcs::test_dcs_stream_opens_mid_superframe` | rig | a | OK |
| P-DCS-25 Egress 100 B with RPT2 = DCSnnn + module; RPT1, UR, MY, suffix passed | protocols/dcs.md:231 | `test_dcs::test_dcs_egress_fields` | rig | a | OK |
| P-DCS-26 Egress sequence from 0, 0..20 wrapping, 0x40 on last packet | protocols/dcs.md:232 | `test_dcs::test_dcs_egress_sequence` | rig | a | OK |
| P-DCS-27 Egress sync 55 2D 16 at sequence 0 only | protocols/dcs.md:233 | `test_dcs::test_dcs_egress_sequence` | rig | a | OK |
| P-DCS-28 AMBE and slow data relayed unchanged on same-mode egress | protocols/dcs.md:234 | `test_dcs::test_dcs_passthrough_exact` | rig | a | OK |
| P-DCS-29 Last packet (0x40) in every client form ends stream at once and reaches listeners | protocols/dcs.md:235 | `test_dcs::test_dcs_last_packet`, `test_dcs::test_dcs_end_forms` | rig | a | OK |
| P-DCS-30 Payload of the egress last packet (ingress payload passed) | protocols/dcs.md:236 | `test_dcs::test_dcs_last_packet_payload` | rig | a | OK |
| P-DCS-31 Egress counter 58-60 and text area 61-99 (own counter, 0x01 at 61) | protocols/dcs.md:237 | `test_dcs::test_dcs_counter_and_text` | rig | a | OK |
| P-DCS-32 Invalid characters in MY dropped; RPT1/RPT2 rewritten (DV-Router spec) | protocols/dcs.md:238 | `test_dcs::test_dcs_invalid_callsign` | rig | a | OK |
| P-DCS-33 Flag bytes 4-6 passed unchanged on egress | protocols/dcs.md:239 | `test_dcs::test_dcs_egress_fields` | rig | a | OK |
| P-DCS-34 Stream reaches every other client of the module once, none on other modules | protocols/dcs.md:245 | `test_dcs::test_dcs_module_fanout` | rig | a | OK |
| P-DCS-35 Stream routed by the client's linked module, not RPT2 module | protocols/dcs.md:246 | `test_dcs::test_dcs_rpt2_other_module` | rig | a | OK |
| P-DCS-36 Talker never receives its own stream back | protocols/dcs.md:247 | `test_dcs::test_dcs_last_packet` | rig | a | OK |
| P-DCS-37 One stream id per transmission on egress, equal to ingress id | protocols/dcs.md:248 | `test_dcs::test_dcs_last_packet` | rig | a | OK |
| P-DCS-38 Second talker on a busy module not mixed in (PARTIAL: floor cases run on DExtra only, no DCS cell) | protocols/dcs.md:249 | `test_floor::test_first_talker_wins`, `test_floor::test_late_entry_denied` | rig | a | PARTIAL |
| P-DCS-39 Stream without last packet ends after silence timeout, module free, last synthesized | protocols/dcs.md:250 | `test_dcs::test_dcs_stream_timeout` | rig | a | OK |
| P-DCS-40 Voice after the last packet with the same stream id is not relayed | protocols/dcs.md:251 | `test_dcs::test_dcs_voice_after_last` | rig | a | OK |
| P-DCS-46 Ingress stream id 0 accepted (DV-Router spec heard 63) | protocols/dcs.md:252 | `test_dcs::test_dcs_stream_id_zero` | rig | a | OK |
| P-DCS-47 DCS, DPlus and DExtra talkers on one module reach each other with AMBE unchanged | protocols/dcs.md:253 | `test_clients2::test_dstar_family_exact` | rig | a | OK |
| P-DCS-41 Listener that links while a stream runs receives the rest of it | protocols/dcs.md:254 | `test_dcs::test_dcs_late_listener` | rig | a | OK |
| P-DCS-42 Several links from one IP on different ports are separate clients | protocols/dcs.md:260 | `test_dcs::test_dcs_two_ports_one_ip` | rig | a | OK |
| P-DCS-43 Several modules from one ip:port keyed by ip:port + module; unlink removes one | protocols/dcs.md:261 | `test_dcs::test_dcs_two_modules_one_port` | rig | a | OK |
| P-DCS-44 Packets to the client come from source port 30051 | protocols/dcs.md:262 | `test_dcs::test_dcs_source_port` | rig | a | OK |
| P-DCS-45 DCS peering between reflectors (out of scope, needs interlink rig) | protocols/dcs.md:268 | - | - | - | GAP |

## DExtra
Sources: protocols/dextra.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-DXT-01 Link to a served module answered by 14 B ACK (bytes 0-9 echoed + ACK 0x00) (PARTIAL: only sut == ref) | protocols/dextra.md:165 | `test_dextra::test_dextra_link_ack` | rig | a | PARTIAL |
| P-DXT-02 ACK latency (DV-Router: at once) | protocols/dextra.md:166 | `test_dextra::test_dextra_ack_latency` | rig | a | OK |
| P-DXT-03 Link to an unserved module letter gets NAK and no link (PARTIAL: skips on this rig, needs DVT_UNSERVED_MODULE) | protocols/dextra.md:167 | `test_dextra::test_dextra_link_unserved_module_nak` | rig | a | PARTIAL |
| P-DXT-04 Link with invalid callsign or non-letter module never links; NAK per profile | protocols/dextra.md:168 | `test_dextra::test_dextra_link_invalid` | rig | a | OK |
| P-DXT-05 Link with last byte 11 ACKed; unlink answered DISCONNECTED (PARTIAL: only sut == ref) | protocols/dextra.md:169 | `test_dextra::test_dextra_link_rev1` | rig | a | PARTIAL |
| P-DXT-06 Repeated link while linked: ACK again, one client | protocols/dextra.md:170 | `test_dextra::test_dextra_relink_single_client` | rig | a | OK |
| P-DXT-07 Reflector sends 9 B keepalive (callsign + 0x00) at a fixed period (PARTIAL: form only sut == ref) | protocols/dextra.md:171 | `test_dextra::test_dextra_reflector_keepalive` | rig | a | PARTIAL |
| P-DXT-08 Own-callsign 9 B keepalive refreshes link, echo of reflector keepalive does not | protocols/dextra.md:172 | `test_dextra::test_dextra_keepalive_echo_not_refresh` | rig+slow | a | OK |
| P-DXT-09 Silent client dropped after the keepalive timeout, notice sent | protocols/dextra.md:173 | `test_dextra::test_dextra_silent_client_dropped` | rig+slow | a | OK |
| P-DXT-10 A dropped client gets no further voice (PARTIAL: only sut == ref) | protocols/dextra.md:174 | `test_dextra::test_dextra_dropped_client_no_voice` | rig+slow | a | PARTIAL |
| P-DXT-11 A client is not dropped while it is talking (PARTIAL: only sut == ref) | protocols/dextra.md:175 | `test_dextra::test_dextra_talker_not_dropped` | rig+slow | a | PARTIAL |
| P-DXT-12 Unlink (11 B) removes the client, no reply (PARTIAL: only sut == ref) | protocols/dextra.md:176 | `test_dextra::test_dextra_unlink_no_reply` | rig | a | PARTIAL |
| P-DXT-13 After the unlink no further voice reaches the client (tail judged by B5) | protocols/dextra.md:177 | `test_xlxd_clients::test_leave_mid_stream_tail` | rig | a | OK |
| P-DXT-14 Unlink by the talking client ends its stream and frees the module | protocols/dextra.md:178 | `test_dextra::test_dextra_unlink_while_talking` | rig | a | OK |
| P-DXT-54 Unlink with the client's own stream open removes it at once (no keepalive, no later stream) | protocols/dextra.md:179 | `test_dextra::test_dextra_unlink_with_open_stream` | rig | a | OK |
| P-DXT-52 9 B keepalive from a non-linked address creates no link, no traffic | protocols/dextra.md:180 | `test_dextra::test_dextra_poll_without_link` | rig | a | OK |
| P-DXT-15 Unknown sizes and wrong-size DSVT get no reply, reflector keeps serving (PARTIAL: only sut == ref) | protocols/dextra.md:186 | `test_dextra::test_dextra_garbage_ignored` | rig | a | PARTIAL |
| P-DXT-16 DSVT from a non-linked address is not relayed (PARTIAL: only sut == ref) | protocols/dextra.md:187 | `test_dextra::test_dextra_dsvt_not_linked` | rig | a | PARTIAL |
| P-DXT-17 DSVT prefix checks: byte 8, byte 14, bytes 9-11 variants (DV-Router spec) | protocols/dextra.md:188 | `test_dextra::test_dextra_header_prefix` | rig | a | OK |
| P-DXT-18 56 B JARL-layout header opens a stream on a linked client's module | protocols/dextra.md:199 | `test_dextra::test_dextra_header_opens_stream` | rig | a | OK |
| P-DXT-19 Ingress header CRC not required valid (PARTIAL: only sut == ref) | protocols/dextra.md:200 | `test_dextra::test_dextra_header_crc_ingress` | rig | a | PARTIAL |
| P-DXT-20 Egress header carries a valid P_FCS | protocols/dextra.md:201 | `test_dextra::test_dextra_egress_header_crc` | rig | a | OK |
| P-DXT-21 Header with invalid MY/RPT1/RPT2 characters: MY dropped, RPT rewritten (DV-Router spec) | protocols/dextra.md:202 | `test_dextra::test_dextra_header_invalid_callsign` | rig | a | OK |
| P-DXT-22 Flag bytes reach egress (DV-Router spec flags 400102) | protocols/dextra.md:203 | `test_dextra::test_dextra_header_flags_passed` | rig | a | OK |
| P-DXT-23 Header with stream id 0 opens no stream (B8) | protocols/dextra.md:204 | `test_floor::test_admission_sid_zero` | rig | a | OK |
| P-DXT-24 Header repeated with same stream id mid-stream: no second stream, no restart | protocols/dextra.md:205 | `test_dextra::test_dextra_header_repeat_discarded` | rig | a | OK |
| P-DXT-25 Header whose stream id is open on another module dropped (PARTIAL: DV-Router allows local same-sid, B8) | protocols/dextra.md:206 | `test_floor::test_admission_same_sid_two_rooms` | rig | a | PARTIAL |
| P-DXT-26 Header or voice reusing a module's last outbound id from another address (DV-Router relays) | protocols/dextra.md:207 | `test_dextra::test_dextra_outbound_id_echo` | rig | a | OK |
| P-DXT-27 The 9 AMBE bytes relayed unchanged on same-mode egress | protocols/dextra.md:216 | `test_xlxd_matrix::test_passthrough_exact` | rig | a | OK |
| P-DXT-28 The 3 slow-data bytes relayed unchanged on same-mode egress (PARTIAL: only sut == ref) | protocols/dextra.md:217 | `test_dextra::test_dextra_slow_data_passthrough` | rig | a | PARTIAL |
| P-DXT-29 Egress counter 0..20 in order, header byte 0x80, last frame 0x40 | protocols/dextra.md:218 | `test_dextra::test_dextra_egress_counter` | rig | a | OK |
| P-DXT-30 Egress frame 0 of every 21 carries sync 55 2D 16, no other frame does | protocols/dextra.md:219 | `test_dextra::test_dextra_egress_sync` | rig | a | OK |
| P-DXT-31 Voice with counter outside 0..20 or bit 0x80 (DV-Router spec: passed unchanged) | protocols/dextra.md:220 | `test_dextra::test_dextra_counter_out_of_range` | rig | a | OK |
| P-DXT-32 A last frame (0x40) ends the stream and reaches the listeners | protocols/dextra.md:221 | `test_dextra::test_dextra_last_frame_ends_stream` | rig | a | OK |
| P-DXT-33 Payload of the egress last frame (DV-Router spec: ingress payload passed) | protocols/dextra.md:222 | `test_dextra::test_dextra_last_frame_payload` | rig | a | OK |
| P-DXT-34 Voice after the last frame with the same stream id is not relayed (PARTIAL: only sut == ref) | protocols/dextra.md:223 | `test_dextra::test_dextra_voice_after_last` | rig | a | PARTIAL |
| P-DXT-53 Stream whose first voice counter is not 0 relayed from its first frame (PARTIAL: only sut == ref) | protocols/dextra.md:224 | `test_dextra::test_dextra_first_counter_not_zero` | rig | a | PARTIAL |
| P-DXT-35 Header with RPT2 = reflector + linked module reaches every other client once (PARTIAL: only sut == ref) | protocols/dextra.md:230 | `test_dextra::test_dextra_module_fanout` | rig | a | PARTIAL |
| P-DXT-36 Header whose RPT2 module differs from the linked module (DV-Router: linked module) | protocols/dextra.md:231 | `test_dextra::test_dextra_rpt2_other_module` | rig | a | OK |
| P-DXT-37 Egress RPT2 = reflector + module; RPT1, UR, MY pass (PARTIAL: RPT2 only sut == ref) | protocols/dextra.md:232 | `test_dextra::test_dextra_egress_rpt_fields` | rig | a | PARTIAL |
| P-DXT-38 Module change by unlink + link; heard on the new module only | protocols/dextra.md:233 | `test_xlxd_clients::test_module_change_follows_talker` | rig | a | OK |
| P-DXT-39 Link to a second module from the same address without unlink (DV-Router: module change) | protocols/dextra.md:234 | `test_dextra::test_dextra_second_module_link` | rig | a | OK |
| P-DXT-40 Stream with lost last frame ends after silence timeout, module free (B3) | protocols/dextra.md:240 | `test_xlxd_clients::test_stream_timeout_frees_module` | rig | a | OK |
| P-DXT-41 Second header on a busy module not mixed into the running stream (B1/B2) | protocols/dextra.md:241 | `test_floor::test_first_talker_wins`, `test_floor::test_late_entry_denied` | rig | a | OK |
| P-DXT-42 Voice whose stream id had no header is not relayed (DV-Router spec) | protocols/dextra.md:242 | `test_dextra::test_dextra_voice_without_header` | rig | a | OK |
| P-DXT-43 The talker never receives its own stream back (PARTIAL: only sut == ref) | protocols/dextra.md:243 | `test_dextra::test_dextra_no_self_echo` | rig | a | PARTIAL |
| P-DXT-44 Client linking mid-stream gets the stream's header before its voice (DV-Router spec) | protocols/dextra.md:244 | `test_dextra::test_dextra_late_listener_header` | rig | a | OK |
| P-DXT-45 Header copies on egress (DV-Router spec: 5 copies, none later) | protocols/dextra.md:245 | `test_dextra::test_dextra_egress_header_copies` | rig | a | OK |
| P-DXT-46 One stream id per egress transmission, equal to the ingress id | protocols/dextra.md:246 | `test_dextra::test_dextra_egress_stream_id`, `test_floor::test_sid_preserved` | rig | a | OK |
| P-DXT-47 Two callsigns from one IP are two clients, each gets traffic once (PARTIAL: only sut == ref) | protocols/dextra.md:252 | `test_dextra::test_dextra_two_callsigns_one_ip` | rig | a | PARTIAL |
| P-DXT-48 Same callsign from two ports (DV-Router spec: each gets traffic once) | protocols/dextra.md:253 | `test_dextra::test_dextra_same_callsign_two_ports` | rig | a | OK |
| P-DXT-49 A keepalive from one client never refreshes another client (PARTIAL: only sut == ref) | protocols/dextra.md:254 | `test_dextra::test_dextra_keepalive_isolated` | rig+slow | a | PARTIAL |
| P-DXT-50 Link from an XRF callsign: reply form (DV-Router spec: NAK, no peer links) | protocols/dextra.md:265 | `test_dextra::test_dextra_xrf_peer_link` | rig | a | OK |
| P-DXT-51 Stream from an XRF peer routed to the linked local module (DV-Router: not built, nothing heard) | protocols/dextra.md:266 | `test_dextra::test_dextra_xrf_peer_routing` | rig | a | OK |

## DPlus
Sources: protocols/dplus.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-DPL-01 Connect 05 00 18 00 01 (5 B) echoed byte for byte to the sender | protocols/dplus.md:120 | `test_dplus::test_dplus_connect_echo` | rig | a | OK |
| P-DPL-02 Replies go to the packet's source ip:port and leave from UDP 20001 | protocols/dplus.md:121 | `test_dplus::test_dplus_reply_source_port` | rig | a | OK |
| P-DPL-03 28 B login with a valid callsign answered 08 C0 04 00 OKRW (IRC, QNET, DROID forms) | protocols/dplus.md:122 | `test_dplus::test_dplus_login_okrw` | rig | a | OK |
| P-DPL-04 The login reply comes within a few seconds (3 s bound) | protocols/dplus.md:123 | `test_dplus::test_dplus_login_reply_time` | rig | a | OK |
| P-DPL-05 No login reply other than OKRW or BUSY | protocols/dplus.md:124 | `test_dplus::test_dplus_login_reply_set` | rig | a | OK |
| P-DPL-06 A refused login (invalid callsign) is refused; BUSY per DV-Router spec, no IP-wide ban | protocols/dplus.md:125 | `test_dplus::test_dplus_login_invalid_callsign` | rig | a | OK |
| P-DPL-07 Server does not consult the US-Trust registration (unregistered test call links) | protocols/dplus.md:126 | `test_dplus::test_dplus_login_okrw` | rig | a | OK |
| P-DPL-08 A login without a preceding connect is accepted (DV-Router spec OKRW) | protocols/dplus.md:127 | `test_dplus::test_dplus_login_without_connect` | rig | a | OK |
| P-DPL-09 A repeated login from a linked ip:port keeps one session, traffic once | protocols/dplus.md:128 | `test_dplus::test_dplus_relogin_one_session` | rig | a | OK |
| P-DPL-10 Server sends keepalive 03 60 00 to every linked client about once a second | protocols/dplus.md:134 | `test_dplus::test_dplus_server_keepalive` | rig | a | OK |
| P-DPL-11 Client keepalives keep the link; server does not answer one for one (PARTIAL: one-for-one only sut == ref) | protocols/dplus.md:135 | `test_dplus::test_dplus_server_keepalive` | rig | a | PARTIAL |
| P-DPL-12 Silent client dropped after timeout (30 s) and sent 05 00 18 00 00, then no traffic | protocols/dplus.md:136 | `test_dplus::test_dplus_timeout_drop` | rig | a | OK |
| P-DPL-13 A transmitting client is not timed out for missing keepalives | protocols/dplus.md:137 | `test_dplus::test_dplus_talker_kept_alive` | rig | a | OK |
| P-DPL-14 Disconnect from a linked client is echoed and ends the session | protocols/dplus.md:138 | `test_dplus::test_dplus_disconnect` | rig | a | OK |
| P-DPL-15 Disconnect from an ip:port that is not linked is echoed too | protocols/dplus.md:139 | `test_dplus::test_dplus_disconnect_unknown` | rig | a | OK |
| P-DPL-16 58 B header opens a stream on the module in RPT2 byte 8, RPT1 module ignored | protocols/dplus.md:145 | `test_dplus::test_dplus_module_from_rpt2` | rig | a | OK |
| P-DPL-17 Header CRC is not checked on input | protocols/dplus.md:146 | `test_dplus::test_dplus_header_crc_ignored` | rig | a | OK |
| P-DPL-18 Egress headers carry a correct CRC-CCITT (from DPlus and DMR talkers) | protocols/dplus.md:147 | `test_dplus::test_dplus_egress_header` | rig | a | OK |
| P-DPL-19 Egress RPT2 = REF + reflector number + module (REF999 P) | protocols/dplus.md:148 | `test_dplus::test_dplus_egress_header` | rig | a | OK |
| P-DPL-20 Egress header flags are 00 00 00 (PARTIAL: only sut == ref) | protocols/dplus.md:149 | `test_dplus::test_dplus_egress_header` | rig | a | PARTIAL |
| P-DPL-21 29 B voice relayed with AMBE and slow data unchanged | protocols/dplus.md:150 | `test_dplus::test_dplus_passthrough` | rig | a | OK |
| P-DPL-22 Egress seq 0 frames carry sync 55 2D 16, also for DMR-sourced streams | protocols/dplus.md:151 | `test_dplus::test_dplus_egress_sync` | rig | a | OK |
| P-DPL-23 32 B end frame (IRC, DROID, xlxd forms) closes the stream at once | protocols/dplus.md:152 | `test_dplus::test_dplus_end_forms` | rig | a | OK |
| P-DPL-24 Egress end frame 32 B, 0x40 bit, JARL end pattern at 26-31 (DV-Router spec) | protocols/dplus.md:153 | `test_dplus::test_dplus_egress_end` | rig | a | OK |
| P-DPL-25 Egress header repeated once per superframe so a late listener can start | protocols/dplus.md:154 | `test_dplus::test_dplus_late_listener` | rig | a | OK |
| P-DPL-26 Repeated headers from a talker neither restart nor duplicate the stream | protocols/dplus.md:155 | `test_dplus::test_dplus_repeated_headers` | rig | a | OK |
| P-DPL-27 One nonzero stream id per egress stream, equal to the source id | protocols/dplus.md:156 | `test_dplus::test_dplus_passthrough` | rig | a | OK |
| P-DPL-28 The talker's own ip:port gets nothing of its stream | protocols/dplus.md:157 | `test_dplus::test_dplus_passthrough` | rig | a | OK |
| P-DPL-29 Stream with lost end frame closed after a timeout, module freed (B3) | protocols/dplus.md:158 | `test_dplus::test_dplus_lost_end` | rig | a | OK |
| P-DPL-30 Client linked but not yet transmitted hears its module (DV-Router spec) | protocols/dplus.md:164 | `test_dplus::test_dplus_listen_before_transmit` | rig | a | OK |
| P-DPL-31 After transmitting on module M the client hears module M | protocols/dplus.md:165 | `test_dplus::test_dplus_listen_after_transmit` | rig | a | OK |
| P-DPL-32 Each ip:port is its own client (two ports on one IP) | protocols/dplus.md:166 | `test_dplus::test_dplus_two_ports_one_ip` | rig | a | OK |
| P-DPL-33 DPlus, DExtra and DCS talkers on one module reach each other with AMBE unchanged | protocols/dplus.md:167 | `test_clients2::test_dstar_family_exact` | rig | a | OK |
| P-DPL-34 Unknown, short or oversize packets get no reply, server keeps serving | protocols/dplus.md:173 | `test_dplus::test_dplus_malformed_ignored` | rig | a | OK |
| P-DPL-35 DV packets from an ip:port that has not logged in are not relayed | protocols/dplus.md:174 | `test_dplus::test_dplus_dv_without_login` | rig | a | OK |
| P-DPL-36 Header with a malformed MY dropped (DV-Router spec) | protocols/dplus.md:175 | `test_dplus::test_dplus_header_bad_my` | rig | a | OK |

## G3
Sources: protocols/g3.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-G3-01 32 B presence answered with 36 B (bytes 0-31 echoed, byte 2 0x80, gateway IPv4) | protocols/g3.md:151 | `test_g3::test_g3_presence_reply` | rig | a | OK |
| P-G3-02 A presence of any other size gets no reply | protocols/g3.md:152 | `test_g3::test_g3_presence_wrong_size` | rig | a | OK |
| P-G3-03 IPv4 in the presence reply is an address of the reflector the terminal can reach | protocols/g3.md:153 | `test_g3::test_g3_presence_address` | rig | a | OK |
| P-G3-04 Presence creates the terminal entry; one entry per IP; new callsign replaces it | protocols/g3.md:154 | `test_g3::test_g3_presence_replaces` | rig | a | OK |
| P-G3-05 Presence from a blacklisted callsign (PARTIAL: skips without DVT_XLXD_BLACKLIST=N0G3X) | protocols/g3.md:155 | `test_g3::test_g3_blacklisted` | rig | a | PARTIAL |
| P-G3-06 Route query for reflector + open module accepted (status 0, UR, RPT1 G, RPT2, IPv4) | protocols/g3.md:166 | `test_g3::test_g3_route_accepted` | rig | a | OK |
| P-G3-07 Routed call, foreign callsign or closed module rejected (status 1, IPv4 0) (PARTIAL: closed module not probed) | protocols/g3.md:167 | `test_g3::test_g3_route_rejected` | rig | a | PARTIAL |
| P-G3-08 A route query of any other size gets no reply | protocols/g3.md:168 | `test_g3::test_g3_route_wrong_size` | rig | a | OK |
| P-G3-09 A route query with an all-space callsign is taken as the reflector | protocols/g3.md:169 | `test_g3::test_g3_route_blank_callsign` | rig | a | OK |
| P-G3-10 Reflector callsign on the G3 wire (DV-Router spec XLX999) | protocols/g3.md:170 | `test_g3::test_g3_route_accepted` | rig | a | OK |
| P-G3-11 Voice from an IP that sent no presence is not relayed | protocols/g3.md:180 | `test_g3::test_g3_voice_without_presence` | rig | a | OK |
| P-G3-12 After presence a header with RPT2 = reflector + module opens a stream, other protocols hear it | protocols/g3.md:181 | `test_g3::test_g3_talk_reaches_dextra` | rig | a | OK |
| P-G3-13 A header whose RPT2 is not the reflector is dropped | protocols/g3.md:182 | `test_g3::test_g3_foreign_rpt2_dropped` | rig | a | OK |
| P-G3-14 Terminal's module is RPT2 of its last header; silent terminal hears nothing | protocols/g3.md:183 | `test_g3::test_g3_module_follows_talk` | rig | a | OK |
| P-G3-15 Egress to the terminal goes to its IP:40000 from port 40000 (PARTIAL: source port 40000 not asserted) | protocols/g3.md:184 | `test_g3::test_g3_listen` | rig | a | PARTIAL |
| P-G3-16 Egress layout: header 56 B, voice 27 B, sequence 0..20, 0x40 last, RPT2 = reflector + module | protocols/g3.md:185 | `test_g3::test_g3_listen` | rig | a | OK |
| P-G3-17 Header copies and last-frame payload on egress (DV-Router spec: payload passed) | protocols/g3.md:186 | `test_g3::test_g3_listen` | rig | a | OK |
| P-G3-18 Voice from the terminal's IP accepted from any source port | protocols/g3.md:187 | `test_g3::test_g3_other_source_port` | rig | a | OK |
| P-G3-19 The talking terminal gets no echo of its own stream (B9) | protocols/g3.md:188 | `test_g3::test_g3_talk_reaches_dextra` | rig | a | OK |
| P-G3-20 AMBE byte exact to and from other D-STAR protocols (other modes transcoded: not asserted) | protocols/g3.md:189 | `test_g3::test_g3_talk_reaches_dextra`, `test_g3::test_g3_listen` | rig | a | PARTIAL |
| P-G3-21 Header CRC not checked; invalid MY dropped, RPT1 rewritten (DV-Router spec) | protocols/g3.md:190 | `test_g3::test_g3_invalid_callsign` | rig | a | OK |
| P-G3-22 Lost last frame: stream ends after silence timeout, module free (B3) | protocols/g3.md:191 | `test_g3::test_g3_lost_last` | rig | a | OK |
| P-G3-23 Terminal removed on ICMP port unreachable from its own port only | protocols/g3.md:197 | `test_g3::test_g3_icmp_removal` | rig | a | OK |
| P-G3-24 PING (4 B) to each terminal's IP:40000 about every 10 s | protocols/g3.md:198 | `test_g3::test_g3_ping` | rig | a | OK |
| P-G3-25 Silent terminal removed after a timeout, presence does not refresh (code only, 1 h) | protocols/g3.md:199 | - | - | - | GAP |
| P-G3-26 HPCH dummy packets from the terminal on 40000 get no reply (DV-Router spec reply 0) | protocols/g3.md:200 | `test_g3::test_g3_hpch` | rig | a | OK |
| P-G3-27 Two terminals on one module both hear a third party, neither hears itself (PARTIAL: skips without --profile g3) | protocols/g3.md:206 | `test_g3::test_g3_two_terminals` | rig | a | PARTIAL |
| P-G3-28 Reflector keeps serving its other protocols when G3 cannot start (code only) | protocols/g3.md:207 | - | - | - | GAP |

## D-STAR slow data
Sources: dstar-slowdata-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| STD6 4.1.3: 21 frames per superframe, frame 0 = 55 2D 16 sync, no slow data in it | dstar-slowdata-cases.md:78 | `test_dstar_sd::test_render_sync_scrambling_and_superframe_length`, `test_dstar_sd_dvrouter::test_unit66_of_a_rendered_superframe_keeps_the_wire_bytes`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| STD6 Ap1.1: scrambler x^7+x^4+1 derived from the polynomial, bytes 70 4F 93 40 64 74 6D 30 2B | dstar-slowdata-cases.md:79 | `test_dstar_sd::test_scrambler_is_derived_from_the_polynomial_not_typed_in`, `test_dstar_sd::test_filler_scrambles_to_the_known_wire_bytes` | unit | a | OK |
| STD6 Ap1.3: sync frame and last frame are not scrambled (PARTIAL: only the sync frame is asserted) | dstar-slowdata-cases.md:80 | `test_dstar_sd::test_render_sync_scrambling_and_superframe_length` | unit | a | PARTIAL |
| STD6 6.1: block = mini-header + 5 payload bytes, unused bytes 0x66 not counted in the length | dstar-slowdata-cases.md:81 | `test_dstar_sd::test_text_blocks`, `test_dstar_sd::test_simple_data_blocks_length_nibble_and_filler`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| STD6 6.1.3: block types 3x, 4x, 5x, 66, 8x/9x, Cx with their length or number nibble | dstar-slowdata-cases.md:82 | `test_dstar_sd::test_text_blocks`, `test_dstar_sd::test_simple_data_blocks_length_nibble_and_filler`, `test_dstar_sd::test_header_blocks_cover_41_bytes_in_9_blocks`, `test_dstar_sd::test_squelch_block_is_the_published_example`, `test_dstar_sd::test_fast_data_mini_headers` | unit | c | OK |
| STD6 figures: 10 blocks per superframe, no block spans a sync (a straddling block is undecodable) | dstar-slowdata-cases.md:83 | `test_dstar_sd::test_render_even_start_makes_a_block_straddle_the_sync`, `test_dstar_sd::test_straddling_blocks_never_decode`, `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on` | unit | c | OK |
| STD6 6.3: 20 character message in 4 blocks 0x40..0x43, start at an even data frame; SUT shows one TX status | dstar-slowdata-cases.md:84 | `test_dstar_sd::test_text_blocks`, `test_dstar_sd::test_render_is_inverse_of_decoder_pairing`, `test_dstar_sd::test_tx_message_fields_decode_to_the_text_in_every_superframe`, `test_dp15_streams::test_dp15_1a_tx_message_becomes_the_status` | unit/rig | c | OK |
| STD6 6.4: radio header resend = 41 bytes as 8 blocks 0x55 + one 0x51, FCS checked | dstar-slowdata-cases.md:85 | `test_dstar_sd::test_header_blocks_cover_41_bytes_in_9_blocks`, `test_dstar_sd::test_decoder_text_header_squelch_and_fast_events` | unit | c | OK |
| STD6 6.5: code squelch C2 xx xx, code = hex of the decimal digits shown | dstar-slowdata-cases.md:86 | `test_dstar_sd::test_squelch_block_is_the_published_example`, `test_dstar_sd::test_decoder_text_header_squelch_and_fast_events` | unit | a | OK |
| STD6 6.8.1: D-PRS line $$CRC + 4 hex + comma + APRS string + CR, CRC-16 X.25 covering the CR | dstar-slowdata-cases.md:87 | `test_dstar_sd::test_dprs_line_shape_and_crc_scope`, `test_dstar_sd::test_crc16_check_values`, `test_dstar_sd::test_decoder_dprs_and_nmea`, `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on`, `test_dp15_streams::test_dp15_1b_dprs_beacon_is_a_position` | unit/rig | c | OK |
| STD6 6.8.1 example $$CRCEF2E and the D-PRS field rules (symbol, data extension, comment) reproduced | dstar-slowdata-cases.md:87 | - | - | - | GAP |
| STD6 6.7: NMEA sentence as simple data ending CR LF, XOR checksum between $ and * | dstar-slowdata-cases.md:88 | `test_dstar_sd::test_nmea_checksum_known_sentence`, `test_dstar_sd::test_decoder_dprs_and_nmea`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| STD6 6.7 figure: 1 to 3 NMEA sentences + callsign + message as ONE comma-joined line (not generated) | dstar-slowdata-cases.md:89 | - | - | - | GAP |
| STD6 6.2: simple data must not carry 00, 11, 13, 76, 84, E7, FE (generator refuses; no receiver checks) | dstar-slowdata-cases.md:90 | `test_dstar_sd::test_simple_data_forbidden_characters` | unit | a | OK |
| STD6 7: fast data blocks 0x9C then 0x94 x 9, 28 and 20 byte blocks (PARTIAL: byte order and scrambling range unverified) | dstar-slowdata-cases.md:91 | `test_dstar_sd::test_fast_data_mini_headers`, `test_dstar_sd::test_fast_data_superframe_round_trip_and_structure`, `test_dstar_sd::test_fast_data_payload_rejects_a_damaged_block`, `test_dstar_sd::test_fast_data_pairs_look_like_blocks_to_a_slow_data_receiver` | unit | a | PARTIAL |
| STD6 6.6: packet loss rewrite table 31/32/33/34/35 (PARTIAL: checked against the table only, no decoder) | dstar-slowdata-cases.md:92 | `test_dstar_sd::test_packet_loss_rewrite_table` | unit | a | PARTIAL |
| Oracle runs are contained (docker --network none, read-only, caps dropped) and pins come from .env.example | dstar-slowdata-cases.md:96 | `test_oracle_dstar_sd::test_docker_argv_is_the_contained_form`, `test_oracle_dstar_sd::test_pins_come_from_env_example_not_from_code`, `test_oracle_dsdcc::test_docker_argv_is_the_contained_form`, `test_oracle_dsdcc::test_pins_come_from_env_example` | unit | a | OK |
| Aligned messages (slots 0,1,3,6) decoded by both oracles; DV-Router text over repeats the 4 blocks | dstar-slowdata-cases.md:129 | `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| MMDVMHost CDStarSlowData clears the text segments at every sync (split message never emitted) | dstar-slowdata-cases.md:130 | `test_dstar_sd::test_sync_policy_decides_the_split_text`, `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on` | unit | c | OK |
| DStarGateway TextCollector keeps the segments over a sync (split message decodes) | dstar-slowdata-cases.md:131 | `test_dstar_sd::test_sync_policy_decides_the_split_text`, `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on` | unit | c | OK |
| Both oracles clear the 4-segment mask when a message is handed out | dstar-slowdata-cases.md:132 | `test_dstar_sd::test_a_consumed_message_clears_the_slots_and_old_slots_mix`, `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on` | unit | c | OK |
| Segments carry no message id: old and new blocks mix (text-stale-mix) | dstar-slowdata-cases.md:133 | `test_dstar_sd::test_a_consumed_message_clears_the_slots_and_old_slots_mix`, `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | c | OK |
| Both oracles strip bit 7 of every text byte | dstar-slowdata-cases.md:134 | `test_dstar_sd::test_text_bit_7_is_stripped`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| A block straddling a sync is garbled by both pair phases; a lost frame shifts pairing until the next sync | dstar-slowdata-cases.md:135 | `test_dstar_sd::test_straddling_blocks_never_decode`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| A missing sync frame does nothing when the previous superframe ended on a pair boundary | dstar-slowdata-cases.md:136 | `test_dstar_sd_dvrouter::test_split_units_follows_the_frame_counter_not_the_sync`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| MMDVMHost decodes the header resend only with all 9 blocks after one sync and a valid FCS | dstar-slowdata-cases.md:137 | `test_dstar_sd::test_decoder_text_header_squelch_and_fast_events`, `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| MMDVMHost reports only block type bytes for GPS, squelch and fast data (no payload decode) | dstar-slowdata-cases.md:138 | `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| DStarGateway $$CRC collector: CRC must include the CR, 300 and 301 byte lines both accepted | dstar-slowdata-cases.md:139 | `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on`, `test_dstar_sd::test_decoder_limits_of_the_dvrouter_buffers` | unit | c | OK |
| DStarGateway NMEA collectors: six sentence types, bad checksum gives nothing, output line format | dstar-slowdata-cases.md:140 | `test_dstar_sd::test_committed_corpus_keeps_the_findings_the_doc_relies_on`, `test_oracle_dstar_sd::test_dgw_nmea_line_is_what_the_collector_builds` | unit | c | OK |
| DStarGateway RepeaterHandler collects text only while its stored text is empty (first message only) | dstar-slowdata-cases.md:141 | - | - | - | GAP |
| DStarGateway sentence collectors ignore the length nibble; ours honours it (strict) or reproduces it (ignore) | dstar-slowdata-cases.md:142 | `test_dstar_sd_dvrouter::test_nibble_policies_on_a_full_block`, `test_dstar_sd_dvrouter::test_nibble_vectors_name_the_difference_and_both_results_are_recorded` | unit | c | OK |
| GPS collectors keep their buffer across syncs (a D-PRS line spans two superframes) | dstar-slowdata-cases.md:143 | `test_dstar_sd::test_committed_corpus_is_current_and_reproduced_by_our_decoders` | unit | c | OK |
| DV-Router DP5e: four indexed 5-byte text segments kept across superframe units (PARTIAL: Python model of dstar.rs, SUT not run) | dstar-slowdata-cases.md:149 | `test_dstar_sd_dvrouter::test_model_keeps_the_text_across_units_today_and_resets_it_under_dp5f` | unit | a | PARTIAL |
| DV-Router: first complete message is consumed and clears the mask (PARTIAL: SUT only shows one status) | dstar-slowdata-cases.md:150 | `test_dstar_sd::test_a_consumed_message_clears_the_slots_and_old_slots_mix`, `test_dp15_streams::test_dp15_1a_tx_message_becomes_the_status` | unit/rig | a | PARTIAL |
| DV-Router: a missing text block gives no partial status (PARTIAL: only block 2 dropped in every superframe) | dstar-slowdata-cases.md:151 | `test_dp15_streams::test_dp15_1c_a_missing_text_block_gives_no_status`, `test_dstar_sd::test_tx_message_with_a_dropped_block_never_completes` | unit/rig | a | PARTIAL |
| DP5f decision: DV-Router resets text segments at every sync like MMDVMHost (not in the code read: planned) | dstar-slowdata-cases.md:202 | - | - | - | GAP |
| Frame pairs (1,2)..(19,20): a pair with a missing frame skips the block, resets only the GPS line (model only) | dstar-slowdata-cases.md:236 | `test_dstar_sd_dvrouter::test_model_hole_skips_one_block_and_resets_only_the_gps_line`, `test_dstar_sd_dvrouter::test_pair_hole_vectors_have_holes_in_the_bitmap` | unit | a | PARTIAL |
| DIFF-NIBBLE: header 0x36..0x3F ignored, line then fails its checksum (Reject Crc) (model only) | dstar-slowdata-cases.md:265 | `test_dstar_sd_dvrouter::test_nibble_vectors_name_the_difference_and_both_results_are_recorded`, `test_dstar_sd_dvrouter::test_model_ignores_other_header_bytes_without_resetting_the_gps_line` | unit | a | PARTIAL |
| DIFF-NIBBLE: a block with n = 0 (0x30) carries 5 bytes (model only) | dstar-slowdata-cases.md:267 | `test_dstar_sd_dvrouter::test_nibble_policies_on_a_full_block`, `test_dstar_sd_dvrouter::test_nibble_vectors_name_the_difference_and_both_results_are_recorded` | unit | a | PARTIAL |
| A legal short block in a GPS line gives the correct sentence, DStarGateway garbles it (model only) | dstar-slowdata-cases.md:268 | `test_dstar_sd_dvrouter::test_nibble_vectors_name_the_difference_and_both_results_are_recorded` | unit | a | PARTIAL |
| D-PRS limit 300 bytes including the CR; longer is rejected (Size) and discarded to the next delimiter (model only) | dstar-slowdata-cases.md:275 | `test_dstar_sd_dvrouter::test_model_limits_dprs_300_including_cr_and_nmea_82_without_the_terminator`, `test_dstar_sd_dvrouter::test_gps_boundary_vectors_today_and_after_dp5f`, `test_dstar_sd::test_decoder_limits_of_the_dvrouter_buffers` | unit | a | PARTIAL |
| NMEA line limit today: 82 stored bytes, terminator not counted, 84 with CR LF accepted and 85 rejected (model only) | dstar-slowdata-cases.md:278 | `test_dstar_sd_dvrouter::test_model_limits_dprs_300_including_cr_and_nmea_82_without_the_terminator`, `test_dstar_sd_dvrouter::test_gps_boundary_vectors_today_and_after_dp5f` | unit | a | PARTIAL |
| DP5f NMEA 0183 limit: 82 bytes including CR LF (83 and 84 become Reject Size), planned | dstar-slowdata-cases.md:283 | - | - | - | GAP |
| Fast data, header resend, code squelch and filler are passed through opaque (PARTIAL: no rig case for header, squelch, fast) | dstar-slowdata-cases.md:292 | `test_dstar_sd_dvrouter::test_model_passes_header_squelch_and_fast_data_through`, `test_dp15_streams::test_dp15_1d_native_dstar_slow_data_is_byte_identical` | unit/rig | a | PARTIAL |
| Stage 1c rule 1: unit is exactly 66 bytes (shorter TooShort, longer Size), state reset (model only) | dstar-slowdata-cases.md:319 | `test_dstar_sd_dvrouter::test_unit_vectors_are_stated_by_hand_and_the_model_agrees` | unit | a | PARTIAL |
| Stage 1c rule 2: bitmap little endian, LSB first, bit i = frame i (model only) | dstar-slowdata-cases.md:320 | `test_dstar_sd_dvrouter::test_unit66_layout_and_bit_order` | unit | a | PARTIAL |
| Stage 1c rule 3: top 3 bits of bitmap byte 2 set gives Invalid and a state reset (model only) | dstar-slowdata-cases.md:321 | `test_dstar_sd_dvrouter::test_unit66_layout_and_bit_order`, `test_dstar_sd_dvrouter::test_unit_vectors_are_stated_by_hand_and_the_model_agrees` | unit | a | PARTIAL |
| Stage 1c rule 4: bit 0 set and frame 0 not 55 2d 16 gives Invalid and a state reset (model only) | dstar-slowdata-cases.md:322 | `test_dstar_sd_dvrouter::test_unit_vectors_are_stated_by_hand_and_the_model_agrees` | unit | a | PARTIAL |
| Stage 1c rule 5: both bits of a pair set or the block is skipped and the GPS line reset (model only) | dstar-slowdata-cases.md:323 | `test_dstar_sd_dvrouter::test_model_hole_skips_one_block_and_resets_only_the_gps_line` | unit | a | PARTIAL |
| Stage 1c rule 6: unscramble 70 4f 93 over the 6 bytes of a block (SUT decodes scrambled text and D-PRS) | dstar-slowdata-cases.md:324 | `test_dp15_streams::test_dp15_1a_tx_message_becomes_the_status`, `test_dp15_streams::test_dp15_1b_dprs_beacon_is_a_position`, `test_dstar_sd_dvrouter::test_unit66_of_a_rendered_superframe_keeps_the_wire_bytes` | rig/unit | a | OK |
| Stage 1c rule 7: only 0x30..0x35 carry GPS bytes, other header bytes ignored without resetting the line (model only) | dstar-slowdata-cases.md:325 | `test_dstar_sd_dvrouter::test_model_ignores_other_header_bytes_without_resetting_the_gps_line` | unit | a | PARTIAL |
| Stage 1c rule 8: CR or LF finish a line, empty line gives nothing, limits 82 and 300 (model only) | dstar-slowdata-cases.md:326 | `test_dstar_sd_dvrouter::test_model_cr_or_lf_ends_a_line_and_an_empty_line_gives_nothing`, `test_dstar_sd_dvrouter::test_model_limits_dprs_300_including_cr_and_nmea_82_without_the_terminator` | unit | a | PARTIAL |
| Stage 1c rule 9: a push past the limit gives Size and drops everything to the next CR or LF (model only) | dstar-slowdata-cases.md:327 | `test_dstar_sd_dvrouter::test_model_overlong_line_is_discarded_to_the_next_delimiter` | unit | a | PARTIAL |
| Stage 1c rule 10: D-PRS needs the CR (an LF gives Invalid) (model only) | dstar-slowdata-cases.md:328 | `test_dstar_sd_dvrouter::test_model_cr_or_lf_ends_a_line_and_an_empty_line_gives_nothing`, `test_dstar_sd_dvrouter::test_crc_reject_and_terminator_vectors` | unit | a | PARTIAL |
| Stage 1c rule 11: a line error resets the GPS line, reported only when the unit has no item (model only) | dstar-slowdata-cases.md:329 | `test_dstar_sd_dvrouter::test_model_reports_a_line_error_only_when_the_unit_has_no_text_item` | unit | a | PARTIAL |
| Stage 1c rule 12: $GPRMC/$GPGGA with a bad or missing checksum give Reject Crc, other sentences ignored (model only) | dstar-slowdata-cases.md:330 | `test_dstar_sd_dvrouter::test_model_bad_checksum_is_a_crc_reject_and_resets_the_line`, `test_dstar_sd_dvrouter::test_crc_reject_and_terminator_vectors` | unit | a | PARTIAL |
| Stage 1c rule 13: D-PRS prefix wrong Invalid, CRC mismatch (CRC covers the CR) Reject Crc (model only) | dstar-slowdata-cases.md:331 | `test_dstar_sd_dvrouter::test_model_bad_checksum_is_a_crc_reject_and_resets_the_line`, `test_dstar_sd_dvrouter::test_crc_reject_and_terminator_vectors` | unit | a | PARTIAL |
| Stage 1c rule 14: text 0x40..0x43 into state, at most one text item per unit, persists across units (model + rig text) | dstar-slowdata-cases.md:332 | `test_dstar_sd_dvrouter::test_model_hands_out_one_text_per_unit`, `test_dstar_sd_dvrouter::test_model_keeps_the_text_across_units_today_and_resets_it_under_dp5f`, `test_dp15_streams::test_dp15_1a_tx_message_becomes_the_status` | unit/rig | a | PARTIAL |
| Stage 1c rule 15: a rejected unit (rules 1, 3, 4) resets the whole state, text included (model only) | dstar-slowdata-cases.md:333 | `test_dstar_sd_dvrouter::test_unit_vectors_are_stated_by_hand_and_the_model_agrees` | unit | a | PARTIAL |
| Corpus: every case has unit66, expected_dvrouter and a named difference for each oracle gap (22 of 63) | dstar-slowdata-cases.md:375 | `test_dstar_sd_dvrouter::test_every_case_has_the_dvrouter_fields_and_the_unit_reproduces_the_frames`, `test_dstar_sd_dvrouter::test_every_difference_between_expectation_and_lenient_result_is_named`, `test_dstar_sd_dvrouter::test_corpus_is_current_for_the_new_cases` | unit | a | OK |
| D-PRS source binding: a line whose source is another station is refused as spoof (not in the model) | dstar-slowdata-cases.md:381 | `test_dp15_streams::test_dp15_1b_dprs_beacon_is_a_position` | rig | a | OK |
| Valid $GPRMC/$GPGGA field parsing, the 29 byte station ID line, blank-text status rule (not modelled) | dstar-slowdata-cases.md:379 | - | - | - | GAP |
| Stage 2: synthetic RF stream (4800 baud, sync = 24 bit slow data field) is what DSDcc locks onto | dstar-slowdata-cases.md:408 | `test_oracle_dsdcc::test_frame_bits_are_lsb_first_and_end_with_the_sync_pattern`, `test_oracle_dsdcc::test_modulate_is_deterministic_two_level_and_sized`, `test_oracle_dsdcc::test_modulable_only_when_every_superframe_is_complete` | unit | a | OK |
| Stage 2: DSDcc recordings, recording 1 carries a text message, recording 2 a D-PRS position (PARTIAL: summarizer only) | dstar-slowdata-cases.md:426 | `test_oracle_dsdcc::test_summary_of_a_recording_holds_no_content` | unit | b:DSDcc | PARTIAL |
| DSDcc shows aligned/offset messages, repeats and text interleaved with D-PRS/NMEA like everyone | dstar-slowdata-cases.md:443 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc accumulates over a sync (split messages show the full text), unlike MMDVMHost and DP5f | dstar-slowdata-cases.md:444 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on`, `test_oracle_dsdcc::test_new_cases_of_stage_1c_are_recorded_and_the_text_view_is_recomputed_without_docker` | unit | b:DSDcc | OK |
| DSDcc old and new blocks mix: text-stale-mix shows the DStarGateway string | dstar-slowdata-cases.md:445 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc shows both messages of text-two-messages-split (recorded in dsdcc.json, content not asserted) | dstar-slowdata-cases.md:446 | - | - | - | GAP |
| DSDcc leaves a missing block's place blank (text-missing-block-never) | dstar-slowdata-cases.md:447 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc decodes the straddling layout (self-aligning byte counter, no frame pairs) | dstar-slowdata-cases.md:448 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc does not strip bit 7: bytes outside 32..127 become a dot | dstar-slowdata-cases.md:449 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc practically does not decode the radio header resend (only header-printable-bytes passes) | dstar-slowdata-cases.md:450 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc decodes D-PRS only when a text or header block starts a later superframe (dprs-then-text) | dstar-slowdata-cases.md:451 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on` | unit | b:DSDcc | OK |
| DSDcc 256 byte GPS buffer without bound check (long lines overrun; reference defect) | dstar-slowdata-cases.md:452 | - | - | - | GAP |
| DSDcc shows nothing for NMEA, squelch, fast data and filler | dstar-slowdata-cases.md:453 | `test_oracle_dsdcc::test_dsdcc_findings_the_doc_relies_on`, `test_oracle_dsdcc::test_new_cases_of_stage_1c_are_recorded_and_the_text_view_is_recomputed_without_docker` | unit | b:DSDcc | OK |
| Open question 3: a rig case that sends the corpus overs (over_datagrams) to a DV-Router edge | dstar-slowdata-cases.md:486 | - | - | - | GAP |

## DP7 (D-PRS positions)
Sources: dp7-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| DP7-a: every admitted position is public (anonymous state, public SSE, operator feed) for D-STAR, M17, YSF, DMR | dp7-cases.md:41 | `test_dp15_positions::test_dp7_a_position_is_public` | rig | a | OK |
| Public position row has only the allow-listed keys, never in the user message history | dp7-cases.md:24 | `test_dp15_positions::test_dp7_a_position_is_public` | rig | a | OK |
| Position and object share 1 per 30 s per base callsign (denied/10, result rate) | dp7-cases.md:33 | - | - | - | GAP |
| Page prints decimal degrees at the precision the station sent (5 places for level 0, 4 minus level otherwise) | dp7-cases.md:31 | `test_dp15_positions::test_dp7_c_ambiguity_in_m17_packet_mode`, `test_dp7_positions_page::test_positions_page_without_a_basemap` | rig/browser | c | OK |
| DP7-b: `[data] positions = false` drops all positions and objects (7 sources, counters rise, nothing public) | dp7-cases.md:42 | `test_dp15_positions::test_dp7_b_positions_false_drops_everything` | rig | a | OK |
| DP7-b: `position_optout` matches the base callsign on every source (counters rise, absent on all surfaces) | dp7-cases.md:43 | `test_dp15_positions::test_dp7_b_position_optout` | rig | a | OK |
| DP7-b: account `hide_position` drops D-STAR, M17, YSF, object; the retained row leaves; flag cleared restores | dp7-cases.md:44 | `test_dp15_positions::test_dp7_b_account_hide_position` | rig | a | OK |
| DP7-b: the worker applies the same drop controls to direct RF transit (nothing of the opted-out call at aprsc) | dp7-cases.md:45 | `test_dp15_positions::test_dp7_b_rf_transit` | rig | c | OK |
| DP7-b: dropped before publication, rendering, ride-along or APRS-IS uplink (control uplinked, opt-out not) | dp7-cases.md:46 | `test_dp15_positions::test_dp7_b_uplink_to_aprsis` | rig | c | OK |
| DP7-c: position body carries ambiguity 0..4 and the box centre (M17 packet mode, source 5) | dp7-cases.md:47 | `test_dp15_positions::test_dp7_c_ambiguity_in_m17_packet_mode` | rig | a | OK |
| DP7-c: the same for RF-heard APRS positions (levels 1..4) | dp7-cases.md:48 | `test_dp15_positions::test_dp7_c_ambiguity_over_rf` | rig | a | OK |
| DP7-c: D-PRS GPS-A line with 2 blanked digits is source 1 ambiguity 2, uplink re-applies it (FAILS: DP7-FIND1) | dp7-cases.md:49 | `test_dp15_positions::test_dp7_c_dprs_ambiguity_and_uplink` | rig | c | OK |
| DP7-c: ambiguity is 0 for every GPS fix (YSF GPS, DMRG, full precision D-PRS, M17 position) | dp7-cases.md:50 | `test_dp15_positions::test_dp7_c_gps_sources_never_claim_ambiguity` | rig | a | OK |
| DP7-d: an object kill emits only name and killed state, no lat/lon key at any depth; object leaves listing | dp7-cases.md:51 | `test_dp15_positions::test_dp7_d_object_kill_is_coordinate_free` | rig | a | OK |
| Rebased DP15 1b, 2d, 4a, 4b assert the anonymous public view (SSE, state, source, symbol, ambiguity 0) | dp7-cases.md:67 | `test_dp15_streams::test_dp15_1b_dprs_beacon_is_a_position`, `test_dp15_streams::test_dp15_2d_m17_packet_aprs_is_a_position`, `test_dp15_streams::test_dp15_4a_ysf_gps_fix_is_one_position`, `test_dp15_streams::test_dp15_4b_dmrg_is_one_position` | rig | a | OK |
| NXDN positions are covered (after the HWFREE1 generator) | dp7-cases.md:142 | - | - | - | GAP |
| Object ambiguity levels are covered | dp7-cases.md:142 | - | - | - | GAP |
| NMEA GPS mode of a D-STAR radio ($GPRMC + $GPGGA + ID line) as a position source | dp7-cases.md:139 | - | - | - | GAP |
| DP7-e courtesy: ONE courtesy position follows a message gated to RF (courtesy_posit = true), second not sent | dp7-cases.md:165 | `test_dp15_positions2::test_dp7_e_courtesy_posit_after_a_gated_message` | rig | c | OK |
| DP7-e no credit: a position with no gated message before it is not transmitted | dp7-cases.md:166 | `test_dp15_positions2::test_dp7_e_no_courtesy_without_a_gated_message` | rig | c | OK |
| DP7-e opt-out / positions=false apply to the courtesy posit (message sent, position not) | dp7-cases.md:167 | `test_dp15_positions2::test_dp7_e_no_courtesy_for_optout_or_positions_off` | rig | c | OK |
| DP7-e hide_position applies to the courtesy posit; cleared flag transmits it | dp7-cases.md:168 | `test_dp15_positions2::test_dp7_e_no_courtesy_for_a_hidden_account` | rig | c | OK |
| DP7-e courtesy_posit = false on the path: message transmitted, position not | dp7-cases.md:169 | `test_dp15_positions2::test_dp7_e_no_courtesy_when_the_path_has_it_off` | rig | c | OK |
| DP7-e transmit off: no data frame at the TNC | dp7-cases.md:170 | `test_dp15_positions2::test_dp7_e_nothing_on_rf_when_transmit_is_off` | rig | c | OK |
| DP7-f hide_position applies to RF-heard APRS and RF transit (counters, absence, no line at aprsc) | dp7-cases.md:171 | `test_dp15_positions2::test_dp7_f_hide_position_drops_rf_heard_positions` | rig | c | OK |
| DP7-g SSE replay and state omit a hidden account; cleared flag brings the row back | dp7-cases.md:172 | `test_dp15_positions2::test_dp7_g_sse_replay_and_state_after_a_hide` | rig | a | OK |
| DP7-h APRS items `)NAME!` over M17 and RF become object rows; timestamp-free kill is coordinate-free | dp7-cases.md:173 | `test_dp15_positions2::test_dp7_h_item_position_and_kill` | rig | a | OK |
| DP7-i position retention 30 minutes (present at 2 and 25 min, gone by 33 min; opt-in env, 35 minute run) | dp7-cases.md:174 | `test_dp15_positions2::test_dp7_i_position_retention_thirty_minutes` | rig | a | PARTIAL |
| DOM without a basemap: note shown, map hidden, table at the sent precision, only the page origin requested | dp7-cases.md:175 | `test_dp7_positions_page::test_positions_page_without_a_basemap` | browser | b:browser | OK |
| DOM with a basemap: map visible, attribution, manifest and archive from own origin, one circle per row (needs file) | dp7-cases.md:176 | `test_dp7_positions_page::test_positions_page_with_a_basemap` | browser | b:browser | PARTIAL |
| ETag idle: weak validator identical across samples, conditional GET and HEAD give 304 | dp7-cases.md:177 | `test_web_etag_diag::test_public_etag_idle_and_after_a_real_change` | web | a | OK |
| ETag after a real change (link, position, over): ETag differs, old ETag gives 200, current gives 304 | dp7-cases.md:177 | `test_web_etag_diag::test_public_etag_idle_and_after_a_real_change` | web | a | OK |
| DP7-dprs-a: a D-PRS position keeps its ambiguity level 1, 3, 4 on all three surfaces (FAILS: DP7-FIND1) | dp7-cases.md:278 | `test_dp7_dprs::test_dp7_dprs_a_ambiguity_level` | rig | a | OK |
| DP7-dprs-b: a compressed position in a D-PRS line is a position (49.5 N, -72.75) | dp7-cases.md:279 | `test_dp7_dprs::test_dp7_dprs_b_compressed_position` | rig | a | OK |
| DP7-dprs-b: a Mic-E position in a D-PRS line is a position (33 25.64 N 112 07.74 W) | dp7-cases.md:280 | `test_dp7_dprs::test_dp7_dprs_b_mice_position` | rig | a | OK |
| DP7-dprs-b: Mic-E with the spec destination (longitude offset +0) is admitted, not rejected | dp7-cases.md:281 | `test_dp7_dprs::test_dp7_dprs_b_mice_offset_zero_position` | rig | a | OK |
| DP7-dprs-c: an ambiguous D-PRS position gives no GNSS META in the M17 stream LSF; an unambiguous one does | dp7-cases.md:282 | `test_dp7_dprs::test_dp7_dprs_c_ambiguous_position_gives_no_m17_gnss_meta` | rig | a | OK |
| DP7-dprs-d: the D-STAR ride-along re-applies the sent ambiguity level in the D-PRS line | dp7-cases.md:283 | `test_dp7_dprs::test_dp7_dprs_d_ride_along_keeps_the_level` | rig | a | OK |
| DP7-dprs-d: the APRS-IS uplink re-applies the sent ambiguity level | dp7-cases.md:284 | `test_dp7_dprs::test_dp7_dprs_d_uplink_keeps_the_level` | rig | c | OK |

## Stream registry (LEAK2)
Sources: stream-registry-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| L1: remote-origin RTS whose sid is open in another room is refused, no frame delivered | stream-registry-cases.md:44 | `test_stream_registry::test_l1_loop_refused_while_first_stream_open` | interlink | c | OK |
| Refusal is DENY 5, silent (PARTIAL: only silence and 0 frames are observed, the DENY code is not) | stream-registry-cases.md:9 | `test_stream_registry::test_l1_loop_refused_while_first_stream_open` | interlink | c | PARTIAL |
| L1/L2 control: a stream with another sid in the same holder over is delivered (all 60 frames) | stream-registry-cases.md:44 | `test_stream_registry::test_l1_loop_refused_while_first_stream_open`, `test_stream_registry::test_l2_refresh_keeps_the_entry_alive` | interlink | c | OK |
| L2: the 1 s streamev re-put keeps the entry alive beyond the 5 s expiry while the stream is open | stream-registry-cases.md:45 | `test_stream_registry::test_l2_refresh_keeps_the_entry_alive` | interlink | c | OK |
| A close removes the registry entry at once | stream-registry-cases.md:8 | - | - | - | GAP |
| L3: a killed controller sends no refresh; its entry expires 5 s after the last one (P2 at kill+6.5 s passes) | stream-registry-cases.md:46 | `test_stream_registry::test_l3_stale_entry_expires_after_a_controller_kill` | interlink | c | OK |
| L3 P1: the entry is still held at kill+1.5 s (PARTIAL: recorded as data, not asserted) | stream-registry-cases.md:46 | `test_stream_registry::test_l3_stale_entry_expires_after_a_controller_kill` | interlink | c | PARTIAL |
| L4: the restarted controller serves local overs again (some over heard within the poll budget) | stream-registry-cases.md:47 | `test_stream_registry::test_l4_restarted_controller_works_and_nothing_else_restarted` | interlink | c | OK |
| L4: exactly one dv-room is replaced, the other controllers and the launcher are the same processes | stream-registry-cases.md:47 | `test_stream_registry::test_l4_restarted_controller_works_and_nothing_else_restarted` | interlink | c | OK |
| Accepted tolerance: with 4 or more refreshes lost a looped stream may be admitted for 0 to 1 s | stream-registry-cases.md:10 | - | - | - | GAP |

## Config load (COMPAT1)
Sources: config-load-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Baseline: the rendered rig template loads (exit 0, no error line) | config-load-cases.md:42 | `test_config_load::test_config_load` | cfgload | a | OK |
| An old key is never silently accepted: exit status 0 on a REJECT case is the loudest failure | config-load-cases.md:33 | `test_config_load::test_config_load`, `test_cfgload::test_reject_silent_success_is_the_loudest_problem` | cfgload | a | OK |
| A rejected file exits 1 or 2 (wip-488b89d: 2) with a FILE:LINE:COL: KEY: MESSAGE line naming dv-router.toml, the line and the key | config-load-cases.md:33 | `test_config_load::test_config_load`, `test_cfgload::test_reject_ok_when_file_line_key_and_message_are_there` | cfgload | a | OK |
| `[admin] totp` is refused (now totp_required), key admin.totp | config-load-cases.md:43 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[web] operator_totp` is refused (now totp_required), key web.operator_totp | config-load-cases.md:44 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[[edges.xlx.peer]]` row without `kind` is refused (missing field kind, line of the row header) | config-load-cases.md:45 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[edges.mmdvm]` is refused (now edges.dmrhb) | config-load-cases.md:46 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[fabric]` with `acl = true` is refused (the fabric ACL is gone) | config-load-cases.md:47 | `test_config_load::test_config_load` | cfgload | a | OK |
| An empty `[fabric]` table is refused | config-load-cases.md:48 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[access] allow_file` and `deny_file` are refused (lists go inline in allow and deny) | config-load-cases.md:49 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[callhome] interlink_file` is refused (the list is the xlx peer rows) | config-load-cases.md:50 | `test_config_load::test_config_load` | cfgload | a | OK |
| A typo key in `[web]` (enabel) is refused with the accepted field list | config-load-cases.md:51 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[web] enable = "yes"` (wrong type) is refused with the column of the value | config-load-cases.md:52 | `test_config_load::test_config_load` | cfgload | a | OK |
| `[log] level = 5` (wrong type) is refused | config-load-cases.md:53 | `test_config_load::test_config_load` | cfgload | a | OK |
| A TOML syntax error is refused (error line with no key) | config-load-cases.md:54 | `test_config_load::test_config_load` | cfgload | a | OK |
| Controls: the new spellings (totp_required, kind, inline allow and deny) load with exit 0 | config-load-cases.md:55 | `test_config_load::test_config_load` | cfgload | a | OK |
| An image without the `config check` subcommand reads as unsupported, never as every old key rejected | config-load-cases.md:67 | `test_config_check::test_judge_check_image_without_the_subcommand_is_unsupported_not_invalid`, `test_cfgload::test_accept_and_unsupported` | unit | a | OK |
| Warnings carry no `config warning:` word; a positioned line is a warning only when exit status is 0 | config-load-cases.md:69 | `test_config_check::test_judge_check_valid_exit_zero_keeps_positioned_lines_as_warnings` | unit | a | OK |
| `DV_MMDVM_*` environment names are ignored on compat1 (only `DV_DMRHB_*` counts); no file to refuse | config-load-cases.md:61 | - | - | - | GAP |

## Secret-file reload (SECRELOAD1)
Sources: secreload-cases.md (config.md "Actions", reload table; status.md launcher metrics).

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| A secret file replaced at the same path and a reload restart the owning child (`edges.aprsis.passcode_file` contents: Restart(dv-edge-aprsis)); the reply lists the action, a new process starts, `dv_launcher_child_restarts_total` rises by one | secreload-cases.md "Stages" 3, 5 | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path` | rig | b:aprsc | OK |
| The new bytes are what the edge uses: the fixture passcode logs in verified, a same-size throwaway one unverified, the fixture one again after an atomic replacement | secreload-cases.md "Stages" 3, 5 | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path` | rig | b:aprsc | OK |
| A same-size rewrite in place (same inode) and a rename-over replacement (new inode) are both detected | secreload-cases.md "Stages" 3, 5 | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path`, `test_secret_reload_case::test_write_in_place_keeps_the_inode_the_size_and_the_mode` | rig | b:aprsc | OK |
| The action preview names the dotted key and says `content changed` | secreload-cases.md "Stages" 0 | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path`, `test_secret_reload_helpers::test_capability_is_read_from_the_preview` | rig | b:aprsc | OK |
| An unchanged secret (and no other change) causes no restart | secreload-cases.md "Stages" 1, 2c, 4 | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path` | rig | b:aprsc | OK |
| An unreadable (mode 000) or missing explicit secret file refuses the reload with the key and the path and keeps the running generation (the old login stays up) | secreload-cases.md "Stages" 2a, 2b | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path` | rig | b:aprsc | OK |
| No secret value in any admin reply, note or file of the run, nor in the collected logs | secreload-cases.md "Stages" 6 | `test_secret_reload::test_secreload1_passcode_file_replaced_at_the_same_path`, `test_secret_reload_helpers::test_scan_tree_reports_paths_and_lines_never_values` | rig | b:aprsc | PARTIAL |
| A byte-identical TOML reload by SIGHUP re-reads the secrets: a replaced file restarts the owning child (counter +1, nothing else), a second SIGHUP restarts nothing, an unreadable file is refused and the old login stays up (the launcher-log line is an end-of-run check of run.sh) | secreload-cases.md "The SIGHUP variant" | `test_secret_reload::test_secreload1_sighup_reload_of_unchanged_toml` | rig | b:aprsc | OK |
| `GET /api/v1/admin/config/reload` previews the current file against the running generation: exact ReloadPreview shape, `changed` = dotted key names only, nothing applied | secreload-cases.md "SECRELOAD2" R0, R1 | `test_secret_reload::test_secreload2_preview_and_reload_apply_a_replaced_secret`, `test_secret_reload_helpers::test_preview_problems_accepts_the_specified_shape` | rig | b:aprsc | OK |
| `POST /api/v1/admin/config/reload` with `If-Match` applies a replaced secret without a TOML edit or history entry: ReloadApplied shape, owning child restarts, new value in effect | secreload-cases.md "SECRELOAD2" R2, R4 | `test_secret_reload::test_secreload2_preview_and_reload_apply_a_replaced_secret`, `test_secret_reload_helpers::test_applied_problems` | rig | b:aprsc | OK |
| A second reload is `no_change` with empty `changed` / `actions` and restarts nothing | secreload-cases.md "SECRELOAD2" R3 | `test_secret_reload::test_secreload2_preview_and_reload_apply_a_replaced_secret` | rig | b:aprsc | OK |
| An unreadable secret file is 422 `validation` naming the key and path (never content) on preview and POST, the running generation kept | secreload-cases.md "SECRELOAD2" R5 | `test_secret_reload::test_secreload2_preview_and_reload_apply_a_replaced_secret` | rig | b:aprsc | OK |
| The reload is audited as `config.reload` requested / applied / unchanged / invalid with key names only | secreload-cases.md "SECRELOAD2" R6 | `test_secret_reload::test_secreload2_preview_and_reload_apply_a_replaced_secret` | rig | b:aprsc | OK |
| No session 401; viewer and operator 403 `forbidden`, audited as `denied` | secreload-cases.md "SECRELOAD2" G1, G2, G4 | `test_secret_reload::test_secreload2_guards_and_preconditions` | rig | b:aprsc | OK |
| POST `If-Match`: missing 428, stale and weak 412 `etag_mismatch` with `current_etag`, audited `denied`, nothing reloads | secreload-cases.md "SECRELOAD2" G3, G4 | `test_secret_reload::test_secreload2_guards_and_preconditions` | rig | b:aprsc | OK |
| Both routes need step-up: 403 `step_up_required` after the 5 minute window | secreload-cases.md "SECRELOAD2" G5 | `test_secret_reload::test_secreload2_guards_and_preconditions` | rig | b:aprsc | OK |
| The reload routes for a node-link account key (`edges.link.account[N].secret_file`), 409 conflict, 429, 503 | secreload-cases.md "SECRELOAD2" | - | - | - | GAP |
| Link, AllStar, TURN and callhome secret kinds follow the same rule | secreload-cases.md "Limits" | - | - | - | GAP |

## Session rotation (WEBSID1)
Sources: websid1-cases.md (web.md "Built (WEBSID1)", review-248). Written offline, not run.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| A step-up rotates the cookie and the CSRF token; the old cookie answers 401 at once | websid1-cases.md "Cases" 1 | `test_web_session_rotation::test_step_up_keeps_sse_circuit_and_wav` | rig | browser | OK |
| An open operator SSE stream, a Listen circuit and a WAV transfer survive the rotation (no `bye`, no RTC loss, no truncation); the new cookie renews and closes the circuit | websid1-cases.md "Cases" 1 | `test_web_session_rotation::test_step_up_keeps_sse_circuit_and_wav` | rig | browser | OK |
| Caps count across rotations: a ninth circuit and a third WAV body are refused after a step-up, the TURN issue budget does not refill | websid1-cases.md "Cases" 2 | `test_web_session_rotation::test_step_up_does_not_multiply_the_caps` | rig | browser | PARTIAL |
| Logout, own-session revoke, role change and account disable end the stream (one heartbeat), the circuit (the 5 s sweep) and the WAV body (cut) | websid1-cases.md "Cases" 3 | `test_web_session_rotation::test_session_end_ends_the_handles` | rig | browser | PARTIAL |
| A repeated reload denial across a step-up is one record per 10 s with `suppressed` on the next | websid1-cases.md "Cases" 4 | `test_web_session_rotation::test_denials_coalesce_across_a_step_up`, `test_web_session_rotation_case::test_denials_coalesce_across_the_step_up_with_a_stable_session_key` | rig | a:admin | OK |
| Idle and absolute expiry end the handles; TOTP enable / replace / disable keep current handles and end siblings | websid1-cases.md "GAPs" | - | - | - | GAP |
| A WAV body ends within 100 ms of the end of the session | websid1-cases.md "GAPs" | - | - | - | GAP |

## M17
Sources: protocols/m17.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-M17-01 CONN for a configured module answered by ACKN, exactly 4 B | protocols/m17.md:111 | `test_m17::test_m17_conn_ack` | rig | a | OK |
| P-M17-02 CONN callsign with a 9th character links and receives (PARTIAL: sut == ref only) | protocols/m17.md:112 | `test_m17::test_m17_conn_suffix` | rig | a | PARTIAL |
| P-M17-03 CONN for an unconfigured module gets 4 B NACK, no link (PARTIAL: skips unless DVT_UNSERVED_MODULE is set) | protocols/m17.md:113 | `test_m17::test_m17_conn_unconfigured_module` | rig | a | PARTIAL |
| P-M17-04 CONN with bad module byte or undecodable callsign links nothing; NACK for bad module | protocols/m17.md:114 | `test_m17::test_m17_conn_bad_module` | rig | a | OK |
| P-M17-05 CONN/LSTN of another length (10, 12 B) is ignored (PARTIAL: sut == ref only) | protocols/m17.md:115 | `test_m17::test_m17_conn_wrong_size` | rig | a | PARTIAL |
| P-M17-06 LSTN links a listen-only client: ACKN, it hears the module, its own streams not relayed | protocols/m17.md:116 | `test_m17::test_m17_listen_only`, `test_m17::test_m17_listen_only_cannot_talk` | rig | a | OK |
| P-M17-07 Repeated CONN: same module refreshed with ACKN, other module moves the client | protocols/m17.md:117 | `test_m17::test_m17_reconn` | rig | a | OK |
| P-M17-08 Same callsign from two ports is two clients, each gets the stream once (PARTIAL: sut == ref only) | protocols/m17.md:118 | `test_m17::test_m17_two_ports` | rig | a | PARTIAL |
| P-M17-09 PING 10 B with reflector callsign to every client every 3 s (PARTIAL: callsign not asserted) | protocols/m17.md:124 | `test_m17::test_m17_ping_interval` | rig | a | PARTIAL |
| P-M17-10 Unsolicited PONG every 8 s without answering PING keeps the link | protocols/m17.md:125 | `test_m17::test_m17_unsolicited_pong_keeps_link` | rig+slow | a | OK |
| P-M17-11 Client PING accepted as keepalive (PARTIAL: sut == ref only) | protocols/m17.md:126 | `test_m17::test_m17_client_ping_keepalive` | rig+slow | a | PARTIAL |
| P-M17-12 Client silent for 30 s is dropped and sent DISC 10 B (PARTIAL: DISC form asserted, 30 s timing only printed) | protocols/m17.md:127 | `test_m17::test_m17_silent_client_dropped` | rig+slow | a | PARTIAL |
| P-M17-13 Dropped client gets no more PING or traffic, other clients kept (PARTIAL: silent_heard only sut == ref) | protocols/m17.md:128 | `test_m17::test_m17_dropped_client_no_traffic` | rig+slow | a | PARTIAL |
| P-M17-14 Stream packets reset the timeout, a long over without PONG keeps the link (PARTIAL: sut == ref only) | protocols/m17.md:129 | `test_m17::test_m17_stream_resets_timeout` | rig+slow | a | PARTIAL |
| P-M17-15 DISC 10 B answered with DISC 4 B, client removed, no more PING or traffic | protocols/m17.md:135 | `test_m17::test_m17_disc_ack` | rig | a | OK |
| P-M17-16 Repeated DISC from an unlinked address acked again with 4 B | protocols/m17.md:136 | `test_m17::test_m17_disc_repeat` | rig | a | OK |
| P-M17-17 DISC of another length (4 B, 11 B) ignored, link stays (PARTIAL: sut == ref only) | protocols/m17.md:137 | `test_m17::test_m17_disc_wrong_size` | rig | a | PARTIAL |
| P-M17-18 DISC 10 B to every client on orderly shutdown | protocols/m17.md:138 | `test_m17::test_m17_disc_on_shutdown` | rig+slow | a | OK |
| P-M17-19 54 B stream from a linked client relayed to the module's other clients | protocols/m17.md:144 | `test_m17::test_m17_stream_relayed`, `test_m17_relay::test_m17_relay_client_stream_to_all` | rig | a | OK |
| P-M17-20 First packet FN 0 carries voice and is relayed with its payload | protocols/m17.md:145 | `test_floor::test_m17_frame0_relayed` | rig | a | OK |
| P-M17-21 Stream opens on any packet with a new stream id (FN 0 lost) | protocols/m17.md:146 | `test_m17::test_m17_stream_without_fn0` | rig | a | OK |
| P-M17-22 Last-frame bit 15 ends the stream, module free at once | protocols/m17.md:147 | `test_m17::test_m17_last_frame_closes`, `test_m17_relay::test_m17_relay_last_frame_frees_module` | rig | a | OK |
| P-M17-23 Stream without last frame ends after silence timeout, listeners get a closing packet | protocols/m17.md:148 | `test_m17_relay::test_m17_relay_timeout_client_stream`, `test_xlxd_clients::test_stream_timeout_frees_module` | rig | a | OK |
| P-M17-24 Destination field does not decide routing: all forms relayed on the linked module only | protocols/m17.md:149 | `test_m17::test_m17_destination_forms` | rig | a | OK |
| P-M17-25 Stream packet with a wrong CRC is relayed (PARTIAL: sut == ref only) | protocols/m17.md:150 | `test_m17::test_m17_bad_crc_relayed` | rig | a | PARTIAL |
| P-M17-26 Every unencrypted stream TYPE relayed unchanged same-mode (PARTIAL: cross-mode 1600 / data-only rule not asserted) | protocols/m17.md:151 | `test_m17::test_m17_type_variants` | rig | a | PARTIAL |
| P-M17-27 Source callsign relayed as sent, need not equal the linked callsign (PARTIAL: sut == ref only) | protocols/m17.md:152 | `test_m17::test_m17_source_not_login` | rig | a | PARTIAL |
| P-M17-28 Stream id 0 is not relayed (no M17 case; the claimed B8 case is D-STAR) | protocols/m17.md:153 | - | - | - | GAP |
| P-M17-29 Stream packets from an unlinked address are not relayed | protocols/m17.md:154 | `test_m17::test_m17_unlinked_stream_dropped` | rig | a | OK |
| P-M17-30 A listen-only client's stream is not relayed | protocols/m17.md:155 | `test_m17::test_m17_listen_only_cannot_talk`, `test_m17_relay::test_m17_relay_listen_only_not_relayed` | rig | a | OK |
| P-M17-31 Stream packets of 53, 55, 56 B from a client are not relayed (PARTIAL: sut == ref only) | protocols/m17.md:156 | `test_m17::test_m17_stream_wrong_size` | rig | a | PARTIAL |
| P-M17-32 Second stream on a busy module is not mixed (PARTIAL: M17 asserted client vs peer origin only; B1/B2 cases are D-STAR/DMR) | protocols/m17.md:157 | `test_m17_relay::test_m17_relay_floor_peer_first`, `test_m17_relay::test_m17_relay_floor_client_first` | rig | a | PARTIAL |
| P-M17-33 The talker never receives its own stream back | protocols/m17.md:158 | `test_m17_relay::test_m17_relay_client_stream_to_all`, `test_m17_packet::test_m17p_does_not_take_the_floor` | rig | a | OK |
| P-M17-34 Egress 54 B, FN counts up by one, bit 15 on the last only | protocols/m17.md:164 | `test_m17::test_m17_egress_fn`, `test_m17_relay::test_m17_relay_client_stream_to_all` | rig | a | OK |
| P-M17-35 Egress stream id equals the ingress stream id | protocols/m17.md:165 | `test_floor::test_sid_preserved` | rig | a | OK |
| P-M17-36 Egress destination unchanged, a reflector designation becomes broadcast (PARTIAL: reflector8 form has no spec entry, case ends skipped) | protocols/m17.md:166 | `test_m17::test_m17_egress_destination` | rig | a | PARTIAL |
| P-M17-37 Egress CRC valid for the packet as sent | protocols/m17.md:167 | `test_m17::test_m17_egress_crc` | rig | a | OK |
| P-M17-38 Egress TYPE and META kept M17 to M17; other-mode origin gets TYPE 0x0005 and zero META | protocols/m17.md:168 | `test_m17::test_m17_egress_type_meta`, `test_crossmode_exact::test_m17_meta_untouched`, `test_crossmode_exact::test_cross_pair_exact` | rig/crossmode | a | OK |
| P-M17-39 Egress source is the talker's source callsign | protocols/m17.md:169 | `test_m17::test_m17_egress_source` | rig | a | OK |
| P-M17-40 A client that links mid-stream receives from the next packet (within 200 ms) | protocols/m17.md:170 | `test_m17::test_m17_late_link_receives` | rig | a | OK |
| P-M17-45 No datagram to a client exceeds 100 B | protocols/m17.md:171 | `test_m17::test_m17_egress_max_size` | rig | a | OK |
| P-M17-41 M17P (38-859 B) from a linked client forwarded unchanged to the module's clients | protocols/m17.md:177 | `test_m17::test_m17_packet_mode`, `test_m17_packet::test_m17p_legacy_type` | rig | a | OK |
| P-M17-42 Stream or M17P to PARROT echoed to sender (doc: record only; spec asserts no echo) | protocols/m17.md:178 | `test_m17::test_m17_parrot` | rig | a | PARTIAL |
| P-M17-43 Unknown magic, short and oversize M17 packets get no reply, reflector keeps serving (PARTIAL: sut == ref only) | protocols/m17.md:179 | `test_m17::test_m17_garbage_ignored` | rig | a | PARTIAL |
| P-M17-44 37 B reflector CONN from an unconfigured peer not linked, sut answers 4 B NACK | protocols/m17.md:180 | `test_m17::test_m17_peer_conn_unconfigured` | rig | a | OK |
| P-M17-46 Reflector sends 37 B CONN (designation, peer module list) to an unlinked peer from port 17000 | protocols/m17.md:317 | `test_m17_peer_link::test_m17_peer_conn_sent` | rig | a | OK |
| P-M17-47 Peer 37 B CONN answered with 37 B ACKN, no link by itself, own CONN at once | protocols/m17.md:318 | `test_m17_peer_link::test_m17_peer_conn_answered` | rig | a | OK |
| P-M17-48 Outbound link only by a matching 37 B ACKN (PARTIAL: CONN retry after NACK only recorded) | protocols/m17.md:319 | `test_m17_peer_link::test_m17_peer_only_matching_ackn_links` | rig+slow | a | PARTIAL |
| P-M17-49 Unanswered CONN repeated after 3, 6, 12, 24 s then every 30 s | protocols/m17.md:320 | `test_m17_peer_link::test_m17_peer_conn_retry_schedule` | rig+slow | a | OK |
| P-M17-50 One 10 B PING with designation every 3 s per linked peer, two-module peer same rate | protocols/m17.md:321 | `test_m17_peer_link::test_m17_peer_ping_cadence` | rig | a | OK |
| P-M17-51 PING-only or PONG-only peer stays linked 45 s, no DISC, CONN or PONG from reflector | protocols/m17.md:322 | `test_m17_peer_link::test_m17_peer_keepalive_ping_or_pong_only` | rig+slow | a | OK |
| P-M17-52 Peer silent 30 s is dropped; any accepted datagram refreshes the timer | protocols/m17.md:323 | `test_m17_peer_link::test_m17_peer_silent_is_dropped`, `test_m17_peer_link::test_m17_peer_traffic_refreshes_timer` | rig+slow | a | OK |
| P-M17-53 Peer DISC 10 B removes every module session, no reply, nothing sent or relayed | protocols/m17.md:324 | `test_m17_peer_link::test_m17_peer_disc_removes_all_modules` | rig | a | OK |
| P-M17-54 Orderly stop sends one DISC 10 B with designation to every linked peer | protocols/m17.md:325 | `test_m17_peer_link::test_m17_peer_disc_on_stop` | rig+slow | a | OK |
| P-M17-55 Repeated CONN from a linked peer gets ACKN again, link kept, stream arrives once | protocols/m17.md:326 | `test_m17_peer_link::test_m17_peer_repeat_conn` | rig | a | OK |
| P-M17-56 Datagrams from a DISCed or half-linked peer endpoint reach no client | protocols/m17.md:332 | `test_m17_peer_link::test_m17_peer_unlinked_ingress_dropped` | rig | a | OK |
| P-M17-57 Datagrams from a non-configured endpoint dropped without reply while the peer is linked | protocols/m17.md:333 | `test_m17_peer_link::test_m17_peer_foreign_endpoint_dropped` | rig | a | OK |
| P-M17-58 CONN with an identity not accepted: 4 B NACK, no link, peer links stay up | protocols/m17.md:334 | `test_m17_peer_link::test_m17_peer_conn_identity_foreign_endpoint`, `test_m17_peer_link::test_m17_peer_conn_identity_configured_endpoint` | rig | a | OK |
| P-M17-59 CONN with a wrong module set: 4 B NACK, no link; same set in another order links | protocols/m17.md:335 | `test_m17_peer_link::test_m17_peer_conn_wrong_module_set` | rig | a | OK |
| P-M17-60 Malformed 37 B CONN: 4 B NACK, 36/38 B and LSTN not a peer CONN, reflector keeps serving | protocols/m17.md:336 | `test_m17_peer_link::test_m17_peer_conn_malformed` | rig+slow | a | OK |
| P-M17-61 Client stream reaches module clients (listen-only too) and every peer serving it as 55 B | protocols/m17.md:342 | `test_m17_relay::test_m17_relay_client_stream_to_all` | rig | a | OK |
| P-M17-62 Peer routing by module: a stream on N reaches only the peer serving N | protocols/m17.md:343 | `test_m17_relay::test_m17_relay_routes_by_module` | rig | a | OK |
| P-M17-63 Peer 55 B stream reaches the module's clients as exactly 54 B, fields unchanged | protocols/m17.md:344 | `test_m17_relay::test_m17_relay_peer_stream_to_clients` | rig | a | OK |
| P-M17-65 A listen-only client's stream is relayed to no peer and no client | protocols/m17.md:345 | `test_m17_relay::test_m17_relay_listen_only_not_relayed` | rig | a | OK |
| P-M17-66 One stream per module across origins, the late stream not mixed or relayed | protocols/m17.md:346 | `test_m17_relay::test_m17_relay_floor_peer_first`, `test_m17_relay::test_m17_relay_floor_client_first` | rig | a | OK |
| P-M17-67 Bit 15 reaches the peer and frees the module; timeout gives one synthesised last packet | protocols/m17.md:347 | `test_m17_relay::test_m17_relay_last_frame_frees_module`, `test_m17_relay::test_m17_relay_timeout_client_stream`, `test_m17_relay::test_m17_relay_timeout_peer_stream` | rig | a | OK |
| P-M17-69 Designation destinations become broadcast with CRC recomputed on every path; XLX/URF forms too | protocols/m17.md:348 | `test_m17_relay::test_m17_relay_destination_rewrite`, `test_m17_relay::test_m17_relay_destination_rewrite_xlx_urf` | rig | a | OK |
| P-M17-64 One hop: a peer datagram is never forwarded to any peer, streams and M17P | protocols/m17.md:354 | `test_m17_relay::test_m17_relay_peer_one_hop` | rig | a | OK |
| P-M17-68 Every stream datagram to a peer is 55 B with upper-case module trailer, CRC valid | protocols/m17.md:355 | `test_m17_relay::test_m17_relay_peer_egress_trailer` | rig | a | OK |
| P-M17-70 One-module peer may send a bare 54 B stream; a bare M17P loses its last byte and is dropped | protocols/m17.md:356 | `test_m17_relay::test_m17_relay_bare_stream_one_module_peer`, `test_m17_relay::test_m17_relay_bare_m17p_one_module_peer` | rig | a | OK |
| P-M17-71 Bare 54 B stream from a two-module peer is dropped, with trailer delivered | protocols/m17.md:357 | `test_m17_relay::test_m17_relay_bare_stream_two_module_peer_dropped` | rig | a | OK |
| P-M17-72 Trailer routes a multi-module peer's stream to that module's clients only | protocols/m17.md:358 | `test_m17_relay::test_m17_relay_trailer_routes_multi_module_peer` | rig | a | OK |
| P-M17-73 Trailer outside the peer's set, 0x00 or lower case dropped silently | protocols/m17.md:359 | `test_m17_relay::test_m17_relay_trailer_outside_set_multi_module_peer`, `test_m17_relay::test_m17_relay_trailer_outside_set_one_module_peer` | rig | a | OK |
| P-M17-74 Unencrypted legacy-TYPE SMS reaches module clients (n B) and sharing peers (n+1 B) | protocols/m17.md:370 | `test_m17_packet::test_m17p_legacy_type`, `test_m17_packet::test_m17p_legacy_type_second_module` | rig | a | OK |
| P-M17-75 v3-TYPE and 0x1000 M17P relayed with TYPE bytes unchanged, never SMS-ingested | protocols/m17.md:371 | `test_m17_packet::test_m17p_v3_type`, `test_m17_packet::test_m17p_v3_type_can5`, `test_m17_packet::test_m17p_other_layout` | rig | a | OK |
| P-M17-76 Non-SMS specifiers (RAW, AX.25, APRS, IPv4, Winlink, 2-byte) relayed natively | protocols/m17.md:372 | `test_m17_packet::test_m17p_specifier` | rig | a | OK |
| P-M17-77 Peer M17P reaches the trailer's module as n B, trailer stripped, one hop | protocols/m17.md:373 | `test_m17_packet::test_m17p_from_peer`, `test_m17_packet::test_m17p_from_peer_second_module` | rig | a | OK |
| P-M17-78 M17P from a listen-only client is relayed to nobody | protocols/m17.md:374 | `test_m17_packet::test_m17p_listen_only_sender` | rig | a | OK |
| P-M17-79 M17P designation destination becomes broadcast, LSF CRC recomputed, client and peer copy | protocols/m17.md:375 | `test_m17_packet::test_m17p_dst_rewrite`, `test_m17_packet::test_m17p_dst_rewrite_from_peer` | rig | a | OK |
| P-M17-80 Native relay checks no CRC; only a destination rewrite repairs the LSF CRC | protocols/m17.md:376 | `test_m17_packet::test_m17p_bad_crc_relayed` | rig | a | OK |
| P-M17-82 M17P with the stream bit set is invalid (PARTIAL: record only, asserts relay continues) | protocols/m17.md:377 | `test_m17_packet::test_m17p_stream_bit_type` | rig | a | PARTIAL |
| P-M17-83 M17P never takes the voice floor, the stream goes on undisturbed | protocols/m17.md:378 | `test_m17_packet::test_m17p_does_not_take_the_floor` | rig | a | OK |
| P-M17-84 M17P 38, 300, 859 B relayed byte for byte; 37 B and 860 B from a client dropped | protocols/m17.md:384 | `test_m17_packet::test_m17p_size_to_clients`, `test_m17_packet::test_m17p_size_37_dropped`, `test_m17_packet::test_m17p_size_860_from_client_dropped` | rig | a | OK |
| P-M17-85 M17P between reflectors is n+1 B; a peer stream reaches clients as exactly 54 B | protocols/m17.md:385 | `test_m17_packet::test_m17p_size_to_peer`, `test_m17_packet::test_m17p_size_from_peer`, `test_m17_packet::test_m17_stream_from_peer_to_clients_is_54_bytes`, `test_m17_packet::test_m17_stream_bare_from_peer_to_clients_is_54_bytes` | rig | a | OK |
| P-M17-81 M17P with an encrypted TYPE dropped at ingress from client and peer | protocols/m17.md:391 | `test_m17_packet::test_m17p_encrypted_type` | rig | a | OK |
| P-M17-86 Streams with an encrypted TYPE dropped at the SUT edge on all four paths | protocols/m17.md:392 | `test_m17_relay::test_m17_relay_encrypted_stream_blocked` | rig | a | OK |

## M17 v3
Sources: m17v3-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Readings: text block index is 1-based, control byte count<<4 or index (PARTIAL: our encoder only, DV-Router not run) | m17v3-cases.md:86 | `test_m17v3::test_v3_text_hand_values`, `test_m17v3::test_v3_text_limits`, `test_m17v3::test_v3_text_decoder_rejects_inconsistent_blocks` | unit | a | PARTIAL |
| Readings: altitude maximum is 32267.5 m (PARTIAL: our encoder only, DV-Router not run) | m17v3-cases.md:87 | `test_m17v3::test_gnss_extremes_and_signs` | unit | a | PARTIAL |
| Readings: AES counter is timestamp, random, FN last (PARTIAL: our encoder only, DV-Router not run) | m17v3-cases.md:88 | `test_m17v3::test_aes_iv_and_counter_readings` | unit | a | PARTIAL |
| Readings: GNSS radius field uses the 2.0.10 power-of-two rule (PARTIAL: our encoder only, libm17 not run) | m17v3-cases.md:89 | `test_m17v3::test_gnss_hand_values`, `test_m17v3::test_gnss_extremes_and_signs` | unit | a | PARTIAL |
| Readings: lat/lon cut toward zero, scale 8388607 per 90/180 deg (PARTIAL: our encoder only, libm17 not run) | m17v3-cases.md:90 | `test_m17v3::test_gnss_hand_values`, `test_m17v3::test_gnss_invalid_fields_are_zero_and_limits` | unit | a | PARTIAL |
| Collector complete_valid: valid, valid-meta-changes, boundary-max-frames (PARTIAL: reference collector, DV-Router not run) | m17v3-cases.md:125 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand`, `test_m17v3::test_boundary_frame_counts` | unit | a | PARTIAL |
| Collector complete_invalid_signature: tampered payload, wrong key, 5 digest variants (PARTIAL: reference collector only) | m17v3-cases.md:126 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand`, `test_m17v3::test_negative_builders_give_the_stated_verdicts` | unit | a | PARTIAL |
| Collector incomplete: missing tail frames, truncated mid-stream (PARTIAL: reference collector only) | m17v3-cases.md:127 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand` | unit | a | PARTIAL |
| Collector sequence_error: lost, reordered, duplicate, early tail, EOT, tail FN, one-too-many (PARTIAL: reference collector only) | m17v3-cases.md:128 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand`, `test_m17v3::test_new_negative_builders`, `test_m17v3::test_reorder_is_the_only_ambiguous_negative` | unit | a | PARTIAL |
| Collector size_error: 53 B, 55 B packets (PARTIAL: reference collector only) | m17v3-cases.md:129 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand` | unit | a | PARTIAL |
| Collector bad_public_key: off-curve, 63 B, zero key (PARTIAL: reference collector only) | m17v3-cases.md:130 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand`, `test_ecdsa_p256::test_malformed_signature_and_key` | unit | a | PARTIAL |
| Collector bad_signature_encoding: r or s zero, equal to n, s max (PARTIAL: reference collector only) | m17v3-cases.md:131 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand`, `test_ecdsa_p256::test_malformed_signature_and_key` | unit | a | PARTIAL |
| Collector null: unsigned streams with no tail expected (PARTIAL: reference collector only) | m17v3-cases.md:132 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts` | unit | a | PARTIAL |
| P26j-1 TYPE codec: v3_type rows give TYPE integer and 2 bytes, invalid rows refused (PARTIAL: vectors vs our encoder, DV-Router test not run) | m17v3-cases.md:155 | `test_m17v3::test_type_table_matches_hand_values`, `test_m17v3::test_type_bytes_follow_the_bit_table`, `test_m17v3::test_type_range_checks`, `test_m17v3::test_bad_type_rows_have_problems`, `test_m17v3::test_type_meta_vectors_match_encoders` | unit | a | PARTIAL |
| P26j-2 Legacy to v3 TYPE mapping, 1600 bps and reserved bits unmappable (PARTIAL: vectors vs our mapper only) | m17v3-cases.md:156 | `test_m17v3::test_legacy_table_matches_hand_values`, `test_m17v3::test_legacy_fields_of_known_values`, `test_m17v3::test_legacy_round_trips_through_convert_policy`, `test_m17v3::test_type_meta_vectors_match_encoders` | unit | a | PARTIAL |
| P26j-3 Gate matrix per signed stream, gate on then off (PARTIAL: reference verifier only, no rig run, gate-off not tested) | m17v3-cases.md:157 | `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_m17v3::test_collector_classes_by_hand`, `test_m17v3::test_negative_builders_give_the_stated_verdicts`, `test_m17v3::test_new_negative_builders`, `test_m17v3::test_boundary_frame_counts` | unit | a | PARTIAL |
| P26j-4 ECDSA P-256 signature verification over the 16 B digest (PARTIAL: own verifier and RFC 6979; m17-fme run was manual) | m17v3-cases.md:158 | `test_ecdsa_p256::test_rfc6979_sha256_vectors`, `test_ecdsa_p256::test_micro_ecc_left_padding_convention`, `test_ecdsa_p256::test_cross_check_with_openssl_binding`, `test_m17v3::test_ecdsa_vectors`, `test_m17v3::test_key_vectors`, `test_m17v3::test_digest_hand_values`, `test_m17v3::test_valid_stream_verifies_with_both_type_layouts` | unit | a | PARTIAL |
| P26j-5 META encodings: text v2/v3, GNSS, ECD, AES IV (PARTIAL: vectors vs our encoders, libm17/mrefd oracle not run) | m17v3-cases.md:159 | `test_m17v3::test_type_meta_vectors_match_encoders`, `test_m17v3::test_v3_text_hand_values`, `test_m17v3::test_v2_text_hand_values`, `test_m17v3::test_gnss_hand_values`, `test_m17v3::test_gnss_decoded_integers_by_hand`, `test_m17v3::test_ecd_layout`, `test_m17v3::test_aes_iv_and_counter_readings` | unit | a | PARTIAL |
| P26j-6 Legacy receiver gets META omitted (policy omit keeps AES IV) (PARTIAL: our mapper only) | m17v3-cases.md:160 | `test_m17v3::test_v3_to_legacy_omit_drops_meta_but_keeps_iv`, `test_m17v3::test_type_meta_vectors_match_encoders` | unit | a | PARTIAL |
| P26j-7 ECD META refused on an Internet link, packet not forwarded (no case asserts refusal; layout only) | m17v3-cases.md:161 | - | - | - | GAP |
| P26j-8 AES streams: gate expectation null on purpose (PARTIAL: IV layout vectors only, nothing to assert on the gate) | m17v3-cases.md:162 | `test_m17v3::test_aes_iv_and_counter_readings`, `test_m17v3::test_type_meta_vectors_match_encoders` | unit | a | PARTIAL |
| P26j-9 Valid stream re-labelled with legacy TYPE also verifies (PARTIAL: own verifier; m17-fme leg was manual) | m17v3-cases.md:163 | `test_m17v3::test_valid_stream_verifies_with_both_type_layouts`, `test_m17v3::test_signed_stream_vectors_give_their_stated_verdicts`, `test_oracle_m17::test_legacy_view_relabels_and_keeps_signature` | unit | a | PARTIAL |
| Run: micro-ecc digest convention accepted by m17-fme for valid, boundary and both keys (PARTIAL: one-time docker run, only parser/compare unit-tested) | m17v3-cases.md:192 | `test_oracle_m17::test_parse_valid_and_invalid`, `test_oracle_m17::test_compare_rules`, `test_oracle_m17::test_verify_entry_with_a_fake_runner` | unit | c | PARTIAL |
| Run: m17-fme prints Signature Invalid for every broken-signature or digest stream (PARTIAL: one-time docker run, only parser/compare unit-tested) | m17v3-cases.md:200 | `test_oracle_m17::test_parse_valid_and_invalid`, `test_oracle_m17::test_compare_rules` | unit | c | PARTIAL |
| Run: no signature line for streams not ending at FN 0x7FFF (PARTIAL: one-time docker run, only parser unit-tested) | m17v3-cases.md:207 | `test_oracle_m17::test_parse_colours_and_other_markers` | unit | c | PARTIAL |
| Run: m17-fme META decode equals decoded_float for GNSS, text and ECD rows (manual run only) | m17v3-cases.md:214 | - | - | - | GAP |
| Run: v3 TYPE prints only Unknown LSF TYPE in m17-fme, so v3 packets must be re-labelled (PARTIAL: parser classification unit-tested) | m17v3-cases.md:225 | `test_oracle_m17::test_parse_colours_and_other_markers`, `test_oracle_m17::test_legacy_view_relabels_and_keeps_signature` | unit | c | PARTIAL |
| Unverified: GNSS rows against libm17 and mrefd, step 7 (no C driver written) | m17v3-cases.md:252 | - | - | - | GAP |
| Not covered by design: META late-joiner timing, real v3 client interop, packet-mode v3 | m17v3-cases.md:165 | - | - | - | GAP |

## IMRS
Sources: protocols/imrs.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-IMR-01 Listen on UDP 21110 and send every packet to the peer IP at port 21110, not to the source port | protocols/imrs.md:123 | `test_imrs::test_imrs_port_21110` | rig | a | OK |
| P-IMR-02 A peer is identified by its IP address (one session per IP, two callsigns get one copy) | protocols/imrs.md:124 | `test_imrs::test_imrs_two_callsigns_one_ip` | rig | a | OK |
| P-IMR-03 A 16 B ping is answered with a 60 B info packet and a 16 B ping | protocols/imrs.md:130 | `test_imrs::test_imrs_ping_answer`, `test_imrs_conf::test_ping_and_info` | rig/unit | a | OK |
| P-IMR-04 Pings from addresses that are not peers are not answered (no reflection amplifier) | protocols/imrs.md:131 | `test_imrs::test_imrs_ping_from_stranger` | rig | a | OK |
| P-IMR-05 Info packet carries the server callsign and a DG-ID bitmap of exactly the served DG-IDs | protocols/imrs.md:132 | `test_imrs::test_imrs_info_content`, `test_imrs_conf::test_ping_and_info` | rig/unit | a | OK |
| P-IMR-06 A 60 B info packet with a 7 or 8 char callsign registers the repeater on the default room | protocols/imrs.md:133 | `test_imrs::test_imrs_register` | rig | a | OK |
| P-IMR-07 A silent peer is dropped after the timeout; activity of one peer does not keep another alive | protocols/imrs.md:134 | `test_imrs::test_imrs_peer_timeout` | rig+slow | a | OK |
| P-IMR-08 The header DG-ID selects the room and the peer follows; an unmapped DG-ID is dropped without unlinking (PARTIAL: mapped part only sut == ref, unmapped asserts link kept not the drop) | protocols/imrs.md:140 | `test_imrs::test_imrs_dgid_room` | rig | a | PARTIAL |
| P-IMR-09 Egress carries the room DG-ID in FICH SQ and SQL 0 | protocols/imrs.md:141 | `test_imrs::test_imrs_egress_dgid` | rig | a | OK |
| P-IMR-24 An empty call from another mode (header + terminator, no voice) is not sent to IMRS peers | protocols/imrs.md:142 | `test_imrs::test_imrs_empty_stream_not_relayed` | rig | a | OK |
| P-IMR-10 Egress goes only to peers on the stream room | protocols/imrs.md:143 | `test_imrs::test_imrs_egress_room_only` | rig | a | OK |
| P-IMR-11 Call starts with one 91 B header: FI 0, DT 2, src, dest tail equal rem4 and a neutral constant | protocols/imrs.md:149 | `test_imrs::test_imrs_egress_header`, `test_imrs_conf::test_header_layout` | rig/unit | a | OK |
| P-IMR-12 Voice: FI 1, DT 2, five V/D type 2 channels per packet, AMBE+2 frames unchanged both ways | protocols/imrs.md:150 | `test_imrs::test_imrs_voice_passthrough`, `test_imrs_conf::test_voice_round_trip` | rig/unit | a | OK |
| P-IMR-13 A call ends with one 31 B terminator, FI 2 | protocols/imrs.md:151 | `test_imrs::test_imrs_egress_terminator`, `test_imrs_conf::test_terminator_and_stream` | rig/unit | a | OK |
| P-IMR-14 No voice lost at the end of a call: a last group of fewer than five frames is padded and sent | protocols/imrs.md:152 | `test_imrs::test_imrs_partial_group` | rig | a | OK |
| P-IMR-15 An ingress terminator adds no audio (no injected zero frame) | protocols/imrs.md:153 | `test_imrs::test_imrs_terminator_no_audio` | rig | a | OK |
| P-IMR-16 A missing voice channel is not replaced by stale audio (needs a lossy path, not built) | protocols/imrs.md:154 | - | - | - | GAP |
| P-IMR-17 Each over gets its own stream id; peers in one /16 do not collide (PARTIAL: two overs of one peer only, no /16 collision) | protocols/imrs.md:160 | `test_imrs::test_imrs_stream_id` | rig | a | PARTIAL |
| P-IMR-18 The talking repeater gets nothing of its own call back, tail included | protocols/imrs.md:161 | `test_imrs::test_imrs_no_echo` | rig | a | OK |
| P-IMR-19 A header from an address that never sent info is dropped (PARTIAL: sut == ref only, via a timed-out peer, slow) | protocols/imrs.md:162 | `test_imrs::test_imrs_peer_timeout` | rig+slow | a | PARTIAL |
| P-IMR-20 Header callsigns in lower case or with a NUL are accepted or rejected consistently (PARTIAL: lower case only, no NUL) | protocols/imrs.md:163 | `test_imrs::test_imrs_callsign_case` | rig | a | PARTIAL |
| P-IMR-21 A call whose terminator is lost ends after the silence timeout and every egress gets a terminator | protocols/imrs.md:164 | `test_imrs::test_imrs_lost_terminator` | rig | a | OK |
| P-IMR-22 Two repeaters on one room at once: one winner, no mixing (PARTIAL: asserts no mixing, not that one wins) | protocols/imrs.md:165 | `test_imrs::test_imrs_two_talkers` | rig | a | PARTIAL |
| P-IMR-23 IMRS to DMR/NXDN is passthrough; IMRS to D-STAR/M17 transcoded, sut == ref | protocols/imrs.md:166 | `test_imrs::test_imrs_cross_mode`, `test_imrs::test_imrs_voice_passthrough` | rig | a | OK |

## XLX interlink
Sources: protocols/xlx.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-XLX-01 L from a listed peer with shared modules is answered with A (39 B) carrying callsign, version, shared modules | protocols/xlx.md:86 | `test_xlx::test_xlx_connect_ack`, `test_xlx_conf::test_link_packets_round_trip` | interlink/unit | a | OK |
| P-XLX-02 The module list of the A is the intersection of the L list and the configured list | protocols/xlx.md:87 | `test_xlx::test_xlx_ack_module_intersection` | interlink | a | OK |
| P-XLX-03 L from an unlisted callsign gets N (10 B), no link | protocols/xlx.md:88 | `test_xlx::test_xlx_unlisted_nack` | interlink | a | OK |
| P-XLX-04 L whose modules do not overlap the configured ones gets N | protocols/xlx.md:89 | `test_xlx::test_xlx_no_common_module_nack` | interlink | a | OK |
| P-XLX-05 L with a listed callsign from an address that is not the listed one is refused | protocols/xlx.md:90 | `test_xlx::test_xlx_wrong_address` | interlink | a | OK |
| P-XLX-06 A link carries traffic only after both directions completed (L then A each way) | protocols/xlx.md:91 | `test_xlx::test_xlx_half_link_no_traffic` | interlink | a | OK |
| P-XLX-07 The reflector sends L to every unlinked listed peer every 5 s, also after N (PARTIAL: asserts at least 2 L in 11.5 s and the module sets, not the 5 s period, not after N) | protocols/xlx.md:92 | `test_xlx::test_xlx_initiates_connect` | interlink | a | PARTIAL |
| P-XLX-08 Protocol revision from the version in the peer A: 2.6.0 and up 53 B, 2.2.0 and up 45 B, else 27 B | protocols/xlx.md:93 | `test_xlx::test_xlx_revision_by_version`, `test_xlx_conf::test_revision_of` | interlink/unit | a | OK |
| P-XLX-09 A BM-prefixed peer is a DMR peer by configured kind: 45 B frames, D-Star slot filled | protocols/xlx.md:94 | `test_xlx::test_xlx_bm_peer_class` | interlink | a | OK |
| P-XLX-10 A repeated L or A while linked keeps one link, traffic sent once | protocols/xlx.md:95 | `test_xlx::test_xlx_relink_single` | interlink | a | OK |
| P-XLX-11 Keepalive: 9 B with the own callsign to every linked peer about every second | protocols/xlx.md:101 | `test_xlx::test_xlx_keepalive_form` | interlink | a | OK |
| P-XLX-12 A peer silent for 30 s is sent U and removed, then L retries resume | protocols/xlx.md:102 | `test_xlx::test_xlx_silent_peer_dropped` | interlink+slow | a | OK |
| P-XLX-13 U from the peer removes the link at once, L retries resume | protocols/xlx.md:103 | `test_xlx::test_xlx_disconnect` | interlink | a | OK |
| P-XLX-14 Keepalive or U carrying another callsign than the linked peer from its address | protocols/xlx.md:104 | `test_xlx::test_xlx_foreign_callsign_unlink` | interlink | a | OK |
| P-XLX-15 Egress header: 56 B, RPT2 = own reflector callsign + module, stream id of the source | protocols/xlx.md:119 | `test_xlx::test_xlx_egress_header` | interlink | a | OK |
| P-XLX-16 Egress voice has the negotiated revision size (27/45/53 B), counter mod 21, last frame flagged | protocols/xlx.md:120 | `test_xlx::test_xlx_egress_voice_layout`, `test_xlx_conf::test_voice_round_trip` | interlink/unit | a | OK |
| P-XLX-17 With the transcoder running the AMBE+2 and Codec2 slots carry transcoded audio of a D-Star source | protocols/xlx.md:121 | `test_xlx::test_xlx_egress_slots_transcoded` | crossmode | a | OK |
| P-XLX-18 Transcoded output missing inside a stream: frame still sent at revision size with zero slots | protocols/xlx.md:122 | `test_xlx::test_xlx_egress_slot_missing_mid_stream`, `test_xlxd_interlink::test_interlink_missing_output_slot_zeroed` | crossmode/interlink | c | OK |
| P-XLX-33 No transcoder for the whole stream: full revision size frames with zero slots | protocols/xlx.md:123 | `test_xlx::test_xlx_egress_transcoder_down`, `test_xlxd_interlink::test_interlink_missing_output_slot_zeroed` | interlink | c | OK |
| P-XLX-19 Last frame: D-Star slot is the DExtra end pattern, flagged | protocols/xlx.md:124 | `test_xlx::test_xlx_egress_last_frame` | interlink | a | OK |
| P-XLX-20 Ingress revision 3 frame: slots used as given (AMBE+2 to DMR, Codec2 to M17), no local transcoding | protocols/xlx.md:125 | `test_xlx::test_xlx_ingress_rev3_slots` | interlink | a | OK |
| P-XLX-21 Ingress revision 1 frame (27 B): the reflector transcodes the D-Star slot for its other modes | protocols/xlx.md:126 | `test_xlx::test_xlx_ingress_rev1_transcoded` | crossmode | a | OK |
| P-XLX-22 Ingress frame whose size does not match the negotiated revision: slots judged by received size | protocols/xlx.md:127 | `test_xlx::test_xlx_ingress_size_mismatch` | crossmode | a | OK |
| P-XLX-23 Malformed packets (bad size, unknown tag, bad keepalive callsign) are ignored, link stays | protocols/xlx.md:128 | `test_xlx::test_xlx_garbage_ignored`, `test_xlx_conf::test_parsers_reject_other_forms` | interlink/unit | a | OK |
| P-XLX-24 The stream id of an ingress stream is kept on every egress | protocols/xlx.md:134 | `test_xlx::test_xlx_stream_id_kept`, `test_xlx::test_xlx_egress_header` | interlink | a | OK |
| P-XLX-25 A header with stream id 0 opens no stream (PARTIAL: sut == ref only, refusal itself not asserted) | protocols/xlx.md:135 | `test_xlx::test_xlx_sid_zero` | interlink | a | PARTIAL |
| P-XLX-26 A peer stream whose id is already open on any module is refused | protocols/xlx.md:136 | `test_xlx::test_xlx_sid_open_elsewhere`, `test_xlxd_interlink::test_admission_remote_sid_open_elsewhere` | interlink | c | OK |
| P-XLX-27 Module mapping by RPT2 letter; an unshared module opens nothing and sends nothing to the peer | protocols/xlx.md:137 | `test_xlx::test_xlx_module_mapping` | interlink | a | OK |
| P-XLX-28 No XLX transit: a stream from an XLX peer is never sent to an XLX peer (back or onward) | protocols/xlx.md:138 | `test_xlx::test_xlx_no_transit`, `test_xlxd_interlink::test_interlink_no_xlx_transit`, `test_xlxd_interlink::test_interlink_no_loop`, `test_check_streams::test_xlx_no_transit` | interlink/unit | c | OK |
| P-XLX-29 Cross-family transit: XLX ingress reaches the reflector clients and its non-XLX peers | protocols/xlx.md:139 | `test_xlx::test_xlx_cross_family`, `test_xlxd_interlink::test_interlink_cross_family_transit`, `test_xlxd_interlink::test_interlink_dstar_exact` | interlink | c | OK |
| P-XLX-30 A peer stream without a last frame ends after 1.6 s of silence, the module is free after | protocols/xlx.md:140 | `test_xlx::test_xlx_stream_timeout` | interlink | a | OK |
| P-XLX-31 A repeated header of an open stream does not open a second stream | protocols/xlx.md:141 | `test_xlx::test_xlx_repeated_header` | interlink | a | OK |
| P-XLX-32 Voice frames whose stream id had no header are not relayed | protocols/xlx.md:142 | `test_xlx::test_xlx_headerless_voice` | interlink | a | OK |
| P-XLX-34 A cut XLX stream sends nothing to another XLX peer, the closing last frame included | protocols/xlx.md:143 | `test_xlx::test_xlx_cut_stream_no_closing_frame` | interlink | c | OK |

## AMBEd
Sources: protocols/ambed.md. The doc has no obligation ids; rows follow its sections. Transcoder-side rows are run by our AmbedClient against the rig transcoder; reflector-side rows (the SUT acting as AMBEd client) run against FakeAmbed or the fake worker.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Byte order: tags ASCII, stream id and data port little endian, CAPS mask and transaction big endian | protocols/ambed.md:40 | `test_ambed_wire::test_ext_open_std18_and_retransmit`, `test_ambed_wire::test_close_stops_replies`, `test_ambed_ext::test_offsets_and_endianness` | wire/unit | a | OK |
| Codec ids and 20 ms frame widths (9/9/8/11/320 B), PCM only through the extended open | protocols/ambed.md:29 | `test_ambed_wire::test_codec2_multi_output_round_trip`, `test_ambed_wire::test_pcm_to_codec2`, `test_ambed_ext::test_widths_and_request_sizes` | wire/unit | a | OK |
| PING 17 B (AMBEDPING + callsign) is answered with PONG 9 B | protocols/ambed.md:51 | `test_ambed_wire::test_ping_pong` | wire | a | OK |
| CAPS request 9 B is answered with a 15 B CAPS reply: version 1, max outputs 1..4, codec mask | protocols/ambed.md:70 | `test_ambed_wire::test_caps_reply` | wire | a | OK |
| Reflector accepts a CAPS reply only when version is 1, max outputs 1..4 and mask AND 0x3E is non-zero (PARTIAL: library rule only, reflector not fed an invalid reply) | protocols/ambed.md:79 | `test_ambed_ext::test_caps_validation_rule`, `test_fake_ambed::test_custom_caps_and_invalid_caps_refused` | unit | a | PARTIAL |
| Legacy OPEN 17/18/19 B gets STD 14 B or BUSY 9 B, never an extended reply, same kind as the reference | protocols/ambed.md:82 | `test_ambed_wire::test_legacy_open_gets_legacy_reply`, `test_ambed_wire::test_legacy_open_matches_reference`, `test_ambed::test_legacy_open_round_trip` | wire/unit | a | OK |
| xlxd always sends the 19 B legacy open with three fixed outputs per input codec (PARTIAL: asserted under the xlxd profile; DV-Router profile spec asserts extended opens only) | protocols/ambed.md:93 | `test_xlxd_usrp_netns::test_legacy_ambed_digital_uses_legacy_open`, `test_xlxd_usrp_netns::test_no_usrp_legacy_open` | netns | a | PARTIAL |
| Extended OPEN 24 B: codec in 1..5, up to 4 outputs, transaction never 0, answered with STD 18 B echoing it | protocols/ambed.md:97 | `test_ambed_wire::test_ext_open_std18_and_retransmit`, `test_xlxd_usrp_netns::test_extended_opens`, `test_ambed::test_extended_open_round_trip` | wire/netns/unit | a | OK |
| Extended open only when PCM is input or a needed output, every other stream keeps the 19 B open (PARTIAL: xlxd profile only; DV-Router profile spec opens extended for every stream) | protocols/ambed.md:107 | `test_xlxd_usrp_netns::test_extended_opens`, `test_xlxd_usrp_netns::test_no_usrp_legacy_open` | netns | a | PARTIAL |
| Outputs listed ascending without the input codec, limited to CAPS mask and max outputs; transaction random start +1 per open (PARTIAL: order, limit and distinct tx asserted, random start and +1 not) | protocols/ambed.md:110 | `test_xlxd_usrp_netns::test_extended_opens`, `test_xlxd_usrp_netns::test_one_output_opens` | netns | a | PARTIAL |
| Retransmitted open (same peer, transaction, codecs) returns the same stream; same transaction with other codecs is a new open | protocols/ambed.md:112 | `test_ambed_wire::test_ext_open_std18_and_retransmit`, `test_ambed_wire::test_ext_open_same_tx_other_codecs` | wire | a | OK |
| An extended open always gets a reply; invalid ones (unknown or duplicate codec, out equals in, no output, bad callsign) get BUSY 13 | protocols/ambed.md:115 | `test_ambed_wire::test_ext_open_invalid_busy`, `test_ambed_wire::test_ext_open_unserved_codec_busy`, `test_ambed_wire::test_imbe_follows_caps` | wire | a | OK |
| STD layout 14/18 B: stream id LE, data port LE, codec in, codec out 1, transaction BE (18 B form) | protocols/ambed.md:119 | `test_ambed_wire::test_ext_open_std18_and_retransmit`, `test_ambed_wire::test_legacy_open_gets_legacy_reply`, `test_ambed::test_std_busy_close` | wire/unit | a | OK |
| The reference ambed uses data port 10100 + stream id (tests treat the port as opaque; checked only by offline capture reading) | protocols/ambed.md:130 | - | - | - | GAP |
| BUSY 9 B (legacy, also when the transcoder is full) and BUSY 13 B (extended, echoes the transaction) | protocols/ambed.md:132 | `test_ambed_wire::test_legacy_open_full_gets_busy9`, `test_ambed_wire::test_ext_open_invalid_busy`, `test_ambed_wire::test_stream_cap` | wire | a | OK |
| CS close stream 9 B ends the stream, no reply, 9 B or longer accepted (PARTIAL: close effect asserted, no-reply and longer CS not) | protocols/ambed.md:139 | `test_ambed_wire::test_close_stops_replies`, `test_fake_ambed::test_cs_for_unknown_stream_is_ignored_without_reply` | wire/unit | a | PARTIAL |
| Stream port: client binds it, the transcoder replies to the client address at the same port | protocols/ambed.md:150 | `test_ambed_wire::test_codec2_to_pcm_nonzero`, `test_ambed_wire::test_concurrent_streams_do_not_mix` | wire | a | OK |
| Data request: codec in, pid wrapping at 256, one frame (PARTIAL: pid echo asserted for 50 frames, the wrap at 256 only on the fake) | protocols/ambed.md:154 | `test_ambed_wire::test_pcm_to_codec2`, `test_fake_ambed::test_pid_wraps_modulo_256_on_the_wire`, `test_ambed_ext::test_index_xor_wraps_at_256_for_pcm` | wire/unit | a | PARTIAL |
| Data reply: one entry per output in open order, pid after the first codec byte | protocols/ambed.md:162 | `test_ambed_wire::test_pcm_two_outputs_keep_order`, `test_ambed_wire::test_codec2_multi_output_round_trip` | wire | a | OK |
| Reply sizes for the 19 B legacy opens: 32 B for AMBE+ and AMBE2+, 33 B for Codec2, 30 B for IMBE input (PARTIAL: extended sizes asserted on the wire, legacy layouts only unit golden vectors, IMBE input not observed) | protocols/ambed.md:176 | `test_ambed_ext::test_expected_reply_layouts`, `test_ambed_ext::test_golden_vectors`, `test_ambed_wire::test_codec2_multi_output_round_trip` | unit/wire | a | PARTIAL |
| A legacy transcoder may return only the first n outputs; the missing ones are zero filled by xlxd (PARTIAL: reflector accepts the cut reply, DV-Router sends no frame for the missing output instead of zero fill) | protocols/ambed.md:186 | `test_crossmode_failure::test_data_reply_cut_after_complete_entry` | crossmode | a | PARTIAL |
| Keepalive: reflector PINGs every 5 s, CAPS probed after the first PONG, once per connection (PARTIAL: CAPS once per connection asserted, the 5 s PING period only in the legacy swap case) | protocols/ambed.md:195 | `test_xlxd_usrp_netns::test_extended_single_caps_probe`, `test_xlxd_transcoder_lifecycle::test_lost_transcoder_caps_probed_again`, `test_xlxd_transcoder_lifecycle::test_legacy_swap_recovers` | netns | a | PARTIAL |
| Probe only when the reflector has a PCM/USRP endpoint configured (PARTIAL: xlxd profile asserts no CAPS without USRP; DV-Router profile spec probes always) | protocols/ambed.md:195 | `test_xlxd_usrp_netns::test_no_usrp_sends_no_caps` | netns | a | PARTIAL |
| No CAPS reply within 1 s means legacy: extended opens off for the connection, legacy opens used (PARTIAL: legacy fallback asserted, the 1 s wait not) | protocols/ambed.md:203 | `test_xlxd_usrp_netns::test_legacy_ambed_single_caps_probe`, `test_xlxd_usrp_netns::test_legacy_ambed_digital_uses_legacy_open` | netns | a | PARTIAL |
| Fallback to a transcoder without the extension: PCM input is not transcoded (logged once) (PARTIAL: no PCM open and no USRP output asserted, log line not) | protocols/ambed.md:235 | `test_xlxd_usrp_netns::test_legacy_ambed_pcm_not_transcoded` | netns | a | PARTIAL |
| PING period 5 s | protocols/ambed.md:245 | `test_xlxd_transcoder_lifecycle::test_legacy_swap_recovers` | netns | a | PARTIAL |
| Keepalive timeout 30 s without PONG (PARTIAL: transcoder lost for 40 s then recovered, the 30 s value itself not asserted) | protocols/ambed.md:246 | `test_xlxd_transcoder_lifecycle::test_rig_transcoder_lost_and_back_usrp_resumes`, `test_xlxd_transcoder_lifecycle::test_lost_transcoder_pcm_resumes` | crossmode/netns | a | PARTIAL |
| OPEN reply wait 1000 ms + 50 ms grace; a late STD is closed with CS | protocols/ambed.md:247 | `test_crossmode_failure::test_open_silent`, `test_crossmode_failure::test_open_late_beyond_window`, `test_crossmode_failure::test_open_late_within_window`, `test_xlxd_usrp_netns::test_open_refused_late_std_given_up` | crossmode/netns | a | OK |
| CAPS reply wait 1000 ms (PARTIAL: only through the CAPS re-probe after a failed open and the legacy probe repeats) | protocols/ambed.md:248 | `test_crossmode_failure::test_open_silent`, `test_xlxd_usrp_netns::test_legacy_ambed_single_caps_probe` | crossmode/netns | a | PARTIAL |
| Frame period 20 ms (the reflector sends one data request per frame; only the request count is checked, not the period) | protocols/ambed.md:249 | - | - | - | GAP |
| Data reply timeout 400 ms: session ends with CS, CAPS re-probed, a 100 ms late reply is fine | protocols/ambed.md:250 | `test_crossmode_failure::test_data_reply_never_accepted`, `test_crossmode_failure::test_data_delay_long`, `test_crossmode_failure::test_data_delay_short` | crossmode | a | OK |
| Extended open carries up to max outputs (4) in open order, four-output replies parsed | protocols/ambed.md:257 | `test_ambed_wire::test_codec2_multi_output_round_trip`, `test_fake_ambed::test_four_outputs_353_bytes` | wire/unit | a | OK |
| A legacy retransmit opens a second stream (extended returns the same stream) | protocols/ambed.md:262 | - | - | - | GAP |
| Transcoder stream limit: the open past the limit gets BUSY, closes free the slots | protocols/ambed.md:332 | `test_ambed_wire::test_legacy_open_full_gets_busy9`, `test_ambed_wire::test_stream_cap` | wire | a | OK |

## USRP
Sources: protocols/usrp.md. Rows P-USR-01..26 drive the static endpoint of module C (and the real app_rpt on modules A and B); rows 27..41 are the dynamic-client hub (DV-Router profile spec has dynamic_server True).

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-USR-01 Voice is 352 B: USRP, type 0, keyup 1, 320 B of PCM | protocols/usrp.md:121 | `test_usrp::test_usrp_egress_voice_frame`, `test_usrp_conf::test_voice_and_unkey_layout` | allstar/unit | a | OK |
| P-USR-02 memory, talkgroup, type, mpxid and reserved are zero in voice frames | protocols/usrp.md:122 | `test_usrp::test_usrp_egress_fields_zero` | allstar | a | OK |
| P-USR-03 PCM byte order is little endian | protocols/usrp.md:123 | `test_usrp::test_usrp_pcm_byte_order` | allstar | a | OK |
| P-USR-04 seq is big endian and counts +1 per packet sent, unkey included | protocols/usrp.md:124 | `test_usrp::test_usrp_egress_seq` | allstar | a | OK |
| P-USR-05 Voice is paced in real time (20 ms), never more than 25 frames ahead of play-out | protocols/usrp.md:125 | `test_usrp::test_usrp_egress_pacing` | allstar | a | OK |
| P-USR-06 A transmission ends with exactly one 32 B header-only frame: keyup 0, type 0 | protocols/usrp.md:126 | `test_usrp::test_usrp_single_unkey` | allstar | a | OK |
| P-USR-07 The unkey is also sent when the source stream ends without a terminator (PARTIAL: D-STAR lost last frame only, not source timeout of other modes, disconnect or backend loss) | protocols/usrp.md:127 | `test_usrp::test_usrp_unkey_after_lost_source_end`, `test_floor::test_usrp_unkey_after_silence` | allstar/rig | a | PARTIAL |
| P-USR-08 Nothing is sent to an idle peer, never a 352 B frame that is not voice | protocols/usrp.md:128 | `test_usrp::test_usrp_idle_silent` | allstar | a | OK |
| P-USR-09 Talker metadata (TEXT with TLV SET_INFO) before the voice (DV-Router: off by default) | protocols/usrp.md:129 | `test_usrp::test_usrp_egress_set_info`, `test_usrp_conf::test_set_info_round_trip` | allstar/unit | a | OK |
| P-USR-10 The first keyed 352 B voice frame opens a stream on the endpoint module, transcoded to digital clients | protocols/usrp.md:135 | `test_usrp::test_usrp_ingress_opens_stream` | allstar | a | OK |
| P-USR-11 A 32 B unkey ends the stream promptly, listeners get the end of stream | protocols/usrp.md:136 | `test_usrp::test_usrp_ingress_unkey_ends_stream` | allstar | a | OK |
| P-USR-12 A 352 B voice frame with keyup 0 (pyUC unkey) is handled as the profile spec says | protocols/usrp.md:137 | `test_usrp::test_usrp_ingress_keyup0_voice` | allstar | a | OK |
| P-USR-13 Voice that stops without an unkey ends the stream after a silence timeout (doc claims test_floor, which tests the egress direction; ingress silence not asserted) | protocols/usrp.md:138 | - | - | - | GAP |
| P-USR-14 seq gaps, reordering and a non-zero first seq never drop voice or split the stream | protocols/usrp.md:139 | `test_usrp::test_usrp_ingress_seq_ignored` | allstar | a | OK |
| P-USR-15 Packets from any address other than the configured peer ip:port are ignored (PARTIAL: other port of the same IP only, no other IP) | protocols/usrp.md:140 | `test_usrp::test_usrp_ingress_foreign_source` | allstar | a | PARTIAL |
| P-USR-16 TEXT, DTMF, PING, TLV, ADPCM/ULAW, wrong-size and non-USRP packets open no stream, endpoint keeps working | protocols/usrp.md:141 | `test_usrp::test_usrp_ingress_non_voice_ignored` | allstar | a | OK |
| P-USR-17 A PING (type 3) gets no reply | protocols/usrp.md:142 | `test_usrp::test_usrp_ping_no_reply` | allstar | a | OK |
| P-USR-18 Ingress TEXT SET_INFO names the talker of the next over | protocols/usrp.md:143 | `test_usrp::test_usrp_ingress_set_info_callsign` | allstar | a | OK |
| P-USR-19 A stream from a USRP endpoint is never sent back to that endpoint | protocols/usrp.md:149 | `test_allstar::test_usrp_no_echo` | allstar | a | OK |
| P-USR-20 A digital stream on the module reaches the endpoint as keyed voice, whatever its source protocol | protocols/usrp.md:150 | `test_usrp::test_usrp_egress_from_digital` | allstar | a | OK |
| P-USR-21 With several endpoints each gets only its own module streams, and only once | protocols/usrp.md:151 | `test_usrp::test_usrp_endpoints_isolated` | allstar | c | OK |
| P-USR-22 The endpoint is permanent: no login, no keepalive, no idle drop; works after idle and a seq restart | protocols/usrp.md:152 | `test_usrp::test_usrp_endpoint_permanent` | allstar | a | OK |
| P-USR-23 A USRP over on a module busy with another talker is not mixed in | protocols/usrp.md:153 | `test_floor::test_usrp_over_suppressed_when_busy` | rig | a | OK |
| P-USR-24 No fabricated audio: with the transcoder lost no PCM (silence or zeros) is sent to the peer (PARTIAL: USRP to digital direction and legacy transcoder asserted, D-STAR to USRP during a loss not) | protocols/usrp.md:154 | `test_backend_loss::test_usrp_pcm_not_fabricated`, `test_xlxd_usrp_netns::test_legacy_ambed_pcm_not_transcoded` | rig/netns | a | PARTIAL |
| P-USR-25 An over from a digital client reaches app_rpt: chan_usrp read counter rises by the frames sent | protocols/usrp.md:160 | `test_usrp::test_usrp_real_asl_receives` | allstar | b:app_rpt | OK |
| P-USR-26 An over from app_rpt opens a stream on module A and ends with the single unkey | protocols/usrp.md:161 | `test_usrp::test_usrp_real_asl_sends` | allstar | b:app_rpt | OK |
| P-USR-27 A sender that registers with TEXT REG:DVSWITCH is admitted and gets REG:OK back | protocols/usrp.md:191 | `test_usrp_dynamic::test_usrp_dyn_register` | rig | a | OK |
| P-USR-28 Everything for a dynamic client goes to the source ip:port of its datagrams | protocols/usrp.md:192 | `test_usrp_dynamic::test_usrp_dyn_reply_to_source` | rig | a | OK |
| P-USR-29 Voice from an unknown address that never registered is ignored | protocols/usrp.md:193 | `test_usrp_dynamic::test_usrp_dyn_unregistered_voice` | rig | a | OK |
| P-USR-30 REG:UNREG from a client ends its session, no further traffic to it | protocols/usrp.md:194 | `test_usrp_dynamic::test_usrp_dyn_unregister` | rig | a | OK |
| P-USR-31 A silent registered client is dropped after the timeout and can register again (PARTIAL: 3600 s timeout skips unless DVT_LONG_TIMEOUTS=1) | protocols/usrp.md:195 | `test_usrp_dynamic::test_usrp_dyn_idle_timeout` | rig+slow | a | PARTIAL |
| P-USR-32 A PING from a registered client keeps it registered and gets no reply (PARTIAL: waits 1.3 x the 3600 s timeout, skips unless DVT_LONG_TIMEOUTS=1) | protocols/usrp.md:196 | `test_usrp_dynamic::test_usrp_dyn_ping_keeps` | rig+slow | a | PARTIAL |
| P-USR-33 The client picks its module by DTMF dial string (letter or 4001..4026) | protocols/usrp.md:197 | `test_usrp_dynamic::test_usrp_dyn_select_module` | rig | a | OK |
| P-USR-34 Module of a new client before it selects one: none | protocols/usrp.md:198 | `test_usrp_dynamic::test_usrp_dyn_default_module` | rig | a | OK |
| P-USR-35 The client SET_INFO sent after REG:OK names it as talker | protocols/usrp.md:199 | `test_usrp_dynamic::test_usrp_dyn_set_info_in` | rig | a | OK |
| P-USR-36 Each call to a dynamic client starts with SET_INFO (talker callsign and ids) | protocols/usrp.md:200 | `test_usrp_dynamic::test_usrp_dyn_set_info_out` | rig | a | OK |
| P-USR-37 INFO: request is answered with a small JSON | protocols/usrp.md:201 | `test_usrp_dynamic::test_usrp_dyn_info` | rig | a | OK |
| P-USR-38 Several dynamic clients: each gets its module streams once, never its own, one talker per module | protocols/usrp.md:202 | `test_usrp_dynamic::test_usrp_dyn_two_clients` | rig | a | OK |
| P-USR-39 Audio to a dynamic client is SLIN (type 0, 320 B) | protocols/usrp.md:203 | `test_usrp_dynamic::test_usrp_dyn_slin` | rig | a | OK |
| P-USR-40 Admission limit: bounded client table (64), REG:UNREG when full | protocols/usrp.md:204 | `test_usrp_dynamic::test_usrp_dyn_table_full` | rig | a | OK |
| P-USR-41 A client without a valid SET_INFO callsign (none, invalid, denied) may listen but never transmit (PARTIAL: denied form skips without DVT_XLXD_BLACKLIST=N0DENY) | protocols/usrp.md:205 | `test_usrp_dynamic::test_usrp_dyn_listen_only_without_callsign` | rig | a | PARTIAL |

## Transcoder levels
Sources: transcoder-levels.md. The cases skip unless DVT_TRANSCODER_LEVELS=1 and the real transcoder is on the rig (one run per image tag). The cases assert delivery (frames at least 90 percent of sent) and content (tone frequency, speech envelope correlation at least 0.5); levels are recorded data. The VOC1 decision rules run offline in dvtest.tools.voc1_rule on the recorded files.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Pair M17 to DMR, D-STAR, YSF: every listener hears every over, content ok (tone and 9 speech signals) | transcoder-levels.md:18 | `test_transcoder_levels::test_levels_by_source` | rig | a | OK |
| Pair DMR to D-STAR, M17, YSF: every listener hears every over, content ok | transcoder-levels.md:18 | `test_transcoder_levels::test_levels_by_source` | rig | a | OK |
| Pair D-STAR to DMR, M17, YSF: every listener hears every over, content ok | transcoder-levels.md:18 | `test_transcoder_levels::test_levels_by_source` | rig | a | OK |
| Levels per pair and signal (active dBFS delta to the source) are measured for each tag (PARTIAL: recorded as data, not asserted by the case) | transcoder-levels.md:28 | `test_transcoder_levels::test_levels_by_source` | rig | a | PARTIAL |
| YSF egress changes the AMBE+2 gain index b2 by -0.4 dB (included in the rows into YSF) (PARTIAL: only recorded inside the YSF rows) | transcoder-levels.md:36 | `test_transcoder_levels::test_levels_by_source` | rig | a | PARTIAL |
| Load: 16 simultaneous cross-mode streams complete, each receiving at least 90 percent of its frames | transcoder-levels.md:32 | `test_transcoder_levels::test_load_16_streams` | rig | a | OK |
| VOC1 rule 1 delivery: frames received >= sent and content ok on every row (PARTIAL: case asserts 90 percent, the strict rule is in the offline tool) | transcoder-levels.md:93 | `test_transcoder_levels::test_levels_by_source`, `test_voc1_rule::test_rule1_lost_frames_and_content_failure` | rig/unit | a | PARTIAL |
| VOC1 rule 2 speech: mean level delta per pair within 0.5 dB of production or toward the source (PARTIAL: offline tool only, unit-tested) | transcoder-levels.md:94 | `test_voc1_rule::test_rule2_within_half_a_dB_passes_and_over_half_a_dB_away_fails`, `test_voc1_rule::test_rule2_moving_toward_the_source_passes` | unit | a | PARTIAL |
| VOC1 rule 3 tone: Codec2 to DMR and D-STAR rms rises to at least 900, other pairs identical (PARTIAL: offline tool only, unit-tested) | transcoder-levels.md:95 | `test_voc1_rule::test_rule3_tone_must_reach_900_and_rise`, `test_voc1_rule::test_rule3_a_changed_ambe_pair_fails_even_by_a_tenth_of_a_dB` | unit | a | PARTIAL |
| VOC1 rule 4 load: 16 of 16 streams complete and mean transcoder CPU at most 2 x production (PARTIAL: completion asserted by the case, CPU judged offline) | transcoder-levels.md:96 | `test_transcoder_levels::test_load_16_streams`, `test_voc1_rule::test_rule4_load_streams_and_cpu` | rig/unit | a | PARTIAL |
| Conclusion 1: the unity tag has exactly the levels of 823ce72a (carrier check with identical-ok) (PARTIAL: offline tool only, unit-tested) | transcoder-levels.md:120 | `test_voc1_rule::test_identical_ok_passes_a_carrier_that_changes_nothing` | unit | a | PARTIAL |
| Conclusion 4: no decode or delivery problem on any tag (PARTIAL: case asserts 90 percent of frames and envelope correlation at least 0.5, not zero loss or 0.94) | transcoder-levels.md:133 | `test_transcoder_levels::test_levels_by_source` | rig | a | PARTIAL |

## MMDVM / Homebrew DMR
Sources: protocols/mmdvm.md (testlist K1).

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-DMR-01 RPTL from a known id answered with RPTACK + 4-byte salt | protocols/mmdvm.md:224 | `test_mmdvm::test_mmdvm_login_ack_salt` | rig | a | OK |
| P-DMR-02 The salt is random, not a constant (differs after a master restart) (PARTIAL: sut==ref only) | protocols/mmdvm.md:225 | `test_mmdvm::test_mmdvm_salt_changes_on_restart` | rig | a | PARTIAL |
| P-DMR-03 RPTL from an id the master does not accept never logs in (MSTNAK) | protocols/mmdvm.md:226 | `test_mmdvm::test_mmdvm_login_unknown_id` | rig | a | OK |
| P-DMR-04 RPTK + SHA-256 hash completes login, wrong hash gets MSTNAK (PARTIAL: only zero hash asserted, wrong hash recorded) | protocols/mmdvm.md:227 | `test_mmdvm::test_mmdvm_auth_zero_hash` | rig | a | PARTIAL |
| P-DMR-05 RPTK without a preceding RPTL gets MSTNAK and no session | protocols/mmdvm.md:228 | `test_mmdvm::test_mmdvm_auth_without_login` | rig | a | OK |
| P-DMR-06 RPTC (302 B) is acknowledged; later acks carry the 4-byte id | protocols/mmdvm.md:229 | `test_mmdvm::test_mmdvm_config_ack` | rig | a | OK |
| P-DMR-07 RPTC of another size: 301 B not acked, 303 B acked, login kept | protocols/mmdvm.md:230 | `test_mmdvm::test_mmdvm_config_wrong_size` | rig | a | OK |
| P-DMR-08 Voice is routed for a client that never sent RPTC | protocols/mmdvm.md:231 | `test_mmdvm::test_mmdvm_no_config_still_routed` | rig | a | OK |
| P-DMR-09 RPTO + id + options answered with RPTACK (PARTIAL: sut==ref only) | protocols/mmdvm.md:232 | `test_mmdvm::test_mmdvm_options_ack` | rig | a | PARTIAL |
| P-DMR-10 Re-login with same id and address keeps one session, traffic once (PARTIAL: sut==ref only) | protocols/mmdvm.md:233 | `test_mmdvm::test_mmdvm_relogin_single_session` | rig | a | PARTIAL |
| P-DMR-11 RPTPING from a logged-in client answered with MSTPONG + id, big endian | protocols/mmdvm.md:239 | `test_mmdvm::test_mmdvm_ping_pong` | rig | a | OK |
| P-DMR-12 RPTPING from an address not logged in gets no MSTPONG (MSTNAK) | protocols/mmdvm.md:240 | `test_mmdvm::test_mmdvm_ping_not_logged_in` | rig | a | OK |
| P-DMR-13 A client silent past the keepalive timeout is dropped and sent MSTCL | protocols/mmdvm.md:241 | `test_mmdvm::test_mmdvm_silent_client_dropped` | rig+slow | a | OK |
| P-DMR-14 After the drop the client gets no further voice (PARTIAL: sut==ref only) | protocols/mmdvm.md:242 | `test_mmdvm::test_mmdvm_dropped_client_no_voice` | rig+slow | a | PARTIAL |
| P-DMR-15 RPTCL (13 B and 9 B) closes the session, held floor released | protocols/mmdvm.md:243 | `test_mmdvm::test_mmdvm_rptcl_close`, `test_floor::test_dmr_rptcl_closes_session` | rig/crossmode | a | OK |
| P-DMR-16 RPTINTR (17 B) gets no reply, the login stays (PARTIAL: sut==ref only) | protocols/mmdvm.md:244 | `test_mmdvm::test_mmdvm_rptintr_ignored` | rig | a | PARTIAL |
| P-DMR-17 Unknown tags, short and oversize packets get no reply, master keeps serving (PARTIAL: sut==ref only) | protocols/mmdvm.md:250 | `test_mmdvm::test_mmdvm_garbage_ignored` | rig | a | PARTIAL |
| P-DMR-18 A DMRD of a size other than 55 B is not relayed | protocols/mmdvm.md:251 | `test_mmdvm::test_mmdvm_dmrd_wrong_size` | rig | a | OK |
| P-DMR-19 DMRD from an address not logged in is not relayed (MSTNAK, rate limited) | protocols/mmdvm.md:252 | `test_mmdvm::test_mmdvm_dmrd_not_logged_in` | rig | a | OK |
| P-DMR-20 Only slot 2 is a reflector slot, slot 1 header/voice/terminator ignored (PARTIAL: sut==ref only) | protocols/mmdvm.md:266 | `test_mmdvm::test_mmdvm_slot1_ignored` | rig | a | PARTIAL |
| P-DMR-21 Group calls relayed, private-call (bit 6) voice is not (PARTIAL: sut==ref only) | protocols/mmdvm.md:267 | `test_mmdvm::test_mmdvm_private_call_not_relayed` | rig | a | PARTIAL |
| P-DMR-22 Stream opens with a type-1 header with BS or MS data sync, other sync opens nothing | protocols/mmdvm.md:268 | `test_mmdvm::test_mmdvm_header_sync_required` | rig | a | OK |
| P-DMR-23 Voice superframes A-F, three AMBE+2 frames per burst relayed unchanged (PARTIAL: sut==ref only) | protocols/mmdvm.md:269 | `test_mmdvm::test_mmdvm_superframe_relayed_exact` | rig | a | PARTIAL |
| P-DMR-24 A stream ends with a type-2 terminator passed to listeners | protocols/mmdvm.md:270 | `test_mmdvm::test_mmdvm_terminator_ends_stream` | rig | a | OK |
| P-DMR-25 Egress: header, voice A-F, terminator, slot 2, group call, one stream id, dst TG 9 | protocols/mmdvm.md:271 | `test_mmdvm::test_mmdvm_egress_structure` | rig | a | OK |
| P-DMR-26 Egress sequence byte starts at 0 and counts up per stream (PARTIAL: sut==ref only) | protocols/mmdvm.md:272 | `test_mmdvm::test_mmdvm_egress_sequence` | rig | a | PARTIAL |
| P-DMR-27 Egress source id = talker radio id; egress repeater id | protocols/mmdvm.md:273 | `test_mmdvm::test_mmdvm_egress_ids` | rig | a | OK |
| P-DMR-28 Egress stream id (DV-Router keeps the ingress id) | protocols/mmdvm.md:274 | `test_mmdvm::test_mmdvm_egress_stream_id`, `test_floor::test_sid_preserved` | rig | a | OK |
| P-DMR-29 A header to TG 4001..4026 links the client to module A..Z (PARTIAL: sut==ref only) | protocols/mmdvm.md:286 | `test_mmdvm::test_mmdvm_tg_links_module` | rig | a | PARTIAL |
| P-DMR-30 A header to TG 4000 unlinks, no further voice (PARTIAL: sut==ref only) | protocols/mmdvm.md:287 | `test_mmdvm::test_mmdvm_tg4000_unlinks` | rig | a | PARTIAL |
| P-DMR-31 A linked client talking to another module's TG stays on its module (PARTIAL: sut==ref only) | protocols/mmdvm.md:288 | `test_mmdvm::test_mmdvm_other_tg_stays_on_module` | rig | a | PARTIAL |
| P-DMR-32 TG 9 from a linked client goes to its module, from an unlinked client ignored (PARTIAL: sut==ref only) | protocols/mmdvm.md:289 | `test_mmdvm::test_mmdvm_tg9_routing` | rig | a | PARTIAL |
| P-DMR-33 A header+terminator stream joins and is not relayed as voice (PARTIAL: sut==ref only) | protocols/mmdvm.md:290 | `test_mmdvm::test_mmdvm_join_by_empty_stream` | rig | a | PARTIAL |
| P-DMR-43 Private-call link burst (3 headers + 2 terminators) links 400x / unlinks 4000, not relayed (PARTIAL: sut==ref only) | protocols/mmdvm.md:291 | `test_mmdvm::test_mmdvm_gateway_private_link` | rig | a | PARTIAL |
| P-DMR-34 A stream whose terminator is lost ends after a silence timeout, module free | protocols/mmdvm.md:297 | `test_mmdvm::test_mmdvm_lost_terminator_timeout` | rig | a | OK |
| P-DMR-35 A second header on a busy module is not mixed into the running stream | protocols/mmdvm.md:298 | `test_mmdvm::test_mmdvm_busy_module_no_mix` | rig | a | OK |
| P-DMR-36 Voice bursts without a header: late entry relayed behind a header | protocols/mmdvm.md:299 | `test_mmdvm::test_mmdvm_voice_without_header` | rig | a | OK |
| P-DMR-37 The talker never receives its own stream back (PARTIAL: sut==ref only) | protocols/mmdvm.md:300 | `test_mmdvm::test_mmdvm_no_self_echo` | rig | a | PARTIAL |
| P-DMR-41 Non-voice data bursts (CSBK, data header, rate 1/2, 3/4) open no voice stream | protocols/mmdvm.md:301 | `test_mmdvm::test_mmdvm_data_burst_not_voice` | rig | a | OK |
| P-DMR-42 Ingress sequence byte wraps 255 to 0 in a long over without loss | protocols/mmdvm.md:302 | `test_mmdvm::test_mmdvm_sequence_wrap` | rig | a | OK |
| P-DMR-38 Two ids from one IP are two sessions, each gets the module's traffic once (PARTIAL: sut==ref only) | protocols/mmdvm.md:308 | `test_mmdvm::test_mmdvm_two_ids_one_ip` | rig | a | PARTIAL |
| P-DMR-39 The same id from two ports: two sessions, traffic once each | protocols/mmdvm.md:309 | `test_mmdvm::test_mmdvm_same_id_two_ports` | rig | a | OK |
| P-DMR-40 A keepalive from one client never refreshes another client's session (PARTIAL: sut==ref only) | protocols/mmdvm.md:310 | `test_mmdvm::test_mmdvm_keepalive_isolated` | rig+slow | a | PARTIAL |

## DMRplus
Sources: protocols/dmrplus.md (testlist rows at the end of the doc).

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-DMP-01 31 B connect from a known id links to the module and is answered ACK OK (PARTIAL: sut==ref only) | protocols/dmrplus.md:140 | `test_dmrplus::test_dmp_connect_ack` | rig | a | PARTIAL |
| P-DMP-02 Connect from an id missing from the database gets no link (NAK, ACK in fallback mode) | protocols/dmrplus.md:141 | `test_dmrplus::test_dmp_connect_unknown_id` | rig | a | OK |
| P-DMP-03 A connect the access list refuses is answered NAK (PARTIAL: skips unless DVT_XLXD_BLACKLIST is set; sut==ref only) | protocols/dmrplus.md:142 | `test_dmrplus::test_dmp_connect_refused` | rig | a | PARTIAL |
| P-DMP-04 Connect to a reflector number mapping to no module gets NAK, nothing heard | protocols/dmrplus.md:143 | `test_dmrplus::test_dmp_connect_no_module` | rig | a | OK |
| P-DMP-05 Keepalive repeats the 31 B packet, each ACKed, no server keepalive (PARTIAL: sut==ref only) | protocols/dmrplus.md:144 | `test_dmrplus::test_dmp_keepalive_ack` | rig | a | PARTIAL |
| P-DMP-06 Keepalive naming another reflector number from a linked client moves it | protocols/dmrplus.md:145 | `test_dmrplus::test_dmp_keepalive_other_module` | rig | a | OK |
| P-DMP-07 A client silent for 10 s is dropped, nothing sent on drop (PARTIAL: sut==ref only) | protocols/dmrplus.md:146 | `test_dmrplus::test_dmp_silent_client_dropped` | rig | a | PARTIAL |
| P-DMP-08 A dropped client gets no further voice (PARTIAL: sut==ref only) | protocols/dmrplus.md:147 | `test_dmrplus::test_dmp_dropped_client_no_voice` | rig | a | PARTIAL |
| P-DMP-09 32 B disconnect from a linked address unlinks it, no reply (PARTIAL: sut==ref only) | protocols/dmrplus.md:148 | `test_dmrplus::test_dmp_disconnect` | rig | a | PARTIAL |
| P-DMP-10 A disconnect from an address that is not linked is ignored (PARTIAL: sut==ref only) | protocols/dmrplus.md:149 | `test_dmrplus::test_dmp_disconnect_unknown` | rig | a | PARTIAL |
| P-DMP-11 A type-2 voice LC header from a linked client opens a stream | protocols/dmrplus.md:155 | `test_dmrplus::test_dmp_header_opens_stream` | rig | a | OK |
| P-DMP-12 Slot 1 packets are ignored (PARTIAL: sut==ref only) | protocols/dmrplus.md:156 | `test_dmrplus::test_dmp_ingress_filtered` | rig | a | PARTIAL |
| P-DMP-13 A colour code other than 1 is ignored (PARTIAL: sut==ref only) | protocols/dmrplus.md:157 | `test_dmrplus::test_dmp_ingress_filtered` | rig | a | PARTIAL |
| P-DMP-14 Private-call voice (call type not 1) is not relayed (PARTIAL: sut==ref only) | protocols/dmrplus.md:158 | `test_dmrplus::test_dmp_ingress_filtered` | rig | a | PARTIAL |
| P-DMP-15 A header to another module's TG goes to the linked module | protocols/dmrplus.md:159 | `test_dmrplus::test_dmp_header_other_module` | rig | a | OK |
| P-DMP-16 A header with a source id missing from the id database | protocols/dmrplus.md:160 | `test_dmrplus::test_dmp_unknown_source` | rig | a | OK |
| P-DMP-17 The three AMBE+2 frames of each burst reach listeners unchanged | protocols/dmrplus.md:161 | `test_dmrplus::test_dmp_frames_exact_to_mmdvm` | rig | a | OK |
| P-DMP-18 Voice without a header: late entry behind a synthesized header | protocols/dmrplus.md:162 | `test_dmrplus::test_dmp_no_header_not_relayed` | rig | a | OK |
| P-DMP-19 A type-3 terminator ends the stream and frees the module at once | protocols/dmrplus.md:163 | `test_dmrplus::test_dmp_terminator_frees_module` | rig | a | OK |
| P-DMP-20 A second header within a running stream does not open a new one | protocols/dmrplus.md:164 | `test_dmrplus::test_dmp_repeated_header` | rig | a | OK |
| P-DMP-21 The talker never receives its own stream back (PARTIAL: sut==ref only) | protocols/dmrplus.md:165 | `test_dmrplus::test_dmp_no_echo` | rig | a | PARTIAL |
| P-DMP-22 Egress type-2 header: TS2, CC 1, group, dst 4001 + module, source = talker id | protocols/dmrplus.md:171 | `test_dmrplus::test_dmp_egress_header_fields` | rig | a | OK |
| P-DMP-23 Egress header burst is a real voice LC header (BPTC LC, data sync, data type 1) | protocols/dmrplus.md:172 | `test_dmrplus::test_dmp_egress_lc_header` | rig | a | OK |
| P-DMP-24 Egress voice A-F: A voice sync, B-F EMB (CC 1) and embedded LC | protocols/dmrplus.md:173 | `test_dmrplus::test_dmp_egress_embedded_lc` | rig | a | OK |
| P-DMP-25 Egress stream ends with a type-3 terminator carrying LC | protocols/dmrplus.md:174 | `test_dmrplus::test_dmp_egress_terminator` | rig | a | OK |
| P-DMP-26 One voice packet per burst (60 ms), sequence 4 after the header, +1 mod 256 (PARTIAL: sut==ref only) | protocols/dmrplus.md:175 | `test_dmrplus::test_dmp_egress_sequence` | rig | a | PARTIAL |
| P-DMP-27 Burst on the wire: 33 B + 1 pad, 16-bit pairs swapped, ids little endian (PARTIAL: sut==ref only) | protocols/dmrplus.md:176 | `test_dmrplus::test_dmp_frames_exact_from_mmdvm` | rig | a | PARTIAL |
| P-DMP-28 Datagrams of a size other than 31, 32 or 72 get no reply, service continues (PARTIAL: sut==ref only) | protocols/dmrplus.md:182 | `test_dmrplus::test_dmp_garbage_ignored` | rig | a | PARTIAL |
| P-DMP-29 A 72 B packet of a type other than 1, 2, 3 is ignored | protocols/dmrplus.md:183 | `test_dmrplus::test_dmp_unknown_type` | rig | a | OK |
| P-DMP-30 Voice from an address that is not linked is not relayed (PARTIAL: sut==ref only) | protocols/dmrplus.md:184 | `test_dmrplus::test_dmp_unlinked_sender` | rig | a | PARTIAL |
| P-DMP-31 Two clients on one IP (two ports) are two sessions, each gets traffic once (PARTIAL: sut==ref only) | protocols/dmrplus.md:190 | `test_dmrplus::test_dmp_two_clients` | rig | a | PARTIAL |
| P-DMP-32 The same DMR id from two addresses: two clients (PARTIAL: sut==ref only) | protocols/dmrplus.md:191 | `test_dmrplus::test_dmp_two_clients` | rig | a | PARTIAL |
| P-DMP-33 DMR+ <-> MMDVM frames pass unchanged in both directions (PARTIAL: sut==ref only) | protocols/dmrplus.md:192 | `test_dmrplus::test_dmp_frames_exact_to_mmdvm`, `test_dmrplus::test_dmp_frames_exact_from_mmdvm` | rig | a | PARTIAL |
| P-DMP-34 DMR+ <-> D-STAR / M17 / P25 / NXDN through the transcoder (PARTIAL: only D-STAR and M17) | protocols/dmrplus.md:193 | `test_dmrplus::test_dmp_cross_mode` | crossmode | a | PARTIAL |
| P-DMP-35 One talker per module: a second DMR+ header on a busy module is not mixed in | protocols/dmrplus.md:194 | `test_dmrplus::test_dmp_busy_module_no_mix` | rig | a | OK |

## YSF
Sources: protocols/ysf.md (testlist rows at the end of the doc).

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-YSF-01 14 B YSFP from a new address linked and answered at once, every later poll too (PARTIAL: sut==ref only) | protocols/ysf.md:133 | `test_ysf::test_ysf_poll_reply` | rig | a | PARTIAL |
| P-YSF-02 The poll reply comes from the address and port the client sent to (PARTIAL: sut==ref only) | protocols/ysf.md:134 | `test_ysf::test_ysf_poll_reply` | rig | a | PARTIAL |
| P-YSF-03 The 10 bytes after YSFP in the poll reply (REFLECTOR) | protocols/ysf.md:135 | `test_ysf::test_ysf_poll_reply_text` | rig | a | OK |
| P-YSF-04 Poll with a suffixed or 10-character callsign answered (PARTIAL: slash form not probed) | protocols/ysf.md:136 | `test_ysf::test_ysf_poll_callsign_forms` | rig | a | PARTIAL |
| P-YSF-05 A poll that is not 14 B (PARTIAL: only the 15 B form probed, short polls not) | protocols/ysf.md:137 | `test_ysf::test_ysf_poll_length` | rig | a | PARTIAL |
| P-YSF-06 The reflector does not poll the client on its own | protocols/ysf.md:138 | `test_ysf::test_ysf_server_polls` | rig | a | OK |
| P-YSF-07 A client silent past the timeout is dropped, polls alone keep it linked | protocols/ysf.md:139 | `test_ysf::test_ysf_timeout_drop` | rig+slow | a | OK |
| P-YSF-08 YSFU from a linked client removes it (no further voice), no reply | protocols/ysf.md:145 | `test_ysf::test_ysf_unlink`, `test_clients2::test_link_unlink_lifecycle` | rig | a | OK |
| P-YSF-09 YSFS from any source answered with the 42 B status (PARTIAL: sut==ref only) | protocols/ysf.md:146 | `test_ysf::test_ysf_status` | rig | a | PARTIAL |
| P-YSF-10 Status fields: id 5 digits, name 16, description 14, count 3 digits (PARTIAL: name/description padding not asserted) | protocols/ysf.md:147 | `test_ysf::test_ysf_status_fields` | rig | a | PARTIAL |
| P-YSF-11 A YSFD from a linked client reaches every other client of its module once | protocols/ysf.md:153 | `test_ysf::test_ysf_relay_once` | rig | a | OK |
| P-YSF-12 The talker never receives its own stream back | protocols/ysf.md:154 | `test_ysf::test_ysf_no_echo` | rig | a | OK |
| P-YSF-13 A YSFD from an address that is not linked is not relayed | protocols/ysf.md:155 | `test_ysf::test_ysf_unlinked_sender` | rig | a | OK |
| P-YSF-14 Egress YSFD 155 B, frame sync, decodable FICH, header first, terminator last | protocols/ysf.md:156 | `test_ysf::test_ysf_egress_structure` | rig | a | OK |
| P-YSF-15 Egress gateway field stays the same for a whole stream | protocols/ysf.md:157 | `test_ysf::test_ysf_egress_gateway_constant` | rig | a | OK |
| P-YSF-16 Egress counter differs per frame, first frame end bit clear (PARTIAL: +1 step not asserted) | protocols/ysf.md:158 | `test_ysf::test_ysf_egress_counter` | rig | a | PARTIAL |
| P-YSF-17 The last frame of a stream has the end bit set | protocols/ysf.md:159 | `test_ysf::test_ysf_egress_end_bit` | rig | a | OK |
| P-YSF-18 Same-mode relay keeps voice, source, destination, DCH/CSD text | protocols/ysf.md:160 | `test_ysf::test_ysf_same_mode_kept`, `test_clients2::test_ysf_gain_step_only` | rig | a | OK |
| P-YSF-19 Egress source = talker, destination ALL for a cross-mode stream | protocols/ysf.md:161 | `test_ysf::test_ysf_crossmode_callsigns` | rig | a | OK |
| P-YSF-20 Stream ends on the end bit, FI terminator or about 1.5 s silence | protocols/ysf.md:167 | `test_ysf::test_ysf_stream_end_markers` | rig | a | OK |
| P-YSF-21 A stream that starts without a header (late entry) is carried | protocols/ysf.md:168 | `test_ysf::test_ysf_late_entry` | rig | a | OK |
| P-YSF-22 A second talker on a busy module is not mixed into the running stream | protocols/ysf.md:169 | `test_ysf::test_ysf_second_talker` | rig | a | OK |
| P-YSF-23 A stream longer than 128 frames wraps the counter and stays one stream | protocols/ysf.md:170 | `test_ysf::test_ysf_counter_wrap` | rig | a | OK |
| P-YSF-24 V/D type 2 (DN2) voice is carried | protocols/ysf.md:176 | `test_ysf::test_ysf_relay_once` | rig | a | OK |
| P-YSF-25 V/D type 1 (DN1) and Voice FR (VW) are carried between YSF clients | protocols/ysf.md:177 | `test_ysf::test_ysf_dn1_vw_carried` | rig | a | OK |
| P-YSF-26 Data FR frames that are not WIRES-X commands are relayed | protocols/ysf.md:178 | `test_ysf::test_ysf_data_fr` | rig | a | OK |
| P-YSF-27 A YSFD whose FICH does not decode is not relayed | protocols/ysf.md:179 | `test_ysf::test_ysf_bad_fich` | rig | a | OK |
| P-YSF-28 A header with DG-ID 10..35 moves the client to module A..Z | protocols/ysf.md:180 | `test_ysf::test_ysf_dgid_module` | rig | a | OK |
| P-YSF-29 A new client's module before any selection (default module B) | protocols/ysf.md:181 | `test_ysf::test_ysf_default_module` | rig | a | OK |
| P-YSF-30 Egress DG-ID (FICH SQ) is 0 | protocols/ysf.md:182 | `test_ysf::test_ysf_egress_dgid` | rig | a | OK |
| P-YSF-31 A WIRES-X DX_REQ is answered with a DX reply to the requester | protocols/ysf.md:193 | `test_ysf::test_ysf_wiresx_dx` | rig | a | OK |
| P-YSF-32 CONN_REQ room n selects module, DISC_REQ leaves the module | protocols/ysf.md:194 | `test_ysf::test_ysf_wiresx_conn_disc` | rig | a | OK |
| P-YSF-33 YSFO, YSFI, YSFV and unknown tags get no reply, link continues (PARTIAL: sut==ref only) | protocols/ysf.md:200 | `test_ysf::test_ysf_other_tags` | rig | a | PARTIAL |
| P-YSF-34 Short/long YSFD, bad frame sync, short YSFU: no reply, not relayed | protocols/ysf.md:201 | `test_ysf::test_ysf_malformed` | rig | a | OK |
| P-YSF-35 Two ports on one IP are two clients, each gets the traffic once | protocols/ysf.md:207 | `test_ysf::test_ysf_two_ports` | rig | a | OK |
| P-YSF-36 Two callsigns polling from the same address are one client (one copy) | protocols/ysf.md:208 | `test_ysf::test_ysf_two_callsigns_one_address` | rig | a | OK |
| P-YSF-37 A poll from one client never refreshes another client's link | protocols/ysf.md:209 | `test_ysf::test_ysf_timeout_drop` | rig+slow | a | OK |

## NXDN
Sources: protocols/nxdn.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-NXD-01 Poll NXDNP + 10 B padded callsign + TG, 17 B, echoed (PARTIAL: callsign form only sut == ref) | protocols/nxdn.md:128 | `test_nxdn_peer::test_nxdn_poll_format` | nxdn | a | PARTIAL |
| P-NXD-02 Poll every 5 s, never 120 s silent (PARTIAL: 5 s period only sut == ref; 120 s bound asserted) | protocols/nxdn.md:129 | `test_nxdn_peer::test_nxdn_poll_period` | nxdn | a | PARTIAL |
| P-NXD-03 Reflector stops echoing: keep polling, relink when it answers again | protocols/nxdn.md:130 | `test_nxdn_peer::test_nxdn_reflector_loss_relink` | nxdn+slow | a | OK |
| P-NXD-04 NXDNU + callsign + TG on leaving the TG (row removed, shutdown) | protocols/nxdn.md:131 | `test_nxdn_peer::test_nxdn_unlink_on_leave` | nxdn+slow | a | OK |
| P-NXD-05 Every egress packet is 43 B NXDND, group flag, dst = TG | protocols/nxdn.md:137 | `test_nxdn_peer::test_nxdn_egress_framing` | nxdn | c | OK |
| P-NXD-06 Flags match the payload (header 0x05, voice 0x01, trailer 0x09); header first, trailer last | protocols/nxdn.md:138 | `test_nxdn_peer::test_nxdn_egress_flags` | nxdn | c | OK |
| P-NXD-07 Header frame: LICH 0x81, FACCH1 VCALL with CRC-12 in both blocks, SACCH IDLE | protocols/nxdn.md:139 | `test_nxdn_peer::test_nxdn_egress_header`, `test_nxdn::test_dmr_to_nxdn_passthrough_and_lifecycle` | nxdn | c | OK |
| P-NXD-08 VCALL fields: group call type, CC 0, src = talker id, dst = TG, cipher 00 | protocols/nxdn.md:140 | `test_nxdn_peer::test_nxdn_egress_vcall_fields` | nxdn | c | OK |
| P-NXD-09 Voice frames: LICH 0xAC, four 49-bit AMBE+2 frames per packet | protocols/nxdn.md:141 | `test_nxdn_peer::test_nxdn_egress_voice` | nxdn | c | OK |
| P-NXD-10 Voice SACCH superframe VCALL 1/4..4/4 from the first voice frame, CRC-6, parts rebuild VCALL | protocols/nxdn.md:142 | `test_nxdn_peer::test_nxdn_egress_sacch`, `test_nxdn::test_dmr_to_nxdn_passthrough_and_lifecycle` | nxdn | c | OK |
| P-NXD-11 Exactly one trailer: LICH 0x81, FACCH1 TX_REL with CRC-12, flags 0x09 | protocols/nxdn.md:143 | `test_nxdn_peer::test_nxdn_egress_trailer`, `test_nxdn::test_dmr_to_nxdn_passthrough_and_lifecycle` | nxdn | c | OK |
| P-NXD-12 72 to 49 bit conversion bit-exact outside the gain bits, both directions | protocols/nxdn.md:144 | `test_nxdn_peer::test_nxdn_frame_conversion`, `test_nxdn::test_nxdn_to_dmr_passthrough`, `test_nxdn::test_dmr_to_nxdn_passthrough_and_lifecycle` | nxdn | c | OK |
| P-NXD-13 No gain step on AMBE+2 parameters in either direction | protocols/nxdn.md:145 | `test_nxdn_peer::test_nxdn_gain_step`, `test_nxdn::test_nxdn_to_dmr_passthrough`, `test_nxdn::test_dmr_to_nxdn_passthrough_and_lifecycle` | nxdn | c | OK |
| P-NXD-14 Partial last group padded to 4 frames with the spec silence frame (PARTIAL: only sut == ref) | protocols/nxdn.md:146 | `test_nxdn_peer::test_nxdn_partial_group_padded` | nxdn | c | PARTIAL |
| P-NXD-15 One voice packet per 80 ms frame, paced at source rate, no bursts | protocols/nxdn.md:147 | `test_nxdn_peer::test_nxdn_egress_pacing` | nxdn | c | OK |
| P-NXD-16 src from the NXDN id table by callsign; unknown talker per no-radio-id policy | protocols/nxdn.md:148 | `test_nxdn_peer::test_nxdn_egress_src_id` | nxdn | c | OK |
| P-NXD-30 Empty stream from another mode (join, kerchunk) not sent to NXDN | protocols/nxdn.md:149 | `test_nxdn_peer::test_nxdn_empty_stream_not_relayed` | nxdn | c | OK |
| P-NXD-17 Peer never sends a stream back to the reflector it came from | protocols/nxdn.md:150 | `test_nxdn_peer::test_nxdn_no_echo`, `test_nxdn::test_nxdn_to_dmr_passthrough` | nxdn | c | OK |
| P-NXD-18 Only 17 B NXDNP and 43 B NXDND accepted, garbage ignored (PARTIAL: only sut == ref) | protocols/nxdn.md:156 | `test_nxdn_peer::test_nxdn_garbage_ignored` | nxdn | a | PARTIAL |
| P-NXD-19 Group header opens the stream, frames routed, trailer ends it at once | protocols/nxdn.md:157 | `test_nxdn_peer::test_nxdn_ingress_header_trailer` | nxdn | c | OK |
| P-NXD-20 Voice without a header joins late after the SACCH 1/4..4/4 | protocols/nxdn.md:158 | `test_nxdn_peer::test_nxdn_ingress_late_entry` | nxdn | c | OK |
| P-NXD-21 Repeated header inside a running call does not split or restart it | protocols/nxdn.md:159 | `test_nxdn_peer::test_nxdn_ingress_repeated_header` | nxdn | c | OK |
| P-NXD-22 Repeated trailers end the call once, next call opens (PARTIAL: only sut == ref) | protocols/nxdn.md:160 | `test_nxdn_peer::test_nxdn_ingress_repeated_trailer` | nxdn | c | PARTIAL |
| P-NXD-23 Frame with FACCH1-stolen half delivers only its real voice frames | protocols/nxdn.md:161 | `test_nxdn_peer::test_nxdn_ingress_steal` | nxdn | c | OK |
| P-NXD-24 Data frames (flags 0x02/0x03, LICH 0x9X) open no voice stream | protocols/nxdn.md:162 | `test_nxdn_peer::test_nxdn_ingress_data` | nxdn | c | OK |
| P-NXD-25 Private calls and dst != TG not routed (tester as reflector) | protocols/nxdn.md:163 | `test_nxdn_peer::test_nxdn_ingress_private_or_other_tg` | nxdn | a | OK |
| P-NXD-26 Encrypted calls (cipher != 00) not transcoded as clear audio | protocols/nxdn.md:164 | `test_nxdn_peer::test_nxdn_ingress_encrypted` | nxdn | c | OK |
| P-NXD-27 Two NXDN talkers at once: one winner, no mixing | protocols/nxdn.md:165 | `test_nxdn_peer::test_nxdn_two_talkers` | nxdn | c | OK |
| P-NXD-28 Lost trailer: call ends by timeout, terminator to egress, module free | protocols/nxdn.md:166 | `test_nxdn_peer::test_nxdn_lost_trailer_timeout` | nxdn | c | OK |
| P-NXD-29 Ingress src id maps to callsign and DMR id for other modes; unknown per policy | protocols/nxdn.md:167 | `test_nxdn_peer::test_nxdn_ingress_src_mapping` | nxdn | c | OK |
| P-NXD-31 Poll for the served TG echoed identically, once per poll; registers the gateway | protocols/nxdn.md:192 | `test_nxdn_server::test_nxdn_server_poll_echo` | nxdn | a | OK |
| P-NXD-32 Poll for a TG the server does not carry gets no reply | protocols/nxdn.md:193 | `test_nxdn_server::test_nxdn_server_poll_other_tg` | nxdn | a | OK |
| P-NXD-33 Group call reaches every other gateway: header, frames unchanged, trailer, dst = TG | protocols/nxdn.md:194 | `test_nxdn_server::test_nxdn_server_relay` | nxdn | a | OK |
| P-NXD-34 NXDNU removes the gateway at once, no reply, no further traffic | protocols/nxdn.md:195 | `test_nxdn_server::test_nxdn_server_unlink` | nxdn | a | OK |
| P-NXD-35 NXDND from an address that never polled is not relayed | protocols/nxdn.md:196 | `test_nxdn_server::test_nxdn_server_unknown_source` | nxdn | a | OK |
| P-NXD-36 The talking gateway gets nothing of its own call back | protocols/nxdn.md:197 | `test_nxdn_server::test_nxdn_server_relay` | nxdn | a | OK |
| P-NXD-37 Individual calls and group calls to another TG are not relayed | protocols/nxdn.md:198 | `test_nxdn_server::test_nxdn_server_private_or_other_tg` | nxdn | a | OK |
| P-NXD-38 Two gateways talk at once: one winner, no mixing | protocols/nxdn.md:199 | `test_nxdn_server::test_nxdn_server_two_talkers` | nxdn | a | OK |
| P-NXD-39 Call without trailer: listeners get a synthesized trailer, next call relayed | protocols/nxdn.md:200 | `test_nxdn_server::test_nxdn_server_lost_trailer` | nxdn | a | OK |
| P-NXD-40 Non-NXDN, short, long, unknown-tag packets get no reply, gateway stays | protocols/nxdn.md:201 | `test_nxdn_server::test_nxdn_server_garbage_ignored` | nxdn | a | OK |
| P-NXD-41 Server sends no polls of its own and does not relay polls | protocols/nxdn.md:202 | `test_nxdn_server::test_nxdn_server_no_unsolicited` | nxdn | a | OK |
| P-NXD-42 Data frames from a gateway relayed NXDN-to-NXDN (PARTIAL: count only, not byte-exact) | protocols/nxdn.md:203 | `test_nxdn_server::test_nxdn_server_data_frames` | nxdn | a | PARTIAL |
| P-NXD-43 Silent gateway dropped after the timeout, then gets no traffic | protocols/nxdn.md:204 | `test_nxdn_server::test_nxdn_server_client_expiry` | nxdn+slow | a | OK |

## NXDN GPS
Sources: nxdn-gps-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| UDCH is 22 layer 3 octets; DCALL header 0x09 octets 0-10 (call type, src, dst, packet info) | nxdn-gps-cases.md:42 | `test_nxdn_l3::test_header_and_block_octets`, `test_nxdn_l3::test_full_gps_call_on_the_wire` | unit | a | OK |
| DCALL user data 0x0B: octet 1 = frame/block nibbles, 20 user octets | nxdn-gps-cases.md:43 | `test_nxdn_l3::test_header_and_block_octets` | unit | a | OK |
| Packet information bits: delivery, retry, block count - 1, pad count, start/circular, 9-bit fragment count | nxdn-gps-cases.md:44 | `test_nxdn_l3::test_packet_information_fields` | unit | a | OK |
| Block/frame nibbles count down to 0; retry keeps block numbers, recounts frame numbers | nxdn-gps-cases.md:45 | `test_nxdn_l3::test_published_block_numbering_ts1b_figure_5_4_1`, `test_nxdn_l3::test_retry_block_range` | unit | a | OK |
| Message CRC-32 over user data, preset ones, MSB first, 0x04C11DB7, no reflection, no final xor | nxdn-gps-cases.md:46 | `test_nxdn_l3::test_crc32_published_check_value`, `test_nxdn_l3::test_crc32_matches_table_implementation` | unit | a | OK |
| Pad octets follow the CRC; total bytes = blocks * 20 - pad | nxdn-gps-cases.md:47 | `test_nxdn_l3::test_blocks_and_pad`, `test_nxdn_l3::test_user_data_over_limit` | unit | a | OK |
| 33-byte frame = LICH, RAN, 22 octets, 9 zeros; NXDND flags 0x03+0x04 header, 0x03+0x08 TX_REL | nxdn-gps-cases.md:48 | `test_nxdn_l3::test_udch_frame_and_flags`, `test_nxdn_l3::test_full_gps_call_on_the_wire` | unit | a | OK |
| LICH of a data frame 0x90 is what a radio or repeater sends (UNVERIFIED) | nxdn-gps-cases.md:49 | - | - | - | GAP |
| F1/F2 (octet 0 bits 7-6) are 0 (spare) | nxdn-gps-cases.md:50 | `test_nxdn_l3::test_header_and_block_octets` | unit | a | OK |
| Kenwood FULL 38 B / SHORT 17 B, type byte 0x00/0x01 (PARTIAL: encoder only, gateway run is manual) | nxdn-gps-cases.md:51 | `test_nxdn_l3::test_full_round_trip_table`, `test_nxdn_l3::test_short_and_no_fix_and_errors` | unit | a | PARTIAL |
| Latitude: 15-bit fraction bytes 9-10, hemisphere bit, deg*100+min LE bytes 11-12 (PARTIAL: encoder only) | nxdn-gps-cases.md:52 | `test_nxdn_l3::test_base_position_fields`, `test_nxdn_l3::test_hemispheres_and_negative_longitude` | unit | a | PARTIAL |
| Longitude: degrees*100+minutes LE in bytes 15-16, hemisphere bit in byte 13 (PARTIAL: encoder only) | nxdn-gps-cases.md:53 | `test_nxdn_l3::test_base_position_fields`, `test_nxdn_l3::test_hemispheres_and_negative_longitude` | unit | a | PARTIAL |
| Fraction unit 1e-4 minute; gateway truncates to 1/100 (PARTIAL: gateway cut only in manual run) | nxdn-gps-cases.md:54 | `test_nxdn_l3::test_degmin_boundaries`, `test_nxdn_l3::test_degmin_round_trip_grid`, `test_nxdn_l3::test_longitude_fraction_rounding_in_message` | unit | a | PARTIAL |
| Longitude fraction place disputed: GW bytes 13-14, dsd-fme 32-33 (PARTIAL: dsd-fme unrun) | nxdn-gps-cases.md:55 | `test_nxdn_l3::test_the_two_readings_disagree_on_exactly_three_fields` | unit | a | PARTIAL |
| Course (tenths, 12 bits) packing disputed between GW and dsd-fme (PARTIAL: dsd-fme side unrun, truth open) | nxdn-gps-cases.md:56 | `test_nxdn_l3::test_course_boundaries`, `test_nxdn_l3::test_course_359_and_wrap_in_both_packings`, `test_nxdn_l3::test_the_two_readings_disagree_on_exactly_three_fields` | unit | a | PARTIAL |
| Speed packing disputed (GW whole + tenths, dsd-fme 14-bit tenths) (PARTIAL: dsd-fme unrun) | nxdn-gps-cases.md:57 | `test_nxdn_l3::test_speed_limits_and_sentinels`, `test_nxdn_l3::test_the_two_readings_disagree_on_exactly_three_fields`, `test_oracle_nxdn::test_gateway_reading_of_speed_nibble_over_9` | unit | a | PARTIAL |
| Elevation: signed 16 bit, byte 27 low, byte 28 high (PARTIAL: one source, no decoder) | nxdn-gps-cases.md:58 | `test_nxdn_l3::test_elevation_signed_and_time_fields` | unit | a | PARTIAL |
| Time: hour 5 bits, minute 6 bits over bytes 3-5, no seconds (PARTIAL: one source, no decoder) | nxdn-gps-cases.md:59 | `test_nxdn_l3::test_elevation_signed_and_time_fields` | unit | a | PARTIAL |
| Date: year-2000 7 bits, month 4 bits, day-1 5 bits, bytes 17-18 (PARTIAL: one source, no decoder) | nxdn-gps-cases.md:60 | `test_nxdn_l3::test_elevation_signed_and_time_fields` | unit | a | PARTIAL |
| Bytes 0-2 zero, byte 36 = 0xFC, byte 37 = 0xFF marker | nxdn-gps-cases.md:61 | `test_nxdn_l3::test_full_round_trip_table` | unit | a | OK |
| No-fix sentinels print no line; course/speed sentinels 1023 and 15 (PARTIAL: course/speed sentinels not run) | nxdn-gps-cases.md:62 | `test_nxdn_l3::test_short_and_no_fix_and_errors`, `test_nxdn_l3::test_speed_limits_and_sentinels`, `test_oracle_nxdn::test_gateway_records_no_fix_and_missing_output` | unit | a | PARTIAL |
| Unmapped bytes (6-8, 19-22, 29-35) are zero (UNKNOWN meaning) | nxdn-gps-cases.md:63 | - | - | - | GAP |
| Status field (nothing in any source) | nxdn-gps-cases.md:64 | - | - | - | NOT-IMPLEMENTED |
| VERY_SHORT (type 0x02) not generated, parser refuses it | nxdn-gps-cases.md:65 | `test_nxdn_l3::test_short_and_no_fix_and_errors` | unit | a | OK |
| Icom form: opcode 0x06 then NMEA GPRMC with XOR checksum (PARTIAL: builder only, gateway run manual) | nxdn-gps-cases.md:66 | `test_nxdn_l3::test_nmea_checksum_published_example`, `test_nxdn_l3::test_nmea_rmc_and_icom_form` | unit | a | PARTIAL |
| Disagreement record verdicts agree/disagree/no-output* and exit status 0/1/2 | nxdn-gps-cases.md:84 | `test_oracle_nxdn::test_main_writes_record_and_exit_status`, `test_oracle_nxdn::test_record_missing_output` | unit | a | OK |
| Record fields per decoder: values, agree, encoded input, which decoder matches; prediction_ok | nxdn-gps-cases.md:86 | `test_oracle_nxdn::test_record_disagreement_on_the_three_disputed_fields`, `test_oracle_nxdn::test_record_prediction_miss_is_flagged`, `test_oracle_nxdn::test_record_layouts_swap_who_is_right`, `test_oracle_nxdn::test_record_agree_when_the_input_hides_the_dispute` | unit | a | OK |
| Reduction rules: 1/100 minute truncated, whole course and speed, signs from letters or printed sign | nxdn-gps-cases.md:94 | `test_oracle_nxdn::test_parse_gateway_line`, `test_oracle_nxdn::test_parse_dsdfme_log`, `test_oracle_nxdn::test_dsdfme_negative_and_truncation` | unit | a | OK |
| NXG1 data call relay: five NXDND packets reach the other peer byte for byte, no voice stream | nxdn-gps-cases.md:107 | - | - | - | GAP |
| NXG2 NXDNGateway oracle: one APRS line per table row (PARTIAL: manual run, plumbing unit-tested) | nxdn-gps-cases.md:108 | `test_oracle_nxdn::test_gateway_records_from_capture`, `test_oracle_nxdn::test_icom_packet_framing` | unit | b:NXDNGateway | PARTIAL |
| NXG3 dsd-fme oracle: report line per table row (blocked on RF encoder) | nxdn-gps-cases.md:109 | - | - | - | GAP |
| NXG4 CRC: flipped CRC bit and lost middle block are refused by DV-Router | nxdn-gps-cases.md:110 | - | - | - | GAP |
| NXG5 negatives: selective retry, fragment, cipher, 17 blocks, second header, missing TX_REL | nxdn-gps-cases.md:111 | - | - | - | GAP |
| NXG6 SHORT and no-fix reports handled, VERY_SHORT not accepted | nxdn-gps-cases.md:112 | - | - | - | GAP |
| NXG7 pacing of data frames (rate UNVERIFIED) | nxdn-gps-cases.md:113 | - | - | - | GAP |
| Gateway oracle container run: no network, read-only, cap-drop ALL, memory/CPU/pids limits | nxdn-gps-cases.md:150 | `test_oracle_nxdn::test_gateway_docker_argv` | unit | a | OK |
| Sender feeds each case as 5 (FULL) or 4 (SHORT) ICOM packets, one case after the other | nxdn-gps-cases.md:145 | `test_oracle_nxdn::test_gateway_input_lines`, `test_oracle_nxdn::test_split_gateway_capture` | unit | a | OK |
| Packet with a 0x0a byte must reach the gateway as one datagram (entry script writes a file, one cat) | nxdn-gps-cases.md:157 | - | - | - | GAP |
| Oracle table is 87 cases: FULL x3 layouts x17 rows, SHORT x17, 2 no-fix, Icom x17 | nxdn-gps-cases.md:167 | `test_oracle_nxdn::test_gateway_cases_table` | unit | a | OK |
| Gateway prints the encoded values for gateway/lon-both layouts, 68 of 68 (PARTIAL: synthetic lines) | nxdn-gps-cases.md:198 | `test_oracle_nxdn::test_gateway_records_from_capture` | unit | b:NXDNGateway | PARTIAL |
| Gateway reads a dsdfme-layout frame as fraction .00, other course, speed (PARTIAL: synthetic lines) | nxdn-gps-cases.md:204 | `test_oracle_nxdn::test_gateway_records_from_capture`, `test_nxdn_l3::test_the_two_readings_disagree_on_exactly_three_fields` | unit | b:NXDNGateway | PARTIAL |
| GW prints only the whole part of the speed; nibble over 9 not folded into it | nxdn-gps-cases.md:216 | `test_oracle_nxdn::test_gateway_reading_of_speed_nibble_over_9` | unit | a | OK |
| Icom branch rounds the minute fraction, Kenwood truncates, 15 of 17 rows (PARTIAL: synthetic lines) | nxdn-gps-cases.md:221 | `test_oracle_nxdn::test_gateway_records_from_capture` | unit | b:NXDNGateway | PARTIAL |
| ICOM framing bytes 37-39 (23 02 18) accepted by the gateway (PARTIAL: builder only) | nxdn-gps-cases.md:231 | `test_oracle_nxdn::test_icom_packet_framing` | unit | b:NXDNGateway | PARTIAL |
| dsd-fme builds and prints the expected report line (PARTIAL: parser unit only, UNVERIFIED) | nxdn-gps-cases.md:289 | `test_oracle_nxdn::test_parse_dsdfme_log` | unit | a | PARTIAL |
| A CRC-checked call from the generator is accepted by dsd-fme (needs RF input) | nxdn-gps-cases.md:292 | - | - | - | GAP |
| Gateway behaviour with a bad CRC, a lost block or a retry (not run) | nxdn-gps-cases.md:284 | - | - | - | GAP |

## P25
Sources: protocols/p25.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| P-P25-01 Poll 0xF0 + callsign (11 B) links the sender and is echoed byte for byte | protocols/p25.md:141 | `test_p25_network::test_p25_poll_echo` | p25 | a | OK |
| P-P25-02 Server never sends polls of its own to a client (PARTIAL: only sut == ref) | protocols/p25.md:142 | `test_p25_network::test_p25_no_server_polls` | p25 | a | PARTIAL |
| P-P25-03 Poll of another size than 11 B ignored silently, not linked | protocols/p25.md:143 | `test_p25_network::test_p25_poll_wrong_size` | p25 | a | OK |
| P-P25-04 Repeated polls from one address keep one session, traffic delivered once | protocols/p25.md:144 | `test_p25_network::test_p25_repoll_single_session` | p25 | a | OK |
| P-P25-05 Two clients from one IP on different ports are two sessions | protocols/p25.md:145 | `test_p25_network::test_p25_two_ports_two_sessions` | p25 | a | OK |
| P-P25-06 0xF1 from a linked address unlinks it: no reply, no further traffic (PARTIAL: only sut == ref) | protocols/p25.md:146 | `test_p25_network::test_p25_unlink` | p25 | a | PARTIAL |
| P-P25-07 0xF1 from an unlinked address silently ignored (PARTIAL: only sut == ref) | protocols/p25.md:147 | `test_p25_network::test_p25_unlink_unknown` | p25 | a | PARTIAL |
| P-P25-08 Client silent past the timeout dropped (30 s, refreshed by polls and voice) | protocols/p25.md:148 | `test_p25_network::test_p25_silent_client_dropped` | p25+slow | a | OK |
| P-P25-09 A dropped or unlinked client gets no further voice (PARTIAL: only sut == ref) | protocols/p25.md:149 | `test_p25_network::test_p25_dropped_client_no_voice` | p25+slow | a | PARTIAL |
| P-P25-10 Voice records reach every other linked client of the room | protocols/p25.md:155 | `test_p25_network::test_p25_relay_all_listeners` | p25 | a | OK |
| P-P25-11 The talker never receives its own call back | protocols/p25.md:156 | `test_p25_network::test_p25_no_echo` | p25 | a | OK |
| P-P25-12 Records from an address that is not linked are not relayed (PARTIAL: only sut == ref) | protocols/p25.md:157 | `test_p25_network::test_p25_unlinked_sender_dropped` | p25 | a | PARTIAL |
| P-P25-13 One talker per room: second client dropped, not mixed | protocols/p25.md:158 | `test_p25_network::test_p25_second_talker_dropped` | p25 | a | OK |
| P-P25-14 0x80 ends the call, is relayed, frees the floor at once | protocols/p25.md:159 | `test_p25_network::test_p25_terminator_frees_floor` | p25 | a | OK |
| P-P25-15 Lost 0x80: floor freed after silence timeout, terminator sent (PARTIAL: 1.6 s not asserted) | protocols/p25.md:160 | `test_p25_network::test_p25_stream_timeout` | p25 | a | PARTIAL |
| P-P25-16 Voice record whose size does not match its type is not relayed | protocols/p25.md:166 | `test_p25_network::test_p25_record_wrong_size` | p25 | a | OK |
| P-P25-17 Unknown first bytes are not relayed, server keeps serving | protocols/p25.md:167 | `test_p25_network::test_p25_unknown_type_dropped` | p25 | a | OK |
| P-P25-18 Short garbage and oversize datagrams get no reply, links stay (PARTIAL: only sut == ref) | protocols/p25.md:168 | `test_p25_network::test_p25_garbage_ignored` | p25 | a | PARTIAL |
| P-P25-19 Call = records 0x62..0x73 cycling from 0x62, one IMBE per record, no header record | protocols/p25.md:174 | `test_p25_network::test_p25_egress_superframe` | p25 | c | OK |
| P-P25-20 Record sizes and IMBE offsets as in the Motorola V.24 layout | protocols/p25.md:175 | `test_p25_network::test_p25_egress_record_layout` | p25 | c | OK |
| P-P25-21 Record 0x64 LCF 0x00 and MFID 0x00 for a group call | protocols/p25.md:176 | `test_p25_network::test_p25_egress_lcf` | p25 | c | OK |
| P-P25-22 Record 0x65 bytes 1-3 carry the room's talkgroup | protocols/p25.md:177 | `test_p25_network::test_p25_egress_dst_tg` | p25 | c | OK |
| P-P25-23 Record 0x66 bytes 1-3 carry the talker's unit id; unknown talker per policy | protocols/p25.md:178 | `test_p25_network::test_p25_egress_src_id` | p25 | c | OK |
| P-P25-24 One 0x80 after the last voice record, last LDU completed with silence | protocols/p25.md:179 | `test_p25_network::test_p25_egress_terminator_ldu_boundary`, `test_p25::test_digital_to_p25` | p25 | c | OK |
| P-P25-25 No 0x80 outside a call: an empty stream sends nothing | protocols/p25.md:180 | `test_p25_network::test_p25_no_lone_terminator` | p25 | c | OK |
| P-P25-26 A call shorter than one superframe is padded so it reaches 0x73 | protocols/p25.md:181 | `test_p25_network::test_p25_short_call_padded` | p25 | c | OK |
| P-P25-27 Encryption sync: ALGID 0x80 clear, KID 0, MI zero | protocols/p25.md:182 | `test_p25_network::test_p25_egress_clear_es` | p25 | c | OK |
| P-P25-28 LC/ES Reed-Solomon parity and LSD (PARTIAL: asserts today's constant bytes, not real RS parity) | protocols/p25.md:183 | `test_p25_network::test_p25_egress_parity_recorded` | p25 | c | PARTIAL |
| P-P25-29 Egress pacing at voice rate, no long bursts (PARTIAL: only sut == ref on a burst flag) | protocols/p25.md:184 | `test_p25_network::test_p25_egress_pacing` | p25 | c | PARTIAL |
| P-P25-30 Call opens without a header record and loses no frames before 0x66 | protocols/p25.md:190 | `test_p25_network::test_p25_ingress_no_frame_loss` | p25 | c | OK |
| P-P25-31 A call that starts late (not at 0x62) is still carried | protocols/p25.md:191 | `test_p25_network::test_p25_ingress_late_start` | p25 | c | OK |
| P-P25-32 Ingress 0x65 TG differing from the room's TG not used for routing (PARTIAL: only sut == ref) | protocols/p25.md:192 | `test_p25_network::test_p25_ingress_other_tg` | p25 | c | PARTIAL |
| P-P25-33 Source id in 0x66 not in the id database: numeric id kept, cross-mode per policy | protocols/p25.md:193 | `test_p25_network::test_p25_ingress_unknown_src` | p25 | c | OK |
| P-P25-34 Lost records inside a call do not end it, nothing invented on relay | protocols/p25.md:194 | `test_p25_network::test_p25_ingress_record_loss` | p25 | c | OK |
| P-P25-35 Encrypted ingress (ALGID != 0x80 or protected LCF) not transcoded | protocols/p25.md:195 | `test_p25_network::test_p25_ingress_encrypted` | p25 | c | OK |
| P-P25-36 One room per UDP port: room chosen by host:port, never by poll content or 0x65 | protocols/p25.md:201 | `test_p25_network::test_p25_room_per_port` | p25 | a | OK |
| P-P25-37 P25 to and from D-STAR / DMR / M17 / AllStar via IMBE transcoding, no echo to the talker | protocols/p25.md:202 | `test_p25::test_p25_to_digital`, `test_p25::test_digital_to_p25`, `test_p25::test_morse_p25_to_m17`, `test_p25::test_morse_m17_to_p25`, `test_p25::test_p25_to_allstar`, `test_p25::test_allstar_to_p25` | p25/crossmode/allstar | c | OK |

## IPSC (phase 0, preparation)
Sources: ipsc-phase0.md. No IPSC edge, no rig profile and no rig case exist; the only cases are unit tests of the packet layer and a simulated repeater (cases/unit/test_ipsc.py). IPSC has no row in protocols/clients.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Auth digest = first 10 bytes of HMAC-SHA1(key, packet) appended; published vector reproduced | ipsc-phase0.md:50 | `test_ipsc::test_auth_digest_vector`, `test_ipsc::test_auth_rejects`, `test_ipsc::test_voice_with_auth` | unit | a | OK |
| Authenticated master drops every datagram without a valid digest, voice included | ipsc-phase0.md:58 | - | - | - | GAP |
| Link messages 0x90..0x9B have the stated layouts and lengths; strict decode rejects bad lengths | ipsc-phase0.md:71 | `test_ipsc::test_registration_layouts`, `test_ipsc::test_peer_list_layout`, `test_ipsc::test_decode_control_roundtrip`, `test_ipsc::test_decode_rejects_bad_lengths` | unit | a | OK |
| MODE byte (four 2-bit fields), FLAGS word and VERSION field encode and decode as stated | ipsc-phase0.md:90 | `test_ipsc::test_mode_flags_version` | unit | a | OK |
| Peer session: register, peer list, mesh, keep-alive, de-register (PARTIAL: hand-fed replies, stub master) | ipsc-phase0.md:117 | `test_ipsc::test_peer_session_flow`, `test_ipsc::test_peer_session_auth_and_errors`, `test_ipsc::test_ipsc_peer_over_loopback` | unit | a | PARTIAL |
| Voice common header bytes 0-30: opcode, ids, stream id, call type, call info, RTP, burst type | ipsc-phase0.md:164 | `test_ipsc::test_header_layout_by_hand`, `test_ipsc::test_decode_voice_fields` | unit | a | OK |
| RTP marker 0xDD on the first header only, 0x5D after, 0x5E terminator; seq +1, timestamp +480 | ipsc-phase0.md:175 | `test_ipsc::test_header_layout_by_hand` | unit | a | OK |
| Voice header and terminator are 54 B with LC, RS(12,9) parity XOR 0x96 or 0x99, RSSI | ipsc-phase0.md:181 | `test_ipsc::test_header_layout_by_hand`, `test_ipsc::test_decode_voice_rejects` | unit | a | OK |
| RS(12,9) over GF(256) reproduces the four documented captured parities | ipsc-phase0.md:196 | `test_ipsc::test_rs129_documented_captures`, `test_ipsc::test_rs129_properties` | unit | a | OK |
| Voice bursts A..F have the stated sizes, length bytes, control bytes, embedded LC and EMB bytes | ipsc-phase0.md:202 | `test_ipsc::test_burst_layouts` | unit | a | OK |
| AMBE field: 19 bytes, frame a/b/c at bits 0/50/100, bad-frame flags, 72 to 49 round trip | ipsc-phase0.md:220 | `test_ipsc::test_ambe_packing_positions`, `test_ipsc::test_ambe_bad_flag_bits`, `test_ipsc::test_ambe_dmr_roundtrip` | unit | a | OK |
| Timeslot appears twice: bit 5 of byte 17 and bit 7 of byte 30 | ipsc-phase0.md:225 | `test_ipsc::test_burst_layouts` | unit | a | OK |
| Private call (opcode 0x81, FLCO 3) uses the same layout as group voice | ipsc-phase0.md:159 | `test_ipsc::test_private_call_and_ids` | unit | a | OK |
| A call is 3 headers 60 ms apart, bursts A..F every 60 ms, one terminator (PARTIAL: 60 ms spacing not asserted) | ipsc-phase0.md:229 | `test_ipsc::test_peer_session_voice_and_calls`, `test_ipsc::test_ipsc_peer_over_loopback` | unit | a | PARTIAL |
| Other opcodes (0x9E, 0xF0, 0x70, 0x85, ...) are named and returned raw, not processed (PARTIAL: only 0x9E) | ipsc-phase0.md:235 | `test_ipsc::test_peer_session_voice_and_calls` | unit | a | PARTIAL |
| Short 50 B header (8 words) accepted by the decoder (PARTIAL: layout and hardware origin unverified) | ipsc-phase0.md:297 | `test_ipsc::test_short_header_follows_length_to_follow` | unit | a | PARTIAL |
| Simulator voice frames are exact AMBE+2 codewords so 72-bit and 49-bit forms are equivalent | ipsc-phase0.md:335 | `test_ipsc::test_ambe_dmr_roundtrip` | unit | a | OK |
| dmr_utils3 AMBE conversion of the bridge equals nxdn.to_dmr bit for bit (open) | ipsc-phase0.md:340 | - | - | - | GAP |
| IPSC1-01 Register without auth: MASTER_REG_REPLY (16 B), peer list contains the peer | ipsc-phase0.md:359 | - | - | - | GAP |
| IPSC1-02 Register with auth, wrong key gets silence and no state change | ipsc-phase0.md:360 | - | - | - | GAP |
| IPSC1-03 Keep-alive: MASTER_ALIVE_REPLY within 1 s, none for an unregistered id | ipsc-phase0.md:361 | - | - | - | GAP |
| IPSC1-04 Watchdog: silence for the watchdog time ends the Homebrew login, re-register works | ipsc-phase0.md:362 | - | - | - | GAP |
| IPSC1-05 De-registration: DE_REG_REQ answered by DE_REG_REPLY, Homebrew logout | ipsc-phase0.md:363 | - | - | - | GAP |
| IPSC1-06 Group call slot 2: DMRD flags 0xA1, 0x90, 0x81..0x85, 0xA2; constant ids, seq +1 | ipsc-phase0.md:364 | - | - | - | GAP |
| IPSC1-07 Slot 1 call and byte 17 vs byte 30 disagreement: slot follows byte 30 | ipsc-phase0.md:365 | - | - | - | GAP |
| IPSC1-08 Private call (0x81): DMRD bit 0x40 set, LC FLCO 3 | ipsc-phase0.md:366 | - | - | - | GAP |
| IPSC1-09 Late entry: nothing forwarded before burst E, identity from burst E | ipsc-phase0.md:367 | - | - | - | GAP |
| IPSC1-10 No terminator: RTP jump starts a new call, 2 s silence ends the old one | ipsc-phase0.md:368 | - | - | - | GAP |
| IPSC1-11 Sync-undetected placeholder 0x13 forwards nothing, call continues | ipsc-phase0.md:369 | - | - | - | GAP |
| IPSC1-12 Bad AMBE flags make the frame the silence frame | ipsc-phase0.md:370 | - | - | - | GAP |
| IPSC1-13 Homebrew to IPSC: head, 18 voice, terminator at 60 ms, +480 timestamps | ipsc-phase0.md:371 | - | - | - | GAP |
| IPSC1-14 Two simulated peers: peer lists pushed to both, no echo to sender, source lock | ipsc-phase0.md:372 | - | - | - | GAP |
| IPSC1-15 Robustness: truncated, oversize, unknown opcode, 0xF0, 0x70, keep-alive flood, no crash | ipsc-phase0.md:373 | - | - | - | GAP |
| IPSC1-16 Homebrew master gone: calls dropped without a crash, recovery after return | ipsc-phase0.md:374 | - | - | - | GAP |
| H1 Repeater sends 3 voice headers 60 ms apart, marker on first (PARTIAL: bridge variants untested) | ipsc-phase0.md:419 | `test_ipsc::test_header_layout_by_hand` | unit | a | PARTIAL |
| H2 Repeaters ignore keep-alives while transmitting: master must take voice as proof of life | ipsc-phase0.md:420 | - | - | - | GAP |
| H3 Newer firmware changes stream id per superframe with Talker Alias (not simulated) | ipsc-phase0.md:421 | - | - | - | NOT-IMPLEMENTED |
| H4 Later firmware sends opcode 0xF0 after every MASTER_ALIVE_REPLY | ipsc-phase0.md:422 | - | - | - | GAP |
| H7 Sync-not-detected placeholder 0x13 is 34 bytes | ipsc-phase0.md:425 | `test_ipsc::test_sync_undetect_placeholder` | unit | a | OK |
| H8 Version, mode and flags of real devices vary: 3 variants must be built as inputs | ipsc-phase0.md:426 | - | - | - | GAP |
| H10 Peer waits for the peer list: simulator accepts both pushed and requested orders | ipsc-phase0.md:428 | `test_ipsc::test_peer_session_flow`, `test_ipsc::test_peer_session_without_peer_list_request` | unit | a | OK |
| H11 A repeater may need RPT_WAKE_UP (0x85) before keying a call (not simulated) | ipsc-phase0.md:429 | - | - | - | NOT-IMPLEMENTED |
| H12 Repeater wants a continuous 60 ms grid; measure the bridge's gap statistics | ipsc-phase0.md:430 | - | - | - | GAP |
| H16 Mesh voice from several sources for one slot, duplicates and lagging tails | ipsc-phase0.md:434 | - | - | - | GAP |
| H18 Keep-alive and watchdog times and the repeater's retry after a lost master | ipsc-phase0.md:436 | - | - | - | GAP |
| H22 Master answers the peer's own source port (PARTIAL: loopback only, NAT rebind case not written) | ipsc-phase0.md:440 | `test_ipsc::test_ipsc_peer_over_loopback` | unit | a | PARTIAL |

## IAX2 / AllStar
Sources: asl-lab.md, asl-edge-results.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| ASL-LAB link-dns: NEW without and with call token, CALLTOKEN, ACCEPT, ANSWER, ACK, HANGUP | asl-lab.md:118 | `test_asl_lab::test_link_dns_handshake` | asl_lab | b:app_rpt | OK |
| ASL-LAB link-dns: TEXT frames !NEWKEY1!, T CONNECTED, L list, !!DISCONNECT!! | asl-lab.md:118 | `test_asl_lab::test_link_dns_text_frames` | asl_lab | b:app_rpt | OK |
| ASL-LAB link-dns: voice full then mini frames, ulaw 160 B, RADIO_UNKEY (CNG, STOP_SOUND not asserted) | asl-lab.md:118 | `test_asl_lab::test_link_dns_voice_and_control` | asl_lab | b:app_rpt | PARTIAL |
| ASL-LAB link-dns: CNG frame follows RADIO_UNKEY, STOP_SOUND control after ANSWER | asl-lab.md:118 | - | - | - | GAP |
| ASL-LAB link-dns: node number resolved by SRV then A query in the lab zone | asl-lab.md:84 | `test_asl_lab::test_link_dns_lookup_goes_through_the_lab_zone` | asl_lab | b:app_rpt | OK |
| ASL-LAB app_rpt probes DNS per typed digit and never asks the TXT record | asl-lab.md:92 | - | - | - | GAP |
| ASL-LAB audio: 1 kHz tone, sha256 fixed, recorded at far side (>=1.5 s, rms>1000, 0.9 power at 1 kHz) | asl-lab.md:138 | `test_asl_lab::test_link_dns_tone_recorded_at_far_side` | asl_lab | b:app_rpt | OK |
| ASL-LAB callee validates the claimed caller through DNS at the caller address (Rpt without X) | asl-lab.md:97 | `test_asl_lab::test_minimal_new_wrong_address_is_refused`, `test_asl_lab::test_link_dns_handshake` | asl_lab | c | OK |
| ASL-LAB account-md5: AUTHREQ md5 with CHALLENGE, AUTHREP MD5_RESULT (32 hex), ACCEPT, for acct and acct-ct | asl-lab.md:119 | `test_asl_lab::test_account_md5_auth` | asl_lab | b:app_rpt | OK |
| ASL-LAB account-md5: NEW and CALLTOKEN for a user that needs no token and one that does | asl-lab.md:119 | `test_asl_lab::test_call_token_is_offered_even_when_not_required` | asl_lab | c | OK |
| ASL-LAB account-md5: tone from A arrives at E on both accounts | asl-lab.md:119 | `test_asl_lab::test_account_md5_tone_arrives` | asl_lab | b:app_rpt | OK |
| ASL-LAB callee: B links A, E originates to B, L T199901 reaches the edge stand-in | asl-lab.md:120 | `test_asl_lab::test_callee_mode_c` | asl_lab | b:app_rpt | OK |
| ASL-LAB callee: tone from E reaches B and, through B, A (three-node chain) | asl-lab.md:120 | `test_asl_lab::test_callee_tone_reaches_b_and_a` | asl_lab | b:app_rpt | OK |
| ASL-LAB loop: three parallel dials close A-B-E-A, a node listed twice in the tree | asl-lab.md:121 | `test_asl_lab::test_loop_through_three_nodes` | asl_lab | b:app_rpt | OK |
| ASL-LAB legacy-key: RADIO_KEY and RADIO_UNKEY both ways, legacy !NEWKEY!, AUTHREQ/AUTHREP | asl-lab.md:122 | `test_asl_lab::test_legacy_key_unkey_both_ways` | asl_lab | c | OK |
| ASL-LAB legacy-key: E plays the tone to the client and the client tone is recorded at E | asl-lab.md:122 | `test_asl_lab::test_legacy_client_hears_e` | asl_lab | c | OK |
| ASL-LAB legacy-key: client PING and LAGRQ answered by PONG and LAGRP | asl-lab.md:122 | `test_asl_lab::test_ping_pong_lagrq_lagrp_answered` | asl_lab | c | OK |
| ASL-LAB minimal-new: smallest NEW of the ASL1 design (8 IEs + CALLTOKEN) accepted, ANSWER, !NEWKEY1!, L | asl-lab.md:123 | `test_asl_lab::test_minimal_new_is_accepted_and_answered` | asl_lab | c | OK |
| ASL-LAB minimal-new: voice without RADIO_KEY keys the link and is recorded | asl-lab.md:123 | `test_asl_lab::test_minimal_new_voice_without_key_is_heard` | asl_lab | c | OK |
| ASL-LAB minimal-new: same NEW claiming 199901 from the wrong address gets ACCEPT then HANGUP, no ANSWER | asl-lab.md:123 | `test_asl_lab::test_minimal_new_wrong_address_is_refused` | asl_lab | c | OK |
| ASL-LAB registration: REGREQ x3, REGAUTH, REGACK (refresh 180), REGREJ for a wrong secret | asl-lab.md:124 | `test_asl_lab::test_registration_ok_and_rejected` | asl_lab | b:app_rpt | OK |
| ASL-LAB dns-zone: SRV, A (TTL<=60), TXT of a node, NXDOMAIN for an unknown node | asl-lab.md:125 | `test_asl_lab::test_dns_zone_shape` | asl_lab | a | OK |
| ASL-LAB long-call (DVT_ASL_LONG=1): mini frame ts16 wraps 0xFFF0 to 0x0004 in a 75 s call | asl-lab.md:126 | `test_asl_lab::test_long_call_timestamp_wrap_ping_lagrq` | asl_lab | b:app_rpt | OK |
| ASL-LAB long-call: natural PING/PONG every 21 s and LAGRQ/LAGRP every 10 s (counts only, intervals not asserted) | asl-lab.md:151 | `test_asl_lab::test_long_call_timestamp_wrap_ping_lagrq` | asl_lab | b:app_rpt | PARTIAL |
| ASL-LAB no new full voice frame at the ts16 wrap (observed, not asserted) | asl-lab.md:148 | - | - | - | GAP |
| ASL-LAB answer 1: ulaw only, 160 B per 20 ms, first voice frame full then mini | asl-lab.md:413 | `test_asl_lab::test_link_dns_voice_and_control` | asl_lab | b:app_rpt | OK |
| ASL-LAB answer 2: !NEWKEY1! timing, L every linkpost_time, texts end with NUL (presence only, no timing/order) | asl-lab.md:414 | `test_asl_lab::test_link_dns_text_frames`, `test_asl_lab::test_callee_mode_c` | asl_lab | b:app_rpt | PARTIAL |
| ASL-LAB answer 3: CALLTOKEN frame both ways, Asterisk takes a NEW without token IE and the smallest NEW | asl-lab.md:415 | `test_asl_lab::test_link_dns_handshake`, `test_asl_lab::test_call_token_is_offered_even_when_not_required`, `test_asl_lab::test_minimal_new_is_accepted_and_answered` | asl_lab | c | OK |
| ASL-LAB answer 4: no RADIO_KEY in new mode, RADIO_UNKEY plus CNG (CNG, 2 s legacy refresh not asserted) | asl-lab.md:416 | `test_asl_lab::test_minimal_new_voice_without_key_is_heard`, `test_asl_lab::test_link_dns_voice_and_control` | asl_lab | c | PARTIAL |
| ASL-LAB answer 5: LAGRQ every 10 s and PING every 21 s from each end (counts only) | asl-lab.md:417 | `test_asl_lab::test_long_call_timestamp_wrap_ping_lagrq` | asl_lab | b:app_rpt | PARTIAL |
| ASL-LAB answer 6: IAX2 phone client works with account and no registrar (no REGREQ, RADIO_KEY, UNKEY) | asl-lab.md:418 | `test_asl_lab::test_legacy_key_unkey_both_ways` | asl_lab | c | OK |
| ASL-LAB surprise 3: text frames carry the NUL, !!DISCONNECT!! is exactly 15 B | asl-lab.md:443 | - | - | - | GAP |
| ASL-LAB surprise 4: app_rpt NEW IE order, AUTHREQ 9-digit CHALLENGE, PONG RR_* IEs, REGACK APPARENT_ADDR 16 B | asl-lab.md:448 | `test_asl_lab::test_account_md5_auth`, `test_asl_lab::test_registration_ok_and_rejected` | asl_lab | b:app_rpt | PARTIAL |
| ASL-LAB surprise 7: no retransmission and no VNAK in any scenario | asl-lab.md:466 | - | - | - | GAP |
| ASL-LAB surprise 11: inbound acct via channel originate has an empty calling number (L list T) | asl-lab.md:479 | - | - | - | GAP |
| ASL-LAB lab node numbers private and renumbered (no public node below 2000, tables match lab nodes) | asl-lab.md:295 | `test_asl_lab_node_numbers::test_no_public_lab_node_below_2000`, `test_asl_lab_node_numbers::test_lab_edge_and_registration_nodes_are_the_renumbered_ones`, `test_asl_lab_node_numbers::test_expectation_tables_name_exactly_the_lab_nodes` | unit | a | OK |
| ASL-LAB registrar variants map the eight nodes one to one (ok, port, 500, notext, slow, oversize, 429ra, 503bad) | asl-lab.md:298 | `test_asl_lab_node_numbers::test_registrar_variants_map_the_eight_nodes_one_to_one`, `test_asl_registrar::test_http500_notext_oversize`, `test_asl_registrar::test_rate_limit_variants_send_the_status_and_the_retry_after_header` | unit | a | OK |
| ASL-LAB registrar bare 429/503 without Retry-After exists but no lab node uses it (rule b without header) | asl-lab.md:301 | `test_asl_registrar::test_bare_429_and_503_have_no_retry_after` | unit | a | PARTIAL |
| ASL-LAB retry rule (a): ordinary failure waits 120 s * 2^(n-1), 80..120 %, clamp 120..900 s (n=1,2 only observable) | asl-lab.md:310 | `test_asl_edge::test_r3_http_500_is_retried_with_backoff`, `test_asl_edge::test_r4_success_without_the_text_is_a_failure`, `test_asl_edge::test_r5_slow_reply_times_out_on_the_client`, `test_asl_edge::test_r6_oversize_reply_is_refused` | asl_lab | a | PARTIAL |
| ASL-LAB retry rule (b): 429 or 503 with Retry-After uses that value, bounded 120..86400 s, no jitter | asl-lab.md:311 | `test_asl_edge::test_r14_rate_limited_follows_retry_after_or_doubles_the_interval` | asl_lab | a | PARTIAL |
| ASL-LAB retry rule (b): no usable Retry-After doubles the interval per failure (n=1,2 only) | asl-lab.md:312 | `test_asl_edge::test_r14_rate_limited_follows_retry_after_or_doubles_the_interval` | asl_lab | a | PARTIAL |
| ASL-LAB retry rule (c): success and Mismatch refresh at max(120, min(interval, refresh)) | asl-lab.md:313 | `test_asl_edge::test_r1_success_registers_and_refreshes`, `test_asl_edge::test_r2_other_port_is_a_mismatch` | asl_lab | a | OK |
| ASL-LAB delay is counted from completion of the failed attempt (gap = delay + attempt duration) | asl-lab.md:317 | `test_asl_edge::test_r5_slow_reply_times_out_on_the_client`, `test_asl_register::test_retry_gap_range_widens_by_the_attempt_duration` | asl_lab | a | PARTIAL |
| ASL-LAB registration schedule is per edge process: node k first registers at start + k * S, S = max(I,60)/N | asl-lab.md:328 | `test_asl_edge::test_r1_success_registers_and_refreshes` | asl_lab | a | OK |
| ASL-LAB registration helpers: slots, gaps, limiter, trimming to the last process, window budget (harness arithmetic) | asl-lab.md:334 | `test_asl_register::test_slot_spacing_is_the_interval_over_the_node_count`, `test_asl_register::test_failure_delay_range_is_120_times_2_to_n_minus_1_with_80_120_percent_and_the_clamps`, `test_asl_register::test_rate_limited_delay_uses_retry_after_or_doubles_the_interval`, `test_asl_register::test_trim_keeps_the_last_process_in_time_order`, `test_asl_register::test_limiter_admits_the_first_failure_and_then_one_per_300_s` | unit | a | OK |
| ASL-LAB fixture fails when the edge restarted during the registration window | asl-lab.md:340 | `test_asl_edge::test_r1_success_registers_and_refreshes` | asl_lab | a | PARTIAL |
| ASL-LAB R13: one failure log line per admitted attempt (300 s limiter), names node and class, delay inside rule | asl-lab.md:364 | `test_asl_edge::test_r13_failure_log_lines_and_no_secret_in_the_registration_logging`, `test_asl_reg_log::test_judge_node_models_the_300_s_limiter_for_the_second_retry` | asl_lab | a | PARTIAL |
| ASL-LAB R13: no registration log line holds a password, password file, directory or URL | asl-lab.md:369 | `test_asl_edge::test_r13_failure_log_lines_and_no_secret_in_the_registration_logging`, `test_asl_reg_log::test_leak_problems_finds_each_kind_but_whitelists_only_the_lab_warning` | asl_lab | a | PARTIAL |
| ASL-LAB T5n: caller behind a source NAT (address mode refused ACCEPT+HANGUP, port mode answered) | asl-lab.md:255 | `test_asl_edge::test_t5n_caller_behind_a_nat_is_refused_before_answer_and_right_address_answered` | asl_lab | b:app_rpt | PARTIAL |
| ASL-LAB T5n sidecar: SNAT rule, control protocol, edge_reaction judge, compose and run.sh lint | asl-lab.md:273 | `test_asl_nat::test_mode_commands_one_snat_rule_for_the_iax_socket_towards_the_edge_only`, `test_asl_nat::test_edge_reaction_refusal_is_accept_then_hangup_without_answer`, `test_asl_nat::test_set_mode_applies_the_rule_then_deletes_the_udp_conntrack_entries`, `test_asl_nat::test_run_sh_adds_the_profile_builds_and_starts_the_sidecar_only_with_the_flag` | unit | a | OK |
| ASL-LAB T5 with a routed NAT (second network and router between node and edge) | asl-lab.md:263 | - | - | - | GAP |
| ASL-LAB T11s: spoofed NEW/POKE from random sources at 10k pkt/s (not built) | asl-lab.md:282 | - | - | - | GAP |
| ASL-LAB T11m: many real source addresses, sustained flood (not built) | asl-lab.md:283 | - | - | - | GAP |
| ASL-LAB edge global and per-bucket caps (design 7.3/7.4) covered on the product side only | asl-lab.md:284 | - | - | - | GAP |
| ASL-LAB asl-ext: node 400169 registers with the real registry (outbound only), dry run against another name | asl-lab.md:376 | `test_asl_ext::test_external_dial` | asl_ext | b:app_rpt | PARTIAL |
| ASL-LAB asl-ext helper parsers (registrations table, lstats, calls, text, audio, DNS) | asl-lab.md:381 | `test_asl_ext_helpers::test_parse_registrations`, `test_asl_ext_helpers::test_lstats_state`, `test_asl_ext_helpers::test_calls_text_audio_dns` | unit | a | OK |
| ASL-LAB asl-ext password never appears in out/ (secretscan after the run) | asl-lab.md:380 | `test_secretscan::test_main_exit_codes_and_no_value_in_output`, `test_secretscan::test_scan_root_finds_across_chunk_boundary` | unit | a | PARTIAL |
| ASL-LAB iax2_frames tool: every datagram decoded with hex, per type, summary, ts16 wraps, DNS | asl-lab.md:388 | `test_iax2_frames::test_new_frame_is_decoded_with_its_hex`, `test_iax2_frames::test_control_text_cng_and_full_voice`, `test_iax2_frames::test_mini_frames_and_ts16_wrap`, `test_iax2_frames::test_dns_srv_a_txt_answers`, `test_iax2_frames::test_meta_frame_and_truncated_ie` | unit | a | OK |
| ASL-LAB iax2_frames --limit keeps wrap frames, --anon rewrites addresses to 192.0.2.x | asl-lab.md:393 | `test_iax2_frames::test_limit_keeps_wrap_frames_but_counts_all`, `test_iax2_frames::test_anon_rewrites_addresses_in_first_seen_order` | unit | a | OK |
| ASL-LAB tone generator deterministic ulaw (3 s, 24000 B, same sha256) | asl-lab.md:131 | `test_iax2_frames::test_tone_is_deterministic_ulaw` | unit | a | OK |
| ASL-LAB own IAX2 client wire format (full header, retransmit bit, IE lengths, auth retry, bounded retransmission) | asl-lab.md:122 | `test_iax2::test_full_header_and_retransmit_bit`, `test_iax2::test_information_lengths`, `test_iax2::test_authentication_after_lost_new_and_duplicate_challenge`, `test_iax2::test_unresponsive_peer_has_bounded_retransmission`, `test_iax2::test_signed_network_pcm_samples`, `test_iax2::test_allstar_redundant_radio_handshake` | unit | a | OK |
| ASL-LAB existing allstar profile: IAX2 audio in reaches M17, D-STAR and DMR listeners, no echo to caller | asl-lab.md:461 | `test_allstar::test_iax_to_all_digital_listeners` | allstar | c | OK |
| ASL-LAB existing allstar profile: digital talk reaches the IAX2 caller with key/unkey | asl-lab.md:461 | `test_allstar::test_digital_to_iax` | allstar | c | OK |
| ASL-LAB existing allstar profile: two rooms carry their own tone (no crosstalk) | asl-lab.md:461 | `test_allstar::test_rooms_no_crosstalk` | allstar | c | OK |
| ASL-LAB existing allstar profile: nothing echoed to the originating USRP peer | asl-lab.md:461 | `test_allstar::test_usrp_no_echo` | allstar | c | OK |
| ASL-LAB existing allstar profile: killing the node while keyed releases the module | asl-lab.md:461 | `test_allstar::test_node_crash_releases_module` | allstar | c | OK |
| ASL-LAB existing allstar profile: AllStar audio reaches an NXDN or P25 listener | asl-lab.md:461 | `test_nxdn::test_allstar_to_nxdn`, `test_p25::test_allstar_to_p25` | nxdn/p25 | c | PARTIAL |
| ASL-EDGE T1: node A links the edge, 10 min hold, audio edge to client at start and end, PING/PONG | asl-edge-results.md:71 | `test_asl_edge::test_t1_ten_minute_hold_audio_both_ways` | asl_lab | c | OK |
| ASL-EDGE T1b: client keys, edge relays the over to node A | asl-edge-results.md:72 | `test_asl_edge::test_t1b_client_to_node_audio` | asl_lab | c | OK |
| ASL-EDGE T2: one 190 s over, room TOT cuts audio at about 180 s | asl-edge-results.md:73 | `test_asl_edge::test_t2_three_minute_over_tot` | asl_lab | c | OK |
| ASL-EDGE T3: hub of A, B and E, per-destination L lists (split horizon, T/R letters) | asl-edge-results.md:74 | `test_asl_edge::test_t3_hub_l_lists_split_horizon` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T4: A, B dial the edge and A dials B, no triangle forms (app_rpt refuses, edge guard not exercised) | asl-edge-results.md:75 | `test_asl_edge::test_t4_loop_a_b_edge_no_loop_forms` | asl_lab | b:app_rpt | PARTIAL |
| ASL-EDGE T4b: loop through the edge is broken, edge drops one branch (variant OUT unset) | asl-edge-results.md:76 | `test_asl_edge::test_t4b_loop_through_the_edge_is_broken_by_the_edge` | asl_lab | c | PARTIAL |
| ASL-EDGE loop guard: edge drops the YOUNGEST branch with DISCONNECT then HANGUP (only some HANGUP asserted) | asl-edge-results.md:120 | `test_asl_edge::test_t4b_loop_through_the_edge_is_broken_by_the_edge` | asl_lab | c | PARTIAL |
| ASL-EDGE loop guard: 60 s hold-down refuses the dropped branch retry (NEW, ACCEPT, HANGUP) | asl-edge-results.md:122 | - | - | - | GAP |
| ASL-EDGE loop guard: no audio forwarded twice after the drop | asl-edge-results.md:124 | `test_asl_edge::test_t4b_loop_through_the_edge_is_broken_by_the_edge` | asl_lab | c | OK |
| ASL-EDGE T5: wrong source address for a static node gets ACCEPT then HANGUP, never ANSWER | asl-edge-results.md:77 | `test_asl_edge::test_t5_wrong_source_ip_is_refused_before_answer` | asl_lab | a | OK |
| ASL-EDGE T5: right source address of the static node is answered | asl-edge-results.md:77 | `test_asl_edge::test_t5_right_source_ip_is_accepted` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T5n: caller behind a NAT refused before answer, right address (port changed) answered (never run) | asl-edge-results.md:78 | `test_asl_edge::test_t5n_caller_behind_a_nat_is_refused_before_answer_and_right_address_answered` | asl_lab | b:app_rpt | PARTIAL |
| ASL-EDGE T6: outbound rows (transceive to B, monitor to E) established after an edge restart | asl-edge-results.md:79 | `test_asl_edge::test_t6_outbound_rows_reach_the_lab_nodes` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T6m: monitor row is listen only, no voice and no RADIO_KEY to E | asl-edge-results.md:80 | `test_asl_edge::test_t6_monitor_is_listen_only` | asl_lab | c | OK |
| ASL-EDGE T7: edge restart (SIGUSR1) under A's link, the link comes back | asl-edge-results.md:81 | `test_asl_edge::test_t7_edge_restart_recovers_permanent_link` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T8: asl-b container restart, the edge redials B | asl-edge-results.md:82 | `test_asl_edge::test_t8_lab_node_restart_recovers_edge_outbound` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T8b: permanent row redials after a plain HANGUP | asl-edge-results.md:83 | `test_asl_edge::test_t8b_edge_redials_after_hangup_but_never_after_disconnect` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T8b: permanent row never redials after a received !!DISCONNECT!! (70 s watch) | asl-edge-results.md:115 | `test_asl_edge::test_t8b_edge_redials_after_hangup_but_never_after_disconnect` | asl_lab | b:app_rpt | OK |
| ASL-EDGE T9: 80 ms jitter and 5 % loss on edge egress, link and tone survive, retransmissions bounded | asl-edge-results.md:84 | `test_asl_edge::test_t9_lossy_jittery_link_still_establishes_and_carries_audio` | asl_lab | c | OK |
| ASL-EDGE T10: client account acct, md5 AUTHREQ/AUTHREP, RADIO_KEY, no REGREQ | asl-edge-results.md:85 | `test_asl_edge::test_t10_client_account_no_registration_needed` | asl_lab | a | OK |
| ASL-EDGE T11: 60 NEW and 20 POKE from one address, same pid, cpu bounded, real node still links (per-IP caps not asserted) | asl-edge-results.md:86 | `test_asl_edge::test_t11_new_poke_abuse_is_bounded_and_does_not_block_real_links` | asl_lab | c | PARTIAL |
| ASL-EDGE back-to-back: three overs 1 s apart each reach the client (no over cut after grant) | asl-edge-results.md:87 | `test_asl_edge::test_back_to_back_overs_are_not_cut` | asl_lab | c | OK |
| ASL-EDGE restart x5: 5 SIGUSR1 restarts, one over after each heard at the first attempt | asl-edge-results.md:88 | `test_asl_edge::test_restart_then_over_five_times` | asl_lab | c | OK |
| ASL-EDGE R1: registration request shape, one node per request, refresh gap, spread slots | asl-edge-results.md:89 | `test_asl_edge::test_r1_success_registers_and_refreshes` | asl_lab | a | OK |
| ASL-EDGE R2: success with another port counted as mismatch, refresh cadence kept | asl-edge-results.md:90 | `test_asl_edge::test_r2_other_port_is_a_mismatch` | asl_lab | a | OK |
| ASL-EDGE R3: HTTP 500 retried with backoff (first two retries; new windows not yet run on a rig) | asl-edge-results.md:91 | `test_asl_edge::test_r3_http_500_is_retried_with_backoff` | asl_lab | a | PARTIAL |
| ASL-EDGE R4: 200 without the success text is a failure, retried (first two retries) | asl-edge-results.md:92 | `test_asl_edge::test_r4_success_without_the_text_is_a_failure` | asl_lab | a | PARTIAL |
| ASL-EDGE R5: reply after 20 s times out on the client at 15 s, then backoff (first two retries) | asl-edge-results.md:93 | `test_asl_edge::test_r5_slow_reply_times_out_on_the_client` | asl_lab | a | PARTIAL |
| ASL-EDGE R6: reply over 4 KiB refused, then backoff (first two retries) | asl-edge-results.md:94 | `test_asl_edge::test_r6_oversize_reply_is_refused` | asl_lab | a | PARTIAL |
| ASL-EDGE R7: dv_edge_allstar_register_total counts ok, mismatch, http/parse, timeout, no unattributed | asl-edge-results.md:95 | `test_asl_edge::test_r7_metrics_count_every_outcome` | asl_lab | a | OK |
| ASL-EDGE R14: 429 + Retry-After 300 retried at 300 s, 503 + invalid Retry-After doubles interval (never run on a rig) | asl-edge-results.md:30 | `test_asl_edge::test_r14_rate_limited_follows_retry_after_or_doubles_the_interval` | asl_lab | a | PARTIAL |
| ASL-EDGE DVSwitch Mobile 2.0.7 radio mode: real app connects, keys, hangs up, edge relays to A (by hand) | asl-edge-results.md:96 | `test_dvswitch_phone::test_app_registration_order_is_what_the_app_sends` | unit | b:DVSwitch Mobile | PARTIAL |
| ASL-EDGE R8: phone registration REGREQ, CALLTOKEN, REGAUTH md5, REGACK, REFRESH 60..300, counters | asl-edge-results.md:311 | `test_asl_edge::test_r8_phone_registration_md5_regack`, `test_iax2_reg::test_registration_walks_regreq_calltoken_regauth_regack` | asl_lab | a | OK |
| ASL-EDGE R9: wrong secret gets REGREJ (CAUSE authentication failed, CAUSECODE 29), no REGACK | asl-edge-results.md:312 | `test_asl_edge::test_r9_wrong_secret_is_regrej`, `test_iax2_reg::test_wrong_secret_is_regrej_with_cause` | asl_lab | a | OK |
| ASL-EDGE R10: unknown user indistinguishable from a wrong secret (same steps, cause, REGAUTH IEs) | asl-edge-results.md:313 | `test_asl_edge::test_r10_unknown_user_is_indistinguishable_from_a_wrong_secret` | asl_lab | a | OK |
| ASL-EDGE R11: REGREQ without a CALLTOKEN IE gets no reply of any kind | asl-edge-results.md:314 | `test_asl_edge::test_r11_regreq_without_calltoken_is_dropped`, `test_iax2_reg::test_no_calltoken_ie_is_silence` | asl_lab | a | OK |
| ASL-EDGE R12: node account 2999 cannot register as a phone (REGREJ) | asl-edge-results.md:315 | `test_asl_edge::test_r12_node_account_cannot_register_as_a_phone` | asl_lab | a | OK |
| ASL-EDGE phone registration client wire (MD5_RESULT, USERNAME/REFRESH IEs, silent peer times out) | asl-edge-results.md:305 | `test_iax2_reg::test_md5_result_is_hex_md5_of_challenge_and_secret`, `test_iax2_reg::test_request_elements_carry_username_refresh_and_one_proof`, `test_iax2_reg::test_silent_peer_times_out` | unit | a | OK |
| ASL-EDGE ASL1h log lines allstar_call and allstar_client_registration counted, no secret (post-run tool only) | asl-edge-results.md:317 | `test_asl_reg_log::test_scan_counts_classes_and_finds_leaks`, `test_asl_reg_log::test_main_exit_codes` | unit | a | PARTIAL |
| ASL-EDGE DNS lookup of nodes through the lab zone by the edge without static rows | asl-edge-results.md:172 | - | - | - | GAP |
| ASL-EDGE T5 with a routed NAT (blocked) | asl-edge-results.md:171 | - | - | - | GAP |
| ASL-EDGE T11 with real source-address spoofing (not built) | asl-edge-results.md:171 | - | - | - | GAP |
| ASL-EXT T12: node 400169 registers with the real registry and is listed (Registered, DNS agrees) | asl-edge-results.md:204 | `test_asl_ext::test_external_dial` | asl_ext | b:app_rpt | PARTIAL |
| ASL-EXT T12: dials 400161 and 400168 listen-only, link ESTABLISHED for 60 s each | asl-edge-results.md:208 | `test_asl_ext::test_external_dial` | asl_ext | b:app_rpt | PARTIAL |
| ASL-EXT T12: !NEWKEY1! both ways, L every 30 s, !!DISCONNECT!! at the end (recorded in summary.json only) | asl-edge-results.md:231 | `test_asl_ext::test_external_dial` | asl_ext | b:app_rpt | PARTIAL |
| ASL-EXT T12: no voice, CNG or RADIO_KEY arrives from the far node (recorded rx_bytes, not asserted) | asl-edge-results.md:240 | - | - | - | GAP |

## SvxReflector (SVX)
Sources: svx-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Layout: TCP frame is frame_len:u32, type:u16, body (frame_len = 2 + body) | svx-cases.md:100 | `test_svx_proto::test_proto_ver_3_0_is_the_hand_derived_example_of_the_research_note`, `test_svx_proto::test_splitter_handles_coalesced_frames` | unit | a | OK |
| Layout: ProtoVer, ProtoVerDowngrade, Heartbeat, AuthChallenge (20 B), AuthResponse (HMAC-SHA1), Error bodies | svx-cases.md:109 | `test_svx_proto::test_proto_ver_2_0_and_downgrade`, `test_svx_proto::test_heartbeat_and_empty_messages`, `test_svx_proto::test_auth_challenge_is_a_byte_vector_of_20`, `test_svx_proto::test_auth_response_callsign_then_digest`, `test_svx_proto::test_hmac_matches_two_independent_references`, `test_svx_proto::test_error_carries_one_string` | unit | a | OK |
| Layout: ServerInfo, NodeJoined/Left, TalkerStart/Stop, SelectTG, TgMonitor (sorted set) bodies | svx-cases.md:118 | `test_svx_proto::test_server_info_reserved_client_id_nodes_codecs`, `test_svx_proto::test_talker_start_and_stop_carry_tg_before_callsign`, `test_svx_proto::test_select_tg_and_monitor_set_is_sorted_and_unique` | unit | a | OK |
| Layout: NodeInfo v3 (iv_rand, key, json) versus v2 (json), CAInfo u16 size, SignalStrength/TxStatus bits | svx-cases.md:127 | `test_svx_proto::test_node_info_v3_and_v2_differ`, `test_svx_proto::test_decode_ca_info_has_a_u16_size`, `test_svx_proto::test_decode_signal_strength_and_tx_status_bits` | unit | a | OK |
| Layout: decoder depends on protocol version (talker, node info), rejects short and trailing bytes, keeps unknown types | svx-cases.md:121 | `test_svx_proto::test_decode_round_trips_the_builders`, `test_svx_proto::test_decode_talker_depends_on_the_protocol_version`, `test_svx_proto::test_decode_rejects_short_and_trailing_bytes`, `test_svx_proto::test_unknown_type_is_kept_raw_because_peers_ignore_it` | unit | a | OK |
| Layout: framing limit applies at the length word, fragments and coalesced frames split correctly | svx-cases.md:100 | `test_svx_proto::test_splitter_handles_one_byte_fragments`, `test_svx_proto::test_splitter_limit_applies_to_frame_len_at_the_length_word`, `test_svx_proto::test_splitter_rejects_frames_too_short_for_a_type` | unit | a | OK |
| Layout: UDP v2 header type:u16 client_id:u16 seq:u16, Heartbeat 1, Audio 101, Flush 102, signal strength 104 | svx-cases.md:136 | `test_svx_proto::test_udp_v2_header_is_type_client_seq`, `test_svx_proto::test_udp_v2_decode_and_audio_body`, `test_svx_proto::test_udp_signal_strength_body` | unit | a | OK |
| Layout: UDP v3 datagram AAD then tag[8] then ciphertext, 6 B AAD for the registration datagram, IV layout | svx-cases.md:138 | `test_svx_proto::test_udp_v3_registration_datagram_has_a_6_byte_aad`, `test_svx_proto::test_udp_v3_ordinary_datagram_has_a_4_byte_aad_and_tag_before_ciphertext`, `test_svx_proto::test_v3_iv_and_plaintext_layout` | unit | a | OK |
| Layout: v3 UDP counters and reject rules (counter below expected dropped, gap accepted) | svx-cases.md:141 | - | - | - | NOT-IMPLEMENTED |
| SVXE-1 login protocol 2.0: challenge (20 B), AuthOk, ServerInfo in that order with nothing between | svx-cases.md:283 | - | - | - | GAP |
| SVXE-1 UDP Heartbeat from a new socket answered with the same client id and seq 0 within 1 s | svx-cases.md:289 | - | - | - | GAP |
| SVXE-1 two logins give different challenges; digest accepted only for the right key | svx-cases.md:292 | - | - | - | GAP |
| SVXE-2 login protocol 3.0 with TLS, CSR, CAInfo and fallback password, UDP AES-GCM register (level N, real svx node) | svx-cases.md:309 | - | - | - | NOT-IMPLEMENTED |
| SVXE-2 server certificate without the dialled address in the SAN: node disconnects, no AuthResponse | svx-cases.md:322 | - | - | - | NOT-IMPLEMENTED |
| SVXE-2 ClientCsr with a CN that is not an accepted callsign gets Error with the reason | svx-cases.md:323 | - | - | - | NOT-IMPLEMENTED |
| SVXE-3a wrong key: Error "Access denied", no AuthOk or ServerInfo | svx-cases.md:337 | - | - | - | GAP |
| SVXE-3b unknown user: Error "Access denied" (same bytes as 3a) | svx-cases.md:338 | - | - | - | GAP |
| SVXE-3c invalid callsign (lowercase): Error "Invalid callsign" | svx-cases.md:339 | - | - | - | GAP |
| SVXE-3d duplicate login of a connected callsign: Error "Access denied", first connection undisturbed | svx-cases.md:340 | - | - | - | GAP |
| SVXE-3e AuthResponse as first frame: Error "Authentication response unexpected" | svx-cases.md:341 | - | - | - | GAP |
| SVXE-3f ProtoVer twice: Error "Protocol version expected" | svx-cases.md:342 | - | - | - | GAP |
| SVXE-3g truncated AuthResponse: Error "Illegal MsgAuthResponse protocol message received" | svx-cases.md:343 | - | - | - | GAP |
| SVXE-3h user message before login ignored, no Error, later login works | svx-cases.md:344 | - | - | - | GAP |
| SVXE-4a ProtoVer 4.0 gets ProtoVerDowngrade 3.0, connection stays open | svx-cases.md:354 | - | - | - | GAP |
| SVXE-4b ProtoVer 3.1 gets the same Downgrade bytes | svx-cases.md:355 | - | - | - | GAP |
| SVXE-4c retry after Downgrade continues as SVXE-2, 4.0 again gets Downgrade again | svx-cases.md:356 | - | - | - | GAP |
| SVXE-4d ProtoVer 0.5: Error "Unsupported protocol version 0.5. Must be at least 0.6." | svx-cases.md:357 | - | - | - | GAP |
| SVXE-4e ProtoVer 0.0: Error "Unsupported protocol version 0.0. Must be at least 0.6." | svx-cases.md:358 | - | - | - | GAP |
| SVXE-4f ProtoVer 2.0 gets AuthChallenge | svx-cases.md:359 | - | - | - | GAP |
| SVXE-4g ProtoVer 1.0: node in TG 999, no NodeList, one-field TalkerStart for TG 999 (D) | svx-cases.md:360 | - | - | - | GAP |
| SVXE-4h ProtoVer 0.6: additionally NodeList (101) after ServerInfo (D) | svx-cases.md:361 | - | - | - | GAP |
| SVXE-4i ProtoVer with a 2 byte body: Error "Illegal MsgProtoVer protocol message received" | svx-cases.md:362 | - | - | - | GAP |
| SVXE-5 ServerInfo.nodes lists logged-in nodes (A: itself, B: both), unauthenticated C appears nowhere | svx-cases.md:371 | - | - | - | GAP |
| SVXE-5 NodeJoined to A after B's AuthOk, NodeLeft within 2 s of B's close, none for B itself | svx-cases.md:373 | - | - | - | GAP |
| SVXE-5 no NodeList (101) at any time for protocol 2.0 clients | svx-cases.md:376 | - | - | - | GAP |
| SVXE-5 HTTP /status lists nodes with protoVer 2.0 (reference extra, D) | svx-cases.md:377 | - | - | - | GAP |
| SVXE-6.1 SelectTG gets no reply | svx-cases.md:392 | - | - | - | GAP |
| SVXE-6.2 Audio from A reaches B in the same TG byte-equal, not C or A; TalkerStart(111) to A and B | svx-cases.md:393 | - | - | - | GAP |
| SVXE-6.3 TgMonitor {111}: monitor gets TalkerStart/Stop over TCP but never UDP Audio | svx-cases.md:394 | - | - | - | GAP |
| SVXE-6.4 TgMonitor with a restricted TG: set accepted silently, no TalkerStart for the restricted TG | svx-cases.md:395 | - | - | - | GAP |
| SVXE-6.5 node in TG 0 receives nothing and its own Audio is ignored | svx-cases.md:396 | - | - | - | GAP |
| SVXE-6.6 restricted TG 900: SelectTG silently ignored, forwarded to members only | svx-cases.md:397 | - | - | - | GAP |
| SVXE-6.7 member leaving a TG while another talks receives UDP FlushSamples (timing D) | svx-cases.md:398 | - | - | - | GAP |
| SVXE-7.1 first Audio: TalkerStart within 100 ms at all members, Audio forwarded unchanged, never back to the talker | svx-cases.md:411 | - | - | - | GAP |
| SVXE-7.2 second talker in the same TG: audio reaches nobody, no TalkerStart, no Error | svx-cases.md:412 | - | - | - | GAP |
| SVXE-7.3 FlushSamples: AllSamplesFlushed to the talker, TalkerStop to all, UDP Flush to others | svx-cases.md:413 | - | - | - | GAP |
| SVXE-7.4 after the flush the other node's Audio takes the floor with TalkerStart | svx-cases.md:414 | - | - | - | GAP |
| SVXE-7.5 empty-payload Audio while idle never claims the floor | svx-cases.md:415 | - | - | - | GAP |
| SVXE-7.6 a talker in another TG is independent | svx-cases.md:416 | - | - | - | GAP |
| SVXE-7.7 talker silence without Flush: TalkerStop and UDP Flush 4.0 to 5.5 s after last audio | svx-cases.md:417 | - | - | - | GAP |
| SVXE-7.8 talker closes TCP: TalkerStop then NodeLeft at the others | svx-cases.md:418 | - | - | - | GAP |
| SVXE-7.9 simultaneous first Audio (20 trials): exactly one TalkerStart, loser never forwarded | svx-cases.md:419 | - | - | - | GAP |
| SVXE-7 level N variant with real svx-a and svx-b nodes (tone in, tone out) | svx-cases.md:407 | - | - | - | NOT-IMPLEMENTED |
| SVXE-7 SQL_TIMEOUT variant: TalkerStop at about 5 s, node blocked 60 s after its last audio | svx-cases.md:426 | - | - | - | GAP |
| SVXE-8a talker flush: AllSamplesFlushed within 100 ms, members get Flush and TalkerStop, floor free at once | svx-cases.md:434 | - | - | - | GAP |
| SVXE-8b non-talker flush: AllSamplesFlushed to sender only, talker unchanged | svx-cases.md:435 | - | - | - | GAP |
| SVXE-8c flush in TG 0: AllSamplesFlushed only | svx-cases.md:436 | - | - | - | GAP |
| SVXE-8d flush without audio: AllSamplesFlushed only, no TalkerStart/Stop | svx-cases.md:437 | - | - | - | GAP |
| SVXE-8e leave while talking: members get UDP Flush, TalkerStop recorded (open question) | svx-cases.md:438 | - | - | - | GAP |
| SVXE-8f AllSamplesFlushed does not wait for a node that never reads its socket | svx-cases.md:439 | - | - | - | GAP |
| SVXE-9a silent connection: Error "TCP heartbeat timeout" at 15 s, closed by 25 s | svx-cases.md:450 | - | - | - | GAP |
| SVXE-9b TCP heartbeats only: Error "UDP heartbeat timeout" at 120 s (slow) | svx-cases.md:451 | - | - | - | GAP |
| SVXE-9c both heartbeats alive for 60 s: no Error, edge sends TCP Heartbeat after 10 s and UDP after 15 s idle | svx-cases.md:452 | - | - | - | GAP |
| SVXE-9d TCP dead: Error "TCP heartbeat timeout" about 15 s after the last TCP frame | svx-cases.md:453 | - | - | - | GAP |
| SVXE-9e any TCP frame resets the heartbeat timer | svx-cases.md:454 | - | - | - | GAP |
| SVXE-10a login with every byte sent separately gives the same result as SVXE-1 | svx-cases.md:465 | - | - | - | GAP |
| SVXE-10b coalesced ProtoVer and AuthResponse behave as sequential | svx-cases.md:466 | - | - | - | GAP |
| SVXE-10c frame_len 65 before login: connection closed, no Error frame | svx-cases.md:467 | - | - | - | GAP |
| SVXE-10d ProtoVer padded to frame_len 64 accepted (trailing bytes unchecked, D) | svx-cases.md:468 | - | - | - | GAP |
| SVXE-10e frame_len 32769 after login: connection closed, no Error | svx-cases.md:469 | - | - | - | GAP |
| SVXE-10f unknown message type 9999 after login is ignored | svx-cases.md:470 | - | - | - | GAP |
| SVXE-10g malformed SelectTG, TgMonitor, NodeInfo bodies: Error "Illegal MsgXxx protocol message received" | svx-cases.md:471 | - | - | - | GAP |
| SVXE-10h SelectTG with 6 trailing bytes accepted (D) | svx-cases.md:472 | - | - | - | GAP |
| SVXE-10i zero length frame: nothing until the next byte, then Error "Protocol error" (D) | svx-cases.md:473 | - | - | - | GAP |
| SVXE-11a UDP datagram with unknown client id 0xFFFF ignored | svx-cases.md:482 | - | - | - | GAP |
| SVXE-11b valid datagram from another source IP ignored | svx-cases.md:483 | - | - | - | GAP |
| SVXE-11c second socket with the right id (port change) ignored, first socket keeps working | svx-cases.md:484 | - | - | - | GAP |
| SVXE-11d sequence 10, 11, 9, 12, 20: late 9 dropped, gap 20 accepted | svx-cases.md:485 | - | - | - | GAP |
| SVXE-11e seq wrap 0xFFFE, 0xFFFF, 0, 1 all accepted | svx-cases.md:486 | - | - | - | GAP |
| SVXE-11f first datagram an Audio: accepted, edge answers with UDP Heartbeat | svx-cases.md:487 | - | - | - | GAP |
| SVXE-12 optional HTTP /status JSON, 404 other paths, 501 non-GET (D, edge may omit) | svx-cases.md:491 | - | - | - | GAP |

## netns vs DV-Router classification
Sources: netns-dvrouter-classification.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| Root cause: AMBEDPING every 5 s in every state, first at process start (only first ping and CAPS cadence asserted) | netns-dvrouter-classification.md:37 | `test_xlxd_usrp_netns::test_legacy_ambed_single_caps_probe` | netns | a | PARTIAL |
| Environment: one idle scenario peaks at 435 tasks (THREADS1, not asserted) | netns-dvrouter-classification.md:44 | - | - | - | GAP |
| test_no_usrp_sends_no_caps: no USRP, one CAPS probe (extended_always), answered | netns-dvrouter-classification.md:130 | `test_xlxd_usrp_netns::test_no_usrp_sends_no_caps` | netns | a | OK |
| test_no_usrp_legacy_open: no OPEN at all, nobody listens in another profile | netns-dvrouter-classification.md:131 | `test_xlxd_usrp_netns::test_no_usrp_legacy_open` | netns | a | OK |
| test_legacy_ambed_single_caps_probe: CAPS probe repeats every 5.0 s (+-0.3) while unanswered | netns-dvrouter-classification.md:132 | `test_xlxd_usrp_netns::test_legacy_ambed_single_caps_probe` | netns | a | OK |
| test_legacy_ambed_digital_uses_legacy_open: extended opens only, never a legacy 19 B OPEN | netns-dvrouter-classification.md:133 | `test_xlxd_usrp_netns::test_legacy_ambed_digital_uses_legacy_open` | netns | a | OK |
| test_legacy_ambed_pcm_not_transcoded: no PCM open and no USRP packets with a legacy transcoder | netns-dvrouter-classification.md:134 | `test_xlxd_usrp_netns::test_legacy_ambed_pcm_not_transcoded` | netns | a | OK |
| test_extended_single_caps_probe: one CAPS probe, answered | netns-dvrouter-classification.md:135 | `test_xlxd_usrp_netns::test_extended_single_caps_probe` | netns | a | OK |
| test_extended_opens: opens [24,1,[5]] and [24,5,[1]], distinct transactions, two closes | netns-dvrouter-classification.md:136 | `test_xlxd_usrp_netns::test_extended_opens` | netns | a | OK |
| test_extended_dstar_to_usrp: 50 keyed USRP frames carry the transcoder PCM, seq increasing, unkey last | netns-dvrouter-classification.md:137 | `test_xlxd_usrp_netns::test_extended_dstar_to_usrp` | netns | a | OK |
| test_extended_usrp_to_digital: PCM requests of 322 B, PCM unchanged to the transcoder | netns-dvrouter-classification.md:138 | `test_xlxd_usrp_netns::test_extended_usrp_to_digital` | netns | a | OK |
| test_extended_no_echo_to_usrp_peer: nothing echoed to the originating USRP peer | netns-dvrouter-classification.md:139 | `test_xlxd_usrp_netns::test_extended_no_echo_to_usrp_peer` | netns | a | OK |
| test_extended_bad_frame_dropped: TLV frame dropped, no open, router alive | netns-dvrouter-classification.md:140 | `test_xlxd_usrp_netns::test_extended_bad_frame_dropped` | netns | a | OK |
| test_one_output_opens: one output per open, distinct transactions (spec one_output_opens) | netns-dvrouter-classification.md:141 | `test_xlxd_usrp_netns::test_one_output_opens` | netns | a | OK |
| test_slow_open_short_burst_delivered: slow STD without a listener opens nothing (spec slow_open_opens) | netns-dvrouter-classification.md:142 | `test_xlxd_usrp_netns::test_slow_open_short_burst_delivered` | netns | a | OK |
| test_no_usrp_link_bytes_equal_reference: link bytes equal the xlxd reference (skipped without DVT_XLXD_REF_BIN) | netns-dvrouter-classification.md:143 | `test_xlxd_usrp_netns::test_no_usrp_link_bytes_equal_reference` | netns | a | PARTIAL |
| lifecycle lost_transcoder_caps_probed_again: second connection probed again after a 40 s loss | netns-dvrouter-classification.md:160 | `test_xlxd_transcoder_lifecycle::test_lost_transcoder_caps_probed_again` | netns | a | OK |
| lifecycle lost_transcoder_pcm_resumes: no open without a listener before and after the reconnect | netns-dvrouter-classification.md:161 | `test_xlxd_transcoder_lifecycle::test_lost_transcoder_pcm_resumes` | netns | a | OK |
| lifecycle lost_transcoder_dstar_to_usrp_resumes: D-STAR to USRP open and keyed PCM after the reconnect | netns-dvrouter-classification.md:162 | `test_xlxd_transcoder_lifecycle::test_lost_transcoder_dstar_to_usrp_resumes` | netns | a | OK |
| lifecycle lost_listener_pcm_opens: AMBE+ open for a D-STAR listener before and after the loss | netns-dvrouter-classification.md:163 | `test_xlxd_transcoder_lifecycle::test_lost_listener_pcm_opens` | netns | a | OK |
| lifecycle lost_listener_heard: listener hears the transcoded stream before and after (>=45 frames) | netns-dvrouter-classification.md:164 | `test_xlxd_transcoder_lifecycle::test_lost_listener_heard` | netns | a | OK |
| lifecycle fast_swap_caps_probed_after_failed_open: ignored extended OPEN, CAPS re-probe within 1.2 s | netns-dvrouter-classification.md:165 | `test_xlxd_transcoder_lifecycle::test_fast_swap_caps_probed_after_failed_open` | netns | a | OK |
| lifecycle legacy_swap_recovers: legacy transcoder gets no OPEN, CAPS after each PONG, extended back and opens | netns-dvrouter-classification.md:166 | `test_xlxd_transcoder_lifecycle::test_legacy_swap_recovers` | netns+slow | a | OK |
| lifecycle fast_swap_no_disconnect: swap inside the keepalive window, new transcoder pinged, old one served PCM (not in the doc table) | netns-dvrouter-classification.md:19 | `test_xlxd_transcoder_lifecycle::test_fast_swap_no_disconnect` | netns | a | OK |
| lifecycle rig variant: transcoder lost and back with the docker rig, USRP to D-STAR resumes (not part of the task) | netns-dvrouter-classification.md:168 | `test_xlxd_transcoder_lifecycle::test_rig_transcoder_lost_and_back_usrp_resumes` | rig | a | OK |
| Class (a): no spec adjustment, netns_ambed and netns_lifecycle match wip-0348b98 behaviour | netns-dvrouter-classification.md:173 | `test_xlxd_usrp_netns::test_extended_opens`, `test_xlxd_transcoder_lifecycle::test_lost_transcoder_caps_probed_again` | netns | a | OK |
| H1: -k launches only the scenarios of the selected cases (cases prefix per scenario) | netns-dvrouter-classification.md:372 | `test_netns_runs::test_usrp_k_selects_one_scenario`, `test_netns_runs::test_selected_names_filters_by_module_and_strips_params`, `test_netns_runs::test_every_scenario_has_cases_and_every_case_has_a_scenario`, `test_netns_runs::test_lifecycle_selection_unchanged` | unit | a | OK |
| H2: DVT_NETNS_PARALLEL waves (default 1) bound the concurrent scenarios | netns-dvrouter-classification.md:374 | `test_netns_runs::test_parallel_default_and_value`, `test_netns_runs::test_parallel_rejects_bad_values`, `test_netns_runs::test_run_in_waves_bounds_concurrency`, `test_netns_runs::test_run_in_waves_next_wave_after_collect`, `test_netns_runs::test_waves_cut` | unit | a | OK |
| H3: get() names the task-limit environment cause instead of "never sent AMBEDPING" | netns-dvrouter-classification.md:376 | `test_netns_runs::test_get_names_the_environment_when_never_pinged`, `test_netns_runs::test_starvation_found_in_xlxd_log`, `test_netns_runs::test_starvation_harness_died`, `test_netns_runs::test_lifecycle_restarted_transcoder_not_pinged_in_a_starved_run`, `test_netns_runs::test_get_keeps_the_product_message_without_the_signature` | unit | a | OK |
| H4: slow OPEN with a D-STAR listener, OPEN 0..0.3 s after the first voice, opens [[24,5,[1]]] | netns-dvrouter-classification.md:223 | `test_xlxd_usrp_netns::test_open_wait_listener_short_burst_heard` | netns | a | OK |
| H4: 5-frame burst heard in full, in order, one stream id, PCM unchanged to the transcoder | netns-dvrouter-classification.md:224 | `test_xlxd_usrp_netns::test_open_wait_listener_short_burst_heard` | netns | a | OK |
| H4: a slow STD below 1050 ms is a normal open (STD about 1.0 s after the OPEN) | netns-dvrouter-classification.md:225 | `test_xlxd_usrp_netns::test_open_wait_listener_short_burst_heard` | netns | a | OK |
| H4: target StreamEnd follows the outputs, one end frame after the 5 frames | netns-dvrouter-classification.md:226 | `test_xlxd_usrp_netns::test_open_wait_listener_short_burst_heard` | netns | a | PARTIAL |
| H4: last frame heard within the 3.0 s controller give-up, nothing heard before the STD | netns-dvrouter-classification.md:227 | `test_xlxd_usrp_netns::test_open_wait_listener_short_burst_heard` | netns | a | OK |
| H4: variant stays xlxd-only for slow_open, listener variant dropped under the xlxd profile | netns-dvrouter-classification.md:212 | `test_netns_runs::test_for_profile_drops_scenarios_of_another_profile`, `test_netns_runs::test_slow_open_variants_are_selected_apart` | unit | a | OK |
| H5: more than 128 units before SessionReady, transcoder gets exactly 128 requests, indexes 72..199 in order | netns-dvrouter-classification.md:282 | `test_xlxd_usrp_netns::test_pre_ready_cap_oldest_units_dropped` | netns | a | OK |
| H5: the D-STAR listener hears 128 voice frames, one stream id, none before the STD, inside the give-up | netns-dvrouter-classification.md:283 | `test_xlxd_usrp_netns::test_pre_ready_cap_oldest_units_dropped` | netns | a | OK |
| H5: counter pre_ready_dropped says 72, sessions_opened 1, open_failed 0, frames_out published 128 | netns-dvrouter-classification.md:284 | `test_xlxd_usrp_netns::test_pre_ready_cap_oldest_units_dropped` | netns | a | OK |
| H5: XCOBS1 session metrics (requested == opened + open_failed) read for both generations | netns-dvrouter-classification.md:307 | `test_netns_runs::test_normalise_sessions_reads_both_metric_generations`, `test_netns_runs::test_check_session_accounting_only_binds_xcobs1`, `test_netns_runs::test_xcode_counters_read_the_snapshot_of_the_step` | unit | a | PARTIAL |
| H6: STD later than 1050 ms: one CAPS re-probe 1.0..1.2 s after the first OPEN | netns-dvrouter-classification.md:333 | `test_xlxd_usrp_netns::test_open_refused_late_std_given_up` | netns | a | OK |
| H6: failed open counted, target streams of that over not produced, no retry within a stream | netns-dvrouter-classification.md:334 | `test_xlxd_usrp_netns::test_open_refused_late_std_given_up` | netns | a | OK |
| H6: STD after the session closed gets one CS for that stream id, nothing bound | netns-dvrouter-classification.md:335 | `test_xlxd_usrp_netns::test_open_refused_late_std_given_up` | netns | a | OK |
| H6: next over opens again and the 5 frames are heard in full with one end frame inside 3 s | netns-dvrouter-classification.md:336 | `test_xlxd_usrp_netns::test_open_refused_late_std_given_up` | netns | a | OK |
| H7: worker ends the session (SIGTERM to dv-codec-ambed): events failed 1, opened unchanged, listener hears fewer than 100 | netns-dvrouter-classification.md:421 | `test_xlxd_usrp_netns::test_session_failed_when_the_worker_ends_the_session` | netns | a | OK |
| H8: worker stopped (SIGSTOP then SIGCONT): drain_timeout 1, failed 0, attempts 1 / opened 1 / open_failed 0 | netns-dvrouter-classification.md:422 | `test_xlxd_usrp_netns::test_drain_timeout_when_the_worker_is_stopped` | netns | a | OK |

## Built-in echo (ECHO1)
Sources: echo-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| E1a one replay over, starts 0.8 to 2.0 s after the last packet (D-STAR, DMR, M17, YSF) | echo-cases.md:58 | `test_echo::test_e1a_one_replay_after_the_delay` | rig | a | OK |
| E1b replay names the talker as the live relay does; D-STAR header synthesized | echo-cases.md:59 | `test_echo::test_e1b_talker_identity_is_kept` | rig | a | OK |
| E1c replay payload byte identical (YSF: only gain index b2 steps), 2.5 to 3.6 s, ends | echo-cases.md:60 | `test_echo::test_e1c_payload_is_byte_identical_and_about_3_s` | rig | a | OK |
| E1d talker's own client hears the same replay (payload, name, D-STAR header) | echo-cases.md:61 | `test_echo::test_e1d_talker_hears_the_replay_too` | rig | a | OK |
| E1e dynamic USRP hub client over is local (class 0): replayed once, PCM identical | echo-cases.md:66 | `test_echo::test_e1e_dynamic_usrp_client_over_is_replayed` | rig | a | OK |
| E2 over of 25 frames (0.5 s) is not replayed (D-STAR, DMR, YSF) | echo-cases.md:62 | `test_echo::test_e2_short_over_is_not_replayed` | rig | a | OK |
| E3 over longer than max_seconds is replayed once, truncated to max_seconds, prefix identical (D-STAR, DMR) | echo-cases.md:63 | `test_echo::test_e3_long_over_is_truncated` | rig | a | OK |
| E4 no storm: the replay is not recorded again, nothing for 3 x delay_ms after it | echo-cases.md:64 | `test_echo::test_e4_no_storm_after_the_replay` | rig | a | OK |
| E5a over from the XLX peer (origin class 1) is heard once, byte for byte, not replayed | echo-cases.md:65 | `test_echo::test_e5a_xlx_peer_over_is_not_replayed` | interlink | c | OK |
| E5b over from a static USRP peer row (class 2) is not replayed, nothing sent back to the peer | echo-cases.md:67 | `test_echo::test_e5b_static_usrp_peer_over_is_not_replayed` | rig | a | OK |
| E5c over from the M17 peer on module M (class 2) is not replayed; local M17 over is (control) | echo-cases.md:68 | `test_echo::test_e5c_m17_peer_over_is_not_replayed` | rig | a | OK |
| E5d over from NXDN and P25 peers is not replayed (no peer rows in the native rig) | echo-cases.md:69 | `test_echo::test_e5d_nxdn_p25_not_reachable` | rig | - | GAP |
| E5e WebRTC talk (class 0) is replayed to the talker page and a second page | echo-cases.md:177 | `test_web_talk_echo_return::test_talk_echo_returns_to_the_page` | web/browser/webrtc | b:browser | OK |
| E6 floor is busy during the replay: second talker not relayed, replay intact, later human over relayed | echo-cases.md:70 | `test_echo::test_e6_floor_is_busy_during_the_replay` | rig | a | OK |
| E7a modules E=false stops replays after reload; back to true replays once in the delay window | echo-cases.md:71 | `test_echo::test_e7a_toggle_off_and_on` | rig | a | OK |
| E7b row removed during a replay: replay ends in an end of stream, floor free, child respawns | echo-cases.md:72 | `test_echo::test_e7b_row_removed_while_playing` | rig | a | OK |
| E7c latency of the live config edit (design: about two seconds) is not measured | echo-cases.md:181 | - | - | - | GAP |
| E8 two XLX-linked nodes both with echo on E | echo-cases.md:73 | `test_echo::test_e8_two_nodes_not_reachable` | rig | - | GAP |
| T dv_echo_overs_total moves by the overs made: played within range, truncated exact, short at least | echo-cases.md:74 | `test_echo::test_e7_telemetry_counts_the_overs` | rig | a | OK |
| Origin protocol echo (edge 19) of the replay shows in the hub events | echo-cases.md:178 | - | - | - | GAP |
| Out-of-range [echo] values (max_seconds 5..120, delay_ms 200..10000) and unknown module row are refused | echo-cases.md:183 | - | - | - | GAP |
| Bridge ping-pong with an external parrot | echo-cases.md:184 | - | - | - | GAP |
| Snippets echo, echo-short, echo-off, echo-peers, echo-usrp-peer, xlx-peer-tester merge as documented | echo-cases.md:29 | `test_echo_lib::test_echo_snippet_switches_module_e_on_with_defaults`, `test_echo_lib::test_short_variant_merges_into_the_table`, `test_echo_lib::test_off_variant_keeps_the_row_but_switches_it_off`, `test_echo_lib::test_peers_variant_adds_module_m`, `test_echo_lib::test_usrp_peer_snippet_adds_a_static_endpoint_row`, `test_echo_lib::test_xlx_peer_tester_snippet_names_the_tester` | unit | a | OK |
| Live edit of the rendered TOML (EchoCfg.apply) keeps everything else and restores the original | echo-cases.md:40 | `test_echo_lib::test_edit_modules_off_and_back`, `test_echo_lib::test_edit_removes_every_row`, `test_echo_lib::test_edit_keeps_everything_else` | unit | a | OK |
| ECHO1f: joins are silent, result=short equals the short overs made (only with DVT_ECHO_JOIN_SILENT=1) | echo-cases.md:264 | `test_echo::test_e7_telemetry_counts_the_overs` | rig | a | PARTIAL |
| ECHO1f: link-only probe leaves result=short and YSF frame counters unchanged (needs JOIN_SILENT and debug counters) | echo-cases.md:265 | `test_echo::test_e9y_link_only_probe` | rig | a | PARTIAL |
| ECHO1f: a real 25 frame over still counts short, also for YSF | echo-cases.md:267 | `test_echo::test_e2_short_over_is_not_replayed` | rig | a | OK |

## Echo return integrity E9 (D-STAR kinds)
Sources: echo-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| E9 dextra: DExtra sender to DExtra listener, 27 B DSVT, seq byte at 14 (forward, echo to L, echo to S) | echo-cases.md:97 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 dplus: DPlus sender to DPlus listener, 29 B and 32 B last packet | echo-cases.md:98 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 dcs: DCS 100 B packet, seq byte at 45 and frame counter at 58 equal to position | echo-cases.md:99 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 g3: G3 terminal to DExtra, echo to the terminal in G2 framing | echo-cases.md:100 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 xlx: DExtra to XLX peer, 27 / 45 / 53 B DSVT, seq and DMR ids (PARTIAL: peer-reflector mode does not judge the DMR packet id; needs interlink and a peer snippet) | echo-cases.md:101 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | PARTIAL |
| E9 frames: every frame sent is heard byte for byte, none extra (defect A, lost frame) | echo-cases.md:107 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 gaps: no arrival gap above 25 ms, none of 35 ms or more (catch-up tick drop) | echo-cases.md:108 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 seq: seq byte at position n is n mod 21, last packet 0x40 on top (defect B) | echo-cases.md:109 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 sync: slow data 55 2d 16 on positions mod 21 == 0 and nowhere else | echo-cases.md:110 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 own counter: DCS frame counter == position; XLX DMR packet id and sub id advance, echo follows forward copy | echo-cases.md:111 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 end bit on the last packet of the forward copy and of every echo return | echo-cases.md:164 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 header rule (defect C): echo keeps flags, RPT1, UR, MY, suffix; RPT2 is the live rule (XRF999 E) | echo-cases.md:117 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 forward copy is the control and is judged by the same rules | echo-cases.md:113 | `test_echo_dstar_integrity::test_e9_dstar_echo_return_integrity` | rig | a | OK |
| E9 judges (integrity, header rule, DCS counter, DMR ids, voice views) catch the injected defects | echo-cases.md:165 | `test_echo_lib::test_integrity_one_lost_frame_is_a_40_ms_gap_and_449_of_450`, `test_echo_lib::test_integrity_wrong_seq_bytes_give_a_share_near_chance`, `test_echo_lib::test_header_failures_rule`, `test_echo_lib::test_dcs_frame_counter_must_equal_the_position`, `test_echo_lib::test_xlx_dmr_ids_must_advance_one_step_per_packet` | unit | a | OK |

## Real-transcoder cross-mode audio into YSF (related to ECHO1)
Sources: echo-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| DMR to YSF: frames equal the sent ones through the VCH coder plus egress b2 step, tone 700 Hz, level | echo-cases.md:254 | `test_crossmode_real_ysf::test_cross_mode_into_ysf_is_not_garbled` | rig | a | OK |
| D-STAR to YSF: decoded tone within 80 Hz, tone share and level against transcoder-ref output | echo-cases.md:255 | `test_crossmode_real_ysf::test_cross_mode_into_ysf_is_not_garbled` | rig | a | OK |
| M17 to YSF: tone correct and level within 3 dB of the reference reflector with the same transcoder | echo-cases.md:256 | `test_crossmode_real_ysf::test_cross_mode_into_ysf_is_not_garbled` | rig | a | OK |

## DP15 (APRS / AX.25 / KISS data path)
Sources: dp15-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| DP15-1a radio TX message becomes the talker's status on the web within one superframe plus pipeline | dp15-cases.md:95 | `test_dp15_streams::test_dp15_1a_tx_message_becomes_the_status` | rig | a | OK |
| DP15-1b D-PRS GPS-A beacon becomes one position (operator and public views); spoofed source rejected | dp15-cases.md:96 | `test_dp15_streams::test_dp15_1b_dprs_beacon_is_a_position` | rig | a | OK |
| DP15-1c loss of a required text block gives no status and never a partial one | dp15-cases.md:97 | `test_dp15_streams::test_dp15_1c_a_missing_text_block_gives_no_status` | rig | a | OK |
| DP15-1d native D-STAR to D-STAR slow data byte identical with the worker alive and killed | dp15-cases.md:98 | `test_dp15_streams::test_dp15_1d_native_dstar_slow_data_is_byte_identical` | rig | a | OK |
| DP15-2a M17 SMS during a D-STAR QSO reaches M17 at once and D-STAR only after a 2 s pause | dp15-cases.md:99 | `test_dp15_streams::test_dp15_2a_m17_sms_during_a_dstar_qso` | rig | a | OK |
| DP15-2b web text during a D-STAR QSO: same timing, M17 voice unchanged (asserted with fake transcoder only) | dp15-cases.md:100 | `test_dp15_streams::test_dp15_2b_web_text_during_a_dstar_qso` | rig | a | OK |
| DP15-2c M17 META text appears as D-STAR text blocks in the cross-mode egress; D-STAR over untouched | dp15-cases.md:101 | `test_dp15_streams::test_dp15_2c_m17_meta_text_rides_into_the_dstar_egress` | rig | a | OK |
| DP15-2d APRS payload in M17 packet mode (specifier 0x02) gives one position | dp15-cases.md:102 | `test_dp15_streams::test_dp15_2d_m17_packet_aprs_is_a_position` | rig | a | OK |
| DP15-3a kill the worker during traffic: voice untouched, web chat works, gap text never rendered, new text at token | dp15-cases.md:129 | `test_dp15_worker::test_dp15_3a_kill_the_worker_during_traffic` | rig | a | OK |
| DP15-3b restart: renders resume within 2 s of the token (3 rounds), gap text never rendered | dp15-cases.md:130 | `test_dp15_worker::test_dp15_3b_renders_resume_within_two_seconds_of_the_token` | rig | a | OK |
| DP15-4a YSF GPS fix in FN 6/7 gives one position | dp15-cases.md:103 | `test_dp15_streams::test_dp15_4a_ysf_gps_fix_is_one_position` | rig | a | OK |
| DP15-4a' a changed YSF fix inside 30 s gives no second position | dp15-cases.md:103 | - | - | - | GAP |
| DP15-4b DMRG inside a DMR over gives one position; DMRG outside a stream gives none | dp15-cases.md:104 | `test_dp15_streams::test_dp15_4b_dmrg_is_one_position` | rig | a | OK |
| DP15-4c DMRA gives a status and never changes the displayed callsign (alias and foreign callsign) | dp15-cases.md:105 | `test_dp15_streams::test_dp15_4c_dmra_is_a_status_and_never_the_callsign` | rig | a | OK |
| DP15-5a APRS-IS message to XLX999B reaches the room (D-STAR, M17, web) and is acked once, no q written | dp15-cases.md:319 | `test_dp15_aprsis::test_dp15_5a_aprsis_message_reaches_the_room_and_is_acked_once` | rig | b:aprsc | OK |
| DP15-5b repeated APRS-IS message is acked again byte for byte and delivered once | dp15-cases.md:320 | `test_dp15_aprsis::test_dp15_5b_a_repeated_aprsis_message_is_acked_again_and_delivered_once` | rig | b:aprsc | OK |
| DP15-5c to 5e unverified login, qAC on an uplink path, NOGATE | dp15-cases.md:324 | - | - | - | GAP |
| DP15-5f 2 s repeat of the plan (server dupecheck only) | dp15-cases.md:324 | - | - | - | GAP |
| DP18-a DP18 node beacon and object arrive at the private aprsc as the gateway call's own reports (`TCPIP*`, server `qAC`, hand-built information field, first line after one interval) | dp18-cases.md "DP18-a" | `test_dp18_origin::test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports` | rig | b:aprsc | OK |
| DP18-b the object comes 35 s after the beacon (stagger) | dp18-cases.md "DP18-b" | `test_dp18_origin::test_dp18_b_the_object_comes_35_s_after_the_beacon` | rig | b:aprsc | OK |
| DP18-c the object HMS timestamp is the send time within 1 s | dp18-cases.md "DP18-c" | `test_dp18_origin::test_dp18_c_the_object_timestamp_is_the_send_time` | rig | b:aprsc | OK |
| DP18-n `dv_pkt_origin_total{result=generated}` grows per report | dp18-cases.md "DP18-n" | `test_dp18_origin::test_dp18_n_origin_counters_count_generated_reports` | rig | b:aprsc | OK |
| DP18-k after a dv-data-worker kill nothing arrives before one full interval | dp18-cases.md "DP18-k" | `test_dp18_origin::test_dp18_k_nothing_arrives_before_one_full_interval_after_a_worker_kill` | rig | b:aprsc | OK |
| DP18-e a position policy withholding the gateway call: no line, `withheld` grows | dp18-cases.md "DP18-e" | `test_dp18_origin::test_dp18_e_a_withheld_gateway_call_sends_nothing` | rig | b:aprsc | OK |
| DP12c-s a simulated AX.25 port never reaches its TNC, counts `simulated`, the real port on the same physical keeps its timing | dp18-cases.md "DP18-s" | `test_dp18_rf::test_dp18_s_a_simulated_port_never_reaches_its_tnc` | rig | a | OK |
| DP12c-r reload simulate <-> true through the admin config API: the edge restarts, nothing queued before the change is transmitted after it, restore from history | dp18-cases.md "DP18-r1" | `test_dp18_rf::test_dp18_r1_simulate_to_true_drops_the_queued_simulated_frames`, `test_dp18_rf::test_dp18_r2_true_to_simulate_then_restore_from_history` | rig | a | OK |
| DP18-o several RF objects on afsk1200-shared: releases spread, drops counted and not always the same object (the second half recorded only) | dp18-cases.md "DP18-o" | `test_dp18_rf::test_dp18_o_object_copies_are_spread_and_drops_are_counted` | rig | a | PARTIAL |
| DP12c radio port with `tx = "simulate"` never keys PTT or sends audio (no radio ports in the rig) | dp18-cases.md "Limits" | - | - | - | GAP |
| DP12c the `rf/<physical>/ptt` token of a simulated port is never declared (not observable black-box) | dp18-cases.md "Limits" | `test_dp18_rf::test_dp18_s_a_simulated_port_never_reaches_its_tnc` (counters and silence only) | rig | a | PARTIAL |
| DP11e `listen` is required and a non-loopback bind needs `allow_from` (and 15 more `kiss_server` config rules) | dp11e-cases.md "config" | `test_dp11e_config::test_dp11e_config` | cfgload | - | OK |
| DP11e the default bind is loopback (not reachable from another host) | dp11e-cases.md "DP11e-k" | `test_dp11e_kiss::test_dp11e_k_a_loopback_listener_is_not_reachable_from_another_host` | rig | a | OK |
| DP11e a peer outside `allow_from` is closed before any byte; the fourth client of three is refused | dp11e-cases.md "DP11e-c" | `test_dp11e_kiss::test_dp11e_c_a_peer_outside_allow_from_is_closed_before_any_byte`, `test_dp11e_kiss::test_dp11e_c_the_fourth_client_of_three_is_refused_full` | rig | a | OK |
| DP11e client TX only from `tx_from`, with a source in `allow_src`, channel mapping, path, size, third-party and command refusals, each with its label | dp11e-cases.md "DP11e-b" | `test_dp11e_kiss::test_dp11e_b_each_refusal_label_is_counted`, `test_dp11e_kiss::test_dp11e_b_only_the_control_reaches_the_modem_as_sent` | rig | a | OK |
| DP11e ASCII outside frames is ignored and does not close the client | dp11e-cases.md "DP11e-a" | `test_dp11e_kiss::test_dp11e_a_ascii_outside_frames_is_ignored` | rig | a | OK |
| DP11e the lone-FEND keepalive, on and off | dp11e-cases.md "DP11e-d" | `test_dp11e_kiss::test_dp11e_d_keepalive_on_and_off` | rig | a | OK |
| DP11e UI/F0 fan-out to other clients with the mapped channel, no echo to the sender | dp11e-cases.md "DP11e-e" | `test_dp11e_kiss::test_dp11e_e_fan_out_to_every_client_with_the_mapped_channel_and_no_echo` | rig | a | OK |
| DP11e client frames never reach APRS-IS or rooms | dp11e-cases.md "DP11e-g" | `test_dp11e_kiss::test_dp11e_g_client_frames_never_reach_aprs_is_or_rooms` | rig | b:aprsc | OK |
| DP11e sessions `kiss_client`, kick, no peer address or content in the log | dp11e-cases.md "DP11e-h" | `test_dp11e_kiss::test_dp11e_h_sessions_are_listed_and_kick_closes_the_client`, `test_dp11e_kiss::test_dp11e_h_the_sut_log_has_no_peer_address_and_no_frame_content` | rig | a | OK |
| DP11e slow client (`slow`) and write stall (`stalled`) | dp11e-cases.md "DP11e-i" | `test_dp11e_kiss::test_dp11e_i_a_slow_client_loses_the_newest_frames_and_a_stalled_one_is_closed` | rig | a | OK |
| DP11e edge restart closes clients and the first frame meets the RFSAFE1 guard | dp11e-cases.md "DP11e-j" | `test_dp11e_kiss::test_dp11e_j_an_edge_restart_closes_clients_and_the_first_frame_meets_the_guard` | rig | a | OK |
| DP11e listener bucket `rate`; simulated port without modem byte | dp11e-cases.md "DP11e-l" | `test_dp11e_kiss::test_dp11e_l_the_listener_bucket_counts_rate`, `test_dp11e_kiss::test_dp11e_m_a_simulated_port_counts_simulated_and_writes_no_modem_byte` | rig | a | OK |
| DP11e a client frame to a receive-only exposed port is refused as `tx_off` (not queued, nothing at the modems) | dp11e-cases.md "DP11e-n" | `test_dp11e_kiss::test_dp11e_n_a_frame_to_a_receive_only_port_is_tx_off` | rig | a | OK |
| DP11e the KISS counter label sets are the fixed ones (dir in/out, per-direction results, listener names as port) | dp11e-cases.md "DP11e-y" | `test_dp11e_kiss::test_dp11e_y_the_counter_label_sets_are_the_fixed_ones` | rig | a | OK |
| DP11e `disabled` admission is defensive only (TxCore admit in tx.rs): only its absence is checked | dp11e-cases.md "GAPs" | `test_dp11e_kiss::test_dp11e_z_no_admission_is_counted_disabled` | rig | a | PARTIAL |
| DP11e loopback peers are accepted and `tx_from` defaults to loopback (tester in another namespace; DV-Router shell tests) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| DP11e a mapped TNC2 port is refused (no TNC2 port in the rig; config fixtures) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| DP11e IPv4-mapped IPv6 peers match IPv4 rules (no dual-stack peer; `ipv4_mapped_peers_match_ipv4_rules`) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| DP11e the source quota key `kiss/<listener>` (only the dropped_quota effect is visible; scheduler tests) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| DP11e Nagle off and TCP keepalive socket options (not visible from the peer; shell tests) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| DP11e at most eight frames per client per turn and the 16 KiB/s read budget (core tests) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| DP11e the PTT holder token of the client TX path (a fabric key, not observable; shell tests) | dp11e-cases.md "GAPs" | - | - | - | GAP |
| OBSPKT1 the packet monitor is admin-only: viewer and operator get 403 forbidden, admin with a step-up gets the documented snapshot and stream | obspkt1-cases.md "OBSPKT1-a" | `test_obspkt1_monitor::test_obspkt1_a_viewer_and_operator_get_403_on_both_routes`, `test_obspkt1_monitor::test_obspkt1_a_admin_with_a_step_up_gets_the_documented_snapshot_and_stream` | rig | a | OK |
| OBSPKT1 admin without a fresh step-up gets 403 step_up_required | obspkt1-cases.md "OBSPKT1-f" | `test_obspkt1_monitor::test_obspkt1_f_admin_without_a_fresh_step_up_gets_403_step_up_required` | rig | a | OK |
| OBSPKT1 RF rx / tx and APRS-IS rx / tx rows with the gate decision; worker rows carry the line, edge rows the header and outcomes | obspkt1-cases.md "OBSPKT1-b" | `test_obspkt1_monitor::test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header`, `test_obspkt1_monitor::test_obspkt1_b_aprs_is_rx_and_rf_tx_rows`, `test_obspkt1_monitor::test_obspkt1_b_a_local_kiss_client_row_keeps_its_line_but_not_a_position` | rig | a | OK |
| OBSPKT1 a hidden station's position is blank in every row; Mic-E destinations show (mic-e); edge rows have no line; a station hidden later loses its position | obspkt1-cases.md "OBSPKT1-c" | `test_obspkt1_monitor::test_obspkt1_c_a_hidden_stations_position_is_blank_in_every_row`, `test_obspkt1_monitor::test_obspkt1_c_mic_e_destinations_show_the_placeholder`, `test_obspkt1_monitor::test_obspkt1_c_edge_rows_of_worker_decided_packets_have_no_line`, `test_obspkt1_monitor::test_obspkt1_c_a_station_hidden_later_loses_its_position_from_retained_rows` | rig | a | OK |
| OBSPKT1 no packet body in logs, public state, other admin routes or telemetry | obspkt1-cases.md "OBSPKT1-e" | `test_obspkt1_monitor::test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry` | rig | a | OK |
| OBSPKT1 a process restart clears the rings (worker kill, AX.25 child restart), the stream reports exactly one gap for the restarted worker and one for the old edge id, none for others, no bye (OBSPKT1-d2: step-up and logout of the stream's own session, asserted with DVT_OBSPKT1B=on) | obspkt1-cases.md "OBSPKT1-d" | `test_obspkt1_monitor::test_obspkt1_d_a_process_restart_clears_its_rings` | rig | a | OK |
| OBSPKT1 ring bounds, stream limits, audit rows, cursor loss, retired port rings, raw TNC2 header (long runs or SUT races; DV-Router's tests) | obspkt1-cases.md "GAPs" | - | - | - | GAP |
| DP15-8w RF-heard positionless weather is gated to the private aprsc once, `qAR` + igate call, path as heard, information byte-identical (`qAO` receive-only) | dp15-cases.md "RF to APRS-IS igate" | `test_dp15_igate::test_dp15_8w_rf_weather_is_gated_with_qar` | rig | b:aprsc | OK |
| DP15-8t RF-heard telemetry `T#` is gated the same way | dp15-cases.md "RF to APRS-IS igate" | `test_dp15_igate::test_dp15_8t_rf_telemetry_is_gated_with_qar` | rig | b:aprsc | OK |
| DP15-8q RF-heard general query `?APRS?` is never gated (bounded window from the run's latency, control frame) | dp15-cases.md "RF to APRS-IS igate" | `test_dp15_igate::test_dp15_8q_rf_general_query_is_not_gated` | rig | b:aprsc | OK |
| DP15-8n, 8r NOGATE / RFONLY in the path is never gated (aprsc drops them too: evidence is the path counter) | dp15-cases.md "RF to APRS-IS igate" | `test_dp15_igate::test_dp15_8n_nogate_path_is_not_gated`, `test_dp15_8r_rfonly_path_is_not_gated` | rig | b:aprsc | OK |
| DP15-6g startup guard is reported as a fence (`startup_guard`, guard_ms) in the first 13 s after a start; late reads skip | dp15-cases.md "DP15-6g" | `test_dp15_rf::test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds` | rig | a | OK |
| DP15-6s KISS parameters (TXDELAY, P, SlotTime, TXtail, FullDuplex) written at connect and reconnect, no other command | dp15-cases.md:192 | `test_dp15_rf::test_dp15_6s_kiss_parameters_are_written_after_every_connect` | rig | a | OK |
| DP15-6s' the same against a real Dire Wolf TNC (tier B) | dp15-cases.md:381 | - | - | - | GAP |
| DP15-6a RF message to the room name reaches the room once (M17, D-STAR, SSE, history), no RF ack | dp15-cases.md:193 | `test_dp15_rf::test_dp15_6a_message_to_the_room_name_on_rf_reaches_the_room`, `test_dp15_rf_dw::test_dp15_6a_message_to_the_room_name_on_rf_reaches_the_room` | rig | c | OK |
| DP15-6b off no RF transmit while tx_enable is false (room still gets the text) | dp15-cases.md:194 | `test_dp15_rf::test_dp15_6b_no_rf_transmit_while_tx_enable_is_false`, `test_dp15_rf_dw::test_dp15_6b_no_rf_transmit_while_tx_enable_is_false` | rig | c | OK |
| DP15-6b port a receive-only port never transmits | dp15-cases.md:195 | `test_dp15_rf::test_dp15_6b_the_receive_only_port_never_transmits` | rig | a | OK |
| DP15-6c room text goes out as exactly one group message frame (src, tocall, no digis, no id, <= 67 chars) | dp15-cases.md:196 | `test_dp15_rf::test_dp15_6c_room_text_goes_out_as_one_group_message`, `test_dp15_rf_dw::test_dp15_6c_room_text_goes_out_as_one_group_message` | rig | c | OK |
| DP15-6c' a 100 character text is cut to 67 or refused (PARTIAL: asserted only when a frame is sent) | dp15-cases.md:196 | `test_dp15_rf::test_dp15_6c_room_text_goes_out_as_one_group_message` | rig | a | PARTIAL |
| DP15-6d never above the duty cycle: frames per minute, min gap, airtime bound | dp15-cases.md:197 | `test_dp15_rf::test_dp15_6d_never_above_the_duty_cycle` | rig | a | OK |
| DP15-6d' a low tx_budget run (class defaults only are exposed) | dp15-cases.md:197 | - | - | - | GAP |
| DP15-6d'' 6d on tier B (frame times against the budget through the modem loop) | dp15-cases.md:381 | - | - | - | GAP |
| DP15-6e a frame heard on two ports is one item; a different message is a second item | dp15-cases.md:198 | `test_dp15_rf::test_dp15_6e_a_frame_heard_on_two_ports_is_one_item` | rig | a | OK |
| DP15-6e' 6e on tier B (same audio in two Dire Wolf instances) | dp15-cases.md:381 | - | - | - | GAP |
| DP15-6f RF ack with alias_calls (the room name cannot be an AX.25 source) | dp15-cases.md:381 | - | - | - | GAP |
| DP15-6l modem loop passes a frame both ways byte for byte within 20 s (tier B control) | dp15-cases.md:269 | `test_dp15_rf_dw::test_dp15_6l_the_modem_loop_passes_a_frame_both_ways` | rig | b:Dire Wolf | OK |
| DP15-7a XLX peer wire: synthetic text over is a whole stream, MY XLX999, null AMBE, valid text slow data, spacing >= 1 s | dp15-cases.md:106 | `test_dp15_streams::test_dp15_7a_text_over_on_the_xlx_peer_wire` | rig | a | OK |
| DP15-7a' xlx_text_overs = false (live config edit) stops the text over on the peer wire | dp15-cases.md:383 | - | - | - | GAP |
| DP15-7a'' a text over is preempted by peer voice | dp15-cases.md:383 | - | - | - | GAP |
| DP15-7a''' the sending node's own XLX listener does not get its own text over back | dp15-cases.md:383 | - | - | - | GAP |
| DP15-7b remote floor: receiving node's floor is held for the text over, second talker denied then granted; sender node excluded | dp15-cases.md:107 | `test_dp15_peer::test_dp15_7b_remote_floor_of_the_text_over` | rig | a | OK |
| DP15-7c receiving node records the text over; sending node does not record (local exclusion) | dp15-cases.md:108 | `test_dp15_peer::test_dp15_7c_the_receiving_node_records_the_text_over` | rig | a | OK |
| DP15-7d cross-mode effects on the receiving node: DExtra whole text over, M17 stream, no data item derived | dp15-cases.md:109 | `test_dp15_peer::test_dp15_7d_cross_mode_effects_on_the_receiving_node` | rig | a | OK |
| DP15-7e web side of the second node (room on-air shown as remote, public/state, recordings API, web listeners) | dp15-cases.md:382 | - | - | - | GAP |
| DP15-7f DMR listener on the second node (transcoded null AMBE to DMR) | dp15-cases.md:382 | - | - | - | GAP |
| Room path dp15-b-to-c: forwarded copy of a web text to room C (PARTIAL: recorded only, nothing depends on it) | dp15-cases.md:387 | `test_dp15_streams::test_dp15_2b_web_text_during_a_dstar_qso` | rig | a | PARTIAL |
| Open question Q12: changed status at most once per 30 s (per stream, callsign or room) | dp15-cases.md:419 | - | - | - | GAP |
| Builder YSF GPS record (FN 6 / FN 7, magic, checksum) matches hand vectors and the wire format | dp15-cases.md:401 | `test_dp15_builders::test_ysf_record_hand_vector`, `test_dp15_builders::test_ysf_record_second_hand_vector`, `test_dp15_builders::test_ysf_record_round_trip`, `test_dp15_builders::test_ysf_gps_stream_dch_survives_the_wire_format` | unit | a | OK |
| Builder DMRG (id, 7 bytes, longitude and latitude steps) matches hand vectors | dp15-cases.md:402 | `test_dp15_builders::test_dmrg_hand_vectors`, `test_dp15_builders::test_dmrg_round_trip` | unit | a | OK |
| Builder DMRA (block, 7 bit / ISO 8 / UTF-16 text) matches hand vectors | dp15-cases.md:403 | `test_dp15_builders::test_dmra_hand_vectors`, `test_dp15_builders::test_dmra_round_trip` | unit | a | OK |
| Builder legacy M17 META text (bit map nibbles, 13 byte blocks, up to 4) | dp15-cases.md:404 | `test_dp15_builders::test_meta_text_control_bytes`, `test_dp15_builders::test_meta_text_round_trip_and_order`, `test_dp15_builders::test_meta_text_stream_shape` | unit | a | OK |
| Builder D-STAR TX message and D-PRS overs decode with the DV-Router unit decoder | dp15-cases.md:405 | `test_dstar_sd::test_tx_message_fields_decode_to_the_text_in_every_superframe`, `test_dstar_sd::test_text_gps_fields_carry_text_and_a_valid_dprs_line`, `test_dstar_sd::test_over_packets_shape` | unit | a | OK |
| Builder M17P APRS packet, specifier 0x02 (PARTIAL: generic m17p_packet test, no 0x02 vector) | dp15-cases.md:406 | `test_m17_conf::test_m17p_packet` | unit | a | PARTIAL |
| AX.25 UI frame codec (FCS, address bytes, TNC2 text, digipeater H bits) | dp15-cases.md:140 | `test_ax25::test_fcs_check_value`, `test_ax25::test_address_bytes`, `test_ax25::test_tnc2_round_trip_and_h_bits` | unit | a | OK |
| KISS framing in our client agrees with Dire Wolf's own decoder and encoder vectors | dp15-cases.md:226 | `test_ax25::test_kiss_encode_decode`, `test_ax25::test_tx_vectors_dire_wolf_unwrap_equals_our_decoder`, `test_ax25::test_rx_vectors_agree_with_our_encoder` | unit | a | OK |
| Fake KISS TNC records the five startup parameters per connection, injects and records data frames | dp15-cases.md:140 | `test_kiss_tnc::test_records_the_five_startup_frames_in_order`, `test_kiss_tnc::test_a_reconnect_gives_a_second_list_of_parameters`, `test_kiss_tnc::test_frames_the_edge_transmits_are_recorded_and_decoded` | unit | a | OK |
| APRS-IS client and passcode scheme (login, q construct check, ack line shape) | dp15-cases.md:327 | `test_aprs_passcode::test_public_vector_n0call`, `test_aprsis_client::test_login_message_round_trip_and_filter`, `test_dp15_aprsis_config::test_check_ack_line_accepts_the_documented_shape` | unit | a | OK |
| Snippets and rig wiring of tier A, tier B, aprsc and two-node runs are valid and consistent | dp15-cases.md:181 | `test_dp15_rf_helpers::test_read_rf_config_in_both_run_shapes`, `test_dp15_rf_dw_config::test_snippet_with_the_token_true_and_the_out_path_transmits_on_rf_a_only`, `test_dp15_aprsis_config::test_snippet_carries_the_path_row_with_the_edge_enable`, `test_dp15_peer_config::test_node_two_has_its_own_callsign_address_and_recorder_dir` | unit | a | OK |


## DP6c / DP19 (weather, telemetry, locator status)
Sources: dp6c-cases.md.

| Obligation | Source | Case ids | Marker | Client | Status |
|---|---|---|---|---|---|
| DP6c RF: separate stations per SSID with position + weather (G5), position + telemetry (G9 under G10), a separate position, positionless weather (G1); rows carry hops and forwarded, none for the bare base, none in the message history | dp6c-cases.md "DP6c-1" | `test_dp6c_rf::test_dp6c_1_stations_weather_and_telemetry_over_rf` | rig | a | OK |
| DP6c a compressed weather position with speed byte z keeps its marker and shows the weather without wind speed | dp6c-cases.md "DP6c-8" | `test_dp6c_rf::test_dp6c_8_compressed_weather_with_an_out_of_range_speed_byte` | rig | a | OK |
| DP6c hide_position of a base removes every SSID and kind on the state refresh, from the SSE and its replay; new items are denied/13 | dp6c-cases.md "DP6c-3" | `test_dp6c_rf::test_dp6c_3_hide_position_removes_every_ssid_and_every_kind` | rig | a | OK |
| DP6c position_optout and positions = false deny weather and telemetry as 13 | dp6c-cases.md "DP6c-3" | `test_dp6c_rf::test_dp6c_3_position_optout_denies_weather_and_telemetry`, `test_dp6c_rf::test_dp6c_3_positions_false_denies_weather_and_telemetry` | rig | a | OK |
| DP6c APRS-IS weather and telemetry through a typed path give rows without a position or marker; IS positions stay refused | dp6c-cases.md "DP6c-2" | `test_dp6c_aprsis::test_dp6c_2_is_weather_and_telemetry_without_a_position` | rig | a | OK |
| DP6c without the weather types no annex row appears (the edge counted the packets) | dp6c-cases.md "DP6c-2" | `test_dp6c_aprsis::test_dp6c_2_is_without_weather_types_gives_no_rows` | rig | a | OK |
| DP6c an APRS-IS -> room path with weather and telemetry loads, an unknown type and the forbidden pairs are refused at load | dp6c-cases.md "Configuration" | `test_dp6c_config::test_dp6c_config` | cfgload | a | OK |
| DP6c browsers, EN and TH: Weather column, details, "No position received", literal markup, own origin only, no CSP report | dp6c-cases.md "DP6c-4" | `test_dp6c_browser::test_dp6c_4_station_details_are_text_only_in_both_languages` | rig | a | OK |
| DP6c the operator events view shows no weather or telemetry row | dp6c-cases.md "DP6c-9" | `test_dp6c_browser::test_dp6c_9_operator_event_view_shows_no_weather_or_telemetry` | rig | a | OK |
| DP6c golden frames G1, G5, G6, G7, G7z, G9, G10 and the DP19 locator vectors are what the builders emit | dp6c-cases.md "Frames" | `test_proto_aprs::test_g5_and_g6_weather_with_an_uncompressed_position`, `test_proto_aprs::test_g7_compressed_weather_and_the_out_of_range_speed_byte`, `test_proto_aprs::test_g9_g10_telemetry_report_and_definitions`, `test_proto_aprs::test_dp19_locator_statuses_and_look_alikes` | unit | a | OK |
| DP19 a valid locator status of a private base is denied/13, look-alikes and a plain status are ordinary statuses, a public base's is public | dp6c-cases.md "DP19-a" | `test_dp19_locator::test_dp19_a_locator_status_is_dropped_for_a_private_base` | rig | a | OK |
| DP19 a private base's valid locator status is not gated to APRS-IS by RF transit; look-alikes and plain statuses are | dp6c-cases.md "DP19-b" | `test_dp19_locator::test_dp19_b_locator_status_is_not_gated_to_aprsis_for_a_private_base` | rig | a | OK |
| DP19 the packet monitor blanks a locator status like a position (worker row of a hidden base, local KISS client TX row, retained rows follow the current policy); edge RX is header only | dp6c-cases.md "OBSPKT1-g" | `test_obspkt1_monitor::test_obspkt1_g_locator_status_is_redacted_like_a_position` | rig | a | OK |
| DP6c the marker click that opens and focuses the station details (a WebGL marker has no DOM element) | dp6c-cases.md "GAPs" | - | - | - | GAP |
| DP6c the admin SPA event view (admin host sign-in with TOTP) | dp6c-cases.md "GAPs" | - | - | - | GAP |
| DP19 embedded M17 locator status and an APRS-IS locator status (IS positions and statuses never enter a room; no room-bound seam on the rig for M17 statuses of a private base) | dp6c-cases.md "GAPs" | - | - | - | GAP |
| DP19b a locator status, a plain status and the look-alike text are visible on the SSE and in the message history; only the first is spatial (state `spatial_status` holds it alone) | dp19b-cases.md "DP19b-1" | `test_dp19b_status::test_dp19b_1_three_items_are_visible_and_only_the_locator_status_is_spatial` | rig | a | OK |
| DP19b hide_position withdraws the spatial status from the state, a fresh SSE replay and the user history within one state refresh; plain status and text stay | dp19b-cases.md "DP19b-2" | `test_dp19b_status::test_dp19b_2_hide_position_withdraws_the_spatial_status_everywhere` | rig | a | OK |
| DP19b the open dashboard message panel drops the hidden base's spatial status and keeps the plain status and the text | dp19b-cases.md "DP19b-2 browser" | `test_dp19b_browser::test_dp19b_2_the_open_message_panel_drops_the_hidden_bases_spatial_status` | rig | a | OK |
| DP19b F1: a position and a spatial status arrive live on an open SSE after a quiet minute | dp19b-cases.md "DP19b-3" | `test_dp19b_status::test_dp19b_3_a_quiet_node_still_delivers_position_and_spatial_status_live` | rig | a | OK |
| DP19b a restart with the account store unreadable withholds spatial rows and delivers text and plain status (opt-in, destructive) | dp19b-cases.md "DP19b-4" | `test_dp19b_status::test_dp19b_4_spatial_rows_are_withheld_while_accounts_are_unavailable` | rig | a | PARTIAL |
| DP19b the talker status drops a hidden base's spatial status; `position_optout` plus reload as the hide | dp19b-cases.md "GAPs" | - | - | - | GAP |
