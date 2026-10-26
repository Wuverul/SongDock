"""Tests for the notes about a player's size (dock_limits).

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_limits as lim  # noqa: E402

MP3 = {"device": {"type": "stock_ipod", "convert_to": "mp3"}}


class LimitTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertIsNone(lim.library_note(19_999))
        self.assertIn("syncs may be slower", lim.library_note(20_000))
        self.assertIn("slowdowns or crashes", lim.library_note(30_000))
        self.assertIn("close to the limit", lim.library_note(45_000))
        self.assertIn("limit reached", lim.library_note(47_000))
        self.assertIn("slowdowns or crashes", lim.library_note(47_000, stock=False))

    def test_the_larger_of_on_the_player_and_planned_counts(self):
        self.assertEqual(lim.player_notes(1_455, 21_000, None, MP3),
                         ["Large library (21,000 songs): syncs may be slower"])
        self.assertEqual(lim.player_notes(21_000, 900, None, MP3),
                         ["Large library (21,000 songs): syncs may be slower"])

    def test_alac_suggested_only_for_big_stock_ipods_on_mp3_when_it_fits(self):
        big, small, music = 512e9, 16e9, 12e9            # 12 GB of MP3 -> ~40 GB of ALAC
        notes = lambda *a, **kw: lim.player_notes(1_455, 1_455, *a, **kw)   # noqa: E731
        self.assertIn("ALAC", notes(big, MP3, music_bytes=music)[0])
        self.assertEqual(notes(small, MP3, music_bytes=music), [])
        alac = {"device": {"type": "stock_ipod", "convert_to": "alac"}}
        self.assertEqual(notes(big, alac, music_bytes=music), [])
        self.assertEqual(notes(big, MP3, kind="rockbox", music_bytes=music), [])
        self.assertEqual(notes(None, MP3, music_bytes=music), [])          # not known yet
        self.assertEqual(notes(big, MP3), [])                               # size unknown
        # 300 GB of MP3 would be ~1 TB of ALAC: no suggestion.
        self.assertEqual(notes(big, MP3, music_bytes=300e9), [])

    def test_plan_size_counts_uncached_songs_at_the_average(self):
        plan = {"tracks": [{"id": "a", "size": 8e6}, {"id": "b", "size": 12e6}],
                "on_player": [{"id": "c"}], "skipped": [{"id": "d"}]}
        self.assertEqual(lim.plan_bytes(plan), 40e6)
        self.assertIsNone(lim.plan_bytes({"tracks": []}))

    def test_written_into_mediadock_txt(self):
        import dock_setup
        players = [{"guid": "G1", "name": "Mel", "model": "xC293", "present": 21_000,
                    "last_sync": 1, "forgotten_at": None}]
        text = dock_setup.marker_text(players, "G1", "intro", notes=["Large library"])
        self.assertIn("About this iPod:\n  * Large library", text)
        self.assertNotIn("About this iPod", dock_setup.marker_text(players, "G1", "intro"))

    def test_shown_in_the_status_report(self):
        import dock_report
        self.assertIn("Large library", dock_report.problems({"size_notes": ["Large library"]}))

    def test_sync_records_the_capacity_and_logs_the_notes(self):
        import logging
        import dock_sync
        previous = logging.root.manager.disable          # other tests turn logging off
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, previous)
        from dock_record import Record
        with tempfile.TemporaryDirectory() as tmp:
            rec = Record(os.path.join(tmp, "dock.db"))
            rec.upsert_player("G1")
            # 20,500 songs, 41 GB as MP3: ~135 GB as ALAC, fits 512 GB.
            manifest = {"tracks": [{"id": f"s{i}", "size": 2_000_000} for i in range(20_500)]}
            usage = mock.Mock(total=int(512e9))
            with mock.patch("dock_sync.shutil.disk_usage", return_value=usage), \
                    self.assertLogs("dock_sync", "WARNING") as logs:
                notes = dock_sync.size_notes(rec, "G1", tmp, manifest, MP3, "stock")
            self.assertEqual(len(notes), 2)                     # large library + ALAC
            self.assertEqual(rec.player("G1")["capacity"], int(512e9))
            self.assertEqual(len(logs.output), 2)
            rec.close()


if __name__ == "__main__":
    unittest.main()
