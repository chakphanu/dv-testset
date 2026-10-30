<!-- SPDX-License-Identifier: MIT -->
# Test list

What has to be verified, and which case verifies it. Status:
**covered** (an automated case asserts it), **planned** (being written),
**gap** (nothing checks it yet). Test ids are `file::function`; `[..]` means
the case is parametrized.

Levels: `unit` offline, `wire` against a transcoder, `netns` xlxd in user
namespaces, `rig` docker rig, `allstar` / `nxdn` / `p25` rig profiles.
Every `--capture` run is also checked passively by `dvcheck` (section H).
Rig cases are also marked `interop` or `behaviour` (DV-Router profile below)
and, when their assertions need cross-mode audio through the system under
test's transcoding, `crossmode` (only the cross-mode params of a
parametrized case); a system under test without transcoding runs
`-m "rig and not crossmode"`. `proto` marks the per-protocol conformance
cases (section K); `slow` marks cases that wait for a server timeout. The table lives in `cases/conftest.py`.

## A. Transcoder link conformance (`wire`)

| # | Check | Case | Status |
|---|---|---|---|
| A1 | PING is answered with PONG | test_ambed_wire::test_ping_pong | covered |
| A2 | CAPS reply: 15 bytes, version 1, max outputs, codec mask | test_ambed_wire::test_caps_reply | covered |
| A3 | Legacy 17/18/19-byte opens get a legacy reply (STD14 or BUSY9), never 18/13 bytes | test_ambed_wire::test_legacy_open_gets_legacy_reply[..] | covered |
| A4 | Legacy replies equal a reference transcoder's | test_ambed_wire::test_legacy_open_matches_reference[..] | covered (needs DVT_AMBED_REF) |
| A5 | Legacy opens beyond capacity get BUSY9 | test_ambed_wire::test_legacy_open_full_gets_busy9 | covered |
| A6 | Extended open with a codec outside the CAPS mask gets BUSY13 | test_ambed_wire::test_ext_open_unserved_codec_busy | covered (skipped when every codec is served) |
| A7 | Invalid extended opens (duplicate, gap, out = in, no outputs, unknown codec, bad callsign) get BUSY13 with the transaction | test_ambed_wire::test_ext_open_invalid_busy[..] | covered |
| A8 | Extended open gets STD18; an exact retransmit gets the same STD18 and no new stream | test_ambed_wire::test_ext_open_std18_and_retransmit | covered |
| A9 | Same transaction with other codecs is a new open; the earlier stream is kept | test_ambed_wire::test_ext_open_same_tx_other_codecs | covered |
| A10 | Stream cap: N opens succeed, N+1 gets BUSY13 | test_ambed_wire::test_stream_cap | covered (needs DVT_AMBED_STREAM_CAP) |
| A11 | PCM -> Codec2 produces Codec2 frames | test_ambed_wire::test_pcm_to_codec2 | covered |
| A12 | Codec2 -> PCM is not silent | test_ambed_wire::test_codec2_to_pcm_nonzero | covered |
| A13 | Multi-output replies keep open order and widths; round trip correlates | test_ambed_wire::test_codec2_multi_output_round_trip | covered |
| A14 | Two outputs keep the open order in every reply | test_ambed_wire::test_pcm_two_outputs_keep_order | covered |
| A15 | IMBE follows CAPS: served when advertised (non-zero payloads), refused when not | test_ambed_wire::test_imbe_follows_caps | covered |
| A16 | Morse survives PCM -> AMBE+ / AMBE2+ / Codec2 -> PCM (decoded text exact) | test_morse_codecs::test_morse_through_transcoder[..] | covered |
| A17 | Morse survives PCM -> IMBE -> PCM | - | gap (add IMBE to test_morse_through_transcoder) |
| A18 | CLOSE releases the stream; data after CLOSE is ignored | dvcheck `ambed.data_after_close`; no active case | gap (active case) |
| A19 | Idle stream times out (reference: 3 s) | - | gap |
| A20 | Data from an address other than the opener is ignored | - | gap |
| A21 | Concurrent streams do not mix payloads | - | gap |

## B. xlxd with a transcoder (`netns`)

Under `DVT_SUT_PROFILE=dvrouter` rows B2, B4, B6, B11 and B12 assert the
expectations of `dvtest/specs/dvrouter.py` "netns_ambed" (design D5: AMBEd
outputs opened on demand, extended OPEN only) instead of xlxd's; the dvrouter
run needs an xlxd-compatible DV-Router binary as `DVT_XLXD_BIN`.

| # | Check | Case | Status |
|---|---|---|---|
| B1 | Without USRP endpoints xlxd sends no CAPS | test_xlxd_usrp_netns::test_no_usrp_sends_no_caps | covered |
| B2 | Without USRP endpoints xlxd uses legacy opens (dvrouter: no OPEN at all, no listener in another profile) | test_xlxd_usrp_netns::test_no_usrp_legacy_open | covered |
| B3 | Without USRP endpoints the transcoder link bytes equal upstream master | test_xlxd_usrp_netns::test_no_usrp_link_bytes_equal_reference | covered |
| B4 | Legacy transcoder: a single CAPS probe, then legacy opens (dvrouter: CAPS re-sent on each PONG of the 5 s PING cycle until answered, never a legacy OPEN) | test_xlxd_usrp_netns::test_legacy_ambed_single_caps_probe, ::test_legacy_ambed_digital_uses_legacy_open | covered |
| B5 | Legacy transcoder: PCM streams are not transcoded (and logged once) | test_xlxd_usrp_netns::test_legacy_ambed_pcm_not_transcoded | covered |
| B6 | Extended transcoder: a single CAPS probe and extended opens for PCM (dvrouter: outputs on demand, opens [[24,1,[5]],[24,5,[1]]]) | test_xlxd_usrp_netns::test_extended_single_caps_probe, ::test_extended_opens | covered |
| B7 | D-STAR -> USRP peer: voice then unkey | test_xlxd_usrp_netns::test_extended_dstar_to_usrp | covered |
| B8 | USRP peer -> digital listeners | test_xlxd_usrp_netns::test_extended_usrp_to_digital | covered |
| B9 | No echo back to the originating USRP peer | test_xlxd_usrp_netns::test_extended_no_echo_to_usrp_peer | covered |
| B10 | Malformed frames from a USRP peer are dropped; xlxd survives | test_xlxd_usrp_netns::test_extended_bad_frame_dropped | covered |
| B11 | Transcoder that serves one output: separate opens, distinct transactions (dvrouter: [[24,1,[5]]] twice) | test_xlxd_usrp_netns::test_one_output_opens | covered |
| B12 | Slow open reply: a short burst is still delivered (dvrouter: no OPEN, no digital listener) | test_xlxd_usrp_netns::test_slow_open_short_burst_delivered | covered |
| B13 | Transcoder lost > 30 s and back: CAPS is probed again, PCM transcoding resumes | - (seen in a triage run only) | gap |
| B14 | Transcoder replaced within 30 s: stale CAPS is not used | - | gap (known PR edge case) |

## C. Reflector routing (`rig`)

| # | Check | Case | Status |
|---|---|---|---|
| C1 | Same-mode passthrough is byte-exact on both reflectors | test_xlxd_matrix::test_passthrough_exact[..] | covered |
| C2 | Same-mode passthrough equals the recorded baseline for the reference commit | test_xlxd_matrix::test_passthrough_baseline | covered |
| C3 | Cross-mode cells (D-STAR <-> DMR, M17 -> DMR / D-STAR, DMR / D-STAR -> M17): system under test == reference | test_xlxd_matrix::test_cross_sut_equals_ref[..] | covered |
| C4 | Cross-mode on a USRP-bridged module | test_xlxd_matrix (module B with profile allstar) | covered |
| C5 | Morse ID through a reflector decodes exact on a listener | test_morse_codecs::test_live_id_through_reflector | covered |
| C6 | Interlink between two reflectors (XLX peer) | - | gap |
| C7 | Module change / unlink / relink of clients | - | gap |
| C8 | Stream timeout without a last frame (reference 1.6 s) | dvcheck `header_only` (passive) | gap (active case) |
| C9 | Many modules talking at once (load) | - | gap (street profile, later) |
| C10 | YSF, DPlus, DCS clients | - | gap (no clients yet) |

## D. Resilience (`rig`)

| # | Check | Case | Status |
|---|---|---|---|
| D1 | Transcoder stopped: native passthrough unchanged | test_backend_loss::test_native_passthrough_while_down | covered |
| D2 | Transcoder stopped: no invented voice on other modes | test_backend_loss::test_usrp_pcm_not_fabricated | covered |
| D3 | Transcoder started again: cross-mode recovers | test_backend_loss::test_cross_path_recovers | covered |
| D4 | AllStar node killed while keyed: stream ends, module released | test_allstar::test_node_crash_releases_module | covered |
| D5 | xlxd restart with clients connected | - | gap |

## E. AllStarLink / USRP (`allstar`)

| # | Check | Case | Status |
|---|---|---|---|
| E1 | IAX2 caller reaches M17, D-STAR and DMR listeners at once | test_allstar::test_iax_to_all_digital_listeners | covered |
| E2 | Digital talker reaches the IAX2 caller (tone, KEY/UNKEY) | test_allstar::test_digital_to_iax[..] | covered |
| E3 | Two rooms at once without crosstalk | test_allstar::test_rooms_no_crosstalk | covered |
| E4 | No echo to the USRP origin | test_allstar::test_usrp_no_echo | covered |
| E5 | Morse through AllStar decodes exact | - | gap |

## F. NXDN and P25 peers (`nxdn`, `p25`)

| # | Check | Case | Status |
|---|---|---|---|
| F1 | NXDN reflector peered with xlxd; polls both ways | rig profile nxdn (manual check in a capture run) | planned |
| F2 | NXDN <-> DMR passthrough (same AMBE2+), both directions | cases/test_nxdn.py | planned |
| F3 | NXDN <-> D-STAR / M17 / AllStar through the transcoder; system under test == reference | cases/test_nxdn.py | planned |
| F4 | P25 reflector peered with xlxd | rig profile p25 | planned |
| F5 | P25 (IMBE) <-> DMR / D-STAR / M17 / AllStar through the transcoder | cases/test_p25.py | planned |
| F6 | NXDN and P25 client codecs (offline) | cases/unit/test_nxdn.py, cases/unit/test_p25.py | covered |
| F7 | NXDN and P25 client codecs (offline) | cases/unit/test_nxdn_proto.py, cases/unit/test_nxdn_conf.py, cases/unit/test_p25_proto.py, cases/unit/test_p25_conf.py | covered |

## G. Audio content

| # | Check | Case | Status |
|---|---|---|---|
| G1 | Morse encoder/decoder: speeds, tones, noise, jitter, dropouts, gain, DC, silence | cases/unit/test_morse.py | covered |
| G2 | Morse through Codec2 3200 / 1600 | test_morse_codecs::test_codec2_round_trip[..] | covered (needs c2enc/c2dec) |
| G3 | Speech quality (PESQ) | - | gap (phase 3) |

## H. Passive wire checks (`dvcheck` on every `--capture` run)

| # | Protocol | What is checked | Status |
|---|---|---|---|
| H1 | Transcoder link | request/reply pairing by transaction, STD/BUSY form, open within CAPS, retransmit keeps the stream, every data request answered in open order with correct widths, packet id order, no data after CLOSE, unknown peers | covered |
| H2 | D-STAR (DExtra) | header -> voice -> last frame, frame counter 0..20, constant stream id, sync position, loss/dup/reorder, jitter | covered |
| H3 | DMR (MMDVM) | sequence, voice header -> bursts A..F -> terminator, sizes, traffic after close (with the 1 s upstream client-cache window) | covered |
| H4 | M17 | frame number, last-frame bit, constant LICH/stream id, sizes | covered |
| H5 | USRP | sequence, 352-byte keyed / 32-byte unkey, no voice after unkey | covered |
| H6 | IAX2 | oseqno/iseqno, ACK for every full frame, timestamps, mini-frame continuity, voice size per codec, call lifecycle | covered |
| H7 | NXDN | poll/echo pairing, frame layout, sequence, header -> terminator | planned |
| H8 | P25 | poll, LDU1/LDU2 sequence and sizes, header/terminator | planned (checker written, needs a capture) |
| H9 | pcap/pcapng reader | link types, byte orders, timestamp units, skips | covered (cases/unit/test_pcap.py) |

## I. Library (`unit`)

| # | Check | Case | Status |
|---|---|---|---|
| I1 | Transcoder protocol encoders/parsers and FakeAmbed modes | cases/unit/test_ambed.py | covered |
| I2 | DExtra, DMR, M17, USRP, UDP client | cases/unit/test_protocols.py | covered |
| I3 | IAX2 client | cases/unit/test_iax2.py | covered |
| I4 | Signals, scoring (Goertzel, LSD, spectrum) | cases/unit/test_audio.py | covered |
| I5 | Target settings from env/TOML | cases/unit/test_target.py | covered |
| I6 | Client helpers (fresh stream ids, wire frames) | cases/unit/test_clients.py | covered |
| I7 | dvcheck rules with injected faults | cases/unit/test_check_*.py | covered |

## J. Repository hygiene

| # | Check | How | Status |
|---|---|---|---|
| J1 | No home paths, emails, public IPs, device paths/serials, ELF files, local words (callsigns, private names) in files or git metadata | scripts/pii-check.sh (also run by run.sh) | covered |
| J2 | Pinned sources verified (git commit, sha256 for downloads) | scripts/pre-fetch-*.sh, fetch_verified | covered |
| J3 | No audio, bitstreams or captures committed | .gitignore + pii-check ELF rule | covered (audio by .gitignore only) |

## K. Protocol conformance (`proto`)

One protocol at a time, each obligation of `docs/protocols/<protocol>.md`
as one case in `cases/proto/test_<protocol>.py` (markers `proto` and `rig`).
"hb" = the system under test is held to the protocol obligation itself
(xlxd-ref is recorded); "dev" = xlxd-ref deviates from the protocol: under
the `xlxd` profile sut == ref, under another profile its spec entry (spec
pending -> skip); "diff" = sut == ref in the same run; "spec" = judged by
`DVT_SUT_PROFILE` (`dvtest/specs/<profile>.py`); "slow" = needs the server
timeout (about 100 s on xlxd), also marked `slow`. Egress embedded LC and
sync patterns: dvcheck H3. "probe" = an active case drives the packets;
"dvcheck" = the passive capture check also covers it. Where no reference
endpoint exists (USRP, the NXDN server): "conv" = an implementation
convention the clients rely on, asserted on the system under test for every
profile; "real" = checked against the real chan_usrp in the AllStar
container.

### K1. MMDVM Homebrew / DMR (`docs/protocols/mmdvm.md`)

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DMR-01 | RPTL from a known id -> RPTACK + 4-byte salt | test_mmdvm::test_mmdvm_login_ack_salt | hb | probe | covered |
| P-DMR-02 | Salt not constant across master restarts | test_mmdvm::test_mmdvm_salt_changes_on_restart | diff | probe (rig_control restart) | covered |
| P-DMR-03 | RPTL from an unknown id: no login (silence or MSTNAK) | test_mmdvm::test_mmdvm_login_unknown_id | dev | probe | covered |
| P-DMR-04 | RPTK completes the login; hash check strict vs lenient (zero hash) | test_mmdvm::test_mmdvm_auth_zero_hash | dev | probe | covered |
| P-DMR-05 | RPTK without a preceding RPTL | test_mmdvm::test_mmdvm_auth_without_login | dev | probe | covered |
| P-DMR-06 | RPTC 302 B -> RPTACK; RPTK/RPTC/RPTO acks carry the 4-byte id (RPTL ack carries the salt) | test_mmdvm::test_mmdvm_config_ack | hb + dev | probe | covered |
| P-DMR-07 | RPTC short/long not acknowledged, login kept | test_mmdvm::test_mmdvm_config_wrong_size | dev | probe | covered |
| P-DMR-08 | Voice routed without RPTC | test_mmdvm::test_mmdvm_no_config_still_routed | dev | probe | covered |
| P-DMR-09 | RPTO -> RPTACK | test_mmdvm::test_mmdvm_options_ack | diff | probe | covered |
| P-DMR-10 | Re-login while logged in: one session, traffic once | test_mmdvm::test_mmdvm_relogin_single_session | diff | probe | covered |
| P-DMR-11 | RPTPING -> MSTPONG + id (id byte order recorded) | test_mmdvm::test_mmdvm_ping_pong | dev | probe + dvcheck | covered |
| P-DMR-12 | RPTPING from a non-logged-in client: no MSTPONG; reply kind (silence or MSTNAK) recorded | test_mmdvm::test_mmdvm_ping_not_logged_in | dev | probe | covered |
| P-DMR-13 | Silent client dropped after the server timeout, MSTCL sent; reply to its next packet recorded | test_mmdvm::test_mmdvm_silent_client_dropped | dev | probe | covered (slow) |
| P-DMR-14 | Dropped client gets no further voice | test_mmdvm::test_mmdvm_dropped_client_no_voice | diff | probe | covered (slow) |
| P-DMR-15 | RPTCL 13 B / 9 B closes the session | test_mmdvm::test_mmdvm_rptcl_close[13\|9] | dev (B15) | probe | covered |
| P-DMR-16 | RPTINTR ignored | test_mmdvm::test_mmdvm_rptintr_ignored | diff | probe | covered |
| P-DMR-17 | Unknown / short / oversize packets ignored, master keeps serving | test_mmdvm::test_mmdvm_garbage_ignored[unknown\|short3\|rptl7\|oversize] | diff | probe | covered |
| P-DMR-18 | DMRD not 55 B ignored | test_mmdvm::test_mmdvm_dmrd_wrong_size | dev | probe | covered |
| P-DMR-19 | DMRD from a non-logged-in address not relayed; MSTNAK and its rate per 10 s recorded | test_mmdvm::test_mmdvm_dmrd_not_logged_in | hb + dev | probe | covered |
| P-DMR-20 | Slot 1 header/voice/terminator ignored | test_mmdvm::test_mmdvm_slot1_ignored | diff | probe | covered |
| P-DMR-21 | Private-call voice not relayed | test_mmdvm::test_mmdvm_private_call_not_relayed | diff | probe | covered |
| P-DMR-22 | Header needs data type 1 + valid data-sync pattern | test_mmdvm::test_mmdvm_header_sync_required | hb + dev | probe | covered |
| P-DMR-23 | 6-burst superframes, three AMBE+2 frames per burst, relayed unchanged | test_mmdvm::test_mmdvm_superframe_relayed_exact | diff | probe + dvcheck | covered |
| P-DMR-24 | Stream ends with a type-2 terminator | test_mmdvm::test_mmdvm_terminator_ends_stream | hb | probe | covered |
| P-DMR-25 | Egress structure: header, A-F voice, terminator, slot 2, group, one sid; dst TG recorded | test_mmdvm::test_mmdvm_egress_structure | hb + diff | probe + dvcheck | covered |
| P-DMR-26 | Egress sequence byte counts up per stream | test_mmdvm::test_mmdvm_egress_sequence | diff | probe | covered |
| P-DMR-27 | Egress source id and repeater id | test_mmdvm::test_mmdvm_egress_ids | diff + dev | probe | covered |
| P-DMR-28 | Egress stream id (folded on xlxd, kept under dvrouter) | test_mmdvm::test_mmdvm_egress_stream_id | dev (B10) | probe | covered |
| P-DMR-29 | TG 4001..4026 links module A..Z | test_mmdvm::test_mmdvm_tg_links_module[A\|Z] | diff | probe | covered |
| P-DMR-30 | TG 4000 unlinks; no further voice | test_mmdvm::test_mmdvm_tg4000_unlinks | diff | probe | covered |
| P-DMR-31 | Talk to another module's TG while linked stays on the linked module | test_mmdvm::test_mmdvm_other_tg_stays_on_module | diff | probe | covered |
| P-DMR-32 | TG 9 / unmapped TG: linked -> own module, unlinked -> ignored | test_mmdvm::test_mmdvm_tg9_routing[linked\|unlinked] | diff | probe | covered |
| P-DMR-33 | Join by an empty stream; join not relayed as voice | test_mmdvm::test_mmdvm_join_by_empty_stream | diff | probe | covered |
| P-DMR-43 | DMRGateway form: 3 private headers + 2 terminators link 400x / unlink 4000, burst not relayed | test_mmdvm::test_mmdvm_gateway_private_link[login_id\|rf_id] | diff | probe | covered |
| P-DMR-34 | Lost terminator: stream ends after the silence timeout, module free | test_mmdvm::test_mmdvm_lost_terminator_timeout | spec (B3) | probe | covered |
| P-DMR-35 | Second header on a busy module not mixed in | test_mmdvm::test_mmdvm_busy_module_no_mix | spec (B1/B2) | probe | covered |
| P-DMR-36 | Voice without a header: relayed or not; when relayed, a header comes first | test_mmdvm::test_mmdvm_voice_without_header | dev | probe | covered |
| P-DMR-37 | Talker never gets its own stream back | test_mmdvm::test_mmdvm_no_self_echo | diff | probe | covered |
| P-DMR-41 | Non-voice data bursts (data type >= 3) not relayed, open no stream | test_mmdvm::test_mmdvm_data_burst_not_voice[csbk\|data_header\|rate12\|rate34] | dev | probe | covered |
| P-DMR-42 | Ingress sequence wraps 255 -> 0 in a long over: no loss, one stream | test_mmdvm::test_mmdvm_sequence_wrap | hb | probe | covered |
| P-DMR-38 | Two ids from one IP: two sessions, traffic once each | test_mmdvm::test_mmdvm_two_ids_one_ip | diff | probe | covered |
| P-DMR-39 | Same id from two ports: traffic exactly once per port | test_mmdvm::test_mmdvm_same_id_two_ports | dev | probe | covered |
| P-DMR-40 | Keepalive of one client does not refresh another | test_mmdvm::test_mmdvm_keepalive_isolated | diff | probe | covered (slow) |

### K2. DExtra / D-STAR (`docs/protocols/dextra.md`)

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DXT-01 | Link to a served module -> 14 B ACK (bytes 0-9 + ACK 0x00) | test_dextra::test_dextra_link_ack | diff | probe + dvcheck | covered |
| P-DXT-02 | ACK latency recorded (xlxd: random 0-2 s) | test_dextra::test_dextra_ack_latency | dev | probe | covered |
| P-DXT-03 | Link to a module not served -> NAK, no link | test_dextra::test_dextra_link_unserved_module_nak | diff | probe | covered (skips unless DVT_UNSERVED_MODULE names a letter the sut does not serve) |
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
| P-DXT-38 | Module change via unlink + relink | test_xlxd_clients::test_module_change_follows_talker[xlxd_ref-dstar\|xlxd_sut-dstar] | diff | probe | covered (C7) |
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

### K3. DPlus / D-STAR (`docs/protocols/dplus.md`)

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

### K4. DCS / D-STAR (`docs/protocols/dcs.md`)

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

### K5. P25 network (`docs/protocols/p25.md`)

`ref-chain` = probed at the P25 endpoint of each chain (`DVT_P25_REF` /
`DVT_P25_SUT`); sections 4-5 of the doc need a transcoder that serves IMBE
(skip otherwise).

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-P25-01 | Poll linked and echoed byte for byte | test_p25_network::test_p25_poll_echo | diff | probe + dvcheck (ref-chain) | covered |
| P-P25-02 | No unsolicited polls to a client | test_p25_network::test_p25_no_server_polls | diff | probe + dvcheck (ref-chain) | covered |
| P-P25-03 | Poll of another size ignored | test_p25_network::test_p25_poll_wrong_size[10\|12] | dev | probe (ref-chain) | covered |
| P-P25-04 | Repeated polls: one session, traffic once | test_p25_network::test_p25_repoll_single_session | diff | probe | covered |
| P-P25-05 | Two clients on one IP: two sessions | test_p25_network::test_p25_two_ports_two_sessions | diff | probe | covered |
| P-P25-06 | Unlink stops traffic, no reply | test_p25_network::test_p25_unlink | diff | probe | covered |
| P-P25-07 | Unlink from an unknown address ignored | test_p25_network::test_p25_unlink_unknown | diff | probe | covered |
| P-P25-08 | Silent client dropped (timeout recorded) | test_p25_network::test_p25_silent_client_dropped | dev | probe | covered (slow) |
| P-P25-09 | Dropped client gets no voice | test_p25_network::test_p25_dropped_client_no_voice | diff | probe | covered (slow) |
| P-P25-10 | Relay to every other linked client | test_p25_network::test_p25_relay_all_listeners | diff | probe | covered |
| P-P25-11 | No echo to the talker | test_p25_network::test_p25_no_echo | diff | probe | covered |
| P-P25-12 | Records from an unlinked address dropped | test_p25_network::test_p25_unlinked_sender_dropped | diff | probe | covered |
| P-P25-13 | One talker per room, no mixing | test_p25_network::test_p25_second_talker_dropped | spec (B1) | probe | covered |
| P-P25-14 | 0x80 relayed, floor freed at once | test_p25_network::test_p25_terminator_frees_floor | dev | probe | covered |
| P-P25-15 | Lost 0x80: floor free after the timeout, terminator recorded | test_p25_network::test_p25_stream_timeout | spec (B3) | probe | covered |
| P-P25-16 | Wrong-size records not relayed | test_p25_network::test_p25_record_wrong_size | dev | probe | covered |
| P-P25-17 | Unknown record types not relayed | test_p25_network::test_p25_unknown_type_dropped | dev | probe | covered |
| P-P25-18 | Short / oversize garbage ignored | test_p25_network::test_p25_garbage_ignored[short\|oversize] | diff | probe | covered |
| P-P25-19 | Egress 0x62..0x73 cycle from 0x62 | test_p25_network::test_p25_egress_superframe | hb | probe + dvcheck | covered |
| P-P25-20 | Record sizes and IMBE offsets | test_p25_network::test_p25_egress_record_layout | diff | probe + dvcheck | covered |
| P-P25-21 | 0x64 LCF 0x00, MFID 0x00 | test_p25_network::test_p25_egress_lcf | hb | probe | covered |
| P-P25-22 | 0x65 = room TG | test_p25_network::test_p25_egress_dst_tg | dev | probe | covered |
| P-P25-23 | 0x66 = talker id; unknown callsign recorded | test_p25_network::test_p25_egress_src_id[known\|unknown] | dev | probe | covered |
| P-P25-24 | 0x80 after a completed LDU | test_p25_network::test_p25_egress_terminator_ldu_boundary | dev | probe + dvcheck (terminator_in_ldu) | covered |
| P-P25-25 | No lone 0x80 for an empty stream | test_p25_network::test_p25_no_lone_terminator | dev | probe + dvcheck (stray_terminator) | covered |
| P-P25-26 | Short call padded to a superframe | test_p25_network::test_p25_short_call_padded | dev | probe | covered |
| P-P25-27 | ALGID 0x80, KID 0, MI 0 | test_p25_network::test_p25_egress_clear_es | hb | probe | covered |
| P-P25-28 | LC/ES parity and LSD recorded | test_p25_network::test_p25_egress_parity_recorded | dev (record) | probe | covered |
| P-P25-29 | Egress pacing (no long bursts) | test_p25_network::test_p25_egress_pacing | diff | probe + dvcheck | covered |
| P-P25-30 | Call opens without header, no frames lost | test_p25_network::test_p25_ingress_no_frame_loss | dev | probe | covered |
| P-P25-31 | Late start (not at 0x62) carried | test_p25_network::test_p25_ingress_late_start | dev | probe | covered |
| P-P25-32 | Ingress 0x65 not used for routing | test_p25_network::test_p25_ingress_other_tg | diff | probe | covered |
| P-P25-33 | Unknown source id handled | test_p25_network::test_p25_ingress_unknown_src | dev | probe | covered |
| P-P25-34 | Lost records do not end the call | test_p25_network::test_p25_ingress_record_loss | diff | probe | covered |
| P-P25-35 | Encrypted ingress not transcoded | test_p25_network::test_p25_ingress_encrypted | dev | probe | covered |
| P-P25-36 | One room per port | test_p25_network::test_p25_room_per_port | diff | probe | covered |
| P-P25-37 | Cross-mode via IMBE | cases/test_p25.py (F5) | diff | probe | covered |

### K6. USRP (`docs/protocols/usrp.md`)

xlxd-ref has no USRP endpoint: "conv" rows are asserted on the system under
test, "dev" rows are recorded under the `xlxd` profile.

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

### K7. NXDN reflector network, peer and server (`docs/protocols/nxdn.md`)

`test_nxdn_peer` judges xlxd as a peer of an NXDNReflector (rig profile
`nxdn`); `test_nxdn_server` judges the server side toward gateways.

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

K8 (M17, `docs/protocols/m17.md`), K9 (YSF, `docs/protocols/ysf.md`) and
K10 (XLX interlink, `docs/protocols/xlx.md`) are reserved for those suites
when they land.

## Upstream behaviour the cases accept

Known xlxd behaviour that is not a defect of the build under test; cases and
dvcheck report it as a warning with a note:

- M17 -> D-STAR drops the two Codec2 frames of M17 frame 0.
- A talker that stops without a last frame: its stream times out after 1.6 s and
  every egress still gets an end marker (D-STAR last frame, DMR terminator, M17 EOT; C8, P-DMR-34).
- After MSTCL a DMR peer can still receive up to 1 s of stream (client cache).
- A 9-byte RPTCL is ignored by xlxd.
- With the transcoder down, cross-mode frames are all zero and D-STAR frame 0 has no sync.
- Legacy opens always ask for IMBE as the third output.
- MMDVM (section K1): RPTL from an unknown id gets no reply (not MSTNAK);
  RPTK hash not checked and accepted without a prior RPTL; MSTPONG carries
  the id little endian; neither RPTCL form closes the session; egress
  destination is always TG 9; egress stream id is the 16-bit fold of the
  ingress id; egress repeater id is the source id, not the talker's login
  id; RPTACK to RPTK/RPTC/RPTO carries no id. A DMR stream that times out
  still ends with a terminator on DMR egress (measured, P-DMR-34).
- DExtra (section K2): first ACK delayed 0-2 s; unlink by the talker lost (the client stays linked, keepalives and later streams keep coming); RPT2 module routes a rev 0 header; last frame AMBE 55 C8 7A + zeros / slow 25 1A C6 and the previous frame's slow data set to 55 55 55; empty MY suffix filled DXTR; timeout notice 10 spaces + 0x00.
- DPlus (section K3): a disconnect from an address that is not linked is
  not echoed; a linked client hears its module only after it has
  transmitted; egress end frame byte 8 = 81 with the tail
  `55 C8 7A 55 x9 25 1A C6` (JARL pattern not at 26-31); a malformed-callsign
  login gets BUSY (source: from the 4th the IP is ignored 5 / 30 / 60 min).
- DCS (section K4): ACK/NAK after a random 0-2 s, and after a NAK the IP is
  ignored up to 60 min; unlink by the talker lost (stream runs on), unlink
  from an unknown address unanswered; last packet payload 55 C8 7A + zeros /
  25 1A C6 and an empty MY suffix filled `DCS`; ingress stream id 0 refused.
- P25 (section K5): xlxd is no P25 server (the server rows compare two
  P25Reflectors); records before the first 0x62 are dropped; an unknown
  callsign gets a hashed source id; 0x80 ends inside an incomplete LDU,
  short calls are not padded and a DMR join sends a lone 0x80 (P25Reflector
  itself sends no terminator after a silence timeout).
- USRP (section K6): pyUC's 352-byte keyup-0 unkey is dropped as malformed
  (the over ends on the 100 ms timeout with 49 of 50 frames); ingress
  SET_INFO is ignored (the talker stays the endpoint row's callsign); xlxd
  never sends SET_INFO.
- NXDN (section K7): xlxd never sends NXDNU; the header SACCH is 18 zero
  bits, not IDLE; the AMBE+2 gain index is stepped -0.3 dB both ways;
  headerless voice is dropped (no late entry), a FACCH1-stolen half is
  decoded as voice and an empty DMR join goes out as VCALL + TX_REL.
- Short-stream guard (B18): xlxd's loop guard counts short streams per module by callsign, so across protocols. It blocks with the 3 / 10 / 300 s ladder and enforces the block only on DExtra/DPlus/DCS (DMR and M17 pass no module to the gatekeeper). At strike 3 it refuses links and disconnects the talker, and it stops acking YSF polls of the callsign (MayLink on every poll). Measured 2026-10-30: out/20261030T062417Z-i13, out/20261030T063238Z-i13 (decay), out/20261030T064052Z-i13 (per-protocol refusals).

## DV-Router profile (`DVT_SUT_PROFILE=dvrouter`)

Behaviour cases judge the system under test against `dvtest/specs/dvrouter.py`
(source: the DV-Router floor-control design). Under the default `xlxd`
profile the same cases stay differential (system under test == reference).
Interop rows stay differential for every profile.

| # | Requirement | Case | Kind | Status |
|---|---|---|---|---|
| B1 | Two clients key up on one module: exactly one winner, no mixing | test_floor::test_first_talker_wins | behaviour | covered |
| B2 | Late entry denied: a second header on a busy module is never relayed | test_floor::test_late_entry_denied | behaviour | covered |
| B2p | `pending` knob: relayed live from close, transcoded, no stale replay | test_floor::test_late_entry_pending | behaviour | placeholder |
| B3 | Silence timeout 1.6 s: synthesized terminator on every egress; USRP unkey after 100 ms; busy module suppresses a USRP over | test_xlxd_clients::test_stream_timeout_frees_module, test_floor::test_usrp_unkey_after_silence, test_floor::test_usrp_over_suppressed_when_busy | behaviour | covered |
| B4 | TOT 180 s: revoke plus terminator | test_xlxd_clients::test_tot_180s | behaviour (slow) | covered |
| B5 | Immediate revoke after unlink/disconnect | test_xlxd_clients::test_leave_mid_stream_tail[..] | behaviour | covered |
| B6 | No zero-fill on single-codec egress | test_backend_loss::test_native_passthrough_while_down, ::test_usrp_pcm_not_fabricated | behaviour | covered |
| B6x | XLX rev 3 egress keeps the frame with the slot zeroed | test_xlxd_interlink::test_interlink_missing_output_slot_zeroed | behaviour | covered |
| B7 | Extended 24-byte OPEN for every stream when CAPS answers | test_xlxd_usrp_netns::test_no_usrp_* (specs.rule("ambed_open")), dvcheck | behaviour | covered (dvcheck rule planned) |
| B8 | Admission: reject sid 0, one floor per client; same sid in two rooms allowed | test_floor::test_admission_sid_zero, ::test_admission_one_floor_per_client, ::test_admission_same_sid_two_rooms | behaviour | covered |
| B8r | Remote-origin sid already open in another room is rejected | test_xlxd_interlink::test_admission_remote_sid_open_elsewhere | behaviour | covered |
| B9 | No echo to the origin IP:port, USRP included | test_allstar::test_usrp_no_echo, test_xlxd_usrp_netns::test_extended_no_echo_to_usrp_peer | interop | covered |
| B10 | Stream id preserved on same-protocol egress (D-STAR, DMR, M17, XLX, DExtra) | test_floor::test_sid_preserved[..] | interop | covered |
| B10x | Cross-protocol egress carries the canonical id (low 16 bits to D-STAR/M17, zero-extended to DMR) | test_floor::test_sid_cross_mapping[..] | behaviour | covered |
| B10p | Patch of one stream to two modules of the same peer rewrites the second leg's id | test_floor::test_sid_patch_second_leg_rewritten | behaviour | placeholder |
| B11 | Cross-family transit allowed; XLX/BM never re-forwarded to XLX/BM | test_xlxd_interlink::test_interlink_no_loop, ::test_interlink_no_xlx_transit, ::test_interlink_cross_family_transit, dvcheck xlx.transit | interop | covered |
| B12 | Header CRC/hash not verified (tester zeros/spaces accepted) | dvtest/specs/dvrouter.py NOT_CHECKED | - | recorded |
| B13 | No frame loss under load; SUT forwarding latency p99 <= 2 ms (same-mode, wire-measured) | test_load::test_no_load_loss_*, test_load::test_dvrouter_forwarding_latency | behaviour | covered |
| B14 | Per-room isolation: killing one room's controller loses nothing elsewhere | test_floor::test_b14_placeholder | behaviour | placeholder |
| B15 | DMR RPTCL (13 B and 9 B) closes the session: stream ends, floor released | test_floor::test_dmr_rptcl_closes_session[13\|9] | behaviour | covered |
| B16 | No voice before the header on any egress; no echo to the talker under any sid | test_floor::test_no_voice_before_header, ::test_no_self_echo_other_sid | behaviour | covered (self echo recorded only under xlxd) |
| B16p | `pending` entrant starts with a header | test_floor::test_pending_entrant_starts_with_header | behaviour | placeholder |
| B17 | M17 frame 0 is relayed | test_floor::test_m17_frame0_relayed | behaviour | covered |
| B18 | Short-stream guard: 3 short streams (< 50 frames, each within 2 s of the previous close) -> TX of that callsign refused silently 3 s, then 10 s, then 300 s; strikes decay after 300 s; per callsign, across protocols; third strike refuses new links of local clients with the protocol's refusal (DExtra NAK, DPlus BUSY, DCS NAK, MMDVM MSTNAK, M17 NACK, YSF no poll reply); existing sessions kept, a linked YSF session keeps poll replies (xlxd: talker disconnected, YSF polls no longer acked, DMR/M17 overs not blocked) | test_short_stream_guard::test_short_stream_guard_first_block, ::_second_block, ::_per_callsign, ::_cross_protocol_block, ::_cross_protocol_strikes, ::_third_strike, ::_third_strike_protocols, ::_decay (slow) | behaviour | covered |

Ingress late entry is allowed where the protocol carries in-band LC (DMR embedded LC, M17 LSF in every packet, DCS header in every packet); B16 judges egress (no voice before its header on any receiver).

Measured on xlxd (reference and PR build alike): a late talker on a busy
module is relayed after the first stream ends (late-entry promotion), a USRP
unkey arrives 99-101 ms after the last voice, a same-sid stream in a second
room is dropped, xlxd keeps the D-STAR stream id but not the DMR or M17 one, and
across protocols it maps the id only for D-STAR -> DMR and M17 -> D-STAR.
A late entrant on DV-Router is denied silently (these protocols have no
stream-level NACK).

Also measured on xlxd: after RPTCL (13 B or 9 B) the floor stays held by the
closed stream (next talker hears 0 frames); a promoted late D-STAR entrant
reaches D-STAR listeners with 80-81 voice packets and no header, and is echoed
back to its sender (71 frames) in some runs only (timing-dependent); M17 egress
carries 42 of 44 frames (both frames of packet 0 dropped); added forwarding
latency p50 171-222 ms at 26 modules.
