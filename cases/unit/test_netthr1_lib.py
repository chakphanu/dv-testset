# SPDX-License-Identifier: MIT
"""dvtest/netthr1.py and dvtest/dnshole.py (NETTHR1a/b, docs/netthr1-cases.md): the numbers are the DV-Router documents' (deployment.md "Process
schedulers", config.md "Fabric session admission"), written out by hand here, not read back from the module under test."""
import socket
import struct
import tomllib
from pathlib import Path

import pytest

from dvtest import dnshole, metrics, netthr1, sut_config

pytestmark = pytest.mark.unit

SNIPPETS = Path(__file__).resolve().parents[2] / "rig" / "config" / "dv-router"


def render(name, extras=()):
    text = (SNIPPETS / name).read_text(encoding="utf-8")
    return sut_config.render(text, "172.30.0", extras=[(SNIPPETS / e).read_text(encoding="utf-8") for e in extras], gen=sut_config.COMPAT1)


# ---------------------------------------------------------------- the ceilings of the documents

def test_http_ceiling_matches_the_two_documented_values():
    assert netthr1.http_ceiling(4, 16) == 27          # router defaults (deployment.md)
    assert netthr1.http_ceiling(2, 2) == 11           # hotspot defaults
    assert netthr1.http_ceiling(1, 1) == 3 + 1 + 1 + 3 + 1   # explicit 1 / 1 (K6b explicit_http_caps case: admin 1 slot, 1 stream = 9)
    assert netthr1.http_ceiling(1, 1) == 9


def test_launcher_and_net():
    assert netthr1.launcher_fixed() == 6
    assert netthr1.launcher_ceiling(63) == 6 + 65
    assert netthr1.launcher_ceiling(38) == 6 + 40


def test_router_k6b_sums_to_186_outside_net_and_251_in_total():
    fixed = 0
    for comm, copies in netthr1.ROUTER_K6B.items():
        if comm == "dv-router":
            continue
        fixed += netthr1.process_ceiling(comm, web=(16, 4), admin=(16, 4), cap=63) * copies
    fixed += netthr1.launcher_fixed()
    assert fixed == 186                                # deployment.md table
    total, unknown = netthr1.expected_total(netthr1.ROUTER_K6B, cap=63)
    assert (total, unknown) == (251, [])
    assert netthr1.headline("router-k6b") == 251


def test_hotspot_sums_to_55_and_95_and_101():
    total, unknown = netthr1.expected_total(netthr1.HOTSPOT, web=(2, 2), admin=(2, 2), cap=38)
    assert (total, unknown) == (95, [])
    link = netthr1.expected_total(netthr1.HOTSPOT_LINK, web=(2, 2), admin=(2, 2), cap=38, dns_edges=("dv-edge-link",))
    assert link == (101, [])
    assert netthr1.headline("hotspot") == 95 and netthr1.headline("hotspot-link") == 101


def test_dns_workers_only_for_edges_with_host_rows():
    assert netthr1.process_ceiling("dv-edge-m17") == 4
    assert netthr1.process_ceiling("dv-edge-m17", dns_edges=("dv-edge-m17",)) == 6
    assert netthr1.process_ceiling("dv-edge-dextra", dns_edges=("dv-edge-dextra",)) == 4     # no resolver pool in that edge
    assert netthr1.process_ceiling("dv-edge-allstar") is None
    assert netthr1.process_ceiling("something-else") is None
    assert netthr1.process_ceiling("dv-svc-iddb") == 6 and netthr1.process_ceiling("dv-svc-recorder") == 5


def test_profile_of_needs_the_exact_set():
    assert netthr1.profile_of(dict(netthr1.HOTSPOT)) == "hotspot"
    assert netthr1.profile_of({**netthr1.HOTSPOT, "dv-edge-link": 1}) == "hotspot-link"
    assert netthr1.profile_of({**netthr1.ROUTER_K6B}) == "router-k6b"
    assert netthr1.profile_of({**netthr1.ROUTER_K6B, "dv-codec-ambed": 1}) is None
    assert netthr1.profile_of({}) is None


# ---------------------------------------------------------------- the derived cap, and the two templates

def test_rooms_count():
    assert netthr1.rooms_count("A-Z") == 26 and netthr1.rooms_count("A-D") == 4 and netthr1.rooms_count("B") == 1
    assert netthr1.rooms_count("ABCDEFGHIJKLMNOPQRSTUVWXYZ") == 26 and netthr1.rooms_count("A-C,X") == 4


def test_derived_cap_documented_values():
    # config.md: one-room hotspot with its default pkt row = 38; 26 rooms with one data row and no transcoder row = 63;
    # DV-Router fabric_session_tests: A-D without rows = 41, rows 2..4 -> 40 + rows
    assert netthr1.derived_cap({"node": {"rooms": "B"}}) == 1 + 19 + 9 + 1 + 8 == 38
    assert netthr1.derived_cap({"node": {"rooms": "A-Z"}}) == 63
    assert netthr1.derived_cap({"node": {"rooms": "A-D"}}) == 41
    assert netthr1.derived_cap({"node": {"rooms": "A-D"}, "data": {"worker": [{"id": "a"}, {"id": "b"}]}}) == 42
    assert netthr1.derived_cap({"node": {"rooms": "A-Z"}, "transcoder": {"worker": [{"id": "ambed"}]}}) == 64
    assert netthr1.derived_cap({"node": {"rooms": "A-Z", "fabric_max_sessions": 9}}) == 9
    # answer 8: the launch's transcoder address is one more worker row: hotspot 39, router 64
    assert netthr1.derived_cap({"node": {"rooms": "B"}}, argv_workers=1) == 39
    assert netthr1.derived_cap({"node": {"rooms": "A-Z"}}, argv_workers=1) == 64
    assert 64 + 2 + 186 == 252 and 39 + 2 + 55 == 96                  # the totals at those caps
    huge = {"node": {"rooms": "A-Z"}, "data": {"worker": [{"id": str(i)} for i in range(100)]}}
    assert netthr1.derived_cap(huge) == 128                   # clamped
    assert netthr1.derived_cap({"node": {"rooms": ""}}) == 0 + 19 + 9 + 1 + 8


def test_router_template_is_the_k6b_fixture():
    doc = tomllib.loads(render("netthr1-router.toml"))
    assert netthr1.rooms_count(doc["node"]["rooms"]) == 26
    assert netthr1.derived_cap(doc) == 63
    enabled = sorted(k for k, v in doc["edges"].items() if v.get("enable") is True)
    assert enabled == ["usrp"]
    assert doc["ids"]["registry"] is False
    assert netthr1.hostname_edges(doc) == ()
    assert len(doc["edges"]) == 19                            # the registry the cap formula counts (usrp + 18 disabled)
    assert "<" not in render("netthr1-router.toml")


def test_hotspot_template_is_the_k6b_preset():
    doc = tomllib.loads(render("netthr1-hotspot.toml"))
    assert doc["node"]["profile"] == "hotspot" and netthr1.rooms_count(doc["node"]["rooms"]) == 1
    assert netthr1.derived_cap(doc) == 38
    assert netthr1.http_settings(doc) == ((2, 2), (2, 2))
    assert doc["edges"]["radio"]["port"][0]["kind"] == "null"
    assert "enable" not in doc["edges"]["radio"]               # only the profile selects the children
    assert "<" not in render("netthr1-hotspot.toml")


def test_http_caps_snippet_merges_into_web_and_admin():
    doc = tomllib.loads(render("netthr1-router.toml", extras=["netthr1-http-caps.toml"]))
    assert netthr1.http_settings(doc) == ((1, 1), (2, 2))
    assert doc["web"]["static_dir"] == doc["admin"]["static_dir"] == "/var/lib/dv/netthr1-static"
    assert doc["admin"]["enable"] is True                      # the template's own keys stay


def test_http_settings_defaults_follow_the_profile():
    assert netthr1.http_settings({}) == ((16, 4), (16, 4))
    assert netthr1.http_settings({"node": {"profile": "hotspot"}}) == ((2, 2), (2, 2))
    assert netthr1.http_settings({"node": {"profile": "hotspot"}, "web": {"login_slots": 3}}) == ((2, 3), (2, 2))


def test_hostname_rows():
    doc = {"edges": {"m17": {"peers": [{"host": "192.0.2.5"}, {"host": "peer.example.invalid"}]},
                     "xlx": {"peer": [{"host": "10.0.0.1"}]},
                     "link": {"uplink": [{"host": "localhost:31800"}, {"host": "192.0.2.9:31800"}]},
                     "dextra": {"enable": True}}}
    assert netthr1.hostname_rows(doc) == {"m17": ["peer.example.invalid"], "link": ["localhost"]}
    assert netthr1.hostname_edges(doc) == ("dv-edge-link", "dv-edge-m17")


# ---------------------------------------------------------------- the gauge

def test_gauge_from_samples_and_problems():
    text = ('dv_fabric_sessions{proc="dv-router",kind="open"} 12\ndv_fabric_sessions{proc="dv-router",kind="cap"} 63\n'
            'dv_fabric_sessions{proc="dv-router",kind="peak"} 14\ndv_fabric_sessions{proc="dv-edge-xlx",kind="open"} 99\n'
            'dv_other{kind="open"} 1\n')
    kinds = netthr1.sessions_from_samples(metrics.parse(text))
    assert kinds == {"open": 12, "cap": 63, "peak": 14}
    assert netthr1.gauge_problems(kinds) == []
    assert netthr1.gauge_problems({"open": 15, "cap": 63, "peak": 14})[0].startswith("open 15 is above peak 14")
    assert "above cap" in netthr1.gauge_problems({"open": 1, "cap": 5, "peak": 6})[0]
    assert "want exactly" in netthr1.gauge_problems({"open": 1, "cap": 5})[0]
    assert "whole number" in netthr1.gauge_problems({"open": 1.5, "cap": 5, "peak": 5})[0]


def test_find_sessions_in_any_nesting():
    body = {"v": 1, "summary": {"fabric": {"sessions": {"open": 3, "cap": 4, "peak": 4}}}}
    assert netthr1.find_sessions(body) == {"open": 3, "cap": 4, "peak": 4}
    assert netthr1.find_sessions({"data": {"fabric": {}}}) is None
    assert netthr1.find_sessions([{"x": 1}, {"launcher": {"fabric": {"sessions": {"open": 1, "cap": 9, "peak": 2}}}}]) == {"open": 1, "cap": 9, "peak": 2}
    assert netthr1.find_sessions({"summary": {"fabric": None}}) is None


def test_reload_text_edits():
    assert netthr1.data_worker_rows(2) == "[[data.worker]]\nid = 'nt01'\n[[data.worker]]\nid = 'nt02'\n"
    assert netthr1.add_rows("a = 1", "[[x.y]]\n") == "a = 1\n[[x.y]]\n"
    rows = netthr1.m17_rows([("M17-DVH1", "hole1.hole.dvt.test", 17013)])
    parsed = tomllib.loads(rows)
    assert parsed["edges"]["m17"]["peers"] == [{"callsign": "M17-DVH1", "host": "hole1.hole.dvt.test", "port": 17013, "modules": "M"}]


# ---------------------------------------------------------------- sweeps

def sweep(*rows):
    return [{"comm": c, "pid": p, "start": s, "threads": t} for c, p, s, t in rows]


def test_analyse_sweeps_flags_a_ceiling_and_a_restart():
    ceilings = {"dv-room": 4, "dv-edge-m17": 6, "dv-router": 71}
    a = sweep(("dv-room", 10, 100, 4), ("dv-edge-m17", 11, 101, 6), ("dv-router", 1, 1, 60))
    b = sweep(("dv-room", 10, 100, 5), ("dv-edge-m17", 11, 101, 4), ("dv-router", 1, 1, 61), ("dv-x", 7, 9, 1))
    c = sweep(("dv-room", 12, 300, 4), ("dv-edge-m17", 11, 101, 4), ("dv-router", 1, 1, 61))
    got = netthr1.analyse_sweeps([a, b, c], ceilings, total_limit=80)
    assert got["violations"] == ["sweep 1: dv-room pid=10 has 5 threads, ceiling 4"]
    assert got["restarts"] == ["sweep 1: dv-x pid=7 is new", "sweep 2: dv-room pid=10 is gone", "sweep 2: dv-x pid=7 is gone",
                               "sweep 2: dv-room pid=12 is new"]
    assert got["unknown"] == ["dv-x"]
    assert got["peak_total"] == 71 and got["peak_by_comm"]["dv-room"] == 5
    over = netthr1.analyse_sweeps([a], ceilings, total_limit=69)
    assert over["violations"] == ["sweep 0: 70 threads in total, limit 69"]
    changed = netthr1.analyse_sweeps([a, sweep(("dv-room", 10, 777, 4), ("dv-edge-m17", 11, 101, 6), ("dv-router", 1, 1, 60))], ceilings)
    assert changed["restarts"] == ["sweep 1: dv-room pid=10 changed start time"]


def test_counts_of():
    assert netthr1.counts_of(sweep(("dv-room", 1, 1, 4), ("dv-room", 2, 1, 4), ("dv-router", 3, 1, 5))) == {"dv-room": 2, "dv-router": 1}


# ---------------------------------------------------------------- login timing

def test_login_arithmetic():
    assert netthr1.median([3.0, 1.0, 2.0]) == 2.0 and netthr1.median([4.0, 1.0, 2.0, 3.0]) == 2.5
    assert netthr1.hash_estimate_ms(120.0, 20.0) == 100.0 and netthr1.hash_estimate_ms(10.0, 20.0) == 0.0
    # one slot, 110 ms hashes: the 5th waits 4 x 110 = 440 ms, the 6th waits 550 ms
    assert netthr1.concurrency_needed(110.0, 500.0, 1) == 6
    # two slots: ceil(k / 2) - 1 rounds of 110 ms: k = 9 waits 4 rounds (440), k = 11 waits 5 rounds (550)
    assert netthr1.concurrency_needed(110.0, 500.0, 2) == 11
    assert netthr1.concurrency_needed(500.0, 500.0, 1) == 3          # a hash as long as the wait: the third waits 2 x 500
    assert netthr1.concurrency_needed(0.0) > 1000


# ---------------------------------------------------------------- the DNS server that does not answer

def query(name, qtype=1, txid=0x1234):
    q = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        q += bytes([len(label)]) + label.encode()
    return q + b"\x00" + struct.pack("!HH", qtype, 1)


def test_parse_query():
    assert dnshole.parse_query(query("Hole1.hole.dvt.test")) == (0x1234, "hole1.hole.dvt.test", 1)
    assert dnshole.parse_query(query("a.b", 28))[2] == 28
    assert dnshole.parse_query(b"\x00" * 5) is None
    assert dnshole.parse_query(b"\x12\x34\x81\x80" + b"\x00" * 20) is None          # a response, not a query
    assert dnshole.parse_query(query("x.y")[:-3]) is None                           # cut in the question


def test_build_answer_a_and_aaaa():
    reply = dnshole.build_answer(query("ok1.ok.dvt.test"), "192.0.2.30")
    txid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", reply[:12])
    assert (txid, flags, qd, an, ns, ar) == (0x1234, 0x8180, 1, 1, 0, 0)
    assert reply.endswith(b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + socket.inet_aton("192.0.2.30"))
    empty = dnshole.build_answer(query("ok1.ok.dvt.test", 28), "192.0.2.30")
    assert struct.unpack("!HHHHHH", empty[:12])[3] == 0 and empty == empty[:12] + query("ok1.ok.dvt.test", 28)[12:]
    assert dnshole.build_answer(b"junk", "192.0.2.30") is None


def test_which_names_get_an_answer():
    assert dnshole.answers_name("ok1.ok.dvt.test") and dnshole.answers_name("ok.dvt.test")
    assert not dnshole.answers_name("hole1.hole.dvt.test") and not dnshole.answers_name("hook.dvt.test")


def test_schedule_and_times():
    log = [(10.0, "a.hole", 1), (10.1, "b.hole", 1), (12.0, "a.hole", 1), (15.2, "c.hole", 1), (20.3, "d.ok", 1), (20.4, "d.ok", 28)]
    assert dnshole.first_times(log, {"a.hole", "d.ok"}) == {"a.hole": 10.0, "d.ok": 20.3}
    t0, rel = dnshole.schedule(log, ["a.hole", "b.hole", "c.hole", "d.ok", "never"])
    assert t0 == 10.0 and [None if r is None else round(r, 1) for r in rel] == [0.0, 0.1, 5.2, 10.3, None]
    assert dnshole.last_times(log)["a.hole"] == 12.0
    assert dnshole.schedule([], ["x"]) == (None, [None])


def test_hole_server_answers_ok_and_stays_silent_otherwise():
    hole = dnshole.Hole(host="127.0.0.1", port=0, ip="192.0.2.30")
    port = hole.sock.getsockname()[1]
    hole.start()
    try:
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(1.0)
        c.sendto(query("ok1.ok.dvt.test"), ("127.0.0.1", port))
        assert c.recvfrom(512)[0].endswith(socket.inet_aton("192.0.2.30"))
        c.sendto(query("hole1.hole.dvt.test"), ("127.0.0.1", port))
        c.settimeout(0.4)
        with pytest.raises(socket.timeout):
            c.recvfrom(512)
        c.close()
        names = [n for _t, n, _q in hole.log]
        assert names == ["ok1.ok.dvt.test", "hole1.hole.dvt.test"]
    finally:
        hole.stop()
