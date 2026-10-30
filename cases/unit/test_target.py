# SPDX-License-Identifier: MIT
from dvtest import target


def test_defaults_from_subnet():
    t = target.load(env={})
    assert t.subnet == "172.30.0.0/24"
    assert (t.xlxd_ref, t.xlxd_sut, t.ambed_ref, t.ambed_sut, t.tester, t.allstar) == \
        ("172.30.0.10", "172.30.0.11", "172.30.0.20", "172.30.0.21", "172.30.0.30", "172.30.0.40")
    assert t.ambed_addr == "" and t.xlxd_bin == "" and t.ambed_port == 10100
    assert not t.is_given("ambed_ref") and t.reflector == "XLX999"


def test_env_overrides(tmp_path):
    t = target.load(env={"DVT_SUBNET": "172.30.7.0/24", "DVT_AMBED_SUT": "192.0.2.5",
                         "DVT_AMBED_PORT": "10200", "DVT_AMBED_ADDR": "127.0.0.9"})
    assert t.xlxd_sut == "172.30.7.11" and t.ambed_sut == "192.0.2.5"
    assert t.ambed_port == 10200 and t.is_given("ambed_addr") and not t.is_given("xlxd_sut")


def test_toml_then_env(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text('[target]\nxlxd_sut = "192.0.2.11"\nambed_addr = "192.0.2.21"\n'
                    'ambed_stream_cap = 4\nunknown = 1\n')
    t = target.load(env={"DVT_TARGET_FILE": str(path), "DVT_AMBED_ADDR": "127.0.0.2"})
    assert t.xlxd_sut == "192.0.2.11" and t.ambed_addr == "127.0.0.2" and t.ambed_stream_cap == 4
    flat = tmp_path / "flat.toml"
    flat.write_text('xlxd_bin = "build/xlxd"\n')
    assert target.load(env={}, path=str(flat)).xlxd_bin == "build/xlxd"


def test_divergences(tmp_path):
    import pytest
    from dvtest import divergences
    assert target.load(env={}).divergences == frozenset()
    t = target.load(env={"DVT_SUT_DIVERGENCES": " late_entry_transcoded , no_zero_fill,"})
    assert t.divergences == {"late_entry_transcoded", "no_zero_fill"}
    path = tmp_path / "t.toml"
    path.write_text('sut_divergences = ["tot_180s"]\n')
    assert target.load(env={}, path=str(path)).divergences == {"tot_180s"}
    assert target.load(env={"DVT_SUT_DIVERGENCES": "immediate_revoke"}, path=str(path)).divergences == \
        {"immediate_revoke"}
    with pytest.raises(ValueError, match="known: ambed_extended_open_always"):
        target.load(env={"DVT_SUT_DIVERGENCES": "bogus"})
    assert divergences.load({"DVT_SUT_DIVERGENCES": "no_load_loss"}) == {"no_load_loss"}
    assert len(divergences.TABLE) == 7 and all(len(v) == 2 for v in divergences.TABLE.values())


def test_sut_profile_and_specs():
    import pytest
    from dvtest import specs
    assert target.load(env={}).sut_profile == "xlxd"
    assert target.load(env={"DVT_SUT_PROFILE": "dvrouter"}).sut_profile == "dvrouter"
    assert specs.load("xlxd") is None and specs.expectation("xlxd", "test_x") is None
    assert "test_first_talker_wins" in specs.load("dvrouter").SPEC and specs.expectation("dvrouter", "test_x") is None
    with pytest.raises(ValueError, match="no spec module"):
        specs.load("nosuch")
    with pytest.raises(ValueError, match="invalid"):
        specs.load("../x")


def test_profile_implies_divergences():
    import pytest
    from dvtest import divergences
    t = target.load(env={"DVT_SUT_PROFILE": "dvrouter", "DVT_SUT_DIVERGENCES": "tot_180s"})
    assert t.divergences == {"tot_180s", *divergences.PROFILES["dvrouter"]}
    assert divergences.effective({"DVT_SUT_PROFILE": "dvrouter"}) == set(divergences.PROFILES["dvrouter"])
    assert "no_load_loss" in divergences.PROFILES["dvrouter"]
    assert divergences.effective({}) == set()
    with pytest.raises(ValueError, match="unknown DVT_SUT_PROFILE"):
        target.load(env={"DVT_SUT_PROFILE": "nosuch"})
    t = target.load(env={})
    assert (t.xlxd_peer2_sut, t.xlxd_peer2_ref) == ("172.30.0.14", "172.30.0.15")


def test_no_radio_id_rule_mode(monkeypatch):
    import pytest
    from dvtest import specs
    table = {"p25": {"P-P25-23": {"src": []}, "P-P25-23@fallback": {"src": [1023000]},
                     "P-P25-01": {"x": 1}}}
    monkeypatch.setattr(specs, "rule", lambda t, key: table.get(key))
    t = target.load(env={"DVT_SUT_PROFILE": "dvrouter"})
    assert t.no_radio_id == "" and specs.rule_mode(t, "p25", "P-P25-23") == {"src": []}
    t = target.load(env={"DVT_SUT_PROFILE": "dvrouter", "DVT_NO_RADIO_ID": "fallback"})
    assert specs.rule_mode(t, "p25", "P-P25-23") == {"src": [1023000]}
    assert specs.rule_mode(t, "p25", "P-P25-01") == {"x": 1}          # no @fallback variant
    assert specs.rule_mode(t, "p25", "P-P25-99") is None
    t = target.load(env={"DVT_SUT_PROFILE": "dvrouter", "DVT_NO_RADIO_ID": "local"})
    assert specs.rule_mode(t, "p25", "P-P25-23") == {"src": []}       # no @local variant: plain entry
    t = target.load(env={"DVT_NO_RADIO_ID": "bogus"})
    with pytest.raises(ValueError, match="DVT_NO_RADIO_ID"):
        specs.rule_mode(t, "p25", "P-P25-23")


def test_urfd_reference():
    import pytest
    t = target.load(env={})
    assert (t.urfd, t.ref_kind, t.xlxd_ref) == ("172.30.0.70", "xlxd", "172.30.0.10")
    # rig: run.sh passes DVT_XLXD_REF=<net>.70 with DVT_REF_KIND=urfd
    t = target.load(env={"DVT_SUBNET": "172.30.7.0/24", "DVT_REF_KIND": "urfd",
                         "DVT_XLXD_REF": "172.30.7.70", "DVT_URFD": "172.30.7.70"})
    assert (t.xlxd_ref, t.urfd, t.ref_kind) == ("172.30.7.70", "172.30.7.70", "urfd")
    # no DVT_XLXD_REF: the reference follows the URFd host
    t = target.load(env={"DVT_REF_KIND": "urfd", "DVT_URFD": "192.0.2.70"})
    assert t.xlxd_ref == "192.0.2.70" and not t.is_given("xlxd_ref")
    assert target.load(env={"DVT_REF_KIND": "urfd"}).xlxd_ref == "172.30.0.70"
    # explicit DVT_XLXD_REF wins
    assert target.load(env={"DVT_REF_KIND": "urfd", "DVT_XLXD_REF": "192.0.2.10"}).xlxd_ref == "192.0.2.10"
    with pytest.raises(ValueError, match="DVT_REF_KIND"):
        target.load(env={"DVT_REF_KIND": "urf"})
