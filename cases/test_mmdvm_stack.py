# SPDX-License-Identifier: MIT
"""The REAL client stack of a hotspot against the reflector under test: upstream MMDVMHost with DMRGateway, YSFGateway,
P25Gateway, NXDNGateway and DStarGateway (rig service mmdvm-stack, profile mmdvm), driven by the VIRTUAL MMDVM modem of
dvtest/mmdvm_modem.py that the tester runs on UDP DVT_MMDVM_MODEM_PORT. The real software runs end to end without radio
hardware; what the radio would hear and send is the modem. Run command, address list and the UNVERIFIED list:
docs/mmdvm-stack-cases.md.

  MMDVM1 uplink     the modem keys up 3 s on the mode, MMDVMHost and its gateway carry the over to the reflector:
                    the public state shows the callsign on air (needs --profile web), and one of OUR clients on the same
                    module / talk group receives the over with the right frame count and the same voice words
  MMDVM2 downlink   one of OUR clients talks 3 s into the module; the modem records what MMDVMHost delivers down: the mode,
                    the frame count and the header / LC identity of the talker
  MMDVM3 lifecycle  every gateway is linked (a client row of the public state in its module), the link holds over 65 s
                    (the keepalives), disappears when the stack is stopped (unlink at stop) and comes back when it starts

M17 is not here: the pinned MMDVMHost has no M17 mode (docs/mmdvm-stack-cases.md). Every case skips with the reason unless the
profile is up (DVT_MMDVM=on). NOT RUN ON A RIG yet.
"""
import math
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, mmdvm_modem
from dvtest.proto import dextra, dmr, nxdn, p25, ysf

pytestmark = [pytest.mark.rig]

MODES = ("dstar", "dmr", "ysf", "p25", "nxdn")
LOG = "mmdvm-stack.txt"
OVER_S = 3.0                                    # length of one over
HANDSHAKE_S = float(os.environ.get("DVT_MMDVM_HANDSHAKE_S", "240"))   # the container starts with the rig; MMDVMHost retries the modem
LINK_WAIT_S = float(os.environ.get("DVT_MMDVM_LINK_WAIT_S", "90"))
KEEPALIVE_WINDOW_S = float(os.environ.get("DVT_MMDVM_KEEPALIVE_S", "65"))
UNLINK_WAIT_S = float(os.environ.get("DVT_MMDVM_UNLINK_WAIT_S", "25"))
# the module each gateway links to at start (rig/config/mmdvmstack, .env.example), and the talk group of P25 / NXDN
MODULE = {"dstar": os.environ.get("DVT_MMDVM_MODULE_DSTAR", "H"), "dmr": os.environ.get("DVT_MMDVM_MODULE_DMR", "I"),
          "ysf": os.environ.get("DVT_MMDVM_MODULE_YSF", "J")}
TG = {"p25": int(os.environ.get("DVT_MMDVM_P25_TG", "31672")), "nxdn": int(os.environ.get("DVT_MMDVM_NXDN_TG", "31672"))}
ROOM = dict(MODULE, p25=os.environ.get("DVT_P25_MODULE", "G"), nxdn=os.environ.get("DVT_NXDN_MODULE", "F"))
# the identity of the radio (modem key-up) and of our talker; synthetic: N0CALL / N0TEST, DMR ids 1023007 / 1023008
# DMR: group call on TG 6, which DMRGateway maps to slot 2 TG 9 of the XLX link; YSF: the DG-ID of the frames picks the module
RADIO = {"dstar": {"my": "N0CALL"}, "dmr": {"src": 1023007, "dst": 6}, "ysf": {"src": "N0CALL", "dgid": ysf.module_dgid(MODULE["ysf"])},
         "p25": {"src": 1023007, "dst": TG["p25"]}, "nxdn": {"src": 1001, "dst": TG["nxdn"]}}
TALKER = {"dstar": "N0TEST", "dmr": 1023008, "ysf": "N0TEST", "p25": 1023008, "nxdn": 1002}
# a protocol word of the public client row of each mode's gateway
PROTO_WORDS = {"dstar": ("dextra", "d-star", "dstar"), "dmr": ("dmr", "mmdvm", "homebrew", "dmrhb"), "ysf": ("ysf",),
               "p25": ("p25",), "nxdn": ("nxdn",)}
# frames in one over after the padding the air interface adds (whole bursts / LDUs / RF frames)
PAD = {"dstar": 1, "dmr": 3, "ysf": 5, "p25": 9, "nxdn": 4}
# allowed difference of the frame count (a documented, measured deviation; empty until a run says otherwise)
SLACK = {mode: int(os.environ.get("DVT_MMDVM_SLACK_" + mode.upper(), "0")) for mode in MODES}
# gateways that send an unlink when stopped (SIGTERM): DStarGateway (DExtra disconnect) and DMRGateway (RPTCL). YSFGateway, P25Gateway and
# NXDNGateway send nothing at exit (read in the pinned sources): the timeout of the reflector removes them, so they get a longer window
# (TIMEOUT_GONE_S) and the time is recorded, not judged against UNLINK_WAIT_S (docs/mmdvm-stack-cases.md)
UNLINK_AT_STOP = {"dstar": True, "dmr": True, "ysf": False, "p25": False, "nxdn": False}
TIMEOUT_GONE_S = float(os.environ.get("DVT_MMDVM_TIMEOUT_GONE_S", "180"))


def note(line):
    print("MMDVM-STACK %s" % line, flush=True)
    out = os.environ.get("DVT_OUT")
    if out:
        try:
            with open(Path(out) / LOG, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))
        except OSError:
            pass


def padded(mode, count):
    return int(math.ceil(count / PAD[mode]) * PAD[mode])


# MMDVMHost keeps one mode for RFModeHang (10 s) after RF and NetModeHang (3 s) after network activity and refuses data of
# other modes meanwhile (MMDVM-Host.cpp:860-980); every over is followed by a quiet time before the next one starts
MODE_HANG_S = float(os.environ.get("DVT_MMDVM_MODE_HANG_S", "12"))
_last_activity = [0.0]


def quiet():
    wait = MODE_HANG_S - (time.monotonic() - _last_activity[0])
    if wait > 0:
        time.sleep(wait)


def activity():
    _last_activity[0] = time.monotonic()


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="session")
def mmdvm_on(rig):
    if os.environ.get("DVT_MMDVM") != "on":
        pytest.skip("the real client stack is not up: run scripts/run.sh --profile mmdvm (the tester gets DVT_MMDVM=on)")
    return rig


@pytest.fixture(scope="session")
def modem(mmdvm_on, target):
    """The virtual modem on the tester, with MMDVMHost connected to it (SET_CONFIG seen)."""
    port = int(os.environ.get("DVT_MMDVM_MODEM_PORT", "3334"))
    bind = os.environ.get("DVT_MMDVM_MODEM_BIND") or target.tester or "0.0.0.0"
    transport = mmdvm_modem.UdpTransport((bind, port))
    vm = mmdvm_modem.VirtualModem(transport, log=lambda *a: note("modem " + " ".join(str(x) for x in a))).start()
    if not vm.wait_configured(HANDSHAKE_S):
        vm.stop()
        pytest.fail("MMDVMHost of mmdvm-stack never configured the virtual modem on %s within %.0f s (counts %s); see "
                    "out/<run>/logs/mmdvm-stack.log" % (transport.description, HANDSHAKE_S, vm.counts))
    note("modem %s, host configured: %s, version polls %d" % (transport.description, vm.config, vm.counts["version"]))
    yield vm
    vm.stop()


@pytest.fixture(scope="session")
def web_optional(request):
    """A factory of anonymous web clients, or None when the web profile is not up."""
    try:
        return request.getfixturevalue("web_client")
    except pytest.skip.Exception:
        note("web profile not up: the public state is not checked")
        return None


def state_rows(anon):
    state = anon.get("/api/v1/public/state", pace=False, expect=200).json()
    return state, list((state.get("clients") or {}).get("items") or [])


def row_matches(row, mode):
    room = str(row.get("room") or row.get("module") or "")
    text = " ".join(str(row.get(k, "")) for k in ("protocol", "edge", "kind", "proto")).lower()
    return room == ROOM[mode] and any(word in text for word in PROTO_WORDS[mode])


def linked_modes(anon):
    _state, rows = state_rows(anon)
    return {mode for mode in MODES if any(row_matches(r, mode) for r in rows)}, rows


@pytest.fixture(scope="session")
def stack(modem, web_optional, target):
    """Every gateway linked to the reflector (public state when the web profile is up, else a fixed wait)."""
    if web_optional is None:
        time.sleep(min(LINK_WAIT_S, 40.0))
        return modem
    anon = web_optional()
    end = time.monotonic() + LINK_WAIT_S
    seen = set()
    while time.monotonic() < end:
        seen, rows = linked_modes(anon)
        if seen == set(MODES):
            break
        time.sleep(2.0)
    note("gateways linked in the public state: %s of %s" % (sorted(seen), list(MODES)))
    if rows:
        note("client rows (first 3): %s" % [{k: v for k, v in r.items() if k in ("callsign", "protocol", "edge", "room", "kind")} for r in rows[:3]])
    return modem


class OnAir(threading.Thread):
    """Samples /api/v1/public/state and keeps (room, callsign) of every on-air entry."""

    def __init__(self, anon):
        super().__init__(daemon=True)
        self.anon, self.seen, self.halt = anon, [], threading.Event()

    def run(self):
        while not self.halt.is_set():
            try:
                state = self.anon.get("/api/v1/public/state", pace=False).json()
                for row in state.get("rooms", []):
                    if row.get("onair"):
                        self.seen.append((row.get("room"), row["onair"].get("callsign"), row["onair"].get("callsign_base")))
            except Exception:  # noqa: BLE001 - a missed sample is fine
                pass
            self.halt.wait(0.4)


def make_peer(mode, ip):
    """Our Python client on the same module / talk group as the gateway, linked, inbox empty."""
    ident = TALKER[mode]
    if mode == "dstar":
        peer = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=MODULE["dstar"])
        ok = peer.link(ip)
    elif mode == "dmr":
        peer = dmr.DmrClient("0.0.0.0", dmrid=ident, module=MODULE["dmr"])
        ok = peer.link(ip)
    elif mode == "ysf":
        peer = ysf.YsfClient("0.0.0.0", callsign="N0TEST", module=MODULE["ysf"])
        ok = peer.link(ip)
    elif mode == "p25":
        peer = p25.P25Client("0.0.0.0", callsign="N0TEST", src_id=ident, tg=TG["p25"])
        ok = peer.link(ip)
    else:
        peer = nxdn.NxdnClient("0.0.0.0", TG["nxdn"], nxdn_id=ident, callsign="N0TEST")
        ok = peer.link(ip)
    if not ok:
        clients.close(peer)
        pytest.skip("%s: the reflector under test did not answer our own %s client link (no %s service for clients yet?)"
                    % (mode, mode, mode))
    peer.client.inbox.clear()
    return peer


def close_peer(peer):
    try:
        if getattr(peer, "dest", None):
            peer.unlink()
    finally:
        peer.close()


def talk(peer, mode, frames):
    if mode == "p25" or mode == "nxdn":
        peer.talk(frames, tail=2.0)
    else:
        clients.talk(peer, mode, frames, tail=2.0)


# ------------------------------------------------------------------ MMDVM1 uplink

@pytest.mark.parametrize("mode", MODES)
def test_mmdvm1_uplink_modem_to_reflector(stack, web_optional, target, mode):
    """The modem keys up OVER_S s; the stack carries it to the reflector. Public state: the callsign on air in the module;
    our client on the module hears the over with the right frame count and the voice words of the modem."""
    peer = make_peer(mode, target.xlxd_sut)
    probe = OnAir(web_optional()) if web_optional else None
    try:
        quiet()
        if probe:
            probe.start()
        stack.clear()
        ku = stack.keyup(mode, seconds=OVER_S, **RADIO[mode])
        assert ku.wait(OVER_S + 20), "the key-up did not finish"
        got = peer.collect(settle=6.0)
    finally:
        activity()
        if probe:
            probe.halt.set()
        close_peer(peer)
    want = len(ku.voice)
    note("uplink %s: sent %d words (%d with padding) in %.1f s, listener heard %d" % (mode, ku.voice_count, want, ku.duration_s, len(got)))
    assert got, "%s: nothing arrived at our client on the module after a %.0f s key-up (host wrote %d frames up, modem saw %d)" % (
        mode, OVER_S, len(ku.frames), len(stack.recording()))
    assert abs(len(got) - want) <= SLACK[mode], "%s: %d voice frames heard, the over has %d (slack %d)" % (mode, len(got), want, SLACK[mode])
    assert got[:len(ku.voice)] == ku.voice[:len(got)], "%s: the voice words changed on the way (first differing frame %d)" % (
        mode, next((i for i, (a, b) in enumerate(zip(got, ku.voice)) if a != b), -1))
    if probe is not None:
        calls = sorted({c for _r, c, _b in probe.seen if c})
        rooms = sorted({r for r, _c, _b in probe.seen if r})
        note("uplink %s: on air %s in rooms %s" % (mode, calls, rooms))
        assert probe.seen, "%s: never an on-air entry in the public state" % mode
        assert any(str(c).startswith("N0CALL") or str(b) == "N0CALL" for _r, c, b in probe.seen), \
            "%s: the callsign on air was %s, want N0CALL" % (mode, calls)
        assert ROOM[mode] in rooms, "%s: on air in %s, the gateway is linked to module %s" % (mode, rooms, ROOM[mode])


# ------------------------------------------------------------------ MMDVM2 downlink

@pytest.mark.parametrize("mode", MODES)
def test_mmdvm2_downlink_reflector_to_modem(stack, target, mode):
    """Our client talks OVER_S s into the module; the modem records what MMDVMHost delivers down to RF."""
    peer = make_peer(mode, target.xlxd_sut)
    frames = mmdvm_modem.default_frames(mode, int(OVER_S * 50), seed="down")
    t0 = time.monotonic()
    try:
        quiet()
        stack.clear()
        t0 = time.monotonic()
        talk(peer, mode, frames)
        deadline = time.monotonic() + 10
        down = None
        while time.monotonic() < deadline:
            down = stack.downlink(mode, since=t0)
            if _terminated(mode, down):
                break
            time.sleep(0.5)
    finally:
        activity()
        close_peer(peer)
    other = sorted({r.mode for r in stack.recording(since=t0) if r.mode in MODES} - {mode})
    voice = _voice(mode, down)
    want = padded(mode, len(frames))
    note("downlink %s: %d voice words recorded for %d sent (%d with padding); identity %s" % (mode, len(voice), len(frames), want, _identity(mode, down)))
    assert stack.recording(mode, since=t0), "%s: MMDVMHost wrote nothing down to the modem" % mode
    assert not other, "%s: frames of other modes reached the modem: %s" % (mode, other)
    assert abs(len(voice) - want) <= SLACK[mode], "%s: %d voice frames written down, the over has %d (slack %d)" % (mode, len(voice), want, SLACK[mode])
    ident = _identity(mode, down)
    assert ident == _expected_identity(mode), "%s: header / LC identity %r, want %r" % (mode, ident, _expected_identity(mode))


def _terminated(mode, down):
    if mode == "dstar":
        return down["eot"] > 0
    if mode == "dmr":
        return bool(down[2]["terminators"])
    if mode == "ysf":
        return bool(down["terminators"])
    return bool(down.get("terminated"))


def _voice(mode, down):
    if mode == "dmr":
        return down[2]["voice"]
    return down["voice"]


def _identity(mode, down):
    if mode == "dstar":
        return down["headers"][0]["my"].strip() if down["headers"] else None
    if mode == "dmr":
        return down[2]["headers"][0]["src"] if down[2]["headers"] else None
    if mode == "ysf":
        return down["headers"][0]["src"].strip() if down["headers"] else None
    return down.get("src")


def _expected_identity(mode):
    return TALKER[mode]


# ------------------------------------------------------------------ MMDVM3 lifecycle

def test_mmdvm3_link_keepalive_and_unlink(stack, web_optional, rig_control):
    """Every gateway is a client row of its module in the public state, still there after KEEPALIVE_WINDOW_S (its keepalives are
    answered), gone within UNLINK_WAIT_S of `stop mmdvm-stack` for the gateways that unlink at SIGTERM (D-STAR, DMR; the others leave it to
    the timeout of the reflector, TIMEOUT_GONE_S) and back after `start`."""
    if web_optional is None:
        pytest.skip("needs --profile web: the link is read from the public state (/api/v1/public/state clients)")
    anon = web_optional()
    seen, rows = linked_modes(anon)
    note("lifecycle: linked %s; rows %s" % (sorted(seen), [{k: v for k, v in r.items() if k in ("callsign", "protocol", "edge", "room")} for r in rows[:6]]))
    assert seen == set(MODES), "gateways not linked in the public state: %s (rows: %d)" % (sorted(set(MODES) - seen), len(rows))
    # keepalives: the rows stay for the whole window
    end = time.monotonic() + KEEPALIVE_WINDOW_S
    lost = {}
    while time.monotonic() < end:
        now, _ = linked_modes(anon)
        for mode in set(MODES) - now:
            lost.setdefault(mode, time.monotonic())
        time.sleep(5.0)
    assert not lost, "link dropped during the %.0f s keepalive window: %s" % (KEEPALIVE_WINDOW_S, sorted(lost))
    # unlink at stop
    gone = {}
    t_stop = time.monotonic()
    rig_control("stop", "mmdvm-stack", timeout=90)
    try:
        while time.monotonic() < t_stop + TIMEOUT_GONE_S and len(gone) < len(MODES):
            now, _ = linked_modes(anon)
            for mode in set(MODES) - now:
                gone.setdefault(mode, time.monotonic() - t_stop)
            time.sleep(1.0)
        note("lifecycle: unlinked after stop (s): %s" % {m: round(t, 1) for m, t in sorted(gone.items())})
    finally:
        rig_control("start", "mmdvm-stack", timeout=90)
    for mode in MODES:
        assert mode in gone, "%s: still linked %.0f s after the stack stopped" % (mode, TIMEOUT_GONE_S)
        if UNLINK_AT_STOP[mode]:
            assert gone[mode] <= UNLINK_WAIT_S, "%s: gone after %.1f s, an unlink at stop is expected within %.0f s (the reflector's timeout, not an unlink)" % (
                mode, gone[mode], UNLINK_WAIT_S)
    # and back: MMDVMHost reconnects to the modem, the gateways link again
    stack.configured.clear()
    assert stack.wait_configured(HANDSHAKE_S), "MMDVMHost did not reconfigure the modem after the restart"
    end = time.monotonic() + LINK_WAIT_S
    back = set()
    while time.monotonic() < end and back != set(MODES):
        back, _ = linked_modes(anon)
        time.sleep(2.0)
    assert back == set(MODES), "after start, linked again: %s of %s" % (sorted(back), list(MODES))
