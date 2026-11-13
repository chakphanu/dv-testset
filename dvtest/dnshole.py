# SPDX-License-Identifier: MIT
"""A DNS server that does not answer: the resolver the NETTHR1b cancel case points the SUT at (docs/netthr1-cases.md, D1).

The SUT's /etc/resolv.conf names the tester (DVT_SUT_RESOLV, scripts/run.sh); this module binds UDP 53 there. Every query is
logged with the monotonic time it arrived. A name under `.ok.` gets an answer (A = the address given, AAAA = empty), every
other name gets NO answer at all, so a lookup of it can only end by the resolver's own deadline (DV-Router: 5 s,
deployment.md "Process schedulers"). Stdlib only; the parts that need no socket (parse, build, schedule) are unit-tested.

  parse_query(data)             (txid, qname, qtype) or None
  build_answer(data, ip)        reply bytes for an A query (an empty NOERROR for any other type), None for a bad packet
  class Hole                    the UDP server thread; .log = [(t, qname, qtype)]
  first_times(log, names)       {name: time of its first query}
  schedule(log, order, t0)      seconds after t0 of the first query of each name in `order` (None = never asked)
"""
import socket
import struct
import threading
import time

TYPE_A, TYPE_AAAA = 1, 28


def parse_query(data):
    """(txid, qname lower case without the final dot, qtype) of a standard query with one question, else None."""
    if len(data) < 17:
        return None
    txid, flags, qd = struct.unpack("!HHH", data[:6])
    if flags & 0x8000 or qd < 1:
        return None
    labels, i = [], 12
    while True:
        if i >= len(data):
            return None
        n = data[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 or i + 1 + n > len(data):
            return None
        labels.append(data[i + 1:i + 1 + n].decode("ascii", "replace").lower())
        i += 1 + n
    if i + 4 > len(data):
        return None
    qtype = struct.unpack("!H", data[i:i + 2])[0]
    return txid, ".".join(labels), qtype


def build_answer(data, ip):
    """The reply to a query: the question echoed, one A record for type A, no answer record for any other type."""
    parsed = parse_query(data)
    if parsed is None:
        return None
    txid, _name, qtype = parsed
    end = 12
    while data[end] != 0:
        end += 1 + data[end]
    question = data[12:end + 1 + 4]
    answers = b""
    if qtype == TYPE_A:
        answers = b"\xc0\x0c" + struct.pack("!HHIH", TYPE_A, 1, 30, 4) + socket.inet_aton(ip)
    flags = 0x8180                                   # response, recursion desired + available, NOERROR
    header = struct.pack("!HHHHHH", txid, flags, 1, 1 if answers else 0, 0, 0)
    return header + question + answers


def answers_name(name):
    return name == "ok" or name.endswith(".ok") or ".ok." in name or name.startswith("ok.")


class Hole(threading.Thread):
    """UDP server on (host, port). Names for which answers_name() is true are answered with `ip`; the rest is only logged."""

    def __init__(self, host="0.0.0.0", port=53, ip="127.0.0.1"):
        super().__init__(daemon=True)
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.1)
        self.halt = threading.Event()
        self.log = []                                # (monotonic, qname, qtype)
        self.sources = set()

    def run(self):
        while not self.halt.is_set():
            try:
                data, addr = self.sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                return
            stamp = time.monotonic()
            parsed = parse_query(data)
            if parsed is None:
                continue
            self.sources.add(addr[0])
            self.log.append((stamp, parsed[1], parsed[2]))
            if answers_name(parsed[1]):
                reply = build_answer(data, self.ip)
                if reply:
                    try:
                        self.sock.sendto(reply, addr)
                    except OSError:
                        pass

    def stop(self):
        self.halt.set()
        self.join(1.0)
        try:
            self.sock.close()
        except OSError:
            pass


def first_times(log, names=None):
    """{name: monotonic time of its first query} (A or AAAA alike); only `names` when given."""
    out = {}
    for stamp, name, _qtype in log:
        if names is not None and name not in names:
            continue
        if name not in out or stamp < out[name]:
            out[name] = stamp
    return out


def schedule(log, order, t0=None):
    """Seconds after t0 (default: the earliest query of `order`) of the first query of each name of `order`; None for a name
    that was never asked. Returns (t0, [seconds or None])."""
    firsts = first_times(log, set(order))
    if t0 is None:
        t0 = min(firsts.values()) if firsts else None
    return t0, [None if (n not in firsts or t0 is None) else firsts[n] - t0 for n in order]


def last_times(log, names=None):
    out = {}
    for stamp, name, _qtype in log:
        if names is not None and name not in names:
            continue
        out[name] = max(out.get(name, stamp), stamp)
    return out
