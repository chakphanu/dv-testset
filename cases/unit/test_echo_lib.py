# SPDX-License-Identifier: MIT
"""dvtest/echo.py (pure helpers of cases/test_echo.py) and the rig snippets echo*.toml."""
import random
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


def test_usrp_peer_snippet_adds_a_static_endpoint_row():
    out = sut_config.render(TEMPLATE, "172.30.0", extras=[snippet("echo.toml"), snippet("echo-usrp-peer.toml")])
    row = tomllib.loads(out)["edges"]["usrp"]["endpoint"]
    assert row == [{"module": "E", "peer": "172.30.0.30:32003", "callsign": "N0TEST", "bind": "172.30.0.11:34003"}]
    assert echo.echo_settings(out)["modules"] == {"E": True}


@pytest.mark.parametrize("name", ["echo.toml", "echo-short.toml", "echo-off.toml", "echo-peers.toml", "echo-usrp-peer.toml"])
def test_snippets_are_ascii_and_licensed(name):
    text = snippet(name)
    text.encode("ascii")
    assert text.startswith("# SPDX-License-Identifier: MIT")
    if name != "echo-usrp-peer.toml":                 # the peer row uses the address tokens
        assert "<" not in text.replace("<=", "")      # no tokens: nothing to substitute
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


# ---------------------------------------------------------------- E9: D-STAR echo return integrity

N_FRAMES = 450
SID = 0x1234


def sent_frames(n=N_FRAMES):
    return signals.prng_frames("dstar", n, seed="e9-unit")


def stream(frames, sid=SID, seq=None, sync=None, end_bit=True, drop=(), end_slow=b"\x00\x00\x00"):
    """DSVT voice packets of a stream as the SUT should send it: seq = position mod 21, sync on
    positions mod 21 == 0, a last packet with bit 0x40. `drop`: sent indexes left out (positions
    are numbered over the frames that remain), `seq` / `sync` override per position."""
    kept = [f for i, f in enumerate(frames) if i not in set(drop)]
    out = []
    for pos, frame in enumerate(kept):
        counter = pos % 21 if seq is None else seq(pos)
        has_sync = pos % 21 == 0 if sync is None else sync(pos)
        out.append(dextra.voice_packet(sid, counter, frame, dextra.SYNC_DATA if has_sync else dextra.SLOW_DATA))
    pos = len(kept)
    out.append(dextra.voice_packet(sid, (pos % 21) | (0x40 if end_bit else 0), dextra.END_PATTERN[:9], end_slow))
    return out


def clock(n, step=0.020, start=100.0, jitter=None):
    """Arrival times of n packets, step apart (jitter: {position: extra seconds before it})."""
    out, t = [], start
    for i in range(n):
        t += (jitter or {}).get(i, 0.0) + (step if i else 0.0)
        out.append(t)
    return out


def test_integrity_perfect_stream():
    frames = sent_frames()
    pkts = stream(frames)
    got = echo.integrity(frames, pkts, clock(len(pkts)))
    assert got["frames_sent"] == got["frames_heard"] == N_FRAMES and got["lost"] == 0 and got["extra"] == 0
    assert got["reordered"] == 0 and got["gaps_over_25ms"] == [] and 19.0 < got["max_gap_ms"] < 21.0
    assert got["seq_ok_share"] == 1.0 and got["seq_bad"] == [] and got["last_frame_has_end_bit"]
    assert got["sync_frames"] == list(range(0, N_FRAMES, 21)) and got["sync_missing"] == [] and got["sync_extra"] == []
    assert got["header"] is None and got["packets"] == N_FRAMES + 1
    summary = echo.summarize("forward", [got, got])
    assert summary["lost_frames_total"] == 0 and summary["share_of_frames_with_wrong_seq"] == 0.0
    assert summary["runs"] == 2 and summary["sync_errors"] == 0 and summary["gaps_over_25ms"] == 0
    assert echo.integrity_failures({"forward": summary}) == []


def test_integrity_one_lost_frame_is_a_40_ms_gap_and_449_of_450():
    frames = sent_frames()
    pkts = stream(frames, drop={200})
    # the arrivals of the frames after the hole are 20 ms later than they would be: a 40 ms gap at position 200
    got = echo.integrity(frames, pkts, clock(len(pkts), jitter={200: 0.020}))
    assert got["frames_sent"] == 450 and got["frames_heard"] == 449 and got["lost"] == 1 and got["lost_indexes"] == [200]
    assert got["gaps_over_25ms"] == [(200, pytest.approx(40.0, abs=0.2))] and 39.5 < got["max_gap_ms"] < 40.5
    assert got["extra"] == 0 and got["seq_bad_count"] == 0       # the SUT renumbered: only the gap tells
    summary = echo.summarize("echo_listener", [got, echo.integrity(frames, stream(frames), clock(451))])
    assert summary["echoes_with_lost_frame"] == 1 and summary["lost_frames_total"] == 1 and summary["gaps_over_25ms"] == 1
    msgs = echo.integrity_failures({"forward": echo.summarize("forward", [echo.integrity(frames, stream(frames), clock(451))]),
                                    "echo_listener": summary})
    assert len(msgs) == 1 and msgs[0].startswith("echo return to the listener") and "1 lost frame" in msgs[0]


def test_integrity_lost_frames_are_found_by_payload_not_by_count():
    frames = sent_frames(60)
    pkts = stream(frames, drop={0, 30, 31})
    got = echo.integrity(frames, pkts)
    assert got["lost"] == 3 and got["lost_indexes"] == [0, 30, 31] and got["frames_heard"] == 57
    assert got["max_gap_ms"] == 0.0 and got["gaps_over_25ms"] == []      # no arrival times given


def test_integrity_wrong_seq_bytes_give_a_share_near_chance():
    frames = sent_frames()
    rng = random.Random(7)
    pkts = stream(frames, seq=lambda pos: rng.randrange(21))
    got = echo.integrity(frames, pkts, clock(len(pkts)))
    assert 0.02 < got["seq_ok_share"] < 0.12                 # 1 in 21 by chance (the owner saw 3 to 5 percent)
    assert got["seq_bad_count"] > 400 and len(got["seq_bad"]) == echo.SEQ_BAD_KEEP
    pos, have, want = got["seq_bad"][0]
    assert have != want and want == (pos % 21)
    assert got["lost"] == 0 and got["sync_missing"] == [] and got["last_frame_has_end_bit"]
    summary = echo.summarize("echo_sender", [got])
    assert summary["share_of_frames_with_wrong_seq"] > 0.85
    assert "wrong seq byte" in echo.integrity_failures({"echo_sender": summary})[0]


def test_integrity_seq_off_by_one_everywhere_is_all_wrong():
    frames = sent_frames(100)
    pkts = stream(frames, seq=lambda pos: (pos + 1) % 21)
    got = echo.integrity(frames, pkts)
    assert got["seq_bad"][0] == (0, 1, 0) and got["seq_ok_share"] < 0.02


def test_integrity_missing_end_bit():
    frames = sent_frames(100)
    got = echo.integrity(frames, stream(frames, end_bit=False), clock(101))
    assert got["last_frame_has_end_bit"] is False
    assert got["seq_bad"] == [(100, 100 % 21, (100 % 21) | 0x40)]          # the last packet's seq lacks 0x40
    assert got["extra"] == 1                                                 # and it counts as a frame that was not sent
    summary = echo.summarize("echo_listener", [got])
    assert summary["missing_end_bit"] == 1 and "end bit" in echo.integrity_failures({"echo_listener": summary})[0]


def test_integrity_end_bit_on_the_last_packet_only():
    frames = sent_frames(40)
    pkts = stream(frames)
    pkts[10] = dextra.voice_packet(SID, 10 | 0x40, frames[10])     # an end bit in the middle
    got = echo.integrity(frames, pkts)
    assert got["last_frame_has_end_bit"] is True and got["frames_heard"] == 39       # the packet is taken as a last one
    assert got["lost"] == 1 and got["lost_indexes"] == [10]


def test_integrity_sync_on_a_wrong_frame_and_a_missing_sync():
    frames = sent_frames(100)
    pkts = stream(frames, sync=lambda pos: pos in (0, 21, 43, 63, 84))
    got = echo.integrity(frames, pkts)
    assert got["sync_frames"] == [0, 21, 43, 63, 84]
    assert got["sync_extra"] == [43] and got["sync_missing"] == [42]
    assert echo.summarize("forward", [got])["sync_errors"] == 2
    clean = echo.integrity(frames, stream(frames))
    assert clean["sync_frames"] == [0, 21, 42, 63, 84] and clean["sync_extra"] == [] and clean["sync_missing"] == []


def test_integrity_end_pattern_slow_data_is_not_a_sync_error():
    frames = sent_frames(42)                       # the last packet sits at position 42 == 0 mod 21
    got = echo.integrity(frames, stream(frames, end_slow=dextra.SYNC_DATA))
    assert got["sync_missing"] == [] and got["sync_extra"] == [] and 42 in got["sync_frames"]


def test_integrity_corrupt_and_extra_and_reordered_frames():
    frames = sent_frames(30)
    pkts = stream(frames)
    pkts[5] = dextra.voice_packet(SID, 5, b"\xff" * 9)
    got = echo.integrity(frames, pkts)
    assert got["lost"] == 1 and got["lost_indexes"] == [5] and got["extra"] == 1
    swapped = stream(frames)
    swapped[3], swapped[4] = stream(frames)[4], stream(frames)[3]
    assert echo.integrity(frames, swapped)["reordered"] == 1


def test_integrity_empty_stream_loses_everything():
    frames = sent_frames(10)
    got = echo.integrity(frames, [], [])
    assert got["lost"] == 10 and got["frames_heard"] == 0 and got["last_frame_has_end_bit"] is False
    assert got["seq_ok_share"] == 0.0 and got["packets"] == 0
    summary = echo.summarize("echo_sender", [got])
    assert summary["empty_streams"] == 1 and summary["share_of_frames_with_wrong_seq"] is None
    assert "no stream" in echo.integrity_failures({"echo_sender": summary})[0]


def test_header_record_names_the_four_fields():
    head = dextra.header(SID, "N0CALL", "E", reflector="XLX345")
    assert echo.header_record(head) == {"rpt1": "N0CALL E", "rpt2": "XLX345 E", "my": "N0CALL", "ur": "CQCQCQ"}
    other = dextra.header_packet(SID, my="N0CALL", rpt2="N0CALL B", rpt1="N0CALL E")
    assert echo.header_record(other)["rpt2"] == "N0CALL B"
    got = echo.integrity(sent_frames(5), stream(sent_frames(5)), header=head)
    assert got["header"]["my"] == "N0CALL" and got["header"]["rpt2"] == "XLX345 E"
    assert echo.header_record(None) is None and echo.header_record(b"DSVT") is None


def test_dstar_streams_split_forward_and_echo_and_skip_keepalives():
    frames = sent_frames(5)
    fwd = [dextra.header(1, "N0CALL", "E")] + stream(frames, sid=1)
    rep = [dextra.header(2, "N0CALL", "E")] + stream(frames, sid=2)
    items = [(10.0, b"XRF000 \x20\x00")]
    items += [(10.1 + 0.02 * i, d) for i, d in enumerate(fwd)]
    items += [(11.4 + 0.02 * i, d) for i, d in enumerate(rep)]
    items += [(12.0, b"N0TEST  \x00")]
    got = echo.dstar_streams(items)
    assert len(got) == 2 and got[0]["sids"] == [1] and got[1]["sids"] == [2]
    assert got[0]["header"] == fwd[0] and len(got[0]["voice"]) == 6 and len(got[1]["voice"]) == 6
    assert got[1]["start"] > got[0]["end"] + 1.0
    judged = echo.integrity(frames, [d for _, d in got[1]["voice"]], [t for t, _ in got[1]["voice"]], got[1]["header"])
    assert judged["lost"] == 0 and judged["header"]["my"] == "N0CALL"
    assert echo.dstar_streams([]) == []
