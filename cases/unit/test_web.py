# SPDX-License-Identifier: MIT
"""dvtest/web.py: TOTP (RFC 6238 test vectors), pacing, cookie parsing and the
client's account flow against a small fake web service over TLS."""
import json
import shutil
import threading
import http.server

import pytest

from dvtest import web
from dvtest.tools import tls_proxy

pytestmark = pytest.mark.unit

# RFC 6238 appendix B, HMAC-SHA1, secret "12345678901234567890", 8 digits
RFC_KEY = b"12345678901234567890"
RFC_VECTORS = [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
               (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")]


@pytest.mark.parametrize("at,code", RFC_VECTORS)
def test_totp_rfc6238_vectors(at, code):
    assert web.totp(RFC_KEY, at, digits=8) == code
    assert web.totp(RFC_KEY, at, digits=6) == code[-6:]      # 6 digits are the last 6 of the 8


def test_hotp_rfc4226_vectors():
    expected = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
    assert [web.hotp(RFC_KEY, i) for i in range(10)] == expected


def test_b32decode_tolerates_case_space_and_padding():
    import base64
    secret = base64.b32encode(RFC_KEY).decode()
    assert web.b32decode(secret) == RFC_KEY
    assert web.b32decode(secret.rstrip("=").lower()) == RFC_KEY
    assert web.b32decode(" ".join([secret[:8], secret[8:]])) == RFC_KEY


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_authenticator_never_reuses_a_counter_and_stays_within_one_step():
    import base64
    secret = base64.b32encode(RFC_KEY).decode()
    clock = Clock(30 * 100 + 5.0)          # inside step 100
    auth = web.Authenticator(secret, clock=clock, sleep=clock.sleep)
    a = auth.next_code()
    assert a == web.hotp(RFC_KEY, 100)
    b = auth.next_code()                    # step 100 is used: step 101 is within +1
    assert b == web.hotp(RFC_KEY, 101)
    t0 = clock.t
    c = auth.next_code()                    # 102 would be two steps ahead: wait until it is +1
    assert c == web.hotp(RFC_KEY, 102)
    assert clock.t > t0 and int(clock.t // 30) >= 101
    assert "Authenticator(...)" == repr(auth)


def test_pacer_login_burst_then_rate():
    clock = Clock(0.0)
    pacer = web.Pacer(clock=clock, sleep=clock.sleep)
    # burst 10, one token kept in reserve: 9 immediate, then one per second
    waits = [pacer.wait("login") for _ in range(12)]
    assert waits[:9] == [0.0] * 9
    assert all(0.9 < w < 1.3 for w in waits[9:])


def test_pacer_register_keeps_two_seconds_and_the_budget():
    clock = Clock(0.0)
    pacer = web.Pacer(clock=clock, sleep=clock.sleep)
    pacer.wait("register")
    t1 = clock.t
    assert pacer.wait("register") >= 1.9           # min gap, though a burst token is free
    t2 = clock.t
    assert t2 - t1 >= 2.0
    third = pacer.wait("register")                 # burst 2 and 0.2/s: three registrations need 5 s in all
    assert third >= 2.9 and clock.t >= 5.0


def test_classify_routes_to_buckets():
    c = web.classify
    assert c("POST", "/api/v1/auth/session") == "login"
    assert c("GET", "/api/v1/auth/session") == "session"
    assert c("POST", "/api/v1/auth/register") == "register"
    assert c("GET", "/api/v1/public/state?x=1") == "public"
    assert c("POST", "/api/v1/operator/invites") == "action"
    assert c("GET", "/api/v1/user/me") == "session"


def test_parse_set_cookie():
    name, val, attrs = web.parse_set_cookie("__Host-dv_web=abc; Path=/; Secure; HttpOnly; SameSite=Strict")
    assert (name, val) == ("__Host-dv_web", "abc")
    assert attrs == {"path": "/", "secure": True, "httponly": True, "samesite": "Strict"}


def test_read_env_file(tmp_path):
    p = tmp_path / "e.env"
    p.write_text("# c\nA=1\n\nB = two words \n")
    assert web.read_env_file(p) == {"A": "1", "B": "two words"}


# ------------------------------------------------------------ fake web service

if not shutil.which("openssl"):
    pytest.skip("openssl CLI not installed", allow_module_level=True)

SECRET_B32 = "JBSWY3DPEHPK3PXP"


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = {}

    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200, cookie=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path.startswith("/api/v1/public/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for frame in (b": hi\n\n", b"event: hello\nid: 1-1\ndata: {\"a\":1}\n\n", b"event: x\ndata: y\ndata: z\n\n"):
                self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if self.path == "/api/v1/auth/session":
            if "__Host-dv_web=" not in (self.headers.get("Cookie") or ""):
                return self.send_json({"title": "no session"}, 401)
            return self.send_json({"callsign": "N0CALL", "role": "operator", "totp": "ok", "csrf": "csrf-token-2"})
        if self.path == "/boom":
            return self.send_json({"detail": "password hunter2hunter2 leaked"}, 500)
        self.send_json({"title": "not found"}, 404)

    def do_POST(self):
        data = self.body()
        if self.path == "/api/v1/auth/session":
            if data.get("password") != "pw-good-1234":
                return self.send_json({"title": "bad credentials"}, 401)
            return self.send_json({"callsign": data["callsign"], "role": "operator", "totp": "enrol",
                                   "csrf": "csrf-token-1"}, 200,
                                  "__Host-dv_web=cookie-value-1; Path=/; Secure; HttpOnly; SameSite=Strict")
        if self.path == "/api/v1/auth/register":
            ok = data.get("invite") == "invite-code-xyz-123" and "X-DV-CSRF" not in self.headers
            return self.send_json({}, 201 if ok else 401)
        if self.headers.get("X-DV-CSRF") not in ("csrf-token-1", "csrf-token-2") or not self.headers.get("Origin"):
            return self.send_json({"title": "csrf"}, 403)
        if self.path == "/api/v1/auth/session/totp":
            return self.send_json({"secret": SECRET_B32, "uri": "otpauth://totp/x?secret=" + SECRET_B32,
                                   "digits": 6, "period": 30})
        if self.path == "/api/v1/auth/session/totp/confirm":
            return self.send_json({}, 200, "__Host-dv_web=cookie-value-2; Path=/; Secure; HttpOnly; SameSite=Strict")
        if self.path == "/api/v1/operator/invites":
            Fake.state["invite"] = (data["callsign"], data["may_tx"])
            return self.send_json({"code": "invite-code-xyz-123"}, 201)
        self.send_json({"title": "not found"}, 404)

    do_PUT = do_DELETE = do_POST


@pytest.fixture(scope="module")
def fake(tmp_path_factory):
    work = tmp_path_factory.mktemp("webfake")
    ca = str(work / "ca.pem")
    cert, key = tls_proxy.make_certs(str(work), ["localhost"], ["127.0.0.1"], ca)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    ctx = tls_proxy.server_context(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "https://localhost:%d" % srv.server_address[1], ca
    srv.shutdown()


def fast_pacer():
    clock = Clock(0.0)
    return web.Pacer(clock=clock, sleep=clock.sleep)


def test_account_flow_against_the_fake(fake):
    url, ca = fake
    op = web.WebClient(url, ca, pacer=fast_pacer())
    ident = op.login("N0CALL", "pw-good-1234")
    assert ident["totp"] == "enrol" and op.csrf == "csrf-token-1"
    attrs = op.cookie_attrs[web.COOKIE_NAME]
    assert attrs["secure"] is True and attrs["httponly"] is True and attrs["samesite"] == "Strict"
    auth = op.enrol_totp()
    assert op.cookies[web.COOKIE_NAME] == "cookie-value-2" and op.csrf == "csrf-token-2"
    assert op.authenticator is auth
    hdr = op.step_up_headers()
    assert list(hdr) == ["X-DV-TOTP"] and len(hdr["X-DV-TOTP"]) == 6
    assert op.register_user("N0CALL1", "user-pw-5678", may_tx=True) == 201
    assert Fake.state["invite"] == ("N0CALL1", True)


def test_errors_carry_status_and_body_but_no_secrets(fake):
    url, ca = fake
    c = web.WebClient(url, ca, pacer=fast_pacer())
    c.hide("hunter2hunter2", "pw-bad-9999")
    with pytest.raises(web.WebError) as exc:
        c.login("N0CALL", "pw-bad-9999")
    assert exc.value.status == 401 and "HTTP 401" in str(exc.value) and "bad credentials" in str(exc.value)
    assert "pw-bad-9999" not in str(exc.value)
    with pytest.raises(web.WebError) as exc:
        c.get("/boom", expect=200)
    assert "HTTP 500" in str(exc.value) and "hunter2hunter2" not in str(exc.value) and "***" in str(exc.value)
    assert exc.value.status == 500


def test_connection_failure_is_a_clean_error(fake):
    _, ca = fake
    c = web.WebClient("https://127.0.0.1:9", ca, timeout=2, pacer=fast_pacer())
    with pytest.raises(web.WebError, match="GET /x"):
        c.get("/x")


def test_untrusted_server_is_refused(fake):
    url, _ = fake
    c = web.WebClient(url, None, pacer=fast_pacer())
    with pytest.raises(web.WebError, match="SSLCertVerificationError"):
        c.get("/api/v1/auth/session")


def test_sse_reader(fake):
    url, ca = fake
    status, ctype, events = web.WebClient(url, ca, pacer=fast_pacer()).sse("/api/v1/public/events", max_events=2)
    assert status == 200 and ctype == "text/event-stream"
    assert events[0] == {"event": "hello", "id": "1-1", "data": '{"a":1}'}
    assert events[1]["data"] == "y\nz"
    # `until` stops at the named event
    _, _, got = web.WebClient(url, ca, pacer=fast_pacer()).sse("/api/v1/public/events", max_events=9, until="hello")
    assert [f.get("event") for f in got] == ["hello"]


def test_base_url_must_be_https():
    with pytest.raises(ValueError):
        web.WebClient("http://dvt-web")


def test_finding_writes_the_file_and_warns(tmp_path):
    with pytest.warns(UserWarning, match="FINDING: observed x"):
        web.finding("observed x", out_dir=str(tmp_path))
    assert (tmp_path / "web-findings.txt").read_text() == "FINDING: observed x\n"
