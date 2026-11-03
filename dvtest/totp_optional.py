# SPDX-License-Identifier: MIT
"""Helpers of the TOTP1 cases (cases/test_web_totp_optional.py, docs/web-totp-optional-cases.md).

TOTP1 (DV-Router owner decision 2026-11-03): two-factor authentication is optional per account and
not enforced by default (web.totp_required / admin.totp_required unset). An account without a secret
logs in with its password only; it enables 2FA from its own settings after a password step-up; an
account with a secret always needs its code. These helpers drive that account flow on one of the two
surfaces that have it, the web service (/api/v1/auth/session...) and the admin service
(<admin api>/session...), through any dvtest.web.WebClient (an AdminClient is one).

They send requests and read answers; they never decide what is right: a case asserts what the specs
state and records the rest with dvtest.web.finding(). Nothing here prints a password, a secret or a
code; the clients scrub them (WebClient.hide).
"""
import binascii
import json
from dataclasses import dataclass

from dvtest import web

# every status a case may see from a refused step: anything else is a transport problem
ANY_STATUS = (200, 400, 401, 403, 404, 409, 422, 429, 500, 503)
REFUSED = (401, 403)                    # what the specs list for "not allowed without ..." (Forbidden, Unauthorized)

# throw-away callsigns of the cases: one account per (case letter, role), never a shared one
ACCOUNTS = {
    ("a", "user"): "N0TOA", ("b", "user"): "N0TOB", ("c", "user"): "N0TOC", ("d", "user"): "N0TOD",
    ("e", "operator"): "N0TOE", ("f", "user"): "N0TOF",
    ("a", "operator"): "N0TOG", ("b", "operator"): "N0TOH", ("c", "operator"): "N0TOI",
    ("d", "operator"): "N0TOJ", ("f", "operator"): "N0TOK",
}


def callsign_for(case, role):
    """The throw-away callsign of case `case` ("a".."f") for an account of role user / operator."""
    try:
        return ACCOUNTS[(case, role)]
    except KeyError:
        raise ValueError("no throw-away account for case %r, role %r" % (case, role)) from None


@dataclass(frozen=True)
class Surface:
    """Where the account flow lives: `session` is the path of the session resource; the 2FA routes are
    below it (/totp, /totp/confirm, /totp/disable, /step-up), as in openapi-web.yaml and openapi-admin.yaml."""
    name: str
    session: str

    def path(self, suffix=""):
        return self.session + suffix


WEB = Surface("web", "/api/v1/auth/session")


def admin_surface(client):
    """The surface of an AdminClient (host or prefix mount)."""
    return Surface("admin", client.path("/session"))


# ---------------------------------------------------------------- reading answers

def problem_code(resp):
    """The `code` of a JSON problem body, else None."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return data.get("code") if isinstance(data, dict) else None


def brief(resp):
    """`HTTP 403 step_up_required` (the status and, when there is one, the problem code)."""
    code = problem_code(resp)
    return "HTTP %d%s" % (resp.status, " " + code if code else "")


def body_json(resp):
    """The JSON object of an answer, {} when it is none."""
    try:
        data = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def unspecified(what, resp, listed):
    """The text of a FINDING for an answer the spec leaves open: what was done, what came back, what the
    spec lists. The caller passes it to dvtest.web.finding (dv-router rules on it)."""
    return "TOTP1: %s -> %s; the spec lists %s and names no single answer" % (what, brief(resp), listed)


def parse_offer(resp):
    """(secret, uri, digits, period) of a 2FA offer answer; ValueError when it is not one (status, JSON
    keys, base32 secret, otpauth totp uri)."""
    if resp.status != 200:
        raise ValueError("offer answered %s" % brief(resp))
    data = body_json(resp)
    for key in ("secret", "uri", "digits", "period"):
        if key not in data:
            raise ValueError("offer lacks %r (keys: %s)" % (key, sorted(data)))
    secret, uri = data["secret"], data["uri"]
    try:
        web.b32decode(secret)
    except (ValueError, binascii.Error, TypeError, AttributeError):
        raise ValueError("offer secret is not base32") from None
    if not isinstance(uri, str) or not uri.startswith("otpauth://totp/"):
        raise ValueError("offer uri is not an otpauth://totp/ uri")
    return secret, uri, int(data["digits"]), int(data["period"])


def no_store(resp):
    """True when the answer says Cache-Control: no-store."""
    return "no-store" in resp.headers.get("cache-control", "").lower()


# ---------------------------------------------------------------- the account flow

def try_login(client, user, password, code=None):
    """Log in without raising on a refusal. Returns the identity dict (HTTP 200, the client keeps the
    cookie and the CSRF token) or the Response."""
    return client.login(user, password, code, expect=ANY_STATUS)


def adopt_rotation(client, resp):
    """After an answer that rotates the login (step-up, confirm, disable): keep the new CSRF token. The
    web answers carry it; the admin ones do not, so the session is read again. The new cookie is already
    stored by the client."""
    csrf = body_json(resp).get("csrf")
    if isinstance(csrf, str) and csrf:
        client.csrf = csrf
        client.hide(csrf)
    else:
        client.session()


def step_up_password(client, surface, password):
    """POST <session>/step-up with the password (an account without a secret). Returns the Response; a 200
    is adopted (new cookie and CSRF token)."""
    client.hide(password)
    resp = client.post(surface.path("/step-up"), {"password": password})
    if resp.status == 200:
        adopt_rotation(client, resp)
    return resp


def step_up_empty(client, surface):
    """POST <session>/step-up with neither password nor code."""
    return client.post(surface.path("/step-up"), {})


def offer(client, surface):
    """POST <session>/totp (the offer of 2FA setup). Returns the Response."""
    resp = client.post(surface.path("/totp"))
    if resp.status == 200:
        try:
            secret, uri, _, _ = parse_offer(resp)
            client.hide(secret, uri)
        except ValueError:
            pass
    return resp


def confirm(client, surface, code):
    """POST <session>/totp/confirm with a code. Returns the Response; a 200 is adopted."""
    client.hide(code)
    resp = client.post(surface.path("/totp/confirm"), {"code": code})
    if resp.status == 200:
        adopt_rotation(client, resp)
    return resp


def disable(client, surface, password, code):
    """POST <session>/totp/disable with password and code. Returns the Response; a 200 is adopted."""
    client.hide(password, code)
    resp = client.post(surface.path("/totp/disable"), {"password": password, "code": code})
    if resp.status == 200:
        adopt_rotation(client, resp)
    return resp


def enable_totp(client, surface, password):
    """The whole documented enrolment of a full session without a secret: password step-up, offer, the first
    code. Returns the Authenticator (also `client.authenticator`). Raises WebError at the first step that
    does not answer 200."""
    resp = step_up_password(client, surface, password)
    if resp.status != 200:
        raise web.WebError("enrolment: step-up -> %s" % brief(resp), resp.status)
    resp = offer(client, surface)
    secret, _, digits, period = parse_offer(resp)
    auth = web.Authenticator(secret, digits=digits, period=period)
    resp = confirm(client, surface, auth.next_code())
    if resp.status != 200:
        raise web.WebError("enrolment: confirm -> %s" % brief(resp), resp.status)
    client.authenticator = auth
    return auth


def session_alive(client, surface):
    """(True, identity) when GET <session> still answers 200 for this client's cookie, else (False, Response)."""
    resp = client.get(surface.session)
    if resp.status == 200:
        return True, body_json(resp)
    return False, resp

