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
    assert [n for n, spec in scenarios.items() if "profile" in spec] == ["slow_open_listener"]


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
