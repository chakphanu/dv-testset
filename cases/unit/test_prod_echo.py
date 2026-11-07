# SPDX-License-Identifier: MIT
"""dvtest/tools/prod_echo.py: the production echo check, against small echo reflectors on 127.0.0.1 (nothing
else is ever contacted), the pure verdict, and the safety rails."""
import io
import json
import socket
import threading
import time

import pytest

from dvtest import echo
from dvtest.proto import dcs, dextra, dmr, dplus, m17, ysf
from dvtest.tools import prod_echo as pe

SECRET = "s3cret-pass-7431"
BASE = ["--host", "127.0.0.1", "--callsign", "N0CALL"]


# ---------------------------------------------------------------- a loopback echo reflector for every protocol

class FakeEcho(threading.Thread):
    """Answers the link of one protocol, notes the unlink, records the over and replays it verbatim `delay`
    seconds after its end marker (mode "echo"), never ("silent"), twice ("double") or does not answer the
    link at all ("mute"). Streams shorter than 8 packets (the join of DPlus, YSF and DMR) are not replayed."""

    def __init__(self, proto, delay=0.4, mode="echo", password=SECRET):
        super().__init__(daemon=True)
        self.proto, self.delay, self.mode, self.password = proto, delay, mode, password
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.01)
        self.port = self.sock.getsockname()[1]
        self.halt = threading.Event()
        self.log = []                   # (t, data) of every datagram received
        self.cur, self.ready = [], None
        self.linked = self.unlinked = False
        self.auth_ok = None
        self.replays = 0
        self.salt = b"\x0a\x0b\x0c\x0d"
        self.start()

    # -- per protocol: reply to a link, classify the rest
    def reply(self, data, addr):
        self.sock.sendto(data, addr)

    def stream_packet(self, data):
        p = self.proto
        if p == "dextra":
            return len(data) in (56, 27) and data.startswith(b"DSVT")
        if p == "dplus":
            return (len(data), data[:2]) in ((58, b"\x3a\x80"), (29, b"\x1d\x80"), (32, b"\x20\x80"))
        if p == "dcs":
            return len(data) == 100 and data.startswith(b"0001")
        if p == "ysf":
            return len(data) == 155 and data.startswith(b"YSFD")
        if p == "m17":
            return len(data) == 54 and data.startswith(b"M17 ")
        return len(data) == 55 and data.startswith(b"DMRD")

    def is_end(self, data):
        p = self.proto
        if p == "dextra":
            return len(data) == 27 and bool(data[14] & 0x40)
        if p == "dplus":
            return len(data) == 32
        if p == "dcs":
            return bool(data[45] & 0x40)
        if p == "ysf":
            info = ysf.parse(data)
            return bool(info and info["eot"])
        if p == "m17":
            return m17.parse(data)["eot"]
        return data[15] == 0xA2

    def handle(self, data, addr):
        p = self.proto
        if self.mode == "mute":
            return False
        if p == "dextra" and len(data) == 11 and data[10] == 0:
            if data[9:10] == b" ":
                self.unlinked = True
            else:
                self.linked = True
                self.reply(data[:10] + b"ACK\x00", addr)
            return True
        if p == "dplus":
            if data == dplus.CONNECT:
                self.reply(dplus.CONNECT, addr)
                return True
            if data[:4] == b"\x1c\xc0\x04\x00":
                self.linked = True
                self.reply(dplus.LOGIN_OK, addr)
                return True
            if data == dplus.DISCONNECT:
                self.unlinked = True
                return True
        if p == "dcs" and len(data) == 519:
            self.linked = True
            self.reply(data[:10] + b"ACK\x00", addr)
            return True
        if p == "dcs" and len(data) == 11:
            self.unlinked = True
            return True
        if p == "ysf" and data[:4] == b"YSFP" and len(data) == 14:
            self.linked = True
            self.reply(ysf.ACK, addr)
            return True
        if p == "ysf" and data[:4] == b"YSFU":
            self.unlinked = True
            return True
        if p == "m17" and data[:4] == b"CONN":
            self.linked = True
            self.reply(b"ACKN", addr)
            return True
        if p == "m17" and data[:4] == b"DISC":
            self.unlinked = True
            return True
        if p == "dmrhb":
            if data[:4] == b"RPTL" and len(data) == 8:
                self.reply(b"RPTACK" + self.salt, addr)
                return True
            if data[:4] == b"RPTK":
                self.auth_ok = data[8:] == dmr.auth_hash(self.salt, self.password)
                self.reply((b"RPTACK" if self.auth_ok else b"MSTNAK") + data[4:8], addr)
                return True
            if data[:4] == b"RPTC" and len(data) == 302:
                self.reply(b"RPTACK" + data[4:8], addr)
                self.linked = self.auth_ok is True
                return True
            if data[:7] == b"RPTPING":
                self.reply(b"MSTPONG" + data[7:11], addr)
                return True
            if data[:5] == b"RPTCL":
                self.unlinked = True
                return True
            if self.stream_packet(data) and int.from_bytes(data[8:11], "big") == 4000:
                self.unlinked = True
                return True
        return False

    def run(self):
        peer = None
        while not self.halt.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                data = None
            except OSError:
                return
            now = time.monotonic()
            if data is not None:
                self.log.append((now, data))
                peer = addr
                if not self.handle(data, addr) and self.stream_packet(data):
                    self.cur.append((now, data))
                    if self.is_end(data):
                        if len(self.cur) >= 8:
                            self.ready = now
                        else:
                            self.cur = []
            if self.ready is not None and now - self.ready >= self.delay and peer is not None:
                cur, self.cur, self.ready = self.cur, [], None
                if self.mode != "silent":
                    self.replay(cur, peer)
                    if self.mode == "double":
                        time.sleep(0.8)
                        self.replay(cur, peer)

    def replay(self, cur, addr):
        self.replays += 1
        t0, prev = time.monotonic(), cur[0][0]
        at = 0.0
        for t, data in cur:
            at += min(t - prev, 0.1)
            prev = t
            wait = t0 + at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.sock.sendto(data, addr)

    def over_log(self):
        """The datagrams that are stream packets of the over (the join and unlink streams are short)."""
        out = [d for _, d in self.log if self.stream_packet(d)]
        return out

    def close(self):
        self.halt.set()
        self.join(2.0)
        self.sock.close()


def fast(opts, fake=None):
    """Timing of a loopback run: the fake replays 0.4 s after the over."""
    opts.settle, opts.link_settle, opts.unlink_wait, opts.quiet_after = 0.2, 0.1, 0.2, 0.6
    opts.window = (0.15, 1.6)
    opts.seconds = 1.2
    return opts


def options(proto, port, extra=(), tmp=None):
    argv = BASE + ["--protocol", proto, "--port", "%s=%d" % (proto, port), "--i-have-owner-approval"] + list(extra)
    if tmp is not None:
        argv += ["--out", str(tmp)]
    return fast(pe.build_options(argv))


@pytest.fixture
def dmr_pass(tmp_path):
    path = tmp_path / "dmr.pw"
    path.write_text(SECRET + "\n")
    return str(path)


def dmr_extra(path):
    return ["--dmr-id", "1023007", "--dmr-pass-file", path]


# ---------------------------------------------------------------- whole flow per protocol

@pytest.mark.parametrize("name", ["dextra", "dplus", "dcs", "ysf", "m17", "dmrhb"])
def test_flow_link_over_replay_unlink(name, tmp_path, dmr_pass):
    fake = FakeEcho(name)
    try:
        opts = options(name, fake.port, dmr_extra(dmr_pass) if name == "dmrhb" else (), tmp_path)
        opts.stream_ids[name] = 4321
        proto = pe.PROTOCOLS[name]
        res = pe.run_protocol(proto, opts, "127.0.0.1", time.monotonic() + 40)
        assert res["pass"], res
        assert res["reason"] == "ok"
        assert res["protocol"] == name and res["host"] == "127.0.0.1" and res["port"] == fake.port
        assert res["module"] == "E" and res["callsign_used"] == "N0CALL"
        assert res["sent_frames"] == 60 and res["received_frames"] >= 60
        assert res["replays"] == 1 and fake.replays == 1
        assert res["byte_identical"] is True
        assert 0.15 <= res["replay_delay_s"] <= 1.6
        assert 0.7 <= res["duration_s"] <= 1.8
        assert res["rms"] is None and res["correlation"] is None and "no codec decoder" in res["metric_note"]
        assert res["linked"] and res["unlinked"] and fake.linked
        time.sleep(0.3)
        assert fake.unlinked, "the reflector never saw the unlink"
        json.dumps(res)
        # what was planned (dry run) is what the real client put on the wire, packet for packet
        plan = proto.plan("N0CALL", "E", proto.frames(opts.frames, "prod-echo-" + name), 4321, opts)["over"]
        seen = fake.over_log()
        at = next((k for k in range(len(seen) - len(plan) + 1) if seen[k:k + len(plan)] == plan), None)
        assert at is not None, "%s: the planned over is not on the wire" % name
    finally:
        fake.close()


def test_silent_reflector_is_a_failure_and_still_unlinks(tmp_path):
    fake = FakeEcho("dextra", mode="silent")
    try:
        res = pe.run_protocol(pe.PROTOCOLS["dextra"], options("dextra", fake.port, tmp=tmp_path), "127.0.0.1", time.monotonic() + 40)
        assert not res["pass"] and "no replay" in res["reason"]
        assert res["replays"] == 0 and res["received_frames"] == 0 and res["unlinked"]
        time.sleep(0.3)
        assert fake.unlinked
    finally:
        fake.close()


def test_two_replays_fail(tmp_path):
    fake = FakeEcho("dextra", mode="double")
    try:
        opts = options("dextra", fake.port, tmp=tmp_path)
        opts.quiet_after = 1.2          # the second replay starts 0.8 s after the first one ended
        res = pe.run_protocol(pe.PROTOCOLS["dextra"], opts, "127.0.0.1", time.monotonic() + 40)
        assert not res["pass"] and "2 replays" in res["reason"]
    finally:
        fake.close()


def test_link_refused_fails_and_sends_the_unlink(tmp_path, monkeypatch):
    fake = FakeEcho("dextra", mode="mute")
    try:
        monkeypatch.setattr(pe.PROTOCOLS["dextra"], "link_timeout", 0.5)
        res = pe.run_protocol(pe.PROTOCOLS["dextra"], options("dextra", fake.port, tmp=tmp_path), "127.0.0.1", time.monotonic() + 40)
        assert not res["pass"] and res["reason"].startswith("link failed")
        assert res["linked"] is False and res["sent_frames"] == 60 and res["received_frames"] == 0
        assert not any(len(d) == 56 for _, d in fake.log), "an over was sent without a link"
    finally:
        fake.close()


def test_dmr_wrong_password_does_not_link(tmp_path, dmr_pass, monkeypatch):
    fake = FakeEcho("dmrhb", password="another-password")
    try:
        monkeypatch.setattr(pe.PROTOCOLS["dmrhb"], "link_timeout", 0.5)
        res = pe.run_protocol(pe.PROTOCOLS["dmrhb"], options("dmrhb", fake.port, dmr_extra(dmr_pass), tmp_path), "127.0.0.1", time.monotonic() + 40)
        assert not res["pass"] and res["linked"] is False and fake.auth_ok is False
        assert not any(d[:4] == b"DMRD" and d[8:11] != (4000).to_bytes(3, "big") for _, d in fake.log)
    finally:
        fake.close()


def test_execute_writes_files_and_never_prints_the_password(tmp_path, dmr_pass):
    a, b = FakeEcho("dextra"), FakeEcho("dmrhb")
    try:
        argv = BASE + ["--protocol", "dextra,dmrhb", "--port", "dextra=%d" % a.port, "--port", "dmrhb=%d" % b.port,
                       "--i-have-owner-approval", "--out", str(tmp_path / "run")] + dmr_extra(dmr_pass)
        opts = fast(pe.build_options(argv))
        out, err = io.StringIO(), io.StringIO()
        results, code = pe.execute(opts, out=out, err=err)
        assert code == 0 and [r["protocol"] for r in results] == ["dextra", "dmrhb"]
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        assert [x["protocol"] for x in lines] == ["dextra", "dmrhb"] and all(x["pass"] for x in lines)
        keys = ["protocol", "host", "port", "module", "callsign_used", "sent_frames", "received_frames", "duration_s",
                "replay_delay_s", "byte_identical", "rms", "correlation", "pass", "reason"]
        for x in lines:
            assert all(k in x for k in keys)
        run = tmp_path / "run"
        assert sorted(p.name for p in run.iterdir()) == ["dextra.json", "dmrhb.json", "results.json", "summary.txt"]
        doc = json.loads((run / "results.json").read_text())
        assert doc["results"][0]["pass"] and "g3" in doc["not_supported"] and "imrs" in doc["not_supported"]
        everything = out.getvalue() + err.getvalue() + "".join(p.read_text() for p in run.iterdir())
        assert SECRET not in everything
        assert "PASS" in (run / "summary.txt").read_text()
    finally:
        a.close()
        b.close()


def test_global_timeout_skips_the_rest_without_a_socket(tmp_path, monkeypatch):
    opts = fast(pe.build_options(BASE + ["--protocol", "dextra,m17", "--i-have-owner-approval", "--out", str(tmp_path)]))
    opts.timeout = 0.0
    out, err = io.StringIO(), io.StringIO()
    made = []
    real = socket.socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: made.append(a) or real(*a, **k))
    results, code = pe.execute(opts, out=out, err=err)
    assert code == 1 and [r["pass"] for r in results] == [False, False]
    assert all("global timeout" in r["reason"] for r in results)
    assert made == []


# ---------------------------------------------------------------- the verdict, pure

def replay_of(proto, frames, start, sid=7, drop=0, ident="N0CALL"):
    """(packets, times) of a replay that begins at `start`, built from the planned over of a protocol."""
    opts = pe.build_options(BASE + ["--protocol", proto.name, "--dry-run"])
    packets = proto.plan(ident, "E", frames, sid, opts)["over"]
    if drop:
        packets = packets[:-1 - drop] + packets[-1:]
    gap = {"m17": 0.04, "ysf": 0.1, "dmrhb": 0.06}.get(proto.name, 0.02)      # the packet spacing of each protocol
    return packets, [start + i * gap for i in range(len(packets))]


def judged(name, start, **kw):
    proto = pe.PROTOCOLS[name]
    frames = proto.frames(150, "prod-echo-" + name)
    end = 100.0
    other = kw.pop("other", None)                    # the replay carries other frames than the ones sent
    packets, times = replay_of(proto, proto.frames(150, other) if other else frames, end + start, **kw)
    return pe.judge(proto, frames, end, packets, times)


@pytest.mark.parametrize("name", ["dextra", "dplus", "dcs", "m17", "ysf"])
def test_judge_accepts_a_good_replay(name):
    res, fails = judged(name, 1.0)
    assert fails == [], fails
    assert res["replay_delay_s"] == pytest.approx(1.0, abs=0.01)
    assert res["byte_identical"] is True and res["replays"] == 1 and res["sent_frames"] == 150
    assert res["received_frames"] >= 150


def test_judge_timing_window():
    for start in (0.3, 3.0):
        _, fails = judged("dextra", start)
        assert any("replay starts" in f for f in fails)
    assert judged("dextra", 0.85)[1] == [] and judged("dextra", 2.45)[1] == []


def test_judge_truncated_replay():
    res, fails = judged("dextra", 1.0, drop=50)
    assert any("frames heard" in f for f in fails) and any("lasts" in f for f in fails)
    assert res["byte_identical"] is False


def test_judge_altered_payload_fails_except_on_ysf():
    _, fails = judged("dextra", 1.0, other="not-the-sent-frames")
    assert any("payload differs" in f for f in fails)
    _, fails = judged("ysf", 1.0, other="not-the-sent-frames")
    assert not any("payload differs" in f for f in fails)


def test_judge_nothing_heard_and_not_linked():
    proto = pe.PROTOCOLS["dextra"]
    frames = proto.frames(150, "x")
    res, fails = pe.judge(proto, frames, 10.0, [], [])
    assert res["replays"] == 0 and fails and "no replay" in fails[0]
    res, fails = pe.judge(proto, frames, 10.0, [], [], linked=False)
    assert fails == ["link failed"]


def test_judge_ignores_the_live_copy_before_the_end_of_the_over():
    proto = pe.PROTOCOLS["dextra"]
    frames = proto.frames(150, "prod-echo-dextra")
    live, live_t = replay_of(proto, frames, 96.9)         # someone else's traffic ending before our over ended
    echo_p, echo_t = replay_of(proto, frames, 101.0)
    res, fails = pe.judge(proto, frames, 100.0, live + echo_p, live_t + echo_t)
    assert fails == [] and res["replays"] == 1


# ---------------------------------------------------------------- safety rails

def usage(argv):
    with pytest.raises(pe.UsageError) as exc:
        pe.build_options(argv)
    return str(exc.value)


def test_real_run_needs_the_approval_flag(monkeypatch, capsys):
    msg = usage(BASE)
    assert "--i-have-owner-approval" in msg
    made = []
    monkeypatch.setattr(socket, "socket", lambda *a, **k: made.append(a))
    assert pe.main(BASE) == 2
    assert "--i-have-owner-approval" in capsys.readouterr().err
    assert made == []
    assert pe.build_options(BASE + ["--dry-run"]).dry_run
    assert not pe.build_options(BASE + ["--i-have-owner-approval"]).dry_run


def test_module_guard():
    ok = BASE + ["--i-have-owner-approval"]
    assert pe.build_options(ok).module == "E"
    assert "module F refused" in usage(ok + ["--module", "F"])
    assert "module F refused" in usage(BASE + ["--dry-run", "--module", "F"])
    assert pe.build_options(ok + ["--module", "F", "--allow-other-module"]).module == "F"
    assert pe.build_options(ok + ["--allow-other-module"]).module == "E"       # the flag alone changes nothing
    assert "one capital letter" in usage(ok + ["--module", "e"])
    assert "one capital letter" in usage(ok + ["--module", "EE", "--allow-other-module"])


def test_seconds_cap_and_floor():
    ok = BASE + ["--i-have-owner-approval"]
    assert pe.build_options(ok).seconds == 3.0 and pe.build_options(ok).frames == 150
    assert pe.build_options(ok + ["--seconds", "5.0"]).frames == 250
    assert "hard cap" in usage(ok + ["--seconds", "5.1"])
    assert "hard cap" in usage(ok + ["--seconds", "nan"])
    assert "below" in usage(ok + ["--seconds", "1.0"])


def test_settle_and_timeout_rails():
    ok = BASE + ["--i-have-owner-approval"]
    assert pe.build_options(ok).settle == 6.0
    assert "--settle" in usage(ok + ["--settle", "2"])
    assert "--timeout" in usage(ok + ["--timeout", "0"])
    assert pe.build_options(ok + ["--one-at-a-time"]).protocols == list(pe.DEFAULT_PROTOCOLS)


def test_protocol_selection_and_unsupported():
    ok = BASE + ["--i-have-owner-approval"]
    assert pe.build_options(ok).protocols == ["dextra", "dplus", "dcs", "ysf", "m17"]
    assert pe.build_options(ok + ["--protocol", "ysf", "--protocol", "m17,ysf"]).protocols == ["ysf", "m17"]
    assert "not supported" in usage(ok + ["--protocol", "g3"])
    assert "not supported" in usage(ok + ["--protocol", "imrs"])
    assert "unknown protocol" in usage(ok + ["--protocol", "xyz"])
    assert "dmrhb needs --dmr-id" in usage(ok + ["--protocol", "dmrhb"])
    assert "not a file" in usage(ok + ["--protocol", "dmrhb", "--dmr-id", "1", "--dmr-pass-file", "/nonexistent-file"])


def test_callsign_mapping_and_limits():
    argv = ["--host", "h.example", "--i-have-owner-approval", "--callsign", "N0CALL", "--callsign", "dextra=N0TEST-T",
            "--callsign", "dcs=N0DCS"]
    opts = pe.build_options(argv)
    assert opts.callsigns == {"dextra": "N0TEST-T", "dplus": "N0CALL", "dcs": "N0DCS", "ysf": "N0CALL", "m17": "N0CALL"}
    only = ["--host", "h.example", "--i-have-owner-approval", "--protocol", "ysf", "--callsign", "ysf=N0CALL"]
    assert pe.build_options(only).callsigns == {"ysf": "N0CALL"}
    assert "no callsign for ysf" in usage(["--host", "h.example", "--i-have-owner-approval", "--protocol", "ysf"])
    assert "no callsign for m17" in usage(["--host", "h.example", "--i-have-owner-approval", "--protocol", "m17",
                                           "--callsign", "ysf=N0CALL"])
    assert "unknown protocol" in usage(["--host", "h", "--dry-run", "--callsign", "nope=N0CALL"])
    assert "holds 7" in usage(["--host", "h", "--dry-run", "--protocol", "dcs", "--callsign", "N0TEST-T"])
    assert "outside" in usage(["--host", "h", "--dry-run", "--protocol", "ysf", "--callsign", "N0C;ALL"])
    assert pe.build_options(["--host", "h", "--dry-run", "--protocol", "ysf", "--callsign", "n0call"]).callsigns["ysf"] == "N0CALL"


def test_no_callsign_or_host_is_built_in():
    import inspect
    source = inspect.getsource(pe)
    assert "N0CALL" in source           # only in the docstring example
    for word in ("jane", "in.th", "xlx345"):
        assert word not in source.lower()


def test_dry_run_sends_nothing_and_writes_nothing(monkeypatch, tmp_path, capsys, dmr_pass):
    monkeypatch.chdir(tmp_path)
    touched = []

    def spy(name):
        def inner(*a, **k):
            touched.append(name)
            raise AssertionError("dry run touched the network: " + name)
        return inner

    for name in ("socket", "create_connection", "gethostbyname", "getaddrinfo", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, spy(name))
    argv = ["--host", "prod.example", "--dry-run", "--protocol", "dextra,dplus,dcs,ysf,m17,dmrhb",
            "--callsign", "dextra=N0CALL-T", "--callsign", "N0CALL", "--dmr-id", "1023007", "--dmr-pass-file", dmr_pass]
    assert pe.main(argv) == 0
    out = capsys.readouterr().out
    assert touched == []
    assert not (tmp_path / "out").exists()
    assert "DRY RUN" in out and "nothing is sent" in out
    for name in ("dextra", "dplus", "dcs", "ysf", "m17", "dmrhb"):
        assert "[%s]" % name in out
    assert "not supported: g3" in out and "not supported: imrs" in out
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["dry_run"] is True and len(doc["plan"]) == 6
    first = {p["protocol"]: p for p in doc["plan"]}
    # the first packet of the DExtra over is the DSVT header; the last is the end voice packet
    assert first["dextra"]["over_first"].startswith(b"DSVT".hex() + "10")
    assert first["dextra"]["over_packets"] == 152 and first["dextra"]["frames"] == 150
    assert first["dextra"]["link_packets_before_reply"][0] == dextra.link_packet("N0CALL-T", "E").hex()
    assert first["dextra"]["callsign_used"] == "N0CALL-T" and first["dplus"]["callsign_used"] == "N0CALL"
    assert first["dcs"]["link_packets_before_reply"][0].startswith(dcs.link_packet("N0CALL", "E")[:48].hex())
    assert first["ysf"]["stream_id"] is None and first["m17"]["over_packets"] == 77
    assert SECRET not in out


def test_dry_run_plan_matches_the_builders():
    opts = pe.build_options(BASE + ["--dry-run", "--protocol", "m17"])
    plan = pe.make_plan(opts)[0]
    frames = echo.make_frames("m17", 150, "prod-echo-m17")
    packets = m17.voice_packets(frames, plan["stream_id"], "N0CALL")
    assert plan["over_first"] == pe.hexshort(packets[0]) and plan["over_last"] == pe.hexshort(packets[-1])
    assert pe.hexshort(b"\x01" * 100).endswith("(100 bytes)")


def test_frames_are_the_e1_frames():
    for name, proto in pe.PROTOCOLS.items():
        assert proto.frames(150, "s") == echo.make_frames(proto.kind, 150, "s")
    assert len(set(pe.PROTOCOLS["dextra"].frames(150, "s"))) == 150
