"""Tests for the prepare job and config loading. No network or ffmpeg needed.

Run: python3 -m unittest discover -s tests -v
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from collections import namedtuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_config  # noqa: E402
import dock_prepare  # noqa: E402
from dock_prepare import needs_conversion, prepare  # noqa: E402
from subsonic import SubsonicError  # noqa: E402

logging.disable(logging.CRITICAL)

Usage = namedtuple("Usage", "total used free")
ROOMY = lambda path: Usage(100e9, 10e9, 90e9)   # noqa: E731


def song(id, suffix="flac", size=1000, **kw):
    return {"id": id, "suffix": suffix, "size": size, "title": f"Song {id}",
            "artist": "Artist", "album": "Album", "duration": 200, **kw}


class FakeServer:
    """Stands in for subsonic.Client."""

    def __init__(self, songs=(), albums=None, fail_download=()):
        self.songs = list(songs)
        self.albums = albums or {}            # album id -> [songs]
        self.fail_download = set(fail_download)
        self.downloads = []

    def call(self, endpoint, **params):
        if endpoint == "getStarred2":
            return {"starred2": {"song": self.songs,
                                 "album": [{"id": a} for a in self.albums]}}
        if endpoint == "getAlbum":
            return {"album": {"song": self.albums[params["id"]]}}
        raise AssertionError(endpoint)

    def download(self, song_id, dest):
        self.downloads.append(song_id)
        if song_id in self.fail_download:
            raise SubsonicError("download: HTTP 500")
        with open(dest, "wb") as f:
            f.write(b"audio-" + song_id.encode())


def fake_convert(src, dst, bitrate, duration=None):
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(b"aac:" + f.read())


def config(device="stock_ipod", max_gb=20):
    return {"sync": {"target": "starred", "max_cache_gb": max_gb},
            "device": {"type": device, "aac_bitrate": "256k"}}


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def run_prepare(self, server, cfg=None, **kw):
        kw.setdefault("disk_usage", ROOMY)
        return prepare(server, cfg or config(), self.cache,
                       convert_fn=fake_convert, **kw)

    def files(self):
        return sorted(os.listdir(os.path.join(self.cache, "tracks")))

    def manifest(self):
        with open(os.path.join(self.cache, "manifest.json")) as f:
            return json.load(f)

    def test_starred_album_expands_and_duplicates_collapse(self):
        server = FakeServer(songs=[song("a1")],
                            albums={"alb": [song("a1"), song("a2")]})
        summary = self.run_prepare(server)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(self.files(), ["a1.m4a", "a2.m4a"])

    def test_formats_for_stock_ipod(self):
        server = FakeServer(songs=[song("f", "flac"), song("m", "mp3"),
                                   song("a", "m4a"), song("o", "opus")])
        self.run_prepare(server)
        self.assertEqual(self.files(), ["a.m4a", "f.m4a", "m.mp3", "o.m4a"])
        with open(os.path.join(self.cache, "tracks", "m.mp3"), "rb") as f:
            self.assertEqual(f.read(), b"audio-m")          # copied, not re-encoded
        with open(os.path.join(self.cache, "tracks", "f.m4a"), "rb") as f:
            self.assertTrue(f.read().startswith(b"aac:"))   # converted

    def test_rockbox_keeps_flac(self):
        self.assertFalse(needs_conversion("flac", "rockbox"))
        self.run_prepare(FakeServer(songs=[song("f", "flac")]), config("rockbox"))
        self.assertEqual(self.files(), ["f.flac"])

    def test_second_run_downloads_nothing_new(self):
        server = FakeServer(songs=[song("a"), song("b")])
        self.run_prepare(server)
        server.downloads.clear()
        summary = self.run_prepare(server)
        self.assertEqual(server.downloads, [])
        self.assertEqual(summary["added"], 0)

    def test_unstarred_tracks_are_pruned(self):
        server = FakeServer(songs=[song("a"), song("b")])
        self.run_prepare(server)
        server.songs = [song("a")]
        summary = self.run_prepare(server)
        self.assertEqual(self.files(), ["a.m4a"])
        self.assertEqual(summary["removed"], 1)
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["a"])

    def test_one_failed_track_doesnt_block_others(self):
        server = FakeServer(songs=[song("a"), song("bad"), song("c")],
                            fail_download={"bad"})
        summary = self.run_prepare(server)
        self.assertEqual(self.files(), ["a.m4a", "c.m4a"])
        self.assertEqual([s["id"] for s in summary["skipped"]], ["bad"])
        self.assertTrue(summary["skipped"][0]["error"])
        self.assertNotIn("bad", [t["id"] for t in self.manifest()["tracks"]])

    def test_leftover_partial_files_are_never_queued(self):
        # Simulate a run cut off mid-conversion.
        os.makedirs(os.path.join(self.cache, "tmp"))
        with open(os.path.join(self.cache, "tmp", "a.m4a"), "wb") as f:
            f.write(b"half")
        self.run_prepare(FakeServer(songs=[song("a")]))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "tmp")))
        with open(os.path.join(self.cache, "tracks", "a.m4a"), "rb") as f:
            self.assertEqual(f.read(), b"aac:audio-a")

    def test_stops_when_sd_card_would_pass_80_percent(self):
        nearly_full = lambda path: Usage(100e9, 79.9999e9, 20e9)   # noqa: E731
        server = FakeServer(songs=[song("a", size=10_000_000)])
        summary = self.run_prepare(server, disk_usage=nearly_full)
        self.assertEqual(server.downloads, [])
        self.assertFalse(summary["skipped"][0]["error"])   # a limit, not an error

    def test_stops_at_cache_limit(self):
        server = FakeServer(songs=[song("a", size=600_000_000)])
        summary = self.run_prepare(server, cfg=config(max_gb=1))
        self.assertEqual(summary["ready"], 0)
        self.assertIn("max_cache_gb", summary["skipped"][0]["reason"])

    def test_dry_run_touches_nothing(self):
        server = FakeServer(songs=[song("a")])
        self.run_prepare(server, dry_run=True)
        self.assertEqual(server.downloads, [])
        self.assertEqual(os.listdir(self.cache), [])

    def test_progress_screen_names_the_track(self):
        shown = []
        self.run_prepare(FakeServer(songs=[song("a")]),
                         show=lambda *a: shown.append(a))
        self.assertEqual(shown[0][:5], ("preparing", "--done", "0", "--total", "1"))
        self.assertIn("Converting: Artist - Song a", shown[0])


class ConfigTests(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text)
        f.close()
        os.chmod(f.name, 0o600)
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_defaults_fill_in(self):
        cfg = dock_config.load(self.write(
            '[server]\nurl="https://x"\nusername="u"\npassword="p"\n'))
        self.assertEqual(cfg["device"]["aac_bitrate"], "256k")
        self.assertFalse(cfg["sync"]["cache_dir"].startswith("~"))

    def test_placeholder_password_rejected(self):
        path = self.write('[server]\nurl="https://x"\nusername="u"\n'
                          'password="your-navidrome-password"\n')
        with self.assertRaises(dock_config.ConfigError):
            dock_config.load(path)

    def test_missing_file_explains_where_it_goes(self):
        with self.assertRaisesRegex(dock_config.ConfigError, "chmod 600"):
            dock_config.load("/nonexistent/config.toml")

    def test_example_config_loads_apart_from_password(self):
        example = os.path.join(os.path.dirname(__file__), "..", "config.example.toml")
        with self.assertRaisesRegex(dock_config.ConfigError, "placeholder"):
            dock_config.load(example)


class ClientSecrecyTests(unittest.TestCase):
    def test_repr_hides_password(self):
        from subsonic import Client
        self.assertNotIn("hunter2", repr(Client("https://x", "u", "hunter2")))


if __name__ == "__main__":
    unittest.main()
