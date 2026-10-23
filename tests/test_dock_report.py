"""Tests for the status report. Run: python3 -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_report  # noqa: E402
from dock_record import Record  # noqa: E402

T0 = 1_790_000_000


def ev(offset, msg):
    return (T0 + offset, msg)


class JournalParseTests(unittest.TestCase):
    def test_short_unix_lines(self):
        text = ("-- Boot abc --\n"
                "1790000000.123456 dock systemd[1]: Starting musicdock-prepare.service - x...\n"
                "1790000001.5 dock python3[42]: INFO Done: 3/3 ready (1 added, 0 removed, 0 skipped)\n"
                "1790000002.0 dock python3[42]: INFO Target: a: b\n")
        out = dock_report.parse_journal(text)
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(out[0][0], 1790000000.123456)
        self.assertTrue(out[0][1].startswith("Starting musicdock-prepare"))
        self.assertEqual(out[2][1], "INFO Target: a: b")      # keeps later colons


class PrepareSummaryTests(unittest.TestCase):
    def test_counts_checks_and_changes(self):
        s = dock_report.summarize_prepare([
            ev(0, "Starting musicdock-prepare.service - SongDock prepare"),
            ev(5, "INFO Done: 10/10 ready (2 added, 1 removed, 0 skipped)"),
            ev(300, "Starting musicdock-prepare.service - SongDock prepare"),
            ev(305, "INFO Done: 10/10 ready (0 added, 3 removed, 0 skipped)"),
        ])
        self.assertEqual((s["checks"], s["added"], s["removed"]), (2, 2, 4))
        self.assertEqual(s["last_done"][0], T0 + 305)
        self.assertIsNone(s["batch"])

    def test_running_batch_estimates_time_left(self):
        s = dock_report.summarize_prepare([
            ev(0, "Starting musicdock-prepare.service - x"),
            ev(1, "INFO Getting 100 tracks with 1 conversion worker(s)"),
            ev(10, "INFO Screen: PREPARING --done 0 --total 100 --detail A --only-over x"),
            ev(610, "INFO Screen: PREPARING --done 10 --total 100 --detail B --only-over x"),
        ])
        b = s["batch"]
        self.assertEqual((b["done"], b["total"]), (10, 100))
        self.assertAlmostEqual(b["per_track"], 60)
        self.assertAlmostEqual(b["left"], 60 * 90)

    def test_finished_batch_is_not_reported_as_running(self):
        s = dock_report.summarize_prepare([
            ev(1, "INFO Getting 2 tracks with 1 conversion worker(s)"),
            ev(2, "INFO Screen: PREPARING --done 0 --total 2 --detail A"),
            ev(9, "INFO Done: 2/2 ready (2 added, 0 removed, 0 skipped)"),
        ])
        self.assertIsNone(s["batch"])

    def test_outage_counted_once_per_check(self):
        s = dock_report.summarize_prepare([
            ev(0, "Starting musicdock-prepare.service - x"),
            ev(1, "ERROR Couldn't prepare A"),
            ev(1, "subsonic.SubsonicError: download: can't reach server (name resolution)"),
            ev(2, "ERROR Couldn't prepare B"),
            ev(2, "subsonic.SubsonicError: download: can't reach server (name resolution)"),
            ev(300, "Starting musicdock-prepare.service - x"),
            ev(301, "WARNING Server unreachable (x); trying again at the next check"),
        ])
        self.assertEqual(s["outage_runs"], 2)


class ErrorFilterTests(unittest.TestCase):
    def test_outage_errors_are_not_problems(self):
        events = [
            ev(0, "ERROR Couldn't prepare A"),
            ev(0.2, "Traceback (most recent call last):"),
            ev(0.3, "subsonic.SubsonicError: download: can't reach server (x)"),
            ev(100, "ERROR Couldn't prepare B"),
            ev(100.1, "ValueError: bad file"),
        ]
        self.assertEqual(dock_report.real_errors(events), [(T0 + 100, "Couldn't prepare B")])


class SyncSummaryTests(unittest.TestCase):
    def test_session_with_summary_and_outcome(self):
        sessions = dock_report.summarize_syncs([
            ev(-5, "INFO Screen: SAFE (left over from before the window)"),
            ev(0, "Starting musicdock-sync@sda.service - SongDock sync"),
            ev(60, "INFO Session summary: {'plays_new': 3, 'plays_sent': 3, 'added': 5, "
                   "'removed': 1, 'wrote': True, 'wifi': None}"),
            ev(61, "INFO Screen: SAFE --only-over x"),
            ev(500, "Starting musicdock-sync@sda.service - SongDock sync"),
            ev(530, "INFO Screen: FAILED --detail Unknown iPod model"),
        ])
        self.assertEqual(len(sessions), 2)
        text = dock_report.describe_session(sessions[0])
        self.assertIn("OK", text)
        self.assertIn("+5 -1 tracks", text)
        self.assertIn("3 plays read, 3 sent", text)
        self.assertIn("FAILED", dock_report.describe_session(sessions[1]))

    def test_session_still_running(self):
        sessions = dock_report.summarize_syncs([ev(0, "Starting musicdock-sync@sda.service")])
        self.assertIn("running", dock_report.describe_session(sessions[0]))


class ProblemTests(unittest.TestCase):
    def test_healthy_dock_has_no_problems(self):
        self.assertEqual(dock_report.problems(
            {"throttled": "0x0", "temp": 40, "disk_pct": 16, "cache_pct": 24,
             "plays_waiting": 0, "errors": [], "failed_units": []}), [])

    def test_flags_what_needs_attention(self):
        now = T0 + 3 * 86400
        found = dock_report.problems(
            {"throttled": "0x50005", "temp": 75, "disk_pct": 80, "cache_pct": 95,
             "plays_waiting": 4, "oldest_waiting": T0, "failed_units": ["musicdock-prepare.service"],
             "failed_syncs": [{"started": T0}], "errors": [(T0, "Couldn't prepare A")]},
            now=now)
        text = "\n".join(found)
        for expected in ("musicdock-prepare.service", "0x50005", "hot", "SD card 80%",
                         "Cache at 95%", "4 plays waiting", "Sync failed", "Couldn't prepare A"):
            self.assertIn(expected, text)

    def test_recent_waiting_plays_are_normal(self):
        self.assertEqual(dock_report.problems(
            {"plays_waiting": 4, "oldest_waiting": T0}, now=T0 + 3600), [])


class IpodSectionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "dock.db")

    def test_never_synced(self):
        lines, _ = dock_report.ipod_section(self.path, {})
        self.assertEqual(lines, ["iPod: never synced"])
        self.assertFalse(os.path.exists(self.path))      # read-only: never creates it

    def test_reports_tracks_pending_changes_and_plays(self):
        rec = Record(self.path)
        rec.upsert_player("000A2700ABCD1234", "xB903")
        rec.mark_synced("000A2700ABCD1234")
        for n, nid in enumerate(["a", "b"]):
            rec.add_track("000A2700ABCD1234", n + 1, navidrome_id=nid, title=nid)
        rec.queue_plays("000A2700ABCD1234", [
            {"dbid": 1, "played_at": T0, "navidrome_id": "a"},
            {"dbid": 9, "played_at": T0 + 1, "navidrome_id": None}])
        rec.close()
        manifest = {"tracks": [{"id": "a"}, {"id": "c"}, {"id": "d"}], "skipped": []}
        lines, facts = dock_report.ipod_section(self.path, manifest)
        text = "\n".join(lines)
        self.assertIn("...1234 (xB903)", text)
        self.assertIn("2 tracks", text)
        self.assertIn("+2 -1 tracks", text)
        self.assertIn("1 waiting to send, 1 unmatched", text)
        self.assertEqual(facts["plays_waiting"], 1)


class ScreenSectionTests(unittest.TestCase):
    def test_preparing_shows_current_track(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"screen": "preparing", "last_draw": 1, "argv": ["preparing", "--done", '
                    '"4", "--total", "9", "--detail", "Converting: A - B"]}')
        self.addCleanup(os.unlink, f.name)
        lines, _ = dock_report.screen_section(f.name)
        self.assertIn("PREPARING track 5 of 9 · Converting: A - B", lines[0])

    def test_missing_state_file(self):
        self.assertEqual(dock_report.screen_section("/nonexistent/x.json")[0],
                         ["Screen: unknown"])


if __name__ == "__main__":
    unittest.main()
