# SPDX-License-Identifier: MIT
"""SECRELOAD1 (DV-Router, requested by dv-router): a referenced secret file replaced AT THE SAME PATH is picked up by a configuration
reload, with the reload class of the key that names the file, without a TOML change that matters. Specification: config.md, the
reload table ("`edges.aprsis.passcode_file` contents" are Restart(dv-edge-aprsis)) and the paragraph after it ("Referenced secret
content is part of the key that names the file"); docs/secreload-cases.md here. One case, one rig run (about 4 minutes).

The secret is the APRS-IS passcode file of the private aprsc (profile aprs, rig/config/dv-router/dp15-aprsis.toml): the SUT reads
/etc/dv-router/aprs-secrets/passcode, which is out/<run>/aprs-secrets/passcode of this tester (same uid, mode 0600), so the case
replaces the file where the rig mounts it. A wrong passcode logs in "unverified" on the aprsc (the passcode is a public function of
the callsign, dvtest/aprs_passcode.py), which makes the value in effect observable: dv_edge_aprsis_verified is 1 with the fixture
value and 0 with the throwaway one.

The reload is the admin API's (PUT /api/v1/admin/config/raw, step-up, If-Match: the ETag of the raw read). The API applies nothing for
byte-identical text (`noop`), so every reload carries a changed comment line (dvtest.secret_reload.with_marker): the TOML hash changes,
the configuration does not. The configuration file must be replaceable: scripts/run.sh DVT_SUT_CONFIG_RW=1 (docs/secreload-cases.md).

Stages (in this order, one running edge process throughout; pid of dv-edge-aprsis through proc-stat, restarts through
dv_launcher_child_restarts_total, the login through dv_edge_aprsis_verified):
  0  baseline: the edge is logged in and verified; the capability probe: with other bytes in the passcode file an admin PREVIEW lists
     the Restart of dv-edge-aprsis with `... passcode_file content changed` (an image without that skips; DVT_SECRELOAD1=on fails)
  1  an unchanged reload (marker only) restarts nothing
  2  the file unreadable (mode 000), then missing: the reload is refused, the reply names the key and the path, nothing restarts, the
     old login stays up; after the file is back an unchanged reload restarts nothing
  3  the file replaced in place by a throwaway value of the same size: the reload restarts dv-edge-aprsis (actions name the key and
     `content changed`; new pid; restarts +1) and the new value is in effect (verified 0)
  4  the same reload again restarts nothing
  5  the file replaced atomically (rename) by the fixture value: restart again, verified 1 again
  6  no secret value (old or new) in any admin reply, note or file of out/<run> visible now; the values are left in
     out/<run>/secreload/scan.json (mode 0600) for the end-of-run scan of scripts/run.sh over the collected logs

A second case, test_secreload1_sighup_reload_of_unchanged_toml, uses the documented operator path for "same TOML, secret file
replaced": SIGHUP to the launcher (control action `hup xlxd-sut`, scripts/lib/hup.sh, allowed by dv-router for this case only). No
admin API, no TOML change, no DVT_SUT_CONFIG_RW. The refusal of an unreadable file has no reply to read, so run.sh checks the collected
launcher log after the run (a line after the SIGHUP naming the key and the path, shape recorded in out/<run>/secreload/log-shape.txt).

SECRELOAD2 (DV-Router merge 11e19ace): test_secreload2_preview_and_reload_apply_a_replaced_secret and test_secreload2_guards_and_preconditions
use GET / POST /api/v1/admin/config/reload (a preview and a reload of the current file, no TOML edit, no history entry) with their guards
(401, 403 forbidden / step_up_required, 428 / 412 If-Match, 422 for an unreadable secret) and the config.reload audit records. They skip
on a 404 (DVT_SECRELOAD2=on makes that a failure) and need no DVT_SUT_CONFIG_RW. docs/secreload-cases.md, "SECRELOAD2".

Values are never printed, logged or committed: notes carry lengths only. Run:
  ... DVT_SUT_CONFIG_RW=1 DVT_SECRELOAD1=on DVT_SUT_CONFIG_EXTRA=<web:admin:totp-required:data-ident:dp15-aprsis snippets> \\
      scripts/run.sh -m rig --profile web,aprs -- cases/test_secret_reload.py
"""
import json
import os
import re
import time
import types
from pathlib import Path

import pytest

from dvtest import metrics, secret_reload as sr, webload
from data_support import write_note

pytestmark = pytest.mark.rig

CASE = "SECRELOAD1"
LOG = "secreload.txt"
CHILD = "dv-edge-aprsis"
KEY = "passcode_file"
QUIET_S = 12.0              # one hub refresh (10 s) and a bit: "nothing restarted" is judged after it
EFFECT_S = 60.0             # a restart, its new login and the next hub refresh
SERIES_WAIT_S = 60.0        # the hub series of the edge and of the launcher: the first 10 s publication plus the edge's own start
SUT_PASSCODE_PATH = "/etc/dv-router/aprs-secrets/passcode"    # passcode_file of rig/config/dv-router/dp15-aprsis.toml, as the SUT sees it
STEP_UP_S = 200.0           # the step-up window is 5 minutes; a TOTP step-up costs up to one 30 s step, so it is renewed only when old


def note(target, tag, text):
    write_note(target, CASE, tag, text, log=LOG)


# ---------------------------------------------------------------- fixture

@pytest.fixture(scope="module")
def sr_ctx(sr_base, admin_session):
    """sr_base plus the admin client (admin role, enrolled, step-up on demand) for the reloads through the admin API."""
    sr_base.client = admin_session("admin")
    return sr_base


@pytest.fixture(scope="module")
def sr_base(target, rig_control):
    """The passcode file as this tester sees it, the values and the scan list. Restores the fixture value, the mode and the name of
    the file at the end, whatever happened."""
    out = Path(target.out)
    path = out / "aprs-secrets" / "passcode"
    if not path.is_file():
        pytest.skip("no %s: the case needs --profile aprs (run.sh writes the fixture passcode file there)" % path)
    client = None
    original = path.read_text()
    old = original
    try:
        new = sr.collision_free_passcode(old, out, skip=["aprs-secrets/passcode", "secreload/scan.json"], skip_dirs=["capture"])
    except ValueError as exc:
        pytest.fail("cannot choose a throwaway passcode: %s" % exc)
    entries = sr.entries_for(old, new)
    scan_dir = out / "secreload"
    scan_dir.mkdir(exist_ok=True)
    scan_file = scan_dir / "scan.json"
    fd = os.open(scan_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(entries, fh)
    os.chmod(scan_file, 0o600)
    ctx = types.SimpleNamespace(out=out, path=path, client=client, old=old, new=new, entries=entries, leaks=sr.Leaks(entries),
                                control=rig_control, target=target, marker=0, original_mode=path.stat().st_mode & 0o777,
                                stepped=None)
    yield ctx
    away = path.with_name(path.name + ".away")
    try:
        if away.exists() and not path.exists():
            os.replace(away, path)
        write_in_place(path, original)
        os.chmod(path, ctx.original_mode)
    except OSError:
        pass


# ---------------------------------------------------------------- helpers

def say(ctx, tag, text):
    """A note; refuses to write a line that holds a secret value."""
    assert ctx.leaks.check("note " + tag, text), "a secret value was about to be written to a note: %s" % ctx.leaks.report()
    note(ctx.target, tag, text)


def write_in_place(path, text):
    """Same inode, same size when the value has the same length (equal-size rewrites must be detected, config.md)."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def write_atomic(path, text):
    """A new inode: temporary file in the same directory (0600), rename over the path."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def step_up(ctx):
    """Opens the step-up window when it is older than STEP_UP_S."""
    if ctx.stepped is None or time.monotonic() - ctx.stepped > STEP_UP_S:
        ctx.client.step_up()
        ctx.stepped = time.monotonic()


def raw_config(ctx):
    """(TOML text, ETag header) of GET /config/raw after a step-up; the reply is checked for secret values."""
    client = ctx.client
    step_up(ctx)
    resp = client.get(client.path("/config/raw"), expect=200)
    ctx.leaks.check("GET config/raw", resp.body)
    return resp.text(), resp.headers.get("etag", "")


def put_config(ctx, text, etag, mode):
    client = ctx.client
    step_up(ctx)
    resp = client.request("PUT", client.path("/config/raw") + "?mode=%s&comment=secreload1" % mode, raw_body=text.encode("utf-8"),
                          headers={"Content-Type": "text/plain", "If-Match": etag})
    ctx.leaks.check("PUT config/raw mode=%s" % mode, resp.body)
    return resp


def reload_with_marker(ctx, mode="apply"):
    """An admin reload whose only difference is a comment line. Returns (Response, parsed JSON or text)."""
    text, etag = raw_config(ctx)
    ctx.marker += 1
    resp = put_config(ctx, sr.with_marker(text, ctx.marker), etag, mode)
    try:
        return resp, resp.json()
    except Exception:
        return resp, resp.text()


def preview_unchanged(ctx):
    """A PREVIEW of the TOML as it is (no write, no hash gate): the actions the secrets alone would cause."""
    text, etag = raw_config(ctx)
    resp = put_config(ctx, text, etag, "preview")
    assert resp.status == 200, "preview of the unchanged TOML answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:200])
    return resp.json()


def hub(ctx):
    """(verified or None, restarts of the APRS-IS edge or None, restarts of all children) from the hub metrics."""
    samples = metrics.parse(ctx.control.metrics("xlxd-sut"))
    own, everything = sr.child_restarts(samples)
    return sr.verified(samples), own, everything


def series_strict():
    """True when the edge is expected: DVT_SECRELOAD1 or DVT_SECRELOAD2 is on (the run asks for the cases, so a missing edge is a failure)."""
    return any(os.environ.get(name, "").lower() in ("on", "1", "true", "yes") for name in ("DVT_SECRELOAD1", "DVT_SECRELOAD2"))


def wait_edge_series(ctx, strict, timeout=SERIES_WAIT_S, poll=2.0):
    """(verified, restarts of the edge, restarts of all children) once BOTH series are in the hub metrics, polled for up to `timeout`
    seconds: the launcher families reach the hub with the first 10 s publication after the SUT started (run 20261110T085417Z read
    them 7 s after the start, saw `dv_edge_aprsis_verified` but no `dv_launcher_child_restarts_total`, and skipped). After the bound a
    missing series is a FAILURE when `strict` (DVT_SECRELOAD1 / DVT_SECRELOAD2 = on: the edge is expected), else a skip that names
    which series is missing."""
    t0 = time.monotonic()
    missing, error = "both", ""
    while True:
        try:
            ver, own, everything = hub(ctx)
        except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
            ver = own = everything = None
            error = "; the last metrics read failed (%s)" % type(exc).__name__
        else:
            error = ""
        if ver is not None and own is not None:
            if time.monotonic() - t0 > 1.0:
                say(ctx, "series", "dv_edge_aprsis_verified and dv_launcher_child_restarts_total{child=%s} in the hub metrics after %.0f s" % (
                    CHILD, time.monotonic() - t0))
            return ver, own, everything
        missing = " and ".join(name for name, v in (("dv_edge_aprsis_verified", ver), ("dv_launcher_child_restarts_total{child=%s}" % CHILD, own))
                               if v is None)
        if time.monotonic() - t0 >= timeout:
            break
        time.sleep(poll)
    reason = ("no %s in the hub metrics %.0f s after the case started polling%s: the APRS-IS edge is not running (--profile aprs with "
              "dp15-aprsis.toml) or the image does not export the launcher families" % (missing, time.monotonic() - t0, error))
    if strict:
        pytest.fail("the edge is expected (strict mode) but " + reason)
    pytest.skip(reason)


def pid_of_edge(ctx):
    """(pid, starttime) of dv-edge-aprsis, None while no such process runs."""
    try:
        sample = webload.parse_procstat_out(ctx.control.proc_stat("xlxd-sut", CHILD))
    except RuntimeError as exc:
        if "error 3" in str(exc):
            return None
        raise
    return sample.pid, sample.starttime


def wait_for(fn, timeout, poll=2.0):
    """The first truthy fn() within `timeout` seconds, else the last value."""
    end = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() >= end:
            return value
        time.sleep(poll)


def assert_nothing_restarted(ctx, before, what):
    """Judged after a hub refresh: the process is the same, the restart counters did not move, the login is as it was."""
    time.sleep(QUIET_S)
    pid = pid_of_edge(ctx)
    ver, own, everything = hub(ctx)
    assert pid == before.pid, "%s: dv-edge-aprsis restarted (process %s -> %s)" % (what, pid_text(before.pid), pid_text(pid))
    assert own == before.restarts and everything == before.all_restarts, \
        "%s: dv_launcher_child_restarts_total moved (edge %s -> %s, all children %s -> %s)" % (
            what, before.restarts, own, before.all_restarts, everything)
    assert ver == before.verified, "%s: dv_edge_aprsis_verified %s -> %s" % (what, before.verified, ver)


def state(ctx):
    ver, own, everything = hub(ctx)
    return types.SimpleNamespace(pid=pid_of_edge(ctx), restarts=own, all_restarts=everything, verified=ver)


def pid_text(pid):
    """The pid of a (pid, starttime) pair for a note; `none` while no process runs."""
    return "none" if pid is None else str(pid[0])


def new_pid(ctx, before):
    pid = pid_of_edge(ctx)
    return pid if pid is not None and pid != before.pid else None


def counted(ctx, before):
    now = state(ctx)
    return now if now.restarts is not None and now.restarts >= (before.restarts or 0) + 1 else None


def assert_restarted(ctx, before, reply, what):
    """The reload restarted the owning child: the reply names the key and `content changed`, the process is a new one, the restart
    counter rose by one."""
    actions = sr.reload_actions(reply)
    assert actions, "%s: the reload reply lists no restart of %s: %s" % (what, CHILD, json.dumps(reply.get("actions") if isinstance(reply, dict) else reply)[:300])
    assert any(sr.names_secret_change(a) for a in actions), "%s: no action names %s and `content changed`: %s" % (what, KEY, json.dumps(actions)[:300])
    now = wait_for(lambda: new_pid(ctx, before), 30.0, 1.0)
    assert now, "%s: dv-edge-aprsis did not restart (process %s)" % (what, pid_text(before.pid))
    after = wait_for(lambda: counted(ctx, before), EFFECT_S)
    assert after, "%s: dv_launcher_child_restarts_total of %s did not rise within %.0f s" % (what, CHILD, EFFECT_S)
    assert after.restarts == (before.restarts or 0) + 1, "%s: restarts rose by %s, want 1 (one restart for the changed file)" % (
        what, after.restarts - (before.restarts or 0))
    return after


def assert_refused(ctx, resp, body, path_in_sut, what):
    assert resp.status >= 400 and resp.status != 429, "%s: the reload answered %d, want a refusal" % (what, resp.status)
    names_key, names_path = sr.error_names(body, KEY, path_in_sut)
    assert names_key and names_path, "%s: the refusal names the key %s (%s) and the path (%s): %s" % (
        what, KEY, names_key, names_path, ctx.client.scrub(resp.text())[:300])


# ---------------------------------------------------------------- the case

def test_secreload1_passcode_file_replaced_at_the_same_path(target, rig_control, sr_ctx):
    ctx = sr_ctx
    strict = os.environ.get("DVT_SECRELOAD1", "").lower() in ("on", "1", "true", "yes")
    say(ctx, "setup", "passcode file: old value %s, throwaway value %s (same size), mode %o; strict=%s" % (
        sr.digest_note(ctx.old.strip()), sr.digest_note(ctx.new.strip()), ctx.original_mode, strict))

    # ---- 0 baseline and capability
    ver, own, everything = wait_edge_series(ctx, series_strict())
    if not wait_for(lambda: hub(ctx)[0], 60.0):
        pytest.fail("the APRS-IS edge is not logged in and verified on the aprsc with the fixture passcode (dv_edge_aprsis_verified 0): "
                    "check out/<run>/logs/aprsc.log and the passcode file")
    raw_text, _etag = raw_config(ctx)
    path_match = re.search(r"(?m)^\s*passcode_file\s*=\s*\"([^\"]+)\"", raw_text)
    assert path_match, "the raw configuration has no passcode_file under [edges.aprsis]"
    sut_path = path_match.group(1)
    base = state(ctx)
    assert base.pid is not None, "no dv-edge-aprsis process"
    say(ctx, "0", "baseline: edge process %s, restarts %s (all children %s), verified %s, passcode_file %s" % (
        pid_text(base.pid), base.restarts, base.all_restarts, base.verified, sut_path))

    write_in_place(ctx.path, ctx.new)                  # only for the preview: nothing reloads it
    try:
        probe = preview_unchanged(ctx)
    finally:
        write_in_place(ctx.path, ctx.old)
    has, why = sr.capability(probe, strict)
    say(ctx, "0", "SECRELOAD1 capability: %s (%s)" % (has, why))
    if not has:
        if strict:
            pytest.fail("DVT_SECRELOAD1=on but the image lacks the rule: " + why)
        pytest.skip("image without SECRELOAD1: " + why)

    # ---- 1 an unchanged reload restarts nothing
    resp, body = reload_with_marker(ctx)
    if sr.read_only_reply(resp.status, body):
        pytest.skip("the configuration file is read-only in the SUT container: run.sh needs DVT_SUT_CONFIG_RW=1 (docs/secreload-cases.md)")
    assert resp.status == 200, "reload with a comment change answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:300])
    assert not sr.reload_actions(body), "an unchanged secret and a comment change list a restart of %s: %s" % (CHILD, json.dumps(body.get("actions"))[:300])
    assert_nothing_restarted(ctx, base, "1 unchanged reload")
    say(ctx, "1", "unchanged reload (comment only): HTTP 200, no restart action, process, counters and login unchanged")

    # ---- 2 unreadable and missing: refused, running configuration kept
    os.chmod(ctx.path, 0o000)
    try:
        resp, body = reload_with_marker(ctx)
        assert_refused(ctx, resp, body, sut_path, "2a mode 000")
        say(ctx, "2a", "mode 000: HTTP %d, the refusal names %s and the path" % (resp.status, KEY))
        assert_nothing_restarted(ctx, base, "2a mode 000")
    finally:
        os.chmod(ctx.path, 0o600)
    away = ctx.path.with_name(ctx.path.name + ".away")
    os.replace(ctx.path, away)
    try:
        resp, body = reload_with_marker(ctx)
        assert_refused(ctx, resp, body, sut_path, "2b missing")
        say(ctx, "2b", "missing file: HTTP %d, the refusal names %s and the path" % (resp.status, KEY))
        assert_nothing_restarted(ctx, base, "2b missing")
    finally:
        os.replace(away, ctx.path)
    resp, body = reload_with_marker(ctx)
    assert resp.status == 200, "reload after the file is back answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:300])
    assert not sr.reload_actions(body), "the restored file (same content) lists a restart: %s" % json.dumps(body.get("actions"))[:300]
    assert_nothing_restarted(ctx, base, "2c file back")
    say(ctx, "2c", "file back with the same content: reload applied, nothing restarted; the old login stayed up through 2a and 2b")

    # ---- 3 replaced in place by the throwaway value: restart, new value in effect
    write_in_place(ctx.path, ctx.new)
    resp, body = reload_with_marker(ctx)
    assert resp.status == 200, "reload after the replacement answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:300])
    after = assert_restarted(ctx, base, body, "3 in-place replacement")
    unverified = wait_for(lambda: hub(ctx)[0] is False, EFFECT_S)
    assert unverified, "the new passcode is not in effect: dv_edge_aprsis_verified stayed %s (a throwaway value logs in unverified)" % hub(ctx)[0]
    say(ctx, "3", "same-size in-place replacement: restart action names %s content changed, new process %s, restarts %s -> %s, "
        "verified 1 -> 0 (the new value is what the edge logged in with)" % (KEY, pid_text(after.pid), base.restarts, after.restarts))

    # ---- 4 the same reload again: nothing
    keep = state(ctx)
    resp, body = reload_with_marker(ctx)
    assert resp.status == 200 and not sr.reload_actions(body), "an unchanged reload after the replacement: HTTP %d, actions %s" % (
        resp.status, json.dumps(body.get("actions") if isinstance(body, dict) else body)[:200])
    assert_nothing_restarted(ctx, keep, "4 repeat")
    say(ctx, "4", "repeat reload with the same secret: nothing restarted")

    # ---- 5 the fixture value back by an atomic replacement: restart, verified again
    write_atomic(ctx.path, ctx.old)
    resp, body = reload_with_marker(ctx)
    assert resp.status == 200, "reload after the restoring replacement answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:300])
    back = assert_restarted(ctx, keep, body, "5 atomic replacement")
    assert wait_for(lambda: hub(ctx)[0] is True, EFFECT_S), "the fixture passcode is not in effect again (dv_edge_aprsis_verified %s)" % hub(ctx)[0]
    say(ctx, "5", "atomic replacement (new inode) by the fixture value: restart, new process %s, restarts %s, verified 0 -> 1" % (
        pid_text(back.pid), back.restarts))

    # ---- 6 no secret value in any reply, note or file visible now
    assert not ctx.leaks.found, "a secret value appeared in: %s" % ctx.leaks.report()
    hits = sr.scan_tree(ctx.out, ctx.entries, skip=["aprs-secrets/passcode", "secreload/scan.json"], skip_dirs=["capture"])
    assert not hits, "a secret value is in out/<run> files: %s" % "; ".join("%s (%s, line %d)" % h for h in hits[:6])
    say(ctx, "6", "no secret value in any admin reply, the notes or the files of out/<run> visible now; the collected logs are "
        "scanned by run.sh from out/<run>/secreload/scan.json after the run")


# ---------------------------------------------------------------- SIGHUP variant: the same TOML, the file replaced

def hup(ctx, what):
    """SIGHUP to the launcher through the control channel; returns the host epoch time of the signal."""
    t_hup = ctx.control.hup()
    say(ctx, "hup", "%s: SIGHUP sent to the launcher (t_hup %.3f)" % (what, t_hup))
    return t_hup


def hup_logic_restarted(ctx, before, what):
    """What a reload that changed a secret must show without a reply to read: a new process of the owning child within 30 s and
    exactly one more restart of it (counters checked after the hub refresh; the other children must not have restarted)."""
    now = wait_for(lambda: new_pid(ctx, before), 30.0, 1.0)
    assert now, "%s: dv-edge-aprsis did not restart after the SIGHUP (process %s)" % (what, pid_text(before.pid))
    after = wait_for(lambda: counted(ctx, before), EFFECT_S)
    assert after, "%s: dv_launcher_child_restarts_total of %s did not rise within %.0f s" % (what, CHILD, EFFECT_S)
    time.sleep(QUIET_S)
    last = state(ctx)
    assert last.restarts == (before.restarts or 0) + 1, "%s: the restarts of %s rose by %s, want 1" % (
        what, CHILD, last.restarts - (before.restarts or 0))
    assert last.all_restarts == before.all_restarts + 1, "%s: other children restarted too (all children %s -> %s)" % (
        what, before.all_restarts, last.all_restarts)
    return last


def test_secreload1_sighup_reload_of_unchanged_toml(target, rig_control, sr_base):
    ctx = sr_base
    strict = os.environ.get("DVT_SECRELOAD1", "").lower() in ("on", "1", "true", "yes")
    say(ctx, "H0", "SIGHUP variant: old value %s, throwaway value %s (same size); strict=%s" % (
        sr.digest_note(ctx.old.strip()), sr.digest_note(ctx.new.strip()), strict))
    ver, own, everything = wait_edge_series(ctx, series_strict())
    if not wait_for(lambda: hub(ctx)[0], 60.0):
        # an earlier case of this module may have left the throwaway value running: put the fixture value back and reload
        write_atomic(ctx.path, ctx.old)
        hup(ctx, "H0 repair")
        if not wait_for(lambda: hub(ctx)[0], EFFECT_S):
            pytest.fail("the APRS-IS edge is not logged in and verified with the fixture passcode, also not after a SIGHUP repair")
    base = state(ctx)
    assert base.pid is not None, "no dv-edge-aprsis process"
    say(ctx, "H0", "baseline: edge process %s, restarts %s (all children %s), verified %s" % (
        pid_text(base.pid), base.restarts, base.all_restarts, base.verified))

    # ---- H1 the file rewritten in place (same inode and size), the TOML untouched, SIGHUP: restart, new value in effect
    write_in_place(ctx.path, ctx.new)
    hup(ctx, "H1 in-place replacement")
    if not wait_for(lambda: new_pid(ctx, base), 30.0, 1.0):
        reason = "no restart of %s 30 s after a SIGHUP with a changed passcode file: the image does not re-read referenced secrets on SIGHUP (config.md)" % CHILD
        if strict:
            pytest.fail("DVT_SECRELOAD1=on but " + reason)
        pytest.skip("image without SECRELOAD1, or SIGHUP not handled: " + reason)
    changed = hup_logic_restarted(ctx, base, "H1")
    assert wait_for(lambda: hub(ctx)[0] is False, EFFECT_S), \
        "the new passcode is not in effect: dv_edge_aprsis_verified stayed %s (a throwaway value logs in unverified)" % hub(ctx)[0]
    say(ctx, "H1", "SIGHUP after an in-place same-size replacement: new process %s, restarts %s -> %s, other children unchanged, "
        "verified 1 -> 0" % (pid_text(changed.pid), base.restarts, changed.restarts))

    # ---- H2 a second SIGHUP with nothing changed: nothing restarts
    keep = state(ctx)
    hup(ctx, "H2 nothing changed")
    assert_nothing_restarted(ctx, keep, "H2 second SIGHUP")
    say(ctx, "H2", "second SIGHUP, secret and TOML unchanged: nothing restarted, login unchanged")

    # ---- H3 the fixture value back by an atomic replacement: restart, verified again
    write_atomic(ctx.path, ctx.old)
    hup(ctx, "H3 atomic replacement")
    if not wait_for(lambda: new_pid(ctx, keep), 30.0, 1.0):
        pytest.fail("H3: dv-edge-aprsis did not restart after the SIGHUP that follows an atomic replacement")
    back = hup_logic_restarted(ctx, keep, "H3")
    assert wait_for(lambda: hub(ctx)[0] is True, EFFECT_S), "the fixture passcode is not in effect again (dv_edge_aprsis_verified %s)" % hub(ctx)[0]
    say(ctx, "H3", "SIGHUP after an atomic replacement (new inode): new process %s, restarts %s, verified 0 -> 1" % (
        pid_text(back.pid), back.restarts))

    # ---- H4 the file unreadable, SIGHUP: refused, the running generation and login stay
    held = state(ctx)
    os.chmod(ctx.path, 0o000)
    try:
        t_refused = hup(ctx, "H4 mode 000")
        assert_nothing_restarted(ctx, held, "H4 mode 000")
    finally:
        os.chmod(ctx.path, 0o600)
    expect = ctx.out / "secreload" / "log-expect.json"
    fd = os.open(expect, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"t_hup": t_refused, "key": KEY, "path": SUT_PASSCODE_PATH}, fh)
    say(ctx, "H4", "mode 000 and SIGHUP: process, counters and the verified login unchanged (the old login stays up); the launcher log "
        "line that names %s and the path is checked by run.sh after the run (out/<run>/secreload/log-shape.txt)" % KEY)

    # ---- H5 the file back: a SIGHUP restarts nothing (same content as the running generation)
    hup(ctx, "H5 file back")
    assert_nothing_restarted(ctx, held, "H5 file back")
    say(ctx, "H5", "file readable again, SIGHUP: nothing restarted")

    assert not ctx.leaks.found, "a secret value appeared in: %s" % ctx.leaks.report()
    hits = sr.scan_tree(ctx.out, ctx.entries, skip=["aprs-secrets/passcode", "secreload/scan.json"], skip_dirs=["capture"])
    assert not hits, "a secret value is in out/<run> files: %s" % "; ".join("%s (%s, line %d)" % h for h in hits[:6])


# ---------------------------------------------------------------- SECRELOAD2: GET / POST /api/v1/admin/config/reload

RELOAD_ROUTE = "/config/reload"
STEP_UP_WINDOW_S = 300.0        # admin.md: re-authentication within the last 5 minutes


def jbody(resp):
    """The parsed JSON of a response, else its text (never raises)."""
    try:
        return resp.json()
    except Exception:
        return resp.text()


def reload_get(ctx, client=None, stepped=True):
    """GET /config/reload (the preview). The reply is checked for secret values."""
    client = client or ctx.client
    if stepped:
        step_up(ctx)
    resp = client.get(client.path(RELOAD_ROUTE))
    ctx.leaks.check("GET config/reload", resp.body)
    return resp


def reload_post(ctx, etag, client=None, stepped=True):
    """POST /config/reload with `If-Match: etag` (None: no If-Match header), JSON body {}. Checked for secret values."""
    client = client or ctx.client
    if stepped:
        step_up(ctx)
    headers = {} if etag is None else {"If-Match": etag}
    resp = client.post(client.path(RELOAD_ROUTE), headers=headers)
    ctx.leaks.check("POST config/reload", resp.body)
    return resp


def history_len(ctx):
    step_up(ctx)
    resp = ctx.client.get(ctx.client.path("/config/history"))
    ctx.leaks.check("GET config/history", resp.body)
    return sr.history_count(jbody(resp)) if resp.status == 200 else None


def audit_tail(ctx):
    step_up(ctx)
    resp = ctx.client.get(ctx.client.path("/audit") + "?limit=200")
    ctx.leaks.check("GET audit", resp.body)
    assert resp.status == 200, "GET audit answered %d" % resp.status
    return jbody(resp)


def is_subsequence(wanted, seen):
    it = iter(seen)
    return all(any(w == x for x in it) for w in wanted)


def require_route(ctx, strict):
    """The preview route answers (200, 422 or 403 are all "present"); 404 is an image without SECRELOAD2."""
    resp = reload_get(ctx)
    if resp.status == 404:
        reason = "GET %s answered 404: the image has no admin reload route (SECRELOAD2)" % ctx.client.path(RELOAD_ROUTE)
        if strict:
            pytest.fail("DVT_SECRELOAD2=on but " + reason)
        pytest.skip("image without SECRELOAD2: " + reason)
    return resp


def test_secreload2_preview_and_reload_apply_a_replaced_secret(target, rig_control, sr_ctx):
    ctx = sr_ctx
    strict = os.environ.get("DVT_SECRELOAD2", "").lower() in ("on", "1", "true", "yes")
    say(ctx, "R0", "SECRELOAD2: old value %s, throwaway value %s (same size); strict=%s" % (
        sr.digest_note(ctx.old.strip()), sr.digest_note(ctx.new.strip()), strict))
    first = require_route(ctx, strict)
    ver, own, everything = wait_edge_series(ctx, series_strict())
    raw_text, file_etag = raw_config(ctx)
    path_match = re.search(r"(?m)^\s*passcode_file\s*=\s*\"([^\"]+)\"", raw_text)
    assert path_match, "the raw configuration has no passcode_file under [edges.aprsis]"
    sut_path = path_match.group(1)

    # ---- R0 the preview of an unchanged file: the shape, no_change, the ETag of the file
    assert first.status == 200, "GET config/reload answered %d: %s" % (first.status, ctx.client.scrub(first.text())[:300])
    body = jbody(first)
    problems = sr.preview_problems(body, first.headers.get("etag"))
    assert not problems, "the preview of an unchanged file: %s" % "; ".join(problems)
    assert body["no_change"] is True and body["changed"] == [] and body["actions"] == [], \
        "the preview of an unchanged file lists changes: %s" % json.dumps({k: body[k] for k in ("changed", "no_change", "actions")})[:300]
    assert sr.etag_value(first.headers.get("etag")) == sr.etag_value(file_etag), "the preview ETag differs from the ETag of GET config/raw"
    if not wait_for(lambda: hub(ctx)[0], 60.0):
        pytest.fail("the APRS-IS edge is not logged in and verified with the fixture passcode (dv_edge_aprsis_verified 0)")
    base = state(ctx)
    hist0 = history_len(ctx)
    say(ctx, "R0", "preview of the unchanged file: no_change, changed [], ETag equals the file's; baseline edge process %s, restarts %s, "
        "history entries %s" % (pid_text(base.pid), base.restarts, hist0))

    # ---- R1 the file replaced in place (same inode and size): the preview lists the key, applies nothing
    write_in_place(ctx.path, ctx.new)
    prev = reload_get(ctx)
    assert prev.status == 200, "preview after the replacement answered %d: %s" % (prev.status, ctx.client.scrub(prev.text())[:300])
    body = jbody(prev)
    problems = sr.preview_problems(body, prev.headers.get("etag"))
    assert not problems, "the preview after the replacement: %s" % "; ".join(problems)
    assert body["changed"] == [sr.RELOAD_KEY] and body["no_change"] is False, \
        "preview changed %s no_change %s, want exactly [%s] and false" % (body["changed"], body["no_change"], sr.RELOAD_KEY)
    assert any(sr.names_secret_change(a) for a in sr.reload_actions(body)), \
        "no preview action restarts %s and names `content changed`: %s" % (CHILD, json.dumps(body["actions"])[:300])
    assert sr.etag_value(prev.headers.get("etag")) == sr.etag_value(file_etag), "the TOML changed under a preview"
    time.sleep(3.0)
    assert pid_of_edge(ctx) == base.pid, "a preview restarted dv-edge-aprsis (it applies nothing)"
    say(ctx, "R1", "preview after a same-size in-place replacement: changed [%s], restart action names the key and `content changed`, "
        "nothing applied" % sr.RELOAD_KEY)

    # ---- R2 POST with If-Match: applied, no TOML edit, no history entry, the owning child restarts, the new value is in effect
    resp = reload_post(ctx, prev.headers.get("etag"))
    assert resp.status == 200, "POST config/reload answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:300])
    applied = jbody(resp)
    problems = sr.applied_problems(applied, resp.headers.get("etag"))
    assert not problems, "the reload reply: %s" % "; ".join(problems)
    assert applied["changed"] == [sr.RELOAD_KEY] and applied["no_change"] is False and applied["pending_restart"] == [], \
        "reload reply changed %s no_change %s pending_restart %s" % (applied["changed"], applied["no_change"], applied["pending_restart"])
    _text, etag_after = raw_config(ctx)
    assert sr.etag_value(etag_after) == sr.etag_value(file_etag), "the reload edited the TOML (ETag %s -> %s)" % (file_etag, etag_after)
    hist1 = history_len(ctx)
    if hist0 is None:
        say(ctx, "R2", "GET config/history has no list shape the case knows: the 'no history entry' check is not made")
    else:
        assert hist1 == hist0, "the reload added a history entry (%s -> %s)" % (hist0, hist1)
    changed = assert_restarted(ctx, base, applied, "R2 reload")
    assert wait_for(lambda: hub(ctx)[0] is False, EFFECT_S), \
        "the new passcode is not in effect: dv_edge_aprsis_verified stayed %s (a throwaway value logs in unverified)" % hub(ctx)[0]
    say(ctx, "R2", "POST with If-Match: applied (inc %s, gen %s), changed [%s], new process %s, restarts %s -> %s, verified 1 -> 0; TOML ETag and "
        "history unchanged" % (applied["inc"], applied["gen"], sr.RELOAD_KEY, pid_text(changed.pid), base.restarts, changed.restarts))

    # ---- R3 a second POST: no_change, empty changed and actions, nothing restarts
    keep = state(ctx)
    again = reload_post(ctx, resp.headers.get("etag"))
    assert again.status == 200, "second POST answered %d: %s" % (again.status, ctx.client.scrub(again.text())[:300])
    second = jbody(again)
    problems = sr.applied_problems(second, again.headers.get("etag"))
    assert not problems, "the second reload reply: %s" % "; ".join(problems)
    assert second["no_change"] is True and second["changed"] == [] and second["actions"] == [], \
        "second reload: no_change %s changed %s actions %s" % (second["no_change"], second["changed"], json.dumps(second["actions"])[:200])
    assert (second["inc"], second["gen"]) == (applied["inc"], applied["gen"]), \
        "the second reload advanced the generation (%s.%s -> %s.%s)" % (applied["inc"], applied["gen"], second["inc"], second["gen"])
    assert_nothing_restarted(ctx, keep, "R3 second POST")
    quiet = jbody(reload_get(ctx))
    assert isinstance(quiet, dict) and quiet.get("no_change") is True and quiet.get("changed") == [], "the preview after the reload is not no_change"
    say(ctx, "R3", "second POST: no_change true, changed and actions empty, generation unchanged, nothing restarted; the preview agrees")

    # ---- R4 the fixture value back by an atomic replacement: preview, POST, restart, verified again
    write_atomic(ctx.path, ctx.old)
    prev = reload_get(ctx)
    assert jbody(prev).get("changed") == [sr.RELOAD_KEY], "preview after the restoring replacement: %s" % ctx.client.scrub(prev.text())[:200]
    resp = reload_post(ctx, prev.headers.get("etag"))
    assert resp.status == 200, "POST after the restoring replacement answered %d: %s" % (resp.status, ctx.client.scrub(resp.text())[:300])
    back = assert_restarted(ctx, keep, jbody(resp), "R4 atomic replacement")
    assert wait_for(lambda: hub(ctx)[0] is True, EFFECT_S), "the fixture passcode is not in effect again (dv_edge_aprsis_verified %s)" % hub(ctx)[0]
    say(ctx, "R4", "atomic replacement (new inode) by the fixture value: preview lists the key, POST restarts, new process %s, verified 0 -> 1" % pid_text(back.pid))

    # ---- R5 an unreadable secret file: preview and POST are 422 naming the key and the path, the running login stays
    held = state(ctx)
    os.chmod(ctx.path, 0o000)
    try:
        prev = reload_get(ctx)
        post_etag = prev.headers.get("etag") or raw_config(ctx)[1]
        post = reload_post(ctx, post_etag)
        for what, r in (("preview", prev), ("POST", post)):
            body = jbody(r)
            assert r.status == 422 and sr.problem_code(body) == "validation", \
                "%s with an unreadable file answered %d %s, want 422 validation" % (what, r.status, sr.problem_code(body))
            names_key, names_path = sr.error_names(body, KEY, sut_path)
            assert names_key and names_path, "%s: the 422 names the key %s (%s) and the path (%s): %s" % (
                what, KEY, names_key, names_path, ctx.client.scrub(r.text())[:300])
        assert_nothing_restarted(ctx, held, "R5 unreadable file")
    finally:
        os.chmod(ctx.path, 0o600)
    say(ctx, "R5", "mode 000: preview and POST are 422 validation naming %s and the path; process, counters and the verified login unchanged" % KEY)

    # ---- R6 the audit: requested / applied / unchanged / invalid, key names only
    me = ctx.client.identity["user"]
    audit = audit_tail(ctx)
    mine = [(a, u, r, c) for a, u, r, c in sr.reload_audit(audit, user=me, actions=("config.reload",))]
    results = [(r, c) for _a, _u, r, c in mine]
    expected = [("requested", []), ("applied", [sr.RELOAD_KEY]), ("requested", []), ("unchanged", []), ("requested", []),
                ("applied", [sr.RELOAD_KEY]), ("requested", []), ("invalid", None)]
    got = [(r, c if r != "invalid" else None) for r, c in results]
    assert is_subsequence(expected, got), "the audit lacks the config.reload sequence %s; saw %s" % (expected, got[-12:])
    invalid = [c for r, c in results if r == "invalid"]
    assert invalid and any(isinstance(c, list) and any(sr.RELOAD_KEY in x or KEY in x for x in c) for c in invalid), \
        "the invalid record does not name the key: %s" % invalid
    say(ctx, "R6", "audit: config.reload requested / applied / requested / unchanged / requested / applied / requested / invalid, "
        "changed key names only")

    # ---- R7 no secret value anywhere visible now
    assert not ctx.leaks.found, "a secret value appeared in: %s" % ctx.leaks.report()
    hits = sr.scan_tree(ctx.out, ctx.entries, skip=["aprs-secrets/passcode", "secreload/scan.json"], skip_dirs=["capture"])
    assert not hits, "a secret value is in out/<run> files (the audit file is under sut-state/admin): %s" % "; ".join(
        "%s (%s, line %d)" % h for h in hits[:6])
    say(ctx, "R7", "no secret value in any reply, the audit tail, the notes or the files of out/<run> visible now; run.sh scans the collected logs")


def test_secreload2_guards_and_preconditions(target, rig_control, sr_ctx, admin_session):
    ctx = sr_ctx
    strict = os.environ.get("DVT_SECRELOAD2", "").lower() in ("on", "1", "true", "yes")
    require_route(ctx, strict)
    ctx.client.step_up()
    ctx.stepped = t_step = time.monotonic()
    wait_edge_series(ctx, series_strict())
    base = state(ctx)
    _text, file_etag = raw_config(ctx)
    current = sr.etag_value(file_etag)

    # ---- G1 no session: 401 on both methods
    anon = ctx.client.clone()
    for method, r in (("GET", reload_get(ctx, anon, stepped=False)), ("POST", reload_post(ctx, file_etag, anon, stepped=False))):
        assert r.status == 401, "%s config/reload without a session answered %d, want 401" % (method, r.status)
    say(ctx, "G1", "no session: GET and POST are 401")

    # ---- G2 below admin: 403 forbidden before anything else (the audit records the denial)
    denied_users = []
    for role in ("viewer", "operator"):
        client = admin_session(role)
        for method, r in (("GET", reload_get(ctx, client, stepped=False)), ("POST", reload_post(ctx, file_etag, client, stepped=False))):
            body = jbody(r)
            assert r.status == 403 and sr.problem_code(body) == "forbidden", \
                "%s config/reload as %s answered %d %s, want 403 forbidden" % (method, role, r.status, sr.problem_code(body))
        denied_users.append((role, client.identity["user"]))
    say(ctx, "G2", "viewer and operator: GET and POST are 403 forbidden")

    # ---- G3 If-Match: missing 428, stale 412, weak 412 (current_etag in the body); nothing reloads
    for what, header, status, code in (("missing", None, 428, "precondition_required"), ("stale", sr.stale_etag(file_etag), 412, "etag_mismatch"),
                                       ("weak", sr.weak_etag(file_etag), 412, "etag_mismatch")):
        r = reload_post(ctx, header)
        body = jbody(r)
        assert (r.status, sr.problem_code(body)) == (status, code), "%s If-Match: HTTP %d %s, want %d %s" % (
            what, r.status, sr.problem_code(body), status, code)
        assert isinstance(body, dict) and sr.etag_value(body.get("current_etag")) == current, \
            "%s If-Match: current_etag %r differs from the file's ETag" % (what, body.get("current_etag") if isinstance(body, dict) else body)
        time.sleep(1.2)         # the action bucket (2/s) and the denial records
    assert pid_of_edge(ctx) == base.pid, "a refused POST restarted dv-edge-aprsis"
    say(ctx, "G3", "POST without If-Match is 428 precondition_required; a stale and a weak If-Match are 412 etag_mismatch, each with current_etag")

    # ---- G4 the audit: the denials are recorded, no secret byte in it
    audit = audit_tail(ctx)
    me = ctx.client.identity["user"]
    mine = sr.reload_audit(audit, user=me, actions=("config.reload",))
    assert any(r == "denied" for _a, _u, r, _c in mine), "no config.reload record with result denied for %s after the 428 / 412 refusals: %s" % (
        me, [r for _a, _u, r, _c in mine][-6:])
    for role, user in denied_users:
        seen = sr.reload_audit(audit, user=user)
        assert ("config.reload.preview", user, "denied", None) in [(a, u, r, None) for a, u, r, _c in seen] and \
            any(a == "config.reload" and r == "denied" for a, _u, r, _c in seen), \
            "the audit has no denied config.reload(.preview) record for the %s account: %s" % (role, [(a, r) for a, _u, r, _c in seen])
    say(ctx, "G4", "audit: config.reload denied for the missing / stale / weak If-Match (admin) and config.reload + config.reload.preview denied for viewer and operator")

    # ---- G5 step_up_required once the 5 minute window of this session has passed
    left = t_step + STEP_UP_WINDOW_S + 5.0 - time.monotonic()
    if left > 0:
        say(ctx, "G5", "waiting %.0f s for the step-up window of the session to pass" % left)
        while left > 0:
            time.sleep(min(60.0, left))
            left = t_step + STEP_UP_WINDOW_S + 5.0 - time.monotonic()
            if left > 0:
                ctx.client.session()        # keeps the session from idling out; a read is not a step-up
    ctx.stepped = None
    for method, r in (("GET", reload_get(ctx, stepped=False)), ("POST", reload_post(ctx, file_etag, stepped=False))):
        body = jbody(r)
        assert r.status == 403 and sr.problem_code(body) == "step_up_required", \
            "%s config/reload after the step-up window answered %d %s, want 403 step_up_required" % (method, r.status, sr.problem_code(body))
    assert pid_of_edge(ctx) == base.pid, "a POST without step-up restarted dv-edge-aprsis"
    say(ctx, "G5", "after the 5 minute window: GET and POST are 403 step_up_required, nothing restarted")
    assert not ctx.leaks.found, "a secret value appeared in: %s" % ctx.leaks.report()
