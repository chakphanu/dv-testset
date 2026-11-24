# SPDX-License-Identifier: MIT
"""dvtest.node1g (NODE1g, docs/node1g-cases.md): the switches, the hub counter reader, the refusal rate, the key files, the arrival evidence of a
P25 listener, the plain-HTTP admin client and the WAN router client, and the plumbing the cases rely on (the control-channel target `wan`, the
node1g services of the compose file, the addresses the wire checker must know). Stdlib only, no rig."""
import http.server
import json
import os
import re
import socketserver
import stat
import threading
from pathlib import Path

import pytest

from dvtest import control, nat, node1g as n1
from dvtest.proto import p25

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

METRICS = """\
# TYPE dv_edge_link_handshakes_total counter
dv_edge_link_handshakes_total{result="welcomed"} 2
dv_edge_link_handshakes_total{result="accepted"} 0
dv_edge_link_handshakes_total{result="auth"} 11
dv_edge_link_sessions 1
dv_edge_link_rtt_ms 121.5
dv_edge_link_datagrams_total{dir="rx",result="ok"} 40
dv_edge_link_datagrams_total{dir="rx",result="replay"} 1
dv_edge_link_datagrams_total{dir="tx",result="ok"} 45
dv_edge_link_refused_total{reason="busy"} 3
dv_launcher_child_restarts_total{child="dv-edge-link"} 1
dv_launcher_child_restarts_total{child="dv-room.c"} 0
dv_room_overs_total{room="C",result="end_missing"} 2
dv_room_overs_total{room="c",result="complete"} 5
dv_status_room_onair{room="c"} 0
dv_status_room_onair{room="b"} 1
dv_status_process_up{kind="dv-edge-link"} 1
dv_status_process_up{kind="dv-edge-radio"} 0
"""


def test_switches():
    assert n1.on({"DVT_NODE1G": "on"}) and n1.on({"DVT_NODE1G": "1"}) and not n1.on({}) and not n1.on({"DVT_NODE1G": "off"})
    assert n1.linkobs_on({"DVT_LINKOBS1": "on"}) and not n1.linkobs_on({"DVT_NODE1G": "on"})


def test_counters_reads_the_link_families():
    c = n1.counters(METRICS)
    assert c["hs.welcomed"] == 2 and c["hs.auth"] == 11 and c["hs.accepted"] == 0
    assert c["sessions"] == 1 and c["rtt_ms"] == 121.5
    assert c["dgram.rx.ok"] == 40 and c["dgram.rx.replay"] == 1 and c["dgram.tx.ok"] == 45
    assert c["refused.busy"] == 3 and c["restarts.dv-edge-link"] == 1
    assert c["overs.C.end_missing"] == 2 and c["overs.C.complete"] == 5      # the room label is folded to upper case
    assert c["onair.C"] == 0 and c["onair.B"] == 1
    assert c["procs_down"] == 1


def test_delta_and_collateral():
    a = n1.counters(METRICS)
    b = dict(a)
    b["hs.welcomed"] += 1
    b["dgram.rx.replay"] += 3
    moved = n1.delta(a, b)
    assert moved == {"hs.welcomed": 1, "dgram.rx.replay": 3}
    assert n1.collateral(moved) == []                     # welcomed and replay are not on the must-not-move list
    b["hs.bad_proof"] = 1.0
    b["dgram.rx.bad_tag"] = 2.0
    b["refused.loop"] = 1.0
    b["restarts.dv-edge-link"] += 1
    b["restarts.dv-room.c"] = 1.0
    bad = n1.collateral(n1.delta(a, b))
    assert len(bad) == 5 and any("bad_proof" in x for x in bad) and any("bad_tag" in x for x in bad) and any("loop" in x for x in bad)
    assert len(n1.collateral(n1.delta(a, b), allow_restarts=("dv-edge-link",))) == 4


def test_refusal_rate_counts_between_the_first_and_the_last_refresh():
    # the hub refreshes every 10 s: the value steps; reads every 2 s; 10 refusals per 10 s refresh
    samples = [(t, 10.0 * (t // 10)) for t in range(0, 125, 2)]
    rate, window, events = n1.refusal_rate(samples)
    assert rate == pytest.approx(1.0) and window == pytest.approx(110.0) and events == 110.0
    # a rate above the bound is seen as such
    fast = [(t, 12.0 * (t // 10)) for t in range(0, 125, 2)]
    assert n1.refusal_rate(fast)[0] == pytest.approx(1.2) and n1.refusal_rate(fast)[0] > n1.REFUSAL_RATE_MAX
    assert n1.refusal_rate([(0, 5.0), (3, 5.0), (6, 5.0)]) is None          # nothing grew
    assert n1.refusal_rate([(0, 0.0), (5, 3.0)]) is None                    # one change is not a window


def test_key_files_are_replaced_by_rename_with_mode_0600(tmp_path):
    path = tmp_path / "hs1.key"
    old, new = n1.new_key_hex(), n1.new_key_hex()
    assert old != new and n1.valid_key_text(old) and n1.valid_key_text(old + "\n")
    n1.write_key(path, old)
    inode = path.stat().st_ino
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and n1.read_key(path) == old
    n1.write_key(path, new)
    assert n1.read_key(path) == new and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_ino != inode                    # a new file renamed over the old one, never an in-place write
    assert [p.name for p in tmp_path.iterdir()] == ["hs1.key"]        # no temp file is left
    for bad in ("", "abc", "g" * 64, new + "00"):
        assert not n1.valid_key_text(bad)
        with pytest.raises(ValueError):
            n1.write_key(path, bad)
    assert n1.read_key(path) == new                       # a refused key changed nothing


def test_read_key_refuses_a_file_that_is_not_a_key(tmp_path):
    (tmp_path / "x.key").write_text("not a key\n")
    with pytest.raises(ValueError) as exc:
        n1.read_key(tmp_path / "x.key")
    assert "not a key" not in str(exc.value)              # the content never goes into a message


STATUS = {"link_edges": [{"edge": "link", "age_ms": 100, "sessions": [
    {"name": "account:n0call-hs1", "state": "connected", "rtt_ms": 120, "loss_ppm": 0, "ip": "172.30.0.98", "port": 40001, "last_error": None},
    {"name": "uplink:central", "state": "reconnecting", "last_error": "refused_key"}]}]}


def test_link_rows():
    assert len(n1.link_rows(STATUS)) == 2 and n1.link_rows({}) == [] and n1.link_rows(None) == []
    assert n1.link_row(STATUS, "uplink:central")["last_error"] == "refused_key"
    assert n1.link_row(STATUS, "uplink:none") is None
    assert "connected" in n1.row_summary(n1.link_row(STATUS, "account:n0call-hs1")) and n1.row_summary(None) == "no row"


def p25_listener(arrivals):
    """A stand-in for a P25Client whose inbox holds voice records at the given times."""
    class Inner:
        pass
    class Fake:
        pass
    fake = Fake()
    fake.client = Inner()
    fake.client.inbox, fake.client.inbox_times = [], []
    for t, imbe in arrivals:
        fake.client.inbox.append(p25.voice_records([imbe], 1023007, 31672)[0])
        fake.client.inbox_times.append(t)
    return fake


def test_arrival_evidence():
    frames = [bytes([i]) * 11 for i in range(1, 6)]
    fake = p25_listener([(10.0, frames[0]), (10.02, frames[1]), (12.0, frames[2]), (17.5, frames[3]), (17.52, b"\xaa" * 11)])
    # voice_records pads to whole LDUs: only the frames that were sent matter
    arr = n1.voice_arrivals(fake)
    got = {imbe for _, imbe in arr}
    assert set(frames[:4]) <= got
    assert n1.heard_share(arr, frames[:4]) == 1.0 and n1.heard_share(arr, frames) == pytest.approx(0.8)
    assert n1.heard_share(arr, frames[:4], since=11.0) == 0.5 and n1.heard_share(arr, []) == 0.0
    assert n1.first_heard(arr, frames[2:4]) == 12.0 and n1.first_heard(arr, frames[2:4], since=13.0) == 17.5
    assert n1.first_heard(arr, [b"\x77" * 11]) is None
    gap, at = n1.max_gap([1.0, 1.02, 6.5, 6.52])
    assert gap == pytest.approx(5.48) and at == 1.02 and n1.max_gap([1.0]) == (0.0, None)
    # only the frames of ONE over, inside its own window: the gap between the frames of that over
    gap, at = n1.stream_gap(arr, frames[:4], 9.0, 18.0)
    assert gap == pytest.approx(5.5) and at == 12.0
    assert n1.stream_gap(arr, frames[:1], 9.0, 18.0) == (0.0, None)
    assert n1.floor_probe_ok(0.8) and not n1.floor_probe_ok(0.79)
    assert len({n1.unmapped_id(n1.UNMAPPED_BASE, k) for k in range(40)}) == 40


class FakeAdmin(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeAdmin.seen.append((self.path, self.headers.get("Host"), self.headers.get("Origin"), self.headers.get("Cookie")))
        body = json.dumps(STATUS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_plain_admin_speaks_plain_http_with_the_host_and_origin_of_the_admin_mount():
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeAdmin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = n1.PlainAdmin("127.0.0.1", srv.server_address[1])
        status = client.get(client.path("/status"), expect=200).json()
    finally:
        srv.shutdown()
    assert n1.link_row(status, "uplink:central")["state"] == "reconnecting"
    path, host, origin, cookie = FakeAdmin.seen[0]
    port = srv.server_address[1]
    assert path == "/api/v1/admin/status" and host == "127.0.0.1:%d" % port and origin == "https://127.0.0.1:%d" % port


class RouterStub(socketserver.StreamRequestHandler):
    lines = []

    def handle(self):
        line = self.rfile.readline()
        req = json.loads(line)
        RouterStub.lines.append(req)
        ans = {"ok": req["op"] != "boom"}
        if req["op"] == "mappings":
            ans["mappings"] = [{"browser_port": 1, "seen_ip": "172.30.0.98", "seen_port": 40001}]
        if req["op"] == "boom":
            ans["error"] = "no such thing"
        self.wfile.write(nat.encode(ans))


def test_wan_client_speaks_the_nat_control_protocol():
    srv = socketserver.TCPServer(("127.0.0.1", 0), RouterStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        wan = n1.Wan("127.0.0.1", srv.server_address[1])
        RouterStub.lines.clear()
        wan.blackhole(True)
        wan.blackhole(False)
        wan.mode("symmetric")
        wan.readdress(True)
        assert wan.mappings()[0]["seen_port"] == 40001
        with pytest.raises(nat.NatError):
            wan.ctl("boom")
    finally:
        srv.shutdown()
    assert RouterStub.lines[:4] == [{"op": "blackhole", "state": "on"}, {"op": "blackhole", "state": "off"},
                                    {"op": "nat_mode", "mode": "symmetric"}, {"op": "readdress", "state": "on"}]
    for req in RouterStub.lines[:4]:                      # the router accepts exactly these requests
        nat.parse_request(json.dumps(req))


def test_netem_target_wan_goes_through_the_documented_channel(tmp_path):
    rc = control.RigControl(tmp_path)
    seen = []

    def watch():
        import time
        end = time.monotonic() + 5
        while time.monotonic() < end:
            for req in tmp_path.glob("*.req"):
                seen.append(req.read_text().strip())
                req.with_suffix(".ack").write_text("ok\n")
                req.unlink()
                return
            time.sleep(0.02)

    thread = threading.Thread(target=watch)
    thread.start()
    rc.netem("wan", n1.DELAY_ARGS, timeout=5)
    thread.join(5)
    assert seen == ["netem wan delay 60ms 10ms"]
    for args in (n1.DELAY_ARGS, n1.LOSS_ARGS, "off"):     # the values the cases use pass the host's own argument pattern
        assert re.fullmatch(r"[A-Za-z0-9 .%_-]+", args)


def test_run_sh_knows_the_wan_target_and_the_node1g_gate():
    text = (ROOT / "scripts" / "run.sh").read_text()
    assert re.search(r"^\s*wan\) target=\(netem-wan\) ;;", text, re.M)
    assert "DVT_NODE1G=on needs --profile twonode" in text and "netem_svcs+=(netem-wan)" in text
    assert "rig/config/dv-router/node1g-central.toml" in text and "rig/compose.node1g.yml" in text


def test_node1g_services_have_limits_and_the_router_has_no_forward():
    text = (ROOT / "rig" / "compose.yml").read_text()
    for svc, anchor in (("wan-router", "<<: *nat"), ("wan-edge", "<<: *nat"), ("netem-wan", "<<: *netem")):
        m = re.search(r"^  %s:\n((?:    .*\n|\n)+)" % svc, text, re.M)
        assert m, svc
        body = m.group(1)
        assert anchor in body and "profiles: [node1g]" in body        # the limit set (mem = swap, cpus, pids) comes with the anchor
    assert "--no-forward" in text and "DVT_NETEM_ALL" in text
    override = (ROOT / "rig" / "compose.node1g.yml").read_text()
    assert "dvr-peer:" in override and "tester:" in override and "nat-inside" in override


def test_templates_carry_one_uplink_and_one_account_row():
    hs = (ROOT / "rig/config/dv-router/node1g-hotspot.toml").read_text()
    central = (ROOT / "rig/config/dv-router/node1g-central.toml").read_text()
    assert hs.count("[[edges.link.uplink]]") == 1 and 'profile = "hotspot"' in hs and "<CENTRAL_IP>:31800" in hs
    assert central.count("[[edges.link.account]]") == 2 and 'rooms = ["C"]' in central                  # hs1 and the unused hs2 that keeps the edge alive on a revocation
    assert "link-secrets/hs2.key" in central
    for text in (hs, central):
        assert "/etc/dv-router/link-secrets/hs1.key" in text
        assert re.search(r"[0-9a-f]{64}", text) is None             # no key in a repo file
        text.encode("ascii")


def test_collateral_allow_rx_names_the_datagram_results_a_restart_may_move():
    moved = {"dgram.rx.unknown_session": 3.0, "dgram.rx.bad_tag": 1.0}
    assert len(n1.collateral(moved)) == 2
    assert len(n1.collateral(moved, allow_rx=("unknown_session",))) == 1
    assert n1.collateral({"dgram.rx.replay": 9.0, "hs.welcomed": 1.0, "hs.auth": 40.0}) == []


def test_rebase_treats_the_link_edge_series_as_restarted():
    before = {"hs.accepted": 1.0, "sessions": 1.0, "dgram.rx.ok": 900.0, "restarts.dv-room.c": 2.0, "overs.C.complete": 7.0}
    after = {"hs.accepted": 1.0, "sessions": 1.0, "dgram.rx.ok": 40.0, "restarts.dv-room.c": 2.0, "overs.C.complete": 9.0, "hs.auth": 3.0}
    assert n1.rebase(before, after) == {"hs.accepted": 1.0, "sessions": 1.0, "dgram.rx.ok": 40.0, "overs.C.complete": 2.0, "hs.auth": 3.0}
    assert n1.delta(before, after)["hs.accepted"] if "hs.accepted" in n1.delta(before, after) else True


def test_refusal_rate_skips_a_counter_that_restarted():
    # the edge restarted at t=20 (the value falls to 0): not a growth
    samples = [(t, 10.0 * (t // 10) if t < 20 else 10.0 * ((t - 20) // 10)) for t in range(0, 125, 2)]
    rate, window, events = n1.refusal_rate(samples)
    assert 0.85 < rate <= 1.0                           # the events of the step that hid the restart are lost: never an overcount


def test_missing_run_is_the_silence_inside_one_over_with_its_lost_tail():
    sent = [bytes([i]) * 11 for i in range(100)]
    arrivals = [(float(i), sent[i]) for i in range(40)] + [(60.0, sent[90])]          # frames 0..39 heard, then 40..89 missing, 90 heard
    run, at = n1.missing_run(arrivals, sent)
    assert run == pytest.approx(50 * 0.02) and at == 40
    tail = [(float(i), sent[i]) for i in range(40)]                                    # the rest never arrives: the run goes to the end
    assert n1.missing_run(tail, sent) == (pytest.approx(60 * 0.02), 40)
    assert n1.missing_run([(float(i), sent[i]) for i in range(100)], sent) == (0.0, None)
    assert n1.missing_run(arrivals, sent, since=45.0) == (pytest.approx(90 * 0.02), 0)   # earlier arrivals do not count when `since` is later


def test_linkrebind1_gate_is_off_by_default_and_counters_read_the_unit_families():
    assert not n1.linkrebind_on({}) and not n1.linkrebind_on({"DVT_NODE1G": "on"}) and n1.linkrebind_on({"DVT_LINKREBIND1": "on"})
    assert n1.LINKREBIND_GAP_S == 1.2 and n1.LINKREBIND_SHARE == 0.97
    text = ('dv_room_over_units_total{room="c",result="late"} 2\ndv_room_over_units_total{room="c",result="gap"} 18\n'
            'dv_edge_over_units_total{dir="rx",proc="link",result="late"} 1\ndv_edge_over_units_total{dir="rx",proc="p25",result="gap"} 3\n')
    c = n1.counters(text)
    assert c["ounits.C.late"] == 2 and c["ounits.C.gap"] == 18 and c["eunits.link.rx.late"] == 1 and c["eunits.p25.rx.gap"] == 3


def test_linkseq1_asserts_100_percent_accounting_and_loss_ppm():
    assert not n1.linkseq_on({}) and n1.linkseq_on({"DVT_LINKSEQ1": "on"})
    rows = {"central": {"loss_ppm": 0}, "hotspot": {"loss_ppm": 0}}
    assert n1.linkseq_problems({"h2c": 1.0, "c2h": 1.0}, 500, {"central": {}, "hotspot": {}}, rows) == []         # nothing missing, nothing needed
    # today's behaviour (run 20261123T234228Z): 4 of 500 missing each way, late 4 / 1, row loss_ppm = the reordered-datagram share
    mv = {"central": {"ounits.C.late": 4.0, "eunits.link.rx.late": 4.0}, "hotspot": {"ounits.B.late": 1.0, "eunits.link.rx.late": 1.0}}
    rows = {"central": {"loss_ppm": 22058}, "hotspot": {"loss_ppm": 13157}}
    out = n1.linkseq_problems({"h2c": 0.992, "c2h": 0.992}, 500, mv, rows)
    assert len([p for p in out if p.startswith("LINKSEQ1 a")]) == 2                                              # not 100 percent
    assert not [p for p in out if p.startswith("LINKSEQ1 b")]                                                    # 8 missing <= 10 counted
    assert len([p for p in out if p.startswith("LINKSEQ1 c")]) == 2                                              # 22058 ppm against 8000 ppm, 13157 against 8000
    mv = {"central": {"ounits.C.late": 1.0}, "hotspot": {}}
    assert any(p.startswith("LINKSEQ1 b") for p in n1.linkseq_problems({"h2c": 0.992, "c2h": 0.992}, 500, mv, rows))        # 8 missing, 1 counted
    ok_rows = {"central": {"loss_ppm": 8000}, "hotspot": {"loss_ppm": 11000}}
    assert not [p for p in n1.linkseq_problems({"h2c": 0.992, "c2h": 0.992}, 500, {"central": {"ounits.C.lost": 8.0}, "hotspot": {"ounits.B.gap": 1.0}}, ok_rows)
                if p.startswith(("LINKSEQ1 b", "LINKSEQ1 c"))]
    assert n1.unit_counters({"ounits.C.late": 2.0, "eunits.link.rx.gap": 1.0, "eunits.link.tx.lost": 3.0, "dgram.rx.ok": 9.0, "ounits.C.zzz": 1.0}) == {
        "ounits.C.late": 2.0, "eunits.link.rx.gap": 1.0, "eunits.link.tx.lost": 3.0}


def test_session_end_reasons_and_the_linkobs1_series_are_read():
    text = ('dv_edge_link_session_ends_total{reason="timeout"} 1\ndv_edge_link_session_ends_total{reason="bye"} 0\n'
            'dv_edge_link_session_ends_total{reason="revoked"} 2\ndv_edge_link_datagrams_total{dir="rx",result="bad_source"} 3\n'
            'dv_edge_link_handshakes_total{result="unknown_session"} 1\n')
    c = n1.counters(text)
    assert c["se.timeout"] == 1 and c["se.bye"] == 0 and c["se.revoked"] == 2 and c["dgram.rx.bad_source"] == 3 and c["hs.unknown_session"] == 1
    assert n1.ends_problems({"se.timeout": 1.0}, {"timeout": 1}) == []
    assert len(n1.ends_problems({"se.timeout": 1.0, "se.revoked": 1.0}, {"timeout": 1})) == 1
    assert len(n1.ends_problems({}, {"revoked": 1})) == 1 and n1.ends_problems({}) == []
    # collateral: bad_source and unknown_session only count with LINKOBS1, and unknown_session can be allowed
    moved = {"dgram.rx.bad_source": 1.0, "hs.unknown_session": 2.0}
    assert n1.collateral(moved) == [] and len(n1.collateral(moved, linkobs=True)) == 2
    assert len(n1.collateral(moved, allow_rx=("unknown_session",), linkobs=True)) == 1


CONFIG = """[node]
callsign = "XLX999"

[[edges.link.account]]
name = "n0call-hs1"
callsign = "N0CALL"
secret_file = "/etc/dv-router/link-secrets/hs1.key"
rooms = ["C"]
talk = true

[[edges.link.account]]
name = "n0call-hs2"
callsign = "N0TEST"
secret_file = "/etc/dv-router/link-secrets/hs2.key"
rooms = ["C"]
talk = false

[edges.p25]
enable = true
"""


def test_account_rows_are_removed_and_repointed_in_the_rendered_toml():
    without = n1.remove_account(CONFIG, "n0call-hs1")
    assert "hs1.key" not in without and "n0call-hs2" in without and without.count("[[edges.link.account]]") == 1 and "[edges.p25]" in without
    assert n1.remove_account(without, "n0call-hs2").count("[[edges.link.account]]") == 0
    moved = n1.set_account_secret_file(CONFIG, "n0call-hs1", "/etc/dv-router/link-secrets/hs1b.key")
    assert moved.count("hs1b.key") == 1 and "hs2.key" in moved and moved.count("secret_file") == 2
    assert n1.set_account_secret_file(moved, "n0call-hs1", "/etc/dv-router/link-secrets/hs1.key") == CONFIG
    for fn in (lambda: n1.remove_account(CONFIG, "nobody"), lambda: n1.set_account_secret_file(CONFIG, "nobody", "/x")):
        with pytest.raises(ValueError):
            fn()
    import tomllib
    assert tomllib.loads(without)["edges"]["link"]["account"][0]["name"] == "n0call-hs2"


def test_linkfix2_end_reason_is_read_from_a_separate_row_field():
    assert not n1.linkfix2_on({}) and n1.linkfix2_on({"DVT_LINKFIX2": "on"})
    assert n1.END_REASON_FIELDS == ("last_session_end",) and n1.SESSION_END_VALUES == ("timeout", "bye", "replaced", "revoked")
    row = {"state": "reconnecting", "last_error": "refused_key", "last_session_end": "revoked"}
    assert n1.end_reason_field(row) == "last_session_end" and n1.row_end_reason(row) == "revoked"
    assert n1.row_end_reason({"state": "connected", "last_session_end": "bye"}) == "bye" and n1.row_end_reason({"state": "connected"}) is None
    assert n1.row_end_reason({"last_session_end": ""}) is None and n1.row_end_reason(None) is None
    assert n1.row_end_reason({"end_reason": "bye", "last_end_reason": "bye"}) is None                      # only the one name is read
    assert n1.end_vs_hub_problems("r", None, {}) == [] and n1.end_vs_hub_problems("r", "revoked", {"se.revoked": 1.0}) == []
    out = n1.end_vs_hub_problems("r", "timeout", {"se.revoked": 1.0})
    assert len(out) == 1 and "session_ends{reason=timeout}" in out[0] and "se.revoked" in out[0]
    good = [("connected", None, None), ("reconnecting", "refused_key", "revoked"), ("reconnecting", "refused_key", "revoked")]
    assert n1.end_reason_problems(good, "r") == []
    # today's image: last_error carries the reason for under a second, then refused_key; no end-reason field at all
    today = [("connected", None, None), ("reconnecting", "refused_key", None)]
    out = n1.end_reason_problems(today, "r")
    assert len(out) == 1 and "None" in out[0] and "last_session_end" in out[0]
    assert any("refused_key" in p for p in n1.end_reason_problems([("reconnecting", "revoked", "revoked")], "r"))        # last_error must stay the attempt error
    assert n1.end_reason_problems([("connected", None, None)], "r") == ["r: no sample of the row while it retries"]


def test_keybox_never_shows_the_key():
    box = n1.KeyBox("ab" * 32)
    assert box.value == "ab" * 32 and "ab" not in repr(box) and "ab" not in str(box)
    assert "ab" * 32 not in repr({"old": box})
