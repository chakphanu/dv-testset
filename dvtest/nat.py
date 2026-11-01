# SPDX-License-Identifier: MIT
"""NAT rig library (profile `nat`, docs/web-nat-cases.md, docs/rig.md "NAT profile").

The pure parts of the NAT router container: the control protocol, the iptables rules of each NAT
type, the classification of a NAT from probe results, and the tester-side client. Stdlib only.

NAT types (RFC 4787 terms; the first word is the mapping, the second the filtering):

  full_cone         a static 1:1 NAT (SNAT of the browser to a dedicated outside alias address plus
                    DNAT of everything sent to that alias back to the browser): endpoint-independent
                    mapping AND endpoint-independent filtering
  port_restricted   MASQUERADE on conntrack (the Linux default): the mapping keeps the source port and
                    is the same for every destination (endpoint-independent), a reply is accepted only
                    from the address AND port the browser sent to (address-and-port-dependent filtering)
  symmetric         MASQUERADE --random: a new mapping per destination (address-and-port-dependent
                    mapping), same filtering as above
  none              no translation rules at all (the inside addresses are not routable outside: the
                    browser cannot reach anything, which is what a case of the router itself uses)

The control channel is one JSON object per line over TCP (one request per connection):

  {"op": "nat_mode", "mode": "full_cone|port_restricted|symmetric|none"}
  {"op": "flush"}                                     delete the UDP conntrack entries (not TCP: the
                                                      Playwright websocket and the page's HTTPS flows
                                                      live in conntrack too)
  {"op": "blackhole", "state": "on|off"}              drop every UDP datagram between inside and outside
  {"op": "blackhole", "state": "for", "seconds": 3}   same, switched off by the router after the time
  {"op": "drop_direct", "state": "on|off"}            drop UDP from inside to the SUT's media address:port
                                                      (only a TURN relay path can then work)
  {"op": "status"}
  {"op": "reset"}                                     port_restricted, no blackhole, no drop_direct, flush

The answer is {"ok": true, ...} or {"ok": false, "error": "..."}.
"""
import json
import re
import socket
import threading
import time
from collections import namedtuple

MODES = ("full_cone", "port_restricted", "symmetric", "none")
OPS = ("nat_mode", "flush", "blackhole", "drop_direct", "status", "reset")
MAX_LINE = 4096
MAX_BLACKHOLE_S = 300.0

CHAIN_PRE = "DVT_PRE"          # nat PREROUTING: the control forwards (always)
CHAIN_MODE_PRE = "DVT_MODE_PRE"    # nat PREROUTING: the NAT type's inbound rules
CHAIN_MODE_POST = "DVT_MODE_POST"  # nat POSTROUTING: the NAT type's outbound rules
CHAIN_FWD = "DVT_FWD"          # filter FORWARD: blackhole and drop_direct
C_BLACKHOLE_OUT, C_BLACKHOLE_IN, C_DROP_DIRECT = "dvt-blackhole-out", "dvt-blackhole-in", "dvt-drop-direct"

Topology = namedtuple("Topology", "inside_net out_ip in_ip alias_ip browser_ip sut_ip media_port forwards out_if in_if out_prefix")

_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_CIDR = re.compile(r"^(\d{1,3}\.){3}\d{1,3}/(\d|[12]\d|3[0-2])$")


class NatError(ValueError):
    """A request or a setting the router refuses (the message goes back to the caller)."""


def valid_ip(text):
    m = _IPV4.match(text or "")
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


def need_ip(text, what):
    if not valid_ip(text):
        raise NatError("%s must be an IPv4 address (got %r)" % (what, text))
    return text


# ------------------------------------------------------------------ control protocol

def encode(obj):
    return (json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")


def parse_request(line):
    """One request line (bytes or str) -> a normalised dict {'op': ..., ...}. NatError for anything
    that is not exactly one of the documented requests (unknown keys are refused too)."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE:
            raise NatError("request longer than %d bytes" % MAX_LINE)
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise NatError("request is not ASCII") from exc
    elif len(line) > MAX_LINE:
        raise NatError("request longer than %d bytes" % MAX_LINE)
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise NatError("request is not JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
        raise NatError("request must be an object with an 'op'")
    op = obj["op"]
    if op not in OPS:
        raise NatError("unknown op %r (known: %s)" % (op, ", ".join(OPS)))
    allowed = {"nat_mode": {"op", "mode"}, "blackhole": {"op", "state", "seconds"},
               "drop_direct": {"op", "state"}, "flush": {"op"}, "status": {"op"}, "reset": {"op"}}[op]
    extra = sorted(set(obj) - allowed)
    if extra:
        raise NatError("unknown key %s for op %s" % (", ".join(extra), op))
    if op == "nat_mode":
        if obj.get("mode") not in MODES:
            raise NatError("mode must be one of %s" % ", ".join(MODES))
    elif op == "drop_direct":
        if obj.get("state") not in ("on", "off"):
            raise NatError("state must be on or off")
    elif op == "blackhole":
        state = obj.get("state")
        if state not in ("on", "off", "for"):
            raise NatError("state must be on, off or for")
        if state == "for":
            sec = obj.get("seconds")
            if isinstance(sec, bool) or not isinstance(sec, (int, float)) or not 0 < sec <= MAX_BLACKHOLE_S:
                raise NatError("seconds must be a number in (0, %g]" % MAX_BLACKHOLE_S)
        elif "seconds" in obj:
            raise NatError("seconds only goes with state 'for'")
    return obj


# ------------------------------------------------------------------ iptables rules

def parse_ip_addr(text):
    """`ip -o -4 addr show` -> {ip: interface name}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/\d+", line)
        if m:
            out[m.group(2)] = m.group(1).split("@")[0]
    return out


def make_topology(inside_net, out_ip, in_ip, alias_ip, browser_ip, sut_ip, media_port=50000, forwards=(3000, 3001),
                  out_if="eth0", in_if="eth1", out_prefix=24):
    if not _CIDR.match(inside_net or ""):
        raise NatError("inside_net must be a CIDR (got %r)" % inside_net)
    for what, ip in (("out_ip", out_ip), ("in_ip", in_ip), ("alias_ip", alias_ip), ("browser_ip", browser_ip), ("sut_ip", sut_ip)):
        need_ip(ip, what)
    if not 1 <= int(media_port) <= 65535:
        raise NatError("media_port out of range")
    for port in forwards:
        if not 1 <= int(port) <= 65535:
            raise NatError("forward port out of range")
    for name in (out_if, in_if):
        if not re.match(r"^[A-Za-z0-9_.-]{1,15}$", name):
            raise NatError("bad interface name %r" % name)
    return Topology(inside_net, out_ip, in_ip, alias_ip, browser_ip, sut_ip, int(media_port), tuple(int(p) for p in forwards),
                    out_if, in_if, int(out_prefix))


def setup_commands(t):
    """argv lists run once at start: the alias address, the chains with their jumps, the control forwards
    (outside address:port -> the browser's port, TCP only: the Playwright websocket and the probe server)."""
    cmds = [["ip", "addr", "add", "%s/%d" % (t.alias_ip, t.out_prefix), "dev", t.out_if],
            ["iptables", "-P", "FORWARD", "ACCEPT"]]
    for table, chain in (("nat", CHAIN_PRE), ("nat", CHAIN_MODE_PRE), ("nat", CHAIN_MODE_POST), ("filter", CHAIN_FWD)):
        cmds.append(["iptables", "-t", table, "-N", chain])
    cmds += [
        ["iptables", "-t", "nat", "-A", "PREROUTING", "-j", CHAIN_PRE],
        ["iptables", "-t", "nat", "-A", "PREROUTING", "-j", CHAIN_MODE_PRE],
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-j", CHAIN_MODE_POST],
        ["iptables", "-t", "filter", "-I", "FORWARD", "1", "-j", CHAIN_FWD],
    ]
    for port in t.forwards:
        cmds.append(["iptables", "-t", "nat", "-A", CHAIN_PRE, "-i", t.out_if, "-d", t.out_ip, "-p", "tcp", "--dport", str(port),
                     "-j", "DNAT", "--to-destination", "%s:%d" % (t.browser_ip, port)])
    return cmds


def mode_commands(t, mode):
    """argv lists that make the NAT type `mode`: both mode chains are emptied, then filled."""
    if mode not in MODES:
        raise NatError("unknown mode %r" % mode)
    cmds = [["iptables", "-t", "nat", "-F", CHAIN_MODE_PRE], ["iptables", "-t", "nat", "-F", CHAIN_MODE_POST]]
    post = ["iptables", "-t", "nat", "-A", CHAIN_MODE_POST]
    if mode == "full_cone":
        cmds.append(post + ["-s", t.browser_ip, "-o", t.out_if, "-j", "SNAT", "--to-source", t.alias_ip])
        cmds.append(["iptables", "-t", "nat", "-A", CHAIN_MODE_PRE, "-i", t.out_if, "-d", t.alias_ip, "-j", "DNAT",
                     "--to-destination", t.browser_ip])
    elif mode == "port_restricted":
        cmds.append(post + ["-s", t.inside_net, "-o", t.out_if, "-j", "MASQUERADE"])
    elif mode == "symmetric":
        cmds.append(post + ["-s", t.inside_net, "-o", t.out_if, "-p", "udp", "-j", "MASQUERADE", "--random"])
        cmds.append(post + ["-s", t.inside_net, "-o", t.out_if, "-j", "MASQUERADE"])
    return cmds


def blackhole_rules(t):
    """[(comment, rule words)] for the chain DVT_FWD: UDP both ways between inside and outside."""
    return [(C_BLACKHOLE_OUT, ["-i", t.in_if, "-o", t.out_if, "-p", "udp"]),
            (C_BLACKHOLE_IN, ["-i", t.out_if, "-o", t.in_if, "-p", "udp"])]


def drop_direct_rule(t):
    """The browser's UDP to the SUT's media address:port (ICE checks and media); a TURN relay path does not
    cross this router (coturn is on the outside)."""
    return (C_DROP_DIRECT, ["-i", t.in_if, "-o", t.out_if, "-p", "udp", "-d", t.sut_ip, "--dport", str(t.media_port)])


def rule_argv(action, rule):
    comment, words = rule
    return ["iptables", "-t", "filter", action, CHAIN_FWD] + words + ["-m", "comment", "--comment", comment, "-j", "DROP"]


def parse_counters(text):
    """`iptables -t filter -L DVT_FWD -n -v -x` -> {comment: packets}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+\d+\s+DROP\s.*?/\*\s*(\S+)\s*\*/", line)
        if m:
            out[m.group(2)] = int(m.group(1))
    return out


# ------------------------------------------------------------------ the router

class Router:
    """State and actions of the NAT router. `run(argv)` -> (returncode, output) is injected (subprocess in
    the container, a recorder in the unit tests)."""

    def __init__(self, topology, run, clock=time.time):
        self.t = topology
        self.run = run
        self.clock = clock
        self.lock = threading.RLock()
        self.mode = "none"
        self.blackhole_on = False
        self.blackhole_until = None
        self.drop_direct_on = False
        self.last_blackhole = None
        self._timer = None
        self._t_on = None

    def _must(self, argv):
        rc, out = self.run(argv)
        if rc != 0:
            raise NatError("%s failed (%d): %s" % (" ".join(argv[:4]), rc, (out or "").strip()[:200]))
        return out

    def start(self):
        with self.lock:
            for argv in setup_commands(self.t):
                rc, out = self.run(argv)
                if rc != 0 and "exist" not in (out or ""):
                    raise NatError("setup %s failed (%d): %s" % (" ".join(argv[:5]), rc, (out or "").strip()[:200]))
            self.set_mode("port_restricted")

    def set_mode(self, mode):
        with self.lock:
            for argv in mode_commands(self.t, mode):
                self._must(argv)
            self.mode = mode
            self.flush()
            return {"mode": mode}

    def flush(self):
        """UDP conntrack entries only (TCP flows of the control path and the page stay)."""
        with self.lock:
            rc, out = self.run(["conntrack", "-D", "-p", "udp"])
            if rc not in (0, 1):                    # 1 = nothing to delete
                raise NatError("conntrack failed (%d): %s" % (rc, (out or "").strip()[:200]))
            m = re.search(r"(\d+) flow entries have been deleted", out or "")
            return {"deleted": int(m.group(1)) if m else 0}

    def _rule(self, rule, want):
        have = self.run(rule_argv("-C", rule))[0] == 0
        if want and not have:
            self._must(rule_argv("-A", rule))
        elif have and not want:
            self._must(rule_argv("-D", rule))

    def _counters(self):
        rc, out = self.run(["iptables", "-t", "filter", "-L", CHAIN_FWD, "-n", "-v", "-x"])
        return parse_counters(out or "") if rc == 0 else {}

    def set_blackhole(self, on, seconds=None):
        with self.lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if on:
                if not self.blackhole_on:
                    for rule in blackhole_rules(self.t):
                        self._rule(rule, True)
                    self.blackhole_on = True
                    self._t_on = self.clock()
                self.blackhole_until = self.clock() + seconds if seconds else None
                if seconds:
                    self._timer = threading.Timer(seconds, self.set_blackhole, args=(False,))
                    self._timer.daemon = True
                    self._timer.start()
                return {"blackhole": True, "until": self.blackhole_until}
            if self.blackhole_on:
                counters = self._counters()
                for rule in blackhole_rules(self.t):
                    self._rule(rule, False)
                self.blackhole_on = False
                self.blackhole_until = None
                self.last_blackhole = {"t_on": self._t_on, "t_off": self.clock(),
                                       "dropped_out": counters.get(C_BLACKHOLE_OUT, 0),
                                       "dropped_in": counters.get(C_BLACKHOLE_IN, 0)}
            return {"blackhole": False, "last": self.last_blackhole}

    def set_drop_direct(self, on):
        with self.lock:
            self._rule(drop_direct_rule(self.t), on)
            self.drop_direct_on = on
            return {"drop_direct": on}

    def status(self):
        with self.lock:
            counters = self._counters()
            rc, out = self.run(["cat", "/proc/sys/net/netfilter/nf_conntrack_count"])
            n = int(out.strip()) if rc == 0 and (out or "").strip().isdigit() else None
            return {"mode": self.mode, "blackhole": self.blackhole_on, "blackhole_until": self.blackhole_until,
                    "drop_direct": self.drop_direct_on, "last_blackhole": self.last_blackhole,
                    "dropped": {"blackhole_out": counters.get(C_BLACKHOLE_OUT, 0), "blackhole_in": counters.get(C_BLACKHOLE_IN, 0),
                                "drop_direct": counters.get(C_DROP_DIRECT, 0)},
                    "conntrack_entries": n, "time": self.clock()}

    def handle(self, req):
        """A parsed request -> the answer dict (always has 'ok')."""
        try:
            op = req["op"]
            if op == "nat_mode":
                body = self.set_mode(req["mode"])
            elif op == "flush":
                body = self.flush()
            elif op == "blackhole":
                body = self.set_blackhole(req["state"] != "off", req.get("seconds") if req["state"] == "for" else None)
            elif op == "drop_direct":
                body = self.set_drop_direct(req["state"] == "on")
            elif op == "reset":
                self.set_blackhole(False)
                self.set_drop_direct(False)
                body = self.set_mode("port_restricted")
            else:
                body = self.status()
        except NatError as exc:
            return {"ok": False, "error": str(exc)}
        out = {"ok": True}
        out.update(body)
        return out


# ------------------------------------------------------------------ classification

EI, AD, APD = "endpoint-independent", "address-dependent", "address-and-port-dependent"


def classify(mapped, unsolicited):
    """NAT type from probe results.

    mapped: {'a1': (ip, port), 'a2': (ip, port), 'b1': (ip, port)} - the address:port a reflector saw for
      one local socket sent to reflector A port 1 (a1), reflector A port 2 (a2) and reflector B (b1).
    unsolicited: {'third_address': bool, 'same_address_other_port': bool} - whether a datagram from an
      address the socket never contacted / from the contacted address but another port reached the socket.

    Returns {'mapping', 'filtering', 'type'} with RFC 4787 terms; type is full_cone, restricted_cone,
    port_restricted, symmetric or unknown."""
    for key in ("a1", "a2", "b1"):
        if not mapped.get(key):
            return {"mapping": "unknown", "filtering": "unknown", "type": "unknown"}
    a1, a2, b1 = (tuple(mapped[k]) for k in ("a1", "a2", "b1"))
    if a1 == a2 == b1:
        mapping = EI
    elif a1 == a2:
        mapping = AD
    else:
        mapping = APD
    third, other_port = bool(unsolicited.get("third_address")), bool(unsolicited.get("same_address_other_port"))
    if third and other_port:
        filtering = EI
    elif other_port and not third:
        filtering = AD
    elif not third and not other_port:
        filtering = APD
    else:
        filtering = "unknown"           # a third address passes but the contacted address does not: no such NAT
    if mapping == EI:
        typ = {EI: "full_cone", AD: "restricted_cone", APD: "port_restricted"}.get(filtering, "unknown")
    elif filtering == "unknown":
        typ = "unknown"
    else:
        typ = "symmetric"
    return {"mapping": mapping, "filtering": filtering, "type": typ}


# ------------------------------------------------------------------ reflector and probe wire formats

REFLECT_REQ = b"DVT-NAT-PROBE "
REFLECT_REP = b"DVT-NAT-REPLY "
POKE_REQ = b"DVT-NAT-POKE "


def reflect_reply(request, addr):
    """The reflector's answer to a probe datagram: the token it carried and the source address:port it was
    seen from (the NAT's mapping); None when the datagram is not a probe."""
    if not request.startswith(REFLECT_REQ):
        return None
    token = request[len(REFLECT_REQ):].decode("ascii", "replace").strip()[:64]
    return REFLECT_REP + json.dumps({"token": token, "src": [addr[0], addr[1]]}, separators=(",", ":")).encode("ascii")


def parse_reflect_reply(data):
    """-> (token, (ip, port)) or None."""
    if not data.startswith(REFLECT_REP):
        return None
    try:
        obj = json.loads(data[len(REFLECT_REP):].decode("ascii"))
        return obj["token"], (obj["src"][0], int(obj["src"][1]))
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def parse_poke(data):
    """A poke datagram {'to': [ip, port], 'from_port': n, 'data': text} -> dict, or None."""
    if not data.startswith(POKE_REQ):
        return None
    try:
        obj = json.loads(data[len(POKE_REQ):].decode("ascii"))
        need_ip(obj["to"][0], "to")
        int(obj["to"][1])
        int(obj["from_port"])
        return {"to": (obj["to"][0], int(obj["to"][1])), "from_port": int(obj["from_port"]), "data": str(obj.get("data", "x"))[:64]}
    except (ValueError, KeyError, IndexError, TypeError, NatError):
        return None


# ------------------------------------------------------------------ tester side

def call(addr, request, timeout=10.0):
    """One request line to a JSON-line server (router control or probe server) -> the answer dict."""
    with socket.create_connection(addr, timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(encode(request))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    if not buf:
        raise NatError("no answer from %s:%d" % addr)
    return json.loads(buf.decode("ascii"))


class NatClient:
    """The tester's view of the NAT rig: router control, the probe server in the browser's namespace, the
    two reflectors. All addresses come from the environment (cases/nat_support.py)."""

    def __init__(self, control, probe, reflector_a, reflector_b, ports=(3479, 3480), poke_port=3481):
        self.control, self.probe = control, probe
        self.a, self.b = reflector_a, reflector_b
        self.ports, self.poke_port = tuple(ports), poke_port

    def ctl(self, op, **kw):
        ans = call(self.control, dict(op=op, **kw))
        if not ans.get("ok"):
            raise NatError("router: %s" % ans.get("error"))
        return ans

    def mode(self, mode):
        return self.ctl("nat_mode", mode=mode)

    def status(self):
        return self.ctl("status")

    def poke(self, reflector, to, from_port=None, timeout=2.0):
        """Ask a reflector to send a datagram from its poke socket (another port than the one the browser
        contacted) to `to`."""
        body = POKE_REQ + json.dumps({"to": list(to), "from_port": from_port or self.poke_port, "data": "unsolicited"},
                                     separators=(",", ":")).encode("ascii")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(body, (reflector, self.ports[0]))
            try:
                return s.recvfrom(256)[0] == b"ok"
            except socket.timeout:
                return False

    def classify(self, local_port=41000, settle=0.6):
        """The self-test: returns {'mapped', 'unsolicited', 'classification'}.

        Part 1: one inside socket sends a probe to reflector A (two ports) and B; each reflector says which
        source address:port it saw: that is the mapping per destination.
        Part 2: a fresh inside socket sends to reflector A port 1 only; an unsolicited datagram from the
        tester (a third address) and one from reflector A's other port are sent to the mapped address; the
        probe server says which of them reached the socket."""
        targets = [[self.a, self.ports[0]], [self.a, self.ports[1]], [self.b, self.ports[0]]]
        r1 = call(self.probe, {"op": "probe_start", "local_port": local_port, "targets": targets})
        call(self.probe, {"op": "probe_stop"})
        if not r1.get("ok"):
            raise NatError("probe: %s" % r1.get("error"))
        res = r1["mapped"]
        mapped = {"a1": res[0], "a2": res[1], "b1": res[2]}
        time.sleep(0.2)
        r2 = call(self.probe, {"op": "probe_start", "local_port": local_port + 1, "targets": [targets[0]]})
        if not r2.get("ok") or not r2["mapped"][0]:
            call(self.probe, {"op": "probe_stop"})
            raise NatError("probe 2 got no reflection: %s" % (r2,))
        dst = tuple(r2["mapped"][0])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"DVT-NAT-UNSOLICITED third", dst)
        self.poke(self.a, dst)
        time.sleep(settle)
        r3 = call(self.probe, {"op": "probe_stop"})
        got = {tuple(x["from"])[0] for x in r3.get("received", [])}
        third = any(x["from"][0] != self.a and x["from"][0] != self.b for x in r3.get("received", []))
        other = any(x["from"][0] == self.a and x["from"][1] == self.poke_port for x in r3.get("received", []))
        unsolicited = {"third_address": third, "same_address_other_port": other}
        return {"mapped": mapped, "mapped_single": list(dst), "unsolicited": unsolicited,
                "senders": sorted(got), "classification": classify(mapped, unsolicited)}
