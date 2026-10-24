#!/usr/bin/env python3
"""
dock_boot.py - the screens shown while the Pi powers on.

musicdock-boot.service runs this once per boot, as early as it can (about a
minute after power-on on a Pi Zero; Wi-Fi often takes another 2 minutes).
The sync service is ordered after it, so a player docked at power-on isn't
mounted until the network is up (a sync sends plays to the server).

1. STARTING UP: drawn right away, then updated in place about every TICK
   seconds (partial refresh, no flash) with Wi-Fi/IP, signal strength, the
   clock once it's set, and uptime, so it never looks frozen.
2. Ready = an IP, a Wi-Fi reading, and network time (the Pi Zero has no clock
   battery; until it reaches a time server its clock shows roughly when it
   last shut down). Then a full refresh to the final screen:
     * normal boot: DOCK READY, "Powered on <time>"
     * last screen was SYNCING or PREPARE & SYNC: a sync was cut off by a power loss or reboot.
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
import json
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
    if last_screen in ("syncing", "prepsync"):
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
    from dock_sync import DOCK_STATUS, Screen

    # Screen draws all happen in separate dock_status.py processes, so the
    # panel's GPIO pins are released between them (see dock_sync.Screen).
    screen = Screen(args.preview_dir)     # also clears old preview frames

    # Read this before anything overwrites it.
    last = dock_status.load_state().get("screen")
    log.info("Last screen before boot: %s", last)

    def read():
        return {"ip": dock_status.read_ip(), "wifi": dock_status.read_wifi(),
                "clock": clock_synced()}

    def draw(screen_argv):
        log.info("Drawing: %s", " ".join(screen_argv))
        try:
            screen.show(*screen_argv)
        except Exception:
            log.exception("Screen update failed (continuing)")

    readings, gave_up = None, True
    cmd = [sys.executable, DOCK_STATUS, "--partial-stream"]
    if args.preview_dir:
        cmd += ["--preview-dir", args.preview_dir]
    burst = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
    try:
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
            try:
                burst.stdin.write(json.dumps(a) + "\n")
                burst.stdin.flush()
                screen.count += 1         # keep preview numbering in step
            except (BrokenPipeError, ValueError):
                # The drawing process died (it logged why). Keep checking
                # the network; only the screen updates are lost.
                pass

        readings, gave_up = starting_phase(show, read)
    except Exception:
        log.exception("STARTING UP screen failed; checking the network without it")
    finally:
        try:
            burst.stdin.close()           # end of input: panel sleeps, child exits
            burst.wait(timeout=120)
        except Exception:
            burst.kill()
        if burst.returncode:
            log.warning("STARTING UP drawing process exited %s", burst.returncode)

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
