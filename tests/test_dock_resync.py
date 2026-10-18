"""Tests for re-plugging a docked iPod. Run: python3 -m unittest discover -s tests -v"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_prepare  # noqa: E402
import dock_resync  # noqa: E402

GUID = "000A2700000000AB"


class FakeSys:
    """A tiny /sys: USB devices, their disks, and /sys/class/block links."""

    def __init__(self, root):
        self.usb = os.path.join(root, "bus", "usb", "devices")
        self.block = os.path.join(root, "class", "block")
        self.devices = os.path.join(root, "devices")
        for d in (self.usb, self.block, self.devices):
            os.makedirs(d)

    def device(self, name, vendor="05ac", serial=GUID, disk_size=None):
        real = os.path.join(self.devices, name)
        os.makedirs(real)
        for f, v in (("idVendor", vendor), ("serial", serial), ("authorized", "1")):
            if v is not None:
                with open(os.path.join(real, f), "w") as fh:
                    fh.write(v + "\n")
        os.symlink(real, os.path.join(self.usb, name))
        if disk_size is not None:
            letter = "abcdefgh"[len(os.listdir(self.block))]
            disk = os.path.join(real, f"{name}:1.0", "host0", "block", f"sd{letter}")
            os.makedirs(disk)
            with open(os.path.join(disk, "size"), "w") as fh:
                fh.write(f"{disk_size}\n")
            os.symlink(disk, os.path.join(self.block, f"sd{letter}"))
        return real



class ResyncTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sys = FakeSys(os.path.join(tmp.name, "sys"))
        self.record = os.path.join(tmp.name, "dock.db")
        db = sqlite3.connect(self.record)
        db.execute("CREATE TABLE players (guid TEXT, model TEXT, first_seen INT, last_sync INT)")
        db.execute("INSERT INTO players VALUES (?, 'xB903', 1, 2)", (GUID,))
        db.commit()
        db.close()
        self.request = os.path.join(tmp.name, "resync.request")
        self.running = False
        self.sleeps = []

    def resync(self):
        return dock_resync.resync(self.sys.usb, self.sys.block, self.record,
                                  sync_running=lambda: self.running,
                                  sleep=self.sleeps.append)

    def authorized(self, real):
        with open(os.path.join(real, "authorized")) as f:
            return f.read().strip()

    def test_ejected_known_ipod_is_replugged(self):
        real = self.sys.device("1-1", disk_size=0)          # ejected: disk size 0
        self.assertEqual(self.resync(), 1)
        self.assertEqual(self.authorized(real), "1")         # taken back afterwards
        self.assertEqual(self.sleeps, [dock_resync.REPLUG_PAUSE])

    def test_replug_drops_then_restores(self):
        real = self.sys.device("1-1", disk_size=0)
        seen = []
        dock_resync.replug(real, sleep=lambda s: seen.append(self.authorized(real)))
        self.assertEqual((seen, self.authorized(real)), (["0"], "1"))

    def test_left_alone_when_disk_is_up(self):
        self.sys.device("1-1", disk_size=31559176)          # mounted or about to be
        self.assertEqual(self.resync(), 0)

    def test_left_alone_during_a_sync(self):
        self.sys.device("1-1", disk_size=0)
        self.running = True
        self.assertEqual(self.resync(), 0)
        self.assertEqual(self.sleeps, [])

    def test_unknown_ipod_and_other_devices_left_alone(self):
        self.sys.device("1-1", serial="000A270000000002", disk_size=0)   # someone else's
        self.sys.device("1-2", vendor="0781", serial="STICK", disk_size=0)  # a USB stick
        self.assertEqual(self.resync(), 0)

    def test_no_record_means_no_known_players(self):
        os.remove(self.record)
        self.sys.device("1-1", disk_size=0)
        self.assertEqual(self.resync(), 0)

    def test_request_file(self):
        dock_resync.request(self.request)
        self.assertTrue(os.path.exists(self.request))
        self.assertEqual(oct(os.stat(self.request).st_mode & 0o777), "0o644")


class PrepareRequestTests(unittest.TestCase):
    def test_has_pending(self):
        base = ["idle", "--ready", "5", "--total", "5"]
        self.assertFalse(dock_prepare.has_pending(base))
        self.assertFalse(dock_prepare.has_pending(
            base + ["--pending-add", "0", "--pending-remove", "0", "--last-sync", "1"]))
        self.assertTrue(dock_prepare.has_pending(
            base + ["--pending-add", "3", "--pending-remove", "0", "--last-sync", "1"]))
        self.assertTrue(dock_prepare.has_pending(
            base + ["--pending-add", "0", "--pending-remove", "2", "--last-sync", "1"]))


if __name__ == "__main__":
    unittest.main()
