# SPDX-License-Identifier: MIT
"""dvtest.node1g_capture: the fold rule and the reordering counts, on synthetic captures of link datagrams (no rig)."""
import json
import struct

import pytest

from dvtest import node1g_capture as cap, pcap, rfpos1

pytestmark = pytest.mark.unit

SID = 0x1234
SESSION = 77


def rec(kind, counter, body):
    return rfpos1.build_record(kind, SESSION, counter, body)


def start(counter):
    body = struct.pack("<BIQBHH", 0, SID, 1, 0, 4, 0) + b"N0CALL    " + struct.pack("<IB", 1023007, 0)
    return rec(19, counter, body)


def units(counter, seq, stream=SID):
    return rfpos1.build_units(SESSION, counter, stream, 4, seq, b"\x01" * 11)


def end(counter, reason=1):
    return rec(21, counter, struct.pack("<IH", SID, reason))


def frame(ts, payload, to_central=True):
    if to_central:
        return ts, pcap.ethernet_udp("172.30.0.98", 5000, "172.30.0.11", 31800, payload)
    return ts, pcap.ethernet_udp("172.30.0.11", 31800, "172.31.0.16", 5000, payload)


def write(tmp_path, name, packets):
    path = tmp_path / name
    pcap.write(str(path), packets)
    return str(path)


def test_a_stream_cut_by_a_blackout_has_no_end_and_folds_end_missing(tmp_path):
    t0, cut, end_t = 1000.0, 1003.0, 1023.0
    packets = [frame(t0, start(1))] + [frame(t0 + 0.02 * i, units(2 + i, i)) for i in range(1, 150)]       # units until the cut, End lost in it
    path = write(tmp_path, "central.pcapng", packets)
    found = cap.streams(cap.read(path, "central"), t0 - 1, t0 + 2)
    assert len(found) == 1 and found[0]["stream_id"] == SID and found[0]["units"] == 149 and found[0]["holes"] == 0
    assert cap.end_state(found[0], cut, end_t) == "none"
    assert cap.expected_fold(found[0], cut, end_t) == "end_missing"


def test_complete_needs_an_end_outside_the_blackout_and_no_hole(tmp_path):
    t0, cut, end_t = 1000.0, 1003.0, 1023.0
    whole = [frame(t0, start(1))] + [frame(t0 + 0.02 * i, units(1 + i, i)) for i in range(1, 100)] + [frame(t0 + 2.1, end(101))]
    s = cap.streams(cap.read(write(tmp_path, "a.pcapng", whole), "central"), t0 - 1, t0 + 2)[0]
    assert cap.end_state(s, cut, end_t) == "before" and cap.expected_fold(s, cut, end_t) == "complete"
    holey = [p for i, p in enumerate(whole) if i != 50]                                                    # one unit never arrived
    s = cap.streams(cap.read(write(tmp_path, "b.pcapng", holey), "central"), t0 - 1, t0 + 2)[0]
    assert s["holes"] == 1 and cap.expected_fold(s, cut, end_t) == "end_missing"
    inside = whole[:-1] + [frame(t0 + 10.0, end(101))]                                                     # an End that crossed inside the blackout cannot be
    s = cap.streams(cap.read(write(tmp_path, "c.pcapng", inside), "central"), t0 - 1, t0 + 2)[0]
    assert cap.end_state(s, cut, end_t) == "inside" and cap.expected_fold(s, cut, end_t) == "end_missing"
    after = whole[:-1] + [frame(t0 + 24.0, end(101))]
    s = cap.streams(cap.read(write(tmp_path, "d.pcapng", after), "central"), t0 - 1, t0 + 2)[0]
    assert cap.end_state(s, cut, end_t) == "after" and cap.expected_fold(s, cut, end_t) == "complete"


def test_the_hotspot_capture_reads_what_the_central_node_sends(tmp_path):
    packets = [frame(1000.0, start(1), to_central=False), frame(1000.02, units(2, 1), to_central=False), frame(1000.03, units(1, 1))]
    path = write(tmp_path, "hs.pcapng", packets)
    assert len(cap.read(path, "hotspot")) == 2 and len(cap.read(path, "central")) == 1


def test_observed_fold_names_the_one_result_that_moved():
    assert cap.observed_fold({"overs.C.end_missing": 1.0, "dgram.rx.ok": 40.0}, "C") == "end_missing"
    assert cap.observed_fold({"overs.C.complete": 1.0}, "C") == "complete"
    assert cap.observed_fold({"overs.C.complete": 1.0, "overs.C.lossy": 1.0}, "C") == {"complete": 1.0, "lossy": 1.0}
    assert cap.observed_fold({}, "C") == {}


def test_reorder_counts_late_datagrams_and_units(tmp_path):
    t0 = 2000.0
    order = [(1, 1), (2, 2), (4, 4), (3, 3), (5, 5), (7, 7), (6, 6)]                  # (counter, unit position): 3 and 6 arrive late
    packets = [frame(t0 + 0.02 * i, units(c, p)) for i, (c, p) in enumerate(order)]
    r = cap.reorder(cap.read(write(tmp_path, "r.pcapng", packets), "central"), t0 - 1, t0 + 5)
    assert r["datagrams"] == 7 and r["reordered"] == 2 and r["max_counter_back"] == 1
    assert r["units"] == 7 and r["units_reordered"] == 2 and r["max_seq_back"] == 1
    assert r["max_late_ms"] == pytest.approx(20.0, abs=1.0)
    # a new session restarts the counters: not a reordering
    other = rfpos1.build_units(SESSION + 1, 1, SID, 4, 99, b"\x01" * 11)
    r = cap.reorder(cap.read(write(tmp_path, "s.pcapng", packets + [frame(t0 + 1.0, other)]), "central"), t0 - 1, t0 + 5)
    assert r["reordered"] == 2


def test_unit_lateness_is_measured_from_the_unit_that_overtook_it(tmp_path):
    t0 = 4000.0
    # positions 1 2 4 3 (3 arrives 30 ms after 4) 5 7 6 (6 arrives 60 ms after 7)
    arrivals = [(0.00, 1, 1), (0.02, 2, 2), (0.04, 3, 4), (0.07, 4, 3), (0.09, 5, 5), (0.11, 6, 7), (0.17, 7, 6)]
    packets = [frame(t0 + t, units(c, p)) for t, c, p in arrivals]
    r = cap.reorder(cap.read(write(tmp_path, "l.pcapng", packets), "central"), t0 - 1, t0 + 5)
    assert r["units_reordered"] == 2 and [round(x) for x in r["unit_late_ms"]] == [30, 60] and r["units_over_40ms"] == 1


def test_judge_checks_linkseq1_late_counts_against_the_capture(tmp_path):
    run = tmp_path / "20990101T000001Z"
    (run / "node1g").mkdir(parents=True)
    (run / "capture").mkdir()
    t0 = 5000.0
    arrivals = [(0.00, 1, 1), (0.02, 2, 2), (0.04, 3, 4), (0.07, 4, 3), (0.09, 5, 5), (0.11, 6, 7), (0.17, 7, 6)]            # 30 ms and 60 ms late
    pkts = [frame(t0 + t, units(c, p)) for t, c, p in arrivals]
    pcap.write(str(run / "capture" / cap.CENTRAL_PCAP), pkts)
    pcap.write(str(run / "capture" / cap.HOTSPOT_PCAP), [frame(t0 + t, units(c, p), to_central=False) for t, c, p in arrivals])
    win = {"case": "a", "kind": "delay", "t_start": t0 - 1, "t_stop": t0 + 5, "linkseq1": True, "late_units": {"central": 1.0, "hotspot": 0.0}}
    (run / "node1g" / "windows.jsonl").write_text(json.dumps(win) + "\n")
    lines = []
    assert cap.judge(str(run), out=lines.append) == 0 and any("LINKSEQ1" in x and "-> ok" in x for x in lines)          # one unit over 40 ms may be late
    win["late_units"] = {"central": 2.0, "hotspot": 0.0}
    (run / "node1g" / "windows.jsonl").write_text(json.dumps(win) + "\n")
    lines = []
    assert cap.judge(str(run), out=lines.append) == 1 and any("DEFECT: a unit reordered inside 40 ms was counted late" in x for x in lines)


def test_judge_prints_a_defect_with_the_stream_id(tmp_path):
    run = tmp_path / "20990101T000000Z"
    (run / "node1g").mkdir(parents=True)
    (run / "capture").mkdir()
    t0 = 3000.0
    pcap.write(str(run / "capture" / cap.CENTRAL_PCAP), [frame(t0, start(1))] + [frame(t0 + 0.02 * i, units(1 + i, i)) for i in range(1, 100)])
    win = {"case": "m1", "kind": "fold", "dir": "h2c", "t_start": t0, "t_cut": t0 + 3.0, "t_end": t0 + 23.0, "fold": {"central": {"overs.C.complete": 1.0}}}
    (run / "node1g" / "windows.jsonl").write_text(json.dumps(win) + "\n")
    lines = []
    assert cap.judge(str(run), out=lines.append) == 1
    assert "DEFECT" in lines[0] and "stream %d" % SID in lines[0] and "20990101T000000Z" in lines[0]
    win["fold"] = {"central": {"overs.C.end_missing": 1.0}}
    (run / "node1g" / "windows.jsonl").write_text(json.dumps(win) + "\n")
    assert cap.judge(str(run), out=lines.append) == 0 and "agree" in lines[-1]
    assert cap.main([str(run)]) == 0 and cap.main([]) == 2
