# SPDX-License-Identifier: MIT
"""Shared pieces of the cross-mode cases that run against the fake extended
AMBEd (DVT_TRANSCODER_FAKE=1 scripts/run.sh, docs/ambed-fake.md): the fixtures
that talk to its control socket, a scenario runner (one talker, several
listeners of any protocol the rig clients speak) and the FINDING record.

Fixtures (import them into a test module):
  fake_ctl  session: FakeCtl of transcoder-sut, skips with a reason unless the
            profile is dvrouter, DVT_TRANSCODER_FAKE=1 and the fake answers
            `stats`; waits until the worker has been through CAPS
  fake      per case: modes reset before, the Fake handle (counters, log,
            sessions, findings), modes reset again after

A case that injects a fault (Fake.set, or Fake.fault for a fault made some
other way, like a worker KILL) is bracketed with the control channel mark
"ambed-fault-<mode>" from the injection to the end of the case's recovery:
dvcheck then reports the ambed.* errors of the sut <-> transcoder-sut link
inside that window (plus a few seconds) as expected warnings. Cases that
inject nothing are not marked, so any finding in them stays an error.
"""
import json
import os
import re
import socket
import time
from pathlib import Path

import pytest

from dvtest import clients, crossmode, specs
from dvtest.check import AMBED_FAULT_PREFIX
from dvtest.control import RigControl
from dvtest.audio import signals
from dvtest.fake_ctl import FakeCtl, FakeCtlError
from dvtest.net import pace
from dvtest.proto import dextra, usrp

MODULE = "D"
FRAMES = 60
LISTEN_S = 1.0                      # collect() settle after the talk's own tail
FRAME_KEY = {"dstar": "dstar", "dplus": "dstar", "dcs": "dstar", "dmr": "dmr", "m17": "m17", "usrp": "pcm"}
# check ids (case id + point) recorded as FINDINGs instead of asserted: the
# spec is silent or the sut differs from it (see docs/ambed-fake.md, Findings)
RECORD_ONLY = set()
FINDINGS = []                       # appended by Fake.finding, written at session end


def prng(kind, count=FRAMES, seed="crossexact"):
    """Deterministic frames of the kind's codec width."""
    return signals.prng_frames(FRAME_KEY[kind], count, seed="%s/%s" % (seed, kind))


# ---- USRP dynamic client (DVSwitch style: REG, module by DTMF) -----------------

class UsrpPeer:
    """The same surface as the digital clients: link / talk / collect / close."""

    def __init__(self, hub, callsign="N0TEST", module=MODULE):
        self.hub, self.callsign, self.module = hub, callsign, module
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)
        self.seq = 0
        self.inbox = []                      # (monotonic time, datagram)
        self.dest = hub

    def send(self, build, *args):
        self.sock.sendto(build(self.seq, *args), self.hub)
        self.seq += 1

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            try:
                data, _src = self.sock.recvfrom(2048)
                self.inbox.append((time.monotonic(), data))
                continue
            except BlockingIOError:
                pass
            if time.monotonic() >= end:
                return
            time.sleep(0.002)

    def link(self, _ip=None, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.send(usrp.text, usrp.REG)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                self.pump(0.02)
                if any((usrp.text_payload(d) or b"").startswith(usrp.REG_OK) for _t, d in self.inbox):
                    self.send(usrp.text_set_info, self.callsign)
                    self.send(usrp.dtmf, self.module)
                    self.pump(0.3)
                    return True
        return False

    def talk(self, frames, gap=0.020, tail=1.0):
        start = time.monotonic()
        for index, frame in enumerate(frames):
            pace(start, index, gap)
            self.send(usrp.voice, frame)
        self.send(usrp.unkey)
        self.pump(tail)

    def voice(self):
        out = []
        for _t, d in self.inbox:
            if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0 and usrp.parse(d).keyup == 1:
                out.append(d[usrp.HEADER_SIZE:])
        return out

    def collect(self, settle=LISTEN_S):
        self.pump(settle)
        return self.voice()

    def unlink(self):
        self.send(usrp.text, usrp.REG_UNREG)

    def close(self):
        self.sock.close()


def usrp_hub(target):
    """(host, port) of the dynamic USRP hub of the sut, or a skip reason."""
    rules = specs.rule(target, "usrp") or {}
    if not rules.get("dynamic_server"):
        return None, "profile %s has no dynamic USRP hub (SPEC['usrp']['dynamic_server'])" % target.sut_profile
    return (target.xlxd_sut, int(rules.get("dynamic_port", 34001))), None


# ---- fixtures ------------------------------------------------------------------

@pytest.fixture(scope="session")
def fake_ctl(target):
    if target.sut_profile != "dvrouter":
        pytest.skip("cross-mode exactness cases judge DV-Router (DVT_SUT_PROFILE=dvrouter), not %s"
                    % target.sut_profile)
    if os.environ.get("DVT_TRANSCODER_FAKE") != "1":
        pytest.skip("transcoder-sut is not the fake extended AMBEd (run with DVT_TRANSCODER_FAKE=1 scripts/run.sh)")
    if not target.is_given("ambed_sut"):
        pytest.skip("no transcoder-sut address (DVT_AMBED_SUT)")
    ctl = FakeCtl(target.ambed_sut)
    try:
        ctl.stats()
    except FakeCtlError as exc:
        pytest.skip("transcoder-sut does not answer `stats` on its control socket %s:%d: %s"
                    % (target.ambed_sut, ctl.dest[1], exc))
    stats = ctl.wait_for(lambda s: s["counters"].get("tx_caps_reply", 0) >= 1, 90.0, 0.5)
    if stats["counters"].get("tx_caps_reply", 0) < 1:
        pytest.fail("dv-codec-ambed never asked the fake for CAPS within 90 s (counters %r)" % stats["counters"])
    time.sleep(2.0)                                # token, caps query and demand watch
    try:
        yield ctl
    finally:
        out = Path(target.out)
        try:
            out.mkdir(parents=True, exist_ok=True)
            (out / "crossmode_findings.json").write_text(json.dumps(FINDINGS, indent=1) + "\n")
        except OSError:
            pass


def fault_tag(**modes):
    """Mark tag of a fault made by Fake.set(**modes): ambed-fault-open-busy,
    ambed-fault-data-delay-100, ambed-fault-caps-changed-open-silent."""
    parts = ["%s-%s" % (key, re.sub(r"[^A-Za-z0-9._-]", "-", str(modes[key]))) for key in sorted(modes)]
    return AMBED_FAULT_PREFIX + "-".join(parts)


class Fake:
    """One case's view of the fake: counters, log, sessions, findings."""

    def __init__(self, ctl, case, control=None):
        self.ctl, self.case = ctl, case
        self.control = control             # RigControl for the dvcheck marks, or None
        self.dirty = False
        self.marked = None                 # tag of the open mark window

    def fault(self, name):
        """Open the dvcheck window "ambed-fault-<name>" (once per case; the
        fixture closes it after the recovery). No-op without the control
        channel; a failing mark never fails the case."""
        tag = name if name.startswith(AMBED_FAULT_PREFIX) else AMBED_FAULT_PREFIX + name
        if self.marked is not None or self.control is None:
            return
        try:
            self.control.mark("begin", tag)
            self.marked = tag
        except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
            print("mark begin %s failed: %s" % (tag, exc))

    def end_fault(self):
        if self.marked is None:
            return
        tag, self.marked = self.marked, None
        try:
            self.control.mark("end", tag)
        except (RuntimeError, TimeoutError, ValueError, OSError) as exc:
            print("mark end %s failed: %s" % (tag, exc))

    def counters(self):
        return self.ctl.counters()

    def log(self, kinds=None, since_seq=0):
        return self.ctl.log(since_seq=since_seq, kinds=kinds)

    def sessions(self, expect_cs=1, wait=4.0):
        """(sessions, strays) of the log so far; waits up to `wait` s for
        expect_cs CS datagrams (the end of the sessions) first."""
        if expect_cs:
            self.ctl.wait_for(lambda s: s["counters"].get("rx_cs", 0) >= expect_cs, wait)
        return crossmode.sessions(self.log())

    def set(self, **modes):
        self.dirty = True
        self.fault(fault_tag(**modes))
        return self.ctl.set(**modes)

    def recover(self):
        """Back to normal modes with a ready worker: modes reset, then one
        PING (the worker re-probes CAPS at the next PONG when it is not ready)
        and the 1000 ms CAPS wait."""
        self.ctl.reset()
        before = self.ctl.counters().get("rx_ping", 0)
        self.ctl.wait_for(lambda s: s["counters"].get("rx_ping", 0) > before, 7.0, 0.2)
        time.sleep(1.4)
        self.dirty = False

    def expect(self, ok, fid, documented, observed):
        """Assert a point the written spec states; ids in RECORD_ONLY (the sut
        differs from the spec, or the spec is open) are recorded as FINDINGs
        instead of failing the case."""
        if ok:
            return
        if fid in RECORD_ONLY:
            self.finding(fid, documented, observed)
            return
        pytest.fail("%s: documented %s; observed %s" % (fid, documented, observed))

    def finding(self, fid, documented, observed):
        """Record a deviation of the sut from the written spec (or a point the
        spec leaves open) without failing the case."""
        row = {"case": self.case, "id": fid, "documented": documented, "observed": observed,
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        FINDINGS.append(row)
        print("FINDING %s [%s]: documented %s; observed %s (at %s)" % (fid, self.case, documented, observed, row["utc"]))


@pytest.fixture
def fake(fake_ctl, request, target):
    fake_ctl.wait_for(lambda s: s["streams_live"] == 0, 4.0)
    fake_ctl.reset()
    control = RigControl(Path(target.out) / "control") if os.environ.get("DVT_RIG_CONTROL") == "1" else None
    handle = Fake(fake_ctl, request.node.name, control)
    try:
        yield handle
        if handle.dirty:
            handle.recover()
        else:
            fake_ctl.reset()
    finally:
        handle.end_fault()


# ---- scenario ------------------------------------------------------------------

class Result:
    """What the listeners of a scenario received."""

    def __init__(self):
        self.frames = {}            # listener label -> unit list
        self.raw = {}               # listener label -> raw inbox (digital: bytes, usrp: (t, bytes))
        self.sid = None
        self.t_talk = 0.0           # wall clock (time.time) when the talker started


def make_peer(target, kind, module, index):
    if kind == "usrp":
        hub, why = usrp_hub(target)
        if hub is None:
            pytest.skip(why)
        return UsrpPeer(hub, callsign=clients.CALLSIGNS[index], module=module)
    return clients.make(kind, module, index)


def link_peer(peer, kind, ip):
    if kind == "usrp":
        return peer.link()
    return peer.link(ip)


def settle_clients(*peers):
    for peer in peers:
        if isinstance(peer, UsrpPeer):
            peer.pump(0.1)
        else:
            peer.client.drain()
            peer.client.inbox.clear()


def scenario(target, src, dsts, frames, module=MODULE, talk=None, listen=LISTEN_S, labels=None):
    """Link one listener per entry of dsts (kind names, repeats allowed when
    labels give distinct names), link the talker, run talk(talker, kind) (default
    the client library's talk), and collect every listener. Result.frames is
    keyed by the label (the kind when labels is None)."""
    ip = target.xlxd_sut
    labels = labels or list(dsts)
    listeners = []
    talker = None
    res = Result()
    try:
        for label, kind in zip(labels, dsts):
            peer = make_peer(target, kind, module, 1)
            assert link_peer(peer, kind, ip), "%s listener cannot link to %s" % (kind, ip)
            listeners.append((label, kind, peer))
        talker = make_peer(target, src, module, 0)
        assert link_peer(talker, src, ip), "%s talker cannot link to %s" % (src, ip)
        settle_clients(talker, *[p for _l, _k, p in listeners])
        res.t_talk = time.time()
        if talk is not None:
            res.sid = talk(talker, src)
        elif src == "usrp":
            talker.talk(frames)
        else:
            res.sid = clients.talk(talker, src, frames)
        for label, kind, peer in listeners:
            res.frames[label] = peer.collect(listen)
            res.raw[label] = peer.inbox if kind == "usrp" else peer.client.inbox
        return res
    finally:
        for _l, kind, peer in listeners:
            _close(peer, kind)
        if talker is not None:
            _close(talker, src)


def _close(peer, kind):
    try:
        if kind == "usrp":
            peer.unlink()
            peer.close()
        else:
            clients.close(peer)
    except OSError:
        pass


# ---- comparing -----------------------------------------------------------------

def first_diff(got, want):
    """None when equal, else a short description of the first difference."""
    for index, (a, b) in enumerate(zip(got, want)):
        if a != b:
            return "frame %d: got %s, expected %s" % (index, bytes(a).hex(), bytes(b).hex())
    if len(got) != len(want):
        return "got %d frames, expected %d" % (len(got), len(want))
    return None


def dstar_voice(inbox):
    """Parsed voice packets (dicts, last frame excluded) of a DExtra inbox."""
    out = []
    for data in inbox:
        parsed = dextra.parse_voice_raw(data)
        if parsed and not parsed["last"]:
            out.append(parsed)
    return out


def units_sent(src, frames):
    """The unit list the source edge sends to the transcoder: voice units and
    the end unit when the source has one."""
    unit = crossmode.source_units(src, frames)
    return unit.voice + ([unit.end] if unit.end is not None else [])
