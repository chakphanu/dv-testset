# SPDX-License-Identifier: MIT
"""docs/coverage-matrix.md must not rot: every case id it names still exists,
every row is well formed and the summary table matches the rows
(scripts/coverage-check.py). Offline, no rig."""
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts" / "coverage-check.py"


def _run(*args):
    return subprocess.run([sys.executable, str(CHECK), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def test_matrix_ids_exist_and_summary_is_current():
    r = _run()
    assert r.returncode == 0, "coverage matrix problems:\n" + r.stdout + r.stderr


def test_matrix_names_case_ids():
    r = _run("--list")
    assert r.returncode == 0, r.stderr
    ids = r.stdout.split()
    assert len(ids) > 100, "the matrix names suspiciously few case ids: %d" % len(ids)
    assert all("::" in i for i in ids)
