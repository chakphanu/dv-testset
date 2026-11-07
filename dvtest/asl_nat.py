# SPDX-License-Identifier: MIT
"""Source NAT in a lab node's own network namespace (profile asl-edge + asl-nat, case T5n,
docs/asl-edge-results.md "T5n", docs/asl-lab.md "T5n").

The edge's identity rule (dv-router docs/design/allstar.md, "Identity"): the SOURCE IP of a NEW is compared
with the registered / static address of the claimed node, the UDP source port is ignored; a validated
refusal is ACCEPT followed by HANGUP, never ANSWER. A caller behind a NAT shows the edge the NAT's address,
not its own, so the edge must refuse it. This module makes the edge see another address for a REAL app_rpt
node (asl-a), without any packet forging: a sidecar with CAP_NET_ADMIN shares asl-a's network namespace and
keeps one SNAT rule for asl-a's IAX2 socket towards the edge (netfilter, the same iptables and conntrack
tools as the NAT router of the web profile, dvtest/nat.py). Asterisk sends its own packets from its own
socket; only the kernel rewrites the source on the way out and the destination of the replies on the way in.

  address   SNAT to <alias ip>:<map port>     what a NAT with another public address does: the edge must refuse
  port      SNAT to <own ip>:<map port>       a NAT that only remaps the port: same address, the edge must answer
  off       no rule                           the plain lab (T5, test_t5_right_source_ip_is_accepted)

The alias address is added to the node's interface (a second address of the same host, so the replies of the
edge find their way back through conntrack). This is NOT a routed NAT with two networks: there is no second
hop and no mapping timeout of a router; what it shares with a real NAT is the thing under test, a caller whose
source address and port as seen by the edge differ from its own.

The control protocol is one JSON object per line over TCP, one request per connection, like dvtest/nat.py:

  {"op": "snat", "mode": "off|address|port"}   set the mode (also deletes the UDP conntrack entries, so that the
                                               next datagram is a new flow and the rule applies)
  {"op": "status"}                             mode, the source the edge sees, packets the rule translated
  {"op": "reset"}                              mode off

Stdlib only. Pure parts are unit-tested (cases/unit/test_asl_nat.py).
"""
import json
import re
import threading
from collections import namedtuple

from . import nat

MODES = ("off", "address", "port")
OPS = ("snat", "status", "reset")
CHAIN = "DVT_ASL_NAT"
COMMENT = "dvt-asl-nat"

LocalNat = namedtuple("LocalNat", "local_ip alias_ip peer_ip port map_port out_if out_prefix")

_IFNAME = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


def make_local_nat(local_ip, alias_ip, peer_ip, port=4569, map_port=44569, out_if="eth0", out_prefix=24):
    """Validated settings. local_ip: the lab node, alias_ip: the address the edge sees in mode `address`,
    peer_ip: the edge, port: the IAX2 port of node and edge, map_port: the source port the edge sees."""
    for what, ip in (("local_ip", local_ip), ("alias_ip", alias_ip), ("peer_ip", peer_ip)):
        nat.need_ip(ip, what)
    if len({local_ip, alias_ip, peer_ip}) != 3:
        raise nat.NatError("local_ip, alias_ip and peer_ip must differ")
    for what, value in (("port", port), ("map_port", map_port)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
            raise nat.NatError("%s must be an integer in 1024..65535" % what)
    if map_port == port:
        raise nat.NatError("map_port must differ from port (the NAT must change the source port)")
    if not _IFNAME.match(out_if or ""):
        raise nat.NatError("bad interface name %r" % (out_if,))
    if isinstance(out_prefix, bool) or not isinstance(out_prefix, int) or not 8 <= out_prefix <= 30:
        raise nat.NatError("out_prefix must be 8..30")
    return LocalNat(local_ip, alias_ip, peer_ip, port, map_port, out_if, out_prefix)


def setup_commands(t):
    """argv lists run once: the alias address on the node's interface, the chain and its jump from POSTROUTING."""
    return [["ip", "addr", "add", "%s/%d" % (t.alias_ip, t.out_prefix), "dev", t.out_if],
            ["iptables", "-t", "nat", "-N", CHAIN],
            ["iptables", "-t", "nat", "-A", "POSTROUTING", "-j", CHAIN]]


def source_seen(t, mode):
    """(ip, port) the edge sees for the node's IAX2 socket in `mode`; (own ip, None) for off: the port is the
    node's own and not set by the rig."""
    if mode == "address":
        return t.alias_ip, t.map_port
    if mode == "port":
        return t.local_ip, t.map_port
    if mode == "off":
        return t.local_ip, None
    raise nat.NatError("unknown mode %r" % (mode,))


def mode_commands(t, mode):
    """argv lists that make the mode: the chain is emptied, then it gets its one rule (none for off). The rule
    matches the node's IAX2 socket towards the edge only: DNS, the lab agent and every other flow stay as they are."""
    ip, port = source_seen(t, mode)
    cmds = [["iptables", "-t", "nat", "-F", CHAIN]]
    if mode != "off":
        cmds.append(["iptables", "-t", "nat", "-A", CHAIN, "-s", t.local_ip, "-d", t.peer_ip, "-p", "udp",
                     "--sport", str(t.port), "--dport", str(t.port), "-o", t.out_if,
                     "-m", "comment", "--comment", COMMENT, "-j", "SNAT", "--to-source", "%s:%d" % (ip, port)])
    return cmds


def parse_snat_packets(text):
    """`iptables -t nat -L DVT_ASL_NAT -n -v -x` -> packets the SNAT rule translated (0 without a rule)."""
    total = 0
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)\s+\d+\s+SNAT\s", line)
        if m:
            total += int(m.group(1))
    return total


# ------------------------------------------------------------------ control protocol

def _json_line(line):
    if isinstance(line, bytes):
        if len(line) > nat.MAX_LINE:
            raise nat.NatError("request longer than %d bytes" % nat.MAX_LINE)
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise nat.NatError("request is not ASCII") from exc
    elif len(line) > nat.MAX_LINE:
        raise nat.NatError("request longer than %d bytes" % nat.MAX_LINE)
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise nat.NatError("request is not JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
        raise nat.NatError("request must be an object with an 'op'")
    return obj


def parse_request(line):
    """One request line -> a dict {'op': ...}. NatError for anything but exactly a documented request."""
    obj = _json_line(line)
    op = obj["op"]
    if op not in OPS:
        raise nat.NatError("unknown op %r (known: %s)" % (op, ", ".join(OPS)))
    allowed = {"snat": {"op", "mode"}, "status": {"op"}, "reset": {"op"}}[op]
    extra = sorted(set(obj) - allowed)
    if extra:
        raise nat.NatError("unknown key %s for op %s" % (", ".join(extra), op))
    if op == "snat" and obj.get("mode") not in MODES:
        raise nat.NatError("mode must be one of %s" % ", ".join(MODES))
    return obj


class Snat:
    """State and actions of the sidecar. `run(argv)` -> (returncode, output) is injected (subprocess in the
    container, a recorder in the unit tests)."""

    def __init__(self, local_nat, run):
        self.t = local_nat
        self.run = run
        self.lock = threading.RLock()
        self.mode = "off"

    def _must(self, argv):
        rc, out = self.run(argv)
        if rc != 0:
            raise nat.NatError("%s failed (%d): %s" % (" ".join(argv[:4]), rc, (out or "").strip()[:200]))
        return out

    def start(self):
        with self.lock:
            for argv in setup_commands(self.t):
                rc, out = self.run(argv)
                if rc != 0 and "exist" not in (out or ""):
                    raise nat.NatError("setup %s failed (%d): %s" % (" ".join(argv[:5]), rc, (out or "").strip()[:200]))
            self.set_mode("off")

    def flush(self):
        """UDP conntrack entries only: an entry made before the rule keeps its old (untranslated) mapping."""
        rc, out = self.run(["conntrack", "-D", "-p", "udp"])
        if rc not in (0, 1):                                  # 1 = nothing to delete
            raise nat.NatError("conntrack failed (%d): %s" % (rc, (out or "").strip()[:200]))

    def set_mode(self, mode):
        with self.lock:
            for argv in mode_commands(self.t, mode):
                self._must(argv)
            self.mode = mode
            self.flush()
            return {"mode": mode, "source": list(source_seen(self.t, mode))}

    def status(self):
        with self.lock:
            rc, out = self.run(["iptables", "-t", "nat", "-L", CHAIN, "-n", "-v", "-x"])
            return {"mode": self.mode, "source": list(source_seen(self.t, self.mode)),
                    "snat_packets": parse_snat_packets(out) if rc == 0 else None}

    def handle(self, req):
        try:
            op = req["op"]
            if op == "snat":
                body = self.set_mode(req["mode"])
            elif op == "reset":
                body = self.set_mode("off")
            else:
                body = self.status()
        except nat.NatError as exc:
            return {"ok": False, "error": str(exc)}
        out = {"ok": True}
        out.update(body)
        return out


# ------------------------------------------------------------------ tester side

class Client:
    """The tester's view: the sidecar's control address (host:port)."""

    def __init__(self, addr):
        self.addr = addr

    def ctl(self, op, **kw):
        ans = nat.call(self.addr, dict(op=op, **kw))
        if not ans.get("ok"):
            raise nat.NatError("asl-nat: %s" % ans.get("error"))
        return ans

    def mode(self, mode):
        return self.ctl("snat", mode=mode)

    def status(self):
        return self.ctl("status")

    def reset(self):
        return self.ctl("reset")


def client_from_env(env):
    """Client for DVT_ASL_NAT_CTL ("host:port") when DVT_ASL_EDGE_NAT=1, else None."""
    if env.get("DVT_ASL_EDGE_NAT") != "1":
        return None
    host, _, port = (env.get("DVT_ASL_NAT_CTL") or "").rpartition(":")
    if not nat.valid_ip(host) or not port.isdigit():
        return None
    return Client((host, int(port)))


# ------------------------------------------------------------------ reading the edge's capture

def _ip(addr):
    return addr.rsplit(":", 1)[0]


def _port(addr):
    return int(addr.rsplit(":", 1)[1])


def edge_reaction(doc, edge_ip, caller_ip):
    """What the edge did to the NEW frames of `caller_ip` in an iax2_frames.extract() document (a capture taken
    next to the edge): counts of NEW, ACCEPT, ANSWER and HANGUP between the two addresses, the source ports of
    the NEWs, and `refused`: at least one NEW, an ACCEPT, a HANGUP after the first ACCEPT and no ANSWER.
    `answered`: an ANSWER came back."""
    types = doc.get("types", {})

    def between(name, src, dst):
        return [f for f in types.get(name, []) if _ip(f["src"]) == src and _ip(f["dst"]) == dst]

    new = between("iax.NEW", caller_ip, edge_ip)
    accept = between("iax.ACCEPT", edge_ip, caller_ip)
    answer = between("control.ANSWER", edge_ip, caller_ip)
    hangup = between("iax.HANGUP", edge_ip, caller_ip)
    first_accept = min((f["t"] for f in accept), default=None)
    hangup_after = [f for f in hangup if first_accept is not None and f["t"] >= first_accept]
    return {"new": len(new), "accept": len(accept), "answer": len(answer), "hangup": len(hangup),
            "new_ports": sorted({_port(f["src"]) for f in new}),
            "accept_to_hangup_s": round(min(f["t"] for f in hangup_after) - first_accept, 4) if hangup_after else None,
            "refused": bool(new and accept and hangup_after and not answer),
            "answered": bool(new and accept and answer)}
