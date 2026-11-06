# SPDX-License-Identifier: MIT
"""dvtest.lora_gw: the scripted raw TNC2 source for the LoRa APRS gateway path, its reference
classification, the committed vectors vectors/lora_gw/*.json and the loopback TCP source.
Offline (127.0.0.1 only). Every expectation under test is UNVERIFIED against any gateway."""
import json
import os
import socket
import threading

import pytest

from dvtest import airtime
from dvtest import lora_gw as g

VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "lora_gw")


def _verdicts(variant, prefix):
    model = g.classify_stream(variant.chunks, prefix, variant.eof)
    return ([b for v, _r, b in model if v == "accept"],
            [r for v, r, _b in model if v == "reject"],
            [r for v, r, _b in model if v == "unspecified"])


# ------------------------------------------------------------------ scenario table

def test_what_was_asked_for_is_there():
    names = {s.name for s in g.SCENARIOS}
    groups = {s.group for s in g.SCENARIOS}
    for needed in ["unprefixed-line", "prefixed-line", "line-crlf", "lf-only", "cr-only", "prefix-3cff02", "prefix-3dff01",
                   "prefix-two-bytes", "size-511", "size-4096", "overlong-then-good", "empty-line", "split-mid-line",
                   "split-in-prefix", "split-bytewise", "mid-line-connect", "no-terminator-open"]:
        assert needed in names, needed
    assert {"basic", "profile", "ending", "empty", "prefix", "garbage", "size", "split", "resync"} <= groups
    assert len(names) == len(g.SCENARIOS)


def test_base_lines_are_the_plans_synthetic_lines():
    assert [x.decode() for x in (g.STATUS, g.POSITION, g.IGATE)] == airtime.LORA_APRS_LINES
    for line in (g.STATUS, g.POSITION, g.IGATE):
        assert line.startswith(b"N0CALL-7>")
        assert g.classify_line(line) == ("accept", line)
        assert g.classify_line(g.PREFIX + line, g.PREFIX) == ("accept", line)


def test_every_scenario_matches_the_reference_model_in_both_profiles():
    for s in g.SCENARIOS:
        for profile, prefix in g.PROFILES.items():
            v = s.variant(profile)
            assert v.chunks and all(isinstance(c, bytes) and c for c in v.chunks), (s.name, profile)
            assert _verdicts(v, prefix) == (v.accept, v.reject, v.unspecified), (s.name, profile)


def test_every_expectation_has_a_basis_and_is_marked_unverified():
    doc = g.streams_document()
    for rec in doc["scenarios"]:
        assert rec["status"] == "UNVERIFIED"
        assert rec["basis"] and all(b in g.BASIS for b in rec["basis"]), rec["name"]
    assert set(doc["basis"]) == set(g.BASIS)


def test_split_scenarios_equal_their_unsplit_twin():
    for name, twin in g.SPLIT_TWINS.items():
        for profile in g.PROFILES:
            a, b = g.SCENARIO_BY_NAME[name].variant(profile), g.SCENARIO_BY_NAME[twin].variant(profile)
            assert a.stream() == b.stream(), (name, profile)
            assert (a.accept, a.reject, a.unspecified) == (b.accept, b.reject, b.unspecified)
            assert len(a.chunks) > len(b.chunks) or name == "split-after-prefix" and profile == "none"
    # bytewise: one byte per read, same expectation as the whole line
    v = g.SCENARIO_BY_NAME["split-bytewise"].variant("3cff01")
    assert all(len(c) == 1 for c in v.chunks) and v.accept == [g.POSITION]
    # cut between CR and LF really separates them
    v = g.SCENARIO_BY_NAME["split-between-cr-lf"].variant("none")
    assert v.chunks[0].endswith(b"\r") and v.chunks[1] == b"\n"


def test_splitting_never_changes_the_verdicts_for_any_cut():
    # property check of the model itself: every single cut of every scenario stream gives the same verdicts
    for s in g.SCENARIOS:
        if s.group == "size":
            continue
        for profile, prefix in g.PROFILES.items():
            v = s.variant(profile)
            whole = g.classify_stream([v.stream()], prefix, v.eof)
            data = v.stream()
            for cut in range(1, min(len(data), 120)):
                assert g.classify_stream([data[:cut], data[cut:]], prefix, v.eof) == whole, (s.name, profile, cut)


def test_size_boundaries():
    p = g.PREFIX
    for n, expect in [(80, "accept"), (255, "accept"), (256, "unspecified"), (510, "unspecified"), (511, "reject"), (4096, "reject")]:
        line = g._line_of_raw_size(n - 3)
        assert g.classify_line(p + line, p)[0] == expect, n
        line = g._line_of_raw_size(n)
        assert g.classify_line(line)[0] == expect, n
    with pytest.raises(ValueError):
        g._line_of_raw_size(3)


def test_line_rules():
    c = g.classify_line
    assert c(b"") == ("ignore", None)
    assert c(b"", g.PREFIX) == ("ignore", None)
    assert c(g.PREFIX, g.PREFIX)[0] == "reject"                   # prefix only
    assert c(g.PREFIX + g.PREFIX + g.STATUS, g.PREFIX)[0] == "reject"
    assert c(g.STATUS, g.PREFIX)[0] == "reject"                   # prefix missing
    assert c(g.PREFIX + g.STATUS)[0] == "reject"                  # prefix not configured
    assert c(b"N0CALL-7>APLRT1,D1*,WIDE2-1:x")[0] == "accept"
    assert c(b"N0CALL-7>APLRT1:x\ry")[0] == "unspecified"         # bare CR inside a line
    assert c(b"n0call>aplrt1:x")[0] == "unspecified"


def test_tail_handling_in_the_stream_model():
    m = g.classify_stream
    assert m([g.STATUS]) == []                                    # unfinished, connection open
    assert m([g.STATUS], eof=True)[0][0] == "unspecified"
    assert m([g.STATUS + b"\r"]) == []                            # may be the first half of CRLF
    assert m([g.STATUS + b"\n"])[0][0] == "unspecified"
    assert m([b"x" * 600])[0][0] == "reject"                      # no terminator and already over the limit
    assert m([g.STATUS + g.CRLF + g.POSITION]) == [("accept", g.STATUS, g.STATUS)]


# ------------------------------------------------------------------ vectors

def _load(name):
    with open(os.path.join(VEC, name), encoding="ascii") as fh:
        return json.load(fh)


def test_committed_vectors_are_the_regenerated_ones(tmp_path):
    g.write_vectors(str(tmp_path))
    for name in ("lines.json", "streams.json"):
        with open(os.path.join(VEC, name), "rb") as fh:
            committed = fh.read()
        with open(os.path.join(str(tmp_path), name), "rb") as fh:
            assert fh.read() == committed, name
        committed.decode("ascii")


def test_vectors_alone_reproduce_the_verdicts():
    """What the DV-Router side can do with the JSON alone: rebuild the reads, run a reference and compare."""
    doc = _load("streams.json")
    assert doc["prefix_hex"] == "3cff01" and doc["terminator_hex"] == "0d0a"
    assert len(doc["scenarios"]) == len(g.SCENARIOS)
    for rec in doc["scenarios"]:
        for profile, var in rec["variants"].items():
            chunks = [bytes.fromhex(h) for h in var["chunks_hex"]]
            prefix = bytes.fromhex(doc["profiles"][profile])
            model = g.classify_stream(chunks, prefix, var["eof"])
            assert [b.hex() for v, _r, b in model if v == "accept"] == var["accept_hex"], (rec["name"], profile)
            assert [r.hex() for v, r, _b in model if v == "reject"] == var["reject_hex"], (rec["name"], profile)
            assert [r.hex() for v, r, _b in model if v == "unspecified"] == var["unspecified_hex"], (rec["name"], profile)


def test_lines_vector_matches_the_airtime_anchors():
    doc = _load("lines.json")
    anchors = {r["id"]: r for r in airtime.anchor_rows()}
    for i, row in enumerate(doc["rows"], start=1):
        assert bytes.fromhex(row["air_hex"]) == g.PREFIX + bytes.fromhex(row["tnc2_hex"])
        assert bytes.fromhex(row["host_hex"]) == bytes.fromhex(row["tnc2_hex"]) + b"\r\n"
        assert row["payload_bytes"] == len(bytes.fromhex(row["air_hex"]))
        for label in ("sf12", "sf9", "sf7"):
            assert row["air_ms_%s" % label] == anchors["aprs-%d-%s" % (i, label)]["computed_ms"]
    assert doc["rows"][2]["air_ms_sf12"] == "2793.472"


def test_no_real_callsigns_or_addresses_in_the_vectors():
    for name in ("lines.json", "streams.json"):
        with open(os.path.join(VEC, name), encoding="ascii") as fh:
            text = fh.read()
        assert "/home/" not in text and "@" not in text
    doc = _load("streams.json")
    for rec in doc["scenarios"]:
        for var in rec["variants"].values():
            for h in var["accept_hex"]:
                line = bytes.fromhex(h)
                assert line.startswith((b"N0CALL-7>", b"n0call")), rec["name"]


# ------------------------------------------------------------------ the loopback source

def _receive_all(port, out, idle=1.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(idle)
    reads = []
    try:
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            reads.append(d)
    finally:
        s.close()
    out.extend(reads)


@pytest.mark.parametrize("name", ["split-mid-line", "three-lines-three-reads", "overlong-split-then-good"])
def test_serve_once_sends_the_scenario_bytes_on_loopback(name):
    v = g.SCENARIO_BY_NAME[name].variant("3cff01")
    ready = threading.Event()
    port = []
    sent = []

    def on_ready(p):
        port.append(p)
        ready.set()

    t = threading.Thread(target=lambda: sent.append(g.serve_once(v.chunks, delay=0.02, ready=on_ready, hold=0.1, accept_timeout=10)))
    t.start()
    assert ready.wait(5)
    got = []
    _receive_all(port[0], got)
    t.join(10)
    assert b"".join(got) == v.stream()
    assert sent == [len(v.stream())]
    assert len(got) >= 2                                          # the reads are not one big write


def test_serve_once_eof_closes_at_once():
    ready = threading.Event()
    port = []
    t = threading.Thread(target=lambda: g.serve_once([g.STATUS], ready=lambda p: (port.append(p), ready.set()), eof=True, accept_timeout=10))
    t.start()
    assert ready.wait(5)
    got = []
    _receive_all(port[0], got, idle=3.0)
    t.join(10)
    assert b"".join(got) == g.STATUS


def test_cli_list_and_unknown_scenario(capsys):
    assert g.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "split-mid-line" in out and "size-511" in out
    assert g.main(["serve", "--scenario", "no-such"]) == 2
