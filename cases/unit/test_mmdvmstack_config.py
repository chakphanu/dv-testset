# SPDX-License-Identifier: MIT
"""The real client stack (MMDVMHost and its gateways, rig/Dockerfile.mmdvmstack, rig/mmdvmstack-entry.sh,
rig/config/mmdvmstack/*.tmpl). Offline: nothing is built or started. It checks that the templates render with no
placeholder left, that the entry script declares the placeholders the templates use, that the pins are well
formed and are the only MMDVMSTACK_* names the Dockerfile and the fetch script use, that no address but loopback
and the placeholders is in a template, and that the loopback ports of MMDVMHost and of each gateway agree."""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TMPL_DIR = ROOT / "rig/config/mmdvmstack"
ENTRY = ROOT / "rig/mmdvmstack-entry.sh"
DOCKERFILE = ROOT / "rig/Dockerfile.mmdvmstack"
PREFETCH = ROOT / "scripts/pre-fetch-mmdvmstack.sh"
ENV_EXAMPLE = ROOT / ".env.example"

EXPECTED_TEMPLATES = {
    "DExtra_Hosts.txt.tmpl", "DMRGateway.ini.tmpl", "DStarGateway.cfg.tmpl", "MMDVM-Host.ini.tmpl",
    "NXDNGateway.ini.tmpl", "NXDNHosts.json.tmpl", "NXDNHosts.txt.tmpl", "P25Gateway.ini.tmpl",
    "P25Hosts.json.tmpl", "P25Hosts.txt.tmpl", "XLXHosts.txt.tmpl", "YSFGateway.ini.tmpl",
    "YSFHosts.json.tmpl",
}

VALUES = {
    "MODEM_ADDR": "172.30.0.20", "MODEM_PORT": "3334", "LOCAL_ADDR": "172.30.0.100", "LOCAL_PORT": "3335",
    "REFLECTOR_IP": "172.30.0.11", "REFLECTOR_NAME": "XLX999", "CALLSIGN": "N0CALL", "DMR_ID": "1023007",
    "MODULE_DSTAR": "D", "MODULE_DMR": "E", "MODULE_YSF": "F", "P25_TG": "31672", "NXDN_TG": "31672",
    "DUPLEX": "0", "RF_MODE_HANG": "10", "NET_MODE_HANG": "3", "LOG_LEVEL": "2",
    "REFLECTOR_NUM": "999", "DSTARGW_LOG_LEVEL": "info",
}
ENTRY_ENV = {k: v for k, v in VALUES.items()
             if k not in ("DUPLEX", "RF_MODE_HANG", "NET_MODE_HANG", "LOG_LEVEL", "REFLECTOR_NUM", "DSTARGW_LOG_LEVEL")}

PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def templates():
    return sorted(TMPL_DIR.glob("*.tmpl"))


def render(text, values=VALUES):
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def rendered(name):
    return render((TMPL_DIR / (name + ".tmpl")).read_text())


def ini(text):
    """{section: {key: value}} the way the g4klx programs read an ini (comment lines start in column 0)."""
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith("["):
            cur = out.setdefault(line.strip(), {})
            continue
        key, _, value = line.partition("=")
        cur[key.strip()] = value.strip()
    return out


def env_pins():
    pins = dict(re.findall(r"^(MMDVMSTACK_[A-Z0-9_]+)=(\S+)$", ENV_EXAMPLE.read_text(), re.M))
    assert pins, "no MMDVMSTACK_* section in .env.example"
    return pins


# ---- templates -------------------------------------------------------------------------------------------

def test_the_template_set_is_complete():
    assert {t.name for t in templates()} == EXPECTED_TEMPLATES


def test_every_template_renders_with_no_placeholder_left():
    for t in templates():
        text = render(t.read_text())
        assert not re.search(r"@[A-Z][A-Z0-9_]*@", text), t.name
        assert text.endswith("\n"), t.name


def test_json_files_are_json_after_rendering():
    for name in ("YSFHosts.json", "P25Hosts.json", "NXDNHosts.json"):
        data = json.loads(rendered(name))
        assert isinstance(data["reflectors"], list)
    ysf = json.loads(rendered("YSFHosts.json"))["reflectors"][0]
    # every key YSFGateway reads without a default is there
    for key in ("designator", "country", "name", "use_xx_prefix", "user_count", "description", "port", "ipv4", "ipv6"):
        assert key in ysf
    assert ysf["ipv4"] == VALUES["REFLECTOR_IP"] and ysf["port"] == 42000
    # the Startup of YSFGateway.ini is "<country>-<name>" of this entry
    startup = ini(rendered("YSFGateway.ini"))["[Network]"]["Startup"]
    assert startup == f"{ysf['country']}-{ysf['name']}"
    assert len(ysf["description"]) <= 14      # YSFGateway cuts it at 14 characters


def test_host_files_point_at_the_reflector_with_the_real_ports():
    assert rendered("XLXHosts.txt").split("\n")[-2] == "999;172.30.0.11;4001"
    assert rendered("DExtra_Hosts.txt").split("\n")[-2] == "XRF999 172.30.0.11"
    assert rendered("P25Hosts.txt").split("\n")[-2] == "31672 172.30.0.11 41000"
    assert rendered("NXDNHosts.txt").split("\n")[-2] == "31672 172.30.0.11 41400"


def test_the_startup_links_follow_the_placeholders():
    dstar = ini(rendered("DStarGateway.cfg"))
    rpt = dstar["[Repeater_1]"]
    assert rpt["reflector"] == "XRF999 D" and len(rpt["reflector"]) == 8       # name padded to 7 + module
    assert rpt["reflectorAtStartup"] == "true" and rpt["band"] == "B"
    dmr = ini(rendered("DMRGateway.ini"))["[XLX Network]"]
    assert dmr["Startup"] == "999" and dmr["Module"] == "E" and dmr["Port"] == "62030" and dmr["Slot"] == "2"
    assert ini(rendered("P25Gateway.ini"))["[Network]"]["Static"] == "31672"
    assert ini(rendered("NXDNGateway.ini"))["[Network]"]["Static"] == "31672"


def test_ircddb_off_dplus_off_no_downloads_in_dstargateway():
    text = rendered("DStarGateway.cfg")
    sections = ini(text)
    assert not [s for s in sections if s.lower().startswith("[ircddb")]
    assert sections["[DPlus]"]["enabled"] == "false"
    assert sections["[APRS]"]["enabled"] == "false"
    for name in ("[DExtra]", "[DPlus]", "[DCS]", "[XLX]"):
        assert sections[name]["hostfileUrl"] == ""
    assert sections["[Log]"]["fileLevel"] == "none"


def test_mmdvmhost_has_the_virtual_modem_and_all_five_modes():
    host = ini(rendered("MMDVM-Host.ini"))
    modem = host["[Modem]"]
    assert modem["Protocol"] == "udp"
    assert (modem["ModemAddress"], modem["ModemPort"]) == ("172.30.0.20", "3334")
    assert (modem["LocalAddress"], modem["LocalPort"]) == ("172.30.0.100", "3335")
    for mode, net in (("[D-Star]", "[D-Star Network]"), ("[DMR]", "[DMR Network]"),
                      ("[System Fusion]", "[System Fusion Network]"), ("[P25]", "[P25 Network]"),
                      ("[NXDN]", "[NXDN Network]")):
        assert host[mode]["Enable"] == "1", mode
        assert host[net]["Enable"] == "1", net
    for off in ("[POCSAG]", "[FM]", "[POCSAG Network]", "[FM Network]", "[Remote Control]", "[Lock File]",
                "[Transparent Data]", "[CW Id]", "[DMR Trunking]"):
        assert host[off]["Enable"] == "0", off
    assert host["[General]"]["Daemon"] == "0"
    assert host["[General]"]["Callsign"] == "N0CALL" and host["[General]"]["Id"] == "1023007"
    assert host["[MQTT]"]["Host"] == "127.0.0.1"
    assert host["[Log]"]["MQTTLevel"] == "0"


def test_loopback_ports_of_mmdvmhost_and_the_gateways_agree():
    host = ini(rendered("MMDVM-Host.ini"))
    dstar = ini(rendered("DStarGateway.cfg"))
    dmr = ini(rendered("DMRGateway.ini"))["[General]"]
    ysf = ini(rendered("YSFGateway.ini"))["[General]"]
    p25 = ini(rendered("P25Gateway.ini"))["[General]"]
    nxdn = ini(rendered("NXDNGateway.ini"))["[General]"]
    pairs = [
        # (MMDVMHost section, its LocalPort = the gateway's Rpt port, its GatewayPort = the gateway's own port)
        ("[D-Star Network]", dstar["[Repeater_1]"]["port"], dstar["[Gateway]"]["hbPort"]),
        ("[DMR Network]", dmr["RptPort"], dmr["LocalPort"]),
        ("[System Fusion Network]", ysf["RptPort"], ysf["LocalPort"]),
        ("[P25 Network]", p25["RptPort"], p25["LocalPort"]),
        ("[NXDN Network]", nxdn["RptPort"], nxdn["LocalPort"]),
    ]
    for section, rpt_port, gw_port in pairs:
        net = host[section]
        assert net["LocalPort"] == rpt_port, section
        assert net["GatewayPort"] == gw_port, section
        assert net["LocalAddress"] == net["GatewayAddress"] == "127.0.0.1", section
    assert host["[NXDN Network]"]["Protocol"] == nxdn["RptProtocol"] == "Icom"
    for g in (dmr, ysf, p25, nxdn):
        assert g.get("RptAddress", "127.0.0.1") == "127.0.0.1"


def test_no_address_but_loopback_and_the_placeholders_and_no_personal_callsign():
    ipv4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
    host_like = re.compile(r"\b[a-z0-9-]+\.(?:com|org|net|uk|lu|de|io|info|eu|it|nl)\b", re.I)
    callsign_like = re.compile(r"\b[A-Z]{1,2}\d[A-Z]{1,3}\b")
    for t in templates():
        raw = t.read_text()
        assert set(ipv4.findall(raw)) <= {"127.0.0.1"}, t.name
        assert not host_like.findall(raw), t.name
        assert not callsign_like.findall(raw), t.name
        out = render(raw)
        assert set(ipv4.findall(out)) <= {"127.0.0.1", VALUES["MODEM_ADDR"], VALUES["LOCAL_ADDR"],
                                          VALUES["REFLECTOR_IP"]}, t.name
        assert set(callsign_like.findall(out)) <= {"N0CALL"}, t.name
        assert "@" not in out, t.name
    # a template needs the reflector placeholder to name the reflector at all
    assert "@REFLECTOR_IP@" in (TMPL_DIR / "XLXHosts.txt.tmpl").read_text()


def test_no_stray_documentation_ipv4_in_the_other_files():
    ipv4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
    for path in (ENTRY, DOCKERFILE, PREFETCH):
        found = set(ipv4.findall(path.read_text()))
        # the entry script documents the rig defaults; they are private addresses of the rig subnet
        assert all(ip == "127.0.0.1" or ip.startswith("172.30.0.") for ip in found), (path.name, found)


# ---- the entry script ----------------------------------------------------------------------------------------

def entry_declared():
    """{'required': set, 'optional': set, 'derived': set} from the header comment of the entry script."""
    groups, mode = {"required": set(), "optional": set(), "derived": set()}, None
    for line in ENTRY.read_text().splitlines():
        if not line.startswith("#"):
            if mode:
                break
            continue
        low = line.lower()
        if low.startswith("# placeholders") or "set by compose (required)" in low:
            mode = "required"
        elif low.startswith("# optional"):
            mode = "optional"
        elif low.startswith("# derived"):
            mode = "derived"
        m = re.match(r"#\s{2,}@([A-Z][A-Z0-9_]*)@", line)
        if m and mode:
            groups[mode].add(m.group(1))
    return groups


def test_the_entry_script_declares_exactly_the_placeholders_the_templates_use():
    used = set()
    for t in templates():
        used |= set(PLACEHOLDER.findall(t.read_text()))
    declared = entry_declared()
    assert declared["required"] and declared["optional"] and declared["derived"]
    assert declared["required"] | declared["optional"] | declared["derived"] == used
    assert used == set(VALUES)          # and so does this test's value table


def test_the_entry_script_code_agrees_with_its_header():
    text = ENTRY.read_text()
    declared = entry_declared()
    required = set(re.search(r"^REQUIRED=\(([^)]*)\)", text, re.M | re.S).group(1).split())
    assert required == declared["required"]
    optional = set(re.findall(r'^: "\$\{([A-Z_]+):=', text, re.M))
    assert optional == declared["optional"]
    for name in declared["derived"]:
        assert re.search(rf"(^|[\s)]){name}=", text, re.M), name
    names = re.search(r"^NAMES=\((.*)\)$", text, re.M).group(1)
    assert set(re.findall(r"[A-Z_]{3,}", names)) >= declared["derived"] | declared["optional"]


def test_the_entry_script_has_the_supervisor_and_signal_handling():
    text = ENTRY.read_text()
    assert text.splitlines()[1] == "# SPDX-License-Identifier: MIT"
    assert 'trap \'STOPPING_REQUEST=1\' TERM INT' in text
    assert "kill -TERM" in text and "RESTART_DELAY=5" in text
    for name in ("mosquitto", "DMRGateway", "YSFGateway", "P25Gateway", "NXDNGateway", "DStarGateway", "MMDVMHost"):
        assert re.search(rf"^\s*start {name}\b", text, re.M), name
    order = [m.group(1) for m in re.finditer(r"^\s*start (\w+)\b", text, re.M)]
    assert order[0] == "mosquitto" and order[-1] == "MMDVMHost"      # broker first, MMDVMHost after the gateways


def run_entry(tmp_path, env=None, tmpl=TMPL_DIR, args=("render",)):
    run_dir = tmp_path / "run"
    full = {"PATH": "/usr/bin:/bin", "MMDVM_RUN": str(run_dir), "MMDVM_TMPL": str(tmpl),
            "MMDVM_DMRID": str(ROOT / "rig/config/dmrid.dat")}
    full.update(ENTRY_ENV if env is None else env)
    done = subprocess.run(["bash", str(ENTRY), *args], env=full, capture_output=True, text=True, timeout=30)
    return done, run_dir


def test_entry_render_matches_the_python_rendering(tmp_path):
    done, run_dir = run_entry(tmp_path)
    assert done.returncode == 0, done.stderr
    for t in templates():
        out = run_dir / t.name[:-len(".tmpl")]
        assert out.read_text() == render(t.read_text()), t.name
    ids = (run_dir / "DMRIds.dat").read_text().splitlines()
    assert ids[0].count("\t") == 2 and ";" not in ids[0]          # the ';' file turned into tab separated
    assert f"{VALUES['DMR_ID']}\tN0CALL\tdvt" in ids
    assert "listener 1883 127.0.0.1" in (run_dir / "mosquitto.conf").read_text()


@pytest.mark.parametrize("name,value", [
    ("MODULE_DSTAR", "d"), ("MODULE_DMR", "AB"), ("MODULE_YSF", "1"), ("REFLECTOR_IP", "xlx999.example"),
    ("P25_TG", "70000"), ("NXDN_TG", "0"), ("MODEM_PORT", "x"), ("CALLSIGN", "N0 CALL"), ("REFLECTOR_NAME", "XLX"),
    ("DMR_ID", ""),
])
def test_entry_refuses_odd_values(tmp_path, name, value):
    env = dict(ENTRY_ENV)
    env[name] = value
    done, _ = run_entry(tmp_path, env)
    assert done.returncode == 2, (name, done.stderr)
    assert "mmdvmstack:" in done.stderr


def test_entry_refuses_a_missing_variable(tmp_path):
    env = dict(ENTRY_ENV)
    del env["MODULE_DMR"]
    done, _ = run_entry(tmp_path, env)
    assert done.returncode == 2 and "MODULE_DMR is required" in done.stderr


def test_entry_fails_loudly_on_an_unreplaced_placeholder(tmp_path):
    bad = tmp_path / "tmpl"
    bad.mkdir()
    (bad / "X.ini.tmpl").write_text("[General]\nCallsign=@CALLSIGN@\nOops=@NOT_A_PLACEHOLDER@\n")
    done, _ = run_entry(tmp_path, tmpl=bad)
    assert done.returncode == 2
    assert "unreplaced placeholder" in done.stderr and "NOT_A_PLACEHOLDER" in done.stderr


def test_entry_optional_values_can_be_overridden(tmp_path):
    env = dict(ENTRY_ENV, DUPLEX="1", RF_MODE_HANG="2", NET_MODE_HANG="1", LOG_LEVEL="1")
    done, run_dir = run_entry(tmp_path, env)
    assert done.returncode == 0, done.stderr
    host = ini((run_dir / "MMDVM-Host.ini").read_text())
    assert host["[General]"]["Duplex"] == "1" and host["[General]"]["RFModeHang"] == "2"
    assert host["[General]"]["NetModeHang"] == "1" and host["[Log]"]["DisplayLevel"] == "1"
    assert ini((run_dir / "DStarGateway.cfg").read_text())["[Log]"]["displayLevel"] == "debug"


# ---- pins, Dockerfile, fetch script ---------------------------------------------------------------------------

def test_the_pins_are_forty_hex_and_the_repos_are_https_github():
    pins = env_pins()
    commits = {k: v for k, v in pins.items() if k.endswith("_COMMIT")}
    repos = {k: v for k, v in pins.items() if k.endswith("_REPO")}
    assert len(commits) == len(repos) == 6
    for k, v in commits.items():
        assert re.fullmatch(r"[0-9a-f]{40}", v), k
        assert k[:-len("_COMMIT")] + "_REPO" in repos, k
    for k, v in repos.items():
        assert re.fullmatch(r"https://github\.com/(g4klx|F4FXL)/[A-Za-z0-9_.-]+", v), k
    assert re.fullmatch(r"[a-z0-9.:/_-]+", pins["MMDVMSTACK_BASE_IMAGE"])
    assert len(set(commits.values())) == 6


def test_dockerfile_and_fetch_script_use_only_defined_names():
    defined = set(env_pins())
    for path in (DOCKERFILE, PREFETCH):
        used = set(re.findall(r"MMDVMSTACK_[A-Z0-9_]+", path.read_text()))
        assert used <= defined, (path.name, used - defined)
    fetch = PREFETCH.read_text()
    for name in defined - {"MMDVMSTACK_BASE_IMAGE"}:
        assert name in fetch, name
    # the fetch script's error text builds MMDVMSTACK_${label}_REPO: every label is a real name stem
    labels = re.findall(r"^fetch_one (\w+) ", fetch, re.M)
    assert len(labels) == 6
    for label in labels:
        assert f"MMDVMSTACK_{label}_REPO" in defined and f"MMDVMSTACK_{label}_COMMIT" in defined, label
    assert "MMDVMSTACK_BASE_IMAGE" in DOCKERFILE.read_text()


def test_dockerfile_contexts_match_the_fetch_directories():
    docker = DOCKERFILE.read_text()
    fetch = PREFETCH.read_text()
    contexts = dict(re.findall(r"--build-context (\w+)=(\S+)", docker))
    assert set(contexts) == {"mmdvmhost", "dmrgw", "ysfclients", "p25clients", "nxdnclients", "dstargw"}
    for path in contexts.values():
        assert path.startswith(".cache/mmdvmstack/")
        assert path[len(".cache/"):] in fetch, path
    used = set(re.findall(r"^COPY --from=(\w+) ", docker, re.M))
    assert used - {"build"} == set(contexts)
    assert docker.splitlines()[0] == "# SPDX-License-Identifier: MIT"
    assert "docker build -f rig/Dockerfile.mmdvmstack" in docker
    assert "-t dvt-mmdvmstack:local rig" in docker


def test_dockerfile_final_stage_is_small_and_non_root():
    docker = DOCKERFILE.read_text()
    final = docker[docker.rindex("\nFROM "):]
    assert "--uid 10005" in final and "USER mmdvm" in final
    assert 'ENTRYPOINT ["/mmdvm/entry.sh"]' in final
    assert "COPY config/mmdvmstack/ /mmdvm/tmpl/" in final
    # no compiler and no dev package in the final image
    assert not re.search(r"\b(g\+\+|gcc|make|libboost|nlohmann|libmosquitto-dev|libcurl)", final.replace("libcurl.so.4", ""))
    # the build stage links the curl stand-in, not libcurl
    build = docker[:docker.rindex("\nFROM ")]
    assert "libcurl4" not in build and "libcurl-dev" not in build
    assert "-Werror" not in build.split("CPPFLAGS=", 1)[1].split('"')[1]


def test_shim_files_exist_and_carry_the_four_calls():
    header = (ROOT / "rig/mmdvmstack/shim/curl/curl.h").read_text()
    stub = (ROOT / "rig/mmdvmstack/shim/curl-stub.c").read_text()
    for fn in ("curl_easy_init", "curl_easy_setopt", "curl_easy_perform", "curl_easy_cleanup"):
        assert fn in header and fn in stub
    assert "SPDX-License-Identifier: MIT" in header.splitlines()[0]
    assert "SPDX-License-Identifier: MIT" in stub.splitlines()[0]


def test_new_files_carry_the_licence_line_and_scripts_are_executable():
    for path in (ENTRY, PREFETCH):
        assert path.read_text().splitlines()[1] == "# SPDX-License-Identifier: MIT", path.name
        assert path.stat().st_mode & 0o111, path.name
    for t in templates():
        if t.name.endswith(".json.tmpl"):
            continue                        # JSON has no comments
        assert t.read_text().splitlines()[0] == "# SPDX-License-Identifier: MIT", t.name
