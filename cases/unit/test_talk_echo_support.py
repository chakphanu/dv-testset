# SPDX-License-Identifier: MIT
"""The pure parts of the Talk echo return case (cases/talk_echo_support.py, cases/test_web_talk_echo_return.py): the judges that say which
stage stopped, the request audit, the evidence path, and (when node is installed) the in-page recorder itself, run against a fake
RTCPeerConnection / AudioContext and read back through the same judges. Offline."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import talk_echo_support as tes  # noqa: E402

pytestmark = pytest.mark.unit

NODE = shutil.which("node")


# ------------------------------------------------------------------ hand-built dumps (no node)

def sample(t, pcs):
    return {"t": float(t), "wall": 1000000 + int(t), "hidden": False, "ctx": "running", "pcs": pcs}


def pc(pid, b=0, p=0, energy=0.0, calls=0, rms05=0.0, rms1=0.0, peak=0.0, frac=0.0, stale=False, conn="connected", tapped=True):
    return {"id": pid, "conn": "closed" if stale else conn, "ice": "connected", "sig": "stable", "calls": calls, "stale": stale or None,
            "in": {"bytes": b, "packets": p, "lost": 0, "energy": energy, "n": 1} if b is not None else None,
            "out": None, "pair": None,
            "a": None if (stale or not tapped) else {"calls": calls, "have_s": 1.0, "rms05": rms05, "rms1": rms1, "peak_hz": peak, "fraction": frac}}


MARKS = [{"name": n, "t": t, "wall": 0} for n, t in (("listening", 0.0), ("press", 2000.0), ("on_air", 2500.0), ("release", 5500.0), ("ended", 5600.0))]


def dump(rows, marks=MARKS, pcs=None):
    return {"samples": rows, "marks": marks, "pcs": pcs or [], "errors": [], "ctx_state": "running", "rate": 48000}


def healthy_rows(live=True, echo=True, bytes_=True, swap=False):
    """A 500 ms series 0..20 s: the live over 3.5 .. 6 s, the echo 7 .. 9.5 s; the talker's own page has the echo only."""
    rows, b, calls = [], 0, 0
    for i in range(0, 41):
        t = i * 500
        calls += 12
        tone = (live and 3500 <= t <= 6000) or (echo and 7000 <= t <= 9500)
        if tone and bytes_:
            b += 4000
        loud = dict(rms05=0.17, rms1=0.17, peak=700.0, frac=0.5) if tone else {}
        energy = b / 8000.0 * 0.03
        if swap and t >= 2000:
            rows.append(sample(t, [pc(0, 0, 0, 0.0, 12 * 4, stale=True), pc(1, b, b // 160, energy, calls, **loud)]))
        else:
            rows.append(sample(t, [pc(0, b, b // 160, energy, calls, **loud)]))
    return rows


def test_a_healthy_page_passes_all_three_checks():
    d = dump(healthy_rows())
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    got = {c["name"]: c for c in out["checks"]}
    assert all(c["ok"] for c in out["checks"]), [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
    assert got["talker_hears_echo"]["verdict"]["via"] == "analyser"
    assert got["talker_hears_echo"]["verdict"]["peak_hz"] == 700.0
    assert out["diagnosis"] == "audio reaches both pages (live and echo)"
    assert tes.failure_lines(out["checks"], out["diagnosis"]) == []


def test_no_bytes_after_the_release_is_the_edge_stage():
    talker = dump(healthy_rows(echo=False, live=False, bytes_=False))
    listener = dump(healthy_rows())
    out = tes.analyse_run({"talker": talker, "listener": listener}, "chromium")
    got = {c["name"]: c for c in out["checks"]}
    assert got["talker_hears_echo"]["stage"] == "no_inbound_bytes" and not got["talker_hears_echo"]["ok"]
    assert got["listener_hears_live"]["ok"] and got["listener_hears_echo"]["ok"]
    assert "SECOND page" in out["diagnosis"] and "TALKER page" in out["diagnosis"]
    lines = tes.failure_lines(out["checks"], out["diagnosis"])
    assert lines[0].startswith("PRODUCT CHECK talker_hears_echo stopped at stage no_inbound_bytes: ")
    assert "stops before the page" in lines[0] and lines[-1].startswith("diagnosis: ")


def test_no_bytes_anywhere_says_the_media_stops_at_the_edge():
    quiet = dump(healthy_rows(live=False, echo=False, bytes_=False))
    out = tes.analyse_run({"talker": quiet, "listener": quiet}, "chromium")
    assert [c["stage"] for c in out["checks"]] == ["no_inbound_bytes"] * 3
    assert "stops at or before the edge" in out["diagnosis"]


def test_bytes_but_a_silent_decoder_is_the_page_stage():
    rows = healthy_rows(live=False, echo=False, bytes_=False)
    b = 0
    for r in rows:
        if r["t"] >= 7000:
            b += 4000                                   # bytes arrive after the release, nothing audible, energy flat
        for p in r["pcs"]:
            p["in"]["bytes"], p["in"]["packets"], p["in"]["energy"] = b, b // 160, 0.0
    talker = dump(rows)
    out = tes.analyse_run({"talker": talker, "listener": dump(healthy_rows())}, "chromium")
    got = {c["name"]: c for c in out["checks"]}
    assert got["talker_hears_echo"]["stage"] == "decoder_silent"
    assert "page/decoder" in got["talker_hears_echo"]["detail"]
    assert "page/decoder, not the edge" in out["diagnosis"]


def test_audible_decoder_but_silent_analyser_is_its_own_stage():
    rows = healthy_rows()
    for r in rows:
        for p in r["pcs"]:
            if p.get("a"):
                p["a"].update(rms05=0.0, rms1=0.0, peak_hz=0.0, fraction=0.0)
    v = tes.judge_window(tes.series(dump(rows)), 5500.0, 19500.0, expect_hz=700.0)
    assert not v["ok"] and v["stage"] == "analyser_silent_decoder_audible"


def test_a_dead_tap_falls_back_to_the_decoder_energy_or_the_packets():
    rows = healthy_rows()
    for r in rows:
        for p in r["pcs"]:
            p["calls"] = 0
            if p.get("a"):
                p["a"].update(calls=0, rms05=0.0, rms1=0.0, peak_hz=0.0, fraction=0.0)
    v = tes.judge_window(tes.series(dump(rows)), 5500.0, 19500.0)
    assert v["ok"] and v["stage"] == "ok_decoder_only" and v["via"] == "decoder_energy"
    for r in rows:
        for p in r["pcs"]:
            p["in"]["energy"] = None
    v = tes.judge_window(tes.series(dump(rows)), 5500.0, 19500.0)
    assert v["ok"] and v["stage"] == "ok_packets_only"
    for r in rows:
        for p in r["pcs"]:
            p["in"]["energy"] = 0.0
    v = tes.judge_window(tes.series(dump(rows)), 5500.0, 19500.0)
    assert not v["ok"] and v["stage"] == "tap_dead_decoder_silent"


def test_wrong_frequency_is_judged_on_chromium_only():
    rows = healthy_rows()
    for r in rows:
        for p in r["pcs"]:
            if p.get("a") and p["a"]["rms1"]:
                p["a"]["peak_hz"] = 1000.0
    d = dump(rows)
    assert tes.analyse_run({"talker": d, "listener": d}, "chromium")["checks"][0]["stage"] == "wrong_frequency"
    out = tes.analyse_run({"talker": d, "listener": d}, "firefox")
    assert all(c["ok"] for c in out["checks"]) and out["expect_hz"] is None


def test_a_swapped_session_is_summed_and_detected():
    rows = healthy_rows(swap=True)
    d = dump(rows, pcs=[{"id": 0, "created_at": 100.0, "closed_at": 2000.0}, {"id": 1, "created_at": 2010.0, "closed_at": None}])
    s = tes.series(d)
    assert s[-1]["pcs_total"] == 2 and s[-1]["pcs_live"] == 1
    story = tes.pc_story(d)
    assert story["new_pc_at_talk"] and not story["new_pc_at_release"]
    assert story["at_talk"] == {"created": [1], "closed": [0]}
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    assert all(c["ok"] for c in out["checks"])
    assert out["talker"]["pcs"]["pcs_total"] == 2


def test_missing_data_is_a_failed_check_not_an_exception():
    out = tes.analyse_run({"talker": None, "listener": None}, "chromium")
    assert [c["stage"] for c in out["checks"]] == ["no_data"] * 3
    out = tes.analyse_run({"talker": dump([]), "listener": dump([sample(0, [])])}, "webkit")
    assert not any(c["ok"] for c in out["checks"])
    assert tes.analyse_run({}, "chromium")["checks"][0]["stage"] == "no_data"


def test_sampling_facts_and_series_of_an_empty_dump():
    assert tes.series({}) == []
    f = tes.sampling_facts(tes.series(dump(healthy_rows())))
    assert f["samples"] == 41 and f["max_gap_ms"] == 500.0 and f["hidden_samples"] == 0


# ------------------------------------------------------------------ the request audit and the rest

class FakeReq:
    def __init__(self, url, method="POST", failure=None):
        self.url, self.method, self.failure = url, method, failure
        self.post_data = None


class FakeResp:
    def __init__(self, request, status):
        self.request, self.status = request, status


def test_the_audit_keeps_only_rtc_requests_with_status_and_time():
    clock = iter(x * 1.0 for x in range(100, 200))
    audit = tes.RtcAudit(clock=lambda: next(clock))
    offer = FakeReq("https://dvt-web/api/v1/rtc/offer")
    other = FakeReq("https://dvt-web/api/v1/public/state", "GET")
    ice = FakeReq("https://dvt-web/api/v1/rtc/ice?x=1", "GET")
    close = FakeReq("https://dvt-web/api/v1/rtc/close")
    for r in (offer, other, ice, close):
        audit.on_request(r)
    audit.on_response(FakeResp(offer, 200))
    audit.on_response(FakeResp(other, 200))
    audit.on_response(FakeResp(ice, 404))
    close.failure = "net::ERR_ABORTED"
    audit.on_failed(close)
    assert [(r["method"], r["path"], r["status"]) for r in audit.rows] == [
        ("POST", "/api/v1/rtc/offer", 200), ("GET", "/api/v1/rtc/ice", 404), ("POST", "/api/v1/rtc/close", None)]
    assert audit.rows[2]["failure"] == "net::ERR_ABORTED"
    table = tes.requests_table(audit.rows, {"listening": 99.0, "press": 102.0, "release": 105.0})
    assert [r["phase"] for r in table] == ["listening", "listening", "press"]
    assert table[0]["t_s"] == 1.0
    assert tes.requests_summary(table) == ["POST /api/v1/rtc/offer -> 200", "GET /api/v1/rtc/ice -> 404", "POST /api/v1/rtc/close -> net::ERR_ABORTED"]
    assert tes.rtc_path("https://h/x/rtc/offer?a=b") == "/x/rtc/offer" and tes.rtc_path("https://h/api") is None


def test_phase_of_and_the_public_state_reader():
    phases = {"listening": 10.0, "press": 12.0, "release": 15.0}
    assert tes.phase_of(9.0, phases) == "before" and tes.phase_of(13.0, phases) == "press" and tes.phase_of(99.0, phases) == "release"
    state = {"rooms": [{"room": "D"}, {"room": "E", "onair": {"callsign": "N0BRU"}}]}
    assert tes.onair_summary(state, "E")["callsign"] == "N0BRU" and tes.onair_summary(state, "E")["onair"]
    assert tes.onair_summary(state, "Z") == {"onair": False, "callsign": None, "entry": {}}
    assert tes.onair_summary({}, "E")["onair"] is False


def test_evidence_file_is_written_and_a_bad_path_does_not_raise(tmp_path):
    path = tes.evidence_path(tmp_path, "chromium")
    assert path == tmp_path / "web" / "talk-echo-chromium.json"
    assert tes.write_evidence(path, {"a": 1}) and json.loads(path.read_text()) == {"a": 1}
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert tes.write_evidence(blocker / "sub" / "x.json", {"a": 1}) is False
    assert tes.write_evidence(tmp_path / "y.json", {"bad": object()}) is False


def test_thresholds_are_prod_talks():
    from dvtest.tools import prod_talk
    assert (tes.RMS_FLOOR, tes.TONE_HZ, tes.TONE_TOL_HZ, tes.MIN_TONE_FRACTION) == (
        prod_talk.RMS_FLOOR, prod_talk.TONE_HZ, prod_talk.TONE_TOL_HZ, prod_talk.MIN_TONE_FRACTION)
    assert tes.EXPECT_HZ == {"chromium": 700.0}


def test_recorder_script_is_ascii_and_wraps_the_constructor():
    js = tes.ECHO_RECORDER_JS
    assert js.isascii()
    for needle in ("window.RTCPeerConnection = Wrapped", "closedAt", "createdAt", "getStats", "totalAudioEnergy", "bytesSent", "responseStatus",
                   "createScriptProcessor"):
        assert needle in js


# ------------------------------------------------------------------ the recorder in node

PRELUDE = r"""
globalThis.window = globalThis;
let T = 0;
globalThis.performance = { now: () => T, getEntriesByType: () => [
  {name: 'https://h/api/v1/rtc/offer', startTime: 10, duration: 5, responseStatus: 200},
  {name: 'https://h/api/v1/public/state', startTime: 11, duration: 5, responseStatus: 200}] };
const UI = { talk: 'Ready' };
globalThis.document = { hidden: false, documentElement: {}, createElement: () => ({ muted: false, play: () => Promise.resolve() }),
  querySelector: (s) => (s === '#talk-status' ? { textContent: UI.talk } : null) };
globalThis.MutationObserver = class { constructor(f) { globalThis.__mo = f; } observe() {} };
const connHandlers = [];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { connection: { effectiveType: '4g', rtt: 50, downlink: 10,
  saveData: false, addEventListener: (n, f) => connHandlers.push(f) } } });
globalThis.MediaStream = class { constructor(t) { this.tracks = t; } };
const procs = [];
globalThis.AudioContext = class {
  constructor() { this.sampleRate = 48000; this.state = 'running'; this.destination = {}; }
  resume() { return Promise.resolve(); }
  createMediaStreamSource() { return { connect() {} }; }
  createScriptProcessor() { const p = { connect() {}, onaudioprocess: null }; procs.push(p); return p; }
};
globalThis.RTCPeerConnection = class {
  constructor() { this.connectionState = 'connected'; this.iceConnectionState = 'connected'; this.signalingState = 'stable'; this.h = {};
                  this.s = { bytes: 0, packets: 0, energy: 0 }; }
  addEventListener(n, f) { (this.h[n] = this.h[n] || []).push(f); }
  emit(n, ev) { (this.h[n] || []).forEach((f) => f(ev)); }
  close() { this.connectionState = 'closed'; }
  getStats() {
    const s = this.s, m = new Map();
    m.set('i1', {id: 'i1', type: 'inbound-rtp', kind: 'audio', bytesReceived: s.bytes, packetsReceived: s.packets, totalAudioEnergy: s.energy, audioLevel: 0.1});
    m.set('o1', {id: 'o1', type: 'outbound-rtp', kind: 'audio', bytesSent: 7, packetsSent: 1});
    m.set('t1', {id: 't1', type: 'transport', selectedCandidatePairId: 'p1'});
    m.set('p1', {id: 'p1', type: 'candidate-pair', state: 'succeeded', bytesReceived: s.bytes + 100, bytesSent: 50});
    return Promise.resolve(m);
  }
};
"""

SCENARIO = r"""
const P = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const feed = (proc, amp, seconds) => {
  const n = Math.round(48000 * seconds);
  for (let at = 0; at < n; at += 4096) {
    const chunk = new Float32Array(Math.min(4096, n - at));
    for (let i = 0; i < chunk.length; i++) chunk[i] = amp * Math.sin(2 * Math.PI * 700 * (at + i) / 48000);
    proc.onaudioprocess({inputBuffer: {getChannelData: () => chunk}});
  }
};
(async () => {
  const rec = window.__dvtEcho;
  let cur = new RTCPeerConnection();
  cur.emit('track', {track: {kind: 'audio'}});
  let proc = procs[procs.length - 1];
  for (T = 0; T <= 20000; T += 500) {
    if (T === 0) rec.mark('listening');
    if (T === 2000) {
      rec.mark('press');
      if (P.swap) { cur.close(); cur = new RTCPeerConnection(); cur.emit('track', {track: {kind: 'audio'}}); proc = procs[procs.length - 1]; }
    }
    if (T === 2500) rec.mark('on_air');
    if (P.conn_change && T === 4500) { navigator.connection.effectiveType = '3g'; navigator.connection.rtt = 300; connHandlers.forEach((f) => f()); }
    if (T === 2500) { UI.talk = 'On air'; __mo(); }
    if (P.early_end && T === 4500) { UI.talk = 'Ended: Button released.'; __mo(); }
    if (T === 5500) rec.mark('release');
    if (!P.early_end && T === 5500) { UI.talk = 'Ended: Button released.'; __mo(); }
    if (T === 5600) rec.mark('ended');
    const live = P.live && T >= 3500 && T <= 6000;
    const echo = P.echo && T >= 7000 && T <= 9500;
    const tone = live || echo;
    if (P.tap && proc) feed(proc, tone ? 0.25 : 0, 0.5);
    if (tone || P.bytes_always) { cur.s.bytes += 4000; cur.s.packets += 25; }
    if (tone && P.energy) cur.s.energy += 0.25 * 0.25 / 2 * 0.5;
    await rec.tick();
  }
  rec.mark('final');
  console.log(JSON.stringify(rec.dump()));
})();
"""


def run_recorder(**params):
    params = dict({"swap": True, "live": True, "echo": True, "tap": True, "energy": True, "bytes_always": False}, **params)
    script = PRELUDE + tes.ECHO_RECORDER_JS + SCENARIO
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scenario.js"
        path.write_text(script, encoding="utf-8")
        done = subprocess.run([NODE, str(path)], input=json.dumps(params), capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[:600]
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed: the in-page recorder is not run")


def test_recorder_script_is_valid_javascript():
    if NODE is None:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recorder.js"
        path.write_text(tes.ECHO_RECORDER_JS, encoding="utf-8")
        done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[:400]


@needs_node
def test_recorder_in_node_sees_the_swap_the_tone_and_the_requests():
    d = run_recorder()
    assert d["errors"] == [] and d["missing"] is False and d["rate"] == 48000
    assert [p["id"] for p in d["pcs"]] == [0, 1]
    assert d["pcs"][0]["closed_at"] == 2000.0 and d["pcs"][1]["closed_at"] is None and d["pcs"][1]["created_at"] == 2000.0
    assert d["pcs"][1]["tapped"] and d["pcs"][1]["calls"] > 0
    assert d["resources"] == [{"path": "/api/v1/rtc/offer", "t": 10.0, "dur": 5.0, "status": 200}]
    story = tes.pc_story(d)
    assert story["new_pc_at_talk"] and story["at_talk"]["closed"] == [0] and not story["new_pc_at_release"]
    # the closed pc is carried forward as stale, with its last counters
    first_closed = next(s for s in d["samples"] if s["t"] == 2500.0)
    assert [p.get("stale") for p in first_closed["pcs"]] == [True, None]
    # the analyser reads the tone: rms of amp 0.25 is 0.1768, the scan finds 700 Hz
    loud = [s["pcs"][1]["a"] for s in d["samples"] if s["t"] == 8500.0]
    assert loud and abs(loud[0]["rms05"] - 0.1768) < 0.005 and loud[0]["peak_hz"] == 700 and loud[0]["fraction"] > 0.9
    quiet = [s["pcs"][1]["a"] for s in d["samples"] if s["t"] == 12000.0]
    assert quiet[0]["rms05"] == 0.0
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    assert all(c["ok"] for c in out["checks"]), [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
    assert out["talker"]["sampling"]["samples"] == 41


@needs_node
def test_recorder_in_node_no_echo_bytes_is_the_edge_stage():
    d = run_recorder(echo=False)
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    got = {c["name"]: c for c in out["checks"]}
    assert got["listener_hears_live"]["ok"]
    assert got["listener_hears_echo"]["stage"] == "no_inbound_bytes"          # the live over is heard, the echo never arrives


@needs_node
def test_recorder_in_node_bytes_but_silent_decoder():
    d = run_recorder(live=False, echo=False, bytes_always=True, energy=False)
    v = tes.judge_window(tes.series(d), 5500.0, 19500.0, expect_hz=700.0)
    assert v["stage"] == "decoder_silent" and v["inbound_bytes"] > 0 and v["tap_callbacks"] > 0


@needs_node
def test_recorder_in_node_dead_tap_is_told_from_a_silent_page():
    d = run_recorder(tap=False)
    v = tes.judge_window(tes.series(d), 5500.0, 19500.0)
    assert v["ok"] and v["stage"] == "ok_decoder_only" and v["tap_callbacks"] == 0


@needs_node
def test_recorder_in_node_keeps_connection_changes_ui_and_the_talk_pc():
    d = run_recorder(conn_change=True, early_end=True)
    assert d["errors"] == []
    assert d["connection"]["supported"] and d["connection"]["initial"]["effectiveType"] == "4g"
    assert [(e["t"], e["effectiveType"], e["rtt"]) for e in d["connection"]["events"]] == [(4500.0, "3g", 300)]
    ended = [u for u in d["ui"] if u["talk"] == "Ended: Button released."]
    assert ended and ended[0]["t"] == 4500.0
    line = tes.ice1_diagnosis(d, [], None)
    assert "connection-change fired before the echo window: 1" in line and "effectiveType 3g" in line
    assert "-1000 ms from the release (BEFORE the release" in line and "offers: 0" in line
    # the pc created at Talk keeps being sampled and carries the echo; the closed Listen pc does not
    per = {p["id"]: p for p in tes.per_pc_inbound(d, 5500.0, 19500.0)}
    assert per[1]["bytes"] > 0 and per[1]["packets"] > 0 and per[1]["last_in"]["bytes"] > 0 and per[1]["sampled_live_until_ms"] == 19500.0
    assert per[0]["bytes"] == 0 and per[0]["closed_at"] == 2000.0 and per[0]["live_samples_in_window"] == 0


@needs_node
def test_recorder_in_node_without_a_connection_change_or_early_end():
    d = run_recorder()
    line = tes.ice1_diagnosis(d, [{"sess_id_in_body": False}, {"sess_id_in_body": True}], 2.0)
    assert line.startswith("no connection-change before the echo window (0 in all)")
    assert "offers: 2 (1 with sess_id" in line and "restarts counter delta: 2.0" in line
    assert "'Ended: Button released.' shown +0 ms from the release;" in line + ";"


def test_offer_bodies_are_sanitised_and_summarised():
    body = json.dumps({"sdp": "v=0\nm=audio 9 UDP/TLS/RTP/SAVPF 0\na=ice-ufrag:abc\n", "type": "offer", "sess_id": "S1", "room": "E",
                       "csrf": "tok", "extra": {"Cookie": "c", "password": "p", "ok": 1}})
    got = tes.sanitize_body(body)
    assert got["csrf"] == "<removed>" and got["extra"] == {"Cookie": "<removed>", "password": "<removed>", "ok": 1}
    assert got["sess_id"] == "S1" and got["room"] == "E"
    assert tes.sanitize_body(None) == {"raw_length": 0} and tes.sanitize_body("not json") == {"raw_length": 8}
    assert tes.sanitize_body("[1]") == {"raw_length": 3}
    assert len(tes.sanitize_body(json.dumps({"sdp": "x" * 30000}))["sdp"]) < 20100
    audit = tes.RtcAudit(clock=lambda: 5.0)
    first, second = FakeReq("https://h/api/v1/rtc/offer"), FakeReq("https://h/api/v1/rtc/offer")
    first.post_data, second.post_data = body, json.dumps({"sess_id": "S1", "sdp": "m=audio 9 x"})
    for r, st in ((first, 200), (second, 200)):
        audit.on_request(r)
        audit.on_response(FakeResp(r, st))
    table = tes.requests_table(audit.rows, {"listening": 0.0})
    assert "csrf" not in json.dumps(table) or "<removed>" in json.dumps(table)
    offers = tes.offers_summary(table)
    assert [o["index"] for o in offers] == [1, 2] and all(o["sess_id_in_body"] for o in offers)
    assert offers[0]["m_lines"] == ["m=audio"] and offers[0]["ice_ufrag_lines"] == 1 and offers[0]["status"] == 200


def test_ice_metrics_filter_and_delta():
    samples = [{"name": "dv_edge_webrtc_ice_restarts_total", "labels": {"proc": "webrtc"}, "value": 1.0},
               {"name": "dv_edge_webrtc_listeners", "labels": {}, "value": 1.0},
               {"name": "dv_edge_webrtc_service_total", "labels": {}, "value": 4.0},
               {"name": "dv_edge_webrtc_ice_failed_total", "labels": {}, "value": 0.0},
               {"name": "dv_other_ice_total", "labels": {}, "value": 9.0}]
    kept = tes.ice_metrics(samples)
    assert [s["name"] for s in kept] == ["dv_edge_webrtc_ice_restarts_total", "dv_edge_webrtc_ice_failed_total"]
    after = [dict(kept[0], value=3.0), dict(kept[1])]
    delta = tes.metrics_delta(kept, after)
    assert tes.restarts_delta(delta) == 2.0
    assert tes.metrics_delta(None, after) is None and tes.restarts_delta(None) is None
    assert tes.restarts_delta(tes.metrics_delta([kept[1]], [kept[1]])) == "absent"


def test_forced_change_phase_lines_and_paths(tmp_path):
    assert tes.conn_phase({}) == "off" and tes.conn_phase({tes.FORCE_ENV: " Talk "}) == "talk"
    for good in ("off", "talk", "release", "echo"):
        assert tes.conn_phase({tes.FORCE_ENV: good}) == good
    with pytest.raises(ValueError):
        tes.conn_phase({tes.FORCE_ENV: "now"})
    assert tes.forced_lines(["a", "b"], "off") == ["a", "b"]
    assert tes.forced_lines(["PRODUCT CHECK x"], "release") == ["after a forced connection change at release: PRODUCT CHECK x"]
    assert tes.evidence_path(tmp_path, "chromium").name == "talk-echo-chromium.json"
    assert tes.evidence_path(tmp_path, "chromium", "echo").name == "talk-echo-chromium-conn-echo.json"
    assert "dispatchEvent(new Event('change'))" in tes.FORCE_JS and tes.FORCE_JS.isascii()


def test_evidence_path_numbers_the_pttrx1_repeats(tmp_path):
    assert tes.evidence_path(tmp_path, "chromium", "talk", 3).name == "talk-echo-chromium-conn-talk-3.json"
    assert tes.evidence_path(tmp_path, "chromium", "talk").name == "talk-echo-chromium-conn-talk.json"
    assert tes.evidence_path(tmp_path, "chromium").name == "talk-echo-chromium.json"
