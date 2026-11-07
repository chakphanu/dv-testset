# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp15_positions.py (DP7): the run-shape reader against the REAL snippets
(rig/config/dv-router/dp7-*.toml rendered with dvtest.sut_config), the frames and packets the senders build (decoded
back independently: AX.25 UI frame, M17P packet, the D-PRS line of the D-STAR over), the drop-counter watcher, the hide_position
helper and the key scan. Offline: no sockets."""
import contextlib
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position_support as ps  # noqa: E402
import test_dp15_positions as tp  # noqa: E402

from dvtest import ax25, dstar_sd, sut_config  # noqa: E402
from dvtest.proto import dextra, m17  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def snippet(name):
    return (SNIPPETS / name).read_text()


def rendered(tmp_path, names):
    text = sut_config.render(BASE, "172.30.0", require_ident="true", dp15_tx_enable="false", extras=[snippet(n) for n in names])
    path = tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return SimpleNamespace(out=str(tmp_path))


# ------------------------------------------------------------------ the run shapes

def test_shape_of_the_optout_run(tmp_path):
    shape = tp.shape_of(tp.read_config(rendered(tmp_path, ["data-ident.toml", "dp7-position-optout.toml"])))
    assert shape.known and shape.positions and shape.optout == {"N0OPT", "N0DMP"}
    assert not shape.rf and not shape.aprsis and not shape.igate and not shape.uplink


def test_shape_of_the_positions_off_run_and_the_order_of_the_extras(tmp_path):
    shape = tp.shape_of(tp.read_config(rendered(tmp_path / "a", ["data-ident.toml", "dp7-positions-off.toml"])))
    assert shape.positions is False and shape.optout == set()
    shape = tp.shape_of(tp.read_config(rendered(tmp_path / "b", ["dp7-positions-off.toml", "data-ident.toml"])))
    assert shape.positions is False
    both = tp.shape_of(tp.read_config(rendered(tmp_path / "c", ["data-ident.toml", "dp7-position-optout.toml", "dp7-positions-off.toml"])))
    assert both.positions is False and both.optout == {"N0OPT", "N0DMP"}


def test_shape_of_the_rf_and_aprs_run(tmp_path):
    names = ["data-ident.toml", "dp15-aprsis.toml", "dp7-rf.toml", "dp7-rf-igate.toml", "dp7-aprsis-up.toml", "dp7-position-optout.toml"]
    shape = tp.shape_of(tp.read_config(rendered(tmp_path, names)))
    assert shape.rf and shape.rf_port == 8101 and shape.aprsis and shape.igate and shape.uplink
    assert shape.positions and shape.optout == {"N0OPT", "N0DMP"}
    doc = tomllib.loads((tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml").read_text())
    paths = {p["name"]: p for p in doc["data"]["path"]}
    assert paths["dp7-igate"]["types"] == ["transit"] and paths["dp7-aprsis-up"]["min_ident"] == "stream"
    assert doc["edges"]["ax25"]["callsign"] == "N0TEST-1" and doc["edges"]["ax25"]["tx_enable"] is False


def test_shape_of_the_rf_only_run_and_the_tier_a_snippet(tmp_path):
    only = tp.shape_of(tp.read_config(rendered(tmp_path / "a", ["data-ident.toml", "dp7-rf.toml"])))
    assert only.rf and not only.aprsis and not only.igate and not only.uplink
    tier_a = tp.shape_of(tp.read_config(rendered(tmp_path / "b", ["data-ident.toml", "dp15-rf.toml"])))
    assert not tier_a.rf, "the ports of dp15-rf.toml are not bound to a room: the DP7 RF cases must skip there"


def test_shape_of_an_unreadable_config(tmp_path):
    shape = tp.shape_of(tp.read_config(SimpleNamespace(out=str(tmp_path))))
    assert shape.known is False and shape.positions and shape.optout == set() and not shape.rf


# ------------------------------------------------------------------ what the senders put on the wire

class FakeTnc:
    def __init__(self, ok=True):
        self.frames, self.ok = [], ok

    def inject(self, frame, channel=0):
        self.frames.append(frame)
        return self.ok


def test_rf_sender_builds_a_ui_frame_with_the_blanked_digits():
    tnc = FakeTnc()
    ctx = SimpleNamespace(rf=tnc)
    tp.send_rf(ctx, "N0OPT-9", ps.APRS_LAT, ps.APRS_LON, level=2, comment="x", spend=False)
    (frame,) = tnc.frames
    assert ax25.dvrouter_accepts_frame(frame)
    assert ax25.to_tnc2(frame) == "N0OPT-9>APDVT1,WIDE1-1:!4903.  N/07201.  W>x"
    tp.send_rf_object(ctx, "N0OPT-9", ps.LAT, ps.LON, name="DVT7OPT", live=False, spend=False)
    assert ax25.to_tnc2(tnc.frames[1]).startswith("N0OPT-9>APDVT1,WIDE1-1:;DVT7OPT  _")
    with pytest.raises(AssertionError, match="no connection"):
        tp.send_rf(SimpleNamespace(rf=FakeTnc(ok=False)), "N0PCR", 1.0, 2.0, spend=False)


def test_m17_sender_builds_the_packet_mode_aprs_payload(monkeypatch):
    sent = []

    class Client:
        dest = ("192.0.2.1", 17000)
        client = SimpleNamespace(send=lambda dest, data: sent.append((dest, data)))

    monkeypatch.setattr(tp, "m17_client", lambda *a, **k: contextlib.nullcontext(Client()))
    monkeypatch.setattr(tp, "pump_for", lambda *a, **k: None)
    ctx = SimpleNamespace(rig=SimpleNamespace(xlxd_sut="192.0.2.1"))
    tp.send_m17(ctx, "N0PM2-7", ps.APRS_LAT, ps.APRS_LON, level=2, comment="amb2", spend=False)
    (dest, data), = sent
    parsed = m17.parse_m17p(data, trailer=False)
    assert parsed["payload"][0] == 0x02 and parsed["payload_crc_ok"]
    assert parsed["payload"][1:].decode() == "N0PM2-7>APDVT1:!4903.  N/07201.  W>amb2"
    assert parsed["src"].startswith("N0PM2")
    tp.send_m17_object(ctx, "N0PDO-7", ps.LAT, ps.LON, name="DVT7OBJ", live=False, spend=False)
    assert b";DVT7OBJ  _" in sent[1][1]


def test_dstar_sender_carries_the_dprs_line_with_the_ambiguity(monkeypatch):
    captured = []
    monkeypatch.setattr(tp, "dstar_listener", lambda *a, **k: contextlib.nullcontext(SimpleNamespace()))
    monkeypatch.setattr(tp, "send_over", lambda talker, packets, **k: captured.append(packets))
    monkeypatch.setattr(tp, "pump_for", lambda *a, **k: None)
    ctx = SimpleNamespace(rig=SimpleNamespace(xlxd_sut="192.0.2.1"))
    tp.send_dstar(ctx, "N0PDA-3", ps.APRS_LAT, ps.APRS_LON, level=2, comment="amb2", spend=False)
    packets, = captured
    header = dextra.parse_header_full(packets[0])
    assert bytes(header["my"]).rstrip() == b"N0PDA"                            # MY is the base, the line carries the SSID
    fields = []
    for packet in packets[1:-1]:
        voice = dextra.parse_voice_raw(packet)
        if voice:
            fields.append((voice["counter"] & 0x1F, voice["slow"]))
    events = dstar_sd.DvrUnitDecoder().feed_frames(fields)
    lines = [e for e in events if e["kind"] == "dprs"]
    assert lines and lines[0]["crc_ok"]
    assert lines[0]["line"] == b"N0PDA-3>API510,DSTAR*:!4903.  N/07201.  W>amb2\r"
    # the 19 byte position field the product slices (dstar_dprs.rs:105-108): ddmm.hhN, table, dddmm.hhE, code; blanks keep the width
    info = lines[0]["line"].split(b":", 1)[1]
    assert info[0:1] == b"!" and len(info[1:20]) == 19
    assert info[1:8] == b"4903.  " and info[8:9] == b"N" and info[9:10] == b"/" and info[10:18] == b"07201.  " and info[18:19] == b"W"
    assert info[19:20] == b">"
    texts = [e for e in events if e["kind"] == "text"]
    assert texts and texts[0]["text"].startswith("DVT DP7")


def test_pace_helpers_only_touch_the_pacer_when_the_send_is_expected_to_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(tp.PACER, "wait", lambda call, kind: calls.append(("wait", call, kind)))
    monkeypatch.setattr(tp.PACER, "offered", lambda call, kind, at=None: calls.append(("offered", call, kind)))
    tp.pace_before("N0X", False)
    tp.pace_after("N0X", False)
    assert calls == []
    tp.pace_before("N0X", True, ("status", "position"))
    tp.pace_after("N0X", True)
    assert calls == [("wait", "N0X", "status"), ("wait", "N0X", "position"), ("offered", "N0X", "position")]


def test_case_callsigns_are_distinct_and_the_dmr_ones_resolve():
    per_case = [tp.A_CALL, tp.CONTROL_CALL, tp.OFF_CALL, tp.AMB_M17_CALL, tp.AMB_RF_CALL]
    bases = [ps.base_call(c) for d in per_case for c in d.values()]
    bases += [tp.AMB_DPRS_CALL, tp.UPLINK_CONTROL_CALL, tp.TRANSIT_CONTROL_CALL, tp.OBJECT_REPORTER, tp.HIDE_CALL]
    assert len(bases) == len(set(bases)), "a base callsign is used by two cases (the controller admits 1 position per 30 s per base)"
    assert tp.OPT_CALL not in bases and tp.OPT_DMR not in bases
    for call in (tp.A_CALL["dmr"], tp.CONTROL_CALL["dmr"], tp.OFF_CALL["dmr"], tp.OPT_DMR):
        assert ps.dmr_id_for(call) is not None
    # AX.25 addresses have at most 6 characters in the base
    assert all(len(ps.base_call(c)) <= 6 for c in bases)


# ------------------------------------------------------------------ watchers and small helpers

class FakeHub:
    def __init__(self, series):
        self.series = series            # (family, labels) -> list of values returned one per read
        self.reads = {}

    def total(self, family, **labels):
        key = (family, tuple(sorted(labels.items())))
        values = self.series[key]
        i = self.reads.get(key, 0)
        self.reads[key] = i + 1
        return values[min(i, len(values) - 1)]


def test_drops_waits_for_both_counters(monkeypatch):
    monkeypatch.setattr(tp.time, "sleep", lambda s: None)
    items = ("dv_pkt_items_total", (("kind", "position"), ("result", "optout")))
    room = ("dv_room_data_total", (("result", "optout"),))
    hub = FakeHub({items: [5, 5, 6, 6], room: [2, 2, 2, 3]})
    drops = tp.Drops(hub, "position")
    assert (drops.items, drops.room) == (5, 2)
    got = drops.wait(1, timeout=30)
    assert got == (1, 1, True)
    stuck = tp.Drops(FakeHub({items: [5], room: [2]}), "position")
    assert stuck.wait(1, timeout=0) == (0, 0, False)


def test_spatial_pred_and_keys_of():
    row = {"type": "data", "item_type": "object", "callsign": "N0PDO-7", "body": {"name": "DVT7OBJ", "state": 0}}
    assert tp.spatial_pred("N0PDO", "object", "DVT7OBJ", state=0)(row)
    assert not tp.spatial_pred("N0PDO", "object", "DVT7OBJ", state=1)(row)
    assert not tp.spatial_pred("N0PDO", "position")(row) and not tp.spatial_pred("N0XXX", "object")(row)
    assert tp.keys_of({"a": 1, "b": {"lat_e7": 1}, "c": [{"d": 2}]}) == {"a", "b", "lat_e7", "c", "d"}


class FakeOp:
    def __init__(self, status=200, flag=True):
        self.status, self.flag, self.calls = status, flag, []

    def request(self, method, path, json_body=None, expect=None):
        self.calls.append((method, path, json_body))
        return SimpleNamespace(status=self.status, text=lambda: "boom")

    def get(self, path, expect=None):
        return SimpleNamespace(json=lambda: {"callsign": "N0HIDE", "hide_position": self.flag})

    def scrub(self, text):
        return text


def test_set_hide_position_patches_and_reads_back():
    op = FakeOp(flag=True)
    ctx = SimpleNamespace(op=op)
    tp.set_hide_position(ctx, "N0HIDE", True)
    assert op.calls == [("PATCH", "/api/v1/operator/accounts/N0HIDE", {"hide_position": True})]
    with pytest.raises(AssertionError, match="reads hide_position"):
        tp.set_hide_position(SimpleNamespace(op=FakeOp(flag=False)), "N0HIDE", True)
    with pytest.raises(AssertionError, match="HTTP 403"):
        tp.set_hide_position(SimpleNamespace(op=FakeOp(status=403)), "N0HIDE", False)


def test_dprs_ambiguity_finding_wraps_only_the_missing_event_failure():
    missing = "DP7-c[d-prs level 2]: no position / object event of the base callsign on the ANONYMOUS public SSE data topic"
    wrapped = tp.dprs_ambiguity_finding(missing)
    assert wrapped.startswith("FINDING DP7-FIND1") and wrapped.endswith(missing)
    assert "dstar_dprs.rs:105" in wrapped and "bad_position" in wrapped
    other = "DP7-c[d-prs level 2]: the operator feed body carries ambiguity 0, want 2"
    assert tp.dprs_ambiguity_finding(other) == other
