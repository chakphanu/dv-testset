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
        "2026-10-29T13:37:00.010Z stop xlxd-sut -> refused\n"
        "2026-10-29T13:38:00.000Z request restart xlxd-sut\n"
        "2026-10-29T13:38:01.000Z restart xlxd-sut -> 0\n")
    hosts = check.rig_config("172.30.0.0/24")["hosts"]
    got = check.outages(str(tmp_path), hosts)
    assert [(ip, svc) for ip, _t0, _t1, svc in got] == [("172.30.0.21", "transcoder-sut"),
                                                       ("172.30.0.11", "xlxd-sut restart"),
                                                       ("172.30.0.40", "allstar")]
    assert got[1][2] - got[1][1] == pytest.approx(13.0)
    (ip, t0, t1, _), _, (_, k0, k1, _) = got
    assert (t1 - t0, k1) == (pytest.approx(34.85), float("inf")) and t0 % 60 == pytest.approx(36.15)
    assert k0 % 60 == pytest.approx(58.3)                     # 2 s before an unrequested kill
    cap = tmp_path / "capture"
    cap.mkdir()
    for name in ("allstar.2.pcapng", "allstar.pcapng", "allstar.10.pcapng", "xlxd-sut.pcapng", "notes.txt"):
        (cap / name).write_bytes(b"")
    assert [p.rsplit("/", 1)[1] for p in check.captures(str(tmp_path))] == [
        "allstar.pcapng", "allstar.2.pcapng", "allstar.10.pcapng", "xlxd-sut.pcapng"]


def test_netem_impairment_windows(tmp_path):
    (tmp_path / "control").mkdir()
    (tmp_path / "control" / "control.log").write_text(
        "2026-10-29T16:49:31.529Z request netem tester delay 100ms\n"
        "2026-10-29T16:49:31.797Z netem tester delay 100ms -> 0\n"
        "2026-10-29T16:49:35.330Z netem tester off -> 0\n"
        "2026-10-29T16:49:35.541Z request netem xlxd loss 10%\n"
        "2026-10-29T16:49:35.897Z netem xlxd loss 10% -> 0\n"
        "2026-10-29T16:49:47.256Z netem xlxd bogus;rm -> refused\n")
    (tmp_path / "netem.txt").write_text(
        "==== 2026-10-29T16:49:24.535Z start\n-- netem-xlxd-sut\n"
        "qdisc netem 8005: root refcnt 33 limit 1000 delay 20ms\n"
        "==== 2026-10-29T16:49:31.602Z netem tester delay 100ms\n")
    hosts = check.rig_config("172.30.0.0/24")["hosts"]
    got = {(ip, label): (t0, t1) for ip, t0, t1, label in check.impairments(str(tmp_path), hosts)}
    t0, t1 = got[("172.30.0.30", "netem tester delay 100ms")]
    assert t1 - t0 == pytest.approx(5.801)
    assert got[("172.30.0.10", "netem xlxd loss 10%")][1] == float("inf")
    t0, t1 = got[("172.30.0.11", "netem xlxd-sut limit 1000 delay 20ms")]
    assert t0 == float("-inf") and t1 < float("inf")         # from the start, ended by "netem xlxd"
    # a loss on an AMBEd data stream of an impaired host becomes a warning,
    # a pairing finding (unsolicited reply) keeps its severity
    rig = check.rig_config("172.30.0.0/24")
    path = ambed_wire(tmp_path, "xlxd-sut", drop={2}, stray=True)
    w0 = check.check_file(path, rig)
    windows = [("172.30.0.11", 0, 2e9, "netem xlxd loss 10%")]
    sut = check.check_file(path, rig, windows=windows)
    assert severities(w0, "ambed.pid_loss") == ["error"]
    assert severities(sut, "ambed.pid_loss") == ["warn"]
    assert [f for f in sut["findings"] if f["code"] == "ambed.pid_loss"][0]["message"].endswith(
        "[during netem xlxd loss 10%]")
    assert severities(sut, "ambed.reply_unsolicited") == ["error"]


def ambed_wire(tmp_path, name, drop=(), stray=False):
    """One AMBEd stream from xlxd (.11 sut / .10 ref) to its transcoder
    (.21 / .20), pids 0..39 minus drop."""
    x, t = ("172.30.0.11", "172.30.0.21") if name.endswith("sut") else ("172.30.0.10", "172.30.0.20")
    w = Wire()
    w.send(x, 10100, t, 10100, b"AMBEDOS" + b"XLX999  " + bytes([5, 1, 0, 0, 0]) + bytes(4))
    w.send(t, 10100, x, 10100, b"AMBEDSTD\x01\x00\x75\x27\x05\x01" + bytes(4), 0.001)
    for pid in range(40):
        if pid not in drop:
            w.send(x, 10101, t, 10101, bytes([5, pid]) + bytes(320), 0.02)
            w.send(t, 10101, x, 10101, bytes([1, pid]) + bytes(9), 0.001)
    if stray:
        w.send(t, 10101, x, 10101, bytes([1, 99]) + bytes(9), 0.001)
    return w.save(tmp_path / ("%s.pcapng" % name))


def severities(result, code):
    return [f["severity"] for f in result["findings"] if f["code"] == code]


def test_netem_differential_tolerance(tmp_path):
    """Under the same impairment a sut loss count over 2 x ref + 3 is an
    error; within the tolerance, or with no comparable ref stream, a warning."""
    rig = check.rig_config("172.30.0.0/24")
    windows = [("172.30.0.11", 0, 2e9, "netem xlxd loss 10%"), ("172.30.0.10", 0, 2e9, "netem xlxd loss 10%")]

    def pair(sut_drop, ref_drop, ref_stream=True):
        sut = check.check_file(ambed_wire(tmp_path, "xlxd-sut", sut_drop), rig, windows=windows)
        if ref_stream:
            ref = check.check_file(ambed_wire(tmp_path, "xlxd-ref", ref_drop), rig, windows=windows)
        else:
            ref = check.check_file(Wire().save(tmp_path / "xlxd-ref.pcapng"))
        check.differential([ref, sut])
        return set(severities(sut, "ambed.pid_loss"))
    five = {2, 6, 10, 14, 18}
    assert pair(five, ()) == {"error"}                 # 5 > 2 x 0 + 3
    assert pair({2}, ()) == {"warn"}                   # 1 <= 3
    assert pair(five, {30}) == {"warn"}                # 5 <= 2 x 1 + 3
    assert pair(five, (), ref_stream=False) == {"warn"}     # not comparable


def test_mark_windows(tmp_path):
    """control.log mark begin/end brackets deliberate malformed traffic:
    errors on flows the tester sends inside it become warnings; the sut's
    own errors, and the tester's outside the window, stay errors."""
    (tmp_path / "control").mkdir()
    (tmp_path / "control" / "control.log").write_text(
        "2026-10-30T06:05:35.613Z request mark begin usrp-keyup0\n"
        "2026-10-30T06:05:35.614Z mark begin usrp-keyup0 -> 0\n"
        "2026-10-30T06:05:36.000Z request asl usrp-show\n"
        "2026-10-30T06:05:40.933Z request mark end usrp-keyup0\n"
        "2026-10-30T06:05:40.936Z mark end usrp-keyup0 -> 0\n"
        "2026-10-30T06:05:41.146Z request mark begin open-ended\n")
    hosts = check.rig_config("172.30.0.0/24")["hosts"]
    got = check.marks(str(tmp_path), hosts)
    assert [(ip, round(t1 - t0, 3), label) for ip, t0, t1, label in got][0] == (
        "172.30.0.30", round(41.146 - 35.613, 3), "mark usrp-keyup0")      # tail stops at the next begin
    assert got[1][2] == float("inf") and got[1][3] == "mark open-ended"
    # back to back: the first window's 1 s tail stops at the next begin
    (tmp_path / "control" / "control.log").write_text(
        "2026-10-30T06:05:35.000Z request mark begin a\n2026-10-30T06:05:35.001Z mark begin a -> 0\n"
        "2026-10-30T06:05:40.000Z request mark end a\n2026-10-30T06:05:40.300Z mark end a -> 0\n"
        "2026-10-30T06:05:40.200Z request mark begin b\n2026-10-30T06:05:40.500Z mark begin b -> 0\n"
        "2026-10-30T06:05:45.000Z request mark end b\n2026-10-30T06:05:45.001Z mark end b -> 0\n")
    (_, a0, a1, _), (_, b0, b1, _) = check.marks(str(tmp_path), hosts)
    assert a1 == b0 and round(b1 - b0, 3) == 5.801
    usrp = __import__("dvtest.proto.usrp", fromlist=["usrp"])
    w = Wire(t0=1000.0)
    w.send("172.30.0.30", 32003, "172.30.0.11", 34003, usrp.header(0, 1))            # keyed, 32 B: bad_length
    w.send("172.30.0.11", 34003, "172.30.0.30", 32003, usrp.header(0, 1), 0.02)      # same from the sut
    w.send("172.30.0.30", 32003, "172.30.0.11", 34003, usrp.header(1, 1), 10.0)      # outside the window
    rig = check.rig_config("172.30.0.0/24")
    windows = [("172.30.0.30", 999.0, 1001.0, "mark usrp-keyup0")]
    result = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"), rig, windows=windows)
    bad = [(f["frame"], f["severity"]) for f in result["findings"] if f["code"] == "usrp.bad_length"]
    assert bad == [(1, "warn"), (2, "error"), (3, "error")]
    assert result["findings"][0]["message"].endswith("[during mark usrp-keyup0]")


def test_gap_across_a_mark_window(tmp_path):
    """A sequence gap that shows on the first good frame after a mark window,
    but opened before its end, belongs to the window; a gap outside stays."""
    usrp = __import__("dvtest.proto.usrp", fromlist=["usrp"])
    pcm = b"\x01\x00" * 160
    rig = check.rig_config("172.30.0.0/24")

    def loss(window_start, window_end):
        w = Wire(t0=1000.0)
        for seq in range(10):
            w.send("172.30.0.30", 32003, "172.30.0.11", 34003, usrp.voice(seq, pcm), 0.02)
        w.wait(3.0)                       # frames 10..59 went out on another port in the window
        for seq in range(60, 70):
            w.send("172.30.0.30", 32003, "172.30.0.11", 34003, usrp.voice(seq, pcm), 0.02)
        w.send("172.30.0.30", 32003, "172.30.0.11", 34003, usrp.unkey(70), 0.02)
        windows = [("172.30.0.30", window_start, window_end, "mark usrp-foreign")]
        result = check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"), rig, windows=windows)
        return [f["severity"] for f in result["findings"] if f["code"] == "usrp.seq_loss"]
    # the gap opens after frame 9 (t 1000.2) and shows at frame 10 (t ~1003.2)
    assert loss(1000.5, 1002.0) == ["warn"]          # window inside the gap
    assert loss(1003.0, 1004.0) == ["warn"]          # window still open where it shows
    assert loss(999.0, 1000.1) == ["error"]          # window closed before the gap opened


def test_urfd_is_a_reference_host(tmp_path):
    """urfd.pcapng (DVT_REF_KIND=urfd) is a reference capture: its own
    address is the urfd rig host, its errors are warnings, and it is the
    reference side the sut is compared with."""
    rig = check.rig_config("172.30.0.0/24")
    rig["hosts"].setdefault("urfd", "172.30.0.70")
    usrp = __import__("dvtest.proto.usrp", fromlist=["usrp"])

    def wire(host):
        w = Wire()
        w.send("172.30.0.30", 30001, host, 30001, b"N0TEST  \x00")
        w.send(host, 30001, "172.30.0.30", 30001, b"x" * 13, 0.01)      # unparseable DExtra from the reflector
        w.send("172.30.0.30", 32003, host, 34003, usrp.header(0, 1), 0.01)
        return w
    urfd = check.check_file(wire("172.30.0.70").save(tmp_path / "urfd.pcapng"), rig)
    sut = check.check_file(wire("172.30.0.11").save(tmp_path / "xlxd-sut.pcapng"), rig)
    assert urfd["own"] == "172.30.0.70" and urfd["errors"] > 0
    check.differential([urfd, sut])
    assert urfd["errors"] == 0 and all("reference urfd" in f["message"] for f in urfd["findings"])
    unparseable = [f for f in sut["findings"] if f["code"] == "dextra.unparseable"][0]
    assert unparseable["severity"] == "warn" and "also on reference" in unparseable["message"]


def test_urfd_reference_is_a_compat_run(tmp_path):
    """With ref_kind urfd a sut-only behavioural error (here ambed.no_reply)
    becomes a warning tagged as a compat run, while a malformed packet the
    sut sends (usrp.bad_length) stays an error; with ref_kind xlxd both
    keep today's severity."""
    rig = check.rig_config("172.30.0.0/24")
    usrp = __import__("dvtest.proto.usrp", fromlist=["usrp"])

    def sut():
        w = Wire()
        w.send("172.30.0.11", 10100, "172.30.0.21", 10100, b"AMBEDPINGXLX999  ")
        w.send("172.30.0.11", 34003, "172.30.0.30", 32003, usrp.header(0, 1), 0.01)
        w.send("172.30.0.11", 10100, "172.30.0.21", 10100, b"AMBEDPINGXLX999  ", 3.0)
        w.send("172.30.0.21", 10100, "172.30.0.11", 10100, b"AMBEDPONG", 0.001)
        return check.check_file(w.save(tmp_path / "xlxd-sut.pcapng"), rig)

    def ref(name, host):
        w = Wire()
        w.send("172.30.0.30", 30001, host, 30001, b"N0TEST  \x00")
        return check.check_file(w.save(tmp_path / ("%s.pcapng" % name)), rig)

    def severities(result):
        return {f["code"]: (f["severity"], "compat run" in f["message"]) for f in result["findings"]}
    compat = sut()
    check.differential([ref("urfd", "172.30.0.70"), compat], "urfd")
    assert severities(compat) == {"ambed.no_reply": ("warn", True), "usrp.bad_length": ("error", False)}
    gate = sut()
    check.differential([ref("xlxd-ref", "172.30.0.10"), gate], "xlxd")
    assert severities(gate) == {"ambed.no_reply": ("error", False), "usrp.bad_length": ("error", False)}


def test_run_setting(tmp_path):
    (tmp_path / "run.txt").write_text("markers=proto profile=core sut_profile=xlxd ref_kind=urfd\nhttps://x y z\n")
    assert check.run_setting(str(tmp_path), "ref_kind") == "urfd"
    assert check.run_setting(str(tmp_path), "nope") is None
    assert check.run_setting(str(tmp_path / "missing"), "ref_kind") is None


def ipv6_mdns(payload=b"mdns"):
    """Ethernet + IPv6 + UDP link-local mDNS datagram (fe80::1 -> ff02::fb)."""
    import struct
    udp = struct.pack(">HHHH", 5353, 5353, 8 + len(payload), 0) + payload
    src, dst = bytes.fromhex("fe800000000000000000000000000001"), bytes.fromhex("ff0200000000000000000000000000fb")
    ip = struct.pack(">IHBB", 6 << 28, len(udp), 17, 255) + src + dst + udp
    return b"\x33\x33\x00\x00\x00\xfb\x02\x00\x00\x00\x00\x01\x86\xdd" + ip


def test_idle_segment_is_not_a_wrong_subnet(tmp_path):
    """A numbered segment of a freshly restarted service may hold only IPv6
    link-local or loopback noise: idle, no error. IPv4 traffic with none of
    it in the rig subnet is still the wrong-subnet error."""
    rig = check.rig_config("172.30.0.0/24")
    v6 = tmp_path / "transcoder-sut.3.pcapng"
    pcap.write(v6, [(1000.0 + i, ipv6_mdns()) for i in range(15)])
    result = check.check_file(str(v6), rig)
    assert result["idle"] and result["errors"] == 0 and result["read"]["udp"] == 15
    assert "idle (no routable IPv4 traffic" in check.summary_lines(result)[0]
    lo = Wire()
    lo.send("127.0.0.1", 40000, "127.0.0.1", 40001, b"local")
    result = check.check_file(lo.save(tmp_path / "transcoder-sut.4.pcapng"), rig)
    assert result["idle"] and result["errors"] == 0
    foreign = Wire()
    foreign.send("192.0.2.5", 10100, "192.0.2.6", 10100, b"AMBEDPINGXLX999  ")
    with pytest.raises(pcap.CaptureError):
        check.check_file(foreign.save(tmp_path / "transcoder-sut.5.pcapng"), rig)
    busy = Wire()
    busy.send("172.30.0.11", 10100, "172.30.0.21", 10100, b"AMBEDPINGXLX999  ")
    busy.rows.append((busy.t + 0.1, ipv6_mdns()))
    assert "idle" not in check.check_file(busy.save(tmp_path / "transcoder-sut.pcapng"), rig)


def test_cut_stream_mark_covers_peer_flows(tmp_path):
    """xlx-cut-stream also opens windows on the xlxd peers; they excuse
    stream-end errors only."""
    (tmp_path / "control").mkdir()
    (tmp_path / "control" / "control.log").write_text(
        "2026-10-30T06:05:35.000Z request mark begin xlx-cut-stream\n"
        "2026-10-30T06:05:35.001Z mark begin xlx-cut-stream -> 0\n"
        "2026-10-30T06:05:40.000Z request mark end xlx-cut-stream\n"
        "2026-10-30T06:05:40.001Z mark end xlx-cut-stream -> 0\n")
    hosts = {"tester": "172.30.0.30", "xlxd_peer_sut": "172.30.0.12", "xlxd_peer_ref": "172.30.0.13"}
    got = check.marks(str(tmp_path), hosts)
    assert sorted(w[0] for w in got) == ["172.30.0.12", "172.30.0.13", "172.30.0.30"]
    assert sum(1 for w in got if w[3] == "mark xlx-cut-stream (peer)") == 2
    report = check.Report(None)
    flow = "172.30.0.12:30001>172.30.0.11:30001"
    report.add("xlx", flow, 5, check.ERROR, "no_last", "stream ended without a last frame")
    report.add("xlx", flow, 6, check.ERROR, "bad_other", "something else")
    ts = got[0][1] + 1.0
    check._excuse(report, {5: ts, 6: ts}, got)
    sev = {f.code: f.severity for f in report.findings}
    assert sev["xlx.no_last"] == check.WARN and sev["xlx.bad_other"] == check.ERROR
