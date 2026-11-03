# SPDX-License-Identifier: MIT
"""tools/dvswitch_phone_case.py: the pure parts (no emulator, no rig)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import dvswitch_phone_case as phone  # noqa: E402

pytestmark = pytest.mark.unit


def test_registered_text_is_not_unregistered():
    assert phone.registered_text(["Accounts", "Registered"])
    assert phone.registered_text(["Registered (refresh 60 s)"])
    assert not phone.registered_text(["Unregistered", "Dialer"])
    assert not phone.registered_text([])
    assert not phone.registered_text(["Registering..."])
    assert phone.registered_text(["Registration: Registered", "Not connected"])
    assert not phone.registered_text(["Registration: Unregistered"])


def test_app_registration_order_is_what_the_app_sends():
    from dvtest import iax2_frames
    seen = ["CALLTOKEN", "CALLTOKEN", "REGREQ:token", "REGAUTH", "REGREQ:md5", "REGACK", "CALLTOKEN", "REGREQ:token", "REGAUTH",
            "REGREQ:md5", "REGACK"]
    assert iax2_frames.judge_phone_registration(seen, phone.APP_REGISTRATION)[0]
    assert not iax2_frames.judge_phone_registration(["CALLTOKEN", "REGREQ:token", "REGAUTH", "REGREQ:md5", "REGREJ"],
                                                    phone.APP_REGISTRATION)[0]


def test_tabs_are_on_the_tab_row():
    assert set(phone.TABS) == {"Dialer", "Status", "Accounts"}
    assert all(y == 147 for _x, y in phone.TABS.values())
