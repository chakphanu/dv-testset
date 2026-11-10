# SPDX-License-Identifier: MIT
"""DP6c configuration (docs/dp6c-cases.md, "Configuration"): `dv-router config check` through the real launcher loader. The path `edge:aprsis -> room:B` with
`types = ["weather","telemetry"]` loads (review 244 D3: the path the F1 defect broke); a path with an unknown type is refused at load, and so are the pairs the launcher
validation forbids (a room source with weather, room -> edge:aprsis with weather: data.md 8.2). Host side, no rig, the machinery of cases/test_dp11e_config.py: the rig
template plus data-ident.toml, dp15-aprsis.toml, dp6c-aprsis-filter.toml and dp6c-aprsis-wx.toml (one mutation per case, dvtest/dp6c.py CONFIG_CASES), checked in a
throw-away container (`--network none`).

  DVT_CFGLOAD_IMAGE=<image with DP6c> DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_dp6c_config.py
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from dvtest import aprs_passcode, dp6c, sut_config
from dvtest.tools import config_check

import test_config_load as tcl

pytestmark = pytest.mark.cfgload

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE_FILES = ("data-ident.toml", "dp15-aprsis.toml", "dp6c-aprsis-filter.toml")
MUTATED = SNIPPETS / "dp6c-aprsis-wx.toml"


def stand_in_content(inside):
    """What the config check reads from the file at `inside`. The generic stand-in (`standin`) is refused for the APRS-IS passcode file: the router validates its CONTENT
    (`invalid APRS-IS passcode file content`), so it gets the fixture passcode of the DV-Router login, as run.sh writes it (dvtest/aprs_passcode.py --dvr)."""
    if os.path.basename(inside) == "passcode":
        return "%d\n" % aprs_passcode.aprs_passcode(aprs_passcode.DVR_LOGIN)
    return "standin\n"


def run_config_check(image, text):
    """(verdict, exit code) of `dv-router config check` on `text`, as cases/test_config_load.py run_config_check, with stand-ins of the right content."""
    with tempfile.TemporaryDirectory(prefix="dvt-cfgload-") as tmp:
        path = os.path.join(tmp, "dv-router.toml")
        Path(path).write_text(text, encoding="utf-8")
        os.chmod(path, 0o644)
        stand_ins = []
        for n, inside in enumerate(config_check.secret_file_paths(text)):
            host = os.path.join(tmp, "secret-%d" % n)
            Path(host).write_text(stand_in_content(inside), encoding="utf-8")
            os.chmod(host, 0o600)
            stand_ins.append((host, inside))
        lines, code = config_check.run_check(image, path, tcl.NET, timeout_s=60.0, stand_ins=stand_ins, mode="check")
    assert lines, "the container printed nothing (is %s present?)" % image
    return config_check.judge_check(lines, code), code


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
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (an image with DP6c)")
    return value


@pytest.fixture(scope="module")
def results():
    rows = {}
    yield rows
    out = Path(os.environ["DVT_OUT"]) / "config-load" if os.environ.get("DVT_OUT") else None
    if out:
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "dp6c-config.json").write_text(json.dumps(list(rows.values()), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass


@pytest.mark.parametrize("case", dp6c.CONFIG_CASES, ids=[c.id for c in dp6c.CONFIG_CASES])
def test_dp6c_config(case, image, template, results):
    extras = [(SNIPPETS / name).read_text(encoding="utf-8") for name in BASE_FILES] + [case.mutate(MUTATED.read_text(encoding="utf-8"))]
    rendered = sut_config.render(template, tcl.NET, extras=extras, gen=sut_config.COMPAT1)
    verdict, code = run_config_check(image, rendered)
    assert "aprsis-wx" in rendered, "the snippet did not reach the rendered file"
    problems = dp6c.config_problems(case, verdict, code)
    first = (verdict.get("parsed") or [None])[0]
    results[case.id] = {"id": case.id, "what": case.what, "expect": case.verdict, "exit_code": code, "error": first, "problems": problems}
    print("\n%s [%s] exit=%r %s" % (case.id, case.verdict, code, (verdict["errors"] or ["loaded"])[0]))
    assert not problems, "%s: %s" % (case.id, "; ".join(problems))
