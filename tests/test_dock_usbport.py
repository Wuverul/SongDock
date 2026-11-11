"""Tests for noticing a USB port left dead by a failed plug-in.

Run: python3 -m unittest discover -s tests -v
"""

import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import dock_usbport  # noqa: E402

logging.disable(logging.CRITICAL)

# The kernel's messages from the failed plug-in on 2026-10-25 (19:55).
GAVE_UP = [
    "songdock kernel: usb 1-1: new full-speed USB device number 6 using dwc_otg",
    "songdock kernel: usb 1-1: device descriptor read/64, error -71",
    "songdock kernel: usb 1-1: device descriptor read/64, error -71",
    "songdock kernel: usb 1-1: new full-speed USB device number 7 using dwc_otg",
    "songdock kernel: usb 1-1: device descriptor read/64, error -71",
    "songdock kernel: usb usb1-port1: attempt power cycle",
]
IPOD = [
    "songdock kernel: usb 1-1: new high-speed USB device number 3 using dwc_otg",
    "songdock kernel: usb 1-1: New USB device found, idVendor=05ac, idProduct=1263",
]
UNPLUG = ["songdock kernel: usb 1-1: USB disconnect, device number 2"]


class UsbPortTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = os.path.join(tmp.name, "run", "usbport.json")
        self.pending = []           # kernel lines not read yet: (time, text)
        self.shown = []
        self.sync = False

    def kernel(self, at, lines):
        self.pending += [(at, text) for text in lines]

    def read_log(self, cursor):
        lines, self.pending = self.pending, []
        return lines, "c"

    def check(self, now):
        return dock_usbport.check(self.state, now=now, read_log=self.read_log,
                                  show=lambda: self.shown.append(now),
                                  busy=lambda: self.sync)

    def test_normal_plugging_never_shows_anything(self):
        self.kernel(0, IPOD)
        self.kernel(50, UNPLUG)
        for now in range(0, 600, 30):
            self.assertIsNone(self.check(now))
        self.assertEqual(self.shown, [])

    def test_a_port_that_gave_up_is_reported_after_a_minute_of_silence(self):
        self.kernel(100, GAVE_UP)
        self.assertIsNone(self.check(110))        # the kernel may still be trying
        self.assertIsNone(self.check(140))
        self.assertEqual(self.check(170), "usb1")
        for now in range(200, 2000, 30):          # once per failure
            self.assertIsNone(self.check(now))
        self.assertEqual(self.shown, [170])

    def test_a_device_connecting_after_the_trouble_means_nothing_to_report(self):
        self.kernel(100, GAVE_UP)
        self.check(110)
        self.kernel(120, IPOD)                    # replugged and it worked
        for now in range(140, 400, 30):
            self.assertIsNone(self.check(now))
        self.assertEqual(self.shown, [])

    def test_never_reported_during_a_sync(self):
        self.kernel(100, GAVE_UP)
        self.sync = True
        self.assertIsNone(self.check(200))
        self.sync = False
        self.assertEqual(self.check(230), "usb1")

    def test_a_new_failure_after_the_port_worked_again_is_reported_again(self):
        self.kernel(0, GAVE_UP)
        self.check(60)
        self.kernel(70, IPOD)
        self.check(90)
        self.kernel(100, GAVE_UP)
        self.assertEqual(self.check(160), "usb1")
        self.assertEqual(self.shown, [60, 160])

    def test_never_touches_the_usb_controller(self):
        # Rebinding the controller's driver crashed the kernel (2026-10-25).
        with open(dock_usbport.__file__) as f:
            code = f.read()
        for word in ("unbind", "authorized", "/disable"):
            self.assertNotIn(f'"{word}', code.replace("'", '"'))


if __name__ == "__main__":
    unittest.main()


# The kernel's messages when the Wi-Fi chip stopped answering (2026-11-10 23:32), every 6 s.
WIFI_DEAD = [
    "songdock kernel: ieee80211 phy0: brcmf_proto_bcdc_query_dcmd: brcmf_proto_bcdc_msg "
    "failed w/status -110",
    "songdock kernel: ieee80211 phy0: brcmf_cfg80211_dump_station: BRCMF_C_GET_ASSOCLIST "
    "failed, err=-110",
]


class WifiWatchTests(UsbPortTests):
    def setUp(self):
        super().setUp()
        self.reboots, self.updating = [], False
        self.reboot_state = os.path.join(os.path.dirname(self.state), "lib", "wifiwatch.json")

    def check(self, now):
        return dock_usbport.check(self.state, now=now, read_log=self.read_log,
                                  show=lambda: self.shown.append(now),
                                  busy=lambda: self.sync, reboot=lambda: self.reboots.append(now),
                                  reboot_state=self.reboot_state,
                                  updating=lambda: self.updating)

    def dead_for(self, start, seconds):
        for t in range(start, start + seconds + 1, 6):
            self.kernel(t, WIFI_DEAD)

    def test_a_wifi_chip_that_stays_silent_restarts_the_dock_once(self):
        self.dead_for(1800001000, 120)
        self.check(1800001125)
        self.assertEqual(self.reboots, [])                  # 2 minutes: not yet
        self.dead_for(1800001126, 70)
        self.check(1800001200)
        self.assertEqual(self.reboots, [1800001200])
        self.dead_for(1800001201, 300)                             # still dead after it
        self.check(1800001510)
        self.assertEqual(self.reboots, [1800001200])               # at most once an hour

    def test_a_glitch_that_recovers_restarts_nothing(self):
        # 2026-11-11 00:12: one burst of errors, then the Wi-Fi carried on
        self.dead_for(1800001000, 30)
        self.check(1800001040)
        self.check(1800001300)
        self.dead_for(1800001400, 30)                              # another short one later
        self.check(1800001440)
        self.assertEqual(self.reboots, [])

    def test_never_during_a_sync_or_an_update(self):
        self.dead_for(1800001000, 200)
        self.sync = True
        self.check(1800001200)
        self.sync, self.updating = False, True
        self.check(1800001201)
        self.assertEqual(self.reboots, [])
        self.updating = False
        self.dead_for(1800001202, 6)
        self.check(1800001210)
        self.assertEqual(self.reboots, [1800001210])
