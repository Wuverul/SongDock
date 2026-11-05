"""Songs Navidrome gives new IDs (a re-index after a server update, 2026-11-04:
3,572 songs at once) are followed, not fetched and copied again."""

import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import isolate_state  # noqa: E402,F401  (before anything reads the dock's state)
import dock_prepare  # noqa: E402
from test_dock_prepare import ROOMY, FakeServer, config, fake_convert, song  # noqa: E402


class MatchTests(unittest.TestCase):
    def entry(self, nid, title, **kw):
        return {"id": nid, "artist": "Artist", "title": title, "album": "Album",
                "track": 1, "disc": 1, "duration": 200, **kw}

    def test_by_recording_then_by_names(self):
        earlier = [self.entry("o1", "One", mbid="AAA"), self.entry("o2", "Two")]
        targets = [song("n1", title="One renamed", musicBrainzId="aaa", duration=200),
                   song("n2", title="Two", track=1, discNumber=1)]
        self.assertEqual(dock_prepare.match_new_ids(earlier, targets), {"o1": "n1", "o2": "n2"})

    def test_identical_copies_on_two_editions_are_paired_up(self):
        earlier = [self.entry("o1", "Eva"), self.entry("o2", "Eva")]
        targets = [song("n1", title="Eva", track=1, discNumber=1),
                   song("n2", title="Eva", track=1, discNumber=1)]
        self.assertEqual(dock_prepare.match_new_ids(earlier, targets), {"o1": "n1", "o2": "n2"})
        one_more = targets + [song("n3", title="Eva", track=1, discNumber=1)]
        self.assertEqual(dock_prepare.match_new_ids(earlier, one_more), {})   # 2 vs 3: no

    def test_a_different_length_isnt_the_same_song(self):
        earlier = [self.entry("o1", "One")]
        targets = [song("n1", title="One", track=1, discNumber=1, duration=231)]
        self.assertEqual(dock_prepare.match_new_ids(earlier, targets), {})

    def test_the_same_song_on_another_edition_of_its_album(self):
        # Two-edition albums: the kept copy moved to the deluxe one (other album name,
        # track number and recording ID), as with San Holo's after the re-index.
        earlier = [self.entry("o1", "One", mbid="AAA", album="Album", track=11)]
        targets = [song("n1", title="One", album="Album (Deluxe)", track=13, discNumber=1,
                        musicBrainzId="BBB")]
        self.assertEqual(dock_prepare.match_new_ids(earlier, targets), {"o1": "n1"})
        twice = targets + [song("n2", title="One", album="Album (Live)", track=2, discNumber=1)]
        self.assertEqual(dock_prepare.match_new_ids(earlier, twice), {})   # which one? neither

    def test_songs_still_under_their_id_are_left_alone(self):
        earlier = [self.entry("s1", "Kept")]
        self.assertEqual(dock_prepare.match_new_ids(earlier, [song("s1", title="Kept")]), {})


class FollowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = tmp.name
        self.renamed = {}

    def run_prepare(self, server):
        return dock_prepare.prepare(server, config(), self.cache, convert_fn=fake_convert,
                                    disk_usage=ROOMY, player_kinds={"stock"}, lower_fn=None,
                                    rename_fn=lambda m: self.renamed.update(m) or len(m))

    def manifest(self):
        with open(os.path.join(self.cache, "manifest.json")) as f:
            return json.load(f)

    def test_a_reindexed_library_keeps_its_files_and_nothing_is_fetched(self):
        old = [song("o1", title="One", track=1, discNumber=1, musicBrainzId="m1"),
               song("o2", title="Two", track=2, discNumber=1)]
        server = FakeServer(songs=old)
        self.run_prepare(server)
        made = {t["id"]: t for t in self.manifest()["tracks"]}
        with open(os.path.join(self.cache, "tracks", "o1.mp3"), "ab") as f:
            f.write(b"|lowered")                      # stands for a lowered, measured file
        m = self.manifest()
        m["tracks"][0]["encoding"], m["tracks"][0]["peak"] = "mp3 v0 -1.5dB", 0.8
        with open(os.path.join(self.cache, "manifest.json"), "w") as f:
            json.dump(m, f)

        server = FakeServer(songs=[song("n1", title="One", track=1, discNumber=1,
                                        musicBrainzId="m1"),
                                   song("n2", title="Two", track=2, discNumber=1)])
        summary = self.run_prepare(server)
        self.assertEqual(server.downloads, [])                       # nothing fetched
        self.assertEqual((summary["added"], summary["removed"]), (0, 0))
        self.assertEqual(self.renamed, {"o1": "n1", "o2": "n2"})     # the record follows
        tracks = {t["id"]: t for t in self.manifest()["tracks"]}
        self.assertEqual(set(tracks), {"n1", "n2"})
        self.assertEqual(tracks["n1"]["encoding"], "mp3 v0 -1.5dB")  # lowered stays lowered
        self.assertEqual(tracks["n1"]["mbid"], "m1")
        self.assertEqual(sorted(os.listdir(os.path.join(self.cache, "tracks"))),
                         ["n1.mp3", "n2.mp3"])
        with open(os.path.join(self.cache, "tracks", "n1.mp3"), "rb") as f:
            self.assertTrue(f.read().endswith(b"|lowered"))
        self.assertEqual(made["o1"]["file"], "tracks/o1.mp3")

    def test_files_a_run_set_aside_come_back(self):
        # 2026-11-04: the run that saw the new IDs moved the old files to the grace
        # list and downloaded new copies before it was stopped.
        server = FakeServer(songs=[song("o1", title="One", track=1, discNumber=1)])
        self.run_prepare(server)
        entry = self.manifest()["tracks"][0]
        with open(os.path.join(self.cache, "retired.json"), "w") as f:
            json.dump({"o1.mp3": {**entry, "since": 1}}, f)
        plan = {**self.manifest(), "tracks": []}
        with open(os.path.join(self.cache, "manifest.json"), "w") as f:
            json.dump(plan, f)
        with open(os.path.join(self.cache, "tracks", "n1.mp3"), "wb") as f:
            f.write(b"a fresh download")
        server = FakeServer(songs=[song("n1", title="One", track=1, discNumber=1)])
        self.run_prepare(server)
        self.assertEqual(server.downloads, [])
        with open(os.path.join(self.cache, "tracks", "n1.mp3"), "rb") as f:
            self.assertNotEqual(f.read(), b"a fresh download")      # the old file won
        with open(os.path.join(self.cache, "retired.json")) as f:
            self.assertEqual(json.load(f), {})


class RecordTests(unittest.TestCase):
    def test_every_table_follows(self):
        import dock_record
        with tempfile.TemporaryDirectory() as d:
            rec = dock_record.Record(os.path.join(d, "dock.db"))
            rec.upsert_player("G", model="xB903")
            rec.add_track("G", 1, ipod_path=":a", navidrome_id="o1", title="One")
            rec.queue_plays("G", [{"navidrome_id": "o1", "played_at": 5, "dbid": "1"}])
            rec.save_room_removed("G", "o2")
            rec.log_copied("G", ":b", "o2", {"id": "o2", "title": "Two"})
            self.assertGreaterEqual(rec.rename_songs({"o1": "n1", "o2": "n2"}), 4)
            self.assertEqual([r["navidrome_id"] for r in rec.tracks("G")], ["n1"])
            self.assertEqual(set(rec.room_removed("G")), {"n2"})
            self.assertEqual([(c["navidrome_id"], c["entry"]["id"]) for c in rec.copied("G")],
                             [("n2", "n2")])
            self.assertEqual(rec.db.execute("SELECT navidrome_id FROM pending_plays").fetchall()[0][0],
                             "n1")
            rec.close()


class IpodCommentTests(unittest.TestCase):
    def test_the_id_kept_on_the_track_follows(self):
        import ipod_handler as h
        track = types.SimpleNamespace(album="A", title="T", artist="X", genre=None,
                                      albumartist="X", comment=h.NAVIDROME_TAG + "o1")
        t = {"id": "n1", "album": "A", "title": "T", "artist": "X"}
        self.assertEqual(h.fill_missing_names(track, t), {"id"})
        self.assertEqual(track.comment, h.NAVIDROME_TAG + "n1")
        track.comment = "a comment of the user's"
        self.assertEqual(h.fill_missing_names(track, t), set())      # left alone


if __name__ == "__main__":
    unittest.main()
