# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp15_worker.py (DP15 group 3): worker events of the public SSE, the down/up cycle, the
kill retry and the proc-stat time conversion; plus the over/M17 helpers moved into cases/data_support.py. Offline."""
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import data_support as ds  # noqa: E402
import test_dp15_worker as tw  # noqa: E402

pytestmark = pytest.mark.unit


class FakeWatch:
    def __init__(self, frames):
        self.frames = frames

    def snapshot(self):
        return list(self.frames)


def frame(at, **obj):
    return (at, {"event": "status", "data": json.dumps(obj)})


def test_worker_events_reads_only_data_worker_status_events():
    watch = FakeWatch([frame(1.0, type="status", ev="edge_up"), frame(2.0, ev="data_worker_down", worker="pkt"),
                       (2.5, {"event": "hello", "data": "not json"}), frame(3.0, ev="data_worker_up", worker="pkt", kind="data-worker"),
                       frame(0.5, ev="data_worker_down")])
    assert tw.worker_events(watch) == [(2.0, "data_worker_down"), (3.0, "data_worker_up"), (0.5, "data_worker_down")]
    assert tw.worker_events(watch, since=2.0) == [(2.0, "data_worker_down"), (3.0, "data_worker_up")]


def test_wait_cycle_is_down_then_the_first_up_after_it():
    watch = FakeWatch([frame(time.monotonic() - 5, ev="data_worker_up"), frame(time.monotonic() - 1, ev="data_worker_down"),
                       frame(time.monotonic() - 0.5, ev="data_worker_up")])
    t_down, t_up = tw.wait_cycle(watch, since=time.monotonic() - 2, timeout=1.0)
    assert t_down is not None and t_up is not None and t_up > t_down


def test_wait_cycle_times_out_with_what_it_has():
    now = time.monotonic()
    only_down = FakeWatch([frame(now, ev="data_worker_down")])
    t_down, t_up = tw.wait_cycle(only_down, since=now - 1, timeout=0.2)
    assert t_down == now and t_up is None
    assert tw.wait_cycle(FakeWatch([]), since=0.0, timeout=0.1) == (None, None)
    up_before_down = FakeWatch([frame(now + 1, ev="data_worker_down"), frame(now, ev="data_worker_up")])
    assert tw.wait_cycle(up_before_down, since=now - 1, timeout=0.1) == (now + 1, None)


class FakeControl:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    def kill_proc(self, service, process, signal, **kw):
        self.calls.append((service, process, signal))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def proc_stat(self, service, process):
        self.calls.append(("proc-stat", service, process))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_kill_worker_retries_while_there_is_no_process_and_gives_up_on_other_errors(monkeypatch):
    monkeypatch.setattr(tw.time, "sleep", lambda s: None)
    ok = SimpleNamespace(pids=[7], t_kill_monotonic=1.0)
    ctl = FakeControl([RuntimeError("kill-proc: error 3"), RuntimeError("kill-proc: error 3"), ok])
    assert tw.kill_worker(ctl) is ok and ctl.calls == [("xlxd-sut", "dv-data-worker", "KILL")] * 3
    with pytest.raises(RuntimeError):
        tw.kill_worker(FakeControl([RuntimeError("error refused")]))
    with pytest.raises(RuntimeError):
        tw.kill_worker(FakeControl([RuntimeError("error 3")] * 50), retry_s=0.0)


PROC_OUT = "pid=42\ncomm=dv-data-worker\nutime=5\nstime=3\nclk_tck=100\nthreads=4\nvmhwm_kb=100\nvmrss_kb=90\nfds=12\nstarttime=777\nt=1700000000.5\n"


def test_worker_sample_parses_proc_stat_and_maps_error_3_to_none():
    sample = tw.worker_sample(FakeControl([PROC_OUT]))
    assert sample.pid == 42 and sample.comm == "dv-data-worker" and sample.starttime == 777
    assert tw.worker_sample(FakeControl([RuntimeError("proc-stat xlxd-sut dv-data-worker: error 3")])) is None
    with pytest.raises(RuntimeError):
        tw.worker_sample(FakeControl([RuntimeError("error 1")]))


def test_wait_new_pid_skips_the_old_pid_and_the_gap(monkeypatch):
    monkeypatch.setattr(tw.time, "sleep", lambda s: None)
    err = RuntimeError("error 3")
    new_out = PROC_OUT.replace("pid=42", "pid=43")
    ctl = FakeControl([PROC_OUT, err, new_out])
    sample, at = tw.wait_new_pid(ctl, 42, timeout=5.0)
    assert sample.pid == 43 and abs(at - tw.epoch_to_monotonic(sample.t)) < 1e-6
    assert tw.wait_new_pid(FakeControl([PROC_OUT] * 3 + [err] * 200), 42, timeout=0.0) == (None, None)


def test_epoch_to_monotonic_is_the_same_clock_offset_as_time():
    assert abs(tw.epoch_to_monotonic(time.time()) - time.monotonic()) < 0.05


def test_pump_until_stops_early(monkeypatch):
    drained = []
    item = SimpleNamespace(client=SimpleNamespace(drain=lambda: drained.append(1)))
    state = {"n": 0}

    def until():
        state["n"] += 1
        return state["n"] >= 3

    assert tw.pump_until(5.0, [item], until=until) is True and len(drained) == 3
    assert tw.pump_until(0.05, [item]) is False


def test_the_moved_helpers_live_in_data_support():
    for name in ("ambe", "plain_fields", "build_over", "send_over", "send_over_thread", "pump_for", "header_times",
                 "text_headers", "over_by_my", "m17p_packets", "sms_text", "m17_voice_payloads"):
        assert callable(getattr(ds, name)), name
    assert ds.ROOM == "B"
    fields = ds.plain_fields(42)
    assert len(fields) == 42 and fields[0][0] == 0 and fields[21][0] == 0 and fields[1][0] == 1
    sid, packets = ds.build_over("N0TEST", fields, ds.ambe(42, "x"))
    assert len(packets) == 44 and packets[0].startswith(b"DSVT")


def test_sms_text_and_m17p_packets_read_the_sms_payload():
    assert ds.sms_text({"payload": b"\x05hello\x00"}) == "hello"
    assert ds.sms_text({"payload": b"\x02position"}) is None and ds.sms_text({"payload": b""}) is None
    packet = ds.sms_packet("hi there", "N0CALL")
    client = SimpleNamespace(client=SimpleNamespace(inbox=[packet, b"M17 junk"], inbox_times=[10.0, 11.0]))
    found = ds.m17p_packets(client)
    assert len(found) == 1 and found[0][0] == 10.0 and ds.sms_text(found[0][1]) == "hi there"
    assert tw.m17_text_times(client, "there") == [10.0] and tw.m17_text_times(client, "nope") == []


def text_over_datagrams(my, text, sid):
    from dvtest import slowdata
    from dvtest.proto import dextra
    out = [dextra.header_packet(sid, radio=dextra.radio_header(my=my, module="B"))]
    for seq, field in enumerate(slowdata.encode_text_over(text)):
        out.append(dextra.voice_packet(sid, seq, dextra.NULL_AMBE, slow=field))
    out.append(dextra.last_frame(sid, 21))
    return out


def test_dstar_text_times_finds_the_header_of_the_matching_text_over_only():
    first = text_over_datagrams("XLX999", "N0DWB: N 1234", 0x1111)
    second = text_over_datagrams("XLX999", "N0DWA: G 9999", 0x2222)
    inbox = first + second
    listener = SimpleNamespace(client=SimpleNamespace(inbox=inbox, inbox_times=[100.0 + i for i in range(len(inbox))]))
    assert tw.dstar_text_times(listener, "N 1234") == [100.0]
    assert tw.dstar_text_times(listener, "G 9999") == [100.0 + len(first)]
    assert tw.dstar_text_times(listener, "absent") == []
