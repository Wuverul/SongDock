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
                         ("San Holo", "Big Gigantic & Evalyn"))
        self.assertEqual(a.split_credit("Afroham, No Spirit ft. BAKERMAN"),
                         ("Afroham", "No Spirit, BAKERMAN"))
        self.assertEqual(a.split_credit("LEASHES • Someone;Other"),
                         ("LEASHES", "Someone;Other"))

    def test_equal_partners_stay_together(self):
        for credit in ("Porter Robinson & Madeon", "Pentagon / Porter Robinson",
                       "Flitz&Suppe", "Psalm//Trees"):
            self.assertEqual(a.split_credit(credit), (credit, None))
        self.assertEqual(a.split_credit("Bao & Venuz Beats feat. Justin Wyatt"),
                         ("Bao & Venuz Beats", "Justin Wyatt"))

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
    def test_a_credit_naming_the_album_artist_files_under_it(self):
        for credit, want in (("No Mana & Chipzel", ("No Mana", "Chipzel")),
                             ("James Egbert & No Mana", ("No Mana", "James Egbert")),
                             ("Kero Kero Bonito / No Mana", ("No Mana", "Kero Kero Bonito")),
                             ("A & No Mana / B feat. C", ("No Mana", "A, B, C"))):
            self.assertEqual(a.split_credit(credit, album_artist="No Mana"), want)

    def test_a_release_under_both_names_makes_them_equal(self):
        self.assertEqual(a.split_credit("Porter Robinson & Madeon",
                                        album_artist="Porter Robinson", duo=True),
                         ("Porter Robinson & Madeon", None))

    def test_a_credit_without_the_album_artist_stays(self):
        self.assertEqual(a.split_credit("EDDIE & Bad Disk", album_artist="No Mana"),
                         ("EDDIE & Bad Disk", None))
        self.assertEqual(a.split_credit("Bao & Venuz", album_artist="Various Artists"),
                         ("Bao & Venuz", None))

    def test_tidy_tells_the_two_apart(self):
        songs = [{"id": "1", "artist": "PR & Madeon", "title": "A", "displayAlbumArtist": "PR"},
                 {"id": "2", "artist": "PR & Madeon", "title": "B",
                  "displayAlbumArtist": "PR & Madeon"},
                 {"id": "3", "artist": "PR & Zo", "title": "C", "displayAlbumArtist": "PR"}]
        a.tidy(songs)
        self.assertEqual([s["artist"] for s in songs], ["PR & Madeon", "PR & Madeon", "PR"])
        self.assertEqual(songs[2]["title"], "C (feat. Zo)")


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

    def test_extra_names(self):
        songs = self.songs("Mine, Yours")
        a.tidy(songs, ("Mine, Yours",))
        self.assertEqual(songs[0]["artist"], "Mine, Yours")


if __name__ == "__main__":
    unittest.main()
