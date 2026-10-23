"""Tests for the manual written onto each synced iPod.

Run: python3 -m unittest discover -s tests -v
"""

import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_config  # noqa: E402
import dock_guide  # noqa: E402


def cfg(**sync):
    c = copy.deepcopy(dock_config.DEFAULTS)
    c["sync"].update(sync)
    return c


class GuideTests(unittest.TestCase):
    def test_describes_every_file_and_this_docks_settings(self):
        text = dock_guide.guide_text(cfg(targets=["starred", "playlist:Road Trip"],
                                         playlist_tag="#car", removal_grace_days=0))
        for needle in ("MediaDock.txt", "wifi.txt", "SSID=", "Password=", "Hidden=yes",
                       "DockSetup.txt", "Forget=all others", "Keep=",
                       "comment contains #car", "the playlist \"Road Trip\"",
                       "\"Favorites\"", "deleted right away", "SAFE TO REMOVE"):
            self.assertIn(needle, text)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 80)

    def test_settings_that_are_off_arent_described(self):
        text = dock_guide.guide_text(cfg(targets=[], playlist_tag="", favorites_playlist=""))
        self.assertIn("nothing is set up yet", text)
        self.assertNotIn("comment contains", text)
        self.assertIn("kept 7 days", text)

    def test_written_only_when_it_changes(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(dock_guide.write_guide(root, cfg()))
            self.assertFalse(dock_guide.write_guide(root, cfg()))
            self.assertTrue(dock_guide.write_guide(root, cfg(playlist_tag="#car")))
            self.assertEqual(os.listdir(root), [dock_guide.GUIDE_FILE])


if __name__ == "__main__":
    unittest.main()
