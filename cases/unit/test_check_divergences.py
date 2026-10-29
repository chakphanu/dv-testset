# SPDX-License-Identifier: MIT
"""Declared divergences of the system under test (DVT_SUT_DIVERGENCES):
dvtest.check.apply_divergences on -sut captures, one case per name."""
from dvtest import check, divergences
from dvtest.proto import ambed as A
from test_check_common import Wire
from test_check_streams import FRAME, R, T, dextra, dextra_wire, mstcl_mid_stream


def sut_result(tmp_path, wire, names, ref=None):
    results = [check.check_file(wire.save(tmp_path / "xlxd-sut.pcapng"))]
    if ref is not None:
        results.append(check.check_file(ref.save(tmp_path / "xlxd-ref.pcapng")))
        check.differential(results)
    check.apply_divergences(results, set(names), {R})
    return results[0]


def severity(result, code):
    found = {f["severity"] for f in result["findings"] if f["code"] == code}
    assert len(found) == 1, (code, result["findings"])
    return found.pop()


def test_names_come_from_the_table(tmp_path):
    assert set(check.DIVERGENCES) <= set(divergences.TABLE)
    assert check.declared({"DVT_SUT_DIVERGENCES": "no_zero_fill, immediate_revoke"}) == {
        "no_zero_fill", "immediate_revoke"}
    path = dextra_wire().save(tmp_path / "xlxd-sut.pcapng")
    assert check.main([path, "--divergences", "tot_180s"]) == 0
    assert check.main([path, "--divergences", "bogus"]) == 2


def test_ambed_extended_open_always(tmp_path):
    def wire():
        w = Wire()
        w.send(R, A.PORT, T, A.PORT, A.open_ext("XLX999", A.AMBEPLUS, (A.AMBE2PLUS,), 9))
        w.send(T, A.PORT, R, A.PORT, A.std(1, 10101, A.AMBEPLUS, A.AMBE2PLUS, 9), 0.001)
        return w
    assert severity(sut_result(tmp_path, wire(), []), "ambed.ext_open_non_pcm") == "error"
    result = sut_result(tmp_path, wire(), ["ambed_extended_open_always"])
    assert severity(result, "ambed.ext_open_non_pcm") == "warn" and result["errors"] == 0


def test_immediate_revoke(tmp_path):
    assert severity(sut_result(tmp_path, mstcl_mid_stream(3), [], ref=mstcl_mid_stream(3)),
                    "dmr.after_close_cache") == "warn"
    result = sut_result(tmp_path, mstcl_mid_stream(3), ["immediate_revoke"], ref=mstcl_mid_stream(3))
    assert severity(result, "dmr.after_close_cache") == "error" and result["errors"] == 3


def test_no_zero_fill(tmp_path):
    def wire():
        w = Wire()
        w.send(T, 40001, R, dextra.PORT, dextra.link_packet("N0TEST", "B"))
        w.send(R, dextra.PORT, T, 40001, dextra.header(0x2222, "N0TEST", "B"), 0.01)
        for seq in range(10):
            w.send(R, dextra.PORT, T, 40001, dextra.voice(0x2222, seq, bytes(9), False,
                                                           dextra.SYNC_DATA if seq == 0 else dextra.SLOW_DATA), 0.02)
        w.send(R, dextra.PORT, T, 40001, dextra.voice(0x2222, 10, FRAME, True), 0.02)
        return w
    assert severity(sut_result(tmp_path, wire(), []), "dextra.zero_fill") == "warn"
    assert severity(sut_result(tmp_path, wire(), ["no_zero_fill"]), "dextra.zero_fill") == "error"


def test_synthesized_terminator(tmp_path):
    # the same timeout on ref and sut is upstream behaviour (warn) unless declared
    assert severity(sut_result(tmp_path, dextra_wire(last=False), [], ref=dextra_wire(last=False)),
                    "dextra.no_last") == "warn"
    result = sut_result(tmp_path, dextra_wire(last=False), ["synthesized_terminator"], ref=dextra_wire(last=False))
    assert severity(result, "dextra.no_last") == "error"
    assert "declared divergence synthesized_terminator" in result["findings"][-1]["message"]


def test_dvrouter_profile_implies_the_four(tmp_path, monkeypatch):
    """DVT_SUT_PROFILE=dvrouter declares the four wire divergences at once."""
    assert check.declared({"DVT_SUT_PROFILE": "dvrouter"}) >= set(check.DIVERGENCES)
    assert check.declared({"DVT_SUT_PROFILE": "xlxd"}) == set()
    names = check.declared({"DVT_SUT_PROFILE": "dvrouter"})
    result = sut_result(tmp_path, mstcl_mid_stream(3), names, ref=mstcl_mid_stream(3))
    assert severity(result, "dmr.after_close_cache") == "error"
    result = sut_result(tmp_path, dextra_wire(last=False), names, ref=dextra_wire(last=False))
    assert severity(result, "dextra.no_last") == "error"
    path = dextra_wire().save(tmp_path / "xlxd-sut.pcapng")
    monkeypatch.setenv("DVT_SUT_PROFILE", "bogus")
    assert check.main([path]) == 2
