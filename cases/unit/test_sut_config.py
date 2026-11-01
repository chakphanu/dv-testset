# SPDX-License-Identifier: MIT
"""dvtest/sut_config.py (the dv-router.toml template of DVT_SUT_LAUNCH=native)
and the early refusals of scripts/run.sh for that launch mode."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dvtest import sut_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# a tiny fictional template, not a copy of any real one
TEMPLATE = """\
# fictional template; <net> in lower case is plain text
[node]
bind_ip = "<OWN_IP>"
[[worker]]
host = "<TRANSCODER_IP>"
[[peer]]
host = "<TESTER_IP>"
other = "<NET>.12"
again = "<OWN_IP>"
"""


def test_render_replaces_all_tokens():
    out = sut_config.render(TEMPLATE, "172.30.0")
    assert 'bind_ip = "172.30.0.11"' in out
    assert 'host = "172.30.0.21"' in out
    assert 'host = "172.30.0.30"' in out
    assert 'other = "172.30.0.12"' in out
    assert 'again = "172.30.0.11"' in out
    assert "<net>" in out                       # lower case is not a token
    assert not sut_config.tokens(out)


def test_render_other_instance_and_octets():
    out = sut_config.render(TEMPLATE, "172.30.7", own=111, transcoder=121, tester=130)
    assert 'bind_ip = "172.30.7.111"' in out and 'host = "172.30.7.121"' in out
    assert 'other = "172.30.7.12"' in out


def test_unknown_token_is_an_error():
    with pytest.raises(sut_config.ConfigError) as exc:
        sut_config.render('a = "<OWN_IP>"\nb = "<PEER_IP>"\nc = "<PEER_IP> <ZED>"\n', "172.30.0")
    assert "<PEER_IP>" in str(exc.value) and "<ZED>" in str(exc.value)
    assert "<OWN_IP>" not in str(exc.value).split("(known")[0]


def test_token_names():
    assert sut_config.tokens('<B> <A> <B> <a> <1X> < X> <X_1>') == ["B", "A", "X_1"]


@pytest.mark.parametrize("net", ["", "172.30", "172.30.0.0", "172.30.0.0/24", "172.30.256", "a.b.c", "172.30.0 "])
def test_bad_net(net):
    with pytest.raises(sut_config.ConfigError):
        sut_config.render(TEMPLATE, net)


@pytest.mark.parametrize("octet", [0, 255, -1, 300, "11", True])
def test_bad_octet(octet):
    with pytest.raises(sut_config.ConfigError):
        sut_config.render(TEMPLATE, "172.30.0", own=octet)


def test_render_file(tmp_path):
    src = tmp_path / "t.toml"
    src.write_text(TEMPLATE)
    out = sut_config.render_file(src, tmp_path / "x" / "dv-router.toml", "172.30.0")
    assert out.read_text() == sut_config.render(TEMPLATE, "172.30.0")
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert src.read_text() == TEMPLATE          # the template is never touched


EXTRA_DATA = '[data]\nenable = true\nrequire_ident = <REQUIRE_IDENT>\n'
EXTRA_PEER = '[[edges.xlx.peer]]\ncallsign = "XLX998"\nhost = "<NET>.12"\nmodules = "DE"\n'


def test_require_ident_token_default_and_values(monkeypatch):
    monkeypatch.delenv("DVT_REQUIRE_IDENT", raising=False)
    assert sut_config.render("r = <REQUIRE_IDENT>\n", "172.30.0") == "r = true\n"
    assert sut_config.render("r = <REQUIRE_IDENT>\n", "172.30.0", require_ident="false") == "r = false\n"
    monkeypatch.setenv("DVT_REQUIRE_IDENT", "false")
    assert sut_config.render("r = <REQUIRE_IDENT>\n", "172.30.0") == "r = false\n"
    assert sut_config.render("r = <REQUIRE_IDENT>\n", "172.30.0", require_ident="true") == "r = true\n"
    monkeypatch.setenv("DVT_REQUIRE_IDENT", "")
    assert sut_config.render("r = <REQUIRE_IDENT>\n", "172.30.0") == "r = true\n"


@pytest.mark.parametrize("value", ["TRUE", "yes", "1", "0", "False", " true"])
def test_bad_require_ident(value):
    with pytest.raises(sut_config.ConfigError):
        sut_config.render("x\n", "172.30.0", require_ident=value)


def test_extras_are_appended_in_order_with_tokens(tmp_path):
    out = sut_config.render(TEMPLATE, "172.30.0", require_ident="false", extras=[EXTRA_DATA, EXTRA_PEER])
    assert out.startswith(sut_config.render(TEMPLATE, "172.30.0").rstrip("\n"))
    assert out.index("[data]") < out.index("[[edges.xlx.peer]]")
    assert "require_ident = false" in out and 'host = "172.30.0.12"' in out
    assert not sut_config.tokens(out)
    # an extra with an unknown token is refused like the template
    with pytest.raises(sut_config.ConfigError) as exc:
        sut_config.render(TEMPLATE, "172.30.0", extras=['x = "<WHO>"\n'])
    assert "<WHO>" in str(exc.value)
    # no extras: byte for byte the old result
    assert sut_config.render(TEMPLATE, "172.30.0", extras=[]) == sut_config.render(TEMPLATE, "172.30.0")


def test_split_extras():
    assert sut_config.split_extras("a.toml:b.toml") == ["a.toml", "b.toml"]
    assert sut_config.split_extras(":a.toml::") == ["a.toml"]
    assert sut_config.split_extras("") == [] and sut_config.split_extras(None) == []


def test_render_file_with_extras_and_missing_extra(tmp_path):
    src = tmp_path / "t.toml"
    src.write_text(TEMPLATE)
    one, two = tmp_path / "one.toml", tmp_path / "two.toml"
    one.write_text(EXTRA_DATA)
    two.write_text(EXTRA_PEER)
    out = sut_config.render_file(src, tmp_path / "o.toml", "172.30.0", require_ident="true", extras=[one, two])
    text = out.read_text()
    assert "require_ident = true" in text and 'host = "172.30.0.12"' in text
    assert src.read_text() == TEMPLATE and one.read_text() == EXTRA_DATA
    with pytest.raises(sut_config.ConfigError, match="extra"):
        sut_config.render_file(src, tmp_path / "p.toml", "172.30.0", extras=[tmp_path / "none.toml"])
    assert not (tmp_path / "p.toml").exists()


def test_repo_snippets_render_to_valid_toml(tmp_path):
    import tomllib
    base = TEMPLATE.replace("[[worker]]", "[worker]").replace("[[peer]]", "[peer]")
    snippets = [ROOT / "rig" / "config" / "dv-router" / "data-ident.toml",
                ROOT / "rig" / "config" / "dv-router" / "xlx-peer.toml"]
    for value in ("true", "false"):
        text = sut_config.render(base + "[edges.xlx]\nenable = true\n", "172.30.0", require_ident=value,
                                 extras=[p.read_text() for p in snippets])
        doc = tomllib.loads(text)
        assert doc["data"] == {"enable": True, "require_ident": value == "true", "xlx_text_overs": True}
        assert doc["edges"]["xlx"]["peer"] == [{"callsign": "XLX998", "host": "172.30.0.12", "modules": "DE"}]


def test_missing_template_and_nothing_written(tmp_path):
    with pytest.raises(sut_config.ConfigError):
        sut_config.render_file(tmp_path / "nope.toml", tmp_path / "o.toml", "172.30.0")
    bad = tmp_path / "bad.toml"
    bad.write_text("x = '<WHO>'\n")
    with pytest.raises(sut_config.ConfigError):
        sut_config.render_file(bad, tmp_path / "o.toml", "172.30.0")
    assert not (tmp_path / "o.toml").exists()


def cli(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "dvtest.sut_config", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)


def test_cli_render_and_check_only(tmp_path):
    src = tmp_path / "t.toml"
    src.write_text(TEMPLATE)
    run = cli(str(src), str(tmp_path / "o.toml"), "--net", "172.30.0", "--own", "11", "--transcoder", "21", "--tester", "30")
    assert run.returncode == 0, run.stderr
    assert 'bind_ip = "172.30.0.11"' in (tmp_path / "o.toml").read_text()
    run = cli(str(src), "-", "--net", "172.30.0")
    assert run.returncode == 0 and run.stdout == ""


def test_cli_errors_exit_2(tmp_path):
    src = tmp_path / "t.toml"
    src.write_text('x = "<BOGUS>"\n')
    run = cli(str(src), str(tmp_path / "o.toml"), "--net", "172.30.0")
    assert run.returncode == 2 and "<BOGUS>" in run.stderr and not (tmp_path / "o.toml").exists()
    assert cli(str(tmp_path / "none.toml"), "-", "--net", "172.30.0").returncode == 2
    assert cli(str(src), "-", "--net", "nonsense").returncode == 2


def test_cli_extra_and_require_ident(tmp_path):
    src = tmp_path / "t.toml"
    src.write_text(TEMPLATE)
    one = tmp_path / "one.toml"
    one.write_text(EXTRA_DATA)
    run = cli(str(src), str(tmp_path / "o.toml"), "--net", "172.30.0", "--require-ident", "false", "--extra", str(one))
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "o.toml").read_text().rstrip().endswith("require_ident = false")
    run = cli(str(src), "-", "--net", "172.30.0", "--extra", str(tmp_path / "none.toml"))
    assert run.returncode == 2 and "extra" in run.stderr
    run = cli(str(src), "-", "--net", "172.30.0", "--require-ident", "maybe")
    assert run.returncode == 2 and "require_ident" in run.stderr


# ---- scripts/run.sh refuses a bad native launch before it touches docker

def run_sh(tmp_path, **env):
    full = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "DVT_CONTAINED": "1",
            "DVT_SUT_LAUNCH": "native", "XLXD_SUT_IMAGE": "", "DVT_SUT_CONFIG": "", "TRANSCODER_IMAGE": "x"}
    full.update(env)
    return subprocess.run(["bash", str(ROOT / "scripts" / "run.sh")], cwd=ROOT, env=full,
                          capture_output=True, text=True, timeout=60)


def test_run_sh_native_needs_image(tmp_path):
    cfg = tmp_path / "t.toml"
    cfg.write_text(TEMPLATE)
    run = run_sh(tmp_path, DVT_SUT_CONFIG=str(cfg))
    assert run.returncode == 2 and "needs XLXD_SUT_IMAGE" in run.stderr


def test_run_sh_native_needs_config(tmp_path):
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x")
    assert run.returncode == 2 and "needs DVT_SUT_CONFIG" in run.stderr
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x", DVT_SUT_CONFIG=str(tmp_path / "missing.toml"))
    assert run.returncode == 2 and "not a readable file" in run.stderr


def test_run_sh_native_unknown_token(tmp_path):
    cfg = tmp_path / "t.toml"
    cfg.write_text(TEMPLATE + 'bad = "<NOPE>"\n')
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x", DVT_SUT_CONFIG=str(cfg))
    assert run.returncode == 2 and "<NOPE>" in run.stderr


def test_run_sh_launch_value_checked(tmp_path):
    run = run_sh(tmp_path, DVT_SUT_LAUNCH="bogus")
    assert run.returncode == 2 and "direct, import or native" in run.stderr


@pytest.mark.parametrize("letter", ["M", "N"])
def test_run_sh_native_refuses_unserved_peer_module(tmp_path, letter):
    cfg = tmp_path / "t.toml"
    cfg.write_text(TEMPLATE)
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x", DVT_SUT_CONFIG=str(cfg), DVT_UNSERVED_MODULE=letter)
    assert run.returncode == 2 and "DVT_UNSERVED_MODULE=%s is refused" % letter in run.stderr


def test_run_sh_native_warns_about_ignored_settings(tmp_path):
    cfg = tmp_path / "t.toml"
    cfg.write_text(TEMPLATE)
    # later checks (no such image) end the run with exit 2; the warnings come first
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:dvt-no-such-image", DVT_SUT_CONFIG=str(cfg), DVT_UNSERVED_MODULE="Z",
                 DVT_NXDN="on", DVT_INTERLINK="on", DVT_SUT_USRP="off", DVT_XLXD_BLACKLIST="N0CALL")
    err = run.stderr
    assert "DVT_NXDN is set" in err and "DVT_INTERLINK is set" in err and "DVT_XLXD_BLACKLIST is set" in err
    assert "DVT_SUT_USRP is set" not in err and "DVT_P25 is set" not in err


def test_run_sh_rejects_a_bad_require_ident(tmp_path):
    cfg = tmp_path / "t.toml"
    cfg.write_text(TEMPLATE)
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x", DVT_SUT_CONFIG=str(cfg), DVT_REQUIRE_IDENT="maybe")
    assert run.returncode == 2 and "DVT_REQUIRE_IDENT must be true or false" in run.stderr


def test_run_sh_native_checks_the_extras_before_docker(tmp_path):
    cfg = tmp_path / "t.toml"
    cfg.write_text(TEMPLATE)
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x", DVT_SUT_CONFIG=str(cfg),
                 DVT_SUT_CONFIG_EXTRA=str(tmp_path / "missing.toml"))
    assert run.returncode == 2 and "extra" in run.stderr and "missing.toml" in run.stderr
    bad = tmp_path / "bad.toml"
    bad.write_text('x = "<NOPE>"\n')
    run = run_sh(tmp_path, XLXD_SUT_IMAGE="img:x", DVT_SUT_CONFIG=str(cfg), DVT_SUT_CONFIG_EXTRA=str(bad))
    assert run.returncode == 2 and "<NOPE>" in run.stderr

