# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_mmdvm_stack.py (docs/mmdvm-stack-cases.md): which gateway row of the public state belongs to which mode,
and which modes the reflector under test offers at all. No rig needed to test them (cases/unit/test_mmdvm_stack_lib.py)."""


def row_matches(row, mode, room, words):
    """True when a client row of the public state is the gateway of `mode`: in module `room` and one of `words` in its
    protocol / edge / kind text."""
    row_room = str(row.get("room") or row.get("module") or "")
    text = " ".join(str(row.get(k, "")) for k in ("protocol", "edge", "kind", "proto")).lower()
    return row_room == room and any(word in text for word in words[mode])


def linked_modes(rows, modes, rooms, words):
    """The set of `modes` that have a gateway row in the public state `rows`."""
    return {mode for mode in modes if any(row_matches(r, mode, rooms[mode], words) for r in rows)}


def offered_modes(modes, probe):
    """The modes (in order) for which probe(mode) is true: the reflector answers a client link of that mode. A mode that is not
    offered cannot have a gateway linked either, so the public-state checks leave it out the way the voice cases skip it."""
    return [mode for mode in modes if probe(mode)]


def not_linked(expected, seen):
    """Sorted modes of `expected` that are not in `seen`."""
    return sorted(set(expected) - set(seen))
