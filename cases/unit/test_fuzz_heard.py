# SPDX-License-Identifier: MIT
"""fuzz_run: which frames of a valid round count (FUZZP25A). The P25 round counts only the frames of the
valid over, fuzz voice heard beside it is recorded as heard_other. Offline, no sockets."""
from dvtest import fuzz_run
from dvtest.proto import p25

VALID_SRC = 1023007
FUZZ_SRC = 1023009


def frames(tag, count):
    return [bytes([tag]) + bytes([i]) * 10 for i in range(count)]


def call(frame_list, src, terminate=True):
    """Parsed received records of one call: voice records with the source id on 0x66, then a terminator."""
    recs = p25.voice_records(frame_list, src, 31672)
    out = [p25.parse(r) for r in recs]
    if not terminate:
        out = [r for r in out if not r["terminator"]]
    return [r for r in out if r is not None]


def voice(records):
    return [r for r in records if not r["terminator"]]


def test_match_sent_is_a_multiset():
    sent = [b"a", b"b", b"b"]
    assert fuzz_run.match_sent(sent, [b"b", b"b", b"b", b"a", b"z"]) == 3
    assert fuzz_run.match_sent(sent, []) == 0


def test_p25_calls_split_on_terminator_and_first_ldu1():
    one, two = frames(1, 18), frames(2, 18)
    calls = fuzz_run.p25_calls(call(one, VALID_SRC) + call(two, FUZZ_SRC, terminate=False))
    assert [c["src"] for c in calls] == [VALID_SRC, FUZZ_SRC]
    assert [len(c["frames"]) for c in calls] == [18, 18]
    # two calls with no terminator between them still split at the next 0x62
    glued = fuzz_run.p25_calls(voice(call(one, VALID_SRC)) + voice(call(two, FUZZ_SRC)))
    assert [c["src"] for c in glued] == [VALID_SRC, FUZZ_SRC]


def test_fuzz_voice_cannot_make_up_the_count():
    """The old round counted every voice record: 18 valid + 36 fuzz records read as 54 of 54."""
    sent = frames(1, 54)
    got = voice(call(sent[:18], VALID_SRC)) + voice(call(frames(7, 36), FUZZ_SRC))
    assert len(got) == 54                                   # what heard() counted
    ident = fuzz_run.p25_identity(fuzz_run.p25_count(voice(call(sent, VALID_SRC)), sent))
    assert ident["how"] == "content"
    valid, extra = fuzz_run.split_heard("p25", got, sent, ident)
    assert valid == 18
    assert extra["heard_other"] == 36 and extra["heard_total"] == 54 and extra["heard_valid"] == 18
    assert valid < 0.5 * len(sent)          # HEARD_MIN of the case file


def test_valid_over_served_in_full_with_fuzz_voice_beside_it():
    sent = frames(1, 54)
    got = voice(call(frames(7, 9), FUZZ_SRC)) + voice(call(sent, VALID_SRC))
    ident = {"how": "content", "valid_src": [VALID_SRC]}
    valid, extra = fuzz_run.split_heard("p25", got, sent, ident)
    assert valid == 54 and extra["heard_other"] == 9 and extra["heard_src"] == 54


def test_content_copied_by_the_fuzzer_counts_once_per_sent_frame():
    sent = frames(1, 18)
    got = voice(call(sent, FUZZ_SRC)) + voice(call(sent, FUZZ_SRC))
    valid, extra = fuzz_run.split_heard("p25", got, sent, {"how": "content", "valid_src": [VALID_SRC]})
    assert valid == 18 and extra["heard_other"] == 18


def test_identity_falls_back_to_the_source_id_when_the_payload_is_changed():
    sent = frames(1, 18)
    warm = voice(call(frames(9, 18), VALID_SRC))            # the server re-encoded the payload
    ident = fuzz_run.p25_identity(fuzz_run.p25_count(warm, sent))
    assert ident == {"how": "src", "valid_src": [VALID_SRC]}
    got = voice(call(frames(8, 18), VALID_SRC)) + voice(call(frames(7, 36), FUZZ_SRC))
    valid, extra = fuzz_run.split_heard("p25", got, sent, ident)
    assert valid == 18 and extra["heard_other"] == 36 and extra["identity"] == "src"


def test_nothing_heard_is_zero_valid():
    sent = frames(1, 18)
    valid, extra = fuzz_run.split_heard("p25", [], sent, {"how": "content", "valid_src": [VALID_SRC]})
    assert valid == 0 and extra["heard_other"] == 0


def test_other_protocols_keep_the_plain_count_and_record_the_content_match():
    sent = frames(1, 54)
    got = sent[:20] + frames(7, 40)
    valid, extra = fuzz_run.split_heard("dextra", got, sent)
    assert valid == 60                                      # verdict unchanged: every voice frame
    assert extra == {"heard_total": 60, "heard_content": 20, "heard_other_est": 40}


def test_guard_deltas_by_proc():
    before = ('dv_edge_drops_total{proc="p25",reason="deny_guard_block"} 1\n'
              'dv_edge_drops_total{proc="dcs",reason="deny_guard_block"} 5\n'
              'dv_edge_drops_total{proc="p25",reason="guard_blocked"} 0\n')
    after = ('dv_edge_drops_total{proc="p25",reason="deny_guard_block"} 3\n'
             'dv_edge_drops_total{proc="dcs",reason="deny_guard_block"} 6\n'
             'dv_edge_drops_total{proc="p25",reason="guard_blocked"} 0\n')
    assert fuzz_run.guard_deltas(before, after, "p25") == {"deny_guard_block": 2, "guard_blocked": 0}
    assert fuzz_run.guard_deltas(before, after) == {"deny_guard_block": 3, "guard_blocked": 0}
    assert fuzz_run.guard_deltas(None, after, "p25") is None


def test_round_record_line():
    row = {"tag": "same-889", "heard": 18, "expected": 54, "heard_other": 36, "heard_total": 54, "identity": "content",
           "guard_deny": {"fuzz_batch": {"deny_guard_block": 0, "guard_blocked": 0},
                          "valid_round": {"deny_guard_block": 2, "guard_blocked": 0}}}
    line = fuzz_run.round_record(row)
    assert "same-889 18/54" in line and "other 36" in line and "valid_round deny_guard_block +2" in line
    quiet = dict(row, guard_deny={"fuzz_batch": {"deny_guard_block": 0}, "valid_round": {"deny_guard_block": 0}})
    assert "guard deny 0" in fuzz_run.round_record(quiet)
    assert fuzz_run.round_record({"tag": "x", "heard": None}) == ""
