# SPDX-License-Identifier: MIT
"""ICE1 address change in the NAT router (dvtest/nat.py): the control words `readdress` and `mappings`, the iptables
command lists that move the browser's UDP to the other outside address and back, the conntrack reader and the
router state machine against a recording runner. No rig, no iptables."""
import pytest

from dvtest import nat

pytestmark = pytest.mark.unit

TOPO = nat.make_topology("172.31.0.0/24", "172.30.0.92", "172.31.0.2", "172.30.0.93", "172.31.0.91", "172.30.0.11",
                         out_if="eth0", in_if="eth1")


def flat(cmds):
    return [" ".join(c) for c in cmds]


# ---------------------------------------------------------------- protocol

@pytest.mark.parametrize("text,op", [('{"op":"readdress","state":"on"}', "readdress"), ('{"op":"readdress","state":"off"}', "readdress"),
                                     ('{"op":"mappings"}', "mappings")])
def test_parse_request_accepts_the_new_words(text, op):
    assert nat.parse_request(text)["op"] == op
    assert nat.parse_request(text.encode())["op"] == op
    assert op in nat.OPS


@pytest.mark.parametrize("bad", ['{"op":"readdress"}', '{"op":"readdress","state":"for"}', '{"op":"readdress","state":"on","seconds":3}',
                                 '{"op":"readdress","state":true}', '{"op":"mappings","state":"on"}', '{"op":"mappings","all":1}'])
def test_parse_request_refuses_a_malformed_readdress(bad):
    with pytest.raises(nat.NatError):
        nat.parse_request(bad)


# ---------------------------------------------------------------- the pure command lists

def test_readdress_addresses_swap_the_two_outside_addresses():
    assert nat.readdress_addresses(TOPO, "full_cone") == ("172.30.0.93", "172.30.0.92")
    assert nat.readdress_addresses(TOPO, "port_restricted") == ("172.30.0.92", "172.30.0.93")
    assert nat.readdress_addresses(TOPO, "symmetric") == ("172.30.0.92", "172.30.0.93")
    for mode in ("none", "cone", ""):
        with pytest.raises(nat.NatError):
            nat.readdress_addresses(TOPO, mode)


def test_readdress_flushes_both_mode_chains_first_for_every_mode():
    for mode in nat.MODES[:3]:
        cmds = flat(nat.readdress_commands(TOPO, mode))
        assert cmds[0] == "iptables -t nat -F DVT_MODE_PRE" and cmds[1] == "iptables -t nat -F DVT_MODE_POST"


def test_readdress_none_has_nothing_to_move():
    with pytest.raises(nat.NatError):
        nat.readdress_commands(TOPO, "none")


def test_readdress_port_restricted_moves_udp_to_the_alias_and_keeps_masquerade_for_the_rest():
    cmds = flat(nat.readdress_commands(TOPO, "port_restricted"))[2:]
    assert cmds == [
        "iptables -t nat -A DVT_MODE_POST -s 172.31.0.0/24 -o eth0 -p udp -j SNAT --to-source 172.30.0.93",
        "iptables -t nat -A DVT_MODE_POST -s 172.31.0.0/24 -o eth0 -j MASQUERADE"]
    assert not any("--random" in c for c in cmds)           # the mapping stays endpoint independent: the port is kept


def test_readdress_symmetric_keeps_the_random_port_per_destination():
    cmds = flat(nat.readdress_commands(TOPO, "symmetric"))[2:]
    assert cmds == [
        "iptables -t nat -A DVT_MODE_POST -s 172.31.0.0/24 -o eth0 -p udp -j SNAT --to-source 172.30.0.93 --random",
        "iptables -t nat -A DVT_MODE_POST -s 172.31.0.0/24 -o eth0 -j MASQUERADE"]


def test_readdress_full_cone_moves_udp_to_the_router_address_and_closes_the_old_one_for_udp():
    cmds = flat(nat.readdress_commands(TOPO, "full_cone"))[2:]
    assert cmds == [
        "iptables -t nat -A DVT_MODE_POST -s 172.31.0.91 -o eth0 -p udp -j SNAT --to-source 172.30.0.92",
        "iptables -t nat -A DVT_MODE_POST -s 172.31.0.91 -o eth0 -j SNAT --to-source 172.30.0.93",
        "iptables -t nat -A DVT_MODE_PRE -i eth0 -d 172.30.0.93 -p udp -j RETURN",
        "iptables -t nat -A DVT_MODE_PRE -i eth0 -d 172.30.0.93 -j DNAT --to-destination 172.31.0.91",
        "iptables -t nat -A DVT_MODE_PRE -i eth0 -d 172.30.0.92 -p udp -j DNAT --to-destination 172.31.0.91"]
    # the RETURN must come before the old DNAT, or UDP would still reach the browser through the old address
    pre = [c for c in cmds if "DVT_MODE_PRE" in c]
    assert pre.index([c for c in pre if "-j RETURN" in c][0]) < pre.index([c for c in pre if "-d 172.30.0.93 -j DNAT" in c][0])


def test_readdress_never_moves_the_control_forwards_or_tcp():
    for mode in nat.MODES[:3]:
        cmds = flat(nat.readdress_commands(TOPO, mode))
        assert not any(" DVT_PRE " in c and " -A " in c for c in cmds)       # the control forwards are in DVT_PRE, set up once
        for c in cmds:
            if "-j SNAT" in c and "-s 172.31.0.0/24" in c:                   # the two-argument NATs move UDP only
                assert "-p udp" in c


def test_the_way_back_is_the_modes_own_rules():
    for mode in nat.MODES[:3]:
        back = flat(nat.mode_commands(TOPO, mode))
        assert not any("-p udp -j SNAT" in c for c in back if mode != "full_cone")
        assert back != flat(nat.readdress_commands(TOPO, mode))


def test_setup_adds_the_input_chain_for_the_unmapped_media_counter():
    cmds = flat(nat.setup_commands(TOPO))
    assert "iptables -t filter -N DVT_IN" in cmds
    assert "iptables -t filter -I INPUT 1 -j DVT_IN" in cmds


def test_unmapped_rule_matches_only_the_suts_media_port_toward_the_router():
    argv = " ".join(nat.rule_argv("-A", nat.unmapped_rule(TOPO), nat.CHAIN_IN))
    assert argv == ("iptables -t filter -A DVT_IN -i eth0 -p udp -s 172.30.0.11 --sport 50000 -m comment --comment "
                    "dvt-unmapped-media -j DROP")
    # the blackhole rules keep their chain
    assert nat.rule_argv("-A", nat.drop_direct_rule(TOPO))[4] == "DVT_FWD"


# ---------------------------------------------------------------- conntrack

CT = """\
udp      17 27 src=172.31.0.91 dst=172.30.0.11 sport=41234 dport=50000 src=172.30.0.11 dst=172.30.0.93 sport=50000 dport=41234 [ASSURED] mark=0 use=1
udp      17 29 src=172.31.0.91 dst=172.30.0.94 sport=41234 dport=3478 [UNREPLIED] src=172.30.0.94 dst=172.30.0.93 sport=3478 dport=41234 mark=0 use=1
udp      17 5 src=172.31.0.91 dst=172.30.0.11 sport=43000 dport=50000 [UNREPLIED] src=172.30.0.11 dst=172.30.0.92 sport=50000 dport=43000 mark=0 use=1
tcp      6 431999 ESTABLISHED src=172.31.0.91 dst=172.30.0.90 sport=50000 dport=443 src=172.30.0.90 dst=172.30.0.92 sport=443 dport=50000 [ASSURED] mark=0 use=1
conntrack v1.4.7 (conntrack-tools): 4 flow entries have been shown.
"""


def test_parse_conntrack_reads_both_tuples_and_the_flags():
    flows = nat.parse_conntrack(CT)
    assert len(flows) == 3                                          # the tcp line and the summary are skipped
    first = flows[0]
    assert (first["orig_src"], first["orig_sport"], first["orig_dst"], first["orig_dport"]) == ("172.31.0.91", 41234, "172.30.0.11", 50000)
    assert (first["reply_src"], first["reply_sport"], first["reply_dst"], first["reply_dport"]) == ("172.30.0.11", 50000, "172.30.0.93", 41234)
    assert first["assured"] and not first["unreplied"]
    assert flows[1]["unreplied"] and not flows[1]["assured"]


def test_parse_conntrack_survives_garbage():
    assert nat.parse_conntrack("") == []
    assert nat.parse_conntrack(None) == []
    assert nat.parse_conntrack("udp 17 src=1 dst=2\nudp 17 src=a dst=b sport=x dport=y src=c dst=d sport=e dport=f\n") == []


def test_media_mappings_are_what_the_sut_sees():
    rows = nat.media_mappings(nat.parse_conntrack(CT), TOPO)
    assert rows == [{"browser_port": 43000, "seen_ip": "172.30.0.92", "seen_port": 43000},       # sorted by the address seen
                    {"browser_port": 41234, "seen_ip": "172.30.0.93", "seen_port": 41234}]       # the coturn flow is not one of them


# ---------------------------------------------------------------- router state

class Recorder:
    def __init__(self, fail=None, ct=""):
        self.calls, self.present, self.counter, self.fail, self.ct = [], set(), 0, fail, ct

    def __call__(self, argv):
        self.calls.append(argv)
        if self.fail and self.fail(argv):
            return (1 if argv[0] == "iptables" else 2), "%s: boom" % argv[0]
        if argv[:2] == ["conntrack", "-D"]:
            return 0, "conntrack v1.4.7: 2 flow entries have been deleted.\n"
        if argv[:2] == ["conntrack", "-L"]:
            return 0, self.ct
        if argv[:1] == ["cat"]:
            return 0, "5\n"
        if argv[:5] == ["iptables", "-t", "filter", "-L", "DVT_IN"]:
            return 0, ("    pkts      bytes target\n   %d       99 DROP       17   --  eth0   *   172.30.0.11  0.0.0.0/0  udp spt:50000 "
                       "/* dvt-unmapped-media */\n" % self.counter)
        if argv[:5] == ["iptables", "-t", "filter", "-L", "DVT_FWD"]:
            return 0, "    pkts      bytes target\n"
        if argv[:2] == ["iptables", "-t"] and argv[2] == "filter" and argv[3] in ("-C", "-A", "-D"):
            comment = argv[-3]
            if argv[3] == "-C":
                return (0 if comment in self.present else 1), ""
            (self.present.add if argv[3] == "-A" else self.present.discard)(comment)
        return 0, ""

    def index(self, text):
        return next(i for i, c in enumerate(self.calls) if " ".join(c) == text)


def router(rec, mode="port_restricted"):
    r = nat.Router(TOPO, rec)
    r.handle({"op": "nat_mode", "mode": mode})
    rec.calls.clear()
    return r


def test_readdress_on_installs_the_counter_then_moves_then_flushes():
    rec = Recorder()
    r = router(rec, "port_restricted")
    ans = r.handle({"op": "readdress", "state": "on"})
    assert ans == {"ok": True, "readdressed": True, "from": "172.30.0.92", "to": "172.30.0.93"}
    assert r.readdressed and rec.present == {"dvt-unmapped-media"}
    counter = rec.index("iptables -t filter -A DVT_IN -i eth0 -p udp -s 172.30.0.11 --sport 50000 -m comment --comment dvt-unmapped-media -j DROP")
    move = rec.index("iptables -t nat -A DVT_MODE_POST -s 172.31.0.0/24 -o eth0 -p udp -j SNAT --to-source 172.30.0.93")
    flush = rec.index("conntrack -D -p udp")
    assert counter < move < flush


def test_readdress_off_restores_the_mode_rules_removes_the_counter_and_keeps_the_total():
    rec = Recorder()
    r = router(rec, "symmetric")
    r.handle({"op": "readdress", "state": "on"})
    rec.calls.clear()
    rec.counter = 321
    ans = r.handle({"op": "readdress", "state": "off"})
    assert ans["ok"] and ans["readdressed"] is False and ans["last"]["unmapped_media"] == 321
    assert not r.readdressed and rec.present == set()
    assert flat(nat.mode_commands(TOPO, "symmetric")) == [" ".join(c) for c in rec.calls if c[:3] == ["iptables", "-t", "nat"]]
    assert ["conntrack", "-D", "-p", "udp"] in rec.calls
    st = r.handle({"op": "status"})
    assert st["readdressed"] is False and st["dropped"]["unmapped_media"] == 321 and st["last_readdress"]["unmapped_media"] == 321


def test_status_reads_the_live_counter_while_moved():
    rec = Recorder()
    r = router(rec, "full_cone")
    r.handle({"op": "readdress", "state": "on"})
    rec.counter = 77
    st = r.handle({"op": "status"})
    assert st["readdressed"] is True and st["dropped"]["unmapped_media"] == 77 and st["mode"] == "full_cone"


def test_readdress_is_idempotent_and_off_without_a_move_does_nothing():
    rec = Recorder()
    r = router(rec, "port_restricted")
    assert r.handle({"op": "readdress", "state": "off"}) == {"ok": True, "readdressed": False, "last": None}
    assert rec.calls == []                                           # no rule, no flush: a finalizer may call it any time
    r.handle({"op": "readdress", "state": "on"})
    n = len(rec.calls)
    again = r.handle({"op": "readdress", "state": "on"})
    assert again["ok"] and again["readdressed"] is True and len(rec.calls) == n
    assert sum(1 for c in rec.calls if c[3:5] == ["-A", "DVT_IN"]) == 1


def test_readdress_with_mode_none_is_refused_and_touches_nothing():
    rec = Recorder()
    r = router(rec, "none")
    ans = r.handle({"op": "readdress", "state": "on"})
    assert ans["ok"] is False and "no translation" in ans["error"]
    assert rec.calls == [] and not r.readdressed


def test_a_failing_move_goes_back_to_the_mode_rules():
    rec = Recorder(fail=lambda argv: argv[:4] == ["iptables", "-t", "nat", "-A"] and "SNAT" in argv)
    r = router(rec, "port_restricted")
    ans = r.handle({"op": "readdress", "state": "on"})
    assert ans["ok"] is False and "boom" in ans["error"]
    assert not r.readdressed and rec.present == set()
    assert flat(nat.mode_commands(TOPO, "port_restricted"))[-1] in [" ".join(c) for c in rec.calls]


def test_a_new_nat_mode_ends_the_move():
    rec = Recorder()
    r = router(rec, "port_restricted")
    r.handle({"op": "readdress", "state": "on"})
    ans = r.handle({"op": "nat_mode", "mode": "symmetric"})
    assert ans["ok"] and not r.readdressed and rec.present == set()
    assert r.handle({"op": "status"})["readdressed"] is False


def test_reset_ends_the_move_and_goes_to_port_restricted():
    rec = Recorder()
    r = router(rec, "full_cone")
    r.handle({"op": "readdress", "state": "on"})
    ans = r.handle({"op": "reset"})
    assert ans["ok"] and ans["mode"] == "port_restricted" and not r.readdressed and rec.present == set()


def test_mappings_answer_from_conntrack():
    rec = Recorder(ct=CT)
    r = router(rec, "port_restricted")
    ans = r.handle({"op": "mappings"})
    assert ans["ok"] and ans["readdressed"] is False and ans["mode"] == "port_restricted"
    assert [m["seen_ip"] for m in ans["mappings"]] == ["172.30.0.92", "172.30.0.93"]
    assert ["conntrack", "-L", "-p", "udp"] in rec.calls


def test_mappings_reports_a_failing_conntrack():
    rec = Recorder(fail=lambda argv: argv[:2] == ["conntrack", "-L"])
    r = nat.Router(TOPO, rec)
    ans = r.handle({"op": "mappings"})
    assert ans["ok"] is False and "conntrack failed" in ans["error"]


# ---------------------------------------------------------------- the tester side

def test_client_sends_the_words_as_documented(monkeypatch):
    sent = []

    def fake_call(addr, request, timeout=10.0):
        sent.append(request)
        return {"ok": True, "mappings": [{"browser_port": 1, "seen_ip": "172.30.0.93", "seen_port": 1}]}

    monkeypatch.setattr(nat, "call", fake_call)
    client = nat.NatClient(("127.0.0.1", 9100), ("127.0.0.1", 3001), "127.0.0.3", "127.0.0.4")
    client.readdress(True)
    client.readdress(False)
    assert client.media_mappings()[0]["seen_ip"] == "172.30.0.93"
    assert sent == [{"op": "readdress", "state": "on"}, {"op": "readdress", "state": "off"}, {"op": "mappings"}]
    for request in sent:
        assert nat.parse_request(nat.encode(request))["op"] == request["op"]
