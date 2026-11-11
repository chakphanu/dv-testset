# SPDX-License-Identifier: MIT
"""COMPAT1 config-load cases: an old key or shape must fail with a clear error, never be ignored (docs/config-load-cases.md).

Host side only, no rig: the rig's template (env DVT_SUT_CONFIG) is rendered for gen compat1 through dvtest.sut_config, one mutation
(dvtest.cfgload.CASES) is applied to the rendered text, and `dv-router config check FILE` runs on it in a throw-away container
(dvtest.tools.config_check: --network none, read-only root, no capabilities, the dvtest.limits caps, one container at a time).
Every rejected case must exit non-zero with an error line naming the file, the line and the key and a non-empty message; the valid
controls (the same thing in its new spelling) and the unmutated baseline must load. The exact outputs go to
$DVT_OUT/config-load/results.json (else the pytest tmp dir).

  DVT_CFGLOAD_IMAGE=dv-router:wip-488b89d DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=/path/to/rig-dv-router.toml \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_config_load.py
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from dvtest import cfgload, sut_config
from dvtest.tools import config_check

pytestmark = pytest.mark.cfgload

NET = "172.30.0"


@pytest.fixture(scope="module")
def image():
    value = os.environ.get("DVT_CFGLOAD_IMAGE")
    if not value:
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (a compat1 image, e.g. dv-router:wip-488b89d)")
    return value


@pytest.fixture(scope="module")
def rendered():
    template = os.environ.get("DVT_SUT_CONFIG")
    if not template:
        pytest.skip("DVT_SUT_CONFIG is unset (the rig's template, e.g. /path/to/rig-dv-router.toml)")
    if not Path(template).is_file():
        pytest.skip("DVT_SUT_CONFIG=%s is not a file" % template)
    if sut_config.gen_value() != sut_config.COMPAT1:
        pytest.skip("DVT_SUT_GEN must be compat1 (the old keys are refused only there)")
    extras = sut_config.split_extras(os.environ.get("DVT_CFGLOAD_EXTRA", ""))
    texts = [Path(p).read_text(encoding="utf-8") for p in extras]
    return sut_config.render(Path(template).read_text(encoding="utf-8"), NET, extras=texts, gen=sut_config.COMPAT1)


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    rows = {}
    yield rows
    out_dir = Path(os.environ["DVT_OUT"]) / "config-load" if os.environ.get("DVT_OUT") else tmp_path_factory.getbasetemp()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "results.json").write_text(json.dumps({"image": os.environ.get("DVT_CFGLOAD_IMAGE"), "cases": list(rows.values())},
                                                         indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def run_config_check(image, text, stand_in_text=None):
    """(verdict, exit code) of `dv-router config check` on `text`; secret-file stand-ins are mounted as config_check does.
    `stand_in_text(inside_path)` may return another content for a stand-in (the APRS-IS passcode file is checked for its content: "-1")."""
    with tempfile.TemporaryDirectory(prefix="dvt-cfgload-") as tmp:
        path = os.path.join(tmp, "dv-router.toml")
        Path(path).write_text(text, encoding="utf-8")
        os.chmod(path, 0o644)
        stand_ins = []
        for n, inside in enumerate(config_check.secret_file_paths(text)):
            host = os.path.join(tmp, "secret-%d" % n)
            Path(host).write_text((stand_in_text(inside) if stand_in_text else None) or "standin\n", encoding="utf-8")
            os.chmod(host, 0o600)
            stand_ins.append((host, inside))
        lines, code = config_check.run_check(image, path, NET, timeout_s=60.0, stand_ins=stand_ins, mode="check")
    assert lines, "the container printed nothing (is %s present?)" % image
    return config_check.judge_check(lines, code), code


@pytest.mark.parametrize("case", cfgload.CASES, ids=[c.id for c in cfgload.CASES])
def test_config_load(case, image, rendered, results):
    text, expected_line = cfgload.apply(case, rendered)
    verdict, code = run_config_check(image, text)
    problems = cfgload.evaluate(case, verdict, code, expected_line)
    results[case.id] = cfgload.record(case, verdict, code, expected_line, problems)
    print("\n%s [%s] exit=%r %s" % (case.id, case.verdict, code, (verdict["errors"] or ["loaded"])[0]))
    assert not problems, "%s: %s" % (case.id, "; ".join(problems))
