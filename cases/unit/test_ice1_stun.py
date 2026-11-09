# SPDX-License-Identifier: MIT
"""dvtest.tools.ice1_stun: STUN parsing, the cell analysis on a synthetic capture, the log window and the report of a run directory."""

import datetime
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dvtest import pcap  # noqa: E402
from dvtest.tools import ice1_stun as tool  # noqa: E402

pytestmark = pytest.mark.unit

SUT = "172.30.0.11"
OLD, NEW = ("172.30.0.93", 35884), ("172.30.0.92", 35884)
PRFLX, SRFLX = 1853817087, 1686052863
BASE = 1794172000.0


def attr(kind, value):
    return struct.pack(">HH", kind, len(value)) + value + b"\x00" * (-len(value) % 4)


def stun(klass, txid, priority=None, use_candidate=False, username="dv4+abc:def", method=1):
    mtype = (method & 0x000F) | ((method & 0x0070) << 1) | ((method & 0x0F80) << 2) | ((klass & 1) << 4) | ((klass & 2) << 7)
    body = b""
    if username is not None:
        body += attr(tool.ATTR_USERNAME, username.encode())
    if priority is not None:
        body += attr(tool.ATTR_PRIORITY, struct.pack(">I", priority))
    if use_candidate:
        body += attr(tool.ATTR_USE_CANDIDATE, b"")
    return struct.pack(">HHI", mtype, len(body), tool.COOKIE) + txid + body


def txid(n):
    return bytes([n]) * 12


def exchange(packets, ts, src, priority, n, use_candidate=False, answer=True, username="dv4+abc:def"):
    """A request from src to the SUT (and its success response) as capture frames."""
    packets.append((ts, pcap.ethernet_udp(src[0], src[1], SUT, 50000, stun(0, txid(n), priority, use_candidate, username))))
    if answer:
        packets.append((ts + 0.001, pcap.ethernet_udp(SUT, 50000, src[0], src[1], stun(2, txid(n)))))


def firefox_capture(consent_after):
    """Old mapping: a plain check, the nominating check, two consent checks; after the change (t0 = BASE + 10): the first check
    4.5 s later, three more each 5 s."""
    pk = []
    exchange(pk, BASE + 1.0, OLD, PRFLX, 1)
    exchange(pk, BASE + 1.02, OLD, PRFLX, 2, use_candidate=True)
    exchange(pk, BASE + 5.0, OLD, SRFLX, 3)
    exchange(pk, BASE + 9.5, OLD, SRFLX, 4)
    for i in range(4):
        exchange(pk, BASE + 14.5 + 5 * i, NEW, consent_after, 10 + i)
    pk.append((BASE + 12.0, pcap.ethernet_udp(NEW[0], NEW[1], SUT, 50000, b"\x80\x00" + b"\x00" * 30)))   # RTCP, ignored
    return pk


def test_stun_message_fields_and_rejections():
    msg = tool.stun_message(stun(0, txid(7), PRFLX, True, "dv4+x:y"))
    assert (msg["kind"], msg["priority"], msg["use_candidate"], msg["username"], msg["txid"]) == ("request", PRFLX, True, "dv4+x:y", txid(7))
    plain = tool.stun_message(stun(0, txid(8), SRFLX, username=None))
    assert plain["use_candidate"] is False and plain["username"] is None and plain["priority"] == SRFLX
    assert tool.stun_message(stun(2, txid(9)))["kind"] == "response"
    assert tool.stun_message(stun(3, txid(9)))["kind"] == "error"
    assert tool.stun_message(stun(1, txid(9))) is None                      # indication
    assert tool.stun_message(stun(0, txid(9), method=3)) is None            # not Binding
    assert tool.stun_message(b"\x80\x00" + b"\x00" * 30) is None            # RTP-shaped
    assert tool.stun_message(b"\x00\x01") is None


def test_priority_labels():
    assert tool.type_of(PRFLX) == "prflx" and tool.type_of(SRFLX) == "srflx"
    assert tool.type_of(2113929471) == "host" and tool.type_of(92217343) == "tp5"
    assert tool.label(None) == "-" and tool.label(PRFLX) == "1853817087(prflx)"


def test_analysis_with_a_priority_mismatch(tmp_path):
    path = tmp_path / "c.pcapng"
    pcap.write(str(path), firefox_capture(SRFLX))
    requests, responses = tool.read_checks(str(path), {OLD, NEW})
    got = tool.analyse_checks(requests, responses, OLD, NEW, first_stun_new_s=4.5)
    assert got["nominate_priorities"] == [PRFLX]
    assert got["consent_before"] == [SRFLX, SRFLX]            # the plain check before the nomination is not a consent check
    assert got["consent_after"] == [SRFLX] * 4
    assert got["priority_match"] is False
    assert (got["after_requests"], got["after_answered"], got["after_use_candidate"]) == (4, 4, 0)
    assert got["t0"] == pytest.approx(BASE + 10.0, abs=1e-3) and got["first_after_s"] == 4.5
    assert got["problems"] == []


def test_analysis_with_matching_priority_and_unanswered_checks(tmp_path):
    pk = firefox_capture(PRFLX)
    pk = [p for p in pk if p[0] != BASE + 14.5 + 0.001]       # drop the first response
    path = tmp_path / "c.pcapng"
    pcap.write(str(path), pk, fmt="pcap")
    requests, responses = tool.read_checks(str(path), {OLD, NEW})
    got = tool.analyse_checks(requests, responses, OLD, NEW, first_stun_new_s=None)
    assert got["priority_match"] is True and got["after_answered"] == 3
    assert got["t0_source"].startswith("first request after (results have no")
    assert got["t0"] == pytest.approx(BASE + 14.5, abs=1e-3)


def test_analysis_nominating_checks_after_the_change_are_not_consent(tmp_path):
    pk = []
    exchange(pk, BASE + 1.0, OLD, 1845501695, 1, use_candidate=True)
    exchange(pk, BASE + 3.0, NEW, 1845501695, 2, use_candidate=True)
    path = tmp_path / "c.pcapng"
    pcap.write(str(path), pk)
    requests, responses = tool.read_checks(str(path), {OLD, NEW})
    got = tool.analyse_checks(requests, responses, OLD, NEW, first_stun_new_s=1.5)
    assert got["consent_after"] == [] and got["priority_match"] is None and got["after_use_candidate"] == 1


def test_analysis_other_session_on_the_new_mapping_is_ignored(tmp_path):
    pk = firefox_capture(SRFLX)
    exchange(pk, BASE + 11.0, NEW, 5, 50, username="dv9+other:one")
    path = tmp_path / "c.pcapng"
    pcap.write(str(path), pk)
    requests, responses = tool.read_checks(str(path), {OLD, NEW})
    got = tool.analyse_checks(requests, responses, OLD, NEW, first_stun_new_s=4.5)
    assert got["after_requests"] == 4 and 5 not in got["consent_after"]


def test_analysis_reports_what_is_missing(tmp_path):
    path = tmp_path / "c.pcapng"
    pcap.write(str(path), [])
    assert tool.analyse_checks([], {}, OLD, NEW)["problems"]
    pk = []
    exchange(pk, BASE + 1.0, OLD, PRFLX, 1)
    pcap.write(str(path), pk)
    requests, responses = tool.read_checks(str(path), {OLD, NEW})
    got = tool.analyse_checks(requests, responses, OLD, NEW)
    assert any("nominating" in p for p in got["problems"]) and any("new mapping" in p for p in got["problems"])


def log_line(epoch, text):
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
    return "xlxd-sut-1  | %s %s\n" % (stamp, text)


def test_log_events_and_window(tmp_path):
    path = tmp_path / "sut.log"
    path.write_text(
        log_line(BASE + 5, "dv-edge-webrtc: sessions 1 counters []")
        + log_line(BASE + 14.5, "webrtc: rebinding reason=consent old=ipv4 new=ipv4")
        + log_line(BASE + 31.1, "webrtc: peer failed level=info reason=control_closed")
        + log_line(BASE + 31.1, "webrtc: peer removed level=info reason=control_closed")
        + "no stamp webrtc: rebinding reason=nomination\n")
    events = tool.webrtc_log_events(str(path))
    assert [(e[1], e[2]) for e in events] == [("rebinding", "consent"), ("peer failed", "control_closed"), ("peer removed", "control_closed")]
    assert events[0][0] == pytest.approx(BASE + 14.5, abs=1e-6)
    assert tool.events_in_window(events, BASE + 10, 20.0) == [(4.5, "rebinding", "consent")]
    assert len(tool.events_in_window(events, BASE + 10, 22.0)) == 3


def result_rows(consent_cell_ok):
    """Two cells' evidence rows as cases/test_web_nat_ice1.py writes them, reduced to the keys the tool reads."""
    def cell(engine, nat, local, first):
        return [
            {"engine": engine, "nat": nat, "key": "address change", "value": {"from": OLD[0], "to": NEW[0], "tcp": "unchanged"}},
            {"engine": engine, "nat": nat, "key": "router view: the SUT sees the browser at (before / after)",
             "value": [[{"browser_port": 1, "seen_ip": OLD[0], "seen_port": OLD[1]}], [{"browser_port": 1, "seen_ip": NEW[0], "seen_port": NEW[1]}]]},
            {"engine": engine, "nat": nat, "key": "selected pair (before / after)", "value": [{"local": local}, {"local": local}]},
            {"engine": engine, "nat": nat, "key": "firsts: detail (kinds of the firsts, STUN response / RTP to the new mapping, stale totals)",
             "value": {"first_stun_new_s": first}},
            {"engine": engine, "nat": nat, "key": "stale_to_old_per_s", "value": [50] * 20},
            {"engine": engine, "nat": nat, "key": "audio after the change", "value": {"analysis": {"resumed": consent_cell_ok, "resume_s": 4.0}}},
        ]
    rows = [{"engine": None, "nat": "full_cone", "key": "readdress: back", "value": {}}]       # a router self-test row
    rows += cell("Playwright Firefox", "full_cone", "srflx", 4.5)
    return rows


def make_run(tmp_path, consent_after, with_rebinding):
    run = tmp_path / "run"
    (run / "web").mkdir(parents=True)
    (run / "capture").mkdir()
    (run / "logs").mkdir()
    (run / "web" / "ice1-results.jsonl").write_text("\n".join(json.dumps(r) for r in result_rows(with_rebinding)) + "\n")
    pcap.write(str(run / "capture" / "xlxd-sut.pcapng"), firefox_capture(consent_after))
    text = log_line(BASE + 2, "dv-edge-webrtc: sessions 1 counters []")
    if with_rebinding:
        text += log_line(BASE + 14.5, "webrtc: rebinding reason=consent old=ipv4 new=ipv4")
    text += log_line(BASE + 31.1, "webrtc: peer failed level=info reason=control_closed")
    (run / "logs" / "xlxd-sut.log").write_text(text)
    return run


def test_report_of_a_run_blocked_by_priority(tmp_path, capsys):
    run = make_run(tmp_path, SRFLX, with_rebinding=False)
    assert tool.main([str(run)]) == 0
    report = json.loads((run / "web" / "ice1-stun.json").read_text())
    assert len(report["cells"]) == 1                                  # the router self-test row is not a cell
    cell = report["cells"][0]
    assert (cell["engine"], cell["nat"], cell["local_before"], cell["resumed"]) == ("Playwright Firefox", "full_cone", "srflx", False)
    assert cell["nominate_priorities"] == [PRFLX] and cell["consent_after"] == [SRFLX] * 4
    assert cell["priority_match"] is False and cell["rebinding"] == "none" and cell["blocked_by_priority"] is True
    out = capsys.readouterr().out
    assert "1686052863(srflx)x4" in out and "NO" in out and "rule (b) blocked" in out


def test_report_of_a_run_that_rebinds(tmp_path, capsys):
    run = make_run(tmp_path, PRFLX, with_rebinding=True)
    assert tool.main([str(run)]) == 0
    cell = json.loads((run / "web" / "ice1-stun.json").read_text())["cells"][0]
    assert cell["priority_match"] is True and cell["rebinding"] == "consent" and cell["rebinding_at_s"] == 4.5
    assert cell["blocked_by_priority"] is False
    assert [e[1] for e in cell["log"]] == ["rebinding"]               # the teardown peer line at +21 s is outside the 20 s window
    assert "consent@4.50s" in capsys.readouterr().out


def test_missing_capture_is_reported(tmp_path, capsys):
    run = make_run(tmp_path, PRFLX, with_rebinding=True)
    (run / "capture" / "xlxd-sut.pcapng").unlink()
    assert tool.main([str(run)]) == 2
    assert "--capture" in capsys.readouterr().err
