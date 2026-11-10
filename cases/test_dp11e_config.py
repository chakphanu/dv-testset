# SPDX-License-Identifier: MIT
"""DP11e configuration refusals (docs/dp11e-cases.md, "Configuration"): `dv-router config check` must refuse a bad `[[edges.ax25.kiss_server]]` with
an error that names the key, and load the good ones. Host side, no rig, the same machinery as cases/test_config_load.py: the rig template is
rendered for gen compat1 with the snippet rig/config/dv-router/dp11e-kiss.toml (one mutation per case, dvtest/dp11e.py CONFIG_CASES) and checked
in a throw-away container (`--network none`, dvtest.tools.config_check).

Rules covered: `listen` is required (no default port); a non-loopback bind needs allow_from; a duplicate listen; at most four listeners;
keepalive_secs 0..300; channel 0 must be mapped; unique channels; existing AX.25 ports only; tx needs allow_src and a mapped TX port; allow_src
must be an SSID of the station base; name, max_clients, max_hops and tx_burst bounds; a loopback listener needs no allow_from. NOT covered: a
mapped TNC2 port (the rig has no TNC2 port to name).

  DVT_CFGLOAD_IMAGE=<image with DP11e> DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_dp11e_config.py
"""
import json
import os
from pathlib import Path

import pytest

from dvtest import dp11e, sut_config

import test_config_load as tcl

pytestmark = pytest.mark.cfgload

ROOT = Path(__file__).resolve().parents[1]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "dp11e-kiss.toml"


@pytest.fixture(scope="module")
def template():
    value = os.environ.get("DVT_SUT_CONFIG")
    if not value or not Path(value).is_file():
        pytest.skip("DVT_SUT_CONFIG is unset or not a file (the rig's template)")
    if sut_config.gen_value() != sut_config.COMPAT1:
        pytest.skip("DVT_SUT_GEN must be compat1")
    return Path(value).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def image():
    value = os.environ.get("DVT_CFGLOAD_IMAGE")
    if not value:
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (an image with DP11e)")
    return value


@pytest.fixture(scope="module")
def results():
    rows = {}
    yield rows
    out = Path(os.environ["DVT_OUT"]) / "config-load" if os.environ.get("DVT_OUT") else None
    if out:
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "dp11e-config.json").write_text(json.dumps(list(rows.values()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass


@pytest.mark.parametrize("case", dp11e.CONFIG_CASES, ids=[c.id for c in dp11e.CONFIG_CASES])
def test_dp11e_config(case, image, template, results):
    text = case.mutate(SNIPPET.read_text(encoding="utf-8"))
    rendered = sut_config.render(template, tcl.NET, extras=[text], gen=sut_config.COMPAT1)
    verdict, code = tcl.run_config_check(image, rendered)
    assert "kiss_server" in rendered, "the snippet did not reach the rendered file"
    problems = dp11e.config_problems(case, verdict, code)
    first = (verdict.get("parsed") or [None])[0]
    results[case.id] = {"id": case.id, "what": case.what, "expect": case.verdict, "exit_code": code, "error": first, "problems": problems}
    print("\n%s [%s] exit=%r %s" % (case.id, case.verdict, code, (verdict["errors"] or ["loaded"])[0]))
    assert not problems, "%s: %s" % (case.id, "; ".join(problems))
