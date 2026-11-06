# SPDX-License-Identifier: MIT
"""dvtest/oracle_dstar_sd.py: corpus, driver text, views and comparison (no Docker)."""
import pytest

from dvtest import dstar_sd as sd
from dvtest import oracle_dstar_sd as orc

pytestmark = pytest.mark.unit


def test_corpus_names_are_unique_and_described():
    cases = orc.corpus()
    names = [n for n, _note, _f in cases]
    assert len(names) == len(set(names)) >= 40
    for name, note, frames in cases:
        assert note and name == name.lower() and " " not in name
        assert frames and all(0 <= seq < 21 and len(w) == 3 for seq, w in frames)
        assert frames[0] == (0, sd.SYNC), name


def test_corpus_has_only_documentation_callsigns_and_text():
    for _name, _note, frames in orc.corpus():
        plain = b"".join(sd.xor3(w) for s, w in frames if s)
        for word in (b"DL3", b"JA1"):
            assert word not in plain
    assert orc.CALL == "N0CALL" and orc.TEXT.startswith("N0CALL") and len(orc.TEXT) == len(orc.TEXT_B) == 20


def test_driver_input_format():
    cases = [("a", "n", [(0, sd.SYNC), (1, bytes.fromhex("1629f5"))]), ("b", "n", [(0, sd.SYNC)])]
    assert orc.driver_input(cases) == "B a\nF 0 552d16\nF 1 1629f5\nE\nB b\nF 0 552d16\nE\n"


def test_parse_driver_output():
    text = "\n".join([
        "[log 2] noise", "D a", "R block 2 40", "R text 8 4e30", "D end", "D b", "R gpsa 14 aabb",
        "R nmea:GPGGA 3 cc", "D end", "stray line", "R text 1 00",
    ])
    out = orc.parse_driver_output(text)
    assert out == {"a": [{"kind": "block", "seq": 2, "hex": "40"}, {"kind": "text", "seq": 8, "hex": "4e30"}],
                   "b": [{"kind": "gpsa", "seq": 14, "hex": "aabb"}, {"kind": "nmea:GPGGA", "seq": 3, "hex": "cc"},
                         {"kind": "text", "seq": 1, "hex": "00"}]}
    assert orc.parse_driver_output("R text 1 00\n") == {}


def test_docker_argv_is_the_contained_form():
    argv = orc.docker_argv("/usr/local/bin/dstarsd-mmdvm", env={})
    joined = " ".join(argv)
    for word in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--memory", "--pids-limit",
                 "--entrypoint /usr/local/bin/dstarsd-mmdvm"):
        assert word in joined
    assert argv[-1] == orc.IMAGE == "dvt-dstarsd:local"


def test_dgw_nmea_line_is_what_the_collector_builds():
    # checksum cut and recomputed as lower case hex, CR LF removed, N0CALL-5>GPS30,DSTAR* in front
    sentence = sd.nmea_sentence("GPGLL,4916.45,N,12311.12,W,225444,A")
    assert orc.dgw_nmea_line(sentence) == "N0CALL-5>GPS30,DSTAR*:$GPGLL,4916.45,N,12311.12,W,225444,A*31"
    assert orc.dgw_nmea_line(sd.nmea_sentence("GPGGA,1,2,3,4,5,6,7,8"))[-3:] == "*" + "%02x" % sd.nmea_checksum("GPGGA,1,2,3,4,5,6,7,8")


def test_views_and_compare_on_a_text_case():
    tb = sd.text_blocks(orc.TEXT)
    frames = sd.render(sd.superframes_to_halves(sd.place(tb, 8)))
    m = orc.ours_mmdvm_view(frames)
    d = orc.ours_dgw_view(frames)
    assert [e["kind"] for e in m].count("block") == 20 and not [e for e in m if e["kind"] == "text"]
    assert [(e["kind"], e["seq"]) for e in d] == [("text", 4)]
    text_hex = orc.TEXT.encode().hex()
    rec = orc.compare("x", frames, [e for e in m], [{"kind": "text", "seq": 4, "hex": text_hex}])
    assert rec["mmdvm_match"] and rec["dgw_match"] and not rec["text_agree"]
    rec = orc.compare("x", frames, [], [])
    assert not rec["mmdvm_match"] and not rec["dgw_match"]


def test_pins_come_from_env_example_not_from_code():
    pins = orc.pins()
    assert set(pins) == {"DSTARSD_MMDVM_REPO", "DSTARSD_MMDVM_COMMIT", "DSTARSD_DGW_REPO", "DSTARSD_DGW_COMMIT",
                         "DSTARSD_BASE_IMAGE"}
    for key in ("DSTARSD_MMDVM_COMMIT", "DSTARSD_DGW_COMMIT"):
        assert len(pins[key]) == 40 and int(pins[key], 16) >= 0
