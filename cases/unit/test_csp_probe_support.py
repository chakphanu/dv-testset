# SPDX-License-Identifier: MIT
"""The pure parts of the CSP probe (cases/csp_probe_support.py, cases/test_web_csp_probe.py): which directive governs a style attribute,
the violation key of a finding line, the summary, and (when node is installed) a syntax check of the in-page recorder. Offline."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import csp_probe_support as cps  # noqa: E402

pytestmark = pytest.mark.unit

SERVED = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def test_style_governor_follows_the_csp3_fallback_chain():
    assert cps.style_governor(SERVED) == ("default-src", ["'self'"])
    assert cps.style_governor(SERVED + "; style-src 'self'") == ("style-src", ["'self'"])
    assert cps.style_governor(SERVED + "; style-src 'self'; style-src-attr 'unsafe-inline'") == ("style-src-attr", ["'unsafe-inline'"])
    assert cps.style_governor("frame-ancestors 'none'") == (None, [])
    assert cps.style_governor("") == (None, [])


def test_attr_allowed_inline():
    assert not cps.attr_allowed_inline(SERVED)
    assert not cps.attr_allowed_inline(SERVED + "; style-src 'self'")
    assert cps.attr_allowed_inline(SERVED + "; style-src-attr 'unsafe-inline'")
    assert cps.attr_allowed_inline(SERVED + "; style-src 'self' 'unsafe-inline'")
    assert not cps.attr_allowed_inline(SERVED + "; style-src 'self' 'unsafe-inline' 'nonce-abc'")      # a nonce disables unsafe-inline
    assert cps.attr_allowed_inline("frame-ancestors 'none'")                                           # nothing governs: allowed


def test_short_source_and_violation_key():
    assert cps.short_source("https://dvt-web/shared/vendor/htm-preact-3.1.1.module.js?x=1") == "/shared/vendor/htm-preact-3.1.1.module.js"
    assert cps.short_source("") == "-"
    v = {"violatedDirective": "style-src-attr", "effectiveDirective": "style-src-attr", "sourceFile": "https://dvt-web/site/public.js",
         "lineNumber": 12, "columnNumber": 7}
    assert cps.violation_key("route-map", "webkit", v) == ("route-map", "webkit", "style-src-attr", "/site/public.js:12:7")
    assert cps.violation_key("p", "webkit", {})[2:] == ("?", "-:0:0")


def test_summarize_counts_events_and_distinct_pairs():
    v1 = {"violatedDirective": "style-src-attr", "sourceFile": "https://h/a.js", "lineNumber": 1, "columnNumber": 2, "count": 5}
    v2 = {"violatedDirective": "style-src-attr", "sourceFile": "https://h/a.js", "lineNumber": 1, "columnNumber": 2, "count": 2}
    v3 = {"violatedDirective": "script-src-elem", "sourceFile": "", "lineNumber": 0, "columnNumber": 0}
    out = cps.summarize([{"violations": [v1]}, {"violations": [v2, v3]}, {}])
    assert out["events_by_directive"] == {"style-src-attr": 7, "script-src-elem": 1}
    assert out["distinct"] == [("script-src-elem", "-:0:0"), ("style-src-attr", "/a.js:1:2")]
    assert out["any_violation"] is True
    assert cps.summarize([{"violations": []}])["any_violation"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed: the recorder script is not syntax-checked")
def test_probe_script_is_valid_javascript():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.js"
        path.write_text(cps.PROBE_JS, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[:400]
