# SPDX-License-Identifier: MIT
"""Web unit, FLK15 (DV-Router b8d9db72 in bc9af2b1, image dv-router:wip-f1f3777): an outage of the position policy hides, never leaks, and resyncs
without a restart. Sources: data.md ("The web process owns persisted flags ..."), fabric.md ("link state", `Up` is not application readiness), config.md
(the fabric client). Case table, run command, time and GAPs: docs/flk15-cases.md. Pure helpers: dvtest/flk15.py.

The policy is the hidden-base-callsign set of dv-svc-web (account flag hide_position). Consumers: the room controllers, the data workers, the basemap worker.
Black box through the public API and the operator feed, with positions sent by an M17 packet-mode client (the DP7 senders), the outage made with the
control channel (SIGKILL of dv-svc-web again and again for 15 s: the launcher backs the respawn off 0.5 s doubling to 5 s; SIGKILL of the consumer):

  FLK15-1  web (the authority) is down for about 15 s while hidden accounts keep sending: a sampler reads the anonymous public state every second over
           the whole window (a retained row of a hidden account never shows, before, during or after the outage); positions of hidden accounts sent
           during the outage are dropped (the consumers keep the last set), a control account's position sent during the outage is public after
           web is back (positions come back); no consumer process restarted (proc-stat identity of dv-data-worker and every dv-room is unchanged);
           then, with NO restart of any consumer, an unhide and a hide made after web is back reach them within the documented 1 + 5 + 1 s (plus
           slack, DVT_FLK15_BOUND_S): the unhidden account's next position is public, the newly hidden account's next position is dropped
  FLK15-2  a consumer restarts (dv-data-worker; dv-room of module B) again and again for 8 s while hidden accounts send and while a flag changes (an
           account is hidden DURING the consumer outage): nothing of a hidden account is ever public (cold start = unknown = fail closed, then the
           set of the snapshot); after the restarts stop, a control position is public (positions come back)

NOT covered, a GAP (docs/flk15-cases.md): the consumer's OWN fabric link loss without a process restart (the `withheld after link loss` branch, the 5 s
refresh after an authority-only link loss) needs the router session cut, and the router runs inside the launcher: no control action reaches it. The log
lines are judged on the host by `python3 -m dvtest.flk15 out/<run>`.

DVT_FLK15=on is the switch; without it every case skips with the reason. NOT RUN on a rig: written against the sources of bc9af2b1.
"""
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import flk15, web, webload  # noqa: E402

import data_support as ds  # noqa: E402
import position_support as ps  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
from test_dp15_positions import hub, shape, web_ctx  # noqa: E402,F401

pytestmark = [pytest.mark.rig, pytest.mark.web]

MODULE = p1.MODULE
LOG = "flk15.txt"
SETTING = p1.SETTING
OUTAGE_S = 15.0                    # web: SIGKILL again and again for this long
CONSUMER_OUTAGE_S = 8.0
WEB_BACK_S = 90.0                  # web answers again after the kill loop ends
PUSH_S = 4.0                       # a flag change before the outage reaches the consumers (the DP7 assumption of 3 s, plus one)
ABSENT_S = p1.ABSENT_S
PUBLIC_WAIT_S = ps.PUBLIC_WAIT_S

# one base callsign per role: the controller admits 1 position per 30 s per base callsign
H1, H2 = "N0FHA", "N0FHB"          # hidden before the outage; H1 has a retained public row first, H2 sends during the outage
K1 = "N0FKA"                       # control: sends during the outage, public afterwards
L1 = "N0FLA"                       # hidden before, cleared after web is back
L2 = "N0FLB"                       # public before, hidden after web is back
C_HIDDEN = ("N0FCA", "N0FCB", "N0FCC")     # FLK15-2: hidden before the consumer restarts
C_LATE = "N0FCD"                   # FLK15-2: hidden while the consumer is down
C_K = {"dv-data-worker": "N0FCK", "dv-room": "N0FCM"}      # FLK15-2 control per consumer


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(autouse=True)
def _gate(target):
    if not flk15.strict_on():
        pytest.skip("DVT_FLK15=on is not set (the image under test has FLK15, DV-Router b8d9db72: bc9af2b1 / wip-f1f3777 or later)")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_PROFILE=dvrouter DVT_SUT_LAUNCH=native")


@pytest.fixture(scope="module", autouse=True)
def _restore_operator(web_ctx):
    """The cases kill dv-svc-web: its sessions end. The session-scoped operator is logged in again at the end so that later modules keep working."""
    yield
    try:
        operator_login(web_ctx)
    except Exception as exc:                            # not a case failure
        print("[RECORD] FLK15: the operator could not log in again at the end: %s" % exc)


# ---------------------------------------------------------------- helpers

def operator_login(web_ctx):
    """Log the run's operator in again (the cookies of a killed dv-svc-web are gone; accounts and the TOTP enrolment are in the state directory)."""
    env = web.read_env_file(Path(os.environ.get("DVT_OUT", "out")) / "web" / web.OPERATOR_FILE)
    client = web_ctx.op
    client.cookies.clear()
    client.csrf = None
    client.login(env["DVT_WEB_OPERATOR"], env["DVT_WEB_OPERATOR_PASSWORD"], client.authenticator.next_code() if client.authenticator else None)
    client.session()


def wait_web(web_ctx, budget_s=WEB_BACK_S):
    """Seconds until the public state answers 200 twice in a row (as cases/test_web_api.py Sut.restart_web); pytest.fail when it never does."""
    probe = web_ctx.anon()
    t0, good = time.monotonic(), 0
    while good < 2:
        if time.monotonic() - t0 > budget_s:
            pytest.fail("dv-svc-web did not answer within %.0f s after the kill loop" % budget_s)
        try:
            good = good + 1 if probe.get(ps.PUBLIC_STATE, pace=False).status == 200 else 0
        except web.WebError:
            good = 0
        time.sleep(0.5)
    return time.monotonic() - t0


def patch_hide(web_ctx, call, value):
    """PATCH hide_position of an account; a step-up refusal (401 / 403) is retried once with a fresh TOTP code."""
    op = web_ctx.op
    path = "/api/v1/operator/accounts/%s" % call
    resp = op.request("PATCH", path, json_body={"hide_position": bool(value)}, expect=None)
    if resp.status in (401, 403) and op.authenticator is not None:
        resp = op.request("PATCH", path, json_body={"hide_position": bool(value)}, headers=op.step_up_headers(), expect=None)
    assert resp.status == 200, "PATCH %s hide_position=%s: HTTP %d %s" % (call, value, resp.status, op.scrub(resp.text())[:120])


def procs_of(rig_control, name):
    return webload.parse_procstat_all(rig_control.proc_stat("xlxd-sut", name))


def send(ctx, target, case, call, comment):
    """One M17 packet-mode position of `call`; a failed link is recorded, never a leak."""
    try:
        p1.send_m17(ctx, call, ps.LAT, ps.LON, comment=comment)
        return True
    except AssertionError as exc:
        note(target, case, "RECORD", "send for %s (%s) did not complete: %s" % (call, comment, str(exc)[:120]))
        return False


def state_calls(web_ctx, calls):
    """The rows of the anonymous public state that belong to one of `calls` (base callsign match)."""
    rows = ps.public_rows(web_ctx.anon(), MODULE)
    return [r for c in calls for r in ps.spatial_rows(rows, c)]


def operator_calls(web_ctx, baseline, calls, timeout=1.0):
    """The operator feed rows after `baseline` of one of `calls`."""
    _hits, rows = ps.wait_operator_rows(web_ctx.op, baseline, lambda r: False, timeout=timeout)
    return [r for c in calls for r in ps.spatial_rows(rows, c)]


def hidden_reader(web_ctx, calls):
    """(read, match) for the sampler: the retained position rows of the public state, and the predicate of a row that must never be there."""
    anon = web_ctx.anon()
    return (lambda: ps.public_rows(anon, MODULE)), (lambda row: any(ps.same_base(row.get("callsign", ""), c) for c in calls))


def kill_in_thread(kill, seconds):
    """Runs flk15.kill_loop on a thread; returns (thread, result dict filled at the end)."""
    result = {}

    def run():
        result["landed"], result["t_start"], result["t_end"] = flk15.kill_loop(kill, seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


# ---------------------------------------------------------------- FLK15-1

def test_flk15_1_web_outage_hides_never_leaks_and_resyncs(rig, target, rig_control, hub, web_ctx, shape):
    case = "FLK15-1"
    if not shape.positions:
        pytest.skip("this run has positions = false")
    for call in (H1, H2, K1, L1, L2):
        web_ctx.user(call, may_tx=False)
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    hidden = (H1, H2, L1)
    try:
        # before: H1 is public once, then hidden (its retained row must leave); H2 and L1 hidden; L2 public
        with p1.feeds(web_ctx) as fd:
            send(ctx, target, case, H1, "before")
            send(ctx, target, case, L2, "before")
            first = p1.collect(web_ctx, fd, H1)
            second = p1.collect(web_ctx, fd, L2)
        p1.assert_visible(first, case + " H1 before its flag")
        p1.assert_visible(second, case + " L2 before its flag")
        for call in (H1, H2, L1):
            patch_hide(web_ctx, call, True)
        time.sleep(PUSH_S)
        assert ps.wait_gone(web_ctx.anon(), H1, room=MODULE, item_type="position"), "the retained row of the hidden H1 is still public before the outage"
        before = {name: flk15.identities(procs_of(rig_control, name)) for name in ("dv-data-worker", "dv-room")}
        baseline = ps.operator_baseline(web_ctx.op)

        read, match = hidden_reader(web_ctx, hidden)
        with flk15.Sampler(read, match) as sampler:
            thread, killed = kill_in_thread(lambda: rig_control.kill_proc("xlxd-sut", "dv-svc-web", "KILL"), OUTAGE_S)
            time.sleep(1.5)                                      # the first kill landed; the sends below overlap the outage
            send(ctx, target, case, H2, "during")
            send(ctx, target, case, K1, "during")
            thread.join(OUTAGE_S + 30.0)
            back_s = wait_web(web_ctx)
            time.sleep(2.0)                                      # a few more samples of the returned service
        window = flk15.outage_window(sampler.samples)
        note(target, case, "RECORD", "kills landed %s, web back %.1f s after the loop; %s" % (killed.get("landed"), back_s, flk15.describe_samples(sampler.samples)))
        assert window is not None, "the kill loop never made the public state unreachable (%s): the outage was not produced" % flk15.describe_samples(sampler.samples)
        leaked = flk15.leaks(sampler.samples)
        assert not leaked, "a hidden account's row was PUBLIC at some instant of the outage: %s" % [
            (round(s.t - window[0], 1), [r.get("callsign") for r in s.rows]) for s in leaked[:3]]

        operator_login(web_ctx)
        row, _rows = ps.wait_public_row(web_ctx.anon(), K1, room=MODULE, item_type="position", timeout=PUBLIC_WAIT_S)
        assert row is not None, "the control position sent during the outage never became public after web returned (positions must come back)"
        ps.check_public_row(row)
        time.sleep(ABSENT_S)
        assert not state_calls(web_ctx, (H1, H2, L1)), "a hidden account is in the public state after the outage: %s" % [
            r.get("callsign") for r in state_calls(web_ctx, (H1, H2, L1))]
        assert not operator_calls(web_ctx, baseline, (H1, H2, L1)), "a hidden account's position reached the operator feed during or after the outage"
        for name, ids in before.items():
            now = flk15.identities(procs_of(rig_control, name))
            assert now == ids, "%s restarted during the web outage (before %s, after %s): the resync must not need a consumer restart" % (name, sorted(ids), sorted(now))

        # the flags change AFTER web is back; the consumers were never restarted
        patch_hide(web_ctx, L1, False)
        patch_hide(web_ctx, L2, True)
        wait_s = flk15.resync_wait_s()
        time.sleep(wait_s)
        baseline = ps.operator_baseline(web_ctx.op)
        with p1.feeds(web_ctx) as fd:
            send(ctx, target, case, L1, "back")
            send(ctx, target, case, L2, "after")
            back = p1.collect(web_ctx, fd, L1)
            time.sleep(ABSENT_S)
            on_sse = ps.sse_rows(fd.pub, L2, fd.t0)
        note(target, case, "RECORD", "after the resync wait of %.0f s: L1 (unhidden) sse %s state %s op %s" % (wait_s, back.sse is not None, back.state is not None, back.op is not None))
        p1.assert_visible(back, case + " L1 after its flag was cleared (%.0f s after the PATCH, no consumer restart)" % wait_s)
        assert not on_sse, "L2 was hidden %.0f s earlier and its next position still reached the public SSE" % wait_s
        assert not state_calls(web_ctx, (L2,)), "L2 (hidden after the outage) is in the public state"
        assert not operator_calls(web_ctx, baseline, (L2,)), "L2 (hidden after the outage) reached the operator feed: a consumer kept the stale set"
    finally:
        for call in (H1, H2, L1, L2):
            try:
                patch_hide(web_ctx, call, False)
            except Exception as exc:                            # the next case does not use these accounts
                note(target, case, "RECORD", "could not clear hide_position of %s at the end: %s" % (call, str(exc)[:100]))


# ---------------------------------------------------------------- FLK15-2

@pytest.mark.parametrize("consumer,rooms", [("dv-data-worker", None), ("dv-room", "B")], ids=["data-worker", "room-B"])
def test_flk15_2_consumer_restart_fails_closed(rig, target, rig_control, hub, web_ctx, shape, consumer, rooms):
    case = "FLK15-2[%s]" % consumer
    if not shape.positions:
        pytest.skip("this run has positions = false")
    calls = list(C_HIDDEN) + [C_LATE, C_K[consumer]]
    for call in calls:
        web_ctx.user(call, may_tx=False)
    ctx = types.SimpleNamespace(rig=rig, rf=None)
    hidden_all = C_HIDDEN + (C_LATE,)
    try:
        for call in C_HIDDEN:
            patch_hide(web_ctx, call, True)
        time.sleep(PUSH_S)
        web_before = flk15.identities(procs_of(rig_control, "dv-svc-web"))
        consumer_before = flk15.identities(procs_of(rig_control, consumer))
        baseline = ps.operator_baseline(web_ctx.op)
        read, match = hidden_reader(web_ctx, hidden_all)
        with flk15.Sampler(read, match) as sampler:
            thread, killed = kill_in_thread(lambda: rig_control.kill_proc("xlxd-sut", consumer, "KILL", **({"rooms": rooms} if rooms else {})), CONSUMER_OUTAGE_S)
            time.sleep(1.0)
            patch_hide(web_ctx, C_LATE, True)                    # a change made while the consumer is down: the next incarnation must know it
            for call in hidden_all:
                send(ctx, target, case, call, "during")
            thread.join(CONSUMER_OUTAGE_S + 30.0)
            time.sleep(flk15.resync_wait_s())
        note(target, case, "RECORD", "kills landed %s; %s" % (killed.get("landed"), flk15.describe_samples(sampler.samples)))
        assert killed.get("landed", 0) >= 1, "no kill of %s landed: the consumer was never restarted, the case proved nothing" % consumer
        assert not flk15.leaks(sampler.samples), "a hidden account's row was public while %s restarted: %s" % (
            consumer, [(s.t, [r.get("callsign") for r in s.rows]) for s in flk15.leaks(sampler.samples)[:3]])
        assert not state_calls(web_ctx, hidden_all), "a hidden account is in the public state after %s restarted" % consumer
        assert not operator_calls(web_ctx, baseline, hidden_all), "a hidden account's position reached the operator feed while %s restarted (cold start must fail closed)" % consumer
        assert flk15.identities(procs_of(rig_control, "dv-svc-web")) == web_before, "dv-svc-web restarted in a consumer case"
        assert flk15.identities(procs_of(rig_control, consumer)) != consumer_before, "%s kept its pid and start time: it was not restarted" % consumer
        with p1.feeds(web_ctx) as fd:                            # positions come back for an account that is not hidden
            send(ctx, target, case, C_K[consumer], "after")
            got = p1.collect(web_ctx, fd, C_K[consumer])
        p1.assert_visible(got, case + " control position after the restarts stopped")
    finally:
        for call in hidden_all:
            try:
                patch_hide(web_ctx, call, False)
            except Exception as exc:
                note(target, case, "RECORD", "could not clear hide_position of %s at the end: %s" % (call, str(exc)[:100]))
