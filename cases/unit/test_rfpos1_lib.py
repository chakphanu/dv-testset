# SPDX-License-Identifier: MIT
"""dvtest/rfpos1.py (pure helpers and the link relay of cases/test_rfpos1_link.py) and the rig snippets rfpos1-link-a.toml / -b.toml."""
import socket
import struct
import time
import tomllib
from pathlib import Path

import pytest

from dvtest import rfpos1, sut_config
from dvtest.proto import dextra

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
TEMPLATE = """\
schema = 1
[node]
callsign = "XLX999"
bind_ip = "<OWN_IP>"
rooms = "A-Z"
"""


def units(counter, sequence, profile=2, stream=7, size=9, session=5):
    return rfpos1.build_units(session, counter, stream, profile, sequence, bytes([sequence & 0xFF]) * size)


# ---------------------------------------------------------------- the wire

def test_units_record_layout_matches_the_documented_offsets():
    data = units(3, 40)
    assert data[0] == 20
    assert struct.unpack_from("<I", data, 1)[0] == 5            # session
    assert struct.unpack_from("<Q", data, 5)[0] == 3            # counter
    assert struct.unpack_from("<I", data, 13)[0] == 7           # stream id
    assert struct.unpack_from("<H", data, 17)[0] == 2           # profile
    assert struct.unpack_from("<Q", data, 19)[0] == 40          # sequence: bytes 19..27, as the DV-Router two-node harness reads it
    rec = rfpos1.parse_record(data)
    assert (rec["kind"], rec["profile"], rec["sequence"], rec["payload_len"], rec["stream_id"]) == (20, 2, 40, 9, 7)
    assert rec["payload"] == bytes([40]) * 9


def test_parse_record_other_kinds():
    assert rfpos1.parse_record(bytes([4]) + bytes(36)) == {"kind": 4}
    assert rfpos1.parse_record(b"") is None
    assert rfpos1.parse_record(bytes([20, 1, 2])) is None
    roommap = rfpos1.build_record(16, 1, 1, bytes([1, ord("G"), ord("G")]))
    assert rfpos1.parse_record(roommap)["pairs"] == [(71, 71)]
    ack = rfpos1.build_record(17, 1, 1, bytes([1, 1]))
    assert rfpos1.parse_record(ack)["results"] == [1]
    demand = rfpos1.build_record(18, 1, 2, struct.pack("<BH", 0, 1 << 1))
    rec = rfpos1.parse_record(demand)
    assert (rec["pair"], rec["profiles"]) == (0, 2)
    start = rfpos1.build_record(19, 1, 2, struct.pack("<BIQBHH", 0, 9, 1, 0, 2, 0) + bytes(20))
    rec = rfpos1.parse_record(start)
    assert (rec["stream_id"], rec["profile"], rec["carried"]) == (9, 2, 0)


def test_a_units_record_with_a_wrong_length_is_not_read():
    data = bytearray(units(1, 3))
    struct.pack_into("<H", data, 27, 18)                        # declares 18 payload bytes, holds 9
    assert rfpos1.parse_record(bytes(data)) is None


def test_ack_status_finds_the_pair_of_the_room():
    assert rfpos1.ack_status([1], [(ord("G"), ord("G"))]) == (1, "talk")
    assert rfpos1.ack_status([2, 3], [(ord("A"), ord("A")), (ord("G"), ord("G"))]) == (3, "not_served")
    assert rfpos1.ack_status([1], [(6, 6)]) == (1, "talk")      # letter indexes work too
    assert rfpos1.ack_status([1], [(ord("A"), ord("A"))])[0] is None


# ---------------------------------------------------------------- the relay (loopback)

class Peer:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(1.0)

    @property
    def addr(self):
        return self.sock.getsockname()

    def recv(self):
        try:
            return self.sock.recvfrom(2048)
        except socket.timeout:
            return None

    def close(self):
        self.sock.close()


@pytest.fixture
def relay_pair():
    server = Peer()
    client = Peer()
    relay = rfpos1.LinkRelay(listen_port=0, upstream=server.addr, bind_ip="127.0.0.1").start()
    try:
        yield relay, client, server
    finally:
        relay.stop()
        client.close()
        server.close()


def test_relay_forwards_both_ways_and_logs(relay_pair):
    relay, client, server = relay_pair
    hello = bytes([1]) + bytes(40)
    client.sock.sendto(hello, ("127.0.0.1", relay.port))
    got = server.recv()
    assert got and got[0] == hello
    welcome = bytes([4]) + bytes(36)
    server.sock.sendto(welcome, got[1])                         # the server answers the source address it saw (the relay's upstream socket)
    back = client.recv()
    assert back and back[0] == welcome
    kinds = [(ev["dir"], ev["kind"]) for ev in relay.snapshot()]
    assert kinds == [("c2s", 1), ("s2c", 4)]
    assert rfpos1.handshake_state(relay.snapshot())[:2] == (True, None)


def test_relay_drops_exactly_the_armed_record_once(relay_pair):
    relay, client, server = relay_pair
    relay.arm(2, 4)
    for sequence in range(8):
        client.sock.sendto(units(sequence + 1, sequence), ("127.0.0.1", relay.port))
    seen = []
    while True:
        got = server.recv()
        if got is None:
            break
        seen.append(rfpos1.parse_record(got[0])["sequence"])
    assert seen == [0, 1, 2, 3, 5, 6, 7]
    assert relay.dropped == 1
    # a record of another profile with the same sequence is not touched, and the drop does not fire twice
    client.sock.sendto(units(20, 4, profile=1), ("127.0.0.1", relay.port))
    client.sock.sendto(units(21, 4), ("127.0.0.1", relay.port))
    more = [rfpos1.parse_record(server.recv()[0])["profile"] for _ in range(2)]
    assert more == [1, 2]
    assert relay.dropped == 1
    marked = [ev["sequence"] for ev in relay.snapshot() if ev.get("dropped")]
    assert marked == [4]


def test_relay_ignores_datagrams_from_strangers_on_the_upstream_side(relay_pair):
    relay, client, server = relay_pair
    client.sock.sendto(bytes([1]) + bytes(40), ("127.0.0.1", relay.port))
    server.recv()
    stranger = Peer()
    try:
        stranger.sock.sendto(bytes([4]) + bytes(36), relay._up.getsockname())
        time.sleep(0.2)
        assert client.recv() is None
    finally:
        stranger.close()


# ---------------------------------------------------------------- what the relay saw

def event(direction, rec, dropped=False):
    entry = {"dir": direction, "dropped": dropped}
    entry.update(rec)
    entry.pop("payload", None)
    return entry


def over(profile, positions, dropped=None, size=9, stream=11):
    out = [event("c2s", {"kind": 19, "stream_id": stream, "profile": profile, "carried": 0, "pair": 0})]
    for q in positions:
        out.append(event("c2s", {"kind": 20, "stream_id": stream, "profile": profile, "sequence": q, "payload_len": size}, dropped=(q == dropped)))
    return out


def test_wire_problems_hold_for_one_unit_per_record_and_one_drop():
    rec = rfpos1.over_records(over(2, range(90), dropped=40), 2)
    assert len(rec) == 90
    assert rfpos1.wire_problems(rec, 2, 90, dropped=40) == []
    # the end unit one position past the last is allowed
    rec = rfpos1.over_records(over(2, range(91)), 2)
    assert rfpos1.wire_problems(rec, 2, 90) == []


def test_wire_problems_catch_a_bundle_a_missing_position_and_the_wrong_drop():
    bundled = rfpos1.over_records(over(2, range(90), size=27), 2)
    assert any("holds [27]" in p for p in rfpos1.wire_problems(bundled, 2, 90))
    short = rfpos1.over_records(over(2, [q for q in range(90) if q != 7]), 2)
    assert any("[7] never left node 1" in p for p in rfpos1.wire_problems(short, 2, 90))
    undropped = rfpos1.over_records(over(2, range(90)), 2)
    assert any("dropped link positions [], wanted [40]" in p for p in rfpos1.wire_problems(undropped, 2, 90, dropped=40))
    assert rfpos1.wire_problems([], 2, 90) and "no Units record" in rfpos1.wire_problems([], 2, 90)[0]


def test_over_records_pick_the_first_stream_and_profile_only():
    events = over(2, range(3), stream=11) + over(2, range(5), stream=12) + over(1, range(4), stream=13)
    assert [r["sequence"] for r in rfpos1.over_records(events, 2)] == [0, 1, 2]
    assert [r["sequence"] for r in rfpos1.over_records(events, 1)] == [0, 1, 2, 3]
    assert rfpos1.over_records(events, 3) == []


def test_demand_for_reads_the_profile_bit_after_a_position():
    events = [event("s2c", {"kind": 18, "pair": 0, "profiles": 1 << 2}), event("s2c", {"kind": 18, "pair": 0, "profiles": 1 << 1})]
    assert rfpos1.demand_for(events, 1)
    assert not rfpos1.demand_for(events, 1, since=2)
    assert rfpos1.demand_for(events, 2, since=0) and not rfpos1.demand_for(events, 2, since=1)


def test_handshake_state_reports_what_is_missing():
    assert rfpos1.handshake_state([]) == (False, None, "no Welcome seen")
    events = [event("c2s", {"kind": 1}), event("s2c", {"kind": 4})]
    assert rfpos1.handshake_state(events)[:2] == (True, None)
    events += [event("c2s", {"kind": 16, "pairs": [(71, 71)]}), event("s2c", {"kind": 17, "results": [2]})]
    assert rfpos1.handshake_state(events) == (True, (2, "not_permitted"), "")
    assert rfpos1.kinds_seen(events) == {"hello": 1, "welcome": 1, "roommap": 1, "roommapack": 1}


# ---------------------------------------------------------------- the listener's positions

def voice_datagrams(counters, stream=0x1234, last=False):
    out = [dextra.voice(stream, c, bytes([c]) * 9, False) for c in counters]
    if last:
        out.append(dextra.voice(stream, 0, bytes(9), True))
    return out


def test_unwrap_counters_follow_the_superframe_and_keep_gaps():
    assert rfpos1.unwrap_counters([0, 1, 2]) == [0, 1, 2]
    assert rfpos1.unwrap_counters([19, 20, 0, 1]) == [19, 20, 21, 22]          # wrap
    assert rfpos1.unwrap_counters([0, 1, 3, 4]) == [0, 1, 3, 4]                # a skipped counter is a skipped position
    assert rfpos1.unwrap_counters([18, 20, 1]) == [18, 20, 22]                 # a gap across the wrap
    assert rfpos1.unwrap_counters([5, 5, 6]) == [5, None, 6]                   # a repeat is not a position


def positions_of(counters):
    got, repeats = rfpos1.dstar_voice(voice_datagrams(counters, last=True))
    return got, repeats


def test_dstar_voice_excludes_the_last_frame_and_a_second_stream():
    datagrams = voice_datagrams(range(5), last=True) + voice_datagrams(range(3), stream=0x9999)
    got, repeats = rfpos1.dstar_voice(datagrams)
    assert [p for p, _f in got] == [0, 1, 2, 3, 4] and repeats == 0
    assert got[3][1] == bytes([3]) * 9


def test_gap_problems_accept_the_position_keeping_output():
    count, dropped = 90, 40
    counters = [q % 21 for q in range(count) if q != dropped]
    got, _ = positions_of(counters)
    expected = [bytes([q % 21]) * 9 for q in range(count)]
    assert rfpos1.gap_problems(got, count, dropped=dropped, expected=expected) == []
    # no loss
    got, _ = positions_of([q % 21 for q in range(count)])
    assert rfpos1.gap_problems(got, count) == []


def test_gap_problems_refuse_the_pre_rfpos1_shape_a_gap_at_the_end():
    """Before RFPOS1 step B the transcoded output was numbered by arrival: the survivors contiguous 0..N-2, the lost unit missing at the END."""
    count, dropped = 90, 40
    counters = [q % 21 for q in range(count - 1)]
    got, _ = positions_of(counters)
    problems = rfpos1.gap_problems(got, count, dropped=dropped)
    assert any("missing positions [40" in p or "missing positions [89]" in p for p in problems)
    assert any("lost count 1" in p for p in problems)


def test_gap_problems_refuse_a_shifted_payload_even_with_the_right_gap():
    count, dropped = 30, 10
    counters = [q % 21 for q in range(count) if q != dropped]
    rows, _ = positions_of(counters)
    expected = [bytes([q % 21]) * 9 for q in range(count)]
    shifted = [(p, f if p < dropped else expected[p - 1]) for p, f in rows]     # the survivors renumbered by arrival, the gap kept
    problems = rfpos1.gap_problems(shifted, count, dropped=dropped, expected=expected)
    assert any("differ from the expected unit" in p for p in problems)


def test_gap_problems_count_two_losses_and_a_truncated_stream():
    count = 30
    got, _ = positions_of([q % 21 for q in range(count) if q not in (10, 11)])
    assert any("lost count 2, wanted 1" in p for p in rfpos1.gap_problems(got, count, dropped=10))
    got, _ = positions_of([q % 21 for q in range(20)])
    assert any("truncated" in p for p in rfpos1.gap_problems(got, count))
    assert rfpos1.gap_problems([], count) == ["no D-STAR voice frame reached the listener of node 2"]


def test_a_drop_of_the_last_position_is_not_called_truncated():
    count = 30
    got, _ = positions_of([q % 21 for q in range(count - 1)])
    assert rfpos1.gap_problems(got, count, dropped=count - 1) == []


# ---------------------------------------------------------------- the rig snippets

def render(*names):
    return sut_config.render(TEMPLATE, "172.30.0", extras=[(SNIPPETS / n).read_text(encoding="utf-8") for n in names])


def test_node1_snippet_is_an_uplink_through_the_tester_relay():
    cfg = tomllib.loads(render("rfpos1-link-a.toml"))
    (uplink,) = cfg["edges"]["link"]["uplink"]
    assert uplink["host"] == "172.30.0.30:%d" % rfpos1.LINK_PORT
    assert uplink["account"] == "rfpos1" and uplink["rooms"] == {rfpos1.ROOM: rfpos1.ROOM}
    assert uplink["secret_file"] == "/etc/dv-router/link-secrets/link.key"
    assert "account" not in cfg["edges"]["link"]


def test_node2_snippet_is_an_account_with_talk_for_the_room():
    cfg = tomllib.loads(render("rfpos1-link-b.toml"))
    (account,) = cfg["edges"]["link"]["account"]
    assert account["name"] == "rfpos1" and account["rooms"] == [rfpos1.ROOM] and account["talk"] is True
    assert account["secret_file"] == "/etc/dv-router/link-secrets/link.key"
    assert len(account["name"]) <= 64 and account["callsign"]


def test_the_two_snippets_share_the_account_and_never_use_the_xlx_modules_of_the_pair():
    a = tomllib.loads(render("dp15-twonode-a.toml", "rfpos1-link-a.toml"))
    b = tomllib.loads(render("dp15-twonode-b.toml", "rfpos1-link-b.toml"))
    assert a["edges"]["link"]["uplink"][0]["account"] == b["edges"]["link"]["account"][0]["name"]
    for cfg in (a, b):
        assert rfpos1.ROOM not in cfg["edges"]["xlx"]["peer"][0]["modules"]


def test_run_sh_wires_the_link_for_the_switch_only():
    text = (ROOT / "scripts" / "run.sh").read_text(encoding="utf-8")
    assert "rig/config/dv-router/rfpos1-link-a.toml" in text and "rig/config/dv-router/rfpos1-link-b.toml" in text
    assert text.count("DVT_RFPOS1B_LINK") >= 4
    compose = (ROOT / "rig" / "compose.yml").read_text(encoding="utf-8")
    assert "link-secrets:/etc/dv-router/link-secrets:ro" in compose and "DVT_RFPOS1B_LINK: ${DVT_RFPOS1B_LINK:-}" in compose


def test_strict_switch():
    assert rfpos1.strict_on({"DVT_RFPOS1B_LINK": "on"}) and rfpos1.strict_on({"DVT_RFPOS1B_LINK": "1"})
    assert not rfpos1.strict_on({}) and not rfpos1.strict_on({"DVT_RFPOS1B_LINK": "off"})
