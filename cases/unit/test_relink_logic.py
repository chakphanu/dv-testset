# SPDX-License-Identifier: MIT
"""dvtest/relink.py: the restore decision and the SUT state text."""
import pytest

from dvtest import relink

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("relayed,reachable,waited,want", [
    (True, True, False, "none"),
    (True, True, True, "none"),
    (False, True, False, "wait"),
    (False, True, True, "restart"),
    (True, False, False, "restart"),     # container gone: restart even if the last probe relayed
    (False, False, False, "restart"),
    (False, False, True, "restart"),
])
def test_restore_action(relayed, reachable, waited, want):
    assert relink.restore_action(relayed, reachable, waited) == want


def test_state_text():
    assert "running" in relink.sut_state_text(True)
    text = relink.sut_state_text(False, "link failed")
    assert "NOT answer" in text and "not visible" in text and text.endswith("last error: link failed")
