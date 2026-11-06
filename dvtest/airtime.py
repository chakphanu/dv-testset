# SPDX-License-Identifier: MIT
"""LoRa time on air and the airtime anchor table (HWFREE1 item 3, docs/direwolf-cases.md).

A reference for the DV-Router side (the LoRa airtime formula of its txsched
crate), written from the Semtech application note AN1200.13 / SX1276 datasheet
section 4.1.1, items 6 and 7 and nothing else. Exact arithmetic (fractions, no floats), so
every number in `vectors/airtime/anchors.json` can be regenerated bit for bit:

    Tsym      = 2^SF / BW
    Tpreamble = (n_preamble + 4.25) * Tsym
    n_payload = 8 + max(ceil((8*PL - 4*SF + 28 + 16*CRC - 20*IH) / (4*(SF - 2*DE))) * (CR + 4), 0)
    ToA       = Tpreamble + n_payload * Tsym

with PL the payload bytes, CRC 1 when the payload CRC is on, IH 1 for an
implicit header, DE 1 when low data rate optimisation is on, CR 1..4 for coding
rate 4/5..4/8. By default DE is 1 exactly when Tsym is over 16 ms (the rule the
datasheet gives and the rule DV-Router states).

Two independent paths are kept so the table is not one formula checked against
itself: `time_on_air` (fractions, as above) and `time_on_air_us_style`, which
counts symbols in quarters and truncates the symbol time to whole microseconds
(the way an integer-only library does it; exact for 125, 250 and 500 kHz).

Anchor kinds in ANCHORS:
  external   a number published by somebody else (a calculator quoted in a forum
             thread); copied from the research note HWFREE1 part A, section 3.2.
             The source page was NOT re-read in this stage: UNVERIFIED as a quote.
  project    a value from the DV-Router test file airtime_tests.rs, written by the
             same agent family as the code it tests: reproduced here, not independent.
  derived    this module's own output for a shape the project cares about
             (LoRa APRS lines); a regression table, not an independent value.
"""
from fractions import Fraction
from math import ceil

SYMBOL_LDRO_LIMIT = Fraction(16, 1000)       # seconds; DE = 1 above this symbol time


def symbol_time(sf, bw_hz):
    """Seconds as a Fraction."""
    return Fraction(2 ** sf, bw_hz)


def low_data_rate(sf, bw_hz):
    """DE the datasheet way: on when the symbol time is over 16 ms."""
    return 1 if symbol_time(sf, bw_hz) > SYMBOL_LDRO_LIMIT else 0


def payload_symbols(payload_bytes, sf, *, cr=1, crc=True, implicit_header=False, de=0):
    """Number of payload symbols (header and CRC included), an int."""
    if not 6 <= sf <= 12:
        raise ValueError("SF must be 6..12")
    if not 1 <= cr <= 4:
        raise ValueError("cr is 1..4 for 4/5..4/8")
    if sf - 2 * de <= 0:
        raise ValueError("SF too small for low data rate optimisation")
    num = 8 * payload_bytes - 4 * sf + 28 + (16 if crc else 0) - (20 if implicit_header else 0)
    den = 4 * (sf - 2 * de)
    return 8 + max(-(-num // den) * (cr + 4), 0)       # -(-a // b) is ceil(a / b) for ints


def time_on_air(payload_bytes, sf, bw_hz, *, cr=1, preamble=8, crc=True, implicit_header=False, de=None):
    """Time on air in seconds as an exact Fraction. de=None picks the datasheet rule."""
    if de is None:
        de = low_data_rate(sf, bw_hz)
    tsym = symbol_time(sf, bw_hz)
    n = payload_symbols(payload_bytes, sf, cr=cr, crc=crc, implicit_header=implicit_header, de=de)
    return (Fraction(preamble) + Fraction(17, 4)) * tsym + n * tsym


def time_on_air_us_style(payload_bytes, sf, bw_hz, *, cr=1, preamble=8, crc=True, implicit_header=False, de=None):
    """Integer microseconds, the integer-library way: symbol time truncated to whole
    microseconds, symbols counted times four (preamble + 4.25 -> 4*preamble + 17),
    result = symbol_us * symbols_x4 // 4. Differs from time_on_air by less than
    (symbols * 1 us) and is exact when BW divides 2^SF * 10^6."""
    if de is None:
        de = low_data_rate(sf, bw_hz)
    sym_us = (1_000_000 << sf) // bw_hz
    n = payload_symbols(payload_bytes, sf, cr=cr, crc=crc, implicit_header=implicit_header, de=de)
    x4 = 4 * preamble + 17 + 4 * n
    return sym_us * x4 // 4


def ms(value):
    """A Fraction of seconds as a decimal string in milliseconds with up to 6 places,
    trailing zeros kept to 3 places (exact for these inputs, see exact_ms)."""
    v = value * 1000
    text = ("%.6f" % float(v)).rstrip("0")
    whole, _, frac = text.partition(".")
    return "%s.%s" % (whole, frac.ljust(3, "0"))


def exact_ms(value):
    """Milliseconds as a 'num/den' string when the Fraction is not a terminating
    decimal of at most 6 places, else the decimal text of ms()."""
    v = value * 1000
    if (v * 10 ** 6).denominator == 1:
        return ms(value)
    return "%d/%d" % (v.numerator, v.denominator)


# ------------------------------------------------------------------ anchors

# (id, kind, source, payload, sf, bw_hz, cr, expected_ms or None, published)
# `published` is the number as the source printed it (a string) or None.
_ANCHOR_ROWS = [
    ("ext-pl10-sf7", "external", "third-party calculator, found in search results (research note 3.2)", 10, 7, 125000, 1, "41.216", "41.216"),
    ("ext-pl10-sf11", "external", "same third-party calculator (research note 3.2)", 10, 11, 125000, 1, "577.536", "577.536"),
    ("ext-pl64-sf12", "external", "TTN forum thread 'fair use policy explained', avbentem calculator, 64 bytes (research note 3.2)", 64, 12, 125000, 1, "2793.472", "2793.5"),
    ("ext-pl68-sf12", "external", "same TTN forum thread, 55 + 13 bytes (research note 3.2)", 68, 12, 125000, 1, "2957.312", "2957.31"),
    ("proj-pl80-sf12", "project", "DV-Router airtime_tests.rs lines 55-56 (research note 3.2)", 80, 12, 125000, 1, "3284.992", None),
    ("proj-pl20-sf7", "project", "DV-Router airtime_tests.rs (20, base) 56_576_000 ns", 20, 7, 125000, 1, "56.576", None),
    ("proj-pl1-sf7", "project", "DV-Router airtime_tests.rs (1, base) 25_856_000 ns", 1, 7, 125000, 1, "25.856", None),
]

# LoRa APRS shaped rows: PL = 3 prefix bytes + TNC2 line length (research note 3.2)
LORA_APRS_LINES = [
    "N0CALL-7>APLRT1:>Test status",
    "N0CALL-7>APLRT1,WIDE1-1:!0000.00N/00000.00E[Test",
    "N0CALL-7>APLRG1,WIDE1-1:=0000.00N/00000.00E-LoRa iGate test",
]
LORA_APRS_PREFIX = 3
LORA_APRS_SETTINGS = [("SF12", 12), ("SF9", 9), ("SF7", 7)]


def anchor_rows():
    """The anchor table as a list of dicts, every expected value recomputed."""
    rows = []
    for ident, kind, source, pl, sf, bw, cr, expected, published in _ANCHOR_ROWS:
        toa = time_on_air(pl, sf, bw, cr=cr)
        rows.append({
            "id": ident, "kind": kind, "source": source, "payload_bytes": pl, "sf": sf, "bw_hz": bw,
            "cr": "4/%d" % (cr + 4), "preamble": 8, "crc": True, "explicit_header": True,
            "de": low_data_rate(sf, bw), "expected_ms": expected, "published": published,
            "computed_ms": exact_ms(toa), "us_style_us": time_on_air_us_style(pl, sf, bw, cr=cr),
        })
    for i, line in enumerate(LORA_APRS_LINES):
        pl = LORA_APRS_PREFIX + len(line)
        for label, sf in LORA_APRS_SETTINGS:
            toa = time_on_air(pl, sf, 125000)
            rows.append({
                "id": "aprs-%d-%s" % (i + 1, label.lower()), "kind": "derived",
                "source": "LoRa APRS shaped line, PL = 3 + len(%r)" % line, "payload_bytes": pl, "sf": sf,
                "bw_hz": 125000, "cr": "4/5", "preamble": 8, "crc": True, "explicit_header": True,
                "de": low_data_rate(sf, 125000), "expected_ms": exact_ms(toa), "published": None,
                "computed_ms": exact_ms(toa), "us_style_us": time_on_air_us_style(pl, sf, 125000),
            })
    for cr in range(1, 5):
        toa = time_on_air(50, 12, 125000, cr=cr)
        rows.append({"id": "sweep-cr-4-%d" % (cr + 4), "kind": "derived", "source": "coding rate sweep, PL 50, SF12",
                     "payload_bytes": 50, "sf": 12, "bw_hz": 125000, "cr": "4/%d" % (cr + 4), "preamble": 8,
                     "crc": True, "explicit_header": True, "de": 1, "expected_ms": exact_ms(toa), "published": None,
                     "computed_ms": exact_ms(toa), "us_style_us": time_on_air_us_style(50, 12, 125000, cr=cr)})
    for bw in (250000, 500000):
        toa = time_on_air(30, 8, bw)
        rows.append({"id": "sweep-bw-%d" % (bw // 1000), "kind": "derived", "source": "bandwidth sweep, PL 30, SF8",
                     "payload_bytes": 30, "sf": 8, "bw_hz": bw, "cr": "4/5", "preamble": 8, "crc": True,
                     "explicit_header": True, "de": low_data_rate(8, bw), "expected_ms": exact_ms(toa),
                     "published": None, "computed_ms": exact_ms(toa),
                     "us_style_us": time_on_air_us_style(30, 8, bw)})
    toa = time_on_air(20, 12, 125000, de=0)
    rows.append({"id": "sweep-de-off-sf12", "kind": "derived",
                 "source": "SF12 BW125 with low data rate optimisation forced OFF (illegal at 125 kHz), PL 20",
                 "payload_bytes": 20, "sf": 12, "bw_hz": 125000, "cr": "4/5", "preamble": 8, "crc": True,
                 "explicit_header": True, "de": 0, "expected_ms": exact_ms(toa), "published": None,
                 "computed_ms": exact_ms(toa), "us_style_us": time_on_air_us_style(20, 12, 125000, de=0)})
    return rows


# ------------------------------------------------------------------ AFSK upper bound (DV-Router)

# KISS parameters DV-Router sends on every (re)connect, as stated by the DV-Router lead
# (docs/direwolf-cases.md, "Documented differences"): TXDELAY 30 and TXtail 3 in units of 10 ms.
DVR_TXDELAY_MS = 300
DVR_TXTAIL_MS = 30
STUFFING_NUM, STUFFING_DEN = 6, 5           # the 6/5 allowance for HDLC bit stuffing


def afsk_upper_bound_bits(frame_len):
    """DV-Router's deliberate upper bound for the bits of one AX.25 frame on the air:
    (len + 4) * 8 * 6 / 5 as an exact Fraction. len + 4 is the frame plus two FCS bytes plus
    two flag octets; 6/5 allows one stuffed zero per five bits, the worst case."""
    if frame_len < 0:
        raise ValueError("frame_len must not be negative")
    return Fraction((frame_len + 4) * 8 * STUFFING_NUM, STUFFING_DEN)


def afsk_upper_bound_ms(frame_len, bps=1200, txdelay_ms=DVR_TXDELAY_MS, txtail_ms=DVR_TXTAIL_MS, granule_ms=1):
    """The upper bound as an int number of milliseconds: ceil(bits / bps) rounded up to a
    multiple of `granule_ms` (the granularity of DV-Router's ceil is an ASSUMPTION: one
    millisecond), plus TXDELAY and TXtail. The exact stuffed bit count of
    dvtest.ax25.air_bits(frame) is a LOWER bound of the first term."""
    if bps <= 0 or granule_ms <= 0:
        raise ValueError("bps and granule_ms must be positive")
    ms_exact = afsk_upper_bound_bits(frame_len) * 1000 / bps
    granules = -(-ms_exact // granule_ms)                  # ceil of a Fraction
    return int(granules) * granule_ms + txdelay_ms + txtail_ms


def afsk_exact_frame_ms(frame, bps=1200):
    """Exact time of one frame (opening flag, stuffed frame + FCS, closing flag) at `bps` as a
    Fraction of milliseconds: the lower bound that afsk_upper_bound_ms must not undercut."""
    from . import ax25
    return Fraction(ax25.air_bits(frame) * 1000, bps)


def main(argv=None):
    """python3 -m dvtest.airtime [--out vectors/airtime/anchors.json]: write the
    anchor table (regenerated, no container)."""
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(prog="airtime", description=main.__doc__.splitlines()[0])
    parser.add_argument("--out", default="vectors/airtime/anchors.json")
    args = parser.parse_args(argv)
    doc = {"schema": "dvt-airtime-anchors/1",
           "formula": "Semtech AN1200.13 / SX1276 datasheet 4.1.1 (items 6 and 7), exact fractions (dvtest/airtime.py)",
           "kinds": {"external": "number published by a third party, copied from the research note, source page not re-read",
                     "project": "DV-Router airtime_tests.rs value, same author family as the code: reproduced, not independent",
                     "derived": "this module's output for a shape the project cares about: a regression table"},
           "rows": anchor_rows()}
    with open(args.out, "w", encoding="ascii") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("wrote %d rows to %s" % (len(doc["rows"]), args.out))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
