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
        "P-USR-04": {"seq": "continuous"},
        # SET_INFO egress off by default (opt-in per DVSwitch endpoint later)
        "P-USR-09": {"set_info_egress": False},
        # pyUC's 352 B keyup-0 frame ends the over
        "P-USR-12": {"keyup0_voice": "unkey"},
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
        "P-P25-23": {"src_is_talker": True, "src": [1023000]},
        "P-P25-24": {"terminated": True, "whole_ldu": True, "records": 46},
        "P-P25-25": {"lone": 0, "voice": 0},
        "P-P25-26": {"has_0x73": True, "records": 19},
        # today's constant parity bytes; real RS parity later
        "P-P25-28": {"parity": {"67": ["f09d6a"], "68": ["19d426"], "69": ["e0eb7b"], "71": ["acb8a4"],
                                "72": ["9bdc75"]}, "lsd": ["0000"]},
        "P-P25-30": {"received": 36},
        "P-P25-31": {"received": 34},
        "P-P25-33": {"my": ["P31672"], "received": 36},
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
        "P-DCS-22": {"heard": 42},
        "P-DCS-25": {"suffix": b"    "},
        # the ingress end payload is passed (the tester sends the JARL pattern)
        "P-DCS-30": {"ambe": "55555555c87a000000", "slow": "555555"},
        # own counter and text area per stream
        "P-DCS-31": {"counter_start": 0, "counter_steps": [1], "tail_passed": False, "byte61": "01"},
        "P-DCS-32": {"heard_my": 0, "heard_rpt1": 42, "heard_rpt2": 42},
        "P-DCS-33": {"flags": b"\x08\x00\x00"},
        "P-DCS-35": {"linked_module": True, "rpt2_module": False},
        "P-DCS-39": {"synthesized_last": 1},
        "P-DCS-40": {"heard_after_last": 0},
        "P-DCS-43": {"second_ack": True, "hears_j": True, "hears_k": True, "j_after_unlink_b": True,
                     "k_after_unlink_b": False},
        "P-DCS-46": {"heard": 42},
    },
}

# B12: header fields DV-Router does not verify, because the tester (like
# real clients) sends zeros or spaces there. Recorded, not a case.
NOT_CHECKED = ("DExtra header CRC", "M17 LSF CRC", "DMR RPTK hash")
