"""Tests for genres looked up outside the library (dock_lookup) and their use
(dock_genres, dock_prepare). No network: every answer comes from a stand-in.

Run: python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import isolate_state  # noqa: E402,F401
import dock_genres as g  # noqa: E402
import dock_lookup as lk  # noqa: E402
import dock_prepare  # noqa: E402
from test_dock_prepare import FakeServer, ROOMY, config, fake_convert, song  # noqa: E402

MB, DZ = lk.MUSICBRAINZ, lk.DEEZER


class FakeFetch:
    """{url start: answer}; an answer that is an exception is raised."""

    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def __call__(self, url):
        self.urls.append(url)
        for start, answer in self.answers.items():
            if url.startswith(start):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return {}


def recording(rid, genres=(), isrcs=(), groups=(), artist=None):
    return {f"{MB}recording/{rid}?": {
        "genres": [{"name": n, "count": 9 - i} for i, n in enumerate(genres)],
        "isrcs": list(isrcs),
        "releases": [{"release-group": {"id": rg}} for rg in groups],
        "artist-credit": [{"artist": {"id": artist}}] if artist else []}}


class LookupTests(unittest.TestCase):
    def test_release_types_by_release_group_or_release(self):
        rg = "469f9762-7c38-4502-8ff9-418a3178826a"
        fetch = FakeFetch({f"{MB}release-group/{rg}?": {"primary-type": "Single"},
                           f"{MB}release/r1?": {"release-group": {
                               "primary-type": "Album", "secondary-types": ["Live"]}}})
        self.assertEqual(lk.release_group_types(f"album:release-group:{rg}", "x", fetch),
                         ["single"])                       # Aurral's album ID names it
        self.assertEqual(lk.release_group_types("al-123", "r1", fetch), ["album", "live"])
        self.assertEqual(lk.release_group_types("al-123", None, fetch), [])
        self.assertEqual(len(fetch.urls), 2)

    def test_a_recordings_own_genres_come_first(self):
        fetch = FakeFetch(recording("r1", ["drum and bass", "jungle"], groups=["g1"]))
        self.assertEqual(lk.musicbrainz("r1", fetch), (["drum and bass", "jungle"], []))
        self.assertEqual(len(fetch.urls), 1)               # nothing more asked

    def test_then_the_album_then_the_artist(self):
        fetch = FakeFetch({**recording("r1", groups=["g1"], artist="a1"),
                           f"{MB}release-group/g1?": {"genres": [{"name": "electro house",
                                                                   "count": 1}]}})
        self.assertEqual(lk.musicbrainz("r1", fetch)[0], ["electro house"])
        fetch = FakeFetch({**recording("r1", groups=["g1"], artist="a1"),
                           f"{MB}artist/a1?": {"genres": [{"name": "dubstep", "count": 1}]}})
        self.assertEqual(lk.musicbrainz("r1", fetch)[0], ["dubstep"])

    def test_deezer_by_isrc_when_musicbrainz_has_none(self):
        # 2026-11-14: Vulpey - Sever, no genres on MusicBrainz, "Electro, Dance" on Deezer.
        fetch = FakeFetch({**recording("r1", isrcs=["TCACM1662170"]),
                           f"{DZ}track/isrc:TCACM1662170": {"album": {"id": 77}},
                           f"{DZ}album/77": {"genres": {"data": [{"name": "Electro"},
                                                                 {"name": "Dance"}]}}})
        songs = [{"id": "s", "musicBrainzId": "r1"}]
        self.assertEqual(lk.album_genres(songs, fetch), ["Electro", "Dance"])

    def test_album_and_artist_are_asked_once_per_album(self):
        # Each MusicBrainz request costs a second; they're the same for every song.
        fetch = FakeFetch({**recording("r1", groups=["g1"], artist="a1"),
                           **recording("r2", groups=["g1"], artist="a1"),
                           **recording("r3", groups=["g1"], artist="a1")})
        songs = [{"id": i, "musicBrainzId": f"r{i}"} for i in (1, 2, 3)]
        self.assertEqual(lk.album_genres(songs, fetch), [])
        self.assertEqual(sum("release-group/" in u for u in fetch.urls), 1)
        self.assertEqual(sum("/artist/" in u for u in fetch.urls), 1)
        self.assertEqual(len(fetch.urls), 5)

    def test_navidromes_isrc_works_without_a_recording_id(self):
        fetch = FakeFetch({f"{DZ}track/isrc:X1": {"album": {"id": 5}},
                           f"{DZ}album/5": {"genres": {"data": [{"name": "Pop"}]}}})
        self.assertEqual(lk.album_genres([{"id": "s", "isrc": ["X1"]}], fetch), ["Pop"])
        self.assertTrue(all(u.startswith(DZ) for u in fetch.urls))

    def test_each_album_is_asked_once_and_empty_answers_again_later(self):
        albums = {"A": [{"id": "s", "musicBrainzId": "r1"}],
                  "B": [{"id": "t", "musicBrainzId": "r2"}]}
        fetch = FakeFetch(recording("r1", ["house"]))
        known = lk.lookup(albums, {}, fetch, now=1000)
        self.assertEqual(known, {"A": {"genres": ["house"], "checked": 1000},
                                 "B": {"genres": [], "checked": 1000}})
        fetch.urls.clear()
        self.assertEqual(lk.lookup(albums, known, fetch, now=2000), known)
        self.assertEqual(fetch.urls, [])
        later = 1000 + lk.RECHECK_DAYS * 86400 + 1
        lk.lookup(albums, known, fetch, now=later)
        self.assertTrue(fetch.urls and all("r2" in u or "r1" not in u for u in fetch.urls))

    def test_a_failure_keeps_nothing_and_stops(self):
        albums = {"A": [{"id": "s", "musicBrainzId": "r1"}],
                  "B": [{"id": "t", "musicBrainzId": "r2"}]}
        fetch = FakeFetch({f"{MB}recording/": lk.LookupFailed("offline")})
        self.assertEqual(lk.lookup(albums, {}, fetch, now=1), {})
        self.assertEqual(len(fetch.urls), 1)

    def test_songs_without_ids_ask_nothing(self):
        fetch = FakeFetch({})
        self.assertEqual(lk.lookup({"A": [{"id": "s"}]}, {}, fetch, now=5),
                         {"A": {"genres": [], "checked": 5}})
        self.assertEqual(fetch.urls, [])

    def test_runs_are_capped(self):
        albums = {f"A{i:03}": [{"id": str(i), "isrc": ["X"]}]
                  for i in range(lk.MAX_ALBUMS_PER_RUN + 5)}
        self.assertEqual(len(lk.lookup(albums, {}, FakeFetch({}), now=1)),
                         lk.MAX_ALBUMS_PER_RUN)


class FetcherTests(unittest.TestCase):
    def test_musicbrainz_is_asked_at_most_once_a_second_and_busy_is_retried(self):
        now, waits = [100.0], []

        def sleep(s):
            waits.append(round(s, 2))
            now[0] += s
        answers = [urllib.error.HTTPError("u", 503, "busy", {}, None),
                   io.BytesIO(b'{"ok": 1}'), io.BytesIO(b'{"ok": 2}')]

        def urlopen(request, timeout):
            self.assertIn("SongDock/", request.get_header("User-agent"))
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        fetch = lk.Fetcher(clock=lambda: now[0], sleep=sleep)
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(fetch(f"{MB}recording/x?fmt=json"), {"ok": 1})
            self.assertEqual(fetch(f"{MB}recording/y?fmt=json"), {"ok": 2})
        # The retry's own wait covered the gap; the next request waits its 1.1 s.
        self.assertEqual(waits, [2, 0.0, lk.MB_INTERVAL])

    def test_unreachable_raises(self):
        def urlopen(request, timeout):
            raise urllib.error.URLError("no route")
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(lk.LookupFailed):
                lk.Fetcher(sleep=lambda s: None)(f"{DZ}album/1")


def gsong(id, *tags, album="x", **kw):
    return {"id": id, "albumId": album, "genres": [{"name": t} for t in tags], **kw}


class AssignTests(unittest.TestCase):
    def test_only_albums_nothing_else_places_are_looked_up(self):
        songs = [gsong("a", "house", album="tagged"),
                 gsong("b", album="borrows", artist="Known"),
                 gsong("c", "rock", album="other", artist="Known"),
                 gsong("d", album="nothing", artist="New")]
        unplaced = set()
        out = g.assign(songs, g.DEFAULT_GROUPS, "Other", unplaced=unplaced)
        self.assertEqual(unplaced, {"nothing"})
        self.assertEqual(out["b"], "Rock")
        self.assertEqual(out["d"], "Other")

    def test_looked_up_genres_go_through_the_list(self):
        songs = [gsong("a", album="1"), gsong("b", album="1"), gsong("c", album="2"),
                 gsong("d", album="3")]
        out = g.assign(songs, g.DEFAULT_GROUPS, "",       # unmatched: the genre's own name
                       web_genres={"1": ["Electro", "Dance"], "2": ["Alternative"]})
        self.assertEqual(out, {"a": "Electronic", "b": "Electronic",
                               "c": "Alternative", "d": "Other"})

    def test_they_never_replace_a_genre_from_the_library(self):
        songs = [gsong("a", "hip hop", album="1")]
        self.assertEqual(g.assign(songs, g.DEFAULT_GROUPS, "Other",
                                  web_genres={"1": ["Electro"]}), {"a": "Hip-Hop"})


class PrepareTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = tmp.name

    def run_prepare(self, server, cfg, fetch):
        dock_prepare.prepare(server, cfg, self.cache, convert_fn=fake_convert,
                             disk_usage=ROOMY, player_kinds={"stock"}, lower_fn=None,
                             genre_fetch=fetch)
        with open(os.path.join(self.cache, "manifest.json")) as f:
            return json.load(f)

    def test_the_answer_is_kept_in_the_plan_and_used(self):
        server = FakeServer(songs=[song("s", albumId="A", musicBrainzId="r1", artist="New"),
                                   song("t", albumId="B", genre="hip hop")])
        cfg = config()
        cfg["sync"]["genre_lookup"] = True
        fetch = FakeFetch(recording("r1", ["electronic"]))
        plan = self.run_prepare(server, cfg, fetch)
        self.assertEqual({t["id"]: t["genre"] for t in plan["tracks"]},
                         {"s": "Electronic", "t": "Hip-Hop"})
        self.assertEqual(plan["web_genres"]["A"]["genres"], ["electronic"])
        self.assertNotIn("B", plan["web_genres"])
        fetch.urls.clear()
        self.run_prepare(server, cfg, fetch)
        self.assertEqual(fetch.urls, [])                    # asked once

    def test_off_asks_nothing(self):
        server = FakeServer(songs=[song("s", albumId="A", musicBrainzId="r1")])
        fetch = FakeFetch(recording("r1", ["electronic"]))
        cfg = config()
        cfg["sync"]["genre_lookup"] = False
        plan = self.run_prepare(server, cfg, fetch)
        self.assertEqual((fetch.urls, plan["tracks"][0]["genre"], "web_genres" in plan),
                         ([], "Other", False))


if __name__ == "__main__":
    unittest.main()
