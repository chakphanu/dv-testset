# SPDX-License-Identifier: MIT
"""dvtest/aprsis_client.py (the tester's tiny APRS-IS client for the private aprsc, DP15-5a / 5b) against an in-test fake
server on loopback: the login (verified and unverified), the filter, a message round trip with an ack, the line helpers.
Offline: loopback sockets only."""
import socket
import threading
import time

import pytest

from dvtest import aprs_passcode as ap
from dvtest import aprsis_client as ai

pytestmark = pytest.mark.unit


class FakeServer:
    """Behaves like the part of aprsc the client uses: banner, `# logresp`, a keepalive comment, and an ack for every
    message to XLX999B that carries an id (source = the room, path TCPIP*, then a q construct)."""

    def __init__(self, banner=True, answer=True, drop_after_login=False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.banner, self.answer, self.drop_after_login = banner, answer, drop_after_login
        self.logins, self.lines = [], []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        conn.settimeout(5.0)
        with conn:
            buf = b""
            if self.banner:
                conn.sendall(b"# fake-aprsd 1.0\r\n")
            first = True
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                    while b"\r\n" in buf:
                        raw, buf = buf.split(b"\r\n", 1)
                        line = raw.decode("ascii")
                        if first:
                            first = False
                            self.logins.append(line)
                            if not self.answer:
                                continue
                            words = line.split()
                            ok = words[3] == str(ap.aprs_passcode(words[1]))
                            conn.sendall(("# logresp %s %s, server FAKE\r\n" % (words[1], "verified" if ok else "unverified")).encode())
                            conn.sendall(b"# keepalive comment\r\n")
                            if self.drop_after_login:
                                return
                            continue
                        self.lines.append(line)
                        parsed = ai.parse_line(line)
                        msg = ai.parse_message(parsed["info"]) if parsed else None
                        if msg and msg[0] == "XLX999B" and msg[2]:
                            ack = "XLX999B>APZDVR,TCPIP*,qAS,N0TEST-5::%s:ack%s\r\n" % (parsed["src"].ljust(9), msg[2])
                            conn.sendall(ack.encode())
            except (OSError, socket.timeout):
                return

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    fake = FakeServer()
    yield fake
    fake.close()


def test_login_message_round_trip_and_filter(server):
    with ai.AprsisClient("127.0.0.1", server.port, login="N0TEST-7", filter="g/XLX999B") as client:
        logresp = client.connect()
        assert logresp == {"login": "N0TEST-7", "verified": True, "server": "FAKE"}
        assert client.banner == "# fake-aprsd 1.0"
        mark = client.mark()
        line = client.send_message("N0TEST-7", "XLX999B", "DVT 1234", "12")
        assert line == "N0TEST-7>APDVT1,TCPIP*::XLX999B  :DVT 1234{12"
        hit = client.wait_packet(lambda p: p["src"] == "XLX999B", 3.0, since=mark)
        assert hit is not None
        _at, parsed, raw = hit
        assert raw == "XLX999B>APZDVR,TCPIP*,qAS,N0TEST-5::N0TEST-7 :ack12"
        assert parsed["before_q"] == ["TCPIP*"] and parsed["q"] == ["qAS", "N0TEST-5"]
        assert ai.ack_id(parsed["info"]) == "12"
        assert [x[1] for x in client.sent] == [line]
    assert server.logins == ["user N0TEST-7 pass %d vers dvtest 1.0 filter g/XLX999B" % ap.aprs_passcode("N0TEST-7")]
    assert server.lines == [line]


def test_comment_lines_are_not_packets(server):
    with ai.AprsisClient("127.0.0.1", server.port, login="N0TEST-7") as client:
        client.connect()
        time.sleep(0.2)
        assert client.snapshot() == []
        assert [line for _t, line in client.comments][-1] == "# keepalive comment"
    assert server.logins[0].endswith("vers dvtest 1.0")      # no filter option without a filter


def test_unverified_login_raises_unless_allowed(server):
    with ai.AprsisClient("127.0.0.1", server.port, login="N0TEST-7", passcode=1) as client:
        with pytest.raises(ai.AprsisError, match="unverified"):
            client.connect()
    other = FakeServer()
    try:
        with ai.AprsisClient("127.0.0.1", other.port, login="N0TEST-7", passcode=-1) as client:
            assert client.connect(require_verified=False)["verified"] is False
    finally:
        other.close()


def test_no_banner_or_no_logresp_is_an_error():
    for kw, text in ((dict(banner=False), "no banner"), (dict(answer=False), "no logresp")):
        fake = FakeServer(**kw)
        try:
            with ai.AprsisClient("127.0.0.1", fake.port, timeout=0.6) as client:
                with pytest.raises(ai.AprsisError, match=text):
                    client.connect()
        finally:
            fake.close()


def test_wait_packet_times_out_and_matching_filters(server):
    with ai.AprsisClient("127.0.0.1", server.port) as client:
        client.connect()
        assert client.wait_packet(lambda p: True, 0.2) is None
        client.send_message("N0TEST-7", "XLX999B", "a", "1")
        client.send_message("N0TEST-7", "XLX999B", "b", "2")
        assert client.wait_packet(lambda p: p["info"].endswith("ack2"), 3.0) is not None
        acks = client.matching(lambda p: p["src"] == "XLX999B")
        assert [ai.ack_id(p["info"]) for _t, p in acks] == ["1", "2"]


def test_connection_closed_is_reported():
    fake = FakeServer(drop_after_login=True)
    try:
        with ai.AprsisClient("127.0.0.1", fake.port) as client:
            client.connect()
            time.sleep(0.3)
            assert client.error and "closed" in client.error
    finally:
        fake.close()


def test_line_helpers():
    assert ai.pad_addressee("XLX999B") == "XLX999B  "
    assert ai.message_info("N0TEST-7", "ack12") == ":N0TEST-7 :ack12"
    assert ai.message_info("XLX999B", "hi") == ":XLX999B  :hi"
    assert ai.message_line("N0TEST-7", "XLX999B", "hi", "7", dest="APRS") == "N0TEST-7>APRS,TCPIP*::XLX999B  :hi{7"
    assert ai.parse_message(":XLX999B  :hello{12") == ("XLX999B", "hello", "12")
    assert ai.parse_message(":XLX999B  :hello") == ("XLX999B", "hello", None)
    assert ai.parse_message("!1030.00N/02015.00E>") is None
    assert ai.ack_id(":N0TEST-7 :ack12") == "12" and ai.ack_id(":N0TEST-7 :hello") is None and ai.ack_id(":N0TEST-7 :ack") is None
    assert ai.login_line("N0TEST-7", 15043) == "user N0TEST-7 pass 15043 vers dvtest 1.0"
    assert ai.parse_logresp("# logresp N0TEST-7 verified, server DVTEST") == {"login": "N0TEST-7", "verified": True, "server": "DVTEST"}
    assert ai.parse_logresp("# logresp N0TEST-7 unverified, server DVTEST")["verified"] is False
    assert ai.parse_logresp("# aprsc 3.0.0") is None
    with pytest.raises(ValueError):
        ai.pad_addressee("TOOLONGCALL")
    with pytest.raises(ValueError):
        ai.message_info("XLX999B", "two\r\nlines")
    with pytest.raises(ValueError):
        ai.message_info("XLX999B", "x", msgid="123456")


def test_parse_line_q_constructs():
    plain = ai.parse_line("XLX999B>APZDVR,TCPIP*::N0TEST-7 :ack12")
    assert plain["path"] == ["TCPIP*"] and plain["q"] == [] and plain["before_q"] == ["TCPIP*"]
    served = ai.parse_line("N0TEST-7>APDVT1,TCPIP*,qAC,DVTEST::XLX999B  :hi{1")
    assert served["before_q"] == ["TCPIP*"] and served["q"] == ["qAC", "DVTEST"] and served["info"] == ":XLX999B  :hi{1"
    assert ai.parse_line("# logresp x") is None
    assert ai.parse_line("no colon here") is None


def test_overlong_line_is_refused(server):
    with ai.AprsisClient("127.0.0.1", server.port) as client:
        client.connect()
        with pytest.raises(ValueError):
            client.send_line("N0TEST-7>APDVT1,TCPIP*:>" + "x" * 600)
