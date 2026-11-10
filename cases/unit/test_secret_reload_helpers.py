# SPDX-License-Identifier: MIT
"""dvtest/secret_reload.py: the helpers of the SECRELOAD1 case (values, reload marker, scanning, admin replies, metrics)."""
import json
import os
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from dvtest import metrics, secret_reload as sr  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- values

@pytest.mark.parametrize("current", ["12345", "12345\n", "7", "7\n", "30000"])
def test_new_passcode_keeps_length_and_newline_and_differs(current):
    rng = random.Random(1)
    for _ in range(50):
        value = sr.new_passcode(current, rng)
        assert value != current and value.strip().isdigit()
        assert len(value) == len(current) and value.endswith("\n") == current.endswith("\n")
        assert len(value.strip()) == len(current.strip()) and not (len(value.strip()) > 1 and value.startswith("0"))


@pytest.mark.parametrize("bad", ["", "abc", "123456", "-1", "12 3"])
def test_new_passcode_refuses_what_is_not_one_to_five_digits(bad):
    with pytest.raises(ValueError):
        sr.new_passcode(bad)


def test_digest_note_has_the_length_only():
    assert sr.digest_note("12345") == "5 characters" and "12345" not in sr.digest_note("12345")


# ---------------------------------------------------------------- reload marker

def test_with_marker_appends_one_marker_and_replaces_the_old_one():
    base = "[node]\ncallsign = \"XLX999\"\n"
    once = sr.with_marker(base, 1)
    twice = sr.with_marker(once, 2)
    assert once.endswith("# secreload1 reload marker 1\n") and twice.endswith("# secreload1 reload marker 2\n")
    assert twice.count(sr.MARKER) == 1 and twice.startswith(base.rstrip("\n"))
    assert once != twice != base


def test_with_marker_changes_only_a_comment():
    lines = [ln for ln in sr.with_marker("a = 1\n", 7).splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines == ["a = 1"]


# ---------------------------------------------------------------- scanning

ENTRIES = sr.entries_for("4242\n", "31337\n")


def test_find_in_text_finds_the_value_after_a_word_alone_on_a_line_or_as_a_token():
    assert sr.find_in_text("user N0TEST-5 pass 4242 vers dvt", ENTRIES) == [("old", 1)]
    assert sr.find_in_text("a\nb\n31337\nc\n", ENTRIES) == [("new", 3)]
    assert sr.find_in_text("login ok 31337 done", ENTRIES) == [("new", 1)]          # the new value is also a bare token
    assert sr.find_in_text("login ok 4242 done", ENTRIES) == []                     # the old value only after a word or alone


@pytest.mark.parametrize("text", [
    "port 131337 open", "172.30.0.31337", "pid=313371", "/proc/31337/status", "2026-11-10T01:31:33.7Z", "x31337y", "-31337", "v1.31337",
])
def test_bare_token_does_not_match_inside_longer_words_numbers_paths_or_addresses(text):
    assert sr.find_in_text(text, ENTRIES) == []


def test_scan_tree_reports_paths_and_lines_never_values(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "a.log").write_text("ok\nuser X pass 4242 vers\n")
    (tmp_path / "logs" / "b.log").write_text("nothing here\n")
    (tmp_path / "aprs-secrets").mkdir()
    (tmp_path / "aprs-secrets" / "passcode").write_text("31337\n")
    (tmp_path / "capture").mkdir()
    (tmp_path / "capture" / "x.txt").write_text("pass 31337\n")
    (tmp_path / "wire.pcap").write_bytes(b"\x00 pass 31337 \x00")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01 secret=31337 \xff")
    hits = sr.scan_tree(tmp_path, ENTRIES, skip=["aprs-secrets/passcode"], skip_dirs=["capture"])
    assert hits == [("bin.dat", "new", 1), (os.path.join("logs", "a.log"), "old", 2)]
    assert "31337" not in json.dumps(hits) and "4242" not in json.dumps(hits)


def test_scan_tree_does_not_follow_links_and_skips_big_files(tmp_path):
    (tmp_path / "real.txt").write_text("pass 4242\n")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    (tmp_path / "big.txt").write_text("pass 4242\n" + "x" * 100)
    hits = sr.scan_tree(tmp_path, ENTRIES, skip=["real.txt"], max_bytes=50)
    assert hits == []


def test_collision_free_passcode_avoids_a_value_that_is_already_a_token(tmp_path):
    (tmp_path / "t.log").write_text("port 20000 20001 20002 20003 20004 20005 20006 20007 20008 20009\n")
    rng = random.Random(3)
    for _ in range(20):
        value = sr.collision_free_passcode("20000\n", tmp_path, rng)
        assert value != "20000\n" and not sr.scan_tree(tmp_path, [{"name": "n", "value": value.strip(), "token": True}])


def test_collision_free_passcode_gives_up_when_every_value_is_taken(tmp_path):
    (tmp_path / "t.log").write_text(" ".join(str(n) for n in range(0, 10)) + "\n")
    with pytest.raises(ValueError):
        sr.collision_free_passcode("5", tmp_path, random.Random(1), tries=30)


def test_leaks_keeps_a_label_and_a_line_number_not_the_value():
    leaks = sr.Leaks(ENTRIES)
    assert leaks.check("PUT 200", '{"actions": []}')
    assert not leaks.check("PUT 422", 'x\n"detail": "pass 4242"')
    report = leaks.report()
    assert "PUT 422" in report and "old" in report and "line 2" in report and "4242" not in report
    assert not sr.Leaks(ENTRIES).check("bytes", b"..31337..".replace(b"..", b" "))


# ---------------------------------------------------------------- admin replies

PREVIEW = {"mode": "preview", "actions": [
    {"class": "Restart", "child": "dv-edge-aprsis", "path": "edges.aprsis.passcode_file",
     "impact": "edges.aprsis.passcode_file content changed; restart dv-edge-aprsis"},
    {"class": "Live", "path": "log.level", "impact": "applied live"}]}


def test_reload_actions_selects_the_restart_of_the_child():
    got = sr.reload_actions(PREVIEW)
    assert len(got) == 1 and got[0]["child"] == "dv-edge-aprsis" and sr.names_secret_change(got[0])
    assert sr.reload_actions({"actions": []}) == [] and sr.reload_actions({}) == [] and sr.reload_actions(None) == []
    assert sr.reload_actions(PREVIEW, "allstar") == []


def test_names_secret_change_needs_key_and_content_changed():
    assert not sr.names_secret_change({"class": "Restart", "child": "dv-edge-aprsis", "impact": "restart"})
    assert not sr.names_secret_change({"path": "edges.aprsis.passcode_file", "impact": "changed"})


def test_error_names_reports_key_and_path_separately():
    body = {"errors": [{"path": "edges.aprsis.passcode_file", "msg": "cannot read /etc/dv-router/aprs-secrets/passcode"}]}
    assert sr.error_names(body, "passcode_file", "/etc/dv-router/aprs-secrets/passcode") == (True, True)
    assert sr.error_names("nothing", "passcode_file", "/x") == (False, False)


def test_capability_is_read_from_the_preview():
    assert sr.capability(PREVIEW, strict=False)[0] is True
    none_listed = sr.capability({"mode": "preview", "actions": []}, strict=True)
    assert none_listed[0] is False and "no restart of the APRS-IS edge" in none_listed[1]
    unnamed = {"actions": [{"class": "Restart", "child": "dv-edge-aprsis", "impact": "restart"}]}
    assert sr.capability(unnamed, strict=False)[0] is False and "does not name the key" in sr.capability(unnamed, strict=False)[1]


@pytest.mark.parametrize("status,body,want", [
    (409, {"code": "read_only", "detail": "the configuration file is read-only (download the TOML instead)"}, True),
    (409, "the configuration file is read-only: x", True),
    (422, {"detail": "1 configuration error(s)"}, False),
    (200, {"detail": "read-only"}, False),
])
def test_read_only_reply(status, body, want):
    assert sr.read_only_reply(status, body) is want


# ---------------------------------------------------------------- hub metrics

METRICS = """\
dv_launcher_child_restarts_total{child="dv-edge-aprsis"} 2
dv_launcher_child_restarts_total{child="dv-svc-web"} 1
dv_launcher_child_restarts_total{child="dv-room.a"} 0
dv_edge_aprsis_verified{proc="aprsis"} 1
"""


def test_child_restarts_sums_the_child_and_all():
    samples = metrics.parse(METRICS)
    assert sr.child_restarts(samples) == (2.0, 3.0)
    assert sr.child_restarts(samples, "web") == (1.0, 3.0)
    assert sr.child_restarts(samples, "nothing") == (None, 3.0)
    assert sr.child_restarts([]) == (None, 0.0)


def test_verified_reads_the_gauge():
    assert sr.verified(metrics.parse(METRICS)) is True
    assert sr.verified(metrics.parse(METRICS.replace("proc=\"aprsis\"} 1", "proc=\"aprsis\"} 0"))) is False
    assert sr.verified([]) is None


# ---------------------------------------------------------------- command line

def test_cli_scan_exit_codes(tmp_path, capsys):
    root = tmp_path / "out"
    root.mkdir()
    (root / "a.log").write_text("pass 4242\n")
    values = tmp_path / "values.json"
    values.write_text(json.dumps(ENTRIES))
    assert sr.main(["scan", "--values-file", str(values), "--root", str(root)]) == 1
    printed = capsys.readouterr().out
    assert "a.log" in printed and "old" in printed and "4242" not in printed
    assert sr.main(["scan", "--values-file", str(values), "--root", str(root), "--skip", "a.log"]) == 0
    values.write_text("[]")
    assert sr.main(["scan", "--values-file", str(values), "--root", str(root)]) == 2
