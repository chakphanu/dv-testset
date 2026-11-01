# SPDX-License-Identifier: MIT
"""Send the safe probe of an API operation and judge the answer by its
AUTHORIZATION class only (cases/test_web_api.py). Stdlib only; the judging
functions are pure and unit-tested (cases/unit/test_api_probe.py).

Classes (what the spec's role floor demands of a role):
  "401"     below the floor and not signed in: 401 (code `unauthorized`)
  "403"     signed in, role too low: 403 (code `forbidden`). On the web service a
            probe whose body is not JSON (op.raw) gets the write guard's 403 `csrf`
            instead: the session write guards run before the audience floor
            (documented by API4: web.md "Guard order"). The admin service checks
            the role first (admin.md "Guard order"), so there it is `forbidden`
  "refuse"  conditional admission without a proof (rtc/* as anonymous with guest
            listening off): 401 or 403; for the TURN route a documented 404 (TURN
            disabled, problem+json) counts too
  "allow"   at or above the floor: the router lets the request reach its
            handler. Any handler answer counts (400, 405, 415, 422, 428, 503 ...)
            EXCEPT 401, 403 (but a 403 that is not a role refusal, for instance
            step_up_required, is a handler answer) and 404 (but a handler's own
            "no such target" 404 with an application/problem+json body is, for
            operations whose probe names a missing target). Limit: a route miss
            and a missing target are both 404 problem+json `not_found` (documented
            by API4: web.md "Account and operator API"), so for those operations
            a regression to "no route" cannot be told from "no target" here; the
            removed-URL cases and the other roles of the same route cover it. On
            rtc/* a 403 `forbidden` is the handler's answer for an unknown or
            foreign circuit (documented by API4, web.md RTC rows; the probe names
            a circuit nobody owns). 5xx other than the documented 503 is a failure.

Error codes (docs/design/admin.md "Errors", shared by both services): 403 is
`forbidden` for a role refusal, but also `csrf`, `step_up_required`,
`totp_invalid`, `totp_enrolment_required` for guards that run after the role.
"""
import json
import time

from dvtest import web

NON_ROLE_403 = frozenset(("csrf", "step_up_required", "totp_invalid", "totp_enrolment_required"))
WEB_ROLES = ("anonymous", "user", "operator")


class Result:
    """Status, lower-case headers, body bytes and (SSE) frames of one probe."""

    def __init__(self, status, headers=None, body=b"", frames=None):
        self.status, self.headers, self.body = status, headers or {}, body or b""
        self.frames = frames or []

    @property
    def ctype(self):
        return self.headers.get("content-type", "")

    def is_problem(self):
        return self.ctype.startswith("application/problem+json")

    def code(self):
        """The `code` of a problem+json body, else None."""
        if not self.ctype.startswith("application/") or "json" not in self.ctype:
            return None
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data.get("code") if isinstance(data, dict) else None

    def retry_after(self):
        value = self.headers.get("retry-after", "")
        return int(value) if value.isdigit() else None

    def brief(self):
        code = self.code()
        return "HTTP %d%s%s" % (self.status, " " + code if code else "",
                                " (%s)" % self.ctype.split(";")[0] if self.ctype and not code else "")


def send(client, op, path=None, headers=None, retries=2):
    """Send the probe of `op` with `client` (a WebClient/AdminClient). `path`
    overrides the request target (another mount). A 429 is waited out and
    retried (at most `retries` times): rate limits are not the question here."""
    target = path or op.url()
    extra = dict(op.headers)
    extra.update(headers or {})
    if op.stream:
        retries = max(retries, 6)            # stream slots of closed connections are freed late (heartbeat)
    for attempt in range(retries + 1):
        if op.stream:
            res = _send_stream(client, target, extra)
        else:
            kw = {}
            if op.raw is not None:
                kw["raw_body"] = op.raw[0]
                extra["Content-Type"] = op.raw[1]
            elif op.body is not None:
                kw["json_body"] = op.body
            resp = client.request(op.method, target, headers=extra or None, **kw)
            res = Result(resp.status, resp.headers, resp.body)
        if res.status != 429 or attempt == retries:
            return res
        time.sleep(min(res.retry_after() or 2, 8) + 0.5)
    return res


def _send_stream(client, target, extra):
    try:
        status, ctype, frames = client.sse(target, max_events=1, timeout=8)
        return Result(status, {"content-type": ctype}, b"", frames)
    except web.WebError as exc:
        if exc.status is None:
            raise
        return Result(exc.status, {}, (exc.body or "").encode("utf-8"))


def expectation(op, role, ranks=None):
    """The authorization class a role must get for `op`: see the module doc.
    `ranks` is the ordered role list of the service (default: the web roles
    anonymous < user < operator); admin passes api_admin.ROLES."""
    if op.floor == "rtc":
        return "refuse" if role == "anonymous" else "allow"
    order = list(ranks or WEB_ROLES)
    if order.index(role) >= order.index(op.floor):
        return "allow"
    return "401" if role == "anonymous" else "403"


def judge(op, expect, res):
    """(ok, why) for the Result of the probe against the expected class."""
    st = res.status
    if expect == "401":
        return st == 401, "wanted 401, got " + res.brief()
    if expect == "403":
        if op.raw is not None and op.audience != "admin":
            # web: the write guard answers a nonempty non-JSON body before the floor (API4 guard order)
            return st == 403 and res.code() == "csrf", "wanted 403 csrf (write guard before the floor), got " + res.brief()
        ok = st == 403 and res.code() not in NON_ROLE_403
        return ok, "wanted 403 forbidden, got " + res.brief()
    if expect == "refuse":
        if st == 404 and op.missing_ok and res.is_problem():
            return True, "404 (feature disabled) before the session check"
        return st in (401, 403), "wanted 401 or 403, got " + res.brief()
    # allow
    if st == 401:
        return op.credential_401, "401 at or above the floor: " + res.brief()
    if st == 403:
        if op.audience == "rtc" and res.code() == "forbidden":
            return True, "403 forbidden: the circuit is not owned by the caller"
        return res.code() in NON_ROLE_403, "403 forbidden at or above the floor: " + res.brief()
    if st == 404:
        ok = op.missing_ok and (op.method == "HEAD" or res.is_problem())
        return ok, "404 at or above the floor (router-level or not a handler answer): " + res.brief()
    if st == 429:
        return False, "still rate limited after the retries: " + res.brief()
    if st >= 500 and st != 503:
        return False, "server error: " + res.brief()
    return True, res.brief()
