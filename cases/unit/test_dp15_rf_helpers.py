# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp15_rf.py (DP15 group 6, tier A): the rendered-config reader against the real snippets in both
run shapes, the frame builders, the budget numbers, the sliding window, and the case fixture against a loopback "edge" that
behaves like the AX.25 edge (connects to the fake TNC, writes the five parameter frames, transmits one group message).
Offline: loopback sockets only."""
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp15_rf as tr  # noqa: E402

from dvtest import ax25, kiss_tnc, sut_config  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered(tmp_path, tx, out):
    extras = [(SNIPPETS / "dp15-rf.toml").read_text()] + ([(SNIPPETS / "dp15-rf-out.toml").read_text()] if out else [])
    text = sut_config.render(BASE, "172.30.0", dp15_tx_enable=tx, extras=extras)
    path = tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return SimpleNamespace(out=str(tmp_path))


def test_read_rf_config_in_both_run_shapes(tmp_path):
    off = tr.read_rf_config(rendered(tmp_path / "off", "false", False))
    ax, paths = off
    assert ax["tx_enable"] is False and tr.port_row(ax, "rf-a")["tx"] is False and tr.port_row(ax, "rf-b")["port"] == 8102
    assert [p["name"] for p in paths] == ["rf-in"] and tr.port_row(ax, "nope") is None
    ax, paths = tr.read_rf_config(rendered(tmp_path / "on", "true", True))
    assert ax["tx_enable"] is True and tr.port_row(ax, "rf-a")["tx"] is True and tr.port_row(ax, "rf-b")["tx"] is False
    assert [p["name"] for p in paths] == ["rf-in", "rf-out"] and paths[1]["to"] == "edge:ax25/rf-a"


def test_read_rf_config_is_none_without_the_edge_or_the_file(tmp_path):
    assert tr.read_rf_config(SimpleNamespace(out=str(tmp_path))) is None
    path = tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    path.parent.mkdir(parents=True)
    path.write_text(BASE)
    assert tr.read_rf_config(SimpleNamespace(out=str(tmp_path))) is None
    path.write_text(BASE + "[edges.ax25]\nenable = false\n")
    assert tr.read_rf_config(SimpleNamespace(out=str(tmp_path))) is None
    path.write_text("not toml [[[")
    assert tr.read_rf_config(SimpleNamespace(out=str(tmp_path))) is None


def test_rf_frame_is_an_aprs_message_to_the_room_name():
    frame = tr.rf_frame("DVT 1234", msg_id="3")
    assert ax25.dvrouter_accepts_frame(frame)
    msg = kiss_tnc.decode_message(frame)
    assert (msg["src"], msg["dst"], msg["digis"]) == ("N0TEST-7", "APDVT1", ["WIDE1-1"])
    assert (msg["addressee"], msg["text"], msg["msg_id"]) == ("XLX999B", "DVT 1234", "3")
    assert kiss_tnc.decode_message(tr.rf_frame("x", digis=()))["digis"] == []
    # the has-been-repeated flag of a digipeater survives the builder
    parsed = ax25.parse_frame(tr.rf_frame("x", digis=("WIDE1-1*", "WIDE2-1")))
    assert [d["bit7"] for d in parsed["digis"]] == [True, False]


def test_budget_numbers_default_and_override():
    assert tr.budget_numbers(SimpleNamespace(budget={})) == (0.05, 600.0, 1.0)
    assert tr.budget_numbers(SimpleNamespace(budget={"duty_pct": 2, "window_secs": 120, "min_gap_ms": 2500})) == (0.02, 120.0, 2.5)


def test_sliding_max_counts_events_in_a_half_open_window():
    assert tr.sliding_max([], 60.0) == 0
    assert tr.sliding_max([0.0], 60.0) == 1
    assert tr.sliding_max([0.0, 30.0, 60.0], 60.0) == 2          # [0, 60) holds 0 and 30; [30, 90) holds 30 and 60
    assert tr.sliding_max([0.0, 1.0, 2.0, 100.0], 60.0) == 3
    assert tr.sliding_max([5.0, 0.0, 29.9], 60.0) == 3


def test_need_tx_and_need_rx_only_skip_in_the_wrong_shape():
    on = SimpleNamespace(tx_on=True, has_out_path=True)
    off = SimpleNamespace(tx_on=False, has_out_path=False)
    tr.need_tx(on)
    tr.need_rx_only(off)
    with pytest.raises(pytest.skip.Exception):
        tr.need_tx(off)
    with pytest.raises(pytest.skip.Exception):
        tr.need_rx_only(on)
    with pytest.raises(pytest.skip.Exception):
        tr.need_rx_only(SimpleNamespace(tx_on=True, has_out_path=False))


def test_wait_tx_spacing_waits_for_the_last_frame_only(monkeypatch):
    slept = []
    monkeypatch.setattr(tr.time, "sleep", slept.append)
    now = time.monotonic()
    rf = SimpleNamespace(a=SimpleNamespace(data_frames=lambda: [(now - 10.0, 0, b"x")]), b=SimpleNamespace(data_frames=lambda: []))
    tr.wait_tx_spacing(rf)
    assert len(slept) == 1 and 20.0 <= slept[0] <= 21.1
    slept.clear()
    old = SimpleNamespace(a=SimpleNamespace(data_frames=lambda: [(now - 100.0, 0, b"x")]), b=SimpleNamespace(data_frames=lambda: []))
    tr.wait_tx_spacing(old)
    tr.wait_tx_spacing(SimpleNamespace(a=SimpleNamespace(data_frames=lambda: []), b=SimpleNamespace(data_frames=lambda: [])))
    assert slept == []


def test_the_fixture_against_a_loopback_edge(tmp_path, monkeypatch):
    """The rf fixture binds the ports of the rendered config; here two free ports are written into a config and a loopback
    "edge" connects, writes the parameters and transmits a group message: the case helpers see it."""
    ports = []
    for _ in range(2):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        s.close()
    cfg = (tmp_path / "xlxd-config" / "xlxd-sut")
    cfg.mkdir(parents=True)
    (cfg / "dv-router.toml").write_text(
        '[edges.ax25]\nenable = true\ncallsign = "N0TEST-1"\ntx_enable = true\n'
        '[[edges.ax25.port]]\nname = "rf-a"\nkind = "kiss-tcp"\nhost = "127.0.0.1"\nport = %d\ntx = true\ntx_budget = {}\ntx_per_min = 2\n'
        '[[edges.ax25.port]]\nname = "rf-b"\nkind = "kiss-tcp"\nhost = "127.0.0.1"\nport = %d\ntx = false\n'
        '[[data.path]]\nname = "rf-out"\nfrom = "room:B"\nto = "edge:ax25/rf-a"\n' % tuple(ports))
    notes = []
    monkeypatch.setattr(tr, "note", lambda *a: notes.append(a))
    fixture_function = getattr(tr.rf, "__wrapped__", None) or tr.rf._get_wrapped_function()
    gen = fixture_function(SimpleNamespace(out=str(tmp_path)), object())
    edges = []
    try:
        # the fixture waits for both connections: connect the loopback edges from a thread
        import threading

        def connect():
            time.sleep(0.5)
            for port in ports:
                edges.append(_edge(port))

        thread = threading.Thread(target=connect)
        thread.start()
        ctx = next(gen)
        thread.join()
        assert ctx.tx_on is True and ctx.has_out_path is True and ctx.tx_per_min == 2 and ctx.budget == {}
        assert ctx.a.wait_params(timeout=5)[0][1] == 1 and ctx.b.wait_params(timeout=5)[0][1] == 1
        t0 = time.monotonic()
        out = kiss_tnc.ui_frame("N0TEST-1", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", "hello"))
        edges[0].sendall(ax25.kiss_encode(0, 0, out))
        assert ctx.a.wait_data(1, timeout=5, since=t0) and ctx.b.data_frames(t0) == []
        assert tr.last_data_frame(ctx) >= t0
        assert ctx.a.inject(tr.rf_frame("hi"))
    finally:
        for e in edges:
            e.close()
        try:
            next(gen)
        except StopIteration:
            pass


def _edge(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(ax25.dvrouter_startup_params(0))
    return sock
