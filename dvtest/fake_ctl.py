# SPDX-License-Identifier: MIT
"""Client of the JSON control socket of the fake AMBEd (dvtest/fake_ambed.py,
docs/ambed-fake.md): one UDP datagram per request and per reply."""
import json
import random
import socket
import time

CONTROL_PORT = 10190
# Source ports of the control client: a fixed range far from every protocol port
# of the rig. An ephemeral source port can land on a protocol port (34003 is a
# USRP port of the rig) and the wire checker then reads the JSON as that protocol.
SOURCE_PORTS = range(10300, 10400)


def bind_source(sock):
    """Bind sock to a free port of SOURCE_PORTS; on failure of all, leave it to the OS."""
    start = random.randrange(len(SOURCE_PORTS))
    for i in range(len(SOURCE_PORTS)):
        port = SOURCE_PORTS[(start + i) % len(SOURCE_PORTS)]
        try:
            sock.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
    return None


class FakeCtlError(RuntimeError):
    """The fake did not answer, or answered ok = false."""


class FakeCtl:
    def __init__(self, host, port=CONTROL_PORT, timeout=2.0, tries=3):
        self.dest = (host, port)
        self.timeout = timeout
        self.tries = tries

    def call(self, **request):
        """One request; the reply object. Retries a lost datagram (every op
        is idempotent except reset, which is harmless to repeat)."""
        data = json.dumps(request, separators=(",", ":")).encode()
        last = None
        for _ in range(self.tries):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bind_source(sock)
            try:
                sock.settimeout(self.timeout)
                sock.sendto(data, self.dest)
                reply = json.loads(sock.recvfrom(65535)[0])
            except (socket.timeout, OSError, ValueError) as exc:
                last = exc
                continue
            finally:
                sock.close()
            if not reply.get("ok"):
                raise FakeCtlError("%s: %s" % (request.get("op"), reply.get("error", reply)))
            return reply
        raise FakeCtlError("no answer from %s:%d to %s (%r)" % (self.dest + (request.get("op"), last)))

    def stats(self):
        return self.call(op="stats")

    def reset(self):
        return self.call(op="reset")

    def set(self, **modes):
        return self.call(op="set", **modes)

    def counters(self):
        return self.stats()["counters"]

    def last_seq(self):
        return self.stats()["last_seq"]

    def log(self, since_seq=0, kinds=None):
        """Log entries (dicts with hex, kind, dir, port, note, t, seq) newer
        than since_seq, following the `more` flag until the log is read."""
        out = []
        while True:
            request = {"op": "log", "since_seq": since_seq}
            if kinds is not None:
                request["kinds"] = list(kinds)
            reply = self.call(**request)
            out.extend(reply["entries"])
            if not reply["more"] or not reply["entries"]:
                return out
            since_seq = reply["last_seq"]

    def wait_for(self, cond, timeout=5.0, interval=0.1):
        """Poll stats() until cond(stats) is true; the last stats either way."""
        end = time.monotonic() + timeout
        while True:
            stats = self.stats()
            if cond(stats) or time.monotonic() >= end:
                return stats
            time.sleep(interval)
