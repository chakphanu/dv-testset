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
             "dv-edge-*", "", "sh", "dv-edge-webrtc;id", "../x"]


def bash(script, *args):
    if not shutil.which("bash"):
        pytest.skip("bash is not installed")
    return subprocess.run(["bash", "-c", 'source "$1"; shift; ' + script, "x", str(LIB), *args],
                          capture_output=True, text=True, timeout=30)


def test_python_table():
    assert control.proc_stat_allowed("xlxd-sut", "dv-edge-webrtc")
    assert not control.proc_stat_allowed("xlxd-sut", "dv-router")          # the launcher: not in the table
    assert not control.proc_stat_allowed("xlxd-ref", "dv-edge-webrtc")
    assert control.proc_stat_line("xlxd-sut", "dv-svc-web") == "proc-stat xlxd-sut dv-svc-web"
    with pytest.raises(ValueError):
        control.proc_stat_line("xlxd-sut", "dv-edge-xlx")


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
    assert len(allowed_bash) == 3


@pytest.mark.parametrize("line,out", [
    ("proc-stat xlxd-sut dv-edge-webrtc", "xlxd-sut dv-edge-webrtc"),
    ("proc-stat  xlxd-sut\tdv-svc-status  ", "xlxd-sut dv-svc-status"),
])
def test_parse_accepts(line, out):
    run = bash('dvt_procstat_parse "$1"', line)
    assert run.returncode == 0 and run.stdout == out + "\n", run.stderr


@pytest.mark.parametrize("line", ["", "proc-stat", "proc-stat xlxd-sut", "proc-stat xlxd-sut dv-router", "proc-stat xlxd-ref dv-edge-webrtc",
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
        none = subprocess.run([sys.executable, "-c", script, "dvt-nosuchproc"], capture_output=True, text=True, timeout=30)
        assert none.returncode == 3 and none.stdout.strip() == "none=dvt-nosuchproc"
    finally:
        for p in procs:
            p.kill()
            p.wait(5)
