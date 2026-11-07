# SPDX-License-Identifier: MIT
"""xlxd transcoder link + USRP endpoints, black box, in private network
namespaces (unshare -rn, no root, no docker).

Each scenario runs this file inside its own namespace: xlxd XLX999 on
127.0.0.1 with the transcoder at 127.0.0.2, a FakeAmbed there, a DExtra
talker on 127.0.0.3 and a USRP peer on 127.0.0.4. The recorded traffic is
written as JSON and asserted here. Only the scenarios read by the selected
cases are launched ("cases" prefix), DVT_NETNS_PARALLEL of them at a time
(default 1: one DV-Router scenario peaks near 435 tasks, the scope allows
1024; dvtest/netns_runs.py).

Needs DVT_XLXD_BIN: an xlxd built in the foreground with config paths
relative to its working directory (scripts/build-xlxd-netns.sh). Optional
DVT_XLXD_REF_BIN (e.g. upstream master, same patch) adds a byte-equality
check of the transcoder link without xlxd.usrp.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.netns

XLXD_IP, AMBED_IP, DEXTRA_IP, USRP_IP = "127.0.0.1", "127.0.0.2", "127.0.0.3", "127.0.0.4"
USRP_LINE = "B %s 34001 %s 32001 N0CALL" % (XLXD_IP, USRP_IP)
VOICE_FRAME = bytes(range(9))
USRP_PCM = (b"\xe8\x03\x18\xfc") * 80          # +1000, -1000 S16LE
LISTENER = "N0LSTN"
LISTEN_SETTLE = 1.0                             # s after the link ack (DV-Router demand token)
SHORT_BURST = 5                                 # USRP frames of usrpShortB
HUB_METRICS = "http://127.0.0.1:19108/metrics"  # dv-svc-status hub of the converted config (in the namespace)
METRIC_PREFIXES = ("dv_xcode_", "dv_room_data_total")
METRICS_WAIT = 15.0                             # s to wait for the controllers' 10 s telemetry

# name: usrp config present, fake ambed mode / max outputs / STD delay, steps,
# "cases": name prefix of the cases that read the scenario (only scenarios of
# selected cases are launched; the prefixes must not be prefixes of each other:
# "test_slow_open_" and "test_open_wait_"; also test_pre_ready_cap_, test_open_refused_), optional "profile": the scenario is
# launched only under that DVT_SUT_PROFILE (H4)
SCENARIOS = {
    "no_usrp": dict(usrp=False, mode="extended", max_outputs=4, open_delay=0.05,
                    steps=["dstarA"], cases="test_no_usrp_"),
    "legacy_ambed": dict(usrp=True, mode="legacy", max_outputs=4, open_delay=0.05,
                         steps=["dstarB", "usrpB"], cases="test_legacy_ambed_"),
    "extended": dict(usrp=True, mode="extended", max_outputs=4, open_delay=0.05,
                     steps=["dstarB", "usrpB", "badB"], cases="test_extended_"),
    "one_output": dict(usrp=True, mode="extended", max_outputs=1, open_delay=0.05,
                       steps=["dstarB", "dstarB"], cases="test_one_output_"),
    "slow_open": dict(usrp=True, mode="extended", max_outputs=4, open_delay=1.0,
                      steps=["usrpShortB"], cases="test_slow_open_"),
    # H4: the same slow STD with a D-STAR listener on the USRP module, so a
    # DV-Router really opens (demand = AMBE+) and the 5-frame burst runs over
    # the slow OPEN; the xlxd profile has the case above for B12
    "slow_open_listener": dict(usrp=True, mode="extended", max_outputs=4, open_delay=1.0,
                               steps=["listenB", "usrpShortB"], cases="test_open_wait_",
                               profile="dvrouter"),
    # H5: a USRP over of 200 units sent 2 ms apart (0.4 s, faster than real
    # time) while the STD is 1.0 s late: more than the 128 units the
    # controller buffers before SessionReady are in hand, the oldest go.
    # "metrics_wait": after that step the hub counters are read in the namespace,
    # once dv_xcode_sessions_total{result=<label>} has reached the minimum
    "pre_ready_overflow": dict(usrp=True, mode="extended", max_outputs=4, open_delay=1.0,
                               steps=["listenB", "usrp:200:2"], cases="test_pre_ready_cap_",
                               profile="dvrouter", metrics_wait={"usrp:200:2": ["opened", 1]}),
    # H6: the first STD comes 1.5 s after its OPEN (later than the 1050 ms
    # OPEN wait), the second one at once; a 100-unit over (2 s) first, a short
    # one after it
    "open_late_std": dict(usrp=True, mode="extended", max_outputs=4, open_delay=0.05,
                          open_delays=[1.5, 0.05], steps=["listenB", "usrp:100:20", "usrpShortB"],
                          cases="test_open_refused_", profile="dvrouter",
                          metrics_wait={"usrp:100:20": ["open_failed", 1], "usrpShortB": ["opened", 2]}),
}
TIMEOUT = 120


# ---------------------------------------------------------------- inside

def indexed_pcm(index):
    """USRP_PCM with the frame index in the first sample (S16LE): the PCM
    request at the fake transcoder names the unit it carries."""
    return index.to_bytes(2, "little") + USRP_PCM[2:]


def pcm_index(request):
    """Index of an indexed_pcm() unit in a PCM data request (codec, pid,
    320 B of PCM)."""
    return int.from_bytes(request[2:4], "little")


def _inside(spec, run_dir):
    """Runs in the namespace: xlxd + fakes, writes run_dir/result.json."""
    import ctypes
    import signal
    import socket
    import threading
    import urllib.request

    from dvtest.net import pace
    from dvtest.proto import ambed, dextra, usrp

    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    run_dir = Path(run_dir)
    if spec["usrp"]:
        (run_dir / "xlxd.usrp").write_text(USRP_LINE + "\n")
    libc = ctypes.CDLL(None, use_errno=True)

    def die_with_parent():
        libc.prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG

    fake = ambed.FakeAmbed(AMBED_IP, mode=spec["mode"], max_outputs=spec["max_outputs"],
                           open_delay=spec["open_delay"], open_delays=spec.get("open_delays"))
    log = open(run_dir / "xlxd.log", "wb")
    # DV_RUN_DIR: a DV-Router keeps its run state here (xlxd ignores it)
    xlxd = subprocess.Popen([spec["bin"], "XLX999", XLXD_IP, AMBED_IP], cwd=run_dir,
                            env=dict(os.environ, DV_RUN_DIR=str(run_dir)),
                            stdout=log, stderr=subprocess.STDOUT, preexec_fn=die_with_parent)
    result = {"phases": [], "usrp_rx": [], "dextra_ack": [], "listen_ack": [], "dstar_rx": [],
              "dstar_hdr": [], "usrp_tx": [], "metrics": []}
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
                result["usrp_rx"].append([fake.now(), len(data), f.seq if f else -1,
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
        """D-STAR listener linked on module for the rest of the run: header
        times go to result["dstar_hdr"] as [t, sid], voice frames to
        result["dstar_rx"] as [t, sid, seq, last, ambe == fake AMBE+] (t on
        the fake's clock, like usrp_rx and opens)."""
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
                header = dextra.parse_header(data)
                reply = dextra.parse_link_reply(data)
                with lock:
                    if voice:
                        result["dstar_rx"].append([fake.now(), voice[0], voice[1], voice[2],
                                                   voice[3] == ambed.fake_payload(ambed.AMBEPLUS)])
                    elif header:
                        result["dstar_hdr"].append([fake.now(), header["stream_id"]])
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
        first = None
        for seq in range(count):
            pace(start, seq)
            if first is None:
                first = fake.now()
            peer.sendto(usrp.voice(seq, USRP_PCM), (XLXD_IP, 34001))
        last = fake.now()
        peer.sendto(usrp.unkey(count), (XLXD_IP, 34001))
        # [first voice, last voice, unkey] sent, fake clock
        result["usrp_tx"].append([first, last, fake.now()])

    def usrp_over(count, gap):
        """count USRP voice frames gap seconds apart, frame i carrying its
        index in the PCM (indexed_pcm), then the unkey."""
        start = time.monotonic()
        first = None
        for seq in range(count):
            pace(start, seq, gap)
            if first is None:
                first = fake.now()
            peer.sendto(usrp.voice(seq, indexed_pcm(seq)), (XLXD_IP, 34001))
        last = fake.now()
        peer.sendto(usrp.unkey(count), (XLXD_IP, 34001))
        result["usrp_tx"].append([first, last, fake.now()])

    def scrape(step, result_label, minimum):
        """The xcode / room data counters of the hub as [step, t, lines]. The
        controllers put their counters on the fabric every 10 s (status.md),
        so poll until dv_xcode_sessions_total{result=result_label} reached
        minimum, at most METRICS_WAIT s; the last text is kept either way."""
        from dvtest import metrics
        lines = []
        deadline = time.monotonic() + METRICS_WAIT
        while True:
            try:
                with urllib.request.urlopen(HUB_METRICS, timeout=2) as reply:
                    text = reply.read().decode("utf-8", "replace")
                lines = [ln for ln in text.splitlines() if ln.startswith(METRIC_PREFIXES)]
                if metrics.total(metrics.parse("\n".join(lines)), "dv_xcode_sessions_total",
                                 result=result_label) >= minimum:
                    break
            except Exception as exc:            # a missing hub is a finding, not a crash
                lines = ["error: %s" % exc]
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        result["metrics"].append([step, fake.now(), lines])

    threading.Thread(target=usrp_rx, daemon=True).start()
    try:
        result["pinged"] = fake.connected.wait(15)
        time.sleep(1.5)                     # CAPS probe window
        for index, step in enumerate(spec["steps"]):
            start = fake.now()
            if step.startswith("dstar"):
                dstar(step[-1], 0x1234 + index)
            elif step == "usrpB":
                usrp_talk(50)
            elif step == "listenB":
                listen("B")
            elif step == "usrpShortB":
                usrp_talk(SHORT_BURST)
            elif step.startswith("usrp:"):
                _name, count, gap_ms = step.split(":")
                usrp_over(int(count), int(gap_ms) / 1000.0)
            elif step == "badB":
                # TLV/text frame from the configured peer: must be dropped
                peer.sendto(usrp.frame(0, usrp.TYPE_TEXT, b"\x08\x00"), (XLXD_IP, 34001))
            time.sleep(2.5)
            if step in spec.get("metrics_wait", {}):
                scrape(step, *spec["metrics_wait"][step])
            result["phases"].append([step, start, fake.now()])
        result["xlxd_alive"] = xlxd.poll() is None
    finally:
        xlxd.terminate()
        try:
            xlxd.wait(5)
        except subprocess.TimeoutExpired:
            xlxd.kill()
        stop.set()
        fake.stop()
        log.close()
    with fake.lock:
        result["ctrl"] = [[t, b.hex()] for t, b in fake.ctrl]
        result["opens"] = [[t, o.size, o.codec_in, list(o.outs), o.transaction, o.callsign.hex(), port]
                           for t, o, port in fake.opens]
        result["closes"] = [[t, i] for t, i in fake.closes]
        result["data_in"] = {str(p): [[t, b.hex()] for t, b in v] for p, v in fake.data_in.items()}
        result["events"] = fake.events
    (run_dir / "result.json").write_text(json.dumps(result, indent=1))


# ---------------------------------------------------------------- outside

def _launch(binary, name, spec, out_root):
    run_dir = out_root / "netns" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = dict(spec, bin=str(Path(binary).resolve()))
    cmd = ["unshare", "-rn", sys.executable, "-I", str(Path(__file__).resolve()),
           "--inside", json.dumps(spec), str(run_dir)]
    stderr = open(run_dir / "harness.err", "wb")
    return run_dir, subprocess.Popen(cmd, stdout=stderr, stderr=subprocess.STDOUT), stderr


def _collect(launched):
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


@pytest.fixture(scope="module")
def runs(request, xlxd_bin, out_root, target):
    """Result of every scenario a selected case reads (and that applies to
    the profile under test), started in waves of DVT_NETNS_PARALLEL
    (default 1)."""
    from dvtest import netns_runs
    selected = netns_runs.selected_names(request.session.items, request.module.__name__)
    return netns_runs.run_in_waves(
        netns_runs.for_profile(netns_runs.scenarios_for(SCENARIOS, selected), SCENARIOS,
                               target.sut_profile),
        lambda name: _launch(xlxd_bin, name, SCENARIOS[name], out_root),
        _collect, netns_runs.parallel())


@pytest.fixture(scope="module")
def ref_run(xlxd_ref_bin, out_root):
    launched = {"ref_no_usrp": _launch(xlxd_ref_bin, "ref_no_usrp", SCENARIOS["no_usrp"], out_root)}
    return _collect(launched)["ref_no_usrp"]


def environment_check(run):
    """Fail with the environment text, not a product-sounding one, when the
    scenario's logs show an exhausted task limit (dvtest/netns_runs.py)."""
    from dvtest import netns_runs
    message = netns_runs.starvation_message(run)
    if message:
        pytest.fail(message, pytrace=False)


def get(runs, name):
    assert name in runs, "scenario %s was not launched" % name
    run = runs[name]
    if "error" in run or not run.get("pinged"):
        environment_check(run)
    assert "error" not in run, run.get("error")
    assert run["pinged"], "xlxd never sent AMBEDPING to the transcoder"
    return run


def ctrl_bytes(run):
    """Control packets other than PING, as bytes."""
    return [bytes.fromhex(h) for _t, h in run["ctrl"] if not h.startswith(b"AMBEDPING".hex())]


def caps_count(run):
    return sum(1 for b in ctrl_bytes(run) if b == b"AMBEDCAPS")


def requests(run, port):
    return [bytes.fromhex(h) for _t, h in run["data_in"].get(str(port), [])]


def phase(run, step_index):
    """(start, end) of a step: until the next step starts."""
    phases = run["phases"]
    end = phases[step_index + 1][1] if step_index + 1 < len(phases) else phases[step_index][2]
    return phases[step_index][1], end


def check_dstar_requests(run, port):
    reqs = requests(run, port)
    assert len(reqs) >= 45, "only %d data requests for 50 frames" % len(reqs)
    assert all(len(r) == 11 for r in reqs), sorted({len(r) for r in reqs})
    assert all(r[0] == 1 and r[2:] == VOICE_FRAME for r in reqs), reqs[0].hex()


# no xlxd.usrp: nothing new on the transcoder link

def extended_always(target):
    """B7: the profile spec "ambed_open" == "extended_always" (dvrouter) or
    the legacy ambed_extended_open_always divergence: once CAPS is answered
    the sut uses the 24 B extended OPEN for every stream, so it probes CAPS
    without USRP endpoints too."""
    from dvtest import specs
    return specs.rule(target, "ambed_open") == "extended_always" or \
        "ambed_extended_open_always" in target.divergences


def netns_spec(target):
    """The profile's expectations for these cases (dvrouter: on-demand
    extended opens, dvtest/specs/dvrouter.py "netns_ambed"), None under the
    xlxd profile."""
    from dvtest import specs
    return specs.rule(target, "netns_ambed")


def caps_times(run):
    return [t for t, h in run["ctrl"] if bytes.fromhex(h) == b"AMBEDCAPS"]


def assert_no_legacy_open(run):
    legacy = [o for o in run["opens"] if o[1] != 24]
    assert not legacy, "legacy OPEN sent: %s" % legacy


def test_no_usrp_sends_no_caps(runs, target):
    assert caps_count(get(runs, "no_usrp")) == (1 if extended_always(target) else 0)


def test_no_usrp_legacy_open(runs, target):
    """B2: xlxd opens a legacy 19 B stream for the D-STAR talker. Profile
    spec (dvrouter): no OPEN at all, nobody listens in another profile."""
    run = get(runs, "no_usrp")
    spec = netns_spec(target)
    if spec is not None:
        assert [o[1:4] for o in run["opens"]] == spec["no_usrp_opens"], run["opens"]
        return
    assert len(run["opens"]) == 1
    _t, size, codec_in, outs, tx, callsign, port = run["opens"][0]
    if extended_always(target):
        assert (size, codec_in) == (24, 1) and tx and outs, run["opens"][0]
    else:
        assert (size, codec_in, outs, tx) == (19, 1, [2, 3, 4], None)
    assert bytes.fromhex(callsign) == b"XLX999  "
    check_dstar_requests(run, port)
    assert [i for _t, i in run["closes"]] == [1]


def test_no_usrp_link_bytes_equal_reference(runs, ref_run, target):
    if extended_always(target):
        pytest.skip("sut declares ambed_extended_open_always: its link bytes differ from ref by design")
    run = get(runs, "no_usrp")
    assert "error" not in ref_run, ref_run.get("error")
    assert ctrl_bytes(run) == ctrl_bytes(ref_run)


# transcoder without the extension (never answers CAPS)

def test_legacy_ambed_single_caps_probe(runs, target):
    """B4: xlxd probes CAPS once. Profile spec (dvrouter): the probe repeats
    every period_s until answered (this transcoder never answers)."""
    run = get(runs, "legacy_ambed")
    spec = netns_spec(target)
    if spec is None:
        assert caps_count(run) == 1
        return
    probe = spec["caps_probe"]
    times = caps_times(run)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= probe["min_probes"], "CAPS probes %d" % len(times)
    assert all(abs(g - probe["period_s"]) <= probe["tolerance_s"] for g in gaps), \
        "CAPS probe gaps %s s, expected %.1f +- %.1f" % ([round(g, 2) for g in gaps], probe["period_s"],
                                                       probe["tolerance_s"])


def test_legacy_ambed_digital_uses_legacy_open(runs, target):
    """B4: xlxd falls back to the legacy open. Profile spec (dvrouter):
    extended opens only, so never a legacy OPEN."""
    run = get(runs, "legacy_ambed")
    spec = netns_spec(target)
    if spec is not None and spec["legacy_open"] is False:
        assert_no_legacy_open(run)
        return
    digital = [o for o in run["opens"] if o[2] == 1]
    assert len(digital) == 1 and digital[0][1:5] == [19, 1, [2, 3, 4], None], digital
    check_dstar_requests(run, digital[0][6])


def test_legacy_ambed_pcm_not_transcoded(runs):
    run = get(runs, "legacy_ambed")
    assert not [o for o in run["opens"] if o[2] == 5], run["opens"]
    assert not run["usrp_rx"], "USRP peer got %d packets" % len(run["usrp_rx"])


# extended transcoder: D-STAR -> USRP, USRP -> digital, bad frame

def test_extended_single_caps_probe(runs):
    """B6: one CAPS probe; the transcoder answers it (every profile)."""
    assert caps_count(get(runs, "extended")) == 1


def test_extended_opens(runs, target):
    """B6: xlxd asks for every codec its module may need; profile spec
    (dvrouter): outputs on demand, only the listeners' profiles."""
    run = get(runs, "extended")
    shapes = [o[1:4] for o in run["opens"]]
    spec = netns_spec(target)
    want = spec["extended_opens"] if spec is not None else [[24, 1, [2, 5]], [24, 5, [1, 2]]]
    assert shapes == want, shapes
    txs = [o[4] for o in run["opens"]]
    assert all(txs) and len(set(txs)) == 2, txs
    assert len(run["closes"]) == 2


def test_extended_dstar_to_usrp(runs):
    run = get(runs, "extended")
    assert run["opens"], "no open"
    check_dstar_requests(run, run["opens"][0][6])
    start, end = phase(run, 0)
    rx = [r for r in run["usrp_rx"] if start <= r[0] < end]
    keyed = [r for r in rx if r[1] == 352]
    assert len(keyed) >= 45, "USRP voice frames %d" % len(keyed)
    assert all(r[3] == 1 and r[4] for r in keyed), "keyed frames must carry the transcoder PCM"
    seqs = [r[2] for r in rx]
    assert seqs == sorted(seqs), "USRP seq not increasing"
    assert rx[-1][1] == 32 and rx[-1][3] == 0, "stream must end with an unkey"


def test_extended_usrp_to_digital(runs):
    run = get(runs, "extended")
    assert len(run["opens"]) == 2, run["opens"]
    reqs = requests(run, run["opens"][1][6])
    assert len(reqs) >= 50, "PCM data requests %d" % len(reqs)
    assert all(len(r) == 322 and r[0] == 5 for r in reqs)
    assert reqs[0][2:] == USRP_PCM, "PCM must reach the transcoder unchanged"


def test_extended_no_echo_to_usrp_peer(runs):
    run = get(runs, "extended")
    start, end = phase(run, 1)
    echoed = [r for r in run["usrp_rx"] if start <= r[0] < end]
    assert not echoed, "%d packets echoed to the originating USRP peer" % len(echoed)


def test_extended_bad_frame_dropped(runs):
    run = get(runs, "extended")
    start, _end = phase(run, 2)
    later = [o for o in run["opens"] if o[0] >= start]
    assert not later, later
    assert run["xlxd_alive"], "xlxd exited"


# transcoder announcing a single output

def test_one_output_opens(runs, target):
    """B11: one output per open, distinct transactions; profile spec
    (dvrouter): the on-demand output (PCM for the USRP peer)."""
    run = get(runs, "one_output")
    shapes = [o[1:4] for o in run["opens"]]
    spec = netns_spec(target)
    want = spec["one_output_opens"] if spec is not None else [[24, 1, [2]], [24, 1, [2]]]
    assert shapes == want, shapes
    assert run["opens"][0][4] != run["opens"][1][4], "transaction reused"
    for o in run["opens"]:
        check_dstar_requests(run, o[6])


# STD delayed 1 s: a short USRP burst is held until the stream is open

def test_slow_open_short_burst_delivered(runs, target):
    """B12: STD delayed 1 s, the burst is held and delivered. Profile spec
    (dvrouter): no digital listener, so no OPEN at all."""
    run = get(runs, "slow_open")
    spec = netns_spec(target)
    if spec is not None:
        assert [o[1:4] for o in run["opens"]] == spec["slow_open_opens"], run["opens"]
        return
    assert [o[1:4] for o in run["opens"]] == [[24, 5, [1, 2]]]
    reqs = requests(run, run["opens"][0][6])
    assert len(reqs) >= 5, "data requests %d for a 5-frame burst" % len(reqs)
    assert all(len(r) == 322 for r in reqs) and reqs[0][2:] == USRP_PCM


def open_wait_observed(run, open_delay):
    """The numbers of the slow_open_listener scenario (times on the fake's
    clock): burst sent, OPEN seen, STD answered, what the listener heard."""
    burst = run["usrp_tx"][0]
    open_t = run["opens"][0][0]
    std = [t for t, text in run["events"] if text.startswith("OPEN")]
    std_t = std[0] if std else open_t + open_delay
    rx = run["dstar_rx"]
    voice = [r for r in rx if r[4]]                  # frames carrying the transcoder AMBE+
    return {
        "usrp_frames_sent": SHORT_BURST, "usrp_first_sent": burst[0], "usrp_last_sent": burst[1],
        "usrp_unkey_sent": burst[2], "open_at": open_t, "open_lag_s": open_t - burst[0],
        "std_at": std_t, "std_delay_s": std_t - open_t,
        "dstar_headers": run["dstar_hdr"], "dstar_frames_heard": len(voice),
        "dstar_end_frames": sum(1 for r in rx if r[3]),
        "dstar_first_heard": voice[0][0] if voice else None,
        "dstar_last_heard": voice[-1][0] if voice else None,
        "dstar_seqs": [r[2] for r in voice], "dstar_stream_ids": sorted({r[1] for r in rx}),
        "dstar_rx": rx, "fake_data_requests": sum(len(v) for v in run["data_in"].values()),
    }


def test_open_wait_listener_short_burst_heard(runs, target):
    """H4 / B12 for a DV-Router: the 5-frame USRP burst (100 ms) ends long
    before the slow OPEN is answered (STD 1.0 s after the OPEN, inside the
    1050 ms OPEN wait), and a D-STAR listener on the module must still hear
    all of it. The design (DV-Router docs/design/transcoding.md): the OPEN
    starts with the first voice unit, no added timer (:344); before
    SessionReady the controller buffers up to 128 units with no age drop
    (:344), so 5 units are all kept; EndSession of a stream closed while
    opening waits for SessionReady (crates/room/src/xcode.rs close_stream),
    the buffer is then flushed in order (:344) and the target StreamEnd
    follows the outputs of the last input (:347); a slow STD below 1050 ms is
    a normal open (:510); the controller gives up 3000 ms after the first
    voice unit (:341, :344). So: nothing lost, nothing before the STD, all
    inside the give-up. Skipped under the xlxd profile (the scenario is not
    launched; B12 is test_slow_open_short_burst_delivered)."""
    spec = netns_spec(target)
    if spec is None:
        pytest.skip("DV-Router profile spec only (xlxd: test_slow_open_short_burst_delivered)")
    want = spec["slow_open_listener"]
    run = get(runs, "slow_open_listener")
    assert run["listen_ack"] and run["listen_ack"][0], "the D-STAR listener was not acked on module B"
    assert [o[1:4] for o in run["opens"]] == want["opens"], run["opens"]
    seen = open_wait_observed(run, SCENARIOS["slow_open_listener"]["open_delay"])
    (Path(run["run_dir"]) / "observed.json").write_text(json.dumps(seen, indent=1))
    # OPEN starts with the first voice unit (transcoding.md:344, no added timer)
    assert 0 <= seen["open_lag_s"] <= want["open_lag_s"], \
        "OPEN %.3f s after the first USRP frame" % seen["open_lag_s"]
    assert abs(seen["std_delay_s"] - SCENARIOS["slow_open_listener"]["open_delay"]) <= 0.15, seen["std_delay_s"]
    # the burst is over before the STD: this is the situation under test
    assert seen["usrp_unkey_sent"] < seen["std_at"], "burst not over before the STD"
    assert seen["dstar_frames_heard"] == want["frames_heard"], \
        "listener heard %d of %d burst frames (seqs %s, end frames %d)" % (
            seen["dstar_frames_heard"], SHORT_BURST, seen["dstar_seqs"], seen["dstar_end_frames"])
    assert len(seen["dstar_seqs"]) == len(set(seen["dstar_seqs"])), seen["dstar_seqs"]
    assert seen["dstar_seqs"] == sorted(seen["dstar_seqs"]), seen["dstar_seqs"]
    assert len(seen["dstar_stream_ids"]) == 1, seen["dstar_stream_ids"]
    # nothing is heard before the transcoder answered, all of it inside the give-up
    assert seen["dstar_first_heard"] >= seen["std_at"] - 0.01, seen["dstar_first_heard"]
    assert seen["dstar_last_heard"] - seen["usrp_first_sent"] <= want["give_up_s"], \
        "last frame heard %.3f s after the first USRP frame" % (seen["dstar_last_heard"] - seen["usrp_first_sent"])
    # the transcoder got every unit of the burst (5 PCM requests, unchanged PCM)
    assert seen["fake_data_requests"] == SHORT_BURST, seen["fake_data_requests"]
    reqs = requests(run, run["opens"][0][6])
    assert all(len(r) == 322 and r[0] == 5 and r[2:] == USRP_PCM for r in reqs)


# ---- H5 / H6: the documented limits of the opening session

def hub_counter(run, step, name, **labels):
    """Value of a hub counter in the snapshot taken after the step (name of
    the step as in the scenario), summed over the rooms; None when the
    scenario has no snapshot of it."""
    from dvtest import metrics
    for got, _t, lines in run.get("metrics", []):
        if got == step:
            if lines and lines[0].startswith("error:"):
                return None
            return metrics.total(metrics.parse("\n".join(lines)), name, **labels)
    return None


def xcode_counters(run, step):
    """The transcoding counters of the hub after the step: sessions by result
    (dv_xcode_sessions_total) and frames by direction and result
    (dv_xcode_frames_total)."""
    out = {}
    for result in ("opened", "open_failed", "failed", "drain_timeout"):
        out["sessions_" + result] = hub_counter(run, step, "dv_xcode_sessions_total", result=result)
    for dir_, result in (("in", "sent"), ("in", "skipped"), ("in", "pre_ready_dropped"),
                         ("out", "published"), ("out", "dropped")):
        out["frames_%s_%s" % (dir_, result)] = hub_counter(run, step, "dv_xcode_frames_total",
                                                           dir=dir_, result=result)
    return out


def pre_ready_observed(run, open_delay, frames, gap):
    """The numbers of the pre_ready_overflow scenario (times on the fake's
    clock): the over sent, OPEN and STD, the PCM units the transcoder got
    (named by indexed_pcm), what the listener heard, the hub counters."""
    sent = run["usrp_tx"][0]
    open_t = run["opens"][0][0]
    std = [t for t, text in run["events"] if text.startswith("OPEN")]
    std_t = std[0] if std else open_t + open_delay
    port = run["opens"][0][6]
    reqs = run["data_in"].get(str(port), [])
    indexes = [pcm_index(bytes.fromhex(h)) for _t, h in reqs]
    rx = run["dstar_rx"]
    voice = [r for r in rx if r[4]]
    step = "usrp:%d:%d" % (frames, round(gap * 1000))
    return {
        "usrp_frames_sent": frames, "usrp_gap_s": gap, "usrp_first_sent": sent[0],
        "usrp_last_sent": sent[1], "usrp_unkey_sent": sent[2],
        "open_at": open_t, "open_lag_s": open_t - sent[0], "std_at": std_t, "std_delay_s": std_t - open_t,
        "fake_data_requests": len(reqs), "fake_request_indexes": indexes,
        "fake_first_request_at": reqs[0][0] if reqs else None,
        "fake_last_request_at": reqs[-1][0] if reqs else None,
        "dstar_headers": run["dstar_hdr"], "dstar_frames_heard": len(voice),
        "dstar_end_frames": sum(1 for r in rx if r[3]),
        "dstar_first_heard": voice[0][0] if voice else None,
        "dstar_last_heard": voice[-1][0] if voice else None,
        "dstar_seqs": [r[2] for r in voice], "dstar_stream_ids": sorted({r[1] for r in rx}),
        "closes": run["closes"], "counters": xcode_counters(run, step),
    }


def test_pre_ready_cap_oldest_units_dropped(runs, target):
    """H5: a USRP over of 200 units sent 2 ms apart (0.4 s) while the STD is
    1.0 s late has 200 units in hand before SessionReady, 72 more than the
    pre-ready buffer. The design (DV-Router docs/design/transcoding.md:344,
    :352; crates/room/src/xcode.rs PRE_READY_CAP and Xcode::unit): before
    SessionReady the controller buffers up to 128 units per session with no
    age drop; when the buffer is full the oldest unit is dropped with a
    counter (dv_xcode_frames_total{dir="in",result="pre_ready_dropped"}), and
    the buffer is then sent in order. So the transcoder gets the newest 128
    units (input_seq 72 to 199), in order, the D-STAR listener hears 128
    voice frames and the counter says 72. A real-time over cannot get there
    (128 units are 2.56 s, the OPEN wait is 1050 ms): the over has to be
    faster than real time. Skipped under the xlxd profile."""
    spec = netns_spec(target)
    if spec is None:
        pytest.skip("DV-Router profile spec only (the scenario is not launched under xlxd)")
    want = spec["pre_ready_overflow"]
    scenario = SCENARIOS["pre_ready_overflow"]
    run = get(runs, "pre_ready_overflow")
    assert run["listen_ack"] and run["listen_ack"][0], "the D-STAR listener was not acked on module B"
    assert [o[1:4] for o in run["opens"]] == want["opens"], run["opens"]
    seen = pre_ready_observed(run, scenario["open_delay"], want["frames"], want["gap_s"])
    (Path(run["run_dir"]) / "observed.json").write_text(json.dumps(seen, indent=1))
    assert 0 <= seen["open_lag_s"] <= want["open_lag_s"], "OPEN %.3f s after the first USRP frame" % seen["open_lag_s"]
    assert abs(seen["std_delay_s"] - scenario["open_delay"]) <= 0.15, seen["std_delay_s"]
    # the whole over is in hand before SessionReady: this is the situation under test
    assert seen["usrp_unkey_sent"] < seen["std_at"], "over not finished before the STD"
    kept = want["frames"] - want["dropped"]
    # which units: the newest `kept`, in order (the oldest are dropped, :344)
    assert seen["fake_request_indexes"] == list(range(want["dropped"], want["frames"])), \
        "transcoder got %d units, indexes %s .. %s" % (
            len(seen["fake_request_indexes"]), seen["fake_request_indexes"][:3], seen["fake_request_indexes"][-3:])
    # the listener hears exactly those, after the STD, inside the give-up
    assert seen["dstar_frames_heard"] == kept, \
        "listener heard %d of %d units" % (seen["dstar_frames_heard"], want["frames"])
    assert len(seen["dstar_stream_ids"]) == 1, seen["dstar_stream_ids"]
    assert seen["dstar_first_heard"] >= seen["std_at"] - 0.01, seen["dstar_first_heard"]
    assert seen["dstar_last_heard"] - seen["usrp_first_sent"] <= want["give_up_s"], \
        "last frame heard %.3f s after the first USRP frame" % (seen["dstar_last_heard"] - seen["usrp_first_sent"])
    # the counter of the drop
    counters = seen["counters"]
    assert counters["frames_in_pre_ready_dropped"] == want["dropped"], counters
    assert counters["sessions_opened"] == 1 and counters["sessions_open_failed"] == 0, counters
    assert counters["frames_out_published"] == kept, counters


def open_refused_observed(run):
    """The numbers of the open_late_std scenario (times on the fake's clock):
    over 1 (100 units, STD 1.5 s late) and over 2 (5 units, STD at once), what
    the fake saw (OPENs, STDs, CS, CAPS probes, PCM requests per open), what
    the listener heard in each window, the hub counters after each over."""
    over1, over2 = run["usrp_tx"][0], run["usrp_tx"][1]
    opens = run["opens"]
    stds = [t for t, text in run["events"] if text.startswith("OPEN")]
    caps_after_open1 = [t - opens[0][0] for t in caps_times(run) if opens[0][0] < t < opens[1][0]]
    rx, hdr = run["dstar_rx"], run["dstar_hdr"]
    window1 = [r for r in rx if r[0] < over2[0]]
    window2 = [r for r in rx if r[0] >= over2[0]]
    voice2 = [r for r in window2 if r[4]]
    ports = [o[6] for o in opens]
    return {
        "over1_first_sent": over1[0], "over1_last_sent": over1[1], "over1_unkey_sent": over1[2],
        "over2_first_sent": over2[0], "over2_last_sent": over2[1], "over2_unkey_sent": over2[2],
        "open1_at": opens[0][0], "open2_at": opens[1][0] if len(opens) > 1 else None,
        "open1_lag_s": opens[0][0] - over1[0],
        "std_at": stds, "std1_delay_s": stds[0] - opens[0][0] if stds else None,
        "std2_delay_s": stds[1] - opens[1][0] if len(stds) > 1 else None,
        "closes": run["closes"], "caps_after_open1_s": caps_after_open1,
        "requests_open1": len(run["data_in"].get(str(ports[0]), [])),
        "requests_open2": len(run["data_in"].get(str(ports[1]), [])) if len(ports) > 1 else None,
        "over1_dstar_frames": len(window1), "over1_dstar_headers": len([h for h in hdr if h[0] < over2[0]]),
        "over2_dstar_frames_heard": len(voice2), "over2_dstar_end_frames": sum(1 for r in window2 if r[3]),
        "over2_dstar_first_heard": voice2[0][0] if voice2 else None,
        "over2_dstar_last_heard": voice2[-1][0] if voice2 else None,
        "over2_dstar_stream_ids": sorted({r[1] for r in window2}),
        "counters_after_over1": xcode_counters(run, "usrp:100:20"),
        "counters_after_over2": xcode_counters(run, "usrpShortB"),
    }


def test_open_refused_late_std_given_up(runs, target):
    """H6: an STD 1.5 s after its OPEN is later than the 1050 ms OPEN wait.
    The design (DV-Router docs/design/transcoding.md:472, :473, :509, :346):
    nothing within 1050 ms closes the worker session, one immediate CAPS
    re-probe, no CS yet, SessionReady accepted = 0 retryable = 1; the
    controller counts a failed open (:346, dv_xcode_sessions_total{result=
    "open_failed"}), does not produce the target streams of that over and
    never retries within a stream; the late STD finds the transaction
    forgotten: CS for its stream, nothing bound (:473, so the transcoder sees
    no data request on that stream). The next over opens again and is heard
    in full. Skipped under the xlxd profile."""
    spec = netns_spec(target)
    if spec is None:
        pytest.skip("DV-Router profile spec only (the scenario is not launched under xlxd)")
    want = spec["open_late_std"]
    scenario = SCENARIOS["open_late_std"]
    run = get(runs, "open_late_std")
    assert run["listen_ack"] and run["listen_ack"][0], "the D-STAR listener was not acked on module B"
    seen = open_refused_observed(run)
    (Path(run["run_dir"]) / "observed.json").write_text(json.dumps(seen, indent=1))
    # one OPEN per over: no retry within the first stream (:140, :346)
    assert [o[1:4] for o in run["opens"]] == want["opens"], run["opens"]
    assert 0 <= seen["open1_lag_s"] <= want["open_lag_s"], "OPEN %.3f s after the first USRP frame" % seen["open1_lag_s"]
    assert abs(seen["std1_delay_s"] - scenario["open_delays"][0]) <= 0.15, seen["std1_delay_s"]
    assert seen["std1_delay_s"] > want["open_wait_s"], "the STD is not later than the OPEN wait"
    assert seen["open2_at"] > seen["std_at"][0], "the second OPEN is not after the late STD"
    # the open is given up: one CAPS re-probe shortly after the OPEN wait (:472)
    assert len(seen["caps_after_open1_s"]) == 1, seen["caps_after_open1_s"]
    assert want["open_wait_s"] - 0.05 <= seen["caps_after_open1_s"][0] <= want["reprobe_within_s"], \
        "CAPS re-probe %.3f s after the OPEN" % seen["caps_after_open1_s"][0]
    # the late STD: CS for its stream, no data request on it (:473, :509)
    cs1 = [t for t, stream in run["closes"] if stream == 1]
    assert len(cs1) == 1 and seen["std_at"][0] <= cs1[0] <= seen["std_at"][0] + want["cs_within_s"], \
        "CS for the late stream %s, STD at %.3f" % (run["closes"], seen["std_at"][0])
    assert seen["requests_open1"] == 0, "%d data requests on the given-up open" % seen["requests_open1"]
    # nothing of the over is heard: no header, no frame, until the second over starts
    assert seen["over1_dstar_frames"] == 0 and seen["over1_dstar_headers"] == 0, \
        "listener heard %d frames, %d headers of the refused over" % (seen["over1_dstar_frames"],
                                                                      seen["over1_dstar_headers"])
    # the counters (:346): a failed open, nothing sent to the transcoder, nothing published. Note:
    # result="opened" counts the CreateSessions sent (xcode.rs open_pending), the failed one included
    first = seen["counters_after_over1"]
    assert first["sessions_open_failed"] == 1 and first["sessions_opened"] == 1, first
    assert first["frames_in_sent"] == 0 and first["frames_out_published"] == 0, first
    # the room recovers: the next over opens at once and is heard in full
    assert abs(seen["std2_delay_s"] - scenario["open_delays"][1]) <= 0.15, seen["std2_delay_s"]
    assert seen["over2_dstar_frames_heard"] == want["frames_heard"], \
        "listener heard %d of %d units of the second over" % (seen["over2_dstar_frames_heard"], SHORT_BURST)
    assert seen["over2_dstar_end_frames"] == 1 and len(seen["over2_dstar_stream_ids"]) == 1, seen
    assert seen["over2_dstar_last_heard"] - seen["over2_first_sent"] <= want["give_up_s"], seen
    assert seen["requests_open2"] == SHORT_BURST, seen["requests_open2"]
    reqs = requests(run, run["opens"][1][6])
    assert all(len(r) == 322 and r[0] == 5 and r[2:] == USRP_PCM for r in reqs)
    second = seen["counters_after_over2"]
    assert second["sessions_opened"] == 2 and second["sessions_open_failed"] == 1, second
    assert second["frames_out_published"] == SHORT_BURST, second


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--inside":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _inside(json.loads(sys.argv[2]), sys.argv[3])
