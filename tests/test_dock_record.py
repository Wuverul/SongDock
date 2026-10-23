"""Tests for the Pi's SQLite record. Run: python3 -m unittest discover -s tests -v"""

import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dock_record import Record  # noqa: E402

GUID = "000A270000000002"


def play(dbid, seq=0, at=1792100000, nid="song1", **kw):
    return {"dbid": dbid, "seq": seq, "played_at": at, "navidrome_id": nid, **kw}


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "dock.db")
        self.rec = Record(self.path)
        self.addCleanup(lambda: self.rec.close())     # self.rec may be replaced

    def test_songs_on_every_player(self):
        self.assertEqual(self.rec.songs_on_every_player(), {})
        self.rec.add_track(GUID, 1, navidrome_id="n1", encoding="mp3 v0")
        self.rec.add_track(GUID, 2, navidrome_id="n2", encoding="mp3 v0")
        self.rec.add_track(GUID, 3, navidrome_id="n3")
        self.rec.mark_removed(GUID, 3, "no longer in any synced playlist")
        self.assertEqual(self.rec.songs_on_every_player(),
                         {"n1": {"mp3 v0"}, "n2": {"mp3 v0"}})
        other = "000A270000000003"
        self.rec.upsert_player(other)                       # e.g. just reset: empty
        self.assertEqual(self.rec.songs_on_every_player(), {})
        self.rec.add_track(other, 9, navidrome_id="n2", encoding="original")
        self.assertEqual(self.rec.songs_on_every_player(), {"n2": {"mp3 v0", "original"}})

    def test_forgotten_player_holds_nothing_back_until_it_docks(self):
        nano, classic = GUID, "000A270000000003"
        for guid in (nano, classic):
            self.rec.upsert_player(guid)
            self.rec.mark_synced(guid)
        self.rec.add_track(nano, 1, navidrome_id="n1", encoding="mp3 v0")
        for i, nid in enumerate(["n1", "n2", "n3"]):
            self.rec.add_track(classic, 10 + i, navidrome_id=nid, encoding="mp3 v0")
        self.assertEqual(set(self.rec.songs_on_every_player()), {"n1"})
        self.rec.forget_player(nano)
        self.assertEqual(set(self.rec.songs_on_every_player()), {"n1", "n2", "n3"})
        self.assertEqual(self.rec.last_synced_player()["guid"], classic)
        nano_row = {p["guid"]: p for p in self.rec.players()}[nano]
        self.assertIsNotNone(nano_row["forgotten_at"])
        self.assertEqual(nano_row["present"], 1)                  # history kept
        self.rec.mark_synced(nano)                                 # docked again
        self.assertEqual(set(self.rec.songs_on_every_player()), {"n1"})
        self.assertIsNone(self.rec.player(nano)["forgotten_at"])
        self.rec.forget_player(nano)
        self.rec.forget_player(classic)
        self.assertEqual(self.rec.songs_on_every_player(), {})
        self.assertIsNone(self.rec.last_synced_player())

    def test_active_stock_players(self):
        nano, rockbox, unseen = GUID, "000A270000000003", "000A270000000004"
        for guid in (nano, rockbox, unseen):
            self.rec.upsert_player(guid)
        self.rec.set_player_kind(nano, "stock")
        self.rec.set_player_kind(rockbox, "rockbox")
        self.assertEqual({p["guid"] for p in self.rec.active_stock_players()},
                         {nano, unseen})                  # unseen = assumed stock
        self.rec.forget_player(nano)
        self.assertEqual([p["guid"] for p in self.rec.active_stock_players()], [unseen])

    def test_older_record_gains_the_forgotten_column(self):
        self.rec.close()
        old = os.path.join(self.tmp.name, "old.db")
        db = sqlite3.connect(old)
        db.execute("CREATE TABLE players (guid TEXT PRIMARY KEY, model TEXT, "
                   "first_seen INTEGER NOT NULL, last_sync INTEGER)")
        db.execute("INSERT INTO players VALUES (?, 'xB903', 1, 2)", (GUID,))
        db.commit()
        db.close()
        self.rec = Record(old)
        self.assertIsNone(self.rec.player(GUID)["forgotten_at"])
        self.assertEqual(self.rec.last_synced_player()["guid"], GUID)
        Record(old).close()                                        # second open: no-op

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

    def test_last_synced_player(self):
        self.assertIsNone(self.rec.last_synced_player())
        self.rec.upsert_player(GUID, model="xB903")
        self.rec.upsert_player("OTHER")
        self.rec.mark_synced(GUID)
        self.assertEqual(self.rec.last_synced_player()["guid"], GUID)

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
