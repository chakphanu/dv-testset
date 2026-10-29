# SPDX-License-Identifier: MIT
"""dvtest.check.p25 on synthetic P25Reflector traffic built with
dvtest.proto.p25: a clean call gives no finding, each fault its code."""
from dvtest.proto import p25
from test_check_common import Wire, codes, frames_of

X, REF = "192.0.2.11", "192.0.2.60"
CPORT = 40004
FRAME = bytes(range(11))


def poll(w, echo=True, gap=0.0):
    w.send(X, CPORT, REF, p25.PORT, p25.poll_packet("XLX999 B"), gap)
    if echo:
        w.send(REF, p25.PORT, X, CPORT, p25.poll_packet("XLX999 B"), 0.001)


def p25_wire(fault=None):
    w = Wire()
    poll(w)
    records = p25.voice_records([FRAME] * 30, 1023007, 10200)        # 4 LDUs + terminator
    if fault == "drop":
        del records[11]
    elif fault == "short":
        records[5] = records[5][:-1]
    elif fault == "early_end":
        del records[-3:-1]
    elif fault == "no_terminator":
        records.pop()
    elif fault == "src":
        records[22] = p25.record(0x66, FRAME, src=4242, dst=10200)
    if fault == "unlink":
        w.send(X, CPORT, REF, p25.PORT, p25.unlink_packet("XLX999 B"), 0.01)
    for rec in records:
        w.send(REF, p25.PORT, X, CPORT, rec, 0.020)
    poll(w, echo=fault != "no_echo", gap=6.0)
    if fault == "no_echo":
        poll(w, gap=2.0)
    return w


def test_clean_call(tmp_path):
    result = p25_wire().check(tmp_path)
    assert codes(result) == []
    s = result["protocols"]["p25"]["streams"][0]
    assert (s["frames"], s["loss"], s["end"]) == (36, 0, "terminated")
    assert result["protocols"]["p25"]["kinds"]["poll_echo"] == 2


def test_faults(tmp_path):
    result = p25_wire("drop").check(tmp_path)
    assert codes(result) == ["p25.record_loss"] and frames_of(result, "p25.record_loss") == [14]
    result = p25_wire("short").check(tmp_path)
    assert codes(result, "error") == ["p25.bad_length", "p25.record_loss"]
    assert codes(p25_wire("early_end").check(tmp_path)) == ["p25.terminator_in_ldu"]
    assert codes(p25_wire("no_terminator").check(tmp_path)) == ["p25.no_terminator"]
    assert codes(p25_wire("src").check(tmp_path)) == ["p25.field_changed"]
    assert codes(p25_wire("unlink").check(tmp_path)) == ["p25.after_unlink"]
    result = p25_wire("no_echo").check(tmp_path)
    assert codes(result) == ["p25.no_echo"]
