# SPDX-License-Identifier: MIT
"""New coverage of TOTP1 (DV-Router owner decision 2026-11-03): two-factor authentication is OPTIONAL per
account and not enforced by default. Cases for the default policy, web.totp_required and
admin.totp_required both unset.

THIS FILE NEEDS THE DEFAULT POLICY. Run it with DVT_SUT_CONFIG_EXTRA = web.toml:admin.toml (run.sh adds
both for --profile web) and WITHOUT rig/config/dv-router/totp-required.toml: that snippet turns the
mandatory policy on (first operator / admin login = "enrol"), which the other web cases need and these
ones are the opposite of. With it, the first test fails ("the first operator login says enrol") and the
rest skip with the reason. Run it in a rig of its own (a run.sh invocation: a fresh state, so the first
operator and the admin accounts have no secret yet). docs/web-totp-optional-cases.md.

Documented behaviour only (docs/api/openapi-web.yaml, openapi-admin.yaml of DV-Router; the TOTP1 review note) plus
the exact answers dv-router ruled on 2026-11-04 (the first run, 20261103T203002Z, recorded them as FINDINGs): a wrong or
missing second factor is 403 totp_invalid (wrong first code at confirm, an enrolled account stepping up with its password
only, disable with a wrong code, and the ADMIN wrong password at step-up and at disable); a missing reauthentication window
is 403 step_up_required (web ids command, admin GET /clients, admin offer). Asserted as (status, problem code), constants in
dvtest/totp_optional.py.

TOTP1b PENDING: the WEB side answers a wrong password at the step-up and at disable with 401 unauthorized, admin with 403
totp_invalid (an inconsistency dv-router will fix). The web cases assert the CURRENT 401 through
dvtest.totp_optional.WEB_WRONG_PASSWORD and write a "TOTP1b pending" note line; when the fix lands they fail with 403
totp_invalid until that one constant is changed. What dv-router did not rule on (the answer to a made-up code on an account
without a secret, an empty step-up request) is asserted as "refused, not 200" only.

Accounts: every flow runs on a throw-away account (N0TOA..N0TOK, dvtest.totp_optional.ACCOUNTS), invited
by the run's bootstrap operator (N0CALL, which is only logged in with its password, never enrolled here).
The admin service has no API to make accounts: the admin cases use the run's `viewer` login of
out/<run>/web/admin.env and leave it without a secret again (the lifecycle ends with a disable).

  a  password-only login: totp "off", step_up false, full session            user, operator, admin viewer
  b  enable 2FA: password step-up first (403 without), offer, first code      user, operator, admin viewer
  c  after enabling, login needs the code (401 totp_required, no cookie)       user, operator, admin viewer
  d  disable needs password AND current code                                  user, operator, admin viewer
  e  a sensitive action without a window is refused; the password step-up
     opens it                                                                 operator (ids command), admin viewer (clients)
  f  wrong password on the enable / disable step: an error, the session lives  user, operator, admin viewer
"""
import os
import time
from pathlib import Path

import pytest

from dvtest import admin as admin_mod
from dvtest import totp_optional as to
from dvtest import web

pytestmark = [pytest.mark.rig, pytest.mark.web]

ROLES = ["user", "operator"]
STEP_UP_MAX_S = 300                     # "five minute reauthentication window"
IDS_LIST = {"v": 1, "op": "list", "kind": "dmr"}        # the smoke case's ids command


def _out_dir():
    return Path(os.environ.get("DVT_OUT", "out"))


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def bootstrap(web_base_url, web_ca):
    """The run's first operator logged in with its password only (this file never enrols it). Skips when
    the account already has a secret (not a fresh state) and says so."""
    path = _out_dir() / "web" / web.OPERATOR_FILE
    if not path.is_file():
        pytest.skip("no operator login at %s (run.sh writes it with --profile web)" % path)
    env = web.read_env_file(path)
    callsign, password = env.get("DVT_WEB_OPERATOR"), env.get("DVT_WEB_OPERATOR_PASSWORD")
    if not callsign or not password:
        pytest.skip("%s lacks DVT_WEB_OPERATOR / DVT_WEB_OPERATOR_PASSWORD" % path)
    client = web.WebClient(web_base_url, web_ca)
    client.hide(password)
    out = to.try_login(client, callsign, password)
    if not isinstance(out, dict):
        pytest.skip("first operator login: %s (an enrolled or locked account: not a fresh rig state)"
                    % to.brief(out))
    client.callsign, client.bootstrap_state = callsign, out.get("totp")
    return client


@pytest.fixture(scope="module")
def operator(bootstrap):
    """The bootstrap operator, once it is known to be in the optional state (it can invite)."""
    if bootstrap.bootstrap_state != "off":
        pytest.skip("first operator login says totp %r, not 'off': the mandatory policy is on "
                    "(totp-required.toml in DVT_SUT_CONFIG_EXTRA?); see test_first_operator_login_is_optional"
                    % (bootstrap.bootstrap_state,))
    return bootstrap


@pytest.fixture(scope="module")
def account(operator, web_base_url, web_ca):
    """account(case, role) -> (callsign, password): the throw-away account of that case, registered once
    through the bootstrap operator (the register budget is paced by dvtest.web.Pacer)."""
    made = {}

    def make(case, role):
        callsign = to.callsign_for(case, role)
        if callsign not in made:
            password = web.new_password()
            operator.hide(password)
            operator.register_user(callsign, password, may_tx=False, role=role)
            made[callsign] = password
        return callsign, made[callsign]

    return make


def note_totp1b_pending(role, step):
    """The web side answers a wrong password at the step-up and at disable with 401 unauthorized where admin answers 403
    totp_invalid; dv-router calls it an inconsistency (TOTP1b, fix pending). The cases assert the CURRENT 401 through
    dvtest.totp_optional.WEB_WRONG_PASSWORD, which is the one value to change when the fix lands."""
    web.finding("TOTP1b pending: web %s wrong password at %s asserted as %s (to.WEB_WRONG_PASSWORD); the fix makes it %s"
                % (role, step, to.want_text(to.WEB_WRONG_PASSWORD), to.want_text(to.TOTP_INVALID)))


def _login(web_client, callsign, password):
    """A client logged in with the password only; the identity must be a full session."""
    client = web_client()
    client.hide(password)
    out = to.try_login(client, callsign, password)
    assert isinstance(out, dict), "%s password-only login: %s" % (callsign, to.brief(out))
    return client, out


# ---------------------------------------------------------------- (a) password-only login

def test_first_operator_login_is_optional(bootstrap):
    """The run's first operator: no secret, so the password alone gives a full session: totp "off" and no
    mandatory enrolment (policy off). Fails when the rig runs with totp-required.toml."""
    assert bootstrap.bootstrap_state == "off", \
        "first operator login: totp %r, wanted 'off' (TOTP1: optional by default; is totp-required.toml in " \
        "DVT_SUT_CONFIG_EXTRA?)" % (bootstrap.bootstrap_state,)
    ident = bootstrap.session()
    assert ident["role"] == "operator" and ident["totp"] == "off"
    assert ident["totp_required"] is False, "totp_required %r with the policy unset" % (ident["totp_required"],)
    assert ident["step_up"] is False
    assert bootstrap.get("/api/v1/operator/invites").status == 200, "an 'off' operator session is a full one"


@pytest.mark.parametrize("role", ROLES)
def test_a_password_only_login(account, web_client, role):
    callsign, password = account("a", role)
    client, ident = _login(web_client, callsign, password)
    assert ident["totp"] == "off", "no secret: totp state %r, wanted 'off'" % (ident["totp"],)
    assert ident["totp_required"] is False
    assert ident["role"] == role and ident["callsign"] == callsign
    assert ident["step_up"] is False, "a password login is not a reauthentication window (TOTP1 review)"
    assert ident["csrf"] and web.COOKIE_NAME in client.cookies, "full session: cookie and CSRF token"
    me = client.get("/api/v1/user/me")
    assert me.status == 200 and me.json()["callsign"] == callsign, "full session: GET /user/me %s" % to.brief(me)
    if role == "operator":
        assert client.get("/api/v1/operator/invites").status == 200, "an 'off' operator session is a full one"
    # the failure answer does not tell the account apart from an unknown one (openapi-web, webLogin)
    wrong = web_client()
    wrong.hide(password)
    bad = to.try_login(wrong, callsign, web.new_password())
    unknown = to.try_login(web_client(), "N0TOZZ", web.new_password())
    assert not isinstance(bad, dict) and not isinstance(unknown, dict)
    assert (bad.status, to.problem_code(bad)) == (401, "unauthorized"), "wrong password: %s" % to.brief(bad)
    assert (unknown.status, to.problem_code(unknown)) == (401, "unauthorized"), "unknown: %s" % to.brief(unknown)


# ---------------------------------------------------------------- (b) enable after a password step-up

@pytest.mark.parametrize("role", ROLES)
def test_b_enable_needs_password_step_up(account, web_client, role):
    callsign, password = account("b", role)
    client, _ = _login(web_client, callsign, password)
    # without a window: 403 step_up_required (openapi-web, webTotpOffer)
    early = to.offer(client, to.WEB)
    assert early.status == 403 and to.problem_code(early) == "step_up_required", \
        "offer without step-up: %s, documented 403 step_up_required" % to.brief(early)
    # the password opens the window; the answer rotates cookie and CSRF token
    up = to.step_up_password(client, to.WEB, password)
    assert up.status == 200, "password step-up of an account without a secret: %s" % to.brief(up)
    body = up.json()
    assert body["totp"] == "off" and 0 < body["step_up_s"] <= STEP_UP_MAX_S, "step-up answer %s" % sorted(body)
    ident = client.session()
    assert ident["step_up"] is True and 0 < ident["step_up_s"] <= STEP_UP_MAX_S
    # now the offer: the key (secret, otpauth uri, 6 digits, 30 s), never cached
    resp = to.offer(client, to.WEB)
    assert resp.status == 200, "offer after step-up: %s" % to.brief(resp)
    secret, uri, digits, period = to.parse_offer(resp)
    assert (digits, period) == (6, 30) and to.no_store(resp), "offer: digits %s period %s no-store %s" % (
        digits, period, to.no_store(resp))
    auth = web.Authenticator(secret, digits=digits, period=period)
    # a wrong first code does not enable; the right one does
    bad = to.confirm(client, to.WEB, auth.wrong_code())
    assert to.is_answer(bad, to.TOTP_INVALID), "%s confirm with a wrong first code: %s, ruled %s" % (
        role, to.brief(bad), to.want_text(to.TOTP_INVALID))
    other, other_ident = _login(web_client, callsign, password)
    if other_ident["totp"] != "off":
        web.finding("TOTP1: %s password login after an unconfirmed offer and a wrong first code says totp %r "
                    "(an offer alone must not enable 2FA)" % (role, other_ident["totp"]))
    ok = to.confirm(client, to.WEB, auth.next_code())
    assert ok.status == 200 and ok.json()["totp"] == "ok", "first code: %s" % to.brief(ok)
    ident = client.session()
    assert ident["totp"] == "ok", "after confirm the state is ok, got %r" % (ident["totp"],)


# ---------------------------------------------------------------- (c) after enabling, login needs the code

@pytest.mark.parametrize("role", ROLES)
def test_c_enrolled_login_needs_code(account, web_client, role):
    callsign, password = account("c", role)
    client, _ = _login(web_client, callsign, password)
    auth = to.enable_totp(client, to.WEB, password)
    # the right password without a code: 401 totp_required, no session, nothing set
    anon = web_client()
    anon.hide(password)
    out = to.try_login(anon, callsign, password)
    assert not isinstance(out, dict), "an enrolled account logged in with the password only"
    assert (out.status, to.problem_code(out)) == (401, "totp_required"), \
        "password without a code: %s, documented 401 totp_required" % to.brief(out)
    assert not anon.cookies and not out.set_cookies, "totp_required answer sets no cookie"
    # a wrong code is the plain 401 (one failure, far from the lockout of five)
    wrong = to.try_login(anon, callsign, password, auth.wrong_code())
    assert not isinstance(wrong, dict) and (wrong.status, to.problem_code(wrong)) == (401, "unauthorized"), \
        "wrong code: %s" % (to.brief(wrong) if not isinstance(wrong, dict) else "logged in")
    # password and the current code: a full session in state ok
    ok = web_client()
    ident = to.try_login(ok, callsign, password, auth.next_code())
    assert isinstance(ident, dict), "password and code: %s" % to.brief(ident)
    assert ident["totp"] == "ok" and ident["role"] == role
    assert ok.get("/api/v1/user/me").status == 200
    # an enrolled account cannot open a window with its password: the code is the factor
    up = to.step_up_password(ok, to.WEB, password)
    assert to.is_answer(up, to.TOTP_INVALID), "%s enrolled account step-up with the password only: %s, ruled %s " \
        "(an enrolled account must give a code)" % (role, to.brief(up), to.want_text(to.TOTP_INVALID))


# ---------------------------------------------------------------- (d) disable needs password and code

@pytest.mark.parametrize("role", ROLES)
def test_d_disable_needs_password_and_code(account, web_client, role):
    callsign, password = account("d", role)
    client, _ = _login(web_client, callsign, password)
    auth = to.enable_totp(client, to.WEB, password)          # the window is open here: it must not matter
    wrong_code = to.disable(client, to.WEB, password, auth.wrong_code())
    assert to.is_answer(wrong_code, to.TOTP_INVALID), "%s disable with the right password and a wrong code: %s, ruled %s " \
        "(an open step-up window does not replace the code)" % (role, to.brief(wrong_code), to.want_text(to.TOTP_INVALID))
    alive, ident = to.session_alive(client, to.WEB)
    assert alive and ident["totp"] == "ok", "after a refused disable: %s" % (ident if alive else to.brief(ident))
    done = to.disable(client, to.WEB, password, auth.next_code())
    assert done.status == 200 and done.json()["totp"] == "off", "disable: %s" % to.brief(done)
    ident = client.session()
    assert ident["totp"] == "off", "after disable the state is off, got %r" % (ident["totp"],)
    # back to a password-only account
    again = web_client()
    out = to.try_login(again, callsign, password)
    assert isinstance(out, dict) and out["totp"] == "off", "login after disable: %s" % (
        out["totp"] if isinstance(out, dict) else to.brief(out))


# ---------------------------------------------------------------- (e) a sensitive action asks for the step-up

def test_e_operator_sensitive_action_needs_password_step_up(account, web_client):
    callsign, password = account("e", "operator")
    client, ident = _login(web_client, callsign, password)
    assert ident["totp"] == "off" and ident["step_up"] is False
    path = "/api/v1/operator/ids/cmd"                          # step_up guard, openapi-web webIdCommand
    nothing = client.post(path, IDS_LIST)
    assert to.is_answer(nothing, to.STEP_UP_REQUIRED), "ids command with nothing: %s, ruled %s" % (
        to.brief(nothing), to.want_text(to.STEP_UP_REQUIRED))
    # a code means nothing to an account without a secret
    guess = client.post(path, IDS_LIST, headers={"X-DV-TOTP": "000000"})
    assert guess.status in to.REFUSED, "ids command with a made-up code on an account without a secret: %s" \
        % to.brief(guess)
    # an empty step-up request opens nothing
    empty = to.step_up_empty(client, to.WEB)
    assert empty.status != 200, "a step-up without password or code opened the window"
    assert client.session()["step_up"] is False
    # the password opens it
    up = to.step_up_password(client, to.WEB, password)
    assert up.status == 200, "password step-up: %s" % to.brief(up)
    done = client.post(path, IDS_LIST)
    assert done.status not in to.REFUSED, "ids command after the password step-up: %s" % to.brief(done)
    if done.status != 200:
        web.finding("operator ids command list/dmr after the password step-up -> %s (spec: 200 when the registry "
                    "answers)" % to.brief(done))


# ---------------------------------------------------------------- (f) wrong password keeps the session

@pytest.mark.parametrize("role", ROLES)
def test_f_wrong_password_keeps_session(account, web_client, role):
    callsign, password = account("f", role)
    client, _ = _login(web_client, callsign, password)
    # enable step: a wrong password at the step-up
    wrong = to.step_up_password(client, to.WEB, web.new_password())
    assert to.is_answer(wrong, to.WEB_WRONG_PASSWORD), "%s step-up with a wrong password: %s, expected %s (TOTP1b " \
        "pending: ruled %s once the fix lands)" % (role, to.brief(wrong), to.want_text(to.WEB_WRONG_PASSWORD),
                                                   to.want_text(to.TOTP_INVALID))
    note_totp1b_pending(role, "step-up")
    alive, ident = to.session_alive(client, to.WEB)
    assert alive and ident["callsign"] == callsign, "a wrong password ended the session: %s" % (
        "" if alive else to.brief(ident))
    assert ident["step_up"] is False
    assert to.offer(client, to.WEB).status == 403, "the offer still needs the step-up"
    # the session is still good for the right password
    right = to.step_up_password(client, to.WEB, password)
    assert right.status == 200, "right password after a wrong one: %s" % to.brief(right)
    # disable step: wrong password, valid code
    auth = to.enable_totp(client, to.WEB, password)
    bad = to.disable(client, to.WEB, web.new_password(), auth.next_code())
    assert to.is_answer(bad, to.WEB_WRONG_PASSWORD), "%s disable with a wrong password and a valid code: %s, expected " \
        "%s (TOTP1b pending: ruled %s once the fix lands)" % (role, to.brief(bad), to.want_text(to.WEB_WRONG_PASSWORD),
                                                              to.want_text(to.TOTP_INVALID))
    note_totp1b_pending(role, "disable")
    alive, ident = to.session_alive(client, to.WEB)
    assert alive and ident["totp"] == "ok", "a wrong password on disable ended the session or changed the " \
        "state: %s" % (ident if alive else to.brief(ident))
    ok = to.disable(client, to.WEB, password, auth.next_code())
    assert ok.status == 200, "disable with the right password after a wrong one: %s" % to.brief(ok)


# ---------------------------------------------------------------- admin side (viewer login of the run)

@pytest.fixture(scope="module")
def admin_viewer(web_url_admin, web_ca):
    """(client, user, password) of the run's `viewer` login of the admin service, logged in with the password
    only; skips when the account is not in its first state."""
    path = _out_dir() / "web" / admin_mod.ACCOUNTS_ENV
    if not path.is_file():
        pytest.skip("no admin logins at %s (run.sh writes them with --profile web)" % path)
    accounts = admin_mod.read_accounts(path)
    if "viewer" not in accounts:
        pytest.skip("%s has no viewer account" % path)
    user, password = accounts["viewer"]
    client = admin_mod.AdminClient(web_url_admin, web_ca)
    client.hide(password)
    out = to.try_login(client, user, password)
    if not isinstance(out, dict):
        pytest.skip("admin viewer login: %s (an enrolled account: not a fresh rig state)" % to.brief(out))
    client.first_login = out
    return client, user, password


def test_admin_default_is_optional(admin_viewer):
    """(a) admin.totp_required unset: the viewer's first login is a full password-only session."""
    client, user, _ = admin_viewer
    first = client.first_login
    assert first["totp"] == "off", "admin first login: totp %r, wanted 'off' (is totp-required.toml in " \
        "DVT_SUT_CONFIG_EXTRA?)" % (first["totp"],)
    if "totp_required" in first:
        assert first["totp_required"] is False
    ident = client.session()
    assert ident["user"] == user and ident["totp"] == "off" and ident["step_up"] is False
    assert ident.get("totp_required", False) is False
    assert client.get(client.path("/status")).status not in to.REFUSED, "full session: /status"


def test_admin_lifecycle(admin_viewer):
    """(b)-(f) on the admin viewer, one ordered flow that ends with the account without a secret again."""
    client, user, password = admin_viewer
    sf = to.admin_surface(client)
    if client.first_login["totp"] != "off":
        pytest.skip("admin viewer is not in the optional state (see test_admin_default_is_optional)")
    enrolled = None
    try:
        # (e) a step-up guarded read, and (b) the offer: refused without a window
        guarded = client.path("/clients")
        nothing = client.get(guarded)
        assert to.is_answer(nothing, to.STEP_UP_REQUIRED), "admin clients without a window: %s, ruled %s" % (
            to.brief(nothing), to.want_text(to.STEP_UP_REQUIRED))
        early = to.offer(client, sf)
        assert to.is_answer(early, to.STEP_UP_REQUIRED), "admin offer without a window: %s, ruled %s" % (
            to.brief(early), to.want_text(to.STEP_UP_REQUIRED))
        # (f) wrong password at the step-up: refused, the session lives
        wrong = to.step_up_password(client, sf, web.new_password())
        assert to.is_answer(wrong, to.ADMIN_WRONG_PASSWORD), "admin step-up with a wrong password: %s, ruled %s " \
            "(admin.md: the password step-up keeps totp_invalid)" % (to.brief(wrong), to.want_text(to.ADMIN_WRONG_PASSWORD))
        alive, ident = to.session_alive(client, sf)
        assert alive and ident["user"] == user and ident["step_up"] is False, \
            "a wrong password ended the admin session: %s" % ("" if alive else to.brief(ident))
        # the password opens the window (ttl_s) and the guarded read works
        up = to.step_up_password(client, sf, password)
        assert up.status == 200 and 0 < up.json()["ttl_s"] <= STEP_UP_MAX_S, "admin password step-up: %s" % to.brief(up)
        assert client.get(guarded).status not in to.REFUSED, "admin clients after the password step-up"
        # (b) offer and the first code
        offer = to.offer(client, sf)
        secret, _, digits, period = to.parse_offer(offer)
        assert (digits, period) == (6, 30) and to.no_store(offer)
        auth = web.Authenticator(secret, digits=digits, period=period)
        bad = to.confirm(client, sf, auth.wrong_code())
        assert to.is_answer(bad, to.TOTP_INVALID), "admin confirm with a wrong first code: %s, ruled %s" % (
            to.brief(bad), to.want_text(to.TOTP_INVALID))
        ok = to.confirm(client, sf, auth.next_code())
        assert ok.status == 200 and ok.json()["totp"] == "ok", "admin first code: %s" % to.brief(ok)
        enrolled = auth
        assert client.session()["totp"] == "ok"
        # (c) the login needs the code now
        anon = client.clone()
        anon.hide(password)
        out = to.try_login(anon, user, password)
        assert not isinstance(out, dict) and out.status == 401, "admin enrolled login without a code: %s" % (
            "logged in" if isinstance(out, dict) else to.brief(out))
        if to.problem_code(out) != "totp_required":
            web.finding("TOTP1: admin enrolled login without a code -> %s; documented totp_required" % to.brief(out))
        assert not anon.cookies, "no cookie without the code"
        with_code = client.clone()
        ident = to.try_login(with_code, user, password, auth.next_code())
        assert isinstance(ident, dict) and ident["totp"] == "ok", "admin login with the code"
        # (f) wrong password at disable, a valid code: refused, the session and the secret stay
        refused = to.disable(client, sf, web.new_password(), auth.next_code())
        assert to.is_answer(refused, to.ADMIN_WRONG_PASSWORD), "admin disable with a wrong password and a valid code: " \
            "%s, ruled %s" % (to.brief(refused), to.want_text(to.ADMIN_WRONG_PASSWORD))
        alive, ident = to.session_alive(client, sf)
        assert alive and ident["totp"] == "ok", "a wrong password on admin disable ended the session or changed " \
            "the state"
        # (d) right password and a wrong code: refused even with the window open
        no_code = to.disable(client, sf, password, auth.wrong_code())
        assert to.is_answer(no_code, to.TOTP_INVALID), "admin disable with the password and a wrong code: %s, ruled %s" % (
            to.brief(no_code), to.want_text(to.TOTP_INVALID))
        # (d) password and the current code
        done = to.disable(client, sf, password, auth.next_code())
        assert done.status == 200 and done.json()["totp"] == "off", "admin disable: %s" % to.brief(done)
        enrolled = None
        again = client.clone()
        out = to.try_login(again, user, password)
        assert isinstance(out, dict) and out["totp"] == "off", "admin login after disable"
    finally:
        if enrolled is not None:                  # a failed run must not leave the viewer enrolled
            try:
                for _ in range(2):
                    if to.disable(client, sf, password, enrolled.next_code()).status == 200:
                        break
                    time.sleep(1.0)
            except (web.WebError, OSError):
                pass
