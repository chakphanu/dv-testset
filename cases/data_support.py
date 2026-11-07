# SPDX-License-Identifier: MIT
"""Shared pieces of the data-channel cases of DV-Router (data.md): the identity cases of
test_data_ident.py and the DP15 cases of test_dp15_*.py (docs/dp15-plan.md, docs/dp15-cases.md).

Moved out of test_data_ident.py without a change of behaviour: the hub counters through the
control channel (`Hub`, `snapshot`, `delta`, `hub_session`), the D-STAR listener and M17 client
context managers, the M17P SMS builder, the text over helpers and the note writer. New for DP15:
the bounds of the 15.5 wording (module constants), a rate pacer for the per-callsign limits of the
controller, an SSE watcher with arrival times and a timeline of events.

Bounds (answer of the DV-Router lead to the DP15 questions, docs/dp15-cases.md; data.md 15.5):
  STATUS_CEILING_S   "a radio TX message appears as the talker's status within one superframe plus
                     pipeline": one superframe is 420 ms, the edge sends one raw unit per superframe, so
                     0.42 to 0.84 s plus tens of ms; the CEILING asserted is 2.0 s (measured and recorded)
  AT_ONCE_S          "at once": the independent paths (M17 packet mode, web, APRS-IS) have no timer;
                     0.5 s from the submission to the listener's datagram
  IDLE_GAP_S         "only in a pause of at least 2 s": [data] idle_gap_ms 2000 from the last voice end
  TOKEN_RENDER_S     "renders resume within 2 s of the token" (worker cases, not in this file's users yet)
"""
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from m17_common import MOD, clear, done, new, ready  # noqa: E402,F401  (MOD, clear re-exported for the case files)
from dvtest import clients, dstar_sd, metrics, slowdata  # noqa: E402
from dvtest.audio import signals  # noqa: E402
from dvtest.proto import dextra, m17  # noqa: E402

SMS = 0x05
LINK_CALLSIGN = "N0CALL"
FAMILIES = ("dv_room_data_total", "dv_pkt_items_total")
QUIET_S = 12.0                  # a baseline is quiet when nothing changed for one refresh and a bit
FAMILY_WAIT_S = 60.0            # the data families appear after the first publish of the controller and worker
POLL_S = 25.0                   # the change shows up within this after the event
OVER_WAIT_S = 15.0              # a text over starts after the floor was idle for idle_gap_ms
RESULTS = ("accepted", "unidentified")
# the per-edge counter is only recorded, never asserted; compat1 removed dv_edge_data_total (dvtest/metrics.py edge_data_family): the
# records then say "n/a (removed on compat1)" and the edge deltas are 0
EDGE_NAME = metrics.edge_data_family() or "dv_edge_data_total n/a (removed on compat1)"

# bounds of the 15.5 wording (see the module docstring)
SUPERFRAME_S = 0.42
STATUS_CEILING_S = 2.0
AT_ONCE_S = 0.5
IDLE_GAP_S = 2.0
TEXT_OVER_S = 0.44
TOKEN_RENDER_S = 2.0
# controller limits per base callsign: status 1 per 10 s, position and object 1 per 30 s (burst 1); room text bucket
# 1 per 5 s, burst 4; web account 1 per 5 s, burst 2. The pacer waits these plus a margin of 1 s plus OFFER_DELAY_S.
# OFFER_DELAY_S: the pacer stamps the CALL, but a D-STAR over reaches the controller only when its slow data text or
# D-PRS line is complete (header settle 0.3 s plus up to 3 superframes of 0.42 s, about 1.6 s) while a datagram
# (M17 packet, SMS) is offered at once. First DP15 rig run: 2c's D-PRS position was offered 1.6 s after its pacer
# call and 2d's M17 APRS packet right after its own call 31 s later, so 29.4 s apart: `rate` (limit 30 s), no
# position row. The margin must cover the longest over so the later offer is never earlier than the limit.
OFFER_DELAY_S = 3.0
MIN_GAP_S = {"status": 11.0 + OFFER_DELAY_S, "position": 31.0 + OFFER_DELAY_S, "text": 6.0}


# ---------------------------------------------------------------- findings

def write_note(target, case, tag, text, setting="", log="data-ident.txt"):
    """Print and append "[tag] case (setting): text" (RECORD, FINDING or OK) to out/<run>/<log>."""
    line = "[%s] %s%s: %s" % (tag, case, " (%s)" % setting if setting else "", text)
    print(line)
    try:
        with open(Path(target.out) / log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- hub counters

def snapshot(samples):
    """The counters the cases read, by flat name: room.<result> (controller,
    dv_room_data_total), pkt.<kind>.<result> (worker, dv_pkt_items_total),
    edge.<result> (dv_edge_data_total; metrics.edge_data_family(): 0 on compat1, which removed it), each also as .all (any result)."""
    snap = {"room.all": metrics.total(samples, "dv_room_data_total"),
            "pkt.all": metrics.total(samples, "dv_pkt_items_total"),
            "edge.all": metrics.edge_total(samples),
            # compat1 successors of dv_edge_data_total: recorded only (their labels are not asserted); 0 on older images
            "edge_raw.all": metrics.total(samples, "dv_edge_raw_total"),
            "edge_render.all": metrics.total(samples, "dv_edge_render_total")}
    for result in RESULTS:
        snap["room." + result] = metrics.total(samples, "dv_room_data_total", result=result)
        snap["edge." + result] = metrics.edge_total(samples, result=result)
        for kind in ("text", "status"):
            snap["pkt.%s.%s" % (kind, result)] = metrics.total(
                samples, "dv_pkt_items_total", kind=kind, result=result)
    for kind in ("text", "status"):
        snap["pkt.%s.all" % kind] = metrics.total(samples, "dv_pkt_items_total", kind=kind)
    return snap


def delta(after, before):
    return {key: after[key] - before[key] for key in after}


def edge_counts_known():
    """True on the generations that have dv_edge_raw_total / dv_edge_render_total (compat1 and newer; they replace dv_edge_data_total)."""
    from dvtest import sut_config
    return sut_config.gen_at_least(None, sut_config.COMPAT1)


def check_edge_counts(change, delivered):
    """By sums only (no label values): dv_edge_raw_total counts the item the edge saw (+1, delivered or not), dv_edge_render_total only
    what was rendered for delivery: > 0 for a delivered text item, 0 for an unidentified one denied under require_ident=true."""
    if not edge_counts_known():
        return
    assert change["edge_raw.all"] == 1, "dv_edge_raw_total moved by %s, want 1: %s" % (change["edge_raw.all"], moved(change))
    if delivered:
        assert change["edge_render.all"] > 0, "dv_edge_render_total did not move for a delivered item: %s" % moved(change)
    else:
        assert change["edge_render.all"] == 0, "dv_edge_render_total moved for a denied item: %s" % moved(change)


def moved(delta_map):
    """The non-zero entries, for printing."""
    return {key: int(value) for key, value in sorted(delta_map.items()) if value}


class Hub:
    """The hub metrics of xlxd-sut through the control channel."""

    def __init__(self, control):
        self.control = control

    def text(self):
        return self.control.metrics("xlxd-sut")

    def snap(self):
        return snapshot(metrics.parse(self.text()))

    def total(self, family, **labels):
        """The sum of one family (label subset) in a fresh read of the hub metrics."""
        return metrics.total(metrics.parse(self.text()), family, **labels)

    def settle(self, quiet=QUIET_S, limit=90.0, poll=2.0):
        """A snapshot after the counters did not change for `quiet` seconds (a
        refresh of an earlier case has landed)."""
        snap, changed, end = self.snap(), time.monotonic(), time.monotonic() + limit
        while time.monotonic() - changed < quiet and time.monotonic() < end:
            time.sleep(poll)
            current = self.snap()
            if current != snap:
                snap, changed = current, time.monotonic()
        return snap

    def wait_for(self, before, pred, timeout=POLL_S, poll=2.0):
        """(snapshot, True) once pred(snapshot, before) holds, else (last
        snapshot, False) after `timeout` seconds."""
        end = time.monotonic() + timeout
        while True:
            current = self.snap()
            if pred(current, before):
                return current, True
            if time.monotonic() >= end:
                return current, False
            time.sleep(poll)

    def wait_total(self, family, minimum, timeout=POLL_S, poll=2.0, **labels):
        """(value, True) once the family (label subset) has reached `minimum`, else (last value, False)."""
        end = time.monotonic() + timeout
        while True:
            value = self.total(family, **labels)
            if value >= minimum:
                return value, True
            if time.monotonic() >= end:
                return value, False
            time.sleep(poll)


def anything_counted(now, before):
    """Both the controller's and the worker's counter moved. They refresh on
    their own 10 s clocks, so the second one may show up one refresh after the first."""
    return now["room.all"] > before["room.all"] and now["pkt.all"] > before["pkt.all"]


def anything_counted_any(now, before):
    return now["room.all"] > before["room.all"] or now["pkt.all"] > before["pkt.all"]


def hub_session(target, rig_control, note_end):
    """Generator behind a module-scoped `hub` fixture: the Hub of the DV-Router SUT with the data worker on; skips with
    the reason otherwise. At the end `note_end(line)` is called for every non-zero data counter line (the case file
    writes them to its note log)."""
    if target.sut_profile != "dvrouter":
        pytest.skip("DV-Router only: DVT_SUT_PROFILE=dvrouter (profile %s)" % target.sut_profile)
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("needs DVT_SUT_LAUNCH=native with the data-ident.toml config extra")
    helper = Hub(rig_control)
    # the controller and the worker publish their counters 10 s after they start (and then
    # every 10 s), so the families may show up only after the rig is up for a while
    end = time.monotonic() + FAMILY_WAIT_S
    while True:
        try:
            text = helper.text()
        except RuntimeError as exc:
            pytest.skip("the hub metrics are not reachable through the control channel (%s)" % exc)
        if set(FAMILIES) & metrics.families(text) or time.monotonic() >= end:
            break
        time.sleep(3.0)
    if not set(FAMILIES) & metrics.families(text):
        pytest.skip("[data] is not enabled: no %s family in the hub metrics after %.0f s; run with "
                    "DVT_SUT_CONFIG_EXTRA=rig/config/dv-router/data-ident.toml" % (" or ".join(FAMILIES), FAMILY_WAIT_S))
    yield helper
    try:
        for line in helper.text().splitlines():
            if line.startswith(("dv_room_data_total", "dv_pkt_items_total", "dv_edge_raw_total", "dv_edge_render_total") + ((metrics.edge_data_family(),) if metrics.edge_data_family() else ())) \
                    and line.rsplit(" ", 1)[-1] not in ("0", "0.0"):
                note_end(line)
    except Exception:                                    # the SUT may be gone; not a case failure
        pass


# ---------------------------------------------------------------- clients

@contextlib.contextmanager
def dstar_listener(ip, module=MOD, callsign="N0TEST"):
    """A DExtra client linked to the module, with an empty inbox."""
    listener = dextra.DstarClient("0.0.0.0", callsign=callsign, module=module)
    try:
        assert listener.link(ip), "DExtra link to %s module %s failed" % (ip, module)
        time.sleep(1.2)                         # the module's client list refreshes once per second
        listener.client.drain()
        listener.client.inbox.clear()
        listener.client.inbox_times.clear()
        yield listener
    finally:
        clients.close(listener)


@contextlib.contextmanager
def m17_client(ip, callsign=LINK_CALLSIGN, module=MOD):
    """A linked M17 client (CONN, not LSTN) with an empty inbox."""
    c = new(callsign, module)
    try:
        ready(ip, c)
        yield c
    finally:
        done(c)


def nonce_body(tag="DVT"):
    return "%s %04d" % (tag, int(time.time() * 10) % 10000)


def sms_packet(body, src):
    """M17P packet-mode SMS: LSF with DST @ALL and the legacy TYPE, specifier
    0x05, UTF-8 body, NUL, payload CRC (dvtest/proto/m17.py)."""
    return m17.m17p_packet(SMS, body.encode("ascii") + b"\0", src=src)


def wait_for_overs(listener, seconds, want=None):
    """Drain the listener for up to `seconds` (stop early when want(overs));
    the overs received so far (dvtest.slowdata.overs)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        listener.client.drain()
        if want is not None and want(slowdata.overs(listener.client.inbox)):
            time.sleep(0.3)
            listener.client.drain()
            break
        time.sleep(0.02)
    return slowdata.overs(listener.client.inbox)


def texts_of(overs):
    """[(stream id, decoded dict)] of the whole text overs among `overs`."""
    out = []
    for over in overs:
        got = slowdata.over_text(over)
        if got:
            out.append((over["stream_id"], got))
    return out


def has_text_with(body):
    return lambda overs: any(got["text"].rstrip().endswith(body) for _sid, got in texts_of(overs))


def describe(over):
    header = over["header"] or {}
    return {"stream_id": over["stream_id"], "frames": over["frames"],
            "my": bytes(header.get("my", b"")).decode("ascii", "replace"),
            "ur": bytes(header.get("ur", b"")).decode("ascii", "replace"),
            "suffix": bytes(header.get("suffix", b"")).decode("ascii", "replace"),
            "seqs": [v["counter"] for v in over["voice"]][:3] + ["..."]}


# ---------------------------------------------------------------- DP15: pacing, timeline, SSE

class Pacer:
    """Waits so that the same (base callsign, kind) is not offered to the controller faster than its limit
    (MIN_GAP_S: status 1 per 10 s, position 1 per 30 s, text 1 per 5 s). `clock` and `sleep` are injectable."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep, gaps=None):
        self.clock, self.sleep, self.gaps = clock, sleep, dict(gaps or MIN_GAP_S)
        self.last = {}

    def wait(self, callsign, kind):
        """Seconds slept; the call counts as the offer."""
        key = (callsign.upper().split("-")[0], kind)
        slept = 0.0
        if key in self.last:
            need = self.last[key] + self.gaps[kind] - self.clock()
            if need > 0:
                self.sleep(need)
                slept = need
        self.last[key] = self.clock()
        return slept


PACER = Pacer()


class Timeline:
    """Thread safe list of (monotonic time, label, detail) events of one case."""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.t0 = time.monotonic()

    def add(self, label, detail=None, at=None):
        with self.lock:
            self.events.append((time.monotonic() if at is None else at, label, detail))

    def first(self, label, since=0.0, pred=None):
        with self.lock:
            for at, name, detail in self.events:
                if name == label and at >= since and (pred is None or pred(detail)):
                    return at, detail
        return None

    def render(self):
        with self.lock:
            return ["%+.3f %s %s" % (at - self.t0, name, "" if detail is None else detail)
                    for at, name, detail in sorted(self.events, key=lambda e: e[0])]


class SseWatch:
    """An SSE stream read in a thread with the arrival time of every frame: `frames` is a list of
    (monotonic time, frame dict). `ready()` is true after the first frame (the hello). Use as a context manager."""

    def __init__(self, client, path, seconds=120.0):
        self.client, self.path, self.seconds = client, path, seconds
        self.frames = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _on_frame(self, frame):
        with self.lock:
            self.frames.append((time.monotonic(), frame))

    def _run(self):
        try:
            self.client.sse(self.path, max_events=1 << 30, timeout=self.seconds, on_frame=self._on_frame, stop=self.stop)
        except Exception as exc:  # noqa: BLE001 - reported through .error
            self.error = exc

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()               # the reader gives its connection back within about 0.5 s
        self.thread.join(2.0)
        return False

    def ready(self, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.frames:
                    return True
            if self.error is not None:
                return False
            time.sleep(0.05)
        return False

    def snapshot(self):
        with self.lock:
            return list(self.frames)

    def data_events(self, since=0.0):
        """[(time, object)] of the `data` events (decoded JSON) after `since`."""
        out = []
        for at, frame in self.snapshot():
            if at >= since and frame.get("event") == "data":
                try:
                    out.append((at, json.loads(frame.get("data", ""))))
                except ValueError:
                    pass
        return out

    def wait_data(self, pred, timeout, since=0.0, poll=0.02):
        """(time, object) of the first data event matching pred, or None."""
        end = time.monotonic() + timeout
        while True:
            for at, obj in self.data_events(since):
                if pred(obj):
                    return at, obj
            if time.monotonic() >= end:
                return None
            time.sleep(poll)


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- DP15: over builders and listeners helpers
# (moved out of test_dp15_streams.py without a change of behaviour; the module of the cases is room B)

ROOM = "B"


def ambe(count, seed):
    return signals.prng_frames(1, count, seed="dp15/%s" % seed)


def plain_fields(count):
    """(seq, wire data field) of `count` frames with sync and the idle slow data (zeros), like DstarClient.talk."""
    return [(i % 21, dextra.SYNC_DATA if i % 21 == 0 else dextra.SLOW_DATA) for i in range(count)]


def build_over(callsign, fields, frames=None, module=ROOM, sid=None):
    sid = sid or clients.fresh_stream_id("dstar")
    radio = dextra.radio_header(my=callsign, module=module)
    return sid, dstar_sd.over_packets(sid, fields, frames, radio=radio)


def send_over(talker, packets, settle=0.3, gap=0.020):
    """Header, settle, then the rest paced at `gap`. Returns (t_header, voice send stamps); the last stamp is the last frame."""
    t_header = time.monotonic()
    talker.client.send(talker.dest, packets[0])
    talker.client.pump(settle)
    return t_header, talker.client.paced(talker.dest, packets[1:], gap)


def send_over_thread(talker, packets, out, settle=0.3, gap=0.020):
    thread = threading.Thread(target=lambda: out.append(send_over(talker, packets, settle, gap)), daemon=True)
    thread.start()
    return thread


def pump_for(seconds, *listeners):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for item in listeners:
            item.client.drain()
        time.sleep(0.01)


def header_times(listener):
    """[(arrival time, parsed header)] of the DSVT headers in a listener's inbox."""
    out = []
    for data, at in zip(listener.client.inbox, listener.client.inbox_times):
        parsed = dextra.parse_header_full(data)
        if parsed:
            out.append((at, parsed))
    return out


def first_header_times(listener, pred=None):
    """[(arrival time of the FIRST header of each stream id, parsed header)] of a listener, optionally filtered by
    pred(header). DV-Router's DExtra edge sends every header 5 times at the start of an over (xlxd repeatCount 5,
    crates/edge/dextra/src/wire.rs HEADER_REPEAT): the five datagrams arrive within microseconds and are ONE over.
    First DP15 RF run: 6a / 6e counted five D-STAR text overs for one frame."""
    seen, out = set(), []
    for at, header in header_times(listener):
        if header["stream_id"] in seen or (pred is not None and not pred(header)):
            continue
        seen.add(header["stream_id"])
        out.append((at, header))
    return out


def text_headers(listener):
    """Arrival times of the headers of synthetic text overs (MY = the reflector callsign), one per over."""
    return [at for at, _h in first_header_times(listener, lambda h: bytes(h["my"]).rstrip() == b"XLX999")]


def over_by_my(overs, my):
    for over in overs:
        header = over["header"] or {}
        if bytes(header.get("my", b"")).rstrip() == my.encode():
            return over
    return None


def m17p_packets(client):
    """[(arrival time, parsed M17P datagram)] of a linked M17 client."""
    out = []
    for data, at in zip(client.client.inbox, client.client.inbox_times):
        parsed = m17.parse_m17p(data, trailer=False) if data.startswith(b"M17P") else None
        if parsed:
            out.append((at, parsed))
    return out


def sms_text(parsed):
    """The text of a parsed M17P SMS (specifier 0x05), else None."""
    payload = parsed["payload"]
    if not payload or payload[0] != 0x05:
        return None
    return payload[1:].split(b"\0", 1)[0].decode("utf-8", "replace")


def m17_voice_payloads(client):
    """The 16 byte payloads of the stream packets a linked M17 client received, in arrival order."""
    return [p["payload"] for p in (m17.parse_stream(d) for d in client.client.inbox) if p]
