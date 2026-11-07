# SPDX-License-Identifier: MIT
"""dvtest/tools/config_check.py: reading the router's startup lines (no docker here)."""
import pytest

from dvtest.tools import config_check as cc

pytestmark = pytest.mark.unit

INVALID = [
    "dv-router: /etc/dv-router/dv-router.toml:51:7: fabric.acl: fabric.acl was removed with the broker ACL; delete the line and restart",
    "dv-router: /etc/dv-router/dv-router.toml: 1 error(s), not started",
]
VALID = [
    "dv-router: XLX999 node xlx999 on 172.30.0.11, rooms ABC",
    "dv-router: config /etc/dv-router/dv-router.toml gen 1 (f-33); edges use fabric configuration",
    "dv-router: config warning: /etc/dv-router/dv-router.toml:196:8: admin.totp: admin.totp is ignored; use admin.totp_required",
    "dv-router: config warning: ids.registry: node.state_dir is not set: dv-svc-iddb runs lookup-only",
    "dv-router: edges: dv-edge-dextra dv-edge-mmdvm",
]


def test_judge_invalid_lists_the_positioned_errors():
    v = cc.judge(INVALID, 2)
    assert not v["valid"] and not v["started"] and v["warnings"] == []
    assert v["errors"][0].startswith("/etc/dv-router/dv-router.toml:51:7: fabric.acl")
    assert "1 error(s), not started" in v["errors"][-1]


def test_judge_valid_keeps_warnings():
    v = cc.judge(VALID, None)
    assert v["valid"] and v["started"] and not v["errors"]
    assert [w.split(": ")[1] if ":" in w else w for w in v["warnings"]][0] == "admin.totp"
    assert len(v["warnings"]) == 2


def test_judge_exit_code_two_is_invalid_even_without_the_summary_line():
    assert not cc.judge(["dv-router: usage: dv-router --config FILE"], 2)["valid"]
    assert cc.judge(["dv-router: usage"], None)["valid"] is True


def test_secret_file_paths():
    text = '''[edges.allstar]
password_file = "/etc/dv-router/asl-secrets/node-2999.pw"
secret_file = "/etc/dv-router/asl-secrets/acct.secret"
other = "/etc/not-a-file-key"
# turn_secret_file = "/commented"
turn_secret_file = "/run/secrets/turn-secret"
secret_file = "/etc/dv-router/asl-secrets/acct.secret"
relative_file = "rel/path"
'''
    assert cc.secret_file_paths(text) == ["/etc/dv-router/asl-secrets/node-2999.pw", "/etc/dv-router/asl-secrets/acct.secret",
                                         "/run/secrets/turn-secret"]


def test_docker_command_is_contained():
    cmd = cc.docker_command("dv-router:wip-c165c93", "/tmp/x/dv-router.toml", "dvt-cfgcheck-1", "172.30.0",
                            [("/tmp/x/secret-0", "/etc/dv-router/asl-secrets/acct.secret")], uid=1000, gid=1001)
    assert cmd[:3] == ["docker", "run", "--rm"] and "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    for flag in ("--read-only", "--memory", "--memory-swap", "--cpus", "--pids-limit"):
        assert flag in cmd, flag
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL" and cmd[cmd.index("--security-opt") + 1] == "no-new-privileges"
    assert cmd[cmd.index("--user") + 1] == "1000:1001"
    assert "-p" not in cmd and "--publish" not in cmd and "--privileged" not in cmd
    mounts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    assert mounts == ["/tmp/x/dv-router.toml:/etc/dv-router/dv-router.toml:ro",
                      "/tmp/x/secret-0:/etc/dv-router/asl-secrets/acct.secret:ro"]
    at = cmd.index("dv-router:wip-c165c93")
    assert cmd[at + 1:] == ["--config", "/etc/dv-router/dv-router.toml", "XLX999", "172.30.0.11", "172.30.0.21"]
    assert "--entrypoint" not in cmd


# ---- `dv-router config check FILE` (compat1 images): output as seen on wip-488b89d
CHECK_BAD = [
    "dv-router: /etc/dv-router/dv-router.toml:170:1: web.operator_totp: unknown field `operator_totp`, expected one of `enable`, `totp_required`",
]
CHECK_SYNTAX = [
    "dv-router: /etc/dv-router/dv-router.toml:169:10: extra `=`, expected nothing",
    "dv-router: /etc/dv-router/dv-router.toml:169:12: unexpected key or value, expected newline, `#`",
]
CHECK_OK = [
    "dv-router: /etc/dv-router/dv-router.toml:66:9: ids.dmrid: /xlxd/dmrid.dat not found: callsigns fall back to ids",
    "dv-router: ids.registry: node.state_dir is not set: dv-svc-iddb runs lookup-only",
    "configuration: /etc/dv-router/dv-router.toml",
    "child: dv-room[A]",
    "listen: dv-edge-dextra udp 172.30.0.11:30001",
]
CHECK_OLD_IMAGE = [
    'dv-router: invalid CALLSIGN "config": expected 3-8 letters/digits with a letter and a digit',
    "usage: dv-router --config FILE [CALLSIGN OWN_IP TRANSCODER_IP]",
]


def test_judge_check_invalid_parses_file_line_key_message():
    v = cc.judge_check(CHECK_BAD, 2)
    assert not v["valid"] and not v["unsupported"] and v["warnings"] == []
    assert v["parsed"] == [{"file": "/etc/dv-router/dv-router.toml", "line": 170, "col": 1, "key": "web.operator_totp",
                            "message": "unknown field `operator_totp`, expected one of `enable`, `totp_required`"}]
    assert v["errors"][0].startswith("/etc/dv-router/dv-router.toml:170:1: web.operator_totp")


def test_judge_check_syntax_error_has_no_key():
    v = cc.judge_check(CHECK_SYNTAX, 2)
    assert not v["valid"] and [p["key"] for p in v["parsed"]] == ["", ""]
    assert v["parsed"][0]["line"] == 169 and v["parsed"][0]["message"].startswith("extra `=`")


def test_judge_check_valid_exit_zero_keeps_positioned_lines_as_warnings():
    v = cc.judge_check(CHECK_OK, 0)
    assert v["valid"] and v["errors"] == [] and v["parsed"] == []
    assert v["warnings"] == ["/etc/dv-router/dv-router.toml:66:9: ids.dmrid: /xlxd/dmrid.dat not found: callsigns fall back to ids"]


def test_judge_check_nonzero_exit_without_a_position_line_is_still_invalid():
    v = cc.judge_check(["dv-router: something odd"], 1)
    assert not v["valid"] and not v["unsupported"] and v["errors"] == [] and v["parsed"] == []


def test_judge_check_image_without_the_subcommand_is_unsupported_not_invalid():
    v = cc.judge_check(CHECK_OLD_IMAGE, 2)
    assert v["unsupported"] and not v["valid"] and v["parsed"] == []


def test_docker_command_check_mode_runs_config_check_with_the_same_caps():
    cmd = cc.docker_command("dv-router:wip-488b89d", "/tmp/x/dv-router.toml", "dvt-cfgcheck-1", "172.30.0", [], uid=1000, gid=1001,
                            mode="check")
    at = cmd.index("dv-router:wip-488b89d")
    assert cmd[at + 1:] == ["config", "check", "/etc/dv-router/dv-router.toml"]
    assert cmd[cmd.index("--network") + 1] == "none" and "--read-only" in cmd and "--memory" in cmd and "--pids-limit" in cmd
    assert cmd[cmd.index("--cap-drop") + 1] == "ALL" and "-p" not in cmd and "--privileged" not in cmd
    with pytest.raises(ValueError):
        cc.docker_command("i", "/x", "n", mode="bogus")
    assert "compat1" in cc.GENS_WITH_CHECK and "c2465de" not in cc.GENS_WITH_CHECK
