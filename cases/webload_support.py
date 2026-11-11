# SPDX-License-Identifier: MIT
"""Helpers of the web load case (cases/test_web_load.py, docs/web-load-cases.md); not a test module.

  LoadGen   the tester's client of the control server of the load generator (dvtest/tools/webload_gen.py)
  Source    the protocol side: one USRP dynamic client per room that talks the marker frames (dvtest.webload) at
            50 pps on an ABSOLUTE schedule (frame i is due at start + i * 20 ms, a late frame is never followed by
            a catch-up burst) and records how late every frame left
  Sampler   reads cpu ticks and VmHWM of the edge through the control channel (proc-stat) and cpu of the
            generator, at chosen wall-clock instants
  WebProcSampler  dv-svc-web cpu and thread count about once a second while the listeners connect (WEBSTALL1)

Offer retries and slow requests (run 20261111T123937Z: dv-svc-web stopped answering for 5.6 s during the ramp, the generator's queued offers were
released at once and the edge refused two for its 5-per-second join window): the generator sends a refused offer again (503 "WebRTC capacity
reached", at most 3 times, after max(Retry-After, 1.1 s)); the case fails when more than 5 % of the offers needed a retry or one ran out of retries.
Every web request slower than 1 s is reported as FINDING WEBSTALL1 (count, longest, the dv-svc-web cpu while it ran); not a failure yet.
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


# ------------------------------------------------------------------ offer retries and slow requests (WEBSTALL1)

RETRY_SHARE_MAX = 0.05             # more than this share of the offers needing a retry fails the case
SLOW_REPORT_S = 1.0                # a web request slower than this is a WEBSTALL1 observation (the generator's SLOW_S)
SLOW_ITEMS = 20


def retry_stats(status, n):
    """The retry numbers of a generator /status document for a run of n listeners."""
    retried = int(status.get("retried") or 0)
    return {"offers": n, "retried": retried, "retries": int(status.get("retries") or 0), "exhausted": int(status.get("exhausted") or 0),
            "share": round(retried / n, 4) if n else 0.0}


def retry_problems(stats):
    """What a run's retries say: an offer that ran out of retries, or more than RETRY_SHARE_MAX of the offers needing one, fails the run."""
    if not stats:
        return []
    out = []
    if stats.get("exhausted"):
        out.append("%d offers were refused 503 'WebRTC capacity reached' on every try (3 retries)" % stats["exhausted"])
    if stats.get("share", 0.0) > RETRY_SHARE_MAX:
        out.append("%d of %d offers (%.1f %%) needed a retry, more than %.0f %%" % (stats["retried"], stats["offers"], 100.0 * stats["share"], 100.0 * RETRY_SHARE_MAX))
    return out


def web_cpu_during(series, start, seconds):
    """Highest dv-svc-web cpu percent (one core = 100) of the sampling intervals that overlap [start, start + seconds]; None without samples there.
    series: rows [epoch of the interval end, cpu percent, threads]."""
    best = None
    for end, cpu, _threads in series:
        if end >= start and end - 1.0 <= start + seconds:
            best = cpu if best is None else max(best, cpu)
    return best


def slow_stats(status, series=()):
    """The slow-request numbers of a generator /status document: count, the longest, and up to SLOW_ITEMS in start order, each with the dv-svc-web
    cpu while it ran (a request that waited shows low cpu, a request that burned cpu shows high)."""
    items = []
    for row in status.get("slow") or []:
        item = {"method": row.get("m"), "path": row.get("p"), "seconds": row.get("d"), "start": row.get("t")}
        item["web_cpu_pct_during"] = web_cpu_during(series, item["start"] or 0.0, item["seconds"] or 0.0)
        items.append(item)
    count = int(status.get("slow_count") or len(items))
    longest = max(items, key=lambda i: i["seconds"] or 0.0) if items else None
    return {"count": count, "kept": len(items), "longest": longest,
            "items": sorted(items, key=lambda i: i["start"] or 0.0)[:SLOW_ITEMS]}


def stall_text(slow):
    """The text of FINDING WEBSTALL1, None when no request was slow."""
    if not slow or not slow.get("count"):
        return None
    top = slow["longest"] or {}
    return ("WEBSTALL1: %d web requests took longer than %.0f s during the load run; longest %s %s %.2f s (started %.3f, dv-svc-web cpu %s %% while it ran)"
            % (slow["count"], SLOW_REPORT_S, top.get("method"), top.get("path"), top.get("seconds") or 0.0, top.get("start") or 0.0,
               top.get("web_cpu_pct_during")))


def web_proc_series(samples):
    """[[epoch of the interval end, cpu percent of one core, threads]] from ProcSamples (cpu ticks, t, clk_tck, threads) of one process."""
    out = []
    for a, b in zip(samples, samples[1:]):
        dt = b.t - a.t
        if dt <= 0 or a.pid != b.pid:
            continue
        out.append([round(b.t, 3), round(100.0 * (b.ticks - a.ticks) / b.clk_tck / dt, 1), b.threads])
    return out


def web_proc_summary(samples, keep=400):
    """What the dv-svc-web sampler saw: sample count, pid changes, threads min / max, cpu p50 / max, the longest gap between two samples, and the
    series (thinned to `keep` rows) for a later look at a stall."""
    series = web_proc_series(samples)
    cpus = sorted(r[1] for r in series)
    gaps = [b.t - a.t for a, b in zip(samples, samples[1:])]
    stride = max(1, len(series) // keep)
    return {"samples": len(samples), "pids": sorted({s.pid for s in samples}), "threads_min": min((s.threads for s in samples), default=None),
            "threads_max": max((s.threads for s in samples), default=None), "cpu_pct_p50": wl.nearest_rank(cpus, 50) if cpus else None,
            "cpu_pct_max": cpus[-1] if cpus else None, "max_gap_s": round(max(gaps), 2) if gaps else None, "series": series[::stride]}


class WebProcSampler(threading.Thread):
    """dv-svc-web cpu ticks and thread count through the control channel (proc-stat), one request after the other (about one a second), until
    stop(). A request that fails is skipped. Runs while the listeners connect; the measured window is not shared with it."""

    def __init__(self, rig_control, process="dv-svc-web", service="xlxd-sut"):
        super().__init__(daemon=True, name="web-proc-sampler")
        self.ctl, self.process, self.service = rig_control, process, service
        self.samples = []
        self._stop_flag = threading.Event()

    def run(self):
        while not self._stop_flag.is_set():
            try:
                self.samples.append(wl.parse_procstat_out(self.ctl.proc_stat(self.service, self.process, timeout=10.0)))
            except Exception:                                    # noqa: BLE001 - a missing sample is a gap, never an error of the run
                time.sleep(0.5)
            self._stop_flag.wait(0.3)

    def stop(self):
        self._stop_flag.set()
        self.join(15.0)
        return list(self.samples)
