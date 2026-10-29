# SPDX-License-Identifier: MIT
"""Shared fixtures. Every fixture that needs something from the outside
skips with the reason when it is missing; a target that is configured but
does not answer is an error, not a skip."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dvtest import target as target_mod  # noqa: E402
from dvtest.proto import ambed  # noqa: E402


@pytest.fixture(scope="session")
def target():
    return target_mod.load()


@pytest.fixture(scope="session")
def out_root(target):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = Path(target.out)
    if not path.is_absolute():
        path = ROOT / path
    path = path / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _wait_pong(addr, port, seconds):
    client = ambed.AmbedClient(addr, port, timeout=0.5)
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if client.rpc(ambed.ping("N0TEST")) == ambed.PONG:
                return True
        return False
    finally:
        client.close()


def _start_transcoder_image(image):
    """docker run the image on the default bridge; extra -e from
    DVT_TRANSCODER_ENV and image arguments from DVT_TRANSCODER_ARGS (both
    space separated). Returns (name, ip)."""
    if not shutil.which("docker"):
        pytest.skip("TRANSCODER_IMAGE is set but docker is not installed")
    name = "dvt-wire-%d" % os.getpid()
    cmd = ["docker", "run", "-d", "--rm", "--name", name]
    for item in os.environ.get("DVT_TRANSCODER_ENV", "").split():
        cmd += ["-e", item]
    cmd.append(image)
    cmd += os.environ.get("DVT_TRANSCODER_ARGS", "").split()
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        pytest.fail("docker run %s failed: %s" % (image, run.stderr.strip()))
    ip = subprocess.run(["docker", "inspect", "-f",
                         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name],
                        capture_output=True, text=True).stdout.strip()
    return name, ip


@pytest.fixture(scope="session")
def ambed_addr(target):
    """(ip, port) of one transcoder answering AMBEDPING: DVT_AMBED_ADDR, or
    a container started from TRANSCODER_IMAGE for the session."""
    name = None
    if target.is_given("ambed_addr"):
        addr = target.ambed_addr
    elif os.environ.get("TRANSCODER_IMAGE"):
        name, addr = _start_transcoder_image(os.environ["TRANSCODER_IMAGE"])
    else:
        pytest.skip("no transcoder: set DVT_AMBED_ADDR=<ip> (or TRANSCODER_IMAGE=<image>)")
    try:
        if not _wait_pong(addr, target.ambed_port, 15):
            pytest.fail("transcoder %s:%d does not answer AMBEDPING" % (addr, target.ambed_port))
        yield addr, target.ambed_port
    finally:
        if name:
            subprocess.run(["docker", "stop", name], capture_output=True)


@pytest.fixture(scope="session")
def ambed_ref_addr(target):
    """Optional reference transcoder (DVT_AMBED_REF) for legacy compares."""
    if not target.is_given("ambed_ref"):
        pytest.skip("no reference transcoder: set DVT_AMBED_REF=<ip> to compare legacy replies")
    if not _wait_pong(target.ambed_ref, target.ambed_port, 5):
        pytest.fail("reference transcoder %s does not answer AMBEDPING" % target.ambed_ref)
    return target.ambed_ref, target.ambed_port


NETNS_PATCH = ("sed -i 's|^#define RUN_AS_DAEMON|//#define RUN_AS_DAEMON|; "
               "s|\"/xlxd/|\"./|g; s|\"/var/log/|\"./|g; "
               "s|_USE_RLX_SERVER\\( *\\)1|_USE_RLX_SERVER\\10|' src/main.h")


def _check_xlxd_bin(path, env_name):
    """Skip unless path is an xlxd built for the netns case: foreground,
    config/XML paths relative to the working directory (NETNS_PATCH)."""
    if not path:
        pytest.skip("no xlxd binary: set %s to an xlxd built for netns (cases/conftest.py NETNS_PATCH)"
                    % env_name)
    binary = Path(path)
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        pytest.skip("%s=%s is not an executable file" % (env_name, path))
    data = binary.read_bytes()
    if b"/xlxd/xlxd." in data or b"/var/log/xlxd" in data:
        pytest.skip("%s has absolute /xlxd paths (daemon build); rebuild after `%s`"
                    % (env_name, NETNS_PATCH))
    return binary


@pytest.fixture(scope="session")
def netns_ok():
    if not (shutil.which("unshare") and shutil.which("ip")):
        pytest.skip("netns cases need unshare and ip (util-linux, iproute2)")
    if subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode != 0:
        pytest.skip("unshare -rn is not permitted (unprivileged user namespaces disabled)")
    return True


@pytest.fixture(scope="session")
def xlxd_bin(target, netns_ok):
    return _check_xlxd_bin(target.xlxd_bin, "DVT_XLXD_BIN")


@pytest.fixture(scope="session")
def xlxd_ref_bin(target, netns_ok):
    return _check_xlxd_bin(target.xlxd_ref_bin, "DVT_XLXD_REF_BIN")
