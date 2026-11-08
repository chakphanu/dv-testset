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
  4. repeats the valid round after every 200 datagrams (first half of the datagrams with the
     fuzz client on another module than the valid clients; second half on the same module, with
     a wait of the stream timeout + 1 s before each round, the floor rule), and checks that no
     process of the launcher's tree is gone or new,
  5. reads the hub counters again.

Four cases per protocol read that one run:

  test_fuzz_<p>_edge_survives        launcher alive, no child gone or restarted
  test_fuzz_<p>_valid_client_served  every valid round: link ack, over heard
  test_fuzz_<p>_memory_bounded       VmRSS of the edge grew by at most RSS_GROWTH_MAX_KIB
  test_fuzz_<p>_counters_move        RECORD + FINDING of what the hub counted (no counter of malformed input yet)

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
import warnings
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
METRICS_MIN = 4.0                 # s after the last datagram before the hub is read for the end dump
METRICS_SETTLE = 12.0             # s at most for the hub totals to stop changing (the controllers publish every 10 s)
FLOOR_RETRIES = 3                 # extra valid rounds, 2 s apart, to measure how long a module stayed busy after a failure
EXPECTED_MALFORMED = ("dv_edge_drops_total", "malformed")   # (family, reason) DV-Router OBS1 will add (open); the proc label is the edge
# edges the xlxd files cannot switch on: merged into the converted dv-router.toml by scripts/dvr-import-wrap.sh
# (DVT_DVR_EXTRA_TOML), the keys of rig/config/dv-router/soak-edges.toml; g3 is the plain switch (UNVERIFIED keys)
EXTRA_TOML = {"nxdn": '[edges.nxdn]\nenable = true\ntg_modules = "31672:F"\n',
              "p25": '[edges.p25]\nenable = true\nserver_module = "G"\nserver_tg = 31672\n',
              "g3": '[edges.g3]\nenable = true\n'}
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
    import itertools
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
    env = dict(os.environ, DV_RUN_DIR=str(run_dir))
    if spec.get("extra_toml"):
        (run_dir / "dv-extra.toml").write_text(spec["extra_toml"])
        env["DVT_DVR_EXTRA_TOML"] = str(run_dir / "dv-extra.toml")
    xlxd = subprocess.Popen([spec["bin"], "XLX999", XLXD_IP, AMBED_IP], cwd=run_dir, env=env,
                            stdout=log, stderr=subprocess.STDOUT, preexec_fn=die_with_parent)
    result = {"scenario": spec["name"], "group": group, "seed": spec["seed"], "n": spec["n"], "rounds": [],
              "ports": {}, "errors": 0, "sent": 0}
    sources, valid = [], []
    stream_ids = itertools.count(0x2000)            # a DExtra header with the last outbound stream id is dropped

    def scrape():
        """(text of the whole hub scrape, None) or (None, error text)."""
        try:
            with urllib.request.urlopen(HUB_METRICS, timeout=2) as reply:
                return reply.read().decode("utf-8", "replace"), None
        except Exception as exc:                # a missing hub is a finding, not a crash
            return None, "error: %s" % exc

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
                fuzz_run.drive(valid_protocol, talker, frames, next(stream_ids))
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
            text0, err0 = scrape()
            if text0:
                (run_dir / "metrics-start.txt").write_text(text0)

            # ---- the fuzz sources, one set per phase
            stranger = fuzz_run.Source(STRANGER_IP)
            sources.append(stranger)

            def open_linked(module):
                """(linked source, how, objects to close) for a fuzz client on `module`."""
                if valid_protocol == "usrp":
                    return fuzz_run.Source(USRP_IP, sock=dict(valid)["peer"]), "usrp peer socket", []
                if valid_protocol in fuzz_run.CLIENT_KINDS:
                    fz = fuzz_run.make_client(valid_protocol, FUZZ_IP, "fuzz", module=module, nxdn_tg=spec["nxdn_tg"],
                                              p25_tg=spec["p25_tg"])
                    if fuzz_run.link(valid_protocol, fz, XLXD_IP):
                        return fuzz_run.ClientSource(fz), "linked client on module %s" % module, [fz]
                    return fuzz_run.Source(FUZZ_IP), "plain socket (the fuzz client link was not acknowledged)", [fz]
                if valid_protocol in fuzz_run.FIXED_PORT:
                    try:
                        return (fuzz_run.Source(FUZZ_IP, fuzz_run.FIXED_PORT[valid_protocol]), "fixed source port", [])
                    except OSError:
                        pass
                return fuzz_run.Source(FUZZ_IP), "plain socket", []

            replay = spec.get("replay")
            total = len(replay) if replay else spec["n"]
            phases = fuzz_run.plan_phases(total, None if replay else fuzz.OTHER_MODULE.get(valid_protocol))
            ring = fuzz_run.Ring()
            result["phases"] = []
            sent = errors = case_start = 0
            stop = False
            for ph in phases:
                linked, how, owned = open_linked(ph["module"])
                sources.append(linked)
                if replay:
                    fuzzer = fuzz_run.Replayer(replay, XLXD_IP, linked, stranger, gap=spec["gap"], ring=ring)
                else:
                    fuzzer = fuzz_run.Fuzzer(spec["name"], group, XLXD_IP, spec["seed"], linked, stranger,
                                             gap=spec["gap"], ring=ring, module=ph["module"], start=case_start)
                result["phases"].append(dict(ph, linked_source=how, sent_before=sent))
                done = 0
                while done < ph["count"] and not stop:      # a valid round every EVERY datagrams
                    done += fuzzer.batch(min(EVERY, ph["count"] - done))
                    quiet_from = time.monotonic()
                    if ph["quiet"]:                         # floor rule: the module is idle after the stream timeout
                        time.sleep(fuzz_run.QUIET_S)
                    row = valid_round("%s-%d" % (ph["name"], sent + done), kind)
                    row.update(phase=ph["name"], fuzz_module=ph["module"], quiet_s=fuzz_run.QUIET_S if ph["quiet"] else 0.0,
                               sent=sent + done)
                    if not row["ok"] and result["valid_mode"] == "served":
                        # how long does the module stay busy? further valid rounds, 2 s apart (recorded, still a failure)
                        row["recovered_after_s"] = None
                        for _retry in range(FLOOR_RETRIES):
                            time.sleep(2.0)
                            again = valid_round("%s-%d-retry" % (ph["name"], sent + done), kind)
                            if again["ok"]:
                                row["recovered_after_s"] = round(time.monotonic() - quiet_from, 1)
                                break
                    procs = fuzz_run.snapshot(xlxd.pid)
                    gone, new = fuzz_run.compare_processes(procs0, procs)
                    row.update(gone=[p["comm"] for p in gone], new=[p["comm"] for p in new],
                               launcher_alive=xlxd.poll() is None)
                    result["rounds"].append(row)
                    served = result["valid_mode"] != "served" or row["ok"]
                    if gone or new or not row["launcher_alive"] or not served:
                        result["failure"] = {"at": row["tag"], "sent": sent + done, "row": row,
                                             "last50": ring.entries()}
                        stop = True
                    elif time.monotonic() - started > SCENARIO_BUDGET:
                        result["truncated"] = True
                        stop = True
                sent += fuzzer.sent
                errors += fuzzer.errors
                case_start = getattr(fuzzer, "case_index", 0)
                result["phases"][-1]["sent"] = fuzzer.sent
                if stop:
                    break
                for obj in owned:
                    try:
                        obj.close()
                    except (OSError, AttributeError):
                        pass
                if valid_protocol != "usrp":
                    linked.close()
                    sources.remove(linked)
            result["sent"], result["errors"] = sent, errors + stranger.errors
            result["last50"] = ring.entries()
            procs1 = fuzz_run.snapshot(xlxd.pid)
            result["procs1"] = procs1
            edge1 = next((p for p in procs1 if edge0 and p["pid"] == edge0["pid"] and p["start"] == edge0["start"]), None)
            result["rss0_kib"] = edge0["rss_kib"] if edge0 else None
            result["rss1_kib"] = edge1["rss_kib"] if edge1 else None

            # ---- the hub again: wait until its _total series stop changing, dump the whole text, list what changed
            begin, previous, text1, err1 = time.monotonic(), None, None, None
            while True:
                text1, err1 = scrape()
                totals = ({k: v for k, v in fuzz_run.sample_map(text1).items() if k[0].endswith("_total")}
                          if text1 else None)
                if (totals is not None and totals == previous and time.monotonic() - begin >= METRICS_MIN) \
                        or time.monotonic() - begin >= METRICS_SETTLE:
                    break
                previous = totals
                time.sleep(2.0)
            changed = fuzz_run.metric_changes(text0, text1) if text0 and text1 else []
            if text1:
                (run_dir / "metrics-end.txt").write_text(text1)
            result["metrics"] = {"error": err0 or err1, "edge_proc": fuzz_run.edge_label(result["edge"] or ""),
                                 "start_dumped": bool(text0), "end_dumped": bool(text1),
                                 "families": sorted({k[0] for k in fuzz_run.sample_map(text1)}) if text1 else [],
                                 "changed": [[n, lab, a, b] for n, lab, a, b in changed][:800]}
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
        if name in EXTRA_TOML and os.environ.get("DVT_FUZZ_ENABLE_EDGES", "1") != "0":
            spec["extra_toml"] = EXTRA_TOML[name]
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


def quiet_text(row):
    """What the failure text says about the floor: variant, how long the module was quiet, how long it stayed busy."""
    if row.get("phase") == "other":
        return ("the fuzz client was on module %s and the valid clients on B, so no floor hold of the fuzz client "
                "can explain it" % row.get("fuzz_module"))
    quiet = row.get("quiet_s") or 0.0
    text = "the fuzz client was on the valid clients' module B; the module had been quiet for %.1f s " \
           "(stream timeout %.1f s + %.1f s margin) before the round" % (
               quiet, fuzz_run_mod().STREAM_TIMEOUT_S, fuzz_run_mod().QUIET_MARGIN_S)
    if "recovered_after_s" in row:
        text += "; %s" % ("served again %.1f s after the last fuzz datagram" % row["recovered_after_s"]
                          if row["recovered_after_s"] is not None else "still not served 6 s later")
    return text


def fuzz_run_mod():
    from dvtest import fuzz_run
    return fuzz_run


def check_served(runs, name):
    run = get(runs, name)
    warm = run["warmup"]
    if run["valid_mode"] != "served":
        message = "the valid client was not served before any fuzz (%s; heard %s of %s, probe %s)" % (
            warm.get("reason") or "round not ok", warm.get("heard"), warm.get("expected"), warm.get("probe"))
        if name in fuzz_run_mod().OPTIONAL:
            pytest.skip("%s: liveness only (UNVERIFIED default config of this protocol, docs/fuzz-cases.md)" % message)
        fail(run, name, message)
    bad = [r for r in run["rounds"] if not r["ok"]]
    if bad:
        row = bad[0]
        text = "valid client not served after %d datagrams (phase %s, heard %s of %s, link probe %s): %s" % (
            row["sent"], row.get("phase"), row["heard"], row["expected"], row["probe"], quiet_text(row))
        finding(run, name, "FINDING: " + text)
        fail(run, name, text)
    assert run["rounds"], "no valid round ran"


def finding(run, name, text):
    """A FINDING line: a pytest warning and a line in fuzz-findings.txt of the scenario's run directory."""
    warnings.warn(text, UserWarning, stacklevel=2)
    with open(Path(run["run_dir"]) / "fuzz-findings.txt", "a", encoding="utf-8") as handle:
        handle.write("[%s] %s\n" % (name, text))


def check_memory(runs, name):
    run = get(runs, name)
    if run.get("rss0_kib") is None or run.get("rss1_kib") is None:
        fail(run, name, "no VmRSS for the edge %s (children: %s)" % (
            SCENARIOS[name]["edges"], sorted({p["comm"] for p in run["procs0"]})))
    grown = run["rss1_kib"] - run["rss0_kib"]
    if grown > RSS_GROWTH_MAX_KIB:
        fail(run, name, "VmRSS of %s grew by %d KiB (%d -> %d), bound %d KiB" % (
            run["edge"], grown, run["rss0_kib"], run["rss1_kib"], RSS_GROWTH_MAX_KIB))


def assert_malformed():
    """True once DV-Router OBS1 has landed: DVT_FUZZ_ASSERT_MALFORMED=1 makes counters_move assert
    EXPECTED_MALFORMED > 0 for the edge instead of recording a finding."""
    return os.environ.get("DVT_FUZZ_ASSERT_MALFORMED", "") == "1"


def check_counters(runs, name):
    """Record which hub series moved while the malformed datagrams were sent (counters.json, metrics-start.txt
    and metrics-end.txt are the whole scrapes). No counter of malformed input exists yet (OBS1 open), so
    this is a RECORD and a FINDING, not a failure; with DVT_FUZZ_ASSERT_MALFORMED=1 the counter
    dv_edge_drops_total{proc=<edge>,reason="malformed"} must have grown."""
    from dvtest import fuzz_run
    run = get(runs, name)
    m = run["metrics"]
    if m["error"] or not (m["start_dumped"] and m["end_dumped"]):
        fail(run, name, "no hub scrape at %s (start dumped %s, end dumped %s): %s" % (
            HUB_METRICS, m["start_dumped"], m["end_dumped"], m["error"]))
    changes = [tuple(c) for c in m["changed"]]
    edge, family, reason = m["edge_proc"], EXPECTED_MALFORMED[0], EXPECTED_MALFORMED[1]
    by_reason = {}
    for n, lab, a, b in changes:
        if n == "dv_edge_drops_total" and lab.get("proc") == edge:
            by_reason[lab.get("reason", "-")] = by_reason.get(lab.get("reason", "-"), 0) + (b - a)
    reject = {}
    for n, lab, a, b in changes:
        if n == "dv_pkt_reject_total":
            reject[lab.get("reason", "-")] = reject.get(lab.get("reason", "-"), 0) + (b - a)
    malformed = fuzz_run.family_delta(changes, family, proc=edge, reason=reason)
    record = {"protocol": name, "edge_proc": edge, "datagrams": run["sent"], "expected": "%s{reason=%s}" % (family, reason),
              "expected_delta": malformed, "edge_drops_by_reason": by_reason, "pkt_reject_by_reason": reject,
              "changed_series": len(changes), "changed_families": sorted({c[0] for c in changes}),
              "families_exposed": m["families"]}
    (Path(run["run_dir"]) / "counters.json").write_text(json.dumps(record, indent=1))
    if assert_malformed():
        if not malformed > 0:
            fail(run, name, "%s{proc=%s,reason=%s} did not grow after %d malformed datagrams (edge drops moved: %s)" % (
                family, edge, reason, run["sent"], by_reason))
        return
    if not malformed > 0:
        finding(run, name, "FINDING: no counter of malformed input moved (OBS1 open): %s edge %s, %d malformed datagrams; "
                           "dv_edge_drops_total{proc=%s} moved by reason %s, dv_pkt_reject_total by %s, %d series changed "
                           "in all (counters.json)" % (name, edge, run["sent"], edge, by_reason, reject, len(changes)))


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
