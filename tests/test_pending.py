"""Tests for the "next sync" counts on DOCK READY. Run: python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
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
