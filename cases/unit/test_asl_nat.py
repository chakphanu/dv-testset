# SPDX-License-Identifier: MIT
"""dvtest/asl_nat.py (case T5n of the asl-lab, docs/asl-lab.md "T5n"): the validated settings, the iptables
rules of each mode, the sidecar state machine against a recording runner, the control protocol over loopback,
the reading of the edge's capture, and lint tests that read rig/compose.yml and scripts/run.sh (no rig, no docker
run; the rendered-config test uses `docker compose config` only and skips without docker)."""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from dvtest import asl_nat, limits, nat
from dvtest.tools import asl_nat as asl_nat_tool

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
T = asl_nat.make_local_nat("192.0.2.42", "192.0.2.46", "192.0.2.11", 4569, 44569, "eth0", 24)


def flat(cmds):
    return [" ".join(c) for c in cmds]


# ---------------------------------------------------------------- settings

def test_make_local_nat_accepts_the_lab_values():
    assert T.local_ip == "192.0.2.42" and T.alias_ip == "192.0.2.46" and T.peer_ip == "192.0.2.11"
    assert (T.port, T.map_port, T.out_if, T.out_prefix) == (4569, 44569, "eth0", 24)


@pytest.mark.parametrize("kw", [
    dict(local_ip="nope"), dict(alias_ip="192.0.2.300"), dict(peer_ip=""), dict(alias_ip="192.0.2.42"),
    dict(peer_ip="192.0.2.42"), dict(alias_ip="192.0.2.11"), dict(port=80), dict(map_port=70000), dict(map_port=4569),
    dict(port=True), dict(map_port="44569"), dict(out_if="eth0; rm"), dict(out_if=""), dict(out_prefix=31), dict(out_prefix=True)])
def test_make_local_nat_refuses_bad_settings(kw):
    args = dict(local_ip="192.0.2.42", alias_ip="192.0.2.46", peer_ip="192.0.2.11", port=4569, map_port=44569,
                out_if="eth0", out_prefix=24)
    args.update(kw)
    with pytest.raises(nat.NatError):
        asl_nat.make_local_nat(**args)


# ---------------------------------------------------------------- rules

def test_setup_commands_add_the_alias_and_the_chain():
    cmds = flat(asl_nat.setup_commands(T))
    assert cmds[0] == "ip addr add 192.0.2.46/24 dev eth0"
    assert cmds[1] == "iptables -t nat -N DVT_ASL_NAT"
    assert cmds[2] == "iptables -t nat -A POSTROUTING -j DVT_ASL_NAT"


def test_source_seen_per_mode():
    assert asl_nat.source_seen(T, "address") == ("192.0.2.46", 44569)
    assert asl_nat.source_seen(T, "port") == ("192.0.2.42", 44569)
    assert asl_nat.source_seen(T, "off") == ("192.0.2.42", None)
    with pytest.raises(nat.NatError):
        asl_nat.source_seen(T, "cone")


def test_mode_commands_off_only_empties_the_chain():
    assert flat(asl_nat.mode_commands(T, "off")) == ["iptables -t nat -F DVT_ASL_NAT"]


@pytest.mark.parametrize("mode,to", [("address", "192.0.2.46:44569"), ("port", "192.0.2.42:44569")])
def test_mode_commands_one_snat_rule_for_the_iax_socket_towards_the_edge_only(mode, to):
    cmds = flat(asl_nat.mode_commands(T, mode))
    assert cmds[0] == "iptables -t nat -F DVT_ASL_NAT"
    assert len(cmds) == 2
    rule = cmds[1]
    # only the node's IAX2 socket towards the edge: DNS, the lab agent and every other flow are not touched
    for word in ("-s 192.0.2.42", "-d 192.0.2.11", "-p udp", "--sport 4569", "--dport 4569", "-o eth0", "-j SNAT", "--to-source " + to):
        assert word in rule, rule
    assert rule.startswith("iptables -t nat -A DVT_ASL_NAT ")


def test_mode_commands_refuse_an_unknown_mode():
    with pytest.raises(nat.NatError):
        asl_nat.mode_commands(T, "symmetric")


def test_parse_snat_packets():
    text = ("Chain DVT_ASL_NAT (1 references)\n    pkts      bytes target     prot opt in     out     source               destination\n"
            "      14     2480 SNAT       udp  --  *      eth0    192.0.2.42           192.0.2.11           udp spt:4569 dpt:4569 /* dvt-asl-nat */ to:192.0.2.46:44569\n")
    assert asl_nat.parse_snat_packets(text) == 14
    assert asl_nat.parse_snat_packets("Chain DVT_ASL_NAT (1 references)\n pkts bytes target\n") == 0
    assert asl_nat.parse_snat_packets("") == 0 and asl_nat.parse_snat_packets(None) == 0


# ---------------------------------------------------------------- protocol

@pytest.mark.parametrize("text,op", [('{"op":"snat","mode":"off"}', "snat"), ('{"op":"snat","mode":"address"}', "snat"),
                                     ('{"op":"snat","mode":"port"}', "snat"), ('{"op":"status"}', "status"), ('{"op":"reset"}', "reset")])
def test_parse_request_accepts_every_documented_op(text, op):
    assert asl_nat.parse_request(text)["op"] == op
    assert asl_nat.parse_request(text.encode())["op"] == op


@pytest.mark.parametrize("bad", [
    "", "not json", "[]", '{"op": 3}', '{"op":"rm"}', '{"op":"snat"}', '{"op":"snat","mode":"full_cone"}',
    '{"op":"snat","mode":"off","x":1}', '{"op":"status","x":1}', '{"op":"reset","mode":"off"}', '{"op":"nat_mode","mode":"off"}'])
def test_parse_request_refuses_everything_else(bad):
    with pytest.raises(nat.NatError):
        asl_nat.parse_request(bad)


def test_parse_request_limits_size_and_charset():
    with pytest.raises(nat.NatError):
        asl_nat.parse_request(b"x" * (nat.MAX_LINE + 1))
    with pytest.raises(nat.NatError):
        asl_nat.parse_request('{"op":"status","k":"\u00e9"}'.encode("utf-8"))


def test_client_from_env():
    assert asl_nat.client_from_env({}) is None
    assert asl_nat.client_from_env({"DVT_ASL_NAT_CTL": "192.0.2.42:9101"}) is None             # not switched on
    assert asl_nat.client_from_env({"DVT_ASL_EDGE_NAT": "1", "DVT_ASL_NAT_CTL": "junk"}) is None
    assert asl_nat.client_from_env({"DVT_ASL_EDGE_NAT": "1", "DVT_ASL_NAT_CTL": "192.0.2.42:x"}) is None
    assert asl_nat.client_from_env({"DVT_ASL_EDGE_NAT": "1", "DVT_ASL_NAT_CTL": "192.0.2.42:9101"}).addr == ("192.0.2.42", 9101)


# ---------------------------------------------------------------- the sidecar

class Recorder:
    def __init__(self, fail=(), outputs=None):
        self.calls, self.fail, self.outputs = [], set(fail), outputs or {}

    def __call__(self, argv):
        self.calls.append(" ".join(argv))
        for key in self.fail:
            if key in self.calls[-1]:
                return 2, "boom"
        for key, out in self.outputs.items():
            if key in self.calls[-1]:
                return 0, out
        return 0, ""


def test_start_sets_up_then_turns_the_nat_off_and_flushes():
    run = Recorder()
    snat = asl_nat.Snat(T, run)
    snat.start()
    assert run.calls[:3] == flat(asl_nat.setup_commands(T))
    assert run.calls[3] == "iptables -t nat -F DVT_ASL_NAT"
    assert run.calls[-1] == "conntrack -D -p udp"
    assert snat.mode == "off"


def test_start_tolerates_existing_alias_and_chain_but_not_other_errors():
    class Exists(Recorder):
        def __call__(self, argv):
            super().__call__(argv)
            return (2, "RTNETLINK answers: File exists") if argv[:3] == ["ip", "addr", "add"] else (0, "")
    asl_nat.Snat(T, Exists()).start()
    with pytest.raises(nat.NatError):
        asl_nat.Snat(T, Recorder(fail=["iptables -t nat -N"])).start()


def test_set_mode_applies_the_rule_then_deletes_the_udp_conntrack_entries():
    run = Recorder()
    snat = asl_nat.Snat(T, run)
    ans = snat.handle({"op": "snat", "mode": "address"})
    assert ans == {"ok": True, "mode": "address", "source": ["192.0.2.46", 44569]}
    assert run.calls[-1] == "conntrack -D -p udp"                         # a flow made before the rule would keep its old mapping
    assert any("--to-source 192.0.2.46:44569" in c for c in run.calls)
    assert snat.handle({"op": "snat", "mode": "port"})["source"] == ["192.0.2.42", 44569]
    assert snat.handle({"op": "reset"}) == {"ok": True, "mode": "off", "source": ["192.0.2.42", None]}


def test_conntrack_status_1_means_nothing_to_delete_and_other_codes_fail():
    class Code(Recorder):
        def __init__(self, rc):
            super().__init__()
            self.rc = rc

        def __call__(self, argv):
            super().__call__(argv)
            return (self.rc, "") if argv[0] == "conntrack" else (0, "")
    assert asl_nat.Snat(T, Code(1)).handle({"op": "reset"})["ok"] is True
    ans = asl_nat.Snat(T, Code(3)).handle({"op": "reset"})
    assert ans["ok"] is False and "conntrack" in ans["error"]


def test_a_failing_rule_is_an_error_answer_and_keeps_the_old_mode():
    snat = asl_nat.Snat(T, Recorder())
    snat.handle({"op": "snat", "mode": "port"})
    snat.run = Recorder(fail=["-j SNAT"])
    ans = snat.handle({"op": "snat", "mode": "address"})
    assert ans["ok"] is False and "failed" in ans["error"]
    assert snat.mode == "port"


def test_status_reports_mode_source_and_translated_packets():
    listing = "    pkts      bytes target\n      7      900 SNAT       udp  --  *      eth0 192.0.2.42 192.0.2.11 to:192.0.2.46:44569\n"
    snat = asl_nat.Snat(T, Recorder(outputs={"-L DVT_ASL_NAT": listing}))
    snat.handle({"op": "snat", "mode": "address"})
    assert snat.handle({"op": "status"}) == {"ok": True, "mode": "address", "source": ["192.0.2.46", 44569], "snat_packets": 7}
    assert asl_nat.Snat(T, Recorder(fail=["-L DVT_ASL_NAT"])).handle({"op": "status"})["snat_packets"] is None


# ---------------------------------------------------------------- the control server over loopback

def _serve(snat, allow):
    srv = asl_nat_tool.Server(("127.0.0.1", 0), asl_nat_tool.Handler)
    srv.snat, srv.allow = snat, set(allow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_control_server_answers_the_tester_and_refuses_others():
    snat = asl_nat.Snat(T, Recorder())
    srv = _serve(snat, {"127.0.0.1"})
    try:
        client = asl_nat.Client(srv.server_address)
        assert client.mode("address")["source"] == ["192.0.2.46", 44569]
        assert client.status()["mode"] == "address"
        assert client.reset()["mode"] == "off"
        with pytest.raises(nat.NatError):
            client.ctl("snat", mode="bogus")
        with pytest.raises(nat.NatError):
            client.ctl("nat_mode", mode="full_cone")                      # the web router's ops do not exist here
    finally:
        srv.shutdown()
        srv.server_close()
    srv = _serve(snat, {"192.0.2.30"})
    try:
        with pytest.raises(nat.NatError) as exc:
            asl_nat.Client(srv.server_address).status()
        assert "not allowed" in str(exc.value)
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_dead_control_port_is_an_oserror_for_the_fixture_to_retry():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(OSError):
        asl_nat.Client(("127.0.0.1", port)).status()


# ---------------------------------------------------------------- reading the edge's capture

EDGE, NODE, ALIAS = "192.0.2.11", "192.0.2.42", "192.0.2.46"


def fr(src, dst, t):
    return {"src": src, "dst": dst, "t": t}


def doc_of(**types):
    return {"types": types}


def test_edge_reaction_refusal_is_accept_then_hangup_without_answer():
    doc = doc_of(**{"iax.NEW": [fr(ALIAS + ":44569", EDGE + ":4569", 0.0), fr(ALIAS + ":44569", EDGE + ":4569", 0.01)],
                    "iax.ACCEPT": [fr(EDGE + ":4569", ALIAS + ":44569", 0.02)],
                    "iax.HANGUP": [fr(EDGE + ":4569", ALIAS + ":44569", 0.0215)]})
    r = asl_nat.edge_reaction(doc, EDGE, ALIAS)
    assert r["refused"] is True and r["answered"] is False
    assert (r["new"], r["accept"], r["answer"], r["hangup"]) == (2, 1, 0, 1)
    assert r["new_ports"] == [44569] and r["accept_to_hangup_s"] == 0.0015


def test_edge_reaction_answer_is_not_a_refusal_even_with_a_later_hangup():
    doc = doc_of(**{"iax.NEW": [fr(NODE + ":44569", EDGE + ":4569", 0.0)],
                    "iax.ACCEPT": [fr(EDGE + ":4569", NODE + ":44569", 0.02)],
                    "control.ANSWER": [fr(EDGE + ":4569", NODE + ":44569", 0.021)],
                    "iax.HANGUP": [fr(EDGE + ":4569", NODE + ":44569", 5.0)]})
    r = asl_nat.edge_reaction(doc, EDGE, NODE)
    assert r["answered"] is True and r["refused"] is False


def test_edge_reaction_looks_at_one_address_only_and_one_direction():
    doc = doc_of(**{"iax.NEW": [fr(NODE + ":44569", EDGE + ":4569", 0.0)],
                    "iax.ACCEPT": [fr(EDGE + ":4569", NODE + ":44569", 0.02)],
                    "control.ANSWER": [fr(EDGE + ":4569", NODE + ":44569", 0.021)],
                    "iax.HANGUP": [fr(ALIAS + ":44569", EDGE + ":4569", 1.0)]})            # not from the edge
    r = asl_nat.edge_reaction(doc, EDGE, ALIAS)
    assert (r["new"], r["accept"], r["answer"], r["hangup"]) == (0, 0, 0, 0)
    assert r["refused"] is False and r["answered"] is False and r["accept_to_hangup_s"] is None


def test_edge_reaction_hangup_before_accept_or_no_accept_is_no_validated_refusal():
    doc = doc_of(**{"iax.NEW": [fr(ALIAS + ":44569", EDGE + ":4569", 0.0)],
                    "iax.HANGUP": [fr(EDGE + ":4569", ALIAS + ":44569", 0.01)]})
    assert asl_nat.edge_reaction(doc, EDGE, ALIAS)["refused"] is False                      # silent / bare hangup is another thing
    doc = doc_of(**{"iax.NEW": [fr(ALIAS + ":44569", EDGE + ":4569", 0.0)],
                    "iax.ACCEPT": [fr(EDGE + ":4569", ALIAS + ":44569", 0.02)],
                    "iax.HANGUP": [fr(EDGE + ":4569", ALIAS + ":44569", 0.01)]})
    assert asl_nat.edge_reaction(doc, EDGE, ALIAS)["refused"] is False
    assert asl_nat.edge_reaction({}, EDGE, ALIAS)["refused"] is False


# ---------------------------------------------------------------- compose.yml and run.sh lint (text only)

def _service_block(text, name):
    m = re.search(r"^  %s:\n((?:    .*\n|\s*\n|  #.*\n)*)" % re.escape(name), text, re.M)
    assert m, "service %s not found in rig/compose.yml" % name
    return m.group(1)


def test_compose_asl_nat_a_is_a_netadmin_sidecar_of_asl_a_in_its_own_profile():
    text = (ROOT / "rig" / "compose.yml").read_text()
    block = _service_block(text, "asl-nat-a")
    assert "<<: *nat\n" in block                                       # the image, caps (NET_ADMIN only), limits and read-only fs of the NAT profile
    assert "profiles: [asl-nat]" in block                              # not the nat profile (no web, no browser) and not a default one
    assert 'network_mode: "service:asl-a"' in block
    assert re.search(r"depends_on:\n\s+asl-a: \{condition: service_started", block)
    assert "cap_add" not in block and "privileged" not in block and "ports:" not in block
    assert "dvtest.tools.asl_nat" in block
    # addresses: the node's own address is listened on and is the only local ip; the alias is the free .46; the peer is the edge
    for flag, value in (("--listen", ".42:9101"), ("--local-ip", ".42"), ("--alias-ip", ".46"), ("--peer-ip", ".11"), ("--allow", ".30")):
        assert re.search(r"- %s\n\s+- \$\{DVT_NET:-172\.30\.0\}%s\n" % (re.escape(flag), re.escape(value)), block), flag
    assert "--map-port\n      - \"44569\"" in block
    # the shared x-nat anchor keeps the lines the sidecar relies on
    anchor = re.search(r"^x-nat: &nat\n((?:  .*\n)+)", text, re.M).group(1)
    assert "cap_drop: [ALL]" in anchor and "cap_add: [NET_ADMIN]" in anchor and "read_only: true" in anchor


def test_compose_alias_address_is_used_by_no_service_and_the_tester_env_matches():
    text = (ROOT / "rig" / "compose.yml").read_text()
    assert not re.search(r'ipv4_address: "\$\{DVT_NET:-172\.30\.0\}\.46"', text)
    assert "DVT_ASL_NAT_CTL: ${DVT_NET:-172.30.0}.42:9101" in text
    assert "DVT_ASL_EDGE_NAT: ${DVT_ASL_EDGE_NAT:-}" in text
    asl_a = _service_block(text, "asl-a")
    assert 'ipv4_address: "${DVT_NET:-172.30.0}.42"' in asl_a


def test_run_sh_adds_the_profile_builds_and_starts_the_sidecar_only_with_the_flag():
    sh = (ROOT / "scripts" / "run.sh").read_text()
    assert 'if [ "${DVT_ASL_EDGE_NAT:-}" = 1 ]; then\n        profiles+=(--profile asl-nat)\n        has_asl_nat=1' in sh
    assert "--profile asl-ext --profile asl-nat)" in sh                         # --down removes it too
    assert '[ "$has_asl_nat" = 0 ] || to_build+=(asl-nat-a)' in sh
    assert '[ "$has_asl_nat" = 0 ] || "${compose[@]}" up -d --no-build asl-nat-a' in sh
    # the explicit up list of the plain asl-edge run does not name the sidecar
    up = [ln for ln in sh.splitlines() if "up -d --no-build tester transcoder-sut xlxd-sut asl-reg" in ln]
    assert len(up) == 1 and "asl-nat" not in up[0]


def test_rendered_compose_caps_the_sidecar_and_shares_asl_a_namespace():
    if not shutil.which("docker"):
        pytest.skip("docker is not installed")
    cmd = ["docker", "compose", "-p", "dvtasln", "-f", str(ROOT / "rig/compose.yml"), "--env-file", str(ROOT / ".env.example")]
    for name in ("core", "asl-lab", "asl-nat"):
        cmd += ["--profile", name]
    cmd += ["config", "--format", "json"]
    env = dict(os.environ, TRANSCODER_IMAGE="example.invalid/transcoder:none")
    run = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    if run.returncode != 0:
        pytest.skip("docker compose config unavailable: %s" % run.stderr.strip()[:200])
    svc = json.loads(run.stdout)["services"]["asl-nat-a"]
    assert limits.check_services({"services": {"asl-nat-a": svc}}) == []
    assert svc["network_mode"] == "service:asl-a"
    assert svc["cap_add"] == ["NET_ADMIN"] and svc["cap_drop"] == ["ALL"]
    assert svc["profiles"] == ["asl-nat"] and svc.get("read_only") is True
    assert "networks" not in svc and "ports" not in svc
    cmdline = svc["command"]
    assert cmdline[:3] == ["python", "-m", "dvtest.tools.asl_nat"]
    assert cmdline[cmdline.index("--listen") + 1] == "172.30.0.42:9101"
    assert cmdline[cmdline.index("--alias-ip") + 1] == "172.30.0.46"
    assert cmdline[cmdline.index("--port") + 1] == "4569" and cmdline[cmdline.index("--map-port") + 1] == "44569"
