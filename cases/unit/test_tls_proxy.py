# SPDX-License-Identifier: MIT
"""dvtest/tools/tls_proxy.py (rig service web-proxy) against a local fake HTTP
backend: headers, bodies in both directions (large, chunked), streaming (SSE is
not buffered), pass-through statuses, errors, the connection cap and the
certificates made with the openssl CLI."""
import hashlib
import http.client
import http.server
import json
import os
import shutil
import socket
import ssl
import threading
import time

import pytest

from dvtest.tools import tls_proxy

pytestmark = pytest.mark.unit

if not shutil.which("openssl"):
    pytest.skip("openssl CLI not installed", allow_module_level=True)


class Backend(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _json(self, obj, status=200, extra=()):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_any(self):
        path = self.path
        if path.startswith("/idle"):                  # one frame, then silence until the peer (the proxy) closes
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            frame = b"event: hello\ndata: {}\n\n"
            self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
            self.wfile.flush()
            self.server.idle_ended = None
            t0 = time.monotonic()
            self.connection.settimeout(8)
            try:
                while self.connection.recv(1024):
                    pass
            except OSError:
                pass
            self.server.idle_ended = time.monotonic() - t0
            self.close_connection = True
            return
        if path.startswith("/sse"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(4):
                frame = b"event: tick\ndata: %d\n\n" % i
                self.wfile.write(b"%x\r\n" % len(frame) + frame + b"\r\n")
                self.wfile.flush()
                time.sleep(0.4)
            self.wfile.write(b"0\r\n\r\n")
            return
        if path.startswith("/big"):
            data = bytes(range(256)) * 24576          # 6 MiB
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/close"):                 # no length, no chunking: ends with the connection
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"x" * 100000)
            self.close_connection = True
            return
        if path.startswith("/304"):
            self.send_response(304)
            self.send_header("ETag", 'W/"1"')
            self.end_headers()
            return
        if path.startswith("/cookies"):
            self.send_response(204)
            self.send_header("Set-Cookie", "a=1; Secure")
            self.send_header("Set-Cookie", "b=2; HttpOnly")
            self.end_headers()
            return
        body = self._body()
        self._json({"method": self.command, "path": path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "len": len(body), "sha256": hashlib.sha256(body).hexdigest()},
                   404 if path.startswith("/missing") else 200)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    work = tmp_path_factory.mktemp("tls")
    ca = str(work / "ca.pem")
    cert, key = tls_proxy.make_certs(str(work), ["dvt-web", "localhost"], ["127.0.0.1"], ca)
    backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    backend.daemon_threads = True
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    proxy = tls_proxy.make_server(("127.0.0.1", 0), backend.server_address, cert, key, quiet=True,
                                  upstream_timeout=10)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    yield {"port": proxy.server_address[1], "ca": ca, "proxy": proxy, "backend": backend, "work": work}
    proxy.shutdown()
    backend.shutdown()


def conn(stack, host="127.0.0.1", timeout=10):
    ctx = ssl.create_default_context(cafile=stack["ca"])
    return http.client.HTTPSConnection(host, stack["port"], timeout=timeout, context=ctx)


def get_json(stack, method, path, body=None, headers=None, **kw):
    c = conn(stack)
    c.request(method, path, body=body, headers=headers or {}, **kw)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, (json.loads(data) if data else None)


def test_certificate_is_signed_by_the_written_ca(stack):
    work = stack["work"]
    assert os.stat(stack["ca"]).st_mode & 0o777 == 0o644
    assert not (work / "ca.key").exists() and not (work / "server.csr").exists()
    assert (os.stat(work / "server.key").st_mode & 0o077) == 0
    # verified by name: the host name and the address are both in the certificate
    ctx = ssl.create_default_context(cafile=stack["ca"])
    for name in ("dvt-web", "localhost", "127.0.0.1"):
        with socket.create_connection(("127.0.0.1", stack["port"]), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname=name) as tls:
                assert tls.getpeercert()["subjectAltName"]
    with socket.create_connection(("127.0.0.1", stack["port"]), timeout=5) as raw:
        with pytest.raises(ssl.SSLCertVerificationError):
            ctx.wrap_socket(raw, server_hostname="other.example")
    # without the CA the certificate is not trusted
    with pytest.raises(ssl.SSLCertVerificationError):
        c = http.client.HTTPSConnection("127.0.0.1", stack["port"], timeout=5, context=ssl.create_default_context())
        c.request("GET", "/")


def test_forwarding_headers_and_method(stack):
    r, got = get_json(stack, "PUT", "/a/b?q=1&r=2", body=b'{"k":1}', headers={
        "Content-Type": "application/json", "Cookie": "c=d", "X-Custom": "yes",
        "X-Forwarded-For": "192.0.2.9", "X-Forwarded-Proto": "http", "Origin": "https://dvt-web",
        "Connection": "keep-alive", "Upgrade-Insecure-Requests": "1"})
    assert r.status == 200
    h = got["headers"]
    assert got["method"] == "PUT" and got["path"] == "/a/b?q=1&r=2" and got["len"] == 7
    assert h["x-forwarded-proto"] == "https"
    assert h["x-forwarded-for"] == "127.0.0.1", "a client-sent X-Forwarded-For is discarded"
    assert h["x-forwarded-host"] == h["host"] == "127.0.0.1:%d" % stack["port"]
    assert h["cookie"] == "c=d" and h["x-custom"] == "yes" and h["origin"] == "https://dvt-web"
    assert h["content-type"] == "application/json"
    assert "keep-alive" not in h.get("connection", "").lower()


def test_status_and_empty_responses_pass_through(stack):
    r, got = get_json(stack, "GET", "/missing")
    assert r.status == 404 and got["path"] == "/missing"
    r, _ = get_json(stack, "HEAD", "/x")
    assert r.status == 200 and int(r.getheader("Content-Length")) > 0
    c = conn(stack)
    c.request("GET", "/304")
    r = c.getresponse()
    assert r.status == 304 and r.read() == b"" and r.getheader("ETag") == 'W/"1"'
    c.request("GET", "/cookies")                 # same keep-alive connection
    r = c.getresponse()
    r.read()
    assert r.status == 204
    assert r.headers.get_all("Set-Cookie") == ["a=1; Secure", "b=2; HttpOnly"]
    c.close()


def test_large_request_body_with_length(stack):
    data = os.urandom(3 * 1024 * 1024)
    r, got = get_json(stack, "POST", "/up", body=data, headers={"Content-Type": "application/octet-stream"})
    assert got["len"] == len(data) and got["sha256"] == hashlib.sha256(data).hexdigest()


def test_chunked_request_body(stack):
    parts = [os.urandom(40000), os.urandom(10), os.urandom(65536)]
    r, got = get_json(stack, "POST", "/up", body=iter(parts), encode_chunked=True,
                      headers={"Transfer-Encoding": "chunked"})
    assert got["len"] == sum(map(len, parts))
    assert got["sha256"] == hashlib.sha256(b"".join(parts)).hexdigest()


def test_large_response_and_unknown_length_response(stack):
    c = conn(stack)
    c.request("GET", "/big")
    r = c.getresponse()
    data = r.read()
    assert len(data) == 6 * 1024 * 1024 and data[:256] == bytes(range(256))
    c.request("GET", "/close")
    r = c.getresponse()
    assert r.getheader("Transfer-Encoding") == "chunked"       # length unknown upstream: chunked to the client
    assert r.read() == b"x" * 100000
    c.close()


def test_sse_is_streamed_not_buffered(stack):
    c = conn(stack, timeout=10)
    t0 = time.monotonic()
    c.request("GET", "/sse", headers={"Accept": "text/event-stream"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    first = r.readline()
    t_first = time.monotonic() - t0
    assert first == b"event: tick\n"
    assert t_first < 0.35, "first frame took %.2f s: the proxy buffers (the backend needs 1.6 s in all)" % t_first
    rest = r.read()
    assert rest.count(b"event: tick") == 3
    assert time.monotonic() - t0 >= 1.2
    c.close()


def test_client_close_closes_the_upstream_stream_at_once(stack):
    """A stream whose client goes away is closed upstream at once (the service frees its slot then, not at
    its next heartbeat)."""
    c = conn(stack, timeout=10)
    c.request("GET", "/idle", headers={"Accept": "text/event-stream"})
    r = c.getresponse()
    assert r.readline() == b"event: hello\n"
    stack["backend"].idle_ended = None
    c.close()
    deadline = time.monotonic() + 3
    while stack["backend"].idle_ended is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stack["backend"].idle_ended is not None, "the upstream connection stayed open after the client closed"


def test_upstream_down_is_502_and_upgrade_501(stack):
    c = conn(stack)
    c.request("GET", "/x", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
    r = c.getresponse()
    r.read()
    assert r.status == 501
    c.close()
    dead = tls_proxy.make_server(("127.0.0.1", 0), ("127.0.0.1", 9), *_cert(stack), quiet=True)
    threading.Thread(target=dead.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context(cafile=stack["ca"])
        c = http.client.HTTPSConnection("127.0.0.1", dead.server_address[1], timeout=5, context=ctx)
        c.request("GET", "/")
        assert c.getresponse().status == 502
        c.close()
    finally:
        dead.shutdown()


def _cert(stack):
    return str(stack["work"] / "server.crt"), str(stack["work"] / "server.key")


def test_connection_cap(stack):
    small = tls_proxy.make_server(("127.0.0.1", 0), stack["backend"].server_address, *_cert(stack),
                                  quiet=True, max_conns=1)
    threading.Thread(target=small.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context(cafile=stack["ca"])
        first = http.client.HTTPSConnection("127.0.0.1", small.server_address[1], timeout=5, context=ctx)
        first.request("GET", "/x")
        assert first.getresponse().read()           # keep-alive: still holds the one slot
        second = http.client.HTTPSConnection("127.0.0.1", small.server_address[1], timeout=5, context=ctx)
        second.request("GET", "/x")
        assert second.getresponse().status == 503
        first.close()
        second.close()
    finally:
        small.shutdown()


def test_parse_hostport_and_cli_errors(capsys):
    assert tls_proxy.parse_hostport("0.0.0.0:443") == ("0.0.0.0", 443)
    for bad in ("443", "host:", "host:0", "host:99999", ":80"):
        with pytest.raises(ValueError):
            tls_proxy.parse_hostport(bad)
    assert tls_proxy.main(["--upstream", "nonsense", "--ca-out", "/nonexistent/ca.pem"]) == 2
    assert "HOST:PORT" in capsys.readouterr().err


def test_parse_route():
    assert tls_proxy.parse_route("0.0.0.0:8443=172.30.0.11:8080") == (("0.0.0.0", 8443), ("172.30.0.11", 8080))
    for bad in ("0.0.0.0:8443", "0.0.0.0:8443=", "=172.30.0.11:8080", "a:1=b:x"):
        with pytest.raises(ValueError):
            tls_proxy.parse_route(bad)
    assert tls_proxy.main(["--upstream", "127.0.0.1:1", "--route", "oops", "--ca-out", "/nonexistent/ca.pem"]) == 2


def test_extra_route_serves_its_own_upstream(stack):
    """A second server over the same certificate forwards to another backend (the admin host)."""
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    other.daemon_threads = True
    threading.Thread(target=other.serve_forever, daemon=True).start()
    cert = str(stack["work"] / "server.crt")
    key = str(stack["work"] / "server.key")
    second = tls_proxy.make_server(("127.0.0.1", 0), other.server_address, cert, key, quiet=True, upstream_timeout=10)
    threading.Thread(target=second.serve_forever, daemon=True).start()
    try:
        ctx = ssl.create_default_context(cafile=stack["ca"])
        c = http.client.HTTPSConnection("127.0.0.1", second.server_address[1], timeout=10, context=ctx)
        c.request("GET", "/who", headers={"Host": "dvt-admin:8443"})
        r = c.getresponse()
        got = json.loads(r.read())
        c.close()
        assert r.status == 200 and got["headers"]["host"] == "dvt-admin:8443"
        assert got["headers"]["x-forwarded-host"] == "dvt-admin:8443"
        assert got["headers"]["x-forwarded-proto"] == "https"
    finally:
        second.shutdown()
        other.shutdown()
