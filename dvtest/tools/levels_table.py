# SPDX-License-Identifier: MIT
"""Markdown table of the transcoder level measurements of one or more runs of cases/test_transcoder_levels.py:

  python3 -m dvtest.tools.levels_table [--summary] out/<run1>/transcoder-levels.jsonl out/<run2>/transcoder-levels.jsonl ...

--load LOAD.json CPU.txt: the 16-stream line of a run (out/<run>/transcoder-levels-load.json, the sampler file of scripts/transcoder-cpu-sample.sh).
--summary: one line per pair (mean speech level change per tag, tone, lost frames) instead of one line per measurement.

Each file holds one JSON row per (pair, tag, signal); the tag of a row is the transcoder image of its run."""
import json
import sys

from dvtest import crossaudio


def short(tag):
    """The image tag without repository and prefix (older rows hold the whole image name)."""
    return tag.split(":")[-1].replace("64s-pcm-", "")


def read(paths):
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            rows += [dict(json.loads(line), tag=short(json.loads(line)["tag"])) for line in fh if line.strip()]
    return rows


def main(argv=None):
    args = (argv if argv is not None else sys.argv[1:])
    if args[:1] == ["--load"] and len(args) == 3:
        with open(args[1], encoding="utf-8") as fh:
            load = json.loads(fh.read().strip().splitlines()[-1])
        with open(args[2], encoding="utf-8") as fh:
            print(json.dumps(crossaudio.load_summary(load, fh.read().splitlines()), sort_keys=True))
        return
    summary = "--summary" in args
    paths = [a for a in args if a != "--summary"]
    if not paths:
        sys.exit("usage: python3 -m dvtest.tools.levels_table [--summary] FILE.jsonl ...")
    rows = read(paths)
    print(crossaudio.summarize(rows) if summary else crossaudio.format_table(rows))


if __name__ == "__main__":
    main()
