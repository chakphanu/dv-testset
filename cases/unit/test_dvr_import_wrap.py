# SPDX-License-Identifier: MIT
"""scripts/dvr-import-wrap.sh with shell stubs for the converter and dv-router:
the command lines, the TOML hand-over and the fallbacks, offline (no DV-Router
build, no namespace, no network)."""
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WRAP = Path(__file__).resolve().parents[2] / "scripts" / "dvr-import-wrap.sh"
WORDS = ["XLX999", "127.0.0.1", "127.0.0.2"]

# the stub records its argv (one word per line) and the environment it was
# started with; the converter honours --out like the real one-shot mode
CONVERTER = """#!/usr/bin/env bash
rec="${STUB_REC:?}"
if [ "$1" = "--help" ]; then
  echo "usage: conv [--dir DIR] [--out FILE] CALLSIGN OWN_IP TRANSCODER_IP"
  exit 0
fi
printf '%s\\n' "$@" > "$rec/conv.argv"
out=""
while [ "$#" -gt 0 ]; do
  [ "$1" = "--out" ] && out="$2"
  shift
done
[ -z "${STUB_CONV_FAIL:-}" ] || { echo "conv: no good" >&2; exit 1; }
printf '# stub toml\\n[edges.usrp]\\n' > "$out"
"""
CONVERTER_OLD = """#!/usr/bin/env bash
rec="${STUB_REC:?}"
if [ "$1" = "--help" ]; then
  echo "usage: conv --exec [--dir DIR] CALLSIGN OWN_IP TRANSCODER_IP [-- DV_ROUTER [ARGS..]]"
  exit 0
fi
printf '%s\\n' "$@" > "$rec/shim.argv"
"""
ROUTER = """#!/usr/bin/env bash
rec="${STUB_REC:?}"
printf '%s\\n' "$@" > "$rec/router.argv"
printf 'DV_CONFIG_DIR=%s\\nDV_RUN_DIR=%s\\nPWD=%s\\n' "${DV_CONFIG_DIR-unset}" "${DV_RUN_DIR-unset}" "$PWD" > "$rec/router.env"
cfg=""
while [ "$#" -gt 0 ]; do
  [ "$1" = "--config" ] && cfg="$2"
  shift
done
[ -z "$cfg" ] || cp "$cfg" "$rec/router.config"
"""


def _stub(path, text):
    path.write_text(text)
    path.chmod(0o755)


def _run(tmp_path, converter, name="dv-tool-xlxd-import", words=WORDS, bin_dir=None, env_extra=None):
    bins = tmp_path / "bin"
    bins.mkdir(parents=True)
    rec = tmp_path / "rec"
    rec.mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _stub(bins / "dv-router", ROUTER)
    if converter is not None:
        _stub(bins / name, converter)
    env = dict(os.environ, STUB_REC=str(rec), DVT_DVR_BIN_DIR=str(bin_dir or bins))
    env.pop("DV_RUN_DIR", None)
    env.pop("DV_CONFIG_DIR", None)
    env.update(env_extra or {})
    proc = subprocess.run(["bash", str(WRAP)] + list(words), cwd=work, env=env,
                          capture_output=True, text=True, timeout=30)
    return proc, rec, work


def _lines(path):
    return path.read_text().splitlines()


def test_convert_then_config(tmp_path):
    proc, rec, work = _run(tmp_path, CONVERTER)
    assert proc.returncode == 0, proc.stderr
    toml = "%s/dv-router.toml" % work
    # step 1: the one-shot converter, directory = the working directory, output in it
    assert _lines(rec / "conv.argv") == ["--dir", str(work), "--out", toml] + WORDS
    # step 2: the product on that file, the three words as overrides, no --exec, no importer
    assert _lines(rec / "router.argv") == ["--config", toml] + WORDS
    assert (rec / "router.config").read_text() == (work / "dv-router.toml").read_text()
    env = dict(line.split("=", 1) for line in _lines(rec / "router.env"))
    assert env["DV_CONFIG_DIR"] == str(work)      # local id seeds beside the xlxd files
    assert env["DV_RUN_DIR"] == str(work)         # run state per scenario
    assert env["PWD"] == str(work)
    assert "path=convert" in proc.stderr
    assert "--exec" not in proc.stderr
    assert not (rec / "shim.argv").exists()


def test_old_converter_name(tmp_path):
    proc, rec, work = _run(tmp_path, CONVERTER, name="dv-xlxd-import")
    assert proc.returncode == 0, proc.stderr
    assert "path=convert" in proc.stderr
    assert _lines(rec / "conv.argv")[:2] == ["--dir", str(work)]


def test_conversion_failure_stops_before_the_router(tmp_path):
    proc, rec, work = _run(tmp_path, CONVERTER, env_extra={"STUB_CONV_FAIL": "1"})
    assert proc.returncode == 1
    assert "conv: no good" in proc.stderr
    assert not (rec / "router.argv").exists()


def test_fallback_shim_when_converter_has_no_out(tmp_path):
    proc, rec, work = _run(tmp_path, CONVERTER_OLD)
    assert proc.returncode == 0, proc.stderr
    assert "path=shim" in proc.stderr
    assert _lines(rec / "shim.argv") == ["--exec", "--dir", str(work)] + WORDS + ["--", str(tmp_path / "bin" / "dv-router")]
    assert not (rec / "router.argv").exists()


def test_fallback_direct_without_converter(tmp_path):
    proc, rec, work = _run(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert "path=direct" in proc.stderr
    assert _lines(rec / "router.argv") == WORDS
    env = dict(line.split("=", 1) for line in _lines(rec / "router.env"))
    assert env["DV_CONFIG_DIR"] == str(work) and env["DV_RUN_DIR"] == str(work)


def test_usage_and_missing_router(tmp_path):
    proc, rec, work = _run(tmp_path, CONVERTER, words=WORDS[:2])
    assert proc.returncode == 2 and "usage" in proc.stderr
    assert not (rec / "router.argv").exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    proc, rec, work = _run(tmp_path / "y", CONVERTER, bin_dir=empty)
    assert proc.returncode == 2 and "not executable" in proc.stderr


def test_bin_dir_is_required(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DVT_DVR_BIN_DIR"}
    proc = subprocess.run(["bash", str(WRAP)] + WORDS, cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and "DVT_DVR_BIN_DIR" in proc.stderr


def test_extra_toml_is_merged_into_the_converted_file(tmp_path):
    extra = tmp_path / "extra.toml"
    extra.write_text('[edges.nxdn]\nenable = true\ntg_modules = "31672:F"\n[edges.usrp]\nenable = true\n')
    proc, rec, work = _run(tmp_path, CONVERTER, env_extra={"DVT_DVR_EXTRA_TOML": str(extra)})
    assert proc.returncode == 0, proc.stderr
    text = (rec / "router.config").read_text()
    assert "# stub toml" in text and text.count("[edges.usrp]") == 1       # merged key by key, not repeated
    assert "[edges.nxdn]" in text and 'tg_modules = "31672:F"' in text
    import tomllib
    cfg = tomllib.loads(text)
    assert cfg["edges"]["nxdn"]["enable"] is True and cfg["edges"]["usrp"]["enable"] is True
    assert "merging" in proc.stderr and _lines(rec / "router.argv")[0] == "--config"


def test_extra_toml_is_refused_without_the_one_shot_converter(tmp_path):
    extra = tmp_path / "extra.toml"
    extra.write_text("[edges.nxdn]\nenable = true\n")
    proc, rec, work = _run(tmp_path, CONVERTER_OLD, env_extra={"DVT_DVR_EXTRA_TOML": str(extra)})
    assert proc.returncode == 2 and "DVT_DVR_EXTRA_TOML" in proc.stderr
    assert not (rec / "router.argv").exists()
    proc, rec, work = _run(tmp_path / "direct", None, env_extra={"DVT_DVR_EXTRA_TOML": str(extra)})
    assert proc.returncode == 2 and not (rec / "router.argv").exists()
