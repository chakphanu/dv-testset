# SPDX-License-Identifier: MIT
"""DV-Router DP11e rig cases: the KISS TCP server of the AX.25 edge (`[[edges.ax25.kiss_server]]`, data.md 10.10, config.md, review 235). The
tester plays the fake KISS modems rf-a / rf-b (dvtest/kiss_tnc.py, as RF tier A of cases/test_dp15_rf.py) AND the APRS app (dvtest/kiss_app.py,
an APRSdroid / PinPoint-like client). Case table, shapes, run commands, limits: docs/dp11e-cases.md; helpers: dvtest/dp11e.py; the
configuration refusals are cases/test_dp11e_config.py (host side, no rig).

Shape K (rig/config/dv-router/dp11e-kiss.toml): listeners apps / rxonly / txdeny / outside.  Shape B (dp11e-kiss-b.toml): loop / rate / sim.

  DP11e-a  ASCII outside frames (an APRSdroid init string, text between frames) is ignored, the client stays open, the frames are served
  DP11e-b  the TX gate: one client frame per refusal label (tx_off, tx_addr, channel, bad_frame, not_ui, src, path, too_long, third_party,
           command) is refused and counted, nothing reaches the modem, the control is queued and sent AS SENT (source, destination, path)
  DP11e-c  connections: a peer outside allow_from is closed before any byte (refused_addr); the fourth client of three is refused (full)
  DP11e-d  the lone-FEND keepalive: on (keepalive_secs 10) an idle client of 130 s is kept alive and sees the FENDs at the period; off
           (keepalive_secs 0) it sees zero bytes
  DP11e-e  RX fan-out: UI/PID F0 frames of an exposed port reach every client with the mapped channel, other frames and unmapped ports do not;
           the sender never gets its own frame back
  DP11e-g  client frames never reach APRS-IS or rooms (needs the private aprsc and dp11e-kiss-igate.toml; otherwise the counter half only)
  DP11e-h  sessions (kind kiss_client) in the admin client list, kick, and no peer address or frame content in the SUT log
  DP11e-i  slow client: the output queue drops the newest (slow) and a 30 s write stall closes the client (stalled)       [slow]
  DP11e-j  an AX.25 edge restart closes the clients, the listener rebinds, a frame right after the restart meets the RFSAFE1 guard (rf_fenced)
  DP11e-k  (shape B) a loopback listener is not reachable from another host (the default bind is loopback)
  DP11e-l  (shape B) the listener packet bucket (tx_per_min 2, tx_burst 1): `rate`
  DP11e-m  (shape B) a simulated port: `simulated`, no modem byte, no PTT
  DP11e-z  no admission is ever counted `disabled` in a valid configuration (a TTL-less client frame cannot be provoked black-box)

Every case skips with a reason on an image without the kiss_server schema, without the shape, or without the prerequisite service.
NOT RUN ON A RIG (written against data.md 10.10 / config.md at cfa765a and review 235). Observations: stdout and out/<run>/dp11e-kiss.txt.
"""
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import ax25, dp11e as dp, kiss_app, kiss_tnc, metrics, txguard  # noqa: E402

import data_support as ds  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_rf as tr  # noqa: E402
# the fake modems rf-a / rf-b (their `hub` argument is the fixture of THIS module) and the private aprsc with its `shape`, reused by name
from test_dp15_positions import aprsc, shape  # noqa: E402,F401
from test_dp15_rf import rf  # noqa: E402,F401

pytestmark = pytest.mark.rig

LOG = "dp11e-kiss.txt"
SETTING = "dp11e"
COUNTER_WAIT_S = 45.0               # the edge publishes its counters every 10 s
QUIET_S = 12.0                      # one more publish: a surplus count lands here
MODEM_WAIT_S = 25.0
SILENCE_S = 130.0                   # longer than the 120 s read timeout of APRSdroid
KEEPALIVE_ON = 10                   # keepalive_secs of the listener `apps` in dp11e-kiss.toml
APP_TIMEOUT_S = kiss_app.APRSDROID_SO_TIMEOUT_S


def note(target, case, tag, text):
    ds.write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def kiss(request, target, hub, admin_session):
    """The run shape (listeners of the rendered config), the capability probe and the fake modems; skips with the reason otherwise."""
    doc = p1.read_config(target)
    shp = dp.kiss_shape(doc)
    if shp is None:
        pytest.skip("the rendered config has no [[edges.ax25.kiss_server]]: add rig/config/dv-router/dp11e-kiss.toml (or dp11e-kiss-b.toml) "
                    "INSTEAD of dp15-rf.toml (docs/dp11e-cases.md)")
    admin = admin_session("operator")
    schema = admin.get(admin.path("/schema"), expect=None)
    if schema.status != 200 or "kiss_server" not in schema.text():
        pytest.skip("the image does not know DP11e: the configuration schema has no `kiss_server` (HTTP %d)" % schema.status)
    tnc = request.getfixturevalue("rf")
    note(target, "kiss", "RECORD", "shape: station %s, ports %s, listeners %s" % (shp.station, shp.ports, {
        n: "%s:%d allow_from=%s tx=%s keepalive=%d channels=%s" % (l.host, l.port, l.allow_from, l.tx, l.keepalive_secs, l.channels)
        for n, l in shp.listeners.items()}))
    return types.SimpleNamespace(shape=shp, tnc=tnc, hub=hub, admin=admin, admin_session=admin_session, request=request)


def listener(kiss, name):
    lst = kiss.shape.listeners.get(name)
    if lst is None:
        pytest.skip("this run has no listener %r (shape %s)" % (name, "K" if "apps" in kiss.shape.listeners else "B"))
    return lst


def app(kiss, name, **kw):
    """A connected AppKissClient on listener `name`."""
    lst = listener(kiss, name)
    client = kiss_app.AppKissClient(lst.host, lst.port, name="app-" + name, **kw)
    assert client.connect(), "cannot connect to listener %s (%s:%d): %s" % (name, lst.host, lst.port, client.error)
    return client


def samples(kiss):
    return metrics.parse(kiss.hub.text())


def counters(kiss, name):
    s = samples(kiss)
    return {"frames": dp.frame_counts(s, name), "clients": dp.client_counts(s, name)}


def settled(kiss, name):
    """The counters of listener `name` once they did not change for QUIET_S (an earlier publish has landed)."""
    last, since, end = counters(kiss, name), time.monotonic(), time.monotonic() + 90.0
    while time.monotonic() - since < QUIET_S and time.monotonic() < end:
        time.sleep(3.0)
        now = counters(kiss, name)
        if now != last:
            last, since = now, time.monotonic()
    return last


def wait_frames_delta(kiss, name, before, want, timeout=COUNTER_WAIT_S):
    """{(dir, result): delta} of dv_edge_kiss_frames_total once every `want` key reached its count (or `timeout`), then QUIET_S more."""
    end = time.monotonic() + timeout
    while True:
        got = dp.delta(counters(kiss, name)["frames"], before["frames"])
        if all(got.get(k, 0) >= n for k, n in want.items()) or time.monotonic() >= end:
            break
        time.sleep(3.0)
    time.sleep(QUIET_S)
    return dp.delta(counters(kiss, name)["frames"], before["frames"])


def modem_frames(kiss, since, port="a"):
    """[(t, tnc2 text)] of the data frames the edge wrote to fake modem rf-a (or rf-b) since `since`."""
    tnc = kiss.tnc.a if port == "a" else kiss.tnc.b
    return [(t, ax25.to_tnc2(f)) for t, _ch, f in tnc.data_frames(since)]


def guard(kiss, case, port="rf-a"):
    kiss.tnc.tx_guard(case)


# ================================================================ DP11e-a: ASCII outside frames

def test_dp11e_a_ascii_outside_frames_is_ignored(rig, target, kiss):
    """data.md 10.10: bytes before the first FEND (an APRSdroid `kiss.init` string) are discarded uncounted; text between frames is one non-data
    record counted `command` or `bad_frame`; neither closes the client, and the frames around the text are served."""
    case = "DP11e-a"
    guard(kiss, case)
    before = settled(kiss, "apps")
    client = app(kiss, "apps")
    try:
        t0 = time.monotonic()
        first = dp.good_frame(dp.info_for("ascii1"))
        assert client.send_raw(kiss_app.aprsdroid_init_bytes("ATE0%0AKISS%20ON")) and client.send_frame(first)
        got = []
        end = time.monotonic() + MODEM_WAIT_S
        while not got and time.monotonic() < end:
            got = [m for _t, m in modem_frames(kiss, t0) if "dp11e-ascii1" in m]
            time.sleep(0.2)
        assert got == [ax25.to_tnc2(first)], "the frame after the init text was not served as sent: %s" % got
        second = dp.good_frame(dp.info_for("ascii2"))
        assert client.send_raw(b"\r\n") and client.send_frame(second)
        got2 = []
        end = time.monotonic() + MODEM_WAIT_S
        while not got2 and time.monotonic() < end:
            got2 = [m for _t, m in modem_frames(kiss, t0) if "dp11e-ascii2" in m]
            time.sleep(0.2)
        assert got2 == [ax25.to_tnc2(second)], "the frame after text between frames was not served as sent: %s" % got2
        assert client.is_open and client.closed_at is None, "the server closed the client after ASCII outside frames"
    finally:
        client.close()
    delta = wait_frames_delta(kiss, "apps", before, {("in", "queued"): 2})
    note(target, case, "RECORD", "dv_edge_kiss_frames_total{port=apps,dir=in} deltas: %s" % delta)
    assert delta.get(("in", "queued"), 0) == 2, "queued moved by %s, expected 2 (%s)" % (delta.get(("in", "queued"), 0), delta)
    text_records = delta.get(("in", "command"), 0) + delta.get(("in", "bad_frame"), 0)
    assert text_records == 1, ("the init text before the first FEND counts nothing and the CRLF between frames is ONE non-data record counted command or "
                               "bad_frame, got %d (%s)" % (text_records, delta))


# ================================================================ DP11e-b: the TX gate, one label each

@pytest.fixture(scope="module")
def refusal_run(kiss, target):
    """Every refusal frame sent once through its listener (K shape); returns the table, the counters' deltas per listener and what the modem got."""
    if not {"apps", "rxonly", "txdeny"} <= set(kiss.shape.listeners):
        pytest.skip("shape K (dp11e-kiss.toml) is needed for the TX gate cases")
    guard(kiss, "DP11e-b")
    table = dp.refusal_frames()
    before = {n: settled(kiss, n) for n in ("apps", "rxonly", "txdeny")}
    t0 = time.monotonic()
    clients = {n: app(kiss, n) for n in ("apps", "rxonly", "txdeny")}
    try:
        for r in table:
            client = clients[r.listener]
            if r.kind == "command":
                assert client.send_command(r.payload[0], r.payload[1], channel=r.channel if r.payload[0] == 15 else 0)
            else:
                assert client.send_frame(r.payload, channel=r.channel), "write of %s failed: the server closed the client" % r.id
            time.sleep(0.15)
        time.sleep(MODEM_WAIT_S / 2)
        still_open = {n: c.is_open for n, c in clients.items()}
    finally:
        for c in clients.values():
            c.close()
    want = dp.expected_in_deltas(table)
    deltas = {}
    for name, labels in want.items():
        deltas[name] = wait_frames_delta(kiss, name, before[name], {("in", lab): n for lab, n in labels.items()})
    at_modem = modem_frames(kiss, t0)
    note(target, "DP11e-b", "RECORD", "deltas per listener %s; frames at the modem: %s; clients still open: %s" % (deltas, [m for _t, m in at_modem], still_open))
    return types.SimpleNamespace(table=table, want=want, deltas=deltas, at_modem=at_modem, still_open=still_open)


LABELS = sorted({r.label for r in dp.refusal_frames()})


@pytest.mark.parametrize("label", LABELS)
def test_dp11e_b_each_refusal_label_is_counted(label, refusal_run):
    """data.md 10.10, in order: the first failing step is counted, per listener: tx_off / tx_addr (listener, peer, port), channel, bad_frame,
    not_ui, src, path (H bit, TCPIP, qA*, hops), too_long, third_party, command; queued for the control."""
    for name, labels in refusal_run.want.items():
        if label not in labels:
            continue
        got = refusal_run.deltas[name].get(("in", label), 0)
        assert got == labels[label], "listener %s: dv_edge_kiss_frames_total{dir=in,result=%s} moved by %d, expected %d (all deltas %s)" % (
            name, label, got, labels[label], refusal_run.deltas[name])


def test_dp11e_b_only_the_control_reaches_the_modem_as_sent(refusal_run):
    """No refused frame is transmitted; the queued control leaves unchanged: source, destination and via path as the app sent them (no tocall
    or tx_path rewrite). No refusal closes a client."""
    control = next(r for r in refusal_run.table if r.id == "queued")
    sent = [m for _t, m in refusal_run.at_modem]
    assert sent == [ax25.to_tnc2(control.payload)], "the modem got %s, expected exactly the control %s" % (sent, ax25.to_tnc2(control.payload))
    assert all(refusal_run.still_open.values()), "a refusal closed a client: %s" % refusal_run.still_open


# ================================================================ DP11e-c: connection gates

def test_dp11e_c_a_peer_outside_allow_from_is_closed_before_any_byte(rig, target, kiss):
    """data.md 10.10: a non-loopback listener checks the peer against allow_from before reading; a refused peer is closed (refused_addr)."""
    before = settled(kiss, "outside")
    client = app(kiss, "outside")
    try:
        assert client.wait_closed(5.0), "the listener kept a peer that is outside allow_from"
        assert client.bytes_received == 0, "a refused peer received %d bytes" % client.bytes_received
    finally:
        client.close()
    end = time.monotonic() + COUNTER_WAIT_S
    while time.monotonic() < end and counters(kiss, "outside")["clients"].get("refused_addr", 0) - before["clients"].get("refused_addr", 0) < 1:
        time.sleep(3.0)
    delta = dp.delta(counters(kiss, "outside")["clients"], before["clients"])
    note(target, "DP11e-c", "RECORD", "dv_edge_kiss_clients_total{port=outside} deltas: %s" % delta)
    assert delta.get("refused_addr", 0) >= 1 and delta.get("accepted", 0) == 0, "refused_addr/accepted deltas: %s" % delta


def test_dp11e_c_the_fourth_client_of_three_is_refused_full(rig, target, kiss):
    """data.md 10.10: each listener accepts at most max_clients (3 on `apps`); the next client is closed and counted `full`; the gauge shows 3."""
    lst = listener(kiss, "apps")
    before = settled(kiss, "apps")
    held = [app(kiss, "apps") for _ in range(lst.max_clients)]
    extra = None
    try:
        time.sleep(1.0)
        assert all(c.is_open for c in held)
        extra = app(kiss, "apps")
        assert extra.wait_closed(5.0) and extra.bytes_received == 0, "client %d of %d was not refused" % (lst.max_clients + 1, lst.max_clients)
        end = time.monotonic() + COUNTER_WAIT_S
        gauge = None
        while time.monotonic() < end:
            gauge = dp.clients_gauge(samples(kiss), "apps")
            if gauge == lst.max_clients and counters(kiss, "apps")["clients"].get("full", 0) > before["clients"].get("full", 0):
                break
            time.sleep(3.0)
        delta = dp.delta(counters(kiss, "apps")["clients"], before["clients"])
        note(target, "DP11e-c", "RECORD", "dv_edge_kiss_clients_total{port=apps} deltas %s, gauge dv_edge_kiss_clients %s" % (delta, gauge))
        assert delta.get("full", 0) >= 1, "full did not move: %s" % delta
        assert gauge == lst.max_clients, "dv_edge_kiss_clients{port=apps} = %s, expected %d" % (gauge, lst.max_clients)
    finally:
        for c in held + ([extra] if extra else []):
            c.close()


# ================================================================ DP11e-d: the keepalive

def test_dp11e_d_keepalive_on_and_off(rig, target, kiss):
    """data.md 10.10: after keepalive_secs without a written server byte the edge sends a lone FEND, so an app with a read timeout (APRSdroid: 120 s)
    stays attached on a quiet channel; keepalive_secs = 0 sends nothing. `apps` has 10 s, `rxonly` 0; one idle client on each for 130 s,
    nothing injected at the modem."""
    case = "DP11e-d"
    on, off = listener(kiss, "apps"), listener(kiss, "rxonly")
    assert on.keepalive_secs == KEEPALIVE_ON and off.keepalive_secs == 0, "the snippet changed: keepalive %d / %d" % (on.keepalive_secs, off.keepalive_secs)
    a, b = app(kiss, "apps"), app(kiss, "rxonly")
    try:
        start = time.monotonic()
        time.sleep(SILENCE_S)
        end = time.monotonic()
        problems = dp.keepalive_problems(a.lone_fends(start), on.keepalive_secs, start, end, min_count=int(SILENCE_S // on.keepalive_secs) - 3)
        note(target, case, "RECORD", "keepalive on (%d s): %d lone FENDs in %.0f s, intervals %s, longest silence %.1f s; keepalive off: %d bytes" % (
            on.keepalive_secs, len(a.lone_fends(start)), end - start, a.fend_intervals(start), a.max_silence(start, end), b.bytes_received))
        assert a.is_open, "the client of the keepalive listener was closed"
        assert not problems, "; ".join(problems)
        assert not a.would_time_out(APP_TIMEOUT_S, start, end), "an app with a %d s timeout would have dropped the keepalive listener" % APP_TIMEOUT_S
        assert a.frames(start) == [] and a.other_commands() == [], "the keepalive listener sent more than FEND bytes"
        assert b.is_open, "the listener with keepalive_secs = 0 closed an idle client"
        assert b.bytes_received == 0 and b.lone_fends(start) == [], "keepalive_secs = 0 sent %d bytes" % b.bytes_received
        assert b.would_time_out(APP_TIMEOUT_S, start, end), "the idle client of the keepalive-off listener saw bytes in %.0f s" % (end - start)
    finally:
        a.close()
        b.close()


# ================================================================ DP11e-e: RX fan-out

def test_dp11e_e_fan_out_to_every_client_with_the_mapped_channel_and_no_echo(rig, target, kiss):
    """data.md 10.10: every UI/PID F0 frame of an exposed port goes to every attached client with its mapped channel (rf-a = 0, rf-b = 1 on `apps`,
    rf-a = 0 on `rxonly`), also frames that position policy or the worker would refuse; other frames (non-UI) and unmapped ports do not;
    a client's own transmission is never echoed to anyone."""
    case = "DP11e-e"
    guard(kiss, case)
    before_apps, before_rx = settled(kiss, "apps"), settled(kiss, "rxonly")
    a1, a2, r1 = app(kiss, "apps"), app(kiss, "apps"), app(kiss, "rxonly")
    try:
        time.sleep(1.0)
        t0 = time.monotonic()
        heard = kiss_tnc.ui_frame("N0TEST-3", "APDVT1", "=1030.00N/02015.00E>dp11e-heard-a", digis=("WIDE1-1*",))
        heard_nogate = kiss_tnc.ui_frame("N0TEST-3", "APDVT1", "_11100100c220s004g005t077dvt1", digis=("NOGATE",))
        heard_b = kiss_tnc.ui_frame("N0TEST-4", "APDVT1", "=1030.00N/02015.00E>dp11e-heard-b")
        not_ui = ax25.from_tnc2("N0TEST-3>APDVT1:dp11e-sabm", control=0x2F)
        assert kiss.tnc.a.inject(heard) and kiss.tnc.a.inject(heard_nogate) and kiss.tnc.a.inject(not_ui) and kiss.tnc.b.inject(heard_b)
        for c in (a1, a2, r1):
            c.wait_frames(2, timeout=10.0, since=t0)
        time.sleep(2.0)
        for name, c in (("apps#1", a1), ("apps#2", a2)):
            got = [(ch, ax25.to_tnc2(f)) for _t, ch, f in c.frames(t0)]
            assert (0, ax25.to_tnc2(heard)) in got and (0, ax25.to_tnc2(heard_nogate)) in got, "%s: rf-a frames not delivered on channel 0: %s" % (name, got)
            assert (1, ax25.to_tnc2(heard_b)) in got, "%s: the rf-b frame was not delivered on channel 1: %s" % (name, got)
            assert len(got) == 3, "%s received %d frames, expected 3 (the non-UI frame must not be fanned out): %s" % (name, len(got), got)
        got_r = [(ch, ax25.to_tnc2(f)) for _t, ch, f in r1.frames(t0)]
        assert sorted(got_r) == sorted([(0, ax25.to_tnc2(heard)), (0, ax25.to_tnc2(heard_nogate))]), "rxonly (rf-a only) received %s" % got_r
        # the app reader takes the channel-0 frames and not the channel-1 one
        reader = kiss_app.AprsdroidReader()
        taken = [f for _t, ch, f in a1.frames(t0) if reader.feed(ax25.kiss_encode(ch, 0, f))]
        assert len(taken) == 2 and reader.dropped == 1, "the APRSdroid reader took %d frames and dropped %d" % (len(taken), reader.dropped)
        # no echo: a1 transmits, nobody gets it back
        t1 = time.monotonic()
        mine = dp.good_frame(dp.info_for("echo"))
        assert a1.send_frame(mine)
        got = []
        end = time.monotonic() + MODEM_WAIT_S
        while not got and time.monotonic() < end:
            got = [m for _t, m in modem_frames(kiss, t1) if "dp11e-echo" in m]
            time.sleep(0.2)
        assert got == [ax25.to_tnc2(mine)], "the client frame did not reach the modem: %s" % got
        time.sleep(8.0)
        for name, c in (("apps#1 (the sender)", a1), ("apps#2", a2), ("rxonly", r1)):
            echoed = [ax25.to_tnc2(f) for _t, _ch, f in c.frames(t1)]
            assert echoed == [], "%s received %s after a client transmission: an echo or KISSCOPY" % (name, echoed)
    finally:
        for c in (a1, a2, r1):
            c.close()
    got_apps = wait_frames_delta(kiss, "apps", before_apps, {("out", "sent"): 6})
    got_rx = wait_frames_delta(kiss, "rxonly", before_rx, {("out", "sent"): 2})
    note(target, case, "RECORD", "dv_edge_kiss_frames_total deltas: apps %s, rxonly %s" % (got_apps, got_rx))
    assert got_apps.get(("out", "sent"), 0) == 6, "apps delivered %d frames, expected 6 (3 frames x 2 clients)" % got_apps.get(("out", "sent"), 0)
    assert got_rx.get(("out", "sent"), 0) == 2, "rxonly delivered %d frames, expected 2" % got_rx.get(("out", "sent"), 0)


# ================================================================ DP11e-g: never to APRS-IS or rooms

def test_dp11e_g_client_frames_never_reach_aprs_is_or_rooms(rig, target, kiss, request):
    """data.md 10.10: client frames go only to the TX scheduler; never to rooms (raw units, items) or APRS-IS. A client frame is transmitted; the
    raw / item / room counters do not move and the private aprsc sees nothing from its source; a frame HEARD on rf-a (the control) does reach
    the aprsc over the transit path of dp11e-kiss-igate.toml, proving the path is alive."""
    case = "DP11e-g"
    listener(kiss, "apps")
    guard(kiss, case)
    families = ("dv_edge_raw_total", "dv_pkt_raw_total", "dv_pkt_items_total", "dv_room_data_total")

    def totals():
        s = samples(kiss)
        return {f: sum(int(x.value) for x in s if x.name == f) for f in families}

    try:
        host = request.getfixturevalue("aprsc")
        paths = (p1.read_config(target).get("data") or {}).get("path") or []
        igate = any(p.get("from") == "edge:ax25/rf-a" and p.get("to") == "edge:aprsis" and "transit" in (p.get("types") or []) for p in paths)
    except pytest.skip.Exception as exc:
        host, igate = None, False
        note(target, case, "RECORD", "no private aprsc: only the counter half runs (%s)" % exc)
    if host and not igate:
        host = None
        note(target, case, "RECORD", "no transit path edge:ax25/rf-a -> edge:aprsis (dp11e-kiss-igate.toml): only the counter half runs")
    before = totals()
    time.sleep(QUIET_S)
    before = totals()
    client = app(kiss, "apps")
    login = p1.aprsc_login(host) if host else None
    tester = login.__enter__() if login else None
    try:
        mark = tester.mark() if tester else 0
        t0 = time.monotonic()
        mine = dp.good_frame(dp.info_for("never_is"), src=dp.SRC2)
        assert client.send_frame(mine)
        got = []
        end = time.monotonic() + MODEM_WAIT_S
        while not got and time.monotonic() < end:
            got = [m for _t, m in modem_frames(kiss, t0) if "dp11e-never_is" in m]
            time.sleep(0.2)
        assert got == [ax25.to_tnc2(mine)], "the client frame did not reach the modem: %s" % got
        time.sleep(20.0 + QUIET_S)
        after = totals()
        note(target, case, "RECORD", "counter totals before %s after %s" % (before, after))
        assert after == before, "a client frame moved raw / item / room counters: %s -> %s" % (before, after)
        if tester:
            lines = [p["raw"] for _a, p in tester.matching(lambda p: p["src"] == dp.SRC2, mark)]
            assert not lines, "a client frame reached APRS-IS: %s" % lines
            control = kiss_tnc.ui_frame("N0TEST-3", "APDVT1", "=1030.00N/02015.00E>dp11e-control", digis=("WIDE1-1*",))
            assert kiss.tnc.a.inject(control)
            hit = tester.wait_packet(lambda p: p["src"] == "N0TEST-3", 45.0, since=mark)
            assert hit is not None, "the control frame heard on rf-a never reached the aprsc: the absence above proves nothing"
            note(target, case, "RECORD", "control reached the aprsc: %s" % hit[2])
    finally:
        client.close()
        if login:
            login.__exit__(None, None, None)


# ================================================================ DP11e-h: sessions, kick, log

def find_session(kiss, name):
    admin = kiss.admin
    admin.step_up() if getattr(admin, "authenticator", None) else None
    resp = admin.get(admin.path("/clients"), expect=None)
    if resp.status != 200:
        return None, resp
    items = (resp.json() or {}).get("items") or []
    for item in items:
        if isinstance(item, dict) and item.get("kind") == "kiss_client" and name in (item.get("callsign"), item.get("name"), item.get("listener")):
            return item, resp
    return None, resp


def test_dp11e_h_sessions_are_listed_and_kick_closes_the_client(rig, target, kiss):
    """data.md 10.10: administrative sessions are `kiss_client` (callsign = listener name, IP and port); kick closes the client (no timed ban) and
    counts `kicked`. Route shapes were read from the documents, not seen on a rig (UNVERIFIED): the item fields and the kick path."""
    case = "DP11e-h"
    before = settled(kiss, "apps")
    client = app(kiss, "apps")
    try:
        time.sleep(2.0)
        item, resp = find_session(kiss, "apps")
        note(target, case, "RECORD", "GET /clients HTTP %d; kiss_client item: %s" % (resp.status, sorted(item) if item else None))
        if item is None:
            pytest.skip("no `kiss_client` item for listener apps in GET /api/v1/admin/clients (HTTP %d): the session list shape is unverified" % resp.status)
        circuit = item.get("circuit") or item.get("id")
        kind = item.get("protocol") or item.get("edge") or "ax25"
        if not circuit:
            pytest.skip("the kiss_client item has no circuit field to kick: %s" % sorted(item))
        admin = kiss.admin_session("operator")
        admin.step_up()
        kick = admin.post(admin.path("/edges/%s/clients/%s/kick" % (kind, circuit)), {}, expect=None)
        note(target, case, "RECORD", "kick of %s/%s answered HTTP %d" % (kind, circuit, kick.status))
        assert kick.status in (200, 202, 204), "kick answered HTTP %d" % kick.status
        assert client.wait_closed(10.0), "the kicked client is still connected"
    finally:
        client.close()
    again = app(kiss, "apps")                                   # no timed ban: the same peer connects again
    try:
        time.sleep(1.0)
        assert again.is_open, "a kicked peer could not reconnect at once (kick must not ban)"
    finally:
        again.close()
    end = time.monotonic() + COUNTER_WAIT_S
    while time.monotonic() < end and counters(kiss, "apps")["clients"].get("kicked", 0) <= before["clients"].get("kicked", 0):
        time.sleep(3.0)
    assert counters(kiss, "apps")["clients"].get("kicked", 0) > before["clients"].get("kicked", 0), "dv_edge_kiss_clients_total{result=kicked} did not move"


def test_dp11e_h_the_sut_log_has_no_peer_address_and_no_frame_content(rig, target, kiss):
    """config.md: the edge logs bind address, mapped ports, TX state and allowed sources at start, never a peer address or packet contents. Lines
    of the SUT log that mention kiss are scanned for the tester's address and for the information fields this module sent."""
    logs = sorted((Path(target.out) / "logs").glob("*sut*")) if (Path(target.out) / "logs").is_dir() else []
    if not logs:
        pytest.skip("no SUT log under %s/logs" % target.out)
    allow = (kiss.shape.listeners.get("apps") or next(iter(kiss.shape.listeners.values()))).allow_from
    tester_ip = (allow[0].split("/")[0] if allow else "") or ""
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
    kiss_lines = [l for l in text.splitlines() if "kiss" in l.lower()]
    note(target, "DP11e-h", "RECORD", "%d kiss log line(s) in %d file(s); first: %s" % (len(kiss_lines), len(logs), kiss_lines[:2]))
    bad = [l for l in kiss_lines if (tester_ip and tester_ip in l) or "dp11e-" in l or "ATE0" in l]
    assert not bad, "the SUT log names a peer address or frame content on a kiss line: %s" % bad[:3]
    if not kiss_lines:
        pytest.skip("the SUT log has no kiss line at all (log level?): nothing to scan")


# ================================================================ DP11e-i: slow client

@pytest.mark.slow
def test_dp11e_i_a_slow_client_loses_the_newest_frames_and_a_stalled_one_is_closed(rig, target, kiss):
    """data.md 10.10: an output queue is at most 64 frames / 64 KiB and drops the newest (`dir=out,result=slow`); a 30 s write stall closes the client
    (`stalled`). The client stops reading while about 4 MB of frames are injected at the modem, so the socket buffers fill and the queue overflows."""
    case = "DP11e-i"
    before = settled(kiss, "apps")
    client = app(kiss, "apps")
    healthy = app(kiss, "apps")
    try:
        time.sleep(1.0)
        client.pause_reading()
        pad = "x" * 800
        t0 = time.monotonic()
        for n in range(5000):
            frame = kiss_tnc.ui_frame("N0TEST-3", "APDVT1", "=1030.00N/02015.00E>dp11e-slow-%05d %s" % (n, pad))
            if not kiss.tnc.a.inject(frame):
                break
            if n % 200 == 0:
                time.sleep(0.05)
        time.sleep(40.0)                                          # past the 30 s write stall
        delta = wait_frames_delta(kiss, "apps", before, {("out", "slow"): 1})
        cdelta = dp.delta(counters(kiss, "apps")["clients"], before["clients"])
        note(target, case, "RECORD", "after %.0f s: dv_edge_kiss_frames_total deltas %s, clients %s; healthy client got %d frames" % (
            time.monotonic() - t0, delta, cdelta, len(healthy.frames(t0))))
        assert delta.get(("out", "slow"), 0) >= 1, "no frame was dropped as slow: %s" % delta
        assert cdelta.get("stalled", 0) >= 1, "the stalled client was not closed (stalled): %s" % cdelta
        client.resume_reading()
        assert client.wait_closed(15.0), "the stalled client is still connected after it resumed reading"
        assert healthy.is_open and len(healthy.frames(t0)) > 0, "a healthy client was disturbed by the slow one"
    finally:
        client.close()
        healthy.close()


# ================================================================ DP11e-j: restart

def test_dp11e_j_an_edge_restart_closes_clients_and_the_first_frame_meets_the_guard(rig, target, kiss):
    """config.md: every kiss_server key is Restart(child). The AX.25 child is restarted through the admin API (as DP15-6g): the clients are closed,
    the listener rebinds, a frame sent at once is held back by the RFSAFE1 guard (counted rf_fenced, never transmitted inside the 13 s), and after
    the guard a frame is served."""
    case = "DP11e-j"
    guard(kiss, case)
    admin = kiss.admin_session("admin")
    status = kiss.admin.get(kiss.admin.path("/status"), expect=None)
    names, everything = txguard.ax25_child_names(status.json() if status.status == 200 else {})
    if names != [txguard.AX25_CHILD]:
        pytest.skip("the launcher child %r is not listed; children seen: %s" % (txguard.AX25_CHILD, everything))
    before_drops = sum(int(s.value) for s in samples(kiss) if s.name == "dv_edge_drops_total" and s.labels.get("reason") == "rf_fenced")
    client = app(kiss, "apps")
    try:
        admin.step_up()
        resp = admin.post(admin.path("/children/%s/restart" % txguard.AX25_CHILD), {}, expect=None)
        assert resp.status == 200, "restart answered HTTP %d" % resp.status
        t_restart = time.monotonic()
        assert client.wait_closed(15.0), "the restart did not close the KISS client"
        again = None
        end = time.monotonic() + 30.0
        lst = listener(kiss, "apps")
        while again is None and time.monotonic() < end:
            probe = kiss_app.AppKissClient(lst.host, lst.port, timeout=2.0)
            if probe.connect():
                time.sleep(0.5)
                if probe.is_open:
                    again = probe
                    break
                probe.close()
            time.sleep(0.5)
        assert again is not None and again.is_open, "the listener did not rebind within 30 s of the restart"
        note(target, case, "RECORD", "listener back %.1f s after the restart call" % (time.monotonic() - t_restart))
        t1 = time.monotonic()
        held = dp.good_frame(dp.info_for("fenced"))
        assert again.send_frame(held)
        time.sleep(5.0)
        inside = [m for _t, m in modem_frames(kiss, t1) if "dp11e-fenced" in m]
        assert inside == [], "a client frame was transmitted %.1f s after the restart, inside the RFSAFE1 guard: %s" % (time.monotonic() - t_restart, inside)
        result = txguard.wait_unfenced(txguard.ax25_reader(kiss.admin, "rf-a"), kind="ax25")
        note(target, case, "RECORD", txguard.describe(result, "ax25", "rf-a"))
        t2 = time.monotonic()
        served = dp.good_frame(dp.info_for("after_guard"))
        assert again.send_frame(served)
        got = []
        end = time.monotonic() + MODEM_WAIT_S
        while not got and time.monotonic() < end:
            got = [m for _t, m in modem_frames(kiss, t2) if "dp11e-after_guard" in m]
            time.sleep(0.2)
        assert got == [ax25.to_tnc2(served)], "the frame after the guard was not served: %s" % got
        end = time.monotonic() + COUNTER_WAIT_S
        drops = before_drops
        while time.monotonic() < end:
            drops = sum(int(s.value) for s in samples(kiss) if s.name == "dv_edge_drops_total" and s.labels.get("reason") == "rf_fenced")
            if drops > before_drops:
                break
            time.sleep(3.0)
        note(target, case, "RECORD", "dv_edge_drops_total{reason=rf_fenced} %d -> %d" % (before_drops, drops))
        assert drops > before_drops, "the frame held by the guard was not counted rf_fenced"
        again.close()
    finally:
        client.close()


# ================================================================ shape B: loopback, rate, simulate

def test_dp11e_k_a_loopback_listener_is_not_reachable_from_another_host(rig, target, kiss):
    """config.md: a loopback `listen` accepts loopback peers only; seen from the tester (another network namespace) there is no listener on the
    rig address of the SUT: the connection is refused. The default for a missing bind is loopback; `listen` itself has no default."""
    lst = listener(kiss, "loop")
    own = next((l.host for l in kiss.shape.listeners.values() if not l.loopback), None)
    if own is None:
        pytest.skip("no non-loopback listener to learn the SUT's rig address from")
    client = kiss_app.AppKissClient(own, lst.port, timeout=3.0)
    ok = client.connect()
    note(target, "DP11e-k", "RECORD", "connect to %s:%d (the loopback listener's port on the rig address): %s %s" % (own, lst.port, ok, client.error))
    client.close()
    assert not ok, "the loopback listener %s:%d accepted a connection on the rig address %s" % (lst.host, lst.port, own)


def test_dp11e_l_the_listener_bucket_counts_rate(rig, target, kiss):
    """data.md 10.10 step 7: the listener bucket (`rate` listener: tx_per_min 2, tx_burst 1) admits the burst and refuses the rest as `rate`."""
    case = "DP11e-l"
    listener(kiss, "rate")
    guard(kiss, case)
    before = settled(kiss, "rate")
    client = app(kiss, "rate")
    t0 = time.monotonic()
    try:
        for n in range(4):
            assert client.send_frame(dp.good_frame(dp.info_for("rate%d" % n)))
        time.sleep(MODEM_WAIT_S / 2)
    finally:
        client.close()
    delta = wait_frames_delta(kiss, "rate", before, {("in", "queued"): 1, ("in", "rate"): 2})
    at_modem = [m for _t, m in modem_frames(kiss, t0) if "dp11e-rate" in m]
    note(target, case, "RECORD", "deltas %s; frames at the modem %s" % (delta, at_modem))
    queued, rate = delta.get(("in", "queued"), 0), delta.get(("in", "rate"), 0)
    assert queued + rate == 4, "4 frames sent, queued %d + rate %d: %s" % (queued, rate, delta)
    assert rate >= 2 and 1 <= queued <= 2, "burst 1 / 2 per minute: queued %d rate %d" % (queued, rate)
    assert len(at_modem) == queued, "%d frames reached the modem, %d were queued" % (len(at_modem), queued)


def test_dp11e_m_a_simulated_port_counts_simulated_and_writes_no_modem_byte(rig, target, kiss):
    """data.md 10.10: a client frame to a port with tx = "simulate" passes the same gates and counts dv_edge_rf_tx_total{result=simulated} without a
    modem byte or PTT; a frame to the real port rf-a is transmitted (sent)."""
    case = "DP11e-m"
    listener(kiss, "sim")
    guard(kiss, case)
    s0 = samples(kiss)
    sim0 = sum(int(x.value) for x in s0 if x.name == "dv_edge_rf_tx_total" and x.labels.get("port") == "rf-b" and x.labels.get("result") == "simulated")
    sent0 = sum(int(x.value) for x in s0 if x.name == "dv_edge_rf_tx_total" and x.labels.get("port") == "rf-a" and x.labels.get("result") == "sent")
    before = settled(kiss, "sim")
    b_received = len(kiss.tnc.b.received)
    client = app(kiss, "sim")
    t0 = time.monotonic()
    try:
        real, simulated = dp.good_frame(dp.info_for("sim_real")), dp.good_frame(dp.info_for("sim_virtual"))
        assert client.send_frame(real, channel=0) and client.send_frame(simulated, channel=1)
        got = []
        end = time.monotonic() + MODEM_WAIT_S
        while not got and time.monotonic() < end:
            got = [m for _t, m in modem_frames(kiss, t0) if "dp11e-sim_real" in m]
            time.sleep(0.2)
        assert got == [ax25.to_tnc2(real)], "the frame on the real port was not transmitted: %s" % got
        time.sleep(15.0)
    finally:
        client.close()
    assert [m for _t, m in modem_frames(kiss, t0, "b")] == [], "a simulated release wrote a data frame to the modem rf-b"
    assert len(kiss.tnc.b.received) == b_received, "the simulated port's modem received KISS bytes"
    delta = wait_frames_delta(kiss, "sim", before, {("in", "queued"): 2})
    end = time.monotonic() + COUNTER_WAIT_S
    while True:
        s1 = samples(kiss)
        sim1 = sum(int(x.value) for x in s1 if x.name == "dv_edge_rf_tx_total" and x.labels.get("port") == "rf-b" and x.labels.get("result") == "simulated")
        sent1 = sum(int(x.value) for x in s1 if x.name == "dv_edge_rf_tx_total" and x.labels.get("port") == "rf-a" and x.labels.get("result") == "sent")
        if (sim1 > sim0 and sent1 > sent0) or time.monotonic() >= end:
            break
        time.sleep(3.0)
    note(target, case, "RECORD", "kiss deltas %s; rf-b simulated %d -> %d; rf-a sent %d -> %d" % (delta, sim0, sim1, sent0, sent1))
    assert delta.get(("in", "queued"), 0) == 2, "queued moved by %s, expected 2 (%s)" % (delta.get(("in", "queued"), 0), delta)
    assert sim1 - sim0 == 1 and sent1 - sent0 == 1, "simulated +%d (expected 1), sent +%d (expected 1)" % (sim1 - sim0, sent1 - sent0)


# ================================================================ the last case

def test_dp11e_z_no_admission_is_counted_disabled(rig, target, kiss):
    """tx.rs `enqueue_client`: `disabled` is the admission result for a frame whose port has no scheduler budget (no class TTL), which a valid
    configuration (tx true or simulate needs an explicit budget) cannot produce. After every case of the run the counter must still be 0.
    LIMIT: the refusal itself is not reachable black-box; this case only checks it never appears."""
    s = samples(kiss)
    disabled = [(x.labels.get("port"), int(x.value)) for x in s if x.name == "dv_edge_rf_tx_total" and x.labels.get("result") == "disabled" and int(x.value)]
    note(target, "DP11e-z", "RECORD", "dv_edge_rf_tx_total{result=disabled} non-zero series: %s" % disabled)
    assert not disabled, "client or node frames were counted disabled: %s" % disabled
