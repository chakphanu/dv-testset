# SPDX-License-Identifier: MIT
"""Stream statistics and the checker CLI; Wire builds the synthetic
captures the test_check_* files use."""
import json

import pytest

from dvtest import check, pcap
from dvtest.check.common import Stream

A, B, C = "192.0.2.10", "192.0.2.30", "192.0.2.21"


class Wire:
    """Datagrams with a clock; save() writes pcap or pcapng, check() runs
    the checker on it."""

    def __init__(self, t0=1700000000.0):
        self.t = t0
        self.rows = []

    def send(self, src, sport, dst, dport, payload, gap=0.0):
        self.t += gap
        self.rows.append((self.t, pcap.ethernet_udp(src, sport, dst, dport, payload)))

    def wait(self, seconds):
        self.t += seconds

    def save(self, path, fmt="pcapng", end="<"):
        pcap.write(path, self.rows, fmt, end)
        return str(path)

    def check(self, tmp_path, fmt="pcapng", end="<"):
        return check.check_file(self.save(tmp_path / ("cap." + fmt), fmt, end))


def codes(result, severity=None):
    return sorted({f["code"] for f in result["findings"] if severity in (None, f["severity"])})


def frames_of(result, code):
    return [f["frame"] for f in result["findings"] if f["code"] == code]


def test_stream_loss_dup_reorder_and_wrap():
    d = pcap.Datagram(1, 0.0, A, 1, B, 2, b"")
    st = Stream("x", "f", "s", 21, 0.02, d)
    seqs = [19, 20, 0, 1, 3, 3, 2, 4]
    got = [st.add(d._replace(frame=i + 1, ts=i * 0.02), s) for i, s in enumerate(seqs)]
    assert got == [None, None, None, None, ("loss", 1), ("dup", 0), ("reorder", 0), None]
    assert (st.frames, st.loss, st.dup, st.reorder) == (8, 0, 1, 1)


def test_stream_expected_first_and_jitter():
    d = pcap.Datagram(1, 0.0, A, 1, B, 2, b"")
    st = Stream("x", "f", "s", 256, 0.02, d, expect=0)
    assert st.add(d._replace(ts=0.0), 2) == ("loss", 2)
    for i, ts in enumerate((0.02, 0.05, 0.06)):
        st.add(d._replace(ts=ts), 3 + i)
    summary = st.summary()
    assert summary["loss"] == 2 and summary["jitter_ms"]["max"] == pytest.approx(10.0)


def test_cli_exit_codes(tmp_path, capsys):
    clean = Wire()
    clean.send(A, 10100, C, 10100, b"AMBEDPINGN0TEST  ")
    clean.send(C, 10100, A, 10100, b"AMBEDPONG", 0.001)
    clean_path = clean.save(tmp_path / "clean.pcapng")
    bad = Wire()
    bad.send(A, 10100, C, 10100, b"AMBEDPINGN0TEST  ")
    bad.send(A, 10100, C, 10100, b"AMBEDPINGN0TEST  ", 3.0)
    bad.send(C, 10100, A, 10100, b"AMBEDPONG", 0.001)
    bad_path = bad.save(tmp_path / "bad.pcap", "pcap")
    junk = tmp_path / "junk.pcapng"
    junk.write_bytes(b"not a capture at all")
    out = tmp_path / "r.json"
    assert check.main([clean_path]) == 0
    assert check.main([bad_path, "--json", str(out)]) == 1
    result = json.loads(out.read_text())
    assert result["errors"] == 1 and result["files"][0]["findings"][0]["frame"] == 1
    assert check.main([str(junk)]) == 2
    assert check.main([str(tmp_path / "missing.pcapng")]) == 2
    assert check.main(["--bogus"]) == 2
    assert "ambed.no_reply" in capsys.readouterr().out


def test_unclassified_foreign_and_multicast(tmp_path):
    w = Wire()
    w.send(A, 10100, C, 10100, b"AMBEDPINGN0TEST  ")
    w.send(C, 10100, A, 10100, b"AMBEDPONG", 0.001)
    w.send(A, 5000, C, 6000, b"hello")                 # no protocol
    w.send(B, 30001, "192.0.2.99", 40000, b"x" * 27)   # not this host
    w.send(A, 5353, "224.0.0.251", 5353, b"mdns")
    result = w.check(tmp_path)
    assert result["own"] in (A, C)
    assert (result["foreign"], result["multicast"]) == (1, 1)
    assert codes(result) == ["other.unclassified"] and frames_of(result, "other.unclassified") == [3]


def test_rig_ports_unknown_peer_and_outage(tmp_path):
    rig = check.rig_config("172.30.0.0/24")
    assert ("172.30.0.11", 34003) in [e[0] for e in rig["usrp"]]
    w = Wire()
    w.send("172.30.0.11", 10100, "172.30.0.21", 10100, b"AMBEDPINGXLX999  ")
    w.send("192.0.2.7", 10100, "172.30.0.21", 10100, b"AMBEDPINGXLX999  ", 0.01)
    w.send("172.30.0.21", 10100, "192.0.2.7", 10100, b"AMBEDPONG", 0.001)
    w.send("172.30.0.11", 10100, "172.30.0.21", 10100, b"AMBEDPINGXLX999  ", 5.0)
    path = w.save(tmp_path / "transcoder-sut.pcapng")
    result = check.check_file(path, rig)
    assert result["own"] == "172.30.0.21"
    assert frames_of(result, "ambed.unknown_peer") == [2, 3]
    assert frames_of(result, "ambed.no_reply") == [1]
    windows = [("172.30.0.21", w.t - 10, w.t + 10, "transcoder-sut")]
    excused = check.check_file(path, rig, windows=windows)
    late = [f for f in excused["findings"] if f["code"] == "ambed.no_reply"][0]
    assert late["severity"] == "warn" and "control stop" in late["message"]


def test_outages_from_control_log_and_split_captures(tmp_path):
    (tmp_path / "control").mkdir()
    (tmp_path / "control" / "control.log").write_text(
        "2026-10-29T13:34:38.150Z request stop transcoder-sut\n"
        " Container dvt-transcoder-sut-1 Stopping\n"
        "2026-10-29T13:34:48.364Z stop transcoder-sut -> 0\n"
        "2026-10-29T13:35:01.000Z start transcoder-sut -> 0\n"
        "2026-10-29T13:36:00.300Z kill allstar -> 0\n"
        "2026-10-29T13:37:00.010Z stop xlxd-sut -> refused\n")
    hosts = check.rig_config("172.30.0.0/24")["hosts"]
    got = check.outages(str(tmp_path), hosts)
    assert [(ip, svc) for ip, _t0, _t1, svc in got] == [("172.30.0.21", "transcoder-sut"),
                                                       ("172.30.0.40", "allstar")]
    (ip, t0, t1, _), (_, k0, k1, _) = got
    assert (t1 - t0, k1) == (pytest.approx(32.85), float("inf")) and t0 % 60 == pytest.approx(38.15)
    assert k0 % 60 == pytest.approx(58.3)                     # 2 s before an unrequested kill
    cap = tmp_path / "capture"
    cap.mkdir()
    for name in ("allstar.2.pcapng", "allstar.pcapng", "allstar.10.pcapng", "xlxd-sut.pcapng", "notes.txt"):
        (cap / name).write_bytes(b"")
    assert [p.rsplit("/", 1)[1] for p in check.captures(str(tmp_path))] == [
        "allstar.pcapng", "allstar.2.pcapng", "allstar.10.pcapng", "xlxd-sut.pcapng"]
