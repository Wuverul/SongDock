"""
dock_safehours.py - "Safe Hours": a daily window with no syncs.

[sync] safe_hours = "07:00-10:00" (local time; "" = off; may wrap past
midnight, e.g. "22:00-06:00"). User request, 2026-10-26: grab the iPod
before a morning commute without a sync holding it on the dock.

During the window no sync starts: an iPod docked then isn't mounted (so it
stays usable while it charges), and the auto re-sync waits. A sync that is
running when the window opens stops copying, finishes (playlists, the
database save) and unmounts. Either way a one-off timer reconnects a
still-docked iPod when the window ends (dock_resync.py --after-safe-hours).
Preparing new music (downloads, conversion) carries on: it doesn't touch
the iPod.

Standard library only (also used by the system-python3 services).
"""

import os
import re
import subprocess
from datetime import datetime, timedelta

AFTER_UNIT = "musicdock-after-safe-hours"
_TIME = r"(\d{1,2})(?::(\d{2}))?"


class SafeHoursError(ValueError):
    pass


def parse(text):
    """(start, end) in minutes after midnight, or None for off.
    Accepts "07:00-10:00", "7-10", "7:30 - 10" ("" / "off" / "none" = off)."""
    text = (text or "").strip().lower()
    if text in ("", "off", "none", "no"):
        return None
    m = re.fullmatch(_TIME + r"\s*-\s*" + _TIME, text)
    if not m:
        raise SafeHoursError(f'safe hours must look like "07:00-10:00", not {text!r}')
    h1, m1, h2, m2 = (int(g) if g else 0 for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise SafeHoursError(f"safe hours: {text!r} isn't a time of day")
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        raise SafeHoursError("safe hours: start and end are the same")
    return start, end


def text(window):
    """"07:00-10:00" for a window (as written back to config.toml)."""
    if not window:
        return ""
    (s, e) = window
    return f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}"


def _at(day, minutes):
    return datetime(day.year, day.month, day.day) + timedelta(minutes=minutes)


def active(window, now=None):
    """True if `now` (local, naive datetime) is inside the window."""
    if not window:
        return False
    now = now or datetime.now()
    start, end = window
    minute = now.hour * 60 + now.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def window_end(window, now=None):
    """When the window `now` is in ends (local datetime)."""
    now = now or datetime.now()
    start, end = window
    when = _at(now, end)
    return when if when > now else when + timedelta(days=1)


def next_start(window, now=None):
    """When the window next opens after `now` (local datetime), or None."""
    if not window:
        return None
    now = now or datetime.now()
    when = _at(now, window[0])
    return when if when > now else when + timedelta(days=1)


def from_config(cfg):
    """The window set in a loaded config (dock_config validated it)."""
    try:
        return parse(cfg.get("sync", {}).get("safe_hours", ""))
    except SafeHoursError:
        return None


def clock_text(when):
    return when.strftime("%-I:%M %p")


def schedule_after(window, script, python=None, now=None, run=subprocess.run):
    """One-off timer: run `script --after-safe-hours` (dock_resync.py) when
    the window ends, to sync an iPod still docked. Needs root (systemd-run).
    Returns True if scheduled now or already scheduled."""
    when = window_end(window, now)
    env = [f"--setenv={k}={os.environ[k]}" for k in ("MUSICDOCK_CONFIG", "EPD_LIB")
           if k in os.environ]
    r = run(["systemd-run", "--unit", AFTER_UNIT, "--collect", *env,
             "--on-calendar", when.strftime("%Y-%m-%d %H:%M:%S"),
             "--timer-property=AccuracySec=5s", "--timer-property=RemainAfterElapse=no",
             python or "/usr/bin/python3", script, "--after-safe-hours"],
            capture_output=True, text=True)
    return r.returncode == 0 or "already" in (r.stderr or "")
