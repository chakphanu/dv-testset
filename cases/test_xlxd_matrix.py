# SPDX-License-Identifier: MIT
"""Digital <-> digital matrix on xlxd-ref and xlxd-sut of the docker rig.

Passthrough cells (dstar, dmr, m17 to the same kind) use seeded PRNG
frames: the listener must get them byte-identical, on both reflectors,
and their sha256/count must match baselines/<ref>/matrix.json.
Cross cells (dstar<->dmr, m17->dmr/dstar, dmr/dstar->m17) are
differential: the same input (tone bursts encoded by transcoder-ref at run
time) goes through both reflectors in the same run, and xlxd-sut must
deliver exactly the frames xlxd-ref delivers (both use the same
transcoder image).

Module D carries no USRP endpoint in any profile; with DVT_SUT_USRP=on
module B (USRP-bridged on xlxd-sut only) is added, which checks that a
bridged module leaves digital audio unchanged.

DVT_UPDATE_BASELINE=1 or pytest --update-baseline (run.sh ... -- --update-baseline)
writes the baseline instead of comparing.
Results: $DVT_OUT/matrix.json.
"""
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from dvtest import clients
from dvtest.audio import signals
from dvtest.proto import ambed

pytestmark = pytest.mark.rig

ROOT = Path(__file__).resolve().parents[1]
FRAMES = 60
PASSTHROUGH = [("dstar", "dstar"), ("dmr", "dmr"), ("m17", "m17")]
CROSS = [("dstar", "dmr"), ("dmr", "dstar"), ("m17", "dmr"), ("m17", "dstar"),
         ("dmr", "m17"), ("dstar", "m17")]
CELLS = PASSTHROUGH + CROSS


def sha(frames):
    return hashlib.sha256(b"".join(frames)).hexdigest()


def run_cell(ip, src, dst, module, frames):
    """Talker of kind src and listener of kind dst on one module."""
    listener = clients.make(dst, module, 1)
    talker = clients.make(src, module, 0)
    try:
        assert listener.link(ip), "%s listener link to %s failed" % (dst, ip)
        assert talker.link(ip), "%s talker link to %s failed" % (src, ip)
        listener.client.drain()
        listener.client.inbox.clear()
        clients.talk(talker, src, frames)
        return listener.collect()
    finally:
        clients.close(talker, listener)


@pytest.fixture(scope="module")
def modules():
    return ["D", "B"] if os.environ.get("DVT_SUT_USRP") == "on" else ["D"]


@pytest.fixture(scope="module")
def encoded(rig):
    """Tone bursts encoded once by transcoder-ref: {kind: frames}."""
    pcm = signals.frames(signals.bursts(700, 3, on_ms=200, off_ms=200))[:FRAMES]
    out = ambed.transcode(rig.ambed_ref, ambed.PCM, [clients.CODEC[k] for k in clients.KINDS], pcm,
                          port=rig.ambed_port)
    return {k: out[clients.CODEC[k]] for k in clients.KINDS}


@pytest.fixture(scope="module")
def matrix(rig, modules, encoded):
    """Every cell on both reflectors; ref and sut run concurrently."""
    rows = []
    for module in modules:
        for src, dst in CELLS:
            if (src, dst) in PASSTHROUGH:
                frames = signals.prng_frames(src, FRAMES, seed="matrix")
            else:
                frames = encoded[src]
            got = {}

            def side(name, ip):
                try:
                    got[name] = run_cell(ip, src, dst, module, frames)
                except Exception as exc:            # reported by the cell test
                    got[name] = exc

            threads = [threading.Thread(target=side, args=(n, ip))
                       for n, ip in (("ref", rig.xlxd_ref), ("sut", rig.xlxd_sut))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            row = {"cell": "%s-%s" % (src, dst), "module": module, "sent": len(frames),
                   "input_sha256": sha(frames), "kind": "passthrough" if src == dst else "cross"}
            for name in ("ref", "sut"):
                value = got[name]
                if isinstance(value, Exception):
                    row[name] = {"error": repr(value)}
                else:
                    row[name] = {"received": len(value), "sha256": sha(value)}
                    if src == dst:
                        row[name]["exact"] = value == clients.wire_frames(src, frames)
            rows.append(row)
    out = Path(rig.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "matrix.json").write_text(json.dumps({"rows": rows}, indent=1) + "\n")
    return {(r["cell"], r["module"]): r for r in rows}


def ids(cells):
    return ["%s-%s" % c for c in cells]


def row_of(matrix, cell, module):
    row = matrix[("%s-%s" % cell, module)]
    for name in ("ref", "sut"):
        assert "error" not in row[name], "%s: %s" % (name, row[name]["error"])
    return row


@pytest.mark.parametrize("cell", PASSTHROUGH, ids=ids(PASSTHROUGH))
def test_passthrough_exact(matrix, modules, cell):
    for module in modules:
        row = row_of(matrix, cell, module)
        for name in ("ref", "sut"):
            assert row[name]["exact"], "%s module %s: %d frames, not the sent frames" % (
                name, module, row[name]["received"])


@pytest.mark.parametrize("cell", CROSS, ids=ids(CROSS))
def test_cross_sut_equals_ref(matrix, modules, cell):
    for module in modules:
        row = row_of(matrix, cell, module)
        assert row["ref"]["received"] >= FRAMES * 0.8, "ref module %s got %d frames" % (
            module, row["ref"]["received"])
        assert (row["sut"]["received"], row["sut"]["sha256"]) == \
            (row["ref"]["received"], row["ref"]["sha256"]), "module %s: sut differs from ref" % module


def baseline_path(rig):
    name = os.environ.get("DVT_BASELINE")
    if not name:
        fetched = ROOT / ".cache" / "xlxd" / "ref.fetched"
        if not fetched.exists():
            pytest.skip("no baseline name: set DVT_BASELINE or fetch .cache/xlxd/ref")
        name = fetched.read_text().split()[2][:7]
    return ROOT / "baselines" / name / "matrix.json"


def test_passthrough_baseline(rig, matrix, modules, update_baseline):
    current = {}
    for cell in PASSTHROUGH:
        for module in modules:
            row = row_of(matrix, cell, module)
            current["%s/%s" % (row["cell"], module)] = {
                "sent": row["sent"], "received": row["ref"]["received"], "sha256": row["ref"]["sha256"]}
    path = baseline_path(rig)
    if update_baseline:
        path.parent.mkdir(parents=True, exist_ok=True)
        known = json.loads(path.read_text())["cells"] if path.exists() else {}
        known.update(current)
        path.write_text(json.dumps({"frames": FRAMES, "seed": "matrix", "cells": known},
                                   indent=1, sort_keys=True) + "\n")
        return
    if not path.exists():
        pytest.skip("no baseline %s (--update-baseline writes it)" % path.relative_to(ROOT))
    known = json.loads(path.read_text())["cells"]
    for key, value in current.items():
        if key not in known:
            continue
        assert value == known[key], "%s differs from %s" % (key, path.relative_to(ROOT))
