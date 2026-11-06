#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Write (or check) the M17 v3 vector files in vectors/m17v3/.

  python3 scripts/gen-m17v3-vectors.py            # write the files
  python3 scripts/gen-m17v3-vectors.py --check    # exit 1 when a file differs
  python3 scripts/gen-m17v3-vectors.py --out DIR  # write somewhere else

Deterministic: running it twice gives the same bytes. The content comes from
dvtest/m17v3_vectors.py (docs/m17v3-cases.md); README.md in the target
directory is hand written and is not touched.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dvtest import m17v3_vectors  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=os.path.join(ROOT, "vectors", "m17v3"))
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)
    files = m17v3_vectors.build_all()
    bad = 0
    for name, text in sorted(files.items()):
        path = os.path.join(args.out, name)
        if args.check:
            try:
                with open(path, encoding="ascii") as handle:
                    same = handle.read() == text
            except OSError:
                same = False
            print("%s %s" % ("ok     " if same else "DIFFERS", name))
            bad += 0 if same else 1
        else:
            os.makedirs(args.out, exist_ok=True)
            with open(path, "w", encoding="ascii", newline="\n") as handle:
                handle.write(text)
            print("wrote %s (%d bytes)" % (path, len(text)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
