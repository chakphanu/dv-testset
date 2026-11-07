# SPDX-License-Identifier: MIT
"""dvtest/netns_runs.py (waves of netns scenarios, task starvation reading)
and the get() functions of the two netns case files, offline with fake logs."""
import importlib.util
import types
from pathlib import Path

import pytest

from dvtest import netns_runs

CASES = Path(__file__).resolve().parents[1]

OS11 = ("dv-router: dv-codec-ambed: cannot start /x/dv-codec-ambed: "
        "Resource temporarily unavailable (os error 11)\n")
SPAWN = ("thread 'main' panicked at tokio/worker.rs:515:13: "
         "OS can't spawn worker thread: Resource temporarily unavailable (os error 11)\n")
QUIET = "dvr-import-wrap: path=convert\ndv-router: up\ndv-codec-ambed: connected\n"


def load(name):
    spec = importlib.util.spec_from_file_location("netns_under_test_" + name, CASES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- parallel

@pytest.mark.parametrize("env, want", [({}, 1), ({"DVT_NETNS_PARALLEL": ""}, 1),
                                       ({"DVT_NETNS_PARALLEL": "1"}, 1),
                                       ({"DVT_NETNS_PARALLEL": " 2 "}, 2)])
def test_parallel_default_and_value(env, want):
    assert netns_runs.parallel(env) == want


@pytest.mark.parametrize("value", ["0", "-1", "two", "1.5"])
def test_parallel_rejects_bad_values(value):
    with pytest.raises(ValueError, match="DVT_NETNS_PARALLEL"):
        netns_runs.parallel({"DVT_NETNS_PARALLEL": value})


# ---------------------------------------------------------------- waves

def test_waves_cut():
    names = list("abcde")
    assert netns_runs.waves(names, 1) == [["a"], ["b"], ["c"], ["d"], ["e"]]
    assert netns_runs.waves(names, 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert netns_runs.waves(names, 9) == [names]
    assert netns_runs.waves([], 3) == []


class Probe:
    """launch / collect fakes that count the scenarios alive at once."""

    def __init__(self):
        self.alive = 0
        self.peak = 0
        self.events = []

    def launch(self, name):
        self.alive += 1
        self.peak = max(self.peak, self.alive)
        self.events.append(("launch", name))
        return "h-" + name

    def collect(self, launched):
        self.events.append(("collect", tuple(launched)))
        self.alive -= len(launched)
        return {name: {"handle": handle} for name, handle in launched.items()}


@pytest.mark.parametrize("size, peak, collects", [(1, 1, 5), (2, 2, 3), (5, 5, 1), (8, 5, 1)])
def test_run_in_waves_bounds_concurrency(size, peak, collects):
    names = ["no_usrp", "legacy_ambed", "extended", "one_output", "slow_open"]
    probe = Probe()
    results = netns_runs.run_in_waves(names, probe.launch, probe.collect, size)
    assert probe.peak == peak
    assert list(results) == names                       # order kept
    assert results["extended"] == {"handle": "h-extended"}
    assert sum(1 for kind, _ in probe.events if kind == "collect") == collects


def test_run_in_waves_next_wave_after_collect():
    probe = Probe()
    netns_runs.run_in_waves(["a", "b", "c"], probe.launch, probe.collect, 2)
    assert probe.events == [("launch", "a"), ("launch", "b"), ("collect", ("a", "b")),
                            ("launch", "c"), ("collect", ("c",))]


def test_run_in_waves_nothing_selected():
    probe = Probe()
    assert netns_runs.run_in_waves([], probe.launch, probe.collect, 1) == {}
    assert probe.events == []


# ---------------------------------------------------------------- selection

class Item:
    def __init__(self, name, module="m"):
        self.name = name
        self.originalname = name.split("[")[0]
        self.module = types.SimpleNamespace(__name__=module)


def test_selected_names_filters_by_module_and_strips_params():
    items = [Item("test_extended_opens"), Item("test_x[1]"), Item("test_other", module="n")]
    assert netns_runs.selected_names(items) == {"test_extended_opens", "test_x", "test_other"}
    assert netns_runs.selected_names(items, "m") == {"test_extended_opens", "test_x"}


@pytest.mark.parametrize("module_name", ["test_xlxd_usrp_netns", "test_xlxd_transcoder_lifecycle"])
def test_every_scenario_has_cases_and_every_case_has_a_scenario(module_name):
    module = load(module_name)
    for name, spec in module.SCENARIOS.items():
        assert isinstance(spec["cases"], str) and spec["cases"].startswith("test_"), name
    tests = [n for n in dir(module) if n.startswith("test_")]
    for test in tests:
        if test.startswith("test_rig_"):
            continue
        owners = netns_runs.scenarios_for(module.SCENARIOS, {test})
        assert len(owners) == 1, (test, owners)


def test_usrp_k_selects_one_scenario():
    module = load("test_xlxd_usrp_netns")
    scenarios = module.SCENARIOS
    assert netns_runs.scenarios_for(scenarios, {"test_extended_opens", "test_extended_dstar_to_usrp"}) == ["extended"]
    assert netns_runs.scenarios_for(scenarios, {"test_no_usrp_link_bytes_equal_reference"}) == ["no_usrp"]
    assert netns_runs.scenarios_for(scenarios, {"test_one_output_opens", "test_slow_open_short_burst_delivered"}) == \
        ["one_output", "slow_open"]
    assert netns_runs.scenarios_for(scenarios, set()) == []
    assert netns_runs.scenarios_for(scenarios, {"test_something_else"}) == []


def test_slow_open_variants_are_selected_apart():
    """H4: the no-listener variant (test_slow_open_) and the listener variant
    (test_open_wait_) never pull each other's scenario with -k."""
    module = load("test_xlxd_usrp_netns")
    scenarios = module.SCENARIOS
    assert netns_runs.scenarios_for(scenarios, {"test_slow_open_short_burst_delivered"}) == ["slow_open"]
    assert netns_runs.scenarios_for(scenarios, {"test_open_wait_listener_short_burst_heard"}) == \
        ["slow_open_listener"]
    prefixes = [spec["cases"] for spec in scenarios.values()]
    for prefix in prefixes:
        assert [p for p in prefixes if p.startswith(prefix)] == [prefix], prefix


def test_for_profile_drops_scenarios_of_another_profile():
    module = load("test_xlxd_usrp_netns")
    scenarios = module.SCENARIOS
    names = ["slow_open", "slow_open_listener"]
    assert netns_runs.for_profile(names, scenarios, "dvrouter") == names
    assert netns_runs.for_profile(names, scenarios, "xlxd") == ["slow_open"]
    assert netns_runs.for_profile([], scenarios, "xlxd") == []
    assert [n for n, spec in scenarios.items() if "profile" in spec] == \
        ["slow_open_listener", "pre_ready_overflow", "open_late_std"]
    # H5 / H6: every scenario that only the DV-Router profile can judge is dropped under xlxd
    assert netns_runs.for_profile(list(scenarios), scenarios, "xlxd") == \
        [n for n in scenarios if "profile" not in scenarios[n]]
    assert netns_runs.for_profile(list(scenarios), scenarios, "dvrouter") == list(scenarios)


def test_boundary_variants_are_selected_apart():
    """H5 / H6: -k test_pre_ready_cap_ and -k test_open_refused_ each launch their
    own scenario only, never the slow-open ones (and the other way round)."""
    module = load("test_xlxd_usrp_netns")
    scenarios = module.SCENARIOS
    assert netns_runs.scenarios_for(scenarios, {"test_pre_ready_cap_oldest_units_dropped"}) == ["pre_ready_overflow"]
    assert netns_runs.scenarios_for(scenarios, {"test_open_refused_late_std_given_up"}) == ["open_late_std"]
    assert netns_runs.scenarios_for(scenarios, {"test_slow_open_short_burst_delivered"}) == ["slow_open"]
    assert netns_runs.scenarios_for(scenarios, {"test_open_wait_listener_short_burst_heard"}) == \
        ["slow_open_listener"]
    both = {"test_pre_ready_cap_oldest_units_dropped", "test_open_refused_late_std_given_up"}
    assert netns_runs.scenarios_for(scenarios, both) == ["pre_ready_overflow", "open_late_std"]
    # the test names really start with the prefix of their scenario
    for case, scenario in (("test_pre_ready_cap_oldest_units_dropped", "pre_ready_overflow"),
                           ("test_open_refused_late_std_given_up", "open_late_std")):
        assert hasattr(module, case) and case.startswith(scenarios[scenario]["cases"])
    prefixes = [spec["cases"] for spec in scenarios.values()]
    assert len(prefixes) == len(set(prefixes))
    for prefix in prefixes:
        assert [p for p in prefixes if p.startswith(prefix)] == [prefix], prefix


def test_indexed_pcm_names_the_unit():
    module = load("test_xlxd_usrp_netns")
    assert len(module.indexed_pcm(0)) == 320 and module.indexed_pcm(5)[2:] == module.USRP_PCM[2:]
    for index in (0, 1, 71, 72, 199, 255, 256, 1000):
        request = bytes([5, index % 256]) + module.indexed_pcm(index)      # codec, pid, 320 B
        assert len(request) == 322 and module.pcm_index(request) == index


def hub_lines(opened, failed, dropped=0, sent=0, published=0):
    return ['dv_xcode_sessions_total{proc="b",result="opened",room="b"} %d' % opened,
            'dv_xcode_sessions_total{proc="b",result="open_failed",room="b"} %d' % failed,
            'dv_xcode_frames_total{dir="in",proc="b",result="pre_ready_dropped",room="b"} %d' % dropped,
            'dv_xcode_frames_total{dir="in",proc="b",result="sent",room="b"} %d' % sent,
            'dv_xcode_frames_total{dir="out",proc="b",result="published",room="b"} %d' % published,
            'dv_room_data_total{proc="x",result="accepted",room="x"} 0']


def test_xcode_counters_read_the_snapshot_of_the_step():
    module = load("test_xlxd_usrp_netns")
    run = {"metrics": [["a", 1.0, hub_lines(1, 1)], ["b", 2.0, hub_lines(2, 1, 3, 4, 5)]]}
    first = module.xcode_counters(run, "a")
    assert (first["sessions_opened"], first["sessions_open_failed"], first["frames_out_published"]) == (1, 1, 0)
    second = module.xcode_counters(run, "b")
    assert second["sessions_opened"] == 2 and second["frames_in_pre_ready_dropped"] == 3
    assert second["frames_in_sent"] == 4 and second["frames_out_published"] == 5
    assert module.xcode_counters(run, "missing")["sessions_opened"] is None
    assert module.xcode_counters({"metrics": [["a", 1.0, ["error: refused"]]]}, "a")["sessions_opened"] is None


def pcm_request(index):
    return (bytes([5, index % 256]) + (index).to_bytes(2, "little") + bytes(318)).hex()


def test_pre_ready_observed_reads_the_run():
    module = load("test_xlxd_usrp_netns")
    kept = list(range(72, 200))
    run = {"usrp_tx": [[5.0, 5.4, 5.401]], "opens": [[5.003, 24, 5, [1], 7, "00", 41000]],
           "events": [[0.5, "CAPS request"], [6.003, "OPEN len=24"]],
           "data_in": {"41000": [[6.004, pcm_request(i)] for i in kept]},
           "dstar_hdr": [[6.005, 9]],
           "dstar_rx": [[6.006, 9, i % 21, False, True] for i in kept] + [[6.007, 9, 8, True, False]],
           "closes": [[6.01, 1]],
           "metrics": [["usrp:200:2", 10.0, hub_lines(1, 0, 72, 128, 128)]]}
    seen = module.pre_ready_observed(run, 1.0, 200, 0.002)
    assert seen["fake_request_indexes"] == kept and seen["fake_data_requests"] == 128
    assert seen["dstar_frames_heard"] == 128 and seen["dstar_end_frames"] == 1
    assert seen["dstar_seqs"][:3] == [9, 10, 11] and seen["dstar_stream_ids"] == [9]
    assert round(seen["std_delay_s"], 3) == 1.0 and round(seen["open_lag_s"], 3) == 0.003
    assert seen["counters"]["frames_in_pre_ready_dropped"] == 72
    # a transcoder that kept the OLDEST 128 would show indexes 0..127
    run["data_in"] = {"41000": [[6.004, pcm_request(i)] for i in range(128)]}
    assert module.pre_ready_observed(run, 1.0, 200, 0.002)["fake_request_indexes"] == list(range(128))


def test_open_refused_observed_reads_the_run():
    module = load("test_xlxd_usrp_netns")
    caps = "AMBEDCAPS".encode().hex()
    run = {"usrp_tx": [[5.0, 7.0, 7.001], [11.0, 11.08, 11.081]],
           "opens": [[5.003, 24, 5, [1], 7, "00", 41000], [11.003, 24, 5, [1], 8, "00", 41001]],
           "events": [[7.0, "OPEN len=24 tr=7"], [11.05, "OPEN len=24 tr=8"]],
           "ctrl": [[0.5, caps], [6.06, caps], [11.9, caps]],
           "closes": [[7.001, 1], [11.12, 2]],
           "data_in": {"41000": [], "41001": [[11.06, "00"]] * 5},
           "dstar_hdr": [[11.06, 3]],
           "dstar_rx": [[11.06, 3, i, False, True] for i in range(5)] + [[11.07, 3, 5, True, False]],
           "metrics": [["usrp:100:20", 10.0, hub_lines(1, 1)], ["usrpShortB", 15.0, hub_lines(2, 1, 0, 5, 5)]]}
    seen = module.open_refused_observed(run)
    assert round(seen["std1_delay_s"], 3) == 1.997 and round(seen["std2_delay_s"], 3) == 0.047
    assert [round(x, 2) for x in seen["caps_after_open1_s"]] == [1.06]      # the later CAPS is after OPEN 2
    assert seen["requests_open1"] == 0 and seen["requests_open2"] == 5
    assert seen["over1_dstar_frames"] == 0 and seen["over1_dstar_headers"] == 0
    assert seen["over2_dstar_frames_heard"] == 5 and seen["over2_dstar_end_frames"] == 1
    assert seen["over2_dstar_stream_ids"] == [3]
    assert seen["counters_after_over1"]["sessions_open_failed"] == 1
    assert seen["counters_after_over2"]["sessions_opened"] == 2
    # a frame of the refused over leaks into the first window
    run["dstar_rx"].append([8.0, 4, 0, False, True])
    assert module.open_refused_observed(run)["over1_dstar_frames"] == 1


def test_open_wait_observed_reads_the_run():
    module = load("test_xlxd_usrp_netns")
    run = {"usrp_tx": [[5.0, 5.08, 5.081]], "opens": [[5.002, 24, 5, [1], 7, "00", 41000]],
           "events": [[0.5, "CAPS request"], [6.002, "OPEN len=24"], [6.003, "CLOSE id=1"]],
           "dstar_hdr": [[6.004, 9]],
           "dstar_rx": [[6.005, 9, i, False, True] for i in range(5)] + [[6.005, 9, 5, True, False]],
           "data_in": {"41000": [[6.003, "00"]] * 5}}
    seen = module.open_wait_observed(run, 1.0)
    assert seen["dstar_frames_heard"] == 5 and seen["dstar_end_frames"] == 1
    assert seen["dstar_seqs"] == [0, 1, 2, 3, 4] and seen["dstar_stream_ids"] == [9]
    assert seen["std_at"] == 6.002 and round(seen["std_delay_s"], 3) == 1.0
    assert round(seen["open_lag_s"], 3) == 0.002 and seen["fake_data_requests"] == 5
    assert seen["dstar_first_heard"] == seen["dstar_last_heard"] == 6.005
    # no OPEN event: the STD time falls back to OPEN + the scenario delay
    run["events"] = []
    assert round(module.open_wait_observed(run, 1.0)["std_at"], 3) == 6.002
    # nothing heard
    run["dstar_rx"] = []
    seen = module.open_wait_observed(run, 1.0)
    assert seen["dstar_frames_heard"] == 0 and seen["dstar_first_heard"] is None


def test_lifecycle_selection_unchanged():
    module = load("test_xlxd_transcoder_lifecycle")
    assert netns_runs.scenarios_for(module.SCENARIOS, {"test_lost_listener_heard"}) == ["listener_lost_and_back"]
    assert netns_runs.scenarios_for(module.SCENARIOS, {"test_lost_transcoder_pcm_resumes",
                                                       "test_legacy_swap_recovers"}) == \
        ["lost_and_back", "legacy_swap"]


# ---------------------------------------------------------------- starvation

def run_with_log(tmp_path, log=None, error=None):
    if log is not None:
        (tmp_path / "xlxd.log").write_text(log)
    run = {"run_dir": str(tmp_path)}
    if error is not None:
        run["error"] = error
    return run


@pytest.mark.parametrize("log", [OS11, SPAWN, QUIET + OS11 + QUIET, QUIET * 3 + SPAWN])
def test_starvation_found_in_xlxd_log(tmp_path, log):
    assert netns_runs.starvation_message(run_with_log(tmp_path, log)) == netns_runs.MESSAGE


def test_starvation_message_text(tmp_path):
    message = netns_runs.starvation_message(run_with_log(tmp_path, OS11))
    assert message.startswith("environment: task limit exhausted "
                              "(os error 11 / can't spawn worker thread in xlxd.log)")


def test_starvation_not_reported_for_a_quiet_log(tmp_path):
    assert netns_runs.starvation_message(run_with_log(tmp_path, QUIET)) is None
    assert netns_runs.starvation_message(run_with_log(tmp_path, "")) is None


def test_starvation_other_errors_are_not_starvation(tmp_path):
    log = "Resource temporarily unavailable\nerror 11\nspawn failed\n"   # similar, not the signature
    assert netns_runs.starvation_message(run_with_log(tmp_path, log)) is None


def test_starvation_missing_log_and_run_dir(tmp_path):
    assert netns_runs.starvation_message({"run_dir": str(tmp_path)}) is None
    assert netns_runs.starvation_message({}) is None


def test_starvation_harness_died(tmp_path):
    error = "Traceback ...\nRuntimeError: can't start new thread\n"
    assert netns_runs.starvation_message(run_with_log(tmp_path, QUIET, error)) == netns_runs.MESSAGE_HARNESS
    assert netns_runs.starvation_message({"error": error}) == netns_runs.MESSAGE_HARNESS
    assert netns_runs.starvation_message({"error": "Traceback\nValueError: boom\n"}) is None


def test_starvation_reads_only_the_tail(tmp_path):
    (tmp_path / "xlxd.log").write_text(OS11 + "x" * (netns_runs.TAIL_BYTES + 10) + "\n")
    assert netns_runs.starvation_message({"run_dir": str(tmp_path)}) is None
    (tmp_path / "xlxd.log").write_text("x" * (netns_runs.TAIL_BYTES + 10) + OS11)
    assert netns_runs.starvation_message({"run_dir": str(tmp_path)}) == netns_runs.MESSAGE


# ---------------------------------------------------------------- get() of the case files

@pytest.mark.parametrize("module_name", ["test_xlxd_usrp_netns", "test_xlxd_transcoder_lifecycle"])
def test_get_names_the_environment_when_never_pinged(tmp_path, module_name):
    module = load(module_name)
    runs = {"s": dict(run_with_log(tmp_path, QUIET + SPAWN), pinged=False, up_pinged=[], xlxd_alive=True)}
    with pytest.raises(pytest.fail.Exception, match="environment: task limit exhausted"):
        module.get(runs, "s")


@pytest.mark.parametrize("module_name", ["test_xlxd_usrp_netns", "test_xlxd_transcoder_lifecycle"])
def test_get_keeps_the_product_message_without_the_signature(tmp_path, module_name):
    module = load(module_name)
    runs = {"s": dict(run_with_log(tmp_path, QUIET), pinged=False, up_pinged=[], xlxd_alive=True)}
    with pytest.raises(AssertionError, match="never sent AMBEDPING"):
        module.get(runs, "s")


@pytest.mark.parametrize("module_name", ["test_xlxd_usrp_netns", "test_xlxd_transcoder_lifecycle"])
def test_get_harness_died_of_threads(tmp_path, module_name):
    module = load(module_name)
    runs = {"s": run_with_log(tmp_path, None, "RuntimeError: can't start new thread")}
    with pytest.raises(pytest.fail.Exception, match="environment: task limit exhausted"):
        module.get(runs, "s")


@pytest.mark.parametrize("module_name", ["test_xlxd_usrp_netns", "test_xlxd_transcoder_lifecycle"])
def test_get_passes_a_healthy_run_even_with_a_restart_in_the_log(tmp_path, module_name):
    """A pinged run is not turned red by an old restart line: results of the
    default (xlxd reference) runs do not change."""
    module = load(module_name)
    run = dict(run_with_log(tmp_path, OS11), pinged=True, up_pinged=[True], xlxd_alive=True)
    assert module.get({"s": run}, "s") is run


@pytest.mark.parametrize("module_name", ["test_xlxd_usrp_netns", "test_xlxd_transcoder_lifecycle"])
def test_get_scenario_not_launched(module_name):
    module = load(module_name)
    with pytest.raises(AssertionError, match="was not launched"):
        module.get({}, "extended")


def test_lifecycle_restarted_transcoder_not_pinged_in_a_starved_run(tmp_path):
    module = load("test_xlxd_transcoder_lifecycle")
    run = dict(run_with_log(tmp_path, OS11), pinged=True, up_pinged=[False], xlxd_alive=True)
    with pytest.raises(pytest.fail.Exception, match="environment: task limit exhausted"):
        module.get({"s": run}, "s")


def test_normalise_sessions_reads_both_metric_generations():
    module = load("test_xlxd_usrp_netns")
    old = {"sessions_opened": 2, "sessions_open_failed": 1, "sessions_requested": 0}
    assert module.normalise_sessions(old) == {"attempts": 2, "ok": 1, "failed": 1, "xcobs1": False}
    new = {"sessions_opened": 1, "sessions_open_failed": 1, "sessions_requested": 2}
    assert module.normalise_sessions(new) == {"attempts": 2, "ok": 1, "failed": 1, "xcobs1": True}
    quiet = {"sessions_opened": 1, "sessions_open_failed": 0, "sessions_requested": 1}
    assert module.normalise_sessions(quiet)["ok"] == 1
    assert module.normalise_sessions({"sessions_opened": None})["attempts"] is None
