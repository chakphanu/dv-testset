# SPDX-License-Identifier: MIT
"""dvtest/cfgload.py: the mutation table and the verdict rules of the config-load cases (no docker)."""
import pytest

from dvtest import cfgload
from dvtest.tools import config_check as cc

pytestmark = pytest.mark.unit

MINI = """[node]
callsign = "XLX999"

[log]
level = "info"

[access]
allow = []
deny = []

[edges.dmrhb]
enable = true

[edges.xlx]
enable = true

[edges.ysf]
enable = true

[callhome]
enable = false

[admin]
enable = false

[web]
enable = false
"""


def test_every_case_applies_to_a_template_with_the_rig_tables():
    seen = set()
    for case in cfgload.CASES:
        assert case.id not in seen
        seen.add(case.id)
        text, line = cfgload.apply(case, MINI)
        if case.id == "baseline":
            assert text == MINI and line is None
        else:
            assert text != MINI, case.id
            assert line is not None and 1 <= line <= len(text.split("\n"))
    for case in cfgload.CASES:
        assert case.pair in seen or case.pair == "", case.id
        assert case.verdict in (cfgload.REJECT, cfgload.ACCEPT)


def test_required_cases_are_present():
    ids = {c.id for c in cfgload.CASES}
    for need in ("admin-totp", "web-operator_totp", "xlx-peer-no-kind", "edges-mmdvm", "fabric-acl", "access-allow_file",
                 "callhome-interlink_file", "web-unknown-key", "web-wrong-type", "baseline"):
        assert need in ids, need
    assert all(c.key for c in cfgload.CASES if c.verdict == cfgload.REJECT and c.id != "toml-syntax")


def test_mutations_land_on_the_reported_line():
    text, line = cfgload.apply(cfgload.by_id("web-operator_totp"), MINI)
    assert text.split("\n")[line - 1] == "operator_totp = true" and text.split("\n")[line - 2] == "[web]"
    text, line = cfgload.apply(cfgload.by_id("edges-mmdvm"), MINI)
    assert text.split("\n")[line - 1] == "[edges.mmdvm]"
    text, line = cfgload.apply(cfgload.by_id("web-wrong-type"), MINI)
    assert text.split("\n")[line - 1] == 'enable = "yes"' and 'enable = false' not in text.split("[web]")[1]
    text, line = cfgload.apply(cfgload.by_id("xlx-peer-no-kind"), MINI)
    assert text.split("\n")[line - 1] == "[[edges.xlx.peer]]" and 'kind = "xlx"' not in text
    text, _ = cfgload.apply(cfgload.by_id("xlx-peer-kind"), MINI)
    assert 'kind = "xlx"' in text


def test_missing_anchor_is_a_mutation_error():
    with pytest.raises(cfgload.MutationError):
        cfgload.apply(cfgload.by_id("web-operator_totp"), "[node]\ncallsign = 'X'\n")


def _verdict(lines, code):
    return cc.judge_check(lines, code), code


GOOD = ["dv-router: /etc/dv-router/dv-router.toml:170:1: web.operator_totp: unknown field `operator_totp`, expected `totp_required`"]


def test_reject_ok_when_file_line_key_and_message_are_there():
    case = cfgload.by_id("web-operator_totp")
    v, code = _verdict(GOOD, 2)
    assert cfgload.evaluate(case, v, code, 170) == []
    row = cfgload.record(case, v, code, 170, [])
    assert row["ok"] and row["exit_code"] == 2 and row["error_key"] == "web.operator_totp" and not row["loaded"]


def test_reject_silent_success_is_the_loudest_problem():
    case = cfgload.by_id("web-operator_totp")
    v, code = _verdict(["configuration: /etc/dv-router/dv-router.toml"], 0)
    problems = cfgload.evaluate(case, v, code, 170)
    assert len(problems) == 1 and problems[0].startswith("SILENT SUCCESS")


@pytest.mark.parametrize("lines, code, expect", [
    (GOOD, 3, "exit status 3"),
    (["dv-router: invalid thing"], 2, "no `FILE:LINE:COL"),
    (["dv-router: other.toml:170:1: web.operator_totp: unknown field"], 2, "does not name the file"),
    (GOOD, 2, "names line 170"),                         # expected line 171 below
    (["dv-router: /etc/dv-router/dv-router.toml:170:1: web.enabel: unknown field `enabel`"], 2, "names key 'web.enabel'"),
    (["dv-router: /etc/dv-router/dv-router.toml:170:1: web.operator_totp: unknown field `operator_totp`"], 2, "mention"),
])
def test_reject_problems(lines, code, expect):
    case = cfgload.by_id("web-operator_totp")
    v, c = _verdict(lines, code)
    problems = cfgload.evaluate(case, v, c, 171 if "names line" in expect else 170)
    assert any(expect in p for p in problems), problems


def test_accept_and_unsupported():
    case = cfgload.by_id("web-totp_required")
    assert cfgload.evaluate(case, *_verdict(["configuration: x"], 0), None) == []
    assert cfgload.evaluate(case, *_verdict(GOOD, 2), None)
    v, code = _verdict(["usage: dv-router --config FILE"], 2)
    assert "no `config check`" in cfgload.evaluate(case, v, code, None)[0]
    assert "no `config check`" in cfgload.evaluate(cfgload.by_id("admin-totp"), v, code, 1)[0]
