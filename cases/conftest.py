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

from dvtest import limits  # noqa: E402
from dvtest import target as target_mod  # noqa: E402
from dvtest.control import RigControl  # noqa: E402
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
    cmd = ["docker", "run", "-d", "--rm", "--name", name] + limits.docker_run_args()
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


def _m17_ack(ip, seconds=3.0):
    """mrefd speaks M17 only: a client CONN on its first module is ACKed."""
    from dvtest.proto import m17
    client = m17.M17Client("0.0.0.0", callsign="N0TEST", module=(os.environ.get("DVT_M17_MODULES") or "MNOP")[0])
    try:
        return client.link(ip, timeout=seconds)
    finally:
        if client.dest:
            client.unlink()
        client.close()


@pytest.fixture(scope="session")
def rig(target):
    """xlxd-ref and xlxd-sut of the docker rig (scripts/run.sh sets the
    DVT_XLXD_* addresses in the tester), both answering a DExtra link (an
    mrefd reference, DVT_REF_KIND=mrefd, an M17 link)."""
    if not (target.is_given("xlxd_ref") and target.is_given("xlxd_sut")):
        pytest.skip("no rig: run inside the tester via scripts/run.sh (DVT_XLXD_REF/DVT_XLXD_SUT unset)")
    for name in ("xlxd_ref", "xlxd_sut"):
        ip = getattr(target, name)
        mrefd = name == "xlxd_ref" and target.ref_kind == "mrefd"
        deadline = time.monotonic() + 30
        while not (_m17_ack(ip) if mrefd else _dextra_ack(ip)):
            if time.monotonic() > deadline:
                pytest.fail("%s %s does not ACK %s" % (name, ip, "an M17 link" if mrefd else "a DExtra link"))
    return target


@pytest.fixture(scope="session")
def rig_usrp(rig):
    """xlxd-sut loaded rig/config/xlxd.usrp.in (DVT_SUT_USRP=on)."""
    if os.environ.get("DVT_SUT_USRP") != "on":
        pytest.skip("xlxd-sut has no USRP endpoints (DVT_SUT_USRP=on, e.g. run.sh --profile allstar)")
    return rig


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
    "test_floor.py::test_dmr_rptcl_closes_session",                  # B15
    "test_floor.py::test_no_voice_before_header",                    # B16
    "test_floor.py::test_no_self_echo_other_sid",                    # B16
    "test_floor.py::test_pending_entrant_starts_with_header",        # B16 knob
    "test_floor.py::test_m17_frame0_relayed",                        # B17
    "test_xlxd_interlink.py::test_admission_remote_sid_open_elsewhere",  # B8c
    "test_xlxd_interlink.py::test_interlink_missing_output_slot_zeroed",  # B6b
    "test_mmdvm.py::test_mmdvm_lost_terminator_timeout",             # P-DMR-34 (B3)
    "test_mmdvm.py::test_mmdvm_busy_module_no_mix",                  # P-DMR-35 (B1/B2)
    "test_p25_network.py::test_p25_second_talker_dropped",          # P-P25-13 (B1)
    "test_p25_network.py::test_p25_stream_timeout",                 # P-P25-15 (B3)
    "test_short_stream_guard.py::test_short_stream_guard_first_block",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_second_block",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_per_callsign",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_cross_protocol_block",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_cross_protocol_strikes",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_third_strike",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_decay",  # B18
    "test_short_stream_guard.py::test_short_stream_guard_third_strike_protocols",  # B18
    "test_dmrplus.py::test_dmp_busy_module_no_mix",                  # P-DMP-35 (B1)
    "test_p25.py::test_digital_to_p25",                              # F5 (P-P25-24 padding)
}
SYSTEM_MARKERS = {"rig", "netns", "allstar", "nxdn", "p25", "load", "interlink"}


# Rig cases whose assertions need cross-mode audio through the system
# under test's transcoding ("file::function": True for every param, or
# the set of param ids that are cross-mode), so a sut without transcoding
# can run -m "rig and not crossmode".
CROSSMODE = {
    "test_xlxd_matrix.py::test_cross_sut_equals_ref": True,
    "test_xlxd_clients.py::test_module_change_follows_talker": True,
    "test_xlxd_clients.py::test_stream_timeout_frees_module": True,
    "test_xlxd_clients.py::test_late_entry_is_native_only": True,
    "test_xlxd_clients.py::test_leave_mid_stream_tail": {"dmr"},
    "test_floor.py::test_first_talker_wins": True,
    "test_floor.py::test_usrp_unkey_after_silence": True,
    "test_floor.py::test_admission_one_floor_per_client": True,
    "test_floor.py::test_admission_same_sid_two_rooms": True,
    "test_floor.py::test_sid_cross_mapping": True,
    "test_floor.py::test_dmr_rptcl_closes_session": True,
    "test_floor.py::test_no_voice_before_header": True,
    "test_floor.py::test_no_self_echo_other_sid": True,
    "test_backend_loss.py::test_native_passthrough_while_down": True,
    "test_backend_loss.py::test_usrp_pcm_not_fabricated": True,
    "test_backend_loss.py::test_cross_path_recovers": True,
    "test_allstar.py::test_iax_to_all_digital_listeners": True,
    "test_allstar.py::test_digital_to_iax": True,
    "test_allstar.py::test_rooms_no_crosstalk": True,
    "test_allstar.py::test_usrp_no_echo": True,
    "test_allstar.py::test_node_crash_releases_module": True,
    "test_morse_codecs.py::test_morse_dstar_to_iax": True,
    "test_morse_codecs.py::test_morse_iax_to_m17": True,
    "test_nxdn.py::test_cross_sut_equals_ref": True,
    "test_nxdn.py::test_morse_round_trip": True,
    "test_nxdn.py::test_nxdn_to_allstar": True,
    "test_nxdn.py::test_allstar_to_nxdn": True,
    "test_p25.py::test_p25_to_digital": True,
    "test_p25.py::test_digital_to_p25": True,
    "test_p25.py::test_morse_p25_to_m17": True,
    "test_p25.py::test_morse_m17_to_p25": True,
    "test_p25.py::test_p25_to_allstar": True,
    "test_p25.py::test_allstar_to_p25": True,
    "test_clients2.py::test_cross_sut_equals_ref": True,
    "test_xlxd_interlink.py::test_interlink_dmr_sut_equals_ref": {"forward"},   # reverse: XLX998 has none
    "test_xlxd_restart.py::test_restart_module_works_after_relink": True,
    "test_xlxd_restart.py::test_restart_dmr_sut_equals_ref": True,
    "test_xlxd_transcoder_lifecycle.py::test_rig_transcoder_lost_and_back_usrp_resumes": True,
    "test_load.py::test_no_load_loss_cross": True,
    "test_load.py::test_loss_not_worse_than_ref": {"cross"},
    "test_load.py::test_loss_not_systematically_worse": {"cross"},
    # P25 sections 4-5 (egress / ingress through the IMBE transcoder)
    "test_p25_network.py::test_p25_egress_superframe": True,
    "test_p25_network.py::test_p25_egress_record_layout": True,
    "test_p25_network.py::test_p25_egress_lcf": True,
    "test_p25_network.py::test_p25_egress_dst_tg": True,
    "test_p25_network.py::test_p25_egress_src_id": True,
    "test_p25_network.py::test_p25_egress_terminator_ldu_boundary": True,
    "test_p25_network.py::test_p25_no_lone_terminator": True,
    "test_p25_network.py::test_p25_short_call_padded": True,
    "test_p25_network.py::test_p25_egress_clear_es": True,
    "test_p25_network.py::test_p25_egress_parity_recorded": True,
    "test_p25_network.py::test_p25_egress_pacing": True,
    "test_p25_network.py::test_p25_ingress_no_frame_loss": True,
    "test_p25_network.py::test_p25_ingress_late_start": True,
    "test_p25_network.py::test_p25_ingress_other_tg": True,
    "test_p25_network.py::test_p25_ingress_unknown_src": True,
    "test_p25_network.py::test_p25_ingress_record_loss": True,
    "test_p25_network.py::test_p25_ingress_encrypted": True,
    # B18: a DMR / M17 over is seen only through transcoding to the DExtra listener
    "test_short_stream_guard.py::test_short_stream_guard_cross_protocol_block": True,
    "test_dmrplus.py::test_dmp_cross_mode": True,
    # XLX interlink cases that need transcoded slots
    "test_xlx.py::test_xlx_egress_slots_transcoded": True,
    "test_xlx.py::test_xlx_ingress_rev1_transcoded": True,
    "test_xlx.py::test_xlx_ingress_size_mismatch": True,
    "test_xlx.py::test_xlx_egress_slot_missing_mid_stream": True,
}


# Cases whose talker has no real radio id on (or into) an id-based edge
# (MMDVM, DMR+, NXDN, P25): their sut expectation depends on
# DVT_NO_RADIO_ID (specs.rule_mode); a second dvrouter pass with
# DVT_NO_RADIO_ID=fallback runs -m radio_id. Same value form as CROSSMODE.
RADIO_ID = {
    "test_p25_network.py::test_p25_egress_src_id": {"unknown"},
    "test_p25_network.py::test_p25_ingress_unknown_src": True,
    "test_nxdn_peer.py::test_nxdn_egress_src_id": {"unknown"},
    "test_nxdn_peer.py::test_nxdn_ingress_src_mapping": True,
    "test_dmrplus.py::test_dmp_egress_header_fields": {"unknown"},
    "test_dmrplus.py::test_dmp_unknown_source": True,
}


def _in_table(table, key, item):
    rule = table.get(key)
    if rule is True:
        return True
    callspec = getattr(item, "callspec", None)
    return bool(rule and callspec and set(callspec.id.split("-")) & rule)


def _crossmode(key, item):
    rule = CROSSMODE.get(key)
    if rule is True:
        return True
    callspec = getattr(item, "callspec", None)
    return bool(rule and callspec and set(callspec.id.split("-")) & rule)


def pytest_report_header(config):
    """Say whether this run is inside the capped scope (scripts/contained.sh)."""
    if os.environ.get("DVT_CONTAINED"):
        return "resource scope: %s" % os.environ["DVT_CONTAINED"]
    if os.environ.get("DVT_RIG_CONTROL") or os.path.exists("/.dockerenv"):
        return "resource scope: container limits (rig/compose.yml)"
    return ("resource scope: NONE - host-side wire/netns runs belong in "
            "scripts/contained.sh --name test -- python3 -m pytest ... (README, 'Resource limits')")


def pytest_collection_modifyitems(items):
    for item in items:
        if not SYSTEM_MARKERS & {m.name for m in item.iter_markers()}:
            continue
        key = "%s::%s" % (Path(str(item.fspath)).name, getattr(item, "originalname", item.name))
        item.add_marker(pytest.mark.behaviour if key in BEHAVIOUR else pytest.mark.interop)
        if _crossmode(key, item):
            item.add_marker(pytest.mark.crossmode)
        if _in_table(RADIO_ID, key, item):
            item.add_marker(pytest.mark.radio_id)


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
