# SPDX-License-Identifier: MIT
"""Load generator of the web load test (W8): many receive-only WebRTC listeners through the real HTTP
signalling relay of dv-svc-web, in the rig service `webload` (profile webload, docs/web-load-cases.md).

Client stack: aiortc (pinned in .env.example, the only third-party code of the rig besides Playwright).
It speaks ICE (full agent against the edge's ICE-lite), DTLS-SRTP and PCMU like a browser does. Everything
above the socket is repo code: the listener signals through dvtest.web.WebClient (login, POST /rtc/offer,
POST /rtc/alive like the page), and the receive path is cut short so that nothing buffers: aiortc's jitter
buffer, depacketiser and decoder are bypassed, and the arrival time of every RTP datagram is taken where
asyncio hands it to the UDP protocol (`aioice.ice.StunProtocol.datagram_received`), before the DTLS
transport, SRTP authentication and parsing. The packet is then SRTP-authenticated by libsrtp (pylibsrtp)
as usual and its marker (dvtest.webload) says which source frame it is.

Two kinds of process, one image:
  python -m dvtest.tools.webload_gen serve --port 3100 --allow <tester ip>
      control server (stdlib http.server): POST /run, GET /status, GET /sample, POST /finish, POST /abort.
  python -m dvtest.tools.webload_gen worker
      one asyncio process holding a share of the listeners (whole logins), config on stdin (one JSON line),
      events on stdout (JSON lines), "finish" on stdin ends the run.

The server hands a run to W worker processes (the load is split over processes only because one Python
event loop cannot carry 200 receivers at 50 pps within the 2 cpus of the service); the listener j of the
plan belongs to login j // 8 and every login to one worker, so a login is made once. Nothing is written to
disk: counts and statistics are aggregated in memory and returned as one JSON document.
"""
import argparse
import array
import asyncio
import collections
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from dvtest import webload as wl

OFFER_PATH = "/api/v1/rtc/offer"
ALIVE_PATH = "/api/v1/rtc/alive"
CLOSE_PATH = "/api/v1/rtc/close"
CONNECT_TIMEOUT_S = 20.0
MAX_PACKETS = 20000             # per listener: a runaway source cannot grow memory without bound


def relay_sdp(sdp):
    """The offer as the web relay takes it. aiortc offers its certificate under sha-256, sha-384 and sha-512
    (RFC 8122 allows several); the relay's envelope check (web service `rtc/sdp.rs`) accepts only SHA-256
    fingerprint lines and refuses the offer otherwise, as it does for no fingerprint: a browser sends one. The DTLS
    certificate is the same, so only the extra lines are dropped."""
    keep = [ln for ln in sdp.splitlines()
            if not (ln.startswith("a=fingerprint:") and not ln.lower().startswith("a=fingerprint:sha-256 "))]
    return "\r\n".join(keep) + "\r\n"


LATE_MS = 5.0               # a packet whose spacing is off by more than this is "late" (late_bins)
LATE_BIN_MS = 10
LATE_BINS_MAX = 6000


def now_ns():
    return time.perf_counter_ns()


# ------------------------------------------------------------------ procfs / cgroup

def read_proc_ticks(pid="self"):
    """(utime + stime ticks, rss kib) of a process from /proc, or (None, None)."""
    try:
        with open("/proc/%s/stat" % pid, "rb") as fh:
            rest = fh.read().rsplit(b")", 1)[1].split()
        ticks = int(rest[11]) + int(rest[12])
        rss_pages = int(rest[21])
        return ticks, rss_pages * os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, IndexError, ValueError):
        return None, None


def read_cgroup():
    """cpu usage (microseconds) and memory of this container's cgroup (v2), or {}."""
    out = {}
    try:
        with open("/sys/fs/cgroup/cpu.stat", encoding="ascii") as fh:
            for line in fh:
                k, _, v = line.partition(" ")
                if k in ("usage_usec", "user_usec", "system_usec", "nr_throttled", "throttled_usec"):
                    out[k] = int(v)
        for name, key in (("memory.current", "mem_current"), ("memory.peak", "mem_peak")):
            try:
                with open("/sys/fs/cgroup/" + name, encoding="ascii") as fh:
                    out[key] = int(fh.read().strip())
            except OSError:
                pass
    except OSError:
        return {}
    return out


# ====================================================================== worker

class Listener:
    """One receive-only peer: its counters and arrival log. The receive hook runs in the event loop."""

    def __init__(self, slot, plan, counts_only=False):
        self.slot, self.plan = slot, plan
        self.counts_only = counts_only      # soak runs: count packets, keep no arrival log
        self.left = False                   # soak runs: the session ended by its own `leave`
        self.j = slot["j"]
        self.t = array.array("q")           # arrival ns of valid marker packets
        self.idx = array.array("i")         # their source frame indices
        self.rtp_seq = array.array("H")
        self.invalid = 0
        self.packets = 0
        self.tail_ok = True
        self.first_ns = None
        self.connected_at = None            # seconds since the offer went out
        self.answer_at = None
        self.alive = collections.Counter()
        self.error = None
        self.sess_id = None

    def on_rtp(self, t_ns, pkt):
        """pkt: one SRTP-authenticated, decrypted RTP packet."""
        self.packets += 1
        if len(pkt) < 12 or pkt[0] >> 6 != 2 or (pkt[1] & 0x7F) != 0:
            self.invalid += 1
            return
        if self.counts_only:
            return
        b0 = pkt[0]
        start = 12 + 4 * (b0 & 0x0F)
        if b0 & 0x10:
            if len(pkt) < start + 4:
                self.invalid += 1
                return
            start += 4 + 4 * ((pkt[start + 2] << 8) | pkt[start + 3])
        end = len(pkt) - (pkt[-1] if b0 & 0x20 and len(pkt) > start else 0)
        payload = bytes(pkt[start:end])
        idx = wl.payload_index(payload)
        if idx is None:
            self.invalid += 1
            return
        if len(self.t) >= MAX_PACKETS:
            return
        if not wl.tail_matches(payload):
            self.tail_ok = False
        if self.first_ns is None:
            self.first_ns = t_ns
        self.t.append(t_ns)
        self.idx.append(idx)
        self.rtp_seq.append((pkt[2] << 8) | pkt[3])


class _Datagram(bytes):
    """bytes that carry the arrival time (perf_counter_ns) taken in datagram_received: a plain bytes
    object has no __dict__ for the attribute."""


_STAMP = [0]                # arrival ns of the datagram being unprotected (read right after, same task step)
_SRTP_ERRORS = [0]


def install_patches():
    """Cut aiortc's receive path short and take arrival times at the UDP protocol. The three hooks keep the
    library's own ICE, DTLS handshake, SRTP authentication and RTCP handling; only the RTP packets go to the
    listener instead of the jitter buffer, depacketiser and decoder."""
    import pylibsrtp
    from aioice import ice as aice
    from aiortc import rtcdtlstransport as dtls

    def datagram_received(self, data, addr):
        if data and 127 < data[0] < 192:                   # SRTP / SRTCP (STUN starts with 0-3, DTLS with 20-63)
            s = _Datagram(data)
            s.t = now_ns()
            self.receiver.data_received(s, self.local_candidate.component)
            return
        orig_datagram(self, data, addr)

    orig_datagram = aice.StunProtocol.datagram_received
    aice.StunProtocol.datagram_received = datagram_received

    orig_unprotect = pylibsrtp.Session.unprotect

    def unprotect(self, data):
        _STAMP[0] = getattr(data, "t", None) or now_ns()
        try:
            return orig_unprotect(self, data)
        except pylibsrtp.Error:
            _SRTP_ERRORS[0] += 1
            raise

    pylibsrtp.Session.unprotect = unprotect

    orig_rtp = dtls.RTCDtlsTransport._handle_rtp_data

    async def handle_rtp_data(self, data, arrival_time_ms):
        sink = getattr(self, "_dvt_sink", None)
        if sink is None:
            return await orig_rtp(self, data, arrival_time_ms)
        sink(_STAMP[0], data)

    dtls.RTCDtlsTransport._handle_rtp_data = handle_rtp_data


class Worker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.plan = wl.Plan(**cfg["plan"])
        self.soak = bool(cfg.get("soak"))
        self.slots = cfg["slots"]
        self.accounts = {a["account"]: (a["callsign"], a["password"]) for a in cfg["accounts"]}
        self.start_mono = cfg["start_mono"]
        self.listeners = []
        self.clients = {}                   # login -> asyncio.Future of a logged-in WebClient
        self.budgets = {}                   # login -> wl.Bucket of the session's unsafe requests (2/s, burst 10)
        self.stop = asyncio.Event()
        self.pool = ThreadPoolExecutor(max_workers=int(cfg.get("threads", 4)), thread_name_prefix="http")
        self.lag = []                       # (ns, ms) of the event loop lag monitor
        self.pcs = []
        self.shown = False                  # the first offer/answer pair of this worker goes to the log (no secrets in SDP)

    # -- output
    @staticmethod
    def emit(**event):
        sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    # -- http (threads: urllib blocks)
    def make_client(self, account):
        from dvtest.web import WebClient
        callsign, password = self.accounts[account]
        client = WebClient(self.cfg["base_url"], self.cfg["ca"], timeout=30.0, user_agent=self.cfg["user_agent"])
        client.hide(password)
        client.login(callsign, password)
        return client

    async def pace(self, slot):
        """Wait for a token of this login's session budget (every offer and every alive is an unsafe request)."""
        bucket = self.budgets.setdefault(slot["login"], wl.Bucket(2.0, 10, reserve=3))
        wait = bucket.take()
        if wait > 0:
            await asyncio.sleep(wait)

    async def client_of(self, slot):
        login = slot["login"]
        fut = self.clients.get(login)
        if fut is None:
            fut = self.clients[login] = asyncio.get_running_loop().create_future()
            try:
                client = await asyncio.get_running_loop().run_in_executor(self.pool, self.make_client, slot["account"])
                fut.set_result(client)
            except Exception as exc:                    # noqa: BLE001 - reported per listener
                fut.set_exception(exc)
        return await fut

    # -- one listener
    async def listener(self, slot):
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpReceiver, RTCSessionDescription
        loop = asyncio.get_running_loop()
        lst = Listener(slot, self.plan, counts_only=self.soak)
        self.listeners.append(lst)
        wait = self.start_mono + slot["offset"] - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        pc = None
        try:
            client = await self.client_of(slot)
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.pcs.append(pc)
            connected = asyncio.Event()

            @pc.on("connectionstatechange")
            async def _state():                         # noqa: ANN202
                if pc.connectionState == "connected":
                    connected.set()
                elif pc.connectionState in ("failed", "closed") and not connected.is_set():
                    lst.error = lst.error or "connection " + pc.connectionState
                    connected.set()

            tr = pc.addTransceiver("audio", direction="recvonly")
            pcmu = [c for c in RTCRtpReceiver.getCapabilities("audio").codecs if c.name == "PCMU"]
            tr.setCodecPreferences(pcmu)
            tr.receiver.transport._dvt_sink = lst.on_rtp
            await pc.setLocalDescription(await pc.createOffer())
            offer_sdp = relay_sdp(pc.localDescription.sdp)
            await self.pace(slot)
            t_offer = time.monotonic()
            reply = await loop.run_in_executor(
                self.pool, lambda: client.request("POST", OFFER_PATH, json_body={"sdp": offer_sdp, "room": slot["room"]},
                                                  pace=False))
            if reply.status != 200:
                lst.error = "offer HTTP %d %s" % (reply.status, reply.text()[:120].replace("\n", " "))
                self.emit(ev="failed", j=lst.j, why=lst.error)
                return
            body = reply.json()
            lst.sess_id = body["sess_id"]
            if not self.shown:
                self.shown = True
                self.emit(ev="sdp", j=lst.j, offer=offer_sdp.splitlines(), answer=body["sdp"].splitlines())
            lst.answer_at = time.monotonic() - t_offer
            # setRemoteDescription(answer) runs ICE and the DTLS handshake to the end (aiortc awaits both)
            try:
                await asyncio.wait_for(pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="answer")),
                                       CONNECT_TIMEOUT_S)
                await asyncio.wait_for(connected.wait(), 2.0)
            except asyncio.TimeoutError:
                lst.error = "not connected %.0f s after the answer" % CONNECT_TIMEOUT_S
            if lst.error or pc.connectionState != "connected":
                lst.error = lst.error or "connection " + pc.connectionState
                self.emit(ev="failed", j=lst.j, why=lst.error, ice=pc.iceConnectionState, state=pc.connectionState)
                return
            lst.connected_at = time.monotonic() - t_offer
            self.emit(ev="connected", j=lst.j, s=round(lst.connected_at, 3), a=round(lst.answer_at, 3))
            t0 = loop.time()
            # the page renews at once, every second until six answers, then every 10 s; a login with eight circuits cannot
            # (8 per second against 2/s per session), and an authenticated circuit needs one renewal within its 15 s:
            # at once, then every 10 s, each through the login's budget
            leave = slot.get("leave")
            leave_at = None if leave is None else self.start_mono + leave
            for off in wl.alive_schedule(first_seconds=1):
                left = t0 + off - loop.time()
                if leave_at is not None:
                    left = min(left, leave_at - time.monotonic())
                try:
                    await asyncio.wait_for(self.stop.wait(), max(left, 0.0))
                    break
                except asyncio.TimeoutError:
                    pass
                if leave_at is not None and time.monotonic() >= leave_at:
                    await self.release(lst, client, pc)
                    return
                await self.pace(slot)
                status = await loop.run_in_executor(self.pool, lambda: self.post(client, ALIVE_PATH, lst.sess_id))
                lst.alive[status] += 1
        except Exception as exc:                        # noqa: BLE001 - one listener must not end the run
            lst.error = lst.error or "%s: %s" % (type(exc).__name__, str(exc)[:160])
            self.emit(ev="failed", j=lst.j, why=lst.error)
        finally:
            if not lst.left:
                lst.client, lst.pc = locals().get("client"), pc

    async def release(self, lst, client, pc):
        """A soak session ends: revoke the circuit like the page's Disconnect, close the peer and forget it."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.pool, lambda: self.post(client, CLOSE_PATH, lst.sess_id))
        try:
            await asyncio.wait_for(pc.close(), 15)
        except Exception:                               # noqa: BLE001 - a peer that does not close is still forgotten
            pass
        if pc in self.pcs:
            self.pcs.remove(pc)
        lst.left = True
        lst.client = lst.pc = None
        self.emit(ev="left", j=lst.j)

    @staticmethod
    def post(client, path, sess_id):
        try:
            return client.request("POST", path, json_body={"sess_id": sess_id}, pace=False).status
        except Exception as exc:                        # noqa: BLE001
            return "error:" + type(exc).__name__

    async def lag_monitor(self):
        while not self.stop.is_set():
            t0 = now_ns()
            await asyncio.sleep(0.005)
            t1 = now_ns()
            if len(self.lag) < 400000:
                self.lag.append((t1, (t1 - t0) / 1e6 - 5.0))

    def result_soak(self):
        """Compact rows of a soak run: no arrival log, one small row per session."""
        rows = [{"j": lst.j, "seat": lst.slot.get("seat"), "room": lst.slot["room"], "login": lst.slot["login"],
                 "connect_s": None if lst.connected_at is None else round(lst.connected_at, 3), "packets": lst.packets,
                 "invalid": lst.invalid, "left": lst.left, "error": lst.error,
                 "alive": {str(k): v for k, v in sorted(lst.alive.items(), key=str)}}
                for lst in sorted(self.listeners, key=lambda x: x.j)]
        ticks, rss = read_proc_ticks()
        return {"rows": rows, "srtp_errors": _SRTP_ERRORS[0],
                "loop_lag": {"samples": 0, "p50_ms": None, "p99_ms": None, "max_ms": None}, "pid": os.getpid(),
                "ticks": ticks, "rss_kib": rss}

    def result(self):
        if self.soak:
            return self.result_soak()
        rows = []
        lo = hi = None
        for lst in sorted(self.listeners, key=lambda x: x.j):
            arrivals = list(zip(lst.t, lst.idx))
            row = wl.analyse_listener(self.plan, arrivals, lst.invalid)
            row.update(j=lst.j, room=lst.slot["room"], login=lst.slot["login"],
                       connect_s=None if lst.connected_at is None else round(lst.connected_at, 3),
                       answer_s=None if lst.answer_at is None else round(lst.answer_at, 3),
                       packets=lst.packets, tail_ok=lst.tail_ok, error=lst.error,
                       alive={str(k): v for k, v in sorted(lst.alive.items(), key=str)})
            rows.append(row)
            first, end = self.plan.window
            win = [t for t, i in arrivals if first <= i < end]
            if win:
                lo = min(win) if lo is None else min(lo, min(win))
                hi = max(win) if hi is None else max(hi, max(win))
        late_bins = self.late_bins()
        lags = sorted(ms for t, ms in self.lag if lo is not None and lo <= t <= hi)
        lag = {"samples": len(lags), "p50_ms": wl.nearest_rank(lags, 50), "p99_ms": wl.nearest_rank(lags, 99),
               "max_ms": lags[-1] if lags else None}
        ticks, rss = read_proc_ticks()
        return {"rows": rows, "srtp_errors": _SRTP_ERRORS[0], "loop_lag": lag, "pid": os.getpid(),
                "ticks": ticks, "rss_kib": rss, "late_bins": late_bins}

    def late_bins(self):
        """Where in time the late packets are (THREADS1b evidence). A packet is late when its spacing to the
        previous in-window packet of the same listener differs from the frame interval by more than LATE_MS (the
        deviation the jitter figures use). Returned as {epoch ms of a LATE_BIN_MS bin: [late packets, largest
        deviation ms]} over all listeners of this worker, so a stall that hits every listener at once shows as one
        bin with many packets. The perf_counter arrival times are mapped to the wall clock at the time of the call."""
        off_ns = time.time_ns() - time.perf_counter_ns()
        first, end = self.plan.window
        bins = {}
        for lst in self.listeners:
            seen = set()
            times = []
            for t_ns, idx in zip(lst.t, lst.idx):
                if idx in seen:
                    continue
                seen.add(idx)
                if first <= idx < end:
                    times.append(t_ns)
            for a, b in zip(times, times[1:]):
                dev = abs((b - a) / 1e6 - wl.FRAME_MS)
                if dev > LATE_MS:
                    key = str(int((b + off_ns) / 1e6) // LATE_BIN_MS * LATE_BIN_MS)
                    cur = bins.setdefault(key, [0, 0.0])
                    cur[0] += 1
                    cur[1] = max(cur[1], round(dev, 2))
        if len(bins) > LATE_BINS_MAX:
            keep = sorted(bins.items(), key=lambda kv: -kv[1][0])[:LATE_BINS_MAX]
            bins = dict(keep)
        return bins

    async def teardown(self):
        loop = asyncio.get_running_loop()
        jobs = []
        for lst in self.listeners:
            client = getattr(lst, "client", None)
            if client is not None and lst.sess_id:
                jobs.append(loop.run_in_executor(self.pool, lambda c=client, s=lst.sess_id: self.post(c, CLOSE_PATH, s)))
        if jobs:
            await asyncio.wait(jobs, timeout=30)
        await asyncio.wait([asyncio.ensure_future(pc.close()) for pc in self.pcs] or [asyncio.sleep(0)], timeout=15)

    async def run(self):
        install_patches()
        loop = asyncio.get_running_loop()
        lag_task = None if self.soak else asyncio.ensure_future(self.lag_monitor())
        tasks = [asyncio.ensure_future(self.listener(s)) for s in self.slots]
        self.emit(ev="ready", n=len(tasks), pid=os.getpid())

        def stdin_reader():
            for line in sys.stdin:
                if line.strip() == "finish":
                    break
            loop.call_soon_threadsafe(self.stop.set)

        threading.Thread(target=stdin_reader, daemon=True).start()
        await self.stop.wait()
        res = self.result()
        self.emit(ev="result", **res)
        if lag_task is not None:
            lag_task.cancel()
        await self.teardown()
        for t in tasks:
            t.cancel()
        self.emit(ev="closed")


def worker_main():
    cfg = json.loads(sys.stdin.readline())
    asyncio.run(Worker(cfg).run())
    return 0


# ====================================================================== server

class Job:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.state = "starting"
        self.connected = {}                 # j -> connect seconds
        self.failed = {}                    # j -> why
        self.left = set()                   # soak: sessions that ended by their own leave
        self.workers = []                   # Popen
        self.ready = 0
        self.results = {}                   # worker index -> result event
        self.closed = 0
        self.started = time.time()
        self.t_first_offer = None
        self.error = None

    def status(self):
        with self.lock:
            return {"state": self.state, "listeners": self.cfg["listeners"], "workers": len(self.workers),
                    "connected": len(self.connected), "failed": len(self.failed), "left": len(self.left),
                    "live": len(set(self.connected) - self.left),
                    "failures": sorted(set(self.failed.values()))[:5], "ready": self.ready,
                    "results": len(self.results), "closed": self.closed, "error": self.error,
                    "elapsed_s": round(time.time() - self.started, 1)}


class Server:
    def __init__(self, allow):
        self.allow = allow
        self.job = None
        self.lock = threading.Lock()

    # -- starting a run
    def start(self, req):
        with self.lock:
            if self.job is not None and self.job.state not in ("done", "aborted", "error"):
                raise RuntimeError("a run is in progress")
            listeners = int(req["listeners"])
            workers = max(1, int(req.get("workers", 4)))
            plan = {"listeners": listeners, **req.get("plan", {})}
            wl.Plan(**plan)
            soak = req.get("soak")
            if soak:
                # a soak run: the tester sends the sessions (join and leave offsets, dvtest.soak.build_sessions);
                # `listeners` is the number of seats held at a time
                from dvtest import soak as soak_lib
                slot_rows = [dict(row) for row in soak["sessions"]]
                problems = soak_lib.validate_sessions(slot_rows)
                if problems:
                    raise ValueError("sessions break the budgets: %s" % "; ".join(problems[:3]))
                offsets = sorted(row["offset"] for row in slot_rows)
            else:
                slot_rows = [{"j": s.index, "login": s.login, "account": s.account, "room": s.room} for s in wl.slots(listeners)]
                offsets = wl.paced_offsets(listeners, wl.RTC_IP_RATE, wl.RTC_IP_BURST)
                for row, off in zip(slot_rows, offsets):
                    row["offset"] = off
            accounts = req["accounts"]
            if len(accounts) < wl.accounts_needed(listeners):
                raise ValueError("%d listeners need %d accounts, got %d" % (listeners, wl.accounts_needed(listeners), len(accounts)))
            cfg = {"listeners": listeners, "workers": workers, "plan": plan, "base_url": req["base_url"], "ca": req["ca"],
                   "user_agent": req.get("user_agent", "dvt-webload/1"), "threads": req.get("threads", 4), "soak": bool(soak)}
            job = Job(cfg)
            start_mono = time.monotonic() + 2.0
            for w in range(workers):
                mine = [r for r in slot_rows if wl.worker_of_login(r["login"], workers) == w]
                if not mine:
                    continue
                need = sorted({r["account"] for r in mine})
                wcfg = dict(cfg, slots=mine, start_mono=start_mono,
                            accounts=[{"account": a, "callsign": accounts[a][0], "password": accounts[a][1]} for a in need])
                proc = subprocess.Popen([sys.executable, "-m", "dvtest.tools.webload_gen", "worker"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, bufsize=1)
                proc.stdin.write(json.dumps(wcfg) + "\n")
                proc.stdin.flush()
                job.workers.append(proc)
                threading.Thread(target=self.reader, args=(job, w, proc), daemon=True).start()
            job.state = "connecting"
            self.job = job
            return {"workers": len(job.workers), "connect_estimate_s": round(offsets[-1] + 20, 1) if offsets else 0}

    @staticmethod
    def reader(job, w, proc):
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with job.lock:
                kind = ev.get("ev")
                if kind == "ready":
                    job.ready += 1
                elif kind == "connected":
                    job.connected[ev["j"]] = ev["s"]
                elif kind == "left":
                    job.left.add(ev["j"])
                elif kind == "failed":
                    job.failed[ev["j"]] = str(ev.get("why", ""))[:200]
                elif kind == "result":
                    job.results[w] = ev
                elif kind == "closed":
                    job.closed += 1
        with job.lock:
            if job.state in ("connecting", "measuring") and proc.poll() not in (None, 0):
                job.error = "worker %d exited with %s" % (w, proc.returncode)
                job.state = "error"

    def sample(self):
        """CPU of the container, of the control server and of every worker; used for the generator's own
        CPU over the window (two samples, difference)."""
        out = {"t": time.time(), "cgroup": read_cgroup(), "clk_tck": os.sysconf("SC_CLK_TCK"), "procs": {}}
        ticks, rss = read_proc_ticks()
        out["procs"]["server"] = {"ticks": ticks, "rss_kib": rss}
        job = self.job
        if job:
            for w, proc in enumerate(job.workers):
                t, r = read_proc_ticks(proc.pid)
                out["procs"]["worker%d" % w] = {"ticks": t, "rss_kib": r}
        return out

    def finish(self, timeout=120):
        job = self.job
        if job is None:
            raise RuntimeError("no run")
        with job.lock:
            job.state = "collecting"
        for proc in job.workers:
            try:
                proc.stdin.write("finish\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with job.lock:
                if len(job.results) >= len(job.workers):
                    break
            time.sleep(0.2)
        with job.lock:
            got = dict(job.results)
        if len(got) < len(job.workers):
            job.state = "error"
            raise RuntimeError("only %d of %d workers returned a result" % (len(got), len(job.workers)))
        rows, errors, lags, procs, late = [], 0, [], {}, {}
        for w, res in sorted(got.items()):
            rows += res["rows"]
            for key, (n, dev) in (res.get("late_bins") or {}).items():
                cur = late.setdefault(key, [0, 0.0])
                cur[0] += n
                cur[1] = max(cur[1], dev)
            errors += res["srtp_errors"]
            lags.append(dict(res["loop_lag"], worker=w))
            procs["worker%d" % w] = {"ticks": res["ticks"], "rss_kib": res["rss_kib"]}
        rows.sort(key=lambda r: r["j"])
        job.state = "done"
        return {"rows": rows, "srtp_errors": errors, "loop_lag": lags, "connect_failures": dict(job.failed),
                "late_bins": late}

    def abort(self):
        job = self.job
        if job is None:
            return
        for proc in job.workers:
            try:
                proc.stdin.write("finish\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + 45
        for proc in job.workers:
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
        job.state = "aborted"


def handler_for(server):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):             # no request log: bodies hold passwords, paths are few
            pass

        def reply(self, code, obj):
            body = json.dumps(obj, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            if server.allow and self.client_address[0] not in server.allow:
                self.reply(403, {"error": "forbidden"})
                return False
            return True

        def do_GET(self):                               # noqa: N802
            if not self.allowed():
                return
            if self.path == "/health":
                return self.reply(200, {"ok": True})
            if self.path == "/sample":
                return self.reply(200, server.sample())
            if self.path == "/status":
                return self.reply(200, server.job.status() if server.job else {"state": "idle"})
            self.reply(404, {"error": "not found"})

        def do_POST(self):                              # noqa: N802
            if not self.allowed():
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(raw or b"{}")
                if self.path == "/run":
                    return self.reply(200, server.start(req))
                if self.path == "/finish":
                    return self.reply(200, server.finish(float(req.get("timeout", 120))))
                if self.path == "/abort":
                    server.abort()
                    return self.reply(200, {"ok": True})
            except (KeyError, ValueError, RuntimeError, TypeError) as exc:
                return self.reply(400, {"error": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
            self.reply(404, {"error": "not found"})

    return Handler


def serve_main(args):
    allow = set(a for a in args.allow.split(",") if a)
    server = Server(allow)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", args.port), handler_for(server))
    httpd.daemon_threads = True
    print(json.dumps({"ev": "serving", "port": args.port, "pid": os.getpid(), "host": socket.gethostname()}), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dvtest.tools.webload_gen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=3100)
    s.add_argument("--allow", default="", help="comma separated client addresses (default: any)")
    sub.add_parser("worker")
    args = ap.parse_args(argv)
    return serve_main(args) if args.cmd == "serve" else worker_main()


if __name__ == "__main__":
    sys.exit(main())
