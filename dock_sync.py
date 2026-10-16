#!/usr/bin/env python3
"""
dock_sync.py - sync job: runs when a player docks.

Phase 2 scope: find the USB storage device, show SYNCING, mount it, list its
contents, unmount cleanly, show SAFE. Later phases put the real work (play
upload, keep list, track copy) between mount and unmount.

Usage:
  sudo dock_sync.py                         # use the one USB filesystem plugged in
  sudo dock_sync.py --device /dev/sda1      # what the systemd service runs
  dock_sync.py --fake-device ./fake_ipod    # Mac: a folder stands in for the player
  dock_sync.py --fake-device ./fake_ipod --fake-fail unmount   # try failure paths
  dock_sync.py --fake-device ./fake_ipod --panel   # Pi: fake player, real screen

With --fake-device nothing is mounted, and screens are saved as numbered PNGs
in --preview-dir (default ./previews) instead of drawn on the panel.

Safety rules (from CLAUDE.md):
  * SYNCING is drawn before mounting. If it can't be drawn, we don't mount.
  * Once mounted, we always try to unmount, even if a step failed.
  * SAFE and FAILED both say "safe to remove", so they're only drawn after the
    unmount is confirmed. If unmount fails, SYNCING stays up with a warning.
  * Phase 2 mounts read-only: nothing on the player can change yet.
"""

import argparse
import fcntl
import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

log = logging.getLogger("dock_sync")

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNT_POINT = "/media/musicdock"
LOCK_FILE = os.path.join(tempfile.gettempdir(), "musicdock-sync.lock")
LIST_LIMIT = 200          # files logged by name; a full player would flood the log
UNMOUNT_TRIES = 3         # "target is busy" usually clears within a few seconds
TOTAL_STEPS = 3           # mount, read, unmount


class SyncError(Exception):
    """A failure with a short message that fits on the e-ink screen."""


def run(cmd, short):
    """Run a command; on failure log the full error and raise `short`."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("%s: %s", " ".join(cmd), (r.stderr or r.stdout).strip())
        raise SyncError(short)


# ------------------------------------------------------------- detection ----
def find_usb_filesystems(lsblk_data=None):
    """Return USB block devices that hold a filesystem.

    Each item is {"path": "/dev/sda1", "fstype": "vfat", "mountpoint": None}.
    lsblk_data is parsed `lsblk --json` output; tests pass a sample instead.
    """
    if lsblk_data is None:
        out = subprocess.run(
            ["lsblk", "--json", "--paths", "-o", "NAME,TRAN,FSTYPE,MOUNTPOINT"],
            check=True, capture_output=True, text=True).stdout
        lsblk_data = json.loads(out)

    found = []

    def walk(dev, tran):
        tran = dev.get("tran") or tran     # partitions inherit the disk's bus
        if tran == "usb" and dev.get("fstype"):
            found.append({"path": dev["name"], "fstype": dev["fstype"],
                          "mountpoint": dev.get("mountpoint")})
        for child in dev.get("children", []):
            walk(child, tran)

    for dev in lsblk_data.get("blockdevices", []):
        walk(dev, None)
    return found


def detect_one():
    """Pick the single unmounted USB filesystem, or explain why we can't."""
    devices = [d for d in find_usb_filesystems() if not d["mountpoint"]]
    if not devices:
        raise SyncError("No USB storage device found")
    if len(devices) > 1:
        names = ", ".join(d["path"] for d in devices)
        raise SyncError(f"Several USB devices ({names}); pick one with --device")
    log.info("Detected %s (%s)", devices[0]["path"], devices[0]["fstype"])
    return devices[0]["path"]


# --------------------------------------------------------------- devices ----
def mounted_source(mount_point):
    """Return the device mounted at mount_point, or None."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                source, target = line.split()[:2]
                if target == mount_point:
                    return source
    except OSError:
        pass
    return None


def is_mounted_anywhere(dev_path):
    real = os.path.realpath(dev_path)
    try:
        with open("/proc/mounts") as f:
            return any(os.path.realpath(line.split()[0]) == real for line in f)
    except OSError:
        return False


class BlockDevice:
    """A real USB partition on the Pi. mount/umount need root."""

    def __init__(self, path, mount_point=MOUNT_POINT):
        self.path = path
        self.root = mount_point
        self.mounted = False

    def mount(self):
        if not os.path.exists(self.path):
            raise SyncError("Player disconnected")
        if is_mounted_anywhere(self.path):
            raise SyncError(f"{self.path} already mounted elsewhere")
        os.makedirs(self.root, exist_ok=True)
        if mounted_source(self.root):
            raise SyncError(f"{self.root} already in use")
        log.info("Mounting %s at %s (read-only)", self.path, self.root)
        run(["mount", "-o", "ro,noatime", self.path, self.root],
            "Couldn't mount player")
        self.mounted = True

    def unmount(self):
        log.info("Flushing and unmounting %s", self.root)
        subprocess.run(["sync"])
        for attempt in range(1, UNMOUNT_TRIES + 1):
            try:
                run(["umount", self.root], "Couldn't unmount player")
                break
            except SyncError:
                if attempt == UNMOUNT_TRIES:
                    raise
                time.sleep(2)
        if mounted_source(self.root):
            raise SyncError("Player still mounted")
        self.mounted = False
        self._eject()

    def _eject(self):
        """Best effort: tells a stock iPod it may leave "Do not disconnect".

        Not a safety step (the unmount above is), so a missing `eject`
        command or an error is only logged.
        """
        if not shutil.which("eject"):
            log.info("`eject` not installed; skipping (sudo apt install eject)")
            return
        r = subprocess.run(["eject", self.path], capture_output=True, text=True)
        if r.returncode != 0:
            log.warning("eject %s: %s", self.path, r.stderr.strip())


class FakeDevice:
    """A folder standing in for the player, for testing on the Mac.

    fail="mount" or fail="unmount" makes that step fail, to try error paths.
    """

    def __init__(self, folder, fail=None):
        self.path = folder
        self.root = os.path.abspath(folder)
        self.fail = fail
        self.mounted = False

    def mount(self):
        if self.fail == "mount":
            raise SyncError("Couldn't mount player (fake)")
        if not os.path.isdir(self.root):
            raise SyncError(f"Fake device {self.path} not found")
        log.info("[fake] Would mount; using folder %s", self.root)
        self.mounted = True

    def unmount(self):
        if self.fail == "unmount":
            raise SyncError("Couldn't unmount player (fake)")
        log.info("[fake] Would unmount %s", self.root)
        self.mounted = False


# --------------------------------------------------------------- listing ----
def human(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def list_contents(root, limit=LIST_LIMIT):
    """Log the files under root (first `limit` by name) and return (count, bytes)."""
    count = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                size = os.lstat(full).st_size
            except OSError:
                continue
            count += 1
            total += size
            if count <= limit:
                log.info("  %s  (%s)", os.path.relpath(full, root), human(size))
    if count > limit:
        log.info("  ... and %d more", count - limit)
    log.info("Player holds %d files, %s", count, human(total))
    return count, total


# ---------------------------------------------------------------- screen ----
class Screen:
    """Draws via dock_status.py: on the panel, or as PNGs in preview_dir."""

    def __init__(self, preview_dir=None):
        self.preview_dir = preview_dir
        self.count = 0
        if preview_dir:
            os.makedirs(preview_dir, exist_ok=True)
            for old in glob.glob(os.path.join(preview_dir, "[0-9][0-9]-*.png")):
                os.remove(old)    # so a previous run's screens don't mix in

    def show(self, screen, *args, force=False):
        argv = [screen, *args] + (["--force"] if force else [])
        if self.preview_dir:
            self.count += 1
            argv += ["--preview",
                     os.path.join(self.preview_dir, f"{self.count:02d}-{screen}.png")]
        log.info("Screen: %s %s", screen.upper(), " ".join(args))
        try:
            sys.path.insert(0, HERE)
            import dock_status
        except ImportError:
            if self.preview_dir:   # e.g. Pillow not installed on the Mac
                log.warning("Pillow not installed: screen logged, not rendered")
                return
            raise                  # on the panel this is a real failure
        dock_status.main(argv)


# ------------------------------------------------------------------ sync ----
def run_sync(device, show):
    """Run one sync. show(screen, *args, force=False) draws a screen.

    Returns True if the player was synced and is safe to remove.
    """
    def step(n, detail):
        # Progress redraws are nice-to-have: SYNCING is already on screen,
        # so a panel hiccup here shouldn't abort the sync.
        try:
            show("syncing", "--step", str(n), "--total", str(TOTAL_STEPS),
                 "--detail", detail)
        except Exception:
            log.exception("Progress redraw failed (continuing)")

    # 1. SYNCING goes up before anything touches the player. --force skips
    #    rate limiting, so it's guaranteed to draw. If it can't be drawn,
    #    the user could unplug mid-sync, so we stop before mounting.
    try:
        show("syncing", "--step", "1", "--total", str(TOTAL_STEPS),
             "--detail", "Mounting player", force=True)
    except Exception:
        log.exception("Couldn't draw SYNCING; not mounting the player")
        return False

    error = None
    try:
        device.mount()
        step(2, "Reading player")
        list_contents(device.root)
        # Phase 3+: upload plays, read keep list, copy tracks - all here.
    except Exception as e:
        log.exception("Sync failed")
        error = str(e) or type(e).__name__

    # Always unmount if we mounted, even after an error.
    if device.mounted:
        step(3, "Unmounting")
        try:
            device.unmount()
        except Exception:
            log.exception("Unmount failed; leaving SYNCING on screen")
            try:
                show("syncing", "--detail", "Unmount failed - see log", force=True)
            except Exception:
                log.exception("Couldn't update screen")
            return False

    # Player is unmounted: now (and only now) it's safe to say so.
    try:
        if error:
            show("failed", "--reason", error, force=True)
        else:
            show("safe", force=True)
    except Exception:
        log.exception("Couldn't draw final screen; SYNCING stays up")
        return False
    return error is None


def acquire_lock(path=LOCK_FILE):
    """Return an open, locked file, or None if another sync holds the lock.

    The lock is released automatically when the process exits.
    """
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


# ------------------------------------------------------------------ main ----
def main(argv=None):
    p = argparse.ArgumentParser(description="Music dock sync job")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--device", help="block device, e.g. /dev/sda1 "
                     "(default: detect the one USB filesystem)")
    src.add_argument("--fake-device", metavar="DIR",
                     help="use a folder as the player (no mounting, no root)")
    p.add_argument("--fake-fail", choices=["mount", "unmount"],
                   help="with --fake-device: make this step fail")
    p.add_argument("--preview-dir", metavar="DIR",
                   help="save screens as PNGs here instead of drawing "
                        "(default with --fake-device: ./previews)")
    p.add_argument("--panel", action="store_true",
                   help="with --fake-device: draw on the real e-ink panel "
                        "instead of saving PNGs (on the Pi)")
    p.add_argument("--mount-point", default=MOUNT_POINT)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if (args.fake_fail or args.panel) and not args.fake_device:
        p.error("--fake-fail and --panel only work with --fake-device")
    if args.panel and args.preview_dir:
        p.error("choose --panel or --preview-dir, not both")

    if args.fake_device:
        device = FakeDevice(args.fake_device, fail=args.fake_fail)
        if args.panel:
            preview_dir = None
        else:
            preview_dir = args.preview_dir or os.path.join(HERE, "previews")
    else:
        if os.geteuid() != 0:
            p.error("mounting needs root: run with sudo, or use --fake-device")
        try:
            path = args.device or detect_one()
        except SyncError as e:
            log.error("%s", e)
            return 1
        device = BlockDevice(path, args.mount_point)
        preview_dir = args.preview_dir

    lock = acquire_lock()
    if lock is None:
        log.info("Another sync is already running; exiting")
        return 0

    ok = run_sync(device, Screen(preview_dir).show)
    if preview_dir:
        log.info("Screen previews in %s", preview_dir)
    log.info("Sync %s", "finished: safe to remove" if ok else "did not complete")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
