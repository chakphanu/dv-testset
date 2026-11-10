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
    spatial, plain = tb.bodies("firefox")
    bp = fake_bp([None, [spatial, spatial, plain], [spatial, plain]], monkeypatch)
    bp.engine = "firefox"
    assert tb.wait_counts(bp, 2, 1, 30.0) == (2, 1)
    assert tb.wait_counts(bp, 1, 1, 30.0) == (1, 1)
    bp = fake_bp([[plain]], monkeypatch)
    bp.engine = "firefox"
    assert tb.wait_counts(bp, 2, 1, 0.0) == (0, 1)


def test_an_earlier_engines_rows_do_not_count_for_the_next_engine(monkeypatch):
    """The first rig run: the retained locator status of an earlier engine's base came back after its unhide and shared the body, so the counts were 2, 3, 4."""
    first, _ = tb.bodies("chromium")
    mine, plain = tb.bodies("firefox")
    bp = fake_bp([[first, mine, plain, mine]], monkeypatch)
    bp.engine = "firefox"
    assert tb.wait_counts(bp, 2, 1, 0.0) == (2, 1)


def test_one_base_and_one_body_set_per_engine():
    assert set(tb.BASES) == set(tb.SUFFIXES) == {"chromium", "firefox", "webkit"} and len(set(tb.BASES.values())) == 3 and len(set(tb.SUFFIXES.values())) == 3
    assert tb.bodies("webkit") == ("IO91SX/G dph", "Ready dph")
    assert len({tb.bodies(e)[0] for e in tb.SUFFIXES}) == 3 and len({tb.bodies(e)[1] for e in tb.SUFFIXES}) == 3
    assert (Path(__file__).resolve().parent.parent / "test_dp19b_browser.py").read_text(encoding="utf-8").isascii()
