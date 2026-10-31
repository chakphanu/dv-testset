# SPDX-License-Identifier: MIT
"""Resource limits: docker run words, the compose service check and the
shipped rig/compose.yml (every service of every profile must be capped)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dvtest import limits

ROOT = Path(__file__).resolve().parents[2]
GOOD = {"mem_limit": 536870912, "memswap_limit": "536870912", "cpus": 0.5, "pids_limit": 128}


def test_parse_bytes():
    assert limits.parse_bytes("512m") == 512 * 1024 ** 2
    assert limits.parse_bytes("2G") == 2 * 1024 ** 3
    assert limits.parse_bytes(1024) == 1024 and limits.parse_bytes("1024") == 1024
    for bad in ("", "lots", "0", 0, -5, "1x", None, True):
        with pytest.raises(ValueError):
            limits.parse_bytes(bad)


def test_docker_run_args_defaults_and_env():
    assert limits.docker_run_args({}) == ["--memory", "1g", "--memory-swap", "1g",
                                          "--cpus", "2", "--pids-limit", "512"]
    got = limits.docker_run_args({"DVT_CTR_MEM": "256m", "DVT_CTR_CPUS": "0.5", "DVT_CTR_PIDS": "64"})
    assert got == ["--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64"]
    with pytest.raises(ValueError):
        limits.docker_run_args({"DVT_CTR_MEM": "lots"})
    with pytest.raises(ValueError):
        limits.docker_run_args({"DVT_CTR_CPUS": "0"})


def test_check_services_accepts_capped():
    assert limits.check_services({"services": {"a": dict(GOOD), "b": dict(GOOD)}}) == []


@pytest.mark.parametrize("field", ["mem_limit", "memswap_limit", "cpus", "pids_limit"])
def test_check_services_flags_each_missing_field(field):
    svc = dict(GOOD)
    del svc[field]
    problems = limits.check_services({"services": {"ok": dict(GOOD), "bad": svc}})
    assert problems and all(p.startswith("bad:") for p in problems)
    assert any(field in p for p in problems)


def test_check_services_flags_swap_and_empty():
    swap = dict(GOOD, memswap_limit=1073741824)
    assert any("swap" in p for p in limits.check_services({"services": {"s": swap}}))
    assert limits.check_services({"services": {}})
    assert limits.check_services({})


def test_main_exit_status(monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"services": {"s": dict(GOOD)}})))
    assert limits.main() == 0
    bad = dict(GOOD)
    del bad["pids_limit"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"services": {"s": bad}})))
    assert limits.main() == 1
    assert "pids_limit" in capsys.readouterr().err


def test_shipped_compose_caps_every_service_of_every_profile():
    if not shutil.which("docker"):
        pytest.skip("docker is not installed")
    profiles = ["core", "allstar", "nxdn", "p25", "interlink", "load", "g3", "urfd", "netem",
                "capture", "capture-allstar", "capture-nxdn", "capture-p25", "capture-urfd",
                "capture-interlink"]
    cmd = ["docker", "compose", "-p", "dvtlimits", "-f", str(ROOT / "rig/compose.yml"),
           "--env-file", str(ROOT / ".env.example")]
    for name in profiles:
        cmd += ["--profile", name]
    cmd += ["config", "--format", "json"]
    env = dict(os.environ, TRANSCODER_IMAGE="example.invalid/transcoder:none")
    run = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    if run.returncode != 0:
        pytest.skip("docker compose config unavailable: %s" % run.stderr.strip()[:200])
    config = json.loads(run.stdout)
    assert len(config["services"]) >= 25
    assert limits.check_services(config) == []
