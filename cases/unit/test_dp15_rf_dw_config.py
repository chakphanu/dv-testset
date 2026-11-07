# SPDX-License-Identifier: MIT
"""The shipped pieces of the DP15 RF tier B rig (two real Dire Wolf TNCs on a modem loop): the DV-Router snippet
rig/config/dv-router/dp15-rf-dw.toml after the real merge (both transmit shapes), the compose services dw-a / dw-b, the run.sh
profile, the pure helpers of cases/test_dp15_rf_dw.py and the guard that keeps tier A off a tier B config. Offline."""
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp15_rf as tier_a  # noqa: E402
import test_dp15_rf_dw as tier_b  # noqa: E402

from dvtest import ax25, limits, sut_config  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'
DW_A, DW_B = 48, 49


def rendered(tx, with_out):
    names = ["data-ident.toml", "dp15-rf-dw.toml"] + (["dp15-rf-out.toml"] if with_out else [])
    extras = [(SNIPPETS / name).read_text() for name in names]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", require_ident="true", extras=extras, dp15_tx_enable=tx))


def test_snippet_points_the_ports_at_the_two_tncs_receive_only():
    cfg = rendered("false", False)
    ax = cfg["edges"]["ax25"]
    assert ax["enable"] is True and ax["tx_enable"] is False and ax["callsign"] == "N0TEST-1" and ax["alias_calls"] == []
    ports = {p["name"]: p for p in ax["port"]}
    assert set(ports) == {"rf-a", "rf-b"}
    assert (ports["rf-a"]["kind"], ports["rf-a"]["host"], ports["rf-a"]["port"], ports["rf-a"]["tx"]) == ("kiss-tcp", "172.30.0.%d" % DW_A, 8001, False)
    assert (ports["rf-b"]["kind"], ports["rf-b"]["host"], ports["rf-b"]["port"], ports["rf-b"]["tx"]) == ("kiss-tcp", "172.30.0.%d" % DW_B, 8001, False)
    assert [p["name"] for p in cfg["data"]["path"]] == ["rf-in"]


def test_snippet_with_the_token_true_and_the_out_path_transmits_on_rf_a_only():
    cfg = rendered("true", True)
    ax = cfg["edges"]["ax25"]
    ports = {p["name"]: p for p in ax["port"]}
    assert ax["tx_enable"] is True and ports["rf-a"]["tx"] is True and ports["rf-b"]["tx"] is False
    assert ports["rf-a"]["tx_per_min"] == 2 and ports["rf-a"]["tx_burst"] == 1 and ports["rf-a"]["tx_budget"] == {}
    out = [p for p in cfg["data"]["path"] if p["to"] == "edge:ax25/rf-a"]
    assert [p["name"] for p in out] == ["rf-out"] and out[0]["addressee"] == "XLX999B"


def test_ports_and_paths_match_tier_a_so_that_the_split_files_work_unchanged():
    a = tomllib.loads(sut_config.render(BASE, "172.30.0", require_ident="true", dp15_tx_enable="true",
                                        extras=[(SNIPPETS / "data-ident.toml").read_text(), (SNIPPETS / "dp15-rf.toml").read_text()]))
    b = rendered("true", False)
    strip = lambda cfg: [{k: v for k, v in p.items() if k not in ("host", "port")} for p in cfg["edges"]["ax25"]["port"]]    # noqa: E731
    assert strip(a) == strip(b)
    assert a["data"]["path"] == b["data"]["path"]


def test_compose_services_have_free_fixed_addresses_the_loop_and_the_hardening():
    compose = (ROOT / "rig" / "compose.yml").read_text()
    used = re.findall(r'ipv4_address: "\$\{DVT_NET:-172\.30\.0\}\.(\d+)"', compose)
    for octet in (DW_A, DW_B):
        assert used.count(str(octet)) == 1, "address .%d used %d times: %s" % (octet, used.count(str(octet)), sorted(used, key=int))
    a = compose.split("\n  dw-a: &dw\n", 1)[1].split("\n  dw-b:\n", 1)[0]
    b = compose.split("\n  dw-b:\n", 1)[1].split("\n\n", 1)[0]
    for block in (a, b):
        assert "limits-small" in block or "<<: *dw" in block
        assert "ports:" not in block
    assert "profiles: [dw]" in a and "read_only: true" in a and "cap_drop: [ALL]" in a and "no-new-privileges" in a and "- /tmp" in a
    assert 'command: ["loop"]' in a and "image: dvt-direwolf:local" in a and "<<: *limits-small" in a
    assert "DW_PEER: ${DVT_NET:-172.30.0}.%d:7355" % DW_B in a and "DW_PEER: ${DVT_NET:-172.30.0}.%d:7355" % DW_A in b
    assert '${DVT_NET:-172.30.0}.%d"' % DW_A in a and '${DVT_NET:-172.30.0}.%d"' % DW_B in b
    assert "DVT_DW: ${DVT_DW:-off}" in compose and "DVT_DW_A: ${DVT_NET:-172.30.0}.%d" % DW_A in compose
    assert "      - ${DVT_NET:-172.30.0}.46\n" in compose and "dvt-aprsc:local" in compose           # the neighbours stay where they were
    assert not re.search(r"^\s+ports:", a + b, re.M)


def test_compose_config_is_capped_when_docker_compose_is_there():
    done = subprocess.run(["docker", "compose", "-p", "dvt", "-f", str(ROOT / "rig/compose.yml"), "--env-file", str(ROOT / ".env.example"),
                           "--profile", "dw", "config", "--format", "json"], capture_output=True, timeout=60)
    if done.returncode != 0:
        pytest.skip("docker compose config is not available")
    import json
    services = json.loads(done.stdout)["services"]
    assert {"dw-a", "dw-b"} <= set(services)
    problems = [p for p in limits.check_services({"services": {k: services[k] for k in ("dw-a", "dw-b")}})]
    assert problems == []
    for name in ("dw-a", "dw-b"):
        assert services[name]["networks"].keys() == {"rig"} and services[name]["read_only"] is True


def test_run_sh_knows_the_profile():
    run = (ROOT / "scripts/run.sh").read_text()
    assert "--profile aprs --profile dw --profile asl-lab" in run
    assert re.search(r"^    dw\)\n(?:.*\n){1,4}      export DVT_DW=on", run, re.M)
    assert "rig/config/dv-router/dp15-rf-dw.toml" in run and "scripts/pre-fetch-direwolf.sh" in run and "--profile dw needs DVT_SUT_LAUNCH=native" in run
    assert "aprs, dw or a comma list" in run
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts/run.sh")]).returncode == 0


def test_probe_frames_are_accepted_by_the_edge_and_stay_off_the_room():
    for seq, src in ((1, tier_b.PROBE_SRC), (102, "N0TPRC")):
        frame = tier_b.probe_frame(seq, src=src)
        assert ax25.dvrouter_accepts_frame(frame)
        parsed = ax25.parse_frame(frame)
        assert parsed["info"].startswith(b":%-9s:" % tier_b.PROBE_TO.encode()) and tier_b.ROOM_NAME.encode() not in parsed["info"][:11]
        base = src.split("-")[0]
        assert len(base) == 6 and any(c.isdigit() for c in base) and base.isupper()
        assert base != tier_a.RF_SRC.split("-")[0]               # the room text bucket is per base callsign: probes must not eat the case's budget


def test_tier_b_shares_the_names_of_tier_a():
    assert tier_b.MODULE == tier_a.MODULE == "B" and tier_b.ROOM_NAME == "XLX999B" and tier_b.STATION == "N0TEST-1"
    assert tier_b.PAUSE_S >= 4.0 and tier_b.LOOP_WAIT_S > 10.0           # the deafness of a TNC after its own transmission (docs/direwolf-cases.md)


def test_tier_a_skips_a_config_that_points_at_the_dire_wolf_tncs(monkeypatch, tmp_path):
    """The rf fixture of tier A would listen on 8101 / 8102 for an edge that connects to the TNCs: it must skip, not fail."""
    cfg = sut_config.render(BASE, "172.30.0", require_ident="true", dp15_tx_enable="false",
                            extras=[(SNIPPETS / "data-ident.toml").read_text(), (SNIPPETS / "dp15-rf-dw.toml").read_text()])
    out = tmp_path / "xlxd-config" / "xlxd-sut"
    out.mkdir(parents=True)
    (out / "dv-router.toml").write_text(cfg)
    monkeypatch.setenv("DVT_DW_A", "172.30.0.%d" % DW_A)
    target = type("T", (), {"out": str(tmp_path)})()
    ax, _paths = tier_a.read_rf_config(target)
    assert tier_a.dw_config(ax) is True
    plain = tomllib.loads(sut_config.render(BASE, "172.30.0", require_ident="true", dp15_tx_enable="false",
                                            extras=[(SNIPPETS / "data-ident.toml").read_text(), (SNIPPETS / "dp15-rf.toml").read_text()]))
    assert tier_a.dw_config(plain["edges"]["ax25"]) is False
