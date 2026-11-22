# SPDX-License-Identifier: MIT
"""dvtest/bud1.py (the checker) and dvtest/bud1_plugin.py (the session hook). Pure, no rig."""
import io
import json

import pytest

from dvtest import bud1, bud1_plugin, metrics

pytestmark = pytest.mark.unit


def snapshot(procs=("dv-router", "dv-svc-web"), value=0, extra="", edge=True):
    lines = ["# HELP dv_fabric_body_rejected_total Fabric bodies dropped before application copying", "# TYPE dv_fabric_body_rejected_total counter"]
    for proc in procs:
        for reason in bud1.ADAPTER_REASONS:
            v = value if (proc, reason) == (procs[0], bud1.ADAPTER_REASONS[0]) else 0
            lines.append('dv_fabric_body_rejected_total{proc="%s",reason="%s"} %s' % (proc, reason, v))
    lines.append('dv_status_body_rejected_total 0')
    if edge:
        lines.append('dv_edge_drops_total{proc="usrp",reason="body_oversized"} 0')
        lines.append('dv_edge_drops_total{proc="usrp",reason="rx_ignored"} 7')
    return "\n".join(lines) + "\n" + extra


def make_run(tmp_path, final=None, records=()):
    base = tmp_path / "bud1"
    base.mkdir()
    if final is not None:
        (base / "final-xlxd-sut.prom").write_text(final)
    if records:
        (base / "samples.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(tmp_path)


def test_strict_switch():
    assert bud1.strict_on({"DVT_BUD1": "on"}) and not bud1.strict_on({})


def test_refusals_only_the_watched_nonzero_series():
    samples = metrics.parse(snapshot(value=2))
    got = bud1.refusals(samples)
    assert len(got) == 1 and got[0].value == 2 and got[0].labels["reason"] == "query_reply_oversized"
    assert bud1.refusals(metrics.parse(snapshot())) == []                       # rx_ignored = 7 is not a refusal


def test_other_watched_families():
    text = snapshot() + 'dv_codec_body_dropped_total{proc="ambed",reason="x"} 1\ndv_pkt_body_dropped_total{proc="pkt",reason="oversized"} 3\ndv_pkt_body_dropped_total{proc="pkt",reason="other"} 5\n'
    names = sorted(r.name for r in bud1.refusals(metrics.parse(text)))
    assert names == ["dv_codec_body_dropped_total", "dv_pkt_body_dropped_total"]


def test_family_gaps():
    text = snapshot(edge=False) + 'dv_edge_drops_total{proc="newproc",reason="x"} 0\n'
    assert bud1.family_gaps(metrics.parse(text)) == {"newproc": list(bud1.ADAPTER_REASONS)}
    assert bud1.family_gaps(metrics.parse(snapshot(edge=False))) == {}
    assert bud1.family_gaps(metrics.parse(snapshot())) == {"usrp": list(bud1.ADAPTER_REASONS)}   # usrp has only the edge drops lines here


def test_judge_clean_dir(tmp_path):
    text = snapshot(procs=("dv-router",), edge=False)
    v = bud1.judge_dir(make_run(tmp_path, final=text, records=[{"t": 1, "service": "xlxd-sut", "rejected": []}]))
    assert v.problems == [] and v.finals == 1 and v.samples == 1


def test_judge_flags_a_refusal_seen_only_in_a_sample(tmp_path):
    v = bud1.judge_dir(make_run(tmp_path, final=snapshot(procs=("dv-router",), edge=False),
                                records=[{"t": 5, "service": "xlxd-sut", "rejected": [["dv_fabric_body_rejected_total", {"proc": "dv-svc-web", "reason": "web_reply_oversize"}, 1]]}]))
    assert any("web_reply_oversize" in p for p in v.problems)


def test_judge_flags_a_refusal_in_the_final_snapshot(tmp_path):
    v = bud1.judge_dir(make_run(tmp_path, final=snapshot(procs=("dv-router",), value=1, edge=False)))
    assert any("final snapshot" in p for p in v.problems)


def test_judge_image_without_the_family(tmp_path):
    run = make_run(tmp_path, final='dv_edge_drops_total{proc="usrp",reason="x"} 0\n')
    assert any("no BUD1" in p for p in bud1.judge_dir(run).problems)
    assert bud1.judge_dir(run, lenient=True).problems == []


def test_main_exit_codes(tmp_path):
    out = io.StringIO()
    assert bud1.main([str(tmp_path / "nothing")], out) == 2
    text = snapshot(procs=("dv-router",), edge=False)
    good = tmp_path / "good"
    good.mkdir()
    make_run(good, final=text)
    bad = tmp_path / "bad"
    bad.mkdir()
    make_run(bad, final=text.replace(' 0\n', ' 1\n', 1))
    assert bud1.main([str(good)], io.StringIO()) == 0
    assert bud1.main([str(bad)], io.StringIO()) == 1
    assert bud1.main([str(good), str(bad)], io.StringIO()) == 1
    assert bud1.main([], io.StringIO()) == 2


def test_hook_samples_and_writes_the_final_snapshot(tmp_path):
    calls = []

    def fetch(service):
        calls.append(service)
        if len(calls) == 2:
            raise RuntimeError("hub down")
        return snapshot(procs=("dv-router",), value=0)

    hook = bud1_plugin.Hook(tmp_path, fetch, ["xlxd-sut"], settle_s=0.0, sleep=lambda s: None)
    hook.sample()
    hook.sample()                                                              # the failing read
    hook.finish()
    lines = [json.loads(x) for x in (tmp_path / "bud1" / "samples.jsonl").read_text().splitlines()]
    assert len(lines) == 3 and "error" in lines[1] and lines[2]["final"] is True and hook.errors == 1
    assert (tmp_path / "bud1" / "final-xlxd-sut.prom").read_text().startswith("# HELP")


def test_enabled_needs_the_switch_and_the_channel():
    assert not bud1_plugin.enabled({"DVT_BUD1": "on"})
    assert not bud1_plugin.enabled({"DVT_RIG_CONTROL": "1"})
    assert bud1_plugin.enabled({"DVT_BUD1": "on", "DVT_RIG_CONTROL": "1"})
    assert bud1_plugin.services({"DVT_TWONODE": "on"}) == ["xlxd-sut", "dvr-peer"]
    assert bud1_plugin.services({}) == ["xlxd-sut"]


def test_pre_kill_waits_once_per_burst_and_samples(tmp_path):
    naps, texts = [], []

    def fetch(service):
        texts.append(service)
        return snapshot(procs=("dv-router",), edge=False)

    now = [1000.0]
    hook = bud1_plugin.Hook(tmp_path, fetch, ["xlxd-sut"], settle_s=11.0, clock=lambda: now[0], sleep=lambda s: naps.append(s))
    hook.pre_kill("xlxd-sut", "dv-svc-web")
    now[0] += 0.3
    hook.pre_kill("xlxd-sut", "dv-svc-web")                    # a kill loop: no more waiting
    hook.pre_kill("xlxd-sut", "dv-room")                       # another process: its own wait
    now[0] += 100.0
    hook.pre_kill("xlxd-sut", "dv-svc-web")                    # a new burst
    assert naps == [11.0, 11.0, 11.0] and len(texts) == 3


def test_kill_hooks_run_before_the_request(tmp_path):
    from dvtest import control
    seen = []
    rc = control.RigControl(tmp_path)
    rc._request = lambda line, timeout: (_ for _ in ()).throw(RuntimeError("stop here"))
    control.RigControl.kill_hooks.append(lambda s, p: seen.append((s, p)))
    try:
        with pytest.raises(RuntimeError):
            rc.kill_proc("xlxd-sut", "dv-svc-web", "KILL")
    finally:
        control.RigControl.kill_hooks.clear()
    assert seen == [("xlxd-sut", "dv-svc-web")]
