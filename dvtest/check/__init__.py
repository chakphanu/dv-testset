# SPDX-License-Identifier: MIT
"""Passive wire checker for rig captures: classify every UDP datagram of a
pcap/pcapng file by protocol and check it (dvtest.check.<protocol>).

    python3 -m dvtest.check <capture|dir> [--json out.json] [--ports-from rig]

Each capture is checked on its own. Datagrams that do not touch the
capture's own address (bridge flooding) are counted as foreign; multicast
and broadcast datagrams as multicast; any other datagram that no protocol
claims is an `unclassified` warning per flow. Exit 0 = no error findings
(warnings allowed unless --strict), 1 = anomalies, 2 = usage or read error.
"""
import ipaddress
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

from .. import pcap
from ..target import DEFAULT_SUBNET, HOST_OFFSETS
from . import ambed, dcs, dextra, dmr, dplus, iax2, m17, nxdn, p25, usrp, xlx, ysf
from .common import ERROR, WARN, Report, flow_of

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# capture file stem -> rig host key (dvtest.target.HOST_OFFSETS)
RIG_FILES = {"xlxd-ref": "xlxd_ref", "xlxd-sut": "xlxd_sut", "transcoder-ref": "ambed_ref",
             "transcoder-sut": "ambed_sut", "tester": "tester", "allstar": "allstar",
             "nxdn-ref": "nxdn_ref", "nxdn-sut": "nxdn_sut", "p25-ref": "p25_ref", "p25-sut": "p25_sut",
             "xlxd-peer-ref": "xlxd_peer_ref", "xlxd-peer-sut": "xlxd_peer_sut"}


def rig_config(subnet=None):
    """Rig hosts and USRP links from the repository (rig/config)."""
    subnet = subnet or os.environ.get("DVT_SUBNET") or DEFAULT_SUBNET
    net = ipaddress.ip_network(subnet, strict=False)
    hosts = {key: str(net.network_address + off) for key, off in HOST_OFFSETS.items()}
    prefix = str(net.network_address).rsplit(".", 1)[0]
    links = usrp.endpoints_from(os.path.join(ROOT, "rig", "config", "xlxd.usrp.in"), prefix)
    return {"hosts": hosts, "usrp": links, "subnet": str(net)}


LEAD = 2.0


def outages(run_dir, hosts):
    """[(ip, t0, t1, label)] from <run>/control/control.log (scripts/run.sh
    control channel): a stopped, killed or restarted service is down from
    its "request stop|kill|restart" line (without one: 12 s / 2 s before the completed
    line, compose stop waits up to 10 s) until 10 s after its "start" line
    (a restart: until 10 s after its own completed line)."""
    path = os.path.join(run_dir, "control", "control.log")
    if not os.path.isfile(path):
        return []
    requested, down, out = {}, {}, []
    with open(path) as fh:
        for line in fh:
            m = re.match(r"^(\S+Z) (?:(request) )?(stop|kill|start|restart) (\S+)(?: -> (\S+))?$", line.strip())
            ip = hosts.get(RIG_FILES.get(m.group(4), "")) if m else None
            if ip is None or (m.group(2) is None) == (m.group(5) is None):
                continue
            ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
            action, svc = m.group(3), m.group(4)
            if m.group(2):
                # a stream cut by the action may have sent its last packet
                # just before the request
                requested[(action, svc)] = ts - LEAD
            elif m.group(5) != "0":
                continue
            elif action == "restart":
                t0 = requested.pop((action, svc), ts - 12.0)
                out.append((ip, t0, ts + 10.0, svc + " restart"))
            elif action != "start":
                t0 = requested.pop((action, svc), ts - (12.0 if action == "stop" else 2.0))
                down.setdefault(svc, (ip, t0))
            elif svc in down:
                ip, t0 = down.pop(svc)
                out.append((ip, t0, ts + 10.0, svc))
    out += [(ip, t0, float("inf"), svc) for svc, (ip, t0) in down.items()]
    return out


def _excuse(report, frame_ts, windows):
    """Errors on flows of a service the control channel had stopped become
    warnings (still listed, message tagged)."""
    for index, f in enumerate(report.findings):
        ts = frame_ts.get(f.frame)
        if f.severity != ERROR or ts is None:
            continue
        for ip, t0, t1, svc in windows:
            if t0 <= ts <= t1 and re.search(r"(^|[>:<])%s:" % re.escape(ip), f.flow):
                report.findings[index] = f._replace(severity=WARN, message=f.message + (
                    " [during control stop of %s]" % svc))
                break


def _in(ip, net):
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def _is_group(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_multicast or ip.endswith(".255")


def check_file(path, rig=None, jitter_ms=None, windows=()):
    """Check one capture; returns a JSON-able dict. windows: outages()."""
    report = Report(jitter_ms)
    stats = {}
    datagrams = list(pcap.udp_datagrams(path, stats))
    stem = os.path.basename(path).split(".")[0]
    own = None
    if rig and rig["hosts"].get(RIG_FILES.get(stem)):
        own = rig["hosts"][RIG_FILES[stem]]
        net = ipaddress.ip_network(rig["subnet"])
        if datagrams and not any(_in(a, net) for d in datagrams for a in (d.src, d.dst)):
            # an idle service only sees bridge noise, but some of it is
            # from the rig subnet; none at all means the subnet is wrong
            raise pcap.CaptureError("no datagram of %s touches the rig subnet %s; wrong DVT_SUBNET?" % (
                stem, rig["subnet"]))
    if own is None:
        seen = Counter()
        for d in datagrams:
            if not _is_group(d.dst):
                seen[d.src] += 1
                seen[d.dst] += 1
        own = seen.most_common(1)[0][0] if seen else None
    rig_hosts = set(rig["hosts"].values()) if rig else None
    m17_checker = m17.Checker(report)
    checkers = [ambed.Checker(report), dextra.Checker(report, m17_checker), dmr.Checker(report),
                m17_checker, iax2.Checker(report), nxdn.Checker(report), p25.Checker(report), xlx.Checker(report, m17_checker),
                dplus.Checker(report, m17_checker), dcs.Checker(report, m17_checker), ysf.Checker(report),
                usrp.Checker(report, rig["usrp"] if rig else ())]
    names = [ambed.NAME, dextra.NAME, dmr.NAME, m17.NAME, iax2.NAME, nxdn.NAME, p25.NAME, xlx.NAME, dplus.NAME, dcs.NAME,
             ysf.NAME, usrp.NAME]
    foreign = multicast = 0
    other = Counter()
    other_flows = {}
    end_ts = None
    for d in datagrams:
        if d.ts is not None:
            end_ts = d.ts
        if _is_group(d.dst):
            multicast += 1
            continue
        if own not in (d.src, d.dst):
            foreign += 1
            continue
        for name, checker in zip(names, checkers):
            if checker.claims(d):
                if rig_hosts and (d.src not in rig_hosts or d.dst not in rig_hosts):
                    report.error(name, d, "unknown_peer", "%s is not a rig host" % (
                        d.src if d.src not in rig_hosts else d.dst))
                checker.feed(d)
                break
        else:
            flow = flow_of(d)
            other[flow] += 1
            if flow not in other_flows:
                other_flows[flow] = d.frame
                report.warn("other", d, "unclassified", "%d B datagram no protocol claims" % len(d.payload))
    for checker in checkers:
        checker.finish(end_ts)
    if windows:
        _excuse(report, {d.frame: d.ts for d in datagrams}, windows)
    report.findings.sort(key=lambda f: (f.frame, f.code))
    protocols = {}
    for name, p in sorted(report.protocols.items()):
        if not p["datagrams"]:
            continue
        out = dict(p)
        out["flows"] = dict(p["flows"])
        out["kinds"] = dict(p["kinds"])
        protocols[name] = out
    return {"file": path, "own": own, "read": stats, "foreign": foreign, "multicast": multicast,
            "other": dict(other), "protocols": protocols,
            "errors": sum(1 for f in report.findings if f.severity == ERROR),
            "warnings": sum(1 for f in report.findings if f.severity == WARN),
            "findings": [f._asdict() for f in report.findings]}


# Codes whose sut-only occurrence needs a precondition on the reference
# side before it counts as a regression: code -> (test on a ref result,
# what the reference lacked). Other codes compare as they are.
def _dmr_closes(ref):
    return ref["protocols"].get("dmr", {}).get("closes_mid_stream", 0) > 0


def _p25_calls(ref):
    return bool(ref["protocols"].get("p25", {}).get("streams"))


COMPARABLE = {"dmr.after_close": (_dmr_closes, "no stream at close"),
              "dmr.after_close_cache": (_dmr_closes, "no stream at close"),
              "p25.terminator_in_ldu": (_p25_calls, "no P25 call"),
              "p25.stray_terminator": (_p25_calls, "no P25 call")}
# warnings by default (known upstream quirks) that become errors when only
# the sut shows them on a comparable reference
UPSTREAM_CODES = ("dmr.after_close_cache", "p25.terminator_in_ldu", "p25.stray_terminator")


def _stem(result):
    return os.path.basename(result["file"]).split(".")[0]


def _mark(result, finding, severity, note):
    finding["severity"] = severity
    finding["message"] += " " + note


def differential(results):
    """Severity across a reference/system-under-test pair of captures
    (<name>-ref vs <name>-sut, every .N part of each): a code seen on both
    sides is upstream behaviour (warn); a code seen only on the sut is a
    regression (error) when the reference had its precondition (COMPARABLE),
    else a warning; errors only on the reference are warnings. Captures
    without a partner keep their severities."""
    sides = {}
    for r in results:
        name, _, side = _stem(r).rpartition("-")
        if side in ("ref", "sut") and name:
            sides.setdefault(name, {"ref": [], "sut": []})[side].append(r)
    for name, pair in sides.items():
        if not pair["ref"] or not pair["sut"]:
            continue
        codes = {side: {f["code"] for r in pair[side] for f in r["findings"]} for side in pair}
        for r in pair["sut"]:
            for f in r["findings"]:
                if f["code"] in codes["ref"]:
                    _mark(r, f, WARN, "(also on reference: upstream behaviour)")
                elif f["code"] in COMPARABLE and not any(COMPARABLE[f["code"]][0](x) for x in pair["ref"]):
                    _mark(r, f, WARN, "(not comparable on reference: %s)" % COMPARABLE[f["code"]][1])
                elif f["code"] in UPSTREAM_CODES:
                    _mark(r, f, ERROR, "[sut only: not seen on %s-ref, differential regression]" % name)
        for r in pair["ref"]:
            for f in r["findings"]:
                if f["severity"] == ERROR:
                    _mark(r, f, WARN, "(also on system under test)" if f["code"] in codes["sut"]
                          else "(reference only)")
        for r in pair["ref"] + pair["sut"]:
            r["errors"] = sum(1 for f in r["findings"] if f["severity"] == ERROR)
            r["warnings"] = sum(1 for f in r["findings"] if f["severity"] == WARN)


def captures(target):
    if os.path.isdir(target):
        sub = os.path.join(target, "capture")
        base = sub if os.path.isdir(sub) else target
        names = [n for n in os.listdir(base) if n.endswith((".pcap", ".pcapng", ".cap"))]
        # run.sh restarts a sniffer as <svc>.2.pcapng, <svc>.3.pcapng, ...
        part = lambda n: (n.split(".")[0], int(n.split(".")[1]) if n.split(".")[1].isdigit() else 1)
        return [os.path.join(base, n) for n in sorted(names, key=part)]
    return [target]


def summary_lines(result, per_code=3):
    lines = []
    read = result["read"]
    lines.append("%s: %d packets, %d udp, own %s, foreign %d, multicast %d, unclassified %d%s" % (
        os.path.basename(result["file"]), read.get("packets", 0), read.get("udp", 0), result["own"],
        result["foreign"], result["multicast"], sum(result["other"].values()),
        "".join(", %s %d" % (k, v) for k, v in sorted(read.items()) if k not in ("packets", "udp", "non_udp", "non_ip"))))
    by_proto = Counter()
    for f in result["findings"]:
        by_proto[(f["protocol"], f["severity"])] += 1
    for name, p in result["protocols"].items():
        streams = p["streams"]
        lines.append("  %-6s %6d dgrams %3d flows %4d streams  loss %d dup %d reorder %d  errors %d warn %d" % (
            name, p["datagrams"], len(p["flows"]), len(streams),
            sum(s["loss"] for s in streams), sum(s["dup"] for s in streams), sum(s["reorder"] for s in streams),
            by_proto[(name, ERROR)], by_proto[(name, WARN)]))
        lines.append("         kinds: " + " ".join("%s=%d" % kv for kv in sorted(p["kinds"].items())))
    codes = {}
    for f in result["findings"]:
        codes.setdefault((f["severity"], f["code"]), []).append(f)
    for (sev, code), found in sorted(codes.items()):
        lines.append("  %s %s x%d" % (sev.upper(), code, len(found)))
        for f in found[:per_code]:
            lines.append("    frame %d %s: %s" % (f["frame"], f["flow"], f["message"]))
    return lines


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m dvtest.check", description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="pcap/pcapng file, a directory of them, or a run dir out/<utc>")
    parser.add_argument("--json", help="write the full result as JSON here")
    parser.add_argument("--ports-from", choices=["rig"], help="rig: own address from the file name, "
                        "USRP links from rig/config/xlxd.usrp.in, peers outside the rig hosts are errors")
    parser.add_argument("--jitter-ms", type=float, help="warn above this jitter p95 (default: one frame period)")
    parser.add_argument("--strict", action="store_true", help="warnings fail too")
    parser.add_argument("--per-code", type=int, default=3, help="findings printed per code (default 3)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    files = captures(args.target)
    if not files or not all(os.path.isfile(f) for f in files):
        print("dvcheck: no capture at %s" % args.target, file=sys.stderr)
        return 2
    rig = rig_config() if args.ports_from == "rig" else None
    windows = []
    if rig and os.path.isdir(args.target):
        windows = outages(args.target, rig["hosts"])
    results = []
    for path in files:
        try:
            results.append(check_file(path, rig, args.jitter_ms, windows))
        except (OSError, pcap.CaptureError) as exc:
            print("dvcheck: %s: %s" % (path, exc), file=sys.stderr)
            return 2
    differential(results)
    for result in results:
        print("\n".join(summary_lines(result, args.per_code)))
    errors = sum(r["errors"] for r in results)
    warnings = sum(r["warnings"] for r in results)
    print("dvcheck: %d file(s), %d error(s), %d warning(s)" % (len(results), errors, warnings))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"files": results, "errors": errors, "warnings": warnings}, fh, indent=1)
    return 1 if errors or (args.strict and warnings) else 0
