"""Tests for the restore count (filling a new or wiped iPod in rounds).

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import dock_restore  # noqa: E402


def plan(n, skipped=0):
    return {"tracks": [{"id": f"s{i}"} for i in range(n - skipped)],
            "skipped": [{"id": f"s{i}"} for i in range(n - skipped, n)]}


class RestoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "restore.json")

    def status(self, have, manifest, now):
        return dock_restore.status("G", {f"s{i}" for i in range(have)}, manifest, now,
                                   self.path)

    def test_a_new_ipod_is_a_restore_counted_against_the_whole_plan(self):
        info = self.status(0, plan(40000, skipped=37000), now=0)
        self.assertEqual((info["have"], info["total"], info["left"]), (0, 40000, None))

    def test_a_few_new_songs_are_not(self):
        self.assertIsNone(self.status(1400, plan(1575), now=0))

    def test_time_left_from_the_pace_so_far(self):
        self.status(0, plan(4000), now=0)
        info = self.status(1000, plan(4000), now=3600)      # 1000 songs an hour
        self.assertEqual(info["have"], 1000)
        self.assertAlmostEqual(info["left"], 3 * 3600)
        self.assertEqual(dock_restore.left_text(info["left"]), "About 3 h left")

    def test_it_carries_on_below_the_threshold_until_done(self):
        self.status(0, plan(4000), now=0)
        self.assertIsNotNone(self.status(3900, plan(4000), now=100))   # 100 missing
        self.assertIsNone(self.status(4000, plan(4000), now=200))      # done: forgotten
        self.assertIsNone(self.status(3900, plan(4000), now=300))      # a new one needs 500

    def test_left_text(self):
        self.assertEqual(dock_restore.left_text(None), "")
        self.assertEqual(dock_restore.left_text(20 * 60), "About 20 min left")
        self.assertEqual(dock_restore.left_text(3 * 86400), "About 3 days left")

    def test_screen_args(self):
        args = dock_restore.screen_args({"have": 5, "total": 900, "left": 61.5}, "Preparing 3/10")
        self.assertEqual(args, ["restoring", "--done", "5", "--total", "900",
                                "--detail", "Preparing 3/10", "--left", "61"])


if __name__ == "__main__":
    unittest.main()
