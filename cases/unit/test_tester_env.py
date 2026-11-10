# SPDX-License-Identifier: MIT
"""The tester container gets an explicit `environment:` list from rig/compose.yml (plus the `-e` of run.sh's `exec` and the env_file
.local/rig.env). A DVT_* switch that a rig case reads from os.environ and that is in none of them is silently inert in a rig run:
DVT_SECRELOAD1 / DVT_SECRELOAD2 (strict mode, fail instead of skip) were not passed, so the strict switch never applied (found by
dp15-qar, 2026-11-10). This test fails when a tester-side Python file reads a DVT_* name that is neither passed nor in an explicit,
documented host-only / alias list. docs/rig.md, "Environment of the tester". Stdlib only.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"""["'](DVT_[A-Z0-9_]*[A-Z0-9_])["']""")
# a line that reads the environment: os.environ[...] / .get / getenv, an `env.get(`, or a helper taking `env` as its first argument
READS_ENV = re.compile(r"environ|getenv|\benv\.get\(|\(env,|^\s*ENV_[A-Z0-9_]+\s*=")     # ENV_X = "DVT_X" constants are read through env.get(ENV_X)

# files that never run inside the tester container: host tools, netns and wire runners, the host-side docker caps, the NAT router's own
# shell variables. Their DVT_* names are read on the host (scripts/run.sh, scripts/contained.sh, systemd scope) and need no pass-through.
HOST_FILES = {
    "dvtest/limits.py": "host-side docker run caps (DVT_CTR_*)",
    "dvtest/netns_runs.py": "netns scenarios run on the host under contained.sh (DVT_NETNS_PARALLEL)",
    "dvtest/fuzz_run.py": "protocol-edge fuzz, netns, host (DVT_FUZZ_*)",
    "dvtest/nat.py": "iptables variables of the NAT router container, not of the tester",
}
HOST_DIRS = ("dvtest/tools/", "dvtest/check/")      # command line tools run on the host (some inside a throwaway container with their own env)
HOST_MARKERS = ("pytest.mark.netns", "pytest.mark.wire", "pytest.mark.cfgload")

# names a tester-side file reads that the tester does not get, each with the reason. Keep it short: a rig case that needs a switch gets
# it in the compose list instead.
HOST_ONLY_NAMES = {
    "DVT_CONTAINED": "set by scripts/contained.sh for host runs; conftest only prints the scope in the report header",
    "DVT_WEB_OPERATOR": "run.sh writes out/<run>/web/operator.env; the environment is only an override of that file",
    "DVT_WEB_OPERATOR_PASSWORD": "run.sh writes out/<run>/web/operator.env; the environment is only an override of that file",
    "DVT_TRANSCODER_ARGS": "wire cases (host): arguments of the transcoder under test",
    "DVT_TRANSCODER_ENV": "wire cases (host): environment of the transcoder under test",
    "DVT_PROD_CROSS_DRY": "production cross-mode check run on the host (docs/prod-checks.md); the rig only runs the dry plan",
    "DVT_PROD_MODULE": "production cross-mode check run on the host (docs/prod-checks.md)",
    "DVT_PROD_REFLECTOR": "production cross-mode check run on the host (docs/prod-checks.md)",
    "DVT_TARGET_FILE": "dvtest/target.py: the optional TOML of a host run (keys = variable names without the prefix); a rig run uses the environment",
    "DVT_SOAK_MEASURED_S": "old spelling of DVT_SOAK_MEASURE_S; rig/compose.yml maps it: DVT_SOAK_MEASURE_S: ${DVT_SOAK_MEASURE_S:-${DVT_SOAK_MEASURED_S:-}}",
    # prefix of names built in code: the full names are passed one by one (checked by test_built_names_are_passed)
    "DVT_MMDVM_SLACK_": "prefix: DVT_MMDVM_SLACK_<MODE> is built in cases/test_mmdvm_stack.py",
    # the RSS isolation case sets its own variables from pytest options (dvtest/isolation_plugin.py: --isolation, --isolation-short,
    # --isolation-env NAME=VAL), because the tester environment is a fixed list; they are read in the tester process after the plugin ran
    "DVT_ISOLATION": "set in the tester process by the pytest option --isolation (dvtest/isolation_plugin.py)",
    "DVT_ISOLATION_SHORT": "set by --isolation-short (dvtest/isolation_plugin.py)",
    "DVT_ISOLATION_SEED": "set by --isolation-env (dvtest/isolation_plugin.py)",
    "DVT_ISOLATION_E_LANE": "set by --isolation-env (dvtest/isolation_plugin.py)",
    "DVT_ISOLATION_WARMUP_S": "set by --isolation-env (dvtest/isolation_plugin.py)",
    "DVT_ISOLATION_MEASURE_S": "set by --isolation-env (dvtest/isolation_plugin.py)",
    "DVT_ISOLATION_SAMPLE_S": "set by --isolation-env (dvtest/isolation_plugin.py)",
}


def _tester_environment():
    """(names of the tester's `environment:` list in rig/compose.yml, names injected by run.sh's `exec -e`, names of .local/rig.env that
    scripts/gen-local.sh writes)."""
    text = (ROOT / "rig" / "compose.yml").read_text()
    start = text.index("\n  tester:\n")
    end = text.index("\n  allstar:\n", start)
    block = text[start:end]
    env = block[block.index("    environment:\n"):block.index("    env_file:\n")]
    listed = set(re.findall(r"^\s+(DVT_[A-Z0-9_]+):", env, re.M))
    injected = set(re.findall(r"exec -T (?:-e (DVT_[A-Z0-9_]+)(?:=\S*)? )+", (ROOT / "scripts" / "run.sh").read_text()))
    run_sh = (ROOT / "scripts" / "run.sh").read_text()
    for line in run_sh.splitlines():
        if '"${compose[@]}" exec -T -e' in line:
            injected |= set(re.findall(r"-e (DVT_[A-Z0-9_]+)", line))
    rig_env = set(re.findall(r"(?<![A-Z0-9_])(DVT_[A-Z0-9_]+)=", (ROOT / "scripts" / "gen-local.sh").read_text()))
    return listed, injected, rig_env


def _tester_side(path, text):
    rel = path.relative_to(ROOT).as_posix()
    if rel in HOST_FILES or rel.startswith(HOST_DIRS) or "/unit/" in rel or "__pycache__" in rel:
        return False
    if path.name.startswith("test_") and any(m in text for m in HOST_MARKERS) and "pytest.mark.rig" not in text:
        return False
    return True


def _names_read_by_the_tester():
    """{DVT_ name: sorted relative files} read through os.environ / getenv in the tester-side Python files of cases/ and dvtest/."""
    found = {}
    for base in ("cases", "dvtest"):
        for path in sorted((ROOT / base).rglob("*.py")):
            text = path.read_text()
            if not _tester_side(path, text):
                continue
            for line in text.splitlines():
                if READS_ENV.search(line):
                    for name in NAME.findall(line):
                        found.setdefault(name, set()).add(path.relative_to(ROOT).as_posix())
    return {name: sorted(files) for name, files in found.items()}


def test_every_dvt_variable_a_rig_case_reads_reaches_the_tester():
    listed, injected, rig_env = _tester_environment()
    passed = listed | injected | rig_env
    missing = {name: files for name, files in _names_read_by_the_tester().items() if name not in passed and name not in HOST_ONLY_NAMES}
    assert not missing, (
        "DVT_* variables read by tester-side Python but not in the tester environment of rig/compose.yml (add `NAME: ${NAME:-}` or, for a "
        "number, `${NAME:-<the code default>}`), nor in HOST_ONLY_NAMES with a reason:\n  " +
        "\n  ".join("%s  (%s)" % (name, ", ".join(files[:3])) for name, files in sorted(missing.items())))


def test_the_host_only_list_is_not_stale():
    listed, injected, rig_env = _tester_environment()
    read = _names_read_by_the_tester()
    stale = sorted(name for name in HOST_ONLY_NAMES if name not in read)
    assert not stale, "HOST_ONLY_NAMES lists names no tester-side file reads any more: %s" % stale
    passed_and_listed = sorted(name for name in HOST_ONLY_NAMES if name in listed | injected | rig_env)
    assert not passed_and_listed, "HOST_ONLY_NAMES lists names the tester does get: %s" % passed_and_listed


def test_built_names_are_passed():
    """Names that code builds from a prefix (DVT_MMDVM_SLACK_<MODE>, DVT_NXDN_<REF|SUT>, DVT_NXDN_PROBE_PORT_<REF|SUT>) cannot be found as
    literals: check the full names here."""
    listed, injected, rig_env = _tester_environment()
    passed = listed | injected | rig_env
    modes = re.search(r'^MODES = \(([^)]*)\)', (ROOT / "cases" / "test_mmdvm_stack.py").read_text(), re.M).group(1)
    wanted = {"DVT_MMDVM_SLACK_" + m.strip().strip('"').upper() for m in modes.split(",") if m.strip()}
    wanted |= {"DVT_NXDN_REF", "DVT_NXDN_SUT", "DVT_NXDN_PROBE_PORT_REF", "DVT_NXDN_PROBE_PORT_SUT"}
    assert len(wanted) == 9 and not sorted(wanted - passed), sorted(wanted - passed)


def test_host_files_exist():
    for rel in HOST_FILES:
        assert (ROOT / rel).is_file(), rel
    for rel in HOST_DIRS:
        assert (ROOT / rel).is_dir(), rel


def test_the_secreload_switches_are_passed():
    listed, _injected, _rig_env = _tester_environment()
    assert {"DVT_SECRELOAD1", "DVT_SECRELOAD2", "DVT_OBSPKT1B"} <= listed


def test_injected_and_rig_env_names_are_found():
    listed, injected, rig_env = _tester_environment()
    assert {"DVT_OUT", "DVT_RIG_CONTROL"} <= listed | injected
    assert "DVT_IAX_SECRET" in rig_env
    assert len(listed) > 100


def test_a_new_switch_is_caught(tmp_path, monkeypatch):
    """The detector itself: a tester-side file that reads an unknown variable shows up in _names_read_by_the_tester()."""
    probe = ROOT / "cases" / "zz_probe_tester_env.py"
    probe.write_text('import os\nX = os.environ.get("DVT_ZZ_PROBE_SWITCH", "")\n')
    try:
        assert "DVT_ZZ_PROBE_SWITCH" in _names_read_by_the_tester()
        listed, injected, rig_env = _tester_environment()
        assert "DVT_ZZ_PROBE_SWITCH" not in listed | injected | rig_env | set(HOST_ONLY_NAMES)
    finally:
        probe.unlink()
