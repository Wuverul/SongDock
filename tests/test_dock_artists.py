#!/usr/bin/env python3
"""Tests for dock_artists.py: one listing per artist."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dock_artists as a  # noqa: E402


class SplitTests(unittest.TestCase):
    def test_guests_go(self):
        for credit, want in (
                ("Daft Punk feat. Panda Bear", ("Daft Punk", "Panda Bear")),
                ("Aso ft. iomoo", ("Aso", "iomoo")),
                ("Foo Featuring Bar", ("Foo", "Bar")),
                ("Foo (feat. Bar)", ("Foo", "Bar")),
                ("usedcvnt w/ Akiba", ("usedcvnt", "Akiba")),
                ("Purity Ring .feat Young Magic", ("Purity Ring", "Young Magic")),
                ("Daft Punk feat. Pharrell Williams & Nile Rodgers",
                 ("Daft Punk", "Pharrell Williams & Nile Rodgers"))):
            self.assertEqual(a.split_credit(credit), want)

    def test_first_of_a_comma_list_is_main(self):
        self.assertEqual(a.split_credit("Afroham, Pleij"), ("Afroham", "Pleij"))
        self.assertEqual(a.split_credit("San Holo, Big Gigantic & Evalyn"),
                         ("San Holo", "Big Gigantic, Evalyn"))
        self.assertEqual(a.split_credit("Afroham, No Spirit ft. BAKERMAN"),
                         ("Afroham", "No Spirit, BAKERMAN"))
        self.assertEqual(a.split_credit("LEASHES • Someone;Other"),
                         ("LEASHES", "Someone, Other"))

    def test_first_of_partners_is_main(self):
        self.assertEqual(a.split_credit("Porter Robinson & Madeon"),
                         ("Porter Robinson", "Madeon"))
        self.assertEqual(a.split_credit("Pentagon / Porter Robinson"),
                         ("Pentagon", "Porter Robinson"))
        self.assertEqual(a.split_credit("Bao & Venuz Beats feat. Justin Wyatt"),
                         ("Bao", "Venuz Beats, Justin Wyatt"))
        for credit in ("Flitz&Suppe", "Psalm//Trees"):
            self.assertEqual(a.split_credit(credit), (credit, None))

    def test_names_that_only_look_like_credits(self):
        for credit in ("Ft. Lauderdale", "Left Boy", "Without Words", "Feat"):
            self.assertEqual(a.split_credit(credit), (credit, None))

    def test_real_names_with_commas(self):
        self.assertEqual(a.split_credit("chromo, the dreamer"), ("chromo, the dreamer", None))
        self.assertEqual(a.split_credit("Chromo, The Dreamer ft. X"), ("Chromo, The Dreamer", "X"))
        self.assertEqual(a.split_credit("chromo, the dreamer, Aso"),
                         ("chromo, the dreamer", "Aso"))
        self.assertEqual(a.split_credit("Mine, Yours", keep=("mine, yours",)), ("Mine, Yours", None))


class AlbumArtistTests(unittest.TestCase):
    def test_a_song_takes_its_albums_artist(self):
        for credit, want in (("No Mana & Chipzel", ("No Mana", "Chipzel")),
                             ("James Egbert & No Mana", ("No Mana", "James Egbert")),
                             ("A & No Mana / B feat. C", ("No Mana", "A, B, C")),
                             ("Caribou", ("No Mana", "Caribou")),
                             ("No Mana", ("No Mana", None))):
            self.assertEqual(a.split_credit(credit, album_main="No Mana"), want)

    def tidied(self, **song):
        records = [{"id": "1", "title": "T", **song}, {"id": "2", "artist": "Solo", "title": "S"}]
        a.tidy(records)
        return records[0]["artist"], records[0]["title"]

    def test_tidy_uses_the_first_album_artist(self):
        self.assertEqual(self.tidied(artist="Caribou", displayAlbumArtist="Solo & Madeon"),
                         ("Solo", "T (feat. Caribou)"))

    def test_a_remixer_named_in_the_title_isnt_credited_twice(self):
        self.assertEqual(self.tidied(artist="Caribou", title="Song (Caribou Remix)",
                                     displayAlbumArtist="Solo"),
                         ("Solo", "Song (Caribou Remix)"))

    def test_compilations_keep_the_tracks_own_artist(self):
        for album in ("Various Artists", "Chillhop Music \u2022 Various Artists"):
            self.assertEqual(self.tidied(artist="Solo feat. X", displayAlbumArtist=album),
                             ("Solo", "T (feat. X)"))

    def test_a_label_album_artist_is_an_artist(self):
        self.assertEqual(self.tidied(artist="Solo", displayAlbumArtist="Chillhop Music"),
                         ("Chillhop Music", "T (feat. Solo)"))

    def test_a_listed_compilation_gets_one_artist(self):
        for album_artist in ("Various Artists", "Label", "Label \u2022 Various Artists"):
            songs = [{"id": "1", "artist": "Aso, Bao", "title": "T", "album": "Mix Series: Fall",
                      "displayAlbumArtist": album_artist},
                     {"id": "2", "artist": "Aso", "title": "U", "album": "Aso's Album",
                      "displayAlbumArtist": "Aso"}]
            a.tidy(songs, compilation_albums=["mix series = Mix Releases"])
            self.assertEqual((songs[0]["artist"], songs[0]["title"]),
                             ("Mix Releases", "T (feat. Aso, Bao)"))
            self.assertEqual(songs[1]["artist"], "Aso")

    def test_a_compilation_without_a_name_lists_under_its_words(self):
        self.assertEqual(a.compilations(["Now That's Music", "Mix = Mixes"]),
                         {"now that's music": "Now That's Music", "mix": "Mixes"})

    def test_a_band_with_and_in_its_name_survives_on_its_album(self):
        self.assertEqual(self.tidied(artist="Above & Beyond",
                                     displayAlbumArtist="Above & Beyond"),
                         ("Above & Beyond", "T"))


class CommaEvidenceTests(unittest.TestCase):
    def tidied(self, *songs):
        records = [{"id": str(i), "title": "T", **s} for i, s in enumerate(songs)]
        unproven = set()
        a.tidy(records, unproven=unproven)
        return [r["artist"] for r in records], unproven

    def test_navidrome_listing_the_names_separately_proves_it(self):
        artists = [{"name": "DLJ"}, {"name": "9ICK"}]
        self.assertEqual(self.tidied({"artist": "DLJ, 9ICK", "artists": artists}),
                         (["DLJ"], set()))
        self.assertEqual(self.tidied({"artist": "A, B", "artists": [{"name": "A;B"}]}),
                         (["A"], set()))

    def test_either_name_alone_in_the_library_proves_it(self):
        self.assertEqual(self.tidied({"artist": "A, B"}, {"artist": "A"})[0], ["A", "A"])
        self.assertEqual(self.tidied({"artist": "A, B"}, {"artist": "B"})[0], ["A", "B"])
        self.assertEqual(self.tidied({"artist": "a, b"}, {"artist": "A."})[0][0], "A.")

    def test_no_evidence_keeps_the_name_and_reports_it(self):
        one = {"artist": "Hello, Goodbye", "artists": [{"name": "Hello, Goodbye"}]}
        self.assertEqual(self.tidied(one), (["Hello, Goodbye"], {"Hello, Goodbye"}))

    def test_a_guest_in_artists_is_not_evidence(self):
        one = {"artist": "Hello, Goodbye ft. Guest",
               "artists": [{"name": "Hello, Goodbye"}, {"name": "Guest"}]}
        self.assertEqual(self.tidied(one), (["Hello, Goodbye"], {"Hello, Goodbye"}))

    def test_and_needs_evidence_too(self):
        self.assertEqual(self.tidied({"artist": "Hall & Oates"}),
                         (["Hall & Oates"], {"Hall & Oates"}))
        self.assertEqual(self.tidied({"artist": "A & B"}, {"artist": "A"})[0], ["A", "A"])

    def test_known_names_are_not_reported(self):
        self.assertEqual(self.tidied({"artist": "chromo, the dreamer"}),
                         (["chromo, the dreamer"], set()))


class TidyTests(unittest.TestCase):
    def songs(self, *artists):
        return [{"id": str(i), "artist": n, "title": f"T{i}"} for i, n in enumerate(artists)]

    def test_groups_an_artists_work_and_credits_guests(self):
        songs = self.songs("Daft Punk", "Daft Punk feat. Panda Bear")
        self.assertEqual(a.tidy(songs), 1)
        self.assertEqual(songs[1]["artist"], "Daft Punk")
        self.assertEqual(songs[1]["title"], "T1 (feat. Panda Bear)")
        self.assertEqual(songs[0]["title"], "T0")

    def test_most_common_spelling_wins(self):
        songs = self.songs("chromonicci", "chromonicci", "chromonicci.", "Purity Ring .",
                           "Purity Ring", "Purity Ring", "leavv", "Leavv")
        a.tidy(songs)
        self.assertEqual({s["artist"] for s in songs}, {"chromonicci", "Purity Ring", "Leavv"})

    def test_symbols_only_name_survives(self):
        songs = self.songs("♥", "♥")
        a.tidy(songs)
        self.assertEqual(songs[0]["artist"], "♥")

    def test_title_already_crediting_is_left_alone(self):
        self.assertEqual(a.title_with_guests("Song (ft. Bar)", "Bar"), "Song (ft. Bar)")
        self.assertEqual(a.title_with_guests("Song", None), "Song")

    def test_second_pass_changes_nothing(self):
        songs = self.songs("A feat. B", "A")
        a.tidy(songs)
        self.assertEqual(a.tidy(songs), 0)

    def test_no_artist_is_left_alone(self):
        songs = [{"id": "1", "title": "T"}]
        self.assertEqual(a.tidy(songs), 0)
        self.assertNotIn("artist", songs[0])

    def test_listed_names_stay_whole_with_evidence_against_them(self):
        def run(extra):
            songs = [{"id": "1", "artist": "Simon & Garfunkel feat. Z", "title": "V",
                      "album": "Mix", "albumArtist": "Various Artists"},
                     {"id": "2", "artist": "Simon & Garfunkel", "title": "W",
                      "album": "Duo", "albumArtist": "Simon & Garfunkel"},
                     {"id": "3", "artist": "Simon", "title": "X", "album": "Solo",
                      "albumArtist": "Simon"}]
            a.tidy(songs, extra)
            return [(t["artist"], t["title"]) for t in songs[:2]]
        self.assertEqual(run(())[0][0], "Simon")
        for extra in (["Simon & Garfunkel"], ["simon & garfunkel"]):
            self.assertEqual(run(extra), [("Simon & Garfunkel", "V (feat. Z)"),
                                          ("Simon & Garfunkel", "W")])

    def test_extra_names(self):
        songs = self.songs("Mine, Yours")
        a.tidy(songs, ("Mine, Yours",))
        self.assertEqual(songs[0]["artist"], "Mine, Yours")


if __name__ == "__main__":
    unittest.main()
