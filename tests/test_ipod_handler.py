"""Tests for the stock iPod handler (needs pygpod + mutagen; the Pi venv).

Run: ~/musicdock/venv/bin/python -m unittest discover -s tests -v
"""

import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import ipod_fixtures as fx  # noqa: E402
import ipod_handler as h  # noqa: E402
from dock_record import Record  # noqa: E402

logging.disable(logging.CRITICAL)
G = fx.FAKE_GUID


@unittest.skipUnless(fx.have_ipod_tools(), "needs pygpod + mutagen (the Pi venv)")
class HandlerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.mount = os.path.join(self.tmp, "ipod")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(os.path.join(self.cache, "tracks"))
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        fx.make_ipod(self.mount, [], src)                  # empty, signed-DB iPod
        self.rec = Record(os.path.join(self.tmp, "dock.db"))
        self.addCleanup(self.rec.close)

    # ------------------------------------------------------------ helpers ----
    def song(self, nid, title, album="Album", track=1, cover=False):
        path = os.path.join(self.cache, "tracks", f"{nid}.mp3")
        if not os.path.exists(path):
            fx.make_mp3(path, title, album=album, track=track, seconds=2, cover=cover)
        return {"id": nid, "file": f"tracks/{nid}.mp3", "title": title,
                "artist": "Artist", "album": album, "track": track,
                "duration": 2, "encoding": "mp3 v0"}

    def manifest(self, songs, playlists=()):
        return {"tracks": songs, "playlists": [
            {"target": t, "name": n, "song_ids": ids} for t, n, ids in playlists]}

    def db(self):
        import pygpod
        return pygpod.Database(self.mount)

    def apply(self, manifest, **kw):
        db = self.db()
        report = h.reconcile(db, self.rec, G, self.mount, manifest=manifest)
        return h.apply_plan(db, self.rec, G, self.mount, manifest, self.cache,
                            dirty=report["dirty"], **kw)

    def files(self):
        return h.music_files(self.mount)

    # -------------------------------------------------------------- tests ----
    def test_adds_tracks_and_playlists_then_is_idempotent(self):
        a, b, c = self.song("n1", "One"), self.song("n2", "Two"), self.song("n3", "Three")
        m = self.manifest([a, b, c], [("starred", "Favorites", ["n1", "n2"]),
                                      ("playlist:Mel Test", "Mel Test", ["n3", "n1"])])
        summary = self.apply(m)
        self.assertEqual((summary["added"], summary["removed"]), (3, 0))
        db = self.db()
        self.assertEqual(sorted(t.title for t in db.tracks), ["One", "Three", "Two"])
        self.assertEqual({h.navidrome_id_from(t) for t in db.tracks}, {"n1", "n2", "n3"})
        pls = {p.name: [t.title for t in p.tracks] for p in db.playlists if not p.is_master}
        self.assertEqual(pls, {"Favorites": ["One", "Two"], "Mel Test": ["Three", "One"]})
        self.assertEqual(len(self.rec.tracks(G)), 3)
        again = self.apply(m)
        self.assertEqual((again["added"], again["removed"], again["playlists_changed"]),
                         (0, 0, 0))
        self.assertEqual(len(self.files()), 3)

    def test_removed_track_and_its_file_are_gone(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a, b]))
        summary = self.apply(self.manifest([a]))
        self.assertEqual(summary["removed"], 1)
        self.assertEqual([t.title for t in self.db().tracks], ["One"])
        self.assertEqual(len(self.files()), 1)                   # no orphan left
        self.assertEqual(self.rec.tracks(G, "removed")[0]["note"],
                         "no longer in any synced playlist")

    def test_targeted_but_uncached_track_stays_on_player(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a, b]))
        m = self.manifest([a])
        m["skipped"] = [{"id": "n2", "title": "Two", "reason": "cache full", "error": False}]
        summary = self.apply(m)                    # n2 still wanted, just not cached
        self.assertEqual(summary["removed"], 0)
        self.assertEqual(sorted(t.title for t in self.db().tracks), ["One", "Two"])

    def test_crash_mid_sync_resumes_without_duplicates(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(5)]
        real_save, calls = h.checked_save, []

        def crash_on_second_save(db, guid):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("power cut")
            real_save(db, guid)
        with self.assertRaises(RuntimeError):
            self.apply(self.manifest(songs), batch_size=2, save=crash_on_second_save)
        self.assertEqual(len(self.db().tracks), 2)              # first batch saved
        summary = self.apply(self.manifest(songs), batch_size=2)
        self.assertEqual(summary["added"], 3)
        self.assertEqual(sorted(t.title for t in self.db().tracks),
                         [f"Song {i}" for i in range(5)])
        self.assertEqual(len(self.files()), 5)                  # orphans cleaned up

    def test_tracks_saved_but_not_recorded_are_adopted(self):
        a = self.song("n1", "One")
        with mock.patch.object(self.rec, "add_track"):          # record write "lost"
            self.apply(self.manifest([a]))
        db = self.db()
        report = h.reconcile(db, self.rec, G, self.mount)
        self.assertEqual(report["adopted"], 1)
        self.assertEqual(h.apply_plan(db, self.rec, G, self.mount,
                                      self.manifest([a]), self.cache)["added"], 0)

    def test_reset_player_is_detected_and_rebuilt(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a, b]))
        shutil.rmtree(self.mount)                               # factory restore
        fx.make_ipod(self.mount, [], os.path.join(self.tmp, "src"))
        db = self.db()
        report = h.reconcile(db, self.rec, G, self.mount)
        self.assertTrue(report["reset"])
        summary = h.apply_plan(db, self.rec, G, self.mount, self.manifest([a, b]), self.cache)
        self.assertEqual(summary["added"], 2)

    def test_foreign_tracks_left_alone(self):
        import pygpod
        db = self.db()
        db.add_track(fx.make_mp3(os.path.join(self.tmp, "mine.mp3"), "Not the dock's"))
        db.save()
        report = h.reconcile(self.db(), self.rec, G, self.mount)
        self.assertEqual(report["foreign"], 1)
        self.apply(self.manifest([self.song("n1", "One")]))
        self.assertIn("Not the dock's", [t.title for t in pygpod.Database(self.mount).tracks])

    def add_by_hand(self, title, artist="Artist", seconds=2, album="Album"):
        db = self.db()
        db.add_track(fx.make_mp3(os.path.join(self.tmp, f"{title}.mp3"), title,
                                 artist=artist, album=album, seconds=seconds))
        db.save()

    def test_hand_copied_matching_track_adopted_not_duplicated(self):
        self.add_by_hand("BLOOM", album="CURRENTS")              # like the Mac test
        a = self.song("n1", "BLOOM")
        self.apply(self.manifest([a], [("starred", "Favorites", ["n1"])]))
        db = self.db()
        self.assertEqual([t.title for t in db.tracks], ["BLOOM"])     # no duplicate
        self.assertEqual(h.navidrome_id_from(db.tracks[0]), "n1")     # saved
        favs = [p for p in db.playlists if p.name == "Favorites"][0]
        self.assertEqual([t.title for t in favs.tracks], ["BLOOM"])

    def test_hand_copied_old_format_is_replaced(self):
        # An old AAC-era copy (here: a different file type) of a track now
        # cached as MP3 is adopted, then replaced by the current file.
        db = self.db()
        src = fx.make_mp3(os.path.join(self.tmp, "old.mp3"), "BLOOM", seconds=2)
        m4a = os.path.join(self.tmp, "old.m4a")
        os.rename(src, m4a)
        try:
            db.add_track(m4a)
        except Exception:
            self.skipTest("pygpod won't add a fake .m4a")
        db.save()
        a = self.song("n1", "BLOOM")
        summary = self.apply(self.manifest([a]))
        self.assertEqual((summary["replaced"], summary["added"]), (1, 1))
        tracks = self.db().tracks
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].ipod_path.endswith(".mp3"))

    def test_ambiguous_match_not_adopted(self):
        self.add_by_hand("Intro")
        a, b = self.song("n1", "Intro", album="A"), self.song("n2", "Intro", album="B")
        report = h.reconcile(self.db(), self.rec, G, self.mount,
                             manifest=self.manifest([a, b]))
        self.assertEqual((report["adopted"], report["foreign"]), (0, 1))

    def test_length_mismatch_not_adopted(self):
        self.add_by_hand("BLOOM", seconds=9)
        report = h.reconcile(self.db(), self.rec, G, self.mount,
                             manifest=self.manifest([self.song("n1", "BLOOM")]))
        self.assertEqual(report["foreign"], 1)

    def test_navidrome_album_name_used(self):
        a = dict(self.song("n1", "Salt in the Wounds", album="Inertia"),
                 album="Inertia (deluxe digital)")              # file tag differs
        self.apply(self.manifest([a]))
        self.assertEqual(self.db().tracks[0].album, "Inertia (deluxe digital)")

    def untagged(self, nid, title):
        path = os.path.join(self.cache, "tracks", f"{nid}.mp3")
        with open(path, "wb") as f:
            f.write(fx.FRAME * int(2 * fx.FRAMES_PER_SECOND))
        return dict(self.song(nid, title), artist="LEASHES")

    def test_untagged_file_named_from_navidrome(self):
        self.apply(self.manifest([self.untagged("sFqzz", "FERAL")]))
        t = self.db().tracks[0]
        self.assertEqual((t.title, t.artist), ("FERAL", "LEASHES"))   # not "sFqzz"

    def test_untagged_track_already_on_player_is_renamed(self):
        a = self.untagged("sFqzz", "FERAL")
        with mock.patch.object(h, "fill_missing_names", lambda track, t: False):
            self.apply(self.manifest([a]))                 # added the old way
        self.assertEqual(self.db().tracks[0].title, "sFqzz")
        summary = self.apply(self.manifest([a]))
        self.assertEqual((summary["renamed"], summary["added"]), (1, 0))  # no re-copy
        t = self.db().tracks[0]
        self.assertEqual((t.title, t.artist), ("FERAL", "LEASHES"))
        self.assertEqual(self.apply(self.manifest([a]))["renamed"], 0)

    def test_orphan_files_removed(self):
        stray = os.path.join(self.mount, "iPod_Control", "Music", "F01", "old.m4a")
        with open(stray, "wb") as f:
            f.write(b"leftover")
        self.assertEqual(h.reconcile(self.db(), self.rec, G, self.mount)["orphans"], 1)
        self.assertFalse(os.path.exists(stray))

    def test_playlist_membership_rename_and_removal(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a, b], [("playlist:P", "Mel Test", ["n1"]),
                                          ("starred", "Favorites", ["n2"])]))
        self.apply(self.manifest([a, b], [("playlist:P", "Road Trip", ["n1", "n2"])]))
        pls = {p.name: [t.title for t in p.tracks] for p in self.db().playlists
               if not p.is_master}
        self.assertEqual(pls, {"Road Trip": ["One", "Two"]})

    def test_players_own_playlist_never_touched(self):
        db = self.db()
        db.create_playlist("Favorites")                          # user-made
        db.save()
        a = self.song("n1", "One")
        self.apply(self.manifest([a], [("starred", "Favorites", ["n1"])]))
        favs = [p for p in self.db().playlists if p.name == "Favorites"]
        self.assertEqual([len(p.tracks) for p in favs], [0])

    def test_quality_change_replaces_track(self):
        a = self.song("n1", "One")
        self.apply(self.manifest([a]))
        b = dict(a, encoding="mp3 320k")
        summary = self.apply(self.manifest([b]))
        self.assertEqual((summary["replaced"], summary["added"]), (1, 1))
        self.assertEqual(len(self.db().tracks), 1)
        self.assertEqual(self.rec.tracks(G)[0]["encoding"], "mp3 320k")

    def test_sysinfo_restored_after_reset(self):
        self.rec.upsert_player(G, model="xB903")
        open(h.sysinfo_path(self.mount), "w").close()            # emptied by a restore
        self.assertEqual(h.ensure_sysinfo(self.mount, G, self.rec), "xB903")
        values, _ = h.read_sysinfo(self.mount)
        self.assertEqual(values, {"ModelNumStr": "xB903", "FirewireGuid": f"0x{G}"})
        self.assertEqual(self.db()._device.checksum_type.name, "HASH58")

    def test_model_from_serial_number(self):
        open(h.sysinfo_path(self.mount), "w").close()          # emptied by a restore
        model = h.ensure_sysinfo(self.mount, G, self.rec, usb_product=0x1261,
                                 block_device="/dev/sda",
                                 read_serial=lambda dev: "8K1234567Y5N")  # a Classic 80GB
        self.assertEqual(model, "xB029")
        self.assertEqual(h.read_sysinfo(self.mount)[0]["ModelNumStr"], "xB029")
        self.assertEqual(self.rec.player(G)["model"], "xB029")   # remembered

    def test_serial_read_failure_falls_back(self):
        open(h.sysinfo_path(self.mount), "w").close()

        def broken(dev):
            raise OSError("SG_IO not permitted")
        self.assertEqual(h.ensure_sysinfo(self.mount, G, self.rec, usb_product=0x1263,
                                          block_device="/dev/sda", read_serial=broken),
                         "xB903")

    def test_unknown_model_is_a_clear_error(self):
        open(h.sysinfo_path(self.mount), "w").close()
        with self.assertRaisesRegex(h.HandlerError, "Unknown iPod model"):
            h.ensure_sysinfo(self.mount, G, self.rec)
        self.assertEqual(h.ensure_sysinfo(self.mount, G, self.rec, usb_product=0x1263),
                         "xB903")

    def test_refuses_to_save_unsigned(self):
        with open(h.sysinfo_path(self.mount), "w") as f:
            f.write(f"FirewireGuid: 0x{G}\n")                    # model missing
        with self.assertRaisesRegex(h.HandlerError, "refusing to save"):
            h.checked_save(self.db(), G)

    def test_backup_and_restore(self):
        self.apply(self.manifest([self.song("n1", "One")]))
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        b = h.backup_player(self.mount, backups)
        self.apply(self.manifest([]))                            # removes the track
        self.assertEqual(len(self.db().tracks), 0)
        h.restore_backup(self.mount, b)
        self.assertEqual([t.title for t in self.db().tracks], ["One"])

    def test_failed_save_keeps_files_for_restore(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a, b]))
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        backup = h.backup_player(self.mount, backups)

        def failing_save(db, guid):
            raise OSError("USB unplugged")
        with self.assertRaises(OSError):
            self.apply(self.manifest([a]), save=failing_save)
        self.assertEqual(len(self.files()), 2)                   # nothing deleted yet
        h.restore_backup(self.mount, backup)
        db = self.db()
        self.assertEqual(sorted(t.title for t in db.tracks), ["One", "Two"])
        for t in db.tracks:                                      # every file is there
            self.assertTrue(os.path.exists(os.path.join(self.mount,
                                                        h.ipod_path_to_rel(t.ipod_path))))

    def test_artwork_ids_start_at_100(self):
        self.apply(self.manifest([self.song("n1", "One", cover=True),
                                  self.song("n2", "Two", cover=True)]))
        db = self.db()
        self.assertTrue(all(t.has_artwork for t in db.tracks))
        ids = [m.fields.get("image_id") for m in db._get_artwork_manager()._iter_mhii()] \
            if hasattr(db._get_artwork_manager(), "_iter_mhii") else None
        nxt = db._get_artwork_manager()._next_image_id
        self.assertGreaterEqual(nxt, 102)                        # used 100 and 101
        if ids is not None:
            self.assertTrue(all(i >= 100 for i in ids))

    def test_backups_pruned(self):
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        for _ in range(5):
            h.backup_player(self.mount, backups, keep=3)
        self.assertEqual(len(os.listdir(backups)), 3)


if __name__ == "__main__":
    unittest.main()
