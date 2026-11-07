# SPDX-License-Identifier: MIT
"""The shipped pieces of the two-node rig (DP15 7b, 7c, 7d): the two DV-Router snippets after the real merge
(rig/config/dv-router/dp15-twonode-a.toml for the SUT, -b.toml for the second node), the compose services dvr-peer and
transcoder-peer, the run.sh profile, and the pure helpers of cases/test_dp15_peer.py. Offline."""
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp15_peer as peer  # noqa: E402

from dvtest import limits, sut_config  # noqa: E402
from dvtest.proto import m17  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nbind_ip = "<OWN_IP>"\nrooms = "A-Z"\n[edges.xlx]\nenable = true\n'
PEER_OCTET, TRANSCODER_OCTET = 16, 17


def node1(gen=None):
    return tomllib.loads(sut_config.render(BASE, "172.30.0", extras=[(SNIPPETS / "dp15-twonode-a.toml").read_text()], gen=gen))


def node2(gen=None):
    return tomllib.loads(sut_config.render(BASE, "172.30.0", own=PEER_OCTET, transcoder=TRANSCODER_OCTET,
                                           extras=[(SNIPPETS / "dp15-twonode-b.toml").read_text()], gen=gen))


# ---- the snippets

@pytest.mark.parametrize("gen", ["c165c93", "compat1"])
def test_the_two_snippets_list_each_other_on_the_same_modules(gen):
    one, two = node1(gen), node2(gen)
    row1 = [r for r in one["edges"]["xlx"]["peer"] if r["callsign"] == "XLX998"]
    row2 = [r for r in two["edges"]["xlx"]["peer"] if r["callsign"] == "XLX999"]
    assert len(row1) == len(row2) == 1
    assert row1[0]["host"] == "172.30.0.%d" % PEER_OCTET and row2[0]["host"] == "172.30.0.11"
    assert row1[0]["modules"] == row2[0]["modules"] == "DE"
    if gen == "compat1":
        assert row1[0]["kind"] == row2[0]["kind"] == "xlx"            # the generation's own requirement, added at render time
    assert peer.ROOM in row1[0]["modules"]


def test_node_two_has_its_own_callsign_address_and_recorder_dir():
    one, two = node1(), node2()
    assert one["node"]["callsign"] == "XLX999" and one["node"]["bind_ip"] == "172.30.0.11"
    assert two["node"]["callsign"] == "XLX998" and two["node"]["bind_ip"] == "172.30.0.%d" % PEER_OCTET
    for cfg in (one, two):
        assert cfg["recorder"]["dir"] == "/var/lib/dv-recorder" and cfg["recorder"]["keep_days"] == 1
    assert two["node"]["rooms"] == one["node"]["rooms"] == "A-Z"          # the merge keeps the template's other keys


def test_snippet_b_alone_adds_no_peer_row_of_the_sut_and_snippet_a_none_of_node_two():
    assert [r["callsign"] for r in node1()["edges"]["xlx"]["peer"]] == ["XLX998"]
    assert [r["callsign"] for r in node2()["edges"]["xlx"]["peer"]] == ["XLX999"]


def test_snippets_use_only_known_tokens_and_ascii():
    for name in ("dp15-twonode-a.toml", "dp15-twonode-b.toml"):
        text = (SNIPPETS / name).read_text()
        text.encode("ascii")
        assert set(sut_config.tokens(text)) <= {"NET"} and "SPDX-License-Identifier: MIT" in text.splitlines()[0]


# ---- compose

def service_block(compose, name):
    return compose.split("\n  %s:\n" % name, 1)[1].split("\n\n", 1)[0]


def test_compose_services_have_free_fixed_addresses_the_hardening_and_the_limits():
    compose = (ROOT / "rig" / "compose.yml").read_text()
    used = re.findall(r'ipv4_address: "\$\{DVT_NET:-172\.30\.0\}\.(\d+)"', compose)
    for octet in (PEER_OCTET, TRANSCODER_OCTET):
        assert used.count(str(octet)) == 1, "address .%d used %d times: %s" % (octet, used.count(str(octet)), sorted(used, key=int))
    block = service_block(compose, "dvr-peer")
    assert "profiles: [twonode]" in block and "<<: *limits-medium" in block
    assert "pids_limit: ${DVT_PIDS_SUT:-1024}" in block and "mem_limit: ${DVT_MEM_SUT:-1g}" in block and "memswap_limit: ${DVT_MEM_SUT:-1g}" in block
    assert "image: ${XLXD_SUT_IMAGE:-" in block and "pull_policy: never" in block and "build:" not in block
    assert "cap_drop: [ALL]" in block and "read_only: true" in block and "no-new-privileges" in block and "- /run" in block and "- /tmp" in block
    assert 'user: "${DVT_UID:-1000}:${DVT_GID:-1000}"' in block and not re.search(r"^\s+ports:", block, re.M)
    assert '"XLX998", "${DVT_NET:-172.30.0}.16", "${DVT_NET:-172.30.0}.17"]' in block and 'command: ["--config", "/etc/dv-router/dv-router.toml"' in block
    assert "xlxd-config/dvr-peer/dv-router.toml:/etc/dv-router/dv-router.toml:ro" in block
    assert "recorder-peer:/var/lib/dv-recorder\n" in block + "\n" and '${DVT_NET:-172.30.0}.%d"' % PEER_OCTET in block
    other = service_block(compose, "transcoder-peer")
    assert "profiles: [twonode]" in other and "<<: *limits-medium" in other
    assert "${DVT_NET:-172.30.0}.17" in other and "DVT_TRANSCODER_PEERS: ${DVT_NET:-172.30.0}.16 ${DVT_NET:-172.30.0}.30" in other
    assert "image: ${TRANSCODER_SUT_IMAGE:-" in other
    assert "DVT_TWONODE: ${DVT_TWONODE:-off}" in compose and "DVT_DVR_PEER: ${DVT_NET:-172.30.0}.16" in compose
    assert "DVT_DVR_PEER_TRANSCODER: ${DVT_NET:-172.30.0}.17" in compose
    assert "      - ${DVT_NET:-172.30.0}.46\n" in compose and "dvt-direwolf:local" in compose            # the neighbours stay where they were


def test_compose_config_is_capped_when_docker_compose_is_there():
    done = subprocess.run(["docker", "compose", "-p", "dvt", "-f", str(ROOT / "rig/compose.yml"), "--env-file", str(ROOT / ".env.example"),
                           "--profile", "twonode", "config", "--format", "json"], capture_output=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin", "XLXD_SUT_IMAGE": "dv-router:test", "DVT_RUN_ID": "unit"})
    if done.returncode != 0:
        pytest.skip("docker compose config is not available")
    services = json.loads(done.stdout)["services"]
    assert {"dvr-peer", "transcoder-peer"} <= set(services)
    assert limits.check_services({"services": {k: services[k] for k in ("dvr-peer", "transcoder-peer")}}) == []
    assert services["dvr-peer"]["read_only"] is True and services["dvr-peer"]["image"] == "dv-router:test"
    assert services["dvr-peer"]["command"][-3:] == ["XLX998", "172.30.0.16", "172.30.0.17"]


# ---- run.sh

def test_run_sh_knows_the_profile():
    run = (ROOT / "scripts/run.sh").read_text()
    assert "--profile core --profile twonode --profile allstar" in run
    assert re.search(r"^    twonode\)\n(?:.*\n){1,4}      export DVT_TWONODE=on", run, re.M)
    assert "--profile twonode needs DVT_SUT_LAUNCH=native" in run and "--profile twonode needs XLXD_SUT_IMAGE" in run
    assert "rig/config/dv-router/dp15-twonode-a.toml" in run and "rig/config/dv-router/dp15-twonode-b.toml" in run
    assert "--own 16 --transcoder 17" in run and "xlxd-config/dvr-peer" in run and "recorder-peer" in run
    assert "interlink, twonode, load" in run
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts/run.sh")]).returncode == 0


def test_run_sh_renders_node_two_from_the_suts_template_without_the_suts_extras():
    run = (ROOT / "scripts/run.sh").read_text()
    body = run.split("render_dvr_peer() {\n", 1)[1].split("\n}\n", 1)[0]
    assert '"$DVT_SUT_CONFIG"' in body and "--extra rig/config/dv-router/dp15-twonode-b.toml" in body
    assert "cfg_opts" not in body and "DVT_SUT_CONFIG_EXTRA" not in body                 # only the node's own snippet


# ---- pure helpers of the case file

HUB = """# TYPE dv_room_rts_total counter
dv_room_rts_total{proc="d",result="grant",room="d"} 3
dv_room_rts_total{proc="d",result="deny",room="d"} 1
dv_room_rts_total{proc="d",result="busy_delayed",room="d"} 1
dv_room_rts_total{proc="d",result="duplicate",room="d"} 0
dv_room_rts_total{proc="e",result="grant",room="e"} 9
dv_room_streams_closed_total{proc="d",room="d"} 3
dv_room_units_total{proc="d",kind="published",room="d"} 66
dv_room_units_total{proc="d",kind="native",room="d"} 5
dv_room_data_total{proc="d",result="accepted",room="d"} 2
dv_room_data_total{proc="e",result="duplicate",room="e"} 1
dv_pkt_items_total{kind="text",result="accepted"} 4
dv_recorder_records_total 7
dv_recorder_bytes_total 1234
dv_recorder_dropped_total 0
dv_edge_raw_total{proc="m17",kind="sms",result="sent"} 2
dv_edge_raw_total{proc="m17",kind="sms",result="no_worker"} 5
dv_edge_text_overs_total{proc="xlx",result="sent"} 4
dv_edge_text_overs_total{proc="dextra",result="skipped"} 1
dv_edge_sessions{kind="peer",proc="xlx"} 1
dv_edge_sessions{kind="client",proc="xlx"} 0
"""


def test_counts_reads_the_room_label_and_the_node_wide_families():
    c = peer.counts(HUB)
    assert (c["rts.grant"], c["rts.deny"], c["rts.busy_delayed"], c["rts.duplicate"]) == (3, 1, 1, 0)        # room d only
    assert c["closed"] == 3 and c["units.published"] == 66
    assert (c["rec.records"], c["rec.bytes"], c["rec.dropped"]) == (7, 1234, 0)
    assert (c["data.all"], c["items.all"], c["raw.sent"]) == (3, 4, 2)
    assert (c["textover.sent"], c["textover.all"], c["xlx.peers"]) == (4, 5, 1)
    assert c["has.recorder"] == 1.0 and c["has.data"] == 1.0
    assert peer.counts(HUB, room="e")["rts.grant"] == 9


def test_counts_without_the_families_is_all_zero_and_flags_them_absent():
    c = peer.counts("# TYPE dv_up gauge\ndv_up 1\n")
    assert c["has.recorder"] == 0.0 and c["has.data"] == 0.0
    assert all(v == 0 for k, v in c.items() if not k.startswith("has."))


def test_delta_skips_the_flags_and_the_gauge():
    before, after = peer.counts(HUB), peer.counts(HUB.replace("grant\",room=\"d\"} 3", "grant\",room=\"d\"} 5").replace("records_total 7", "records_total 8"))
    d = peer.delta(after, before)
    assert d["rts.grant"] == 2 and d["rec.records"] == 1 and d["rts.deny"] == 0
    assert "has.recorder" not in d and "xlx.peers" not in d
    assert peer.moved(d) == {"rec.records": 1, "rts.grant": 2}


def stream_packets(sid, count, src="XLX999", last=True):
    out = []
    for fn in range(count):
        end = last and fn == count - 1
        out.append(m17.stream_packet(sid, fn, bytes(16), src=src, last=end))
    return out


def test_m17_streams_groups_by_stream_id_and_ignores_other_datagrams():
    data = stream_packets(0x1111, 11) + [b"PING" + b"\0" * 10] + stream_packets(0x2222, 3, src="N0CALL", last=False)
    got = peer.m17_streams(data)
    assert [(s["stream_id"], s["src"], s["frames"], s["last"]) for s in got] == [(0x1111, "XLX999", 11, True), (0x2222, "N0CALL", 3, False)]
    assert got[0]["dst"] == "@ALL" and got[0]["first_fn"] == 0
    assert peer.M17_FRAMES_MIN <= got[0]["frames"] <= peer.M17_FRAMES_MAX
    assert peer.m17_streams([]) == []


def test_header_text_decodes_the_fields():
    header = {"my": b"XLX999  ", "ur": b"ALL     ", "suffix": b"M17 ", "rpt1": b"XLX998 D", "rpt2": b"XLX998 G"}
    assert peer.header_text(header) == ("XLX999  ", "ALL     ", "M17 ", "XLX998 D", "XLX998 G")
    assert peer.header_text({})[0] == ""


def test_file_count(tmp_path):
    assert peer.file_count(tmp_path / "missing") == 0
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "x").write_bytes(b"1")
    (tmp_path / "a" / "b" / "y").write_bytes(b"2")
    assert peer.file_count(tmp_path) == 2


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_settle_returns_after_a_quiet_stretch():
    clock = Clock()
    reads = iter([1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2])
    last = []

    def read():
        last.append(next(reads))
        return last[-1]

    assert peer.settle(read, quiet=6.0, limit=100.0, poll=2.0, clock=clock, sleep=clock.sleep) == 2
    assert clock.now <= 12.0


def test_settle_gives_up_at_the_limit_when_it_never_settles():
    clock = Clock()
    n = iter(range(1000))
    value = peer.settle(lambda: next(n), quiet=10.0, limit=20.0, poll=2.0, clock=clock, sleep=clock.sleep)
    assert value >= 9 and clock.now >= 20.0


def test_wait_for_returns_the_hit_or_the_last_value_at_the_timeout():
    clock = Clock()
    n = iter(range(100))
    assert peer.wait_for(lambda: next(n), lambda v: v >= 3, timeout=30.0, poll=2.0, clock=clock, sleep=clock.sleep) == (3, True)
    clock = Clock()
    value, ok = peer.wait_for(lambda: 1, lambda v: v > 5, timeout=6.0, poll=2.0, clock=clock, sleep=clock.sleep)
    assert (value, ok) == (1, False) and clock.now >= 6.0


def test_the_case_constants_agree_with_the_rig():
    assert peer.ROOM == "D" and peer.ROOM_LABEL == "d" and peer.REFLECTOR_1 == "XLX999"
    assert peer.TALKER_FRAMES_LONG > 50 > peer.TALKER_FRAMES                  # the short stream guard: only the denied talk is short
    assert peer.SENDER_CALL != peer.TALKER_CALL


# 7b: node 1 is read twice (the text over alone, then the relayed second talker); the numbers are those of the first rig run
# (20261107T111542Z): the whole-case delta of node 1 was grant 1, closed 1, units 61, rec.records 1 (the relayed stream)

def test_own_over_problems_accepts_a_text_over_alone_and_names_what_moved():
    quiet = {"textover.sent": 1, "data.all": 1, "items.all": 2, "raw.sent": 1}      # 7c: node 1 delta of the text over alone
    assert peer.own_over_problems(quiet) == []
    bad = peer.own_over_problems({"rts.grant": 1, "closed": 1, "units.published": 61, "rec.records": 1, "rec.bytes": 8654})
    assert len(bad) == 4 and "rts.grant moved by 1" in bad[0] and any("recorder" in x for x in bad)
    assert peer.own_over_problems({"rec.records": 1}, recorder=False) == []


def test_relayed_stream_problems_wants_one_grant_one_close_and_the_frames():
    first_run = {"rts.grant": 1, "closed": 1, "units.published": 61}
    assert peer.relayed_stream_problems(first_run, 61) == []
    assert peer.relayed_stream_problems({"rts.grant": 1, "closed": 1, "units.published": 59}, 61) == []
    assert len(peer.relayed_stream_problems({"rts.grant": 2, "closed": 0, "units.published": 83}, 61)) == 3
    assert peer.relayed_stream_problems({}, 61) == ["grants 0, want 1", "closed streams 0, want 1", "published units 0, want about 61"]


def test_the_first_run_whole_case_delta_splits_into_the_two_checks():
    base = {"rts.grant": 0, "closed": 0, "units.published": 0, "rec.records": 0, "rec.bytes": 0, "textover.sent": 0}
    mid = dict(base, **{"textover.sent": 1})
    final = dict(mid, **{"rts.grant": 1, "closed": 1, "units.published": 61, "rec.records": 1, "rec.bytes": 8654})
    assert peer.own_over_problems(peer.delta(mid, base)) == []
    assert peer.relayed_stream_problems(peer.delta(final, mid), 61) == []
    assert peer.own_over_problems(peer.delta(final, base)) != []          # what the first run asserted over the whole case
