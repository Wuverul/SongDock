#!/usr/bin/env python3
"""
dock_status.py - e-ink status screen for the music player dock.

Target: Waveshare 2.13" e-Paper HAT V4 (250x122) on a Raspberry Pi Zero W.

Usage:
  dock_status.py idle [--ready 38 --total 40]
  dock_status.py preparing --done 12 --total 40 --detail "Converting: Artist - Song"
  dock_status.py syncing --step 3 --total 6 --detail "Copying tracks 12/40"
  dock_status.py safe --added 14 --removed 9 --plays 38
  dock_status.py failed --reason "Server unreachable"

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
import json
import os
import socket
import sys
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- config ----
WIDTH, HEIGHT = 250, 122            # landscape
EPD_LIB = os.environ.get(
    "EPD_LIB",
    os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/lib"),
)
STATE_FILE = os.path.expanduser("~/.cache/dock_status.json")
MIN_PROGRESS_INTERVAL = 30          # seconds between progress-only redraws
FULL_REFRESH_EVERY = 10             # fast refreshes before a full anti-ghost refresh

PROGRESS_SCREENS = {"syncing", "preparing"}   # rate-limited when repeated
PREPARE_SCREENS = {"idle", "preparing"}       # drawn by the prepare job

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
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


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
def render(screen, args):
    img = Image.new("1", (WIDTH, HEIGHT), 255)  # 255 = white
    d = ImageDraw.Draw(img)
    title_f, has_glyphs = load_font(FONT_BOLD, 20)
    body_f, _ = load_font(FONT_REG, 13)
    small_f, _ = load_font(FONT_REG, 11)
    now = datetime.now().strftime("%-I:%M %p")

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
        if args.step and args.total:
            progress_bar(args.step, args.total)

    elif screen == "safe":
        header(f"{check}SAFE TO REMOVE", inverted=False)
        stats = f"+{args.added}  \u2212{args.removed} tracks \u00b7 {args.plays} plays sent" \
            if has_glyphs else f"+{args.added} -{args.removed} tracks, {args.plays} plays sent"
        lines([(stats, body_f), (f"Synced {now}", body_f)])

    elif screen == "failed":
        header(f"{warn}SYNC FAILED", inverted=True)
        lines([
            ("Safe to remove", body_f),
            (fit(args.reason or "Unknown error", body_f), body_f),
            (f"Plays kept on device \u00b7 {now}" if has_glyphs
             else f"Plays kept on device - {now}", small_f),
        ])

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
        rows.append((f"Since {now}", small_f))
        lines(rows)
        ip = args.ip if args.ip is not None else read_ip()
        if ip:
            d.text((8, 106), f"IP {ip}", font=small_f, fill=0)

    wifi_indicator(args.wifi if args.wifi is not None else read_wifi())
    return img


# --------------------------------------------------------------- display ----
def draw_on_panel(img, full_refresh):
    sys.path.insert(0, EPD_LIB)
    from waveshare_epd import epd2in13_V4  # noqa: E402

    epd = epd2in13_V4.EPD()
    # Panel is natively portrait (122x250); rotate our landscape image.
    # 270 (not 90) matches how the HAT sits on this Pi - confirmed on hardware.
    buf = epd.getbuffer(img.rotate(270, expand=True))

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
def main():
    p = argparse.ArgumentParser(description="Dock e-ink status screen")
    p.add_argument("screen", choices=["idle", "preparing", "syncing", "safe", "failed"])
    p.add_argument("--step", type=int, help="syncing: current step")
    p.add_argument("--done", type=int, help="preparing: tracks converted so far")
    p.add_argument("--ready", type=int, help="idle: tracks ready for next sync")
    p.add_argument("--total", type=int)
    p.add_argument("--detail")
    p.add_argument("--added", type=int, default=0)
    p.add_argument("--removed", type=int, default=0)
    p.add_argument("--plays", type=int, default=0)
    p.add_argument("--reason")
    p.add_argument("--preview", metavar="PNG", help="save a PNG instead of drawing")
    p.add_argument("--wifi", type=lambda v: "off" if v == "off" else int(v),
                   help="preview only: fake Wi-Fi reading, e.g. -56 or off")
    p.add_argument("--ip", help="preview only: fake IP address")
    p.add_argument("--force", action="store_true", help="ignore rate limiting")
    args = p.parse_args()

    img = render(args.screen, args)

    if args.preview:
        img.save(args.preview)
        print(f"Saved preview to {args.preview}")
        return

    state = load_state()
    now = time.time()
    same_screen = state.get("screen") == args.screen

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
                "fast_count": 0 if full else count})


if __name__ == "__main__":
    main()
