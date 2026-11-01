# SPDX-License-Identifier: MIT
"""System under test, described by DVT_* environment variables and an
optional TOML file (DVT_TARGET_FILE, keys = env names without the DVT_
prefix, lower case, top level or under [target]). Environment wins.

Rig defaults (DVT_SUBNET, default 172.30.0.0/24): xlxd-ref .10,
xlxd-sut .11, transcoder-ref .20, transcoder-sut .21, tester .30,
allstar .40, urfd .70, mrefd .80.

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
  DVT_AMBED_ADDR     single transcoder for the wire case (no default)
  DVT_AMBED_PORT     transcoder control port (10100)
  DVT_TRANSCODER_FAKE  1 when transcoder-sut is the fake extended AMBEd (control socket
                     <ambed_sut>:10190, dvtest/fake_ctl.py; scripts/run.sh sets it)
  DVT_AMBED_STREAM_CAP  optional stream limit the wire case checks
  DVT_XLXD_BIN       local xlxd binary for the netns case (no default)
  DVT_XLXD_REF_BIN   optional reference xlxd binary for the netns case
  DVT_REFLECTOR      reflector callsign (XLX999)
  DVT_IAX_SECRET     IAX2 secret of the AllStar node (rig: .local/rig.env)
  DVT_OUT            results directory (out)
  DVT_NO_RADIO_ID    drop | fallback | local: how the sut's id-based edges
                     treat a talker without a real radio id (unset = drop)
  DVT_SUT_LAUNCH     how xlxd-sut starts: direct (default), import or native (DV-Router with its own TOML)
  DVT_REQUIRE_IDENT  true (default) or false: [data] require_ident the native config was rendered with
                     (rig/config/dv-router/data-ident.toml; cases/test_data_ident.py)
  DVT_SUT_PROFILE    behaviour rule for the sut: xlxd (default, sut == ref) or a
                     dvtest/specs/<name>.py spec
  DVT_SUT_DIVERGENCES  legacy: declared divergences of the sut (dvtest/divergences.py)
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
                "p25_ref": 60, "p25_sut": 61, "urfd": 70, "mrefd": 80}
KEYS = ("subnet", "xlxd_ref", "xlxd_sut", "xlxd_peer_sut", "xlxd_peer_ref", "xlxd_peer2_sut", "xlxd_peer2_ref",
        "ambed_ref", "ambed_sut", "tester", "allstar",
        "nxdn_ref", "nxdn_sut", "p25_ref", "p25_sut", "urfd", "mrefd", "ref_kind",
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

