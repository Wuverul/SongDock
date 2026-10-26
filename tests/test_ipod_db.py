"""Tests for the streaming iTunesDB (ipod_db.py; needs pygpod + mutagen).

The main check: the same changes made through pygpod and through
StreamDatabase must give databases that read back the same in pygpod -
tracks byte for byte, playlists, browse lists, album and artist lists,
album art and the signature.

Run: ~/musicdock/venv/bin/python -m unittest discover -s tests -v
"""

import logging
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import ipod_fixtures as fx  # noqa: E402
import ipod_db  # noqa: E402

logging.disable(logging.CRITICAL)
NOW = 3_900_000_000          # a fixed Mac-epoch time, so both writers stamp alike


def itunesdb(root):
    return os.path.join(root, "iPod_Control", "iTunes", "iTunesDB")


def records(path):
    """What pygpod reads back from a database file."""
    import pygpod
    db = pygpod.Database.from_file(path)
    out = {"tracks": [bytes(t.record.raw_bytes) for t in db.tracks],
           "names": [(t.title, t.artist, t.album, t.genre, t.comment) for t in db.tracks]}
    for mhsd in db._root.children:
        kind = mhsd.fields["mhsd_type"]
        if kind in (2, 3):
            lists = []
            for mhyp in mhsd.children[0].children:
                sort = [bytes(c.raw_bytes) for c in mhyp.children if c.magic == b"mhod"
                        and c.fields.get("mhod_type") in (52, 53)]
                lists.append((mhyp.get_mhod(1), mhyp.fields["playlist_type"],
                              mhyp.fields["playlist_id"],
                              [c.fields["track_id"] for c in mhyp.children if c.magic == b"mhip"],
                              sort))
            out[f"playlists{kind}"] = lists
        elif kind == 4:
            out["albums"] = [(a.fields.get("album_id"), a.get_mhod(200), a.get_mhod(201))
                             for a in mhsd.children[0].children]
        elif kind == 8:
            out["artists"] = [(a.fields.get("artist_id"), a.get_mhod(300))
                              for a in mhsd.children[0].children]
    return out


def install_sort_order():
    """Make pygpod's browse lists break ties by album, disc and track, as
    ipod_db does (the dock patched pygpod this way before ipod_db), so the
    two can be compared. pygpod sorts with a stable sort on one field, so
    tracks handed over already in the full order come out right."""
    from pygpod.model import database as pdb
    original = pdb.Database._build_sort_pair
    if getattr(original, "_musicdock", False):
        return

    def build_sort_pair(self, sort_key, field_name, tracks_meta):
        ids = [c.fields.get("track_id") for c in next(
            mhyp for mhsd in self._root.children if mhsd.fields.get("mhsd_type") == 2
            for mhyp in mhsd.children[0].children if mhyp.fields.get("playlist_type") == 1
        ).children if c.magic == b"mhip"]

        def numbers(meta):
            t = self._track_lookup.get(ids[meta["index"]])
            return (t.cd_number or 0, t.track_number or 0) if t else (0, 0)

        def key(meta):
            text = ipod_db._sort_text
            album, artist, title = text(meta["album"]), text(meta["artist"]), text(meta["title"])
            if sort_key == 4:
                return (album, *numbers(meta), title)
            if sort_key == 5:
                return (artist, album, *numbers(meta), title)
            if sort_key == 7:
                return (text(meta["genre"]), artist, album, *numbers(meta), title)
            return (text(meta[field_name]), title)
        return original(self, sort_key, field_name, sorted(tracks_meta, key=key))

    build_sort_pair._musicdock = True
    pdb.Database._build_sort_pair = build_sort_pair


@unittest.skipUnless(fx.have_ipod_tools(), "needs pygpod + mutagen (the Pi venv)")
class StreamDatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        songs = [dict(title=f"Song {i}", artist="The Band" if i < 3 else "Artist",
                      album="First" if i < 3 else "Second", track=3 - i if i < 3 else i,
                      seconds=1) for i in range(6)]
        self.base = os.path.join(self.tmp, "base")
        fx.make_ipod(self.base, songs, self.src)
        import pygpod
        db = pygpod.Database(self.base)
        mix = db.create_playlist("Mix")
        for t in db.tracks[:3]:
            db.add_track_to_playlist(mix, t)
        db.save()
        patcher = mock.patch("pygpod.model.database.now_mac", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy(self, name):
        root = os.path.join(self.tmp, name)
        shutil.copytree(self.base, root)
        return root

    def config(self):
        from pygpod.model.config import DatabaseConfig
        return DatabaseConfig(random_seed=7)

    def stream(self, root):
        work = os.path.join(self.tmp, "work-" + os.path.basename(root))
        os.makedirs(work, exist_ok=True)
        shutil.copy(itunesdb(root), os.path.join(work, "iTunesDB"))
        return ipod_db.StreamDatabase(root, os.path.join(work, "iTunesDB"), work,
                                      config=self.config())

    def new_song(self, name, album, cover):
        return fx.make_mp3(os.path.join(self.src, name + ".mp3"), name, artist="New",
                           album=album, seconds=1, cover=cover)

    # ------------------------------------------------------------ tests ----
    def test_index_matches_what_pygpod_reads(self):
        import pygpod
        theirs = pygpod.Database.from_file(itunesdb(self.base))
        ours = ipod_db.StreamDatabase.from_file(itunesdb(self.base))
        self.assertEqual(
            [(t.track_id, t.dbid, t.title, t.artist, t.album, t.ipod_path, t.track_number,
              t.duration) for t in ours.tracks],
            [(t.track_id, t.dbid, t.title, t.artist, t.album, t.ipod_path, t.track_number,
              t.duration) for t in theirs.tracks])
        self.assertEqual([(p.name, p.is_master) for p in ours.playlists],
                         [(p.name, p.is_master) for p in theirs.playlists])
        mix = next(p for p in ours.playlists if p.name == "Mix")
        self.assertEqual([t.dbid for t in mix.tracks], [t.dbid for t in theirs.tracks[:3]])
        self.assertEqual(ours.master_playlist.name, theirs.master_playlist.name)

    def test_same_changes_give_the_same_database_as_pygpod(self):
        import pygpod
        install_sort_order()                  # pygpod's browse lists with disc/track ties
        a, b = self.copy("a"), self.copy("b")
        songs = [self.new_song("Fresh", "Third", (200, 30, 30)),
                 self.new_song("Later", "Third", (20, 30, 200))]

        theirs = pygpod.Database(a, self.config())
        ours = self.stream(b)
        for db in (theirs, ours):
            tracks = list(db.tracks)
            db.remove_track(tracks[1])
            tracks[3].album = "Renamed"
            tracks[0].genre = "Pop"
            tracks[4].comment = "navidrome:abc"
            added = [db.add_track(path, album="Third") for path in songs]
            fav = db.create_playlist("Fav")
            for t in (added[1], tracks[0], added[0]):
                db.add_track_to_playlist(fav, t)
            db.save()

        mine, pygpods = records(itunesdb(b)), records(itunesdb(a))
        self.assertEqual(mine["names"], pygpods["names"])
        self.assertEqual(mine["tracks"], pygpods["tracks"])
        for kind in ("playlists2", "playlists3"):
            # pygpod leaves the master's entries numbered as they were;
            # compare names, ids and entries (by track), and browse lists.
            self.assertEqual(mine[kind], pygpods[kind], kind)
        self.assertEqual(mine["albums"], pygpods["albums"])
        self.assertEqual(mine["artists"], pygpods["artists"])
        with open(os.path.join(a, "iPod_Control", "Artwork", "ArtworkDB"), "rb") as f1, \
                open(os.path.join(b, "iPod_Control", "Artwork", "ArtworkDB"), "rb") as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_signature_is_what_pygpod_computes(self):
        from pygpod.hash.hash58 import compute_hash58
        b = self.copy("b")
        ours = self.stream(b)
        ours.tracks[0].title = "Changed"
        ours.save()
        with open(itunesdb(b), "rb") as f:
            data = f.read()
        self.assertEqual(data, compute_hash58(data, fx.FAKE_GUID))
        self.assertNotEqual(data[0x58:0x6C], bytes(20))

    def test_saves_chain_and_the_player_gets_the_new_file_whole(self):
        import pygpod
        b = self.copy("b")
        ours = self.stream(b)
        added = ours.add_track(self.new_song("One", "Third", False), album="Third")
        ours.save()
        ours.remove_track(ours.tracks[0])
        added.title = "One (edit)"
        ours.save()
        self.assertFalse(os.path.exists(itunesdb(b) + ".musicdock-new"))
        theirs = pygpod.Database.from_file(itunesdb(b))
        self.assertEqual(len(theirs.tracks), 6)
        self.assertEqual(theirs.tracks[-1].title, "One (edit)")
        self.assertEqual([t.dbid for t in theirs.tracks], [t.dbid for t in ours.tracks])
        again = ipod_db.StreamDatabase.from_file(itunesdb(b))
        self.assertEqual([t.title for t in again.tracks], [t.title for t in theirs.tracks])

    def test_play_counts_merge_as_pygpod_merges_them(self):
        import pygpod
        from pygpod.db.playcounts import parse_play_counts
        a, b = self.copy("a"), self.copy("b")
        for root in (a, b):
            fx.write_play_counts(root, [(0, 0), (3, 3_800_000_000), (0, 0), (1, 3_800_000_100)])
        theirs = pygpod.Database(a)                      # merges and deletes the file
        theirs.save()
        with open(os.path.join(b, "iPod_Control", "iTunes", "Play Counts"), "rb") as f:
            entries = parse_play_counts(f.read())
        ours = self.stream(b)
        ours.merge_play_counts(entries)
        self.assertTrue(os.path.exists(os.path.join(b, "iPod_Control", "iTunes", "Play Counts")))
        ours.save()
        self.assertFalse(os.path.exists(os.path.join(b, "iPod_Control", "iTunes", "Play Counts")))
        self.assertEqual([t.play_count for t in ours.tracks], [0, 3, 0, 1, 0, 0])
        self.assertEqual(records(itunesdb(b))["tracks"], records(itunesdb(a))["tracks"])

    def test_play_files_stay_when_the_save_fails(self):
        from pygpod.db.playcounts import parse_play_counts
        b = self.copy("b")
        fx.write_play_counts(b, [(2, 3_800_000_000)])
        with open(os.path.join(b, "iPod_Control", "iTunes", "Play Counts"), "rb") as f:
            entries = parse_play_counts(f.read())
        ours = self.stream(b)
        ours.merge_play_counts(entries)
        with mock.patch("ipod_db.os.replace", side_effect=OSError("unplugged")):
            with self.assertRaises(OSError):
                ours.save()
        self.assertTrue(os.path.exists(os.path.join(b, "iPod_Control", "iTunes", "Play Counts")))

    def test_album_art_is_shared_and_kept_while_a_track_shows_it(self):
        b = self.copy("b")
        ours = self.stream(b)
        ours.share_art = True
        cover = (10, 200, 10)
        one = ours.add_track(self.new_song("A1", "Shared", cover), album="Shared")
        two = ours.add_track(self.new_song("A2", "Shared", cover), album="Shared")
        ours.save()
        self.assertTrue(one.has_artwork)
        self.assertEqual(one.mhii_link, two.mhii_link)
        image = one.mhii_link
        ours.remove_track(one)
        self.assertIn(image, ours.images())
        self.assertEqual(ours.images()[image].fields["song_id"], two.dbid)
        ours.remove_track(two)
        self.assertNotIn(image, ours.images())
        ours.save()

    def test_a_removed_track_leaves_the_playlists_it_was_in(self):
        b = self.copy("b")
        ours = self.stream(b)
        gone = ours.tracks[1]
        ours.remove_track(gone)
        ours.save()
        for kind in ("playlists2", "playlists3"):
            mix = next(p for p in records(itunesdb(b))[kind] if p[0] == "Mix")
            self.assertNotIn(gone.track_id, mix[3])
            self.assertEqual(len(mix[3]), 2)

    def test_deleted_and_rebuilt_playlists(self):
        b = self.copy("b")
        ours = self.stream(b)
        mix = next(p for p in ours.playlists if p.name == "Mix")
        ours.delete_playlist(mix)
        fav = ours.create_playlist("Fav")
        for t in reversed(ours.tracks):
            ours.add_track_to_playlist(fav, t)
        ours.save()
        out = records(itunesdb(b))
        for kind in ("playlists2", "playlists3"):
            names = [p[0] for p in out[kind]]
            self.assertNotIn("Mix", names)
            fav = next(p for p in out[kind] if p[0] == "Fav")
            self.assertEqual(fav[3], [t.track_id for t in reversed(ours.tracks)])

    def test_browse_lists_order_albums_by_disc_and_track(self):
        b = self.copy("b")
        ours = self.stream(b)
        ours.save()
        master = next(p for p in records(itunesdb(b))["playlists2"] if p[1] == 1)
        album_index = master[4][4]                           # sort key 4 (album), type 52
        count = struct.unpack_from("<I", album_index, 24 + 4)[0]
        order = struct.unpack_from(f"<{count}I", album_index, 24 + 48)
        titles = [ours.tracks[i].title for i in order]
        # "First" has tracks numbered 3, 2, 1 for Song 0, 1, 2.
        self.assertEqual(titles[:3], ["Song 2", "Song 1", "Song 0"])

    def test_a_database_missing_sections_is_tidied_by_pygpod_once(self):
        b = self.copy("b")
        path = itunesdb(b)
        with open(path, "rb") as f:
            data = bytearray(f.read())
        # Drop the artist list (type 8, last) as an iTunes-made file might.
        hl = struct.unpack_from("<I", data, 4)[0]
        pos, last = hl, None
        while pos < len(data):
            if struct.unpack_from("<I", data, pos + 12)[0] == 8:
                last = pos
            pos += struct.unpack_from("<I", data, pos + 8)[0]
        del data[last:]
        struct.pack_into("<I", data, 8, len(data))
        struct.pack_into("<I", data, 0x14, struct.unpack_from("<I", data, 0x14)[0] - 1)
        with open(path, "wb") as f:
            f.write(data)
        ours = self.stream(b)
        ours.save()
        self.assertTrue(records(itunesdb(b))["artists"])

    def test_read_only_copies_refuse_to_save(self):
        ours = ipod_db.StreamDatabase.from_file(itunesdb(self.base))
        with self.assertRaises(ipod_db.DatabaseError):
            ours.save()


if __name__ == "__main__":
    unittest.main()
