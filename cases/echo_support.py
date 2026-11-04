# SPDX-License-Identifier: MIT
"""Rig helpers of cases/test_echo.py: a talker and a listener on a module with their packets
timed by a background drain, the live edit of the rendered dv-router.toml, a minimal dynamic
USRP client and the notes file. The pure part (events, comparison, TOML editing) is
dvtest/echo.py, unit-tested offline."""
import os
import socket
import threading
import time
from pathlib import Path

from dvtest import clients, echo
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import usrp

MODULE = "E"
TALK_GAP = 0.020


# ---------------------------------------------------------------- notes

def note(target, case, tag, text):
    """Print "[tag] case: text" (RECORD, FINDING or OK) and append it to out/<run>/echo.txt."""
    line = "[%s] %s: %s" % (tag, case, text)
    print(line)
    try:
        with open(Path(target.out) / "echo.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- timed inboxes

class Drainer(threading.Thread):
    """Drains UdpClient sockets every few ms so that inbox_times tell when a packet arrived
    (UdpClient.talk drains only its own socket, and only while it sends). One thread per
    group of clients; never drain a socket that the main thread pumps at the same time."""

    def __init__(self, *udp):
        super().__init__(daemon=True)
        self.udp = udp
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            for client in self.udp:
                try:
                    client.drain()
                except Exception:          # a closed socket at teardown
                    return
            time.sleep(0.003)

    def stop(self):
        self.halt.set()
        self.join(2.0)


class Scene:
    """A talker (identity 0 of clients.make) and a listener (identity 1) of one protocol, both linked
    to `module` of the system under test. Every packet either one receives is timed (a Drainer thread).
    over() sends one talk; the times of the sender's packets are in .ends / .sids."""

    def __init__(self, ip, kind, module=MODULE):
        self.ip, self.kind, self.module = ip, kind, module
        self.talker = clients.make(kind, module, 0)
        self.listener = clients.make(kind, module, 1)
        self.made = [self.talker, self.listener]
        self.listen_drain = self.talk_drain = None
        for c in self.made:
            if not c.link(ip):
                self.close()
                raise AssertionError("%s client did not link to %s (module %s)" % (kind, ip, module))
        self.ends, self.sids, self.sent, self.raw = [], [], [], []
        for c in self.made:
            c.client.drain()
            c.client.inbox.clear()
            c.client.inbox_times.clear()
        self.listen_drain = Drainer(self.listener.client)
        self.listen_drain.start()
        self.talk_drain = None
        self.t0 = time.monotonic()

    def over(self, n, seed):
        """Send one talk of n frames; returns the frames. The monotonic time of the last packet sent is
        appended to .ends, the stream id to .sids, the frames put on the wire to .sent."""
        if self.talk_drain is not None:
            self.talk_drain.stop()
            self.talk_drain = None
        frames = echo.make_frames(self.kind, n, "%s-%s" % (seed, self.kind))
        sid = clients.talk(self.talker, self.kind, frames, tail=0.0)
        # time of the last packet sent (the YSF client does not keep its send times: now is that time)
        self.ends.append(time.monotonic() if self.kind == "ysf" else self.talker.last_stamps[-1])
        self.sids.append(sid)
        self.sent.append(echo.expected_frames(self.kind, frames))
        self.raw.append(frames)
        self.talk_drain = Drainer(self.talker.client)
        self.talk_drain.start()
        return frames

    def until(self, t):
        """Sleep until the monotonic time t."""
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def packets(self, who):
        c = self.talker if who == "talker" else self.listener
        data, times = list(c.client.inbox), list(c.client.inbox_times)
        return data, times

    def overs(self, who, gap=0.4, voiced=True):
        """The overs `who` heard. voiced: leave out streams without a single voice frame (the empty header +
        terminator stream of a DMR / YSF join is relayed to the other clients of the module and is no over)."""
        data, times = self.packets(who)
        overs = echo.split_overs(echo.events(self.kind, data, times), gap)
        return [o for o in overs if o.n_voice > 0] if voiced else overs

    def ident(self):
        """The talker as the packets name it: the D-STAR callsign (MY), the DMR id, the M17 / YSF callsign."""
        t = self.talker
        if self.kind == "dstar":
            return t.callsign.decode("ascii").strip()
        if self.kind == "dmr":
            return t.id
        return t.callsign

    def stop_drains(self):
        for d in (self.listen_drain, self.talk_drain):
            if d is not None:
                d.stop()
        self.listen_drain = self.talk_drain = None

    def close(self, settle=1.0):
        self.stop_drains()
        try:
            clients.close(*self.made)
        except Exception:
            pass
        time.sleep(settle)                      # the module is quiet before the next scene starts


def wait_for_over(scene, after_t, who="listener", timeout=6.0):
    """Poll until `who` has an over that starts after `after_t`; returns it or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        later = echo.after(scene.overs(who), after_t)
        if later:
            return later[0]
        time.sleep(0.05)
    return None


# ---------------------------------------------------------------- the rendered config of the run

class EchoCfg:
    """The rendered dv-router.toml of this run (out/<run>/xlxd-config/xlxd-sut/dv-router.toml,
    bind-mounted read-only into the SUT by scripts/run.sh). The file is the run's own and is edited
    in place (same inode, so the single-file bind mount follows; the launcher's watcher applies a
    change after it looked the same for two polls). Always restore() in a finally."""

    def __init__(self, path):
        self.path = Path(path)
        self.original = self.path.read_text(encoding="utf-8")
        self.settings = echo.echo_settings(self.original)

    @classmethod
    def locate(cls, target):
        path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
        if not path.is_file():
            return None
        return cls(path)

    def writable(self):
        return os.access(self.path, os.W_OK)

    def current(self):
        return self.path.read_text(encoding="utf-8")

    def write(self, text):
        """Replace the content in place: new bytes first, then cut to the new length, so the file is
        never empty and the inode stays the one the container's bind mount holds."""
        data = text.encode("utf-8")
        with open(self.path, "r+b") as fh:
            fh.seek(0)
            fh.write(data)
            fh.truncate(len(data))
            fh.flush()
            os.fsync(fh.fileno())

    def apply(self, **edits):
        """Edit [echo] (modules=, max_seconds=, delay_ms= as dvtest.echo.edit_echo) in the live file."""
        self.write(echo.edit_echo(self.current(), **edits))

    def restore(self):
        if self.current() != self.original:
            self.write(self.original)


def wait_reload(extra=0.0):
    time.sleep(echo.reload_wait() + extra)


# ---------------------------------------------------------------- a minimal dynamic USRP client

class UsrpDyn:
    """A DVSwitch-style client of the SUT's dynamic USRP hub (docs/protocols/usrp.md section 5):
    REG, SET_INFO, DTMF module select, voice and unkey; the voice it receives is timed."""

    def __init__(self, addr, callsign):
        self.dest, self.callsign = addr, callsign
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.setblocking(False)
        self.seq = 0
        self.inbox = []                         # (t, data)

    def send(self, build, *args):
        self.sock.sendto(build(self.seq, *args), self.dest)
        self.seq += 1

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while True:
            try:
                data, _ = self.sock.recvfrom(2048)
                self.inbox.append((time.monotonic(), data))
                continue
            except BlockingIOError:
                pass
            if time.monotonic() >= end:
                return
            time.sleep(0.002)

    def texts(self):
        return [p for _, d in self.inbox if (p := usrp.text_payload(d)) is not None]

    def register(self, timeout=3.0):
        self.send(usrp.text, usrp.REG)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.05)
            if any(t.startswith(usrp.REG_OK) for t in self.texts()):
                return True
        return False

    def join(self, module):
        self.send(usrp.text_set_info, self.callsign)
        self.send(usrp.dtmf, module)
        self.pump(0.3)

    def talk(self, n_frames, drain_with=()):
        pcm = signals.frames(signals.tone(700, n_frames * 20))
        start = time.monotonic()
        for i, frame in enumerate(pcm):
            pace(start, i, TALK_GAP)
            self.send(usrp.voice, frame)
            for other in drain_with:
                other.pump(0)
        self.send(usrp.unkey)
        return time.monotonic()

    def voice_times(self):
        return [t for t, d in self.inbox
                if len(d) == usrp.VOICE_SIZE and usrp.type_le(d) == 0 and usrp.parse(d).keyup == 1]

    def close(self):
        try:
            self.send(usrp.text, usrp.REG_UNREG)
        except OSError:
            pass
        self.sock.close()


def count_streams(times, gap=0.4):
    """Number of runs of timestamps with no hole longer than `gap` seconds."""
    runs, last = 0, None
    for t in sorted(times):
        if last is None or t - last > gap:
            runs += 1
        last = t
    return runs
