# SPDX-License-Identifier: MIT
"""XLX interlink (C6): a stream on an interlinked module reaches clients
of the peer reflector and the other way round, without coming back.

Rig profile interlink: xlxd-peer-sut (XLX998) is interlinked with xlxd-sut
and xlxd-peer-ref (XLX998) with xlxd-ref on DVT_INTERLINK_MODULES (default
DE); the peers have no transcoder. Every direction runs on both pairs in
the same run:

  forward  D-STAR talker on xlxd-sut/-ref, listeners on the XLX998 peer
  reverse  D-STAR talker on the XLX998 peer, listeners on xlxd-sut/-ref

Checks per direction: the D-STAR listener across the interlink gets the
frames byte-exact and one stream only; a second D-STAR client on the
talker's own reflector gets the stream once (no loop over the interlink);
the talker itself gets nothing back. The DMR listener across the link is
differential: xlxd-sut pair == xlxd-ref pair (same transcoder image), and
in the forward direction (transcoded on the talker's reflector) it carries
content.
"""
import os
import time

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.proto import ambed, dextra

pytestmark = [pytest.mark.rig, pytest.mark.interlink]

FRAMES = 60
PEER_CALLSIGN = "XLX998"
PEER_SUT, PEER_REF = 12, 13         # default host offsets (DVT_XLXD_PEER_SUT / _REF win)


@pytest.fixture(scope="module")
def interlink(rig):
    if os.environ.get("DVT_INTERLINK") != "on":
        pytest.skip("no interlink peers: run.sh --profile interlink (DVT_INTERLINK=on)")
    modules = os.environ.get("DVT_INTERLINK_MODULES", "DE")
    pairs = {"sut": (rig.xlxd_sut, os.environ.get("DVT_XLXD_PEER_SUT", rig.host(PEER_SUT))),
             "ref": (rig.xlxd_ref, os.environ.get("DVT_XLXD_PEER_REF", rig.host(PEER_REF)))}
    return modules[0], pairs


@pytest.fixture(scope="module")
def frames(rig):
    """Tone bursts encoded once by transcoder-ref as D-STAR frames."""
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [ambed.AMBEPLUS], pcm,
                           port=rig.ambed_port)[ambed.AMBEPLUS]


def streams(client):
    """Distinct D-STAR streams received: xlxd repeats a header, so count
    stream ids of headers and voice packets."""
    ids = set()
    for p in client.client.inbox:
        parsed = dextra.parse_header(p) or dextra.parse_voice(p)
        if parsed:
            ids.add(parsed["stream_id"] if isinstance(parsed, dict) else parsed[0])
    return len(ids)


def voice_packets(client):
    return [p for p in client.client.inbox if dextra.parse_voice(p)]


def run_direction(talk_ip, far_ip, module, frames, reflector="XLX999"):
    """D-STAR talker on talk_ip; D-STAR + DMR listeners on far_ip, a
    second D-STAR client on talk_ip. Returns what each one received."""
    talker = clients.make("dstar", module, 0)
    far_dstar = clients.make("dstar", module, 1)
    far_dmr = clients.make("dmr", module, 1)
    near_dstar = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=module)
    everyone = (talker, far_dstar, far_dmr, near_dstar)
    try:
        for client, ip in ((far_dstar, far_ip), (far_dmr, far_ip), (near_dstar, talk_ip), (talker, talk_ip)):
            assert client.link(ip), "link to %s failed" % ip
        for client in everyone:
            client.client.drain()
            client.client.inbox.clear()
        clients.talk(talker, "dstar", frames, reflector=reflector)
        out = {"far_dstar": far_dstar.collect(), "far_dmr": far_dmr.collect(),
               "near_dstar": near_dstar.collect(), "far_streams": streams(far_dstar),
               "near_streams": streams(near_dstar)}
        talker.client.drain()
        out["talker_back"] = len(voice_packets(talker))
        return out
    finally:
        clients.close(*everyone)


@pytest.fixture(scope="module")
def results(interlink, frames):
    """{(setup, direction): received}; waits up to 60 s for each interlink
    to carry a stream before the recorded run."""
    module, pairs = interlink
    out = {}
    for setup, (main_ip, peer_ip) in pairs.items():
        deadline = time.monotonic() + 60
        while True:
            probe = run_direction(main_ip, peer_ip, module, frames)
            if len(probe["far_dstar"]) >= FRAMES * 0.8:
                break
            if time.monotonic() > deadline:
                pytest.fail("%s: interlink %s <-> %s carries no stream on %s (%d frames)"
                            % (setup, main_ip, peer_ip, module, len(probe["far_dstar"])))
            time.sleep(5)
        out[(setup, "forward")] = run_direction(main_ip, peer_ip, module, frames)
        out[(setup, "reverse")] = run_direction(peer_ip, main_ip, module, frames, reflector=PEER_CALLSIGN)
    return out


CASES = [(s, d) for s in ("sut", "ref") for d in ("forward", "reverse")]


@pytest.mark.parametrize("setup,direction", CASES)
def test_interlink_dstar_exact(results, frames, setup, direction):
    got = results[(setup, direction)]
    assert got["far_dstar"] == frames, "%s %s: %d frames, content differs" % (
        setup, direction, len(got["far_dstar"]))
    assert got["far_streams"] == 1, "%s %s: %d streams across the interlink" % (
        setup, direction, got["far_streams"])


@pytest.mark.parametrize("setup,direction", CASES)
def test_interlink_no_loop(results, frames, setup, direction):
    got = results[(setup, direction)]
    assert got["talker_back"] == 0, "%s %s: %d voice packets back to the talker" % (
        setup, direction, got["talker_back"])
    assert got["near_streams"] == 1, "%s %s: the talker's reflector sent %d streams" % (
        setup, direction, got["near_streams"])
    assert got["near_dstar"] == frames, "%s %s: own-reflector listener got %d frames" % (
        setup, direction, len(got["near_dstar"]))


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_interlink_dmr_sut_equals_ref(results, direction):
    sut, ref = results[("sut", direction)]["far_dmr"], results[("ref", direction)]["far_dmr"]
    assert sut == ref, "%s DMR across the interlink: sut %d frames, ref %d, content differs" % (
        direction, len(sut), len(ref))
    assert len(ref) >= FRAMES * 0.8, "%s: DMR listener got %d frames" % (direction, len(ref))
    if direction == "forward":
        voiced = [f for f in sut if any(f)]
        assert len(voiced) >= FRAMES * 0.8, "forward DMR carries content in %d frames" % len(voiced)
