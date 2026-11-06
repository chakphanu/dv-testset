# SPDX-License-Identifier: MIT
"""dvtest/echo.py (pure helpers of cases/test_echo.py) and the rig snippets echo*.toml."""
import random
import tomllib
from pathlib import Path

import pytest

from dvtest import clients, echo, sut_config
from dvtest.audio import signals
from dvtest.proto import dcs, dextra, dmr, dplus, m17, xlx, ysf

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


@pytest.mark.parametrize("name", ["echo.toml", "echo-short.toml", "echo-off.toml", "echo-peers.toml", "echo-usrp-peer.toml",
                                  "xlx-peer-tester.toml"])
def test_snippets_are_ascii_and_licensed(name):
    text = snippet(name)
    text.encode("ascii")
    assert text.startswith("# SPDX-License-Identifier: MIT")
    if name not in ("echo-usrp-peer.toml", "xlx-peer-tester.toml"):     # the peer rows use the address tokens
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


def test_header_record_names_the_fields():
    head = dextra.header(SID, "N0CALL", "E", reflector="XLX345")
    assert echo.header_record(head) == {"rpt1": "N0CALL E", "rpt2": "XLX345 E", "my": "N0CALL", "ur": "CQCQCQ",
                                        "flags": "000000", "suffix": ""}
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


# ---------------------------------------------------------------- E9: the five kinds (dextra, dplus, dcs, g3, xlx)

HDR_FIELDS = dict(my="N0CALL", rpt1="N0CALL E", rpt2="XRF999 E", ur="CQCQCQ", suffix=b"    ", flags=b"\x00\x00\x00")


def kind_header(kind, sid=SID, **over):
    """The header packet a client of `kind` sends (dcs: its first voice packet)."""
    f = dict(HDR_FIELDS, **over)
    if kind in ("dextra", "g3", "xlx"):
        return dextra.header_packet(sid, **f)
    if kind == "dplus":
        return b"\x3a\x80" + dextra.header_packet(sid, **f)
    return dcs.voice_packet(sid, 0, bytes(9), **f)


def kind_stream(kind, frames, sid=SID, seq=None, counter=None, pids=None, drop=(), end_bit=True):
    """Voice packets of a stream of `kind` as the SUT should send it. `drop`: sent indexes left out; `seq`
    (position -> seq byte), `counter` (position -> DCS frame counter) and `pids` (position -> (id, sub id))
    override the right values."""
    kept = [f for i, f in enumerate(frames) if i not in set(drop)]
    n = len(kept)
    out = []
    for pos, frame in enumerate(kept):
        want = pos % 21 if seq is None else seq(pos)
        slow = dextra.SYNC_DATA if pos % 21 == 0 else dextra.SLOW_DATA
        if kind in ("dextra", "g3"):
            out.append(dextra.voice_packet(sid, want, frame, slow))
        elif kind == "dplus":
            out.append(b"\x1d\x80" + dextra.voice_packet(sid, want, frame, slow))
        elif kind == "dcs":
            out.append(dcs.voice_packet(sid, want, frame, slow, counter=pos if counter is None else counter(pos), **HDR_FIELDS))
        else:
            pid, sub = ((pos // 3) % 6, pos % 3 + 1) if pids is None else pids(pos)
            packet = bytearray(xlx.voice(sid, pos, frame, slow=slow, revision=3, dmr_pid=pid, dmr_subid=sub))
            packet[14] = want
            out.append(bytes(packet))
    last = (n % 21) | (0x40 if end_bit else 0)
    if kind in ("dextra", "g3"):
        out.append(dextra.voice_packet(sid, last, dextra.END_PATTERN[:9], b"\x00\x00\x00"))
    elif kind == "dplus":
        tail = bytearray(dplus.end_frame(sid, n))
        tail[16] = last
        out.append(bytes(tail))
    elif kind == "dcs":
        out.append(dcs.voice_packet(sid, last, bytes(9), counter=n, **HDR_FIELDS))
    else:
        out.append(xlx.voice(sid, n, xlx.END_AMBE, last=end_bit, slow=xlx.END_SLOW, revision=3))
    return out


KIND_FRAMES = sent_frames(100)


@pytest.mark.parametrize("kind", echo.KINDS)
def test_kind_clean_stream_passes_every_check(kind):
    pkts = kind_stream(kind, KIND_FRAMES)
    got = echo.integrity(KIND_FRAMES, pkts, clock(len(pkts)), kind_header(kind), kind=kind)
    assert got["kind"] == kind and got["lost"] == 0 and got["extra"] == 0 and got["frames_heard"] == 100
    assert got["seq_ok_share"] == 1.0 and got["last_frame_has_end_bit"] and got["sync_missing"] == [] and got["sync_extra"] == []
    assert got["gaps_over_25ms"] == [] and got["gaps_ge_35ms"] == [] and got["aux_bad_count"] == 0
    assert got["aux_name"] == {"dcs": "frame_counter", "xlx": "dmr_packet_id"}.get(kind)
    assert got["aux_checked"] == (100 if got["aux_name"] else 0)
    assert got["header"]["my"] == "N0CALL" and got["header"]["rpt2"] == "XRF999 E"
    summary = echo.summarize("echo_listener", [got])
    assert echo.integrity_failures({"echo_listener": summary}) == []


@pytest.mark.parametrize("kind", echo.KINDS)
def test_kind_one_dropped_unit_is_a_lost_frame_and_a_40_ms_gap(kind):
    pkts = kind_stream(kind, KIND_FRAMES, drop={50})
    got = echo.integrity(KIND_FRAMES, pkts, clock(len(pkts), jitter={50: 0.020}), kind=kind)
    assert got["lost"] == 1 and got["lost_indexes"] == [50] and got["frames_heard"] == 99
    assert got["gaps_over_25ms"] == [(50, pytest.approx(40.0, abs=0.2))] and got["gaps_ge_35ms"] == got["gaps_over_25ms"]
    summary = echo.summarize("echo_sender", [got])
    msg = echo.integrity_failures({"echo_sender": summary})[0]
    assert "1 lost frame" in msg and "1 gap(s) of 35 ms or more" in msg and "max 40.0 ms" in msg


@pytest.mark.parametrize("kind", echo.KINDS)
def test_kind_wrong_seq_bytes_are_found(kind):
    rng = random.Random(3)
    pkts = kind_stream(kind, KIND_FRAMES, seq=lambda pos: rng.randrange(21))
    got = echo.integrity(KIND_FRAMES, pkts, clock(len(pkts)), kind=kind)
    assert got["seq_ok_share"] < 0.2 and got["seq_bad_count"] > 80 and got["lost"] == 0
    assert got["aux_bad_count"] == 0                   # the seq byte is not the protocol's own counter
    assert "wrong seq byte" in echo.integrity_failures({"echo_listener": echo.summarize("echo_listener", [got])})[0]


@pytest.mark.parametrize("kind", echo.KINDS)
def test_kind_missing_end_bit(kind):
    got = echo.integrity(KIND_FRAMES, kind_stream(kind, KIND_FRAMES, end_bit=False), kind=kind)
    assert got["last_frame_has_end_bit"] is False
    assert echo.summarize("forward", [got])["missing_end_bit"] == 1


def test_gap_counters_split_at_35_ms():
    frames = KIND_FRAMES[:10]
    pkts = kind_stream("dextra", frames)
    # 10, 16 and 20 ms late on top of the 20 ms step: gaps of 30, 36 and 40 ms
    got = echo.integrity(frames, pkts, clock(len(pkts), jitter={2: 0.010, 5: 0.016, 8: 0.020}))
    assert [p for p, _ in got["gaps_over_25ms"]] == [2, 5, 8] and [p for p, _ in got["gaps_ge_35ms"]] == [5, 8]
    summary = echo.summarize("echo_listener", [got])
    assert summary["gaps_over_25ms"] == 3 and summary["gaps_ge_35ms"] == 2 and summary["max_gap_ms"] == pytest.approx(40.0, abs=0.2)
    msgs = echo.integrity_failures({"echo_listener": summary})
    assert "3 arrival gap(s) over 25 ms" in msgs[0] and "2 gap(s) of 35 ms or more" in msgs[0]


def test_dcs_frame_counter_must_equal_the_position():
    pkts = kind_stream("dcs", KIND_FRAMES, counter=lambda pos: pos + 1 if pos >= 40 else pos)
    got = echo.integrity(KIND_FRAMES, pkts, kind="dcs")
    assert got["aux_bad_count"] == 60 and got["aux_bad"][0] == (40, 41, 40) and got["seq_bad_count"] == 0
    msg = echo.integrity_failures({"echo_sender": echo.summarize("echo_sender", [got])})[0]
    assert "60 packet(s) with a wrong frame counter" in msg
    pkts = kind_stream("dcs", KIND_FRAMES, drop={10})                    # a dropped unit with the counters renumbered: only gap and payload tell
    assert echo.integrity(KIND_FRAMES, pkts, kind="dcs")["aux_bad_count"] == 0


def test_xlx_dmr_ids_must_advance_one_step_per_packet():
    got = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES), kind="xlx")
    assert got["aux_seq"][:7] == [(0, 1), (0, 2), (0, 3), (1, 1), (1, 2), (1, 3), (2, 1)] and got["aux_seq"][18] == (0, 1)
    bad = kind_stream("xlx", KIND_FRAMES, pids=lambda pos: ((pos // 3) % 6, pos % 3 + 1) if pos != 30 else (5, 1))
    got = echo.integrity(KIND_FRAMES, bad, kind="xlx")
    assert got["aux_bad"][0] == (30, (5, 1), (4, 1)) and got["aux_bad_count"] == 1
    assert got["seq_bad_count"] == 0
    # a stream that starts at another step is consistent with itself (the rule is the advance, not the start)
    shifted = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES, pids=lambda pos: (((pos + 7) // 3) % 6, (pos + 7) % 3 + 1)), kind="xlx")
    assert shifted["aux_bad_count"] == 0 and shifted["aux_seq"][0] == (2, 2)
    invalid = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES, pids=lambda pos: (0, 0)), kind="xlx")
    assert invalid["aux_bad_count"] > 90
    # a revision 1 peer (27 byte frames) has only the seq byte (xlxd's packet id)
    rev1 = [xlx.voice(SID, i, f, revision=1) for i, f in enumerate(KIND_FRAMES)] + [xlx.voice(SID, 100, xlx.END_AMBE, last=True, revision=1)]
    got = echo.integrity(KIND_FRAMES, rev1, kind="xlx")
    assert got["aux_name"] is None and got["seq_ok_share"] == 1.0 and got["lost"] == 0


def test_aux_vs_forward_compares_the_sequences_position_by_position():
    fwd = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES), kind="xlx")
    same = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES), kind="xlx")
    assert echo.aux_vs_forward(fwd, same) == {"compared": 100, "differ": 0, "first": []}
    other = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES, pids=lambda pos: (((pos + 3) // 3) % 6, pos % 3 + 1)), kind="xlx")
    got = echo.aux_vs_forward(fwd, other)
    assert got["differ"] == 100 and got["first"][0] == (0, (0, 1), (1, 1)) and other["aux_bad_count"] == 0
    short = echo.integrity(KIND_FRAMES, kind_stream("xlx", KIND_FRAMES, drop={99}), kind="xlx")
    assert echo.aux_vs_forward(fwd, short)["differ"] == 1
    none = echo.integrity(KIND_FRAMES, kind_stream("dextra", KIND_FRAMES), kind="dextra")
    assert echo.aux_vs_forward(fwd, none) is None and echo.aux_vs_forward(none, fwd) is None
    other["aux_vs_forward"] = got
    summary = echo.summarize("echo_listener", [other])
    assert summary["aux_vs_forward_errors"] == 100 and summary["aux_name"] == "dmr_packet_id"
    assert "differs from the forward copy" in echo.integrity_failures({"echo_listener": summary})[0]


def test_voice_view_per_kind_and_non_stream_packets():
    f = KIND_FRAMES[0]
    assert echo.voice_view("dextra", dextra.voice_packet(SID, 3, f))["counter"] == 3
    assert echo.voice_view("dplus", b"\x1d\x80" + dextra.voice_packet(SID, 3, f))["ambe"] == f
    last = echo.voice_view("dplus", dplus.last_voice(SID, 5))
    assert last["last"] and last["counter"] == (5 | 0x40) and last["stream_id"] == SID
    view = echo.voice_view("dcs", dcs.voice_packet(SID, 7, f, counter=1234, **HDR_FIELDS))
    assert view["counter"] == 7 and view["frame_counter"] == 1234 and view["ambe"] == f and view["stream_id"] == SID
    view = echo.voice_view("xlx", xlx.voice(SID, 4, f, revision=3))
    assert (view["pid"], view["subid"]) == (1, 2) and view["counter"] == 4
    assert "pid" not in echo.voice_view("xlx", xlx.voice(SID, 4, f, revision=1))
    for kind, junk in (("dextra", b"XRF000 \x20\x00"), ("dplus", dplus.KEEPALIVE), ("dcs", bytes(22)),
                       ("xlx", xlx.keepalive("XLX999")), ("g3", b"PING")):
        assert echo.voice_view(kind, junk) is None and echo.header_view(kind, junk) is None
    assert echo.voice_view("dextra", dextra.header(SID, "N0CALL", "E")) is None
    with pytest.raises(ValueError):
        echo.voice_view("m17", b"x")


def test_header_views_and_records_per_kind():
    for kind in echo.KINDS:
        rec = echo.header_record(kind_header(kind))
        assert rec == {"flags": "000000", "rpt1": "N0CALL E", "rpt2": "XRF999 E", "my": "N0CALL", "ur": "CQCQCQ", "suffix": ""}, kind
        assert echo.header_view(kind, kind_header(kind))["stream_id"] == SID
    odd = echo.header_record(kind_header("dplus", flags=b"\x01\x02\x03", suffix=b"AB  "))
    assert odd["flags"] == "010203" and odd["suffix"] == "AB"


def test_dstar_streams_per_kind_skip_other_packets_and_dcs_header_is_the_first_voice_packet():
    frames = sent_frames(4)
    for kind in ("dplus", "dcs", "g3", "xlx"):
        fwd = ([kind_header(kind, 1)] if kind != "dcs" else []) + kind_stream(kind, frames, sid=1)
        rep = ([kind_header(kind, 2)] if kind != "dcs" else []) + kind_stream(kind, frames, sid=2)
        items = [(10.0, b"\x03\x60\x00" if kind == "dplus" else bytes(9))]
        items += [(10.1 + 0.02 * i, d) for i, d in enumerate(fwd)] + [(11.4 + 0.02 * i, d) for i, d in enumerate(rep)]
        got = echo.dstar_streams(items, kind=kind)
        assert len(got) == 2 and got[0]["sids"] == [1] and got[1]["sids"] == [2], kind
        assert len(got[0]["voice"]) == 5 and len(got[1]["voice"]) == 5
        assert echo.header_record(got[1]["header"])["my"] == "N0CALL", kind
        if kind == "dcs":
            assert got[1]["header"] == got[1]["voice"][0][1]
        judged = echo.integrity(frames, [d for _, d in got[1]["voice"]], [t for t, _ in got[1]["voice"]], got[1]["header"], kind=kind)
        assert judged["lost"] == 0 and judged["header"]["rpt2"] == "XRF999 E", kind


def test_header_failures_rule():
    sent = echo.header_record(kind_header("dextra", rpt2="XLX999 E"))
    live = echo.header_record(kind_header("dextra", rpt2="XRF999 E"))
    assert echo.header_failures(sent, live, dict(live)) == []              # the echo repeats the live RPT2, not the sent one
    assert echo.header_failures(sent, live, None) == ["no echo header"]
    for key, value in (("flags", "400000"), ("rpt1", "N0CALL B"), ("ur", "CQCQCQ B"), ("my", "N0TEST"), ("suffix", "ABCD")):
        msgs = echo.header_failures(sent, live, dict(live, **{key: value}))
        assert len(msgs) == 1 and msgs[0].startswith("echo %s is" % key.upper()), (key, msgs)
    node = dict(live, rpt2="N0CALL B")                                       # the node callsign + module of the voice test
    assert echo.header_failures(sent, live, node) == ["echo RPT2 is 'N0CALL B', the forward copy carries 'XRF999 E'"]
    assert "no forward header" in echo.header_failures(sent, None, dict(live))[0]
    assert "sender sent is unknown" in echo.header_failures(None, live, dict(live))[0]
    assert len(echo.header_failures(sent, live, dict(live, my="X", rpt2="Y"))) == 2


def test_parse_kinds():
    assert echo.parse_kinds("") == (list(echo.KINDS), []) and echo.parse_kinds(None) == (list(echo.KINDS), [])
    assert echo.parse_kinds(" xlx, dextra ,xlx") == (["dextra", "xlx"], [])
    assert echo.parse_kinds("dcs,bogus,Bogus,G3") == (["dcs", "g3"], ["bogus"])


def test_xlx_peer_rows_and_mode():
    text = rendered("echo.toml") + """
[[edges.xlx.peer]]
callsign = "XLX998"
host = "172.30.0.12"
modules = "DE"

[[edges.xlx.peer]]
callsign = "XLX996"
host = "172.30.0.30"
modules = "D"
"""
    rows = echo.xlx_peer_rows(text)
    assert [r["callsign"] for r in rows] == ["XLX998", "XLX996"] and rows[0]["modules"] == "DE"
    assert echo.xlx_peer_rows(rendered("echo.toml")) == []
    assert echo.xlx_mode(rows, "172.30.0.30", "E")[0] == "reflector"          # the tester row does not serve E
    assert echo.xlx_mode(rows, "172.30.0.30", "D")[0] == "tester" and echo.xlx_mode(rows, "172.30.0.30", "D")[1]["callsign"] == "XLX996"
    assert echo.xlx_mode(rows, "172.30.0.30", "F") == (None, None) and echo.xlx_mode([], "172.30.0.30", "E") == (None, None)
    assert echo.xlx_mode([{"callsign": "X", "host": "tester", "modules": "E"}], "10.0.0.1", "E")[0] == "tester"
    with pytest.raises(ValueError):
        echo.xlx_peer_rows("not = [toml")


def test_xlx_peer_tester_snippet_names_the_tester():
    rows = echo.xlx_peer_rows(rendered("echo.toml", "xlx-peer-tester.toml"))
    assert rows == [{"callsign": "XLX996", "host": "172.30.0.30", "modules": "DE"}]
    assert echo.xlx_mode(rows, "172.30.0.30", "E")[0] == "tester"
    both = echo.xlx_peer_rows(rendered("echo.toml", "xlx-peer.toml", "xlx-peer-tester.toml"))
    assert echo.xlx_mode(both, "172.30.0.30", "E")[1]["callsign"] == "XLX996"
