"""Tests for noticing a failing SD card (dock_sdcard) and its warning on DOCK READY.

Run: python3 -m unittest discover -s tests -v
"""

import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import isolate_state  # noqa: E402,F401  (before anything reads the dock's state)
import dock_sdcard  # noqa: E402
import dock_usbport  # noqa: E402

logging.disable(logging.CRITICAL)

# Kernel messages of SD cards failing (as Raspberry Pi OS words them).
CARD_ERRORS = [
    "songdock kernel: I/O error, dev mmcblk0, sector 4202496 op 0x1:(WRITE) flags 0x800",
    "songdock kernel: mmcblk0: error -110 transferring data, sector 4202496, nr 8",
    "songdock kernel: mmcblk0: recovery failed!",
    "songdock kernel: Buffer I/O error on dev mmcblk0p2, logical block 525312",
    "songdock kernel: EXT4-fs error (device mmcblk0p2): ext4_find_entry:1658: inode #2",
    "songdock kernel: EXT4-fs (mmcblk0p2): Remounting filesystem read-only",
]
# Messages that aren't the card: the Wi-Fi chip (mmc1 on a Zero 2 W), the card being
# found at boot, an iPod's own disk.
HARMLESS = [
    "songdock kernel: mmc1: Controller never released inhibit bit(s).",
    "songdock kernel: mmcblk0: mmc0:aaaa SD64G 58.2 GiB",
    "songdock kernel: EXT4-fs (mmcblk0p2): mounted filesystem with ordered data mode",
    "songdock kernel: I/O error, dev sda, sector 1234 op 0x0:(READ)",
    "songdock kernel: FAT-fs (sda1): Volume was not properly unmounted.",
]


class SdCardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ,
                              {"DOCK_STATE_FILE": os.path.join(tmp.name, "dock_status.json")})
        env.start()
        self.addCleanup(env.stop)
        self.card, self.errors, self.read_only = "card-1", 0, False

    def check(self, texts=(), now=1000):
        return dock_sdcard.check([(now, t) for t in texts], now,
                                 read_id=lambda: self.card,
                                 read_root=lambda: ("mmcblk0p2", self.read_only),
                                 read_errors=lambda device: self.errors)

    def warned(self):
        with mock.patch.object(dock_sdcard, "card_id", lambda: self.card):
            return dock_sdcard.warning()

    def test_a_healthy_card_shows_nothing(self):
        self.assertIsNone(self.check(HARMLESS))
        self.assertIsNone(self.warned())

    def test_each_kind_of_card_error_is_noticed(self):
        for text in CARD_ERRORS:
            self.assertTrue(dock_sdcard.CARD_ERROR.search(text), text)
        for text in HARMLESS:
            self.assertFalse(dock_sdcard.CARD_ERROR.search(text), text)

    def test_reported_once_and_kept(self):
        self.assertIn("I/O error", self.check(CARD_ERRORS[:1]))
        self.assertIsNone(self.check(CARD_ERRORS[1:2], now=2000))   # already warned
        w = self.warned()
        self.assertEqual((w["since"], w["count"]), (1000, 2))
        self.assertIn("error -110", w["detail"])

    def test_filesystem_errors_count_even_after_a_restart(self):
        self.errors = 2                       # ext4 kept them in the card's superblock
        self.assertIn("2 error(s)", self.check())
        self.assertIsNone(self.check(now=2000))
        self.assertEqual(self.warned()["count"], 1)    # not counted again every run

    def test_a_card_gone_read_only_is_noticed_once(self):
        self.read_only = True
        self.assertIn("read-only", self.check())
        self.assertIsNone(self.check(now=2000))
        self.assertEqual(self.warned()["count"], 1)

    def test_nothing_is_written_while_all_is_well(self):
        self.check(HARMLESS)
        kept, ram = dock_sdcard._folders()
        self.assertFalse(os.path.exists(kept) or os.path.exists(ram))

    def test_a_new_card_starts_clean(self):
        self.check(CARD_ERRORS[:1])
        self.card = "card-2"
        self.assertIsNone(self.warned())
        self.assertIsNone(self.check(HARMLESS))

    def test_cleared_by_hand_until_something_new(self):
        self.errors = 3
        self.check()
        dock_sdcard.mark_ok(read_id=lambda: self.card, read_root=lambda: ("mmcblk0p2", False),
                            read_errors=lambda device: self.errors)
        self.assertIsNone(self.warned())
        self.assertIsNone(self.check(now=2000))         # the 3 it was told about
        self.errors = 4
        self.assertIsNotNone(self.check(now=3000))

    def test_the_ram_copy_keeps_it_when_the_card_cant_be_written(self):
        kept, ram = dock_sdcard._folders()
        real = dock_sdcard._write
        with mock.patch.object(dock_sdcard, "_write",
                               lambda path, data: path != kept and real(path, data)):
            self.check(CARD_ERRORS[-1:])
        self.assertFalse(os.path.exists(kept))
        self.assertIsNotNone(self.warned())

    def test_the_usb_watch_runs_it_and_redraws_on_a_new_problem(self):
        redraws, seen = [], []

        def sd_check(lines, now):
            seen.append(lines)
            return "I/O error" if len(seen) == 1 else None
        for now in (100, 130):
            dock_usbport.check(os.path.join(os.path.dirname(dock_sdcard._folders()[0]),
                                            "usbport.json"),
                               now=now, read_log=lambda cursor: ([(now, CARD_ERRORS[0])], "c"),
                               show=lambda: None, busy=lambda: False,
                               sd_check=sd_check, redraw=lambda: redraws.append(now))
        self.assertEqual(redraws, [100])
        self.assertEqual(seen[0], [(100, CARD_ERRORS[0])])

    def test_musicdock_status_lists_it_first(self):
        import dock_report
        self.check(CARD_ERRORS[:1])
        out = dock_report.problems({"sd_card": self.warned(), "temp": 75})
        self.assertIn("SD card may be failing", out[0])
        self.assertIn("--ok", out[0])


class ScreenTests(unittest.TestCase):
    def render(self, warning):
        import dock_status
        args = dock_status.build_parser().parse_args(
            ["idle", "--sd-warning", warning, "--ip", "10.0.0.2", "--wifi", "-56",
             "--pending-add", "0", "--pending-remove", "0"])
        with mock.patch.object(dock_status, "dark_mode", lambda: False):
            return dock_status.render("idle", args)

    def test_dock_ready_shows_the_warning_in_place_of_the_ip(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("needs Pillow")
        on, off = self.render("on"), self.render("off")
        box = (0, 100, 150, 122)                       # bottom left: IP or warning
        black = lambda img: sum(1 for p in img.crop(box).getdata() if p == 0)   # noqa: E731
        self.assertGreater(black(on), 3 * black(off))  # white on black, not a line of text


if __name__ == "__main__":
    unittest.main()
