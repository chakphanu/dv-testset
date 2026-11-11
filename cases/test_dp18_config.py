# SPDX-License-Identifier: MIT
"""DP18 configuration refusals (docs/dp18-cases.md, "Configuration"): `dv-router config check` must refuse a bad node object or beacon
(`[edges.aprsis.beacon]`, `[[edges.aprsis.objects]]`) with an error that names the field, and load the good ones. Host side, no rig, the same
machinery as cases/test_dp11e_config.py: the rig template is rendered for gen compat1 with the snippets data-ident.toml, dp15-aprsis.toml and
rig/config/dv-router/dp18-origin.toml (one mutation per case, dvtest/dp18cfg.py CONFIG_CASES) and checked in a throw-away container
(`--network none`, dvtest.tools.config_check).

Rules covered (aprsis_origin.rs): object name 1-9 printable ASCII bytes without outer spaces and not a station callsign (the gateway's own
call excepted, case-insensitive), unique names, at most 16 objects, interval_secs 600-86400, lat and lon not both 0, frequency `ddd.dddMHz`,
the comment as sent at most 43 bytes (objects: frequency + room + comment). Cases the source does not refuse are accept rows.

  DVT_CFGLOAD_IMAGE=<image with DP18> DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_dp18_config.py
"""
import json
import os
from pathlib import Path

import pytest

from dvtest import dp18cfg, sut_config

import test_config_load as tcl

pytestmark = pytest.mark.cfgload

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
SNIPPET = SNIPPETS / "dp18-origin.toml"
BASE_EXTRAS = ("data-ident.toml", "dp15-aprsis.toml")      # the data worker (carriers default to all) and the APRS-IS edge with its login


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
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (an image with DP18)")
    return value


@pytest.fixture(scope="module")
def results():
    rows = {}
    yield rows
    out = Path(os.environ["DVT_OUT"]) / "config-load" if os.environ.get("DVT_OUT") else None
    if out:
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "dp18-config.json").write_text(json.dumps(list(rows.values()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass


@pytest.mark.parametrize("case", dp18cfg.CONFIG_CASES, ids=[c.id for c in dp18cfg.CONFIG_CASES])
def test_dp18_config(case, image, template, results):
    extras = [(SNIPPETS / name).read_text(encoding="utf-8") for name in BASE_EXTRAS]
    extras.append(case.mutate(SNIPPET.read_text(encoding="utf-8")))
    rendered = sut_config.render(template, tcl.NET, extras=extras, gen=sut_config.COMPAT1)
    verdict, code = tcl.run_config_check(image, rendered, stand_in_text=dp18cfg.stand_in_text)
    assert "edges.aprsis.beacon" in rendered, "the snippet did not reach the rendered file"
    problems = dp18cfg.config_problems(case, verdict, code)
    first = (verdict.get("parsed") or [None])[0]
    results[case.id] = {"id": case.id, "what": case.what, "expect": case.verdict, "exit_code": code, "error": first, "problems": problems}
    print("\n%s [%s] exit=%r %s" % (case.id, case.verdict, code, (verdict["errors"] or ["loaded"])[0]))
    assert not problems, "%s: %s" % (case.id, "; ".join(problems))
