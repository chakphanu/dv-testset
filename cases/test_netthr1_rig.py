# SPDX-License-Identifier: MIT
"""NETTHR1a/b rig cases (DV-Router fce97cd3 + c8f9b4ed, image wip-eeb44b4; docs/netthr1-cases.md has the commands, the times and the GAPs). Written offline,
NOT RUN. Every case needs DVT_NETTHR1=on (a skip naming the switch otherwise). Black box: the hub metrics and `proc-stat` through the control channel, HTTP
to the web and admin services, the rendered dv-router.toml of the run (edited in place for G3 / G4 / D1 like the echo cases) and UDP DNS to the tester.

  G1  gauge shape            dv_fabric_sessions{kind=open|cap|peak}: exactly the three kinds, open <= peak <= cap, cap = the derived default of the rendered config,
                             open covers every child of the launcher
  G2  peak persists          a child is killed and respawned: open is back, peak did not fall, cap did not move
  G4  fitting reload         two more [[data.worker]] rows fit the running cap: both spawn, the running cap stays (a cap change is a restart)
  G3  reload over the cap    forty more rows do not fit: nothing is spawned, the cap stays (the refusal text is looked for in the admin status)
  H0  capacity counters      dv_web_capacity_refused_total{reason=login_slots|file_streams} exist for web and admin from the start (zero)
  H1  stream cap (web, admin) a streamed body > 8 MiB holds one slot; the next one is 503 + Retry-After 1 + no-store; small bodies and HEAD still answer;
                             the counter moves by the refusals; closing the first frees the slot
  H2  login slots (web)      concurrent logins beyond the slots wait 0.5 s then 503 + Retry-After 1; counted exactly; no lockout
  H3  login slots (admin)    the same on the admin service (password only: TotpRequired, not a lockout failure but it hashes and takes rate-limit tokens)
  H4  login time (ask b)     median login time, median of a request that hashes nothing, their difference = one argon2 hash as the rig sees it
  T1  thread ceilings idle   proc-stat threads of every process, N sweeps, against the documented ceilings; the headline 251 / 95 / 101 when the process set
                             is the documented one; no restart
  T2  thread ceilings loaded the same under logins and held streams (http-caps run): web and admin stay under the ceiling of THEIR configured caps
  T3  pids limit at ceiling  only for a run with DVT_PIDS_SUT at the ceiling: nothing restarted under the cap (ask a)
  D1  dns cancel at 5 s      (DVT_NETTHR1_DNS=on, DVT_SUT_RESOLV=30) two lookups that never answer are cancelled at 5 s each, the queued rows follow at 5 s and
                             10 s, the answering row connects, the edge has at most 4 + 2 threads

Differences from the documents that the specification does not settle are FINDING lines in out/<run>/netthr1.txt, never assertions.
"""
import http.client
import json
import os
import socket
import threading
import time
import tomllib
from pathlib import Path

import pytest

from dvtest import control as ctl
from dvtest import dnshole, metrics, netthr1, web, webload
from echo_support import EchoCfg, wait_reload

pytestmark = pytest.mark.rig

SERVICE = "xlxd-sut"
SWEEPS = int(os.environ.get("DVT_NETTHR1_SWEEPS") or 4)
SWEEP_GAP_S = 5.0
GAUGE_WAIT_S = 90.0                   # the launcher publishes every 10 s and the hub caches
BIG_BYTES = 40 * 1024 * 1024          # sparse; above the 8 MiB that takes a reader slot
PROBE = "/api/v1/public/state"        # answers without a hash and without a reader slot
CAPACITY = "dv_web_capacity_refused_total"
WEB_PORT, ADMIN_PORT = 8081, 8080     # rig/config/dv-router/web.toml / admin.toml

NOTES = []


@pytest.fixture(scope="module", autouse=True)
def gate():
    if not netthr1.on():
        pytest.skip(netthr1.skip_reason())


def note(target, case, tag, text):
    line = "[%s] NETTHR1 %s: %s" % (tag, case, text)
    print(line)
    NOTES.append(line)
    try:
        with open(Path(target.out) / "netthr1.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def save_json(target, name, data):
    try:
        path = Path(target.out) / "web" if (Path(target.out) / "web").is_dir() else Path(target.out)
        (path / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


@pytest.fixture(scope="module")
def rig_control(target):
    """Overrides the session fixture of cases/conftest.py, which depends on `rig` and so on a DExtra ACK of the SUT: the netthr1-router / netthr1-hotspot
    templates have no DExtra edge (first rig run: every case ERRORed with "does not ACK a DExtra link")."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        pytest.skip("no rig control channel (DVT_RIG_CONTROL=1 is set by scripts/run.sh)")
    return ctl.RigControl(Path(target.out) / "control")


@pytest.fixture(scope="module")
def sut(target):
    if target.sut_profile != "dvrouter":
        pytest.skip("NETTHR1 is DV-Router's: DVT_SUT_PROFILE=dvrouter")
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("NETTHR1 reads the rendered TOML: DVT_SUT_LAUNCH=native")
    cfg = EchoCfg.locate(target)
    text = cfg.original if cfg else None
    try:
        doc = tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError:
        doc = {}
    return {"cfg": cfg, "text": text, "doc": doc, "ip": target.xlxd_sut}


# ---------------------------------------------------------------- gauge and counters from the hub

def hub_samples(control):
    return metrics.parse(control.metrics(SERVICE))


def read_gauge(control):
    return netthr1.sessions_from_samples(hub_samples(control))


def poll(read, ok, budget_s, step_s=4.0):
    """(value, True) at the first read that satisfies ok, else (last value, False)."""
    end = time.monotonic() + budget_s
    value = None
    while True:
        try:
            value = read()
        except (RuntimeError, TimeoutError, OSError, ValueError):
            value = None
        if value is not None and ok(value):
            return value, True
        if time.monotonic() > end:
            return value, False
        time.sleep(step_s)


def settled(read, step_s=11.0, budget_s=90.0):
    """The first value that two reads one telemetry period apart agree on (the hub shows a record up to ~10 s old)."""
    end = time.monotonic() + budget_s
    last = None
    while True:
        value = read()
        if value == last:
            return value
        last = value
        if time.monotonic() > end:
            return value
        time.sleep(step_s)


def service_of(labels):
    """'web' or 'admin' from the hub's `proc` label (the family itself has only `reason`; DV-Router answer 7: telemetry/web/dv-svc-web vs
    telemetry/admin/dv-svc-admin), else None."""
    proc = str(labels.get("proc", ""))
    for name in ("admin", "web"):
        if proc == "dv-svc-" + name:
            return name
    return None


def capacity(samples):
    """{(service, reason): value}; services that cannot be told apart are under None."""
    out = {}
    for s in samples:
        if s.name == CAPACITY:
            out[(service_of(s.labels), s.labels.get("reason"))] = s.value
    return out


# ---------------------------------------------------------------- processes

def launcher_children(procs):
    """[(pid, comm)] of the direct children of dv-router (every one of them holds one fabric session)."""
    root = [p for p, _pp, c in procs if c == "dv-router"]
    return [(p, c) for p, pp, c in procs if root and pp == root[0] and c.startswith("dv-")]


def take_sweep(control):
    """([{comm, pid, start, threads}], [comm not readable]) over every allow-listed process name in `list`."""
    procs = control.proc_list()
    comms = sorted({c for _p, _pp, c in procs})
    rows, unreadable = [], []
    for comm in comms:
        if not ctl.proc_stat_allowed(SERVICE, comm):
            unreadable.append(comm)
            continue
        try:
            text = control.proc_stat(SERVICE, comm)
        except RuntimeError:                      # it ended between the list and the read
            continue
        for s in webload.parse_procstat_all(text):
            rows.append({"comm": s.comm, "pid": s.pid, "start": s.starttime, "threads": s.threads})
    return rows, unreadable


def ceilings_for(sut, cap, comms):
    doc = sut["doc"]
    web_http, admin_http = netthr1.http_settings(doc)
    dns = netthr1.hostname_edges(doc)
    return {c: netthr1.process_ceiling(c, web=web_http, admin=admin_http, cap=cap, dns_edges=dns) for c in comms}


# ================================================================ G: the gauge

def test_g1_gauge_shape_and_derived_cap(rig_control, sut, target):
    kinds, ok = poll(lambda: read_gauge(rig_control), lambda k: bool(k), GAUGE_WAIT_S)
    note(target, "G1", "RECORD", "gauge %s" % kinds)
    assert ok, "dv_fabric_sessions is not exposed within %d s (an image without NETTHR1a?)" % GAUGE_WAIT_S
    problems = netthr1.gauge_problems(kinds)
    assert not problems, "; ".join(problems)
    if sut["doc"]:
        children_now = launcher_children(rig_control.proc_list())
        has_row = bool((sut["doc"].get("transcoder", {}) or {}).get("worker"))
        argv_worker = 1 if (not has_row and any(c == netthr1.CODEC for _p, c in children_now)) else 0
        want = netthr1.derived_cap(sut["doc"], argv_workers=argv_worker)
        note(target, "G1", "RECORD", "derived cap %d (the transcoder address of the launch adds a worker row the file does not hold: %d)" % (want, argv_worker))
        assert kinds["cap"] == want, "cap %d, the formula over the rendered config gives %d (argv worker %d)" % (kinds["cap"], want, argv_worker)
    else:
        note(target, "G1", "RECORD", "rendered config not visible: the derived cap is not compared")
    children = launcher_children(rig_control.proc_list())
    note(target, "G1", "RECORD", "%d children of the launcher, open %d, cap %d, peak %d" % (len(children), kinds["open"], kinds["cap"], kinds["peak"]))
    assert kinds["open"] >= len(children) - 1, "open %d is below the %d children (a child may still be starting: one is allowed)" % (kinds["open"], len(children))
    assert kinds["open"] <= kinds["cap"]
    if kinds["open"] > len(children) + 2:
        note(target, "G1", "FINDING", "open %d is more than two above the %d children of the launcher (tool sessions are expected to be few)" % (kinds["open"], len(children)))


def test_g1b_status_body_carries_the_same_gauge(rig_control, admin_session, target):
    """DV-Router answer 6: GET /api/v1/admin/status has `launcher.fabric.sessions` (admin/config/status) and `live.fabric.sessions` (the hub summary);
    the public state does not expose it. Null when the launcher status is not available yet (polled)."""
    client = admin_session("viewer")

    def read():
        body = client.get(client.path("/status"), expect=200).json()
        launcher = ((body.get("launcher") or {}).get("fabric") or {}).get("sessions")
        live = ((body.get("live") or {}).get("fabric") or {}).get("sessions")
        return {"launcher": launcher, "live": live, "any": netthr1.find_sessions(body)}
    got, ok = poll(read, lambda g: g["launcher"] is not None and g["live"] is not None, 60.0)
    kinds = read_gauge(rig_control)
    note(target, "G1b", "RECORD", "launcher.fabric.sessions %s, live.fabric.sessions %s, hub metrics %s" % (got and got["launcher"], got and got["live"], kinds))
    assert ok, "launcher.fabric.sessions / live.fabric.sessions missing in the admin status body (found anywhere: %s)" % (got and got["any"])
    for name in ("launcher", "live"):
        assert {"open", "cap", "peak"} <= set(got[name]), "%s.fabric.sessions has %s" % (name, sorted(got[name]))
        assert got[name]["cap"] == kinds["cap"], "%s cap %s, hub metrics cap %s" % (name, got[name]["cap"], kinds["cap"])
        assert got[name]["peak"] >= got[name]["open"]
    public = web.WebClient(os.environ.get("DVT_WEB_URL") or "https://dvt-web", os.environ.get("DVT_WEB_CA") or str(Path(target.out) / "web" / web.CA_FILE), timeout=10)
    try:
        state = public.get("/api/v1/public/state", pace=False, expect=200).json()
        if netthr1.find_sessions(state) is not None:
            note(target, "G1b", "FINDING", "the PUBLIC state exposes fabric.sessions (the public allowlist omits it)")
    except (web.WebError, OSError, ValueError) as exc:
        note(target, "G1b", "RECORD", "public state not read: %s" % type(exc).__name__)


def test_g2_peak_persists_and_open_recovers(rig_control, target):
    before, ok = poll(lambda: read_gauge(rig_control), lambda k: bool(k), GAUGE_WAIT_S)
    assert ok, "no gauge"
    n_before = len(launcher_children(rig_control.proc_list()))
    victims = [pid for pid, comm in launcher_children(rig_control.proc_list()) if comm == "dv-edge-xlx"]
    if not victims:
        pytest.skip("no dv-edge-xlx child in this configuration (the kill-proc allow-list names dv-edge-xlx, nxdn, p25, ...)")
    rig_control.kill_proc(SERVICE, "dv-edge-xlx", "KILL")

    def back():
        procs = launcher_children(rig_control.proc_list())
        now = [pid for pid, comm in procs if comm == "dv-edge-xlx"]
        return (len(procs), now) if now and now != victims else None
    got, ok = poll(back, lambda v: v[0] == n_before, 60.0, step_s=3.0)
    assert ok, "dv-edge-xlx was not respawned within 60 s (children before %d, now %s)" % (n_before, got)
    after = settled(lambda: read_gauge(rig_control))
    note(target, "G2", "RECORD", "before %s, after the respawn %s" % (before, after))
    assert after["cap"] == before["cap"], "cap moved: %d -> %d" % (before["cap"], after["cap"])
    assert after["peak"] >= before["peak"], "peak fell: %d -> %d" % (before["peak"], after["peak"])
    assert after["open"] >= n_before - 1 and after["open"] <= after["cap"]
    assert abs(after["open"] - before["open"]) <= 1, "open %d -> %d after the respawn" % (before["open"], after["open"])


def _need_data_rows(sut):
    cfg = sut["cfg"]
    if cfg is None or not cfg.writable():
        pytest.skip("not reachable: the rendered config is not visible or not writable in out/ (G3 / G4 edit it in place)")
    if not (sut["doc"].get("data", {}) or {}).get("enable"):
        pytest.skip("[data] is not enabled in the rendered config: add rig/config/dv-router/data-ident.toml to DVT_SUT_CONFIG_EXTRA (extra [[data.worker]] rows are planned only then)")
    return cfg


def count_workers(control):
    return len([1 for _pid, comm in launcher_children(control.proc_list()) if comm == "dv-data-worker"])


def test_g4_fitting_reload_spawns_and_keeps_the_running_cap(rig_control, sut, target):
    cfg = _need_data_rows(sut)
    before_gauge = settled(lambda: read_gauge(rig_control))
    before = count_workers(rig_control)
    implicit = None if (sut["doc"].get("data", {}) or {}).get("worker") else "pkt"        # explicit rows replace the implicit default row
    plan = netthr1.add_rows(cfg.original, netthr1.data_worker_rows(2, "fit", first=implicit))
    want_cap_after_restart = netthr1.derived_cap(tomllib.loads(plan))
    try:
        cfg.write(plan)
        wait_reload(extra=3.0)
        got, ok = poll(lambda: count_workers(rig_control), lambda n: n >= before + 2, 45.0, step_s=3.0)
        after_gauge = settled(lambda: read_gauge(rig_control))
        note(target, "G4", "RECORD", "data workers %d -> %s; cap %d -> %d (a restart would resolve %d); open %d -> %d" % (
            before, got, before_gauge["cap"], after_gauge["cap"], want_cap_after_restart, before_gauge["open"], after_gauge["open"]))
        assert ok, "two fitting [[data.worker]] rows spawned %s new workers within 45 s (before %d)" % (None if got is None else got - before, before)
        assert after_gauge["cap"] == before_gauge["cap"], "the running cap moved %d -> %d on a reload (node.fabric_max_sessions is a restart key)" % (
            before_gauge["cap"], after_gauge["cap"])
        assert after_gauge["open"] >= before_gauge["open"] + 1, "open did not grow with two new children: %s -> %s" % (before_gauge, after_gauge)
        assert after_gauge["peak"] >= after_gauge["open"]
    finally:
        cfg.restore()
        wait_reload(extra=3.0)


def test_g3_reload_over_the_running_cap_is_refused(rig_control, sut, target, request):
    cfg = _need_data_rows(sut)
    before_gauge = settled(lambda: read_gauge(rig_control))
    before = count_workers(rig_control)
    rows = int(before_gauge["cap"]) + 5                                 # more extra children than the whole cap holds
    implicit = None if (sut["doc"].get("data", {}) or {}).get("worker") else "pkt"
    plan = netthr1.add_rows(cfg.original, netthr1.data_worker_rows(rows, "big", first=implicit))
    refusal = None
    try:
        cfg.write(plan)
        wait_reload(extra=3.0)
        time.sleep(6.0)
        after = count_workers(rig_control)
        after_gauge = settled(lambda: read_gauge(rig_control))
        try:
            admin = request.getfixturevalue("admin_session")("viewer")
            body = admin.get(admin.path("/status"), expect=200).json()
            text = json.dumps(body)
            refusal = "required for" in text and "planned children plus 1 reserved session" in text
        except (Exception, pytest.skip.Exception) as exc:               # noqa: BLE001 - the admin API is a second witness, not the case (a skip of its fixture must not end G3)
            note(target, "G3", "RECORD", "admin status not read: %s" % type(exc).__name__)
        note(target, "G3", "RECORD", "%d extra rows at cap %d: data workers %d -> %d, gauge %s -> %s, refusal text in the admin status: %s" % (
            rows, before_gauge["cap"], before, after, before_gauge, after_gauge, refusal))
        assert after == before, "an over-cap plan spawned %d new data workers (the reload must be invalid and spawn no over-cap child)" % (after - before)
        assert after_gauge["cap"] == before_gauge["cap"] and after_gauge["peak"] == before_gauge["peak"], "cap or peak moved: %s -> %s" % (before_gauge, after_gauge)
        if refusal is False:
            note(target, "G3", "FINDING", "the admin status shows no `cap N is below M required for K planned children plus 1 reserved session` error after the refused reload")
    finally:
        cfg.restore()
        wait_reload(extra=3.0)


# ================================================================ H: HTTP admission caps

def test_h0_capacity_counters_exist_from_the_start(rig_control, target):
    got, ok = poll(lambda: capacity(hub_samples(rig_control)), lambda c: len(c) >= 2, GAUGE_WAIT_S)
    note(target, "H0", "RECORD", "%s series: %s" % (CAPACITY, got))
    assert ok and got, "%s is not exposed (an image without NETTHR1b?)" % CAPACITY
    reasons = {r for (_svc, r) in got}
    assert reasons == {"login_slots", "file_streams"}, "reasons %s, want exactly login_slots and file_streams" % sorted(str(r) for r in reasons)
    services = {s for (s, _r) in got}
    if services != {"web", "admin"}:
        note(target, "H0", "FINDING", "services seen in the labels: %s (web and admin both expected; the hub label that tells them apart was not recognised)" % sorted(str(s) for s in services))
    assert all(v >= 0 for v in got.values())


def http_head(ip, port, path, host):
    conn = http.client.HTTPConnection(ip, port, timeout=10)
    try:
        conn.request("HEAD", path, headers={"Host": host, "Connection": "close"})
        resp = conn.getresponse()
        resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


class Hold:
    """A GET of a large file whose body is NOT read: the server's reader keeps its slot while the socket buffers are full."""

    def __init__(self, ip, port, path, host, rcvbuf=4096):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(10)
        self.sock.connect((ip, port))
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nAccept: */*\r\nConnection: close\r\n\r\n" % (path, host)).encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 16384:
            chunk = self.sock.recv(512)
            if not chunk:
                break
            head += chunk
        first, _, rest = head.partition(b"\r\n")
        try:
            self.status = int(first.split()[1])
        except (IndexError, ValueError):
            self.status = 0
        raw, _, self.body = rest.partition(b"\r\n\r\n")
        self.headers = {}
        for line in raw.split(b"\r\n"):
            k, _, v = line.partition(b":")
            self.headers[k.strip().decode("latin-1").lower()] = v.strip().decode("latin-1")

    def drain_small(self):
        """The rest of a short answer (a 503 body)."""
        self.sock.settimeout(3)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                self.body += chunk
        except (socket.timeout, OSError):
            pass
        return self.body

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def make_big_files(target):
    base = Path(target.out) / "sut-state" / "netthr1-static"
    try:
        for sub in ("shared", "admin"):
            (base / sub).mkdir(parents=True, exist_ok=True)
            with open(base / sub / "netthr1-big.bin", "wb") as fh:
                fh.truncate(BIG_BYTES)
    except OSError as exc:
        pytest.skip("cannot write the static tree %s: %s (the run needs --profile web and the same uid as the SUT)" % (base, exc))


STREAM_TARGETS = {
    "web": dict(port=WEB_PORT, host="dvt-web", paths=["/shared/netthr1-big.bin"], cap=1),
    "admin": dict(port=ADMIN_PORT, host="dvt-admin:8443", paths=["/netthr1-big.bin", "/shared/netthr1-big.bin"], cap=2),
}


def configured_cap(sut, service, key):
    web_http, admin_http = netthr1.http_settings(sut["doc"])
    pair = web_http if service == "web" else admin_http
    return pair[0] if key == "streams" else pair[1]


@pytest.mark.parametrize("service", ["web", "admin"])
def test_h1_stream_cap(service, rig_control, sut, target):
    spec = STREAM_TARGETS[service]
    cap = configured_cap(sut, service, "streams")
    if cap > 8:
        pytest.skip("%s.max_file_streams = %d: this case holds one body per slot; name rig/config/dv-router/netthr1-http-caps.toml" % (service, cap))
    make_big_files(target)
    ip = sut["ip"]
    path = None
    for candidate in spec["paths"]:
        try:
            status, headers = http_head(ip, spec["port"], candidate, spec["host"])
        except OSError as exc:
            pytest.skip("%s service not reachable at %s:%d: %s" % (service, ip, spec["port"], exc))
        if status == 200 and int(headers.get("content-length", 0)) >= BIG_BYTES:
            path = candidate
            break
    if path is None:
        pytest.skip("the %s service does not serve the large file under %s (static_dir not applied: netthr1-http-caps.toml in DVT_SUT_CONFIG_EXTRA?)" % (service, spec["paths"]))
    before = settled(lambda: capacity(hub_samples(rig_control)))
    holds, refused = [], []
    try:
        for _ in range(cap):
            h = Hold(ip, spec["port"], path, spec["host"])
            holds.append(h)
            assert h.status in (200, 206), "stream %d of %d: HTTP %s" % (len(holds), cap, h.status)
        extra = [Hold(ip, spec["port"], path, spec["host"]) for _ in range(2)]
        refused = extra
        for h in extra:
            h.drain_small()
            assert h.status == 503, "%s: stream %d beyond the cap %d answered HTTP %s, want 503" % (service, cap + 1, cap, h.status)
            assert h.headers.get("retry-after") == "1", "Retry-After %r, want 1" % h.headers.get("retry-after")
            assert "no-store" in h.headers.get("cache-control", ""), "Cache-Control %r, want no-store" % h.headers.get("cache-control")
        # a HEAD of the same file and a small body use no reader slot
        status, _ = http_head(ip, spec["port"], path, spec["host"])
        assert status == 200, "HEAD of the large file while the slots are full: HTTP %s" % status
        # freeing a slot makes room again
        holds.pop().close()
        time.sleep(2.0)
        again = Hold(ip, spec["port"], path, spec["host"])
        holds.append(again)
        assert again.status in (200, 206), "after closing one stream the next one answered HTTP %s" % again.status
    finally:
        for h in holds + refused:
            h.close()
    time.sleep(1.0)
    after = settled(lambda: capacity(hub_samples(rig_control)))
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    note(target, "H1-%s" % service, "RECORD", "cap %d, 2 refusals made; counter deltas %s" % (cap, delta))
    mine = delta.get((service, "file_streams"))
    if mine is None and (None, "file_streams") in delta:
        note(target, "H1-%s" % service, "FINDING", "the hub labels do not tell web from admin; the unlabelled file_streams delta is %s" % delta[(None, "file_streams")])
        mine = delta[(None, "file_streams")]
    assert mine == 2, "dv_web_capacity_refused_total{reason=file_streams} moved by %r for %s, the case made exactly 2 refusals" % (mine, service)
    assert delta.get((service, "login_slots"), 0) == 0


# ---------------------------------------------------------------- logins

def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, (time.monotonic() - t0) * 1000.0


def login_latency(base_url, ca, callsign, password, n=7):
    """(median login ms, median plain GET ms, [login ms]) with fresh connections each time; logins are paced by the client's own budget."""
    logins, plains = [], []
    for _ in range(n):
        c = web.WebClient(base_url, ca)
        c.hide(password)
        _, ms = timed(lambda: c.request("POST", "/api/v1/auth/session", json_body={"callsign": callsign, "password": password}, expect=200))
        logins.append(ms)
        c2 = web.WebClient(base_url, ca)
        _, ms2 = timed(lambda: c2.request("GET", PROBE, pace=False, expect=200))
        plains.append(ms2)
    return netthr1.median(logins), netthr1.median(plains), logins


def burst(n, one):
    """n threads released together; list of (status, retry_after, ms)."""
    gate_ = threading.Barrier(n)
    out = [None] * n

    def run(i):
        gate_.wait()
        t0 = time.monotonic()
        try:
            resp = one(i)
            out[i] = (resp.status, resp.headers.get("retry-after"), (time.monotonic() - t0) * 1000.0)
        except web.WebError as exc:
            out[i] = (exc.status or 0, None, (time.monotonic() - t0) * 1000.0)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return [r for r in out if r is not None]


def tally(results):
    counts = {}
    for status, _ra, _ms in results:
        counts[status] = counts.get(status, 0) + 1
    return counts


def test_h4_login_time_is_recorded(web_user, web_base_url, web_ca, target):
    """Ask (b): one argon2 login hash as the rig sees it. The cpu cap of the SUT is DVT_CPUS_SUT (docs/netthr1-cases.md: 0.25 for a small board)."""
    user = web_user()
    login_ms, plain_ms, all_ms = login_latency(web_base_url, web_ca, user.callsign, user.password)
    hash_ms = netthr1.hash_estimate_ms(login_ms, plain_ms)
    record = {"cpus_sut": os.environ.get("DVT_CPUS_SUT") or "unset", "login_ms_median": round(login_ms, 1), "plain_ms_median": round(plain_ms, 1),
              "hash_ms_estimate": round(hash_ms, 1), "login_ms": [round(x, 1) for x in all_ms], "login_slots_web": None}
    save_json(target, "netthr1-login-time.json", record)
    note(target, "H4", "RECORD", "SUT cpus %s: login %.0f ms, plain request %.0f ms, one hash about %.0f ms (default-cost argon2: 19 MiB)" % (
        record["cpus_sut"], login_ms, plain_ms, hash_ms))
    assert login_ms > plain_ms, "a login (%.0f ms) is not slower than a plain request (%.0f ms): no hash measured" % (login_ms, plain_ms)
    assert login_ms < 10000.0, "median login %.0f ms" % login_ms


@pytest.mark.parametrize("service", ["web", "admin"])
def test_h2_login_slots(service, rig_control, sut, target, request):
    slots = configured_cap(sut, service, "slots")
    if slots > 4:
        pytest.skip("%s.login_slots = %d: name rig/config/dv-router/netthr1-http-caps.toml (1 for web and admin)" % (service, slots))
    before = settled(lambda: capacity(hub_samples(rig_control)))
    if service == "web":
        user = request.getfixturevalue("web_user")()
        base, ca = request.getfixturevalue("web_base_url"), request.getfixturevalue("web_ca")
        login_ms, plain_ms, _ = login_latency(base, ca, user.callsign, user.password, n=5)

        def one(_i):
            c = web.WebClient(base, ca)
            c.hide(user.password)
            return c.request("POST", "/api/v1/auth/session", json_body={"callsign": user.callsign, "password": user.password}, pace=False)

        def after_ok():
            c = web.WebClient(base, ca)
            c.hide(user.password)
            return c.request("POST", "/api/v1/auth/session", json_body={"callsign": user.callsign, "password": user.password}, pace=False)
    else:
        admin = request.getfixturevalue("admin_session")
        admin("viewer")
        user_name, password = admin.credentials("viewer")
        url, ca = request.getfixturevalue("web_url_admin"), request.getfixturevalue("web_ca")
        from dvtest.admin import AdminClient
        probe = AdminClient(url, ca)
        _, plain_ms = timed(lambda: probe.request("GET", probe.path("/health"), pace=False, expect=200))
        c0 = AdminClient(url, ca)
        c0.hide(password)
        _, login_ms = timed(lambda: c0.request("POST", c0.path("/session"), json_body={"user": user_name, "password": password}))   # paced by the Pacer

        def one(_i):
            c = AdminClient(url, ca)
            c.hide(password)
            return c.request("POST", c.path("/session"), json_body={"user": user_name, "password": password}, pace=False)
        def after_ok():
            return one(0)
    hash_ms = netthr1.hash_estimate_ms(login_ms, plain_ms)
    need = netthr1.concurrency_needed(hash_ms, 500.0, slots)
    n = 9                                                           # the per-address login burst is 10: nine leave one token spare
    note(target, "H2-%s" % service, "RECORD", "slots %d, login %.0f ms, plain %.0f ms, hash about %.0f ms: %d concurrent logins are needed for a 503, %d are sent (SUT cpus %s)" % (
        slots, login_ms, plain_ms, hash_ms, need, n, os.environ.get("DVT_CPUS_SUT") or "unset"))
    time.sleep(12.0)                                                # the login buckets (1 per s, burst 10) refill
    results = burst(n, one)
    for _ in range(n):                                              # the burst took n tokens of the per-address and global login limits: charge the client's own model too
        web.PACER.wait("login")
    counts = tally(results)
    refused = [r for r in results if r[0] == 503]
    note(target, "H2-%s" % service, "RECORD", "statuses %s, %d refused, slowest %.0f ms" % (counts, len(refused), max(r[2] for r in results)))
    # admin: a correct password without a TOTP code on an enrolled account is TotpRequired (DV-Router answer 12): it hashes (uses a slot), takes a token of the
    # per-address and global login limits, and is NOT a failure toward the (account, address) lockout; a wrong code would count, so none is sent
    allowed = {200, 400, 401, 403, 429, 503} if service == "admin" else {200, 429, 503}
    assert set(counts) <= allowed, "unexpected statuses %s (allowed %s)" % (counts, sorted(allowed))
    for status, retry, _ms in refused:
        assert retry == "1", "a 503 carried Retry-After %r, want 1" % retry
    if need > n and not refused:
        after = settled(lambda: capacity(hub_samples(rig_control)))
        delta = after.get((service, "login_slots"), 0) - before.get((service, "login_slots"), 0)
        assert delta == len(refused), "login_slots counter moved by %d, %d refusals seen" % (delta, len(refused))
        pytest.skip("one hash is about %.0f ms: %d concurrent logins are needed to pass the 0.5 s wait and the burst holds %d; run with DVT_CPUS_SUT=0.25 (docs/netthr1-cases.md)" % (hash_ms, need, n))
    assert refused, "%d concurrent logins on %d slot(s), each hash about %.0f ms: not one was refused (statuses %s)" % (n, slots, hash_ms, counts)
    after = settled(lambda: capacity(hub_samples(rig_control)))
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    mine = delta.get((service, "login_slots"))
    if mine is None and (None, "login_slots") in delta:
        mine = delta[(None, "login_slots")]
        note(target, "H2-%s" % service, "FINDING", "the hub labels do not tell web from admin; the unlabelled login_slots delta is %s" % mine)
    assert mine == len(refused), "dv_web_capacity_refused_total{reason=login_slots} moved by %r, the burst saw %d refusals" % (mine, len(refused))
    assert delta.get((service, "file_streams"), 0) == 0
    time.sleep(2.0)
    last = after_ok()
    assert last.status in (200, 400, 401, 403, 429), "a login after the burst answered HTTP %s: a busy refusal must not lock the account" % last.status
    if service == "web":
        assert last.status != 401, "the correct password was refused after the burst"


# ================================================================ T: thread ceilings

def run_sweeps(control, n, gap_s):
    sweeps, unreadable = [], set()
    for i in range(n):
        rows, bad = take_sweep(control)
        sweeps.append(rows)
        unreadable.update(bad)
        if i + 1 < n:
            time.sleep(gap_s)
    return sweeps, sorted(unreadable)


def judge(sut, kinds, sweeps, unreadable, target, case):
    """Judge the sweeps against the documented ceilings. The codec worker the rig's transcoder address spawns (dv-codec-ambed) is in no derivation (answer 8,
    NETTHR1c): it is left out of the counts, the sums and the profile match, and its own thread count is recorded. The running cap includes its session."""
    cap = kinds["cap"] if kinds else netthr1.derived_cap(sut["doc"] or {"node": {"rooms": "A-Z"}})
    codec = [[dict(r) for r in sw if r["comm"] == netthr1.CODEC] for sw in sweeps]
    sweeps = [[r for r in sw if r["comm"] != netthr1.CODEC] for sw in sweeps]
    codec_peak = max([r["threads"] for sw in codec for r in sw] or [0])
    counts = netthr1.counts_of(sweeps[0])
    ceilings = ceilings_for(sut, cap, counts)
    profile = netthr1.profile_of(counts)
    web_http, admin_http = netthr1.http_settings(sut["doc"])
    dns = netthr1.hostname_edges(sut["doc"])
    expected, unknown = netthr1.expected_total(counts, web_http, admin_http, cap, dns)
    limit = expected if not unknown else None
    result = netthr1.analyse_sweeps(sweeps, ceilings, total_limit=limit)
    result["codec_peak"] = codec_peak
    note(target, case, "RECORD", "%s: %d process(es), peak %d threads each, in no budget and left out of the sums (NETTHR1c)" % (netthr1.CODEC, len(codec[0]), codec_peak))
    if profile:
        head = netthr1.headline(profile)
        documented_cap = netthr1.PROFILES[profile][3]
        note(target, case, "RECORD", "process set is the documented profile %s: headline ceiling %d at cap %d; at the running cap %d the per-process ceilings sum to %d" % (
            profile, head, documented_cap, cap, expected))
        assert expected == head + (cap - documented_cap), "the per-process ceilings sum to %d at cap %d, the document says %d at cap %d for %s" % (
            expected, cap, head, documented_cap, profile)
    else:
        note(target, case, "RECORD", "no documented profile matches this process set %s: the sum of the per-process ceilings is the limit (%s), the headline 251 / 95 / 101 does not apply" % (
            dict(sorted(counts.items())), expected))
    note(target, case, "RECORD", "peak total %d without the codec worker (lower bound of the true peak: sweeps are seconds apart), per process %s, unreadable %s, no ceiling for %s" % (
        result["peak_total"], dict(sorted(result["peak_by_comm"].items())), unreadable, result["unknown"]))
    save_json(target, "netthr1-threads-%s.json" % case, {"profile": profile, "cap": cap, "expected_total": expected, "result": result, "counts": counts,
                                                         "codec_peak": codec_peak, "pids_limit": os.environ.get("DVT_PIDS_SUT") or "unset",
                                                         "cpus_sut": os.environ.get("DVT_CPUS_SUT") or "unset"})
    return result, profile, expected


def test_t1_thread_ceilings_idle(rig_control, sut, target):
    kinds, _ = poll(lambda: read_gauge(rig_control), lambda k: bool(k), GAUGE_WAIT_S)
    time.sleep(10.0)                                                 # the start-up burst of tasks is over
    sweeps, unreadable = run_sweeps(rig_control, SWEEPS, SWEEP_GAP_S)
    result, profile, expected = judge(sut, kinds, sweeps, unreadable, target, "T1")
    assert not result["violations"], "; ".join(result["violations"])
    assert not result["restarts"], "a process restarted during the idle sweeps: %s" % "; ".join(result["restarts"])


def test_t2_thread_ceilings_under_load(rig_control, sut, target, request):
    """Logins at about one per second and held large streams while the sweeps run: web and admin must stay under the ceiling of the caps they are configured with
    (7 + login_slots + max_file_streams: 9 and 11 with netthr1-http-caps.toml), not under the old 27."""
    web_http, admin_http = netthr1.http_settings(sut["doc"])
    if web_http == netthr1.ROUTER_HTTP and not (sut["doc"].get("web", {}) or {}).get("max_file_streams"):
        pytest.skip("default HTTP caps: T2 needs rig/config/dv-router/netthr1-http-caps.toml (ceilings 9 and 11) to tell a configured cap from the old 27")
    user = request.getfixturevalue("web_user")()
    base, ca = request.getfixturevalue("web_base_url"), request.getfixturevalue("web_ca")
    make_big_files(target)
    kinds = read_gauge(rig_control)
    stop = threading.Event()
    holds = []

    def load():
        while not stop.is_set():
            try:
                c = web.WebClient(base, ca)
                c.hide(user.password)
                c.request("POST", "/api/v1/auth/session", json_body={"callsign": user.callsign, "password": user.password}, pace=False)
            except (web.WebError, OSError):
                pass
            stop.wait(1.1)
    th = threading.Thread(target=load, daemon=True)
    th.start()
    try:
        for service in ("web", "admin"):
            spec = STREAM_TARGETS[service]
            for path in spec["paths"]:
                try:
                    status, headers = http_head(sut["ip"], spec["port"], path, spec["host"])
                except OSError:
                    break
                if status == 200 and int(headers.get("content-length", 0)) >= BIG_BYTES:
                    for _ in range(configured_cap(sut, service, "streams")):
                        holds.append(Hold(sut["ip"], spec["port"], path, spec["host"]))
                    break
        sweeps, unreadable = run_sweeps(rig_control, max(SWEEPS, 3), 3.0)
    finally:
        stop.set()
        th.join(5)
        for h in holds:
            h.close()
    result, profile, expected = judge(sut, kinds, sweeps, unreadable, target, "T2")
    note(target, "T2", "RECORD", "streams held: %d; web ceiling %d, admin ceiling %d" % (len(holds), netthr1.http_ceiling(web_http[1], web_http[0]), netthr1.http_ceiling(admin_http[1], admin_http[0])))
    assert not result["violations"], "; ".join(result["violations"])
    assert not result["restarts"], "a process restarted under load: %s" % "; ".join(result["restarts"])
    assert result["peak_by_comm"].get("dv-svc-web", 0) <= netthr1.http_ceiling(web_http[1], web_http[0])
    assert result["peak_by_comm"].get("dv-svc-admin", 0) <= netthr1.http_ceiling(admin_http[1], admin_http[0])


def test_t3_nothing_restarts_under_a_pids_limit_at_the_ceiling(rig_control, sut, target):
    """Ask (a). The SUT container runs with DVT_PIDS_SUT = the documented total at the RUNNING cap (cap + 2 + the other ceilings; the transcoder address adds a
    session to the cap) + the codec worker's own threads (in no budget) + 1 for docker-init, + a spare of at most 2: a process that needs one thread more
    than its ceiling fails to spawn it and restarts (a new pid) or the launcher counts a restart. Skips unless the run is at the ceiling."""
    limit = os.environ.get("DVT_PIDS_SUT")
    if not limit or not limit.isdigit() or int(limit) >= 1024:
        pytest.skip("DVT_PIDS_SUT is %r: run with the pids limit at the ceiling (docs/netthr1-cases.md, T3)" % (limit,))
    kinds = read_gauge(rig_control)
    time.sleep(5.0)
    sweeps, unreadable = run_sweeps(rig_control, max(SWEEPS, 3), SWEEP_GAP_S)
    result, profile, expected = judge(sut, kinds, sweeps, unreadable, target, "T3")
    if result["unknown"]:
        pytest.skip("no documented ceiling for %s: the limit cannot be set at the ceiling" % result["unknown"])
    codec = result["codec_peak"]
    spare = int(limit) - expected - codec
    note(target, "T3", "RECORD", "pids limit %s, documented total %d at the running cap, codec worker %d, spare %d (docker-init takes 1)" % (limit, expected, codec, spare))
    if not 1 <= spare <= 3:
        pytest.skip("the limit %s is not at the ceiling %d + codec worker %d (+1 init, spare up to 3): the run proves nothing about the ceiling" % (limit, expected, codec))
    assert not result["violations"], "; ".join(result["violations"])
    assert not result["restarts"], "a process restarted under pids_limit %s (documented total %d + codec worker %d): %s" % (limit, expected, codec, "; ".join(result["restarts"]))
    assert result["peak_total"] + codec <= int(limit) - 1


# ================================================================ D1: the DNS cancel

HOLE_NAMES = ["hole%d.hole.dvt.test" % i for i in range(1, 5)]
OK_NAME = "ok1.ok.dvt.test"
PEER_PORT = 17013


def test_d1_dns_lookups_are_cancelled_at_5_s(rig_control, sut, target):
    if os.environ.get("DVT_NETTHR1_DNS") != "on":
        pytest.skip("DNS cancel case: DVT_NETTHR1_DNS=on with DVT_SUT_RESOLV=30 on the host (the SUT must resolve through the tester)")
    cfg = sut["cfg"]
    if cfg is None or not cfg.writable():
        pytest.skip("not reachable: the rendered config is not visible or not writable (D1 appends five M17 peer rows)")
    if not (sut["doc"].get("edges", {}).get("m17", {}) or {}).get("enable"):
        pytest.skip("edges.m17 is not enabled in the rendered config")
    try:
        hole = dnshole.Hole("0.0.0.0", 53, ip=target.tester)
    except OSError as exc:
        pytest.fail("cannot bind UDP 53 in the tester: %s" % exc)
    hole.start()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("0.0.0.0", PEER_PORT))
    peer.settimeout(0.2)
    arrivals = []
    stop = threading.Event()

    def listen():
        while not stop.is_set():
            try:
                data, addr = peer.recvfrom(2048)
                arrivals.append((time.monotonic(), addr[0], data[:4]))
            except socket.timeout:
                continue
            except OSError:
                return
    lt = threading.Thread(target=listen, daemon=True)
    lt.start()
    rows = [("M17-H%02d" % (i + 1), name, PEER_PORT) for i, name in enumerate(HOLE_NAMES)] + [("M17-OK1", OK_NAME, PEER_PORT)]
    samples = []
    sampler_stop = threading.Event()

    def sampler():
        while not sampler_stop.is_set():
            try:
                for s in webload.parse_procstat_all(rig_control.proc_stat(SERVICE, "dv-edge-m17")):
                    samples.append((time.monotonic(), s.pid, s.threads))
            except (RuntimeError, TimeoutError, OSError, ValueError):
                pass
            sampler_stop.wait(0.5)
    st = threading.Thread(target=sampler, daemon=True)
    try:
        st.start()
        cfg.write(netthr1.add_rows(cfg.original, netthr1.m17_rows(rows)))
        wait_reload(extra=0.0)
        time.sleep(30.0)
    finally:
        cfg.restore()
        sampler_stop.set()
        st.join(5)
        stop.set()
        lt.join(2)
        hole.stop()
        peer.close()
        wait_reload(extra=2.0)
    order = HOLE_NAMES + [OK_NAME]
    t0, rel = dnshole.schedule(hole.log, order)
    shown = {n: (None if r is None else round(r, 2)) for n, r in zip(order, rel)}
    top = max((t for _t, _p, t in samples), default=0)
    note(target, "D1", "RECORD", "first query of each name, seconds after the first: %s; dv-edge-m17 threads max %d over %d samples; sources %s; datagrams at the peer socket %d" % (
        shown, top, len(samples), sorted(hole.sources), len(arrivals)))
    save_json(target, "netthr1-dns.json", {"first_query_s": shown, "threads_max": top, "queries": [(round(t - (t0 or 0), 3), n, q) for t, n, q in hole.log][:200]})
    assert t0 is not None, "the SUT sent no query to the tester within the window: is DVT_SUT_RESOLV=30 set (the SUT's resolv.conf) and did the M17 edge restart?"
    assert all(r is not None for r in rel), "never asked: %s" % [n for n, r in zip(order, rel) if r is None]
    # two workers start two lookups at once; the next two start when those are cancelled (5 s); the answering row when those are (10 s)
    starts = sorted(rel[:4])
    assert starts[1] <= 1.0, "two lookups were not started together: first queries %s" % starts
    assert 4.6 <= starts[2] <= 5.9 and 4.6 <= starts[3] <= 5.9, "the third and fourth lookups started at %.2f and %.2f s, not when the first two were cancelled (4.6 to 5.9 s)" % (starts[2], starts[3])
    assert 9.6 <= rel[4] <= 11.2, "the answering row was asked at %.2f s, not when the second pair was cancelled (9.6 to 11.2 s)" % rel[4]
    assert top <= 6, "dv-edge-m17 had %d threads while lookups hung: ceiling 4 + 2 resolver workers" % top
    ok_ip_arrivals = [a for a in arrivals if a[1] == sut["ip"]]
    assert ok_ip_arrivals, "nothing reached the answering peer row (%s:%d) from the SUT after its name resolved" % (OK_NAME, PEER_PORT)
