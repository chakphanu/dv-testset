# SPDX-License-Identifier: MIT
"""dvtest/mmdvm_stack.py (pure helpers of cases/test_mmdvm_stack.py), the rig snippet mmdvm-edges.toml and its run.sh wiring. Offline."""
import tomllib
from pathlib import Path

import pytest

from dvtest import mmdvm_stack, sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "mmdvm-edges.toml"
MODES = ("dstar", "dmr", "ysf", "p25", "nxdn")
ROOMS = {"dstar": "H", "dmr": "I", "ysf": "J", "p25": "G", "nxdn": "F"}
WORDS = {"dstar": ("dextra", "d-star", "dstar"), "dmr": ("dmr", "mmdvm", "homebrew", "dmrhb"), "ysf": ("ysf",),
         "p25": ("p25",), "nxdn": ("nxdn",)}

# rows as the public state of the first run showed them (synthetic callsigns); no room key, a module key, or a room key
ROWS = [{"callsign": "N0CALL", "edge": "dextra-1", "kind": "client", "protocol": "dextra", "room": "H"},
        {"callsign": "N0CALL", "edge": "dmrhb-1", "kind": "client", "protocol": "dmrhb", "module": "I"},
        {"callsign": "N0CALL", "edge": "ysf-1", "kind": "client", "protocol": "ysf", "room": "J"},
        {"callsign": "N0TEST", "edge": "dmrhb-1", "kind": "client", "protocol": "dmrhb", "room": "A"}]


def test_row_matches_needs_the_module_and_a_protocol_word():
    assert mmdvm_stack.row_matches(ROWS[0], "dstar", "H", WORDS)
    assert mmdvm_stack.row_matches(ROWS[1], "dmr", "I", WORDS)          # "module" is read when there is no "room"
    assert not mmdvm_stack.row_matches(ROWS[0], "dstar", "I", WORDS)    # right protocol, wrong module
    assert not mmdvm_stack.row_matches(ROWS[0], "ysf", "H", WORDS)      # right module, wrong protocol
    assert not mmdvm_stack.row_matches({}, "dstar", "H", WORDS)


def test_linked_modes_and_not_linked_with_the_first_run_state():
    seen = mmdvm_stack.linked_modes(ROWS, MODES, ROOMS, WORDS)
    assert seen == {"dstar", "dmr", "ysf"}
    assert mmdvm_stack.not_linked(MODES, seen) == ["nxdn", "p25"]       # the failure of the first run: both edges off
    # the modes the reflector offers are the ones that must be linked: nothing is missing, the check holds
    assert mmdvm_stack.not_linked(["dstar", "dmr", "ysf"], seen) == []
    assert mmdvm_stack.not_linked([], set()) == []
    assert mmdvm_stack.not_linked(["p25"], set()) == ["p25"]            # an offered mode that does not link is still found


def test_offered_modes_keeps_the_order_and_calls_probe_once_per_mode():
    calls = []

    def probe(mode):
        calls.append(mode)
        return mode not in ("p25", "nxdn")

    assert mmdvm_stack.offered_modes(MODES, probe) == ["dstar", "dmr", "ysf"]
    assert calls == list(MODES)
    assert mmdvm_stack.offered_modes(MODES, lambda m: False) == []


def test_snippet_serves_tg_31672_as_nxdn_f_and_p25_g():
    data = tomllib.loads(SNIPPET.read_text())
    assert data["edges"]["nxdn"] == {"enable": True, "tg_modules": "31672:F"}
    assert data["edges"]["p25"] == {"enable": True, "server_module": "G", "server_tg": 31672}
    # the same values as the soak snippet and as the module / TG defaults of the case
    soak = tomllib.loads((ROOT / "rig" / "config" / "dv-router" / "soak-edges.toml").read_text())
    assert soak["edges"] == data["edges"]
    case = (ROOT / "cases" / "test_mmdvm_stack.py").read_text()
    assert 'p25=os.environ.get("DVT_P25_MODULE", "G")' in case and 'nxdn=os.environ.get("DVT_NXDN_MODULE", "F")' in case
    assert '"DVT_MMDVM_P25_TG", "31672"' in case and '"DVT_MMDVM_NXDN_TG", "31672"' in case


def test_snippet_turns_the_auto_edges_of_the_template_on():
    template = 'schema = 1\n[node]\ncallsign = "XLX999"\n\n[edges.nxdn]\nenable = "auto"\n\n[edges.p25]\nenable = "auto"\n'
    out = tomllib.loads(sut_config.render(template, "172.30.0", extras=[SNIPPET.read_text()]))
    assert out["edges"]["nxdn"]["enable"] is True and out["edges"]["nxdn"]["tg_modules"] == "31672:F"
    assert out["edges"]["p25"]["enable"] is True and out["edges"]["p25"]["server_module"] == "G"
    plain = tomllib.loads(sut_config.render(template, "172.30.0"))
    assert plain["edges"]["nxdn"]["enable"] == "auto" and "tg_modules" not in plain["edges"]["nxdn"]    # without it: not served


def test_run_sh_adds_the_snippet_to_a_native_mmdvm_launch_only():
    run = (ROOT / "scripts" / "run.sh").read_text()
    start = run.index("if [ \"$has_mmdvm\" = 1 ]; then")
    block = run[start:run.index("\nfi\n", run.index("mmdvm-edges.toml", start)) + 4]
    assert '"${DVT_SUT_LAUNCH:-direct}" = native' in block
    assert "rig/config/dv-router/soak-edges.toml" in block            # not added twice when the soak snippet is listed
    assert block.count("mmdvm-edges.toml") >= 3
