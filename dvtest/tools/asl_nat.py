# SPDX-License-Identifier: MIT
"""Control server of the asl-nat sidecar (rig service asl-nat-a, profile asl-nat, case T5n).

Shares the network namespace of the lab node asl-a, runs as root with only CAP_NET_ADMIN (ip, iptables,
conntrack). It finds the node's interface by address, adds the alias address, installs the chain and answers
the JSON-line protocol of dvtest/asl_nat.py on --listen (the node's own address, TCP). Only the addresses
given with --allow (the tester) may ask. Nothing secret passes through it and it never sends a packet of its own.

  python -m dvtest.tools.asl_nat --listen 172.30.0.42:9101 --allow 172.30.0.30 \\
      --local-ip 172.30.0.42 --alias-ip 172.30.0.46 --peer-ip 172.30.0.11 --port 4569 --map-port 44569
"""
import argparse
import socketserver
import subprocess
import sys

from dvtest import asl_nat, nat


def run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        snat, allow = self.server.snat, self.server.allow
        if allow and self.client_address[0] not in allow:
            self.wfile.write(nat.encode({"ok": False, "error": "client address not allowed"}))
            print("asl-nat: refused %s" % self.client_address[0], flush=True)
            return
        line = self.rfile.readline(nat.MAX_LINE + 1)
        try:
            req = asl_nat.parse_request(line.strip())
        except nat.NatError as exc:
            self.wfile.write(nat.encode({"ok": False, "error": str(exc)}))
            return
        ans = snat.handle(req)
        print("asl-nat: %s %s -> %s" % (req["op"], {k: v for k, v in req.items() if k != "op"}, "ok" if ans["ok"] else ans["error"]),
              flush=True)
        self.wfile.write(nat.encode(ans))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dvtest.tools.asl_nat")
    ap.add_argument("--listen", required=True, help="address:port of the control server (the node's own address)")
    ap.add_argument("--allow", action="append", default=[], help="client address allowed to ask (repeatable)")
    ap.add_argument("--local-ip", required=True, help="the lab node's address")
    ap.add_argument("--alias-ip", required=True, help="the address the edge sees in mode address")
    ap.add_argument("--peer-ip", required=True, help="the edge")
    ap.add_argument("--port", type=int, default=4569, help="IAX2 port of node and edge")
    ap.add_argument("--map-port", type=int, default=44569, help="the source port the edge sees")
    ap.add_argument("--out-prefix", type=int, default=24)
    args = ap.parse_args(argv)
    rc, out = run(["ip", "-o", "-4", "addr", "show"])
    names = nat.parse_ip_addr(out)
    try:
        topo = asl_nat.make_local_nat(args.local_ip, args.alias_ip, args.peer_ip, args.port, args.map_port,
                                      names[args.local_ip], args.out_prefix)
    except (KeyError, nat.NatError) as exc:
        print("asl-nat: cannot set up: %s (addresses seen: %s)" % (exc, sorted(names)), file=sys.stderr)
        return 2
    snat = asl_nat.Snat(topo, run)
    try:
        snat.start()
    except nat.NatError as exc:
        print("asl-nat: %s" % exc, file=sys.stderr)
        return 2
    host, _, port = args.listen.rpartition(":")
    srv = Server((host, int(port)), Handler)
    srv.snat, srv.allow = snat, set(args.allow)
    print("asl-nat: up, node %s (%s) alias %s edge %s port %d -> %d, mode %s, control %s" % (
        topo.local_ip, topo.out_if, topo.alias_ip, topo.peer_ip, topo.port, topo.map_port, snat.mode, args.listen), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
