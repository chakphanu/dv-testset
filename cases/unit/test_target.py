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
