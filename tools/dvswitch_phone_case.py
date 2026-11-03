#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""DVSwitch Mobile (Android app) against dv-edge-allstar, PHONE mode (docs/asl-dvswitch.md), host side. For dv-router:wip-c165c93
and later (ASL1g: the edge answers phone-mode IAX2 registration); on wip-7efdd33 / wip-71ab207 it ends in REGREJ and this case
FAILS by design (that is the old answer, docs/asl-dvswitch.md).

Prerequisites, as tools/dvswitch_radio_case.py (same environment variables, same emulator addressing, nothing committed), except
that the account saved in the app has "Phone mode IAX2 connection" ON (host <net>.11, username acct, the fixture password,
CallerID N0TEST, node 1999). The app registers by itself when it starts, so the capture on the edge is started BEFORE the app is
(re)started.

Expected on the wire (dvtest.iax2_frames.PHONE_REGISTRATION): REGREQ -> CALLTOKEN -> REGREQ with the token -> REGAUTH ->
REGREQ with MD5_RESULT -> REGACK. Expected in the app: "Registered" somewhere on the Status or Accounts tab, the Dialer's
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


def registered_text(texts):
    """True when the app shows the registered state: a text that says "Registered" (not "Unregistered")."""
    for text in texts:
        low = text.strip().lower()
        if low == "registered" or low.startswith("registered "):
            return True
    return False


def texts_now():
    return [n.get("text") for n in radio.dump().iter("node") if n.get("text")]


def connect_enabled():
    """The Dialer's send button (the app's Connect) as the UI dump says; None when the Dialer is not shown."""
    for n in radio.dump().iter("node"):
        if n.get("resource-id", "").endswith("/send_button"):
            return n.get("enabled") == "true"
    return None


def wait_registered(timeout_s=90.0):
    """Walk the Status and Accounts tabs until a "Registered" text shows; True / False."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        for tab in ("Status", "Accounts"):
            radio.tap(*TABS[tab])
            time.sleep(1.5)
            if registered_text(texts_now()):
                return True
        time.sleep(3)
    return False


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
    radio.adb("shell", "am", "force-stop", "org.dvswitch")          # the registration must happen inside the capture
    cap = subprocess.Popen(radio.COMPOSE + ["exec", "-T", "tester", "python", "-m", "dvtest.tools.dvswitch_capture", SCENARIO,
                                            "--link-a"], stdout=subprocess.PIPE, text=True)
    assert cap.stdout.readline().strip() == "capturing"
    for _ in range(4):
        radio.adb("shell", "monkey", "-p", "org.dvswitch", "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(6)
        radio.dismiss_dialogs()
        if "org.dvswitch" in radio.adb("shell", "dumpsys", "window").split("mCurrentFocus", 1)[-1][:200]:
            break
        radio.adb("shell", "input", "keyevent", "4")
        radio.adb("shell", "am", "force-stop", "org.dvswitch")
    registered = wait_registered()
    radio.tap(*TABS["Dialer"])
    time.sleep(1.5)
    enabled = connect_enabled()
    status = ""
    for _ in range(3):                                              # the first Connect after a start-up is sometimes swallowed
        radio.tap(130, 420)                                         # Connect
        time.sleep(7)
        status = radio.line1()
        if "Node: 1999" in status:
            break
    radio.adb("shell", "input", "swipe", "540", "2221", "540", "2221", "3500")      # PTT held
    time.sleep(1.5)
    radio.tap(940, 420)                                             # Hangup
    time.sleep(3)
    open(os.path.join(host_out, "dvswitch-stop-" + SCENARIO), "w").close()
    verdict = json.loads(cap.stdout.read().strip().splitlines()[-1])
    cap.wait()
    s = verdict["summary"]
    steps = verdict.get("registration", [])
    reg_ok, why = iax2_frames.judge_phone_registration(steps)
    checks = [("REGREQ seen (Phone mode IAX2 connection is on in the app)", any(x.startswith("REGREQ") for x in steps)),
              ("registration %s" % " -> ".join(iax2_frames.PHONE_REGISTRATION) + ("" if reg_ok else " [%s]" % why), reg_ok),
              ("app shows Registered", registered),
              ("Connect (send) button enabled", enabled is True),
              ("app shows the node (connected)", "Node: 1999" in status),
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
