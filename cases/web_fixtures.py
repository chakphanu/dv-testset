# SPDX-License-Identifier: MIT
"""Fixtures of the web unit (DV-Router's dv-svc-web behind the rig's TLS proxy,
`scripts/run.sh --profile web`, docs/rig.md "Web profile"). Imported by
conftest.py. Every fixture skips with the reason unless the profile is up.

  web_base_url   "https://dvt-web" (DVT_WEB_URL); the service answered
                 /api/v1/public/state with 200
  web_ca         path of the CA certificate the proxy wrote ($DVT_OUT/web/ca.pem,
                 DVT_WEB_CA): the trust anchor for HTTPS clients (and a browser)
  web_client     factory of anonymous WebClients (dvtest.web) with that CA
  web_operator   a WebClient logged in as the run's operator (N0CALL, password
                 from $DVT_OUT/web/operator.env), TOTP enrolled, step-up window open
  web_user       factory web_user(callsign=None, may_tx=False) -> WebUser: invites
                 and registers the account through the operator, logs it in; one
                 account per callsign for the whole session (each registration
                 waits for the server's register budget, dvtest.web.Pacer)
  web_operator2  a second operator (N0OP, own TOTP): the destructive and "authenticated
                 but throwaway" checks of cases/test_web_api.py use it, so the run's
                 first operator never loses its session
  web_url_admin  "https://dvt-admin:8443" (DVT_ADMIN_URL): the admin host answered /health
  admin_session  factory admin_session(role, mount="host") -> AdminClient logged in as
                 the run's admin / operator / viewer account (random passwords in
                 $DVT_OUT/web/admin.env), TOTP enrolled; skips with the reason when the
                 admin service is not reachable or the account file is missing
"""
import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dvtest import admin as admin_mod
from dvtest import web

WAIT_S = 90.0


@dataclass
class WebUser:
    callsign: str
    password: str = field(repr=False)
    may_tx: bool
    client: web.WebClient = field(repr=False)


def _out_dir():
    return Path(os.environ.get("DVT_OUT", "out"))


@pytest.fixture(scope="session")
def web_ca():
    if os.environ.get("DVT_WEB") != "on":
        pytest.skip("web profile not up: run scripts/run.sh --profile web (the tester gets DVT_WEB=on)")
    path = Path(os.environ.get("DVT_WEB_CA") or _out_dir() / "web" / web.CA_FILE)
    deadline = time.monotonic() + 30
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.5)
    if not path.is_file():
        pytest.skip("web profile is on but the proxy wrote no CA certificate at %s" % path)
    return str(path)


@pytest.fixture(scope="session")
def web_base_url(web_ca):
    url = os.environ.get("DVT_WEB_URL") or "https://dvt-web"
    client = web.WebClient(url, web_ca, timeout=5)
    deadline = time.monotonic() + WAIT_S
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            resp = client.get("/api/v1/public/state", pace=False)
            if resp.status == 200:
                return client.base_url
            last = "HTTP %d" % resp.status
        except web.WebError as exc:
            last = str(exc)
        time.sleep(1.0)
    pytest.skip("web profile is on but %s does not answer /api/v1/public/state with 200 within %d s (last: %s)"
                % (url, WAIT_S, last))


@pytest.fixture(scope="session")
def web_client(web_base_url, web_ca):
    def make():
        return web.WebClient(web_base_url, web_ca)
    return make


@pytest.fixture(scope="session")
def web_operator(web_base_url, web_ca):
    path = _out_dir() / "web" / web.OPERATOR_FILE
    if not path.is_file():
        pytest.skip("no operator login at %s (run.sh writes it with --profile web)" % path)
    env = web.read_env_file(path)
    callsign, password = env.get("DVT_WEB_OPERATOR"), env.get("DVT_WEB_OPERATOR_PASSWORD")
    if not callsign or not password:
        pytest.skip("%s lacks DVT_WEB_OPERATOR / DVT_WEB_OPERATOR_PASSWORD" % path)
    client = web.WebClient(web_base_url, web_ca)
    client.hide(password)
    ident = client.login(callsign, password)
    if ident.get("totp") != "enrol":
        pytest.fail("first operator login: totp state %r, wanted 'enrol' (accounts persisted from an earlier run?)"
                    % (ident.get("totp"),))
    client.enrol_totp()
    return client


@pytest.fixture(scope="session")
def web_user(web_operator):
    made = {}
    counter = itertools.count(1)

    def factory(callsign=None, may_tx=False):
        callsign = callsign or "N0CALL%d" % next(counter)
        if callsign in made:
            user = made[callsign]
            if user.may_tx != bool(may_tx):
                pytest.fail("web_user(%s) was made with may_tx=%s" % (callsign, user.may_tx))
            return user
        password = web.new_password()
        web_operator.hide(password)
        web_operator.register_user(callsign, password, may_tx)
        client = web_operator.user_session(callsign, password)
        made[callsign] = WebUser(callsign, password, bool(may_tx), client)
        return made[callsign]

    return factory


@pytest.fixture(scope="session")
def web_operator2(web_operator, web_base_url, web_ca):
    """A second operator account with its own TOTP (the first one stays untouched)."""
    password = web.new_password()
    web_operator.hide(password)
    web_operator.register_user("N0OP", password, may_tx=True, role="operator")
    client = web.WebClient(web_base_url, web_ca)
    client.hide(password)
    ident = client.login("N0OP", password)
    if ident.get("totp") != "enrol":
        pytest.fail("second operator first login: totp state %r, wanted 'enrol'" % (ident.get("totp"),))
    client.enrol_totp()
    return WebUser("N0OP", password, True, client)


@pytest.fixture(scope="session")
def web_url_admin(web_base_url, web_ca):
    if os.environ.get("DVT_WEB") != "on":
        pytest.skip("web profile not up")
    url = os.environ.get("DVT_ADMIN_URL") or "https://dvt-admin:8443"
    client = web.WebClient(url, web_ca, timeout=5)
    deadline = time.monotonic() + 45
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            resp = client.get("/health", pace=False)
            if resp.status == 200:
                return client.base_url
            last = "HTTP %d" % resp.status
        except web.WebError as exc:
            last = str(exc)
        time.sleep(1.0)
    pytest.skip("the admin host %s does not answer /health with 200 within 45 s (last: %s)" % (url, last))


@pytest.fixture(scope="session")
def admin_session(web_url_admin, web_ca):
    path = _out_dir() / "web" / admin_mod.ACCOUNTS_ENV
    if not path.is_file():
        pytest.skip("no admin logins at %s (run.sh writes them with --profile web)" % path)
    accounts = admin_mod.read_accounts(path)
    made = {}

    def factory(role, mount="host"):
        if role not in accounts:
            pytest.skip("%s has no %s account" % (path, role))
        key = (role, mount)
        if key not in made:
            user, password = accounts[role]
            client = admin_mod.AdminClient(web_url_admin, web_ca, mount=mount)
            client.hide(password)
            if mount == "host":
                ident = client.login(user, password)
                if ident.get("totp") == "enrol":
                    client.enrol_totp()
            else:
                # the account is enrolled by the host-mount session: same authenticator, a code with the password
                base = factory(role, "host")
                client.authenticator = base.authenticator
                client.login(user, password, base.authenticator.next_code())
                client.session()
            made[key] = (client, user, password)
        return made[key][0]

    factory.credentials = lambda role: accounts[role]
    return factory
