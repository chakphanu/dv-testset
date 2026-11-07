# SPDX-License-Identifier: MIT
"""The wire checker's list of rig hosts (dvtest.check.rig_config) against
rig/compose.yml: a service added by a profile (twonode dvr-peer .16 and
transcoder-peer .17, aprsc .47, dw-a .48 / dw-b .49, the T5n alias .46) must
not make every datagram to it an 'xlx.unknown_peer ... is not a rig host'."""
import os
import re

import pytest

from dvtest import check
from dvtest.target import CHECK_HOST_OFFSETS, HOST_OFFSETS

from test_check_common import Wire

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# compose addresses that are not a UDP protocol peer of any captured service:
# they may stay out of the checker's list (web, browser, NAT lab, load, g3
# and capture-only containers). A new address must be added to HOST_OFFSETS /
# CHECK_HOST_OFFSETS or here, with the reason.
NOT_A_PROTOCOL_PEER = {
    31: "load-node (profile load, HTTP/TCP only)",
    91: "browser", 92: "nat-router", 93: "nat-router alias", 94: "coturn", 95: "nat-ref-a",
    96: "nat-ref-b", 97: "webload",
}


def compose_offsets():
    text = open(os.path.join(ROOT, "rig", "compose.yml")).read()
    found = set()
    for m in re.finditer(r'ipv4_address: "\$\{DVT_NET:-172\.30\.0\}\.(\d+)"', text):
        found.add(int(m.group(1)))
    # the T5n alias is a plain list entry under the asl-nat sidecar
    for m in re.finditer(r'^\s+- \$\{DVT_NET:-172\.30\.0\}\.(\d+)\s*$', text, re.M):
        found.add(int(m.group(1)))
    return found


def test_new_profile_hosts_are_rig_hosts():
    hosts = check.rig_config("172.30.0.0/24")["hosts"]
    assert hosts["dvr_peer"] == "172.30.0.16" and hosts["ambed_peer"] == "172.30.0.17"
    assert hosts["aprsc"] == "172.30.0.47" and hosts["dw_a"] == "172.30.0.48" and hosts["dw_b"] == "172.30.0.49"
    assert hosts["asl_nat_alias"] == "172.30.0.46"
    # follows the subnet
    assert check.rig_config("10.9.8.0/24")["hosts"]["dvr_peer"] == "10.9.8.16"
    assert not set(HOST_OFFSETS) & set(CHECK_HOST_OFFSETS)


def test_every_compose_address_is_known_or_excused():
    known = set(HOST_OFFSETS.values()) | set(CHECK_HOST_OFFSETS.values()) | set(NOT_A_PROTOCOL_PEER)
    missing = sorted(compose_offsets() - known)
    assert not missing, "rig/compose.yml addresses unknown to the wire checker: %s" % missing
    # the excuse table does not hide an address the checker already knows
    assert not set(NOT_A_PROTOCOL_PEER) & (set(HOST_OFFSETS.values()) | set(CHECK_HOST_OFFSETS.values()))


def test_xlx_link_to_the_twonode_peer_is_clean(tmp_path):
    """The XLX peer link of node 1 (xlxd-sut .11) to dvr-peer .16 raises no unknown_peer."""
    rig = check.rig_config("172.30.0.0/24")
    w = Wire()
    # XLX interlink keepalive from the peer (xlxd interlink, port 10002): callsign + 6 modules
    w.send("172.30.0.16", 10002, "172.30.0.11", 10002, b"XLX998   ", 0.01)
    w.send("172.30.0.11", 10002, "172.30.0.16", 10002, b"XLX999   ", 0.01)
    got = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"), rig)
    assert "unknown_peer" not in {f["code"].split(".")[-1] for f in got["findings"]}


def test_a_host_outside_the_rig_is_still_an_error(tmp_path):
    rig = check.rig_config("172.30.0.0/24")
    w = Wire()
    w.send("172.30.0.99", 10002, "172.30.0.11", 10002, b"XLX998   ", 0.01)
    w.send("172.30.0.11", 10002, "172.30.0.99", 10002, b"XLX999   ", 0.01)
    got = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"), rig)
    assert "unknown_peer" in {f["code"].split(".")[-1] for f in got["findings"]}
