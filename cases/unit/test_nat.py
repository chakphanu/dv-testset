# SPDX-License-Identifier: MIT
"""dvtest/nat.py and the NAT rig tools (profile nat): the control protocol, the iptables rules of each
NAT type, the router state machine against a recording runner, the classification logic and the probe /
reflector pair over loopback (127.0.0.x addresses stand in for the outside addresses; with no NAT in
between, the loopback result must classify as full cone)."""
import json
import socket
import threading
import time

import pytest

from dvtest import nat
from dvtest.tools import nat_probe, nat_reflector

pytestmark = pytest.mark.unit

TOPO = nat.make_topology("172.31.0.0/24", "172.30.0.92", "172.31.0.2", "172.30.0.93", "172.31.0.91", "172.30.0.11",
                         out_if="eth0", in_if="eth1")


# ---------------------------------------------------------------- protocol

def test_parse_request_accepts_every_documented_op():
    for text, op in (('{"op":"nat_mode","mode":"symmetric"}', "nat_mode"), ('{"op":"flush"}', "flush"),
                     ('{"op":"blackhole","state":"on"}', "blackhole"), ('{"op":"blackhole","state":"off"}', "blackhole"),
                     ('{"op":"blackhole","state":"for","seconds":3}', "blackhole"), ('{"op":"drop_direct","state":"on"}', "drop_direct"),
                     ('{"op":"status"}', "status"), ('{"op":"reset"}', "reset")):
        assert nat.parse_request(text)["op"] == op
        assert nat.parse_request(text.encode())["op"] == op


@pytest.mark.parametrize("bad", [
    "", "not json", "[]", '{"op": 3}', '{"op":"rm"}', '{"op":"nat_mode"}', '{"op":"nat_mode","mode":"cone"}',
    '{"op":"nat_mode","mode":"full_cone","x":1}', '{"op":"flush","all":true}', '{"op":"blackhole","state":"maybe"}',
    '{"op":"blackhole","state":"for"}', '{"op":"blackhole","state":"for","seconds":0}', '{"op":"blackhole","state":"for","seconds":-1}',
    '{"op":"blackhole","state":"for","seconds":301}', '{"op":"blackhole","state":"for","seconds":true}',
    '{"op":"blackhole","state":"on","seconds":3}', '{"op":"drop_direct","state":"for"}', '{"op":"drop_direct"}',
    '{"op":"status","x":1}'])
def test_parse_request_refuses_everything_else(bad):
    with pytest.raises(nat.NatError):
        nat.parse_request(bad)


def test_parse_request_limits_size_and_charset():
    with pytest.raises(nat.NatError):
        nat.parse_request(b"x" * (nat.MAX_LINE + 1))
    with pytest.raises(nat.NatError):
        nat.parse_request('{"op":"status","k":"é"}'.encode("utf-8"))


def test_encode_is_one_line():
    line = nat.encode({"ok": True, "b": 1, "a": [1, 2]})
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"ok": True, "b": 1, "a": [1, 2]}


# ---------------------------------------------------------------- rules

def flat(cmds):
    return [" ".join(c) for c in cmds]


def test_make_topology_validates():
    for kw in ({"inside_net": "172.31.0.0"}, {"out_ip": "172.30.0"}, {"browser_ip": "x"}, {"media_port": 0}, {"out_if": "eth0;rm"}):
        args = dict(inside_net="172.31.0.0/24", out_ip="172.30.0.92", in_ip="172.31.0.2", alias_ip="172.30.0.93",
                    browser_ip="172.31.0.91", sut_ip="172.30.0.11")
        args.update(kw)
        with pytest.raises(nat.NatError):
            nat.make_topology(**args)


def test_setup_has_the_control_forwards_tcp_only():
    cmds = flat(nat.setup_commands(TOPO))
    assert "ip addr add 172.30.0.93/24 dev eth0" in cmds
    forwards = [c for c in cmds if "-j DNAT" in c]
    assert len(forwards) == 2
    for c in forwards:
        assert "-p tcp" in c and "-d 172.30.0.92" in c and "--to-destination 172.31.0.91:" in c
    assert not any("udp" in c for c in forwards)


def test_full_cone_is_a_static_one_to_one_nat():
    cmds = flat(nat.mode_commands(TOPO, "full_cone"))
    assert any("-s 172.31.0.91" in c and "-j SNAT --to-source 172.30.0.93" in c for c in cmds)
    assert any("-d 172.30.0.93" in c and "-j DNAT --to-destination 172.31.0.91" in c and "DVT_MODE_PRE" in c for c in cmds)
    assert not any("MASQUERADE" in c for c in cmds)


def test_port_restricted_is_plain_masquerade():
    cmds = flat(nat.mode_commands(TOPO, "port_restricted"))
    masq = [c for c in cmds if "MASQUERADE" in c]
    assert len(masq) == 1 and "--random" not in masq[0] and "-s 172.31.0.0/24" in masq[0]
    assert not any("DNAT" in c or "SNAT" in c for c in cmds)


def test_symmetric_randomises_udp_only():
    cmds = flat(nat.mode_commands(TOPO, "symmetric"))
    rnd = [c for c in cmds if "--random" in c]
    assert len(rnd) == 1 and "-p udp" in rnd[0]
    assert any("MASQUERADE" in c and "--random" not in c for c in cmds)       # TCP keeps plain masquerade
    assert cmds.index(rnd[0]) < max(i for i, c in enumerate(cmds) if "MASQUERADE" in c and "--random" not in c)


def test_none_installs_nothing_and_every_mode_flushes_first():
    for mode in nat.MODES:
        cmds = flat(nat.mode_commands(TOPO, mode))
        assert cmds[0].endswith("-F DVT_MODE_PRE") and cmds[1].endswith("-F DVT_MODE_POST")
    assert len(nat.mode_commands(TOPO, "none")) == 2
    with pytest.raises(nat.NatError):
        nat.mode_commands(TOPO, "cone")


def test_blackhole_and_drop_direct_rules():
    rules = nat.blackhole_rules(TOPO)
    argv = [" ".join(nat.rule_argv("-A", r)) for r in rules]
    assert all("-p udp" in a and a.endswith("-j DROP") for a in argv)
    assert "-i eth1 -o eth0" in argv[0] and "-i eth0 -o eth1" in argv[1]
    d = " ".join(nat.rule_argv("-A", nat.drop_direct_rule(TOPO)))
    assert "-d 172.30.0.11" in d and "--dport 50000" in d and "-i eth1 -o eth0" in d


def test_parse_counters_and_ip_addr():
    text = ("Chain DVT_FWD (1 references)\n    pkts      bytes target     prot opt in     out     source               destination\n"
            "      12     2400 DROP       17   --  eth1   eth0    0.0.0.0/0            0.0.0.0/0            /* dvt-blackhole-out */\n"
            "       0        0 DROP       17   --  eth0   eth1    0.0.0.0/0            0.0.0.0/0            /* dvt-blackhole-in */\n")
    assert nat.parse_counters(text) == {"dvt-blackhole-out": 12, "dvt-blackhole-in": 0}
    ip = "2: eth0    inet 172.30.0.92/24 brd 172.30.0.255 scope global eth0\\       valid_lft forever\n3: eth1@if9    inet 172.31.0.2/24 scope global eth1\n"
    assert nat.parse_ip_addr(ip) == {"172.30.0.92": "eth0", "172.31.0.2": "eth1"}


# ---------------------------------------------------------------- router state

class Recorder:
    def __init__(self):
        self.calls, self.present, self.counters = [], set(), "0"

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["conntrack", "-D"]:
            return 0, "conntrack v1.4.7: 3 flow entries have been deleted.\n"
        if argv[:1] == ["cat"]:
            return 0, "5\n"
        if argv[:5] == ["iptables", "-t", "filter", "-L", "DVT_FWD"]:
            return 0, ("    pkts      bytes target\n   %s       99 DROP       17   --  eth1   eth0   0.0.0.0/0  0.0.0.0/0  /* dvt-blackhole-out */\n"
                       "       4       99 DROP       17   --  eth0   eth1   0.0.0.0/0  0.0.0.0/0  /* dvt-blackhole-in */\n"
                       "       7       99 DROP       17   --  eth1   eth0   0.0.0.0/0  0.0.0.0/0  /* dvt-drop-direct */\n") % self.counters
        if "-C" in argv[:5]:
            return (0 if argv[-3] in self.present else 1), ""
        if argv[3:5] == ["-A", "DVT_FWD"]:
            self.present.add(argv[-3])
        if argv[3:5] == ["-D", "DVT_FWD"]:
            self.present.discard(argv[-3])
        return 0, ""


def test_router_start_sets_port_restricted_and_flushes_udp_only():
    rec = Recorder()
    r = nat.Router(TOPO, rec)
    r.start()
    assert r.mode == "port_restricted"
    assert ["conntrack", "-D", "-p", "udp"] in rec.calls
    assert not any(c[:2] == ["conntrack", "-F"] for c in rec.calls)


def test_router_modes_and_answers():
    rec = Recorder()
    r = nat.Router(TOPO, rec)
    ans = r.handle(nat.parse_request('{"op":"nat_mode","mode":"symmetric"}'))
    assert ans["ok"] and ans["mode"] == "symmetric" and r.mode == "symmetric"
    assert r.handle({"op": "flush"}) == {"ok": True, "deleted": 3}
    st = r.handle({"op": "status"})
    assert st["ok"] and st["mode"] == "symmetric" and st["conntrack_entries"] == 5 and st["blackhole"] is False


def test_router_blackhole_toggle_and_counters():
    rec = Recorder()
    r = nat.Router(TOPO, rec)
    assert r.handle({"op": "blackhole", "state": "on"})["blackhole"] is True
    assert rec.present == {"dvt-blackhole-out", "dvt-blackhole-in"}
    assert r.handle({"op": "blackhole", "state": "on"})["ok"]            # idempotent: no second rule
    assert sum(1 for c in rec.calls if c[3:5] == ["-A", "DVT_FWD"]) == 2
    rec.counters = "321"
    off = r.handle({"op": "blackhole", "state": "off"})
    assert off["blackhole"] is False and rec.present == set()
    assert off["last"]["dropped_out"] == 321 and off["last"]["dropped_in"] == 4
    assert off["last"]["t_off"] >= off["last"]["t_on"]


def test_router_blackhole_for_switches_itself_off():
    rec = Recorder()
    r = nat.Router(TOPO, rec)
    ans = r.handle({"op": "blackhole", "state": "for", "seconds": 0.2})
    assert ans["ok"] and ans["until"] is not None and r.blackhole_on
    deadline = time.time() + 3
    while r.blackhole_on and time.time() < deadline:
        time.sleep(0.05)
    assert not r.blackhole_on and rec.present == set()
    assert r.last_blackhole["t_off"] - r.last_blackhole["t_on"] < 1.5


def test_router_drop_direct_and_reset():
    rec = Recorder()
    r = nat.Router(TOPO, rec)
    assert r.handle({"op": "drop_direct", "state": "on"})["drop_direct"] is True
    assert rec.present == {"dvt-drop-direct"}
    r.handle({"op": "blackhole", "state": "on"})
    ans = r.handle({"op": "reset"})
    assert ans["ok"] and ans["mode"] == "port_restricted"
    assert rec.present == set() and not r.blackhole_on and not r.drop_direct_on


def test_router_reports_a_failing_command():
    def run(argv):
        return (1, "iptables: boom") if argv[0] == "iptables" else (0, "")
    ans = nat.Router(TOPO, run).handle({"op": "nat_mode", "mode": "full_cone"})
    assert ans["ok"] is False and "boom" in ans["error"]


# ---------------------------------------------------------------- classification

A1, A2, B1 = ("203.0.113.10", 41000), ("203.0.113.10", 41000), ("203.0.113.10", 41000)


@pytest.mark.parametrize("mapped,unsol,mapping,filtering,typ", [
    ({"a1": A1, "a2": A2, "b1": B1}, {"third_address": True, "same_address_other_port": True}, nat.EI, nat.EI, "full_cone"),
    ({"a1": A1, "a2": A2, "b1": B1}, {"third_address": False, "same_address_other_port": True}, nat.EI, nat.AD, "restricted_cone"),
    ({"a1": A1, "a2": A2, "b1": B1}, {"third_address": False, "same_address_other_port": False}, nat.EI, nat.APD, "port_restricted"),
    ({"a1": A1, "a2": ("203.0.113.10", 41001), "b1": ("203.0.113.10", 41002)}, {"third_address": False, "same_address_other_port": False},
     nat.APD, nat.APD, "symmetric"),
    ({"a1": A1, "a2": A2, "b1": ("203.0.113.10", 41002)}, {"third_address": False, "same_address_other_port": False},
     nat.AD, nat.APD, "symmetric"),
    ({"a1": A1, "a2": A2, "b1": B1}, {"third_address": True, "same_address_other_port": False}, nat.EI, "unknown", "unknown"),
])
def test_classify(mapped, unsol, mapping, filtering, typ):
    assert nat.classify(mapped, unsol) == {"mapping": mapping, "filtering": filtering, "type": typ}


def test_classify_without_a_reflection_is_unknown():
    assert nat.classify({"a1": None, "a2": A1, "b1": A1}, {})["type"] == "unknown"


# ---------------------------------------------------------------- reflector and probe over loopback

def test_reflect_reply_and_poke_formats():
    rep = nat.reflect_reply(nat.REFLECT_REQ + b"tok1", ("198.51.100.5", 4242))
    assert nat.parse_reflect_reply(rep) == ("tok1", ("198.51.100.5", 4242))
    assert nat.reflect_reply(b"hello", ("192.0.2.7", 5)) is None
    assert nat.parse_reflect_reply(b"DVT-NAT-REPLY {bad") is None
    poke = nat.parse_poke(nat.POKE_REQ + b'{"to":["192.0.2.9",4000],"from_port":3481,"data":"x"}')
    assert poke == {"to": ("192.0.2.9", 4000), "from_port": 3481, "data": "x"}
    assert nat.parse_poke(nat.POKE_REQ + b'{"to":["nope",4000],"from_port":3481}') is None
    assert nat.parse_poke(b"x") is None


def free_udp_port(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def start_reflector(host, ports, poke_port):
    poke = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    poke.bind((host, poke_port))
    socks = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((host, p))
        socks.append(s)
        threading.Thread(target=nat_reflector.serve, args=(s, poke, "t"), daemon=True).start()
    return socks + [poke]


def test_probe_and_reflectors_on_loopback_classify_no_nat_as_full_cone():
    ports = (free_udp_port("127.0.0.3"), free_udp_port("127.0.0.3"))
    poke_port = free_udp_port("127.0.0.3")
    socks = start_reflector("127.0.0.3", ports, poke_port)
    socks += start_reflector("127.0.0.4", (ports[0],), free_udp_port("127.0.0.4"))
    # the probe server on loopback; the reflector pokes from 127.0.0.3:poke_port, the tester sends from 127.0.0.1
    srv = nat_probe.Server(("127.0.0.1", 0), nat_probe.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = nat.NatClient(("127.0.0.1", 1), ("127.0.0.1", srv.server_address[1]), "127.0.0.3", "127.0.0.4",
                               ports=ports, poke_port=poke_port)
        out = client.classify(local_port=free_udp_port("127.0.0.1"), settle=0.3)
    finally:
        srv.shutdown()
        srv.server_close()
        for s in socks:
            s.close()
    assert out["classification"]["type"] == "full_cone", out
    assert out["unsolicited"] == {"third_address": True, "same_address_other_port": True}
    assert out["mapped"]["a1"][0] == "127.0.0.1" and out["mapped"]["a1"] == out["mapped"]["b1"]


def test_probe_refuses_a_bad_target():
    srv = nat_probe.Server(("127.0.0.1", 0), nat_probe.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ans = nat.call(("127.0.0.1", srv.server_address[1]), {"op": "probe_start", "local_port": 0, "targets": [["not-an-ip", 1]]})
        assert ans["ok"] is False
        assert nat.call(("127.0.0.1", srv.server_address[1]), {"op": "nope"})["ok"] is False
    finally:
        srv.shutdown()
        srv.server_close()
