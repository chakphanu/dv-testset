# SPDX-License-Identifier: MIT
"""Pure helpers of the DIGI1 configuration cases (cases/test_digi1_config.py; docs/digi1-cases.md, "Configuration"): mutations of the snippet
rig/config/dv-router/digi1-roomy.toml that `dv-router config check` must refuse or load. No network, no pytest.

The rules and their exact operator messages are DV-Router's (code a8692074, `crates/lib/config/src/ax25_digi.rs` and `config.md` "Digi key"):

  digi.enable        "digipeating requires effective RF TX on this port (tx = true or \\"simulate\\")"        (also with tx_enable = false)
  digi.enable        "digipeating requires tx_per_min >= 10 and tx_burst >= 3 on this port"
  digi.enable        "ports on the same physical transmitter must all enable or all disable digipeating"
  digi.dedupe_secs   "must be 10-600 seconds"  /  "must be greater than digi.ttl_secs"
  digi.ttl_secs      "the digi TTL must be 1-10 seconds; a later digipeated copy falls outside the 30 s network duplicate window (data 8.3)"
  digi.share         "a digipeating port needs a nonzero digi class share"  /  "a nonzero digi class share requires digi.enable on the ports of this physical transmitter"

  CONFIG_CASES        the mutation table (id, what, key, mutate, hint, verdict)
  render_extras       the snippet texts of one case
  config_problems     problems of a `config check` verdict for one entry
"""
import types

PORT_KEY = "edges.ax25"
M_TX = "digipeating requires effective RF TX on this port"      # the full sentence ends: (tx = true or "simulate")
M_RATE = "digipeating requires tx_per_min >= 10 and tx_burst >= 3 on this port"
M_SHARED = "ports on the same physical transmitter must all enable or all disable digipeating"
M_WINDOW = "must be 10-600 seconds"
M_WINDOW_TTL = "must be greater than digi.ttl_secs"
M_TTL = "the digi TTL must be 1-10 seconds"
M_SHARE_ON = "a digipeating port needs a nonzero digi class share"
M_SHARE_OFF = "a nonzero digi class share requires digi.enable"

CONFIG_CASES = []


def _case(cid, what, key, mutate, hint=None, verdict="reject"):
    CONFIG_CASES.append(types.SimpleNamespace(id=cid, what=what, key=key, mutate=mutate, hint=hint, verdict=verdict))


def _plain(old, new):
    """Substitute a line-anchored snippet text once (the header comments of the snippet repeat the key names, so every anchor starts with a
    newline and the key at the start of its line); ValueError when it is not found exactly once."""
    def f(text):
        if text.count(old) != 1:
            raise ValueError("anchor %r found %d times" % (old, text.count(old)))
        return text.replace(old, new)
    return f


def _chain(*fs):
    def f(text):
        for g in fs:
            text = g(text)
        return text
    return f


_DIGI_ON = "\ndigi = { enable = true }\n"
_digi = lambda body: _plain(_DIGI_ON, "\ndigi = { %s }\n" % body)       # noqa: E731

_case("accept-baseline", "the snippet as shipped loads", None, lambda t: t, verdict="accept")

# ---- effective TX
_case("digi-port-tx-off", "digi.enable with the port's tx = false", "digi.enable", _plain("\ntx = true\n", "\ntx = false\n"), hint=M_TX)
_case("digi-master-tx-off", "digi.enable with [edges.ax25] tx_enable = false", "digi.enable", _plain("\ntx_enable = true\n", "\ntx_enable = false\n"), hint=M_TX)
_case("accept-digi-simulate", "digi.enable on a port with tx = \"simulate\" (the same gates, no RF)", None, _plain("\ntx = true\n", '\ntx = "simulate"\n'), verdict="accept")

# ---- the packet-rate floor (tx_per_min >= 10 and tx_burst >= 3)
_case("digi-per-min-9", "digi.enable with tx_per_min = 9 (the floor is 10)", "digi.enable", _plain("\ntx_per_min = 30\n", "\ntx_per_min = 9\n"), hint=M_RATE)
_case("digi-per-min-2", "digi.enable with the old default tx_per_min = 2 and tx_burst = 1", "digi.enable",
      _chain(_plain("\ntx_per_min = 30\n", "\ntx_per_min = 2\n"), _plain("\ntx_burst = 10\n", "\ntx_burst = 1\n")), hint=M_RATE)
_case("digi-burst-2", "digi.enable with tx_burst = 2 (the floor is 3)", "digi.enable", _plain("\ntx_burst = 10\n", "\ntx_burst = 2\n"), hint=M_RATE)
_case("accept-rate-floor", "tx_per_min = 10 and tx_burst = 3 exactly", None,
      _chain(_plain("\ntx_per_min = 30\n", "\ntx_per_min = 10\n"), _plain("\ntx_burst = 10\n", "\ntx_burst = 3\n")), verdict="accept")
_case("accept-digi-off-low-rate", "digi off keeps the old 2 / 1 defaults valid", None,
      _chain(_digi("enable = false"), _plain("\ntx_per_min = 30\n", "\ntx_per_min = 2\n"), _plain("\ntx_burst = 10\n", "\ntx_burst = 1\n")), verdict="accept")

# ---- the duplicate window and the TTL
_case("digi-dedupe-9", "dedupe_secs = 9 (the window is 10-600 s)", "digi.dedupe_secs", _digi("enable = true, dedupe_secs = 9"), hint=M_WINDOW)
_case("digi-dedupe-601", "dedupe_secs = 601", "digi.dedupe_secs", _digi("enable = true, dedupe_secs = 601"), hint=M_WINDOW)
_case("digi-dedupe-equals-ttl", "dedupe_secs = 10 with ttl_secs = 10 (the window must be greater than the TTL)", "digi.dedupe_secs",
      _digi("enable = true, dedupe_secs = 10, ttl_secs = 10"), hint=M_WINDOW_TTL)
_case("digi-ttl-0", "ttl_secs = 0 (1-10 s)", "digi.ttl_secs", _digi("enable = true, ttl_secs = 0"), hint=M_TTL)
_case("digi-ttl-11", "ttl_secs = 11", "digi.ttl_secs", _digi("enable = true, ttl_secs = 11"), hint=M_TTL)
_case("accept-dedupe-10", "dedupe_secs = 10 with the default ttl_secs = 5", None, _digi("enable = true, dedupe_secs = 10"), verdict="accept")
_case("accept-dedupe-600", "dedupe_secs = 600", None, _digi("enable = true, dedupe_secs = 600"), verdict="accept")
_case("accept-ttl-10", "ttl_secs = 10 with dedupe_secs = 11", None, _digi("enable = true, dedupe_secs = 11, ttl_secs = 10"), verdict="accept")

# ---- the digi class share
_case("digi-share-0", "digi.share = 0 on a digipeating port", "digi.share", _digi("enable = true, share = 0"), hint=M_SHARE_ON)
_case("digi-share-while-off", "a nonzero digi.share with digi.enable = false", "digi.share", _digi("enable = false, share = 4"), hint=M_SHARE_OFF)

# ---- ports of one transmitter agree
_case("digi-shared-physical", "rf-b on the same physical as the digipeating rf-a, digi off", "digi.enable",
      _plain("\ntx = false\n", '\ntx = false\nphysical = "radio1"\n'), hint=M_SHARED)


def render_extras(case, read):
    """The snippet texts to append after the template for one case, in order (pure; `read(name)` returns the text of a snippet of
    rig/config/dv-router): data-ident.toml, then digi1-roomy.toml with the mutation of the case."""
    return [read("data-ident.toml"), case.mutate(read("digi1-roomy.toml"))]


def config_problems(case, verdict, exit_code):
    """Problems ([] = as expected) of a `dv-router config check` verdict (dvtest.tools.config_check.judge_check) for a CONFIG_CASES entry:
    reject = a non-zero exit status and a positioned error under `edges.ax25` whose key (or message) names the field and whose message holds
    DV-Router's sentence; accept = exit status 0."""
    if verdict.get("unsupported"):
        return ["the image has no `config check` (usage text printed)"]
    if case.verdict == "accept":
        return [] if exit_code == 0 and verdict.get("valid") else ["expected to load, got exit status %r: %s" % (exit_code, "; ".join(verdict.get("errors") or ["no error line"]))]
    if exit_code == 0 or verdict.get("valid"):
        return ["SILENT SUCCESS: %s loaded (exit status 0); expected an error naming %s" % (case.what, case.key)]
    parsed = verdict.get("parsed") or []
    if not parsed:
        return ["no `FILE:LINE:COL: message` error line (exit status %r only)" % (exit_code,)]
    mine = [e for e in parsed if PORT_KEY in e["key"] or PORT_KEY in e["message"]]
    if not mine:
        return ["no error names %s (first: %r %r)" % (PORT_KEY, parsed[0]["key"], parsed[0]["message"])]
    named = [e for e in mine if case.key in e["key"] or case.key in e["message"]]
    if not named:
        return ["no error names %r (edges.ax25 errors: %s)" % (case.key, "; ".join("%s: %s" % (e["key"], e["message"]) for e in mine))]
    if case.hint and not any(case.hint in e["message"] for e in named):
        return ["the message does not say %r: %s" % (case.hint, "; ".join(e["message"] for e in named))]
    return []
