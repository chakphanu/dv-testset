# SPDX-License-Identifier: MIT
"""The DIGI1 configuration cases (dvtest/digi1cfg.py, cases/test_digi1_config.py, rig/config/dv-router/digi1*.toml): every mutation applies once, changes only what
it says, the rendered config stays valid TOML, and the judge of a `config check` verdict is strict. No docker."""
import tomllib
from pathlib import Path

import pytest

from dvtest import digi1, digi1cfg, sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def read(name):
    return (SNIPPETS / name).read_text(encoding="utf-8")


CASES = digi1cfg.CONFIG_CASES


def port(case):
    extras = digi1cfg.render_extras(case, read)
    return tomllib.loads(sut_config.render(BASE, "172.30.0", extras=extras, gen=sut_config.COMPAT1))["edges"]["ax25"]


def test_the_case_ids_are_unique_and_cover_the_three_requested_rules():
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    assert {"digi-port-tx-off", "digi-master-tx-off", "digi-per-min-9", "digi-burst-2", "accept-rate-floor", "accept-baseline"} <= set(ids)
    assert sum(c.verdict == "reject" for c in CASES) >= 12 and sum(c.verdict == "accept" for c in CASES) >= 6


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_every_mutation_applies_and_renders_valid_toml_with_the_digi_row(case):
    ax = port(case)
    row = next(p for p in ax["port"] if p["name"] == "rf-a")
    assert "digi" in row
    base = tomllib.loads(sut_config.render(BASE, "172.30.0", extras=digi1cfg.render_extras(CASES[0], read), gen=sut_config.COMPAT1))["edges"]["ax25"]
    if case.id != "accept-baseline":
        assert ax != base, "the mutation of %s changed nothing" % case.id


def test_the_baseline_is_the_roomy_shape_that_the_rig_runs():
    row = port(CASES[0])["port"][0]
    assert (row["tx_per_min"], row["tx_burst"], row["tx_budget"], row["digi"], row["tx"]) == (30, 10, {"duty_pct": 20}, {"enable": True}, True)
    assert digi1.read_shape({"edges": {"ax25": port(CASES[0])}}).name == "roomy"


def test_the_rate_cases_move_exactly_the_floor_keys():
    by = {c.id: port(c)["port"][0] for c in CASES}
    assert (by["digi-per-min-9"]["tx_per_min"], by["digi-per-min-9"]["tx_burst"]) == (9, 10)
    assert (by["digi-burst-2"]["tx_per_min"], by["digi-burst-2"]["tx_burst"]) == (30, 2)
    assert (by["accept-rate-floor"]["tx_per_min"], by["accept-rate-floor"]["tx_burst"]) == (10, 3)
    assert by["digi-port-tx-off"]["tx"] is False and port(next(c for c in CASES if c.id == "digi-master-tx-off"))["tx_enable"] is False
    assert by["accept-digi-simulate"]["tx"] == "simulate"
    assert by["digi-shared-physical"]["physical"] == "radio1" and port(next(c for c in CASES if c.id == "digi-shared-physical"))["port"][1]["physical"] == "radio1"


def test_hints_are_the_operator_sentences_of_dv_router():
    sentences = {c.hint for c in CASES if c.hint}
    assert digi1cfg.M_RATE == "digipeating requires tx_per_min >= 10 and tx_burst >= 3 on this port"
    assert {digi1cfg.M_TX, digi1cfg.M_RATE, digi1cfg.M_SHARED, digi1cfg.M_WINDOW, digi1cfg.M_WINDOW_TTL, digi1cfg.M_TTL, digi1cfg.M_SHARE_ON,
            digi1cfg.M_SHARE_OFF} == sentences
    assert all(c.key in ("digi.enable", "digi.dedupe_secs", "digi.ttl_secs", "digi.share") for c in CASES if c.verdict == "reject")


def verdict(valid, parsed=(), unsupported=False):
    return {"valid": valid, "unsupported": unsupported, "errors": ["x"] if parsed else [], "parsed": list(parsed)}


def err(key, message):
    return {"file": "f", "line": 1, "col": 1, "key": key, "message": message}


def test_the_judge_accepts_the_right_refusal_and_names_every_way_to_be_wrong():
    case = next(c for c in CASES if c.id == "digi-per-min-9")
    good = verdict(False, [err("edges.ax25.port[0].digi.enable", digi1cfg.M_RATE)])
    assert digi1cfg.config_problems(case, good, 2) == []
    assert "SILENT SUCCESS" in digi1cfg.config_problems(case, verdict(True), 0)[0]
    assert "no `FILE:LINE:COL" in digi1cfg.config_problems(case, verdict(False), 2)[0]
    assert "no error names edges.ax25" in digi1cfg.config_problems(case, verdict(False, [err("node.callsign", "bad")]), 2)[0]
    assert "no error names 'digi.enable'" in digi1cfg.config_problems(case, verdict(False, [err("edges.ax25.port[0].tx_budget", "bad")]), 2)[0]
    assert "does not say" in digi1cfg.config_problems(case, verdict(False, [err("edges.ax25.port[0].digi.enable", digi1cfg.M_TX)]), 2)[0]
    assert "no `config check`" in digi1cfg.config_problems(case, verdict(False, unsupported=True), 2)[0]


def test_the_judge_for_an_accept_row():
    case = CASES[0]
    assert digi1cfg.config_problems(case, verdict(True), 0) == []
    assert "expected to load" in digi1cfg.config_problems(case, verdict(False, [err("edges.ax25", "unknown key digi")]), 2)[0]


def test_an_anchor_that_is_not_found_once_is_an_error_not_a_silent_no_op():
    with pytest.raises(ValueError):
        digi1cfg._plain("\nnot_there = 1\n", "x")(read("digi1-roomy.toml"))
    with pytest.raises(ValueError):
        digi1cfg._plain("tx_per_min", "x")(read("digi1-roomy.toml"))          # in the header comment too: more than once
