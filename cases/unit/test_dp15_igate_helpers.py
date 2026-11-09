# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp15_igate.py (DP15 group 8, the RF-to-APRS-IS igate): the metric route label, the shape
reader against the real snippets in both run shapes, the line check, the counter reader and the window arithmetic. Offline."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp15_igate as ti  # noqa: E402

from dvtest import aprsis_client as ai, kiss_tnc, metrics, sut_config  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered_doc(tx, files):
    import tomllib
    extras = [(SNIPPETS / name).read_text() for name in files]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", dp15_tx_enable=tx, extras=extras))


FILES = ["dp15-rf.toml", "dp15-aprsis.toml", "dp15-rf-igate.toml"]


def test_route_label_is_fnv1a64_of_the_name():
    assert ti.route_label("") == "pcbf29ce484222325"                       # the FNV-1a 64 offset basis
    assert ti.route_label("a") == "paf63dc4c8601ec8c"                      # published FNV-1a 64 test vector
    assert len(ti.route_label("dp15-igate")) == 17 and ti.route_label("dp15-igate") != ti.route_label("dp7-igate")


def test_shape_in_both_run_shapes_from_the_real_snippets():
    tx = ti.igate_shape(rendered_doc("true", FILES + ["dp15-rf-out.toml"]))
    assert (tx.path, tx.gateway, tx.tx_on, tx.q, tx.per_min) == ("dp15-igate", "N0TEST-1", True, "qAR", 20)
    rx = ti.igate_shape(rendered_doc("false", FILES))
    assert (rx.tx_on, rx.q, rx.gateway) == (False, "qAO", "N0TEST-1")


def test_shape_is_none_without_a_piece():
    assert ti.igate_shape({}) is None
    assert ti.igate_shape(rendered_doc("true", ["dp15-rf.toml", "dp15-aprsis.toml"])) is None            # no transit path
    assert ti.igate_shape(rendered_doc("true", ["dp15-aprsis.toml"])) is None                              # no AX.25 edge
    doc = rendered_doc("true", FILES)
    doc["data"]["path"][-1]["enable"] = False
    assert ti.igate_shape(doc) is None
    doc = rendered_doc("true", FILES)
    doc["edges"]["ax25"]["callsign"] = ""
    assert ti.igate_shape(doc) is None


def good_line(info, q="qAR", gateway="N0TEST-1", digis=ti.DIGIS, src="N0IGW-3"):
    return ai.parse_line("%s>%s,%s,%s,%s:%s" % (src, ti.TOCALL, ",".join(digis), q, gateway, info))


def test_check_gated_line_accepts_the_expected_line_and_names_every_difference():
    info = aprs.weather_info(11, 10, 1, 0, 220, 4, 5, 77)
    assert ti.check_gated_line(good_line(info), "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1") == []
    extra = ai.parse_line("N0IGW-3>APDVT1,WIDE1-1*,qAR,N0TEST-1,N0TEST-5:" + info)                      # the server may add calls after it
    assert ti.check_gated_line(extra, "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1") == []
    bad = ti.check_gated_line(good_line(info, q="qAO"), "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1")
    assert len(bad) == 1 and "qAO" in bad[0]
    assert any("N0TEST-5" in p for p in ti.check_gated_line(good_line(info, gateway="N0TEST-5"), "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1"))
    assert any("information" in p for p in ti.check_gated_line(good_line(info + " "), "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1"))
    assert any("path before" in p for p in ti.check_gated_line(good_line(info, digis=("WIDE1-1",)), "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1"))
    twice = ai.parse_line("N0IGW-3>APDVT1,WIDE1-1*,qAR,N0TEST-1,qAC,T2TEST:" + info)
    assert any("2 q constructs" in p for p in ti.check_gated_line(twice, "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1"))
    none = ai.parse_line("N0IGW-3>APDVT1,WIDE1-1*:" + info)
    assert any("0 q constructs" in p for p in ti.check_gated_line(none, "N0IGW-3", info, ti.DIGIS, "qAR", "N0TEST-1"))


def test_a_colon_in_the_information_field_stays_inside_it():
    info = aprs.telemetry_info(7, (1, 2, 3, 4, 5), "00000001", comment=":x")
    assert ti.check_gated_line(good_line(info, src="N0IGT-3"), "N0IGT-3", info, ti.DIGIS, "qAR", "N0TEST-1") == []


def test_path_counts_filter_by_route_and_direction():
    text = "\n".join([
        'dv_pkt_path_total{route="%s",dir="out",result="forwarded"} 3' % ti.route_label("dp15-igate"),
        'dv_pkt_path_total{route="%s",dir="out",result="no_gate"} 2' % ti.route_label("dp15-igate"),
        'dv_pkt_path_total{route="%s",dir="in",result="forwarded"} 9' % ti.route_label("dp15-igate"),
        'dv_pkt_path_total{route="%s",dir="out",result="forwarded"} 5' % ti.route_label("rf-in"),
        'dv_pkt_ack_total{result="ack"} 1'])
    counts, known = ti.path_counts(metrics.parse(text), ti.route_label("dp15-igate"))
    assert counts == {"forwarded": 3, "no_gate": 2} and known
    counts, known = ti.path_counts(metrics.parse(text), ti.route_label("other"))
    assert counts == {} and not known


def test_absent_window_scales_with_the_slowest_latency_and_is_bounded():
    assert ti.absent_window([]) == ti.ABSENT_MIN_S
    assert ti.absent_window([0.2, 0.4]) == ti.ABSENT_MIN_S
    assert ti.absent_window([1.0, 5.0]) == 20.0
    assert ti.absent_window([100.0]) == ti.ABSENT_MAX_S


def test_the_injected_frames_decode_to_the_expected_tnc2_lines():
    for src, info, digis in [(ti.SRC_WEATHER, aprs.weather_info(11, 10, 1, 0, 220, 4, 5, 77), ti.DIGIS),
                             (ti.SRC_QUERY, aprs.query_info("APRS"), ("WIDE1-1",)),
                             (ti.SRC_NOGATE, aprs.weather_info(11, 10, 1, 1, 100, 1, 2, 60), ("WIDE1-1*", "NOGATE")),
                             (ti.SRC_RFONLY, aprs.weather_info(11, 10, 1, 3, 110, 2, 3, 61), ("RFONLY",))]:
        frame = kiss_tnc.ui_frame(src, ti.TOCALL, info, digis=digis)
        from dvtest import ax25
        assert ax25.dvrouter_accepts_frame(frame)
        assert ax25.to_tnc2(frame) == "%s>%s,%s:%s" % (src, ti.TOCALL, ",".join(digis), info)
    assert len({ti.SRC_WEATHER, ti.SRC_TELEMETRY, ti.SRC_QUERY, ti.SRC_NOGATE, ti.SRC_RFONLY, *ti.SRC_CONTROL.values()}) == 8


# ---------------------------------------------------------------- the case flow against fakes (no rig, short waits)

class FakeAprsc:
    """The tester's login on the aprsc: `lines` are what the server delivers (parsed on read, like AprsisClient)."""

    def __init__(self, lines):
        self.lines = lines

    def mark(self):
        return len(self.lines)

    def _parsed(self, since):
        return [(0.0, ai.parse_line(line), line) for line in self.lines[since:]]

    def wait_packet(self, pred, timeout, since=0):
        import time as _t
        end = _t.monotonic() + timeout
        while True:
            for at, parsed, line in self._parsed(since):
                if parsed and pred(parsed):
                    return _t.monotonic(), parsed, line
            if _t.monotonic() >= end:
                return None
            _t.sleep(0.005)

    def matching(self, pred, since=0):
        return [(at, parsed) for at, parsed, _l in self._parsed(since) if parsed and pred(parsed)]


class FakeEdge:
    """What DV-Router does with a heard frame: gates it (or not), counts the verdict on the route."""

    def __init__(self, label, q="qAR", gateway="N0TEST-1", drop_src=(), double=False):
        self.label, self.q, self.gateway, self.drop_src, self.double = label, q, gateway, set(drop_src), double
        self.lines, self.counts = [], {}
        self.a = self

    def inject(self, frame):
        from dvtest import ax25
        tnc2 = ax25.to_tnc2(frame)
        head, _sep, info = tnc2.partition(":")
        src, _gt, rest = head.partition(">")
        parts = rest.split(",")
        if src in self.drop_src:
            result = "filtered" if info.startswith("?") else "no_gate"
            self.counts[result] = self.counts.get(result, 0) + 1
            return True
        line = "%s>%s:%s" % (src, ",".join([parts[0]] + parts[1:] + [self.q, self.gateway]), info)
        self.lines.append(line)
        self.counts["forwarded"] = self.counts.get("forwarded", 0) + (2 if self.double else 1)
        return True

    def text(self):
        return "\n".join('dv_pkt_path_total{route="%s",dir="out",result="%s"} %d' % (self.label, r, n) for r, n in self.counts.items())


@pytest.fixture
def fast(monkeypatch):
    import time as real
    monkeypatch.setattr(ti, "time", SimpleNamespace(monotonic=real.monotonic, sleep=lambda s: real.sleep(min(s, 0.01))))
    for name, value in (("ONCE_S", 0.02), ("ABSENT_MIN_S", 0.05), ("COUNTER_QUIET_S", 0.03), ("GATE_WAIT_S", 0.5), ("COUNTER_WAIT_S", 0.05)):
        monkeypatch.setattr(ti, name, value)
    return monkeypatch


def flow_ctx(edge, q="qAR"):
    shape = SimpleNamespace(path="dp15-igate", gateway="N0TEST-1", tx_on=q == "qAR", q=q, per_min=20)
    return SimpleNamespace(shape=shape, rf=edge, host="127.0.0.1", label=ti.route_label("dp15-igate"))


def run_case(fast, edge, ctx, which, **kw):
    import contextlib
    ledger = {"latency_s": {}, "window_s": {}, "counters": {}}
    tester = FakeAprsc(edge.lines)
    fast.setattr(ti.p1, "aprsc_login", lambda host: contextlib.nullcontext(tester))
    fast.setattr(ti.ds, "write_note", lambda *a, **k: None)
    target = SimpleNamespace(out="/nonexistent")
    hub = SimpleNamespace(text=edge.text)
    if which == "positive":
        info = aprs.weather_info(11, 10, 1, 0, 220, 4, 5, 77)
        ti.positive_case(target, hub, ctx, ledger, "T", ti.SRC_WEATHER, info)
    else:
        ti.negative_case(target, hub, ctx, ledger, "N", "nogate", ti.SRC_NOGATE, aprs.weather_info(11, 10, 1, 1, 100, 1, 2, 60),
                         ("WIDE1-1*", "NOGATE"), kw.get("result", "no_gate"))
    return ledger


def test_flow_positive_case_passes_and_records_the_latency(fast):
    edge = FakeEdge(ti.route_label("dp15-igate"))
    ledger = run_case(fast, edge, flow_ctx(edge), "positive")
    assert "T" in ledger["latency_s"] and ledger["counters"]["T"] == {"forwarded": 1}


def test_flow_positive_case_fails_on_a_wrong_q_construct_and_on_a_double_count(fast):
    edge = FakeEdge(ti.route_label("dp15-igate"), q="qAO")
    with pytest.raises(AssertionError, match="qAO"):
        run_case(fast, edge, flow_ctx(edge, "qAR"), "positive")
    edge = FakeEdge(ti.route_label("dp15-igate"), double=True)
    with pytest.raises(AssertionError, match="forwarded"):
        run_case(fast, edge, flow_ctx(edge), "positive")


def test_flow_negative_case_passes_when_the_bad_frame_is_dropped(fast):
    edge = FakeEdge(ti.route_label("dp15-igate"), drop_src=[ti.SRC_NOGATE])
    ledger = run_case(fast, edge, flow_ctx(edge), "negative")
    assert ledger["window_s"]["N"] >= ti.ABSENT_MIN_S and ledger["counters"]["N"] == {"forwarded": 1, "no_gate": 1}


def test_flow_negative_case_fails_when_the_bad_frame_is_gated_or_the_control_is_lost(fast):
    edge = FakeEdge(ti.route_label("dp15-igate"))                                  # gates everything, the bad frame too
    with pytest.raises(AssertionError, match="must not be gated"):
        run_case(fast, edge, flow_ctx(edge), "negative")
    edge = FakeEdge(ti.route_label("dp15-igate"), drop_src=[ti.SRC_NOGATE, ti.SRC_CONTROL["nogate"]])
    with pytest.raises(AssertionError, match="proves nothing"):
        run_case(fast, edge, flow_ctx(edge), "negative")


def test_flow_a_counter_family_without_the_route_is_a_finding_not_a_failure(fast):
    edge = FakeEdge("p0000000000000000")                                           # the series carries another route label
    ledger = run_case(fast, edge, flow_ctx(edge), "positive")
    assert "T" in ledger["latency_s"]
