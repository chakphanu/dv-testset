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
}

# B12: header fields DV-Router does not verify, because the tester (like
# real clients) sends zeros or spaces there. Recorded, not a case.
NOT_CHECKED = ("DExtra header CRC", "M17 LSF CRC", "DMR RPTK hash")
