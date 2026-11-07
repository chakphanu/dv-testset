# SPDX-License-Identifier: MIT
"""dvtest/tools/asl_reg_log.py: the log scan of the ASL1h lines (no rig)."""
import json

import pytest

from dvtest.tools import asl_reg_log as al

pytestmark = pytest.mark.unit


def test_scan_counts_classes_and_finds_leaks():
    lines = ["x allstar_client_registration account=acct result=ok", "level=info allstar_call event=setup direction=in kind=node peer=2999",
             "level=info allstar_call event=hangup direction=in kind=node peer=2999", "allstar_client_registration account=unknown result=rejected",
             "noise", "oops password test-secret-not-real in a line"]
    out = al.scan(lines)
    assert out["counts"] == {"allstar_client_registration": 2, "allstar_call event=setup": 1, "allstar_call event=hangup": 1}
    assert out["leaks"] == [(6, "test-secret-not-real")]
    assert al.scan(["clean"])["leaks"] == []


def test_main_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.log"
    good.write_text("allstar_client_registration result=ok\n")
    assert al.main([str(good), "--expect-registrations", "1"]) == 0
    assert al.main([str(good), "--expect-registrations", "2"]) == 1
    bad = tmp_path / "bad.log"
    bad.write_text("allstar_client_registration wrong-secret-not-real\n")
    assert al.main([str(bad)]) == 1
    assert "LEAK" in capsys.readouterr().out


# ---------------------------------------------------------------- registration failure lines (wip-1590f26, ASL1i / ASL1j)
# Sample lines of the first registration-lab run on wip-1590f26 (out/20261107T034616Z/logs/xlxd-sut.log; lab data, fixture nodes only)
# and the registrar-side request times (asl-lab/register/requests.jsonl) of the same run.
P = "xlxd-sut-1  | "
SAMPLE = [
    P + '2026-11-07T03:46:23.065225083Z WARNING: AllStar registrar insecure = true: LAB CLEARTEXT HTTP sends portal credentials without encryption',
    P + '2026-11-07T03:46:23.076398071Z level=info allstar_registration node=2999 summary={"http_status":200,"content_type":"application/json","body_len":100,"json":{"data":"node 2999 successfully registered","ipaddr":"172.30.0.11","port":4569,"refresh":180},"ipaddr":"172.30.0.11","port":4569,"refresh_s":180,"success_path":"$[\\"data\\"]","attributed":true}',
    P + '2026-11-07T03:46:57.366490831Z level=warn event=allstar_registration_failed node=2992 result=mismatch http_status=200 error="registrar address or IAX port differs from configuration" next_retry_delay_ms=120000',
    P + '2026-11-07T03:47:14.506455049Z level=warn allstar_registration node=2993 summary={"http_status":500,"content_type":"text/plain","body_len":15,"json":null,"ipaddr":null,"port":null,"refresh_s":null,"success_path":null,"attributed":false}',
    P + '2026-11-07T03:47:14.506488418Z level=warn event=allstar_registration_failed node=2993 result=http http_status=500 error="registrar returned an unsuccessful HTTP status" next_retry_delay_ms=120000',
    P + '2026-11-07T03:47:31.646929034Z level=warn event=allstar_registration_failed node=2994 result=parse http_status=200 error="registrar reply invalid or node registration unsuccessful" next_retry_delay_ms=123601',
    P + '2026-11-07T03:48:04.086789049Z level=warn event=allstar_registration_failed node=2995 result=timeout http_status=- error="registration attempt timed out" next_retry_delay_ms=123228',
    P + '2026-11-07T03:48:05.936624573Z level=warn event=allstar_registration_failed node=2996 result=parse http_status=200 error="registrar reply invalid or node registration unsuccessful" next_retry_delay_ms=134271',
    P + '2026-11-07T03:50:29.025940274Z level=info allstar_client_registration account=acct result=bad_auth',
]
START = 1794023183.0683131
TIMES = {                       # registrar-side request epochs of that run (two per failing node)
    "2992": [1794023217.358, 1794023337.367], "2993": [1794023234.498, 1794023354.508], "2994": [1794023251.638, 1794023375.258],
    "2995": [1794023268.788, 1794023407.318], "2996": [1794023285.928, 1794023420.208],
}


def test_parse_failures_reads_the_sample_lines():
    rows = al.parse_failures(SAMPLE)
    assert [(r["node"], r["result"], r["http_status"], r["delay_s"]) for r in rows] == [
        ("2992", "mismatch", 200, 120.0), ("2993", "http", 500, 120.0), ("2994", "parse", 200, 123.601),
        ("2995", "timeout", None, 123.228), ("2996", "parse", 200, 134.271)]          # "http_status=-" is None
    assert rows[1]["error"] == "registrar returned an unsuccessful HTTP status" and rows[1]["line"] == 5
    assert abs(rows[1]["t"] - 1794023234.5064) < 0.001 and al.stamp("no time here") is None
    assert al.parse_failures(["event=allstar_registration_failed node=1 result=http"]) == []        # incomplete line: not a failure line


def test_sample_lines_have_no_secret_password_file_or_url():
    assert al.leak_problems(SAMPLE) == []


def test_leak_problems_finds_each_kind_but_whitelists_only_the_lab_warning():
    base = P + "2026-11-07T03:47:14.5Z level=warn event=allstar_registration_failed node=2993 result=http http_status=500 error=\"%s\" next_retry_delay_ms=120000"
    for text, token in (("bad test-secret-not-real", "<secret>"), ("passwd leaked", "passwd"), ("password=x", "password"),
                        ("file /etc/dv-router/asl-secrets/node-2993.pw", ".pw"), ("posting to http://172.30.0.41:8082/", "://")):
        found = al.leak_problems([base % text])
        assert found and any(token in p for p in found), (text, found)
    warning = P + "WARNING: AllStar registrar insecure = true: LAB CLEARTEXT HTTP sends portal credentials without encryption"
    assert al.leak_problems([warning]) == []
    assert al.leak_problems([warning + " server=http://172.30.0.41:8082/"]) == []         # exactly that warning, whatever follows
    assert al.leak_problems([P + "WARNING: registrar url http://172.30.0.41:8082/ (other text)"])      # any other registrar line is checked
    assert al.leak_problems([P + "dv-svc-status: /metrics on http://127.0.0.1:19108"]) == []          # not registration logging


def test_judge_node_accepts_the_sample_and_notes_the_wrong_ones():
    fails = al.parse_failures(SAMPLE)
    assert al.judge_node("2993", TIMES["2993"], fails, "http", START) == []
    assert al.judge_node("2994", TIMES["2994"], fails, "parse", START) == []
    assert al.judge_node("2995", TIMES["2995"], fails, "timeout", START, timeout=True) == []      # 123.228 s + 15.3 s attempt = 138.5 s
    assert al.judge_node("2996", TIMES["2996"], fails, "parse", START) == []
    assert al.judge_node("2992", TIMES["2992"], fails, "mismatch", START, delay_fn=lambda n: 120.0) == []
    assert any("result=http, expected parse" in p for p in al.judge_node("2993", TIMES["2993"], fails, "parse", START))
    assert any("attempt" in p and "15" in p for p in al.judge_node("2995", TIMES["2995"], fails, "timeout", START))   # not a prompt attempt
    late = [dict(f, delay_s=f["delay_s"] - 5) if f["node"] == "2994" else f for f in fails]               # logged delay != measured gap
    assert any("next request" in p for p in al.judge_node("2994", TIMES["2994"], late, "parse", START))
    short = [dict(f, delay_s=30.0) if f["node"] == "2993" else f for f in fails]                           # the old 30 s back-off
    out = al.judge_node("2993", TIMES["2993"], short, "http", START)
    assert any("outside 120..144" in p for p in out)


def test_judge_node_models_the_300_s_limiter_for_the_second_retry():
    # attempts at 0, 125 (suppressed: 125 s after a logged line), 125 + 250 = 375 (logged: 375 s after the first)
    t0 = 1794023000.0
    times = [t0, t0 + 125.0, t0 + 375.0, t0 + 375.0 + 400.0]
    def line(at, delay):
        return {"line": 1, "t": at, "node": "2993", "result": "http", "http_status": 500, "error": "x", "delay_s": delay}
    ok = [line(t0 + 0.005, 125.0 - 0.005), line(t0 + 375.0 + 0.005, 400.0 - 0.005)]
    assert al.judge_node("2993", times, ok, "http") == []
    extra = ok + [line(t0 + 125.005, 250.0 - 0.005)]                                  # a line the limiter would not admit
    assert any("inside the 300 s limit window" in p for p in al.judge_node("2993", times, extra, "http"))
    assert any("no allstar_registration_failed line" in p for p in al.judge_node("2993", times, ok[:1], "http"))


def test_judge_run_rate_limited_nodes_have_exact_delays():
    t0 = 1794023000.0
    f2997 = {"line": 1, "t": t0 + 0.004, "node": "2997", "result": "rate_limited", "http_status": 429, "error": "x", "delay_s": 300.0}
    f2998 = {"line": 2, "t": t0 + 0.004, "node": "2998", "result": "rate_limited", "http_status": 503, "error": "x", "delay_s": 120.0}
    assert al.judge_node("2997", [t0, t0 + 300.004], [f2997], "rate_limited", delay_fn=lambda n: 300.0) == []
    assert al.judge_node("2998", [t0, t0 + 120.004], [f2998], "rate_limited", delay_fn=lambda n: 120.0 * 2 ** (n - 1)) == []
    wrong = dict(f2997, delay_s=240.0)
    assert al.judge_node("2997", [t0, t0 + 240.004], [wrong], "rate_limited", delay_fn=lambda n: 300.0)
    problems = al.judge_run([], {})                                                  # nothing recorded: every node is reported
    assert len(problems) == 7 and all("nothing to judge" in p for p in problems)


def test_main_register_dir(tmp_path, capsys):
    reg = tmp_path / "register"
    reg.mkdir()
    rows = []
    for node, times in TIMES.items():
        rows += [{"t": t, "body": {"data": {"nodes": {node: {"node": node}}}}} for t in times]
    (reg / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (reg / "schedule.json").write_text(json.dumps({"process_start": START, "interval_s": 120.0}))
    log = tmp_path / "sut.log"
    log.write_text("\n".join(SAMPLE) + "\n")
    assert al.main([str(log), "--register", str(reg)]) == 1          # nodes 2997 and 2998 are not in the old run
    out = capsys.readouterr().out
    assert "node 2997: fewer than two" in out and "node 2998: fewer than two" in out and "node 2993" not in out
