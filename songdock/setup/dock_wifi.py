"""
dock_wifi.py - change the dock's Wi-Fi from a `wifi.txt` on a docked iPod.

    SSID=New Network
    Password=the password        (omit for an open network)
    Hidden=yes                   (optional: network doesn't broadcast its name)

Rules (the sync job calls apply_wifi_file after the music is done):
  * Only from players that opted in (MediaDock.txt) and only in write mode,
    because a successful change deletes wifi.txt.
  * The password goes into a root-only NetworkManager keyfile, never onto a
    command line (visible to other processes) or into the log.
  * The current network stays saved; the new one gets a higher priority.
  * Joined + got an address = success: wifi.txt is deleted. Otherwise the
    previous network is brought back, the new profile removed, and wifi.txt
    left in place with the reason shown on screen.
"""

import logging
import os
import re
import subprocess
import time

log = logging.getLogger("dock_wifi")

WIFI_FILE = "wifi.txt"
IFACE = "wlan0"
KEYFILE_DIR = "/etc/NetworkManager/system-connections"
PRIORITY = 10            # above the existing profiles (0), so it's preferred
JOIN_TIMEOUT = 60        # seconds


class WifiFileError(Exception):
    pass


def find_wifi_file(root):
    """Path of wifi.txt in the player's top folder (any case), or None."""
    try:
        for name in os.listdir(root):
            if name.lower() == WIFI_FILE:
                return os.path.join(root, name)
    except OSError:
        pass
    return None


def parse_wifi_file(text):
    """{"ssid", "password", "hidden"} from 'Key=Value' / 'Key: Value' lines."""
    values = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z]+)\s*[:=]\s*(.*?)\s*$", line)
        if m:
            values[m.group(1).lower()] = m.group(2)
    ssid = values.get("ssid", "")
    if not ssid:
        raise WifiFileError("no SSID line")
    if len(ssid.encode()) > 32:
        raise WifiFileError("SSID longer than 32 bytes")
    password = values.get("password", "")
    if password and not 8 <= len(password) <= 63:
        raise WifiFileError("password must be 8-63 characters")
    hidden = values.get("hidden", "").lower() in ("yes", "true", "1")
    return {"ssid": ssid, "password": password, "hidden": hidden}


def keyfile_text(name, ssid, password, hidden):
    def esc(v):              # keyfile values: escape backslashes and newlines
        return v.replace("\\", "\\\\").replace("\n", "\\n")
    lines = ["[connection]", f"id={esc(name)}", "type=wifi", f"interface-name={IFACE}",
             "autoconnect=true", f"autoconnect-priority={PRIORITY}", "",
             "[wifi]", "mode=infrastructure", f"ssid={esc(ssid)}",
             f"hidden={'true' if hidden else 'false'}", ""]
    if password:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={esc(password)}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def friendly_reason(stderr):
    s = stderr.lower()
    if "secrets were required" in s or "no secrets" in s or "4-way handshake" in s:
        return "Wrong password"
    if "no network with ssid" in s or "not found" in s or "ssid not found" in s:
        return "Network not found"
    if "timeout" in s or "timed out" in s:
        return "Timed out joining"
    first = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
    return first.replace("Error: ", "")[:60]


class Wifi:
    """NetworkManager operations (run is injectable for tests)."""

    def __init__(self, run=subprocess.run, keyfile_dir=KEYFILE_DIR):
        self.run = run
        self.keyfile_dir = keyfile_dir

    def nmcli(self, *args, timeout=JOIN_TIMEOUT + 15):
        return self.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)

    def active(self):
        """(connection name, ssid) active on the Wi-Fi interface, or (None, None)."""
        r = self.nmcli("-t", "-f", "NAME,DEVICE", "connection", "show", "--active")
        for line in r.stdout.splitlines():
            name, _, dev = line.rpartition(":")
            if dev == IFACE:
                name = name.replace("\\:", ":")
                ssid = self.nmcli("-g", "802-11-wireless.ssid", "connection", "show",
                                  name).stdout.strip()
                return name, ssid
        return None, None

    def has_address(self):
        r = self.nmcli("-g", "IP4.ADDRESS", "device", "show", IFACE)
        return bool(r.stdout.strip())

    def join(self, ssid, password, hidden):
        """Add a keyfile profile and bring it up. Returns (ok, reason, name)."""
        name = f"musicdock: {ssid}"
        path = os.path.join(self.keyfile_dir,
                            "musicdock-" + re.sub(r"[^A-Za-z0-9_-]", "_", ssid) + ".nmconnection")
        old = os.umask(0o077)                      # root-only: holds the password
        try:
            with open(path, "w") as f:
                f.write(keyfile_text(name, ssid, password, hidden))
                f.flush()
                os.fsync(f.fileno())
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        load = self.nmcli("connection", "load", path)
        if load.returncode != 0:
            os.remove(path)
            return False, friendly_reason(load.stderr), name
        up = self.nmcli("--wait", str(JOIN_TIMEOUT), "connection", "up", name)
        if up.returncode != 0:
            return False, friendly_reason(up.stderr), name
        for _ in range(10):                        # DHCP may lag activation
            if self.has_address():
                return True, "", name
            time.sleep(1)
        return False, "No address from the network", name

    def remove(self, name):
        self.nmcli("connection", "delete", name)

    def up(self, name):
        return self.nmcli("--wait", str(JOIN_TIMEOUT), "connection", "up", name).returncode == 0


def apply_wifi_file(root, wifi=None, server_check=None):
    """Handle wifi.txt on a mounted, writable, opted-in player.

    Returns None if there's no wifi.txt, else a result dict:
    {"ok", "ssid", "reason", "server"} (server: True/False/None reachability).
    """
    path = find_wifi_file(root)
    if not path:
        return None
    wifi = wifi or Wifi()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            settings = parse_wifi_file(f.read())
    except WifiFileError as e:
        log.warning("Bad wifi.txt: %s (left on the player)", e)
        return {"ok": False, "ssid": "", "reason": f"Bad wifi.txt: {e}", "server": None}

    ssid = settings["ssid"]
    prev_name, prev_ssid = wifi.active()
    if prev_ssid == ssid:
        log.info("Already on %r; removing wifi.txt", ssid)
        _delete(path)
        return {"ok": True, "ssid": ssid, "reason": "Already connected", "server": None}

    log.info("Trying Wi-Fi %r from wifi.txt (was on %r)", ssid, prev_ssid)
    ok, reason, name = wifi.join(ssid, settings["password"], settings["hidden"])
    if not ok:
        log.warning("Couldn't join %r: %s; going back to %r", ssid, reason, prev_ssid)
        wifi.remove(name)
        if prev_name:
            wifi.up(prev_name)
        return {"ok": False, "ssid": ssid, "reason": reason, "server": None}

    _delete(path)
    server = server_check() if server_check else None
    log.info("Now on %r (Navidrome reachable: %s); wifi.txt removed", ssid, server)
    return {"ok": True, "ssid": ssid, "reason": "", "server": server}


def _delete(path):
    os.remove(path)
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)                                # make the deletion stick (FAT)
    finally:
        os.close(fd)
