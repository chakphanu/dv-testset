# SPDX-License-Identifier: MIT
"""External dial test T12 (profile asl-ext): the REAL AllStarLink node 400169, outbound only.

    DVT_ASL_EXT_GO=400169 scripts/run.sh --profile asl-ext -m asl_ext -- cases/test_asl_ext.py

This is the only case that leaves the rig. The user authorised exactly this: register node 400169 with the real
AllStarLink registry and dial the nodes of DVT_ASL_EXT_MODULES (XLX345 modules A and H: 400161, 400168) listen-only.
No key, no voice, no tone: the node has no transmit path (rig/config/asl-lab/ext/rpt.conf.in, chan_usrp to a dummy
address, no MixMonitor, only the functions connect / disconnect / disconnect all), and what arrives is read from the
capture. At most two dial attempts per node, 60 s apart. The registration password is a file the container reads
(rig/asl-lab-entry.sh): it is never an argument, variable, log line or output of this case.

Everything goes to out/<run>/asl-ext/: timeline.jsonl (UTC events, appended as they happen so that the runner can
watch it), asl-ext.pcap (DNS, TLS to the registry and IAX2 on the node's side), frames.json, registration tables,
rx-<node>-<n>.ulaw (what the far side sent), summary.json. Public addresses appear only there.

Safety: `touch out/<run>/asl-ext/stop` ends the case at the next check (links dropped, registration withdrawn,
Asterisk stopped). DVT_ASL_EXT_REGISTRY other than the real registry makes it a dry run (registration attempted
against that name, no dial).
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dvtest import asl_ext, asl_lab, iax2_frames

pytestmark = [pytest.mark.rig, pytest.mark.asl_ext]

REAL_REGISTRY = "register.allstarlink.org"
HOLD_S = int(os.environ.get("DVT_ASL_EXT_HOLD", "60"))
LINK_WAIT_S = 25
RETRY_GAP_S = 60
MODULE_GAP_S = 45
REG_WAIT_S = 150
DNS_WAIT_S = 120


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Stop(Exception):
    pass


class Run:
    def __init__(self, host, out):
        self.host = host
        self.out = out
        self.node = host.node
        self.timeline = out / "timeline.jsonl"
        self.stop_file = out / "stop"
        self.results = []
        self.reg_wait = REG_WAIT_S

    def ev(self, name, **data):
        rec = {"utc": utc(), "event": name}
        rec.update(data)
        with open(self.timeline, "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def check_stop(self):
        if self.stop_file.exists():
            raise Stop("stop file present")

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check_stop()
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def regs(self):
        text = self.host.cli("rpt show registrations")
        return text, asl_ext.parse_registrations(text)

    def lookup(self, node):
        """SRV, A and TXT of a node in the real zone, asked through the node's own resolver (what app_rpt asks);
        appended to dns.jsonl (public addresses: out/ only). Returns {"srv": [...], "a": [...], "txt": [...], "rcode": {...}}."""
        got = {"node": str(node), "utc": utc(), "srv": [], "a": [], "txt": [], "rcode": {}}
        zone = "%s.nodes.allstarlink.org" % node
        for qtype, name in (("SRV", "_iax._udp." + zone), ("A", zone), ("TXT", zone)):
            reply = self.host.post("/resolve", {"name": name, "type": qtype})
            got["rcode"][qtype] = reply.get("rcode")
            got[qtype.lower()] += [a["text"] for a in reply.get("answers", [])]
        with open(self.out / "dns.jsonl", "a") as fh:
            fh.write(json.dumps(got, sort_keys=True) + "\n")
        return got

    def resolve_self(self):
        return self.lookup(self.node)["a"]

    def snapshot(self, tag):
        snap = {"lstats": self.host.cli("rpt lstats %s" % self.node),
                "channels": self.host.cli("iax2 show channels"),
                "nodes": self.host.cli("rpt nodes %s" % self.node)}
        (self.out / ("state-%s.txt" % tag)).write_text(
            "".join("== %s\n%s\n" % (k, v) for k, v in snap.items()))
        return snap

    def register(self):
        """Switch the registration on and wait until the module's own table says Registered."""
        self.host.post("/ext/register", {"on": True})
        t0 = time.monotonic()
        self.ev("registration_requested")
        last = ""
        while time.monotonic() - t0 < self.reg_wait:
            self.check_stop()
            text, rows = self.regs()
            last = text
            row = asl_ext.registered(rows, self.node)
            if row:
                (self.out / "registrations-up.txt").write_text(text)
                self.ev("registered", seconds=round(time.monotonic() - t0, 1), refresh=row["refresh"])
                return row
            time.sleep(2)
        (self.out / "registrations-fail.txt").write_text(last)
        self.ev("registration_failed", seconds=self.reg_wait)
        return None

    def dns_matches(self, perceived):
        """The registry's DNS A record of this node against the address the registration reported (the port is
        ignored). Polls until they agree; returns (agrees, addrs)."""
        want = perceived.rsplit(":", 1)[0]
        t0 = time.monotonic()
        addrs = []
        while time.monotonic() - t0 < DNS_WAIT_S:
            self.check_stop()
            addrs = self.resolve_self()
            if addrs == [want]:
                return True, addrs
            time.sleep(10)
        return False, addrs

    def dial(self, module, attempt):
        """One dial: connect, hold, disconnect. Returns the result dict."""
        res = {"module": module, "attempt": attempt, "established": False}
        text, rows = self.regs()
        row = asl_ext.registered(rows, self.node)
        res["registered_before"] = bool(row)
        look = self.lookup(module)
        res["dns_rcodes"] = look["rcode"]
        res["dns_has_srv"] = bool(look["srv"])
        res["dns_has_a"] = bool(look["a"])
        res["start_utc"] = utc()
        self.ev("dial_start", module=module, attempt=attempt, dns_srv=bool(look["srv"]), dns_a=bool(look["a"]))
        self.host.cli("rpt fun %s *3%s" % (self.node, module))
        deadline = time.monotonic() + LINK_WAIT_S
        state = None
        while time.monotonic() < deadline:
            self.check_stop()
            state = asl_ext.lstats_state(self.host.cli("rpt lstats %s" % self.node), module)
            if state == "ESTABLISHED":
                break
            time.sleep(0.5)
        res["link_state"] = state
        if state == "ESTABLISHED":
            res["established"] = True
            res["established_utc"] = utc()
            self.ev("link_established", module=module)
            samples = []
            t0 = time.monotonic()
            n = 0
            while time.monotonic() - t0 < HOLD_S:
                self.check_stop()
                n += 1
                snap = self.snapshot("%s-%d-s%d" % (module, attempt, n))
                samples.append({"at_s": round(time.monotonic() - t0, 1),
                                "state": asl_ext.lstats_state(snap["lstats"], module)})
                time.sleep(10)
            res["samples"] = samples
            res["state_end"] = asl_ext.lstats_state(self.host.cli("rpt lstats %s" % self.node), module)
        else:
            self.snapshot("%s-%d-fail" % (module, attempt))
        self.host.cli("rpt fun %s *1%s" % (self.node, module))
        gone = False
        for _ in range(40):
            if asl_ext.lstats_state(self.host.cli("rpt lstats %s" % self.node), module) is None:
                gone = True
                break
            time.sleep(0.25)
        if not gone:
            self.host.cli("rpt fun %s *6" % self.node)
        time.sleep(1.5)
        self.host.hangup_all()
        res["disconnected"] = gone
        res["end_utc"] = utc()
        self.ev("dial_end", module=module, attempt=attempt, established=res["established"], link_state=state)
        self.results.append(res)
        return res


def analyse(run, pcap):
    """frames.json, per-call results and the rx audio files, from the capture."""
    doc = iax2_frames.extract(pcap, ports=(4569,))
    (run.out / "frames.json").write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    calls = asl_ext.calls(doc)
    summary = {"frames": doc["summary"], "calls": []}
    for i, call in enumerate(calls, 1):
        rx = asl_ext.rx_audio(doc, call)
        name = "rx-%s-%d.ulaw" % (call["called"], i)
        if rx:
            (run.out / name).write_bytes(rx)
        entry = dict(call)
        entry.update(dns=asl_ext.dns_for(doc, call["called"]), text=asl_ext.text_exchange(doc, call),
                     rx_bytes=len(rx), rx_file=name if rx else None)
        if rx:
            pcm = asl_lab.ulaw_to_pcm(rx)
            entry["rx_rms"] = round(asl_lab.score.rms(pcm), 1)
            entry["rx_seconds"] = round(len(rx) / 8000, 2)
        summary["calls"].append(entry)
    summary["dns_all"] = [{"names": [q["name"] for q in m["questions"]], "rcode": m["rcode"], "t": m["t"],
                           "answers": [a.get("text") for a in m["answers"]]} for m in doc["dns"] if m.get("qr")]
    return summary


@pytest.fixture(scope="module")
def ext():
    if os.environ.get("DVT_ASL_EXT") != "on" or os.environ.get("DVT_ASL_EXT_GO") != "400169":
        pytest.skip("profile asl-ext with DVT_ASL_EXT_GO=400169 is not running")
    node = os.environ.get("DVT_ASL_EXT_NODE", "400169")
    host = asl_lab.Host("asl-ext", os.environ["DVT_ASL_EXT_IP"], node)
    out = Path(os.environ.get("DVT_OUT", "out")) / "asl-ext"
    out.mkdir(parents=True, exist_ok=True)
    host.ready()
    return Run(host, out)


def test_external_dial(ext):
    modules = [m for m in os.environ.get("DVT_ASL_EXT_MODULES", "400161,400168").split(",") if m]
    real = os.environ.get("DVT_ASL_EXT_REGISTRY", REAL_REGISTRY) == REAL_REGISTRY
    host = ext.host
    ext.ev("start", node=ext.node, modules=modules, real_registry=real, hold_s=HOLD_S)
    (ext.out / "versions.txt").write_text(host.versions_text() + host.cli("core show version") + "\n")
    host.capture_start("asl-ext")
    ext.ev("capture_started")
    problem = None
    try:
        ext.reg_wait = REG_WAIT_S if real else 25
        row = ext.register()
        if row is None:
            if real:
                problem = "registration did not reach Registered"
            else:
                ext.ev("dry_run_ok", note="no registration against a non-real registry, as expected")
                look = ext.lookup(modules[0])
                ext.ev("dry_run_lookup", node=modules[0], srv=len(look["srv"]), a=len(look["a"]), rcode=look["rcode"])
        elif not real:
            problem = "dry run: the registration reached Registered against a name that is not the real registry"
        else:
            agrees, addrs = ext.dns_matches(row["perceived"])
            ext.ev("dns_check", agrees=agrees, count=len(addrs))
            (ext.out / "dns-self.json").write_text(json.dumps({"registered": row["perceived"], "dns_a": addrs}) + "\n")
            if not agrees:
                problem = "the registry DNS does not point at this node after %d s" % DNS_WAIT_S
        if problem is None and real:
            for mi, module in enumerate(modules):
                for attempt in (1, 2):
                    ext.check_stop()
                    res = ext.dial(module, attempt)
                    if res["established"]:
                        break
                    if attempt == 1:
                        ext.ev("retry_wait", module=module, seconds=RETRY_GAP_S)
                        ext.sleep(RETRY_GAP_S)
                if mi + 1 < len(modules):
                    ext.ev("module_gap", seconds=MODULE_GAP_S)
                    ext.sleep(MODULE_GAP_S)
    except Stop:
        problem = "stopped by the stop file"
    finally:
        # always: links down, registration withdrawn, capture fetched, Asterisk stopped (the node stops registering)
        try:
            host.cli("rpt fun %s *6" % ext.node)
            time.sleep(1.5)
            host.hangup_all()
        except asl_lab.LabError as err:
            ext.ev("cleanup_error", where="disconnect", error=str(err)[:200])
        try:
            host.post("/ext/register", {"on": False})
            ext.ev("registration_withdrawn")
            (ext.out / "registrations-end.txt").write_text(host.cli("rpt show registrations"))
        except asl_lab.LabError as err:
            ext.ev("cleanup_error", where="withdraw", error=str(err)[:200])
        time.sleep(2)
        pcap = ext.out / "asl-ext.pcap"
        try:
            size = host.capture_stop("asl-ext")
            pcap.write_bytes(host.fetch("pcap", "asl-ext.pcap"))
            ext.ev("capture_fetched", bytes=size)
        except asl_lab.LabError as err:
            ext.ev("cleanup_error", where="capture", error=str(err)[:200])
        try:
            host.post("/cli", {"cmd": "core stop gracefully"})
            ext.ev("asterisk_stopping")
        except asl_lab.LabError as err:
            # the agent's answer is cut when Asterisk exits first: that is the expected end
            ext.ev("asterisk_stopped", note=str(err)[:120])
    summary = {"problem": problem, "dials": ext.results}
    if pcap.exists():
        try:
            summary.update(analysis=analyse(ext, pcap))
        except Exception as err:        # the analysis must never hide the run's own result
            summary["analysis_error"] = "%s: %s" % (err.__class__.__name__, str(err)[:200])
    (ext.out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    ext.ev("done", problem=problem)
    assert problem is None, problem
    if real:
        assert ext.results, "no dial was made"
