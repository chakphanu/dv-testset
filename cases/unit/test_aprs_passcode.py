# SPDX-License-Identifier: MIT
"""The APRS-IS passcode helper (dvtest/aprs_passcode.py) that the private aprsc of the rig verifies logins against.
Offline: no sockets, no docker."""
import pytest

from dvtest import aprs_passcode as ap

pytestmark = pytest.mark.unit


def reference(call):
    """An independent, differently written version of the public algorithm (pairs of bytes as 16 bit words)."""
    base = call.split("-")[0].upper()[:9].encode("ascii")
    if len(base) % 2:
        base += b"\0"
    value = 0x73E2
    for i in range(0, len(base), 2):
        value ^= int.from_bytes(base[i:i + 2], "big")
    return value & 0x7FFF


def test_public_vector_n0call():
    # the well known passcode of the placeholder callsign N0CALL (published in the APRS-IS documentation and many tutorials)
    assert ap.aprs_passcode("N0CALL") == 13023


def test_vectors_the_private_aprsc_accepted():
    # smoke test of the pinned aprsc 3.0.0 (rig/Dockerfile.aprsc): `user N0TEST-5 pass 15043` and `user N0TEST-7 pass 15043`
    # answered `# logresp ... verified`, `user N0TEST-9 pass 1` answered unverified
    assert ap.aprs_passcode("N0TEST-5") == 15043
    assert ap.aprs_passcode("N0TEST-7") == 15043
    assert ap.aprs_passcode("N0TEST-9") != 1


def test_ssid_and_case_do_not_count():
    assert ap.aprs_passcode("n0call-9") == ap.aprs_passcode("N0CALL") == ap.aprs_passcode("N0CALL-15")
    assert ap.aprs_passcode("N0TEST-5") == ap.aprs_passcode("N0TEST-7") == ap.aprs_passcode("N0TEST")


def test_odd_and_even_lengths_and_truncation():
    assert ap.aprs_passcode("A") == (0x73E2 ^ (ord("A") << 8)) & 0x7FFF
    assert ap.aprs_passcode("AB") == (0x73E2 ^ (ord("A") << 8) ^ ord("B")) & 0x7FFF
    # only the first 9 characters of the base count
    assert ap.aprs_passcode("ABCDEFGHI") == ap.aprs_passcode("ABCDEFGHIJKL")
    assert ap.base_call("abcdefghijkl-3") == "ABCDEFGHI"


@pytest.mark.parametrize("call", ["N0CALL", "N0TEST-5", "XLX999B", "XLX999", "A", "AB", "ABC", "W1AW-7", "ABCDEFGHI", "N0TEST-12"])
def test_matches_an_independent_version(call):
    assert ap.aprs_passcode(call) == reference(call)
    assert 0 <= ap.aprs_passcode(call) <= 0x7FFF


def test_fixture_logins_are_distinct_synthetic_and_valid_station_names():
    assert ap.DVR_LOGIN != ap.TESTER_LOGIN                # two verified logins with one name kick each other
    for login in (ap.DVR_LOGIN, ap.TESTER_LOGIN):
        base = ap.base_call(login)
        assert base == "N0TEST" and len(base) <= 6        # DV-Router's valid_station: <= 6 characters, a digit, an upper-case letter
        assert not base.startswith(("N0CALL", "NOCALL", "SERVER"))      # aprsc drops sources with these prefixes


def test_main(capsys):
    assert ap.main(["N0CALL"]) == 0
    assert capsys.readouterr().out.strip() == "13023"
    assert ap.main(["--dvr"]) == 0
    assert capsys.readouterr().out.strip() == str(ap.aprs_passcode(ap.DVR_LOGIN))
    assert ap.main([]) == 2
    assert ap.main(["A", "B"]) == 2
