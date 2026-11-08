#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Keep docs/coverage-matrix.md from rotting (stdlib only, no network).

    scripts/coverage-check.py            check the matrix, exit 1 on a problem
    scripts/coverage-check.py --write    regenerate the summary table in place
    scripts/coverage-check.py --list     print the case ids the matrix names

Checks: every case id `module::function` named in a matrix row still exists
as a function or method in cases/**/<module>.py (a [param] suffix is
ignored); every row has six cells and a valid status; the summary table
between the summary markers matches the rows.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "coverage-matrix.md"
STATUSES = ("OK", "PARTIAL", "GAP", "NOT-IMPLEMENTED")
BEGIN = "<!-- summary:begin -->"
END = "<!-- summary:end -->"
ID_RE = re.compile(r"`([A-Za-z0-9_]+)::([A-Za-z0-9_]+)(?:\[[^`]*\])?`")


def functions():
    """{module basename: set of function names} for every cases/**/test_*.py."""
    out = {}
    for path in sorted((ROOT / "cases").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = out.setdefault(path.stem, set())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return out


def parse(text):
    """[(section title, [cells of each row])] for the sections that hold a
    matrix table (header cell 'Obligation')."""
    sections = []
    title = None
    in_table = False
    for line in text.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            in_table = False
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] == "Obligation":
            sections.append((title, []))
            in_table = True
            continue
        if in_table and not set(line.replace("|", "").strip()) <= set("-: "):
            sections[-1][1].append((cells, line))
    return sections


def summary(sections):
    head = ["| Protocol | Rows | OK | PARTIAL | GAP | NOT-IMPL |",
            "|---|---|---|---|---|---|"]
    body = []
    tot = [0, 0, 0, 0, 0]
    for title, rows in sections:
        n = [len(rows)] + [sum(1 for c, _ in rows if len(c) == 6 and c[5] == s)
                           for s in STATUSES]
        body.append("| %s | %s |" % (title, " | ".join(map(str, n))))
        tot = [a + b for a, b in zip(tot, n)]
    body.append("| **Total** | %s |" % " | ".join("**%d**" % v for v in tot))
    return "\n".join(head + body)


def main(argv):
    text = MATRIX.read_text(encoding="utf-8")
    sections = parse(text)
    if "--list" in argv:
        for m in sorted(set(ID_RE.findall(text))):
            print("%s::%s" % m)
        return 0
    block = "%s\n%s\n%s" % (BEGIN, summary(sections), END)
    if "--write" in argv:
        if BEGIN in text and END in text:
            a, b = text.index(BEGIN), text.index(END) + len(END)
            text = text[:a] + block + text[b:]
        else:
            print("coverage-check: no summary markers in %s" % MATRIX)
            return 1
        MATRIX.write_text(text, encoding="utf-8")
        return 0

    problems = []
    known = functions()
    for title, rows in sections:
        for cells, line in rows:
            if len(cells) != 6:
                problems.append("%s: %d cells, want 6: %s" % (title, len(cells), line[:80]))
                continue
            if cells[5] not in STATUSES:
                problems.append("%s: bad status %r: %s" % (title, cells[5], cells[0][:60]))
            ids = ID_RE.findall(cells[2])
            if cells[5] in ("OK", "PARTIAL") and not ids:
                problems.append("%s: %s row without a case id: %s" % (title, cells[5], cells[0][:60]))
            if cells[5] == "NOT-IMPLEMENTED" and ids:
                problems.append("%s: %s row names a case id: %s" % (title, cells[5], cells[0][:60]))
    for mod, fn in sorted(set(ID_RE.findall(text))):
        if fn not in known.get(mod, ()):
            problems.append("missing test function %s::%s" % (mod, fn))
    if BEGIN in text and END in text:
        a, b = text.index(BEGIN), text.index(END) + len(END)
        if text[a:b] != block:
            problems.append("summary table is stale (run scripts/coverage-check.py --write)")
    else:
        problems.append("no summary markers")
    for p in problems:
        print("coverage-check: " + p)
    if not problems:
        print("coverage-check: %d rows, %d case ids, all present" %
              (sum(len(r) for _, r in sections), len(set(ID_RE.findall(text)))))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
