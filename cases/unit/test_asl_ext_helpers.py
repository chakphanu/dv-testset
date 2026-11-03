# SPDX-License-Identifier: MIT
"""dvtest.asl_ext: registration table, lstats, and call / text / audio extraction from frames JSON."""
import pytest

from dvtest import asl_ext

pytestmark = pytest.mark.unit

REG = """Host                                           Username    Perceived                            Refresh  State
register.allstarlink.org:443                   400169      192.0.2.7:4569                           180  Registered
1 HTTP registration.
"""
REG_BAD = """Host                                           Username    Perceived                            Refresh  State
register.allstarlink.org:443                   400169      <Unregistered>                           180  Not Registered
"""


def test_parse_registrations():
    rows = asl_ext.parse_registrations(REG)
    assert rows == [{"host": "register.allstarlink.org:443", "user": "400169", "perceived": "192.0.2.7:4569",
                     "refresh": 180, "state": "Registered"}]
    assert asl_ext.registered(rows, 400169) == rows[0]
    bad = asl_ext.parse_registrations(REG_BAD)
    assert bad[0]["perceived"] is None and bad[0]["state"] == "Not Registered"
    assert asl_ext.registered(bad, 400169) is None
    assert asl_ext.registered(rows, 400170) is None


def test_lstats_state():
    text = "NODE      PEER                RECONNECTS  DIRECTION  CONNECT TIME        CONNECT STATE\n400161    192.0.2.9           0           OUT        00:00:01:000        ESTABLISHED\n"
    assert asl_ext.lstats_state(text, 400161) == "ESTABLISHED"
    assert asl_ext.lstats_state(text, 400168) is None


def _doc():
    def fr(n, t, kind, src, dst, scall, dcall, hexs="", **kw):
        d = {"frame": n, "t": t, "type": kind, "src": src, "dst": dst, "scall": scall, "dcall": dcall, "hex": hexs}
        d.update(kw)
        return d
    own, peer = "10.0.0.2:4569", "192.0.2.9:4569"
    new = fr(1, 0.0, "iax.NEW", own, peer, 7, 0, ies=[{"ie": 1, "name": "CALLED_NUMBER", "text": "400161"}])
    types = {
        "iax.NEW": [new, dict(new, frame=3, t=0.1)],
        "iax.ACCEPT": [fr(4, 0.2, "iax.ACCEPT", peer, own, 21, 7)],
        "control.ANSWER": [fr(5, 0.21, "control.ANSWER", peer, own, 21, 7)],
        "text": [fr(6, 0.3, "text", own, peer, 7, 21, text="L \x00"), fr(7, 0.35, "text", peer, own, 21, 7, text="!NEWKEY1!\x00")],
        "voice.full": [fr(8, 1.0, "voice.full", peer, own, 21, 7, ("00" * 12) + "aabb", codec="ulaw")],
        "voice.mini": [fr(9, 1.02, "voice.mini", peer, own, 21, 0, ("00" * 4) + "ccdd"),
                       fr(10, 1.03, "voice.mini", own, peer, 7, 0, ("00" * 4) + "ffff")],
        "iax.HANGUP": [fr(11, 2.0, "iax.HANGUP", own, peer, 7, 21)],
    }
    dns = [{"qr": 1, "rcode": 0, "frame": 2, "t": 0.05, "questions": [{"name": "_iax._udp.400161.nodes.allstarlink.org", "type": "SRV"}],
            "answers": [{"name": "x", "type": "SRV", "text": "0 0 4569 400161.nodes.allstarlink.org"}]},
           {"qr": 1, "rcode": 0, "frame": 2, "t": 0.06, "questions": [{"name": "400161.nodes.allstarlink.org", "type": "A"}],
            "answers": [{"name": "400161.nodes.allstarlink.org", "type": "A", "text": "192.0.2.9"}]}]
    return {"types": types, "dns": dns}


def test_calls_text_audio_dns():
    doc = _doc()
    [call] = asl_ext.calls(doc)
    assert call["called"] == "400161" and call["accepted"] and call["answered"] and call["peer_scall"] == 21
    assert call["hangup_by"] == "own"
    assert [(e["dir"], e["text"]) for e in asl_ext.text_exchange(doc, call)] == [("out", "L \\0"), ("in", "!NEWKEY1!\\0")]
    assert asl_ext.rx_audio(doc, call) == bytes.fromhex("aabbccdd")
    d = asl_ext.dns_for(doc, 400161)
    assert d["srv"] == ["0 0 4569 400161.nodes.allstarlink.org"] and d["a"][0]["ip"] == "192.0.2.9"
