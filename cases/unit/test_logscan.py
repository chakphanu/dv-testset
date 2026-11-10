# SPDX-License-Identifier: MIT
"""dvtest.logscan: the post-run scan of the SUT logs for needles."""
import io
import os

import pytest

from dvtest import logscan

pytestmark = pytest.mark.unit


def mk_logs(tmp_path, text, name="xlxd-sut.log"):
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text)
    return str(tmp_path)


def test_scan_text_finds_needles_with_line_numbers_and_honours_the_filter():
    text = "a kiss listener up\nkiss peer 192.0.2.7 accepted\nother 192.0.2.7 line\n"
    assert logscan.scan_text(text, ["192.0.2.7"]) == [(2, "192.0.2.7"), (3, "192.0.2.7")]
    assert logscan.scan_text(text, ["192.0.2.7"], line_filter="kiss") == [(2, "192.0.2.7")]
    assert logscan.scan_text(text, [""]) == []


def test_request_without_logs_leaves_a_needle_file_and_is_pending(tmp_path):
    state, path, looked = logscan.request(str(tmp_path), "CASE-x", ["n1", "", "n2"], line_filter="kiss")
    assert state == "pending" and looked == 0
    assert os.path.isfile(path) and path.endswith("logscan/CASE-x.json")


def test_request_with_logs_scans_at_once(tmp_path):
    out = mk_logs(tmp_path, "kiss up\nmarker-1 here\n")
    state, found, looked = logscan.request(out, "CASE-y", ["marker-1"])
    assert state == "scanned" and looked == 2
    assert [(os.path.basename(p), h) for p, h in found.items()] == [("xlxd-sut.log", [(2, "marker-1")])]


def test_the_host_scan_reports_clean_found_and_missing(tmp_path):
    logscan.request(str(tmp_path), "CASE-z", ["marker-2"], line_filter="kiss")
    buf = io.StringIO()
    assert logscan.run_dir(str(tmp_path), buf) == 2                  # needle file, but no log yet
    assert "no SUT log" in buf.getvalue()
    mk_logs(tmp_path, "kiss started\nmarker-2 without the filter word\n")
    buf = io.StringIO()
    assert logscan.run_dir(str(tmp_path), buf) == 0                  # the marker line has no `kiss`: filtered out
    assert "clean" in buf.getvalue()
    mk_logs(tmp_path, "kiss started\nkiss got marker-2\n")
    buf = io.StringIO()
    assert logscan.run_dir(str(tmp_path), buf) == 1
    assert "xlxd-sut.log:2" in buf.getvalue() and "FOUND" in buf.getvalue()


def test_run_dir_without_a_scan_request_is_an_error(tmp_path):
    assert logscan.run_dir(str(tmp_path), io.StringIO()) == 2
    assert logscan.main([str(tmp_path)]) == 2
