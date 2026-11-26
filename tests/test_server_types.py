"""Navidrome and other Subsonic servers (Aurral) are both supported (user, 2026-11-26):
server.type says which, the dock checks it against what the server says it is, and the
whole prepare job works against either kind of server.

Run: python3 -m unittest discover -s tests -v
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
sys.path.insert(0, os.path.dirname(__file__))
import isolate_state  # noqa: E402,F401  (before anything reads the dock's state)
import dock_config  # noqa: E402
import dock_guide  # noqa: E402
import dock_prepare  # noqa: E402
import dock_report  # noqa: E402
from test_dock_prepare import ROOMY, FakeServer, config, fake_convert, song  # noqa: E402

logging.disable(logging.CRITICAL)

SERVER = """[server]
url = "http://192.168.1.50:4533"
username = "jo"
password = "secret"
{type}
"""


class ConfigTests(unittest.TestCase):
    def load(self, type_line):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(SERVER.format(type=type_line))
        self.addCleanup(os.remove, f.name)
        os.chmod(f.name, 0o600)
        return dock_config.load(f.name)["server"]["type"]

    def test_a_config_without_the_line_is_navidrome(self):
        self.assertEqual(self.load(""), "navidrome")

    def test_both_kinds_load(self):
        self.assertEqual(self.load('type = "navidrome"'), "navidrome")
        self.assertEqual(self.load('type = "subsonic"'), "subsonic")

    def test_anything_else_is_refused(self):
        with self.assertRaises(dock_config.ConfigError) as e:
            self.load('type = "aurral"')
        self.assertIn('"subsonic"', str(e.exception))

    def test_older_placeholder_passwords_are_still_refused(self):
        for placeholder in ("your-password", "your-navidrome-password"):
            with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
                f.write(SERVER.format(type="").replace("secret", placeholder))
            self.addCleanup(os.remove, f.name)
            os.chmod(f.name, 0o600)
            with self.assertRaises(dock_config.ConfigError):
                dock_config.load(f.name)


class MismatchTests(unittest.TestCase):
    def test_what_each_server_says_it_is(self):
        mismatch = dock_config.type_mismatch
        self.assertIsNone(mismatch("navidrome", "navidrome"))
        self.assertIsNone(mismatch("subsonic", "Aurral"))
        self.assertIsNone(mismatch("subsonic", ""))              # a server that doesn't say
        self.assertIn('server.type = "subsonic"', mismatch("navidrome", "Aurral"))
        self.assertIn('server.type = "navidrome"', mismatch("subsonic", "navidrome"))
        self.assertIn("another Subsonic server", mismatch("navidrome", ""))


class EndToEndTests(unittest.TestCase):
    """The whole prepare job against a Navidrome-like and an Aurral-like server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "cache")
        os.makedirs(self.cache)

    def run_prepare(self, server, kind):
        cfg = config()
        cfg["sync"]["playlist_tag"] = "#ipod"
        cfg["server"] = {"type": kind}
        warnings = []
        with mock.patch.object(dock_prepare.log, "warning",
                               lambda msg, *a: warnings.append(msg % a if a else msg)):
            prepare_summary = dock_prepare.prepare(
                server, cfg, self.cache, convert_fn=fake_convert, disk_usage=ROOMY,
                player_kinds={"stock"}, lower_fn=None)
        with open(os.path.join(self.cache, "manifest.json")) as f:
            return json.load(f), prepare_summary, "\n".join(warnings)

    def test_navidrome_as_the_dock_was_built_for(self):
        server = FakeServer(
            songs=[song("n1", releaseTypes=["album"], samplingRate=44100, bitDepth=16)],
            playlists={"pl-1": ("Road Trip", [song("n2", samplingRate=48000)], "#ipod")})
        manifest, _, logs = self.run_prepare(server, "navidrome")
        self.assertEqual(sorted(t["id"] for t in manifest["tracks"]), ["n1", "n2"])
        self.assertEqual([p["name"] for p in manifest["playlists"] if p.get("via") == "tag"],
                         ["Road Trip"])
        self.assertEqual(manifest["server"], {"type": "navidrome", "version": "0.64.2"})
        self.assertNotIn("server.type", logs)

    def test_aurral_as_a_generic_subsonic_server(self):
        # Aurral: MusicBrainz-style IDs (some very long), tags in playlist names (its web
        # page can't edit comments), Flows, no release types or sample rates.
        long_id = "song:name:track:name%20album%20mbid%20" + "a%20b%20" * 40
        server = FakeServer(
            songs=[song("song:recording:0d853d72-20c1-4268-9126-d1d6b3cdee50")],
            playlists={"shared:1": ("Road Trip #iPod", [song(long_id)]),
                       "flow:2": ("Trending Mix #iPod", [song("song:recording:x-2")])})
        server.server_type, server.server_version = "Aurral", "3.1.0"
        manifest, _, logs = self.run_prepare(server, "subsonic")
        self.assertEqual(len(manifest["tracks"]), 3)
        self.assertEqual(sorted(p["name"] for p in manifest["playlists"]
                                if p.get("via") == "tag"), ["Road Trip", "Trending Mix"])
        self.assertEqual([p.get("flow", False) for p in manifest["playlists"]
                          if p["id"] == "flow:2"], [True])
        self.assertEqual(manifest["server"], {"type": "Aurral", "version": "3.1.0"})
        self.assertNotIn("server.type", logs)

    def test_the_wrong_type_is_said_but_the_music_still_comes(self):
        server = FakeServer(songs=[song("n1")])
        server.server_type = "Aurral"
        manifest, _, logs = self.run_prepare(server, "navidrome")
        self.assertIn('set server.type = "subsonic"', logs)
        self.assertEqual([t["id"] for t in manifest["tracks"]], ["n1"])
        facts = {"server_type": dock_config.type_mismatch("navidrome", "Aurral")}
        self.assertIn("server.type", dock_report.problems(facts)[0])


class GuideTests(unittest.TestCase):
    def guide(self, kind):
        cfg = {"sync": {"playlist_tag": "#ipod", "targets": ["starred"]},
               "server": {"type": kind}}
        return dock_guide.guide_text(cfg)

    def test_where_the_tag_goes_follows_the_server(self):
        self.assertIn('"Comment" field', self.guide("navidrome"))
        self.assertNotIn('"Comment" field', self.guide("subsonic"))
        self.assertIn("if your server lets", self.guide("subsonic"))


if __name__ == "__main__":
    unittest.main()
