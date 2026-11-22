# SPDX-License-Identifier: MIT
"""dvtest/ax25ack1.py: the pure helpers of the AX25ACK1 / TXOFF2 cases."""
import pytest

from dvtest import ax25ack1 as a

pytestmark = pytest.mark.unit

ACK = "I am the control operator of N0TEST-1 and accept responsibility for its transmissions"


def test_sentence_and_body():
    assert a.ack_sentence("n0test-1") == ACK
    assert a.radio_tx_body(False) == {"kind": "ax25", "on": False}
    assert a.radio_tx_body(True, port=1, ack=ACK) == {"kind": "ax25", "on": True, "port": 1, "ack": ACK}
    with pytest.raises(ValueError):
        a.radio_tx_body(True, port=16)
    with pytest.raises(ValueError):
        a.radio_tx_body(True, kind="web")


STATUS = {"procs": [{"kind": "edge", "proc": "xlx"}, {"kind": "ax25", "proc": "ax25", "ports": [{"name": "rf-a", "tx": "off"}, {"name": "rf-b", "tx": "on"}],
                                                       "ax25": {"callsign": "N0TEST-1", "tx_enable": True, "acknowledged": False}}]}


def test_status_readers():
    assert a.ax25_info(STATUS)["acknowledged"] is False
    assert a.port_tx(STATUS, "rf-a") == "off" and a.port_tx(STATUS, "rf-b") == "on" and a.port_tx(STATUS, "zz") is None
    assert a.ax25_info({"procs": []}) is None and a.ax25_info({}) is None


def test_config_ack_problems():
    good = {"edges": {"ax25": {"callsign": "N0TEST-1", "tx_enable": True, "tx_ack": ACK}}}
    assert a.config_ack_problems(good) == []
    assert "dormant" in a.config_ack_problems({"edges": {"ax25": {"callsign": "N0TEST-1", "tx_enable": True}}})[0]
    assert a.config_ack_problems({"edges": {"ax25": {"callsign": "N0TEST-1", "tx_enable": False}}}) == []
    assert "tx_ack is" in a.config_ack_problems({"edges": {"ax25": {"callsign": "N0TEST-1", "tx_ack": ACK.replace("N0TEST-1", "N0TEST-2")}}})[0]
    assert a.config_ack_problems({}) == ["no [edges.ax25] table"]


def test_diagnostics_shapes():
    body = {"errors": [{"path": "edges.ax25.tx_ack", "msg": "RF TX acknowledgement must equal: x"}], "warnings": ["w1"], "diagnostics": [{"message": "m"}]}
    got = a.diagnostics(body)
    assert ("errors", "edges.ax25.tx_ack: RF TX acknowledgement must equal: x") in got and ("warnings", "w1") in got and ("diagnostics", ": m") in got
    assert a.diagnostics(None) == [] and a.diagnostics({"errors": None}) == []


LOG = """dv-edge-ax25: port rf-a RF TX off (no control-operator acknowledgement: RX only); callsign N0TEST-1; physical radio1; band 2m
dv-edge-ax25: port rf-b RF TX off (port off); callsign N0TEST-1; physical -; band -
dv-edge-ax25: port rf-a RF TX enabled; callsign N0TEST-1; physical radio1; band 2m; budget None; override_cap false
dv-edge-ax25: port rf-c RF TX simulate; callsign N0TEST-1
"""


def test_start_lines_keep_the_last_per_port():
    got = a.start_lines(LOG)
    assert got["rf-a"] == ("enabled", None) and got["rf-b"] == ("off", "port off") and got["rf-c"] == ("simulate", None)
    assert a.start_line_problems(LOG, "rf-b", "off", "port off") == []
    assert a.start_line_problems(LOG, "rf-a", "off") != []
    assert a.start_line_problems(LOG, "rf-z", "off") == ["no `RF TX` start line for port rf-z in the log"]
    first = LOG.splitlines()[0]
    assert a.start_lines(first)["rf-a"] == ("off", "no control-operator acknowledgement: RX only")


def test_rebase_reply():
    assert a.rebase_problems(200, {"rebased": True, "history_seq": 4}) == []
    assert len(a.rebase_problems(200, {"rebased": False})) == 2
    assert "HTTP 422" in a.rebase_problems(422, {})[0]
