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

def element(t, owner, rms=None, ready="live", muted=False, paused=False, stream=None, tracks=None, ctx="running"):
    """The page's audio element bound to the stream of pc `owner` (a dict of pc()), as the recorder reports it."""
    if tracks is None:
        tracks = [{"id": "t%s" % owner["id"], "kind": "audio", "ready": ready, "enabled": True, "muted": muted, "pc": owner["id"]}]
    a = owner.get("a") or {}
    level = a.get("rms05", 0.0) if rms is None else rms
    return {"present": True, "stream": stream or "s%s" % owner["id"], "tracks": tracks, "paused": paused, "muted": False, "volume": 1.0, "ready": 4,
            "attached": True, "attach_error": None, "changes": 1, "polls": int(t // 250), "an_rms": level, "an_last": level, "an_n": 2, "ctx": ctx}


def sample(t, pcs, el="auto"):
    if el == "auto":                                   # a healthy page: the element plays the newest live session
        live = [p for p in pcs if not p.get("stale")]
        el = element(t, live[-1]) if live else None
    return {"t": float(t), "wall": 1000000 + int(t), "hidden": False, "ctx": "running", "pcs": pcs, "el": el}


RECEIVER = ("talker_hears_echo", "listener_hears_live", "listener_hears_echo")


def stages(out, names=RECEIVER):
    return [c["stage"] for c in out["checks"] if c["name"] in names]


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
    assert lines[0].startswith("evidence talker_hears_echo stopped at stage no_inbound_bytes: ")
    assert "stops before the page" in lines[0] and lines[-1].startswith("diagnosis: ")


def test_no_bytes_anywhere_says_the_media_stops_at_the_edge():
    quiet = dump(healthy_rows(live=False, echo=False, bytes_=False))
    out = tes.analyse_run({"talker": quiet, "listener": quiet}, "chromium")
    assert stages(out) == ["no_inbound_bytes"] * 3
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
    assert "page/decoder, not the edge" in out["diagnosis"]                       # the element sits on the live pc and is silent like the receiver


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
    assert stages(out) == ["no_data"] * 3
    out = tes.analyse_run({"talker": dump([]), "listener": dump([sample(0, [])])}, "webkit")
    assert not any(c["ok"] for c in out["checks"] if c["name"] != "over_not_cut_short")
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
const EL = { talk: { textContent: 'Ready' }, listen: { textContent: 'Listening on module E' }, replace: false };
const setText = (k, v) => { if (EL.replace) EL[k] = { textContent: v }; else EL[k].textContent = v; };
const UI = { get talk() { return EL.talk.textContent; }, set talk(v) { setText('talk', v); }, get listen() { return EL.listen.textContent; }, set listen(v) { setText('listen', v); } };
let SEQ = 0;
class Ev { constructor() { this.h = {}; } addEventListener(n, f) { (this.h[n] = this.h[n] || []).push(f); } emit(n, e) { (this.h[n] || []).forEach((f) => f(e || {})); } }
globalThis.MediaStream = class extends Ev {
  constructor(t) { super(); this.id = 's' + (++SEQ); this.tracks = (t || []).slice(); }
  getAudioTracks() { return this.tracks.filter((x) => x.kind === 'audio'); }
  addTrack(t) { this.tracks.push(t); this.emit('addtrack', {track: t}); }
};
const makeTrack = (id, muted) => Object.assign(new Ev(), {id: id, kind: 'audio', readyState: 'live', enabled: true, muted: !!muted});
class HME { get srcObject() { return this._s || null; } set srcObject(v) { this._s = v; } matches(sel) { return sel === "[data-testid='listen-audio']"; } }
globalThis.HTMLMediaElement = HME;
const ELEM = Object.assign(new HME(), { paused: false, muted: false, volume: 1, readyState: 4 });
const ANALYSER = { amp: 0 };
globalThis.document = { hidden: false, documentElement: {}, createElement: () => ({ muted: false, play: () => Promise.resolve() }),
  querySelector: (s) => (s === '#talk-status' ? EL.talk : (s === "[data-testid='listen-connection'] p[role=status]" ? EL.listen : (s === "[data-testid='listen-audio']" ? ELEM : null))),
  querySelectorAll: (s) => { const e = document.querySelector(s); return e ? [e] : []; } };
globalThis.MutationObserver = class { constructor(f) { globalThis.__mo = f; } observe() {} };
const connHandlers = [];
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { connection: { effectiveType: '4g', rtt: 50, downlink: 10,
  saveData: false, addEventListener: (n, f) => connHandlers.push(f) } } });
const procs = [];
globalThis.AudioContext = class {
  constructor() { this.sampleRate = 48000; this.state = 'running'; this.destination = {}; }
  resume() { return Promise.resolve(); }
  createMediaStreamSource(stream) {
    if (stream.getAudioTracks && !stream.getAudioTracks().length) throw new Error('no audio tracks in the stream');
    return { connect() {}, disconnect() {} };
  }
  createAnalyser() {
    return { fftSize: 0, connect() {}, getFloatTimeDomainData(buf) { for (let i = 0; i < buf.length; i++) buf[i] = ANALYSER.amp * Math.sin(2 * Math.PI * 700 * i / 48000); } };
  }
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
  EL.replace = !!P.replace_el;
  let cur = new RTCPeerConnection();
  let curTrack = makeTrack('t0');
  cur.emit('track', {track: curTrack});
  let proc = procs[procs.length - 1];
  if (P.element) { rec.el.selector = "[data-testid='listen-audio']"; ELEM.srcObject = new MediaStream([curTrack]); }
  for (T = 0; T <= 20000; T += 500) {
    if (T === 0) { rec.mark('listening'); __mo(); }
    if (T === 9000) { UI.listen = 'Idle'; __mo(); }
    if (T === 2000) {
      rec.mark('press');
      if (P.pending) { UI.talk = 'Pending - queued / waiting for the floor'; __mo(); T = 2034; UI.talk = 'On air'; __mo(); T = 2000; }
      if (P.swap) {
        const old = curTrack;
        cur.close(); cur = new RTCPeerConnection();
        curTrack = makeTrack('t1', P.element === 'late_track');
        cur.emit('track', {track: curTrack});
        proc = procs[procs.length - 1];
        if (P.element === 'rebind') ELEM.srcObject = new MediaStream([curTrack]);
        if (P.element === 'stale') { old.readyState = 'ended'; old.emit('ended'); }
        if (P.element === 'late_track') ELEM.srcObject = new MediaStream([]);
      }
    }
    if (T === 2500) rec.mark('on_air');
    if (T === 2500 && P.element === 'late_track') ELEM.srcObject.addTrack(curTrack);
    if (T === 3000 && P.element === 'late_track') { curTrack.muted = false; curTrack.emit('unmute'); }
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
    if (P.element) {
      ANALYSER.amp = tone && ELEM.srcObject && ELEM.srcObject.getAudioTracks().indexOf(curTrack) >= 0 && curTrack.readyState === 'live' ? 0.25 : 0;
      const base = T; T = base - 250; rec.elPoll(); T = base; rec.elPoll();
    }
    await rec.tick();
  }
  rec.mark('final');
  rec.stop();
  console.log(JSON.stringify(rec.dump()));
})();
"""


def run_recorder(prelude=None, **params):
    params = dict({"swap": True, "live": True, "echo": True, "tap": True, "energy": True, "bytes_always": False}, **params)
    script = (prelude or PRELUDE) + tes.ECHO_RECORDER_JS + SCENARIO
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
    assert all(c["ok"] for c in out["checks"] if c["name"] in RECEIVER), [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
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


# ------------------------------------------------------------------ the echo window follows the SUT's over (PTTRX1 artefact)

T0_WALL = 1_000_000.0                                  # ms epoch of the recorder's zero in these dumps


def tone_dump(tones, release=5500.0):
    """A 500 ms series 0..20 s whose rows listed in `tones` carry a 700 Hz tone and inbound bytes; marks of a hold (press 2000, on_air 2500,
    release 5500: 3.0 s)."""
    rows, b, calls = [], 0, 0
    for i in range(0, 41):
        t = i * 500
        calls += 12
        tone = t in tones
        if tone:
            b += 4000
        loud = dict(rms05=0.17, rms1=0.17, peak=700.0, frac=0.5) if tone else {}
        rows.append(sample(t, [pc(0, b, b // 160, b / 8000.0 * 0.03, calls, **loud)]))
    marks = [{"name": n, "t": t, "wall": 0} for n, t in (("listening", 0.0), ("press", 2000.0), ("on_air", 2500.0), ("release", release), ("ended", release + 100.0))]
    d = dump(rows, marks=marks)
    d["t0_wall"] = T0_WALL
    return d


# PTTRX1: the SUT ended a 3 s hold's over after 1 s (2.6 .. 3.6 s) and replayed it 1 s later (4.6 .. 5.6 s), BEFORE the tester released at 5.5 s
CUT = {"live": {3000, 3500, 4000}, "echo": {5000, 5500, 6000}}
CUT_SUT = {"end_wall_s": (T0_WALL + 3600.0) / 1000.0, "start_wall_s": (T0_WALL + 2600.0) / 1000.0, "duration_s": 1.0,
           "start_source": "state.start_ms"}


def test_a_cut_short_over_is_found_and_its_echo_is_judged_in_the_suts_window():
    d = tone_dump(CUT["live"] | CUT["echo"], release=6600.0)                       # held 4.1 s; the echo came and went before the release
    # anchored to the tester's release (the old window) the echo is missed: that was the timing artefact
    assert tes.judge_window(tes.series(d), 6600.0, 6600.0 + 14000.0, expect_hz=700.0)["stage"] == "no_inbound_bytes"
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium", sut=CUT_SUT)
    got = {c["name"]: c for c in out["checks"]}
    assert got["talker_hears_echo"]["ok"] and got["listener_hears_live"]["ok"] and got["listener_hears_echo"]["ok"], \
        [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
    assert out["windows"]["talker_echo"] == {"anchor": "state", "from_ms": 3600.0, "to_ms": 7600.0}
    assert out["windows"]["listener_echo"]["from_ms"] == 4200.0 and out["windows"]["listener_live"]["to_ms"] == 4000.0
    assert out["windows"]["tester_held_s"] == 4.1 and out["windows"]["echo_duration_s"] == 1.0
    assert out["windows"]["echo_duration_source"] == "state"
    cut = got["over_not_cut_short"]
    assert not cut["ok"] and cut["kind"] == tes.PRODUCT and cut["stage"] == "over_cut_short"
    assert "after 1.00 s while Talk was held 4.10 s" in cut["detail"] and "PTTRX1" in cut["detail"]
    lines = tes.failure_lines(out["checks"], out["diagnosis"])
    assert lines[0].startswith("PRODUCT CHECK over_not_cut_short stopped at stage over_cut_short")
    assert "ended the over much earlier than the release" in out["diagnosis"]


def test_an_over_of_the_held_length_passes_over_not_cut_short():
    d = tone_dump({3000, 3500, 4000, 4500, 5000, 5500, 7000, 7500, 8000, 8500, 9000, 9500})
    sut = {"end_wall_s": (T0_WALL + 5600.0) / 1000.0, "start_wall_s": (T0_WALL + 2600.0) / 1000.0, "duration_s": 3.0, "start_source": "x"}
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium", sut=sut)
    got = {c["name"]: c for c in out["checks"]}
    assert got["over_not_cut_short"]["ok"] and got["over_not_cut_short"]["stage"] == "ok"
    assert all(c["ok"] for c in out["checks"]), [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
    assert out["windows"]["listener_echo"]["from_ms"] == 6200.0 and out["windows"]["listener_echo"]["to_ms"] == 11600.0


def test_the_first_packet_fallback_anchors_the_window_to_the_echo():
    d = tone_dump(CUT["live"] | CUT["echo"])
    rows = tes.series(d)
    assert tes.first_echo_start(rows, 2500.0) == 5000.0
    assert tes.first_echo_start(rows[:10], 2500.0) is None                       # no second burst yet
    assert tes.burst_seconds(rows, 2500.0) == 1.5                                # live burst: the sample before the first growth .. the last growth
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    assert out["windows"]["talker_echo"]["anchor"] == "first_packet" and out["windows"]["talker_echo"]["from_ms"] == 4500.0
    assert out["windows"]["echo_duration_source"] == "live_burst"
    got = {c["name"]: c for c in out["checks"]}
    assert got["talker_hears_echo"]["ok"] and got["listener_hears_echo"]["ok"]
    assert got["over_not_cut_short"]["stage"] == "not_observed" and got["over_not_cut_short"]["ok"]


def test_a_real_missing_echo_still_fails_in_the_suts_window():
    d = tone_dump(CUT["live"])
    out = tes.analyse_run({"talker": tone_dump(set()), "listener": d}, "chromium", sut=CUT_SUT)
    got = {c["name"]: c for c in out["checks"]}
    assert got["listener_hears_live"]["ok"]
    assert got["talker_hears_echo"]["stage"] == "no_inbound_bytes" and got["listener_hears_echo"]["stage"] == "no_inbound_bytes"


def test_min_loud_and_page_clock_conversion():
    assert tes.min_loud_for(1.0) == 1 and tes.min_loud_for(3.0) == tes.MIN_LOUD and tes.min_loud_for(None) == tes.MIN_LOUD
    assert tes.page_ms({"t0_wall": 1000.0}, 2.5) == 1500.0 and tes.page_ms({}, 2.5) is None and tes.page_ms({"t0_wall": 1.0}, None) is None


def polls(over_on, ends, start_key=None, until=8.0, base_end=100000):
    """Public-state polls every 0.5 s from wall 1000 s: `over_on` = [(from, to)] wall seconds with the module on air, `ends` = [(wall s the
    end becomes visible, end_ms)]; the server clock is the tester's (skew 0)."""
    out, w = [], 1000.0
    while w < 1000.0 + until:
        on = next(((a, b) for a, b in over_on if a <= w < b), None)
        end = base_end
        for seen, ms in ends:
            if w >= seen:
                end = ms
        onair = None
        if on:
            onair = {"callsign": "N0BRU"}
            if start_key:
                onair[start_key] = on[0] * 1000.0
        out.append((w, {"now_ms": w * 1000.0, "rooms": [{"room": "D"}, {"room": "E", "onair": onair, "last": {"end_ms": end}}]}))
        w += 0.5
    return out


def test_over_tracker_reads_the_end_and_a_lower_bound_of_the_start():
    tr = tes.OverTracker()
    for w, st in polls([(1002.5, 1003.6), (1004.6, 1005.6)], [(1003.6, 1003600.0), (1005.6, 1005600.0)]):
        tr.observe(w, st)
    assert [o["end_ms"] for o in tr.overs] == [1003600.0, 1005600.0]
    s = tr.summary(1002.0, 1005.0)
    assert s["end_wall_s"] == 1003.6 and s["start_source"] == "first poll on air less one interval"
    assert s["start_wall_s"] == 1002.0 and s["duration_s"] == 1.6                    # an upper bound of the true 1.1 s: never invented as short
    assert s["all_ends_wall_s"] == [1003.6, 1005.6] and tr.entries[0]["entry"] == {"room": "E", "onair": None, "last": {"end_ms": 100000}}


def test_over_tracker_prefers_a_start_field_and_ignores_the_baseline_over():
    tr = tes.OverTracker()
    for w, st in polls([(1002.5, 1003.6)], [(1003.6, 1003600.0)], start_key="start_ms"):
        tr.observe(w, st)
    s = tr.summary(1002.0, 1005.0)
    assert s["start_source"] == "state.start_ms" and s["start_wall_s"] == 1002.5 and s["duration_s"] == 1.1
    assert tr.overs[0]["end_ms"] != 100000                                            # the first poll's end is the baseline, not an over


def test_over_tracker_does_not_take_the_echo_as_the_over():
    tr = tes.OverTracker()
    for w, st in polls([(1004.6, 1005.6)], [(1005.6, 1005600.0)]):                   # the original end was never seen (polls missed it)
        tr.observe(w, st)
    assert len(tr.overs) == 1
    assert tr.summary(1002.0, 1003.0) is None                                         # its end is 2.6 s after the release: that is the echo's
    assert tes.OverTracker().summary(0.0, 1.0) is None



def test_relay_switch_parses():
    assert tes.relay_forced({"DVT_TALK_ECHO_RELAY": "1"}) and tes.relay_forced({"DVT_TALK_ECHO_RELAY": "on"})
    assert not tes.relay_forced({}) and not tes.relay_forced({"DVT_TALK_ECHO_RELAY": "0"})
    assert "iceTransportPolicy: 'relay'" in tes.RELAY_JS


# ------------------------------------------------------------------ the page's audio element (what the user hears)

ELEMENT_CHECKS = ("talker_page_audio_element_hears_echo", "listener_page_audio_element_hears_live", "listener_page_audio_element_hears_echo")


def element_stages(out):
    return {c["name"]: c["stage"] for c in out["checks"] if c["name"] in ELEMENT_CHECKS}


def swapped_dump(element_for):
    """healthy_rows(swap=True) with the audio element supplied by element_for(t, pc0, pc1) from the swap (2000 ms) on."""
    rows = healthy_rows(swap=True)
    for r in rows:
        if r["t"] >= 2000:
            r["el"] = element_for(r["t"], r["pcs"][0], r["pcs"][1])
    return dump(rows, pcs=[{"id": 0, "created_at": 100.0, "closed_at": 2000.0}, {"id": 1, "created_at": 2010.0, "closed_at": None}])


def test_a_page_that_plays_the_closed_listen_stream_fails_the_element_check_while_the_receiver_hears():
    # production: the Talk press closes the Listen session and offers a talk session; the audio element keeps the OLD stream (track ended)
    d = swapped_dump(lambda t, p0, p1: element(t, {"id": 0}, rms=0.0, ready="ended"))
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    got = {c["name"]: c for c in out["checks"]}
    assert got["talker_hears_echo"]["ok"] and got["talker_hears_echo"]["kind"] == tes.EVIDENCE            # the receiver-level evidence row
    el = got["talker_page_audio_element_hears_echo"]
    assert not el["ok"] and el["kind"] == tes.PRODUCT and el["stage"] == "element_on_closed_stream"
    assert "the page plays the closed listen stream (track ended) while the talk session receives the echo" in el["detail"]
    assert "pc1(talk, live) +32000 bytes +200 packets" in el["detail"] and "audio element analyser: best rms 0.0000" in el["detail"], el["detail"]
    assert out["diagnosis"].startswith("talker_page_audio_element_hears_echo: the page plays the closed listen stream (track ended)")
    lines = tes.failure_lines(out["checks"], out["diagnosis"])
    assert any(x.startswith("PRODUCT CHECK talker_page_audio_element_hears_echo stopped at stage element_on_closed_stream") for x in lines)
    assert not any("talker_hears_echo stopped" in x for x in lines)                                         # the receiver is fine


def test_the_other_element_stages_are_told_apart():
    def stage(**kw):
        d = swapped_dump(lambda t, p0, p1: element(t, p1, **kw))
        return element_stages(tes.analyse_run({"talker": d, "listener": d}, "chromium"))["talker_page_audio_element_hears_echo"]
    assert stage() == "ok"
    assert stage(rms=0.0) == "element_silent_on_live_stream"
    assert stage(rms=0.0, paused=True) == "element_paused_or_muted"
    assert stage(rms=0.0, tracks=[]) == "element_stream_without_audio_track"
    assert stage(rms=0.0, tracks=[{"id": "x", "kind": "audio", "ready": "live", "enabled": True, "muted": False, "pc": None}]) == "element_stream_foreign"
    assert stage(rms=0.0, ctx="suspended") == "not_observable"
    d = swapped_dump(lambda t, p0, p1: dict(element(t, p1, rms=0.0), stream=None, tracks=[]))
    assert element_stages(tes.analyse_run({"talker": d, "listener": d}, "chromium"))["talker_page_audio_element_hears_echo"] == "element_without_stream"
    d = swapped_dump(lambda t, p0, p1: dict(element(t, p1, rms=0.0), present=False))
    assert element_stages(tes.analyse_run({"talker": d, "listener": d}, "chromium"))["talker_page_audio_element_hears_echo"] == "no_audio_element"
    d = swapped_dump(lambda t, p0, p1: None)
    assert element_stages(tes.analyse_run({"talker": d, "listener": d}, "chromium"))["talker_page_audio_element_hears_echo"] == "no_element_data"


def test_element_timing_and_the_session_order():
    d = dump(healthy_rows(), marks=MARKS)
    d["t0_wall"] = T0_WALL
    d["element"] = {"assignments": [
        {"t": 100.0, "via": "setter", "stream": "s0", "tracks_at": 1, "first_audio_track_t": 100.0, "first_unmute_t": 100.0, "ev": []},
        {"t": 2100.0, "via": "setter", "stream": "s1", "tracks_at": 0, "first_audio_track_t": 2600.0, "first_unmute_t": 3100.0,
         "ev": [{"t": 2600.0, "what": "addtrack", "track": "t1"}]}]}
    answer = tes.talk_offer_answer_ms(d, [(T0_WALL + 300.0) / 1000.0, (T0_WALL + 2050.0) / 1000.0, (T0_WALL + 9000.0) / 1000.0])
    assert answer == 2050.0
    rows = tes.element_timing(d, answer)
    assert rows[1]["rel_press_ms"] == 100.0 and rows[1]["rel_offer_answer_ms"] == 50.0
    assert rows[1]["audio_track_after_assignment_ms"] == 500.0 and rows[1]["unmute_after_assignment_ms"] == 1000.0
    assert rows[1]["audio_track_rel_press_ms"] == 600.0 and rows[1]["unmute_rel_offer_answer_ms"] == 1050.0
    assert rows[0]["audio_track_after_assignment_ms"] == 0.0
    assert tes.timing_lines(rows)[1] == ("audio element srcObject s1 (setter): assigned +100 ms from the press, +50 ms from the offer answer; "
                                         "audio track +500 ms after the assignment, unmute +1000 ms after it")
    assert tes.talk_offer_answer_ms(d, []) is None and tes.element_timing({}) == []
    table = [{"method": "POST", "path": "/api/v1/rtc/offer", "t_s": 1.0, "response_s": 1.2, "status": 200, "body": {"sdp": "x"}},
             {"method": "POST", "path": "/api/v1/rtc/close", "t_s": 5.05, "response_s": None, "status": None},
             {"method": "POST", "path": "/api/v1/rtc/offer", "t_s": 5.2, "response_s": 5.4, "status": 200, "body": {"sess_id": "S"}},
             {"method": "POST", "path": "/api/v1/rtc/alive", "t_s": 5.1, "response_s": 5.2, "status": 204},
             {"method": "POST", "path": "/api/v1/rtc/close", "t_s": 40.0, "response_s": None, "status": None}]
    order = tes.session_order(table, {"listening": 100.0, "press": 105.0})
    assert order["order"] == "close -> offer" and [r["rel_press_s"] for r in order["rows"]] == [0.05, 0.2]
    assert order["rows"][1]["sess_id_in_body"] is True and tes.session_order(table, {"listening": 1.0}) is None


@needs_node
def test_recorder_in_node_follows_the_element_when_the_page_rebinds():
    d = run_recorder(element="rebind")
    assert d["errors"] == []
    a = d["element"]["assignments"]
    assert [x["via"] for x in a] == ["setter", "setter"] and [x["tracks_at"] for x in a] == [1, 1]
    assert [[t["pc"] for t in e["tracks"]] for e in d["element"]["log"]] == [[0], [1]]
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    assert all(s == "ok" for s in element_stages(out).values()), [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
    row = next(s for s in d["samples"] if s["t"] == 8500.0)["el"]
    assert row["an_rms"] > 0.1 and row["attached"] and [t["pc"] for t in row["tracks"]] == [1]


@needs_node
def test_recorder_in_node_finds_the_stale_stream_after_the_swap():
    d = run_recorder(element="stale")
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    got = {c["name"]: c for c in out["checks"]}
    assert got["talker_hears_echo"]["ok"]                                                    # the talk session receives and decodes
    el = got["talker_page_audio_element_hears_echo"]
    assert el["stage"] == "element_on_closed_stream" and "(track ended)" in el["detail"], el["detail"]
    row = next(s for s in d["samples"] if s["t"] == 8500.0)["el"]
    assert row["an_rms"] == 0.0 and [(t["ready"], t["pc"]) for t in row["tracks"]] == [("ended", 0)]


@needs_node
def test_recorder_in_node_reattaches_when_the_track_is_added_to_the_same_stream_later():
    d = run_recorder(element="late_track")
    assert d["errors"] == []
    a = d["element"]["assignments"][1]
    assert a["tracks_at"] == 0 and a["first_audio_track_t"] == 2500.0 and a["first_unmute_t"] == 3000.0
    assert [e["what"] for e in a["ev"]] == ["addtrack", "unmute"]
    timing = tes.element_timing(d)[1]
    assert timing["audio_track_after_assignment_ms"] == 500.0 and timing["unmute_after_assignment_ms"] == 1000.0
    assert timing["rel_press_ms"] == 0.0 and timing["audio_track_rel_press_ms"] == 500.0
    # the analyser was attached again on 'addtrack' and hears the echo (an attach to the empty stream threw and was recorded, then recovered)
    out = tes.analyse_run({"talker": d, "listener": d}, "chromium")
    assert all(s == "ok" for s in element_stages(out).values()), [(c["name"], c["stage"], c["detail"]) for c in out["checks"]]
    assert len(d["element"]["log"]) >= 3


def test_webrtc_max_tx_snippet_merges_into_the_web_edge_table():
    import tomllib
    from dvtest import sut_config
    root = Path(__file__).resolve().parents[2] / "rig" / "config" / "dv-router"
    template = 'schema = 1\n[node]\ncallsign = "XLX999"\nbind_ip = "<OWN_IP>"\nrooms = "A-Z"\n'
    text = (root / "webrtc-max-tx-1.toml").read_text(encoding="utf-8")
    text.encode("ascii")
    assert text.startswith("# SPDX-License-Identifier: MIT")
    out = sut_config.render(template, "172.30.0", extras=[(root / "web.toml").read_text(encoding="utf-8"), text])
    edge = tomllib.loads(out)["edges"]["webrtc"]
    assert edge["max_tx"] == 1 and edge["enable"] is True and edge["public_ips"] == ["172.30.0.11"]


def test_status_timeline_offsets_and_the_record_line():
    d = {"marks": [{"name": "press", "t": 2000.0}, {"name": "release", "t": 5500.0}],
         "talk_status_timeline": [{"t": 0.5, "text": "Ready"}, {"t": 2003.0, "text": "Requesting"},
                                  {"t": 2010.0, "text": "Pending - queued / waiting for the floor"}, {"t": 2044.0, "text": "On air"},
                                  {"t": 5502.0, "text": "Ended: Button released."}]}
    rows = tes.status_timeline(d, "talk_status_timeline")
    assert [r["text"] for r in rows] == ["Ready", "Requesting", "Pending - queued / waiting for the floor", "On air", "Ended: Button released."]
    assert rows[2]["rel_press_ms"] == 10.0 and rows[2]["held_ms"] == 34.0 and rows[3]["held_ms"] == 3458.0 and rows[4]["held_ms"] is None
    assert rows[4]["rel_release_ms"] == 2.0 and rows[0]["rel_press_ms"] == -1999.5
    line = tes.status_timeline_line(rows)
    assert "'Pending - queued / waiting for the floor' @ +10 ms from the press (-3490 from the release), held 34 ms" in line
    assert line.endswith("'Ended: Button released.' @ +3502 ms from the press (+2 from the release)")
    assert tes.status_timeline({}, "x") == [] and tes.status_timeline_line([]) == "no change recorded"
    no_marks = tes.status_timeline({"listen_status_timeline": [{"t": 1.0, "text": None}]}, "listen_status_timeline")
    assert no_marks[0]["rel_press_ms"] is None and "None @ ? ms from the press (? from the release)" in tes.status_timeline_line(no_marks)


@needs_node
def test_recorder_in_node_logs_every_status_change_even_a_34_ms_transient():
    d = run_recorder(pending=True)
    assert d["errors"] == []
    talk = tes.status_timeline(d, "talk_status_timeline")
    texts = [r["text"] for r in talk]
    assert texts == ["Ready", "Pending - queued / waiting for the floor", "On air", "Ended: Button released."], texts
    pending = talk[1]
    assert pending["rel_press_ms"] == 0.0 and pending["held_ms"] == 34.0 and talk[2]["rel_press_ms"] == 34.0
    assert talk[3]["rel_release_ms"] == 0.0
    listen = tes.status_timeline(d, "listen_status_timeline")
    assert [r["text"] for r in listen] == ["Listening on module E", "Idle"] and listen[1]["t_ms"] == 9000.0


@needs_node
def test_recorder_in_node_follows_a_status_element_the_page_replaces_on_every_render():
    d = run_recorder(pending=True, replace_el=True)
    assert d["errors"] == []
    talk = tes.status_timeline(d, "talk_status_timeline")
    # "On air" appears twice: the page renders it again at 2.5 s with a NEW node and the same text, which is recorded too
    assert [r["text"] for r in talk] == ["Ready", "Pending - queued / waiting for the floor", "On air", "On air", "Ended: Button released."]
    assert [r["el_changed"] for r in talk] == [False, True, True, True, True]           # a new node each time, its identity is recorded
    assert len({r["el"] for r in talk}) == 5 and all(r["matches"] == 1 for r in talk)
    assert talk[1]["held_ms"] == 34.0 and talk[2]["rel_press_ms"] == 34.0
    assert "[node replaced]" in tes.status_timeline_line(talk)
    listen = tes.status_timeline(d, "listen_status_timeline")
    assert [r["text"] for r in listen] == ["Listening on module E", "Idle"] and listen[1]["el_changed"]
    # the same page without replacement keeps one node
    same = tes.status_timeline(run_recorder(pending=True), "talk_status_timeline")
    assert len({r["el"] for r in same}) == 1 and not any(r["el_changed"] for r in same)


@needs_node
def test_recorder_observes_the_document_even_when_the_root_element_does_not_exist_yet():
    # an init script runs before document.documentElement exists: observing it threw and left no observer (one 'Ready' row only)
    prelude = PRELUDE.replace("documentElement: {}", "documentElement: null")
    assert "documentElement: null" in prelude
    d = run_recorder(prelude=prelude, pending=True)
    assert d["errors"] == [] and d["observer"]["target"] == "document"
    assert [r["text"] for r in tes.status_timeline(d, "talk_status_timeline")][:3] == ["Ready", "Pending - queued / waiting for the floor", "On air"]
    assert ".observe(document," in tes.ECHO_RECORDER_JS and "document.documentElement" not in tes.ECHO_RECORDER_JS.split("rec.observer = ")[1].split("rec.fastTimer")[0]
    assert "rec.fast(30000)" in tes.ECHO_RECORDER_JS and "rec.fast(15000)" in tes.ECHO_RECORDER_JS
