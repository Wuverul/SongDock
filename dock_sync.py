#!/usr/bin/env python3
"""
dock_sync.py - sync job: runs when a player docks.

Started by the udev rule (musicdock-sync@<device>), or by musicdock-resync
for an iPod left docked when new music is ready. It identifies the player
(USB serial = FireWire GUID), mounts it read-only, checks it opted in
(MediaDock.txt), makes it writable only in write mode ([device] write =
true), and hands it to ipod_handler (stock firmware) or rockbox_handler
(a .rockbox folder): plays read, queued and sent to Navidrome, music added
and removed to match the prepare job's plan, playlists rebuilt. Then it
handles wifi.txt and DockSetup.txt, rewrites MediaDock.txt and the iPod's
README, unmounts and ejects, and shows SAFE TO REMOVE (or FAILED). During
concurrent batches it shows PREPARE & SYNC instead, and asks for the next
sync itself when more music was prepared while it copied.

Usage:
  sudo dock_sync.py                         # use the one USB filesystem plugged in
  sudo dock_sync.py --device /dev/sda1      # what the systemd service runs
  dock_sync.py --fake-device ./fake_ipod    # Mac: a folder stands in for the player
  dock_sync.py --fake-device ./fake_ipod --fake-fail unmount   # try failure paths
  dock_sync.py --fake-device ./fake_ipod --panel   # Pi: fake player, real screen

With --fake-device nothing is mounted, and screens are saved as numbered PNGs
in --preview-dir (default ./previews) instead of drawn on the panel.

Safety rules (from CLAUDE.md):
  * SYNCING (or PREPARE & SYNC) is drawn before mounting. If it can't be
    drawn, we don't mount.
  * Every player is mounted read-only first, and made writable only after
    the MediaDock.txt check, in write mode.
  * Once mounted, we always try to unmount, even if a step failed.
  * SAFE and FAILED both say "safe to remove", so they're only drawn after the
    unmount is confirmed. If unmount fails, SYNCING stays up with a warning.
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
MARKER_TEXT = ("This iPod is synced by SongDock "
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
    dock already syncs this player), or "unmarked". Any drive with
    MediaDock.txt is synced: an iPod (iPod_Control), a Rockbox player
    (.rockbox), or a generic music-folder player (neither). Only iPods and
    Rockbox players get a marker created for them ("known")."""
    try:
        names = {n.lower() for n in os.listdir(root)}
    except OSError:
        names = set()
    if MARKER.lower() in names:
        return "marked"
    if not any(os.path.isdir(os.path.join(root, d)) for d in ("iPod_Control", ".rockbox")):
        return "not_ipod"
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
DOCK_RESYNC = os.path.join(HERE, "dock_resync.py")
# This sync's restore (dock_restore: songs on the player when it started,
# and in all) for the copy screens; empty when it isn't one.
RESTORE = {}
# The player type once it's known (after mounting), for the screens' icon.
PLAYER = {}


def device_args():
    return ["--device", PLAYER["kind"]] if PLAYER.get("kind") else []


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
    without it the player is just listed (--list-only: a hardware check).
    Returns True if the player was synced and is safe to remove.
    """
    total = total or (IPOD_STEPS if work else TOTAL_STEPS)
    PLAYER.clear()
    import dock_progress
    batch = {}     # concurrent batches: this sync's place in the prepare run

    def run_screen(part_done=None):
        """PREPARE & SYNC arguments while this sync is part of a prepare run
        (concurrent batches: still converting, or its final sync), counting
        every batch of the run; None for an ordinary sync."""
        prep = dock_progress.running_prepare()
        if not prep:
            return None
        if batch.get("run") != prep["run"]:
            base, syncs = dock_progress.copied(prep["run"])
            batch.update(run=prep["run"], base=base, synced=base, syncs=syncs + 1)
        if part_done is not None:          # steps without a count keep the last one
            batch["synced"] = batch["base"] + part_done
        synced = batch["synced"]
        dock_progress.write_copied(prep["run"], synced, batch["syncs"])
        args = dock_progress.screen_args(prep, synced, batch["syncs"])
        if RESTORE:          # a restore: songs on the iPod out of all of them
            args += ["--restored", str(RESTORE["have"] + (part_done or 0)),
                     "--restore-total", str(RESTORE["total"])]
        return args + device_args()

    def step(n, detail, part_done=None, part_total=None):
        # Progress redraws are nice-to-have: SYNCING is already on screen,
        # so a panel hiccup here shouldn't abort the sync. part_done/total:
        # progress within this step (e.g. tracks copied) for the bar.
        part = (["--part-done", str(part_done), "--part-total", str(part_total)]
                if part_total else [])
        try:
            args = run_screen(part_done)
            if args:
                show(*args)
            else:
                show("syncing", "--step", str(n), "--total", str(total),
                     "--detail", detail, *part, *device_args())
        except Exception:
            log.exception("Progress redraw failed (continuing)")

    # 1. SYNCING goes up before anything touches the player. --force skips
    #    rate limiting, so it's guaranteed to draw. If it can't be drawn,
    #    the user could unplug mid-sync, so we stop before mounting.
    #    Part of a run with concurrent batches: PREPARE & SYNC instead, which
    #    says "Do not remove" just the same (and counts as SYNCING after a crash).
    try:
        args = run_screen()
        if args:
            show(*args, force=True)
        else:
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
            if batch.get("run"):                  # the whole batch is on the player now
                batch["synced"] = batch["base"] + summary.get("added", 0)
                dock_progress.write_copied(batch["run"], batch["synced"], batch["syncs"])
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
        elif summary.get("paused"):
            import dock_safehours
            window = dock_safehours.from_config(dock_config_or_empty())
            end = dock_safehours.clock_text(dock_safehours.window_end(window)) if window else None
            resume = ("rest at the next sync" if not end
                      else f"replug after {end}" if summary.get("replug_to_finish")
                      else f"rest after {end}")
            show("safehours", "--detail", f"Synced +{summary.get('added', 0)}, {resume}",
                 force=True)
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
            prep = dock_progress.running_prepare() if batch.get("run") else None
            if prep and prep["run"] == batch["run"] and (
                    not prep.get("final") or summary.get("followup")):
                # More batches to come (still converting, or this sync just
                # asked for the next one): the run's screen stays up until
                # it's all done (user's choice: "Do not remove" throughout).
                show(*dock_progress.screen_args(
                    prep, dock_progress.copied(prep["run"])[0], batch["syncs"]), force=True)
            else:
                if batch.get("run"):
                    dock_progress.clear_run()          # the run's last sync: done
                show("nospace" if summary.get("no_space") else "safe",
                     *(["--not-added", str(summary["no_space"])]
                       if summary.get("no_space") else []),
                     "--added", str(summary.get("added", 0)),
                     "--removed", str(summary.get("removed", 0)),
                     "--plays", str(summary.get("plays_sent", 0)),
                     "--queued", str(summary.get("plays_queued", 0)),
                     *(["--no-plays"] if summary.get("plays_supported") is False else []),
                     force=True)
    except Exception:
        log.exception("Couldn't draw final screen; SYNCING stays up")
        return False
    return error is None


def size_notes(record, guid, root, manifest, cfg, kind):
    """Record the player's capacity and log dock_limits' notes about its
    size (also written into MediaDock.txt). Returns the notes."""
    try:
        import dock_limits
        import dock_restore
        capacity = shutil.disk_usage(root).total
        record.set_player_capacity(guid, capacity)
        on_player = len({r["navidrome_id"] for r in record.tracks(guid)})
        planned = len(dock_restore.targets(manifest or {}))
        notes = dock_limits.player_notes(on_player, planned, capacity, cfg, kind,
                                         dock_limits.plan_bytes(manifest or {}),
                                         dock_limits.alac_bytes(manifest or {}))
        for note in notes:
            log.warning("%s", note)
        import dock_artists
        artists = dock_artists.note((manifest or {}).get("unsplit_artists"))
        return notes + [artists] if artists else notes
    except Exception:
        log.exception("Couldn't check the player's size (continuing)")
        return []


def dock_config_or_empty():
    try:
        import dock_config
        return dock_config.load()
    except Exception:
        return {}


def safe_hours_now(show, schedule=True, now=None):
    """Safe hours (dock_safehours): if the window is open, don't mount the
    player - it charges and stays usable - show SAFE HOURS, and have a
    still-docked iPod synced when the window ends. Returns True if so."""
    import dock_safehours
    window = dock_safehours.from_config(dock_config_or_empty())
    if not dock_safehours.active(window, now):
        return False
    resume = dock_safehours.clock_text(dock_safehours.window_end(window, now))
    log.info("Safe hours (%s): not syncing; a docked iPod syncs after %s",
             dock_safehours.text(window), resume)
    try:
        show("safehours", "--detail", f"Syncs resume at {resume}", force=True)
    except Exception:
        log.exception("Couldn't draw SAFE HOURS")
    if schedule:
        dock_safehours.schedule_after(window, DOCK_RESYNC, now=now)
    return True


def whole_disk(dev_path):
    """/dev/sda1 -> /dev/sda (SCSI inquiries go to the disk); else None."""
    m = re.fullmatch(r"(/dev/sd[a-z]+)\d*", dev_path or "")
    return m.group(1) if m else None


def make_ipod_work(cfg, guid, product, write):
    """The player session for run_sync (needs the venv: pygpod, mutagen):
    ipod_handler for stock iPods, rockbox_handler for players with a
    .rockbox folder. Both take the same staged plan."""
    import ipod_handler
    import rockbox_handler
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
        # Rockbox and generic players are "players" in messages, not iPods.
        what = "iPod" if os.path.isdir(os.path.join(device.root, "iPod_Control")) else "player"
        if status in ("not_ipod", "unmarked"):
            record.close()
            reason = f"No {MARKER} on this {what}"
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
                log.info("Created %s on this already-synced %s", MARKER, what)
        rockbox = rockbox_handler.is_rockbox(device.root)
        generic = not rockbox and not os.path.isdir(os.path.join(device.root, "iPod_Control"))
        kind = "rockbox" if rockbox else "generic" if generic else "stock"
        PLAYER["kind"] = kind
        player_guid = guid
        if generic:
            # A plain music-folder player: known by the ID the dock writes on
            # it (cheap players often share one USB serial). No plays to read,
            # so without write mode there's nothing to do.
            player_guid = rockbox_handler.player_id(device.root, create=writing)
            if not player_guid:
                record.close()
                log.info("Generic music player, dock read-only: nothing read or written")
                return {"not_synced": "Dock is read-only (write = false)"}
            log.info("Generic music player %s: syncing files (no plays, no playlists)",
                     player_guid)
        elif not player_guid:
            record.close()
            raise SyncError("Can't identify the player")
        # type = "rockbox" keeps FLAC in the cache, which a stock iPod can't
        # play: its plays are still read, and DockSetup.txt / wifi.txt still
        # handled (so StockCompatible=yes can switch the dock back from this
        # very iPod), but its music isn't touched.
        refused = kind == "stock" and cfg.get("device", {}).get("type") == "rockbox"
        if refused:
            log.warning("Stock iPod on a Rockbox-only dock: reading plays only")
        handler = ipod_handler if kind == "stock" else rockbox_handler
        if rockbox:
            log.info("Rockbox player: syncing files and playlists")
        client = Client(cfg["server"]["url"], cfg["server"]["username"],
                        cfg["server"]["password"])
        # Safe hours: a sync still copying when the window opens wraps up.
        import dock_safehours
        window = dock_safehours.from_config(cfg)
        opens = dock_safehours.next_start(window)
        deadline = opens.timestamp() if opens else None

        RESTORE.clear()
        if kind == "stock" and writing and not refused:
            try:
                import dock_restore
                present = {r["navidrome_id"] for r in record.tracks(player_guid)}
                present |= set(record.room_removed(player_guid))
                info = dock_restore.status(player_guid, present, manifest)
                if info:
                    RESTORE.update(have=info["have"], total=info["total"])
                    log.info("Restoring this iPod: %d of %d songs on it", info["have"],
                             info["total"])
            except Exception:
                log.exception("Couldn't work out the restore count (continuing)")

        def progress(n, total, t):
            what = f"{t.get('artist')} - {t.get('title')}"
            if RESTORE:                              # one count for the whole restore
                detail = f"Restoring {RESTORE['have'] + n + 1:,}/{RESTORE['total']:,}: {what}"
            else:
                detail = f"Copying {n + 1}/{total}: {what}"
            step(5, detail, part_done=n, part_total=total)
        try:
            summary = handler.sync_player(
                device.root, player_guid, record, manifest, cache_dir,
                work_dir=os.path.join(base, "work", player_guid),
                backup_root=os.path.join(base, "backups", player_guid),
                write=writing and not refused, client=client, usb_product=product, step=step,
                progress=progress, block_device=whole_disk(getattr(device, "path", "")),
                deadline=deadline,
                **({"next_plan": lambda plan: next_plan(cache_dir, plan, deadline=deadline)}
                   if kind == "stock" else {"kind": kind}))
            if writing:
                summary["notes"] = size_notes(record, player_guid, device.root, manifest,
                                              cfg, kind)
        finally:
            record.close()
        if mac_formatted:
            raise SyncError("Mac-formatted iPod (see guide)")
        # Wi-Fi first: a new server in DockSetup.txt is tried on the network
        # the dock ends up on.
        summary["wifi"] = handle_wifi_file(device, write, client)
        summary["setup"] = handle_setup_file(device, writing, player_guid, cfg,
                                             summary.get("notes"))
        if refused:
            setup = summary["setup"] or {}
            if setup.get("stock_compatible"):
                log.info("Stock compatibility back on: this iPod's music follows "
                         "once the next prepare run has converted it")
            elif not setup or setup.get("ok"):
                raise SyncError("Stock iPod: dock is set up for Rockbox")
            # else: SETUP NOT USED shows why the DockSetup.txt failed
        elif writing:
            if summary.get("paused"):
                log.info("Safe hours from %s: stopped copying; the rest follows after "
                         "%s (an iPod still docked syncs by itself; other players "
                         "need plugging in again)", dock_safehours.text(window),
                         dock_safehours.clock_text(dock_safehours.window_end(window)))
                if not isinstance(device, FakeDevice):
                    dock_safehours.schedule_after(window, DOCK_RESYNC)
            applied = {"generated": summary.get("applied", manifest.get("generated"))}
            summary["followup"] = ask_for_followup(cfg, applied, player_guid)
            try:
                import dock_guide
                if dock_guide.write_guide(device.root, cfg, kind=kind):
                    log.info("Updated %s on the %s", dock_guide.GUIDE_FILE, what)
            except Exception:
                log.exception("Couldn't write the guide (continuing)")
        log.info("Session summary: %s", summary)
        return summary
    return work


FOLLOW_POLL = 30          # seconds between looks for the prepare run's next plan


def next_plan(cache_dir, applied, poll=FOLLOW_POLL, sleep=time.sleep, running=None,
              deadline=None, clock=time.time):
    """For a sync following a prepare run (ipod_handler.follow_plans): wait
    for the plan after `applied` and return it, or None once no prepare
    run is left to hand one over (it ended or crashed without a newer
    plan). Never waits for the prepare job to exit: the end of a run waits
    for a running sync, so the final plan on disk is the signal."""
    import dock_progress
    running = running or dock_progress.running_prepare
    path = os.path.join(cache_dir, "manifest.json")
    while True:
        try:
            with open(path) as f:
                newest = json.load(f)
        except (OSError, ValueError):
            newest = None
        if newest and "tracks" in newest and newest.get("generated") != applied.get("generated"):
            return newest
        if running() is None or (deadline and clock() >= deadline):
            return None                     # run over, or safe hours begin
        sleep(poll)


def ask_for_followup(cfg, applied, guid):
    """Concurrent batches: the prepare job may have handed over a newer plan
    while this sync was copying. If it has more for this player, ask for the
    next sync now (dock_resync waits for this one to end) rather than at the
    next batch. Only when the plan changed since this sync read it, so a
    player that can't take more (full, files missing) never loops.
    Returns True if a sync was asked for."""
    if not cfg.get("device", {}).get("auto_resync", True):
        return False
    try:
        with open(os.path.join(cfg["sync"]["cache_dir"], "manifest.json")) as f:
            newest = json.load(f)
    except (OSError, ValueError, KeyError):
        return False
    if "tracks" not in newest or newest.get("generated") == (applied or {}).get("generated"):
        return False
    try:
        import dock_resync
        import ipod_handler
        from dock_record import Record
        record = Record()
        try:
            records = record.tracks(guid)
            no_room = (record.player(guid) or {}).get("swaps_no_room")
            counted = ipod_handler.counted_ids(record, guid, records)
        finally:
            record.close()
        add, remove = ipod_handler.pending_changes(newest, counted)
        if not (add or remove
                or any(ipod_handler.conversions_ready(newest, records, no_room)[:2])):
            return False
        dock_resync.request()
    except Exception:
        log.exception("Couldn't ask for the next sync (the next batch will)")
        return False
    log.info("More music was prepared during this sync (+%d -%d): syncing again next",
             add, remove)
    return True


def handle_setup_file(device, write, guid, cfg=None, notes=None):
    """DockSetup.txt on an opted-in player (forget/keep other iPods, change
    settings in config.toml; write mode only, since success deletes it -
    and any password in it), then MediaDock.txt rewritten with
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
                result = dock_setup.apply_setup_file(device.root, record, guid, cfg)
            dock_setup.write_marker(device.root, MARKER, record.players(), guid,
                                    MARKER_TEXT, result, notes)
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
    if isinstance(device, FakeDevice):
        guid, product = sysinfo_guid(device.root), None
    else:
        guid, product = player_identity(device.path)
    if not guid:
        # Fine for a generic music player (the dock writes an ID on it); an
        # iPod or Rockbox player without one is refused once mounted.
        log.info("No USB serial / FirewireGuid for this player")
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
    p = argparse.ArgumentParser(description="SongDock sync job")
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
                   help="just mount, list and unmount (checks USB, mounting and the screen)")
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

    if not args.list_only and safe_hours_now(Screen(preview_dir).show,
                                             schedule=not args.fake_device):
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
