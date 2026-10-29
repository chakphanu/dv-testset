# SPDX-License-Identifier: MIT
"""Shared UDP client with inbox and paced sending."""
import socket
import time


def pace(start, index, gap=0.020):
    """Sleep until start + index * gap (monotonic schedule, no drift)."""
    delay = start + index * gap - time.monotonic()
    if delay > 0:
        time.sleep(delay)


class UdpClient:
    def __init__(self, bind_ip, responder=None, port=0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, port))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.sock.settimeout(0.05)
        self.inbox = []
        self.inbox_times = []
        # responder(data, addr) -> reply bytes or None; used to answer
        # protocol keepalives (e.g. the DExtra 9-byte XRF keepalive) so
        # the reflector does not drop an otherwise idle listener.
        self.responder = responder

    @property
    def local(self):
        return self.sock.getsockname()

    def send(self, dest, packet):
        self.sock.sendto(packet, dest)

    def drain(self):
        self.sock.setblocking(False)
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                break
            if self.responder:
                reply = self.responder(data, addr)
                if reply is not None:
                    try:
                        self.sock.sendto(reply, addr)
                    except OSError:
                        pass
            self.inbox.append(data)
            self.inbox_times.append(time.monotonic())
        self.sock.settimeout(0.05)

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.drain()
            time.sleep(0.01)

    def paced(self, dest, packets, gap, *extra_clients):
        """Send at gap spacing, draining self and extra clients each step."""
        start = time.monotonic()
        stamps = []
        for index, packet in enumerate(packets):
            target = start + index * gap
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.sock.sendto(packet, dest)
            stamps.append(time.monotonic())
            self.drain()
            for client in extra_clients:
                client.drain()
        return stamps

    def close(self):
        self.sock.close()
