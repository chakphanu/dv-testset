# SPDX-License-Identifier: MIT
"""Control server of the NAT router container (rig service nat-router, profile nat).

Runs as root with only CAP_NET_ADMIN (iptables, conntrack, ip). It finds its interfaces by address,
installs the control forwards, starts as `port_restricted` and then answers the JSON-line protocol of
dvtest/nat.py on --listen (the OUTSIDE address only; the browser on the inside network cannot reach it).
Only the addresses given with --allow (the tester) may ask. Nothing secret passes through it.

  python -m dvtest.tools.nat_router --listen 172.30.0.92:9100 --allow 172.30.0.30 \\
      --inside-net 172.31.0.0/24 --out-ip 172.30.0.92 --in-ip 172.31.0.2 --alias-ip 172.30.0.93 \\
      --browser-ip 172.31.0.91 --sut-ip 172.30.0.11 --forward 3000 --forward 3001
"""
import argparse
import socketserver
import subprocess
import sys

from dvtest import nat


def run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        router, allow = self.server.router, self.server.allow
        if allow and self.client_address[0] not in allow:
            self.wfile.write(nat.encode({"ok": False, "error": "client address not allowed"}))
            print("nat-router: refused %s" % self.client_address[0], flush=True)
            return
        line = self.rfile.readline(nat.MAX_LINE + 1)
        try:
            req = nat.parse_request(line.strip())
        except nat.NatError as exc:
            self.wfile.write(nat.encode({"ok": False, "error": str(exc)}))
            return
        ans = router.handle(req)
        print("nat-router: %s %s -> %s" % (req["op"], {k: v for k, v in req.items() if k != "op"}, "ok" if ans["ok"] else ans["error"]),
              flush=True)
        self.wfile.write(nat.encode(ans))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dvtest.tools.nat_router")
    ap.add_argument("--listen", required=True, help="outside address:port of the control server")
    ap.add_argument("--allow", action="append", default=[], help="client address allowed to ask (repeatable)")
    ap.add_argument("--inside-net", required=True)
    ap.add_argument("--out-ip", required=True)
    ap.add_argument("--in-ip", required=True)
    ap.add_argument("--alias-ip", required=True, help="outside alias address of the full cone (1:1) NAT")
    ap.add_argument("--browser-ip", required=True)
    ap.add_argument("--sut-ip", required=True)
    ap.add_argument("--media-port", type=int, default=50000)
    ap.add_argument("--forward", type=int, action="append", default=[], help="TCP port forwarded outside -> browser (repeatable)")
    ap.add_argument("--out-prefix", type=int, default=24)
    args = ap.parse_args(argv)
    rc, out = run(["ip", "-o", "-4", "addr", "show"])
    names = nat.parse_ip_addr(out)
    try:
        topo = nat.make_topology(args.inside_net, args.out_ip, args.in_ip, args.alias_ip, args.browser_ip, args.sut_ip,
                                 args.media_port, args.forward or (3000, 3001), names[args.out_ip], names[args.in_ip], args.out_prefix)
    except (KeyError, nat.NatError) as exc:
        print("nat-router: cannot set up: %s (addresses seen: %s)" % (exc, sorted(names)), file=sys.stderr)
        return 2
    router = nat.Router(topo, run)
    try:
        router.start()
    except nat.NatError as exc:
        print("nat-router: %s" % exc, file=sys.stderr)
        return 2
    host, _, port = args.listen.rpartition(":")
    srv = Server((host, int(port)), Handler)
    srv.router, srv.allow = router, set(args.allow)
    print("nat-router: up, outside %s (%s) inside %s (%s) alias %s, mode %s, control %s" % (
        topo.out_ip, topo.out_if, topo.in_ip, topo.in_if, topo.alias_ip, router.mode, args.listen), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
