# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_mmdvm_stack.py (docs/mmdvm-stack-cases.md): which gateway row of the public state belongs to which mode,
and which modes the reflector under test offers at all. No rig needed to test them (cases/unit/test_mmdvm_stack_lib.py)."""


def row_matches(row, mode, room, words):
    """True when a client row of the public state is the gateway of `mode`: in module `room` (one module, or a collection of
    modules: YSF is linked to the autolink module B at its first poll and moves to the module of its DG-ID with the first frame) and
    one of `words` in its protocol / edge / kind text."""
    rooms = {room} if isinstance(room, str) else set(room)
    row_room = str(row.get("room") or row.get("module") or "")
    text = " ".join(str(row.get(k, "")) for k in ("protocol", "edge", "kind", "proto")).lower()
    return row_room in rooms and any(word in text for word in words[mode])


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


def needs_prime(last_activity, now, valid_s):
    """True when the gateway of a mode has to be switched to the reflector by a short key-up before a byte-exact check: it has had no
    RF activity yet (last_activity None), or none within valid_s seconds. P25Gateway forwards nothing from RF until the first 0x65
    record of a call has switched it to the talk group (Static= only polls the reflector) and forgets that after its hang time
    (RFHangTime 120 s, NetHangTime 60 s), so the first three voice records (0x62..0x64) of such a call never leave the gateway."""
    return last_activity is None or now - last_activity > valid_s
