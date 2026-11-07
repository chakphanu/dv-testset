#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""DVSwitch Mobile (Android app) against dv-edge-allstar, RADIO mode (docs/asl-dvswitch.md), host side.

Prerequisites, all local and none committed: the asl-edge rig is up (`scripts/run.sh --profile asl-edge --keep
...` with a dv-router image), an Android emulator AVD with the app installed and one account saved as in the
doc (host <net>.11, username acct, the fixture password, CallerID N0TEST, node 2999, Phone mode OFF), and
    DVT_ANDROID_SDK   SDK root (emulator, platform-tools)
    DVT_AVD_HOME      ANDROID_AVD_HOME with the AVD       DVT_AVD_NAME   AVD name (default dvt_dvswitch)
    DVT_EMU_PORT      console port (default 5570)         DVT_COMPOSE    compose prefix (default below)
The emulator is only ever addressed as emulator-<port>; nothing is installed or typed anywhere else.

Flow: boot the emulator (no wipe) if it is not up, start the app, dismiss its start-up dialogs, open the
Dialer, start the capture on the edge (dvtest.tools.dvswitch_capture, node A linked), tap Connect, hold PTT
3.5 s, tap Hangup, stop the capture. PASS needs: ANSWER, AUTHREP (md5), RADIO_KEY from the app and more than
20000 bytes of ulaw relayed by the edge to node A. Prints one line per check and exits 0 / 1.
"""
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

SDK = os.environ.get("DVT_ANDROID_SDK") or os.environ.get("ANDROID_SDK_ROOT") or ""
AVD_HOME = os.environ.get("DVT_AVD_HOME", "")
AVD = os.environ.get("DVT_AVD_NAME", "dvt_dvswitch")
PORT = os.environ.get("DVT_EMU_PORT", "5570")
SERIAL = "emulator-" + PORT
COMPOSE = os.environ.get("DVT_COMPOSE", "docker compose -p dvt -f rig/compose.yml --env-file .env.example "
                         "--env-file .env --profile core --profile asl-lab").split()
SCENARIO = os.environ.get("DVT_DVSWITCH_SCENARIO", "dvswitch-radio-mode")
UI = "/tmp/dvt-dvswitch-ui.xml"


def adb(*args, check=False):
    return subprocess.run([SDK + "/platform-tools/adb", "-s", SERIAL] + list(args), capture_output=True, text=True, check=check).stdout


def booted():
    out = subprocess.run([SDK + "/platform-tools/adb", "devices"], capture_output=True, text=True).stdout
    return any(ln.startswith(SERIAL) and ln.split()[1] == "device" for ln in out.splitlines()) \
        and adb("shell", "getprop", "sys.boot_completed").strip() == "1"


def boot():
    if booted():
        return
    env = dict(os.environ, ANDROID_AVD_HOME=AVD_HOME, ANDROID_SDK_ROOT=SDK)
    subprocess.Popen([SDK + "/emulator/emulator", "-avd", AVD, "-port", PORT, "-no-window", "-no-audio",
                      "-no-snapshot-save", "-no-boot-anim", "-memory", "2048", "-cores", "2", "-gpu",
                      "swiftshader_indirect", "-no-metrics"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if booted():
            return
        time.sleep(5)
    raise SystemExit("emulator did not boot")


def dump():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    subprocess.run([SDK + "/platform-tools/adb", "-s", SERIAL, "pull", "/sdcard/ui.xml", UI], capture_output=True)
    return ET.parse(UI)


def centre(node):
    b = list(map(int, node.get("bounds").replace("][", ",").strip("[]").split(",")))
    return (b[0] + b[2]) // 2, (b[1] + b[3]) // 2


def tap(x, y):
    adb("shell", "input", "tap", str(x), str(y))


def dismiss_dialogs():
    """Answer the app's start-up dialogs: deny location/notifications, OK the battery notice, allow the microphone."""
    pick = ("permission_allow_foreground_only_button", "permission_deny_and_dont_ask_again_button", "permission_deny_button")
    for _ in range(8):
        tree = dump()
        ids = {n.get("resource-id", "").split("/")[-1]: n for n in tree.iter("node")}
        text = {n.get("text"): n for n in tree.iter("node") if n.get("text")}
        msg = (ids.get("permission_message") is not None and ids["permission_message"].get("text")) or ""
        if "record audio" in msg and "permission_allow_foreground_only_button" in ids:
            tap(*centre(ids["permission_allow_foreground_only_button"]))
        elif ids.get("permission_message") is not None:
            for key in pick[1:]:
                if key in ids:
                    tap(*centre(ids[key]))
                    break
        elif "alertTitle" in ids and "button1" in ids:
            tap(*centre(ids["button2" if "button2" in ids and "Deny" in (ids["button2"].get("text") or "") else "button1"]))
        elif "OK" in text and "org.dvswitch" in adb("shell", "dumpsys", "window").split("mCurrentFocus", 1)[-1][:200]:
            tap(*centre(text["OK"]))
        else:
            return
        time.sleep(2)


def line1():
    for n in dump().iter("node"):
        if n.get("resource-id", "").endswith("/digital_line1"):
            return n.get("text") or ""
    return ""


def main():
    if not SDK or not AVD_HOME:
        print("set DVT_ANDROID_SDK and DVT_AVD_HOME", file=sys.stderr)
        return 2
    out = subprocess.run(COMPOSE + ["exec", "-T", "tester", "printenv", "DVT_OUT"], capture_output=True, text=True).stdout.strip()
    if not out:
        print("rig not up (no tester)", file=sys.stderr)
        return 2
    host_out = os.path.join("out", os.path.basename(out))
    boot()
    for _ in range(4):                              # a dialog's OK can land in a system settings page: back out, relaunch
        adb("shell", "am", "force-stop", "org.dvswitch")
        adb("shell", "monkey", "-p", "org.dvswitch", "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(6)
        dismiss_dialogs()
        if "org.dvswitch" in adb("shell", "dumpsys", "window") .split("mCurrentFocus", 1)[-1][:200]:
            break
        adb("shell", "input", "keyevent", "4")
    tap(140, 147)                                   # Dialer tab
    time.sleep(1)
    cap = subprocess.Popen(COMPOSE + ["exec", "-T", "tester", "python", "-m", "dvtest.tools.dvswitch_capture", SCENARIO, "--link-a"],
                           stdout=subprocess.PIPE, text=True)
    assert cap.stdout.readline().strip() == "capturing"
    status = ""
    for _ in range(3):                              # the first Connect after a start-up is sometimes swallowed
        tap(130, 420)                               # Connect
        time.sleep(7)
        status = line1()
        if "Node: 2999" in status:
            break
    adb("shell", "input", "swipe", "540", "2221", "540", "2221", "3500")   # PTT held
    time.sleep(1.5)
    tap(940, 420)                                   # Hangup
    time.sleep(3)
    open(os.path.join(host_out, "dvswitch-stop-" + SCENARIO), "w").close()
    verdict = json.loads(cap.stdout.read().strip().splitlines()[-1])
    cap.wait()
    s = verdict["summary"]
    checks = [("app shows the node (connected)", "Node: 2999" in status),
              ("ANSWER", s.get("control.ANSWER", 0) >= 1),
              ("AUTHREP md5", s.get("iax.AUTHREP", 0) >= 1),
              ("RADIO_KEY from the app", s.get("control.RADIO_KEY", 0) >= 1),
              ("edge relayed >20000 bytes ulaw to A", verdict["edge_to_a_ulaw_bytes"] > 20000)]
    for label, ok in checks:
        print("%s %s" % ("PASS" if ok else "FAIL", label))
    print("summary", json.dumps(s, sort_keys=True), "status line:", status)
    return 0 if all(ok for _l, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
