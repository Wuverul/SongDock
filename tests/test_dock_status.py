"""Tests for dock_status.py's Wi-Fi refresh (needs Pillow; runs on the Pi).

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import dock_status
except ImportError:            # Pillow missing (e.g. on the Mac)
    dock_status = None


@unittest.skipIf(dock_status is None, "Pillow not installed (pip install pillow)")
class WifiRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.draws = []
        self.rendered = []
        self.wifi = -50
        real_render = dock_status.render

        def render(screen, args):
            self.rendered.append(args)
            return real_render(screen, args)

        for p in (
            mock.patch.object(dock_status, "STATE_FILE", os.path.join(tmp.name, "state.json")),
            mock.patch.object(dock_status, "draw_on_panel",
                              lambda img, full_refresh: self.draws.append(full_refresh)),
            mock.patch.object(dock_status, "read_wifi", lambda: self.wifi),
            mock.patch.object(dock_status, "render", render),
        ):
            p.start()
            self.addCleanup(p.stop)

    def refresh(self):
        dock_status.refresh_main(settle=0)

    def test_draw_records_what_to_redraw(self):
        dock_status.main(["safe", "--added", "3", "--force",
                          "--only-over", "none,idle", "--ip", "10.0.0.2"])
        state = dock_status.load_state()
        self.assertEqual(state["argv"], ["safe", "--added", "3"])
        self.assertTrue(state["wifi_on"])

    def test_wifi_drop_redraws_with_original_time(self):
        dock_status.main(["idle", "--boot"])
        drawn_at = dock_status.load_state()["drawn_at"]
        self.draws.clear()
        self.rendered.clear()
        self.wifi = "off"                                  # connection lost
        self.refresh()
        self.assertEqual(len(self.draws), 1)
        self.assertFalse(self.draws[0])                    # quick refresh, no flash
        args = self.rendered[-1]
        self.assertEqual((args.screen, args.boot, args.wifi, args.at),
                         ("idle", True, "off", drawn_at))
        self.assertFalse(dock_status.load_state()["wifi_on"])

        self.wifi = -60                                    # reconnected
        self.refresh()
        self.assertEqual(len(self.draws), 2)
        self.assertTrue(dock_status.load_state()["wifi_on"])

    def test_signal_change_alone_doesnt_redraw(self):
        dock_status.main(["safe"])
        self.draws.clear()
        self.wifi = -80                                    # weaker, still connected
        self.refresh()
        self.assertEqual(self.draws, [])

    def test_active_screens_are_left_alone(self):
        dock_status.main(["syncing", "--step", "1", "--total", "3"])
        self.draws.clear()
        self.wifi = "off"
        self.refresh()
        self.assertEqual(self.draws, [])


@unittest.skipIf(dock_status is None, "Pillow not installed (pip install pillow)")
class RestScreenTests(WifiRefreshTests):
    """SAFE/FAILED -> DOCK READY after an unplug or the 10-minute timer."""

    def rest(self, mode, waited=None):
        record = waited.append if waited is not None else (lambda s: None)
        dock_status.rest_main(["--mode", mode], sleep=record)

    def test_timer_turns_safe_into_ready_with_summary(self):
        dock_status.main(["safe", "--added", "79", "--removed", "0", "--plays", "5"])
        self.rest("timer")
        args = self.rendered[-1]
        self.assertEqual(args.screen, "idle")
        self.assertIn("Last synced", args.detail)
        self.assertIn("+79", args.detail)
        self.assertIn("5 plays", args.detail)
        self.assertEqual(dock_status.load_state()["screen"], "idle")

    def test_timer_leaves_failed_up(self):
        dock_status.main(["failed", "--reason", "Player is full"])
        self.draws.clear()
        self.rest("timer")
        self.assertEqual(self.draws, [])

    def test_unplug_clears_failed_but_keeps_the_news(self):
        dock_status.main(["failed", "--reason", "Player is full"])
        self.rest("unplug")
        self.assertIn("Last sync failed", self.rendered[-1].detail)

    def test_unplug_keeps_result_up_for_a_minute(self):
        dock_status.main(["safe"])
        waited = []
        self.rest("unplug", waited)
        self.assertEqual(len(waited), 1)
        self.assertGreater(waited[0], 50)

    def test_never_covers_a_running_sync(self):
        dock_status.main(["syncing", "--step", "2", "--total", "6"])
        self.draws.clear()
        self.rest("unplug")
        self.rest("timer")
        self.assertEqual(self.draws, [])


if __name__ == "__main__":
    unittest.main()
