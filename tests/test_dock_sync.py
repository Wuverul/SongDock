"""Tests for the sync job's ordering and safety rules.

Run from the project folder:  python3 -m unittest discover -s tests -v
"""

import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_sync  # noqa: E402
from dock_sync import FakeDevice, SyncError, run_sync  # noqa: E402

# The failure tests log tracebacks on purpose; keep test output readable.
logging.disable(logging.CRITICAL)


class RecordingDevice(FakeDevice):
    """FakeDevice that records mount/unmount into a shared event list."""

    def __init__(self, folder, events, fail=None):
        super().__init__(folder, fail=fail)
        self.events = events

    def mount(self):
        self.events.append("mount")
        super().mount()

    def unmount(self):
        self.events.append("unmount")
        super().unmount()


class SyncOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        with open(os.path.join(self.folder, "song.m4a"), "wb") as f:
            f.write(b"x" * 10)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def show(self, screen, *args, force=False):
        self.events.append(screen)

    def screens(self):
        return [e for e in self.events if e not in ("mount", "unmount")]

    def test_happy_path(self):
        dev = RecordingDevice(self.folder, self.events)
        self.assertTrue(run_sync(dev, self.show))
        self.assertEqual(self.events[0], "syncing")
        self.assertLess(self.events.index("syncing"), self.events.index("mount"))
        self.assertLess(self.events.index("unmount"), self.events.index("safe"))
        self.assertEqual(self.events[-1], "safe")
        self.assertFalse(dev.mounted)

    def test_no_mount_if_syncing_screen_fails(self):
        def broken_show(screen, *args, force=False):
            raise OSError("SPI error")
        dev = RecordingDevice(self.folder, self.events)
        self.assertFalse(run_sync(dev, broken_show))
        self.assertNotIn("mount", self.events)

    def test_mount_failure_shows_failed(self):
        dev = RecordingDevice(self.folder, self.events, fail="mount")
        self.assertFalse(run_sync(dev, self.show))
        self.assertNotIn("unmount", self.events)   # never mounted
        self.assertEqual(self.events[-1], "failed")

    def test_read_failure_still_unmounts(self):
        dev = RecordingDevice(self.folder, self.events)
        with mock.patch.object(dock_sync, "list_contents",
                               side_effect=OSError("I/O error")):
            self.assertFalse(run_sync(dev, self.show))
        self.assertIn("unmount", self.events)
        self.assertLess(self.events.index("unmount"), self.events.index("failed"))
        self.assertFalse(dev.mounted)

    def test_unmount_failure_never_says_safe(self):
        dev = RecordingDevice(self.folder, self.events, fail="unmount")
        self.assertFalse(run_sync(dev, self.show))
        self.assertNotIn("safe", self.screens())
        self.assertNotIn("failed", self.screens())   # also says "safe to remove"
        self.assertEqual(self.screens()[-1], "syncing")

    def test_progress_redraw_failure_does_not_abort(self):
        calls = []

        def flaky_show(screen, *args, force=False):
            calls.append(screen)
            if len(calls) == 2:          # first progress redraw fails
                raise OSError("SPI hiccup")
        dev = FakeDevice(self.folder)
        self.assertTrue(run_sync(dev, flaky_show))
        self.assertEqual(calls[-1], "safe")


class WorkTests(unittest.TestCase):
    """run_sync with a player-specific session (e.g. the iPod handler)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events, self.shown = [], []

    def show(self, screen, *args, force=False):
        self.events.append(screen)
        self.shown.append((screen, args))

    def test_work_runs_between_mount_and_unmount_and_feeds_safe_screen(self):
        dev = RecordingDevice(self.tmp.name, self.events)

        def work(device, step):
            self.events.append("work")
            step(5, "Copying 1/2: A - B")
            return {"added": 2, "removed": 1, "plays_sent": 3, "plays_queued": 4}
        self.assertTrue(run_sync(dev, self.show, work=work))
        order = [e for e in self.events if e in ("syncing", "mount", "work", "unmount", "safe")]
        self.assertEqual(order[:3], ["syncing", "mount", "work"])
        self.assertLess(self.events.index("unmount"), self.events.index("safe"))
        self.assertEqual(self.shown[-1], ("safe", ("--added", "2", "--removed", "1",
                                                   "--plays", "3", "--queued", "4")))
        self.assertIn(("syncing", ("--step", "1", "--total", "6", "--detail",
                                   "Mounting player")), self.shown)

    def test_copy_progress_feeds_the_bar(self):
        dev = RecordingDevice(self.tmp.name, self.events)

        def work(device, step):
            step(5, "Copying 3/79: A - B", part_done=2, part_total=79)
            return {}
        run_sync(dev, self.show, work=work)
        self.assertIn(("syncing", ("--step", "5", "--total", "6", "--detail",
                                   "Copying 3/79: A - B", "--part-done", "2",
                                   "--part-total", "79")), self.shown)

    def test_work_failure_still_unmounts_and_shows_failed(self):
        dev = RecordingDevice(self.tmp.name, self.events)

        def work(device, step):
            raise RuntimeError("Player is full")
        self.assertFalse(run_sync(dev, self.show, work=work))
        self.assertIn("unmount", self.events)
        self.assertEqual(self.shown[-1], ("failed", ("--reason", "Player is full")))

    def test_sysinfo_guid_for_fake_devices(self):
        os.makedirs(os.path.join(self.tmp.name, "iPod_Control", "Device"))
        with open(os.path.join(self.tmp.name, "iPod_Control", "Device", "SysInfo"), "w") as f:
            f.write("ModelNumStr: xB903\nFirewireGuid: 0x000a2700000000ab\n")
        self.assertEqual(dock_sync.sysinfo_guid(self.tmp.name), "000A2700000000AB")


class RestTimerTests(unittest.TestCase):
    def test_schedules_ten_minute_reset_as_root(self):
        with mock.patch.object(dock_sync.os, "geteuid", return_value=0), \
                mock.patch.object(dock_sync.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(dock_sync.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stderr="")) as run:
            dock_sync.schedule_rest()
        cmd = run.call_args_list[-1][0][0]
        self.assertIn("--on-active=10min", cmd)
        self.assertEqual(cmd[-1], "musicdock-rest@timer.service")

    def test_no_timer_when_not_root(self):
        with mock.patch.object(dock_sync.os, "geteuid", return_value=1000), \
                mock.patch.object(dock_sync.subprocess, "run") as run:
            dock_sync.schedule_rest()
        run.assert_not_called()


class ScreenProcessTests(unittest.TestCase):
    """Screen must draw in a separate process (GPIO is released on exit)."""

    def test_draws_in_a_child_process(self):
        with mock.patch.object(dock_sync.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            dock_sync.Screen().show("syncing", "--step", "1")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:], [dock_sync.DOCK_STATUS, "syncing", "--step", "1"])

    def test_failed_draw_raises_so_sync_wont_mount(self):
        with mock.patch.object(dock_sync.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            with self.assertRaises(SyncError):
                dock_sync.Screen().show("syncing", force=True)


class ListingTests(unittest.TestCase):
    def test_counts_files_and_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "iPod_Control", "Music", "F00"))
            for name, size in [("a.m4a", 100), ("b.m4a", 50)]:
                with open(os.path.join(d, "iPod_Control", "Music", "F00", name), "wb") as f:
                    f.write(b"x" * size)
            self.assertEqual(dock_sync.list_contents(d, limit=1), (2, 150))


class DetectionTests(unittest.TestCase):
    # Trimmed `lsblk --json --paths -o NAME,TRAN,FSTYPE,MOUNTPOINT` from a Pi
    # with its SD card plus a USB player. Partitions have no "tran" of their
    # own, so they must inherit it from the disk.
    LSBLK = {"blockdevices": [
        {"name": "/dev/sda", "tran": "usb", "fstype": None, "mountpoint": None,
         "children": [
             {"name": "/dev/sda1", "tran": None, "fstype": "vfat", "mountpoint": None},
         ]},
        {"name": "/dev/mmcblk0", "tran": None, "fstype": None, "mountpoint": None,
         "children": [
             {"name": "/dev/mmcblk0p1", "tran": None, "fstype": "vfat",
              "mountpoint": "/boot/firmware"},
             {"name": "/dev/mmcblk0p2", "tran": None, "fstype": "ext4",
              "mountpoint": "/"},
         ]},
    ]}

    def test_finds_only_usb_partition(self):
        found = dock_sync.find_usb_filesystems(self.LSBLK)
        self.assertEqual([d["path"] for d in found], ["/dev/sda1"])

    def test_detect_one_errors_when_nothing_plugged_in(self):
        with mock.patch.object(dock_sync, "find_usb_filesystems", return_value=[]):
            with self.assertRaises(SyncError):
                dock_sync.detect_one()


try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "Pillow not installed (pip install pillow)")
class StatusGuardTests(unittest.TestCase):
    """dock_status.py: the prepare job must never draw over SYNCING."""

    def setUp(self):
        import dock_status
        self.ds = dock_status
        self.tmp = tempfile.TemporaryDirectory()
        self.drawn = []
        patches = [
            mock.patch.object(dock_status, "STATE_FILE",
                              os.path.join(self.tmp.name, "state.json")),
            mock.patch.object(dock_status, "draw_on_panel",
                              lambda img, full_refresh: self.drawn.append(img)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        dock_status.main(["syncing", "--wifi", "off"])
        self.drawn.clear()

    def test_idle_cannot_cover_syncing(self):
        self.ds.main(["idle", "--wifi", "off", "--ip", "10.0.0.2"])
        self.ds.main(["preparing", "--wifi", "off"])
        self.assertEqual(self.drawn, [])

    def test_partial_stream_draws_each_line(self):
        import io
        lines = io.StringIO('["starting", "--uptime", "70", "--ip", "", "--wifi", "off"]\n'
                            '["starting", "--uptime", "80", "--ip", "10.0.0.2", "--wifi", "-50"]\n')
        with mock.patch.object(sys, "stdin", lines):
            self.ds.stream_main(["--preview-dir", self.tmp.name])
        frames = sorted(f for f in os.listdir(self.tmp.name) if f.endswith(".png"))
        self.assertEqual(frames, ["01-starting.png", "02-starting.png"])

    def test_force_and_sync_screens_can_replace_syncing(self):
        self.ds.main(["idle", "--force", "--wifi", "off", "--ip", "10.0.0.2"])
        self.assertEqual(len(self.drawn), 1)
        self.ds.main(["syncing", "--force", "--wifi", "off"])
        self.ds.main(["safe", "--wifi", "off"])
        self.assertEqual(len(self.drawn), 3)


if __name__ == "__main__":
    unittest.main()
