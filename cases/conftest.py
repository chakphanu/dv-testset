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


def pytest_addoption(parser):
    parser.addoption("--update-baseline", action="store_true",
                     help="write baselines/ instead of comparing (same as DVT_UPDATE_BASELINE=1)")


@pytest.fixture(scope="session")
def update_baseline(request):
    return request.config.getoption("--update-baseline") or os.environ.get("DVT_UPDATE_BASELINE") == "1"


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


NETNS_BUILD = "scripts/build-xlxd-netns.sh"


def _check_xlxd_bin(path, env_name):
    """Skip unless path is an xlxd built for the netns case by NETNS_BUILD:
    foreground, config/XML paths relative to the working directory."""
    if not path:
        pytest.skip("no xlxd binary: run %s and set %s=.cache/netns/<dir>/src/xlxd"
                    % (NETNS_BUILD, env_name))
    binary = Path(path)
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        pytest.skip("%s=%s is not an executable file" % (env_name, path))
    data = binary.read_bytes()
    if b"/xlxd/xlxd." in data or b"/var/log/xlxd" in data:
        pytest.skip("%s has absolute /xlxd paths (daemon build); build it with %s"
                    % (env_name, NETNS_BUILD))
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


# ---------------------------------------------------------------- docker rig

def _dextra_ack(ip, seconds=3.0):
    from dvtest.proto import dextra
    client = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module="A")
    try:
        return client.link(ip, timeout=seconds)
    finally:
        if client.dest:
            client.unlink()
        client.close()


@pytest.fixture(scope="session")
def rig(target):
    """xlxd-ref and xlxd-sut of the docker rig (scripts/run.sh sets the
    DVT_XLXD_* addresses in the tester), both answering a DExtra link."""
    if not (target.is_given("xlxd_ref") and target.is_given("xlxd_sut")):
        pytest.skip("no rig: run inside the tester via scripts/run.sh (DVT_XLXD_REF/DVT_XLXD_SUT unset)")
    for name in ("xlxd_ref", "xlxd_sut"):
        ip = getattr(target, name)
        deadline = time.monotonic() + 30
        while not _dextra_ack(ip):
            if time.monotonic() > deadline:
                pytest.fail("%s %s does not ACK a DExtra link" % (name, ip))
    return target


@pytest.fixture(scope="session")
def rig_usrp(rig):
    """xlxd-sut loaded rig/config/xlxd.usrp.in (DVT_SUT_USRP=on)."""
    if os.environ.get("DVT_SUT_USRP") != "on":
        pytest.skip("xlxd-sut has no USRP endpoints (DVT_SUT_USRP=on, e.g. run.sh --profile allstar)")
    return rig


class RigControl:
    """stop/start/kill of a rig service through the run.sh watcher: a
    request file in $DVT_OUT/control, answered by an .ack file."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def __call__(self, action, service, timeout=30.0):
        self.count += 1
        name = "%d-%d" % (os.getpid(), self.count)
        tmp = self.dir / (name + ".tmp")
        tmp.write_text("%s %s\n" % (action, service))
        tmp.rename(self.dir / (name + ".req"))
        ack = self.dir / (name + ".ack")
        deadline = time.monotonic() + timeout
        while not ack.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("no ack for %s %s" % (action, service))
            time.sleep(0.1)
        reply = ack.read_text().strip()
        if reply != "ok":
            raise RuntimeError("%s %s: %s" % (action, service, reply))


@pytest.fixture(scope="session")
def rig_control(rig, target):
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1 is set by scripts/run.sh)")
    return RigControl(Path(target.out) / "control")


@pytest.fixture(scope="session")
def allstar(rig_usrp, target):
    """AllStar node of the rig (profile allstar) and the IAX2 secret."""
    if not (target.is_given("allstar") and target.iax_secret):
        pytest.skip("no AllStar node: run.sh --profile allstar (DVT_ALLSTAR and DVT_IAX_SECRET)")
    return target


# ---------------------------------------------------------------- interop / behaviour

# Cases that judge reflector behaviour (floor, late entry, stream timeout
# and terminators, revoke tail, zero fill, TOT, load loss) as
# "file::function". Every other rig/netns/allstar/... case is interop
# (wire format, handshakes, keepalives, framing, codec routing and bytes,
# stream ids, no re-forwarding, no echo) and is always judged against
# xlxd-ref. Behaviour cases follow DVT_SUT_PROFILE (dvtest/specs).
BEHAVIOUR = {
    "test_xlxd_clients.py::test_late_entry_is_native_only",          # C11
    "test_xlxd_clients.py::test_stream_timeout_frees_module",        # C8
    "test_xlxd_clients.py::test_leave_mid_stream_tail",              # C7b
    "test_xlxd_clients.py::test_tot_180s",
    "test_backend_loss.py::test_native_passthrough_while_down",      # D2 zero fill
    "test_backend_loss.py::test_usrp_pcm_not_fabricated",            # D2 zero fill
    "test_allstar.py::test_node_crash_releases_module",              # floor after peer loss
    "test_xlxd_restart.py::test_restart_no_stale_stream",            # stream timing
    "test_load.py::test_no_load_loss_passthrough",                   # C9 load loss
    "test_load.py::test_no_load_loss_cross",
    "test_load.py::test_no_load_loss_latency",
    "test_load.py::test_loss_not_worse_than_ref",
    "test_load.py::test_loss_not_systematically_worse",
    "test_floor.py::test_first_talker_wins",                         # B1
    "test_floor.py::test_late_entry_denied",                         # B2
    "test_floor.py::test_late_entry_pending",                        # B2 knob
    "test_floor.py::test_usrp_unkey_after_silence",                  # B3
    "test_floor.py::test_usrp_over_suppressed_when_busy",            # B3
    "test_floor.py::test_admission_sid_zero",                        # B8
    "test_floor.py::test_admission_one_floor_per_client",            # B8
    "test_floor.py::test_admission_same_sid_two_rooms",              # B8
    "test_floor.py::test_b14_placeholder",                           # B14
    "test_xlxd_interlink.py::test_admission_remote_sid_open_elsewhere",  # B8c
    "test_xlxd_interlink.py::test_interlink_missing_output_slot_zeroed",  # B6b
}
SYSTEM_MARKERS = {"rig", "netns", "allstar", "nxdn", "p25", "load", "interlink"}


def pytest_collection_modifyitems(items):
    for item in items:
        if not SYSTEM_MARKERS & {m.name for m in item.iter_markers()}:
            continue
        key = "%s::%s" % (Path(str(item.fspath)).name, getattr(item, "originalname", item.name))
        item.add_marker(pytest.mark.behaviour if key in BEHAVIOUR else pytest.mark.interop)


@pytest.fixture
def behaviour_spec(request, target):
    """The profile's expectation for this behaviour case: None under the
    xlxd profile (the case is differential), else the dvtest/specs entry
    (pytest_runtest_setup already skipped a pending one)."""
    from dvtest import specs
    return specs.rule(target, getattr(request.node, "originalname", request.node.name))


def pytest_runtest_setup(item):
    if item.get_closest_marker("behaviour") is None:
        return
    from dvtest import specs
    profile = target_mod.load().sut_profile
    if profile != specs.DEFAULT and specs.expectation(profile, getattr(item, "originalname", item.name)) is None:
        pytest.skip("spec pending: no %s entry in dvtest/specs/%s.py for this behaviour case"
                    % (getattr(item, "originalname", item.name), profile))
