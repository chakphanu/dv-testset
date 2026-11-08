# SPDX-License-Identifier: MIT
"""INFO1 acceptance (DV-Router): the public info is built from the configuration file the launcher loaded, not from a guessed
<state>/config path. A room configured with a name ([room.B] name = "Example room", rig/config/dv-router/room-name.toml) must
appear in GET /api/v1/public/state as info.rooms[] {room: "B", name: "Example room"} (an array, not a map).

Expected to FAIL on images before INFO1 in the rig shape (the launcher runs with --config /etc/dv-router/dv-router.toml and
<state>/config/ is empty, so dv-svc-web falls back to the live data and has no names); expected to PASS after INFO1.
Run with the web profile and the snippet: DVT_SUT_CONFIG_EXTRA=...web.toml:...:rig/config/dv-router/room-name.toml
scripts/run.sh -m rig --profile web -- cases/test_web_info_names.py. Skips with the reason when the snippet is not in the
rendered configuration of the run.
"""
import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.rig

ROOM = "B"
NAME = "Example room"


def rendered_room_name(target):
    path = Path(target.out) / "xlxd-config" / "xlxd-sut" / "dv-router.toml"
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ((doc.get("room") or {}).get(ROOM) or {}).get("name")


def info_room_entry(state, room):
    """The info.rooms[] entry of `room` in a public state document, or None (info.rooms is an array of {room, name})."""
    rooms = ((state or {}).get("info") or {}).get("rooms")
    if not isinstance(rooms, list):
        return None
    for entry in rooms:
        if isinstance(entry, dict) and entry.get("room") == room:
            return entry
    return None


def test_info_names_the_configured_room(target, web_client):
    if rendered_room_name(target) != NAME:
        pytest.skip("rig/config/dv-router/room-name.toml is not in the rendered configuration of this run (DVT_SUT_CONFIG_EXTRA)")
    state = json.loads(web_client().get("/api/v1/public/state", expect=200).body)
    info = state.get("info") or {}
    print("[RECORD] info-names: info keys %s; info.rooms %s" % (sorted(info), json.dumps(info.get("rooms"))[:400]))
    entry = info_room_entry(state, ROOM)
    assert entry is not None, "info.rooms has no entry for room %s (info.rooms = %s): INFO1, the public info does not come from " \
                              "the file the launcher loaded" % (ROOM, json.dumps(info.get("rooms"))[:300])
    assert entry.get("name") == NAME, "info.rooms[room=%s].name is %r, want %r (configured in [room.%s])" % (ROOM, entry.get("name"), NAME, ROOM)
