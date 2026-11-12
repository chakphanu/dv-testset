# SPDX-License-Identifier: MIT
"""dvtest/sdp_offer.py and cases/test_web_rtc_mid.py (FLK10, DV-Router 98c88b2): the hand-built offers, the verdict logic, the switch, and the
audit that no rig SDP is built or rewritten without keeping its mids. Pure logic, no rig."""
import importlib
import json
from pathlib import Path

import pytest

from dvtest import metrics, refusals as rf, sdp_offer as so
from dvtest.tools import webload_gen as gen

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def sections(sdp):
    return so.mid_values(sdp)


# ---------------------------------------------------------------- the offers

def test_the_limit_is_sixteen_bytes():
    assert so.MID_MAX == 16
    assert len(so.at_limit_mid()) == 16 and len(so.over_long_mid()) == 17


def test_build_offer_shapes_and_crlf():
    sdp = so.build_offer(["audio"])
    assert sdp.startswith("v=0\r\n") and sdp.endswith("\r\n") and "\n" not in sdp.replace("\r\n", "")
    assert sdp.count("m=") == 1 and sdp.count("m=audio ") == 1
    assert sections(sdp) == [["audio"]]
    assert "a=group:BUNDLE audio" in sdp and "a=rtpmap:0 PCMU/8000" in sdp and "a=recvonly" in sdp
    assert "a=fingerprint:sha-256 " + ":".join(["AB"] * 32) in sdp
    two = so.build_offer(["0", "1"])
    assert sections(two) == [["0"], ["1"]] and "m=application 9 UDP/DTLS/SCTP webrtc-datachannel" in two
    assert "a=group:BUNDLE 0 1" in two


def test_build_offer_can_leave_out_or_repeat_a_mid():
    assert sections(so.build_offer([None])) == [[]]
    assert "a=group:BUNDLE" not in so.build_offer([None])
    assert sections(so.build_offer([""])) == [[""]] and "a=mid:\r\n" in so.build_offer([""])
    assert sections(so.build_offer([("a", "b")])) == [["a", "b"]]
    assert sections(so.build_offer(["audio", None])) == [["audio"], []]
    with pytest.raises(ValueError):
        so.build_offer([])
    with pytest.raises(ValueError):
        so.build_offer(["a", "b", "c"])


def test_the_ufrag_differs_per_offer():
    seen = [sdp for _, sdp in so.refused_offers() + so.control_offers()]
    ufrags = [line for sdp in seen for line in sdp.splitlines() if line.startswith("a=ice-ufrag:")]
    assert len(ufrags) == len(set(ufrags)) == len(seen)


def test_every_refused_offer_breaks_the_rule_and_every_control_keeps_it():
    names = [n for n, _ in so.refused_offers()]
    assert names[:4] == ["no_mid", "duplicate_mid", "empty_mid", "over_long_mid"]
    assert len(set(names)) == len(names)
    for name, sdp in so.refused_offers():
        assert so.mid_problems(sdp), name
    for name, sdp in so.control_offers():
        assert so.mid_problems(sdp) == [], name
    assert [n for n, _ in so.control_offers()] == ["plain", "at_limit"]
    assert sections(so.control_offers()[1][1]) == [[so.at_limit_mid()]]


@pytest.mark.parametrize("sdp,expect", [
    ("v=0\r\nm=audio 9 X 0\r\na=mid:0\r\n", []),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:0\r\nm=application 9 Y z\r\na=mid:1\r\n", []),
    ("v=0\r\nm=audio 9 X 0\r\n", ["section 0 has 0 a=mid lines"]),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:\r\n", ["section 0 has an empty mid"]),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:0\r\na=mid:1\r\n", ["section 0 has 2 a=mid lines"]),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:%s\r\n" % ("a" * 16), []),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:%s\r\n" % ("a" * 17), ["section 0 mid is 17 bytes (limit 16)"]),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:a b\r\n", ["section 0 mid has a byte that is not graphic ASCII"]),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:x\r\nm=application 9 Y z\r\na=mid:x\r\n", ["section 1 repeats the mid of the previous section"]),
    ("v=0\r\nm=audio 9 X 0\r\na=mid:x\r\nm=application 9 Y z\r\n", ["section 1 has 0 a=mid lines"]),
    ("v=0\r\na=mid:x\r\n", ["no media section"]),
])
def test_mid_problems_mirror_the_rule(sdp, expect):
    assert so.mid_problems(sdp) == expect


# ---------------------------------------------------------------- the answer

PROBLEM_403 = json.dumps({"type": "https://example.invalid/problems/forbidden", "title": "Forbidden", "status": 403,
                          "detail": "WebRTC offer denied"}).encode()


def test_summarize_reads_a_problem_by_its_type_or_its_code():
    got = so.summarize(403, {}, PROBLEM_403)
    assert got == {"status": 403, "code": "forbidden", "detail": "WebRTC offer denied", "retry_after": None, "sess_id": None}
    got = so.summarize(403, {"retry-after": "5"}, json.dumps({"code": "forbidden", "detail": "x" * 300}).encode())
    assert got["code"] == "forbidden" and len(got["detail"]) == 120 and got["retry_after"] == "5"
    ok = so.summarize(200, {}, json.dumps({"sdp": "v=0", "sess_id": "S1"}))
    assert ok["status"] == 200 and ok["sess_id"] == "S1" and ok["code"] is None


@pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b"null"])
def test_summarize_survives_a_body_that_is_not_an_object(body):
    got = so.summarize(502, None, body)
    assert got["status"] == 502 and got["code"] is None and got["detail"] is None and got["sess_id"] is None


def test_refusal_contract_is_403_forbidden_without_retry_after():
    good = so.summarize(403, {}, PROBLEM_403)
    assert so.refusal_problems("x", good) == [] and so.detail_matches(good) and so.is_sdp_refusal(good)
    capacity = so.summarize(503, {"retry-after": "5"}, json.dumps({"code": "unavailable", "detail": "WebRTC listener capacity reached"}))
    problems = so.refusal_problems("x", capacity)
    assert len(problems) == 3 and "503" in problems[0] and "unavailable" in problems[1] and "Retry-After" in problems[2]
    assert not so.is_sdp_refusal(capacity)
    other = so.summarize(403, {}, json.dumps({"code": "csrf", "detail": "csrf"}))
    assert not so.is_sdp_refusal(other) and not so.detail_matches(other)
    assert not so.is_sdp_refusal(so.summarize(200, {}, json.dumps({"sess_id": "S"})))


# ---------------------------------------------------------------- the counters

def snap(edge=None, web=None):
    lines = ['%s{proc="webrtc",reason="%s"} %s' % (rf.EDGE_FAMILY, r, (edge or {}).get(r, 0)) for r in rf.EDGE_REASONS]
    lines += ['%s{proc="web",reason="%s"} %s' % (rf.WEB_FAMILY, r, (web or {}).get(r, 0)) for r in rf.WEB_REASONS]
    return rf.snapshot(metrics.parse("\n".join(lines) + "\n"))


def test_counter_problems_pass_when_nothing_moved():
    assert so.counter_problems(snap(), snap()) == []
    assert so.counter_problems(snap({"slots": 2}, {"slots": 2}), snap({"slots": 2}, {"slots": 2})) == []


def test_counter_problems_name_every_moved_reason():
    bad = so.counter_problems(snap(), snap(edge={"join_window": 1}, web={"login_circuits": 2, "join_window": 1}))
    assert len(bad) == 2
    assert rf.EDGE_FAMILY in bad[0] and "join_window +1" in bad[0]
    assert rf.WEB_FAMILY in bad[1] and "join_window +1" in bad[1] and "login_circuits +2" in bad[1]


def test_counter_problems_refuse_an_unexposed_family():
    gone = {"edge": None, "web": snap()["web"]}
    assert "not exposed" in so.counter_problems(snap(), gone)[0]
    assert so.counter_problems(gone, snap())[0].startswith("before:")


# ---------------------------------------------------------------- the switch and the case file

def test_switch_values():
    assert not so.switch_on({}) and not so.switch_on({"DVT_FLK10": ""}) and not so.switch_on({"DVT_FLK10": "off"})
    for value in ("on", "ON", "1", "true", " yes "):
        assert so.switch_on({"DVT_FLK10": value}), value
    assert "DVT_FLK10=on" in so.skip_reason()


def _skipif(module):
    marks = [m for m in module.pytestmark if m.name == "skipif"]
    assert len(marks) == 1
    return marks[0]


def test_the_case_file_skips_naming_the_switch_without_it(monkeypatch):
    monkeypatch.delenv("DVT_FLK10", raising=False)
    import test_web_rtc_mid as case
    case = importlib.reload(case)
    mark = _skipif(case)
    assert mark.args == (True,) and "DVT_FLK10=on" in mark.kwargs["reason"]
    monkeypatch.setenv("DVT_FLK10", "on")
    case = importlib.reload(case)
    assert _skipif(case).args == (False,)
    monkeypatch.delenv("DVT_FLK10")
    importlib.reload(case)


def test_the_case_file_has_the_named_cases():
    import test_web_rtc_mid as case
    params = [m for m in case.test_refused_offer.pytestmark if m.name == "parametrize"][0]
    assert [n for n, _ in params.args[1]][:4] == ["no_mid", "duplicate_mid", "empty_mid", "over_long_mid"]
    assert callable(case.test_mid_at_the_limit_is_not_refused) and callable(case.test_mid_refusals_move_no_refusal_counter)
    assert case.ROOM == "V" and case.OFFER_GAP_S >= 0.7


class FakeClient:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    def request(self, method, path, json_body=None, **kw):
        self.sent.append((method, path, json_body))

        class R:
            pass
        r = R()
        r.status, r.headers, r.body = self.replies.pop(0)
        return r


def test_post_offer_and_close(monkeypatch):
    import test_web_rtc_mid as case
    monkeypatch.setattr(case.time, "sleep", lambda s: None)
    client = FakeClient([(200, {}, json.dumps({"sdp": "v=0", "sess_id": "S9"}).encode()), (204, {}, b"")])
    answer = case.post_offer(client, "v=0\r\n")
    assert client.sent[0] == ("POST", "/api/v1/rtc/offer", {"sdp": "v=0\r\n", "room": "V"})
    assert answer["sess_id"] == "S9"
    case.close_circuit(client, answer)
    assert client.sent[1] == ("POST", "/api/v1/rtc/close", {"sess_id": "S9"})
    case.close_circuit(client, {"sess_id": None})
    assert len(client.sent) == 2


# ---------------------------------------------------------------- the audit of the rig's own SDP

def test_the_load_generator_keeps_every_mid_it_gets_from_aiortc():
    """webload_gen.relay_sdp only drops the sha-384 / sha-512 fingerprint lines of aiortc's offer; the `a=mid` lines (aiortc numbers them
    "0", "1") and everything else stay."""
    fp = ":".join(["AB"] * 32)
    offer = ("v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\nc=IN IP4 0.0.0.0\r\n"
             "a=mid:0\r\na=recvonly\r\na=fingerprint:sha-256 %s\r\na=fingerprint:sha-384 %s\r\na=fingerprint:sha-512 CD\r\na=setup:actpass\r\n" % (fp, fp))
    assert so.mid_problems(offer) == []
    out = gen.relay_sdp(offer)
    assert so.mid_problems(out) == [] and sections(out) == [["0"]]
    assert [l for l in out.splitlines() if l.startswith("a=mid:")] == ["a=mid:0"]
    two = offer + "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:1\r\na=sctp-port:5000\r\n"
    assert sections(gen.relay_sdp(two)) == [["0"], ["1"]] and so.mid_problems(gen.relay_sdp(two)) == []


def test_no_rig_python_file_builds_sdp_text_except_the_one_helper():
    """The audit as a guard: outside cases/unit/, the only Python that writes `m=audio` lines is dvtest/sdp_offer.py. Browsers and aiortc build
    every other offer; test_web_webrtc.py only reads a browser's SDP for facts. A new file with SDP text must keep its mids and be listed here."""
    allowed = {"dvtest/sdp_offer.py": "FLK10 helper (this unit)", "cases/test_web_webrtc.py": "reads the browser's SDP for facts (m=application)"}
    found = {}
    for base in ("cases", "dvtest"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("cases/unit/"):
                continue
            text = path.read_text(encoding="utf-8")
            if "m=audio" in text or "m=application" in text:
                found[rel] = True
    extra = sorted(set(found) - set(allowed) - {"cases/test_web_rtc_mid.py", "dvtest/target.py"})
    assert not extra, "SDP text in %s: keep an a=mid per m-line and add the file to this audit" % extra
