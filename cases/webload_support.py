# SPDX-License-Identifier: MIT
"""Helpers of the web load case (cases/test_web_load.py, docs/web-load-cases.md); not a test module.

  LoadGen   the tester's client of the control server of the load generator (dvtest/tools/webload_gen.py)
  Source    the protocol side: one USRP dynamic client per room that talks the marker frames (dvtest.webload) at
            50 pps on an ABSOLUTE schedule (frame i is due at start + i * 20 ms, a late frame is never followed by
            a catch-up burst) and records how late every frame left
  Sampler   reads cpu ticks and VmHWM of the edge through the control channel (proc-stat) and cpu of the
            generator, at chosen wall-clock instants
"""
import json
import threading
import time
import urllib.error
import urllib.request

from crossmode_fake import UsrpPeer, usrp_hub
from dvtest import webload as wl
from dvtest.proto import usrp

SPIN_S = 0.0015                 # busy-wait for the last 1.5 ms before a frame is due


class LoadGenError(RuntimeError):
    pass


class LoadGen:
    """HTTP/JSON client of the generator's control server (no proxy, no TLS: it is rig-internal)."""

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, timeout=30.0):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with self._opener.open(req, timeout=timeout) as reply:
                return json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise LoadGenError("%s %s -> HTTP %d %s" % (method, path, exc.code, exc.read()[:300].decode("utf-8", "replace"))) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LoadGenError("%s %s: %s" % (method, path, exc)) from exc

    def health(self):
        return self.call("GET", "/health", timeout=3.0).get("ok") is True

    def run(self, request):
        return self.call("POST", "/run", request, timeout=60.0)

    def status(self):
        return self.call("GET", "/status", timeout=10.0)

    def sample(self):
        return self.call("GET", "/sample", timeout=10.0)

    def finish(self, timeout=180.0):
        return self.call("POST", "/finish", {"timeout": timeout}, timeout=timeout + 30.0)

    def abort(self):
        return self.call("POST", "/abort", {}, timeout=90.0)


def generator_cpu(a, b):
    """Own cpu of the generator between two /sample documents: percent of ONE core for the container (cgroup
    usage) and for the control server and each worker (process ticks), peak memory of the cgroup in MiB."""
    dt = b["t"] - a["t"]
    out = {"seconds": round(dt, 3), "processes_pct": {}}
    ca, cb = a.get("cgroup", {}), b.get("cgroup", {})
    if "usage_usec" in ca and "usage_usec" in cb and dt > 0:
        out["container_pct"] = round(100.0 * (cb["usage_usec"] - ca["usage_usec"]) / 1e6 / dt, 2)
        out["throttled_usec"] = cb.get("throttled_usec", 0) - ca.get("throttled_usec", 0)
    tck = b.get("clk_tck", 100)
    total = 0.0
    for name, pb in b.get("procs", {}).items():
        pa = a.get("procs", {}).get(name)
        if pa and pa.get("ticks") is not None and pb.get("ticks") is not None and dt > 0:
            pct = 100.0 * (pb["ticks"] - pa["ticks"]) / tck / dt
            out["processes_pct"][name] = round(pct, 2)
            total += pct
    out["processes_total_pct"] = round(total, 2)
    if "mem_peak" in cb:
        out["mem_peak_mib"] = round(cb["mem_peak"] / 1048576.0, 1)
    if "mem_current" in cb:
        out["mem_current_mib"] = round(cb["mem_current"] / 1048576.0, 1)
    out["rss_mib"] = {n: round(p["rss_kib"] / 1024.0, 1) for n, p in b.get("procs", {}).items() if p.get("rss_kib")}
    return out


class Sampler:
    """proc-stat of the SUT's edge and /sample of the generator."""

    def __init__(self, rig_control, loadgen, process="dv-edge-webrtc"):
        self.ctl, self.gen, self.process = rig_control, loadgen, process
        self.lead_s = 0.0

    def edge(self):
        t_req = time.time()
        sample = wl.parse_procstat_out(self.ctl.proc_stat("xlxd-sut", self.process))
        return sample, t_req

    def calibrate(self, rounds=3):
        """Time from a proc-stat request to the read inside the helper; later samples are requested that much
        earlier so that the read lands on the instant asked for."""
        leads = []
        for _ in range(rounds):
            sample, t_req = self.edge()
            leads.append(sample.t - t_req)
        self.lead_s = sorted(leads)[len(leads) // 2]
        return self.lead_s

    def at(self, epoch):
        """Both samples as close to `epoch` (wall clock) as the request latency allows. Returns (edge, gen,
        error_s) with error_s = actual read time - epoch of the edge sample."""
        wait = epoch - self.lead_s - time.time()
        if wait > 0:
            time.sleep(wait)
        g = self.gen.sample()
        sample, _t_req = self.edge()
        return sample, g, sample.t - epoch


class Source:
    """USRP dynamic clients (one per room) sending marker frames on an absolute schedule."""

    CALLSIGNS = ("N0TEST", "N0CALL", "N0TEST1")      # one per room: see docs/web-load-cases.md, finding F3 (OriginHoldsFloor)

    def __init__(self, target, rooms):
        hub, why = usrp_hub(target)
        if hub is None:
            import pytest
            pytest.skip(why)
        self.peers = {}
        for room, callsign in zip(rooms, self.CALLSIGNS):
            peer = UsrpPeer(hub, callsign=callsign, module=room)
            if not peer.link(timeout=8.0):
                for p in self.peers.values():
                    p.close()
                peer.close()
                import pytest
                pytest.skip("USRP client %s gets no REG:OK from %s:%d: the dynamic USRP hub is off (run with DVT_USRP_DYNAMIC=1)"
                            % (callsign, hub[0], hub[1]))
            peer.pump(0.2)
            peer.inbox.clear()
            self.peers[room] = peer
        self.threads = []
        self.sched = {r: [] for r in rooms}
        self.actual = {r: [] for r in rooms}
        self.errors = []
        self.at_start = threading.Event()        # set when the first frame of the measured window is due
        self.at_end = threading.Event()          # ... and when the window is over
        self.done = threading.Event()
        self.t0_mono = None
        self.t0_epoch = None

    def start(self, plan, delay_s=2.0):
        """Begin sending `delay_s` from now; returns the epoch time of frame 0."""
        frames = [wl.frame_pcm(i) for i in range(plan.total_frames)]          # before the clock starts
        self.t0_mono = time.monotonic() + delay_s
        self.t0_epoch = time.time() + delay_s
        first, end = plan.window
        for room, peer in self.peers.items():
            th = threading.Thread(target=self._run, args=(room, peer, frames), daemon=True, name="src-" + room)
            self.threads.append(th)
            th.start()
        threading.Thread(target=self._marks, args=(first, end), daemon=True).start()
        return self.t0_epoch

    def epoch_of(self, index):
        return self.t0_epoch + index * wl.FRAME_MS / 1000.0

    def _marks(self, first, end):
        for event, index in ((self.at_start, first), (self.at_end, end)):
            time.sleep(max(0.0, self.t0_mono + index * wl.FRAME_MS / 1000.0 - time.monotonic()))
            event.set()

    def _run(self, room, peer, frames):
        gap = wl.FRAME_MS / 1000.0
        try:
            for i, pcm in enumerate(frames):
                due = self.t0_mono + i * gap
                left = due - time.monotonic()
                if left > SPIN_S:
                    time.sleep(left - SPIN_S)
                while time.monotonic() < due:
                    pass
                sent = time.monotonic()
                peer.send(usrp.voice, pcm)
                self.sched[room].append(due)
                self.actual[room].append(sent)
            peer.send(usrp.unkey)
        except Exception as exc:                     # noqa: BLE001 - reported by the case
            self.errors.append("%s: %s: %s" % (room, type(exc).__name__, exc))
        finally:
            self.done.set()

    def join(self, timeout):
        for th in self.threads:
            th.join(timeout)
        return all(not th.is_alive() for th in self.threads)

    def lateness(self):
        """Per room and in all: wl.source_lateness over every frame sent."""
        per = {r: wl.source_lateness(self.sched[r], self.actual[r]) for r in self.peers}
        allsched = [t for r in self.peers for t in self.sched[r]]
        allact = [t for r in self.peers for t in self.actual[r]]
        total = wl.source_lateness(allsched, allact)
        total["rooms"] = per
        return total

    def close(self):
        for peer in self.peers.values():
            try:
                peer.unlink()
            except OSError:
                pass
            peer.close()
