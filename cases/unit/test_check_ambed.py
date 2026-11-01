# SPDX-License-Identifier: MIT
"""dvtest.check.ambed on synthetic captures built with dvtest.proto.ambed."""
import pytest

from dvtest.proto import ambed as A
from test_check_common import Wire, codes, frames_of

X, T = "192.0.2.11", "192.0.2.21"     # reflector (client), transcoder (server)
PORT = 10101


def ctl(w, data, reply, gap=0.0):
    w.send(X, A.PORT, T, A.PORT, data, gap)
    if reply is not None:
        w.send(T, A.PORT, X, A.PORT, reply, 0.001)


def stream(w, frames=10, codec_in=A.PCM, outs=(A.AMBEPLUS, A.CODEC2_3200), pids=None, skip_reply=()):
    for i, pid in enumerate(pids if pids is not None else range(frames)):
        w.send(X, PORT, T, PORT, A.data_request(codec_in, pid, b"\x00" * A.WIDTH[codec_in]), 0.020)
        if i not in skip_reply:
            w.send(T, PORT, X, PORT, A.data_reply(pid, [(c, A.fake_payload(c)) for c in outs]), 0.001)


def session(w, outs=(A.AMBEPLUS, A.CODEC2_3200), tx=0x1000, **kw):
    ctl(w, A.ping("XLX999"), A.PONG)
    ctl(w, A.CAPS_REQUEST, A.caps_reply(4, A.MASK_ALL))
    ctl(w, A.open_ext("XLX999", A.PCM, outs, tx), A.std(1, PORT, A.PCM, outs[0], tx), 0.1)
    stream(w, outs=outs, **kw)
    ctl(w, A.close(1), None, 0.1)
    ctl(w, A.open_legacy("XLX999", A.AMBEPLUS, A.AMBE2PLUS), A.std(2, PORT + 1, A.AMBEPLUS, A.AMBE2PLUS), 0.1)
    ctl(w, A.open_ext("XLX999", A.PCM, (A.PCM,), tx + 1), A.busy(tx + 1), 0.1)
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)


@pytest.mark.parametrize("fmt,end", [("pcapng", "<"), ("pcapng", ">"), ("pcap", "<")])
def test_clean_session(tmp_path, fmt, end):
    w = Wire()
    session(w)
    result = w.check(tmp_path, fmt, end)
    assert codes(result) == ["ambed.open_invalid"]          # the request side of the BUSY case
    p = result["protocols"]["ambed"]
    assert p["kinds"]["data_request"] == p["kinds"]["data_reply"] == 10
    assert p["streams"][0]["frames"] == 10 and p["streams"][0]["loss"] == 0


def test_tx_mismatch(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 7), A.std(1, PORT, A.PCM, A.AMBEPLUS, 8))
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.tx_mismatch") == [2] and frames_of(result, "ambed.no_reply") == [1]


def test_missing_and_bad_replies(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS, A.CODEC2_3200), 1), A.std(1, PORT, A.PCM, A.AMBEPLUS, 1))
    stream(w, frames=6, skip_reply=(2,))
    w.send(T, PORT, X, PORT, A.data_reply(9, [(A.AMBEPLUS, A.fake_payload(A.AMBEPLUS))]), 0.001)
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.reply_missing") == [7]      # request pid 2
    assert codes(result, "error") == ["ambed.reply_bad", "ambed.reply_missing"]


def test_pid_dup_and_data_after_close(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 1), A.std(1, PORT, A.PCM, A.AMBEPLUS, 1))
    stream(w, outs=(A.AMBEPLUS,), pids=[0, 1, 1, 2])
    ctl(w, A.close(1), None)
    stream(w, outs=(A.AMBEPLUS,), pids=[3])
    ctl(w, A.ping("XLX999"), A.PONG, 2.0)
    result = w.check(tmp_path)
    assert codes(result, "error") == ["ambed.data_after_close", "ambed.pid_dup"]


def test_retransmit_must_keep_the_stream(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 5), A.std(1, PORT, A.PCM, A.AMBEPLUS, 5))
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS,), 5), A.std(2, PORT + 1, A.PCM, A.AMBEPLUS, 5))
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.retransmit_new_stream") == [4]


def test_invalid_open_accepted_and_unanswered_ping(tmp_path):
    w = Wire()
    ctl(w, A.open_ext("XLX999", A.PCM, (A.AMBEPLUS, A.AMBEPLUS), 3), A.std(1, PORT, A.PCM, A.AMBEPLUS, 3))
    ctl(w, A.ping("XLX999"), None)
    ctl(w, A.CAPS_REQUEST + b"x", None, 2.0)
    result = w.check(tmp_path)
    assert frames_of(result, "ambed.open_invalid_accepted") == [2]
    assert frames_of(result, "ambed.no_reply") == [3]
    assert frames_of(result, "ambed.unparseable") == [4]


def test_fault_injection_stream_is_warned(tmp_path):
    """Streams opened with callsign N0FAULT break the protocol on purpose:
    their findings are warnings tagged intentional; others stay errors."""
    for callsign, severity in (("N0FAULT", "warn"), ("N0TEST", "error")):
        w = Wire()
        ctl(w, A.open_ext(callsign, A.PCM, (A.AMBEPLUS,), 1), A.std(1, PORT, A.PCM, A.AMBEPLUS, 1))
        stream(w, outs=(A.AMBEPLUS,), frames=3)
        ctl(w, A.close(1), None)
        stream(w, outs=(A.AMBEPLUS,), pids=[3, 4], skip_reply=(0, 1))
        ctl(w, A.ping("XLX999"), A.PONG, 2.0)
        result = w.check(tmp_path)
        found = [f for f in result["findings"] if f["code"] in ("ambed.data_after_close", "ambed.reply_missing")]
        assert found and {f["severity"] for f in found} == {severity}
        assert all(("intentional" in f["message"]) == (severity == "warn") for f in found)


# ---- injected transcoder faults: mark windows "ambed-fault-<mode>" -----------------------

RIG_SUT, RIG_AMBED = "172.30.0.11", "172.30.0.21"
T0 = 1000.0


def faulty_wire(gap=0.0):
    """OPEN with no answer (the fake in open=silent mode), then a stream whose
    pid 1 is never answered, both on the rig's sut <-> transcoder-sut link;
    the second OPEN comes gap s later."""
    w = Wire(t0=T0)
    w.send(RIG_SUT, A.PORT, RIG_AMBED, A.PORT, A.open_ext("DVR", A.PCM, (A.AMBEPLUS,), 1), 0.1)
    w.send(RIG_SUT, A.PORT, RIG_AMBED, A.PORT, A.open_ext("DVR", A.PCM, (A.AMBEPLUS,), 2), gap)
    w.send(RIG_AMBED, A.PORT, RIG_SUT, A.PORT, A.std(1, PORT, A.PCM, A.AMBEPLUS, 2), 0.1)
    for pid in range(3):
        w.send(RIG_SUT, PORT, RIG_AMBED, PORT, A.data_request(A.PCM, pid, b"\x00" * A.WIDTH[A.PCM]), 0.02)
        if pid != 1:
            w.send(RIG_AMBED, PORT, RIG_SUT, PORT, A.data_reply(pid, [(A.AMBEPLUS, A.fake_payload(A.AMBEPLUS))]), 0.001)
    w.send(RIG_SUT, A.PORT, RIG_AMBED, A.PORT, A.ping("DVR"), 3.0)
    w.send(RIG_AMBED, A.PORT, RIG_SUT, A.PORT, A.PONG, 0.001)
    return w


def check_rig(w, tmp_path, windows=()):
    from dvtest import check
    rig = check.rig_config("172.30.0.0/24")
    return check.check_file(w.save(tmp_path / "transcoder-sut.pcapng"), rig, windows=windows)


def severities(result):
    return {(f["code"], f["severity"]) for f in result["findings"] if f["code"].startswith("ambed.")
            and f["code"] not in ("ambed.close_unknown",)}


def test_ambed_findings_are_errors_without_a_window(tmp_path):
    result = check_rig(faulty_wire(), tmp_path)
    assert severities(result) == {("ambed.no_reply", "error"), ("ambed.reply_missing", "error")}
    assert result["errors"] == 2


def test_ambed_findings_inside_a_fault_window_are_expected(tmp_path):
    windows = [(RIG_AMBED, T0 - 1, T0 + 6, "mark ambed-fault-open-silent")]
    result = check_rig(faulty_wire(), tmp_path, windows)
    assert result["errors"] == 0
    excused = [f for f in result["findings"] if "[expected during mark ambed-fault-open-silent]" in f["message"]]
    assert {f["code"] for f in excused} == {"ambed.no_reply", "ambed.reply_missing"}
    assert all(f["severity"] == "warn" for f in excused)         # listed, never dropped


def test_ambed_findings_outside_the_fault_window_stay_errors(tmp_path):
    # the window ends before the unanswered requests are reported (and before pid 1)
    before = [(RIG_AMBED, T0 - 5, T0 + 0.05, "mark ambed-fault-open-silent")]
    result = check_rig(faulty_wire(), tmp_path, before)
    assert {c for c, s in severities(result) if s == "error"} == {"ambed.no_reply", "ambed.reply_missing"}
    # a window of another host (the tester's address) never covers the link
    other = [("172.30.0.30", T0 - 1, T0 + 10, "mark ambed-fault-open-silent")]
    assert check_rig(faulty_wire(), tmp_path, other)["errors"] == 2
    # a tester-side mark with another tag does not excuse the transcoder link either
    plain = [(RIG_AMBED, T0 - 1, T0 + 10, "mark usrp-keyup0")]
    assert check_rig(faulty_wire(), tmp_path, plain)["errors"] == 2


def test_ambed_fault_window_excuses_only_ambed_codes(tmp_path):
    from dvtest import check
    w = Wire(t0=T0)
    w.send(RIG_SUT, A.PORT, RIG_AMBED, A.PORT, b"garbage", 0.1)           # ambed.unparseable
    w.send(RIG_AMBED, 32003, RIG_SUT, 34003, b"\x00" * 7, 0.1)            # not an ambed port
    windows = [(RIG_AMBED, T0 - 1, T0 + 10, "mark ambed-fault-data-silent")]
    result = check.check_file(w.save(tmp_path / "transcoder-sut.pcapng"), check.rig_config("172.30.0.0/24"),
                              windows=windows)
    assert not [f for f in result["findings"] if f["severity"] == "error" and f["code"].startswith("ambed.")]
    assert any(f["code"] == "ambed.unparseable" and f["severity"] == "warn" for f in result["findings"])


def test_marks_builds_ambed_fault_windows(tmp_path):
    from dvtest import check
    (tmp_path / "control").mkdir()
    (tmp_path / "control" / "control.log").write_text(
        "2026-11-01T09:21:00.000Z request mark begin ambed-fault-open-silent\n"
        "2026-11-01T09:21:00.100Z mark begin ambed-fault-open-silent -> 0\n"
        "2026-11-01T09:21:30.000Z request mark end ambed-fault-open-silent\n"
        "2026-11-01T09:21:30.100Z mark end ambed-fault-open-silent -> 0\n"
        "2026-11-01T09:22:00.000Z request mark begin usrp-keyup0\n"
        "2026-11-01T09:22:00.100Z mark begin usrp-keyup0 -> 0\n"
        "2026-11-01T09:22:05.000Z request mark end usrp-keyup0\n"
        "2026-11-01T09:22:05.100Z mark end usrp-keyup0 -> 0\n"
        "2026-11-01T09:23:00.000Z request mark begin ambed-fault-worker-kill\n")
    hosts = check.rig_config("172.30.0.0/24")["hosts"]
    got = check.marks(str(tmp_path), hosts)
    assert [(ip, label) for ip, _t0, _t1, label in got] == [
        (RIG_AMBED, "mark ambed-fault-open-silent"), ("172.30.0.30", "mark usrp-keyup0"),
        (RIG_AMBED, "mark ambed-fault-worker-kill")]
    assert round(got[0][2] - got[0][1], 3) == 30.1 + check.AMBED_FAULT_SETTLE        # a few s after the end
    assert got[2][2] == float("inf")                                                    # never ended: to the capture end
    assert all(w[3] != got[0][3] for w in check.peer_marks(got, hosts))         # no copy for the tester's peers
    assert check.marks(str(tmp_path), {"tester": "172.30.0.30"})[0][3] == "mark usrp-keyup0"      # no transcoder host


def test_fake_fault_marks(tmp_path):
    """The cases bracket an injected fault: tag from the modes, one window per case."""
    import crossmode_fake as cf
    assert cf.fault_tag(open="busy") == "ambed-fault-open-busy"
    assert cf.fault_tag(data="delay:100") == "ambed-fault-data-delay-100"
    assert cf.fault_tag(open="silent", caps="changed") == "ambed-fault-caps-changed-open-silent"
    assert cf.fault_tag(max_sessions=1) == "ambed-fault-max_sessions-1"

    class Control:
        def __init__(self, fail=False):
            self.calls, self.fail = [], fail

        def mark(self, phase, tag):
            if self.fail:
                raise TimeoutError("no ack")
            self.calls.append((phase, tag))

    class Ctl:
        def set(self, **modes):
            return modes

    control = Control()
    fake = cf.Fake(Ctl(), "case", control)
    fake.set(open="busy")
    fake.set(data="silent")                              # second injection: same window
    fake.fault("worker-kill")
    fake.end_fault()
    fake.end_fault()
    assert control.calls == [("begin", "ambed-fault-open-busy"), ("end", "ambed-fault-open-busy")]
    quiet = cf.Fake(Ctl(), "case")                       # no control channel: no-op
    quiet.set(open="busy")
    quiet.end_fault()
    broken = cf.Fake(Ctl(), "case", Control(fail=True))  # a failing mark never fails the case
    broken.set(open="busy")
    broken.end_fault()
