# SPDX-License-Identifier: MIT
"""The helpers and flows of the DP19b rig cases (cases/test_dp19b_status.py, test_dp19b_browser.py) against fakes: the spatial_status readers of cases/dp6c_support.py, the three
injected items, the flag checks, the probe skip, the hide flow with fake surfaces and the opt-in account-store swap with its restore. No rig, no sleeps beyond milliseconds."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dp6c_support as sp  # noqa: E402
import test_dp19b_status as st  # noqa: E402

from dvtest import ax25, dp6c  # noqa: E402

pytestmark = pytest.mark.unit


def srow(call, body, spatial, kind="status", rid=1):
    row = {"type": "data", "id": rid, "item_type": kind, "room": "B", "callsign": call, "protocol": "aprs", "ts_ms": 1, "ident": "none", "via": "rf", "body": body}
    if kind == "status":
        row["spatial"] = spatial
    return row


def test_message_rows_select_by_base_type_and_body():
    rows = [srow("N0DPA-7", "IO91SX/G", True), srow("N0DPA-9", "Ready", False), srow("N0DPA-9", "IO91SX/G", None, "text"), srow("N0DPAB-1", "Ready", False),
            {"item_type": "position", "callsign": "N0DPA-7"}]
    assert len(sp.message_rows(rows, "N0DPA")) == 3 and len(sp.message_rows(rows, "n0dpa", "status")) == 2
    assert [r["item_type"] for r in sp.message_rows(rows, "N0DPA", body="IO91SX/G")] == ["status", "text"]
    assert sp.message_rows(rows, "N0DPA", "text", "Ready") == []


class State:
    def __init__(self, rooms):
        self.rooms = rooms


def test_the_state_readers(monkeypatch):
    doc = {"rooms": [{"room": "A", "spatial_status": []}, {"room": "B", "positions": [], "spatial_status": [srow("N0DPA-7", "IO91SX/G", True)]}]}
    monkeypatch.setattr(sp.ps, "public_state", lambda anon: doc)
    assert [r["callsign"] for r in sp.state_spatial(None)] == ["N0DPA-7"] and sp.state_spatial(None, "A") == [] and sp.state_spatial(None, "Z") == []
    assert sp.has_spatial_status_key(None) is True and sp.has_spatial_status_key(None, "Z") is False
    doc["rooms"][1].pop("spatial_status")
    assert sp.has_spatial_status_key(None) is False and sp.state_spatial(None) == []                    # an image before DP19b


def test_wait_state_spatial_and_wait_history(monkeypatch):
    seq = iter([[], [srow("N0DPA-7", "x", True)], [srow("N0DPA-7", "x", True)], []])
    monkeypatch.setattr(sp, "state_spatial", lambda anon, room="B": next(seq))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    waited, rows = sp.wait_state_spatial(None, "N0DPA", True, timeout=5.0)
    assert waited is not None and len(rows) == 1
    waited, rows = sp.wait_state_spatial(None, "N0DPA", False, timeout=5.0)
    assert waited is not None and rows == []
    monkeypatch.setattr(sp, "state_spatial", lambda anon, room="B": [srow("N0DPA-7", "x", True)])
    assert sp.wait_state_spatial(None, "N0DPA", False, timeout=0.0)[0] is None
    from dvtest import web
    hist = iter([[srow("N0DPA-9", "Ready", False)], [srow("N0DPA-9", "Ready", False), srow("N0DPA-7", "x", True), srow("N0DPA-9", "y", None, "text")]])
    monkeypatch.setattr(web, "room_messages", lambda client, room, limit=64, after_ms=None: next(hist))
    waited, rows = sp.wait_history(None, "N0DPA", lambda r: len(r) >= 3, timeout=5.0)
    assert waited is not None and len(rows) == 3


class Tnc:
    def __init__(self):
        self.frames = []

    def inject(self, frame):
        self.frames.append(frame)
        return True


def test_inject_trio_sends_the_three_items_of_review_247(monkeypatch):
    slept = []
    clock = {"t": 0.0}
    monkeypatch.setattr(sp, "PACER", sp.ds.Pacer(clock=lambda: clock["t"], sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s))))
    tnc = Tnc()
    st.inject_trio(tnc, "N0DPA")
    lines = [ax25.to_tnc2(f) for f in tnc.frames]
    assert lines[0].startswith("N0DPA-7>APDVT1,WIDE1-1:>IO91SX/G") and lines[1].startswith("N0DPA-9>APDVT1,WIDE1-1:>Ready")
    assert lines[2].startswith("N0DPA-9>APDVT1,WIDE1-1::XLX999B  :IO91SX/G") and not lines[2].endswith("{1")
    assert slept == [sp.ds.MIN_GAP_S["status"]]                                              # only the second status of the base waits, for the status bucket
    tnc = Tnc()
    st.inject_trio(tnc, "N0DPB", " dpb")
    assert [ax25.to_tnc2(f).split(":", 1)[1] for f in tnc.frames] == [">IO91SX/G dpb", ">Ready dpb", ":XLX999B  :IO91SX/G dpb"]


def test_check_flags_and_the_sse_item_selection():
    spatial, plain, text = [srow("N0DPA-7", "IO91SX/G", True)], [srow("N0DPA-9", "Ready", False)], [srow("N0DPA-9", "IO91SX/G", None, "text")]
    st.check_flags(spatial, plain, text)
    for bad in ((plain, plain, text), (spatial, spatial, text), (spatial, plain, [dict(text[0], spatial=False)])):
        with pytest.raises(AssertionError):
            st.check_flags(*bad)
    watch = SimpleNamespace(data_events=lambda since=0.0: [(1.0, spatial[0]), (2.0, plain[0]), (3.0, text[0]), (4.0, srow("N0OTH-1", "Ready", False))])
    got = st.sse_items(watch, "N0DPA")
    assert [len(g) for g in got] == [1, 1, 1] and st.wait_trio_on_sse(watch, "N0DPA", 0.0, timeout=0.0) == got
    assert [len(g) for g in st.wait_trio_on_sse(watch, "N0NONE", 0.0, timeout=0.0)] == [0, 0, 0]


def test_the_image_probe_skips_without_spatial_status(monkeypatch):
    anon = object()
    web_ctx = SimpleNamespace(anon=lambda: anon)
    probe = st.image.__wrapped__
    monkeypatch.setattr(sp, "has_spatial_status_key", lambda a, room="B": False)
    with pytest.raises(pytest.skip.Exception, match="no DP19b"):
        probe(web_ctx, SimpleNamespace(positions=True), object())
    with pytest.raises(pytest.skip.Exception, match="positions = false"):
        probe(web_ctx, SimpleNamespace(positions=False), object())
    monkeypatch.setattr(sp, "has_spatial_status_key", lambda a, room="B": True)
    assert probe(web_ctx, SimpleNamespace(positions=True), object()) is anon


def test_the_hide_case_withdraws_only_the_spatial_status(monkeypatch):
    """test_dp19b_2 against fakes whose surfaces follow the hide flag."""
    flag = {"hidden": False}
    now = {"t": 0.0}
    spatial_row = srow("N0DPB-7", "IO91SX/G dpb", True)
    plain_row, text_row = srow("N0DPB-9", "Ready dpb", False), srow("N0DPB-9", "IO91SX/G dpb", None, "text")
    monkeypatch.setattr(st.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))
    monkeypatch.setattr(sp.time, "sleep", lambda s: now.__setitem__("t", now["t"] + s))
    monkeypatch.setattr(sp.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(st, "inject_trio", lambda rf, base, suffix="": None)
    monkeypatch.setattr(st, "wait_trio_on_sse", lambda watch, base, since, timeout=0: ([spatial_row], [plain_row], [text_row]))
    monkeypatch.setattr(sp, "state_spatial", lambda anon, room="B": [] if flag["hidden"] else [spatial_row])
    from dvtest import web
    monkeypatch.setattr(web, "room_messages", lambda client, room, limit=64, after_ms=None: ([plain_row, text_row] if flag["hidden"] else [spatial_row, plain_row, text_row]))
    monkeypatch.setattr(st, "set_hide_position", lambda web_ctx, call, value: flag.__setitem__("hidden", bool(value)))
    notes = []
    monkeypatch.setattr(sp, "note", lambda target, case, tag, text: notes.append((tag, text)))

    class Feeds:
        def __enter__(self):
            return SimpleNamespace(pub=SimpleNamespace(), t0=0.0)

        def __exit__(self, *exc):
            return False

    class Replay:
        def __init__(self, *a, **k):
            self.error = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ready(self):
            return True

        def data_events(self, since=0.0):
            return [(1.0, plain_row), (2.0, text_row)] + ([] if flag["hidden"] else [(3.0, spatial_row)])

    monkeypatch.setattr(sp, "feeds", lambda web_ctx, seconds=600.0: Feeds())
    monkeypatch.setattr(sp, "first_event_id", lambda watch: "7-1")
    monkeypatch.setattr(sp, "SseReplay", Replay)
    web_ctx = SimpleNamespace(user=lambda call, may_tx=False: None, reader=None, anon=lambda: None)
    st.test_dp19b_2_hide_position_withdraws_the_spatial_status_everywhere(object(), None, web_ctx, None)
    assert flag["hidden"] is False and any("left the state after" in t for _tag, t in notes)
    # a spatial status that stays in the state after the hide fails the case
    monkeypatch.setattr(sp, "state_spatial", lambda anon, room="B": [spatial_row])
    with pytest.raises(AssertionError, match="still in the public state"):
        st.test_dp19b_2_hide_position_withdraws_the_spatial_status_everywhere(object(), None, web_ctx, None)


def test_the_account_store_swap_is_opt_in_and_restores_the_store(tmp_path, monkeypatch):
    fixture = st.accounts_down.__wrapped__
    store_dir = tmp_path / "sut-state" / "web"
    store_dir.mkdir(parents=True)
    store = store_dir / "accounts.jsonl"
    store.write_text("{}\n")
    target = SimpleNamespace(out=str(tmp_path))
    kills = []
    control = SimpleNamespace(kill_proc=lambda *a: kills.append(a))
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    monkeypatch.setattr(st.web, "WebClient", lambda *a, **k: SimpleNamespace(get=lambda path, pace=False: SimpleNamespace(status=200)))
    monkeypatch.delenv(st.SWITCH, raising=False)
    with pytest.raises(pytest.skip.Exception, match="opt-in and destructive"):
        next(fixture(target, control, "https://x", None))
    monkeypatch.setenv(st.SWITCH, "on")
    gen = fixture(target, control, "https://x", None)
    restart = next(gen)
    assert store.is_dir() and (store_dir / "accounts.jsonl.dvt-hold").is_file()                  # the store is now unreadable (a directory), the file held aside
    restart()
    assert kills == [("xlxd-sut", "dv-svc-web", "KILL")]
    with pytest.raises(StopIteration):
        next(gen)
    assert store.is_file() and store.read_text() == "{}\n" and not (store_dir / "accounts.jsonl.dvt-hold").exists() and len(kills) == 2        # restored, restarted again
    store.unlink()
    with pytest.raises(pytest.skip.Exception, match="no account store"):
        next(fixture(target, control, "https://x", None))


def test_the_dp19b_bodies_and_constants():
    assert st.SPATIAL_BODY == st.SPATIAL.lstrip(">") == "IO91SX/G" and st.PLAIN_BODY == "Ready" and st.TEXT == st.SPATIAL_BODY
    from dvtest.proto import aprs
    assert aprs.is_locator_status(st.SPATIAL) and aprs.is_locator_status(st.SPATIAL + " dpb") and not aprs.is_locator_status(st.PLAIN)
    for base in (st.BASE_VISIBLE, st.BASE_HIDE, st.BASE_QUIET, st.BASE_DOWN):
        assert len(base) == 5 and base.startswith("N0") and base.isupper()
    assert len({st.BASE_VISIBLE, st.BASE_HIDE, st.BASE_QUIET, st.BASE_DOWN}) == 4 and dp6c.ROOM == "B"
