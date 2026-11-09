# SPDX-License-Identifier: MIT
"""The compose profile `mmdvm` and its run.sh wiring (offline): the service mmdvm-stack has the limit set, the hardened
shape and a rig address nobody else uses, the tester gets the variables the cases read, run.sh knows the profile and lets
the control channel stop and start the service, and the placeholders compose sets are the ones the entry script requires."""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "rig" / "compose.yml").read_text()
RUN_SH = (ROOT / "scripts" / "run.sh").read_text()


def service_block(name):
    m = re.search(r"^  %s:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|^\S)" % re.escape(name), COMPOSE, re.M | re.S)
    assert m, "service %s not in rig/compose.yml" % name
    return m.group(1)


def test_service_has_limits_hardening_and_a_free_address():
    block = service_block("mmdvm-stack")
    assert "<<: *limits-small" in block                       # mem_limit = memswap_limit, cpus, pids_limit from the anchor
    assert "pids_limit: ${DVT_PIDS_MMDVM:-256}" in block
    assert "profiles: [mmdvm]" in block and "dvt-mmdvmstack:local" in block
    for need in ("cap_drop: [ALL]", "no-new-privileges:true", "read_only: true", "- /tmp", "stop_grace_period: 20s"):
        assert need in block
    assert 'ipv4_address: "${DVT_NET:-172.30.0}.100"' in block
    assert len(re.findall(r'\$\{DVT_NET:-172\.30\.0\}\.100"', COMPOSE)) == 1     # the address is used once
    assert "/mmdvm/dmrid.dat:ro" in block


def test_limits_anchor_carries_the_four_keys():
    m = re.search(r"x-limits-small: &limits-small\n((?:  .*\n)+)", COMPOSE)
    assert m and all(k in m.group(1) for k in ("mem_limit", "memswap_limit", "cpus", "pids_limit"))


def test_build_contexts_match_the_dockerfile():
    block = service_block("mmdvm-stack")
    dockerfile = (ROOT / "rig" / "Dockerfile.mmdvmstack").read_text()
    contexts = set(re.findall(r"^        (\w+): \.\./\.cache/mmdvmstack/(\w+)$", block, re.M))
    assert {c for c, _ in contexts} == set(re.findall(r"COPY --from=(\w+) ", dockerfile)) - {"build"}
    pre = (ROOT / "scripts" / "pre-fetch-mmdvmstack.sh").read_text()
    for _ctx, directory in contexts:
        assert directory in pre or ".cache/mmdvmstack" in pre


def test_environment_matches_the_entry_script():
    block = service_block("mmdvm-stack")
    env = set(re.findall(r"^      ([A-Z0-9_]+): ", block, re.M))
    entry = (ROOT / "rig" / "mmdvmstack-entry.sh").read_text()
    required = set(re.search(r"REQUIRED=\((.*?)\)", entry, re.S).group(1).split())
    assert required <= env, "compose lacks %s" % sorted(required - env)
    assert env - required <= {"DUPLEX", "RF_MODE_HANG", "NET_MODE_HANG", "LOG_LEVEL"}, sorted(env - required)


def test_tester_gets_the_variables_of_the_cases():
    for var in ("DVT_MMDVM: ${DVT_MMDVM:-off}", "DVT_MMDVM_STACK: ${DVT_NET:-172.30.0}.100", "DVT_MMDVM_MODEM_PORT:",
                "DVT_MMDVM_MODULE_DSTAR:", "DVT_MMDVM_MODULE_DMR:", "DVT_MMDVM_MODULE_YSF:", "DVT_MMDVM_P25_TG:", "DVT_MMDVM_NXDN_TG:"):
        assert var in service_block("tester"), var
    # the modem port of the tester and the port MMDVMHost is told are one setting
    assert "MODEM_PORT: ${DVT_MMDVM_MODEM_PORT:-3334}" in service_block("mmdvm-stack")


def test_run_sh_knows_the_profile():
    assert re.search(r"^    mmdvm\)\n.*?profiles\+=\(--profile mmdvm\)\n\s+export DVT_MMDVM=on", RUN_SH, re.M | re.S)
    assert "--profile mmdvm" in re.search(r"compose_all=\((.*?)\)\n", RUN_SH, re.S).group(1)
    assert ".cache/mmdvmstack/MMDVMHost/MMDVM-Host.cpp" in RUN_SH
    assert re.search(r"asl-e\|mmdvm-stack\) ;;", RUN_SH)       # stop / start / restart / kill through the control channel
    assert "webload, mmdvm, mapsrc, nat, aprs, dw or a comma list" in RUN_SH


def test_header_lists_the_profile_and_the_address():
    head = COMPOSE.split("name: dvt")[0]
    assert "#   mmdvm    the REAL client stack" in head and "mmdvm-stack .100 (profile mmdvm)" in head
