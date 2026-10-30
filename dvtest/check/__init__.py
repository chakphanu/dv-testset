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

from .. import divergences, pcap, target
from ..target import DEFAULT_SUBNET, HOST_OFFSETS
from . import ambed, dcs, dextra, dmr, dplus, iax2, m17, nxdn, p25, usrp, xlx, ysf
from .common import ERROR, FAMILY, WARN, Report, flow_of

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# capture file stem -> rig host key (dvtest.target.HOST_OFFSETS)
RIG_FILES = {"xlxd-ref": "xlxd_ref", "xlxd-sut": "xlxd_sut", "transcoder-ref": "ambed_ref",
             "transcoder-sut": "ambed_sut", "tester": "tester", "allstar": "allstar",
             "nxdn-ref": "nxdn_ref", "nxdn-sut": "nxdn_sut", "p25-ref": "p25_ref", "p25-sut": "p25_sut",
             "xlxd-peer-ref": "xlxd_peer_ref", "xlxd-peer-sut": "xlxd_peer_sut",
             "xlxd-peer2-ref": "xlxd_peer2_ref", "xlxd-peer2-sut": "xlxd_peer2_sut",
             "urfd": "urfd"}
# reference-class captures outside the <name>-ref naming: they join the
# reference side of a pair (DVT_REF_KIND=urfd runs URFd as the reference)
REFERENCES = {"urfd": "xlxd"}


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


NETEM_HOSTS = {"tester": ("tester",), "xlxd": ("xlxd_ref", "xlxd_sut"),
               "xlxd-ref": ("xlxd_ref",), "xlxd-sut": ("xlxd_sut",)}
SETTLE = 2.0


MARK_SETTLE = 1.0


# mark tags under which the xlxd peers' own flows may lack a closing frame
# (test_xlx_cut_stream_no_closing_frame); their windows carry PEER_SUFFIX and
# excuse only stream-end findings
PEER_CUT_MARKS = {"xlx-cut-stream"}
PEER_SUFFIX = " (peer)"


def marks(run_dir, hosts):
    """[(tester ip, t0, t1, "mark <tag>")] from control.log "mark begin
    <tag>" / "mark end <tag>" lines: a case brackets traffic it makes
    malformed on purpose. A window runs from the begin request to 1 s after
    the completed end (to the capture end without one), but no further
    than the next mark's begin."""
    path = os.path.join(run_dir, "control", "control.log")
    ip = hosts.get("tester")
    if not ip or not os.path.isfile(path):
        return []
    begun, out = {}, []
    with open(path) as fh:
        for line in fh:
            m = re.match(r"^(\S+Z) (?:(request) )?mark (begin|end) ([A-Za-z0-9._-]+)(?: -> (\S+))?$", line.strip())
            if not m or (m.group(2) is None) == (m.group(5) is None):
                continue
            ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
            edge, tag = m.group(3), m.group(4)
            if edge == "begin" and m.group(2):
                begun.setdefault(tag, ts)
            elif edge == "end" and m.group(5) == "0" and tag in begun:
                out.append((ip, begun.pop(tag), ts + MARK_SETTLE, "mark " + tag))
    out += [(ip, t0, float("inf"), "mark " + tag) for tag, t0 in begun.items()]
    # a case that cuts a stream on an XLX peer on purpose (no closing frame)
    # also marks the stream-end findings of the peers' own flows
    for ip_, t0, t1, label in list(out):
        if label[len("mark "):] in PEER_CUT_MARKS:
            out += [(hosts[k], t0, t1, label + PEER_SUFFIX) for k in sorted(hosts) if k.startswith("xlxd_peer")]
    # the 1 s tail of a window ends where the next mark begins, so back to
    # back cases each get their own tag
    out.sort(key=lambda w: w[1])
    for index in range(len(out) - 1):
        nxt = out[index + 1][1]
        if out[index][2] > nxt:
            out[index] = out[index][:2] + (max(nxt, out[index][1]),) + out[index][3:]
    return out


def impairments(run_dir, hosts):
    """[(ip, t0, t1, label)] of netem windows (run.sh --netem): impaired from
    the start when netem.txt's start block shows a netem qdisc on a host,
    and from each control.log "request netem <target> <spec>" until 2 s
    after the next completed change of that target."""
    windows, active = [], {}

    def close(target, ts):
        # "xlxd" also ends a start-up window of xlxd-ref / xlxd-sut
        for other in [t for t in active if set(NETEM_HOSTS[t]) <= set(NETEM_HOSTS[target])]:
            t0, label = active.pop(other)
            windows.extend((hosts[k], t0, ts + SETTLE, label) for k in NETEM_HOSTS[other] if k in hosts)

    path = os.path.join(run_dir, "netem.txt")
    if os.path.isfile(path):
        with open(path) as fh:
            text = fh.read().split("\n==== ")[0]
        for target, qdisc in re.findall(r"-- netem-(\S+)\n(qdisc netem [^\n]*)", text):
            if target in NETEM_HOSTS:
                spec = re.sub(r"^qdisc netem \S+ root (refcnt \d+ )?", "", qdisc).strip()
                active[target] = (float("-inf"), "netem %s %s" % (target, spec))
    path = os.path.join(run_dir, "control", "control.log")
    requested = {}
    if os.path.isfile(path):
        with open(path) as fh:
            for line in fh:
                m = re.match(r"^(\S+Z) (?:(request) )?netem (\S+) (.+?)(?: -> (\S+))?$", line.strip())
                if not m or m.group(3) not in NETEM_HOSTS or (m.group(2) is None) == (m.group(5) is None):
                    continue
                ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
                target, spec = m.group(3), m.group(4)
                if m.group(2):
                    requested[target] = ts
                elif m.group(5) == "0":
                    t0 = requested.pop(target, ts)
                    close(target, ts)
                    if spec != "off":
                        active[target] = (t0, "netem %s %s" % (target, spec))
    for target in list(active):
        close(target, float("inf"))
    return windows


# findings an injected impairment explains (netem loss, delay, reorder,
# dup): sequence gaps, duplicates, reorders and timing (jitter). Stream-end
# codes (a dropped last frame) are not included: the sniffer sits behind the
# qdisc, so a drop cannot be tied to the missing packet.
NETEM_CODES = re.compile(r"\.(\w+_)?(loss|dup|reorder)$|\.jitter$")
# sut vs ref under the same impairment: a netem-tagged code is an error when
# the sut shows more than NETEM_FACTOR x the ref count + NETEM_SLACK
NETEM_FACTOR, NETEM_SLACK = 2, 3


def _excuse(report, frame_ts, windows):
    """Errors on flows of a service the control channel had stopped become
    warnings tagged "[during control stop of X]"; inside a netem window only
    loss/dup/reorder/jitter findings on flows of the impaired host do, tagged
    "[during netem ...]"; inside a mark window errors on flows the tester
    sends do, tagged "[during mark <tag>]". All stay listed."""
    for index, f in enumerate(report.findings):
        ts = frame_ts.get(f.frame)
        if f.severity != ERROR or ts is None:
            continue
        # a sequence gap spans from the packet before it to where it showed
        start = report.gap_start.get(f.frame) or ts
        for ip, t0, t1, svc in windows:
            if not (start <= t1 and ts >= t0 and re.search(r"(^|[<>])%s[:<]" % re.escape(ip), f.flow)):
                continue
            if svc.startswith("mark "):
                if not f.flow.startswith(ip + ":"):
                    continue
                if svc.endswith(PEER_SUFFIX) and f.code not in END_CODES:
                    continue
                note = " [during %s]" % svc
            elif svc.startswith("netem "):
                if not NETEM_CODES.search(f.code):
                    continue
                note = " [during %s]" % svc
            else:
                note = " [during control stop of %s]" % svc
            report.findings[index] = f._replace(severity=WARN, message=f.message + note)
            break


PASSTHROUGH = "(passthrough: "
# an egress stream relays an ingress stream that starts up to this much
# earlier or ends up to this much later
RELAY_LEAD, RELAY_TAIL = 0.5, 1.0


def _ends(flow):
    """(sender ip, receiver ip) of a flow_of() name."""
    a, b = flow.split(">")
    return a.rsplit(":", 1)[0], b.rsplit(":", 1)[0]


def _passthrough(report, own):
    """A zero_fill stream sent by the capture host whose codec data arrived
    all zero in an overlapping ingress stream (same codec family, e.g. the
    DMR AMBE+2 slot of an XLX frame) is passthrough, not fill: the finding
    stays a warning and is tagged, so no_zero_fill does not raise it."""
    ingress = [s for s in report.spans if s["t0"] is not None and "<>" not in s["flow"]
               and _ends(s["flow"])[1] == own and _ends(s["flow"])[0] != own]
    for index, f in enumerate(report.findings):
        if not f.code.endswith(".zero_fill") or "<>" in f.flow or _ends(f.flow)[0] != own:
            continue
        span = next((s for s in report.spans if s["protocol"] == f.protocol and s["first_frame"] == f.frame
                     and s["flow"] == f.flow), None)
        family = FAMILY.get(f.protocol)
        if span is None or span["t0"] is None:
            continue
        for s in ingress:
            counts = s["codecs"].get(family)
            if counts and counts[0] and not counts[1] and \
                    s["t0"] - RELAY_LEAD <= span["t0"] <= (s["t1"] or s["t0"]) + RELAY_TAIL:
                source = _ends(s["flow"])[0]
                why = ("relayed from XLX peer %s, which sent the %s data all zero" % (source, family)
                       if s["protocol"] == "xlx" else
                       "its %s source %s sent the %s data all zero" % (s["protocol"], source, family))
                report.findings[index] = f._replace(message=f.message + " " + PASSTHROUGH + why + ")")
                break


def share_passthrough(results):
    """The same egress stream seen in another capture of the set (e.g. the
    sut's NXDN stream in nxdn-sut.pcapng, where its source is not visible)
    gets the passthrough tag its sender's capture found."""
    tags = {}
    for r in results:
        for f in r["findings"]:
            if f["code"].endswith(".zero_fill") and PASSTHROUGH in f["message"]:
                tags[(f["code"], f["flow"])] = f["message"][f["message"].index(PASSTHROUGH):].split(")")[0] + ")"
    for r in results:
        for f in r["findings"]:
            tag = tags.get((f["code"], f["flow"]))
            if tag and PASSTHROUGH not in f["message"]:
                f["message"] += " " + tag


def _routable_v4(ip):
    """An IPv4 address that is not loopback (multicast and broadcast count:
    their sender is a rig host)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and not a.is_loopback


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
    idle = False
    if rig and rig["hosts"].get(RIG_FILES.get(stem)):
        own = rig["hosts"][RIG_FILES[stem]]
        net = ipaddress.ip_network(rig["subnet"])
        # an idle service only sees bridge noise, some of it from the rig
        # subnet; IPv4 traffic with none of it in the subnet means the subnet
        # is wrong. No routable IPv4 at all (a fresh namespace sends only
        # IPv6 link-local or loopback noise) is an idle segment, not an error.
        v4 = [a for d in datagrams for a in (d.src, d.dst) if _routable_v4(a)]
        idle = not v4
        if v4 and not any(_in(a, net) for a in v4):
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
    _passthrough(report, own)
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
    result = {"file": path, "own": own, "read": stats, "foreign": foreign, "multicast": multicast,
              "other": dict(other), "protocols": protocols,
              "errors": sum(1 for f in report.findings if f.severity == ERROR),
              "warnings": sum(1 for f in report.findings if f.severity == WARN),
              "findings": [f._asdict() for f in report.findings]}
    if idle:
        result["idle"] = True
    return result


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


# errors that hold whatever the reference does: malformed packets the sut
# sends (sizes, CRCs, field layout) and sequence faults
ABSOLUTE = re.compile(r"\.((\w+_)?(loss|dup|reorder)|bad_\w+|unparseable|\w*crc|lich_\w+|flags_lich|facch_\w+"
                      r"|field_changed|eot_bit|burst_\w+|m17p_\w+|request_bad|reply_bad|std_codecs)$")
COMPAT = "(compat run: reference %s does not show it)"


def run_setting(run_dir, key):
    """A key=value setting from the first line of <run>/run.txt, or None."""
    try:
        with open(os.path.join(run_dir, "run.txt")) as fh:
            line = fh.readline()
    except OSError:
        return None
    for token in line.split():
        if token.startswith(key + "="):
            return token.split("=", 1)[1]
    return None


def differential(results, ref_kind="xlxd"):
    """Severity across a reference/system-under-test pair of captures
    (<name>-ref vs <name>-sut, every .N part of each): a code seen on both
    sides is upstream behaviour (warn); a code seen only on the sut is a
    regression (error) when the reference had its precondition (COMPARABLE),
    else a warning; errors only on the reference are warnings. Captures
    without a partner keep their severities. With ref_kind "urfd" (a
    compatibility run, not a gate) a sut-only finding is never escalated,
    and a sut-only error that is not ABSOLUTE becomes a warning (COMPAT)."""
    sides = {}
    for r in results:
        name, _, side = _stem(r).rpartition("-")
        if _stem(r) in REFERENCES:
            name, side = REFERENCES[_stem(r)], "ref"
        if side in ("ref", "sut") and name:
            sides.setdefault(name, {"ref": [], "sut": []})[side].append(r)
    for r in results:
        if _stem(r) in REFERENCES:
            # a reference host is not the system under test
            for f in r["findings"]:
                if f["severity"] == ERROR:
                    _mark(r, f, WARN, "(reference %s, not the system under test)" % _stem(r))
            r["errors"] = 0
            r["warnings"] = len(r["findings"])
    for name, pair in sides.items():
        if not pair["ref"] or not pair["sut"]:
            continue
        codes = {side: {f["code"] for r in pair[side] for f in r["findings"]} for side in pair}
        counts = {side: Counter(f["code"] for r in pair[side] for f in r["findings"]) for side in pair}
        for r in pair["sut"]:
            for f in r["findings"]:
                if "[during netem " in f["message"]:
                    # both sides ran under the same impairment: an error when
                    # the sut shows clearly more than the ref (NETEM_FACTOR,
                    # NETEM_SLACK) and the ref carried that protocol's streams
                    proto, want = f["code"].split(".")[0], counts["ref"].get(f["code"], 0)
                    if not any(x["protocols"].get(proto, {}).get("streams") for x in pair["ref"]):
                        f["message"] += " (not comparable on reference: no %s stream)" % proto
                    elif counts["sut"][f["code"]] > NETEM_FACTOR * want + NETEM_SLACK:
                        _mark(r, f, ERROR, "[sut %d vs ref %d under the same netem impairment, over %dx+%d]" % (
                            counts["sut"][f["code"]], want, NETEM_FACTOR, NETEM_SLACK))
                    else:
                        f["message"] += " (ref %d under the same impairment)" % want
                elif f["code"] in codes["ref"]:
                    _mark(r, f, WARN, "(also on reference: upstream behaviour)")
                elif f["code"] in COMPARABLE and not any(COMPARABLE[f["code"]][0](x) for x in pair["ref"]):
                    _mark(r, f, WARN, "(not comparable on reference: %s)" % COMPARABLE[f["code"]][1])
                elif ref_kind == "urfd":
                    if f["severity"] == ERROR and not ABSOLUTE.search(f["code"]):
                        _mark(r, f, WARN, COMPAT % ref_kind)
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


# Declared divergences of the system under test (DVT_SUT_DIVERGENCES, names
# and meaning in dvtest.divergences.TABLE) that change a wire finding:
# name -> (finding codes, message condition, severity when declared). They
# apply to findings sent by a system-under-test host on -sut captures; the
# other TABLE names change no wire finding.
END_CODES = ("dextra.no_last", "dextra.header_only", "dplus.no_last", "dplus.header_only", "dcs.no_last",
             "xlx.no_last", "xlx.header_only", "dmr.no_terminator", "dmr.header_only", "m17.no_eot",
             "nxdn.no_trailer", "p25.no_terminator", "ysf.no_terminator", "usrp.missing_unkey")
ZERO_FILL = tuple(p + ".zero_fill" for p in ("dextra", "dplus", "dcs", "xlx", "dmr", "m17", "nxdn", "p25", "ysf"))
DIVERGENCES = {
    # accepted: a 24 B extended OPEN without PCM from the sut is no error
    "ambed_extended_open_always": (("ambed.ext_open_non_pcm",), None, WARN),
    # the sut revokes a closed peer at once: no upstream client-cache excuse
    "immediate_revoke": (("dmr.after_close", "dmr.after_close_cache", "dmr.after_unlink", "m17.after_disc")
                         + tuple(p + ".after_unlink" for p in ("dextra", "dplus", "dcs", "xlx")), None, ERROR),
    # the sut never zero-fills cross-mode codec frames (zero data its source
    # already sent is passthrough, see _passthrough)
    "no_zero_fill": (ZERO_FILL, None, ERROR),
    # the sut ends a stream that goes silent with a synthesized terminator
    "synthesized_terminator": (END_CODES, "ended by timeout", ERROR),
}
# rig hosts that make up the system under test (the -sut peers such as
# nxdn-sut or xlxd-peer-sut are upstream software next to it)
SUT_HOSTS = ("xlxd_sut", "ambed_sut")
# without the declaration a sut finding of these codes is an error
UNDECLARED_ERROR = {"ambed_extended_open_always"}


def declared(env=None):
    """Declared names from DVT_SUT_DIVERGENCES or the target TOML key
    sut_divergences (dvtest.target.load; ValueError on an unknown name)."""
    return set(target.load(env).divergences)


def _sender(flow):
    return flow.split(":", 1)[0] if ">" in flow and "<>" not in flow else None


def apply_divergences(results, names, sut_hosts=()):
    """Severity of DIVERGENCES codes on -sut captures for findings whose
    sender is a system-under-test host (sut_hosts, else the capture's own
    address): a declared divergence sets the table severity and names it;
    an undeclared one in UNDECLARED_ERROR is an error."""
    for r in results:
        stem = _stem(r)
        if not stem.endswith("-sut") or "peer" in stem:
            continue
        hosts = set(sut_hosts) or {r["own"]}
        for f in r["findings"]:
            if _sender(f["flow"]) not in hosts:
                continue
            for name, (codes, condition, severity) in DIVERGENCES.items():
                if f["code"] not in codes or (condition and condition not in f["message"]):
                    continue
                if f["code"] in ZERO_FILL and PASSTHROUGH in f["message"]:
                    continue
                if name in names:
                    _mark(r, f, severity, "(declared divergence %s)" % name)
                elif name in UNDECLARED_ERROR:
                    _mark(r, f, ERROR, "(not declared: %s)" % name)
        r["errors"] = sum(1 for f in r["findings"] if f["severity"] == ERROR)
        r["warnings"] = sum(1 for f in r["findings"] if f["severity"] == WARN)


def _part(name):
    """Sort key: <svc>.pcapng, then <svc>.2.pcapng, <svc>.3.pcapng, ..."""
    stem, rest = name.split(".", 1)
    number = rest.split(".", 1)[0]
    return stem, int(number) if number.isdigit() else 1


# findings that are known, timing dependent xlxd behaviour: a warning
# whichever side shows them, except under a profile whose sut never does it
PROFILE_EXCUSED = {"m17.after_eot": m17.REPLAY_TAG}
STRICT_PROFILES = ("dvrouter",)


def apply_profile(results, profile):
    """Under a strict profile (dvrouter: a correct router never replays a
    stream), PROFILE_EXCUSED findings on -sut captures are errors; under
    the xlxd profile they stay warnings, after the differential too."""
    for r in results:
        sut = _stem(r).endswith("-sut")
        for f in r["findings"]:
            tag = PROFILE_EXCUSED.get(f["code"])
            if not tag or tag not in f["message"]:
                continue
            if profile in STRICT_PROFILES and sut:
                _mark(r, f, ERROR, "(profile %s: never expected)" % profile)
            elif f["severity"] == ERROR:
                _mark(r, f, WARN, "(profile %s)" % profile)
        r["errors"] = sum(1 for f in r["findings"] if f["severity"] == ERROR)
        r["warnings"] = sum(1 for f in r["findings"] if f["severity"] == WARN)


def captures(target):
    if os.path.isdir(target):
        sub = os.path.join(target, "capture")
        base = sub if os.path.isdir(sub) else target
        names = [n for n in os.listdir(base) if n.endswith((".pcap", ".pcapng", ".cap"))]
        # run.sh restarts a sniffer as <svc>.2.pcapng, <svc>.3.pcapng, ...
        return [os.path.join(base, n) for n in sorted(names, key=_part)]
    return [target]


def summary_lines(result, per_code=3):
    lines = []
    read = result["read"]
    lines.append("%s: %d packets, %d udp, own %s, foreign %d, multicast %d, unclassified %d%s" % (
        os.path.basename(result["file"]), read.get("packets", 0), read.get("udp", 0), result["own"],
        result["foreign"], result["multicast"], sum(result["other"].values()),
        "".join(", %s %d" % (k, v) for k, v in sorted(read.items()) if k not in ("packets", "udp", "non_udp", "non_ip"))
        + (", idle (no routable IPv4 traffic: nothing to check)" if result.get("idle") else "")))
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
    parser.add_argument("--divergences", help="comma list of declared sut divergences "
                        "(default: $DVT_SUT_DIVERGENCES)")
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
        windows = (outages(args.target, rig["hosts"]) + impairments(args.target, rig["hosts"])
                   + marks(args.target, rig["hosts"]))
    results = []
    for path in files:
        try:
            results.append(check_file(path, rig, args.jitter_ms, windows))
        except (OSError, pcap.CaptureError) as exc:
            print("dvcheck: %s: %s" % (path, exc), file=sys.stderr)
            return 2
    ref_kind = (run_setting(args.target, "ref_kind") if os.path.isdir(args.target) else None) \
        or os.environ.get("DVT_REF_KIND") or "xlxd"
    differential(results, ref_kind)
    try:
        names = declared() if args.divergences is None else divergences.parse(args.divergences)
    except ValueError as exc:
        print("dvcheck: %s" % exc, file=sys.stderr)
        return 2
    sut_hosts = {rig["hosts"][key] for key in SUT_HOSTS if key in rig["hosts"]} if rig else ()
    share_passthrough(results)
    apply_divergences(results, names, sut_hosts)
    try:
        profile = target.load().sut_profile or "xlxd"
    except ValueError:
        profile = "xlxd"
    apply_profile(results, profile)
    for result in results:
        print("\n".join(summary_lines(result, args.per_code)))
    errors = sum(r["errors"] for r in results)
    warnings = sum(r["warnings"] for r in results)
    print("dvcheck: %d file(s), %d error(s), %d warning(s)" % (len(results), errors, warnings))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"files": results, "errors": errors, "warnings": warnings}, fh, indent=1)
    return 1 if errors or (args.strict and warnings) else 0
