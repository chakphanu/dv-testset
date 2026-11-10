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
W, E_AX, E_IS = "pkt", "ax25-0000000000000007", "aprsis-0000000000000007"       # the worker id is the configured one (default pkt)


def row(pid, seq, port, direction, text, out=(("rf", "heard"),), key="line", **extra):
    r = {"id": pid, "boot": BOOT, "seq": seq, "t_ms": 1700000000000 + seq, "port": port, "dir": direction, key: text,
         "out": [{"stage": s, "result": res} for s, res in out]}
    r.update(extra)
    return r


MK = {k: "OBSPKT1X%s0A1B2C" % k for k in "SNHVMWAIRCPX"}


def synthetic():
    rows = [
        row(W, 20, "rf-a", "rx", "N0OBA-1>APDVT1,WIDE1-1*:>" + MK["S"], (("rf", "heard"), ("route", "forwarded")), ref=5),
        row(E_AX, 9, "rf-a", "rx", "N0OBA-1>APDVT1,WIDE1-1*", (("modem", "rx"), ("raw", "sent")), key="header", ref=5),
        row(W, 21, "rf-a", "rx", "N0OBB-1>APDVT1,WIDE1-1*,NOGATE:>" + MK["N"], (("route", "no_gate"),), ref=6),
        row(E_AX, 10, "rf-a", "rx", "N0OBB-1>APDVT1,WIDE1-1*,NOGATE", (("modem", "rx"),), key="header", ref=6),
        row(W, 22, "rf-a", "rx", "N0OBC-1>APDVT1,WIDE1-1*:?APRS?", (("route", "filtered"),), ref=7),
        row(E_AX, 11, "rf-a", "rx", "N0OBC-1>APDVT1,WIDE1-1*", (("modem", "rx"),), key="header", ref=7),
        row(E_IS, 3, "is", "tx", "N0OBA-1>APDVT1,WIDE1-1*,qAO,N0TEST-1", (("render", "received"),), key="header", ref=40),
        row(W, 23, "rf-a", "rx", "N0HID-7>APDVT1,WIDE1-1*:", (("rf", "heard"),), ref=8, redacted="position_policy"),
        row(W, 24, "rf-a", "rx", "N0OBD-1>APDVT1,WIDE1-1*:=1030.42N/02015.17E>" + MK["V"], ref=9),
        row(W, 25, "rf-a", "rx", "N0HID-9>(mic-e),WIDE1-1*:", ref=10, redacted="position_policy"),
        row(E_AX, 12, "rf-a", "rx", "N0HID-9>(mic-e),WIDE1-1*", (("modem", "rx"),), key="header", ref=10),
        row(E_AX, 13, "rf-a", "rx", "N0OBE-1>(mic-e),WIDE1-1*", (("modem", "rx"),), key="header", ref=11),
        row(W, 26, "rf-a", "rx", "N0OBE-1>TQTQ30,WIDE1-1*:`abc" + MK["W"], ref=11),
        row(W, 27, "rf-a", "rx", "N0OBH-5>APDVT1,WIDE1-1*:=0912.34N/01945.67E>" + MK["A"], ref=12),
        row(W, 28, "is", "rx", "N0TEST-7>APDVT1,TCPIP*,qAC,SRV::XLX999B  :OBS " + MK["I"] + "{81", ref=2),
        row(E_IS, 4, "is", "rx", "N0TEST-7>APDVT1,TCPIP*,qAC,SRV", (("raw", "sent"),), key="header", ref=2),
        row(W, 29, "rf-a", "tx", "N0TEST-1>APZDVR::XLX999B  :OBS " + MK["R"], (("render", "sent"),), ref=3),
        row(E_AX, 14, "rf-a", "tx", "N0TEST-1>APZDVR", (), key="header", ref=3),       # the start row of a tx: header and ref, no outcome yet
        row(E_AX, 15, "rf-a", "tx", "N0TEST-7>APDR16,WIDE1-1::N0OBC    :" + MK["C"], (("client", "queued"),)),
        row(E_AX, 16, "rf-a", "tx", "N0TEST-7>APDR16,WIDE1-1:", (("client", "queued"),), redacted="position_policy"),
        {"id": E_AX, "boot": BOOT, "seq": 17, "t_ms": 1700000000017, "port": "rf-a", "dir": "tx", "out": [{"stage": "client", "result": "src"}]},   # refused: body-free
        # the later rows: the KISS client delivery of the status rx row (seq 9), the scheduler / modem outcomes of the rf tx start row (seq 14) and of the
        # client message (seq 15, no ref)
        {"id": E_AX, "boot": BOOT, "seq": 18, "t_ms": 1700000000018, "port": "rf-a", "dir": "rx", "ref": 5, "of": 9, "out": [{"stage": "client", "result": "sent"}]},
        {"id": E_AX, "boot": BOOT, "seq": 19, "t_ms": 1700000000019, "port": "rf-a", "dir": "tx", "ref": 3, "of": 14, "out": [{"stage": "sched", "result": "sent"}]},
        {"id": E_AX, "boot": BOOT, "seq": 20, "t_ms": 1700000000020, "port": "rf-a", "dir": "tx", "ref": 3, "of": 14, "out": [{"stage": "modem", "result": "tx"}]},
        {"id": E_AX, "boot": BOOT, "seq": 21, "t_ms": 1700000000021, "port": "rf-a", "dir": "tx", "of": 15, "out": [{"stage": "sched", "result": "sent"}]},
        {"id": E_IS, "boot": BOOT, "seq": 5, "t_ms": 1700000000005, "port": "is", "dir": "tx", "ref": 40, "of": 3, "out": [{"stage": "write", "result": "framed"}]},
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


def test_ids_headers_query_rules_and_the_row_fields_follow_the_confirmed_shapes():
    assert [ob.producer_kind(i) for i in ("pkt", "pkt2", "ax25-0000000000000007", "aprsis-00000000000000ab", "worker-1", "ax25", "ax25-7")] == [
        "worker", "worker", "edge", "edge", "worker", "worker", "worker"]
    assert ob.edge_name("ax25-0000000000000007") == "ax25" and ob.edge_name("aprsis-00000000000000ab") == "aprsis" and ob.edge_name("pkt") is None
    assert ob.header_problems("N0OBA-1>APDVT1,WIDE1-1*") == [] and ob.header_problems("N0OBA-1>APDVT1:x") and ob.header_problems("nothing")
    assert ob.line_header("N0OBA-1>APDVT1,WIDE1-1*:>text:more") == "N0OBA-1>APDVT1,WIDE1-1*"
    assert [ob.query_ok("port", v) for v in ("rf-a", "is", "a" * 64, "a" * 65, "a/b", "a b", "")] == [True, True, True, False, False, False, False]
    assert [ob.query_ok("dir", v) for v in ("rx", "tx", "both", "", "up")] == [True, True, True, True, False]
    assert [ob.query_ok("limit", v) for v in ("0", "100", "5000", "-1", "abc", "")] == [True, True, True, False, False, False]
    assert [ob.clamped_limit(v) for v in ("0", "1", "100", "5000")] == [1, 1, 100, 1000]
    assert ob.ROUTE_RESULTS == ("forwarded", "seen", "hops", "rate", "filtered", "no_gate", "not_heard", "unreachable", "dedupe", "overflow", "unknown_origin")
    assert len(ob.WRITE_RESULTS) == 12 and "framed" in ob.WRITE_RESULTS and "sent" not in ob.WRITE_RESULTS
    assert any("route result" in p for p in ob.row_problems(dict(synthetic()[0], out=[{"stage": "route", "result": "heard"}])))
    assert any("write result" in p for p in ob.row_problems(dict(synthetic()[0], out=[{"stage": "write", "result": "sent"}])))
    assert ob.row_problems(synthetic()[-1]) == []                                  # the late write row: of, ref, no header, no line
    start = synthetic()[6]
    assert [r["seq"] for r in ob.late_rows(synthetic(), start)] == [5] and ob.late_rows(synthetic(), synthetic()[0]) == []
    assert ob.BYE_REASONS == ("session ended", "stream lifetime", "replaced") and "rate" in ob.CLIENT_REFUSALS and len(ob.CLIENT_REFUSALS) == 11
    good = synthetic()[0]
    assert ob.row_problems(good) == []
    assert any("row-level stage" in p for p in ob.row_problems(dict(good, stage="client")))
    assert any("ref is" in p for p in ob.row_problems(dict(good, ref="x"))) and any("contains a colon" in p for p in ob.row_problems(dict(synthetic()[1], header="a>b:c")))
    assert any("must end at the colon" in p for p in ob.row_problems(dict(good, redacted="position_policy")))
    assert ob.row_problems(synthetic()[7]) == [] and ob.row_problems(dict(synthetic()[18])) == []


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
              {"event": "resync", "data": json.dumps({"reason": "lag"})}, {"event": "odd", "data": "{}"}, {"event": "rows", "data": "{not json"}]
    ev = ob.parse_stream(frames)
    assert ev.hello == [{"id": W, "boot": BOOT}] and [r[2]["seq"] for r in ev.rows] == [1, 2] and ev.gaps == [E_AX]
    assert ev.byes == [{"reason": "limit"}] and ev.resync == "lag" and ev.other == ["odd"] and len(ev.bad) == 1 and ev.rows[0][:2] == (W, BOOT)
    assert ob.parse_stream([]).resync is None


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
    bad[12]["line"] = "N0OBE-1>(mic-e),WIDE1-1*:`abc" + MK["W"]                             # a visible station's worker line must show the real destination
    with pytest.raises(AssertionError, match="shows .mic-e. only when redacted"):
        tm.test_obspkt1_c_mic_e_destinations_show_the_placeholder(ctx, traffic(bad), None)
    notes = []
    bad = synthetic()
    bad[-1]["out"] = [{"stage": "write", "result": "sent"}]                                # outside the fixed write set
    with pytest.raises(AssertionError, match="outside"):
        tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic(bad), None)
    bad = [r for r in synthetic() if not (r.get("of") == 9)]                                # the late client / sent row is missing
    with pytest.raises(AssertionError, match="no later row with a client / sent outcome"):
        tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic(bad), None)
    bad = [r for r in synthetic() if not (r.get("of") == 14)]                               # the late sched / modem rows are missing
    with pytest.raises(AssertionError, match="no txcore / sched outcome in a later row"):
        tm.test_obspkt1_b_aprs_is_rx_and_rf_tx_rows(ctx, traffic(bad), None)
    bad = [r for r in synthetic() if not (r.get("out") and r["out"][0].get("result") == "src")]       # no refusal row
    with pytest.raises(AssertionError, match="no client tx row with the refusal"):
        tm.test_obspkt1_b_a_local_kiss_client_row_keeps_its_line_but_not_a_position(ctx, traffic(bad), None)
    bad = synthetic()
    bad[20]["line"] = "N0TEST-9>APDR16,WIDE1-1::N0OBC    :" + MK["X"]                       # a refusal row that carries the payload
    with pytest.raises(AssertionError, match="carries a line"):
        tm.test_obspkt1_b_a_local_kiss_client_row_keeps_its_line_but_not_a_position(ctx, traffic(bad), None)
    bad = synthetic()
    bad.pop()                                                                              # the late write row is missing
    with pytest.raises(AssertionError, match="no later row with of=3 and ref=40"):
        tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic(bad), None)
    bad = synthetic()
    bad[-1]["out"] = [{"stage": "write", "result": "expired"}]                             # in the set but not the expected `framed`: a finding, not a failure
    quiet_notes = []
    tm.ds.write_note = lambda target, case, tag, text, **kw: quiet_notes.append((tag, text))
    tm.test_obspkt1_b_rf_rx_rows_carry_the_gate_decision_and_edge_rows_only_the_header(ctx, traffic(bad), None)
    assert any(tag == "FINDING" and "expired" in text for tag, text in quiet_notes)
    bad = synthetic()
    bad[11]["header"] = "N0OBE-1>TQTQ30,WIDE1-1*"                                         # the edge shows the real Mic-E destination
    with pytest.raises(AssertionError, match="edge rows of N0OBE-1"):
        tm.test_obspkt1_c_mic_e_destinations_show_the_placeholder(ctx, traffic(bad), None)
    bad = synthetic()
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

    def clone(self):
        """A logged-out client: no session is 401 unauthorized."""
        return FakeAdmin(status=401, code="unauthorized", sse_status=401)

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
    ctx = SimpleNamespace(admin=FakeAdmin(status=200, body={}), admin_session=lambda role: FakeAdmin())
    tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(ctx)
    with pytest.raises(AssertionError, match="viewer GET packets: HTTP 200"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin=ctx.admin, admin_session=lambda role: FakeAdmin(status=200, body={})))
    with pytest.raises(AssertionError, match="code 'step_up_required'"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin=ctx.admin, admin_session=lambda role: FakeAdmin(code="step_up_required")))
    with pytest.raises(AssertionError, match="packets/stream: HTTP 200"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin=ctx.admin, admin_session=lambda role: FakeAdmin(sse_status=200)))
    leaky = FakeAdmin(status=200, body={})                                         # a server that answers a logged-out client with 200
    leaky.clone = lambda: FakeAdmin(status=200, body={})
    with pytest.raises(AssertionError, match="no session, GET packets: HTTP 200"):
        tm.test_obspkt1_a_viewer_and_operator_get_403_on_both_routes(SimpleNamespace(admin=leaky, admin_session=lambda role: FakeAdmin()))


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
    web_ctx = SimpleNamespace(anon=lambda: SimpleNamespace(get=lambda path, expect=None: FakeResp(200, {"rooms": []})))      # anon is a factory, as in the fixture
    hub = SimpleNamespace(text=lambda: 'dv_pkt_items_total{kind="text"} 3')
    tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, t, web_ctx, hub, None)
    (logs / "xlxd-sut.log").write_text("debug: rx N0OBA-1>APDVT1:>%s\n" % MK["S"])
    with pytest.raises(AssertionError, match=r"packet body appears in the SUT log.*xlxd-sut.log"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, t, web_ctx, hub, None)
    (logs / "xlxd-sut.log").write_text("clean\n")
    with pytest.raises(AssertionError, match="hub metrics"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(
            ctx, t, web_ctx, SimpleNamespace(text=lambda: 'dv_x{route="%s"} 1' % MK["N"]), None)
    with pytest.raises(AssertionError, match="the control"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(ctx, traffic([]), web_ctx, hub, None)
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(pytest.skip.Exception, match="python3 -m dvtest.logscan"):
        tm.test_obspkt1_e_no_packet_body_in_logs_public_state_other_routes_or_telemetry(
            SimpleNamespace(target=SimpleNamespace(out=str(empty)), admin=ctx.admin), t, web_ctx, hub, None)
    import json
    needles = json.loads((empty / "logscan" / "OBSPKT1-e.json").read_text())["needles"]       # left for the host scan
    assert needles == [MK["S"], MK["N"], MK["C"]]


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


# ---------------------------------------------------------------- a server that follows the confirmed query rules, and the restart flow

def pk_body(rows, producers):
    return {"rows": rows, "ports": [{"id": E_AX, "port": "rf-a", "first_seq": 1, "last_seq": 9}], "producers": producers}


class PacketServer:
    """GET /packets with the confirmed rules: limit clamped to 1-1000 (default 100), a non-numeric or negative limit, a bad port and a bad dir are 400
    bad_request; /packets/stream gives hello first. `leak` makes it accept what it must refuse."""

    def __init__(self, many=1500, leak=False):
        self.rows = [row(W, n, "rf-a", "rx", "N0OBA-1>APDVT1:>x%d" % n) for n in range(many, 0, -1)]
        self.leak, self.step_ups, self.authenticator = leak, 0, None

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def step_up(self):
        self.step_ups += 1

    def get(self, path, expect=None):
        _, _, qs = path.partition("?")
        query = dict(p.partition("=")[::2] for p in qs.split("&") if p)
        for name, value in query.items():
            if not ob.query_ok(name, value) and not self.leak:
                return FakeResp(400, {"code": "bad_request"})
        limit = ob.clamped_limit(query["limit"]) if "limit" in query else 100
        rows = [r for r in self.rows if query.get("dir") in (None, "", "both", r["dir"]) and query.get("port") in (None, r["port"])]
        return FakeResp(200, pk_body(rows[:limit if not self.leak else None], [{"id": W, "boot": BOOT}]), {"Cache-Control": "no-store"})

    def sse(self, path, max_events=1, timeout=1.0, **kw):
        return 200, "text/event-stream", [{"event": "hello", "retry": "3000", "data": json.dumps({"producers": [{"id": W, "boot": BOOT}]})}]


def test_the_snapshot_case_follows_the_confirmed_rules_and_catches_a_server_that_does_not(quiet):
    ctx = SimpleNamespace(admin=PacketServer(), target=None)
    tm.test_obspkt1_a_admin_with_a_step_up_gets_the_documented_snapshot_and_stream(ctx, None)
    with pytest.raises(AssertionError):
        tm.test_obspkt1_a_admin_with_a_step_up_gets_the_documented_snapshot_and_stream(SimpleNamespace(admin=PacketServer(leak=True), target=None), None)


class Scripted:
    """Admin double for the restart case: snapshots follow `script` (a list of (rows, producers)), then repeat the last; the stream replays `frames`."""

    def __init__(self, script, frames):
        self.script, self.frames, self.calls, self.step_ups, self.restarts = script, frames, 0, 0, 0
        self.authenticator = None

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def step_up(self):
        self.step_ups += 1

    def get(self, path, expect=None):
        rows, producers = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return FakeResp(200, pk_body(rows, producers))

    def post(self, path, body, expect=None):
        self.restarts += 1
        return SimpleNamespace(status=200)

    def sse(self, path, max_events=1, timeout=1.0, on_frame=None, stop=None, **kw):
        for frame in self.frames:
            on_frame(frame)
        stop.wait(2.0)
        return 200, "text/event-stream", []


def restart_flow(monkeypatch, new_ax="ax25-0000000000000009", gaps=True, keep_old_rows=False, worker_boot_changes=True, old_edge_gap=True):
    new_boot = [9] * 16
    worker0 = {"id": W, "boot": BOOT}
    edge0, edge_is = {"id": E_AX, "boot": BOOT}, {"id": E_IS, "boot": BOOT}
    t = traffic()
    rows0 = t.rows
    edge_rows = [r for r in rows0 if r["id"] in (E_AX, E_IS)]
    stage1 = [r for r in edge_rows]
    stage2 = [r for r in edge_rows if r["id"] == E_IS] + ([r for r in edge_rows if r["id"] == E_AX] if keep_old_rows else [])
    script = [(rows0, [worker0, edge0, edge_is]),
              (stage1, [{"id": W, "boot": new_boot if worker_boot_changes else BOOT}, edge0, edge_is]),
              (stage2, [{"id": W, "boot": new_boot if worker_boot_changes else BOOT}, {"id": new_ax, "boot": [7] * 16}, edge_is])]
    frames = [{"event": "hello", "data": json.dumps({"producers": [worker0, edge0, edge_is]})}]
    if gaps:
        frames += [{"event": "gap", "data": json.dumps({"id": W})}]
        if old_edge_gap:
            frames += [{"event": "gap", "data": json.dumps({"id": E_AX})}]
    frames += [{"event": "rows", "data": json.dumps({"id": new_ax, "boot": [7] * 16, "rows": [{"seq": 1}]})}]
    admin = Scripted(script, frames)
    ticks = iter(range(0, 10 ** 6))
    monkeypatch.setattr(tm, "time", SimpleNamespace(sleep=lambda s: None, monotonic=lambda: next(ticks)))
    monkeypatch.setattr(tm.ds, "write_note", lambda *a, **k: None)
    ctx = SimpleNamespace(admin=admin, admin_session=lambda role: admin)
    control = SimpleNamespace(kill_proc=lambda *a, **k: SimpleNamespace(pids=[1]))
    hub = SimpleNamespace(total=lambda family, **labels: 0)
    notes = []
    monkeypatch.setattr(tm.ds, "write_note", lambda target, case, tag, text, **kw: notes.append((tag, text)))
    tm.test_obspkt1_d_a_process_restart_clears_its_rings(ctx, t, control, hub, None)
    admin.notes = notes
    return admin


def test_the_restart_case_expects_the_worker_id_to_stay_and_the_edge_id_to_change(monkeypatch):
    admin = restart_flow(monkeypatch)
    assert admin.restarts == 1
    assert not [t for tag, t in admin.notes if tag == "FINDING"] and any("monitor eviction counters" in t for _tag, t in admin.notes)
    assert any("producer ids seen" in t for _tag, t in admin.notes) and any("rows around the gap" in t for _tag, t in admin.notes)
    with pytest.raises(AssertionError, match="no `gap` .id. for the restarted worker"):
        restart_flow(monkeypatch, gaps=False)                                            # the worker gap stays a hard assertion
    no_old = restart_flow(monkeypatch, old_edge_gap=False)                               # the old edge id's gap is a recorded FINDING (OBSPKT1b)
    assert [t for tag, t in no_old.notes if tag == "FINDING" and "OBSPKT1b" in t and "OLD AX.25 id" in t]
    with pytest.raises(AssertionError, match="rows of the old AX.25 id survived"):
        restart_flow(monkeypatch, keep_old_rows=True)
    with pytest.raises(AssertionError, match="worker boot did not change"):
        restart_flow(monkeypatch, worker_boot_changes=False)


def test_the_streamer_timeline_names_gaps_byes_and_row_batches():
    admin = SimpleNamespace(path=lambda s: s, sse=lambda *a, **k: (200, "text/event-stream", []))
    st = tm.Streamer(admin)
    for frame in ({"event": "hello", "data": json.dumps({"producers": []})}, {"event": "rows", "data": json.dumps({"id": "pkt", "boot": [1], "rows": []})},
                  {"event": "gap", "data": json.dumps({"id": E_AX})}, {"event": "bye", "data": json.dumps({"reason": "session ended"})}, {"event": "rows"}):
        st.on_frame(frame)
    assert st.gap_seen([E_AX]) and not st.gap_seen(["pkt"]) and st.ended() == [("bye", "session ended")]
    text = st.timeline()
    assert "gap %s" % E_AX in text and "bye session ended" in text and "'pkt': 1" in text and len(st.frames) == 5


def test_replay_after_gap_separates_a_new_boot_from_a_replay_of_old_sequences():
    def rows(boot, seqs):
        return {"event": "rows", "data": json.dumps({"id": W, "boot": boot, "rows": [{"seq": q} for q in seqs]})}
    gap = {"event": "gap", "data": json.dumps({"id": W})}
    fresh = ob.replay_after_gap([rows([1], [5, 6]), gap, rows([2], [1, 2])], W)
    assert fresh["gap"] and fresh["before"] == [((1,), 5, 6)] and fresh["after"] == [((2,), 1, 2)] and fresh["replayed"] == []
    replay = ob.replay_after_gap([rows([1], [5, 6]), gap, rows([1], [6, 7])], W)
    assert replay["replayed"] == [((1,), 6)]
    assert ob.replay_after_gap([rows([1], [5])], W)["gap"] is False and ob.replay_after_gap([], W)["after"] == []
    other = {"event": "rows", "data": json.dumps({"id": "ax25-1", "boot": [1], "rows": [{"seq": 5}]})}
    assert ob.replay_after_gap([other, {"event": "gap", "data": "not json"}], W) == {"gap": False, "before": [], "after": [], "replayed": []}
