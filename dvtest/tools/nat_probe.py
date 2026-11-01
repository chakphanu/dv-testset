# SPDX-License-Identifier: MIT
"""Probe server of the NAT self-test: runs INSIDE the browser's network namespace (rig service nat-edge
shares the namespace of `browser`), so its UDP traffic takes the same NAT path as the browser's.

First it sets the default route of that namespace via the NAT router (--gateway; the inside network is
internal, so Docker gives the browser none) and drops root. Then it answers JSON lines on --listen
(reached from the tester through the router's TCP forward):

  {"op": "probe_start", "local_port": 41000, "targets": [[ip, port], ...], "timeout": 2}
      opens a UDP socket on local_port, sends "DVT-NAT-PROBE <token>" to every target and waits for the
      reflectors' replies; answers {"ok": true, "mapped": [[ip, port] | null, ...], "local": [ip, port]}
      (mapped = the source address the reflector saw). The socket stays open.
  {"op": "probe_stop"}
      answers {"ok": true, "received": [{"from": [ip, port], "data": "..."}, ...]}: every datagram that
      reached the open socket since probe_start apart from the reflector replies (the unsolicited ones),
      and closes it.
"""
import argparse
import json
import os
import socket
import socketserver
import subprocess
import sys
import time

from dvtest import nat

STATE = {"sock": None, "received": [], "local": None}


def _drain(sock, seconds):
    """Read everything for `seconds`; returns [(data, addr)]."""
    out = []
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return out
        sock.settimeout(left)
        try:
            out.append(sock.recvfrom(2048))
        except (socket.timeout, BlockingIOError):
            return out
        except OSError:
            return out


def probe_start(req):
    if STATE["sock"] is not None:
        STATE["sock"].close()
        STATE["sock"] = None
    port = int(req.get("local_port", 0))
    targets = [(need(t[0]), int(t[1])) for t in req.get("targets", [])]
    timeout = float(req.get("timeout", 2.0))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    STATE["sock"], STATE["received"], STATE["local"] = s, [], list(s.getsockname())
    mapped = [None] * len(targets)
    for i, (ip, p) in enumerate(targets):
        token = "t%d-%d" % (os.getpid(), i)
        s.sendto(nat.REFLECT_REQ + token.encode("ascii"), (ip, p))
        end = time.monotonic() + timeout
        while mapped[i] is None and time.monotonic() < end:
            for data, addr in _drain(s, 0.05 if mapped[i] is None else 0.0):
                got = nat.parse_reflect_reply(data)
                if got is None:
                    STATE["received"].append({"from": [addr[0], addr[1]], "data": data[:40].decode("ascii", "replace")})
                elif got[0] == token:
                    mapped[i] = list(got[1])
                else:
                    pass
    return {"ok": True, "mapped": mapped, "local": STATE["local"]}


def need(ip):
    return nat.need_ip(ip, "target")


def probe_stop(_req):
    s = STATE["sock"]
    if s is None:
        return {"ok": True, "received": []}
    for data, addr in _drain(s, 0.05):
        if nat.parse_reflect_reply(data) is None:
            STATE["received"].append({"from": [addr[0], addr[1]], "data": data[:40].decode("ascii", "replace")})
    s.close()
    STATE["sock"] = None
    return {"ok": True, "received": STATE["received"]}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(nat.MAX_LINE + 1)
        try:
            req = json.loads(line.decode("ascii"))
            op = req.get("op")
            if op == "probe_start":
                ans = probe_start(req)
            elif op == "probe_stop":
                ans = probe_stop(req)
            else:
                ans = {"ok": False, "error": "unknown op"}
        except (ValueError, OSError, nat.NatError, TypeError, IndexError, KeyError) as exc:
            ans = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        self.wfile.write(nat.encode(ans))


class Server(socketserver.TCPServer):
    allow_reuse_address = True


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dvtest.tools.nat_probe")
    ap.add_argument("--listen", required=True, help="address:port of the probe server (in the browser's namespace)")
    ap.add_argument("--gateway", help="default route via this address (needs CAP_NET_ADMIN; done before dropping root)")
    ap.add_argument("--drop-to", type=int, default=65534, help="uid/gid to run as after the route is set")
    args = ap.parse_args(argv)
    if args.gateway:
        nat.need_ip(args.gateway, "gateway")
        rc = subprocess.run(["ip", "route", "replace", "default", "via", args.gateway]).returncode
        if rc != 0:
            print("nat-probe: cannot set the default route via %s" % args.gateway, file=sys.stderr)
            return 2
    if os.getuid() == 0:
        os.setgid(args.drop_to)
        os.setuid(args.drop_to)
    host, _, port = args.listen.rpartition(":")
    srv = Server((host, int(port)), Handler)
    print("nat-probe: up on %s as uid %d, default route via %s" % (args.listen, os.getuid(), args.gateway), flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
