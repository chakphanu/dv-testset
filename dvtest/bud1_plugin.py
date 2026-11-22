# SPDX-License-Identifier: MIT
"""The session hook of BUD1 (dvtest/bud1.py): with DVT_BUD1=on and the control channel (DVT_RIG_CONTROL=1, set by scripts/run.sh) the tester samples the hub metrics of the SUT (and of
`dvr-peer` with DVT_TWONODE=on) every DVT_BUD1_EVERY_S seconds into out/<run>/bud1/samples.jsonl and at the end of the session writes bud1/final-<service>.prom. Wired in cases/conftest.py
(pytest_sessionstart / pytest_sessionfinish); it never fails a run: an unreachable hub is counted and written as a record without data. The verdict is `python3 -m dvtest.bud1 out/<run>`."""
import json
import os
import threading
import time
from pathlib import Path

from dvtest import bud1, metrics

_state = {}


def enabled(env=None):
    env = os.environ if env is None else env
    return bud1.strict_on(env) and env.get("DVT_RIG_CONTROL") == "1"


def services(env=None):
    env = os.environ if env is None else env
    return ["xlxd-sut"] + (["dvr-peer"] if env.get("DVT_TWONODE", "").lower() == "on" else [])


def _number(env, name, default):
    try:
        value = float((env or os.environ).get(name) or default)
    except ValueError:
        value = default
    return value if value > 0 else default


class Hook:
    """The sampler. `fetch(service)` returns the hub text (default: RigControl.metrics); `clock` and `sleep` are injectable."""

    def __init__(self, out_dir, fetch, service_names, every_s=bud1.EVERY_S, settle_s=bud1.SETTLE_S, clock=time.time, sleep=time.sleep):
        self.dir = Path(out_dir) / bud1.SUBDIR
        self.fetch, self.services, self.every_s, self.settle_s = fetch, list(service_names), every_s, settle_s
        self.clock, self.sleep = clock, sleep
        self.errors = 0
        self._last_kill = {}
        self._stop = threading.Event()
        self._thread = None

    def sample(self, final=False):
        """One read of every service; a line per service in samples.jsonl. Returns the texts read (service -> text)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        texts = {}
        for service in self.services:
            record = {"t": round(self.clock(), 1), "service": service, "final": bool(final)}
            try:
                text = self.fetch(service)
                texts[service] = text
                record.update(bud1.summarize(metrics.parse(text)))
            except Exception as exc:                          # the hub may be gone (a restart case); not a failure of the run
                self.errors += 1
                record["error"] = str(exc)[:100]
            with open(self.dir / bud1.SAMPLES, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        return texts

    def pre_kill(self, service, process, keep_s=30.0):
        """Before the FIRST kill of a process in a burst: wait one telemetry period (+1 s: the process has put its last record, dv-router answered 2026-11-22, Q10: no refused-body
        counter survives a restart) and sample, so a refusal of the incarnation about to die is on record. Kills of the same process within `keep_s` of the last one (a kill loop) pass at once."""
        now = self.clock()
        key = (service, process)
        if now - self._last_kill.get(key, -1e9) > keep_s:
            self.sleep(self.settle_s)
            self.sample()
        self._last_kill[key] = self.clock()

    def _run(self):
        while not self._stop.wait(self.every_s):
            self.sample()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def finish(self):
        """Stop the sampler, wait one telemetry period for the last records, write the final snapshots."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5.0)
        self.sleep(self.settle_s)
        texts = self.sample(final=True)
        for service, text in texts.items():
            with open(self.dir / (bud1.FINAL_PREFIX + service + bud1.FINAL_SUFFIX), "w", encoding="utf-8") as fh:
                fh.write(text)
        return texts


def session_start(config):
    if getattr(config.option, "collectonly", False) or not enabled():
        return
    from dvtest.control import RigControl
    out = os.environ.get("DVT_OUT") or "out"
    control = RigControl(Path(out) / "control")
    hook = Hook(out, lambda service: control.metrics(service), services(), every_s=_number(None, "DVT_BUD1_EVERY_S", bud1.EVERY_S),
                settle_s=_number(None, "DVT_BUD1_SETTLE_S", bud1.SETTLE_S))
    _state["hook"] = hook
    # the pre-kill sample waits DVT_BUD1_SETTLE_S before the first kill of a process: only with DVT_BUD1_KILLWAIT=on, so that standing
    # cases that time a restart from the kill keep their timing when DVT_BUD1=on samples a whole chain
    if os.environ.get("DVT_BUD1_KILLWAIT", "").lower() in ("on", "1", "true", "yes"):
        RigControl.kill_hooks.append(hook.pre_kill)
    hook.start()


def session_finish(config):
    hook = _state.pop("hook", None)
    if hook is not None:
        from dvtest.control import RigControl
        if hook.pre_kill in RigControl.kill_hooks:
            RigControl.kill_hooks.remove(hook.pre_kill)
        try:
            hook.finish()
        except Exception:                                      # never turn a finished run into an error
            pass
