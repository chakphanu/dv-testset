# SPDX-License-Identifier: MIT
"""dvtest/control.py (tester side of the run.sh control channel) and
scripts/lib/killproc.sh (host side allow-list of kill-proc)."""
import itertools
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dvtest import control

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "killproc.sh"
METRICS_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "metrics.sh"

SERVICES = ["xlxd-sut", "xlxd-ref", "tester", "transcoder-sut", "allstar", "XLXD-SUT", "", "xlxd-sut ", "*"]
PROCESSES = ["dv-edge-xlx", "dv-edge-nxdn", "dv-edge-p25", "dv-router", "dv-codec-ambed", "dv-codec", "dv-codec-ambed2", "dv-codec-*", "dv-edge-dextra", "dv-edge-*", "dv-edge",
             "dv-router2", "DV-ROUTER", "xlxd", "sh", "", "dv-edge-xlx;id", "../dv-router", "-1", "1"]
SIGNALS = ["KILL", "USR1", "TERM", "SIGKILL", "kill", "9", "HUP", "USR2", "INT", "STOP", "", "-9", "KILL;id"]


def bash(script, *args, lib=LIB):
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    return subprocess.run(["bash", "-c", 'source "$1"; shift; ' + script, "x", str(lib), *args],
                          capture_output=True, text=True, timeout=30)


# ---- the allow-list: python table, bash function, and that they agree

def test_python_table():
    assert control.kill_proc_allowed("xlxd-sut", "dv-edge-xlx", "KILL")
    assert control.kill_proc_allowed("xlxd-sut", "dv-edge-nxdn", "KILL")
    assert control.kill_proc_allowed("xlxd-sut", "dv-edge-p25", "KILL")
    for edge in ("dv-edge-xlx", "dv-edge-nxdn", "dv-edge-p25"):
        assert control.kill_proc_allowed("xlxd-sut", edge, "USR1")
    for sig in ("KILL", "USR1", "TERM", "HUP", "INT", "STOP"):
        assert not control.kill_proc_allowed("xlxd-sut", "dv-router", sig)      # the launcher is never signalled
    assert not control.kill_proc_allowed("xlxd-sut", "dv-edge-xlx", "TERM")
    assert not control.kill_proc_allowed("xlxd-ref", "dv-edge-xlx", "KILL")
    # the codec worker: KILL only (never the launcher)
    assert control.kill_proc_allowed("xlxd-sut", "dv-codec-ambed", "KILL")
    for sig in ("USR1", "TERM", "HUP", "INT", "STOP"):
        assert not control.kill_proc_allowed("xlxd-sut", "dv-codec-ambed", sig)
    assert not control.kill_proc_allowed("xlxd-ref", "dv-codec-ambed", "KILL")
    assert sum(len(v) for v in control.KILL_PROC_ALLOWED.values()) == 7
    assert not any(proc == "dv-router" for _svc, proc in control.KILL_PROC_ALLOWED)


def test_bash_and_python_agree_on_every_combination():
    combos = list(itertools.product(SERVICES, PROCESSES, SIGNALS))
    # one bash process for all combinations: "<verdict> <index>" per line
    script = ('i=0; while IFS=$\'\\t\' read -r s p g; do '
              'if dvt_killproc_allowed "$s" "$p" "$g"; then echo "1 $i"; else echo "0 $i"; fi; i=$((i+1)); done')
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    feed = "".join("%s\t%s\t%s\n" % c for c in combos)
    run = subprocess.run(["bash", "-c", 'source "$1"; ' + script, "x", str(LIB)], input=feed,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    verdicts = [line.split() for line in run.stdout.splitlines()]
    assert len(verdicts) == len(combos)
    allowed_bash = {combos[int(i)] for v, i in verdicts if v == "1"}
    allowed_py = {c for c in combos if control.kill_proc_allowed(*c)}
    assert allowed_bash == allowed_py
    assert len(allowed_py) == 7
    assert not any(p == "dv-router" for _s, p, _g in allowed_bash)


def test_bash_allowed_needs_exactly_three_words():
    assert bash('dvt_killproc_allowed xlxd-sut dv-edge-p25 USR1').returncode == 0
    assert bash('dvt_killproc_allowed xlxd-sut dv-edge-p25').returncode == 1
    assert bash('dvt_killproc_allowed xlxd-sut dv-edge-p25 USR1 extra').returncode == 1
    assert bash('dvt_killproc_allowed xlxd-sut dv-router USR1').returncode == 1
    assert bash('dvt_killproc_allowed').returncode == 1


# ---- request parsing on the host

@pytest.mark.parametrize("line,out", [
    ("kill-proc xlxd-sut dv-edge-xlx KILL", "xlxd-sut dv-edge-xlx KILL"),
    ("kill-proc  xlxd-sut\tdv-edge-nxdn   KILL  ", "xlxd-sut dv-edge-nxdn KILL"),
    ("kill-proc xlxd-sut dv-edge-p25 KILL", "xlxd-sut dv-edge-p25 KILL"),
    ("kill-proc xlxd-sut dv-edge-xlx USR1", "xlxd-sut dv-edge-xlx USR1"),
    ("kill-proc xlxd-sut dv-edge-nxdn USR1", "xlxd-sut dv-edge-nxdn USR1"),
    ("kill-proc xlxd-sut dv-edge-p25 USR1", "xlxd-sut dv-edge-p25 USR1"),
    ("kill-proc xlxd-sut dv-codec-ambed KILL", "xlxd-sut dv-codec-ambed KILL"),
])
def test_parse_accepts(line, out):
    run = bash('dvt_killproc_parse "$1"', line)
    assert run.returncode == 0 and run.stdout == out + "\n", run.stderr


@pytest.mark.parametrize("line", [
    "", "kill-proc", "kill-proc xlxd-sut", "kill-proc xlxd-sut dv-router",
    "kill-proc xlxd-sut dv-router KILL",              # the launcher is never signalled at all
    "kill-proc xlxd-sut dv-router USR1",
    "kill-proc xlxd-sut dv-router TERM",
    "kill-proc xlxd-sut dv-codec-ambed TERM",
    "kill-proc xlxd-sut dv-codec-ambed USR1",
    "kill-proc xlxd-ref dv-codec-ambed KILL",
    "kill-proc xlxd-sut dv-codec KILL",
    "kill-proc xlxd-sut dv-edge-xlx TERM",
    "kill-proc xlxd-sut dv-edge-xlx USR2",
    "kill-proc xlxd-ref dv-edge-xlx USR1",
    "kill-proc xlxd-ref dv-edge-xlx KILL",
    "kill-proc xlxd-sut dv-edge-* KILL",
    "kill-proc xlxd-sut dv-edge KILL",
    "kill-proc xlxd-sut dv-edge-dextra KILL",
    "kill-proc xlxd-sut xlxd KILL",
    "kill-proc xlxd-sut dv-edge-xlx KILL extra",
    "kill-proc xlxd-sut dv-edge-xlx;id KILL",
    "kill-proc xlxd-sut dv-edge-xlx SIGKILL",
    "kill-proc xlxd-sut dv-edge-xlx 9",
    "kill xlxd-sut dv-edge-xlx KILL",
    "KILL-PROC xlxd-sut dv-edge-xlx KILL",
    "stop xlxd-sut",
])
def test_parse_refuses(line):
    run = bash('dvt_killproc_parse "$1"', line)
    assert run.returncode == 1 and run.stdout == ""


def test_exec_refuses_odd_names_before_docker():
    # even if the caller forgot the allow-list the helper takes only plain names
    for proc, sig in (("a b", "KILL"), ("dv-edge-*", "KILL"), ("x;y", "KILL"), ("dv-router", "-9"), ("dv-router", "")):
        run = bash('dvt_killproc_exec cid img "$1" "$2"', proc, sig)
        assert run.returncode == 1, (proc, sig)


# ---- the script that runs in the helper container: exact comm only

def named_sleeper(name):
    """A python process whose /proc comm is `name` (prctl PR_SET_NAME)."""
    code = "import ctypes, sys, time; ctypes.CDLL(None).prctl(15, sys.argv[1].encode()); time.sleep(30)"
    return subprocess.Popen([sys.executable, "-c", code, name])


def wait_comm(proc, name):
    for _ in range(100):
        try:
            if Path("/proc/%d/comm" % proc.pid).read_text().strip() == name:
                return
        except OSError:
            pass
        time.sleep(0.05)
    pytest.skip("cannot set the process name with prctl here")


def test_helper_script_signals_exact_comm_only():
    """The container script on the real /proc: two processes called
    dvt-victim are signalled, one called dvt-victim2, a longer and a shorter
    name are not (no docker; the script is plain sh)."""
    if not (shutil.which("sh") and shutil.which("date") and Path("/proc/self/comm").exists()):
        pytest.skip("sh, date or /proc missing")
    names = ("dvt-victim", "dvt-victim2", "dvt-victim", "dvt-victi", "xdvt-victim")
    procs = [named_sleeper(n) for n in names]
    try:
        for proc, name in zip(procs, names):
            wait_comm(proc, name)
        script = bash('printf "%s" "$DVT_KILLPROC_SCRIPT"').stdout
        assert "kill -s" in script
        run = subprocess.run(["sh", "-c", script, "dvt-killproc", "TERM", "dvt-victim"],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        lines = run.stdout.splitlines()
        pids = sorted(int(x.split("=")[1]) for x in lines if x.startswith("pid="))
        assert pids == sorted([procs[0].pid, procs[2].pid])
        assert lines[-1].startswith("t_kill=") and abs(float(lines[-1].split("=")[1]) - time.time()) < 5
        for index in (0, 2):
            procs[index].wait(5)
        assert [procs[i].poll() for i in (1, 3, 4)] == [None, None, None]
        # a zombie keeps its comm until reaped: it is not signalled again
        zombie = named_sleeper("dvt-victim")
        procs.append(zombie)
        wait_comm(zombie, "dvt-victim")
        os.kill(zombie.pid, signal.SIGKILL)
        for _ in range(100):
            if "Z" in Path("/proc/%d/status" % zombie.pid).read_text().split("State:")[1].split()[0]:
                break
            time.sleep(0.05)
        run = subprocess.run(["sh", "-c", script, "dvt-killproc", "TERM", "dvt-victim"],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 3 and "pid=" not in run.stdout, run.stdout
        run = subprocess.run(["sh", "-c", script, "dvt-killproc", "TERM", "dvt-nothing-like-this"],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 3 and "none=dvt-nothing-like-this" in run.stdout
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


# ---- tester side: request line, reply parsing, the channel

def test_kill_proc_line():
    assert control.kill_proc_line("xlxd-sut", "dv-edge-xlx", "KILL") == "kill-proc xlxd-sut dv-edge-xlx KILL"
    for bad in (("xlxd-sut", "dv-router", "KILL"), ("xlxd-ref", "dv-edge-xlx", "USR1"), ("xlxd-sut", "dv-router", "USR1"), ("xlxd-sut", "xlxd", "KILL")):
        with pytest.raises(ValueError):
            control.kill_proc_line(*bad)


def test_parse_kill_out():
    got = control.parse_kill_out("pid=7\npid=9\nt_kill=1761900000.123456789\n")
    assert got.pids == [7, 9] and got.t_kill == pytest.approx(1761900000.123457)
    for bad in ("", "t_kill=1.5\n", "pid=3\n", "pid=x\nt_kill=1\n", "pid=3\nt_kill=abc\n", "none=dv-router\n"):
        with pytest.raises(ValueError):
            control.parse_kill_out(bad)


def test_t_kill_monotonic_scale():
    result = control.KillResult(pids=[1], t_kill=time.time() - 2.0)
    assert time.monotonic() - result.t_kill_monotonic == pytest.approx(2.0, abs=0.05)


def watcher(directory, reply, out=None, stop=None):
    """A fake host watcher: answers each request with `reply` (and an .out)."""
    seen = []

    def run():
        end = time.monotonic() + 10
        while time.monotonic() < end and not (stop and stop.is_set()):
            for req in sorted(directory.glob("*.req")):
                name = req.stem
                seen.append(req.read_text().strip())
                if out is not None:
                    (directory / (name + ".out")).write_text(out)
                (directory / (name + ".ack")).write_text(reply + "\n")
                req.rename(directory / (name + ".done"))
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return seen, thread


def test_rig_control_plain_action(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    seen, thread = watcher(ctl.dir, "ok")
    ctl("restart", "xlxd-sut", timeout=5)
    thread.join(5)
    assert seen == ["restart xlxd-sut"]


def test_rig_control_errors(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    seen, thread = watcher(ctl.dir, "error refused")
    with pytest.raises(RuntimeError, match="error refused"):
        ctl("explode", "xlxd-sut", timeout=5)
    thread.join(5)
    with pytest.raises(TimeoutError):
        ctl("stop", "xlxd-sut", timeout=0.3)


def test_rig_control_kill_proc(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    seen, thread = watcher(ctl.dir, "ok", out="pid=12\nt_kill=%f\n" % time.time())
    got = ctl.kill_proc("xlxd-sut", "dv-edge-xlx", "KILL", timeout=5)
    thread.join(5)
    assert seen == ["kill-proc xlxd-sut dv-edge-xlx KILL"] and got.pids == [12]


def test_rig_control_kill_proc_not_allowed_sends_nothing(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    with pytest.raises(ValueError):
        ctl.kill_proc("xlxd-sut", "dv-router", "KILL")
    with pytest.raises(ValueError):
        ctl.kill_proc("xlxd-ref", "dv-edge-xlx", "KILL")
    assert not list(ctl.dir.iterdir())


def test_rig_control_kill_proc_no_process(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    seen, thread = watcher(ctl.dir, "error 3", out="t_kill=1.0\nnone=dv-edge-p25\n")
    with pytest.raises(RuntimeError, match="error 3"):
        ctl.kill_proc("xlxd-sut", "dv-edge-p25", "KILL", timeout=5)
    thread.join(5)


def test_mark_request_line(tmp_path):
    """RigControl.mark sends "mark begin|end <tag>" and refuses odd tags."""
    rc = control.RigControl(tmp_path)

    def watcher():
        while not list(tmp_path.glob("*.req")):
            time.sleep(0.01)
        req = next(tmp_path.glob("*.req"))
        lines.append(req.read_text().strip())
        req.with_suffix(".ack").write_text("ok\n")

    lines = []
    thread = threading.Thread(target=watcher)
    thread.start()
    rc.mark("begin", "ambed-fault-data-delay-100")
    thread.join()
    assert lines == ["mark begin ambed-fault-data-delay-100"]
    for phase, tag in (("start", "x"), ("begin", ""), ("begin", "a b"), ("end", "a;b"), ("end", "x\ny")):
        with pytest.raises(ValueError):
            rc.mark(phase, tag)


def test_request_names_are_unique_across_instances(tmp_path):
    """Two RigControl objects in one process (a fixture and a helper) must never
    issue the same request name: a clash makes one request overwrite the .ack or
    .out of the other (seen as a missing .out in the T14 caps-changed case)."""
    a = control.RigControl(tmp_path)
    b = control.RigControl(tmp_path)
    names = []

    def answer():
        deadline = time.monotonic() + 5
        while len(names) < 6 and time.monotonic() < deadline:
            for req in sorted(tmp_path.glob("*.req")):
                names.append(req.stem)
                req.rename(req.with_suffix(".done"))
                req.with_suffix(".ack").write_text("ok\n")
            time.sleep(0.005)

    thread = threading.Thread(target=answer)
    thread.start()
    for ctl in (a, b, a, b, a, b):
        ctl.mark("begin", "t")
    thread.join(10)
    assert len(names) == 6 and len(set(names)) == 6, names


# ---- metrics: "metrics xlxd-sut" (scripts/lib/metrics.sh), python table and bash agree

METRICS_SERVICES = ["xlxd-sut", "xlxd-ref", "tester", "transcoder-sut", "allstar", "XLXD-SUT", "", "xlxd-sut ", "*", "xlxd-sut;id",
                    "container:abc", "http://127.0.0.1:19108/metrics"]


def test_metrics_python_table():
    assert control.METRICS_ALLOWED == ("xlxd-sut",)
    assert control.metrics_allowed("xlxd-sut")
    for service in METRICS_SERVICES[1:]:
        assert not control.metrics_allowed(service), service
    assert control.metrics_line("xlxd-sut") == "metrics xlxd-sut"
    for service in ("xlxd-ref", "tester", "", "xlxd-sut extra"):
        with pytest.raises(ValueError):
            control.metrics_line(service)


def test_metrics_bash_and_python_agree():
    feed = "".join("%s\n" % s for s in METRICS_SERVICES if "\n" not in s)
    script = 'while IFS= read -r s; do if dvt_metrics_allowed "$s"; then echo "1 $s"; else echo "0 $s"; fi; done'
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    run = subprocess.run(["bash", "-c", 'source "$1"; ' + script, "x", str(METRICS_LIB)], input=feed,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    allowed_bash = {line[2:] for line in run.stdout.splitlines() if line.startswith("1 ")}
    allowed_py = {s for s in METRICS_SERVICES if control.metrics_allowed(s)}
    assert allowed_bash == allowed_py == {"xlxd-sut"}


def test_metrics_bash_allowed_takes_exactly_one_word():
    assert bash('dvt_metrics_allowed xlxd-sut', lib=METRICS_LIB).returncode == 0
    assert bash('dvt_metrics_allowed xlxd-sut extra', lib=METRICS_LIB).returncode == 1
    assert bash('dvt_metrics_allowed', lib=METRICS_LIB).returncode == 1
    assert bash('dvt_metrics_allowed xlxd-ref', lib=METRICS_LIB).returncode == 1


@pytest.mark.parametrize("line,out", [
    ("metrics xlxd-sut", "xlxd-sut"),
    ("metrics  xlxd-sut\t ", "xlxd-sut"),
])
def test_metrics_parse_accepts(line, out):
    run = bash('dvt_metrics_parse "$1"', line, lib=METRICS_LIB)
    assert run.returncode == 0 and run.stdout == out + "\n", run.stderr


@pytest.mark.parametrize("line", [
    "", "metrics", "metrics xlxd-ref", "metrics tester", "metrics xlxd-sut extra",
    "metrics xlxd-sut http://127.0.0.1:19108/metrics", "metrics xlxd-sut;id", "METRICS xlxd-sut",
    "metric xlxd-sut", "kill-proc xlxd-sut dv-edge-xlx KILL", "stop xlxd-sut", "metrics *",
])
def test_metrics_parse_refuses(line):
    run = bash('dvt_metrics_parse "$1"', line, lib=METRICS_LIB)
    assert run.returncode == 1 and run.stdout == ""


def test_metrics_helper_fetches_one_fixed_url():
    """The helper script has the URL as argv only; the allow-list function and
    the exec wrapper pass the fixed constant, never request data."""
    script = bash('printf "%s" "$DVT_METRICS_URL"', lib=METRICS_LIB).stdout
    assert script == "http://127.0.0.1:19108/metrics"
    body = bash('printf "%s" "$DVT_METRICS_SCRIPT"', lib=METRICS_LIB).stdout
    assert "urlopen" not in body and "opener.open(sys.argv[1], timeout=5)" in body
    assert "ProxyHandler({})" in body


def test_metrics_helper_script_reads_a_local_server():
    """The python of the helper container, run against a local HTTP server."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802
            body = b"dv_up 1\n" if self.path == "/metrics" else b"nope"
            self.send_response(200 if self.path == "/metrics" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = bash('printf "%s" "$DVT_METRICS_SCRIPT"', lib=METRICS_LIB).stdout
        url = "http://127.0.0.1:%d/metrics" % server.server_address[1]
        run = subprocess.run([sys.executable, "-I", "-c", body, url], capture_output=True, text=True, timeout=30)
        assert run.returncode == 0 and run.stdout == "dv_up 1\n", run.stderr
        run = subprocess.run([sys.executable, "-I", "-c", body, url.replace("/metrics", "/other")],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode != 0 and run.stdout == ""
    finally:
        server.shutdown()
        server.server_close()


def test_rig_control_metrics(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    seen, thread = watcher(ctl.dir, "ok", out='dv_room_data_total{room="m",result="accepted"} 3\n')
    text = ctl.metrics("xlxd-sut", timeout=5)
    thread.join(5)
    assert seen == ["metrics xlxd-sut"] and "dv_room_data_total" in text


def test_rig_control_metrics_not_allowed_sends_nothing(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    for service in ("xlxd-ref", "tester", "xlxd-sut extra"):
        with pytest.raises(ValueError):
            ctl.metrics(service)
    assert not list(ctl.dir.iterdir())


def test_rig_control_metrics_failure(tmp_path):
    ctl = control.RigControl(tmp_path / "control")
    seen, thread = watcher(ctl.dir, "error 1", out="")
    with pytest.raises(RuntimeError, match="error 1"):
        ctl.metrics("xlxd-sut", timeout=5)
    thread.join(5)
