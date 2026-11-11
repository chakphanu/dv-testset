# SPDX-License-Identifier: MIT
"""dvtest/stallscan.py (WEBSTALL1 evidence: host I/O pressure and the nearest audit login of a slow web request) and the load case's use of it."""
import json
import sys
import time
from pathlib import Path

import pytest

from dvtest import stallscan

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import webload_support as ws  # noqa: E402

pytestmark = pytest.mark.unit

PSI = "some avg10=0.50 avg60=0.10 avg300=0.04 total=4868761706\nfull avg10=0.25 avg60=0.05 avg300=0.02 total=4411498921\n"


def test_parse_psi_reads_totals_and_averages():
    got = stallscan.parse_psi(PSI)
    assert got == {"some_us": 4868761706, "some_avg10": 0.5, "full_us": 4411498921, "full_avg10": 0.25}
    assert stallscan.parse_psi("some avg10=0.00 total=5\n") == {"some_us": 5, "some_avg10": 0.0}
    assert stallscan.parse_psi("") is None and stallscan.parse_psi("full avg10=1 total=3\n") is None and stallscan.parse_psi("some total=x\n") is None


def test_read_psi_is_none_without_the_file(tmp_path):
    assert stallscan.read_psi(str(tmp_path / "missing")) is None
    path = tmp_path / "io"
    path.write_text(PSI)
    assert stallscan.read_psi(str(path))["some_us"] == 4868761706


def test_psi_delta_brackets_the_window():
    rows = [[100.0, 1000, 500], [101.0, 1000, 500], [102.0, 801000, 400500], [103.0, 901000, 450500]]
    assert stallscan.psi_delta(rows, 101.2, 101.9) == (800000, 400000)
    assert stallscan.psi_delta(rows, 101.0, 102.0) == (800000, 400000)
    assert stallscan.psi_delta(rows, 101.5, 102.5) == (900000, 450000)               # from the row at or before the start to the first at or after the end
    assert stallscan.psi_delta(rows, 99.0, 101.0) is None and stallscan.psi_delta(rows, 102.5, 109.0) is None and stallscan.psi_delta([], 1, 2) is None


def test_nearest_login_is_signed_and_ignores_other_actions(tmp_path):
    audit = [{"action": "login", "ts_ms": 1000000, "result": "ok"}, {"action": "register", "ts_ms": 1004000, "result": "ok"},
             {"action": "login", "ts_ms": 1006000, "result": "denied"}, {"action": "login", "ts_ms": "bad"}]
    near = stallscan.nearest_login(audit, 1004.2)
    assert near == {"ts": 1006.0, "delta_s": 1.8, "result": "denied"}
    assert stallscan.nearest_login(audit, 999.0)["delta_s"] == 1.0
    assert stallscan.nearest_login([], 5.0) is None and stallscan.nearest_login([{"action": "register", "ts_ms": 1}], 5.0) is None
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(audit[0]) + "\nnot json\n[1]\n" + json.dumps(audit[2]) + "\n")
    assert len(stallscan.load_audit(str(path))) == 2 and stallscan.load_audit(str(tmp_path / "none")) == []


def slow_items():
    return [{"method": "POST", "path": "/api/v1/rtc/offer", "seconds": 5.5, "start": 101.0, "web_cpu_pct_during": 3.0}]


def test_annotate_adds_io_stall_and_nearest_login():
    rows = [[100.0, 0, 0], [101.0, 0, 0], [105.0, 4000000, 3000000], [107.0, 5000000, 3500000]]
    audit = [{"action": "login", "ts_ms": 100500, "result": "ok"}]
    item = stallscan.annotate(slow_items(), rows, audit)[0]
    assert item["io_some_ms"] == 5000.0 and item["io_full_ms"] == 3500.0 and item["nearest_login"]["delta_s"] == -0.5
    line = stallscan.describe(item)
    assert "nearest login -0.500 s (ok)" in line and "some 5000.0 ms / full 3500.0 ms" in line
    bare = stallscan.annotate(slow_items(), [], [])[0]
    assert bare["io_some_ms"] is None and bare["nearest_login"] is None and "io pressure not sampled" in stallscan.describe(bare)


def test_the_cli_prints_the_table_of_a_run(tmp_path, capsys):
    run = tmp_path / "run"
    (run / "web").mkdir(parents=True)
    (run / "sut-state" / "web").mkdir(parents=True)
    (run / "web" / "load.json").write_text(json.dumps({"cases": {"w8_200": {
        "slow_requests": {"count": 1, "items": [{"method": "POST", "path": "/api/v1/rtc/offer", "seconds": 5.5, "start": 101.0}]},
        "io_pressure": {"series": [[100.0, 0, 0], [101.0, 0, 0], [107.0, 5000000, 3500000]]}}}}))
    (run / "sut-state" / "web" / "audit.jsonl").write_text(json.dumps({"action": "login", "ts_ms": 100500, "result": "ok"}) + "\n")
    assert stallscan.main([str(run)]) == 0
    out = capsys.readouterr().out
    assert "w8_200: 1 slow requests (1 audit records)" in out and "nearest login -0.500 s" in out
    assert stallscan.main([str(tmp_path / "nothing")]) == 2


def test_the_psi_sampler_collects_rows_and_summarises(tmp_path):
    path = tmp_path / "io"
    path.write_text(PSI)
    sampler = ws.PsiSampler(str(path), period_s=0.05)
    sampler.start()
    time.sleep(0.2)
    path.write_text("some avg10=1.00 avg60=0.10 avg300=0.04 total=4869761706\nfull avg10=0.25 avg60=0.05 avg300=0.02 total=4411598921\n")
    time.sleep(0.2)
    rows = sampler.stop()
    assert len(rows) >= 4 and not sampler.is_alive()
    summary = ws.psi_summary(rows)
    assert summary["available"] and summary["some_ms_total"] == 1000.0 and summary["full_ms_total"] == 100.0 and summary["some_ms_max_step"] == 1000.0
    assert ws.psi_summary([])["available"] is False
    missing = ws.PsiSampler(str(tmp_path / "none"), period_s=0.05)
    missing.start()
    time.sleep(0.15)
    assert missing.stop() == []


def test_annotate_slow_reads_the_audit_log_and_updates_the_longest(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({"action": "login", "ts_ms": 100500, "result": "ok"}) + "\n")
    summary = {"slow_requests": ws.slow_stats({"slow_count": 1, "slow": [{"m": "POST", "p": "/api/v1/rtc/offer", "d": 5.5, "t": 101.0}]}),
               "io_pressure": {"series": [[100.0, 0, 0], [101.0, 0, 0], [107.0, 5000000, 3500000]]}}
    ws.annotate_slow(summary, str(audit))
    top = summary["slow_requests"]["longest"]
    assert top["io_some_ms"] == 5000.0 and top["nearest_login"]["delta_s"] == -0.5
    text = ws.stall_text(summary["slow_requests"])
    assert "nearest login in the audit log: -0.500 s (ok)" in text and "some 5000.0 ms, full 3500.0 ms" in text
    assert ws.annotate_slow({"listeners": 1}) == {"listeners": 1}
