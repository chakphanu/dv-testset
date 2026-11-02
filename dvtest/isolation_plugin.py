# SPDX-License-Identifier: MIT
"""Pytest plugin of the RSS isolation case: command line options that set the DVT_ISOLATION* environment of the test process.

rig/compose.yml forwards only the variables it lists to the tester container, and DVT_ISOLATION* is not among them, so
the options travel as pytest arguments (scripts/run.sh passes everything after `--` to pytest in the container):

  scripts/run.sh -m rig --profile web,webload -- -p dvtest.isolation_plugin --isolation=A cases/test_web_rss_isolation.py

  --isolation=MODE          DVT_ISOLATION (A to F or the mode name)
  --isolation-short         DVT_ISOLATION_SHORT=1 (2 + 3 minute smoke run)
  --isolation-env NAME=VAL  any other DVT_ISOLATION_* setting (SEED, E_LANE, WARMUP_S, MEASURE_S, SAMPLE_S) or DVT_SOAK_MODULES; repeatable

A variable that is already in the environment is replaced by the option. Docs: docs/web-isolation-cases.md.
"""
import os

ALLOWED_PREFIXES = ("DVT_ISOLATION_",)
ALLOWED_NAMES = ("DVT_SOAK_MODULES", "DVT_SOAK_SEED")


def parse_env_option(text):
    """(name, value) of 'NAME=VALUE'; ValueError for anything but a DVT_ISOLATION_* name, DVT_SOAK_MODULES or DVT_SOAK_SEED."""
    name, sep, value = (text or "").partition("=")
    name = name.strip()
    if not sep or not (name.startswith(ALLOWED_PREFIXES) or name in ALLOWED_NAMES):
        raise ValueError("--isolation-env %r: want NAME=VALUE with NAME one of DVT_ISOLATION_*, %s" % (text, ", ".join(ALLOWED_NAMES)))
    return name, value


def apply_options(environ, mode=None, short=False, extra=()):
    """Set the environment from the options; returns the {name: value} it set."""
    wanted = {}
    if mode:
        wanted["DVT_ISOLATION"] = mode
    if short:
        wanted["DVT_ISOLATION_SHORT"] = "1"
    for item in extra or ():
        name, value = parse_env_option(item)
        wanted[name] = value
    environ.update(wanted)
    return wanted


def pytest_addoption(parser):
    group = parser.getgroup("isolation", "RSS isolation experiment (docs/web-isolation-cases.md)")
    group.addoption("--isolation", default=None, metavar="MODE", help="mode A to F (or idle, webrtc-churn, voice, proto-churn, webrtc-voice, voice-x3): DVT_ISOLATION")
    group.addoption("--isolation-short", action="store_true", default=False, help="the 2 + 3 minute smoke run: DVT_ISOLATION_SHORT=1")
    group.addoption("--isolation-env", action="append", default=[], metavar="NAME=VALUE", help="another DVT_ISOLATION_* setting (repeatable)")


def pytest_configure(config):
    try:
        apply_options(os.environ, config.getoption("--isolation"), config.getoption("--isolation-short"), config.getoption("--isolation-env"))
    except ValueError as exc:
        import pytest
        raise pytest.UsageError(str(exc)) from exc
