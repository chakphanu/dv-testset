# SPDX-License-Identifier: MIT
"""DIGI1 configuration refusals (docs/digi1-cases.md, "Configuration"): `dv-router config check` must refuse a digipeating AX.25 port that breaks
a rule of the fill-in (digi.enable without effective TX, tx_per_min below 10, tx_burst below 3, a window or TTL out of range, a share that does not
match digi.enable, ports of one transmitter that disagree) with the operator sentence of DV-Router and the field it names, and load the good ones.
Host side, no rig, the same machinery as cases/test_dp18_config.py: the rig template is rendered for gen compat1 with the snippets data-ident.toml
and rig/config/dv-router/digi1-roomy.toml (one mutation per case, dvtest/digi1cfg.py CONFIG_CASES) and checked in a throw-away container
(`--network none`, dvtest.tools.config_check).

  DVT_CFGLOAD_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_digi1_config.py

Not gated by DVT_DIGI1: these cases only need an image that has DIGI1; on an image without it the baseline row fails (unknown key `digi`), which
is the answer. NOT RUN: written offline.
"""
import json
import os
from pathlib import Path

import pytest

from dvtest import digi1cfg, sut_config

import test_config_load as tcl

pytestmark = pytest.mark.cfgload

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"


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
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (an image with DIGI1, e.g. dv-router:wip-eeb44b4)")
    return value


@pytest.fixture(scope="module")
def results():
    rows = {}
    yield rows
    out = Path(os.environ["DVT_OUT"]) / "config-load" if os.environ.get("DVT_OUT") else None
    if out:
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "digi1-config.json").write_text(json.dumps(list(rows.values()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass


@pytest.mark.parametrize("case", digi1cfg.CONFIG_CASES, ids=[c.id for c in digi1cfg.CONFIG_CASES])
def test_digi1_config(case, image, template, results):
    extras = digi1cfg.render_extras(case, lambda name: (SNIPPETS / name).read_text(encoding="utf-8"))
    rendered = sut_config.render(template, tcl.NET, extras=extras, gen=sut_config.COMPAT1)
    assert "digi = " in rendered, "the snippet did not reach the rendered file"
    verdict, code = tcl.run_config_check(image, rendered)
    problems = digi1cfg.config_problems(case, verdict, code)
    first = (verdict.get("parsed") or [None])[0]
    results[case.id] = {"id": case.id, "what": case.what, "expect": case.verdict, "exit_code": code, "error": first, "problems": problems}
    print("\n%s [%s] exit=%r %s" % (case.id, case.verdict, code, (verdict["errors"] or ["loaded"])[0]))
    assert not problems, "%s: %s" % (case.id, "; ".join(problems))
