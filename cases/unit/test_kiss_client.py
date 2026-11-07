# SPDX-License-Identifier: MIT
"""dvtest/kiss_client.py (the tester as a KISS TCP client of a Dire Wolf, DP15 tier B) against the loopback FakeKissTnc as the
server. Offline: loopback sockets only."""
import time

import pytest

from dvtest import kiss_client, kiss_tnc

pytestmark = pytest.mark.unit

FRAME = kiss_tnc.ui_frame("N0TEST-7", "APDVT1", kiss_tnc.aprs_message_info("XLX999B", "DVT 1234", "3"), digis=("WIDE1-1",))


def test_send_reaches_the_server_and_injected_frames_arrive():
    with kiss_tnc.FakeKissTnc() as tnc:
        with kiss_client.KissClient("127.0.0.1", tnc.port) as client:
            assert tnc.wait_connected(5)
            assert client.send(FRAME)
            assert tnc.wait_data(1, timeout=5)[0][2] == FRAME
            t0 = time.monotonic()
            assert tnc.inject(FRAME)
            got = client.wait_frames(1, timeout=5, since=t0)
            assert [f for _t, _ch, f in got] == [FRAME]
            assert client.frames(time.monotonic() + 1) == []


def test_escapes_survive_both_ways():
    frame = kiss_tnc.ui_frame("N0TEST-7", "APDVT1", b":XLX999B  :\xc0\xdb\xdc\xdd end", digis=())
    with kiss_tnc.FakeKissTnc() as tnc:
        with kiss_client.KissClient("127.0.0.1", tnc.port) as client:
            assert tnc.wait_connected(5)
            client.send(frame)
            assert tnc.wait_data(1, timeout=5)[0][2] == frame
            tnc.inject(frame)
            assert client.wait_frames(1, timeout=5)[0][2] == frame


def test_non_data_commands_are_not_frames():
    with kiss_tnc.FakeKissTnc() as tnc:
        with kiss_client.KissClient("127.0.0.1", tnc.port) as client:
            assert tnc.wait_connected(5)
            tnc.inject_raw(b"\xc0\x06DIREWOLF\xc0")          # a SetHardware answer: command 6
            time.sleep(0.4)
            assert client.frames() == []
            assert [c for _t, _ch, c, _d in client.received] == [6]


def test_connect_gives_up_after_the_wait_and_send_fails_when_not_connected():
    client = kiss_client.KissClient("127.0.0.1", 1, name="none")
    assert client.connect(wait=0.6) is False
    assert client.send(FRAME) is False
    client.close()
