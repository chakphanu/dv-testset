# SPDX-License-Identifier: MIT
"""The public APRS-IS passcode of a callsign (the "pass" value of an APRS-IS login).

The algorithm is the long public one used by every APRS-IS client and server: the upper-cased base callsign (the text
before the first "-", at most 9 characters), a 16 bit hash seeded with 0x73e2 that XORs the characters in pairs (first
character shifted left 8 bits, second as is), masked to 15 bits. Written from that public description; no code of any
server or client was copied.

The private aprsc of the rig (rig/Dockerfile.aprsc) verifies every login against this value, so a fixture login only
needs its own callsign: the value is a function of a synthetic callsign, not a credential. A wrong value logs in
"unverified" and the server drops the packets of such a client.

  python3 -m dvtest.aprs_passcode N0TEST-5      prints the passcode of a callsign
  python3 -m dvtest.aprs_passcode --dvr         prints the passcode of DVR_LOGIN (run.sh writes it to
                                                out/<run>/aprs-secrets/passcode, mode 600, for --profile aprs)
"""
import sys

SEED = 0x73E2
MAX_BASE = 9
# the login of the DV-Router APRS-IS edge of the rig (rig/config/dv-router/dp15-aprsis.toml: callsign) and of the tester client
DVR_LOGIN = "N0TEST-5"
TESTER_LOGIN = "N0TEST-7"


def base_call(call):
    """The upper-cased callsign without its SSID, cut to MAX_BASE characters."""
    return call.split("-", 1)[0].upper()[:MAX_BASE]


def aprs_passcode(call):
    """The passcode (0..32767) of `call`; the SSID does not count."""
    base = base_call(call)
    value = SEED
    for index in range(0, len(base), 2):
        value ^= ord(base[index]) << 8
        if index + 1 < len(base):
            value ^= ord(base[index + 1])
    return value & 0x7FFF


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--dvr"]:
        args = [DVR_LOGIN]
    if len(args) != 1 or not args[0].strip():
        print("usage: python3 -m dvtest.aprs_passcode CALLSIGN[-SSID] | --dvr", file=sys.stderr)
        return 2
    print(aprs_passcode(args[0].strip()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
