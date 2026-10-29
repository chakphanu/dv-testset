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

B11 (no XLX re-forwarding): test_interlink_no_loop covers the return of a
stream to its origin peer, as far as clients can see it (a peer that is
xlxd drops a returning stream itself). test_interlink_no_xlx_transit uses
a second peer, XLX997 (xlxd-peer2-sut/-ref), interlinked only with
xlxd-sut/-ref: a stream from one XLX peer reaches the reflector's own
clients and never the other XLX peer, while each peer link alone carries
streams from the reflector (control). Cross-family
transit (XLX ingress -> NXDN peer) is allowed: with profiles
interlink,nxdn and DVT_NXDN_MODULE in DVT_INTERLINK_MODULES, a DMR stream
from XLX998 reaches the NXDNReflector of xlxd-sut / -ref (sut == ref).

Behaviour cases (dvtest/specs, DVT_SUT_PROFILE):
B8c test_admission_remote_sid_open_elsewhere: a stream arriving over the
interlink whose stream id is already open on another module of the
reflector is refused (xlxd: CReflector::IsStreamOpen). A control run with
another stream id shows the path itself works.
B6b test_interlink_missing_output_slot_zeroed: with both transcoders
stopped, XLX egress keeps every frame and leaves the transcoded slots
zero: the peer's D-STAR listener gets the frames exact, its DMR and M17
listeners get frames of zeros, none dropped.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.proto import ambed, dextra, nxdn

pytestmark = [pytest.mark.rig, pytest.mark.interlink]

FRAMES = 60
PEER_CALLSIGN = "XLX998"
PEER2_CALLSIGN = "XLX997"
HOLD_MODULE = "H"                   # not interlinked, not a USRP/NXDN/P25 module
HOLD_FRAMES = 200                   # 4 s open stream on HOLD_MODULE


@pytest.fixture(scope="module")
def interlink(rig):
    if os.environ.get("DVT_INTERLINK") != "on":
        pytest.skip("no interlink peers: run.sh --profile interlink (DVT_INTERLINK=on)")
    modules = os.environ.get("DVT_INTERLINK_MODULES", "DE")
    pairs = {"sut": (rig.xlxd_sut, rig.xlxd_peer_sut), "ref": (rig.xlxd_ref, rig.xlxd_peer_ref)}
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


# ---------------------------------------------------------------- B11 XLX transit

def one_talk(talk_ip, reflector, module, frames, listen):
    """D-STAR talker on talk_ip; returns {ip: frames} of a D-STAR listener
    on each listen address."""
    talker = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=module)
    listeners = {ip: dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=module) for ip in listen}
    try:
        for ip, client in listeners.items():
            assert client.link(ip), "link to %s failed" % ip
        assert talker.link(talk_ip), "link to %s failed" % talk_ip
        for client in listeners.values():
            client.client.drain()
            client.client.inbox.clear()
        talker.talk(frames, clients.fresh_stream_id("dstar"), reflector=reflector)
        return {ip: client.collect() for ip, client in listeners.items()}
    finally:
        clients.close(talker, *listeners.values())


@pytest.fixture(scope="module")
def transit(rig, interlink, frames):
    module, pairs = interlink
    peer2 = {"sut": rig.xlxd_peer2_sut, "ref": rig.xlxd_peer2_ref}
    out = {}
    for setup, (main_ip, peer_ip) in pairs.items():
        p2 = peer2[setup]
        deadline = time.monotonic() + 60
        while True:
            control = one_talk(main_ip, "XLX999", module, frames, [peer_ip, p2])
            if all(len(v) >= FRAMES * 0.8 for v in control.values()) or time.monotonic() > deadline:
                break
            time.sleep(5)
        out[(setup, "control")] = {"peer": control[peer_ip], "peer2": control[p2]}
        got = one_talk(peer_ip, PEER_CALLSIGN, module, frames, [main_ip, p2])
        out[(setup, "from_peer")] = {"main": got[main_ip], "other": got[p2]}
        got = one_talk(p2, PEER2_CALLSIGN, module, frames, [main_ip, peer_ip])
        out[(setup, "from_peer2")] = {"main": got[main_ip], "other": got[peer_ip]}
    return out


def test_interlink_no_xlx_transit(transit, frames):
    for setup in ("sut", "ref"):
        control = transit[(setup, "control")]
        assert control["peer"] == frames and control["peer2"] == frames, (
            "%s control: reflector -> XLX998 %d, -> XLX997 %d frames" % (
                setup, len(control["peer"]), len(control["peer2"])))
        for source in ("from_peer", "from_peer2"):
            got = transit[(setup, source)]
            assert got["main"] == frames, "%s %s: own clients got %d frames" % (setup, source, len(got["main"]))
            assert not got["other"], "%s %s: %d frames passed on to the other XLX peer" % (
                setup, source, len(got["other"]))


# ---------------------------------------------------------------- B11 cross-family transit

@pytest.fixture(scope="module")
def dmr_frames(rig):
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [ambed.AMBE2PLUS], pcm,
                           port=rig.ambed_port)[ambed.AMBE2PLUS]


def test_interlink_cross_family_transit(rig, interlink, dmr_frames):
    _module, pairs = interlink
    if os.environ.get("DVT_NXDN") != "on":
        pytest.skip("cross-family transit needs --profile interlink,nxdn")
    module = os.environ.get("DVT_NXDN_MODULE", "")
    if module not in os.environ.get("DVT_INTERLINK_MODULES", "DE"):
        pytest.skip("DVT_NXDN_MODULE %r is not in DVT_INTERLINK_MODULES" % module)
    tg = int(os.environ["DVT_NXDN_TG"])
    nxdn_ip = {"sut": rig.nxdn_sut, "ref": rig.nxdn_ref}
    got = {}
    for setup, (_main_ip, peer_ip) in pairs.items():
        talker = clients.make("dmr", module, 0)
        listener = nxdn.NxdnClient("0.0.0.0", tg)
        try:
            assert listener.link(nxdn_ip[setup]), "NXDN link to %s failed" % nxdn_ip[setup]
            assert talker.link(peer_ip), "DMR link to %s failed" % peer_ip
            listener.client.drain()
            listener.client.inbox.clear()
            clients.talk(talker, "dmr", dmr_frames)
            got[setup] = listener.collect()
        finally:
            clients.close(talker, listener)
    assert len(got["ref"]) >= FRAMES * 0.8, "ref: XLX998 DMR -> NXDN %d frames" % len(got["ref"])
    assert got["sut"] == got["ref"], "XLX998 DMR -> NXDN: sut %d frames, ref %d, content differs" % (
        len(got["sut"]), len(got["ref"]))


# ---------------------------------------------------------------- B8c admission

def collision_run(main_ip, peer_ip, module, frames, same_sid):
    """Holder streams on main HOLD_MODULE with sid S; meanwhile a talker on
    the peer sends a stream on module with S (same_sid) or another sid.
    Returns D-STAR frames of a listener on main module and on the peer."""
    holder = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=HOLD_MODULE)
    talker = dextra.DstarClient("0.0.0.0", callsign="N0CALL", module=module)
    main_listener = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=module)
    peer_listener = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=module)
    everyone = (holder, talker, main_listener, peer_listener)
    try:
        for client, ip in ((main_listener, main_ip), (peer_listener, peer_ip),
                           (holder, main_ip), (talker, peer_ip)):
            assert client.link(ip), "link to %s failed" % ip
        for client in everyone:
            client.client.drain()
            client.client.inbox.clear()
        sid = clients.fresh_stream_id("dstar")
        other = sid ^ 0x5A5A or 1
        hold = threading.Thread(target=holder.talk, args=([frames[0]] * HOLD_FRAMES, sid),
                                kwargs={"tail": 0.5})
        hold.start()
        try:
            time.sleep(0.8)
            talker.talk(frames, sid if same_sid else other, reflector=PEER_CALLSIGN, tail=1.0)
        finally:
            hold.join()
        return {"main": main_listener.collect(), "peer": peer_listener.collect()}
    finally:
        clients.close(*everyone)


@pytest.fixture(scope="module")
def collisions(interlink, frames):
    module, pairs = interlink
    assert HOLD_MODULE not in os.environ.get("DVT_INTERLINK_MODULES", "DE")
    return {(setup, same): collision_run(main_ip, peer_ip, module, frames, same)
            for setup, (main_ip, peer_ip) in pairs.items() for same in (False, True)}


def test_admission_remote_sid_open_elsewhere(collisions, frames, behaviour_spec):
    for setup in ("sut", "ref"):
        control = collisions[(setup, False)]
        assert control["main"] == frames, "%s control (other sid): %d frames over the interlink" % (
            setup, len(control["main"]))
        assert collisions[(setup, True)]["peer"] == frames, "%s: the peer's own listener lost the stream" % setup
    sut, ref = collisions[("sut", True)]["main"], collisions[("ref", True)]["main"]
    spec = behaviour_spec
    if spec is None:
        assert len(sut) == len(ref), "remote stream with a sid open on %s: sut %d frames, ref %d" % (
            HOLD_MODULE, len(sut), len(ref))
        assert not ref, "xlxd-ref delivered %d frames of a remote stream whose sid is open" % len(ref)
    elif spec["remote_sid_open_elsewhere"] == "deny":
        assert not sut, "remote stream with a sid open on %s: %d frames delivered" % (HOLD_MODULE, len(sut))
    else:
        assert sut == frames, "remote stream with a sid open elsewhere: %d frames" % len(sut)


# ---------------------------------------------------------------- B6b zero slots (last: stops the transcoders)

@pytest.fixture(scope="module")
def transcoders_down(rig, rig_control, interlink, frames):
    module, pairs = interlink
    rig_control("stop", "transcoder-sut")
    rig_control("stop", "transcoder-ref")
    try:
        out = {}
        for setup, (main_ip, peer_ip) in pairs.items():
            talker = clients.make("dstar", module, 0)
            far = {kind: clients.make(kind, module, 1) for kind in ("dstar", "dmr", "m17")}
            try:
                for client in far.values():
                    assert client.link(peer_ip), "link to %s failed" % peer_ip
                assert talker.link(main_ip), "link to %s failed" % main_ip
                for client in far.values():
                    client.client.drain()
                    client.client.inbox.clear()
                clients.talk(talker, "dstar", frames)
                out[setup] = {kind: client.collect() for kind, client in far.items()}
            finally:
                clients.close(talker, *far.values())
        return out
    finally:
        rig_control("start", "transcoder-sut")
        rig_control("start", "transcoder-ref")


def test_interlink_missing_output_slot_zeroed(rig, transcoders_down, frames, behaviour_spec):
    """Both chains end in the same XLX998 build, so what its DMR / M17
    clients get is compared sut == ref: the peer itself may add frames
    (e.g. a Codec2 silence frame at the end), the reflector under test
    must not drop the frame or put content in a missing slot."""
    (Path(rig.out) / "interlink-zero-slot.json").write_text(json.dumps(
        {s: {k: [f.hex() for f in v] for k, v in r.items()} for s, r in transcoders_down.items()}, indent=1))
    for setup in ("sut", "ref"):
        got = transcoders_down[setup]
        assert got["dstar"] == frames, "%s: D-STAR over the interlink %d frames, content differs" % (
            setup, len(got["dstar"]))
    ref = transcoders_down["ref"]
    for kind in ("dmr", "m17"):
        voiced = [f for f in ref[kind] if any(f)]
        assert len(voiced) <= 2, "ref: %d %s frames carry content: transcoder not down?" % (len(voiced), kind)
        assert len(ref[kind]) >= FRAMES * 0.8, "ref: %d %s frames" % (len(ref[kind]), kind)
    sut = transcoders_down["sut"]
    if behaviour_spec is not None and behaviour_spec["xlx_missing_output"] == "drop":
        assert not sut["dmr"] and not sut["m17"], "zero frames sent instead of dropped"
        return
    for kind in ("dmr", "m17"):
        assert sut[kind] == ref[kind], "%s without a transcoder: sut %d frames (%d with content), ref %d (%d)" % (
            kind, len(sut[kind]), sum(1 for f in sut[kind] if any(f)),
            len(ref[kind]), sum(1 for f in ref[kind] if any(f)))
