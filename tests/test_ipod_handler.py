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
    def art(self):
        """{(image_id, format): thumbnail bytes} as stored on the player."""
        import glob
        mgr = self.db().artwork_manager()
        out = {}
        for mhii in mgr._get_or_create_mhli().children:
            for child in mhii.children:
                for mhni in [child] + list(getattr(child, "children", [])):
                    if mhni.magic != b"mhni":
                        continue
                    f = mhni.fields
                    path = glob.glob(os.path.join(self.mount, "iPod_Control", "Artwork",
                                                  f"F{f['format_id']}_1.ithmb"))[0]
                    with open(path, "rb") as fh:
                        fh.seek(f["ithumb_offset"])
                        out[(mhii.fields["image_id"], f["format_id"])] = fh.read(f["image_size"])
        return out

    def ithmb_sizes(self):
        import glob
        return {os.path.basename(p): os.path.getsize(p) for p in
                glob.glob(os.path.join(self.mount, "iPod_Control", "Artwork", "*.ithmb"))}

    def covered(self, n):
        """n songs, each with a cover of its own colour."""
        out = []
        for i in range(n):
            path = os.path.join(self.cache, "tracks", f"c{i}.mp3")
            fx.make_mp3(path, f"Song {i}", album=f"Album {i}", seconds=2,
                        cover=(20 * i % 256, 255 - 25 * i % 256, 90))
            out.append({"id": f"c{i}", "file": f"tracks/c{i}.mp3", "title": f"Song {i}",
                        "artist": "Artist", "album": f"Album {i}", "track": 1,
                        "duration": 2, "encoding": "mp3 v0"})
        return out

    def album(self, name, n, colour, first=0):
        """n tracks of one album sharing one cover colour."""
        out = []
        for i in range(first, first + n):
            nid = f"{name}{i}"
            path = os.path.join(self.cache, "tracks", f"{nid}.mp3")
            fx.make_mp3(path, f"{name} {i}", album=name, track=i + 1, seconds=2, cover=colour)
            out.append({"id": nid, "file": f"tracks/{nid}.mp3", "title": f"{name} {i}",
                        "artist": "Artist", "album": name, "track": i + 1, "duration": 2,
                        "encoding": "mp3 v0"})
        return out

    def links(self):
        return {t.title: t.mhii_link for t in self.db().tracks if t.has_artwork}

    def pictures(self):
        """{track title: its largest thumbnail's bytes}, as the iPod would show it."""
        art = self.art()
        out = {}
        for t in self.db().tracks:
            mine = {fmt: data for (image, fmt), data in art.items() if image == t.mhii_link}
            if mine:
                out[t.title] = mine[max(mine)]
        return out

    def test_an_albums_tracks_share_one_image(self):
        a, b = self.album("Red", 3, (200, 30, 30)), self.album("Blue", 2, (30, 30, 200))
        self.apply(self.manifest(a + b))
        links = self.links()
        self.assertEqual(len(set(links.values())), 2)                     # 5 tracks, 2 images
        self.assertEqual(len({links[f"Red {i}"] for i in range(3)}), 1)
        self.assertNotEqual(links["Red 0"], links["Blue 0"])
        self.assertEqual(len({k[0] for k in self.art()}), 2)
        more = self.album("Red", 1, (200, 30, 30), first=3)                # later sync
        self.apply(self.manifest(a + b + more))
        self.assertEqual(self.links()["Red 3"], links["Red 0"])

    def test_removing_tracks_keeps_the_shared_cover_until_the_last(self):
        a = self.album("Red", 3, (200, 30, 30))
        self.apply(self.manifest(a))
        before = self.pictures()
        owner = next(m for m in self.db().artwork_manager()._get_or_create_mhli().children)
        owner_dbid = owner.fields["song_id"]
        owner_title = next(t.title for t in self.db().tracks if t.dbid == owner_dbid)
        keep = [s for s in a if s["title"] != owner_title]                # remove the owner
        self.apply(self.manifest(keep))
        after = self.pictures()
        self.assertEqual(set(after), {s["title"] for s in keep})
        self.assertTrue(all(after[t] == before[t] for t in after))         # same picture
        mhii = self.db().artwork_manager()._get_or_create_mhli().children[0]
        self.assertIn(mhii.fields["song_id"], {t.dbid for t in self.db().tracks})   # re-owned
        self.apply(self.manifest([]))
        self.assertEqual(self.art(), {})                                   # last one: gone

    def test_existing_per_track_art_is_shared_once(self):
        from unittest import mock as m
        a, b = self.album("Red", 4, (200, 30, 30)), self.album("Blue", 2, (30, 30, 200))
        with m.patch.object(h, "_shares_art", return_value=False):        # the old way
            self.apply(self.manifest(a + b))
        self.assertEqual(len(set(self.links().values())), 6)
        before = self.pictures()
        db = self.db()
        self.assertEqual(h.share_existing_artwork(db), 4)                  # 6 images -> 2
        h.checked_save(db, G)
        self.assertEqual(len(set(self.links().values())), 2)
        self.assertEqual(self.pictures(), before)                          # every cover kept
        images = self.db().artwork_manager()._get_or_create_mhli().children
        dbids = {t.dbid for t in self.db().tracks}
        self.assertTrue(all(i.fields["song_id"] in dbids for i in images))

    def test_failed_sync_puts_the_old_art_files_back(self):
        songs = self.covered(8)
        self.apply(self.manifest(songs))
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        backup = h.backup_player(self.mount, backups)
        before, sizes = self.art(), self.ithmb_sizes()
        db = self.db()
        for t in list(db.tracks)[:5]:
            db.remove_track(t, delete_file=False)
        h.checked_save(db, G)                                              # compacts
        self.assertNotEqual(self.ithmb_sizes(), sizes)
        self.assertTrue(h._ithmb_leftovers(self.mount, h.BEFORE_COMPACT))
        h.restore_backup(self.mount, backup)                               # ...then fails
        self.assertEqual(self.ithmb_sizes(), sizes)
        self.assertEqual(self.art(), before)

    def test_crash_mid_compaction_keeps_the_matching_files(self):
        songs = self.covered(8)
        self.apply(self.manifest(songs))
        before = self.art()
        db = self.db()
        for t in list(db.tracks)[:5]:
            db.remove_track(t, delete_file=False)
        h.checked_save(db, G)
        compacted = self.art()
        # Crash after the swap, before the sync finished: the new ArtworkDB is
        # on disk and fits the new files, so they stay.
        self.assertEqual(h.recover_compaction(self.mount), "kept compacted art")
        self.assertEqual(self.art(), compacted)
        # Crash before the new ArtworkDB landed: old ArtworkDB, new files.
        self.apply(self.manifest(self.covered(8)))
        artdb = os.path.join(self.mount, "iPod_Control", "Artwork", "ArtworkDB")
        with open(artdb, "rb") as f:
            old_artdb = f.read()
        before = self.art()
        db = self.db()
        for t in list(db.tracks)[:5]:
            db.remove_track(t, delete_file=False)
        h.checked_save(db, G)
        with open(artdb, "wb") as f:
            f.write(old_artdb)
        self.assertEqual(h.recover_compaction(self.mount), "restored art from before compaction")
        self.assertEqual(self.art(), before)

    def test_a_few_removals_leave_the_art_files_alone(self):
        songs = self.covered(8)
        self.apply(self.manifest(songs))
        before, sizes = self.art(), self.ithmb_sizes()
        self.apply(self.manifest(songs[1:]))                     # 1 of 8: 12.5% unused
        after = self.art()
        self.assertEqual(self.ithmb_sizes(), sizes)             # nothing rewritten
        self.assertEqual(len(after), len(before) - len(before) // 8)
        self.assertTrue(all(before[k] == v for k, v in after.items()))

    def test_many_removals_compact_without_mixing_up_covers(self):
        songs = self.covered(8)
        self.apply(self.manifest(songs))
        before, sizes = self.art(), self.ithmb_sizes()
        self.apply(self.manifest(songs[:3]))                    # 5 of 8 gone
        after = self.art()
        self.assertTrue(all(before[k] == v for k, v in after.items()))   # same pictures
        self.assertEqual(len({k[0] for k in after}), 3)
        for name, size in self.ithmb_sizes().items():
            self.assertLess(size, sizes[name])                  # files shrank ...
        used = {}
        for (image, fmt), data in after.items():
            used[fmt] = used.get(fmt, 0) + len(data)
        self.assertEqual(sorted(used.values()), sorted(self.ithmb_sizes().values()))
        self.apply(self.manifest(songs[:3] + self.covered(9)[8:]))   # new art still fits
        self.assertTrue(all(v == self.art()[k] for k, v in after.items()))

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
        """The live player's database as the handler opens it (its own work
        folder each time, so two can be open at once)."""
        return h.open_database(self.mount, tempfile.mkdtemp(dir=self.tmp))

    def apply(self, manifest, **kw):
        db = self.db()
        report = h.reconcile(db, self.rec, G, self.mount, manifest=manifest)
        return h.apply_plan(db, self.rec, G, self.mount, manifest, self.cache,
                            dirty=report["dirty"], pending=report["pending"], **kw)

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

    def test_track_dropped_from_full_cache_stays_and_gets_renamed(self):
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a, b]))
        os.remove(os.path.join(self.cache, b["file"]))           # cache made room
        m = self.manifest([a])
        m["on_player"] = [{k: v for k, v in dict(b, genre="Chiptune").items()
                           if k not in ("file", "size")}]
        summary = self.apply(m)
        self.assertEqual((summary["removed"], summary["added"]), (0, 0))
        self.assertEqual({t.title: t.genre for t in self.db().tracks}["Two"], "Chiptune")
        self.assertEqual(h.pending_changes(m, ["n1", "n2"]), (0, 0))

    def test_player_bigger_than_cache_fills_over_several_syncs(self):
        # Prepare -> sync cycles with a cache that holds about 2 of 6 tracks:
        # each sync adds what's cached, the next prepare drops those files
        # (now on the player) for the next ones. Nothing is ever removed.
        import json
        import dock_prepare
        from test_dock_prepare import ROOMY, FakeServer, config, song

        def convert(src, dst, options, duration=None):
            fx.make_mp3(dst, os.path.basename(dst), seconds=2)
        sample = fx.make_mp3(os.path.join(self.tmp, "sample.mp3"), "x", seconds=2)
        size = os.path.getsize(sample)
        server = FakeServer(songs=[song(f"s{i}", size=size // 2, track=i) for i in range(6)])
        limit = 2 * size + 6000                    # + the plan file itself
        cfg = config(max_gb=limit / 1e9)
        cfg["sync"]["workers"] = 1
        for cycle in range(6):
            dock_prepare.prepare(server, cfg, self.cache, convert_fn=convert,
                                 disk_usage=ROOMY,
                                 on_player=self.rec.songs_on_every_player())
            self.assertLessEqual(dock_prepare.dir_bytes(self.cache), limit + size)
            with open(os.path.join(self.cache, "manifest.json")) as f:
                summary = self.apply(json.load(f))
            self.assertEqual(summary["removed"], 0)
            if len(self.db().tracks) == 6:
                break
        self.assertEqual(sorted(h.navidrome_id_from(t) for t in self.db().tracks),
                         [f"s{i}" for i in range(6)])
        self.assertGreater(cycle, 1)                       # it really took batches

    def test_crash_mid_sync_resumes_without_copying_again(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(5)]
        real_save, calls = h.checked_save, []

        def crash_on_second_save(db, guid):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("power cut")
            real_save(db, guid)
        with self.assertRaises(RuntimeError):         # save_every=0: a save per track
            self.apply(self.manifest(songs), save_every=0, save=crash_on_second_save)
        self.assertEqual(len(self.db().tracks), 1)              # the first save
        self.assertEqual(len(self.files()), 2)                  # the second file got copied
        self.assertEqual(len(self.rec.copied(G)), 1)            # ... and logged
        from pygpod.device import fileutils
        with mock.patch.object(fileutils, "copy_track_to_ipod",
                               wraps=fileutils.copy_track_to_ipod) as copies:
            summary = self.apply(self.manifest(songs))
        self.assertEqual(summary["added"], 3)                   # only the 3 never copied
        self.assertEqual(copies.call_count, 3)
        self.assertEqual(sorted(t.title for t in self.db().tracks),
                         [f"Song {i}" for i in range(5)])
        self.assertEqual(len(self.files()), 5)
        self.assertEqual(len(self.rec.tracks(G)), 5)
        self.assertEqual(self.rec.copied(G), [])

    def test_logged_file_that_is_gone_is_just_forgotten(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(2)]

        def crash(db, guid):
            raise RuntimeError("power cut")
        with self.assertRaises(RuntimeError):
            self.apply(self.manifest(songs), save=crash)
        for rel in self.files():
            os.remove(os.path.join(self.mount, rel))            # e.g. a restore wiped them
        summary = self.apply(self.manifest(songs))
        self.assertEqual(summary["added"], 2)
        self.assertEqual(len(self.db().tracks), 2)
        self.assertEqual(self.rec.copied(G), [])

    def follow(self, plans, first, save=None, **kw):
        """One sync following a prepare run that hands over `plans` after
        `first` (interim ones marked so); returns (summary, saves)."""
        saves, queue = [], list(plans)

        def counting_save(db, guid):
            saves.append(len(db.tracks))
            (save or h.checked_save)(db, guid)

        def next_plan(plan):
            return queue.pop(0) if queue else None
        db = self.db()
        report = h.reconcile(db, self.rec, G, self.mount, manifest=first)
        summary = h.follow_plans(db, self.rec, G, self.mount, first, self.cache, next_plan,
                                 session=h.new_session(report["pending"]), save=counting_save,
                                 **kw)
        return summary, saves

    def interim(self, ready, waiting, playlists=()):
        plan = self.manifest(ready, playlists)
        plan["skipped"] = [{"id": t["id"], "reason": "waiting to be prepared", "error": False}
                           for t in waiting]
        plan["interim"] = True
        return plan

    def test_one_sync_follows_a_prepare_run_and_saves_once(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(4)]
        favs = [("starred", "Favorites", [s["id"] for s in songs])]
        first = self.interim(songs[:1], songs[1:])
        plans = [self.interim(songs[:3], songs[3:]),
                 self.manifest(songs, favs)]                    # the run's final plan
        summary, saves = self.follow(plans, first)
        self.assertEqual(saves, [4])                            # one save, at the end
        self.assertEqual((summary["added"], summary["plans"]), (4, 3))
        self.assertEqual(sorted(t.title for t in self.db().tracks),
                         [f"Song {i}" for i in range(4)])
        pls = {p.name: [t.title for t in p.tracks] for p in self.db().playlists
               if not p.is_master}
        self.assertEqual(pls, {"Favorites": [f"Song {i}" for i in range(4)]})
        self.assertEqual(len(self.rec.tracks(G)), 4)
        self.assertEqual(self.rec.copied(G), [])

    def test_a_run_that_goes_away_still_ends_with_a_save(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(3)]
        self.apply(self.manifest(songs[2:]))                    # already on the player
        first = self.interim(songs[:2], songs[2:])
        summary, saves = self.follow([], first)                 # no plan ever follows
        self.assertEqual(saves, [3])
        self.assertEqual((summary["added"], summary["removed"]), (2, 0))  # waiting kept
        self.assertEqual(len(self.rec.tracks(G)), 3)

    def test_safe_hours_stop_copying_and_finish_the_sync(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(4)]
        favs = [("starred", "Favorites", [s["id"] for s in songs])]
        checks = []

        def wall():                          # the window opens after two songs
            checks.append(1)
            return 0 if len(checks) <= 2 else 1000
        first = self.interim(songs[:3], songs[3:], favs)
        later = [self.manifest(songs, favs)]           # never reached: safe hours
        summary, saves = self.follow(later, first, deadline=500, wall=wall)
        self.assertTrue(summary["paused"])
        self.assertEqual(summary["added"], 2)
        self.assertEqual(saves, [2])                     # finished properly: one save
        pls = {p.name: len(p.tracks) for p in self.db().playlists if not p.is_master}
        self.assertEqual(pls, {"Favorites": 2})
        self.assertEqual(len(self.rec.tracks(G)), 2)
        self.assertEqual(self.rec.copied(G), [])
        self.assertEqual(self.apply(self.manifest(songs, favs))["added"], 2)   # after

    def test_no_more_than_the_stock_limit_is_added(self):
        import dock_limits
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(5)]
        with mock.patch.object(dock_limits, "STOCK_LIMIT", 3):
            summary = self.apply(self.manifest(songs))
        self.assertEqual((summary["added"], summary["not_added_limit"]), (3, 2))
        self.assertEqual(len(self.db().tracks), 3)

    def test_unsaved_tracks_are_capped(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(5)]
        with mock.patch.object(h, "SAVE_AFTER", 2):
            _, saves = self.follow([], self.manifest(songs))
        self.assertEqual(saves, [2, 4, 5])

    def test_database_saved_once_unless_an_hour_passes(self):
        songs = [self.song(f"n{i}", f"Song {i}") for i in range(7)]
        saves, now = [], [0]

        def counting_save(db, guid):
            saves.append(len(db.tracks))
            h.checked_save(db, guid)

        def clock():
            now[0] += 25 * 60                   # each track "takes" 25 minutes
            return now[0]
        self.apply(self.manifest(songs[:3]), save=counting_save)
        self.assertEqual(saves, [3])                            # one save at the end
        saves.clear()
        self.apply(self.manifest(songs), save=counting_save, clock=clock)
        self.assertEqual(saves, [6, 7])                         # after an hour, then the end
        self.assertEqual(self.rec.copied(G), [])

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

    def test_corrected_album_name_reaches_tracks_on_player(self):
        a = self.song("p10", "Hold Your Colour", album="Hold Your Colour")
        self.apply(self.manifest([a]))
        fixed = dict(a, album="Hold Your Colour (2007 re-release)")
        summary = self.apply(self.manifest([fixed]))
        self.assertEqual((summary["renamed"], summary["added"]), (1, 0))   # no re-copy
        self.assertEqual(self.db().tracks[0].album, "Hold Your Colour (2007 re-release)")
        self.assertEqual(self.apply(self.manifest([fixed]))["renamed"], 0)

    def test_plan_genre_replaces_file_tags_on_new_and_existing_tracks(self):
        a = self.song("g1", "Liebe")
        self.apply(self.manifest([a]))                     # plan without genres
        self.apply(self.manifest([dict(a, genre="Electronic"),
                                  dict(self.song("g2", "Squaredance"), genre="Chiptune")]))
        genres = {t.title: t.genre for t in self.db().tracks}
        self.assertEqual(genres, {"Liebe": "Electronic", "Squaredance": "Chiptune"})
        summary = self.apply(self.manifest([dict(a, genre="Pop"),
                                            dict(self.song("g2", "Squaredance"),
                                                 genre="Chiptune")]))
        self.assertEqual((summary["renamed"], summary["added"]), (1, 0))   # no re-copy
        self.assertEqual({t.title: t.genre for t in self.db().tracks}["Liebe"], "Pop")

    def album_list_titles(self):
        """Titles in the order the iPod's album browse list (sort index 4)
        gives them, read back from the saved iTunesDB."""
        import struct
        db = self.db()
        ids = [t.track_id for t in db.tracks]          # the master playlist's order
        title = {t.track_id: t.title for t in db.tracks}
        with open(os.path.join(self.mount, "iPod_Control", "iTunes", "iTunesDB"), "rb") as f:
            data = f.read()
        pos = data.find(b"mhod")
        while pos != -1:
            hlen, _, mtype = struct.unpack_from("<III", data, pos + 4)
            if mtype == 52:
                key, count = struct.unpack_from("<II", data, pos + hlen)
                if key == 4:
                    idx = struct.unpack_from(f"<{count}I", data, pos + hlen + 48)
                    return [title[ids[i]] for i in idx]
            pos = data.find(b"mhod", pos + 4)
        self.fail("no album sort index in the iTunesDB")

    def test_album_list_follows_track_numbers_not_add_order(self):
        # The title track went on first (its album name differed back then).
        hyc = self.song("t10", "Hold Your Colour", album="HYC", track=10)
        self.apply(self.manifest([hyc]))
        rest = [self.song(f"t{n}", f"Song {n}", album="HYC", track=n) for n in (2, 1, 3)]
        self.apply(self.manifest([hyc] + rest))
        self.assertEqual(self.album_list_titles(),
                         ["Song 1", "Song 2", "Song 3", "Hold Your Colour"])

    def test_sort_order_marker(self):
        self.assertTrue(h.sort_order_outdated(self.mount))
        h.mark_sort_order(self.mount)
        self.assertFalse(h.sort_order_outdated(self.mount))

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

    def test_emptied_playlist_of_the_same_name_is_refilled(self):
        # E.g. after a mass removal took every track out of it (2026-10-23).
        db = self.db()
        db.create_playlist("Favorites")
        db.save()
        a = self.song("n1", "One")
        self.apply(self.manifest([a], [("starred", "Favorites", ["n1"])]))
        favs = [p for p in self.db().playlists if p.name == "Favorites"]
        self.assertEqual([[t.title for t in p.tracks] for p in favs], [["One"]])

    def test_playlist_of_tracks_the_record_knows_is_adopted(self):
        # Adopted hand-copied tracks have no Navidrome ID in their comment,
        # but the record knows them.
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a], [("playlist:Mix", "Mix", ["n1"])]))
        db = self.db()
        for t in db.tracks:
            t.comment = ""
        h.checked_save(db, G)
        self.rec.set_managed_playlists(G, {})                     # forgotten by the record
        self.apply(self.manifest([a, b], [("playlist:Mix", "Mix", ["n1", "n2"])]))
        mixes = [p for p in self.db().playlists if p.name == "Mix"]
        self.assertEqual([[t.title for t in p.tracks] for p in mixes], [["One", "Two"]])

    def test_lost_record_readopts_the_docks_playlists(self):
        # A new dock (or a lost record) re-adopts tracks by their Navidrome
        # ID; its playlists, holding only such tracks, must follow, or they
        # never update again (seen 2026-10-23 after moving to a new Pi).
        a, b = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([a], [("playlist:Mix", "Mix", ["n1"])]))
        self.rec.close()
        self.rec = Record(os.path.join(self.tmp, "new-dock.db"))
        summary = self.apply(self.manifest([a, b], [("playlist:Mix", "Mix", ["n1", "n2"])]))
        self.assertEqual(summary["playlists_changed"], 1)
        mixes = [p for p in self.db().playlists if p.name == "Mix"]
        self.assertEqual([[t.title for t in p.tracks] for p in mixes], [["One", "Two"]])
        self.assertEqual(self.rec.managed_playlists(G), {"playlist:Mix": "Mix"})

    def test_playlist_with_a_track_the_dock_didnt_add_is_left_alone(self):
        own = fx.make_mp3(os.path.join(self.tmp, "own.mp3"), "Mine", seconds=2)
        db = self.db()
        mine = db.add_track(own)                                 # no Navidrome ID
        pl = db.create_playlist("Mix")
        db.add_track_to_playlist(pl, mine)
        db.save()
        a = self.song("n1", "One")
        self.apply(self.manifest([a], [("playlist:Mix", "Mix", ["n1"])]))
        mixes = [p for p in self.db().playlists if p.name == "Mix"]
        self.assertEqual([[t.title for t in p.tracks] for p in mixes], [["Mine"]])

    def test_quality_change_replaces_track(self):
        a = self.song("n1", "One")
        self.apply(self.manifest([a]))
        b = dict(a, encoding="mp3 320k")
        summary = self.apply(self.manifest([b]))
        self.assertEqual((summary["replaced"], summary["added"]), (1, 1))
        self.assertEqual(len(self.db().tracks), 1)
        self.assertEqual(self.rec.tracks(G)[0]["encoding"], "mp3 320k")

    def test_artist_and_title_follow_the_plan_without_a_copy(self):
        one, two = self.song("n1", "One"), self.song("n2", "Two")
        self.apply(self.manifest([one, two]))
        files = self.files()
        renamed = dict(two, artist="Main", title="Two (feat. Guest)")
        self.apply(self.manifest([dict(one, artist="Main"), renamed]))
        self.assertEqual(self.files(), files)
        tracks = {t.title: t for t in self.db().tracks}
        self.assertEqual(tracks["Two (feat. Guest)"].artist, "Main")
        self.assertEqual(tracks["One"].artist, "Main")
        # The saved artist list, which is what the iPod's Artists menu reads.
        with open(os.path.join(self.mount, "iPod_Control", "iTunes", "iTunesDB"), "rb") as f:
            saved = f.read()
        self.assertIn("Main".encode("utf-16-le"), saved)
        self.assertEqual(saved.count(b"mhii"), 1)      # one artist item

    def test_replacing_a_file_keeps_the_ipods_counts(self):
        a = self.song("n1", "One")
        self.apply(self.manifest([a]))
        db = self.db()
        for offset, fmt, value in ((0x50, "<I", 7), (0x9C, "<I", 2), (0x1F, "<B", 80),
                                   (0x58, "<I", 3000000000)):
            db.tracks[0].set_header(offset, fmt, value)
        db.save()
        before = self.db().tracks[0]
        self.assertEqual((before.play_count, before.skip_count), (7, 2))
        summary = self.apply(self.manifest([dict(a, encoding="alac 16")]))
        self.assertEqual(summary["replaced"], 1)
        tracks = self.db().tracks
        self.assertEqual(len(tracks), 1)
        self.assertNotEqual(tracks[0].dbid, before.dbid)
        self.assertEqual((tracks[0].play_count, tracks[0].skip_count, tracks[0].rating,
                          tracks[0].time_played), (7, 2, 80, 3000000000))

    def counted_swap(self):
        """A song with iPod counts, plus a playlist, and a plan to swap it."""
        a = self.song("n1", "One")
        lists = [("playlist:L", "L", ["n1"])]
        self.apply(self.manifest([a], lists))
        db = self.db()
        db.tracks[0].set_header(0x50, "<I", 7)
        db.tracks[0].set_header(0x1F, "<B", 80)
        db.save()
        shutil.copy(os.path.join(self.cache, "tracks", "n1.mp3"),
                    os.path.join(self.cache, "tracks", "n1.m4a"))
        return self.manifest([dict(a, file="tracks/n1.m4a", encoding="alac 16")], lists)

    def test_power_cut_during_a_swap_keeps_the_old_track_and_its_counts(self):
        plan = self.counted_swap()
        before = self.db().tracks[0].dbid

        def power_cut(db, guid):
            raise RuntimeError("power cut")
        with self.assertRaises(RuntimeError):
            self.apply(plan, save=power_cut)
        self.assertEqual(len(self.files()), 2)                # the new file was copied
        track = self.db().tracks[0]
        self.assertEqual((track.dbid, track.play_count, track.rating), (before, 7, 80))
        self.apply(plan)                                      # the next sync finishes it
        tracks = self.db().tracks
        self.assertEqual(len(tracks), 1)
        self.assertNotEqual(tracks[0].dbid, before)
        self.assertEqual((tracks[0].play_count, tracks[0].rating), (7, 80))
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.rec.copied(G), [])
        self.assertEqual([t.dbid for t in self.db().playlists[-1].tracks], [tracks[0].dbid])

    def test_safe_hours_before_a_swap_leaves_the_song_alone(self):
        plan = self.counted_swap()
        before = self.db().tracks[0].dbid
        session = h.new_session()
        summary = self.apply(plan, session=session, deadline=1, wall=lambda: 2)
        self.assertTrue(session["paused"])
        self.assertEqual((summary["replaced"], summary["removed"]), (0, 0))
        track = self.db().tracks[0]
        self.assertEqual((track.dbid, track.play_count), (before, 7))
        self.assertEqual(len(self.files()), 1)

    def test_swap_keeps_its_counts_and_playlist_place(self):
        plan = self.counted_swap()
        self.apply(plan)
        db = self.db()
        self.assertEqual((db.tracks[0].play_count, db.tracks[0].rating), (7, 80))
        self.assertEqual([t.dbid for t in db.playlists[-1].tracks], [db.tracks[0].dbid])
        self.assertEqual(len(self.files()), 1)

    def test_tight_player_saves_between_swaps_to_free_the_old_files(self):
        songs = [self.song(f"n{i}", f"Song {i}", track=i) for i in (1, 2)]
        self.apply(self.manifest(songs))
        new = []
        for s in songs:
            shutil.copy(os.path.join(self.cache, s["file"]),
                        os.path.join(self.cache, "tracks", f"{s['id']}.m4a"))
            new.append(dict(s, file=f"tracks/{s['id']}.m4a", encoding="alac 16"))
        one = os.path.getsize(os.path.join(self.cache, songs[0]["file"]))
        saves, copying = [], []                       # room for one extra file at a time
        real = shutil.disk_usage(self.tmp)

        def save(db, guid):
            saves.append(1)
            h.checked_save(db, guid)

        def free(path):
            extra = one if saves or not copying else 0
            return real._replace(free=h.FREE_SPACE_MARGIN + extra)
        with mock.patch("shutil.disk_usage", side_effect=free):
            summary = self.apply(self.manifest(new), save=save,
                                 progress=lambda *a: copying.append(1))
        self.assertEqual((summary["replaced"], summary["swaps_no_room"]), (2, 0))
        self.assertEqual(len(saves), 2)
        self.assertEqual(len(self.db().tracks), 2)

    def swap_with_free(self, free):
        """An MP3 on the player, then the plan asks for a 40 MB lossless file."""
        a = self.song("n1", "One")
        self.apply(self.manifest([a]))
        with open(os.path.join(self.cache, "tracks", "n1.m4a"), "wb") as f:
            f.truncate(40_000_000)
        before = self.db().tracks[0].dbid
        usage = shutil.disk_usage(self.tmp)
        with mock.patch("shutil.disk_usage", return_value=usage._replace(free=free)):
            summary = self.apply(self.manifest(
                [dict(a, file="tracks/n1.m4a", encoding="alac 16", title="Renamed")]))
        return before, summary

    def test_swap_without_room_keeps_the_song_as_it_is(self):
        self.rec.upsert_player(G, model="xB903")
        before, summary = self.swap_with_free(h.FREE_SPACE_MARGIN + 1_000_000)
        self.assertEqual((summary["replaced"], summary["removed"], summary["swaps_no_room"]),
                         (0, 0, 1))
        track = self.db().tracks[0]
        self.assertEqual((track.dbid, track.title), (before, "Renamed"))    # names still follow
        self.assertEqual(self.rec.tracks(G)[0]["encoding"], "mp3 v0")
        self.assertEqual(self.rec.player(G)["swaps_no_room"], 1)

    def test_a_full_sync_marks_the_player_and_a_later_one_clears_it(self):
        self.rec.upsert_player(G, model="xB903")
        a = self.song("n1", "One")
        usage = shutil.disk_usage(self.tmp)
        with mock.patch("shutil.disk_usage", return_value=usage._replace(free=1000)):
            summary = self.apply(self.manifest([a]))
        self.assertEqual(summary["not_added_full"], 1)
        self.assertEqual(self.rec.player(G)["full"], 1)
        self.apply(self.manifest([a]))
        self.assertEqual(self.rec.player(G)["full"], 0)

    def test_swap_with_room_happens_and_clears_the_count(self):
        self.rec.upsert_player(G, model="xB903")
        self.rec.set_swaps_no_room(G, 5)
        before, summary = self.swap_with_free(h.FREE_SPACE_MARGIN + 100_000_000)
        self.assertEqual((summary["replaced"], summary["swaps_no_room"]), (1, 0))
        self.assertNotEqual(self.db().tracks[0].dbid, before)
        self.assertEqual(self.rec.tracks(G)[0]["encoding"], "alac 16")
        self.assertEqual(self.rec.player(G)["swaps_no_room"], 0)

    def test_swap_whose_new_file_is_gone_keeps_the_song(self):
        a = self.song("n1", "One")
        self.apply(self.manifest([a]))
        before = self.db().tracks[0].dbid
        summary = self.apply(self.manifest([dict(a, file="tracks/gone.m4a", encoding="alac 16")]))
        self.assertEqual((summary["removed"], summary["swaps_no_room"]), (0, 0))
        self.assertEqual(self.db().tracks[0].dbid, before)

    def test_room_goes_to_missing_songs_then_the_smallest_swaps(self):
        margin = h.FREE_SPACE_MARGIN
        swaps = {"big": (100, 10), "small": (30, 10), "shrinks": (10, 50)}
        self.assertEqual(h.swaps_without_room(margin + 30, swaps), {"big"})
        self.assertEqual(h.swaps_without_room(margin + 130, swaps, new_bytes=100),
                         {"big"})
        self.assertEqual(h.swaps_without_room(margin + 30, swaps, new_bytes=100),
                         {"big", "small", "shrinks"})
        self.assertEqual(h.swaps_without_room(margin, swaps, freed=500), set())

    def test_held_back_swaps_arent_counted_as_pending(self):
        plan = {"tracks": [{"id": "a", "encoding": "alac 16"}, {"id": "b", "encoding": "alac 16"}]}
        records = [{"navidrome_id": n, "encoding": "mp3 v0"} for n in ("a", "b")]
        self.assertEqual(h.conversions_ready(plan, records), (2, 0, 0))
        self.assertEqual(h.conversions_ready(plan, records, 1), (1, 0, 1))
        self.assertEqual(h.conversions_ready(plan, records, 9), (0, 0, 2))

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

    def set_model(self, model):
        with open(h.sysinfo_path(self.mount), "w") as f:
            f.write(f"ModelNumStr: {model}\nFirewireGuid: 0x{G}\n")

    def test_saves_for_every_supported_generation(self):
        for model in ("xB029", "xB565", "xA980", "xB903",      # hash58 required
                      "xA002", "xA446", "xA004", "xA426"):     # no signature
            with self.subTest(model=model):
                self.set_model(model)
                self.assertEqual(h.ensure_sysinfo(self.mount, G, self.rec), model)
                h.checked_save(self.db(), G)

    def test_unsupported_models_refused_before_any_write(self):
        for model, reason in (("xC027", "Nano 5th Gen isn't supported"),
                              ("xC525", "Nano 6th Gen isn't supported"),
                              ("xA546", "Shuffle 2nd Gen isn't supported"),
                              ("x9282", "4th Gen isn't supported"),
                              ("xZZZZ", "Unknown iPod model xZZZZ")):
            with self.subTest(model=model):
                with open(h.sysinfo_path(self.mount), "w") as f:
                    f.write(f"ModelNumStr: {model}\n")
                with self.assertRaisesRegex(h.HandlerError, reason):
                    h.ensure_sysinfo(self.mount, G, self.rec)
                self.assertEqual(h.read_sysinfo(self.mount)[0], {"ModelNumStr": model})
                with self.assertRaisesRegex(h.HandlerError, "refusing to save"):
                    h.checked_save(self.db(), G)

    def test_empty_sysinfo_model_from_usb_id(self):
        for product, model in ((0x1261, "xB029"), (0x1209, "xA002"), (0x1260, "xA426")):
            with self.subTest(product=hex(product)):
                open(h.sysinfo_path(self.mount), "w").close()
                rec = Record(os.path.join(self.tmp, f"{product}.db"))
                self.addCleanup(rec.close)
                self.assertEqual(h.ensure_sysinfo(self.mount, G, rec, usb_product=product),
                                 model)

    def test_refuses_to_save_unsigned(self):
        with open(h.sysinfo_path(self.mount), "w") as f:
            f.write(f"FirewireGuid: 0x{G}\n")                    # model missing
        # With no model in SysInfo, pygpod borrows the model of the first
        # Apple USB device it finds (via /sys): on a dock with an iPod
        # plugged in, that made this test pass the save. Keep it hermetic.
        with mock.patch("pygpod.device.usb.detect_ipod_usb", return_value=[]), \
                self.assertRaisesRegex(h.HandlerError, "refusing to save"):
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
        ids = [m.fields.get("image_id") for m in db.artwork_manager()._iter_mhii()] \
            if hasattr(db.artwork_manager(), "_iter_mhii") else None
        nxt = db.artwork_manager()._next_image_id
        self.assertGreaterEqual(nxt, 101)                        # used 100 ...
        self.assertEqual({t.mhii_link for t in db.tracks}, {100})   # ... shared (one album)
        if ids is not None:
            self.assertTrue(all(i >= 100 for i in ids))

    def test_navidrome_cover_added_to_track_already_on_player(self):
        a = self.song("n1", "One")                       # synced without art
        self.apply(self.manifest([a]))
        self.assertFalse(self.db().tracks[0].has_artwork)
        fx.make_mp3(os.path.join(self.cache, a["file"]), "One", seconds=2, cover=True)
        summary = self.apply(self.manifest([{**a, "cover": "navidrome"}]))
        self.assertEqual((summary["art_added"], summary["added"], summary["removed"]),
                         (1, 0, 0))                      # in place, no re-copy
        self.assertTrue(self.db().tracks[0].has_artwork)
        again = self.apply(self.manifest([{**a, "cover": "navidrome"}]))
        self.assertEqual(again["art_added"], 0)

    def test_backups_pruned(self):
        backups = os.path.join(self.tmp, "backups")
        os.makedirs(backups)
        for _ in range(5):
            h.backup_player(self.mount, backups, keep=3)
        self.assertEqual(len(os.listdir(backups)), 3)


if __name__ == "__main__":
    unittest.main()
