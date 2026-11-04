# SPDX-License-Identifier: MIT
"""Markdown table of the transcoder level measurements of one or more runs of cases/test_transcoder_levels.py:

  python3 -m dvtest.tools.levels_table out/<run1>/transcoder-levels.jsonl out/<run2>/transcoder-levels.jsonl ...

Each file holds one JSON row per (pair, tag, signal); the tag of a row is the transcoder image of its run."""
import json
import sys

from dvtest import crossaudio


def read(paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh if line.strip()]
    return rows


def main(argv=None):
    paths = (argv if argv is not None else sys.argv[1:])
    if not paths:
        sys.exit("usage: python3 -m dvtest.tools.levels_table FILE.jsonl ...")
    print(crossaudio.format_table(read(paths)))


if __name__ == "__main__":
    main()
