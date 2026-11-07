# SPDX-License-Identifier: MIT
"""The modem loop of rig/direwolf-entry.py (DP15 tier B, docs/dp15-cases.md): the pure parts (sample aligned datagrams, the
Dire Wolf configuration text, the FIFO relay with its silence tail, the KISS reader of the self test) without Dire Wolf and
without docker. The loop itself (two real Dire Wolf instances) is checked by `python3 -m dvtest.oracle_direwolf loop`."""
import importlib.util
import os
import time
from pathlib import Path

import pytest

from dvtest import ax25

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("direwolf_entry", ROOT / "rig" / "direwolf-entry.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def test_split_chunks_keeps_samples_whole():
    chunks, rest = entry.split_chunks(bytes(range(256)) * 9 + b"\x01", 1024)
    assert [len(c) for c in chunks] == [1024, 1024] and len(rest) == 257
    assert b"".join(chunks) + rest == (bytes(range(256)) * 9 + b"\x01")
    assert all(len(c) % 2 == 0 for c in chunks)
    chunks, rest = entry.split_chunks(b"\x01\x02\x03", 4)
    assert chunks == [] and rest == b"\x01\x02\x03"
    with pytest.raises(ValueError):
        entry.split_chunks(b"abc", 3)


def test_loop_conf_reads_udp_and_writes_through_the_alsa_file_plugin():
    conf = entry.loop_conf("N0TEST-8", 8001, 7355, "/tmp/dw/tx-a.raw")
    lines = conf.splitlines()
    assert lines[0] == "ADEVICE udp:7355 file:/tmp/dw/tx-a.raw"
    assert "MYCALL N0TEST-8" in lines and "MODEM 1200" in lines and "AGWPORT 0" in lines
    assert lines.index("KISSPORT 0") < lines.index("KISSPORT 8001")      # 0 first: it removes the built-in default 8001
    assert "KISSCOPY" not in lines
    assert "KISSCOPY" in entry.loop_conf("N0TEST-9", 8001, 7355, "/tmp/x", kisscopy=True).splitlines()
    assert conf.isascii() and conf.endswith("\n")


def test_relay_forwards_whole_samples_then_one_silence_tail(tmp_path):
    fifo = str(tmp_path / "tx.raw")
    os.mkfifo(fifo)
    got = []
    relay = entry.Relay(fifo, lambda d: got.append(d), rate=1000, speed=1000.0, tail_s=0.5, idle_s=0.1, chunk=64)
    relay.start()
    try:
        payload = bytes((i * 7) % 251 + 1 for i in range(300))             # no zero byte, so silence is recognisable
        fd = os.open(fifo, os.O_WRONLY)
        os.write(fd, payload[:101])                                       # an odd split of the stream
        os.write(fd, payload[101:])
        end = time.monotonic() + 5
        tail = b"\0" * (1000 // 64 * 64)                                   # 0.5 s of silence, whole pieces only
        while time.monotonic() < end and sum(len(d) for d in got) < 300 + (64 - 300 % 64) + len(tail):
            time.sleep(0.02)
        os.close(fd)
    finally:
        relay.stop()
    assert all(len(d) == 64 for d in got), [len(d) for d in got]
    data = b"".join(got)
    assert data[:300] == payload
    padded = 64 - 300 % 64                                                 # the last partial piece is padded with silence
    assert data[300:300 + padded] == b"\0" * padded
    assert data[300 + padded:] == b"\0" * len(tail)
    assert relay.bytes_in == 300


def test_kiss_reader_returns_data_frames_with_escapes_resolved():
    a = ax25.from_tnc2("N0TEST-7>APDVT1::XLX999B  :x")
    b = b"\xc0\xdb" + a
    stream = ax25.kiss_encode(0, 0, a) + ax25.kiss_encode(0, 1, b"\x1e") + ax25.kiss_encode(0, 0, b)
    assert entry.kiss_frames(stream) == [a, b]
    assert entry.kiss_frames(b"\xc0\x00" + a) == []                      # no closing FEND yet


def test_self_test_frame_is_a_valid_ui_message_frame():
    frame = entry.test_frame("N0TEST", 3, 40)
    parsed = ax25.parse_frame(frame)
    assert parsed["src"]["text"] == "N0TEST-7" and parsed["dest"]["text"] == "APDVT1"
    assert parsed["info"].startswith(b":XLX999B  :loop3 ") and parsed["info"].endswith(b"{3")
    assert len(entry.test_frame("N0TEST", 0, 600)) > 600


def test_loop_service_requires_the_peer(monkeypatch):
    monkeypatch.delenv("DW_PEER", raising=False)
    assert entry.run_loop_service() == 2


def test_udp_drops_is_a_mapping():
    assert isinstance(entry.udp_drops(), dict)


def test_loop_verdict_needs_both_directions_and_equal_frames():
    from dvtest import oracle_direwolf as o
    good = {"frames": [{"seq": 0, "direction": "a->b", "equal": True, "latency_s": 1.5}, {"seq": 1, "direction": "b->a", "equal": True, "latency_s": 1.2}]}
    assert o.loop_verdict(good)[0] is True
    assert o.loop_verdict({"frames": good["frames"][:1]})[0] is False
    bad = {"frames": [dict(good["frames"][0]), dict(good["frames"][1], equal=False)]}
    assert o.loop_verdict(bad)[0] is False
    assert o.loop_verdict({"error": "x"}) == (False, ["error: x"])
