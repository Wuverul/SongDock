"""Tests for the "next sync" counts on DOCK READY. Run: python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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


if __name__ == "__main__":
    unittest.main()
