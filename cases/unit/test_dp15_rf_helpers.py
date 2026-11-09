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


def test_need_tx_waits_for_the_tx_guard_only_when_transmit_is_on():
    calls = []
    on = SimpleNamespace(tx_on=True, has_out_path=True, tx_guard=calls.append)
    tr.need_tx(on, "6c")
    assert calls == ["DP15-6c"]
    off = SimpleNamespace(tx_on=False, has_out_path=True, tx_guard=calls.append)
    with pytest.raises(pytest.skip.Exception):
        tr.need_tx(off, "6c")
    assert calls == ["DP15-6c"]


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
    request = SimpleNamespace(getfixturevalue=lambda name: (_ for _ in ()).throw(pytest.skip.Exception("no admin session in this test")))
    gen = fixture_function(request, SimpleNamespace(out=str(tmp_path)), object())
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


def test_radio_tx_ports_reads_only_enabled_radio_rows_with_tx():
    doc = {"edges": {"radio": {"enable": True, "tx_enable": True, "port": [{"name": "p1", "tx": True}, {"name": "p2", "tx": False}, {"name": "p3"}]}}}
    assert tr.radio_tx_ports(doc) == ["p1"]
    assert tr.radio_tx_ports({"edges": {"radio": {"enable": True, "tx_enable": False, "port": [{"name": "p1", "tx": True}]}}}) == []
    assert tr.radio_tx_ports({"edges": {"radio": {"enable": False, "port": [{"name": "p1", "tx": True}]}}}) == []
    assert tr.radio_tx_ports({}) == []


def test_6g_is_the_first_test_of_the_module_and_skips_without_an_effective_tx_port(tmp_path):
    import inspect
    names = [n for n, f in vars(tr).items() if n.startswith("test_") and inspect.isfunction(f)]
    assert names[0] == "test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds"
    with pytest.raises(pytest.skip.Exception, match="no startup fence to read"):
        tr.test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds(None, None, rendered(tmp_path, "false", False), None, None)


def fake_admin(uptime, fence):
    def get(path, expect=None):
        if path.endswith("/status"):
            return SimpleNamespace(status=200, json=lambda: {"live": {"node_uptime_s": uptime}, "launcher": {"children": [
                {"name": "dv-edge-ax25", "state": "up", "restarts": 0}, {"name": "dv-room[B]", "state": "up", "restarts": 0}]}})
        modem = {"name": "rf-a", "adapter": "kiss-tcp", "connected": True}
        if fence:
            modem.update({"tx_fence": "startup_guard", "tx_guard_ms": 11000})
        return SimpleNamespace(status=200, json=lambda: {"items": [{"kind": "modem", "modem": modem}]})
    return SimpleNamespace(path=lambda s: s, get=get)


def test_6g_flow_fenced_at_startup_and_violation(tmp_path, monkeypatch):
    notes = []
    monkeypatch.setattr(tr, "note", lambda *a: notes.append(a))
    target = rendered(tmp_path, "true", True)
    tr.test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds(None, None, target, lambda role: fake_admin(3.0, True), None)
    assert any("fenced" in a[3] and "startup_guard" in a[3] for a in notes if a[2] == "RECORD")
    with pytest.raises(AssertionError, match="expected a startup_guard fence"):
        tr.test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds(None, None, target, lambda role: fake_admin(3.0, False), None)


class RearmAdmin:
    """Admin client double for the re-arm: the clients route reports a fence for `fenced_reads` reads after the restart, then none."""

    def __init__(self, fenced_reads=6, children=None, restart_status=200):
        self.fenced_reads, self.reads, self.restarted, self.posts, self.step_ups = fenced_reads, 0, False, [], 0
        self.children = [{"name": "dv-edge-ax25", "restarts": 0}, {"name": "dv-room[B]", "restarts": 0}] if children is None else children
        self.restart_status = restart_status

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def step_up(self):
        self.step_ups += 1

    def post(self, path, body, expect=None):
        self.posts.append(path)
        self.restarted = True
        return SimpleNamespace(status=self.restart_status, json=lambda: {"ok": True})

    def get(self, path, expect=None):
        if path.endswith("/status"):
            restarts = 1 if self.restarted else 0
            return SimpleNamespace(status=200, json=lambda: {"launcher": {"children": [dict(c, restarts=restarts) for c in self.children]}, "live": {"node_uptime_s": 50}})
        self.reads += 1
        modem = {"name": "rf-a", "adapter": "kiss-tcp"}
        if self.restarted and self.reads <= self.fenced_reads:
            modem.update({"tx_fence": "recovery_probe", "tx_guard_ms": 13000 - 1500 * self.reads})
        return SimpleNamespace(status=200, json=lambda: {"items": [{"kind": "modem", "modem": modem}]})


class FakeTnc:
    def __init__(self):
        self.connections = 1
        self.a = self
        self.b = SimpleNamespace(connections=1)

    def startup_params(self, conn=None):
        return [(0, c, 1) for c in range(1, 6)]

    def wait(self, pred, timeout):
        self.connections += 1
        return pred(self)


def fast_timing():
    state = {"t": 0.0}
    return {"clock": lambda: state["t"], "sleep": lambda s: state.__setitem__("t", state["t"] + s)}


def run_rearm(monkeypatch, tmp_path, admin_adm, admin_op=None):
    notes = []
    monkeypatch.setattr(tr, "note", lambda *a: notes.append(a))
    request = SimpleNamespace(getfixturevalue=lambda name: FakeTnc())
    sessions = {"operator": admin_op or admin_adm, "admin": admin_adm}
    tr.rearm_ax25(request, SimpleNamespace(out=str(tmp_path)), "DP15-6g", lambda role: sessions[role], "rf-a", timing=fast_timing())
    return notes


def test_6g_rearm_restarts_the_ax25_child_reads_the_fence_and_waits_until_unfenced(tmp_path, monkeypatch):
    admin = RearmAdmin()
    notes = run_rearm(monkeypatch, tmp_path, admin)
    assert admin.posts == ["/api/v1/admin/children/dv-edge-ax25/restart"] and admin.step_ups == 1
    text = " ".join(a[3] for a in notes)
    assert "restarting the launcher child 'dv-edge-ax25'" in text and "recovery_probe" in text and "reconnected" in text
    assert "restarts counter after: [1]" in text


def test_6g_rearm_skips_when_the_child_key_is_not_unique(tmp_path, monkeypatch):
    for children in ([], [{"name": "dv-edge-ax25"}, {"name": "dv-edge-ax25[b]"}]):
        admin = RearmAdmin(children=children)
        with pytest.raises(pytest.skip.Exception, match=r"launcher children contain 'ax25'"):
            run_rearm(monkeypatch, tmp_path, admin)
        assert admin.posts == []


def test_6g_rearm_fails_when_no_fence_appears_or_the_restart_is_refused(tmp_path, monkeypatch):
    with pytest.raises(AssertionError, match="no fence by any cause"):
        run_rearm(monkeypatch, tmp_path, RearmAdmin(fenced_reads=0))
    with pytest.raises(AssertionError, match="answered HTTP 403"):
        run_rearm(monkeypatch, tmp_path, RearmAdmin(restart_status=403))
    with pytest.raises(AssertionError, match="still fenced"):
        run_rearm(monkeypatch, tmp_path, RearmAdmin(fenced_reads=10 ** 6))


def test_6g_runs_the_rearm_only_after_a_late_startup_attempt(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "note", lambda *a: None)
    monkeypatch.setattr(tr, "rearm_ax25", lambda *a, **k: calls.append(a[3]))
    target = rendered(tmp_path, "true", True)
    tr.test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds(None, None, target, lambda role: fake_admin(3.0, True), None)
    assert calls == []                                                   # fenced inside the window: no re-arm
    tr.test_dp15_6g_the_startup_guard_is_a_fence_in_the_first_13_seconds(None, None, target, lambda role: fake_admin(40.0, False), None)
    assert calls == ["DP15-6g"] or len(calls) == 1                       # late: the re-arm path
