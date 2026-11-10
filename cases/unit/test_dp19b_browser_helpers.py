# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp19b_browser.py against fakes of the Playwright objects. Needs the playwright package for the import of the case module only (the tester
image has it; skipped elsewhere)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api", reason="the playwright Python package is only in the tester image")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_dp19b_browser as tb  # noqa: E402

pytestmark = pytest.mark.unit


class Inner:
    def __init__(self, texts):
        self.texts = texts

    def all_inner_texts(self):
        return list(self.texts)


class Section:
    def __init__(self, texts):
        self.texts = texts

    def count(self):
        return 1 if self.texts is not None else 0

    def locator(self, css):
        return Inner(self.texts or [])


def fake_bp(sequence, monkeypatch):
    state = {"i": 0}

    class Page:
        def locator(self, css):
            return Section(sequence[min(state["i"], len(sequence) - 1)])

    monkeypatch.setattr(tb.time, "sleep", lambda s: state.__setitem__("i", state["i"] + 1))
    return SimpleNamespace(page=Page())


def test_wait_counts_follows_the_panel_until_the_wanted_counts(monkeypatch):
    bp = fake_bp([None, [tb.BODY_SPATIAL, tb.BODY_SPATIAL, tb.BODY_PLAIN], [tb.BODY_SPATIAL, tb.BODY_PLAIN]], monkeypatch)
    assert tb.wait_counts(bp, 2, 1, 30.0) == (2, 1)
    assert tb.wait_counts(bp, 1, 1, 30.0) == (1, 1)
    bp = fake_bp([[tb.BODY_PLAIN]], monkeypatch)
    assert tb.wait_counts(bp, 2, 1, 0.0) == (0, 1)


def test_one_base_per_engine_and_the_shared_body():
    assert set(tb.BASES) == {"chromium", "firefox", "webkit"} and len(set(tb.BASES.values())) == 3
    assert tb.BODY_SPATIAL == "IO91SX/G dpe" and tb.BODY_PLAIN == "Ready dpe"
    assert (Path(__file__).resolve().parent.parent / "test_dp19b_browser.py").read_text(encoding="utf-8").isascii()
