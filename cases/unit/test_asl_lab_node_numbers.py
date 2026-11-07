# SPDX-License-Identifier: MIT
"""Guard for the AllStar lab's edge node numbers and register interval (no rig, no image).

DV-Router wip-1590f26 refuses `[[edges.allstar.node]] public = true` with a number below 2000 ("public node numbers must be at least
2000; private numbers cannot be registered") and a `[edges.allstar.register] interval` below 120 s. The lab edge is therefore node 2999
and the eight registration nodes are 2991..2998 (2997 and 2998 are the rate_limited ones; the DV-Router lead decided to keep the nodes public, not to use public = false: that
would stop testing the public-node path), with interval "120s". These tests keep the repo from drifting back: the snippets under
rig/config/dv-router/allstar-lab*.toml, the files and variants that depend on the numbers (password files written by scripts/run.sh,
the stand-in registrar's answer variants, the compose default) and the rendered config of every DVT_SUT_GEN generation.
"""
import importlib.util
import re
import tomllib
from pathlib import Path

import pytest

from dvtest import asl_register, sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = sorted((ROOT / "rig" / "config" / "dv-router").glob("allstar-lab*.toml"))
EDGE_NODE = 2999
REG_NODES = [2991, 2992, 2993, 2994, 2995, 2996, 2997, 2998]
VARIANTS = ["ok", "port", "http500", "notext", "slow", "oversize", "429ra", "503bad"]       # one per registration node, in order
MIN_PUBLIC_NUMBER = 2000
MIN_REGISTER_INTERVAL_S = 120.0
TEMPLATE = '[core]\nname = "unit"\n'                                    # the real template lives outside the repo


def _allstar(path):
    return (tomllib.loads(path.read_text()).get("edges") or {}).get("allstar") or {}


def _public_numbers(allstar):
    return [int(n["number"]) for n in allstar.get("node", []) if n.get("public") is True]


def test_snippets_are_found():
    names = {p.name for p in SNIPPETS}
    assert {"allstar-lab.toml", "allstar-lab-reg.toml", "allstar-lab-out.toml", "allstar-lab-lookup.toml"} <= names


@pytest.mark.parametrize("path", SNIPPETS, ids=lambda p: p.name)
def test_no_public_lab_node_below_2000(path):
    allstar = _allstar(path)
    for row in allstar.get("node", []):
        assert "number" in row, (path.name, row)
        if row.get("public") is True:
            assert int(row["number"]) >= MIN_PUBLIC_NUMBER, \
                "%s: public node %s is below %d (wip-1590f26 refuses it)" % (path.name, row["number"], MIN_PUBLIC_NUMBER)
    for row in allstar.get("outbound", []):
        assert int(row["from"]) >= MIN_PUBLIC_NUMBER, "%s: outbound from %s" % (path.name, row["from"])


def test_lab_edge_and_registration_nodes_are_the_renumbered_ones():
    assert _public_numbers(_allstar(ROOT / "rig/config/dv-router/allstar-lab.toml")) == [EDGE_NODE]
    assert _public_numbers(_allstar(ROOT / "rig/config/dv-router/allstar-lab-reg.toml")) == REG_NODES
    for row in _allstar(ROOT / "rig/config/dv-router/allstar-lab-out.toml")["outbound"]:
        assert int(row["from"]) == EDGE_NODE
    # the app_rpt lab nodes are not renumbered
    statics = [int(r["node"]) for r in _allstar(ROOT / "rig/config/dv-router/allstar-lab.toml")["static"]]
    assert statics == [199901, 199902, 299901, 299902]


def test_register_interval_is_at_least_120_s_in_the_snippet_text():
    path = ROOT / "rig/config/dv-router/allstar-lab-reg.toml"
    text = path.read_text()
    assert re.search(r'^\s*interval\s*=\s*"[^"]+"', text, re.M), "the registration snippet sets no interval"
    for match in re.finditer(r'^\s*interval\s*=\s*"([^"]+)"', text, re.M):
        assert asl_register.parse_interval(match.group(1)) >= MIN_REGISTER_INTERVAL_S, match.group(0)
    allstar = _allstar(path)
    assert asl_register.parse_interval(allstar["register"]["interval"]) >= MIN_REGISTER_INTERVAL_S
    # the lab snippets that do not register must not set a short interval either
    for other in SNIPPETS:
        interval = (_allstar(other).get("register") or {}).get("interval")
        if interval is not None:
            assert asl_register.parse_interval(interval) >= MIN_REGISTER_INTERVAL_S, other.name


@pytest.mark.parametrize("gen", sut_config.GENS)
def test_rendered_config_of_every_generation(gen):
    extras = [(ROOT / "rig/config/dv-router" / name).read_text() for name in (
        "allstar-lab.toml", "allstar-lab-out.toml", "allstar-lab-lookup.toml", "allstar-lab-reg.toml")]
    text = sut_config.render(TEMPLATE, "172.30.0", extras=extras, gen=gen)
    nodes, interval = asl_register.schedule(text)                        # public nodes in config order, register interval
    assert nodes == [EDGE_NODE] + REG_NODES, (gen, nodes)
    assert all(n >= MIN_PUBLIC_NUMBER for n in nodes)
    assert interval >= MIN_REGISTER_INTERVAL_S, (gen, interval)
    rendered = tomllib.loads(text)["edges"]["allstar"]
    assert min(int(n["number"]) for n in rendered["node"] if n.get("public") is True) >= MIN_PUBLIC_NUMBER
    assert "<NET>" not in text


def test_password_files_follow_the_node_numbers():
    run_sh = (ROOT / "scripts" / "run.sh").read_text()
    for path in (ROOT / "rig/config/dv-router/allstar-lab.toml", ROOT / "rig/config/dv-router/allstar-lab-reg.toml"):
        for row in _allstar(path).get("node", []):
            name = Path(row["password_file"]).name
            assert name == "node-%d.pw" % row["number"], (path.name, row)
            assert name in run_sh, "scripts/run.sh does not write %s" % name
            assert row["number"] >= MIN_PUBLIC_NUMBER or not row.get("public")
    assert "node-1999.pw" not in run_sh and not re.search(r"node-199[1-6]\.pw", run_sh)


def test_registrar_variants_map_the_eight_nodes_one_to_one():
    spec = importlib.util.spec_from_file_location("asl_lab_registrar_guard", ROOT / "rig" / "asl-lab-registrar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mapping = dict(item.split("=") for item in mod.DEFAULT_VARIANTS.split(","))
    assert mapping == {str(n): v for n, v in zip(REG_NODES, VARIANTS)}
    assert mod.variants().get("2993") == "http500"                       # (no env override in the unit run)
    assert mod.variants()["2997"] == "429ra" and mod.variants()["2998"] == "503bad"
    assert mod.extra_headers("429ra") == {"Retry-After": "300"} and mod.extra_headers("503bad") == {"Retry-After": "invalid"}
    assert mod.RETRY_AFTER_S == asl_register.RETRY_AFTER_S                # the case expects what the registrar sends


def test_compose_default_and_lab_default_are_the_edge_node():
    compose = (ROOT / "rig" / "compose.yml").read_text()
    assert compose.count("DVT_ASL_EDGE_NODE:-%d" % EDGE_NODE) == 2          # tester env and the registrar zone
    assert "DVT_ASL_EDGE_NODE:-1999" not in compose
    lab_py = (ROOT / "dvtest" / "asl_lab.py").read_text()
    assert 'env.get("DVT_ASL_EDGE_NODE", "%d")' % EDGE_NODE in lab_py


def test_case_module_uses_the_registration_nodes():
    cases = (ROOT / "cases" / "test_asl_edge.py").read_text()
    assert ('REG_NODES = {"2991": "ok", "2992": "port", "2993": "http500", "2994": "notext", "2995": "slow", "2996": "oversize",\n'
            '             "2997": "429ra", "2998": "503bad"}') in cases
    assert "EDGE_REG_NODE = %d" % EDGE_NODE in cases


def test_expectation_tables_name_exactly_the_lab_nodes():
    lab = {str(n) for n in REG_NODES}
    assert set(asl_register.REG_NEEDED) == lab and set(asl_register.REG_RESULTS) <= lab
    # the rate_limited nodes are the last two public nodes of the snippet, in config order (slot 7 and 8 of nine)
    assert [str(n) for n in REG_NODES[-2:]] == [k for k, v in asl_register.REG_RESULTS.items() if v == "rate_limited"]
    assert asl_register.window_budget_s([EDGE_NODE] + REG_NODES, 120.0) / 60 < 12      # the whole registration window stays under 12 minutes


def test_run_sh_writes_all_eight_password_files():
    run_sh = (ROOT / "scripts" / "run.sh").read_text()
    for number in [EDGE_NODE] + REG_NODES:
        assert "node-%d.pw" % number in run_sh
