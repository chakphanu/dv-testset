# SPDX-License-Identifier: MIT
"""Client for DV-Router's web service (dv-svc-web) as the rig's profile `web`
serves it: HTTPS through the TLS proxy, stdlib only. For the cases of the web
unit (cases/test_web_*.py); docs/rig.md, "Web profile".

What is here

  * TOTP, RFC 6238 (HMAC-SHA1, 6 digits, 30 s): `hotp`, `totp`, `Authenticator`
    (never reuses a counter, because the server accepts a code once);
  * `Pacer`: token buckets for the server's documented budgets, so a case cannot
    trip a limit by accident (login 1/s burst 10 per client address, register
    0.2/s burst 2 with at least 2 s between registrations, public reads 5/s burst
    20, authenticated reads 20/s burst 20, unsafe requests 2/s burst 10). One
    process-wide instance (`PACER`): the server counts per client address, and
    every client of this process has the same one;
  * `WebClient`: HTTPS with the run's CA, a cookie jar that records the cookie
    attributes, the CSRF token of the login, JSON in and out, Origin and
    Sec-Fetch-Site on every request, SSE reading, and `WebError` with the HTTP
    status and a scrubbed body;
  * the account flow, as the DV-Router tests do it (docs/design/web.md, "Web
    accounts"; crates/router/tests/real_children/accounts.rs): `login`,
    `enrol_totp`, `step_up_headers`, `create_invite`, `register_user`,
    `user_session`.

Paths are the API3 ones (/api/v1/{public,auth,rtc,user,operator}/...). Nothing
here prints a password, an invite code, a TOTP secret, a CSRF token or a cookie
value: they are registered with the client and replaced by *** in every error
message. Request bodies are never part of an error.

Findings (observed vs documented) are the case's business: `finding()` writes
them to $DVT_OUT/web-findings.txt and as a warning.
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import struct
import threading
import time
import urllib.parse
import warnings

USER_AGENT = "dvt-web/1"
COOKIE_NAME = "__Host-dv_web"          # web.md, "Hostnames and routing"
OPERATOR_FILE = "operator.env"         # out/<run>/web/ (run.sh)
CA_FILE = "ca.pem"                     # out/<run>/web/ (the web-proxy container)


# ---------------------------------------------------------------- TOTP (RFC 6238)

def hotp(key, counter, digits=6):
    """RFC 4226 HOTP with HMAC-SHA1; `key` is bytes."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp(key, at, digits=6, period=30):
    """RFC 6238 TOTP (HMAC-SHA1) of the unix time `at`; `key` is bytes."""
    return hotp(key, int(at // period), digits)


def b32decode(secret):
    """Bytes of a base32 secret (case and padding tolerant)."""
    text = secret.strip().replace(" ", "").upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


class Authenticator:
    """The operator's authenticator: codes from a base32 secret, each counter
    used at most once (the server's replay guard accepts a code once), and only
    counters within +-1 step of now (the documented tolerance). `next_code()`
    takes the current step when it is free, else the next one, else waits for
    the clock to come into range."""

    def __init__(self, secret_b32, digits=6, period=30, clock=time.time, sleep=time.sleep):
        self._key = b32decode(secret_b32)
        self.digits, self.period = digits, period
        self._clock, self._sleep = clock, sleep
        self._last = -1
        self._lock = threading.Lock()

    def __repr__(self):
        return "Authenticator(...)"

    def next_code(self):
        with self._lock:
            while True:
                now = int(self._clock() // self.period)
                counter = max(self._last + 1, now)
                if counter <= now + 1:
                    self._last = counter
                    return hotp(self._key, counter, self.digits)
                # counter is two steps ahead: wait until it is within one step
                self._sleep(max(0.05, (counter - 1) * self.period - self._clock() + 0.05))

    def wrong_code(self):
        """A well-formed code that is NOT valid now: it differs from the codes of
        the steps within +-2 of the clock (wider than the server's +-1 tolerance),
        so it is always a wrong guess. Uses no counter."""
        now = int(self._clock() // self.period)
        valid = {hotp(self._key, c, self.digits) for c in range(max(0, now - 2), now + 3)}
        for d in "0123456789":
            code = d * self.digits
            if code not in valid:
                return code
        raise AssertionError("unreachable: five valid codes cannot cover ten candidates")


# ------------------------------------------------------------------- pacing

class Pacer:
    """Token buckets. `wait(name)` blocks until a token is free and takes it.
    Each bucket is (rate per second, burst, min gap in seconds between uses);
    the defaults are the documented server budgets (module doc)."""

    BUDGETS = {
        "login": (1.0, 10, 0.0),         # per client prefix; global 4/s burst 40 is looser
        "register": (0.2, 2, 2.0),
        "public": (5.0, 20, 0.0),
        "session": (20.0, 20, 0.0),
        "action": (2.0, 10, 0.0),
    }

    def __init__(self, budgets=None, clock=time.monotonic, sleep=time.sleep, reserve=1):
        # `reserve` tokens of every burst stay unused as a safety margin (not below 1)
        self._b = {}
        for name, (rate, burst, gap) in (budgets or self.BUDGETS).items():
            cap = max(1, burst - reserve) if burst > 2 else burst
            self._b[name] = {"rate": rate, "cap": cap, "gap": gap, "tokens": float(cap), "t": None, "last": None}
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()

    def wait(self, name):
        """Seconds slept."""
        slept = 0.0
        with self._lock:
            b = self._b[name]
            while True:
                now = self._clock()
                if b["t"] is not None:
                    b["tokens"] = min(b["cap"], b["tokens"] + (now - b["t"]) * b["rate"])
                b["t"] = now
                need = 0.0
                if b["tokens"] < 1.0:
                    need = (1.0 - b["tokens"]) / b["rate"]
                if b["last"] is not None and now - b["last"] < b["gap"]:
                    need = max(need, b["gap"] - (now - b["last"]))
                if need <= 0:
                    b["tokens"] -= 1.0
                    b["last"] = now
                    return slept
                self._sleep(need + 0.01)
                slept += need + 0.01


PACER = Pacer()


def classify(method, path):
    """The pacer bucket of a request."""
    route = path.split("?", 1)[0]
    if route.startswith("/admin/"):                      # the admin prefix mount: same budgets, shorter path
        route = route[len("/admin"):]
    if route in ("/api/v1/auth/session", "/api/v1/admin/session") and method == "POST":
        return "login"
    if route in ("/health", "/ready", "/api/v1/admin/health", "/api/v1/admin/ready", "/api/v1/admin/version"):
        return "public"
    if route == "/api/v1/auth/register":
        return "register"
    if route.startswith("/api/v1/public/"):
        return "public"
    if method not in ("GET", "HEAD"):
        return "action"
    return "session"


# ------------------------------------------------------------------- client

class WebError(AssertionError):
    """An HTTP answer the caller did not expect: status and (scrubbed) body."""

    def __init__(self, message, status=None, body=""):
        super().__init__(message)
        self.status, self.body = status, body


class Response:
    def __init__(self, status, headers, body, cookies=()):
        self.status, self.headers, self.body = status, headers, body
        self.set_cookies = list(cookies)

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebError("body is not JSON (status %d): %s" % (self.status, self.text()[:200]),
                           self.status, self.text()[:200]) from exc


def parse_set_cookie(value):
    """(name, cookie value, attributes) of a Set-Cookie header; attribute names
    are lower case, flags map to True."""
    parts = [p.strip() for p in value.split(";")]
    name, _, val = parts[0].partition("=")
    attrs = {}
    for part in parts[1:]:
        key, eq, v = part.partition("=")
        attrs[key.strip().lower()] = v.strip() if eq else True
    return name, val, attrs


class WebClient:
    """One browser-like session against `base_url` (https://host[:port])."""

    def __init__(self, base_url, ca_file=None, timeout=20.0, user_agent=USER_AGENT, pacer=None, host_header=None):
        """`host_header` (host[:port]) makes the client present another host name
        than the one it connects to (Host and Origin), which is how a second
        virtual host of the same listener is reached (the admin prefix mount)."""
        url = urllib.parse.urlsplit(base_url)
        if url.scheme != "https" or not url.hostname:
            raise ValueError("base_url must be https://host[:port], got %r" % base_url)
        self.base_url = "%s://%s" % (url.scheme, url.netloc)
        self.host_header = host_header
        self.origin = "https://%s" % host_header if host_header else self.base_url
        self.host, self.port = url.hostname, url.port or 443
        self.timeout, self.user_agent = timeout, user_agent
        self.pacer = PACER if pacer is None else pacer
        self.cookies = {}          # name -> value
        self.cookie_attrs = {}     # name -> attributes of its last Set-Cookie
        self.csrf = None
        self.identity = None
        self.authenticator = None
        self.last_sse_headers = {}           # headers of the last sse() answer
        self._secrets = set()
        self._ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()

    # -- secrets never leave in an error
    def hide(self, *values):
        for v in values:
            if v and len(str(v)) >= 4:
                self._secrets.add(str(v))

    def scrub(self, text):
        for v in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(v, "***")
        return text

    # -- plumbing
    def _headers(self, method, headers, has_body):
        h = {"User-Agent": self.user_agent, "Accept": "application/json", "Origin": self.origin,
             "Connection": "close"}
        if method not in ("GET", "HEAD"):
            h["Sec-Fetch-Site"] = "same-origin"
            if self.csrf:
                h["X-DV-CSRF"] = self.csrf
        if has_body:
            h["Content-Type"] = "application/json"
        if self.cookies:
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        if self.host_header:
            h["Host"] = self.host_header
        h.update(headers or {})
        return {k: v for k, v in h.items() if v is not None}        # None removes a default header

    def _connect(self):
        return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._ctx)

    def _store_cookies(self, resp):
        raw = resp.headers.get_all("Set-Cookie") or []
        for value in raw:
            name, val, attrs = parse_set_cookie(value)
            self.hide(val)
            self.cookie_attrs[name] = attrs
            if val == "" or attrs.get("max-age") == "0":
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = val
        return raw

    def request(self, method, path, json_body=None, headers=None, expect=None, pace=True, raw_body=None):
        """One request; returns Response. `expect` (an int or a collection) makes
        any other status a WebError. The body is JSON from `json_body`, or the
        bytes of `raw_body` (set Content-Type in `headers`). A header whose value
        is None is left out (for example Origin)."""
        if pace:
            self.pacer.wait(classify(method, path))
        body = raw_body
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
        conn = self._connect()
        try:
            try:
                conn.request(method, path, body=body, headers=self._headers(method, headers, body is not None))
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                raise WebError("%s %s: %s: %s" % (method, path, type(exc).__name__, self.scrub(str(exc))[:200])) from exc
            cookies = self._store_cookies(resp)
            out = Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data, cookies)
        finally:
            conn.close()
        if expect is not None:
            ok = (expect,) if isinstance(expect, int) else tuple(expect)
            if out.status not in ok:
                raise self.error(method, path, out, ok)
        return out

    def error(self, method, path, resp, expected=None):
        text = self.scrub(resp.text())[:300]
        want = "" if expected is None else " (wanted %s)" % "/".join(str(e) for e in expected)
        return WebError("%s %s -> HTTP %d%s: %s" % (method, path.split("?", 1)[0], resp.status, want, text),
                        resp.status, text)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json_body=None, **kw):
        return self.request("POST", path, json_body=json_body if json_body is not None else {}, **kw)

    def sse(self, path, max_events=1, timeout=15.0, until=None):
        """Open an SSE stream and read frames until `max_events` frames, a frame
        whose event name is `until`, the stream's end, or `timeout` seconds in all
        (comment lines and heartbeats do not extend it). Returns
        (status, content_type, frames), frames being {"event", "id", "data",
        "retry"} dicts of the fields present (data joined with newlines). The
        connection is closed afterwards."""
        self.pacer.wait("public" if path.startswith("/api/v1/public/") else "session")
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=self._ctx)
        frames = []
        deadline = time.monotonic() + timeout
        try:
            conn.request("GET", path, headers=self._headers("GET", {"Accept": "text/event-stream"}, False))
            resp = conn.getresponse()
            ctype = resp.getheader("Content-Type", "")
            self.last_sse_headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status != 200:
                raise self.error("GET", path, Response(resp.status, {}, resp.read(4096)))
            cur = {}
            while len(frames) < max_events:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                conn.sock.settimeout(left)
                try:
                    line = resp.readline(65536)
                except TimeoutError:
                    break
                if not line:
                    break
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if cur:
                        frames.append(cur)
                        if until and cur.get("event") == until:
                            break
                        cur = {}
                elif not line.startswith(":"):
                    key, _, val = line.partition(":")
                    cur[key] = (cur[key] + "\n" if key == "data" and key in cur else "") + val.lstrip(" ")
            return resp.status, ctype, frames
        except (OSError, http.client.HTTPException) as exc:
            raise WebError("GET %s (SSE): %s: %s" % (path, type(exc).__name__, self.scrub(str(exc))[:200])) from exc
        finally:
            conn.close()

    # -- account flow
    def login(self, callsign, password, totp_code=None, expect=200):
        """POST /api/v1/auth/session. Returns the identity (dict, `csrf`
        included, which the client keeps for unsafe requests)."""
        self.hide(password, totp_code)
        body = {"callsign": callsign, "password": password}
        if totp_code:
            body["totp"] = totp_code
        resp = self.request("POST", "/api/v1/auth/session", json_body=body, expect=expect)
        if resp.status == 200:
            self.identity = resp.json()
            self.csrf = self.identity.get("csrf")
            self.hide(self.csrf)
        return resp.json() if resp.status == 200 else resp

    def session(self):
        """GET /api/v1/auth/session; refreshes the CSRF token."""
        ident = self.get("/api/v1/auth/session", expect=200).json()
        self.identity, self.csrf = ident, ident.get("csrf") or self.csrf
        self.hide(self.csrf)
        return ident

    def logout(self):
        self.request("DELETE", "/api/v1/auth/session", expect=(200, 204))
        self.cookies.clear()
        self.csrf = None

    def enrol_totp(self):
        """Operator TOTP enrolment: offer, then confirm one code. Returns the
        Authenticator (also kept as `self.authenticator`). The confirm rotates
        the cookie and opens the 5 minute step-up window."""
        offer = self.post("/api/v1/auth/session/totp", expect=200).json()
        secret = offer["secret"]
        self.hide(secret, offer.get("uri"))
        auth = Authenticator(secret, digits=int(offer.get("digits", 6)), period=int(offer.get("period", 30)))
        self.post("/api/v1/auth/session/totp/confirm", {"code": auth.next_code()}, expect=200)
        self.session()
        self.authenticator = auth
        return auth

    def step_up_headers(self):
        """Headers that satisfy a step-up gated request with a fresh code
        (X-DV-TOTP). The code is a one-use secret: it is registered for scrubbing."""
        if self.authenticator is None:
            raise WebError("no authenticator: enrol_totp() first")
        code = self.authenticator.next_code()
        self.hide(code)
        return {"X-DV-TOTP": code}

    def create_invite(self, callsign, role="user", may_tx=False, expires_in_secs=None):
        """POST /api/v1/operator/invites; returns the one-time code (a secret)."""
        body = {"callsign": callsign, "role": role, "may_tx": bool(may_tx)}
        if expires_in_secs is not None:
            body["expires_in_secs"] = int(expires_in_secs)
        data = self.post("/api/v1/operator/invites", body, expect=(200, 201)).json()
        code = data.get("code")
        if not isinstance(code, str) or not code:
            raise WebError("invite answer has no `code` field (keys: %s)" % sorted(data))
        self.hide(code)
        return code

    def register_user(self, callsign, password, may_tx=False, name=None, role="user"):
        """Invite `callsign` (this client must be an operator) and redeem the
        code from a fresh anonymous client. Returns the registration status."""
        code = self.create_invite(callsign, role, may_tx)
        anon = WebClient(self.base_url, timeout=self.timeout, user_agent=self.user_agent, pacer=self.pacer)
        anon._ctx = self._ctx
        anon.hide(code, password)
        self._secrets |= anon._secrets
        body = {"callsign": callsign, "invite": code, "password": password}
        if name:
            body["name"] = name
        resp = anon.request("POST", "/api/v1/auth/register", json_body=body, expect=(200, 201))
        return resp.status

    def user_session(self, callsign, password):
        """A new client logged in as `callsign`."""
        user = WebClient(self.base_url, timeout=self.timeout, user_agent=self.user_agent, pacer=self.pacer)
        user._ctx = self._ctx
        user.login(callsign, password)
        return user


def new_password():
    """A random password within the documented 8-1024 bytes."""
    return secrets.token_urlsafe(18)


# ----------------------------------------------------------- run environment

def read_env_file(path):
    """KEY=VALUE lines of a small env file (no quoting, # comments)."""
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip()
    return out


def finding(text, out_dir=None):
    """Record an observed-vs-documented difference: a line in
    $DVT_OUT/web-findings.txt and a pytest warning. Never encode it as an assertion."""
    line = "FINDING: " + text
    warnings.warn(line, UserWarning, stacklevel=2)
    out_dir = out_dir or os.environ.get("DVT_OUT")
    if out_dir:
        try:
            with open(os.path.join(out_dir, "web-findings.txt"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return line
