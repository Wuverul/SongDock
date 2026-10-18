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
class DarkModeTests(unittest.TestCase):
    def render(self, invert):
        args = dock_status.build_parser().parse_args(
            ["safe", "--added", "3", "--wifi", "-50", "--at", "1792195000"])
        with mock.patch.object(dock_status, "display_settings",
                               return_value={"invert": invert}):
            return dock_status.render("safe", args)

    def test_dark_mode_is_the_exact_inverse(self):
        light, dark = self.render(False), self.render(True)
        self.assertEqual(light.size, dark.size)
        self.assertTrue(all(a != b for a, b in zip(light.getdata(), dark.getdata())))
        self.assertEqual(dark.getpixel((125, 60)), 0)       # background now black


@unittest.skipIf(dock_status is None, "Pillow not installed (pip install pillow)")
class SyncingBarTests(unittest.TestCase):
    def progress(self, *argv):
        return dock_status.syncing_progress(dock_status.build_parser().parse_args(
            ["syncing", *argv]))

    def test_bar_follows_tracks_within_the_copy_step(self):
        self.assertEqual(self.progress("--step", "5", "--total", "6",
                                       "--part-done", "2", "--part-total", "79"), (2, 79))

    def test_bar_shows_stage_when_step_has_no_count(self):
        self.assertEqual(self.progress("--step", "2", "--total", "6"), (2, 6))

    def test_no_bar_without_numbers(self):
        self.assertEqual(self.progress("--detail", "Unmount failed"), (0, 0))


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
        first, second = args.detail.split("\n")             # two lines: nothing cut off
        self.assertIn("Last synced", first)
        self.assertIn("+79", second)
        self.assertIn("5 plays", second)
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

    def test_not_synced_clears_on_unplug_or_timer(self):
        for mode in ("unplug", "timer"):
            dock_status.main(["notsynced", "--reason", "No MediaDock.txt on this iPod"])
            self.rest(mode)
            self.assertEqual(dock_status.load_state()["screen"], "idle")

    def test_wifi_results_clear_like_safe_and_failed(self):
        dock_status.main(["wifi", "--ssid", "Cafe", "--added", "3"])
        self.rest("timer")
        self.assertEqual(dock_status.load_state()["screen"], "idle")
        self.assertIn("+3", self.rendered[-1].detail)
        dock_status.main(["wififail", "--ssid", "Cafe", "--reason", "Wrong password"])
        self.draws.clear()
        self.rest("timer")                                   # stays up: you should see it
        self.assertEqual(self.draws, [])
        self.rest("unplug")
        self.assertEqual(dock_status.load_state()["screen"], "idle")

    def test_never_covers_a_running_sync(self):
        dock_status.main(["syncing", "--step", "2", "--total", "6"])
        self.draws.clear()
        self.rest("unplug")
        self.rest("timer")
        self.assertEqual(self.draws, [])


class FakeEPD:
    """Records what draw_on_panel asks of the Waveshare driver."""

    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def getbuffer(self, img):
        return img.tobytes()

    def init(self):
        self.log.append("init")

    def send_command(self, c):
        self.log.append(f"cmd {c:#x}")

    def send_data2(self, data):
        self.log.append(("data", bytes(data)))

    def display(self, buf):
        if self.fail:
            raise OSError("SPI error")
        self.log.append("full")

    def displayPartial(self, buf):
        self.log.append("partial")

    def sleep(self):
        self.log.append("sleep")


@unittest.skipIf(dock_status is None, "Pillow not installed (pip install pillow)")
class PartialRefreshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = []
        self.fail = False
        for p in (
            mock.patch.object(dock_status, "STATE_FILE", os.path.join(tmp.name, "state.json")),
            mock.patch.object(dock_status, "open_panel",
                              lambda: FakeEPD(self.log, self.fail)),
            mock.patch.object(dock_status, "read_wifi", lambda: -50),
        ):
            p.start()
            self.addCleanup(p.stop)

    def frame(self, n):
        from PIL import Image, ImageDraw
        img = Image.new("1", (250, 122), 1)
        ImageDraw.Draw(img).rectangle((0, 0, n, 10), fill=0)
        return img

    def steps(self):
        return [e for e in self.log if isinstance(e, str) and e != "init"]

    def test_first_draw_is_full_then_partial_from_saved_frame(self):
        dock_status.draw_on_panel(self.frame(5), full_refresh=False)
        self.assertEqual(self.steps(), ["full", "sleep"])      # nothing saved yet
        self.log.clear()
        dock_status.draw_on_panel(self.frame(50), full_refresh=False)
        self.assertEqual(self.steps(), ["cmd 0x26", "partial", "sleep"])
        old = dock_status.panel_buffer(FakeEPD([]), self.frame(5))
        self.assertIn(("data", bytes(old)), self.log)          # 0x26 = what's shown

    def test_full_refresh_when_asked(self):
        dock_status.draw_on_panel(self.frame(5), full_refresh=False)
        self.log.clear()
        dock_status.draw_on_panel(self.frame(50), full_refresh=True)
        self.assertEqual(self.steps(), ["full", "sleep"])

    def test_failed_draw_forgets_the_frame(self):
        dock_status.draw_on_panel(self.frame(5), full_refresh=False)
        self.fail = True
        with self.assertRaises(OSError):
            dock_status.draw_on_panel(self.frame(50), full_refresh=True)
        self.assertIn("sleep", self.log)                       # still put to sleep
        self.assertIsNone(dock_status.load_frame((250, 122)))  # panel state unknown
        self.fail = False
        self.log.clear()
        dock_status.draw_on_panel(self.frame(60), full_refresh=False)
        self.assertEqual(self.steps(), ["full", "sleep"])

    def test_frame_lives_in_ram_folder_on_the_pi(self):
        with tempfile.TemporaryDirectory() as shared, tempfile.TemporaryDirectory() as run:
            with mock.patch.object(dock_status, "SHARED_STATE_DIR", shared), \
                    mock.patch.object(dock_status, "RUNTIME_DIR", run), \
                    mock.patch.object(dock_status, "STATE_FILE",
                                      os.path.join(shared, "dock_status.json")):
                dock_status.draw_on_panel(self.frame(5), full_refresh=False)
                self.assertEqual(os.listdir(run), ["dock_status.frame.png"])
                self.assertEqual(os.listdir(shared), [])      # nothing on the SD card
            with mock.patch.object(dock_status, "SHARED_STATE_DIR", shared), \
                    mock.patch.object(dock_status, "RUNTIME_DIR", run + "-missing"), \
                    mock.patch.object(dock_status, "STATE_FILE",
                                      os.path.join(shared, "dock_status.json")):
                self.assertTrue(dock_status.frame_path().startswith(shared))  # fallback

    def test_connecting_screen_renders(self):
        args = dock_status.build_parser().parse_args(["connecting", "--wifi", "-50"])
        img = dock_status.render("connecting", args)
        self.assertEqual(img.size, (250, 122))

    def test_sync_progress_flashes_only_on_screen_change_and_every_n(self):
        draws = []
        real = dock_status.draw_on_panel
        with mock.patch.object(dock_status, "draw_on_panel",
                               lambda img, full_refresh: (draws.append(full_refresh),
                                                          real(img, full_refresh))):
            with mock.patch.object(dock_status, "MIN_PROGRESS_INTERVAL", 0):
                for n in range(dock_status.FULL_REFRESH_EVERY + 2):
                    dock_status.main(["syncing", "--step", "5", "--total", "6",
                                      "--detail", f"Copying {n}/40"])
        self.assertTrue(draws[0])                              # new screen: full
        self.assertEqual(draws[1:dock_status.FULL_REFRESH_EVERY], [False] *
                         (dock_status.FULL_REFRESH_EVERY - 1))
        self.assertTrue(draws[dock_status.FULL_REFRESH_EVERY])  # anti-ghost


if __name__ == "__main__":
    unittest.main()
