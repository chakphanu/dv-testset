# SPDX-License-Identifier: MIT
"""K9C: DV-Router enforces the `direction` of an endpoint row (K9c, 39337fb5; images from wip-ae11b8f, docs/k9c-cases.md). Behind DVT_K9C=on and
DVT_SUT_GEN=k9, on ONE edge: USRP. Two static rows (rig/config/dv-router/k9c-usrp.toml; the tester is their far end), each alone on its module: k9c-from
(from_room) on K and k9c-to (to_room) on M, and in each room one client of the dynamic hub (DVT_USRP_DYNAMIC=1, class 0) as the third party. The image
allows one USRP row per module, so the two directions live in two rooms.

  a  k9c-from keys up twice: every over is refused, `dv_edge_drops_total{reason="deny_tx_acl"}` moves exactly once per over, the third party of room K hears
     nothing, the refused talker is sent no voice (no busy tone); it still hears the room (the third party talks)
  b  k9c-to keys up: the third party of room M hears it, no TxAcl; the row hears nothing while the third party talks
  c  k9c-from is switched to both in the middle of its over (rendered-config edit + `hup xlxd-sut`): same processes, the over is not heard before the
     switch and is heard from the switch on; an over of its own after it is heard in full
  d  k9c-to is switched to from_room in the middle of its over: same processes, the over is heard until the switch and not after it; the next RTS of
     the row is refused (TxAcl once per over) and it hears the room now
  e  the file and the rows are back to their start, no process was replaced in the whole run

Needs (docs/k9c-cases.md): DVT_K9C=on DVT_SUT_GEN=k9 DVT_SUT_LAUNCH=native DVT_SUT_CONFIG_RW=1 DVT_USRP_DYNAMIC=1,
DVT_SUT_CONFIG_EXTRA with rig/config/dv-router/k9c-usrp.toml. Every number is recorded to out/<run>/k9c.txt as [RECORD] or [FINDING].
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from dvtest import k9c, specs, sut_config
from dvtest.audio import signals
from dvtest.control import RigControl
from dvtest.net import pace
from dvtest.proto import usrp

from echo_support import UsrpDyn

pytestmark = pytest.mark.rig

FRAMES = 75                         # 1.5 s
LONG_FRAMES = 500                   # 10 s: the switch lands about 2 s in
SWITCH_AT = 2.0
APPLY_S = 4.0                       # time allowed for the edge to apply a reload after the SIGHUP
HEARD_MIN = 0.8                     # share of the frames of an over that a listener must hear
TELEMETRY_S = 12.0                  # one hub telemetry period plus margin (cases/test_echo.py waits 11 s)


# ---------------------------------------------------------------- notes

def note(target, case, tag, text):
    line = "[%s] %s: %s" % (tag, case, text)
    print(line)
    try:
        with open(Path(target.out) / "k9c.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- the tester's end of a static row

class Peer:
    """The far end of one static USRP row: a UDP socket on the row's `peer` port, sending to the row's bind port. Arrival times are time.monotonic()."""

    def __init__(self, ip, row):
        bind_port, port, _, _ = k9c.ROWS[row]
        self.row, self.dest = row, (ip, bind_port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.sock.setblocking(False)
        self.inbox = []

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            try:
                data, _ = self.sock.recvfrom(2048)
                self.inbox.append((time.monotonic(), data))
                continue
            except BlockingIOError:
                pass
            if time.monotonic() >= end:
                return
            time.sleep(0.002)

    def talk(self, frames, others=()):
        """One over of `frames` voice packets at 20 ms, then the unkey; `others` are drained meanwhile. Returns (first, last) send time."""
        pcm = signals.frames(signals.tone(700, frames * 20))
        start = time.monotonic()
        for i, frame in enumerate(pcm):
            pace(start, i, 0.020)
            self.sock.sendto(usrp.voice(i, frame), self.dest)
            self.pump(0)
            for other in others:
                other.pump(0)
        self.sock.sendto(usrp.unkey(len(pcm)), self.dest)
        return start, time.monotonic()

    def voice_times(self):
        return [t for t, d in self.inbox if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0 and usrp.parse(d).keyup == 1]

    def close(self):
        self.sock.close()


def pump_all(seconds, *clients):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for c in clients:
            c.pump(0.01)


# ---------------------------------------------------------------- the rig

class Rig:
    def __init__(self, target, control, path, hub):
        self.target, self.control, self.path, self.hub = target, control, path, hub
        self.ip = target.xlxd_sut
        self.original = path.read_text(encoding="utf-8")
        self.pids = None
        self.results = []

    def deny(self):
        return k9c.deny_text(self.control.metrics("xlxd-sut"))

    def settle(self, base, expect, limit=60.0):
        """The deny series after `expect` more TxAcl (waited for up to `limit` s), plus one more telemetry period so that an extra count would show.
        Returns (series, moved)."""
        end = time.monotonic() + limit
        series = self.deny()
        while expect and k9c.tx_acl(series) - k9c.tx_acl(base) < expect and time.monotonic() < end:
            time.sleep(2.0)
            series = self.deny()
        time.sleep(TELEMETRY_S)
        series = self.deny()
        return series, k9c.delta(base, series)

    def procs(self):
        return sorted((pid, comm) for pid, _ppid, comm in self.control.proc_list("xlxd-sut"))

    def reload(self, text):
        """Write the rendered config in place and signal the launcher; returns (t_write, t_hup) as time.monotonic() values."""
        t_write = time.monotonic()
        self.path.write_text(text, encoding="utf-8")
        t_hup = self.control.hup()
        return t_write, t_hup - (time.time() - time.monotonic())

    def restore(self):
        if self.path.read_text(encoding="utf-8") != self.original:
            self.path.write_text(self.original, encoding="utf-8")
            self.control.hup()
            time.sleep(APPLY_S)


@pytest.fixture(scope="module")
def rig_k9c(rig, target):
    if os.environ.get("DVT_K9C") != "on":
        pytest.skip("K9C: set DVT_K9C=on for an image with K9c (wip-ae11b8f and later; docs/k9c-cases.md)")
    if sut_config.gen_value() != sut_config.K9:
        pytest.skip("DVT_SUT_GEN must be k9")
    if target.sut_profile != "dvrouter" or os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("K9C needs DVT_SUT_PROFILE=dvrouter and DVT_SUT_LAUNCH=native")
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1): hup and metrics of xlxd-sut are not reachable")
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "rw" / "dv-router.toml"
    if not path.is_file():
        pytest.skip("the config is not replaceable: run with DVT_SUT_CONFIG_RW=1 (the case edits the run's own rendered copy and puts it back)")
    text = path.read_text(encoding="utf-8")
    for row, (_, _, start, _) in k9c.ROWS.items():
        have = k9c.row_direction(text, row)
        if have is None:
            pytest.skip("no USRP row %s in the rendered config: add rig/config/dv-router/k9c-usrp.toml to DVT_SUT_CONFIG_EXTRA" % row)
        assert have == start, "row %s starts as %s, want %s" % (row, have, start)
    rules = specs.rule(target, "usrp") or {}
    if not rules.get("dynamic_server"):
        pytest.skip("no dynamic USRP hub in the profile (DVT_USRP_DYNAMIC=1)")
    hub = (target.xlxd_sut, int(rules.get("dynamic_port", 34101)))
    ctx = Rig(target, RigControl(Path(target.out) / "control"), path, hub)
    ctx.pids = ctx.procs()
    note(target, "setup", "RECORD", "processes at the start: %s; hub %s:%d" % (", ".join("%d %s" % p for p in ctx.pids), hub[0], hub[1]))
    yield ctx
    try:
        ctx.restore()
    except Exception as exc:                                  # noqa: BLE001 - never hide the case result
        print("k9c: restore failed: %s" % exc)


def join(ctx, callsign, module):
    third = UsrpDyn(ctx.hub, callsign)
    end = time.monotonic() + 15
    while not third.register():
        if time.monotonic() > end:
            third.close()
            pytest.skip("the dynamic USRP hub does not answer REG:DVSWITCH at %s:%d" % ctx.hub)
    third.join(module)
    return third


@pytest.fixture()
def scene(rig_k9c):
    """(ctx, from_room row, to_room row, third party of room K, third party of room M); everything closed at the end."""
    ctx = rig_k9c
    f, t = Peer(ctx.ip, "k9c-from"), Peer(ctx.ip, "k9c-to")
    ok = om = None
    try:
        ok = join(ctx, "N0OBK", k9c.ROWS["k9c-from"][3])
        om = join(ctx, "N0OBM", k9c.ROWS["k9c-to"][3])
        yield ctx, f, t, ok, om
    finally:
        for c in (f, t, ok, om):
            if c is not None:
                c.close()


def finish(ctx, case, problems):
    ctx.results.append((case, problems))
    for p in problems:
        note(ctx.target, case, "FINDING", p)
    assert not problems, "%s: %s" % (case, "; ".join(problems))


def heard_problems(who, heard, sent, want_min=False):
    """want_min False: nothing may have been heard; True: at least HEARD_MIN of the frames."""
    if not want_min:
        return ["%s heard %d voice packets, want 0" % (who, heard)] if heard else []
    return ["%s heard %d of %d voice packets (%.0f percent), want at least %.0f percent" % (who, heard, sent, 100.0 * heard / sent, 100 * HEARD_MIN)] \
        if heard < sent * HEARD_MIN else []


def deny_note(ctx, case, moved, got, what):
    note(ctx.target, case, "RECORD", "deny counters moved: %s (TxAcl delta %g%s)" % (k9c.describe(moved), got, what))


# ---------------------------------------------------------------- a: from_room

def test_k9c_a_from_room_is_refused_with_txacl_and_still_hears(scene):
    ctx, f, t, ok, om = scene
    problems = []
    base = ctx.deny()
    note(ctx.target, "a", "RECORD", "deny series before (%d): %s" % (len(base), sorted((n, dict(l)) for (n, l) in base)[:12]))
    for n in (1, 2):
        f.talk(FRAMES, others=(ok,))
        pump_all(1.0, f, ok)
        note(ctx.target, "a", "RECORD", "over %d of the from_room row sent: third party heard %d voice, the row itself got %d datagrams (%d voice)" % (
            n, len(ok.voice_times()), len(f.inbox), len(f.voice_times())))
    problems += heard_problems("the third party of the refused from_room overs", len(ok.voice_times()), 2 * FRAMES)
    problems += heard_problems("the refused from_room row (no busy tone, no voice)", len(f.voice_times()), 2 * FRAMES)
    ok.talk(FRAMES, drain_with=(f,))
    pump_all(1.0, f, ok)
    heard_f = len(f.voice_times())
    note(ctx.target, "a", "RECORD", "third party talked %d frames: the from_room row heard %d" % (FRAMES, heard_f))
    problems += heard_problems("the from_room row (it must still hear the room)", heard_f, FRAMES, want_min=True)
    series, moved = ctx.settle(base, 2)
    got = k9c.tx_acl(series) - k9c.tx_acl(base)
    deny_note(ctx, "a", moved, got, " after two refused overs and one room over")
    if got != 2:
        problems.append("dv_edge_drops_total{reason=deny_tx_acl} moved by %g for two overs of the from_room row, want exactly 2 (once per over)" % got)
    other = {k: v for k, v in moved.items() if not (k[0] == k9c.DROPS_FAMILY and dict(k[1]).get("reason") == k9c.DENY_REASON)}
    if other:
        problems.append("other deny counters moved: %s" % k9c.describe(other))
    finish(ctx, "a", problems)


# ---------------------------------------------------------------- b: to_room

def test_k9c_b_to_room_is_heard_and_hears_nothing(scene):
    ctx, f, t, ok, om = scene
    problems = []
    base = ctx.deny()
    t.talk(FRAMES, others=(om,))
    pump_all(1.0, t, om)
    heard_om, own = len(om.voice_times()), len(t.voice_times())
    note(ctx.target, "b", "RECORD", "to_room row sent %d frames: third party heard %d, the row itself got %d datagrams (%d voice)" % (
        FRAMES, heard_om, len(t.inbox), own))
    problems += heard_problems("the third party (the to_room over must reach the room)", heard_om, FRAMES, want_min=True)
    problems += heard_problems("the to_room row of its own over", own, FRAMES)
    t.inbox.clear()
    om.talk(FRAMES, drain_with=(t,))
    pump_all(1.0, t, om)
    note(ctx.target, "b", "RECORD", "third party talked %d frames: the to_room row got %d datagrams of any kind, %d voice" % (
        FRAMES, len(t.inbox), len(t.voice_times())))
    problems += heard_problems("the to_room row while the room talks (no listener leg)", len(t.voice_times()), FRAMES)
    series, moved = ctx.settle(base, 0)
    deny_note(ctx, "b", moved, k9c.tx_acl(series) - k9c.tx_acl(base), " after the to_room over and the room over")
    if moved:
        problems.append("deny counters moved although nothing was refused: %s" % k9c.describe(moved))
    finish(ctx, "b", problems)


# ---------------------------------------------------------------- c, d: direction switch in the middle of an over

def switch_during(ctx, talker, others, text, frames=LONG_FRAMES):
    """`talker` sends one over of `frames` packets; SWITCH_AT s after its first one the config `text` is written and the launcher is signalled.
    Returns (t_write, t_hup, over start) as time.monotonic()."""
    stamp = {}

    def later():
        time.sleep(SWITCH_AT)
        stamp["t_write"], stamp["t_hup"] = ctx.reload(text)

    th = threading.Thread(target=later, daemon=True)
    th.start()
    start, _ = talker.talk(frames, others=others)
    th.join(30.0)
    assert "t_hup" in stamp, "the reload thread did not finish"
    return stamp["t_write"], stamp["t_hup"], start


def procs_problems(ctx, what):
    now = ctx.procs()
    if now != ctx.pids:
        return ["%s: the process set changed: %s -> %s (a restart; the change must apply without one)" % (
            what, ", ".join("%d %s" % p for p in ctx.pids), ", ".join("%d %s" % p for p in now))]
    return []


def switch_note(ctx, case, what, t_write, t_hup, start, facts):
    note(ctx.target, case, "RECORD", "%s: the file was written %.2f s into the over, SIGHUP %.2f s after the write; the third party heard %s (first packet %s s "
         "and last %s s after the write; %d of the %d packets sent)" % (
             what, t_write - start, t_hup - t_write, {k: facts[k] for k in ("total", "before", "after")}, facts["first_after"], facts["last"],
             facts["total"], LONG_FRAMES))


def test_k9c_c_from_room_to_both_in_the_middle_of_an_over(scene):
    ctx, f, t, ok, om = scene
    problems = []
    base = ctx.deny()
    try:
        text = k9c.set_direction(ctx.original, "k9c-from", "both")
        t_write, t_hup, start = switch_during(ctx, f, (ok,), text)
        pump_all(APPLY_S, f, ok)
        wait = (t_hup - t_write) + APPLY_S
        bad, facts = k9c.judge_switch(ok.voice_times(), t_write, wait, expect_after=True)
        problems += ["third party: " + b for b in bad]
        switch_note(ctx, "c", "from_room -> both mid-over", t_write, t_hup, start, facts)
        problems += procs_problems(ctx, "c after the reload")
        ok.inbox.clear()
        f.talk(FRAMES, others=(ok,))
        pump_all(1.0, f, ok)
        problems += heard_problems("the third party (an over of the row after the switch to both)", len(ok.voice_times()), FRAMES, want_min=True)
        series, moved = ctx.settle(base, 1)
        got = k9c.tx_acl(series) - k9c.tx_acl(base)
        deny_note(ctx, "c", moved, got, ": the refusal of the RTS before the switch")
        if got != 1:
            problems.append("dv_edge_drops_total{reason=deny_tx_acl} moved by %g, want exactly 1 (the RTS refused before the switch; none after it)" % got)
    finally:
        ctx.restore()
    problems += procs_problems(ctx, "c after the restoring reload")
    finish(ctx, "c", problems)


def test_k9c_d_to_room_to_from_room_in_the_middle_of_an_over(scene):
    ctx, f, t, ok, om = scene
    problems = []
    base = ctx.deny()
    try:
        text = k9c.set_direction(ctx.original, "k9c-to", "from_room")
        t_write, t_hup, start = switch_during(ctx, t, (om,), text)
        pump_all(APPLY_S, t, om)
        wait = (t_hup - t_write) + APPLY_S
        bad, facts = k9c.judge_switch(om.voice_times(), t_write, wait, expect_after=False)
        problems += ["third party: " + b for b in bad]
        switch_note(ctx, "d", "to_room -> from_room mid-over", t_write, t_hup, start, facts)
        problems += procs_problems(ctx, "d after the reload")
        om.inbox.clear()
        t.inbox.clear()
        om.talk(FRAMES, drain_with=(t,))
        pump_all(1.0, t, om)
        problems += heard_problems("the switched row (from_room now: it hears the room)", len(t.voice_times()), FRAMES, want_min=True)
        om.inbox.clear()
        t.talk(FRAMES, others=(om,))
        pump_all(1.0, t, om)
        problems += heard_problems("the third party (a new over of the row after the switch to from_room)", len(om.voice_times()), FRAMES)
        series, moved = ctx.settle(base, 2)
        got = k9c.tx_acl(series) - k9c.tx_acl(base)
        deny_note(ctx, "d", moved, got, ": the rest of the first over after the switch and the second over")
        if got != 2:
            problems.append("dv_edge_drops_total{reason=deny_tx_acl} moved by %g, want exactly 2 (one per over refused after the switch)" % got)
    finally:
        ctx.restore()
    problems += procs_problems(ctx, "d after the restoring reload")
    finish(ctx, "d", problems)


def test_k9c_e_every_row_is_back_to_its_start_direction(rig_k9c):
    """The last case: the file and the rows are as they started and no process was replaced in the whole run."""
    ctx = rig_k9c
    ctx.restore()
    text = ctx.path.read_text(encoding="utf-8")
    problems = ["row %s is %s, started %s" % (row, k9c.row_direction(text, row), start)
                for row, (_, _, start, _) in k9c.ROWS.items() if k9c.row_direction(text, row) != start]
    problems += procs_problems(ctx, "e at the end")
    finish(ctx, "e", problems)
