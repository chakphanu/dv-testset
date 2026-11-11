# SPDX-License-Identifier: MIT
"""Web unit, stage 5: W8 load. 200 receive-only WebRTC listeners x 50 packets/s (20 ms PCMU) through the real HTTP
signalling relay of dv-svc-web (docs/web-load-cases.md). Needs `scripts/run.sh --profile web,webload` with
DVT_USRP_DYNAMIC=1 (the source is a USRP dynamic client per room), native launch.

The adopted gate (DV-Router docs/project/testing.md "Gates" and "Browser/NAT rig criteria"), judged by
dvtest.webload.evaluate_gates: edge process user+system CPU <= 25 percent of ONE core over a 60 s window that starts
5 s after the source starts; peak RSS (VmHWM) <= 64 MiB and growth over the pre-client startup <= 100 KiB per listener +
4 MiB; the WORST listener's jitter (absolute deviation of the spacing of successive unique authenticated RTP packets
from 20 ms, read where the client takes the datagram off the socket) p99 <= 20 ms and max <= 60 ms; zero lost,
duplicate, reordered or invalid packets; the source does not miss deadlines.

  a  test_calibration[1|10|50]  the generator checked at small sizes (its own floor of jitter and cpu); integrity asserted
  b  test_w8_load_200           the gate at 200 x 50 pps; JSON summary out/<run>/web/load.json
  c  test_flood_keeps_serving   a short bounded flood of the public API, the login/register buckets and the per-address
                                connection share while one probe listener keeps listening (MS9 limits, 429 behaviour)
  d  test_join_burst_counts_join_window
                                WEBSTALL1 (DVT_WEBSTALL1FIX=on and rig/config/dv-router/join-window.toml): a burst of joins from one
                                address inside the edge's join window is refused 503 and counted in both refusal families

Every load case also records the WebRTC refusal counters before and after the gate (`refusals` in load.json: dv_edge_webrtc_offer_refused_total
and dv_web_rtc_refused_total, by reason, never summed; dvtest/refusals.py). With DVT_WEBSTALL1FIX=on the run also FAILS when the families are not
fully exposed or a web request other than auth/register took longer than 1 s (the slow-request detector is then a failure, not only FINDING WEBSTALL1).

Every number goes into ONE file, out/<run>/web/load.json, rewritten after each case (aggregates and one compact row per
listener; no per-listener files, no packet logs). Differences from the documents are web.finding() lines.
"""
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from dvtest import metrics, refusals, web
from dvtest import webload as wl
import webload_support as ws
from webload_support import LoadGen, LoadGenError, Sampler, Source, generator_cpu
from stall_kills_support import check_end, stall_kills_start  # noqa: F401  (RFWD1 false-kill guard: autouse start read, last test)

pytestmark = [pytest.mark.rig, pytest.mark.web, pytest.mark.slow]

LISTENERS = "dv_edge_webrtc_listeners"
JOIN_REFUSED = "dv_edge_webrtc_join_refused_total"   # EDG2: {reason="core"}, zero from the first record
ROOMS = ("A", "B")
READY_WAIT_S = 180.0

RESULTS = {"schema": 1, "cases": {}}


# ------------------------------------------------------------------ fixtures

@pytest.fixture(scope="session")
def load_gen():
    if os.environ.get("DVT_WEBLOAD") != "on":
        pytest.skip("load generator not up: run scripts/run.sh --profile web,webload (the tester gets DVT_WEBLOAD=on)")
    gen = LoadGen(os.environ.get("DVT_WEBLOAD_URL") or "http://172.30.0.97:3100")
    deadline = time.monotonic() + 60
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            if gen.health():
                return gen
        except LoadGenError as exc:
            last = str(exc)
        time.sleep(1.0)
    pytest.fail("the load generator does not answer /health within 60 s (last: %s)" % last)


@pytest.fixture(scope="session")
def load_ready(web_base_url, web_ca, rig_control):
    """The web service is ready (RTC offers are refused with 503 until the journal recovery, 36 s after its start)
    and the hub knows the WebRTC edge."""
    client = web.WebClient(web_base_url, web_ca, timeout=5)
    end = time.monotonic() + READY_WAIT_S
    last = "?"
    while time.monotonic() < end:
        try:
            ready = client.get("/ready", pace=False).status
            fam = metrics.families(rig_control.metrics("xlxd-sut"))
            last = "ready %s, gauge %s" % (ready, LISTENERS in fam)
            if ready == 200 and LISTENERS in fam:
                return True
        except (web.WebError, RuntimeError, TimeoutError) as exc:
            last = str(exc)[:120]
        time.sleep(2.0)
    pytest.fail("web service or WebRTC edge not ready within %d s (%s)" % (READY_WAIT_S, last))


@pytest.fixture(scope="session")
def load_accounts(web_operator):
    """accounts(n) -> n accounts [(callsign, password)] registered through the operator (the register budget is
    0.2/s with at least 2 s between registrations: the shared Pacer waits). Cached for the session."""
    made = []

    def accounts(n):
        while len(made) < n:
            callsign = "N0LD%02d" % (len(made) + 1)
            password = web.new_password()
            web_operator.hide(password)
            web_operator.register_user(callsign, password, may_tx=False)
            made.append((callsign, password))
        return list(made[:n])

    return accounts


def write_results():
    path = Path(os.environ.get("DVT_OUT", "out")) / "web" / "load.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(RESULTS, indent=1, sort_keys=True) + "\n")
    except OSError as exc:
        web.finding("could not write %s: %s" % (path, exc))


def loadavg():
    try:
        return [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return None


def edge_listeners(rig_control):
    return metrics.total(metrics.parse(rig_control.metrics("xlxd-sut")), LISTENERS)


def join_refused(rig_control):
    """Sum of dv_edge_webrtc_join_refused_total{reason="core"}, None when the SUT does not export it (images
    before EDG2): then it is recorded as absent and not judged."""
    samples = metrics.parse(rig_control.metrics("xlxd-sut"))
    if not any(sample.name == JOIN_REFUSED for sample in samples):
        return None
    return metrics.total(samples, JOIN_REFUSED, reason="core")


def fix_problems(summary):
    """DVT_WEBSTALL1FIX=on: what the fix image must show in a load run (empty without the switch). The counter families are fully exposed and no web
    request other than auth/register took longer than 1 s (ws.slow_problems)."""
    if not refusals.fix_on():
        return []
    shape = (summary.get("refusals") or {}).get("shape_problems")
    out = ["the refusal counters: %s" % p for p in (["were not recorded"] if shape is None else shape)]
    out.extend("slow web request: %s" % p for p in ws.slow_problems(summary.get("slow_requests")))
    return out


def wait_gauge(rig_control, n, budget_s=45.0):
    """The edge publishes its listener gauge every few seconds and the hub caches it (web.md; K22 waited up to 35 s):
    poll until it equals n. Returns (last value, [values seen])."""
    end = time.monotonic() + budget_s
    seen = []
    while True:
        seen.append(round(edge_listeners(rig_control)))
        if seen[-1] == n or time.monotonic() >= end:
            return seen[-1], seen[-12:]
        time.sleep(3.0)


def wait_connected(load_gen, n, budget_s):
    """Poll the generator until every listener is connected or failed. Returns the last status."""
    end = time.monotonic() + budget_s
    st = load_gen.status()
    while time.monotonic() < end:
        st = load_gen.status()
        if st["state"] == "error" or st["connected"] + st["failed"] >= n:
            return st
        time.sleep(2.0)
    return st


def generator_observations(summary, load_gen, n):
    """The offer retries and the slow web requests the generator has seen so far (called after the connect phase and again after the run, so a
    run that is aborted before the window still reports them)."""
    try:
        st = load_gen.status()
    except Exception as exc:                                        # noqa: BLE001 - an observation, never the verdict
        summary["generator_observations_error"] = "%s: %s" % (type(exc).__name__, exc)
        return
    summary["offer_retries"] = ws.retry_stats(st, n)
    summary["slow_requests"] = ws.slow_stats(st, (summary.get("web_proc") or {}).get("series") or ())


def record_refusals(summary, before, rig_control):
    """`refusals` in load.json: both families by reason before the run and after the gate (settled reads), the reasons that moved and the shape
    problems of the exposition. The two families are never added together. Next to it what the generator saw: refusals it received (retries
    plus the offers that ran out of retries) and the detail texts of the retried ones (recorded, never judged)."""
    after, settled = refusals.settled(lambda: refusals.read(rig_control))
    summary["refusals"] = refusals.record(before, after)
    summary["refusals"]["settled"] = settled
    rt = summary.get("offer_retries") or {}
    summary["refusals"]["generator_saw"] = {"retries": rt.get("retries"), "exhausted": rt.get("exhausted"), "details": rt.get("refused_details") or {}}


def stall_finding(summary):
    """FINDING WEBSTALL1 (not a failure): web requests slower than 1 s, with the dv-svc-web cpu while they ran."""
    ws.annotate_slow(summary)
    text = ws.stall_text(summary.get("slow_requests"))
    if text:
        web.finding(text)
        print("[RECORD] %s" % text)
    wp, rt = summary.get("web_proc") or {}, summary.get("offer_retries") or {}
    print("[RECORD] W8 %d listeners: offers retried %s of %s (%s retries, %s out of retries); dv-svc-web during the connect phase: %s samples, "
          "threads %s..%s, cpu p50 %s max %s %%, longest gap between samples %s s" % (
              summary["listeners"], rt.get("retried"), rt.get("offers"), rt.get("retries"), rt.get("exhausted"), wp.get("samples"),
              wp.get("threads_min"), wp.get("threads_max"), wp.get("cpu_pct_p50"), wp.get("cpu_pct_max"), wp.get("max_gap_s")))


# ------------------------------------------------------------------ the measurement

def run_load(n, plan_kw, web_base_url, web_ca, load_gen, rig_control, target, accounts, workers):
    """One complete run: n listeners connected through the HTTP relay, the source talks the marker frames to both
    rooms, the edge and the generator are sampled over the window. Returns the summary dict."""
    plan = wl.Plan(listeners=n, **plan_kw)
    first, end = plan.window
    sampler = Sampler(rig_control, load_gen)
    lead = sampler.calibrate()
    pre, _ = sampler.edge()
    refusals_before, _settled = refusals.settled(lambda: refusals.read(rig_control))
    summary = {"listeners": n, "rate_pps": plan.rate, "warmup_s": plan.warmup_s, "window_s": plan.window_s,
               "workers": workers, "loadavg_start": loadavg(), "edge_pid": pre.pid, "edge_threads_pre": pre.threads,
               "edge_vmhwm_pre_kib": pre.vmhwm_kib, "edge_vmrss_pre_kib": pre.vmrss_kib, "proc_stat_lead_s": round(lead, 3)}
    creds = accounts(wl.accounts_needed(n))
    rooms = sorted({s.room for s in wl.slots(n)})
    offsets = wl.paced_offsets(n, wl.RTC_IP_RATE, wl.RTC_IP_BURST)
    webproc = ws.WebProcSampler(rig_control)
    webproc.start()
    psi = ws.PsiSampler()
    psi.start()
    t_run = time.time()
    started = load_gen.run({"listeners": n, "workers": workers, "accounts": creds, "base_url": web_base_url,
                            "ca": "/webout/" + os.path.basename(web_ca), "plan": plan_kw})
    summary["generator_workers"] = started.get("workers")
    st = wait_connected(load_gen, n, (offsets[-1] if offsets else 0) + 120.0)
    summary["connect_all_s"] = round(time.time() - t_run, 1)
    summary["connect_status"] = {k: st[k] for k in ("state", "connected", "failed", "failures", "error")}
    ok_connect = st["connected"] == n and st["state"] != "error"
    summary["edge_listeners_gauge"], summary["edge_listeners_gauge_seen"] = wait_gauge(rig_control, n)
    summary["web_proc"] = ws.web_proc_summary(webproc.stop())
    generator_observations(summary, load_gen, n)
    summary["join_refused_core"] = join_refused(rig_control)
    post_connect, _ = sampler.edge()
    summary["edge_vmhwm_after_connect_kib"] = post_connect.vmhwm_kib
    summary["edge_threads_after_connect"] = post_connect.threads
    if not ok_connect:
        load_gen.abort()
        summary["aborted"] = True
        summary["io_pressure"] = ws.psi_summary(psi.stop())
        record_refusals(summary, refusals_before, rig_control)
        return summary, plan, None, None
    src = Source(target, rooms)
    try:
        t_src = src.start(plan, delay_s=3.0)
        summary["source_start_epoch_offset_s"] = round(t_src - t_run, 1)
        e0, g0, err0 = sampler.at(src.epoch_of(first))
        t_cpu0 = time.process_time()
        e1, g1, err1 = sampler.at(src.epoch_of(end))
        t_cpu1 = time.process_time()
        src.join(plan.drain_s + 10.0)
        e2, g2, _err2 = sampler.at(src.epoch_of(plan.total_frames) + 1.0)
        summary["tester_cpu_window_pct"] = round(100.0 * (t_cpu1 - t_cpu0) / max(e1.t - e0.t, 1e-9), 2)
    finally:
        src.close()
    summary["source_errors"] = list(src.errors)
    res = load_gen.finish()
    summary["io_pressure"] = ws.psi_summary(psi.stop())
    generator_observations(summary, load_gen, n)
    record_refusals(summary, refusals_before, rig_control)
    summary["loadavg_end"] = loadavg()
    edge = {"cpu_pct": round(wl.cpu_percent(e0, e1), 3), "window_s": round(e1.t - e0.t, 3),
            "window_start_error_s": round(err0, 3), "window_end_error_s": round(err1, 3),
            "cpu_s_in_window": round(e1.cpu_s - e0.cpu_s, 3), "vmhwm_kib": e2.vmhwm_kib, "vmhwm_pre_kib": pre.vmhwm_kib,
            "vmhwm_at_window_end_kib": e1.vmhwm_kib, "vmrss_end_kib": e2.vmrss_kib, "threads_end": e2.threads,
            "cpu_pct_whole_run_after_connect": round(wl.cpu_percent(post_connect, e2), 3)}
    gen_cpu = generator_cpu(g0, g1)
    summary.update(edge=edge, generator=gen_cpu, generator_after=generator_cpu(g0, g2), edge_same_pid=(e2.pid == pre.pid))
    summary["source"] = src.lateness()
    rows = res["rows"]
    totals = wl.aggregate(rows)
    totals["invalid"] += res["srtp_errors"]
    totals["srtp_errors"] = res["srtp_errors"]
    totals["tail_ok_all"] = all(r["tail_ok"] for r in rows)
    totals["alive"] = {}
    for r in rows:
        for k, v in r["alive"].items():
            totals["alive"][k] = totals["alive"].get(k, 0) + v
    cs = sorted(r["connect_s"] for r in rows if r["connect_s"] is not None)
    totals["connect_s"] = {"p50": wl.nearest_rank(cs, 50), "max": cs[-1] if cs else None}
    ans = sorted(r["answer_s"] for r in rows if r["answer_s"] is not None)
    totals["answer_s"] = {"p50": wl.nearest_rank(ans, 50), "max": ans[-1] if ans else None}
    totals["errors"] = sorted({r["error"] for r in rows if r["error"]})[:5]
    summary["listener_totals"] = totals
    summary["loop_lag"] = res["loop_lag"]
    summary["late_bins_count"] = len(res.get("late_bins") or {})
    summary["late_bins"] = res.get("late_bins") or {}
    checks = wl.evaluate_gates(n, edge, totals, summary["source"])
    summary["gate"] = [{"name": c.name, "measured": c.measured, "limit": c.limit, "ok": c.ok, "note": c.note} for c in checks]
    summary["gate_ok"] = wl.all_ok(checks)
    worst = sorted(rows, key=lambda r: (r["max_ms"] is None, -(r["max_ms"] or 0)))[:5]
    summary["worst_listeners"] = [{k: r[k] for k in ("j", "room", "login", "p50_ms", "p99_ms", "max_ms", "lost", "duplicate", "reordered")}
                                  for r in worst]
    summary["listeners_detail"] = [[r["j"], r["room"], r["login"], r["received"], r["lost"], r["p50_ms"], r["p99_ms"], r["max_ms"],
                                    r["connect_s"]] for r in rows]
    summary["listeners_detail_columns"] = ["j", "room", "login", "received", "lost", "p50_ms", "p99_ms", "max_ms", "connect_s"]
    return summary, plan, totals, checks


def report(name, summary, checks):
    lines = ["W8 %s: %d listeners, window %.1f s" % (name, summary["listeners"], summary.get("edge", {}).get("window_s", 0))]
    for c in checks or []:
        lines.append("  %-52s %-10s limit %-9s %s %s" % (c.name, c.measured, c.limit, "ok" if c.ok else "FAIL", c.note))
    if summary.get("refusals"):
        lines.append("  " + refusals.line(summary["refusals"]))
    print("\n".join(lines))


def judge_integrity(summary, totals, n):
    """What every size must satisfy (the gate's zero-error checks and a clean set-up)."""
    problems = []
    problems.extend(ws.retry_problems(summary.get("offer_retries")))
    st = summary["connect_status"]
    if st["connected"] != n:
        problems.append("only %d of %d listeners connected (failed %d: %s %s)" % (st["connected"], n, st["failed"], st["failures"], st["error"]))
        return problems
    if summary["edge_listeners_gauge"] != n:
        problems.append("the edge counts %s listeners (%s), %d connected" % (summary["edge_listeners_gauge"], LISTENERS, n))
    for key in ("lost", "duplicate", "reordered", "invalid"):
        if totals[key]:
            problems.append("%s packets: %d" % (key, totals[key]))
    if summary["source"]["missed"] or summary["source_errors"]:
        problems.append("source: %d missed deadlines, errors %s" % (summary["source"]["missed"], summary["source_errors"]))
    if summary.get("join_refused_core"):
        problems.append("the edge refused %s JOINs (%s{reason=core}): peers accepted at signalling but closed"
                        % (summary["join_refused_core"], JOIN_REFUSED))
    if not summary["edge_same_pid"]:
        problems.append("dv-edge-webrtc restarted during the run")
    problems.extend(fix_problems(summary))
    return problems


def finding_lines(summary, totals):
    if totals is None:
        return
    if not totals["tail_ok_all"]:
        web.finding("W8 %d listeners: the PCMU tone after the marker differs from the reference G.711 encoder (consistent table, "
                    "markers intact)" % summary["listeners"])
    odd = {k: v for k, v in totals["alive"].items() if k != "204"}
    if odd:
        web.finding("W8 %d listeners: /rtc/alive answered %s besides 204 (documented: 204 for an owned live circuit)"
                    % (summary["listeners"], odd))


# ------------------------------------------------------------------ cases

@pytest.mark.parametrize("n", [1, 10, 50])
def test_calibration(n, load_gen, load_ready, rig_control, target, load_accounts, web_base_url, web_ca):
    """The generator at small sizes: connects through HTTP, receives every marker packet, and shows its own floor
    of jitter and cpu. Integrity is asserted; the gate table is printed and recorded (the 200-listener limits are
    not asserted at 1, 10 and 50)."""
    summary, plan, totals, checks = run_load(n, {}, web_base_url, web_ca, load_gen, rig_control, target, load_accounts, workers=1 if n < 20 else 2)
    RESULTS["cases"]["calibration_%d" % n] = summary
    write_results()
    report("calibration %d" % n, summary, checks)
    stall_finding(summary)
    finding_lines(summary, totals)
    problems = judge_integrity(summary, totals, n) if totals is not None else (
        ["did not connect: %s" % summary["connect_status"]] + ws.retry_problems(summary.get("offer_retries")) + fix_problems(summary))
    assert not problems, "; ".join(problems)


def test_w8_load_200(load_gen, load_ready, rig_control, target, load_accounts, web_base_url, web_ca):
    """The adopted gate: 200 listeners x 50 pps, 60 s window after 5 s warmup, through HTTP signalling."""
    n = 200
    summary, plan, totals, checks = run_load(n, {}, web_base_url, web_ca, load_gen, rig_control, target, load_accounts, workers=4)
    summary["gate_reference_c90"] = {"cpu_pct": 16.2, "peak_rss_mib": 33.6, "p99_ms": 4.6, "max_ms": 12.5, "packets": 600000, "lost": 0}
    RESULTS["cases"]["w8_200"] = summary
    write_results()
    stall_finding(summary)
    assert totals is not None, "did not connect: %s; %s" % (summary["connect_status"], "; ".join(ws.retry_problems(summary.get("offer_retries"))) or "no offer ran out of retries")
    report("200 x 50 pps", summary, checks)
    finding_lines(summary, totals)
    problems = judge_integrity(summary, totals, n)
    failed = ["%s: %s (limit %s)" % (c.name, c.measured, c.limit) for c in checks if not c.ok]
    assert not problems and not failed, "; ".join(problems + failed)


# ---- the short flood

def _status_count(codes):
    out = {}
    for c in codes:
        out[c] = out.get(c, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def flood_public(client, requests):
    codes, retry = [], set()
    t0 = time.monotonic()
    for _ in range(requests):
        try:
            resp = client.get("/api/v1/public/state", pace=False)
            codes.append(resp.status)
            if resp.status == 429:
                retry.add(resp.headers.get("retry-after"))
        except web.WebError:
            codes.append("error")
            if codes[-5:] == ["error"] * 5:
                break
    return {"codes": _status_count(codes), "retry_after": sorted(r for r in retry if r), "seconds": round(time.monotonic() - t0, 2)}


def flood_login(client, attempts):
    """Wrong password for attempts DIFFERENT unknown callsigns (so the per-callsign lockout is not what answers)."""
    codes, problem = [], None
    for i in range(attempts):
        resp = client.request("POST", "/api/v1/auth/session", json_body={"callsign": "N0FL%02d" % i, "password": "wrong-password-%d" % i},
                              pace=False)
        codes.append(resp.status)
        if resp.status == 429 and problem is None:
            try:
                body = resp.json()
            except (ValueError, web.WebError):
                body = {}
            problem = {"retry_after": resp.headers.get("retry-after"), "type": body.get("type"), "code": body.get("code")}
    return {"codes": _status_count(codes), "first_429": problem}


def flood_register(client, attempts):
    codes = []
    lock = threading.Lock()

    def one(i):
        resp = client.request("POST", "/api/v1/auth/register",
                              json_body={"callsign": "N0FR%02d" % i, "invite": "not-a-real-invite-%d" % i, "password": "wrong-password-%d" % i},
                              pace=False)
        with lock:
            codes.append(resp.status)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return {"codes": _status_count(codes)}


def raw_connections(host, port, count, request=b"GET /health HTTP/1.1\r\nHost: dvt\r\nConnection: close\r\n\r\n"):
    """Open `count` TCP connections at once and keep them open, then send one request on each and read the status
    line. Returns {status: count}; 'refused'/'timeout' when a connection could not be made or answered."""
    socks, results = [], []
    try:
        for _ in range(count):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            try:
                s.connect((host, port))
                socks.append(s)
            except OSError:
                results.append("refused")
                s.close()
        time.sleep(0.5)
        for s in socks:
            try:
                s.sendall(request)
            except OSError:
                pass
        for s in socks:
            try:
                s.settimeout(3.0)
                head = s.recv(64)
                results.append(int(head.split(b" ")[1]) if head.startswith(b"HTTP/1.") else "garbled")
            except socket.timeout:
                results.append("timeout")
            except (OSError, ValueError, IndexError):
                results.append("reset")
    finally:
        for s in socks:
            s.close()
    return _status_count(results)


def test_flood_keeps_serving(load_gen, load_ready, rig_control, target, load_accounts, web_base_url, web_ca):
    """MS9 limits under a short bounded flood from the tester's address while a probe listener (the generator's address)
    keeps hearing the source: 429 with Retry-After on the public reads, login and register buckets; 33rd connection of
    one address refused by the socket cap while the trusted proxy is exempt; nothing crashes; the probe loses nothing."""
    plan_kw = {"warmup_s": 2.0, "window_s": 36.0, "drain_s": 1.0}
    plan = wl.Plan(listeners=1, **plan_kw)
    first, end = plan.window
    sampler = Sampler(rig_control, load_gen)
    sampler.calibrate()
    pre, _ = sampler.edge()
    creds = load_accounts(1)
    load_gen.run({"listeners": 1, "workers": 1, "accounts": creds, "base_url": web_base_url,
                  "ca": "/webout/" + os.path.basename(web_ca), "plan": plan_kw})
    st = wait_connected(load_gen, 1, 90.0)
    assert st["connected"] == 1, "probe listener did not connect: %s" % st
    time.sleep(3.0)
    src = Source(target, ["A"])
    out = {"probe_edge_pid": pre.pid}
    try:
        src.start(plan, delay_s=2.0)
        time.sleep(max(0.0, src.epoch_of(first + 50) - time.time()))              # let the probe hear the source first
        client = web.WebClient(web_base_url, web_ca, timeout=10)
        t0 = time.time()
        out["public_reads"] = flood_public(client, 150)
        out["login"] = flood_login(client, 16)
        out["register"] = flood_register(client, 6)
        out["connections_direct_to_sut_40"] = raw_connections(target.xlxd_sut, 8081, 40)
        out["flood_seconds"] = round(time.time() - t0, 1)
        # the trusted proxy forwards: 40 simultaneous client connections to the proxy become 40 from its address
        out["proxy_forwarded_40"] = _via_proxy(web_base_url, web_ca, 40)
        time.sleep(max(0.0, src.epoch_of(end + 10) - time.time() - 8.0))
        time.sleep(6.0)                                                           # buckets refill: the service answers again
        out["after_public_state"] = web.WebClient(web_base_url, web_ca, timeout=10).get("/api/v1/public/state", pace=False).status
        out["after_ready"] = web.WebClient(web_base_url, web_ca, timeout=10).get("/ready", pace=False).status
        src.join(60.0)
    finally:
        src.close()
    res = load_gen.finish()
    post, _ = sampler.edge()
    row = res["rows"][0]
    out["probe"] = {k: row[k] for k in ("received", "lost", "duplicate", "reordered", "invalid", "p99_ms", "max_ms")}
    out["edge_same_pid_after"] = post.pid == pre.pid
    out["source_missed"] = src.lateness()["missed"]
    RESULTS["cases"]["flood"] = out
    write_results()
    print(json.dumps(out, sort_keys=True))
    # documented behaviour (web.md MS9, API4): 429 with Retry-After once a bucket is empty
    pub, login, reg = out["public_reads"]["codes"], out["login"]["codes"], out["register"]["codes"]
    assert pub.get(429, 0) > 0 and out["public_reads"]["retry_after"], "no 429 with Retry-After on 150 public reads: %s" % out["public_reads"]
    assert login.get(401, 0) > 0 and login.get(429, 0) > 0, "login bucket: %s" % out["login"]
    assert reg.get(429, 0) > 0, "register bucket: %s" % out["register"]
    direct = out["connections_direct_to_sut_40"]
    assert direct.get(503, 0) >= 1, "40 connections of one address to the web service: no 503 (cap 32): %s" % direct
    # /health is also behind the per-address public bucket (the flood just emptied it), so a request that reached the
    # service is 200 or 429; only a 503 would be the socket cap
    via = out["proxy_forwarded_40"]
    assert via.get(503, 0) == 0 and via.get(200, 0) + via.get(429, 0) == 40, \
        "trusted proxy is not exempt from the per-address cap: %s" % via
    if direct.get(503, 0) != 40 - 32:
        web.finding("W8 flood: 40 simultaneous connections of one untrusted address got %s (documented cap 32: expected 8 x 503)" % direct)
    assert out["edge_same_pid_after"], "dv-edge-webrtc restarted during the flood"
    assert out["after_public_state"] == 200 and out["after_ready"] == 200, "the service does not answer after the flood: %s" % out
    p = out["probe"]
    assert p["lost"] == 0 and p["duplicate"] == 0 and p["reordered"] == 0 and p["invalid"] == 0, "probe listener disturbed: %s" % p
    assert out["source_missed"] == 0


def _via_proxy(base_url, ca, count):
    """`count` simultaneous HTTPS connections through the trusted proxy, each held open until all are up, then one
    GET /health each: the web service sees all of them from the proxy's address, which is exempt."""
    import ssl
    import urllib.parse
    url = urllib.parse.urlsplit(base_url)
    ctx = ssl.create_default_context(cafile=ca)
    conns, results = [], []
    try:
        for _ in range(count):
            raw = socket.create_connection((url.hostname, url.port or 443), timeout=5.0)
            conns.append(ctx.wrap_socket(raw, server_hostname=url.hostname))
        for c in conns:
            c.sendall(b"GET /health HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % url.hostname.encode())
        for c in conns:
            try:
                head = c.recv(64)
                results.append(int(head.split(b" ")[1]) if head.startswith(b"HTTP/1.") else "garbled")
            except (OSError, ValueError, IndexError):
                results.append("error")
    finally:
        for c in conns:
            c.close()
    return _status_count(results)


# ---- WEBSTALL1: a burst of joins inside the edge's join window

JOIN_BURST = 4                  # offers sent at once from one address: the first is admitted, the rest fall in the same one second window


def rendered_config_text(target):
    """The run's rendered dv-router.toml (out/<run>/xlxd-config/xlxd-sut/dv-router.toml), '' when it is not there."""
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def test_join_burst_counts_join_window(load_gen, load_ready, rig_control, target, load_accounts, web_base_url, web_ca):
    """WEBSTALL1 (3c): offers sent at once land in the edge's join window and are refused 503 `unavailable` with Retry-After 5; each refusal counts
    dv_edge_webrtc_offer_refused_total{reason=join_window} (the deciding edge boundary) and dv_web_rtc_refused_total{reason=join_window} (the HTTP
    reply), the two families are judged one by one and never added. The other reasons of both families do not move. The harness retry rule must see
    the refusals as capacity refusals by status and code (not by the detail text): it sends the offers again after the Retry-After.

    Why the window is set to 1 per second (rig/config/dv-router/join-window.toml, [edges.webrtc] joins_per_second = 1): with the default 5 the
    web service's own gates stop one address first (per address 1/s, burst 5; global 5/s, burst 5), so a burst of MORE than 5 joins per second
    cannot reach the edge from a single address. Only two addresses, the second one timed 0.2 to 0.9 s after the five of the first (a refilled
    global token while the first five are still in the edge window), can: that needs two client addresses with a timed second wave, a GAP
    (docs/web-refusals-cases.md). One address and a window of 1 per second exercises the same counters and the same refusal deterministically."""
    if not refusals.fix_on():
        pytest.skip(refusals.skip_reason())
    setting = refusals.join_window_setting(rendered_config_text(target), os.environ.get("DVT_SUT_CONFIG_EXTRA"))
    if setting["joins_per_second"] != 1:
        pytest.skip("the edge's join window is not 1 per second (%s): name rig/config/dv-router/join-window.toml in DVT_SUT_CONFIG_EXTRA "
                    "(docs/web-refusals-cases.md)" % setting)
    before, _settled = refusals.settled(lambda: refusals.read(rig_control))
    core_before = join_refused(rig_control)
    creds = load_accounts(wl.accounts_needed(JOIN_BURST))
    load_gen.run({"listeners": JOIN_BURST, "workers": 1, "accounts": creds, "base_url": web_base_url,
                  "ca": "/webout/" + os.path.basename(web_ca), "plan": {}, "burst": True})
    st = wait_connected(load_gen, JOIN_BURST, 90.0)
    summary = {"listeners": JOIN_BURST, "join_window_setting": setting, "connect_status": {k: st[k] for k in ("state", "connected", "failed", "failures", "error")}}
    try:
        status = load_gen.status()
    except Exception as exc:                                        # noqa: BLE001 - an observation
        status = {}
        summary["status_error"] = "%s: %s" % (type(exc).__name__, exc)
    summary["offer_retries"] = ws.retry_stats(status, JOIN_BURST)
    load_gen.abort()
    summary["edge_listeners_after_abort"], summary["edge_listeners_after_abort_seen"] = wait_gauge(rig_control, 0)
    after, settled = refusals.settled(lambda: refusals.read(rig_control))
    summary["refusals"] = refusals.record(before, after)
    summary["refusals"]["settled"] = settled
    core_after = join_refused(rig_control)
    summary["join_refused_core"] = {"before": core_before, "after": core_after}
    RESULTS["cases"]["join_burst"] = summary
    write_results()
    print("[RECORD] WEBSTALL1 join burst of %d: %s; generator retries %s, exhausted %s, refusal details %s; connected %s of %d" % (
        JOIN_BURST, refusals.line(summary["refusals"]), summary["offer_retries"]["retries"], summary["offer_retries"]["exhausted"],
        {k: refusals.detail_note(k) for k in summary["offer_retries"].get("refused_details", {})}, st["connected"], JOIN_BURST))

    problems = list(summary["refusals"]["shape_problems"])
    delta = summary["refusals"]["delta"]
    want = JOIN_BURST - 1                                                  # the first offer is admitted, the others fall in the same window
    for fam, family in (("edge", refusals.EDGE_FAMILY), ("web", refusals.WEB_FAMILY)):
        counts = delta.get(fam)
        if counts is None:
            continue
        got = counts.get("join_window", 0)
        if got < want:
            problems.append("%s{reason=join_window} rose by %g, want at least %d (a burst of %d offers in a window of 1 per second)" % (family, got, want, JOIN_BURST))
        other = {r: v for r, v in refusals.moved(counts).items() if r != "join_window"}
        if other:
            problems.append("%s moved other reasons too: %s" % (family, other))
    if summary["offer_retries"]["retries"] < want:
        problems.append("the generator sent %d offers again, want at least %d: its retry rule did not see the refusals (503 + code unavailable + Retry-After) as capacity refusals"
                        % (summary["offer_retries"]["retries"], want))
    if core_before is not None and core_after is not None and core_after != core_before:
        problems.append("%s{reason=core} moved from %s to %s: a JOIN refused by the room core is another counter and must not follow an offer refusal"
                        % (JOIN_REFUSED, core_before, core_after))
    assert not problems, "; ".join(problems)


def test_zz_rfwd1_no_stall_kills(stall_kills_start):
    """RFWD1 (dv-router wip-8891cc7): dv_launcher_child_stall_kills_total{child} stays 0 for every child across this module; any other
    value is a false kill (this module stalls no RF child on purpose). Runs last; start read before the first test, end read after one hub
    publication, both outside the measured windows; out/<run>/web/stall-kills.json (cases/stall_kills_support.py)."""
    check_end("test_web_load", stall_kills_start)
