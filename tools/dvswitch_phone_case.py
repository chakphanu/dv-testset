#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""DVSwitch Mobile (Android app) against dv-edge-allstar, PHONE mode (docs/asl-dvswitch.md), host side. For dv-router:wip-c165c93
and later (ASL1g: the edge answers phone-mode IAX2 registration); on wip-7efdd33 / wip-71ab207 it ends in REGREJ and this case
FAILS by design (that is the old answer, docs/asl-dvswitch.md).

Prerequisites, as tools/dvswitch_radio_case.py (same environment variables, same emulator addressing, nothing committed), except
that the account saved in the app has "Phone mode IAX2 connection" ON (host <net>.11, username acct, the fixture password,
CallerID N0TEST, node 1999). The app registers by itself when it starts, so the capture on the edge is started BEFORE the app is
(re)started.

Expected on the wire (APP_REGISTRATION below; observed with DVSwitch Mobile 2.0.7 against wip-c165c93): the bare NEW probe is
answered with a CALLTOKEN and the app's first REGREQ already carries that token, then REGAUTH, REGREQ with MD5_RESULT, REGACK. (The
Asterisk-style order, REGREQ with an empty token first, is what cases R8-R12 send: dvtest.iax2_frames.PHONE_REGISTRATION.) Expected in
the app: the Status tab says "Registration: Registered", the Dialer's
Connect (send) button enabled, then a call that works like radio mode: ANSWER, AUTHREP (md5), RADIO_KEY from the app and more than
20000 bytes of ulaw relayed by the edge to node A. One PASS / FAIL line per check, exit 0 / 1.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dvswitch_radio_case as radio  # noqa: E402

from dvtest import iax2_frames  # noqa: E402

SCENARIO = os.environ.get("DVT_DVSWITCH_SCENARIO", "dvswitch-phone-mode")
TABS = {"Dialer": (140, 147), "Status": (405, 147), "Accounts": (675, 147)}     # the tab bar (title row, 1080 x 2400 emulator)
TAB_MARK = {"Dialer": "send_button", "Status": "status_text", "Accounts": "accounts"}     # a resource id that only that tab has
# DVSwitch Mobile 2.0.7 does not open with a REGREQ: its bare NEW probe gets a CALLTOKEN, and its first REGREQ already carries that
# token (observed on dv-router:wip-c165c93): CALLTOKEN, REGREQ with the token, REGAUTH, REGREQ with MD5_RESULT, REGACK
APP_REGISTRATION = ("CALLTOKEN", "REGREQ:token", "REGAUTH", "REGREQ:md5", "REGACK")


def registered_text(texts):
    """True when the app shows the registered state: the Status tab's "Registration: Registered" (or a bare "Registered"), never
    "Unregistered" or "Registering..."."""
    for text in texts:
        low = text.strip().lower()
        if low.startswith("registration:"):
            low = low.split(":", 1)[1].strip()
        if low == "registered" or low.startswith("registered "):
            return True
    return False


def texts_now():
    return [n.get("text") for n in radio.dump().iter("node") if n.get("text")]


def open_tab(name):
    """Tap the tab and check that its own screen is shown (a resource id only it has); True / False. A start-up dialog that is
    still up is answered first."""
    for y in (147, 192, 120, 147):
        radio.dismiss_dialogs()
        radio.tap(TABS[name][0], y)
        time.sleep(2.5)
        if TAB_MARK[name] in {n.get("resource-id", "").split("/")[-1] for n in radio.dump().iter("node")}:
            return True
    return False


def connect_enabled():
    """The Dialer's send button (the app's Connect) as the UI dump says; None when the Dialer is not shown."""
    if not open_tab("Dialer"):
        return None
    for n in radio.dump().iter("node"):
        if n.get("resource-id", "").endswith("/send_button"):
            return n.get("enabled") == "true"
    return None


def connected_text():
    """The Status tab's connection line (`connected_node_status_text`), "" when it cannot be read."""
    if not open_tab("Status"):
        return ""
    for n in radio.dump().iter("node"):
        if n.get("resource-id", "").endswith("/connected_node_status_text"):
            return n.get("text") or ""
    return ""


def wait_registered(timeout_s=90.0):
    """Read the Status tab until it says "Registration: Registered"; True / False."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if open_tab("Status") and registered_text(texts_now()):
            return True
        time.sleep(3)
    return False


def launch_app():
    """(Re)start the app in front, answering its start-up dialogs; a dialog's OK can land in a system settings page, so back out
    and relaunch (as in tools/dvswitch_radio_case.py)."""
    for _ in range(5):
        radio.adb("shell", "am", "force-stop", "org.dvswitch")
        radio.adb("shell", "monkey", "-p", "org.dvswitch", "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(6)
        radio.dismiss_dialogs()
        time.sleep(3)
        radio.dismiss_dialogs()                                     # the location notice comes a little later
        ids = {n.get("resource-id", "").split("/")[-1] for n in radio.dump().iter("node")}
        if "tabhost" in ids or "accounts" in ids:                   # the app's own screen is in front
            return True
        radio.adb("shell", "input", "keyevent", "4")
    return False


def open_accounts():
    """Open the Accounts list; True / False."""
    return open_tab("Accounts")


def set_phone_mode(on=True):
    """Open the first account in the Accounts tab, set the "Phone mode IAX2 connection" checkbox to `on` and Save (resource ids
    phone_mode / save of the account editor). Returns the checkbox state after the call, None when the editor did not open."""
    if not open_accounts():
        return None
    radio.tap(520, 215)                                             # the first account row
    time.sleep(2)
    nodes = {n.get("resource-id", "").split("/")[-1]: n for n in radio.dump().iter("node") if n.get("resource-id")}
    box = nodes.get("phone_mode")
    if box is None or "save" not in nodes:
        return None
    if (box.get("checked") == "true") != on:
        radio.tap(*radio.centre(box))
        time.sleep(1)
        box = {n.get("resource-id", "").split("/")[-1]: n for n in radio.dump().iter("node")}.get("phone_mode", box)
    state = box.get("checked") == "true"
    radio.tap(*radio.centre(nodes["save"]))
    time.sleep(2)
    return state


def main():
    if not radio.SDK or not radio.AVD_HOME:
        print("set DVT_ANDROID_SDK and DVT_AVD_HOME", file=sys.stderr)
        return 2
    out = subprocess.run(radio.COMPOSE + ["exec", "-T", "tester", "printenv", "DVT_OUT"], capture_output=True, text=True).stdout.strip()
    if not out:
        print("rig not up (no tester)", file=sys.stderr)
        return 2
    host_out = os.path.join("out", os.path.basename(out))
    radio.boot()
    launch_app()
    if set_phone_mode(True) is not True:                            # the account must be in phone mode: set it in the app first
        launch_app()
        if set_phone_mode(True) is not True:
            print("could not set Phone mode IAX2 connection in the app", file=sys.stderr)
            return 2
    radio.adb("shell", "am", "force-stop", "org.dvswitch")          # the registration must happen inside the capture
    cap = subprocess.Popen(radio.COMPOSE + ["exec", "-T", "tester", "python", "-m", "dvtest.tools.dvswitch_capture", SCENARIO,
                                            "--link-a"], stdout=subprocess.PIPE, text=True)
    assert cap.stdout.readline().strip() == "capturing"
    launch_app()
    registered = wait_registered()
    enabled = connect_enabled()
    status = ""
    for _ in range(3):                                              # the first Connect after a start-up is sometimes swallowed
        if open_tab("Dialer"):
            radio.tap(130, 420)                                     # Connect
        time.sleep(7)
        status = radio.line1()
        if "Node: 1999" in status or connected_text() not in ("", "Not connected"):
            break
    status = status + " | status tab: " + connected_text()
    open_tab("Dialer")
    radio.adb("shell", "input", "swipe", "540", "2221", "540", "2221", "3500")      # PTT held
    time.sleep(1.5)
    radio.tap(940, 420)                                             # Hangup
    time.sleep(3)
    open(os.path.join(host_out, "dvswitch-stop-" + SCENARIO), "w").close()
    verdict = json.loads(cap.stdout.read().strip().splitlines()[-1])
    cap.wait()
    s = verdict["summary"]
    steps = verdict.get("registration", [])
    reg_ok, why = iax2_frames.judge_phone_registration(steps, APP_REGISTRATION)
    checks = [("REGREQ seen (Phone mode IAX2 connection is on in the app)", any(x.startswith("REGREQ") for x in steps)),
              ("registration %s" % " -> ".join(APP_REGISTRATION) + ("" if reg_ok else " [%s]" % why), reg_ok),
              ("app shows Registered", registered),
              ("Connect (send) button enabled", enabled is True),
              ("app shows the node (connected)", "Node: 1999" in status or "Connected" in status.split("| status tab:")[-1]),
              ("ANSWER", s.get("control.ANSWER", 0) >= 1),
              ("AUTHREP md5", s.get("iax.AUTHREP", 0) >= 1),
              ("RADIO_KEY from the app", s.get("control.RADIO_KEY", 0) >= 1),
              ("edge relayed >20000 bytes ulaw to A", verdict["edge_to_a_ulaw_bytes"] > 20000)]
    for label, ok in checks:
        print("%s %s" % ("PASS" if ok else "FAIL", label))
    print("registration steps", steps)
    print("summary", json.dumps(s, sort_keys=True), "status line:", status)
    return 0 if all(ok for _l, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
