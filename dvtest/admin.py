# SPDX-License-Identifier: MIT
"""Client for DV-Router's admin service (dv-svc-admin) as the rig's profile `web`
serves it: HTTPS through the TLS proxy's second listener (https://dvt-admin:8443),
stdlib only. Builds on dvtest.web.WebClient (cookies, CSRF, TOTP, pacing).

Mounts (docs/design/admin.md "Mounts"):

  host     Host == [admin] host (dvt-admin:8443): API /api/v1/admin/..., cookie
           __Host-dv_admin (Path=/)
  prefix   any other host name, path /admin/...: API /admin/api/v1/admin/...,
           cookie __Secure-dv_admin (Path=/admin/); needs [admin] allow_from
           (the tester). The client presents the other name with host_header.
  direct   (legacy) no [admin] host, loopback only: not reachable from the tester.

The login answer carries the CSRF token (`csrf`), `totp` ("enrol", "ok", "off")
and the role. Everything secret is registered with the client and scrubbed
from errors, as in dvtest.web.
"""
from dvtest import web
from dvtest.api_admin import ADMIN_BASE, PREFIX_BASE

COOKIE_HOST = "__Host-dv_admin"
COOKIE_PREFIX = "__Secure-dv_admin"
COOKIE_DIRECT = "dv_admin"
ACCOUNTS_ENV = "admin.env"                       # out/<run>/web/ (run.sh)
PREFIX_HOST = "dvt-prefix.example"               # an arbitrary other host name for the prefix mount


class AdminClient(web.WebClient):
    """One session against the admin service on a mount ("host" or "prefix")."""

    def __init__(self, base_url, ca_file=None, mount="host", prefix_host=PREFIX_HOST, **kw):
        if mount not in ("host", "prefix"):
            raise ValueError("mount must be host or prefix")
        super().__init__(base_url, ca_file, host_header=prefix_host if mount == "prefix" else None, **kw)
        self.mount = mount
        self.api = ADMIN_BASE if mount == "host" else PREFIX_BASE
        self.root = "" if mount == "host" else "/admin"
        self.cookie_name = COOKIE_HOST if mount == "host" else COOKIE_PREFIX

    def path(self, suffix):
        """`/status` -> the full path of that admin API route on this mount."""
        return self.api + suffix

    def clone(self, **kw):
        """A new, logged-out client for the same mount and trust."""
        other = AdminClient(self.base_url, mount=self.mount, prefix_host=self.host_header or PREFIX_HOST, **kw)
        other._ctx = self._ctx
        other.pacer = self.pacer
        return other

    def login(self, user, password, totp_code=None, expect=200):
        """POST <api>/session; keeps the CSRF token. Returns the identity dict
        (or the Response when `expect` allows another status)."""
        self.hide(password, totp_code)
        body = {"user": user, "password": password}
        if totp_code:
            body["totp"] = totp_code
        resp = self.request("POST", self.path("/session"), json_body=body, expect=expect)
        if resp.status == 200:
            self.identity = resp.json()
            self.csrf = self.identity.get("csrf")
            self.hide(self.csrf)
            return self.identity
        return resp

    def session(self):
        ident = self.get(self.path("/session"), expect=200).json()
        self.identity, self.csrf = ident, ident.get("csrf") or self.csrf
        self.hide(self.csrf)
        return ident

    def logout(self):
        self.request("DELETE", self.path("/session"), expect=(200, 204))
        self.cookies.clear()
        self.csrf = None

    def enrol_totp(self):
        """First login of an account without a secret: offer, confirm one code.
        Returns the Authenticator (kept as `self.authenticator`)."""
        offer = self.post(self.path("/session/totp"), expect=200).json()
        secret = offer["secret"]
        self.hide(secret, offer.get("uri"))
        auth = web.Authenticator(secret, digits=int(offer.get("digits", 6)), period=int(offer.get("period", 30)))
        self.post(self.path("/session/totp/confirm"), {"code": auth.next_code()}, expect=200)
        self.session()
        self.authenticator = auth
        return auth

    def step_up(self):
        """Open the step-up window with a fresh code (POST /session/step-up)."""
        code = self.authenticator.next_code()
        self.hide(code)
        self.post(self.path("/session/step-up"), {"code": code}, expect=200)

    def login_totp(self, user, password):
        """Login of an enrolled account: the TOTP code goes with the password."""
        if self.authenticator is None:
            raise web.WebError("no authenticator: enrol first")
        return self.login(user, password, self.authenticator.next_code())


def read_accounts(path):
    """{role: (user, password)} from out/<run>/web/admin.env (run.sh writes it)."""
    env = web.read_env_file(path)
    out = {}
    for role in ("admin", "operator", "viewer"):
        user, pw = env.get("DVT_ADMIN_%s" % role.upper()), env.get("DVT_ADMIN_%s_PASSWORD" % role.upper())
        if user and pw:
            out[role] = (user, pw)
    return out
