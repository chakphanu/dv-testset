# SPDX-License-Identifier: MIT
"""dvtest.proto.svx: framing and message layouts of the SvxReflector protocol.

The expected bytes are written out by hand from the declarations in the pinned
SvxLink source (docs/svx-cases.md, "Message layouts"), field by field, and are
never produced by the module under test. The one digest was made with
`openssl dgst -sha1 -hmac` over the bytes 00..13 (a different implementation),
and the HMAC helper is also checked against RFC 2202 test case 1.
"""
import pytest

from dvtest.proto import svx

pytestmark = pytest.mark.unit


def hx(text):
    return bytes.fromhex(text.replace(" ", ""))


CHALLENGE = bytes(range(20))
# printf of the bytes 00..13 | openssl dgst -sha1 -hmac example-password
DIGEST = hx("25ef79b3a79b16a5547ecb94a264ca65178affaf")


# --- TCP builders, byte by byte ---

def test_proto_ver_3_0_is_the_hand_derived_example_of_the_research_note():
    assert svx.proto_ver(3, 0) == hx("00000006 0005 0003 0000")


def test_proto_ver_2_0_and_downgrade():
    assert svx.proto_ver(2, 0) == hx("00000006 0005 0002 0000")
    assert svx.proto_ver_downgrade(3, 0) == hx("00000006 0006 0003 0000")


def test_heartbeat_and_empty_messages():
    assert svx.heartbeat() == hx("00000002 0001")
    assert svx.auth_ok() == hx("00000002 000c")
    assert svx.start_udp_encryption() == hx("00000002 0072")


def test_auth_challenge_is_a_byte_vector_of_20():
    assert svx.auth_challenge(CHALLENGE) == hx(
        "00000018 000a 0014 000102030405060708090a0b0c0d0e0f10111213")
    with pytest.raises(svx.SvxError):
        svx.auth_challenge(b"\x00" * 19)


def test_auth_response_callsign_then_digest():
    assert svx.auth_response("N0CALL", "example-password", CHALLENGE) == hx(
        "00000020 000b 0006 4e3043414c4c 0014 "
        "25ef79b3a79b16a5547ecb94a264ca65178affaf")


def test_hmac_matches_two_independent_references():
    assert svx.auth_digest("example-password", CHALLENGE) == DIGEST
    # RFC 2202, test case 1
    assert svx.hmac_sha1(b"\x0b" * 20, b"Hi There") == hx(
        "b617318655057264e28bc0b6fb378c8ef146be00")


def test_error_carries_one_string():
    assert svx.error("Access denied") == hx(
        "00000011 000d 000d 4163636573732064656e696564")


def test_server_info_reserved_client_id_nodes_codecs():
    assert svx.server_info(0x1234, ["N0TEST"], ["OPUS"]) == hx(
        "00000018 0064 0000 1234 0001 0006 4e3054455354 0001 0004 4f505553")


def test_talker_start_and_stop_carry_tg_before_callsign():
    assert svx.talker_start(10, "N0CALL") == hx(
        "0000000e 0068 0000000a 0006 4e3043414c4c")
    assert svx.talker_stop(10, "N0CALL") == hx(
        "0000000e 0069 0000000a 0006 4e3043414c4c")


def test_select_tg_and_monitor_set_is_sorted_and_unique():
    assert svx.select_tg(9) == hx("00000006 006a 00000009")
    assert svx.tg_monitor([2, 1, 2]) == hx(
        "0000000c 006b 0002 00000001 00000002")
    assert svx.tg_monitor([]) == hx("00000004 006b 0000")


def test_node_info_v3_and_v2_differ():
    v3 = svx.node_info_v3(hx("010203040506"),
                          hx("00112233445566778899aabbccddeeff"), "{}")
    assert v3 == hx("00000020 006f 0006 010203040506 0010 "
                    "00112233445566778899aabbccddeeff 0002 7b7d")
    assert svx.node_info_v2("{}") == hx("00000006 006f 0002 7b7d")


# --- framing ---

def test_splitter_handles_coalesced_frames():
    s = svx.FrameSplitter()
    got = s.feed(svx.proto_ver(3, 0) + svx.heartbeat())
    assert got == [(5, hx("0003 0000")), (1, b"")]
    assert s.pending == 0


def test_splitter_handles_one_byte_fragments():
    stream = svx.server_info(7, ["N0TEST"], ["OPUS"]) + svx.node_joined("N0CALL")
    s = svx.FrameSplitter()
    got = []
    for i in range(len(stream)):
        got += s.feed(stream[i:i + 1])
    assert [t for t, _ in got] == [svx.SERVER_INFO, svx.NODE_JOINED]


def test_splitter_limit_applies_to_frame_len_at_the_length_word():
    s = svx.FrameSplitter(max_frame=svx.MAX_PREAUTH_FRAME)
    assert s.feed(hx("00000040")) == []          # 64 is allowed, body pending
    s = svx.FrameSplitter(max_frame=svx.MAX_PREAUTH_FRAME)
    with pytest.raises(svx.SvxError):
        s.feed(hx("00000041"))                   # 65 is not, before any body


@pytest.mark.parametrize("word", ["00000000", "00000001"])
def test_splitter_rejects_frames_too_short_for_a_type(word):
    with pytest.raises(svx.SvxError):
        svx.FrameSplitter().feed(hx(word))


# --- decoder ---

def decode(frame, proto=(3, 0)):
    (t, body), = svx.FrameSplitter().feed(frame)
    return svx.decode_tcp(t, body, proto)


def test_decode_round_trips_the_builders():
    m = decode(svx.server_info(0x1234, ["N0TEST", "N0CALL"], ["OPUS"]))
    assert (m.name, m.fields) == ("ServerInfo", {
        "reserved": 0, "client_id": 0x1234,
        "nodes": ["N0TEST", "N0CALL"], "codecs": ["OPUS"]})
    m = decode(svx.auth_response("N0CALL", "example-password", CHALLENGE))
    assert m.fields == {"callsign": "N0CALL", "digest": DIGEST}
    assert decode(svx.tg_monitor([5, 3])).fields == {"tgs": [3, 5]}
    assert decode(svx.error("Access denied")).fields == {"text": "Access denied"}


def test_decode_talker_depends_on_the_protocol_version():
    body = svx.p_str("N0CALL")
    assert svx.decode_tcp(svx.TALKER_START, body, (1, 0)).fields == {
        "callsign": "N0CALL"}
    assert svx.decode_tcp(svx.TALKER_START, svx.p_u32(10) + body, (3, 0)).fields == {
        "tg": 10, "callsign": "N0CALL"}


def test_decode_node_info_depends_on_the_protocol_version():
    v3 = hx("0006 010203040506 0010 00112233445566778899aabbccddeeff 0002 7b7d")
    assert svx.decode_tcp(svx.NODE_INFO, v3, (3, 0)).fields["json"] == "{}"
    assert svx.decode_tcp(svx.NODE_INFO, hx("0002 7b7d"), (2, 0)).fields == {
        "json": "{}"}


def test_decode_signal_strength_and_tx_status_bits():
    m = decode(hx("00000007 0070 0001 411e03"))
    assert m.fields == {"rx": [{"id": "A", "siglev": 30, "enabled": True,
                                "sql_open": True, "active": False}]}
    m = decode(hx("00000006 0071 0001 5401"))
    assert m.fields == {"tx": [{"id": "T", "transmit": True}]}


def test_decode_ca_info_has_a_u16_size():
    body = hx("0abc 0020") + bytes(32)
    m = svx.decode_tcp(svx.CA_INFO, body)
    assert m.fields["pem_size"] == 0x0ABC and len(m.fields["sha256"]) == 32


def test_decode_rejects_short_and_trailing_bytes():
    with pytest.raises(svx.SvxError):
        svx.decode_tcp(svx.SELECT_TG, hx("000000"))
    with pytest.raises(svx.SvxError):
        svx.decode_tcp(svx.SELECT_TG, hx("0000000900"))
    with pytest.raises(svx.SvxError):      # string longer than the body
        svx.decode_tcp(svx.ERROR, hx("0005 4142"))


def test_unknown_type_is_kept_raw_because_peers_ignore_it():
    m = svx.decode_tcp(0x7777, b"\x01\x02")
    assert (m.name, m.fields) == ("Unknown", {"raw": b"\x01\x02"})


# --- UDP ---

def test_udp_v2_header_is_type_client_seq():
    assert svx.udp_v2(svx.UDP_HEARTBEAT, 1, 0) == hx("0001 0001 0000")
    assert svx.udp_v2(svx.UDP_AUDIO, 1, 5, svx.udp_audio_body(hx("aabbcc"))) == hx(
        "0065 0001 0005 0003 aabbcc")
    assert svx.udp_v2(svx.UDP_FLUSH_SAMPLES, 0x0102, 0xFFFF) == hx("0066 0102 ffff")
    assert svx.udp_v2(svx.UDP_ALL_SAMPLES_FLUSHED, 1, 0x10000) == hx("0067 0001 0000")


def test_udp_v2_decode_and_audio_body():
    d = svx.decode_udp_v2(hx("0065 0001 0005 0003 aabbcc"))
    assert (d.name, d.client_id, d.seq) == ("Audio", 1, 5)
    assert svx.decode_udp_body(d.type, d.body) == {"audio": hx("aabbcc")}
    assert svx.decode_udp_body(svx.UDP_AUDIO, hx("0000")) == {"audio": b""}
    with pytest.raises(svx.SvxError):
        svx.decode_udp_v2(hx("0001 0001"))
    with pytest.raises(svx.SvxError):
        svx.decode_udp_body(svx.UDP_AUDIO, hx("0003 aabb"))


def test_udp_signal_strength_body():
    body = svx.signal_strength_body([("A", 30, 3)])
    assert body == hx("0001 411e03")
    assert svx.decode_udp_body(svx.UDP_SIGNAL_STRENGTH, body)["rx"][0]["siglev"] == 30


def test_udp_v3_registration_datagram_has_a_6_byte_aad():
    tag = hx("a0a1a2a3a4a5a6a7")
    d = svx.split_udp_v3(hx("00000000 0102") + tag + hx("bbcc"))
    assert (d.counter, d.client_id) == (0, 0x0102)
    assert d.aad == hx("00000000 0102") and d.tag == tag and d.ciphertext == hx("bbcc")


def test_udp_v3_ordinary_datagram_has_a_4_byte_aad_and_tag_before_ciphertext():
    tag = hx("a0a1a2a3a4a5a6a7")
    d = svx.split_udp_v3(hx("00000007") + tag + hx("bbccdd"))
    assert d.counter == 7 and d.client_id is None
    assert d.aad == hx("00000007") and d.tag == tag and d.ciphertext == hx("bbccdd")
    with pytest.raises(svx.SvxError):
        svx.split_udp_v3(hx("00000007 a0a1a2a3a4a5a6"))   # tag cut short
    with pytest.raises(svx.SvxError):
        svx.split_udp_v3(hx("000007"))


def test_v3_iv_and_plaintext_layout():
    rand = hx("010203040506")
    assert svx.v3_iv(rand, 0x0102, 10) == hx("010203040506 0102 0000000a")
    assert svx.v3_iv(rand, 0, 0) == hx("010203040506 0000 00000000")
    with pytest.raises(svx.SvxError):
        svx.v3_iv(b"\x00" * 5, 1, 1)
    assert svx.v3_plaintext(svx.UDP_HEARTBEAT) == hx("0001")
    assert svx.v3_plaintext(svx.UDP_AUDIO, svx.udp_audio_body(b"\x01")) == hx(
        "0065 0001 01")
