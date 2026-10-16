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

    def __init__(self, songs=(), albums=None, fail_download=(), playlists=None):
        self.songs = list(songs)
        self.albums = albums or {}            # album id -> [songs]
        self.playlists = playlists or {}      # playlist id -> (name, [songs])
        self.fail_download = set(fail_download)
        self.downloads = []

    def call(self, endpoint, **params):
        if endpoint == "getStarred2":
            return {"starred2": {"song": self.songs,
                                 "album": [{"id": a} for a in self.albums]}}
        if endpoint == "getAlbum":
            return {"album": {"song": self.albums[params["id"]]}}
        if endpoint == "getPlaylists":
            return {"playlists": {"playlist": [
                {"id": pid, "name": name} for pid, (name, _) in self.playlists.items()]}}
        if endpoint == "getPlaylist":
            if params["id"] not in self.playlists:
                raise SubsonicError("getPlaylist: not found (code 70)")
            name, songs = self.playlists[params["id"]]
            return {"playlist": {"id": params["id"], "name": name,
                                 "changed": "2026-10-16", "entry": songs}}
        raise AssertionError(endpoint)

    def download(self, song_id, dest):
        self.downloads.append(song_id)
        if song_id in self.fail_download:
            raise SubsonicError("download: HTTP 500")
        with open(dest, "wb") as f:
            f.write(b"audio-" + song_id.encode())


def fake_convert(src, dst, options, duration=None):
    """Writes "<options>:<source bytes>" so tests can see how it was made."""
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(" ".join(options).encode() + b":" + f.read())


def config(device="stock_ipod", max_gb=20, convert_to="mp3", mp3_quality="v0",
           targets=("starred",)):
    return {"sync": {"targets": list(targets), "max_cache_gb": max_gb},
            "device": {"type": device, "convert_to": convert_to,
                       "mp3_quality": mp3_quality, "aac_bitrate": "256k"}}


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
        self.assertEqual(self.files(), ["a1.mp3", "a2.mp3"])

    def test_formats_for_stock_ipod(self):
        server = FakeServer(songs=[song("f", "flac"), song("m", "mp3"),
                                   song("a", "m4a"), song("o", "opus")])
        self.run_prepare(server)
        self.assertEqual(self.files(), ["a.m4a", "f.mp3", "m.mp3", "o.mp3"])
        with open(os.path.join(self.cache, "tracks", "m.mp3"), "rb") as f:
            self.assertEqual(f.read(), b"audio-m")          # copied, not re-encoded
        with open(os.path.join(self.cache, "tracks", "a.m4a"), "rb") as f:
            self.assertEqual(f.read(), b"audio-a")          # AAC/ALAC kept as-is
        with open(os.path.join(self.cache, "tracks", "f.mp3"), "rb") as f:
            self.assertTrue(f.read().startswith(b"-c:a libmp3lame -q:a 0"))   # converted

    def read(self, name):
        with open(os.path.join(self.cache, "tracks", name), "rb") as f:
            return f.read()

    def test_quality_change_reconverts_only_converted_tracks(self):
        server = FakeServer(songs=[song("f", "flac"), song("m", "mp3")])
        self.run_prepare(server, config(mp3_quality="v0"))
        summary = self.run_prepare(server, config(mp3_quality="320k"))
        self.assertEqual(summary["added"], 1)                 # only the FLAC
        self.assertTrue(self.read("f.mp3").startswith(b"-c:a libmp3lame -b:a 320k"))
        self.assertEqual(self.read("m.mp3"), b"audio-m")       # original untouched
        encodings = {t["id"]: t["encoding"] for t in self.manifest()["tracks"]}
        self.assertEqual(encodings, {"f": "mp3 320k", "m": "original"})
        server.downloads.clear()
        self.assertEqual(self.run_prepare(server, config(mp3_quality="320k"))["added"], 0)

    def test_old_manifest_without_encoding_is_not_reconverted(self):
        server = FakeServer(songs=[song("f", "flac")])
        self.run_prepare(server)
        m = self.manifest()
        for t in m["tracks"]:
            del t["encoding"]                                  # as written before
        dock_prepare.write_json_atomic(os.path.join(self.cache, "manifest.json"), m)
        server.downloads.clear()
        self.run_prepare(server)
        self.assertEqual(server.downloads, [])

    def test_audio_options(self):
        opts = dock_prepare.audio_options
        self.assertEqual(opts(config()["device"])[:4], ["-c:a", "libmp3lame", "-q:a", "0"])
        self.assertEqual(opts(config(mp3_quality="320K")["device"])[:4],
                         ["-c:a", "libmp3lame", "-b:a", "320k"])
        self.assertIn("-id3v2_version", opts(config()["device"]))

    def test_starred_and_playlist_share_tracks_once(self):
        # Album "alb" is starred; the playlist has one of its songs plus a
        # different album. Every song is stored once.
        server = FakeServer(albums={"alb": [song("a1"), song("a2")]},
                            playlists={"P1": ("Mel Test", [song("a2"), song("b1", album="Other"),
                                                           song("b2", album="Other")])})
        summary = self.run_prepare(server, config(targets=["starred", "playlist:Mel Test"]))
        self.assertEqual(self.files(), ["a1.mp3", "a2.mp3", "b1.mp3", "b2.mp3"])
        self.assertEqual(sorted(server.downloads), ["a1", "a2", "b1", "b2"])   # no repeats
        self.assertEqual(summary["total"], 4)
        m = self.manifest()
        sources = {t["id"]: t["sources"] for t in m["tracks"]}
        self.assertEqual(sources["a2"], ["starred", "playlist:Mel Test"])
        self.assertEqual(sources["b1"], ["playlist:Mel Test"])
        self.assertEqual(m["playlists"], [
            {"target": "starred", "id": None, "name": "Favorites", "changed": None,
             "song_ids": ["a1", "a2"]},
            {"target": "playlist:Mel Test", "id": "P1", "name": "Mel Test",
             "changed": "2026-10-16", "song_ids": ["a2", "b1", "b2"]}])
        plan = dock_prepare.show_plan(m)
        self.assertIn("iPod playlist 'Favorites': 2 songs from 1 album(s): Album", plan)
        self.assertIn("iPod playlist 'Mel Test': 3 songs from 2 album(s): Album, Other", plan)

    def test_favorites_playlist_dedupes_and_can_be_turned_off(self):
        # A starred song that's also on a starred album appears once.
        server = FakeServer(songs=[song("a2", track=2)],
                            albums={"alb": [song("a1", track=1), song("a2", track=2)]})
        self.run_prepare(server)
        self.assertEqual(self.manifest()["playlists"][0]["song_ids"], ["a1", "a2"])
        cfg = config()
        cfg["sync"]["favorites_playlist"] = ""
        self.run_prepare(server, cfg)
        self.assertEqual(self.manifest()["playlists"], [])

    def test_missing_playlist_stops_without_pruning(self):
        server = FakeServer(playlists={"P1": ("Mel Test", [song("b1")])})
        self.run_prepare(server, config(targets=["playlist:Mel Test"]))
        server.playlists = {}                       # gone (or a typo in the config)
        with self.assertRaisesRegex(dock_prepare.PrepareError, "not found"):
            self.run_prepare(server, config(targets=["playlist:Mel Test"]))
        self.assertEqual(self.files(), ["b1.mp3"])  # nothing deleted

    def test_renamed_playlist_found_by_previous_id(self):
        server = FakeServer(playlists={"P1": ("Mel Test", [song("b1")])})
        self.run_prepare(server, config(targets=["playlist:Mel Test"]))
        server.playlists = {"P1": ("Mel Road Trip", [song("b1"), song("b2")])}
        summary = self.run_prepare(server, config(targets=["playlist:Mel Test"]))
        self.assertEqual(summary["added"], 1)
        self.assertEqual(self.manifest()["playlists"][0]["name"], "Mel Road Trip")

    def test_duplicate_playlist_names_rejected(self):
        server = FakeServer(playlists={"P1": ("Mix", []), "P2": ("Mix", [])})
        with self.assertRaisesRegex(dock_prepare.PrepareError, "several"):
            self.run_prepare(server, config(targets=["playlist:Mix"]))

    def test_alac_target(self):
        self.run_prepare(FakeServer(songs=[song("f", "flac")]), config(convert_to="alac"))
        self.assertEqual(self.files(), ["f.m4a"])

    def test_changing_format_reconverts_old_files(self):
        # Switching from AAC to MP3: old .m4a conversions are replaced.
        server = FakeServer(songs=[song("f", "flac")])
        self.run_prepare(server, config(convert_to="aac"))
        self.assertEqual(self.files(), ["f.m4a"])
        summary = self.run_prepare(server, config(convert_to="mp3"))
        self.assertEqual(self.files(), ["f.mp3"])
        self.assertEqual((summary["added"], summary["removed"]), (1, 1))

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
        self.assertEqual(self.files(), ["a.mp3"])
        self.assertEqual(summary["removed"], 1)
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["a"])

    def test_one_failed_track_doesnt_block_others(self):
        server = FakeServer(songs=[song("a"), song("bad"), song("c")],
                            fail_download={"bad"})
        summary = self.run_prepare(server)
        self.assertEqual(self.files(), ["a.mp3", "c.mp3"])
        self.assertEqual([s["id"] for s in summary["skipped"]], ["bad"])
        self.assertTrue(summary["skipped"][0]["error"])
        self.assertNotIn("bad", [t["id"] for t in self.manifest()["tracks"]])

    def test_leftover_partial_files_are_never_queued(self):
        # Simulate a run cut off mid-conversion.
        os.makedirs(os.path.join(self.cache, "tmp"))
        with open(os.path.join(self.cache, "tmp", "a.mp3"), "wb") as f:
            f.write(b"half")
        self.run_prepare(FakeServer(songs=[song("a")]))
        self.assertFalse(os.path.exists(os.path.join(self.cache, "tmp")))
        with open(os.path.join(self.cache, "tracks", "a.mp3"), "rb") as f:
            self.assertTrue(f.read().endswith(b":audio-a"))

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

    def test_unknown_format_rejected(self):
        path = self.write('[server]\nurl="https://x"\nusername="u"\npassword="p"\n'
                          '[device]\nconvert_to="ogg"\n')
        with self.assertRaisesRegex(dock_config.ConfigError, "convert_to"):
            dock_config.load(path)

    def test_old_single_target_still_works(self):
        cfg = dock_config.load(self.write('[server]\nurl="https://x"\nusername="u"\n'
                                          'password="p"\n[sync]\ntarget="starred"\n'))
        self.assertEqual(cfg["sync"]["targets"], ["starred"])

    def test_bad_targets_rejected(self):
        base = '[server]\nurl="https://x"\nusername="u"\npassword="p"\n[sync]\n'
        for bad in ('targets="starred"', 'targets=[]', 'targets=["favorites"]',
                    'targets=["playlist:"]'):
            with self.assertRaisesRegex(dock_config.ConfigError, "targets"):
                dock_config.load(self.write(base + bad + "\n"))

    def test_mp3_quality_validated(self):
        base = '[server]\nurl="https://x"\nusername="u"\npassword="p"\n[device]\n'
        self.assertEqual(dock_config.load(self.write(base + 'mp3_quality="320K"\n'))
                         ["device"]["mp3_quality"], "320K")
        for bad in ("v10", "300k", "high"):
            with self.assertRaisesRegex(dock_config.ConfigError, "mp3_quality"):
                dock_config.load(self.write(base + f'mp3_quality="{bad}"\n'))

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
