# SPDX-License-Identifier: MIT
"""dvtest/dp11e.py, the DP11e snippets and the assertion machinery of cases/test_dp11e_kiss.py / test_dp11e_config.py against fakes (no rig, no
docker). The expected labels follow data.md 10.10 (DV-Router cfa765a)."""
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp11e_kiss as tk  # noqa: E402

from dvtest import ax25, dp11e as dp, kiss_tnc, metrics, sut_config  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered(*names, text=None):
    extras = [(SNIPPETS / n).read_text() for n in names] if text is None else [text]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", extras=extras))


# ---------------------------------------------------------------- the snippets

def test_shape_k_has_four_listeners_with_the_documented_roles():
    shp = dp.kiss_shape(rendered("dp11e-kiss.toml"))
    assert shp.station == "N0TEST-1" and shp.ports == {"rf-a": True, "rf-b": False}
    ls = shp.listeners
    assert list(ls) == ["apps", "rxonly", "txdeny", "outside"]
    assert ls["apps"].channels == {0: "rf-a", 1: "rf-b"} and ls["apps"].keepalive_secs == 10 and ls["apps"].tx and ls["apps"].max_clients == 3
    assert ls["apps"].allow_src == ["N0TEST-7", "N0TEST-8"] and ls["apps"].host == "172.30.0.11" and ls["apps"].port == 8201
    assert not ls["rxonly"].tx and ls["rxonly"].keepalive_secs == 0
    assert ls["txdeny"].tx and not dp.peer_in(ls["txdeny"].tx_from, "172.30.0.30") and dp.peer_in(ls["apps"].tx_from, "172.30.0.30")
    assert dp.peer_in(ls["apps"].allow_from, "172.30.0.30") and not dp.peer_in(ls["outside"].allow_from, "172.30.0.30")
    assert all(not l.loopback for l in ls.values()) and len({l.port for l in ls.values()}) == 4


def test_shape_b_has_the_loopback_rate_and_simulate_listeners():
    shp = dp.kiss_shape(rendered("dp11e-kiss-b.toml"))
    assert shp.ports == {"rf-a": True, "rf-b": "simulate"}
    ls = shp.listeners
    assert list(ls) == ["loop", "rate", "sim"] and ls["loop"].loopback and ls["loop"].host == "127.0.0.1" and ls["loop"].allow_from == []
    assert (ls["rate"].tx_per_min, ls["rate"].tx_burst) == (2, 1) and ls["sim"].channels == {0: "rf-a", 1: "rf-b"}
    assert dp.kiss_shape({}) is None and dp.kiss_shape({"edges": {"ax25": {"enable": True}}}) is None


def test_the_station_base_matches_every_allowed_source_and_the_igate_snippet_is_a_transit_path():
    for name in ("dp11e-kiss.toml", "dp11e-kiss-b.toml"):
        shp = dp.kiss_shape(rendered(name))
        base = shp.station.split("-")[0]
        for l in shp.listeners.values():
            assert all(s.split("-")[0] == base for s in l.allow_src), (name, l.name)
    doc = rendered("dp11e-kiss.toml", "dp11e-kiss-igate.toml")
    path = doc["data"]["path"][0]
    assert path["from"] == "edge:ax25/rf-a" and path["to"] == "edge:aprsis" and path["types"] == ["transit"]


# ---------------------------------------------------------------- the refusal table

def test_the_refusal_table_has_one_unique_frame_per_id_and_every_documented_label():
    table = dp.refusal_frames()
    assert len({r.id for r in table}) == len(table) == 20
    assert {r.label for r in table} == {"queued", "tx_off", "tx_addr", "channel", "bad_frame", "not_ui", "src", "path", "too_long", "third_party", "command"}
    assert dp.expected_in_deltas(table) == {
        "apps": {"queued": 1, "tx_off": 1, "channel": 2, "bad_frame": 1, "not_ui": 2, "src": 2, "path": 4, "too_long": 2, "third_party": 1, "command": 2},
        "rxonly": {"tx_off": 1}, "txdeny": {"tx_addr": 1}}
    infos = {}
    for r in table:
        if r.kind == "frame" and r.id != "bad_frame":
            infos[r.id] = ax25.parse_frame(r.payload)["info"].decode("latin-1")
    assert len(set(infos.values())) == len(infos)                                      # every frame is told apart at the modem
    assert len(infos["too_long_257"]) == 257 and infos["too_long_empty"] == "" and infos["third_party"].startswith("}")


def test_each_refusal_frame_breaks_exactly_the_rule_it_names():
    by_id = {r.id: r for r in dp.refusal_frames()}

    def parsed(rid):
        return ax25.parse_frame(by_id[rid].payload)
    good = parsed("queued")
    assert good["src"]["text"] == "N0TEST-7" and good["dest"]["text"] == "APDR16" and [d["text"] for d in good["digis"]] == ["WIDE1-1"]
    assert ax25.to_tnc2(by_id["queued"].payload).startswith("N0TEST-7>APDR16,WIDE1-1:=")
    assert parsed("not_ui_control")["control"] == 0x2F and parsed("not_ui_pid")["pid"] == 0xCC
    assert parsed("src_unlisted")["src"]["text"] == "N0TEST-9" and parsed("src_other_base")["src"]["text"] == "N0CALL-7"
    assert [d["text"] for d in parsed("path_tcpip")["digis"]] == ["TCPIP"] and [d["text"] for d in parsed("path_qar")["digis"]][0] == "QAR"
    assert [d["text"] for d in parsed("path_hops")["digis"]] == ["WIDE2-2", "WIDE3-1"]
    assert by_id["channel_3"].channel == 3 and by_id["tx_off_port"].channel == 1 and by_id["command_return"].payload == (15, b"")
    with pytest.raises(ValueError):
        ax25.parse_frame(by_id["bad_frame"].payload)


# ---------------------------------------------------------------- counters and the keepalive

def test_counter_readers_and_delta():
    text = "\n".join([
        'dv_edge_kiss_frames_total{port="apps",dir="in",result="queued"} 3', 'dv_edge_kiss_frames_total{port="apps",dir="out",result="sent"} 7',
        'dv_edge_kiss_frames_total{port="rxonly",dir="in",result="tx_off"} 1', 'dv_edge_kiss_clients_total{port="apps",result="accepted"} 4',
        'dv_edge_kiss_clients_total{port="apps",result="full"} 1', 'dv_edge_kiss_clients{port="apps"} 3'])
    s = metrics.parse(text)
    assert dp.frame_counts(s, "apps") == {("in", "queued"): 3, ("out", "sent"): 7} and dp.client_counts(s, "apps") == {"accepted": 4, "full": 1}
    assert dp.clients_gauge(s, "apps") == 3 and dp.clients_gauge(s, "none") is None
    assert dp.delta({"a": 5, "b": 2}, {"a": 3, "b": 2, "c": 1}) == {"a": 2, "c": -1}


def test_keepalive_problems():
    period = 10
    good = [10.2, 20.1, 30.3, 40.0, 50.2]
    assert dp.keepalive_problems(good, period, 0.0, 55.0) == []
    assert "at least" in dp.keepalive_problems([10.0], period, 0.0, 15.0)[0]
    assert any("outside" in p for p in dp.keepalive_problems([10.0, 30.0, 40.0], period, 0.0, 45.0))
    assert any("first lone FEND" in p for p in dp.keepalive_problems([25.0, 35.0, 45.0], period, 0.0, 50.0))
    assert dp.keepalive_problems([], period, 0.0, 130.0, min_count=2)


# ---------------------------------------------------------------- the configuration refusals

def test_every_config_mutation_applies_renders_and_changes_the_snippet():
    text = (SNIPPETS / "dp11e-kiss.toml").read_text()
    for case in dp.CONFIG_CASES:
        mutated = case.mutate(text)
        doc = rendered(text=mutated)                                              # still TOML after the token substitution
        assert (mutated != text) == (case.id != "accept-baseline"), case.id
        assert "kiss_server" in str(doc["edges"]["ax25"]), case.id
    assert len({c.id for c in dp.CONFIG_CASES}) == len(dp.CONFIG_CASES) == 17
    assert len(dp.kiss_shape(rendered(text=dict((c.id, c) for c in dp.CONFIG_CASES)["fifth-listener"].mutate(text))).listeners) == 5


def verdict(valid, parsed=()):
    return {"valid": valid, "unsupported": False, "errors": [] if valid else ["x"], "parsed": list(parsed)}


def test_config_problems_judge_reject_and_accept():
    case = {c.id: c for c in dp.CONFIG_CASES}
    err = {"file": "dv-router.toml", "line": 9, "col": 1, "key": "edges.ax25.kiss_server[0].listen", "message": "required"}
    assert dp.config_problems(case["listen-required"], verdict(False, [err]), 2) == []
    assert "SILENT SUCCESS" in dp.config_problems(case["listen-required"], verdict(True), 0)[0]
    assert dp.config_problems(case["listen-required"], verdict(False, []), 2)[0].startswith("no `FILE:LINE:COL")
    assert any("expected the field" in p for p in dp.config_problems(case["keepalive-301"], verdict(False, [err]), 2))
    assert any("does not name kiss_server" in p for p in dp.config_problems(case["listen-required"], verdict(False, [dict(err, key="edges.ax25.port[0].tx", message="m")]), 2))
    c0 = dict(err, key="edges.ax25.kiss_server[0].ports", message="KISS channel 0 must map the default port")
    assert dp.config_problems(case["channel-0-unmapped"], verdict(False, [c0]), 2) == []
    assert any("channel 0" in p for p in dp.config_problems(case["channel-0-unmapped"], verdict(False, [dict(c0, message="other")]), 2))
    assert dp.config_problems(case["accept-baseline"], verdict(True), 0) == []
    assert dp.config_problems(case["accept-loopback"], verdict(False, [err]), 2)[0].startswith("expected to load")
    assert dp.config_problems(case["accept-baseline"], {"unsupported": True}, 2)[0].startswith("the image has no")


# ---------------------------------------------------------------- the rig case machinery against fakes

def test_the_label_cases_check_each_listener_and_the_control_check_reads_the_modem():
    table = dp.refusal_frames()
    want = dp.expected_in_deltas(table)
    deltas = {n: {("in", lab): c for lab, c in labels.items()} for n, labels in want.items()}
    run = SimpleNamespace(table=table, want=want, deltas=deltas, at_modem=[(1.0, ax25.to_tnc2(next(r for r in table if r.id == "queued").payload))],
                          still_open={"apps": True, "rxonly": True, "txdeny": True})
    for label in tk.LABELS:
        tk.test_dp11e_b_each_refusal_label_is_counted(label, run)
    tk.test_dp11e_b_only_the_control_reaches_the_modem_as_sent(run)
    run.deltas["apps"][("in", "path")] = 3
    with pytest.raises(AssertionError, match="result=path"):
        tk.test_dp11e_b_each_refusal_label_is_counted("path", run)
    run.at_modem.append((2.0, "N0TEST-7>APDR16,WIDE1-1:=1030.00N/02015.00E>dp11e-src_unlisted"))
    with pytest.raises(AssertionError, match="expected exactly the control"):
        tk.test_dp11e_b_only_the_control_reaches_the_modem_as_sent(run)
    run.at_modem.pop()
    run.still_open["apps"] = False
    with pytest.raises(AssertionError, match="closed a client"):
        tk.test_dp11e_b_only_the_control_reaches_the_modem_as_sent(run)


def test_disabled_never_appears_and_the_fixture_skips_without_the_shape(tmp_path, monkeypatch):
    class Hub:
        def __init__(self, text):
            self._t = text

        def text(self):
            return self._t
    kiss = SimpleNamespace(hub=Hub('dv_edge_rf_tx_total{port="rf-a",kind="position",result="sent"} 3'))
    monkeypatch.setattr(tk.ds, "write_note", lambda *a, **k: None)
    tk.test_dp11e_z_no_admission_is_counted_disabled(None, SimpleNamespace(out=str(tmp_path)), kiss)
    kiss = SimpleNamespace(hub=Hub('dv_edge_rf_tx_total{port="rf-a",kind="position",result="disabled"} 1'))
    with pytest.raises(AssertionError, match="disabled"):
        tk.test_dp11e_z_no_admission_is_counted_disabled(None, SimpleNamespace(out=str(tmp_path)), kiss)
    monkeypatch.setattr(tk.p1, "read_config", lambda target: {})
    with pytest.raises(pytest.skip.Exception, match="no \\[\\[edges.ax25.kiss_server\\]\\]"):
        tk.kiss.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, None)
    doc = rendered("dp11e-kiss.toml")
    monkeypatch.setattr(tk.p1, "read_config", lambda target: doc)
    old = SimpleNamespace(path=lambda s: s, get=lambda p, expect=None: SimpleNamespace(status=200, text=lambda: '{"properties": {"enable": {}}}'))
    with pytest.raises(pytest.skip.Exception, match="does not know DP11e"):
        tk.kiss.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, lambda role: old)


def test_listener_helper_skips_a_missing_listener_and_modem_frames_decode():
    kiss = SimpleNamespace(shape=dp.kiss_shape(rendered("dp11e-kiss-b.toml")))
    with pytest.raises(pytest.skip.Exception, match="no listener 'apps'"):
        tk.listener(kiss, "apps")
    assert tk.listener(kiss, "rate").port == 8206
    frame = kiss_tnc.ui_frame("N0TEST-7", "APDR16", "=x", digis=("WIDE1-1",))
    tnc = SimpleNamespace(a=SimpleNamespace(data_frames=lambda since=0.0: [(5.0, 0, frame)]), b=SimpleNamespace(data_frames=lambda since=0.0: []))
    kiss = SimpleNamespace(tnc=tnc)
    assert tk.modem_frames(kiss, 0.0) == [(5.0, "N0TEST-7>APDR16,WIDE1-1:=x")] and tk.modem_frames(kiss, 0.0, "b") == []
