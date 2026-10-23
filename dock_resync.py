#!/usr/bin/env python3
"""
dock_resync.py - re-plug a docked iPod so new music syncs without unplugging.

After a sync the iPod is ejected but stays on USB (charging); only its disk
is gone. When the prepare job finishes a batch that changes the plan, it
leaves a request file (request()); musicdock-resync.path then runs this as
root. It tells the USB port to drop the iPod and take it back (the device's
`authorized` flag, 0 then 1: no power cut, so charging carries on). The
iPod comes back as a disk, udev fires as for a real plug-in, and the normal
sync runs.

It only re-plugs an iPod the dock has synced before (by FireWire GUID =
USB serial), whose disk is ejected, while no sync is running. Whether it
opted in (MediaDock.txt) is checked by the sync itself, as always.
"""

import glob
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

log = logging.getLogger("dock_resync")

SHARED_DIR = "/var/lib/musicdock"
REQUEST = os.path.join(SHARED_DIR, "resync.request")
STATE_FILE = os.path.join(SHARED_DIR, "dock_status.json")
DOCK_STATUS = os.path.join(HERE, "dock_status.py")
RECORD = os.path.join(SHARED_DIR, "dock.db")
USB_DEVICES = "/sys/bus/usb/devices"
BLOCK = "/sys/class/block"
APPLE = "05ac"
REPLUG_PAUSE = 2              # seconds between dropping the iPod and taking it back
SYNC_START_WAIT = 60          # seconds for the sync to take over the screen
# CONNECTING may replace any resting screen, never SYNCING or the boot screens.
CONNECTING_OVER = ("none,idle,preparing,safe,failed,notsynced,wifi,wififail,setup,"
                   "setupfail")


def request(path=REQUEST):
    """Ask for a re-plug (called by the prepare job, as the login user)."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".resync.")
    with os.fdopen(fd, "w") as f:
        f.write(f"{int(time.time())}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def docked_apple_devices(usb_root=USB_DEVICES):
    """[(sysfs dir, serial)] for Apple USB devices that report a serial."""
    found = []
    for d in sorted(glob.glob(os.path.join(usb_root, "*"))):
        if read(os.path.join(d, "idVendor")) == APPLE:
            serial = read(os.path.join(d, "serial"))
            if serial:
                found.append((d, serial))
    return found


def known_players(record=RECORD):
    """FireWire GUIDs of players the dock has synced (record opened read-only)."""
    if not os.path.exists(record):
        return set()
    db = sqlite3.connect(f"file:{record}?mode=ro", uri=True, timeout=10)
    try:
        return {r[0].upper() for r in
                db.execute("SELECT guid FROM players WHERE last_sync IS NOT NULL")}
    finally:
        db.close()


def disk_present(device_dir, block_root=BLOCK):
    """True if the device currently offers a disk with media (not ejected).
    An ejected iPod keeps its block device, with size 0."""
    # Look from the disks' side: walking down /sys from the USB device would
    # follow its many links back up the tree.
    device = os.path.realpath(device_dir) + os.sep
    for blk in glob.glob(os.path.join(block_root, "*")):
        if os.path.realpath(blk).startswith(device):
            size = read(os.path.join(blk, "size"))
            if size and size != "0":
                return True
    return False


def draw(*argv):
    """One screen, in a short-lived dock_status.py (it holds the GPIO pins
    until it exits). A failed draw is logged; the re-plug goes ahead."""
    r = subprocess.run([sys.executable, DOCK_STATUS, *argv])
    if r.returncode != 0:
        log.warning("Couldn't draw %s (dock_status.py exit %d)", argv[0], r.returncode)


def screen_state(path=STATE_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wait_for_sync(state=screen_state, sleep=time.sleep, clock=time.monotonic,
                  timeout=SYNC_START_WAIT):
    """True once something replaced CONNECTING (normally the sync's SYNCING)."""
    deadline = clock() + timeout
    while clock() < deadline:
        if state().get("screen") != "connecting":
            return True
        sleep(2)
    return state().get("screen") != "connecting"


def replug(device_dir, sleep=time.sleep):
    flag = os.path.join(device_dir, "authorized")
    with open(flag, "w") as f:
        f.write("0")
    sleep(REPLUG_PAUSE)
    with open(flag, "w") as f:
        f.write("1")


def resync(usb_root=USB_DEVICES, block_root=BLOCK, record=RECORD,
           sync_running=None, sleep=time.sleep, show=draw, state=screen_state,
           clock=time.monotonic):
    """Re-plug every eligible docked iPod. Returns how many were re-plugged.

    Shows CONNECTING first, so the dock visibly does something and the
    unplug rule (which fires as the iPod drops off USB) doesn't redraw
    DOCK READY in between. If no sync takes over within a minute (the iPod
    didn't come back), the previous screen is put back with its own time.
    """
    if sync_running is None:
        from dock_prepare import sync_running
    if sync_running():
        log.info("A sync is running; nothing to do")
        return 0
    known = known_players(record)
    replugged = 0
    for device_dir, serial in docked_apple_devices(usb_root):
        if serial.upper() not in known:
            log.info("iPod %s: not one the dock syncs; leaving it alone", serial)
        elif disk_present(device_dir, block_root):
            log.info("iPod %s: its disk is already up; leaving it alone", serial)
        else:
            log.info("iPod %s is docked and new music is ready: re-plugging it", serial)
            before = state()
            show("connecting", "--only-over", CONNECTING_OVER)
            replug(device_dir, sleep)
            replugged += 1
            if not wait_for_sync(state, sleep, clock):
                log.warning("iPod %s didn't come back for a sync; restoring the screen",
                            serial)
                argv = before.get("argv") or ["idle"]
                at = ["--at", str(before["drawn_at"])] if before.get("drawn_at") else []
                show(*argv, *at, "--only-over", "connecting")
    if not replugged:
        log.info("No docked iPod to sync")
    return replugged


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        os.remove(REQUEST)                 # first, so the path unit can't loop
    except FileNotFoundError:
        pass
    resync()


if __name__ == "__main__":
    main()
