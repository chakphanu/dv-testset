# SPDX-License-Identifier: MIT
"""Tester side of the run.sh control channel (header of scripts/run.sh).

A request is a file <id>.req (written as .tmp, renamed) in $DVT_OUT/control
with one line; the host watcher answers with <id>.ack ("ok", "error <rc>" or
"error refused") and, for some actions, a text file <id>.out.

  "<action> <service>"                       stop | start | restart | kill
  "kill-proc <service> <process> <signal>"   signal one process by name INSIDE
                                             a rig container (allow-list below)
  "mark begin|end <tag>"                     only logged: brackets traffic a case
                                             makes abnormal on purpose (dvcheck)
  "metrics xlxd-sut"                         the Prometheus text of the sut hub
                                             (127.0.0.1:19108 inside its network
                                             namespace) in <id>.out

KILL_PROC_ALLOWED is the tester's copy of the allow-list in
scripts/lib/killproc.sh (the host enforces it; cases/unit/test_control.py
checks that both agree). The reply of kill-proc in <id>.out is one line per
signalled process "pid=<pid>" (pid inside the container's pid namespace) and
"t_kill=<epoch seconds>", taken inside the container right after the
signals were sent (same host clock as time.time() of the tester).
"""
import itertools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

# (service, process name = exact /proc/<pid>/comm) -> signals the host will send
KILL_PROC_ALLOWED = {
    ("xlxd-sut", "dv-edge-xlx"): ("KILL", "USR1"),
    ("xlxd-sut", "dv-edge-nxdn"): ("KILL", "USR1"),
    ("xlxd-sut", "dv-edge-p25"): ("KILL", "USR1"),
    ("xlxd-sut", "dv-codec-ambed"): ("KILL",),
    ("xlxd-sut", "dv-svc-web"): ("KILL",),       # resets the web service's in-memory limits and lockouts
}


# services whose hub the host will scrape (scripts/lib/metrics.sh), no arguments
METRICS_ALLOWED = ("xlxd-sut",)


def metrics_allowed(service):
    """True when the host watcher would run `metrics service`."""
    return service in METRICS_ALLOWED


def metrics_line(service):
    """The request line, or ValueError for a service outside the allow-list."""
    if not metrics_allowed(service):
        raise ValueError("metrics %s is not allow-listed" % service)
    return "metrics %s" % service


def kill_proc_allowed(service, process, signal):
    """True when the host watcher would run `kill-proc service process signal`."""
    return signal in KILL_PROC_ALLOWED.get((service, process), ())


def kill_proc_line(service, process, signal):
    """The request line, or ValueError for anything outside the allow-list
    (nothing is sent then; the host refuses the same requests)."""
    if not kill_proc_allowed(service, process, signal):
        raise ValueError("kill-proc %s %s %s is not allow-listed" % (service, process, signal))
    return "kill-proc %s %s %s" % (service, process, signal)


@dataclass
class KillResult:
    """What a kill-proc reply says."""
    pids: list = field(default_factory=list)
    t_kill: float = 0.0                 # epoch seconds, inside the container, after the signals

    @property
    def t_kill_monotonic(self):
        """t_kill on the time.monotonic() scale of this process (the scale of
        the timestamps the protocol helpers record)."""
        return self.t_kill - (time.time() - time.monotonic())


def parse_kill_out(text):
    """KillResult of a kill-proc <id>.out; ValueError when it names no pid or
    no kill time (the host answers "error 3" before that when nothing matched)."""
    result = KillResult()
    for line in text.splitlines():
        key, _, value = line.strip().partition("=")
        if key == "pid" and value.isdigit():
            result.pids.append(int(value))
        elif key == "t_kill":
            try:
                result.t_kill = float(value)
            except ValueError as exc:
                raise ValueError("bad t_kill in kill-proc reply: %r" % line) from exc
    if not result.pids or result.t_kill <= 0:
        raise ValueError("kill-proc reply has no pid or no t_kill: %r" % text)
    return result


class RigControl:
    """stop/start/kill of a rig service and kill-proc inside the sut container
    through the run.sh watcher: a request file in $DVT_OUT/control, answered
    by an .ack file."""

    # one counter for every instance of the process: a fixture and a helper that
    # each hold a RigControl must never issue the same request name
    _next_id = itertools.count(1)

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.count = 0

    def _request(self, line, timeout):
        """Send one request line; the request name once it is acked "ok"."""
        self.count += 1
        name = "%d-%d" % (os.getpid(), next(RigControl._next_id))
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(line + "\n")
        tmp.rename(self.dir / (name + ".req"))
        ack = self.dir / (name + ".ack")
        deadline = time.monotonic() + timeout
        while not ack.exists():
            if time.monotonic() > deadline:
                raise TimeoutError("no ack for %s" % line)
            time.sleep(0.1)
        reply = ack.read_text().strip()
        if reply != "ok":
            raise RuntimeError("%s: %s" % (line, reply))
        return name

    def __call__(self, action, service, timeout=30.0):
        self._request("%s %s" % (action, service), timeout)

    def mark(self, phase, tag, timeout=10.0):
        """Log "mark begin|end <tag>" in control.log for dvcheck (tag chars
        A-Za-z0-9._-; ValueError otherwise)."""
        if phase not in ("begin", "end") or not tag or not all(c.isalnum() or c in "._-" for c in tag) \
                or not tag.isascii():
            raise ValueError("bad mark %r %r" % (phase, tag))
        self._request("mark %s %s" % (phase, tag), timeout)

    def kill_proc(self, service, process, signal, timeout=30.0):
        """Signal the process called `process` (exact comm) inside `service`
        (allow-list: KILL_PROC_ALLOWED). Returns a KillResult; raises
        ValueError before sending when the request is not allow-listed,
        RuntimeError("... error 3") when no such process runs, TimeoutError
        when the host does not answer in `timeout` seconds."""
        line = kill_proc_line(service, process, signal)
        name = self._request(line, timeout)
        return parse_kill_out((self.dir / (name + ".out")).read_text())

    def metrics(self, service="xlxd-sut", timeout=30.0):
        """The Prometheus text of the hub of `service` (allow-list:
        METRICS_ALLOWED), fetched on the host from inside its network
        namespace. ValueError before sending when not allow-listed,
        RuntimeError("... error 1") when the GET failed, TimeoutError when the
        host does not answer in `timeout` seconds."""
        name = self._request(metrics_line(service), timeout)
        return (self.dir / (name + ".out")).read_text(encoding="utf-8", errors="replace")
