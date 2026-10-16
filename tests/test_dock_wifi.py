"""Tests for wifi.txt handling (fake NetworkManager; no real Wi-Fi touched).

Run: python3 -m unittest discover -s tests -v
"""

import logging
import os
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_wifi  # noqa: E402
from dock_wifi import WifiFileError, parse_wifi_file  # noqa: E402

PASSWORD = "correct horse battery"


class FakeNM:
    """Just enough nmcli: profiles, one active Wi-Fi connection, visible networks."""

    def __init__(self, networks):
        self.networks = networks                  # ssid -> password ("" = open)
        self.profiles = {"netplan-wlan0-Home": {"ssid": "Home", "psk": "homepass"}}
        self.active = "netplan-wlan0-Home"
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        a = argv[1:]
        out = lambda s="", rc=0, err="": SimpleNamespace(stdout=s, returncode=rc, stderr=err)  # noqa
        if a[:6] == ["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"]:
            return out(f"{self.active}:wlan0\nlo:lo\n" if self.active else "lo:lo\n")
        if a[:4] == ["-g", "802-11-wireless.ssid", "connection", "show"]:
            return out(self.profiles.get(a[4], {}).get("ssid", ""))
        if a[:2] == ["connection", "load"]:
            text = open(a[2]).read()
            get = lambda k: next((l.split("=", 1)[1] for l in text.splitlines()  # noqa
                                  if l.startswith(k + "=")), "")
            self.profiles[get("id")] = {"ssid": get("ssid"), "psk": get("psk")}
            return out()
        if a[:1] == ["--wait"] and a[2:4] == ["connection", "up"]:
            p = self.profiles.get(a[4])
            if p is None:
                return out(rc=10, err="Error: unknown connection")
            if p["ssid"] not in self.networks:
                return out(rc=10, err=f"Error: No network with SSID '{p['ssid']}' found.")
            if self.networks[p["ssid"]] != p["psk"]:
                return out(rc=4, err="Error: Connection activation failed: "
                                     "Secrets were required, but not provided.")
            self.active = a[4]
            return out()
        if a[:4] == ["-g", "IP4.ADDRESS", "device", "show"]:
            return out("192.168.50.20/24" if self.active else "")
        if a[:2] == ["connection", "delete"]:
            self.profiles.pop(a[2], None)
            if self.active == a[2]:
                self.active = None
            return out()
        raise AssertionError(argv)


class ParseTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_wifi_file("SSID=Cafe\nPassword=12345678\n"),
                         {"ssid": "Cafe", "password": "12345678", "hidden": False})
        self.assertEqual(parse_wifi_file("ssid: Cafe Wifi\npassword : 12345678\nHidden=yes")
                         ["hidden"], True)
        self.assertEqual(parse_wifi_file("SSID=Open")["password"], "")

    def test_bad_files(self):
        for text in ("Password=12345678", "SSID=x\nPassword=short", ""):
            with self.assertRaises(WifiFileError):
                parse_wifi_file(text)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "ipod")
        self.keyfiles = os.path.join(tmp.name, "nm")
        os.makedirs(self.root)
        os.makedirs(self.keyfiles)
        self.nm = FakeNM({"Home": "homepass", "Cafe": PASSWORD})
        self.wifi = dock_wifi.Wifi(run=self.nm, keyfile_dir=self.keyfiles)

    def write(self, text, name="wifi.txt"):
        with open(os.path.join(self.root, name), "w") as f:
            f.write(text)

    def apply(self, **kw):
        return dock_wifi.apply_wifi_file(self.root, wifi=self.wifi, **kw)

    def test_success_switches_and_deletes_file(self):
        self.write(f"SSID=Cafe\nPassword={PASSWORD}\n")
        result = self.apply(server_check=lambda: True)
        self.assertEqual((result["ok"], result["ssid"], result["server"]), (True, "Cafe", True))
        self.assertEqual(self.nm.profiles[self.nm.active]["ssid"], "Cafe")
        self.assertIsNone(dock_wifi.find_wifi_file(self.root))
        self.assertIn("netplan-wlan0-Home", self.nm.profiles)       # old network kept

    def test_wrong_password_goes_back_and_keeps_file(self):
        self.write("SSID=Cafe\nPassword=wrongwrong\n")
        result = self.apply()
        self.assertEqual((result["ok"], result["reason"]), (False, "Wrong password"))
        self.assertEqual(self.nm.active, "netplan-wlan0-Home")       # back on Home
        self.assertNotIn("musicdock: Cafe", self.nm.profiles)        # profile removed
        self.assertIsNotNone(dock_wifi.find_wifi_file(self.root))     # file left

    def test_unknown_network(self):
        self.write("WIFI.TXT content ignored\nSSID=Nowhere\nPassword=12345678", name="WiFi.TXT")
        result = self.apply()
        self.assertEqual(result["reason"], "Network not found")
        self.assertEqual(self.nm.active, "netplan-wlan0-Home")

    def test_already_connected_just_removes_file(self):
        self.write("SSID=Home\nPassword=homepass\n")
        result = self.apply()
        self.assertEqual((result["ok"], result["reason"]), (True, "Already connected"))
        self.assertIsNone(dock_wifi.find_wifi_file(self.root))

    def test_bad_file_reported_and_kept(self):
        self.write("Password=12345678\n")
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertIn("no SSID", result["reason"])
        self.assertIsNotNone(dock_wifi.find_wifi_file(self.root))

    def test_password_never_on_command_line_or_in_log(self):
        self.write(f"SSID=Cafe\nPassword={PASSWORD}\n")
        logging.disable(logging.NOTSET)          # other test modules silence logging
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs("dock_wifi", level=logging.DEBUG) as logs:
            self.apply()
        self.assertFalse(any(PASSWORD in " ".join(c) for c in self.nm.calls))
        self.assertFalse(any(PASSWORD in line for line in logs.output))

    def test_keyfile_is_root_only_and_preferred(self):
        self.write(f"SSID=Cafe\nPassword={PASSWORD}\n")
        self.apply()
        [name] = os.listdir(self.keyfiles)
        path = os.path.join(self.keyfiles, name)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        text = open(path).read()
        self.assertIn("autoconnect-priority=10", text)
        self.assertIn(f"psk={PASSWORD}", text)

    def test_no_file_no_action(self):
        self.assertIsNone(self.apply())
        self.assertEqual(self.nm.calls, [])


if __name__ == "__main__":
    unittest.main()
