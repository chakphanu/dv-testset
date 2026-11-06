# SPDX-License-Identifier: MIT
"""The two-decoder oracle for the Kenwood NXDN GPS payload
(dvtest/oracle_nxdn.py) with synthetic decoder outputs. No decoder runs."""
import json
import sys

import pytest

from dvtest import oracle_nxdn as oracle
from dvtest.proto import nxdn, nxdn_l3 as l3
from dvtest.proto.nxdn_l3 import DSDFME, GATEWAY, LON_BOTH, GpsFix

# shapes of the two decoders' output, written by hand from their format strings
GW_LINE = "N0CALL>APDPRS,NXDN*,qAR,N0GW:!1345.37N/10030.10Er090/055 Kenwood NEXEDGE via MMDVM\r\n"
DSD_LINE = ("\n GPS: (13.756300°, 100.500000°) Speed: 1408.000000 k/h; COG: 108.000000; "
            "Elevation: 0; Date: 2026/11/06 Time: 12:34 \n")
FIX = GpsFix(l3.BASE_LAT, l3.BASE_LON, 90.0, 55.0)


def gw_line(fields, source="N0CALL"):
    """Gateway output line for normalised fields (hand format, see GW_LINE)."""
    lat, lon = fields["latitude"], fields["longitude"]
    text = "%s>APDPRS,NXDN*,qAR,N0GW:!%04d.%02d%s/%05d.%02d%sr" % (
        source, int(lat[1:]), fields["latitude_fraction"], lat[0], int(lon[1:]), fields["longitude_fraction"], lon[0])
    if fields["course"] is not None:
        text += "%03d/%03d" % (fields["course"], fields["speed"])
    return text + " Kenwood NEXEDGE via MMDVM\r\n"


def dsd_line(lat, lon, speed, cog, elevation=0):
    return (" GPS: (%f°, %f°) Speed: %f k/h; COG: %f; Elevation: %d; Date: 2026/11/06 Time: 12:34 "
            % (lat, lon, speed, cog, elevation))


def test_parse_gateway_line():
    got = oracle.parse_gateway_line("noise\n" + GW_LINE)
    assert got["source"] == "N0CALL" and got["kind"] == "kenwood"
    assert (got["latitude_hem"], got["latitude_degmin"], got["latitude_hundredths"]) == ("N", 1345, 37)
    assert (got["longitude_hem"], got["longitude_degmin"], got["longitude_hundredths"]) == ("E", 10030, 10)
    assert (got["course"], got["speed"]) == (90, 55)
    assert oracle.normalize_gateway(got) == {
        "latitude": "N1345", "latitude_fraction": 37, "longitude": "E10030", "longitude_fraction": 10,
        "course": 90, "speed": 55}


def test_parse_gateway_line_without_course_and_icom_form_and_last_line_wins():
    short = "N0CALL>APDPRS,NXDN*,qAR,N0GW:!1345.37S/10030.10Wr Kenwood NEXEDGE via MMDVM"
    got = oracle.parse_gateway_line(short)
    assert got["course"] is None and got["speed"] is None
    assert got["latitude_hem"] == "S" and got["longitude_hem"] == "W"
    icom = "N0CALL>APDPRS,NXDN*,qAR,N0GW:!01345.37N/010030.10Er090/055 Icom IDAS via MMDVM"
    assert oracle.parse_gateway_line(icom)["kind"] == "icom"
    assert oracle.parse_gateway_line(short + "\n" + GW_LINE)["course"] == 90
    assert oracle.parse_gateway_line("") is None and oracle.parse_gateway_line("garbage") is None


def test_parse_dsdfme_log():
    got = oracle.parse_dsdfme_log("DATA: 0000\n" + DSD_LINE)
    assert got["latitude_text"] == "13.756300" and got["longitude_text"] == "100.500000"
    assert got["speed_text"] == "1408.000000" and got["cog_text"] == "108.000000"
    assert got["date"] == "2026-11-06" and got["time"] == "12:34"
    assert oracle.normalize_dsdfme(got) == {
        "latitude": "N1345", "latitude_fraction": 37, "longitude": "E10030", "longitude_fraction": 0,
        "course": 108, "speed": 1408}
    assert oracle.parse_dsdfme_log("") is None and oracle.parse_dsdfme_log(" GPS: nothing") is None
    # the report may be split over lines by the terminal
    split = DSD_LINE.replace("Speed", "\n Speed").replace("Date", "\n Date")
    assert oracle.parse_dsdfme_log(split)["time"] == "12:34"


def test_dsdfme_negative_and_truncation():
    parsed = oracle.parse_dsdfme_log(dsd_line(-33.8688, -151.2093, 12.9, 359.9))
    norm = oracle.normalize_dsdfme(parsed)
    assert norm["latitude"].startswith("S3352") and norm["longitude"].startswith("W15112")
    assert norm["course"] == 359 and norm["speed"] == 12            # tenths cut off like the gateway
    zero = oracle.normalize_dsdfme(oracle.parse_dsdfme_log(dsd_line(-0.0, 0.0, 0.0, 0.0)))
    assert zero["latitude"] == "S0"                                  # sign taken from the printed text


def test_record_disagreement_on_the_three_disputed_fields():
    user = l3.kenwood_full(FIX, GATEWAY)
    record = oracle.build_record("base-c90-s55", user, gateway_text=GW_LINE, dsdfme_text=DSD_LINE,
                                 layout="gateway")
    assert record["schema"] == oracle.SCHEMA and record["verdict"] == "disagree"
    assert record["disputed_fields"] == ["longitude_fraction", "course", "speed"]
    assert record["disagreements"] == ["longitude_fraction", "course", "speed"]
    rows = {row["field"]: row for row in record["fields"]}
    assert rows["longitude_fraction"]["gateway"] == 10 and rows["longitude_fraction"]["dsdfme"] == 0
    assert (rows["course"]["gateway"], rows["course"]["dsdfme"]) == (90, 108)
    assert (rows["speed"]["gateway"], rows["speed"]["dsdfme"]) == (55, 1408)
    assert rows["latitude"]["agree"] and rows["longitude"]["agree"] and rows["latitude_fraction"]["agree"]
    # which one matches the encoded input
    assert all(rows[f]["gateway_matches_input"] for f in oracle.FIELDS)
    assert not rows["course"]["dsdfme_matches_input"] and rows["latitude"]["dsdfme_matches_input"]
    # my reading of both sources predicted both outputs
    assert record["gateway"]["prediction_ok"] is True and record["dsdfme"]["prediction_ok"] is True
    assert record["input"]["user_data_hex"] == user.hex() and record["input"]["kind"] == "full"
    assert record["input"]["message_hex"] == (user + l3.message_crc32(user).to_bytes(4, "big")).hex()
    # exact values of both decoders are in the record
    assert record["dsdfme"]["output"]["speed_text"] == "1408.000000"
    assert record["gateway"]["output"]["line"].startswith("N0CALL>APDPRS")
    # JSON round trip
    assert json.loads(oracle.to_json(record)) == record


def test_record_prediction_miss_is_flagged():
    user = l3.kenwood_full(FIX, GATEWAY)
    wrong = DSD_LINE.replace("COG: 108.000000", "COG: 90.000000")
    record = oracle.build_record("x", user, gateway_text=GW_LINE, dsdfme_text=wrong)
    assert record["dsdfme"]["prediction_ok"] is False and record["gateway"]["prediction_ok"] is True
    assert record["disputed_fields"] == ["longitude_fraction", "speed"]


def test_record_agree_when_the_input_hides_the_dispute():
    fix = GpsFix(l3.BASE_LAT, l3.BASE_LON, 0.0, 0.0)
    user = l3.kenwood_full(fix, LON_BOTH)
    gw = gw_line(oracle.normalize_fields(l3.parse_kenwood_fields(user, GATEWAY)))
    dsd = dsd_line(13.7563, 100.5018, 0.0, 0.0)
    record = oracle.build_record("base-c0-s0", user, gateway_text=gw, dsdfme_text=dsd, layout="lon-both")
    assert record["verdict"] == "agree" and record["disagreements"] == []
    assert record["gateway"]["prediction_ok"] and record["dsdfme"]["prediction_ok"]


def test_record_layouts_swap_who_is_right():
    user = l3.kenwood_full(FIX, DSDFME)
    pred = oracle.predict(user)
    record = oracle.build_record("base-c90-s55-dsd", user, gateway_text=gw_line(pred["gateway"]),
                                 dsdfme_text=dsd_line(13.7563, 100.5018, 55.0, 90.0), layout="dsdfme")
    assert record["verdict"] == "disagree"
    rows = {row["field"]: row for row in record["fields"]}
    assert rows["course"]["dsdfme_matches_input"] and not rows["course"]["gateway_matches_input"]
    assert rows["speed"]["dsdfme_matches_input"] and not rows["speed"]["gateway_matches_input"]
    assert rows["longitude_fraction"]["dsdfme_matches_input"] and not rows["longitude_fraction"]["gateway_matches_input"]
    assert record["gateway"]["prediction_ok"] and record["dsdfme"]["prediction_ok"]


def test_record_missing_output():
    user = l3.kenwood_full(FIX)
    assert oracle.build_record("x", user)["verdict"] == "no-output"
    only_gw = oracle.build_record("x", user, gateway_text=GW_LINE)
    assert only_gw["verdict"] == "no-output-dsdfme" and only_gw["disagreements"] == list(oracle.FIELDS)
    only_dsd = oracle.build_record("x", user, dsdfme_text=DSD_LINE)
    assert only_dsd["verdict"] == "no-output-gateway"
    assert only_dsd["dsdfme"]["normalized"]["course"] == 108


def test_record_short_message_and_packets():
    user = l3.kenwood_short(FIX)
    gw = gw_line({"latitude": "N1345", "latitude_fraction": 37, "longitude": "E10030",
                  "longitude_fraction": 10, "course": None, "speed": None})
    packets = l3.data_call_packets(1001, 31672, user)
    record = oracle.build_record("short", user, gateway_text=gw, packets=packets)
    assert record["input"]["kind"] == "short" and record["verdict"] == "no-output-dsdfme"
    assert record["gateway"]["prediction_ok"] is True
    assert len(record["input"]["packets_hex"]) == len(packets)
    assert all(row["gateway_matches_input"] for row in record["fields"])


def test_icom_packet_framing():
    packets = l3.data_call_packets(1001, 31672, l3.kenwood_full(FIX))
    wrapped = oracle.icom_packets(packets)
    assert [len(p) for p in wrapped] == [102] * 5
    first = wrapped[0]
    assert first[:8] == b"ICOM\x01\x01\x08\xe0" and first[8:37] == bytes(29)
    assert first[37:40] == bytes([0x23, 0x02, 0x18]) and first[40:73] == packets[0][10:]
    assert first[73:] == bytes(29)
    assert oracle.parse_icom_packet(first) == packets[0][10:]
    assert oracle.parse_icom_packet(first[:101]) is None and oracle.parse_icom_packet(b"X" * 102) is None
    voice = nxdn.control_payload(nxdn.VCALL, 1, 2)
    assert oracle.icom_packet(voice)[37:40] == bytes([0x23, 0x1C, 0x21])
    with pytest.raises(ValueError):
        oracle.icom_packet(bytes(32))
    request = oracle.icom_connect_request()
    assert len(request) == 102 and request[:6] == b"ICOM\x01\x61"


def test_run_command_wrapper():
    done = oracle.run_command([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err')"])
    assert done["returncode"] == 0 and done["stdout"].strip() == "out" and done["stderr"] == "err"
    assert not done["timed_out"]
    echoed = oracle.run_command([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                                stdin=b"abc")
    assert echoed["stdout"] == "abc"
    slow = oracle.run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    assert slow["timed_out"] and slow["returncode"] is None
    failing = oracle.run_command([sys.executable, "-c", "raise SystemExit(3)"])
    assert failing["returncode"] == 3
    for bad in ([], "echo hi"):
        with pytest.raises(ValueError):
            oracle.run_command(bad)


def test_main_writes_record_and_exit_status(tmp_path, capsys):
    user = l3.kenwood_full(FIX)
    gw_file, dsd_file, out = tmp_path / "gw.txt", tmp_path / "dsd.log", tmp_path / "rec.json"
    gw_file.write_text(GW_LINE)
    dsd_file.write_text(DSD_LINE)
    args = ["--case", "base-c90-s55", "--user-data", user.hex(), "--gateway-out", str(gw_file),
            "--dsdfme-log", str(dsd_file), "--out", str(out)]
    assert oracle.main(args) == 1
    assert json.loads(out.read_text())["verdict"] == "disagree"
    assert oracle.main(["--case", "c", "--user-data", user.hex(), "--dsdfme-log", str(dsd_file),
                        "--out", str(tmp_path / "half.json")]) == 2
    assert json.loads((tmp_path / "half.json").read_text())["verdict"] == "no-output-gateway"
    capsys.readouterr()
    assert oracle.main(["--case", "c", "--user-data", user.hex()]) == 2
    assert json.loads(capsys.readouterr().out)["verdict"] == "no-output"


# ------------------------------------------------------------------ NXDNGateway container run (pure parts)

def _capture(*cases):
    """Entry script stdout for (name, [aprs lines]) pairs, as mosquitto_sub prints it."""
    out = ["noise before the first marker"]
    for name, lines in cases:
        out.append("dvt/case\tbegin %s" % name)
        for line in lines:
            out.append("aprs-gateway/aprs\t" + line.rstrip("\r\n") + "\r")
            out.append("")                      # the payload's own line end plus the one of -F
    out.append("dvt/case\tend")
    return "\n".join(out) + "\n"


def test_gateway_cases_table():
    cases = oracle.gateway_cases()
    rows = len(l3.KNOWN_POSITIONS)
    assert len(cases) == 3 * rows + rows + 2 + rows
    assert len({c["name"] for c in cases}) == len(cases)
    assert all(" " not in c["name"] and "\t" not in c["name"] for c in cases)
    kinds = {}
    for case in cases:
        kinds.setdefault(case["kind"], []).append(case)
    assert {k: len(v) for k, v in kinds.items()} == {
        "kenwood-full": 3 * rows, "kenwood-short": rows, "kenwood-nofix-full": 1,
        "kenwood-nofix-short": 1, "icom": rows}
    base = next(c for c in cases if c["name"] == "dsdfme.base-c90-s55.kenwood-full")
    assert base["user_data"] == l3.kenwood_full(base["fix"], DSDFME)
    assert next(c for c in cases if c["kind"] == "icom")["user_data"][0] == 0x06
    assert oracle.gateway_cases([("only", FIX)])[0]["name"] == "gateway.only.kenwood-full"


def test_gateway_input_lines():
    cases = oracle.gateway_cases([("only", FIX)])
    lines = oracle.gateway_input(cases).splitlines()
    expected = sum(len(l3.data_call_packets(oracle.GATEWAY_SRC_ID, oracle.GATEWAY_DST_TG, c["user_data"]))
                   for c in cases)
    assert len(lines) == expected
    for line in lines:
        name, hexa = line.split(" ")
        assert name in {c["name"] for c in cases}
        assert len(bytes.fromhex(hexa)) == oracle.ICOM_SIZE
    first = bytes.fromhex(lines[0].split(" ")[1])
    assert first[:4] == b"ICOM" and first[40] == 0x90      # LICH of a data frame
    # one case after the other, no interleaving
    names = [line.split(" ")[0] for line in lines]
    assert names == sorted(names, key=lambda n: [c["name"] for c in cases].index(n))


def test_split_gateway_capture():
    text = _capture(("a", [GW_LINE]), ("b", []), ("c", [GW_LINE, GW_LINE]))
    got = oracle.split_gateway_capture(text)
    assert got["order"] == ["a", "b", "c"] and got["complete"] is True
    assert len(got["cases"]["a"]) == 1 and got["cases"]["b"] == [] and len(got["cases"]["c"]) == 2
    assert oracle.parse_gateway_line(got["cases"]["a"][0])["course"] == 90
    cut = oracle.split_gateway_capture(text.rsplit("dvt/case\tend", 1)[0])
    assert cut["complete"] is False
    assert oracle.split_gateway_capture("") == {"cases": {}, "order": [], "complete": False}


def test_gateway_docker_argv(tmp_path):
    argv = oracle.gateway_docker_argv(str(tmp_path / "p.txt"), "dvt-nxdngw-1", env={})
    assert argv[:2] == ["docker", "run"] and "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == argv[argv.index("--memory-swap") + 1]
    assert "--cpus" in argv and "--pids-limit" in argv and "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[-3:] == [oracle.GATEWAY_IMAGE, "run", "/in/packets.txt"]
    assert any(word.endswith(":/in/packets.txt:ro") for word in argv)


def test_gateway_records_from_capture():
    cases = oracle.gateway_cases([("base", FIX)])
    lines = {
        "gateway.base.kenwood-full": [GW_LINE],
        "dsdfme.base.kenwood-full": ["N0CALL>APDPRS,NXDN*,qAR,N0GW:!1345.37N/10030.00Er211/130 Kenwood NEXEDGE via MMDVM\r\n"],
        "lon-both.base.kenwood-full": [GW_LINE],
        "gateway.base.kenwood-short": ["N0CALL>APDPRS,NXDN*,qAR,N0GW:!1345.37N/10030.10Er Kenwood NEXEDGE via MMDVM\r\n"],
        "icom.base.icom": ["N0CALL>APDPRS,NXDN*,qAR,N0GW:!1345.38N/10030.11Er090/055 Icom IDAS via MMDVM\r\n"],
    }
    capture = oracle.split_gateway_capture(_capture(*[(c["name"], lines.get(c["name"], [])) for c in cases]))
    records = {r["case"]: r for r in oracle.gateway_records(cases, capture)}
    assert len(records) == len(cases)
    gw = records["gateway.base.kenwood-full"]
    assert gw["verdict"] == "no-output-dsdfme" and gw["gateway"]["prediction_ok"] is True
    assert all(f["gateway_matches_input"] for f in gw["fields"])
    dsd = records["dsdfme.base.kenwood-full"]
    assert dsd["gateway"]["prediction_ok"] is True
    assert [f["field"] for f in dsd["fields"] if not f["gateway_matches_input"]] == [
        "longitude_fraction", "course", "speed"]
    assert records["gateway.base.kenwood-short"]["kind"] == "kenwood-short"
    icom = records["icom.base.icom"]
    assert icom["mismatches"] == ["latitude_fraction", "longitude_fraction"]      # rounded, not truncated
    assert icom["input"]["intended"]["latitude_fraction"] == 37 and icom["gateway"]["normalized"]["latitude_fraction"] == 38
    assert all(r["gateway_lines"] is not None for r in records.values())


def test_gateway_records_no_fix_and_missing_output():
    cases = oracle.gateway_cases([])                   # only the two no-fix rows
    assert [c["kind"] for c in cases] == ["kenwood-nofix-full", "kenwood-nofix-short"]
    capture = oracle.split_gateway_capture(_capture(*[(c["name"], []) for c in cases]))
    assert [r["verdict"] for r in oracle.gateway_records(cases, capture)] == ["no-output-gateway"] * 2
    missing = oracle.gateway_records(oracle.gateway_cases([("base", FIX)]),
                                     oracle.split_gateway_capture(""))
    assert all(r["verdict"] in ("no-output", "no-output-gateway") and r["gateway_lines"] is None
               for r in missing)
    assert {r["verdict"] for r in missing if r["kind"] == "kenwood-full"} == {"no-output"}


def test_gateway_reading_of_speed_nibble_over_9():
    """GW prints the whole part of the speed only: a low nibble over 9 in byte 25
    (what the dsd-fme layout writes) must not be folded into the whole part."""
    user = bytearray(l3.kenwood_full(GpsFix(l3.BASE_LAT, l3.BASE_LON, 12.3, 1.0), DSDFME))
    assert user[25] == 10 and user[26] == 0
    predicted = oracle.predict(bytes(user))["gateway"]
    assert predicted["speed"] == 0 and predicted["course"] == 196
