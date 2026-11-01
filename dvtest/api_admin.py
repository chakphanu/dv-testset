# SPDX-License-Identifier: MIT
"""Route table of DV-Router's admin service API (dv-svc-admin), API3 path scheme.

One record per operation of docs/api/openapi-admin.yaml (28 operations, 25 path
templates; same record type and probe fields as dvtest/api_web.py, whose module
doc explains them). The admin service is a separate service and host, not a
blanket role floor: `floor` is the declared minimum role of the operation,

  anonymous   health, ready, version, login
  viewer      read-only status, rooms, events, schema, session; config forms (step-up)
  operator    kick (step-up)
  admin       configuration edit and history, audit, restart-node, child restart

Mounts (docs/design/admin.md "Mounts"): the API is `/api/v1/admin/...` on the
admin host and on the legacy direct mount, `/admin/api/v1/admin/...` on the
prefix mount; `/health` and `/ready` also exist at the root of a mount
(ROOT_PROBES). The 25 old admin templates are in api_web.REMOVED_ADMIN.

Success statuses (`success`, API4): 204 logout, 202 restart-node, 200 the rest
(/ready may answer the documented 503).

Guard order (admin.md, API4) differs from the web service: Origin (403 `csrf`)
before authentication, then cookie/binding (401), the minimum role (403
`forbidden`), TOTP enrolment (403), and only then X-DV-CSRF and content type
(403 `csrf`): a caller below the floor gets `forbidden` before a non-JSON body
is looked at.

Probes never act: bodies are empty or invalid, targets do not exist
(history sequence, child name, circuit), and the raw configuration PUT carries
no If-Match, so it is refused (428) before anything is written.
"""
from dvtest.api_web import _op

ADMIN_BASE = "/api/v1/admin"
PREFIX_BASE = "/admin/api/v1/admin"
ROOT_PROBES = ("/health", "/ready")
ROLES = ("anonymous", "viewer", "operator", "admin")          # in increasing order of power
NO_SEQ = "999999"
NO_CHILD = "dv-nonexistent"
NO_CIRCUIT = "dvt-none"

_B = ADMIN_BASE
ADMIN_OPS = (
    _op("GET", _B + "/health", "anonymous"),
    _op("GET", _B + "/ready", "anonymous", note="200 ready or 503 with reasons; HEAD too"),
    _op("GET", _B + "/version", "anonymous"),
    _op("POST", _B + "/session", "anonymous", body={}, credential_401=True, note="empty body: no login"),
    _op("GET", _B + "/session", "viewer"),
    _op("DELETE", _B + "/session", "viewer", fresh=True, success=204),
    _op("POST", _B + "/session/totp", "viewer", body={}, step_up=True,
        note="re-enrolment of an enrolled account needs its current code: refused, nothing changes"),
    _op("POST", _B + "/session/totp/confirm", "viewer", body={}),
    _op("POST", _B + "/session/step-up", "viewer", body={}),
    _op("GET", _B + "/status", "viewer"),
    _op("GET", _B + "/rooms", "viewer"),
    _op("GET", _B + "/clients", "viewer", query="limit=1", step_up=True),
    _op("GET", _B + "/events", "viewer", query="limit=1"),
    _op("GET", _B + "/lastheard", "viewer", query="limit=1"),
    _op("GET", _B + "/stream", "viewer", stream=True),
    _op("GET", _B + "/config", "viewer", step_up=True),
    _op("GET", _B + "/schema", "viewer"),
    _op("GET", _B + "/config/raw", "admin", step_up=True),
    _op("PUT", _B + "/config/raw", "admin", raw=(b"x", "text/plain"), step_up=True,
        note="no If-Match: 428 before anything is written"),
    _op("POST", _B + "/config/changes", "admin", body={}, step_up=True, note="no base_etag: refused"),
    _op("GET", _B + "/config/history", "admin", step_up=True),
    _op("GET", _B + "/config/history/{seq}", "admin", params={"seq": NO_SEQ}, step_up=True, missing_ok=True),
    _op("POST", _B + "/config/history/{seq}/restore", "admin", params={"seq": NO_SEQ}, body={}, step_up=True,
        missing_ok=True),
    _op("GET", _B + "/audit", "admin", query="limit=1", step_up=True),
    _op("GET", _B + "/audit/logins", "admin", query="limit=1", step_up=True),
    _op("POST", _B + "/system/restart-node", "admin", body={}, step_up=True, success=202,
        note="no confirm string: 400/422, the node is not restarted"),
    _op("POST", _B + "/children/{name}/restart", "admin", params={"name": NO_CHILD}, body={}, step_up=True,
        missing_ok=True, note="a child the launcher does not supervise: 404"),
    _op("POST", _B + "/edges/{kind}/clients/{circuit}/kick", "operator",
        params={"kind": "xlx", "circuit": NO_CIRCUIT}, body={}, step_up=True, missing_ok=True),
)

ADMIN_KEYS = frozenset(op.key for op in ADMIN_OPS)
ADMIN_TEMPLATES = frozenset(op.path for op in ADMIN_OPS)


def rank(role):
    """Position of a role in ROLES (anonymous 0 ... admin 3)."""
    return ROLES.index(role)


def mounted(op, mount):
    """The operation's path on a mount: "host"/"direct" keep /api/v1/admin, "prefix" is /admin/api/v1/admin."""
    if mount == "prefix":
        return op.path.replace(ADMIN_BASE, PREFIX_BASE, 1)
    if mount in ("host", "direct"):
        return op.path
    raise ValueError("mount %r" % mount)


def moved_to_prefix(path):
    """The prefix-mount form of a host-mount path (`/health` -> `/admin/health`)."""
    return "/admin" + path
