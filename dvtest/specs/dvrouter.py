# SPDX-License-Identifier: MIT
"""DV-Router behaviour spec (DVT_SUT_PROFILE=dvrouter).

Source: DV-Router docs/design/floor-control.md ("Floor policy", "Interop
obligations", "Cross-room state"), commit 8925abf. SPEC maps a behaviour
case (test function name) to what that case expects of the sut; a case
without an entry skips with "spec pending". Interop cases (wire format,
sid preservation B10, no echo B9, no XLX/BM re-forwarding B11) are always
judged against xlxd-ref and need no entry here.
"""

SPEC = {
    # B1 first talker: exactly one winner, no mixing (which one is open)
    "test_first_talker_wins": {"winners": 1},
    # B2 late entry default deny, silent (no NACK), never promoted later
    "test_late_entry_denied": {"late_entry": "deny"},
    # B2 knob "pending" (not configured by default: the case skips)
    "test_late_entry_pending": {"late_entry": "deny"},
    # C11: the DMR terminator releases the floor at once, so a header 30 ms
    # later is a normal grant (transcoded); no close window
    "test_late_entry_is_native_only": {"after_terminator": "granted"},
    # B3 silence timeout 1.6 s: every egress gets a synthesized terminator
    "test_stream_timeout_frees_module": {"terminator": "all_egress", "timeout_s": 1.6},
    # B3 USRP: unkey after 100 ms without voice; a busy module suppresses the over
    "test_usrp_unkey_after_silence": {"unkey_ms": 100},
    "test_usrp_over_suppressed_when_busy": {"usrp_over_when_busy": "suppress"},
    # B4 TOT 180 s: revoke + terminator
    "test_tot_180s": {"tot_s": 180, "terminator": True},
    # B5 immediate revoke on leave / disconnect
    "test_leave_mid_stream_tail": {"revoke": "immediate"},
    # B6 no zero fill on single-codec egress
    "test_native_passthrough_while_down": {"zero_fill": False},
    "test_usrp_pcm_not_fabricated": {"zero_fill": False},
    # module release after a peer dies: same expectation as on xlxd
    "test_node_crash_releases_module": {},
    # B8 admission
    "test_admission_sid_zero": {"admit_sid_zero": False},
    "test_admission_one_floor_per_client": {"admit_second_floor": False},
    "test_admission_same_sid_two_rooms": {"admit_same_sid_local": True},
    "test_admission_remote_sid_open_elsewhere": {"remote_sid_open_elsewhere": "deny"},
    # B6 XLX revision 3 egress: missing transcoded output leaves its slot zeroed
    "test_interlink_missing_output_slot_zeroed": {"xlx_missing_output": "zero_slot"},
    # B15 RPTCL (13 B and 9 B) closes the session and releases the floor
    "test_dmr_rptcl_closes_session": {"rptcl": "close"},
    # B16 no voice before its header on any egress; no self echo under any sid
    "test_no_voice_before_header": {"strict": True},
    "test_no_self_echo_other_sid": {"strict": True},
    "test_pending_entrant_starts_with_header": {"late_entry": "deny"},
    # B17 M17 frame number 0 payload is relayed
    "test_m17_frame0_relayed": {"fn0": "relay"},
    # D5 restart: all state in memory; no stale stream after a restart (voice
    # under the old sid is dropped until a new header after the relink),
    # and clients that did not relink get nothing
    "test_restart_no_stale_stream": {"stale": "none", "old_links": "none"},
    # B18 short-stream guard: xlxd-compatible ladder (3 short streams < 50
    # frames, each within 2 s -> TX refused silently 3 s / 10 s / 300 s;
    # strikes decay after 300 s), per callsign, across protocols and rooms;
    # the third strike refuses new links of local clients (NAK / BUSY /
    # MSTNAK / NACK, or no poll reply on YSF / NXDN / P25), sessions kept
    "test_short_stream_guard_first_block": {"block_s": 3, "reply_while_blocked": 0},
    "test_short_stream_guard_second_block": {"block_s": 10},
    "test_short_stream_guard_per_callsign": {"other_heard": True},
    "test_short_stream_guard_cross_protocol_block": {"dmr_heard": False, "m17_heard": False},
    "test_short_stream_guard_cross_protocol_strikes": {"struck": True},
    "test_short_stream_guard_third_strike": {"new_link": "refused", "existing_kept": True, "third_blocks_tx": True},
    "test_short_stream_guard_decay": {"block_after_decay_s": 3},
    "test_short_stream_guard_third_strike_protocols": {
        "new_link_by_protocol": {"dextra": "NAK", "dplus": "BUSY", "dcs": "NAK", "dmr": "MSTNAK", "m17": "NACK",
                                 "ysf": "none"},
        "ysf_existing_poll_ack": True},
    # F5 digital -> P25: padded to whole LDUs (P-P25-24); 63 tone frames in
    "test_digital_to_p25": {"received": {"dstar": 63, "dmr": 63, "m17": 72}, "whole_ldu": True},
    # B13 no frame loss under load (C9 path)
    "test_no_load_loss_passthrough": {"loss": 0},
    "test_no_load_loss_cross": {"loss": 0},
    "test_no_load_loss_latency": {"loss": 0},
    # B7 AMBEd open policy, read by the netns link cases through specs.rule
    "ambed_open": "extended_always",
    # netns transcoder-link cases (test_xlxd_usrp_netns, design D5): AMBEd
    # outputs are opened on demand, only for listeners in another profile,
    # and only with the 24 B extended OPEN (never a legacy OPEN)
    "netns_ambed": {
        "no_usrp_opens": [],                        # B2: no listener in another profile
        "caps_probe": {"period_s": 5.0, "tolerance_s": 0.3, "min_probes": 2},  # B4: one per 5 s PING/PONG cycle until answered
        "legacy_open": False,                       # B4: never a legacy OPEN
        "extended_opens": [[24, 1, [5]], [24, 5, [1]]],         # B6
        "one_output_opens": [[24, 1, [5]], [24, 1, [5]]],       # B11
        "slow_open_opens": [],                      # B12: no digital listener
    },
    # netns transcoder-lifecycle cases (test_xlxd_transcoder_lifecycle, B13/B14);
    # DV-Router docs/design/transcoding.md: one CAPS per connection, opens on
    # demand only, extended AMBEd only (no legacy 19 B OPEN)
    "netns_lifecycle": {
        "caps_per_connection": [1, 1],
        "pcm_opens": [],
        "dstar_opens": [[24, 1, [5]]],
        "swap_ignored_opens": 1,
        "swap_dstar_opens": [],
        # open question to dv-router: CAPS re-probe after an unanswered OPEN
        # D-STAR listener on the USRP module: demand = AMBE+ only, opened at
        # grant (transcoding.md "Demand", "Controller transcoding")
        "pcm_listener_opens": [[24, 5, [1]]],
        "pcm_listener_heard": True,
        # legacy AMBEd: never an OPEN, CAPS after every PONG, ready again on
        # a valid CAPS (transcoding.md "dv-codec-ambed" > "Lifecycle")
        "legacy_swap": {"opens_while_legacy": 0, "caps_after_each_pong": True, "recovers": True},
        # an extended OPEN unanswered for 1000+50 ms triggers a CAPS re-probe when
        # the reply wait expires (OPEN + 1.05 s, retry ~0.5 s later; measured
        # 1.058 s); bound 1.2 s keeps the case off the 50 ms boundary. Was: one immediate
        # CAPS re-probe (dv-router:dev 85ccacfd and later)
        "swap_caps_reprobe": {"within_s": 1.2},
    },
    # B10 stream ids: same-protocol egress carries the exact ingress sid on
    # every protocol (test_sid_preserved); cross-protocol egress carries the
    # canonical id in the target width: low 16 bits for D-STAR/M17, zero
    # extended for DMR (test_sid_cross_mapping). A patch to two modules of
    # one peer rewrites the second leg (placeholder case).
    "sid_kept": ("dstar", "dmr", "m17", "xlx", "dextra"),
    "sid_cross": "canonical",
    # MMDVM conformance (cases/proto/test_mmdvm.py): behaviour rows reuse B3
    # and B2; "mmdvm" maps P-DMR-NN to {observed key: expected value} for the
    # items where xlxd deviates (RPTCL and stream id use the B15/B10 keys)
    "test_mmdvm_lost_terminator_timeout": {"terminator": "all_egress", "timeout_s": 1.6},
    "test_mmdvm_busy_module_no_mix": {"late_entry": "deny"},
    # dev items (docs/protocols/mmdvm.md), DV-Router choices 2026-10-30.
    # Unknown id: id 0 and ids without a dmrid.dat callsign are refused
    # with MSTNAK (9-digit hotspot ids fall back to id/100); with no
    # dmrid.dat every non-zero id is accepted. P-DMR-25 (egress dst TG 9) is a diff
    # item: DV-Router writes TG 9 on every MMDVM egress, as xlxd does.
    "mmdvm": {
        # refused login (id 0, unknown id): MSTNAK + id (HB15 p.3; DroidStar
        # recovers only on MSTNAK)
        "P-DMR-03": {"reply": "MSTNAK"},
        # fallback / local accept a login whose id has no real radio id
        "P-DMR-03@fallback": {"reply": "RPTACK"},
        "P-DMR-03@local": {"reply": "RPTACK"},
        "P-DMR-04": {"auth": "lenient"},
        "P-DMR-05": {"reply": "MSTNAK", "pong": False},
        # every RPTACK except the RPTL salt reply carries the 4-byte id
        "P-DMR-06": {"ack_id": True},
        # RPTC of 302 B or longer is acked (BrandMeister 306 B form)
        "P-DMR-07": {"ack_301": False, "ack_303": True, "pong": True},
        "P-DMR-08": {"routed": True},
        "P-DMR-11": {"order": "be"},
        "P-DMR-12": {"reply": "MSTNAK"},
        "P-DMR-13": {"mstcl": True, "next_after_drop": "MSTNAK"},
        "P-DMR-18": {"relayed": False},
        # DMRD from a non-logged-in address: MSTNAK, at most one per 10 s
        "P-DMR-19": {"relayed": False, "nak": True, "nak_per_10s_max": 1},
        "P-DMR-22": {"voice_sync_header_relayed": False},
        "P-DMR-27": {"repeater_is": "login"},
        # ingress late entry from the embedded LC; egress still starts with
        # a synthesized header (B16 judges egress)
        "P-DMR-36": {"relayed": True},
        "P-DMR-39": {"first_once": True, "second_once": True},
        "P-DMR-41": {"relayed": False},
    },
    # USRP dev items (docs/protocols/usrp.md), DV-Router choices 2026-10-30
    "usrp": {
        # dynamic DVSwitch-style clients (REG:DVSWITCH / REG:OK) not built yet:
        # the dynamic-client cases skip "edge not ready" until this is True
        "dynamic_server": False,
        # UDP port of the dynamic-client hub: 34001 (12345 is Icom G3's fixed
        # config port, urfd Defines.h G3_CONFIG_PORT)
        "dynamic_port": 34001,
        "P-USR-29": {"unregistered_voice": "ignored"},
        # 60 min idle timeout (refreshed by any packet), REG:UNREG on drop
        # and on permanent shutdown
        "P-USR-31": {"idle_timeout_s": 3600, "unreg_on_drop": True},
        "P-USR-33": {"dial_letter": True, "dial_tg": True},
        "P-USR-34": {"default_module": "none"},
        "P-USR-37": {"info_reply": "json"},
        "P-USR-40": {"max_clients": 64, "full_reply": "REG:UNREG"},
        "P-USR-04": {"seq": "continuous"},
        # SET_INFO egress off by default (opt-in per DVSwitch endpoint later)
        "P-USR-09": {"set_info_egress": False},
        # pyUC's 352 B keyup-0 frame ends the over
        "P-USR-12": {"keyup0_voice": "unkey", "keyup0_payload": "played_if_nonzero"},
        # ingress SET_INFO callsign is the talker of the over
        "P-USR-18": {"set_info_talker": "use"},
    },
    # P25 dev items (docs/protocols/p25.md), DV-Router choices 2026-10-30.
    # Egress padding: max(18, ceil(n/9)*9) records with IMBE silence, then
    # one 0x80; nothing for an empty stream.
    "p25": {
        # client-serving P25 edge (UDP 41000) not built yet: server-side
        # cases skip "edge not ready" until this is True
        "server": False,
        "P-P25-03": {"reply": 0, "linked": False},
        # 30 s link timeout refreshed by polls and voice, nothing sent on drop
        "P-P25-08": {"heard": [False, False, False, False]},
        "P-P25-14": {"term_relayed": True, "second_whole": True},
        "P-P25-16": {"relayed": 0},
        "P-P25-17": {"relayed": 0},
        "P-P25-22": {"dst_is_tg": True},
        # unknown talker -> the configured gateway id (rig value 1023000,
        # not in the rig dmrid.dat); no callsign hash
        # no_radio_id policy (default drop): a talker without a real radio
        # id is not sent into the edge; "@fallback" = the placeholder id
        "P-P25-23": {"src_is_talker": True, "src": []},
        "P-P25-23@fallback": {"src_is_talker": True, "src": [1023000]},
        # egress record layout: 40 frames padded to 45 + 0x80 (as P-P25-24)
        "P-P25-20": {"bad": 0, "records": 46},
        "P-P25-24": {"terminated": True, "whole_ldu": True, "records": 46},
        "P-P25-25": {"lone": 0, "voice": 0},
        "P-P25-26": {"has_0x73": True, "records": 19},
        # today's constant parity bytes; real RS parity later
        "P-P25-28": {"parity": {"67": ["f09d6a"], "68": ["19d426"], "69": ["e0eb7b"], "71": ["acb8a4"],
                                "72": ["9bdc75"]}, "lsd": ["0000"]},
        "P-P25-30": {"received": 36},
        "P-P25-31": {"received": 34},
        "P-P25-33": {"received": 0, "my": []},
        "P-P25-33@fallback": {"received": 36, "my": ["P31672"]},
        # encrypted calls are dropped (records are rebuilt; ciphertext must
        # not be relabelled clear)
        "P-P25-35": {"transcoded": False},
    },
    # DPlus dev items (docs/protocols/dplus.md), DV-Router choices 2026-10-30.
    # P-DPL-19: egress RPT2 = "REF" + XLX number + space padding + module.
    "dplus": {
        "P-DPL-06": {"reply": "BUSY"},
        "P-DPL-08": {"reply": "OKRW"},
        "P-DPL-12": {"dropped": True, "disconnect_sent": True, "timeout_s": 30},
        "P-DPL-15": {"echo": True},
        "P-DPL-16": {"rpt1_space_heard": True},
        "P-DPL-24": {"pattern_at_26": True, "byte8": 0},
        "P-DPL-30": {"heard": True},
        "P-DPL-36": {"heard": False},
    },
    # DCS dev items (docs/protocols/dcs.md), DV-Router choices 2026-10-30.
    # Stream id 0 is accepted on DCS ingress only (DroidStar can send it):
    # the over gets a substitute non-zero sid derived from the over, never
    # colliding with an open stream - the one exception to "sid preserved";
    # B8 admit_sid_zero False stays for every other protocol.
    "dcs": {
        "P-DCS-02": {"max_s": 0.5},
        "P-DCS-05": {"reply": "nak"},
        "P-DCS-06": {"acks": 2},
        "P-DCS-11": {"nak": True, "talker_hears_after": False},
        "P-DCS-12": {"reply": "nak"},
        "P-DCS-15": {"ref9_per_poll": 1.0},
        "P-DCS-17": {"size9": True, "size19": True, "size22": True},
        "P-DCS-18": {"silent_notice": "none"},
        "P-DCS-22": {"heard": 63},
        "P-DCS-25": {"suffix": b"    "},
        # the ingress end payload is passed (the tester sends the JARL pattern)
        "P-DCS-30": {"ambe": "55555555c87a000000", "slow": "555555"},
        # own counter and text area per stream
        "P-DCS-31": {"renumbered": True, "tail_passed": False, "byte61": "01"},
        "P-DCS-32": {"heard_my": 0, "heard_rpt1": 63, "heard_rpt2": 63},
        "P-DCS-33": {"flags": b"\x08\x00\x00"},
        "P-DCS-35": {"linked_module": True, "rpt2_module": False},
        "P-DCS-39": {"synthesized_last": 1},
        "P-DCS-40": {"heard_after_last": 0},
        "P-DCS-43": {"second_ack": True, "hears_j": True, "hears_k": True, "j_after_unlink_b": True,
                     "k_after_unlink_b": False},
        "P-DCS-46": {"heard": 63},
    },
    # NXDN peer dev items (docs/protocols/nxdn.md), DV-Router choices
    # 2026-10-30. Client-serving NXDN (UDP 41400) is not built yet.
    "nxdn": {
        # client-serving NXDN (UDP 41400) not built yet: server cases skip
        # "edge not ready" until this is True
        "server": False,
        "P-NXD-03": {"keeps_polling": True, "routed_during_loss": False, "resumed": True},
        # NXDNU x3 on peer removal and shutdown
        "P-NXD-04": {"unlink": True},
        "P-NXD-07": {"sacch_type": 0x10},
        # AMBE bits unchanged both ways (no xlxd gain-index step)
        "P-NXD-13": {"step_out": 0, "step_in": 0},
        "P-NXD-20": {"late_entry": True},
        "P-NXD-23": {"facch_as_voice": False, "frames": 60},
        "P-NXD-25": {"routed": False},
        # encrypted calls are dropped (as P25 and M17)
        "P-NXD-26": {"routed": False},
        # streams keyed by address + src id, floor per B1
        "P-NXD-27": {"mixed": False},
        "P-NXD-28": {"terminator": True, "freed": True},
        # a DMR join (empty stream) is not sent to NXDN as an empty over
        "P-NXD-30": {"relayed": False},
        # no_radio_id policy (default drop), "@fallback" = placeholders
        "P-NXD-16": {"src": []},
        "P-NXD-16@fallback": {"src": [9999]},
        "P-NXD-29": {"my_unknown": [], "dmr_src_unknown": []},
        "P-NXD-29@fallback": {"my_unknown": ["N04242"], "dmr_src_unknown": [0]},
        # server section (applies once "server" is True): no echo for an
        # unknown TG, one winner (B1), synthesized TX_REL at the timeout (B3),
        # NXDN data NXDN-to-NXDN only, 30 s client timeout as the YSF and P25
        # client modes (gateways poll every 5 s)
        "P-NXD-32": {"echo": False},
        "P-NXD-38": {"mixed": False},
        "P-NXD-39": {"trailer": True},
        "P-NXD-42": {"relayed": 6},
        "P-NXD-43": {"after_35s": False, "after_130s": False},
    },
    # DMR+ dongle protocol (UDP 8880, docs/protocols/dmrplus.md): the edge is
    # not built yet; client cases skip "edge not ready" until server is True.
    "dmrplus": {
        "server": False,
        # connect with an id missing from the database: NAK (drop), ACK
        # (fallback / local accept it)
        "P-DMP-02": {"replies": [b"NAK OK\n\x00"]},
        "P-DMP-02@fallback": {"replies": [b"ACK OK\n\x00"]},
        "P-DMP-02@local": {"replies": [b"ACK OK\n\x00"]},
        "P-DMP-04": {"replies": [b"NAK OK\n\x00"], "heard": False},
        "P-DMP-06": {"hears_new": True, "hears_old": False},
        "P-DMP-15": {"on_linked": True, "on_header_tg": False},
        # no_radio_id policy (default drop), "@fallback" = placeholders
        "P-DMP-16": {"relayed": False},
        "P-DMP-16@fallback": {"relayed": True},
        "P-DMP-17": {"exact": True, "c_last_lost": 0},
        "P-DMP-18": {"frames": 0},
        "P-DMP-19": {"second_heard": True, "terminator_within_0.3s": True},
        # unknown talker: the configured gateway id (as P25)
        "P-DMP-22": {"src_known": 1023007, "src_unknown": None},
        "P-DMP-22@fallback": {"src_known": 1023007, "src_unknown": 1023000},
    },
    # YSF (UDP 42000, docs/protocols/ysf.md): DV-Router's YSF edge is built, so
    # no server flag; DV-Router choices 2026-10-30 (P-YSF-28 / 29 follow the
    # configured modules; WiresX DX / CONN / DISC handling is new work there).
    "ysf": {
        # poll reply bytes 4-13: the fixed form every client was tested against (no client checks them)
        "P-YSF-03": {"reply": "YSFPREFLECTOR "},
        # answer suffixed and 10-character callsigns: clients send them, the address is the key
        "P-YSF-04": {"suffix": True, "ten": True},
        # answer polls longer than 14 B (DroidStar sends one for a long callsign)
        "P-YSF-05": {"len15": True},
        # no reflector-initiated polls: every client polls every 5 s, none needs server polls
        "P-YSF-06": {"server_polls": False},
        # 60 s timeout like the G4KLX reference: a client silent for 35 s is still linked
        "P-YSF-07": {"silent_35s": True},
        # YSFU removes the client at once (G4KLX, pYSF, URFd, ysf-nexus; clients send it on unlink)
        "P-YSF-08": {"hears_after_unlink": False},
        # the YSFS count is of YSF clients only (registry users_count), not of other protocols
        "P-YSF-10": {"dmr_counted": False},
        # counter changes every egress frame (MMDVMHost drops a repeated counter), both origins
        "P-YSF-16": {"cross_counter_changes": True, "same_counter_changes": True},
        # YSF -> YSF passthrough: voice bits and callsigns unchanged (DV-Router passthrough rule)
        "P-YSF-18": {"voice_exact": True, "src_kept": True, "dst_kept": True, "gateway_kept": True},
        # end a stream on either marker at once; with neither, after the silence timeout
        "P-YSF-20": {"end_bit_only": "prompt", "terminator_only": "prompt", "none": "timeout"},
        # carry a headerless (late-entry) stream; egress still starts with a header (Yaesu 4.1)
        "P-YSF-21": {"carried": True, "header_synthesised": True},
        # carry V/D mode 1 and Voice FR between YSF clients (same-mode, no transcoding needed)
        "P-YSF-25": {"dn1": True, "vw": True},
        # relay non-WiresX Data FR unchanged (Yaesu 4.3 normal mode; G4KLX, pYSF, ysf-nexus relay it)
        "P-YSF-26": {"relayed": True},
        # do not relay a packet whose FICH does not decode (MMDVMHost and the bridges drop it anyway)
        "P-YSF-27": {"relayed_bad": 0},
        # DG-ID 10..35 -> module A..Z, kept for later DG-ID 0 overs (XLX-compatible; only if enabled)
        "P-YSF-28": {"moved": True, "permanent": True},
        # one default module for a client that only polled: B (XLX-compatible)
        "P-YSF-29": {"hears_A": False, "hears_B": True},
        # egress DG-ID 0: opens every radio's squelch
        "P-YSF-30": {"dgid": [0]},
        # answer DX_REQ to the requester (multi-module server), never relay command frames to the room
        "P-YSF-31": {"reply": "dx", "relayed": 0},
        # CONN room n -> module n with a reply; DISC leaves the module (link kept)
        "P-YSF-32": {"conn_reply": "conn", "conn_moved": True, "disc_hears": False},
        # one client per address: a second callsign from the same socket never doubles delivery
        "P-YSF-36": {"copies": 1},
    },
    # Yaesu IMRS (UDP 21110, docs/protocols/imrs.md): the edge is not built
    # yet; every IMRS case skips "edge not ready" until server is True.
    # Dev values follow when dv-router chooses.
    "imrs": {
        "server": False,
        "P-IMR-02": {"copies": 1},
        # unknown addresses get no ping answer (no amplifier)
        "P-IMR-04": {"answered": False},
        # exactly the mapped DG-IDs
        "P-IMR-05": {"callsign": "XLX999", "dgids": list(range(10, 36))},
        "P-IMR-06": {"registered": True},
        # per-peer refresh: egress does not keep every peer alive
        "P-IMR-07": {"dropped_after_idle": True, "kept_by_egress": False},
        # unmapped DG-ID: drop the call, keep the link
        "P-IMR-08": {"follows_or_linked": True},
        # pad to whole packets
        "P-IMR-14": {"frames": 10},
        "P-IMR-15": {"frames": 15, "zero_frames": 0},
        "P-IMR-17": {"same_sid": False},
        "P-IMR-18": {"echo": 0},
        # lower-case callsigns are normalized to upper-case
        "P-IMR-20": {"heard": True},
        "P-IMR-21": {"terminator": True, "freed": True},
        "P-IMR-22": {"mixed": False},
        "P-IMR-24": {"relayed": False},
    },
    # Icom G3 terminal / access point (docs/protocols/g3.md): the DV-Router
    # edge is not built yet; the cases skip "edge not ready" until True.
    # Dev values follow when dv-router chooses.
    "g3": {
        "server": False,
        "P-G3-03": {"before_route_is_reflector": True},
        # presence is subject to the operator lists; a refused terminal gets
        # no terminal entry (status 1)
        "P-G3-05": {"presence_status": 1},
        "P-G3-10": {"ur": b"XLX999 J", "rpt1": b"XLX999 G", "rpt2": b"XLX999 J"},
        # at least one header copy; the ingress last frame is passed unchanged
        "P-G3-17": {"min_headers": 1, "last_payload": "pass"},
        # drop only on a malformed MY
        "P-G3-21": {"heard_my": 0, "heard_rpt1": 63},
        "P-G3-22": {"synthesized_last": 1},
        # remove a terminal only when its own 40000 port is unreachable
        "P-G3-23": {"removed_own_port": True, "removed_other_port": False},
        # PING every 10 s per terminal
        "P-G3-24": {"period_10s": True},
        "P-G3-26": {"reply": 0},
    },
    # M17 dev items (docs/protocols/m17.md), DV-Router choices 2026-10-30.
    # Cross-mode: only unencrypted voice 3200 (any CAN / signed) is
    # transcoded; 1600, data-only and encrypted streams stay M17-only;
    # other-mode origin gets TYPE 0x0005 and zero META. Ingress CRC is never
    # checked; ACKN is exactly 4 B; egress datagrams never exceed 100 B.
    "m17": {
        # NACK for a module outside the served rooms (run with DV_ROOMS=A-Y)
        "P-M17-03": {"module": "Z", "reply": "NACK 4"},
        "P-M17-04": {"reply_module_digit": "NACK 4", "reply_module_lower": "NACK 4", "reply_callsign_zero": "none"},
        "P-M17-06": {"reply": "ACKN 4", "hears": True},
        # a repeated CONN is ACKed again (mrefd's NACK breaks M17Gateway's retry)
        "P-M17-07": {"same_module": "ACKN 4", "other_module": "ACKN 4", "hears_old": False, "hears_new": True},
        "P-M17-09": {"interval_s": 3},
        "P-M17-12": {"silent_disc": "DISC 10"},
        "P-M17-16": {"repeat": "DISC 4"},
        "P-M17-18": {"disc": "DISC 10"},
        "P-M17-21": {"relayed": True},
        "P-M17-24": {"relayed": True, "other_module": False},
        # same-mode relay of every stream TYPE, encrypted included (packets
        # forwarded as received; never transcoded or sent cross-mode)
        "P-M17-26": {"relayed_voice3200": True, "relayed_voice_data1600": True, "relayed_can1": True,
                     "relayed_can5": True, "relayed_signed": True, "relayed_scrambler": True,
                     "relayed_data": True},
        "P-M17-30": {"relayed": False},
        "P-M17-36": {"egress_broadcast": "@ALL", "egress_designation": "@ALL",
                     "egress_designation_other_module": "@ALL", "egress_reflector": "@ALL",
                     "egress_callsign": "N0TEST"},
        "P-M17-37": {"crc_ok": True},
        "P-M17-38": {"meta_kept": True, "type_1600_kept": True},
        "P-M17-40": {"within_200ms": True},
        "P-M17-41": {"forwarded": True, "unchanged": True},
        "P-M17-42": {"echo": False},
        # no M17 interlink / peer CONN support for now
        "P-M17-44": {"reply": "none"},
    },
    # XLX interlink dev items (docs/protocols/xlx.md), DV-Router choices
    # 2026-10-30. Peer kind comes from the configured row (P-XLX-09; the
    # xlxd-file converter maps BM / FD / FS / PH prefixes), never from the
    # callsign at runtime. No closing frame toward a peer that never got
    # the stream's header (D8).
    "xlx": {
        "P-XLX-02": {"reply": "A", "modules": "DE"},
        # peer address checked (as URFd)
        "P-XLX-05": {"reply": "N"},
        "P-XLX-14": {"linked_after": True},
        "P-XLX-18": {"egress": "zero_slot"},
        # present slots decided by the received size
        "P-XLX-22": {"rev1_53_dmr_is_sent": True, "rev1_53_frames": True, "rev3_27_dmr_zero": False,
                     "rev3_27_frames": True},
        "P-XLX-32": {"relayed": 0},
        "P-XLX-33": {"egress": "zero_slot"},
        # peer kind from the configured row (the BM3109 row maps to a BM peer)
        "P-XLX-09": {"sizes": [45], "dstar_zero": False},
        # D8: nothing of a cut XLX stream to a peer that never got its header
        "P-XLX-34": {"closing_frame_to_other_peers": 0},
    },
    # DExtra dev items (docs/protocols/dextra.md), DV-Router choices
    # 2026-10-30. Reflector name "XRF" + the callsign digits (XRF999 in the
    # rig, as on ref: P-DXT-07 / P-DXT-37 compare bytes with xlxd). No
    # DExtra / XRF peer links in this version (P-DXT-50/51).
    "dextra": {
        # DroidStar links once and never retries: ACK at once
        "P-DXT-02": {"acked": True, "delayed": False},
        "P-DXT-04": {"reply": "nak", "linked": False},
        # IG / DStarGateway send each link twice, QnetGateway 5x
        "P-DXT-06": {"second_reply": "ack", "once": True},
        "P-DXT-07": {"period": 10},
        "P-DXT-08": {"echo_kept": False, "own_kept": True},
        # URFd-form unlink notice on the timeout (IG / QnetGateway ignore it)
        "P-DXT-09": {"dropped": True, "notice": "unlink"},
        # the talker's unlink mid-stream ends the stream and removes it
        "P-DXT-14": {"end_marker": True, "still_linked": False, "next_heard": True},
        "P-DXT-17": {"relayed_byte8": False, "relayed_byte14": True, "relayed_ids": True},
        "P-DXT-21": {"relayed_my": False, "relayed_rpt1": True, "relayed_rpt2": True},
        "P-DXT-22": {"flags": "400102"},
        "P-DXT-26": {"relayed": True},
        # same-mode: the received counter is passed unchanged
        "P-DXT-31": {"exact": True, "egress_mod21": False},
        "P-DXT-33": {"passed": True, "jarl_end": False, "pre_last_kept": True},
        "P-DXT-36": {"linked_heard": True, "rpt2_heard": False},
        "P-DXT-37": {"rpt1": "N0CALL P", "ur": "CQCQCQ  ", "my": "N0CALL  ", "suffix": "    "},
        # a second link from the same address is a module change
        "P-DXT-39": {"second_reply": "ack", "hears_first": False, "hears_second": True,
                     "after_unlink": [False, False]},
        "P-DXT-42": {"relayed": False},
        # a late joiner gets the running header first
        "P-DXT-44": {"header_first": True, "got_voice": True},
        "P-DXT-45": {"copies": 5, "later": 0},
        "P-DXT-48": {"first_once": True, "second_once": True},
        "P-DXT-50": {"reply": "nak", "unlink_reply": None},
        "P-DXT-51": {"linked_heard": False, "rpt2_heard": False},
        "P-DXT-52": {"reply": None, "heard": False},
        # unlink with the client's own stream open: revoke at once (B5)
        "P-DXT-54": {"keepalives_after_unlink": 0, "new_stream_heard": False, "header_heard": False,
                     "voice_heard": 0},
    },
}

# B12: header fields DV-Router does not verify, because the tester (like
# real clients) sends zeros or spaces there. Recorded, not a case.
NOT_CHECKED = ("DExtra header CRC", "M17 LSF CRC", "DMR RPTK hash")
