# SPDX-License-Identifier: MIT
"""scripts/lib/procstat.sh (host side of the read-only proc-stat control action) and dvtest/control.py."""
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dvtest import control
from dvtest import webload

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "procstat.sh"
SERVICES = ["xlxd-sut", "xlxd-ref", "tester", "XLXD-SUT", "", "*"]
PROCESSES = ["dv-edge-webrtc", "dv-svc-web", "dv-svc-status", "dv-router", "dv-edge-xlx", "dv-codec-ambed", "dv-edge-webrtc2",
             "dv-svc-admin", "dv-room", "dv-recorder", "dv-edge-nxdn", "dv-edge-p25", "dv-edge-usrp", "list", "LIST", "list ", "dv-roo",
             "dv-edge-*", "", "sh", "dv-edge-webrtc;id", "../x", "dv-router2", "DV-ROUTER"]


def bash(script, *args):
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    return subprocess.run(["bash", "-c", 'source "$1"; shift; ' + script, "x", str(LIB), *args],
                          capture_output=True, text=True, timeout=30)


def test_python_table():
    assert control.proc_stat_allowed("xlxd-sut", "dv-edge-webrtc")
    assert control.proc_stat_allowed("xlxd-sut", "dv-router")              # the launcher: read only (never in KILL_PROC_ALLOWED)
    assert not any(proc == "dv-router" for _svc, proc in control.KILL_PROC_ALLOWED)
    assert not control.proc_stat_allowed("xlxd-ref", "dv-edge-webrtc")
    assert control.proc_stat_line("xlxd-sut", "dv-svc-web") == "proc-stat xlxd-sut dv-svc-web"
    assert control.proc_stat_line("xlxd-sut", "list") == "proc-stat xlxd-sut list"
    with pytest.raises(ValueError):
        control.proc_stat_line("xlxd-sut", "dv-edge-dextra")
    with pytest.raises(ValueError):
        control.proc_stat_line("xlxd-ref", "list")
    # every name is a plain comm: at most 15 characters (the kernel truncates longer ones), the helper's pattern
    assert all(len(n) <= 15 and n.replace("-", "").isalnum() and n == n.lower() for n in control.PROC_STAT_NAMES)
    assert len(set(control.PROC_STAT_NAMES)) == len(control.PROC_STAT_NAMES)


def test_bash_and_python_agree_on_every_combination():
    combos = list(itertools.product(SERVICES, PROCESSES))
    script = ('i=0; while IFS=$\'\\t\' read -r s p; do '
              'if dvt_procstat_allowed "$s" "$p"; then echo "1 $i"; else echo "0 $i"; fi; i=$((i+1)); done')
    feed = "".join("%s\t%s\n" % c for c in combos)
    run = subprocess.run(["bash", "-c", 'source "$1"; ' + script, "x", str(LIB)], input=feed,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    allowed_bash = {combos[int(i)] for v, i in (line.split() for line in run.stdout.splitlines()) if v == "1"}
    assert allowed_bash == {c for c in combos if control.proc_stat_allowed(*c)}
    assert len(allowed_bash) == len(control.PROC_STAT_NAMES) + 1          # the names and "list", service xlxd-sut
    assert {p for _s, p in allowed_bash} == set(control.PROC_STAT_NAMES) | {"list"}


@pytest.mark.parametrize("line,out", [
    ("proc-stat xlxd-sut dv-edge-webrtc", "xlxd-sut dv-edge-webrtc"),
    ("proc-stat xlxd-sut dv-router", "xlxd-sut dv-router"),
    ("proc-stat xlxd-sut list", "xlxd-sut list"),
    ("proc-stat  xlxd-sut\tdv-svc-status  ", "xlxd-sut dv-svc-status"),
])
def test_parse_accepts(line, out):
    run = bash('dvt_procstat_parse "$1"', line)
    assert run.returncode == 0 and run.stdout == out + "\n", run.stderr


@pytest.mark.parametrize("line", ["", "proc-stat", "proc-stat xlxd-sut", "proc-stat xlxd-sut dv-router2", "proc-stat xlxd-ref dv-edge-webrtc",
                                  "proc-stat xlxd-ref list", "proc-stat xlxd-sut list extra", "proc-stat xlxd-sut dv-edge-dextra",
                                  "proc-stat xlxd-sut dv-edge-webrtc extra", "proc-stat xlxd-sut dv-edge-*", "kill-proc xlxd-sut dv-edge-webrtc",
                                  "PROC-STAT xlxd-sut dv-edge-webrtc", "proc-stat xlxd-sut dv-edge-webrtc;id"])
def test_parse_refuses(line):
    run = bash('dvt_procstat_parse "$1"', line)
    assert run.returncode == 1 and run.stdout == ""


def test_exec_refuses_odd_names_before_docker():
    for proc in ("a b", "dv-edge-*", "x;y", "", "A"):
        assert bash('dvt_procstat_exec cid img "$1"', proc).returncode == 1, proc


def test_helper_script_reads_exact_comm_only_and_parses():
    """The script of the helper container on the real /proc: a process called dvt-procvictim is reported, a longer
    and a shorter name are not, and the reply is what dvtest.webload reads (the clock tick is the kernel's)."""
    if not Path("/proc/self/stat").exists() or not shutil.which("bash"):
        pytest.skip("/proc or bash missing")
    code = "import ctypes, sys, time; ctypes.CDLL(None).prctl(15, sys.argv[1].encode()); x = bytearray(8 << 20); time.sleep(30)"
    names = ("dvt-procvictim", "dvt-procvictim2", "dvt-procvicti")
    procs = [subprocess.Popen([sys.executable, "-c", code, n]) for n in names]
    try:
        for p, n in zip(procs, names):
            for _ in range(100):
                try:
                    if Path("/proc/%d/comm" % p.pid).read_text().strip() == n:
                        break
                except OSError:
                    pass
                import time
                time.sleep(0.05)
            else:
                pytest.skip("cannot set the process name with prctl here")
        script = bash('printf "%s" "$DVT_PROCSTAT_SCRIPT"').stdout
        run = subprocess.run([sys.executable, "-c", script, "dvt-procvictim"], capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        sample = webload.parse_procstat_out(run.stdout)
        assert sample.pid == procs[0].pid and sample.comm == "dvt-procvictim"
        assert sample.clk_tck == os.sysconf("SC_CLK_TCK") and sample.vmhwm_kib >= 8 * 1024 and sample.vmrss_kib > 0
        assert sample.vmhwm_kib >= sample.vmrss_kib
        assert sample.pid == procs[0].pid
        assert sample.fds == len(os.listdir("/proc/%d/fd" % procs[0].pid)) and sample.fds >= 3
        assert sample.starttime == int(Path("/proc/%d/stat" % procs[0].pid).read_text().rsplit(")", 1)[1].split()[19]) > 0
        none = subprocess.run([sys.executable, "-c", script, "dvt-nosuchproc"], capture_output=True, text=True, timeout=30)
        assert none.returncode == 3 and none.stdout.strip() == "none=dvt-nosuchproc"
    finally:
        for p in procs:
            p.kill()
            p.wait(5)


def test_helper_script_list_mode_names_every_process_but_itself():
    if not Path("/proc/self/stat").exists() or not shutil.which("bash"):
        pytest.skip("/proc or bash missing")
    script = bash('printf "%s" "$DVT_PROCSTAT_SCRIPT"').stdout
    run = subprocess.run([sys.executable, "-c", script, "list"], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    rows = control.parse_proc_list(run.stdout)
    pids = {pid for pid, _ppid, _comm in rows}
    assert os.getpid() in pids                                    # this pytest process is listed; the helper itself is not
    assert all(line.startswith("proc=") for line in run.stdout.splitlines())
    assert len(rows) == len(run.stdout.splitlines())


def test_parse_proc_list_ignores_noise():
    text = "proc=1 0 dv-router\nproc=7 1 dv-edge-xlx\nproc=9 1 tokio runtime\nnoise\nproc=x 1 y\nproc=3 1\n"
    assert control.parse_proc_list(text) == [(1, 0, "dv-router"), (7, 1, "dv-edge-xlx"), (9, 1, "tokio runtime")]


@pytest.mark.parametrize("user,want", [("", "0:0"), ("1000:1000", "1000:1000"), ("65532", "65532:65532"),
                                       ("dvrouter", "1000:1000"), ("dv:dv", "1000:1000")])
def test_helper_runs_as_the_user_of_the_sut_container(user, want):
    """root when the image names no user, the numeric user otherwise, the host uid for a name the helper image cannot resolve."""
    run = subprocess.run(["bash", "-c", 'source "$1"; docker() { printf "%s\n" "$FAKE_USER"; }; DVT_UID=1000 DVT_GID=1000; dvt_procstat_user cid',
                          "x", str(LIB)], capture_output=True, text=True, timeout=30, env={**os.environ, "FAKE_USER": user})
    assert run.returncode == 0 and run.stdout == want + "\n", run.stderr
