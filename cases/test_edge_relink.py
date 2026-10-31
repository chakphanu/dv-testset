# SPDX-License-Identifier: MIT
"""P19 edge relink: the DV-Router launcher supervises one edge process per
protocol and restarts a dead one. With the SUT running and the peer or relay
linked, the case SIGKILLs the edge inside the SUT container (control channel
"kill-proc xlxd-sut <edge> KILL", scripts/lib/killproc.sh), then measures on
the tester side the seconds from the kill (t_kill, taken inside the container)
to the first fully re-established link as the peer sees it. Each edge runs
RUNS (3) times; every run prints and appends one JSON line to
$DVT_OUT/edge_relink.jsonl (edge, signal, pids, seconds of each milestone after
the kill, relay_s, limit_s). The bound is the spec's, not a constant here:
SPEC["relink"]["P-RLK-NN"]["max_s"] of dvtest/specs/dvrouter.py (10 s).

Fully re-established, per edge:
- P-RLK-01 XLX (dv-edge-xlx). The tester is the interlink peer XLX996 of
  xlxd-sut (rig profile interlink: DVT_INTERLINK=on; XlxPeer; the SUT's config
  must list XLX996 with the DVT_INTERLINK_MODULES). Milestones: connect = the
  SUT's new L, ack = our A to it, keepalive = the SUT's first 9 B keepalive
  after that, relay = a probe stream from the peer arrives at a D-STAR client
  of the SUT on the first interlink module.
- P-RLK-02 NXDN (dv-edge-nxdn). The tester is the NXDN reflector of the SUT's
  probe row (profile nxdn: DVT_NXDN=on, DVT_NXDN_PROBE_*; nxdn.FakeReflector, it
  echoes polls). Milestones: poll = the SUT's first NXDNP after the kill (echoed),
  relay = a DMR probe stream on the probe module arrives as NXDND voice.
- P-RLK-03 P25 (dv-edge-p25). The peer is the p25-sut P25Reflector (profile
  p25: DVT_P25=on, DVT_P25_TG, DVT_P25_MODULE; IMBE transcoding needed). The
  tester cannot see that link, so the only milestone is relay = a D-STAR probe
  on the P25 module arrives at a P25 client of p25-sut.

The relay probe is a stream of PROBE_FRAMES (>= 50, so that the short-stream
guard is not hit) frames, repeated back to back once the handshake milestone is
there (XLX, NXDN) or at once (P25), so relay_s is an upper bound within one
probe period (about 2 s). The quiet-restart cases (SIGUSR1 to the same edge
process; it saves its sessions, exits quietly and the launcher respawns it)
record the same numbers and assert nothing. The launcher dv-router is never
signalled (no handler: a stray signal kills the SUT node, exit 138).

Timeouts: every wait is bounded: the baseline before a kill (BASELINE_S), the
observation after it (the spec limit + OBSERVE_EXTRA_S) and the restore in the
finally block (RESTORE_S, never raises), so a dead edge cannot hang a run; the
finally block waits for the SUT to relay again and leaves the peer linked.

Every case restores the SUT in its finally block, whatever happened: it
probes the DExtra port (the only state the tester can see; the exit code of the
container is not visible from here), waits for the edge to relay again within
RESTORE_S and, when the SUT does not answer or still does not relay, restarts
it through the control channel (rig_control("restart", "xlxd-sut")) and waits
for the peer/probe path. If that fails the case fails LOUDLY ("SUT RESTORE
FAILED"), so a dead SUT cannot poison later cases. A baseline that cannot be
established fails with the SUT state, not a generic message. Each JSON line has
sut_reachable_after, sut_exited (inferred: no DExtra answer after the run) and
sut_exit_code (always null: not obtainable from the tester); a restore appends a
line with event "restore".

Skipped unless DVT_SUT_PROFILE=dvrouter and the control channel exists.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest
from p25_common import encode, imbe_served, p25_client

from dvtest import clients, relink, specs
from dvtest.audio import signals
from dvtest.proto import ambed, dextra, dmr, nxdn, p25, xlx

pytestmark = [pytest.mark.rig]

RUNS = 3
PROBE_FRAMES = 52               # D-STAR / DMR frames of a probe (>= 50: no short-stream guard)
BASELINE_S = 60.0               # hard cap: link and relay working before a kill
OBSERVE_EXTRA_S = 20.0          # observation after a kill = spec limit + this
RESTORE_S = 60.0                # hard cap of the restore in finally
SETTLE_S = float(os.environ.get("DVT_RELINK_SETTLE_S") or 12.0)   # between kills of one edge: the launcher backoff resets only after a child ran >= 10 s
SUT_VERSION = (2, 6, 1)
SUT_CALLSIGN = "XLX999"


# ---------------------------------------------------------------- judging and records

def limit_s(rig, nn):
    """max_s of the profile's relink entry nn; skip while there is none."""
    entry = (specs.rule(rig, "relink") or {}).get(nn) or {}
    if "max_s" not in entry:
        pytest.skip("spec pending: no relink %s max_s entry in dvtest/specs/%s.py" % (nn, rig.sut_profile))
    return float(entry["max_s"])


def record(row):
    """Print the row and append it as one JSON line to $DVT_OUT/edge_relink.jsonl."""
    line = json.dumps(row, sort_keys=True)
    print(line)
    out = Path(os.environ.get("DVT_OUT", "out"))
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "edge_relink.jsonl", "a") as fh:
        fh.write(line + "\n")


def sut_reachable(ip):
    """Does the SUT answer a DExtra link (module A)? The only container state
    the tester can see."""
    client = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module="A")
    try:
        return bool(client.link(ip, timeout=1.0))
    except OSError:
        return False
    finally:
        client.close()


def restore_sut(edge, rig_control):
    """Unconditional end of every case: leave the SUT running and relaying.
    Restarts the container through the control channel when it does not
    answer or does not relay again; raises (loudly) when that fails."""
    ip = edge.ip
    reachable = sut_reachable(ip)
    action = relink.restore_action(edge.relayed, reachable, False)
    if action == "wait":
        edge.restore(edge.last_kill)
        reachable = sut_reachable(ip)
        action = relink.restore_action(edge.relayed, reachable, True)
    if action == "none":
        return
    print("%s: %s; restarting xlxd-sut" % (edge.key, relink.sut_state_text(reachable)))
    error = None
    try:
        rig_control("restart", "xlxd-sut", timeout=180)
        restored = edge.prepare(BASELINE_S)
        edge.relayed = restored
    except Exception as exc:                            # noqa: BLE001 - reported below
        restored, error = False, exc
    record({"case": os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0].split("::")[-1], "edge": edge.key,
            "event": "restore", "restarted": True, "ok": bool(restored), "error": repr(error) if error else None})
    if not restored:
        pytest.fail("SUT RESTORE FAILED after %s: xlxd-sut restarted but the %s path does not work again (%s%s); "
                    "later cases will fail" % (edge.key, edge.key, relink.sut_state_text(sut_reachable(ip)),
                                               ", error %r" % error if error else ""), pytrace=False)


@pytest.fixture(scope="module")
def dvr(rig):
    if rig.sut_profile != "dvrouter":
        pytest.skip("edge relink judges the DV-Router launcher/edge supervisor: needs DVT_SUT_PROFILE=dvrouter "
                    "(profile is %s)" % rig.sut_profile)
    return rig


# ---------------------------------------------------------------- helpers

class Keeper:
    """Background drain of idle clients (their socket answers keepalives) and
    an optional periodic ping. Hold .lock while a case talks on the same
    clients."""

    def __init__(self, items, ping=None, period=3.0):
        self.items, self.ping, self.period = list(items), ping, period
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        last = time.monotonic()
        while not self.stop.wait(0.02):
            with self.lock:
                try:
                    for item in self.items:
                        item.client.drain()
                    if self.ping and time.monotonic() - last >= self.period:
                        last = time.monotonic()
                        for item in self.items:
                            self.ping(item)
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        self.thread.join(2)


def first_after(inbox, times, since, pred):
    """Arrival time (monotonic) of the first packet at or after since that satisfies pred."""
    for index in range(min(len(inbox), len(times))):
        if times[index] >= since and pred(inbox[index]):
            return times[index]
    return None


def wait_for(fn, seconds):
    """fn() until it returns something truthy or seconds pass (bounded)."""
    deadline = time.monotonic() + seconds
    while True:
        got = fn()
        if got or time.monotonic() >= deadline:
            return got
        time.sleep(0.05)


def stop_client(item):
    try:
        if item.dest:
            item.unlink()
    except OSError:
        pass
    finally:
        item.close()


class Edge:
    """One edge under test. Subclasses give open/close of the tester side,
    the SUT-facing clients (rebuilt by prepare and, after a quiet restart, at
    every probe), send_probe/heard and step."""

    key = process = nn = ""

    def __init__(self, rig):
        self.rig = rig
        self.stones = {}
        self.sut_side = []              # SUT-facing clients (and their keepers) to rebuild
        self.relayed = True
        self.last_kill = 0.0
        self.last_error = None

    # -- to be provided
    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def ensure_peer(self):
        """The peer side is linked (before a kill)."""

    def build_clients(self):
        raise NotImplementedError

    def send_probe(self):
        raise NotImplementedError

    def heard(self, since):
        raise NotImplementedError

    def gate(self, t_kill):
        """Fill the handshake milestones in self.stones; True when probing makes sense."""
        return True

    # -- common
    def drop_clients(self):
        for keeper, items in self.sut_side:
            keeper.close()
            for item in items:
                stop_client(item)
        self.sut_side = []

    def ensure_clients(self, fresh=False):
        if fresh or not self.sut_side:
            self.drop_clients()
            self.build_clients()

    def probe(self, fresh=False):
        """One probe stream; its arrival time at the listener, or None."""
        self.ensure_clients(fresh)
        start = time.monotonic()
        self.send_probe()
        return wait_for(lambda: self.heard(start), 0.8)

    def prepare(self, seconds=BASELINE_S):
        """Rebuild the SUT-facing clients and wait (bounded) until a probe
        gets through; False when none does."""
        self.drop_clients()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.ensure_peer()
                if self.probe():
                    return True
            except (AssertionError, OSError, RuntimeError) as exc:
                self.last_error = repr(exc)
                print("%s: baseline attempt: %r" % (self.key, exc))
                self.drop_clients()
            time.sleep(0.5)
        return False

    def begin(self):
        self.stones = {}
        self.relayed = False

    def step(self, t_kill, fresh):
        """One bounded unit of work after the kill (about 2 s at most)."""
        if self.gate(t_kill) and "relay" not in self.stones:
            got = self.probe(fresh)
            if got:
                self.stones["relay"] = got
                self.relayed = True
        else:
            time.sleep(0.05)

    def restore(self, t_kill, seconds=RESTORE_S):
        """Best effort, bounded, never raises: wait for the link to work again."""
        deadline = time.monotonic() + seconds
        while not self.relayed and time.monotonic() < deadline:
            try:
                self.step(t_kill, True)
            except Exception as exc:                    # noqa: BLE001 - restore must not mask the failure
                print("%s: restore: %r" % (self.key, exc))
                time.sleep(1.0)
        return self.relayed


# ---------------------------------------------------------------- XLX

class XlxEdge(Edge):
    key, process, nn = "xlx", "dv-edge-xlx", "P-RLK-01"

    def open(self):
        if os.environ.get("DVT_INTERLINK") != "on":
            pytest.skip("no interlink rig: run.sh --profile interlink (DVT_INTERLINK=on)")
        self.ip = self.rig.xlxd_sut
        self.modules = os.environ.get("DVT_INTERLINK_MODULES", "DE")
        self.module = self.modules[0]
        self.peer = xlx.XlxPeer("XLX996", self.modules, SUT_VERSION)

    def close(self):
        self.drop_clients()
        self.peer.close()

    def ensure_peer(self):
        if self.ip not in self.peer.alive:
            assert self.peer.link(self.ip, seconds=8.0), \
                "no full XLX link to %s as XLX996 (does the SUT's config list XLX996 on %s?)" % (self.ip, self.modules)

    def build_clients(self):
        listener = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=self.module)
        assert listener.link(self.ip, timeout=1.0), "D-STAR listener link to %s failed" % self.ip
        self.sut_side.append((Keeper([listener]), [listener]))
        self.listener = listener

    def send_probe(self):
        frames = signals.prng_frames("dstar", PROBE_FRAMES, seed="relink-xlx")
        self.peer.talk(self.ip, self.module, frames, clients.fresh_stream_id("dstar"))

    def heard(self, since):
        c = self.listener.client
        return first_after(c.inbox, c.inbox_times, since, lambda d: dextra.parse_header(d) is not None)

    def gate(self, t_kill):
        st, peer = self.stones, self.peer
        if "connect" not in st:
            got = peer.packets(self.ip, t_kill, lambda d: (xlx.parse_link(d) or {}).get("tag") == "L")
            if got:
                st["connect"] = got[0][0]
        if "connect" in st and "ack" not in st and peer.answered.get(self.ip, 0) >= st["connect"]:
            st["ack"] = peer.answered[self.ip]
        if "ack" in st and "keepalive" not in st:
            got = peer.packets(self.ip, st["ack"], lambda d: xlx.parse_keepalive(d) == SUT_CALLSIGN)
            if got:
                st["keepalive"] = got[0][0]
        return "ack" in st


# ---------------------------------------------------------------- NXDN

class NxdnEdge(Edge):
    key, process, nn = "nxdn", "dv-edge-nxdn", "P-RLK-02"

    def open(self):
        env = os.environ
        if env.get("DVT_NXDN") != "on":
            pytest.skip("no NXDN rig: run.sh --profile nxdn (DVT_NXDN=on)")
        if not env.get("DVT_NXDN_PROBE_PORT_SUT"):
            pytest.skip("no tester-as-reflector row (DVT_NXDN_PROBE_*; rig/compose.yml profile nxdn)")
        self.ip = self.rig.xlxd_sut
        self.module = env["DVT_NXDN_PROBE_MODULE"]
        self.probe_ref = nxdn.FakeReflector("0.0.0.0", int(env["DVT_NXDN_PROBE_PORT_SUT"]),
                                            int(env["DVT_NXDN_PROBE_TG"]))
        self.pump = Keeper([self.probe_ref])        # answers (echoes) the SUT's polls all the time

    def close(self):
        self.drop_clients()
        self.pump.close()
        self.probe_ref.close()

    def build_clients(self):
        talker = dmr.DmrClient("0.0.0.0", dmrid=clients.DMR_IDS[0], module=self.module)
        listener = dmr.DmrClient("0.0.0.0", dmrid=clients.DMR_IDS[1], module=self.module)
        for item in (listener, talker):
            assert item.link(self.ip), "DMR link to %s failed" % self.ip
            item.client.inbox.clear()
            item.client.inbox_times.clear()
        self.talker = talker
        self.sut_side.append((Keeper([talker, listener], ping=lambda c: c.keepalive()), [talker, listener]))

    def send_probe(self):
        frames = [nxdn.to_dmr(nxdn.from_dmr(f)) for f in signals.prng_frames("dmr", PROBE_FRAMES, seed="relink-nxdn")]
        keeper = self.sut_side[0][0]
        with keeper.lock:
            self.talker.talk(frames, stream_id=clients.fresh_stream_id("dmr"), settle=0.3, tail=0.3)

    def heard(self, since):
        c = self.probe_ref.client

        def voice(data):
            info = nxdn.parse(data)
            return bool(info) and info["kind"] == "voice"
        return first_after(c.inbox, c.inbox_times, since, voice)

    def gate(self, t_kill):
        if "poll" not in self.stones:
            polls = [p for p in list(self.probe_ref.polls) if p[0] >= t_kill and p[2]["kind"] == "poll"]
            if polls:
                self.stones["poll"] = polls[0][0]
        return "poll" in self.stones


# ---------------------------------------------------------------- P25

class P25Edge(Edge):
    key, process, nn = "p25", "dv-edge-p25", "P-RLK-03"

    def open(self):
        env = os.environ
        if env.get("DVT_P25") != "on":
            pytest.skip("no P25 reflectors: run.sh --profile p25 (DVT_P25=on)")
        for name in ("ambed_ref", "ambed_sut"):
            ok, how = imbe_served(getattr(self.rig, name), self.rig.ambed_port)
            if not ok:
                pytest.skip("%s does not serve IMBE (%s): P25 needs IMBE transcoding" % (name, how))
        self.ip = self.rig.xlxd_sut
        self.p25_ip = self.rig.p25_sut
        self.tg = int(env.get("DVT_P25_TG", "31672"))
        self.module = env.get("DVT_P25_MODULE", "G")
        self.frames = encode(self.rig, ambed.AMBEPLUS, signals.frames(signals.tone(700, 20 * PROBE_FRAMES + 100)))
        self.listener = p25_client(self.tg, 1)
        assert self.listener.link(self.p25_ip, timeout=5.0), "P25 listener link to %s failed" % self.p25_ip
        self.pump = Keeper([self.listener], ping=lambda c: c.keepalive(), period=4.0)

    def close(self):
        self.drop_clients()
        self.pump.close()
        stop_client(self.listener)

    def build_clients(self):
        talker = dextra.DstarClient("0.0.0.0", callsign=clients.CALLSIGNS[0], module=self.module)
        assert talker.link(self.ip, timeout=1.0), "D-STAR talker link to %s failed" % self.ip
        self.talker = talker
        self.sut_side.append((Keeper([talker]), [talker]))

    def send_probe(self):
        with self.sut_side[0][0].lock:
            clients.talk(self.talker, "dstar", self.frames, SUT_CALLSIGN, tail=0.3)

    def heard(self, since):
        c = self.listener.client

        def voice(data):
            rec = p25.parse(data)
            return rec is not None and not rec["terminator"]
        return first_after(c.inbox, c.inbox_times, since, voice)


# ---------------------------------------------------------------- engine

def one_run(rig_control, edge, mode, run, limit):
    """Kill (or quietly restart) once; the seconds from the kill to the
    relay, or None. Every wait is bounded."""
    proc, sig = (edge.process, "USR1" if mode == "quiet" else "KILL")
    if not edge.prepare():
        pytest.fail("%s: baseline not established within %.0f s, nothing was killed: %s"
                    % (edge.key, BASELINE_S, relink.sut_state_text(sut_reachable(edge.ip), edge.last_error)),
                    pytrace=False)
    edge.begin()
    try:
        killed = rig_control.kill_proc("xlxd-sut", proc, sig, timeout=60)
    except BaseException:
        edge.relayed = True                 # nothing was signalled: nothing to restore
        raise
    t_kill = edge.last_kill = killed.t_kill_monotonic
    hard = time.monotonic() + (limit or 10.0) + OBSERVE_EXTRA_S
    try:
        while not edge.relayed and time.monotonic() < hard:
            try:
                edge.step(t_kill, fresh=False)
            except (AssertionError, OSError, RuntimeError) as exc:
                # a client could not link yet (the edge is still down): next attempt
                print("%s: attempt failed: %r" % (edge.key, exc))
                time.sleep(0.5)
    finally:
        if not edge.relayed:
            edge.restore(t_kill)
    stones = {k: round(v - t_kill, 3) for k, v in edge.stones.items()}
    seconds = stones.get("relay")
    reachable = sut_reachable(edge.ip)
    record({"case": os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0].split("::")[-1], "edge": edge.key,
            "mode": mode, "process": proc, "signal": sig, "run": run, "pids": killed.pids,
            "t_kill_epoch": killed.t_kill, "stones_s": stones, "relay_s": seconds,
            "limit_s": limit, "probe_frames": PROBE_FRAMES, "sut_reachable_after": reachable,
            "sut_exited": not reachable, "sut_exit_code": None})
    return seconds


def kill_runs(rig, rig_control, edge):
    limit = limit_s(rig, edge.nn)
    got = []
    try:
        for run in range(1, RUNS + 1):
            if run > 1:
                time.sleep(SETTLE_S)
            got.append(one_run(rig_control, edge, "kill", run, limit))
    finally:
        # always: leave the SUT running, relaying and the peer linked
        restore_sut(edge, rig_control)
    print("%s relay_s %s (limit %s)" % (edge.key, got, limit))
    assert all(s is not None for s in got), "%s: link not re-established after a kill: %s" % (edge.key, got)
    assert all(s <= limit for s in got), "%s: relay_s %s exceeds %s s (%s)" % (edge.key, got, limit, edge.nn)


def quiet_run(rig, rig_control, edge):
    entry = (specs.rule(rig, "relink") or {}).get(edge.nn) or {}
    try:
        one_run(rig_control, edge, "quiet", 1, entry.get("max_s"))
    finally:
        restore_sut(edge, rig_control)


@pytest.fixture(scope="module")
def xlx_edge(dvr):
    edge = XlxEdge(dvr)
    edge.open()
    yield edge
    edge.close()


@pytest.fixture(scope="module")
def nxdn_edge(dvr):
    edge = NxdnEdge(dvr)
    edge.open()
    yield edge
    edge.close()


@pytest.fixture(scope="module")
def p25_edge(dvr):
    edge = P25Edge(dvr)
    edge.open()
    yield edge
    edge.close()


# ---------------------------------------------------------------- cases

@pytest.mark.interlink
def test_edge_relink_xlx(dvr, rig_control, xlx_edge):
    """P-RLK-01: SIGKILL dv-edge-xlx three times; the XLX996 peer sees a new
    L (answered), keepalives and a probe stream relayed again within max_s.
    Needs the interlink profile (DVT_INTERLINK=on)."""
    kill_runs(dvr, rig_control, xlx_edge)


@pytest.mark.nxdn
def test_edge_relink_nxdn(dvr, rig_control, nxdn_edge):
    """P-RLK-02: SIGKILL dv-edge-nxdn three times; the SUT polls the tester's
    NXDN reflector again and a DMR probe stream is relayed as NXDND within
    max_s. Needs the nxdn profile (DVT_NXDN=on)."""
    kill_runs(dvr, rig_control, nxdn_edge)


@pytest.mark.p25
def test_edge_relink_p25(dvr, rig_control, p25_edge):
    """P-RLK-03: SIGKILL dv-edge-p25 three times; a D-STAR probe on the P25
    module reaches the P25 client of p25-sut within max_s. Needs the p25
    profile (DVT_P25=on) and IMBE transcoding."""
    kill_runs(dvr, rig_control, p25_edge)


# ---- quiet restart: record only, last in the module

@pytest.mark.interlink
def test_quiet_restart_xlx(dvr, rig_control, xlx_edge):
    """Record only: SIGUSR1 to dv-edge-xlx (quiet restart, sessions restored);
    seconds until the XLX996 peer link and a probe stream work again. Asserts
    nothing; the SUT is restarted when it does not come back. Needs the
    interlink profile (DVT_INTERLINK=on)."""
    quiet_run(dvr, rig_control, xlx_edge)


@pytest.mark.nxdn
def test_quiet_restart_nxdn(dvr, rig_control, nxdn_edge):
    """Record only: SIGUSR1 to dv-edge-nxdn; seconds until the SUT polls the
    NXDN reflector again and a probe is relayed. Asserts nothing. Needs the
    nxdn profile (DVT_NXDN=on)."""
    quiet_run(dvr, rig_control, nxdn_edge)


@pytest.mark.p25
def test_quiet_restart_p25(dvr, rig_control, p25_edge):
    """Record only: SIGUSR1 to dv-edge-p25; seconds until a D-STAR probe
    reaches the P25 client of p25-sut again. Asserts nothing. Needs the p25
    profile (DVT_P25=on) and IMBE transcoding."""
    quiet_run(dvr, rig_control, p25_edge)
