"""
dock_sdcard.py - notice signs that the Pi's SD card is failing (user, 2026-11-26).

Everything the dock knows lives on the SD card, and cheap cards wear out. Many die
without warning, but some show signs first, and those are worth a line on the screen:

  * the kernel reporting read/write errors on the card ("I/O error, dev mmcblk0",
    "mmcblk0: error -110 ...", "mmcblk0: recovery failed");
  * the filesystem finding damage ("EXT4-fs error (device mmcblk0p2)"); ext4 also
    counts these in its superblock, so they're seen again after a restart;
  * the system remounting the card read-only after an error.

Only messages naming the card itself count: "mmc1" on a Pi Zero 2 W is the Wi-Fi chip
(see dock_usbport), and a bare "mmc0" timeout is often harmless.

Once seen, a warning stays until the card is replaced (it's tied to the card's ID,
so a new card starts clean) or someone says it's fine:
    sudo python3 /opt/songdock/app/songdock/core/dock_sdcard.py --ok
DOCK READY then shows "SD card failing" in place of the IP address, and musicdock-
status says what was seen. The warning is kept on the card and, in case the card
can't be written any more, in /run (RAM) too.

dock_usbport.check (root, every 30 s) passes it the kernel's new messages. Standard
library only.
"""

import json
import os
import re
import sys
import time

# The card's own errors (mmcblk0 = the SD card on every Raspberry Pi).
CARD_ERROR = re.compile(
    r"I/O error,? dev mmcblk0"                  # blk_update_request / Buffer I/O error
    r"|Buffer I/O error on dev mmcblk0"
    r"|mmcblk0: (?:error -?\d+|recovery failed|timed out)"
    r"|mmcblk0: response CRC error"
    r"|EXT4-fs error \(device mmcblk0"
    r"|EXT4-fs \(mmcblk0p\d+\): (?:Remounting filesystem read-only|"
    r"I/O error while writing superblock)"
    r"|FAT-fs \(mmcblk0p\d+\): (?:error|Filesystem has been set read-only)")
CARD_ID = "/sys/block/mmcblk0/device/cid"


def _folders():
    """(kept on the card, kept in RAM). Tests and previews set DOCK_STATE_FILE and
    get its folder for both (never a live dock's files)."""
    state = os.environ.get("DOCK_STATE_FILE")
    if state:
        folder = os.path.dirname(state)
        return (os.path.join(folder, "sdcard.json"), os.path.join(folder, "sdcard.run.json"))
    return ("/var/lib/musicdock/sdcard.json", "/run/musicdock/sdcard.json")


def _read(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    """Best effort: a failing card may not take it (the RAM copy then does)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)                 # the prepare job (not root) draws it too
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def card_id():
    try:
        with open(CARD_ID) as f:
            return f.read().strip()
    except OSError:
        return None


def root_device():
    """The root filesystem's device name (e.g. "mmcblk0p2") and whether it's read-only."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/":
                    return os.path.basename(parts[0]), "ro" in parts[3].split(",")
    except OSError:
        pass
    return None, False


def ext4_errors(device):
    """How many errors ext4 has recorded on `device` (kept on the card; 0 if unknown)."""
    if not device:
        return 0
    try:
        with open(f"/sys/fs/ext4/{device}/errors_count") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def state():
    """The warning as kept (the RAM copy wins: it's the newer when the card failed)."""
    kept, ram = _folders()
    return _read(ram) or _read(kept)


def warning():
    """What was seen, if this card has a warning: {"problem", "since", ...}, else None."""
    s = state()
    if not s.get("problem"):
        return None
    if s.get("card") and card_id() and s["card"] != card_id():
        return None                          # a new card: the warning was the old one's
    return s


def check(lines, now=None, read_id=card_id, read_root=root_device, read_errors=ext4_errors):
    """Look at the kernel's new messages (`lines`: (time, text)) and the card's state.
    Returns the problem when it's newly seen (to redraw the screen), else None."""
    now = time.time() if now is None else now
    kept_path, ram_path = _folders()
    s = state()
    card = read_id()
    if s.get("card") and card and s["card"] != card:
        s = {}                               # a new card: start over
    device, read_only = read_root()
    errors = read_errors(device)
    found = []
    for stamp, text in lines:
        if CARD_ERROR.search(text):
            found.append(text.split(": ", 1)[-1].strip()[:160])
    # Only what's new: these are seen on every run (and a failing card shouldn't be
    # written every 30 seconds).
    if errors > max(s.get("ok_errors", 0), s.get("errors", 0)):
        found.append(f"the filesystem has recorded {errors} error(s) on the card")
    if read_only and not s.get("read_only"):
        found.append("the card was switched to read-only")
    if not found:
        return None
    new = not s.get("problem")
    s.update(card=card, last=now, count=s.get("count", 0) + len(found),
             detail=found[-1], errors=max(errors, s.get("errors", 0)),
             read_only=read_only or s.get("read_only", False))
    if new:
        s.update(problem=found[0], since=now)
    _write(ram_path, s)
    _write(kept_path, s)
    return s["problem"] if new else None


def mark_ok(read_id=card_id, read_root=root_device, read_errors=ext4_errors):
    """Clear the warning (the card was checked); errors ext4 already counted stay
    behind it, new ones bring it back."""
    device, _ = read_root()
    s = {"card": read_id(), "ok_errors": read_errors(device), "ok_at": time.time()}
    for path in _folders():
        _write(path, s)


def describe(s):
    """One line for musicdock-status."""
    since = time.strftime("%a %d %b %H:%M", time.localtime(s["since"]))
    more = f" ({s['count']} signs so far, last: {s['detail']})" if s.get("count", 1) > 1 else ""
    return (f"SD card may be failing since {since}: {s['problem']}{more}. Back up and "
            f"replace it; after checking it, clear this with: sudo python3 "
            f"/opt/songdock/app/songdock/core/dock_sdcard.py --ok")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--ok"]:
        mark_ok()
        print("SD card warning cleared; new errors will bring it back")
        return 0
    s = warning()
    print(describe(s) if s else "No signs of SD card trouble")
    return 0


if __name__ == "__main__":
    sys.exit(main())
