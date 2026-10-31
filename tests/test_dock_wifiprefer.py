"""Tests for returning to the preferred Wi-Fi network (fake NetworkManager).

Run: python3 -m unittest discover -s tests -v
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import dock_wifi  # noqa: E402
import dock_wifiprefer as wp  # noqa: E402

logging.disable(logging.CRITICAL)
PREFERRED, FALLBACK = "musicdock: Near AP", "netplan-wlan0-Downstairs"


class FakeNM:
    """Saved profiles (as nmcli -t prints them, colons escaped), the active
    one, what a scan sees, and which networks can be joined."""

    def __init__(self, active=FALLBACK, visible=None, joinable=("Near AP", "Downstairs")):
        self.profiles = {PREFERRED: ("Near AP", 10), FALLBACK: ("Downstairs", 0),
                         "Wired connection 1": (None, 0)}
        self.active = active
        self.visible = {"Near AP": 80, "Downstairs": 35} if visible is None else visible
        self.joinable = set(joinable)
        self.ups = []

    def __call__(self, argv, **kw):
        a = argv[1:]
        out = lambda s="", rc=0: SimpleNamespace(stdout=s, returncode=rc, stderr="")  # noqa
        esc = lambda s: s.replace(":", "\\:")                                         # noqa
        if a[:6] == ["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"]:
            return out(f"{esc(self.active)}:wlan0\n" if self.active else "")
        if a[:4] == ["-t", "-f", "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY", "connection"]:
            rows = [f"{esc(n)}:{'802-11-wireless' if ssid else '802-3-ethernet'}:yes:{p}"
                    for n, (ssid, p) in self.profiles.items()]
            return out("\n".join(rows) + "\n")
        if a[:4] == ["-g", "802-11-wireless.ssid", "connection", "show"]:
            return out(self.profiles.get(a[4], ("", 0))[0] or "")
        if a[:4] == ["-t", "-f", "SSID,SIGNAL", "device"]:
            return out("".join(f"{esc(s)}:{v}\n" for s, v in self.visible.items()) + ":55\n")
        if a[:1] == ["--wait"] and a[2:4] == ["connection", "up"]:
            self.ups.append(a[4])
            if self.profiles[a[4]][0] in self.joinable:
                self.active = a[4]
                return out()
            self.active = None
            return out(rc=4)
        if a[:4] == ["-g", "IP4.ADDRESS", "device", "show"]:
            return out("192.168.1.20/24" if self.active else "")
        raise AssertionError(argv)


class WifiPreferTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, "run", "wifiprefer.json")

    def check(self, nm, now=1000, busy=False):
        return wp.check(dock_wifi.Wifi(run=nm), self.state, now=now,
                        busy=lambda: busy, settle=lambda s: None)

    def test_goes_back_to_the_preferred_network(self):
        nm = FakeNM()
        self.assertEqual(self.check(nm), "Near AP")
        self.assertEqual((nm.active, nm.ups), (PREFERRED, [PREFERRED]))

    def test_stays_when_already_on_the_preferred_network(self):
        nm = FakeNM(active=PREFERRED)
        self.assertIsNone(self.check(nm))
        self.assertEqual(nm.ups, [])

    def test_stays_when_the_preferred_network_is_out_of_range_or_weak(self):
        for visible in ({"Downstairs": 60}, {"Near AP": 30, "Downstairs": 60}):
            nm = FakeNM(visible=visible)
            self.assertIsNone(self.check(nm))
            self.assertEqual(nm.ups, [])

    def test_never_during_a_sync(self):
        nm = FakeNM()
        self.assertIsNone(self.check(nm, busy=True))
        self.assertEqual(nm.ups, [])

    def test_a_failed_switch_goes_back_and_waits_before_trying_again(self):
        nm = FakeNM(joinable=("Downstairs",))
        self.assertIsNone(self.check(nm, now=1000))
        self.assertEqual((nm.active, nm.ups), (FALLBACK, [PREFERRED, FALLBACK]))
        with open(self.state) as f:
            self.assertEqual(json.load(f)["failed"], {PREFERRED: 1000})
        self.assertIsNone(self.check(nm, now=1000 + 10 * 60))      # too soon: not tried
        self.assertEqual(len(nm.ups), 2)
        nm.joinable.add("Near AP")
        self.assertEqual(self.check(nm, now=1000 + 31 * 60), "Near AP")

    def test_not_connected_or_unknown_profile_is_left_to_networkmanager(self):
        for active in (None, "some other profile"):
            nm = FakeNM(active=active)
            nm.profiles.setdefault("some other profile", ("Elsewhere", 0))
            if active:
                del nm.profiles[active]
            self.assertIsNone(self.check(nm))
            self.assertEqual(nm.ups, [])

    def test_terse_fields_keep_escaped_colons(self):
        self.assertEqual(wp.split_terse(r"musicdock\: Net:802-11-wireless:yes:10"),
                         ["musicdock: Net", "802-11-wireless", "yes", "10"])


if __name__ == "__main__":
    unittest.main()
