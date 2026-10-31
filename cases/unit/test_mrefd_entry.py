# SPDX-License-Identifier: MIT
"""rig/mrefd-entry.sh: the mrefd.cfg, mrefd.interlink and list files it
renders from the environment (DVT_MREFD_RENDER_ONLY=1 stops before mrefd)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ENTRY = Path(__file__).resolve().parents[2] / "rig" / "mrefd-entry.sh"


def render(tmp_path, **env):
    if not shutil.which("sh"):
        pytest.skip("sh is not installed")
    full = {"PATH": os.environ.get("PATH", ""), "DVT_MREFD_DIR": str(tmp_path / "out"),
            "DVT_MREFD_RENDER_ONLY": "1"}
    full.update(env)
    return subprocess.run(["sh", str(ENTRY)], env=full, capture_output=True, text=True, timeout=30)


def cfg(tmp_path):
    lines = (tmp_path / "out" / "mrefd.cfg").read_text().splitlines()
    return dict(line.split(" = ", 1) for line in lines if " = " in line)


def read(tmp_path, name):
    return (tmp_path / "out" / name).read_text()


def test_defaults(tmp_path):
    run = render(tmp_path, DVT_IP="172.30.0.80", DVT_M17_PEER_IP="172.30.0.30")
    assert run.returncode == 0, run.stderr
    conf = cfg(tmp_path)
    assert conf["Callsign"] == "M17-REF" and conf["Modules"] == "MNOP"
    assert conf["IPv4BindAddr"] == "172.30.0.80" and conf["Port"] == "17000"
    # every file path stays in the output directory; nothing needs the network
    for key in ("PidPath", "JsonPath", "WhitelistPath", "BlacklistPath", "InterlinkPath"):
        assert conf[key].startswith(str(tmp_path / "out"))
    for key in ("Bootstrap", "DashboardURL", "EmailAddr", "IPv6BindAddr", "DHTSavePath"):
        assert key not in conf
    assert read(tmp_path, "mrefd.interlink").splitlines() == [
        "M17-DVT 172.30.0.30 17011 M", "M17-DVU 172.30.0.30 17012 MN"]
    assert read(tmp_path, "mrefd.blacklist") == "" and read(tmp_path, "mrefd.whitelist") == ""


def test_peers_and_modules_from_env(tmp_path):
    run = render(tmp_path, DVT_IP="172.30.7.80", DVT_M17_PEER_IP="172.30.7.30", DVT_M17_MODULES="ABM",
                 DVT_M17_PEER_DVT_PORT="20001", DVT_M17_PEER_DVT_MODULES="AB",
                 DVT_M17_PEER_DVU_PORT="20002", DVT_M17_PEER_DVU_MODULES="M",
                 DVT_MREFD_CALLSIGN="M17-X15", DVT_BLACKLIST="N0CALL N0TEST")
    assert run.returncode == 0, run.stderr
    conf = cfg(tmp_path)
    assert (conf["Callsign"], conf["Modules"]) == ("M17-X15", "ABM")
    assert read(tmp_path, "mrefd.interlink").splitlines() == [
        "M17-DVT 172.30.7.30 20001 AB", "M17-DVU 172.30.7.30 20002 M"]
    assert read(tmp_path, "mrefd.blacklist").splitlines() == ["N0CALL", "N0TEST"]


def test_dvu_peer_has_its_own_address(tmp_path):
    """mrefd keys peers by IP only: DVT_M17_PEER_DVU_HOST moves the M17-DVU
    row to another tester address, M17-DVT keeps DVT_M17_PEER_IP."""
    run = render(tmp_path, DVT_IP="172.30.0.80", DVT_M17_PEER_IP="172.30.0.30", DVT_M17_PEER_DVU_HOST="172.30.0.32")
    assert run.returncode == 0, run.stderr
    assert read(tmp_path, "mrefd.interlink").splitlines() == [
        "M17-DVT 172.30.0.30 17011 M", "M17-DVU 172.30.0.32 17012 MN"]
    bad = render(tmp_path, DVT_IP="172.30.0.80", DVT_M17_PEER_IP="172.30.0.30", DVT_M17_PEER_DVU_HOST="peer.example")
    assert bad.returncode != 0 and "DVT_M17_PEER_DVU_HOST" in bad.stderr


def test_no_peer_address_means_no_interlinks(tmp_path):
    assert render(tmp_path, DVT_IP="172.30.0.80").returncode == 0
    assert read(tmp_path, "mrefd.interlink") == ""


@pytest.mark.parametrize("env,message", [
    ({}, "DVT_IP"),
    ({"DVT_IP": "172.30.0.80", "DVT_MREFD_CALLSIGN": "URF999"}, "M17-ABC"),
    ({"DVT_IP": "172.30.0.80", "DVT_MREFD_CALLSIGN": "M17-ab1"}, "M17-ABC"),
    ({"DVT_IP": "172.30.0.80", "DVT_M17_MODULES": "m1"}, "DVT_M17_MODULES"),
    ({"DVT_IP": "172.30.0.80", "DVT_M17_PEER_IP": "peer.example"}, "DVT_M17_PEER_IP"),
    ({"DVT_IP": "172.30.0.80", "DVT_M17_PEER_IP": "172.30.0.30", "DVT_M17_PEER_DVT_PORT": "80"}, "PORT"),
    ({"DVT_IP": "172.30.0.80", "DVT_M17_PEER_IP": "172.30.0.30", "DVT_M17_PEER_DVU_PORT": "x"}, "PORT"),
    ({"DVT_IP": "172.30.0.80", "DVT_M17_PEER_IP": "172.30.0.30", "DVT_M17_PEER_DVU_MODULES": "M N"}, "MODULES"),
])
def test_rejects_bad_settings(tmp_path, env, message):
    run = render(tmp_path, **env)
    assert run.returncode != 0 and message in run.stderr
