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
  // every text change of the Talk status line and of the Listen status line on its own (evidence: a transient such as "Pending - queued /
  // waiting for the floor" that lasts tens of ms); the selectors are set by start() from cases/web_pages.py, these are the same defaults
  rec.sels = { talk: '#talk-status', listen: "[data-testid='listen-connection'] p[role=status]" };
  rec.talk_tl = [];
  rec.listen_tl = [];
  const textOf = (sel) => { const e = document.querySelector(sel); return e ? String(e.textContent).trim() : null; };
  const tlNote = (tl, text, ts) => {
    const last = tl[tl.length - 1];
    if ((last ? last.text : undefined) !== text && tl.length < 300) tl.push({ t: ts, wall: Date.now(), text: text });
  };
  const uiNote = () => {
    try {
      const ts = rd(now(), 1);
      const card = document.querySelector("[data-testid='listen-connection']");
      const r = { talk: textOf(rec.sels.talk), listen: textOf(rec.sels.listen), state: card ? card.getAttribute('data-state') : null };
      tlNote(rec.talk_tl, r.talk, ts);
      tlNote(rec.listen_tl, r.listen, ts);
      const last = rec.ui[rec.ui.length - 1];
      if (!last || last.talk !== r.talk || last.listen !== r.listen || last.state !== r.state) {
        r.t = ts; r.wall = Date.now();
        if (rec.ui.length < 300) rec.ui.push(r);
      }
    } catch (e) { err('ui', e); }
  };
  rec.uiNote = uiNote;
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

  const ensureCtx = () => {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!rec.ctx) {
      rec.ctx = new AC();
      rec.rate = rec.ctx.sampleRate;
      try { const p = rec.ctx.resume(); if (p && p.catch) p.catch(() => {}); } catch (e) { err('resume', e); }
    }
    return rec.ctx;
  };
  const tapTrack = (r, track) => {
    r.audioTracks += 1;
    if (r.tapped) return;
    try {
      if (!ensureCtx()) { r.tap_error = 'no AudioContext'; return; }
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
    const r = { id: rec.pcs.length, pc: pc, createdAt: now(), closedAt: null, audioTracks: 0, tapped: false, buf: [], have: 0, calls: 0, trackIds: [],
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
      pc.addEventListener('track', (e) => {
        if (e.track) r.trackIds.push(e.track.id);
        if (e.track && e.track.kind === 'audio') tapTrack(r, e.track);
      });
    } catch (e) { err('wrap', e); }
    return pc;
  }
  Wrapped.prototype = Prev.prototype;
  Object.setPrototypeOf(Wrapped, Prev);
  window.RTCPeerConnection = Wrapped;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Wrapped;

  // The page's own audio element (what the USER hears): an AnalyserNode on its CURRENT srcObject, polled every 250 ms and re-attached
  // whenever the stream or its audio tracks change; each track is matched to the RTCPeerConnection that owns it (by track id).
  rec.el = { selector: null, timer: null, sig: null, src: null, analyser: null, buf: null, log: [], polls: 0, changes: 0, an_max: 0, an_last: 0,
             an_n: 0, attach_error: null, assignments: [], cur: null, cur_sid: undefined, hooked: new WeakSet() };
  // srcObject assignments of the element (the setter is wrapped, so the time is exact; the poll notes one it missed) with the time of the
  // first audio track and the first 'unmute' of that stream; 'addtrack' / 'removetrack' of the stream and 'ended' / 'unmute' of a track
  // re-attach the analyser at once (a stream that gets its track AFTER the assignment is the case the page itself got wrong)
  const hookTrack = (a, t) => {
    if (rec.el.hooked.has(t)) return;
    rec.el.hooked.add(t);
    for (const what of ['unmute', 'mute', 'ended']) {
      t.addEventListener(what, () => {
        const ts = rd(now(), 1);
        if (a.ev.length < 60) a.ev.push({ t: ts, what: what, track: t.id });
        if (what === 'unmute' && a.first_unmute_t === null) a.first_unmute_t = ts;
        if (what === 'ended' || what === 'unmute') { rec.el.sig = null; rec.elPoll(); }
      });
    }
  };
  const noteAssignment = (stream, via) => {
    const E = rec.el;
    const a = { t: rd(now(), 1), wall: Date.now(), via: via, stream: stream ? stream.id : null, tracks_at: 0, first_audio_track_t: null,
                first_unmute_t: null, ev: [] };
    E.assignments.push(a);
    E.cur = a;
    E.cur_sid = a.stream;
    if (!stream) return;
    const audio = stream.getAudioTracks ? stream.getAudioTracks() : [];
    a.tracks_at = audio.length;
    if (audio.length) a.first_audio_track_t = a.t;
    audio.forEach((t) => { hookTrack(a, t); if (t.muted === false && a.first_unmute_t === null) a.first_unmute_t = a.t; });
    if (stream.addEventListener) {
      stream.addEventListener('addtrack', (e) => {
        const ts = rd(now(), 1);
        if (a.ev.length < 60) a.ev.push({ t: ts, what: 'addtrack', track: e.track ? e.track.id : null });
        if (e.track && e.track.kind === 'audio') {
          if (a.first_audio_track_t === null) a.first_audio_track_t = ts;
          hookTrack(a, e.track);
          if (e.track.muted === false && a.first_unmute_t === null) a.first_unmute_t = ts;
        }
        rec.elPoll();
      });
      stream.addEventListener('removetrack', (e) => {
        if (a.ev.length < 60) a.ev.push({ t: rd(now(), 1), what: 'removetrack', track: e.track ? e.track.id : null });
        rec.elPoll();
      });
    }
  };
  try {
    if (typeof HTMLMediaElement !== 'undefined') {
      const d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
      if (d && d.set && d.configurable) {
        Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', { configurable: true, enumerable: d.enumerable, get: d.get,
          set: function (v) {
            d.set.call(this, v);
            try { if (rec.el.selector && this.matches && this.matches(rec.el.selector)) { noteAssignment(v, 'setter'); rec.elPoll(); } } catch (e) { err('srcObject', e); }
          } });
      }
    }
  } catch (e) { err('srcObject setter', e); }
  const ownerOf = (track) => {
    for (const r of rec.pcs) {
      if (r.trackIds.indexOf(track.id) >= 0) return r.id;
      try { if (r.pc.getReceivers().some((x) => x.track && x.track.id === track.id)) return r.id; } catch (e) { /* closed */ }
    }
    return null;
  };
  const trackInfo = (t) => ({ id: t.id, kind: t.kind, ready: t.readyState, enabled: t.enabled, muted: t.muted, pc: ownerOf(t) });
  const elDetach = () => {
    const E = rec.el;
    try { if (E.src) E.src.disconnect(); } catch (e) { /* ignore */ }
    E.src = null; E.analyser = null; E.buf = null;
  };
  const elAttach = (stream) => {
    const E = rec.el;
    try {
      const ctx = ensureCtx();
      if (!ctx) { E.attach_error = 'no AudioContext'; return; }
      E.src = ctx.createMediaStreamSource(stream);
      E.analyser = ctx.createAnalyser();
      E.analyser.fftSize = 2048;
      E.buf = new Float32Array(E.analyser.fftSize);
      E.src.connect(E.analyser);
    } catch (e) { E.attach_error = String((e && e.message) || e).slice(0, 100); E.analyser = null; }
  };
  rec.elPoll = () => {
    const E = rec.el;
    try {
      const el = document.querySelector(E.selector);
      E.polls += 1;
      const stream = el ? el.srcObject : null;
      const tracks = stream && stream.getAudioTracks ? stream.getAudioTracks() : [];
      const sid = stream ? stream.id : null;
      if (sid !== E.cur_sid && !(sid === null && E.cur_sid === undefined)) noteAssignment(stream, 'poll');
      const sig = stream ? (stream.id + '|' + tracks.map((t) => t.id).join(',')) : null;
      if (sig !== E.sig) {
        E.sig = sig; E.changes += 1; E.attach_error = null;
        elDetach();
        E.log.push({ t: rd(now(), 1), wall: Date.now(), stream: stream ? stream.id : null, tracks: tracks.map(trackInfo) });
        if (stream && tracks.length) elAttach(stream);
      }
      if (E.analyser) {
        E.analyser.getFloatTimeDomainData(E.buf);
        const v = rmsOf(E.buf);
        E.an_last = v; E.an_max = Math.max(E.an_max, v); E.an_n += 1;
      }
    } catch (e) { err('element', e); }
  };
  const elSnapshot = () => {
    const E = rec.el;
    if (!E.selector) return null;
    const el = document.querySelector(E.selector);
    const stream = el ? el.srcObject : null;
    const out = { present: !!el, stream: stream ? stream.id : null,
                  tracks: (stream && stream.getAudioTracks ? stream.getAudioTracks() : []).map(trackInfo),
                  paused: el ? el.paused : null, muted: el ? el.muted : null, volume: el ? el.volume : null, ready: el ? el.readyState : null,
                  attached: !!E.analyser, attach_error: E.attach_error, changes: E.changes, polls: E.polls,
                  an_rms: rd(E.an_max, 5), an_last: rd(E.an_last, 5), an_n: E.an_n, ctx: rec.ctx ? rec.ctx.state : null };
    E.an_max = 0; E.an_n = 0;
    return out;
  };

  rec.tick = async () => {
    if (rec.busy) { rec.skipped += 1; return null; }
    rec.busy = true;
    try {
      const t = now();
      const row = { t: rd(t, 1), wall: Date.now(), hidden: !!document.hidden, ctx: rec.ctx ? rec.ctx.state : null, pcs: [] };
      try { row.el = elSnapshot(); } catch (e) { err('elSnapshot', e); }
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
  rec.start = (ms, selector, sels) => {
    if (rec.timer) return false;
    rec.interval_ms = ms || 500;
    if (sels) { if (sels.talk) rec.sels.talk = sels.talk; if (sels.listen) rec.sels.listen = sels.listen; }
    rec.uiNote();
    if (selector) {
      rec.el.selector = selector;
      rec.el.timer = setInterval(rec.elPoll, 250);
      rec.elPoll();
    }
    rec.timer = setInterval(() => { rec.tick(); }, rec.interval_ms);
    rec.tick();
    return true;
  };
  rec.stop = () => {
    if (rec.timer) { clearInterval(rec.timer); rec.timer = null; }
    if (rec.el.timer) { clearInterval(rec.el.timer); rec.el.timer = null; }
    return true;
  };
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
    element: { selector: rec.el.selector, changes: rec.el.changes, log: rec.el.log, assignments: rec.el.assignments },
    connection: rec.connection, ui: rec.ui, talk_status_timeline: rec.talk_tl, listen_status_timeline: rec.listen_tl, samples: rec.samples, marks: rec.marks, resources: rec.resources() }));
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
               "pcs_live": 0, "pcs_total": 0, "ctx": s.get("ctx"), "hidden": s.get("hidden"),
               "el": s.get("el"), "el_rms": float((s.get("el") or {}).get("an_rms") or 0.0), "el_polls": int((s.get("el") or {}).get("polls") or 0)}
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


def _pcs(ids):
    return ", ".join("pc%s" % i for i in sorted(ids)) or "no pc"


def element_rows(rows):
    """series() rows seen through the page's AUDIO ELEMENT: the AnalyserNode's rms stands for the analyser columns, its poll count for the
    callbacks (the byte, packet and energy columns stay the receiver's), so judge_window judges what the user hears."""
    return [dict(r, rms05=r["el_rms"], rms1=r["el_rms"], peak_hz=0.0, fraction=0.0, calls=r["el_polls"]) for r in rows]


def receiver_summary(dump, lo, hi):
    """getStats inbound-rtp of every RTCPeerConnection in the window, in one line; the pc created at the Talk press is marked 'talk'."""
    created = ((pc_story(dump).get("at_talk") or {}).get("created")) or []
    parts = []
    for p in per_pc_inbound(dump, lo, hi):
        closed = p["closed_at"] is not None and p["closed_at"] <= hi
        parts.append("pc%s(%s%s) +%d bytes +%d packets" % (p["id"], "talk, " if p["id"] in created else "", "closed" if closed else "live",
                                                          p["bytes"], p["packets"]))
    return "getStats inbound-rtp in the window: " + ("; ".join(parts) if parts else "no RTCPeerConnection")


def element_text(facts):
    """The audio element's state in one line for a failure text."""
    if not facts:
        return "audio element: no samples"
    last = facts["last"]
    tracks = ", ".join("%s %s%s pc%s" % (t.get("id"), t.get("ready"), " muted" if t.get("muted") else "", t.get("pc")) for t in last.get("tracks") or []) or "no audio track"
    return ("audio element analyser: best rms %.4f in %d samples, stream %s, tracks [%s], paused %s muted %s volume %s, AudioContext %s%s"
            % (facts["best_an_rms"], facts["samples"], last.get("stream"), tracks, last.get("paused"), last.get("muted"), last.get("volume"),
               last.get("ctx"), (", attach error: %s" % last["attach_error"]) if last.get("attach_error") else ""))


def element_facts(rows, lo, hi):
    """The audio element's evidence in [lo, hi]: {samples, present, streams, best_an_rms, last} or None without element samples."""
    inside = [r for r in rows if lo <= r["t"] <= hi and r.get("el")]
    if not inside:
        return None
    return {"samples": len(inside), "present": any(r["el"].get("present") for r in inside),
            "streams": sorted({str(r["el"].get("stream")) for r in inside}), "best_an_rms": round(max(r["el_rms"] for r in inside), 5),
            "last": inside[-1]["el"], "last_t": inside[-1]["t"], "polls": inside[-1]["el_polls"] - inside[0]["el_polls"]}


def judge_element(rows, dump, lo, hi, floor=RMS_FLOOR, min_loud=MIN_LOUD):
    """Judge what the page's AUDIO ELEMENT plays in [lo, hi] (page clock ms): its AnalyserNode must rise above the floor in `min_loud` samples.
    A failure names the stage, with the element and the receiver (getStats inbound-rtp of every pc) side by side:
      no_audio_element / element_without_stream / element_stream_without_audio_track   nothing is bound to play
      element_on_closed_stream      the element plays the stream of a closed session (track ended) while another session receives
      element_on_other_session      the element's session receives nothing while another one does
      element_stream_foreign        its tracks belong to no RTCPeerConnection of the page
      element_paused_or_muted       the element's live session receives and it is paused / muted / volume 0
      element_silent_on_live_stream the element's live session receives and plays, the analyser still hears nothing
      no_inbound_bytes              (receiver level) nothing reaches the page at all
      not_observable                the AudioContext is not running: the analyser cannot read (Firefox in the container)"""
    facts = element_facts(rows, lo, hi)
    recv = receiver_summary(dump, lo, hi)
    if facts is None:
        return {"ok": False, "stage": "no_element_data", "via": None, "element": None, "receiver": recv,
                "detail": "the recorder has no audio element sample in the window (the element watch was not started or the selector matches nothing); " + recv}
    v = judge_window(element_rows(rows), lo, hi, expect_hz=None, floor=floor, min_loud=min_loud)
    v["element"], v["receiver"] = facts, recv
    tail = " | " + recv + " | " + element_text(facts)
    if v["ok"]:
        v.update(stage="ok", via="element analyser", detail="the page's audio element is audible: %d loud 0.5 s samples, best rms %.3f%s" % (v["loud_samples"], v["best_rms05"], tail))
        return v
    if v["stage"] == "no_inbound_bytes":
        v["detail"] = "no bytes reach the page, so the audio element has nothing to play" + tail
        return v
    last = facts["last"]
    tracks = last.get("tracks") or []
    owners = {t["pc"] for t in tracks if t.get("pc") is not None}
    per = {p["id"]: p for p in per_pc_inbound(dump, lo, hi)}
    receiving = {i for i, p in per.items() if p["bytes"] > 0}
    last_t = facts["last_t"]
    closed = {i for i in per if per[i]["closed_at"] is not None and per[i]["closed_at"] <= last_t}
    if not facts["present"]:
        stage, why = "no_audio_element", "the page has no audio element for the selector (nothing can play the stream)"
    elif last.get("ctx") not in ("running", None):
        v.update(ok=True, stage="not_observable", via="none", detail="the AudioContext is %s, so the element's analyser cannot read; judged by the receiver only%s" % (last.get("ctx"), tail))
        return v
    elif last.get("stream") is None:
        stage, why = "element_without_stream", "the page's audio element has no srcObject (no stream is bound to it)"
    elif not tracks:
        stage, why = "element_stream_without_audio_track", ("the audio element's stream %s has no audio track (the stream was bound before its track "
                                                              "arrived and no track was added, or the track was removed)" % last.get("stream"))
    elif not owners:
        stage, why = "element_stream_foreign", "the audio element plays a stream whose tracks belong to no RTCPeerConnection of the page"
    elif (owners <= closed or all(t.get("ready") == "ended" for t in tracks)) and (receiving - owners):
        ended = any(t.get("ready") == "ended" for t in tracks)
        stage, why = "element_on_closed_stream", ("the page plays the closed listen stream (%s) while the talk session receives the echo "
                                                    "(element on %s, receiving %s)" % ("track ended" if ended else "its session is closed",
                                                                                          _pcs(owners), _pcs(receiving - owners)))
    elif owners & receiving:
        if last.get("paused") or last.get("muted") or last.get("volume") == 0:
            stage, why = "element_paused_or_muted", "the audio element is paused, muted or at volume 0 while its session receives the echo"
        else:
            stage, why = "element_silent_on_live_stream", ("the audio element plays the live session (%s) that receives the echo, "
                                                           "but its AnalyserNode hears nothing" % _pcs(owners & receiving))
    else:
        stage, why = "element_on_other_session", "the audio element plays %s, which receives nothing, while %s receives" % (_pcs(owners), _pcs(receiving))
    v.update(stage=stage, detail=why + tail)
    return v


def element_timing(dump, offer_answer_ms=None):
    """Per srcObject assignment of the page's audio element (the page clock, ms): when it happened relative to the Talk press and to the answer
    of the Talk circuit's rtc/offer, and how long the stream took to get its first audio track and its first 'unmute' (0 = it had one at the
    assignment; None = never). A primary result to compare between images."""
    press = mark_t(dump, "press")
    out = []
    for a in ((dump or {}).get("element") or {}).get("assignments") or []:
        row = {"stream": a.get("stream"), "via": a.get("via"), "assigned_ms": a["t"], "tracks_at_assignment": a.get("tracks_at"),
               "rel_press_ms": None if press is None else round(a["t"] - press, 1),
               "rel_offer_answer_ms": None if offer_answer_ms is None else round(a["t"] - offer_answer_ms, 1), "events": a.get("ev") or []}
        for key, field in (("audio_track", "first_audio_track_t"), ("unmute", "first_unmute_t")):
            at = a.get(field)
            row[key + "_after_assignment_ms"] = None if at is None else round(at - a["t"], 1)
            row[key + "_rel_press_ms"] = None if at is None or press is None else round(at - press, 1)
            row[key + "_rel_offer_answer_ms"] = None if at is None or offer_answer_ms is None else round(at - offer_answer_ms, 1)
        out.append(row)
    return out


def timing_lines(rows):
    """One line per srcObject assignment of element_timing(): when it happened (from the press and from the Talk offer's answer) and how long
    the stream took to get its audio track and its first unmute."""
    fmt = lambda v: "never" if v is None else "%+.0f ms" % v                    # noqa: E731
    return ["audio element srcObject %s (%s): assigned %s from the press, %s from the offer answer; audio track %s after the assignment, "
            "unmute %s after it" % (r["stream"], r["via"], fmt(r["rel_press_ms"]), fmt(r["rel_offer_answer_ms"]),
                                    fmt(r["audio_track_after_assignment_ms"]), fmt(r["unmute_after_assignment_ms"])) for r in rows]


def status_timeline(dump, key):
    """The text changes of one status line from a recorder dump (key 'talk_status_timeline' or 'listen_status_timeline'), each as {text,
    t_ms, rel_press_ms, rel_release_ms, held_ms}: the offsets are against the press / release marks of the same dump (None without the
    mark), held_ms is how long the text stayed until the next change (None for the last one)."""
    press, release = mark_t(dump, "press"), mark_t(dump, "release")
    rows = list((dump or {}).get(key) or [])
    out = []
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["t"] if i + 1 < len(rows) else None
        out.append({"text": r.get("text"), "t_ms": r["t"], "rel_press_ms": None if press is None else round(r["t"] - press, 1),
                    "rel_release_ms": None if release is None else round(r["t"] - release, 1),
                    "held_ms": None if nxt is None else round(nxt - r["t"], 1)})
    return out


def status_timeline_line(rows):
    """One line: 'text' @ offset from the press (offset from the release), held N ms; ... (the transient states are in it)."""
    fmt = lambda v: "?" if v is None else "%+.0f" % v                             # noqa: E731
    return "; ".join("%r @ %s ms from the press (%s from the release)%s" % (
        r["text"], fmt(r["rel_press_ms"]), fmt(r["rel_release_ms"]), "" if r["held_ms"] is None else ", held %.0f ms" % r["held_ms"])
        for r in rows) or "no change recorded"


def talk_offer_answer_ms(dump, offer_walls):
    """Page-clock ms of the answer of the first rtc/offer that came back at or after the press (offer_walls: the tester's wall seconds of
    the POST /rtc/offer responses of this page), or None."""
    press = mark_t(dump, "press")
    if press is None:
        return None
    for w in sorted(offer_walls or ()):
        t = page_ms(dump, w)
        if t is not None and t >= press - 200.0:
            return t
    return None


def session_order(table, phases, before=0.5, after=3.0):
    """The POST /rtc/close and /rtc/offer requests around the Talk press, in time order: {'order': 'close -> offer', 'rows': [...]} (production:
    the Listen session is closed, then the talk session is offered). `table` is requests_table(), `phases` the wall-clock phases."""
    press = phases.get("press")
    if press is None:
        return None
    base = min(v for v in phases.values() if v is not None)
    p = press - base
    rows = []
    for r in table:
        if r["method"] != "POST" or r["t_s"] is None or not (p - before <= r["t_s"] <= p + after):
            continue
        what = "close" if r["path"].endswith("/rtc/close") else ("offer" if r["path"].endswith("/rtc/offer") else None)
        if what:
            rows.append({"what": what, "t_s": r["t_s"], "rel_press_s": round(r["t_s"] - p, 2), "response_s": r["response_s"], "status": r["status"],
                         "sess_id_in_body": bool((r.get("body") or {}).get("sess_id")) if what == "offer" else None})
    rows.sort(key=lambda x: x["t_s"])
    return {"order": " -> ".join(x["what"] for x in rows), "rows": rows}


CUT_SHORT_TOL_S = 0.5           # the SUT may end the over this much earlier than the release (grant delay, the page's own timing)
ECHO_MARGIN_S = 3.0             # the echo window is [anchor, anchor + the over's duration + this]
ECHO_GUARD_MS = 600.0           # the listener's echo window starts this late after the over's end: the 0.5 s analyser window must not hold the live tail
FIRST_PACKET_LEAD_MS = SAMPLE_MS     # a first echo packet seen in a sample arrived within the sampling interval before it


def min_loud_for(duration_s):
    """Loud 0.5 s samples asked for in an over of this length: MIN_LOUD (one second of sound), one for an over shorter than 1.5 s."""
    return MIN_LOUD if duration_s is None or duration_s >= 1.5 else 1


def first_echo_start(rows, after_ms, quiet_samples=1):
    """Page-clock ms of the first sample in which the inbound bytes of a series() GREW again after a quiet stretch (at least `quiet_samples`
    samples without growth) that followed the first burst of bytes after `after_ms` (the live over): the first echo packet, to within one
    sampling interval. None when there is no such second burst."""
    state, quiet, prev = "idle", 0, None
    for r in rows:
        if prev is not None and r["t"] > after_ms:
            grew = r["bytes"] > prev["bytes"]
            if state == "idle":
                if grew:
                    state = "burst"
            elif state == "burst":
                if grew:
                    quiet = 0
                else:
                    quiet += 1
                    if quiet >= quiet_samples:
                        state = "gap"
            elif grew:
                return r["t"]
        prev = r
    return None


def burst_seconds(rows, after_ms):
    """Length in seconds of the first burst of inbound bytes after `after_ms` (the sample before the first growth to the last sample of that
    growth), or None."""
    start = end = None
    prev = None
    for r in rows:
        if prev is not None and r["t"] > after_ms:
            if r["bytes"] > prev["bytes"]:
                if start is None:
                    start = prev["t"]
                end = r["t"]
            elif start is not None and r["t"] - end >= SAMPLE_MS:
                break
        prev = r
    return None if start is None else round((end - start) / 1000.0, 2)


def page_ms(dump, wall_s):
    """Seconds on the tester's wall clock -> page-clock ms of a recorder dump (its t0_wall), or None."""
    t0 = (dump or {}).get("t0_wall")
    return None if t0 is None or wall_s is None else wall_s * 1000.0 - float(t0)


def echo_anchor(dump, sut, listener_rows=None, listener_dump=None):
    """(anchor ms on the page clock of `dump`, kind) where the echo window of a page opens:
       'state'         the over's end as the SUT reports it (public state, rooms[E].last.end_ms, converted by the wall clock);
       'first_packet'  fallback: the first inbound packet after a quiet gap that followed the live over, in the second page's series
                       (converted to this page's clock by the wall clock), less one sampling interval;
       'release'       last resort: the tester's release mark."""
    if sut and sut.get("end_wall_s") is not None and page_ms(dump, sut["end_wall_s"]) is not None:
        return page_ms(dump, sut["end_wall_s"]), "state"
    if listener_rows and listener_dump and dump and listener_dump.get("t0_wall") is not None and dump.get("t0_wall") is not None:
        air = mark_t(listener_dump, "on_air")
        first = first_echo_start(listener_rows, air if air is not None else 0.0)
        if first is not None:
            return page_ms(dump, (listener_dump["t0_wall"] + first) / 1000.0) - FIRST_PACKET_LEAD_MS, "first_packet"
    return mark_t(dump, "release"), "release"


def analyse_run(dumps, engine, floor=RMS_FLOOR, sut=None, offers_wall=None):
    """Judge a whole run from the two recorder dumps {'talker': dump, 'listener': dump} (either may be None). `sut` is OverTracker.summary():
    the over as the SUT reports it (end_wall_s, start_wall_s, duration_s, ...) or None. The echo windows are anchored to the SUT's over end, not
    to the tester's release (PTTRX1: the SUT ended a 3 s hold's over after 1 s and replayed it BEFORE the tester released). Returns {'checks':
    [...], 'diagnosis': str, 'windows': {...}, 'talker': {...}, 'listener': {...}}; each check is {name, kind (product | evidence), ok, stage,
    detail}. The audio checks and over_not_cut_short are the product check:
      talker_hears_echo     the talker's own page: audio in [anchor, anchor + the SUT over's duration + 3 s]
      listener_hears_live   the second page: audio during the over the SUT reports (the tester's hold when the state is not available)
      listener_hears_echo   the second page: the echo replay in the same window (from ECHO_GUARD_MS after the anchor)
      over_not_cut_short    the SUT's over lasts within 0.5 s of the tester's hold (only when the state gave a duration)"""
    want = EXPECT_HZ.get(engine)
    out = {"checks": [], "diagnosis": "", "talker": {}, "listener": {}, "expect_hz": want, "windows": {}}
    rows = {name: series(d) for name, d in (dumps or {}).items() if d}
    for name, d in (dumps or {}).items():
        if not d:
            continue
        out[name] = {"sampling": sampling_facts(rows[name]), "pcs": pc_story(d), "pc_states": [p.get("final") for p in d.get("pcs") or []],
                     "recorder_errors": d.get("errors") or [], "ctx_state": d.get("ctx_state"), "rate": d.get("rate")}
        answer = talk_offer_answer_ms(d, (offers_wall or {}).get(name))
        out[name]["talk_offer_answer_ms"] = answer
        out[name]["talk_status_timeline"] = status_timeline(d, "talk_status_timeline")
        out[name]["listen_status_timeline"] = status_timeline(d, "listen_status_timeline")
        out[name]["element_timing"] = element_timing(d, answer)

    def add(name, kind, verdict=None, ok=None, stage="", detail=""):
        if verdict is not None:
            ok, stage, detail = verdict["ok"], verdict["stage"], verdict["detail"]
        out["checks"].append({"name": name, "kind": kind, "ok": bool(ok), "stage": stage, "detail": detail, "verdict": verdict})

    tail_ms = TAIL_S * 1000.0
    tx, rx = dumps.get("talker") if dumps else None, dumps.get("listener") if dumps else None
    sut_dur = (sut or {}).get("duration_s")
    held = None
    if tx is not None and mark_t(tx, "release") is not None and mark_t(tx, "on_air") is not None:
        held = round((mark_t(tx, "release") - mark_t(tx, "on_air")) / 1000.0, 2)
    # the length of the echo: the SUT's over; else the live burst the second page received; else the tester's hold
    dur = sut_dur
    if dur is None and rx is not None and mark_t(rx, "on_air") is not None:
        dur = burst_seconds(rows["listener"], mark_t(rx, "on_air"))
    dur_source = "state" if sut_dur is not None else ("live_burst" if dur is not None else "tester_hold")
    if dur is None:
        dur = held if held is not None else HOLD_S
    out["windows"] = {"sut_over": sut, "tester_held_s": held, "echo_duration_s": dur, "echo_duration_source": dur_source}
    loud_needed = min_loud_for(dur)

    if tx is None or mark_t(tx, "release") is None:
        add("talker_hears_echo", EVIDENCE, ok=False, stage="no_data", detail="no recorder data of the talker page after the release")
        add("talker_page_audio_element_hears_echo", PRODUCT, ok=False, stage="no_data", detail="no recorder data of the talker page after the release")
    else:
        anchor, kind = echo_anchor(tx, sut, rows.get("listener"), rx)
        lo, hi = (anchor, anchor + tail_ms) if kind == "release" else (anchor, anchor + dur * 1000.0 + ECHO_MARGIN_S * 1000.0)
        out["windows"]["talker_echo"] = {"anchor": kind, "from_ms": round(lo, 1), "to_ms": round(hi, 1)}
        add("talker_hears_echo", EVIDENCE, judge_window(rows["talker"], lo, hi, expect_hz=want, floor=floor, min_loud=loud_needed))
        add("talker_page_audio_element_hears_echo", PRODUCT, judge_element(rows["talker"], tx, lo, hi, floor=floor, min_loud=loud_needed))
    if rx is None or mark_t(rx, "release") is None or mark_t(rx, "on_air") is None:
        add("listener_hears_live", EVIDENCE, ok=False, stage="no_data", detail="no recorder data of the second page during the over")
        add("listener_hears_echo", EVIDENCE, ok=False, stage="no_data", detail="no recorder data of the second page after the release")
        add("listener_page_audio_element_hears_live", PRODUCT, ok=False, stage="no_data", detail="no recorder data of the second page during the over")
        add("listener_page_audio_element_hears_echo", PRODUCT, ok=False, stage="no_data", detail="no recorder data of the second page after the release")
    else:
        air, rel = mark_t(rx, "on_air"), mark_t(rx, "release")
        s_start = page_ms(rx, (sut or {}).get("start_wall_s"))
        s_end = page_ms(rx, (sut or {}).get("end_wall_s"))
        live_lo = min(air, s_start) if s_start is not None else air
        live_hi = (s_end if s_end is not None else rel) + LIVE_GRACE_MS
        out["windows"]["listener_live"] = {"anchor": "state" if s_end is not None else "tester_hold",
                                           "from_ms": round(live_lo, 1), "to_ms": round(live_hi, 1)}
        # frequency of the live over is recorded, not asserted (the fake microphone of Firefox and WebKit is not 700 Hz anyway)
        add("listener_hears_live", EVIDENCE, judge_window(rows["listener"], live_lo, live_hi, expect_hz=None, floor=floor, min_loud=loud_needed))
        add("listener_page_audio_element_hears_live", PRODUCT, judge_element(rows["listener"], rx, live_lo, live_hi, floor=floor, min_loud=loud_needed))
        anchor, kind = echo_anchor(rx, sut, rows["listener"], rx)
        if kind == "release":
            lo, hi, settle = anchor + ECHO_FROM_MS, anchor + tail_ms, ECHO_SETTLE_MS
        elif kind == "state":
            lo, hi, settle = anchor + ECHO_GUARD_MS, anchor + dur * 1000.0 + ECHO_MARGIN_S * 1000.0, ECHO_SETTLE_MS
        else:
            lo, hi, settle = anchor, anchor + dur * 1000.0 + ECHO_MARGIN_S * 1000.0, 0.0
        out["windows"]["listener_echo"] = {"anchor": kind, "from_ms": round(lo, 1), "to_ms": round(hi, 1)}
        add("listener_hears_echo", EVIDENCE, judge_window(rows["listener"], lo, hi, expect_hz=want, floor=floor, settle_ms=settle,
                                                          min_loud=loud_needed))
        add("listener_page_audio_element_hears_echo", PRODUCT, judge_element(rows["listener"], rx, lo, hi, floor=floor, min_loud=loud_needed))
    if sut_dur is not None and held is not None:
        short = held - sut_dur
        ok = short <= CUT_SHORT_TOL_S
        if ok:
            detail = "the SUT's over lasted %.2f s for a Talk held %.2f s (tolerance %.1f s)" % (sut_dur, held, CUT_SHORT_TOL_S)
        else:
            detail = ("the SUT ended the over after %.2f s while Talk was held %.2f s (%.2f s early, tolerance %.1f s): the user's over "
                      "was cut short (PTTRX1)" % (sut_dur, held, short, CUT_SHORT_TOL_S))
        add("over_not_cut_short", PRODUCT, ok=ok, stage="ok" if ok else "over_cut_short", detail=detail)
    else:
        add("over_not_cut_short", EVIDENCE, ok=True, stage="not_observed",
            detail="the public state gave no over duration (%s)" % ("no SUT over seen" if not sut else "no tester hold"))
    out["diagnosis"] = diagnose({c["name"]: c for c in out["checks"]})
    return out


def diagnose(checks):
    """One line on WHERE the media stopped, from the three audio checks (dict by name); '' when there is nothing to say."""
    get = lambda n: (checks.get(n) or {}).get("stage", "")                       # noqa: E731
    t, live, echo = get("talker_hears_echo"), get("listener_hears_live"), get("listener_hears_echo")
    silent_page = ("decoder_silent", "analyser_silent_decoder_audible", "tap_dead_decoder_silent")
    if get("over_not_cut_short") == "over_cut_short":
        return ("the SUT ended the over much earlier than the release (%s); the echo windows follow the SUT's end, so a quiet page here is "
                "a consequence, not a separate fault" % (checks["over_not_cut_short"]["detail"],))
    for name in ("talker_page_audio_element_hears_echo", "listener_page_audio_element_hears_live", "listener_page_audio_element_hears_echo"):
        c = checks.get(name) or {}
        if c.get("stage") in ("element_on_closed_stream", "element_on_other_session", "element_stream_without_audio_track",
                              "element_without_stream", "element_stream_foreign", "element_paused_or_muted"):
            return "%s: %s" % (name, c["detail"].split(" | ")[0])
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


START_FIELDS = ("start_ms", "started_ms", "since_ms", "begin_ms")


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _start_field(*objs):
    for obj in objs:
        if isinstance(obj, dict):
            for key in START_FIELDS:
                if _num(obj.get(key)) is not None:
                    return float(obj[key]), key
    return None, None


class OverTracker:
    """Follows the public state of one module poll by poll (wall = the tester's time.time() at the middle of the request) and keeps every over
    that ENDED after the first poll: rooms[E].last.end_ms changes value. The first poll is the baseline (an earlier over is ignored). The
    server clock is tied to the tester's by skew = wall - state.now_ms at the poll that saw the end. The start of an over is read from a field
    of onair / last (start_ms, started_ms, since_ms, begin_ms) when the state has one, else it is the first poll that showed the module on
    air LESS one poll interval (an earlier bound, so a duration is never underestimated and a cut-short over is not invented)."""

    def __init__(self, room=ROOM):
        self.room, self.polls = room, 0
        self.last_end = None
        self.cur = None
        self.overs = []
        self.entries = []                  # distinct room entries seen, with the poll's wall time (evidence)
        self._last_wall = None
        self._last_json = None

    def observe(self, wall_s, state):
        entry = room_entry(state, self.room) or {}
        now_ms = _num((state or {}).get("now_ms"))
        skew = None if now_ms is None else wall_s * 1000.0 - now_ms
        interval = None if self._last_wall is None else wall_s - self._last_wall
        self._last_wall = wall_s
        text = json.dumps(entry, sort_keys=True)
        if text != self._last_json and len(self.entries) < 60:
            self.entries.append({"wall_s": round(wall_s, 3), "skew_ms": None if skew is None else round(skew, 1), "entry": entry})
        self._last_json = text
        last = entry.get("last") if isinstance(entry.get("last"), dict) else {}
        end = _num(last.get("end_ms"))
        onair = entry.get("onair")
        if self.polls == 0:
            self.last_end = end
        self.polls += 1
        if onair and self.cur is None:
            value, key = _start_field(onair if isinstance(onair, dict) else None)
            self.cur = {"first_onair_wall": wall_s, "poll_interval_s": interval, "start_ms": value, "start_key": key}
        if end is not None and end != self.last_end:
            value, key = _start_field(last)
            cur = self.cur or {}
            if value is None:
                value, key = cur.get("start_ms"), cur.get("start_key")
            self.overs.append({"end_ms": end, "skew_ms": skew, "seen_wall_s": wall_s, "start_ms": value, "start_key": key,
                               "first_onair_wall_s": cur.get("first_onair_wall"), "poll_interval_s": cur.get("poll_interval_s") or interval,
                               "duration_ms_field": _num(last.get("duration_ms"))})
            self.cur = None
            self.last_end = end

    def summary(self, press_wall_s, release_wall_s, early_s=0.5, late_s=1.5):
        """The over of the Talk hold: the first over whose end (tester clock) lies between the press and the release + late_s (a later end is the
        echo's own over, taken when the original end was not caught). {'end_wall_s', 'start_wall_s', 'duration_s', 'start_source', 'end_ms',
        'all_ends_wall_s', 'polls'} or None."""
        usable = [o for o in self.overs if o["skew_ms"] is not None]
        ends = [round((o["end_ms"] - o["skew_ms"]) / 1000.0, 3) for o in usable]
        for o, end_wall in zip(usable, ends):
            if not press_wall_s - early_s <= end_wall <= release_wall_s + late_s:
                continue
            if o["start_ms"] is not None:
                start_wall, source = (o["start_ms"] - o["skew_ms"]) / 1000.0, "state." + str(o["start_key"])
            elif o["first_onair_wall_s"] is not None:
                start_wall, source = o["first_onair_wall_s"] - (o["poll_interval_s"] or 0.5), "first poll on air less one interval"
            else:
                start_wall, source = None, "unknown"
            if o["duration_ms_field"] is not None:
                dur = o["duration_ms_field"] / 1000.0
            else:
                dur = None if start_wall is None else end_wall - start_wall
            return {"end_wall_s": end_wall, "start_wall_s": start_wall, "start_source": source, "end_ms": o["end_ms"],
                    "duration_s": None if dur is None else round(max(dur, 0.0), 2), "all_ends_wall_s": ends, "polls": self.polls}
        return None


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
    meta = {p["id"]: p for p in (dump or {}).get("pcs") or []}
    for s in (dump or {}).get("samples") or []:                  # a pc the dump's list lacks is known from its samples
        for q in s.get("pcs") or []:
            if q.get("id") is not None and q["id"] not in meta:
                meta[q["id"]] = {"id": q["id"], "created_at": None, "closed_at": float(s["t"]) if q.get("stale") else None}
    for p in [meta[k] for k in sorted(meta)]:
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


# ------------------------------------------------------------------------- TURN-only relay (DVT_TALK_ECHO_RELAY=1)
# Production goes through the public HTTPS proxy and TURN over the internet; the rig is a LAN. With DVT_TALK_ECHO_RELAY=1 every
# RTCPeerConnection the page creates gets iceTransportPolicy 'relay' (a test-side override of the page's configuration), so the
# media must go through the rig's coturn (--profile web,browser,nat with turn.toml). The relay pair is then visible in the
# recorder's selected-pair stats and in the coturn log.
RELAY_ENV = "DVT_TALK_ECHO_RELAY"
RELAY_JS = """
(() => {
  const Orig = window.RTCPeerConnection;
  if (!Orig || Orig.__dvtRelay) return;
  function Relay(cfg, ...rest) {
    const c = Object.assign({}, cfg || {}, { iceTransportPolicy: 'relay' });
    return new Orig(c, ...rest);
  }
  Relay.prototype = Orig.prototype;
  Object.setPrototypeOf(Relay, Orig);
  Relay.__dvtRelay = true;
  window.RTCPeerConnection = Relay;
})();
"""


def relay_forced(env):
    return str(env.get(RELAY_ENV, "")).strip() in ("1", "true", "yes", "on")
