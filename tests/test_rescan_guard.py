"""A server half-way through rescanning its library must not empty the iPod
(2026-11-26: Navidrome, rescanning after its music folders changed, listed every
playlist with its song count but returned no songs, and no favorites).

Run: python3 -m unittest discover -s tests -v
"""

import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import isolate_state  # noqa: E402,F401  (before anything reads the dock's state)
import dock_prepare  # noqa: E402
import dock_report  # noqa: E402
from test_dock_prepare import ROOMY, FakeServer, config, fake_convert, song  # noqa: E402

logging.disable(logging.CRITICAL)
HOUR = 3600


def songs(n, prefix="s", title="Song"):
    return [song(f"{prefix}{i}", title=f"{title} {i}") for i in range(n)]


class RescanGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "cache")
        os.makedirs(self.cache)
        self.now = 1_800_000_000

    def run_prepare(self, server, cfg=None):
        if cfg is None:
            cfg = config()
            cfg["sync"]["playlist_tag"] = "#ipod"
        return dock_prepare.prepare(server, cfg, self.cache, convert_fn=fake_convert,
                                    disk_usage=ROOMY, player_kinds={"stock"}, lower_fn=None,
                                    clock=lambda: self.now)

    def planned(self):
        with open(os.path.join(self.cache, "manifest.json")) as f:
            m = json.load(f)
        return {t["id"] for k in ("tracks", "on_player", "skipped") for t in m.get(k, [])}

    def test_playlists_that_list_songs_but_return_none_hold_everything(self):
        server = FakeServer(songs=songs(3),
                            playlists={"p1": ("Road Trip #ipod", songs(5, "r"))})
        self.run_prepare(server)
        before = self.planned()
        server.playlists["p1"] = ("Road Trip #ipod", [])
        server.listed = {"p1": 5}                         # "5 songs", none returned
        with self.assertRaises(dock_prepare.HeldBack) as e:
            self.run_prepare(server)
        self.assertIn("say they have songs but return none (Road Trip)", str(e.exception))
        self.assertEqual(self.planned(), before)          # nothing changed
        self.now += 25 * HOUR                             # still so a day later: meant
        self.run_prepare(server)
        self.assertEqual(self.planned(), {f"s{i}" for i in range(3)})

    def test_most_of_the_plan_vanishing_at_once_holds(self):
        server = FakeServer(songs=songs(120))
        self.run_prepare(server)
        server.songs = server.songs[:20]                  # favorites half-read
        with self.assertRaises(dock_prepare.HeldBack) as e:
            self.run_prepare(server)
        self.assertIn("100 of the 120 songs planned last time are gone", str(e.exception))
        self.assertEqual(len(self.planned()), 120)
        self.now += 2 * HOUR                              # the rescan finished
        server.songs = songs(120)
        self.run_prepare(server)
        self.assertEqual(len(self.planned()), 120)
        self.assertFalse(os.path.exists(os.path.join(self.cache, dock_prepare.SHRINK_FILE)))

    def test_ordinary_changes_pass(self):
        server = FakeServer(songs=songs(120))
        self.run_prepare(server)
        server.songs = server.songs[:70]                  # 50 of 120 unstarred: under half
        self.run_prepare(server)
        self.assertEqual(len(self.planned()), 70)
        small = FakeServer(songs=songs(40))               # small plans aren't checked
        self.cache = os.path.join(self.cache, "small")
        os.makedirs(self.cache)
        self.run_prepare(small)
        small.songs = small.songs[:2]
        self.run_prepare(small)
        self.assertEqual(len(self.planned()), 2)

    def test_a_move_to_another_server_keeping_the_songs_passes(self):
        server = FakeServer(songs=songs(120))
        self.run_prepare(server)
        server.songs = songs(120, prefix="song:recording:")   # every ID new, same songs
        self.run_prepare(server)
        self.assertEqual(len(self.planned()), 120)

    def test_the_hold_starts_once_and_says_when_it_ends(self):
        server = FakeServer(songs=songs(120))
        self.run_prepare(server)
        server.songs = []
        for later in (0, 5 * HOUR, 10 * HOUR):
            self.now += later
            with self.assertRaises(dock_prepare.HeldBack):
                self.run_prepare(server)
        with open(os.path.join(self.cache, dock_prepare.SHRINK_FILE)) as f:
            held = json.load(f)
        self.assertEqual(held["until"] - held["since"], 24 * HOUR)
        self.assertEqual(held["since"], 1_800_000_000)     # from the first, not reset
        problems = dock_report.problems({"held_back": held}, now=self.now)
        self.assertIn("Music changes held back", problems[0])

    def test_the_saved_album_copies_survive_a_hold(self):
        server = FakeServer(albums={f"a{i}": songs(30, f"a{i}-") for i in range(4)},
                            album_meta={f"a{i}": {"songCount": 30} for i in range(4)})
        self.run_prepare(server)
        path = os.path.join(self.cache, dock_prepare.ALBUM_CACHE)
        with open(path) as f:
            kept = json.load(f)
        server.albums = {}                                # no favorite albums mid-rescan
        with self.assertRaises(dock_prepare.HeldBack):
            self.run_prepare(server)
        with open(path) as f:
            self.assertEqual(json.load(f), kept)

    def test_the_prepare_job_treats_a_hold_as_a_pause_not_a_failure(self):
        self.assertTrue(issubclass(dock_prepare.HeldBack, dock_prepare.PrepareError))
        with open(dock_prepare.__file__) as f:
            code = f.read()
        self.assertLess(code.index("except HeldBack"), code.index("except PrepareError as e:"))


if __name__ == "__main__":
    unittest.main()
