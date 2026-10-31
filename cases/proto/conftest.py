# SPDX-License-Identifier: MIT
"""Fixtures of the M17 stage 2 cases: the fake reflector peers M17-DVT and
M17-DVU the tester plays for the reflector under test (dvtest/m17_peer.py)."""
import errno
import os
import time

import pytest

from dvtest import m17_peer, specs

MREFD_CALLSIGN = "M17-REF"
DESIGNATIONS = {"dvt": m17_peer.DESIGNATION_DVT, "dvu": m17_peer.DESIGNATION_DVU}


def peer_sides(target, env=None):
    """({side: (reflector address, designation it presents)}, [skip
    reasons]) for the sides that can be judged: "sut" when its profile has
    M17 peering (SPEC["m17_peering"]) and is started natively, "ref" when
    the reference is mrefd (DVT_REF_KIND=mrefd)."""
    env = os.environ if env is None else env
    sides, reasons = {}, []
    if not specs.m17_peering(target):
        reasons.append("sut: xlxd has no M17 peering" if target.sut_profile == specs.DEFAULT
                       else "sut: profile %s has no M17 peering" % target.sut_profile)
    elif env.get("DVT_SUT_LAUNCH") == "import":
        reasons.append("sut: the legacy xlxd importer has no M17 interlink input; run the peer cases with "
                       "the native-config launch")
    else:
        sides["sut"] = (target.xlxd_sut, (specs.rule(target, "m17") or {}).get("designation"))
    if target.ref_kind != "mrefd":
        reasons.append("ref: the reference is %s, not mrefd (DVT_REF_KIND=mrefd)" % target.ref_kind)
    else:
        sides["ref"] = (target.xlxd_ref, env.get("DVT_MREFD_CALLSIGN") or MREFD_CALLSIGN)
    return sides, reasons


class HubPool:
    """The session hubs: one PeerHub per (peer key, local ip), on the port
    DVT_M17_PEER_{DVT,DVU}_PORT (default 17011 / 17012) with the module list
    of DVT_M17_PEER_*_MODULES. A bind to an address the rig has not added yet
    (the tester-addr sidecar) is retried for a few seconds."""

    def __init__(self, config, wait=15.0):
        self.config, self.wait, self.hubs = config, wait, {}

    def get(self, key, bind_ip):
        hub = self.hubs.get((key, bind_ip))
        if hub is not None:
            return hub
        end = time.monotonic() + self.wait
        while True:
            try:
                hub = m17_peer.PeerHub(DESIGNATIONS[key], self.config[key]["modules"], port=self.config[key]["port"],
                                       bind_ip=bind_ip, name="%s@%s" % (key, bind_ip))
                break
            except OSError as exc:
                if getattr(exc.__cause__, "errno", None) == errno.EADDRNOTAVAIL and time.monotonic() < end:
                    time.sleep(0.5)
                    continue
                pytest.fail("cannot hold the %s peer port: %s" % (DESIGNATIONS[key], exc))
        self.hubs[(key, bind_ip)] = hub
        return hub

    def close(self):
        for hub in self.hubs.values():
            hub.close()


@pytest.fixture(scope="session")
def m17_peer_hubs():
    """The HubPool of the session; closed (DISC to every contacted
    reflector) at the end of the session."""
    pool = HubPool(m17_peer.peer_env(os.environ))
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def m17_peers(rig, target, request):
    """{side: PeerSet} for every judged side, each with .dvt and .dvu
    (PeerLink to the reflector's M17-DVT / M17-DVU peer rows), .address (the
    reflector ip, client port .port 17000) and .side. Sides: "sut" (profile
    with M17 peering, native launch) and "ref" (mrefd as reference). Both
    links of a side are up when the fixture returns (up to 40 s; a
    reflector that is already linked is recognised from its PING), logs are
    empty and modes are the defaults. Skips with the combined reason when no
    side is judgeable; fails with what both sides sent when a link does not
    come up."""
    sides, reasons = peer_sides(target)
    if not sides:
        pytest.skip("no M17 peering to judge: " + "; ".join(reasons))
    pool = request.getfixturevalue("m17_peer_hubs")
    binds = m17_peer.peer_binds(sides, os.environ, target.tester)
    made = {}
    for side, (address, designation) in sides.items():
        links = {key: pool.get(key, binds[side][key]).peer(
                     address, expect_designation=designation, label="%s %s" % (side, DESIGNATIONS[key]))
                 for key in DESIGNATIONS}
        made[side] = m17_peer.PeerSet(side, address, links["dvt"], links["dvu"])
    failures = []
    for side, peers in made.items():
        peers.reset()
        try:
            peers.link(m17_peer.LINK_TIMEOUT)
        except m17_peer.PeerLinkTimeout as exc:
            failures.append(str(exc))
        peers.clear()
    if failures:
        pytest.fail("M17 peers not linked: " + " || ".join(failures))
    yield made
    for peers in made.values():
        peers.reset()
