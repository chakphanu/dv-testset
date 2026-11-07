# SPDX-License-Identifier: MIT
"""The pure helpers of cases/test_dp15_positions2.py (DP7 second set): the run-shape reader against the REAL snippets
(rig/config/dv-router/dp7-courtesy-*.toml rendered with dvtest.sut_config, merged key by key as the rig does), the transmitted-frame
filter against a fake TNC, the frame checks and the SSE helper. Offline: no sockets."""
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position2_support as p2  # noqa: E402
import position_support as ps  # noqa: E402
import test_dp15_positions as p1  # noqa: E402
import test_dp15_positions2 as t2  # noqa: E402

from dvtest import ax25, kiss_tnc, sut_config  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SNIPPETS = ROOT / "rig" / "config" / "dv-router"
BASE = '[node]\ncallsign = "XLX999"\nrooms = "A-Z"\n'


def rendered(tmp_path, names):
    text = sut_config.render(BASE, "172.30.0", require_ident="true", dp15_tx_enable="false",
                             extras=[(SNIPPETS / n).read_text() for n in names])
    path = tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return SimpleNamespace(out=str(tmp_path))


COURTESY = ["data-ident.toml", "dp15-aprsis.toml", "dp7-courtesy-tx.toml", "dp7-courtesy-filter.toml", "dp7-courtesy-path.toml",
            "dp7-position-optout.toml"]


def shape(tmp_path, names):
    return t2.shape2_of(p1.read_config(rendered(tmp_path, names)))


# ------------------------------------------------------------------ the run shapes

def test_courtesy_shape_with_the_path_on(tmp_path):
    s = shape(tmp_path, COURTESY)
    assert s.rf and s.rf_port == 8101 and s.tx_on and s.to_rf and s.courtesy and s.aprsis
    assert s.positions and s.optout == {"N0OPT", "N0DMP"} and "N0PS" in s.filter_extra and not s.igate
    doc = tomllib.loads((tmp_path / "xlxd-config" / "xlxd-sut" / "dv-router.toml").read_text())
    assert doc["edges"]["aprsis"]["filter_extra"] == "p/N0PS/N0OPT" and doc["edges"]["aprsis"]["callsign"] == "N0TEST-5", \
        "the snippet must merge into dp15-aprsis.toml's table key by key"
    path = next(p for p in doc["data"]["path"] if p["name"] == "dp7-is-to-rf")
    assert path["types"] == ["transit"] and path["from"] == "edge:aprsis" and path["to"] == "edge:ax25/rf-a"
    port = doc["edges"]["ax25"]["port"][0]
    assert port["tx"] is True and port["room"] == "B" and port["tx_per_min"] >= 3 and port["physical"] and port["tx_budget"] == {}


def test_courtesy_shape_with_the_path_off(tmp_path):
    names = [n if n != "dp7-courtesy-path.toml" else "dp7-courtesy-path-off.toml" for n in COURTESY]
    s = shape(tmp_path, names)
    assert s.tx_on and s.to_rf and s.courtesy is False


def test_receive_only_shape_has_no_path_and_no_tx(tmp_path):
    names = ["data-ident.toml", "dp15-aprsis.toml", "dp7-rf.toml", "dp7-courtesy-filter.toml", "dp7-position-optout.toml"]
    s = shape(tmp_path, names)
    assert s.rf and not s.tx_on and not s.to_rf and not s.courtesy and s.aprsis and "N0PS" in s.filter_extra


def test_igate_and_the_first_set_snippets_combine_with_the_tx_edge(tmp_path):
    s = shape(tmp_path, COURTESY + ["dp7-rf-igate.toml", "dp7-aprsis-up.toml"])
    assert s.tx_on and s.courtesy and s.igate


def test_positions_off_and_unreadable(tmp_path):
    s = shape(tmp_path / "a", COURTESY[:5] + ["dp7-positions-off.toml"])
    assert s.positions is False and s.tx_on
    gone = t2.shape2_of(p1.read_config(SimpleNamespace(out=str(tmp_path / "none"))))
    assert gone.known is False and gone.positions and not gone.rf and not gone.tx_on and gone.optout == set()


def test_blocked_correspondent():
    assert t2.blocked_correspondent(SimpleNamespace(positions=False, optout=set())) == (t2.CORR_POSOFF, "[data] positions = false")
    assert t2.blocked_correspondent(SimpleNamespace(positions=True, optout={"N0OPT"}))[0] == "N0OPT"
    assert t2.blocked_correspondent(SimpleNamespace(positions=True, optout=set())) is None


def test_the_correspondents_are_distinct_and_covered_by_the_filter():
    calls = [t2.CORR_OK, t2.CORR_NOCREDIT, t2.CORR_OFF, t2.CORR_TXOFF, t2.CORR_POSOFF, t2.CORR_HIDE]
    assert len(set(calls)) == len(calls)
    assert all(c.startswith("N0PS") for c in calls), "p/N0PS in dp7-courtesy-filter.toml must pass every correspondent"
    others = [t2.HIDE_RF_CALL, t2.HIDE_RF_CONTROL, t2.SSE_MARK, t2.SSE_CONTROL, t2.SSE_HIDE, t2.RETENTION_CALL, *t2.ITEM_CALL.values()]
    # distinct from each other and from every base callsign of the first set (the controller admits 1 position per 30 s per base)
    first = set()
    for table in (p1.A_CALL, p1.CONTROL_CALL, p1.OFF_CALL, p1.AMB_M17_CALL, p1.AMB_RF_CALL):
        first |= set(table.values())
    first |= {p1.AMB_DPRS_CALL, p1.UPLINK_CONTROL_CALL, p1.TRANSIT_CONTROL_CALL, p1.OBJECT_REPORTER, p1.HIDE_CALL, p1.OPT_CALL, p1.OPT_DMR}
    assert len(set(others)) == len(others)
    assert not (set(others) | set(calls)) & first
    assert len(set(t2.ITEM_NAME.values())) == 2 and all(3 <= len(n) <= 9 for n in t2.ITEM_NAME.values())
    assert ps.base_call(t2.RX_STATION) not in set(calls) | set(others)


# ------------------------------------------------------------------ transmitted frames

class FakeTnc:
    def __init__(self):
        self.frames = []          # (time, frame bytes)
        self.injected = []

    def decoded(self, since=0.0):
        out = []
        for at, frame in self.frames:
            if at >= since:
                out.append((at, ax25.parse_frame(frame), ax25.to_tnc2(frame)))
        return out

    def inject(self, frame, channel=0):
        self.injected.append(frame)
        return True


def gated(src, info, at, gateway="N0TEST-1"):
    inner = "}%s>APDVT1,TCPIP,%s*:%s" % (src, gateway, info)
    return at, kiss_tnc.ui_frame(gateway, "APZDVR", inner)


def test_tx_frames_and_filters():
    tnc = FakeTnc()
    tnc.frames = [
        gated("N0PSD-1", ":N0PRX-2  :hello{1", 10.0),
        gated("N0PSD-1", "!1030.00N/02015.00E>cp", 12.0),
        gated("N0PSX-1", "!1030.00N/02015.00E>other", 13.0),
        (14.0, kiss_tnc.ui_frame("N0TEST-1", "APZDVR", ">beacon")),
    ]
    assert [t[2]["inner_src"] for t in t2.tx_frames(tnc, 0.0)] == ["N0PSD-1", "N0PSD-1", "N0PSX-1"]
    assert len(t2.tx_from(tnc, 0.0, "N0PSD")) == 2
    assert len(t2.tx_from(tnc, 0.0, "N0PSD", positions=True)) == 1
    assert len(t2.tx_from(tnc, 0.0, "N0PSD", positions=False)) == 1
    assert t2.tx_from(tnc, 11.0, "N0PSD", positions=False) == []
    assert t2.tx_from(tnc, 0.0, "N0PSY") == []


def test_check_message_and_courtesy_frame():
    at, frame = gated("N0PSD-1", ":N0PRX-2  :dp7 courtesy 1234{1", 1.0)
    parsed = ax25.parse_frame(frame)
    third = p2.third_party(parsed["info"])
    t2.check_message_frame((at, parsed, third), "N0PSD-1", "dp7 courtesy 1234")
    with pytest.raises(AssertionError):
        t2.check_message_frame((at, parsed, third), "N0PSD-1", "other text")
    info = ps.position_info(ps.LAT, ps.LON, 0, "/>", "cp 1")
    at2, frame2 = gated("N0PSD-1", info, 2.0)
    parsed2 = ax25.parse_frame(frame2)
    t2.check_courtesy_frame((at2, parsed2, p2.third_party(parsed2["info"])), "N0PSD-1", info)
    with pytest.raises(AssertionError):
        t2.check_courtesy_frame((at2, parsed2, p2.third_party(parsed2["info"])), "N0PSD-1", info + "x")
    wrong = ax25.parse_frame(kiss_tnc.ui_frame("N0TEST-2", "APZDVR", "}N0PSD-1>APDVT1,TCPIP,N0TEST-2*:" + info))
    with pytest.raises(AssertionError):
        t2.check_courtesy_frame((0.0, wrong, p2.third_party(wrong["info"])), "N0PSD-1", info)


def test_rf_hear_injects_a_unique_status(monkeypatch):
    monkeypatch.setattr(t2.time, "sleep", lambda s: None)
    tnc = FakeTnc()
    t2.rf_hear(tnc)
    parsed = ax25.parse_frame(tnc.injected[0])
    assert parsed["src"]["text"] == t2.RX_STATION and parsed["info"].startswith(b">dp7 heard ")


def test_send_helpers_build_the_lines():
    sent = []

    class Client:
        def send_line(self, line):
            sent.append(line)
            return (0.0, line)

    t2.send_is_message(Client(), "N0PSD-1", "hello", "7")
    info = t2.send_is_position(Client(), "N0PSD-1", "cp")
    assert sent[0] == "N0PSD-1>APDVT1,TCPIP*::N0PRX-2  :hello{7"
    assert sent[1] == "N0PSD-1>APDVT1,TCPIP*:" + info and info == "!1030.00N/02015.00E>cp"


def test_rows_of_a_watch():
    class Watch:
        def data_events(self, since=0.0):
            return [(1.0, {"item_type": "position", "callsign": "N0PGH-1"}), (2.0, {"item_type": "text", "callsign": "N0PGH"}),
                    (3.0, {"item_type": "object", "callsign": "N0PGK"})]
    assert len(t2.rows_of(Watch(), "N0PGH")) == 1 and len(t2.rows_of(Watch(), "N0PGK")) == 1
