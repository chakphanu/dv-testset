# SPDX-License-Identifier: MIT
"""dvtest/dp18cfg.py (DP18 configuration refusals, cases/test_dp18_config.py) against the snippet and fake verdicts: no docker, no rig."""
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from dvtest import dp18cfg as dc  # noqa: E402

pytestmark = pytest.mark.unit

SNIPPET = Path(__file__).resolve().parents[2] / "rig" / "config" / "dv-router" / "dp18-origin.toml"
CASE = {c.id: c for c in dc.CONFIG_CASES}


def text():
    return SNIPPET.read_text()


def doc_of(case_id):
    return tomllib.loads(CASE[case_id].mutate(text()))["edges"]["aprsis"]


def test_every_mutation_applies_and_stays_toml():
    base = text()
    assert len({c.id for c in dc.CONFIG_CASES}) == len(dc.CONFIG_CASES)
    for case in dc.CONFIG_CASES:
        mutated = case.mutate(base)
        tomllib.loads(mutated)
        assert (mutated != base) == (case.id != "accept-baseline"), case.id
        assert case.verdict in ("reject", "accept") and (case.verdict == "accept") == (case.key is None), case.id
    assert sum(1 for c in dc.CONFIG_CASES if c.verdict == "reject") >= 20


def test_the_header_comments_do_not_confuse_the_split():
    pre, beacon, obj = dc._split(text())
    assert beacon.count("interval_secs") == 1 and obj.count("interval_secs") == 1
    assert pre.endswith("[edges.aprsis.beacon]\n") and obj.startswith("\n[[edges.aprsis.objects]]\n")
    assert pre + beacon + obj == text()


def test_name_cases_put_the_value_in_the_one_object():
    assert doc_of("name-callsign-ssid")["objects"][0]["name"] == "N0CALL-7"
    assert doc_of("name-10-chars")["objects"][0]["name"] == "DVTOBJ1234"
    assert doc_of("name-leading-space")["objects"][0]["name"] == " DVTOBJ1"
    assert doc_of("name-trailing-space")["objects"][0]["name"] == "DVTOBJ1 "
    assert doc_of("name-non-ascii")["objects"][0]["name"] == "DVTOBJ\u00e9"
    assert [o["name"] for o in doc_of("name-duplicate")["objects"]] == ["DVTOBJ1", "DVTOBJ1"]
    assert len(doc_of("accept-name-9-chars")["objects"][0]["name"]) == 9
    assert doc_of("accept-name-gateway-call")["objects"][0]["name"] == dc.GATEWAY == "N0TEST-5"


def test_comment_boundaries_are_exact_as_sent():
    for cid, total in (("object-comment-44", 44), ("accept-object-comment-43", 43)):
        o = doc_of(cid)["objects"][0]
        assert len(dc.object_comment(o["frequency"], o["room"], o["comment"])) == total, cid
    assert len(doc_of("beacon-comment-44")["beacon"]["comment"]) == 44
    assert len(doc_of("accept-beacon-comment-43")["beacon"]["comment"]) == 43
    assert dc.object_comment("144.800MHz", "B", "dvt") == "144.800MHz room=B dvt"
    assert dc.object_comment("", None, "x") == "x" and dc.object_comment("", None, "") == ""


def test_numeric_cases_touch_only_their_part():
    assert doc_of("object-interval-599")["objects"][0]["interval_secs"] == 599 and doc_of("object-interval-599")["beacon"]["interval_secs"] == 600
    assert doc_of("beacon-interval-599")["beacon"]["interval_secs"] == 599 and doc_of("beacon-interval-599")["objects"][0]["interval_secs"] == 600
    assert doc_of("object-interval-86401")["objects"][0]["interval_secs"] == 86401
    z = doc_of("object-position-zero")
    assert (z["objects"][0]["lat_e7"], z["objects"][0]["lon_e7"]) == (0, 0) and z["beacon"]["lat_e7"] == 105000000
    b = doc_of("beacon-position-zero")
    assert (b["beacon"]["lat_e7"], b["beacon"]["lon_e7"]) == (0, 0) and b["objects"][0]["lat_e7"] == 105500000
    lz = doc_of("accept-lat-zero-only")["objects"][0]
    assert lz["lat_e7"] == 0 and lz["lon_e7"] != 0
    assert doc_of("frequency-short")["objects"][0]["frequency"] == "144.80MHz"


def test_object_counts_and_distinct_names():
    for cid, n in (("objects-17", 17), ("accept-objects-16", 16)):
        objs = doc_of(cid)["objects"]
        assert len(objs) == n and len({o["name"] for o in objs}) == n
        assert all(o["lat_e7"] != 0 for o in objs)


def test_stand_in_text_is_numeric_only_for_the_passcode_file():
    assert dc.stand_in_text("/etc/dv-router/aprs-secrets/passcode") == "-1\n"
    assert dc.stand_in_text("/etc/dv-router/web-secrets/password") is None


def verdict(valid, parsed=()):
    return {"valid": valid, "unsupported": False, "errors": [] if valid else ["x"], "parsed": list(parsed)}


def err(key, message):
    return {"file": "f", "line": 1, "col": 1, "key": key, "message": message}


def test_config_problems_judge_reject_and_accept():
    name = CASE["name-callsign-ssid"]
    good = err("edges.aprsis.objects[0].name", "object name must not impersonate a station callsign")
    assert dc.config_problems(name, verdict(False, [good]), 2) == []
    assert "SILENT SUCCESS" in dc.config_problems(name, verdict(True), 0)[0]
    assert dc.config_problems(name, verdict(False, []), 2)[0].startswith("no `FILE:LINE:COL")
    assert dc.config_problems(name, verdict(False, [err("edges.ax25.port[0]", "x")]), 2)[0].startswith("no error names edges.aprsis")
    assert dc.config_problems(name, verdict(False, [err("edges.aprsis.beacon", "m")]), 2)[0].startswith("no error names 'name'")
    assert dc.config_problems(name, verdict(False, [err("edges.aprsis.objects[0].name", "other text")]), 2)[0].startswith("the message does not mention")
    pos = CASE["beacon-position-zero"]
    assert dc.config_problems(pos, verdict(False, [err("edges.aprsis.beacon", "enabled beacon requires a non-null configured position")]), 2) == []
    # an unrelated error before the right one does not hide it
    assert dc.config_problems(name, verdict(False, [err("data.worker[0]", "z"), good]), 2) == []
    base = CASE["accept-baseline"]
    assert dc.config_problems(base, verdict(True), 0) == []
    assert dc.config_problems(base, verdict(False, [good]), 2)[0].startswith("expected to load")
    assert dc.config_problems(base, {"unsupported": True}, 2)[0].startswith("the image has no")
