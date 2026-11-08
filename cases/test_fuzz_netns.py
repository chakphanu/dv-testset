# SPDX-License-Identifier: MIT
"""Robustness of the DV-Router protocol edges against malformed input, black box,
in private network namespaces (unshare -rn, no root, no docker). DV-Router profile
only (DVT_SUT_PROFILE=dvrouter); the scenarios are not launched under xlxd.

Every scenario runs this file inside its own namespace, like
cases/test_xlxd_usrp_netns.py: DV-Router (scripts/dvr-import-wrap.sh) on 127.0.0.1
with a FakeAmbed at 127.0.0.2. For one protocol port it then

  1. waits for the UDP port(s) the edge should bind (/proc/net/udp of the namespace);
     none bound: the cases skip with that reason,
  2. runs one VALID round (a fresh client must get its link acknowledged, and an over
     of a linked talker must reach a linked listener), takes the baseline (the
     launcher's descendants with pid and start time, VmRSS of the edge, the hub
     counters),
  3. sends DVT_FUZZ_N (2000) mutated datagrams (dvtest/fuzz.py), 1 ms apart, from a
     client address (127.0.0.5: two cases in three from a linked session of that
     client, one in three from a socket that never linked),
  4. repeats the valid round after every 200 datagrams, and checks that no process of
     the launcher's tree is gone or new,
  5. reads the hub counters again.

Four cases per protocol read that one run:

  test_fuzz_<p>_edge_survives        launcher alive, no child gone or restarted
  test_fuzz_<p>_valid_client_served  every valid round: link ack, over heard
  test_fuzz_<p>_memory_bounded       VmRSS of the edge grew by at most RSS_GROWTH_MAX_KIB
  test_fuzz_<p>_counters_move        the edge's drop / reject counters moved

A failure writes fuzz-replay-<p>.json (protocol, seed, index and hex of the last 50
datagrams) to the scenario's run directory; python3 -m dvtest.tools.fuzz_replay resends
it, or DVT_FUZZ_REPLAY=<file> runs the scenario again with exactly those datagrams.
Details, ports and what is unverified: docs/fuzz-cases.md.

DVT_FUZZ_N, DVT_FUZZ_SEED, DVT_FUZZ_GAP_MS, DVT_FUZZ_REPLAY: dvtest/fuzz_run.py
settings_from_env. DVT_NETNS_PARALLEL stays 1 (a DV-Router scenario peaks near 435
tasks of the 1024 of the scope).
"""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

if __name__ == "__main__":                  # the namespace process runs this file with -I: no cwd, no PYTHONPATH
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.netns

XLXD_IP, AMBED_IP, USRP_IP = "127.0.0.1", "127.0.0.2", "127.0.0.4"
TALK_IP, FUZZ_IP, LISTEN_IP, PROBE_IP, STRANGER_IP = "127.0.0.3", "127.0.0.5", "127.0.0.6", "127.0.0.7", "127.0.0.9"
USRP_LINE = "B %s 34001 %s 32001 N0CALL" % (XLXD_IP, USRP_IP)
HUB_METRICS = "http://127.0.0.1:19108/metrics"      # dv-svc-status hub of the converted config (in the namespace)

EVERY = 200                       # datagrams between two valid rounds
VALID_FRAMES = 54                 # frames of a valid over: not below 50 (the short-stream guard counts overs under 50)
HEARD_MIN = 0.5                   # the listener must hear at least this share of the over
RSS_GROWTH_MAX_KIB = 8192         # VmRSS of the edge after the run minus before (KiB): UNVERIFIED, see docs/fuzz-cases.md
PORT_WAIT = 12.0                  # s for the edge to bind its port after the pinged transcoder
DROPS_WAIT = 14.0                 # s to wait for the drop counters to appear on the hub after the last datagram
SCENARIO_BUDGET = 150.0           # s inside the namespace; longer is reported as "truncated"
LISTEN_SETTLE = 1.0               # s after the link ack (DV-Router demand token), as in the usrp netns cases
USRP_PCM = (b"\xe8\x03\x18\xfc") * 80

# scenario name == protocol group (dvtest.fuzz.PORT_GROUPS); the child processes that serve the port
EDGES = {"dextra": ("dv-edge-dextra",), "dplus": ("dv-edge-dplus",), "dcs": ("dv-edge-dcs",),
         "dmr": ("dv-edge-dmrhb", "dv-edge-mmdvm"), "dmrplus": ("dv-edge-dmrplus",), "ysf": ("dv-edge-ysf",),
         "m17": ("dv-edge-m17",), "nxdn": ("dv-edge-nxdn",), "p25": ("dv-edge-p25",), "usrp": ("dv-edge-usrp",),
         "xlx": ("dv-edge-xlx",), "imrs": ("dv-edge-imrs",), "g3": ("dv-edge-g3",)}
PROTOCOL_NAMES = tuple(EDGES)
TIMEOUT_FLOOR = 240


def _scenarios():
    from dvtest import fuzz
    out = {}
    for name in PROTOCOL_NAMES:
        out[name] = dict(group=list(fuzz.PORT_GROUPS[name]), usrp=(name == "usrp"), edges=list(EDGES[name]),
                         cases="test_fuzz_%s_" % name, profile="dvrouter")
    return out


SCENARIOS = _scenarios()


# ---------------------------------------------------------------- inside

def _inside(spec, run_dir):
    """Runs in the namespace: DV-Router + a fake transcoder + the fuzzer, writes run_dir/result.json."""
    import ctypes
    import signal
    import socket
    import urllib.request

    from dvtest import fuzz, fuzz_run
    from dvtest.proto import ambed, dextra, usrp
    from dvtest.net import pace

    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    run_dir = Path(run_dir)
    if spec["usrp"]:
        (run_dir / "xlxd.usrp").write_text(USRP_LINE + "\n")
    libc = ctypes.CDLL(None, use_errno=True)
    group, valid_protocol = spec["group"], spec["group"][0]

    def die_with_parent():
        libc.prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG

    fake = ambed.FakeAmbed(AMBED_IP, mode="extended", max_outputs=4, open_delay=0.05)
    log = open(run_dir / "xlxd.log", "wb")
    xlxd = subprocess.Popen([spec["bin"], "XLX999", XLXD_IP, AMBED_IP], cwd=run_dir,
                            env=dict(os.environ, DV_RUN_DIR=str(run_dir)),
                            stdout=log, stderr=subprocess.STDOUT, preexec_fn=die_with_parent)
    result = {"scenario": spec["name"], "group": group, "seed": spec["seed"], "n": spec["n"], "rounds": [],
              "ports": {}, "errors": 0, "sent": 0}
    sources, valid = [], []

    def scrape():
        try:
            with urllib.request.urlopen(HUB_METRICS, timeout=2) as reply:
                text = reply.read().decode("utf-8", "replace")
            return [ln for ln in text.splitlines() if ln.startswith(fuzz_run.METRIC_PREFIXES)]
        except Exception as exc:                # a missing hub is a finding, not a crash
            return ["error: %s" % exc]

    # ---- the valid clients: a talker and a listener linked for the whole run, a fresh link probe per round
    def usrp_peer_socket():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((USRP_IP, 32001))
        return sock

    def valid_setup():
        """Link the valid clients; returns (ok, reason, kind). kind "voice" or "reply" (xlx, imrs, g3)."""
        if valid_protocol == "usrp":
            peer = usrp_peer_socket()
            valid.append(("peer", peer))
            listener = dextra.DstarClient(LISTEN_IP, callsign="N0TEST", module="B")
            valid.append(("listener", listener))
            if not listener.link(XLXD_IP):
                return False, "the D-STAR listener on module B was not acked", "voice"
            time.sleep(LISTEN_SETTLE)
            return True, "", "voice"
        if valid_protocol == "imrs":
            return True, "no link or reply to check (the peer list is static)", "liveness"
        if valid_protocol not in fuzz_run.CLIENT_KINDS:
            ok = fuzz_run.link_probe(valid_protocol, XLXD_IP, PROBE_IP)
            return bool(ok), ("" if ok else "no reply to a valid request"), "reply"
        talker = fuzz_run.make_client(valid_protocol, TALK_IP, "talker", nxdn_tg=spec["nxdn_tg"], p25_tg=spec["p25_tg"])
        listener = fuzz_run.make_client(valid_protocol, LISTEN_IP, "listener", nxdn_tg=spec["nxdn_tg"],
                                        p25_tg=spec["p25_tg"])
        valid.extend([("talker", talker), ("listener", listener)])
        if not fuzz_run.link(valid_protocol, listener, XLXD_IP):
            return False, "the listener link was not acknowledged", "voice"
        if not fuzz_run.link(valid_protocol, talker, XLXD_IP):
            return False, "the talker link was not acknowledged", "voice"
        return True, "", "voice"

    def usrp_over(frames):
        peer = dict(valid)["peer"]
        listener = dict(valid)["listener"]
        listener.client.inbox.clear()
        start = time.monotonic()
        for seq in range(frames):
            pace(start, seq)
            peer.sendto(usrp.voice(seq, USRP_PCM), (XLXD_IP, 34001))
        peer.sendto(usrp.unkey(frames), (XLXD_IP, 34001))
        got = listener.collect(1.5)
        return sum(1 for f in got if f == ambed.fake_payload(ambed.AMBEPLUS))

    def valid_round(tag, kind):
        """One valid round: {tag, probe, heard, expected, ok}."""
        row = {"tag": tag, "probe": None, "heard": None, "expected": None, "ok": True}
        box = {}
        probe = None
        if valid_protocol not in ("usrp", "imrs"):
            probe = threading.Thread(target=lambda: box.update(
                ok=fuzz_run.link_probe(valid_protocol, XLXD_IP, PROBE_IP)), daemon=True)
            probe.start()
        if kind == "voice":
            if valid_protocol == "usrp":
                row["expected"] = VALID_FRAMES
                row["heard"] = usrp_over(VALID_FRAMES)
            else:
                talker, listener = dict(valid)["talker"], dict(valid)["listener"]
                frames = fuzz_run.frames_for(valid_protocol, VALID_FRAMES, seed="valid-%s" % tag)
                listener.client.inbox.clear()
                fuzz_run.drive(valid_protocol, talker, frames, 0x2000 + len(result["rounds"]) + 1)
                row["expected"] = len(frames)
                row["heard"] = fuzz_run.heard(valid_protocol, listener)
            row["ok"] = row["heard"] >= HEARD_MIN * row["expected"]
        if probe is not None:
            probe.join(15)
            row["probe"] = box.get("ok", False)
            row["ok"] = row["ok"] and bool(row["probe"])
        return row

    def ports_open(want):
        deadline = time.monotonic() + PORT_WAIT
        while True:
            with open("/proc/net/udp") as handle:
                bound = fuzz_run.parse_udp_ports(handle.read())
            found = {port: port in bound for port in want}
            if all(found.values()) or time.monotonic() >= deadline:
                return found
            time.sleep(0.25)

    started = time.monotonic()
    try:
        result["pinged"] = fake.connected.wait(15)
        time.sleep(1.5)                         # CAPS probe window
        want = sorted({p for proto in group for p in fuzz.PORTS[proto]})
        result["ports"] = {str(p): v for p, v in ports_open(want).items()}
        if not any(result["ports"].values()):
            result["skip"] = ("UDP port(s) %s not bound inside the namespace: the converted config of this build "
                              "has no %s edge enabled" % (want, spec["name"]))
        else:
            ok, reason, kind = valid_setup()
            warm = valid_round("warm", kind) if ok and kind != "liveness" else {"tag": "warm", "ok": False, "probe": None, "heard": None,
                                                         "expected": None}
            result["warmup"] = dict(warm, setup_ok=ok, reason=reason, kind=kind)
            result["valid_mode"] = "served" if (ok and warm["ok"] and kind != "liveness") else "liveness"
            procs0 = fuzz_run.snapshot(xlxd.pid)
            edge0 = fuzz_run.edge_process(procs0, spec["edges"])
            result["procs0"] = procs0
            result["edge"] = edge0["comm"] if edge0 else None
            lines0 = scrape()
            result["metrics0"] = lines0

            # ---- the fuzz sources
            stranger = fuzz_run.Source(STRANGER_IP)
            sources.append(stranger)
            linked, linked_how = None, "none"
            if valid_protocol == "usrp":
                linked, linked_how = fuzz_run.Source(USRP_IP, sock=dict(valid)["peer"]), "usrp peer socket"
            elif valid_protocol in fuzz_run.CLIENT_KINDS:
                fz = fuzz_run.make_client(valid_protocol, FUZZ_IP, "fuzz", nxdn_tg=spec["nxdn_tg"], p25_tg=spec["p25_tg"])
                sources.append(fz)
                if fuzz_run.link(valid_protocol, fz, XLXD_IP):
                    linked, linked_how = fuzz_run.ClientSource(fz), "linked client"
            elif valid_protocol in fuzz_run.FIXED_PORT:
                try:
                    linked, linked_how = fuzz_run.Source(FUZZ_IP, fuzz_run.FIXED_PORT[valid_protocol]), "fixed source port"
                except OSError:
                    pass
            if linked is None:
                linked = fuzz_run.Source(FUZZ_IP)
            sources.append(linked)
            result["linked_source"] = linked_how
            if spec.get("replay"):
                fuzzer = fuzz_run.Replayer(spec["replay"], XLXD_IP, linked, stranger, gap=spec["gap"])
                total = len(spec["replay"])
            else:
                fuzzer = fuzz_run.Fuzzer(spec["name"], group, XLXD_IP, spec["seed"], linked, stranger, gap=spec["gap"])
                total = spec["n"]

            # ---- fuzz, a valid round every EVERY datagrams
            while fuzzer.sent < total:
                fuzzer.batch(min(EVERY, total - fuzzer.sent))
                row = valid_round("after-%d" % fuzzer.sent, kind)
                procs = fuzz_run.snapshot(xlxd.pid)
                gone, new = fuzz_run.compare_processes(procs0, procs)
                row.update(sent=fuzzer.sent, gone=[p["comm"] for p in gone], new=[p["comm"] for p in new],
                           launcher_alive=xlxd.poll() is None)
                result["rounds"].append(row)
                served = result["valid_mode"] != "served" or row["ok"]
                if gone or new or not row["launcher_alive"] or not served:
                    result["failure"] = {"at": row["tag"], "sent": fuzzer.sent, "row": row,
                                         "last50": fuzzer.ring.entries()}
                    break
                if time.monotonic() - started > SCENARIO_BUDGET:
                    result["truncated"] = True
                    break
            result["sent"], result["errors"] = fuzzer.sent, fuzzer.errors + sum(s.errors for s in (stranger, linked))
            result["last50"] = fuzzer.ring.entries()
            procs1 = fuzz_run.snapshot(xlxd.pid)
            result["procs1"] = procs1
            edge1 = next((p for p in procs1 if edge0 and p["pid"] == edge0["pid"] and p["start"] == edge0["start"]), None)
            result["rss0_kib"] = edge0["rss_kib"] if edge0 else None
            result["rss1_kib"] = edge1["rss_kib"] if edge1 else None
            lines1 = scrape()
            deadline = time.monotonic() + DROPS_WAIT
            while (not lines1 or lines1[0].startswith("error:") or
                   fuzz_run.error_count(lines1, spec["edges"]) <= fuzz_run.error_count(lines0, spec["edges"])) \
                    and time.monotonic() < deadline:
                time.sleep(1.0)
                lines1 = scrape()
            result["metrics1"] = lines1
            result["errors0"] = fuzz_run.error_count(lines0, spec["edges"])
            result["errors1"] = fuzz_run.error_count(lines1, spec["edges"])
            result["alive"] = xlxd.poll() is None
    finally:
        for src in sources:
            try:
                src.close()
            except OSError:
                pass
        for _name, obj in valid:
            try:
                obj.close()
            except (OSError, AttributeError):
                pass
        xlxd.terminate()
        try:
            xlxd.wait(5)
        except subprocess.TimeoutExpired:
            xlxd.kill()
        fake.stop()
        log.close()
    result["seconds"] = round(time.monotonic() - started, 1)
    (run_dir / "result.json").write_text(json.dumps(result, indent=1))


# ---------------------------------------------------------------- outside

def _launch(binary, name, spec, out_root):
    run_dir = out_root / "netns" / ("fuzz-" + name)
    run_dir.mkdir(parents=True, exist_ok=True)
    spec = dict(spec, bin=str(Path(binary).resolve()), name=name)
    cmd = ["unshare", "-rn", sys.executable, "-I", str(Path(__file__).resolve()),
           "--inside", json.dumps(spec), str(run_dir)]
    stderr = open(run_dir / "harness.err", "wb")
    return run_dir, subprocess.Popen(cmd, stdout=stderr, stderr=subprocess.STDOUT), stderr


def _collect(launched, timeout):
    results = {}
    for name, (run_dir, proc, stderr) in launched.items():
        try:
            proc.wait(timeout)
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
def dvrouter_only(target):
    if target.sut_profile != "dvrouter":
        pytest.skip("DV-Router profile only (DVT_SUT_PROFILE=dvrouter): a protocol edge fuzz, not an xlxd comparison")


@pytest.fixture(scope="module")
def runs(request, dvrouter_only, xlxd_bin, out_root, target):
    """Result of every scenario a selected case reads, one after the other (DVT_NETNS_PARALLEL
    is the wave size, default 1)."""
    from dvtest import fuzz_run, netns_runs
    n, seed, gap, replay_path = fuzz_run.settings_from_env()
    replay = None
    if replay_path:
        _doc, replay = fuzz_run.read_replay(Path(replay_path).read_text())
    selected = netns_runs.selected_names(request.session.items, request.module.__name__)
    names = netns_runs.for_profile(netns_runs.scenarios_for(SCENARIOS, selected), SCENARIOS, target.sut_profile)
    if replay is not None:
        wanted = {e["protocol"] for e in replay}
        names = [name for name in names if wanted & set(SCENARIOS[name]["group"])]

    def spec_of(name):
        spec = dict(SCENARIOS[name], n=n, seed=seed, gap=gap,
                    nxdn_tg=int(os.environ.get("DVT_FUZZ_NXDN_TG", "31672")),
                    p25_tg=int(os.environ.get("DVT_FUZZ_P25_TG", "31672")))
        if replay is not None:
            spec["replay"] = [e for e in replay if e["protocol"] in spec["group"]]
        return spec

    total = len(replay) if replay is not None else n
    return netns_runs.run_in_waves(
        names, lambda name: _launch(xlxd_bin, name, spec_of(name), out_root),
        lambda launched: _collect(launched, max(TIMEOUT_FLOOR, fuzz_run.timeout_s(total))), netns_runs.parallel())


def environment_check(run):
    """Fail with the environment text, not a product-sounding one, when the scenario's logs show an
    exhausted task limit (dvtest/netns_runs.py)."""
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
    assert run["pinged"], "DV-Router never sent AMBEDPING to the transcoder"
    if "skip" in run:
        pytest.skip(run["skip"])
    return run


def fail(run, name, message):
    """Write fuzz-replay-<name>.json (the last 50 datagrams at the failure) and fail."""
    from dvtest import fuzz_run
    entries = (run.get("failure") or {}).get("last50") or run.get("last50") or []
    doc = fuzz_run.replay_document(name, run.get("seed"), entries, reason=message, host=XLXD_IP)
    path = Path(run["run_dir"]) / ("fuzz-replay-%s.json" % name)
    path.write_text(json.dumps(doc, indent=1))
    pytest.fail("%s (last %d datagrams: %s; python3 -m dvtest.tools.fuzz_replay --file %s --host <ip>)"
                % (message, len(entries), path, path), pytrace=False)


def check_survives(runs, name):
    run = get(runs, name)
    failure = run.get("failure")
    if failure and (failure["row"].get("gone") or failure["row"].get("new") or not failure["row"].get("launcher_alive")):
        row = failure["row"]
        fail(run, name, "%s after %d datagrams: launcher alive %s, children gone %s, new %s" % (
            failure["at"], failure["sent"], row.get("launcher_alive"), row.get("gone"), row.get("new")))
    if not run.get("alive", False):
        fail(run, name, "the launcher exited during the run")
    gone_new = [(r["tag"], r["gone"], r["new"]) for r in run["rounds"] if r["gone"] or r["new"]]
    if gone_new:
        fail(run, name, "children changed (restart): %s" % gone_new)
    if run.get("truncated"):
        fail(run, name, "the run hit SCENARIO_BUDGET (%s s) after %d of %d datagrams" % (
            SCENARIO_BUDGET, run["sent"], run["n"]))


def check_served(runs, name):
    run = get(runs, name)
    warm = run["warmup"]
    if run["valid_mode"] != "served":
        from dvtest import fuzz_run
        message = "the valid client was not served before any fuzz (%s; heard %s of %s, probe %s)" % (
            warm.get("reason") or "round not ok", warm.get("heard"), warm.get("expected"), warm.get("probe"))
        if name in fuzz_run.OPTIONAL:
            pytest.skip("%s: liveness only (UNVERIFIED default config of this protocol, docs/fuzz-cases.md)" % message)
        fail(run, name, message)
    bad = [r for r in run["rounds"] if not r["ok"]]
    if bad:
        fail(run, name, "valid client not served after %d datagrams: %s" % (bad[0]["sent"], bad[0]))
    assert run["rounds"], "no valid round ran"


def check_memory(runs, name):
    run = get(runs, name)
    if run.get("rss0_kib") is None or run.get("rss1_kib") is None:
        fail(run, name, "no VmRSS for the edge %s (children: %s)" % (
            SCENARIOS[name]["edges"], sorted({p["comm"] for p in run["procs0"]})))
    grown = run["rss1_kib"] - run["rss0_kib"]
    if grown > RSS_GROWTH_MAX_KIB:
        fail(run, name, "VmRSS of %s grew by %d KiB (%d -> %d), bound %d KiB" % (
            run["edge"], grown, run["rss0_kib"], run["rss1_kib"], RSS_GROWTH_MAX_KIB))


def check_counters(runs, name):
    run = get(runs, name)
    if run["metrics1"] and run["metrics1"][0].startswith("error:"):
        fail(run, name, "no hub metrics at %s: %s" % (HUB_METRICS, run["metrics1"][0]))
    if not run["metrics1"]:
        fail(run, name, "the hub answered at %s but none of dv_edge_drops_total, dv_pkt_reject_total, "
                        "dv_edge_queue_drops_total, dv_hub_dropped_total is on it after %d s" % (HUB_METRICS, DROPS_WAIT))
    if not run["errors1"] > run["errors0"]:
        fail(run, name, "no drop / reject counter moved (%s -> %s, dv_edge_drops_total of %s + dv_pkt_reject_total) "
                        "after %d malformed datagrams" % (run["errors0"], run["errors1"],
                                                          SCENARIOS[name]["edges"], run["sent"]))


CHECKS = {"edge_survives": check_survives, "valid_client_served": check_served, "memory_bounded": check_memory,
          "counters_move": check_counters}


def _define(name, check, fn):
    def case(runs):
        fn(runs, name)
    case.__name__ = case.__qualname__ = "test_fuzz_%s_%s" % (name, check)
    case.__doc__ = ("FUZZ-%s-%s: %s" % (name, check, (fn.__doc__ or fn.__name__)))
    globals()[case.__name__] = case


for _name in PROTOCOL_NAMES:
    for _check, _fn in CHECKS.items():
        _define(_name, _check, _fn)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--inside":
    _inside(json.loads(sys.argv[2]), sys.argv[3])
