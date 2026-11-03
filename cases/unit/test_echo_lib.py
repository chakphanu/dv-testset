# SPDX-License-Identifier: MIT
"""dvtest/echo.py (pure helpers of cases/test_echo.py) and the rig snippets echo*.toml."""
import tomllib
from pathlib import Path

import pytest

from dvtest import clients, echo, sut_config
from dvtest.audio import signals
from dvtest.proto import dextra, dmr, m17, ysf

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"

# a tiny fictional template, not a copy of the real one
TEMPLATE = """\
schema = 1
[node]
callsign = "XLX999"
bind_ip = "<OWN_IP>"
rooms = "A-Z"

[log]
level = "info"
"""


def snippet(name):
    return (SNIPPETS / name).read_text(encoding="utf-8")


def lines(text, key):
    """Value lines `key = ...` of a text (comments do not count)."""
    return [x for x in text.split("\n") if x.split("=")[0].strip() == key and "=" in x]


def headers(text, name="echo"):
    return [x for x in text.split("\n") if x.strip() == "[%s]" % name]


def rendered(*names):
    return sut_config.render(TEMPLATE, "172.30.0", extras=[snippet(n) for n in names])


# ---------------------------------------------------------------- snippets

def test_echo_snippet_switches_module_e_on_with_defaults():
    got = echo.echo_settings(rendered("echo.toml"))
    assert got == {"max_seconds": 30, "delay_ms": 1000, "modules": {"E": True}}


def test_template_alone_has_no_echo():
    assert echo.echo_settings(sut_config.render(TEMPLATE, "172.30.0")) is None


def test_short_variant_merges_into_the_table():
    out = rendered("echo.toml", "echo-short.toml")
    assert len(headers(out)) == 1
    assert echo.echo_settings(out) == {"max_seconds": 5, "delay_ms": 1000, "modules": {"E": True}}


def test_off_variant_keeps_the_row_but_switches_it_off():
    out = rendered("echo.toml", "echo-off.toml")
    assert len(headers(out)) == 1 and len(lines(out, "modules")) == 1
    assert echo.echo_settings(out)["modules"] == {"E": False}


def test_peers_variant_adds_module_m():
    out = rendered("echo.toml", "echo-peers.toml")
    assert len(lines(out, "modules")) == 1
    assert echo.echo_settings(out)["modules"] == {"E": True, "M": True}


def test_variants_stack_in_any_order_of_the_keys():
    out = rendered("echo.toml", "echo-short.toml", "echo-peers.toml")
    assert echo.echo_settings(out) == {"max_seconds": 5, "delay_ms": 1000, "modules": {"E": True, "M": True}}


@pytest.mark.parametrize("name", ["echo.toml", "echo-short.toml", "echo-off.toml", "echo-peers.toml"])
def test_snippets_are_ascii_and_licensed(name):
    text = snippet(name)
    text.encode("ascii")
    assert text.startswith("# SPDX-License-Identifier: MIT")
    assert "<" not in text.replace("<=", "")          # no tokens: nothing to substitute
    tomllib.loads(text)


def test_echo_modules_are_served_by_the_rig_rooms():
    rooms = tomllib.loads(rendered("echo.toml", "echo-peers.toml"))["node"]["rooms"]
    assert rooms == "A-Z"
    for letter in echo.echo_settings(rendered("echo.toml", "echo-peers.toml"))["modules"]:
        assert letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------- editing the rendered file

def test_edit_modules_off_and_back():
    base = rendered("echo.toml")
    off = echo.edit_echo(base, modules={"E": False})
    assert echo.echo_settings(off)["modules"] == {"E": False}
    assert echo.edit_echo(off, modules={"E": True}) == base


def test_edit_removes_every_row():
    out = echo.edit_echo(rendered("echo.toml"), modules={})
    assert echo.echo_settings(out)["modules"] == {}
    assert "modules = {}" in out


def test_edit_adds_an_absent_key_and_changes_a_present_one():
    base = rendered("echo.toml")
    five = echo.edit_echo(base, max_seconds=5)
    assert echo.echo_settings(five)["max_seconds"] == 5 and len(lines(five, "max_seconds")) == 1
    six = echo.edit_echo(five, max_seconds=6, delay_ms=1500)
    assert echo.echo_settings(six) == {"max_seconds": 6, "delay_ms": 1500, "modules": {"E": True}}
    back = echo.edit_echo(six, max_seconds=None, delay_ms=None)
    assert back == base


def test_edit_keeps_everything_else():
    base = rendered("echo.toml") + "\n[web]\nenable = false  # comment\n"
    out = echo.edit_echo(base, modules={"E": False})
    assert out.replace("{ E = false }", "{ E = true }") == base
    assert "enable = false  # comment" in out


def test_edit_keeps_the_comment_lines_of_the_table():
    base = "# about echo\n[echo]\n# about modules\nmodules = { E = true }\n"
    out = echo.edit_echo(base, modules={"E": False})
    assert out == "# about echo\n[echo]\n# about modules\nmodules = { E = false }\n"


def test_edit_refuses_without_a_table_or_with_a_multiline_value():
    with pytest.raises(ValueError):
        echo.edit_echo(sut_config.render(TEMPLATE, "172.30.0"), modules={"E": True})
    with pytest.raises(ValueError):
        echo.edit_echo('[echo]\nmodules = {\n  E = true }\n', modules={})


def test_modules_value_forms():
    assert echo.modules_value({}) == "{}"
    assert echo.modules_value({"E": True, "M": False}) == "{ E = true, M = false }"


def test_settings_of_a_non_toml_text():
    with pytest.raises(ValueError):
        echo.echo_settings("[echo\nx")


def test_reload_wait_covers_two_stable_polls():
    assert echo.reload_wait() == 4.0
    assert echo.reload_wait(1.0, 2, 0.0) == 3.0


# ---------------------------------------------------------------- telemetry

METRICS = """\
# HELP dv_echo_overs_total Echo overs by outcome
# TYPE dv_echo_overs_total counter
dv_echo_overs_total{room="e",result="played"} 3
dv_echo_overs_total{result="short",room="e"} 2
dv_echo_overs_total{room="m",result="played"} 1
dv_echo_frames_total{room="e",dir="in"} 900
"""


def test_echo_overs_by_room_and_result_any_label_order():
    assert echo.echo_overs(METRICS, "E", "played") == 3
    assert echo.echo_overs(METRICS, "e", "short") == 2
    assert echo.echo_overs(METRICS, "M", "played") == 1
    assert echo.echo_overs(METRICS, "E", "truncated") == 0
    assert echo.echo_overs("", "E", "played") == 0


def test_room_label_spelling():
    assert echo.room_label_cases(METRICS) == {"lower"}
    assert echo.room_label_cases('dv_echo_overs_total{room="E",result="played"} 1\n') == {"upper"}
    assert echo.room_label_cases("") == set()


# ---------------------------------------------------------------- events per protocol

def _times(n, start=10.0, step=0.02):
    return [start + i * step for i in range(n)]


def test_dstar_events_header_voice_end():
    frames = signals.prng_frames("dstar", 4, seed="e1")
    sid = 0x1234
    packets = [dextra.header(sid, "N0CALL", "E")]
    packets += [dextra.voice(sid, i, f, False) for i, f in enumerate(frames)]
    packets.append(dextra.voice(sid, 4, bytes(9), True))
    packets.insert(2, b"\x00" * 9)                          # a keepalive is left out
    evs = echo.events("dstar", packets, _times(len(packets)))
    assert [e.kind for e in evs] == ["header", "voice", "voice", "voice", "voice", "end"]
    assert evs[0].ident == "N0CALL" and evs[0].sid == sid
    assert [f for e in evs for f in e.frames] == frames


def test_dmr_events_voice_and_control():
    frames = signals.prng_frames("dmr", 6, seed="e2")
    bursts, seq = dmr.voice_packets(frames, 1023007, 4005, 0x22222222)
    head = dmr.packet(0, 1023007, 4005, 1023007, 0xA1, 0x22222222, dmr.data_sync_payload())
    term = dmr.packet(seq, 1023007, 4005, 1023007, 0xA2, 0x22222222, dmr.data_sync_payload())
    packets = [head] + bursts + [term]
    evs = echo.events("dmr", packets, _times(len(packets), step=0.06))
    assert evs[0].kind == "ctrl" and evs[-1].kind == "ctrl"
    assert [f for e in evs for f in e.frames] == frames
    assert {e.ident for e in evs} == {1023007}


def test_m17_events_include_the_eot_pair():
    frames = signals.prng_frames("m17", 6, seed="e3")
    packets = m17.voice_packets(frames, 0x4242, "N0CALL")
    evs = echo.events("m17", packets, _times(len(packets), step=0.04))
    assert evs[-1].kind == "end" and evs[-1].frames and evs[0].kind == "voice"
    assert evs[0].ident == "N0CALL"
    assert [f for e in evs for f in e.frames] == echo.expected_frames("m17", frames)


def test_ysf_events_header_voice_terminator():
    frames = echo.make_frames("ysf", 10, "e4")
    packets = ysf.stream_packets(frames, "N0CALL", dgid=5)
    evs = echo.events("ysf", packets, _times(len(packets), step=0.1))
    assert [e.kind for e in evs] == ["header", "voice", "voice", "end"]
    assert [f for e in evs for f in e.frames] == echo.expected_frames("ysf", frames)
    assert {e.ident for e in evs} == {"N0CALL"}


def test_expected_frames_per_kind():
    ds = signals.prng_frames("dstar", 7, seed="x")
    assert echo.expected_frames("dstar", ds) == ds
    dm = echo.expected_frames("dmr", ds)
    assert len(dm) == 9 and dm[:7] == ds and dm[7:] == [bytes(9)] * 2
    assert len(echo.expected_frames("ysf", ds)) == 10
    m = signals.prng_frames("m17", 4, seed="y")
    assert len(echo.expected_frames("m17", m)) == 8        # packet 0 repeats frame 0, pairs, EOT repeats the last pair
    assert echo.expected_frames("m17", m)[:3] == [m[0]] * 3 and echo.expected_frames("m17", m)[3:6] == m[1:]


def test_events_ignore_other_packets():
    assert echo.events("dstar", [b"", b"abc", b"DSVT"], [1, 2, 3]) == []
    assert echo.events("dmr", [b"RPTACK"], [1]) == []
    assert echo.events("m17", [b"PING"], [1]) == []
    assert echo.events("ysf", [b"YSFPxxxxxxxx"], [1]) == []


# ---------------------------------------------------------------- overs and comparison

def _dstar_over(sid, frames, t0, ident="N0CALL"):
    packets = [dextra.header(sid, ident, "E")]
    packets += [dextra.voice(sid, i, f, False) for i, f in enumerate(frames)]
    packets.append(dextra.voice(sid, len(frames), bytes(9), True))
    return packets, _times(len(packets), start=t0)


def test_split_overs_finds_the_replay_a_second_later():
    frames = signals.prng_frames("dstar", 150, seed="s")
    p1, t1 = _dstar_over(1, frames, 10.0)
    p2, t2 = _dstar_over(2, frames, t1[-1] + 1.0)
    evs = echo.events("dstar", p1 + p2, t1 + t2)
    overs = echo.split_overs(evs)
    assert len(overs) == 2
    first, second = overs
    assert first.sids == [1] and second.sids == [2]
    assert first.n_voice == 150 and first.frames == frames and first.header and first.ended
    assert second.start - first.end == pytest.approx(1.0, abs=0.001)
    assert echo.duration(first) == pytest.approx(2.98, abs=0.001)
    assert echo.after(overs, first.end) == [second]
    assert second.idents == ["N0CALL"]


def test_split_overs_one_stream_stays_one_over():
    frames = signals.prng_frames("dstar", 60, seed="s2")
    p, t = _dstar_over(3, frames, 5.0)
    assert len(echo.split_overs(echo.events("dstar", p, t))) == 1


def test_split_overs_unended_stream_and_empty():
    frames = signals.prng_frames("dstar", 10, seed="s3")
    p, t = _dstar_over(4, frames, 5.0)
    over = echo.split_overs(echo.events("dstar", p[:-1], t[:-1]))[0]
    assert not over.ended and over.n_voice == 10
    assert echo.split_overs([]) == []
    assert echo.duration(echo.split_overs(echo.events("dstar", p[:1], t[:1]))[0]) is None


def test_compare_payload_cases():
    a = [bytes([i]) * 9 for i in range(5)]
    same = echo.compare_payload(a, a)
    assert same.identical and same.prefix_ok and same.first_diff is None and same.extra == [] and same.missing == 0
    longer = echo.compare_payload(a, a + [bytes(9)])
    assert not longer.identical and longer.prefix_ok and longer.extra == [bytes(9)]
    shorter = echo.compare_payload(a, a[:3])
    assert not shorter.prefix_ok and shorter.first_diff == 3 and shorter.missing == 2
    bad = list(a)
    bad[2] = b"x" * 9
    changed = echo.compare_payload(a, bad)
    assert not changed.prefix_ok and changed.first_diff == 2 and changed.extra == []


def test_window_and_lengths():
    assert echo.in_window(1.0, 0.8, 2.0) and not echo.in_window(2.1, 0.8, 2.0) and not echo.in_window(None, 0, 1)
    assert echo.over_seconds(150) == pytest.approx(3.0)
    assert echo.retained_frames(5) == 250 and echo.retained_frames(30) == 1500
    assert echo.valid_module("E") and not echo.valid_module("e") and not echo.valid_module("")


def test_clients_kinds_used_by_the_cases_exist():
    for kind in ("dstar", "dmr", "m17", "ysf"):
        assert kind in clients.KINDS + clients.EXTRA_KINDS
        assert kind in echo._DECODERS


@pytest.mark.parametrize("kind", ["dstar", "dmr", "m17", "ysf"])
def test_make_frames_are_deterministic_and_the_right_width(kind):
    a = echo.make_frames(kind, 12, "seed")
    assert a == echo.make_frames(kind, 12, "seed") and a != echo.make_frames(kind, 12, "other")
    assert len(a) == 12 and {len(f) for f in a} == {8 if kind == "m17" else 9}


def test_ysf_frames_round_trip_through_the_vch_coder():
    frames = echo.make_frames("ysf", 5, "rt")
    packets = ysf.stream_packets(frames, "N0CALL", dgid=1)
    got = [f for e in echo.events("ysf", packets, _times(len(packets))) for f in e.frames]
    assert got == frames
