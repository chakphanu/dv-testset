# SPDX-License-Identifier: MIT
"""cases/test_digi1.py and cases/test_digi1_dw.py against a FAKE edge that applies the model of dvtest/digi1.py to what it hears: the case logic itself
(injection, waiting, ledger, assertions, skips), not the product. No rig, no docker; the waits are shrunk to a few hundred milliseconds."""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_digi1 as t  # noqa: E402
import test_digi1_dw as tdw  # noqa: E402

from dvtest import ax25, digi1  # noqa: E402

pytestmark = pytest.mark.unit

SCALE_WINDOW_S = 1.2
SCALE_TIMES = (0.0, 0.3, 1.1, 1.3, 1.9)


class FakeEdgeTnc:
    """The far end of the edge as the case sees it: inject() = a frame heard on the air, data_frames() = what the edge wrote. The edge is the model."""

    def __init__(self, window_s=30.0, live=True):
        self.table = digi1.DedupeTable(window_s)
        self.frames = []
        self.live = live
        self.heard = []

    def inject(self, frame, channel=0):
        self.heard.append(frame)
        result, out = digi1.decide(frame, digi1.OURS, self.table, time.monotonic())
        if out is not None and self.live:
            self.frames.append((time.monotonic(), 0, out))
        return True

    def data_frames(self, since=0.0):
        return [x for x in self.frames if x[0] >= since]

    def wait_data(self, count=1, timeout=10.0, since=0.0):
        end = time.monotonic() + timeout
        while len(self.data_frames(since)) < count and time.monotonic() < end:
            time.sleep(0.01)
        return self.data_frames(since)


def make_digi(window_s=30.0, **kw):
    tnc, other = FakeEdgeTnc(window_s, **kw), FakeEdgeTnc(window_s, live=False)       # rf-b has no digi
    return types.SimpleNamespace(tnc=tnc, other=other, table=digi1.DedupeTable(window_s), expected=digi1.Expected(), t0=time.monotonic(),
                                 shape=types.SimpleNamespace(name="roomy"), base=None, rf=None)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    notes = []
    monkeypatch.setattr(t, "note", lambda target, case, tag, text: notes.append((case, tag, text)))
    monkeypatch.setattr(tdw, "note", lambda target, case, tag, text: notes.append((case, tag, text)))
    monkeypatch.setattr(t, "REPEAT_WAIT_S", 0.5)
    monkeypatch.setattr(t, "AFTER_REPEAT_S", 0.03)
    monkeypatch.setattr(t, "SILENCE_S", 0.03)
    monkeypatch.setattr(t, "KEY_SILENCE_S", 0.03)
    monkeypatch.setattr(t, "ORACLE", {})
    return notes


VECTORS = list(digi1.GOLDENS) + list(digi1.extra_vectors())


@pytest.mark.parametrize("g", VECTORS, ids=[g.id for g in VECTORS])
def test_r1_vector_passes_against_a_model_edge_and_fills_the_ledger(g, fast):
    digi = make_digi()
    t.test_digi1_r1_path_vector(None, None, None, digi, g)
    assert digi.expected.total() == 1 and digi.expected.counts[g.result] == 1
    assert (len(digi.tnc.frames) == 1) == (g.result == "repeat")


def test_r1_vector_fails_when_the_edge_repeats_a_frame_it_should_not():
    digi = make_digi()
    digi.tnc.inject = lambda frame, channel=0: digi.tnc.frames.append((time.monotonic(), 0, ax25.replace_hop(frame, 0, digi1.OURS))) or True
    g = next(x for x in digi1.GOLDENS if x.id == "G4")
    with pytest.raises(AssertionError, match="expected silence"):
        t.test_digi1_r1_path_vector(None, None, None, digi, g)


def test_r1_vector_fails_on_wrong_bytes_and_on_a_missing_repeat():
    g = next(x for x in digi1.GOLDENS if x.id == "G1")
    silent = make_digi(live=False)
    with pytest.raises(AssertionError, match="1 frame|0 frames"):
        t.test_digi1_r1_path_vector(None, None, None, silent, g)
    wrong = make_digi()
    wrong.tnc.inject = lambda frame, channel=0: wrong.tnc.frames.append((time.monotonic(), 0, ax25.replace_hop(frame, 0, "N0TEST-11"))) or True
    with pytest.raises(AssertionError, match="expected"):
        t.test_digi1_r1_path_vector(None, None, None, wrong, g)


def oracle_record(g, frame, heard=None, b_received=True):
    if heard is None:
        _r, out = digi1.decide(frame, digi1.OURS)
        heard = [out.hex()] if out else []
    return {"case": g.id, "kind": "path", "sent_hex": frame.hex(), "dw_heard_hex": heard, "b_received": b_received, "dw_expected": g.dw}


def test_r1_vector_compares_with_the_oracle_when_the_vectors_exist(monkeypatch, fast):
    g = next(x for x in digi1.GOLDENS if x.id == "G1")
    frame = digi1.golden_frame(g, b" o")
    monkeypatch.setattr(t, "ORACLE", {"G1": oracle_record(g, frame)})
    t.test_digi1_r1_path_vector(None, None, None, make_digi(), g)
    monkeypatch.setattr(t, "ORACLE", {"G1": oracle_record(g, frame, heard=[])})           # Dire Wolf silent where the model repeats: the oracle disagrees
    with pytest.raises(AssertionError, match="oracle disagrees"):
        t.test_digi1_r1_path_vector(None, None, None, make_digi(), g)


def test_r1_vector_records_a_documented_deviation_without_failing(monkeypatch, fast):
    g = next(x for x in digi1.GOLDENS if x.id == "G10")
    frame = digi1.golden_frame(g, b" o")
    monkeypatch.setattr(t, "ORACLE", {"G10": oracle_record(g, frame, heard=[ax25.replace_hop(frame, 0, digi1.OURS).hex()])})
    t.test_digi1_r1_path_vector(None, None, None, make_digi(), g)
    assert any("documented deviation" in text for _c, _t, text in fast)


def test_r1_same_port_only_passes_and_fails():
    digi = make_digi()
    hub = types.SimpleNamespace(text=lambda: "")
    t.test_digi1_r1_same_port_only(None, None, hub, digi)
    digi.other.inject = lambda frame, channel=0: digi.other.frames.append((time.monotonic(), 0, frame)) or True
    with pytest.raises(AssertionError, match="written to a TNC"):
        t.test_digi1_r1_same_port_only(None, None, hub, digi)


def test_r2_key_rules_pass_against_the_model_edge_and_count_the_ledger():
    digi = make_digi()
    t.test_digi1_r2_key_rules(None, None, None, digi)
    assert digi.expected.counts["repeat"] == 5 and digi.expected.counts["duplicate"] == 3 and len(digi.tnc.frames) == 5


def test_r2_timeline_with_a_shrunk_clock(monkeypatch):
    monkeypatch.setattr(digi1, "TIMELINE_S", SCALE_TIMES)
    monkeypatch.setattr(digi1, "timeline_expect", lambda times=SCALE_TIMES, window_s=SCALE_WINDOW_S: ["repeat", "duplicate", "duplicate", "repeat", "duplicate"])
    monkeypatch.setattr(t, "SILENCE_S", 0.05)
    digi = make_digi(SCALE_WINDOW_S)
    t.test_digi1_r2_dedupe_timeline(None, None, None, digi)
    assert digi.expected.counts["repeat"] == 2 and digi.expected.counts["duplicate"] == 3 and len(digi.tnc.frames) == 2
    wrong = make_digi(0.2)                                    # a window that is too short repeats the 10 s step too
    with pytest.raises(AssertionError, match="decisions at"):
        t.test_digi1_r2_dedupe_timeline(None, None, None, wrong)


def test_r4_two_fill_ins_against_the_model_edge():
    digi = make_digi()
    t.test_digi1_r4_two_fill_ins_consume_one_hop_each(None, None, None, digi)
    assert digi.expected.counts["repeat"] == 2 and digi.expected.counts["no_match"] == 2 and digi.expected.counts["own_path"] == 1
    assert digi.expected.counts["duplicate"] == 1 and len(digi.tnc.frames) == 2


def test_the_switch_skips_without_dvt_digi1(monkeypatch):
    monkeypatch.delenv("DVT_DIGI1", raising=False)
    with pytest.raises(pytest.skip.Exception, match="DVT_DIGI1"):
        t.digi1_switch.__wrapped__()
    with pytest.raises(pytest.skip.Exception, match="DVT_DIGI1"):
        tdw.digi1_switch.__wrapped__()
    monkeypatch.setenv("DVT_DIGI1", "on")
    assert t.digi1_switch.__wrapped__() is None


def test_need_skips_the_wrong_shape_and_names_the_snippet():
    digi = make_digi()
    t.need(digi, "roomy")
    with pytest.raises(pytest.skip.Exception, match="digi1.toml"):
        t.need(digi, "floor")
    with pytest.raises(pytest.skip.Exception, match="digi1-roomy.toml"):
        digi.shape.name = "floor"
        t.need(digi, "roomy")


def test_sig_ignores_the_information_and_the_command_bits():
    a = ax25.build_ui("APRS", "N0TEST-9", [(digi1.OURS, True), "WIDE2-1"], b">one", cr_src=False)
    b = ax25.build_ui("APRS", "N0TEST-9", [(digi1.OURS, True), "WIDE2-1"], b">two", cr_src=True)
    assert t.sig(a) == t.sig(b) and t.sig(a) != t.sig(ax25.replace_hop(a, 1, "N0TEST-3"))


def test_vector_ids_of_the_tier_b_case_exist():
    ids = {g.id for g in digi1.GOLDENS} | {g.id for g in digi1.extra_vectors()}
    assert set(tdw.VECTORS) <= ids
    results = {g.id: g.result for g in digi1.GOLDENS}
    assert [results[v] for v in tdw.VECTORS] == ["repeat", "repeat", "repeat", "repeat", "no_match", "bad_path"]


def test_the_case_modules_name_only_switches_the_tester_gets():
    # the guard of cases/unit/test_tester_env.py covers the rest; this one pins the name used here
    assert "DVT_DIGI1" in (Path(t.__file__).read_text() + Path(tdw.__file__).read_text())
    assert "DVT_DIGI1: ${DVT_DIGI1:-}" in (Path(__file__).resolve().parents[2] / "rig" / "compose.yml").read_text()
    assert os.environ.get("DVT_DIGI1") in (None, "", "on", "off")
