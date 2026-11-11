# SPDX-License-Identifier: MIT
"""WEBSTALL1 (dv-router unit WEBSTALL1): a slow disk under the SUT state directory must not hold the other requests of dv-svc-web.

Prepared offline, NOT run (docs/webstall1-cases.md). A login writes its audit record with a synchronous fsync; before the fix that ran on the
service's only tokio worker, so every request waited behind it (run 20261111T123937Z: 5.6 s). The case slows the disk without any privilege (a
buffered writer on the same filesystem, webstall_support.IoStaller), repeats logins, and probes /rtc/alive, /api/v1/public/state and /health
every 0.4 to 0.7 s. Over the spans of the logins that were slow (>= 1 s) every probe must be answered within 500 ms. No slow login = the disk was
not slow = inconclusive (skip; a failure with DVT_WEBSTALL1=on, for the fix image). The writer loads the host disk, so it needs
DVT_WEBSTALL1_IO=on (the owner's go); size and length: DVT_WEBSTALL_MB (2048), DVT_WEBSTALL_S (25). It also refuses to start (skip) on a btrfs whose
device has less than 2 GiB unallocated (room_problem): there the writer could push the metadata towards ENOSPC for the whole host.
On an image without the fix a stall that is seen is a FAIL: that is the reproduction.
"""
import json
import os
import time
from pathlib import Path

import pytest

from dvtest import web
from webload_support import PsiSampler
from webstall_support import IoStaller, Recorder, room_problem, verdict

pytestmark = [pytest.mark.rig, pytest.mark.web]

BASELINE_S = 6.0
AFTER_S = 5.0
LOGIN_PERIOD_S = 1.2            # the login budget is 1/s (burst 10)
DEAD_SESSION = "00" * 16


def on(name):
    return os.environ.get(name, "").lower() in ("on", "1", "true", "yes")


def inconclusive(message):
    if on("DVT_WEBSTALL1"):
        pytest.fail("inconclusive (DVT_WEBSTALL1=on): %s" % message)
    pytest.skip(message)


@pytest.fixture(scope="module")
def stall_accounts(web_operator):
    """Two accounts: one logs in again and again, the other keeps a session for the /rtc/alive probe."""
    out = []
    for call in ("N0ST01", "N0ST02"):
        password = web.new_password()
        web_operator.hide(password)
        web_operator.register_user(call, password, may_tx=False)
        out.append((call, password))
    return out


def test_a_slow_disk_does_not_hold_the_other_requests(stall_accounts, web_base_url, web_ca):
    if not on("DVT_WEBSTALL1_IO"):
        pytest.skip("DVT_WEBSTALL1_IO=on not set: the case writes up to DVT_WEBSTALL_MB MiB to the run directory to slow the disk (owner's go; "
                    "docs/webstall1-cases.md)")
    out_dir = Path(os.environ.get("DVT_OUT", "out"))
    megabytes, seconds = int(os.environ.get("DVT_WEBSTALL_MB") or 2048), float(os.environ.get("DVT_WEBSTALL_S") or 25.0)
    problem = room_problem(str(out_dir), megabytes, ignore_btrfs=on("DVT_WEBSTALL_IGNORE_BTRFS"))
    if problem:
        pytest.skip(problem)
    login_call, login_pw = stall_accounts[0]
    session_call, session_pw = stall_accounts[1]
    holder = web.WebClient(web_base_url, web_ca, timeout=30)
    holder.hide(session_pw)
    holder.login(session_call, session_pw)

    def login_once():
        client = web.WebClient(web_base_url, web_ca, timeout=30)
        client.hide(login_pw)
        return client.request("POST", "/api/v1/auth/session", json_body={"callsign": login_call, "password": login_pw}, pace=False).status

    def alive():
        return holder.request("POST", "/api/v1/rtc/alive", json_body={"sess_id": DEAD_SESSION}, pace=False).status

    public_client = web.WebClient(web_base_url, web_ca, timeout=30)
    health_client = web.WebClient(web_base_url, web_ca, timeout=30)
    recorders = [Recorder("login", login_once, LOGIN_PERIOD_S, prepare=lambda: web.PACER.wait("login")),
                 Recorder("alive", alive, 0.7),
                 Recorder("public", lambda: public_client.request("GET", "/api/v1/public/state", pace=False).status, 0.5),
                 Recorder("health", lambda: health_client.request("GET", "/health", pace=False).status, 0.5)]
    psi = PsiSampler()
    staller = IoStaller(str(out_dir / "stall"), megabytes=megabytes, seconds=seconds)
    psi.start()
    for rec in recorders:
        rec.start()
    try:
        time.sleep(BASELINE_S)
        staller.start()
        staller.join(seconds + 60.0)
        time.sleep(AFTER_S)
    finally:
        staller.stop()
        records = {rec.kind: rec.stop() for rec in recorders}
        rows = psi.stop()
    logins = records.pop("login")
    probes = [p for kind in ("alive", "public", "health") for p in records[kind]]
    some_ms = None if len(rows) < 2 else round((rows[-1][1] - rows[0][1]) / 1000.0, 1)
    state, lines = verdict(logins, probes, some_ms)
    lines.append("writer: %d MiB in %.1f s%s" % (staller.written_mb, (staller.ended_at or 0) - (staller.started_at or 0),
                                                  "; error " + staller.error if staller.error else ""))
    for line in lines:
        print("[RECORD] WEBSTALL1: %s" % line)
    try:
        (out_dir / "web").mkdir(parents=True, exist_ok=True)
        (out_dir / "web" / "stall.json").write_text(json.dumps({"verdict": state, "lines": lines, "logins": logins, "probes": probes,
                                                                "psi": rows[::max(1, len(rows) // 400)]}, indent=1) + "\n")
    except OSError as exc:
        web.finding("could not write stall.json: %s" % exc)
    if state == "inconclusive":
        inconclusive("; ".join(lines))
    assert state == "pass", "; ".join(lines)
