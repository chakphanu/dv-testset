# SPDX-License-Identifier: MIT
"""Helpers of cases/test_web_talk_echo_return.py: the in-page recorder (ECHO_RECORDER_JS), the request audit of /rtc/*, and the PURE
judges that read what the recorder saw (unit tests: cases/unit/test_talk_echo_support.py). Not a test module.

The question (a product finding on dv-router:wip-97c669d): a signed-in user listens on the echo module E, holds Talk 3 s, releases; the
echo replays, but the talker's page hears nothing. Does the media stop at the edge, or in the page? The recorder keeps, every 500 ms and
for EVERY RTCPeerConnection the page ever creates (the Talk press may swap the Listen session for a new one):

  states      connectionState, iceConnectionState, signalingState, createdAt, closedAt (page clock, ms since the recorder started)
  getStats    inbound-rtp audio: bytesReceived, packetsReceived, packetsLost, audioLevel, totalAudioEnergy, totalSamplesReceived;
              outbound-rtp audio: bytesSent, packetsSent; the selected candidate pair's bytes
  analyser    ITS OWN tap per RTCPeerConnection (a ScriptProcessorNode on that pc's receiver track, so a swapped session is tapped too:
              the shared probe of dvtest/browser.py taps only the pc that was last when tap.start() ran): rms of the last 0.5 s and 1 s,
              the Goertzel scan (the algorithm of dvtest/browser.py RTC_PROBE_JS) of the last 1 s, and how many audio callbacks the tap got

Verdict stages (judge_window): no_inbound_bytes (the media stops before the page), tap_dead_* (the analyser never got a callback),
decoder_silent / analyser_silent_decoder_audible (bytes arrive but the page does not play them), wrong_frequency, ok.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from dvtest.tools.prod_talk import MIN_TONE_FRACTION, RMS_FLOOR, TONE_HZ, TONE_TOL_HZ, judge_audio

ROOM = "E"
HOLD_S = 3.0                    # the Talk hold
PRE_S = 2.0                     # sampled before the press
TAIL_S = 14.0                   # sampled after the release (the echo replays 1 s after the end and lasts about as long as the over)
SAMPLE_MS = 500
LIVE_GRACE_MS = 400.0           # the live over may still be heard this long after the release
ECHO_FROM_MS = 1000.0           # the echo replays delay_ms (1000) after the end of the over: nothing of it before this
ECHO_SETTLE_MS = 500.0          # frequency windows of the echo start this much later (a 1 s analyser window must not hold live audio)
MIN_LOUD = 2                    # 0.5 s samples above the floor (one second of sound)
MIN_PACKETS = 50                # 1 s of PCMU 20 ms packets: the weakest evidence of audio when nothing else can be read
ENERGY_FLOOR = RMS_FLOOR * RMS_FLOOR * 1.0   # totalAudioEnergy (sum of squares x seconds) of 1 s of sound at the rms floor
EXPECT_HZ = {"chromium": TONE_HZ}            # Firefox: its own fake source (about 1000 Hz), WebKit: its mock device (500 Hz): recorded only
PRODUCT, EVIDENCE = "product", "evidence"

# The in-page recorder. Installed with context.add_init_script AFTER the shared RTC probe (it wraps the probe's wrapper, so the probe keeps
# working and probe_index tells which probe entry a recorder entry is). Everything is guarded: a failure is kept in errors[], never thrown
# into the page.
ECHO_RECORDER_JS = r"""
(() => {
  if (window.__dvtEcho) return;
  const Prev = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  const t0 = performance.now();
  const rec = { t0_wall: Date.now(), pcs: [], samples: [], marks: [], errors: [], ctx: null, rate: null, timer: null, busy: false,
                skipped: 0, interval_ms: 500, max_samples: 800, missing: false };
  window.__dvtEcho = rec;
  const now = () => performance.now() - t0;
  const rd = (v, n) => { const k = Math.pow(10, n); return Math.round(v * k) / k; };
  const err = (where, e) => { if (rec.errors.length < 20) rec.errors.push(where + ': ' + String((e && e.message) || e).slice(0, 120)); };
  // navigator.connection 'change' events (the page's ICE1 path listens to them): the initial values and every change, page clock
  rec.connection = { supported: false, initial: null, events: [] };
  try {
    const nc = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (nc) {
      const snap = () => ({ effectiveType: nc.effectiveType === undefined ? null : nc.effectiveType, rtt: nc.rtt === undefined ? null : nc.rtt,
                            downlink: nc.downlink === undefined ? null : nc.downlink, saveData: nc.saveData === undefined ? null : nc.saveData,
                            type: nc.type === undefined ? null : nc.type });
      rec.connection.supported = true;
      rec.connection.initial = snap();
      nc.addEventListener('change', () => {
        const s = snap();
        s.t = rd(now(), 1); s.wall = Date.now();
        if (rec.connection.events.length < 100) rec.connection.events.push(s);
      });
    }
  } catch (e) { err('connection', e); }
  // the status lines of the Listen / Talk card with their page-clock time, kept when one of them (or the card's data-state) changes:
  // "Ended: Button released." appearing BEFORE the user released is the page's interrupted() path
  rec.ui = [];
  const uiNote = () => {
    try {
      const talk = document.querySelector('#talk-status');
      const card = document.querySelector("[data-testid='listen-connection']");
      const line = card ? card.querySelector('p[role=status]') : null;
      const r = { talk: talk ? String(talk.textContent).trim() : null, listen: line ? String(line.textContent).trim() : null,
                  state: card ? card.getAttribute('data-state') : null };
      const last = rec.ui[rec.ui.length - 1];
      if (!last || last.talk !== r.talk || last.listen !== r.listen || last.state !== r.state) {
        r.t = rd(now(), 1); r.wall = Date.now();
        if (rec.ui.length < 300) rec.ui.push(r);
      }
    } catch (e) { err('ui', e); }
  };
  try {
    new MutationObserver(uiNote).observe(document.documentElement,
      { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['data-state'] });
  } catch (e) { err('observer', e); }
  if (!Prev) { rec.missing = true; return; }

  const rmsOf = (x) => { let e = 0; for (let i = 0; i < x.length; i++) e += x[i] * x[i]; return x.length ? Math.sqrt(e / x.length) : 0; };
  const windowOf = (r, seconds) => {
    const want = Math.min(Math.floor(seconds * rec.rate), r.have);
    const out = new Float32Array(want);
    let at = want;
    for (let i = r.buf.length - 1; i >= 0 && at > 0; i--) {
      const b = r.buf[i];
      const take = Math.min(b.length, at);
      out.set(b.subarray(b.length - take), at - take);
      at -= take;
    }
    return out;
  };
  const goertzel = (x, win, wsum, rate, f) => {
    const w = 2 * Math.PI * f / rate, c = 2 * Math.cos(w);
    let s1 = 0, s2 = 0;
    for (let i = 0; i < x.length; i++) { const s0 = x[i] * win[i] + c * s1 - s2; s2 = s1; s1 = s0; }
    return 2 * Math.sqrt(Math.max(s1 * s1 + s2 * s2 - c * s1 * s2, 0)) / wsum;
  };
  const SCAN_MIN_RMS = 0.004;
  // the same scan as RTC_PROBE_JS tap.analyse (Hann window, 200 .. 3600 Hz in 10 Hz steps), on the last 1 s; rms also of the last 0.5 s
  const analyse = (r) => {
    const out = { calls: r.calls, have_s: rd(r.have / rec.rate, 2), rms05: 0, rms1: 0, peak_hz: 0, fraction: 0 };
    out.rms05 = rd(rmsOf(windowOf(r, 0.5)), 5);
    const x = windowOf(r, 1.0);
    const level = rmsOf(x);
    out.rms1 = rd(level, 5);
    if (level >= SCAN_MIN_RMS && x.length) {
      const n = x.length;
      const win = new Float64Array(n);
      let wsum = 0;
      for (let i = 0; i < n; i++) { win[i] = 0.5 - 0.5 * Math.cos(2 * Math.PI * i / n); wsum += win[i]; }
      let bf = 200, ba = -1;
      for (let f = 200; f <= 3600 + 1e-9; f += 10) {
        const a = goertzel(x, win, wsum, rec.rate, f);
        if (a > ba) { ba = a; bf = f; }
      }
      out.peak_hz = bf;
      out.fraction = rd((ba * ba / 2) / (level * level), 4);
    }
    return out;
  };

  const tapTrack = (r, track) => {
    r.audioTracks += 1;
    if (r.tapped) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) { r.tap_error = 'no AudioContext'; return; }
      if (!rec.ctx) {
        rec.ctx = new AC();
        rec.rate = rec.ctx.sampleRate;
        try { const p = rec.ctx.resume(); if (p && p.catch) p.catch(() => {}); } catch (e) { err('resume', e); }
      }
      const stream = new MediaStream([track]);
      // a remote stream only flows into Web Audio in Chromium when a media element has it too (as RTC_PROBE_JS)
      const el = document.createElement('audio');
      el.muted = true;
      el.srcObject = stream;
      try { const p = el.play(); if (p && p.catch) p.catch((e) => { r.play_error = String(e).slice(0, 80); }); } catch (e) { r.play_error = String(e).slice(0, 80); }
      const src = rec.ctx.createMediaStreamSource(stream);
      const proc = rec.ctx.createScriptProcessor(4096, 1, 1);
      proc.onaudioprocess = (e) => {
        const x = e.inputBuffer.getChannelData(0);
        r.buf.push(new Float32Array(x));
        r.have += x.length;
        r.calls += 1;
        while (r.buf.length > 1 && r.have - r.buf[0].length >= rec.rate * 4) { r.have -= r.buf[0].length; r.buf.shift(); }
      };
      src.connect(proc);
      proc.connect(rec.ctx.destination);
      r.tapped = true; r.src = src; r.proc = proc; r.el = el; r.tap_at = now();
    } catch (e) { r.tap_error = String(e).slice(0, 100); err('tap', e); }
  };

  const withTimeout = (promise, ms, what) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(what + ' timeout')), ms);
    promise.then((v) => { clearTimeout(timer); resolve(v); }, (e) => { clearTimeout(timer); reject(e); });
  });
  const statsOf = async (pc) => {
    const rep = await withTimeout(pc.getStats(), 400, 'getStats');
    const inb = { n: 0, bytes: 0, packets: 0, lost: 0, energy: null, samples: 0, level: 0, jitter: null, concealed: 0 };
    const outb = { n: 0, bytes: 0, packets: 0 };
    let transport = null;
    const byId = {};
    rep.forEach((v) => { byId[v.id] = v; });
    rep.forEach((v) => {
      const kind = v.kind || v.mediaType;
      if (v.type === 'inbound-rtp' && kind === 'audio') {
        inb.n += 1;
        inb.bytes += v.bytesReceived || 0;
        inb.packets += v.packetsReceived || 0;
        inb.lost += v.packetsLost || 0;
        if (typeof v.totalAudioEnergy === 'number') inb.energy = (inb.energy || 0) + v.totalAudioEnergy;
        inb.samples += v.totalSamplesReceived || 0;
        inb.concealed += v.concealedSamples || 0;
        if (typeof v.audioLevel === 'number') inb.level = Math.max(inb.level, v.audioLevel);
        if (typeof v.jitter === 'number') inb.jitter = v.jitter;
      } else if (v.type === 'outbound-rtp' && kind === 'audio') {
        outb.n += 1;
        outb.bytes += v.bytesSent || 0;
        outb.packets += v.packetsSent || 0;
      } else if (v.type === 'transport') {
        transport = v;
      }
    });
    if (inb.energy !== null) inb.energy = rd(inb.energy, 6);
    let pair = null;
    const cp = transport && byId[transport.selectedCandidatePairId];
    if (cp) pair = { state: cp.state, bytes_in: cp.bytesReceived || 0, bytes_out: cp.bytesSent || 0, packets_in: cp.packetsReceived === undefined ? null : cp.packetsReceived,
                     packets_out: cp.packetsSent === undefined ? null : cp.packetsSent };
    return { inb: inb.n ? inb : null, outb: outb.n ? outb : null, pair: pair };
  };
  const keepStats = (r, s) => { r.lastIn = s.inb; r.lastOut = s.outb; r.lastPair = s.pair; };

  function Wrapped(...args) {
    const pc = new Prev(...args);
    const r = { id: rec.pcs.length, pc: pc, createdAt: now(), closedAt: null, audioTracks: 0, tapped: false, buf: [], have: 0, calls: 0,
                lastIn: null, lastOut: null, lastPair: null };
    rec.pcs.push(r);
    try {
      const origClose = pc.close.bind(pc);
      pc.close = () => {
        if (r.closedAt === null) {
          r.closedAt = now();
          // a last snapshot, started before the close (best effort: a closed pc may refuse getStats)
          try { statsOf(pc).then((s) => keepStats(r, s), () => {}); } catch (e) { /* ignore */ }
        }
        return origClose();
      };
      pc.addEventListener('connectionstatechange', () => { if (pc.connectionState === 'closed' && r.closedAt === null) r.closedAt = now(); });
      pc.addEventListener('track', (e) => { if (e.track && e.track.kind === 'audio') tapTrack(r, e.track); });
    } catch (e) { err('wrap', e); }
    return pc;
  }
  Wrapped.prototype = Prev.prototype;
  Object.setPrototypeOf(Wrapped, Prev);
  window.RTCPeerConnection = Wrapped;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Wrapped;

  rec.tick = async () => {
    if (rec.busy) { rec.skipped += 1; return null; }
    rec.busy = true;
    try {
      const t = now();
      const row = { t: rd(t, 1), wall: Date.now(), hidden: !!document.hidden, ctx: rec.ctx ? rec.ctx.state : null, pcs: [] };
      if (rec.ctx && rec.ctx.state === 'suspended') { try { const p = rec.ctx.resume(); if (p && p.catch) p.catch(() => {}); } catch (e) { /* needs a gesture */ } }
      for (const r of rec.pcs) {
        const pc = r.pc;
        const p = { id: r.id, conn: pc.connectionState, ice: pc.iceConnectionState, sig: pc.signalingState, calls: r.calls };
        if (p.conn === 'closed' && r.closedAt === null) r.closedAt = t;
        if (r.closedAt !== null) {
          p.stale = true; p.in = r.lastIn; p.out = r.lastOut; p.pair = r.lastPair; p.a = null;
        } else {
          try { keepStats(r, await statsOf(pc)); } catch (e) { p.stats_error = String((e && e.message) || e).slice(0, 80); }
          p.in = r.lastIn; p.out = r.lastOut; p.pair = r.lastPair;
          p.a = r.tapped ? analyse(r) : null;
        }
        row.pcs.push(p);
      }
      if (rec.samples.length < rec.max_samples) rec.samples.push(row);
      return row;
    } catch (e) { err('tick', e); return null; } finally { rec.busy = false; }
  };
  rec.start = (ms) => {
    if (rec.timer) return false;
    rec.interval_ms = ms || 500;
    rec.timer = setInterval(() => { rec.tick(); }, rec.interval_ms);
    rec.tick();
    return true;
  };
  rec.stop = () => { if (rec.timer) { clearInterval(rec.timer); rec.timer = null; } return true; };
  rec.mark = (name) => { const m = { name: name, t: rd(now(), 1), wall: Date.now() }; rec.marks.push(m); return m; };
  // the request audit of the page's own clock (Resource Timing; responseStatus where the engine has it)
  rec.resources = () => {
    const out = [];
    try {
      performance.getEntriesByType('resource').forEach((e) => {
        if (String(e.name).indexOf('/rtc/') < 0) return;
        out.push({ path: new URL(e.name).pathname, t: rd(e.startTime - t0, 1), dur: rd(e.duration, 1),
                   status: e.responseStatus === undefined ? null : e.responseStatus });
      });
    } catch (e) { err('resources', e); }
    return out;
  };
  rec.dump = () => JSON.parse(JSON.stringify({
    t0_wall: rec.t0_wall, rate: rec.rate, ctx_state: rec.ctx ? rec.ctx.state : null, missing: rec.missing, errors: rec.errors,
    interval_ms: rec.interval_ms, skipped_ticks: rec.skipped,
    pcs: rec.pcs.map((r) => ({
      id: r.id, probe_index: (window.__dvtTap && window.__dvtTap.pcs) ? window.__dvtTap.pcs.indexOf(r.pc) : -1,
      created_at: rd(r.createdAt, 1), closed_at: r.closedAt === null ? null : rd(r.closedAt, 1), audio_tracks: r.audioTracks,
      tapped: r.tapped, tap_at: r.tap_at === undefined ? null : rd(r.tap_at, 1), calls: r.calls,
      tap_error: r.tap_error || null, play_error: r.play_error || null,
      final: { conn: r.pc.connectionState, ice: r.pc.iceConnectionState, sig: r.pc.signalingState } })),
    connection: rec.connection, ui: rec.ui, samples: rec.samples, marks: rec.marks, resources: rec.resources() }));
})();
"""


# ------------------------------------------------------------------------- request audit (Playwright side)

SECRET_KEY = re.compile(r"cookie|csrf|passw|token|secret|authoriz|credential|totp", re.I)


def sanitize_body(text, limit=20000):
    """The JSON body of an offer request as a dict, with every key that looks like a secret (cookie, csrf, password, token, secret,
    authorization, credential, totp) replaced by '<removed>' at any depth and long strings (the SDP) cut at `limit` characters. A body that is
    not a JSON object gives {'raw_length': n}."""
    if text is None:
        return {"raw_length": 0}

    def clean(value):
        if isinstance(value, dict):
            return {k: ("<removed>" if SECRET_KEY.search(str(k)) else clean(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value[:50]]
        if isinstance(value, str) and len(value) > limit:
            return value[:limit] + "...<cut>"
        return value

    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"raw_length": len(text) if hasattr(text, "__len__") else None}
    return clean(data) if isinstance(data, dict) else {"raw_length": len(text)}


def offers_summary(table):
    """One entry per POST /rtc/offer of an audit table (requests_table rows): index, phase, time, status, whether the body carried a sess_id
    (an offer for an EXISTING circuit = an ICE restart or a renegotiation, not a new circuit), the body's keys and the SDP's media lines."""
    out = []
    for r in table:
        if r["method"] != "POST" or not r["path"].endswith("/rtc/offer"):
            continue
        body = r.get("body") or {}
        sdp = body.get("sdp") if isinstance(body.get("sdp"), str) else (body.get("offer", {}).get("sdp") if isinstance(body.get("offer"), dict) else "")
        out.append({"index": len(out) + 1, "phase": r["phase"], "t_s": r["t_s"], "response_s": r["response_s"], "status": r["status"],
                    "sess_id_in_body": bool(body.get("sess_id")), "keys": sorted(body.keys()),
                    "m_lines": re.findall(r"^m=\S+", sdp or "", re.M), "ice_ufrag_lines": len(re.findall(r"^a=ice-ufrag", sdp or "", re.M))})
    return out


def rtc_path(url):
    """The path of a /rtc/* request (no origin, no query), or None for any other URL."""
    path = urlsplit(url or "").path
    return path if "/rtc/" in path else None


class RtcAudit:
    """Every /rtc/* request of one page with its method, status and wall-clock times (the tester's clock, time.time()). A keepalive fetch
    (the close) gives no response event: its status stays None unless the request failed or finished. No body is read, so no session
    id or SDP is kept."""

    def __init__(self, clock=time.time):
        self.rows = []
        self._by_req = {}
        self._clock = clock

    def attach(self, page):
        page.on("request", self.on_request)
        page.on("response", self.on_response)
        page.on("requestfailed", self.on_failed)
        page.on("requestfinished", self.on_finished)
        return self

    def _row(self, req):
        row = self._by_req.get(req)
        if row is None:
            path = rtc_path(getattr(req, "url", ""))
            if path is None:
                return None
            row = {"method": req.method, "path": path, "wall_request": None, "wall_response": None, "status": None, "failure": None}
            self.rows.append(row)
            self._by_req[req] = row
        return row

    def on_request(self, req):
        row = self._row(req)
        if row is not None and row["wall_request"] is None:
            row["wall_request"] = self._clock()
            if req.method == "POST" and row["path"].endswith("/rtc/offer"):
                try:
                    row["body"] = sanitize_body(req.post_data)
                except Exception as exc:                    # noqa: BLE001 - evidence only
                    row["body"] = {"unreadable": type(exc).__name__}

    def on_response(self, resp):
        row = self._row(resp.request)
        if row is not None:
            row["status"] = resp.status
            row["wall_response"] = self._clock()

    def on_failed(self, req):
        row = self._row(req)
        if row is not None:
            row["failure"] = str(getattr(req, "failure", "") or "")[:80]

    def on_finished(self, req):
        row = self._row(req)
        if row is not None and row["wall_response"] is None:
            row["wall_response"] = self._clock()


def phase_of(wall, phases):
    """The name of the last phase that began at or before `wall` (phases: {name: wall seconds}), else 'before'."""
    best, name = None, "before"
    for key, value in phases.items():
        if value is not None and value <= wall and (best is None or value >= best):
            best, name = value, key
    return name


def requests_table(rows, phases):
    """The audit rows with the phase each request began in, ordered by time; times as seconds after the first phase."""
    base = min([v for v in phases.values() if v is not None] or [0.0])
    out = []
    for r in sorted(rows, key=lambda x: x.get("wall_request") or 0.0):
        w = r.get("wall_request")
        row = {"method": r["method"], "path": r["path"], "status": r["status"], "failure": r["failure"],
               "phase": phase_of(w, phases) if w is not None else "before",
               "t_s": round(w - base, 2) if w is not None else None,
               "response_s": round(r["wall_response"] - base, 2) if r.get("wall_response") is not None else None}
        if "body" in r:
            row["body"] = r["body"]
        out.append(row)
    return out


def requests_summary(table):
    """'POST /api/v1/rtc/offer -> 200, 200' style lines, in first-use order."""
    order, got = [], {}
    for r in table:
        key = "%s %s" % (r["method"], r["path"])
        if key not in got:
            got[key] = []
            order.append(key)
        got[key].append(r["status"] if r["status"] is not None else (r["failure"] or "no response"))
    return ["%s -> %s" % (k, ", ".join(str(s) for s in got[k])) for k in order]


# ------------------------------------------------------------------------- reading a recorder dump

def mark_t(dump, name):
    """Page-clock ms of the first mark `name`, or None."""
    for m in (dump or {}).get("marks") or []:
        if m.get("name") == name:
            return float(m["t"])
    return None


def series(dump):
    """One row per recorder sample, summed over every RTCPeerConnection (a closed one keeps its last counters, so the sums only grow):
    {t, wall, bytes, packets, lost, energy (None when no pc exposes it), calls (audio callbacks of the taps), rms05, rms1, peak_hz,
    fraction (the loudest live analyser), out_bytes, out_packets, pcs_live, pcs_total, ctx}."""
    rows = []
    for s in (dump or {}).get("samples") or []:
        row = {"t": float(s["t"]), "wall": s.get("wall"), "bytes": 0, "packets": 0, "lost": 0, "energy": None, "calls": 0,
               "rms05": 0.0, "rms1": 0.0, "peak_hz": 0.0, "fraction": 0.0, "out_bytes": 0, "out_packets": 0,
               "pcs_live": 0, "pcs_total": 0, "ctx": s.get("ctx"), "hidden": s.get("hidden")}
        for p in s.get("pcs") or []:
            row["pcs_total"] += 1
            row["calls"] += p.get("calls") or 0
            if not p.get("stale") and p.get("conn") != "closed":
                row["pcs_live"] += 1
            i = p.get("in") or {}
            row["bytes"] += i.get("bytes") or 0
            row["packets"] += i.get("packets") or 0
            row["lost"] += i.get("lost") or 0
            if i.get("energy") is not None:
                row["energy"] = (row["energy"] or 0.0) + i["energy"]
            o = p.get("out") or {}
            row["out_bytes"] += o.get("bytes") or 0
            row["out_packets"] += o.get("packets") or 0
            a = p.get("a")
            if a:
                row["rms05"] = max(row["rms05"], a.get("rms05", 0.0))
                if a.get("rms1", 0.0) > row["rms1"]:
                    row["rms1"], row["peak_hz"], row["fraction"] = a.get("rms1", 0.0), a.get("peak_hz", 0.0), a.get("fraction", 0.0)
        rows.append(row)
    return rows


def sampling_facts(rows, interval_ms=SAMPLE_MS):
    """How regular the sampling was: samples, largest gap, hidden samples."""
    gaps = [round(b["t"] - a["t"], 1) for a, b in zip(rows, rows[1:])]
    return {"samples": len(rows), "max_gap_ms": max(gaps) if gaps else None, "interval_ms": interval_ms,
            "hidden_samples": sum(1 for r in rows if r.get("hidden"))}


def new_pcs_between(dump, t_from, t_to):
    """{'created': [ids], 'closed': [ids]} of the RTCPeerConnections created / closed in [t_from, t_to] (page clock ms)."""
    created, closed = [], []
    for p in (dump or {}).get("pcs") or []:
        if p.get("created_at") is not None and t_from <= p["created_at"] <= t_to:
            created.append(p["id"])
        if p.get("closed_at") is not None and t_from <= p["closed_at"] <= t_to:
            closed.append(p["id"])
    return {"created": created, "closed": closed}


def pc_story(dump):
    """Did the page swap sessions? new RTCPeerConnections / closes around the press (press .. on_air + 2 s) and around the release
    (release .. ended + 3 s), plus the total number of pcs."""
    press, on_air = mark_t(dump, "press"), mark_t(dump, "on_air")
    release, ended = mark_t(dump, "release"), mark_t(dump, "ended")
    out = {"pcs_total": len((dump or {}).get("pcs") or []), "at_talk": None, "at_release": None}
    if press is not None:
        out["at_talk"] = new_pcs_between(dump, press - 50.0, (on_air if on_air is not None else press) + 2000.0)
    if release is not None:
        out["at_release"] = new_pcs_between(dump, release - 50.0, (ended if ended is not None else release) + 3000.0)
    out["new_pc_at_talk"] = bool(out["at_talk"] and out["at_talk"]["created"])
    out["new_pc_at_release"] = bool(out["at_release"] and out["at_release"]["created"])
    return out


def _delta(rows_before, last, key):
    if last is None:
        return 0
    a = last.get(key)
    if a is None:
        return None
    b = rows_before.get(key) if rows_before else 0
    return a - (b or 0)


def judge_window(rows, t_from, t_to, expect_hz=None, floor=RMS_FLOOR, min_loud=MIN_LOUD, settle_ms=0.0, energy_floor=ENERGY_FLOOR,
                 min_packets=MIN_PACKETS, tol=TONE_TOL_HZ, min_fraction=MIN_TONE_FRACTION):
    """Judge one page over [t_from, t_to] (page clock ms) of a series(). Returns a dict with `ok`, `stage`, `via`, `detail` and the numbers:
    inbound bytes / packets / audio energy gained in the window, analyser callbacks gained, loud 0.5 s samples, first loud second after
    t_from, and the frequency verdict of prod_talk.judge_audio over the 1 s windows that start `settle_ms` after t_from."""
    inside = [r for r in rows if t_from <= r["t"] <= t_to]
    prior = [r for r in rows if r["t"] < t_from]
    base = prior[-1] if prior else None
    last = inside[-1] if inside else base
    res = {"t_from_ms": round(t_from, 1), "t_to_ms": round(t_to, 1), "samples": len(inside), "ok": False, "stage": "", "via": None, "detail": ""}
    if not inside:
        res.update(stage="no_samples", detail="the recorder took no sample between %.1f s and %.1f s of its clock" % (t_from / 1000.0, t_to / 1000.0))
        return res
    d_bytes, d_packets = _delta(base, last, "bytes"), _delta(base, last, "packets")
    d_calls, d_energy = _delta(base, last, "calls"), _delta(base, last, "energy")
    loud = [r for r in inside if r["rms05"] >= floor]
    windows = [{"t": round((r["t"] - t_from) / 1000.0, 2), "rms": max(r["rms1"], r["rms05"]), "peak_hz": r["peak_hz"], "fraction": r["fraction"]}
               for r in inside if r["t"] >= t_from + settle_ms]
    freq = judge_audio(windows, floor, expect_hz or TONE_HZ, tol, min_fraction)
    res.update(inbound_bytes=d_bytes, inbound_packets=d_packets, audio_energy=None if d_energy is None else round(d_energy, 6),
               tap_callbacks=d_calls, loud_samples=len(loud), best_rms05=round(max(r["rms05"] for r in inside), 4),
               first_loud_s=round((loud[0]["t"] - t_from) / 1000.0, 2) if loud else None,
               peak_hz=freq["peak_hz"], tone_fraction=freq["fraction"], freq_ok=freq["freq_ok"], expect_hz=expect_hz)
    energy_known = d_energy is not None
    energy_audible = energy_known and d_energy >= energy_floor
    tap_alive = (d_calls or 0) > 0
    if (d_bytes or 0) <= 0 and (d_packets or 0) <= 0:
        res.update(stage="no_inbound_bytes", detail="no inbound-rtp bytes or packets in the window on any RTCPeerConnection of the page: "
                   "the media stops before the page (at or before the edge)")
    elif len(loud) >= min_loud:
        if expect_hz is not None and not freq["freq_ok"]:
            res.update(stage="wrong_frequency", via="analyser", detail="audio is heard (rms %.3f) but the dominant frequency is %s Hz with tone "
                       "fraction %s, wanted %g Hz +- %g Hz" % (res["best_rms05"], freq["peak_hz"], freq["fraction"], expect_hz, tol))
        else:
            res.update(ok=True, stage="ok", via="analyser", detail="heard: %d loud 0.5 s samples, best rms %.3f, first %s s into the window, "
                       "dominant %s Hz" % (len(loud), res["best_rms05"], res["first_loud_s"], freq["peak_hz"]))
    elif not tap_alive:
        if energy_audible:
            res.update(ok=True, stage="ok_decoder_only", via="decoder_energy", detail="the analyser got no audio callbacks (AudioContext %s), "
                       "but the decoder's totalAudioEnergy grew by %.4f: audible by getStats only" % (_ctx(rows), d_energy))
        elif energy_known:
            res.update(stage="tap_dead_decoder_silent", detail="bytes arrive (%s) but the analyser got no audio callbacks and the decoder's "
                       "totalAudioEnergy grew by only %.6f: nothing audible" % (d_bytes, d_energy))
        elif (d_packets or 0) >= min_packets:
            res.update(ok=True, stage="ok_packets_only", via="packets", detail="the analyser got no audio callbacks and the engine has no "
                       "totalAudioEnergy: %d packets arrived, loudness not judged" % d_packets)
        else:
            res.update(stage="tap_dead_no_evidence", detail="the analyser got no audio callbacks, no totalAudioEnergy, and only %s packets arrived" % d_packets)
    elif energy_audible:
        res.update(stage="analyser_silent_decoder_audible", detail="bytes arrive (%s), the decoder's totalAudioEnergy grew by %.4f, but the page's "
                   "analyser stays under rms %g (best %.4f): the page does not play what the decoder produced (muted or disabled track, "
                   "or a different receiver)" % (d_bytes, d_energy, floor, res["best_rms05"]))
    else:
        res.update(stage="decoder_silent", detail="bytes arrive (%s bytes, %s packets) but the decoded audio is silent (analyser best rms %.4f, "
                   "audio energy %s): the page/decoder drops it" % (d_bytes, d_packets, res["best_rms05"], res["audio_energy"]))
    return res


def _ctx(rows):
    return next((r["ctx"] for r in reversed(rows) if r.get("ctx")), "?")


def analyse_run(dumps, engine, floor=RMS_FLOOR):
    """Judge a whole run from the two recorder dumps {'talker': dump, 'listener': dump} (either may be None). Returns {'checks': [...],
    'diagnosis': str, 'talker': {...}, 'listener': {...}} where each check is {name, kind (product | evidence), ok, stage, detail} and the
    three AUDIO checks are the product check:
      talker_hears_echo     the talker's own page: audio after the release, within TAIL_S
      listener_hears_live   the second page: audio while Talk is held
      listener_hears_echo   the second page: the echo replay after the release"""
    want = EXPECT_HZ.get(engine)
    out = {"checks": [], "diagnosis": "", "talker": {}, "listener": {}, "expect_hz": want}
    rows = {name: series(d) for name, d in (dumps or {}).items() if d}
    for name, d in (dumps or {}).items():
        if not d:
            continue
        out[name] = {"sampling": sampling_facts(rows[name]), "pcs": pc_story(d), "pc_states": [p.get("final") for p in d.get("pcs") or []],
                     "recorder_errors": d.get("errors") or [], "ctx_state": d.get("ctx_state"), "rate": d.get("rate")}

    def add(name, kind, verdict=None, ok=None, stage="", detail=""):
        if verdict is not None:
            ok, stage, detail = verdict["ok"], verdict["stage"], verdict["detail"]
        out["checks"].append({"name": name, "kind": kind, "ok": bool(ok), "stage": stage, "detail": detail, "verdict": verdict})

    tail_ms = TAIL_S * 1000.0
    tx, rx = dumps.get("talker") if dumps else None, dumps.get("listener") if dumps else None
    if tx is None or mark_t(tx, "release") is None:
        add("talker_hears_echo", PRODUCT, ok=False, stage="no_data", detail="no recorder data of the talker page after the release")
    else:
        rel = mark_t(tx, "release")
        add("talker_hears_echo", PRODUCT, judge_window(rows["talker"], rel, rel + tail_ms, expect_hz=want, floor=floor))
    if rx is None or mark_t(rx, "release") is None or mark_t(rx, "on_air") is None:
        add("listener_hears_live", PRODUCT, ok=False, stage="no_data", detail="no recorder data of the second page during the over")
        add("listener_hears_echo", PRODUCT, ok=False, stage="no_data", detail="no recorder data of the second page after the release")
    else:
        air, rel = mark_t(rx, "on_air"), mark_t(rx, "release")
        # frequency of the live over is recorded, not asserted (the fake microphone of Firefox and WebKit is not 700 Hz anyway)
        live = judge_window(rows["listener"], air, rel + LIVE_GRACE_MS, expect_hz=None, floor=floor)
        add("listener_hears_live", PRODUCT, live)
        add("listener_hears_echo", PRODUCT, judge_window(rows["listener"], rel + ECHO_FROM_MS, rel + tail_ms, expect_hz=want, floor=floor,
                                                         settle_ms=ECHO_SETTLE_MS))
    out["diagnosis"] = diagnose({c["name"]: c for c in out["checks"]})
    return out


def diagnose(checks):
    """One line on WHERE the media stopped, from the three audio checks (dict by name); '' when there is nothing to say."""
    get = lambda n: (checks.get(n) or {}).get("stage", "")                       # noqa: E731
    t, live, echo = get("talker_hears_echo"), get("listener_hears_live"), get("listener_hears_echo")
    silent_page = ("decoder_silent", "analyser_silent_decoder_audible", "tap_dead_decoder_silent")
    if t.startswith("ok") and live.startswith("ok") and echo.startswith("ok"):
        return "audio reaches both pages (live and echo)"
    if t == live == echo == "no_inbound_bytes":
        return ("NO inbound-rtp bytes in either page, live or echo: the media stops at or before the edge (nothing reaches the browsers; "
                "the over may not even have reached the router)")
    if t == "no_inbound_bytes" and echo.startswith("ok"):
        return ("the echo reaches the SECOND page but no bytes reach the TALKER page: it stops at the edge for the talker's session "
                "(session swap or binding)")
    if t in silent_page:
        return "bytes reach the talker page but its decoded audio is silent or not played: the page/decoder, not the edge"
    if t == "no_inbound_bytes":
        return "no bytes reach the talker page after the release (the second page: live %s, echo %s)" % (live or "?", echo or "?")
    if t.startswith("ok") and not (live.startswith("ok") and echo.startswith("ok")):
        return "the talker page hears the echo, the second page does not (live %s, echo %s)" % (live or "?", echo or "?")
    return ""


def failure_lines(checks, diagnosis=""):
    """One line per failed check, the audio ones marked PRODUCT CHECK and the others evidence, then the diagnosis."""
    lines = []
    for c in checks:
        if c["ok"]:
            continue
        tag = "PRODUCT CHECK" if c["kind"] == PRODUCT else "evidence"
        lines.append("%s %s stopped at stage %s: %s" % (tag, c["name"], c["stage"] or "?", c["detail"]))
    if lines and diagnosis:
        lines.append("diagnosis: " + diagnosis)
    return lines


# ------------------------------------------------------------------------- state of the room (public state)

def room_entry(state, room):
    """rooms[] entry of one module in the public state, or None."""
    for entry in (state or {}).get("rooms") or []:
        if isinstance(entry, dict) and entry.get("room") == room:
            return entry
    return None


def onair_summary(state, room):
    """What the public state says about a module's over: {'onair': bool, 'callsign': str or None, 'entry': the object}. The entry is kept as the
    page's own JSON (public data only)."""
    entry = room_entry(state, room) or {}
    onair = entry.get("onair")
    call = onair.get("callsign") if isinstance(onair, dict) else None
    return {"onair": bool(onair), "callsign": call, "entry": entry}


# ------------------------------------------------------------------------- ICE1 evidence (restart counters, connection changes, the Talk pc)

def ice_metrics(samples):
    """The edge metric samples ({name, labels, value} dicts) of every dv_edge_webrtc_* family with 'restart' or 'ice' in its name."""
    return [s for s in samples or () if str(s.get("name", "")).startswith("dv_edge_webrtc") and
            ("restart" in s["name"] or "ice" in s["name"].split("_"))]


def metric_key(sample):
    return sample["name"] + json.dumps(sample.get("labels") or {}, sort_keys=True)


def metrics_delta(before, after):
    """{key: {name, labels, before, after, delta}} for every sample of either list (absent = 0); None for a side that was not read."""
    if before is None or after is None:
        return None
    b = {metric_key(s): s for s in before}
    a = {metric_key(s): s for s in after}
    out = {}
    for key in sorted(set(a) | set(b)):
        src = a.get(key) or b.get(key)
        v0, v1 = (b.get(key) or {}).get("value", 0.0), (a.get(key) or {}).get("value", 0.0)
        out[key] = {"name": src["name"], "labels": src.get("labels") or {}, "before": v0, "after": v1, "delta": v1 - v0}
    return out


def restarts_delta(delta):
    """Sum of the deltas of the families with 'restart' in the name (None when the metrics were not read, 'absent' when no such family)."""
    if delta is None:
        return None
    rows = [v["delta"] for v in delta.values() if "restart" in v["name"]]
    return sum(rows) if rows else "absent"


def per_pc_inbound(dump, t_from, t_to):
    """For each RTCPeerConnection of a dump: its inbound-rtp gain between t_from and t_to (page clock ms) and its last inbound-rtp
    snapshot, so the pc created at Talk can be told from the Listen pc the page closed. Rows: {id, created_at, closed_at, samples,
    bytes, packets, energy, last_in, last_t}."""
    out = []
    for p in (dump or {}).get("pcs") or []:
        rows = []
        for s in (dump or {}).get("samples") or []:
            for q in s.get("pcs") or []:
                if q.get("id") == p["id"]:
                    rows.append((float(s["t"]), q.get("in"), q.get("stale")))
        inside = [r for r in rows if t_from <= r[0] <= t_to]
        prior = [r for r in rows if r[0] < t_from]
        base = (prior[-1][1] if prior else None) or {}
        last = (inside[-1][1] if inside else None) or base
        live_after = [r for r in inside if not r[2]]
        out.append({"id": p["id"], "created_at": p.get("created_at"), "closed_at": p.get("closed_at"), "samples": len(rows),
                    "live_samples_in_window": len([r for r in inside if not r[2]]), "sampled_live_until_ms": live_after[-1][0] if live_after else None,
                    "bytes": (last.get("bytes") or 0) - (base.get("bytes") or 0), "packets": (last.get("packets") or 0) - (base.get("packets") or 0),
                    "energy": (None if last.get("energy") is None else round(last["energy"] - (base.get("energy") or 0.0), 6)),
                    "last_in": next((r[1] for r in reversed(rows) if r[1]), None), "last_t": rows[-1][0] if rows else None})
    return out


def ice1_diagnosis(dump, offers, restarts, ended_text="Ended: Button released.", echo_from_ms=ECHO_FROM_MS):
    """The ICE1 line of one page: navigator.connection 'change' events before the echo window (release + echo_from_ms; the first one with its
    effectiveType / rtt / downlink), the offer count and how many carried a sess_id, the restart counter delta (from restarts_delta), and
    when "Ended: Button released." appeared relative to the release (before it = the page's interrupted() path, not the user)."""
    dump = dump or {}
    release = mark_t(dump, "release")
    conn = dump.get("connection") or {}
    events = conn.get("events") or []
    until = (release + echo_from_ms) if release is not None else float("inf")
    early = [e for e in events if e.get("t", 0.0) < until]
    parts = []
    if not conn.get("supported"):
        parts.append("navigator.connection: not supported by this engine")
    elif early:
        e = early[0]
        rel = "" if release is None else " (%+.0f ms from the release)" % (e["t"] - release)
        parts.append("connection-change fired before the echo window: %d, first at %.1f s%s effectiveType %s rtt %s downlink %s"
                     % (len(early), e["t"] / 1000.0, rel, e.get("effectiveType"), e.get("rtt"), e.get("downlink")))
    else:
        parts.append("no connection-change before the echo window (%d in all)" % len(events))
    offers = offers or []
    parts.append("offers: %d (%d with sess_id = restart or renegotiation)" % (len(offers), sum(1 for o in offers if o.get("sess_id_in_body"))))
    parts.append("ice restarts counter delta: %s" % ("not read" if restarts is None else restarts))
    ended = next((u for u in dump.get("ui") or [] if u.get("talk") == ended_text), None)
    if ended is None:
        parts.append("'%s' never shown" % ended_text)
    elif release is None:
        parts.append("'%s' shown at %.1f s" % (ended_text, ended["t"] / 1000.0))
    else:
        d = ended["t"] - release
        parts.append("'%s' shown %+.0f ms from the release%s" % (ended_text, d, " (BEFORE the release: the page's interrupted() path)" if d < -100.0 else ""))
    return "; ".join(parts)


# ------------------------------------------------------------------------- the evidence file

def evidence_path(out_dir, engine, phase="off", rep=None):
    """out/<run>/web/talk-echo-<engine>.json, or talk-echo-<engine>-conn-<phase>.json for a run with a forced connection change
    (talk-echo-<engine>-conn-<phase>-<rep>.json for a numbered repeat of the PTTRX1 regression)."""
    name = re.sub(r"[^a-z0-9]+", "-", engine.lower())
    if phase == "off":
        return Path(out_dir) / "web" / ("talk-echo-%s.json" % name)
    suffix = "" if rep is None else "-%d" % int(rep)
    return Path(out_dir) / "web" / ("talk-echo-%s-conn-%s%s.json" % (name, phase, suffix))


# ------------------------------------------------------------------------- forced navigator.connection change (DVT_TALK_ECHO_CONN_CHANGE)

CONN_PHASES = ("off", "talk", "release", "echo")
FORCE_ENV = "DVT_TALK_ECHO_CONN_CHANGE"
FORCE_DELAY_S = 1.0             # 'talk': this long after the press, while held; 'echo': this long after the release
FORCE_JS = """() => {
  const c = navigator.connection;
  if (!c) return {dispatched: false, why: 'navigator.connection is undefined'};
  const t = window.__dvtEcho ? window.__dvtEcho.mark('forced_change') : null;
  c.dispatchEvent(new Event('change'));
  return {dispatched: true, t_ms: t ? t.t : null, effectiveType: c.effectiveType === undefined ? null : c.effectiveType};
}"""
HAS_CONNECTION_JS = "() => !!navigator.connection"


def conn_phase(environ):
    """The forced-change phase from the environment: off (default), talk, release or echo. ValueError for anything else."""
    value = (environ.get(FORCE_ENV) or "off").strip().lower()
    if value not in CONN_PHASES:
        raise ValueError("%s=%r: wanted one of %s" % (FORCE_ENV, value, ", ".join(CONN_PHASES)))
    return value


def forced_lines(lines, phase):
    """Failure lines of a run with a forced change say so: 'after a forced connection change at <phase>: <line>' (unchanged for off)."""
    if phase == "off":
        return list(lines)
    return ["after a forced connection change at %s: %s" % (phase, line) for line in lines]


def write_evidence(path, doc):
    """Write the JSON document; returns True when written. Never raises (the run's verdict does not depend on the disk)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return True
    except (OSError, TypeError, ValueError):
        return False
