"""Tests for a whole iPod dock session (needs pygpod + mutagen; the Pi venv).

Run: ~/musicdock/venv/bin/python -m unittest discover -s tests -v
"""

import hashlib
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import ipod_fixtures as fx  # noqa: E402
import ipod_handler as h  # noqa: E402
from dock_record import Record  # noqa: E402
from test_dock_plays import FakeScrobbler  # noqa: E402

logging.disable(logging.CRITICAL)
G = fx.FAKE_GUID
MAC_TIME = int((datetime(2026, 10, 16, 0, 14, 46) - h.MAC_EPOCH).total_seconds())


def tree_digest(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha1(f.read()).hexdigest()
    return out


@unittest.skipUnless(fx.have_ipod_tools(), "needs pygpod + mutagen (the Pi venv)")
class SessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.mount = os.path.join(self.tmp, "ipod")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(os.path.join(self.cache, "tracks"))
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        # A hand-copied BLOOM is already on the player, with 1 logged play.
        fx.make_ipod(self.mount, [{"title": "BLOOM", "artist": "GOJII",
                                   "album": "CURRENTS", "seconds": 2}], src)
        fx.write_play_counts(self.mount, [(1, MAC_TIME)])
        self.rec = Record(os.path.join(self.tmp, "dock.db"))
        self.addCleanup(self.rec.close)
        self.manifest = {"tracks": [self.song("nav-bloom", "BLOOM", "CURRENTS"),
                                    self.song("nav-ml", "Make Love", "Human After All")],
                         "playlists": [{"target": "starred", "name": "Favorites",
                                        "song_ids": ["nav-bloom", "nav-ml"]}]}

    def song(self, nid, title, album):
        path = os.path.join(self.cache, "tracks", f"{nid}.mp3")
        fx.make_mp3(path, title, artist="GOJII" if title == "BLOOM" else "Daft Punk",
                    album=album, seconds=2)
        return {"id": nid, "file": f"tracks/{nid}.mp3", "title": title,
                "artist": "GOJII" if title == "BLOOM" else "Daft Punk", "album": album,
                "track": 1, "duration": 2, "encoding": "mp3 v0"}

    def session(self, write, server=None, **kw):
        return h.sync_player(self.mount, G, self.rec, self.manifest, self.cache,
                             work_dir=os.path.join(self.tmp, "work"),
                             backup_root=os.path.join(self.tmp, "backups"),
                             write=write, client=server, **kw)

    def test_read_only_changes_nothing_on_player_but_sends_plays(self):
        before = tree_digest(self.mount)
        server = FakeScrobbler()
        summary = self.session(write=False, server=server)
        self.assertEqual(tree_digest(self.mount), before)        # player untouched
        self.assertEqual((summary["plays_new"], summary["plays_sent"]), (1, 1))
        self.assertEqual(server.recorded[0][0], "nav-bloom")      # matched via adoption

    def test_full_sync(self):
        server = FakeScrobbler()
        summary = self.session(write=True, server=server)
        self.assertEqual((summary["added"], summary["plays_sent"]), (1, 1))
        import pygpod
        db = pygpod.Database(self.mount)
        self.assertEqual(sorted(t.title for t in db.tracks), ["BLOOM", "Make Love"])
        self.assertFalse(os.path.exists(os.path.join(h.itunes_dir(self.mount), "Play Counts")))
        self.assertTrue(os.path.exists(os.path.join(self.mount, h.RECORD_COPY)))
        again = self.session(write=True, server=server)
        self.assertEqual((again["added"], again["plays_new"]), (0, 0))

    def test_no_staged_plan_sends_plays_but_removes_nothing(self):
        # Fresh dock: the first prepare run hasn't written a plan yet
        # (2026-10-23: an empty plan removed 1238 tracks from the Nano).
        self.session(write=True, server=FakeScrobbler())      # BLOOM adopted, ML added
        fx.write_play_counts(self.mount, [(1, MAC_TIME + 60), (0, 0)])
        before = tree_digest(self.mount)
        self.manifest = {}
        server = FakeScrobbler()
        summary = self.session(write=True, server=server)
        self.assertEqual((summary["removed"], summary["wrote"]), (0, False))
        self.assertEqual(summary["plays_sent"], 1)
        self.assertEqual(tree_digest(self.mount), before)    # player untouched
        with self.assertRaises(ValueError):
            h.apply_plan(None, self.rec, G, self.mount, {}, self.cache)

    def test_server_down_keeps_plays_and_still_syncs_music(self):
        server = FakeScrobbler(down_after=0)
        summary = self.session(write=True, server=server)
        self.assertEqual((summary["added"], summary["plays_queued"]), (1, 1))

    def test_failure_restores_backup_and_keeps_plays(self):
        import pygpod
        before = [t.title for t in pygpod.Database.from_file(
            os.path.join(h.itunes_dir(self.mount), "iTunesDB")).tracks]
        with mock.patch.object(h, "apply_plan", side_effect=OSError("USB unplugged")):
            with self.assertRaises(OSError):
                self.session(write=True)
        after = pygpod.Database.from_file(os.path.join(h.itunes_dir(self.mount), "iTunesDB"))
        self.assertEqual([t.title for t in after.tracks], before)
        self.assertEqual(self.rec.unsent_count(), (1, 0))         # play kept, matched
        self.assertTrue(os.path.exists(os.path.join(h.itunes_dir(self.mount), "Play Counts")))

    def test_failure_after_an_hourly_save_goes_back_to_that_save(self):
        import pygpod
        with mock.patch.object(h, "SAVE_EVERY", 0), \
                mock.patch.object(h, "sync_playlists", side_effect=OSError("USB unplugged")):
            with self.assertRaises(OSError):
                self.session(write=True)
        db = pygpod.Database.from_file(os.path.join(h.itunes_dir(self.mount), "iTunesDB"))
        self.assertEqual(sorted(t.title for t in db.tracks), ["BLOOM", "Make Love"])
        self.assertEqual(len(self.rec.tracks(G)), 2)
        # The save merged the play counts and deleted the file; the restore
        # mustn't bring it back (its positions would credit the wrong songs).
        self.assertFalse(os.path.exists(os.path.join(h.itunes_dir(self.mount), "Play Counts")))
        self.assertEqual(db.tracks[0].play_count, 1)
        self.assertEqual(self.rec.unsent_count(), (1, 0))


if __name__ == "__main__":
    unittest.main()
