# SPDX-License-Identifier: MIT
"""YSF, DPlus and DCS clients on xlxd-ref and xlxd-sut of the docker rig.

Each new client links to both reflectors on module D (no USRP endpoint in
any profile) and talks to, or listens to, the clients the matrix already
uses (DExtra "dstar", DMR, M17). Every cell runs on ref and sut at the
same time, and the talker must not hear itself.

D-STAR family (DPlus, DCS, DExtra): same codec, no transcoding, so the
listener must get the sent PRNG frames byte for byte, on both reflectors.

YSF carries AMBE+2 as V/D mode 2 (DN): xlxd accepts no other voice mode.
Like NXDN it is not byte exact: xlxd steps the gain index b2 by +1.0 dB on
the way in (YSF -> anything) and by -0.4 dB on the way out (anything ->
YSF), so YSF <-> YSF and YSF <-> DMR (both AMBE+2, not transcoded) keep
all 49 voice bits except b2. Those cells assert exactly that, plus
ref == sut. A YSF client joins module D with DG-ID 13 (10 + module
index); new YSF clients sit on module B until then.

Cross cells (to and from DMR, M17 and, for YSF, D-STAR) are differential
like the matrix: tone bursts encoded by transcoder-ref, xlxd-sut must
deliver exactly what xlxd-ref delivers.

Lifecycle: link (ACK), hear a talker, unlink (DPlus: disconnect echoed,
DCS: NAK, YSF: WiresX disconnect), hear nothing of the next talk.

Results: $DVT_OUT/clients2.json.
"""
import hashlib
import json
import random
import threading
from pathlib import Path

import pytest

from dvtest import clients, specs
from dvtest.audio import signals
from dvtest.proto import ambed, dcs, dplus, nxdn, ysf

pytestmark = pytest.mark.rig

FRAMES = 60                                 # whole YSF packets (5) and DMR bursts (3)
MODULE = "D"
NEW = ("ysf", "dplus", "dcs")
CODEC = {"dstar": 1, "dplus": 1, "dcs": 1, "dmr": 2, "ysf": 2, "m17": 3}
PASSTHROUGH = [("dplus", "dplus"), ("dcs", "dcs"), ("dplus", "dcs"), ("dcs", "dstar"),
               ("dstar", "dplus")]
GAIN_STEPPED = [("ysf", "ysf"), ("ysf", "dmr"), ("dmr", "ysf")]
CROSS = [("ysf", "dstar"), ("dstar", "ysf"), ("ysf", "m17"), ("m17", "ysf"),
         ("dplus", "dmr"), ("dmr", "dplus"), ("dplus", "m17"), ("m17", "dplus"),
         ("dcs", "dmr"), ("dmr", "dcs"), ("dcs", "m17"), ("m17", "dcs")]
CELLS = PASSTHROUGH + GAIN_STEPPED + CROSS
LIFECYCLE_TALKER = {"ysf": "dmr", "dplus": "dstar", "dcs": "dstar"}
RESULTS = {}

_rng = random.SystemRandom()


def sha(frames):
    return hashlib.sha256(b"".join(frames)).hexdigest()


def make(kind, module, index):
    callsign = clients.CALLSIGNS[index]
    if kind == "ysf":
        return ysf.YsfClient("0.0.0.0", callsign=callsign, module=module)
    if kind == "dplus":
        return dplus.DplusClient("0.0.0.0", callsign=callsign, module=module)
    if kind == "dcs":
        return dcs.DcsClient("0.0.0.0", callsign=callsign, module=module)
    return clients.make(kind, module, index)


def talk(client, kind, frames):
    if kind in NEW:
        client.talk(frames, _rng.randrange(1, 1 << 16))
    else:
        clients.talk(client, kind, frames)


def voice_bits(frame9):
    """The 49 AMBE+2 bits of a DMR-form frame without the gain index."""
    return nxdn.without_gain(nxdn.from_dmr(frame9))


def find_run(sent, got):
    """Offset of sent as a contiguous run in got (compared by key), or None."""
    for offset in range(len(got) - len(sent) + 1):
        if got[offset:offset + len(sent)] == sent:
            return offset
    return None


def run_cell(ip, src, dst, module, frames):
    """Talker of kind src and listener of kind dst on one module:
    (listener frames, frames the talker heard back)."""
    listener = make(dst, module, 1)
    talker = make(src, module, 0)
    try:
        assert listener.link(ip), "%s listener link to %s failed" % (dst, ip)
        if src == "dplus":
            assert talker.link(ip, join=False), "dplus talker link to %s failed" % ip
        else:
            assert talker.link(ip), "%s talker link to %s failed" % (src, ip)
        for client in (listener, talker):
            client.client.drain()
            client.client.inbox.clear()
        talk(talker, src, frames)
        return listener.collect(), talker.collect(settle=0.2)
    finally:
        clients.close(talker, listener)


@pytest.fixture(scope="module")
def encoded(rig):
    """Tone bursts encoded once by transcoder-ref: {codec: frames}."""
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    return ambed.transcode(rig.ambed_ref, ambed.PCM, [1, 2, 3], pcm, port=rig.ambed_port)


def cell_input(src, dst, encoded):
    if (src, dst) in PASSTHROUGH:
        return signals.prng_frames("dstar", FRAMES, seed="clients2")
    if (src, dst) in GAIN_STEPPED:
        # valid AMBE+2 codewords: YSF carries only the 49 data bits
        return [nxdn.to_dmr(nxdn.from_dmr(f))
                for f in signals.prng_frames("dmr", FRAMES, seed="clients2")]
    return encoded[CODEC[src]]


@pytest.fixture(scope="module")
def cells(rig, encoded):
    """Every cell on both reflectors; ref and sut run concurrently."""
    rows = {}
    for src, dst in CELLS:
        frames = cell_input(src, dst, encoded)
        got = {}

        def side(name, ip):
            try:
                got[name] = run_cell(ip, src, dst, MODULE, frames)
            except Exception as exc:            # reported by the cell test
                got[name] = exc

        threads = [threading.Thread(target=side, args=(n, ip))
                   for n, ip in (("ref", rig.xlxd_ref), ("sut", rig.xlxd_sut))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        row = {"cell": "%s-%s" % (src, dst), "module": MODULE, "sent": len(frames),
               "input_sha256": sha(frames)}
        for name in ("ref", "sut"):
            value = got[name]
            if isinstance(value, Exception):
                row[name] = {"error": repr(value)}
                continue
            heard, echo = value
            row[name] = {"received": len(heard), "sha256": sha(heard), "echo": len(echo)}
            if (src, dst) in PASSTHROUGH:
                row[name]["exact"] = heard == frames
            elif (src, dst) in GAIN_STEPPED:
                offset = find_run([voice_bits(f) for f in frames], [voice_bits(f) for f in heard])
                row[name]["bits_offset"] = offset
                if offset is not None:
                    pairs = zip(frames, heard[offset:offset + len(frames)])
                    row[name]["b2_steps"] = sorted({
                        nxdn.gain_index(nxdn.from_dmr(b)) - nxdn.gain_index(nxdn.from_dmr(a))
                        for a, b in pairs})
        rows[row["cell"]] = row
    RESULTS["cells"] = list(rows.values())
    return rows


def ids(cells_):
    return ["%s-%s" % c for c in cells_]


def row_of(cells_, cell):
    row = cells_["%s-%s" % cell]
    for name in ("ref", "sut"):
        assert "error" not in row[name], "%s: %s" % (name, row[name]["error"])
        assert row[name]["echo"] == 0, "%s: talker heard %d of its own frames" % (
            name, row[name]["echo"])
    return row


@pytest.mark.parametrize("cell", PASSTHROUGH, ids=ids(PASSTHROUGH))
def test_dstar_family_exact(cells, cell):
    row = row_of(cells, cell)
    for name in ("ref", "sut"):
        assert row[name]["exact"], "%s: %d frames, not the sent frames" % (
            name, row[name]["received"])


@pytest.mark.parametrize("cell", GAIN_STEPPED, ids=ids(GAIN_STEPPED))
def test_ysf_gain_step_only(rig, cells, cell):
    """All 49 voice bits but b2 arrive unchanged, as one contiguous run, and
    sut delivers the same bytes as ref. YSF -> YSF under another profile whose
    SPEC["ysf"]["P-YSF-18"]["voice_exact"] is True is a passthrough: the sut
    delivers exactly the sent frames (ref keeps its gain steps)."""
    row = row_of(cells, cell)
    if cell == ("ysf", "ysf") and rig.sut_profile != specs.DEFAULT and \
            (specs.rule(rig, "ysf") or {}).get("P-YSF-18", {}).get("voice_exact"):
        assert (row["sut"]["received"], row["sut"]["sha256"]) == (row["sent"], row["input_sha256"]), \
            "sut: %d frames, not the %d sent frames" % (row["sut"]["received"], row["sent"])
        return
    for name in ("ref", "sut"):
        assert row[name]["bits_offset"] is not None, \
            "%s: the sent voice bits are not in the %d received frames" % (name, row[name]["received"])
    assert (row["sut"]["received"], row["sut"]["sha256"]) == \
        (row["ref"]["received"], row["ref"]["sha256"]), "sut differs from ref"


@pytest.mark.parametrize("cell", CROSS, ids=ids(CROSS))
def test_cross_sut_equals_ref(cells, cell):
    row = row_of(cells, cell)
    assert row["ref"]["received"] >= FRAMES * 0.8, "ref got %d frames" % row["ref"]["received"]
    assert (row["sut"]["received"], row["sut"]["sha256"]) == \
        (row["ref"]["received"], row["ref"]["sha256"]), "sut differs from ref"


def lifecycle(ip, kind):
    """Link, hear a talk, unlink, hear nothing of the next talk."""
    out = {}
    source = LIFECYCLE_TALKER[kind]
    frames = signals.prng_frames("dstar", FRAMES, seed="clients2-life")
    if source == "dmr":
        frames = [nxdn.to_dmr(nxdn.from_dmr(f))
                  for f in signals.prng_frames("dmr", FRAMES, seed="clients2-life")]
    listener = make(kind, MODULE, 1)
    talker = make(source, MODULE, 0)
    try:
        out["linked"] = listener.link(ip)
        assert talker.link(ip), "%s talker link failed" % source
        listener.client.inbox.clear()
        talk(talker, source, frames)
        out["heard_linked"] = len(listener.collect())
        listener.client.inbox.clear()
        listener.unlink()
        if kind == "dplus":
            out["unlink_reply"] = dplus.DISCONNECT in listener.client.inbox
        elif kind == "dcs":
            out["unlink_reply"] = any(dcs.parse_ack(p) == "NAK" for p in listener.client.inbox)
        else:
            out["unlink_reply"] = True          # WiresX reply content is not checked
        listener.client.inbox.clear()
        talk(talker, source, frames)
        out["heard_unlinked"] = len(listener.collect())
    finally:
        listener.dest = None                    # already unlinked
        clients.close(talker, listener)
    return out


@pytest.fixture(scope="module")
def lifecycles(rig):
    rows = {}
    for kind in NEW:
        got = {}

        def side(name, ip):
            try:
                got[name] = lifecycle(ip, kind)
            except Exception as exc:            # reported by the test
                got[name] = {"error": repr(exc)}

        threads = [threading.Thread(target=side, args=(n, ip))
                   for n, ip in (("ref", rig.xlxd_ref), ("sut", rig.xlxd_sut))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows[kind] = got
    RESULTS["lifecycle"] = rows
    return rows


@pytest.mark.parametrize("kind", NEW)
def test_link_unlink_lifecycle(lifecycles, kind):
    for name in ("ref", "sut"):
        row = lifecycles[kind][name]
        assert "error" not in row, "%s: %s" % (name, row["error"])
        assert row["linked"], "%s: no link ACK" % name
        assert row["heard_linked"] >= FRAMES * 0.8, "%s: heard %d frames while linked" % (
            name, row["heard_linked"])
        assert row["unlink_reply"], "%s: no unlink reply" % name
        assert row["heard_unlinked"] == 0, "%s: heard %d frames after unlink" % (
            name, row["heard_unlinked"])


def test_write_results(rig, cells, lifecycles):
    out = Path(rig.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "clients2.json").write_text(json.dumps(RESULTS, indent=1) + "\n")
