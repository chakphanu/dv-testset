# SPDX-License-Identifier: MIT
"""A KISS TCP CLIENT for the DP15 RF cases of tier B (cases/test_dp15_rf_dw.py, docs/dp15-cases.md).

Tier A plays the TNC (dvtest/kiss_tnc.py, the AX.25 edge connects to the tester). In tier B the TNCs are real Dire Wolf
processes (rig services dw-a and dw-b) and the tester is one more KISS client of each, next to the AX.25 edge:

* `send(frame)` writes one KISS data frame (AX.25 frame without FCS) to the TNC: Dire Wolf modulates and transmits it;
* `frames(since)` are the data frames the TNC handed to this client: what it demodulated (a frame that the other Dire Wolf
  of the loop transmitted), with the arrival time (time.monotonic).

Dire Wolf serves three clients per KISS port at a time; the edge is one, the tester is one. Pure stdlib; the loopback unit test
is cases/unit/test_kiss_client.py. Connect with retries: the TNC may still be starting.
"""
import socket
import threading
import time

from dvtest import ax25


class KissClient:
    def __init__(self, host, port, name="tnc"):
        self.host, self.port, self.name = host, int(port), name
        self.received = []                  # [(time.monotonic, channel, command, data)] in arrival order
        self.lock = threading.Condition()
        self._sock = None
        self._thread = None
        self._decoder = ax25.KissDecoder()
        self._stop = threading.Event()

    def connect(self, wait=60.0):
        """True once connected (retries every 0.5 s for `wait` seconds)."""
        end = time.monotonic() + wait
        while True:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=3)
                break
            except OSError:
                if time.monotonic() >= end:
                    return False
                time.sleep(0.5)
        sock.settimeout(0.2)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name="kiss-client-%s" % self.name, daemon=True)
        self._thread.start()
        return True

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            now = time.monotonic()
            got = self._decoder.feed(chunk)
            if got:
                with self.lock:
                    self.received.extend((now, ch, cmd, data) for ch, cmd, data, _first in got)
                    self.lock.notify_all()

    def close(self):
        """Close the connection: shutdown first, so Dire Wolf frees the client slot at once."""
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(1.0)

    def __enter__(self):
        if not self.connect():
            raise OSError("no KISS TCP server at %s:%s" % (self.host, self.port))
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def send(self, frame, channel=0):
        """Write one KISS data frame; False when the write failed."""
        try:
            self._sock.sendall(ax25.kiss_encode(channel, 0, frame))
        except (OSError, AttributeError):
            return False
        return True

    def frames(self, since=0.0):
        """[(arrival time, channel, frame bytes)] of the data frames (command 0) received at or after `since`."""
        with self.lock:
            return [(t, ch, data) for t, ch, cmd, data in self.received if cmd == 0 and t >= since]

    def wait_frames(self, count=1, timeout=10.0, since=0.0):
        """The data frames since `since` once there are `count`, else what is there after `timeout` seconds."""
        end = time.monotonic() + timeout
        with self.lock:
            while len([1 for t, _ch, cmd, _d in self.received if cmd == 0 and t >= since]) < count:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.lock.wait(min(left, 0.5))
        return self.frames(since)
