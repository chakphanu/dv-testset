# SPDX-License-Identifier: MIT
"""dvtest/guardid2.py (the pure judge of cases/test_guardid2.py), the StreamStart / Refused reading added to dvtest/rfpos1.py, and the rig wiring."""
import struct
import tomllib
from pathlib import Path

import pytest

from dvtest import guardid2 as g2, rfpos1, sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
RAW = bytes(range(41))


def start_datagram(stream=9, carried=0, callsign="P31672", radio_id=1023961, raw=RAW, profile=4, session=5, counter=2, extra=b""):
    body = struct.pack("<BIQBHH", 0, stream, 77, 0, profile, carried) + callsign.encode().ljust(10, b" ") + struct.pack("<IB", radio_id, len(raw)) + raw
    return rfpos1.build_record(19, session, counter, body + extra)


def event(data, direction="c2s"):
    rec = rfpos1.parse_record(data)
    entry = {"dir": direction, "t": 0.0, "dropped": False}
    entry.update(rec)
    entry.pop("payload", None)
    return entry


# ---------------------------------------------------------------- the wire

def test_streamstart_layout_matches_the_documented_offsets():
    data = start_datagram(carried=g2.FLAG | (1 << 1))
    assert len(data) == g2.FIXED_STREAMSTART + len(RAW)
    assert struct.unpack_from("<H", data, 13 + 16)[0] == 0x0003     # carried: body offset 16..17 (pair 1, stream 4, origin 8, hop 1, profile 2)
    assert data[13 + 18:13 + 28] == b"P31672    "                  # callsign: body offset 18..27
    assert struct.unpack_from("<I", data, 13 + 28)[0] == 1023961    # radio id: body offset 28..31
    assert data[13 + 32] == len(RAW)                                # raw_len: body offset 32
    rec = rfpos1.parse_record(data)
    assert (rec["stream_id"], rec["profile"], rec["carried"], rec["callsign"], rec["radio_id"], rec["raw_len"], rec["size"]) == (
        9, 4, 3, "P31672", 1023961, 41, len(data))


def test_the_older_streamstart_without_a_whole_fixed_part_still_parses():
    short = rfpos1.build_record(19, 1, 2, struct.pack("<BIQBHH", 0, 9, 1, 0, 2, 0) + bytes(5))
    rec = rfpos1.parse_record(short)
    assert (rec["stream_id"], rec["profile"], rec["carried"]) == (9, 2, 0) and "radio_id" not in rec


def test_refused_record():
    data = rfpos1.build_record(22, 1, 3, struct.pack("<IB", 12, 1))
    rec = rfpos1.parse_record(data)
    assert (rec["kind"], rec["stream_id"], rec["reason"]) == (22, 12, 1)
    assert g2.refused([event(data, "s2c"), event(data, "c2s")]) == [{"stream_id": 12, "reason": 1}]


def test_flag_and_companions_are_separate():
    assert g2.flag_of(0x0001) and g2.flag_of(0x0003) and not g2.flag_of(0x0002) and not g2.flag_of(0)
    assert g2.companions_of(0x0003) == 0x0002 and g2.companions_of(0x0001) == 0 and g2.companions_of(0x0002) == 0x0002


def test_starts_reads_one_record_per_stream_and_the_requested_talkers():
    events = [event(start_datagram(stream=1, carried=1)), event(start_datagram(stream=1, carried=1, counter=3)),
              event(start_datagram(stream=2, carried=1, radio_id=1023962)), event(start_datagram(stream=3, radio_id=1023007, callsign="N0CALL")),
              event(start_datagram(stream=4), "s2c")]
    found = g2.starts(events)
    assert [r["stream_id"] for r in found] == [1, 2, 3]
    assert [r["stream_id"] for r in g2.starts(events, {1023962})] == [2]
    assert g2.starts(events, {1}) == []
    assert g2.starts(events, since=2)[0]["stream_id"] == 2
    assert found[0]["flag"] and found[0]["size"] == g2.FIXED_STREAMSTART + 41 and not found[2]["flag"]


def test_wire_problems_for_a_flagged_talker():
    good = g2.starts([event(start_datagram(stream=n, carried=1)) for n in range(4)])
    assert g2.wire_problems(good, 1023961, "P31672", True, 4) == []
    assert "wanted 5" in g2.wire_problems(good, 1023961, "P31672", True, 5)[0]
    lost = g2.starts([event(start_datagram(stream=n, carried=0)) for n in range(4)])
    problems = g2.wire_problems(lost, 1023961, "P31672", True, 4)
    assert len(problems) == 4 and "flag (bit 0) clear, wanted set" in problems[0]
    wrong_name = g2.starts([event(start_datagram(stream=1, carried=1, callsign="N0CALL"))])
    assert "callsign 'N0CALL', wanted 'P31672'" in g2.wire_problems(wrong_name, 1023961, "P31672", True, 1)[0]


def test_wire_problems_for_a_mapped_talker_and_for_added_bytes():
    plain = g2.starts([event(start_datagram(stream=1, carried=0, callsign="N0CALL", radio_id=1023007))])
    assert g2.wire_problems(plain, 1023007, "N0CALL", False, 1) == []
    flagged = g2.starts([event(start_datagram(stream=1, carried=1, callsign="N0CALL", radio_id=1023007))])
    assert "set, wanted clear" in g2.wire_problems(flagged, 1023007, "N0CALL", False, 1)[0]
    padded = g2.starts([event(start_datagram(stream=1, carried=1, extra=bytes(2)))])
    assert "extra bytes" in g2.wire_problems(padded, 1023961, "P31672", True, 1)[0]


def test_same_size():
    flagged = g2.starts([event(start_datagram(stream=1, carried=1))])
    plain = g2.starts([event(start_datagram(stream=2, carried=0, raw=bytes(10)))])
    assert g2.same_size(flagged, plain) is None                      # the raw header differs, the fixed part does not
    grown = g2.starts([event(start_datagram(stream=3, carried=1, extra=bytes(4)))])
    assert "differs" in g2.same_size(grown, plain)


# ---------------------------------------------------------------- listeners

def test_heard_counts_each_sent_frame_once():
    sent = [bytes([n]) * 11 for n in range(5)]
    assert g2.heard([sent[0], sent[0], sent[3], bytes(11)], sent) == 2
    assert g2.heard([], sent) == 0
    assert g2.share(0, 0) == 0.0 and g2.share(3, 4) == 0.75


def test_listener_problems():
    assert g2.listener_problems("node2", 54, 54, True, "Y") == []
    assert g2.listener_problems("node2", 44, 54, True, "Y") == []
    assert "heard 0 of 54" in g2.listener_problems("node2", 0, 54, True, "Y")[0]
    assert g2.listener_problems("node2", 0, 9, False, "X") == []
    assert "wanted none" in g2.listener_problems("node2", 3, 9, False, "X")[0]


# ---------------------------------------------------------------- counters

HUB = """\
# TYPE dv_room_rts_total counter
dv_room_rts_total{room="g",result="grant"} 7
dv_room_rts_total{room="g",result="deny"} 1
dv_room_rts_total{room="h",result="grant"} 2
dv_edge_drops_total{proc="p25",reason="deny_guard_block"} 1
dv_edge_drops_total{proc="p25",reason="id_held"} 4
dv_edge_drops_total{proc="p25",reason="idle"} 0
dv_edge_drops_total{proc="dcs",reason="deny_guard_block"} 5
dv_edge_drops_total{proc="dcs",reason="malformed"} 9
dv_edge_drops_total{proc="link",reason="guard_blocked"} 2
"""


def test_counters_reads_the_rts_results_and_the_guard_and_edge_reasons():
    got = g2.counters(HUB)
    assert got["rts.grant"] == 9 and got["rts.deny"] == 1
    assert got["drops.p25.deny_guard_block"] == 1 and got["drops.p25.id_held"] == 4 and "drops.p25.idle" not in got
    assert got["drops.dcs.deny_guard_block"] == 5 and "drops.dcs.malformed" not in got and got["drops.link.guard_blocked"] == 2


def test_delta_and_denials():
    before = g2.counters(HUB)
    after = dict(before, **{"rts.grant": 14, "drops.p25.deny_guard_block": 2, "drops.link.new": 1})
    moved = g2.delta(before, after)
    assert moved == {"rts.grant": 5, "drops.p25.deny_guard_block": 1, "drops.link.new": 1}
    assert g2.guard_denials(moved) == 1
    assert g2.guard_denials({"drops.p25.deny_guard_block": 1, "drops.link.guard_blocked": 2, "drops.p25.id_held": 9, "rts.deny": 4}) == 3


def test_counter_problems():
    assert g2.counter_problems("node1", {"rts.grant": 5, "rts.deny": 1, "drops.p25.deny_guard_block": 1}, 5, denials_at_least=1) == []
    assert "denials rose by 0, wanted at least 1" in g2.counter_problems("node1", {"rts.grant": 5}, 5, denials_at_least=1)[0]
    assert "grant rose by 3, wanted at least 5" in g2.counter_problems("node1", {"rts.grant": 3, "drops.p25.deny_guard_block": 1}, 5, denials_at_least=1)[0]
    assert g2.counter_problems("node2", {"rts.grant": 5}, 5, denials_exactly=0) == []
    assert "wanted 0" in g2.counter_problems("node2", {"rts.grant": 5, "drops.link.deny_guard_block": 1}, 5, denials_exactly=0)[0]
    assert "deny rose by 1" in g2.counter_problems("node2", {"rts.grant": 5, "rts.deny": 1}, 5, denials_exactly=0)[0]


# ---------------------------------------------------------------- switch and rig wiring

def test_switch():
    assert g2.on({"DVT_GUARDID2": "on"}) and g2.on({"DVT_GUARDID2": " 1 "}) and g2.on({"DVT_GUARDID2": "TRUE"})
    assert not g2.on({}) and not g2.on({"DVT_GUARDID2": "off"}) and not g2.on({"DVT_GUARDID2": ""})


def test_the_unmapped_ids_are_in_no_directory_and_the_mapped_ones_are():
    ids = {}
    for line in (ROOT / "rig" / "config" / "dmrid.dat").read_text(encoding="utf-8").splitlines():
        parts = line.split(";")
        if len(parts) >= 2 and parts[0].isdigit():
            ids[int(parts[0])] = parts[1]
    for name, rid in g2.UNMAPPED.items():
        assert rid not in ids, name
        assert 1 <= rid <= 0xFFFFFF
    for rid, call in g2.MAPPED.values():
        assert ids[rid] == call
    assert g2.SHARED == "P%05d" % g2.TG


def test_the_p25_snippet_renders_into_both_nodes_and_matches_the_constants():
    snippet = tomllib.loads((SNIPPETS / "guardid2-p25.toml").read_text(encoding="utf-8"))["edges"]["p25"]
    assert snippet == {"enable": True, "server_module": g2.ROOM, "server_tg": g2.TG, "no_radio_id": "fallback"}
    template = '[node]\ncallsign = "XLX999"\nbind_ip = "<OWN_IP>"\nrooms = "A-Z"\n[edges.p25]\nenable = "auto"\n'
    link_a = (SNIPPETS / "rfpos1-link-a.toml").read_text(encoding="utf-8")
    link_b = (SNIPPETS / "rfpos1-link-b.toml").read_text(encoding="utf-8")
    p25_text = (SNIPPETS / "guardid2-p25.toml").read_text(encoding="utf-8")
    for extras in ((link_a, p25_text), (link_b, p25_text)):
        cfg = tomllib.loads(sut_config.render(template, "172.30.0", own=11, transcoder=21, tester=30, extras=extras))
        assert cfg["edges"]["p25"]["no_radio_id"] == "fallback" and cfg["edges"]["p25"]["server_module"] == g2.ROOM
        assert cfg["edges"]["p25"]["enable"] is True


def test_run_sh_and_compose_wire_the_switch():
    text = (ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
    assert text.count("DVT_GUARDID2") >= 5 and "rig/config/dv-router/guardid2-p25.toml" in text
    assert "link_extra+=(--extra rig/config/dv-router/guardid2-p25.toml)" in text
    compose = (ROOT / "rig" / "compose.yml").read_text(encoding="utf-8")
    assert "DVT_GUARDID2: ${DVT_GUARDID2:-}" in compose
