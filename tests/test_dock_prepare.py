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

    def __init__(self, songs=(), albums=None, fail_download=(), playlists=None,
                 album_names=None, album_meta=None):
        self.songs = list(songs)
        self.albums = albums or {}            # album id -> [songs]
        self.album_names = album_names or {}  # album id -> the album's own name
        self.album_meta = album_meta or {}    # album id -> releaseTypes, songCount...
        self.playlists = playlists or {}      # playlist id -> (name, [songs][, comment])
        self.fail_download = set(fail_download)
        self.downloads = []

    def call(self, endpoint, **params):
        if endpoint == "getStarred2":
            return {"starred2": {"song": self.songs,
                                 "album": [{"id": a} for a in self.albums]}}
        if endpoint == "getAlbum":
            self.album_lookups = getattr(self, "album_lookups", []) + [params["id"]]
            return {"album": {"name": self.album_names.get(params["id"]),
                              **self.album_meta.get(params["id"], {}),
                              "song": self.albums.get(params["id"], [])}}
        if endpoint == "getPlaylists":
            return {"playlists": {"playlist": [
                {"id": pid, "name": v[0], **({"comment": v[2]} if len(v) > 2 else {})}
                for pid, v in self.playlists.items()]}}
        if endpoint == "getPlaylist":
            if params["id"] not in self.playlists:
                raise SubsonicError("getPlaylist: not found (code 70)")
            name, songs = self.playlists[params["id"]][:2]
            # Like a Navidrome smart playlist: "changed" moves on every read.
            self.reads = getattr(self, "reads", 0) + 1
            return {"playlist": {"id": params["id"], "name": name,
                                 "changed": f"2026-10-18T11:{self.reads:02d}",
                                 "entry": songs}}
        raise AssertionError(endpoint)

    def cover_art(self, cover_id, size=None):
        self.cover_fetches = getattr(self, "cover_fetches", []) + [cover_id]
        if cover_id in getattr(self, "covers", {}):
            return self.covers[cover_id]
        raise SubsonicError("getCoverArt: not found (code 70)", code=70)

    def download(self, song_id, dest):
        self.downloads.append(song_id)
        if song_id in getattr(self, "offline_from", ()):
            raise SubsonicError("download: can't reach server (name resolution)",
                                network=True)
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
                       "mp3_quality": mp3_quality, "aac_bitrate": "256k"},
            # No "release:" groups, so album lookups stay what each test expects.
            "genres": {"unmatched": "Other",
                       "groups": [{"name": "Electronic", "match": ["electronic", "dance"]},
                                  {"name": "Hip-Hop", "match": ["hip hop"]}]}}


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

    def test_one_album_gets_one_name(self):
        # Navidrome groups by album ID; a song's own album field can differ.
        re = "Hold Your Colour (2007 re-release)"
        songs = [song("p1", album=re, albumId="hyc", track=1),
                 song("p10", album="Hold Your Colour", albumId="hyc", track=10)]
        server = FakeServer(albums={"hyc": songs}, album_names={"hyc": re})
        self.run_prepare(server)
        self.assertEqual({t["album"] for t in self.manifest()["tracks"]}, {re})
        self.assertEqual(server.album_lookups, ["hyc"])        # no extra lookup

    def test_album_name_looked_up_only_when_songs_disagree(self):
        server = FakeServer(songs=[song("a", album="X (deluxe)", albumId="x"),
                                   song("b", album="X", albumId="x"),
                                   song("c", album="Y", albumId="y")],
                            album_names={"x": "X (deluxe)"})
        self.run_prepare(server)
        albums = {t["id"]: t["album"] for t in self.manifest()["tracks"]}
        self.assertEqual(albums, {"a": "X (deluxe)", "b": "X (deluxe)", "c": "Y"})
        self.assertEqual(server.album_lookups, ["x"])

    def test_same_recording_on_single_and_album_kept_once_from_album(self):
        single = song("s1", title="Liebe", album="Liebe", albumId="sgl",
                      musicBrainzId="REC-1", duration=166)
        album = song("a1", title="Liebe", album="Squaredance", albumId="alb",
                     musicBrainzId="rec-1", duration=167)       # case, 1 s apart
        server = FakeServer(songs=[single], playlists={"p1": ("Mix", [single, album], "#ipod")},
                            album_meta={"sgl": {"releaseTypes": ["single"], "songCount": 1},
                                        "alb": {"releaseTypes": ["album"], "songCount": 31}})
        cfg = config()
        cfg["sync"]["playlist_tag"] = "#ipod"
        self.run_prepare(server, cfg)
        m = self.manifest()
        self.assertEqual([t["id"] for t in m["tracks"]], ["a1"])
        self.assertEqual(sorted(m["tracks"][0]["sources"]), ["playlist:Mix", "starred"])
        lists = {p["name"]: p["song_ids"] for p in m["playlists"]}
        self.assertEqual(lists, {"Favorites": ["a1"], "Mix": ["a1"]})   # listed once

    def test_album_preferred_over_release_without_a_type(self):
        a = song("old", album="I Kind of Really Want...", albumId="x", musicBrainzId="r")
        b = song("new", album="Squaredance", albumId="y", musicBrainzId="r")
        server = FakeServer(songs=[a, b], album_meta={"x": {"releaseTypes": [], "songCount": 26},
                                                      "y": {"releaseTypes": ["album"],
                                                            "songCount": 31}})
        self.run_prepare(server)
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["new"])

    def test_different_lengths_or_no_ids_are_not_merged(self):
        studio = song("st", title="Look at the Sky", albumId="n", musicBrainzId="r1",
                      duration=309)
        live = song("lv", title="Look at the Sky", albumId="l", musicBrainzId="r1",
                    duration=370)                                # same ID, other length
        a = song("x1", title="Intro", albumId="p")                # no IDs: never merged
        b = song("x2", title="Intro", albumId="q")
        self.run_prepare(FakeServer(songs=[studio, live, a, b]))
        self.assertEqual(sorted(t["id"] for t in self.manifest()["tracks"]),
                         ["lv", "st", "x1", "x2"])

    def test_isrc_identifies_a_recording_without_musicbrainz(self):
        a = song("a", albumId="s", isrc=["USQE92000004"])
        b = song("b", albumId="l", isrc=["usqe92000004"])
        server = FakeServer(songs=[a, b], album_meta={"s": {"releaseTypes": ["single"]},
                                                      "l": {"releaseTypes": ["album"]}})
        self.run_prepare(server)
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["b"])

    def test_manifest_carries_one_genre_per_album(self):
        server = FakeServer(songs=[
            song("a", albumId="x", genres=[{"name": "rave"}, {"name": "dance"}]),
            song("b", albumId="x", genres=[{"name": "electronic"}]),
            song("c", albumId="x", genres=[{"name": "hip hop"}]),      # outvoted
            song("d", albumId="y", genre="jazz fusion")])              # no group
        self.run_prepare(server)
        genres = {t["id"]: t["genre"] for t in self.manifest()["tracks"]}
        self.assertEqual(genres, {"a": "Electronic", "b": "Electronic",
                                  "c": "Electronic", "d": "Other"})
        self.assertNotIn("album_types", self.manifest())     # no "release:" groups

    def test_release_types_looked_up_once_and_kept(self):
        cfg = config()
        cfg["genres"]["groups"].insert(0, {"name": "Live", "match": ["release:live"]})
        server = FakeServer(songs=[song("a", albumId="x", genres=[{"name": "electronic"}]),
                                   song("b", albumId="y", genres=[{"name": "electronic"}])],
                            album_meta={"x": {"releaseTypes": "album;live"},   # a string
                                        "y": {"releaseTypes": ["album"]}})
        self.run_prepare(server, cfg)
        m = self.manifest()
        self.assertEqual({t["id"]: t["genre"] for t in m["tracks"]},
                         {"a": "Live", "b": "Electronic"})
        self.assertEqual(m["album_types"], {"x": ["album", "live"], "y": ["album"]})
        server.album_lookups = []
        self.run_prepare(server, cfg)
        self.assertEqual(server.album_lookups, [])             # remembered

    def test_release_types_given_as_one_string_rank_correctly(self):
        # Read letter by letter, "album" ranked below a single (2026-10-19).
        a = song("lp", album="Squaredance", albumId="x", musicBrainzId="r")
        b = song("single", album="Liebe", albumId="y", musicBrainzId="r")
        server = FakeServer(songs=[a, b],
                            album_meta={"x": {"releaseTypes": "album", "songCount": 12},
                                        "y": {"releaseTypes": ["single"], "songCount": 1}})
        self.run_prepare(server)
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["lp"])

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
            {"target": "starred", "id": None, "name": "Favorites",
             "via": "starred", "song_ids": ["a1", "a2"]},
            {"target": "playlist:Mel Test", "id": "P1", "name": "Mel Test",
             "via": "config", "song_ids": ["a2", "b1", "b2"]}])
        plan = dock_prepare.show_plan(m)
        self.assertIn("iPod playlist 'Favorites': 2 songs from 1 album(s): Album", plan)
        self.assertIn("iPod playlist 'Mel Test' [config]: 3 songs from 2 album(s): "
                      "Album, Other", plan)

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

    def tagged_cfg(self, targets=("starred",)):
        cfg = config(targets=targets)
        cfg["sync"]["playlist_tag"] = "#ipod"
        return cfg

    def test_tagged_playlists_sync_without_config(self):
        server = FakeServer(playlists={
            "P1": ("Mel Test", [song("b1")], "road trip #iPod"),     # any case
            "P2": ("Podcasts", [song("c1")], "#ipodcast"),           # not the tag
            "P3": ("Untagged", [song("d1")])})
        self.run_prepare(server, self.tagged_cfg())
        self.assertEqual(self.files(), ["b1.mp3"])
        pls = [p for p in self.manifest()["playlists"] if p["via"] != "starred"]
        self.assertEqual([(p["name"], p["via"]) for p in pls], [("Mel Test", "tag")])

    def test_tagged_and_configured_playlist_counted_once(self):
        server = FakeServer(playlists={"P1": ("Mel Test", [song("b1")], "#ipod")})
        self.run_prepare(server, self.tagged_cfg(["playlist:Mel Test"]))
        self.assertEqual([p["via"] for p in self.manifest()["playlists"]], ["config"])

    def test_removing_tag_unsyncs_playlist(self):
        server = FakeServer(songs=[song("a1")],
                            playlists={"P1": ("Mel Test", [song("b1")], "#ipod")})
        self.run_prepare(server, self.tagged_cfg())
        server.playlists["P1"] = ("Mel Test", [song("b1")], "")      # tag removed
        cfg = self.tagged_cfg()
        cfg["sync"]["removal_grace_days"] = 0
        summary = self.run_prepare(server, cfg)
        self.assertEqual(self.files(), ["a1.mp3"])
        self.assertEqual(summary["removed"], 1)

    def test_empty_playlist_listing_stops_without_pruning(self):
        server = FakeServer(playlists={"P1": ("Mel Test", [song("b1")], "#ipod")})
        self.run_prepare(server, self.tagged_cfg())
        server.playlists = {}                       # server glitch: no playlists at all
        with self.assertRaisesRegex(dock_prepare.PrepareError, "stopping without removing"):
            self.run_prepare(server, self.tagged_cfg())
        self.assertEqual(self.files(), ["b1.mp3"])

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

    def test_unchanged_plan_not_rewritten(self):
        server = FakeServer(songs=[song("a")])
        self.run_prepare(server)
        path = os.path.join(self.cache, "manifest.json")
        before = (os.stat(path).st_mtime_ns, self.manifest()["generated"])
        os.utime(path, ns=(1, 1))                       # mark the file
        self.run_prepare(server)                        # nothing changed
        self.assertEqual(os.stat(path).st_mtime_ns, 1)  # not rewritten
        self.assertEqual(self.manifest()["generated"], before[1])
        server.songs.append(song("b"))
        self.run_prepare(server)                        # something changed
        self.assertNotEqual(os.stat(path).st_mtime_ns, 1)
        self.assertEqual(len(self.manifest()["tracks"]), 2)

    def test_playlist_read_time_doesnt_rewrite_the_plan(self):
        # Smart playlists report a new "changed" time on every read; only a
        # change in songs should rewrite manifest.json (SD card wear).
        server = FakeServer(playlists={"p1": ("Mix", [song("a")], "#ipod")})
        cfg = config()
        cfg["sync"]["playlist_tag"] = "#ipod"
        self.run_prepare(server, cfg)
        self.assertEqual([p["name"] for p in self.manifest()["playlists"]],
                         ["Favorites", "Mix"])
        path = os.path.join(self.cache, "manifest.json")
        os.utime(path, ns=(1, 1))
        self.run_prepare(server, cfg)
        self.assertEqual(os.stat(path).st_mtime_ns, 1)

    def test_unstarred_tracks_are_pruned(self):
        server = FakeServer(songs=[song("a"), song("b")])
        cfg = config()
        cfg["sync"]["removal_grace_days"] = 0             # no grace: delete at once
        self.run_prepare(server, cfg)
        server.songs = [song("a")]
        summary = self.run_prepare(server, cfg)
        self.assertEqual(self.files(), ["a.mp3"])
        self.assertEqual(summary["removed"], 1)
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["a"])

    def grace_run(self, server, days=7, now=0, **kw):
        cfg = config()
        cfg["sync"]["removal_grace_days"] = days
        return self.run_prepare(server, cfg, clock=lambda: now * dock_prepare.DAY, **kw)

    def test_removed_track_kept_for_grace_period_and_reused(self):
        server = FakeServer(songs=[song("a"), song("b")])
        self.grace_run(server, now=0)
        server.songs = [song("a")]                       # b unstarred
        summary = self.grace_run(server, now=1)
        self.assertEqual(summary["removed"], 1)
        self.assertEqual(self.files(), ["a.mp3", "b.mp3"])           # b still cached
        self.assertEqual([t["id"] for t in self.manifest()["tracks"]], ["a"])  # not planned
        self.assertEqual(self.manifest()["kept_after_removal"], 1)
        self.assertEqual(self.grace_run(server, now=2)["removed"], 0)  # not re-counted
        server.songs = [song("a"), song("b")]           # re-starred on day 3
        server.downloads.clear()
        self.grace_run(server, now=3)
        self.assertEqual(server.downloads, [])                         # reused, no download
        self.assertEqual(sorted(t["id"] for t in self.manifest()["tracks"]), ["a", "b"])

    def test_kept_track_deleted_after_grace_period(self):
        server = FakeServer(songs=[song("a"), song("b")])
        self.grace_run(server, now=0)
        server.songs = [song("a")]
        self.grace_run(server, days=7, now=1)
        self.grace_run(server, days=7, now=7.9)
        self.assertIn("b.mp3", self.files())
        self.grace_run(server, days=7, now=8.1)
        self.assertEqual(self.files(), ["a.mp3"])

    def test_low_disk_deletes_oldest_kept_first(self):
        server = FakeServer(songs=[song("a"), song("b"), song("c")])
        self.grace_run(server, now=0)
        server.songs = [song("a"), song("c")]           # b leaves on day 1
        self.grace_run(server, now=1)
        server.songs = [song("a")]                      # c leaves on day 2
        self.grace_run(server, now=2)
        usage = {"calls": 0}

        def disk(path):
            usage["calls"] += 1
            free = 0.20 if usage["calls"] == 1 else 0.30
            return Usage(100e9, 100e9 * (1 - free), 100e9 * free)
        self.grace_run(server, now=3, disk_usage=disk)
        self.assertEqual(self.files(), ["a.mp3", "c.mp3"])   # oldest (b) went first

    def test_kept_tracks_make_room_for_new_music(self):
        server = FakeServer(songs=[song("a")])
        self.grace_run(server, now=0)
        with open(os.path.join(self.cache, "tracks", "a.mp3"), "ab") as f:
            f.write(b"x" * 10_000)                       # a really takes space
        server.songs = [song("b", size=10)]             # a leaves, b arrives
        cfg = config(max_gb=0.000005)                    # 5 KB cache limit
        cfg["sync"]["removal_grace_days"] = 7
        summary = self.run_prepare(server, cfg, clock=lambda: dock_prepare.DAY)
        self.assertEqual(summary["added"], 1)
        self.assertEqual(self.files(), ["b.mp3"])

    def test_server_dropping_out_stops_the_batch(self):
        server = FakeServer(songs=[song(f"t{i}") for i in range(6)])
        server.offline_from = {f"t{i}" for i in range(2, 6)}   # Wi-Fi drops at t2
        cfg = config()
        cfg["sync"]["workers"] = 1
        summary = self.run_prepare(server, cfg)
        self.assertEqual(summary["added"], 2)
        self.assertLessEqual(len(server.downloads), 4)          # didn't try all 6
        self.assertTrue(all(not s["error"] for s in summary["skipped"]))  # not errors
        self.assertEqual(len(summary["skipped"]), 4)
        server.offline_from = set()                              # back online
        self.assertEqual(self.run_prepare(server, cfg)["added"], 4)

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

    # A full cache and a player bigger than it: songs confirmed on the player
    # give up their cache files (cache limit here: 12,000 bytes).
    SMALL = 0.000012

    def cached(self, name, size):
        os.makedirs(os.path.join(self.cache, "tracks"), exist_ok=True)
        with open(os.path.join(self.cache, "tracks", name), "wb") as f:
            f.write(b"x" * size)

    def test_full_cache_drops_largest_track_on_the_player_for_a_new_one(self):
        self.cached("a.mp3", 6000)
        self.cached("b.mp3", 5000)
        server = FakeServer(songs=[song("a"), song("b"), song("c", size=1500)])
        summary = self.run_prepare(server, cfg=config(max_gb=self.SMALL),
                                   on_player={"a": {"mp3 v0"}, "b": {"mp3 v0"}})
        self.assertEqual(server.downloads, ["c"])
        self.assertEqual(self.files(), ["b.mp3", "c.mp3"])          # a: largest
        m = self.manifest()
        self.assertEqual([t["id"] for t in m["on_player"]], ["a"])
        self.assertEqual(m["on_player"][0]["encoding"], "mp3 v0")
        self.assertEqual(m["skipped"], [])
        self.assertEqual((summary["ready"], summary["evicted"]), (3, 1))

    def test_full_cache_never_drops_a_track_the_player_lacks(self):
        self.cached("a.mp3", 6000)
        self.cached("b.mp3", 5000)
        server = FakeServer(songs=[song("a"), song("b"), song("c", size=1500)])
        summary = self.run_prepare(server, cfg=config(max_gb=self.SMALL),
                                   on_player={"a": {"mp3 v0"}})
        self.assertEqual(summary["evicted"], 1)                      # only a can go
        self.assertIn("b.mp3", self.files())
        self.cached("d.mp3", 6000)                                    # full again
        server.songs.append(song("e", size=1500))
        server.songs.append(song("d"))
        summary = self.run_prepare(server, cfg=config(max_gb=self.SMALL),
                                   on_player={"a": {"mp3 v0"}})
        self.assertEqual(summary["evicted"], 0)
        self.assertEqual([s["id"] for s in summary["skipped"]], ["e"])
        self.assertNotIn("e", server.downloads[1:])

    def test_held_tracks_dont_push_each_other_out(self):
        # The player has both; only b fits. No churn: a stays uncached.
        self.cached("b.mp3", 5000)
        server = FakeServer(songs=[song("a", size=5000), song("b")])
        summary = self.run_prepare(server, cfg=config(max_gb=self.SMALL),
                                   on_player={"a": {"mp3 v0"}, "b": {"mp3 v0"}})
        self.assertEqual(server.downloads, [])
        self.assertEqual(self.files(), ["b.mp3"])
        self.assertEqual([t["id"] for t in self.manifest()["on_player"]], ["a"])
        self.assertEqual((summary["skipped"], summary["ready"]), ([], 2))

    def test_held_tracks_are_still_cached_while_there_is_room(self):
        server = FakeServer(songs=[song("a")])
        self.run_prepare(server, on_player={"a": {"mp3 v0"}})
        self.assertEqual(self.files(), ["a.mp3"])
        self.assertEqual(self.manifest()["on_player"], [])

    def test_track_on_the_player_in_an_old_encoding_isnt_held(self):
        # It needs the new file to be replaced, so it's never dropped.
        self.cached("a.mp3", 6000)
        self.cached("b.mp3", 5000)
        server = FakeServer(songs=[song("a"), song("b"), song("c", size=1500)])
        summary = self.run_prepare(server, cfg=config(max_gb=self.SMALL),
                                   on_player={"a": {"mp3 v2"}, "b": {"mp3 v0", None}})
        self.assertEqual(self.files(), ["a.mp3", "c.mp3"])            # b went, not a
        self.assertEqual(summary["evicted"], 1)

    def test_stops_at_cache_limit(self):
        server = FakeServer(songs=[song("a", size=600_000_000)])
        summary = self.run_prepare(server, cfg=config(max_gb=1))
        self.assertEqual(summary["ready"], 0)
        self.assertIn("max_cache_gb", summary["skipped"][0]["reason"])

    def test_queued_tracks_dont_count_against_the_cache_limit(self):
        # Each FLAC reserves 2x 300 KB while it's being prepared. Ten of them
        # reserved at once (6 MB) would pass the 2 MB limit, but only the
        # ones in progress hold space, and the finished files are tiny.
        server = FakeServer(songs=[song(f"t{i}", size=300_000) for i in range(10)])
        cfg = config(max_gb=0.002)
        cfg["sync"]["workers"] = 1
        summary = self.run_prepare(server, cfg)
        self.assertEqual(summary["skipped"], [])
        self.assertEqual(summary["added"], 10)

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


try:
    import io
    from PIL import Image
    from mutagen.id3 import APIC, ID3
    HAVE_COVER_TOOLS = True
except ImportError:
    HAVE_COVER_TOOLS = False


@unittest.skipUnless(HAVE_COVER_TOOLS, "needs Pillow and mutagen (the Pi venv)")
class ShrinkCoverTests(unittest.TestCase):
    AUDIO = b"\xff\xfb\x90\x00" * 5000          # stands in for MP3 frames

    def make_mp3(self, size_px, fmt="JPEG"):
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(self.AUDIO)
        f.close()
        self.addCleanup(os.remove, f.name)
        buf = io.BytesIO()
        Image.new("RGB", (size_px, size_px), (200, 30, 90)).save(buf, fmt)
        tags = ID3()
        tags.add(APIC(encoding=3, mime=f"image/{fmt.lower()}", type=3, desc="", data=buf.getvalue()))
        tags.save(f.name, v2_version=3)
        return f.name

    def audio_bytes(self, path):
        with open(path, "rb") as f:
            data = f.read()
        return data[data.index(self.AUDIO[:4]):]

    def cover(self, path):
        pic = ID3(path).getall("APIC")[0]
        return Image.open(io.BytesIO(pic.data)), pic

    def test_big_cover_shrunk_audio_untouched(self):
        path = self.make_mp3(2000)
        before = self.audio_bytes(path)
        self.assertEqual(dock_prepare.shrink_cover(path), "shrunk")
        im, pic = self.cover(path)
        self.assertEqual(im.size, (600, 600))
        self.assertEqual(pic.mime, "image/jpeg")
        self.assertEqual(self.audio_bytes(path), before)
        self.assertEqual(ID3(path).version[1], 3)          # still ID3v2.3

    def test_small_jpeg_kept(self):
        path = self.make_mp3(500)
        self.assertEqual(dock_prepare.shrink_cover(path), "kept")

    def test_png_cover_becomes_jpeg(self):
        path = self.make_mp3(500, fmt="PNG")
        self.assertEqual(dock_prepare.shrink_cover(path), "shrunk")
        self.assertEqual(self.cover(path)[0].format, "JPEG")

    def test_album_cover_decoded_once(self):
        a, b = self.make_mp3(2000), self.make_mp3(2000)
        cache = {}
        dock_prepare.shrink_cover(a, cache)
        dock_prepare.shrink_cover(b, cache)
        self.assertEqual(len(cache), 1)

    def test_no_tag_or_cover(self):
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(self.AUDIO)
        f.close()
        self.addCleanup(os.remove, f.name)
        self.assertEqual(dock_prepare.shrink_cover(f.name), "none")

    # Navidrome's cover for files without one (e.g. art in a folder cover.jpg).
    def bare_mp3(self):
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(self.AUDIO)
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    class CoverServer:
        def __init__(self, image=None, network=False):
            self.image, self.network, self.fetches = image, network, []

        def cover_art(self, cover_id, size=None):
            self.fetches.append((cover_id, size))
            if self.network:
                raise SubsonicError("getCoverArt: can't reach server", network=True)
            if self.image is None:
                raise SubsonicError("getCoverArt: not found (code 70)", code=70)
            return self.image

    def png(self, px):
        buf = io.BytesIO()
        Image.new("RGB", (px, px), (10, 200, 90)).save(buf, "PNG")
        return buf.getvalue()

    def test_server_cover_embedded_as_600px_jpeg(self):
        path = self.bare_mp3()
        server = self.CoverServer(self.png(1000))
        t = {"id": "s1", "coverArt": "dc-alb:1_0", "albumId": "alb"}
        self.assertEqual(dock_prepare.add_server_cover(path, t, server, {}), "navidrome")
        im, pic = self.cover(path)
        self.assertEqual((im.format, im.size, pic.mime), ("JPEG", (600, 600), "image/jpeg"))
        self.assertEqual(self.audio_bytes(path), self.AUDIO)
        self.assertEqual(server.fetches, [("dc-alb:1_0", 600)])   # the song's own ID

    def test_server_cover_fetched_once_per_album(self):
        server, images = self.CoverServer(self.png(300)), {}
        t = {"id": "s1", "albumId": "alb"}
        for _ in range(3):
            dock_prepare.add_server_cover(self.bare_mp3(), t, server, images)
        self.assertEqual(server.fetches, [("alb", 600)])

    def test_server_without_cover(self):
        path = self.bare_mp3()
        result = dock_prepare.add_server_cover(path, {"id": "s1", "albumId": "alb"},
                                               self.CoverServer(None), {})
        self.assertEqual(result, "missing")
        self.assertEqual(dock_prepare.shrink_cover(path), "none")

    def test_prepare_rechecks_cached_tracks_without_covers(self):
        with tempfile.TemporaryDirectory() as cache:
            server = FakeServer(songs=[song("a1", suffix="mp3", coverArt="dc-x"),
                                       song("b1", suffix="mp3", albumId="nope")])
            mpath = os.path.join(cache, "manifest.json")

            def run():
                prepare(server, config(), cache, convert_fn=fake_convert, disk_usage=ROOMY)
                with open(mpath) as f:
                    return json.load(f)

            covers = {t["id"]: t["cover"] for t in run()["tracks"]}
            self.assertEqual(covers, {"a1": "missing", "b1": "missing"})
            server.cover_fetches = []
            run()                               # "missing" isn't asked again
            self.assertEqual(server.cover_fetches, [])
            # A track from before this change ("none"): asked once more.
            with open(mpath) as f:
                m = json.load(f)
            for t in m["tracks"]:
                t["cover"] = "none"
            with open(mpath, "w") as f:
                json.dump(m, f)
            server.covers = {"dc-x": self.png(800)}
            covers = {t["id"]: t["cover"] for t in run()["tracks"]}
            self.assertEqual(covers, {"a1": "navidrome", "b1": "missing"})
            self.assertEqual(self.cover(os.path.join(cache, "tracks", "a1.mp3"))[0].size,
                             (600, 600))

    def test_server_unreachable_raises(self):
        with self.assertRaises(SubsonicError):
            dock_prepare.add_server_cover(self.bare_mp3(), {"id": "s1", "albumId": "a"},
                                          self.CoverServer(network=True), {})


class WorkerTests(unittest.TestCase):
    def test_worker_count(self):
        rw = dock_prepare.resolve_workers
        self.assertEqual(rw("auto", cpu_count=1), 1)     # Pi Zero W
        self.assertEqual(rw("auto", cpu_count=4), 3)     # Pi Zero 2 W
        self.assertEqual(rw(2, cpu_count=4), 2)

    def run_slots(self, limit, temp, jobs=6):
        import threading
        import time as _time
        slots = dock_prepare.ConversionSlots(limit, temp=temp, recheck=0.01)
        peak = {"now": 0, "max": 0}
        guard = threading.Lock()

        def job():
            with slots:
                with guard:
                    peak["now"] += 1
                    peak["max"] = max(peak["max"], peak["now"])
                _time.sleep(0.05)
                with guard:
                    peak["now"] -= 1
        threads = [threading.Thread(target=job) for _ in range(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return peak["max"]

    def test_slots_cap_parallel_conversions(self):
        self.assertEqual(self.run_slots(3, temp=lambda: 50.0), 3)

    def test_hot_pi_converts_one_at_a_time(self):
        self.assertEqual(self.run_slots(3, temp=lambda: 80.0), 1)

    def test_parallel_run_matches_sequential(self):
        songs = [song(f"t{i:02d}", track=i) for i in range(12)]
        results = []
        for workers in (1, 4):
            with tempfile.TemporaryDirectory() as cache:
                cfg = config()
                cfg["sync"]["workers"] = workers
                server = FakeServer(songs=songs)
                summary = prepare(server, cfg, cache, convert_fn=fake_convert,
                                  disk_usage=ROOMY)
                with open(os.path.join(cache, "manifest.json")) as f:
                    ids = [t["id"] for t in json.load(f)["tracks"]]
                results.append((summary["added"], sorted(os.listdir(
                    os.path.join(cache, "tracks"))), ids))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1][0], 12)


class SyncFirstTests(unittest.TestCase):
    def test_waits_while_a_sync_runs(self):
        states = iter([True, True, False])          # sync busy for two checks
        slept = []
        waited = dock_prepare.wait_for_syncs(check=lambda: next(states),
                                             sleep=slept.append, poll=15)
        self.assertEqual((waited, slept), (30, [15, 15]))

    def test_no_wait_when_idle(self):
        self.assertEqual(dock_prepare.wait_for_syncs(check=lambda: False,
                                                     sleep=self.fail), 0)


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

    def test_grace_days_validated(self):
        base = '[server]\nurl="https://x"\nusername="u"\npassword="p"\n[sync]\n'
        self.assertEqual(dock_config.load(self.write(base))["sync"]["removal_grace_days"], 7)
        self.assertEqual(dock_config.load(self.write(base + "removal_grace_days=0\n"))
                         ["sync"]["removal_grace_days"], 0)
        for bad in ("removal_grace_days=31", "removal_grace_days=-1",
                    'removal_grace_days="7"', "removal_grace_days=2.5"):
            with self.assertRaisesRegex(dock_config.ConfigError, "removal_grace_days"):
                dock_config.load(self.write(base + bad + "\n"))

    def test_workers_validated(self):
        base = '[server]\nurl="https://x"\nusername="u"\npassword="p"\n[sync]\n'
        self.assertEqual(dock_config.load(self.write(base + 'workers=3\n'))["sync"]["workers"], 3)
        for bad in ('workers=0', 'workers="many"', 'workers=true'):
            with self.assertRaisesRegex(dock_config.ConfigError, "workers"):
                dock_config.load(self.write(base + bad + "\n"))

    def test_home_expanded_from_config_owner(self):
        path = self.write('[server]\nurl="https://x"\nusername="u"\npassword="p"\n')
        import pwd
        owner_home = pwd.getpwuid(os.stat(path).st_uid).pw_dir
        cfg = dock_config.load(path)
        self.assertEqual(cfg["sync"]["cache_dir"], owner_home + "/musicdock/cache")
        self.assertFalse(cfg["device"]["write"])                 # read-only by default

    def test_write_must_be_boolean(self):
        path = self.write('[server]\nurl="https://x"\nusername="u"\npassword="p"\n'
                          '[device]\nwrite="yes"\n')
        with self.assertRaisesRegex(dock_config.ConfigError, "write"):
            dock_config.load(path)

    def test_auto_resync_defaults_on_and_must_be_boolean(self):
        base = '[server]\nurl="https://x"\nusername="u"\npassword="p"\n'
        self.assertIs(dock_config.load(self.write(base))["device"]["auto_resync"], True)
        with self.assertRaisesRegex(dock_config.ConfigError, "auto_resync"):
            dock_config.load(self.write(base + '[device]\nauto_resync="no"\n'))

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



class ConvertCommandTests(unittest.TestCase):
    def ffmpeg_args(self, src):
        from unittest import mock
        calls = []

        def run(args, **kw):
            calls.append(args)
            return mock.Mock(returncode=0, stderr="")
        with mock.patch.object(dock_prepare.subprocess, "run", run):
            dock_prepare.convert(src, "out.mp3", ["-c:a", "libmp3lame"])
        args = calls[0]
        return args[args.index("-map_metadata") + 1]

    def test_ogg_and_opus_tags_come_from_the_audio_stream(self):
        self.assertEqual(self.ffmpeg_args("src-x.opus"), "0:s:a:0")
        self.assertEqual(self.ffmpeg_args("src-x.OGG"), "0:s:a:0")
        self.assertEqual(self.ffmpeg_args("src-x.flac"), "0")


def have_mutagen():
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(have_mutagen(), "needs mutagen (the Pi venv)")
class MissingTagTests(unittest.TestCase):
    FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413      # one silent MP3 frame

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def untagged_convert(self, src, dst, options, duration=None):
        with open(dst, "wb") as f:                    # like ffmpeg dropping Ogg tags
            f.write(self.FRAME * 40)

    def tags(self, name):
        from mutagen.id3 import ID3
        t = ID3(os.path.join(self.cache, "tracks", name))
        return {k: str(t[k]) for k in ("TIT2", "TPE1", "TALB", "TRCK") if k in t}, t.version

    def test_fills_only_missing_tags(self):
        from mutagen.id3 import ID3, TIT2
        path = os.path.join(self.cache, "x.mp3")
        with open(path, "wb") as f:
            f.write(self.FRAME * 40)
        tags = ID3()
        tags.add(TIT2(encoding=3, text="File title"))
        tags.save(path, v2_version=3)
        changed = dock_prepare.fill_missing_tags(
            path, {"id": "x", "title": "Navidrome title", "artist": "LEASHES"})
        self.assertTrue(changed)
        t = ID3(path)
        self.assertEqual((str(t["TIT2"]), str(t["TPE1"])), ("File title", "LEASHES"))
        self.assertEqual(t.version[:2], (2, 3))
        self.assertFalse(dock_prepare.fill_missing_tags(path, {"title": "x", "artist": "y"}))

    def test_converted_ogg_gets_navidrome_tags_and_old_ones_are_repaired(self):
        server = FakeServer(songs=[song("f", "ogg", title="FERAL", artist="LEASHES",
                                        album="FERAL", track=3)])
        prepare(server, config(), self.cache, convert_fn=self.untagged_convert,
                disk_usage=ROOMY)
        self.assertEqual(self.tags("f.mp3"),
                         ({"TIT2": "FERAL", "TPE1": "LEASHES", "TALB": "FERAL",
                           "TRCK": "3"}, (2, 3, 0)))
        from mutagen.id3 import ID3
        ID3(os.path.join(self.cache, "tracks", "f.mp3")).delete()   # an old untagged copy
        prepare(server, config(), self.cache, convert_fn=self.untagged_convert,
                disk_usage=ROOMY)
        self.assertEqual(server.downloads, ["f"])                   # no re-download
        self.assertEqual(self.tags("f.mp3")[0]["TIT2"], "FERAL")


if __name__ == "__main__":
    unittest.main()
