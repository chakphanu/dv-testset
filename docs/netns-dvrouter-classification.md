<!-- SPDX-License-Identifier: MIT -->
# netns cases against DV-Router: classification of the red results

Question from the DV-Router lead: for every netns case that was red against
DV-Router (image `wip-0348b98`), is it (a) xlxd behaviour DV-Router does not
have by design (then the expectation `netns_ambed` / `netns_lifecycle` in
`dvtest/specs/dvrouter.py` changes), or (b) DV-Router contradicting its own
design or losing audio (a product finding)?

Answer: neither. Every one of the 20 cases is green when its scenario runs
alone. The red results came from the harness starting all scenarios at once,
which exhausts the task limit of the `contained.sh` scope and keeps
DV-Router's child processes from starting. There is no class (a) change and no
class (b) finding in this pass. The report itself changed no case, library or spec; the
harness fixes H1 to H3 and the coverage item H4 below were applied afterwards (status at "Harness fixes" and "H4 applied").

Scope: `cases/test_xlxd_usrp_netns.py` (13 cases that run, plus
`test_no_usrp_link_bytes_equal_reference`, skipped without `DVT_XLXD_REF_BIN`)
and `cases/test_xlxd_transcoder_lifecycle.py` (7 netns cases). Design reference:
`docs/design/transcoding.md` of the DV-Router tree (the file
`docs/design/codec.md` named in the request does not exist; the codec worker
contract, "dv-codec-ambed" and "Lifecycle" sections, are in `transcoding.md`).
Worker source: `crates/codec/ambed/src/link.rs`, `lib.rs`.

## Root cause of the common symptom

`xlxd never sent AMBEDPING to the transcoder` (`get()` in both files,
`cases/test_xlxd_usrp_netns.py:201`, `cases/test_xlxd_transcoder_lifecycle.py:308`)
is the harness's `pinged` flag, set when the fake transcoder receives its first
`AMBEDPING` (`dvtest/proto/ambed.py:399`, `FakeAmbed.connected`).

1. The detector is fine. Run alone, the worker pings at once: `dv-codec-ambed`
   starts at about 0.5 s, the first `AMBEDPING` arrives at 0.57 s from
   `127.0.0.1` to `127.0.0.2:10100`, `AMBEDCAPS` follows in the same
   millisecond (PONG -> probe), the next PING 5.0 s later
   (`out/20261107T074907Z/netns/no_usrp/result.json`, `ctrl`). That is the
   design: "`AMBEDPING` every 5 s in every state, the first one at process
   start" (`transcoding.md:432`; `link.rs:152-154`, `PING_EVERY` = 5 s in
   `lib.rs:56`). Not on demand, not from another port.
2. The module fixture `runs` of `test_xlxd_usrp_netns.py` (line 187-188) starts
   all five scenarios at the same time, whatever `-k` selects (the lifecycle
   file selects by test, lines 284-287; run as a whole file it starts four).
3. One idle DV-Router scenario (launcher, 8 rooms, edges, `dv-codec-ambed`,
   status, web, ...) peaks at 435 tasks (threads) in the scope (sampled
   `TasksCurrent` of `dvt-test.scope`, scenario `extended`, alone). Five at
   once need about 2200 against `TasksMax=1024`.
4. Over the limit `clone()` fails with EAGAIN, so the launcher cannot start its
   children and the tokio runtimes of those that did start panic. Signature in
   `xlxd.log` of the red runs (for example
   `out/20261107T074220Z/netns/extended/xlxd.log`, 129 and 40 hits):
   `dv-router: dv-codec-ambed: cannot start .../dv-codec-ambed: Resource temporarily unavailable (os error 11)`,
   `dv-router: dv-codec-ambed: restarting in 500 ms`,
   `thread 'main' panicked at .../tokio-1.54.1/src/runtime/scheduler/multi_thread/worker.rs:515:13: OS can't spawn worker thread: Resource temporarily unavailable (os error 11)`.
   The worker never reaches the fake transcoder in the 15 s `pinged` wait, so
   `pinged` is false and `opens` / `ctrl` are empty. The launcher's retry with
   backoff is the documented behaviour (`crates/router/src/children.rs:560`,
   `:616`; `transcoding.md` "launcher restarts a failed process with 0.5-5 s
   backoff"), not a defect.
5. Which cases of a starved run pass is luck (a worker that started in time).
   That is why `test_legacy_ambed_single_caps_probe` alone passed in the
   earlier parallel results and the other twelve did not.

The same applies to the three lifecycle cases that were also red on
`wip-1590f26` and `dvr-85ccacfd` (below): the stale run directories
`out/20261107T035321Z/netns-lifecycle/*` and `out/20261107T035644Z/netns-lifecycle/*`
(four scenarios started together) carry 13 to 96 "Resource temporarily
unavailable" lines each, and in one the Python harness itself could not start a
thread. I did not re-create the starved configuration for this report (one
scenario at a time was the rule); the red-run evidence is from those existing
directories.

## Run recipe that avoids it (one scenario at a time)

Lifecycle file: select the cases of one scenario with `-k`; only the scenarios
of the selected cases start (`test_lost_transcoder_` -> `lost_and_back`,
`test_lost_listener_` -> `listener_lost_and_back`, `test_fast_swap_` ->
`fast_swap`, `test_legacy_swap_` -> `legacy_swap`):

```
DVT_DVR_BIN_DIR=.cache/dvr-<tag> DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns \
  cases/test_xlxd_transcoder_lifecycle.py -q -k test_lost_listener_
```

USRP file: SUPERSEDED by H1 (applied): `-k` now launches only the scenarios of the selected
cases, so the scratch plugin below is no longer needed; it is kept as the record of how the
results above were made. Before H1, `-k` did not reduce the scenarios and a scratch plugin
OUTSIDE the repository (here
`$SCRATCH/plug/onescen.py`) that drops the other scenarios after collection:

```
# onescen.py
import os
def pytest_collection_finish(session):
    keep = os.environ.get("DVT_ONE_SCEN")
    if not keep:
        return
    for item in session.items:
        sc = getattr(item.module, "SCENARIOS", None)
        if sc is not None:
            for k in list(sc):
                if k != keep:
                    del sc[k]
            return
```

```
PYTHONPATH=$SCRATCH/plug DVT_ONE_SCEN=extended DVT_DVR_BIN_DIR=.cache/dvr-<tag> \
  DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -p onescen -m netns \
  cases/test_xlxd_usrp_netns.py -q -k test_extended
```

Scenario names: `no_usrp`, `legacy_ambed`, `extended`, `one_output`, `slow_open`, and (H4, DV-Router profile only) `slow_open_listener`.
Never run two scenarios in the same scope (435 tasks each, scope limit 1024),
never run a netns job next to another heavy job. A scenario takes 5 to 12 s
(USRP file) or 20 to 66 s (lifecycle).

## Table

Results below: image binaries `wip-0348b98` (`.cache/dvr-wip-0348b98`), each
scenario alone, spec unchanged. Run directories are under `out/` (git-ignored).
Class "environment" = task limit exhausted by the harness's parallel launch,
"harness" = the harness lets that happen unnoticed (fix H1 to H3).

### cases/test_xlxd_usrp_netns.py

| Case | First assertion line when red | Root cause | Class | Evidence (alone: pass) | Proposed fix |
|------|-------------------------------|------------|-------|------------------------|--------------|
| test_no_usrp_sends_no_caps | xlxd never sent AMBEDPING to the transcoder | five scenarios started together, worker not started (os error 11) | environment + harness | `20261107T074907Z/netns/no_usrp`: PING 0.57 s, one CAPS (spec `ambed_open` = extended_always, count 1); `transcoding.md:432` | H1 + H3, no spec change |
| test_no_usrp_legacy_open | same | same | environment + harness | same run: `opens == []` matches `no_usrp_opens: []` (open on first voice only for a demanded profile, `transcoding.md:329`, `:339`) | H1 + H3, no spec change |
| test_legacy_ambed_single_caps_probe | (passed in the parallel run: the worker of this scenario started in time) | none | pass | `20261107T074913Z/netns/legacy_ambed`: CAPS at 0.55 s and 5.55 s, gap 5.00 s (spec 5.0 +- 0.3, `transcoding.md:432`, `:454`) | none |
| test_legacy_ambed_digital_uses_legacy_open | xlxd never sent AMBEDPING to the transcoder | environment | environment + harness | same run: no OPEN at all, so no legacy 19 B OPEN (`transcoding.md:406`, `:463`: never a legacy OPEN) | H1 + H3, no spec change |
| test_legacy_ambed_pcm_not_transcoded | same | environment | environment + harness | same run: no PCM open, 0 USRP packets | H1 + H3 |
| test_extended_single_caps_probe | same | environment | environment + harness | `20261107T074923Z/netns/extended`: one CAPS | H1 + H3 |
| test_extended_opens | same | environment | environment + harness | same run: opens `[24,1,[5]]` at 2.56 s and `[24,5,[1]]` at 6.04 s, equal to `extended_opens` (on-demand outputs for the other profile only, `transcoding.md:339`), two CS | H1 + H3 |
| test_extended_dstar_to_usrp | same | environment | environment + harness | same run: 50 keyed USRP frames with the transcoder PCM, unkey last | H1 + H3 |
| test_extended_usrp_to_digital | same | environment | environment + harness | same run: 322 B PCM requests >= 50 | H1 + H3 |
| test_extended_no_echo_to_usrp_peer | same | environment | environment + harness | same run: nothing echoed in the USRP step | H1 + H3 |
| test_extended_bad_frame_dropped | same | environment | environment + harness | same run: no open after the TLV frame, router alive | H1 + H3 |
| test_one_output_opens | same | environment | environment + harness | `20261107T074936Z/netns/one_output`: `[24,1,[5]]` twice, distinct transactions (spec `one_output_opens`) | H1 + H3 |
| test_slow_open_short_burst_delivered | same | environment | environment + harness | `20261107T074947Z/netns/slow_open`: `opens == []`, as in `slow_open_opens: []`; see coverage note H4 | H1 + H3 (+ H4) |
| test_no_usrp_link_bytes_equal_reference | skip | no `DVT_XLXD_REF_BIN` | not run | skipped in every run | none |

### cases/test_xlxd_transcoder_lifecycle.py

Red mode (from the only starved lifecycle runs in `out/`, four scenarios together
on `wip-1590f26`, `20261107T035321Z/netns-lifecycle/`): the listener scenario's
own harness died (`harness.err`: `RuntimeError: can't start new thread` in
`listen("B")`, so there is no `result.json` and the cases see the `error` text);
`fast_swap` ran, but the router's children failed to start (`dv-room[Y]` /
`dv-room[Z]: cannot start ... os error 11`, 66 restarts) and the legacy fake
never received the extended OPEN (`events` empty), so the case sees
"ignored extended opens [] ... expected 1". The assertion lines below are
derived from those result files and the case code; I did not re-create a starved
run.

| Case | Assertion when red | Root cause | Class | Evidence (alone, `wip-0348b98`: pass) | Proposed fix |
|------|--------------------|------------|-------|----------------------------------------|--------------|
| test_lost_transcoder_caps_probed_again | xlxd never sent AMBEDPING to the transcoder, or a missing CAPS (starved children) | task limit exhausted | environment + harness | `20261107T075009Z/netns-lifecycle/lost_and_back`: first fake CAPS at 0.55 s, second fake probed at 45.6 s (first PONG after the 40 s loss; `transcoding.md:433`, `:454`) | H2 + H3 |
| test_lost_transcoder_pcm_resumes | same | task limit exhausted | environment + harness | same run: second fake pinged and probed, no open without a listener (`pcm_opens: []`) | H2 + H3 |
| test_lost_transcoder_dstar_to_usrp_resumes | same | task limit exhausted | environment + harness | same run: open `[24,1,[5]]` at 51.08 s after the reconnect, keyed PCM frames to the USRP peer (`transcoding.md:339`) | H2 + H3 |
| test_lost_listener_pcm_opens | `error` text: `RuntimeError: can't start new thread` (listener harness) | task limit exhausted; red also on `wip-1590f26` and `dvr-85ccacfd` because the same starved launch was used | environment + harness | alone it passes on all three builds, `listener_lost_and_back`: `20261107T075104Z` (`wip-0348b98`), `20261107T075629Z` (`wip-1590f26`), `20261107T075745Z` (`dvr-85ccacfd`); open `[24,5,[1]]` at 3.06 s before and 52.14 s after the loss = spec `pcm_listener_opens` (demand = AMBE+ for a D-STAR listener on the USRP module, `transcoding.md:327`, `:339`) | H2 + H3 |
| test_lost_listener_heard | same | same | environment + harness | alone passes on the same three builds; `20261107T075200Z`: 50 of 51 listener frames voiced before the loss and 50 of 51 after the reconnect (>= 45 needed) | H2 + H3 |
| test_fast_swap_caps_probed_after_failed_open | ignored extended opens [] (D-STAR step ...), expected 1 | children failed to start; no extended OPEN reached the legacy fake; red also on the two older builds when run together | environment + harness | alone passes on all three builds: `20261107T075256Z` (`wip-0348b98`): ignored extended OPEN at 16.05 s, CAPS at 17.11 s (+1.06 s) and 17.61 s = "OPEN 1000 ms + 50 ms, then re-probe, retry 500 ms later" (`transcoding.md:461`, spec `swap_caps_reprobe` within 1.2 s + 0.3 s); older builds `20261107T075725Z`, `20261107T075841Z` also pass | H2 + H3 |
| test_legacy_swap_recovers | (passed in the earlier results too) | none | pass | `20261107T075316Z/netns-lifecycle/legacy_swap`: legacy fake gets 0 OPENs, CAPS after each PONG (45.6 s, 50.6 s), extended back at 60.6 s and opens `[24,1,[5]]` at 62.61 s (`transcoding.md:433`, `:463`) | none |

Not in the table: `test_rig_transcoder_lost_and_back_usrp_resumes` (same file) needs the
docker rig and was not part of this task.

## Class (a): spec adjustments

None. Every expectation of `netns_ambed` and `netns_lifecycle` matches what
`wip-0348b98` does and what `transcoding.md` describes (extended OPEN only, opens
on first voice for demanded profiles only, PING every 5 s, CAPS after a PONG
while not ready, re-probe 1.05 s after an unanswered OPEN). Nothing to relax or
re-point.

## Class (b): product findings

None. In particular the two cases the request names as examples were looked
at:

- lost listener not heard: `test_lost_listener_heard` passes alone (50 of 51 D-STAR
  frames carry the transcoder AMBE+ before the loss and after the reconnect).
- slow open dropping a short burst: not exercised by the `slow_open` scenario (no digital
  listener, so the spec and the product correctly open nothing). Exercised since H4 by
  `slow_open_listener` (D-STAR listener on the module): the burst is delivered in full, see
  "H4 applied" below. No finding.

## Environment observation for DV-Router (not a defect against its design)

One idle instance with 8 module rooms peaks at 435 tasks. It is documented
behaviour that the launcher restarts a child that fails to start; under task
exhaustion all that is visible is `cannot start ...: Resource temporarily
unavailable` and the backoff. Relevant for the tracker item on thread counts
(THREADS1): the number to bring down is 435 per instance; on a 1024-task scope
two instances already leave no headroom.

## H4 applied: short burst over a slow OPEN with a D-STAR listener

Decision of the DV-Router lead: keep `slow_open` (no listener, DV-Router asserts "no OPEN") and
add a second variant WITH a listener, asserted on real behaviour.

What was added (`cases/test_xlxd_usrp_netns.py`, `dvtest/specs/dvrouter.py`,
`dvtest/netns_runs.py`):

- scenario `slow_open_listener`: as `slow_open` (fake transcoder, STD delayed 1.0 s, 5-frame USRP
  burst on module B) plus step `listenB` first (a D-STAR listener linked on module B, 1 s settle
  for the demand token, as `listener_lost_and_back`). Case prefix `test_open_wait_`, chosen so that
  `-k test_slow_open_` and `-k test_open_wait_` select their own scenario only (offline test
  `test_slow_open_variants_are_selected_apart`). The scenario carries `profile="dvrouter"`:
  `netns_runs.for_profile()` drops it under any other `DVT_SUT_PROFILE`, so the xlxd reference run
  neither launches nor judges it (case skipped, no scenario started; B12 for xlxd stays
  `test_slow_open_short_burst_delivered`, unchanged).
- case `test_open_wait_listener_short_burst_heard`, spec entry `netns_ambed.slow_open_listener`.
  Every run writes `observed.json` (times on the fake's clock) into the scenario directory.

Design rule asserted (DV-Router `docs/design/transcoding.md`; wip tree):

| Line | Rule | Assertion |
|------|------|-----------|
| :339, :344 | the OPEN starts with the first voice unit, "no added timer or lookahead" | OPEN seen 0 to 0.3 s after the first USRP frame; opens == `[[24, 5, [1]]]` (demand = AMBE+ of the listener) |
| :344, `xcode.rs` `close_stream` | before SessionReady up to 128 units are buffered with no age drop; EndSession of a stream closed while opening waits for SessionReady; the buffer is then sent in order | the listener hears all 5 frames; the fake gets 5 PCM requests, PCM unchanged |
| :510 (table) | a slow STD below 1050 ms is a normal open | the open succeeds, STD about 1.0 s after the OPEN |
| :347 | target StreamEnd follows the outputs of the last input (or 400 ms after close) | one end frame after the 5 frames, one stream id, sequence numbers increasing |
| :341, :344 | controller give-up 3000 ms after the first voice unit | last frame heard within 3.0 s of the first USRP frame; nothing heard before the STD |

The design does not allow audio loss for a 5-unit burst when the STD arrives inside the 1050 ms
OPEN wait: the 1.0 s STD delay leaves 50 ms of margin on the OPEN wait, but the burst itself is
only 5 units, far below the 128-unit buffer, so the case asserts "all 5" (no loss bound needed).
A longer burst (above 128 units, about 2.5 s) or an STD after 1050 ms is a different case (drops
the oldest units / refused open, `transcoding.md:344`, `:472`) and is not covered here (see H5 and
H6 below).

Observations on `wip-0348b98` (alone, `DVT_NETNS_PARALLEL` default, two runs, times in s on the
fake's clock; run directories `out/20261107T114540Z/netns/slow_open_listener` and
`out/20261107T114604Z/netns/slow_open_listener`, first one below):

| Item | Value |
|------|-------|
| USRP frames sent | 5 (first 5.546, last 5.626, unkey 5.626) |
| OPEN `[24, 5, [1]]` seen | 5.549 (2.8 ms after the first frame; second run 2.2 ms) |
| STD sent by the fake | 6.549 (1.000 s after the OPEN) |
| fake data requests | 5 x 322 B, all at 6.55 (flushed together right after the STD), then CS at 6.550 |
| D-STAR listener | 5 voice frames with the transcoder AMBE+ payload (seq 0 to 4, one stream id, all at 6.551, 1.005 s after the first USRP frame), 5 header repeats, then 1 end frame (seq 5, last flag, synthesized payload) |
| frames heard / sent | 5 / 5, no loss (second run also 5 / 5, STD +1.000 s, first frame heard +1.004 s) |

Verdict: passes the design rule; no finding. The old variant `slow_open` still gives
`opens == []` (run `out/20261107T114558Z/netns/slow_open`).

Commands (one scenario at a time, `wip-0348b98` binaries):

```
DVT_DVR_BIN_DIR=.cache/dvr-wip-0348b98 DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns cases/test_xlxd_usrp_netns.py -q -k test_open_wait_
# the old variant: -k test_slow_open_
```

## H5 applied: more than 128 units before SessionReady (the oldest are dropped)

Decision of the DV-Router lead: assert the documented limit of the pre-ready buffer.

- scenario `pre_ready_overflow` (profile `dvrouter`, case prefix `test_pre_ready_cap_`, case
  `test_pre_ready_cap_oldest_units_dropped`, spec `netns_ambed.pre_ready_overflow`): as
  `slow_open_listener` (STD 1.0 s after the OPEN, D-STAR listener on module B) with step
  `usrp:200:2`: 200 USRP voice frames 2 ms apart (0.4 s), frame i carrying i in its first PCM sample
  (`indexed_pcm`), so the fake transcoder's PCM requests name the units it received. The hub
  counters are read in the namespace (`docs/rig.md`, "netns cases").
- Why faster than real time: 128 units are 2.56 s of audio and the OPEN wait is 1050 ms, so a
  real-time (20 ms) USRP over has at most about 53 units in the buffer at any SessionReady that
  the worker answers (the controller give-up, 3000 ms, is never reached with a worker that
  answers or refuses by 1050 ms). The cap needs an ingress faster than about 122 units/s
  (a burst, an interlink bundle, a recorder replay). A 200-unit real-time over (4 s) over a 1.0 s
  STD buffers about 50 units and loses none; that is not the limit under test.

Design rule asserted (`docs/design/transcoding.md:344`, `:352`, `crates/room/src/xcode.rs`
`PRE_READY_CAP` = 128 and `Xcode::unit`, `pre_ready_dropped`, `crates/room/src/telemetry.rs`):

| Rule | Assertion |
|------|-----------|
| :344 "Before SessionReady the controller buffers up to 128 units per session with no age drop (when full, the oldest is dropped with a counter), then sends them in order" | the fake transcoder gets exactly 128 PCM requests, indexes 72 to 199 in order (the newest 128) |
| :344 same, listener side | the D-STAR listener hears 128 voice frames (one stream id, none before the STD, last one within the 3 s give-up of the first USRP frame) |
| :344 "with a counter"; telemetry `dv_xcode_frames_total{dir="in",result="pre_ready_dropped"}` | the counter says 72 (hub `/metrics`), `sessions_opened` 1, `open_failed` 0, `frames_out published` 128 |

Observations on `wip-0348b98` (alone, two runs, times in s on the fake's clock; first run
`out/20261107T115121Z/netns/pre_ready_overflow`, second `out/20261107T115333Z/netns/pre_ready_overflow`,
second one below):

| Item | Value |
|------|-------|
| USRP frames | 200, first 5.566, last 5.964 (0.398 s), unkey 5.964 |
| OPEN `[24, 5, [1]]` | 5.568 (2.2 ms after the first frame) |
| STD sent by the fake | 6.568 (1.000 s after the OPEN) |
| fake data requests | 128 x 322 B, indexes 72 to 199 in order, 6.569 to 6.580 (flushed with the 20-request window as the fake answers), then CS 6.580 |
| D-STAR listener | 128 voice frames (one stream id, first 6.571, last 6.580, D-STAR seq 9, 10, ... = 72 mod 21 on: the source position is kept), 5 header repeats, 1 end frame |
| hub counters | `pre_ready_dropped` 72, `sent` 128, `published` 128, `sessions opened` 1, `open_failed` 0 (first run identical) |

Verdict: matches the design, no finding. The oldest 72 units are dropped, the newest 128 are
transcoded in order, the counter is exact.

Remark (not a disagreement, the design does not define the label): `dv_xcode_sessions_total{result="opened"}`
counts the CreateSessions sent (`xcode.rs` `open_pending`), so a failed open is in `opened` and in
`open_failed` (see H6). The hub exposes the xcode counters only after the controller's next 10 s
telemetry record, about 10 s after the over (`status.md`, "Telemetry").

XCOBS1 (images after `wip-ac750c7`, announced by DV-Router, not yet run here): `dv_xcode_sessions_total{result}`
becomes `requested`, `opened`, `open_failed`; each attempt is counted once in `requested` and once in exactly
one of `opened` or `open_failed`. The former `failed` and `drain_timeout` results moved to
`dv_xcode_session_events_total{result}`. H5 then reads requested 1, opened 1, open_failed 0; H6 after over 1
requested 1, opened 0, open_failed 1 and after over 2 requested 2, opened 1, open_failed 1. The case reads both
generations through `normalise_sessions` (attempts, ok, failed) and waits on "settled" (opened + open_failed).

Command (alone, `wip-0348b98` binaries):

```
DVT_DVR_BIN_DIR=.cache/dvr-wip-0348b98 DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns cases/test_xlxd_usrp_netns.py -q -k test_pre_ready_cap_
```

## H6 applied: STD later than 1050 ms (the open is given up, the room recovers)

- scenario `open_late_std` (profile `dvrouter`, case prefix `test_open_refused_`, case
  `test_open_refused_late_std_given_up`, spec `netns_ambed.open_late_std`): `FakeAmbed(open_delays=[1.5, 0.05])`:
  the first STD is sent 1.5 s after its OPEN, the second 0.05 s after its OPEN. Steps: `listenB`,
  `usrp:100:20` (a real-time 100-unit over, 2.0 s), `usrpShortB` (5 units) 2.5 s after the first
  over ended. The delay is a timer, so the fake keeps answering CAPS while the first STD is pending.

Design rule asserted (`transcoding.md:472`, `:473`, `:509`, `:346`, `:140`):

| Rule | Assertion |
|------|-----------|
| :472 "nothing within 1050 ms: closes the session; one immediate CAPS re-probe; no CS; accepted = 0, retryable = 1" | exactly one CAPS between the first and the second OPEN, 1.0 to 1.2 s after the first OPEN |
| :346 "A failed open ... is counted, and the target streams of that over are not produced. There is no retry within a stream" | one OPEN for the 2 s over (opens == two OPENs in all, the second for the next over); the listener hears no frame and no header of the first over; hub `open_failed` 1, `frames in sent` 0, `frames out published` 0 |
| :473 "STD after the session closed: CS for that stream id, nothing bound" | one CS for stream 1 within 0.3 s after the late STD, and no data request on the first open's data port |
| the next over (STD at once) | opened again (second OPEN, STD 0.05 s), the 5 frames heard in full with one end frame inside 3 s, 5 PCM requests; hub `open_failed` stays 1 |

Observations on `wip-0348b98` (alone, two runs; first `out/20261107T115235Z/netns/open_late_std`,
second `out/20261107T115345Z/netns/open_late_std`, second one below, times in s on the fake's clock):

| Item | Value |
|------|-------|
| over 1 | 100 frames 5.562 to 7.542, unkey 7.542 |
| OPEN 1 / STD 1 | 5.564 (2.4 ms after the first frame) / 7.065 (1.5007 s later) |
| CAPS re-probe | 1.0565 s after OPEN 1 (first run 1.0561 s), one only |
| CS | stream 1 at 7.0648 (0.2 ms after the late STD); none before |
| data requests on the first open | 0 |
| listener during over 1 and until over 2 | 0 frames, 0 headers |
| hub after over 1 | `sessions opened` 1 (CreateSession sent), `open_failed` 1, `frames in sent` 0, `published` 0 |
| over 2 | 5 frames 11.058 to 11.138; OPEN 11.060; STD 11.110 (0.050 s); 5 PCM requests; listener 5 voice frames + 1 end frame 11.1116 to 11.1387, one stream id |
| hub after over 2 | `opened` 2, `open_failed` 1, `frames in sent` 5, `published` 5 |

Verdict: matches the design, no finding. The late STD is refused at the OPEN wait (the listener
hears nothing for that over, not even a header), counted once, CS follows the late STD, the room
is ready again for the next over (CAPS re-probe answered).

Command:

```
DVT_DVR_BIN_DIR=.cache/dvr-wip-0348b98 DVT_XLXD_BIN=scripts/dvr-import-wrap.sh DVT_SUT_PROFILE=dvrouter \
  scripts/contained.sh --name test -- python3 -m pytest -m netns cases/test_xlxd_usrp_netns.py -q -k test_open_refused_
```

## Harness fixes

Status: H1, H2 and H3 are APPLIED (commit 67028d9); H4 is APPLIED too, after the lead's
decision (section "H4 applied" below). Shared code
`dvtest/netns_runs.py`, offline tests `cases/unit/test_netns_runs.py`, recipe and variable in
`docs/rig.md` ("netns cases with a DV-Router build"). No expectation was changed. The text below
is the original proposal, kept for the reasoning.

- Applied as: H1 `cases` prefix per USRP scenario (`test_no_usrp_`, `test_legacy_ambed_`,
  `test_extended_`, `test_one_output_`, `test_slow_open_`), the `runs` fixture of both files
  launches only the scenarios of the selected tests of its own module; H2 `DVT_NETNS_PARALLEL`
  (default 1) = scenarios per wave, `TIMEOUT` per wave; H3 `get()` of both files fails with
  `environment: task limit exhausted (os error 11 / can't spawn worker thread in xlxd.log)`
  when the scenario did not get as far as its first `AMBEDPING` (lifecycle: or the restarted
  transcoder was not pinged, or the harness died) and `xlxd.log` shows the signature; a
  `harness.err` with `can't start new thread` gets the matching text. The lifecycle `listener_run()`
  goes through `get()`, so it is covered.
- Cost: with the default 1 the whole USRP file takes 5 scenarios one after the other (about
  5 x 30 s) and the lifecycle file 4 x about 55 s.

### Original proposal

- H1 (mechanical), `cases/test_xlxd_usrp_netns.py`: give each `SCENARIOS` entry
  a `cases` prefix list as `test_xlxd_transcoder_lifecycle.py` does (lines
  71-85, 284-287) and make `runs` start only the scenarios of the selected
  tests (`request.session.items`); `-k` then launches one scenario.
  `ref_run` already starts only its own.
- H2 (mechanical), both files: start the scenarios in waves instead of all at
  once, `DVT_NETNS_PARALLEL` (default 1; 435 tasks each, the scope limit is
  1024, so 2 is the most that could ever fit and is not safe next to pytest).
  `_collect` waits per wave.
- H3 (mechanical), both `get()`: before the `pinged` assertion, read the tail of
  `xlxd.log` (the `run_dir` is known); on "Resource temporarily unavailable
  (os error 11)" or "OS can't spawn worker thread" call `pytest.fail("environment:
  task limit exhausted (435 tasks per scenario, see
  docs/netns-dvrouter-classification.md); run one scenario at a time")` instead
  of the product-sounding "xlxd never sent AMBEDPING". Same for
  `listener_run()`.
- H4 (coverage; applied, see "H4 applied"): the `slow_open` DV-Router spec asserts "no OPEN"
  because nobody listens in another profile. To keep B12 (a short burst during
  a slow open is not lost) meaningful for DV-Router, add a variant with a
  D-STAR listener on the USRP module (as `listener_lost_and_back` does) and
  expect the 5-frame burst to arrive after the 1 s STD delay. Needs a design
  question first: the 1050 ms OPEN wait (`transcoding.md:461`) against a 1.0 s
  STD delay leaves 50 ms, so the expectation needs the lead's decision.
- Spec edits: none (class (a) is empty).

Commands and numbers in this file are from the run directories named above (H4: see its own section); the
tool scope was `scripts/contained.sh --name test` with one scenario at a time.
