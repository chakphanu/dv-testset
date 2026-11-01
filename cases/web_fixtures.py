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
"""
import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

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
