# SPDX-License-Identifier: MIT
"""Control server of the NAT router container (rig service nat-router, profile nat).

Runs as root with CAP_NET_ADMIN (iptables, conntrack, ip) and CAP_NET_RAW (the AF_PACKET sniffer of the `firsts`
evidence; without it everything but that evidence works). It finds its interfaces by address,
installs the control forwards, starts as `port_restricted` and then answers the JSON-line protocol of
dvtest/nat.py on --listen (the OUTSIDE address only; the browser on the inside network cannot reach it).
Only the addresses given with --allow (the tester) may ask. Nothing secret passes through it.

  python -m dvtest.tools.nat_router --listen 172.30.0.92:9100 --allow 172.30.0.30 \\
      --inside-net 172.31.0.0/24 --out-ip 172.30.0.92 --in-ip 172.31.0.2 --alias-ip 172.30.0.93 \\
      --browser-ip 172.31.0.91 --sut-ip 172.30.0.11 --forward 3000 --forward 3001
"""
import argparse
import socket
import socketserver
import subprocess
import sys
import threading
import time

from dvtest import nat

# ETH_P_ALL, not ETH_P_IP: the kernel hands the frames a host SENDS (here: the browser's datagrams leaving the router's outside
# interface after SNAT) only to packet sockets that listen for every protocol (dev_queue_xmit_nit walks ptype_all); a socket bound
# to ETH_P_IP gets the incoming frames only. The first run of the evidence saw the SUT's datagrams and none of the browser's.
ETH_P_ALL = 0x0003
SNAP_BYTES = 128            # enough for the Ethernet, IPv4 (with options) and UDP headers and the 20 byte STUN header


class PacketSniffer:
    """A small AF_PACKET reader for the `firsts` evidence (ICE1c): every frame of one interface, both directions (what
    the router sends out after NAT and what arrives before it is undone; the parser keeps only IPv4 UDP), cut to SNAP_BYTES, handed to on_frame(bytes) on a
    daemon thread for `duration_s` seconds or until stop(). Needs CAP_NET_RAW (rig/compose.yml gives it to nat-router only).
    `open_socket(ifname)` is injectable for the unit tests."""

    def __init__(self, ifname, on_frame, duration_s, open_socket=None):
        self.ifname, self.on_frame, self.duration_s = ifname, on_frame, duration_s
        self.open_socket = open_socket or self._open_packet_socket
        self.sock = None
        self.thread = None
        self.stopping = threading.Event()

    @staticmethod
    def _open_packet_socket(ifname):
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.bind((ifname, ETH_P_ALL))
        except OSError:
            sock.close()
            raise
        return sock

    def start(self):
        """Opens the socket now (a missing capability raises here, in the caller) and starts reading."""
        self.sock = self.open_socket(self.ifname)
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        end = time.monotonic() + self.duration_s
        while not self.stopping.is_set() and time.monotonic() < end:
            try:
                frame = self.sock.recv(SNAP_BYTES)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.on_frame(frame)
            except Exception:                           # noqa: BLE001 - a bad frame must not end the evidence
                pass

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(1.0)
        if self.sock is not None:
            self.sock.close()
            self.sock = None


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
    router = nat.Router(topo, run, sniffer=PacketSniffer)
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
