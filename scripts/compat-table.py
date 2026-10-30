#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compatibility table from rig runs of the same cases against different
reference reflectors (scripts/run.sh with DVT_REF_KIND=xlxd and =urfd):

    scripts/compat-table.py out/<run-a> out/<run-b> [...] > compat.md

Each run directory needs junit.xml and run.txt. A column per run is
labelled by its ref_kind (and run id). A cell says how the system under
test did against that reference: "same" (passed: the sut behaves like the
reference), "differs" (failed), "skip", "error"; "n/a" for files that need
an xlxd-only feature (interlink, USRP rows, transcoder link) when the
reference is not xlxd. A last column flags cases whose verdict changes
between references.
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# test files that need xlxd-only rig features: n/a against another reference
XLXD_ONLY = ("test_xlx.py", "test_xlxd_interlink.py", "test_usrp.py", "test_usrp_dynamic.py",
             "test_xlxd_usrp_netns.py", "test_ambed_wire.py", "test_xlxd_matrix.py",
             "test_nxdn_peer.py", "test_p25_network.py", "test_backend_loss.py",
             "test_xlxd_transcoder_lifecycle.py", "test_allstar.py")


def run_info(directory):
    fields = {}
    first = (directory / "run.txt").read_text().splitlines()[0]
    for item in first.split(" "):
        if "=" in item:
            key, value = item.split("=", 1)
            fields[key] = value
    return fields


def verdicts(directory):
    out = {}
    for case in ET.parse(directory / "junit.xml").getroot().iter("testcase"):
        name = "%s::%s" % (case.get("classname", "").split(".")[-1] + ".py", case.get("name"))
        if case.find("failure") is not None:
            out[name] = "differs"
        elif case.find("error") is not None:
            out[name] = "error"
        elif case.find("skipped") is not None:
            out[name] = "skip"
        else:
            out[name] = "same"
    return out


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    runs = []
    for arg in argv[1:]:
        directory = Path(arg)
        info = run_info(directory)
        runs.append(("%s-ref (%s)" % (info.get("ref_kind", "xlxd"), directory.name), info, verdicts(directory)))
    cases = sorted({name for _, _, v in runs for name in v})
    sut = runs[0][1].get("xlxd_sut", "sut")
    print("Compatibility: system under test %s, profile %s" % (sut, runs[0][1].get("sut_profile", "xlxd")))
    print()
    print("| case | " + " | ".join(label for label, _, _ in runs) + " | changes |")
    print("|---|" + "---|" * (len(runs) + 1))
    counts = [dict() for _ in runs]
    for name in cases:
        cells = []
        for index, (_, info, result) in enumerate(runs):
            cell = result.get(name, "-")
            if info.get("ref_kind", "xlxd") != "xlxd" and name.split("::")[0] in XLXD_ONLY:
                cell = "n/a"
            counts[index][cell] = counts[index].get(cell, 0) + 1
            cells.append(cell)
        judged = {c for c in cells if c in ("same", "differs")}
        print("| %s | %s | %s |" % (name, " | ".join(cells), "yes" if len(judged) > 1 else ""))
    print()
    for (label, _, _), count in zip(runs, counts):
        print("- %s: %s" % (label, ", ".join("%s %d" % item for item in sorted(count.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
