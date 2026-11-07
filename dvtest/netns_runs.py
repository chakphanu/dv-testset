# SPDX-License-Identifier: MIT
"""Scheduling and failure reading shared by the netns case files
(cases/test_xlxd_usrp_netns.py, cases/test_xlxd_transcoder_lifecycle.py).

A scenario of those files runs a whole server (a DV-Router instance peaks near
435 tasks) inside a systemd scope whose TasksMax is 1024 (scripts/contained.sh).
Starting every scenario at once exhausts the limit: children fail with
"Resource temporarily unavailable (os error 11)" and the case sees a product
looking "never sent AMBEDPING". So:

- DVT_NETNS_PARALLEL (default 1) is the size of a wave: that many scenarios
  run at the same time, the next wave starts when the previous one is done;
- starvation_message() reads the logs of a scenario and names the environment
  when they show that signature.

Offline, no sockets: cases/unit/test_netns_runs.py.
"""
import os
from pathlib import Path

PARALLEL_ENV = "DVT_NETNS_PARALLEL"

# xlxd.log signatures of an exhausted task limit: clone() fails with EAGAIN
# (the launcher cannot start a child) or a tokio runtime cannot start a worker
# ("OS can't spawn worker thread: Resource temporarily unavailable (os error 11)")
XLXD_LOG_SIGNS = ("Resource temporarily unavailable (os error 11)",
                  "can't spawn worker thread")
# harness.err: the Python harness inside the namespace could not start a thread
HARNESS_SIGNS = ("can't start new thread",)

TAIL_BYTES = 1 << 20            # the signature repeats on every restart: the tail is enough

MESSAGE = ("environment: task limit exhausted (os error 11 / can't spawn worker thread "
           "in xlxd.log); too many scenarios at once for the TasksMax of the scope, "
           "run them in waves of one (%s=1, the default, see "
           "docs/netns-dvrouter-classification.md)" % PARALLEL_ENV)
MESSAGE_HARNESS = ("environment: task limit exhausted (can't start new thread in "
                   "harness.err); run the scenarios in waves of one (%s=1, the default, see "
                   "docs/netns-dvrouter-classification.md)" % PARALLEL_ENV)


def parallel(environ=None):
    """Wave size from DVT_NETNS_PARALLEL: a positive integer, default 1."""
    value = (os.environ if environ is None else environ).get(PARALLEL_ENV, "").strip()
    if not value:
        return 1
    try:
        size = int(value)
    except ValueError:
        size = 0
    if size < 1:
        raise ValueError("%s must be a positive integer, got %r" % (PARALLEL_ENV, value))
    return size


def waves(names, size):
    """names in order, cut into lists of at most size."""
    names = list(names)
    return [names[i:i + size] for i in range(0, len(names), size)]


def run_in_waves(names, launch, collect, size):
    """launch(name) starts one scenario and returns its handle; collect({name:
    handle}) waits for a whole wave and returns {name: result}. At most size
    scenarios are started before the previous wave has been collected. Returns
    {name: result} in the order of names."""
    results = {}
    for wave in waves(names, size):
        results.update(collect({name: launch(name) for name in wave}))
    return results


def selected_names(items, module_name=None):
    """Original test function names of the collected items, only those of the
    module module_name when given."""
    return {getattr(item, "originalname", None) or item.name for item in items
            if module_name is None or getattr(getattr(item, "module", None), "__name__", None) == module_name}


def scenarios_for(scenarios, selected):
    """Names of the scenarios (dicts with a "cases" prefix or tuple of
    prefixes) that at least one selected test reads, in declaration order."""
    wanted = []
    for name, spec in scenarios.items():
        prefixes = spec["cases"]
        prefixes = (prefixes,) if isinstance(prefixes, str) else tuple(prefixes)
        if any(case.startswith(prefixes) for case in selected):
            wanted.append(name)
    return wanted


def for_profile(names, scenarios, sut_profile):
    """names without the scenarios that declare another "profile" (a scenario
    only one profile can judge is not started, and costs no tasks, under the
    others)."""
    return [name for name in names if scenarios[name].get("profile", sut_profile) == sut_profile]


def _tail(path):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL_BYTES))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def starved_text(text, signs=XLXD_LOG_SIGNS):
    return any(sign in text for sign in signs)


def starvation_message(run):
    """The explicit environment failure text when the scenario's logs show an
    exhausted task limit, else None. run is the result dict of a scenario: its
    "run_dir" (xlxd.log is read there) and its "error" text (the tail of
    harness.err when the harness died)."""
    run_dir = run.get("run_dir")
    if run_dir and starved_text(_tail(Path(run_dir) / "xlxd.log")):
        return MESSAGE
    if starved_text(run.get("error") or "", HARNESS_SIGNS + XLXD_LOG_SIGNS):
        return MESSAGE_HARNESS
    return None
