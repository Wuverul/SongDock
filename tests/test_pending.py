"""Tests for the "next sync" counts on DOCK READY. Run: python3 -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import isolate_state  # noqa: E402,F401  (before anything reads the dock's state)
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


class ConversionTests(unittest.TestCase):
    PLAN = {"tracks": [{"id": "a", "encoding": "alac 16"},
                       {"id": "b", "encoding": "mp3 v0"},
                       {"id": "c", "encoding": "mp3 v0"},
                       {"id": "d", "encoding": "original", "source_suffix": "flac"}]}

    def test_counts_only_songs_whose_format_changed(self):
        records = [{"navidrome_id": "a", "encoding": "mp3 v0"},     # to lossless
                   {"navidrome_id": "b", "encoding": "alac 16"},    # back to MP3
                   {"navidrome_id": "c", "encoding": "mp3 v0"},     # as planned
                   {"navidrome_id": "d", "encoding": "mp3 v0"},     # to FLAC (Rockbox)
                   {"navidrome_id": "e", "encoding": "mp3 v0"},     # leaving: a removal
                   {"navidrome_id": "b", "encoding": None}]         # hand-copied, unknown
        self.assertEqual(ipod_handler.pending_conversions(self.PLAN, records), (2, 1))

    def test_none_when_up_to_date(self):
        records = [{"navidrome_id": "c", "encoding": "mp3 v0"}]
        self.assertEqual(ipod_handler.pending_conversions(self.PLAN, records), (0, 0))


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
        self.assertEqual(self.line("--pending-add", "0", "--pending-lossless", "109"),
                         "Lossless pending: 109 songs")
        self.assertEqual(self.line("--pending-add", "0", "--pending-format", "4"),
                         "Format changes: 4 songs")
        self.assertEqual(self.line("--pending-add", "0", "--swaps-no-room", "7"),
                         "No room to convert 7")
        self.assertEqual(self.line("--pending-add", "2", "--pending-lossless", "109"),
                         "Next sync: +2  −0 tracks")

    def test_renders(self):
        args = dock_status.build_parser().parse_args(
            ["idle", "--ready", "12", "--total", "19", "--pending-add", "19",
             "--pending-remove", "3", "--last-sync", "1792195000", "--wifi", "-50",
             "--ip", "10.0.0.2"])
        dock_status.render("idle", args)          # no exception = it laid out
        args.pending_lossless = 109               # with the extra row
        dock_status.render("idle", args)


class SafeHoursScreenTests(unittest.TestCase):
    def args(self, connected, screen, hour):
        from datetime import datetime
        import dock_prepare
        import dock_safehours
        window = dock_safehours.parse("07:00-10:00")
        now = datetime(2026, 10, 31, hour, 30)
        return dock_prepare.safe_hours_args(window, connected, screen, now)

    def test_docked_in_the_window_shows_it_with_the_resume_time(self):
        self.assertEqual(self.args(True, "idle", 8),
                         ["safehours", "--detail", "Syncs resume at 10:00 AM"])
        self.assertEqual(self.args(True, "preparing", 8)[0], "safehours")

    def test_nothing_docked_or_outside_the_window_leaves_dock_ready(self):
        self.assertIsNone(self.args(False, "idle", 8))
        self.assertIsNone(self.args(True, "idle", 12))

    def test_dock_ready_comes_back_when_it_ends_or_the_player_leaves(self):
        self.assertEqual(self.args(True, "safehours", 12), ["idle"])
        self.assertEqual(self.args(False, "safehours", 8), ["idle"])

    def test_never_covers_a_result_or_a_sync(self):
        for screen in ("syncing", "safe", "failed", "nospace", "safehours"):
            self.assertIsNone(self.args(True, screen, 8), screen)


class RoomDatesTests(unittest.TestCase):
    def dates(self, previous, playlists, now=5000):
        import dock_room
        songs = [{"id": i, "played": p} for i, p in (("a", 100), ("b", 0), ("c", 0))]
        return dock_room.new_dates(previous, songs, playlists, lambda t: t["played"], now)

    def test_first_run_dates_nothing_a_change_dates_the_whole_list(self):
        lists = [{"target": "playlist:L", "song_ids": ["a", "b"]}]
        first = self.dates(None, lists)
        self.assertEqual(first["songs"], {"a": 100})          # lists already there are old
        self.assertEqual(self.dates(first, lists, now=9000), first)     # same songs, same date
        grown = [{"target": "playlist:L", "song_ids": ["a", "b", "c"]}]
        self.assertEqual(self.dates(first, grown)["songs"], {"a": 5000, "b": 5000, "c": 5000})

    def test_a_new_list_counts_favorites_dont(self):
        first = self.dates(None, [])
        lists = [{"target": "starred", "song_ids": ["a", "b"]}]
        self.assertEqual(self.dates(first, lists)["songs"], {"a": 100})
        lists = [{"target": "playlist:New", "song_ids": ["b"]}]
        self.assertEqual(self.dates(first, lists)["songs"], {"a": 100, "b": 5000})

    def test_pick_removes_just_enough_and_nothing_for_what_cant_fit(self):
        import dock_room
        old = [(30, "x", 10), (10, "y", 10), (20, "z", 10)]
        self.assertEqual(dock_room.pick([5], 5, old), [])
        self.assertEqual(dock_room.pick([15], 0, old), ["y", "z"])
        self.assertEqual(dock_room.pick([100], 0, old), [])
        self.assertEqual(dock_room.pick([10, 100], 0, old), ["y"])


class RoomByAlbumTests(unittest.TestCase):
    """Make Room by album (user, 2026-11-01): whole albums go, dated by their newest song."""

    def plan(self):
        def t(nid, album, **kw):
            return {"id": nid, "album": album, "album_artist": "Artist", **kw}
        return {"make_room_albums": True,
                "tracks": [t("a1", "A"), t("a2", "A"), t("b1", "B"),
                           t("s1", "Artist Singles", folder_album="One"),
                           t("s2", "Artist Singles", folder_album="Two"), {"id": "x"}],
                "on_player": [t("a3", "a")]}           # case doesn't split an album

    def rules(self, dates, months=1, now=100 * 86400):
        import dock_room
        return dock_room.Rules({"songs": dates}, {}, months, now, dock_room.albums(self.plan()))

    def test_an_albums_date_is_its_newest_song(self):
        rules = self.rules({"a1": 10, "a3": 50 * 86400, "b1": 20})
        self.assertEqual(rules.date("a1"), 50 * 86400)          # a3 is on the same album
        self.assertEqual(rules.date("b1"), 20)
        rules.added = {"b1": 90 * 86400}                        # just reached the player
        self.assertTrue(rules.recent("b1"))
        self.assertFalse(rules.recent("a2"))

    def test_singles_and_songs_without_an_album_stay_apart(self):
        rules = self.rules({"s2": 99 * 86400})
        self.assertTrue(rules.recent("s2"))
        self.assertFalse(rules.recent("s1"))                    # its own release
        self.assertFalse(rules.recent("x"))

    def test_only_whole_albums_go_and_only_enough(self):
        import dock_room
        rules = self.rules({})
        on_player = {"a1", "a2", "a3", "b1", "s1"}
        candidates = [(30, "a1", 10), (5, "a2", 10), (30, "a3", 10), (20, "b1", 10),
                      (1, "s1", 10)]
        self.assertEqual(dock_room.choose(rules, [10], 0, candidates, on_player), ["s1"])
        self.assertEqual(sorted(dock_room.choose(rules, [15], 0, candidates, on_player)),
                         ["b1", "s1"])
        self.assertEqual(sorted(dock_room.choose(rules, [25], 0, candidates, on_player)),
                         ["a1", "a2", "a3", "b1", "s1"])                 # A goes whole
        self.assertEqual(dock_room.choose(rules, [60], 0, candidates, on_player), [])

    def test_an_album_with_a_song_that_cant_go_stays_whole(self):
        import dock_room
        rules = self.rules({})
        candidates = [(5, "a1", 10), (5, "a2", 10), (20, "b1", 10)]     # a3 isn't allowed
        self.assertEqual(dock_room.choose(rules, [10], 0, candidates, {"a1", "a2", "a3", "b1"}),
                         ["b1"])

    def test_off_it_goes_song_by_song(self):
        import dock_room
        rules = dock_room.Rules({"songs": {}}, {}, 1, 0)
        candidates = [(5, "a1", 10), (5, "a2", 10)]
        self.assertEqual(dock_room.choose(rules, [10], 0, candidates, {"a1", "a2"}), ["a1"])


class RoomRemovedTests(unittest.TestCase):
    """Songs taken off to make room: listed in MediaDock.txt, and brought back
    when played again in Navidrome's own apps (user, 2026-11-04)."""

    def setUp(self):
        import dock_record
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.rec = dock_record.Record(os.path.join(self.dir, "dock.db"))
        self.addCleanup(self.rec.close)
        self.rec.upsert_player("G", model="xB903")
        self.rec.mark_synced("G")
        self.now = 1_800_000_000
        for i, (nid, album) in enumerate((("a1", "A"), ("a2", "A"), ("b1", "B"))):
            self.rec.add_track("G", 100 + i, ipod_path=f":x{i}", navidrome_id=nid,
                               artist="Artist", album=album, title=f"Song {nid}")
            self.rec.mark_removed("G", 100 + i, "made room for new songs")
            self.rec.save_room_removed("G", nid)
        self.manifest = {"make_room_months": 3, "make_room_albums": True, "tracks": [
            {"id": n, "album": a, "album_artist": "Artist"}
            for n, a in (("a1", "A"), ("a2", "A"), ("b1", "B"))]}

    def dates(self, songs):
        import dock_room
        with open(os.path.join(self.dir, dock_room.DATES_FILE), "w") as f:
            json.dump({"playlists": {}, "songs": songs}, f)

    def test_listed_newest_first_with_names(self):
        import dock_setup
        songs = self.rec.room_removed_songs("G")
        self.assertEqual({s["navidrome_id"] for s in songs}, {"a1", "a2", "b1"})
        self.assertEqual(songs[0]["title"][:5], "Song ")
        text = dock_setup.marker_text([], "G", "Intro\n", removed=songs)
        self.assertIn("Taken off this iPod to make room (3; newest first)", text)
        self.assertIn("Artist - Song b1 (B)", text)
        self.assertNotIn("Taken off", dock_setup.marker_text([], "G", "Intro\n"))
        many = [dict(songs[0], navidrome_id=str(i)) for i in range(60)]
        self.assertIn("... and 10 more", dock_setup.marker_text([], "G", "I\n", removed=many))

    def test_a_song_played_again_brings_its_whole_album_back(self):
        import dock_room
        self.dates({"a1": self.now - 400 * 86400, "a2": self.now - 86400,   # played yesterday
                    "b1": self.now - 400 * 86400})
        self.assertEqual(dock_room.coming_back(self.manifest, self.dir, self.rec, "G", self.now),
                         {"a1", "a2"})
        plain = dict(self.manifest)
        del plain["make_room_albums"]                                        # song by song
        self.assertEqual(dock_room.coming_back(plain, self.dir, self.rec, "G", self.now), {"a2"})

    def test_nothing_comes_back_with_make_room_off_or_no_recent_play(self):
        import dock_room
        self.dates({"a1": self.now - 400 * 86400})
        self.assertEqual(dock_room.coming_back(self.manifest, self.dir, self.rec, "G", self.now),
                         set())
        off = {"tracks": self.manifest["tracks"]}
        self.dates({"a1": self.now})
        self.assertEqual(dock_room.coming_back(off, self.dir, self.rec, "G", self.now), set())

    def test_only_new_plays_ask_for_a_sync(self):
        import dock_prepare
        with open(os.path.join(self.dir, "manifest.json"), "w") as f:
            json.dump(self.manifest, f)
        now = time.time()
        self.dates({"a1": now - 400 * 86400, "a2": now - 400 * 86400, "b1": now - 400 * 86400})
        with mock.patch("dock_record.Record", return_value=self.rec), \
                mock.patch.object(self.rec, "close"):
            self.assertFalse(dock_prepare.played_again(self.dir))
            self.dates({"a1": now - 400 * 86400, "a2": now - 400 * 86400, "b1": now - 60})
            self.assertTrue(dock_prepare.played_again(self.dir))          # b1 played
            self.assertFalse(dock_prepare.played_again(self.dir))         # already asked
            args = dock_prepare.pending_args(self.manifest, self.dir)
        self.assertEqual(dict(zip(args[::2], args[1::2]))["--pending-add"], "1")


@unittest.skipIf(dock_status is None, "needs Pillow (dock_status)")
class FullScreenTests(unittest.TestCase):
    def test_renders(self):
        args = dock_status.build_parser().parse_args(
            ["nospace", "--not-added", "12", "--added", "3", "--wifi", "-50", "--ip", "10.0.0.2"])
        dock_status.render("nospace", args)

    def test_update_screens_render(self):
        parse = dock_status.build_parser().parse_args
        dock_status.render("updating", parse(["updating", "--detail", "To version 0.8.0",
                                              "--wifi", "-50", "--ip", "10.0.0.2"]))
        dock_status.render("idle", parse(
            ["idle", "--pending-add", "19", "--pending-lossless", "5", "--ready", "1", "--total",
             "2", "--update-note", "Update 0.8.0 at 3:15 PM", "--wifi", "-50", "--ip", "1.1.1.1"]))


if __name__ == "__main__":
    unittest.main()
