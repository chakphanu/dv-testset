# SPDX-License-Identifier: MIT
"""What a DV-Router image with FLK15 lets a black-box case assert (DV-Router commit b8d9db72, merged in bc9af2b1, docs data.md "position policy",
fabric.md "link state", config.md "fabric client"; GUARDSYNC1 c1b239d1 shares the resync core and has the log line of the guard records).

Switch: DVT_FLK15=on asserts the cases of cases/test_flk15_policy.py (strict); off (the default) skips them with the reason.

The privacy rule (data.md, "The web process owns persisted flags ..."): the hidden-base-callsign set is owned by dv-svc-web and consumed by the room
controllers, the data workers and the basemap worker. An outage of the policy must HIDE, never leak:

  * a consumer's OWN fabric link loss, and every cold start, withholds the set (unknown) and fails spatial admission closed (denied/13) until a snapshot
    queried after recovery answers; a put received meanwhile does not end the withholding;
  * after a completed reconciliation a snapshot query runs every 5 s, so a lost reconnect put cannot leave an unchanged consumer link stale: a change
    reaches the consumers within 1 + 5 + 1 = 7 s plus scheduling;
  * while web alone is down (the consumers' links stay Up) the consumers keep the last set: no flag can change meanwhile.

What a rig can do (scripts/run.sh header): SIGKILL dv-svc-web (the launcher respawns it, a new `boot`), SIGKILL dv-data-worker, SIGKILL one dv-room by
module letter, read proc-stat of all of them. It cannot cut the fabric session of ONE process (the router runs inside the launcher): the own-link-loss
branch (withheld line, 5 s refresh after an authority-only link loss) is a documented GAP (docs/flk15-cases.md).

This module is pure and stdlib only (clock and sleep injectable; cases/unit/test_flk15_lib.py):

  Sampler        polls a reader about every second and keeps (t, status, rows of a base callsign) so that a leak at ANY instant of an outage is seen
  kill_loop      SIGKILL a process again and again for N seconds (the launcher backs the respawn off 0.5 s doubling to 5 s: one kill is not an outage)
  judge_log      the shape of the operator log lines of FLK15 / GUARDSYNC1 (no callsign, a fixed reason set, withheld followed by reconciled)
  python3 -m dvtest.flk15 out/<run>      the host scan of the SUT logs (they are collected after pytest ends)
"""
import os
import re
import sys
import threading
import time
from collections import namedtuple

BOUND_S = 7.0                       # fabric.md / data.md: 1 s query + 5 s refresh + 1 s in-flight query after an update
SLACK_S = 5.0                       # scheduling, the 10 s hub cadence is NOT in this path (the controller and the worker read the policy directly)
KILL_PERIOD_S = 0.3
REASONS = ("start", "durable change", "own link up")
LOG_NEEDLES = ("position policy", "guard records reconciled", "router link")

PUBLISHED_RE = re.compile(r"dv-svc-web: position policy published \((?P<reason>[^)]*)\): revision (?P<rev>\d+), (?P<n>\d+) hidden accounts\s*$")
WITHHELD_RE = re.compile(r"(?P<program>[a-z][a-z0-9-]*): position policy withheld after link loss (?P<loss>\d+): spatial data denied until reconciled\s*$")
RECONCILED_RE = re.compile(r"(?P<program>[a-z][a-z0-9-]*): position policy reconciled after link loss: revision (?P<rev>\d+), (?P<n>\d+) hidden\s*$")
LINK_DOWN_RE = re.compile(r"(?P<program>[a-z][a-z0-9-]*): fabric: router link down \(loss (?P<loss>\d+)\)\s*$")
LINK_UP_RE = re.compile(r"(?P<program>[a-z][a-z0-9-]*): fabric: router link up again after loss (?P<loss>\d+)\s*$")
GUARD_RE = re.compile(r"(?P<program>[a-z][a-z0-9-]*): guard records reconciled after link loss (?P<loss>\d+): (?P<records>\d+) records from (?P<controllers>\d+) controllers\s*$")


def strict_on(environ=None):
    """DVT_FLK15=on: the image has FLK15, so the cases assert (and fail instead of skipping)."""
    return (environ if environ is not None else os.environ).get("DVT_FLK15", "").lower() in ("on", "1", "true", "yes")


def resync_wait_s(environ=None):
    """Seconds a case waits after a policy change before it offers the next position: the documented bound plus slack (DVT_FLK15_BOUND_S overrides)."""
    raw = (environ if environ is not None else os.environ).get("DVT_FLK15_BOUND_S", "")
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    return value if value > 0 else BOUND_S + SLACK_S


# ================================================================ a sampler that sees a leak at any instant

Sample = namedtuple("Sample", "t status rows")           # status: "ok" | "down" (the reader raised) ; rows: the matching rows of that read


class Sampler:
    """Calls `read()` about every `every_s` seconds on a thread. `read()` returns the list of rows to look at (the anonymous public state's position rows)
    and raises on an unreachable service; `match(row)` selects the rows that must never be there. Context manager; `stop()` ends the thread."""

    def __init__(self, read, match, every_s=1.0, clock=time.monotonic, sleep=time.sleep):
        self.read, self.match, self.every_s = read, match, every_s
        self.clock, self.sleep = clock, sleep
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def step(self):
        """One read (also what the thread calls); the Sample is kept and returned."""
        t = self.clock()
        try:
            rows = [r for r in self.read() if self.match(r)]
            sample = Sample(t, "ok", rows)
        except Exception:                                # an unreachable web service is the outage itself, not a failure of the sampler
            sample = Sample(t, "down", [])
        self.samples.append(sample)
        return sample

    def _run(self):
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.every_s)

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop()
        return False

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5.0)


def leaks(samples):
    """The samples that show a row that must never be public."""
    return [s for s in samples if s.rows]


def outage_window(samples):
    """(first down t, last down t, count) of the "down" samples, or None when the service answered every time (an outage shorter than the cadence is possible)."""
    down = [s.t for s in samples if s.status == "down"]
    return (down[0], down[-1], len(down)) if down else None


def describe_samples(samples):
    ok = sum(1 for s in samples if s.status == "ok")
    window = outage_window(samples)
    return "%d reads (%d answered, %d unreachable%s), %d showed a hidden row" % (
        len(samples), ok, len(samples) - ok, "" if window is None else ", unreachable for %.1f s" % (window[1] - window[0]), len(leaks(samples)))


# ================================================================ kill a process again and again

def kill_loop(kill, seconds, clock=time.time, sleep=time.sleep, period_s=KILL_PERIOD_S):
    """Calls `kill()` every `period_s` for `seconds`; a RuntimeError (error 3: no such process right now, between respawns) is not a failure.
    Returns (kills that landed, t_start, t_end)."""
    landed, t_start = 0, clock()
    while clock() - t_start < seconds:
        try:
            kill()
            landed += 1
        except RuntimeError:
            pass
        sleep(period_s)
    return landed, t_start, clock()


def identities(samples):
    """{(pid, starttime)} of parsed proc-stat samples (dvtest.webload.ProcSample has .pid and .starttime): a process that was NOT restarted keeps its pair."""
    return {(s.pid, s.starttime) for s in samples}


# ================================================================ the operator log lines (SUT logs, host scan)

LogJudgement = namedtuple("LogJudgement", "problems findings published withheld reconciled guard")


def judge_log(text):
    """Judge the FLK15 / GUARDSYNC1 lines of one SUT log.
    Problems: a `position policy` line that does not have the documented shape (so no callsign or other text rides on it), a publication reason outside
    REASONS, a `withheld` line with no later `reconciled` line of the same program, a `router link down (loss N)` with no `up again after loss N`.
    Findings (never a failure): the count of each line kind, a revision that goes DOWN between two publications of the same boot-less sequence is NOT
    judged (a new boot restarts it)."""
    problems, findings = [], []
    published, withheld, reconciled, guard, down, up = [], [], [], [], [], []
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if "position policy" in line:
            m = PUBLISHED_RE.search(line)
            if m:
                published.append((no, m.group("reason"), int(m.group("rev")), int(m.group("n"))))
                if m.group("reason") not in REASONS:
                    problems.append("line %d: publication reason %r is outside %s" % (no, m.group("reason"), list(REASONS)))
                continue
            m = WITHHELD_RE.search(line)
            if m:
                withheld.append((no, m.group("program"), int(m.group("loss"))))
                continue
            m = RECONCILED_RE.search(line)
            if m:
                reconciled.append((no, m.group("program"), int(m.group("rev")), int(m.group("n"))))
                continue
            problems.append("line %d contains 'position policy' but not a documented shape (no callsign may ride on these lines): %s" % (no, line[:160]))
        elif "guard records reconciled" in line:
            m = GUARD_RE.search(line)
            if m:
                guard.append((no, m.group("program"), int(m.group("loss")), int(m.group("records")), int(m.group("controllers"))))
            else:
                problems.append("line %d contains 'guard records reconciled' but not the documented shape: %s" % (no, line[:160]))
        elif "fabric: router link" in line:
            m = LINK_DOWN_RE.search(line)
            if m:
                down.append((no, m.group("program"), int(m.group("loss"))))
                continue
            m = LINK_UP_RE.search(line)
            if m:
                up.append((no, m.group("program"), int(m.group("loss"))))
                continue
            problems.append("line %d contains 'fabric: router link' but not a documented shape: %s" % (no, line[:160]))
    for no, program, loss in withheld:
        if not any(r[1] == program and r[0] > no for r in reconciled):
            problems.append("line %d: %s withheld the position policy after link loss %d and no later 'reconciled' line of %s follows" % (no, program, loss, program))
    for no, program, loss in down:
        if not any(u[1] == program and u[2] == loss and u[0] > no for u in up):
            findings.append("line %d: %s: router link down (loss %d) has no 'up again' line (the process may have exited)" % (no, program, loss))
    findings.append("%d publication line(s), %d withheld, %d reconciled, %d guard-records line(s), %d link down, %d link up" % (
        len(published), len(withheld), len(reconciled), len(guard), len(down), len(up)))
    return LogJudgement(problems, findings, published, withheld, reconciled, guard)


def scan_run(out_dir):
    """(problems, findings, files, judgements) for the SUT logs of a run directory."""
    from dvtest import logscan
    problems, findings, files = [], [], logscan.log_files(out_dir)
    judged = {}
    for log in files:
        with open(log, encoding="utf-8", errors="replace") as fh:
            result = judge_log(fh.read())
        name = os.path.basename(log)
        judged[name] = result
        problems += ["%s: %s" % (name, p) for p in result.problems]
        findings += ["%s: %s" % (name, f) for f in result.findings]
    return problems, findings, files, judged


def main(argv=None, out=None):
    out = out or sys.stdout
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("usage: python3 -m dvtest.flk15 out/<run>   (FLK15: the position policy, link and guard-records log lines of the SUT logs; exit 1 on a line that breaks the shape)", file=out)
        return 2
    problems, findings, files, _judged = scan_run(args[0])
    if not files:
        print("flk15: no SUT log under %s/logs" % args[0], file=out)
        return 2
    print("flk15: %d SUT log file(s)" % len(files), file=out)
    for f in findings:
        print("flk15: FINDING %s" % f, file=out)
    for p in problems:
        print("flk15: FAIL %s" % p, file=out)
    print("flk15: %s" % ("FAIL" if problems else "ok"), file=out)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
