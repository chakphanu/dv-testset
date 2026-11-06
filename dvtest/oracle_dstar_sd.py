# SPDX-License-Identifier: MIT
"""Two independent D-STAR slow data decoders as test oracles (HWFREE1 item 4,
docs/dstar-slowdata-cases.md).

Host side of rig/Dockerfile.dstarsd. MMDVMHost's CDStarSlowData and DStarGateway's text, D-PRS
and NMEA collectors (GPL-2.0-or-later) are linked unmodified into two small drivers
(rig/dstarsd/oracle-*.cpp) and run as processes in one throw-away image (--network none);
nothing of their code or text is in this repository, only the output formats and the facts we
read from them.

A corpus of overs is built from the specification (dvtest.dstar_sd): every case is a list of
(seq, wire data field) frames. The frames go to both drivers on stdin ("B name", "F seq hex6",
"E"); each driver prints what its decoder decided. Our own decoder (dvtest.dstar_sd.Decoder, both
sync policies) is compared with them, event by event, and the result goes to
vectors/dstar_sd/corpus.json.

The pure functions (corpus, input and output text, views, comparison) are unit tested without
Docker (cases/unit/test_oracle_dstar_sd.py); run_driver() needs the image.

    python3 -m dvtest.oracle_dstar_sd run --out vectors/dstar_sd/corpus.json
"""
import argparse
import json
import subprocess
import sys

from . import dstar_sd as sd
from . import limits
from .proto import dextra

IMAGE = "dvt-dstarsd:local"
RUN_TIMEOUT = 120
SCHEMA = "dvt-dstar-sd-corpus/2"

CALL = "N0CALL"
TEXT = "N0CALL: CQ DE N0CALL"             # 20 characters
TEXT_B = "N0TEST: HELLO WORLD."            # 20 characters
# D-PRS payload of the same length (81 characters + CR = 17 blocks) as the real-radio sample of
# [SLOW] (the sample itself holds a personal callsign and position and is not committed)
DPRS_PAYLOAD = "N0CALL>API282,DSTAR*:/211234h0000.00N/00000.00E-027/000/N0CALL at home"
# GGA / RMC / free-form line have the lengths of the real-radio samples of [SLOW] (71, 74, 31 bytes)
GGA = sd.nmea_sentence("GPGGA,211234.00,0000.00000,N,00000.0000,E,1,05,1.0,12.0,M,0.0,M,,")
RMC = sd.nmea_sentence("GPRMC,211234.00,A,0000.00000,N,00000.000000,E,000.0,000.0,061126,,,A")
GLL = sd.nmea_sentence("GPGLL,0000.00,N,00000.00,E,211234.00,A,A")
VTG = sd.nmea_sentence("GPVTG,27.0,T,,M,0.0,N,0.0,K,A")
GSA = sd.nmea_sentence("GPGSA,A,3,04,05,09,12,,,,,,,,,1.8,1.0,1.5")
GSV = sd.nmea_sentence("GPGSV,1,1,04,04,40,083,40,05,30,120,35,09,20,250,30,12,60,300,38")
FREEFORM = b"N0CALL  ,TEST MESSAGE        \r\n"
RADIO_HEADER = dextra.radio_header(my=CALL, rpt1="N0CALL B", rpt2="XLX999 B", ur="CQCQCQ")


def _sf(frames, drop=()):
    return [f for i, f in enumerate(frames) if i not in drop]


def _dprs_of_length(total):
    """A D-PRS sentence of `total` bytes from '$$CRC' to the CR (valid CRC): the comment is padded."""
    pad = total - len(sd.dprs_line(DPRS_PAYLOAD))
    return sd.dprs_line(DPRS_PAYLOAD + "x" * pad)


def _nmea_of_length(total):
    """A GGA sentence of `total` bytes including CR LF: the last field is padded."""
    base = len(sd.nmea_sentence("GPGGA,211234.00,0000.00,N,00000.00,E,1,05,1.0,12.0,M,0.0,M,,"))
    return sd.nmea_sentence("GPGGA,211234.00,0000.00,N,00000.00,E,1,05,1.0,12.0,M,0.0,M,," + "0" * (total - base))


def _with_mini(blocks, index, mini):
    """The block list with the mini-header byte of one block replaced (payload untouched)."""
    out = list(blocks)
    out[index] = bytes([mini]) + out[index][1:]
    return out


def _sized_blocks(data, sizes):
    """Simple data blocks whose valid byte counts follow `sizes` (cycled); a block with fewer than 5
    valid bytes carries the filler after them and says so in its length nibble (legal, [STD6] 6.1.3)."""
    out = []
    pos = 0
    i = 0
    while pos < len(data):
        n = min(sizes[i % len(sizes)], len(data) - pos)
        out.append(sd.block(sd.T_DATA | n, data[pos:pos + n]))
        pos += n
        i += 1
    return out


def _printable_header():
    """A radio header whose 41 bytes are ALL printable ASCII (flags 0x20 0x20 0x20, which no radio sends, and
    a suffix chosen so that both FCS bytes are printable too, the first suffix of the search order that
    works): the only kind of header a decoder that maps every byte outside 32..127 to '.' before the
    FCS check (DSDcc's slow data path) can accept."""
    import itertools
    for chars in itertools.product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=2):
        hdr = dextra.radio_header(my=CALL, rpt1="N0CALL B", rpt2="XLX999 B", ur="CQCQCQ", suffix="".join(chars),
                                  flags=b"\x20\x20\x20")
        if all(32 <= b <= 126 for b in hdr):
            return hdr
    raise ValueError("no printable header found")


def _render_blocks(blocks, first_slot=0, superframes=None):
    return sd.render(sd.superframes_to_halves(sd.place(blocks, first_slot, superframes)))


UNIT66_DOC = ("unit66_hex: one string per superframe unit (a new unit starts when the frame number does not increase): "
              "3 byte presence bitmap + 21 x 3 raw slow data bytes as received (frame 0 included, others scrambled; "
              "an absent frame is 3 zero bytes). Bitmap bit order is the generator's ASSUMPTION: 24 bit big-endian, "
              "MSB first over frames 0..20, frame f = bit (23 - f), the 3 spare low bits zero.")


def corpus():
    """[(name, note, frames)] where frames = [(seq, wire3)]. Names are stable: they are the keys of the
    committed vectors."""
    tb = sd.text_blocks(TEXT)
    tb_b = sd.text_blocks(TEXT_B)
    hb = sd.header_blocks(RADIO_HEADER)
    bad_header = bytearray(RADIO_HEADER)
    bad_header[12] ^= 0x01                  # one bit of RPT1, FCS now wrong
    hb_bad = sd.header_blocks(bytes(bad_header))
    dprs = sd.dprs_line(DPRS_PAYLOAD)
    dprs_bad = bytearray(dprs)
    dprs_bad[20] ^= 0x01                    # one bit inside the string: CRC wrong
    gga_b = sd.simple_data_blocks(GGA.encode())
    rmc_b = sd.simple_data_blocks(RMC.encode())
    gga_bad = sd.simple_data_blocks(GGA.encode().replace(b"1.0,12.0", b"1.0,13.0"))
    free_b = sd.simple_data_blocks(FREEFORM)
    dprs_b = sd.simple_data_blocks(dprs)
    F = None
    cases = []

    def add(name, note, frames):
        cases.append((name, note, frames))

    # ---- message (text)
    add("text-aligned", "4 text blocks in slots 0..3, filler after: the layout of [STD6] 6.3 (message from frame 2)", _render_blocks(tb))
    add("text-dvrouter-twice", "the 4 blocks twice, then filler: what DV-Router renders (dvtest.slowdata.encode_text_over)", _render_blocks(tb + tb))
    add("text-slot1", "message from data frame 4 ([STD6] 6.3: 'can start from an even position')", _render_blocks(tb, 1))
    add("text-slot3", "message from data frame 8", _render_blocks(tb, 3))
    add("text-slot6", "last position that still fits one superframe (slots 6..9)", _render_blocks(tb, 6))
    add("text-slot8-split", "blocks 0,1 in slots 8,9; blocks 2,3 in slots 0,1 of the next superframe (cross-superframe)", _render_blocks(tb, 8))
    add("text-slot9-split", "block 0 in slot 9; blocks 1..3 in the next superframe", _render_blocks(tb, 9))
    add("text-straddle", "blocks start in an EVEN frame: every block straddles the sync of its superframe (out of spec)",
        sd.render(sd.halves(tb), start=2))
    add("text-missing-block-late", "sf1: blocks 0,1,3; sf2: block 2 (a late segment completes the message)",
        sd.render(sd.superframes_to_halves([[tb[0], tb[1], F, tb[3]] + [F] * 6, [tb[2]] + [F] * 9])))
    add("text-missing-block-never", "sf1: blocks 0,1,3; sf2: filler (message never completes)",
        sd.render(sd.superframes_to_halves([[tb[0], tb[1], F, tb[3]] + [F] * 6, [F] * 10])))
    add("text-repeat-per-superframe", "the 4 blocks in each of 2 superframes (a radio repeats the message)",
        sd.render(sd.superframes_to_halves([tb + [F] * 6, tb + [F] * 6])))
    add("text-two-messages-split", "A complete in sf1; B blocks 0,1 in sf2 and 2,3 in sf3",
        sd.render(sd.superframes_to_halves([tb + [F] * 6, tb_b[:2] + [F] * 8, tb_b[2:] + [F] * 8])))
    add("text-stale-mix", "sf1: A0 A1 A2; sf2: B3: no message id in the blocks, so old slots mix with new ones",
        sd.render(sd.superframes_to_halves([tb[:3] + [F] * 7, tb_b[3:] + [F] * 9])))
    hi = [blk[:1] + bytes(c | 0x80 for c in blk[1:]) for blk in tb]
    add("text-high-bit", "text bytes with bit 7 set (a bit error): both decoders strip it", _render_blocks(hi))
    add("text-lost-frame", "frame 4 of sf1 never arrives (text in sf1 and sf2): the pair phase is wrong until the next sync",
        _sf(sd.render(sd.superframes_to_halves([tb + [F] * 6, tb + [F] * 6])), drop=(4,)))
    add("text-sync-missing", "text in sf2; the sync frame of sf2 (frame 21) never arrives",
        _sf(sd.render(sd.superframes_to_halves([[F] * 10, tb + [F] * 6])), drop=(21,)))

    # ---- radio header resend
    add("header-aligned", "9 header blocks + filler in one superframe ([STD6] 6.4)", _render_blocks(hb))
    add("header-after-squelch", "code squelch, then the 9 header blocks ([STD6] 6.4: 'or after the code squelch')",
        _render_blocks([sd.squelch_block(19)] + hb))
    add("header-split", "header blocks 0..4 in slots 5..9, blocks 5..8 in the next superframe", _render_blocks(hb, 5))
    add("header-twice", "the full header in each of 2 superframes", sd.render(sd.superframes_to_halves([hb + [F], hb + [F]])))
    add("header-printable-bytes", "9 header blocks of a header whose 41 bytes are all printable ASCII (flags 20 20 20, printable FCS)",
        _render_blocks(sd.header_blocks(_printable_header())))
    add("header-bad-fcs", "9 header blocks of a header whose FCS does not match", _render_blocks(hb_bad))

    # ---- GPS: D-PRS ($$CRC) and NMEA
    add("dprs-aligned", "D-PRS line, 17 blocks over 2 superframes, from slot 0", _render_blocks(dprs_b))
    add("dprs-slot3", "the same from slot 3 (spills over the sync)", _render_blocks(dprs_b, 3))
    add("dprs-bad-crc", "one bit of the string flipped after the CRC was computed", _render_blocks(sd.simple_data_blocks(bytes(dprs_bad))))
    add("dprs-text-interleave", "Text + APRS pattern T A T A T A T A A A (from [SLOW])",
        sd.render(sd.superframes_to_halves(sd.interleave(sd.LAYOUTS["text-aprs"], {"T": ("cycle", tb), "A": ("once", dprs_b)}))))
    add("dprs-crc-without-cr", "the CRC computed over the string WITHOUT the final CR (the oracle must refuse it)",
        _render_blocks(sd.simple_data_blocks(sd.dprs_line(DPRS_PAYLOAD, crc_covers_cr=False))))
    add("dprs-two-lines", "two D-PRS lines one after the other (collector state after a valid line)",
        _render_blocks(dprs_b + sd.simple_data_blocks(sd.dprs_line(DPRS_PAYLOAD.replace("home", "work")))))
    add("dprs-then-text", "D-PRS line (17 blocks, 2 superframes) then a message in the third superframe (DSDcc evaluates a GPS line only when a text or header block starts a superframe)",
        sd.render(sd.superframes_to_halves(sd.place(dprs_b, 0, 2) + [tb + [F] * 6])))
    add("dprs-len-299", "D-PRS sentence of exactly 299 bytes ('$$CRC' to the CR), valid CRC",
        _render_blocks(sd.simple_data_blocks(_dprs_of_length(299))))
    add("dprs-len-300", "D-PRS sentence of exactly 300 bytes ('$$CRC' to the CR), valid CRC",
        _render_blocks(sd.simple_data_blocks(_dprs_of_length(300))))
    add("dprs-len-301", "the same with 301 bytes (one over the DV-Router GPS-A buffer)",
        _render_blocks(sd.simple_data_blocks(_dprs_of_length(301))))
    add("nmea-gga", "$GPGGA with its checksum", _render_blocks(gga_b))
    add("nmea-rmc", "$GPRMC with its checksum", _render_blocks(rmc_b))
    add("nmea-bad-checksum", "$GPGGA with one field changed after the checksum was computed", _render_blocks(gga_bad))
    add("nmea-gga-rmc-free", "GGA, RMC and the free-form line one after the other ([SLOW]: three sets)",
        _render_blocks(gga_b + rmc_b + free_b))
    add("nmea-six-types", "GGA, GLL, VTG, RMC, GSA, GSV one after the other (the six DStarGateway collects)",
        _render_blocks(sum((sd.simple_data_blocks(x.encode()) for x in (GGA, GLL, VTG, RMC, GSA, GSV)), [])))
    add("nmea-len-82", "NMEA sentence of 82 bytes including CR LF (the NMEA 0183 maximum), valid checksum",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(82).encode())))
    add("nmea-len-83", "the same with 83 bytes", _render_blocks(sd.simple_data_blocks(_nmea_of_length(83).encode())))
    add("nmea-len-80", "NMEA sentence of 80 bytes including CR LF (line body 78), valid checksum: inside every reading of the DV-Router limit",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(80).encode())))
    add("nmea-len-81", "the same with 81 bytes including CR LF (line body 79)", _render_blocks(sd.simple_data_blocks(_nmea_of_length(81).encode())))
    add("nmea-len-84", "the same with 84 bytes including CR LF (line body 82, one over the 83 of reading A)",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(84).encode())))
    add("nmea-len-85", "the same with 85 bytes including CR LF (line body 83, over the limit in every reading)",
        _render_blocks(sd.simple_data_blocks(_nmea_of_length(85).encode())))
    add("nmea-text-interleave", "Text + GPS pattern T G T G T G T G G G (from [SLOW]) carrying the GGA",
        sd.render(sd.superframes_to_halves(sd.interleave(sd.LAYOUTS["text-gps"], {"T": ("cycle", tb), "G": ("once", gga_b)}))))

    # ---- length nibble of the 0x3x blocks (named difference DIFF-NIBBLE: DV-Router reads it,
    # DStarGateway does not), block 3 of the GGA sentence is a full block of 5 characters
    add("nmea-nibble-0x36", "GGA with the length nibble of block 3 set to 6 (0x36, full block): DV-Router drops 0x36..0x3F",
        _render_blocks(_with_mini(gga_b, 3, 0x36)))
    add("nmea-nibble-0x3f", "GGA with the length nibble of block 3 set to 15 (0x3F, full block)",
        _render_blocks(_with_mini(gga_b, 3, 0x3F)))
    add("nmea-nibble-0x30", "GGA with the length nibble of block 3 set to 0 (0x30, full block): DV-Router reads n = 0 as 5",
        _render_blocks(_with_mini(gga_b, 3, 0x30)))
    # the two characters a low nibble cuts off must differ: the NMEA checksum is an XOR, equal pairs cancel
    low = next(i for i in range(2, len(gga_b) - 1) if gga_b[i][4] != gga_b[i][5])
    add("nmea-nibble-low", "GGA with block %d marked 0x33 although it holds 5 characters (nibble disagrees with the content): 3 are used" % low,
        _render_blocks(_with_mini(gga_b, low, 0x33)))
    add("nmea-nibble-short-block", "GGA in blocks of 5,5,5,3,5,... (a short block of 3 characters + 2 filler in the middle, legal): the nibble is needed",
        _render_blocks(_sized_blocks(GGA.encode(), [5, 5, 5, 3, 5])))

    # ---- fixed frame pairs of the DV-Router unit: a frame of a pair missing (holes in the presence bitmap)
    add("pair-hole-text-once", "message in sf1 with frame 6 (second of pair 5,6) never received, sf2 filler: the block is skipped, no text",
        _sf(sd.render(sd.superframes_to_halves([tb + [F] * 6, [F] * 10])), drop=(6,)))
    add("pair-hole-text-twice", "the 4 blocks twice in sf1, frame 4 missing (pair 3,4, block 1 of the first copy): the second copy completes it",
        _sf(sd.render(sd.superframes_to_halves([tb + tb + [F] * 2, [F] * 10])), drop=(4,)))
    add("pair-hole-nmea-then-ok", "GGA then RMC (30 blocks), frame 6 missing in sf1: the GGA line is lost, the RMC after it still decodes",
        _sf(_render_blocks(gga_b + rmc_b), drop=(6,)))

    # ---- squelch, fast data, filler
    add("squelch-only", "code squelch 19 in slot 0", _render_blocks([sd.squelch_block(19)]))
    add("squelch-mismatch", "0xC2 with two different code bytes", _render_blocks([sd.block(0xC2, b"\x19\x18")]))
    payload = bytes((7 * i + 1) & 0xFF for i in range(208))
    fast = sd.fast_data_superframe(payload)
    add("fast-data-superframe", "28 + 9 x 20 bytes: only the data fields (mini-headers 0x9C, then 0x94 x 9)",
        [(seq, data) for seq, _v, data in fast])
    add("fast-length-max", "mini-header 0x9C (28 bytes), the largest valid length", _render_blocks([sd.block(0x9C, b"abcde")]))
    add("fast-length-invalid", "mini-header 0x9D: above the 28 bytes a block can hold", _render_blocks([sd.block(0x9D, b"abcde")]))
    add("filler-only", "20 filler data fields (66 66 66 on the wire 16 29 F5)", sd.render([], start=1, frames=21))
    return cases


# ------------------------------------------------------------------ driver text

def driver_input(cases):
    """The stdin text for both drivers: one B / F... / E group per case."""
    lines = []
    for name, _note, frames in cases:
        lines.append("B %s" % name)
        for seq, wire in frames:
            lines.append("F %d %s" % (seq, wire.hex()))
        lines.append("E")
    return "\n".join(lines) + "\n"


def parse_driver_output(text):
    """{case: [event]} from a driver's stdout. Event: {"kind", "seq", "hex"}; lines that do not start
    with 'D' or 'R ' (log text) are ignored. Events before the first 'D' line are dropped."""
    out = {}
    cur = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "D":
            if parts[1] != "end":
                cur = parts[1]
                out[cur] = []
            continue
        if len(parts) == 4 and parts[0] == "R" and cur is not None:
            out[cur].append({"kind": parts[1], "seq": int(parts[2]), "hex": parts[3]})
    return out


def docker_argv(entrypoint, image=IMAGE, env=None):
    return (["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=16m"]
            + limits.docker_run_args(env) + ["--entrypoint", entrypoint, image])


def run_driver(which, cases, image=IMAGE):
    """Run `which` ('mmdvm' or 'dgw') on the corpus; returns {case: [event]}."""
    argv = docker_argv("/usr/local/bin/dstarsd-%s" % which, image)
    proc = subprocess.run(argv, input=driver_input(cases), capture_output=True, text=True,
                          timeout=RUN_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise RuntimeError("dstarsd-%s exited %d: %s" % (which, proc.returncode, proc.stderr[-400:]))
    return parse_driver_output(proc.stdout)


# ------------------------------------------------------------------ our side, in the oracle's terms

def nmea_id(sentence):
    """'GPGGA' of '$GPGGA,...'."""
    return sentence[1:6]


def dgw_nmea_line(sentence, call="N0CALL"):
    """The APRS line DStarGateway's CNMEASentenceCollector builds from a valid sentence (read from its
    source, confirmed by the oracle run): the checksum is cut and recomputed as two LOWER case
    hex digits, the CR LF is gone, a time stamp 000000.00 would be replaced by the clock (not used
    in the corpus), the prefix is "<CALL>-5>GPS30,DSTAR*:"."""
    text = sentence.rstrip("\r\n")
    body = text[1:].rpartition("*")[0]
    cut = "$" + body
    return "%s-5>GPS30,DSTAR*:%s*%02x" % (call, cut, sd.nmea_checksum(body))


def mmdvm_clean_header(raw41):
    """The header MMDVMHost's CDStarSlowData::getHeader() hands out, or None: it first masks the flags byte
    with 0x68, zeroes flag bytes 2 and 3, clears bit 7 of bytes 3..38 (read from the pinned source, confirmed
    by the case header-printable-bytes) and only then checks the FCS, so a header whose flag bytes are not
    the cleaned ones fails."""
    hdr = bytearray(raw41)
    hdr[0] &= 0x68
    hdr[1] = hdr[2] = 0
    for i in range(3, 39):
        hdr[i] &= 0x7F
    if sd.crc16_x25(bytes(hdr[:39])).to_bytes(2, "little") != bytes(hdr[39:41]):
        return None
    return bytes(hdr)


def ours_mmdvm_view(frames):
    """What MMDVMHost's decoder should print, according to dvtest.dstar_sd.Decoder("reset"):
    list of {"kind", "seq", "hex"} in the order of the driver output (block, header, text per frame)."""
    dec = sd.Decoder("reset")
    out = []
    for seq, wire in frames:
        before = len(dec.blocks)
        events = dec.feed(seq, wire)
        if len(dec.blocks) > before:
            out.append({"kind": "block", "seq": seq, "hex": "%02x" % dec.blocks[-1][1]})
        for ev in events:
            if ev["kind"] == "header":
                clean = mmdvm_clean_header(ev["bytes"])
                if clean is not None:
                    out.append({"kind": "header", "seq": seq, "hex": clean.hex()})
        for ev in events:
            if ev["kind"] == "text":
                out.append({"kind": "text", "seq": seq, "hex": ev["text"].encode("ascii").hex()})
    return _mmdvm_order(out)


def _mmdvm_order(events):
    """The driver prints block, then header, then text for one frame: ours is already in that order."""
    return events


def ours_dgw_view(frames):
    """What DStarGateway's collectors should print, according to dvtest.dstar_sd.Decoder("accumulate",
    nibble="ignore"): text, gpsa (valid CRC only), nmea:<ID> (valid checksum only). The collectors
    add 5 bytes per 0x3x block and do not read the length nibble."""
    dec = sd.Decoder("accumulate", max_dprs=10 ** 6, max_nmea=10 ** 6, nibble="ignore")
    out = []
    for seq, wire in frames:
        for ev in dec.feed(seq, wire):
            if ev["kind"] == "text":
                out.append({"kind": "text", "seq": seq, "hex": ev["text"].encode("ascii").hex()})
            elif ev["kind"] == "dprs" and ev["crc_ok"]:
                out.append({"kind": "gpsa", "seq": seq, "hex": ev["line"].hex()})
            elif ev["kind"] == "nmea" and ev["checksum_ok"]:
                out.append({"kind": "nmea:" + nmea_id(ev["sentence"]), "seq": seq,
                            "hex": dgw_nmea_line(ev["sentence"]).encode("ascii").hex()})
    return out


def ours_dvrouter_view(frames):
    """What the DV-Router unit model (dvtest.dstar_sd.DvrUnitDecoder: reset at every unit, fixed
    frame pairs, length nibble 0x30..0x35, limits 300 / 82) says, in the oracles' event terms:
    text, gpsa, nmea:<ID>. Header, squelch and fast data are opaque (no events)."""
    out = []
    dec = sd.DvrUnitDecoder()
    for unit in sd.split_units(frames):
        for ev in dec.feed_unit(unit):
            seq = ev["seq"]
            if ev["kind"] == "text":
                out.append({"kind": "text", "seq": seq, "hex": ev["text"].encode("ascii").hex()})
            elif ev["kind"] == "dprs" and ev["crc_ok"]:
                out.append({"kind": "gpsa", "seq": seq, "hex": ev["line"].hex()})
            elif ev["kind"] == "nmea" and ev["checksum_ok"]:
                out.append({"kind": "nmea:" + nmea_id(ev["sentence"]), "seq": seq,
                            "hex": dgw_nmea_line(ev["sentence"]).encode("ascii").hex()})
    return out


def _norm(events):
    return [(e["kind"], e["seq"], e["hex"]) for e in events]


# Cases whose DV-Router expectation is NOT an oracle result but the lead's statement (UNVERIFIED by
# any oracle): the length nibble (item 5 of the lead's answers) and the missing frame of a pair
# (item 7). Everything else: text = MMDVMHost, GPS = DStarGateway's collectors (see expected_dvrouter).
LEAD_STATEMENT_CASES = frozenset([
    "nmea-nibble-0x36", "nmea-nibble-0x3f", "nmea-nibble-0x30", "nmea-nibble-low", "nmea-nibble-short-block",
    "pair-hole-text-once", "pair-hole-text-twice", "pair-hole-nmea-then-ok"])

# Named, documented differences between DV-Router's expectation and DStarGateway's result (the
# lenient alternative). They are differences by design, not failures.
NAMED_DIFFERENCES = {
    "DIFF-NIBBLE": "DV-Router reads the length nibble of 0x3x blocks (0x30..0x35, 0 means 5, 0x36..0x3F dropped), "
                   "DStarGateway ignores it and adds 5 bytes per block",
    "DIFF-GPS-LIMIT": "DV-Router limits a D-PRS sentence to 300 bytes including the CR and an NMEA line to 82 bytes "
                      "before the delimiter, DStarGateway has no limit",
    "DIFF-PAIR-HOLE": "DV-Router pairs fixed frames (1,2)..(19,20) of a unit, a hole skips one block; the oracles pair "
                      "the frames they receive in turn, so a lost frame shifts their pairing until the next sync",
}
CASE_DIFFERENCES = {
    "nmea-nibble-0x36": "DIFF-NIBBLE", "nmea-nibble-0x3f": "DIFF-NIBBLE", "nmea-nibble-low": "DIFF-NIBBLE",
    "nmea-nibble-short-block": "DIFF-NIBBLE",
    "dprs-len-301": "DIFF-GPS-LIMIT", "nmea-len-84": "DIFF-GPS-LIMIT", "nmea-len-85": "DIFF-GPS-LIMIT",
    "pair-hole-text-twice": "DIFF-PAIR-HOLE",
}
# DStarGateway emits these, the stated DV-Router limit rejects them
LIMIT_CASES = frozenset(["dprs-len-301", "nmea-len-84", "nmea-len-85"])
# NMEA limit reading A (82 bytes before the LF) is an inference; reading B (82 before CR LF) would accept 84 too
LIMIT_READING_CASES = frozenset(["nmea-len-83", "nmea-len-84"])


def split_text_gps(events):
    """([text events], [gps events: gpsa and nmea:*]) of a normalised event list."""
    return [e for e in events if e[0] == "text"], [e for e in events if e[0] != "text" and e[0] != "block" and e[0] != "header"]


def expected_dvrouter(name, mmdvm_events, dgw_events, model_events):
    """DV-Router's expectation of one case, as {"basis", "text", "gps", ...}: two lists of [kind, seq, hex]
    in the oracles' terms, each in time order (the frame number restarts every superframe, so the two
    lists are not merged). basis "oracle": text events are MMDVMHost's (the DP5f decision: reset at every
    sync), GPS events (gpsa, nmea:*) are DStarGateway's collectors' minus the lines the DV-Router limit
    rejects; MMDVMHost has no GPS decoder, DStarGateway's text is only the lenient alternative.
    basis "lead-statement-unverified": the events are the unit model's (text and GPS), taken from the DV-Router lead's
    statement and checked by no oracle. Header resend, squelch and fast data are opaque (no events)."""
    if name in LEAD_STATEMENT_CASES:
        text, gps = split_text_gps(_norm(model_events))
        basis = "lead-statement-unverified"
    else:
        text = [e for e in _norm(mmdvm_events) if e[0] == "text"]
        gps = [] if name in LIMIT_CASES else split_text_gps(_norm(dgw_events))[1]
        basis = "oracle"
    out = {"basis": basis, "text": text, "gps": gps}
    if name in LIMIT_READING_CASES:
        out["limit_reading"] = "reading A (82 bytes before the LF delimiter) assumed; under reading B (before CR LF) the " \
                               "84 byte sentence is accepted too: open question to the DV-Router lead"
    return out


def compare(name, frames, mmdvm_events, dgw_events):
    """One vector record: frames, what each oracle printed, our views and whether they agree. The two
    oracles are compared with ours for the kinds each one implements: MMDVMHost text / header /
    block types; DStarGateway text / gpsa / nmea."""
    ours_m = ours_mmdvm_view(frames)
    ours_d = ours_dgw_view(frames)
    rec = {
        "case": name,
        "frames": ["%d %s" % (seq, wire.hex()) for seq, wire in frames],
        "mmdvm": _norm(mmdvm_events),
        "dgw": _norm(dgw_events),
        "ours_reset": _norm(ours_m),
        "ours_accumulate": _norm(ours_d),
    }
    rec["mmdvm_match"] = rec["mmdvm"] == rec["ours_reset"]
    rec["dgw_match"] = rec["dgw"] == rec["ours_accumulate"]
    # the two oracles side by side on the one kind both decode
    texts_m = [e for e in rec["mmdvm"] if e[0] == "text"]
    texts_d = [e for e in rec["dgw"] if e[0] == "text"]
    rec["text_agree"] = texts_m == texts_d
    # DV-Router: the decision of DP5f is MMDVMHost's reset at every sync; the unit model is the generator's
    # reading of the lead's statements and must reproduce the expectation
    model = ours_dvrouter_view(frames)
    rec["unit66_hex"] = [sd.unit66(u).hex() for u in sd.split_units(frames)]
    rec["expected_dvrouter"] = expected_dvrouter(name, mmdvm_events, dgw_events, model)
    rec["lenient_alternative"] = rec["dgw"]
    rec["ours_dvrouter"] = _norm(model)
    exp = rec["expected_dvrouter"]
    rec["dvrouter_match"] = split_text_gps(rec["ours_dvrouter"]) == (exp["text"], exp["gps"])
    rec["lenient_differs"] = split_text_gps(rec["lenient_alternative"]) != (exp["text"], exp["gps"])
    if name in CASE_DIFFERENCES:
        rec["named_difference"] = CASE_DIFFERENCES[name]
    return rec


def pins(path=None):
    """The DSTARSD_* settings of .env.example (never hard-coded here)."""
    import os
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.example")
    out = {}
    with open(path) as fh:
        for line in fh:
            key, sep, value = line.strip().partition("=")
            if sep and key.startswith("DSTARSD_"):
                out[key] = value
    return out


def build_records(image=IMAGE):
    cases = corpus()
    mm = run_driver("mmdvm", cases, image)
    dg = run_driver("dgw", cases, image)
    records = []
    for name, note, frames in cases:
        rec = compare(name, frames, mm.get(name, []), dg.get(name, []))
        rec["note"] = note
        records.append(rec)
    return records


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["run", "input"])
    ap.add_argument("--out", default="vectors/dstar_sd/corpus.json")
    args = ap.parse_args(argv)
    if args.action == "input":
        sys.stdout.write(driver_input(corpus()))
        return 0
    records = build_records()
    doc = {"schema": SCHEMA, "image": IMAGE, "pins": pins(), "named_differences": NAMED_DIFFERENCES,
           "unit66": UNIT66_DOC, "cases": records}
    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    bad = [r["case"] for r in records if not (r["mmdvm_match"] and r["dgw_match"] and r["dvrouter_match"])]
    print("cases %d, differing from our decoder: %s" % (len(records), ", ".join(bad) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
