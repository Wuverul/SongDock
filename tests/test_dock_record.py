"""Tests for the Pi's SQLite record. Run: python3 -m unittest discover -s tests -v"""

import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dock_record import Record  # noqa: E402

GUID = "000A2700000000AB"


def play(dbid, seq=0, at=1792100000, nid="song1", **kw):
    return {"dbid": dbid, "seq": seq, "played_at": at, "navidrome_id": nid, **kw}


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "dock.db")
        self.rec = Record(self.path)
        self.addCleanup(lambda: self.rec.close())     # self.rec may be replaced

    def test_player_model_kept_once_known(self):
        self.rec.upsert_player(GUID, model="xB903")
        self.rec.upsert_player(GUID)                       # model unknown this time
        self.assertEqual(self.rec.player(GUID)["model"], "xB903")
        self.assertIsNone(self.rec.player("OTHER"))

    def test_tracks_present_removed_and_readded(self):
        big = 16209428067326106446                          # > signed 64-bit range
        self.rec.add_track(GUID, big, navidrome_id="n1", title="Make Love",
                           ipod_path=":iPod_Control:Music:F12:a.mp3")
        self.assertEqual(self.rec.track(GUID, big)["dbid"], str(big))
        self.rec.mark_removed(GUID, big, "no longer in any synced playlist")
        self.assertEqual(self.rec.tracks(GUID), [])
        self.assertEqual(self.rec.tracks(GUID, "removed")[0]["note"],
                         "no longer in any synced playlist")
        self.rec.add_track(GUID, big, navidrome_id="n1", title="Make Love")
        self.assertEqual([t["state"] for t in self.rec.tracks(GUID)], ["present"])

    def test_requeuing_same_plays_adds_nothing(self):
        plays = [play(111, seq=0), play(111, seq=1, approx=True)]
        self.assertEqual(self.rec.queue_plays(GUID, plays), 2)
        self.assertEqual(self.rec.queue_plays(GUID, plays), 0)   # e.g. after a crash
        self.assertEqual(len(self.rec.unsent_plays()), 2)

    def test_sent_plays_leave_queue_but_stay_as_history(self):
        self.rec.queue_plays(GUID, [play(1), play(2)])
        first = self.rec.unsent_plays()[0]["id"]
        self.rec.mark_sent([first])
        self.assertEqual(len(self.rec.unsent_plays()), 1)
        n = self.rec.db.execute("SELECT COUNT(*) FROM pending_plays").fetchone()[0]
        self.assertEqual(n, 2)
        self.assertEqual(self.rec.queue_plays(GUID, [play(1)]), 0)  # not re-queued

    def test_failed_upload_keeps_plays(self):
        self.rec.queue_plays(GUID, [play(1)])
        pid = self.rec.unsent_plays()[0]["id"]
        self.rec.mark_failed([pid], "server unreachable")
        row = self.rec.unsent_plays()[0]
        self.assertEqual((row["attempts"], row["last_error"]), (1, "server unreachable"))

    def test_unmatched_plays_kept_but_not_sendable(self):
        self.rec.queue_plays(GUID, [play(5, nid=None, title="Unknown song"), play(6)])
        self.assertEqual([p["dbid"] for p in self.rec.unsent_plays()], ["6"])
        self.assertEqual(self.rec.unsent_count(), (1, 1))

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.rec.transaction() as db:
                db.execute("INSERT INTO players (guid, first_seen) VALUES ('X', 0)")
                raise RuntimeError("power cut")
        self.assertIsNone(self.rec.player("X"))

    def test_plays_matched_later_get_their_song(self):
        self.rec.queue_plays(GUID, [play(77, nid=None, title="Hand-copied")])
        self.rec.add_track(GUID, 77, navidrome_id="nav77", title="Hand-copied")  # adopted
        self.assertEqual(self.rec.rematch_plays(GUID), 1)
        self.assertEqual(self.rec.unsent_plays()[0]["navidrome_id"], "nav77")

    def test_managed_playlists(self):
        self.rec.set_managed_playlists(GUID, {"starred": "Favorites",
                                              "playlist:Mel Test": "Mel Test"})
        self.assertEqual(self.rec.managed_playlists(GUID)["starred"], "Favorites")
        self.rec.set_managed_playlists(GUID, {"starred": "Favorites"})
        self.assertEqual(list(self.rec.managed_playlists(GUID)), ["starred"])

    def test_backup_is_a_complete_copy(self):
        self.rec.add_track(GUID, 42, navidrome_id="n42", title="BLOOM")
        dest = os.path.join(self.tmp.name, "copy.db")
        self.rec.backup_to(dest)
        with contextlib.closing(sqlite3.connect(dest)) as c:
            self.assertEqual(c.execute("SELECT title FROM tracks").fetchone()[0], "BLOOM")

    def test_reopen_keeps_data(self):
        self.rec.queue_plays(GUID, [play(1)])
        self.rec.close()
        self.rec = Record(self.path)
        self.assertEqual(len(self.rec.unsent_plays()), 1)


if __name__ == "__main__":
    unittest.main()
