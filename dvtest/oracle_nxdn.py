# SPDX-License-Identifier: MIT
"""Two-decoder oracle for the Kenwood NXDN GPS payload (DP14b).

The pure part parses what the two open decoders print for the same user
data, reduces both to the same fields and writes one JSON disagreement
record for the tracker:

  NXDNGateway (g4klx NXDNClients, GPSHandler): an APRS line published on the
    MQTT topic aprs-gateway/aprs, format string read in
    NXDNGateway/GPSHandler.cpp (Kenwood branch about lines 170-252);
  dsd-fme (lwvmobile): one status line on stderr from nxdn_gps_report in
    src/dsd_gps.c (about lines 724-795).

Neither decoder is run from this module in the unit tests; run_command() is
the thin subprocess wrapper the Docker run uses later (docs/nxdn-gps-cases.md
has the recipe, all of it UNVERIFIED until built). The GPL projects are test
references only: only output formats and field positions (facts) are used,
no code or text is copied.

Resolution differences that the comparison respects:
  - the gateway prints the fraction of a minute in 1/100 minute, course in
    whole degrees (tenths cut off) and speed as the whole part only;
  - dsd-fme prints decimal degrees with six places, speed and course with
    one decimal worth of value.
Both are reduced to: hemisphere + degrees*100+minutes, fraction in 1/100
minute (truncated), course in whole degrees (truncated), speed as a whole
number (truncated).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal

from . import limits
from .proto import nxdn, nxdn_l3

SCHEMA = "dvt-nxdn-gps-compare/1"

# the three fields the research found different between the decoders, then
# the ones where they agree (position degrees/minutes, hemispheres)
DISPUTED = ("longitude_fraction", "course", "speed")
FIELDS = ("latitude", "latitude_fraction", "longitude", "longitude_fraction", "course", "speed")

GATEWAY_RE = re.compile(
    r"(?P<source>[^>\s]+)>APDPRS,NXDN\*,qAR,(?P<gw>[^:]*):!"
    r"(?P<lat>\d{4,7}\.\d{2})(?P<ns>[NS])/(?P<lon>\d{5,8}\.\d{2})(?P<ew>[EW])r"
    r"(?:(?P<course>\d{3})/(?P<speed>\d{3}))?\s*(?P<comment>.*?)\s*$")

DSDFME_RE = re.compile(
    r"GPS:\s*\(\s*(?P<lat>-?\d+(?:\.\d+)?)[^,)]*,\s*(?P<lon>-?\d+(?:\.\d+)?)[^)]*\)\s*"
    r"Speed:\s*(?P<speed>-?\d+(?:\.\d+)?)\s*k/h;\s*COG:\s*(?P<cog>-?\d+(?:\.\d+)?);\s*"
    r"Elevation:\s*(?P<elev>-?\d+);\s*Date:\s*(?P<y>\d{4})/(?P<mo>\d{2})/(?P<d>\d{2})\s*"
    r"Time:\s*(?P<h>\d{2}):(?P<mi>\d{2})")


# ------------------------------------------------------------------ parsing

def _split_degmin(text):
    """'1345.37' -> (1345, 37): whole ddmm and hundredths of a minute."""
    whole, _, frac = text.partition(".")
    return int(whole), int(frac)


def parse_gateway_line(text):
    """dict of the last APRS position line in text (one or more lines, e.g.
    the mosquitto_sub capture), or None."""
    found = None
    for line in text.splitlines():
        match = GATEWAY_RE.search(line)
        if match:
            found = match
    if found is None:
        return None
    lat_dm, lat_h = _split_degmin(found["lat"])
    lon_dm, lon_h = _split_degmin(found["lon"])
    return {"line": found.group(0), "source": found["source"], "gateway": found["gw"],
            "latitude_hem": found["ns"], "latitude_degmin": lat_dm, "latitude_hundredths": lat_h,
            "longitude_hem": found["ew"], "longitude_degmin": lon_dm, "longitude_hundredths": lon_h,
            "course": int(found["course"]) if found["course"] is not None else None,
            "speed": int(found["speed"]) if found["speed"] is not None else None,
            "kind": "kenwood" if "Kenwood" in found["comment"] else
                    ("icom" if "Icom" in found["comment"] else "unknown")}


def parse_dsdfme_log(text):
    """dict of the last dsd-fme NXDN GPS report line in text, or None. The
    numbers are kept as the exact strings dsd-fme printed plus Decimals."""
    found = None
    for match in DSDFME_RE.finditer(text.replace("\n", " ")):
        found = match
    if found is None:
        return None
    return {"line": " ".join(found.group(0).split()),
            "latitude_text": found["lat"], "longitude_text": found["lon"],
            "speed_text": found["speed"], "cog_text": found["cog"], "elevation": int(found["elev"]),
            "date": "%s-%s-%s" % (found["y"], found["mo"], found["d"]),
            "time": "%s:%s" % (found["h"], found["mi"])}


# ------------------------------------------------------------------ normalising

def _coordinate(negative, ddmm, fraction_hundredths, positive_letter, negative_letter):
    return ("%s%d" % (negative_letter if negative else positive_letter, ddmm), fraction_hundredths)


def _from_decimal_text(text):
    """('N'/'S' not applied) negative flag, ddmm, hundredths of a minute from
    a printed decimal-degree string; the value is first rounded to 1e-4 min
    (the printed 1e-6 degree is finer) and the fraction then truncated."""
    negative = text.strip().startswith("-")
    units = int((Decimal(text.strip().lstrip("-")) * 600000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    degrees, rest = divmod(units, 600000)
    minute, fraction = divmod(rest, 10000)
    return negative, degrees * 100 + minute, fraction // 100


def _whole(text):
    return int(Decimal(text.strip().lstrip("-")))        # truncation of a non-negative value


def normalize_gateway(parsed):
    lat, lat_f = _coordinate(parsed["latitude_hem"] == "S", parsed["latitude_degmin"],
                             parsed["latitude_hundredths"], "N", "S")
    lon, lon_f = _coordinate(parsed["longitude_hem"] == "W", parsed["longitude_degmin"],
                             parsed["longitude_hundredths"], "E", "W")
    return {"latitude": lat, "latitude_fraction": lat_f, "longitude": lon, "longitude_fraction": lon_f,
            "course": parsed["course"], "speed": parsed["speed"]}


def normalize_dsdfme(parsed):
    neg, dm, frac = _from_decimal_text(parsed["latitude_text"])
    lat, lat_f = _coordinate(neg, dm, frac, "N", "S")
    neg, dm, frac = _from_decimal_text(parsed["longitude_text"])
    lon, lon_f = _coordinate(neg, dm, frac, "E", "W")
    return {"latitude": lat, "latitude_fraction": lat_f, "longitude": lon, "longitude_fraction": lon_f,
            "course": _whole(parsed["cog_text"]), "speed": _whole(parsed["speed_text"])}


def normalize_fields(info):
    """Reduce a parse_kenwood_fields() result to the same fields: what a
    decoder reading the bytes that way should print."""
    raw = info["raw"]
    lat, lat_f = _coordinate(raw["lat_hem"], raw["lat_degmin"], raw["lat_fraction"] // 100, "N", "S")
    lon, lon_f = _coordinate(raw["lon_hem"], raw["lon_degmin"], raw["lon_fraction"] // 100, "E", "W")
    speed = raw.get("speed_tenths")
    if "speed_whole" in raw:                    # gateway reading: the whole part only
        speed = None if raw["speed_whole"] is None else raw["speed_whole"] * 10
    return {"latitude": lat, "latitude_fraction": lat_f, "longitude": lon, "longitude_fraction": lon_f,
            "course": raw["course_tenths"] // 10 if "course_tenths" in raw else None,
            "speed": speed // 10 if speed is not None else None}


def predict(user_data):
    """What each decoder should print for this Kenwood user data if the
    reading of its source in nxdn_l3 is right. A miss against the real
    output means the reading is wrong, not (only) that the decoders differ."""
    return {"gateway": normalize_fields(nxdn_l3.parse_kenwood_fields(user_data, nxdn_l3.GATEWAY)),
            "dsdfme": normalize_fields(nxdn_l3.parse_kenwood_fields(user_data, nxdn_l3.DSDFME))}


# ------------------------------------------------------------------ comparison

def compare(gateway, dsdfme):
    """Field by field: list of {field, gateway, dsdfme, agree} for the
    normalised dicts of both decoders; a decoder without output gives None
    values and agree False."""
    out = []
    for name in FIELDS:
        a = gateway.get(name) if gateway else None
        b = dsdfme.get(name) if dsdfme else None
        out.append({"field": name, "gateway": a, "dsdfme": b,
                    "agree": gateway is not None and dsdfme is not None and a == b})
    return out


def build_record(case, user_data, *, gateway_text="", dsdfme_text="", layout="gateway", packets=None):
    """The disagreement record (a JSON-able dict) for one encoded input.

    user_data is the Kenwood FULL/SHORT bytes before CRC and pad. The
    intended values are recovered from it with the layout it was encoded
    with, so the record can say which decoder matches the input. Never
    raises on decoder output that does not parse: a missing report is part
    of the record (verdict 'no-output-...')."""
    user_data = bytes(user_data)
    layout_obj = nxdn_l3.LAYOUTS[layout]
    info = nxdn_l3.parse_kenwood_fields(user_data, layout_obj)
    intended = normalize_fields(info)
    gw_parsed = parse_gateway_line(gateway_text)
    dsd_parsed = parse_dsdfme_log(dsdfme_text)
    gw_norm = normalize_gateway(gw_parsed) if gw_parsed else None
    dsd_norm = normalize_dsdfme(dsd_parsed) if dsd_parsed else None
    fields = compare(gw_norm, dsd_norm)
    for row in fields:
        want = intended.get(row["field"])
        row["input"] = want
        row["gateway_matches_input"] = gw_norm is not None and gw_norm.get(row["field"]) == want
        row["dsdfme_matches_input"] = dsd_norm is not None and dsd_norm.get(row["field"]) == want
    if gw_norm is None and dsd_norm is None:
        verdict = "no-output"
    elif gw_norm is None:
        verdict = "no-output-gateway"
    elif dsd_norm is None:
        verdict = "no-output-dsdfme"
    else:
        verdict = "agree" if all(row["agree"] for row in fields) else "disagree"
    predicted = predict(user_data) if info["kind"] == "full" else {"gateway": intended, "dsdfme": None}
    record = {
        "schema": SCHEMA,
        "case": case,
        "layout": layout,
        "verdict": verdict,
        "disputed_fields": [r["field"] for r in fields if r["field"] in DISPUTED and not r["agree"]],
        "disagreements": [r["field"] for r in fields if not r["agree"]],
        "input": {"user_data_hex": user_data.hex(), "kind": info["kind"],
                  "message_hex": nxdn_l3.with_crc(user_data).hex(), "intended": intended},
        "gateway": {"output": gw_parsed, "normalized": gw_norm, "predicted": predicted["gateway"],
                    "prediction_ok": None if gw_norm is None else gw_norm == predicted["gateway"]},
        "dsdfme": {"output": dsd_parsed, "normalized": dsd_norm, "predicted": predicted["dsdfme"],
                   "prediction_ok": None if dsd_norm is None or predicted["dsdfme"] is None
                   else dsd_norm == predicted["dsdfme"]},
        "fields": fields,
    }
    if packets is not None:
        record["input"]["packets_hex"] = [bytes(p).hex() for p in packets]
    return record


def to_json(record):
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


# ------------------------------------------------------------------ Icom framing (NXDNGateway local side)

ICOM_SIZE = 102
ICOM_PAYLOAD_OFFSET = 40


def icom_packet(frame):
    """The 102-byte ICOM packet that carries one 33-byte frame to the local
    side of NXDNGateway: 'ICOM', 01 01 08 E0, zeros, three type bytes at
    37..39 (23 02 18 for data frames, as the gateway writes them), the
    frame at 40, zeros to 102. The gateway only reads the frame and checks
    the length and the magic."""
    if len(frame) != nxdn.PAYLOAD_SIZE:
        raise ValueError("frame must be 33 bytes")
    buf = bytearray(ICOM_SIZE)
    buf[0:8] = b"ICOM\x01\x01\x08\xe0"
    buf[37:40] = bytes([0x23, 0x02, 0x18]) if frame[0] & 0xF0 == 0x90 else \
        bytes([0x23, 0x1C if frame[0] in (0x81, 0x83) else 0x10, 0x21])
    buf[ICOM_PAYLOAD_OFFSET:ICOM_PAYLOAD_OFFSET + nxdn.PAYLOAD_SIZE] = frame
    return bytes(buf)


def icom_packets(packets):
    """ICOM packets for a list of NXDND packets (the frame is bytes 10..42)."""
    return [icom_packet(bytes(p[10:])) for p in packets]


def icom_connect_request():
    """Repeater connect request the gateway answers (byte 5 0x61 -> 0x62).
    Length chosen as 102; the gateway echoes whatever length it got."""
    return b"ICOM\x01\x61" + bytes(ICOM_SIZE - 6)


def parse_icom_packet(data):
    """The 33-byte frame of an ICOM data packet, or None."""
    if len(data) != ICOM_SIZE or data[:4] != b"ICOM":
        return None
    return bytes(data[ICOM_PAYLOAD_OFFSET:ICOM_PAYLOAD_OFFSET + nxdn.PAYLOAD_SIZE])


# ------------------------------------------------------------------ thin subprocess wrapper

def run_command(argv, *, stdin=None, timeout=60, env=None, cwd=None):
    """Run one command without a shell and return {argv, returncode, stdout,
    stderr, timed_out}. For the Docker run only (a decoder, mosquitto_sub);
    the unit tests do not call it with a real decoder."""
    if not argv or isinstance(argv, str):
        raise ValueError("argv must be a non-empty list")
    try:
        done = subprocess.run(list(argv), input=stdin, capture_output=True, timeout=timeout,
                              env=env, cwd=cwd, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"argv": list(argv), "returncode": None, "timed_out": True,
                "stdout": (exc.stdout or b"").decode("utf-8", "replace"),
                "stderr": (exc.stderr or b"").decode("utf-8", "replace")}
    return {"argv": list(argv), "returncode": done.returncode, "timed_out": False,
            "stdout": done.stdout.decode("utf-8", "replace"),
            "stderr": done.stderr.decode("utf-8", "replace")}


# ------------------------------------------------------------------ NXDNGateway container run

# rig/Dockerfile.nxdngw + rig/nxdngw-entry.sh: one throw-away container with
# --network none, a broker and the gateway on loopback. The entry script reads
# lines "NAME HEX" (one ICOM packet each), publishes "begin NAME" markers on
# the topic dvt/case between the cases and prints "TOPIC<TAB>PAYLOAD" lines.
GATEWAY_IMAGE = "dvt-nxdngw:local"
GATEWAY_SRC_ID = 1001            # the entry script maps it to N0CALL
GATEWAY_DST_TG = 31672
GATEWAY_RUN_TIMEOUT = 180
GATEWAY_RECORD_SCHEMA = "dvt-nxdn-gps-gateway/1"
APRS_TOPIC = "aprs-gateway/aprs"
CASE_TOPIC = "dvt/case"


def gateway_cases(positions=None):
    """Cases of the gateway oracle: a list of dicts {name, kind, layout,
    row, fix, user_data}. kind is kenwood-full (every layout the code
    offers), kenwood-short, kenwood-nofix-full / -short or icom (NMEA form).
    Names are NAME.ROW.KIND words without blanks."""
    positions = nxdn_l3.KNOWN_POSITIONS if positions is None else positions
    cases = []

    def add(layout, row, kind, fix, user):
        cases.append({"name": "%s.%s.%s" % (layout, row, kind), "kind": kind, "layout": layout,
                      "row": row, "fix": fix, "user_data": user})

    for layout in ("gateway", "dsdfme", "lon-both"):
        for row, fix in positions:
            add(layout, row, "kenwood-full", fix, nxdn_l3.kenwood_full(fix, nxdn_l3.LAYOUTS[layout]))
    for row, fix in positions:
        add("gateway", row, "kenwood-short", fix, nxdn_l3.kenwood_short(fix))
    add("gateway", "nofix", "kenwood-nofix-full", None, nxdn_l3.kenwood_no_fix("full"))
    add("gateway", "nofix", "kenwood-nofix-short", None, nxdn_l3.kenwood_no_fix("short"))
    for row, fix in positions:
        add("icom", row, "icom", fix, nxdn_l3.icom_user_data(fix))
    return cases


def gateway_input(cases, src=GATEWAY_SRC_ID, dst=GATEWAY_DST_TG):
    """Text for rig/nxdngw-entry.sh: per case the five ICOM packets of the
    data call (header, blocks, TX_REL), one "NAME HEX" line each."""
    lines = []
    for case in cases:
        packets = nxdn_l3.data_call_packets(src, dst, case["user_data"])
        for packet in icom_packets(packets):
            lines.append("%s %s" % (case["name"], packet.hex()))
    return "\n".join(lines) + "\n"


def split_gateway_capture(text):
    """{'cases': {name: [APRS payload lines]}, 'order': [names], 'complete':
    bool} from the entry script's stdout. Lines before the first marker are
    ignored; 'complete' means the final 'end' marker was seen."""
    cases, order, current, complete = {}, [], None, False
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line:
            continue
        topic, _, payload = line.partition("\t")
        if topic == CASE_TOPIC:
            if payload.startswith("begin "):
                current = payload[6:].strip()
                if current not in cases:
                    cases[current] = []
                    order.append(current)
            elif payload.strip() == "end":
                current, complete = None, True
        elif topic == APRS_TOPIC and current is not None:
            cases[current].append(payload.rstrip("\r"))
    return {"cases": cases, "order": order, "complete": complete}


def gateway_docker_argv(input_path, name, image=GATEWAY_IMAGE, env=None):
    """docker run words: no network, the repository's resource limits, a
    read-only root with a small tmpfs, no capabilities, the packet file
    mounted read-only. `name` is the container name (for the timeout kill)."""
    return (["docker", "run", "--rm", "--name", name, "--network", "none"]
            + limits.docker_run_args(env)
            + ["--read-only", "--tmpfs", "/tmp:rw,size=16m", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges",
               "-v", "%s:/in/packets.txt:ro" % input_path, image, "run", "/in/packets.txt"])


def run_gateway(cases, image=GATEWAY_IMAGE, timeout=GATEWAY_RUN_TIMEOUT, workdir=None):
    """Run the container once for all cases; returns {capture, stderr,
    returncode, timed_out}. A timed-out container is removed by name."""
    with tempfile.TemporaryDirectory(prefix="dvt-nxdngw-", dir=workdir) as tmp:
        os.chmod(tmp, 0o755)
        path = os.path.join(tmp, "packets.txt")
        with open(path, "w", encoding="ascii") as handle:
            handle.write(gateway_input(cases))
        os.chmod(path, 0o644)
        name = "dvt-nxdngw-%d" % os.getpid()
        done = run_command(gateway_docker_argv(path, name, image), timeout=timeout)
        if done["timed_out"]:
            run_command(["docker", "rm", "-f", name], timeout=30)
    return {"capture": done["stdout"], "stderr": done["stderr"], "returncode": done["returncode"],
            "timed_out": done["timed_out"]}


def icom_record(case, gateway_text):
    """Record for an Icom NMEA input: the printed line, the reduced values and
    the values of the encoded fix (fraction truncated to 1/100 minute, as for
    the Kenwood rows, so a rounding in the gateway's Icom branch shows up)."""
    fix = case["fix"]
    parsed = parse_gateway_line(gateway_text)
    normalized = normalize_gateway(parsed) if parsed else None
    lat_neg, lat_dm, lat_frac = nxdn_l3.to_degmin(fix.latitude)
    lon_neg, lon_dm, lon_frac = nxdn_l3.to_degmin(fix.longitude)
    intended = {"latitude": "%s%d" % ("S" if lat_neg else "N", lat_dm), "latitude_fraction": lat_frac // 100,
                "longitude": "%s%d" % ("W" if lon_neg else "E", lon_dm), "longitude_fraction": lon_frac // 100,
                "course": int(nxdn_l3.course_tenths(fix.course) // 10), "speed": int(fix.speed)}
    fields = [{"field": name, "gateway": None if normalized is None else normalized[name],
               "input": intended[name],
               "gateway_matches_input": normalized is not None and normalized[name] == intended[name]}
              for name in FIELDS]
    return {"schema": GATEWAY_RECORD_SCHEMA, "case": case["name"], "layout": "icom", "kind": "icom",
            "verdict": "no-output-gateway" if normalized is None else "output",
            "input": {"user_data_hex": case["user_data"].hex(), "intended": intended},
            "gateway": {"output": parsed, "normalized": normalized},
            "fields": fields,
            "mismatches": [f["field"] for f in fields if not f["gateway_matches_input"]]}


def gateway_records(cases, capture):
    """One JSON-able record per case from the split capture (see
    split_gateway_capture). Kenwood rows with a fix use build_record (the
    dsd-fme side is empty); a row without a printed line gets the verdict
    no-output-gateway, which for the no-fix rows is the expected result."""
    records = []
    for case in cases:
        lines = capture["cases"].get(case["name"])
        text = "\n".join(lines or [])
        if case["kind"] == "icom":
            record = icom_record(case, text)
        elif case["kind"].startswith("kenwood-nofix"):
            parsed = parse_gateway_line(text)
            record = {"schema": GATEWAY_RECORD_SCHEMA, "case": case["name"], "layout": case["layout"],
                      "kind": case["kind"], "verdict": "no-output-gateway" if parsed is None else "output",
                      "input": {"user_data_hex": case["user_data"].hex()},
                      "gateway": {"output": parsed, "lines": lines}}
        else:
            record = build_record(case["name"], case["user_data"], gateway_text=text, layout=case["layout"])
            record["kind"] = case["kind"]
        record["gateway_lines"] = lines
        records.append(record)
    return records


def gateway_main(argv):
    """Run NXDNGateway in its container for every table row and write the
    records:

      python3 -m dvtest.oracle_nxdn gateway --out vectors/nxdn_gps/gateway-run.jsonl

    One JSON object per line (schema dvt-nxdn-gps-gateway/1); needs docker and
    the image dvt-nxdngw:local (rig/Dockerfile.nxdngw). Exit status 0 = every
    case produced its report (or, for the no-fix rows, none), 1 = otherwise."""
    parser = argparse.ArgumentParser(prog="oracle_nxdn gateway", description=gateway_main.__doc__.splitlines()[0])
    parser.add_argument("--image", default=GATEWAY_IMAGE)
    parser.add_argument("--timeout", type=int, default=GATEWAY_RUN_TIMEOUT)
    parser.add_argument("--out", required=True, help="JSON lines file for the records")
    parser.add_argument("--capture-out", help="also write the raw capture text here")
    args = parser.parse_args(argv)
    cases = gateway_cases()
    run = run_gateway(cases, image=args.image, timeout=args.timeout)
    if args.capture_out:
        with open(args.capture_out, "w", encoding="utf-8") as handle:
            handle.write(run["capture"])
    capture = split_gateway_capture(run["capture"])
    records = gateway_records(cases, capture)
    with open(args.out, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    bad = [r["case"] for r in records
           if (not r["gateway_lines"]) != r["kind"].startswith("kenwood-nofix")]
    sys.stderr.write("gateway oracle: %d cases, %d without the expected outcome, complete=%s, rc=%s\n"
                     % (len(records), len(bad), capture["complete"], run["returncode"]))
    return 0 if capture["complete"] and not bad else 1


# ------------------------------------------------------------------ command line

def main(argv=None):
    """Compare two captured decoder outputs for one input:

      python3 -m dvtest.oracle_nxdn --case base-c90-s55 --user-data HEX \\
          --layout gateway --gateway-out gw.txt --dsdfme-log dsd.txt --out rec.json

    Exit status 0 = agree, 1 = disagree, 2 = a decoder gave no report.

    `python3 -m dvtest.oracle_nxdn gateway ...` runs the NXDNGateway container
    over the whole table (gateway_main)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "gateway":
        return gateway_main(argv[1:])
    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("--case", required=True)
    parser.add_argument("--user-data", required=True, help="hex of the Kenwood user data (no CRC)")
    parser.add_argument("--layout", default="gateway", choices=sorted(nxdn_l3.LAYOUTS))
    parser.add_argument("--gateway-out", help="file with the mosquitto_sub capture")
    parser.add_argument("--dsdfme-log", help="file with the dsd-fme stderr log")
    parser.add_argument("--out", help="write the JSON record here (default stdout)")
    args = parser.parse_args(argv)

    def read(path):
        if not path:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    record = build_record(args.case, bytes.fromhex(args.user_data), gateway_text=read(args.gateway_out),
                          dsdfme_text=read(args.dsdfme_log), layout=args.layout)
    text = to_json(record)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    else:
        sys.stdout.write(text)
    return {"agree": 0, "disagree": 1}.get(record["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
