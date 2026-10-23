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
import importlib.util
import json
import logging
import os
import re
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
TOTAL_STEPS = 3           # listing only: mount, read, unmount
IPOD_STEPS = 6            # mount, plays, upload, backup, copy, unmount
REST_AFTER_MIN = 10       # SAFE -> DOCK READY if the player stays docked

# An iPod opts in to this dock with this file in its top folder (any case).
# Without it the dock touches nothing - no plays sent, no writes - so a
# friend's iPod docked by mistake isn't filled with this library or credited
# to this account. Players the dock already syncs get it created for them.
MARKER = "MediaDock.txt"
MARKER_TEXT = ("This iPod is synced by Music Dock "
               "(plays, tracks and playlists).\n"
               "Delete this file to stop syncing it.\n")


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

    def __init__(self, path, mount_point=MOUNT_POINT, writable=False):
        self.path = path
        self.root = mount_point
        self.writable = writable
        self.mounted = False

    def mount(self):
        if not os.path.exists(self.path):
            raise SyncError("Player disconnected")
        if is_mounted_anywhere(self.path):
            raise SyncError(f"{self.path} already mounted elsewhere")
        os.makedirs(self.root, exist_ok=True)
        if mounted_source(self.root):
            raise SyncError(f"{self.root} already in use")
        # Always read-only first; make_writable() remounts once the player
        # has been checked (it's an iPod and it opted in).
        log.info("Mounting %s at %s (read-only)", self.path, self.root)
        run(["mount", "-o", "ro,noatime", self.path, self.root], "Couldn't mount player")
        self.mounted = True

    def fstype(self):
        return subprocess.run(["lsblk", "-no", "FSTYPE", self.path],
                              capture_output=True, text=True).stdout.strip()

    def make_writable(self):
        fstype = self.fstype()
        # flush: vfat writes data out promptly - safer on a removable player.
        opts = "remount,rw,noatime,flush" if fstype == "vfat" else "remount,rw,noatime"
        log.info("Remounting %s read-write", self.root)
        run(["mount", "-o", opts, self.root], "Couldn't make the player writable")
        self.writable = True

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


def opt_in_status(root, known):
    """"not_ipod", "marked" (has MediaDock.txt), "known" (no marker, but the
    dock already syncs this player), or "unmarked"."""
    if not os.path.isdir(os.path.join(root, "iPod_Control")):
        return "not_ipod"
    try:
        names = {n.lower() for n in os.listdir(root)}
    except OSError:
        names = set()
    if MARKER.lower() in names:
        return "marked"
    return "known" if known else "unmarked"


def player_identity(dev_path):
    """(FireWire GUID, USB product ID) of a docked player, from udev.

    On iPods the USB serial number *is* the FireWire GUID that signs the
    database. Returns (None, None) if udev doesn't know the device.
    """
    r = subprocess.run(["udevadm", "info", "--query=property", f"--name={dev_path}"],
                       capture_output=True, text=True)
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    guid = props.get("ID_SERIAL_SHORT")
    try:
        product = int(props.get("ID_MODEL_ID", ""), 16)
    except ValueError:
        product = None
    return (guid.upper() if guid else None), product


def sysinfo_guid(root):
    """FirewireGuid from a player folder's SysInfo (fake devices), or None."""
    try:
        with open(os.path.join(root, "iPod_Control", "Device", "SysInfo")) as f:
            for line in f:
                key, _, val = line.partition(":")
                if key.strip() == "FirewireGuid":
                    return val.strip().upper().replace("0X", "")
    except OSError:
        pass
    return None


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

    def fstype(self):
        return "vfat"

    def make_writable(self):
        log.info("[fake] Would remount %s read-write", self.root)

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
DOCK_STATUS = os.path.join(HERE, "dock_status.py")


class Screen:
    """Draws via dock_status.py: on the panel, or as PNGs in preview_dir.

    Every draw runs in its own short-lived dock_status.py process. The
    Waveshare driver keeps the panel's GPIO pins (and a busy-looping
    pin-monitor thread) until the process that drew exits. Drawing inside a
    long-running job would block every other job from the panel ("GPIO
    busy") and waste CPU for as long as the job runs.
    """

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
        if self.preview_dir and importlib.util.find_spec("PIL") is None:
            log.warning("Pillow not installed: screen logged, not rendered")
            return                 # e.g. on the Mac; on the panel it's an error
        r = subprocess.run([sys.executable, DOCK_STATUS, *argv])
        if r.returncode != 0:
            raise SyncError(f"Screen {screen} failed (dock_status.py exit {r.returncode})")


# ------------------------------------------------------------------ sync ----
def run_sync(device, show, work=None, total=None):
    """Run one sync. show(screen, *args, force=False) draws a screen.

    work(device, step) does the player-specific part between mount and
    unmount (e.g. ipod_handler.sync_player) and returns a summary dict;
    without it the player is just listed (the Phase 2 check).
    Returns True if the player was synced and is safe to remove.
    """
    total = total or (IPOD_STEPS if work else TOTAL_STEPS)

    def step(n, detail, part_done=None, part_total=None):
        # Progress redraws are nice-to-have: SYNCING is already on screen,
        # so a panel hiccup here shouldn't abort the sync. part_done/total:
        # progress within this step (e.g. tracks copied) for the bar.
        part = (["--part-done", str(part_done), "--part-total", str(part_total)]
                if part_total else [])
        try:
            show("syncing", "--step", str(n), "--total", str(total),
                 "--detail", detail, *part)
        except Exception:
            log.exception("Progress redraw failed (continuing)")

    # 1. SYNCING goes up before anything touches the player. --force skips
    #    rate limiting, so it's guaranteed to draw. If it can't be drawn,
    #    the user could unplug mid-sync, so we stop before mounting.
    try:
        show("syncing", "--step", "1", "--total", str(total),
             "--detail", "Mounting player", force=True)
    except Exception:
        log.exception("Couldn't draw SYNCING; not mounting the player")
        return False

    error, summary = None, {}
    try:
        device.mount()
        if work:
            summary = work(device, step) or {}
        else:
            step(2, "Reading player")
            list_contents(device.root)
    except Exception as e:
        log.exception("Sync failed")
        error = str(e) or type(e).__name__

    # Always unmount if we mounted, even after an error.
    if device.mounted:
        step(total, "Unmounting")
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
        elif summary.get("not_synced"):
            show("notsynced", "--reason", summary["not_synced"], force=True)
        elif summary.get("wifi"):
            w = summary["wifi"]
            server = {True: "yes", False: "no"}.get(w.get("server"), "unknown")
            show("wifi" if w["ok"] else "wififail", "--ssid", w.get("ssid") or "",
                 "--reason", w.get("reason") or "", "--server", server,
                 "--added", str(summary.get("added", 0)),
                 "--removed", str(summary.get("removed", 0)),
                 "--plays", str(summary.get("plays_sent", 0)),
                 "--queued", str(summary.get("plays_queued", 0)), force=True)
        elif summary.get("setup"):
            import dock_setup
            s = summary["setup"]
            show("setup" if s["ok"] else "setupfail",
                 "--reason", dock_setup.summary_line(s),
                 "--added", str(summary.get("added", 0)),
                 "--removed", str(summary.get("removed", 0)),
                 "--plays", str(summary.get("plays_sent", 0)),
                 "--queued", str(summary.get("plays_queued", 0)), force=True)
        else:
            show("safe", "--added", str(summary.get("added", 0)),
                 "--removed", str(summary.get("removed", 0)),
                 "--plays", str(summary.get("plays_sent", 0)),
                 "--queued", str(summary.get("plays_queued", 0)), force=True)
    except Exception:
        log.exception("Couldn't draw final screen; SYNCING stays up")
        return False
    return error is None


def whole_disk(dev_path):
    """/dev/sda1 -> /dev/sda (SCSI inquiries go to the disk); else None."""
    m = re.fullmatch(r"(/dev/sd[a-z]+)\d*", dev_path or "")
    return m.group(1) if m else None


def make_ipod_work(cfg, guid, product, write):
    """The stock-iPod session for run_sync (needs the venv: pygpod)."""
    import ipod_handler
    from dock_record import SHARED_DIR, Record
    from subsonic import Client

    base = SHARED_DIR if os.path.isdir(SHARED_DIR) else os.path.expanduser("~/.cache/musicdock")
    cache_dir = cfg["sync"]["cache_dir"]

    def work(device, step):
        try:
            with open(os.path.join(cache_dir, "manifest.json")) as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            log.warning("No staged plan yet (prepare job hasn't run): plays only")
            manifest = {}
        record = Record()
        status = opt_in_status(device.root, record.player(guid) is not None)
        if status in ("not_ipod", "unmarked"):
            record.close()
            reason = ("Not an iPod" if status == "not_ipod"
                      else f"No {MARKER} on this iPod")
            log.info("Not syncing: %s (nothing read or written)", reason)
            return {"not_synced": reason}
        # A Mac-formatted iPod is HFS+, which Linux can't safely write: read
        # its plays (a restore to FAT32 would wipe them), then fail.
        mac_formatted = write and device.fstype() == "hfsplus"
        if mac_formatted:
            log.warning("This iPod was formatted on a Mac (HFS+), which the dock can't "
                        "write. Reading plays only. Restore it on Windows (FAT32) to sync music.")
        writing = write and not mac_formatted
        if writing:
            device.make_writable()
            if status == "known":
                with open(os.path.join(device.root, MARKER), "w") as f:
                    f.write(MARKER_TEXT)
                    f.flush()
                    os.fsync(f.fileno())
                log.info("Created %s on this already-synced iPod", MARKER)
        client = Client(cfg["server"]["url"], cfg["server"]["username"],
                        cfg["server"]["password"])

        def progress(n, total, t):
            step(5, f"Copying {n + 1}/{total}: {t.get('artist')} - {t.get('title')}",
                 part_done=n, part_total=total)
        try:
            summary = ipod_handler.sync_player(
                device.root, guid, record, manifest, cache_dir,
                work_dir=os.path.join(base, "work", guid),
                backup_root=os.path.join(base, "backups", guid),
                write=writing, client=client, usb_product=product, step=step,
                progress=progress, block_device=whole_disk(getattr(device, "path", "")))
        finally:
            record.close()
        if mac_formatted:
            raise SyncError("Mac-formatted iPod (see guide)")
        summary["setup"] = handle_setup_file(device, writing, guid)
        if writing:
            try:
                import dock_guide
                if dock_guide.write_guide(device.root, cfg):
                    log.info("Updated %s on the iPod", dock_guide.GUIDE_FILE)
            except Exception:
                log.exception("Couldn't write the guide (continuing)")
        summary["wifi"] = handle_wifi_file(device, write, client)
        log.info("Session summary: %s", summary)
        return summary
    return work


def handle_setup_file(device, write, guid):
    """DockSetup.txt on an opted-in player (forget/keep other iPods; write
    mode only, since success deletes it), then MediaDock.txt rewritten with
    the iPods the dock knows. Returns the DockSetup.txt result or None.
    Never fails the sync."""
    import dock_setup
    from dock_record import Record
    result = None
    found = dock_setup.find_file(device.root, dock_setup.SETUP_FILE)
    if found and not write:
        log.info("%s found but the dock is read-only (write = false): ignored",
                 dock_setup.SETUP_FILE)
        return {"ok": False, "done": [], "reason": "Dock is read-only (write = false)"}
    if not write:
        return None
    try:
        record = Record()
        try:
            if found:
                result = dock_setup.apply_setup_file(device.root, record, guid)
            dock_setup.write_marker(device.root, MARKER, record.players(), guid,
                                    MARKER_TEXT, result)
        finally:
            record.close()
    except Exception as e:
        log.exception("%s / %s handling failed", dock_setup.SETUP_FILE, MARKER)
        if found and result is None:
            result = {"ok": False, "done": [], "reason": f"Error: {e}"[:60]}
    return result


def handle_wifi_file(device, write, client):
    """wifi.txt on an opted-in player: try the network (write mode only,
    since success deletes the file). Returns a result dict or None."""
    import dock_wifi
    if not dock_wifi.find_wifi_file(device.root):
        return None
    if not write:
        log.info("wifi.txt found but the dock is read-only (write = false): ignored")
        return {"ok": False, "ssid": "", "reason": "Dock is read-only (write = false)",
                "server": None}

    def server_check():
        try:
            client.call("ping")
            return True
        except Exception:
            return False
    try:
        return dock_wifi.apply_wifi_file(device.root, server_check=server_check)
    except Exception as e:                         # never fail the sync over Wi-Fi
        log.exception("wifi.txt handling failed")
        return {"ok": False, "ssid": "", "reason": f"Error: {e}"[:60], "server": None}


def schedule_rest(minutes=REST_AFTER_MIN):
    """Have systemd swap SAFE for DOCK READY in `minutes`, in case the player
    stays docked (unplugging does it sooner, via the udev rule). Root only."""
    if os.geteuid() != 0 or not shutil.which("systemd-run"):
        return
    subprocess.run(["systemctl", "stop", "musicdock-rest-later.timer"],
                   capture_output=True)
    r = subprocess.run(["systemd-run", f"--on-active={minutes}min",
                        "--unit=musicdock-rest-later", "--collect",
                        "systemctl", "start", "musicdock-rest@timer.service"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("Couldn't schedule the screen reset: %s", r.stderr.strip())


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
def build_work(args, device):
    """Decide what the session does: the iPod sync, or just a listing.
    Returns a work callable, None (listing), or False (can't run)."""
    import dock_config
    try:
        cfg = dock_config.load()
    except dock_config.ConfigError as e:
        log.warning("No usable config (%s): listing the player only", e)
        return None
    if cfg["device"]["type"] != "stock_ipod":
        log.warning("Device type %r has no handler yet: listing only", cfg["device"]["type"])
        return None
    if isinstance(device, FakeDevice):
        guid, product = sysinfo_guid(device.root), None
    else:
        guid, product = player_identity(device.path)
    if not guid:
        log.error("Can't identify the player (no USB serial / FirewireGuid)")
        return False
    write = cfg["device"]["write"] or args.write
    log.info("Player %s (USB product %s): %s", guid,
             f"0x{product:04x}" if product else "?",
             "full sync" if write else "read-only (plays only; config write = false)")
    try:
        return make_ipod_work(cfg, guid, product, write)
    except ImportError as e:
        log.error("Can't load the iPod handler (%s); run with ~/musicdock/venv", e)
        return False


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
    p.add_argument("--list-only", action="store_true",
                   help="just mount, list and unmount (the Phase 2 check)")
    p.add_argument("--write", action="store_true",
                   help="full sync even if the config says write = false "
                        "(e.g. to test against a --fake-device copy)")
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

    work = None
    if not args.list_only:
        work = build_work(args, device)
        if work is False:
            return 1

    ok = run_sync(device, Screen(preview_dir).show, work=work)
    if ok and not preview_dir:
        schedule_rest()
    if preview_dir:
        log.info("Screen previews in %s", preview_dir)
    log.info("Sync %s", "finished: safe to remove" if ok else "did not complete")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
