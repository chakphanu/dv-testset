# SPDX-License-Identifier: MIT
"""RESTART1 / RESTART1b (DV-Router f34eb5ba and 348f984f, in bc9af2b1, image dv-router:wip-f1f3777): a node restart (`POST /api/v1/admin/system/restart-node`)
stops, joins and REAPS every child, closes the fabric session and checks the Unix listener released, and only then re-executes; no bind retry, no sleep. The
new incarnation therefore comes up at the first attempt with new children only. Document: docs/design/deployment.md "Launcher node restart";
docs/restart1-cases.md. Pure judgements: dvtest/restart1.py. Switch DVT_RESTART1=on. NOT RUN on a rig: written against the sources of bc9af2b1.

  RESTART1-a  one restart: 202, /ready goes down and comes back, the admin session is lost (fresh login), launcher.started_ms advances, node_ready, every child
              has a NEW pid, no process of the old incarnation is alive, the launcher pid is unchanged (exec), the child set is the same, no unparented dv-* process, NO process in state Z (the state sweep of `proc-stat xlxd-sut list`)
  RESTART1-b  three restarts one after the other, each as clean as the first and each ready within the budget (a bind or lock failure would show here, once the
              listener of the old incarnation was not released)
  RESTART1-c  after the restarts a protocol over is relayed in full (module Q, D-STAR): the node is not only "ready" but serves

Needs the web profile (admin API), an admin login with step-up, `DVT_SUT_LAUNCH=native`, and the control channel (`proc-stat xlxd-sut list`). The restarts do not
touch the config file. Do NOT combine with cases that hold state across a node restart (the hub counters restart too).
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import restart1  # noqa: E402

import data_support as ds  # noqa: E402
import test_short_stream_guard as sg  # noqa: E402

pytestmark = [pytest.mark.rig, pytest.mark.web]

LOG = "restart1.txt"
MODULE = "Q"
SERVICE = "xlxd-sut"
STATUS_BUDGET_S = 60.0


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target, rig_control):
    if not restart1.strict_on():
        pytest.skip("DVT_RESTART1=on is not set (the image under test has RESTART1 / RESTART1b, DV-Router f34eb5ba + 348f984f: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native with --profile web")


def _list_text(rig_control):
    """The parsed [(pid, ppid, comm)] list of the live processes of the container (dvtest/control.py)."""
    return rig_control.proc_snapshot(SERVICE)[0]


def fresh_admin(admin_session, old):
    """A new admin session after the restart (dv-svc-admin is a child: its in-memory sessions are gone), same account and authenticator as `old`."""
    user, password = admin_session.credentials("admin")
    client = old.clone()
    client.authenticator = getattr(old, "authenticator", None)
    client.hide(password)
    if client.authenticator is not None:
        client.login_totp(user, password)
    else:
        client.login(user, password)
    client.session()
    return client


def status_of(client):
    resp = client.get(client.path("/status"), expect=None)
    return resp.json() if resp.status == 200 else {}


def one_restart(case, target, admin_session, rig_control, admin):
    """One node restart through the admin API; (new admin client, problems, notes)."""
    before_doc = status_of(admin)
    before = restart1.launcher_view(before_doc)
    callsign = restart1.reflector_callsign(before_doc)
    assert before is not None and callsign, "%s: the admin status has no launcher block or no reflector callsign (%s)" % (case, sorted(before_doc)[:12])
    procs_before = _list_text(rig_control)
    admin.step_up()
    status = None
    t0 = time.monotonic()
    try:
        resp = admin.post(admin.path("/system/restart-node"), {"confirm": callsign}, expect=None)
        status = resp.status
    except Exception as exc:                              # noqa: BLE001  "the accepted response may be dropped because this process stops" (openapi-admin)
        note(target, case, "RECORD", "the reply to restart-node was dropped (%s): the launcher stopped dv-svc-admin first" % type(exc).__name__)
    assert status in (None, 202), "%s: POST system/restart-node answered HTTP %s, want 202 (or a dropped reply)" % (case, status)

    def probe():
        return admin.get(admin.path("/ready"), expect=None).status == 200

    ready = restart1.wait_ready(probe)
    assert ready["ready"], "%s: /ready did not answer 200 within %.0f s of the restart request (%d polls)" % (case, restart1.READY_BUDGET_S, ready["polls"])
    assert ready["down_seen"], "%s: /ready never went down after an accepted restart (%d polls): the node was not restarted" % (case, ready["polls"])
    client, view, last = None, None, None
    end = time.monotonic() + STATUS_BUDGET_S
    while time.monotonic() < end:
        try:
            client = client or fresh_admin(admin_session, admin)
            view = restart1.launcher_view(status_of(client))
        except Exception as exc:                          # noqa: BLE001  the admin service may still be starting
            last = exc
            client = None
            view = None
        if view and view["node_ready"] is True:
            break
        time.sleep(1.0)
    assert view is not None, "%s: no admin status with a launcher block within %.0f s of ready (last error %r)" % (case, STATUS_BUDGET_S, last)
    procs_after, zombies_after = rig_control.proc_snapshot(SERVICE)
    problems = restart1.restart_problems(before, view, procs_before, procs_after, zombies_after)
    note(target, case, "RECORD", "restart-node HTTP %s; ready again after %.1f s (down seen %s, %d polls); started_ms %s -> %s, inc %s -> %s; %d children; launcher pid %s" % (
        status, ready["seconds"], ready["down_seen"], ready["polls"], before["started_ms"], view["started_ms"], before["inc"], view["inc"], len(view["children"]),
        restart1.launcher_pid(procs_after)))
    note(target, case, "RECORD", "wall time of the request to ready %.1f s" % (time.monotonic() - t0))
    return client, problems


@pytest.fixture(scope="module")
def admin(admin_session):
    return admin_session("admin")


def test_restart1_a_one_node_restart_is_clean(rig, target, rig_control, admin_session, admin):
    case = "RESTART1-a"
    _client, problems = one_restart(case, target, admin_session, rig_control, admin)
    note(target, case, "RECORD", "state Z sweep (`zombie=` lines of `proc-stat xlxd-sut list`) is part of the problems above")
    assert not problems, "%s: %s" % (case, "; ".join(problems))


def test_restart1_b_three_restarts_in_a_row(rig, target, rig_control, admin_session, admin):
    case = "RESTART1-b"
    # RESTART1-a restarted the node: the session of the module `admin` fixture died with dv-svc-admin, so log in again first
    client, end, last = None, time.monotonic() + STATUS_BUDGET_S, None
    while client is None:
        try:
            client = fresh_admin(admin_session, admin)
            assert status_of(client), "%s: the fresh admin session got no status" % case
        except Exception as exc:                          # noqa: BLE001  the admin service may still be starting
            last, client = exc, None
            assert time.monotonic() < end, "%s: no admin login within %.0f s of the first restart (last error %r)" % (case, STATUS_BUDGET_S, last)
            time.sleep(1.0)
    for n in (1, 2, 3):
        client, problems = one_restart("%s#%d" % (case, n), target, admin_session, rig_control, client)
        assert not problems, "%s restart %d: %s" % (case, n, "; ".join(problems))


def test_restart1_c_the_node_serves_after_the_restarts(rig, target):
    case = "RESTART1-c"
    ip = rig.xlxd_sut
    talker, listener = sg.dstar(ip, "N0RSTA", MODULE), sg.dstar(ip, "N0RSTL", MODULE)
    deadline = time.monotonic() + 90.0
    while not sg.probe(talker, listener):
        assert time.monotonic() < deadline, "%s: no over was relayed within 90 s of the restarts" % case
        time.sleep(1.0)
    assert sg.talk_long(talker, listener), "%s: a long over on module %s was not heard in full after the restarts" % (case, MODULE)
