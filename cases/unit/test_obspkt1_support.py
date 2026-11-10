# SPDX-License-Identifier: MIT
"""dvtest/obspkt1.py and the assertion machinery of cases/test_obspkt1_monitor.py against synthetic snapshots (no rig). The row shapes are those of
openapi-admin.yaml (PktMonRow, PacketSnapshot) and data.md 13.5 at merge b9a6839e."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_obspkt1_monitor as tm  # noqa: E402

from dvtest import ax25, obspkt1 as ob, web  # noqa: E402

pytestmark = pytest.mark.unit

BOOT = list(range(16))
W, E_AX, E_IS = "pkt-0000000000000001", "ax25-0000000000000007", "aprsis-0000000000000007"


def row(pid, seq, port, direction, text, out=(("rf", "heard"),), key="line", **extra):
    r = {"id": pid, "boot": BOOT, "seq": seq, "t_ms": 1700000000000 + seq, "port": port, "dir": direction, key: text,
         "out": [{"stage": s, "result": res} for s, res in out]}
    r.update(extra)
    return r


MK = {k: "OBSPKT1X%s0A1B2C" % k for k in "SNHVMWAIRCP"}


def synthetic():
    rows = [
        row(W, 20, "rf-a", "rx", "N0OBA-1>APDVT1,WIDE1-1*:>" + MK["S"], (("rf", "heard"), ("route", "forwarded")), ref=5),
        row(E_AX, 9, "rf-a", "rx", "N0OBA-1>APDVT1,WIDE1-1*", (("modem", "rx"), ("raw", "sent")), key="header", ref=5),
        row(W, 21, "rf-a", "rx", "N0OBB-1>APDVT1,WIDE1-1*,NOGATE:>" + MK["N"], (("route", "no_gate"),), ref=6),
        row(E_AX, 10, "rf-a", "rx", "N0OBB-1>APDVT1,WIDE1-1*,NOGATE", (("modem", "rx"),), key="header", ref=6),
        row(W, 22, "rf-a", "rx", "N0OBC-1>APDVT1,WIDE1-1*:?APRS?", (("route", "filtered"),), ref=7),
        row(E_AX, 11, "rf-a", "rx", "N0OBC-1>APDVT1,WIDE1-1*", (("modem", "rx"),), key="header", ref=7),
        row(E_IS, 3, "is", "tx", "N0OBA-1>APDVT1,WIDE1-1*,qAO,N0TEST-1", (("write", "sent"),), key="header", ref=40),
        row(W, 23, "rf-a", "rx", "N0HID-7>APDVT1,WIDE1-1*:", (("rf", "heard"),), ref=8, redacted="position_policy"),
        row(W, 24, "rf-a", "rx", "N0OBD-1>APDVT1,WIDE1-1*:=1030.42N/02015.17E>" + MK["V"], ref=9),
        row(W, 25, "rf-a", "rx", "N0HID-9>(mic-e),WIDE1-1*:", ref=10, redacted="position_policy"),
        row(E_AX, 12, "rf-a", "rx", "N0HID-9>(mic-e),WIDE1-1*", (("modem", "rx"),), key="header", ref=10),
        row(E_AX, 13, "rf-a", "rx", "N0OBE-1>(mic-e),WIDE1-1*", (("modem", "rx"),), key="header", ref=11),
        row(W, 26, "rf-a", "rx", "N0OBE-1>(mic-e),WIDE1-1*:`abc" + MK["W"], ref=11),
        row(W, 27, "rf-a", "rx", "N0OBH-5>APDVT1,WIDE1-1*:=0912.34N/01945.67E>" + MK["A"], ref=12),
        row(W, 28, "is", "rx", "N0TEST-7>APDVT1,TCPIP*,qAC,SRV::XLX999B  :OBS " + MK["I"] + "{81", ref=2),
        row(E_IS, 4, "is", "rx", "N0TEST-7>APDVT1,TCPIP*,qAC,SRV", (("raw", "sent"),), key="header", ref=2),
        row(W, 29, "rf-a", "tx", "N0TEST-1>APZDVR::XLX999B  :OBS " + MK["R"], (("render", "sent"),), ref=3),
        row(E_AX, 14, "rf-a", "tx", "N0TEST-1>APZDVR", (("txcore", "queued"), ("sched", "sent")), key="header", ref=3),
        row(E_AX, 15, "rf-a", "tx", "N0TEST-7>APDR16,WIDE1-1::N0OBC    :" + MK["C"], (("client", "queued"), ("sched", "sent"))),
        row(E_AX, 16, "rf-a", "tx", "N0TEST-7>APDR16,WIDE1-1:", (("client", "queued"),), redacted="position_policy"),
    ]
    return rows


def traffic(rows=None):
    rows = synthetic() if rows is None else rows
    return SimpleNamespace(markers={k: MK[k] for k in MK}, body={"rows": rows}, rows=rows, mice_dest_hidden="SWSW20", mice_dest_visible="TQTQ30")


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(tm.ds, "write_note", lambda *a, **k: None)


# ---------------------------------------------------------------- the helpers

def test_a_good_snapshot_has_no_problems_and_each_defect_is_named():
    body = {"rows": synthetic(), "ports": [{"id": E_AX, "port": "rf-a", "first_seq": 1, "last_seq": 16}], "producers": [{"id": W, "boot": BOOT}]}
    assert ob.snapshot_problems(body) == []
    assert ob.snapshot_problems([]) == ["the body is not an object"]
    assert ob.snapshot_problems({"rows": [], "ports": {}, "producers": []}) == ["'ports' is not a list"]
    bad = json.loads(json.dumps(body))
    bad["rows"][0]["dir"] = "both"
    bad["rows"][1]["out"][0]["stage"] = "mystery"
    bad["rows"][2]["redacted"] = "other"
    bad["rows"][3]["boot"] = [1, 2]
    bad["producers"][0]["boot"] = [300] * 16
    problems = ob.snapshot_problems(bad)
    assert len(problems) >= 5 and any("dir" in p for p in problems) and any("outcome" in p for p in problems) and any("redacted" in p for p in problems)
    assert any("exceed the limit" in p for p in ob.snapshot_problems(body, limit=3))
    assert ob.row_problems({"seq": 1}) and ob.row_problems(dict(synthetic()[0], line="x", header="y"))


def test_newest_first_per_producer_and_port():
    rows = [dict(seq=9, id="a", port="p"), dict(seq=3, id="a", port="p"), dict(seq=7, id="b", port="p"), dict(seq=1, id="b", port="p")]
    assert ob.newest_first(rows) and not ob.newest_first(rows[::-1])


def test_tnc2_split_sources_and_outcomes():
    assert ob.split_tnc2("N0OBA-1>APDVT1,WIDE1-1*,qAO,N0TEST-1:>text:with colon") == ("N0OBA-1", "APDVT1", ["WIDE1-1*", "qAO", "N0TEST-1"], ">text:with colon")
    assert ob.split_tnc2("no header") is None
    rows = synthetic()
    assert ob.row_src(rows[0]) == "N0OBA-1" and ob.row_src(rows[1]) == "N0OBA-1" and ob.row_src({"line": "x"}) is None
    assert ob.producer_kind(W) == "worker" and ob.producer_kind(E_AX) == ob.producer_kind(E_IS) == "edge"
    assert len(ob.worker_rows(rows)) + len(ob.edge_rows(rows)) == len(rows)
    assert ob.outcomes(rows[0]) == [("rf", "heard"), ("route", "forwarded")] and ob.has_outcome(rows[0], "route", "forwarded") and not ob.has_outcome(rows[0], "route", "no_gate")
    assert ob.info_of(rows[7]) == "" and ob.info_of(rows[1]) is None and ob.mentions(rows, MK["S"]) and not ob.mentions(rows, "nothing-here")
    assert len(ob.rows_from(rows, "N0OBA-1", dir="rx", port="rf-a")) == 2 and [r["port"] for r in ob.rows_from(rows, "N0OBA-1", dir="tx")] == ["is"]


def test_parse_stream_hello_rows_gap_bye_resync():
    frames = [{"event": "hello", "data": json.dumps({"producers": [{"id": W, "boot": BOOT}]}), "retry": "3000"},
              {"event": "rows", "data": json.dumps({"id": W, "boot": BOOT, "rows": [{"seq": 1}, {"seq": 2}]})},
              {"event": "gap", "data": json.dumps({"id": E_AX})}, {"event": "bye", "data": json.dumps({"reason": "limit"})},
              {"event": "resync", "data": ""}, {"event": "odd", "data": "{}"}, {"event": "rows", "data": "{not json"}]
    ev = ob.parse_stream(frames)
    assert ev.hello == [{"id": W, "boot": BOOT}] and [r[2]["seq"] for r in ev.rows] == [1, 2] and ev.gaps == [E_AX]
    assert ev.byes == [{"reason": "limit"}] and ev.resync and ev.other == ["odd"] and len(ev.bad) == 1


def test_leaks_markers_and_the_step_up_wait():
    assert ob.find_leaks({"a": "xx " + MK["S"], "b": "clean"}, [MK["S"], MK["N"]]) == {"a": [MK["S"]]}
    assert ob.find_leaks({"a": None}, [MK["S"]]) == {}
    m1, m2 = ob.new_marker("S"), ob.new_marker("S")
    assert m1 != m2 and m1.startswith("OBSPKT1XS") and m1.isalnum()
    assert ob.stale_wait_s(0) == 315.0 and ob.stale_wait_s(400) == 0.0 and ob.stale_wait_s(100) == 215.0


def test_the_injected_frames_are_valid_ax25_and_the_mic_e_destination_is_encoded():
    frame, dest = ob.mice_frame(ob.SRC_HIDDEN_MICE, 37.62, -122.4, "MARK")
    parsed = ax25.parse_frame(frame)
    assert parsed["dest"]["text"] == dest and len(dest) == 6 and parsed["info"][:1] == b"`" and parsed["info"].endswith(b"MARK")
    for src, info in ((ob.SRC_STATUS, ">x"), (ob.SRC_HIDDEN_POS, ob.position_info(ob.POS_HIDDEN, "m"))):
        assert ax25.dvrouter_accepts_frame(ob.rf_frame(src, info))
    assert ob.position_info(ob.POS_VISIBLE, "m") == "=1030.42N/02015.17E>m"
    assert len({ob.SRC_STATUS, ob.SRC_NOGATE, ob.SRC_QUERY, ob.SRC_HIDDEN_POS, ob.SRC_HIDDEN_MICE, ob.SRC_VISIBLE_POS, ob.SRC_VISIBLE_MICE, ob.SRC_ACCOUNT}) == 8
    assert ob.SRC_HIDDEN_POS.startswith(ob.HIDDEN_BASE) and ob.SRC_ACCOUNT.startswith(ob.ACCOUNT)


# ---------------------------------------------------------------- the case assertions against the synthetic rows

def test_row_and_privacy_cases_pass_on_the_expected_rows_and_fail_on_each_defect(quiet):
    t = traffic()
    ctx = SimpleNamespace()
    tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, t, None)
    tm.test_obspkt1_b_aprs_is_rx_and_rf_tx_rows(ctx, t, None)
    tm.test_obspkt1_b_a_local_kiss_client_row_keeps_its_line_but_not_a_position(ctx, t, None)
    tm.test_obspkt1_c_a_hidden_stations_position_is_blank_in_every_row(ctx, t, None)
    tm.test_obspkt1_c_mic_e_destinations_show_the_placeholder(ctx, t, None)
    tm.test_obspkt1_c_edge_rows_of_worker_decided_packets_have_no_line(ctx, t)
    # each defect
    bad = synthetic()
    bad[2]["out"] = [{"stage": "route", "result": "forwarded"}]                         # NOGATE gated: the decision is wrong
    with pytest.raises(AssertionError, match="no_gate"):
        tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic(bad), None)
    bad = synthetic()
    bad[1]["line"] = "N0OBA-1>APDVT1,WIDE1-1*:>" + MK["S"]                              # an edge row with the line
    with pytest.raises(AssertionError, match="edge rx row carries a line"):
        tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic(bad), None)
    bad = synthetic()
    bad[7]["line"] = "N0HID-7>APDVT1,WIDE1-1*:=1137.77N/02242.33E>" + MK["H"]             # the hidden position leaked
    with pytest.raises(AssertionError, match="hidden station row"):
        tm.test_obspkt1_c_a_hidden_stations_position_is_blank_in_every_row(ctx, traffic(bad), None)
    bad = synthetic()
    bad[11]["header"] = "N0OBE-1>TQTQ30,WIDE1-1*"                                         # the edge shows the real Mic-E destination
    with pytest.raises(AssertionError, match="edge rows of N0OBE-1"):
        tm.test_obspkt1_c_mic_e_destinations_show_the_placeholder(ctx, traffic(bad), None)
    bad = synthetic()
    bad[8]["line"] = bad[8]["line"]
    bad.append(row(E_IS, 9, "is", "tx", "N0X>Y:z", key="line"))
    with pytest.raises(AssertionError, match="edge rows with a line"):
        tm.test_obspkt1_c_edge_rows_of_worker_decided_packets_have_no_line(ctx, traffic(bad))


# ---------------------------------------------------------------- access, leaks and the wait, with fakes

class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def text(self):
        return json.dumps(self._body)


class FakeAdmin:
    def __init__(self, status=403, code="forbidden", sse_status=403, body=None):
        self.status, self.code, self.sse_status, self.body, self.step_ups, self.authenticator = status, code, sse_status, body, 0, None

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def step_up(self):
        self.step_ups += 1

    def get(self, path, expect=None):
        if self.status == 200:
            return FakeResp(200, self.body, {"Cache-Control": "no-store"})
        return FakeResp(self.status, {"code": self.code})

    def sse(self, path, max_events=1, timeout=1.0, **kw):
        raise web.WebError("refused", self.sse_status)


def test_access_case_for_viewer_and_operator_needs_403_forbidden_on_both_routes(quiet):
    ctx = SimpleNamespace(admin_session=lambda role: FakeAdmin())
    tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(ctx)
    with pytest.raises(AssertionError, match="viewer GET packets: HTTP 200"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin_session=lambda role: FakeAdmin(status=200, body={})))
    with pytest.raises(AssertionError, match="code 'step_up_required'"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin_session=lambda role: FakeAdmin(code="step_up_required")))
    with pytest.raises(AssertionError, match="packets/stream: HTTP 200"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin_session=lambda role: FakeAdmin(sse_status=200)))


def test_the_step_up_case_waits_for_the_window_and_checks_both_routes(quiet, monkeypatch):
    slept = []
    monkeypatch.setattr(tm.time, "sleep", slept.append)
    ctx = SimpleNamespace(stale=FakeAdmin(code="step_up_required"), stale_login_at=tm.time.monotonic() - 100.0)
    ctx.stale.step_up = lambda: setattr(ctx.stale, "status", 200) or setattr(ctx.stale, "body", {"rows": []})
    tm.test_obspkt1_f_admin_without_a_fresh_step_up_gets_403_step_up_required(ctx, None)
    assert len(slept) == 1 and 210 < slept[0] <= 216
    ctx = SimpleNamespace(stale=FakeAdmin(code="forbidden"), stale_login_at=0.0)
    with pytest.raises(AssertionError, match="snapshot without a window"):
        tm.test_obspkt1_f_admin_without_a_fresh_step_up_gets_403_step_up_required(ctx, None)


def test_the_leak_case_scans_logs_public_state_admin_routes_and_metrics(quiet, tmp_path):
    t = traffic()
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "xlxd-sut.log").write_text("clean line\n")
    ctx = SimpleNamespace(target=SimpleNamespace(out=str(tmp_path)), admin=FakeAdmin(status=200, body={"items": []}))
    web_ctx = SimpleNamespace(anon=SimpleNamespace(get=lambda path, expect=None: FakeResp(200, {"rooms": []})))
    hub = SimpleNamespace(text=lambda: 'dv_pkt_items_total{kind="text"} 3')
    tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, t, web_ctx, hub, None)
    (logs / "xlxd-sut.log").write_text("debug: rx N0OBA-1>APDVT1:>%s\n" % MK["S"])
    with pytest.raises(AssertionError, match=r"log:xlxd-sut.log"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, t, web_ctx, hub, None)
    (logs / "xlxd-sut.log").write_text("clean\n")
    with pytest.raises(AssertionError, match="hub metrics"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(
            ctx, t, web_ctx, SimpleNamespace(text=lambda: 'dv_x{route="%s"} 1' % MK["N"]), None)
    with pytest.raises(AssertionError, match="the control"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, traffic([]), web_ctx, hub, None)
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(pytest.skip.Exception, match="no SUT log"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(
            SimpleNamespace(target=SimpleNamespace(out=str(empty)), admin=ctx.admin), t, web_ctx, hub, None)


def test_the_fixture_skips_without_the_shape_the_hidden_base_or_the_routes(tmp_path, monkeypatch):
    target = SimpleNamespace(out=str(tmp_path))
    monkeypatch.setattr(tm.p1, "read_config", lambda t: {})
    with pytest.raises(pytest.skip.Exception, match="shape K of DP11e"):
        tm.ctx.__wrapped__(None, target, None, None)
    import tomllib
    from dvtest import sut_config
    root = Path(__file__).resolve().parents[2] / "rig" / "config" / "dv-router"
    base = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'
    names = ["dp11e-kiss.toml", "dp11e-kiss-igate.toml", "dp15-aprsis.toml", "obspkt1-hidden.toml"]
    doc = tomllib.loads(sut_config.render(base, "172.30.0", extras=[(root / n).read_text() for n in names]))
    monkeypatch.setattr(tm.p1, "read_config", lambda t: doc)
    admin = FakeAdmin(status=404)
    factory = lambda role: admin  # noqa: E731
    factory.credentials = lambda role: ("u", "p")
    with pytest.raises(pytest.skip.Exception, match="no packet monitor"):
        tm.ctx.__wrapped__(None, target, None, factory)
    no_hidden = tomllib.loads(sut_config.render(base, "172.30.0", extras=[(root / n).read_text() for n in names[:3]]))
    monkeypatch.setattr(tm.p1, "read_config", lambda t: no_hidden)
    with pytest.raises(pytest.skip.Exception, match="does not hide N0HID"):
        tm.ctx.__wrapped__(None, target, None, factory)
