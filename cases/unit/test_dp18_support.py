# SPDX-License-Identifier: MIT
"""cases/dp18_support.py, the DP18 builders of dvtest/proto/aprs.py and the case flows of cases/test_dp18_origin.py / test_dp18_rf.py against
fakes (no rig, no sleeps beyond milliseconds). The expected APRS lines are hand-written from APRS101 chapters 6, 8 and 11."""
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dp18_support as dc  # noqa: E402
import test_dp18_origin as to  # noqa: E402
import test_dp18_rf as trf  # noqa: E402

from dvtest import aprsis_client as ai, ax25, kiss_tnc, metrics, sut_config  # noqa: E402
from dvtest.proto import aprs  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered(files, tx="true"):
    extras = [(SNIPPETS / name).read_text() for name in files]
    return tomllib.loads(sut_config.render(BASE, "172.30.0", dp15_tx_enable=tx, extras=extras))


ORIGIN = ["dp15-aprsis.toml", "dp18-origin.toml"]


# ---------------------------------------------------------------- the APRS builders (hand-written literals)

def test_position_and_object_lines_match_hand_written_aprs101():
    assert aprs.lat_text_e7(490583333) == "4903.50N" and aprs.lon_text_e7(-720291667) == "07201.75W"          # 49 03.50 N, 72 01.75 W
    assert aprs.lat_text_e7(-125000000) == "1230.00S" and aprs.lon_text_e7(1799999999) == "18000.00E"            # carry into the degrees
    assert aprs.position_report(105000000, 202500000, "/&", "dvt") == "!1030.00N/02015.00E&dvt"
    assert aprs.object_report("DVTOBJ1", "123456", 105500000, 202500000, "/r", "144.800MHz room=B dvt") == \
        ";DVTOBJ1  *123456h1033.00N/02015.00Er144.800MHz room=B dvt"
    assert aprs.object_report("A", "000000", 105000000, 202500000, "/r", live=False).startswith(";A        _000000h")


def test_position_ambiguity_blanks_trailing_digits_of_both_coordinates():
    for level, lat, lon in [(1, "1030.0 N", "02015.0 E"), (2, "1030.  N", "02015.  E"), (3, "103 .  N", "0201 .  E"), (4, "10  .  N", "020  .  E")]:
        assert aprs.position_report(105000000, 202500000, "/&", "", level) == "!%s/%s&" % (lat, lon)


def test_object_and_position_parsers_round_trip():
    info = aprs.object_report("DVTOBJ1", "235959", 105500000, 202500000, "/r", "144.800MHz room=B dvt")
    obj = aprs.parse_object(info)
    assert (obj["name"], obj["live"], obj["hms"], obj["lat"], obj["lon"], obj["comment"]) == ("DVTOBJ1", True, "235959", "1033.00N", "02015.00E", "144.800MHz room=B dvt")
    assert aprs.parse_position_report("!1030.00N/02015.00E&dvt")["comment"] == "dvt"
    for bad in [lambda: aprs.object_report("TOOLONGNAME", "123456", 0, 0), lambda: aprs.object_report("A", "126000", 0, 0),
                lambda: aprs.parse_object("!1030.00N/02015.00E&"), lambda: aprs.lat_text_e7(900000001), lambda: aprs.position_report(0, 0, "/")]:
        with pytest.raises(ValueError):
            bad()


# ---------------------------------------------------------------- the rendered config

def test_origin_shape_from_the_real_snippet_and_the_stagger():
    shp = dc.origin_shape(rendered(ORIGIN))
    assert shp.gateway == "N0TEST-5" and shp.tocall == "APZDVR" and not shp.withheld
    assert [(r.kind, r.index, r.first_due_s) for r in shp.reports] == [("beacon", 0, 600.0), ("object", 1, 635.0)]
    assert dc.object_comment(shp.objects[0]) == "144.800MHz room=B dvt"
    assert dc.expected_info(shp.beacon) == "!1030.00N/02015.00E&dvt dp18 beacon"
    assert dc.expected_info(shp.objects[0], "010203") == ";DVTOBJ1  *010203h1033.00N/02015.00Er144.800MHz room=B dvt"
    with pytest.raises(ValueError):
        dc.expected_info(shp.objects[0])


def test_origin_shape_withheld_and_missing_pieces():
    assert dc.origin_shape(rendered(ORIGIN + ["dp18-origin-withheld.toml"])).withheld is True
    assert dc.origin_shape(rendered(["dp15-aprsis.toml"])) is None                                  # edge but no report
    assert dc.origin_shape({}) is None
    doc = rendered(ORIGIN)
    doc["data"] = {"positions": False}
    assert dc.origin_shape(doc).withheld is True


def test_objects_snippet_has_ten_staggered_rf_copies():
    shp = dc.origin_shape(rendered(["dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-objects.toml"]))
    assert len(shp.reports) == 11 and all(r.rf_ports == ["rf-a"] for r in shp.reports)
    assert [r.first_due_s for r in shp.reports][:3] == [600.0, 635.0, 670.0] and shp.reports[-1].first_due_s == 950.0
    names = [r.name for r in shp.objects]
    assert names[0] == "DVTOB01" and names[-1] == "DVTOB10" and len(set(names)) == 10
    assert len({(r.lat_e7, r.lon_e7) for r in shp.objects}) == 10


def test_sim_snippet_has_one_real_and_one_simulated_port_on_one_physical():
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml"])
    assert dc.tx_mode(doc, "rf-a") is True and dc.tx_mode(doc, "rf-b") == "simulate" and dc.tx_mode(doc, "nope") is None
    ports = doc["edges"]["ax25"]["port"]
    assert {p["physical"] for p in ports} == {"radio1"} and doc["edges"]["ax25"]["callsign"] == doc["edges"]["aprsis"]["callsign"] == "N0TEST-5"
    assert dc.port_index(doc, "rf-b") == 1 and dc.port_index(doc, "nope") is None
    assert {p["to"] for p in doc["data"]["path"] if p["name"].startswith("rf-out")} == {"edge:ax25/rf-a", "edge:ax25/rf-b"}


# ---------------------------------------------------------------- lines at the aprsc

def line(info, src="N0TEST-5", q="qAC,SRV", path="TCPIP*"):
    return ai.parse_line("%s>APZDVR,%s,%s:%s" % (src, path, q, info))


def test_check_origin_line_accepts_the_node_line_and_names_each_difference():
    shp = dc.origin_shape(rendered(ORIGIN))
    beacon = dc.expected_info(shp.beacon)
    assert dc.check_origin_line(line(beacon), shp.beacon, shp) == []
    obj = dc.expected_info(shp.objects[0], "101010")
    assert dc.check_origin_line(line(obj), shp.objects[0], shp) == []
    assert any("TCPIP*" in p for p in dc.check_origin_line(line(beacon, path="WIDE1-1"), shp.beacon, shp))
    assert any("qAC" in p for p in dc.check_origin_line(line(beacon, q="qAS,SRV"), shp.beacon, shp))
    assert any("2 q constructs" in p for p in dc.check_origin_line(line(beacon, q="qAC,SRV,qAO,X"), shp.beacon, shp))
    assert any("source" in p for p in dc.check_origin_line(line(beacon, src="N0TEST-7"), shp.beacon, shp))
    assert any("information" in p for p in dc.check_origin_line(line(beacon + " "), shp.beacon, shp))
    assert any("not an object" in p for p in dc.check_origin_line(line(beacon), shp.objects[0], shp))
    assert dc.classify(line(beacon), shp) == "beacon" and dc.classify(line(obj), shp) == "object:DVTOBJ1"
    assert dc.classify(line(beacon, src="N0TEST-7"), shp) is None and dc.classify(line(":N0TEST-7 :hi"), shp) is None


def test_hms_offset_and_tolerance_across_midnight():
    import calendar
    epoch = calendar.timegm((2026, 11, 10, 12, 34, 56))
    assert dc.hms_offset_s("123456", epoch) == 0 and dc.hms_offset_s("123455", epoch + 0.4) == pytest.approx(1.4)
    assert dc.hms_ok("123456", epoch + 0.3) and dc.hms_ok("123455", epoch + 0.4) and not dc.hms_ok("123450", epoch) and not dc.hms_ok("123500", epoch)
    midnight = calendar.timegm((2026, 11, 10, 0, 0, 0))
    assert dc.hms_offset_s("235959", midnight + 0.3) == pytest.approx(1.3) and dc.hms_ok("235959", midnight + 0.3)
    assert dc.hms_offset_s("235959", midnight + 1.0) == pytest.approx(2.0) and not dc.hms_ok("235959", midnight + 1.0)


def test_quiet_window_and_wall_of():
    assert dc.quiet_window_s(600, 15) == 585 and dc.quiet_window_s(10, 15) == 0.0
    assert dc.wall_of(90.0, now_wall=1000.0, now_mono=100.0) == 990.0


# ---------------------------------------------------------------- counters and frames

def samples(text):
    return metrics.parse(text)


def test_counter_readers():
    text = "\n".join([
        'dv_pkt_origin_total{kind="beacon",result="generated"} 2', 'dv_pkt_origin_total{kind="object",result="withheld"} 3',
        'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} 4', 'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="dropped_quota"} 6',
        'dv_edge_rf_tx_total{port="rf-b",kind="text",result="simulated"} 5', 'dv_edge_modem_total{port="rf-b",result="rx"} 9'])
    s = samples(text)
    assert dc.origin_counts(s) == {("beacon", "generated"): 2, ("object", "withheld"): 3}
    a = dc.rf_tx_counts(s, "rf-a")
    assert a == {("beacon", "sent"): 4, ("beacon", "dropped_quota"): 6} and dc.drops(a) == {"dropped_quota": 6}
    assert dc.result_total(dc.rf_tx_counts(s, "rf-b"), "simulated") == 5
    assert dc.port_family_total(s, "dv_edge_modem_total", "rf-b") == (9, True) and dc.port_family_total(s, "dv_edge_rf_ptt_total", "rf-b") == (0, False)


def test_object_frames_and_gaps():
    objf = kiss_tnc.ui_frame("N0TEST-5", "APZDVR", aprs.object_report("DVTOB01", "010203", 105000000, 202500000, "/r", "x"))
    beaconf = kiss_tnc.ui_frame("N0TEST-5", "APZDVR", aprs.position_report(105000000, 202500000))
    got = dc.object_frames([(1.0, 0, objf), (2.0, 0, beaconf), (3.0, 0, b"\x00junk")], ax25)
    assert len(got) == 1 and got[0][1] == "DVTOB01" and got[0][2] == "N0TEST-5"
    assert dc.spread_gaps([40.0, 5.0, 75.5]) == [35.0, 35.5]


# ---------------------------------------------------------------- DVT_DP18STRICT: conservation, floor, aprsc side

def frame_of(info):
    return kiss_tnc.ui_frame("N0TEST-5", "APZDVR", info)


def obj_frame(name, hms):
    return frame_of(aprs.object_report(name, hms, 105000000, 202500000, "/r", "x"))


def test_strict_switch_reads_only_the_value_on():
    assert dc.strict_on({"DVT_DP18STRICT": "on"}) is True
    for value in ("", "1", "true", "ON", "off"):
        assert dc.strict_on({"DVT_DP18STRICT": value}) is False
    assert dc.strict_on({}) is False


def test_txcnt1_switch_reads_only_the_value_on():
    assert dc.txcnt1_on({"DVT_TXCNT1": "on"}) is True
    for value in ("", "1", "true", "ON", "off"):
        assert dc.txcnt1_on({"DVT_TXCNT1": value}) is False
    assert dc.txcnt1_on({}) is False


def test_txcnt1_delayed_is_bounded_by_the_copies():
    assert dc.txcnt1_problems(0, 22) == []
    assert dc.txcnt1_problems(22, 22) == []
    bad = dc.txcnt1_problems(8932, 22)
    assert len(bad) == 1 and "8932" in bad[0] and "22" in bad[0]
    assert len(dc.txcnt1_problems(8932, 22, 3, 0)) == 1                      # the per-tick defect fails the upper bound only


def test_txcnt1_upper_bound_allows_the_copies_queued_at_the_start():
    assert dc.txcnt1_problems(24, 22, 0, 2) == []                           # 2 copies queued at the baseline were first held inside the run
    assert len(dc.txcnt1_problems(25, 22, 0, 2)) == 1
    assert len(dc.txcnt1_problems(23, 22, 0, None)) == 1                    # unreadable gauge: the plain bound


def test_txcnt1_lower_bound_every_ttl_expiry_was_held():
    # run R shape on a fixed image: 22 copies, sent 8, dropped_queue 4, dropped_ttl 3, 7 queued: delayed between 3 and 22
    assert dc.txcnt1_problems(3, 22, 3, 0) == []
    assert dc.txcnt1_problems(10, 22, 3, 0) == []
    bad = dc.txcnt1_problems(2, 22, 3, 0)
    assert len(bad) == 1 and "dropped_ttl" in bad[0] and "at least 3" in bad[0]
    assert dc.txcnt1_problems(1, 22, 3, 2) == []                            # 2 queued at the start may have been held before the baseline
    assert len(dc.txcnt1_problems(0, 22, 3, 2)) == 1
    assert dc.txcnt1_problems(0, 22, 3, None) == []                         # baseline unreadable: no lower bound
    assert len(dc.txcnt1_problems(30, 22, 30, 0)) == 1 and len(dc.txcnt1_problems(1, 22, 30, 0)) == 1   # both bounds named when both fail


def test_edge_drops_reads_the_reason_counter_without_a_port():
    smp = samples('dv_edge_drops_total{reason="rf_fenced",proc="e"} 4\ndv_edge_drops_total{reason="rf_fenced",proc="e2"} 1\n'
                  'dv_edge_drops_total{reason="end_dropped"} 9\ndv_edge_rf_tx_total{port="rf-a",kind="beacon",result="rf_fenced"} 7\n')
    assert dc.edge_drops(smp, "rf_fenced") == 5                              # not a dv_edge_rf_tx_total result
    assert dc.edge_drops(smp, "nope") == 0 and dc.edge_drops([], "rf_fenced") == 0


def test_queue_gauge_and_dropped_totals_read_the_hub_text():
    text = ('dv_edge_rf_queue{port="rf-a",proc="x"} 3\ndv_edge_rf_queue{port="rf-b"} 9\n'
            'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="dropped_queue"} 2\n'
            'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="dropped_new_reason"} 1\n')
    smp = samples(text)
    assert dc.queue_len(smp, "rf-a") == 3 and dc.queue_len(smp, "rf-b") == 9 and dc.queue_len(smp, "nope") is None
    assert dc.dropped_total(dc.rf_tx_counts(smp, "rf-a")) == 3          # an undocumented dropped_* still counts


def shape_with(ports_beacon=("rf-a",), ports_object=("rf-a",)):
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-objects.toml"])
    shp = dc.origin_shape(doc)
    for r in shp.reports:
        r.rf_ports = list(ports_beacon if r.kind == "beacon" else ports_object)
    return shp


def test_copies_expected_counts_one_copy_per_generated_report_and_port():
    shp = shape_with()
    assert dc.copies_expected({("beacon", "generated"): 2, ("object", "generated"): 20}, shp, "rf-a") == (22, "")
    assert dc.copies_expected({}, shp, "rf-a") == (0, "")
    assert dc.copies_expected({("beacon", "generated"): 1}, shape_with(ports_beacon=("rf-a", "rf-b")), "rf-a")[0] is None
    assert dc.copies_expected({("object", "generated"): 5}, shape_with(ports_object=()), "rf-a") == (0, "")
    assert "rf-a" in dc.copies_expected({("object", "generated"): 5}, shape_with(ports_object=("rf-z",)), "rf-a")[1]


def test_conservation_is_exact_with_the_queue_and_names_the_unaccounted_frames():
    delta = {("beacon", "sent"): 4, ("beacon", "dropped_queue"): 3, ("beacon", "dropped_ttl"): 1, ("beacon", "delayed"): 5}
    assert dc.conservation_problems(10, delta, 2) == []                          # 4 + 4 + 2 queued; delayed is not terminal
    bad = dc.conservation_problems(11, delta, 2)
    assert len(bad) == 1 and "1 unaccounted" in bad[0] and "dropped 4" in bad[0]
    assert "-1 unaccounted" in dc.conservation_problems(9, delta, 2)[0]
    assert dc.conservation_problems(8, {("object", "simulated"): 3, ("ident", "sent_over_budget"): 1, ("beacon", "sent"): 4}, 0) == []
    # the gauge unreadable: only an impossible excess is a problem
    assert dc.conservation_problems(10, delta, None) == []
    assert "exceed" in dc.conservation_problems(7, delta, None)[0]


def test_no_rf_edge_is_its_own_conservation_term():
    delta = {("beacon", "sent"): 4, ("beacon", "dropped_ttl"): 1}
    assert dc.conservation_problems(8, delta, 1, no_rf_edge=2) == []                 # 4 + 1 + 2 withheld + 1 queued
    bad = dc.conservation_problems(8, delta, 1, no_rf_edge=0)
    assert len(bad) == 1 and "no_rf_edge 0" in bad[0] and "2 unaccounted" in bad[0]
    assert dc.conservation_problems(7, delta, None, no_rf_edge=2) == []
    assert "exceed" in dc.conservation_problems(6, delta, None, no_rf_edge=2)[0]
    assert dc.no_rf_edge_total({("beacon", "no_rf_edge"): 2, ("object", "no_rf_edge"): 3, ("object", "generated"): 9}) == 5
    smp = samples('dv_pkt_rf_downlink_total{kind="aprs-info",result="dedupe"} 2\ndv_pkt_rf_downlink_total{kind="aprs-info",result="rate",proc="w"} 1\n'
                  'dv_pkt_rf_drop_total{reason="x"} 4\ndv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} 9\n')
    assert dc.worker_rf_counts(smp) == {"downlink/dedupe": 2, "downlink/rate": 1, "drop/x": 4}


def test_conservation_terms_name_every_term_and_the_difference():
    delta = {("beacon", "sent"): 4, ("beacon", "dropped_queue"): 3, ("beacon", "dropped_ttl"): 1, ("beacon", "delayed"): 5}
    line = dc.conservation_terms({("beacon", "generated"): 1, ("object", "generated"): 9, ("object", "no_rf_edge"): 2}, 10, delta, 1, 3, {"downlink/dedupe": 1})
    assert "no_rf_edge 2" in line and "'object/no_rf_edge': 2" in line and "'downlink/dedupe': 1" in line and "unaccounted -2" in line
    line = dc.conservation_terms({("beacon", "generated"): 1, ("object", "generated"): 9}, 10, delta, 1, 3)
    for part in ("'beacon/generated': 1", "'object/generated': 9", "copies expected 10", "sent 4", "simulated 0", "sent_over_budget 0",
                 "delayed(not terminal) 5", "'dropped_queue': 3", "'dropped_ttl': 1", "(sum 4)", "terminal 8", "queue gauge start 1 now 3 delta 2", "unaccounted 0"):
        assert part in line, (part, line)
    assert "unaccounted 1" in dc.conservation_terms({}, 11, delta, 1, 3)
    assert "unreadable" in dc.conservation_terms({}, 10, delta, None, 3)
    # rf_fenced is recorded next to the terms, never part of the equation: 0 on an image without it, same unaccounted either way
    assert "rf_fenced edge drops (recorded, not in the equation: it overlaps sent / dropped_ttl / the queue gauge) 0" in line
    with_fenced = dc.conservation_terms({("beacon", "generated"): 1, ("object", "generated"): 9}, 10, delta, 1, 3, None, 6)
    assert "gauge) 6" in with_fenced and "unaccounted 0" in with_fenced


def test_rf_events_keys_stamps_and_ignores_other_frames():
    frames = [(30.0, 0, obj_frame("DVTOB01", "010203")), (10.0, 0, frame_of(aprs.position_report(105000000, 202500000))),
              (40.0, 0, frame_of(":XLX999B  :hello")), (50.0, 0, b"\x00junk")]
    ev = dc.rf_events(frames, ax25)
    assert [(e.key, e.stamp_s) for e in ev] == [("beacon", None), ("object:DVTOB01", 3723)]


def test_floor_report_uses_the_object_stamp_not_the_queue_delayed_arrival():
    ev = dc.rf_events([(100.0, 0, obj_frame("DVTOB01", "100000")), (500.0, 0, obj_frame("DVTOB01", "101000"))], ax25)   # stamps 600 s apart, released 400 s apart
    problems, rows = dc.floor_report(ev, 600)
    assert problems == [] and rows[0]["stamp_gaps"] == [600] and rows[0]["arrival_gaps"] == [400.0]
    problems, _rows = dc.floor_report(ev, 600, arrival=True)
    assert len(problems) == 1 and "400.0 s apart at the TNC" in problems[0]
    # the same report sent twice: stamp gap 0
    twice = dc.rf_events([(100.0, 0, obj_frame("DVTOB01", "100000")), (130.0, 0, obj_frame("DVTOB01", "100000"))], ax25)
    assert "generated 0 s apart" in dc.floor_report(twice, 600)[0][0]
    # a stamp across midnight; a key seen once is not a row; a beacon has no stamp and is judged by arrival only
    over = dc.rf_events([(1.0, 0, obj_frame("DVTOB02", "235500")), (700.0, 0, obj_frame("DVTOB02", "000510")), (5.0, 0, obj_frame("DVTOB03", "000000"))], ax25)
    problems, rows = dc.floor_report(over, 600)
    assert problems == [] and [r["key"] for r in rows] == ["object:DVTOB02"] and rows[0]["stamp_gaps"] == [610]
    beacons = dc.rf_events([(10.0, 0, frame_of(aprs.position_report(105000000, 202500000))), (400.0, 0, frame_of(aprs.position_report(105000000, 202500000)))], ax25)
    assert dc.floor_report(beacons, 600)[0] == [] and len(dc.floor_report(beacons, 600, arrival=True)[0]) == 1


def aprsc_lines(shp, names, hms_offset=0.0, q="qAC,SRV"):
    out = []
    wall = 1_700_000_000.0
    for name in names:
        report = next(r for r in shp.reports if (r.kind == "beacon" and name == "beacon") or (r.kind == "object" and "object:" + r.name == name))
        hms = time.strftime("%H%M%S", time.gmtime(wall - hms_offset))
        info = dc.expected_info(report, hms if report.kind == "object" else None)
        parsed = line(info, q=q)
        out.append((wall, dc.classify(parsed, shp), parsed))
    return out


def test_aprsc_problems_need_several_objects_and_every_report():
    shp = dc.origin_shape(rendered(["dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-objects.toml"]))
    everything = ["beacon"] + ["object:" + r.name for r in shp.objects]
    problems, counts = dc.aprsc_problems(aprsc_lines(shp, everything + everything), shp)
    assert problems == [] and counts["beacon"] == 2 and len(counts) == 11
    problems, _c = dc.aprsc_problems(aprsc_lines(shp, ["beacon", "object:DVTOB01"]), shp)
    assert any("1 distinct object" in p for p in problems) and any("no line at the aprsc for" in p for p in problems)
    problems, _c = dc.aprsc_problems([], shp)
    assert any("0 distinct object" in p for p in problems)
    problems, _c = dc.aprsc_problems(aprsc_lines(shp, everything, q="qAS,SRV"), shp)
    assert any("qAC" in p for p in problems)
    problems, _c = dc.aprsc_problems(aprsc_lines(shp, everything, hms_offset=10.0), shp)
    assert any("off the read time" in p for p in problems)


class Hub:
    def __init__(self, text):
        self._t = text

    def text(self):
        return self._t


def fake_run(shp, origin, delta, queue=0, base_queue=0, lines=None, events=()):
    return SimpleNamespace(shape=shp, port="rf-a", interval=600, wait=1.0, delta=delta, origin=origin, base={}, base_origin={}, base_worker={}, base_fenced=0, base_queue=base_queue,
                           queue_now=queue, aprsc_lines=lines, aprsc_why="no aprsc", events=list(events))


def test_strict_cases_skip_without_the_switch_and_assert_with_it(monkeypatch):
    shp = shape_with()
    monkeypatch.setattr(trf, "note", lambda *a: None)
    ok = fake_run(shp, {("beacon", "generated"): 2, ("object", "generated"): 20}, {("beacon", "sent"): 4, ("beacon", "dropped_queue"): 18})
    monkeypatch.delenv("DVT_DP18STRICT", raising=False)
    for fn in (trf.test_dp18_o_strict_every_report_finds_both_edges, trf.test_dp18_o_strict_nothing_is_sent_over_budget):
        with pytest.raises(pytest.skip.Exception, match="DVT_DP18STRICT=on"):
            fn(None, NOTE, ok)
    monkeypatch.setenv("DVT_DP18STRICT", "on")
    trf.test_dp18_o_strict_every_report_finds_both_edges(None, NOTE, ok)
    trf.test_dp18_o_strict_nothing_is_sent_over_budget(None, NOTE, ok)
    broken = fake_run(shp, {("beacon", "generated"): 2, ("beacon", "no_edge"): 2}, {("ident", "sent_over_budget"): 1})
    with pytest.raises(AssertionError, match="beacon/no_edge.*dv-edge-aprsis"):
        trf.test_dp18_o_strict_every_report_finds_both_edges(None, NOTE, broken)
    with pytest.raises(AssertionError, match="sent over the budget"):
        trf.test_dp18_o_strict_nothing_is_sent_over_budget(None, NOTE, broken)


def test_the_conservation_case_polls_until_the_counters_settle(monkeypatch):
    shp = shape_with()
    monkeypatch.setattr(trf, "note", lambda *a: None)
    monkeypatch.setenv("DVT_DP18STRICT", "on")
    clock = {"t": 0.0}
    monkeypatch.setattr(trf.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(trf.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def text(generated, sent, queue):
        return ('dv_pkt_origin_total{kind="beacon",result="generated"} %d\ndv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} %d\n'
                'dv_edge_rf_queue{port="rf-a"} %d\n' % (generated, sent, queue))

    run = fake_run(shp, {}, {})
    trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(5, 3, 2)), run)
    with pytest.raises(AssertionError, match="1 unaccounted"):
        trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(5, 3, 1)), run)
    assert clock["t"] >= 45.0                                                  # it waited the whole settle time before failing
    run.base_queue = None                                                      # no gauge at the start: only terminal <= copies is checked
    trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(5, 3, 0)), run)
    with pytest.raises(pytest.skip.Exception, match="not computable"):
        trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(5, 3, 2)), fake_run(shape_with(ports_beacon=("rf-a", "rf-b")), {}, {}))


def test_the_conservation_case_uses_the_txcnt1b_equation_only_under_its_switch(monkeypatch):
    shp = shape_with()
    notes = []
    monkeypatch.setattr(trf, "note", lambda *a: notes.append(a[-1]))
    monkeypatch.setenv("DVT_DP18STRICT", "on")
    clock = {"t": 0.0}
    monkeypatch.setattr(trf.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(trf.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def text(fenced):
        return ('dv_pkt_origin_total{kind="beacon",result="generated"} 5\ndv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} 3\n'
                'dv_edge_rf_queue{port="rf-a"} 0\ndv_edge_drops_total{reason="rf_fenced"} %d\n' % fenced)

    run = fake_run(shp, {}, {})
    # 5 copies, 3 sent, nothing queued: 2 copies are unaccounted
    monkeypatch.delenv("DVT_TXCNT1B", raising=False)
    with pytest.raises(AssertionError, match="2 unaccounted"):
        trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(2)), run)           # rf_fenced does not count without the switch
    assert "recorded, not in the equation" in notes[-1] and "TXCNT1b" not in notes[-1]
    monkeypatch.setenv("DVT_TXCNT1B", "on")
    clock["t"] = 0.0
    trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(2)), run)             # exactly the two canceled copies
    assert "TXCNT1b equation" in notes[-1] and "unaccounted after the term 0" in notes[-1]
    trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(3)), run)             # one post-release refusal in the aggregate: slack
    assert "unaccounted after the term -1" in notes[-1]
    clock["t"] = 0.0
    with pytest.raises(AssertionError, match="0 <= unaccounted <= 1"):
        trf.test_dp18_o_strict_every_generated_copy_is_accounted_for(None, NOTE, Hub(text(1)), run)
    assert clock["t"] >= 45.0


def test_the_txcnt1b_label_case_skips_without_its_switch_and_names_a_removed_label(monkeypatch):
    shp = shape_with()
    notes = []
    monkeypatch.setattr(trf, "note", lambda *a: notes.append(a[-1]))
    run = fake_run(shp, {}, {})
    good = "\n".join('dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="%s"} 1' % r for r in dc.TXCNT1B_RESULTS) + "\n"
    monkeypatch.delenv("DVT_TXCNT1B", raising=False)
    with pytest.raises(pytest.skip.Exception, match="DVT_TXCNT1B=on"):
        trf.test_dp18_o_txcnt1b_result_labels_are_the_documented_set(None, NOTE, Hub(good), run)
    monkeypatch.setenv("DVT_TXCNT1B", "on")
    trf.test_dp18_o_txcnt1b_result_labels_are_the_documented_set(None, NOTE, Hub(good), run)
    assert "dropped_too_long" in notes[-1]
    trf.test_dp18_o_txcnt1b_result_labels_are_the_documented_set(None, NOTE, Hub(good + 'dv_edge_rf_tx_total{port="rf-b",kind="text",result="delayed"} 0\n'), run)
    with pytest.raises(AssertionError, match="dropped_budget, dropped_gap"):
        trf.test_dp18_o_txcnt1b_result_labels_are_the_documented_set(
            None, NOTE, Hub(good + 'dv_edge_rf_tx_total{port="rf-b",kind="text",result="dropped_budget"} 0\ndv_edge_rf_tx_total{port="rf-b",kind="text",result="dropped_gap"} 0\n'), run)
    with pytest.raises(pytest.skip.Exception, match="no dv_edge_rf_tx_total series"):
        trf.test_dp18_o_txcnt1b_result_labels_are_the_documented_set(None, NOTE, Hub("dv_edge_rf_queue{port=\"rf-a\"} 0\n"), run)


def test_the_txcnt1_case_records_every_input_and_applies_both_bounds(monkeypatch):
    shp = shape_with()
    notes = []
    monkeypatch.setattr(trf, "note", lambda *a: notes.append(a[-1]))
    monkeypatch.delenv("DVT_TXCNT1", raising=False)
    run = fake_run(shp, {}, {}, base_queue=0)
    run.times = [1.0] * 8
    origin = 'dv_pkt_origin_total{kind="beacon",result="generated"} 2\ndv_pkt_origin_total{kind="object",result="generated"} 20\n'

    def text(delayed, ttl=3, fenced=0):
        return (origin + 'dv_edge_rf_tx_total{port="rf-a",kind="object",result="delayed"} %d\ndv_edge_rf_tx_total{port="rf-a",kind="object",result="dropped_ttl"} %d\n'
                'dv_edge_rf_tx_total{port="rf-a",kind="object",result="sent"} 8\ndv_edge_rf_queue{port="rf-a"} 7\n'
                'dv_edge_drops_total{reason="rf_fenced"} %d\n' % (delayed, ttl, fenced))

    with pytest.raises(pytest.skip.Exception, match="DVT_TXCNT1=on"):
        trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(10)), run)
    monkeypatch.setenv("DVT_TXCNT1", "on")
    trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(10, fenced=2)), run)
    assert len(notes) == 1 and "delayed delta 10; copies expected 22; dropped_ttl 3;" in notes[0] and "sent 8" in notes[0] and "rf_fenced edge drops 2" in notes[0]
    assert "queue gauge base 0 now 7 delta 7" in notes[0] and "frames at the TNC 8" in notes[0]
    trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(3)), run)           # delayed == dropped_ttl
    trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(22)), run)          # delayed == copies
    with pytest.raises(AssertionError, match="8932"):
        trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(8932)), run)
    with pytest.raises(AssertionError, match="at least 3"):
        trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(2)), run)
    run.base_queue = None                                                                           # no baseline: the lower bound is not asserted
    trf.test_dp18_o_txcnt1_delayed_counts_frames_not_ticks(None, NOTE, Hub(text(2)), run)
    assert "none (queue base unreadable)" in notes[-1]


def test_txcnt1b_switch_reads_only_the_value_on():
    assert dc.txcnt1b_on({"DVT_TXCNT1B": "on"}) is True
    for value in ("", "1", "true", "ON", "off"):
        assert dc.txcnt1b_on({"DVT_TXCNT1B": value}) is False
    assert dc.txcnt1b_on({"DVT_TXCNT1": "on"}) is False and dc.txcnt1b_on({}) is False      # independent of the TXCNT1 switch


def test_txcnt1b_label_set_is_the_documented_eight():
    assert set(dc.TXCNT1B_RESULTS) == {"sent", "simulated", "sent_over_budget", "delayed", "dropped_ttl", "dropped_queue", "dropped_exceeds_quota", "dropped_too_long"}
    assert set(dc.TXCNT1B_REMOVED) == {"dropped_budget", "dropped_quota", "dropped_gap"} and not set(dc.TXCNT1B_REMOVED) & set(dc.TXCNT1B_RESULTS)
    smp = samples('dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} 4\ndv_edge_rf_tx_total{port="rf-b",kind="text",result="dropped_ttl"} 0\n'
                  'dv_edge_rf_tx_total{port="rf-a",kind="beacon",result="sent"} 1\ndv_edge_rf_queue{port="rf-a"} 3\ndv_edge_drops_total{reason="rf_fenced"} 2\n')
    assert dc.rf_tx_result_labels(smp) == ["dropped_ttl", "sent"] and dc.rf_tx_result_labels([]) == []
    assert dc.result_label_problems(dc.rf_tx_result_labels(smp)) == [] and dc.result_label_problems(dc.TXCNT1B_RESULTS) == [] and dc.result_label_problems([]) == []
    old = dc.result_label_problems(["sent", "dropped_budget", "dropped_gap", "delayed"])
    assert len(old) == 1 and "dropped_budget, dropped_gap" in old[0] and "dropped_quota" not in old[0]
    mixed = dc.result_label_problems(["dropped_quota", "rf_fenced", "sent"])                # rf_fenced is a drops reason, not a result of this counter
    assert len(mixed) == 2 and "dropped_quota" in mixed[0] and "'rf_fenced'" in mixed[1]


def test_txcnt1b_conservation_adds_the_cancellation_term_as_an_upper_bound():
    delta = {("beacon", "sent"): 4, ("beacon", "dropped_queue"): 3, ("beacon", "dropped_ttl"): 1, ("beacon", "delayed"): 5}     # terminal 8
    # 12 copies, 8 terminal, 1 still queued: 3 unaccounted
    assert len(dc.conservation_problems(12, delta, 1)) == 1                                  # the older equation: unchanged, a mismatch
    assert "3 unaccounted" in dc.conservation_problems(12, delta, 1)[0]
    assert dc.conservation_problems(12, delta, 1, 0, None) == dc.conservation_problems(12, delta, 1)
    assert dc.conservation_problems(12, delta, 1, 0, 3) == []                                # exact: three copies canceled by a fence
    assert dc.conservation_problems(12, delta, 1, 0, 5) == []                                # post-release refusals also count in the aggregate
    bad = dc.conservation_problems(12, delta, 1, 0, 2)
    assert len(bad) == 1 and "3 unaccounted" in bad[0] and "rf_fenced" in bad[0] and "0 <= unaccounted <= 2" in bad[0]
    assert dc.conservation_problems(9, delta, 1, 0, 0) == []
    over = dc.conservation_problems(8, delta, 1, 0, 3)                                       # 8 copies but 8 terminal + 1 queued: over-accounted, negative
    assert len(over) == 1 and "-1 unaccounted" in over[0]
    assert dc.conservation_problems(8, delta, 0, 0, 0) == []                                 # balanced, nothing canceled
    assert dc.conservation_problems(10, delta, 0, 2, 0) == []                                # no_rf_edge is its own term
    assert dc.conservation_problems(7, delta, None, 0, 9) != [] and dc.conservation_problems(12, delta, None, 0, 9) == []        # no gauge: terminal <= copies only


def test_txcnt1b_terms_line_names_the_cancellation_term_and_the_slack():
    delta = {("beacon", "sent"): 4, ("beacon", "dropped_queue"): 3, ("beacon", "dropped_ttl"): 1}
    origin = {("beacon", "generated"): 12}
    plain = dc.conservation_terms(origin, 12, delta, 1, 2, None, 5)
    assert "recorded, not in the equation" in plain and "unaccounted 3;" in plain and "TXCNT1b" not in plain
    line = dc.conservation_terms(origin, 12, delta, 1, 2, None, 1, True)
    assert "TXCNT1b cancellation term" in line and "rf_fenced edge drops (TXCNT1b cancellation term, upper bound: the aggregate also counts post-release and non-copy refusals) 1" in line
    assert line.endswith("unaccounted after the term 2") and "unaccounted 3;" in line
    assert dc.conservation_terms(origin, 12, delta, None, 2, None, 1, True).endswith("unaccounted after the term unreadable")


def test_floor_and_aprsc_cases(monkeypatch):
    shp = shape_with()
    monkeypatch.setattr(trf, "note", lambda *a: None)
    monkeypatch.delenv("DVT_DP18STRICT", raising=False)
    twice = dc.rf_events([(100.0, 0, obj_frame("DVTOB01", "100000")), (130.0, 0, obj_frame("DVTOB01", "100000"))], ax25)
    with pytest.raises(AssertionError, match="generated 0 s apart"):
        trf.test_dp18_o_f_the_same_report_is_not_sent_twice_within_the_interval(None, NOTE, fake_run(shp, {}, {}, events=twice))
    once = dc.rf_events([(100.0, 0, obj_frame("DVTOB01", "100000"))], ax25)
    with pytest.raises(pytest.skip.Exception, match="dp18-rf-floor.toml"):
        trf.test_dp18_o_f_the_same_report_is_not_sent_twice_within_the_interval(None, NOTE, fake_run(shp, {}, {}, events=once))
    spaced = dc.rf_events([(100.0, 0, obj_frame("DVTOB01", "100000")), (700.0, 0, obj_frame("DVTOB01", "101000"))], ax25)
    trf.test_dp18_o_f_the_same_report_is_not_sent_twice_within_the_interval(None, NOTE, fake_run(shp, {}, {}, events=spaced))
    with pytest.raises(pytest.skip.Exception, match="DVT_DP18STRICT=on"):
        trf.test_dp18_o_strict_the_aprsc_sees_several_objects(None, NOTE, fake_run(shp, {}, {}))
    monkeypatch.setenv("DVT_DP18STRICT", "on")
    with pytest.raises(pytest.skip.Exception, match="no private aprsc"):
        trf.test_dp18_o_strict_the_aprsc_sees_several_objects(None, NOTE, fake_run(shp, {}, {}))
    everything = ["beacon"] + ["object:" + r.name for r in shp.objects]
    trf.test_dp18_o_strict_the_aprsc_sees_several_objects(None, NOTE, fake_run(shp, {}, {}, lines=aprsc_lines(shp, everything)))
    with pytest.raises(AssertionError, match="distinct object"):
        trf.test_dp18_o_strict_the_aprsc_sees_several_objects(None, NOTE, fake_run(shp, {}, {}, lines=[]))


def test_the_objects_run_fixture_skips_without_its_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(trf.p1, "read_config", lambda target: {})
    with pytest.raises(pytest.skip.Exception, match="dp18-rf-floor.toml"):
        trf.objects_run.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, None)
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-objects.toml"])
    monkeypatch.setattr(trf.p1, "read_config", lambda target: doc)
    old = SimpleNamespace(path=lambda s: s, get=lambda p, expect=None: SimpleNamespace(status=200, text=lambda: '{"properties": {"enable": {}}}'))
    with pytest.raises(pytest.skip.Exception, match="does not know DP12c"):
        trf.objects_run.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, lambda role: old)


def test_the_floor_snippet_has_three_reports_with_rf_copies_and_the_compose_passes_the_switch():
    shp = dc.origin_shape(rendered(["dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-floor.toml"]))
    assert [r.kind for r in shp.reports] == ["beacon", "object", "object"] and all(r.rf_ports == ["rf-a"] and r.interval_s == 600 for r in shp.reports)
    assert [r.first_due_s for r in shp.reports] == [600.0, 635.0, 670.0] and len({r.name for r in shp.objects}) == 2
    compose = (ROOT / "rig" / "compose.yml").read_text()
    assert "DVT_DP18STRICT: ${DVT_DP18STRICT:-}" in compose and "DVT_TXCNT1B: ${DVT_TXCNT1B:-}" in compose


# ---------------------------------------------------------------- the admin config API helpers

def test_tx_change_request_and_the_other_admin_helpers():
    body = dc.tx_change_request(1, "simulate", "abc", "dvt")
    assert body == {"base_etag": "abc", "mode": "apply", "comment": "dvt",
                    "ops": [{"op": "replace", "path": "/edges/ax25/port/1/tx", "value": "simulate"}]}
    with pytest.raises(ValueError):
        dc.tx_change_request(0, "on", "e", "c")
    assert dc.etag_of(SimpleNamespace(json=lambda: {"meta": {"etag": "h1"}}, headers={})) == "h1"
    assert dc.etag_of(SimpleNamespace(json=lambda: {}, headers={"ETag": "h2"})) == "h2"
    assert dc.etag_of(SimpleNamespace(json=lambda: (_ for _ in ()).throw(ValueError()), headers={})) is None
    assert dc.history_seqs([{"seq": 3}, {"seq": 1}, 2, {"x": 1}]) == [1, 2, 3] and dc.history_seqs({"items": [{"seq": 7}]}) == [7]
    assert dc.history_seqs({"history": [{"seq": True}]}) == [] and dc.history_seqs("x") == []
    status = {"launcher": {"children": [{"name": "dv-edge-ax25", "pid": 10, "restarts": 0, "state": "up", "since_ms": 5}]}}
    before = dc.child_state(status, "dv-edge-ax25")
    named = {"launcher": {"children": [{"name": "dv-edge-ax25[b]", "pid": 7, "restarts": 2, "state": "up", "since_ms": 1794000000000}, {"name": "dv-room[B]", "pid": 8}]}}
    assert dc.child_state(named, "dv-edge-ax25") == {"pid": 7, "restarts": 2, "state": "up", "since_ms": 1794000000000}        # a single named instance stands in
    two = {"launcher": {"children": [{"name": "dv-edge-ax25[a]", "pid": 1}, {"name": "dv-edge-ax25[b]", "pid": 2}]}}
    assert dc.child_state(two, "dv-edge-ax25") is None                                                                       # ambiguous: no guess
    assert before["pid"] == 10 and dc.child_state(status, "nope") is None and dc.child_state({}, "x") is None
    assert dc.restarted(before, dict(before, restarts=1)) and dc.restarted(before, dict(before, pid=11)) and not dc.restarted(before, dict(before))
    assert not dc.restarted(None, before)
    assert dc.schema_has_dp18('{"simulate": 1}') is False                                                    # needs all three markers
    assert dc.schema_has_dp18('{"const": "simulate", "properties": {"beacon": {}, "objects": {}}}')
    assert not dc.schema_has_dp18('{"properties": {"enable": {}}}')


# ---------------------------------------------------------------- the origin case flows against fakes

def origin_ctx(withheld=False):
    shp = dc.origin_shape(rendered(ORIGIN + (["dp18-origin-withheld.toml"] if withheld else [])))
    return SimpleNamespace(shape=shp, host="h", admin=None, base={}, interval_s=600)


def first_lines_for(shp, hms_offset=0.0, object_delay=35.0, q="qAC,SRV", dest_ok=True):
    import calendar
    wall = calendar.timegm((2026, 11, 10, 12, 0, 0)) + 0.2
    out = {}
    for report in shp.reports:
        at = 1000.0 + (object_delay if report.kind == "object" else 0.0)
        w = wall + (object_delay if report.kind == "object" else 0.0)
        hms = time.strftime("%H%M%S", time.gmtime(int(w - hms_offset)))
        info = dc.expected_info(report, hms)
        out[to.report_key(report)] = (w, line(info, q=q), at)
    out["_uptime_now"] = 700.0 + (time.monotonic() - 1000.0) if False else None
    return out


NOTE = SimpleNamespace(out="/nonexistent")


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(to.ds, "write_note", lambda *a, **k: None)


def test_flow_a_b_c_pass_on_a_good_set_and_fail_on_each_defect(quiet):
    ctx = origin_ctx()
    good = first_lines_for(ctx.shape)
    to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, good, NOTE)
    to.test_dp18_b_the_object_comes_35_s_after_the_beacon(ctx, good, NOTE)
    to.test_dp18_c_the_object_timestamp_is_the_send_time(ctx, good, NOTE)
    with pytest.raises(AssertionError, match="qAC"):
        to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, first_lines_for(ctx.shape, q="qAS,SRV"), NOTE)
    with pytest.raises(AssertionError, match="after the beacon"):
        to.test_dp18_b_the_object_comes_35_s_after_the_beacon(ctx, first_lines_for(ctx.shape, object_delay=60.0), NOTE)
    with pytest.raises(AssertionError, match="stamp"):
        to.test_dp18_c_the_object_timestamp_is_the_send_time(ctx, first_lines_for(ctx.shape, hms_offset=5.0), NOTE)


def test_flow_a_floor_uses_the_node_uptime(quiet):
    ctx = origin_ctx()
    good = first_lines_for(ctx.shape)
    first = min(v[2] for k, v in good.items() if k != "_uptime_now")
    good["_uptime_now"] = 650.0 + (time.monotonic() - first)                        # the beacon came at node uptime ~650 s: after the 600 s floor
    to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, good, NOTE)
    good["_uptime_now"] = 300.0 + (time.monotonic() - first)                        # 300 s: before one full interval
    with pytest.raises(AssertionError, match="before one full interval"):
        to.test_dp18_a_beacon_and_object_arrive_as_the_gateways_own_reports(ctx, good, NOTE)


class FakeHub:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


def test_flow_n_counts_generated_reports(quiet, monkeypatch):
    monkeypatch.setattr(to, "COUNTER_WAIT_S", 0.0)
    ctx = origin_ctx()
    hub = FakeHub('dv_pkt_origin_total{kind="beacon",result="generated"} 1\ndv_pkt_origin_total{kind="object",result="generated"} 1')
    to.test_dp18_n_origin_counters_count_generated_reports(ctx, {}, hub, NOTE)
    with pytest.raises(AssertionError, match="grew by 0"):
        to.test_dp18_n_origin_counters_count_generated_reports(ctx, {}, FakeHub('dv_pkt_origin_total{kind="beacon",result="generated"} 1'), NOTE)
    with pytest.raises(AssertionError, match="not generated for a reason"):
        to.test_dp18_n_origin_counters_count_generated_reports(ctx, {}, FakeHub(hub.text() + '\ndv_pkt_origin_total{kind="object",result="withheld"} 1'), NOTE)


class FakeSession:
    def __init__(self, lines=()):
        self.lines = list(lines)

    def mark(self):
        return len(self.lines)

    def snapshot(self):
        return [(time.monotonic(), l) for l in self.lines]


def test_flow_e_withheld_sends_nothing_and_counts_withheld(quiet, monkeypatch):
    monkeypatch.setattr(to.time, "sleep", lambda s: None)
    ctx = origin_ctx(withheld=True)
    hub = FakeHub('dv_pkt_origin_total{kind="beacon",result="withheld"} 2\ndv_pkt_origin_total{kind="object",result="withheld"} 2')
    to.test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, FakeSession(), hub, NOTE)
    beacon = dc.expected_info(ctx.shape.beacon)
    sent = FakeSession(["N0TEST-5>APZDVR,TCPIP*,qAC,SRV:" + beacon])
    sent.snapshot = lambda: [(time.monotonic(), l) for l in sent.lines]
    sent.mark = lambda: 0
    with pytest.raises(AssertionError, match="sent a line"):
        to.test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, sent, hub, NOTE)
    with pytest.raises(pytest.skip.Exception, match="does not withhold"):
        to.test_dp18_e_a_withheld_gateway_call_sends_nothing(origin_ctx(), FakeSession(), hub, NOTE)
    with pytest.raises(AssertionError, match="generated although"):
        to.test_dp18_e_a_withheld_gateway_call_sends_nothing(ctx, FakeSession(), FakeHub(hub.text() + '\ndv_pkt_origin_total{kind="beacon",result="generated"} 1'), NOTE)


# ---------------------------------------------------------------- the reload helpers against a fake admin

class FakeAdmin:
    def __init__(self, writable=True, post_status=200, etag="e1"):
        self.writable, self.post_status, self.etag, self.posts, self.step_ups = writable, post_status, etag, [], 0

    def path(self, suffix):
        return "/api/v1/admin" + suffix

    def step_up(self):
        self.step_ups += 1

    def get(self, path, expect=None):
        return SimpleNamespace(status=200, headers={}, text=lambda: "", json=lambda: {"meta": {"etag": self.etag, "writable": self.writable}})

    def post(self, path, body, expect=None):
        self.posts.append((path, body))
        return SimpleNamespace(status=self.post_status, text=lambda: "boom", json=lambda: {})


def test_apply_tx_posts_the_replace_with_the_current_etag_and_skips_when_read_only():
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml"])
    admin = FakeAdmin()
    sim = SimpleNamespace(admin_adm=admin, doc=doc)
    trf.apply_tx(sim, "rf-b", True, "dvt")
    path, body = admin.posts[0]
    assert path == "/api/v1/admin/config/changes" and body["base_etag"] == "e1" and body["ops"][0]["path"] == "/edges/ax25/port/1/tx" and admin.step_ups == 1
    with pytest.raises(pytest.skip.Exception, match=r"not writable.*DVT_SUT_CONFIG_RW=1"):
        trf.apply_tx(SimpleNamespace(admin_adm=FakeAdmin(writable=False), doc=doc), "rf-b", True, "dvt")
    with pytest.raises(pytest.skip.Exception, match=r"read_only.*DVT_SUT_CONFIG_RW=1"):
        trf.apply_tx(SimpleNamespace(admin_adm=FakeAdmin(post_status=409), doc=doc), "rf-b", True, "dvt")
    with pytest.raises(AssertionError, match="HTTP 422"):
        trf.apply_tx(SimpleNamespace(admin_adm=FakeAdmin(post_status=422), doc=doc), "rf-b", True, "dvt")


def test_frames_with_matches_the_posted_body_at_a_fake_tnc():
    tnc = SimpleNamespace(data_frames=lambda since=0.0: [(5.0, 0, kiss_tnc.ui_frame("N0TEST-5", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", "hello DVT123")))])
    assert [m["text"] for _t, m in trf.frames_with(tnc, "DVT123")] == ["hello DVT123"] and trf.frames_with(tnc, "other") == []


def test_every_dp18_case_skips_on_a_run_without_its_shape(tmp_path, monkeypatch):
    """No shape, no capability: each fixture skips with a reason (the cases never fail on a run that did not ask for them)."""
    monkeypatch.setattr(to.p1, "read_config", lambda target: {})
    with pytest.raises(pytest.skip.Exception, match="no enabled|has no enabled"):
        to.ctx.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, None)
    monkeypatch.setattr(trf.p1, "read_config", lambda target: {})
    with pytest.raises(pytest.skip.Exception, match="rf-a tx = true"):
        trf.sim.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, None)
    doc = rendered(["dp15-aprsis.toml", "dp18-rf.toml"])
    monkeypatch.setattr(trf.p1, "read_config", lambda target: doc)
    old = SimpleNamespace(path=lambda s: s, get=lambda p, expect=None: SimpleNamespace(status=200, text=lambda: '{"properties": {"enable": {}}}'))
    with pytest.raises(pytest.skip.Exception, match="does not know DP12c"):
        trf.sim.__wrapped__(None, SimpleNamespace(out=str(tmp_path)), None, lambda role: old)


# ---------------------------------------------------------------- DP18-k with a fake clock

class Clock:
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def run_k(monkeypatch, line_offsets):
    """The kill case with `line_offsets` = seconds after the kill at which the node's own beacon line reaches the aprsc."""
    clock = Clock()
    monkeypatch.setattr(to, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    monkeypatch.setattr(to.ds, "write_note", lambda *a, **k: None)
    ctx = origin_ctx()
    t_kill = clock.t + 1.0
    beacon = "N0TEST-5>APZDVR,TCPIP*,qAC,SRV:" + dc.expected_info(ctx.shape.beacon)

    class Session:
        def mark(self):
            return 0

        def snapshot(self):
            return [(t_kill + off, beacon) for off in line_offsets if t_kill + off <= clock.t]

    control = SimpleNamespace(kill_proc=lambda *a, **k: SimpleNamespace(t_kill_monotonic=t_kill, pids=[7]))
    to.test_dp18_k_nothing_arrives_before_one_full_interval_after_a_worker_kill(ctx, {}, Session(), control, NOTE)
    return clock


def test_flow_k_passes_when_the_new_worker_waits_a_full_interval(monkeypatch):
    clock = run_k(monkeypatch, [610.0])
    assert clock.t >= 5000.0 + 585.0


def test_flow_k_fails_on_an_early_line_and_on_a_missing_report(monkeypatch):
    with pytest.raises(AssertionError, match="within 585 s of the worker kill"):
        run_k(monkeypatch, [120.0])
    with pytest.raises(AssertionError, match="sent no report"):
        run_k(monkeypatch, [])


# ---------------------------------------------------------------- DP18-s with fakes

class FakeTnc:
    def __init__(self):
        self.frames, self.received, self.connections = [], [], 1

    def data_frames(self, since=0.0):
        return [f for f in self.frames if f[0] >= since]

    def is_connected(self):
        return True


def run_s(monkeypatch, sim_frame_at_b=False, rf_b_counts_sent=False, ptt_moves=False, a_frames=1):
    clock_state = {"posts": 0}
    a, b = FakeTnc(), FakeTnc()
    hub_state = {"a_sent": 0, "b_sim": 0, "b_sent": 0, "ptt": 0}

    class Hub:
        def text(self):
            return "\n".join([
                'dv_edge_rf_tx_total{port="rf-a",kind="text",result="sent"} %d' % hub_state["a_sent"],
                'dv_edge_rf_tx_total{port="rf-b",kind="text",result="simulated"} %d' % hub_state["b_sim"],
                'dv_edge_rf_tx_total{port="rf-b",kind="text",result="sent"} %d' % hub_state["b_sent"],
                'dv_edge_modem_total{port="rf-b",result="tx"} 0', 'dv_edge_rf_ptt_total{port="rf-b"} %d' % hub_state["ptt"]])

    def fake_post(poster, text, pace=True):
        t = time.monotonic()
        clock_state["posts"] += 1
        for _ in range(a_frames):
            a.frames.append((t + 0.5, 0, kiss_tnc.ui_frame("N0TEST-5", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", text))))
        if sim_frame_at_b:
            b.frames.append((t + 0.6, 0, kiss_tnc.ui_frame("N0TEST-5", "APZDVR", kiss_tnc.aprs_message_info("XLX999B", text))))
        hub_state["a_sent"] += 1
        hub_state["b_sim"] += 1
        hub_state["b_sent"] += 1 if rf_b_counts_sent else 0
        hub_state["ptt"] += 1 if ptt_moves else 0
        return t, t, 202

    monkeypatch.setattr(trf.tr, "post", fake_post)
    monkeypatch.setattr(trf.time, "sleep", lambda s: None)
    monkeypatch.setattr(trf.ds, "write_note", lambda *a, **k: None)
    monkeypatch.setattr(trf.ds, "nonce_body", lambda tag: "%s-%d" % (tag, clock_state["posts"]))
    sim = SimpleNamespace(rf=SimpleNamespace(a=a, b=b, tx_guard=lambda case: None), web=SimpleNamespace(posters=[object()] * 3))
    trf.test_dp18_s_a_simulated_port_never_reaches_its_tnc(None, NOTE, Hub(), sim)


def test_flow_s_passes_when_only_the_real_port_transmits(monkeypatch):
    run_s(monkeypatch)


def test_flow_s_fails_on_each_way_a_simulated_port_can_leak(monkeypatch):
    with pytest.raises(AssertionError, match="simulate port's TNC"):
        run_s(monkeypatch, sim_frame_at_b=True)
    with pytest.raises(AssertionError, match="counted a real send"):
        run_s(monkeypatch, rf_b_counts_sent=True)
    with pytest.raises(AssertionError, match="PTT"):
        run_s(monkeypatch, ptt_moves=True)
    with pytest.raises(AssertionError, match="more than once|exactly 1"):
        run_s(monkeypatch, a_frames=2)


def test_the_dp18_modules_never_write_files_and_the_docs_name_the_rw_variable():
    """The reload cases change the config through the admin API only: no file write, no direct edit of the rendered dv-router.toml."""
    import re
    for name in ("cases/dp18_support.py", "cases/test_dp18_rf.py", "cases/test_dp18_origin.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"write_text|write_bytes|shutil\.|os\.(replace|rename|remove|unlink)|open\([^)]*[\"'][wa]b?[\"']", text), name
    docs = (ROOT / "docs" / "dp18-cases.md").read_text(encoding="utf-8")
    s_line = next(l for l in docs.split("\n") if l.startswith("... DVT_SUT_CONFIG_RW=1 DVT_SUT_CONFIG_EXTRA=") and "dp18-rf.toml scripts" in l)
    assert "-k \"dp18_s or dp18_r\"" in s_line


def test_dp18_r1_fence_view_bounds_follow_the_switch(monkeypatch):
    from dvtest import rfsafe1b as rb
    notes = []
    monkeypatch.setattr(trf, "note", lambda *a: notes.append(a))
    S = rb.Sample

    def trace(end):
        samples, t = [], 0.2
        while t < end - 0.01:
            samples.append(S(t, True, "startup_guard", max(0, int(round((13.0 - t) * 1000)))))
            t += 0.5
        return rb.Trace(samples + [S(end, False, None, None)], end, "unfenced")

    def run(tmp, end, since=300, state=None):
        monkeypatch.setattr(trf, "children_state", lambda sim: {"since_ms": None if since is None else int(trf.time.time() * 1000) - since})
        monkeypatch.setattr(trf.txguard, "ax25_reader", lambda admin, port: (lambda: None))
        seen = {}
        def fake_collect(read, origin, **kw):
            seen.update(kw)
            t = trace(end)
            return t if state is None else t._replace(state=state)
        monkeypatch.setattr(trf.rfsafe1b, "collect", fake_collect)
        out = trf.fence_view_bounds(SimpleNamespace(admin=None), SimpleNamespace(out=str(tmp)), "DP18-r1", "rf-b")
        return out, seen

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setenv("DVT_RFSAFE1B", "on")
    judged, seen = run(tmp, 14.0)
    assert judged.problems == [] and seen["max_wait_s"] == 20.0 and (tmp / "logscan" / "DP18-r1-probe-failed.json").is_file()
    with pytest.raises(AssertionError, match="before the guard"):
        run(tmp, 12.0)
    judged, _ = run(tmp, 12.0, since=None)                                      # no usable since_ms: the early bound cannot be checked
    assert judged.problems == []
    judged, _ = run(tmp, 12.0, since=-1000 * 3600)                              # a start in the FUTURE is not a start: fallback, no early check
    assert judged.problems == []
    with pytest.raises(AssertionError, match="before the guard"):
        run(tmp, 12.0, since=2500)                                              # the child started 2.5 s before the first read: the origin is that start, 12 s is early
    with pytest.raises(AssertionError, match="expected within 16.0 s"):
        run(tmp, 17.0)
    with pytest.raises(pytest.fail.Exception, match="no `modem` object"):
        run(tmp, 14.0, state="scope")
    monkeypatch.delenv("DVT_RFSAFE1B")
    judged, seen = run(tmp, 22.0)                                               # lenient: a view that lags up to 24.5 s is recorded, not asserted
    assert judged.problems == [] and seen["max_wait_s"] == 30.0
    assert any("lenient (DVT_RFSAFE1B off)" in a[3] for a in notes)


# ---------------------------------------------------------------- DP18-r3: a refused reload (DP18b)

def run_r3(monkeypatch, cid, status, body, etag_after="e1", aprsis_after=None, doc_files=("dp15-aprsis.toml", "dp18-rf.toml", "dp18-rf-floor.toml"), writable=True):
    monkeypatch.setattr(trf, "note", lambda *a: None)
    monkeypatch.setattr(trf.time, "sleep", lambda s: None)
    aprsis = [{"pid": 7, "restarts": 0}, aprsis_after or {"pid": 7, "restarts": 0}]
    etags = iter([("e1", writable), (etag_after, True)])
    monkeypatch.setattr(trf, "config_state", lambda sim: next(etags))
    monkeypatch.setattr(trf, "aprsis_child_state", lambda sim: aprsis.pop(0))
    ax = [{"pid": 3, "restarts": 0}, {"pid": 3, "restarts": 0}]
    monkeypatch.setattr(trf, "children_state", lambda sim: ax.pop(0))
    posted = []

    class Admin:
        def step_up(self):
            pass

        def path(self, s):
            return "/api/v1/admin" + s

        def post(self, path, body_, expect=None):
            posted.append((path, body_))
            return SimpleNamespace(status=status, text=lambda: body)

    sim = SimpleNamespace(doc=rendered(list(doc_files)), admin_adm=Admin())
    case = next(r for r in trf.RELOAD_REFUSALS if r[0] == cid)
    trf.test_dp18_r3_a_reload_that_breaks_an_rf_copy_rule_is_refused(*case, None, SimpleNamespace(out="/nonexistent"), None, sim)
    return posted


def test_flow_r3_passes_on_a_refusal_with_the_launchers_diagnostic_and_nothing_moved(monkeypatch):
    posted = run_r3(monkeypatch, "tx_off", 422, '{"errors":[{"path":"edges.aprsis.beacon.rf_ports","msg":"RF copy requires an enabled TX-capable AX.25 port"}]}')
    path, body = posted[0]
    assert path == "/api/v1/admin/config/changes" and body["ops"] == [{"op": "replace", "path": "/edges/ax25/port/0/tx", "value": False}] and body["base_etag"] == "e1"
    posted = run_r3(monkeypatch, "callsign", 409, "RF copy requires the APRS-IS gateway and AX.25 station callsigns to match")
    assert posted[0][1]["ops"][0]["path"] == "/edges/ax25/callsign" and posted[0][1]["ops"][0]["value"] == "N0TEST-6"


def test_flow_r3_fails_when_accepted_or_the_diagnostic_or_the_state_differs(monkeypatch):
    good = "RF copy requires an enabled TX-capable AX.25 port"
    with pytest.raises(AssertionError, match="was accepted"):
        run_r3(monkeypatch, "tx_off", 200, '{"status":"applied"}')
    with pytest.raises(AssertionError, match="launcher's diagnostic"):
        run_r3(monkeypatch, "tx_off", 422, '{"error":"bad"}')
    with pytest.raises(AssertionError, match="etag"):
        run_r3(monkeypatch, "tx_off", 422, good, etag_after="e2")
    with pytest.raises(AssertionError, match="APRS-IS child changed"):
        run_r3(monkeypatch, "tx_off", 422, good, aprsis_after={"pid": 9, "restarts": 1})


def test_flow_r3_skips_without_rf_copies_or_a_writable_config(monkeypatch):
    with pytest.raises(pytest.skip.Exception, match="no object or beacon with rf_ports"):
        run_r3(monkeypatch, "tx_off", 422, "x", doc_files=("dp15-aprsis.toml", "dp18-rf.toml", "dp18-origin.toml"))
    with pytest.raises(pytest.skip.Exception, match="DVT_SUT_CONFIG_RW=1"):
        run_r3(monkeypatch, "tx_off", 422, "x", writable=False)
