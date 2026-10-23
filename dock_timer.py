#!/usr/bin/env python3
"""
dock_timer.py - apply [sync] check_interval_minutes from config.toml to the
prepare job's timer (how often the dock checks Navidrome for new music).

The timer is a systemd unit, which only root can change, so this runs as
root: from musicdock-settings.service, which musicdock-settings.path starts
whenever config.toml changes (edited by hand or by a DockSetup.txt), and
once from install.sh. It writes a drop-in that replaces the timer's
OnUnitInactiveSec, and reloads systemd only when the value changed.

Reads just that one setting (not the whole config through dock_config), so
a half-finished config elsewhere doesn't stop it; a missing or invalid value
means the default, 5 minutes.
"""

import logging
import os
import subprocess
import sys
import tomllib

log = logging.getLogger("dock_timer")

TIMER = "musicdock-prepare.timer"
DROPIN_DIR = f"/etc/systemd/system/{TIMER}.d"
DROPIN = "50-check-interval.conf"
DEFAULT = 5
LIMITS = (1, 1440)


def check_interval(config_path):
    """Minutes between checks from config.toml, or DEFAULT."""
    try:
        with open(os.path.expanduser(config_path), "rb") as f:
            value = tomllib.load(f).get("sync", {}).get("check_interval_minutes", DEFAULT)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("Can't read %s (%s): using %d minutes", config_path, e, DEFAULT)
        return DEFAULT
    if isinstance(value, int) and not isinstance(value, bool) \
            and LIMITS[0] <= value <= LIMITS[1]:
        return value
    log.warning("check_interval_minutes = %r isn't 1-1440: using %d minutes", value, DEFAULT)
    return DEFAULT


def dropin_text(minutes):
    # The empty assignment clears the unit's own 5min before setting ours
    # (timer settings add up otherwise).
    return ("# Written by dock_timer.py from config.toml ([sync] check_interval_minutes).\n"
            "# Edit config.toml, not this file.\n"
            "[Timer]\n"
            "OnUnitInactiveSec=\n"
            f"OnUnitInactiveSec={minutes}min\n")


def apply(config_path, dropin_dir=DROPIN_DIR, run=subprocess.run):
    """Write the drop-in if the interval changed, then reload and restart the
    timer. Returns (minutes, changed)."""
    minutes = check_interval(config_path)
    text = dropin_text(minutes)
    path = os.path.join(dropin_dir, DROPIN)
    try:
        with open(path) as f:
            if f.read() == text:
                return minutes, False
    except OSError:
        pass
    os.makedirs(dropin_dir, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    run(["systemctl", "daemon-reload"], check=True)
    run(["systemctl", "restart", TIMER], check=True)
    log.info("The dock now checks for new music every %d minute(s)", minutes)
    return minutes, True


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    path = os.environ.get("MUSICDOCK_CONFIG", "~/.config/musicdock/config.toml")
    minutes, changed = apply(path)
    if not changed:
        log.info("Check interval unchanged (%d minute(s))", minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
