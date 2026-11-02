# SPDX-License-Identifier: MIT
"""Helpers of the web soak case (cases/test_web_soak.py, docs/web-soak-cases.md); not a test module.

  SampleFile    the one raw file of the run (soak-samples.jsonl): thread safe, one flushed JSON object per line
  discover      the children of the launcher, from `proc-stat xlxd-sut list`
  ChildSampler  proc-stat of every child, an hub gauge and error counters, on an absolute 30 s schedule in a thread
  Lane          one protocol client mix on one module that makes a full over (link, talk, collect, unlink)
  Traffic       runs the over schedule (dvtest.soak.build_overs) on a thread pool until told to stop

The protocol clients are the ones of the matrix and protocol cases (dvtest.clients, dvtest.proto.nxdn / p25, the
USRP dynamic peer of cases/crossmode_fake.py); every over makes new sockets and links them, so a client table
leak of the system under test shows as a growing child, and nothing here depends on a client keeping a link alive.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from crossmode_fake import UsrpPeer, usrp_hub
from dvtest import clients, control, metrics, soak
from dvtest import webload as wl
from dvtest.audio import signals
from dvtest.proto import nxdn, p25

LISTENERS = "dv_edge_webrtc_listeners"
REQUIRED_LANES = ("usrp-a", "usrp-b", "dstar", "dmr", "ysf", "m17")
OPTIONAL_LANES = ("nxdn", "p25")
DEFAULT_MODULES = {"usrp-a": "A", "usrp-b": "B", "dstar": "D", "dmr": "E", "ysf": "C", "m17": "J", "nxdn": "F", "p25": "G"}
USRP_CALLSIGNS = {"usrp-a": "N0TEST", "usrp-b": "N0CALL"}        # one per room (docs/web-load-cases.md F3)
NXDN_IDS = (1001, 1002)                                           # N0CALL, N0TEST in rig/config/nxdn.csv


def parse_modules(text, env_nxdn=None, env_p25=None):
    """Module of every lane: the defaults, DVT_SOAK_MODULES ("dstar=D,dmr=E,...") over them, and the NXDN / P25
    modules of the rig profiles (DVT_NXDN_MODULE, DVT_P25_MODULE) unless the list names them. ValueError on a
    lane or module that does not exist, or on two lanes on one module (their overs would take the floor from each other)."""
    mods = dict(DEFAULT_MODULES)
    if env_nxdn:
        mods["nxdn"] = env_nxdn
    if env_p25:
        mods["p25"] = env_p25
    for part in [p for p in (text or "").replace(" ", "").split(",") if p]:
        lane, sep, module = part.partition("=")
        if not sep or lane not in mods or len(module) != 1 or not module.isalpha() or not module.isupper():
            raise ValueError("bad DVT_SOAK_MODULES entry %r (lanes: %s; one module letter A-Z)" % (part, ", ".join(sorted(mods))))
        mods[lane] = module
    if len(set(mods.values())) != len(mods):
        raise ValueError("two lanes share a module: %s" % mods)
    return mods


# ------------------------------------------------------------------ the raw file and the sampling

class SampleFile:
    """Append-only JSONL, flushed per row, usable from several threads."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.rows = 0

    def write(self, row):
        line = soak.dump_line(row) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            self.rows += 1

    def close(self):
        with self._lock:
            self._fh.close()


def discover(ctl):
    """(children, others): the allow-listed process names found among the children of the launcher dv-router, and
    the names of children that are NOT in the allow-list (unsampled: the case reports them). ValueError when the
    launcher is not in the listing."""
    rows = ctl.proc_list()
    launcher = [pid for pid, _ppid, comm in rows if comm == "dv-router"]
    if not launcher:
        raise ValueError("no dv-router in the process list: %s" % sorted({c for _p, _pp, c in rows})[:12])
    kids = [(pid, comm) for pid, ppid, comm in rows if ppid in launcher]
    names = sorted({c for _p, c in kids if c in control.PROC_STAT_NAMES} | {"dv-router"})
    others = sorted({c for _p, c in kids if c not in control.PROC_STAT_NAMES})
    return names, others


class ChildSampler:
    """One proc-stat per child per tick, a listener gauge and the generator's status. `phase` is read at the time
    of the tick; the case moves it. An absent child (error 3) is written as an absent row; any other failure is
    counted in `errors` and written as a mark."""

    def __init__(self, ctl, names, sink, loadgen=None, period_s=soak.SAMPLE_S):
        self.ctl, self.names, self.sink, self.gen, self.period = ctl, list(names), sink, loadgen, period_s
        self.phase = "idle0"
        self.seq = 0
        self.errors = []
        self.slow_ticks = 0
        self.stop = threading.Event()
        self.thread = None

    def tick(self, phase=None):
        phase = phase or self.phase
        self.seq += 1
        for name in self.names:
            try:
                sample = wl.parse_procstat_out(self.ctl.proc_stat("xlxd-sut", name))
                self.sink.write(soak.proc_row(sample, phase, self.seq))
            except RuntimeError as exc:
                if str(exc).rstrip().endswith("error 3"):
                    self.sink.write(soak.absent_row(name, time.time(), phase, self.seq))
                else:
                    self._error(phase, "%s: %s" % (name, str(exc)[:120]))
            except (TimeoutError, ValueError) as exc:
                self._error(phase, "%s: %s: %s" % (name, type(exc).__name__, str(exc)[:120]))
        try:
            text = self.ctl.metrics("xlxd-sut")
            self.sink.write(soak.gauge_row(time.time(), phase, round(metrics.total(metrics.parse(text), LISTENERS))))
        except (RuntimeError, TimeoutError, OSError) as exc:
            self._error(phase, "metrics: %s" % str(exc)[:120])
        if self.gen is not None and phase in ("warmup", "measured", "stopping") and self.seq % 10 == 0:
            try:
                st = self.gen.status()
                self.sink.write(soak.mark_row(time.time(), phase, json.dumps({k: st.get(k) for k in ("state", "connected", "failed", "left", "live")},
                                                                               sort_keys=True)))
            except Exception as exc:                                  # noqa: BLE001 - the generator's status is only a note
                self._error(phase, "generator status: %s" % str(exc)[:100])

    def _error(self, phase, text):
        self.errors.append(text)
        self.sink.write(soak.mark_row(time.time(), phase, "sampling error: " + text))

    def counters(self, label):
        """A counters row labelled for soak.judge_counters (idle0, warmup_end, measured_end, idle_start, idle_end)."""
        for attempt in range(3):
            try:
                samples = metrics.parse(self.ctl.metrics("xlxd-sut"))
                break
            except (RuntimeError, TimeoutError, OSError):
                if attempt == 2:
                    raise
                time.sleep(5.0)
        self.sink.write(soak.counters_row(time.time(), label, soak.error_counters(samples)))

    def _loop(self):
        nxt = time.monotonic()
        while not self.stop.is_set():
            began = time.monotonic()
            try:
                self.tick()
            except Exception as exc:                                  # noqa: BLE001 - the sampler must outlive one bad tick
                self._error(self.phase, "tick: %s: %s" % (type(exc).__name__, str(exc)[:100]))
            nxt += self.period
            if time.monotonic() - began > self.period:
                self.slow_ticks += 1
                nxt = time.monotonic()
            self.stop.wait(max(0.0, nxt - time.monotonic()))

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="soak-sampler", daemon=True)
        self.thread.start()

    def halt(self, timeout=90.0):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout)


# ------------------------------------------------------------------ protocol lanes

def frame_count(dur_s, multiple=1):
    n = int(dur_s * 50)
    return n - n % multiple


def pump_while(job, listeners):
    while not job.done():
        for c in listeners:
            c.client.pump(0.05)
    job.result()


class Lane:
    """A talker and its listeners on one module of the system under test. over(dur_s, seed) runs one full over and
    returns {"lane", "ok", "sent", "heard": {kind: frames}, "error"}; it never raises."""

    def __init__(self, name, module):
        self.name, self.module = name, module

    def over(self, dur_s, seed):
        result = {"lane": self.name, "ok": False, "sent": 0, "heard": {}, "error": None}
        try:
            self._over(dur_s, seed, result)
        except Exception as exc:                                      # noqa: BLE001 - one bad over must not end the soak
            result["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return result


class DigitalLane(Lane):
    """kinds of dvtest.clients: one talker kind, listener kinds on the same module of xlxd-sut. Passthrough
    frames (same AMBE family), so no transcoder is involved."""

    def __init__(self, name, module, ip, talker, listeners, codec, multiple=1, peer_ip=None):
        super().__init__(name, module)
        self.ip, self.talker, self.listeners, self.codec, self.multiple, self.peer_ip = ip, talker, listeners, codec, multiple, peer_ip

    def _over(self, dur_s, seed, result):
        frames = signals.prng_frames(self.codec, frame_count(dur_s, self.multiple), seed="soak/%s" % seed)
        listeners, talker = [], None
        try:
            targets = [(k, self.ip) for k in self.listeners]
            if self.peer_ip:
                targets.append((self.talker, self.peer_ip))
            for kind, ip in targets:
                client = clients.make(kind, self.module, 1)
                if not client.link(ip):
                    client.close()
                    raise AssertionError("%s listener link to %s failed" % (kind, ip))
                client.tag = kind if ip == self.ip else kind + "@peer"
                listeners.append(client)
            talker = clients.make(self.talker, self.module, 0)
            if not talker.link(self.ip):
                raise AssertionError("%s talker link failed" % self.talker)
            for c in listeners:
                c.client.drain()
                c.client.inbox.clear()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pump_while(pool.submit(clients.talk, talker, self.talker, frames), listeners)
            result["sent"] = len(frames)
            result["heard"] = {c.tag: len(c.collect()) for c in listeners}
            result["ok"] = all(n >= 0.5 * len(frames) for n in result["heard"].values())
        finally:
            clients.close(*[c for c in [talker] + listeners if c is not None])


class UsrpLane(Lane):
    """A USRP dynamic client that talks into a room; the audience are the WebRTC listeners of the generator."""

    def __init__(self, name, module, hub, callsign):
        super().__init__(name, module)
        self.hub, self.callsign = hub, callsign

    def _over(self, dur_s, seed, result):
        peer = UsrpPeer(self.hub, callsign=self.callsign, module=self.module)
        try:
            if not peer.link(timeout=8.0):
                raise AssertionError("USRP REG:OK missing from %s:%d" % self.hub)
            peer.inbox.clear()
            frames = signals.frames(signals.tone(700, int(dur_s * 1000), amp=8000))
            peer.talk(frames, tail=0.5)
            result["sent"] = len(frames)
            result["ok"] = True              # judged by the listeners that heard it (soak.generator_summary)
        finally:
            try:
                peer.unlink()
            except OSError:
                pass
            peer.close()


class NxdnLane(Lane):
    """NXDN talker on the NXDNReflector peered with the sut (DVT_NXDN_SUT, DVT_NXDN_TG), DMR listener on the module."""

    def __init__(self, name, module, xlxd_ip, reflector_ip, tg):
        super().__init__(name, module)
        self.ip, self.reflector, self.tg = xlxd_ip, reflector_ip, tg

    def _over(self, dur_s, seed, result):
        frames = [nxdn.from_dmr(f) for f in signals.prng_frames("dmr", frame_count(dur_s, 12), seed="soak/%s" % seed)]
        listener = clients.make("dmr", self.module, 1)
        talker = None
        try:
            if not listener.link(self.ip):
                raise AssertionError("DMR listener link failed")
            talker = nxdn.NxdnClient("0.0.0.0", self.tg, nxdn_id=NXDN_IDS[0], callsign=clients.CALLSIGNS[0])
            if not talker.link(self.reflector):
                raise AssertionError("NXDN link to %s failed" % self.reflector)
            listener.client.drain()
            listener.client.inbox.clear()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pump_while(pool.submit(talker.talk, frames), [listener])
            result["sent"] = len(frames)
            result["heard"] = {"dmr": len(listener.collect())}
            result["ok"] = result["heard"]["dmr"] >= 0.5 * len(frames)
        finally:
            for c in (talker, listener):
                if c is not None:
                    try:
                        if c.dest:
                            c.unlink()
                    finally:
                        c.close()


class P25Lane(Lane):
    """P25 talker on the P25Reflector peered with the sut (DVT_P25_SUT, DVT_P25_TG), D-STAR listener on the module
    (IMBE is transcoded: needs the codec worker with IMBE)."""

    def __init__(self, name, module, xlxd_ip, reflector_ip, tg):
        super().__init__(name, module)
        self.ip, self.reflector, self.tg = xlxd_ip, reflector_ip, tg

    def _over(self, dur_s, seed, result):
        frames = signals.prng_frames("imbe", frame_count(dur_s, 9), seed="soak/%s" % seed)
        listener = clients.make("dstar", self.module, 1)
        talker = None
        try:
            if not listener.link(self.ip):
                raise AssertionError("D-STAR listener link failed")
            talker = p25.P25Client("0.0.0.0", callsign=clients.CALLSIGNS[0], src_id=clients.DMR_IDS[0], tg=self.tg)
            if not talker.link(self.reflector):
                raise AssertionError("P25 link to %s failed" % self.reflector)
            listener.client.drain()
            listener.client.inbox.clear()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pump_while(pool.submit(talker.talk, frames), [listener])
            result["sent"] = len(frames)
            result["heard"] = {"dstar": len(listener.collect())}
            result["ok"] = result["heard"]["dstar"] >= 0.25 * len(frames)
        finally:
            try:
                if talker is not None:
                    talker.unlink()
            finally:
                if talker is not None:
                    talker.close()
                clients.close(listener)


def make_lanes(target, env, modules):
    """({name: Lane}, {name: reason it is not in the mix}). The required lanes always exist (their probe decides);
    NXDN and P25 exist only when the rig profile says the reflector is peered with the sut."""
    lanes, skipped = {}, {}
    hub, why = usrp_hub(target)
    for name in ("usrp-a", "usrp-b"):
        if hub is None:
            skipped[name] = why
        else:
            lanes[name] = UsrpLane(name, modules[name], hub, USRP_CALLSIGNS[name])
    peer = target.xlxd_peer_sut if env.get("DVT_INTERLINK") == "on" and target.xlxd_peer_sut else None
    lanes["dstar"] = DigitalLane("dstar", modules["dstar"], target.xlxd_sut, "dstar", ("dplus", "dcs"), "dstar", peer_ip=peer)
    lanes["dmr"] = DigitalLane("dmr", modules["dmr"], target.xlxd_sut, "dmr", ("dmr",), "dmr", multiple=3)
    lanes["ysf"] = DigitalLane("ysf", modules["ysf"], target.xlxd_sut, "ysf", ("ysf",), 2)
    lanes["m17"] = DigitalLane("m17", modules["m17"], target.xlxd_sut, "m17", ("m17",), "m17")
    if env.get("DVT_NXDN") == "on" and env.get("DVT_NXDN_SUT"):
        lanes["nxdn"] = NxdnLane("nxdn", modules["nxdn"], target.xlxd_sut, env["DVT_NXDN_SUT"], int(env.get("DVT_NXDN_TG", "31672")))
    else:
        skipped["nxdn"] = "no NXDN reflector for the sut (run with --profile nxdn and name the peer in the native template)"
    if env.get("DVT_P25") == "on" and env.get("DVT_P25_SUT"):
        lanes["p25"] = P25Lane("p25", modules["p25"], target.xlxd_sut, env["DVT_P25_SUT"], int(env.get("DVT_P25_TG", "31672")))
    else:
        skipped["p25"] = "no P25 reflector for the sut (run with --profile p25 and name the peer in the native template)"
    return lanes, skipped


class Traffic:
    """Runs a schedule of overs (soak.build_overs, t in seconds after `t0` monotonic) on a pool; `stop` ends it."""

    def __init__(self, lanes, overs, t0, seed, workers=16):
        self.lanes, self.overs, self.t0, self.seed = lanes, overs, t0, seed
        self.stop = threading.Event()
        self.results = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="over")
        self._futures = []
        self.thread = threading.Thread(target=self._run, name="soak-traffic", daemon=True)

    def start(self):
        self.thread.start()

    def _one(self, over, index):
        began = time.time()
        res = self.lanes[over["lane"]].over(over["dur_s"], "%s/%d" % (self.seed, index))
        res.update(t_plan=over["t"], t_start=round(began, 2), t_end=round(time.time(), 2))
        with self._lock:
            self.results.append(res)

    def _run(self):
        for index, over in enumerate(self.overs):
            if over["lane"] not in self.lanes:
                continue
            if self.stop.wait(max(0.0, self.t0 + over["t"] - time.monotonic())):
                break
            self._futures.append(self._pool.submit(self._one, over, index))

    def finish(self, timeout=90.0):
        """Stop scheduling, let the overs in flight end. True when all ended inside `timeout`."""
        self.stop.set()
        self.thread.join(timeout)
        end = time.monotonic() + timeout
        for fut in list(self._futures):
            try:
                fut.result(max(0.1, end - time.monotonic()))
            except Exception:                                         # noqa: BLE001 - reported through the results
                pass
        self._pool.shutdown(wait=False, cancel_futures=True)
        return all(f.done() for f in self._futures)

    def summary(self):
        """{lane: {"overs", "ok", "errors": [...], "heard_min", "heard_mean"}} of the overs done so far."""
        out = {}
        with self._lock:
            results = list(self.results)
        for r in results:
            lane = out.setdefault(r["lane"], {"overs": 0, "ok": 0, "errors": {}, "heard": []})
            lane["overs"] += 1
            lane["ok"] += 1 if r["ok"] else 0
            if r.get("error"):
                lane["errors"][r["error"][:80]] = lane["errors"].get(r["error"][:80], 0) + 1
            if r.get("heard"):
                lane["heard"].append(min(r["heard"].values()))
        for lane in out.values():
            heard = lane.pop("heard")
            lane["heard_min"] = min(heard) if heard else None
            lane["heard_mean"] = round(sum(heard) / len(heard), 1) if heard else None
            lane["errors"] = dict(sorted(lane["errors"].items(), key=lambda kv: -kv[1])[:3])
        return dict(sorted(out.items()))


def lane_checks(summary, required=REQUIRED_LANES):
    """Checks of the protocol mix itself: every required lane that was scheduled must have had overs, and most of
    them must have been delivered (the USRP lanes are judged by the listeners: ok means the talk was sent)."""
    checks = []
    for name in required:
        info = summary.get(name)
        if info is None:
            checks.append(soak.Check("traffic:" + name, False, "no over was made"))
            continue
        good = info["ok"] >= 0.5 * info["overs"] and info["overs"] >= 5
        checks.append(soak.Check("traffic:" + name, good, "%d overs, %d delivered, errors %s" % (info["overs"], info["ok"], info["errors"] or "none")))
    return checks


def getenv_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default
