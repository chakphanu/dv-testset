# SPDX-License-Identifier: MIT
"""Route table of DV-Router's web service API (dv-svc-web), API3 path scheme.

One record per operation (method, path template, audience prefix, minimum role,
declared success status and a SAFE probe) for the 48 audience operations / 34
path templates of the hand-written OpenAPI description (docs/api/openapi-web.yaml
of DV-Router) plus its four root probe methods (GET/HEAD /health and /ready,
WEB2; WEB_ROOT_OPS), and the 55 OLD
templates that API3 removed (docs/api/README.md, "API3 audience paths"; 30 of
the web service, 25 of the admin service, see api_admin.py). Stdlib only: dvtest
has no YAML parser and the spec files are not copied here.

The tables are the repo's own copy of what the cases need; `compare_spec_dir()`
checks them against a DV-Router checkout when the environment names one
(`DVT_API_SPEC_DIR=<dir with openapi-web.yaml, openapi-admin.yaml>`), parsing
just enough of the YAML with regular expressions (path keys at two spaces of
indent under `paths:`, method keys at four) and reports additions and
removals. It never fails a case by itself: the unit test skips without the
variable, and a difference is the news that the tables need an update.

Roles ("floor"): the web router classifies the path first (an unclassified path
under /user or /operator is 404 for everyone), then applies the audience floor
(`public`, `auth`, `rtc` keep their own admission; `user` needs a web session;
`operator` an operator web session; a higher role passes a lower floor). The
session write guards (Origin/JSON, X-DV-CSRF: 403 `csrf`) run BEFORE that floor
(API4: docs/api/README.md "API3 audience paths", web.md "Guard order").
  anonymous   no session needed (login, register, public reads)
  user        a web session (user or operator)
  operator    an operator web session
  rtc         conditional: a session, or a guest lease when [web] guest_listen
              is on (it is off in the rig)

Probe fields (all optional):
  params          values for the {placeholders} of the path
  query           query string without "?"
  body            JSON body (an invalid one on purpose where the operation mutates)
  raw             (bytes, content type): a body that is not JSON, for operations
                  whose success path would act (forced sync): the handler's
                  media-type check answers before it does
  headers         extra request headers
  stream          SSE: read the status and the first frame only
  fresh           the probe ends the session it uses (logout, revoke all): the
                  case runs it on a fresh login of the same account
  missing_ok      the target does not exist, so a 404 problem+json from the
                  handler is the expected answer at or above the floor (a 404 on
                  any other operation is a failure; a text/plain 404 is not a
                  handler answer)
  credential_401  a 401 is the answer to a bad credential (login, register),
                  not a role refusal
  needs_tx        a user with may_tx is needed to get past the may_tx check

Success status (`success`): the 2xx every operation declares since API4
(DV-Router 2fab0f8, "every operation declares its success status explicitly"):
201 register, password-reset code and invite issue; 204 logout and RTC
close/alive (admin: logout); 202 room messages (admin: restart-node); 200 the
rest (GET/HEAD readiness and ID reads may also answer the documented 503). The
matrix judges the authorization class only; cases that make a REAL successful
call assert this status, and `parse_spec_success()` checks the values against
the spec.
"""
import os
import re
from dataclasses import dataclass, field

AUDIENCES = ("public", "auth", "rtc", "user", "operator")
FLOORS = ("anonymous", "user", "operator", "rtc")


@dataclass(frozen=True)
class Op:
    method: str
    path: str                    # template exactly as in the spec
    floor: str                   # minimum role (module doc); admin: anonymous|viewer|operator|admin
    params: dict = field(default_factory=dict, compare=False, hash=False)
    query: str = ""
    body: object = None
    raw: tuple = None
    headers: dict = field(default_factory=dict, compare=False, hash=False)
    stream: bool = False
    fresh: bool = False
    missing_ok: bool = False
    credential_401: bool = False
    needs_tx: bool = False
    step_up: bool = False        # the spec lists the step_up guard
    mutating: bool = False
    success: int = 200           # the declared 2xx of a successful call (module doc)
    note: str = ""

    @property
    def key(self):
        return (self.method, self.path)

    @property
    def audience(self):
        parts = self.path.split("/")
        return parts[3] if len(parts) > 3 else ""

    def url(self):
        """The probe's request target: placeholders filled, query appended."""
        path = self.path
        for name, value in self.params.items():
            path = path.replace("{%s}" % name, value)
        if "{" in path:
            raise ValueError("unfilled placeholder in %s" % self.path)
        return path + ("?" + self.query if self.query else "")


def _op(method, path, floor, **kw):
    if "mutating" not in kw:
        kw["mutating"] = method not in ("GET", "HEAD")
    return Op(method, path, floor, **kw)


# a circuit that is not open and a callsign/id/room that exist nowhere
NO_SESSION_ID = "0" * 32
NO_RECORDING = "0" * 96
NO_CALL = "N0NONE"
ROOM = "A"
WRONG_JSON = {"probe": "dvt-api-invalid"}      # a body no handler accepts

WEB_OPS = (
    # ---- public: anonymous read-only
    _op("GET", "/api/v1/public/state", "anonymous"),
    _op("HEAD", "/api/v1/public/state", "anonymous"),
    _op("GET", "/api/v1/public/lastheard", "anonymous", query="limit=1"),
    _op("HEAD", "/api/v1/public/lastheard", "anonymous"),
    _op("GET", "/api/v1/public/events", "anonymous", stream=True),
    _op("HEAD", "/api/v1/public/events", "anonymous"),
    # ---- auth: anonymous login/register, the rest needs a session
    _op("POST", "/api/v1/auth/register", "anonymous", body={}, credential_401=True, success=201,
        note="empty body: 400/401/422, nothing is redeemed"),
    _op("POST", "/api/v1/auth/session", "anonymous", body={}, credential_401=True,
        note="empty body: 400/401, no login happens"),
    _op("GET", "/api/v1/auth/session", "user"),
    _op("DELETE", "/api/v1/auth/session", "user", fresh=True, success=204),
    _op("POST", "/api/v1/auth/session/totp", "user", body={},
        note="400 for a user (no TOTP in v1); an enrolled operator without X-DV-TOTP: 403 totp_invalid and a "
             "lockout failure (API4, web.md Account API)"),
    _op("POST", "/api/v1/auth/session/totp/confirm", "user", body={}),
    _op("POST", "/api/v1/auth/password", "user", body={}, note="no password is changed with an empty body"),
    _op("PUT", "/api/v1/auth/password", "user", body={}),
    # ---- user: own account, room messages, recordings
    _op("GET", "/api/v1/user/me", "user"),
    _op("GET", "/api/v1/user/me/sessions", "user"),
    _op("DELETE", "/api/v1/user/me/sessions", "user", fresh=True, note="revokes every login of the account"),
    _op("DELETE", "/api/v1/user/me/sessions/{id}", "user", params={"id": NO_SESSION_ID}, missing_ok=True),
    _op("GET", "/api/v1/user/rooms/{R}/messages", "user", params={"R": ROOM}, query="limit=1"),
    _op("POST", "/api/v1/user/rooms/{R}/messages", "user", params={"R": ROOM}, body=WRONG_JSON, needs_tx=True, success=202,
        note="a rejected body (400); nothing is sent to the room"),
    _op("POST", "/api/v1/user/rooms/{R}/messages/directed", "user", params={"R": ROOM}, body=WRONG_JSON,
        needs_tx=True, success=202),
    _op("GET", "/api/v1/user/recordings", "user", missing_ok=True, note="503/404 without a recorder"),
    _op("GET", "/api/v1/user/recordings/{id}", "user", params={"id": NO_RECORDING}, missing_ok=True),
    _op("GET", "/api/v1/user/recordings/{id}/audio.wav", "user", params={"id": NO_RECORDING}, missing_ok=True),
    _op("HEAD", "/api/v1/user/recordings/{id}/audio.wav", "user", params={"id": NO_RECORDING}, missing_ok=True),
    # ---- operator
    _op("GET", "/api/v1/operator/clients", "operator", query="limit=1"),
    _op("GET", "/api/v1/operator/events", "operator", query="limit=1"),
    _op("GET", "/api/v1/operator/stream", "operator", stream=True),
    _op("POST", "/api/v1/operator/edges/{kind}/clients/{circuit}/kick", "operator",
        params={"kind": "xlx", "circuit": "0.0"}, body={}, missing_ok=True, note="no such circuit: nobody is kicked"),
    _op("GET", "/api/v1/operator/accounts", "operator", query="limit=1"),
    _op("GET", "/api/v1/operator/accounts/{callsign}", "operator", params={"callsign": NO_CALL}, missing_ok=True),
    _op("PATCH", "/api/v1/operator/accounts/{callsign}", "operator", params={"callsign": NO_CALL}, body={},
        missing_ok=True),
    _op("DELETE", "/api/v1/operator/accounts/{callsign}", "operator", params={"callsign": NO_CALL}, missing_ok=True),
    _op("POST", "/api/v1/operator/accounts/{callsign}/reset", "operator", params={"callsign": NO_CALL}, body={},
        missing_ok=True, success=201),
    _op("GET", "/api/v1/operator/invites", "operator", query="limit=1"),
    _op("POST", "/api/v1/operator/invites", "operator", body={}, success=201, note="empty body: no invite is created"),
    _op("DELETE", "/api/v1/operator/invites/{callsign}", "operator", params={"callsign": NO_CALL}, missing_ok=True),
    _op("GET", "/api/v1/operator/audit", "operator", query="limit=1"),
    _op("GET", "/api/v1/operator/ids/rows", "operator", query="kind=dmr&limit=1"),
    _op("HEAD", "/api/v1/operator/ids/rows", "operator", query="kind=dmr&limit=1"),
    _op("POST", "/api/v1/operator/ids/cmd", "operator", body={}, step_up=True),
    _op("GET", "/api/v1/operator/ids/sync", "operator"),
    _op("HEAD", "/api/v1/operator/ids/sync", "operator"),
    _op("POST", "/api/v1/operator/ids/sync/force", "operator", raw=(b"x", "text/plain"), step_up=True,
        note="a nonempty non-JSON body: the write guard's 403 csrf answers before the floor and the handler "
             "(API4 guard order; force checks the media type only of a nonempty body)"),
    # ---- rtc: conditional (session, or guest lease); URLs unchanged by API3
    _op("POST", "/api/v1/rtc/offer", "rtc", body=WRONG_JSON),
    _op("POST", "/api/v1/rtc/close", "rtc", body={"sess_id": "0" * 64}, missing_ok=True, success=204,
        note="unknown or foreign circuit: 403 forbidden (API4, web.md RTC)"),
    _op("POST", "/api/v1/rtc/alive", "rtc", body={"sess_id": "0" * 64}, missing_ok=True, success=204,
        note="unknown or foreign circuit: 403 forbidden (API4, web.md RTC)"),
    _op("GET", "/api/v1/rtc/ice", "rtc", headers={"X-DV-RTC-Session": "0" * 64}, missing_ok=True,
        note="empty [web] turn_urls (the rig without turn.toml): 404 before method, Origin, cookie or circuit "
             "checks, anonymous callers included (API4, web.md RTC)"),
)

# Root probes of the web service (WEB2; README: "52 operations (48 audience API operations and four root
# probe methods)"): no audience prefix, anonymous, shared public bucket; /ready is 200 or 503 with reasons.
WEB_ROOT_OPS = (
    _op("GET", "/health", "anonymous"),
    _op("HEAD", "/health", "anonymous"),
    _op("GET", "/ready", "anonymous", note="200 ready or 503 with reasons (hub, rtc_recovery)"),
    _op("HEAD", "/ready", "anonymous"),
)

WEB_KEYS = frozenset(op.key for op in WEB_OPS + WEB_ROOT_OPS)      # 52, as the spec
WEB_TEMPLATES = frozenset(op.path for op in WEB_OPS)                  # the 34 audience templates
RTC_PATHS = ("/api/v1/rtc/offer", "/api/v1/rtc/close", "/api/v1/rtc/alive", "/api/v1/rtc/ice")

# (service, methods, old template, new template): README "API3 audience paths", the 59
# rows; those with old == new are the four unchanged RTC templates, the other 55 moved
# (30 web + 25 admin) and have no aliases: 404 on every mount, for anyone.
MIGRATION = (
    ("web", ("GET", "HEAD"), "/api/public/state", "/api/v1/public/state"),
    ("web", ("GET", "HEAD"), "/api/public/lastheard", "/api/v1/public/lastheard"),
    ("web", ("GET", "HEAD"), "/api/public/events", "/api/v1/public/events"),
    ("web", ("POST",), "/api/v1/register", "/api/v1/auth/register"),
    ("web", ("POST", "GET", "DELETE"), "/api/v1/session", "/api/v1/auth/session"),
    ("web", ("POST",), "/api/v1/session/totp", "/api/v1/auth/session/totp"),
    ("web", ("POST",), "/api/v1/session/totp/confirm", "/api/v1/auth/session/totp/confirm"),
    ("web", ("GET",), "/api/v1/me", "/api/v1/user/me"),
    ("web", ("POST", "PUT"), "/api/v1/me/password", "/api/v1/auth/password"),
    ("web", ("GET", "DELETE"), "/api/v1/me/sessions", "/api/v1/user/me/sessions"),
    ("web", ("DELETE",), "/api/v1/me/sessions/{id}", "/api/v1/user/me/sessions/{id}"),
    ("web", ("GET",), "/api/v1/clients", "/api/v1/operator/clients"),
    ("web", ("GET",), "/api/v1/events", "/api/v1/operator/events"),
    ("web", ("GET",), "/api/v1/stream", "/api/v1/operator/stream"),
    ("web", ("POST",), "/api/v1/edges/{kind}/clients/{circuit}/kick",
     "/api/v1/operator/edges/{kind}/clients/{circuit}/kick"),
    ("web", ("GET",), "/api/v1/accounts", "/api/v1/operator/accounts"),
    ("web", ("GET", "PATCH", "DELETE"), "/api/v1/accounts/{callsign}", "/api/v1/operator/accounts/{callsign}"),
    ("web", ("POST",), "/api/v1/accounts/{callsign}/reset", "/api/v1/operator/accounts/{callsign}/reset"),
    ("web", ("GET", "POST"), "/api/v1/invites", "/api/v1/operator/invites"),
    ("web", ("DELETE",), "/api/v1/invites/{callsign}", "/api/v1/operator/invites/{callsign}"),
    ("web", ("GET",), "/api/v1/audit", "/api/v1/operator/audit"),
    ("web", ("GET", "HEAD"), "/api/v1/ids/rows", "/api/v1/operator/ids/rows"),
    ("web", ("POST",), "/api/v1/ids/cmd", "/api/v1/operator/ids/cmd"),
    ("web", ("GET", "HEAD"), "/api/v1/ids/sync", "/api/v1/operator/ids/sync"),
    ("web", ("POST",), "/api/v1/ids/sync/force", "/api/v1/operator/ids/sync/force"),
    ("web", ("GET", "POST"), "/api/v1/rooms/{R}/messages", "/api/v1/user/rooms/{R}/messages"),
    ("web", ("POST",), "/api/v1/rooms/{R}/messages/directed", "/api/v1/user/rooms/{R}/messages/directed"),
    ("web", ("GET",), "/api/v1/recordings", "/api/v1/user/recordings"),
    ("web", ("GET",), "/api/v1/recordings/{id}", "/api/v1/user/recordings/{id}"),
    ("web", ("GET", "HEAD"), "/api/v1/recordings/{id}/audio.wav", "/api/v1/user/recordings/{id}/audio.wav"),
    ("web", ("POST",), "/api/v1/rtc/offer", "/api/v1/rtc/offer"),
    ("web", ("POST",), "/api/v1/rtc/close", "/api/v1/rtc/close"),
    ("web", ("POST",), "/api/v1/rtc/alive", "/api/v1/rtc/alive"),
    ("web", ("GET",), "/api/v1/rtc/ice", "/api/v1/rtc/ice"),
    ("admin", ("GET",), "/api/v1/health", "/api/v1/admin/health"),
    ("admin", ("GET",), "/api/v1/ready", "/api/v1/admin/ready"),
    ("admin", ("GET",), "/api/v1/version", "/api/v1/admin/version"),
    ("admin", ("POST", "GET", "DELETE"), "/api/v1/session", "/api/v1/admin/session"),
    ("admin", ("POST",), "/api/v1/session/totp", "/api/v1/admin/session/totp"),
    ("admin", ("POST",), "/api/v1/session/totp/confirm", "/api/v1/admin/session/totp/confirm"),
    ("admin", ("POST",), "/api/v1/session/step-up", "/api/v1/admin/session/step-up"),
    ("admin", ("GET",), "/api/v1/status", "/api/v1/admin/status"),
    ("admin", ("GET",), "/api/v1/rooms", "/api/v1/admin/rooms"),
    ("admin", ("GET",), "/api/v1/clients", "/api/v1/admin/clients"),
    ("admin", ("GET",), "/api/v1/events", "/api/v1/admin/events"),
    ("admin", ("GET",), "/api/v1/lastheard", "/api/v1/admin/lastheard"),
    ("admin", ("GET",), "/api/v1/stream", "/api/v1/admin/stream"),
    ("admin", ("GET",), "/api/v1/config", "/api/v1/admin/config"),
    ("admin", ("GET",), "/api/v1/schema", "/api/v1/admin/schema"),
    ("admin", ("GET", "PUT"), "/api/v1/config/raw", "/api/v1/admin/config/raw"),
    ("admin", ("POST",), "/api/v1/config/changes", "/api/v1/admin/config/changes"),
    ("admin", ("GET",), "/api/v1/config/history", "/api/v1/admin/config/history"),
    ("admin", ("GET",), "/api/v1/config/history/{seq}", "/api/v1/admin/config/history/{seq}"),
    ("admin", ("POST",), "/api/v1/config/history/{seq}/restore", "/api/v1/admin/config/history/{seq}/restore"),
    ("admin", ("GET",), "/api/v1/audit", "/api/v1/admin/audit"),
    ("admin", ("GET",), "/api/v1/audit/logins", "/api/v1/admin/audit/logins"),
    ("admin", ("POST",), "/api/v1/system/restart-node", "/api/v1/admin/system/restart-node"),
    ("admin", ("POST",), "/api/v1/children/{name}/restart", "/api/v1/admin/children/{name}/restart"),
    ("admin", ("POST",), "/api/v1/edges/{kind}/clients/{circuit}/kick",
     "/api/v1/admin/edges/{kind}/clients/{circuit}/kick"),
)

REMOVED = tuple(row for row in MIGRATION if row[2] != row[3])          # 55 templates
UNCHANGED = tuple(row for row in MIGRATION if row[2] == row[3])        # 4 RTC templates
REMOVED_WEB = tuple(row for row in REMOVED if row[0] == "web")
REMOVED_ADMIN = tuple(row for row in REMOVED if row[0] == "admin")


def removed_probe_path(template):
    """A concrete request target for an old template (placeholders filled with
    values that match nothing)."""
    fill = {"id": NO_SESSION_ID, "kind": "xlx", "circuit": "0.0", "callsign": NO_CALL, "R": ROOM, "seq": "1",
            "name": "dv-nonexistent"}
    return re.sub(r"\{(\w+)\}", lambda m: fill[m.group(1)], template)


# ------------------------------------------------------------- spec comparison

_PATH_KEY = re.compile(r"^  (/\S*):\s*$")
_METHOD_KEY = re.compile(r"^    (get|put|post|delete|patch|head|options):\s*$")
_ROLE = re.compile(r"^      x-role:\s*(\S+)")


def parse_spec_ops(text):
    """{(METHOD, path template): x-role or None} of an OpenAPI file: just
    enough YAML for the hand-written specs (path keys at two spaces of indent
    under `paths:`, method keys at four, `x-role` at six). Raises ValueError
    when the text has no `paths:` section."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "paths:")
    except StopIteration:
        raise ValueError("no `paths:` section") from None
    ops, path, cur = {}, None, None
    for line in lines[start + 1:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break                                     # next top-level key
        m = _PATH_KEY.match(line)
        if m:
            path, cur = m.group(1), None
            continue
        m = _METHOD_KEY.match(line)
        if m and path:
            cur = (m.group(1).upper(), path)
            ops[cur] = None
            continue
        m = _ROLE.match(line)
        if m and cur:
            ops[cur] = m.group(1)
    return ops


_RESPONSES = re.compile(r"^      responses:(?:\s+([&*])([\w-]+))?\s*$")     # block, &anchor or *alias
_STATUS_KEY = re.compile(r"""^        ['"]?([1-5][0-9][0-9])['"]?:""")


def parse_spec_success(text):
    """{(METHOD, path template): sorted tuple of the 2xx status keys} of an
    OpenAPI file (same subset of YAML as parse_spec_ops: `responses:` at six
    spaces of indent, status keys at eight; a YAML anchor `responses: &name`
    and its alias `responses: *name` are followed). An operation without a 2xx
    maps to ()."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "paths:")
    except StopIteration:
        raise ValueError("no `paths:` section") from None
    out, path, cur, in_resp, anchors, anchor = {}, None, None, False, {}, None
    for line in lines[start + 1:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        m = _PATH_KEY.match(line)
        if m:
            path, cur, in_resp = m.group(1), None, False
            continue
        m = _METHOD_KEY.match(line)
        if m and path:
            cur, in_resp = (m.group(1).upper(), path), False
            out[cur] = ()
            continue
        if cur is None:
            continue
        m = _RESPONSES.match(line)
        if m:
            if m.group(1) == "*":
                out[cur] = anchors.get(m.group(2), ())
                in_resp, anchor = False, None
            else:
                in_resp, anchor = True, m.group(2)
                if anchor:
                    anchors[anchor] = ()
            continue
        if in_resp and line.startswith("      ") and not line.startswith("       ") and line.strip():
            in_resp, anchor = False, None             # the next key of the operation
        m = _STATUS_KEY.match(line) if in_resp else None
        if m and m.group(1).startswith("2"):
            out[cur] = tuple(sorted(set(out[cur]) | {int(m.group(1))}))
            if anchor:
                anchors[anchor] = out[cur]
    return out


def success_mismatches(ops, spec_success):
    """[(METHOD, path, table success, spec 2xx codes)] where the table's
    `success` is not the lowest declared 2xx (or the spec declares none)."""
    bad = []
    for op in ops:
        codes = spec_success.get(op.key)
        if codes is None:
            continue                                   # a route difference: diff_ops reports it
        if not codes or op.success != codes[0]:
            bad.append((op.method, op.path, op.success, codes))
    return sorted(bad)


def compare_spec_success(directory):
    """{"web": mismatches, "admin": mismatches} of the declared success statuses
    (success_mismatches) against the two spec files in `directory`."""
    from dvtest import api_admin
    out = {}
    for name, ops in (("web", WEB_OPS + WEB_ROOT_OPS), ("admin", api_admin.ADMIN_OPS)):
        with open(os.path.join(directory, "openapi-%s.yaml" % name), encoding="utf-8") as fh:
            out[name] = success_mismatches(ops, parse_spec_success(fh.read()))
    return out


def diff_ops(table, spec):
    """(added, removed): in the spec but not in the table, and in the table but
    not in the spec; both sorted lists of (METHOD, path)."""
    table, spec = set(table), set(spec)
    return sorted(spec - table), sorted(table - spec)


def compare_spec_dir(directory, web_table=None, admin_table=None):
    """Compare the tables with openapi-web.yaml and openapi-admin.yaml in
    `directory`. Returns {"web": (added, removed), "admin": (added, removed)};
    FileNotFoundError when a file is missing."""
    from dvtest import api_admin
    web_table = WEB_KEYS if web_table is None else web_table
    admin_table = api_admin.ADMIN_KEYS if admin_table is None else admin_table
    out = {}
    for name, table in (("web", web_table), ("admin", admin_table)):
        with open(os.path.join(directory, "openapi-%s.yaml" % name), encoding="utf-8") as fh:
            out[name] = diff_ops(table, parse_spec_ops(fh.read()))
    return out


def parse_migration_table(text):
    """Rows (service, methods, old, new) of the README "API3 audience paths" table."""
    rows = []
    pat = re.compile(r"^\|\s*(web|admin)\s*\|\s*([A-Z, ]+?)\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|")
    for line in text.splitlines():
        m = pat.match(line)
        if m:
            rows.append((m.group(1), tuple(x.strip() for x in m.group(2).split(",")), m.group(3), m.group(4)))
    return rows
