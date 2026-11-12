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
  DVT_FLK10          on: FLK10, dv-edge-webrtc refuses an offer whose media section has a missing, repeated, empty,
                     over-16-byte or duplicate a=mid (403 forbidden); the cases of cases/test_web_rtc_mid.py skip naming
                     this switch without it (needs an image with DV-Router 98c88b2, wip-bd3ceb9; docs/web-mid-cases.md)
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

