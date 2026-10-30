# SPDX-License-Identifier: MIT
"""xlxd with USRP endpoints when its transcoder goes away and comes back.

B13 (lost and back): xlxd declares the transcoder lost when nothing came
back for TRANSCODER_KEEPALIVE_TIMEOUT (30 s, checked on the 5 s ping
cadence). When the transcoder answers again it is a new connection: xlxd
must probe CAPS once more and PCM (USRP) streams must be transcoded again
through extended opens.

B14 (fast swap): the transcoder is replaced within the keepalive window by
one with other capabilities (here: extended -> legacy, which ignores 24 B
opens and never answers CAPS). xlxd never sees a disconnect, so it keeps
the CAPS of the old transcoder. Current behaviour of the build under test:
the next PCM stream sends an extended open that times out, and every later
stream on a USRP module keeps sending extended opens; no CAPS probe is sent
again, so even D-STAR on that module is no longer transcoded. Desired:
after an open that times out or gets BUSY, probe CAPS again and fall back
to legacy opens for digital input. That case is xfail(strict) until fixed.

B13 with a listener: a D-STAR client stays linked on module B (DExtra
keepalives through the loss), so the USRP PCM streams before the loss and
after the reconnect must be transcoded to D-STAR (the listener hears the
fake's AMBE+ payload) through the new transcoder.

Legacy swap (slow): the extended transcoder is lost (> 30 s) and a legacy
one answers instead, then an extended one is back within the keepalive
window. Under xlxd the observations are only recorded (observed.json);
another profile judges its spec "legacy_swap".

netns cases (marker netns): xlxd XLX999 on 127.0.0.1, FakeAmbed on
127.0.0.2 (stopped and started again by the scenario), DExtra talker on
127.0.0.3, USRP peer on 127.0.0.4, module B bridged to the peer. Needs
DVT_XLXD_BIN (scripts/build-xlxd-netns.sh). Under DVT_SUT_PROFILE xlxd the
netns cases keep the xlxd assertions above; under another profile they read
the profile spec "netns_lifecycle" (dvtest/specs/<profile>.py, e.g. DV-Router:
outputs opened on demand, extended OPEN only) and skip "spec pending" without
it. The xfail of the PR edge case applies only under the xlxd profile.

rig case (marker rig, profile allstar for the USRP endpoint): the tester
is the USRP peer of module C; transcoder-sut is stopped for longer than the
keepalive timeout through the control channel and started again. With
--capture, xlxd-sut.pcapng holds the CAPS requests of both connections.
"""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

XLXD_IP, AMBED_IP, DEXTRA_IP, USRP_IP = "127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4"
USRP_LINE = "B %s 34001 %s 32001 N0CALL" % (XLXD_IP, USRP_IP)
VOICE_FRAME = bytes(range(9))
USRP_PCM = (b"\xe8\x03\x18\xfc") * 80          # +1000, -1000 S16LE
LOST_SECONDS = 40                               # > 30 s keepalive timeout + 5 s ping period
SWAP_SECONDS = 2                                # well inside the keepalive window

CAPS_TOLERANCE = 0.3                            # s, CAPS after a PONG / after the reply wait
LISTENER = "N0LSTN"
LISTEN_SETTLE = 1.0                             # s after the link ack (DV-Router demand token)

# steps: talk on module B ("usrpB", "dstarB"), "listenB" links a D-STAR
# listener on B for the rest of the run, "down:<s>" stops the fake,
# "up:<mode>" starts a new one and waits for its first PING,
# "swap:<mode>:<s>" stops the fake and starts another after s seconds,
# "wait:<s>" idles. "cases": name prefix of the cases that read the
# scenario; only scenarios of selected cases are launched.
SCENARIOS = {
    "lost_and_back": dict(mode="extended", cases="test_lost_transcoder_",
                          steps=["usrpB", "down:%d" % LOST_SECONDS, "up:extended", "usrpB", "dstarB"]),
    "fast_swap": dict(mode="extended", cases="test_fast_swap_",
                      steps=["usrpB", "swap:legacy:%d" % SWAP_SECONDS, "usrpB", "dstarB"]),
    "listener_lost_and_back": dict(mode="extended", cases="test_lost_listener_",
                                   steps=["listenB", "usrpB", "down:%d" % LOST_SECONDS,
                                          "up:extended", "usrpB"]),
    "legacy_swap": dict(mode="extended", cases="test_legacy_swap_",
                        steps=["down:%d" % LOST_SECONDS, "up:legacy", "dstarB", "wait:3",
                               "swap:extended:%d" % SWAP_SECONDS, "dstarB"]),
}
TIMEOUT = 150


# ---------------------------------------------------------------- inside

def _inside(spec, run_dir):
    """Runs in the namespace: xlxd + fakes, writes run_dir/result.json."""
    import ctypes
    import signal
    import threading

    from dvtest.net import pace
    from dvtest.proto import ambed, dextra, usrp

    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    run_dir = Path(run_dir)
    (run_dir / "xlxd.usrp").write_text(USRP_LINE + "\n")
    libc = ctypes.CDLL(None, use_errno=True)
    t0 = time.monotonic()

    def now():
        return time.monotonic() - t0

    def die_with_parent():
        libc.prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG

    fakes = []                              # [(FakeAmbed, mode, started, stopped)]

    def start_fake(mode):
        # stream ports 41000, 41100, ...: xlxd binds the same port on its
        # own address, and 42000 is its YSF port
        fake = ambed.FakeAmbed(AMBED_IP, mode=mode, data_port_base=41000 + 100 * len(fakes))
        fakes.append([fake, mode, now(), None])
        return fake

    def stop_fake():
        entry = fakes[-1]
        entry[0].stop()
        entry[3] = now()

    fake = start_fake(spec["mode"])
    log = open(run_dir / "xlxd.log", "wb")
    xlxd = subprocess.Popen([spec["bin"], "XLX999", XLXD_IP, AMBED_IP], cwd=run_dir,
                            stdout=log, stderr=subprocess.STDOUT, preexec_fn=die_with_parent)
    result = {"phases": [], "usrp_rx": [], "dextra_ack": [], "up_pinged": [],
              "listen_ack": [], "dstar_rx": []}
    lock = threading.Lock()
    stop = threading.Event()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind((USRP_IP, 32001))
    peer.settimeout(0.2)

    def usrp_rx():
        while not stop.is_set():
            try:
                data = peer.recv(2048)
            except socket.timeout:
                continue
            f = usrp.parse(data)
            with lock:
                result["usrp_rx"].append([now(), len(data), f.seq if f else -1,
                                          f.keyup if f else -1,
                                          bool(f) and f.payload == ambed.fake_payload(ambed.PCM)])

    def dstar(module, stream_id):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((DEXTRA_IP, 0))
        s.settimeout(3)
        dest = (XLXD_IP, dextra.PORT)
        s.sendto(dextra.link_packet("N0TEST", module, own_module="C"), dest)
        try:
            result["dextra_ack"].append(s.recv(100).hex())
        except socket.timeout:
            result["dextra_ack"].append(None)
            return
        time.sleep(0.5)
        s.sendto(dextra.header(stream_id, "N0TEST", module), dest)
        start = time.monotonic()
        for index in range(50):
            pace(start, index)
            s.sendto(dextra.voice(stream_id, index, VOICE_FRAME, index == 49,
                                  slow=dextra.SYNC_DATA), dest)
        s.close()

    def listen(module):
        """D-STAR listener linked on module for the rest of the run; voice
        frames go to result["dstar_rx"] as [t, sid, seq, ambe == fake AMBE+]."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((DEXTRA_IP, 0))
        s.settimeout(0.2)
        dest = (XLXD_IP, dextra.PORT)
        acked = threading.Event()

        def loop():
            last = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last >= 3:        # DExtra keepalive period (xlxd 3 s)
                    s.sendto(dextra.keepalive_packet(LISTENER), dest)
                    last = time.monotonic()
                try:
                    data = s.recv(2048)
                except socket.timeout:
                    continue
                voice = dextra.parse_voice(data)
                reply = dextra.parse_link_reply(data)
                with lock:
                    if voice:
                        result["dstar_rx"].append([now(), voice[0], voice[1],
                                                   voice[3] == ambed.fake_payload(ambed.AMBEPLUS)])
                    elif reply and reply["kind"] in ("ack", "nak") and not acked.is_set():
                        result["listen_ack"].append(data.hex())
                        acked.set()
            s.close()

        threading.Thread(target=loop, daemon=True).start()
        s.sendto(dextra.link_packet(LISTENER, module, own_module="D"), dest)
        if not acked.wait(3):
            result["listen_ack"].append(None)
        time.sleep(LISTEN_SETTLE)

    def usrp_talk(count):
        start = time.monotonic()
        for seq in range(count):
            pace(start, seq)
            peer.sendto(usrp.voice(seq, USRP_PCM), (XLXD_IP, 34001))
        peer.sendto(usrp.unkey(count), (XLXD_IP, 34001))

    threading.Thread(target=usrp_rx, daemon=True).start()
    try:
        result["pinged"] = fake.connected.wait(15)
        time.sleep(1.5)                     # CAPS probe window
        for index, step in enumerate(spec["steps"]):
            start = now()
            kind, _, arg = step.partition(":")
            if kind == "dstarB":
                dstar("B", 0x2345 + index)
                time.sleep(2.5)
            elif kind == "usrpB":
                usrp_talk(50)
                time.sleep(2.5)
            elif kind == "listenB":
                listen("B")
            elif kind == "wait":
                time.sleep(float(arg))
            elif kind == "down":
                stop_fake()
                time.sleep(float(arg))
            elif kind == "up":
                fake = start_fake(arg)
                result["up_pinged"].append(fake.connected.wait(15))
                time.sleep(1.5)             # CAPS probe window of the new connection
            elif kind == "swap":
                mode, _, gap = arg.partition(":")
                stop_fake()
                time.sleep(float(gap))
                fake = start_fake(mode)
                result["up_pinged"].append(fake.connected.wait(10))
                time.sleep(1.5)
            result["phases"].append([step, start, now()])
        result["xlxd_alive"] = xlxd.poll() is None
    finally:
        xlxd.terminate()
        try:
            xlxd.wait(5)
        except subprocess.TimeoutExpired:
            xlxd.kill()
        stop.set()
        if fakes and fakes[-1][3] is None:
            stop_fake()
        log.close()
    result["fakes"] = []
    for fake, mode, started, stopped in fakes:
        shift = fake.t0 - t0                # fake clock -> scenario clock
        with fake.lock:
            result["fakes"].append({
                "mode": mode, "started": started, "stopped": stopped,
                "ctrl": [[t + shift, b.hex()] for t, b in fake.ctrl],
                "opens": [[t + shift, o.size, o.codec_in, list(o.outs), o.transaction, port]
                          for t, o, port in fake.opens],
                "closes": [[t + shift, i] for t, i in fake.closes],
                "data_in": {str(p): [[t + shift, b.hex()] for t, b in v] for p, v in fake.data_in.items()},
                "events": [[t + shift, e] for t, e in fake.events],
            })
    (run_dir / "result.json").write_text(json.dumps(result, indent=1))


# ---------------------------------------------------------------- outside

def _launch(binary, name, spec, out_root):
    run_dir = out_root / "netns-lifecycle" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = dict(spec, bin=str(Path(binary).resolve()))
    cmd = ["unshare", "-rn", sys.executable, "-I", str(Path(__file__).resolve()),
           "--inside", json.dumps(spec), str(run_dir)]
    stderr = open(run_dir / "harness.err", "wb")
    return run_dir, subprocess.Popen(cmd, stdout=stderr, stderr=subprocess.STDOUT), stderr


@pytest.fixture(scope="module")
def runs(request, xlxd_bin, out_root):
    selected = {getattr(item, "originalname", item.name) for item in request.session.items}
    launched = {name: _launch(xlxd_bin, name, spec, out_root) for name, spec in SCENARIOS.items()
                if any(case.startswith(spec["cases"]) for case in selected)}
    results = {}
    deadline = time.monotonic() + TIMEOUT
    for name, (run_dir, proc, stderr) in launched.items():
        try:
            proc.wait(max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        stderr.close()
        path = run_dir / "result.json"
        results[name] = json.loads(path.read_text()) if path.exists() else \
            {"error": (run_dir / "harness.err").read_text()[-2000:]}
        results[name]["run_dir"] = str(run_dir)
    return results


def get(runs, name):
    assert name in runs, "scenario %s was not launched" % name
    run = runs[name]
    assert "error" not in run, run.get("error")
    assert run["pinged"], "xlxd never sent AMBEDPING to the transcoder"
    assert run["xlxd_alive"], "xlxd exited"
    return run


def caps_times(fake):
    return [t for t, h in fake["ctrl"] if bytes.fromhex(h) == b"AMBEDCAPS"]


def ping_times(fake):
    return [t for t, h in fake["ctrl"] if bytes.fromhex(h).startswith(b"AMBEDPING")]


def ignored_opens(fake):
    return [t for t, e in fake["events"] if e == "ignored extended open"]


def requests(fake, port):
    return [bytes.fromhex(h) for _t, h in fake["data_in"].get(str(port), [])]


def phase(run, step_index):
    return run["phases"][step_index][1], run["phases"][step_index][2]


def opens_in(fake, start, end):
    return [o for o in fake["opens"] if start <= o[0] < end]


def check_pcm_requests(fake, port):
    reqs = requests(fake, port)
    assert len(reqs) >= 50, "PCM data requests %d" % len(reqs)
    assert all(len(r) == 322 and r[0] == 5 for r in reqs), sorted({len(r) for r in reqs})
    assert reqs[0][2:] == USRP_PCM, "PCM must reach the transcoder unchanged"


def check_dstar_requests(fake, port):
    reqs = requests(fake, port)
    assert len(reqs) >= 45, "only %d data requests for 50 frames" % len(reqs)
    assert all(len(r) == 11 and r[0] == 1 and r[2:] == VOICE_FRAME for r in reqs), reqs[:1]


def keyed_usrp(run, start, end):
    return [r for r in run["usrp_rx"] if start <= r[0] < end and r[1] == 352]


def lifecycle_spec(target):
    """None under the xlxd profile (xlxd assertions); else the profile's
    "netns_lifecycle" dict, or skip "spec pending" without one."""
    from dvtest import specs
    if target.sut_profile == specs.DEFAULT:
        return None
    spec = specs.rule(target, "netns_lifecycle")
    if spec is None:
        pytest.skip("spec pending: no netns_lifecycle entry in dvtest/specs/%s.py" % target.sut_profile)
    return spec


def spec_key(spec, key):
    """One value of the profile spec; skip "spec pending" while it is
    missing or None (an open question for the sut)."""
    if spec.get(key) is None:
        pytest.skip("spec pending: netns_lifecycle[%r] not decided for this profile" % key)
    return spec[key]


# B13: lost > 30 s and back

@pytest.mark.netns
def test_lost_transcoder_caps_probed_again(runs, target):
    """xlxd: one CAPS per connection. Profile spec: caps_per_connection."""
    spec = lifecycle_spec(target)
    run = get(runs, "lost_and_back")
    first, second = run["fakes"]
    assert run["up_pinged"] == [True], "xlxd did not ping the restarted transcoder"
    want = [1, 1] if spec is None else spec_key(spec, "caps_per_connection")
    counts = [len(caps_times(first)), len(caps_times(second))]
    assert counts == want, "CAPS per connection %s, expected %s" % (counts, want)
    start, end = phase(run, 2)
    assert start <= caps_times(second)[0] < end, "CAPS not probed right after the reconnect"


@pytest.mark.netns
def test_lost_transcoder_pcm_resumes(runs, target):
    """xlxd: the PCM stream opens [1, 2] before the loss and after the
    reconnect. Profile spec: pcm_opens (no D-STAR listener is linked
    during the USRP steps; an empty list means no OPEN at all)."""
    spec = lifecycle_spec(target)
    run = get(runs, "lost_and_back")
    first, second = run["fakes"]
    want = [[24, 5, [1, 2]]] if spec is None else spec_key(spec, "pcm_opens")
    before = opens_in(first, *phase(run, 0))
    assert [o[1:4] for o in before] == want, before
    after = opens_in(second, *phase(run, 3))
    assert [o[1:4] for o in after] == want, "PCM open after the reconnect: %s" % after
    for fake, opens in ((first, before), (second, after)):
        for o in opens:
            check_pcm_requests(fake, o[5])
    if want:
        txs = [o[4] for o in before + after]
        assert all(txs) and len(set(txs)) == len(txs), "transactions %s" % txs


@pytest.mark.netns
def test_lost_transcoder_dstar_to_usrp_resumes(runs, target):
    """xlxd: D-STAR opens [2, 5]. Profile spec: dstar_opens (the USRP peer
    is the only listener in another profile)."""
    spec = lifecycle_spec(target)
    run = get(runs, "lost_and_back")
    second = run["fakes"][1]
    start, end = phase(run, 4)
    opens = opens_in(second, start, end)
    want = [[24, 1, [2, 5]]] if spec is None else spec_key(spec, "dstar_opens")
    assert want, "dstar_opens must name the open that feeds the USRP peer"
    assert [o[1:4] for o in opens] == want, opens
    check_dstar_requests(second, opens[0][5])
    keyed = keyed_usrp(run, start, end)
    assert len(keyed) >= 45, "USRP voice frames %d after the reconnect" % len(keyed)
    assert all(r[3] == 1 and r[4] for r in keyed), "keyed frames must carry the transcoder PCM"


# B13 with a D-STAR listener linked on the USRP module

def listener_run(runs):
    run = get(runs, "listener_lost_and_back")
    assert run["listen_ack"] and run["listen_ack"][0], "D-STAR listener link not acked"
    assert run["up_pinged"] == [True], "xlxd did not ping the restarted transcoder"
    return run


@pytest.mark.netns
def test_lost_listener_pcm_opens(runs, target):
    """xlxd: the PCM stream opens [1, 2] before the loss and after the
    reconnect. Profile spec: pcm_listener_opens (a D-STAR listener is
    linked during both USRP steps)."""
    spec = lifecycle_spec(target)
    run = listener_run(runs)
    first, second = run["fakes"]
    want = [[24, 5, [1, 2]]] if spec is None else spec_key(spec, "pcm_listener_opens")
    assert want, "pcm_listener_opens must name the open that feeds the D-STAR listener"
    before = opens_in(first, *phase(run, 1))
    assert [o[1:4] for o in before] == want, before
    after = opens_in(second, *phase(run, 4))
    assert [o[1:4] for o in after] == want, "PCM open after the reconnect: %s" % after
    for fake, opens in ((first, before), (second, after)):
        for o in opens:
            check_pcm_requests(fake, o[5])
    txs = [o[4] for o in before + after]
    assert all(txs) and len(set(txs)) == len(txs), "transactions %s" % txs


@pytest.mark.netns
def test_lost_listener_heard(runs, target):
    """xlxd: the D-STAR listener hears the transcoded USRP stream (the
    fake's AMBE+ payload) before the loss and after the reconnect. Profile
    spec: pcm_listener_heard (True / False)."""
    spec = lifecycle_spec(target)
    run = listener_run(runs)
    want = True if spec is None else spec_key(spec, "pcm_listener_heard")
    for index, label in ((1, "before the loss"), (4, "after the reconnect")):
        start, end = phase(run, index)
        frames = [r for r in run["dstar_rx"] if start <= r[0] < end]
        voiced = [r for r in frames if r[3]]
        if want:
            assert len(voiced) >= 45, "D-STAR listener %s: %d of %d frames carry the transcoder AMBE+" % (
                label, len(voiced), len(frames))
        else:
            assert not voiced, "D-STAR listener %s heard %d transcoded frames" % (label, len(voiced))


# B14: replaced within the keepalive window by a legacy transcoder

@pytest.mark.netns
def test_fast_swap_no_disconnect(runs, target):
    """The swap is inside the keepalive window: the new transcoder is
    pinged within seconds, the old one served the PCM stream before
    (profile spec: pcm_opens, as in lost_and_back)."""
    spec = lifecycle_spec(target)
    run = get(runs, "fast_swap")
    first, second = run["fakes"]
    assert (first["mode"], second["mode"]) == ("extended", "legacy")
    assert run["up_pinged"] == [True], "xlxd did not ping the new transcoder within 10 s"
    want = [[24, 5, [1, 2]]] if spec is None else spec_key(spec, "pcm_opens")
    before = opens_in(first, *phase(run, 0))
    assert [o[1:4] for o in before] == want, before
    for o in before:
        check_pcm_requests(first, o[5])


@pytest.mark.netns
def test_fast_swap_caps_probed_after_failed_open(runs, target, request):
    """xlxd (xfail strict, PR #272 edge case): CAPS probed again after the
    failed open, then a legacy open for D-STAR. Profile spec: see
    fast_swap_profile()."""
    spec = lifecycle_spec(target)
    if spec is not None:
        fast_swap_profile(get(runs, "fast_swap"), spec)
        return
    request.applymarker(pytest.mark.xfail(
        strict=True, reason="xlxd PR #272 edge case: stale CAPS after fast transcoder swap"))
    run = get(runs, "fast_swap")
    second = run["fakes"][1]
    swapped = run["phases"][1][2]
    failed = [t for t, e in second["events"] if e == "ignored extended open"]
    assert failed, "no extended open reached the new transcoder: %s" % second["opens"]
    probes = [t for t in caps_times(second) if t >= failed[0]]
    assert probes, "no CAPS probe after the open to the new transcoder timed out (swap at %.1f s)" % swapped
    start, end = phase(run, 3)
    digital = [o for o in opens_in(second, start, end) if o[2] == 1]
    assert [o[1:4] for o in digital] == [[19, 1, [2, 3, 4]]], \
        "D-STAR after the swap must use a legacy open: %s" % digital
    check_dstar_requests(second, digital[0][5])


def fast_swap_profile(run, spec):
    """Profile spec for the D-STAR step after the swap (the old CAPS is
    kept): swap_ignored_opens = extended OPENs the legacy transcoder
    ignored; swap_dstar_opens = opens it accepted (with none, the USRP peer
    gets no voice); swap_caps_reprobe = CAPS probed again after the ignored
    open: False = never, {"within_s": s} = a CAPS request reaches the new
    transcoder within s + CAPS_TOLERANCE (0.3 s) after the ignored open."""
    second = run["fakes"][1]
    start, end = phase(run, 3)
    ignored = ignored_opens(second)
    want_ignored = spec_key(spec, "swap_ignored_opens")
    assert len([t for t in ignored if start <= t < end]) == want_ignored == len(ignored), \
        "ignored extended opens %s (D-STAR step %.1f-%.1f s), expected %d" % (ignored, start, end, want_ignored)
    want_opens = spec_key(spec, "swap_dstar_opens")
    accepted = opens_in(second, start, end)
    assert [o[1:4] for o in accepted] == want_opens, accepted
    for o in accepted:
        check_dstar_requests(second, o[5])
    keyed = keyed_usrp(run, start, end)
    if not want_opens:
        assert not keyed, "%d USRP voice frames without an open transcoder stream" % len(keyed)
    reprobe = spec_key(spec, "swap_caps_reprobe")
    probes = [t for t in caps_times(second) if ignored and t >= ignored[0]]
    if isinstance(reprobe, dict):
        assert ignored, "no ignored extended open to time the re-probe from"
        limit = reprobe["within_s"] + CAPS_TOLERANCE
        assert probes and probes[0] - ignored[0] <= limit, \
            "first CAPS after the ignored open at %.2f s: %s, expected within %.2f s" % (
                ignored[0], ["%.2f" % t for t in probes[:3]], limit)
        return
    assert bool(probes) == reprobe, "CAPS after the ignored open at %s: %s, expected re-probe %s" % (
        ignored[:1], probes, reprobe)


# Legacy swap: extended lost (> 30 s), legacy answers, extended back

def legacy_swap_observed(run):
    """Summary of the legacy_swap scenario: OPENs the legacy transcoder got
    (accepted legacy forms + ignored extended), PINGs and CAPS on its
    connection, and the opens / USRP voice of the D-STAR step after the
    extended transcoder is back."""
    _first, legacy, back = run["fakes"]
    pings, caps = ping_times(legacy), caps_times(legacy)
    judged = [p for p in pings if p + CAPS_TOLERANCE < legacy["stopped"]]
    start, end = phase(run, 5)
    recovered = opens_in(back, start, end)
    keyed = keyed_usrp(run, start, end)
    return {
        "legacy_opens": [o[1:4] for o in legacy["opens"]],
        "legacy_ignored": len(ignored_opens(legacy)),
        "legacy_pings": len(pings),
        "legacy_caps": len(caps),
        "caps_after_each_pong": bool(judged) and all(
            any(p <= c <= p + CAPS_TOLERANCE for c in caps) for p in judged),
        "legacy_keyed_usrp": len(keyed_usrp(run, *phase(run, 2))),
        "back_caps": len(caps_times(back)),
        "recovered_opens": [o[1:4] for o in recovered],
        "recovered_keyed_usrp": len(keyed),
        "recovered_keyed_pcm": sum(1 for r in keyed if r[3] == 1 and r[4]),
    }


@pytest.mark.slow
@pytest.mark.netns
def test_legacy_swap_recovers(runs, target):
    """xlxd: recorded only (netns-lifecycle/legacy_swap/observed.json).
    Profile spec legacy_swap: opens_while_legacy = OPENs (any form) the
    legacy transcoder gets; caps_after_each_pong = a CAPS request follows
    every PING/PONG of the legacy connection within CAPS_TOLERANCE;
    recovers = extended OPENs and transcoded voice for D-STAR -> USRP once
    the extended transcoder is back, without a restart."""
    spec = lifecycle_spec(target)
    run = get(runs, "legacy_swap")
    assert [f["mode"] for f in run["fakes"]] == ["extended", "legacy", "extended"]
    assert run["up_pinged"] == [True, True], "new transcoder not pinged: %s" % run["up_pinged"]
    observed = legacy_swap_observed(run)
    (Path(run["run_dir"]) / "observed.json").write_text(json.dumps(observed, indent=1))
    print("legacy_swap observed:", json.dumps(observed))
    if spec is None:
        return
    want = spec_key(spec, "legacy_swap")
    opens = len(observed["legacy_opens"]) + observed["legacy_ignored"]
    assert opens == want["opens_while_legacy"], "OPENs to the legacy transcoder: %s" % observed
    if not want["opens_while_legacy"]:
        assert not observed["legacy_keyed_usrp"], "USRP voice without a transcoder stream: %s" % observed
    if want["caps_after_each_pong"]:
        assert observed["legacy_pings"] >= 2 and observed["caps_after_each_pong"], \
            "CAPS must follow every PONG of the legacy connection: %s" % observed
    else:
        assert observed["legacy_caps"] <= 1, "CAPS re-probed on the legacy connection: %s" % observed
    if want["recovers"]:
        start, end = phase(run, 5)
        recovered = opens_in(run["fakes"][2], start, end)
        assert recovered and all(o[1] == 24 for o in recovered), \
            "no extended OPEN after the extended transcoder is back: %s" % observed
        check_dstar_requests(run["fakes"][2], recovered[0][5])
        assert observed["recovered_keyed_pcm"] >= 45, "USRP voice after recovery: %s" % observed
    else:
        assert not observed["recovered_opens"], observed


# ---------------------------------------------------------------- rig

USRP_MODULE = "C"       # tester is the USRP peer (rig/config/xlxd.usrp.in)
USRP_PEER_PORT, USRP_XLXD_PORT = 32003, 34003
RIG_LOST_SECONDS = 40


def usrp_to_dstar(rig, seconds=1.0):
    """USRP tone from the tester into module C; returns (voiced, total)
    D-STAR frames a listener on C received."""
    from dvtest import clients
    from dvtest.audio import signals
    from dvtest.net import pace
    from dvtest.proto import usrp

    listener = clients.make("dstar", USRP_MODULE, 1)
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", USRP_PEER_PORT))
    try:
        assert listener.link(rig.xlxd_sut), "D-STAR link to %s failed" % rig.xlxd_sut
        listener.client.drain()
        listener.client.inbox.clear()
        dest = (rig.xlxd_sut, USRP_XLXD_PORT)
        start = time.monotonic()
        seq = 0
        for seq, frame in enumerate(signals.frames(signals.tone(700, int(seconds * 1000)))):
            pace(start, seq)
            peer.sendto(usrp.voice(seq, frame), dest)
            listener.client.drain()
        peer.sendto(usrp.unkey(seq + 1), dest)
        received = listener.collect(settle=1.0)
    finally:
        peer.close()
        clients.close(listener)
    return sum(1 for f in received if any(f)), len(received)


@pytest.mark.rig
def test_rig_transcoder_lost_and_back_usrp_resumes(rig_usrp, rig_control):
    """B13 on the rig: transcoder-sut down > 30 s, then up; USRP PCM is
    transcoded to D-STAR again. CAPS count: see xlxd-sut.pcapng."""
    voiced, total = usrp_to_dstar(rig_usrp)
    assert voiced >= 40, "USRP->D-STAR before the fault: %d of %d frames voiced" % (voiced, total)
    rig_control("stop", "transcoder-sut")
    try:
        time.sleep(RIG_LOST_SECONDS)
        down_voiced, down_total = usrp_to_dstar(rig_usrp)
    finally:
        rig_control("start", "transcoder-sut")
    assert not down_voiced, "%d of %d frames voiced with the transcoder down" % (down_voiced, down_total)
    restarted = time.monotonic()
    deadline = restarted + 60
    while True:
        voiced, total = usrp_to_dstar(rig_usrp)
        if voiced >= 40:
            break
        if time.monotonic() > deadline:
            pytest.fail("USRP->D-STAR did not recover within 60 s of the restart (%d of %d voiced)"
                        % (voiced, total))
        time.sleep(3)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--inside":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _inside(json.loads(sys.argv[2]), sys.argv[3])
