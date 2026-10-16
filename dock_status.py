#!/usr/bin/env python3
"""
dock_status.py - e-ink status screen for the music player dock.

Target: Waveshare 2.13" e-Paper HAT V4 (250x122) on a Raspberry Pi Zero W.

Usage:
  dock_status.py idle [--ready 38 --total 40] [--boot] [--detail "..."]
  dock_status.py preparing --done 12 --total 40 --detail "Converting: Artist - Song"
  dock_status.py syncing --step 3 --total 6 --detail "Copying tracks 12/40"
  dock_status.py safe --added 14 --removed 9 --plays 38
  dock_status.py failed --reason "Server unreachable"
  dock_status.py starting --uptime 75 [--clock-synced]   # boot progress
  dock_status.py offline --problem wifi --ssid "Home" --uptime 300

Two jobs drive the screen:
  * prepare job (scheduled, never touches the player): idle, preparing
  * sync job (runs when the player docks):              syncing, safe, failed

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
  * The prepare job can never draw over SYNCING. Only the sync job (or --force)
    can replace that screen.
  * Progress redraws (syncing, preparing) are rate-limited to protect the
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
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- config ----
WIDTH, HEIGHT = 250, 122            # landscape
EPD_LIB = os.environ.get(
    "EPD_LIB",
    os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/lib"),
)
# Every job (sync and boot run as root; manual runs and the prepare job as the
# login user) must share one state file, or the "never draw over SYNCING"
# check can't see another job's screen. pi/install.sh creates the shared
# folder; elsewhere (e.g. the Mac) we fall back to ~/.cache.
SHARED_STATE_DIR = "/var/lib/musicdock"
STATE_FILE = os.environ.get("DOCK_STATE_FILE") or (
    os.path.join(SHARED_STATE_DIR, "dock_status.json")
    if os.path.isdir(SHARED_STATE_DIR)
    else os.path.expanduser("~/.cache/dock_status.json"))
MIN_PROGRESS_INTERVAL = 30          # seconds between progress-only redraws
FULL_REFRESH_EVERY = 10             # fast refreshes before a full anti-ghost refresh

PROGRESS_SCREENS = {"syncing", "preparing"}   # rate-limited when repeated
PREPARE_SCREENS = {"idle", "preparing"}       # drawn by the prepare job
# Resting screens --refresh may redraw when Wi-Fi connects or drops. Active
# screens (syncing, preparing, boot) redraw often enough on their own.
REFRESHABLE = {"idle", "safe", "failed"}
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
def syncing_progress(args):
    """(done, total) for the SYNCING bar: progress within the current stage
    when it has its own count (e.g. tracks copied, --part-done/--part-total),
    else which stage of how many. The header always shows the stage."""
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

    def header(text, inverted):
        if inverted:
            d.rectangle((0, 0, WIDTH, 32), fill=0)
            d.text((8, 5), text, font=title_f, fill=255)
        else:
            d.rectangle((0, 0, WIDTH - 1, 32), outline=0, width=2)
            d.text((8, 5), text, font=title_f, fill=0)

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

    if screen == "syncing":
        step = f"  {args.step}/{args.total}" if args.step and args.total else ""
        header(f"SYNCING{step}", inverted=True)
        rows = [("Do not remove player", body_f)]
        if args.detail:
            rows.append((fit(args.detail, body_f), body_f))
        rows.append((f"Updated {now}", small_f))
        lines(rows)
        done, total = syncing_progress(args)
        if total:
            progress_bar(done, total)

    elif screen == "safe":
        header(f"{check}SAFE TO REMOVE", inverted=False)
        stats = f"+{args.added}  \u2212{args.removed} tracks \u00b7 {args.plays} plays sent" \
            if has_glyphs else f"+{args.added} -{args.removed} tracks, {args.plays} plays sent"
        rows = [(stats, body_f), (f"Synced {now}", body_f)]
        if args.queued:
            rows.append((f"{args.queued} plays queued (server offline)", small_f))
        lines(rows)

    elif screen == "failed":
        header(f"{warn}SYNC FAILED", inverted=True)
        lines([
            ("Safe to remove", body_f),
            (fit(args.reason or "Unknown error", body_f), body_f),
            (f"Plays kept on device \u00b7 {now}" if has_glyphs
             else f"Plays kept on device - {now}", small_f),
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
        if args.total:
            rows = [(f"Next sync: {args.ready or 0}/{args.total} tracks ready", body_f)]
        else:
            rows = [("Waiting for player", body_f)]
        if args.detail:
            rows.append((fit(args.detail, body_f), body_f))
        rows.append((f"Powered on {now}" if args.boot else f"Since {now}", small_f))
        lines(rows)
        ip = args.ip if args.ip is not None else read_ip()
        if ip:
            d.text((8, 106), f"IP {ip}", font=small_f, fill=0)

    wifi_indicator(args.wifi if args.wifi is not None else read_wifi())
    return img


# --------------------------------------------------------------- display ----
def open_panel():
    sys.path.insert(0, EPD_LIB)
    from waveshare_epd import epd2in13_V4  # noqa: E402
    return epd2in13_V4.EPD()


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
        if self.frames == 1:
            self.epd.displayPartBaseImage(buf)
        else:
            self.epd.displayPartial(buf)

    def __exit__(self, *exc):
        try:
            if self.epd:
                self.epd.sleep()  # never leave the panel powered
        finally:
            if not self.preview_dir:
                self._lock.__exit__(None, None, None)


def draw_on_panel(img, full_refresh):
    epd = open_panel()
    buf = panel_buffer(epd, img)

    fast = not full_refresh and hasattr(epd, "init_fast") and hasattr(epd, "display_fast")
    try:
        if fast:
            epd.init_fast()
            epd.display_fast(buf)
        else:
            epd.init()
            epd.display(buf)
    finally:
        epd.sleep()  # never leave the panel powered; it can be damaged


# ------------------------------------------------------------------ main ----
def build_parser():
    p = argparse.ArgumentParser(description="Dock e-ink status screen")
    p.add_argument("screen", choices=["idle", "preparing", "syncing", "safe",
                                      "failed", "starting", "offline"])
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
    p.add_argument("--ssid", help="offline: the Wi-Fi network being joined")
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
        if (state.get("screen") == "syncing" and args.screen in PREPARE_SCREENS
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
    "timer": "safe",            # 10 min after a sync: SAFE only (FAILED stays up)
    "unplug": "safe,failed",    # player physically removed
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
    prev = build_parser().parse_args(state.get("argv") or [screen])
    when = datetime.fromtimestamp(drawn_at).strftime("%-I:%M %p")
    if screen == "safe":
        detail = f"Last synced {when} (+{prev.added} \u2212{prev.removed}, {prev.plays} plays)"
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
