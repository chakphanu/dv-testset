# SPDX-License-Identifier: MIT
"""xlxd transcoder link + USRP endpoints, black box, in private network
namespaces (unshare -rn, no root, no docker).

Each scenario runs this file inside its own namespace: xlxd XLX999 on
127.0.0.1 with the transcoder at 127.0.0.2, a FakeAmbed there, a DExtra
talker on 127.0.0.3 and a USRP peer on 127.0.0.4. The recorded traffic is
written as JSON and asserted here. Scenarios run in parallel.

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

# name: usrp config present, fake ambed mode / max outputs / STD delay, steps
SCENARIOS = {
    "no_usrp": dict(usrp=False, mode="extended", max_outputs=4, open_delay=0.05,
                    steps=["dstarA"]),
    "legacy_ambed": dict(usrp=True, mode="legacy", max_outputs=4, open_delay=0.05,
                         steps=["dstarB", "usrpB"]),
    "extended": dict(usrp=True, mode="extended", max_outputs=4, open_delay=0.05,
                     steps=["dstarB", "usrpB", "badB"]),
    "one_output": dict(usrp=True, mode="extended", max_outputs=1, open_delay=0.05,
                       steps=["dstarB", "dstarB"]),
    "slow_open": dict(usrp=True, mode="extended", max_outputs=4, open_delay=1.0,
                      steps=["usrpShortB"]),
}
TIMEOUT = 120


# ---------------------------------------------------------------- inside

def _inside(spec, run_dir):
    """Runs in the namespace: xlxd + fakes, writes run_dir/result.json."""
    import ctypes
    import signal
    import socket
    import threading

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
                           open_delay=spec["open_delay"])
    log = open(run_dir / "xlxd.log", "wb")
    # DV_RUN_DIR: a DV-Router keeps its run state here (xlxd ignores it)
    xlxd = subprocess.Popen([spec["bin"], "XLX999", XLXD_IP, AMBED_IP], cwd=run_dir,
                            env=dict(os.environ, DV_RUN_DIR=str(run_dir)),
                            stdout=log, stderr=subprocess.STDOUT, preexec_fn=die_with_parent)
    result = {"phases": [], "usrp_rx": [], "dextra_ack": []}
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
            start = fake.now()
            if step.startswith("dstar"):
                dstar(step[-1], 0x1234 + index)
            elif step == "usrpB":
                usrp_talk(50)
            elif step == "usrpShortB":
                usrp_talk(5)
            elif step == "badB":
                # TLV/text frame from the configured peer: must be dropped
                peer.sendto(usrp.frame(0, usrp.TYPE_TEXT, b"\x08\x00"), (XLXD_IP, 34001))
            time.sleep(2.5)
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
    return results


@pytest.fixture(scope="module")
def runs(xlxd_bin, out_root):
    launched = {name: _launch(xlxd_bin, name, spec, out_root) for name, spec in SCENARIOS.items()}
    return _collect(launched)


@pytest.fixture(scope="module")
def ref_run(xlxd_ref_bin, out_root):
    launched = {"ref_no_usrp": _launch(xlxd_ref_bin, "ref_no_usrp", SCENARIOS["no_usrp"], out_root)}
    return _collect(launched)["ref_no_usrp"]


def get(runs, name):
    run = runs[name]
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


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--inside":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _inside(json.loads(sys.argv[2]), sys.argv[3])
