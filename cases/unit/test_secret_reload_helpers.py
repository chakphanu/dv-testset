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


# ---------------------------------------------------------------- launcher log (SIGHUP variant)

LOG = """\
xlxd-sut-1  | 2026-11-10T01:00:00.100000000Z dv-router: config reload: applied generation 4
xlxd-sut-1  | 2026-11-10T01:05:00.500000000Z dv-router: reload refused: edges.aprsis.passcode_file: cannot read /etc/dv-router/aprs-secrets/passcode (Permission denied)
xlxd-sut-1  | 2026-11-10T01:05:01.000000000Z dv-router: dv-edge-aprsis: running generation 4 kept
xlxd-sut-1  | 2026-11-10T01:09:00.000000000Z dv-router: reload refused: edges.aprsis.passcode_file: cannot read /etc/dv-router/aprs-secrets/passcode
"""
T_0105 = 1794272700.0       # 2026-11-10T01:05:00Z


def test_log_epoch_and_shape():
    assert sr.log_epoch("xlxd-sut-1  | 2026-11-10T01:05:00.500000000Z x") == pytest.approx(T_0105 + 0.5)
    assert sr.log_epoch("no time stamp") is None
    shape = sr.line_shape(LOG.splitlines()[1])
    assert shape.startswith("dv-router: reload refused: edges.aprsis.passcode_file: cannot read /etc/dv-router/aprs-secrets/passcode")
    assert "2026" not in shape and "xlxd-sut-1" not in shape
    assert sr.line_shape(LOG.splitlines()[0]) == "dv-router: config reload: applied generation N"


def test_refusal_lines_need_the_key_the_path_and_a_time_at_or_after_the_signal():
    path = "/etc/dv-router/aprs-secrets/passcode"
    got = sr.refusal_lines(LOG, T_0105, "passcode_file", path)
    assert len(got) == 2 and all("passcode_file" in g and path in g for g in got)
    assert len(sr.refusal_lines(LOG, T_0105 + 60, "passcode_file", path)) == 1       # the 01:09 line only
    assert sr.refusal_lines(LOG, T_0105 + 600, "passcode_file", path) == []
    assert sr.refusal_lines(LOG, T_0105, "passcode_file", "/other/path") == []
    assert sr.refusal_lines(LOG, T_0105, "turn_secret_file", path) == []
    assert sr.refusal_lines(LOG, T_0105 + 0.4, "passcode_file", path)               # the 1 s slack before the signal


def test_cli_logcheck(tmp_path, capsys):
    log = tmp_path / "xlxd-sut.log"
    log.write_text(LOG)
    expect = tmp_path / "expect.json"
    shape_file = tmp_path / "shape.txt"
    expect.write_text(json.dumps({"t_hup": T_0105, "key": "passcode_file", "path": "/etc/dv-router/aprs-secrets/passcode"}))
    assert sr.main(["logcheck", "--log", str(log), "--expect", str(expect), "--shape-file", str(shape_file)]) == 0
    printed = capsys.readouterr().out
    assert "of the shape: dv-router: reload refused" in printed and "2026" not in printed
    assert "passcode_file" in shape_file.read_text()
    expect.write_text(json.dumps({"t_hup": T_0105 + 3600, "key": "passcode_file", "path": "/etc/dv-router/aprs-secrets/passcode"}))
    assert sr.main(["logcheck", "--log", str(log), "--expect", str(expect)]) == 1
    assert "no launcher log line" in capsys.readouterr().out
    assert sr.main(["logcheck", "--log", str(tmp_path / "missing.log"), "--expect", str(expect)]) == 2


# ---------------------------------------------------------------- SECRELOAD2: reload preview / apply replies

PREVIEW_OK = {"mode": "preview", "etag": "f-0123456789abcdef", "changed": ["edges.aprsis.passcode_file"], "no_change": False,
              "actions": [{"class": "Restart", "child": "dv-edge-aprsis"}], "warnings": []}
APPLIED_OK = {"status": "applied", "etag": "f-0123456789abcdef", "changed": ["edges.aprsis.passcode_file"], "no_change": False, "inc": 1,
              "gen": 3, "actions": [{"class": "Restart", "child": "dv-edge-aprsis"}], "pending_restart": []}


def test_etag_helpers():
    assert sr.etag_value('"f-1"') == sr.etag_value('W/"f-1"') == sr.etag_value("f-1") == "f-1" and sr.etag_value(None) == ""
    stale = sr.stale_etag('"f-0123456789abcdef"')
    assert stale == '"f-0123456789abcde0"' and sr.etag_value(stale) != "f-0123456789abcdef"
    assert sr.stale_etag('"f-0"') == '"f-1"'
    assert sr.weak_etag('"f-1"') == 'W/"f-1"'
    with pytest.raises(ValueError):
        sr.stale_etag("")


def test_preview_problems_accepts_the_specified_shape():
    assert sr.preview_problems(PREVIEW_OK, '"f-0123456789abcdef"') == []
    unchanged = dict(PREVIEW_OK, changed=[], no_change=True, actions=[])
    assert sr.preview_problems(unchanged) == []


@pytest.mark.parametrize("change,needle", [
    (lambda b: b.pop("warnings"), "keys"),
    (lambda b: b.update(extra=1), "keys"),
    (lambda b: b.update(mode="apply"), "mode"),
    (lambda b: b.update(etag=""), "etag"),
    (lambda b: b.update(changed="edges.aprsis.passcode_file"), "changed"),
    (lambda b: b.update(no_change="no"), "no_change"),
    (lambda b: b.update(no_change=True), "no_change is true"),
    (lambda b: b.update(changed=[], actions=[]), "no_change is false"),
    (lambda b: b.update(actions={}), "actions"),
    (lambda b: b.update(warnings=None), "warnings"),
])
def test_preview_problems_names_the_field(change, needle):
    body = json.loads(json.dumps(PREVIEW_OK))
    change(body)
    problems = sr.preview_problems(body)
    assert problems and any(needle in p for p in problems), problems
    assert sr.preview_problems([]) == ["the preview is not a JSON object"]


def test_preview_etag_must_equal_the_header():
    assert any("ETag header" in p for p in sr.preview_problems(PREVIEW_OK, '"f-ffffffffffffffff"'))


def test_applied_problems():
    assert sr.applied_problems(APPLIED_OK, "f-0123456789abcdef") == []
    assert sr.applied_problems(dict(APPLIED_OK, changed=[], no_change=True, actions=[])) == []
    for change, needle in [(lambda b: b.update(status="preview"), "status"), (lambda b: b.update(gen=-1), "gen"),
                           (lambda b: b.update(inc=True), "inc"), (lambda b: b.update(pending_restart="x"), "pending_restart"),
                           (lambda b: b.pop("inc"), "keys"), (lambda b: b.update(mode="apply"), "keys")]:
        body = json.loads(json.dumps(APPLIED_OK))
        change(body)
        assert any(needle in p for p in sr.applied_problems(body)), needle


def test_problem_code():
    assert sr.problem_code({"code": "etag_mismatch"}) == "etag_mismatch" and sr.problem_code([]) is None and sr.problem_code({}) is None


AUDIT_OLDEST_FIRST = [
    {"action": "login", "user": "ada", "result": "ok", "ts_ms": 1},
    {"action": "config.reload.preview", "user": "otto", "result": "denied", "ts_ms": 2},
    {"action": "config.reload", "user": "ada", "result": "requested", "diff": {"changed": []}, "ts_ms": 3},
    {"action": "config.reload", "user": "ada", "result": "applied", "diff": {"changed": ["edges.aprsis.passcode_file"]}, "ts_ms": 3},
]
# as GET /audit serves it: newest first (Audit::tail reverses)
AUDIT = {"total": 4, "items": list(reversed(AUDIT_OLDEST_FIRST))}
EXPECTED = [("config.reload.preview", "otto", "denied", None), ("config.reload", "ada", "requested", []),
            ("config.reload", "ada", "applied", ["edges.aprsis.passcode_file"])]


def test_reload_audit_selects_the_reload_records_oldest_first():
    assert sr.reload_audit(AUDIT) == EXPECTED
    assert sr.reload_audit(AUDIT, user="otto") == [("config.reload.preview", "otto", "denied", None)]
    assert sr.reload_audit(AUDIT, actions=("login",)) == [("login", "ada", "ok", None)]
    assert sr.reload_audit({}) == [] and sr.reload_audit(None) == []


def test_audit_chronological_handles_both_served_orders_and_ties():
    assert [i["ts_ms"] for i in sr.audit_chronological(AUDIT)] == [1, 2, 3, 3]
    assert [i["result"] for i in sr.audit_chronological(AUDIT) if i["ts_ms"] == 3] == ["requested", "applied"]     # ties keep the pair order
    oldest = {"items": AUDIT_OLDEST_FIRST}
    assert sr.reload_audit(oldest) == EXPECTED                       # a service that serves oldest first is read the same way
    ties = {"items": [{"action": "config.reload", "result": "invalid", "ts_ms": 5}, {"action": "config.reload", "result": "requested", "ts_ms": 5}]}
    assert [i["result"] for i in sr.audit_chronological(ties)] == ["requested", "invalid"]
    assert sr.audit_chronological({"items": []}) == [] and sr.audit_chronological(None) == []


def test_audit_chronological_with_distinct_timestamps_in_either_order():
    # OBSPKT1b (432cecc8): the completion record has its own ts_ms >= the requested record's; /audit is newest first, `limit` keeps the newest
    oldest = [{"action": "config.reload", "result": "requested", "ts_ms": 10}, {"action": "config.reload", "result": "applied", "ts_ms": 15},
              {"action": "config.reload", "result": "requested", "ts_ms": 20}, {"action": "config.reload", "result": "unchanged", "ts_ms": 20}]
    newest_first = {"items": list(reversed(oldest))}
    assert [i["result"] for i in sr.audit_chronological(newest_first)] == ["requested", "applied", "requested", "unchanged"]
    assert [i["result"] for i in sr.audit_chronological({"items": oldest})] == ["requested", "applied", "requested", "unchanged"]
    # a single record, and a tail whose first and last record share a ts_ms, keep the newest-first assumption
    assert sr.audit_chronological({"items": oldest[:1]}) == oldest[:1]
    same = {"items": [{"result": "b", "ts_ms": 7}, {"result": "a", "ts_ms": 7}]}
    assert [i["result"] for i in sr.audit_chronological(same)] == ["a", "b"]


def test_a_suppressed_count_does_not_disturb_the_reload_audit():
    body = {"items": [{"action": "config.reload", "user": "ada", "result": "denied", "ts_ms": 30, "suppressed": 4},
                      {"action": "config.reload", "user": "ada", "result": "denied", "ts_ms": 1}]}
    assert sr.reload_audit(body, user="ada") == [("config.reload", "ada", "denied", None)] * 2


def test_the_failure_of_the_first_rig_run_is_gone():
    # served newest first, as in out/20261110T112609Z: invalid, requested, applied, ..., then the five `ok` reloads of the SECRELOAD1 PUTs
    served = {"items": [{"action": "config.reload", "user": "admin", "result": r, "diff": {"changed": c}, "ts_ms": t} for r, c, t in [
        ("invalid", ["edges.aprsis.passcode_file"], 9), ("requested", [], 9), ("applied", ["edges.aprsis.passcode_file"], 8),
        ("requested", [], 8), ("unchanged", [], 7), ("requested", [], 7), ("applied", ["edges.aprsis.passcode_file"], 6),
        ("requested", [], 6), ("ok", None, 2), ("ok", None, 1)]]}
    results = [(r, c) for _a, _u, r, c in sr.reload_audit(served, user="admin", actions=("config.reload",))]
    assert results[0] == ("ok", None) and results[-2:] == [("requested", []), ("invalid", ["edges.aprsis.passcode_file"])]


def test_history_count():
    assert sr.history_count([1, 2]) == 2 and sr.history_count({"total": 1, "items": [{"seq": 1}]}) == 1
    assert sr.history_count({"total": 0, "items": []}) == 0
    assert sr.history_count({"entries": []}) is None and sr.history_count({"x": 1}) is None and sr.history_count("no") is None


def test_names_secret_change_accepts_the_changed_secret_keys_of_the_http_reply():
    action = {"class": "restart", "child": "dv-edge-aprsis", "path": "edges.aprsis.passcode_file",
              "changed_secret_keys": ["edges.aprsis.passcode_file"], "impact": "restart dv-edge-aprsis (quiet)"}
    assert sr.names_secret_change(action) and sr.reload_actions({"actions": [action]}) == [action]
    assert not sr.names_secret_change({"class": "restart", "child": "dv-edge-aprsis", "changed_secret_keys": ["web.turn_secret_file"]})
    assert not sr.names_secret_change({"class": "restart", "child": "dv-edge-aprsis", "changed_secret_keys": "edges.aprsis.passcode_file"})


VALIDATION = {"type": "/api/v1/problems/validation", "title": "configuration rejected", "status": 422, "code": "validation", "detail": "d",
              "instance": "/api/v1/admin/config/reload", "current_etag": "f-0123456789abcdef",
              "errors": [{"path": "edges.aprsis.passcode_file", "pointer": "/edges/aprsis/passcode_file",
                          "msg": "/etc/dv-router/aprs-secrets/passcode: cannot read secret file: permission denied"}]}


def test_validation_problems():
    assert sr.validation_problems(VALIDATION) == []
    for change, needle in [
        (lambda b: b.update(code="etag_mismatch"), "code"),
        (lambda b: b.update(actions=[]), "actions"),
        (lambda b: b.pop("errors"), "errors[]"),
        (lambda b: b.update(errors=[]), "errors[]"),
        (lambda b: b["errors"][0].pop("pointer"), "pointer"),
        (lambda b: b["errors"][0].update(path="web.turn_secret_file"), "no error has path"),
        (lambda b: b.update(current_etag='"f-0123456789abcdef"'), "unquoted"),
    ]:
        body = json.loads(json.dumps(VALIDATION))
        change(body)
        assert any(needle in p for p in sr.validation_problems(body)), needle
    assert sr.validation_problems("x") == ["the 422 body is not a JSON object"]
    assert sr.validation_problems(dict(VALIDATION, errors=[dict(VALIDATION["errors"][0], path="a.b")]), key="a.b") == []
