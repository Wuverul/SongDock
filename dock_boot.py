#!/usr/bin/env python3
"""
dock_boot.py - the screens shown while the Pi powers on.

musicdock-boot.service runs this once per boot, as early as it can (about a
minute after power-on on a Pi Zero; Wi-Fi often takes another 2 minutes).
The sync service is ordered after it, so a player docked at power-on isn't
mounted until the network is up (Phase 3's sync needs the server anyway).

1. STARTING UP: drawn right away, then updated in place about every TICK
   seconds (partial refresh, no flash) with Wi-Fi/IP, signal strength, the
   clock once it's set, and uptime, so it never looks frozen.
2. Ready = an IP, a Wi-Fi reading, and network time (the Pi Zero has no clock
   battery; until it reaches a time server its clock shows roughly when it
   last shut down). Then a full refresh to the final screen:
     * normal boot: DOCK READY, "Powered on <time>"
     * last screen was SYNCING: a sync was cut off by a power loss or reboot.
       After a restart nothing is mounted, and play data is only cleared once
       the server confirms it, so SYNCING is no longer true. Show FAILED
       ("Sync interrupted by restart") rather than hide what happened. If the
       player is still docked, udev starts a fresh sync next.
3. Not ready after GIVE_UP seconds: OFFLINE screen saying what's missing
   (Wi-Fi, naming the SSID, or network time) and how to troubleshoot. We keep
   checking every POLL seconds without touching the panel, and move to the
   final screen once the network works.

If the STARTING UP phase fails for any reason, we skip to step 3's checks.

Usage:
  dock_boot.py                         # on the Pi (the service runs this)
  dock_boot.py --preview-dir previews  # anywhere: save each frame as a PNG
"""

import argparse
import glob
import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("dock_boot")

HERE = os.path.dirname(os.path.abspath(__file__))
TICK = 10                 # seconds between STARTING UP updates
MIN_REST = 2              # if an update runs long, still pause this much
GIVE_UP = 5 * 60          # STARTING UP -> OFFLINE after this long
POLL = 15                 # seconds between network checks while OFFLINE


def clock_synced():
    """True once the clock is set from the network (True if we can't tell)."""
    if not shutil.which("timedatectl"):      # e.g. the Mac
        return True
    r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                       capture_output=True, text=True)
    return r.stdout.strip() == "yes"


def wifi_ssid():
    """SSID of the configured Wi-Fi network (from NetworkManager), or None."""
    if not shutil.which("nmcli"):
        return None
    r = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        name, _, kind = line.rpartition(":")
        if kind == "802-11-wireless":
            name = name.replace("\\:", ":").replace("\\\\", "\\")   # nmcli -t escapes
            s = subprocess.run(["nmcli", "-g", "802-11-wireless.ssid",
                                "connection", "show", name],
                               capture_output=True, text=True)
            return s.stdout.strip() or None
    return None


def uptime():
    """Seconds since power-on, or None if unknown (not Linux)."""
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def ready(r):
    """Everything the final screen needs: an IP, a Wi-Fi reading, the time.

    r["wifi"] is None when it can't be read at all (not a Pi), which
    shouldn't hold things up.
    """
    return bool(r["ip"]) and r["wifi"] != "off" and r["clock"]


def problem(r):
    """What an OFFLINE screen should blame: "wifi" or "clock"."""
    return "wifi" if not r["ip"] or r["wifi"] == "off" else "clock"


def starting_phase(show, read, sleep=time.sleep, clock=time.monotonic,
                   give_up=GIVE_UP, tick=TICK):
    """Show STARTING UP about every tick until ready() or give_up passes.

    Updates are scheduled from the start time, so slow updates (a busy CPU
    during boot) don't stretch the gap. Returns (readings, gave_up).
    """
    start = clock()
    n = 0
    while True:
        r = read()
        show(r)
        if ready(r):
            return r, False
        if clock() - start >= give_up:
            return r, True
        n += 1
        sleep(max(MIN_REST, start + n * tick - clock()))


def wait_for_network(read, show_problem, sleep=time.sleep, poll=POLL):
    """Check every poll seconds until ready(); redraw only when the problem changes."""
    shown = None
    while True:
        r = read()
        if ready(r):
            return r
        kind = problem(r)
        if kind != shown:
            show_problem(kind, r)
            shown = kind
        sleep(poll)


def boot_screen(last_screen, recovered=False):
    """Return the dock_status.py arguments for the final boot screen."""
    if last_screen == "syncing":
        argv = ["failed", "--reason", "Sync interrupted by restart"]
    elif recovered:
        argv = ["idle", "--detail", "Network connected"]   # "Since <time>"
    else:
        argv = ["idle", "--boot"]                            # "Powered on <time>"
    # --force: this is the one non-sync job allowed to replace a stale
    # SYNCING screen, because a reboot means no sync is running.
    return argv + ["--force"]


def main(argv=None):
    p = argparse.ArgumentParser(description="Draw the dock's boot screens")
    p.add_argument("--preview-dir", metavar="DIR",
                   help="save each frame as a PNG here instead of drawing")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    sys.path.insert(0, HERE)
    import dock_status

    if args.preview_dir:
        os.makedirs(args.preview_dir, exist_ok=True)
        for old in glob.glob(os.path.join(args.preview_dir, "[0-9][0-9]-*.png")):
            os.remove(old)

    # Read this before anything overwrites it.
    last = dock_status.load_state().get("screen")
    log.info("Last screen before boot: %s", last)

    def read():
        return {"ip": dock_status.read_ip(), "wifi": dock_status.read_wifi(),
                "clock": clock_synced()}

    frames = 0

    def draw(screen_argv):
        """Full-refresh draw via dock_status (panel, or the next preview PNG)."""
        nonlocal frames
        frames += 1
        if args.preview_dir:
            screen_argv = screen_argv + ["--preview", os.path.join(
                args.preview_dir, f"{frames:02d}-{screen_argv[0]}.png")]
        log.info("Drawing: %s", " ".join(screen_argv))
        dock_status.main(screen_argv)

    readings, gave_up = None, True
    try:
        with dock_status.PartialUpdates(args.preview_dir) as panel:
            if not args.preview_dir:
                # So the next normal draw knows the screen changed and does
                # a full refresh (clearing any partial-update ghosting).
                dock_status.save_state({"screen": "starting",
                                        "last_draw": time.time(), "fast_count": 0})

            def show(r):
                a = ["starting", "--ip", r["ip"] or ""]
                if r["wifi"] is not None:
                    a += ["--wifi", str(r["wifi"])]
                if r["clock"]:
                    a.append("--clock-synced")
                up = uptime()
                if up is not None:
                    a += ["--uptime", str(up)]
                log.info("Starting: ip=%s wifi=%s clock=%s up=%ss",
                         r["ip"], r["wifi"], "set" if r["clock"] else "waiting", up)
                img = dock_status.render("starting", dock_status.build_parser().parse_args(a))
                panel.show(img, "starting")

            readings, gave_up = starting_phase(show, read)
            frames = panel.frames
    except Exception:
        log.exception("STARTING UP screen failed; checking the network without it")

    if gave_up:
        ssid = wifi_ssid()

        def show_problem(kind, r):
            log.warning("Network not ready (%s): ip=%s wifi=%s clock=%s",
                        kind, r["ip"], r["wifi"], r["clock"])
            a = ["offline", "--problem", kind, "--ip", r["ip"] or "", "--force"]
            if r["wifi"] is not None:
                a += ["--wifi", str(r["wifi"])]
            if ssid:
                a += ["--ssid", ssid]
            up = uptime()
            if up is not None:
                a += ["--uptime", str(up)]
            draw(a)

        readings = wait_for_network(read, show_problem)
        log.info("Network ready: ip=%s wifi=%s", readings["ip"], readings["wifi"])

    draw(boot_screen(last, recovered=gave_up))


if __name__ == "__main__":
    main()
