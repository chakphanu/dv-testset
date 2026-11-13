# SPDX-License-Identifier: MIT
"""NETTHR1a/b configuration cases (docs/netthr1-cases.md, "Configuration"; DV-Router fce97cd3 and c8f9b4ed, image wip-eeb44b4): `dv-router config
check` must show the derived fabric cap, refuse a cap outside 4..=128 or below the planned children plus one, refuse web / admin HTTP caps outside their
ranges and accept the edges of the ranges. Host side, no rig, the machinery of cases/test_dp18_config.py: the rig template is rendered for gen compat1
and checked in a throw-away container (`--network none`, dvtest.tools.config_check). Every case needs DVT_NETTHR1=on (a skip naming it otherwise).

  C1  test_c1_default_cap_is_the_documented_formula   `fabric_max_sessions: N (default)` equals clamp(rooms + 19 + 9 + data rows + transcoder rows + 8, 4, 128) of the
                                                      rendered file; the exact hotspot preset prints 38 and the eight children of the K6b preset
  C2  test_c2_cap_outside_the_range_is_refused        3, 0, 129, 1000 refused: `node.fabric_max_sessions: must be 4..=128 (the router Net blocker pool supports
                                                      at most 128 sessions); got N`; -1 (not a count) refused
  C3  test_c3_cap_must_cover_the_planned_children     cap 4 names the requirement (`cap 4 is below R required for P planned children plus 1 reserved session`,
                                                      R = P + 1); cap R is accepted and printed `(configured)`, cap R - 1 is refused with the same numbers; the
                                                      hotspot preset needs 9 (8 children + 1) and 128 is accepted
  C4  test_c4_http_caps_range                         web / admin max_file_streams 1..=64 and login_slots 1..=16: the edges accepted, 0 and one above refused with
                                                      the message of http_caps.rs

  DVT_NETTHR1=on DVT_CFGLOAD_IMAGE=dv-router:wip-eeb44b4 DVT_SUT_GEN=compat1 DVT_SUT_CONFIG=<template> \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_netthr1_config.py
"""
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

import pytest

from dvtest import netthr1, sut_config
from dvtest.tools import config_check

pytestmark = pytest.mark.cfgload

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
NET = "172.30.0"
CAP_LINE = re.compile(r"^\s*fabric_max_sessions:\s*(\d+)\s*\((default|configured)\)")
CHILD_LINE = re.compile(r"^\s*child:\s*(\S+)\s*$")
BELOW = re.compile(r"cap (\d+) is below (\d+) required for (\d+) planned children plus 1 reserved session")
# the eight children of DV-Router's exact hotspot preset (crates/router/tests/real_children/hotspot.rs CHILDREN)
HOTSPOT_CHILDREN = {"dv-data-worker[pkt]", "dv-edge-radio", "dv-room[B]", "dv-svc-admin", "dv-svc-basemap", "dv-svc-iddb", "dv-svc-status", "dv-svc-web"}

RESULTS = {}


@pytest.fixture(scope="module", autouse=True)
def gate():
    if not netthr1.on():
        pytest.skip(netthr1.skip_reason())


@pytest.fixture(scope="module")
def image():
    value = os.environ.get("DVT_CFGLOAD_IMAGE")
    if not value:
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (an image with NETTHR1a/b, e.g. dv-router:wip-eeb44b4)")
    return value


def _need_gen():
    if sut_config.gen_value() != sut_config.COMPAT1:
        pytest.skip("DVT_SUT_GEN must be compat1")


@pytest.fixture(scope="module")
def rig_text():
    _need_gen()
    template = os.environ.get("DVT_SUT_CONFIG")
    if not template or not Path(template).is_file():
        pytest.skip("DVT_SUT_CONFIG is unset or not a file (the rig's template)")
    extras = [Path(p).read_text(encoding="utf-8") for p in sut_config.split_extras(os.environ.get("DVT_CFGLOAD_EXTRA", ""))]
    return sut_config.render(Path(template).read_text(encoding="utf-8"), NET, extras=extras, gen=sut_config.COMPAT1)


@pytest.fixture(scope="module")
def hotspot_text():
    _need_gen()
    return sut_config.render((SNIPPETS / "netthr1-hotspot.toml").read_text(encoding="utf-8"), NET, gen=sut_config.COMPAT1)


@pytest.fixture(scope="module", autouse=True)
def save_results():
    yield
    out = Path(os.environ["DVT_OUT"]) / "config-load" if os.environ.get("DVT_OUT") else None
    if out and RESULTS:
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "netthr1-config.json").write_text(json.dumps(RESULTS, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------- plumbing

def set_in_table(text, table, line):
    """`line` as the first line of [table] (the table is added at the end when the file has none)."""
    lines = text.split("\n")
    header = re.compile(r"^\s*\[" + re.escape(table) + r"\]\s*(#.*)?$")
    for n, existing in enumerate(lines):
        if header.match(existing):
            lines.insert(n + 1, line)
            return "\n".join(lines)
    return text.rstrip("\n") + "\n[%s]\n%s\n" % (table, line)


def check(image, text):
    """(output lines, exit code) of `dv-router config check` on `text` in a throw-away container."""
    with tempfile.TemporaryDirectory(prefix="dvt-netthr1-") as tmp:
        path = os.path.join(tmp, "dv-router.toml")
        Path(path).write_text(text, encoding="utf-8")
        os.chmod(path, 0o644)
        stand_ins = []
        for n, inside in enumerate(config_check.secret_file_paths(text)):
            host = os.path.join(tmp, "secret-%d" % n)
            Path(host).write_text("standin\n", encoding="utf-8")
            os.chmod(host, 0o600)
            stand_ins.append((host, inside))
        lines, code = config_check.run_check(image, path, NET, timeout_s=60.0, stand_ins=stand_ins, mode="check")
    assert lines, "the container printed nothing (is %s present?)" % image
    return lines, code


def errors_of(lines, code):
    return config_check.judge_check(lines, code)["parsed"]


UNPOSITIONED = re.compile(r"^(?:dv-router:\s*)?([a-z_]+(?:\.[a-z_]+)+):\s+(.*)$")


def refusals(lines, code):
    """Positioned errors, else the unpositioned `KEY: message` lines: the capacity refusal has no file:line:col (first rig run)."""
    out = list(errors_of(lines, code))
    if not out and code not in (0, None):
        for line in lines:
            m = UNPOSITIONED.match(line.strip())
            if m:
                out.append({"key": m.group(1), "message": m.group(2), "line": None})
    return out


def cap_line(lines):
    for line in lines:
        m = CAP_LINE.match(line.replace("dv-router:", "", 1))
        if m:
            return int(m.group(1)), m.group(2)
    return None


def with_cap(text, cap):
    return set_in_table(text, "node", "fabric_max_sessions = %s" % cap)


def note(case, **kv):
    RESULTS.setdefault(case, {}).update(kv)
    print("\n[RECORD] %s: %s" % (case, kv))


# ---------------------------------------------------------------- C1

def test_c1_default_cap_is_the_documented_formula(image, rig_text, hotspot_text):
    lines, code = check(image, rig_text)
    assert code == 0, "the rig template does not load: %s" % lines[:3]
    want = netthr1.derived_cap(tomllib.loads(rig_text))
    got = cap_line(lines)
    note("C1-rig", exit=code, cap=got, want=want)
    assert got is not None, "no `fabric_max_sessions:` line in the summary of config check: %s" % lines[:12]
    assert got == (want, "default"), "summary says %s, the formula over the rendered file gives %d (default)" % (got, want)
    lines, code = check(image, hotspot_text)
    assert code == 0, "the hotspot preset does not load: %s" % lines[:3]
    children = {m.group(1) for m in (CHILD_LINE.match(x) for x in lines) if m}
    got = cap_line(lines)
    note("C1-hotspot", exit=code, cap=got, children=sorted(children))
    assert got == (38, "default"), "hotspot preset: summary says %s, the documents give 38 (default)" % (got,)
    assert children == HOTSPOT_CHILDREN, "hotspot children: extra %s, missing %s" % (
        sorted(children - HOTSPOT_CHILDREN), sorted(HOTSPOT_CHILDREN - children))


# ---------------------------------------------------------------- C2

@pytest.mark.parametrize("cap", [3, 0, 129, 1000])
def test_c2_cap_outside_the_range_is_refused(image, rig_text, cap):
    lines, code = check(image, with_cap(rig_text, cap))
    parsed = errors_of(lines, code)
    note("C2-%d" % cap, exit=code, error=parsed[:1])
    assert code not in (0, None), "cap %d was accepted (exit %r)" % (cap, code)
    assert parsed, "cap %d: exit %r but no positioned error line: %s" % (cap, code, lines[:4])
    first = parsed[0]
    assert first["key"] == "node.fabric_max_sessions", "cap %d: error names %r: %s" % (cap, first["key"], first)
    want = "must be 4..=128 (the router Net blocker pool supports at most 128 sessions); got %d" % cap
    assert first["message"] == want, "cap %d: %r, want %r" % (cap, first["message"], want)


def test_c2_negative_cap_is_refused(image, rig_text):
    lines, code = check(image, with_cap(rig_text, -1))
    parsed = errors_of(lines, code)
    note("C2--1", exit=code, error=parsed[:1])
    assert code not in (0, None), "cap -1 was accepted"
    assert parsed, "cap -1: exit %r but no positioned error line: %s" % (code, lines[:4])


# ---------------------------------------------------------------- C3

def requirement(image, text):
    """(R, P) from the refusal of cap 4, which is below every real node's requirement."""
    lines, code = check(image, with_cap(text, 4))
    parsed = refusals(lines, code)
    for p in parsed:
        m = BELOW.search(p["message"])
        if m:
            return int(m.group(2)), int(m.group(3)), p
    return None, None, (parsed[:1] or lines[:3])


def cover(image, text, label, want_children=None):
    required, planned, first = requirement(image, text)
    note(label, required=required, planned=planned, first=first)
    assert required is not None, "%s: cap 4 gave no `below ... required` refusal: %s" % (label, first)
    assert first["key"] == "node.fabric_max_sessions"
    assert first["message"] == "cap 4 is below %d required for %d planned children plus 1 reserved session" % (required, planned)
    assert required == planned + 1, "%s: required %d is not planned %d + 1" % (label, required, planned)
    if want_children is not None:
        assert planned == want_children, "%s: %d planned children, the documents give %d" % (label, planned, want_children)
    lines, code = check(image, with_cap(text, required))
    assert code == 0, "%s: cap %d (= required) was refused: %s" % (label, required, refusals(lines, code)[:1] or lines[:3])
    assert cap_line(lines) == (required, "configured"), "%s: summary %s" % (label, cap_line(lines))
    lines, code = check(image, with_cap(text, required - 1))
    parsed = refusals(lines, code)
    assert code not in (0, None) and parsed, "%s: cap %d (required - 1) was accepted or gave no error" % (label, required - 1)
    assert parsed[0]["message"] == "cap %d is below %d required for %d planned children plus 1 reserved session" % (required - 1, required, planned), parsed[0]
    return required


def test_c3_cap_must_cover_the_planned_children(image, rig_text, hotspot_text):
    required = cover(image, rig_text, "C3-rig")
    if required <= 128:
        lines, code = check(image, with_cap(rig_text, 128))
        assert code == 0 and cap_line(lines) == (128, "configured"), "cap 128 not accepted: %s" % (errors_of(lines, code)[:1] or lines[:3])
    # the exact hotspot preset: eight children plus one tool slot = 9 (docs/project/testing.md: the lowest valid cap of the preset is 9)
    assert cover(image, hotspot_text, "C3-hotspot", want_children=8) == 9


# ---------------------------------------------------------------- C4

HTTP_KEYS = [
    ("max_file_streams", [(1, True), (64, True), (0, False), (65, False)],
     "must be 1..=64 (one reader thread per streamed body); got %d"),
    ("login_slots", [(1, True), (16, True), (0, False), (17, False)],
     "must be 1..=16 (about 19 MiB per password job); got %d"),
]


@pytest.mark.parametrize("section", ["web", "admin"])
@pytest.mark.parametrize("key,values,message", HTTP_KEYS, ids=[k[0] for k in HTTP_KEYS])
def test_c4_http_caps_range(image, rig_text, section, key, values, message):
    seen = {}
    for value, ok in values:
        lines, code = check(image, set_in_table(rig_text, section, "%s = %d" % (key, value)))
        parsed = errors_of(lines, code)
        seen[value] = {"exit": code, "error": parsed[:1]}
        if ok:
            assert code == 0, "%s.%s = %d was refused: %s" % (section, key, value, parsed[:1] or lines[:3])
        else:
            assert code not in (0, None) and parsed, "%s.%s = %d was accepted (exit %r)" % (section, key, value, code)
            assert parsed[0]["key"] == "%s.%s" % (section, key), "%s.%s = %d: error names %r" % (section, key, value, parsed[0]["key"])
            assert parsed[0]["message"] == message % value, "%s.%s = %d: %r" % (section, key, value, parsed[0]["message"])
    note("C4-%s.%s" % (section, key), values=seen)
