# SPDX-License-Identifier: MIT
"""The pure parts of the Map fade cases (cases/map_fade_support.py, cases/test_web_map_fade.py): colour parsing and the fill fraction, the
duration reader, and the three judges against a MODEL of the page built from the page sources (fade step = floor((at - end) / 1000)
with `at` up to 1.25 s behind the server clock, levels 1 / .75 / .5 / .25 / 0, 1 s linear transitions; reduced motion: .45, no
transition). Offline: no browser. The model is the spec as read from web/site/map-layout.js and shared/dv.css of dv-router:wip-1bf347c,
not a measurement of a running page."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import map_fade_support as msf  # noqa: E402

pytestmark = pytest.mark.unit

ROOM, CALL = "N", "N0FAD"
LIT, REST = (255.0, 240.0, 196.0), (207.0, 216.0, 225.0)
LEVELS = [1.0, 0.75, 0.5, 0.25, 0.0]


def rgb(level):
    """color-mix(lit level, rest) as `color(srgb ...)`, the form Chromium and Firefox serialise."""
    return "color(srgb %s)" % " ".join("%.5f" % ((LIT[i] * level + REST[i] * (1 - level)) / 255.0) for i in range(3))


def rgb_text(level):
    return "rgb(%d, %d, %d)" % tuple(round(LIT[i] * level + REST[i] * (1 - level)) for i in range(3))


def model(t, lag=0.0, reduced=False, call=CALL, caption_ok=True):
    """The sample the page would give at server time `t` s since the end (t < 0: during the over)."""
    base = {"found": True, "wall_ms": t * 1000.0, "reduced": reduced, "hidden": False, "onair_rooms": 0, "fading_n": 0, "art_n": 0,
            "art_min": None, "art_max": None, "art_transition": [], "room_transition": "0s", "room_opacity": 1.0}
    te = t - lag
    if t < 0 or te < 0:
        base.update(state="onair", room_class="map-room is-onair", fill=rgb(1.0), onair_rooms=1, call_text=call, call_opacity=1.0,
                    caption="Scene: stations talking and listening. Room %s: %s talking from USRP for 1s, 0 listening. 6 desks open." % (ROOM, call))
        return base
    if te < 5.0:
        step = int(te)
        if reduced:
            level, art_tr, room_tr = 0.45, ["0s"], "0s"
        else:
            prev = LEVELS[step - 1] if step else 1.0
            level = prev + (LEVELS[step] - prev) * min(1.0, te - step)
            art_tr, room_tr = ["1s"], "1s"
        base.update(state="onair", room_class="map-room is-onair twin-fading twin-fade-%d" % step, fill=rgb(level), onair_rooms=1, fading_n=6,
                    art_n=5, art_min=level, art_max=level, art_transition=art_tr, room_transition=room_tr, call_text=call, call_opacity=level,
                    caption=("Scene: transmissions just ended. No one is on air. Room %s: %s finished." % (ROOM, call)) if caption_ok else
                    "Scene: transmissions just ended. No one is on air.")
        return base
    base.update(state="idle", room_class="map-room is-idle", fill=rgb(0.0), caption="Scene: quiet night. No one is on air. 6 desks open.")
    return base


def series_for(lag=0.0, reduced=False, start=-3.0, stop=8.6, **kw):
    out, t = [], start
    while t <= stop:
        out.append(model(round(t, 3), lag, reduced, **kw))
        t += 0.25
    return msf.annotate(out, 0.0, 0.0)


# ---------------------------------------------------------------- colours and durations

def test_parse_color_forms():
    assert msf.parse_color("rgb(207, 216, 225)") == (207.0, 216.0, 225.0)
    assert msf.parse_color("rgba(207, 216, 225, 1)") == (207.0, 216.0, 225.0)
    assert msf.parse_color("rgb(207 216 225 / 0.5)") == (207.0, 216.0, 225.0)
    r, g, b = msf.parse_color("color(srgb 0.8118 0.8471 0.8824)")
    assert (round(r), round(g), round(b)) == (207, 216, 225)
    assert msf.parse_color("color(srgb 0.5 0.5 0.5 / 0.5)") == (127.5, 127.5, 127.5)
    assert msf.parse_color("oklab(0.9 0 0)") is None
    assert msf.parse_color("") is None and msf.parse_color(None) is None


def test_fill_fraction_endpoints_and_middle():
    lit, rest = rgb_text(1.0), rgb_text(0.0)
    assert msf.fill_fraction(lit, lit, rest) == pytest.approx(0.0, abs=1e-6)
    assert msf.fill_fraction(rest, lit, rest) == pytest.approx(1.0, abs=1e-6)
    assert msf.fill_fraction(rgb(0.5), lit, rest) == pytest.approx(0.5, abs=0.02)
    assert msf.fill_fraction("rgb(1, 2, 3)", lit, lit) is None        # identical references cannot be told apart
    assert msf.fill_fraction("oklab(0.9 0 0)", lit, rest) is None


def test_durations_zero():
    for text in ("0s", "0s, 0s", "0ms", "", "0s,0s,0s"):
        assert msf.durations_zero(text), text
    for text in ("1s", "0s, 1s", "250ms", "0.5s"):
        assert not msf.durations_zero(text), text
    assert msf.all_zero(["0s", "0s, 0s"]) and not msf.all_zero(["0s", "1s"])


def test_tile_call_matches_truncation():
    assert msf.tile_call_matches("N0FAD", "N0FAD")
    assert msf.tile_call_matches("N0FA.", "N0FAD")
    assert not msf.tile_call_matches("N0X.", "N0FAD")
    assert not msf.tile_call_matches("N.", "N0FAD")             # too short to be a callsign
    assert not msf.tile_call_matches(None, "N0FAD")


def test_model_sanity():
    assert msf.is_onair_look(model(-1.0)) and not msf.is_fading(model(-1.0))
    s = model(2.5)
    assert msf.is_fading(s) and msf.step_of(s) == 2
    assert msf.finished_caption(s, ROOM, CALL)
    assert msf.is_rest(model(6.6))


# ---------------------------------------------------------------- judge_fade

@pytest.mark.parametrize("lag", [0.0, 0.4, 0.9, 1.25])
def test_judge_fade_accepts_the_page_at_any_documented_lag(lag):
    fail, metrics = msf.judge_fade(series_for(lag), ROOM, CALL)
    assert fail == [], fail
    assert metrics["fade_first_t"] <= msf.LAG_S + msf.SAMPLE_S + msf.START_TOL_S
    assert metrics["mid"]["step"] in (1, 2)
    assert metrics["steps_seen"][0] in (0, 1) and metrics["steps_seen"][-1] == 4


def test_judge_fade_fails_without_a_fade():
    series = [s for s in series_for(0.0) if not msf.is_fading(s)]
    fail, _m = msf.judge_fade(series, ROOM, CALL)
    assert any("no sample shows a fade class" in f for f in fail), fail


def test_judge_fade_fails_when_the_fade_is_late():
    fail, _m = msf.judge_fade(series_for(2.5), ROOM, CALL)
    assert any("the fade starts at" in f for f in fail), fail
    assert any("not at rest" in f or "lasts" in f for f in fail), fail


def test_judge_fade_fails_without_the_finished_caption():
    fail, _m = msf.judge_fade(series_for(0.0, caption_ok=False), ROOM, CALL)
    assert any("lack the caption" in f for f in fail), fail


def test_judge_fade_fails_when_rest_is_never_reached():
    series = [s for s in series_for(0.0) if s["t"] < 6.0]
    fail, _m = msf.judge_fade(series, ROOM, CALL)
    assert any("rest cannot be judged" in f for f in fail), fail


def test_judge_fade_fails_when_the_room_stays_dim_at_rest():
    series = series_for(0.0)
    for s in series:
        if s["t"] >= 6.5:
            s.update(state="onair", room_class="map-room is-onair twin-fading twin-fade-4", fading_n=3)
    fail, _m = msf.judge_fade(series, ROOM, CALL)
    assert any("not at rest" in f for f in fail), fail


def test_judge_fade_fails_without_the_talker_on_the_air():
    series = series_for(0.0)
    for s in series:
        if s["t"] < 0:
            s["caption"] = "Scene: quiet night. No one is on air."
            s["call_text"] = None
    fail, _m = msf.judge_fade(series, ROOM, CALL)
    assert any("caption never says" in f for f in fail), fail
    assert any("not drawn on the room tile" in f for f in fail), fail


def test_judge_fade_flags_a_fade_that_goes_back():
    series = series_for(0.0)
    for s in series:
        if 3.0 <= s["t"] < 3.3:
            s["room_class"] = "map-room is-onair twin-fading twin-fade-1"
    fail, _m = msf.judge_fade(series, ROOM, CALL)
    assert any("goes backwards" in f for f in fail), fail


# ---------------------------------------------------------------- judge_restore

def restart_series(restore_after=0.4, dimmed_after=False):
    """A fade of the first over (server time t_a since its end), the second over's first frame at t_a = 1.6 s (u = 0), then the page: still
    the fading look until `restore_after`, the full look after it (`dimmed_after`: a dimmed sample 0.6 s longer)."""
    out = []
    t_a = 0.5
    while t_a <= 5.0:
        u = round(t_a - 1.6, 3)
        if u < restore_after:
            sample = model(t_a, 0.0)
        elif dimmed_after and u < restore_after + 0.6:
            sample = model(t_a, 0.0)
        else:
            sample = model(-1.0)
        out.append(dict(sample, wall_ms=t_a * 1000.0, t=t_a, u=u))
        t_a += 0.25
    return out


def test_judge_restore_accepts_an_immediate_restore():
    series = restart_series(0.4)
    fail, metrics = msf.judge_restore(series, ROOM, CALL, rgb_text(1.0), rgb_text(0.0), 3.0)
    assert fail == [], fail
    assert metrics["restored_u"] <= msf.RESTORE_S


def test_judge_restore_fails_when_the_restore_is_late():
    fail, _m = msf.judge_restore(restart_series(2.0), ROOM, CALL, rgb_text(1.0), rgb_text(0.0), 3.5)
    assert any("back" in f and "wanted within" in f for f in fail), fail


def test_judge_restore_fails_on_a_dimmed_intermediate():
    """The class is back to plain on-air but the fill (or the talker text) is still on its way up: not 'at once'."""
    series = restart_series(0.4)
    first = next(s for s in series if s["u"] >= 0.4)
    first["fill"] = rgb_text(0.5)
    fail, _m = msf.judge_restore(series, ROOM, CALL, rgb_text(1.0), rgb_text(0.0), 3.5)
    assert any("room fill" in f for f in fail), fail
    series = restart_series(0.4)
    next(s for s in series if s["u"] >= 0.4)["call_opacity"] = 0.6
    fail, _m = msf.judge_restore(series, ROOM, CALL, rgb_text(1.0), rgb_text(0.0), 3.5)
    assert any("talker text is dimmed" in f for f in fail), fail


def test_judge_restore_fails_when_the_fade_class_comes_back():
    series = restart_series(0.4)
    late = [s for s in series if s["u"] >= 1.0][0]
    late.update(room_class="map-room is-onair twin-fading twin-fade-2", fading_n=4, art_n=3, art_min=0.5)
    fail, _m = msf.judge_restore(series, ROOM, CALL, rgb_text(1.0), rgb_text(0.0), 3.5)
    assert any("fade class or fading element is back" in f for f in fail), fail


def test_judge_restore_needs_a_fade_before_the_new_over():
    series = [model(-1.0)] * 6
    for i, s in enumerate(series):
        series[i] = dict(s, u=i * 0.25 - 0.5, t=i * 0.25 - 0.5, wall_ms=i * 250.0)
    fail, _m = msf.judge_restore(series, ROOM, CALL, rgb_text(1.0), rgb_text(0.0), 3.0)
    assert any("did not start inside a fade" in f for f in fail), fail


# ---------------------------------------------------------------- judge_reduced

def test_judge_reduced_accepts_a_static_dimmed_state():
    fail, metrics = msf.judge_reduced(series_for(0.3, reduced=True), ROOM, CALL)
    assert fail == [], fail
    assert metrics["art_levels"] == [(0.45, 0.45)]


def test_judge_reduced_fails_with_a_transition():
    series = series_for(0.3, reduced=True)
    for s in series:
        if msf.is_fading(s):
            s["art_transition"] = ["1s"]
    fail, _m = msf.judge_reduced(series, ROOM, CALL)
    assert any("CSS transition" in f for f in fail), fail


def test_judge_reduced_fails_when_the_page_still_steps_the_opacity():
    fail, _m = msf.judge_reduced(series_for(0.3, reduced=False), ROOM, CALL)
    assert any("prefers-reduced-motion" in f for f in fail), fail
    assert any("CSS transition" in f or "not the static" in f for f in fail), fail
