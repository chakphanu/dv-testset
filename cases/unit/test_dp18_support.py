# SPDX-License-Identifier: MIT
"""cases/dp18_support.py, the DP18 builders of dvtest/proto/aprs.py and the case flows of cases/test_dp18_origin.py / test_dp18_rf.py against
fakes (no rig, no sleeps beyond milliseconds). The expected APRS lines are hand-written from APRS101 chapters 6, 8 and 11."""
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dp18_support as dc  # noqa: E402
import test_dp18_origin as to  # noqa: E402
import test_dp18_rf as trf  # noqa: E402

from dvtest import aprsis_client as ai, ax25, kiss_tnc, metrics, sut_config  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered(files, tx="true"):
    extras = [(SNIPPETS / name).read_text() for name in files]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", dp15_tx_enable=tx, extras=extras))


ORIGIN = ["dp15-aprsis.toml", "dp18-origin.toml"]


# ---------------------------------------------------------------- the APRS builders (hand-written literals)

def test_position_and_object_lines_match_hand_written_aprs101():
    assert aprs.lat_text_e7(490583333) == "4903.50N" and aprs.lon_text_e7(-720291667) == "07201.75W"          # 49 03.50 N, 72 01.75 W
    assert aprs.lat_text_e7(-125000000) == "1230.00S" and aprs.lon_text_e7(1799999999) == "18000.00E"            # carry into the degrees
    assert aprs.position_report(105000000, 202500000, "/&", "dvt") == "!1030.00N/02015.00E&dvt"
    assert aprs.object_report("DVTOBJ1", "123456", 105500000, 202500000, "/r", "144.800MHz room=B dvt") == \
        ";DVTOBJ1  *123456h1033.00N/02015.00Er144.800MHz room=B dvt"
    assert aprs.object_report("A", "000000", 105000000, 202500000, "/r", live=False).startswith(";A        _000000h")


def test_position_ambiguity_blanks_trailing_digits_of_both_coordinates():
    for level, lat, lon in [(1, "1030.0 N", "02015.0 E"), (2, "1030.  N", "02015.  E"), (3, "103 .  N", "0201 .  E"), (4, "10  .  N", "020  .  E")]:
        assert aprs.position_report(105000000, 202500000, "/&", "", level) == "!%s/%s&" % (lat, lon)


def test_object_and_position_parsers_round_trip():
    info = aprs.object_report("DVTOBJ1", "235959", 105500000, 202500000, "/r", "144.800MHz room=B dvt")
    obj = aprs.parse_object(info)
    assert (obj["name"], obj["live"], obj["hms"], obj["lat"], obj["lon"], obj["comment"]) == ("DVTOBJ1", True, "235959", "1033.00N", "02015.00E", "144.800MHz room=B dvt")
    assert aprs.parse_position_report("!1030.00N/02015.00E&dvt")["comment"] == "dvt"
    for bad in [lambda: aprs.object_report("TOOLONGNAME", "123456", 0, 0), lambda: aprs.object_report("A", "126000", 0, 0),
                lambda: aprs.parse_object("!1030.00N/02015.00E&"), lambda: aprs.lat_text_e7(900000001), lambda: aprs.position_report(0, 0, "/")]:
        with pytest.raises(ValueError):
            bad()


# ---------------------------------------------------------------- the rendered config

def test_origin_shape_from_the_real_snippet_and_the_stagger():
    shp = dc.origin_shape(rendered(ORIGIN))
    assert shp.gateway == "N0TEST-5" and shp.tocall == "APZDVR" and not shp.withheld
    assert [(r.kind, r.index, r.first_due_s) for r in shp.reports] == [("beacon", 0, 600.0), ("object", 1, 635.0)]
    assert dc.object_comment(shp.objects[0]) == "144.800MHz room=B dvt"
    assert dc.expected_info(shp.beacon) == "!1030.00N/02015.00E&dvt dp18 beacon"
    assert dc.expected_info(shp.objects[0], "010203") == ";DVTOBJ1  *010203h1033.00N/02015.00Er144.800MHz room=B dvt"
    with pytest.raises(ValueError):
        dc.expected_info(shp.objects[0])


def test_origin_shape_withheld_and_missing_pieces():
    assert dc.origin_shape(rendered(ORIGIN + ["dp18-origin-withheld.toml"])).withheld is True
    assert dc.origin_shape(rendered(["dp15-aprsis.toml"])) is None                                  # edge but no report
    assert dc.origin_shape({}) is None
    doc = rendered(ORIGIN)
    doc["data"] = {"positions": False}
    assert dc.origin_shape(doc).withheld is True


def test_objects_snippet_has_ten_staggered_rf_copies():
    shp = dc.origin_shape(rendered(["dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-objects.toml"]))
    assert len(shp.reports) == 11 and all(r.rf_ports == ["rf-a"] for r in shp.reports)
    assert [r.first_due_s for r in shp.reports][:3] == [600.0, 635.0, 670.0] and shp.reports[-1].first_due_s == 950.0
    names = [r.name for r in shp.objects]
    assert names[0] == "DVTOB01" and names[-1] == "DVTOB10" and len(set(names)) == 10
    assert len({(r.lat_e7, r.lon_e7) for r in shp.objects}) == 10


def test_sim_snippet_has_one_real_and_one_simulated_port_on_one_physical():
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml"])
    assert dc.tx_mode(doc, "rf-a") is True and dc.tx_mode(doc, "rf-b") == "simulate" and dc.tx_mode(doc, "nope") is None
    ports = doc["edges"]["ax25"]["port"]
    assert {p["physical"] for p in ports} == {"radio1"} and doc["edges"]["ax25"]["callsign"] == doc["edges"]["aprsis"]["callsign"] == "N0TEST-5"
    assert dc.port_index(doc, "rf-b") == 1 and dc.port_index(doc, "nope") is None
    assert {p["to"] for p in doc["data"]["path"] if p["name"].startswith("rf-out")} == {"edge:ax25/rf-a", "edge:ax25/rf-b"}


# ---------------------------------------------------------------- lines at the aprsc

def line(info, src="N0TEST-5", q="qAC,SRV", path="TCPIP*"):
    return ai.parse_line("%s>APZDVR,%s,%s:%s" % (src, path, q, info))


def test_check_origin_line_accepts_the_node_line_and_names_each_difference():
    shp = dc.origin_shape(rendered(ORIGIN))
    beacon = dc.expected_info(shp.beacon)
    assert dc.check_origin_line(line(beacon), shp.beacon, shp) == []
    obj = dc.expected_info(shp.objects[0], "101010")
    assert dc.check_origin_line(line(obj), shp.objects[0], shp) == []
    assert any("TCPIP*" in p for p in dc.check_origin_line(line(beacon, path="WIDE1-1"), shp.beacon, shp))
    assert any("qAC" in p for p in dc.check_origin_line(line(beacon, q="qAS,SRV"), shp.beacon, shp))
    assert any("2 q constructs" in p for p in dc.check_origin_line(line(beacon, q="qAC,SRV,qAO,X"), shp.beacon, shp))
    assert any("source" in p for p in dc.check_origin_line(line(beacon, src="N0TEST-7"), shp.beacon, shp))
    assert any("information" in p for p in dc.check_origin_line(line(beacon + " "), shp.beacon, shp))
    assert any("not an object" in p for p in dc.check_origin_line(line(beacon), shp.objects[0], shp))
    assert dc.classify(line(beacon), shp) == "beacon" and dc.classify(line(obj), shp) == "object:DVTOBJ1"
    assert dc.classify(line(beacon, src="N0TEST-7"), shp) is None and dc.classify(line(":N0TEST-7 :hi"), shp) is None


def test_hms_offset_and_tolerance_across_midnight():
    import calendar
    epoch = calendar.timegm((2026, 11, 10, 12, 34, 56))
    assert dc.hms_offset_s("123456", epoch) == 0 and dc.hms_offset_s("123455", epoch + 0.4) == pytest.approx(1.4)
    assert dc.hms_ok("123456", epoch + 0.3) and dc.hms_ok("123455", epoch + 0.4) and not dc.hms_ok("123450", epoch) and not dc.hms_ok("123500", epoch)
    midnight = calendar.timegm((2026, 11, 10, 0, 0, 0))
    assert dc.hms_offset_s("235959", midnight + 0.3) == pytest.approx(1.3) and dc.hms_ok("235959", midnight + 0.3)
    assert dc.hms_offset_s("235959", midnight + 1.0) == pytest.approx(2.0) and not dc.hms_ok("235959", midnight + 1.0)


def test_quiet_window_and_wall_of():
    assert dc.quiet_window_s(600, 15) == 585 and dc.quiet_window_s(10, 15) == 0.0
    assert dc.wall_of(90.0, now_wall=1000.0, now_mono=100.0) == 990.0


# ---------------------------------------------------------------- counters and frames

def samples(text):
    return metrics.parse(text)


def test_counter_readers():
    text = "\n".join([
        'dv_pkt_origin_total{kind="beacon",result="generated"} 2', 'dv_pkt_origin_total{kind="object",result="withheld"} 3',
        'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} 4', 'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="dropped_quota"} 6',
        'dv_edge_rf_tx_total{port="rf-b",kind="text",result="simulated"} 5', 'dv_edge_modem_total{port="rf-b",result="rx"} 9'])
    s = samples(text)
    assert dc.origin_counts(s) == {("beacon", "generated"): 2, ("object", "withheld"): 3}
    a = dc.rf_tx_counts(s, "rf-a")
    assert a == {("beacon", "sent"): 4, ("beacon", "dropped_quota"): 6} and dc.drops(a) == {"dropped_quota": 6}
    assert dc.result_total(dc.rf_tx_counts(s, "rf-b"), "simulated") == 5
    assert dc.port_family_total(s, "dv_edge_modem_total", "rf-b") == (9, True) and dc.port_family_total(s, "dv_edge_rf_ptt_total", "rf-b") == (0, False)


def test_object_frames_and_gaps():
    objf = kiss_tnc.ui_frame("N0TEST-5", "APZDVR", aprs.object_report("DVTOB01", "010203", 105000000, 202500000, "/r", "x"))
    beaconf = kiss_tnc.ui_frame("N0TEST-5", "APZDVR", aprs.position_report(105000000, 202500000))
    got = dc.object_frames([(1.0, 0, objf), (2.0, 0, beaconf), (3.0, 0, b"\x00junk")], ax25)
    assert len(got) == 1 and got[0][1] == "DVTOB01" and got[0][2] == "N0TEST-5"
    assert dc.spread_gaps([40.0, 5.0, 75.5]) == [35.0, 35.5]


# ---------------------------------------------------------------- the admin config API helpers

def test_tx_change_request_and_the_other_admin_helpers():
    body = dc.tx_change_request(1, "simulate", "abc", "dvt")
    assert body == {"base_etag": "abc", "mode": "apply", "comment": "dvt",
                    "ops": [{"op": "replace", "path": "/edges/ax25/port/1/tx", "value": "simulate"}]}
    with pytest.raises(ValueError):
        dc.tx_change_request(0, "on", "e", "c")
    assert dc.etag_of(SimpleNamespace(json=lambda: {"meta": {"etag": "h1"}}, headers={})) == "h1"
    assert dc.etag_of(SimpleNamespace(json=lambda: {}, headers={"ETag": "h2"})) == "h2"
    assert dc.etag_of(SimpleNamespace(json=lambda: (_ for _ in ()).throw(ValueError()), headers={})) is None
    assert dc.history_seqs([{"seq": 3}, {"seq": 1}, 2, {"x": 1}]) == [1, 2, 3] and dc.history_seqs({"items": [{"seq": 7}]}) == [7]
    assert dc.history_seqs({"history": [{"seq": True}]}) == [] and dc.history_seqs("x") == []
    status = {"launcher": {"children": [{"name": "dv-edge-ax25", "pid": 10, "restarts": 0, "state": "up", "since_ms": 5}]}}
    before = dc.child_state(status, "dv-edge-ax25")
    assert before["pid"] == 10 and dc.child_state(status, "nope") is None and dc.child_state({}, "x") is None
    assert dc.restarted(before, dict(before, restarts=1)) and dc.restarted(before, dict(before, pid=11)) and not dc.restarted(before, dict(before))
    assert not dc.restarted(None, before)
    assert dc.schema_has_dp18('{"simulate": 1}') is False                                                    # needs all three markers
    assert dc.schema_has_dp18('{"const": "simulate", "properties": {"beacon": {}, "objects": {}}}')
    assert not dc.schema_has_dp18('{"properties": {"enable": {}}}')


# ---------------------------------------------------------------- the origin case flows against fakes

def origin_ctx(withheld=False):
    shp = dc.origin_shape(rendered(ORIGIN + (["dp18-origin-withheld.toml"] if withheld else [])))
    return SimpleNamespace(shape=shp, host="h", admin=None, base={}, interval_s=600)


def first_lines_for(shp, hms_offset=0.0, object_delay=35.0, q="qAC,SRV", dest_ok=True):
    import calendar
    wall = calendar.timegm((2026, 11, 10, 12, 0, 0)) + 0.2
    out = {}
    for report in shp.reports:
        at = 1000.0 + (object_delay if report.kind == "object" else 0.0)
        w = wall + (object_delay if report.kind == "object" else 0.0)
        hms = time.strftime("%H%M%S", time.gmtime(int(w - hms_offset)))
        info = dc.expected_info(report, hms)
        out[to.report_key(report)] = (w, line(info, q=q), at)
    out["_uptime_now"] = 700.0 + (time.monotonic() - 1000.0) if False else None
    return out


NOTE = SimpleNamespace(out="/nonexistent")


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(to.ds, "write_note", lambda *a, **k: None)


def test_flow_a_b_c_pass_on_a_good_set_and_fail_on_each_defect(quiet):
    ctx = origin_ctx()
    good = first_lines_for(ctx.shape)
    to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, good, NOTE)
    to.test_dp18_b_the_object_comes_35_s_after_the_beacon(ctx, good, NOTE)
    to.test_dp18_c_the_object_timestamp_is_the_send_time(ctx, good, NOTE)
    with pytest.raises(AssertionError, match="qAC"):
        to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, first_lines_for(ctx.shape, q="qAS,SRV"), NOTE)
    with pytest.raises(AssertionError, match="after the beacon"):
        to.test_dp18_b_the_object_comes_35_s_after_the_beacon(ctx, first_lines_for(ctx.shape, object_delay=60.0), NOTE)
    with pytest.raises(AssertionError, match="stamp"):
        to.test_dp18_c_the_object_timestamp_is_the_send_time(ctx, first_lines_for(ctx.shape, hms_offset=5.0), NOTE)


def test_flow_a_floor_uses_the_node_uptime(quiet):
    ctx = origin_ctx()
    good = first_lines_for(ctx.shape)
    first = min(v[2] for k, v in good.items() if k != "_uptime_now")
    good["_uptime_now"] = 650.0 + (time.monotonic() - first)                        # the beacon came at node uptime ~650 s: after the 600 s floor
    to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, good, NOTE)
    good["_uptime_now"] = 300.0 + (time.monotonic() - first)                        # 300 s: before one full interval
    with pytest.raises(AssertionError, match="before one full interval"):
        to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, good, NOTE)


class FakeHub:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


def test_flow_n_counts_generated_reports(quiet, monkeypatch):
    monkeypatch.setattr(to, "COUNTER_WAIT_S", 0.0)
    ctx = origin_ctx()
    hub = FakeHub('dv_pkt_origin_total{kind="beacon",result="generated"} 1\ndv_pkt_origin_total{kind="object",result="generated"} 1')
    to.test_dp18_n_origin_counters_count_generated_reports(ctx, {}, hub, NOTE)
    with pytest.raises(AssertionError, match="grew by 0"):
        to.test_dp18_n_origin_counters_count_generated_reports(ctx, {}, FakeHub('dv_pkt_origin_total{kind="beacon",result="generated"} 1'), NOTE)
    with pytest.raises(AssertionError, match="not generated for a reason"):
        to.test_dp18_n_origin_counters_count_generated_reports(ctx, {}, FakeHub(hub.text() + '\ndv_pkt_origin_total{kind="object",result="withheld"} 1'), NOTE)


class FakeSession:
    def __init__(self, lines=()):
        self.lines = list(lines)

    def mark(self):
        return len(self.lines)

    def snapshot(self):
        return [(time.monotonic(), l) for l in self.lines]


def test_flow_e_withheld_sends_nothing_and_counts_withheld(quiet, monkeypatch):
    monkeypatch.setattr(to.time, "sleep", lambda s: None)
    ctx = origin_ctx(withheld=True)
    hub = FakeHub('dv_pkt_origin_total{kind="beacon",result="withheld"} 2\ndv_pkt_origin_total{kind="object",result="withheld"} 2')
    to.test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, FakeSession(), hub, NOTE)
    beacon = dc.expected_info(ctx.shape.beacon)
    sent = FakeSession(["N0TEST-5>APZDVR,TCPIP*,qAC,SRV:" + beacon])
    sent.snapshot = lambda: [(time.monotonic(), l) for l in sent.lines]
    sent.mark = lambda: 0
    with pytest.raises(AssertionError, match="sent a line"):
        to.test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, sent, hub, NOTE)
    with pytest.raises(pytest.skip.Exception, match="does not withhold"):
        to.test_dp18_e_a_withheld_gateway_call_sends_nothing(origin_ctx(), FakeSession(), hub, NOTE)
    with pytest.raises(AssertionError, match="generated although"):
        to.test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, FakeSession(), FakeHub(hub.text() + '\ndv_pkt_origin_total{kind="beacon",result="generated"} 1'), NOTE)


# ---------------------------------------------------------------- the reload helpers against a fake admin

class FakeAdmin:
    def __init__(self, writable=True, post_status=200, etag="e1"):
        self.writable, self.post_status, self.etag, self.posts, self.step_ups = writable, post_status, etag, [], 0

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def step_up(self):
        self.step_ups += 1

    def get(self, path, expect=None):
        return SimpleNamespace(status=200, headers={}, text=lambda: "", json=lambda: {"meta": {"etag": self.etag, "writable": self.writable}})

    def post(self, path, body, expect=None):
        self.posts.append((path, body))
        return SimpleNamespace(status=self.post_status, text=lambda: "boom", json=lambda: {})


def test_apply_tx_posts_the_replace_with_the_current_etag_and_skips_when_read_only():
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml"])
    admin = FakeAdmin()
    sim = SimpleNamespace(admin_adm=admin, doc=doc)
    trf.apply_tx(sim, "rf-b", True, "dvt")
    path, body = admin.posts[0]
    assert path == "/api/v1/admin/config/changes" and body["base_etag"] == "e1" and body["ops"][0]["path"] == "/edges/ax25/port/1/tx" and admin.step_ups == 1
    with pytest.raises(pytest.skip.Exception, match=r"not writable.*DVT_SUT_CONFIG_RW=1"):
        trf.apply_tx(SimpleNamespace(admin_adm=FakeAdmin(writable=False), doc=doc), "rf-b", True, "dvt")
    with pytest.raises(pytest.skip.Exception, match=r"read_only.*DVT_SUT_CONFIG_RW=1"):
        trf.apply_tx(SimpleNamespace(admin_adm=FakeAdmin(post_status=409), doc=doc), "rf-b", True, "dvt")
    with pytest.raises(AssertionError, match="HTTP 422"):
        trf.apply_tx(SimpleNamespace(admin_adm=FakeAdmin(post_status=422), doc=doc), "rf-b", True, "dvt")


def test_frames_with_matches_the_posted_body_at_a_fake_tnc():
    tnc = SimpleNamespace(data_frames=lambda since=0.0: [(5.0, 0, kiss_tnc.ui_frame("N0TEST-5", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", "hello DVT123")))])
    assert [m["text"] for _t, m in trf.frames_with(tnc, "DVT123")] == ["hello DVT123"] and trf.frames_with(tnc, "other") == []


def test_every_dp18_case_skips_on_a_run_without_its_shape(tmp_path, monkeypatch):
    """No shape, no capability: each fixture skips with a reason (the cases never fail on a run that did not ask for them)."""
    monkeypatch.setattr(to.p1, "read_config", lambda target: {})
    with pytest.raises(pytest.skip.Exception, match="no enabled|has no enabled"):
        to.ctx.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, None)
    monkeypatch.setattr(trf.p1, "read_config", lambda target: {})
    with pytest.raises(pytest.skip.Exception, match="rf-a tx = true"):
        trf.sim.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, None)
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml"])
    monkeypatch.setattr(trf.p1, "read_config", lambda target: doc)
    old = SimpleNamespace(path=lambda s: s, get=lambda p, expect=None: SimpleNamespace(status=200, text=lambda: '{"properties": {"enable": {}}}'))
    with pytest.raises(pytest.skip.Exception, match="does not know DP12c"):
        trf.sim.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, lambda role: old)


# ---------------------------------------------------------------- DP18-k with a fake clock

class Clock:
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_k(monkeypatch, line_offsets):
    """The kill case with `line_offsets` = seconds after the kill at which the node's own beacon line reaches the aprsc."""
    clock = Clock()
    monkeypatch.setattr(to, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    monkeypatch.setattr(to.ds, "write_note", lambda *a, **k: None)
    ctx = origin_ctx()
    t_kill = clock.t + 1.0
    beacon = "N0TEST-5>APZDVR,TCPIP*,qAC,SRV:" + dc.expected_info(ctx.shape.beacon)

    class Session:
        def mark(self):
            return 0

        def snapshot(self):
            return [(t_kill + off, beacon) for off in line_offsets if t_kill + off <= clock.t]

    control = SimpleNamespace(kill_proc=lambda *a, **k: SimpleNamespace(t_kill_monotonic=t_kill, pids=[7]))
    to.test_dp18_k_nothing_arrives_before_one_full_interval_after_a_worker_kill(ctx, {}, Session(), control, NOTE)
    return clock


def test_flow_k_passes_when_the_new_worker_waits_a_full_interval(monkeypatch):
    clock = run_k(monkeypatch, [610.0])
    assert clock.t >= 5000.0 + 585.0


def test_flow_k_fails_on_an_early_line_and_on_a_missing_report(monkeypatch):
    with pytest.raises(AssertionError, match="within 585 s of the worker kill"):
        run_k(monkeypatch, [120.0])
    with pytest.raises(AssertionError, match="sent no report"):
        run_k(monkeypatch, [])


# ---------------------------------------------------------------- DP18-s with fakes

class FakeTnc:
    def __init__(self):
        self.frames, self.received, self.connections = [], [], 1

    def data_frames(self, since=0.0):
        return [f for f in self.frames if f[0] >= since]

    def is_connected(self):
        return True


def run_s(monkeypatch, sim_frame_at_b=False, rf_b_counts_sent=False, ptt_moves=False, a_frames=1):
    clock_state = {"posts": 0}
    a, b = FakeTnc(), FakeTnc()
    hub_state = {"a_sent": 0, "b_sim": 0, "b_sent": 0, "ptt": 0}

    class Hub:
        def text(self):
            return "\n".join([
                'dv_edge_rf_tx_total{port="rf-a",kind="text",result="sent"} %d' % hub_state["a_sent"],
                'dv_edge_rf_tx_total{port="rf-b",kind="text",result="simulated"} %d' % hub_state["b_sim"],
                'dv_edge_rf_tx_total{port="rf-b",kind="text",result="sent"} %d' % hub_state["b_sent"],
                'dv_edge_modem_total{port="rf-b",result="tx"} 0', 'dv_edge_rf_ptt_total{port="rf-b"} %d' % hub_state["ptt"]])

    def fake_post(poster, text, pace=True):
        t = time.monotonic()
        clock_state["posts"] += 1
        for _ in range(a_frames):
            a.frames.append((t + 0.5, 0, kiss_tnc.ui_frame("N0TEST-5", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", text))))
        if sim_frame_at_b:
            b.frames.append((t + 0.6, 0, kiss_tnc.ui_frame("N0TEST-5", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", text))))
        hub_state["a_sent"] += 1
        hub_state["b_sim"] += 1
        hub_state["b_sent"] += 1 if rf_b_counts_sent else 0
        hub_state["ptt"] += 1 if ptt_moves else 0
        return t, t, 202

    monkeypatch.setattr(trf.tr, "post", fake_post)
    monkeypatch.setattr(trf.time, "sleep", lambda s: None)
    monkeypatch.setattr(trf.ds, "write_note", lambda *a, **k: None)
    monkeypatch.setattr(trf.ds, "nonce_body", lambda tag: "%s-%d" % (tag, clock_state["posts"]))
    sim = SimpleNamespace(rf=SimpleNamespace(a=a, b=b, tx_guard=lambda case: None), web=SimpleNamespace(posters=[object()] * 3))
    trf.test_dp18_s_a_simulated_port_never_reaches_its_tnc(None, NOTE, Hub(), sim)


def test_flow_s_passes_when_only_the_real_port_transmits(monkeypatch):
    run_s(monkeypatch)


def test_flow_s_fails_on_each_way_a_simulated_port_can_leak(monkeypatch):
    with pytest.raises(AssertionError, match="simulate port's TNC"):
        run_s(monkeypatch, sim_frame_at_b=True)
    with pytest.raises(AssertionError, match="counted a real send"):
        run_s(monkeypatch, rf_b_counts_sent=True)
    with pytest.raises(AssertionError, match="PTT"):
        run_s(monkeypatch, ptt_moves=True)
    with pytest.raises(AssertionError, match="more than once|exactly 1"):
        run_s(monkeypatch, a_frames=2)


def test_the_dp18_modules_never_write_files_and_the_docs_name_the_rw_variable():
    """The reload cases change the config through the admin API only: no file write, no direct edit of the rendered dv-router.toml."""
    import re
    for name in ("cases/dp18_support.py", "cases/test_dp18_rf.py", "cases/test_dp18_origin.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"write_text|write_bytes|shutil\.|os\.(replace|rename|remove|unlink)|open\([^)]*[\"'][wa]b?[\"']", text), name
    docs = (ROOT / "docs" / "dp18-cases.md").read_text(encoding="utf-8")
    s_line = next(l for l in docs.split("\n") if l.startswith("... DVT_SUT_CONFIG_RW=1 DVT_SUT_CONFIG_EXTRA=") and "dp18-rf.toml scripts" in l)
    assert "-k \"dp18_s or dp18_r\"" in s_line
