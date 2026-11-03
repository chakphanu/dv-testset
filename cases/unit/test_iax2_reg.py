# SPDX-License-Identifier: MIT
"""dvtest.proto.iax2_reg against a fake phone-mode registrar on localhost (no rig): the frame sequence the client sends and how it
reads REGACK, REGREJ and silence."""
import socket
import struct
import threading

import pytest

from dvtest.proto import iax2
from dvtest.proto import iax2_reg as reg

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-real"
CHALLENGE = b"123456789"


class FakeRegistrar(threading.Thread):
    """Answers like the edge of ASL1g: empty CALLTOKEN -> CALLTOKEN; REGREQ with the token -> REGAUTH; REGREQ with MD5_RESULT ->
    REGACK when it matches the secret, else REGREJ (CAUSE authentication failed, CAUSECODE 29); a REGREQ without CALLTOKEN is
    dropped."""

    def __init__(self, secret=SECRET, answer=True):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.secret, self.answer = secret, answer
        self.seen = []                     # (subclass, set of IE codes) of every full frame received
        self.stop = False
        self.start()

    def reply(self, addr, req, subclass, ies, oseq=0):
        data = iax2.full_frame(7, req.source, 5, oseq, (req.out_sequence + 1) & 255, iax2.IAX, subclass, ies)
        self.sock.sendto(data, addr)

    def run(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if len(data) < 12 or not data[0] & 0x80:
                continue
            req = iax2.parse_full(data)
            ies = iax2.parse_information(req.payload)
            self.seen.append((req.subclass, set(ies)))
            if req.subclass != reg.REGREQ or not self.answer:
                continue
            if reg.IE_CALLTOKEN not in ies and reg.IE_MD5 not in ies:
                continue                                        # no token IE: silence
            if ies.get(reg.IE_CALLTOKEN) == b"":
                self.reply(addr, req, iax2.CALLTOKEN, iax2.information(reg.IE_CALLTOKEN, b"T" * 39))
            elif reg.IE_MD5 in ies:
                good = reg.md5_result(CHALLENGE, self.secret)
                if ies[reg.IE_MD5] == good:
                    self.reply(addr, req, reg.REGACK, iax2.information(reg.IE_USERNAME, ies[reg.IE_USERNAME])
                               + iax2.information(reg.IE_REFRESH, struct.pack("!H", 60)), oseq=1)
                else:
                    self.reply(addr, req, reg.REGREJ, iax2.information(reg.IE_CAUSE, b"authentication failed")
                               + iax2.information(reg.IE_CAUSECODE, b"\x1d"), oseq=1)
            else:
                self.reply(addr, req, reg.REGAUTH, iax2.information(reg.IE_AUTHMETHODS, struct.pack("!H", 2))
                           + iax2.information(reg.IE_CHALLENGE, CHALLENGE) + iax2.information(reg.IE_USERNAME, b"acct"))


@pytest.fixture
def registrar():
    made = []

    def make(**kw):
        made.append(FakeRegistrar(**kw))
        return made[-1]
    yield make
    for r in made:
        r.stop = True
        r.join(2)
        r.sock.close()


def test_md5_result_is_hex_md5_of_challenge_and_secret():
    assert reg.md5_result(b"123456789", SECRET) == b"1ace2b19f9a144fa81dc6edc5cdd13d8"
    assert len(reg.md5_result("1", "2")) == 32
    assert reg.md5_result("abc", "def") == reg.md5_result(b"abc", b"def")


def test_request_elements_carry_username_refresh_and_one_proof():
    ies = iax2.parse_information(reg.request_elements("acct", 60, token=b""))
    assert ies[reg.IE_USERNAME] == b"acct" and ies[reg.IE_REFRESH] == b"\x00\x3c" and ies[reg.IE_CALLTOKEN] == b""
    ies = iax2.parse_information(reg.request_elements("acct", 60, md5=b"x" * 32))
    assert reg.IE_MD5 in ies and reg.IE_CALLTOKEN not in ies
    assert reg.IE_CALLTOKEN not in iax2.parse_information(reg.request_elements("acct", 60))


def test_registration_walks_regreq_calltoken_regauth_regack(registrar):
    r = registrar()
    out = reg.register("127.0.0.1", "acct", SECRET, port=r.port, timeout=4)
    assert out.outcome == "REGACK" and out.refresh == 60
    assert out.steps == ["REGREQ", "CALLTOKEN", "REGREQ:token", "REGAUTH", "REGREQ:md5", "REGACK"]
    regreqs = [ies for sub, ies in r.seen if sub == reg.REGREQ]
    assert [reg.IE_CALLTOKEN in ies for ies in regreqs] == [True, True, False]       # the md5 request carries no token
    assert reg.IE_MD5 in regreqs[2]


def test_wrong_secret_is_regrej_with_cause(registrar):
    r = registrar()
    out = reg.register("127.0.0.1", "acct", "wrong-secret", port=r.port, timeout=4)
    assert out.outcome == "REGREJ" and out.cause == "authentication failed" and out.causecode == 29
    assert out.steps[-1] == "REGREJ" and "REGACK" not in out.steps


def test_no_calltoken_ie_is_silence(registrar):
    r = registrar()
    out = reg.register("127.0.0.1", "acct", SECRET, port=r.port, calltoken=False, timeout=1.0)
    assert out.outcome == "timeout" and out.steps == ["REGREQ"]
    assert out.seconds >= 0.9


def test_silent_peer_times_out(registrar):
    r = registrar(answer=False)
    out = reg.register("127.0.0.1", "acct", SECRET, port=r.port, timeout=0.6)
    assert out.outcome == "timeout"
