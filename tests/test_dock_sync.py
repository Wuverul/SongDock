"""Tests for the sync job's ordering and safety rules.

Run from the project folder:  python3 -m unittest discover -s tests -v
"""

import logging
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_sync  # noqa: E402
from dock_sync import FakeDevice, SyncError, run_sync  # noqa: E402

# The failure tests log tracebacks on purpose; keep test output readable.
logging.disable(logging.CRITICAL)


class RecordingDevice(FakeDevice):
    """FakeDevice that records mount/unmount into a shared event list."""

    def __init__(self, folder, events, fail=None):
        super().__init__(folder, fail=fail)
        self.events = events

    def mount(self):
        self.events.append("mount")
        super().mount()

    def unmount(self):
        self.events.append("unmount")
        super().unmount()


class SyncOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = self.tmp.name
        with open(os.path.join(self.folder, "song.m4a"), "wb") as f:
            f.write(b"x" * 10)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def show(self, screen, *args, force=False):
        self.events.append(screen)

    def screens(self):
        return [e for e in self.events if e not in ("mount", "unmount")]

    def test_happy_path(self):
        dev = RecordingDevice(self.folder, self.events)
        self.assertTrue(run_sync(dev, self.show))
        self.assertEqual(self.events[0], "syncing")
        self.assertLess(self.events.index("syncing"), self.events.index("mount"))
        self.assertLess(self.events.index("unmount"), self.events.index("safe"))
        self.assertEqual(self.events[-1], "safe")
        self.assertFalse(dev.mounted)

    def test_no_mount_if_syncing_screen_fails(self):
        def broken_show(screen, *args, force=False):
            raise OSError("SPI error")
        dev = RecordingDevice(self.folder, self.events)
        self.assertFalse(run_sync(dev, broken_show))
        self.assertNotIn("mount", self.events)

    def test_mount_failure_shows_failed(self):
        dev = RecordingDevice(self.folder, self.events, fail="mount")
        self.assertFalse(run_sync(dev, self.show))
        self.assertNotIn("unmount", self.events)   # never mounted
        self.assertEqual(self.events[-1], "failed")

    def test_read_failure_still_unmounts(self):
        dev = RecordingDevice(self.folder, self.events)
        with mock.patch.object(dock_sync, "list_contents",
                               side_effect=OSError("I/O error")):
            self.assertFalse(run_sync(dev, self.show))
        self.assertIn("unmount", self.events)
        self.assertLess(self.events.index("unmount"), self.events.index("failed"))
        self.assertFalse(dev.mounted)

    def test_unmount_failure_never_says_safe(self):
        dev = RecordingDevice(self.folder, self.events, fail="unmount")
        self.assertFalse(run_sync(dev, self.show))
        self.assertNotIn("safe", self.screens())
        self.assertNotIn("failed", self.screens())   # also says "safe to remove"
        self.assertEqual(self.screens()[-1], "syncing")

    def test_progress_redraw_failure_does_not_abort(self):
        calls = []

        def flaky_show(screen, *args, force=False):
            calls.append(screen)
            if len(calls) == 2:          # first progress redraw fails
                raise OSError("SPI hiccup")
        dev = FakeDevice(self.folder)
        self.assertTrue(run_sync(dev, flaky_show))
        self.assertEqual(calls[-1], "safe")


class WorkTests(unittest.TestCase):
    """run_sync with a player-specific session (e.g. the iPod handler)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events, self.shown = [], []

    def show(self, screen, *args, force=False):
        self.events.append(screen)
        self.shown.append((screen, args))

    def test_work_runs_between_mount_and_unmount_and_feeds_safe_screen(self):
        dev = RecordingDevice(self.tmp.name, self.events)

        def work(device, step):
            self.events.append("work")
            step(5, "Copying 1/2: A - B")
            return {"added": 2, "removed": 1, "plays_sent": 3, "plays_queued": 4}
        self.assertTrue(run_sync(dev, self.show, work=work))
        order = [e for e in self.events if e in ("syncing", "mount", "work", "unmount", "safe")]
        self.assertEqual(order[:3], ["syncing", "mount", "work"])
        self.assertLess(self.events.index("unmount"), self.events.index("safe"))
        self.assertEqual(self.shown[-1], ("safe", ("--added", "2", "--removed", "1",
                                                   "--plays", "3", "--queued", "4")))
        self.assertIn(("syncing", ("--step", "1", "--total", "6", "--detail",
                                   "Mounting player")), self.shown)

    def test_copy_progress_feeds_the_bar(self):
        dev = RecordingDevice(self.tmp.name, self.events)

        def work(device, step):
            step(5, "Copying 3/79: A - B", part_done=2, part_total=79)
            return {}
        run_sync(dev, self.show, work=work)
        self.assertIn(("syncing", ("--step", "5", "--total", "6", "--detail",
                                   "Copying 3/79: A - B", "--part-done", "2",
                                   "--part-total", "79")), self.shown)

    def test_work_failure_still_unmounts_and_shows_failed(self):
        dev = RecordingDevice(self.tmp.name, self.events)

        def work(device, step):
            raise RuntimeError("Player is full")
        self.assertFalse(run_sync(dev, self.show, work=work))
        self.assertIn("unmount", self.events)
        self.assertEqual(self.shown[-1], ("failed", ("--reason", "Player is full")))

    def test_whole_disk(self):
        self.assertEqual(dock_sync.whole_disk("/dev/sda1"), "/dev/sda")
        self.assertEqual(dock_sync.whole_disk("/dev/sdb"), "/dev/sdb")
        self.assertIsNone(dock_sync.whole_disk("/tmp/fake_ipod"))

    def test_sysinfo_guid_for_fake_devices(self):
        os.makedirs(os.path.join(self.tmp.name, "iPod_Control", "Device"))
        with open(os.path.join(self.tmp.name, "iPod_Control", "Device", "SysInfo"), "w") as f:
            f.write("ModelNumStr: xB903\nFirewireGuid: 0x000a270000000002\n")
        self.assertEqual(dock_sync.sysinfo_guid(self.tmp.name), "000A270000000002")


class RestTimerTests(unittest.TestCase):
    def test_schedules_ten_minute_reset_as_root(self):
        with mock.patch.object(dock_sync.os, "geteuid", return_value=0), \
                mock.patch.object(dock_sync.shutil, "which", return_value="/usr/bin/systemd-run"), \
                mock.patch.object(dock_sync.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stderr="")) as run:
            dock_sync.schedule_rest()
        cmd = run.call_args_list[-1][0][0]
        self.assertIn("--on-active=10min", cmd)
        self.assertEqual(cmd[-1], "musicdock-rest@timer.service")

    def test_no_timer_when_not_root(self):
        with mock.patch.object(dock_sync.os, "geteuid", return_value=1000), \
                mock.patch.object(dock_sync.subprocess, "run") as run:
            dock_sync.schedule_rest()
        run.assert_not_called()


class OptInTests(unittest.TestCase):
    """MediaDock.txt decides whether the dock touches a player at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def ipod(self, marker=None):
        os.makedirs(os.path.join(self.root, "iPod_Control"), exist_ok=True)
        if marker:
            open(os.path.join(self.root, marker), "w").close()

    def test_status(self):
        self.assertEqual(dock_sync.opt_in_status(self.root, known=True), "not_ipod")
        self.ipod()
        self.assertEqual(dock_sync.opt_in_status(self.root, known=False), "unmarked")
        self.assertEqual(dock_sync.opt_in_status(self.root, known=True), "known")
        self.ipod(marker="mediadock.TXT")                       # FAT: any case
        self.assertEqual(dock_sync.opt_in_status(self.root, known=False), "marked")

    def run_work(self, known, write=True):
        cfg = {"sync": {"cache_dir": self.root},
               "server": {"url": "https://x", "username": "u", "password": "p"}}
        fake_record = mock.Mock()
        fake_record.player.return_value = {"guid": "G"} if known else None
        dev = FakeDevice(self.root)
        dev.make_writable = mock.Mock()
        with mock.patch("dock_record.Record", return_value=fake_record), \
                mock.patch("ipod_handler.sync_player", return_value={"added": 1}) as sp:
            summary = dock_sync.make_ipod_work(cfg, "G", 0x1263, write)(dev, lambda *a, **k: None)
        return summary, sp, dev

    def test_unmarked_ipod_left_completely_alone(self):
        self.ipod()
        summary, sp, dev = self.run_work(known=False)
        self.assertEqual(summary, {"not_synced": "No MediaDock.txt on this iPod"})
        sp.assert_not_called()
        dev.make_writable.assert_not_called()

    def test_not_an_ipod(self):
        summary, sp, _ = self.run_work(known=False)
        self.assertEqual(summary, {"not_synced": "No MediaDock.txt on this player"})
        sp.assert_not_called()

    def test_generic_player_gets_files_only_under_its_own_id(self):
        # A plain music-folder player (no iPod_Control, no .rockbox) that opted in.
        open(os.path.join(self.root, "MediaDock.txt"), "w").close()
        with mock.patch("rockbox_handler.sync_player",
                        return_value={"added": 3, "plays_supported": False}) as rb:
            summary, sp, dev = self.run_work(known=False)
        sp.assert_not_called()
        dev.make_writable.assert_called_once()
        self.assertEqual(rb.call_args.kwargs["kind"], "generic")
        guid = rb.call_args.args[1]
        self.assertTrue(guid.startswith("SD") and len(guid) == 16)
        self.assertNotEqual(guid, "G")                          # not the USB serial
        with open(os.path.join(self.root, "MediaDock", ".musicdock", "player-id")) as f:
            self.assertEqual(f.read().strip(), guid)            # the same ID next time
        with open(os.path.join(self.root, "MediaDock README.txt")) as f:
            self.assertIn("No play count - unsupported", f.read())

    def test_generic_player_on_a_read_only_dock(self):
        open(os.path.join(self.root, "MediaDock.txt"), "w").close()
        with mock.patch("rockbox_handler.sync_player") as rb:
            summary, _, dev = self.run_work(known=False, write=False)
        rb.assert_not_called()
        self.assertEqual(summary, {"not_synced": "Dock is read-only (write = false)"})
        self.assertFalse(os.path.exists(os.path.join(self.root, "MediaDock")))

    def test_safe_screen_says_play_counts_are_unsupported(self):
        shown = []
        run_sync(RecordingDevice(self.root, []), lambda s, *a, force=False: shown.append((s,) + a),
                 work=lambda d, step: {"added": 2, "plays_supported": False})
        self.assertEqual(shown[-1][0], "safe")
        self.assertIn("--no-plays", shown[-1])

    def test_marked_ipod_is_made_writable_and_synced(self):
        self.ipod(marker="MediaDock.txt")
        summary, sp, dev = self.run_work(known=False)
        dev.make_writable.assert_called_once()
        self.assertEqual((summary["added"], summary["wifi"]), (1, None))   # no wifi.txt

    def test_known_ipod_gets_marker_when_writing(self):
        self.ipod()
        self.run_work(known=True, write=True)
        with open(os.path.join(self.root, "MediaDock.txt")) as f:
            self.assertIn("Delete this file to stop syncing", f.read())

    def test_known_ipod_read_only_writes_nothing(self):
        self.ipod()
        _, sp, dev = self.run_work(known=True, write=False)
        self.assertFalse(os.path.exists(os.path.join(self.root, "MediaDock.txt")))
        dev.make_writable.assert_not_called()
        sp.assert_called_once()                                 # plays still logged

    def test_mac_formatted_ipod_reads_plays_then_fails(self):
        self.ipod(marker="MediaDock.txt")
        dev = FakeDevice(self.root)
        dev.make_writable = mock.Mock()
        dev.fstype = mock.Mock(return_value="hfsplus")
        fake_record = mock.Mock()
        fake_record.player.return_value = None
        cfg = {"sync": {"cache_dir": self.root},
               "server": {"url": "https://x", "username": "u", "password": "p"}}
        with mock.patch("dock_record.Record", return_value=fake_record), \
                mock.patch("ipod_handler.sync_player", return_value={}) as sp, \
                self.assertRaisesRegex(dock_sync.SyncError, "Mac-formatted"):
            dock_sync.make_ipod_work(cfg, "G", 0x1261, True)(dev, lambda *a, **k: None)
        dev.make_writable.assert_not_called()
        self.assertFalse(sp.call_args.kwargs["write"])          # plays only

    def test_rockbox_player_goes_to_the_rockbox_handler(self):
        os.makedirs(os.path.join(self.root, ".rockbox"))         # e.g. a non-iPod Rockbox
        self.assertEqual(dock_sync.opt_in_status(self.root, known=False), "unmarked")
        open(os.path.join(self.root, "MediaDock.txt"), "w").close()
        with mock.patch("rockbox_handler.sync_player", return_value={"added": 2}) as rb:
            summary, sp, _ = self.run_work(known=False)
        rb.assert_called_once()
        sp.assert_not_called()
        self.assertEqual(summary["added"], 2)
        with open(os.path.join(self.root, "MediaDock README.txt")) as f:
            self.assertIn("This player runs Rockbox", f.read())

    def stock_on_flac_dock(self, setup=None):
        self.ipod(marker="MediaDock.txt")
        cfg = {"sync": {"cache_dir": self.root}, "device": {"type": "rockbox"},
               "server": {"url": "https://x", "username": "u", "password": "p"}}
        fake_record = mock.Mock()
        fake_record.player.return_value = None
        dev = FakeDevice(self.root)
        dev.make_writable = mock.Mock()
        with mock.patch("dock_record.Record", return_value=fake_record), \
                mock.patch("ipod_handler.sync_player", return_value={"added": 0}) as sp, \
                mock.patch.object(dock_sync, "handle_setup_file", return_value=setup):
            try:
                return dock_sync.make_ipod_work(cfg, "G", 0x1261, True)(
                    dev, lambda *a, **k: None), sp
            except dock_sync.SyncError as e:
                return e, sp

    def test_flac_dock_reads_a_stock_ipods_plays_but_leaves_its_music(self):
        result, sp = self.stock_on_flac_dock()
        self.assertIsInstance(result, dock_sync.SyncError)
        self.assertIn("set up for Rockbox", str(result))
        self.assertFalse(sp.call_args.kwargs["write"])            # plays only

    def test_stock_ipod_can_switch_compatibility_back_on(self):
        result, sp = self.stock_on_flac_dock(setup={
            "ok": True, "done": ["Stock iPods sync again (music converted)"],
            "reason": "", "stock_compatible": True})
        self.assertEqual(result["setup"]["stock_compatible"], True)   # DOCK UPDATED
        self.assertFalse(sp.call_args.kwargs["write"])

    def test_failed_setup_on_a_flac_dock_shows_its_reason(self):
        result, _ = self.stock_on_flac_dock(setup={"ok": False, "done": [],
                                                   "reason": "Unknown setting 'X'"})
        self.assertEqual(result["setup"]["reason"], "Unknown setting 'X'")

    def test_not_synced_screen_shown(self):
        events, shown = [], []
        dev = RecordingDevice(self.root, events)
        run_sync(dev, lambda s, *a, force=False: shown.append((s, a)),
                 work=lambda device, step: {"not_synced": "Not an iPod"})
        self.assertEqual(shown[-1], ("notsynced", ("--reason", "Not an iPod")))


class WifiFileSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "wifi.txt"), "w") as f:
            f.write("SSID=Cafe\nPassword=12345678\n")

    def test_read_only_reports_and_leaves_wifi_alone(self):
        result = dock_sync.handle_wifi_file(FakeDevice(self.tmp.name), False, None)
        self.assertEqual(result["reason"], "Dock is read-only (write = false)")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "wifi.txt")))

    def test_result_screen_carries_sync_counts(self):
        shown = []
        dev = RecordingDevice(self.tmp.name, [])
        run_sync(dev, lambda s, *a, force=False: shown.append((s, a)),
                 work=lambda d, step: {"added": 3, "plays_sent": 1, "wifi": {
                     "ok": False, "ssid": "Cafe", "reason": "Wrong password", "server": None}})
        screen, args = shown[-1]
        self.assertEqual(screen, "wififail")
        self.assertEqual(args[:6], ("--ssid", "Cafe", "--reason", "Wrong password",
                                    "--server", "unknown"))
        self.assertIn("3", args)


class SetupFileSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        os.environ["MUSICDOCK_RECORD"] = os.path.join(self.root, "dock.db")
        self.addCleanup(os.environ.pop, "MUSICDOCK_RECORD")
        os.makedirs(os.path.join(self.root, "iPod"))
        self.root_db, self.root = self.root, os.path.join(self.root, "iPod")
        from dock_record import Record
        rec = Record(os.environ["MUSICDOCK_RECORD"])
        for guid, name in (("000A270000000001", "New iPod"), ("000A270000000002", "Old iPod")):
            rec.upsert_player(guid)
            rec.set_player_name(guid, name)
            rec.mark_synced(guid)
        rec.close()

    def handle(self, write=True):
        from dock_record import Record
        path = os.environ["MUSICDOCK_RECORD"]
        with mock.patch("dock_record.Record", side_effect=lambda: Record(path)):
            return dock_sync.handle_setup_file(FakeDevice(self.root), write,
                                               "000A270000000001")

    def test_forgets_old_ipod_and_lists_them_in_the_marker(self):
        with open(os.path.join(self.root, "DockSetup.txt"), "w") as f:
            f.write("Forget=Old iPod\n")
        result = self.handle()
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "DockSetup.txt")))
        with open(os.path.join(self.root, dock_sync.MARKER)) as f:
            text = f.read()
        self.assertTrue(text.startswith(dock_sync.MARKER_TEXT))
        self.assertIn("New iPod", text)
        self.assertIn("this iPod", text)
        self.assertIn("forgotten", text)
        self.assertIn("Last DockSetup.txt", text)

    def test_marker_list_written_without_a_setup_file(self):
        self.assertIsNone(self.handle())
        with open(os.path.join(self.root, dock_sync.MARKER)) as f:
            self.assertIn("Old iPod", f.read())

    def test_read_only_leaves_everything_alone(self):
        with open(os.path.join(self.root, "DockSetup.txt"), "w") as f:
            f.write("Forget=Old iPod\n")
        result = self.handle(write=False)
        self.assertEqual(result["reason"], "Dock is read-only (write = false)")
        self.assertTrue(os.path.exists(os.path.join(self.root, "DockSetup.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, dock_sync.MARKER)))

    def test_result_screen(self):
        shown = []
        run_sync(RecordingDevice(self.root, []), lambda s, *a, force=False: shown.append((s, a)),
                 work=lambda d, step: {"added": 2, "setup": {
                     "ok": True, "done": ["Forgot Old iPod (model unknown)"], "reason": ""}})
        screen, args = shown[-1]
        self.assertEqual(screen, "setup")
        self.assertEqual(args[:2], ("--reason", "Forgot Old iPod (model unknown)"))


class PrepareAndSyncScreenTests(unittest.TestCase):
    """While the prepare job converts, a batch sync's copy progress shows as
    PREPARE & SYNC, counting the whole run; otherwise plain SYNCING."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        os.environ["DOCK_STATE_FILE"] = os.path.join(self.root, "state", "s.json")
        self.addCleanup(os.environ.pop, "DOCK_STATE_FILE")
        import dock_progress
        self.dp = dock_progress

    def sync(self, added=3):
        shown = []

        def work(device, step):
            for n in range(added):
                step(5, f"Copying {n + 1}/{added}", part_done=n, part_total=added)
            return {"added": added}
        run_sync(RecordingDevice(self.root, []),
                 lambda s, *a, force=False: shown.append((s,) + a), work=work)
        return shown

    def test_combined_screen_counts_every_batch_of_the_run(self):
        self.dp.write_prepare(run=111, done=32, total=167, batches=1)   # this process: alive
        first = [s for s in self.sync(added=25) if s[0] == "prepsync"]
        self.assertEqual(first[-1][:11], ("prepsync", "--done", "32", "--total", "167",
                                          "--synced", "25", "--to-copy", "167",
                                          "--batches", "1"))      # whole batch, stays up
        self.assertEqual(self.dp.copied(111), (25, 1))                  # batch 1 done
        second = self.sync(added=2)
        self.assertIn(("prepsync", "--done", "32", "--total", "167", "--synced", "26",
                       "--to-copy", "167", "--batches", "2"), second)
        self.assertEqual(self.dp.copied(111), (27, 2))

    def test_reused_songs_count_towards_synced_not_converting(self):
        # 14 re-added songs came straight from the cache (no conversion):
        # "Synced" counts to 73 + 14, "Converting" to 73 (seen 2026-10-23).
        self.dp.write_prepare(run=222, done=38, total=73, batches=1, to_copy=87)
        shown = [s for s in self.sync(added=39) if s[0] == "prepsync"]
        self.assertEqual(shown[-1][:11], ("prepsync", "--done", "38", "--total", "73",
                                          "--synced", "39", "--to-copy", "87",
                                          "--batches", "1"))

    def test_whole_run_shows_prepare_and_sync_until_its_last_sync(self):
        # User's choice: one screen from the first batch to the end of the
        # last sync, "Do not remove" throughout - no SYNCING, no SAFE between.
        self.dp.write_prepare(run=333, done=30, total=60, batches=1)
        first = self.sync(added=25)
        self.assertTrue(all(s[0] == "prepsync" for s in first))        # start to end
        self.dp.mark_final()                                            # conversion over
        last = self.sync(added=35)
        self.assertEqual(last[0][0], "prepsync")                        # before mounting
        self.assertEqual(last[-1][0], "safe")                           # the run is done
        self.assertIsNone(self.dp.running_prepare())                    # and forgotten
        self.assertEqual(self.dp.copied(333), (0, 0))

    def test_a_sync_that_asked_for_the_next_one_doesnt_end_the_run(self):
        # Seen 2026-10-23: conversion ended (run marked final) while batch 9
        # synced; that sync asked for a follow-up (+10) but showed SAFE.
        self.dp.write_prepare(run=555, done=235, total=235, batches=9)
        self.dp.mark_final()
        shown = []

        def work(device, step):
            step(5, "Copying 1/25", part_done=0, part_total=25)
            return {"added": 25, "followup": True}
        run_sync(RecordingDevice(self.root, []),
                 lambda s, *a, force=False: shown.append((s,) + a), work=work)
        self.assertEqual(shown[-1][0], "prepsync")                      # still the run
        self.assertIsNotNone(self.dp.running_prepare())
        last = self.sync(added=10)                                      # the follow-up
        self.assertEqual(last[-1][0], "safe")
        self.assertIsNone(self.dp.running_prepare())

    def test_final_mark_expires(self):
        self.dp.write_prepare(run=444, done=60, total=60, batches=2)
        self.dp.mark_final()
        self.assertIsNotNone(self.dp.running_prepare())
        with mock.patch.object(self.dp.time, "time",
                               return_value=time.time() + self.dp.FINAL_FOR + 1):
            self.assertIsNone(self.dp.running_prepare())               # iPod never synced

    def test_plain_syncing_once_conversion_is_over(self):
        self.dp.write_prepare(run=111, done=167, total=167, batches=2)
        self.dp.clear_prepare()                                           # run finished
        shown = self.sync()
        self.assertFalse(any(s[0] == "prepsync" for s in shown))

    def test_dead_prepare_job_doesnt_count(self):
        self.dp.write_prepare(run=111, done=5, total=50, batches=0)
        path = os.path.join(self.dp.folder(), self.dp.PREPARE_FILE)
        with open(path) as f:
            data = json.load(f)
        data["pid"] = 999999                                              # crashed run
        with open(path, "w") as f:
            json.dump(data, f)
        self.assertIsNone(self.dp.running_prepare())
        self.assertFalse(any(s[0] == "prepsync" for s in self.sync()))


class FollowupSyncTests(unittest.TestCase):
    """A sync that ends while newer batches are ready asks for the next one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        from dock_record import Record
        self.db = os.path.join(self.dir, "dock.db")
        rec = Record(self.db)
        rec.add_track("G", 1, navidrome_id="a")             # what the sync just copied
        rec.close()
        self.cfg = {"sync": {"cache_dir": self.dir}, "device": {"auto_resync": True}}

    def plan(self, generated, ids):
        return {"generated": generated, "tracks": [{"id": i} for i in ids], "skipped": []}

    def followup(self, applied, newest):
        with open(os.path.join(self.dir, "manifest.json"), "w") as f:
            json.dump(newest, f)
        from dock_record import Record
        with mock.patch("dock_record.Record", side_effect=lambda: Record(self.db)), \
                mock.patch("dock_resync.request") as request:
            asked = dock_sync.ask_for_followup(self.cfg, applied, "G")
        self.assertEqual(asked, request.called)
        return asked

    def test_newer_plan_with_more_music_asks_for_the_next_sync(self):
        self.assertTrue(self.followup(self.plan("t1", ["a"]), self.plan("t2", ["a", "b", "c"])))

    def test_same_plan_never_loops(self):
        # e.g. the iPod was full or a file was missing: nothing new to try.
        self.assertFalse(self.followup(self.plan("t1", ["a", "b"]), self.plan("t1", ["a", "b"])))

    def test_newer_plan_with_nothing_new_for_this_ipod(self):
        self.assertFalse(self.followup(self.plan("t1", ["a"]), self.plan("t2", ["a"])))

    def test_auto_resync_off(self):
        self.cfg["device"]["auto_resync"] = False
        self.assertFalse(self.followup(self.plan("t1", ["a"]), self.plan("t2", ["a", "b"])))


class ScreenProcessTests(unittest.TestCase):
    """Screen must draw in a separate process (GPIO is released on exit)."""

    def test_draws_in_a_child_process(self):
        with mock.patch.object(dock_sync.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            dock_sync.Screen().show("syncing", "--step", "1")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[1:], [dock_sync.DOCK_STATUS, "syncing", "--step", "1"])

    def test_failed_draw_raises_so_sync_wont_mount(self):
        with mock.patch.object(dock_sync.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            with self.assertRaises(SyncError):
                dock_sync.Screen().show("syncing", force=True)


class ListingTests(unittest.TestCase):
    def test_counts_files_and_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "iPod_Control", "Music", "F00"))
            for name, size in [("a.m4a", 100), ("b.m4a", 50)]:
                with open(os.path.join(d, "iPod_Control", "Music", "F00", name), "wb") as f:
                    f.write(b"x" * size)
            self.assertEqual(dock_sync.list_contents(d, limit=1), (2, 150))


class DetectionTests(unittest.TestCase):
    # Trimmed `lsblk --json --paths -o NAME,TRAN,FSTYPE,MOUNTPOINT` from a Pi
    # with its SD card plus a USB player. Partitions have no "tran" of their
    # own, so they must inherit it from the disk.
    LSBLK = {"blockdevices": [
        {"name": "/dev/sda", "tran": "usb", "fstype": None, "mountpoint": None,
         "children": [
             {"name": "/dev/sda1", "tran": None, "fstype": "vfat", "mountpoint": None},
         ]},
        {"name": "/dev/mmcblk0", "tran": None, "fstype": None, "mountpoint": None,
         "children": [
             {"name": "/dev/mmcblk0p1", "tran": None, "fstype": "vfat",
              "mountpoint": "/boot/firmware"},
             {"name": "/dev/mmcblk0p2", "tran": None, "fstype": "ext4",
              "mountpoint": "/"},
         ]},
    ]}

    def test_finds_only_usb_partition(self):
        found = dock_sync.find_usb_filesystems(self.LSBLK)
        self.assertEqual([d["path"] for d in found], ["/dev/sda1"])

    def test_detect_one_errors_when_nothing_plugged_in(self):
        with mock.patch.object(dock_sync, "find_usb_filesystems", return_value=[]):
            with self.assertRaises(SyncError):
                dock_sync.detect_one()


try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "Pillow not installed (pip install pillow)")
class StatusGuardTests(unittest.TestCase):
    """dock_status.py: the prepare job must never draw over SYNCING."""

    def setUp(self):
        import dock_status
        self.ds = dock_status
        self.tmp = tempfile.TemporaryDirectory()
        self.drawn = []
        patches = [
            mock.patch.object(dock_status, "STATE_FILE",
                              os.path.join(self.tmp.name, "state.json")),
            mock.patch.object(dock_status, "draw_on_panel",
                              lambda img, full_refresh: self.drawn.append(img)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        dock_status.main(["syncing", "--wifi", "off"])
        self.drawn.clear()

    def test_prepare_screens_cannot_cover_prepare_and_sync(self):
        self.ds.main(["prepsync", "--done", "32", "--total", "167", "--synced", "25",
                      "--batches", "1", "--wifi", "off", "--force"])
        self.drawn.clear()
        self.ds.main(["idle", "--wifi", "off", "--ip", "10.0.0.2"])
        self.ds.main(["preparing", "--wifi", "off"])
        self.assertEqual(self.drawn, [])

    def test_idle_cannot_cover_syncing(self):
        self.ds.main(["idle", "--wifi", "off", "--ip", "10.0.0.2"])
        self.ds.main(["preparing", "--wifi", "off"])
        self.assertEqual(self.drawn, [])

    def test_partial_stream_draws_each_line(self):
        import io
        lines = io.StringIO('["starting", "--uptime", "70", "--ip", "", "--wifi", "off"]\n'
                            '["starting", "--uptime", "80", "--ip", "10.0.0.2", "--wifi", "-50"]\n')
        with mock.patch.object(sys, "stdin", lines):
            self.ds.stream_main(["--preview-dir", self.tmp.name])
        frames = sorted(f for f in os.listdir(self.tmp.name) if f.endswith(".png"))
        self.assertEqual(frames, ["01-starting.png", "02-starting.png"])

    def test_force_and_sync_screens_can_replace_syncing(self):
        self.ds.main(["idle", "--force", "--wifi", "off", "--ip", "10.0.0.2"])
        self.assertEqual(len(self.drawn), 1)
        self.ds.main(["syncing", "--force", "--wifi", "off"])
        self.ds.main(["safe", "--wifi", "off"])
        self.assertEqual(len(self.drawn), 3)


if __name__ == "__main__":
    unittest.main()
