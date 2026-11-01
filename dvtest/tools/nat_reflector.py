# SPDX-License-Identifier: MIT
"""UDP reflector of the NAT self-test (rig services nat-ref-a / nat-ref-b, profile nat).

Echo side: a datagram "DVT-NAT-PROBE <token>" on any --ports port is answered with
"DVT-NAT-REPLY {"token":..., "src":[ip, port]}": the source address a NAT showed this server, i.e.
the mapping. Poke side: a datagram "DVT-NAT-POKE {"to":[ip, port], "from_port": n, "data": text}"
makes the server send `data` to `to` from its poke socket (port --poke-port, a port the NAT's
inside host never contacted) and answers "ok": that is the "same address, other port" unsolicited
packet of the filtering test. Rig-internal test infrastructure: the poke can send a small UDP
datagram to any address of the (offline) rig networks, nothing else.

  python -m dvtest.tools.nat_reflector --listen 172.30.0.95 --ports 3479,3480 --poke-port 3481
"""
import argparse
import socket
import sys
import threading

from dvtest import nat


def serve(sock, poke_sock, name):
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            return
        rep = nat.reflect_reply(data, addr)
        if rep is not None:
            sock.sendto(rep, addr)
            continue
        poke = nat.parse_poke(data)
        if poke is not None and poke["from_port"] == poke_sock.getsockname()[1]:
            poke_sock.sendto(poke["data"].encode("ascii", "replace"), poke["to"])
            sock.sendto(b"ok", addr)
            print("%s: poke %s:%d -> %s:%d" % (name, poke_sock.getsockname()[0], poke["from_port"], poke["to"][0], poke["to"][1]), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m dvtest.tools.nat_reflector")
    ap.add_argument("--listen", required=True, help="address to bind (one rig address)")
    ap.add_argument("--ports", default="3479,3480")
    ap.add_argument("--poke-port", type=int, default=3481)
    args = ap.parse_args(argv)
    poke_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    poke_sock.bind((args.listen, args.poke_port))
    threads = []
    for port in [int(p) for p in args.ports.split(",")]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((args.listen, port))
        t = threading.Thread(target=serve, args=(s, poke_sock, "%s:%d" % (args.listen, port)), daemon=True)
        t.start()
        threads.append(t)
    print("nat-reflector: %s ports %s poke %d" % (args.listen, args.ports, args.poke_port), flush=True)
    for t in threads:
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
