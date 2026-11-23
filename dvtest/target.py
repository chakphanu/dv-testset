# SPDX-License-Identifier: MIT
"""System under test, described by DVT_* environment variables and an
optional TOML file (DVT_TARGET_FILE, keys = env names without the DVT_
prefix, lower case, top level or under [target]). Environment wins.

Rig defaults (DVT_SUBNET, default 172.30.0.0/24): xlxd-ref .10,
xlxd-sut .11, transcoder-ref .20, transcoder-sut .21, tester .30,
allstar .40, urfd .70, mrefd .80, web-proxy .90.

  DVT_SUBNET         rig subnet
  DVT_XLXD_REF       reference reflector address (xlxd-ref .10; with
                     DVT_REF_KIND=urfd / mrefd and no DVT_XLXD_REF: the URFd /
                     mrefd host)
  DVT_REF_KIND       reference reflector kind: xlxd (default), urfd or mrefd
  DVT_XLXD_SUT       xlxd under test
  DVT_XLXD_PEER_SUT  XLX998 interlink peer of xlxd-sut (.12, profile interlink)
  DVT_XLXD_PEER_REF  XLX998 interlink peer of xlxd-ref (.13, profile interlink)
  DVT_XLXD_PEER2_SUT XLX997 transit peer on the sut side (.14, profile interlink)
  DVT_XLXD_PEER2_REF XLX997 transit peer on the ref side (.15, profile interlink)
  DVT_AMBED_REF      reference transcoder
  DVT_AMBED_SUT      transcoder under test
  DVT_TESTER         tester container address
  DVT_ALLSTAR        AllStarLink node
  DVT_NXDN_REF       reference NXDN reflector (.50)
  DVT_NXDN_SUT       NXDN reflector under test (.51)
  DVT_P25_REF        reference P25 reflector (.60)
  DVT_P25_SUT        P25 reflector under test (.61)
  DVT_URFD           URFd reflector (.70, profile urfd)
  DVT_MREFD          mrefd reflector (.80, profile mrefd; M17 only)
  DVT_WEB_PROXY      TLS proxy in front of the sut's web service (.90, profile web)
  DVT_WEB            on when the rig runs profile web (cases/web_fixtures.py)
  DVT_WEB_URL        base URL of the web service (https://dvt-web); DVT_WEB_CA the CA file
                     (default $DVT_OUT/web/ca.pem); operator login in $DVT_OUT/web/operator.env
  DVT_AMBED_ADDR     single transcoder for the wire case (no default)
  DVT_AMBED_PORT     transcoder control port (10100)
  DVT_TRANSCODER_FAKE  1 when transcoder-sut is the fake extended AMBEd (control socket
                     <ambed_sut>:10190, dvtest/fake_ctl.py; scripts/run.sh sets it)
  DVT_AMBED_STREAM_CAP  optional stream limit the wire case checks
  DVT_XLXD_BIN       local xlxd binary for the netns case (no default)
  DVT_XLXD_REF_BIN   optional reference xlxd binary for the netns case
  DVT_NETNS_PARALLEL netns scenarios run at the same time (default 1, waves; a DV-Router
                     scenario peaks near 435 tasks, the scope allows 1024; dvtest/netns_runs.py)
  DVT_REFLECTOR      reflector callsign (XLX999)
  DVT_IAX_SECRET     IAX2 secret of the AllStar node (rig: .local/rig.env)
  DVT_OUT            results directory (out)
  DVT_NO_RADIO_ID    drop | fallback | local: how the sut's id-based edges
                     treat a talker without a real radio id (unset = drop)
  DVT_SUT_LAUNCH     how xlxd-sut starts: direct (default), native (DV-Router with its own TOML) or import
                     (DEPRECATED: only for images before COMPAT1b part 2; the import hand-off is being removed)
  DVT_REQUIRE_IDENT  true (default) or false: [data] require_ident the native config was rendered with
                     (rig/config/dv-router/data-ident.toml; cases/test_data_ident.py)
  DVT_SUT_PROFILE    behaviour rule for the sut: xlxd (default, sut == ref) or a
                     dvtest/specs/<name>.py spec
  DVT_SUT_DIVERGENCES  legacy: declared divergences of the sut (dvtest/divergences.py)
  DVT_DP18STRICT     on: the strict DP18-o cases (aprsis leg with rf_ports, counter conservation, strict floor;
                     cases/dp18_support.py, docs/dp18-cases.md "Strict"); off or unset: they skip
  DVT_TXCNT1         on: DP18-o txcnt1, dv_edge_rf_tx_total{result=delayed} counts frames, not scheduler re-checks
                     (needs an image with the DV-Router unit TXCNT1; fails on wip-125de6f; docs/dp18-cases.md "Strict")
  DVT_TXCNT1B        on: DV-Router 051a4516 (TXCNT1b, not in an image yet): the strict DP18-o conservation adds the
                     cancellation term dv_edge_drops_total{reason=rf_fenced} (a fence or stop drains the scheduler
                     entries, every copy has one terminal result), and the case test_dp18_o_txcnt1b_result_labels
                     asserts the dv_edge_rf_tx_total result labels are the documented eight (no dropped_budget,
                     dropped_quota or dropped_gap); off or unset: the conservation is the older equation and the
                     label case skips (docs/dp18-cases.md "Strict")
  DVT_RIF5           on: DV-Router RIF5 (c017e491 or later), the `mmdvm-serial` radio adapter: cases/test_rif5_mmdvm_serial.py plays a fake
                     MMDVM modem (dvtest/mmdvm_serial.py) on the UDP byte port <tester>:3340 <-> <sut>:3341 of rig/config/dv-router/rif5-mmdvm-udp.toml
                     (DVT_SUT_CONFIG_EXTRA; native launch): D-STAR, DMR and YSF overs in both directions, the open dialogue, the startup guard and a
                     silent modem; off or unset: the cases skip naming this switch (docs/rif5-cases.md)
  DVT_RIF5FW         on: the RIF5 fake modem is firmware-like (dvtest/mmdvm_fw.py, design-276 1.2 / 1.9): TX FIFO in bytes per board, space = free / unit,
                     a TX-delay preamble before the first frame, NAK 5 on overflow, PTT off and an `underrun` event when the FIFO runs dry mid-over;
                     DVT_RIF5FW_PROFILE mmdvm (default) or hs, DVT_RIF5FW_TXDELAY_MS 0-500 (default: the row's SET_CONFIG value); off or unset: the
                     fill-model fake (docs/rif5-cases.md "The firmware-like fake")
  DVT_RIF5J          on: the image has DV-Router RIF5j (fast status poll while keyed, host queue in airtime, tx_underrun, O3 frequencies, RXD silence fill):
                     the cases for that image run (RIF5-J-*, and the RXD / O3 assertions of the fix); off or unset: the assertions for the current image
                     (docs/rif5-cases.md "Cases for the fix image")
  DVT_AX25ACK1       on: the image has AX25ACK1 + TXOFF2 (DV-Router 345f1ebe, wip-f1f3777 or later): every transmitting `[edges.ax25]` of the rendered config
                     carries `tx_ack = "I am the control operator of <CALL-SSID> and accept responsibility for its transmissions"` (dvtest/sut_config.py; older
                     images refuse the key, so it is off by default), the config-refusal cases expect the TXOFF2 / DIGI1b verdicts (an off port or master loads and is
                     dormant) and the AX25ACK1 cases run (docs/rig.md "AX.25 tx_ack", docs/ax25ack1-cases.md); off or unset: the shapes of the older images
  DVT_RESTART1       on: the image has RESTART1 / RESTART1b (DV-Router f34eb5ba + 348f984f, wip-f1f3777 or later): cases/test_restart1.py restarts the node through
                     `POST /api/v1/admin/system/restart-node` (web profile, admin login, native launch) and asserts new children only, the launcher pid kept, ready
                     again within the budget; off or unset: every case skips naming this switch (docs/restart1-cases.md)
  DVT_M17RL1         on: the image has M17RL1 (DV-Router 307525dc, wip-f1f3777 or later): cases/test_m17rl1.py plays the M17 peer M17-DVT in the mrefd manner and asserts an ACKN for
                     every CONN, at most one local CONN per peer row per 3 s, the deferred CONN and the relink within about 3 s (--profile interlink, native launch; side ref
                     = real mrefd is recorded only); off or unset: every case skips naming this switch (docs/m17rl1-cases.md)
  DVT_RIF5N          on: the image is dv-router:wip-f1f3777 (DV-Router bc9af2b1: RIF5d NXDN, RIF5e P25, RIF5h D-STAR ride-along, RIF5k no extra end frame, RIF5l a stream
                     without voice never keys, RFHEARD1): the cases RIF5N-* of cases/test_rif5_mmdvm_serial.py (NXDN and P25 TX / RX / long overs / foreign RAN and NAC,
                     NXDN <-> DMR, the voiceless join, the rider with its negative control, the RFHEARD1 guard) and the RIF5k / RIF5l assertions in the older cases;
                     the config is rig/config/dv-router/rif5n-modes.toml, or rif5n-ride.toml (ride_along = true, with data-ident.toml), INSTEAD of rif5-mmdvm-udp.toml;
                     off or unset: those cases skip naming this switch (docs/rif5-cases.md "The image wip-f1f3777")
  DVT_PORTCTL1       on: the image has DV-Router PORTCTL1 (1ea6bd00, after wip-f1f3777): after the room's timeout the same talker's repeated headers open no new RTS
                     (`over_total{dir=rx,result=rts_suppressed}`): the case RIF5-PORTCTL1 (a 191 s D-STAR RF over with repeated headers, about 4 minutes); off or unset: it skips; no other case reads it
  DVT_SMALL5         on: the image has SMALL5 (DV-Router d09e388f, in 5333e4fc, dv-router:wip-c3b24d2): `frames_total{dir=tx,result=ok}` counts NXDN and P25 frames too, so RIF5-M1
                     asserts its growth equals the native frames of all five modes the modem logged (unset: a FINDING, the old rule), and the reload replies must carry `truncated`
                     (cases/test_secret_reload.py; unset: optional); docs/rfpos1b-cases.md "SMALL5"
  DVT_RFPOS1B        on: the image has RFPOS1 step B (DV-Router 47dac7b4, in 5333e4fc): an end-hint unit no longer widens the egress width, so a YSF over ends without a false
                     end-of-over gap or lost count on its egress edges; cases/test_rfpos1b.py asserts (YSF -> YSF, YSF -> DMR, YSF -> D-STAR; unset: records and writes FINDING
                     lines, nothing fails); needs the MEDLOAD1 series (docs/rfpos1b-cases.md)
  DVT_XSTALE1        on: the image has XSTALE1 (DV-Router b849a082, dv-router:wip-2ec9be4): the leg-less room copy of an own talker on usrp / p25 / nxdn stays open for the over;
                     cases/test_xstale1.py asserts (own over folded clean, a same-edge joiner at 3 s hears the rest and folds as joined, real silence ends the stream) and the soak
                     judges dv_edge_drops_total{reason=x_stale} per edge (docs/xstale1-cases.md); off or unset: the cases skip, the soak records (DVT_XSTALE1_USRP_MODULE: the USRP module, A)
  DVT_XSTALE1C       on: the image has XSTALE1c (DV-Router 5072bf57, dv-router:wip-3f142cd): a leaving listener settles its egress over at the leave, a rejoining one folds as joined,
                     a stream refused for want of a radio id folds no egress over; cases/test_xstale1c.py asserts (docs/xstale1c-cases.md); off or unset: the cases skip
  DVT_XSTALE1D       on: the image has XSTALE1d (DV-Router, after wip-3f142cd): a transcoded over whose only listener left folds complete, not end_missing;
                     XS1c-b[leave] and XS1c-e (DExtra talker, DCS listener leaves) assert it (docs/xstale1c-cases.md); off or unset: b is recorded as a FINDING, e skips
  DVT_RADIOSEQ1      on: the image has RADIOSEQ1 (DV-Router 917b9e3b, dv-router:wip-37b7960): the radio hub numbers received DMR / YSF / NXDN / P25 voice by frame
                     ordinal, so a lossless radio over reaches a DMR (DMRD) client with the voice letters A..F in order (sync on A), the hub gap counters
                     (dv_edge_over_units_total{dir=rx,proc=radio,result=gap}, dv_room_over_units_total{result=gap}) stay 0 and the over is not counted lossy,
                     a YSF egress FN and an NXDN egress SACCH structure advance by one frame: the cases RADIOSEQ1-RX-<mode> of cases/test_rif5_mmdvm_serial.py
                     assert these (shape A of docs/rif5-cases.md, DVT_RIF5N=on for NXDN and P25, DVT_RIG_CONTROL=1 for the counters); off or unset: the same
                     cases play the overs and only RECORD the letter histogram, the FN / SACCH steps and the counters (docs/radioseq1-cases.md)
  DVT_DIGI1          on: the image has the DIGI1 fill-in digipeater (DV-Router 22994519 + 588ffb4c, wip-eeb44b4): cases/test_digi1.py plays fake KISS TNCs
                     (rig/config/dv-router/digi1.toml = the rate floor 10 / 3 and the class defaults, digi1-roomy.toml = 20 % duty for the functional cases)
                     and asserts DIGI-R1 to R5 (the path vectors G1 to G13, the duplicate window, the flood, two fill-ins, the startup guard) and
                     dv_edge_digi_total{port,result} with kind digi on dv_edge_rf_tx_total; cases/test_digi1_dw.py repeats vectors through the Dire Wolf TNCs
                     of profile dw (digi1-dw.toml); off or unset: every case skips naming this switch. The config refusals (cases/test_digi1_config.py,
                     -m cfgload) need an image with DIGI1 but not this switch (docs/digi1-cases.md)
  DVT_FLK15          on: FLK15 (DV-Router b8d9db72, image wip-f1f3777): cases/test_flk15_policy.py asserts that an outage of the position policy hides and never
                     leaks and that the consumers resync without a restart (web killed again and again for 15 s, then a consumer restarted; docs/flk15-cases.md);
                     off or unset: every case skips naming this switch. DVT_FLK15_BOUND_S: seconds waited after a flag change before the next position (default 12 =
                     the documented 7 s plus slack)
  DVT_GUARD1         on: GUARDSYNC1 / GUARDMERGE1 (DV-Router c1b239d1 + da900207, image wip-f1f3777): cases/test_guard1.py earns a strike-3 block, SIGKILLs a room controller and asserts
                     that the block is reconciled (applied within 31 s + slack) in the restarted room; off or unset: the cases skip naming this switch (docs/guard1-cases.md)
  DVT_RTS1           on: RTS1 (DV-Router b64733cd, image wip-f1f3777): cases/test_rts1.py asserts the image marker (HELP of dv_room_rts_total), the fixed series, the no_winner / deny
                     conservation and the counted reasons around a room restart; the winner gap itself is a GAP; off or unset: the cases skip naming this switch (docs/rts1-cases.md)
  DVT_RFPOS1B_LINK   on: RFPOS1 step B (DV-Router 47dac7b4, image wip-c3b24d2): with --profile twonode run.sh also links the pair by a node link (rfpos1-link-a / -b.toml, a
                     per-run key) through a relay in the tester; cases/test_rfpos1_link.py drops one Units record and asserts the gap at its own position (docs/rfpos1-cases.md)
                     off or unset: the cases skip naming this switch
  DVT_GUARDID2       on: GUARDID2 (DV-Router 9a8d1cb4, image wip-3f142cd): like DVT_RFPOS1B_LINK run.sh links the pair by a node link and adds a P25 server port (guardid2-p25.toml,
                     TG 31672 -> room G, no_radio_id fallback) to both nodes; cases/test_guardid2.py asserts that the source-id guard flag crosses the link (bit 0 of the StreamStart
                     carried field, no bytes added) and that a strike on one unmapped P25 talker leaves the other granted on node 2 (docs/guardid2-cases.md); off or unset: skip
  DVT_RFPOS1C        on: RFPOS1 step C (DV-Router fb3167bb): with --profile asl-edge run.sh adds rfpos1c-usrp.toml (a static USRP row on module L, the tester as the peer);
                     cases/test_rfpos1c.py loses one IAX2 voice frame (a mini frame, and a full frame) of an AllStar call and asserts that the D-STAR, DMR and USRP listeners of the
                     room miss exactly that position and the room counts one gap and one lossy over, egress tx lost +0 (docs/rfpos1c-cases.md); off or unset: the cases skip naming this switch
  DVT_MEDLOAD1       on: MEDLOAD1 (DV-Router fb6a2a51 .. 28fc1838, image wip-f1f3777): cases/test_medload1.py sends overs with controlled drops, a late frame, a duplicate and a counter wrap and
                     compares the per-over gap / lost / late counts of the hub (dv_edge_over_units_total, dv_room_over_units_total, dv_room_overs_total) with the edge's rule, plus the S7 analogue
                     (five rounds under busy loops, DVT_MEDLOAD1_BURN processes, default 16); off or unset: the cases skip naming this switch (docs/medload1-cases.md)
  DVT_BUD1           on: BUD1 / BUD1b (DV-Router a15b08fa + 364a5760, image wip-f1f3777): the session hook (dvtest/bud1_plugin.py, cases/conftest.py) samples the hub metrics every DVT_BUD1_EVERY_S s
                     (default 60) into out/<run>/bud1/ and writes the final snapshot after DVT_BUD1_SETTLE_S s (default 11); the verdict is `python3 -m dvtest.bud1 out/<run> ...` (dv_fabric_body_rejected_total
                     must be 0 in every process of every stage); cases/test_bud1.py asserts it in-run, the admin replies without `truncated` and the 422 `truncated.errors` cut; off or unset: no hook,
                     the cases skip naming this switch (docs/bud1-cases.md)
  DVT_BUD1_KILLWAIT  on: with DVT_BUD1=on also sample before the first kill of each process (waits DVT_BUD1_SETTLE_S); off by default so a chain-wide
                     DVT_BUD1=on does not shift the timing of standing restart cases; the BUD1 rig stage sets it
  DVT_EDG4           on: EDG3 / EDG4b / EDG4c (DV-Router 5abe7510, image wip-f1f3777): cases/test_web_edg4.py (browser + tester UDP sockets with signed STUN checks, dvtest/stun_pair.py) asserts
                     one ctl DataChannel per peer (a refused open resets its stream, the second refusal closes the peer), the 30 s expiry of a validated source and the handshake pin against an alternate
                     source's DTLS alert; off or unset: the cases skip naming this switch (docs/edg4-cases.md)
  DVT_FLK10          on: FLK10, dv-edge-webrtc refuses an offer whose media section has a missing, repeated, empty,
                     over-16-byte or duplicate a=mid (403 forbidden); the cases of cases/test_web_rtc_mid.py skip naming
                     this switch without it (needs an image with DV-Router 98c88b2, wip-bd3ceb9; docs/web-mid-cases.md)
  DVT_NETTHR1        on: DV-Router NETTHR1a/b (fce97cd3 + c8f9b4ed, image wip-eeb44b4): cases/test_netthr1_rig.py (fabric session gauge and cap, the web / admin
                     stream and login-slot caps, thread ceilings from `proc-stat`, the DNS cancel) and the host-side cases/test_netthr1_config.py (config check:
                     derived cap, cap range, cap against the planned children, HTTP cap ranges); every case skips naming this switch without it
                     (docs/netthr1-cases.md)
  DVT_NETTHR1_DNS    on: the DNS cancel case D1 (needs DVT_SUT_RESOLV=30 on the host so that the SUT resolves through the tester, and the tester binds UDP 53)
  DVT_NETTHR1_SWEEPS number of proc-stat sweeps of the thread cases (default 4; each sweep reads every process name once)
  DVT_FLK11          on: DV-Router caed3841, the echo admits an over by arrival order relative to a configuration change: cases/test_echo.py F11a / F11b (enable
                     during an over does not join it, disable during an over finishes it and refuses the next); skip naming this switch without it
                     (docs/echo-cases.md, "FLK11")
"""
import ipaddress
import os
import tomllib
from dataclasses import dataclass, field

from . import divergences

REF_KINDS = ("xlxd", "urfd", "mrefd")
DEFAULT_SUBNET = "172.30.0.0/24"
HOST_OFFSETS = {"xlxd_ref": 10, "xlxd_sut": 11, "xlxd_peer_sut": 12, "xlxd_peer_ref": 13,
                "xlxd_peer2_sut": 14, "xlxd_peer2_ref": 15,
                "ambed_ref": 20, "ambed_sut": 21,
                "tester": 30, "allstar": 40, "nxdn_ref": 50, "nxdn_sut": 51,
                "p25_ref": 60, "p25_sut": 61, "urfd": 70, "mrefd": 80, "web_proxy": 90}
# Rig hosts the wire checker (dvtest.check.rig_config) accepts as peers but that
# are not Target fields (no case addresses them through Target): profile
# twonode dvr-peer .16 / transcoder-peer .17, asl-lab asl-reg .. asl-e .41-.44,
# asl-ext .45, the T5n alias address .46 (asl-nat), aprsc .47, dw-a .48 / dw-b .49, mmdvm-stack .100 (profile mmdvm).
# cases/unit/test_check_rig_hosts.py ties this table and HOST_OFFSETS to the
# addresses of rig/compose.yml, so a new service cannot be forgotten silently.
CHECK_HOST_OFFSETS = {"dvr_peer": 16, "ambed_peer": 17,
                      "asl_reg": 41, "asl_a": 42, "asl_b": 43, "asl_e": 44, "asl_ext": 45,
                      "asl_nat_alias": 46, "aprsc": 47, "dw_a": 48, "dw_b": 49,
                      "mmdvm_stack": 100}
KEYS = ("subnet", "xlxd_ref", "xlxd_sut", "xlxd_peer_sut", "xlxd_peer_ref", "xlxd_peer2_sut", "xlxd_peer2_ref",
        "ambed_ref", "ambed_sut", "tester", "allstar",
        "nxdn_ref", "nxdn_sut", "p25_ref", "p25_sut", "urfd", "mrefd", "web_proxy", "ref_kind",
        "ambed_addr", "ambed_port", "ambed_stream_cap", "xlxd_bin", "xlxd_ref_bin", "reflector", "iax_secret",
        "sut_profile", "no_radio_id", "out")


@dataclass
class Target:
    subnet: str = DEFAULT_SUBNET
    xlxd_ref: str = ""
    xlxd_sut: str = ""
    xlxd_peer_sut: str = ""
    xlxd_peer_ref: str = ""
    xlxd_peer2_sut: str = ""
    xlxd_peer2_ref: str = ""
    ambed_ref: str = ""
    ambed_sut: str = ""
    tester: str = ""
    allstar: str = ""
    nxdn_ref: str = ""
    nxdn_sut: str = ""
    p25_ref: str = ""
    p25_sut: str = ""
    urfd: str = ""
    mrefd: str = ""
    web_proxy: str = ""
    ref_kind: str = "xlxd"
    ambed_addr: str = ""
    ambed_port: int = 10100
    ambed_stream_cap: int = 0
    xlxd_bin: str = ""
    xlxd_ref_bin: str = ""
    reflector: str = "XLX999"
    sut_profile: str = "xlxd"
    no_radio_id: str = ""
    iax_secret: str = field(default="", repr=False)
    out: str = "out"
    # keys set explicitly by env or TOML (rig host defaults are not)
    given: set = field(default_factory=set)
    # declared divergences of the system under test (dvtest.divergences)
    divergences: frozenset = frozenset()

    def is_given(self, key):
        return key in self.given

    def host(self, offset):
        net = ipaddress.ip_network(self.subnet, strict=False)
        return str(net.network_address + offset)


def _read_toml(path):
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return data.get("target", data)


def load(env=None, path=None):
    """Target from env (default os.environ) and the TOML at path (default
    $DVT_TARGET_FILE)."""
    env = os.environ if env is None else env
    values = {}
    declared = ""
    path = path or env.get("DVT_TARGET_FILE")
    if path:
        for key, value in _read_toml(path).items():
            if key in KEYS:
                values[key] = value
            elif key == "sut_divergences":
                declared = value
    for key in KEYS:
        value = env.get("DVT_" + key.upper())
        if value:
            values[key] = value
    # effective divergences: declared names plus what the profile implies
    declared = divergences.parse(env.get("DVT_SUT_DIVERGENCES") or declared)
    target = Target(given=set(values),
                    divergences=frozenset(declared | divergences.implied(values.get("sut_profile") or "xlxd")))
    for key, value in values.items():
        setattr(target, key, int(value) if key in ("ambed_port", "ambed_stream_cap") else str(value))
    if target.ref_kind not in REF_KINDS:
        raise ValueError("DVT_REF_KIND must be one of %s, got %r" % ("/".join(REF_KINDS), target.ref_kind))
    for key, offset in HOST_OFFSETS.items():
        if not getattr(target, key):
            setattr(target, key, target.host(offset))
    # a URFd / mrefd reference without an explicit DVT_XLXD_REF is that host
    if target.ref_kind in ("urfd", "mrefd") and not target.is_given("xlxd_ref"):
        target.xlxd_ref = getattr(target, target.ref_kind)
    return target

