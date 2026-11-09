# SPDX-License-Identifier: MIT
"""cases/asl_edge_support.py: the E-circuit guard of the ASL T3 case."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asl_edge_support as aes  # noqa: E402

pytestmark = pytest.mark.unit


class Fake:
    def __init__(self, up_after_restart=True, up_at_start=False):
        self.restarts, self.waits, self.lines = 0, [], []
        self.up_at_start, self.up_after_restart = up_at_start, up_after_restart

    def wait(self, timeout):
        self.waits.append(timeout)
        up = self.up_after_restart if self.restarts else self.up_at_start
        if not up:
            raise AssertionError("asl-e: no ESTABLISHED (IN) link to 2999 after %.0f s:\nrpt lstats ..." % timeout)

    def restart(self):
        self.restarts += 1


def test_circuit_already_up_does_not_restart():
    f = Fake(up_at_start=True)
    assert aes.ensure_circuit(f.wait, f.restart, f.lines.append, "T3 E") == "up"
    assert f.restarts == 0 and f.waits == [30.0]
    assert len(f.lines) == 1 and "path=up" in f.lines[0]


def test_missing_circuit_restarts_the_edge_once_and_waits_again():
    f = Fake(up_after_restart=True)
    assert aes.ensure_circuit(f.wait, f.restart, f.lines.append, "T3 E") == "restarted"
    assert f.restarts == 1 and f.waits == [30.0, 30.0]
    assert "path=restart" in f.lines[0] and "no ESTABLISHED" in f.lines[0] and "path=restarted" in f.lines[1]


def test_circuit_missing_after_the_restart_raises():
    f = Fake(up_after_restart=False)
    with pytest.raises(aes.CircuitMissing) as exc:
        aes.ensure_circuit(f.wait, f.restart, f.lines.append, "T3 E", timeout=5)
    assert f.restarts == 1 and f.waits == [5, 5]
    assert "path=missing" in f.lines[-1] and "5 s" in str(exc.value)


def test_recorder_prints_and_appends(tmp_path, capsys):
    rec = aes.recorder(tmp_path, "T3", clock=lambda fmt: "12:00:00")
    rec("guard: path=up")
    assert "[RECORD] T3: guard: path=up" in capsys.readouterr().out
    assert (tmp_path / "asl-lab" / "guards.txt").read_text() == "12:00:00 T3: guard: path=up\n"


def test_recorder_survives_an_unwritable_dir(tmp_path, capsys):
    blocker = tmp_path / "asl-lab"
    blocker.write_text("a file, not a directory")
    aes.recorder(tmp_path, "T3")("x")
    assert "[RECORD] T3: x" in capsys.readouterr().out
