# SPDX-License-Identifier: MIT
"""dvtest.oracle_direwolf: case tables, log parsing and record building with synthetic
container results. No container is run."""
import json
import re

import pytest

from dvtest import ax25
from dvtest import oracle_direwolf as o

SAMPLE_LOG = """
<<< Rejected Noise from KISS client application, total length = 2
  000:  41 42                                            AB

<<< Data frame from KISS client application, channel 0, total length = 41
  000:  c0 00 82 a0 a4 a6 40 40 e0 9c 60 86 82 98 98 ef  ......@@..`.....
  010:  03 f0 21 30 30 30 30 2e 30 30 4e 2f 30 30 30 30  ..!0000.00N/0000
  020:  30 2e 30 30 45 3e 68 69 c0                       0.00E>hi.

Packet content after removing KISS framing and any escapes:
  000:  82 a0 a4 a6 40 40 e0 9c 60 86 82 98 98 ef 03 f0  ....@@..`.......
  010:  21 30 30 30 30 2e 30 30 4e 2f 30 30 30 30 30 2e  !0000.00N/00000.
  020:  30 30 45 3e 68 69                                00E>hi

>>> Data frame to KISS client application, channel 0, total length = 41
  000:  c0 00 82 a0 a4 a6 40 40 e0 9c 60 86 82 98 98 ef  ......@@..`.....
[0L] N0CALL-7>APRS:!0000.00N/00000.00E>hi
Attached to KISS TCP client application 2 on port 8001 ...
KISS protocol set TXDELAY = 30 (*10mS units = 300 mS), chan 0
Frame length 14 not in allowable range of 15 to 2123.
"""


def test_case_tables_are_sane():
    names = [n for n, _t, _w in o.RX_CASES + o.RX_NEGATIVE_CASES]
    assert len(names) == len(set(names))
    for n, text, _why in o.RX_CASES:
        ax25.from_tnc2(text)                       # our encoder accepts every positive case
    txn = [n for n, _b, _w in o.tx_cases()]
    assert len(txn) == len(set(txn))
    assert {"data-ui", "data-escapes", "param-sethardware", "no-leading-fend", "info-2029", "info-2030"} <= set(txn)
    for n, b, _w in o.tx_cases():
        assert isinstance(b, bytes) and b


def test_no_real_callsigns_in_the_corpus():
    # only the documentation names the repository allows
    allowed = {"N0CALL", "N0TEST", "N0DIG", "WIDE1", "WIDE2", "APRS", "APDW18", "APLRT1", "APLRG1", "ABCDEF",
               "GHIJKL", "A", "B", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"}
    texts = [t for _n, t, _w in o.RX_CASES + o.RX_NEGATIVE_CASES] + [t for _n, t in o.TX_KISSUTIL_CASES]
    for text in texts:
        head = text.partition(":")[0]
        for tok in re.split(r"[>,*]", head):
            tok = tok.strip()
            if tok:
                assert tok.split("-")[0].upper() in allowed, tok


def test_tx_cases_use_their_stated_shape():
    d = {n: b for n, b, _w in o.tx_cases()}
    assert d["data-ui"][:2] == b"\xc0\x00" and d["data-ui"][-1] == 0xC0
    assert d["no-leading-fend"][0] != 0xC0
    assert d["garbage-before"][:2] == b"AB"
    assert ax25.kiss_decode(d["data-port1"])[0][:2] == (1, 0)
    assert len(d["info-2029"]) == 2048 and len(d["info-2030"]) == 2049      # FEND to FEND
    assert d["bad-escape"].count(bytes([ax25.FESC, 0x41])) == 1


def test_parse_debug_log():
    out = o.parse_debug_log(SAMPLE_LOG)
    assert [(r["kind"], r["channel"], r["length"]) for r in out["received"]] == [
        ("Rejected Noise", None, 2), ("Data frame", 0, 41)]
    assert out["received"][0]["hex"] == "4142"
    assert out["received"][1]["hex"].startswith("c00082a0a4a6")
    assert len(out["unwrapped"]) == 1 and len(out["unwrapped"][0]) // 2 == 38
    # the transmit echo and the attach line are dropped, the TNC's own words stay
    assert "KISS protocol set TXDELAY = 30 (*10mS units = 300 mS), chan 0" in out["messages"]
    assert "Frame length 14 not in allowable range of 15 to 2123." in out["messages"]
    assert not any(m.startswith("[0L]") or m.startswith("Attached") for m in out["messages"])


def test_parse_kissutil():
    text = ("From KISS TNC:\n  000:  c0 00 82 c0\n[0] N0CALL-7>APRS:hi\nSave received frame to /tmp/krx/x\n"
            "Frame length 3 not in allowable range of 15 to 2123.\nERROR - Invalid KISS data frame from TNC.\n")
    out = o.parse_kissutil(text)
    assert out["frames"] == ["[0] N0CALL-7>APRS:hi"]
    assert len(out["refused"]) == 2


def test_rx_records_with_synthetic_result():
    frame = ax25.from_tnc2("A>B:x")
    kiss = ax25.kiss_encode(0, 0, frame)
    bits = ax25.air_bits(frame, preamble_flags=o.PRE_FLAGS, postamble_flags=o.POST_FLAGS)
    result = {"kiss_rx_hex": (b"\xc0" + kiss[1:] + b"\xc0").hex(),
              "cases": [
                  {"name": "calls-short", "tnc2": "A>B:x", "generated": True, "samples": int(bits * 36.75) + 1170,
                   "sample_rate": 44100, "atest_frame_hex": frame.hex(), "atest_text": "A>B:x"},
                  {"name": "neg-lowercase", "tnc2": "n0call>aprs:lower", "generated": False, "samples": 0,
                   "sample_rate": 44100, "atest_frame_hex": "", "atest_text": ""}]}
    recs = o.rx_records(result)
    ok, neg = recs
    assert ok["ax25_match"] and ok["kiss_match"] and ok["kiss_data_equals_ax25"]
    assert ok["ours_air_bits"] == bits and abs(ok["quiet_samples"] - 1170) < 1
    assert ok["ours_frame_air_bits"] == ax25.air_bits(frame)
    assert neg["dw_generated"] is False and neg["ax25_match"] is False and "ours_error" in neg
    json.dumps(recs)


def test_rx_records_detect_a_wrong_kiss_frame():
    frame = ax25.from_tnc2("A>B:x")
    wrong = ax25.kiss_encode(0, 0, frame[:-1] + b"y")
    result = {"kiss_rx_hex": wrong.hex(), "cases": [
        {"name": "calls-short", "tnc2": "A>B:x", "generated": True, "samples": 100, "sample_rate": 44100,
         "atest_frame_hex": frame.hex(), "atest_text": "A>B:x"}]}
    rec = o.rx_records(result)[0]
    assert rec["kiss_match"] is False and rec["kiss_data_equals_ax25"] is False


def test_tx_records_with_synthetic_result():
    name, sent, _why = o.tx_cases()[0]
    result = {"cases": [{"name": name, "send_hex": sent.hex(), "client_rx_hex": "", "copy_hex": sent.hex(),
                         "kissutil_text": "[0] N0CALL-7>APRS:!0000.00N/00000.00E>hi\n", "direwolf_log": SAMPLE_LOG},
                        {"name": "ku-x", "tnc2": "A>B:x", "copy_hex": "", "kissutil_text": "", "direwolf_log": ""}]}
    recs = o.tx_records(result)
    assert len(recs) == 1 and recs[0]["dw_copy_equals_sent"] is True
    assert recs[0]["ours_decode"][0]["port"] == 0 and recs[0]["kissutil_frames"]


def test_kissutil_records_compare_bytes():
    ours = ax25.kiss_encode(0, 0, ax25.from_tnc2("A>B:x"))
    recs = o.kissutil_records({"cases": [{"name": "ku-1", "tnc2": "A>B:x", "copy_hex": ours.hex()},
                                         {"name": "ku-2", "tnc2": "A>B:x", "copy_hex": "c000c0"},
                                         {"name": "data-ui", "copy_hex": ""}]})
    assert [r["kiss_match"] for r in recs] == [True, False]


def test_docker_argv_is_contained():
    argv = o.docker_argv("dvt-direwolf-1", env={"DVT_CTR_MEM": "512m"})
    s = " ".join(argv)
    for word in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--rm", "--pids-limit",
                 "--memory 512m", "--memory-swap 512m"):
        assert word in s
    assert argv[-1] == "dvt-direwolf:local"
    assert "-v" not in argv and "--privileged" not in argv


def test_jobs_are_json():
    json.dumps(o.rx_job())
    json.dumps(o.tx_job())
    assert o.rx_job()["mode"] == "rx" and o.tx_job()["mode"] == "tx"


def test_volatile_case_keeps_only_lengths():
    name = "info-2030"
    sent = dict((n, b) for n, b, _w in o.tx_cases())[name]
    junk = bytes(range(256)) * 9
    result = {"cases": [{"name": name, "send_hex": sent.hex(), "client_rx_hex": "", "copy_hex": junk.hex(),
                         "kissutil_text": "", "direwolf_log": SAMPLE_LOG}]}
    rec = o.tx_records(result)[0]
    assert rec["volatile"] is True and rec["dw_copy_len"] == len(junk)
    assert "dw_copy_hex" not in rec and "dw_unwrapped_hex" not in rec
    assert all("hex" not in r for r in rec["dw_received"])
    blob = json.dumps(rec)
    assert junk[:16].hex() not in blob
