# SPDX-License-Identifier: MIT
"""Dire Wolf's APRS decoder as an oracle for DV-Router's "APRS core" (HWFREE1 item 3, DP9;
docs/direwolf-cases.md, section "APRS decode oracle").

Host side of the `aprs` job of rig/direwolf-entry.py: the standalone `decode_aprs` program
of the dvt-direwolf image (Dire Wolf, GPL-2.0-or-later, runs only as a process in the
throw-away container, --network none) is run once per frame and what it PRINTS is stored
as facts: nothing of its code is here. Inputs:

  * the information fields of the 17 frames of vectors/direwolf/rx_frames.json
    (group "existing"), and
  * the strings of dvtest.aprs_cases (spec-derived, one group per APRS type).

The frame is built by dvtest.ax25 (`N0CALL-9>DEST,WIDE1-1:` + information bytes) and given
to decode_aprs as hex, so the information bytes (Mic-E has control characters) arrive
exactly as written. Output: vectors/direwolf/aprs_decode.json, per case the raw information
bytes in hex, the lines decode_aprs printed, and a best-effort field table parsed from
those lines (keys normalised; `lines` is the authority, `fields` is a convenience and
holds only what the parser recognised).

    python3 -m dvtest.oracle_aprs run --out vectors/direwolf/aprs_decode.json
"""
import argparse
import json
import re
import sys

from . import aprs_cases, ax25, oracle_direwolf

SCHEMA = "dvt-direwolf-aprs/1"
SRC = aprs_cases.SRC
VIA = aprs_cases.VIA

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LAT = r"(?:Invalid Latitude|(?P<ns>[NS]) (?P<latd>\d+) (?P<latm>[\d.]+))"
_LON = r"(?:Invalid Longitude|(?P<ew>[EW]) (?P<lond>\d+) (?P<lonm>[\d.]+))"
_LOC = re.compile(r"^(?:Grid square = (?P<grid>[A-Za-z0-9]+), )?" + _LAT + ", " + _LON + r"(?P<rest>.*)$")
_PHG = re.compile(r"(?P<w>\d+) W height\(HAAT\)=(?P<ft>\d+)ft=(?P<m>\d+)m (?P<gain>\d+)dBi (?P<dir>\S+)")
_MIC_E_STATUS = ("Off Duty", "En Route", "In Service", "Returning", "Committed", "Special", "Priority",
                 "Emergency", "EMERGENCY", "Custom-0", "Custom-1", "Custom-2", "Custom-3", "Custom-4", "Custom-5",
                 "Custom-6")
_WARNING = re.compile(r"^(WARNING|Message number|Invalid|Error|APRS Message must|Use of |Tell the sender)|"
                      r"\b(must|should|obsolete|Spec)\b")


# ------------------------------------------------------------------ cases

def existing_cases():
    """(name, group, dest, info bytes, note) for the frames of oracle_direwolf.RX_CASES: the
    TNC2 line is turned into a frame by dvtest.ax25 and the frame keeps its own addresses."""
    out = []
    for name, tnc2, note in oracle_direwolf.RX_CASES:
        frame = ax25.from_tnc2(tnc2)
        out.append(("ex-" + name, "existing", tnc2.split(":", 1)[0], ax25.parse_frame(frame)["info"], note))
    return out


def all_cases():
    return existing_cases() + aprs_cases.cases()


def frame_for(header, info):
    """AX.25 frame bytes for a case. `header` is `SRC>DEST[,VIA]` (existing frames) or a bare
    destination (the generated cases, sent from SRC through VIA)."""
    if ">" not in header:
        header = "%s>%s,%s" % (SRC, header, VIA)
    return ax25.from_tnc2(header + ":") + bytes(info)


def aprs_job(cases=None):
    cases = all_cases() if cases is None else cases
    return {"mode": "aprs", "cases": [{"name": n, "frame_hex": frame_for(d, i).hex()} for n, _g, d, i, _note in cases]}


# ------------------------------------------------------------------ output parser

def output_lines(text):
    """The lines decode_aprs printed about the packet: everything after the AX.25 dump (the line
    of dashes), blank lines and colour codes removed. The first one is the address line plus the
    information as text; the second is the type line."""
    text = _ANSI.sub("", text)
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == "-------------------":
            lines = lines[i + 1:]
            break
    else:
        lines = [ln for ln in lines if not re.match(r"^[0-9a-f]{2}( [0-9a-f]{2})+\s*$", ln)]
    return [ln.rstrip("\r") for ln in lines if ln.strip()]


def _split_top(text):
    """Split on ', ' outside double quotes."""
    parts, cur, quoted, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            quoted = not quoted
        if not quoted and text.startswith(", ", i):
            parts.append("".join(cur))
            cur = []
            i += 2
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _num(text):
    f = float(text)
    return int(f) if f == int(f) and "." not in text else f


def parse_type_line(line):
    """Fields of the second line: data type, quoted name, symbol, manufacturer, Mic-E status,
    PHG, range, and the message / bulletin fields. `parts` is the line split at its top-level
    commas (always present, the authority)."""
    out = {"error": line.startswith("ERROR")}
    parts = _split_top(line)
    out["parts"] = parts
    head = parts[0]
    out["data_type"] = head
    m = re.match(r'^"(.*)" (ACKnowledged|REJected) message number "(.*)" from "(.*)"$', line.split(", UNKNOWN")[0])
    if m:
        out.update({"data_type": m.group(2) + " message", "by_station": m.group(1), "message_number": m.group(3),
                    "of_station": m.group(4)})
    m = re.match(r'^(Group Bulletin|Bulletin) with identifier "(.*)"$', head)
    if m:
        out["data_type"] = m.group(1)
        out["bulletin_id"] = m.group(2)
    left = []
    unknown_vendor = False
    for p in parts[1:]:
        m = re.match(r'^number "(.*)"$', p)
        if m:
            out["message_number"] = m.group(1)
            continue
        if p == "with no number":
            out["message_number"] = None
            continue
        m = re.match(r'^from "(.*)" to "(.*)"$', p)
        if m:
            out["message_from"], out["message_to"] = m.group(1), m.group(2)
            continue
        m = re.match(r'^group name "(.*)"$', p)
        if m:
            out["bulletin_group"] = m.group(1)
            continue
        m = re.match(r'^"(.*)"$', p)
        if m and "name" not in out:
            out["name"] = m.group(1)
            continue
        m = _PHG.match(p)
        if m:
            out["phg"] = {"power_w": int(m.group("w")), "height_ft": int(m.group("ft")), "height_m": int(m.group("m")),
                          "gain_dbi": int(m.group("gain")), "directivity": m.group("dir")}
            continue
        m = re.match(r"^range=([\d.]+)$", p)
        if m:
            out["range_miles"] = float(m.group(1))
            continue
        if p in _MIC_E_STATUS:
            out["mic_e_status"] = p
            continue
        if p == "UNKNOWN vendor/model":
            unknown_vendor = True
            continue
        left.append(p)
    # what is left: [symbol description,] [vendor text]. With an unknown vendor the one
    # remaining part is the symbol; with a known vendor the split is not reliable.
    if unknown_vendor:
        out["vendor"] = None
        if left:
            out["symbol"] = left[0]
    elif left:
        out["symbol_and_vendor"] = left
    return out


def parse_location_line(line):
    """lat / lon (signed decimal degrees from the printed degrees and minutes), speed, course,
    altitude, grid square. None when the line is not a position line."""
    m = _LOC.match(line)
    if not m:
        return None
    out = {"lat": None, "lon": None}
    if m.group("ns"):
        lat = int(m.group("latd")) + float(m.group("latm")) / 60.0
        out["lat"] = round(-lat if m.group("ns") == "S" else lat, 6)
        out["lat_text"] = "%s %s %s" % (m.group("ns"), m.group("latd"), m.group("latm"))
    if m.group("ew"):
        lon = int(m.group("lond")) + float(m.group("lonm")) / 60.0
        out["lon"] = round(-lon if m.group("ew") == "W" else lon, 6)
        out["lon_text"] = "%s %s %s" % (m.group("ew"), m.group("lond"), m.group("lonm"))
    if m.group("grid"):
        out["grid"] = m.group("grid")
    for part in _split_top(m.group("rest").lstrip(", ")) if m.group("rest") else []:
        mm = re.match(r"^(\d+) km/h \((\d+) MPH\)$", part)
        if mm:
            out["speed_kmh"], out["speed_mph"] = int(mm.group(1)), int(mm.group(2))
            continue
        mm = re.match(r"^course (\d+)$", part)
        if mm:
            out["course_deg"] = int(mm.group(1))
            continue
        mm = re.match(r"^alt (-?\d+) m \((-?\d+) ft\)$", part)
        if mm:
            out["alt_m"], out["alt_ft"] = int(mm.group(1)), int(mm.group(2))
            continue
        if part:
            out.setdefault("other", []).append(part)
    return out


_WX_KEYS = [
    (re.compile(r"^wind ([\d.]+) mph$"), "wind_mph", float),
    (re.compile(r"^direction (\d+)$"), "wind_dir_deg", int),
    (re.compile(r"^gust (\d+)$"), "gust_mph", int),
    (re.compile(r"^temperature (-?\d+)$"), "temp_f", int),
    (re.compile(r"^rain ([\d.]+) in last hour$"), "rain_1h_in", float),
    (re.compile(r"^rain ([\d.]+) in last 24 hours$"), "rain_24h_in", float),
    (re.compile(r"^rain ([\d.]+) since midnight$"), "rain_midnight_in", float),
    (re.compile(r"^humidity (\d+)$"), "humidity_pct", int),
    (re.compile(r"^barometer ([\d.]+)$"), "barometer_inhg", float),
    (re.compile(r"^(\d+) watts/m\^2$"), "luminosity_wm2", int),
]


def parse_weather_line(line):
    """Weather fields of a line like `wind 4.6 mph, direction 220, gust 5, temperature 77, ...`;
    None when the line does not start like one."""
    parts = _split_top(line)
    out, other = {}, []
    for p in parts:
        if not p:
            continue
        for rx, key, conv in _WX_KEYS:
            m = rx.match(p)
            if m:
                out[key] = conv(m.group(1))
                break
        else:
            m = re.match(r'^"(.*)"$', p)
            if m:
                if m.group(1):
                    out["software"] = m.group(1)
            else:
                other.append(p)
    if not out:
        return None
    if other:
        out["other"] = other
    return out


def parse_output(text):
    """{lines, fields}. `lines` are the decode_aprs lines after the AX.25 dump, `fields` what the
    parser recognised: type line fields, position, weather, comment (the first line that is
    neither of those nor a warning), `warnings` (lines that read like a complaint, before or after
    the type line) and `extra_lines` (anything else)."""
    lines = output_lines(text)
    fields = {"warnings": [], "extra_lines": []}
    body = lines[1:]
    type_at = next((i for i, ln in enumerate(body) if not _WARNING.search(ln)), None)
    if type_at is None:
        fields["warnings"] = body
        return {"lines": lines, "fields": fields}
    fields["warnings"] = body[:type_at]
    fields.update(parse_type_line(body[type_at]))
    comment = None
    for ln in body[type_at + 1:]:
        loc = parse_location_line(ln) if "position" not in fields else None
        if loc is not None:
            fields["position"] = loc
            continue
        wx = parse_weather_line(ln) if "weather" not in fields and "Weather" in fields.get("data_type", "") else None
        if wx is not None:
            fields["weather"] = wx
            continue
        if _WARNING.search(ln):
            fields["warnings"].append(ln)
        elif comment is None:
            comment = ln
        else:
            fields["extra_lines"].append(ln)
    if comment is not None:
        fields["comment"] = comment
    return {"lines": lines, "fields": fields}


# ------------------------------------------------------------------ records

def records(result, cases=None):
    """One record per case from an aprs job result, joined with the case table."""
    cases = all_cases() if cases is None else cases
    meta = {n: (g, d, i, note) for n, g, d, i, note in cases}
    out = []
    for res in result["cases"]:
        if res["name"] not in meta:
            continue
        group, dest, info, note = meta[res["name"]]
        parsed = parse_output(res["output"])
        header = dest if ">" in dest else "%s>%s,%s" % (SRC, dest, VIA)
        out.append({"schema": SCHEMA, "case": res["name"], "group": group, "note": note,
                    "frame_header": header, "info_hex": bytes(info).hex(),
                    "info_text": ax25.info_to_text(info), "rc": res["rc"],
                    "dw_lines": parsed["lines"], "fields": parsed["fields"]})
    return out


def document(result, version, cases=None):
    recs = records(result, cases)
    groups = {}
    for r in recs:
        groups.setdefault(r["group"], []).append(r["case"])
    return {"schema": SCHEMA, "tool": version, "image": oracle_direwolf.IMAGE, "program": "decode_aprs (hex input)",
            "groups": groups, "records": recs}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="oracle_aprs", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run the aprs job and write the vector file")
    run.add_argument("--out", default="vectors/direwolf/aprs_decode.json")
    run.add_argument("--raw", default=None, help="also write the raw job result here (scratch use)")
    args = parser.parse_args(argv)
    version = oracle_direwolf.run_job({"mode": "versions"})["direwolf"]
    result = oracle_direwolf.run_job(aprs_job())
    if args.raw:
        with open(args.raw, "w", encoding="ascii") as fh:
            json.dump(result, fh, indent=1, ensure_ascii=True)
    doc = document(result, version)
    with open(args.out, "w", encoding="ascii") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True, ensure_ascii=True)
        fh.write("\n")
    bad = [r["case"] for r in doc["records"] if r["rc"] != 0 or len(r["dw_lines"]) < 2]
    print("aprs cases %d, groups %d, without a decode %s" % (len(doc["records"]), len(doc["groups"]), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
