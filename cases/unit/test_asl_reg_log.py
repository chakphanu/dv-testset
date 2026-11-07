# SPDX-License-Identifier: MIT
"""dvtest/tools/asl_reg_log.py: the log scan of the ASL1h lines (no rig)."""
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
