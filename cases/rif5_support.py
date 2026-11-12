# SPDX-License-Identifier: MIT
"""Rig helpers of cases/test_rif5_mmdvm_serial.py (docs/rif5-cases.md): the notes file, timed protocol clients on a room, waits on the fake
modem's log, the readers of the port state (public state, admin clients) and the restart of dv-edge-radio. The pure part is dvtest/rif5.py and
the fake modem dvtest/mmdvm_serial.py (unit tests cases/unit/test_rif5_lib.py, test_mmdvm_serial.py)."""
import threading
import time
from pathlib import Path

from dvtest import clients, echo, rif5, txguard
from dvtest import mmdvm_serial as ms


# ---------------------------------------------------------------- notes

def note(target, case, tag, text):
    """Print "[tag] case: text" (RECORD, FINDING or OK) and append it to out/<run>/rif5.txt."""
    line = "[%s] %s: %s" % (tag, case, text)
    print(line)
    try:
        with open(Path(target.out) / "rif5.txt", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- protocol clients on a room

class Drain(threading.Thread):
    """Drains a UdpClient every few ms so that inbox_times tell when a packet arrived."""

    def __init__(self, udp):
        super().__init__(daemon=True)
        self.udp = udp
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            try:
                self.udp.drain()
            except Exception:                       # a closed socket at teardown
                return
            time.sleep(0.003)

    def stop(self):
        self.halt.set()
        self.join(2.0)


class Member:
    """One protocol client of `kind` (identity `index` of dvtest.clients: 0 = N0CALL, 1 = N0TEST) linked to `module` of the SUT. As a
    listener its packets are timed by a drain thread (overs()); as a talker talk() sends one over."""

    def __init__(self, ip, kind, module, index, listen=True):
        self.kind, self.module, self.index = kind, module, index
        self.client = clients.make(kind, module, index)
        if not self.client.link(ip):
            try:
                clients.close(self.client)
            except Exception:
                pass
            raise AssertionError("%s client %d did not link to %s (module %s)" % (kind, index, ip, module))
        self.client.client.drain()
        self.client.client.inbox.clear()
        self.client.client.inbox_times.clear()
        self.drain = Drain(self.client.client) if listen else None
        if self.drain:
            self.drain.start()

    def ident(self):
        """The client as the packets name it: the D-STAR MY, the DMR id, the YSF callsign."""
        c = self.client
        if self.kind == "dstar":
            return c.callsign.decode("ascii").strip()
        if self.kind == "dmr":
            return c.id
        return c.callsign

    def overs(self, gap=0.4):
        """The overs heard so far (streams with voice only: the empty header + terminator join of DMR / YSF is no over)."""
        data, times = list(self.client.client.inbox), list(self.client.client.inbox_times)
        found = echo.split_overs(echo.events(self.kind, data, times), gap)
        return [o for o in found if o.n_voice > 0]

    def talk(self, frames, tail=0.5):
        if self.drain:
            self.drain.stop()
            self.drain = None
        sid = clients.talk(self.client, self.kind, frames, tail=tail)
        return sid

    def close(self, settle=1.0):
        if self.drain:
            self.drain.stop()
            self.drain = None
        try:
            clients.close(self.client)
        except Exception:
            pass
        time.sleep(settle)


def wait_for(predicate, timeout, step=0.1):
    """Poll predicate() until it returns something truthy (returned) or timeout seconds passed (None)."""
    end = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= end:
            return None
        time.sleep(step)


# ---------------------------------------------------------------- waits on the modem log

def quiesce(modem, since, types, quiet=1.5, timeout=30.0, need=1):
    """Wait until the host wrote at least `need` frames of `types` after `since` and none for `quiet` seconds. Returns the frames written
    after `since` (all types) or None on timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        mine = modem.host_frames(since, types=types)
        if len(mine) >= need and time.monotonic() - mine[-1].mono >= quiet:
            return modem.host_frames(since)
        time.sleep(0.1)
    return None


def settle_idle(modem, timeout=30.0):
    """Wait for the host to give the modem back to idle (its SET_MODE 0 after the mode hang) when its last SET_MODE claimed a mode: another
    mode is claimed only from idle (radio.md 7.2, an over of another mode meanwhile is `mode_busy`). True when idle or already idle."""
    modes = modem.host_frames(types=(ms.SET_MODE,))
    if not modes or modes[-1].payload == bytes((ms.MODE_IDLE,)):
        return True
    return modem.wait_idle_mode(modes[-1].mono, timeout) is not None


def last_dialogue(modem, complete=True):
    """The latest open dialogue of the log (the one that reached SET_CONFIG when complete), or None."""
    found = [d for d in rif5.dialogues(modem.host_frames()) if d.config is not None or not complete]
    return found[-1] if found else None


# ---------------------------------------------------------------- the port state

PUBLIC_STATE = "/api/v1/public/state"


def public_row(web, name=rif5.PORT_NAME):
    """The public-state row of the port (observer fields), or None."""
    return rif5.public_port(web.get(PUBLIC_STATE, expect=200).json(), name)


def admin_row(operator, name=rif5.PORT_NAME):
    """The admin row of the port (all fields, `down_reason` among them) from GET /api/v1/admin/clients of an operator session, or None.
    Opens the step-up window when the route asks for it (403)."""
    resp = operator.get(operator.path("/clients"), expect=None)
    if resp.status == 403:
        operator.step_up()
        resp = operator.get(operator.path("/clients"), expect=None)
    if resp.status != 200:
        return None
    return rif5.admin_port(resp.json(), name)


def child_state(status_body, name=rif5.CHILD):
    """{pid, restarts, state, since_ms} of the launcher child `name` in GET /api/v1/admin/status, or None."""
    launcher = status_body.get("launcher") if isinstance(status_body, dict) else None
    for child in (launcher or {}).get("children") or []:
        if isinstance(child, dict) and child.get("name") == name:
            return {k: child.get(k) for k in ("pid", "restarts", "state", "since_ms")}
    return None


def restart_radio_edge(admin):
    """POST /api/v1/admin/children/dv-edge-radio/restart with an admin session (step-up first), as RFSAFE1c restarts dv-edge-ax25. The new
    process starts with the startup guard. Returns the HTTP status."""
    admin.step_up()
    resp = admin.post(admin.path("/children/%s/restart" % rif5.CHILD), {}, expect=None)
    return resp.status


def wait_unfenced(web, name=rif5.PORT_NAME):
    """txguard.Result for the port: waits until the public state shows no fence."""
    return txguard.wait_unfenced(txguard.radio_reader(web, name), kind="radio")
