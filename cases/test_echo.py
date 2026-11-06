# SPDX-License-Identifier: MIT
"""DV-Router's built-in echo service (ECHO1, ECHO1c; design doc echo.md): a local over on a configured
module is recorded and replayed once, delay_ms after its end, as an ordinary over of the same source
profile. Black box: UDP clients on module E, the hub metrics through the control channel and the
rendered dv-router.toml of the run. Table, snippets, how to run and findings: docs/echo-cases.md.

  E1a..d  per protocol (D-STAR, DMR, M17, YSF): one replay 0.8..2.0 s after the over; talker identity kept;
          payload byte for byte as sent and as the live relay; about 3 s long; the talker hears it too
  E2      an over under 50 frames is not replayed (5 s window)
  E3      an over longer than max_seconds is replayed truncated (live edit to 5 s, or echo-short.toml)
  E4      no storm: nothing more for 3 x delay_ms after the replay, a replay is not recorded again
  E5      overs of other origin classes are not replayed: XLX interlink peer, USRP, M17 peer
          (NXDN, P25: not reachable, docs/echo-cases.md)
  E6      a human keying during the replay gets nothing through; the floor is free after it
  E7      toggle by editing the rendered TOML: E = false, back to true, the row removed while playing
  E8      two XLX-linked nodes: not reachable (one DV-Router node in the rig)
  telemetry  dv_echo_overs_total moves by the played / short / truncated overs of the run

Needs DVT_SUT_PROFILE=dvrouter, native launch and rig/config/dv-router/echo.toml in DVT_SUT_CONFIG_EXTRA.
A difference the specification does not settle is a FINDING line in out/<run>/echo.txt, never an assertion.
"""
import os
import socket
import time
from collections import Counter, namedtuple

import pytest

from dvtest import clients, crossaudio, echo, specs
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import usrp
from dvtest import metrics as metrics_mod
from dvtest.control import RigControl
from dvtest.proto import dextra, m17
from echo_support import (
    MODULE, Drainer, EchoCfg, Scene, UsrpDyn, note, split_runs, wait_for_over, wait_reload,
)

pytestmark = pytest.mark.rig

KINDS = ("dstar", "dmr", "m17", "ysf")
FRAMES_E1 = 150                     # 3 s
SHORT_FRAMES = 25                   # under a second, under the 50 frame threshold
PROBE_FRAMES = 60                   # 1.2 s: over the threshold, short to wait for
START_LOW, START_HIGH = 0.8, 2.0    # allowed delay of the replay after the over (spec: about 1.0 s)
LEN_LOW, LEN_HIGH = 2.5, 3.6        # allowed length of the replay of a 3 s over
MAX_FOR_E3 = 5                      # max_seconds the E3 case uses (the smallest DV-Router accepts)

# overs the run made that the hub should have counted (the last test compares)
TALLY = Counter()

EchoRig = namedtuple("EchoRig", "ip cfg delay max_seconds modules control base")


# ---------------------------------------------------------------- the rig and the config

def hub_text(control):
    if control is None:
        return None
    try:
        return control.metrics("xlxd-sut")
    except (RuntimeError, TimeoutError, OSError, ValueError):
        return None


def results(text):
    return {r: echo.echo_overs(text or "", MODULE, r) for r in ("played", "short", "truncated")}


def probe(ip, delay, seed):
    """One 60 frame D-STAR over on E; True when its replay was heard (and has ended)."""
    scene = Scene(ip, "dstar")
    try:
        scene.over(PROBE_FRAMES, seed)
        replay = wait_for_over(scene, scene.ends[0] + 0.05, timeout=delay + 3.0)
        if replay is None:
            return False
        scene.until(scene.ends[0] + delay + echo.over_seconds(PROBE_FRAMES) + 1.0)
        return True
    finally:
        scene.close()


def ensure_replaying(ip, delay, budget=60.0, label="warm"):
    """Probe until a replay is heard (the echo child may still be starting after a spawn or a reload)."""
    end = time.monotonic() + budget
    n = 0
    while True:
        n += 1
        if probe(ip, delay, "%s-%d" % (label, n)):
            TALLY["played"] += 1
            return True
        TALLY["played_maybe"] += 1
        if time.monotonic() > end:
            return False
        time.sleep(2.0)


@pytest.fixture(scope="module")
def echo_rig(rig, target):
    if target.sut_profile != "dvrouter":
        pytest.skip("the echo service is DV-Router's: DVT_SUT_PROFILE=dvrouter")
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("echo is configured in the TOML: DVT_SUT_LAUNCH=native with rig/config/dv-router/echo.toml")
    cfg = EchoCfg.locate(target)
    settings = cfg.settings if cfg else {"max_seconds": echo.DEFAULT_MAX_SECONDS, "delay_ms": echo.DEFAULT_DELAY_MS,
                                         "modules": {MODULE: True}}
    if settings is None:
        pytest.skip("no [echo] table in the rendered config: add rig/config/dv-router/echo.toml to DVT_SUT_CONFIG_EXTRA")
    if not settings["modules"].get(MODULE):
        pytest.skip("module %s is not switched on in [echo] modules (%s)" % (MODULE, settings["modules"]))
    control = RigControl(os.path.join(target.out, "control")) if os.environ.get("DVT_RIG_CONTROL") == "1" else None
    base = results(hub_text(control))
    if cfg is None:
        note(target, "setup", "RECORD", "rendered config not found under out/: defaults assumed, no live edit")
    delay = settings["delay_ms"] / 1000.0
    if not ensure_replaying(target.xlxd_sut, delay, label="start"):
        pytest.fail("module %s does not replay a %d frame over within 60 s of the start: is dv-svc-echo running "
                    "([echo] modules = { E = true } in the rendered config)?" % (MODULE, PROBE_FRAMES))
    return EchoRig(target.xlxd_sut, cfg, delay, settings["max_seconds"], settings["modules"], control, base)


@pytest.fixture(autouse=True)
def counter_trace(request):
    """DVT_ECHO_DEBUG_COUNTERS=1: after every case wait one telemetry period and note the delta of dv_echo_overs_total against
    the start of the module and the tally of the cases so far (root-cause hunting for E7: which over lacks its count)."""
    yield
    if os.environ.get("DVT_ECHO_DEBUG_COUNTERS") != "1" or "echo_rig" not in request.fixturenames:
        return
    try:
        rig = request.getfixturevalue("echo_rig")
        target = request.getfixturevalue("target")
        if rig.control is None:
            return
        time.sleep(11.0)
        text = hub_text(rig.control) or ""
        got = {k: v - rig.base[k] for k, v in results(text).items()}
        fam = [ln for ln in text.splitlines() if ln.startswith(("dv_echo", "dv_edge_drops_total", "dv_edge_x_")) or "capture_tick" in ln]
        note(target, "trace", "RECORD", "after %s: delta %s, tally %s, families %s" % (request.node.name, got, dict(TALLY), fam))
    except Exception as exc:                                  # noqa: BLE001 - debugging aid only
        print("counter trace failed: %s" % exc)


def need_cfg(echo_rig, what):
    cfg = echo_rig.cfg
    if cfg is None:
        pytest.skip("not reachable: the rendered config is not visible in out/ (%s)" % what)
    if not cfg.writable():
        pytest.skip("not reachable: %s is not writable by the tester (%s)" % (cfg.path, what))
    return cfg


# ---------------------------------------------------------------- E1 / E4: one over per protocol

Run = namedtuple("Run", "kind ident wire raw end observed_until listener talker delay listener_packets")


@pytest.fixture(scope="module", params=KINDS)
def e1_run(request, echo_rig, target):
    kind = request.param
    frames = None
    if kind == "ysf":
        # YSF: the SUT's egress of a foreign-origin (replayed) stream re-encodes the AMBE+2 frames, so frames that are
        # valid codewords are needed (PRNG bytes are not; first run 20261104T052813Z): a tone encoded by the transcoder
        frames = encoded_frames(target, FRAMES_E1)
    scene = Scene(echo_rig.ip, kind)
    try:
        scene.over(FRAMES_E1, "e1", frames=frames)
        end = scene.ends[0]
        until = end + echo_rig.delay + echo.over_seconds(FRAMES_E1) + 3 * echo_rig.delay + 1.0
        scene.until(until)
        run = Run(kind, scene.ident(), scene.sent[0], scene.raw[0], end, until, scene.overs("listener"), scene.overs("talker"),
                  echo_rig.delay, list(zip(*scene.packets("listener"))) if kind == "ysf" else [])
    finally:
        scene.close()
    TALLY["played"] += len([o for o in run.listener if o.start > run.end + 0.05])
    return run


def ysf_packets(packets, end, replay, limit=4):
    """First and last YSF stream packets before (live relay) or after (replay) the end of the over, as short text."""
    from dvtest.proto import ysf
    out = []
    for data, t in packets:
        info = ysf.parse(bytes(data))
        if info is None or info["kind"] not in ("header", "voice", "terminator") or (t > end + 0.05) != replay:
            continue
        f = info["fich"] or {}
        out.append("%s fi=%s dt=%s fn=%s ft=%s c=%d eot=%d %s" % (
            info["kind"], f.get("fi"), f.get("dt"), f.get("fn"), f.get("ft"), info["counter"], info["eot"],
            info["frames"][0].hex() if info.get("frames") else "-"))
    return out[:limit] + ["..."] + out[-2:]


def encoded_frames(target, n):
    """n AMBE+2 (DMR form) frames of a tone, encoded by transcoder-ref, passed through the YSF VCH coder once."""
    from dvtest.proto import ambed, ysf
    pcm = signals.frames(signals.tone(700, FRAMES_E1 * 20))[:n]
    got = ambed.transcode(target.ambed_ref, ambed.PCM, [ambed.AMBE2PLUS], pcm, port=target.ambed_port)[ambed.AMBE2PLUS]
    return [ysf.vch_decode(ysf.vch_encode(f)) for f in got][:n]


def replays_of(run, who="listener"):
    overs = run.listener if who == "listener" else run.talker
    return [o for o in overs if o.start > run.end + 0.05]


def original_of(run):
    first = run.listener[0] if run.listener else None
    assert first is not None and first.n_voice > 0 and first.start < run.end, \
        "precondition: the listener did not hear the over itself (%d overs)" % len(run.listener)
    return first


def test_e1a_one_replay_after_the_delay(e1_run, target):
    original_of(e1_run)
    replays = replays_of(e1_run)
    assert len(replays) == 1, "%s: %d replays heard (starts after the over: %s)" % (
        e1_run.kind, len(replays), ["%.2f" % (o.start - e1_run.end) for o in replays])
    delay = replays[0].start - e1_run.end
    note(target, "E1a[%s]" % e1_run.kind, "RECORD", "replay starts %.2f s after the last packet of the over" % delay)
    assert echo.in_window(delay, START_LOW, START_HIGH), "%s: replay starts %.2f s after the over, want %.1f..%.1f" % (
        e1_run.kind, delay, START_LOW, START_HIGH)


def test_e1b_talker_identity_is_kept(e1_run, target):
    original = original_of(e1_run)
    replays = replays_of(e1_run)
    assert len(replays) == 1, "%s: %d replays" % (e1_run.kind, len(replays))
    replay = replays[0]
    assert replay.idents == [e1_run.ident], "%s: replay names %s, the talker is %r (the live relay names %s)" % (
        e1_run.kind, replay.idents, e1_run.ident, original.idents)
    assert replay.idents == original.idents
    if e1_run.kind == "dstar":
        assert replay.header, "dstar: the replay has no header (the design synthesizes one)"


def test_e1c_payload_is_byte_identical_and_about_3_s(e1_run, target):
    original = original_of(e1_run)
    replays = replays_of(e1_run)
    assert len(replays) == 1, "%s: %d replays" % (e1_run.kind, len(replays))
    replay = replays[0]
    kind = e1_run.kind
    live = echo.compare_payload(e1_run.raw, original.frames)
    got = echo.compare_payload(e1_run.raw, replay.frames)
    note(target, "E1c[%s]" % kind, "RECORD", "sent %d frames (%d on the wire); live relay %d frames, prefix_ok=%s extra=%d; replay %d "
         "frames, prefix_ok=%s extra=%d; replay equals the live relay: %s; frame 0 sent/live/replay %s / %s / %s" % (
             len(e1_run.raw), len(e1_run.wire), len(original.frames), live.prefix_ok, len(live.extra), len(replay.frames),
             got.prefix_ok, len(got.extra), replay.frames == original.frames,
             e1_run.raw[0].hex(), original.frames[0].hex() if original.frames else "-",
             replay.frames[0].hex() if replay.frames else "-"))
    if not got.prefix_ok and kind == "ysf":
        note(target, "E1c[ysf]", "RECORD", "YSF packets of the replay (kind, FICH fi/dt/fn/ft, counter, eot, first frame) %s; of the live relay %s" % (
            ysf_packets(e1_run.listener_packets, e1_run.end, True), ysf_packets(e1_run.listener_packets, e1_run.end, False)))
    if not got.prefix_ok:
        raw, heard = e1_run.raw, replay.frames
        at = next((k for k in range(len(heard) - len(raw) + 1) if heard[k:k + len(raw)] == raw), None)
        same = sum(1 for a, b in zip(raw, heard) if a == b)
        note(target, "E1c[%s]" % kind, "RECORD", "replay is not the sent frames from frame 0: the sent frames start at replay frame %s; %d of the first "
             "%d frames equal; first 6 replay frames %s" % (at, same, len(raw), [f.hex() for f in heard[:6]]))
    if not live.prefix_ok:
        note(target, "E1c[%s]" % kind, "FINDING", "the live relay itself does not carry the sent frames unchanged (first difference at "
             "frame %s): not an echo defect" % live.first_diff)
    if got.prefix_ok and got.extra:
        note(target, "E1c[%s]" % kind, "FINDING", "the replay carries %d frame(s) after the %d sent (the live relay: %d), %s" % (
            len(got.extra), len(e1_run.raw), len(live.extra),
            "all zero" if not any(any(f) for f in got.extra) else "not all zero"))
    if got.prefix_ok and replay.frames != original.frames:
        note(target, "E1c[%s]" % kind, "FINDING", "the replay is not frame for frame what the live relay carried (%d vs %d frames): "
             "lead-in / end frames of the protocol are not part of the sent audio" % (len(replay.frames), len(original.frames)))
    if kind == "ysf":
        # dv-router ruling 2026-11-04 (xlxd parity): the YSF ingress steps the gain index b2 by +1.0 dB and the YSF egress of a
        # foreign-origin stream by -0.4 dB; the echo replay passes both. Every other bit is unchanged; the live relay (raw path) is exact.
        chk = crossaudio.gain_check(e1_run.raw, replay.frames, ("p10", "m04"))
        note(target, "E1c[ysf]", "RECORD", "gain check of %d frames (all bits but b2 equal, b2 = M04[P10[b2]]): %d bad %s" % (
            chk["checked"], len(chk["bad"]), chk["bad"][:3]))
        assert chk["checked"] == len(e1_run.raw) and not chk["bad"], \
            "ysf: %d of %d replay frames differ from the sent ones by more than the two b2 steps (index, b2 sent, b2 heard, b2 wanted, " \
            "other bits equal): %s" % (len(chk["bad"]), chk["checked"], chk["bad"][:3])
        assert len(replay.frames) - len(e1_run.raw) == 5, "ysf: %d frames after the sent ones, want 5 (the terminator's zero unit and pads)" % (
            len(replay.frames) - len(e1_run.raw))
    else:
        assert got.prefix_ok, "%s: the replay differs from what was sent at frame %s of %d (heard %d, missing %d; the live relay " \
            "carried the sent frames unchanged: %s)" % (kind, got.first_diff, len(e1_run.raw), len(replay.frames), got.missing, live.prefix_ok)
    length = echo.duration(replay)
    note(target, "E1c[%s]" % kind, "RECORD", "replay %.2f s, %d voice packets" % (length or 0, replay.n_voice))
    assert echo.in_window(length, LEN_LOW, LEN_HIGH), "%s: replay lasts %s s, want %.1f..%.1f" % (kind, length, LEN_LOW, LEN_HIGH)
    assert replay.ended, "%s: the replay has no end of stream" % kind


def test_e1d_talker_hears_the_replay_too(e1_run, target):
    replays = replays_of(e1_run)
    assert len(replays) == 1, "%s: %d replays at the listener" % (e1_run.kind, len(replays))
    mine = replays_of(e1_run, "talker")
    assert len(mine) == 1, "%s: the talker's own client heard %d replays" % (e1_run.kind, len(mine))
    assert mine[0].frames == replays[0].frames, "%s: the talker heard another payload than the listener" % e1_run.kind
    assert mine[0].idents == replays[0].idents
    if e1_run.kind == "dstar":
        assert mine[0].header, "dstar: the talker's client got no header before the replay"


def test_e4_no_storm_after_the_replay(e1_run, target):
    original_of(e1_run)
    for who in ("listener", "talker"):
        overs = e1_run.listener if who == "listener" else e1_run.talker
        later = [o for o in overs if o.start > e1_run.end + 0.05]
        assert len(later) == 1, "%s %s: %d overs after the original, want exactly the one replay" % (e1_run.kind, who, len(later))
        quiet = e1_run.observed_until - later[0].end
        assert quiet >= 3 * e1_run.delay - 0.1, "%s %s: only %.2f s watched after the replay" % (e1_run.kind, who, quiet)


# ---------------------------------------------------------------- E2: short over

@pytest.mark.parametrize("kind", ["dstar", "dmr"])
def test_e2_short_over_is_not_replayed(echo_rig, target, kind):
    scene = Scene(echo_rig.ip, kind)
    try:
        scene.over(SHORT_FRAMES, "e2")
        end = scene.ends[0]
        scene.until(end + 5.5)
        listener, talker = scene.overs("listener"), scene.overs("talker")
    finally:
        scene.close()
    assert listener and listener[0].n_voice > 0, "precondition: the listener did not hear the short over"
    TALLY["short"] += 1
    assert [o for o in listener if o.start > end + 0.05] == [], "%s: a replay of a %d frame over reached the listener" % (kind, SHORT_FRAMES)
    assert [o for o in talker if o.start > end + 0.05] == [], "%s: a replay of a %d frame over reached the talker" % (kind, SHORT_FRAMES)


# ---------------------------------------------------------------- E3: long over

def test_e3_long_over_is_truncated(echo_rig, target):
    limit = echo_rig.max_seconds
    cfg = echo_rig.cfg
    edited = False
    if limit > MAX_FOR_E3 + 3:
        cfg = need_cfg(echo_rig, "E3 needs max_seconds <= 8: name echo-short.toml, or let the case edit it")
        cfg.apply(max_seconds=MAX_FOR_E3)
        edited = True
        limit = MAX_FOR_E3
        wait_reload()
    problems = []
    try:
        for kind in ("dstar", "dmr"):
            scene = Scene(echo_rig.ip, kind)
            try:
                n = int((limit + 3) * 50)
                scene.over(n, "e3")
                end = scene.ends[0]
                scene.until(end + echo_rig.delay + limit + 2.0)
                overs, wire = scene.overs("listener"), scene.sent[0]
            finally:
                scene.close()
            replays = [o for o in overs if o.start > end + 0.05]
            TALLY["truncated"] += len(replays)
            if len(replays) != 1:
                problems.append("%s: %d replays of the %d s over" % (kind, len(replays), limit + 3))
                continue
            replay = replays[0]
            length = echo.duration(replay)
            cap = echo.retained_frames(limit)
            note(target, "E3[%s]" % kind, "RECORD", "over %d frames, max_seconds %d: replay %.2f s, %d frames (cap %d)" % (
                n, limit, length or 0, len(replay.frames), cap))
            if not echo.in_window(length, limit - 0.5, limit + 0.5):
                problems.append("%s: replay lasts %.2f s, want %d s +- 0.5" % (kind, length or 0, limit))
            if not cap - 3 <= len(replay.frames) <= cap:
                problems.append("%s: replay has %d frames, want %d..%d (whole bundles, at most max_seconds * 50)" % (
                    kind, len(replay.frames), cap - 3, cap))
            cmp = echo.compare_payload(wire[:len(replay.frames)], replay.frames)
            if not cmp.identical:
                problems.append("%s: the retained prefix differs from what was sent at frame %s" % (kind, cmp.first_diff))
    finally:
        if edited:
            cfg.restore()
            wait_reload()
    assert not problems, "; ".join(problems)


# ---------------------------------------------------------------- E5: other origin classes

def stray_replays(scene, ends, window):
    """Overs the listener heard that start after the end of the over that was sent (and in the window)."""
    return [o for o in scene.overs("listener") if any(0.05 < o.start - e < window for e in ends)]


@pytest.mark.interlink
def test_e5a_xlx_peer_over_is_not_replayed(echo_rig, target):
    if os.environ.get("DVT_INTERLINK") != "on" or not target.is_given("xlxd_peer_sut"):
        pytest.skip("no interlink peer: run.sh --profile interlink with rig/config/dv-router/xlx-peer.toml")
    if MODULE not in os.environ.get("DVT_INTERLINK_MODULES", "DE"):
        pytest.skip("module %s is not interlinked (DVT_INTERLINK_MODULES)" % MODULE)
    if echo_rig.cfg is not None and "edges.xlx.peer" not in echo_rig.cfg.original:
        pytest.skip("the SUT has no XLX peer row: add rig/config/dv-router/xlx-peer.toml to DVT_SUT_CONFIG_EXTRA")
    far = clients.make("dstar", MODULE, 0)               # on the peer reflector (xlxd, no echo)
    near = clients.make("dstar", MODULE, 1)              # on the SUT
    drain = None
    try:
        assert far.link(target.xlxd_peer_sut), "no DExtra link to the peer reflector"
        assert near.link(echo_rig.ip), "no DExtra link to the SUT"
        drain = Drainer(near.client)
        drain.start()
        window = echo_rig.delay + 3.0 + 2.0
        carried, ends = False, []
        deadline = time.monotonic() + 60
        n = 0
        while time.monotonic() < deadline:
            n += 1
            frames = echo.make_frames("dstar", PROBE_FRAMES, "e5a-probe-%d" % n)
            near.client.inbox.clear()
            near.client.inbox_times.clear()
            clients.talk(far, "dstar", frames, tail=0.0)
            end = far.last_stamps[-1]
            ends.append(end)
            time.sleep(echo_rig.delay + echo.over_seconds(PROBE_FRAMES) + 2.5)
            overs = echo.split_overs(echo.events("dstar", list(near.client.inbox), list(near.client.inbox_times)))
            if overs and overs[0].n_voice >= PROBE_FRAMES * 0.8:
                carried = True
                stray = [o for o in overs[1:] if o.start > end + 0.05]
                assert not stray, "a probe over from the peer was replayed (%d extra overs)" % len(stray)
                break
        assert carried, "the interlink carried no over from the peer to the SUT in 60 s"
        near.client.inbox.clear()
        near.client.inbox_times.clear()
        frames = echo.make_frames("dstar", FRAMES_E1, "e5a")
        clients.talk(far, "dstar", frames, tail=0.0)
        end = far.last_stamps[-1]
        time.sleep(window + 1.0)
        overs = echo.split_overs(echo.events("dstar", list(near.client.inbox), list(near.client.inbox_times)))
        assert overs and overs[0].n_voice > 0, "precondition: the SUT's listener did not hear the peer's over"
        assert echo.compare_payload(frames, overs[0].frames).prefix_ok, "the over from the peer changed on the way"
        assert len(overs) == 1, "an over from the XLX peer (origin class 1) was replayed: %d overs at the SUT's listener, " \
            "the second starts %.2f s after the over" % (len(overs), overs[1].start - end)
    finally:
        if drain is not None:
            drain.stop()
        clients.close(far, near)


def usrp_hub(echo_rig, target):
    rules = specs.rule(target, "usrp") or {}
    if not rules.get("dynamic_server"):
        pytest.skip("no dynamic USRP hub in the profile (DVT_USRP_DYNAMIC=1, specs usrp dynamic_server)")
    return (echo_rig.ip, int(rules.get("dynamic_port", 34001)))


def usrp_join(addr, callsign):
    client = UsrpDyn(addr, callsign)
    end = time.monotonic() + 15
    while not client.register():
        if time.monotonic() > end:
            client.close()
            pytest.skip("the dynamic USRP hub does not answer REG:DVSWITCH at %s:%d" % addr)
    client.join(MODULE)
    return client


def test_e1e_dynamic_usrp_client_over_is_replayed(echo_rig, target):
    """Clients of the dynamic USRP hub are local (origin class 0, dv-router ruling 2026-11-04): their overs are echoed."""
    addr = usrp_hub(echo_rig, target)
    talker, listener = usrp_join(addr, "N0CALL"), usrp_join(addr, "N0TEST")
    try:
        end = talker.talk(FRAMES_E1, drain_with=(listener,))
        stop = end + echo_rig.delay + 3.0 + 2.0
        while time.monotonic() < stop:
            listener.pump(0.1)
        runs = split_runs(listener.voice_packets())
    finally:
        talker.close()
        listener.close()
    note(target, "E1e", "RECORD", "USRP listener heard runs of %s voice packets; replay starts %s s after the last packet of the over" % (
        [len(r) for r in runs], [round(r[0][0] - end, 2) for r in runs[1:]]))
    assert runs and len(runs[0]) >= FRAMES_E1 * 0.5, "precondition: the USRP listener did not hear the over"
    TALLY["played"] += max(0, len(runs) - 1)
    assert len(runs) == 2, "a dynamic-hub USRP over: %d runs heard, want the over and one replay" % len(runs)
    gap = runs[1][0][0] - runs[0][-1][0]
    assert echo.in_window(gap, START_LOW, START_HIGH), "replay starts %.2f s after the over, want %.1f..%.1f" % (gap, START_LOW, START_HIGH)
    assert [d[32:] for _, d in runs[1]] == [d[32:] for _, d in runs[0]], "the replayed PCM is not what the listener heard of the over"
    assert len(runs[1]) == len(runs[0]) == FRAMES_E1, "voice packets %d / %d, want %d" % (len(runs[0]), len(runs[1]), FRAMES_E1)


def test_e5b_static_usrp_peer_over_is_not_replayed(echo_rig, target):
    """A static [[edges.usrp.endpoint]] row is a peer (origin class 2): its overs are not echoed."""
    if echo_rig.cfg is not None and "edges.usrp.endpoint" not in echo_rig.cfg.original:
        pytest.skip("no static USRP peer on E: add rig/config/dv-router/echo-usrp-peer.toml to DVT_SUT_CONFIG_EXTRA")
    addr = usrp_hub(echo_rig, target)
    listener = usrp_join(addr, "N0TEST")
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", 32003))
    peer.setblocking(False)
    heard = []
    try:
        dest = (echo_rig.ip, 34003)
        pcm = signals.frames(signals.tone(700, FRAMES_E1 * 20))
        start = time.monotonic() + 0.1
        for i, frame in enumerate(pcm):
            pace(start, i, 0.020)
            peer.sendto(usrp.voice(i, frame), dest)
            listener.pump(0)
        peer.sendto(usrp.unkey(len(pcm)), dest)
        end = time.monotonic()
        stop = end + echo_rig.delay + 3.0 + 2.0
        while time.monotonic() < stop:
            listener.pump(0.1)
            try:
                while True:
                    heard.append((time.monotonic(), peer.recv(2048)))
            except BlockingIOError:
                pass
        runs = split_runs(listener.voice_packets())
    finally:
        peer.close()
        listener.close()
    peer_runs = split_runs([(t, d) for t, d in heard if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0 and usrp.parse(d).keyup == 1])
    note(target, "E5b", "RECORD", "static USRP peer over: the dynamic listener heard runs %s, the peer itself heard runs %s" % (
        [len(r) for r in runs], [len(r) for r in peer_runs]))
    assert runs and len(runs[0]) >= FRAMES_E1 * 0.5, "precondition: the USRP listener did not hear the static peer's over"
    assert len(runs) == 1, "an over of a static USRP peer (origin class 2) was replayed: the listener heard %d runs" % len(runs)
    assert len(peer_runs) == 0, "the static peer heard %d runs of voice (a replay to it)" % len(peer_runs)


def test_e5c_m17_peer_over_is_not_replayed(echo_rig, target, request):
    if not echo_rig.modules.get("M"):
        pytest.skip("echo is not on for module M (the module of the M17 peers): add rig/config/dv-router/echo-peers.toml")
    m17_peers = request.getfixturevalue("m17_peers")       # binds the peer ports: only when the case can run
    if "sut" not in m17_peers:
        pytest.skip("no M17 peering on the sut")
    peers = m17_peers["sut"]
    scene = Scene(echo_rig.ip, "m17", module="M")
    try:
        # control: a local M17 over on M is replayed (echo is on for M)
        scene.over(FRAMES_E1, "e5c-local")
        end = scene.ends[0]
        scene.until(end + echo_rig.delay + 3.0 + 1.5)
        local = [o for o in scene.overs("listener") if o.start > end + 0.05]
        assert len(local) == 1, "control: a local M17 over on M was replayed %d times (echo off for M?)" % len(local)
        TALLY["played_m"] += 1                              # room m of dv_echo_overs_total, not room e (E7 compares both)
        # the over from the peer (origin class 2)
        frames = echo.make_frames("m17", FRAMES_E1, "e5c-peer")
        packets = m17.voice_packets(frames, 0x5A5A, "N0PEER")
        scene.listener.client.inbox.clear()
        scene.listener.client.inbox_times.clear()
        peers.dvt.clear()
        start = time.monotonic() + 0.1
        for i, p in enumerate(packets):
            delay = start + i * 0.040 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            peers.dvt.send(m17.with_trailer(p, "M"))
        end = time.monotonic()
        scene.until(end + echo_rig.delay + 3.0 + 2.0)
        overs = scene.overs("listener")
    finally:
        scene.close()
    assert overs and overs[0].n_voice > 0, "precondition: the local listener did not hear the peer's over"
    assert len(overs) == 1, "an over from an M17 peer (origin class 2) was replayed: %d overs at the listener" % len(overs)


def test_e5d_nxdn_p25_not_reachable():
    pytest.skip("not reachable: the rig template has no NXDN or P25 peer rows (the native launch ignores DVT_NXDN / DVT_P25), "
                "and the client ports of soak-edges.toml are not peers; their origin class is not in the design")


# ---------------------------------------------------------------- E6: the floor

def test_e6_floor_is_busy_during_the_replay(echo_rig, target):
    scene = Scene(echo_rig.ip, "dstar")
    intruder = dextra.DstarClient("0.0.0.0", callsign="N0BUSY", module=MODULE)
    try:
        assert intruder.link(echo_rig.ip), "the second talker did not link"
        scene.over(FRAMES_E1, "e6-a")
        end_a = scene.ends[0]
        first = wait_for_over(scene, end_a + 0.05, timeout=echo_rig.delay + 3.0)
        assert first is not None, "precondition: no replay started"
        time.sleep(0.7)                                   # well inside the 3 s replay
        f1 = echo.make_frames("dstar", PROBE_FRAMES, "e6-intruder1")
        intruder.talk(f1, clients.fresh_stream_id("dstar"), tail=0.0)
        end_i1 = time.monotonic()
        scene.until(end_a + echo_rig.delay + 3.0 + 1.0)   # the replay is over
        time.sleep(1.5)
        f2 = echo.make_frames("dstar", PROBE_FRAMES, "e6-intruder2")
        intruder.talk(f2, clients.fresh_stream_id("dstar"), tail=0.0)
        end_i2 = time.monotonic()
        scene.until(end_i2 + echo_rig.delay + 1.2 + 1.5)
        overs = scene.overs("listener")
    finally:
        clients.close(intruder)
        scene.close()
    note(target, "E6", "RECORD", "overs at the listener: %s" % [(round(o.start - end_a, 2), o.n_voice) for o in overs])
    heard = {f for o in overs for f in o.frames}
    leaked = [f for f in f1 if f in heard]
    replay = next((o for o in overs if o.start > end_a + 0.05), None)
    TALLY["played"] += (1 if replay is not None else 0) + len([o for o in overs if o.start > end_i2 + 0.05])
    assert not leaked, "%d of %d frames of the human who keyed during the replay were relayed (the floor is busy: DENY 7)" % (
        len(leaked), len(f1))
    assert replay is not None, "no replay"
    got = echo.compare_payload(scene.sent[0], replay.frames)
    assert got.prefix_ok, "the replay was disturbed by the second talker (first difference at frame %s)" % got.first_diff
    assert echo.in_window(echo.duration(replay), LEN_LOW, LEN_HIGH), "replay %.2f s long" % (echo.duration(replay) or 0)
    after = [o for o in overs if o.start > end_i1 + 0.05]
    assert any(echo.compare_payload(echo.expected_frames("dstar", f2), o.frames).prefix_ok for o in after), \
        "the human over after the replay was not relayed: the floor was not free (%d overs after the denied over)" % len(after)


# ---------------------------------------------------------------- E7: toggle

def test_e7a_toggle_off_and_on(echo_rig, target):
    cfg = need_cfg(echo_rig, "E7 edits the rendered TOML")
    off_replays, on_replays, end_on, off_ok = [], [], None, False
    try:
        cfg.apply(modules={MODULE: False})
        wait_reload()
        scene = Scene(echo_rig.ip, "dstar")
        try:
            scene.over(FRAMES_E1, "e7-off")
            end = scene.ends[0]
            scene.until(end + echo_rig.delay + 3.0 + 1.5)
            overs = scene.overs("listener")
            off_ok = bool(overs) and overs[0].n_voice > 0
            off_replays = [o for o in overs if o.start > end + 0.05]
        finally:
            scene.close()
        cfg.apply(modules={MODULE: True})
        wait_reload(extra=2.0)
        scene = Scene(echo_rig.ip, "dstar")
        try:
            scene.over(FRAMES_E1, "e7-on")
            end_on = scene.ends[0]
            scene.until(end_on + echo_rig.delay + 3.0 + 1.5)
            on_replays = [o for o in scene.overs("listener") if o.start > end_on + 0.05]
        finally:
            scene.close()
    finally:
        cfg.restore()
        wait_reload()
    TALLY["played"] += len(on_replays)
    assert off_ok, "precondition: the over was not heard while E = false"
    assert off_replays == [], "E = false: the over was replayed (%d overs after it), %.1f s after the edit" % (
        len(off_replays), echo.reload_wait())
    assert len(on_replays) == 1, "E = true again: %d replays of the next over, want 1" % len(on_replays)
    assert echo.in_window(on_replays[0].start - end_on, START_LOW, START_HIGH)
    note(target, "E7a", "RECORD", "edit applied within %.1f s (waited that long; the watcher polls every 1 s and wants two "
         "equal polls); the lower bound of the latency is not measured" % echo.reload_wait())


# ---------------------------------------------------------------- telemetry (before E7b: removing the last row stops the
# service, and its counters start again from zero when it is respawned)

def test_e7_telemetry_counts_the_overs(echo_rig, target):
    if echo_rig.control is None:
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1): metrics xlxd-sut is not readable")
    if not TALLY:
        pytest.skip("no echo over was made in this run")
    deadline = time.monotonic() + 40
    while True:
        text = hub_text(echo_rig.control)
        got = {k: v - echo_rig.base[k] for k, v in results(text).items()}
        if got["played"] >= TALLY["played"] and got["short"] >= TALLY["short"] and got["truncated"] >= TALLY["truncated"]:
            break
        if time.monotonic() > deadline:
            break
        time.sleep(3.0)
    time.sleep(11.0)                                       # one more telemetry period: a late extra count shows
    text = hub_text(echo_rig.control)
    got = {k: v - echo_rig.base[k] for k, v in results(text).items()}
    note(target, "telemetry", "RECORD", "dv_echo_overs_total{room=%s} delta %s, made by the cases %s; label spelling %s" % (
        MODULE.lower(), got, dict(TALLY), sorted(echo.room_label_cases(text or ""))))
    if "upper" in echo.room_label_cases(text or ""):
        note(target, "telemetry", "FINDING", "the room label is upper case; the design writes the lower case module letter")
    certain, maybe = TALLY["played"], TALLY["played_maybe"]
    assert certain <= got["played"] <= certain + maybe, "result=played moved by %d, the cases made %d (+ up to %d unsure)" % (
        got["played"], certain, maybe)
    assert got["truncated"] == TALLY["truncated"], "result=truncated moved by %d, the cases made %d" % (
        got["truncated"], TALLY["truncated"])
    assert got["short"] >= TALLY["short"], "result=short moved by %d, the cases made %d" % (got["short"], TALLY["short"])
    if got["short"] > TALLY["short"]:
        note(target, "telemetry", "FINDING", "result=short moved by %d, the cases made %d short overs (DMR / YSF link joins "
             "are empty streams; dv-router: not intended, a follow-up counts only streams with a voice unit; not asserted)" % (got["short"], TALLY["short"]))
    if TALLY["played_m"]:
        m_played = echo.echo_overs(text or "", "M", "played")
        note(target, "telemetry", "RECORD", "room m: result=played %d, the cases made %d" % (m_played, TALLY["played_m"]))
        assert m_played >= TALLY["played_m"], "room m: result=played is %d, the cases made %d" % (m_played, TALLY["played_m"])
    assert metrics_mod.families(text or "") >= {"dv_echo_overs_total"}


def test_e7b_row_removed_while_playing(echo_rig, target):
    cfg = need_cfg(echo_rig, "E7 edits the rendered TOML")
    scene = Scene(echo_rig.ip, "dstar")
    outcome = {}
    try:
        scene.over(200, "e7b")                             # 4 s: the replay takes 4 s
        end = scene.ends[0]
        first = wait_for_over(scene, end + 0.05, timeout=echo_rig.delay + 3.0)
        assert first is not None, "precondition: no replay started"
        time.sleep(0.5)
        cfg.apply(modules={})                              # the last row goes: the service stops
        removed = time.monotonic()
        replay, deadline = first, time.monotonic() + 12
        while time.monotonic() < deadline:                 # until the replay has been quiet for 1 s
            later = [o for o in scene.overs("listener") if o.start > end + 0.05]
            if not later:
                break
            replay = later[0]
            if time.monotonic() - replay.end > 1.0:
                break
            time.sleep(0.1)
        cmp = echo.compare_payload(scene.sent[0], replay.frames)
        outcome.update(complete=cmp.prefix_ok and cmp.missing == 0, frames=len(replay.frames), ended=replay.ended,
                       length=echo.duration(replay) or 0.0, after_removal=replay.end - removed)
        # the module must be free 2 s after the replay's last packet
        scene.until(replay.end + 2.0)
        probe_frames = scene.over(PROBE_FRAMES, "e7b-probe")
        end_p = scene.ends[1]
        scene.until(end_p + echo_rig.delay + echo.over_seconds(PROBE_FRAMES) + 2.5)
        overs = scene.overs("listener")
    finally:
        scene.close()
        cfg.restore()
        wait_reload(extra=3.0)
    note(target, "E7b", "RECORD", "replay %s: %d of 200 frames, %.2f s, ended=%s, last packet %.2f s after the edit" % (
        "completed" if outcome["complete"] else "cut", outcome["frames"], outcome["length"], outcome["ended"],
        outcome["after_removal"]))
    TALLY["played_maybe"] += 1
    ensure_replaying(echo_rig.ip, echo_rig.delay, label="after-e7b")
    probe_over = [o for o in overs if o.start > replay.end + 0.5]
    assert probe_over, "the module was not free 2 s after the replay: the human over of %d frames was not relayed" % PROBE_FRAMES
    assert echo.compare_payload(echo.expected_frames("dstar", probe_frames), probe_over[0].frames).prefix_ok, \
        "the human over after the replay arrived changed"
    assert len(probe_over) == 1, "with no [echo] row the human over was replayed (%d overs after the replay)" % len(probe_over)
    assert outcome["complete"] or outcome["ended"], \
        "the replay was cut after %d frames and not closed with an end of stream" % outcome["frames"]


# ---------------------------------------------------------------- E8

def test_e8_two_nodes_not_reachable():
    pytest.skip("not reachable: the rig has one DV-Router node; xlxd-peer-sut is an xlxd (no echo), so two echo nodes "
                "on one XLX link cannot be built from what the rig has")
