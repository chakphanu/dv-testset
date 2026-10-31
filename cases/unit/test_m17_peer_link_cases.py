# SPDX-License-Identifier: MIT
"""The pure helpers of cases/proto/test_m17_peer_link.py (stage 2 group 1:
handshake, keepalive, teardown, ingress admission of a configured peer)."""
import importlib.util
import sys
from pathlib import Path

import pytest

from dvtest.proto import m17

pytestmark = pytest.mark.unit

PROTO = Path(__file__).resolve().parents[1] / "proto"


@pytest.fixture(scope="module")
def cases():
    sys.path.insert(0, str(PROTO))                       # m17_common
    try:
        spec = importlib.util.spec_from_file_location("m17_peer_link_cases", PROTO / "test_m17_peer_link.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(PROTO))
    return module


def test_peer_stream_is_a_valid_54_byte_stream(cases):
    packets = cases.peer_stream(sid=0x1234, n=5)
    assert [len(p) for p in packets] == [54] * 5
    parsed = [m17.parse_stream(p) for p in packets]
    assert all(p["crc_ok"] and p["stream_id"] == 0x1234 for p in parsed)
    assert [p["fn"] for p in parsed] == [0, 1, 2, 3, 4]
    assert [p["last"] for p in parsed] == [False] * 4 + [True]
    assert len({p["payload"] for p in parsed}) == 5
    assert cases.peer_stream(sid=0x1234, n=5, last=False)[-1] != packets[-1]


def test_peer_sms_is_a_40_byte_legacy_m17p(cases):
    sms = cases.peer_sms("hi")
    assert len(sms) == 40
    parsed = m17.parse_m17p(sms, trailer=False)
    assert parsed["lsf_crc_ok"] and parsed["payload_crc_ok"] and parsed["type"] == 0
    assert len(m17.with_trailer(sms, "M")) == 41


def test_retry_deltas_and_schedule(cases):
    deltas = cases.retry_deltas(100.0, [103.1, 109.0, 121.2, 145.0, 175.3, 205.0])
    assert deltas == [3.1, 5.9, 12.2, 23.8, 30.3, 29.7]
    assert cases.schedule_matches(deltas, [3, 6, 12, 24, 30, 30], 1.5)
    assert not cases.schedule_matches(deltas, [3, 6, 12, 24, 30, 30], 0.2)
    assert not cases.schedule_matches(deltas[:4], [3, 6, 12, 24, 30], 1.5)       # too few attempts
    assert not cases.schedule_matches(cases.retry_deltas(0.0, [10, 20, 30]), [3, 6, 12], 1.5)    # mrefd's flat 10 s
    assert cases.spaced([0.0, 3.001, 6.003]) == [3.001, 3.002]


def test_bad_ackn_packets_are_not_a_good_ackn(cases):
    good = m17.peer_ackn_packet("M17-DVT", "M")
    bad = cases.bad_ackn_packets("M17-DVT", "M")
    assert set(bad) == {"ackn4", "designation", "modules", "nack4", "no_nul"}
    assert bad["ackn4"] == b"ACKN" and bad["nack4"] == b"NACK"
    assert all(v != good for v in bad.values())
    assert m17.parse_peer(bad["designation"])["designation"] == "M17-DVX"
    assert m17.parse_peer(bad["modules"])["modules"] == "MZ"
    no_nul = bad["no_nul"]
    assert len(no_nul) == 37 and m17.parse_peer(no_nul)["nul_ok"] is False


def test_malformed_conns(cases):
    variants = cases.malformed_conns("M17-DVT", "M")
    names = [name for name, _, _ in variants]
    assert names == ["empty", "lower", "dup", "comma", "no_nul", "conn36", "conn38", "lstn37"]
    by_name = {name: (data, kind) for name, data, kind in variants}
    assert all(len(by_name[n][0]) == 37 for n in ("empty", "lower", "dup", "comma", "no_nul", "lstn37"))
    assert len(by_name["conn36"][0]) == 36 and len(by_name["conn38"][0]) == 38
    assert m17.classify(by_name["lstn37"][0]) == "other"
    assert m17.parse_peer(by_name["empty"][0])["modules"] == ""
    assert m17.parse_peer(by_name["lower"][0])["modules"] == "m"
    assert m17.parse_peer(by_name["dup"][0])["modules"] == "MM"
    assert m17.parse_peer(by_name["comma"][0])["modules"] == "M,M"
    assert m17.parse_peer(by_name["no_nul"][0])["nul_ok"] is False
    assert {kind for _, _, kind in variants} == {"refused", "size"}


@pytest.mark.parametrize("modules", ["M", "NO"])
def test_wrong_module_sets_all_differ(cases, modules):
    sets = cases.wrong_module_sets(modules)
    assert sets and all(set(v) != set(modules) for v in sets.values())
    assert all(v == v.upper() and v.isalpha() for v in sets.values())
    assert "Z" in sets.values() and any("Z" in v and len(v) > 1 for v in sets.values())
    assert ("partial" in sets) == (len(modules) > 1)
