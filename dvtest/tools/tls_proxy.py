# SPDX-License-Identifier: MIT
"""TLS-terminating reverse proxy for the rig service web-proxy (profile web).

DV-Router's web service speaks plain HTTP and sets its session cookie with the
__Host- prefix and Secure, so a browser (and the cases) need HTTPS in front of it.
This proxy is that front, stdlib only (ssl, http.server, http.client):

  client --TLS--> listen (default 0.0.0.0:443) --HTTP/1.1--> upstream (SUT :8081)

  * one thread per client connection (daemon threads, at most --max-conns open;
    one more gets 503), keep-alive towards the client, one upstream connection
    per request;
  * method, path, query, headers and body are forwarded as they are; hop-by-hop
    headers (Connection, Keep-Alive, Proxy-*, TE, Trailer, Transfer-Encoding,
    Upgrade) are dropped. Host is kept (the service compares the Origin with it)
    and also sent as X-Forwarded-Host;
  * X-Forwarded-Proto is set to https and X-Forwarded-For to the address of the
    TLS peer (a value sent by the client is discarded: this proxy is the edge);
  * request bodies are streamed (Content-Length or chunked) with no size buffer;
  * responses are streamed chunk by chunk and flushed at once, so SSE and long
    responses are never buffered; an upstream body of unknown length is sent
    chunked to the client;
  * Upgrade/WebSocket is not supported (501): the web service has none.

Certificates: --san-dns/--san-ip name the certificate. `make_certs` runs the
openssl CLI once at start: a throwaway CA signs the server certificate and its
key is deleted at once; the CA certificate is written to --ca-out (the file the
cases and a browser trust). The server key stays in a 0700 temp directory of the
container and is never written anywhere else.

Logging: one line per request with method, path WITHOUT the query, status, bytes
and milliseconds. Headers, cookies and bodies are never logged.

  python -m dvtest.tools.tls_proxy --listen 0.0.0.0:443 --upstream 172.30.0.11:8081 \\
      --ca-out /out/<run>/web/ca.pem --san-dns dvt-web --san-dns localhost --san-ip 172.30.0.90
"""
import argparse
import http.client
import http.server
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HOP_BY_HOP = frozenset((
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade"))
CHUNK = 16384
MAX_CONNS = 256


def make_certs(workdir, san_dns, san_ip, ca_out, days=2):
    """Create a CA and a server certificate for the given names with the openssl
    CLI. Writes the CA certificate to `ca_out` (mode 0644) and returns
    (server_cert_path, server_key_path) inside `workdir`. The CA key is deleted
    before this returns. RuntimeError carries openssl's message on failure."""
    names = ["DNS:%s" % n for n in san_dns] + ["IP:%s" % i for i in san_ip]
    if not names:
        raise RuntimeError("at least one SAN is needed")
    ca_key, ca_crt = os.path.join(workdir, "ca.key"), os.path.join(workdir, "ca.crt")
    key, csr, crt = (os.path.join(workdir, n) for n in ("server.key", "server.csr", "server.crt"))
    ext = os.path.join(workdir, "server.ext")
    with open(ext, "w", encoding="ascii") as fh:
        fh.write("basicConstraints=critical,CA:FALSE\n"
                 "keyUsage=critical,digitalSignature,keyEncipherment\n"
                 "extendedKeyUsage=serverAuth\n"
                 "subjectAltName=%s\n" % ",".join(names))

    def run(*args):
        proc = subprocess.run(("openssl",) + args, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("openssl %s failed: %s" % (args[0], proc.stderr.strip()[:300]))

    old = os.umask(0o077)
    try:
        run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca_crt,
            "-days", str(days), "-subj", "/CN=dvt web test CA", "-addext",
            "basicConstraints=critical,CA:TRUE,pathlen:0", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=dvt-web")
        run("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial", "-out", crt,
            "-days", str(days), "-extfile", ext)
    finally:
        os.umask(old)
        for name in (ca_key, csr, ext, os.path.join(workdir, "ca.srl")):
            try:
                os.unlink(name)
            except OSError:
                pass
    tmp = ca_out + ".tmp"
    shutil.copyfile(ca_crt, tmp)
    os.chmod(tmp, 0o644)
    os.replace(tmp, ca_out)
    return crt, key


def parse_hostport(value):
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("expected HOST:PORT, got %r" % value)
    return host, int(port)


class Proxy(http.server.ThreadingHTTPServer):
    """The listening server; `upstream` is (host, port)."""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, listen, upstream, ssl_context, handler, max_conns=MAX_CONNS,
                 upstream_timeout=300.0, quiet=False):
        self.upstream = upstream
        self.ssl_context = ssl_context
        self.upstream_timeout = upstream_timeout
        self.quiet = quiet
        self.slots = threading.BoundedSemaphore(max_conns)
        super().__init__(listen, handler)

    def handle_error(self, request, client_address):
        # a failed handshake or a reset is routine (scanners, health probes): one line, no traceback
        self.log("connection error from %s: %s" % (client_address[0], sys.exc_info()[0].__name__))

    def log(self, text):
        if not self.quiet:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dvt-web-proxy"
    sys_version = ""
    timeout = 600          # idle client connection

    def setup(self):
        # TLS handshake here, in the connection's own thread, so a slow client
        # never blocks accept()
        self.request.settimeout(15)
        self.request = self.server.ssl_context.wrap_socket(self.request, server_side=True)
        super().setup()          # connection = request, timeout = self.timeout

    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            self.close_connection = True
            try:
                # read the request head first: closing with it unread would reset the connection
                self.connection.settimeout(2)
                for _ in range(200):
                    if self.rfile.readline(8192) in (b"\r\n", b"\n", b""):
                        break
                self.wfile.write(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\n"
                                 b"Connection: close\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
            return
        try:
            super().handle()
        finally:
            self.server.slots.release()

    def log_message(self, fmt, *args):      # BaseHTTPRequestHandler's own lines (errors) go nowhere
        return

    def log_error(self, fmt, *args):
        return

    # every method is a proxied request
    def do_GET(self): self._proxy()
    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    def _client_body(self):
        """(body, length) for http.client: None, a reader of Content-Length bytes,
        or a generator of the de-chunked request body (length None)."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return self._dechunk(), None
        length = self.headers.get("Content-Length")
        if length is None:
            return None, None
        n = int(length)
        return _Limited(self.rfile, n), n

    def _dechunk(self):
        while True:
            line = self.rfile.readline(1024).strip()
            size = int(line.split(b";")[0] or b"0", 16)
            if size == 0:
                while self.rfile.readline(1024).strip():     # trailers
                    pass
                return
            remaining = size
            while remaining:
                data = self.rfile.read(min(remaining, CHUNK))
                if not data:
                    raise ConnectionError("client closed inside a chunk")
                remaining -= len(data)
                yield data
            self.rfile.readline(8)

    def _send_simple(self, status, text):
        body = (text + "\n").encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=us-ascii")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _proxy(self):
        start = time.monotonic()
        path = self.path
        status, sent = 502, 0
        try:
            if self.headers.get("Upgrade"):
                status = 501
                return self._send_simple(501, "upgrade is not supported")
            try:
                body, length = self._client_body()
            except ValueError:
                status = 400
                return self._send_simple(400, "bad request framing")
            headers = {}
            for key, value in self.headers.items():
                low = key.lower()
                if low in HOP_BY_HOP or low in ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
                                                "x-forwarded-port", "forwarded", "x-real-ip", "content-length"):
                    continue
                headers[key] = value if key not in headers else headers[key] + ", " + value
            host = self.headers.get("Host")
            headers["X-Forwarded-For"] = self.client_address[0]
            headers["X-Forwarded-Proto"] = "https"
            if host:
                headers["X-Forwarded-Host"] = host
            if length is not None:
                headers["Content-Length"] = str(length)
            up = http.client.HTTPConnection(*self.server.upstream, timeout=self.server.upstream_timeout)
            try:
                try:
                    up.request(self.command, path, body=body, headers=headers,
                               encode_chunked=length is None and body is not None)
                    resp = up.getresponse()
                except (OSError, http.client.HTTPException) as exc:
                    status = 502
                    self.server.log("upstream error %s: %s" % (type(exc).__name__, exc))
                    return self._send_simple(502, "bad gateway")
                status = resp.status
                sent = self._relay(resp)
            finally:
                up.close()
        except (OSError, ConnectionError):
            self.close_connection = True       # the client went away; nothing more to say
        finally:
            self.server.log("%s %s -> %d %d bytes %d ms" % (
                self.command, path.split("?", 1)[0], status, sent, (time.monotonic() - start) * 1000))

    def _relay(self, resp):
        """Send the upstream response; returns the body bytes sent."""
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        length = resp.getheader("Content-Length")
        self.send_response_only(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        chunked = False
        if no_body:
            if length is not None:
                self.send_header("Content-Length", length)
        elif length is not None:
            self.send_header("Content-Length", length)
        else:
            chunked = True
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        if no_body:
            return 0
        sent = 0
        while True:
            data = resp.read1(CHUNK)
            if not data:
                break
            sent += len(data)
            if chunked:
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
            else:
                self.wfile.write(data)
            self.wfile.flush()
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        return sent


class _Limited:
    """File-like view of the next `n` bytes of a stream (http.client reads it in blocks)."""

    def __init__(self, fh, n):
        self.fh, self.left = fh, n

    def read(self, size=-1):
        if self.left <= 0:
            return b""
        size = self.left if size is None or size < 0 else min(size, self.left)
        data = self.fh.read(size)
        if not data:
            raise ConnectionError("client closed inside the body")
        self.left -= len(data)
        return data


def server_context(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def make_server(listen, upstream, cert, key, **kw):
    return Proxy(listen, upstream, server_context(cert, key), Handler, **kw)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dvtest.tools.tls_proxy", description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", default="0.0.0.0:443")
    ap.add_argument("--upstream", required=True, help="HOST:PORT of the plain HTTP service")
    ap.add_argument("--ca-out", required=True, help="where the CA certificate (PEM) is written")
    ap.add_argument("--san-dns", action="append", default=[])
    ap.add_argument("--san-ip", action="append", default=[])
    ap.add_argument("--max-conns", type=int, default=MAX_CONNS)
    ap.add_argument("--upstream-timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    try:
        listen, upstream = parse_hostport(args.listen), parse_hostport(args.upstream)
    except ValueError as exc:
        print("tls_proxy: %s" % exc, file=sys.stderr)
        return 2
    workdir = tempfile.mkdtemp(prefix="dvt-tls-")
    os.chmod(workdir, 0o700)
    try:
        cert, key = make_certs(workdir, args.san_dns, args.san_ip, args.ca_out)
        srv = make_server(listen, upstream, cert, key, max_conns=args.max_conns,
                          upstream_timeout=args.upstream_timeout)
    except (RuntimeError, OSError) as exc:
        print("tls_proxy: %s" % exc, file=sys.stderr)
        return 2
    print("tls_proxy: listening on %s:%d, upstream %s:%d, CA %s" % (listen + upstream + (args.ca_out,)), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
