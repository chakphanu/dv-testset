# SPDX-License-Identifier: MIT
"""NXDN reflector peer conformance (docs/protocols/nxdn.md, testlist rows
P-NXD-NN): one case per obligation, xlxd-ref and xlxd-sut probed in
parallel.

Two paths per side (rig profile nxdn):
- the NXDNReflector (nxdn-ref / nxdn-sut) peered on DVT_NXDN_MODULE: the
  tester joins it as a gateway (NxdnClient), so payload-level probes reach
  the peer exactly as a real gateway's would;
- a second interlink row "NX<probe tg> <tester>:<port> <probe module>":
  the tester is the peer's reflector (FakeReflector), so it sees and times
  every poll, can withhold echoes and can send what NXDNReflector filters
  (private calls, other TGs, garbage).

Judged as in nxdn.md: hb = spec or what every client needs, asserted on
both sides; diff = sut == ref; dev = sut == ref under the xlxd profile,
else the profile's "nxdn" spec entry {"P-NXD-NN": {key: value}} (skip
"spec pending" without one). Every case prints what both sides did.
"""
import collections
import json
import os
import threading
import time
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.net import pace
from dvtest.proto import dextra, dmr, nxdn

pytestmark = [pytest.mark.proto, pytest.mark.rig, pytest.mark.nxdn]

FRAMES = 60                                     # 15 NXDN packets, 20 DMR bursts
TALKER_DMR, UNKNOWN_DMR = 1023007, 1023101      # N0CALL (NXDN 1001), STR001 (no NXDN id)
GW_IDS = (1001, 1002)                           # N0CALL, N0TEST in rig/config/nxdn.csv
UNKNOWN_NXDN = 4242                             # in no nxdn.csv
SETTLE = 1.5
RESULTS = {}


# ---------------------------------------------------------------- rig

class Side:
    def __init__(self, name, xlxd, reflector, tg, module, probe_tg, probe_module, probe_port):
        self.name, self.xlxd, self.reflector, self.tg, self.module = name, xlxd, reflector, tg, module
        self.probe_tg, self.probe_module, self.probe_port = probe_tg, probe_module, probe_port
        self.probe = None
        self.stop = threading.Event()

    def start_probe(self):
        self.probe = nxdn.FakeReflector("0.0.0.0", self.probe_port, self.probe_tg)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        # answers the peer's polls all the time; cases only read the lists
        while not self.stop.is_set():
            self.probe.client.drain()
            time.sleep(0.02)

    def probe_send(self, packets, gap=nxdn.PACKET_GAP):
        start = time.monotonic()
        for index, data in enumerate(packets):
            pace(start, index, gap)
            self.probe.send(data)


def both(sides, func, *args):
    """{"ref": func(side, *args), "sut": ...} run in parallel."""
    got = {}

    def run(side):
        try:
            got[side.name] = func(side, *args)
        except Exception as exc:                    # reported below
            got[side.name] = exc

    threads = [threading.Thread(target=run, args=(s,)) for s in sides]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for name in ("ref", "sut"):
        assert not isinstance(got[name], Exception), "%s: %r" % (name, got[name])
    record({n: {k: v for k, v in got[n].items() if not k.startswith("_")} for n in ("ref", "sut")})
    return got


def record(value, key=None):
    """Print value and keep it in $DVT_OUT/nxdn_peer.json under the case id
    (plus key), so the measured xlxd column can be read back."""
    print(value)
    case = os.environ.get("PYTEST_CURRENT_TEST", "case").split(" ")[0].split("::")[-1]
    RESULTS[case + ("/" + key if key else "")] = value
    out = os.environ.get("DVT_OUT")
    if out:
        with open(os.path.join(out, "nxdn_peer.json"), "w") as fh:
            json.dump(RESULTS, fh, indent=1, sort_keys=True, default=str)


def judge_dev(rig, nn, got, *keys):
    """dev items: sut == ref on keys under the xlxd profile, else the
    profile's nxdn spec entry for nn; skip while it is pending."""
    if rig.sut_profile == specs.DEFAULT:
        same(got, *keys)
        return
    entry = (specs.rule(rig, "nxdn") or {}).get(nn) or {}
    for key in keys:
        if key in entry:
            assert got["sut"][key] == entry[key], "%s %s: sut %r, spec %r" % (nn, key, got["sut"][key], entry[key])
    pending = [key for key in keys if key not in entry]
    if pending:
        pytest.skip("spec pending: no nxdn %s %s entry in dvtest/specs/%s.py" % (nn, pending, rig.sut_profile))


def mark(rig, phase, tag):
    """dvcheck mark window around deliberate probe traffic (no-op without
    the control channel)."""
    if os.environ.get("DVT_RIG_CONTROL") != "1":
        return
    directory = Path(rig.out) / "control"
    name = "nxdn-mark-%d-%d" % (os.getpid(), time.monotonic_ns())
    (directory / (name + ".tmp")).write_text("mark %s %s\n" % (phase, tag))
    (directory / (name + ".tmp")).rename(directory / (name + ".req"))
    deadline = time.monotonic() + 5
    while not (directory / (name + ".ack")).exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def marked(rig, tag, sides, func, *args):
    """both() inside a dvcheck mark window."""
    mark(rig, "begin", tag)
    try:
        return both(sides, func, *args)
    finally:
        mark(rig, "end", tag)


def same(got, *keys):
    for key in keys:
        assert got["sut"][key] == got["ref"][key], "%s: sut %r, ref %r" % (key, got["sut"][key], got["ref"][key])


def frames49(n=FRAMES, seed="nxdn-peer"):
    return [nxdn.from_dmr(f) for f in signals.prng_frames("dmr", n, seed=seed)]


def dmr_listener(side, module=None, dmrid=1023008):
    client = dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=module or side.module)
    assert client.link(side.xlxd), "%s: DMR link failed" % side.name
    client.client.inbox.clear()
    client.client.inbox_times.clear()
    return client


def gateway(side, index=0, nxdn_id=None):
    client = nxdn.NxdnClient("0.0.0.0", side.tg, nxdn_id=nxdn_id or GW_IDS[index],
                             callsign=clients.CALLSIGNS[index])
    assert client.link(side.reflector), "%s: NXDNReflector link failed" % side.name
    client.client.inbox.clear()
    return client


def dmr_rx(client):
    """Received DMRD as (time, kind, fields): kind header / terminator / voice."""
    out = []
    for data, stamp in zip(client.client.inbox, client.client.inbox_times):
        info = dmr.parse(data)
        if not info:
            continue
        bits = info["bitfield"]
        if (bits >> 4) & 3 == 2:
            kind = {1: "header", 2: "terminator"}.get(bits & 0xF, "data")
        else:
            kind = "voice"
        out.append((stamp, kind, info))
    return out


def dmr_frames49(client):
    return [nxdn.from_dmr(f) for _, kind, info in dmr_rx(client) if kind == "voice"
            for f in dmr.extract(info["payload"])]


def close(*made):
    for client in made:
        try:
            if client.dest:
                client.unlink()
        finally:
            client.close()


def ingress(side, packets, via="reflector", module=None, gap=nxdn.PACKET_GAP, wait=SETTLE):
    """Send NXDND packets into the peer (through the NXDNReflector as a
    gateway, or as the peer's reflector) with a DMR listener on the module;
    returns the listener's frames and DMRD events, and what came back to
    the gateway."""
    listener = dmr_listener(side, module or (side.module if via == "reflector" else side.probe_module))
    gw = gateway(side) if via == "reflector" else None
    try:
        sent_at = time.monotonic()
        if gw:
            gw.client.paced(gw.dest, packets, gap, listener.client)
        else:
            side.probe_send(packets, gap)
        end_at = time.monotonic()
        listener.client.pump(wait)
        return {"_frames": dmr_frames49(listener), "_rx": dmr_rx(listener), "_sent_at": sent_at,
                "_end_at": end_at, "_echo": gw.packets() if gw else []}
    finally:
        close(*[c for c in (gw, listener) if c])


def egress(side, frames, dmrid=TALKER_DMR, dmr_frames=None):
    """A DMR stream on the module, heard by a gateway on the reflector:
    the parsed NXDND packets with arrival times."""
    listener = gateway(side, 1)
    talker = dmr.DmrClient("0.0.0.0", dmrid=dmrid, module=side.module)
    try:
        assert talker.link(side.xlxd), "%s: DMR talker link failed" % side.name
        # the talker's join (an empty DMR stream) reaches NXDN as header + trailer
        listener.client.pump(0.5)
        listener.client.inbox.clear()
        listener.client.inbox_times.clear()
        # DmrClient.talk with the listener drained during the over (arrival times)
        sid = clients.fresh_stream_id("dmr")
        voice = dmr_frames or [nxdn.to_dmr(f) for f in frames]
        talker.client.send(talker.dest, dmr.packet(0, talker.id, talker.tg, talker.id, 0xA1, sid,
                                                   dmr.data_sync_payload()))
        listener.client.pump(0.3)
        bursts, seq = dmr.voice_packets(voice, talker.id, talker.tg, sid)
        talker.client.paced(talker.dest, bursts, 0.060, listener.client)
        talker.client.send(talker.dest, dmr.packet(seq, talker.id, talker.tg, talker.id, 0xA2, sid,
                                                   dmr.data_sync_payload()))
        listener.client.pump(SETTLE)
        rx = [(t, nxdn.parse(d)) for d, t in zip(listener.client.inbox, listener.client.inbox_times)
              if nxdn.parse(d)]
        return {"_rx": rx, "_raw": [d for d in listener.client.inbox if d.startswith(b"NXDND")]}
    finally:
        close(talker, listener)


def _ready_reflector(side):
    try:
        got = egress(side, frames49(8, "ready"))
    except AssertionError:
        return False
    return any(p["kind"] == "voice" for _, p in got["_rx"])


def _ready_probe(side):
    listener = dmr.DmrClient("0.0.0.0", dmrid=1023008, module=side.probe_module)
    talker = dmr.DmrClient("0.0.0.0", dmrid=TALKER_DMR, module=side.probe_module)
    try:
        if not (listener.link(side.xlxd) and talker.link(side.xlxd)):
            return False
        start = len(side.probe.client.inbox)
        talker.talk([nxdn.to_dmr(f) for f in frames49(12, "ready")], stream_id=clients.fresh_stream_id("dmr"),
                    settle=0.3, tail=1.0)
        return any(nxdn.parse(d) for d in side.probe.client.inbox[start:])
    finally:
        close(talker, listener)


@pytest.fixture(scope="module")
def peer(rig):
    """Both sides: the NXDNReflector carries xlxd traffic and the probe row
    is linked to the tester (the tester echoes its polls)."""
    if os.environ.get("DVT_NXDN") != "on":
        pytest.skip("no NXDNReflector: run.sh --profile nxdn (DVT_NXDN=on)")
    env = os.environ
    if not env.get("DVT_NXDN_PROBE_PORT_REF"):
        pytest.skip("no tester-as-reflector row (DVT_NXDN_PROBE_*; rig/compose.yml profile nxdn)")
    sides = [Side(n, getattr(rig, "xlxd_" + n), env["DVT_NXDN_" + n.upper()], int(env["DVT_NXDN_TG"]),
                  env["DVT_NXDN_MODULE"], int(env["DVT_NXDN_PROBE_TG"]), env["DVT_NXDN_PROBE_MODULE"],
                  int(env["DVT_NXDN_PROBE_PORT_" + n.upper()])) for n in ("ref", "sut")]
    for side in sides:
        side.start_probe()
    try:
        for side in sides:
            deadline = time.monotonic() + 60
            while not (_ready_reflector(side) and _ready_probe(side)):
                if time.monotonic() > deadline:
                    pytest.fail("%s: NXDN paths not ready (reflector %s, probe port %d)"
                                % (side.name, side.reflector, side.probe_port))
                time.sleep(2)
        yield {"rig": rig, "sides": sides}
    finally:
        for side in sides:
            side.stop.set()
            side.probe.close()


# ---------------------------------------------------------------- 1. link and keepalive

def test_nxdn_poll_format(peer):
    """P-NXD-01: NXDNP + callsign (10, padded) + the row's TG, 17 B."""
    def run(side):
        polls = [p for p in side.probe.polls if p[2]["kind"] == "poll"]
        assert polls, "%s: no poll at the tester" % side.name
        info = polls[-1][2]
        return {"callsign": info["callsign"], "tg_ok": info["tg"] == side.probe_tg, "polls": len(polls)}

    got = both(peer["sides"], run)
    for name in ("ref", "sut"):
        assert got[name]["tg_ok"], "%s polls another TG" % name
    same(got, "callsign", "tg_ok")


def test_nxdn_poll_period(peer):
    """P-NXD-02: one poll per 5 s, never 120 s silent."""
    def run(side):
        deadline = time.monotonic() + 25
        while len([p for p in side.probe.polls if p[2]["kind"] == "poll"]) < 5 and time.monotonic() < deadline:
            time.sleep(0.5)
        times = [p[0] for p in side.probe.polls if p[2]["kind"] == "poll"]
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        assert gaps, "%s: fewer than two polls" % side.name
        return {"period_s": round(gaps[len(gaps) // 2]), "max_gap_s": round(gaps[-1], 1)}

    got = both(peer["sides"], run)
    for name in ("ref", "sut"):
        assert got[name]["max_gap_s"] < 120, "%s: %.1f s without a poll" % (name, got[name]["max_gap_s"])
    same(got, "period_s")


# ---------------------------------------------------------------- 2. egress

@pytest.fixture(scope="module")
def eg(peer):
    """One DMR stream of FRAMES frames per side, as the gateway hears it."""
    sent = frames49()
    got = both(peer["sides"], egress, sent)
    for name in ("ref", "sut"):
        got[name]["_sent"] = sent
    return got


def packets_of(got, name):
    return [p for _, p in got[name]["_rx"]]


def test_nxdn_egress_framing(peer, eg):
    """P-NXD-05: 43 B NXDND, group, dst = TG on every packet."""
    rows = {}
    for side in peer["sides"]:
        raw = eg[side.name]["_raw"]
        pk = packets_of(eg, side.name)
        rows[side.name] = {"packets": len(raw), "all_43": all(len(d) == nxdn.PACKET_SIZE for d in raw),
                           "group": all(p["group"] for p in pk), "dst_tg": all(p["dst"] == side.tg for p in pk)}
    record(rows)
    for name, row in rows.items():
        assert row["packets"] and row["all_43"] and row["group"] and row["dst_tg"], (name, row)


def test_nxdn_egress_flags(peer, eg):
    """P-NXD-06: flags agree with the payload; header first, trailer last."""
    for side in peer["sides"]:
        pk = packets_of(eg, side.name)
        wrong = [p["flags"] for p in pk if p["flags"] != nxdn.flags_for(p["payload"], p["group"])]
        kinds = [p["kind"] for p in pk]
        record({"kinds": dict(collections.Counter(kinds)), "wrong_flags": wrong}, side.name)
        assert not wrong, "%s: flags disagree with the payload: %s" % (side.name, wrong)
        assert kinds[0] == "header" and kinds[-1] == "trailer", "%s: %s" % (side.name, kinds)


def test_nxdn_egress_header(peer, eg):
    """P-NXD-07: LICH 0x81, FACCH1 VCALL + CRC-12 in both blocks (hb); the
    single SACCH message type (dev: IDLE per spec, 0 on xlxd)."""
    got = {}
    for side in peer["sides"]:
        head = [p for p in packets_of(eg, side.name) if p["kind"] == "header"][0]["payload"]
        f1, f2 = nxdn.parse_facch1(head[5:17]), nxdn.parse_facch1(head[19:31])
        sacch = nxdn.parse_sacch(head[1:5])
        got[side.name] = {"lich": head[0], "facch_ok": f1["crc_ok"] and f1["msg"] == nxdn.VCALL,
                          "blocks_equal": head[5:17] == head[19:31] and f2["crc_ok"],
                          "sacch_single": sacch["structure"] == 0 and sacch["crc_ok"],
                          "sacch_type": (sacch["data"] >> 10) & 0x3F}
    record(got)
    for name, row in got.items():
        assert row["lich"] == 0x81 and row["facch_ok"] and row["blocks_equal"] and row["sacch_single"], (name, row)
    judge_dev(peer["rig"], "P-NXD-07", got, "sacch_type")


def test_nxdn_egress_vcall_fields(peer, eg):
    """P-NXD-08: group call (call type 001), CC option 0, no cipher, src =
    the talker's NXDN id, dst = TG."""
    for side in peer["sides"]:
        head = [p for p in packets_of(eg, side.name) if p["kind"] == "header"][0]
        l3 = nxdn.parse_layer3(head["payload"][5:15])
        record(l3, side.name)
        assert (l3["call_type"], l3["cc_option"], l3["cipher"]) == (nxdn.CALL_GROUP, 0, 0), (side.name, l3)
        assert (l3["src"], l3["dst"]) == (GW_IDS[0], side.tg) == (head["src"], head["dst"]), (side.name, l3, head)


def test_nxdn_egress_voice(peer, eg):
    """P-NXD-09: voice frames LICH 0xAC (superframe SACCH, no steal), four
    49-bit frames each, FRAMES / 4 packets."""
    for side in peer["sides"]:
        voice = [p for p in packets_of(eg, side.name) if p["kind"] == "voice"]
        lichs = collections.Counter(p["payload"][0] for p in voice)
        record({"voice": len(voice), "lich": {hex(k): v for k, v in lichs.items()}}, side.name)
        assert set(lichs) == {0xAC} and len(voice) == FRAMES // 4, (side.name, len(voice), lichs)


def test_nxdn_egress_sacch(peer, eg):
    """P-NXD-10: SACCH superframe 1/4..4/4 from the first voice frame, CRC-6,
    the four parts rebuild the VCALL message."""
    for side in peer["sides"]:
        voice = [p for p in packets_of(eg, side.name) if p["kind"] == "voice"]
        sacch = [nxdn.parse_sacch(p["payload"][1:5]) for p in voice]
        structures = [s["structure"] for s in sacch]
        record(structures, side.name)
        assert all(s["crc_ok"] for s in sacch), side.name
        assert structures == [3 - i % 4 for i in range(len(voice))], (side.name, structures)
        message = 0
        for part in sacch[:4]:
            message = (message << 18) | part["data"]
        l3 = nxdn.parse_layer3(message.to_bytes(9, "big") + b"\x00")
        assert (l3["msg"], l3["src"], l3["dst"]) == (nxdn.VCALL, GW_IDS[0], side.tg), (side.name, l3)


def test_nxdn_egress_trailer(peer, eg):
    """P-NXD-11: exactly one trailer, last, FACCH1 TX_REL with CRC-12."""
    for side in peer["sides"]:
        pk = packets_of(eg, side.name)
        trailers = [p for p in pk if p["kind"] == "trailer"]
        assert len(trailers) == 1 and pk[-1]["kind"] == "trailer", (side.name, [p["kind"] for p in pk])
        f1 = nxdn.parse_facch1(trailers[0]["payload"][5:17])
        assert f1["msg"] == nxdn.TX_REL and f1["crc_ok"] and trailers[0]["payload"][0] == 0x81, (side.name, f1)


def gain_steps(sent, got):
    """Counter of (received b2 - sent b2) over frames with sent b2 > 0, and
    the number of frames that differ outside the gain bits."""
    steps = collections.Counter(nxdn.gain_index(g) - nxdn.gain_index(s) for s, g in zip(sent, got)
                                if nxdn.gain_index(s) > 0)
    other = sum(nxdn.without_gain(s) != nxdn.without_gain(g) for s, g in zip(sent, got))
    return steps, other


@pytest.fixture(scope="module")
def ig(peer):
    """One NXDN stream of FRAMES frames per side through the reflector,
    heard by a DMR listener."""
    sent = frames49(seed="nxdn-peer-in")
    got = both(peer["sides"], ingress, nxdn.stream_packets(sent, GW_IDS[0], peer["sides"][0].tg))
    for name in ("ref", "sut"):
        got[name]["_sent"] = sent
    return got


def test_nxdn_frame_conversion(peer, eg, ig):
    """P-NXD-12: 49 <-> 72 bit conversion leaves every bit outside the
    gain index unchanged, both directions; DMR FEC valid."""
    got = {}
    for side in peer["sides"]:
        out_frames = [f for p in packets_of(eg, side.name) if p["kind"] == "voice" for f in nxdn.voice_frames(p["payload"])]
        _, other_out = gain_steps(eg[side.name]["_sent"], out_frames)
        _, other_in = gain_steps(ig[side.name]["_sent"], ig[side.name]["_frames"])
        got[side.name] = {"out": len(out_frames), "in": len(ig[side.name]["_frames"]),
                          "other_out": other_out, "other_in": other_in}
    record(got)
    for name, row in got.items():
        assert row["out"] == FRAMES and row["in"] == FRAMES, (name, row)
        assert row["other_out"] == 0 and row["other_in"] == 0, (name, row)


def test_nxdn_gain_step(peer, eg, ig):
    """P-NXD-13: gain-index step per direction (dev: xlxd -1 = -0.3 dB;
    spec, clients and other servers 0)."""
    got = {}
    for side in peer["sides"]:
        out_frames = [f for p in packets_of(eg, side.name) if p["kind"] == "voice" for f in nxdn.voice_frames(p["payload"])]
        s_out, _ = gain_steps(eg[side.name]["_sent"], out_frames)
        s_in, _ = gain_steps(ig[side.name]["_sent"], ig[side.name]["_frames"])
        got[side.name] = {"step_out": s_out.most_common(1)[0][0], "step_in": s_in.most_common(1)[0][0],
                          "hist_out": dict(s_out), "hist_in": dict(s_in)}
    record(got)
    judge_dev(peer["rig"], "P-NXD-13", got, "step_out", "step_in")


def test_nxdn_partial_group_padded(peer):
    """P-NXD-14: 9 frames (3 DMR bursts) give 3 NXDN packets; the last one
    is padded with the spec silence frame."""
    sent = frames49(9, "nxdn-partial")
    silence = nxdn.from_dmr(nxdn.SILENCE_DMR)

    def run(side):
        got = egress(side, sent)
        out = [f for _, p in got["_rx"] if p["kind"] == "voice" for f in nxdn.voice_frames(p["payload"])]
        return {"frames": len(out), "pad_silence": out[9:] == [silence] * 3 if len(out) == 12 else None,
                "last_real_step": nxdn.gain_index(out[8]) - nxdn.gain_index(sent[8]) if len(out) > 8 else None}

    got = both(peer["sides"], run)
    same(got, "frames", "pad_silence", "last_real_step")


def test_nxdn_egress_pacing(peer, eg):
    """P-NXD-15: voice packets at the source pace, no bursts (recorded:
    smallest and largest gap)."""
    got = {}
    for side in peer["sides"]:
        times = [t for t, p in eg[side.name]["_rx"] if p["kind"] == "voice"]
        gaps = [b - a for a, b in zip(times, times[1:])]
        got[side.name] = {"burst": min(gaps) < 0.030, "min_ms": round(min(gaps) * 1000),
                          "max_ms": round(max(gaps) * 1000),
                          "mean_ms": round((times[-1] - times[0]) / len(gaps) * 1000)}
    record(got)
    same(got, "burst")


@pytest.mark.parametrize("who", ["known", "unknown"])
def test_nxdn_egress_src_id(peer, eg, who):
    """P-NXD-16: src = NXDN id of the talker's callsign; unknown -> fixed id."""
    def run(side):
        if who == "known":
            pk = packets_of(eg, side.name)
        else:
            pk = [p for _, p in egress(side, frames49(12, "nxdn-unknown"), dmrid=UNKNOWN_DMR)["_rx"]]
        return {"src": sorted({p["src"] for p in pk})}

    got = both(peer["sides"], run)
    same(got, "src")
    if who == "known":
        assert got["sut"]["src"] == [GW_IDS[0]], got


def test_nxdn_no_echo(peer, ig):
    """P-NXD-17: nothing comes back to the talking gateway."""
    got = {n: {"echo": len(ig[n]["_echo"])} for n in ("ref", "sut")}
    record(got)
    assert got["ref"]["echo"] == 0 and got["sut"]["echo"] == 0, got


def test_nxdn_empty_stream_not_relayed(peer):
    """P-NXD-30: an empty stream from another mode (a DMR join: header +
    terminator, no voice) is not put on NXDN as a VCALL + TX_REL call (dev)."""
    def run(side):
        listener = gateway(side, 1)
        talker = dmr.DmrClient("0.0.0.0", dmrid=TALKER_DMR, module=side.module)
        try:
            assert talker.link(side.xlxd)              # the join is the empty stream
            listener.client.pump(SETTLE)
            kinds = [p["kind"] for p in listener.packets()]
            return {"relayed": bool(kinds), "kinds": kinds}
        finally:
            close(talker, listener)

    judge_dev(peer["rig"], "P-NXD-30", both(peer["sides"], run), "relayed")


# ---------------------------------------------------------------- 3. ingress

def test_nxdn_garbage_ignored(peer):
    """P-NXD-18: non-NXDN, short, long and wrong-TG packets from the
    reflector address are ignored; the link stays and a stream after them
    is routed."""
    sent = frames49(16, "nxdn-garbage")

    def run(side):
        junk = [b"XXXX" + bytes(6), b"NXDND" + bytes(37), b"NXDND" + bytes(39),
                nxdn.poll_packet("XLX000", side.probe_tg + 1), b"NXDNQ" + bytes(12)]
        side.probe_send(junk, 0.05)
        got = ingress(side, nxdn.stream_packets(sent, GW_IDS[0], side.probe_tg), via="probe")
        return {"frames": len(got["_frames"])}

    got = marked(peer["rig"], "nxdn-garbage", peer["sides"], run)
    same(got, "frames")


def test_nxdn_ingress_header_trailer(peer, ig):
    """P-NXD-19: the header opens the stream, all frames are routed, the
    trailer ends it at once (DMR terminator well before the 1.6 s timeout)."""
    got = {}
    for name in ("ref", "sut"):
        rx = ig[name]["_rx"]
        terms = [t for t, kind, _ in rx if kind == "terminator"]
        got[name] = {"frames": len(ig[name]["_frames"]), "headers": sum(k == "header" for _, k, _ in rx),
                     "terminators": len(terms),
                     "end_ms": round((terms[0] - ig[name]["_end_at"]) * 1000) if terms else None}
    record(got)
    for name, row in got.items():
        assert row["frames"] == FRAMES and row["headers"] == 1 and row["terminators"] == 1, (name, row)
        assert row["end_ms"] < 800, (name, row)


def test_nxdn_ingress_late_entry(peer):
    """P-NXD-20: voice without a header joins late after the SACCH
    superframe (dev: xlxd drops it)."""
    sent = frames49(seed="nxdn-late")

    def run(side):
        got = ingress(side, nxdn.stream_packets(sent, GW_IDS[0], side.tg, header=False))
        return {"late_entry": len(got["_frames"]) > 0, "frames": len(got["_frames"])}

    judge_dev(peer["rig"], "P-NXD-20", marked(peer["rig"], "nxdn-late-entry", peer["sides"], run), "late_entry")


def test_nxdn_ingress_repeated_header(peer):
    """P-NXD-21: a second VCALL header after 5 voice packets does not split
    or cut the call."""
    sent = frames49(seed="nxdn-rehead")

    def run(side):
        packets = nxdn.stream_packets(sent, GW_IDS[0], side.tg)
        packets = packets[:6] + [packets[0]] + packets[6:]
        got = ingress(side, packets)
        rx = got["_rx"]
        return {"whole": len(got["_frames"]) == FRAMES, "frames": len(got["_frames"]),
                "headers": sum(k == "header" for _, k, _ in rx)}

    got = marked(peer["rig"], "nxdn-repeated-header", peer["sides"], run)
    same(got, "whole", "headers")
    assert got["sut"]["whole"], got


def test_nxdn_ingress_repeated_trailer(peer):
    """P-NXD-22: three trailers end the call once; a new call right after
    them is routed whole."""
    first, second = frames49(20, "nxdn-tr1"), frames49(seed="nxdn-tr2")

    def run(side):
        p1 = nxdn.stream_packets(first, GW_IDS[0], side.tg)
        p2 = nxdn.stream_packets(second, GW_IDS[0], side.tg)
        got = ingress(side, p1 + [p1[-1]] * 2 + p2)
        rx = got["_rx"]
        return {"frames": len(got["_frames"]), "headers": sum(k == "header" for _, k, _ in rx),
                "terminators": sum(k == "terminator" for _, k, _ in rx)}

    got = marked(peer["rig"], "nxdn-repeated-trailer", peer["sides"], run)
    same(got, "frames", "headers", "terminators")


def test_nxdn_ingress_steal(peer):
    """P-NXD-23: frames whose first half is stolen for FACCH1 (LICH 0xA4):
    is the FACCH1 half played as voice? (dev)."""
    sent = frames49(seed="nxdn-steal")

    def run(side):
        packets = nxdn.stream_packets(sent, GW_IDS[0], side.tg)
        stolen = []
        for index in (4, 8):                         # packets[index] is voice packet index - 1
            pair = sent[(index - 1) * 4 + 2:(index - 1) * 4 + 4]
            payload = nxdn.steal_payload(pair, GW_IDS[0], side.tg, index - 1, half=1)
            stolen.extend(nxdn.voice_frames(payload)[:2])
            packets[index] = nxdn.packet(GW_IDS[0], side.tg, payload)
        got = ingress(side, packets)
        heard = got["_frames"]
        return {"frames": len(heard), "facch_as_voice": any(nxdn.without_gain(f) in
                                                          {nxdn.without_gain(s) for s in stolen} for f in heard)}

    judge_dev(peer["rig"], "P-NXD-23", marked(peer["rig"], "nxdn-steal", peer["sides"], run), "facch_as_voice", "frames")


def test_nxdn_ingress_data(peer):
    """P-NXD-24: data frames (flags 0x03, LICH 0x90) open no voice stream."""
    def run(side):
        packets = [nxdn.packet(GW_IDS[0], side.tg, nxdn.data_payload(bytes([i]) * 8)) for i in range(8)]
        got = ingress(side, packets)
        return {"dmr_packets": len(got["_rx"])}

    got = marked(peer["rig"], "nxdn-data", peer["sides"], run)
    for name in ("ref", "sut"):
        assert got[name]["dmr_packets"] == 0, (name, got[name])


@pytest.mark.parametrize("what", ["private", "other_tg"])
def test_nxdn_ingress_private_or_other_tg(peer, what):
    """P-NXD-25: an individual call, or a group call to another TG, sent by
    the peer's reflector (NXDNReflector would filter both) is not routed
    (dev for other_tg: xlxd does not check dst)."""
    sent = frames49(20, "nxdn-" + what)

    def run(side):
        if what == "private":
            packets = nxdn.stream_packets(sent, GW_IDS[0], side.probe_tg, group=False)
        else:
            packets = nxdn.stream_packets(sent, GW_IDS[0], side.probe_tg + 1)
        got = ingress(side, packets, via="probe")
        return {"routed": len(got["_frames"]) > 0, "frames": len(got["_frames"])}

    judge_dev(peer["rig"], "P-NXD-25", marked(peer["rig"], "nxdn-private-other-tg", peer["sides"], run), "routed")


def test_nxdn_ingress_encrypted(peer):
    """P-NXD-26: a VCALL with cipher type 01: routed or not (dev, recorded)."""
    sent = frames49(20, "nxdn-cipher")

    def run(side):
        got = ingress(side, nxdn.stream_packets(sent, GW_IDS[0], side.tg, cipher=1, key=1))
        return {"routed": len(got["_frames"]) > 0, "frames": len(got["_frames"])}

    judge_dev(peer["rig"], "P-NXD-26", marked(peer["rig"], "nxdn-encrypted", peer["sides"], run), "routed")


def test_nxdn_two_talkers(peer):
    """P-NXD-27: two gateways talk at once through the reflector (it relays
    both): one winner, no mixing (spec B1). xlxd's interference is data."""
    a, b = frames49(seed="nxdn-two-a"), frames49(seed="nxdn-two-b")

    def run(side):
        listener = dmr_listener(side)
        ga, gb = gateway(side, 0), gateway(side, 1)
        try:
            pa = nxdn.stream_packets(a, GW_IDS[0], side.tg)
            pb = nxdn.stream_packets(b, GW_IDS[1], side.tg)
            start = time.monotonic()
            for tick in range(len(pa) + 5):
                pace(start, tick, nxdn.PACKET_GAP)
                if tick < len(pa):
                    ga.client.send(ga.dest, pa[tick])
                if 5 <= tick < 5 + len(pb):
                    gb.client.send(gb.dest, pb[tick - 5])
                listener.client.drain()
            listener.client.pump(SETTLE)
            heard = dmr_frames49(listener)
            sa, sb = {nxdn.without_gain(f) for f in a}, {nxdn.without_gain(f) for f in b}
            from_a = sum(nxdn.without_gain(f) in sa for f in heard)
            from_b = sum(nxdn.without_gain(f) in sb for f in heard)
            rx = dmr_rx(listener)
            return {"from_a": from_a, "from_b": from_b, "mixed": from_a > 0 and from_b > 0,
                    "headers": sum(k == "header" for _, k, _ in rx), "streams": len({i["stream"] for _, _, i in rx})}
        finally:
            close(ga, gb, listener)

    judge_dev(peer["rig"], "P-NXD-27", marked(peer["rig"], "nxdn-two-talkers", peer["sides"], run), "mixed")


def test_nxdn_lost_trailer_timeout(peer):
    """P-NXD-28: no trailer: the stream ends by timeout with a terminator to
    the DMR listener, and another gateway's call is then routed (spec B3)."""
    first, second = frames49(20, "nxdn-lost1"), frames49(20, "nxdn-lost2")

    def run(side):
        listener = dmr_listener(side)
        g1, g2 = gateway(side, 0), gateway(side, 1)
        try:
            g1.client.paced(g1.dest, nxdn.stream_packets(first, GW_IDS[0], side.tg, trailer=False),
                            nxdn.PACKET_GAP, listener.client)
            last = time.monotonic()
            listener.client.pump(3.0)
            terms = [t for t, k, _ in dmr_rx(listener) if k == "terminator"]
            mark = len(dmr_frames49(listener))
            g2.client.paced(g2.dest, nxdn.stream_packets(second, GW_IDS[1], side.tg), nxdn.PACKET_GAP,
                            listener.client)
            listener.client.pump(SETTLE)
            return {"terminator": bool(terms), "timeout_s": round(terms[0] - last, 1) if terms else None,
                    "freed": len(dmr_frames49(listener)) - mark >= 20}
        finally:
            close(g1, g2, listener)

    judge_dev(peer["rig"], "P-NXD-28", marked(peer["rig"], "nxdn-lost-trailer", peer["sides"], run), "terminator", "freed")


def test_nxdn_ingress_src_mapping(peer):
    """P-NXD-29: the NXDN src id maps to the callsign (D-STAR MY) and the
    DMR id of that callsign; an unknown id gets a stable fallback."""
    sent = frames49(20, "nxdn-src")

    def heard(side, nxdn_id):
        ds = dextra.DstarClient("0.0.0.0", callsign="N0TEST", module=side.module)
        dm = dmr_listener(side)
        gw = gateway(side, 0, nxdn_id=nxdn_id)
        try:
            assert ds.link(side.xlxd)
            ds.client.inbox.clear()
            gw.client.paced(gw.dest, nxdn.stream_packets(sent, nxdn_id, side.tg), nxdn.PACKET_GAP,
                            ds.client, dm.client)
            ds.client.pump(SETTLE)
            dm.client.pump(0.2)
            my = sorted({dextra.parse_header(d)["my"].decode("ascii", "replace").strip()
                         for d in ds.client.inbox if dextra.parse_header(d)})
            src = sorted({i["src"] for _, k, i in dmr_rx(dm) if k == "header"})
            return my, src
        finally:
            close(ds, dm, gw)

    def run(side):
        my_k, src_k = heard(side, GW_IDS[0])
        my_u, src_u = heard(side, UNKNOWN_NXDN)
        return {"my_known": my_k, "dmr_src_known": src_k, "my_unknown": my_u, "dmr_src_unknown": src_u}

    got = both(peer["sides"], run)
    same(got, "my_known", "dmr_src_known", "my_unknown", "dmr_src_unknown")


# ---------------------------------------------------------------- 1b. loss and leave (slow, last)

@pytest.mark.slow
def test_nxdn_reflector_loss_relink(peer):
    """P-NXD-03: the reflector stops echoing for 40 s: keeps polling?
    still routes? After echoes return, how soon is traffic routed again."""
    sent = frames49(12, "nxdn-loss")

    def run(side):
        side.probe.echo = False
        try:
            start = len(side.probe.polls)
            time.sleep(40)
            polls = len([p for p in side.probe.polls[start:] if p[2]["kind"] == "poll"])
            during = len(ingress(side, nxdn.stream_packets(sent, GW_IDS[0], side.probe_tg), via="probe")["_frames"])
        finally:
            side.probe.echo = True
        back = time.monotonic()
        resumed_s = None
        while time.monotonic() - back < 25:
            if ingress(side, nxdn.stream_packets(sent, GW_IDS[0], side.probe_tg), via="probe", wait=0.8)["_frames"]:
                resumed_s = round(time.monotonic() - back, 1)
                break
        return {"keeps_polling": polls >= 6, "polls": polls, "routed_during_loss": during > 0,
                "resumed": resumed_s is not None, "resumed_s": resumed_s}

    judge_dev(peer["rig"], "P-NXD-03", marked(peer["rig"], "nxdn-reflector-loss", peer["sides"], run), "keeps_polling", "routed_during_loss", "resumed")


@pytest.mark.slow
def test_nxdn_unlink_on_leave(peer, rig_control):
    """P-NXD-04: does the peer send NXDNU when it leaves (xlxd restart)?
    Waits for the peer to come back before returning."""
    def run(side):
        start = len(side.probe.polls)
        rig_control("restart", "xlxd-" + side.name)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not _ready_probe(side):
            time.sleep(2)
        kinds = [p[2]["kind"] for p in side.probe.polls[start:]]
        return {"unlink": "unlink" in kinds, "unlinks": kinds.count("unlink")}

    judge_dev(peer["rig"], "P-NXD-04", both(peer["sides"], run), "unlink")
