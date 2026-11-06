# SPDX-License-Identifier: MIT
"""dvtest.lora_gw: the scripted raw TNC2 / u16 binary source for the LoRa APRS gateway path, the models of
DV-Router's framer and of the worker's TNC2 parser (restated from the cited source lines), the committed vectors
vectors/lora_gw/*.json and the loopback TCP source. Offline (127.0.0.1 only). The expectations are VERIFIED
against the DV-Router source by reading (file:line per scenario), not against a running gateway."""
import json
import os
import re
import socket
import threading

import pytest

from dvtest import airtime, ax25
from dvtest import lora_gw as g

VEC = os.path.join(os.path.dirname(__file__), "..", "..", "vectors", "lora_gw")


def _outcome(variant, prefix, framing):
    r = g.framer_events(variant.chunks, prefix, framing)
    return ([e["payload"] for e in r["events"] if e["verdict"] == "accept"],
            [e["raw"] for e in r["events"] if e["verdict"] == "reject"],
            r["bad_frames"], r["worker_invalid"], r["closed"])


def _text(prefix, data):
    return TextResults(g.TextFramer(prefix).feed(data))


def TextResults(events):
    """The events in the shape of raw_tests.rs: Ok(bytes) / Err(kind)."""
    return [("Ok", ev[1]) if ev[0] == "ok" else ("Err", ev[1]) for ev in events]


def _binary(prefix, data):
    return TextResults(g.BinaryFramer(prefix).feed(data))


# ------------------------------------------------------------------ the cases of raw_tests.rs, through the models

def test_model_reproduces_raw_tests_rs_text_prefix():
    # edge/ax25/src/raw_tests.rs:10-24
    line = b"N0CALL>APZDVR,WIDE1-1:>READY"
    assert _text(b"<\xff\x01", b"<\xff\x01" + line + b"\r\n") == [("Ok", line)]
    assert _text(b"<\xff\x01", b"N0CALL>APZDVR:>BAD\n") == [("Err", "Invalid")]


def test_model_reproduces_raw_tests_rs_binary_length():
    # raw_tests.rs:27-41: 3 prefix octets + 3 payload octets = u16BE 0x0006; FFFF is TooLong
    assert _binary(b"<\xff\x01", b"\x00\x06<\xff\x01ABC\x00\x04<\xff\x01D") == [("Ok", b"ABC"), ("Ok", b"D")]
    assert _binary(b"", b"\xff\xff") == [("Err", "TooLong")]


def test_model_reproduces_raw_tests_rs_recovery():
    # raw_tests.rs:44-56: overflow, embedded CR, embedded NUL, then a good line
    data = b"X" * (g.MAX_LINE + 1) + b"\nBAD\rX\nBAD\0X\nN0CALL>APZDVR:>OK\n"
    assert _text(b"", data) == [("Err", "TooLong"), ("Err", "Invalid"), ("Err", "Invalid"), ("Ok", b"N0CALL>APZDVR:>OK")]


def test_model_reproduces_raw_tests_rs_exact_bounds():
    # raw_tests.rs:59-79
    assert _text(b"<\xff\x01", b"<\xff\x01" + b"X" * g.MAX_LINE + b"\n") == [("Ok", b"X" * g.MAX_LINE)]
    assert _text(b"<\xff\x01", b"<\xff\x01" + b"X" * (g.MAX_LINE + 1) + b"\n") == [("Err", "TooLong")]
    assert _text(b"", b"X" * g.MAX_LINE + b"\n") == [("Ok", b"X" * g.MAX_LINE)]
    raw = bytes([0x04, 0x4F]) + b"<\xff\x01" + b"\x42" * g.MAX_FRAME          # 1100 + 3 = 1103 = 0x044F
    assert _binary(b"<\xff\x01", raw) == [("Ok", b"\x42" * g.MAX_FRAME)]
    assert _binary(b"<\xff\x01", bytes([0x04, 0x50]) + b"<\xff\x01" + b"\x42" * 1101) == [("Err", "TooLong")]
    assert _binary(b"", b"\0\0\0\x01A") == [("Err", "Invalid")]
    with pytest.raises(ValueError):
        g.TextFramer(b"\n")
    with pytest.raises(ValueError):
        g.BinaryFramer(b"A" * (g.MAX_PREFIX + 1))
    g.BinaryFramer(b"\n\x00\r")                                                   # a binary prefix may hold any byte


def test_text_framing_rules_one_by_one():
    f = lambda data, prefix=b"": _text(prefix, data)                             # noqa: E731
    assert f(b"abc\n") == [("Ok", b"abc")]                                       # LF alone ends a line (raw.rs:60)
    assert f(b"abc\r\n") == [("Ok", b"abc")]                                     # CR only before LF (raw.rs:74-77)
    assert f(b"abc\rd\n") == [("Err", "Invalid")]                                # CR followed by anything else (raw.rs:70)
    assert f(b"abc\r\r\n") == [("Err", "Invalid")]
    assert f(b"a\0c\n") == [("Err", "Invalid")]                                  # NUL (raw.rs:70)
    assert f(b"\n") == [("Err", "Invalid")]                                       # empty line (raw.rs:124-126)
    assert f(b"\r\n") == [("Err", "Invalid")]
    assert f(b"abc") == []                                                       # no terminator: nothing
    assert f(b"\x3c\xff\x01\n", b"\x3c\xff\x01") == [("Err", "Invalid")]      # prefix only: empty payload
    assert f(b"\x3c\xff\x02x\n", b"\x3c\xff\x01") == [("Err", "Invalid")]
    assert f(b"x" * 511 + b"\0\n") == [("Err", "TooLong")]                       # the first error wins
    assert f(b"a\0" + b"x" * 600 + b"\n") == [("Err", "Invalid")]
    assert f(b"abc\r") == []                                                     # CR then EOF: nothing
    # a CR does not count towards the 510 byte limit
    assert f(b"x" * 510 + b"\r\n") == [("Ok", b"x" * 510)]
    # state is fully reset at LF
    assert f(b"a\0\nb\n") == [("Err", "Invalid"), ("Ok", b"b")]


def test_binary_framing_rules_one_by_one():
    f = lambda data, prefix=b"": _binary(prefix, data)                           # noqa: E731
    assert f(b"\x00\x01A") == [("Ok", b"A")]
    assert f(b"\x00\x00A") == [("Err", "Invalid")]                               # zero length: terminal
    assert f(b"\x00\x03abc", b"abc") == [("Err", "Invalid")]                     # length == prefix length: terminal
    assert f(b"\x00\x04abcD", b"abc") == [("Ok", b"D")]
    assert f(b"\x00\x04xbcD", b"abc") == [("Err", "Invalid")]                    # prefix mismatch, NOT terminal ...
    fr = g.BinaryFramer(b"abc")
    assert [e[:2] for e in fr.feed(b"\x00\x04xbcD\x00\x04abcE")] == [("bad", "Invalid"), ("ok", b"E")] and not fr.terminal
    fr = g.BinaryFramer(b"")
    assert [e[:2] for e in fr.feed(b"\x04\x4d" + b"x" * 5000 + b"\x00\x01A")] == [("bad", "TooLong")] and fr.terminal   # 1101: terminal, the rest is ignored
    fr = g.BinaryFramer(b"")
    assert fr.feed(b"\x04\x4c") == [] and not fr.terminal                       # 1100 is the largest
    assert g.BinaryFramer(b"").feed(b"\x00") == [] and g.BinaryFramer(b"").feed(b"") == []


def test_worker_tnc2_rules():
    ok = g.worker_tnc2_ok
    assert ok(b"N0CALL-7>APLRT1:>Test status")
    assert ok(b"N0CALL-16>APLRT1:x") and ok(b"N0CALLX>APLRT1:x") and ok(b"N0CALL-AB>APLRT1:x")   # not AX.25 limits
    assert ok(b"N0CALL-7>APLRT1,D1,D2,D3,D4,D5,D6,D7,D8,D9:x")                                      # 9 path entries
    assert ok(b"N0CALL-7>APLRT1," + b",".join(b"D%d" % i for i in range(1, 33)) + b":x")            # 32 path entries
    assert not ok(b"N0CALL-7>APLRT1," + b",".join(b"D%d" % i for i in range(1, 34)) + b":x")        # 33
    assert ok(b"N0CALL-7>APLRT1,qAR,WIDE1-1*:x") and not ok(b"N0CALL-7>APLRT1,qA1:x")
    assert ok(b"N0CALL-7>APLRT1:") and ok(b"N0CALL-7>APLRT1:\xc3\xa9\xff")                         # info is never judged
    assert not ok(b"n0call>aplrt1:x") and not ok(b"N0CALL-7>:x") and not ok(b">APLRT1:x")
    assert not ok(b"N0CALL-7>APLRT1") and not ok(b"N0CALL-7APLRT1:x") and not ok(b"N0CALL-7>APLRT1,,D1:x")
    assert not ok(b"N0CALL-7-8>APLRT1:x") and not ok(b"N0CALLNO-77>APLRT1:x")                       # one suffix; 9 characters at most
    assert not ok(b"\xff\x01N0CALL>APLRT1:x")                                                       # header must be UTF-8
    assert not ok(b"x" * 511) and not ok(b"N0CALL-7>APLRT1:te\x00st")


def test_edge_ax25_rules():
    f = ax25.from_tnc2("N0CALL-7>APLRT1:>Test status")
    assert g.edge_ax25_ok(f) and g.edge_ax25_ok(f[:16]) and not g.edge_ax25_ok(f[:15]) and not g.edge_ax25_ok(b"hello world")
    assert not g.edge_ax25_ok(f[:14] + b"\x13" + f[15:]) and not g.edge_ax25_ok(f[:15] + b"\xcf" + f[16:])
    assert g.edge_ax25_ok(ax25.from_tnc2("A>B,D1,D2,D3,D4,D5,D6,D7,D8:x"))                          # 8 digipeaters
    assert not g.edge_ax25_ok(g.SCENARIO_BY_NAME["bin-nine-digipeaters"].variant("none").rejects[0][1])
    assert g.edge_ax25_ok(g._ui_of_size(1100)) and not g.edge_ax25_ok(g._ui_of_size(1101))


# ------------------------------------------------------------------ scenario table

def test_what_was_asked_for_is_there():
    names = {s.name for s in g.SCENARIOS}
    groups = {s.group for s in g.SCENARIOS}
    for needed in ["unprefixed-line", "prefixed-line", "line-crlf", "lf-only", "cr-only", "prefix-3cff02", "prefix-3dff01",
                   "prefix-two-bytes", "size-511", "size-4096", "overlong-then-good", "empty-line", "split-mid-line",
                   "split-in-prefix", "split-bytewise", "mid-line-connect", "no-terminator-open",
                   "bin-valid", "bin-zero-length", "bin-max", "bin-over-max", "bin-split-header", "bin-header-ffff"]:
        assert needed in names, needed
    assert {"basic", "profile", "ending", "empty", "prefix", "garbage", "size", "split", "resync", "binary"} <= groups
    assert len(names) == len(g.SCENARIOS)
    assert sum(1 for s in g.SCENARIOS if s.framing == "text") == 64          # the 62 earlier scenarios and 2 CR rules
    assert sum(1 for s in g.SCENARIOS if s.framing == "raw") == 21


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
            assert v.framing == s.framing
            assert _outcome(v, prefix, s.framing) == (v.accept, v.reject, v.bad_frames, v.worker_invalid, v.closed), (s.name, profile)


def test_every_scenario_cites_source_lines_and_is_verified():
    doc = g.streams_document()
    cite = re.compile(r"^(edge|lib|data)/[a-z0-9_/]+\.rs:\d+(-\d+)?$")
    for rec in doc["scenarios"]:
        assert rec["status"] == "VERIFIED", rec["name"]
        assert rec["sources"] and all(cite.match(c) for c in rec["sources"]), rec["name"]
        assert rec["basis"] and all(b in g.BASIS for b in rec["basis"]), rec["name"]
    assert set(doc["basis"]) == set(g.BASIS)
    assert "1bb304f0" in doc["source_tree"]
    assert set(g.C) >= {k for s in g.SCENARIOS for k in s.cites}


def test_split_scenarios_equal_their_unsplit_twin():
    for name, twin in g.SPLIT_TWINS.items():
        for profile in g.PROFILES:
            a, b = g.SCENARIO_BY_NAME[name].variant(profile), g.SCENARIO_BY_NAME[twin].variant(profile)
            assert a.stream() == b.stream(), (name, profile)
            assert (a.accept, a.rejects) == (b.accept, b.rejects)
            assert len(a.chunks) > len(b.chunks) or name == "split-after-prefix" and profile == "none"
    # bytewise: one byte per read, same expectation as the whole line
    v = g.SCENARIO_BY_NAME["split-bytewise"].variant("3cff01")
    assert all(len(c) == 1 for c in v.chunks) and v.accept == [g.POSITION]
    # cut between CR and LF really separates them
    v = g.SCENARIO_BY_NAME["split-between-cr-lf"].variant("none")
    assert v.chunks[0].endswith(b"\r") and v.chunks[1] == b"\n"
    # the header of a binary frame is split too
    v = g.SCENARIO_BY_NAME["bin-split-header"].variant("none")
    assert len(v.chunks[0]) == 1 and v.accept


def test_splitting_never_changes_the_outcome_for_any_cut():
    # every single cut of every scenario stream gives the same outcome (the framer is bytewise: raw.rs:55)
    for s in g.SCENARIOS:
        if s.group == "size":
            continue
        for profile, prefix in g.PROFILES.items():
            v = s.variant(profile)
            whole = g.framer_events([v.stream()], prefix, s.framing)
            data = v.stream()
            for cut in range(1, min(len(data), 120)):
                assert g.framer_events([data[:cut], data[cut:]], prefix, s.framing) == whole, (s.name, profile, cut)


def test_size_boundaries_count_the_bytes_after_the_prefix():
    for n, accept in [(80, True), (255, True), (256, True), (510, True), (511, False), (4096, False)]:
        for profile in g.PROFILES:
            v = g.SCENARIO_BY_NAME["size-%d" % n].variant(profile)
            assert bool(v.accept) is accept, (n, profile)
            assert len(v.accept[0] if accept else v.rejects[0][1]) == n + (0 if accept else len(g.PROFILES[profile]))
    # the wire line may hold 510 + len(prefix) bytes: 513 for 3cff01
    v = g.SCENARIO_BY_NAME["size-510"].variant("3cff01")
    assert len(v.chunks[0]) == 513 + 2 and v.accept
    with pytest.raises(ValueError):
        g._line_of_size(3)


def test_line_rules():
    c = g.classify_line
    assert c(b"") == ("reject", None)                             # an empty line is a bad frame, not ignored
    assert c(b"", g.PREFIX) == ("reject", None)
    assert c(g.PREFIX, g.PREFIX)[0] == "reject"                   # prefix only
    assert c(g.PREFIX + g.PREFIX + g.STATUS, g.PREFIX)[0] == "reject"
    assert c(g.STATUS, g.PREFIX)[0] == "reject"                   # prefix missing
    assert c(g.PREFIX + g.STATUS)[0] == "reject"                  # prefix not configured
    assert c(b"N0CALL-7>APLRT1,D1*,WIDE2-1:x")[0] == "accept"
    assert c(b"N0CALL-7>APLRT1:x\ry")[0] == "reject"              # CR inside a line
    assert c(b"n0call>aplrt1:x")[0] == "reject"
    assert c(b"N0CALLX>APLRT1:x")[0] == "accept"                  # not AX.25, valid TNC2


def test_tail_handling_in_the_stream_model():
    m = g.classify_stream
    assert m([g.STATUS]) == []                                    # unfinished, connection open
    assert m([g.STATUS], eof=True) == []                          # a partial line is not flushed at close
    assert m([g.STATUS + b"\r"]) == []
    assert m([g.STATUS + b"\n"]) == [("accept", g.STATUS, g.STATUS)]
    assert m([b"x" * 600]) == []                                  # TooLong is reported at the LF only
    assert m([g.STATUS + g.CRLF + g.POSITION]) == [("accept", g.STATUS, g.STATUS)]
    assert m([g.STATUS + b"\r\n"], framing="text") == [("accept", g.STATUS, g.STATUS)]


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
    assert doc["schema"] == "dvt-lora-gw-streams/2" and "1bb304f0" in doc["source_tree"]
    assert doc["prefix_hex"] == "3cff01" and doc["terminator_hex"] == "0d0a"
    assert len(doc["scenarios"]) == len(g.SCENARIOS)
    for rec in doc["scenarios"]:
        for profile, var in rec["variants"].items():
            chunks = [bytes.fromhex(h) for h in var["chunks_hex"]]
            prefix = bytes.fromhex(doc["profiles"][profile])
            r = g.framer_events(chunks, prefix, rec["framing"])
            assert [e["payload"].hex() for e in r["events"] if e["verdict"] == "accept"] == var["accept_hex"], (rec["name"], profile)
            assert [e["raw"].hex() for e in r["events"] if e["verdict"] == "reject"] == var["reject_hex"], (rec["name"], profile)
            assert r["bad_frames"] == var["edge_bad_frames"], (rec["name"], profile)
            assert [p.hex() for p in r["worker_invalid"]] == var["worker_invalid_hex"], (rec["name"], profile)
            assert r["closed"] == var["connection_closed"], (rec["name"], profile)
            assert var["unspecified_hex"] == []


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
                if rec["framing"] == "text":
                    assert line.startswith((b"N0CALL-7>", b"N0CALL-16>", b"N0CALLX>")), rec["name"]
                else:
                    assert ax25.parse_frame(line)["src"]["text"] == "N0CALL-7", rec["name"]


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
