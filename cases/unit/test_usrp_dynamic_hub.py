# SPDX-License-Identifier: MIT
"""The dynamic-client cases (cases/proto/test_usrp_dynamic.py) proven end
to end against dvtest.usrp_hub.FakeHub, plus the session builders."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dvtest import usrp_hub
from dvtest.proto import usrp

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_session_builders():
    reg = usrp.text(3, usrp.REG)
    assert len(reg) == usrp.HEADER_SIZE + len(usrp.REG) and usrp.text_payload(reg) == usrp.REG
    assert usrp.text_payload(usrp.voice(0, bytes(320))) is None
    assert usrp.type_le(usrp.dtmf(1, "4001")) == usrp.TYPE_DTMF
    cmd = usrp.remote_cmd(2, "tgs=4001")
    assert usrp.parse_tlv(cmd[usrp.HEADER_SIZE:]) == (usrp.TLV_REMOTE_CMD, b"tgs=4001")


def test_dynamic_cases_against_fake_hub(tmp_path):
    hub = usrp_hub.FakeHub()
    env = dict(os.environ, DVT_USRP_DYNAMIC_HUB="%s:%d" % hub.addr, DVT_OUT=str(tmp_path))
    try:
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "proto",
                               "cases/proto/test_usrp_dynamic.py"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    finally:
        hub.close()
    tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else done.stderr[-500:]
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
    assert "15 passed" in tail and "skipped" not in tail, tail
