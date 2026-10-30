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
