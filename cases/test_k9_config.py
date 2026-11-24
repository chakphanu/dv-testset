# SPDX-License-Identifier: MIT
"""K9a / K9b config cases (DV-Router 0a6ec3e5, images from wip-4732c43): the rendered configs of the rig stages must LOAD on a K9 image, and what
the image refuses must stay refused with the key named. Host side, no rig, the machinery of cases/test_config_load.py: the rig template is
rendered for DVT_SUT_GEN=k9 with each snippet set below and checked with `dv-router config check` in a throw-away container
(`--network none`, dvtest.limits caps, dvtest.tools.config_check).

  DVT_CFGLOAD_IMAGE=dv-router:wip-4732c43 DVT_SUT_GEN=k9 DVT_SUT_CONFIG=<template> \\
    scripts/contained.sh --name test -- python3 -m pytest -m cfgload cases/test_k9_config.py

Loads (the same sets the stages name; docs/rig.md "k9"): AllStar lab with its outbound rows (T6 T7 T8), the USRP rows A/B/C of the allstar
profile alone and with the L row of RFPOS1c / ASLDROP1, the echo set with a static USRP peer. Refused (the old dialect, `gen=compat1`):
outbound `mode`, repeated USRP callsigns without a name. Refused by the image until K9c: a `from_room` or `to_room` direction on a row that is
not an AllStar outbound row.
"""
import os
from pathlib import Path

import pytest

from dvtest import sut_config

import test_config_load as tcl

pytestmark = pytest.mark.cfgload

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"

LOADS = {
    "allstar-lab": ("allstar-lab",),
    "allstar-lab-out": ("allstar-lab", "allstar-lab-out"),
    "allstar-lab-all": ("allstar-lab", "allstar-lab-out", "allstar-lab-lookup", "allstar-lab-reg", "rfpos1c-usrp", "bundlefill1-edges"),
    "usrp-rig": ("usrp-rig",),
    "usrp-rig-and-lab-row": ("allstar-lab", "usrp-rig", "rfpos1c-usrp"),
    "echo-usrp-peer": ("echo", "echo-usrp-peer", "echo-peers", "xlx-peer"),
    "web-set": ("web", "admin", "totp-required", "data-ident"),
    "nxdn-peer": ("nxdn-peer",),
    "p25-peer-host": ("p25-peer-host",),
    "xlx-peers": ("xlx-peer", "xlx-peer2"),
}


@pytest.fixture(scope="module")
def image():
    value = os.environ.get("DVT_CFGLOAD_IMAGE")
    if not value:
        pytest.skip("DVT_CFGLOAD_IMAGE is unset (a K9 image, e.g. dv-router:wip-4732c43)")
    return value


@pytest.fixture(scope="module")
def template():
    value = os.environ.get("DVT_SUT_CONFIG")
    if not value or not Path(value).is_file():
        pytest.skip("DVT_SUT_CONFIG is unset or not a file (the rig's template)")
    if sut_config.gen_value() != sut_config.K9:
        pytest.skip("DVT_SUT_GEN must be k9")
    return Path(value).read_text(encoding="utf-8")


def _render(template, names, gen=None):
    extras = [(SNIPPETS / ("%s.toml" % n)).read_text(encoding="utf-8") for n in names]
    return sut_config.render(template, tcl.NET, extras=extras, gen=gen or sut_config.K9)


@pytest.mark.parametrize("name", sorted(LOADS))
def test_k9_rendered_set_loads(name, image, template):
    verdict, code = tcl.run_config_check(image, _render(template, LOADS[name]))
    assert verdict["valid"] and code == 0, "%s: %s" % (name, "; ".join(verdict["errors"]) or code)


def test_k9_outbound_mode_key_is_refused_in_the_old_dialect(image, template):
    verdict, code = tcl.run_config_check(image, _render(template, LOADS["allstar-lab-out"], gen=sut_config.COMPAT1))
    keys = [p["key"] for p in verdict["parsed"]]
    assert not verdict["valid"] and code == 2 and "edges.allstar.outbound[0].mode" in keys, (code, verdict["errors"])


def test_k9_repeated_usrp_callsign_without_a_name_is_refused_in_the_old_dialect(image, template):
    verdict, code = tcl.run_config_check(image, _render(template, LOADS["usrp-rig"], gen=sut_config.COMPAT1))
    text = " ".join(verdict["errors"])
    assert not verdict["valid"] and code == 2 and "duplicate endpoint key" in text, (code, verdict["errors"])


@pytest.mark.parametrize("direction", ["to_room", "from_room"])
def test_k9_a_non_both_direction_is_refused_on_a_usrp_row_until_k9c(direction, image, template):
    text = _render(template, LOADS["usrp-rig"]).replace('module = "A"\n', 'module = "A"\ndirection = "%s"\n' % direction, 1)
    assert 'direction = "%s"' % direction in text
    verdict, code = tcl.run_config_check(image, text)
    assert not verdict["valid"] and code == 2 and "K9c" in " ".join(verdict["errors"]), (code, verdict["errors"])


def test_k9_from_room_outbound_is_refused_and_both_loads(image, template):
    ok = _render(template, LOADS["allstar-lab-out"])
    assert 'direction = "both"' in ok and 'direction = "to_room"' in ok
    verdict, code = tcl.run_config_check(image, ok.replace('direction = "to_room"', 'direction = "from_room"', 1))
    assert not verdict["valid"] and "K9c" in " ".join(verdict["errors"]), (code, verdict["errors"])
