# SPDX-License-Identifier: MIT
"""The shipped pieces of the private aprsc (DP15-5a / 5b): the DV-Router snippet rig/config/dv-router/dp15-aprsis.toml parsed
after the real merge, the fixture config rig/config/aprsc/aprsc.conf, the pins, the compose service, the run.sh profile, and the
pure checks of cases/test_dp15_aprsis.py. Offline (the compose check needs `docker compose config`, it skips without)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp15_aprsis as case  # noqa: E402

from dvtest import aprs_passcode, aprsis_client as ai, limits, sut_config  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'
APRSC_OCTET = 47


def rendered():
    extras = [(SNIPPETS / "data-ident.toml").read_text(), (SNIPPETS / "dp15-aprsis.toml").read_text()]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", require_ident="true", extras=extras))


def test_snippet_points_the_edge_at_the_private_aprsc_and_logs_in_as_the_dvr_fixture():
    cfg = rendered()
    edge = cfg["edges"]["aprsis"]
    assert edge["enable"] is True
    assert edge["server"] == "172.30.0.%d" % APRSC_OCTET and edge["port"] == 14580
    assert edge["callsign"] == aprs_passcode.DVR_LOGIN
    assert edge["passcode_file"] == "/etc/dv-router/aprs-secrets/passcode"
    assert edge["filter_extra"] == "" and edge["tocall"] == "APZDVR"
    # valid_station of DV-Router: base <= 6 characters with a digit and an upper-case letter
    base = aprs_passcode.base_call(edge["callsign"])
    assert len(base) <= 6 and any(c.isdigit() for c in base) and any(c.isupper() for c in base)
    # the tester's login must differ from the edge's (a second verified login with the same name kicks the first)
    assert edge["callsign"] != case.SENDER and case.EDGE_LOGIN == edge["callsign"]


def test_snippet_carries_the_path_row_with_the_edge_enable():
    cfg = rendered()
    rows = [p for p in cfg["data"]["path"] if p["from"] == "edge:aprsis"]
    assert rows == [{"name": "aprsis-down", "from": "edge:aprsis", "to": "room:B", "types": ["text"],
                     "addressee": case.ROOM_NAME, "per_min": 30}]
    assert case.ROOM_NAME == "XLX999B" and case.MODULE == "B"
    assert cfg["data"]["enable"] is True


def test_snippet_merges_after_the_web_snippets_and_dp15_paths():
    extras = [(SNIPPETS / name).read_text() for name in
              ("web.toml", "admin.toml", "totp-required.toml", "data-ident.toml", "dp15-paths.toml", "dp15-aprsis.toml")]
    cfg = tomllib.loads(sut_config.render(BASE, "172.30.0", require_ident="true", extras=extras))
    assert {p["name"] for p in cfg["data"]["path"]} == {"dp15-b-to-c", "aprsis-down"}
    assert cfg["edges"]["aprsis"]["enable"] is True


def test_server_address_is_the_compose_address_and_free_of_the_other_fixed_ones():
    compose = (ROOT / "rig" / "compose.yml").read_text()
    block = compose.split("\n  aprsc:\n", 1)[1].split("\n\n", 1)[0]
    assert '${DVT_NET:-172.30.0}.%d"' % APRSC_OCTET in block
    used = re.findall(r'ipv4_address: "\$\{DVT_NET:-172\.30\.0\}\.(\d+)"', compose)
    assert used.count(str(APRSC_OCTET)) == 1, "address .%d is used more than once: %s" % (APRSC_OCTET, sorted(used, key=int))
    assert "      - ${DVT_NET:-172.30.0}.46\n" in compose                 # the T5n alias address stays where it was
    assert "DVT_APRSC: ${DVT_NET:-172.30.0}.%d" % APRSC_OCTET in compose and "DVT_APRS: ${DVT_APRS:-off}" in compose
    assert "profiles: [aprs]" in block and "read_only: true" in block and "cap_drop: [ALL]" in block
    assert "ports:" not in block and "limits-small" in block


def test_fixture_config_is_standalone_with_one_filter_port():
    lines = [ln.split("#", 1)[0].strip() for ln in (ROOT / "rig/config/aprsc/aprsc.conf").read_text().splitlines()]
    keys = [ln.split()[0].lower() for ln in lines if ln]
    for want in ("serverid", "passcode", "myadmin", "myemail", "rundir", "listen"):
        assert want in keys
    for banned in ("uplink", "uplinkbind", "peergroup", "httpstatus", "httpupload", "magicbadness", "upstreamretrydelay"):
        assert banned not in keys, banned
    listens = [ln for ln in lines if ln.lower().startswith("listen")]
    assert len(listens) == 1 and "igate tcp 0.0.0.0 14580" in listens[0] and "udp" not in listens[0]
    assert re.search(r"^ServerId\s+%s\s*$" % case.SERVER_ID, (ROOT / "rig/config/aprsc/aprsc.conf").read_text(), re.M)


def test_pins_and_dockerfile():
    env = dict(re.findall(r"^(APRSC_[A-Z_]+)=(\S+)$", (ROOT / ".env.example").read_text(), re.M))
    assert set(env) == {"APRSC_REPO", "APRSC_COMMIT", "APRSC_BASE_IMAGE"}
    assert re.fullmatch(r"[0-9a-f]{40}", env["APRSC_COMMIT"]) and env["APRSC_REPO"].startswith("https://")
    docker = (ROOT / "rig/Dockerfile.aprsc").read_text()
    assert re.search(r"^USER aprsc$", docker, re.M) and "ARG APRSC_BASE_IMAGE" in docker and "--disable-is2" in docker
    assert "COPY --from=src src /src/src" in docker and "aprsc.conf" in docker and "-y" in docker
    fetch = (ROOT / "scripts/pre-fetch-aprsc.sh").read_text()
    assert "APRSC_COMMIT" in fetch and ".cache/aprsc" in fetch and os.access(ROOT / "scripts/pre-fetch-aprsc.sh", os.X_OK)


def test_run_sh_knows_the_profile_and_mounts_the_passcode_directory():
    run = (ROOT / "scripts/run.sh").read_text()
    assert "--profile nat --profile aprs --profile dw --profile asl-lab" in run
    assert re.search(r"^    aprs\)\n(?:.*\n){1,4}      export DVT_APRS=on", run, re.M)
    assert "python3 -m dvtest.aprs_passcode --dvr" in run and 'chmod 600 "$out/aprs-secrets/passcode"' in run
    assert "aprs-secrets:/etc/dv-router/aprs-secrets:ro" in run
    assert "rig/config/dv-router/dp15-aprsis.toml" in run and "DVT_SUT_LAUNCH=native" in run
    assert "nat, aprs, dw or a comma list" in run
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts/run.sh")]).returncode == 0


def test_compose_service_is_capped_in_its_profile():
    if not shutil.which("docker"):
        pytest.skip("docker is not installed")
    cmd = ["docker", "compose", "-p", "dvtaprs", "-f", str(ROOT / "rig/compose.yml"), "--env-file", str(ROOT / ".env.example"),
           "--profile", "aprs", "config", "--format", "json"]
    run = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, TRANSCODER_IMAGE="example.invalid/t:none"))
    if run.returncode != 0:
        pytest.skip("docker compose config unavailable: %s" % run.stderr.strip()[:200])
    services = json.loads(run.stdout)["services"]
    assert set(services) == {"aprsc"}
    svc = services["aprsc"]
    assert limits.check_services({"services": services}) == []
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"] and "ports" not in svc and set(svc["networks"]) == {"rig"}
    assert svc["networks"]["rig"]["ipv4_address"] == "172.30.0.%d" % APRSC_OCTET


# ---------------------------------------------------------------- pure checks of the case file

ACK = "XLX999B>APZDVR,TCPIP*,qAS,N0TEST-5::N0TEST-7 :ack51"


def ack_check(line, tmp_path):
    target = SimpleNamespace(out=str(tmp_path))
    case.check_ack_line(target, "t", ai.parse_line(line), "51")


def test_check_ack_line_accepts_the_documented_shape(tmp_path):
    ack_check(ACK, tmp_path)
    ack_check(ACK.replace("qAS", "qAO"), tmp_path)             # the token is the server's choice


@pytest.mark.parametrize("line, why", [
    (ACK.replace("TCPIP*,qAS,N0TEST-5", "TCPIP*,qAC,DVTEST,qAS,N0TEST-5"), "two q constructs"),
    (ACK.replace("TCPIP*,qAS", "qAR,N0TEST-5,TCPIP*,qAS"), "a q construct DV-Router wrote before TCPIP*"),
    (ACK.replace(",qAS,N0TEST-5", ""), "no q construct (the server must add one)"),
    (ACK.replace("XLX999B>", "XLX999>"), "source is not the room name"),
    (ACK.replace("APZDVR", "APRS"), "wrong tocall"),
    (ACK.replace(":ack51", ":ack52"), "wrong id"),
    (ACK.replace("N0TEST-7 :", "N0TEST-7:"), "addressee not padded to 9"),
])
def test_check_ack_line_rejects_other_shapes(line, why, tmp_path):
    with pytest.raises(AssertionError):
        ack_check(line, tmp_path)


def test_acks_for_counts_only_the_acks_of_the_message():
    class Fake:
        def matching(self, pred, since=0):
            lines = [ACK, ACK.replace("ack51", "ack52"), ACK.replace("XLX999B>", "N0TEST-1>")]
            return [(float(i), p) for i, p in enumerate(ai.parse_line(x) for x in lines) if pred(p)]
    assert [round(at) for at, _p in case.acks_for(Fake(), "51")] == [0]
    assert [round(at) for at, _p in case.acks_for(Fake(), "52")] == [1]


def test_repeat_window_sits_between_the_server_and_the_controller_windows():
    assert case.SERVER_DUPE_S < case.REPEAT_AFTER_S < case.CONTROLLER_DUPE_S
    assert case.REPEAT_AFTER_S - case.SERVER_DUPE_S >= 5 and case.CONTROLLER_DUPE_S - case.REPEAT_AFTER_S >= 5
    assert case.MSGID_5A != case.MSGID_5B
