"""Tests for the "next sync" counts on DOCK READY. Run: python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ipod_handler  # noqa: E402


def manifest(ready, skipped=()):
    return {"tracks": [{"id": i} for i in ready],
            "skipped": [{"id": i, "reason": "x", "error": False} for i in skipped]}


class PendingTests(unittest.TestCase):
    def test_adds_and_removes(self):
        m = manifest(["a", "b", "c"])
        self.assertEqual(ipod_handler.pending_changes(m, {"a", "x", "y"}), (2, 2))

    def test_up_to_date(self):
        self.assertEqual(ipod_handler.pending_changes(manifest(["a"]), {"a"}), (0, 0))

    def test_uncached_targeted_track_is_not_a_removal(self):
        m = manifest(["a"], skipped=["b"])
        self.assertEqual(ipod_handler.pending_changes(m, {"a", "b"}), (0, 0))


try:
    import dock_status
except ImportError:
    dock_status = None


@unittest.skipIf(dock_status is None, "Pillow not installed")
class PendingScreenTests(unittest.TestCase):
    def line(self, *argv):
        return dock_status.pending_line(dock_status.build_parser().parse_args(["idle", *argv]))

    def test_text(self):
        self.assertEqual(self.line("--pending-add", "19", "--pending-remove", "3"),
                         "Next sync: +19  −3 tracks")
        self.assertEqual(self.line("--pending-add", "0", "--pending-remove", "0"),
                         "iPod is up to date")

    def test_renders(self):
        args = dock_status.build_parser().parse_args(
            ["idle", "--ready", "12", "--total", "19", "--pending-add", "19",
             "--pending-remove", "3", "--last-sync", "1792195000", "--wifi", "-50",
             "--ip", "10.0.0.2"])
        dock_status.render("idle", args)          # no exception = it laid out


if __name__ == "__main__":
    unittest.main()
