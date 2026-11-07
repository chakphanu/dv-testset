# SPDX-License-Identifier: MIT
"""rig/config/dv-router/usrp-rig.toml (the USRP endpoints A/B/C of the native launch), the helpers that read them back from a
rendered config (dvtest/sut_config.py, used by the rig_usrp fixture) and the way scripts/run.sh adds the snippet."""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from dvtest import sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "usrp-rig.toml"
SNIPPET_REL = "rig/config/dv-router/usrp-rig.toml"

# a tiny fictional template, not a copy of any real one
TEMPLATE = """\
[node]
callsign = "XLX999"
bind_ip = "<OWN_IP>"

[edges.usrp]
enable = "auto"
"""
OWN_ROWS = TEMPLATE + """
[[edges.usrp.endpoint]]
module = "A"
bind = "<OWN_IP>:35001"
peer = "<NET>.40:36001"
callsign = "N0TEST"
"""


def rendered(template=TEMPLATE, net="172.30.0"):
    return sut_config.render(template, net, extras=[SNIPPET.read_text(encoding="utf-8")])


# ---- the snippet

def test_snippet_has_exactly_the_three_rig_endpoints():
    doc = tomllib.loads(rendered())
    rows = doc["edges"]["usrp"]["endpoint"]
    assert [(r["module"], r["bind"], r["peer"], r["callsign"]) for r in rows] == [
        ("A", "172.30.0.11:34001", "172.30.0.40:32001", "N0TEST"),
        ("B", "172.30.0.11:34002", "172.30.0.40:32002", "N0TEST"),
        ("C", "172.30.0.11:34003", "172.30.0.30:32003", "N0TEST"),
    ]
    assert doc["edges"]["usrp"]["enable"] == "auto"


def test_snippet_uses_only_the_known_tokens_and_follows_the_subnet():
    assert set(sut_config.tokens(SNIPPET.read_text(encoding="utf-8"))) <= {"OWN_IP", "NET", "TESTER_IP"}
    rows = tomllib.loads(rendered(net="172.30.7"))["edges"]["usrp"]["endpoint"]
    assert [r["bind"] for r in rows] == ["172.30.7.11:34001", "172.30.7.11:34002", "172.30.7.11:34003"]
    assert rows[2]["peer"] == "172.30.7.30:32003"


def test_snippet_agrees_with_the_xlxd_usrp_template_of_the_import_launch():
    """The importer turns rig/config/xlxd.usrp.in into the rows; the snippet must stay the same three."""
    want = []
    for line in (ROOT / "rig" / "config" / "xlxd.usrp.in").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            module, bind_ip, bind_port, peer_ip, peer_port, callsign = line.replace("@NET@", "172.30.0").split()
            want.append((module, "%s:%s" % (bind_ip, bind_port), "%s:%s" % (peer_ip, peer_port), callsign))
    got = [(r["module"], r["bind"], r["peer"], r["callsign"]) for r in tomllib.loads(rendered())["edges"]["usrp"]["endpoint"]]
    assert got == want


def test_snippet_merges_into_a_template_without_the_usrp_table_or_with_it_off():
    plain = sut_config.render('[node]\ncallsign = "XLX999"\n', "172.30.0", extras=[SNIPPET.read_text(encoding="utf-8")])
    assert len(tomllib.loads(plain)["edges"]["usrp"]["endpoint"]) == 3
    off = sut_config.render('[edges.usrp]\nenable = false\n', "172.30.0", extras=[SNIPPET.read_text(encoding="utf-8")])
    doc = tomllib.loads(off)
    assert doc["edges"]["usrp"]["enable"] == "auto" and len(doc["edges"]["usrp"]["endpoint"]) == 3


def test_snippet_has_no_dynamic_hub_keys():
    """DVT_USRP_DYNAMIC=1 reaches the SUT as env DV_USRP_DYNAMIC (scripts/run.sh), not through this file."""
    usrp = tomllib.loads(rendered())["edges"]["usrp"]
    assert set(usrp) == {"enable", "endpoint"}


# ---- reading the ports back from a rendered config

def test_usrp_bind_ports_and_missing_ports():
    assert sut_config.usrp_bind_ports(rendered()) == {34001, 34002, 34003}
    assert sut_config.missing_usrp_ports(rendered()) == []
    assert sut_config.missing_usrp_ports(sut_config.render(TEMPLATE, "172.30.0")) == [34001, 34002, 34003]
    assert sut_config.missing_usrp_ports(sut_config.render(OWN_ROWS, "172.30.0")) == [34001, 34002, 34003]


def test_missing_ports_names_only_the_absent_ones():
    text = sut_config.render(TEMPLATE + '[[edges.usrp.endpoint]]\nmodule = "C"\nbind = "0.0.0.0:34003"\n'
                             'peer = "<TESTER_IP>:32003"\ncallsign = "N0TEST"\n', "172.30.0")
    assert sut_config.missing_usrp_ports(text) == [34001, 34002]


def test_missing_ports_cannot_tell_for_rows_without_bind_or_bad_text():
    noport = TEMPLATE + '[[edges.usrp.endpoint]]\nmodule = "C"\npeer = "<TESTER_IP>:32003"\ncallsign = "N0TEST"\n'
    assert sut_config.usrp_bind_ports(sut_config.render(noport, "172.30.0")) is None
    assert sut_config.missing_usrp_ports(sut_config.render(noport, "172.30.0")) == []
    assert sut_config.missing_usrp_ports("this is = = not toml") == []


# ---- scripts/run.sh adds the snippet (no docker: a stub `docker` fails, the checks end the run with exit 2)

def run_sh(tmp_path, *args, **env):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    docker = stub / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n")
    docker.chmod(0o755)
    full = {"PATH": "%s:%s" % (stub, os.environ.get("PATH", "")), "HOME": str(tmp_path), "DVT_CONTAINED": "1",
            "DVT_SUT_LAUNCH": "native", "XLXD_SUT_IMAGE": "img:dvt-no-such-image", "TRANSCODER_IMAGE": "x"}
    full.update(env)
    return subprocess.run(["bash", str(ROOT / "scripts" / "run.sh"), *args], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=60)


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "t.toml"
    path.write_text(TEMPLATE)
    return path


def early(tmp_path, template, *args, **env):
    """Stop right after the config check: DVT_UNSERVED_MODULE=M is refused by the native launch there."""
    return run_sh(tmp_path, *args, DVT_SUT_CONFIG=str(template), DVT_UNSERVED_MODULE="M", **env)


@pytest.mark.parametrize("args,env", [
    (("--profile", "allstar"), {}),
    (("--profile", "core,allstar"), {}),
    (("--profile", "load"), {}),
    (("--profile", "core"), {"DVT_SUT_USRP": "on"}),
])
def test_run_sh_native_adds_the_snippet_when_usrp_is_on(tmp_path, template, args, env):
    run = early(tmp_path, template, *args, **env)
    assert run.returncode == 2 and "DVT_UNSERVED_MODULE=M is refused" in run.stderr
    assert "adding %s" % SNIPPET_REL in run.stderr


@pytest.mark.parametrize("args,env", [
    (("--profile", "core"), {}),
    (("--profile", "allstar"), {"DVT_SUT_USRP": "off"}),
    (("--profile", "core"), {"DVT_SUT_USRP": "off"}),
])
def test_run_sh_native_leaves_the_config_alone_without_usrp(tmp_path, template, args, env):
    run = early(tmp_path, template, *args, **env)
    assert run.returncode == 2 and "DVT_UNSERVED_MODULE=M is refused" in run.stderr
    assert "usrp-rig.toml" not in run.stderr


def test_run_sh_adds_the_snippet_once_and_keeps_an_explicit_listing(tmp_path, template):
    run = early(tmp_path, template, "--profile", "allstar", DVT_SUT_CONFIG_EXTRA=SNIPPET_REL)
    assert run.returncode == 2 and "DVT_UNSERVED_MODULE=M is refused" in run.stderr and "adding" not in run.stderr


def test_run_sh_does_not_add_the_snippet_to_a_template_with_its_own_rows(tmp_path):
    own = tmp_path / "own.toml"
    own.write_text(OWN_ROWS)
    run = early(tmp_path, own, "--profile", "allstar")
    assert run.returncode == 2 and "DVT_UNSERVED_MODULE=M is refused" in run.stderr
    assert "own [[edges.usrp.endpoint]] rows" in run.stderr and "adding" not in run.stderr
    # a commented-out row is not a row
    commented = tmp_path / "commented.toml"
    commented.write_text(TEMPLATE + '# [[edges.usrp.endpoint]]\n')
    run = early(tmp_path, commented, "--profile", "allstar")
    assert "adding %s" % SNIPPET_REL in run.stderr


def test_run_sh_refuses_the_snippet_together_with_the_echo_usrp_peer(tmp_path, template):
    run = early(tmp_path, template, "--profile", "allstar",
                DVT_SUT_CONFIG_EXTRA="rig/config/dv-router/echo-usrp-peer.toml")
    assert run.returncode == 2 and "echo-usrp-peer.toml" in run.stderr and "34003" in run.stderr
    # the echo run itself (no USRP profile, DVT_SUT_USRP unset) is untouched
    run = early(tmp_path, template, "--profile", "interlink", DVT_SUT_CONFIG_EXTRA="rig/config/dv-router/echo-usrp-peer.toml")
    assert "DVT_UNSERVED_MODULE=M is refused" in run.stderr and "34003" not in run.stderr


def test_run_sh_native_no_longer_calls_dvt_sut_usrp_ignored(tmp_path, template):
    """The warning loop runs before the image checks, which end the stubbed run with exit 2."""
    run = run_sh(tmp_path, "--profile", "core", DVT_SUT_CONFIG=str(template), DVT_SUT_USRP="on", DVT_NXDN="on")
    assert run.returncode == 2 and "DVT_NXDN is set" in run.stderr      # the loop did run
    assert "DVT_SUT_USRP is set" not in run.stderr
    run = run_sh(tmp_path, "--profile", "core", DVT_SUT_CONFIG=str(template), DVT_SUT_USRP="maybe")
    assert "DVT_SUT_USRP is set" in run.stderr                           # an odd value still draws it


def test_run_sh_import_and_direct_launch_are_unchanged(tmp_path, template):
    run = run_sh(tmp_path, "--profile", "core", DVT_SUT_LAUNCH="import", DVT_SUT_USRP="on", DVT_SUT_CONFIG=str(template))
    assert run.returncode == 2 and "usrp-rig.toml" not in run.stderr and "DVT_SUT_USRP is set" not in run.stderr
    text = (ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
    assert text.count("usrp-rig.toml") >= 1
    # the snippet block is guarded by the native launch
    block = text[text.index("usrp_snippet=0"):text.index("cfg_opts=(")]
    assert '[ "${DVT_SUT_LAUNCH:-direct}" = native ]' in block


# ---- the image check (needs docker and the compat1 image: opt-in, never part of a plain unit run)

@pytest.mark.skipif(not os.environ.get("DVT_CFGLOAD_IMAGE") or not os.environ.get("DVT_SUT_CONFIG"),
                    reason="DVT_CFGLOAD_IMAGE and DVT_SUT_CONFIG (a template) name the image and template to check against")
def test_config_check_accepts_the_template_with_the_snippet():
    run = subprocess.run([sys.executable, "-m", "dvtest.tools.config_check", "--image", os.environ["DVT_CFGLOAD_IMAGE"],
                          "--template", os.environ["DVT_SUT_CONFIG"], "--gen", os.environ.get("DVT_SUT_GEN", "compat1"),
                          "--extra", str(SNIPPET)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "valid" in run.stdout and "INVALID" not in run.stdout
