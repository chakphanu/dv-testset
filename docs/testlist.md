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
"dvcheck" = the passive capture check also covers it.

### K1. MMDVM Homebrew / DMR (`docs/protocols/mmdvm.md`)

| # | Obligation | Case | Judged | How | Status |
|---|---|---|---|---|---|
| P-DMR-01 | RPTL from a known id -> RPTACK + 4-byte salt | test_mmdvm::test_mmdvm_login_ack_salt | hb | probe | covered |
| P-DMR-02 | Salt not constant across master restarts | test_mmdvm::test_mmdvm_salt_changes_on_restart | diff | probe (rig_control restart) | covered |
| P-DMR-03 | RPTL from an unknown id: no login (silence or MSTNAK) | test_mmdvm::test_mmdvm_login_unknown_id | dev | probe | covered |
| P-DMR-04 | RPTK completes the login; hash check strict vs lenient (zero hash) | test_mmdvm::test_mmdvm_auth_zero_hash | dev | probe | covered |
| P-DMR-05 | RPTK without a preceding RPTL | test_mmdvm::test_mmdvm_auth_without_login | dev | probe | covered |
| P-DMR-06 | RPTC 302 B -> RPTACK | test_mmdvm::test_mmdvm_config_ack | hb | probe | covered |
| P-DMR-07 | RPTC short/long not acknowledged, login kept | test_mmdvm::test_mmdvm_config_wrong_size | dev | probe | covered |
| P-DMR-08 | Voice routed without RPTC | test_mmdvm::test_mmdvm_no_config_still_routed | dev | probe | covered |
| P-DMR-09 | RPTO -> RPTACK | test_mmdvm::test_mmdvm_options_ack | diff | probe | covered |
| P-DMR-10 | Re-login while logged in: one session, traffic once | test_mmdvm::test_mmdvm_relogin_single_session | diff | probe | covered |
| P-DMR-11 | RPTPING -> MSTPONG + id (id byte order recorded) | test_mmdvm::test_mmdvm_ping_pong | dev | probe + dvcheck | covered |
| P-DMR-12 | RPTPING from a non-logged-in client: no MSTPONG | test_mmdvm::test_mmdvm_ping_not_logged_in | diff | probe | covered |
| P-DMR-13 | Silent client dropped after the server timeout, MSTCL sent | test_mmdvm::test_mmdvm_silent_client_dropped | dev | probe | covered (slow) |
| P-DMR-14 | Dropped client gets no further voice | test_mmdvm::test_mmdvm_dropped_client_no_voice | diff | probe | covered (slow) |
| P-DMR-15 | RPTCL 13 B / 9 B closes the session | test_mmdvm::test_mmdvm_rptcl_close[13\|9] | dev (B15) | probe | covered |
| P-DMR-16 | RPTINTR ignored | test_mmdvm::test_mmdvm_rptintr_ignored | diff | probe | covered |
| P-DMR-17 | Unknown / short / oversize packets ignored, master keeps serving | test_mmdvm::test_mmdvm_garbage_ignored[unknown\|short3\|rptl7\|oversize] | diff | probe | covered |
| P-DMR-18 | DMRD not 55 B ignored | test_mmdvm::test_mmdvm_dmrd_wrong_size | dev | probe | covered |
| P-DMR-19 | DMRD from a non-logged-in address not relayed | test_mmdvm::test_mmdvm_dmrd_not_logged_in | hb | probe | covered |
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
| P-DMR-34 | Lost terminator: stream ends after the silence timeout, module free | test_mmdvm::test_mmdvm_lost_terminator_timeout | spec (B3) | probe | covered |
| P-DMR-35 | Second header on a busy module not mixed in | test_mmdvm::test_mmdvm_busy_module_no_mix | spec (B1/B2) | probe | covered |
| P-DMR-36 | Voice without a header not relayed | test_mmdvm::test_mmdvm_voice_without_header | dev | probe | covered |
| P-DMR-37 | Talker never gets its own stream back | test_mmdvm::test_mmdvm_no_self_echo | diff | probe | covered |
| P-DMR-41 | Non-voice data bursts (data type >= 3) not relayed, open no stream | test_mmdvm::test_mmdvm_data_burst_not_voice[csbk\|data_header\|rate12\|rate34] | dev | probe | covered |
| P-DMR-42 | Ingress sequence wraps 255 -> 0 in a long over: no loss, one stream | test_mmdvm::test_mmdvm_sequence_wrap | hb | probe | covered |
| P-DMR-38 | Two ids from one IP: two sessions, traffic once each | test_mmdvm::test_mmdvm_two_ids_one_ip | diff | probe | covered |
| P-DMR-39 | Same id from two ports: traffic exactly once per port | test_mmdvm::test_mmdvm_same_id_two_ports | dev | probe | covered |
| P-DMR-40 | Keepalive of one client does not refresh another | test_mmdvm::test_mmdvm_keepalive_isolated | diff | probe | covered (slow) |

## Upstream behaviour the cases accept

Known xlxd behaviour that is not a defect of the build under test; cases and
dvcheck report it as a warning with a note:

- M17 -> D-STAR drops the two Codec2 frames of M17 frame 0.
- A stream that times out is closed without a last frame.
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
