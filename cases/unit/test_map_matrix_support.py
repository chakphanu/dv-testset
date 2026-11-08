# SPDX-License-Identifier: MIT
"""The pure parts of the TWIN1d Map cases (cases/map_matrix_support.py): the matrix judge (overflow, clipped caption, closed desk sign
in EN and TH), the closed-desk rewrite of the public state, the combination names, and the judge of the map-status change log.
Offline: no browser. The measurement shapes are those MEASURE_JS returns, built by hand."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import map_matrix_support as mms  # noqa: E402
import web_pages as pages  # noqa: E402

pytestmark = pytest.mark.unit

TH_CLOSED = "\u0e1b\u0e34\u0e14"
TH_CAPTION = "\u0e2a\u0e16\u0e32\u0e19\u0e01\u0e32\u0e23\u0e13\u0e4c"


def good(lang="en", scheme="light", **over):
    m = {"doc_scroll": 360, "doc_client": 360, "inner": 360, "lang": lang, "dark": scheme == "dark", "items": [], "svg_texts": [],
         "closed_texts": [], "closed_desks": 0, "caption": TH_CAPTION if lang == "th" else "Scene: quiet night.", "svg": {"left": 0, "right": 360}}
    m.update(over)
    return m


def test_a_clean_measurement_passes_with_a_note_about_the_closed_sign():
    failures, notes = mms.judge_measure(good(), "en", "light")
    assert failures == []
    assert any("closed sign was not checked" in n for n in notes)


def test_horizontal_overflow_is_a_failure():
    failures, _ = mms.judge_measure(good(doc_scroll=420), "en", "light")
    assert any("horizontal overflow" in f and "420" in f for f in failures)


def test_html_caption_wider_than_its_box_is_clipped_or_overflowing():
    clipped = {"desc": "p[map-caption]", "sw": 500, "cw": 300, "display": "block", "overflow_x": "hidden", "text_overflow": "ellipsis", "left": 8, "right": 308}
    spill = dict(clipped, overflow_x="visible", text_overflow="clip", desc="span[legend]")
    failures, _ = mms.judge_measure(good(items=[clipped, spill]), "en", "light")
    assert any("clipped" in f and "map-caption" in f for f in failures)
    assert any("overflows its box" in f and "legend" in f for f in failures)


def test_one_pixel_of_slack_and_inline_elements_pass():
    fits = {"desc": "p", "sw": 301, "cw": 300, "display": "block", "overflow_x": "hidden", "text_overflow": "clip", "left": 8, "right": 308}
    inline = {"desc": "a", "sw": 90, "cw": 0, "display": "inline", "overflow_x": "visible", "text_overflow": "clip", "left": 10, "right": 100}
    failures, _ = mms.judge_measure(good(items=[fits, inline]), "en", "light")
    assert failures == []


def test_an_element_outside_the_viewport_fails_even_when_inline():
    out = {"desc": "a", "sw": 90, "cw": 0, "display": "inline", "overflow_x": "visible", "text_overflow": "clip", "left": 300, "right": 420}
    failures, _ = mms.judge_measure(good(items=[out]), "en", "light")
    assert any("outside the viewport" in f for f in failures)


def test_svg_text_must_stay_inside_its_shape_and_the_drawing():
    box = {"l": 100, "r": 160, "t": 0, "b": 40, "d": "rect[map-desk]"}
    ok = {"text": "DMR", "l": 105, "r": 150, "in": box}
    wide = {"text": "Switchboard", "l": 95, "r": 175, "in": box}
    off = {"text": "X", "l": 340, "r": 380, "in": None}
    failures, _ = mms.judge_measure(good(svg_texts=[ok, wide, off]), "en", "light")
    assert len(failures) == 2
    assert any("Switchboard" in f and "map-desk" in f for f in failures)
    assert any("outside the drawing" in f for f in failures)


def test_scheme_and_language_must_be_applied():
    failures, _ = mms.judge_measure(good(lang="en", scheme="light"), "th", "dark")
    assert any("colour scheme dark was not applied" in f for f in failures)
    assert any("document language is 'en', wanted 'th'" in f for f in failures)
    assert any("TH: the caption has no Thai text" in f for f in failures)
    failures, _ = mms.judge_measure(good(lang="th", caption=TH_CAPTION), "en", "light")
    assert any("EN: the caption has Thai text" in f for f in failures)


@pytest.mark.parametrize("lang,sign,ok", [("th", TH_CLOSED, True), ("th", "Closed", False), ("en", "Closed", True), ("en", TH_CLOSED, False)])
def test_closed_desk_sign_text_per_language(lang, sign, ok):
    failures, notes = mms.judge_measure(good(lang=lang, closed_desks=1, closed_texts=[sign]), lang, "light")
    assert (failures == []) is ok
    assert not any("closed sign was not checked" in n for n in notes)


def test_closed_desk_without_sign_text_and_the_forced_case():
    failures, _ = mms.judge_measure(good(lang="th", closed_desks=2, closed_texts=[]), "th", "light")
    assert any("no closed sign text" in f for f in failures)
    failures, _ = mms.judge_measure(good(), "en", "light", closed_required=True)
    assert any("no closed desk in the drawing" in f for f in failures)


def test_thai_closed_string_is_the_table_value():
    assert pages.CLOSED_SIGN_TEXT["th"] == TH_CLOSED
    assert pages.CLOSED_SIGN_TEXT["en"] == "Closed"
    assert all(ord(c) < 128 for c in open(mms.__file__, encoding="utf-8").read())      # the source stays ASCII


def test_combinations_and_names():
    combos = mms.all_combos("map26")
    assert len(combos) == 12 and len(mms.SCENARIOS) * len(combos) == 48
    assert mms.shot_name("map-busy", "th", "dark", 360) == "map-busy-th-dark-360.png"
    assert mms.combo_id("map26", "en", "light", 1440) == "map26/en/light/1440"


# ------------------------------------------------------------------ the closed-desk rewrite

def state():
    return {"info": {"edges_enabled": ["dextra", "dcs", "ysf", "usrp", "webrtc"]},
            "live": {"edges": ["dextra-0123456789abcdef", "dcs", "ysf-fedcba9876543210", "usrp", "webrtc-abc"], "edges_up": 5}}


def test_close_one_edge_picks_the_last_closable_enabled_edge():
    out, pick = mms.close_one_edge(state())
    assert pick == "ysf"
    assert out["live"]["edges"] == ["dextra-0123456789abcdef", "dcs", "usrp", "webrtc-abc"]
    assert out["live"]["edges_up"] == 4


def test_close_one_edge_keeps_a_chosen_edge_and_never_closes_usrp_or_webrtc():
    _, pick = mms.close_one_edge(state(), chosen="dextra")
    assert pick == "dextra"
    only = {"info": {"edges_enabled": ["usrp", "webrtc"]}, "live": {"edges": ["usrp", "webrtc-x"]}}
    out, pick = mms.close_one_edge(only)
    assert pick is None and out["live"]["edges"] == ["usrp", "webrtc-x"]


def test_close_one_edge_leaves_an_unexpected_shape_alone():
    assert mms.close_one_edge({"live": {}}) == ({"live": {}}, None)
    assert mms.close_one_edge([]) == ([], None)


# ------------------------------------------------------------------ the status region judge

T0 = 1_000_000.0          # ms, the first voice packet
UNKEY = T0 + 6000.0
END = UNKEY + 7500.0


def change(t_s, text, base=T0):
    return {"t": base + t_s * 1000.0, "from": "", "text": text}


def log(changes, attaches=None, rewrites=()):
    return {"attaches": attaches if attaches is not None else [{"t": T0 - 9000.0, "text": ""}], "changes": changes, "rewrites": list(rewrites), "batches": len(changes)}


def good_log():
    return log([change(1.0, "Room I: N0MST on air."), change(6.8, "Room I: N0MST finished.")])


def test_one_change_at_start_and_one_at_end_passes():
    failures, notes, metrics = mms.judge_status_log(good_log(), T0, UNKEY, END)
    assert failures == [] and notes == []
    assert [c["t_s"] for c in metrics["changes"]] == [1.0, 6.8]


def test_first_render_must_be_empty_and_idle_must_be_silent():
    bad = log([change(-3.0, "Room I: hello"), change(1.0, "a"), change(6.8, "b")], attaches=[{"t": T0 - 9000.0, "text": "Welcome"}])
    failures, _, _ = mms.judge_status_log(bad, T0, UNKEY, END)
    assert any("not empty on first render" in f for f in failures)
    assert any("before the over started" in f for f in failures)


def test_a_change_per_second_of_steady_voice_fails():
    chatty = log([change(1.0, "on air"), change(2.5, "1s"), change(3.5, "2s"), change(4.5, "3s"), change(5.0, "4s"), change(6.8, "end")])
    failures, _, _ = mms.judge_status_log(chatty, T0, UNKEY, END)
    assert any("during steady voice" in f for f in failures)
    assert any("apart (the limit is one per 2 s)" in f for f in failures)


def test_missing_start_or_end_and_changes_during_the_fade_fail():
    failures, _, _ = mms.judge_status_log(log([change(6.8, "end")]), T0, UNKEY, END)
    assert any("0 change(s) of the region at the over start" in f for f in failures)
    failures, _, _ = mms.judge_status_log(log([change(1.0, "start")]), T0, UNKEY, END)
    assert any("0 change(s) of the region at the over end" in f for f in failures)
    failures, _, _ = mms.judge_status_log(log([change(1.0, "start"), change(6.8, "end"), change(10.5, "again")]), T0, UNKEY, END)
    assert any("after the end announcement" in f for f in failures)


def test_two_starts_within_the_window_fail_and_rewrites_are_only_notes():
    failures, _, _ = mms.judge_status_log(log([change(0.5, "a"), change(3.0, "b"), change(6.8, "c")]), T0, UNKEY, END)
    assert any("2 change(s) of the region at the over start" in f for f in failures)
    failures, notes, _ = mms.judge_status_log(log([change(1.0, "a"), change(6.8, "b")], rewrites=[{"t": T0 + 2000.0, "text": "a"}]), T0, UNKEY, END)
    assert failures == [] and any("same text" in n for n in notes)


def test_no_region_at_all():
    failures, _, _ = mms.judge_status_log({"attaches": [], "changes": []}, T0, UNKEY, END)
    assert failures == ["the region never appeared in the DOM"]


def facts(**over):
    f = {"found": True, "count": 1, "role": "status", "live": "polite", "display": "block", "visibility": "visible", "hidden_chain": False,
         "cls": ["sr-only"], "w": 1, "h": 1, "text": ""}
    f.update(over)
    return f


def test_status_facts():
    assert mms.judge_status_facts(facts()) == []
    assert any("not in the DOM" in f for f in mms.judge_status_facts({"found": False}))
    bad = mms.judge_status_facts(facts(role="alert", live="assertive", display="none", cls=["x"], count=2))
    assert len(bad) == 5
    assert any("accessibility tree" in f for f in mms.judge_status_facts(facts(visibility="hidden")))
    assert any("accessibility tree" in f for f in mms.judge_status_facts(facts(hidden_chain=True)))


# ------------------------------------------------------------------ SIGSTOP / SIGCONT helpers

def test_edge_listed_cuts_the_instance_suffix():
    assert mms.edge_listed(state(), "ysf") and mms.edge_listed(state(), "dextra") and mms.edge_listed(state(), "dcs")
    assert not mms.edge_listed(state(), "xlx")
    gone, _ = mms.close_one_edge(state())
    assert not mms.edge_listed(gone, "ysf")
    assert not mms.edge_listed({}, "ysf") and not mms.edge_listed({"live": {"edges": None}}, "ysf") and not mms.edge_listed([], "ysf")


def test_poll_until_returns_the_elapsed_time_or_none():
    now = [0.0]
    calls = []

    def clock():
        return now[0]

    def sleep(s):
        now[0] += s

    def pred():
        calls.append(now[0])
        if len(calls) == 1:
            raise RuntimeError("a dead hub counts as not yet")
        return now[0] >= 6.0

    assert mms.poll_until(pred, 30.0, every_s=2.0, clock=clock, sleep=sleep) == 6.0
    now[0] = 0.0
    assert mms.poll_until(lambda: False, 5.0, every_s=2.0, clock=clock, sleep=sleep) is None
    assert now[0] >= 5.0
    assert mms.poll_until(lambda: True, 5.0, clock=clock, sleep=sleep) == 0.0


class FakeRig:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def kill_proc(self, service, process, signal):
        self.calls.append((service, process, signal))
        if process in self.fail:
            raise RuntimeError("error 3")


def test_cont_all_sends_cont_twice_to_both_processes_and_never_raises():
    rig = FakeRig()
    assert mms.cont_all(rig) == 4
    assert rig.calls == [("xlxd-sut", "dv-edge-xlx", "CONT"), ("xlxd-sut", "dv-svc-status", "CONT")] * 2
    rig = FakeRig(fail={"dv-edge-xlx"})
    assert mms.cont_all(rig) == 2                       # the hub still got both, the edge failures were swallowed
    assert sum(1 for c in rig.calls if c[1] == "dv-edge-xlx") == 2


def test_the_stoppable_processes_are_allow_listed_for_stop_and_cont():
    from dvtest import control
    for proc in mms.STOPPABLE:
        for sig in ("STOP", "CONT"):
            assert control.kill_proc_allowed(mms.SERVICE, proc, sig)
    assert mms.CLOSED_SCENARIO not in mms.SCENARIOS
