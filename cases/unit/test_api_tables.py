# SPDX-License-Identifier: MIT
"""Route tables of the web unit (dvtest/api_web.py, dvtest/api_admin.py): counts of
the API3 migration, safety rules for probes, and the spec comparison (regex YAML
reader, success statuses) on small fixture snippets. With DVT_API_SPEC_DIR=<dir with openapi-web.yaml
and openapi-admin.yaml> (a DV-Router checkout, docs/api) the tables are also compared
with the real specs and its README migration table; without it that case is skipped."""
import os
from pathlib import Path

import pytest

from dvtest import api_admin, api_web

pytestmark = pytest.mark.unit

WEB_SNIPPET = """\
openapi: 3.1.0
x-note: |
  paths:
    not a path: this is inside a block scalar
components:
  parameters:
    Room:
      name: R
paths:
  /api/v1/public/state:
    get:
      x-role: public
      responses:
        '200': {description: ok}
    head:
      x-role: public
  /api/v1/operator/invites/{callsign}:
    delete:
      x-role: operator
      x-guards: [csrf]
    patch:
      x-role: operator
  /api/v1/new/thing:
    post:
      x-role: user
"""


def test_counts_match_the_api3_migration():
    assert len(api_web.WEB_OPS) == 48 and len(api_web.WEB_TEMPLATES) == 34
    assert len(api_admin.ADMIN_OPS) == 28 and len(api_admin.ADMIN_TEMPLATES) == 25
    assert len(api_web.MIGRATION) == 59 and len(api_web.REMOVED) == 55 and len(api_web.UNCHANGED) == 4
    assert len(api_web.REMOVED_WEB) == 30 and len(api_web.REMOVED_ADMIN) == 25
    assert len(api_web.WEB_KEYS) == 52 and len(api_admin.ADMIN_KEYS) == 28           # no duplicate operation
    # the four root probe methods of WEB2 count in the spec's 52, not in the migration
    assert sorted(op.key for op in api_web.WEB_ROOT_OPS) == [("GET", "/health"), ("GET", "/ready"),
                                                              ("HEAD", "/health"), ("HEAD", "/ready")]
    assert all(op.floor == "anonymous" and not op.mutating for op in api_web.WEB_ROOT_OPS)
    assert sorted(p for _s, _m, p, _n in api_web.UNCHANGED) == sorted(api_web.RTC_PATHS)
    # every moved template has its new template in the table of its service, with each listed method
    for service, methods, old, new in api_web.MIGRATION:
        keys = api_web.WEB_KEYS if service == "web" else api_admin.ADMIN_KEYS
        for method in methods:
            assert (method, new) in keys, (service, method, new)
        assert old.startswith("/api/")
    assert sum(len(m) for s, m, _o, _n in api_web.MIGRATION if s == "web") == 48
    assert sum(len(m) for s, m, _o, _n in api_web.MIGRATION if s == "admin") == 28
    # an old template never equals a new one of the same service (that would hide an alias)
    for service, _m, old, _n in api_web.REMOVED:
        keys = api_web.WEB_TEMPLATES if service == "web" else api_admin.ADMIN_TEMPLATES
        assert old not in keys, old
    assert len({old for s, _m, old, _n in api_web.REMOVED_WEB}) == 30


def test_prefix_floors_and_audiences():
    prefixes = {"public": "anonymous", "auth": None, "rtc": "rtc", "user": "user", "operator": "operator"}
    for op in api_web.WEB_OPS:
        assert op.audience in api_web.AUDIENCES, op.path
        assert op.floor in api_web.FLOORS, (op.path, op.floor)
        if prefixes[op.audience]:
            assert op.floor == prefixes[op.audience], op.key
    assert {op.floor for op in api_web.WEB_OPS if op.audience == "auth"} == {"anonymous", "user"}
    for op in api_admin.ADMIN_OPS:
        assert op.audience == "admin" and op.floor in api_admin.ROLES, op.key
    assert api_admin.rank("viewer") < api_admin.rank("operator") < api_admin.rank("admin")


def test_every_probe_is_safe_and_complete():
    """A mutating probe must be an invalid body, a non-JSON body, a missing target, a credential check
    or a session-ending probe that runs on a throwaway login."""
    for op in tuple(api_web.WEB_OPS) + tuple(api_admin.ADMIN_OPS):
        url = op.url()
        assert url.startswith("/api/v1/") and "{" not in url and " " not in url, url
        if op.mutating:
            assert op.method in ("POST", "PUT", "PATCH", "DELETE")
            assert op.body is not None or op.raw is not None or op.fresh or op.missing_ok, \
                "%s %s would act with no body and no missing target" % op.key
        if op.body is not None and not op.missing_ok and op.mutating:
            assert op.body in ({}, api_web.WRONG_JSON) or "sess_id" in op.body, "%s %s: a body that may act" % op.key
        if op.method in ("GET", "HEAD"):
            assert not op.mutating and op.body is None
        for name in op.params:
            assert "{%s}" % name in op.path
    # forced sync and raw config writes get a non-JSON body, never a JSON one that could act
    force = next(o for o in api_web.WEB_OPS if o.path.endswith("/ids/sync/force"))
    assert force.raw and force.raw[1] == "text/plain"
    raw_put = next(o for o in api_admin.ADMIN_OPS if o.method == "PUT")
    assert raw_put.raw and "If-Match" not in raw_put.headers
    # operations that end the session of the caller are marked
    assert {op.key for op in api_web.WEB_OPS if op.fresh} == {
        ("DELETE", "/api/v1/auth/session"), ("DELETE", "/api/v1/user/me/sessions")}
    assert {op.key for op in api_admin.ADMIN_OPS if op.fresh} == {("DELETE", "/api/v1/admin/session")}


def test_declared_success_statuses():
    """API4: every operation declares its success status; the tables carry it (Op.success)."""
    web = {op.key: op.success for op in api_web.WEB_OPS + api_web.WEB_ROOT_OPS}
    adm = {op.key: op.success for op in api_admin.ADMIN_OPS}
    assert {k for k, v in web.items() if v == 201} == {
        ("POST", "/api/v1/auth/register"), ("POST", "/api/v1/operator/invites"),
        ("POST", "/api/v1/operator/accounts/{callsign}/reset")}
    assert {k for k, v in web.items() if v == 204} == {
        ("DELETE", "/api/v1/auth/session"), ("POST", "/api/v1/rtc/close"), ("POST", "/api/v1/rtc/alive")}
    assert {k for k, v in web.items() if v == 202} == {
        ("POST", "/api/v1/user/rooms/{R}/messages"), ("POST", "/api/v1/user/rooms/{R}/messages/directed")}
    assert set(web.values()) == {200, 201, 202, 204}
    assert {k for k, v in adm.items() if v != 200} == {("DELETE", "/api/v1/admin/session"),
                                                       ("POST", "/api/v1/admin/system/restart-node")}
    assert adm[("DELETE", "/api/v1/admin/session")] == 204 and adm[("POST", "/api/v1/admin/system/restart-node")] == 202


SUCCESS_SNIPPET = """\
paths:
  /a:
    post:
      x-role: user
      responses: &shared
        default: {$ref: '#/components/responses/ProblemFallback'}
        '201': {description: created}
        '400': {$ref: '#/components/responses/BadRequest'}
      x-note: after the responses
    put:
      responses: *shared
  /b:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                properties:
                  '204': {type: string}
        '206': {description: partial}
        '503': {description: busy}
    delete:
      description: no 2xx here
      responses:
        '404': {description: gone}
components:
  responses:
    Ok:
      '299': {description: not a path}
"""


def test_parse_spec_success_follows_anchors_and_indent():
    got = api_web.parse_spec_success(SUCCESS_SNIPPET)
    assert got == {("POST", "/a"): (201,), ("PUT", "/a"): (201,), ("GET", "/b"): (200, 206), ("DELETE", "/b"): ()}
    ops = [api_web._op("POST", "/a", "user", success=201), api_web._op("GET", "/b", "user"),
           api_web._op("DELETE", "/b", "user"), api_web._op("PUT", "/a", "user"), api_web._op("GET", "/zz", "user")]
    assert api_web.success_mismatches(ops, got) == [("DELETE", "/b", 200, ()), ("PUT", "/a", 200, (201,))]
    with pytest.raises(ValueError):
        api_web.parse_spec_success("openapi: 3.1.0\n")


def test_removed_probe_paths_are_concrete_and_distinct():
    seen = set()
    for _s, methods, old, _n in api_web.REMOVED:
        path = api_web.removed_probe_path(old)
        assert "{" not in path and path.startswith("/api/")
        seen.add((path, methods))
    assert api_web.removed_probe_path("/api/v1/edges/{kind}/clients/{circuit}/kick") == "/api/v1/edges/xlx/clients/0.0/kick"


def test_parse_spec_ops_reads_paths_and_methods_only():
    ops = api_web.parse_spec_ops(WEB_SNIPPET)
    assert ops == {("GET", "/api/v1/public/state"): "public", ("HEAD", "/api/v1/public/state"): "public",
                   ("DELETE", "/api/v1/operator/invites/{callsign}"): "operator",
                   ("PATCH", "/api/v1/operator/invites/{callsign}"): "operator",
                   ("POST", "/api/v1/new/thing"): "user"}
    with pytest.raises(ValueError):
        api_web.parse_spec_ops("openapi: 3.1.0\ncomponents: {}\n")
    # the next top-level key ends the paths section
    assert api_web.parse_spec_ops("paths:\n  /a:\n    get:\nx-tail:\n  /b:\n    post:\n") == {("GET", "/a"): None}


def test_diff_ops_reports_additions_and_removals():
    table = {("GET", "/a"), ("POST", "/a"), ("GET", "/gone")}
    spec = {("GET", "/a"), ("POST", "/a"), ("PUT", "/a"), ("GET", "/new")}
    assert api_web.diff_ops(table, spec) == ([("GET", "/new"), ("PUT", "/a")], [("GET", "/gone")])
    assert api_web.diff_ops(table, table) == ([], [])


def test_compare_spec_dir_on_fixture_files(tmp_path):
    (tmp_path / "openapi-web.yaml").write_text(WEB_SNIPPET)
    (tmp_path / "openapi-admin.yaml").write_text("paths:\n  /api/v1/admin/health:\n    get:\n      x-role: public\n")
    res = api_web.compare_spec_dir(str(tmp_path), web_table={("GET", "/api/v1/public/state")},
                                   admin_table=api_admin.ADMIN_KEYS)
    added, removed = res["web"]
    assert ("POST", "/api/v1/new/thing") in added and ("HEAD", "/api/v1/public/state") in added and not removed
    assert res["admin"][0] == [] and len(res["admin"][1]) == 27
    with pytest.raises(FileNotFoundError):
        api_web.compare_spec_dir(str(tmp_path / "nope"))


def test_parse_migration_table_reads_rows():
    text = ("| Service | Methods | Old path | New path | Reason |\n|---|---|---|---|---|\n"
            "| web | GET, HEAD | `/api/public/state` | `/api/v1/public/state` | x |\n"
            "| admin | POST, GET, DELETE | `/api/v1/session` | `/api/v1/admin/session` | y |\n")
    assert api_web.parse_migration_table(text) == [
        ("web", ("GET", "HEAD"), "/api/public/state", "/api/v1/public/state"),
        ("admin", ("POST", "GET", "DELETE"), "/api/v1/session", "/api/v1/admin/session")]


def test_mount_forms():
    op = next(o for o in api_admin.ADMIN_OPS if o.path.endswith("/status"))
    assert api_admin.mounted(op, "host") == api_admin.mounted(op, "direct") == "/api/v1/admin/status"
    assert api_admin.mounted(op, "prefix") == "/admin/api/v1/admin/status"
    assert api_admin.moved_to_prefix("/ready") == "/admin/ready"
    with pytest.raises(ValueError):
        api_admin.mounted(op, "elsewhere")


@pytest.mark.skipif(not os.environ.get("DVT_API_SPEC_DIR"), reason="DVT_API_SPEC_DIR (DV-Router docs/api) not set")
def test_tables_match_the_real_specs():
    directory = os.environ["DVT_API_SPEC_DIR"]
    result = api_web.compare_spec_dir(directory)
    for name, (added, removed) in result.items():
        assert not added and not removed, "%s spec vs table: in the spec only %s, in the table only %s" % (
            name, added, removed)
    for name, bad in api_web.compare_spec_success(directory).items():
        assert not bad, "%s: declared success statuses differ (method, path, table, spec 2xx): %s" % (name, bad)
    readme = Path(directory) / "README.md"
    if readme.is_file():
        rows = api_web.parse_migration_table(readme.read_text(encoding="utf-8"))
        assert set(rows) == set(api_web.MIGRATION), "README migration table differs from api_web.MIGRATION"
