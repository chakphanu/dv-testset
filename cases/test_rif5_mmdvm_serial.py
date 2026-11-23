# SPDX-License-Identifier: MIT
"""DV-Router RIF5: the `mmdvm-serial` radio adapter against a FAKE MMDVM modem played by the tester on a UDP byte port (DV-Router c017e491:
RIF5 S2 fd58a4e the adapter and the D-STAR framer, S3 43483ca DMR simplex, S4 c017e491 YSF). Case table, config shape, run command, time and
GAPs: docs/rif5-cases.md. Pure judgements: dvtest/rif5.py, the modem and the envelope: dvtest/mmdvm_serial.py, rig helpers: cases/rif5_support.py.

  RIF5-G1   (c) nothing that keys the modem is written during the startup guard (restart of dv-edge-radio through the admin API, an over in the
            guard, voice / EOT / CW ID / DMR control frames none, also not later)
  RIF5-O1   the open dialogue: GET_VERSION, SET_FREQ, SET_CONFIG (v2, 40 B), GET_STATUS, the bytes at the documented offsets
  RIF5-O2   GET_STATUS every 250 ms
  RIF5-O3   the port row in the public state and the admin status (state up, modes, firmware, modem)
  RIF5-TX   (a) room to modem, D-STAR / DMR / YSF: a protocol client keys the room, the modem gets the MMDVM frames of the mode
  RIF5-RX   (a) modem to room, D-STAR / DMR / YSF: the modem sends an over, a protocol client of the room hears it; nothing is sent back to RF
  RIF5-RXD  a damaged DMR header copy inside an open over refreshes the 300 ms native-over timeout (S4: "a damaged DMR frame refreshes the over timeout")
  RIF5-RXR  D-STAR RF headers the adapter refuses (RPT2 not station + G, RPT1 not station + module, bad CRC): not in the room, counters
  RIF5-LONG a 3 s over in each mode, record only (the 250 ms status poll limits what the host keeps in flight)
  RIF5-D3   a modem silent for 1.0 s (below the 2 s of the document) keeps the port Up
  RIF5-D2   (b) a short status does not refresh inactivity: Down after 2 s although bytes flow
  RIF5-D1   (b) a silent modem: the port goes Down after 2000 ms without a valid status; polls, backoff, six version tries, recovery
  RIF5-M1   the counters of the run: overs, modem results, port results

  RADIOSEQ1-*  the image wip-37b7960 (DV-Router 917b9e3b, DVT_RADIOSEQ1=on; docs/radioseq1-cases.md): the radio hub numbers received DMR / YSF / NXDN / P25
            voice by frame ordinal: DMR letters A..F, no gap, no lossy over, YSF FN and NXDN SACCH steps; without the switch the cases only record
            RADIOSEQ1-C: two mmdvm-serial ports on the same DMR room (rig/config/dv-router/rif5-two-ports.toml, a second fake board): an RF over of board A is
            transmitted by port B and the reverse, bursts A..F in order, every word, no echo to the origin board
  RIF5N-*   the image dv-router:wip-f1f3777 (DV-Router bc9af2b1, DVT_RIF5N=on; docs/rif5-cases.md "The image wip-f1f3777"): NXDN (RIF5d) and P25 (RIF5e)
            TX / RX / long overs / foreign RAN and NAC / NXDN <-> DMR, the RIF5k end sequence and the RIF5l voiceless join in the older cases, the
            D-STAR ride-along (RIF5h) with its negative control, the RFHEARD1 guard, and PORTCTL1 (DVT_PORTCTL1=on)

Gate: DVT_RIF5=on (rig/compose.yml passes it to the tester); every case skips with the reason otherwise. Needs DVT_SUT_PROFILE=dvrouter, native launch
and rig/config/dv-router/rif5-mmdvm-udp.toml in DVT_SUT_CONFIG_EXTRA. With --profile web (web.toml:admin.toml) the cases read the port state and G1 can
restart the edge; the rig control channel gives the metrics. A difference the documents do not settle is a FINDING line in out/<run>/rif5.txt, never an
assertion. Written offline against DV-Router c017e491, first rig run on dv-router:wip-7b0fca8 (docs/rif5-cases.md, "First rig run"); the unit tests cover the
encoders, the modem and the judges.
"""
import os
import time
import tomllib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from dvtest import clients, echo, medload1, metrics, radioseq1, rif5, rif5n, sut_config
from dvtest import mmdvm_rf as rf
from dvtest import mmdvm_fw
from dvtest import mmdvm_modem as mm
from dvtest import mmdvm_serial as ms
from dvtest.control import RigControl
from dvtest.proto import dmr, nxdn
from rif5_support import (Member, RoomMember, admin_row, last_dialogue, note, public_row, quiesce, restart_radio_edge, settle_idle, wait_for,
                          wait_unfenced, write_timeline)

pytestmark = pytest.mark.rig

ROOT = Path(__file__).resolve().parents[1]
SNIPPET = ROOT / "rig" / "config" / "dv-router" / "rif5-mmdvm-udp.toml"
KINDS = ("dstar", "dmr", "ysf")
# What the flow control of radio.md 7.1 carries without a drop. D-STAR: 22 frames (0.44 s) cover frame 0 of two superframes; the first rig run (50
# frames, 1.0 s) lost words from word 23 on: the first status that refills the window comes up to 250 ms after PTT, the header costs 4 of the 10 frames
# in flight, and the host queue (16, oldest dropped) overflows at the 23rd word of a real-time over. One DMR superframe, three YSF frames.
TX_FRAMES = {"dstar": 22, "dmr": 18, "ysf": 15}
RX_SECONDS = 3.0
RXD_COPIES = 10                                      # damaged header copies of RIF5-RXD
LONG_FRAMES = 150                                    # 3 s
OPEN_WAIT_S = 150.0                                  # the edge retries a failed open with a backoff of up to 60 s
TALLY = Counter()                                    # what the cases did, for RIF5-M1


# ---------------------------------------------------------------- the rig and the modem

def read_config(target):
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_port(target):
    """The port row of the snippet with the rig's addresses, for a rendered config the tester cannot see."""
    net = ".".join(target.tester.split(".")[:3])
    text = sut_config.render("", net, own=int(target.xlxd_sut.split(".")[-1]), tester=int(target.tester.split(".")[-1]),
                             extras=[SNIPPET.read_text(encoding="utf-8")])
    return rif5.port_from_config(tomllib.loads(text))


@pytest.fixture(scope="module")
def ctx(rig, target):
    if not rif5.gate_on(os.environ):
        pytest.skip("DVT_RIF5 is not on: the RIF5 cases need DV-Router c017e491 or later and rig/config/dv-router/rif5-mmdvm-udp.toml (docs/rif5-cases.md)")
    if target.sut_profile != "dvrouter":
        pytest.skip("the mmdvm-serial adapter is DV-Router's: DVT_SUT_PROFILE=dvrouter")
    if os.environ.get("DVT_SUT_LAUNCH") != "native":
        pytest.skip("the radio edge is configured in the TOML: DVT_SUT_LAUNCH=native with rig/config/dv-router/rif5-mmdvm-udp.toml")
    doc = read_config(target)
    port, port_b = radioseq1.port_pair(doc)               # port_b: the second row of rif5-two-ports.toml (RADIOSEQ1-C), None without it
    seen = bool(doc)
    if port is None:
        if seen:
            pytest.skip("the rendered config has no enabled [[edges.radio.port]] kind mmdvm-serial with a udp address: add "
                        "rig/config/dv-router/rif5-mmdvm-udp.toml to DVT_SUT_CONFIG_EXTRA")
        port = default_port(target)
    try:
        transport = ms.PinnedUdp(port.peer, port.bind)
    except OSError as exc:
        pytest.fail("cannot bind the modem side of the UDP byte port %s:%d: %s" % (port.peer[0], port.peer[1], exc))
    if rif5.fw_on(os.environ):
        profile, delay = rif5.fw_settings(os.environ)
        make_modem = lambda tp: mmdvm_fw.FwRif5Modem(tp, profile=profile, tx_delay_ms=delay)
        note(target, "setup", "RECORD", "DVT_RIF5FW=on: the fake modem is firmware-like (profile %s, TX delay %s, dvtest/mmdvm_fw.py)" % (
            profile, "from SET_CONFIG" if delay is None else "%d ms" % delay))
    else:
        make_modem = ms.Rif5Modem
    modem = make_modem(transport).start()
    # the second fake board of RADIOSEQ1-C, started with the first so that both ports open together; the cases that do not need it never look at it
    modem_b, b_error = None, None
    if port_b is not None:
        try:
            modem_b = make_modem(ms.PinnedUdp(port_b.peer, port_b.bind)).start()
        except OSError as exc:
            b_error = "cannot bind the modem side of the UDP byte port %s:%d of %s: %s" % (port_b.peer[0], port_b.peer[1], port_b.name, exc)
        note(target, "setup", "RECORD", "second port %s: %s" % (port_b.name, b_error or "fake modem on %s:%d (RADIOSEQ1-C)" % port_b.peer))
    control = RigControl(Path(target.out) / "control") if os.environ.get("DVT_RIG_CONTROL") == "1" else None
    if not seen:
        note(target, "setup", "RECORD", "rendered config not visible in out/: the snippet's own values are assumed (%s)" % (port.modes,))
    try:
        if not modem.configured.wait(OPEN_WAIT_S):
            pytest.fail("no SET_CONFIG from the SUT's radio edge within %.0f s on %s:%d (frames seen: %d, datagrams from other addresses: %d): is "
                        "dv-edge-radio running (rif5-mmdvm-udp.toml in DVT_SUT_CONFIG_EXTRA, tx_ack exact), and does it send from %s:%d?" % (
                            OPEN_WAIT_S, port.peer[0], port.peer[1], len(modem.host_frames()), transport.foreign, port.bind[0], port.bind[1]))
        time.sleep(1.0)
        yield SimpleNamespace(target=target, port=port, modem=modem, ip=target.xlxd_sut, control=control, baseline=None, baseline_t=None, config_seen=seen,
                              port_b=port_b, modem_b=modem_b, b_error=b_error)
    finally:
        modem.stop()
        if modem_b is not None:
            modem_b.stop()


@pytest.fixture(scope="module")
def web(request):
    """An anonymous client of the public API (profile web), or None."""
    if os.environ.get("DVT_WEB") != "on":
        return None
    try:
        return request.getfixturevalue("web_client")()
    except pytest.skip.Exception:
        return None


@pytest.fixture(scope="module")
def admin_factory(request):
    """admin_session of cases/web_fixtures.py, or None without the web profile."""
    if os.environ.get("DVT_WEB") != "on":
        return None
    try:
        return request.getfixturevalue("admin_session")
    except pytest.skip.Exception:
        return None


def admin_for(factory, role):
    if factory is None:
        return None
    try:
        return factory(role)
    except pytest.skip.Exception:
        return None


def hub_text(ctx):
    if ctx.control is None:
        return None
    try:
        return ctx.control.metrics("xlxd-sut")
    except (RuntimeError, TimeoutError, OSError, ValueError):
        return None


def counters_now(ctx):
    return rif5.counters(hub_text(ctx), ctx.port.name)


def ready(ctx, web, case):
    """The port is Up (the host polls the modem), the RF gate is open (no fence, public state) and the modem is back in idle."""
    modem = ctx.modem
    polled = modem.wait_frame(lambda f: f.ftype == ms.GET_STATUS, since=time.monotonic() - 0.6, timeout=40.0)
    assert polled is not None, "%s: the host does not poll the modem (the port is not Up)" % case
    if web is not None:
        res = wait_unfenced(web, ctx.port.name)
        assert res.state in ("unfenced", "clear", "absent"), "%s: the TX fence did not end: %s" % (case, res)
        row = wait_for(lambda: (lambda r: r if r and r.get("state") == "up" else None)(public_row(web, ctx.port.name)), 30.0, 0.5)
        assert row is not None, "%s: the public state does not show the port up" % case
    else:
        d = last_dialogue(modem)
        left = d.config.mono + rif5.GUARD_S + 6.0 - time.monotonic() if d else 0
        if left > 0:
            time.sleep(left)
    if not settle_idle(modem, 30.0):
        note(ctx.target, case, "FINDING", "the host did not give the modem back to idle (SET_MODE 0) within 30 s of its last mode claim")
    if ctx.baseline is None and ctx.control is not None:
        ctx.baseline = counters_now(ctx)
        ctx.baseline_t = modem.mark()


def report(ctx, case, verdict):
    for line in verdict.findings:
        note(ctx.target, case, "FINDING", line)
    note(ctx.target, case, "RECORD", ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items())) or "no facts")


# ---------------------------------------------------------------- RIF5-G1: the startup guard

def test_g1_nothing_is_written_to_the_modem_during_the_startup_guard(ctx, web, admin_factory):
    """radio.md 7.1 "No frame transmits voice during open", 7.3 and 10.4 rule 4: after a start the radio edge holds TX for G = 13 s; in that time the
    modem gets GET_VERSION, SET_FREQ, SET_CONFIG and GET_STATUS and nothing that keys it. A room over inside the guard never reaches RF, also not
    after the guard (DV-Router's own test: crates/router/tests/real_children/radio/mmdvm.rs). The guard is re-armed by restarting dv-edge-radio."""
    case = "RIF5-G1"
    admin = admin_for(admin_factory, "admin")
    if admin is None or web is None:
        pytest.skip("the startup guard exists only after a start: needs --profile web with admin.toml to restart dv-edge-radio "
                    "(POST /api/v1/admin/children/dv-edge-radio/restart); GAP in docs/rif5-cases.md")
    ready(ctx, web, case)
    modem = ctx.modem
    t_restart = modem.mark()
    status = restart_radio_edge(admin)
    assert status == 200, "POST children/%s/restart answered HTTP %d, want 200" % (rif5.CHILD, status)
    ctx.baseline, ctx.baseline_t = None, None                     # a new process: its counters start at zero
    cfg = modem.wait_configured_after(t_restart, 90.0)
    assert cfg is not None, "the restarted edge did not open the modem again within 90 s"
    # the fence as the public state shows it, as early as it can be read
    fenced = wait_for(lambda: (lambda r: r if r and r.get("tx_state") == "fenced" else None)(public_row(web, ctx.port.name)), 20.0, 0.25)
    inside = fenced is not None
    note(ctx.target, case, "RECORD", "after the restart: opened %.1f s later; fence row %s" % (
        cfg.mono - t_restart, {k: fenced.get(k) for k in ("state", "tx_state", "fence", "guard_ms", "phys_busy")} if inside else "not seen fenced"))
    if inside:
        assert fenced.get("fence") == "startup_guard", "fence %r right after a start, the document says startup_guard (radio.md 10.4 rule 4)" % fenced.get("fence")
        guard = fenced.get("guard_ms")
        assert guard is None or (isinstance(guard, int) and not isinstance(guard, bool) and guard >= 0), "guard_ms %r" % (guard,)
    # a short D-STAR over from the room inside the guard
    frames = echo.make_frames("dstar", 30, "rif5-guard")
    talker = Member(ctx.ip, "dstar", rif5.ROOMS["dstar"], 1, listen=False)
    t_over = time.monotonic()
    try:
        talker.talk(frames, tail=0.5)
    finally:
        talker.close(settle=0.5)
    row_after = public_row(web, ctx.port.name) or {}
    over_inside = row_after.get("tx_state") == "fenced"
    res = wait_unfenced(web, ctx.port.name)
    t_end = time.monotonic()
    note(ctx.target, case, "RECORD", "over sent %.1f s after the restart (fence still up after it: %s); guard ended %.1f s after the restart (%s)" % (
        t_over - t_restart, over_inside, t_end - t_restart, res.state))
    time.sleep(4.0)                                               # the guarded over must not appear later either
    verdict = rif5.judge_guard(modem.host_frames(), t_restart, time.monotonic())
    report(ctx, case, verdict)
    assert not verdict.problems, "RF-producing frames inside the guard window: %s" % verdict.problems
    if not over_inside:
        note(ctx.target, case, "FINDING", "the fence was already over when the over ended: the over was not proven to lie inside the guard (restart-to-fence %.1f s)" % (
            t_over - t_restart))
    assert res.state in ("unfenced", "clear"), "the guard did not end: %s" % (res,)
    # 13 s after the first successful probe: the end of the fence is not earlier than about G after the start of the new process
    if inside and res.state == "unfenced":
        assert t_end - t_restart >= rif5.GUARD_S - 4.0, "the fence ended %.1f s after the restart, the document says G = 13 s" % (t_end - t_restart)


# ---------------------------------------------------------------- RIF5-O: the dialogue, the polls, the port row

def test_o1_open_dialogue(ctx):
    """radio.md 7.1: GET_VERSION, SET_FREQ (E0 11 04 00 + RX, TX, RF level, POCSAG Hz), SET_CONFIG (version 2 modem: 40 B, simplex flag, enables,
    levels, colour code, delay, oscillator 0x80), GET_STATUS; no keying byte in between."""
    case = "RIF5-O1"
    d = last_dialogue(ctx.modem)
    assert d is not None, "no complete open dialogue in the modem log"
    verdict = rif5.judge_open(d, ctx.port)
    report(ctx, case, verdict)
    assert not verdict.problems, "open dialogue: %s" % verdict.problems
    refused = [n for n in ctx.modem.naks if d.versions[0].mono <= n[0] <= d.config.mono + 1.0]
    assert not refused, "the modem had to NAK a frame of the dialogue: %s" % refused


def test_o2_status_poll_every_250_ms(ctx):
    """radio.md 7.1 "Polling": GET_STATUS every 250 ms."""
    d = last_dialogue(ctx.modem)
    assert d is not None
    since = d.config.mono
    wait_for(lambda: len([f for f in ctx.modem.host_frames(since, types=(ms.GET_STATUS,))]) >= 14, 15.0)
    now = time.monotonic()
    verdict = rif5.status_cadence(ctx.modem.host_frames(), since, now if now - since < 8 else since + 8)
    report(ctx, "RIF5-O2", verdict)
    assert not verdict.problems, "status polling: %s" % verdict.problems


def test_o3_port_row_in_the_status(ctx, web, admin_factory):
    """status.md "Radio port telemetry", radio.md 12.3: the public row (observer fields) and the admin row (firmware, modem) of an Up MMDVM port."""
    case = "RIF5-O3"
    if web is None:
        pytest.skip("needs --profile web (public state)")
    ready(ctx, web, case)
    row = public_row(web, ctx.port.name)
    assert row is not None, "the port %s is not in procs[].radio.ports[] of the public state" % ctx.port.name
    note(ctx.target, case, "RECORD", "public row: %s" % row)
    want = {"kind": "mmdvm-serial", "pattern": "mmdvm-host", "duplex": False, "state": "up", "band": "70cm", "rx_freq_hz": ctx.port.rx_freq,
            "tx_freq_hz": ctx.port.tx_freq}
    problems = []
    wrong = {k: (row.get(k), v) for k, v in want.items() if row.get(k) != v}
    if wrong:
        # radio.md 12.3 (RadioPortInfo, scope obs) and status.md "Radio port telemetry" list rx_freq_hz and tx_freq_hz among the observer fields
        problems.append("public row differs (got, wanted): %s; observer fields of the document missing from the row: %s" % (
            wrong, sorted(f for f in rif5.OBSERVER_FIELDS if f not in row)))
    if rif5.j_on(os.environ) and (set(row) != set(rif5.OBSERVER_FIELDS)):
        # RIF5j (design-276 2): the public port row has exactly the 19 observer fields, the two frequencies among them
        problems.append("public row keys differ from the 19 observer fields: missing %s, extra %s" % (
            sorted(set(rif5.OBSERVER_FIELDS) - set(row)), sorted(set(row) - set(rif5.OBSERVER_FIELDS))))
    if sorted(row.get("modes") or []) != sorted(ctx.port.modes):
        problems.append("modes %r" % (row.get("modes"),))
    if row.get("tx") not in ("on", True):
        problems.append("tx %r for a port with tx = true" % (row.get("tx"),))
    for hidden in ("firmware", "modem", "down_reason", "physical"):
        if hidden in row:
            note(ctx.target, case, "FINDING", "the public (observer) row carries the admin-only field %r" % hidden)
    # the admin part runs whatever the public row said, so that one run reports both
    operator = admin_for(admin_factory, "operator")
    if operator is None:
        note(ctx.target, case, "RECORD", "no operator session: the admin fields were not read")
    else:
        admin = admin_row(operator, ctx.port.name)
        if admin is None:
            problems.append("the port is not in radio_edges[].radio.ports[] of GET /api/v1/admin/clients")
        else:
            note(ctx.target, case, "RECORD", "admin row: %s" % admin)
            if admin.get("firmware") != ctx.modem.text:
                problems.append("firmware %r, the modem said %r" % (admin.get("firmware"), ctx.modem.text))
            if (admin.get("modem") or {}).get("protocol") != 2:
                problems.append("modem.protocol %r, the modem speaks version 2" % ((admin.get("modem") or {}).get("protocol"),))
            if admin.get("down_reason") is not None or admin.get("physical") != ctx.port.physical:
                problems.append("down_reason %r, physical %r (wanted None and %r)" % (admin.get("down_reason"), admin.get("physical"), ctx.port.physical))
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-TX: room to modem

def rx_kwargs(kind, port):
    """What the modem's RF over carries: the identity of the radio, and the repeater / colour code fields the adapter checks."""
    if kind == "dstar":
        return {"my": rif5.STATION, "rpt1": rif5.STATION.ljust(7) + port.rpt_module, "rpt2": rif5.STATION.ljust(7) + "G"}
    if kind == "dmr":
        return {"src": 1023007, "dst": port.dmr_tg, "colour_code": port.color_code}
    return {"src": rif5.STATION}


def judge_tx(kind, log, sent, ident, port, lossy=False):
    """The judge of the mode. `lossy`: a long over, where the host queue may drop words (a FINDING, radio.md 7.1 flow control). Under DVT_RIF5N=on
    (RIF5k, radio.md 7.1 "Built (RIF5k P3)") the end sequence has no extra frame: the trailing end unit of D-STAR and the extra YSF frame, a FINDING
    on the older images, are problems."""
    if kind == "dstar":
        verdict = rif5.judge_tx_dstar(log, sent, ident, rif5.STATION, port.rpt_module, lossy=lossy)
    elif kind == "dmr":
        verdict = rif5.judge_tx_dmr(log, sent, ident, port.dmr_tg, port.color_code, lossy=lossy)
    else:
        verdict = rif5.judge_tx_ysf(log, sent, ident, port.ysf_lo_dev, lossy=lossy)
    if rif5.n_on(os.environ) and not lossy:
        verdict.problems.extend("RIF5k: %s" % p for p in rif5n.end_sequence_problems(kind, verdict, len(sent)))
    return verdict


def quiet_after_link(ctx, kind):
    """A YSF client joins a module with a header (DG-ID) and a terminator 0.3 s apart (dvtest/proto/ysf.py `link`). DV-Router relays that voiceless
    stream to the radio port, which keys the transmitter for it: SET_MODE 03, a preamble, a header, a terminator (rig run 20261113T052325Z, timelines:
    two headers and two terminators in the TX-ysf window; the first frames of an RF over refused while the join keyed the port). Wait until the join's key-up
    is over and the host gave the modem back to idle, so that the case sees only its own over. D-STAR and DMR joins do not reach RF."""
    if kind != "ysf":
        return
    time.sleep(0.8)
    if not settle_idle(ctx.modem, 30.0):
        note(ctx.target, "setup", "FINDING", "the host did not give the modem back to idle within 30 s of a YSF client's join")


def member(ctx, kind, module, index, listen):
    """rif5_support.Member, then quiet_after_link."""
    m = Member(ctx.ip, kind, module, index, listen=listen)
    quiet_after_link(ctx, kind)
    return m


def tx_over(ctx, web, kind, n, seed, case, after_link=None):
    """One over of `n` frames from the protocol client N0TEST into the mode's room; returns (log since the start, frames sent, expected words, ident)."""
    ready(ctx, web, case)
    frames = echo.make_frames(kind, n, "%s-%s" % (seed, kind))
    # a YSF client's join is a keyed over of its own (quiet_after_link waits it out); `after_link` runs between the join and the talk
    talker = member(ctx, kind, rif5.ROOMS[kind], 1, listen=False)
    if after_link is not None:
        after_link()
    since = ctx.modem.mark()
    try:
        t_talk = time.monotonic()
        talker.talk(frames, tail=0.5)
        ctx.last_talk = (t_talk, time.monotonic() - 0.5, len(frames))       # the words are spread over the talk without its 0.5 s tail (an estimate)
        ident = talker.ident()
    finally:
        talker.close(settle=0.5)
    log = quiesce(ctx.modem, since, ms.TX_TYPES[kind], quiet=1.5, timeout=30.0)
    assert log is not None, "%s: no %s frames reached the modem within 30 s of the end of the over (frames seen: %s)" % (
        case, kind, sorted({ms.name_of(f.ftype) for f in ctx.modem.host_frames(since)}))
    return log, since, echo.expected_frames(kind, frames), ident


def check_naks(ctx, case, since):
    naks = [n for n in ctx.modem.naks if n[0] >= since]
    full = [n for n in naks if n[2] == ms.NAK_FULL]
    other = [n for n in naks if n[2] != ms.NAK_FULL]
    if full:
        note(ctx.target, case, "FINDING", "the host wrote %d frame(s) the fake modem's buffer model had no room for (NAK 5 %s); a fill model, not a firmware" % (
            len(full), [ms.name_of(n[1]) for n in full][:4]))
    assert not other, "%s: the modem had to refuse frames of the host: %s (NAK 2 = mode or enable, 4 = value, 1 = unknown type)" % (
        case, [(ms.name_of(n[1]), n[2]) for n in other])


def timeline(ctx, case, since, verdict, talk=True):
    """Write out/<run>/rif5/timeline-<case>.jsonl for the flow-control analysis (docs/rif5-cases.md, "The timeline") and name it in the notes."""
    sent = getattr(ctx, "last_talk", None) if talk else None
    summary = {"case": case, "facts": verdict.facts, "problems": verdict.problems, "findings": verdict.findings,
               "frames_total": "not read in the case: hub counters are in control/8-N.out (cumulative, 10 s old at most)"}
    path = write_timeline(ctx.target, case, ctx.modem, since, sent, summary)
    note(ctx.target, case, "RECORD", "timeline: %s" % (path if path is None else "rif5/" + path.name))
    if hasattr(ctx.modem, "fw_events"):
        # the firmware-like fake (DVT_RIF5FW): what it saw of the host's flow, in numbers (times in seconds since the window start)
        events = ctx.modem.fw_events(since)
        by = {}
        for t, name, detail in events:
            by.setdefault(name, []).append(round(t - since, 2))
        under = by.get("underrun", [])
        note(ctx.target, case, "RECORD", "fake: %d underrun(s) at %s, %d tx_off, %d preamble(s), %d NAK 5; the model's FIFO ran dry %s" % (
            len(under), under[:6], len(by.get("tx_off", [])), len(by.get("preamble_start", [])), len(by.get("nak", [])),
            "inside the over (the host did not keep the modem fed)" if under else "only at the end of the over"))


@pytest.mark.parametrize("kind", KINDS)
def test_tx_room_to_modem(kind, ctx, web):
    """(a) A protocol client keys the room of the mode; the modem receives SET_MODE for the mode, then the native frames: D-STAR header + data + EOT,
    DMR eight headers + A to F + three terminators, YSF header + V/D mode 2 frames + terminator, the talker's identity in the header, the voice words
    byte for byte (radio.md 7.1, 7.2, 7.3, 10.3). Sizes are what the 250 ms poll lets the host keep in flight (RIF5-LONG records the rest)."""
    case = "RIF5-TX-%s" % kind
    log, since, sent, ident = tx_over(ctx, web, kind, TX_FRAMES[kind], "rif5-tx", case)
    verdict = judge_tx(kind, log, sent, ident, ctx.port)
    report(ctx, case, verdict)
    timeline(ctx, case, since, verdict)
    TALLY["tx"] += 1
    TALLY["tx_native_frames"] += len([f for f in log if f.ftype in ms.TX_TYPES[kind]])
    check_naks(ctx, case, since)
    assert not verdict.problems, "%s: %s" % (case, verdict.problems)


# ---------------------------------------------------------------- RIF5-RX: modem to room

@pytest.mark.parametrize("kind", KINDS)
def test_rx_modem_to_room(kind, ctx, web):
    """(a) The fake modem sends an RF over (valid header / LC / FICH, valid voice words); a protocol client linked to the room of the mode hears one
    over: the radio's identity, the voice words in order, the end. The RF ingress circuit is not echoed back to RF (radio.md 4.4, 7.2)."""
    case = "RIF5-RX-%s" % kind
    ready(ctx, web, case)
    listener = member(ctx, kind, rif5.ROOMS[kind], 1, listen=True)
    try:
        since = ctx.modem.mark()
        ku = ctx.modem.keyup(kind, seconds=RX_SECONDS, seed="rif5-rx", **rx_kwargs(kind, ctx.port))
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        got = wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
        wanted = {"dstar": rif5.STATION, "dmr": 1023007, "ysf": rif5.STATION}[kind]
    finally:
        listener.close(settle=0.5)
    note(ctx.target, case, "RECORD", "overs heard: %s" % [(round(o.start - overs[0].start, 2), round(o.end - o.start, 2), o.idents, len(o.frames), o.ended) for o in overs])
    assert got and len(overs) == 1, "%s: the client heard %d overs, wanted one" % (case, len(overs))
    over = overs[0]
    assert over.idents == [wanted], "%s: the talker is %s, the modem's RF source is %s" % (case, over.idents, wanted)
    timeline(ctx, case, since, SimpleNamespace(facts={"voice_sent": len(ku.voice), "voice_heard": len(over.frames), "talker": over.idents},
                                               problems=[], findings=[]), talk=False)
    if kind == "ysf":
        # a YSF over is not bit exact through the room: ingress +1.0 dB and egress -0.4 dB step only the gain index b2 (decisions D1, RIF5 D3)
        words = rif5.compare_ysf_words(ku.voice, over.frames)
        for line in words.findings:
            note(ctx.target, case, "FINDING", line)
        note(ctx.target, case, "RECORD", "voice words sent %d, heard %d, extra %d, b2 steps %s" % (
            len(ku.voice), len(over.frames), words.facts["extra_words"], words.facts["b2_steps"]))
        assert not words.problems, "%s: %s" % (case, words.problems)
    else:
        compare = echo.compare_payload(ku.voice, over.frames)
        note(ctx.target, case, "RECORD", "voice words sent %d, heard %d, first difference %s, extra %d, missing %d" % (
            len(ku.voice), len(over.frames), compare.first_diff, len(compare.extra), compare.missing))
        if compare.extra:
            note(ctx.target, case, "FINDING", "%d voice words after the %d the modem sent (a pad or an end marker of the egress)" % (len(compare.extra), len(ku.voice)))
        assert compare.prefix_ok and not compare.missing, "%s: the words heard differ from the words the modem sent (first difference at %s, %d missing)" % (
            case, compare.first_diff, compare.missing)
    assert over.ended, "%s: the over did not end on the client side" % case
    time.sleep(1.5)
    echoed = [ms.name_of(f.ftype) for f in ctx.modem.host_frames(since, types=rif5.RF_TYPES)]
    assert not echoed, "%s: the RF ingress circuit was echoed back to the modem: %s" % (case, echoed[:6])
    TALLY["rx"] += 1


def test_rx_dmr_damaged_copies_refresh_the_over_timeout(ctx, web):
    """radio.md 7.3 (S3/S4): "A header or terminator copy that fails its checks inside an open over is ignored ... and refreshes the native-over timeout:
    the firmware still tracks the carrier and sends LOST2 itself", and 7.1: an RF over ends 300 ms without a native frame. The modem plays a DMR over
    of ten voice bursts and then ten header copies with a wrong colour code, 100 ms apart, and no terminator. If the copies refresh the timeout the
    over stays open until the last copy and ends 300 ms after it (about 1.3 s after the last valid burst); if they did not, it would end 300 ms after
    the last valid burst.

    First rig run (docs/rif5-cases.md, "First rig run", RXD): the over ended 0.20 s after the last valid burst, the radio edge's own SILENCE of radio.md 4.4
    (200 ms without a unit; a damaged copy delivers none). PRODUCT finding: the assertion stays."""
    case = "RIF5-RXD"
    ready(ctx, web, case)
    kw = rx_kwargs("dmr", ctx.port)
    frames = rf.canonical_frames("dmr", echo.make_frames("dmr", 30, "rif5-rxd"))
    air, voice, count = ctx.modem.build_keyup("dmr", frames=frames, **kw)
    valid = air[:-1]                                              # header and the ten voice bursts, the real terminator dropped
    bad = rif5.dmr_damaged_copy(kw["src"], kw["dst"], kw["colour_code"])
    air = valid + [(rf.DMR_DATA2, bad, 100.0)] * RXD_COPIES
    listener = member(ctx, "dmr", rif5.ROOMS["dmr"], 1, listen=True)
    try:
        ku = ctx.modem.play_air("dmr", air, voice, count)
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        t_valid = ku.sent[len(valid) - 1][0]
        t_last = ku.sent[-1][0]
        got = wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    assert got and len(overs) == 1, "%s: the client heard %d overs, wanted one" % (case, len(overs))
    over = overs[0]
    # the listener's clock is the same monotonic clock as the modem's (one process); the end packet is the last event of the over
    after_valid, after_last = over.end - t_valid, over.end - t_last
    note(ctx.target, case, "RECORD", "last valid burst to the end of the over %.2f s, last damaged copy to the end %.2f s (refresh: about %.1f s after the last copy)" % (
        after_valid, after_last, rif5.NATIVE_OVER_TIMEOUT_S))
    compare = echo.compare_payload(ku.voice, over.frames)
    assert compare.prefix_ok and not compare.missing, "%s: the voice words heard differ from the words sent" % case
    # radio.md 4.4 gives the radio edge SILENCE = 200 ms (ten units): the core ends an ingress stream that delivers no UNIT for 200 ms, and a damaged
    # copy delivers none. An end at about 0.20 s after the last valid burst is that timer, not the 300 ms native-over timeout of the hub.
    note(ctx.target, case, "RECORD", "end %.3f s after the last valid burst; the document's radio edge SILENCE is %.1f s, the hub's native-over timeout %.1f s" % (
        after_valid, rif5.EDGE_SILENCE_S, rif5.NATIVE_OVER_TIMEOUT_S))
    if rif5.j_on(os.environ):
        # RIF5j (review-277 F5, a8692074 hub_dmr.rs): a burst whose slot type is a clean codeword of ANOTHER colour code is another station's, not a damaged
        # copy of this over: no silence fill, no refresh. The copies of this case are exactly that (dmr_damaged_copy), so the over ends with the hub's 300 ms
        # native-over timeout after the last VALID burst and nothing follows the sent words. The filled, refreshing copies are RIF5-J-RXD-garbled.
        problems = []
        if compare.extra:
            problems.append("%d words follow the sent ones: a wrong colour code burst must not be filled" % len(compare.extra))
        if not rif5.RXD_J_END_S[0] <= after_valid <= rif5.RXD_J_END_S[1]:
            problems.append("the over ended %.2f s after the last valid burst, wanted %.2f to %.2f s: the wrong colour code copies must not refresh the timeout "
                            "(damaged copies stopped %.2f s after it)" % ((after_valid,) + rif5.RXD_J_END_S + (t_last - t_valid,)))
        assert not problems, "%s: %s" % (case, problems)
        TALLY["rx"] += 1
        return
    assert after_valid >= (t_last - t_valid) - 0.05, "%s: the over ended %.2f s after the last valid burst, before the damaged copies stopped (%.2f s): " \
        "the damaged copies did not refresh the native-over timeout of radio.md 7.3 (the room over ends with the radio edge SILENCE of radio.md 4.4, %.1f s " \
        "without a unit)" % (case, after_valid, t_last - t_valid, rif5.EDGE_SILENCE_S)
    assert after_last <= 3.0, "%s: the over ended %.2f s after the last copy, the timeout is 0.3 s" % (case, after_last)
    TALLY["rx"] += 1


def rx_tail_over(ctx, kind, air, voice, count, valid_n, copies_n, tail_s=10.0):
    """Play `air` (the first `valid_n` frames valid, then `copies_n` damaged ones) and return what a listener of the room heard: a namespace with the
    modem's play (`ku`), the over, the number of overs, the send time of the last valid frame and of each damaged copy."""
    listener = member(ctx, kind, rif5.ROOMS[kind], 1, listen=True)
    try:
        ku = ctx.modem.play_air(kind, air, voice, count)
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        wait_for(lambda: [o for o in listener.overs() if o.ended], tail_s)
        time.sleep(0.6)
        overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    return SimpleNamespace(ku=ku, overs=overs, over=overs[0] if overs else None, t_valid=ku.sent[valid_n - 1][0],
                           t_copies=[ku.sent[valid_n + i][0] for i in range(copies_n)])


@pytest.mark.parametrize("copies", (10, 14), ids=("copies10", "copies14_bound13"))
def test_j_rx_dmr_garbled_copies_are_filled_and_bounded(copies, ctx, web):
    """RIF5j (design-276 3.3 R1, R2; a8692074 hub_dv.rs `rf_fill`): a DMR header copy whose LC fails (right colour code, damaged BPTC: a frame the
    firmware delivered) inside an open over is replaced by 3 codec silence units and refreshes the timeout. 10 copies, 100 ms apart, no terminator: 30 words
    after the 30 sent, all one word, the over ends about 300 ms after the last copy (0.2 to 0.5 s). 14 copies: the 13th fill ends the over as LOST, so 39
    words and the end within 0.2 s of the 13th copy (the 14th finds no open over)."""
    if not rif5.j_on(os.environ):
        pytest.skip("DVT_RIF5J is not on: this case is for an image with DV-Router RIF5j (docs/rif5-cases.md \"Cases for the fix image\")")
    case = "RIF5-J-RXD-garbled-%d" % copies
    ready(ctx, web, case)
    kw = rx_kwargs("dmr", ctx.port)
    frames = rf.canonical_frames("dmr", echo.make_frames("dmr", 30, "rif5-jrxd"))
    air, voice, count = ctx.modem.build_keyup("dmr", frames=frames, **kw)
    valid = air[:-1]
    bad = rif5.dmr_garbled_copy(kw["src"], kw["dst"], kw["colour_code"])
    got = rx_tail_over(ctx, "dmr", valid + [(rf.DMR_DATA2, bad, 100.0)] * copies, voice, count, len(valid), copies)
    problems = rx_fill_problems(ctx, case, got, voice, per_copy=3, fills=min(copies, rif5.DMR_FILL_LIMIT), copies=copies, limit=rif5.DMR_FILL_LIMIT)
    assert not problems, "%s: %s" % (case, problems)
    TALLY["rx"] += 1


def rx_fill_problems(ctx, case, got, sent_words, per_copy, fills, copies, limit):
    """The judgement shared by the DMR and YSF fill cases: one over heard and ended, the sent words intact in front, `fills` x `per_copy` equal silence
    words after them, the end at the right time (after the last copy +0.3 s, or within 0.2 s of the `limit`-th when there are more copies)."""
    problems = []
    if got.over is None or len(got.overs) != 1:
        return ["the client heard %d overs, wanted one" % len(got.overs)]
    over = got.over
    compare = echo.compare_payload(sent_words, over.frames)
    extra = compare.extra
    bounded = copies >= limit
    t_end_ref = got.t_copies[limit - 1] if bounded else got.t_copies[-1]
    after = over.end - t_end_ref
    note(ctx.target, case, "RECORD", "end %.3f s after the %s copy; %d extra words (wanted %d), distinct %d, the first %s" % (
        after, "%d-th" % limit if bounded else "last", len(extra), fills * per_copy, len(set(extra)), extra[0].hex() if extra else None))
    if not compare.prefix_ok or compare.missing:
        problems.append("the sent words are not intact in front (first difference at %s, %d missing)" % (compare.first_diff, compare.missing))
    if len(extra) != fills * per_copy:
        problems.append("%d words after the sent ones, wanted %d (%d silence units for each of %d filled copies)" % (len(extra), fills * per_copy, per_copy, fills))
    elif len(set(extra)) != 1:
        problems.append("the %d words after the sent ones are not one silence word (%d different)" % (len(extra), len(set(extra))))
    if not over.ended:
        problems.append("the over did not end on the client side")
    window = (-0.02, 0.2) if bounded else rif5.RXD_J_END_S
    if not window[0] <= after <= window[1]:
        problems.append("the over ended %.2f s after the %s copy, wanted %.2f to %.2f s (%s)" % (
            after, "%d-th" % limit if bounded else "last", window[0], window[1],
            "LOST at the bound" if bounded else "300 ms native-over timeout after the last refresh"))
    return problems


def test_j_rx_dmr_header_train_keeps_one_room_over(ctx, web):
    """RIF5j F3 (review-277): MMDVMHost sends 8 voice LC headers in simplex (MH/DMRSlot.cpp:70), so the first voice burst comes 480 ms after the first
    header, longer than the radio edge SILENCE. A valid header repeat of the open over now refreshes the edge core: one room over, all 150 voice words, no end
    at 400 ms. The first rig run's overs had one header."""
    if not rif5.j_on(os.environ):
        pytest.skip("DVT_RIF5J is not on: this case is for an image with DV-Router RIF5j (docs/rif5-cases.md \"Cases for the fix image\")")
    case = "RIF5-J-RXH-dmr"
    ready(ctx, web, case)
    listener = member(ctx, "dmr", rif5.ROOMS["dmr"], 1, listen=True)
    try:
        since = ctx.modem.mark()
        ku = ctx.modem.keyup("dmr", seconds=RX_SECONDS, seed="rif5-jrxh", headers=8, **rx_kwargs("dmr", ctx.port))
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    problems = []
    if len(overs) != 1:
        problems.append("the client heard %d overs, wanted one (8 headers, then 50 voice bursts)" % len(overs))
    else:
        compare = echo.compare_payload(ku.voice, overs[0].frames)
        note(ctx.target, case, "RECORD", "overs heard: %s; voice words sent %d, heard %d, first difference %s, extra %d, missing %d" % (
            [(round(o.end - o.start, 2), o.idents, len(o.frames), o.ended) for o in overs], len(ku.voice), len(overs[0].frames), compare.first_diff,
            len(compare.extra), compare.missing))
        if not (compare.prefix_ok and not compare.missing and not compare.extra):
            problems.append("the words heard are not the %d sent (first difference %s, %d missing, %d extra)" % (
                len(ku.voice), compare.first_diff, compare.missing, len(compare.extra)))
        if not overs[0].ended:
            problems.append("the over did not end on the client side")
    echoed = [ms.name_of(f.ftype) for f in ctx.modem.host_frames(since, types=rif5.RF_TYPES)]
    if echoed:
        problems.append("the RF ingress circuit was echoed back to the modem: %s" % echoed[:6])
    assert not problems, "%s: %s" % (case, problems)


@pytest.mark.parametrize("bad", (4, 6), ids=("bad4", "bad6_bound5"))
def test_j_rx_ysf_bad_frames_are_filled_and_bounded(bad, ctx, web):
    """RIF5j (design-276 3.3, a8692074 hub_ysf.rs): a V/D mode 2 frame whose DCH fails its CRC inside an open over is replaced by 5 silence units. 6 good
    frames, `bad` bad ones, then (bad 4) 6 good ones and the terminator: 20 equal silence words between the 30 and the 30 sent words (the bad frames' words
    are not heard). Bad 6: the 5th fill ends the over as LOST, 25 silence words, the end within 0.2 s of the 5th bad frame (no terminator is sent)."""
    if not rif5.j_on(os.environ):
        pytest.skip("DVT_RIF5J is not on: this case is for an image with DV-Router RIF5j (docs/rif5-cases.md \"Cases for the fix image\")")
    case = "RIF5-J-RXD-ysf-bad%d" % bad
    ready(ctx, web, case)
    words = rf.canonical_frames("ysf", echo.make_frames("ysf", 80 if bad == 4 else 60, "rif5-jrxy"))
    air, voice, count = ctx.modem.build_keyup("ysf", frames=words, **rx_kwargs("ysf", ctx.port))
    head, frames_, term = air[0], air[1:-1], air[-1]
    damaged = [(t, rif5.ysf_dch_garbled(p), d) for t, p, d in frames_[6:6 + bad]]
    if bad == 4:
        play = [head] + frames_[:6] + damaged + frames_[10:] + [term]
        valid_n = len(play)
    else:
        play = [head] + frames_[:6] + damaged
        valid_n = 7
    listener = member(ctx, "ysf", rif5.ROOMS["ysf"], 1, listen=True)
    try:
        ku = ctx.modem.play_air("ysf", play, voice, count)
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    problems = []
    if len(overs) != 1 or not overs[0].ended:
        problems.append("the client heard %d overs (ended: %s), wanted one that ended" % (len(overs), [o.ended for o in overs]))
    else:
        heard = overs[0].frames
        fills = 5 * (4 if bad == 4 else 5)
        if bad == 4:
            front, middle, back = heard[:30], heard[30:30 + fills], heard[30 + fills:]
            checks = [rif5.compare_ysf_words(voice[:30], front), rif5.compare_ysf_words(voice[50:], back)]
        else:
            front, middle, back = heard[:30], heard[30:], []
            checks = [rif5.compare_ysf_words(voice[:30], front)]
        for check in checks:
            problems += check.problems
            for line in check.findings:
                note(ctx.target, case, "FINDING", line)
        if len(middle) != fills or len(set(middle)) != 1:
            problems.append("%d words between the good frames, %d different, wanted %d equal silence words (5 for each filled frame)" % (
                len(middle), len(set(middle)), fills))
        if bad == 4 and len(back) != len(voice[50:]):
            problems.append("%d words after the bad frames, %d sent" % (len(back), len(voice[50:])))
        if bad == 6:
            t5 = ku.sent[valid_n + 4][0]
            after = overs[0].end - t5
            note(ctx.target, case, "RECORD", "end %.3f s after the 5th bad frame" % after)
            if not -0.02 <= after <= 0.2:
                problems.append("the over ended %.2f s after the 5th bad frame, wanted within 0.2 s (LOST at the bound of 5)" % after)
        note(ctx.target, case, "RECORD", "words heard %d, silence words %d, distinct %d, the first %s" % (
            len(heard), len(middle), len(set(middle)), middle[0].hex() if middle else None))
    assert not problems, "%s: %s" % (case, problems)


VARIANTS = {"local_only": ({"rpt2": rif5.STATION.ljust(7) + "C"}, "local_only", "radio.md 7.3: a wrong RPT2 is local_only and never enters the room"),
            "bad_rpt1": ({"rpt1": rif5.STATION.ljust(7) + "C"}, "bad_header", "radio.md 7.3: a bad RPT1 is bad_header"),
            "bad_crc": ({}, "bad_header", "radio.md 7.3: a bad CRC is bad_header")}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_rx_dstar_header_refused(variant, ctx, web):
    """RF headers the D-STAR framer refuses: nothing reaches the room and the counter of the reason moves (frames_total{dir=rx,result=local_only |
    bad_header}, radio.md 12.3; the metrics come every 10 s, so they are read for up to 30 s)."""
    case = "RIF5-RXR-%s" % variant
    override, result, source = VARIANTS[variant]
    ready(ctx, web, case)
    before = counters_now(ctx)
    kw = dict(rx_kwargs("dstar", ctx.port), **override)
    frames = rf.canonical_frames("dstar", echo.make_frames("dstar", 40, "rif5-refuse"))
    air = rf.dstar_keyup(frames, **kw)
    if variant == "bad_crc":
        ftype, payload, delay = air[0]
        air[0] = (ftype, payload[:-1] + bytes((payload[-1] ^ 0x01,)), delay)
    listener = Member(ctx.ip, "dstar", rif5.ROOMS["dstar"], 1, listen=True)
    try:
        ku = ctx.modem.play_air("dstar", air, voice=frames, count=len(frames))
        assert ku.wait(10)
        time.sleep(2.0)
        heard = listener.overs()
    finally:
        listener.close(settle=0.5)
    assert not heard, "%s: a refused RF header reached the room (%s)" % (case, source)
    if ctx.control is None:
        note(ctx.target, case, "RECORD", "no control channel: the counter %s was not read" % result)
        return
    moved = wait_for(lambda: (rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result=result) or 0) >= 1, 30.0, 4.0)
    assert moved, "%s: dv_edge_radio_frames_total{dir=rx,result=%s} did not move within 30 s (%s)" % (case, result, source)


# ---------------------------------------------------------------- RIF5-LONG: record only

@pytest.mark.parametrize("kind", KINDS)
def test_long_over_is_recorded(kind, ctx, web):
    """A 3 s over from the room in each mode. radio.md 7.1 "Flow control": the host keeps at most 10 D-STAR, 3 DMR and 2 YSF frames in flight, refilled
    from the status every 250 ms, and its queue holds 16 per mode (the oldest are dropped). Whether that sustains a real-time over is not settled by the
    document: the words written against the words sent are a RECORD and a FINDING when words are missing; the case asserts only that the over has a
    start and an end and that no corrupted word was written."""
    case = "RIF5-LONG-%s" % kind
    log, since, sent, ident = tx_over(ctx, web, kind, LONG_FRAMES, "rif5-long", case)
    verdict = judge_tx(kind, log, sent, ident, ctx.port, lossy=True)
    timeline(ctx, case, since, verdict)
    TALLY["long"] += 1
    native = [f for f in log if f.ftype in ms.TX_TYPES[kind]]
    span = native[-1].mono - native[0].mono if native else 0.0
    note(ctx.target, case, "RECORD", "native frames %d over %.2f s; %s" % (len(native), span, ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items()))))
    check_naks(ctx, case, since)
    for problem in verdict.problems + verdict.findings:
        note(ctx.target, case, "FINDING", problem)
    TALLY["tx_native_frames"] += len(native)
    assert native, "%s: no native frame was written" % case
    assert verdict.facts.get("voice_written", 0) > 0 or kind == "dmr" and verdict.facts.get("voice_bursts", 0) > 0, "%s: no voice written" % case


# ---------------------------------------------------------------- RIF5-J: the image with the RIF5j fix (DVT_RIF5J=on)

def test_j_long_overs_are_lossless(ctx, web):
    """design-276 1.8, "Rig (dv-testset), what RIF5 should assert after the fix". A 3 s over from the room in each mode (RIF5-LONG's, but strict): all 150
    voice words written in order and byte-exact (YSF but for the b2 gain steps), the D-STAR sync at position 0 mod 21, the DMR burst A at 0 mod 6, no NAK
    from the fake, GET_STATUS every 50 ms while keyed (median 0.065 s at most), no `underrun` event of the firmware-like fake (DVT_RIF5FW=on; without
    it the fill model cannot run dry and that check is a RECORD), and over the three overs the hub counters: `frames_total{dir=tx,result=dropped}`
    grew by 0, `modem_total{result=tx_underrun}` exists and grew by 0. Timelines: out/<run>/rif5/timeline-J-LONG-<mode>.jsonl."""
    if not rif5.j_on(os.environ):
        pytest.skip("DVT_RIF5J is not on: this case is for an image with DV-Router RIF5j (design-276; docs/rif5-cases.md \"Cases for the fix image\")")
    case = "RIF5-J-LONG"
    problems = []
    firmware = hasattr(ctx.modem, "fw")
    before = None
    if ctx.control is not None:
        ready(ctx, web, case)
        time.sleep(11.0)                                          # the hub metrics are up to 10 s old: a baseline that holds everything before the overs
        before = counters_now(ctx)
    natives = 0
    snaps = [("start", before)]

    def snap(label):
        if before is not None:
            time.sleep(11.0)                                      # the hub metrics are up to 10 s old
            snaps.append((label, counters_now(ctx)))

    sinces = {}
    for kind in KINDS:
        sub = "%s-%s" % (case, kind)
        log, since, sent, ident = tx_over(ctx, web, kind, LONG_FRAMES, "rif5-jlong", sub, after_link=(lambda: snap("ysf join")) if kind == "ysf" else None)
        sinces[kind] = since
        verdict = judge_tx(kind, log, sent, ident, ctx.port)                   # strict: a dropped word is a problem
        native = [f for f in log if f.ftype in ms.TX_TYPES[kind]]
        natives += len(native)
        keyed = rif5.status_cadence_keyed(log, native[0].mono, native[-1].mono) if native else rif5.verdict(["no native frame was written"])
        naks = [(ms.name_of(n[1]), n[2]) for n in ctx.modem.naks if n[0] >= since]
        fw = ctx.modem.fw_events(since, names=("underrun", "nak")) if firmware else None
        timeline(ctx, sub, since, verdict)
        span = native[-1].mono - native[0].mono if native else 0.0
        note(ctx.target, sub, "RECORD", "native frames %d over %.2f s; %s; status while keyed %s; fake underrun events %s" % (
            len(native), span, ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items())), keyed.facts,
            "not modelled (DVT_RIF5FW is off)" if fw is None else len(fw)))
        for line in verdict.findings:
            note(ctx.target, sub, "FINDING", line)
        problems += ["%s: %s" % (kind, p) for p in verdict.problems + keyed.problems]
        if naks:
            problems.append("%s: the modem had to NAK frames of the host: %s" % (kind, naks[:4]))
        if fw:
            problems.append("%s: the firmware-like fake logged %s" % (kind, [(round(t - since, 3), e, d.get("dry_s")) for t, e, d in fw[:4]]))
        if kind == "dstar":
            off = [t for t, _e, d in ctx.modem.fw_events(since, names=("tx_off",)) if d.get("end_frame")] if firmware else []
            rel = rif5.release_view(ctx.modem.host_frames(since), off[-1] if off else None)
            note(ctx.target, sub, "RECORD", "release after the EOT: %s" % rel.facts)
            problems += ["dstar release: %s" % p for p in rel.problems]
        snap("%s over" % kind)
    if before is None:
        note(ctx.target, case, "RECORD", "no control channel: the counters of dropped frames and tx_underrun were not read")
    else:
        after = snaps[-1][1]
        if not rif5.has_series(after, "dv_edge_radio_modem_total", result="tx_underrun"):
            problems.append("dv_edge_radio_modem_total{result=tx_underrun} is not exported (the fixed result label set of the fix)")
        # per phase: dstar over, dmr over, the YSF client's join (a voiceless keyed over), the YSF over. The join is judged by nobody: its tx_underrun is a
        # FINDING (rig run 20261113T052325Z: tx_underrun grew by 1 in the YSF phase, the fake logged none), the three overs of 150 words must show 0 and 0.
        phases = (("dstar over", "start", "dstar over"), ("dmr over", "dstar over", "dmr over"), ("ysf join", "dmr over", "ysf join"),
                  ("ysf over", "ysf join", "ysf over"))
        table = dict(snaps)
        for label, a, b in phases:
            row = {}
            for key, family, labels in (("dropped", "dv_edge_radio_frames_total", {"dir": "tx", "result": "dropped"}),
                                        ("tx_underrun", "dv_edge_radio_modem_total", {"result": "tx_underrun"}),
                                        ("ok", "dv_edge_radio_frames_total", {"dir": "tx", "result": "ok"})):
                row[key] = rif5.delta(table.get(a), table.get(b), family, **labels)
            note(ctx.target, case, "RECORD", "%s: counters grew by %s" % (label, row))
            if label == "ysf join":
                if row["tx_underrun"] or row["dropped"]:
                    if rif5.n_on(os.environ):
                        # RIF5l: a stream without voice never keys a port, so the join has nothing to count (RIF5-N-JOIN asserts the silence on the byte link)
                        problems.append("the YSF join (a stream without voice) was counted as %s: RIF5l says it never keys" % row)
                    else:
                        note(ctx.target, case, "FINDING", "the YSF client's join (header + terminator, no voice) was keyed to RF and the host counted %s" % row)
            else:
                for key in ("dropped", "tx_underrun"):
                    if row[key]:
                        problems.append("%s grew by %s during the %s of 150 words" % (key, row[key], label))
                if row["tx_underrun"]:
                    # radio.md 7.1 at bc9af2b1: a count needs more DATA of the same over after a fresh TX-off status; a header write clears the candidate (DV-Router
                    # answer 13: a +1 here is a product defect). The evidence to send:
                    kind_ = label.split()[0]
                    note(ctx.target, case, "FINDING", "PRODUCT: tx_underrun +%s in the %s; evidence %s; timeline rif5/timeline-%s-%s.jsonl" % (
                        row["tx_underrun"], label, rif5n.underrun_evidence(ctx.modem.events_between(sinces[kind_], sinces[kind_] + 8.0)), case.replace("RIF5-", ""), kind_))
        ok = rif5.delta(before, after, "dv_edge_radio_frames_total", dir="tx", result="ok")
        note(ctx.target, case, "RECORD", "frames_total{dir=tx,result=ok} grew by %s over the whole case, the modem logged %d native frames of the overs" % (ok, natives))
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-N: the image wip-f1f3777 (DVT_RIF5N=on)

N_MODES = ("nxdn", "p25")


def need_n(ctx, *modes):
    """Skip unless DVT_RIF5N is on and the port row of the rendered config has the modes (rig/config/dv-router/rif5n-modes.toml, used INSTEAD of
    rif5-mmdvm-udp.toml: two [[edges.radio.port]] files would define two ports)."""
    if not rif5.n_on(os.environ):
        pytest.skip(rif5n.gate_text(os.environ))
    missing = [m for m in modes if m not in ctx.port.modes]
    if missing:
        pytest.skip("the port row has no %s mode: put rig/config/dv-router/rif5n-modes.toml (or rif5n-ride.toml) in DVT_SUT_CONFIG_EXTRA instead of "
                    "rif5-mmdvm-udp.toml (docs/rif5-cases.md, \"The image wip-f1f3777\")" % missing)
    problem = rif5n.p25_env_problem(os.environ) if "p25" in modes else None
    assert problem is None, "RIF5N setup: %s" % problem


def n_words(kind, n, seed):
    """n deterministic words of the mode as the room client sends them (NXDN: 49-bit words, the last byte keeps its top bit; P25: 11 byte IMBE)."""
    frames = mm.default_frames(kind, n, seed)
    return rf.canonical_frames("nxdn", frames) if kind == "nxdn" else frames


def n_rx_kwargs(kind, port):
    """What the modem's RF over carries: the identity of the radio (N0CALL), and the RAN / NAC of the row (a foreign one is not heard)."""
    if kind == "nxdn":
        return {"src": rif5n.NXDN_IDS[0], "dst": port.nxdn_tg, "ran": port.nxdn_ran}
    return {"src": rif5n.P25_IDS[0], "dst": port.p25_tg, "nac": port.p25_nac}


def play_n(ctx, kind, words, kwargs):
    """The modem plays an RF over of `words` (NXDN or P25) in the shapes of rif5n.rx_air; returns the KeyUp."""
    air, voice, count = ctx.modem.build_keyup(kind, frames=words, **kwargs)
    return ctx.modem.play_air(kind, rif5n.rx_air(kind, air), voice, count)


def judge_n(kind, log, sent, src, port, lossy=False):
    if kind == "nxdn":
        return rif5n.judge_tx_nxdn(log, sent, src, port.nxdn_tg, port.nxdn_ran, lossy=lossy)
    return rif5n.judge_tx_p25(log, sent, src, port.p25_tg, port.p25_nac, lossy=lossy)


def n_tx_over(ctx, web, kind, n, seed, case, talker="same"):
    """One over of `n` words from N0TEST into the room of `kind` (nxdn: S, p25: T); returns (log since the start, words sent, the source id the RF side
    must name). talker "dmr": a DMR client keys the NXDN room instead (NXDN <-> DMR needs no transcoder, media-format.md: both are ambe2-dmr; the 49
    parameter bits pass unchanged), the words sent are the 49-bit form of its 72-bit words and the RF source is the NXDN id of N0TEST."""
    ready(ctx, web, case)
    room = rif5.ROOMS[kind]
    if talker == "same":
        words = n_words(kind, n, "%s-%s" % (seed, kind))
        client = RoomMember(ctx.ip, kind, 1, listen=False)
        sent, src = words, client.ident()
    else:
        words = echo.make_frames("dmr", n, "%s-%s-dmr" % (seed, kind))
        client = member(ctx, "dmr", room, 1, listen=False)
        sent, src = [nxdn.from_dmr(w) for w in echo.expected_frames("dmr", words)], rif5n.NXDN_IDS[1]
    since = ctx.modem.mark()
    try:
        t_talk = time.monotonic()
        client.talk(words, tail=0.5)
        ctx.last_talk = (t_talk, time.monotonic() - 0.5, len(words))
    finally:
        client.close(settle=0.5)
    log = quiesce(ctx.modem, since, ms.TX_TYPES[kind], quiet=1.5, timeout=30.0)
    assert log is not None, "%s: no %s frames reached the modem within 30 s of the end of the over (frames seen: %s)" % (
        case, kind, sorted({ms.name_of(f.ftype) for f in ctx.modem.host_frames(since)}))
    return log, since, sent, src


def n_tally(log, kind, field="tx"):
    TALLY[field] += 1
    TALLY["tx_native_frames"] += len([f for f in log if f.ftype in ms.TX_TYPES[kind]])


def test_n_o1_set_config_enables_the_two_new_modes(ctx):
    """radio.md 7.1 / 7.3: SET_CONFIG enables NXDN 0x10 and P25 0x08, the P25 level at v2 byte 15 and the NXDN level at byte 16 (pct of the row's
    tx_level), the firmware hangs (bytes 23 to 26) zero; every offset is compared by rif5.judge_open from the row of the rendered config."""
    need_n(ctx, "nxdn", "p25")
    d = last_dialogue(ctx.modem)
    assert d is not None, "no complete open dialogue in the modem log"
    verdict = rif5.judge_open(d, ctx.port)
    report(ctx, "RIF5N-O1", verdict)
    cfg = ms.parse_set_config_v2(d.config.payload)
    assert cfg is not None and sorted(cfg["modes"]) == sorted(ctx.port.modes), "SET_CONFIG enables %s, the row has %s" % (cfg and cfg["modes"], sorted(ctx.port.modes))
    assert not verdict.problems, "open dialogue: %s" % verdict.problems


@pytest.mark.parametrize("kind", N_MODES)
def test_n_tx_room_to_modem(kind, ctx, web):
    """RIF5d / RIF5e (radio.md "NXDN (Built, RIF5d S2)", "P25 (Built, RIF5e S2)"): a protocol client of the NXDN room S (TG 31680) or the P25 room T keys
    it; the modem gets SET_MODE 5 / 4, then NXDN one VCALL frame + the voice frames + one TX_REL frame, or P25 one HDU + the LDUs + one TDU; the row's
    RAN / NAC and TG, the talker's id, the words byte for byte. Same protocol both ways: no transcoder, so the comparison is exact with DVT_TRANSCODER_FAKE."""
    need_n(ctx, kind)
    case = "RIF5N-TX-%s" % kind
    log, since, sent, src = n_tx_over(ctx, web, kind, rif5n.SHORT[kind], "rif5n-tx", case)
    verdict = judge_n(kind, log, sent, src, ctx.port)
    report(ctx, case, verdict)
    timeline(ctx, case, since, verdict)
    n_tally(log, kind)
    check_naks(ctx, case, since)
    assert not verdict.problems, "%s: %s" % (case, verdict.problems)


def test_n_tx_dmr_talker_into_the_nxdn_room(ctx, web):
    """NXDN <-> DMR: a DMR client keys room S; DV-Router passes the AMBE+2 parameter bits unchanged between the two (media-format.md: NXDN 49-bit frames
    re-laid out by `dv_lib_radio::ambe2`, "NXDN <-> DMR needs no transcoder"), so the NXDN words on RF are the 49-bit forms of the DMR words sent, exact.
    The RF source is the NXDN id of the callsign (media-format.md, NXDN header: "src = NXDN id of the callsign"): N0TEST = 1002."""
    need_n(ctx, "nxdn")
    case = "RIF5N-TX-nxdn-from-dmr"
    log, since, sent, src = n_tx_over(ctx, web, "nxdn", rif5n.SHORT["nxdn"], "rif5n-xtx", case, talker="dmr")
    verdict = judge_n("nxdn", log, sent, src, ctx.port)
    report(ctx, case, verdict)
    timeline(ctx, case, since, verdict)
    n_tally(log, "nxdn")
    check_naks(ctx, case, since)
    assert not verdict.problems, "%s: %s" % (case, verdict.problems)


def heard_end(member_, kind, timeout=12.0):
    """Wait for the end of an over a RoomMember hears (NXDN: the trailer packet; P25: the terminator record), then 0.6 s more; returns heard()."""
    key = (lambda h: h["trailers"]) if kind == "nxdn" else (lambda h: h["terminators"])
    wait_for(lambda: key(member_.heard()), timeout)
    time.sleep(0.6)
    return member_.heard()


@pytest.mark.parametrize("kind", N_MODES)
def test_n_rx_modem_to_room(kind, ctx, web):
    """RIF5d / RIF5e RX: the modem plays an RF over (NXDN: VCALL FACCH1 frame, 37 voice frames, TX_REL, the row's RAN; P25: HDU, 16 LDUs, TDU, the row's
    NAC, group LC with src 1023007 and the row's TG); a client of the room hears ONE over from N0CALL with the words unchanged and the end; the RF ingress is
    not echoed back (radio.md 4.4, 7.2)."""
    need_n(ctx, kind)
    case = "RIF5N-RX-%s" % kind
    ready(ctx, web, case)
    words = n_words(kind, rif5n.RX_WORDS[kind], "rif5n-rx")
    listener = RoomMember(ctx.ip, kind, 1, listen=True)
    try:
        since = ctx.modem.mark()
        ku = play_n(ctx, kind, words, n_rx_kwargs(kind, ctx.port))
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        heard = heard_end(listener, kind)
    finally:
        listener.close(settle=0.5)
    compare = echo.compare_payload(ku.voice, heard["voice"])
    note(ctx.target, case, "RECORD", "voice words sent %d (padding included), heard %d, first difference %s, extra %d, missing %d; heard %s" % (
        len(ku.voice), len(heard["voice"]), compare.first_diff, len(compare.extra), compare.missing,
        {k: v for k, v in heard.items() if k not in ("voice", "types", "items")}))
    problems = []
    if kind == "nxdn":
        want_id = (rif5n.NXDN_IDS[0], rif5n.NXDN_ROOM_TG)
        if heard["headers"] != [want_id]:
            problems.append("headers heard %s, wanted exactly one, src %d dst %d (the NXDN id of N0CALL and the TG of the client)" % (heard["headers"], *want_id))
        if heard["trailers"] != 1:
            problems.append("%d trailers heard, one expected" % heard["trailers"])
        if heard["other"] or heard["groups"] != {True}:
            problems.append("%d non-voice packets, group flags %s" % (heard["other"], sorted(heard["groups"])))
    else:
        if heard["srcs"] != {rif5n.P25_IDS[0]} or heard["dst"] != rif5n.P25_ROOM_TG:
            problems.append("source ids %s dst %s, wanted {%d} and the server TG %d" % (sorted(heard["srcs"]), heard["dst"], rif5n.P25_IDS[0], rif5n.P25_ROOM_TG))
        if heard["terminators"] != 1:
            problems.append("%d terminators heard, one expected" % heard["terminators"])
        problems += heard["lifecycle"]
        if heard["algid"] - {0x80}:
            problems.append("ALGID %s heard, the call is clear (0x80)" % sorted(heard["algid"]))
    if not (compare.prefix_ok and not compare.missing):
        problems.append("the words heard differ from the words the modem sent (first difference at %s, %d missing)" % (compare.first_diff, compare.missing))
    if compare.extra:
        note(ctx.target, case, "FINDING", "%d voice words after the %d the modem sent (padding of the egress)" % (len(compare.extra), len(ku.voice)))
    timeline(ctx, case, since, SimpleNamespace(facts={"voice_sent": len(ku.voice), "voice_heard": len(heard["voice"])}, problems=problems, findings=[]), talk=False)
    time.sleep(1.5)
    echoed = [ms.name_of(f.ftype) for f in ctx.modem.host_frames(since, types=rif5.RF_TYPES)]
    if echoed:
        problems.append("the RF ingress circuit was echoed back to the modem: %s" % echoed[:6])
    TALLY["rx"] += 1
    assert not problems, "%s: %s" % (case, problems)


def test_n_rx_nxdn_over_is_heard_by_a_dmr_client(ctx, web):
    """NXDN RF -> DMR room client (no transcoder): the DMR words are the 72-bit forms of the 49-bit words the modem sent (`nxdn.to_dmr`), in order, one
    over, ended; the talker is N0CALL (DMR id 1023007 for the NXDN id 1001, RECORD)."""
    need_n(ctx, "nxdn")
    case = "RIF5N-RX-nxdn-to-dmr"
    ready(ctx, web, case)
    words = n_words("nxdn", rif5n.RX_WORDS["nxdn"], "rif5n-xrx")
    listener = member(ctx, "dmr", rif5.ROOMS["nxdn"], 1, listen=True)
    try:
        since = ctx.modem.mark()
        ku = play_n(ctx, "nxdn", words, n_rx_kwargs("nxdn", ctx.port))
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
        time.sleep(0.6)
        overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    assert len(overs) == 1, "%s: the DMR client heard %d overs, wanted one" % (case, len(overs))
    over = overs[0]
    want = [nxdn.to_dmr(w) for w in ku.voice]
    compare = echo.compare_payload(want, over.frames)
    note(ctx.target, case, "RECORD", "talker %s; words wanted %d, heard %d, first difference %s, extra %d, missing %d" % (
        over.idents, len(want), len(over.frames), compare.first_diff, len(compare.extra), compare.missing))
    TALLY["rx"] += 1
    assert over.ended, "%s: the over did not end on the client side" % case
    assert compare.prefix_ok and not compare.missing, "%s: the DMR words heard are not the 72-bit forms of the NXDN words sent (first difference at %s, %d missing)" % (
        case, compare.first_diff, compare.missing)


FOREIGN = {"nxdn": ("ran", lambda port: (port.nxdn_ran + 1) % 64 or 2, "a foreign RAN: supplies neither silence nor a timer refresh and never opens an over"),
           "p25": ("nac", lambda port: port.p25_nac ^ 0x0F0, "a foreign NAC: counted as an RX drop, no fill, no refresh, no late entry")}


@pytest.mark.parametrize("kind", N_MODES)
def test_n_rx_foreign_ran_or_nac_is_not_heard(kind, ctx, web):
    """radio.md NXDN: "Outbound LICH and valid SACCH with a foreign RAN supply neither silence nor a timer refresh" (RAN 0 is accepted, so the foreign RAN is
    neither the row's nor 0); radio.md P25: "Only the exact configured NAC is ours. A foreign NAC increments the RX drop count without silence fill,
    refreshing the over or feeding late entry." A whole over with the foreign value: nothing reaches the room. The counters are a RECORD."""
    need_n(ctx, kind)
    case = "RIF5N-RXR-%s" % ("ran" if kind == "nxdn" else "nac")
    key, pick, why = FOREIGN[kind]
    ready(ctx, web, case)
    kwargs = dict(n_rx_kwargs(kind, ctx.port), **{key: pick(ctx.port)})
    assert kwargs[key] != getattr(ctx.port, "nxdn_ran" if kind == "nxdn" else "p25_nac") and (kind == "p25" or kwargs[key] != 0)
    before = counters_now(ctx)
    listener = RoomMember(ctx.ip, kind, 1, listen=True)
    try:
        ku = play_n(ctx, kind, n_words(kind, 60, "rif5n-foreign"), kwargs)
        assert ku.wait(10.0) and ku.error is None
        time.sleep(2.0)
        packets = listener.items()
    finally:
        listener.close(settle=0.5)
    after = counters_now(ctx) if ctx.control is not None else None
    note(ctx.target, case, "RECORD", "%s = %s (%s); datagrams heard %d" % (key, kwargs[key], why, len(packets)))
    if ctx.control is not None:
        time.sleep(12.0)
        grown = {r: rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result=r) for r in ("ok", "dropped", "bad_header", "unsupported", "local_only")}
        note(ctx.target, case, "RECORD", "frames_total{dir=rx} grew by %s" % grown)
    assert not packets, "%s: an over with %s reached the room (%s)" % (case, why, rif5n.heard_nxdn(packets) if kind == "nxdn" else rif5n.heard_p25(packets))


@pytest.mark.parametrize("kind", N_MODES)
def test_n_long_over_is_lossless(kind, ctx, web):
    """The strict twin of RIF5-J-LONG for the new modes (radio.md 7.1 flow control: Q = 4 NXDN, 2 P25, host queue 1700 ms): a 3 s over from the room,
    every word written, in order, byte exact, one header / HDU, one end frame, GET_STATUS every 50 ms while keyed (median 0.065 s, longest 0.12 s), no NAK,
    no `underrun` of the firmware-like fake (DVT_RIF5FW=on), and over the case `frames_total{dir=tx,result=dropped}` and `modem_total{result=tx_underrun}`
    grew by 0 (hub counters read 11 s before and 12 s after). P25 hardware checks of the same are RIF13 (radio.md P25): this is the host side only."""
    need_n(ctx, kind)
    case = "RIF5N-LONG-%s" % kind
    before = None
    if ctx.control is not None:
        ready(ctx, web, case)
        time.sleep(11.0)
        before = counters_now(ctx)
    log, since, sent, src = n_tx_over(ctx, web, kind, rif5n.LONG[kind], "rif5n-long", case)
    verdict = judge_n(kind, log, sent, src, ctx.port)
    native = [f for f in log if f.ftype in ms.TX_TYPES[kind]]
    keyed = rif5.status_cadence_keyed(log, native[0].mono, native[-1].mono) if native else rif5.verdict(["no native frame was written"])
    naks = [(ms.name_of(n[1]), n[2]) for n in ctx.modem.naks if n[0] >= since]
    fw = ctx.modem.fw_events(since, names=("underrun", "nak")) if hasattr(ctx.modem, "fw_events") else None
    timeline(ctx, case, since, verdict)
    n_tally(log, kind, "long")
    span = native[-1].mono - native[0].mono if native else 0.0
    note(ctx.target, case, "RECORD", "native frames %d over %.2f s; %s; status while keyed %s; fake underrun events %s" % (
        len(native), span, ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items())), keyed.facts,
        "not modelled (DVT_RIF5FW is off)" if fw is None else len(fw)))
    for line in verdict.findings:
        note(ctx.target, case, "FINDING", line)
    problems = verdict.problems + keyed.problems
    if naks:
        problems.append("the modem had to NAK frames of the host: %s" % naks[:4])
    if fw:
        problems.append("the firmware-like fake logged %s" % [(round(t - since, 3), e, d.get("dry_s")) for t, e, d in fw[:4]])
    if before is not None:
        time.sleep(12.0)
        after = counters_now(ctx)
        row = {"dropped": rif5.delta(before, after, "dv_edge_radio_frames_total", dir="tx", result="dropped"),
               "tx_underrun": rif5.delta(before, after, "dv_edge_radio_modem_total", result="tx_underrun"),
               "ok": rif5.delta(before, after, "dv_edge_radio_frames_total", dir="tx", result="ok")}
        note(ctx.target, case, "RECORD", "counters grew by %s, the modem logged %d native frames" % (row, len(native)))
        problems += ["%s grew by %s during the over of %d words" % (k, row[k], len(sent)) for k in ("dropped", "tx_underrun") if row[k]]
        if row["tx_underrun"]:
            note(ctx.target, case, "FINDING", "PRODUCT: tx_underrun +%s; evidence %s (timeline rif5/timeline-%s.jsonl)" % (
                row["tx_underrun"], rif5n.underrun_evidence(ctx.modem.events_between(since)), case.replace("RIF5", "").lstrip("-")))
    assert not problems, "%s: %s" % (case, problems)


@pytest.mark.parametrize("kind", ("dmr", "ysf"))
def test_n_voiceless_join_never_keys_the_port(kind, ctx, web):
    """RIF5l (radio.md "Built (RIF5l)"): "a stream without voice never keys any radio port": a DMR or YSF client's join (a header, 0.3 s, a terminator, no
    voice) writes NOTHING to the modem (no SET_MODE but an idle, no native frame), and a held start that ends without voice is counted once as
    `over_total{dir=tx,result=no_voice}` (asserted for YSF, whose join reached RF on the older images; a RECORD for DMR). The older images keyed the
    port for a YSF join (rig run 20261113T052325Z: SET_MODE 03, a preamble, a header, a terminator; RIF5l)."""
    if not rif5.n_on(os.environ):
        pytest.skip(rif5n.gate_text(os.environ))
    case = "RIF5N-JOIN-%s" % kind
    ready(ctx, web, case)
    before = None
    if ctx.control is not None:
        time.sleep(11.0)
        before = counters_now(ctx)
    t0 = ctx.modem.mark()
    joined = Member(ctx.ip, kind, rif5.ROOMS[kind], 1, listen=False)          # the link is the voiceless stream
    try:
        time.sleep(4.0)                                                       # the core's egress silence timeout is 1.6 s (radio.md RIF5l)
        t1 = time.monotonic()
        verdict = rif5n.judge_no_keying(ctx.modem.host_frames(), t0, t1)
    finally:
        joined.close(settle=0.5)
    report(ctx, case, verdict)
    problems = list(verdict.problems)
    if before is not None:
        time.sleep(12.0)
        after = counters_now(ctx)
        grown = {"no_voice": rif5.delta(before, after, rif5n.NO_VOICE[0], **rif5n.NO_VOICE[1]),
                 "tx_started": rif5.delta(before, after, "dv_edge_radio_over_total", dir="tx", result="started"),
                 "tx_underrun": rif5.delta(before, after, "dv_edge_radio_modem_total", result="tx_underrun")}
        note(ctx.target, case, "RECORD", "counters grew by %s" % grown)
        if kind == "ysf" and not grown["no_voice"]:
            problems.append("over_total{dir=tx,result=no_voice} did not grow for a voiceless YSF join (radio.md RIF5l: a held start that ends without voice is counted)")
        if grown["tx_underrun"]:
            problems.append("tx_underrun grew by %s although nothing was written" % grown["tx_underrun"])
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-N: D-STAR ride-along (RIF5h) and the RFHEARD1 guard

DMR_LAT, DMR_LON = 10.5, 20.25                      # 10 deg 30.00 N, 20 deg 15.00 E, as the DP15 and DP7 cases
RIDE_WORDS = 180                                    # 60 DMR bursts, 3.6 s: the rider needs at least two superframes (0.84 s) after the position is admitted


def dstar_cross_over(ctx, web, case, side=None, n=RIDE_WORDS, seed="rif5n-ride"):
    """A DMR client N0TEST (id 1023008) keys the D-STAR room P (cross-mode into the radio's D-STAR circuit, through the fake transcoder); `side` maps a
    voice packet index to datagrams sent right after it (a DMRG position). The same talk path with and without `side`, so that two overs are alike.
    Returns (host frames since the start, since)."""
    ready(ctx, web, case)
    frames = echo.make_frames("dmr", n, seed)
    talker = member(ctx, "dmr", rif5.ROOMS["dstar"], 1, listen=False)
    since = ctx.modem.mark()
    try:
        dmr.talk_with_side_packets(talker.client, frames, side or {}, stream_id=clients.fresh_stream_id("dmr"), tail=0.5)
    finally:
        talker.close(settle=0.5)
    log = quiesce(ctx.modem, since, ms.TX_TYPES["dstar"], quiet=1.5, timeout=30.0)
    assert log is not None, "%s: no D-STAR frames reached the modem" % case
    TALLY["tx"] += 1
    TALLY["tx_native_frames"] += len([f for f in log if f.ftype in ms.TX_TYPES["dstar"]])
    return log, since


def test_n_ride_along_dprs_of_a_cross_mode_talker(ctx, web):
    """RIF5h (radio.md 7.7, data.md 7.3, config.md `ride_along`): with `ride_along = true` on the port (rif5n-ride.toml) the D-PRS position of a cross-mode
    talker rides in the unused slow-data slots of the D-STAR over already on air, and the over is otherwise identical: the same voice words, the sync
    triple at 0 mod 21, header, EOT, frame count. Two overs of the SAME DMR talker (N0TEST) in room P: the first without a position (the base), the second
    with a DMRG datagram after the 4th voice packet (10.5 N 20.25 E). The slow data the MODEM receives of the second is decoded (dvtest.dstar_sd.Decoder): a
    D-PRS line with a valid CRC from N0TEST with that position. The negative control: on a port without `ride_along` (rif5n-modes.toml, the default) the
    two overs are byte identical in every slow-data field and no D-PRS line is there. Needs the data worker (data-ident.toml in DVT_SUT_CONFIG_EXTRA).
    The status text (DMRA) is not driven: a RECORD of the text events only."""
    if not rif5.n_on(os.environ):
        pytest.skip(rif5n.gate_text(os.environ))
    case = "RIF5N-RIDE"
    on = bool(ctx.port.ride_along)
    if not ctx.config_seen:
        note(ctx.target, case, "RECORD", "the rendered config is not visible: ride_along is taken from the snippet default (off); run with rif5n-ride.toml and "
                                         "the config visible for the positive case")
    base, _ = dstar_cross_over(ctx, web, case + "-base")
    settle_idle(ctx.modem, 30.0)
    rid = clients.DMR_IDS[1]
    ride, since = dstar_cross_over(ctx, web, case, side={3: [dmr.dmrg_packet(rid, DMR_LAT, DMR_LON)]})
    verdict = rif5n.compare_overs(base, ride, rider=on)
    lines_base, lines = rif5n.dprs_lines(base), rif5n.dprs_lines(ride)
    texts = [e["text"] for e in rif5n.rider_events(ride) if e["kind"] == "text"]
    note(ctx.target, case, "RECORD", "ride_along %s; frames %s, changed slow-data fields %d (first %s); D-PRS lines %s; text events %s" % (
        on, verdict.facts["frames"], len(verdict.facts["changed"]), verdict.facts["changed"][:4], lines, texts[:2]))
    problems = list(verdict.problems)
    if lines_base:
        problems.append("the base over (no position sent) carries D-PRS lines %s" % lines_base)
    if on:
        good = [parsed for crc_ok, parsed in lines if crc_ok and parsed]
        if not good:
            problems.append("no D-PRS line with a valid CRC in the slow data of the over (lines %s): the rider did not carry the position (data worker on, "
                            "DMRG admitted, `ride_along = true` in the rendered config?)" % lines)
        else:
            source, _dest, _path, info = good[0]
            if source != "N0TEST" or info[:1] not in (b"!", b"=") or b"1030.00N" not in info or b"02015.00E" not in info:
                problems.append("the D-PRS line is %s, wanted a position of N0TEST at 1030.00N 02015.00E" % (good[0],))
    elif lines:
        problems.append("ride_along is off and the over still carries D-PRS lines %s" % lines)
    assert not problems, "%s: %s" % (case, problems)


def test_n_rfheard1_a_radio_heard_position_is_never_ridden_along(ctx, web):
    """RFHEARD1 (data.md "RF origin", radio.md 7.7; `origin_is_rf` at every RF transmit decision site): data whose origin is a radio or AX.25 port is never
    transmitted on an RF port, "radio and AX.25 origins ... are refused" as a rider. The guard has no producer on this port today (the radio edge sets no
    RF_HEARD and a rider is bound to its own talker's stream), so what is observable is the absence: the modem plays an RF D-STAR over of N0CALL whose slow data
    carries a D-PRS position (heard by the room, admitted), then a DMR talker N0TEST keys the same room WITHOUT a position: its D-STAR over at the modem must
    carry no D-PRS line (neither of N0CALL nor any), nothing of the RF over is written back to the modem, and `dv_pkt_rf_drop_total{reason=rf_origin}` is
    a RECORD (it counts a worker RF group refusal). On a port without ride_along the same must hold: the case runs in both shapes."""
    if not rif5.n_on(os.environ):
        pytest.skip(rif5n.gate_text(os.environ))
    case = "RIF5N-RFHEARD1"
    ready(ctx, web, case)
    before = rif5n.metric_total(hub_text(ctx), *rif5n.RF_DROP[:1], **rif5n.RF_DROP[1])
    fields = rif5n.rf_dprs_fields("N0CALL")
    air = rf.dstar_keyup(echo.make_frames("dstar", 150, "rif5n-heard"), **dict(rx_kwargs("dstar", ctx.port), slow=lambda n: fields[n][1] if n < len(fields) else rf.DSTAR_NULL_SLOW))
    listener = Member(ctx.ip, "dstar", rif5.ROOMS["dstar"], 1, listen=True)
    try:
        since_rf = ctx.modem.mark()
        ku = ctx.modem.play_air("dstar", air, voice=rf.canonical_frames("dstar", echo.make_frames("dstar", 150, "rif5n-heard")), count=150)
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        wait_for(lambda: [o for o in listener.overs() if o.ended], 10.0)
    finally:
        listener.close(settle=0.5)
    TALLY["rx"] += 1
    time.sleep(1.5)
    echoed = [ms.name_of(f.ftype) for f in ctx.modem.host_frames(since_rf, types=rif5.RF_TYPES)]
    settle_idle(ctx.modem, 30.0)
    log, _since = dstar_cross_over(ctx, web, case + "-after")
    lines = rif5n.dprs_lines(log)
    after = rif5n.metric_total(hub_text(ctx), *rif5n.RF_DROP[:1], **rif5n.RF_DROP[1])
    # the series is created lazily (DV-Router answer 16): absent is 0, the first appearance is growth from 0, a zero series is never asserted
    note(ctx.target, case, "RECORD", "dv_pkt_rf_drop_total{reason=rf_origin} %s -> %s (absent = 0; the hub text is up to 10 s old); D-PRS lines in the over after the RF over: %s" % (
        before or 0, after or 0, lines))
    problems = []
    if echoed:
        problems.append("the radio-heard over was written back to the modem: %s" % echoed[:6])
    if lines:
        problems.append("the D-STAR over of a talker WITHOUT a position carries D-PRS lines %s (a radio-heard position rode along)" % lines)
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RADIOSEQ1 (DVT_RADIOSEQ1=on, DV-Router 917b9e3b, image wip-37b7960)

RS_KINDS = ("dstar", "dmr", "ysf", "nxdn", "p25")
RS_SETTLE_S = 11.0                                   # the hub metrics are up to 10 s old: a baseline that holds everything before the over


def rs_listeners(ctx, kind):
    """{name: client} that hear the radio over of `kind`: the DMR (DMRD) client of the room, the YSF client, for NXDN BOTH the NXDN client and a DMR client of the
    same room (NXDN -> DMR needs no transcoder), the P25 client; none for D-STAR (the control: only the counters are read)."""
    wanted = {"dmr": [("dmr", "dmr")], "ysf": [("ysf", "ysf")], "nxdn": [("nxdn", "nxdn"), ("dmr", "dmr")], "p25": [("p25", "p25")]}.get(kind, [])
    out = {}
    try:
        for name, proto in wanted:
            if proto in ("nxdn", "p25"):
                out[name] = RoomMember(ctx.ip, proto, 1, listen=True)
            else:
                out[name] = member(ctx, proto, rif5.ROOMS[kind], 1, listen=True)
    except BaseException:
        for client in out.values():
            client.close(settle=0.0)
        raise
    return out


def rs_play(ctx, kind):
    """The modem plays one lossless RF over of 3 s (150 words, 148 for NXDN, 144 for P25) and returns the KeyUp."""
    if kind in N_MODES:
        return play_n(ctx, kind, n_words(kind, rif5n.RX_WORDS[kind], "radioseq1"), n_rx_kwargs(kind, ctx.port))
    return ctx.modem.keyup(kind, seconds=RX_SECONDS, seed="radioseq1", **rx_kwargs(kind, ctx.port))


def rs_wait_end(client):
    if isinstance(client, RoomMember):
        heard_end(client, client.kind)
    else:
        wait_for(lambda: [o for o in client.overs() if o.ended], 10.0)
        time.sleep(0.6)


def rs_datagrams(client):
    """What the client has received so far, in arrival order."""
    if isinstance(client, RoomMember):
        return [bytes(d) for _t, d in client.items()]
    return [bytes(d) for d in list(client.client.client.inbox)]


def rs_judge(ctx, case, name, kind, client, data, ku, problems):
    """The numbering of what one listener heard: records it always, adds to `problems` what DVT_RADIOSEQ1 asserts."""
    if name == "dmr":
        bursts = radioseq1.dmrd_bursts(data)
        verdict = radioseq1.superframe_verdict(bursts)
        note(ctx.target, case, "RECORD", "DMRD listener: voice letters %s; %s" % (radioseq1.histogram(bursts), ", ".join(
            "%s=%s" % kv for kv in sorted(verdict.facts.items()) if kv[0] != "histogram")))
        problems += ["DMR listener: %s" % p for p in verdict.problems]
        overs = client.overs()
        want = ku.voice if kind == "dmr" else [nxdn.to_dmr(w) for w in ku.voice]
        heard = [f for o in overs for f in o.frames]
        compare = echo.compare_payload(want, heard)
        note(ctx.target, case, "RECORD", "DMR listener: %d overs, words wanted %d, heard %d, first difference %s, missing %d, extra %d" % (
            len(overs), len(want), len(heard), compare.first_diff, compare.missing, len(compare.extra)))
        if len(overs) != 1 or not (compare.prefix_ok and not compare.missing):
            problems.append("DMR listener: %d overs, %d of %d words (first difference at %s, %d missing)" % (
                len(overs), len(heard), len(want), compare.first_diff, compare.missing))
    elif name == "ysf":
        # dv-router: a YSF voice packet carries FT 6 and FN 0..6 (wire.rs voice_fn); FN +1 modulo 7 is asserted, FN 7 is a FINDING and the modulus seen is recorded
        verdict = radioseq1.ysf_fn_check(radioseq1.ysf_fn_values(data), radioseq1.ysf_ft_values(data))
        note(ctx.target, case, "RECORD", "YSF listener: FICH FN of the voice packets, modulus seen %s: %s" % (verdict.facts.get("modulus"), verdict.facts))
        for line in verdict.findings:
            note(ctx.target, case, "FINDING", "YSF egress: %s" % line)
        problems += ["YSF FN: %s" % p for p in verdict.problems]
    elif name == "nxdn":
        verdict = radioseq1.cyclic_progression(radioseq1.nxdn_sacch_structures(data), (4,), radioseq1.NXDN_SACCH_STEP)
        note(ctx.target, case, "RECORD", "NXDN listener: SACCH structure of the voice packets %s" % verdict.facts)
        problems += ["NXDN SACCH: %s" % p for p in verdict.problems]
    else:
        heard = client.heard()
        note(ctx.target, case, "RECORD", "P25 listener: %d words, record types %s, lifecycle %s (the egress numbering of P25 is not decoded: counters only)" % (
            len(heard["voice"]), dict(Counter(heard["types"])), heard["lifecycle"]))


@pytest.mark.parametrize("kind", RS_KINDS)
def test_radioseq1_rx_over_is_numbered_by_frame(kind, ctx, web):
    """RADIOSEQ1 (DV-Router 917b9e3b; radio.md "Built (RADIOSEQ1)", media-format.md "Unit granularity", status.md "dv_room_over_units_total"): a lossless RF over of
    3 s from the fake modem, in each mode. (a) a DMR (DMRD) client of the room (for NXDN: also an NXDN client) hears the voice letters A..F in order per superframe
    with the sync flag on A, none repeated or skipped, and for NXDN -> DMR every word; (b) over the case the hub counters `dv_edge_over_units_total{dir=rx,
    proc=radio,result=gap}` and `dv_room_over_units_total{room,result=gap}` stay 0 and the over is not counted `lossy`; (d) the FICH FN of a YSF egress and the SACCH
    structure of an NXDN egress advance by one frame (YSF: FN +1 modulo 7, FN 0..6 as dv-router says, FN 7 a FINDING; NXDN: 3, 2, 1, 0); P25 egress and D-STAR: counters only. Evidence on bc9af2b1
    (rig run 20261122T075521Z): the DMRD client saw only A and D, `over_gap` 2 per burst (100 for the 150 words), the over `lossy`, YSF 4 per frame; NXDN -> DMR
    heard 78 of 148 words (dmrhb EgressDropped 36).

    DVT_RADIOSEQ1 off or unset: the case plays the over and only RECORDS the letter histogram, the FN / SACCH steps, the words and the counters (a difference the
    switch would assert is a RECORD line "not asserted"); on: every difference is a problem."""
    if kind in N_MODES:
        need_n(ctx, kind)
    case = "RADIOSEQ1-RX-%s" % kind
    strict = radioseq1.on(os.environ)
    ready(ctx, web, case)
    problems = []
    read, before = None, None
    if ctx.control is not None:
        def read():
            try:
                return radioseq1.counts(metrics.parse(ctx.control.metrics("xlxd-sut")), rif5.ROOMS[kind].lower())
            except (RuntimeError, OSError, ValueError) as exc:
                raise TimeoutError("the hub metrics could not be read: %s" % exc)
        try:
            time.sleep(RS_SETTLE_S)
            before = read()
        except TimeoutError as exc:
            problems.append("hub counters: %s" % exc)
            read = None
    listeners = rs_listeners(ctx, kind)
    got = {}
    try:
        ku = rs_play(ctx, kind)
        assert ku.wait(RX_SECONDS + 20) and ku.error is None, "the modem could not play the over: %s" % ku.error
        for client in listeners.values():
            rs_wait_end(client)
        got = {name: rs_datagrams(client) for name, client in listeners.items()}
        for name, client in listeners.items():
            rs_judge(ctx, case, name, kind, client, got[name], ku, problems)
    finally:
        for client in listeners.values():
            client.close(settle=0.5)
    TALLY["rx"] += 1
    if read is None:
        note(ctx.target, case, "RECORD", "no hub counters (the control channel, DVT_RIG_CONTROL=1, is off or unreadable): the gap counters were not read")
    else:
        try:
            after = medload1.wait_settled(read, before)
        except TimeoutError as exc:
            problems.append("hub counters: %s" % exc)
        else:
            change = radioseq1.delta(after, before)
            verdict = radioseq1.judge_counts(change)
            note(ctx.target, case, "RECORD", "hub counters over the over (room %s): %s" % (rif5.ROOMS[kind], dict(change._asdict())))
            problems += ["hub counters: %s" % p for p in verdict.problems]
            if change.overs_end_missing:
                note(ctx.target, case, "FINDING", "the over was counted end_missing (%g), not complete: the end of a lossless RF over was not seen by the room" % change.overs_end_missing)
            if change.egress_dropped or change.egress_lost or change.egress_late:
                note(ctx.target, case, "RECORD", "egress units: dropped %g, lost %g, late %g (all edges, dir=tx)" % (
                    change.egress_dropped, change.egress_lost, change.egress_late))
                if kind == "nxdn" and change.egress_dropped:
                    problems.append("hub counters: dv_edge_over_units_total{dir=tx,result=dropped} rose by %g (the dmrhb EgressDropped 36 of the NXDN -> DMR over)" % change.egress_dropped)
    if not strict:
        for line in problems:
            note(ctx.target, case, "RECORD", "not asserted (DVT_RADIOSEQ1 is off): %s" % line)
        return
    assert not problems, "%s: %s" % (case, problems)


RC_WORDS = 18                                        # one DMR superframe: what the flow control of an image without RIF5j carries without a drop (TX_FRAMES["dmr"])
RC_WORDS_J = 144                                     # 8 superframes (2.88 s) with DVT_RIF5J=on: the 150 word over of RIF5-J-LONG is written whole, whole superframes only


def rc_ready_b(ctx, web, case):
    """The second port is Up (the host polls fake board B), not fenced, and the board is back in idle: ready() for port B."""
    modem = ctx.modem_b
    polled = modem.wait_frame(lambda f: f.ftype == ms.GET_STATUS, since=time.monotonic() - 0.6, timeout=40.0)
    assert polled is not None, "%s: the host does not poll fake board B (port %s is not Up)" % (case, ctx.port_b.name)
    if web is not None:
        res = wait_unfenced(web, ctx.port_b.name)
        assert res.state in ("unfenced", "clear", "absent"), "%s: the TX fence of %s did not end: %s" % (case, ctx.port_b.name, res)
        row = wait_for(lambda: (lambda r: r if r and r.get("state") == "up" else None)(public_row(web, ctx.port_b.name)), 30.0, 0.5)
        assert row is not None, "%s: the public state does not show port %s up" % (case, ctx.port_b.name)
    else:
        d = last_dialogue(modem)
        left = d.config.mono + rif5.GUARD_S + 6.0 - time.monotonic() if d else 0
        if left > 0:
            time.sleep(left)
    if radioseq1.fixed_mode(ctx.port_b):              # a single-mode row is claimed once at Up and stays in its mode (radio.md 7.2 "Fixed mode"): no idle to wait for
        return
    if not settle_idle(modem, 30.0):
        note(ctx.target, case, "FINDING", "the host did not give fake board B back to idle (SET_MODE 0) within 30 s of its last mode claim")


def rc_direction(ctx, case, label, origin, origin_port, other, other_port, words_n, problems):
    """One lossless RF over of DMR from fake board `origin` (port `origin_port`): board `other` must get it as a DMR TX (rif5 judge of the host frames plus
    the letters A..F of the voice bursts, every word), board `origin` must get nothing that keys RF (no echo of its own over). Returns the findings."""
    for board, port in ((origin, origin_port), (other, other_port)):
        if radioseq1.fixed_mode(port):               # a fixed-mode row never returns to idle after a hang (radio.md 7.2); its mode is checked against the SET_MODE log below
            continue
        if not settle_idle(board, 30.0):
            note(ctx.target, case, "FINDING", "%s: a board did not return to idle before the over" % label)
    since_origin, since_other = origin.mark(), other.mark()
    fixed = radioseq1.fixed_mode(other_port)
    earlier = other.host_frames(until=since_other, types=(ms.SET_MODE,))
    words = mm.default_frames("dmr", words_n, "radioseq1-c-%s" % label)
    ku = origin.keyup("dmr", frames=words, **rx_kwargs("dmr", origin_port))
    assert ku.wait(words_n * 0.02 + 25) and ku.error is None, "%s: the modem could not play the over: %s" % (label, ku.error)
    log = quiesce(other, since_other, ms.TX_TYPES["dmr"], quiet=1.5, timeout=30.0)
    if log is None:
        problems.append("%s: no DMR frame reached board %s within 30 s of the end of the over (frames seen: %s)" % (
            label, other_port.name, sorted({ms.name_of(f.ftype) for f in other.host_frames(since_other)})))
    else:
        verdict = radioseq1.relay_verdict(log, ku.voice, other_port.dmr_tg, other_port.color_code, fixed=fixed, earlier=earlier)
        if fixed:
            note(ctx.target, case, "RECORD", "%s: board %s is a fixed-mode row: SET_MODE frames before the over %s, inside it %s" % (
                label, other_port.name, [f.payload.hex() for f in earlier][-4:], [f.payload.hex() for f in log if f.ftype == ms.SET_MODE]))
        note(ctx.target, case, "RECORD", "%s: board %s got the over, voice letters %s of %d, words sent %d, %s" % (
            label, other_port.name, verdict.facts.get("letters_head"), verdict.facts.get("letters_n"), len(ku.voice),
            ", ".join("%s=%s" % kv for kv in sorted(verdict.facts.items()) if kv[0] not in ("letters_head", "letters_n"))))
        if verdict.facts.get("src") != 1023007:
            note(ctx.target, case, "FINDING", "%s: the source id in the LC of the relayed over is %s, the board %s played 1023007" % (
                label, verdict.facts.get("src"), origin_port.name))
        for line in verdict.findings:
            note(ctx.target, case, "FINDING", "%s: %s" % (label, line))
        problems += ["%s: %s" % (label, p) for p in verdict.problems]
        naks = [n for n in other.naks if n[0] >= since_other and n[2] != ms.NAK_FULL]
        if naks:
            problems.append("%s: board %s had to refuse frames of the host: %s" % (label, other_port.name, [(ms.name_of(n[1]), n[2]) for n in naks][:4]))
    time.sleep(1.5)
    echoed = [ms.name_of(f.ftype) for f in origin.host_frames(since_origin, types=rif5.RF_TYPES)]
    if echoed:
        problems.append("%s: the origin board %s got its own over back, RF frames %s (the fan-out skips only the origin circuit)" % (label, origin_port.name, echoed[:6]))
    note(ctx.target, case, "RECORD", "%s: origin board %s got %d RF frames after its own over" % (label, origin_port.name, len(echoed)))


def test_radioseq1_c_radio_to_radio_dmr(ctx, web):
    """(c) Two `mmdvm-serial` ports serve the SAME DMR room (dv-router: allowed with distinct name, byte link and physical; every circuit is its own client session
    and the fan-out skips only the origin circuit, hub.rs / core.rs). A lossless radio DMR over played on fake board A is transmitted by port B: the DMR frames
    of board B have the headers, the voice bursts A..F in order with the sync on A, every word, the terminators; the reverse, B -> A, the same; the origin board
    gets no RF frame of its own over. With DVT_RADIOSEQ1=on every difference is a problem (and the hub counters of the radio edge and room Q: gap 0, not lossy),
    otherwise it is RECORDed ("not asserted"). Needs rig/config/dv-router/rif5-two-ports.toml AFTER rif5-mmdvm-udp.toml or rif5n-modes.toml in
    DVT_SUT_CONFIG_EXTRA (docs/radioseq1-cases.md "(c) between radio ports"); skips with that reason without the second row. 18 words (one superframe), 144 with DVT_RIF5J=on."""
    case = "RADIOSEQ1-C"
    strict = radioseq1.on(os.environ)
    doc = read_config(ctx.target)
    rows = [p for p in (((doc.get("edges") or {}).get("radio") or {}).get("port") or []) if isinstance(p, dict) and p.get("kind") == "mmdvm-serial"]
    note(ctx.target, case, "RECORD", "mmdvm-serial rows in the rendered config: %d (%s)" % (len(rows), [r.get("name") for r in rows]))
    if ctx.port_b is None:
        pytest.skip("RADIOSEQ1-C needs the second radio port: the rendered config has %s row `%s` (rig/config/dv-router/%s after rif5-mmdvm-udp.toml or rif5n-modes.toml "
                    "in DVT_SUT_CONFIG_EXTRA; docs/radioseq1-cases.md)" % ("no mmdvm-serial udp" if not rows else "%d mmdvm-serial row(s) but no" % len(rows),
                                                                           radioseq1.PORT_B, radioseq1.SNIPPET_B))
    unfit = radioseq1.pair_problems(ctx.port, ctx.port_b)
    if unfit:
        pytest.skip("RADIOSEQ1-C: the rows %s and %s are not a pair the case can use: %s" % (ctx.port.name, ctx.port_b.name, "; ".join(unfit)))
    assert ctx.modem_b is not None, "%s: %s" % (case, ctx.b_error)
    if not ctx.modem_b.configured.wait(OPEN_WAIT_S):
        pytest.fail("%s: no SET_CONFIG from the SUT's radio edge within %.0f s on %s:%d for port %s (does it send from %s:%d?)" % (
            case, OPEN_WAIT_S, ctx.port_b.peer[0], ctx.port_b.peer[1], ctx.port_b.name, ctx.port_b.bind[0], ctx.port_b.bind[1]))
    ready(ctx, web, case)
    rc_ready_b(ctx, web, case)
    words_n = RC_WORDS_J if rif5.j_on(os.environ) else RC_WORDS
    problems = []
    read, before = None, None
    if ctx.control is not None:
        def read():
            try:
                return radioseq1.counts(metrics.parse(ctx.control.metrics("xlxd-sut")), rif5.ROOMS["dmr"].lower())
            except (RuntimeError, OSError, ValueError) as exc:
                raise TimeoutError("the hub metrics could not be read: %s" % exc)
        try:
            time.sleep(RS_SETTLE_S)
            before = read()
        except TimeoutError as exc:
            problems.append("hub counters: %s" % exc)
            read = None
    rc_direction(ctx, case, "A->B", ctx.modem, ctx.port, ctx.modem_b, ctx.port_b, words_n, problems)
    time.sleep(2.0)
    rc_direction(ctx, case, "B->A", ctx.modem_b, ctx.port_b, ctx.modem, ctx.port, words_n, problems)
    TALLY["rx"] += 1                                  # port A (the counters of RIF5-M1): one RF over received (A -> B), one over transmitted (B -> A)
    TALLY["tx"] += 1
    if read is None:
        note(ctx.target, case, "RECORD", "no hub counters (the control channel, DVT_RIG_CONTROL=1, is off or unreadable): the gap counters were not read")
    else:
        try:
            after = medload1.wait_settled(read, before)
        except TimeoutError as exc:
            problems.append("hub counters: %s" % exc)
        else:
            change = radioseq1.delta(after, before)
            verdict = radioseq1.judge_counts(change)
            note(ctx.target, case, "RECORD", "hub counters over the two overs (room %s): %s" % (rif5.ROOMS["dmr"], dict(change._asdict())))
            problems += ["hub counters: %s" % p for p in verdict.problems]
    if not strict:
        for line in problems:
            note(ctx.target, case, "RECORD", "not asserted (DVT_RADIOSEQ1 is off): %s" % line)
        return
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-N: PORTCTL1 (DVT_PORTCTL1=on, 1ea6bd00, after wip-f1f3777)

def test_portctl1_the_same_talker_opens_no_new_rts_after_the_room_timeout(ctx, web):
    """PORTCTL1 (radio.md 10.3 "Built (PORTCTL1)", DV-Router answer 15): after the room revokes an RX over (the room's TOT, 180 s, bounds every received over)
    while the carrier remains, the SAME talker's repeated headers (D-STAR MY) start no new RTS until an end frame, a loss, a Down, a different talker or 300 ms of
    silence; each blocked start is counted `over_total{dir=rx,result=rts_suppressed}`. D-STAR, not DMR: a DMR late entry never reaches the gate (the identity stays
    open through a revoke, so the late entry does not fire; a repeated header of the open identity bypasses it) and `rts_suppressed` stays 0 there. A D-STAR RF over of
    N0CALL runs 191.2 s with its header repeated every 100 ms from 181.2 s (the room ends it at 180 s): the client of room P hears ONE over, `rts_suppressed` grew by at
    least 1 (DV-Router's unit test counts 100), and 0.6 s after the carrier stopped a 2 s over of the same talker is a new over (the silence rule). About 4 minutes. The
    image before PORTCTL1 allocates a new sid per repeated header, so it is expected to fail the first assertion. Written offline: the RECORD lines hold the overs heard."""
    if not rif5.portctl1_on(os.environ):
        pytest.skip("DVT_PORTCTL1 is not on: this case is for an image with DV-Router PORTCTL1 (1ea6bd00, the image after wip-f1f3777; docs/rif5-cases.md)")
    case = "RIF5-PORTCTL1"
    ready(ctx, web, case)
    time.sleep(11.0)
    before = counters_now(ctx)
    air = rif5n.stuck_carrier_air(rx_kwargs("dstar", ctx.port))
    listener = member(ctx, "dstar", rif5.ROOMS["dstar"], 1, listen=True)
    try:
        ku = ctx.modem.play_air("dstar", air, voice=[], count=0)
        assert ku.wait(200.0 + 30) and ku.error is None, "the modem could not play the over: %s" % ku.error
        time.sleep(0.6)                                                   # more than the 300 ms silence rule
        long_overs = listener.overs()
        mark = len(long_overs)
        ku2 = ctx.modem.keyup("dstar", seconds=2.0, seed="rif5-portctl1-after", **rx_kwargs("dstar", ctx.port))
        assert ku2.wait(30.0) and ku2.error is None
        wait_for(lambda: len([o for o in listener.overs() if o.ended]) > mark, 10.0)
        time.sleep(0.6)
        all_overs = listener.overs()
    finally:
        listener.close(settle=0.5)
    note(ctx.target, case, "RECORD", "repeated headers sent: %d; overs heard before the second over: %s; after it: %d more" % (
        len([1 for t, _p, _d in air if t == rf.DSTAR_HEADER]) - 1, [(round(o.end - o.start, 1), o.idents, o.ended) for o in long_overs], len(all_overs) - mark))
    time.sleep(12.0)
    after = counters_now(ctx)
    suppressed = rif5.delta(before, after, rif5n.RTS_SUPPRESSED[0], **rif5n.RTS_SUPPRESSED[1])
    note(ctx.target, case, "RECORD", "over_total{dir=rx,result=rts_suppressed} grew by %s" % suppressed)
    problems = []
    if len(long_overs) != 1:
        problems.append("the room heard %d overs in the stuck-carrier over, one expected (the same talker's repeated headers open no new RTS after the timeout)" % len(long_overs))
    if not suppressed:
        problems.append("over_total{dir=rx,result=rts_suppressed} did not grow")
    if len(all_overs) - mark != 1:
        problems.append("0.6 s after the carrier stopped the same talker's 2 s over was heard as %d overs, one expected" % (len(all_overs) - mark))
    assert not problems, "%s: %s" % (case, problems)


# ---------------------------------------------------------------- RIF5-D: a modem that goes silent

def wait_port_state(ctx, web, state, timeout):
    """(seconds, row) until the public row shows `state`, polled every 0.25 s; (None, last row) on timeout."""
    start = time.monotonic()
    row = None
    while time.monotonic() - start < timeout:
        row = public_row(web, ctx.port.name)
        if row and row.get("state") == state:
            return time.monotonic() - start, row
        time.sleep(0.25)
    return None, row


def recover(ctx, web, case, timeout=120.0):
    """After a fault: the modem answers again, wait for the port Up and the fence open."""
    ctx.modem.mute = False
    ctx.modem.status_mode = "valid"
    if web is not None:
        took, row = wait_port_state(ctx, web, "up", timeout)
        assert took is not None, "%s: the port did not come back Up within %.0f s of the modem answering again (row %s)" % (case, timeout, row)
        return took
    d = wait_for(lambda: (lambda x: x if x and x.statuses else None)(last_dialogue(ctx.modem)), timeout, 0.5)
    assert d is not None, "%s: no new dialogue within %.0f s" % (case, timeout)
    return None


def test_d3_a_gap_below_the_timeout_keeps_the_port_up(ctx, web):
    """radio.md 7.1: 2000 ms without a valid status after Up gives Down. A modem silent for 1.0 s answers the next poll: no Down, no new dialogue."""
    case = "RIF5-D3"
    ready(ctx, web, case)
    modem = ctx.modem
    before = len(rif5.dialogues(modem.host_frames()))
    t_mute = modem.mark()
    modem.mute = True
    time.sleep(1.0)
    modem.mute = False
    time.sleep(6.0)
    versions = modem.host_frames(t_mute, types=(ms.GET_VERSION,))
    assert not versions, "%s: a gap of 1.0 s (below the 2 s of the document) started a new dialogue: %d GET_VERSION frames" % (case, len(versions))
    assert len(rif5.dialogues(modem.host_frames())) == before
    if web is not None:
        row = public_row(web, ctx.port.name)
        assert row and row.get("state") == "up", "%s: the port is %s after a 1.0 s gap" % (case, row and row.get("state"))


def test_d2_a_short_status_does_not_refresh_inactivity(ctx, web):
    """radio.md 7.1: "Short status never refreshes inactivity". The modem answers every poll with a 10 byte status (bytes flow, no valid status); the
    port still goes Down after 2 s."""
    case = "RIF5-D2"
    ready(ctx, web, case)
    modem = ctx.modem
    before = counters_now(ctx)
    t_switch = modem.mark()
    modem.status_mode = "short"
    try:
        if web is not None:
            took, row = wait_port_state(ctx, web, "down", 20.0)
            note(ctx.target, case, "RECORD", "the public state shows Down %s s after the first short status; row %s" % (
                None if took is None else round(took, 2), row))
            assert took is not None, "%s: the port is not Down 20 s after the modem began to answer with short statuses" % case
        got = modem.wait_frame(lambda f: f.ftype == ms.GET_VERSION, since=t_switch, timeout=40.0)
        assert got is not None, "%s: the host did not reopen the link" % case
        verdict = rif5.judge_silence(modem.host_frames(), t_switch)
        report(ctx, case, verdict)
        assert not verdict.problems, "%s: %s" % (case, verdict.problems)
    finally:
        recover(ctx, web, case)
    TALLY["down"] += 1
    if ctx.control is not None and before is not None:
        wait_for(lambda: (rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result="short") or 0) >= 1, 30.0, 4.0)
        short = rif5.delta(before, counters_now(ctx), "dv_edge_radio_frames_total", dir="rx", result="short")
        note(ctx.target, case, "RECORD", "frames_total{dir=rx,result=short} grew by %s" % short)
        if not short:
            note(ctx.target, case, "FINDING", "frames_total{dir=rx,result=short} did not count the short statuses (radio.md 12.3 lists `short` among the rx results)")


def test_d1_a_silent_modem_takes_the_port_down(ctx, web, admin_factory):
    """(b) The mechanism is the status poll, not a timeout of bytes: the host polls GET_STATUS every 250 ms; a valid status refreshes inactivity;
    2000 ms without one after Up gives Down with reason `inactive` (radio.md 7.1), the TX gate closes, the link is closed and reopened after a backoff
    (2 s, doubling to 60 s) with the whole dialogue: settle 2 s, GET_VERSION up to six times 1.8 s apart. Seen from outside: the polls stop about 2 s
    after the last answer, the public state shows `down`, the admin row `down_reason` inactive, the counters dv_edge_radio_modem_total{result=inactive}
    and dv_edge_radio_port_total{result=down} move. The modem answers again after the six tries and the port comes back Up."""
    case = "RIF5-D1"
    ready(ctx, web, case)
    modem = ctx.modem
    operator = admin_for(admin_factory, "operator")
    before = counters_now(ctx)
    t_mute = modem.mark()
    modem.mute = True
    reason = None
    try:
        if web is not None:
            took, row = wait_port_state(ctx, web, "down", 25.0)
            note(ctx.target, case, "RECORD", "public state Down %s s after the modem went silent; row %s" % (None if took is None else round(took, 2), row))
            assert took is not None, "%s: the port is not Down 25 s after the modem went silent" % case
            assert row.get("tx_state") == "rx-only", "tx_state %r of a Down port, the document says rx-only (radio.md 4.3: any Down -> RxOnly until the next Up)" % row.get("tx_state")
            if operator is not None:
                admin = wait_for(lambda: (lambda r: r if r and r.get("state") == "down" else None)(admin_row(operator, ctx.port.name)), 10.0, 0.5)
                if admin is not None:
                    reason = admin.get("down_reason")
                    note(ctx.target, case, "RECORD", "admin row: down_reason %r, errors %r, reconnects %r" % (reason, admin.get("errors"), admin.get("reconnects")))
        first = modem.wait_frame(lambda f: f.ftype == ms.GET_VERSION, since=t_mute, timeout=40.0)
        assert first is not None, "%s: no GET_VERSION within 40 s of the modem going silent: the host did not reopen the link" % case
        time.sleep(12.0)                                        # the six tries are 1.8 s apart: 9 s after the first, then the failure
        verdict = rif5.judge_silence(modem.host_frames(), t_mute, tries=True)
        report(ctx, case, verdict)
        note(ctx.target, case, "RECORD", "dialogue starts after the silence (seconds since the previous frame, GET_VERSION tries): %s" % (
            rif5.reopen_delays(modem.host_frames(), t_mute),))
    finally:
        took = recover(ctx, web, case)
    note(ctx.target, case, "RECORD", "the port was Up again %s s after the modem answered" % (None if took is None else round(took, 1)))
    assert not verdict.problems, "%s: %s" % (case, verdict.problems)
    if reason is not None:
        assert reason == "inactive", "%s: down_reason %r right after the silence, the document says inactive (radio.md 12.3)" % (case, reason)
    TALLY["down"] += 1
    TALLY["down_version"] += 1 if verdict.facts.get("version_tries") == rif5.VERSION_TRIES else 0
    if ctx.control is not None and before is not None:
        wait_for(lambda: (rif5.delta(before, counters_now(ctx), "dv_edge_radio_modem_total", result="inactive") or 0) >= 1, 30.0, 4.0)
        after = counters_now(ctx)
        grown = {r: rif5.delta(before, after, "dv_edge_radio_modem_total", result=r) for r in ("inactive", "version_fail", "reopen", "open")}
        grown.update({"port_" + r: rif5.delta(before, after, "dv_edge_radio_port_total", result=r) for r in ("down", "up")})
        note(ctx.target, case, "RECORD", "counter growth: %s" % grown)
        assert (grown["inactive"] or 0) >= 1, "%s: dv_edge_radio_modem_total{result=inactive} did not move" % case
        assert (grown["port_down"] or 0) >= 1, "%s: dv_edge_radio_port_total{result=down} did not move" % case


# ---------------------------------------------------------------- RIF5-M1

def test_m1_counters_account_for_the_overs(ctx, web):
    """dv_edge_radio_over_total{dir=rx|tx,result=started|ended}, dv_edge_radio_frames_total{dir=tx,result=ok}, dv_edge_radio_ptt_total{result=key|unkey}
    against what the cases did since the counters were last at zero (the edge restart of RIF5-G1) or since their first over. The hub counts every 10 s."""
    case = "RIF5-M1"
    if ctx.control is None:
        pytest.skip("needs the rig control channel (DVT_RIG_CONTROL=1, set by scripts/run.sh) for the hub metrics")
    if ctx.baseline is None:
        pytest.skip("no baseline of the counters was taken (no over ran)")
    time.sleep(12.0)
    after = counters_now(ctx)
    assert after is not None, "the hub metrics have no dv_edge_radio_* family of port %s" % ctx.port.name
    grown = {}
    for key, labels in (("rx_started", ("dv_edge_radio_over_total", {"dir": "rx", "result": "started"})),
                        ("rx_ended", ("dv_edge_radio_over_total", {"dir": "rx", "result": "ended"})),
                        ("tx_started", ("dv_edge_radio_over_total", {"dir": "tx", "result": "started"})),
                        ("tx_ended", ("dv_edge_radio_over_total", {"dir": "tx", "result": "ended"})),
                        ("ptt_key", ("dv_edge_radio_ptt_total", {"result": "key"})),
                        ("ptt_unkey", ("dv_edge_radio_ptt_total", {"result": "unkey"})),
                        ("frames_tx_ok", ("dv_edge_radio_frames_total", {"dir": "tx", "result": "ok"})),
                        ("frames_tx_dropped", ("dv_edge_radio_frames_total", {"dir": "tx", "result": "dropped"})),
                        ("frames_rx_ok", ("dv_edge_radio_frames_total", {"dir": "rx", "result": "ok"})),
                        ("frames_rx_mode_busy", ("dv_edge_radio_frames_total", {"dir": "rx", "result": "mode_busy"})),
                        ("over_tx_mode_busy", ("dv_edge_radio_over_total", {"dir": "tx", "result": "mode_busy"}))):
        grown[key] = rif5.delta(ctx.baseline, after, labels[0], **labels[1])
    note(ctx.target, case, "RECORD", "tally %s; counter growth %s" % (dict(TALLY), grown))
    tx_overs, rx_overs = TALLY["tx"] + TALLY["long"], TALLY["rx"]
    if not (tx_overs or rx_overs):
        pytest.skip("no accepted over was made in this run")
    problems = []
    if grown["rx_started"] < rx_overs:
        problems.append("rx started grew by %s, %d RF overs were accepted" % (grown["rx_started"], rx_overs))
    if grown["rx_ended"] < rx_overs:
        problems.append("rx ended grew by %s, %d RF overs were accepted" % (grown["rx_ended"], rx_overs))
    if grown["tx_started"] < tx_overs:
        problems.append("tx started grew by %s, %d room overs were sent" % (grown["tx_started"], tx_overs))
    if grown["ptt_key"] < tx_overs or grown["ptt_unkey"] < tx_overs:
        problems.append("ptt key / unkey grew by %s / %s, %d room overs were sent" % (grown["ptt_key"], grown["ptt_unkey"], tx_overs))
    if not grown["frames_rx_ok"]:
        problems.append("frames_total{dir=rx,result=ok} did not move although %d RF overs were accepted" % rx_overs)
    # Every frame the fake logged since the counters' baseline (the restart of G1), not only those of the overs the cases tallied: the document says
    # ok is byte-link acceptance, so the comparison is with all frames the host wrote to the byte link in that time. Native frames first; then all
    # types, so that a difference can be read against the SET_MODE and other control frames of the run.
    since = ctx.baseline_t
    written = ctx.modem.host_frames(since) if since is not None else []
    native_types = tuple(sorted({t for types in ms.TX_TYPES.values() for t in types}))
    native = [f for f in written if f.ftype in native_types]
    by_type = Counter(ms.name_of(f.ftype) for f in written if f.ftype not in (ms.GET_STATUS, ms.GET_VERSION))
    note(ctx.target, case, "RECORD", "frames the host wrote since the baseline (status polls left out): native %d, by type %s; tallied native frames of the "
                                    "room overs %d" % (len(native), dict(by_type), TALLY["tx_native_frames"]))
    if since is not None and rif5.small5_on(os.environ):
        # SMALL5 (DV-Router d09e388f): the counter counts NXDN and P25 frames too, so the growth is the sum over all five modes (the image before it: +1073 = D-STAR + DMR
        # + YSF, without NXDN 55 and P25 24, rig run of wip-f1f3777). DVT_SMALL5=on asserts it; unset keeps the FINDING below.
        by_mode = rif5.native_by_mode(written)
        note(ctx.target, case, "RECORD", "SMALL5: native frames by mode since the baseline %s, sum %d; frames_total{dir=tx,result=ok} grew by %s" % (
            by_mode, sum(by_mode.values()), grown["frames_tx_ok"]))
        problems += rif5.small5_problems(grown["frames_tx_ok"], by_mode)
    elif since is not None and grown["frames_tx_ok"] != len(native):
        note(ctx.target, case, "FINDING", "frames_total{dir=tx,result=ok} grew by %s, the modem logged %d native frames since the baseline (all types %s; the "
                                          "document: ok is byte-link acceptance)" % (grown["frames_tx_ok"], len(native), dict(by_type)))
    if grown.get("frames_rx_mode_busy") or grown.get("over_tx_mode_busy"):
        # the second rig run (DVT_RIF5FW=on): 2 RF frames of RIF5-RX-ysf were refused as mode_busy (the host held another mode), 5 words lost
        note(ctx.target, case, "FINDING", "the host refused frames as mode_busy: frames_total{dir=rx,result=mode_busy} grew by %s, over_total{dir=tx,"
                                          "result=mode_busy} by %s (an RF frame arriving while the host holds another mode or is keyed)" % (
                                              grown.get("frames_rx_mode_busy"), grown.get("over_tx_mode_busy")))
    if TALLY["long"]:
        note(ctx.target, case, "RECORD", "frames_total{dir=tx,result=dropped} grew by %s over the whole run (the 3 s overs of RIF5-LONG are the likely source)" %
             grown["frames_tx_dropped"])
    assert not problems, "%s: %s" % (case, problems)
