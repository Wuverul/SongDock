#!/usr/bin/env python3
"""
dock_usbport.py - say so on screen when a failed plug-in leaves the USB
port dead.

When a device fails to connect (a loose plug: "device descriptor read/64,
error -71"), the kernel power-cycles the port ("attempt power cycle") and,
if that fails too, gives up. On 2026-10-25 the port then stayed dead:
nothing plugged in afterwards - not even a working iPod - was noticed until
a restart, while the screen still said DOCK READY. No status file shows
this (the port's `disable` reads 1 whenever nothing is plugged in), so the
kernel log is the signal.

musicdock-usbport.timer runs this as root every 30 seconds. It reads the
kernel messages since its last run (journal cursor kept in /run/musicdock)
and, if the port gave up and nothing has connected for STUCK_AFTER seconds
since, shows USB NOT WORKING ("Restart the dock"), once per failure and
never over a sync. It never touches the USB controller: rebinding the
dwc_otg driver to revive the port crashed the kernel (NULL pointer
dereference in dwc_otg_driver_remove, Raspberry Pi OS 6.18, 2026-10-25).

Uses the standard library only (runs under the system python3).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time

log = logging.getLogger("dock_usbport")

HERE = os.path.dirname(os.path.abspath(__file__))
DOCK_STATUS = os.path.join(HERE, "dock_status.py")
STATE = "/run/musicdock/usbport.json"
STUCK_AFTER = 60                 # seconds of silence after the kernel gave up
# Resting screens only: never over a sync, the boot screens or CONNECTING
# (a re-sync dropping the iPod on purpose).
SHOW_OVER = ("none,idle,preparing,safe,failed,notsynced,wifi,wififail,setup,"
             "setupfail")

# The kernel's last resorts for a device that won't connect...
GAVE_UP = re.compile(r"usb (usb\d+)-port\d+: (attempt power cycle|unable to enumerate USB device)")
# ...and signs the port works: a device connected, or a working one left.
WORKS = re.compile(r"usb (\d+)-[\d.]+: (New USB device found|USB disconnect)")


def read_kernel_log(cursor):
    """(lines as (unix time, text), new cursor) since `cursor` (this boot
    only when there's none)."""
    cmd = ["journalctl", "-k", "-o", "short-unix", "--no-pager", "--show-cursor"]
    cmd += ["--after-cursor", cursor] if cursor else ["-b"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    lines, new_cursor = [], cursor
    for line in out.splitlines():
        if line.startswith("-- cursor: "):
            new_cursor = line[len("-- cursor: "):].strip()
            continue
        stamp, _, text = line.partition(" ")
        try:
            lines.append((float(stamp), text))
        except ValueError:
            pass
    return lines, new_cursor


def show_screen():
    """USB NOT WORKING, in a short-lived dock_status.py (it holds the
    GPIO pins until it exits)."""
    r = subprocess.run([sys.executable, DOCK_STATUS, "usbstuck", "--only-over", SHOW_OVER],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("Couldn't draw USB NOT WORKING (dock_status.py exit %d)", r.returncode)


def sync_running():
    out = subprocess.run(["systemctl", "list-units", "--state=active,activating",
                          "--no-legend", "--plain", "musicdock-sync@*"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def check(state_path=STATE, now=None, read_log=read_kernel_log, show=show_screen,
          busy=sync_running):
    """One run. Returns the bus reported this time, or None."""
    now = time.time() if now is None else now
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    lines, state["cursor"] = read_log(state.get("cursor"))
    trouble = state.setdefault("trouble", {})        # bus -> {"since", "shown"}
    for stamp, text in lines:
        m = GAVE_UP.search(text)
        if m:
            trouble.setdefault(m.group(1), {"since": stamp, "shown": False})
            continue
        m = WORKS.search(text)
        if m:
            trouble.pop(f"usb{m.group(1)}", None)    # works again: forget the trouble
    reported = None
    for bus, t in sorted(trouble.items()):
        if t["shown"] or now - t["since"] < STUCK_AFTER or busy():
            continue
        log.warning("USB port on %s gave up on a device %d s ago and nothing has "
                    "connected since: players won't be noticed until the dock is "
                    "restarted (showing USB NOT WORKING)", bus, now - t["since"])
        show()
        t["shown"] = True
        reported = bus
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(state_path + ".tmp", state_path)
    except OSError:
        pass
    return reported


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
