"""Tests for reading iPod play data (needs pygpod + mutagen; the Pi venv).

Run: ~/musicdock/venv/bin/python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import ipod_fixtures as fx  # noqa: E402
import ipod_handler  # noqa: E402
from dock_record import Record  # noqa: E402

# 2026-10-16 00:14:46 local, as an iPod (Mac-epoch) time.
MAC_TIME = int((datetime(2026, 10, 16, 0, 14, 46) - ipod_handler.MAC_EPOCH).total_seconds())


@unittest.skipUnless(fx.have_ipod_tools(), "needs pygpod + mutagen (the Pi venv)")
class PlayReadingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "ipod")
        src = os.path.join(tmp.name, "src")
        os.makedirs(src)
        self.tracks = fx.make_ipod(self.root, [
            {"title": "BLOOM", "artist": "GOJII", "album": "CURRENTS", "track": 1, "seconds": 6},
            {"title": "Make Love", "artist": "Daft Punk", "album": "HAA", "track": 5, "seconds": 4},
            {"title": "Mystery", "artist": "Someone", "album": "Else", "track": 1, "seconds": 3},
        ], src)
        self.work = os.path.join(tmp.name, "snapshot")
        self.rec = Record(os.path.join(tmp.name, "dock.db"))
        self.addCleanup(self.rec.close)
        # The dock added the first two; "Mystery" was added some other way.
        for t, nid in zip(self.tracks[:2], ("nav-bloom", "nav-makelove")):
            self.rec.add_track(fx.FAKE_GUID, t.dbid, navidrome_id=nid, title=t.title)

    def read(self):
        ipod_handler.snapshot_play_data(self.root, self.work)
        return ipod_handler.plays_from_snapshot(self.work, self.rec, fx.FAKE_GUID)

    def test_maps_plays_by_position_to_navidrome_ids(self):
        fx.write_play_counts(self.root, [(1, MAC_TIME), (0, 0), (0, 0)])
        plays = self.read()
        self.assertEqual(len(plays), 1)
        p = plays[0]
        self.assertEqual((p["dbid"], p["navidrome_id"], p["approx"]),
                         (self.tracks[0].dbid, "nav-bloom", False))
        self.assertEqual(datetime.fromtimestamp(p["played_at"]),
                         datetime(2026, 10, 16, 0, 14, 46))

    def test_repeat_plays_get_estimated_earlier_times(self):
        fx.write_play_counts(self.root, [(0, 0), (3, MAC_TIME), (0, 0)])
        plays = self.read()
        self.assertEqual([p["seq"] for p in plays], [0, 1, 2])
        self.assertEqual([p["approx"] for p in plays], [False, True, True])
        times = [p["played_at"] for p in plays]
        self.assertTrue(times[0] > times[1] > times[2])        # stepping back
        self.assertEqual({p["navidrome_id"] for p in plays}, {"nav-makelove"})

    def test_unknown_track_plays_kept_for_matching(self):
        fx.write_play_counts(self.root, [(0, 0), (0, 0), (2, MAC_TIME)])
        plays = self.read()
        self.assertEqual({p["navidrome_id"] for p in plays}, {None})
        self.assertEqual(plays[0]["title"], "Mystery")

    def test_reading_never_touches_the_player(self):
        fx.write_play_counts(self.root, [(1, MAC_TIME), (0, 0), (0, 0)])
        path = os.path.join(self.root, "iPod_Control", "iTunes", "Play Counts")
        with open(path, "rb") as f:
            before = f.read()
        self.read()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_queueing_twice_after_a_crash_adds_nothing(self):
        fx.write_play_counts(self.root, [(2, MAC_TIME), (1, MAC_TIME), (0, 0)])
        self.assertEqual(self.rec.queue_plays(fx.FAKE_GUID, self.read()), 3)
        self.assertEqual(self.rec.queue_plays(fx.FAKE_GUID, self.read()), 0)

    def test_no_play_file_means_no_plays(self):
        self.assertEqual(self.read(), [])


if __name__ == "__main__":
    unittest.main()
