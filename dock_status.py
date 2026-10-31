#!/usr/bin/env python3
"""
dock_status.py - SongDock's e-ink status screens.

Target: Waveshare 2.13" e-Paper HAT V4 (250x122) on a Raspberry Pi Zero W or
Zero 2 W.

Usage:
  dock_status.py idle [--ready 38 --total 40] [--boot] [--detail "..."]
  dock_status.py preparing --done 12 --total 40 --detail "Converting: Artist - Song"
  dock_status.py syncing --step 3 --total 6 --detail "Copying tracks 12/40"
  dock_status.py prepsync --done 32 --total 73 --synced 25 --to-copy 87
  dock_status.py safe --added 14 --removed 9 --plays 38
  dock_status.py failed --reason "Server unreachable"
  dock_status.py starting --uptime 75 [--clock-synced]   # boot progress
  dock_status.py offline --problem wifi --ssid "Home" --uptime 300

Who draws what:
  * prepare job (scheduled, never touches the player): idle, preparing
    (and keeps prepsync's conversion count current)
  * sync job (runs when the player docks): syncing, prepsync, safe, failed,
    notsynced, wifi / wififail, setup / setupfail
  * boot (dock_boot.py): starting, offline; re-sync (dock_resync.py): connecting
  * USB watch (dock_usbport.py): usbstuck - a failed plug-in left the USB
    port dead; only a restart brings it back

Every screen shows Wi-Fi status (signal bars + dBm, or "No Wi-Fi") in the
bottom-right corner; the idle screen also shows the Pi's IP address. These are
a snapshot from the moment the screen was drawn.

Add --preview to any command to save a PNG instead of drawing on the panel
(works on any computer, no display needed). Use --wifi -56 / --wifi off and
--ip 192.168.20.40 to fake those readings in previews.

Safety rules baked in:
  * Every screen shows the time, so a stale screen can't pass as current.
  * The sync script should call `syncing` BEFORE mounting anything, so a crash
    leaves "SYNCING / do not remove" on screen (the safe failure).
  * The prepare job can never draw over SYNCING or PREPARE & SYNC. Only the
    sync job (or --force) can replace those screens.
  * Progress redraws (syncing, preparing, prepsync) are rate-limited to protect the
    panel; changes between screens always draw immediately.
"""

import argparse
import contextlib
import fcntl
import json
import os
import socket
import sys
import tempfile
import time
import tomllib
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- config ----
WIDTH, HEIGHT = 250, 122            # landscape
_BUNDLED_EPD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
EPD_LIB = os.environ.get("EPD_LIB") or (
    _BUNDLED_EPD if os.path.isdir(os.path.join(_BUNDLED_EPD, "waveshare_epd"))
    else os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/lib"))
# Every job (sync and boot run as root; manual runs and the prepare job as the
# login user) must share one state file, or the "never draw over SYNCING"
# check can't see another job's screen. pi/install.sh creates the shared
# folder; elsewhere (e.g. the Mac) we fall back to ~/.cache.
SHARED_STATE_DIR = "/var/lib/musicdock"
STATE_FILE = os.environ.get("DOCK_STATE_FILE") or (
    os.path.join(SHARED_STATE_DIR, "dock_status.json")
    if os.path.isdir(SHARED_STATE_DIR)
    else os.path.expanduser("~/.cache/dock_status.json"))
RUNTIME_DIR = "/run/musicdock"      # RAM; cleared at every boot
MIN_PROGRESS_INTERVAL = 30         # seconds between progress-only redraws
FULL_REFRESH_EVERY = 20             # partial (flash-free) updates before a full anti-ghost refresh

PROGRESS_SCREENS = {"syncing", "preparing", "prepsync"}   # rate-limited when repeated
SYNC_SCREENS = {"syncing", "prepsync"}         # "do not remove": nothing else covers them
PREPARE_SCREENS = {"idle", "preparing", "restoring"}       # drawn by the prepare job
# Resting screens --refresh may redraw when Wi-Fi connects or drops. Active
# screens (syncing, preparing, boot) redraw often enough on their own.
REFRESHABLE = {"idle", "safe", "nospace", "failed", "notsynced", "wifi", "wififail", "setup",
               "setupfail", "usbstuck", "restoring", "safehours"}
# Options that control drawing rather than what's drawn; not saved for --refresh.
CONTROL_OPTIONS = {"--force": 0, "--only-over": 1, "--preview": 1, "--at": 1,
                   "--wifi": 1, "--ip": 1}

WIFI_IFACE = "wlan0"
# Signal strength (dBm) needed for each bar: 4 bars at -55 or better, etc.
WIFI_BAR_THRESHOLDS = (-85, -75, -65, -55)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size), True
    except OSError:
        return ImageFont.load_default(), False


# ----------------------------------------------------------------- state ----
@contextlib.contextmanager
def panel_lock():
    """One drawing job at a time (sync, prepare, boot can overlap).

    Covers the state check, the draw, and the state save, so two jobs can't
    both decide they may draw and then talk to the panel at once. The lock
    file is opened read-only, so it works whichever user created it.
    """
    path = os.path.join(os.path.dirname(STATE_FILE), ".panel.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    folder = os.path.dirname(STATE_FILE)
    os.makedirs(folder, exist_ok=True)
    # Write a temp file, then rename it over the old one. A power cut can't
    # leave half a file, and it works even when the old file was written by
    # another user (root's sync job vs. the login user).
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".dock_status.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------- network ----
def read_wifi(iface=WIFI_IFACE, sys_root="/sys/class/net",
              proc_file="/proc/net/wireless"):
    """Return signal in dBm (int) if connected, "off" if not, None if unknown.

    "Unknown" means we're not on a Linux machine with that interface (e.g. a
    preview on a Mac), in which case the indicator is simply left off.
    """
    try:
        with open(os.path.join(sys_root, iface, "operstate")) as f:
            if f.read().strip() != "up":
                return "off"
    except OSError:
        return None
    try:
        with open(proc_file) as f:
            for line in f:
                if line.strip().startswith(iface + ":"):
                    level = int(float(line.split()[3].rstrip(".")))
                    if level > 0:            # some drivers report 0-255
                        level -= 256
                    return level if level < 0 else "off"
    except (OSError, ValueError, IndexError):
        pass
    return "off"


def read_ip():
    """Best-effort local IP address. Sends no traffic."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def wifi_bars(dbm):
    return sum(1 for t in WIFI_BAR_THRESHOLDS if dbm >= t)


# ---------------------------------------------------------------- render ----
def pending_line(args, has_glyphs=True):
    add, remove = args.pending_add or 0, args.pending_remove or 0
    if not add and not remove:
        return conversions_line(args) or "iPod is up to date"
    minus = "\u2212" if has_glyphs else "-"
    return f"Next sync: +{add}  {minus}{remove} tracks"


def conversions_line(args):
    """Songs the next sync swaps for another format, or None. They aren't
    added or removed, so the +/- counts don't show them."""
    if args.pending_lossless:
        return f"Lossless pending: {args.pending_lossless} songs"
    if args.pending_format:
        return f"Format changes: {args.pending_format} songs"
    if args.swaps_no_room:
        return f"No room to convert {args.swaps_no_room}"
    return None


def syncing_progress(args):
    """(done, total) for the SYNCING bar: progress within the current stage
    when it has its own count (e.g. tracks copied, --part-done/--part-total),
    else which stage of how many."""
    if args.part_total:
        return args.part_done or 0, args.part_total
    if args.step and args.total:
        return args.step, args.total
    return 0, 0


def render(screen, args):
    img = Image.new("1", (WIDTH, HEIGHT), 255)  # 255 = white
    d = ImageDraw.Draw(img)
    title_f, has_glyphs = load_font(FONT_BOLD, 20)
    body_f, _ = load_font(FONT_REG, 13)
    small_f, _ = load_font(FONT_REG, 11)
    # --at keeps a redrawn screen's original time (see refresh_main).
    when = datetime.fromtimestamp(args.at) if args.at else datetime.now()
    now = when.strftime("%-I:%M %p")

    def header(text, inverted, room=WIDTH):
        # Centered in `room` (the bar left of an icon, when there is one).
        x = max(4, (room - d.textlength(text, font=title_f)) // 2)
        if inverted:
            d.rectangle((0, 0, WIDTH, 32), fill=0)
            d.text((x, 5), text, font=title_f, fill=255)
        else:
            d.rectangle((0, 0, WIDTH - 1, 32), outline=0, width=2)
            d.text((x, 5), text, font=title_f, fill=0)

    def icon_rows(kind):
        import dock_icons
        return dock_icons.ICONS.get(dock_icons.KIND_ICON.get(kind or "", ""))

    def title_room(kind):
        """Width left for the title beside the player icon (WIDTH without one)."""
        rows = icon_rows(kind)
        return WIDTH - 8 - len(rows[0]) - 4 if rows else WIDTH

    def device_icon(kind, inverted=True):
        """The player type (iPod, Rockbox, other) in the top bar's right corner."""
        rows = icon_rows(kind)
        if not rows:
            return
        left, top = WIDTH - 8 - len(rows[0]), (33 - len(rows)) // 2
        for y, row in enumerate(rows):
            for x, cell in enumerate(row):
                if cell == "#":
                    d.point((left + x, top + y), fill=255 if inverted else 0)

    def lines(rows, top=42):
        y = top
        for text, font in rows:
            d.text((8, y), text, font=font, fill=0)
            y += font.size + 6 if hasattr(font, "size") else 18

    # Bottom-right corner is reserved for the Wi-Fi indicator.
    BAR_RIGHT = WIDTH - 90

    def progress_bar(done, total):
        d.rectangle((8, 108, BAR_RIGHT, 116), outline=0)
        fill_w = int((BAR_RIGHT - 10) * min(done / total, 1))
        if fill_w > 0:
            d.rectangle((9, 109, 9 + fill_w, 115), fill=0)

    def wifi_indicator(wifi):
        if wifi is None:          # unknown (not running on the Pi)
            return
        bar_w, gap, base_y, right = 4, 2, 118, WIDTH - 6
        filled = 0 if wifi == "off" else wifi_bars(wifi)
        x = right - (4 * bar_w + 3 * gap)
        for i in range(4):
            h = 4 + i * 3
            box = (x, base_y - h, x + bar_w - 1, base_y)
            d.rectangle(box, fill=0 if i < filled else 255, outline=0)
            x += bar_w + gap
        label = "No Wi-Fi" if wifi == "off" else f"{wifi} dBm"
        tw = d.textlength(label, font=small_f)
        d.text((right - (4 * bar_w + 3 * gap) - 4 - tw, base_y - 12), label,
               font=small_f, fill=0)

    def fit(text, font, max_w=WIDTH - 16):
        """Trim long text (e.g. song titles) with an ellipsis so it fits."""
        if d.textlength(text, font=font) <= max_w:
            return text
        while text and d.textlength(text + "\u2026", font=font) > max_w:
            text = text[:-1]
        return text + "\u2026"

    check = "\u2713 " if has_glyphs else ""
    warn = "\u26a0 " if has_glyphs else "! "

    if screen == "prepsync":
        # Concurrent batches: music still converting while a batch is copied
        # to the docked iPod. "Synced" has its own total: songs re-added from
        # the cache need copying but no conversion. Bar: both together.
        header("PREPARE & SYNC", inverted=True, room=title_room(args.device))
        device_icon(args.device)
        sep = "\u00b7" if has_glyphs else "-"
        to_copy = args.to_copy or args.total or 0
        if args.restore_total:
            # Restoring a new or wiped iPod: songs on it out of all of them,
            # not this round's (dock_restore.py).
            lines([(f"Converting {args.done or 0:,}/{args.total or 0:,}", body_f),
                   (f"On iPod {args.restored or 0:,}/{args.restore_total:,}", body_f),
                   (f"Do not remove {sep} {now}", small_f)])
            progress_bar(args.restored or 0, args.restore_total)
        else:
            lines([(f"Converting {args.done or 0}/{args.total or 0}", body_f),
                   (f"Synced {args.synced or 0}/{to_copy}", body_f),
                   (f"Do not remove {sep} {now}", small_f)])
            if (args.total or 0) + to_copy:
                progress_bar((args.done or 0) + (args.synced or 0), (args.total or 0) + to_copy)

    elif screen == "syncing":
        # No "5/6" stage count: stages skip numbers between redraws, and the
        # detail line already says what's happening (user, 2026-10-26).
        header("SYNCING", inverted=True, room=title_room(args.device))
        device_icon(args.device)
        rows = [("Do not remove player", body_f)]
        if args.detail:
            rows.append((fit(args.detail, body_f), body_f))
        rows.append((f"Updated {now}", small_f))
        lines(rows)
        done, total = syncing_progress(args)
        if total:
            progress_bar(done, total)

    elif screen == "connecting":
        # dock_resync.py re-plugging a docked iPod for new music; the sync's
        # SYNCING (drawn with --force) replaces it moments later.
        dots = "\u2026" if has_glyphs else "..."
        header(f"CONNECTING{dots}", inverted=True)
        lines([("Do not remove player", body_f),
               ("New music ready", body_f),
               (fit(args.detail or "Reconnecting to the iPod", body_f), body_f),
               (f"Started {now}", small_f)])

    elif screen == "safe":
        header(f"{check}SAFE TO REMOVE", inverted=False)
        minus = "\u2212" if has_glyphs else "-"
        plays = f"{args.plays} plays sent"
        if args.queued:
            plays += f", {args.queued} queued"          # server was unreachable
        if args.no_plays:
            plays = "No play count - unsupported"     # a player that logs none
        lines([(f"+{args.added}  {minus}{args.removed} tracks", body_f),
               (fit(plays, body_f), body_f),
               (f"Synced {now}", body_f)])

    elif screen == "nospace":
        # A sync that ended with new songs left off (dock_room).
        header(f"{warn}PLAYER FULL", inverted=True)
        lines([("Safe to remove", body_f),
               (fit(f"{args.not_added} songs didn't fit", body_f), body_f),
               ("Not enough free space to sync", small_f),
               (f"Synced {now}", small_f)])

    elif screen in ("wifi", "wififail"):
        # Result of a wifi.txt on the player (the sync itself succeeded).
        if screen == "wifi":
            # The address on the new network goes at the bottom, so the dock
            # can be found again (the header's check mark says it's done).
            header(f"{check}WI-FI CHANGED", inverted=False)
            rows = [(fit(f"Now on \u201c{args.ssid}\u201d", body_f), body_f)]
            server = {"yes": "Navidrome reachable",
                      "no": "Navidrome NOT reachable"}.get(args.server)
            if server:
                rows.append((fit(server, body_f), body_f))
            rows.append((f"Synced {now}", small_f))
            lines(rows)
            ip = args.ip if args.ip is not None else read_ip()
            if ip:
                d.text((8, 106), f"IP {ip}", font=small_f, fill=0)
        else:
            header(f"{warn}WI-FI UNCHANGED", inverted=True)
            who = (f"Couldn't join \u201c{args.ssid}\u201d" if args.ssid
                   else "wifi.txt not used")
            rows = [("Safe to remove", body_f), (fit(who, body_f), body_f),
                    (fit(args.reason or "", small_f), small_f)]
            rows.append((f"Synced {now}", small_f))
            lines(rows)

    elif screen in ("setup", "setupfail"):
        # Result of a DockSetup.txt on the player (the sync itself succeeded).
        minus = "\u2212" if has_glyphs else "-"
        if screen == "setup":
            header(f"{check}DOCK UPDATED", inverted=False)
            rows = [(fit(args.reason or "", body_f), body_f),
                    (f"+{args.added}  {minus}{args.removed} tracks", body_f)]
        else:
            header(f"{warn}SETUP NOT USED", inverted=True)
            rows = [("Safe to remove", body_f),
                    (fit(args.reason or "DockSetup.txt not used", small_f), small_f),
                    ("See MediaDock.txt on the iPod", small_f)]
        rows.append((f"Synced {now}", small_f))
        lines(rows)

    elif screen == "notsynced":
        # A player the dock left alone: not an iPod, or no MediaDock.txt.
        header("NOT SYNCED", inverted=False)
        rows = [("Safe to remove", body_f), (fit(args.reason or "", body_f), body_f)]
        if "MediaDock" in (args.reason or ""):
            rows.append(("Add MediaDock.txt to sync it", small_f))
        rows.append((f"Checked {now}", small_f))
        lines(rows)

    elif screen == "restoring":
        # Filling a new or wiped iPod over several rounds (dock_restore.py):
        # one count for the whole job. Drawn by the prepare job between
        # syncs, when the iPod is ejected.
        header("RESTORING", inverted=False)
        rows = [(f"{args.done or 0:,}/{args.total or 0:,} songs on the iPod", body_f)]
        if args.detail:
            rows.append((fit(args.detail, small_f), small_f))
        import dock_restore
        left = dock_restore.left_text(args.left)
        sep = "\u00b7" if has_glyphs else "-"
        rows.append((fit(f"{left} {sep} {now}" if left else f"Updated {now}", small_f), small_f))
        rows.append(("OK to unplug: continues when docked", small_f))
        lines(rows)
        if args.total:
            progress_bar(args.done or 0, args.total)

    elif screen == "safehours":
        # Safe Hours (dock_safehours.py): no syncs in this window.
        header("SAFE HOURS", inverted=False)
        rows = [("Safe to remove", body_f)]
        if args.detail:
            rows.append((fit(args.detail, body_f), body_f))
        rows.append((f"Since {now}", small_f))
        lines(rows)

    elif screen == "usbstuck":
        # A device failed to connect and the USB port stopped responding:
        # nothing plugged in is noticed until the Pi restarts (dock_usbport.py).
        header(f"{warn}USB NOT WORKING", inverted=True)
        lines([("Restart the dock:", body_f),
               ("unplug its power briefly", small_f),
               ("Players aren't noticed until then", small_f),
               (f"Seen {now}", small_f)])

    elif screen == "failed":
        header(f"{warn}SYNC FAILED", inverted=True)
        lines([
            ("Safe to remove", body_f),
            (fit(args.reason or "Unknown error", body_f), body_f),
            (f"No plays lost \u00b7 {now}" if has_glyphs     # on the player or
             else f"No plays lost - {now}", small_f),       # queued on the Pi
        ])

    elif screen == "starting":
        # Boot progress (dock_boot.py). Until the clock is set from the
        # network, uptime stands in for the time so the screen visibly moves.
        dots = "…" if has_glyphs else "..."
        header(f"STARTING UP{dots}", inverted=False)
        ip = args.ip if args.ip is not None else read_ip()
        rows = [
            ("Wi-Fi connected" if ip else f"Connecting to Wi-Fi{dots}", body_f),
            (f"Clock set: {now}" if args.clock_synced else f"Setting clock{dots}", body_f),
        ]
        if args.uptime is not None:
            mins, secs = divmod(args.uptime, 60)
            rows.append((f"Up {mins}m {secs:02d}s" if mins else f"Up {secs}s", small_f))
        lines(rows)
        d.text((8, 106), f"IP {ip}" if ip else "No IP yet", font=small_f, fill=0)

    elif screen == "offline":
        # Boot gave up waiting for the network (dock_boot.py keeps retrying).
        # No network time either way, so uptime stands in for the time.
        up = ""
        if args.uptime is not None:
            mins, secs = divmod(args.uptime, 60)
            up = f"Up {mins}m {secs:02d}s"
        ip = args.ip if args.ip is not None else read_ip()
        if args.problem == "clock":
            header(f"{warn}CLOCK NOT SET", inverted=True)
            rows = [("Can't reach a time server", body_f),
                    ("Is NTP (UDP port 123) blocked?", small_f),
                    (f"Still trying · {up}" if up else "Still trying", small_f)]
        else:
            ssid = f"“{args.ssid}”" if args.ssid else "Wi-Fi"
            header(f"{warn}NO WI-FI", inverted=True)
            rows = [(fit(f"Can't join {ssid}", body_f), body_f),
                    (fit("Still trying. To troubleshoot, use", small_f), small_f),
                    ("the serial console (115200 baud).", small_f)]
        lines(rows)
        # Bottom-left: IP when there is one (uptime is already above), else uptime.
        bottom = f"IP {ip}" if ip and args.problem == "clock" else up
        d.text((8, 106), bottom, font=small_f, fill=0)

    elif screen == "preparing":
        count = f"  {args.done}/{args.total}" if args.total else ""
        header(f"PREPARING{count}", inverted=False)
        rows = [("Player safe to remove", body_f)]
        if args.detail:
            rows.append((fit(args.detail, body_f), body_f))
        rows.append((f"Updated {now}", small_f))
        lines(rows)
        if args.total:
            progress_bar(args.done or 0, args.total)

    else:  # idle
        header("DOCK READY", inverted=False)
        # A multi-line detail (the last-sync summary, see rest_main) takes
        # the place of "Waiting for player", leaving room for the IP line.
        detail = args.detail.split("\n") if args.detail else []
        if args.pending_add is not None:
            # What the next sync will do to the docked iPod (prepare job).
            rows = [(pending_line(args, has_glyphs), body_f)]
            if (args.pending_add or args.pending_remove) and conversions_line(args):
                rows.append((conversions_line(args), small_f))
            if args.total and (args.ready or 0) < args.total:
                rows.append((f"{args.ready or 0}/{args.total} tracks prepared", small_f))
            if args.last_sync:
                last = datetime.fromtimestamp(args.last_sync).strftime("%-I:%M %p")
                rows.append((fit(f"Last sync {last} \u00b7 updated {now}", small_f), small_f))
            else:
                rows.append((f"Since {now}", small_f))
            detail = []
        elif args.total:
            rows = [(f"Next sync: {args.ready or 0}/{args.total} tracks ready", body_f)]
        elif len(detail) < 2:
            rows = [("Waiting for player", body_f)]
        else:
            rows = []
        if args.pending_add is None:
            rows += [(fit(line, body_f), body_f) for line in detail]
            rows.append((f"Powered on {now}" if args.boot else f"Since {now}", small_f))
        lines(rows)
        ip = args.ip if args.ip is not None else read_ip()
        if ip:
            d.text((8, 106), f"IP {ip}", font=small_f, fill=0)

    wifi_indicator(args.wifi if args.wifi is not None else read_wifi())
    if dark_mode():
        # Dark mode: white on black, to suit a dark enclosure. E-ink holds
        # either colour without power, so this adds no wear.
        img = Image.eval(img, lambda p: 255 - p)
    return img


_display = None


def display_settings():
    """[display] from config.toml (e.g. invert = true). Read directly so the
    screen works even before the server settings are filled in; root-run
    services find the file via MUSICDOCK_CONFIG."""
    global _display
    if _display is None:
        path = os.path.expanduser(
            os.environ.get("MUSICDOCK_CONFIG", "~/.config/musicdock/config.toml"))
        try:
            with open(path, "rb") as f:
                _display = tomllib.load(f).get("display", {})
        except (OSError, tomllib.TOMLDecodeError):
            _display = {}
    return _display


def dark_mode():
    """[display] theme = "dark" (or "light"); configs from before themes
    used invert = true, which still counts when no theme is set."""
    d = display_settings()
    if d.get("theme") in ("light", "dark"):
        return d["theme"] == "dark"
    return bool(d.get("invert"))


# --------------------------------------------------------------- display ----
# Waveshare's 2.13" e-Paper HAT comes in versions with different controllers
# ([display] model; the version is printed on the back of the screen). V4 is
# the tested one. V3 works the same way as V4. V2 sets partial mode in init()
# and handles the panel's previous-image RAM itself; untested on hardware,
# so it gets full refreshes only (a flash per update, always correct).
PANEL_DRIVERS = {"v4": "epd2in13_V4", "v3": "epd2in13_V3", "v2": "epd2in13_V2"}


def panel_model():
    model = str(display_settings().get("model", "v4")).strip().lower()
    return model if model in PANEL_DRIVERS else "v4"


class Panel:
    """One interface over the V2/V3/V4 drivers (only what SongDock uses)."""

    def __init__(self, model=None, epd=None):
        self.model = model or panel_model()
        if epd is None:                       # the real driver (tests pass a fake)
            import importlib
            sys.path.insert(0, EPD_LIB)
            epd = importlib.import_module(
                f"waveshare_epd.{PANEL_DRIVERS[self.model]}").EPD()
        self.epd = epd
        self.partial_ok = self.model != "v2"

    def getbuffer(self, img):
        return self.epd.getbuffer(img)

    def init(self):
        if self.model == "v2":
            self.epd.init(self.epd.FULL_UPDATE)
        else:
            self.epd.init()

    def display(self, buf):
        self.epd.display(buf)

    def display_base(self, buf):
        """A full refresh that partial updates then build on."""
        self.epd.displayPartBaseImage(buf)

    def display_partial(self, buf, previous=None):
        """Only the changed pixels. `previous`: what the panel shows now, put
        into its old-image RAM (0x26) first when this process didn't draw it."""
        if previous is not None:
            self.epd.send_command(0x26)
            self.epd.send_data2(previous)
        self.epd.displayPartial(buf)

    def sleep(self):
        self.epd.sleep()


def open_panel():
    return Panel()


def panel_buffer(epd, img):
    # Panel is natively portrait (122x250); rotate our landscape image.
    # 270 (not 90) matches how the HAT sits on this Pi - confirmed on hardware.
    return epd.getbuffer(img.rotate(270, expand=True))


class PartialUpdates:
    """A short burst of in-place updates, used by the boot screen.

    The first frame is a full refresh that becomes the base image; later
    frames change only the pixels that differ (no black/white flash). The
    driver's sleep() also closes SPI, so the panel can't sleep between
    partial frames: it stays awake for the burst and always sleeps on exit,
    even after an error. Keep bursts short (the boot screen's is <= 5 min).

    With preview_dir set, frames are saved as numbered PNGs instead.
    """

    def __init__(self, preview_dir=None):
        self.preview_dir = preview_dir
        self.epd = None
        self.frames = 0

    def __enter__(self):
        if not self.preview_dir:
            self._lock = panel_lock()
            self._lock.__enter__()
            self.epd = open_panel()
            self.epd.init()
        return self

    def show(self, img, name="frame"):
        self.frames += 1
        if self.preview_dir:
            img.save(os.path.join(self.preview_dir, f"{self.frames:02d}-{name}.png"))
            return
        buf = panel_buffer(self.epd, img)
        forget_frame()
        if not self.epd.partial_ok:
            self.epd.display(buf)               # V2: full refresh every frame
        elif self.frames == 1:
            self.epd.display_base(buf)
        else:
            self.epd.display_partial(buf)
        save_frame(img)

    def __exit__(self, *exc):
        try:
            if self.epd:
                self.epd.sleep()  # never leave the panel powered
        finally:
            if not self.preview_dir:
                self._lock.__exit__(None, None, None)


def frame_path():
    """The last image sent to the panel, so the next short-lived process can
    do a partial (flash-free) update from it. On the Pi it lives in RAM
    (/run/musicdock, made at boot by install.sh's tmpfiles rule): it changes
    every progress update, and after a restart a full refresh is right
    anyway. Elsewhere (tests, the Mac) it sits next to the state file."""
    if os.path.dirname(STATE_FILE) == SHARED_STATE_DIR and os.path.isdir(RUNTIME_DIR):
        return os.path.join(RUNTIME_DIR, "dock_status.frame.png")
    return os.path.splitext(STATE_FILE)[0] + ".frame.png"


def save_frame(img):
    folder = os.path.dirname(frame_path())
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".frame.", suffix=".png")
    os.close(fd)
    img.convert("1").save(tmp)
    os.chmod(tmp, 0o644)               # root's sync and the login user share it
    os.replace(tmp, frame_path())


def load_frame(size):
    """The image on the panel now, or None if unknown (then: full refresh)."""
    try:
        with Image.open(frame_path()) as f:
            img = f.convert("1")
        return img if img.size == size else None
    except (OSError, ValueError):
        return None


def forget_frame():
    try:
        os.remove(frame_path())
    except OSError:
        pass


def draw_on_panel(img, full_refresh):
    """Draw `img`. A full refresh flashes the panel and clears ghosting;
    otherwise only the changed pixels update (no flash), starting from the
    saved last frame. Without a saved frame it falls back to a full refresh.

    A partial update compares the panel's "old" RAM (0x26) with the new
    image (0x24); deep sleep doesn't keep a trustworthy copy across
    processes, so the saved frame is written into 0x26 first.
    """
    epd = open_panel()
    previous = None if full_refresh or not epd.partial_ok else load_frame(img.size)
    buf = panel_buffer(epd, img)
    forget_frame()                     # unknown if the draw fails midway
    try:
        epd.init()
        if previous is not None:
            epd.display_partial(buf, panel_buffer(epd, previous))
        else:
            epd.display(buf)
    finally:
        epd.sleep()  # never leave the panel powered; it can be damaged
    save_frame(img)


# ------------------------------------------------------------------ main ----
def build_parser():
    p = argparse.ArgumentParser(description="Dock e-ink status screen")
    p.add_argument("screen", choices=["idle", "preparing", "syncing", "safe",
                                      "failed", "starting", "offline", "notsynced", "wifi", "wififail",
                                      "setup", "setupfail", "prepsync",
                                      "connecting", "usbstuck", "restoring", "safehours", "nospace"])
    p.add_argument("--step", type=int, help="syncing: current step")
    p.add_argument("--part-done", type=int,
                   help="syncing: items done within this step (fills the bar)")
    p.add_argument("--part-total", type=int, help="syncing: items in this step")
    p.add_argument("--done", type=int, help="preparing: tracks converted so far")
    p.add_argument("--ready", type=int, help="idle: tracks ready for next sync")
    p.add_argument("--total", type=int)
    p.add_argument("--detail")
    p.add_argument("--added", type=int, default=0)
    p.add_argument("--removed", type=int, default=0)
    p.add_argument("--plays", type=int, default=0)
    p.add_argument("--no-plays", action="store_true",
                   help="safe: the player keeps no play counts (generic players)")
    p.add_argument("--synced", type=int, help="prepsync: tracks copied so far this run")
    p.add_argument("--batches", type=int,
                   help="prepsync: syncs so far this run (kept in the state file, not shown)")
    p.add_argument("--to-copy", type=int,
                   help="prepsync: tracks that must reach the player (default --total)")
    p.add_argument("--restored", type=int,
                   help="prepsync: a restore's songs on the player so far (with --restore-total)")
    p.add_argument("--restore-total", type=int, help="prepsync: a restore's songs in all")
    p.add_argument("--left", type=int, metavar="SECONDS",
                   help="restoring: estimated time left")
    p.add_argument("--device", choices=["stock", "rockbox", "generic"],
                   help="syncing/prepsync: the player type, shown as an icon")
    p.add_argument("--pending-add", type=int,
                   help="idle: tracks the next sync will add to the iPod")
    p.add_argument("--pending-remove", type=int, default=0,
                   help="idle: tracks the next sync will remove")
    p.add_argument("--pending-lossless", type=int, default=0,
                   help="idle: songs the next sync swaps for lossless files")
    p.add_argument("--pending-format", type=int, default=0,
                   help="idle: songs the next sync swaps for another format")
    p.add_argument("--swaps-no-room", type=int, default=0,
                   help="idle: format swaps waiting for room on the player")
    p.add_argument("--not-added", type=int, default=0,
                   help="nospace: new songs that didn't fit on the player")
    p.add_argument("--last-sync", type=float, metavar="EPOCH",
                   help="idle: when the iPod was last synced")
    p.add_argument("--queued", type=int, default=0,
                   help="safe: plays still waiting for the server")
    p.add_argument("--reason")
    p.add_argument("--boot", action="store_true",
                   help="idle: say the dock just powered on (dock_boot.py)")
    p.add_argument("--preview", metavar="PNG", help="save a PNG instead of drawing")
    p.add_argument("--wifi", type=lambda v: "off" if v == "off" else int(v),
                   help="preview only: fake Wi-Fi reading, e.g. -56 or off")
    p.add_argument("--ip", help="preview only: fake IP address")
    p.add_argument("--at", type=float, metavar="EPOCH",
                   help="show this time instead of now (used by --refresh)")
    p.add_argument("--force", action="store_true", help="ignore rate limiting")
    p.add_argument("--uptime", type=int, help="starting: seconds since power-on")
    p.add_argument("--clock-synced", action="store_true",
                   help="starting: the clock has been set from the network")
    p.add_argument("--problem", choices=["wifi", "clock"], default="wifi",
                   help="offline: what's missing")
    p.add_argument("--ssid", help="offline/wifi: the Wi-Fi network")
    p.add_argument("--server", choices=["yes", "no", "unknown"], default="unknown",
                   help="wifi: whether Navidrome is reachable on the new network")
    p.add_argument("--only-over", metavar="SCREENS",
                   help="draw only if the current screen is one of these "
                        "(comma-separated; 'none' = nothing drawn yet)")
    return p


def content_args(argv):
    """argv minus the drawing-control options, to redraw the same screen later."""
    out, skip = [], 0
    for a in argv:
        if skip:
            skip -= 1
        elif a in CONTROL_OPTIONS:
            skip = CONTROL_OPTIONS[a]
        else:
            out.append(a)
    return out


def wifi_connected(reading):
    return isinstance(reading, int)          # dBm when connected; "off"/None otherwise


def main(argv=None):
    """Entry point. dock_sync.py and dock_boot.py call this with an argv list."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw)
    if args.wifi is None:
        args.wifi = read_wifi()              # read once: drawn and recorded
    if args.screen in SYNC_SCREENS and args.device is None and not args.preview:
        # Redraws that don't know the player (e.g. the prepare job's PREPARE &
        # SYNC updates) keep the icon a sync screen already shows.
        before = load_state()
        if before.get("screen") in SYNC_SCREENS and "--device" in (before.get("argv") or []):
            argv_before = before["argv"]
            args.device = argv_before[argv_before.index("--device") + 1]
            raw += ["--device", args.device]

    img = render(args.screen, args)

    if args.preview:
        img.save(args.preview)
        print(f"Saved preview to {args.preview}")
        return

    with panel_lock():
        state = load_state()
        now = time.time()
        same_screen = state.get("screen") == args.screen

        # Checked under the lock, so another job can't change the screen
        # between this check and the draw.
        if args.only_over:
            allowed = set(args.only_over.split(","))
            if (state.get("screen") or "none") not in allowed:
                print(f"Leaving {state.get('screen')} up (not drawing {args.screen})")
                return

        # The prepare job (idle/preparing) must never cover the "do not remove"
        # warning. If a sync crashed, SYNCING stays up until the next sync runs.
        if (state.get("screen") in SYNC_SCREENS and args.screen in PREPARE_SCREENS
                and not args.force):
            return

        # Progress-only redraws are rate-limited.
        if (same_screen and args.screen in PROGRESS_SCREENS and not args.force
                and now - state.get("last_draw", 0) < MIN_PROGRESS_INTERVAL):
            return

        count = state.get("fast_count", 0) + 1
        full = count >= FULL_REFRESH_EVERY or not same_screen
        draw_on_panel(img, full_refresh=full)
        save_state({"screen": args.screen, "last_draw": now,
                    "fast_count": 0 if full else count,
                    # For --refresh: what was drawn, when, and the Wi-Fi state.
                    "argv": content_args(raw), "drawn_at": args.at or now,
                    "wifi_on": wifi_connected(args.wifi)})


def refresh_main(argv=None, attempts=3, settle=3):
    """Redraw the resting screen if Wi-Fi connected or dropped since it was drawn.

    Started by musicdock-wifi-refresh.service, which NetworkManager's
    dispatcher triggers on wlan0 up/down. Keeps the screen's text and
    original time; only the Wi-Fi indicator (and idle's IP) change. Checks
    again after drawing in case the connection changed meanwhile.
    """
    for _ in range(attempts):
        with panel_lock():
            state = load_state()
            screen = state.get("screen")
            if screen not in REFRESHABLE or not state.get("argv"):
                print(f"Not refreshing the {screen} screen")
                return
            wifi = read_wifi()
            if wifi is None:
                print("Can't read Wi-Fi here")
                return
            if state.get("wifi_on") == wifi_connected(wifi):
                print("Wi-Fi indicator already current")
                return
            args = build_parser().parse_args(
                state["argv"] + ["--wifi", str(wifi), "--at", str(state["drawn_at"])])
            count = state.get("fast_count", 0) + 1
            full = count >= FULL_REFRESH_EVERY
            draw_on_panel(render(args.screen, args), full_refresh=full)
            save_state({**state, "last_draw": time.time(),
                        "fast_count": 0 if full else count,
                        "wifi_on": wifi_connected(wifi)})
            print(f"Redrew {screen}: Wi-Fi {'connected' if wifi_connected(wifi) else 'off'}")
        time.sleep(settle)


REST_MODES = {
    # 10 min after a sync; FAILED, WI-FI UNCHANGED and SETUP NOT USED stay up
    # until unplugged.
    "timer": "safe,notsynced,wifi,setup",
    "unplug": "safe,nospace,failed,notsynced,wifi,wififail,setup,setupfail",  # player physically removed
}
MIN_SHOWN = 60   # seconds SAFE/FAILED stay up before an unplug swaps them


def rest_main(argv=None, sleep=time.sleep):
    """Replace a finished SAFE (or FAILED) screen with DOCK READY plus a
    summary of the last sync. Started by the unplug udev rule and by the
    timer the sync job sets. The only-over check happens under the panel
    lock, so a sync that started meanwhile is never covered."""
    p = argparse.ArgumentParser(description="Back to DOCK READY after a sync")
    p.add_argument("--mode", choices=sorted(REST_MODES), default="unplug")
    args = p.parse_args(argv)
    allowed = REST_MODES[args.mode]
    state = load_state()
    screen = state.get("screen")
    if screen not in allowed.split(","):
        print(f"Leaving the {screen} screen up")
        return
    drawn_at = state.get("drawn_at") or time.time()
    if args.mode == "unplug":
        # Some players drop the USB link right after an eject; keep the
        # result readable for a minute either way.
        wait = MIN_SHOWN - (time.time() - drawn_at)
        if wait > 0:
            sleep(wait)
    if screen == "notsynced":
        main(["idle", "--only-over", allowed])          # nothing to summarize
        return
    prev = build_parser().parse_args(state.get("argv") or [screen])
    when = datetime.fromtimestamp(drawn_at).strftime("%-I:%M %p")
    if screen in ("safe", "nospace", "wifi", "wififail", "setup", "setupfail"):
        plays = "no play counts" if prev.no_plays else f"{prev.plays} plays"
        detail = (f"Last synced {when}\n"
                  f"+{prev.added}  \u2212{prev.removed} tracks \u00b7 {plays}")
    else:
        detail = f"Last sync failed {when}"
    main(["idle", "--detail", detail, "--only-over", allowed])


def stream_main(argv=None):
    """Child-process side of a partial-update burst (used by dock_boot.py).

    Reads one JSON list of screen arguments per line from stdin, e.g.
    ["starting", "--uptime", "90"], and draws each as a partial update.
    At end of input the panel sleeps and the process exits, which releases
    the GPIO pins (see dock_sync.Screen for why that matters).
    """
    p = argparse.ArgumentParser(description="Partial-update burst from stdin")
    p.add_argument("--preview-dir", help="save frames as PNGs instead of drawing")
    args = p.parse_args(argv)
    parser = build_parser()
    with PartialUpdates(args.preview_dir) as panel:
        if not args.preview_dir:
            # So the next normal draw knows the screen changed and does a
            # full refresh (clearing any partial-update ghosting).
            save_state({"screen": "starting", "last_draw": time.time(),
                        "fast_count": 0})
        for line in sys.stdin:
            frame = parser.parse_args(json.loads(line))
            panel.show(render(frame.screen, frame), frame.screen)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--partial-stream"]:
        stream_main(sys.argv[2:])
    elif sys.argv[1:2] == ["--refresh"]:
        refresh_main(sys.argv[2:])
    elif sys.argv[1:2] == ["--rest"]:
        rest_main(sys.argv[2:])
    else:
        main()
