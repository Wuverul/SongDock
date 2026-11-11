"""Tests for the player's genre list (dock_genres). No network needed.

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import dock_genres as g  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def song(id, *tags, album="x", **kw):
    return {"id": id, "albumId": album, "genres": [{"name": t} for t in tags], **kw}


def genres(songs, groups=g.DEFAULT_GROUPS, unmatched="Other", types=None):
    return g.assign(songs, groups, unmatched, types)


class MatchTests(unittest.TestCase):
    def test_combination_becomes_one_group(self):
        self.assertEqual(genres([song("a", "dance", "electronic", "rave")]),
                         {"a": "Electronic"})

    def test_whole_words_ignoring_case_and_punctuation(self):
        out = genres([song("a", "Alternative Rock", album="1"),
                      song("b", "dancefloor drum & bass", album="2"),
                      song("c", "Lo-Fi Hip Hop", album="3"),
                      song("d", "electropop", album="4"),
                      song("e", "trip hop", album="5")])
        self.assertEqual(out, {"a": "Rock", "b": "DnB", "c": "Lofi / Chill",
                               "d": "Pop", "e": "Lofi / Chill"})

    def test_lo_fi_alone_is_a_sound_not_the_genre(self):
        # 2026-11-10: San Holo and Message to Bears were in Lofi / Chill by "lo-fi"
        out = genres([song("a", "electronic", "future bass", "lo-fi", album="1"),
                      song("b", "folk", "indie rock", "lo-fi", album="2"),
                      song("c", "lo-fi hip hop", album="3")])
        self.assertEqual(out, {"a": "Electronic", "b": "Indie Rock", "c": "Lofi / Chill"})

    def test_an_artist_named_in_a_group_beats_every_tag(self):
        groups = [{"name": "Lofi / Chill", "match": ["chillout", "trip hop"]},
                  {"name": "Indie Pop", "match": ["artist:Gorillaz", "art pop"]},
                  {"name": "Electronic", "match": ["artist:Snail's House", "electronic"]}]
        songs = [song("a", "downtempo", "chillout", album="1", artist="Snail\u2019s House"),
                 song("b", "trip hop", album="2", artist="Gorillaz"),
                 song("c", "trip hop", album="3", artist="Emancipator"),
                 song("d", "chillout", album="4", artist="Air Supply"),
                 song("e", "chillout", album="5", artist="X",
                      displayAlbumArtist="Gorillaz \u2022 Someone")]
        out = g.assign(songs, groups, "")
        self.assertEqual(out, {"a": "Electronic", "b": "Indie Pop", "c": "Lofi / Chill",
                               "d": "Lofi / Chill", "e": "Indie Pop"})

    def test_not_keeps_a_group_from_songs_with_that_tag(self):
        # 2026-11-11: Ambient for Air's "Moon Safari", not for "ambient, deep house" tracks
        groups = [{"name": "Ambient", "match": ["ambient", "not:house"]},
                  {"name": "Electronic", "match": ["electronic", "house"]},
                  {"name": "Indie Pop", "match": ["artist:Someone"]}]
        out = g.assign([song("a", "downtempo", "electronic", "ambient", album="1"),
                        song("b", "ambient", "deep house", "electronic", album="2"),
                        song("c", "ambient", "electro house", album="3", artist="Someone"),
                        song("d", "ambient", "house", album="4")], groups, "")
        self.assertEqual(out, {"a": "Ambient", "b": "Electronic", "c": "Indie Pop",
                               "d": "Electronic"})

    def test_an_untagged_album_takes_its_artists_genre(self):
        # 2026-11-11: 50 albums with no tags sat in "Other"
        out = genres([song("a", "electronic", album="1", artist="Coyote Kisses"),
                      song("b", album="2", artist="Coyote Kisses"),
                      song("c", album="3", artist="coyote kisses."),
                      song("d", album="4", artist="Nobody Tagged"),
                      song("e", "electronic", album="5", artist="X"),
                      song("f", album="5", artist="X")])           # album partly tagged
        self.assertEqual(out, {"a": "Electronic", "b": "Electronic", "c": "Electronic",
                               "d": "Other", "e": "Electronic", "f": "Electronic"})

    def test_not_also_stops_the_groups_own_artist_rule(self):
        # 2026-11-11: Snail's House's kawaii future bass albums in their own group, his
        # chillout albums in Electronic (never Lofi / Chill)
        groups = [{"name": "Lofi / Chill", "match": ["chillout"]},
                  {"name": "Kawaii Future Bass", "match": ["kawaii future bass"]},
                  {"name": "Electronic", "match": ["artist:Snail's House",
                                                   "not:kawaii future bass", "electronic"]}]
        out = g.assign([song("a", "kawaii future bass", "future funk", album="1",
                             artist="Snail\u2019s House"),
                        song("b", "chillout", "downtempo", album="2", artist="Snail\u2019s House"),
                        song("c", "chillout", album="3", artist="Blackmill")], groups, "")
        self.assertEqual(out, {"a": "Kawaii Future Bass", "b": "Electronic", "c": "Lofi / Chill"})

    def test_list_order_is_priority_not_tag_order(self):
        # Indie Rock is listed before Electronic, whatever order the tags are in.
        self.assertEqual(genres([song("a", "electronic", "indie rock")]), {"a": "Indie Rock"})

    def test_counts_never_change_the_choice(self):
        # Many more Dance-only albums don't pull a dance+electronic album away.
        groups = [{"name": "Electronic", "match": ["electronic"]},
                  {"name": "Dance", "match": ["dance"]}]
        songs = [song("both", "dance", "electronic", album="b")] + \
            [song(f"d{i}", "dance", album=f"d{i}") for i in range(30)]
        self.assertEqual(genres(songs, groups)["both"], "Electronic")

    def test_album_takes_its_majority_group(self):
        out = genres([song("a", "hip hop"), song("b", "boom bap"), song("c", "chillout")])
        self.assertEqual(set(out.values()), {"Hip-Hop"})

    def test_album_tie_goes_to_the_group_listed_first(self):
        out = genres([song("a", "electronic"), song("b", "rock")])
        self.assertEqual(set(out.values()), {"Rock"})

    def test_matched_group_beats_unmatched_songs_on_an_album(self):
        out = genres([song("a"), song("b", "polka"), song("c", "chiptune")])
        self.assertEqual(set(out.values()), {"Chiptune"})

    def test_unmatched_goes_to_other_or_first_tag(self):
        self.assertEqual(genres([song("a", "jazz fusion")], groups=[]), {"a": "Other"})
        self.assertEqual(genres([song("a", "drum and bass", "x")], groups=[], unmatched=""),
                         {"a": "Drum and Bass"})
        self.assertEqual(genres([song("a")], groups=[], unmatched=""), {"a": "Other"})

    def test_album_name_match_beats_later_groups(self):
        # A Chillhop Essentials volume tagged only hip hop still goes to Chill.
        vol = dict(song("a", "hip hop", "boom bap"), album="Chillhop Essentials: Winter 2025")
        self.assertEqual(genres([vol]), {"a": "Lofi / Chill"})
        other = dict(song("b", "hip hop"), album="Chillhopper")             # whole words
        self.assertEqual(genres([other]), {"b": "Hip-Hop"})

    def test_unmatched_takes_the_tag_most_albums_carry(self):
        songs = [song("a", "zydeco", "polka", album="1"),
                 song("b", "polka", album="2"), song("c", "polka", album="3"),
                 song("d", "zydeco", album="4")]
        out = genres(songs, groups=[], unmatched="")
        self.assertEqual(out["a"], "Polka")                    # 3 albums beat 2
        self.assertEqual(out["d"], "Zydeco")

    def test_list_comes_before_counting(self):
        songs = [song("a", "polka", "rock", album="1")] + \
            [song(f"p{i}", "polka", album=f"p{i}") for i in range(10)]
        self.assertEqual(genres(songs, unmatched="")["a"], "Rock")

    def test_default_list_has_general_categories(self):
        out = genres([song("a", "Baroque", album="1"), song("b", "Bluegrass", album="2"),
                      song("c", "Contemporary R&B", album="3"),
                      song("d", "rhythm and blues", album="4"),
                      song("e", "post-hardcore", album="5"),
                      song("f", "dubstep", album="6")], unmatched="")
        self.assertEqual(out, {"a": "Classical", "b": "Country", "c": "R&B / Soul",
                               "d": "R&B / Soul", "e": "Rock", "f": "Electronic"})

    def test_release_type_match(self):
        out = genres([song("a", "edm", album="live1"), song("b", "edm", album="lp")],
                     types={"live1": ["album", "live"], "lp": ["album"]})
        self.assertEqual(out, {"a": "Live Recordings", "b": "Electronic"})

    def test_single_genre_field_with_several_values(self):
        self.assertEqual(g.song_tags({"genre": "dance;electronic"}), ["dance", "electronic"])
        self.assertEqual(g.song_tags({"genre": "rock", "genres": []}), ["rock"])

    def test_release_types_list_or_string(self):
        self.assertEqual(g.release_types({"releaseTypes": "album;Compilation"}),
                         ["album", "compilation"])
        self.assertEqual(g.release_types({"releaseTypes": ["Live"]}), ["live"])
        self.assertEqual(g.release_types({}), [])

    def test_release_types_spelled_out_by_navidrome(self):
        # as Navidrome sent them for three real albums, 2026-11-06
        for sent, want in (
                (["a;l;b;u;m;;; ;c;o;m;p;i;l;a;t;i;o;n", " ", ";", "a", "b", "c"],
                 ["album", "compilation"]),
                (["a;l;b;u;m", "album"], ["album"]),
                ([" ", ";", "e", "i", "m", "p", "r", "x"], [])):
            got = g.release_types({"releaseTypes": sent})
            self.assertEqual(got, want)
            self.assertTrue(g.plausible_types(got))    # kept, not looked up every run


if __name__ == "__main__":
    unittest.main()
