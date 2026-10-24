"""Tests for the Rockbox handler, on fake Rockbox players (folders).
Needs mutagen + Pillow (the Pi venv). Run: python3 -m unittest discover -s tests -v
"""

import io
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import ipod_fixtures as fx  # noqa: E402
import rockbox_handler as rb  # noqa: E402
from dock_record import Record  # noqa: E402

logging.disable(logging.CRITICAL)
G = "000A270000000009"


def have_tools():
    try:
        import mutagen  # noqa: F401
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_tools(), "needs mutagen + Pillow (the Pi venv)")
class RockboxTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.mount = os.path.join(self.tmp, "player")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(os.path.join(self.mount, ".rockbox"))
        os.makedirs(os.path.join(self.mount, "iPod_Control", "Device"))
        with open(os.path.join(self.mount, "iPod_Control", "Device", "SysInfo"), "w") as f:
            f.write("ModelNumStr: xB029\n")
        os.makedirs(os.path.join(self.cache, "tracks"))
        self.rec = Record(os.path.join(self.tmp, "dock.db"))
        self.addCleanup(self.rec.close)

    # ------------------------------------------------------------ helpers ----
    def song(self, nid, title, album="Album", track=1, artist="Artist", **kw):
        path = os.path.join(self.cache, "tracks", f"{nid}.mp3")
        if not os.path.exists(path):
            fx.make_mp3(path, f"file tag {title}", artist=artist, album="file album",
                        track=track, seconds=2, cover=True)
        return {"id": nid, "file": f"tracks/{nid}.mp3", "title": title, "artist": artist,
                "album_artist": artist, "album": album, "track": track, "disc": 1,
                "duration": 2, "encoding": "mp3 v0", **kw}

    def manifest(self, songs, playlists=()):
        return {"tracks": songs, "playlists": [
            {"target": t, "name": n, "song_ids": ids} for t, n, ids in playlists]}

    def sync(self, manifest, write=True):
        return rb.sync_player(self.mount, G, self.rec, manifest, self.cache,
                              work_dir=os.path.join(self.tmp, "work"), write=write)

    def files(self):
        return sorted(os.path.relpath(os.path.join(r, n), self.mount)
                      for r, _, ns in os.walk(os.path.join(self.mount, rb.MUSIC_DIR))
                      for n in ns if not r.endswith(".musicdock"))

    def tags(self, rel):
        f = rb._easy(os.path.join(self.mount, rel))
        return {k: (f.get(k) or [None])[0] for k in ("title", "album", "genre",
                                                     rb.ID_TAG.lower())}

    def write_log(self, lines, tz="UTC"):
        with open(os.path.join(self.mount, rb.SCROBBLER_LOG), "w") as f:
            f.write(f"#AUDIOSCROBBLER/1.1\n#TZ/{tz}\n#CLIENT/Rockbox ipod6g $Revision$\n")
            f.write("".join("\t".join(map(str, line)) + "\n" for line in lines))

    # -------------------------------------------------------------- tests ----
    def test_adds_files_cover_and_playlists_then_is_idempotent(self):
        a = self.song("n1", "One/Two", album="Hold: Your Colour", track=1, genre="Electronic")
        b = self.song("n2", "Three", album="Hold: Your Colour", track=2)
        m = self.manifest([a, b], [("starred", "Favorites", ["n2", "n1"])])
        summary = self.sync(m)
        self.assertEqual((summary["added"], summary["removed"]), (2, 0))
        album = os.path.join("MediaDock", "Artist", "Hold_ Your Colour")
        self.assertEqual(self.files(), [os.path.join(album, "01 One_Two.mp3"),
                                        os.path.join(album, "02 Three.mp3"),
                                        os.path.join(album, "cover.jpg")])
        self.assertEqual(self.tags(os.path.join(album, "01 One_Two.mp3")),
                         {"title": "One/Two", "album": "Hold: Your Colour",
                          "genre": "Electronic", rb.ID_TAG.lower(): "n1"})
        from PIL import Image
        with Image.open(os.path.join(self.mount, album, "cover.jpg")) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertFalse(img.info.get("progressive"))
        with open(os.path.join(self.mount, "Playlists", "Favorites.m3u8")) as f:
            self.assertEqual(f.read().splitlines(), [
                "#EXTM3U", "/MediaDock/Artist/Hold_ Your Colour/02 Three.mp3",
                "/MediaDock/Artist/Hold_ Your Colour/01 One_Two.mp3"])
        self.assertEqual(self.rec.player(G)["model"], "xB029")
        self.assertEqual(self.rec.player(G)["kind"], "rockbox")
        again = self.sync(m)
        self.assertEqual((again["added"], again["removed"], again["playlists_changed"],
                          again["renamed"]), (0, 0, 0, 0))

    def test_removal_tidies_up_and_leaves_other_files_alone(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two", album="Other")
        self.sync(self.manifest([a, b], [("playlist:Mine", "Mine", ["n1", "n2"])]))
        own = os.path.join(self.mount, "Music", "mine.mp3")      # the user's own file
        os.makedirs(os.path.dirname(own))
        fx.make_mp3(own, "Mine", seconds=1)
        with open(os.path.join(self.mount, "Playlists", "Road Trip.m3u8"), "w") as f:
            f.write("#EXTM3U\n/Music/mine.mp3\n")
        summary = self.sync(self.manifest([a], [("playlist:Mine", "Mine", ["n1"])]))
        self.assertEqual(summary["removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.mount, "MediaDock", "Artist",
                                                     "Other")))     # folder + cover gone
        self.assertTrue(os.path.exists(own))
        self.assertTrue(os.path.exists(os.path.join(self.mount, "Playlists",
                                                    "Road Trip.m3u8")))
        self.sync(self.manifest([a]))                                # playlist unsynced
        self.assertEqual(os.listdir(os.path.join(self.mount, "Playlists")),
                         ["Road Trip.m3u8"])

    def test_plays_from_the_log_queued_then_log_deleted(self):
        a = self.song("n1", "One")
        self.sync(self.manifest([a]))
        self.write_log([("Artist", "Album", "One", 1, 2, "L", 1792100000, ""),
                        ("Artist", "Album", "One", 1, 2, "S", 1792100100, ""),
                        ("Someone", "Else", "Unknown", 1, 200, "L", 1792100200, "")])
        summary = self.sync(self.manifest([a]), write=False)        # read-only first
        self.assertEqual(summary["plays_new"], 2)                    # the skip isn't a play
        self.assertTrue(os.path.exists(os.path.join(self.mount, rb.SCROBBLER_LOG)))
        summary = self.sync(self.manifest([a]))                      # same log again
        self.assertEqual(summary["plays_new"], 0)                    # no duplicates
        self.assertFalse(os.path.exists(os.path.join(self.mount, rb.SCROBBLER_LOG)))
        plays = self.rec.unsent_plays()
        self.assertEqual([(p["navidrome_id"], p["played_at"]) for p in plays],
                         [("n1", 1792100000)])
        self.assertEqual(self.rec.unsent_count(), (1, 1))            # 1 sendable, 1 unknown

    def test_unknown_time_zone_means_the_players_local_clock(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        import time
        time.tzset()
        try:
            text = "#TZ/UNKNOWN\nA\tB\tC\t1\t200\tL\t1792800000\t\n"
            self.assertEqual(rb.parse_scrobbler_log(text)[0]["time"], 1792800000 + 7 * 3600)
        finally:
            if old is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_lost_record_is_rebuilt_from_the_files(self):
        songs = [self.song("n1", "One"), self.song("n2", "Two", track=2)]
        self.sync(self.manifest(songs))
        self.rec.close()
        self.rec = Record(os.path.join(self.tmp, "fresh.db"))        # SD card lost
        summary = self.sync(self.manifest(songs))
        self.assertEqual((summary["added"], summary["reconcile"]["adopted"]), (0, 2))

    def test_lost_record_readopts_the_docks_playlists(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two", track=2)
        self.sync(self.manifest([a], [("playlist:Mix", "Mix", ["n1"])]))
        self.rec.close()
        self.rec = Record(os.path.join(self.tmp, "new-dock.db"))
        self.sync(self.manifest([a, b], [("playlist:Mix", "Mix", ["n1", "n2"])]))
        with open(os.path.join(self.mount, "Playlists", "Mix.m3u8")) as f:
            self.assertEqual(len(f.read().splitlines()), 3)          # header + 2 songs

    def test_generic_player_gets_music_but_no_plays_or_playlists(self):
        a = self.song("n1", "One")
        self.write_log([("Artist", "Album", "One", 1, 2, "L", 1792100000, "")])
        summary = rb.sync_player(self.mount, "SDABCDEF01234567", self.rec,
                                 self.manifest([a], [("playlist:Mix", "Mix", ["n1"])]),
                                 self.cache, work_dir=os.path.join(self.tmp, "work"),
                                 write=True, kind="generic")
        self.assertEqual((summary["added"], summary["plays_new"]), (1, 0))
        self.assertIs(summary["plays_supported"], False)
        self.assertFalse(os.path.exists(os.path.join(self.mount, "Playlists")))   # none made
        self.assertTrue(os.path.exists(os.path.join(self.mount, rb.SCROBBLER_LOG)))  # untouched
        player = self.rec.player("SDABCDEF01234567")
        self.assertEqual((player["kind"], player["name"]), ("generic", "Music player"))

    def test_player_id_is_made_once_and_kept(self):
        self.assertIsNone(rb.player_id(self.mount))                       # read-only: none
        made = rb.player_id(self.mount, create=True)
        self.assertEqual(rb.player_id(self.mount, create=True), made)
        self.assertRegex(made, r"^SD[0-9A-F]{14}$")

    def test_names_and_genre_follow_the_plan_without_recopying(self):
        a = self.song("n1", "One", album="Inertia")
        self.sync(self.manifest([a]))
        rel = os.path.join("MediaDock", "Artist", "Inertia", "01 One.mp3")
        before = os.stat(os.path.join(self.mount, rel)).st_ino
        summary = self.sync(self.manifest([dict(a, album="Inertia (deluxe digital)",
                                                genre="Electronic")]))
        self.assertEqual((summary["added"], summary["renamed"]), (0, 1))
        self.assertEqual(self.tags(rel)["album"], "Inertia (deluxe digital)")
        self.assertEqual(os.stat(os.path.join(self.mount, rel)).st_ino, before)

    def test_leftovers_and_foreign_files_in_the_dock_folder(self):
        a = self.song("n1", "One")
        folder = os.path.join(self.mount, "MediaDock", "Artist", "Album")
        os.makedirs(folder)
        with open(os.path.join(folder, rb.TMP_PREFIX + "01 One.mp3"), "wb") as f:
            f.write(b"half a copy")
        fx.make_mp3(os.path.join(folder, "dropped in.mp3"), "Mine", seconds=1)
        summary = self.sync(self.manifest([a]))
        self.assertEqual((summary["reconcile"]["orphans"], summary["reconcile"]["foreign"]),
                         (1, 1))
        self.assertIn(os.path.join("MediaDock", "Artist", "Album", "dropped in.mp3"),
                      self.files())

    def test_no_plan_reads_plays_only(self):
        self.write_log([("Artist", "Album", "One", 1, 2, "L", 1792100000, "")])
        summary = self.sync({})
        self.assertFalse(summary["wrote"])
        self.assertFalse(os.path.exists(os.path.join(self.mount, "MediaDock")))
        self.assertEqual(summary["plays_new"], 1)

    def test_same_name_from_two_songs_gets_a_number(self):
        a = self.song("n1", "Intro", album="Live")
        b = self.song("n2", "Intro", album="Live", artist="Artist")
        self.sync(self.manifest([a, b]))
        names = [os.path.basename(p) for p in self.files()]
        self.assertIn("01 Intro.mp3", names)
        self.assertIn("01 Intro (2).mp3", names)


class SafeNameTests(unittest.TestCase):
    def test_fat_unsafe_characters_and_dots(self):
        self.assertEqual(rb.safe_name('AC/DC: "Back" <in> Black?*'), "AC_DC_ _Back_ _in_ Black__")
        self.assertEqual(rb.safe_name("...hidden. "), "hidden")
        self.assertEqual(rb.safe_name(""), "_")


if __name__ == "__main__":
    unittest.main()
