# SPDX-License-Identifier: MIT
"""Many modules talking at once (testlist C9), on xlxd-ref and xlxd-sut of
the docker rig.

N modules (DVT_LOAD_MODULES, default 26: A..Z) each get one talker and one
listener of every kind (D-STAR, DMR, M17); all talkers on both reflectors
start at the same instant. ROUNDS rounds rotate the talker kind, so every
module carries every source mode once. Each module keys its own Morse
text on its own tone frequency, encoded once by transcoder-ref, and every
client has its own identity (STRnnn / 1023100+n, rig/config/dmrid.dat),
so loss, truncation and cross-module leakage are all visible.

Every row (round, module, reflector, listener) records sent/received
frames, lost frames, sha256, origin identities, latency (talker send ->
first/last voice packet), arrival jitter and the tester's send lateness;
$DVT_OUT/load.json adds CPU/memory per container ($DVT_OUT/stats.jsonl,
sampled on the host by run.sh --profile load), the tester/host saturation
probes and the transcoder findings of the capture; a summary goes to the
terminal.

Upstream xlxd itself drops and delays frames with many modules talking, so
the default verdict is differential (xlxd-sut against xlxd-ref):
  - every client links; no listener sees another module's identity
  - transcoder-sut capture: no ambed.no_reply / reply_missing (--capture)
  - a cell is complete when the listener got the intact stream in one
    unbroken run: passthrough the sent frames, cross the sent frames run
    through a fresh transcoder-ref stream (lost = frames of it missing)
  - cells complete on both reflectors: sut sha256 == ref sha256
  - the Morse text decoded from every sut cross stream is recorded only:
    a complete cell already equals the transcoder's own output, and whether
    tandem coding keeps 20 wpm keying is a codec property (AMBE -> Codec2
    3200 never does, vambed-neo often not into AMBE either)
  - per round and module: lost frames sut <= ref + LOSS_TOL
  - across modules: sut does not lose more than ref systematically (one-sided
    sign test over (round, module) pairs with unequal loss, p < SIGN_ALPHA
    fails)
Declared divergence no_load_loss (DVT_SUT_DIVERGENCES, target.divergences) adds absolute
checks on xlxd-sut: passthrough exact with arrival jitter <= 60 ms, cross
loss only in rounds where the transcoder-sut capture shows BUSY or a reply
deadline miss, first-packet latency p50/p99 <= baselines/<sut>/load.json
+ LAT_MARGIN (written with --update-baseline).

Forwarding latency xlxd-sut adds is measured on the wire from its own
capture (same-mode cells: egress minus matching ingress); under
DVT_SUT_PROFILE=dvrouter its p99 must be <= DVROUTER_P99_MS per protocol
(DV-Router core gate), under xlxd (jitter buffer) it is recorded only.

Preconditions: each transcoder must take one stream per module (opened and
closed once before the load; a BUSY skips the verdict tests with the cap).
A saturated tester or host (send lateness, socket drops, host CPU steal or
idle, tester CPU, inbound capture loss) makes the result inconclusive:
the verdict tests skip with the numbers.

Run (the tester drives every client, no load-node replicas):
  DVT_LOAD_NODES=0 scripts/run.sh -m load --profile load --capture
Results: $DVT_OUT/load.json.
"""
import difflib
import math
import hashlib
import json
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dvtest import check, clients, pcap
from dvtest.audio import morse, signals
from dvtest.net import UdpClient
from dvtest.proto import ambed, dextra, dmr, m17, usrp

pytestmark = pytest.mark.load

ROOT = Path(__file__).resolve().parents[1]

ALL_MODULES = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
KINDS = clients.KINDS
ROUNDS = 3
WPM = 20
# identities STR001.. / 1023101..: 4 per module (talker, one listener per kind)
ID_BASE = 1023100
ROLES = 4
# nominal spacing of forwarded voice packets per listener kind
NOMINAL_GAP = {"dstar": 0.020, "dmr": 0.060, "m17": 0.040, "usrp": 0.020}
USRP_MODULE = "C"
USRP_PEER_PORT, USRP_XLXD_PORT = 32003, 34003   # rig/config/xlxd.usrp.in
WORKERS = 256
# quiet time between the last link and the talk: a DMR link is an empty
# stream on the module, and xlxd keeps a module busy (late entry, stream
# expiry) for up to ~2 s after it; a talk that starts then loses its head
SETTLE_S = 3.0
CODEC_WORKERS = 6
DECODE_GAP = 0.005
KEEPALIVE_S = 8.0
TAIL_S = 1.5
# differential tolerance: lost frames per round and module (all listeners)
# that xlxd-sut may exceed xlxd-ref by. Noise from A/A runs (xlxd-sut built
# from the reference source, XLXD_SUT_DIR=ref), 26 modules, 3 runs per
# transcoder image (out/20261029T1704..1725Z-i12): max(sut - ref) 14 frames
# per round and module (p99 |sut - ref| 13-14); tolerance = max + 4
LOSS_TOL = 18
# sign test: sut losing more than ref on this share of modules by chance
SIGN_ALPHA = 0.01
NO_LOAD_LOSS = "no_load_loss"
# no_load_loss: arrival jitter limit, 3 frame periods
GAP_LIMIT_MS = 60.0
# no_load_loss: latency may exceed the baseline by max(abs ms, rel share)
LAT_MARGIN = (20.0, 0.25)
# saturation: tester send lateness, host CPU steal / idle share, tester CPU
SAT_SEND_LATE_MS = 40.0
SAT_STEAL = 0.05
SAT_IDLE = 0.10
SAT_TESTER_CPU = 90.0
CAPTURE_FLUSH_S = 2.0
# DV-Router core gate: forwarding latency the sut adds, p99, measured on the
# wire in the xlxd-sut capture (same-mode cells only, see wire_latency)
DVROUTER_P99_MS = 2.0
# wire matching: an egress frame matches the first equal ingress frame
# within this many packets after the previous match
MATCH_WINDOW = 12
XLXD_PORTS = {dextra.PORT: "dstar", dmr.PORT: "dmr", m17.PORT: "m17"}


def module_count():
    n = int(os.environ.get("DVT_LOAD_MODULES", str(len(ALL_MODULES))))
    if not 1 <= n <= len(ALL_MODULES):
        pytest.fail("DVT_LOAD_MODULES=%d is outside 1..%d" % (n, len(ALL_MODULES)))
    return n


def identity(m, role):
    n = m * ROLES + role + 1
    return "STR%03d" % n, ID_BASE + n


def freq_of(m):
    # 750..1125 Hz: tones below do not survive AMBE -> Codec2, tones above
    # 1300 Hz not Codec2 -> AMBE (Morse decodes to nothing)
    return 750 + 15 * m


def sha(frames):
    return hashlib.sha256(b"".join(frames)).hexdigest()


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1))))]


def ms(value):
    return None if value is None else round(value * 1000.0, 2)


def make(kind, module, m, role):
    callsign, dmrid = identity(m, role)
    if kind == "dstar":
        return dextra.DstarClient("0.0.0.0", callsign=callsign, module=module)
    if kind == "dmr":
        return dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=module)
    return m17.M17Client("0.0.0.0", callsign=callsign, module=module)


def voice_times(kind, client):
    """Arrival times of forwarded voice packets."""
    out = []
    for data, stamp in zip(client.inbox, client.inbox_times):
        if kind == "dstar":
            parsed = dextra.parse_voice(data)
            ok = parsed is not None and not parsed[2]
        elif kind == "dmr":
            ok = dmr.is_voice(data)
        elif kind == "m17":
            ok = m17.parse(data) is not None
        else:
            ok = usrp.is_voice(data) and len(data) == usrp.VOICE_SIZE
        if ok:
            out.append(stamp)
    return out


def identities(kind, client):
    """Origin identities in the packets a listener got: D-STAR header MY,
    DMRD source id, M17 source callsign (first word)."""
    seen = set()
    for data in client.inbox:
        if kind == "dstar":
            header = dextra.parse_header(data)
            if header:
                seen.add(header["my"].decode("ascii", "replace").split(" ")[0].strip())
        elif kind == "dmr":
            parsed = dmr.parse(data)
            if parsed:
                seen.add(str(parsed["src"]))
        else:
            parsed = m17.parse(data)
            if parsed:
                seen.add(parsed["src"].split(" ")[0])
    return seen


def owner_of(ident):
    """Module index that owns an identity string, or None if unknown."""
    if ident.startswith("STR") and ident[3:].isdigit():
        n = int(ident[3:])
    elif ident.isdigit() and int(ident) > ID_BASE:
        n = int(ident) - ID_BASE
    else:
        return None
    return (n - 1) // ROLES


def missing(expected, got):
    """Where frames of expected are absent from got: head, middle, tail."""
    blocks = [b for b in difflib.SequenceMatcher(None, expected, got, autojunk=False).get_matching_blocks()
              if b.size]
    if not blocks:
        return {"head": len(expected), "middle": 0, "tail": 0}
    matched = sum(b.size for b in blocks)
    head = blocks[0].a
    tail = len(expected) - (blocks[-1].a + blocks[-1].size)
    return {"head": head, "middle": len(expected) - matched - head - tail, "tail": tail}


def timing(arrivals, stamps, kind, t_talk=None):
    """Latency talker send -> listener arrival of the first and last voice
    packet (talker send stamps when the client keeps them, else from the
    talk call: first_after_talk_ms), and arrival jitter."""
    row = {"first_ms": None, "last_ms": None}
    if arrivals and stamps:
        row["first_ms"] = ms(arrivals[0] - stamps[0])
        row["last_ms"] = ms(arrivals[-1] - stamps[-1])
    elif arrivals and t_talk is not None:
        row["first_after_talk_ms"] = ms(arrivals[0] - t_talk)
    dev = [abs(b - a - NOMINAL_GAP[kind]) for a, b in zip(arrivals, arrivals[1:])]
    row["jitter_p50_ms"] = ms(pct(dev, 50))
    row["jitter_p95_ms"] = ms(pct(dev, 95))
    row["jitter_max_ms"] = ms(max(dev) if dev else None)
    return row


def send_lateness(stamps, kind):
    """How far behind its 20/40/60 ms schedule the tester sent: p95/max ms."""
    if not stamps:
        return {}
    gap = NOMINAL_GAP[kind]
    late = [t - (stamps[0] + i * gap) for i, t in enumerate(stamps)]
    return {"send_late_p95_ms": ms(pct(late, 95)), "send_late_max_ms": ms(max(late))}


class Unit:
    """Talker plus one listener per kind for one module on one reflector."""

    def __init__(self, side, ip, m, module, talker_kind):
        self.side, self.ip, self.m, self.module = side, ip, m, module
        self.talker_kind = talker_kind
        self.talker = make(talker_kind, module, m, 0)
        self.listeners = {k: make(k, module, m, 1 + i) for i, k in enumerate(KINDS)}
        self.linked = {}
        self.error = None
        self.sid = None
        self.stamps = None
        self.t_talk = None

    def all(self):
        return [("talker", self.talker_kind, self.talker)] + \
            [("listen-" + k, k, c) for k, c in self.listeners.items()]


def run_round(rig, mods, rnd, encoded, usrp_on):
    sides = (("ref", rig.xlxd_ref), ("sut", rig.xlxd_sut))
    units = [Unit(side, ip, m, mod, KINDS[(m + rnd) % len(KINDS)])
             for side, ip in sides for m, mod in enumerate(mods)]
    peer = UdpClient("0.0.0.0", port=USRP_PEER_PORT) if usrp_on else None
    stop = threading.Event()
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            jobs = [(u, role, pool.submit(c.link, u.ip)) for u in units for role, _k, c in u.all()]
            for u, role, job in jobs:
                try:
                    u.linked[role] = bool(job.result())
                except OSError as exc:
                    u.linked[role] = False
                    u.error = repr(exc)
        listeners = [c for u in units for c in u.listeners.values()]
        sockets = [c.client for c in listeners] + ([peer] if peer else [])
        deadline = time.monotonic() + SETTLE_S
        while time.monotonic() < deadline:
            for client in sockets:
                client.drain()
            time.sleep(0.01)
        for client in sockets:
            client.inbox.clear()
            client.inbox_times.clear()

        def pump():
            last = time.monotonic()
            while not stop.is_set():
                for c in listeners:
                    c.client.drain()
                if peer:
                    peer.drain()
                if time.monotonic() - last >= KEEPALIVE_S:
                    last = time.monotonic()
                    for c in listeners:
                        if hasattr(c, "keepalive") and c.dest:
                            c.keepalive()
                time.sleep(0.005)

        pumper = threading.Thread(target=pump, daemon=True)
        pumper.start()
        talkers = [u for u in units if u.linked.get("talker")]
        barrier = threading.Barrier(len(talkers)) if talkers else None

        def talk(u):
            try:
                barrier.wait(timeout=60)
                u.t_talk = time.monotonic()
                u.sid = clients.talk(u.talker, u.talker_kind, encoded[u.m]["frames"][u.talker_kind])
                u.stamps = getattr(u.talker, "last_stamps", None)
            except Exception as exc:            # reported with the round's rows
                u.error = "%s %s talk: %r" % (u.side, u.module, exc)

        threads = [threading.Thread(target=talk, args=(u,)) for u in talkers]
        cpu0 = cpu_times()
        wall0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(TAIL_S)
        stop.set()
        pumper.join()
        wall1 = time.time()
        probe = {"cpu": cpu_share(cpu0, cpu_times()), "udp_drops": udp_drops()}
        rows = []
        for u in units:
            sent = encoded[u.m]["frames"][u.talker_kind]
            for k, c in u.listeners.items():
                got = c.collect(settle=0)
                seen = identities(k, c.client)
                row = {"round": rnd, "module": u.module, "side": u.side, "src": u.talker_kind, "dst": k,
                       "kind": "passthrough" if k == u.talker_kind else "cross",
                       "linked": u.linked.get("talker") and u.linked.get("listen-" + k),
                       "sent": len(sent), "received": len(got), "sha256": sha(got),
                       "identities": sorted(seen),
                       "talker_port": u.talker.client.local[1], "listener_port": c.client.local[1],
                       "foreign": sorted(i for i in seen if owner_of(i) not in (u.m,))}
                if k == u.talker_kind:
                    want = clients.wire_frames(k, sent)
                    row["exact"] = got == want
                    if not row["exact"]:
                        row["missing"] = missing(want, got)
                row.update(timing(voice_times(k, c.client), u.stamps, k, u.t_talk))
                row.update(send_lateness(u.stamps, u.talker_kind))
                rows.append((row, got))
        if peer:
            m = mods.index(USRP_MODULE)
            pcm = [usrp.parse(p).payload for p in peer.inbox
                   if usrp.is_voice(p) and len(p) == usrp.VOICE_SIZE]
            src = KINDS[(m + rnd) % len(KINDS)]
            row = {"round": rnd, "module": USRP_MODULE, "side": "sut", "src": src, "dst": "usrp",
                   "kind": "usrp", "linked": True,
                   "sent": len(encoded[m]["frames"][src]), "received": len(pcm), "sha256": sha(pcm),
                   "identities": [], "foreign": []}
            sut = [u for u in units if u.side == "sut" and u.m == m][0]
            row.update(timing(voice_times("usrp", peer), sut.stamps, "usrp", sut.t_talk))
            rows.append((row, pcm))
        return rows, (wall0, wall1), [u.error for u in units if u.error], probe
    finally:
        stop.set()
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(lambda c: clients.close(c), [c for u in units for _r, _k, c in u.all()]))
        if peer:
            peer.close()


def resources(out, window):
    """min/mean/max CPU % and memory per service from $DVT_OUT/stats.jsonl
    samples inside the window (host side docker stats sampling)."""
    path = Path(out) / "stats.jsonl"
    if not path.exists():
        return {"missing": "no stats.jsonl (rig does not sample docker stats)"}
    per = {}
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not window[0] <= item.get("t", 0) <= window[1]:
            continue
        per.setdefault(item.get("service", "?"), []).append(item)
    out_rows = {}
    for service, items in sorted(per.items()):
        cpu = [float(i["cpu_pct"]) for i in items if "cpu_pct" in i]
        mem = [int(i["mem_bytes"]) for i in items if "mem_bytes" in i]
        out_rows[service] = {
            "samples": len(items),
            "cpu_pct": {"mean": round(sum(cpu) / len(cpu), 1), "max": max(cpu)} if cpu else None,
            "mem_mib": {"mean": round(sum(mem) / len(mem) / 2 ** 20, 1),
                        "max": round(max(mem) / 2 ** 20, 1)} if mem else None}
    return out_rows


def summarize(rows):
    """Latency/jitter percentiles per (side, dst kind) over all rows."""
    groups = {}
    for row in rows:
        groups.setdefault("%s/%s" % (row["side"], row["dst"]), []).append(row)
    out = {}
    for key, items in sorted(groups.items()):
        entry = {"streams": len(items)}
        for field in ("first_ms", "last_ms", "first_after_talk_ms", "jitter_p95_ms", "jitter_max_ms",
                      "send_late_max_ms"):
            values = [r[field] for r in items if r.get(field) is not None]
            entry[field] = {"p50": pct(values, 50), "p95": pct(values, 95),
                            "max": max(values) if values else None}
        out[key] = entry
    return out


@pytest.fixture(scope="module")
def mods(rig):
    if os.environ.get("DVT_LOAD") != "on":
        pytest.skip("no load rig: scripts/run.sh --profile load (DVT_LOAD=on)")
    return ALL_MODULES[:module_count()]


@pytest.fixture(scope="module")
def usrp_on(mods):
    return os.environ.get("DVT_SUT_USRP") == "on" and USRP_MODULE in mods


@pytest.fixture(scope="module")
def encoded(rig, mods):
    """{m: {"text", "freq", "frames": {kind: frames}}}: each module's Morse
    text on its own tone, frame count a multiple of 6 (whole DMR bursts and
    M17 pairs), encoded by transcoder-ref."""
    rng = random.SystemRandom()

    def one(m):
        text = "%s%s %02d" % (mods[m], mods[m], rng.randrange(100))
        pcm = signals.frames(morse.encode(text, wpm=WPM, freq=freq_of(m)))
        pcm += [bytes(signals.FRAME_BYTES)] * (-len(pcm) % 6)
        out = ambed.transcode(rig.ambed_ref, ambed.PCM, [clients.CODEC[k] for k in KINDS], pcm,
                              port=rig.ambed_port, transaction=0x4C440000 + m)
        return m, {"text": text, "freq": freq_of(m),
                   "frames": {k: out[clients.CODEC[k]] for k in KINDS}}

    with ThreadPoolExecutor(CODEC_WORKERS) as pool:
        return dict(pool.map(one, range(len(mods))))


def decode_pcm(rig, codec, frames, transaction):
    return b"".join(ambed.transcode(rig.ambed_ref, codec, [ambed.PCM], frames, port=rig.ambed_port,
                                    transaction=transaction, gap=DECODE_GAP)[ambed.PCM])


def cpu_times():
    """Host-wide jiffies of the first /proc/stat line (the tester shares
    the host kernel): user nice system idle iowait irq softirq steal."""
    try:
        with open("/proc/stat") as fh:
            return [int(v) for v in fh.readline().split()[1:9]]
    except OSError:
        return None


def cpu_share(a, b):
    if not a or not b:
        return None
    delta = [y - x for x, y in zip(a, b)]
    total = sum(delta) or 1
    return {"idle": round((delta[3] + delta[4]) / total, 3), "steal": round(delta[7] / total, 3)}


def udp_drops():
    """Datagrams the kernel dropped on the tester's UDP sockets (last
    column of /proc/net/udp, per socket; the round's sockets are open)."""
    total = 0
    try:
        with open("/proc/net/udp") as fh:
            for line in fh.readlines()[1:]:
                total += int(line.split()[-1])
    except (OSError, ValueError, IndexError):
        return None
    return total


def stream_cap(addr, port, n):
    """Streams the transcoder accepts at once, up to n: opens n extended
    streams (AMBE+ -> AMBE2+, Codec2), counts STD until the first BUSY and
    closes them again."""
    client = ambed.AmbedClient(addr, port)
    opened = []
    try:
        for i in range(n):
            std = ambed.parse_std(client.rpc(ambed.open_ext("N0TEST", ambed.AMBEPLUS,
                                                            [ambed.AMBE2PLUS, ambed.CODEC2_3200],
                                                            0x4C430000 + i)) or b"")
            if std is None:
                break
            opened.append(std.stream_id)
    finally:
        for stream_id in opened:
            client.close_stream(stream_id)
        client.close()
    return len(opened)


def contains(got, want):
    """want arrives in got as one unbroken run (xlxd may add framing
    frames around it: M17 EOT pair, DMR burst padding)."""
    if not want:
        return True
    blob, needle, width = b"".join(got), b"".join(want), len(want[0])
    at = blob.find(needle)
    while at >= 0 and at % width:
        at = blob.find(needle, at + 1)
    return at >= 0


def lost_of(row):
    """Frames of the intact stream that did not arrive (USRP: by count)."""
    if row["kind"] == "usrp":
        return max(0, row["sent"] - row["received"])
    return sum(row["missing"].values()) if row.get("missing") else 0


def complete(row):
    return row["received"] >= row["sent"] if row["kind"] == "usrp" else row["exact"]


def capture_report(out, windows):
    """Capture findings that matter here (None without --capture):
    transcoder-sut no_reply/reply_missing and BUSY per round, inbound
    (tester -> xlxd) sequence loss per xlxd capture."""
    directory = Path(out) / "capture"
    if not directory.is_dir():
        return None
    time.sleep(CAPTURE_FLUSH_S)
    rig_cfg = check.rig_config()
    tester = rig_cfg["hosts"]["tester"]
    report = {"ambed_no_reply": 0, "busy_rounds": [], "miss_rounds": [], "inbound_loss": {}, "unreadable": []}

    def round_of(ts):
        for rnd, (w0, w1) in enumerate(windows):
            if ts is not None and w0 - SETTLE_S <= ts <= w1 + CAPTURE_FLUSH_S:
                return rnd
        return None

    for stem in ("transcoder-sut", "xlxd-sut", "xlxd-ref"):
        path = directory / (stem + ".pcapng")
        if not path.exists():
            continue
        try:
            result = check.check_file(str(path), rig_cfg)
            stamps = {d.frame: d.ts for d in pcap.udp_datagrams(str(path), {})}
        except (pcap.CaptureError, OSError, ValueError) as exc:
            report["unreadable"].append("%s: %s" % (stem, exc))
            continue
        if stem == "transcoder-sut":
            for f in result["findings"]:
                if f["code"] in ("ambed.no_reply", "ambed.reply_missing"):
                    report["ambed_no_reply"] += 1
                    report["miss_rounds"].append(round_of(stamps.get(f["frame"])))
            for d in pcap.udp_datagrams(str(path), {}):
                if d.sport == ambed.PORT and ambed.is_busy(d.payload):
                    report["busy_rounds"].append(round_of(d.ts))
        else:
            report["inbound_loss"][stem] = sum(
                1 for f in result["findings"]
                if f["code"].endswith("seq_loss") and f["flow"].startswith(tester + ":"))
    report["busy_rounds"] = sorted({r for r in report["busy_rounds"] if r is not None})
    report["miss_rounds"] = sorted({r for r in report["miss_rounds"] if r is not None})
    return report


def saturation(rows, probes, res, captured):
    """Reasons the tester or host, not the reflector, may have lost frames."""
    reasons = []
    late = max((r.get("send_late_max_ms") or 0.0 for r in rows), default=0.0)
    if late > SAT_SEND_LATE_MS:
        reasons.append("tester send lateness %.1f ms > %.0f ms" % (late, SAT_SEND_LATE_MS))
    for rnd, probe in enumerate(probes):
        if probe["udp_drops"]:
            reasons.append("round %d: %d datagrams dropped on tester sockets" % (rnd, probe["udp_drops"]))
        cpu = probe["cpu"]
        if cpu and cpu["steal"] > SAT_STEAL:
            reasons.append("round %d: host CPU steal %.1f%%" % (rnd, 100 * cpu["steal"]))
        if cpu and cpu["idle"] < SAT_IDLE:
            reasons.append("round %d: host CPU idle %.1f%%" % (rnd, 100 * cpu["idle"]))
    tester = (res or {}).get("tester") or {}
    if (tester.get("cpu_pct") or {}).get("max", 0) > SAT_TESTER_CPU:
        reasons.append("tester container CPU %.0f%%" % tester["cpu_pct"]["max"])
    for stem, count in ((captured or {}).get("inbound_loss") or {}).items():
        if count:
            reasons.append("%s capture: %d inbound sequence gaps from the tester" % (stem, count))
    return reasons


def module_loss(rows):
    """{(round, module): {"ref": lost, "sut": lost}} over the xlxd listeners."""
    lost = {}
    for r in rows:
        if r["kind"] != "usrp":
            lost.setdefault((r["round"], r["module"]), {"ref": 0, "sut": 0})[r["side"]] += r["lost"]
    return lost


def sign_test(rows):
    """One-sided sign test: P(at least `worse` of the unequal pairs have
    sut > ref) if either side is as likely to lose more."""
    diffs = [v["sut"] - v["ref"] for v in module_loss(rows).values()]
    worse, better = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    n = worse + better
    p = sum(math.comb(n, k) for k in range(worse, n + 1)) / 2 ** n if n else 1.0
    return {"worse": worse, "better": better, "equal": len(diffs) - n, "p": round(p, 5)}


def voice_unit(kind, payload):
    """Comparable voice content of one packet, or None for non-voice."""
    if kind == "dstar":
        parsed = dextra.parse_voice(payload)
        return parsed[3] if parsed and not parsed[2] else None
    if kind == "dmr":
        return tuple(dmr.extract(payload[20:53])) if dmr.is_voice(payload) else None
    parsed = m17.parse(payload)
    return tuple(parsed["frames"]) if parsed else None


def wire_latency(out, rows, windows):
    """Forwarding latency xlxd-sut adds, from its own capture (the sniffer
    shares its network namespace, one clock): per relayed voice packet of a
    same-mode cell, egress time minus the time of the matching ingress
    packet of the talker's stream (same voice content, in stream order).
    Cross-mode cells are left out: their egress waits for the transcoder
    round trip, which is not the reflector's forwarding time.
    {pair: {"packets", "unmatched", "p50", "p99", "max"} (ms)} or None."""
    path = Path(out) / "capture" / "xlxd-sut.pcapng"
    if not path.exists():
        return None
    hosts = check.rig_config()["hosts"]
    tester, sut = hosts["tester"], hosts["xlxd_sut"]
    ingress, egress = {}, {}
    try:
        for d in pcap.udp_datagrams(str(path), {}):
            if d.src == tester and d.dst == sut and d.dport in XLXD_PORTS:
                unit = voice_unit(XLXD_PORTS[d.dport], d.payload)
                if unit is not None:
                    ingress.setdefault(d.sport, []).append((d.ts, unit))
            elif d.src == sut and d.dst == tester and d.sport in XLXD_PORTS:
                unit = voice_unit(XLXD_PORTS[d.sport], d.payload)
                if unit is not None:
                    egress.setdefault(d.dport, []).append((d.ts, unit))
    except (pcap.CaptureError, OSError, ValueError) as exc:
        return {"unreadable": str(exc)}
    pairs = {}
    for row in rows:
        if row["side"] != "sut" or row["kind"] != "passthrough":
            continue
        w0, w1 = windows[row["round"]]
        inside = lambda items: [x for x in items if w0 - SETTLE_S <= x[0] <= w1]    # noqa: E731
        ing, eg = inside(ingress.get(row["talker_port"], [])), inside(egress.get(row["listener_port"], []))
        entry = pairs.setdefault("%s-%s" % (row["src"], row["dst"]), {"deltas": [], "unmatched": 0})
        i = 0
        for t_out, unit in eg:
            for j in range(i, min(i + MATCH_WINDOW, len(ing))):
                if ing[j][1] == unit:
                    entry["deltas"].append(t_out - ing[j][0])
                    i = j + 1
                    break
            else:
                entry["unmatched"] += 1
    out_rows = {}
    for pair, entry in sorted(pairs.items()):
        deltas = entry["deltas"]
        out_rows[pair] = {"packets": len(deltas), "unmatched": entry["unmatched"],
                          "p50": ms(pct(deltas, 50)), "p99": ms(pct(deltas, 99)),
                          "max": ms(max(deltas)) if deltas else None}
    return out_rows


def sut_name():
    name = os.environ.get("DVT_SUT_NAME")
    if name:
        return name
    fetched = ROOT / ".cache" / "xlxd" / "sut.fetched"
    return "xlxd-" + fetched.read_text().split()[2][:7] if fetched.exists() else None


def latency(rows):
    """First-packet latency p50/p99 (ms) per listener kind on xlxd-sut."""
    out = {}
    for kind in KINDS + ("usrp",):
        values = [r["first_ms"] for r in rows if r["side"] == "sut" and r["dst"] == kind and r["first_ms"] is not None]
        if values:
            out[kind] = {"p50": pct(values, 50), "p99": pct(values, 99)}
    return out


@pytest.fixture(scope="module")
def caps_ok(rig, mods):
    """Both transcoders take one stream per module."""
    caps = {side: stream_cap(addr, rig.ambed_port, len(mods))
            for side, addr in (("ref", rig.ambed_ref), ("sut", rig.ambed_sut))}
    return caps


@pytest.fixture(scope="module")
def load(request, rig, mods, usrp_on, encoded, caps_ok):
    t0 = time.time()
    rows, errors, windows, probes = [], [], [], []
    for rnd in range(ROUNDS):
        got_rows, window, errs, probe = run_round(rig, mods, rnd, encoded, usrp_on)
        rows += got_rows
        windows.append(window)
        errors += errs
        probes.append(probe)
    # Morse text of every sut cross stream (ref is compared by hash) and USRP PCM
    todo = [(i, row, frames) for i, (row, frames) in enumerate(rows)
            if (row["kind"] == "cross" and row["side"] == "sut" and frames) or row["kind"] == "usrp"]

    def morse_of(item):
        i, row, frames = item
        m = mods.index(row["module"])
        if row["kind"] == "usrp":
            pcm = b"".join(frames)
        else:
            pcm = decode_pcm(rig, clients.CODEC[row["dst"]], frames, 0x4C450000 + i)
        return i, morse.check(encoded[m]["text"], morse.decode(pcm))

    with ThreadPoolExecutor(CODEC_WORKERS) as pool:
        for i, result in pool.map(morse_of, todo):
            rows[i][0]["morse"] = {"exact": result["exact"], "got": result["got"], "cer": round(result["cer"], 3)}
    # what a cross listener gets from an intact stream: the frames xlxd
    # receives, run through a fresh transcoder-ref stream
    keys = sorted({(mods.index(r["module"]), r["src"], r["dst"]) for r, _f in rows if r["kind"] == "cross"})

    def expect(item):
        i, (m, src, dst) = item
        # every sent frame goes to the transcoder (M17 packet 0 included,
        # though a same-kind M17 listener does not get it)
        out = ambed.transcode(rig.ambed_ref, clients.CODEC[src], [clients.CODEC[dst]], encoded[m]["frames"][src],
                              port=rig.ambed_port, transaction=0x4C460000 + i, gap=DECODE_GAP)
        return (m, src, dst), out[clients.CODEC[dst]]

    with ThreadPoolExecutor(CODEC_WORKERS) as pool:
        expected = dict(pool.map(expect, enumerate(keys)))
    for row, frames in rows:
        if row["kind"] == "cross":
            want = expected[(mods.index(row["module"]), row["src"], row["dst"])]
            row["exact"] = contains(frames, want)
            if not row["exact"]:
                row["missing"] = missing(want, frames)
    plain = [row for row, _frames in rows]
    for row in plain:
        row["lost"] = lost_of(row)
        row["complete"] = complete(row)
    window = (min(w[0] for w in windows), max(w[1] for w in windows))
    res = resources(rig.out, window)
    captured = capture_report(rig.out, windows)
    wire = wire_latency(rig.out, plain, windows)
    sat = saturation(plain, probes, res, captured)
    lost = {side: sum(r["lost"] for r in plain if r["side"] == side) for side in ("ref", "sut")}
    short = {side: sum(1 for r in plain if r["side"] == side and not r["complete"]) for side in ("ref", "sut")}
    result = {"modules": len(mods), "rounds": ROUNDS, "wpm": WPM, "stream_cap": caps_ok,
              "declared": sorted(rig.divergences), "loss_tol": LOSS_TOL,
              "sign_test": sign_test(plain),
              "texts": {mods[m]: {"text": e["text"], "freq": e["freq"],
                                  "frames": len(e["frames"]["dstar"])} for m, e in encoded.items()},
              "talk_window_s": round(window[1] - window[0], 1), "elapsed_s": round(time.time() - t0, 1),
              "errors": errors, "lost_frames": lost, "incomplete_streams": short,
              "latency_first_ms": latency(plain), "timing": summarize(plain), "resources": res,
              "probes": probes, "capture": captured, "sut_profile": rig.sut_profile,
              "wire_latency_ms": wire, "saturation": sat, "rows": plain}
    out = Path(rig.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "load.json").write_text(json.dumps(result, indent=1) + "\n")
    terminal = request.config.pluginmanager.get_plugin("terminalreporter")
    capture = request.config.pluginmanager.get_plugin("capturemanager")
    if terminal and capture:
        cpu = {s: (res.get(s) or {}).get("cpu_pct") for s in ("xlxd-ref", "xlxd-sut", "transcoder-ref",
                                                             "transcoder-sut", "tester")} \
            if "missing" not in res else res
        for line in ("load: %d modules x %d rounds, %d rows, stream cap ref/sut %s" % (
                         len(mods), ROUNDS, len(plain), caps_ok),
                     "load: lost frames ref %(ref)d sut %(sut)d" % lost +
                     ", incomplete streams ref %(ref)d sut %(sut)d" % short,
                     "load: first-packet latency sut %s" % json.dumps(result["latency_first_ms"]),
                     "load: xlxd-sut forwarding latency on the wire (same-mode) %s" % json.dumps(wire),
                     "load: sign test sut>ref %(worse)d, sut<ref %(better)d, equal %(equal)d, p=%(p)g"
                     % result["sign_test"],
                     "load: Morse exact on sut cross/USRP %d of %d" % (
                         sum(1 for r in plain if (r.get("morse") or {}).get("exact")),
                         sum(1 for r in plain if "morse" in r)),
                     "load: cpu %s" % json.dumps(cpu),
                     "load: capture %s" % json.dumps(captured),
                     "load: saturation %s" % (sat or "none")):
            with capture.global_and_fixture_disabled():
                terminal.write_line(line)
    return result


@pytest.fixture(scope="module")
def verdict(load, mods):
    """Rows, unless the run cannot judge the reflector: a transcoder that
    caps below one stream per module, or a saturated tester/host."""
    capped = {side: n for side, n in load["stream_cap"].items() if n < len(mods)}
    if capped:
        pytest.skip("transcoder stream cap below %d modules: %s (raise its max streams)" % (len(mods), capped))
    if load["saturation"]:
        pytest.skip("inconclusive, tester/host saturated: %s" % "; ".join(load["saturation"]))
    return load["rows"]


def declared_no_load_loss(load):
    if NO_LOAD_LOSS not in load["declared"]:
        pytest.skip("absolute load checks need the declared divergence %s" % NO_LOAD_LOSS)


def label(row):
    return "round %d module %s %s %s->%s" % (row["round"], row["module"], row["side"], row["src"], row["dst"])


def test_all_linked(load):
    bad = [label(r) for r in load["rows"] if not r["linked"]]
    assert not bad, "%d listener/talker pairs not linked: %s" % (len(bad), bad[:8])


def test_no_foreign_streams(load):
    bad = ["%s: %s" % (label(r), r["foreign"]) for r in load["rows"] if r["foreign"]]
    assert not bad, "%d listeners saw other modules' clients: %s" % (len(bad), bad[:8])


def test_transcoder_replies(load):
    captured = load["capture"]
    if captured is None:
        pytest.skip("no capture: run.sh --capture records transcoder-sut")
    assert captured["ambed_no_reply"] == 0, "transcoder-sut left %d requests unanswered (rounds %s)" % (
        captured["ambed_no_reply"], captured["miss_rounds"])


def test_sut_equals_ref_where_complete(verdict):
    ref = {(r["round"], r["module"], r["dst"]): r for r in verdict if r["side"] == "ref"}
    bad = []
    for r in verdict:
        other = ref.get((r["round"], r["module"], r["dst"]))
        if r["side"] == "sut" and other and r["complete"] and other["complete"] and r["sha256"] != other["sha256"]:
            bad.append("%s: sut %d ref %d frames" % (label(r), r["received"], other["received"]))
    assert not bad, "%d complete cells: sut differs from ref: %s" % (len(bad), bad[:8])


def test_loss_not_worse_than_ref(verdict):
    bad = ["round %d module %s: sut %d ref %d" % (k[0], k[1], v["sut"], v["ref"])
           for k, v in sorted(module_loss(verdict).items()) if v["sut"] > v["ref"] + LOSS_TOL]
    assert not bad, "%d modules lost more than %d frames over xlxd-ref: %s" % (len(bad), LOSS_TOL, bad[:8])


def test_loss_not_systematically_worse(verdict):
    sign = sign_test(verdict)
    assert sign["p"] >= SIGN_ALPHA, \
        "sut lost more than ref on %(worse)d modules, less on %(better)d (sign test p=%(p)g)" % sign


def test_no_load_loss_passthrough(load, verdict):
    declared_no_load_loss(load)
    bad = ["%s: %d lost, exact %s, jitter max %s ms" % (label(r), r["lost"], r["exact"], r["jitter_max_ms"])
           for r in verdict if r["side"] == "sut" and r["kind"] == "passthrough"
           and not (r["exact"] and (r["jitter_max_ms"] or 0) <= GAP_LIMIT_MS)]
    assert not bad, "%d sut passthrough streams lost frames or stalled > %.0f ms: %s" % (
        len(bad), GAP_LIMIT_MS, bad[:8])


def test_no_load_loss_cross(load, verdict):
    declared_no_load_loss(load)
    captured = load["capture"]
    if captured is None:
        pytest.skip("no capture: cross loss is explained only by transcoder-sut BUSY / deadline misses")
    excused = set(captured["busy_rounds"]) | set(captured["miss_rounds"])
    bad = ["%s: %d of %d" % (label(r), r["received"], r["sent"])
           for r in verdict if r["side"] == "sut" and r["kind"] in ("cross", "usrp") and not r["complete"]
           and r["round"] not in excused]
    assert not bad, "%d sut cross streams lost frames without transcoder BUSY/deadline miss: %s" % (
        len(bad), bad[:8])


def test_no_load_loss_latency(load, verdict, update_baseline):
    declared_no_load_loss(load)
    name = sut_name()
    if not name:
        pytest.skip("no sut name: set DVT_SUT_NAME or fetch .cache/xlxd/sut")
    path = ROOT / "baselines" / name / "load.json"
    current = {"modules": load["modules"], "latency_first_ms": load["latency_first_ms"]}
    if update_baseline:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        return
    if not path.exists():
        pytest.skip("no baseline %s (--update-baseline writes it)" % path.relative_to(ROOT))
    known = json.loads(path.read_text())
    if known.get("modules") != load["modules"]:
        pytest.skip("baseline is for %s modules, run has %d" % (known.get("modules"), load["modules"]))
    bad = []
    for kind, now in current["latency_first_ms"].items():
        base = known["latency_first_ms"].get(kind)
        for q in ("p50", "p99"):
            if base and now[q] > base[q] + max(LAT_MARGIN[0], LAT_MARGIN[1] * base[q]):
                bad.append("%s %s %.1f ms > baseline %.1f ms" % (kind, q, now[q], base[q]))
    assert not bad, "first-packet latency over baseline + max(%.0f ms, %d%%): %s" % (
        LAT_MARGIN[0], 100 * LAT_MARGIN[1], bad)


def test_dvrouter_forwarding_latency(load, verdict):
    if load["sut_profile"] != "dvrouter":
        pytest.skip("forwarding latency gate is for DVT_SUT_PROFILE=dvrouter (xlxd: recorded only)")
    wire = load["wire_latency_ms"]
    if not wire or "unreadable" in wire:
        pytest.skip("no readable xlxd-sut capture: run.sh --capture (%s)" % wire)
    bad = ["%s p99 %.2f ms" % (pair, v["p99"]) for pair, v in wire.items()
           if v["p99"] is None or v["p99"] > DVROUTER_P99_MS]
    assert not bad, "sut-added forwarding latency over %.1f ms: %s" % (DVROUTER_P99_MS, bad)
