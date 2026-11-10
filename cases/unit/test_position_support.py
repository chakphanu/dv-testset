# SPDX-License-Identifier: MIT
"""cases/position_support.py (DP7 position cases): the display rule of the public positions page ported from
web/site/positions.js, the box centre derived independently of the parser, the APRS strings with an ambiguity level and
the object builder, the DMR id map against rig/config/dmrid.dat, and the row helpers. Offline."""
import random
import re
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position_support as ps  # noqa: E402

from dvtest import aprs_expect  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ format_coordinate (hand vectors of the JS rule)

@pytest.mark.parametrize("e7,level,want", [
    (490583333, 0, "49.05833"),          # 5 places, rounded
    (490583350, 0, "49.05834"),          # half up
    (-720291667, 0, "-72.02917"),
    (490591667, 1, "49.059"),            # 3 places, truncated
    (-720250000, 2, "-72.02"),           # toward zero
    (490833333, 3, "49.0"),
    (-720833333, 3, "-72.0"),
    (495000000, 4, "49"),                # level 4: the sent whole degree
    (-725000000, 4, "-72"),
    (-3000, 2, "0.00"),                  # no minus sign for a zero text
    (0, 0, "0.00000"),
    (900000000, 0, "90.00000"),
])
def test_format_coordinate_hand_vectors(e7, level, want):
    assert ps.format_coordinate(e7, level) == want


def test_format_coordinate_rejects_a_level_outside_0_to_4():
    with pytest.raises(ValueError):
        ps.format_coordinate(1, 5)


# ------------------------------------------------------------------ the box centre, independent of the parser

def parser_minutes(digits, level):
    """The rule of crates/lib/pkt/src/aprs/position.rs centered_minutes (read from the source): digits are the four minute
    digits (tens, units, tenths, hundredths) with the blanked ones as None; the first blanked digit becomes 5, the rest 0;
    level 4 is 30 minutes."""
    known = 4 - level
    d = [x if x is not None else 0 for x in digits]
    if level == 4:
        return Fraction(30)
    if level > 0:
        d[known] = 5
        for i in range(known + 1, 4):
            d[i] = 0
    return Fraction(d[0] * 10 + d[1]) + Fraction(d[2] * 10 + d[3], 100)


def digits_of(total_hundredths_of_a_minute, level):
    minutes, hundredths = divmod(total_hundredths_of_a_minute, 100)
    digits = [minutes // 10, minutes % 10, hundredths // 10, hundredths % 10]
    for i in range(4 - level, 4):
        digits[i] = None
    return digits


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_box_centre_equals_the_parsers_rule_for_random_positions(level):
    rng = random.Random(7000 + level)
    for _ in range(300):
        deg = rng.randrange(0, 180)
        minutes_hund = rng.randrange(0, 6000)
        negative = rng.random() < 0.5
        digits = digits_of(minutes_hund, level)
        lower = deg * 6000 + sum((d or 0) * 10 ** (3 - i) for i, d in enumerate(digits))      # blanks as zeros
        if lower == 0:
            continue            # an integer axis cannot carry the hemisphere of a zero lower edge (documented limit)
        exact = {"int": -lower if negative else lower, "per_degree": 6000}
        parser = deg + parser_minutes(digits, level) / 60
        want = -parser if negative else parser
        assert ps.box_centre_degrees(exact, level) == want, (deg, minutes_hund, level, negative)


def test_expected_display_of_the_example_position_at_every_level():
    # 4903.50N 07201.75W, the specification's own example
    want = {0: ("49.05833", "-72.02917"), 1: ("49.059", "-72.029"), 2: ("49.05", "-72.02"), 3: ("49.0", "-72.0"), 4: ("49", "-72")}
    for level, (lat, lon) in want.items():
        la, lo, amb = ps.exact_axes(ps.APRS_LAT, ps.APRS_LON, level)
        assert amb == level
        assert (ps.expected_display(la, level), ps.expected_display(lo, level)) == (lat, lon)


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_page_text_of_the_stored_centre_equals_the_expected_text(level):
    # what the SUT stores (the centre rounded to e7) printed by the page rule equals the text from the generator's exact value
    la, lo, _amb = ps.exact_axes(ps.APRS_LAT, ps.APRS_LON, level)
    lat_text = ps.format_coordinate(ps.centre_e7(la, level), level)
    lon_text = ps.format_coordinate(ps.centre_e7(lo, level), level)
    assert (lat_text, lon_text) == (ps.expected_display(la, level), ps.expected_display(lo, level))


def test_southern_position_text_is_the_truncated_centre():
    la, lo, _ = ps.exact_axes(-33.8688, 151.2093, 2)
    assert ps.expected_display(la, 2) == "-33.87"
    assert ps.expected_display(lo, 2) == "151.20"                # 151 deg 12.50' = 151.208333


# ------------------------------------------------------------------ APRS strings

def test_plain_text_matches_the_ambiguity_examples_of_the_generator():
    # dvtest/aprs_cases.py amb-1 .. amb-4 are the strings for the position 4903.50N/07201.75W blanked 1..4 digits
    assert ps.plain_text(ps.APRS_LAT, ps.APRS_LON, 0) == "4903.50N/07201.75W>"
    assert ps.plain_text(ps.APRS_LAT, ps.APRS_LON, 1) == "4903.5 N/07201.7 W>"
    assert ps.plain_text(ps.APRS_LAT, ps.APRS_LON, 2) == "4903.  N/07201.  W>"
    assert ps.plain_text(ps.APRS_LAT, ps.APRS_LON, 3) == "490 .  N/0720 .  W>"
    assert ps.plain_text(ps.APRS_LAT, ps.APRS_LON, 4) == "49  .  N/072  .  W>"


def test_position_info_and_tnc2():
    assert ps.position_info(ps.APRS_LAT, ps.APRS_LON, 2, comment="x") == "!4903.  N/07201.  W>x"
    assert ps.tnc2("N0PM2-7", "!x") == "N0PM2-7>APDVT1:!x"
    assert ps.tnc2("N0PM2-7", "!x", path="WIDE1-1") == "N0PM2-7>APDVT1,WIDE1-1:!x"


def test_object_info_layout_and_kill_flag():
    live = ps.object_info("DVT7OBJ", 10.5, 20.25, when=1_700_000_000)         # 2023-11-14 22:13:20 UTC
    assert live == ";DVT7OBJ  *221320h1030.00N/02015.00E>dp7"
    killed = ps.object_info("DVT7OBJ", 10.5, 20.25, live=False, when=1_700_000_034)
    assert killed.startswith(";DVT7OBJ  _221354h") and len(killed) == len(live)
    assert re.fullmatch(r";.{9}[*_]\d{6}h\d{4}\.\d\d[NS]/\d{5}\.\d\d[EW]>.*", live)
    amb = ps.object_info("X", 10.5, 20.25, level=2, when=1_700_000_000)
    assert amb.startswith(";X        *221320h1030.  N/02015.  E>")
    with pytest.raises(ValueError):
        ps.object_info("TOOLONGNAME", 1, 1)


# ------------------------------------------------------------------ the DMR registry of the rig

def test_dmr_id_for_agrees_with_the_rig_registry_file():
    rows = {}
    for line in (ROOT / "rig" / "config" / "dmrid.dat").read_text().splitlines():
        ident, call, _comment = line.split(";")
        rows[int(ident)] = call
    for call in ("N0CALL", "N0TEST", "N0DMP", "STR001", "STR041", "STR120", "str077"):
        assert rows[ps.dmr_id_for(call)] == ps.base_call(call)
    assert ps.dmr_id_for("STR121") is None and ps.dmr_id_for("N0OPT") is None and ps.dmr_id_for("STR000") is None


# ------------------------------------------------------------------ rows

def row(call="N0PAD", item_type="position", room="B", **body):
    base = {"lat_e7": 1, "lon_e7": 2, "alt_dm": 0, "course_deg": 65535, "speed_dmh": 0, "fix_unix_ms": 0, "symbol": "/>",
            "source": 1, "ambiguity": 0, "comment": ""}
    base.update(body)
    return {"type": "data", "id": 5, "item_type": item_type, "room": room, "callsign": call, "protocol": "dextra",
            "ts_ms": 1, "ident": "stream", "via": "rf", "body": base}


def test_base_call_and_same_base():
    assert ps.base_call("n0opt-7") == "N0OPT" and ps.base_call("N0OPT/P") == "N0OPT" and ps.base_call(" N0OPT A") == "N0OPT"
    assert ps.same_base("N0OPT-9", "N0OPT") and not ps.same_base("N0OPTX", "N0OPT")


def test_state_rows_and_selectors():
    state = {"rooms": [{"room": "A", "positions": []}, {"room": "B", "positions": [row("N0PAD-7"), row("N0PAM"),
                                                                                     row("N0PAM", item_type="object", name="OBJ1", state=1)]}]}
    rows = ps.state_rows(state)
    assert len(rows) == 3 and all(r["room"] == "B" for r in rows)
    assert ps.state_rows(state, "A") == [] and len(ps.state_rows(state, "B")) == 3
    assert [r["callsign"] for r in ps.spatial_rows(rows, "N0PAD")] == ["N0PAD-7"]
    assert len(ps.spatial_rows(rows, "N0PAM", "object")) == 1 and ps.spatial_rows(rows, name="OBJ1")[0]["item_type"] == "object"
    assert ps.position_rows(rows, "N0PAM")[0]["item_type"] == "position"
    assert ps.state_rows(None) == [] and ps.state_rows({"rooms": [{"room": "B"}]}) == []
    assert ps.spatial_rows([{"item_type": "text"}, "x"]) == []


def test_check_public_row_accepts_the_schema_and_refuses_extras():
    ps.check_public_row(row())
    ps.check_public_row(row(item_type="object", name="OBJ1", state=1))
    kill = row(item_type="object")
    kill["body"] = {"name": "OBJ1", "state": 0}
    ps.check_public_row(kill)
    leak = row()
    leak["radio_id"] = 1023008
    with pytest.raises(AssertionError, match="allow-list"):
        ps.check_public_row(leak)
    leak = row()
    leak["body"]["origin"] = "x"
    with pytest.raises(AssertionError, match="allow-list"):
        ps.check_public_row(leak)
    bad_kill = row(item_type="object")
    bad_kill["body"] = {"name": "OBJ1", "state": 0, "lat_e7": 1}
    with pytest.raises(AssertionError, match="allow-list"):
        ps.check_public_row(bad_kill)


def test_check_position_and_check_displayed():
    r = row(lat_e7=105000100, lon_e7=202500000, source=3, symbol="/[", comment="FT-1D")
    ps.check_position(r, source=3, symbol="/[", comment="FT-1D", ambiguity=0)
    with pytest.raises(AssertionError):
        ps.check_position(r, source=4)
    with pytest.raises(AssertionError):
        ps.check_position(r, ambiguity=2)
    la, lo, _ = ps.exact_axes(ps.APRS_LAT, ps.APRS_LON, 2)
    good = row(lat_e7=490583333, lon_e7=-720250000, ambiguity=2, source=5)
    assert ps.check_displayed(good, la, lo, 2) == ("49.05", "-72.02")
    refined = row(lat_e7=490583333, lon_e7=-720250000, ambiguity=0, source=5)       # the ambiguity was dropped
    with pytest.raises(AssertionError, match="ambiguity"):
        ps.check_displayed(refined, la, lo, 2)
    corner = row(lat_e7=490500000, lon_e7=-720166667, ambiguity=2, source=5)        # the lower corner, not the centre
    with pytest.raises(AssertionError, match="box centre"):
        ps.check_displayed(corner, la, lo, 2)


# ------------------------------------------------------------------ polling helpers against a fake client

class FakeResp:
    def __init__(self, doc):
        self.doc = doc

    def json(self):
        return self.doc


class FakeAnon:
    def __init__(self, docs):
        self.docs = list(docs)
        self.calls = 0

    def get(self, path, **kw):
        assert path == ps.PUBLIC_STATE
        doc = self.docs[min(self.calls, len(self.docs) - 1)]
        self.calls += 1
        return FakeResp(doc)


def test_wait_public_row_polls_until_the_row_appears(monkeypatch):
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    empty = {"rooms": [{"room": "B", "positions": []}]}
    full = {"rooms": [{"room": "B", "positions": [row("N0PAD-1")]}]}
    anon = FakeAnon([empty, empty, full])
    found, rows = ps.wait_public_row(anon, "N0PAD", timeout=30, room="B")
    assert found["callsign"] == "N0PAD-1" and anon.calls == 3 and len(rows) == 1
    found, _rows = ps.wait_public_row(FakeAnon([empty]), "N0PAD", timeout=0)
    assert found is None


def test_wait_gone(monkeypatch):
    monkeypatch.setattr(ps.time, "sleep", lambda s: None)
    full = {"rooms": [{"room": "B", "positions": [row("N0PAD")]}]}
    empty = {"rooms": [{"room": "B", "positions": []}]}
    assert ps.wait_gone(FakeAnon([full, empty]), "N0PAD", timeout=30) is True
    assert ps.wait_gone(FakeAnon([full]), "N0PAD", timeout=0) is False


def test_axis_values_are_the_generators_not_the_parsers():
    # the generator value of the blanked position is its LOWER corner (blanks as 0), the centre comes from box_centre_degrees
    la, _lo, amb = ps.exact_axes(ps.APRS_LAT, ps.APRS_LON, 2)
    assert amb == 2 and la == aprs_expect.plain("4903.  N/07201.  W>")["lat"]
    assert la == {"int": 49 * 6000 + 3 * 100, "per_degree": 6000}
    assert ps.box_centre_degrees(la, 2) == Fraction(49 * 6000 + 350, 6000)


# ------------------------------------------------------------------ DP6c (DV-Router 4ac79f26, decision D2)

def dp6c(call="N0PAD-9", hops=0, forwarded=False, **kw):
    out = row(call, **kw)
    out["hops"], out["forwarded"] = hops, forwarded
    return out


def test_check_public_row_accepts_both_shapes_and_the_dp6c_fields():
    ps.check_public_row(row())                                           # an image before DP6c
    ps.check_public_row(dp6c())
    ps.check_public_row(dp6c(hops=255, forwarded=True))
    ps.check_public_row(dp6c(item_type="object", name="OBJ1", state=1))
    kill = dp6c(item_type="object")
    kill["body"] = {"name": "OBJ1", "state": 0}
    ps.check_public_row(kill)
    assert ps.DP6C_KEYS == {"hops", "forwarded"} and ps.ROW_KEYS_DP6C == ps.ROW_KEYS | ps.DP6C_KEYS


@pytest.mark.parametrize("change,needle", [
    (lambda r: r.pop("forwarded"), "lacks forwarded"),
    (lambda r: r.pop("hops"), "lacks hops"),
    (lambda r: r.update(hops=256), "hops"),
    (lambda r: r.update(hops=-1), "hops"),
    (lambda r: r.update(hops=True), "hops"),
    (lambda r: r.update(hops="1"), "hops"),
    (lambda r: r.update(forwarded=1), "forwarded"),
    (lambda r: r.update(forwarded="no"), "forwarded"),
])
def test_check_public_row_refuses_a_half_or_malformed_dp6c_row(change, needle):
    bad = dp6c()
    change(bad)
    with pytest.raises(AssertionError, match=needle):
        ps.check_public_row(bad)


def test_check_public_row_still_refuses_other_extra_keys_on_a_dp6c_row():
    leak = dp6c()
    leak["dst_kind"] = 1                       # an operator-feed key, not a public one
    with pytest.raises(AssertionError, match="allow-list"):
        ps.check_public_row(leak)


def test_dp6c_probe_and_consistency():
    assert ps.dp6c_image([]) is None and ps.dp6c_image([row()]) is False and ps.dp6c_image([row(), dp6c()]) is True
    assert ps.dp6c_image(["x", None]) is None
    ps.check_public_rows_consistent([row(), row("N0PAE")])
    ps.check_public_rows_consistent([dp6c(), dp6c("N0PAE")])
    with pytest.raises(AssertionError, match="mix"):
        ps.check_public_rows_consistent([row(), dp6c()])
    assert ps.dp6c_field_problems(row()) == [] and ps.dp6c_field_problems(dp6c()) == []
    assert ps.dp6c_field_problems("x") == []


def test_station_identity_has_the_ssid_but_privacy_stays_per_base_callsign():
    rows = [dp6c("N0PAD-9"), dp6c("N0PAD-13"), dp6c("N0PAD"), dp6c("N0PAX-9")]
    assert [r["callsign"] for r in ps.spatial_rows(rows, "N0PAD")] == ["N0PAD-9", "N0PAD-13", "N0PAD"]       # a hide of the base covers every SSID
    assert [r["callsign"] for r in ps.spatial_rows(rows, "n0pad-1")] == ["N0PAD-9", "N0PAD-13", "N0PAD"]
    assert ps.identity_matches("N0PAD-9", "n0pad-9") and ps.identity_matches("N0PAD-9 T", "N0PAD-9")
    assert not ps.identity_matches("N0PAD-9", "N0PAD") and not ps.identity_matches("N0PAD", "N0PAD-9")


def test_annex_rows_are_not_spatial_rows():
    weather = dp6c("N0WX-13", item_type="weather")
    telemetry = dp6c("N0WX-13", item_type="telemetry")
    assert ps.is_annex(weather) and ps.is_annex(telemetry) and not ps.is_annex(dp6c()) and not ps.is_annex("x")
    assert not ps.is_spatial(weather) and not ps.is_spatial(telemetry)
    state = {"rooms": [{"room": "B", "positions": [dp6c(), weather, telemetry]}]}
    assert len(ps.state_rows(state)) == 3
    assert len(ps.spatial_rows(ps.state_rows(state))) == 1                 # position selectors ignore types 5 and 6
    assert ps.ANNEX_TYPES == ("weather", "telemetry")


def test_identity_note_never_asserts():
    assert "dp6c=True" in ps.identity_note(dp6c("N0PAD-7"), "N0PAD-7") and "same identity True" in ps.identity_note(dp6c("N0PAD-7"), "n0pad-7")
    old = ps.identity_note(row("N0PAD"), "N0PAD-7")
    assert "dp6c=False" in old and "same identity False" in old and "same base True" in old
    assert "same base False" in ps.identity_note({"callsign": "N0XXX"}, "N0PAD-7") and "''" in ps.identity_note("x", "N0PAD")
