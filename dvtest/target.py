# SPDX-License-Identifier: MIT
"""System under test, described by DVT_* environment variables and an
optional TOML file (DVT_TARGET_FILE, keys = env names without the DVT_
prefix, lower case, top level or under [target]). Environment wins.

Rig defaults (DVT_SUBNET, default 172.30.0.0/24): xlxd-ref .10,
xlxd-sut .11, transcoder-ref .20, transcoder-sut .21, tester .30,
allstar .40.

  DVT_SUBNET         rig subnet
  DVT_XLXD_REF       reference xlxd address
  DVT_XLXD_SUT       xlxd under test
  DVT_AMBED_REF      reference transcoder
  DVT_AMBED_SUT      transcoder under test
  DVT_TESTER         tester container address
  DVT_ALLSTAR        AllStarLink node
  DVT_AMBED_ADDR     single transcoder for the wire case (no default)
  DVT_AMBED_PORT     transcoder control port (10100)
  DVT_AMBED_STREAM_CAP  optional stream limit the wire case checks
  DVT_XLXD_BIN       local xlxd binary for the netns case (no default)
  DVT_XLXD_REF_BIN   optional reference xlxd binary for the netns case
  DVT_REFLECTOR      reflector callsign (XLX999)
  DVT_IAX_SECRET     IAX2 secret of the AllStar node (rig: .local/rig.env)
  DVT_OUT            results directory (out)
"""
import ipaddress
import os
import tomllib
from dataclasses import dataclass, field

DEFAULT_SUBNET = "172.30.0.0/24"
HOST_OFFSETS = {"xlxd_ref": 10, "xlxd_sut": 11, "ambed_ref": 20, "ambed_sut": 21,
                "tester": 30, "allstar": 40}
KEYS = ("subnet", "xlxd_ref", "xlxd_sut", "ambed_ref", "ambed_sut", "tester", "allstar",
        "ambed_addr", "ambed_port", "ambed_stream_cap", "xlxd_bin", "xlxd_ref_bin", "reflector", "iax_secret", "out")


@dataclass
class Target:
    subnet: str = DEFAULT_SUBNET
    xlxd_ref: str = ""
    xlxd_sut: str = ""
    ambed_ref: str = ""
    ambed_sut: str = ""
    tester: str = ""
    allstar: str = ""
    ambed_addr: str = ""
    ambed_port: int = 10100
    ambed_stream_cap: int = 0
    xlxd_bin: str = ""
    xlxd_ref_bin: str = ""
    reflector: str = "XLX999"
    iax_secret: str = field(default="", repr=False)
    out: str = "out"
    # keys set explicitly by env or TOML (rig host defaults are not)
    given: set = field(default_factory=set)

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
    path = path or env.get("DVT_TARGET_FILE")
    if path:
        for key, value in _read_toml(path).items():
            if key in KEYS:
                values[key] = value
    for key in KEYS:
        value = env.get("DVT_" + key.upper())
        if value:
            values[key] = value
    target = Target(given=set(values))
    for key, value in values.items():
        setattr(target, key, int(value) if key in ("ambed_port", "ambed_stream_cap") else str(value))
    for key, offset in HOST_OFFSETS.items():
        if not getattr(target, key):
            setattr(target, key, target.host(offset))
    return target

