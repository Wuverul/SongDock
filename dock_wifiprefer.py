#!/usr/bin/env python3
"""
dock_wifiprefer.py - go back to the preferred Wi-Fi network once it's around.

NetworkManager joins the best saved network it can reach, but once
connected it stays put: after a hiccup on the preferred network it falls
back to another saved one and never returns by itself. On 2026-10-25 the
dock lost its network (an access point a few feet away, wifi.txt gave it
priority 10) for a minute, joined the fallback (two floors down, priority
0) and stayed there; downloads ran at 0.15 MB/s instead of ~2.5 MB/s until
someone noticed the next day.

musicdock-wifiprefer.timer runs this as root every 5 minutes. If a saved
network with a higher priority than the current one is visible with a
usable signal, it switches to it - never during a sync. If that fails, the
previous network is brought back and that network isn't tried again for
RETRY_AFTER. It only uses saved profiles: no password is read or logged.

Uses the standard library only (runs under the system python3).
"""

import json
import logging
import os
import re
import subprocess
import sys
import time

import dock_wifi

log = logging.getLogger("dock_wifiprefer")

STATE = "/run/musicdock/wifiprefer.json"
MIN_SIGNAL = 40           # NetworkManager's 0-100 scale (~ -70 dBm): usable, not marginal
RETRY_AFTER = 30 * 60     # seconds before a network that failed to join is tried again


def split_terse(line):
    """Fields of an `nmcli -t` line (':' separates; '\\:' is a literal colon)."""
    return [f.replace("\\:", ":").replace("\\\\", "\\")
            for f in re.split(r"(?<!\\):", line)]


def saved_networks(wifi):
    """[{name, ssid, priority}] of saved Wi-Fi profiles that connect by themselves."""
    r = wifi.nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY",
                   "connection", "show")
    out = []
    for line in r.stdout.splitlines():
        fields = split_terse(line)
        if len(fields) != 4 or fields[1] != "802-11-wireless" or fields[2] != "yes":
            continue
        try:
            priority = int(fields[3])
        except ValueError:
            continue
        ssid = wifi.nmcli("-g", "802-11-wireless.ssid", "connection", "show",
                          fields[0]).stdout.strip()
        if ssid:
            out.append({"name": fields[0], "ssid": ssid, "priority": priority})
    return out


def visible_networks(wifi):
    """{ssid: best signal} from a scan (a recent one is reused)."""
    r = wifi.nmcli("-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "--rescan", "auto")
    seen = {}
    for line in r.stdout.splitlines():
        fields = split_terse(line)
        if len(fields) != 2 or not fields[0]:
            continue
        try:
            seen[fields[0]] = max(seen.get(fields[0], 0), int(fields[1]))
        except ValueError:
            pass
    return seen


def better_network(current, saved, visible, failed, now):
    """The saved network to switch to, or None: higher priority than the
    current one, visible with a usable signal, not failed recently."""
    current_priority = next((s["priority"] for s in saved if s["name"] == current), None)
    if current_priority is None:
        return None                  # not on a saved profile we know: leave it alone
    choices = [s for s in saved
               if s["priority"] > current_priority
               and visible.get(s["ssid"], 0) >= MIN_SIGNAL
               and (s["name"] not in failed or now - failed[s["name"]] >= RETRY_AFTER)]
    return max(choices, key=lambda s: (s["priority"], visible[s["ssid"]]), default=None)


def sync_running():
    out = subprocess.run(["systemctl", "list-units", "--state=active,activating",
                          "--no-legend", "--plain", "musicdock-sync@*"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def check(wifi=None, state_path=STATE, now=None, busy=sync_running, settle=time.sleep):
    """One run. Returns the network switched to, or None."""
    wifi = wifi or dock_wifi.Wifi()
    now = time.time() if now is None else now
    if busy():
        return None
    current, current_ssid = wifi.active()
    if current is None:
        return None                  # not connected: NetworkManager is already trying
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    failed = state.setdefault("failed", {})
    saved = saved_networks(wifi)
    pick = better_network(current, saved, visible_networks(wifi), failed, now)
    if pick is None:
        return None
    log.warning("Switching Wi-Fi from %s to %s, which is preferred and now in range",
                current_ssid, pick["ssid"])
    joined = wifi.up(pick["name"])
    if joined:
        for _ in range(10):                     # DHCP may lag activation
            if wifi.has_address():
                break
            settle(1)
        else:
            joined = False
    if joined:
        failed.pop(pick["name"], None)
        result = pick["ssid"]
    else:
        failed[pick["name"]] = now
        log.warning("Couldn't join %s; back to %s (trying again in %d minutes)",
                    pick["ssid"], current_ssid, RETRY_AFTER // 60)
        if not wifi.up(current):
            log.warning("Couldn't rejoin %s either; NetworkManager will pick a network",
                        current_ssid)
        result = None
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(state_path + ".tmp", state_path)
    except OSError:
        pass
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
